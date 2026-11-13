"""Real fill times and fees, from the executions on the account's trade rows.

From a live run (2026-11-06): 54 of 77 fills had a NULL fee though the venue charged
none of them, every resting order's time was when a reconcile saw it, and one sale
sorted ahead of its own buy and was dropped, so a market sold at 3c scored as held
to settlement. Against the venue's rows, every size and known fee matched.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import db
import terminal_trade as tt
import us_api as us

CID = "cpc-btc-updown-15m-2026-11-06-0815z"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "terminal.db")
    db.init_db()
    return tmp_path


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _placed(oid, *, side="no", action="buy", order_type="GTC",
            created="2026-11-06 08:26:03"):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO terminal_orders
                 (condition_id, title, side, action, order_type, requested_size,
                  requested_price_cents, order_id, status, origin, network, created_at)
               VALUES (?,'T',?,?,?,10,60,?,'placed','ai','mainnet',?)""",
            (CID, side, action, order_type, oid, created))


def _view(oid, filled=30.0, price=0.03, fee=0.0, status="matched"):
    return {"id": oid, "status": status, "size_matched": filled, "original_size": filled,
            "price": price, "side": "no", "action": "buy", "market": CID,
            "fee_usd": fee, "created_at": "2026-11-06T08:26:03Z"}


def _raw_trade(oid, shares, *, fee="0.0000", when="2026-11-06T08:28:43.056353130Z",
               taker=False, state="TRADE_STATE_NEW"):
    """Shaped like a live answer. The block for the other side is a stranger's order."""
    def execution(order_id, commission):
        return {"order": {"id": order_id}, "lastShares": str(shares),
                "lastPx": {"value": "0.97", "currency": "USD"}, "transactTime": when,
                "commissionNotionalCollected": {"value": commission, "currency": "USD"}}
    mine, theirs = execution(oid, fee), execution("stranger", "0.0100")
    return {"type": "ACTIVITY_TYPE_TRADE", "trade": {
        "id": "T1", "marketSlug": CID, "state": state, "isAggressor": taker,
        "qtyDecimal": str(shares), "price": {"value": "0.97", "currency": "USD"},
        "createTime": "2026-11-06T08:28:44Z",
        "aggressorExecution": mine if taker else theirs,
        "passiveExecution": theirs if taker else mine}}


def _venue(monkeypatch, orders, trades):
    """`orders` is {id: view}; `trades` raw activities, or an exception to raise."""
    async def _get_order(oid):
        return {"order": orders[oid]}

    async def _get_activity(limit=500, *, user=None, market=""):
        if isinstance(trades, BaseException):
            raise trades
        return [r for r in (us.activity_row(a) for a in trades) if r]

    async def _summaries(ids):
        return [{"conditionId": c, "title": "T", "_raw": {"feeCoefficient": 0.0695}}
                for c in ids]
    monkeypatch.setattr(tt.api, "get_order", _get_order)
    monkeypatch.setattr(tt.api, "get_activity", _get_activity)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)
    tt._true_up_tried.clear()


def _fill(oid):
    with db.get_db() as conn:
        return dict(conn.execute(
            "SELECT * FROM terminal_fills WHERE order_id=?", (oid,)).fetchone())


def test_a_resting_fill_gets_its_execution_time_and_fee(monkeypatch, fresh_db):
    """The real BTC 3c row, shown as 08:30:51 after a market that closed at 08:30:00."""
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1")}, [_raw_trade("o1", 30)])
    _run(tt.reconcile_fills())
    f = _fill("o1")
    assert f["filled_at"] == "2026-11-06T08:28:43.056353130Z"
    assert f["filled_at_exact"] == 1
    assert f["fees_usd"] == 0.0
    out = tt.fills()["fills"][0]
    assert out["tsExact"] is True and out["feeUsd"] == 0.0
    with db.get_db() as conn:
        row = conn.execute("SELECT fees_usd FROM terminal_orders WHERE order_id='o1'").fetchone()
    assert row["fees_usd"] == 0.0


def test_only_our_side_of_a_trade_is_read(monkeypatch, fresh_db):
    """Each trade also carries the stranger's order and what they paid."""
    row = us.activity_row(_raw_trade("o1", 30, fee="0.0600", taker=True))
    assert (row["orderId"], row["feeUsd"], row["orderShares"]) == ("o1", 0.06, 30.0)
    row = us.activity_row(_raw_trade("o1", 30))
    assert (row["orderId"], row["feeUsd"]) == ("o1", 0.0)


