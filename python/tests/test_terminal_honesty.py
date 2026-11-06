"""The terminal's honesty rules, pinned.

Every assertion here is a behaviour that would be INVISIBLE if it regressed:
a missing number silently becoming zero, a filter quietly hiding the rows it
could not judge, a chart bridging a hole in the tape. Green screenshots do not
catch any of them, which is exactly why they are unit tests.

Pure logic only — no network, no clock. These run in CI.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal as t
import terminal_trade as tt
import us_api as us


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """An isolated DB carrying the real schema — including the terminal's own
    tables, which is what the cost-basis tests below read."""
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "terminal.db")
    db.init_db()
    return tmp_path


# ────────────────────────────────────────────────────────────────────────
# THE ONE RULE: a value nobody could produce is None, never 0.
# ────────────────────────────────────────────────────────────────────────


def test_num_returns_none_for_unknowable_input():
    for bad in (None, "", "abc", [], {}, float("nan"), float("inf")):
        assert t.num(bad) is None, f"{bad!r} was coerced to a number"


def test_num_preserves_a_real_zero():
    # The mirror image of the rule above, and the easier one to break: a
    # market that genuinely traded 0 is NOT the same as one we could not read,
    # and collapsing them loses the distinction the whole product rests on.
    assert t.num(0) == 0.0
    assert t.num("0") == 0.0
    assert t.num(0.0) == 0.0


def test_num_rejects_bools():
    # bool is an int subclass in Python, so `float(True) == 1.0` sails through
    # a naive implementation and a JSON `true` silently becomes a price of 1.
    assert t.num(True) is None
    assert t.num(False) is None


def test_cents_scales_and_preserves_none():
    assert t.cents(0.42) == 42.0
    assert t.cents(1) == 100.0
    assert t.cents(None) is None
    assert t.cents("") is None


# ────────────────────────────────────────────────────────────────────────
# The merge: a null must never erase a better answer.
# ────────────────────────────────────────────────────────────────────────


def test_row_put_ignores_none():
    r = t._Row()
    r.put("probability", 42.0, "gateway", t.PRIO_BEST_SOURCE)
    r.put("probability", None, "clob-ws", t.PRIO_OWN_FEED)
    assert r.get("probability") == 42.0, "a None erased a real value"
    assert r.sources["probability"]["source"] == "gateway"


def test_row_put_prefers_better_priority():
    r = t._Row()
    r.put("bid", 40.0, "gateway", t.PRIO_FALLBACK)
    r.put("bid", 41.0, "clob-ws", t.PRIO_OWN_FEED)
    assert r.get("bid") == 41.0
    assert r.sources["bid"]["source"] == "clob-ws"


def test_row_put_does_not_downgrade():
    r = t._Row()
    r.put("bid", 41.0, "clob-ws", t.PRIO_OWN_FEED)
    r.put("bid", 40.0, "gateway", t.PRIO_FALLBACK)
    assert r.get("bid") == 41.0, "a worse provider overwrote a better one"


def test_unknown_reason_is_dropped_once_the_field_is_known():
    r = t._Row()
    r.unknown("liquidity", "The venue reported nothing.")
    assert r.unknowns and r.unknowns[0]["field"] == "liquidity"
    r2 = t._Row()
    r2.put("liquidity", 10.0, "gateway", t.PRIO_BEST_SOURCE)
    r2.unknown("liquidity", "should not appear")
    assert not r2.unknowns


def _raw_market(**over):
    raw = {"slug": "cpc-abc", "question": "Live?", "active": True, "closed": False,
           "status": "MARKET_STATUS_OPEN"}
    raw.update(over)
    return raw


@pytest.mark.parametrize("quotes", [
    {},
    {"bestBidQuote": {"value": "0"}, "bestAskQuote": {"value": "1"}},
])
def test_an_empty_side_is_not_a_price(quotes):
    """A market with nobody resting has no quote, and neither 0 nor 1 is a price
    anyone could trade. Passing them through produced "bid 0c, ask 100c, spread
    100c" on every freshly-listed market, which is a specific and confident lie."""
    m = t.summary_from_gamma(_raw_market(**quotes))
    assert m["bid"] is None, "0c was reported as a real bid"
    assert m["ask"] is None, "100c was reported as a real ask"
    assert m["spread"] is None, "an empty book produced a spread"
    assert any(u["field"] in ("bid", "ask") for u in m["unknowns"]), \
        "the missing side was not explained"


def test_real_quotes_survive_the_sentinel_filter():
    m = t.summary_from_gamma(_raw_market(
        bestBidQuote={"value": "0.40", "currency": "USD"},
        bestAskQuote={"value": "0.42", "currency": "USD"},
        outcomePrices='["0.41", "0.59"]'))
    assert m["bid"] == 40.0 and m["ask"] == 42.0
    assert abs(m["spread"] - 2.0) < 1e-9
    assert abs(m["mid"] - 41.0) < 1e-9


@pytest.mark.parametrize("prices,want", [('["1", "0"]', "yes"), ('["0", "1"]', "no")])
def test_a_settled_market_reports_its_outcome(prices, want):
    m = t.summary_from_gamma(_raw_market(
        active=False, closed=True, status="MARKET_STATUS_RESOLVED", outcomePrices=prices))
    assert m["resolvedOutcome"] == want
    assert m["status"] == "settled"


def test_a_market_past_expiry_with_no_settlement_has_no_outcome_yet():
    """RESOLVING is "past expiry, final settlement not yet published". Prices that
    happen to sit at 1 and 0 there are not a result."""
    m = t.summary_from_gamma(_raw_market(
        status="MARKET_STATUS_RESOLVING", outcomePrices='["1", "0"]'))
    assert m["resolvedOutcome"] is None
    assert m["status"] != "settled"


# ────────────────────────────────────────────────────────────────────────
# Filters — the deliberate asymmetry.
#
#   NUMERIC  : unknown is SKIPPED   (a threshold on a quantity that has not
#              arrived is not a judgement about that row)
#   CATEGORY : unknown is EXCLUDED  (the user named a set, and "I do not
#              know" is not a member of it)
# ────────────────────────────────────────────────────────────────────────

