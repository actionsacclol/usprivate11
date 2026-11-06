"""Paper trading, pinned.

A simulator is only worth having if it is wrong in the same places the real
thing is. Every test here guards a specific way paper trading usually lies:
filling at your limit, filling more than existed, filling for free, filling a
resting order that was never actually hit, or letting a fake number touch a
real total.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal_paper as tp
import terminal_trade as tt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def paper_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "paper.db")
    db.init_db()
    return tmp_path


def _mock_market(monkeypatch, *, asks=None, bids=None, resolved=None,
                 category="crypto", available=True, fee=0, min_qty=1):
    """Wire a market with a specific book. Sizes are contracts, prices cents."""
    async def _market(cid, include_raw=False):
        m = {
            "conditionId": cid, "title": "Test market", "slug": "t",
            "category": category, "status": "settled" if resolved else "open",
            "resolvedOutcome": resolved, "closeTime": "2030-01-01T00:00:00Z",
            "probability": 50.0, "spread": 2.0, "liquidityUsd": 100000.0,
            "hoursToClose": 100.0,
            "bid": bids[0][0] if bids else None, "ask": asks[0][0] if asks else None,
            "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": min_qty,
                     "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                     "feeCoefficient": fee},
        }
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        return {
            "conditionId": cid, "tokenSide": side,
            "asks": [{"priceCents": p, "size": s, "cumulative": s,
                      "notionalUsd": s * p / 100} for p, s in (asks or [])],
            "bids": [{"priceCents": p, "size": s, "cumulative": s,
                      "notionalUsd": s * p / 100} for p, s in (bids or [])],
            "bestAsk": (asks or [[None]])[0][0] if asks else None,
            "bestBid": (bids or [[None]])[0][0] if bids else None,
            "spreadCents": 2.0, "midCents": 50.0,
            "bidDepthUsd": 1.0, "askDepthUsd": 1.0,
            "available": available, "fetchedAt": "", "source": "gateway",
            "note": None if available else "could not read",
        }

    async def _summaries(ids):
        return [await _market(i) for i in ids]

    monkeypatch.setattr(tp.term, "market", _market)
    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tp.term, "book", _book)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tp.term, "marked_summaries", _summaries)


def _order(**kw):
    base = {"conditionId": "0xabc", "side": "yes", "action": "buy",
            "priceCents": 55, "size": 10, "orderType": "FAK"}
    base.update(kw)
    return base


# ────────────────────────────────────────────────────────────────────────
# The core lie: filling at your limit, for size that was never there.
# ────────────────────────────────────────────────────────────────────────


def test_fill_uses_the_walked_average_not_your_limit(paper_db, monkeypatch):
    # 4 at 50c and 6 at 52c. A limit of 55c sweeps both: average 51.2c, NOT 55.
    _mock_market(monkeypatch, asks=[(50.0, 4), (52.0, 6)])
    r = _run(tp.place(_order(priceCents=55, size=10)))
    assert r["ok"]
    assert r["filledSize"] == 10.0
    assert abs(r["avgPriceCents"] - 51.2) < 1e-6, "paper filled at the limit price"


def test_fill_never_exceeds_what_was_resting(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 3)])
    r = _run(tp.place(_order(priceCents=55, size=10, orderType="FAK")))
    assert r["filledSize"] == 3.0, "paper invented size that was not on the book"
    assert r["status"] == "partial"


def test_nothing_at_your_price_means_no_fill(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    r = _run(tp.place(_order(priceCents=55, size=10, orderType="FAK")))
    assert r["filledSize"] == 0.0
    assert r["status"] == "unfilled"


def test_all_or_none_is_killed_when_the_book_is_short(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 3)])
    r = _run(tp.place(_order(priceCents=55, size=10, orderType="FOK")))
    assert r["filledSize"] == 0.0, "FOK partially filled"
    assert r["status"] == "unfilled"


def test_an_unreadable_book_refuses_to_simulate(paper_db, monkeypatch):
    """A paper fill invented without a book is worse than no fill: it is a
    number the user will trust that came from nowhere at all."""
    _mock_market(monkeypatch, asks=[(50.0, 100)], available=False)
    r = _run(tp.place(_order()))
    assert r["ok"] is False
    assert "book" in r["message"].lower()


# ────────────────────────────────────────────────────────────────────────
# Resting orders — the queue-position problem.
# ────────────────────────────────────────────────────────────────────────


def test_a_non_crossing_limit_rests_instead_of_filling(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    r = _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    assert r["filledSize"] == 0.0
    assert r["status"] == "resting"


def test_a_resting_order_does_not_fill_merely_at_the_touch(paper_db, monkeypatch):
    """We cannot know queue position, so touching the price is not a fill.
    Erring the other way manufactures fills that never happened."""
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    # Market comes exactly TO 50c — not through it.
    _mock_market(monkeypatch, asks=[(50.0, 100)])
    assert _run(tp._fill_resting()) == 0
    orders = tp.orders()["orders"]
    assert orders[0]["filledSize"] == 0.0
    assert orders[0]["status"] == "resting"


def test_a_resting_order_fills_once_the_market_trades_through(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    _mock_market(monkeypatch, asks=[(49.0, 100)])   # strictly through
    assert _run(tp._fill_resting()) == 1
    o = tp.orders()["orders"][0]
    assert o["filledSize"] == 10.0
    assert o["status"] == "filled"
    assert o["feesUsd"] == 0.0 or o["feesUsd"] is None, \
        "a resting fill is a MAKER fill and must not be charged a taker fee"


def test_a_resting_buy_fills_at_its_limit_not_the_later_better_ask(
        paper_db, monkeypatch):
    """A real resting bid at 50c is hit by a seller at 50c. The ask having
    since fallen to 30c does not refund the difference: that gap was never
    ours, and handing it over flattered every GTC strategy."""
    _mock_market(monkeypatch, asks=[(60.0, 1000)])
    _run(tp.place(_order(priceCents=50, size=100, orderType="GTC")))
    _mock_market(monkeypatch, asks=[(30.0, 1000)])
    assert _run(tp._fill_resting()) == 1
    o = tp.orders()["orders"][0]
    assert o["filledSize"] == 100.0
    assert o["avgFillPriceCents"] == 50.0, "the maker fill took the later, better price"
    assert o["costUsd"] == 50.0


def test_a_resting_sell_fills_at_its_limit_not_the_later_better_bid(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, bids=[(50.0, 100)])
    r = _run(tp.place(_order(action="sell", priceCents=60, size=10, orderType="GTC")))
    assert r["status"] == "resting"
    _mock_market(monkeypatch, bids=[(80.0, 100)])
    assert _run(tp._fill_resting()) == 1
    o = next(x for x in tp.orders()["orders"] if x["localId"] == r["localId"])
    assert o["avgFillPriceCents"] == 60.0, "the maker sell took the later, better bid"
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    # Bought 10 @ 40c, sold 10 @ 60c: +$2.00, not the +$4.00 an 80c fill says.
    assert abs(pf["realizedPnlUsd"] - 2.0) < 1e-6


def test_a_pure_maker_fill_has_a_zero_fee_not_an_unknown_one(paper_db, monkeypatch):
    """An order that rested whole was written with no fee, and NULL reads as
    "fee rate unknown" in every total — so every GTC fill used to put the
    'fees excluded' warning on the paper portfolio. A maker fee is known."""
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    _mock_market(monkeypatch, asks=[(49.0, 100)])
    assert _run(tp._fill_resting()) == 1
    assert tp.orders()["orders"][0]["feesUsd"] == 0.0
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert pf["unreconciledNote"] is None, pf["unreconciledNote"]


def test_a_maker_fill_keeps_an_unknown_taker_fee_unknown(paper_db, monkeypatch):
    """The crossed half of the order paid a taker fee we could not price. The
    maker half filling later must not turn that unknown into a zero."""
    _mock_market(monkeypatch, asks=[(50.0, 4)], category="no-such-category")
    monkeypatch.setattr(tt.api, "_fee_schedule_from_gamma", lambda raw: None)
    _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    assert tp.orders()["orders"][0]["feesUsd"] is None
    _mock_market(monkeypatch, asks=[(45.0, 100)], category="no-such-category")
    assert _run(tp._fill_resting()) == 1
    o = tp.orders()["orders"][0]
    assert o["status"] == "filled"
    assert o["feesUsd"] is None


def _paper_cfg(monkeypatch, **cfg):
    """The live config the ticket reads its paper bankroll from."""
    import sys
    import types
    mod = types.ModuleType("service")
    mod.STATE = types.SimpleNamespace(cfg=dict(cfg))
    monkeypatch.setitem(sys.modules, "service", mod)


def test_resting_buys_hold_their_cash_so_the_account_cannot_go_negative(
        paper_db, monkeypatch):
    """Three resting buys of $750 each on a $1,000 account were all accepted,
    because each fit the balance on its own. When they filled, cash went to
    -$1,250 and equity and return were computed on leverage nobody had."""
    _paper_cfg(monkeypatch, terminal_paper_bankroll_usd=1000.0)
    _mock_market(monkeypatch, asks=[(60.0, 10000)])
    big = _order(priceCents=50, size=1500, orderType="GTC")      # $750, rests
    first = _run(tp.place(big))
    assert first["ok"] and first["status"] == "resting"

    pv = _run(tt.preview({**big, "paper": True}))
    assert pv["availableUsd"] == 250.0
    second = _run(tp.place(big))
    assert second["ok"] is False
    assert "resting buys" in second["message"], second["message"]

    # Cancelling the resting buy gives its cash back.
    _run(tp.cancel(first["localId"]))
    assert _run(tt.preview({**big, "paper": True}))["availableUsd"] == 1000.0


def test_a_crossing_limit_fills_the_crossed_part_and_rests_the_remainder(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 4)])
    r = _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    assert r["filledSize"] == 4.0
    o = tp.orders()["orders"][0]
    assert o["restingSize"] == 6.0
    assert o["status"] == "partial"


def test_a_canceled_resting_order_stops_filling(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    r = _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    _run(tp.cancel(r["localId"]))
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    assert _run(tp._fill_resting()) == 0


def test_a_cancel_that_lands_mid_sweep_is_not_overwritten_by_a_fill(
        paper_db, monkeypatch):
    """The sweep reads its rows, then awaits each book. A cancel that landed
    in that gap was told "Paper order canceled." and the order filled anyway."""
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    r = _run(tp.place(_order(priceCents=50, size=10, orderType="GTC")))
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    crossed = tp.term.book
    said = {}

    async def _book_then_cancel(cid, side="yes"):
        said.update(await tp.cancel(r["localId"]))
        return await crossed(cid, side)
    monkeypatch.setattr(tp.term, "book", _book_then_cancel)
    assert _run(tp._fill_resting()) == 0
    assert said["ok"] is True
    o = tp.orders()["orders"][0]
    assert (o["status"], o["filledSize"], o["restingSize"]) == ("canceled", 0.0, 0.0)
    assert _run(tp.portfolio({}))["positions"] == []


def test_a_resting_buy_is_canceled_when_its_market_resolves(paper_db, monkeypatch):
    """A resolved market has no book, so the order can never fill. Left
    resting, it held its cash in the account for good."""
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    r = _run(tp.place(_order(priceCents=50, size=200, orderType="GTC")))
    assert r["status"] == "resting" and tp.reserved_buy_usd() == 100.0
    _mock_market(monkeypatch, asks=[(60.0, 100)], resolved="no")
    assert _run(tp._settle_resolved()) == 0, "no position, so nothing to settle"
    o = tp.orders()["orders"][0]
    assert (o["status"], o["restingSize"]) == ("canceled", 0.0)
    assert "resolved" in o["note"]
    assert tp.reserved_buy_usd() == 0.0


def test_a_resting_sell_is_canceled_when_its_position_settles(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, bids=[(50.0, 100)])
    r = _run(tp.place(_order(action="sell", priceCents=60, size=10, orderType="GTC")))
    assert r["status"] == "resting"
    _mock_market(monkeypatch, bids=[(50.0, 100)], resolved="yes")
    assert _run(tp._settle_resolved()) == 1
    o = next(x for x in tp.orders()["orders"] if x["localId"] == r["localId"])
    assert (o["status"], o["restingSize"]) == ("canceled", 0.0)
    assert tp.held("0xabc", "yes") == 0.0
    # The settlement paid the 10 contracts at 100c; the dead sell changed nothing.
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert abs(pf["cashUsd"] - 1006.0) < 1e-6


# ────────────────────────────────────────────────────────────────────────
# Fees, shorts, and settlement.
# ────────────────────────────────────────────────────────────────────────


def test_a_taker_fill_is_charged_the_real_fee_curve(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 100)], fee=0.0695)
    _run(tp.place(_order(priceCents=50, size=100, orderType="FAK")))
    o = tp.orders()["orders"][0]
    # 0.0695 x 100 x .5 x .5 = 1.7375, to the cent the way the exchange rounds.
    assert o["feesUsd"] == 1.74


def test_a_market_that_states_no_fee_rate_has_an_unknown_fee(paper_db, monkeypatch):
    """There is no category table to guess from, and it must not read as free."""
    _mock_market(monkeypatch, asks=[(50.0, 100)])
    monkeypatch.setattr(tt.api, "_fee_schedule_from_gamma", lambda raw: None)
    r = _run(tp.place(_order(priceCents=50, size=10, orderType="FAK")))
    assert r["ok"] and tp.orders()["orders"][0]["feesUsd"] is None


def test_a_markets_own_fee_free_schedule_is_respected(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 100)])
    _run(tp.place(_order(priceCents=50, size=100, orderType="FAK")))
    assert tp.orders()["orders"][0]["feesUsd"] == 0.0


def test_you_cannot_sell_paper_you_do_not_hold(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(50.0, 100)])
    r = _run(tp.place(_order(action="sell", priceCents=40, size=10)))
    assert r["ok"] is False
    assert "naked short" in r["message"].lower()


def test_settlement_pays_exactly_100c_on_a_winner(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, asks=[(40.0, 100)], resolved="yes")
    assert _run(tp._settle_resolved()) == 1
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert pf["positions"] == []
    # Bought 10 at 40c ($4), settled at $1 each ($10) → +$6 realised.
    assert abs(pf["realizedPnlUsd"] - 6.0) < 1e-6
    assert abs(pf["cashUsd"] - 1006.0) < 1e-6


def test_settlement_pays_exactly_zero_on_a_loser(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, asks=[(40.0, 100)], resolved="no")
    _run(tp._settle_resolved())
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert abs(pf["realizedPnlUsd"] + 4.0) < 1e-6
    assert abs(pf["cashUsd"] - 996.0) < 1e-6


def test_settling_twice_does_not_double_pay(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, asks=[(40.0, 100)], resolved="yes")
    _run(tp._settle_resolved())
    assert _run(tp._settle_resolved()) == 0
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert abs(pf["cashUsd"] - 1006.0) < 1e-6


# ────────────────────────────────────────────────────────────────────────
# Isolation — a paper number must never reach a real one.
# ────────────────────────────────────────────────────────────────────────


def test_paper_fills_never_enter_the_real_ledger(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 100)])
    _run(tp.place(_order(priceCents=55, size=10, orderType="FAK")))
    assert tt._ledger_basis() == {}, "a paper fill reached the real cost basis"
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM terminal_fills").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM terminal_orders").fetchone()[0] == 0


def test_paper_cash_starts_at_the_configured_bankroll(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 100)])
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 250.0}))
    assert pf["cashUsd"] == 250.0
    assert pf["bankrollUsd"] == 250.0


def test_average_cost_across_two_paper_buys(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=100, orderType="FAK")))
    _mock_market(monkeypatch, asks=[(60.0, 100)])
    _run(tp.place(_order(priceCents=60, size=100, orderType="FAK")))
    pf = _run(tp.portfolio(None))
    p = pf["positions"][0]
    assert p["size"] == 200
    assert abs(p["avgCostCents"] - 50.0) < 1e-6


def test_reset_clears_the_paper_account(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 100)])
    _run(tp.place(_order(priceCents=55, size=10, orderType="FAK")))
    assert tp.reset()["cleared"] == 1
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    assert pf["positions"] == [] and pf["cashUsd"] == 1000.0


def test_a_large_order_warns_about_missing_market_impact(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 10)])
    r = _run(tp.place(_order(priceCents=55, size=10, orderType="FAK")))
    assert any("depth" in w for w in r.get("warnings", [])), \
        "sweeping the whole book should say the real fill would be worse"
