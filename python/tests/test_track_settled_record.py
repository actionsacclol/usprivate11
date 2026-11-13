""""Show settled" must show the RECORD, not the wallet's dust.

The owner's report, after the ledger repair landed: *"when i click on show
settled doesnt show any wins, only 105 losses, but i have 453 wins and 117
losses."*

Every one of those 105 rows was true. The table was still a lie, because of
what it could not contain. `/positions` is the WALLET, and on a prediction
market a wallet is a survivorship filter with the sign flipped:

  * a resolved WINNER leaves it the moment it is redeemed — and a redemption
    is an on-chain claim, not a CLOB trade, so it leaves no sell row either;
  * a resolved LOSER stays in it forever as $0 dust, because there is nothing
    to claim and no reason to spend the gas.

So the wallet's settled rows are, by construction, the losses and only the
losses. `settled_record()` reads the FILL LEDGER instead: every market it ever
settled, paid or not, still held or not.

Shaped from the same real wallet as `test_track_real_cases.py` and
`test_track_ledger_record.py`. Public market ids only — a condition id names a
market, not a person.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt

BTC_UP = "0xf996adbcb9d5e40162d514d1616325982ae9ef5a48b2622db107018df80e8884"
DOGE_DOWN = "0x322f0d6502dc08f4f61c06d9fbba252711ac250a625330f92984ff4d0cb423e7"
GIANTS = "0x38644f38c63679e539c578c59f4a1be38eb4693d1bdaf0ead50f70b824efb6ba"
ETH_UP = "0x7d13e4a513aeabd0f0ed3c5703b0195d5feb6e23d17ccdac86f8a1ff41e3635a"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "record.db")
    db.init_db()
    # The resolution and market caches are scoped to the DB file and clear
    # themselves when it moves, but ask for that explicitly so one test can
    # never answer another test's questions.
    tt._resolution_scope_check()
    return tmp_path


def _fills(rows):
    """Seed the fill ledger. `rows`: dicts of cid/side/action/size/price/fee."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM terminal_fills")
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                   (trade_id, order_id, condition_id, title, side, action,
                    size, price_cents, fees_usd, cost_usd, origin, network,
                    filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"t{i}", None, r["cid"], r.get("title", "T"),
                 r.get("side", "yes"), r["action"], r["size"], r["price"],
                 r.get("fee", 0.0), r["size"] * r["price"] / 100.0,
                 r.get("origin", "manual"), "mainnet",
                 r.get("at", f"2026-01-01T00:00:{i:02d}Z")))


def _record(monkeypatch, *, wallet=(), markets=None, wallet_error=False,
            calls=None):
    """Run `settled_record()` against a stubbed wallet and stubbed Gamma.

    `markets` maps conditionId -> dict(outcome=..., labels=[...], close=...).
    A market absent from it is one Gamma would not answer for at all, which is
    the "we could not read the outcome" case — never "lost".
    """
    markets = dict(markets or {})

    async def _positions(*_a, **_kw):
        if wallet_error:
            raise RuntimeError("data-api is down")
        return [
            {"ticker": w["cid"], "title": w.get("title", "T"),
             "position_fp": w["size"] if w.get("side", "yes") == "yes" else -w["size"],
             "cur_price": w.get("price", 0.0), "redeemable": False,
             "_raw": None}
            for w in wallet
        ]

    async def _summaries(ids):
        if calls is not None:
            calls.append(list(ids))
        out = []
        for cid in ids:
            m = markets.get(cid)
            if m is None:
                continue
            out.append({
                "conditionId": cid, "title": m.get("title", "T"), "slug": None,
                "status": "settled" if m.get("outcome") else "open",
                "resolvedOutcome": m.get("outcome"),
                "outcomes": m.get("labels", ["Up", "Down"]),
                "closeTime": m.get("close"), "probability": 50.0,
            })
        return out

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    return _run(tt.settled_record())


# ────────────────────────────────────────────────────────────────────────
# The bug itself: the wins are not in the wallet
# ────────────────────────────────────────────────────────────────────────