ROWS = [
    {"title": "known-pass", "volume24hUsd": 5000.0, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": "crypto"},
    {"title": "known-fail", "volume24hUsd": 10.0, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": "crypto"},
    {"title": "unknown-vol", "volume24hUsd": None, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": "crypto"},
    {"title": "unknown-cat", "volume24hUsd": 5000.0, "liquidityUsd": 100.0,
     "probability": 50.0, "hoursToClose": 5.0, "category": None},
]


def test_numeric_filter_skips_unknown_rather_than_failing_it():
    kept, skipped, excluded = t.apply_filters(ROWS, {"minVolume24hUsd": 1000})
    titles = {r["title"] for r in kept}
    assert titles == {"known-pass", "unknown-cat"}
    # 'known-fail' failed the test; 'unknown-vol' could not be tested. Both are
    # dropped, but only one of them is a JUDGEMENT — and the counters say so.
    assert skipped == 2
    assert excluded == 0


def test_categorical_filter_excludes_unknown():
    kept, skipped, excluded = t.apply_filters(ROWS, {"categories": ["crypto"]})
    titles = {r["title"] for r in kept}
    assert titles == {"known-pass", "known-fail", "unknown-vol"}
    assert excluded == 1, "an uncategorised row survived a category filter"
    assert skipped == 0


def test_categorical_and_numeric_counters_do_not_double_count():
    # A row excluded by category must not ALSO land in the skipped bucket:
    # the two counters are shown separately and have to add up.
    kept, skipped, excluded = t.apply_filters(
        ROWS, {"categories": ["crypto"], "minVolume24hUsd": 1000})
    assert [r["title"] for r in kept] == ["known-pass"]
    assert excluded == 1          # unknown-cat
    assert skipped == 2           # known-fail, unknown-vol
    assert len(kept) + skipped + excluded == len(ROWS)


def test_no_filters_keeps_everything():
    kept, skipped, excluded = t.apply_filters(ROWS, None)
    assert len(kept) == len(ROWS) and skipped == 0 and excluded == 0


def test_probability_bounds_skip_unpriced_rows():
    rows = [{"probability": None, "category": "x"}, {"probability": 80.0, "category": "x"}]
    kept, skipped, _ = t.apply_filters(rows, {"minProbability": 50})
    assert len(kept) == 1 and skipped == 1


# ────────────────────────────────────────────────────────────────────────
# Series normalisation.
# ────────────────────────────────────────────────────────────────────────


def test_history_is_sorted_and_deduped():
    # Providers really do emit the same bucket twice in one response, and
    # every charting library throws on a non-ascending series.
    pts = t._dedupe_points([
        {"t": 3, "p": 30.0}, {"t": 1, "p": 10.0},
        {"t": 3, "p": 33.0}, {"t": 2, "p": 20.0},
    ])
    assert [p["t"] for p in pts] == [1, 2, 3]
    assert pts[-1]["p"] == 33.0, "the later duplicate should win"


def test_history_drops_unparseable_points_without_inventing_one():
    pts = t._dedupe_points(
        [{"t": 1, "p": None}, {"t": None, "p": 5.0}, {"t": 2, "p": 20.0}])
    assert pts == [{"t": 2, "p": 20.0}]


def test_book_levels_collapse_duplicate_prices_and_accumulate():
    levels = t._levels([(0.51, 10.0), (0.51, 5.0), (0.50, 20.0)], descending=True)
    assert [l["priceCents"] for l in levels] == [51.0, 50.0]
    assert levels[0]["size"] == 15.0, "duplicate levels were not summed"
    assert levels[1]["cumulative"] == 35.0
    assert levels[0]["notionalUsd"] == 7.65


def test_book_levels_ignore_zero_and_malformed_rows():
    parsed = us.parse_book({"bids": [
        {"px": {"value": "0.5"}, "qty": "0"},
        {"px": {"value": "x"}, "qty": "1"},
        {"nope": 1},
        {"px": {"value": "0.4"}, "qty": "3"},
    ], "offers": []})
    levels = t._levels(parsed["bids"], descending=True)
    assert len(levels) == 1 and levels[0]["priceCents"] == 40.0


def test_a_no_book_is_the_mirror_of_the_yes_book():
    """One instrument per market. A NO bid is someone offering YES, at 100 less."""
    parsed = us.parse_book({"bids": [{"px": {"value": "0.40"}, "qty": "7"}],
                            "offers": [{"px": {"value": "0.42"}, "qty": "9"}]})
    no = us.book_for_side(parsed, "no")
    bids = t._levels(no["bids"], descending=True)
    asks = t._levels(no["asks"], descending=False)
    assert (bids[0]["priceCents"], bids[0]["size"]) == (58.0, 9.0)
    assert (asks[0]["priceCents"], asks[0]["size"]) == (60.0, 7.0)


# ────────────────────────────────────────────────────────────────────────
# Fees — an unknown fee is not a zero fee.
# ────────────────────────────────────────────────────────────────────────


def test_fee_uses_the_markets_own_schedule():
    # The coefficient is per market, and there is no table to fall back on: the
    # exchange can re-tune it, and any local copy would go stale.
    fee = tt._fee_per_share(50.0, "crypto", {"enabled": True, "rate": 0.02, "exponent": 1})
    assert abs(fee - 0.02 * 0.25) < 1e-9


def test_fee_respects_an_explicitly_fee_free_market():
    assert tt._fee_per_share(50.0, "crypto", {"enabled": False}) == 0.0


def test_fee_is_none_when_we_cannot_determine_it():
    # The ticket then WARNS that the cost excludes fees, rather than quoting a
    # cost that is quietly wrong in the user's favour.
    assert tt._fee_per_share(50.0, None, None) is None
    assert tt._fee_per_share(50.0, "not-a-real-category", None) is None


def _schedule(coefficient):
    return tt.api._fee_schedule_from_gamma({"feeCoefficient": coefficient})


def test_fee_curve_is_the_markets_coefficient_times_p_one_minus_p():
    # At even odds and 0.0695: 1.7375 cents per contract.
    assert abs(tt._fee_per_share(50.0, "crypto", _schedule(0.0695)) - 0.017375) < 1e-9
    # A category is no substitute for the market saying so.
    assert tt._fee_per_share(50.0, "crypto", None) is None
    assert tt._fee_per_share(50.0, "crypto", _schedule(0)) == 0.0


def test_fee_is_symmetric_and_vanishes_at_the_extremes():
    s = _schedule(0.07)
    assert abs(tt._fee_per_share(20.0, "crypto", s) - tt._fee_per_share(80.0, "crypto", s)) < 1e-9
    assert tt._fee_per_share(1.0, "crypto", s) < 0.001


@pytest.mark.parametrize("coefficient,contracts,cents,want", [
    (0.0695, 100, 50.0, "1.74"),   # 1.7375
    (0.07, 50, 50.0, "0.88"),      # 0.875: a tie, to the even cent above
    (0.05, 10, 50.0, "0.12"),      # 0.125: a tie, to the even cent below
    (0.07, 1, 50.0, "0.02"),       # 0.0175
])
def test_a_taker_fee_is_charged_to_the_cent_the_way_the_exchange_rounds(
        coefficient, contracts, cents, want):
    assert str(tt.taker_fee_usd(cents, contracts, _schedule(coefficient))) == want


def test_a_taker_fee_nobody_can_price_is_none():
    assert tt.taker_fee_usd(50.0, 10, None) is None


# ────────────────────────────────────────────────────────────────────────
# Walking the book — "unknown" and "zero available" are different facts.
# ────────────────────────────────────────────────────────────────────────


def test_walking_an_unreadable_book_reports_unknown_not_zero():
    assert tt._walk_book([], 10, 50, is_buy=True) == (None, None)


def test_walking_a_book_with_nothing_in_range_reports_zero():
    # There IS a book; it just has nothing at our price. That is a real zero.
    levels = [{"priceCents": 60.0, "size": 100.0}]
    fillable, avg = tt._walk_book(levels, 10, 50, is_buy=True)
    assert fillable == 0.0 and avg is None


def test_walk_stops_at_the_limit_price_when_buying():
    levels = [{"priceCents": 50.0, "size": 4.0},
              {"priceCents": 51.0, "size": 6.0},
              {"priceCents": 60.0, "size": 100.0}]
    fillable, avg = tt._walk_book(levels, 20, 51, is_buy=True)
    assert fillable == 10.0
    assert abs(avg - 50.6) < 1e-9


def test_walk_stops_at_the_limit_price_when_selling():
    # Selling walks DOWN the bids and must not cross below the limit.
    levels = [{"priceCents": 50.0, "size": 4.0},
              {"priceCents": 49.0, "size": 6.0},
              {"priceCents": 40.0, "size": 100.0}]
    fillable, avg = tt._walk_book(levels, 20, 49, is_buy=False)
    assert fillable == 10.0
    assert abs(avg - 49.4) < 1e-9


def test_walk_never_reports_more_than_requested():
    levels = [{"priceCents": 50.0, "size": 1000.0}]
    fillable, avg = tt._walk_book(levels, 7, 50, is_buy=True)
    assert fillable == 7.0 and avg == 50.0


# ────────────────────────────────────────────────────────────────────────
# Cost basis — average cost, and a sell must not disturb it.
# ────────────────────────────────────────────────────────────────────────


def _seed_fills(rows):
    with db.get_db() as conn:
        conn.execute("DELETE FROM terminal_fills")
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                   (trade_id, order_id, condition_id, title, side, action,
                    size, price_cents, fees_usd, cost_usd, origin, network,
                    filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"t{i}", None, "0xdead", "T", r.get("side", "yes"),
                 r["action"], r["size"], r["price"], r.get("fee", 0.0),
                 r["size"] * r["price"] / 100.0, "manual", "mainnet",
                 f"2026-01-0{i + 1}T00:00:00Z"))


def test_average_cost_across_two_buys(fresh_db):
    _seed_fills([
        {"action": "buy", "size": 100, "price": 40.0},
        {"action": "buy", "size": 100, "price": 60.0},
    ])
    st = tt._ledger_basis()[("0xdead", "yes")]
    assert st["size"] == 200
    assert abs(st["cost"] / st["size"] * 100 - 50.0) < 1e-9


def test_a_sell_reduces_size_and_leaves_the_average_alone(fresh_db):
    # This is the whole reason the convention is stated in the UI: FIFO would
    # give a different basis here, and a trader saying "I am up 2x on my bag"
    # means the average.
    _seed_fills([
        {"action": "buy", "size": 100, "price": 40.0},
        {"action": "buy", "size": 100, "price": 60.0},
        {"action": "sell", "size": 100, "price": 90.0},
    ])
    st = tt._ledger_basis()[("0xdead", "yes")]
    assert st["size"] == 100
    assert abs(st["cost"] / st["size"] * 100 - 50.0) < 1e-6, "the sell moved the average"
    assert abs(st["realized"] - 100 * (0.90 - 0.50)) < 1e-6


def test_an_unknown_fee_marks_the_whole_group_unknown(fresh_db):
    # Summing only the fees we know would understate the total, which is the
    # same "wrong in the user's favour" failure as ignoring fees entirely.
    _seed_fills([{"action": "buy", "size": 10, "price": 50.0, "fee": 0.1}])
    with db.get_db() as conn:
        conn.execute("UPDATE terminal_fills SET fees_usd=NULL WHERE trade_id='t0'")
    st = tt._ledger_basis()[("0xdead", "yes")]
    assert st["fees_known"] is False


# ────────────────────────────────────────────────────────────────────────
# Watchlist — "you starred nothing" vs "we could not load what you starred".
# ────────────────────────────────────────────────────────────────────────


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_empty_watchlist_says_it_is_empty(monkeypatch):
    monkeypatch.setattr(t, "watchlist_list", lambda: [])
    r = _run(t.discover("watchlist"))
    assert "Nothing on your watchlist yet" in r["note"]


def test_unloadable_watchlist_does_not_claim_to_be_empty(monkeypatch):
    """The bug this pins: a starred market that could not be read renders an empty
    column, and the note told the user they had never starred anything — so it
    looks like the app lost their list. An unloadable list must say so."""
    monkeypatch.setattr(
        t, "watchlist_list",
        lambda: [{"conditionId": "0xdead", "title": "x", "addedAt": "", "note": None}])

    async def _none(_ids):
        return []
    monkeypatch.setattr(t, "_summaries_for", _none)

    r = _run(t.discover("watchlist"))
    assert r["rows"] == []
    assert "Nothing on your watchlist yet" not in r["note"]
    assert "watchlist is intact" in r["note"]


def test_partially_loadable_watchlist_reports_the_shortfall(monkeypatch):
    monkeypatch.setattr(t, "watchlist_list", lambda: [
        {"conditionId": "0xa", "title": "a", "addedAt": "", "note": None},
        {"conditionId": "0xb", "title": "b", "addedAt": "", "note": None},
    ])

    async def _one(_ids):
        return [{"conditionId": "0xa", "title": "a", "category": "crypto",
                 "probability": 50.0, "sources": {}, "unknowns": [],
                 "volume24hUsd": None, "liquidityUsd": None,
                 "hoursToClose": None}]
    monkeypatch.setattr(t, "_summaries_for", _one)

    r = _run(t.discover("watchlist"))
    assert len(r["rows"]) == 1
    assert "1 of your 2 watched market(s) could not be loaded" in r["note"]


# ────────────────────────────────────────────────────────────────────────
# Fill reconcile — the order's own record is the only source there is.
#
# A trade activity names no order and no side, so fills cannot be matched to
# orders from it. Every order we placed is read by id instead, and what the
# exchange's record cannot say stays unknown.
# ────────────────────────────────────────────────────────────────────────


def _placed(oid, *, order_type="GTC", side="yes", action="buy", status="placed",
            age="-1 minutes", origin="manual"):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO terminal_orders
                 (condition_id, title, side, action, order_type, requested_size,
                  requested_price_cents, order_id, status, origin, network, created_at)
               VALUES ('0xabc','T',?,?,?,10,60,?,?,?,'mainnet',datetime('now',?))""",
            (side, action, order_type, oid, status, origin, age))


def _view(oid, status="matched", filled=10.0, price=0.60, fee=0.0):
    """`api.get_order`'s view of an order: us_api.order_view."""
    return {"id": oid, "status": status, "size_matched": filled, "original_size": 10.0,
            "price": price, "side": "yes", "action": "buy", "market": "0xabc",
            "fee_usd": fee, "created_at": "2026-11-01T00:00:00Z"}


def _venue_says(monkeypatch, replies, *, coefficient=0.07):
    """`get_order` answering from `replies` {id: view or exception}. Returns the ids
    it was asked about."""
    asked = []

    async def _get_order(oid):
        asked.append(oid)
        r = replies[oid]
        if isinstance(r, BaseException):
            raise r
        return {"order": r}

    async def _summaries(ids):
        return [{"conditionId": c, "title": "T", "category": "crypto",
                 "_raw": {} if coefficient is None else {"feeCoefficient": coefficient}}
                for c in ids]
    monkeypatch.setattr(tt.api, "get_order", _get_order)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)
    return asked


def _fills():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_fills ORDER BY trade_id")]


def _order_row(oid):
    with db.get_db() as conn:
        return dict(conn.execute(
            "SELECT status, reconciled, filled_size, avg_fill_price_cents, fees_usd, error "
            "FROM terminal_orders WHERE order_id=?", (oid,)).fetchone())


def test_reconcile_books_what_the_exchange_says_the_order_filled(monkeypatch, fresh_db):
    _placed("oid_1", order_type="FAK")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1", "canceled", filled=3.0)})
    r = _run(tt.reconcile_fills())
    assert r["ok"] is True and r["complete"] is True and r["note"] is None
    assert (r["inserted"], r["matched"], r["scanned"]) == (1, 1, 1)
    (f,) = _fills()
    assert f["trade_id"] == "order:oid_1"
    assert (f["size"], f["price_cents"], f["cost_usd"]) == (3.0, 60.0, 1.8)
    assert (f["side"], f["action"], f["origin"]) == ("yes", "buy", "manual")
    o = _order_row("oid_1")
    assert (o["filled_size"], o["avg_fill_price_cents"]) == (3.0, 60.0)
    assert (o["status"], o["reconciled"]) == ("canceled", 1)


def test_a_no_order_is_filed_under_the_side_it_was_sent_for(monkeypatch, fresh_db):
    _placed("oid_n", side="no", origin="ai")
    _venue_says(monkeypatch, {"oid_n": _view("oid_n", price=0.30, fee=0.05)})
    _run(tt.reconcile_fills())
    (f,) = _fills()
    assert (f["side"], f["price_cents"], f["origin"]) == ("no", 30.0, "ai")
    assert ("0xabc", "no") in tt._ledger_basis()


def test_each_order_is_one_fill_however_many_passes_read_it(monkeypatch, fresh_db):
    """A resting order fills in stages. Each pass restates the running total rather
    than adding another row, or a re-read would double the position."""
    _placed("oid_g")
    _venue_says(monkeypatch, {"oid_g": _view("oid_g", "live", filled=3.0)})
    r1 = _run(tt.reconcile_fills())
    assert r1["inserted"] == 1
    assert (_order_row("oid_g")["status"], _order_row("oid_g")["reconciled"]) == ("placed", 0)

    _venue_says(monkeypatch, {"oid_g": _view("oid_g", "matched", filled=10.0, price=0.58)})
    r2 = _run(tt.reconcile_fills())
    assert r2["inserted"] == 0 and r2["matched"] == 1
    (f,) = _fills()
    assert (f["size"], f["price_cents"]) == (10.0, 58.0)
    assert (_order_row("oid_g")["status"], _order_row("oid_g")["reconciled"]) == ("filled", 1)

    # Final: it is never asked about again.
    asked = _venue_says(monkeypatch, {})
    _run(tt.reconcile_fills())
    assert asked == []


def test_with_no_key_a_pass_is_not_a_clean_one(monkeypatch, fresh_db):
    """The agents' loss stop reads `ok`. A stale ledger must not pass for a fresh one."""
    _placed("oid_1")
    _venue_says(monkeypatch, {})
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: False)
    r = _run(tt.reconcile_fills())
    assert r["ok"] is False and r["complete"] is False
    assert "No API key" in r["note"]
    assert _fills() == []


