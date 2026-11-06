"""Paper P&L with REAL fees, and markets that resolve 50-50.

Audit H9 (2026-10-12): a paper buy charged its taker fee to cash but not to
cost basis, so realised + unrealised overstated P&L by every entry fee, while
day_pnl (the loss stop) did subtract it. Every older paper test ran with no fee,
so nothing caught it. These run on the fee curve (coefficient 0.07 — 1.75c a share
at 50c) and pin the one identity the record has to satisfy for every book, at
every step:

    realizedPnlUsd + totalUnrealizedPnlUsd == cash + position value - bankroll

Also pinned: a market the venue settles 50-50 (outcomePrices ["0.5","0.5"] with
MARKET_STATUS_RESOLVED) settles BOTH sides at 50c on the paper books, and the
forecast ledger scores it as outcome 0.5.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import db
import forecast_ledger
import terminal as t
import terminal_paper as tp
import terminal_trade as tt

FEE_RATE = 0.07


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def paper_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "paper_fees.db")
    db.init_db()
    return tmp_path


class _Market:
    """One mutable fake market: its book, its quote and how it resolved."""

    def __init__(self):
        self.asks = [(50.0, 1000.0)]
        self.bids = [(49.0, 1000.0)]
        self.prob = 50.0
        self.bid = 50.0      # YES bid — what day_pnl marks a YES holding at
        self.ask = 52.0      # YES ask — a NO holding is marked at 100 - ask
        self.resolved = None   # "yes" | "no" | None
        self.split = False     # settled 50-50
        self.final = True      # only read when split: False is still RESOLVING

    def summary(self, cid):
        final = bool(self.resolved or (self.split and self.final))
        if self.resolved == "yes":
            prices = ["1", "0"]
        elif self.resolved == "no":
            prices = ["0", "1"]
        elif self.split:
            prices = ["0.5", "0.5"]
        else:
            prices = [str(self.prob / 100), str(1 - self.prob / 100)]
        return {
            "conditionId": cid, "title": "Fee market", "slug": "f",
            "category": "crypto",
            "status": "settled" if final else "open",
            "resolvedOutcome": self.resolved, "closeTime": "2030-01-01T00:00:00Z",
            "probability": self.prob, "spread": 2.0, "liquidityUsd": 100000.0,
            "hoursToClose": 100.0, "bid": self.bid, "ask": self.ask,
            "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 1,
                     "feeCoefficient": FEE_RATE,
                     "active": not final, "closed": final,
                     "status": ("MARKET_STATUS_RESOLVED" if final
                                else "MARKET_STATUS_RESOLVING" if self.split
                                else "MARKET_STATUS_OPEN"),
                     "outcomePrices": json.dumps(prices)},
        }


@pytest.fixture
def mkt(monkeypatch):
    m = _Market()

    async def _market(cid, include_raw=False):
        s = m.summary(cid)
        return s if include_raw else {k: v for k, v in s.items() if k != "_raw"}

    async def _book(cid, side="yes"):
        lv = lambda rows: [{"priceCents": p, "size": s, "cumulative": s,  # noqa: E731
                            "notionalUsd": s * p / 100} for p, s in rows]
        return {
            "conditionId": cid, "tokenSide": side,
            "asks": lv(m.asks), "bids": lv(m.bids),
            "bestAsk": m.asks[0][0] if m.asks else None,
            "bestBid": m.bids[0][0] if m.bids else None,
            "spreadCents": 2.0, "midCents": 50.0,
            "bidDepthUsd": 1.0, "askDepthUsd": 1.0,
            "available": True, "fetchedAt": "", "source": "clob", "note": None,
        }

    async def _summaries(ids):
        # Like the real one: the venue's row rides along as `_raw`.
        return [m.summary(i) for i in ids]

    async def _marked(ids):
        return [m.summary(i) for i in ids]

    monkeypatch.setattr(tp.term, "market", _market)
    monkeypatch.setattr(tt.term, "market", _market)
    monkeypatch.setattr(tp.term, "book", _book)
    monkeypatch.setattr(tt.term, "book", _book)
    monkeypatch.setattr(tp.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tp.term, "marked_summaries", _marked)
    return m


def _order(**kw):
    base = {"conditionId": "0xfee", "side": "yes", "action": "buy",
            "priceCents": 55, "size": 100, "orderType": "FAK"}
    base.update(kw)
    return base


BANK = 1000.0


def _pf():
    return _run(tp.portfolio({"terminal_paper_bankroll_usd": BANK,
                              "mcp_paper_bankroll_usd": BANK}))


def _assert_identity(pf, *, from_rows=True):
    """The record is exactly what the cash did, net of every fee."""
    pnl = pf["realizedPnlUsd"] + pf["totalUnrealizedPnlUsd"]
    assert pf["equityUsd"] == pytest.approx(pf["cashUsd"] + pf["totalValueUsd"], abs=0.011)
    assert pnl == pytest.approx(pf["equityUsd"] - pf["bankrollUsd"], abs=0.011)
    if not from_rows:
        return
    # And independently of portfolio()'s own arithmetic: from the order rows.
    cash_delta = 0.0
    for o in tp.orders(500)["orders"]:
        if o["filledSize"] <= 0:
            continue
        notional = o["filledSize"] * (o["avgFillPriceCents"] or 0) / 100.0
        fee = o["feesUsd"] or 0.0
        cash_delta += (-(notional + fee)) if o["action"] == "buy" else (notional - fee)
    assert pf["cashUsd"] == pytest.approx(BANK + cash_delta, abs=0.011)
    assert pnl == pytest.approx(cash_delta + pf["totalValueUsd"], abs=0.011)


def _fees_paid() -> float:
    return sum(o["feesUsd"] or 0.0 for o in tp.orders(500)["orders"])


# ────────────────────────────────────────────────────────────────────────
# H9: buy → partial sell → settle, fees on
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("book", ["manual", "ai", "ai:sam"])
def test_paper_pnl_is_net_of_every_fee_through_buy_partial_sell_settle(paper_db, mkt, book):
    with tp.using_book(book):
        # 1. Buy 100 YES at 50c: the taker fee is 0.07 * 0.25 = 1.75c a share.
        r = _run(tp.place(_order()))
        assert r["ok"] and r["filledSize"] == 100
        buy_fee = tp.orders()["orders"][0]["feesUsd"]
        assert buy_fee == pytest.approx(1.75)

        pf = _pf()
        _assert_identity(pf)
        pos = pf["positions"][0]
        # Avg cost is the break-even: price plus the fee per share.
        assert pos["avgCostCents"] == pytest.approx(51.75)
        assert pos["costBasisUsd"] == pytest.approx(51.75)
        # Marked at the price paid, the position is DOWN exactly its fee — the
        # old code showed 0.00 here while the cash was 1.75 lighter.
        assert pf["totalUnrealizedPnlUsd"] == pytest.approx(-1.75)
        assert pf["realizedPnlUsd"] == 0.0
        assert pf["equityUsd"] - BANK == pytest.approx(-1.75)

        day = _run(tp.day_pnl(_today()))
        # day_pnl marks at the bid (50 here, = the mid), so it agrees exactly.
        assert day["realizedUsd"] + day["unrealizedUsd"] == pytest.approx(
            pf["realizedPnlUsd"] + pf["totalUnrealizedPnlUsd"], abs=0.011)
        assert day["realizedUsd"] == pytest.approx(0.0)
        assert day["openLossUsd"] == pytest.approx(1.75)

        # 2. Market moves to 60; sell 40 into a 60c bid (a taker sell: fee again).
        mkt.prob, mkt.bid, mkt.ask = 60.0, 60.0, 62.0
        mkt.bids = [(60.0, 1000.0)]
        r = _run(tp.place(_order(action="sell", size=40, priceCents=60)))
        assert r["ok"] and r["filledSize"] == 40
        sell_fee = next(o for o in tp.orders()["orders"] if o["action"] == "sell")["feesUsd"]
        # 0.07 * .6 * .4 * 40 = 0.672, to the cent as the exchange charges it.
        assert sell_fee == 0.67

        pf = _pf()
        _assert_identity(pf)
        # 40 closed at 60 against a 51.75 fee-inclusive basis, less the sell fee.
        assert pf["realizedPnlUsd"] == pytest.approx(40 * (0.60 - 0.5175) - sell_fee, abs=0.006)
        pos = pf["positions"][0]
        assert pos["size"] == 60
        assert pos["avgCostCents"] == pytest.approx(51.75)   # avg cost unchanged by a sell
        assert pf["totalUnrealizedPnlUsd"] == pytest.approx(60 * (0.60 - 0.5175), abs=0.006)

        day = _run(tp.day_pnl(_today()))
        assert day["realizedUsd"] == pytest.approx(pf["realizedPnlUsd"], abs=0.006)
        assert day["realizedUsd"] + day["unrealizedUsd"] == pytest.approx(
            pf["realizedPnlUsd"] + pf["totalUnrealizedPnlUsd"], abs=0.011)

        # 3. It resolves YES: the 60 left settle at 100c, no fee.
        mkt.resolved = "yes"
        assert _run(tp._settle_resolved()) == 1
        pf = _pf()
        assert pf["positions"] == []
        # Nothing open: open P&L is exactly zero, equity is the cash.
        assert pf["totalUnrealizedPnlUsd"] == 0.0
        assert pf["totalValueUsd"] == 0.0
        assert pf["equityUsd"] == pf["cashUsd"]
        _assert_identity(pf)
        expected = (-100 * 0.50 - buy_fee) + (40 * 0.60 - sell_fee) + 60 * 1.00
        assert pf["realizedPnlUsd"] == pytest.approx(expected, abs=0.011)
        assert pf["cashUsd"] - BANK == pytest.approx(expected, abs=0.011)
        # Gross of fees it would have been this much better; the record says so.
        assert expected == pytest.approx((-50 + 24 + 60) - _fees_paid())

        day = _run(tp.day_pnl(_today()))
        assert day["realizedUsd"] == pytest.approx(pf["realizedPnlUsd"], abs=0.011)
        assert day["unrealizedUsd"] == 0.0 and day["openLossUsd"] == 0.0


def test_a_flat_round_trip_loses_exactly_both_fees(paper_db, mkt):
    """Buy and sell at the same price: the record must say you lost the fees
    — the pre-fix record said you lost only the SELL fee."""
    _run(tp.place(_order(size=50)))
    mkt.bids = [(50.0, 1000.0)]
    _run(tp.place(_order(action="sell", size=50, priceCents=50)))
    pf = _pf()
    _assert_identity(pf)
    assert pf["realizedPnlUsd"] == pytest.approx(-_fees_paid(), abs=0.006)
    # 0.875 on each leg, a tie that banker's rounding takes to the even 0.88.
    assert _fees_paid() == pytest.approx(2 * 0.88)


def test_a_sell_fee_with_nothing_left_to_close_still_reaches_the_record(paper_db, mkt):
    """A sell row that finds no holding (two sells raced one position) still
    paid its fee in cash, so it is in realised too — or the identity breaks."""
    _run(tp.place(_order(size=10)))
    mkt.bids = [(50.0, 1000.0)]
    _run(tp.place(_order(action="sell", size=10, priceCents=50)))
    # A second sell row that outran the holding, written the way a raced GTC
    # fill would leave it.
    tp._record({"condition_id": "0xfee", "side": "yes", "action": "sell",
                "order_type": "GTC", "requested_size": 5, "requested_price_cents": 50,
                "filled_size": 5, "avg_fill_price_cents": 50, "fees_usd": 0.1,
                "cost_usd": 2.5, "status": "filled", "filled_at": tp._now_iso()})
    pf = _pf()
    # (Not recomputed from the rows: cash credits only the contracts that were
    # actually held, so this row's 5 contracts never reach the cash.)
    _assert_identity(pf, from_rows=False)
    assert pf["realizedPnlUsd"] == pytest.approx(-_fees_paid(), abs=0.006)


def test_an_unpriceable_open_position_makes_the_totals_unknown_not_flattering(paper_db, mkt):
    """One open position with no mark: the open P&L is UNKNOWN. It used to be
    silently left out of the sums, which reads as 'worth its cost'."""
    _run(tp.place(_order(size=10)))
    mkt.prob = None
    pf = _pf()
    assert pf["positions"][0]["unrealizedPnlUsd"] is None
    assert pf["totalUnrealizedPnlUsd"] is None
    assert pf["equityUsd"] is None and pf["returnPct"] is None
    # Realised and cash are still known.
    assert pf["realizedPnlUsd"] == 0.0
    assert pf["cashUsd"] == pytest.approx(BANK - 5.0 - 10 * FEE_RATE * 0.25, abs=0.006)


def test_the_loss_stop_counts_an_entry_fee_once(paper_db, mkt):
    """day_pnl puts the buy fee in the position's cost — not ALSO in today's
    realised, which would double it now that cost carries it."""
    with tp.using_book("ai"):
        _run(tp.place(_order(size=100)))
        day = _run(tp.day_pnl(_today()))
    assert day["realizedUsd"] == 0.0
    assert day["unrealizedUsd"] == pytest.approx(-1.75)
    assert day["openLossUsd"] == pytest.approx(1.75)


# ────────────────────────────────────────────────────────────────────────
# 50-50 resolutions
# ────────────────────────────────────────────────────────────────────────


def _split_summary(**raw_over):
    raw = {"closed": True, "status": "MARKET_STATUS_RESOLVED",
           "outcomePrices": '["0.5", "0.5"]'}
    raw.update(raw_over)
    return {"conditionId": "0xs", "resolvedOutcome": None, "status": "settled", "_raw": raw}


def test_split_detection_needs_a_final_status_and_a_price_between_0_and_1():
    assert tp.split_resolution(_split_summary())
    # A list is fine too, and "0.50" is 0.5.
    assert tp.split_resolution(_split_summary(outcomePrices=["0.50", "0.5"]))
    # Past expiry with its settlement not yet published: prices are not an outcome.
    assert not tp.split_resolution(_split_summary(status="MARKET_STATUS_RESOLVING"))
    assert not tp.split_resolution(_split_summary(status="MARKET_STATUS_OPEN"))
    assert not tp.split_resolution(_split_summary(status=None))
    # A 1/0 result is a decided market, whichever status word it carries.
    assert not tp.split_resolution(_split_summary(outcomePrices='["1", "0"]'))
    # One price cannot say what both sides paid.
    assert not tp.split_resolution(_split_summary(outcomePrices='["0.5"]'))
    assert not tp.split_resolution({"conditionId": "0xs", "resolvedOutcome": None})
    assert not tp.split_resolution(None)
    # A decided market is never a split, whatever the raw row says.
    assert not tp.split_resolution({**_split_summary(), "resolvedOutcome": "yes"})
    assert tp.payout_cents(_split_summary(), "yes") == 50.0
    assert tp.payout_cents(_split_summary(), "no") == 50.0
    assert tp.payout_cents({"resolvedOutcome": "no"}, "no") == 100.0
    assert tp.payout_cents({"resolvedOutcome": "no"}, "yes") == 0.0
    assert tp.payout_cents({"resolvedOutcome": None}, "yes") is None


def test_a_split_other_than_even_pays_each_side_its_own_share():
    m = _split_summary(outcomePrices='["0.3", "0.7"]')
    assert tp.split_resolution(m)
    assert tp.payout_cents(m, "yes") == 30.0
    assert tp.payout_cents(m, "no") == 70.0
    assert "30" in tp._resolution_word(m)


@pytest.mark.parametrize("book", ["manual", "ai:sam"])
def test_a_50_50_resolution_settles_both_sides_at_50c(paper_db, mkt, book):
    with tp.using_book(book):
        _run(tp.place(_order(size=20)))                       # YES at 50
        _run(tp.place(_order(side="no", size=10)))            # NO at 50 (same fake book)
        # And a resting buy that the resolution must cancel.
        mkt.asks = [(60.0, 1000.0)]
        _run(tp.place(_order(size=5, priceCents=40, orderType="GTC")))

        mkt.split = True
        pf = _pf()
        # Before the settle pass: marked at the payout, both sides.
        assert {p["side"]: p["markCents"] for p in pf["positions"]} == {"yes": 50.0, "no": 50.0}
        assert all(p["settled"] for p in pf["positions"])
        _assert_identity(pf)
        day = _run(tp.day_pnl(_today()))
        assert day["unmarked"] == []

        assert _run(tp.tick())["settled"] == 2
        pf = _pf()
        assert pf["positions"] == []
        _assert_identity(pf)
        settles = [o for o in tp.orders()["orders"] if o["orderType"] == "SETTLE"]
        assert sorted((o["side"], o["avgFillPriceCents"], o["filledSize"]) for o in settles) == [
            ("no", 50.0, 10.0), ("yes", 50.0, 20.0)]
        assert all("50-50" in (o["note"] or "") for o in settles)
        resting = [o for o in tp.orders()["orders"] if o["orderType"] == "GTC"]
        assert resting[0]["status"] == "canceled" and "50-50" in resting[0]["note"]
        # 30 contracts bought at 50c + fees, paid back at 50c: down exactly the fees.
        assert pf["realizedPnlUsd"] == pytest.approx(-_fees_paid(), abs=0.006)
        assert pf["cashUsd"] == pytest.approx(BANK - _fees_paid(), abs=0.006)
        # Settled once only.
        assert _run(tp.tick())["settled"] == 0


def test_a_closed_market_awaiting_resolution_is_not_settled_at_50c(paper_db, mkt):
    _run(tp.place(_order(size=10)))
    mkt.split, mkt.final = True, False
    assert _run(tp.tick())["settled"] == 0
    assert _pf()["positions"][0]["size"] == 10


# ── the forecast ledger ─────────────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "forecasts.db")
    db.init_db()
    monkeypatch.setattr(forecast_ledger, "_split_checked", {})

    async def _undecided(ids):          # the 100/0 reader cannot decide a split
        return {c: None for c in ids}
    monkeypatch.setattr(tt, "_resolutions_for", _undecided)

    reads: list[list[str]] = []
    state = {"status": "MARKET_STATUS_RESOLVED"}

    async def _summaries(ids):
        reads.append(list(ids))
        return [{"conditionId": c, "resolvedOutcome": None, "status": "settled",
                 "_raw": {"closed": True, "status": state["status"],
                          "outcomePrices": '["0.5", "0.5"]'}} for c in ids]
    monkeypatch.setattr(t, "_summaries_for", _summaries)
    return reads, state


def _outcomes():
    with db.get_db() as conn:
        return {r["condition_id"]: r["outcome"] for r in conn.execute(
            "SELECT condition_id, outcome FROM ai_forecasts")}


def test_a_50_50_market_is_scored_as_outcome_half(ledger):
    reads, _ = ledger
    forecast_ledger.record(condition_id="0xsplit", prob_yes=0.7,
                           market={"mid": 60.0, "bid": 59.0, "ask": 61.0},
                           agent_id="default")
    # First pass: the market was never looked up before, so only the 100/0
    # reader runs — no extra read for a split on a market just past close.
    assert _run(forecast_ledger.resolve_pending()) == 0
    assert reads == [] and _outcomes() == {"0xsplit": None}
    # Next pass: still undecided, so it is checked for a split — and it is one.
    assert _run(forecast_ledger.resolve_pending()) == 1
    assert reads == [["0xsplit"]]
    assert _outcomes() == {"0xsplit": 0.5}

    sb = forecast_ledger.scoreboard()
    assert sb["overall"]["brierAgent"] == pytest.approx((0.7 - 0.5) ** 2)
    assert sb["overall"]["brierMarket"] == pytest.approx((0.6 - 0.5) ** 2)
    row = sb["recent"][0]
    assert row["outcome"] == 0.5
    assert row["brierAgent"] == pytest.approx(0.04)
    assert sb["buckets"][0]["hitRate"] == 0.5


def test_an_unresolved_closed_market_is_not_scored_and_not_reread_every_pass(ledger):
    reads, state = ledger
    state["status"] = "MARKET_STATUS_RESOLVING"
    forecast_ledger.record(condition_id="0xwait", prob_yes=0.4, agent_id="default")
    for _ in range(4):
        assert _run(forecast_ledger.resolve_pending()) == 0
    assert _outcomes() == {"0xwait": None}
    # One split read, then throttled (SPLIT_RECHECK_SEC), not one per pass.
    assert reads == [["0xwait"]]


def test_a_decided_market_never_costs_a_split_read(ledger, monkeypatch):
    reads, _ = ledger

    async def _yes(ids):
        return {c: "yes" for c in ids}
    monkeypatch.setattr(tt, "_resolutions_for", _yes)
    forecast_ledger.record(condition_id="0xdone", prob_yes=0.7, agent_id="default")
    with db.get_db() as conn:   # as if an earlier pass had already looked
        conn.execute("UPDATE ai_forecasts SET last_checked_at='2026-01-01 00:00:00'")
    assert _run(forecast_ledger.resolve_pending()) == 1
    assert _outcomes() == {"0xdone": 1.0}
    assert reads == []