def test_a_redeemed_winner_is_in_the_record_and_not_in_the_wallet(fresh_db, monkeypatch):
    """THE regression, in one row. 5 contracts at 40c on a market that
    resolved our way, redeemed, and therefore absent from /positions. The old
    "Show settled" could not show it at all."""
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0, "fee": 0.10}])
    r = _record(monkeypatch, wallet=[],
                markets={BTC_UP: {"outcome": "yes", "labels": ["Up", "Down"],
                                  "close": "2026-01-02T00:00:00Z"}})

    assert len(r["rows"]) == 1
    row = r["rows"][0]
    assert row["outcome"] == "won"
    assert row["inWallet"] is False, "a redeemed winner is gone from the wallet"
    assert row["contracts"] == 5.0
    assert row["payoutUsd"] == pytest.approx(5.0)
    assert row["costUsd"] == pytest.approx(2.0)
    assert row["pnlUsd"] == pytest.approx(2.90)   # 5.00 - 2.00 - 0.10
    assert row["avgCostCents"] == pytest.approx(40.0)
    assert row["sideLabel"] == "Up"
    assert row["resolvedOutcomeLabel"] == "Up"
    assert row["settledAt"] == "2026-01-02T00:00:00Z"
    assert r["totals"]["won"] == 1 and r["totals"]["lost"] == 0