def test_an_unreadable_order_fails_the_pass_and_stays_open(monkeypatch, fresh_db):
    _placed("oid_1")
    _placed("oid_2")
    _venue_says(monkeypatch, {
        "oid_1": tt.api.PolymarketAPIError(500, "down"),
        "oid_2": _view("oid_2"),
    })
    r = _run(tt.reconcile_fills())
    assert r["ok"] is False and r["complete"] is False
    assert "could not be read" in r["note"]
    assert _order_row("oid_1")["reconciled"] == 0
    assert _order_row("oid_2")["reconciled"] == 1, "one bad read stopped the others"


def test_an_order_the_exchange_has_not_seen_yet_is_asked_about_again(monkeypatch, fresh_db):
    """A 404 is an answer, not a failed read. Just after sending it can be timing."""
    _placed("oid_1")
    _venue_says(monkeypatch, {"oid_1": tt.api.PolymarketAPIError(404, "not found")})
    r = _run(tt.reconcile_fills())
    assert r["ok"] is True
    assert "no record of 1 order" in r["note"]
    o = _order_row("oid_1")
    assert (o["status"], o["reconciled"]) == ("placed", 0)


def test_an_order_it_still_has_no_record_of_an_hour_on_stops_being_asked_about(
        monkeypatch, fresh_db):
    """Asking forever would crowd out orders that can still change. What became of
    it stays unknown, and the row says that rather than "failed"."""
    _placed("oid_1", age="-2 hours")
    _venue_says(monkeypatch, {"oid_1": tt.api.PolymarketAPIError(404, "not found")})
    _run(tt.reconcile_fills())
    o = _order_row("oid_1")
    assert (o["status"], o["reconciled"]) == ("unknown", 1)
    assert "no record" in o["error"]


def test_a_placeholder_id_is_never_looked_up(monkeypatch, fresh_db):
    """A lost reply loses the id too. `get_order` would answer a placeholder with a
    404, and that must not read as "the exchange never saw it"."""
    oid = tt.api.UNKNOWN_ID_PREFIX + "abc"
    _placed(oid, status="unknown", age="-2 hours")
    asked = _venue_says(monkeypatch, {})
    r = _run(tt.reconcile_fills())
    assert asked == [] and r["skipped"] == 1
    assert "never got an id" in r["note"]
    o = _order_row(oid)
    assert (o["status"], o["reconciled"]) == ("unknown", 0)


def test_a_fill_with_no_price_is_a_failure_not_a_fill_at_zero(monkeypatch, fresh_db):
    _placed("oid_1")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1", price=0.0)})
    r = _run(tt.reconcile_fills())
    assert r["ok"] is False and "no price" in r["note"]
    assert _fills() == [] and _order_row("oid_1")["reconciled"] == 0


def test_a_pass_that_stops_early_says_so(monkeypatch, fresh_db):
    """A truncated pass that reports success reads as "everything is reconciled"."""
    monkeypatch.setattr(tt, "_RECONCILE_MAX_ORDERS", 2)
    for oid in ("a", "b", "c"):
        _placed(oid)
    asked = _venue_says(monkeypatch, {oid: _view(oid) for oid in ("a", "b", "c")})
    r = _run(tt.reconcile_fills())
    assert r["complete"] is False and r["scanned"] == 2 and len(asked) == 2
    assert "Read 2 of 3" in r["note"]
    r = _run(tt.reconcile_fills())
    assert r["complete"] is True and len(_fills()) == 3


def test_a_wipe_leaves_nothing_to_re_import(monkeypatch, fresh_db):
    """"Start my stats fresh" must leave the Track screen empty. Reconcile only reads
    orders on our own rows, so wiping them is the end of it: nothing comes back
    stamped as someone else's."""
    _placed("oid_1")
    asked = _venue_says(monkeypatch, {"oid_1": _view("oid_1")})
    _run(tt.reconcile_fills())
    assert len(_fills()) == 1
    db.clear_trade_history()
    asked.clear()
    r = _run(tt.reconcile_fills())
    assert asked == [] and _fills() == [] and r["ok"] is True


def test_a_fill_the_exchange_stopped_reporting_is_taken_back_out(monkeypatch, fresh_db):
    """A busted trade. Left in, it inflates size and cost and later settles as a
    phantom win or loss, and the order row must stop claiming the fill."""
    _placed("oid_1")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1", "live", filled=4.0)})
    _run(tt.reconcile_fills())
    assert len(_fills()) == 1
    _venue_says(monkeypatch, {"oid_1": _view("oid_1", "canceled", filled=0.0)})
    r = _run(tt.reconcile_fills())
    assert _fills() == [] and r["unwound"] == 1
    assert "taken back out" in r["note"]
    o = _order_row("oid_1")
    assert o["filled_size"] == 0.0 and o["status"] == "canceled"


# ── fees: the order record has a commission figure, and sometimes that is all ──


def test_a_commission_the_exchange_reports_is_the_fee(monkeypatch, fresh_db):
    _placed("oid_1")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1", fee=0.33)})
    _run(tt.reconcile_fills())
    assert _fills()[0]["fees_usd"] == 0.33


def test_a_fak_with_no_reported_commission_is_priced_off_the_curve(monkeypatch, fresh_db):
    """A FAK can only ever take, so the curve applies: 0.07 x 10 x .6 x .4 = .168."""
    _placed("oid_1", order_type="FAK")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1")})
    _run(tt.reconcile_fills())
    assert _fills()[0]["fees_usd"] == 0.17
    assert tt._ledger_basis()[("0xabc", "yes")]["fees_known"] is True


def test_a_gtc_with_no_reported_commission_has_an_unknown_fee_not_a_zero_one(
        monkeypatch, fresh_db):
    """It may have rested and filled as a maker (0 at the base tier) or crossed on
    arrival (the curve), and the record does not say which. Either number would be
    one nobody produced, and a zero is wrong in the user's favour."""
    _placed("oid_1")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1")})
    _run(tt.reconcile_fills())
    assert _fills()[0]["fees_usd"] is None
    assert tt._ledger_basis()[("0xabc", "yes")]["fees_known"] is False


@pytest.mark.parametrize("parked", [False, True])
def test_a_failed_market_read_does_not_freeze_a_fak_fee_unknown(
        monkeypatch, fresh_db, parked):
    """A FAK's fee needs the market's coefficient. One pass whose lookup failed must
    leave the order open for the next, because a single NULL fee turns the Track
    headline from net to gross: on the owner's ledger, a $64 loss shown as an $11
    gain. A parked gateway answers with no rows instead of raising."""
    _placed("oid_1", order_type="FAK")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1")})
    good = tt.term._summaries_for

    async def _down(_ids):
        if parked:
            return []
        raise RuntimeError("gateway down")
    monkeypatch.setattr(tt.term, "_summaries_for", _down)
    r = _run(tt.reconcile_fills())
    assert _fills()[0]["fees_usd"] is None
    assert _order_row("oid_1")["reconciled"] == 0
    assert r["complete"] is False and "fee rate" in r["note"]

    monkeypatch.setattr(tt.term, "_summaries_for", good)
    r = _run(tt.reconcile_fills())
    assert _fills()[0]["fees_usd"] == 0.17
    assert _order_row("oid_1")["reconciled"] == 1 and r["complete"] is True


