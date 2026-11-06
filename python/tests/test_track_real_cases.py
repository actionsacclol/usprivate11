"""The Track screen, pinned against the cases that were actually wrong.

Every case in this file comes from diagnosing five complaints about this page on
a real account: no cost basis, "it says I lost most trades", everything badged
claimable, wrong P&L, and every row labelled "placed elsewhere".

The one thing every test below is really guarding: OUTCOME IDENTITY IS AN
INDEX, NOT A WORD. A market's first outcome is spelled "Yes" on a binary market,
"Up" on a crypto one, "Over" on a total, and a team or player name on a sports
one. Code that asked ``outcome == "yes"`` filed everything else under 'no', so
823 of one account's 1,502 fills sat on the opposite key from the position they
paid for — and a position whose buys and sells land on different keys has no
cost basis at all.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt
import us_api as us

BTC_UP = "cpc-btc-updown-15m-2026-07-24-0230z"
DOGE_DOWN = "cpc-doge-updown-15m-2026-07-24-0230z"
GIANTS = "aec-mlb-atl-sf-2026-07-24"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "track.db")
    db.init_db()
    return tmp_path


# ────────────────────────────────────────────────────────────────────────
# Cost basis — the user's account is the user's account
# ────────────────────────────────────────────────────────────────────────


def _seed(rows):
    with db.get_db() as conn:
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                     (trade_id, order_id, condition_id, title, side, action,
                      size, price_cents, fees_usd, cost_usd, origin, network,
                      filled_at)
                   VALUES (?,NULL,?,'',?,?,?,?,0.0,?,?,'mainnet',?)""",
                (f"f{i}", r["cid"], r["side"], r["action"], r["size"],
                 r["price"], r["size"] * r["price"] / 100.0,
                 r.get("origin", "external"),
                 f"2026-07-0{i + 1}T00:00:00Z"))


def _portfolio(monkeypatch, positions, summaries):
    async def _positions(*a, **kw):
        return positions

    async def _summaries(_ids):
        return summaries

    async def _balance():
        raise RuntimeError("no key in tests")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def _pos(cid, *, idx, size, avg, current=0.0, title="M"):
    """One position through the real mapper. The venue signs the size (NO negative),
    and its cost and cash value are cash, so they read the same on either side.
    None leaves that figure off the row."""
    raw = {"netPositionDecimal": str(size if idx == 0 else -size),
           "realized": {"value": "-0.0679", "currency": "USD"},
           "marketMetadata": {"title": title, "eventSlug": "e"}}
    if avg is not None:
        raw["cost"] = {"value": f"{size * avg:.4f}", "currency": "USD"}
    if current is not None:
        raw["cashValue"] = {"value": f"{current:.4f}", "currency": "USD"}
    row = us.position_row(cid, raw)
    assert row is not None
    return row


def _sum(cid, *, outcomes, resolved, title="M"):
    return {"conditionId": cid, "title": title, "slug": "s",
            "status": "settled" if resolved else "open",
            "outcomes": list(outcomes), "resolvedOutcome": resolved,
            "closeTime": "2026-07-24T00:00:00Z"}


def test_an_engine_placed_fill_still_gives_the_position_a_cost_basis(
        monkeypatch, fresh_db):
    """origin='external' is what every fill on this account was stamped, because
    the engines do not write terminal_orders rows. Letting origin decide whether
    a position HAS a basis meant an account that had spent $634 showed a cost of
    nothing. Origin picks which track record a trade counts toward; it never
    decides whether the money was spent."""
    _seed([{"cid": BTC_UP, "side": "yes", "action": "buy", "size": 33.3333,
            "price": 3.0, "origin": "external"}])
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["avgCostCents"] == 3.0
    assert p["basisSource"] == "ledger"
    assert p["unreconciled"] is False
    assert r["totalCostUsd"] == pytest.approx(1.0, abs=0.01)