def test_a_dust_loser_is_in_the_record_and_still_in_the_wallet(fresh_db, monkeypatch):
    """The other half. The loser never leaves — there is nothing to claim —
    so it is the one thing the wallet CAN show, which is exactly why the
    wallet alone reported "105 losses"."""
    _fills([{"cid": DOGE_DOWN, "side": "no", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _record(
        monkeypatch,
        wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0, "price": 0.0}],
        markets={DOGE_DOWN: {"outcome": "yes", "labels": ["Up", "Down"]}})

    row = r["rows"][0]
    assert row["outcome"] == "lost"
    assert row["inWallet"] is True, "a resolved loser sits in the wallet as dust"
    assert row["payoutUsd"] == pytest.approx(0.0)
    assert row["pnlUsd"] == pytest.approx(-2.0)
    assert row["sideLabel"] == "Down"
    # The venue never told us when it closed, so it is an em dash, not a date.
    assert row["settledAt"] is None
    assert r["totals"]["lost"] == 1 and r["totals"]["won"] == 0


def test_the_record_shows_the_wins_the_wallet_cannot(fresh_db, monkeypatch):
    """The real complaint in miniature: three winners redeemed and gone, one
    loser left in the wallet. The wallet says "1 settled, 1 loss". The record
    says 3 won, 1 lost, and a profit."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 45.0},
        {"cid": GIANTS, "side": "no", "action": "buy", "size": 5.0, "price": 50.0},
        {"cid": DOGE_DOWN, "side": "no", "action": "buy", "size": 5.0, "price": 40.0},
    ])
    markets = {BTC_UP: {"outcome": "yes"}, ETH_UP: {"outcome": "yes"},
               GIANTS: {"outcome": "no"}, DOGE_DOWN: {"outcome": "yes"}}
    r = _record(monkeypatch,
                wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0}],
                markets=markets)

    assert r["totals"]["won"] == 3
    assert r["totals"]["lost"] == 1
    assert r["totals"]["markets"] == 4
    assert [x["outcome"] for x in r["rows"] if x["outcome"] == "won"].count("won") == 3
    assert r["totals"]["pnlUsd"] == pytest.approx(6.25)   # +3 +2.75 +2.5 -2
    assert sorted(x["inWallet"] for x in r["rows"]) == [False, False, False, True]


# ────────────────────────────────────────────────────────────────────────
# What the outcome did NOT decide
# ────────────────────────────────────────────────────────────────────────


def test_a_closed_out_market_has_no_payout_and_its_pnl_is_the_sells(fresh_db, monkeypatch):
    """Bought 10 at 50c, sold all 10 at 60c. Nothing settled here — the
    position was gone before the outcome existed — so the payout column is an
    em dash and the P&L is what the sells banked."""
    _fills([
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 10.0,
         "price": 50.0, "fee": 0.20},
        {"cid": ETH_UP, "side": "yes", "action": "sell", "size": 10.0,
         "price": 60.0, "fee": 0.25},
    ])
    r = _record(monkeypatch, wallet=[], markets={ETH_UP: {"outcome": "yes"}})

    row = r["rows"][0]
    assert row["outcome"] == "closedOut"
    assert row["payoutUsd"] is None, "a market we sold out of paid us nothing"
    assert row["contracts"] == 0.0
    assert row["avgCostCents"] is None, "no contracts left to have an average"
    assert row["soldPnlUsd"] == pytest.approx(1.0)
    assert row["pnlUsd"] == pytest.approx(0.55)   # 1.00 - 0.45 of fees
    assert r["totals"]["closedOut"] == 1
    assert r["totals"]["won"] == 0 and r["totals"]["lost"] == 0


def test_an_unreadable_outcome_is_unknown_never_a_loss(fresh_db, monkeypatch):
    _fills([{"cid": GIANTS, "side": "yes", "action": "buy",
             "size": 4.0, "price": 30.0}])
    r = _record(monkeypatch, wallet=[], markets={})   # Gamma answers nothing.

    row = r["rows"][0]
    assert row["outcome"] == "unknown"
    assert row["payoutUsd"] is None and row["pnlUsd"] is None
    assert row["sideLabel"] is None, "no labels means no invented outcome word"
    assert r["totals"]["unknown"] == 1
    assert r["totals"]["lost"] == 0, "an unknown outcome was counted as a loss"
    assert r["totals"]["pnlUsd"] is None
    assert r["totals"]["pnlMissingRows"] == 1
    assert "could not read" in r["note"]


def test_an_open_position_is_not_in_the_settled_record(fresh_db, monkeypatch):
    """Live 2026-11-07: a position still open was listed as `unknown`, with the
    market's future close as its settledAt and a note that the outcome "could
    not be read". It hadn't happened yet."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 6.0, "price": 50.0},
    ])
    r = _record(monkeypatch, wallet=[{"cid": ETH_UP, "size": 6.0}],
                markets={BTC_UP: {"outcome": "yes", "close": "2026-01-02T00:00:00Z"},
                         ETH_UP: {"outcome": None, "close": "2099-01-01T00:00:00Z"}})

    assert [row["conditionId"] for row in r["rows"]] == [BTC_UP]
    t = r["totals"]
    assert (t["won"], t["unknown"], t["closedOut"], t["markets"]) == (1, 0, 0, 1)
    assert t["openMarkets"] == 1 and t["pnlMissingRows"] == 0
    assert "could not read" not in r["note"] and "not closed yet" in r["note"]

    # An open market is re-asked only every few minutes. Between asks it is
    # still open, not unknown.
    again = _run(tt.settled_record())
    assert [row["conditionId"] for row in again["rows"]] == [BTC_UP]
    assert again["totals"]["openMarkets"] == 1


def test_a_closed_market_with_no_result_yet_is_unknown_with_no_settle_time(
        fresh_db, monkeypatch):
    _fills([{"cid": GIANTS, "side": "yes", "action": "buy", "size": 4.0, "price": 30.0}])

    async def _closed(ids):
        return [{"conditionId": GIANTS, "title": "T", "status": "closed",
                 "resolvedOutcome": None, "outcomes": ["Yes", "No"],
                 "closeTime": "2026-01-01T00:00:00Z"}]

    async def _positions(*_a, **_kw):
        return []
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _closed)
    r = _run(tt.settled_record())
    [row] = r["rows"]
    assert row["outcome"] == "unknown" and row["settledAt"] is None
    assert r["totals"]["unknown"] == 1 and r["totals"]["openMarkets"] == 0