def test_a_market_that_stopped_answering_cannot_hold_an_order_open_forever(
        monkeypatch, fresh_db):
    _placed("oid_1", order_type="FAK", age="-2 days")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1")})

    async def _nothing(_ids):
        return []
    monkeypatch.setattr(tt.term, "_summaries_for", _nothing)
    _run(tt.reconcile_fills())
    assert _order_row("oid_1")["reconciled"] == 1
    assert _fills()[0]["fees_usd"] is None


def test_a_market_that_states_no_coefficient_is_final_with_an_unknown_fee(
        monkeypatch, fresh_db):
    """Retrying would never change the answer, and would keep the order open."""
    _placed("oid_1", order_type="FAK")
    _venue_says(monkeypatch, {"oid_1": _view("oid_1")}, coefficient=None)
    _run(tt.reconcile_fills())
    assert _fills()[0]["fees_usd"] is None
    assert _order_row("oid_1")["reconciled"] == 1


# ────────────────────────────────────────────────────────────────────────
# Settled positions — a decided outcome is EXACT, not a mark.
# ────────────────────────────────────────────────────────────────────────


def _fake_portfolio(monkeypatch, *, resolved, side, cur_price=0.97):
    async def _positions(*a, **kw):
        return [{
            "ticker": "0xabc", "title": "Did it happen?",
            "position_fp": 100.0 if side == "yes" else -100.0,
            "cur_price": cur_price,
        }]

    async def _summaries(_ids):
        return [{
            "conditionId": "0xabc", "title": "Did it happen?", "slug": "s",
            "status": "settled" if resolved else "open",
            "resolvedOutcome": resolved, "closeTime": "2026-01-01T00:00:00Z",
        }]

    async def _balance():
        raise RuntimeError("no key in tests")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)


def test_a_won_settled_position_marks_at_exactly_100c(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved="yes", side="yes", cur_price=0.97)
    p = _run(tt.portfolio())["positions"][0]
    assert p["settled"] is True
    assert p["markCents"] == 100.0, "a decided winner is worth exactly $1, not the last mark"
    assert p["status"] == "settled"
    assert p["resolvedOutcome"] == "yes"


def test_a_lost_settled_position_marks_at_exactly_0c(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved="no", side="yes", cur_price=0.03)
    p = _run(tt.portfolio())["positions"][0]
    assert p["markCents"] == 0.0
    assert p["valueUsd"] == 0.0


def test_an_open_position_keeps_its_mark_and_is_not_called_settled(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved=None, side="yes", cur_price=0.62)
    p = _run(tt.portfolio())["positions"][0]
    assert p["settled"] is False
    assert p["markCents"] == 62.0
    assert p["status"] == "open"


def test_nothing_is_ever_called_redeemable(monkeypatch, fresh_db):
    """Settlement pays automatically, so there is nothing to claim and no button to
    press. The keys stay because the renderer reads them; they all say none."""
    _fake_portfolio(monkeypatch, resolved="yes", side="yes")
    r = _run(tt.portfolio())
    assert r["redeemableCount"] == 0 and r["redeemableValueUsd"] == 0.0
    assert r["redeemableNote"] is None
    assert r["positions"][0]["redeemable"] is False


def test_an_unreadable_wallet_is_flagged_not_reported_as_empty(
        monkeypatch, fresh_db):
    """`portfolio()` degrades a failed account read to an empty list plus a
    note, which is right for a screen and dangerous for anything that acts on
    it: "we could not ask" and "you hold nothing" are opposite instructions to
    a seller. The flag is what lets the two be told apart without matching on
    the note's wording."""
    async def _boom(*_a, **_kw):
        raise RuntimeError("rpc down")

    monkeypatch.setattr(tt.api, "get_positions", _boom)
    r = _run(tt.portfolio())
    assert r["positions"] == []
    assert r["walletReadable"] is False
    assert "could not read" in (r["unreconciledNote"] or "").lower()


def test_a_wallet_that_answers_is_marked_readable(monkeypatch, fresh_db):
    _fake_portfolio(monkeypatch, resolved=None, side="yes", cur_price=0.62)
    assert _run(tt.portfolio())["walletReadable"] is True


def test_a_settled_position_with_no_basis_still_reports_unknown_pnl(monkeypatch, fresh_db):
    """Knowing the OUTCOME does not mean knowing the P&L. Without a cost basis
    there is nothing to measure the settlement against, and guessing one would
    be exactly the lie the ledger exists to prevent."""
    _fake_portfolio(monkeypatch, resolved="yes", side="yes")
    p = _run(tt.portfolio())["positions"][0]
    assert p["markCents"] == 100.0
    assert p["avgCostCents"] is None
    assert p["unrealizedPnlUsd"] is None
    assert p["unreconciled"] is True


# ────────────────────────────────────────────────────────────────────────
# Offline mode — the switch that makes the privacy claim checkable.
# ────────────────────────────────────────────────────────────────────────


def test_offline_mode_makes_no_requests(monkeypatch):
    called = {"n": 0}

    async def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("offline mode still hit the network")
    monkeypatch.setattr(t.api, "_get_client", _boom)

    t.set_offline(True)
    try:
        r = _run(t.discover("volume"))
    finally:
        t.set_offline(False)
    assert r["rows"] == []
    assert called["n"] == 0


def test_offline_reason_beats_a_column_specific_note(monkeypatch):
    """The bug this pins: 'New' explained an empty column with "just-listed
    markets have no book yet" and 'Resolving' with "none of the 0 most
    recently closed markets is awaiting settlement". Both are confident claims
    about Polymarket, made after asking nobody."""
    t.set_offline(True)
    try:
        for column in ("new", "volatility", "volume", "closing"):
            r = _run(t.discover(column))
            assert r["rows"] == []
            assert r["note"] == t.OFFLINE_REASON, (
                f"{column} explained itself with something other than the real "
                f"reason: {r['note']!r}")
    finally:
        t.set_offline(False)


def test_offline_mode_does_not_serve_stale_cache(monkeypatch):
    """Serving a cached answer while the user believes the app is offline is
    its own small lie — the point of the switch is that it is verifiable."""
    key = t._cache_key("gateway", "/v1/markets", {"limit": 1})
    t._cache[key] = (t.time.monotonic() + 999, [{"conditionId": "0x1"}], t._now_iso())
    t.set_offline(True)
    try:
        data, _ = _run(t._fetch("gateway", "/v1/markets", {"limit": 1}))
    finally:
        t.set_offline(False)
        t._cache.pop(key, None)
    assert data is None


def test_health_reports_the_switch_state():
    t.set_offline(True)
    try:
        assert t.health()["offline"] is True
    finally:
        t.set_offline(False)
    assert t.health()["offline"] is False


def test_a_thin_all_or_none_order_warns_rather_than_blocks(monkeypatch):
    """An all-or-none order that crosses nothing is KILLED by the venue and
    reported as a clean zero fill — it costs nothing. Refusing to submit it
    made that outcome unreachable, and paper mode could not simulate the kill
    an FOK exists for."""
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 1,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": True, "bids": [], "note": None,
                "asks": [{"priceCents": 50.0, "size": 3, "cumulative": 3,
                          "notionalUsd": 1.5}]}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 55, "size": 10,
                          "orderType": "FOK"}))
    assert not pv["blockers"], f"a thin FOK was blocked: {pv['blockers']}"
    assert any("killed" in w for w in pv["warnings"])


# ────────────────────────────────────────────────────────────────────────
# The ticket's cost must be a number that can actually happen.
#
# It used to be `requested size × the average of the fillable slice only`,
# which is neither what an FAK can spend nor what a GTC can spend. Both
# examples below are the audited ones: 3 asks at 60¢, 100 contracts at 65¢.
# ────────────────────────────────────────────────────────────────────────


def _thin_book(monkeypatch, *, asks=(), bids=()):
    """A fee-free market whose book holds exactly the levels given."""
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 1,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": True, "note": None,
                "asks": [{"priceCents": p, "size": s} for p, s in asks],
                "bids": [{"priceCents": p, "size": s} for p, s in bids]}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)


def test_a_fak_costs_what_it_can_actually_spend(monkeypatch):
    """3 asks at 60¢, FAK buy of 100 at 65¢. The remainder is KILLED, so the
    most this order can spend is 3 × 60¢ = $1.80. The old ticket said $60.00 —
    a figure that also drove the fee, the max loss and the confirmation gate."""
    _thin_book(monkeypatch, asks=[(60.0, 3)])
    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 65, "size": 100,
                          "orderType": "FAK"}))
    assert pv["fillableSize"] == 3
    assert pv["costUsd"] == 1.80
    assert pv["feeUsd"] == 0.0
    # You end up with the 3 contracts that crossed, not the 100 you asked for.
    assert pv["maxPayoutUsd"] == 3.0
    assert pv["maxLossUsd"] == 1.80


def test_a_gtc_costs_its_real_worst_case(monkeypatch):
    """Same book, same order, GTC: 3 cross at 60¢ and the other 97 REST at the
    65¢ limit, so the worst case is $1.80 + $63.05 = $64.85 — not the $60.00
    the fillable-slice average produced, and more than the user was shown."""
    _thin_book(monkeypatch, asks=[(60.0, 3)])
    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 65, "size": 100,
                          "orderType": "GTC"}))
    assert pv["costUsd"] == 64.85
    assert pv["maxPayoutUsd"] == 100.0
    assert pv["maxLossUsd"] == 64.85


def test_an_order_that_crosses_nothing_costs_nothing_only_if_it_is_killed(monkeypatch):
    """The two order types diverge completely on an untouchable book: the FAK
    spends zero, the GTC rests the whole size at the limit."""
    _thin_book(monkeypatch, asks=[(70.0, 500)])
    fak = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "buy", "priceCents": 65, "size": 100,
                           "orderType": "FAK"}))
    assert fak["fillableSize"] == 0.0
    assert fak["costUsd"] == 0.0 and fak["maxLossUsd"] == 0.0
    gtc = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "buy", "priceCents": 65, "size": 100,
                           "orderType": "GTC"}))
    assert gtc["costUsd"] == 65.00