def test_a_fill_is_filed_under_the_side_it_was_sent_for(monkeypatch, fresh_db):
    """A fill's key and a position's key have to meet, or the position has no basis.
    The order's side comes off our own row, whatever the market calls its outcomes,
    and the position's off the sign of its size: both are the index, never a word."""
    with db.get_db() as conn:
        for oid, cid, side in (("o1", BTC_UP, "yes"), ("o2", DOGE_DOWN, "no")):
            conn.execute(
                """INSERT INTO terminal_orders
                     (condition_id, title, side, action, order_type, requested_size,
                      requested_price_cents, order_id, status, origin, network)
                   VALUES (?,'M',?,'buy','GTC',10,40,?,'placed','manual','mainnet')""",
                (cid, side, oid))

    async def _get_order(oid):
        # Nothing in the reply decides the side. It may not even carry an intent.
        return {"order": {"id": oid, "status": "matched", "size_matched": 10.0,
                          "price": 0.40, "side": "yes", "fee_usd": 0.1, "created_at": ""}}

    async def _no_trades(limit=500, *, user=None, market=""):
        return []
    monkeypatch.setattr(tt.api, "get_order", _get_order)
    monkeypatch.setattr(tt.api, "get_activity", _no_trades)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)
    _run(tt.reconcile_fills())

    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.5), _pos(DOGE_DOWN, idx=1, size=10, avg=0.5)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved=None),
         _sum(DOGE_DOWN, outcomes=["Up", "Down"], resolved=None)])
    by_id = {p["conditionId"]: p for p in r["positions"]}
    assert by_id[BTC_UP]["basisSource"] == "ledger" and by_id[BTC_UP]["sideLabel"] == "Up"
    assert by_id[DOGE_DOWN]["basisSource"] == "ledger" and by_id[DOGE_DOWN]["sideLabel"] == "Down"
    assert by_id[DOGE_DOWN]["avgCostCents"] == 40.0, "the ledger's price, not the venue's average"


def test_a_position_our_ledger_cannot_reach_uses_the_venues_own_average(
        monkeypatch, fresh_db):
    """History older than anything we ever imported. The venue keeps its own
    average for THIS account, so showing "—" for a position we can see the cost
    of is a worse answer than showing theirs and saying whose it is."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["avgCostCents"] == 2.99
    assert p["basisSource"] == "data-api"
    assert p["sources"]["avgCost"]["source"] == "data-api", "provenance is mandatory"
    assert p["unreconciled"] is False, "a basis is a basis, wherever it came from"
    assert r["venueBasisCount"] == 1
    assert "Polymarket's own average" in (r["unreconciledNote"] or "")


def test_with_no_basis_anywhere_it_stays_unknown_and_out_of_the_totals(
        monkeypatch, fresh_db):
    """The mapper reads a missing cost as an average of 0. Taken at its word that is
    a free position, whose whole value would report as profit."""
    r = _portfolio(monkeypatch, [_pos(BTC_UP, idx=0, size=10, avg=None)],
                   [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["avgCostCents"] is None and p["basisSource"] is None
    assert p["unrealizedPnlUsd"] is None and p["settledPnlUsd"] is None
    assert p["unreconciled"] is True
    assert r["unreconciledCount"] == 1
    assert r["totalCostUsd"] is None


# ────────────────────────────────────────────────────────────────────────
# Won or lost — through the index, in every vocabulary
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("outcomes,label", [
    (["Yes", "No"], "Yes"),
    (["Up", "Down"], "Up"),
    (["Over", "Under"], "Over"),
    (["Atlanta Braves", "San Francisco Giants"], "Atlanta Braves"),
])
def test_holding_the_winning_outcome_reads_as_a_win_in_every_vocabulary(
        monkeypatch, fresh_db, outcomes, label):
    """`resolvedOutcome` and `side` are BOTH index-derived, so this comparison
    means the same thing on a Yes/No, an Up/Down, a total and a named-team
    market. It is also the reason the settled badge can say "settled won"
    without the label text ever entering the arithmetic."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.40, current=10.0)],
        [_sum(BTC_UP, outcomes=outcomes, resolved="yes")])
    p = r["positions"][0]
    assert p["side"] == "yes"
    assert p["sideLabel"] == label
    assert p["resolvedOutcome"] == "yes"
    assert p["resolvedOutcomeLabel"] == outcomes[0]
    assert p["markCents"] == 100.0, "a decided winner is worth exactly $1"
    assert p["settledPnlUsd"] == pytest.approx(6.0)