def test_a_market_held_on_both_sides_is_labelled_not_double_counted(fresh_db, monkeypatch):
    """A paired crypto entry holds YES and NO into settlement, so it is a win
    AND a loss. It is shown once per side, both rows flagged, and the totals
    say how many are in both columns instead of pretending they add up."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10.0, "price": 51.0},
        {"cid": BTC_UP, "side": "no", "action": "buy", "size": 10.0, "price": 48.0},
    ])
    r = _record(monkeypatch, wallet=[],
                markets={BTC_UP: {"outcome": "yes", "labels": ["Up", "Down"]}})

    assert len(r["rows"]) == 2
    assert all(x["bothSides"] for x in r["rows"])
    assert {x["outcome"] for x in r["rows"]} == {"won", "lost"}
    t = r["totals"]
    assert (t["won"], t["lost"], t["bothSides"], t["markets"]) == (1, 1, 1, 1)
    assert t["won"] + t["lost"] != t["markets"], "the counts pretend to add up"
    # 10 contracts pay $10 against $9.90 of combined cost.
    assert t["pnlUsd"] == pytest.approx(0.10)


def test_a_residual_too_small_to_redeem_does_not_score(fresh_db, monkeypatch):
    """Hundredths left after selling a position back are a REMAINDER, not a
    bet — the venue never redeems them. The money still counts; the market
    does not score as a win."""
    _fills([
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 10.0, "price": 50.0},
        {"cid": ETH_UP, "side": "yes", "action": "sell", "size": 9.92, "price": 60.0},
    ])
    r = _record(monkeypatch, wallet=[], markets={ETH_UP: {"outcome": "yes"}})

    row = r["rows"][0]
    assert row["outcome"] == "closedOut"
    assert row["contracts"] == pytest.approx(0.08)
    assert r["totals"]["won"] == 0


# ────────────────────────────────────────────────────────────────────────
# It has to agree with the figure at the top of the page
# ────────────────────────────────────────────────────────────────────────


def _portfolio(monkeypatch, *, wallet=(), markets=None):
    markets = dict(markets or {})

    async def _positions(*_a, **_kw):
        return [
            {"ticker": w["cid"], "title": "T",
             "position_fp": w["size"] if w.get("side", "yes") == "yes" else -w["size"],
             "cur_price": w.get("price", 0.0), "redeemable": False, "_raw": None}
            for w in wallet
        ]

    async def _summaries(ids):
        return [{"conditionId": c, "title": "T", "slug": None,
                 "status": "settled", "resolvedOutcome": markets[c].get("outcome"),
                 "outcomes": markets[c].get("labels", ["Up", "Down"]),
                 "closeTime": markets[c].get("close"), "probability": 50.0}
                for c in ids if c in markets]

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


def test_the_totals_are_the_same_counts_the_header_reports(fresh_db, monkeypatch):
    """`portfolio()` leads the page with won/lost and the net figure; the
    settled table is the same arithmetic itemised. If the two ever disagree
    the user is being shown two different accounts of one wallet."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0, "fee": 0.05},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 45.0, "fee": 0.05},
        {"cid": GIANTS, "side": "no", "action": "buy", "size": 5.0, "price": 50.0, "fee": 0.05},
        {"cid": DOGE_DOWN, "side": "no", "action": "buy", "size": 5.0, "price": 40.0, "fee": 0.05},
        # And one closed out by selling, so the two aggregates have to agree
        # about a market no outcome decided.
        {"cid": GIANTS, "side": "yes", "action": "buy", "size": 4.0, "price": 20.0, "fee": 0.02},
        {"cid": GIANTS, "side": "yes", "action": "sell", "size": 4.0, "price": 30.0, "fee": 0.02},
    ])
    markets = {BTC_UP: {"outcome": "yes"}, ETH_UP: {"outcome": "yes"},
               GIANTS: {"outcome": "no"}, DOGE_DOWN: {"outcome": "yes"}}
    wallet = [{"cid": DOGE_DOWN, "side": "no", "size": 5.0}]

    rec = _record(monkeypatch, wallet=wallet, markets=markets)
    pf = _portfolio(monkeypatch, wallet=wallet, markets=markets)

    assert rec["totals"]["won"] == pf["wonMarkets"]
    assert rec["totals"]["lost"] == pf["lostMarkets"]
    assert rec["totals"]["markets"] == pf["ledgerMarkets"]
    assert rec["totals"]["bothSides"] == pf["bothSidesMarkets"]
    assert rec["totals"]["closedOut"] == pf["closedOutMarkets"]
    # The net figure is the same money, itemised.
    assert rec["totals"]["pnlUsd"] == pytest.approx(pf["realizedPnlNetUsd"])
    assert rec["totals"]["feesUsd"] == pytest.approx(pf["realizedFeesUsd"])