def test_sell_proceeds_follow_the_same_rule(monkeypatch):
    """Proceeds are the mirror image: an FAK banks only what crosses, a GTC
    assumes the resting remainder fills at the limit and no better."""
    _thin_book(monkeypatch, bids=[(60.0, 3)])
    fak = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "sell", "priceCents": 55, "size": 100,
                           "orderType": "FAK"}))
    assert fak["costUsd"] == 1.80
    assert fak["maxPayoutUsd"] == 1.80
    # Only the 3 contracts that actually leave give up their upside.
    assert fak["maxLossUsd"] == 1.20
    gtc = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "sell", "priceCents": 55, "size": 100,
                           "orderType": "GTC"}))
    assert gtc["costUsd"] == 55.15


def test_an_unreadable_book_prices_the_whole_order_at_the_limit(monkeypatch):
    """No walk to lean on. The only safe direction to be wrong in is UP, so
    the whole size is priced at the limit rather than left at zero."""
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 1,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": False, "bids": [], "asks": [], "note": "no book"}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 65, "size": 100,
                          "orderType": "FAK"}))
    assert pv["fillableSize"] is None
    assert pv["costUsd"] == 65.00


# ────────────────────────────────────────────────────────────────────────
# Standing instructions must check you still hold the position.
# ────────────────────────────────────────────────────────────────────────


def _no_live_read(monkeypatch):
    """A tick re-quotes each market before it judges a trigger. With neither the feed
    nor the by-slug read answering, the quote it already has is what it judges on."""
    async def _tops(_slugs):
        return {}
    monkeypatch.setattr(tt.term, "_live_quote", lambda tok: None)
    monkeypatch.setattr(tt.term, "_gateway_tops", _tops)


def _standing(monkeypatch, *, positions, prob=30.0, placed=None):
    """One armed stop at 40c on a market currently at `prob`, on a Live
    account (the backend's raw default config reads Paper)."""
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    entries = [{
        "id": "st_1", "conditionId": "0xabc", "title": "T", "side": "yes",
        "kind": "stopLoss", "triggerCents": 40.0, "size": 100,
        "createdAt": "", "armed": True, "lastCheckedAt": None,
        "firedAt": None, "note": None,
    }]
    monkeypatch.setattr(tt, "standing_list", lambda: entries)
    monkeypatch.setattr(tt, "_standing_put", lambda e: None)

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": prob}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        if positions is None:
            raise RuntimeError("positions read failed")
        return positions
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    _no_live_read(monkeypatch)

    async def _place(req, **kw):
        if placed is not None:
            placed.append(req)
        return {"ok": True, "message": "sold"}
    monkeypatch.setattr(tt, "place", _place)

    async def _preview(req):
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)
    return entries


def test_a_stop_retires_itself_when_the_position_is_gone(monkeypatch):
    """Without this the venue rejects the sell, the attempt lands in the order
    log as an error, and the instruction stays armed and does it again on
    every single tick."""
    placed = []
    entries = _standing(monkeypatch, positions=[], placed=placed)
    r = _run(tt.standing_tick(allow_trading=True))
    assert placed == [], "a stop fired against a position that no longer exists"
    assert entries[0]["armed"] is False
    assert "no longer hold" in entries[0]["note"]
    assert r["alerts"] and r["alerts"][0]["blocked"] is True


def test_a_stop_holds_fire_when_positions_cannot_be_read(monkeypatch):
    """An unreadable account is NOT the same as holding nothing. Selling blind
    is worse than waiting, so it stays armed and retries."""
    placed = []
    entries = _standing(monkeypatch, positions=None, placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert placed == []
    assert entries[0]["armed"] is True, "an API blip disarmed a live stop"
    assert entries[0]["firedAt"] is None
    assert "could not be read" in entries[0]["note"]


def test_a_stop_sells_only_what_is_actually_held(monkeypatch):
    placed = []
    entries = _standing(
        monkeypatch,
        positions=[{"ticker": "0xabc", "position_fp": 30.0}],
        placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1
    assert placed[0]["size"] == 30.0, "sold more than the account holds"
    assert "only 30" in entries[0]["note"]


def _listing_lags_the_book(monkeypatch, *, listed, bid, ask):
    """The cached listing says `listed`; the uncached read says bid/ask."""
    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": listed,
                "yesTokenId": f"{cid}#yes", "resolvedOutcome": None}
    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "_live_quote", lambda tok: None)

    async def _tops(slugs):
        return {s: (bid, ask, "2026-10-11T00:00:00+00:00") for s in slugs}
    monkeypatch.setattr(tt.term, "_gateway_tops", _tops)


def test_a_stop_fires_on_the_live_read_when_the_listing_lags(monkeypatch):
    """A listing can be 30 seconds behind, and a 15-minute window moves tens of cents
    in that. A position that has already collapsed must not wait for it to notice."""
    placed = []
    _standing(monkeypatch, positions=[{"ticker": "0xabc", "position_fp": 100.0}],
              placed=placed)
    _listing_lags_the_book(monkeypatch, listed=60.0, bid=28.0, ask=32.0)
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1, "the stop waited on the listing's stale 60c"


def test_a_stop_holds_when_only_the_listing_says_it_collapsed(monkeypatch):
    """And the other way: a stale low must not sell a recovered position."""
    placed = []
    _standing(monkeypatch, positions=[{"ticker": "0xabc", "position_fp": 100.0}],
              placed=placed)
    _listing_lags_the_book(monkeypatch, listed=30.0, bid=58.0, ask=62.0)
    _run(tt.standing_tick(allow_trading=True))
    assert placed == [], "sold a position on the listing's stale 30c"