def test_holding_the_losing_outcome_reads_as_a_loss_not_as_missing_data(
        monkeypatch, fresh_db):
    r = _portfolio(
        monkeypatch,
        [_pos(DOGE_DOWN, idx=1, size=29.9987, avg=0.16)],
        [_sum(DOGE_DOWN, outcomes=["Up", "Down"], resolved="yes")])
    p = r["positions"][0]
    assert p["side"] == "no" and p["sideLabel"] == "Down"
    assert p["resolvedOutcome"] == "yes" and p["resolvedOutcomeLabel"] == "Up"
    assert p["markCents"] == 0.0
    assert p["settledPnlUsd"] == pytest.approx(-4.80, abs=0.01)


def test_an_unread_outcome_stays_unknown_rather_than_becoming_a_loss(
        monkeypatch, fresh_db):
    """`resolvedOutcome == side` is false for BOTH a loss and an outcome we
    could not read, and only one of those is a claim we are entitled to make."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.4, current=4.0)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved=None)])
    p = r["positions"][0]
    assert p["settled"] is False
    assert p["resolvedOutcome"] is None
    assert p["markCents"] == 40.0, "still a mark, not a settlement"


# ────────────────────────────────────────────────────────────────────────
# Nothing to claim — settlement pays automatically
# ────────────────────────────────────────────────────────────────────────


def test_a_worthless_resolved_position_is_counted_not_advertised(monkeypatch, fresh_db):
    """105 expired losers, and the page once told the user all of them were "ready to
    redeem — claim them to turn them back into cash". Total claimable: $0.00."""
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299, current=0.0)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    assert r["positions"][0]["redeemable"] is False
    assert r["redeemableCount"] == 0 and r["redeemableNote"] is None
    assert r["settledWorthlessCount"] == 1


def test_a_won_position_is_worth_its_payout_and_has_nothing_to_claim(monkeypatch, fresh_db):
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.4, current=10.0)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="yes")])
    p = r["positions"][0]
    assert p["redeemable"] is False and p["valueUsd"] == pytest.approx(10.0)
    assert r["redeemableCount"] == 0 and r["redeemableValueUsd"] == 0.0
    assert r["settledWorthlessCount"] == 0


def test_a_position_with_no_readable_value_is_unknown_not_worthless(monkeypatch, fresh_db):
    """No cash value on the row and no price or outcome on the market. We cannot see
    what it is worth, and a 0c mark would report it as a total loss."""
    r = _portfolio(monkeypatch, [_pos(BTC_UP, idx=0, size=10, avg=0.4, current=None)],
                   [{"conditionId": BTC_UP, "title": "M", "slug": None,
                     "status": "closed", "resolvedOutcome": None,
                     "closeTime": None, "outcomes": ["Up", "Down"]}])
    row = r["positions"][0]
    assert row["markCents"] is None and row["valueUsd"] is None
    assert any(u["field"] == "markCents" for u in row["unknowns"])
    assert r["settledWorthlessCount"] == 0, "unknown is not the same as worthless"