# ────────────────────────────────────────────────────────────────────────
# Shape, order and the things we refuse to invent
# ────────────────────────────────────────────────────────────────────────


def test_the_record_is_newest_first(fresh_db, monkeypatch):
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 1.0,
         "price": 40.0, "at": "2026-01-01T00:00:00Z"},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 1.0,
         "price": 40.0, "at": "2026-03-01T00:00:00Z"},
        {"cid": GIANTS, "side": "yes", "action": "buy", "size": 1.0,
         "price": 40.0, "at": "2026-02-01T00:00:00Z"},
    ])
    r = _record(monkeypatch, wallet=[], markets={
        BTC_UP: {"outcome": "yes", "close": "2026-01-02T00:00:00Z"},
        ETH_UP: {"outcome": "yes", "close": "2026-03-02T00:00:00Z"},
        GIANTS: {"outcome": "yes", "close": "2026-02-02T00:00:00Z"},
    })
    assert [x["conditionId"] for x in r["rows"]] == [ETH_UP, GIANTS, BTC_UP]


def test_an_unreadable_wallet_leaves_in_wallet_unknown_not_false(fresh_db, monkeypatch):
    """"We could not tell" and "it is not there" are different answers, and
    the difference is whether a winner was redeemed or the API blinked."""
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _record(monkeypatch, wallet_error=True,
                markets={BTC_UP: {"outcome": "yes"}})

    assert r["walletReadable"] is False
    assert r["rows"][0]["inWallet"] is None
    # The outcome is still known — a wallet read has nothing to do with it.
    assert r["rows"][0]["outcome"] == "won"


def test_an_unknown_fee_leaves_the_pnl_unknown_not_gross(fresh_db, monkeypatch):
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0,
             "price": 40.0, "fee": None}])
    r = _record(monkeypatch, wallet=[], markets={BTC_UP: {"outcome": "yes"}})

    row = r["rows"][0]
    assert row["feesKnown"] is False
    assert row["feesUsd"] is None
    assert row["pnlUsd"] is None, "a net figure was printed over unknown fees"
    # The gross facts are still there — only the net is withheld.
    assert row["payoutUsd"] == pytest.approx(5.0)
    assert r["totals"]["feesKnown"] is False
    assert r["totals"]["feesUsd"] is None


def test_an_empty_ledger_says_so_rather_than_showing_a_clean_slate(fresh_db, monkeypatch):
    _fills([])
    r = _record(monkeypatch, wallet=[], markets={})

    assert r["rows"] == []
    assert r["totals"]["markets"] == 0
    assert r["totals"]["pnlUsd"] is None, "an empty ledger reported $0.00 P&L"
    assert "Reconcile fills" in r["note"]


def test_a_resolved_market_is_asked_about_once_and_then_cached(fresh_db, monkeypatch):
    """The ledger reaches back over hundreds of markets, and a resolved one
    never un-resolves. Opening the table twice must not re-ask Gamma."""
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    calls: list[list[str]] = []
    markets = {BTC_UP: {"outcome": "yes"}}

    first = _record(monkeypatch, wallet=[], markets=markets, calls=calls)
    assert first["rows"][0]["outcome"] == "won"
    assert calls, "the first read never asked"
    calls.clear()

    second = _record(monkeypatch, wallet=[], markets=markets, calls=calls)
    assert second["rows"][0]["outcome"] == "won"
    assert calls == [], "a resolved outcome was looked up twice"