def test_a_stop_fires_normally_when_the_position_is_intact(monkeypatch):
    placed = []
    entries = _standing(
        monkeypatch,
        positions=[{"ticker": "0xabc", "position_fp": 100.0}],
        placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1 and placed[0]["size"] == 100.0
    assert entries[0]["armed"] is False and entries[0]["firedAt"]


def test_a_no_side_stop_reads_the_no_holding(monkeypatch):
    """A NO position comes back as a NEGATIVE size. Reading it as a YES holding would
    make every NO stop think the position was gone."""
    placed = []
    entries = _standing(
        monkeypatch,
        positions=[{"ticker": "0xabc", "position_fp": -100.0}],
        placed=placed)
    entries[0]["side"] = "no"
    # NO price = 100 - 30 = 70, which is above the 40c stop, so it must NOT fire.
    _run(tt.standing_tick(allow_trading=True))
    assert placed == []
    entries[0]["triggerCents"] = 80.0   # now 70 <= 80 -> fires
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == 1 and placed[0]["side"] == "no"


def test_an_alert_does_not_need_a_position(monkeypatch):
    placed = []
    entries = _standing(monkeypatch, positions=[], placed=placed)
    entries[0]["kind"] = "alertBelow"
    entries[0]["size"] = None
    r = _run(tt.standing_tick(allow_trading=True))
    assert r["fired"] == 1
    assert placed == [], "an alert placed an order"


# ────────────────────────────────────────────────────────────────────────
# A tick must not write back over what the user did while it was awaiting.
#
# The tick reads the whole list, awaits the book / the account / an order —
# seconds — then writes its copy back. The RPC handlers run as concurrent
# tasks throughout, so a blind write-back RESURRECTED a cancelled stop with
# its arming intact, and re-armed one the user had just disarmed.
# ────────────────────────────────────────────────────────────────────────


def _standing_store(monkeypatch, entries):
    """A real read/modify/write store behind standing_list/_standing_put, so
    a cancel that lands mid-tick behaves exactly as it does against the KV.
    On a Live account: the backend's raw default config reads Paper."""
    import copy
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    _no_live_read(monkeypatch)
    store = {"v": copy.deepcopy(entries)}
    monkeypatch.setattr(tt, "standing_list", lambda: copy.deepcopy(store["v"]))
    monkeypatch.setattr(
        tt, "_standing_put", lambda e: store.__setitem__("v", copy.deepcopy(e)))
    return store


def _stop_entry(**over):
    e = {
        "id": "st_1", "conditionId": "0xabc", "title": "T", "side": "yes",
        "kind": "stopLoss", "triggerCents": 40.0, "size": 100,
        "createdAt": "", "armed": True, "lastCheckedAt": None,
        "firedAt": None, "note": None,
    }
    e.update(over)
    return e


def test_a_stop_cancelled_during_a_tick_is_not_resurrected(monkeypatch):
    """The worst outcome available here: the user cancels a stop, the tick
    that was already in flight writes its stale copy back, and the stop is
    live again — ARMED, against a position the user has decided not to
    protect. It also fired an order on the way past."""
    store = _standing_store(monkeypatch, [_stop_entry()])

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": 30.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _preview(req):
        # The user hits Cancel while the preview is in flight.
        await asyncio.sleep(0)
        tt.standing_cancel("st_1")
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place(req, **kw):
        return {"ok": True, "message": "sold"}
    monkeypatch.setattr(tt, "place", _place)

    _run(tt.standing_tick(allow_trading=True))
    assert store["v"] == [], f"a cancelled instruction came back: {store['v']}"


def test_a_stop_disarmed_during_a_tick_stays_disarmed(monkeypatch):
    """Same lost update, quieter: the trigger never even fires, and the tick's
    only job is to stamp lastCheckedAt. Writing its copy back re-armed an
    instruction the user had just switched off."""
    store = _standing_store(monkeypatch, [_stop_entry()])

    async def _market(cid, include_raw=False):
        # The user flips it off while the market read is in flight.
        await asyncio.sleep(0)
        tt.standing_set_armed("st_1", False)
        return {"conditionId": cid, "title": "T", "probability": 90.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    _run(tt.standing_tick(allow_trading=True))
    assert store["v"][0]["armed"] is False, "a disarmed stop was re-armed"
    # The tick's own bookkeeping still lands — it is only `armed` and
    # existence that belong to the user.
    assert store["v"][0]["lastCheckedAt"] is not None


def test_an_instruction_added_during_a_tick_survives_it(monkeypatch):
    """The other half of the same write: a stop written while the tick was
    awaiting must not be erased by the older list going back."""
    store = _standing_store(monkeypatch, [_stop_entry()])

    async def _market(cid, include_raw=False):
        await asyncio.sleep(0)
        if not any(e["id"] == "st_2" for e in store["v"]):
            store["v"].append(_stop_entry(id="st_2", conditionId="0xdef"))
        return {"conditionId": cid, "title": "T", "probability": 90.0}
    monkeypatch.setattr(tt.term, "market", _market)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    _run(tt.standing_tick(allow_trading=True))
    assert sorted(e["id"] for e in store["v"]) == ["st_1", "st_2"]


# ────────────────────────────────────────────────────────────────────────
# Related markets — a contradiction claim must survive a moment's thought.
# ────────────────────────────────────────────────────────────────────────


def test_opposite_legs_are_not_treated_as_the_same_question():
    """Found live: "Fed will INCREASE rates by 25bps" and "Fed will DECREASE
    rates by 25bps" share 83% of their words. Flagging their 33c gap as a
    contradiction is a confident wrong claim in the panel that exists to be
    trusted."""
    a = "Will the Fed increase interest rates by 25 bps in September?"
    b = "Will the Fed decrease interest rates by 25 bps in September?"
    assert t._are_opposites(a, b)
    assert t._title_overlap(a, b) == 0.0


def test_rephrasings_of_the_same_question_still_match():
    a = "Will Trump win the 2028 election?"
    b = "Will Donald Trump win the 2028 election?"
    assert not t._are_opposites(a, b)
    assert t._title_overlap(a, b) > 0.7


def test_a_shared_updown_phrase_is_not_an_opposite():
    # Both titles contain BOTH words, so the pair carries no direction.
    a = "Bitcoin Up or Down - 9:00PM"
    b = "Bitcoin Up or Down - 9:05PM"
    assert not t._are_opposites(a, b)


def test_two_legs_of_one_event_are_not_a_contradiction(monkeypatch):
    """Exactly one leg of a multi-outcome event wins, so the legs disagreeing IS the
    design, and this venue has no flag that says which events work that way. Live:
    "Will Alice win the 2026 Alaska Senate election?" and "Will Bob win ..." share 78%
    of their words. Only a repeat of the question is evidence of a wrong price."""
    async def _event_markets(_slug):
        return [{"conditionId": "0xb",
                 "title": "Will Bob Jones win the 2026 Alaska Senate election?",
                 "probability": 20.0}]
    monkeypatch.setattr(t, "_event_markets", _event_markets)
    c = _run(t._sibling_check({
        "conditionId": "0xme", "probability": 70.0, "eventSlug": "e",
        "title": "Will Alice Smith win the 2026 Alaska Senate election?"}))
    assert c["level"] == "ok"
    assert "none asking a near-identical question" in c["detail"]


def test_the_same_question_at_two_prices_is_a_real_contradiction(monkeypatch):
    async def _event_markets(_slug):
        return [{"conditionId": "0xb", "title": "Will Trump win the election?",
                 "probability": 20.0}]
    monkeypatch.setattr(t, "_event_markets", _event_markets)
    c = _run(t._sibling_check({
        "conditionId": "0xme", "title": "Will Trump win the election",
        "probability": 70.0, "eventSlug": "e"}))
    assert c["level"] == "risk"
    assert "50.0¢ away" in c["detail"]


def test_an_events_sum_counts_each_market_exactly_once(monkeypatch):
    """`_event_markets` already returns OUR market among the event's legs, so
    adding it again double-counted its price: a Fed group whose five legs
    really summed to 100.7c was reported as 135.2c — i.e. "35c mispriced",
    which is a confident, entirely wrong claim. It looked plausible, which is
    exactly why it survived a screenshot."""
    legs = [
        {"conditionId": "0xme", "title": "Increase 25", "probability": 34.5},
        {"conditionId": "0xa", "title": "Decrease 25", "probability": 1.05},
        {"conditionId": "0xb", "title": "Decrease 50", "probability": 0.25},
        {"conditionId": "0xc", "title": "No change", "probability": 64.5},
        {"conditionId": "0xd", "title": "Increase 50", "probability": 0.35},
    ]

    async def _event_markets(_slug):
        return legs

    async def _market(cid, include_raw=False):
        return {"conditionId": "0xme", "title": "Increase 25",
                "probability": 34.5, "eventSlug": "e",
                "sources": {}, "unknowns": []}

    monkeypatch.setattr(t, "_event_markets", _event_markets)
    monkeypatch.setattr(t, "market", _market)
    r = _run(t.related("0xme"))
    # The legs sum to 100.65 and the total is rounded to one decimal, so
    # either 100.6 or 100.7 is the honest answer; only a double count fails.
    assert r["sumProbability"] in (100.6, 100.7), \
        f"legs summed to {r['sumProbability']}, expected ~100.65"
    # And our own market must not appear in its own related list.
    assert all(x["conditionId"] != "0xme" for x in r["rows"])
    assert r["negRisk"] is False, "the venue has no neg-risk, and the key stays for the panel"


def _one_settled(monkeypatch, *, outcome, size=30.0):
    async def _positions(*a, **kw):
        return [{"ticker": "0xa", "title": "T", "position_fp": size}]

    async def _summaries(_ids):
        return [{"conditionId": "0xa", "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": outcome,
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no key")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)


def test_a_worthless_settled_position_is_counted_as_exactly_that(monkeypatch, fresh_db):
    """Found against a real account: 105 expired losers, and the panel told the user
    to claim them "to turn them back into cash". They are worth exactly $0.00."""
    _one_settled(monkeypatch, outcome="no")
    r = _run(tt.portfolio())
    assert r["settledWorthlessCount"] == 1
    assert r["positions"][0]["valueUsd"] == 0.0
    assert r["redeemableCount"] == 0 and r["redeemableNote"] is None


def test_a_won_settled_position_reports_what_it_paid(monkeypatch, fresh_db):
    _one_settled(monkeypatch, outcome="yes")
    r = _run(tt.portfolio())
    assert r["settledWorthlessCount"] == 0
    assert r["positions"][0]["valueUsd"] == 30.0
    assert r["redeemableValueUsd"] == 0.0, "there is nothing left to claim"


# ────────────────────────────────────────────────────────────────────────
# A refused stop must not disarm itself.
# ────────────────────────────────────────────────────────────────────────


def _standing_failing(monkeypatch, ok, placed):
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)  # a Live account
    entries = [{
        "id": "st_1", "conditionId": "0xabc", "title": "T", "side": "yes",
        "kind": "stopLoss", "triggerCents": 40.0, "size": 100,
        "createdAt": "", "armed": True, "lastCheckedAt": None,
        "firedAt": None, "note": None,
    }]
    monkeypatch.setattr(tt, "standing_list", lambda: entries)
    monkeypatch.setattr(tt, "_standing_put", lambda e: None)

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "title": "T", "probability": 30.0}
    monkeypatch.setattr(tt.term, "market", _market)
    _no_live_read(monkeypatch)

    async def _positions(*a, **kw):
        return [{"ticker": "0xabc", "position_fp": 100.0}]
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _preview(req):
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place(req, **kw):
        placed.append(req)
        return {"ok": ok, "message": "size below the market minimum"}
    monkeypatch.setattr(tt, "place", _place)
    return entries


def test_a_refused_stop_stays_armed_and_retries(monkeypatch):
    """The order bounced, the position is still open — retiring the stop that
    protects it is the worst outcome available. It used to do exactly that:
    `armed = False` ran regardless of whether the sell succeeded."""
    placed = []
    entries = _standing_failing(monkeypatch, ok=False, placed=placed)
    _run(tt.standing_tick(allow_trading=True))
    assert entries[0]["armed"] is True, "a refused stop disarmed itself"
    assert entries[0]["firedAt"] is None
    assert entries[0]["failures"] == 1
    assert "still armed" in entries[0]["note"]


def test_a_refused_stop_gives_up_after_a_bounded_number_of_tries(monkeypatch):
    """Bounded, or a deterministic refusal re-sends an order every tick until
    the heat death of the universe."""
    placed = []
    entries = _standing_failing(monkeypatch, ok=False, placed=placed)
    for _ in range(tt.MAX_STANDING_FAILURES):
        _run(tt.standing_tick(allow_trading=True))
    assert entries[0]["armed"] is False
    assert len(placed) == tt.MAX_STANDING_FAILURES
    assert "giving up" in entries[0]["note"]
    # And it stops re-sending once retired.
    _run(tt.standing_tick(allow_trading=True))
    assert len(placed) == tt.MAX_STANDING_FAILURES


def test_a_successful_stop_retires_normally(monkeypatch):
    placed = []
    entries = _standing_failing(monkeypatch, ok=True, placed=placed)
    r = _run(tt.standing_tick(allow_trading=True))
    assert entries[0]["armed"] is False
    assert entries[0]["firedAt"]
    assert r["fired"] == 1
    assert len(placed) == 1


def test_a_refused_stop_is_reported_as_blocked(monkeypatch):
    """It has to reach the user — desktop notification and phone push both key
    off `blocked`."""
    placed = []
    _standing_failing(monkeypatch, ok=False, placed=placed)
    r = _run(tt.standing_tick(allow_trading=True))
    assert r["alerts"] and r["alerts"][0]["blocked"] is True


def test_a_sub_minimum_sell_warns_but_is_not_blocked(monkeypatch):
    """Asymmetric on purpose: a buy can be made bigger, a sell cannot. Blocking
    it would guarantee the user stays stuck in the position. But they have to
    be told, because a stop loss on a sub-minimum holding is not a stop."""
    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "x", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 50.0,
             "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 5,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {"available": True, "asks": [], "note": None,
                "bids": [{"priceCents": 50.0, "size": 100, "cumulative": 100,
                          "notionalUsd": 50.0}]}

    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "sell", "priceCents": 50, "size": 3,
                          "orderType": "FAK"}))
    assert not pv["blockers"], f"a sub-minimum SELL was blocked: {pv['blockers']}"
    assert any("minimum order" in w for w in pv["warnings"])
    assert any("letting it settle" in w for w in pv["warnings"])

    # The same size on a BUY is still a hard blocker — you can just buy more.
    pvb = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                           "action": "buy", "priceCents": 50, "size": 3,
                           "orderType": "FAK"}))
    assert pvb["blockers"], "a sub-minimum BUY should still be blocked"


# ────────────────────────────────────────────────────────────────────────
# Realised vs unrealised: settlement is NOT unrealised.
# ────────────────────────────────────────────────────────────────────────