def test_a_fill_in_stages_sums_its_executions(monkeypatch, fresh_db):
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1", filled=4.0, price=0.55)},
           [_raw_trade("o1", 2, fee="0.0300", when="2026-11-06T08:26:04Z", taker=True),
            _raw_trade("o1", 2, when="2026-11-06T08:28:00Z")])
    _run(tt.reconcile_fills())
    f = _fill("o1")
    assert f["fees_usd"] == 0.03
    assert f["filled_at"] == "2026-11-06T08:28:00Z"


@pytest.mark.parametrize("trades, liquidity", [
    ([_raw_trade("o1", 30)], "maker"),
    ([_raw_trade("o1", 30, fee="0.0600", taker=True)], "taker"),
    ([_raw_trade("o1", 10, taker=True), _raw_trade("o1", 20)], "mixed"),
])
def test_a_fill_says_whether_it_made_or_took(monkeypatch, fresh_db, trades, liquidity):
    """The exchange marks every trade with who the aggressor was, and the app
    threw it away: nobody could tell a resting fill from one that paid the fee."""
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1")}, trades)
    _run(tt.reconcile_fills())
    assert _fill("o1")["liquidity"] == liquidity
    assert tt.fills()["fills"][0]["liquidity"] == liquidity


def test_liquidity_is_unknown_until_the_trades_are_read_and_again_when_more_fills(
        monkeypatch, fresh_db):
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1", filled=10.0, status="live")}, [])
    _run(tt.reconcile_fills())
    assert tt.fills()["fills"][0]["liquidity"] is None
    _venue(monkeypatch, {"o1": _view("o1", filled=10.0, status="live")}, [_raw_trade("o1", 10)])
    _run(tt.reconcile_fills())
    assert _fill("o1")["liquidity"] == "maker"
    # Ten more fill: what was read describes the first ten only.
    _venue(monkeypatch, {"o1": _view("o1", filled=20.0, status="live")}, RuntimeError("activity down"))
    _run(tt.reconcile_fills())
    assert _fill("o1")["liquidity"] is None


@pytest.mark.parametrize("trades", [
    RuntimeError("activity down"),
    [],
    [_raw_trade("o1", 29)],                            # not the whole fill yet
    [_raw_trade("somebody-else", 30)],
    [_raw_trade("o1", 30, state="TRADE_STATE_BUSTED")],
])
def test_no_full_record_leaves_the_fill_unknown(monkeypatch, fresh_db, trades):
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1")}, trades)
    _run(tt.reconcile_fills())
    f = _fill("o1")
    assert f["fees_usd"] is None and f["filled_at_exact"] == 0
    assert tt.fills()["fills"][0]["tsExact"] is False


def test_an_execution_with_no_commission_figure_is_not_free(monkeypatch, fresh_db):
    trade = _raw_trade("o1", 30)
    del trade["trade"]["passiveExecution"]["commissionNotionalCollected"]
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1")}, [trade])
    _run(tt.reconcile_fills())
    f = _fill("o1")
    assert f["fees_usd"] is None and f["filled_at_exact"] == 1


def test_the_venues_figure_replaces_an_estimated_fee(monkeypatch, fresh_db):
    _placed("o1", order_type="FAK")
    _venue(monkeypatch, {"o1": _view("o1")}, [_raw_trade("o1", 30, fee="0.0500", taker=True)])
    _run(tt.reconcile_fills())
    assert _fill("o1")["fees_usd"] == 0.05


def test_a_market_that_cannot_be_matched_is_not_reread_every_pass(monkeypatch, fresh_db):
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1")}, [])
    reads = []
    real = tt.api.get_activity

    async def counted(limit=500, *, user=None, market=""):
        reads.append(market)
        return await real(limit, user=user, market=market)
    monkeypatch.setattr(tt.api, "get_activity", counted)
    _run(tt.reconcile_fills())
    _run(tt.reconcile_fills())
    assert reads == [CID]