@pytest.mark.parametrize("idx,want", [(0, 62.0), (1, 38.0)])
def test_an_open_position_with_no_cash_value_marks_off_the_market(
        monkeypatch, fresh_db, idx, want):
    r = _portfolio(monkeypatch, [_pos(BTC_UP, idx=idx, size=10, avg=0.4, current=None)],
                   [{**_sum(BTC_UP, outcomes=["Up", "Down"], resolved=None), "probability": 62.0}])
    assert r["positions"][0]["markCents"] == want


# ────────────────────────────────────────────────────────────────────────
# P&L — reconciled against Polymarket's own figure
# ────────────────────────────────────────────────────────────────────────


def test_our_settled_pnl_agrees_with_the_venues_cash_pnl(monkeypatch, fresh_db):
    """Five positions, one from each outcome vocabulary, all lost. Our arithmetic and
    the venue's cost and value have to land on the same figure."""
    rows = [
        (BTC_UP, 0, 33.3333, 0.0299, ["Up", "Down"]),
        (DOGE_DOWN, 1, 29.9987, 0.16, ["Up", "Down"]),
        ("tec-mlb-atl-sf-2026-07-24-over", 0, 6.0, 0.4599, ["Over", "Under"]),
        ("aec-wta-muc-osa-2026-07-24", 1, 7.0, 0.6099, ["Karolina Muchova", "Naomi Osaka"]),
        (GIANTS, 1, 5.0, 0.61, ["Atlanta Braves", "San Francisco Giants"]),
    ]
    positions = [_pos(cid, idx=i, size=sz, avg=avg) for cid, i, sz, avg, _ in rows]
    # Every one of these lost, so the winner is always the OTHER outcome.
    summaries = [_sum(cid, outcomes=outs, resolved="no" if i == 0 else "yes")
                 for cid, i, _sz, _avg, outs in rows]
    r = _portfolio(monkeypatch, positions, summaries)

    assert all(p["markCents"] == 0.0 for p in r["positions"])
    assert r["settledPnlUsd"] == pytest.approx(r["venueCashPnlUsd"], abs=0.01)
    assert r["settledPnlUsd"] == pytest.approx(-15.88, abs=0.01)
    assert r["redeemableCount"] == 0 and r["settledWorthlessCount"] == 5


def test_every_position_carries_the_venues_own_pnl_for_comparison(
        monkeypatch, fresh_db):
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=33.3333, avg=0.0299)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    p = r["positions"][0]
    assert p["venueCashPnlUsd"] == pytest.approx(-1.0, abs=0.01)
    assert p["settledPnlUsd"] == pytest.approx(p["venueCashPnlUsd"], abs=0.01)


# ────────────────────────────────────────────────────────────────────────
# Origin
# ────────────────────────────────────────────────────────────────────────


def test_a_position_no_longer_claims_an_origin(monkeypatch, fresh_db):
    """Every fill on one account was stamped 'external' — the engines write no
    terminal_orders rows — so the Positions and Fill-ledger tables labelled all
    1,502 of the user's own trades "Placed elsewhere". The column is gone; the
    DB column stays, because who placed an order is still worth recording."""
    _seed([{"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10,
            "price": 40.0, "origin": "external"}])
    r = _portfolio(
        monkeypatch,
        [_pos(BTC_UP, idx=0, size=10, avg=0.4)],
        [_sum(BTC_UP, outcomes=["Up", "Down"], resolved="no")])
    assert "origin" not in r["positions"][0]
    # Still on the fill row, and still in the database.
    assert tt.fills()["fills"][0]["origin"] == "external"


def test_the_fill_ledger_counts_every_origin_towards_the_basis(fresh_db):
    """One market, bought by the crypto engine and by hand. Average cost is
    across BOTH, because both spent the same balance."""
    _seed([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10,
         "price": 40.0, "origin": "external"},
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10,
         "price": 60.0, "origin": "manual"},
    ])
    st = tt._ledger_basis()[(BTC_UP, "yes")]
    assert st["size"] == 20
    assert st["cost"] == pytest.approx(10.0)
    assert "origin" not in st, "origin must not reach the basis calculation"