def _portfolio_with(monkeypatch, fresh_db, *, resolved, side="yes",
                    size=100.0, buy_cents=40.0):
    """One position with a real cost basis, settled or open."""
    import terminal_trade as tt
    _seed_fills([{"action": "buy", "size": size, "price": buy_cents,
                  "side": side}])

    async def _positions(*a, **kw):
        return [{"ticker": "0xdead", "title": "T",
                 "position_fp": size if side == "yes" else -size,
                 "cur_price": 0.6}]

    async def _summaries(_ids):
        return [{"conditionId": "0xdead", "title": "T", "slug": None,
                 "status": "settled" if resolved else "open",
                 "resolvedOutcome": resolved,
                 "closeTime": "2026-01-01T00:00:00Z", "probability": 60.0}]

    async def _balance():
        raise RuntimeError("no key")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def test_a_settled_loser_is_realised_not_unrealised(monkeypatch, fresh_db):
    """The bug on a real account: 105 resolved losers sat under UNREALISED at
    -$229 forever — positions that could never move again — while REALISED
    showed -$17 because the fill ledger only counts SELLS. A resolved market
    pays out without a fill, so settlement was invisible to it."""
    r = _portfolio_with(monkeypatch, fresh_db, resolved="no")
    p = r["positions"][0]
    assert p["settled"] is True
    assert p["unrealizedPnlUsd"] is None, "a decided position claimed unrealised P&L"
    # Bought 100 @ 40c = $40, settled at 0c.
    assert abs(p["settledPnlUsd"] + 40.0) < 1e-6
    assert abs(p["realizedPnlUsd"] + 40.0) < 1e-6
    assert r["totalUnrealizedPnlUsd"] == 0.0
    assert abs(r["realizedPnlUsd"] + 40.0) < 1e-6
    assert abs(r["settledPnlUsd"] + 40.0) < 1e-6


def test_a_settled_winner_is_realised_at_full_value(monkeypatch, fresh_db):
    r = _portfolio_with(monkeypatch, fresh_db, resolved="yes")
    p = r["positions"][0]
    # Bought 100 @ 40c = $40, settled at 100c = $100 → +$60.
    assert abs(p["realizedPnlUsd"] - 60.0) < 1e-6
    assert p["unrealizedPnlUsd"] is None
    assert abs(r["realizedPnlUsd"] - 60.0) < 1e-6


def test_an_open_position_is_unrealised_not_realised(monkeypatch, fresh_db):
    r = _portfolio_with(monkeypatch, fresh_db, resolved=None)
    p = r["positions"][0]
    assert p["settled"] is False
    assert p["settledPnlUsd"] is None
    # Mark 60c vs 40c basis on 100 contracts.
    assert abs(p["unrealizedPnlUsd"] - 20.0) < 1e-6
    assert abs(r["totalUnrealizedPnlUsd"] - 20.0) < 1e-6
    assert r["settledPnlUsd"] is None