def test_more_filling_reopens_what_the_true_up_found(monkeypatch, fresh_db):
    _placed("o1")
    _venue(monkeypatch, {"o1": _view("o1", filled=5.0, status="live")}, [_raw_trade("o1", 5)])
    _run(tt.reconcile_fills())
    assert _fill("o1")["filled_at_exact"] == 1 and _fill("o1")["fees_usd"] == 0.0

    # Same size on another pass: nothing it found is lost.
    tt._true_up_tried[CID] = time.monotonic()
    _run(tt.reconcile_fills())
    assert _fill("o1")["filled_at_exact"] == 1 and _fill("o1")["fees_usd"] == 0.0

    # More filled: the old answer described a smaller fill.
    _venue(monkeypatch, {"o1": _view("o1", filled=9.0, status="live")}, [])
    _run(tt.reconcile_fills())
    assert _fill("o1")["filled_at_exact"] == 0 and _fill("o1")["fees_usd"] is None


def _ledger(rows):
    with db.get_db() as conn:
        for tid, action, px, at, exact in rows:
            conn.execute(
                """INSERT INTO terminal_fills
                     (trade_id, order_id, condition_id, side, action, size, price_cents,
                      fees_usd, origin, filled_at, filled_at_exact)
                   VALUES (?,?,?,'yes',?,5,?,0,'ai',?,?)""",
                (f"order:{tid}", tid, CID, action, px, at, exact))


def test_a_sale_in_the_same_second_as_its_buy_is_not_dropped(fresh_db):
    """The real row: as text "…34Z" sorts after "…34.77…Z", so the sale came first,
    found nothing to close, and the market scored as held to settlement."""
    _ledger([("b", "buy", 30.0, "2026-11-05T15:53:34Z", 1),
             ("s", "sell", 3.0, "2026-11-05T15:53:34.775877278Z", 1)])
    st = tt._ledger_basis()[(CID, "yes")]
    assert st["size"] == 0.0
    assert st["realized"] == pytest.approx(-1.35)


def test_a_reconcile_stamp_later_than_the_sale_does_not_reorder_them(fresh_db):
    """The buy rested from 15:47. A pass only saw it after the sale, so its stamp is
    the later one until the true-up finds its real time."""
    _placed("b", side="yes", created="2026-11-05 15:47:52")
    _ledger([("b", "buy", 30.0, "2026-11-05T15:58:00Z", 0),
             ("s", "sell", 3.0, "2026-11-05T15:53:34.775877278Z", 1)])
    st = tt._ledger_basis()[(CID, "yes")]
    assert st["size"] == 0.0


def _summary(cid, *, close_in=None, outcome=None):
    close = None
    if close_in is not None:
        close = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + close_in))
    return {"conditionId": cid, "title": "T", "closeTime": close,
            "resolvedOutcome": outcome, "outcomes": ["Up", "Down"]}


@pytest.mark.parametrize("close_in,wait", [
    (-60, tt._RESOLVING_MISS_TTL),              # closed: outcome due any moment
    (5, tt._RESOLVING_MISS_TTL),
    (120, 120),                                 # asked again as it closes
    (3600, tt._RESOLUTION_MISS_TTL),
    (None, tt._RESOLUTION_MISS_TTL),
])
def test_a_miss_lasts_only_until_the_answer_can_change(close_in, wait):
    stamp = tt._miss_stamp(_summary("m", close_in=close_in), 1000.0)
    assert not tt._miss_expired(stamp, 1000.0 + wait - 2)
    assert tt._miss_expired(stamp, 1000.0 + wait + 2)


def test_a_closed_market_is_asked_again_within_seconds(monkeypatch, fresh_db):
    answers = [_summary(CID, close_in=-30)]

    async def _summaries(ids):
        return list(answers)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    assert _run(tt._resolutions_for([CID]))[CID] is None

    answers[0] = _summary(CID, close_in=-30, outcome="yes")
    tt._resolution_misses[CID] -= tt._RESOLVING_MISS_TTL + 1
    assert _run(tt._resolutions_for([CID]))[CID] == "yes"


def test_the_two_screens_agree_on_an_outcome_either_one_learned(monkeypatch, fresh_db):
    """The record had missed this market a moment ago; the portfolio's lookup has
    since learned the outcome. Both must now count it."""
    async def _summaries(ids):
        return [_summary(CID, close_in=-30, outcome="no")]
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    tt._resolution_scope_check()
    tt._meta_misses[CID] = time.monotonic()
    tt._resolution_remember(CID, "no")
    assert _run(tt._market_meta_for([CID]))[CID]["outcome"] == "no"