def test_settlement_and_earlier_sells_are_both_counted_once(monkeypatch, fresh_db):
    """Half sold at 60c, the rest settles at 100c. Both halves must appear in
    realised, and neither twice."""
    import terminal_trade as tt
    _seed_fills([
        {"action": "buy", "size": 100, "price": 40.0},
        {"action": "sell", "size": 50, "price": 60.0},
    ])

    async def _positions(*a, **kw):
        return [{"ticker": "0xdead", "title": "T", "position_fp": 50.0,
                 "cur_price": 1.0}]

    async def _summaries(_ids):
        return [{"conditionId": "0xdead", "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": "yes",
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no key")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)

    r = _run(tt.portfolio())
    # Sold 50 @ 60c on a 40c basis = +$10. Remaining 50 settle at 100c = +$30.
    assert abs(r["settledPnlUsd"] - 30.0) < 1e-6
    assert abs(r["realizedPnlUsd"] - 40.0) < 1e-6, \
        f"realised was {r['realizedPnlUsd']}, expected 40 (10 sold + 30 settled)"


def test_a_settled_position_with_no_basis_reports_no_pnl(monkeypatch, fresh_db):
    """Knowing the outcome is not knowing the P&L."""
    import terminal_trade as tt

    async def _positions(*a, **kw):
        return [{"ticker": "0xnobasis", "title": "T", "position_fp": 50.0,
                 "cur_price": 0.0}]

    async def _summaries(_ids):
        return [{"conditionId": "0xnobasis", "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": "no",
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _balance():
        raise RuntimeError("no key")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)

    r = _run(tt.portfolio())
    p = r["positions"][0]
    assert p["unrealizedPnlUsd"] is None and p["settledPnlUsd"] is None
    assert p["unreconciled"] is True
    assert r["settledPnlUsd"] is None


# ────────────────────────────────────────────────────────────────────────
# Flatten the account — one button that closes every position you own.
#
# The failure modes here are all "it said it did something it did not do":
# a sell that never left because paper mode was on, an account read that failed
# and was treated as an empty account, or a single stuck position silently
# abandoning the other nineteen.
# ────────────────────────────────────────────────────────────────────────


def _flat(monkeypatch, positions, *, bid=40.0, min_size=None, blockers=(),
          place_ok=True, paper=False, tracked=frozenset(), book_available=True,
          resolved=None, books=None):
    """Wire flatten_wallet up to stubs and hand back the list of sent orders.

    `resolved` maps conditionId -> 'yes' | 'no' for markets that have SETTLED.
    Anything absent is still open, which is the default for every test here —
    a settled position is not sellable and never reaches the book.

    `books` collects every conditionId the book was actually read for, so a
    test can prove the settled ones were dropped BEFORE the read rather than
    refused after it.
    """
    placed: list[dict] = []
    resolved = dict(resolved or {})

    async def _positions(*_a, **_kw):
        return list(positions)
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _summaries(ids):
        return [{"conditionId": c, "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": resolved[c],
                 "outcomes": ["Up", "Down"], "closeTime": None,
                 "probability": None}
                for c in ids if c in resolved]
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt, "_resolution_remember", lambda *_a, **_k: None)

    async def _book(cid, side="yes"):
        if books is not None:
            books.append(cid)
        if not book_available:
            return {"available": False, "bids": [], "asks": [], "bestBid": None,
                    "note": "the venue did not answer"}
        return {"available": True, "bestBid": bid, "bestAsk": None,
                "bids": [{"priceCents": bid, "size": 10_000}] if bid else [],
                "asks": []}
    monkeypatch.setattr(tt.term, "book", _book)

    # The current bid comes off a market read and overrides the book's snapshot.
    # With none, the snapshot is what's left; that override has its own test.
    async def _unread(cid, include_raw=False):
        return None
    monkeypatch.setattr(tt.term, "market", _unread)

    async def _preview(req):
        return {
            "conditionId": req["conditionId"], "side": req["side"],
            "action": req["action"], "priceCents": req["priceCents"],
            "requestedPriceCents": req["priceCents"], "size": req["size"],
            "requestedSize": req["size"],
            "costUsd": round(req["size"] * req["priceCents"] / 100.0, 2),
            "fillableSize": req["size"], "estimatedAvgCents": req["priceCents"],
            "minOrderSize": min_size, "warnings": [],
            "blockers": list(blockers),
        }
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place(req, **kw):
        placed.append({**req, **kw})
        ok = place_ok(req) if callable(place_ok) else place_ok
        return {"ok": ok, "message": "sent" if ok else "the venue said no",
                "orderId": "o1" if ok else None}
    monkeypatch.setattr(tt, "place", _place)

    monkeypatch.setattr(tt, "_paper_mode_on", lambda: paper)
    monkeypatch.setattr(tt, "_engine_tracked_markets",
                        lambda: None if tracked is None else set(tracked))
    return placed


def _held(cid, size=100.0):
    return {"ticker": cid, "position_fp": size}


def test_flatten_sells_every_position_as_a_marketable_fak_at_the_bid(monkeypatch):
    """A limit above the bid RESTS, and a resting exit is not an exit. Every
    order this sends must be a fill-and-kill sell at the price somebody is
    actually bidding."""
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb", -50.0)], bid=37.0)
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xa", "0xb"]
    assert all(p["action"] == "sell" for p in placed)
    assert all(p["orderType"] == "FAK" for p in placed)
    assert all(p["priceCents"] == 37.0 for p in placed), "not sold at the bid"
    # The short leg is a NO holding, not a YES one.
    assert [p["side"] for p in placed] == ["yes", "no"]
    assert [p["size"] for p in placed] == [100.0, 50.0]
    # Every order carries the confirmation the place() gate re-checks.
    assert all(p["confirmUsd"] is not None for p in placed)
    assert r["ok"] is True
    assert (r["attempted"], r["sold"], r["refused"]) == (2, 2, 0)
    assert r["proceedsUsd"] == 55.50


def test_flatten_prices_off_the_current_bid_not_the_books_snapshot(monkeypatch):
    """The book route can be 30 seconds behind, and an exit limited above where the
    market now is just gets killed. NO's bid is the mirror of YES's ask."""
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb", -50.0)], bid=37.0)

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "bid": 30.0, "ask": 40.0}
    monkeypatch.setattr(tt.term, "market", _market)
    _run(tt.flatten_wallet(allow_trading=True))
    assert [(p["side"], p["priceCents"]) for p in placed] == [("yes", 30.0), ("no", 60.0)]


def test_flatten_will_not_sell_on_a_snapshot_when_the_current_read_shows_no_bid(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")], bid=37.0)

    async def _market(cid, include_raw=False):
        return {"conditionId": cid, "bid": None, "ask": 40.0}
    monkeypatch.setattr(tt.term, "market", _market)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == [] and r["refused"] == 1
    assert "Nobody is bidding" in r["results"][0]["message"]


def test_flatten_refuses_once_in_paper_mode_not_once_per_position(monkeypatch):
    """place() refuses each of these anyway — the point is the MESSAGE. Twenty
    identical "paper mode is on" lines is a worse answer than one."""
    placed = _flat(monkeypatch, [_held(f"0x{i}") for i in range(5)], paper=True)
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert placed == [], "a paper-mode flatten sent real orders"
    assert r["ok"] is False
    assert r["results"] == []
    assert r["message"].count("Paper mode is on") == 1
    assert (r["attempted"], r["sold"]) == (0, 0)


def test_flatten_holds_fire_when_the_wallet_cannot_be_read(monkeypatch):
    """An unreadable account is not an empty one. Selling against a position
    list we could not fetch is worse than waiting a tick."""
    placed = _flat(monkeypatch, [])

    async def _boom(*_a, **_kw):
        raise RuntimeError("positions read failed")
    monkeypatch.setattr(tt.api, "get_positions", _boom)

    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["ok"] is False
    assert r["sold"] == 0 and r["attempted"] == 0
    assert "NOTHING was sold" in r["message"]
    assert "positions read failed" in r["message"]


def test_flatten_reads_positions_strictly(monkeypatch):
    """`strict=True` is what makes the read RAISE instead of coming back empty.
    Without it the test above cannot fail."""
    seen = {}

    async def _positions(*a, **kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    _run(tt.flatten_wallet(allow_trading=True))
    assert seen.get("strict") is True


def test_flatten_is_held_to_the_live_trading_switch(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")])
    r = _run(tt.flatten_wallet(allow_trading=False))
    assert placed == []
    assert r["ok"] is False
    assert "Live trading is off" in r["message"]


def test_flatten_carries_on_past_one_refusal(monkeypatch):
    """A stuck position is the most likely thing here. Abandoning the other
    nineteen because of it is the opposite of de-risking."""
    placed = _flat(
        monkeypatch, [_held("0xa"), _held("0xb"), _held("0xc")],
        place_ok=lambda req: req["conditionId"] != "0xb")
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xa", "0xb", "0xc"], \
        "it stopped at the first failure"
    assert (r["attempted"], r["sold"], r["refused"]) == (3, 2, 1)
    assert r["ok"] is False, "a partial flatten reported success"
    bad = [x for x in r["results"] if not x["ok"]]
    assert len(bad) == 1 and bad[0]["conditionId"] == "0xb"
    assert "the venue said no" in bad[0]["message"]


def test_flatten_refuses_a_sub_minimum_sell_instead_of_sending_it(monkeypatch):
    """The venue would bounce it a second later. Refusing here means the
    reason reaches the user instead of an error row."""
    placed = _flat(monkeypatch, [_held("0xa", 3.0), _held("0xb", 40.0)],
                   min_size=5.0)
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xb"]
    assert (r["attempted"], r["sold"], r["refused"]) == (2, 1, 1)
    stuck = [x for x in r["results"] if x["conditionId"] == "0xa"][0]
    assert "minimum order" in stuck["message"]
    assert "settle" in stuck["message"], "it did not say what the user can do"


def test_flatten_will_not_sell_into_a_book_it_could_not_read(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")], book_available=False)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["refused"] == 1
    assert "did not answer" in r["results"][0]["message"]


def test_flatten_will_not_sell_where_nobody_is_bidding(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")], bid=None)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["refused"] == 1
    assert "Nobody is bidding" in r["results"][0]["message"]


def test_flatten_surfaces_a_blocker_from_the_preview(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa")],
                   blockers=("This market is closed. Nothing can be traded here.",))
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["results"][0]["message"].startswith("This market is closed")


def test_flatten_says_how_many_sold_positions_an_engine_was_tracking(monkeypatch):
    """Selling one of these from here is fine — the engine's wallet reconcile
    books an externally-closed position as sold — but the user has to be told
    that this button reached past their own trades."""
    _flat(monkeypatch, [_held("0xa"), _held("0xb")], tracked={"0xb"})
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["engineTrackedSold"] == 1
    assert "1 of the sold position(s) were being tracked by an engine" in r["message"]


def test_flatten_reports_an_unreadable_engine_table_as_unknown(monkeypatch):
    """Never zero. A count we could not read is not a count of none."""
    _flat(monkeypatch, [_held("0xa")], tracked=None)
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["engineTrackedSold"] is None
    assert "unknown" in r["message"]


def test_flatten_counts_empty_wallet_rows_as_skipped_not_sold(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), {"ticker": "0xz", "position_fp": 0.0}])
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert len(placed) == 1
    assert (r["attempted"], r["sold"], r["skipped"]) == (1, 1, 1)


def test_flatten_says_so_when_there_is_nothing_to_sell(monkeypatch):
    placed = _flat(monkeypatch, [])
    r = _run(tt.flatten_wallet(allow_trading=True))
    assert placed == []
    assert r["ok"] is True
    assert r["proceedsUsd"] == 0.0
    assert "Nothing to sell" in r["message"]


def test_flatten_leaves_proceeds_unknown_when_a_sell_could_not_be_priced(
        monkeypatch):
    """A total that silently drops the leg it could not price is a confident
    lie about how much came back."""
    _flat(monkeypatch, [_held("0xa"), _held("0xb")])
    real_preview = tt.preview

    async def _preview(req):
        pv = await real_preview(req)
        if req["conditionId"] == "0xb":
            pv["costUsd"] = None
        return pv
    monkeypatch.setattr(tt, "preview", _preview)

    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["sold"] == 2
    assert r["proceedsUsd"] is None
    assert "unknown until the fills reconcile" in r["message"]


def test_flatten_reports_a_killed_remainder_rather_than_just_order_sent(
        monkeypatch):
    """An FAK that crosses half the size leaves half the position OPEN, and a
    result that only says "order sent" hides that."""
    _flat(monkeypatch, [_held("0xa")])
    real_preview = tt.preview

    async def _preview(req):
        pv = await real_preview(req)
        pv["fillableSize"] = 30.0
        pv["warnings"] = ["Only 30 of 100 contracts are available at 40 cents "
                          "or better. The remainder will be killed, not rested."]
        return pv
    monkeypatch.setattr(tt, "preview", _preview)

    r = _run(tt.flatten_wallet(allow_trading=True))
    assert r["sold"] == 1
    assert "killed, not rested" in r["results"][0]["message"]


# ── the preview half: read-only, and it spends nothing ──────────────────


def test_flatten_preview_prices_without_sending_anything(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb", 50.0)], bid=40.0)
    r = _run(tt.flatten_preview())
    assert placed == [], "the preview sent an order"
    assert r["ok"] is True
    assert r["count"] == 2
    assert r["proceedsUsd"] == 60.0
    assert [p["bidCents"] for p in r["positions"]] == [40.0, 40.0]


def test_flatten_preview_keeps_an_unreadable_wallet_unknown(monkeypatch):
    _flat(monkeypatch, [])

    async def _boom(*_a, **_kw):
        raise RuntimeError("positions read failed")
    monkeypatch.setattr(tt.api, "get_positions", _boom)

    r = _run(tt.flatten_preview())
    assert r["ok"] is False
    assert r["count"] == 0 and r["proceedsUsd"] is None
    assert "Could not read your positions" in r["message"]


def test_flatten_preview_names_what_cannot_be_sold(monkeypatch):
    _flat(monkeypatch, [_held("0xa", 2.0), _held("0xb", 60.0)], min_size=5.0)
    r = _run(tt.flatten_preview())
    assert r["count"] == 1, "a position that cannot be sold was counted"
    blocked = [p for p in r["positions"] if p["blocked"]]
    assert len(blocked) == 1 and blocked[0]["conditionId"] == "0xa"
    assert blocked[0]["proceedsUsd"] is None
    # 60 contracts at 40c — the sellable leg only.
    assert r["proceedsUsd"] == 24.0


# ── settled positions are not sellable, and never were ──────────────────
#
# The owner's report: "the Flatten all preview lists every one of the 105
# settled positions, each refused with 'Could not read the order book', and
# the button reads Sell 0 positions."
#
# Every one of those refusals was TRUE and every one was about the wrong
# question. A resolved market is closed: there is no book because there is
# nothing left to trade. A resolved loser is worth exactly $0 and the exchange
# pays a resolved winner automatically. So they are dropped before any book
# read, counted once, and explained once.


def test_flatten_preview_drops_settled_positions_before_reading_a_book(monkeypatch):
    reads: list[str] = []
    _flat(monkeypatch,
          [_held("0xs1"), _held("0xs2"), _held("0xs3"),
           _held("0xo1"), _held("0xo2", 50.0)],
          bid=40.0, books=reads,
          resolved={"0xs1": "yes", "0xs2": "no", "0xs3": "yes"})
    r = _run(tt.flatten_preview())

    assert r["ok"] is True
    assert [p["conditionId"] for p in r["positions"]] == ["0xo1", "0xo2"]
    assert r["count"] == 2, "the button would say Sell 0 positions"
    assert r["settledSkipped"] == 3
    # The whole point: the settled ones cost nothing to refuse, because they
    # were never priced.
    assert reads == ["0xo1", "0xo2"], "a settled market was sent to the book"
    assert not any(p["blocked"] for p in r["positions"])
    assert "3 settled position(s) were left out" in r["message"]


def test_flatten_preview_says_nothing_to_sell_when_every_position_settled(monkeypatch):
    reads: list[str] = []
    _flat(monkeypatch, [_held("0xa"), _held("0xb")], books=reads,
          resolved={"0xa": "yes", "0xb": "no"})
    r = _run(tt.flatten_preview())

    assert r["ok"] is True
    assert r["positions"] == [] and r["count"] == 0
    assert r["settledSkipped"] == 2
    assert reads == [], "a settled account still walked the book"
    assert "every position in the account is settled" in r["message"]


def test_flatten_leaves_settled_positions_alone_and_says_so(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xs"), _held("0xo", 60.0)],
                   bid=40.0, resolved={"0xs": "no"})
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert [p["conditionId"] for p in placed] == ["0xo"]
    assert (r["attempted"], r["sold"], r["refused"]) == (1, 1, 0)
    assert r["settledSkipped"] == 1
    assert "1 settled position(s) were left alone" in r["message"]


def test_flatten_of_an_all_settled_account_sells_nothing(monkeypatch):
    placed = _flat(monkeypatch, [_held("0xa"), _held("0xb")],
                   resolved={"0xa": "yes", "0xb": "yes"})
    r = _run(tt.flatten_wallet(allow_trading=True))

    assert placed == []
    assert r["ok"] is True and r["attempted"] == 0
    assert r["settledSkipped"] == 2
    assert "every position in the account is settled" in r["message"]


def test_an_unreadable_resolution_leaves_the_position_open(monkeypatch):
    """Unknown is not settled. If the gateway will not answer we still try to sell —
    the book read then refuses it honestly, one row at a time, instead of the
    position being silently written off as "resolved, nothing to do"."""
    async def _boom(_ids):
        raise RuntimeError("the gateway is down")
    reads: list[str] = []
    _flat(monkeypatch, [_held("0xa")], bid=40.0, books=reads)
    monkeypatch.setattr(tt.term, "_summaries_for", _boom)

    r = _run(tt.flatten_preview())
    assert r["settledSkipped"] == 0
    assert r["count"] == 1 and reads == ["0xa"]


def test_the_unreadable_book_refusal_has_one_full_stop(monkeypatch):
    """`terminal.book`'s note is a finished sentence. Appending our own gave
    "…This is a failed read, not an empty book.. Nothing was sold here…"."""
    _flat(monkeypatch, [_held("0xa")])

    async def _book(_cid, _side="yes"):
        return {"available": False, "bids": [], "asks": [], "bestBid": None,
                "note": ("The exchange did not return a book for this market. This "
                         "is a failed read, not an empty book.")}
    monkeypatch.setattr(tt.term, "book", _book)

    r = _run(tt.flatten_preview())
    blocked = r["positions"][0]["blocked"]
    assert ".." not in blocked, blocked
    assert blocked.startswith("Could not read the order book — ")
    assert "not an empty book. Nothing was sold here" in blocked
