"""Money-path fixes from the 2026-09-01 release audit, pinned.

Each test here is a bug that shipped past 1,040 green tests — in one case
BECAUSE of a test that stubbed a value production never emits. They are kept
together so the next audit can see at a glance which regressions are guarded.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

import crypto15m_trader as c15
import db
import polymarket_api
import terminal_trade as tt
import trader


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ────────────────────────────────────────────────────────────────────────
# 1. Paper mode is a gate inside place(), not only in the RPC handler.
# ────────────────────────────────────────────────────────────────────────


def test_place_refuses_a_real_order_while_paper_mode_is_on(monkeypatch):
    """The standing tick and the chat bots call place() directly. The paper
    check used to live only in the RPC handler, so a stop loss armed before
    switching to paper signed a REAL sell when it triggered."""
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: True)

    async def _preview(req):  # must never be reached
        raise AssertionError("preview ran while paper mode was on")
    monkeypatch.setattr(tt, "preview", _preview)

    async def _place_limit_order(**kw):
        raise AssertionError("a real order was signed in paper mode")
    monkeypatch.setattr(tt.api, "place_limit_order", _place_limit_order)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "sell",
                       "priceCents": 40, "size": 10, "orderType": "FAK",
                       "confirmUsd": 4.0}))
    assert r["ok"] is False
    assert "Paper mode is on" in r["message"]


def test_paper_mode_is_read_from_the_live_config(monkeypatch):
    fake = types.SimpleNamespace(STATE=types.SimpleNamespace(cfg={"terminal_paper_mode": True}))
    monkeypatch.setitem(sys.modules, "service", fake)
    assert tt._paper_mode_on() is True
    fake.STATE.cfg = {"terminal_paper_mode": False}
    assert tt._paper_mode_on() is False
    fake.STATE.cfg = None
    assert tt._paper_mode_on() is False


def _standing(monkeypatch, *, positions, prob=30.0, placed=None):
    """One armed stop at 40c on a market currently at `prob`."""
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

    async def _quotes(rows):
        return rows
    monkeypatch.setattr(tt.term, "clob_quotes", _quotes)

    async def _positions(*a, **kw):
        return positions
    monkeypatch.setattr(tt.api, "get_positions", _positions)

    async def _place(req, **kw):
        if placed is not None:
            placed.append(req)
        return {"ok": True, "message": "sold"}
    monkeypatch.setattr(tt, "place", _place)

    async def _preview(req):
        return {"costUsd": 1.0}
    monkeypatch.setattr(tt, "preview", _preview)
    return entries


def test_a_standing_stop_does_not_fire_in_paper_mode(monkeypatch):
    placed = []
    held = [{"ticker": "0xabc", "position_fp": 100.0}]
    entries = _standing(monkeypatch, positions=held, placed=placed)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    r = _run(tt.standing_tick(allow_trading=True, paper=True))
    assert placed == [], "a standing stop placed an order while paper mode was on"
    assert entries[0]["armed"] is False
    assert "paper mode is on" in entries[0]["note"]
    assert r["alerts"] and r["alerts"][0]["blocked"] is True


def test_the_standing_tick_reads_the_paper_switch_itself(monkeypatch):
    """A caller that forgets to pass `paper=` must still be covered."""
    placed = []
    held = [{"ticker": "0xabc", "position_fp": 100.0}]
    _standing(monkeypatch, positions=held, placed=placed)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: True)
    _run(tt.standing_tick(allow_trading=True))
    assert placed == []


def test_a_standing_stop_still_fires_live(monkeypatch):
    """Sanity: the same set-up with paper OFF does sell."""
    placed = []
    held = [{"ticker": "0xabc", "position_fp": 100.0}]
    _standing(monkeypatch, positions=held, placed=placed)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    _run(tt.standing_tick(allow_trading=True, paper=False))
    assert len(placed) == 1 and placed[0]["action"] == "sell"


# ────────────────────────────────────────────────────────────────────────
# 2. An exit booked from /activity pays its taker fee once, not twice.
# ────────────────────────────────────────────────────────────────────────


def _venue_sell():
    """A real venue row's figures (btc-updown-15m, 2026-10): SELL 70 @ 0.97.
    Gross is $67.90; usdcSize is $67.75741 because the venue WITHHELD the
    taker fee, 0.07 * 0.97 * 0.03 * 70 = $0.14259."""
    return {"conditionId": "0xabc", "type": "TRADE", "side": "SELL",
            "outcomeIndex": 0, "size": 70, "price": 0.97, "usdcSize": 67.75741,
            "timestamp": time.time()}


def test_exit_proceeds_are_net_and_report_the_withheld_fee():
    pos = {"ticker": "0xabc", "direction": "yes", "created_at": "2026-09-01 00:00:00"}
    info = trader._exit_proceeds(pos, [_venue_sell()])
    assert info["kind"] == "sold"
    assert info["proceeds"] == pytest.approx(67.75741)
    assert info["sell_gross"] == pytest.approx(67.90)
    assert info["sell_fee"] == pytest.approx(0.14259)


def test_a_manual_exit_on_polymarket_is_charged_its_exit_fee_once(tmp_path, monkeypatch):
    """The 2026-09-01 fix that made "sold" count as a sell switched on a
    `_sell_fee_usd` charge on top of /activity's usdcSize, which is already
    net of that fee: every manual exit was booked one taker fee short, and the
    loss breakers ran on the understated figure."""
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "exits.db")
    db.init_db()
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, {
            "signal_source": "main", "signal_id": 1, "ticker": "0xabc",
            "direction": "yes", "category": "crypto",
            "target_contracts": 70, "limit_price_cents": 60,
            "filled_contracts": 70, "avg_fill_price_cents": 60.0, "cost_usd": 42.0,
            "client_order_id": "main-1", "status": "filled", "network": "mainnet",
        })

    async def _positions():
        return [{"ticker": "0xother", "position_fp": 5.0}]   # healthy read; ours is gone

    async def _activity(limit=1000, **kw):
        return [_venue_sell()]
    monkeypatch.setattr(trader, "get_positions", _positions)
    monkeypatch.setattr(trader, "get_activity", _activity)

    closed = _run(trader.detect_external_exits({}))
    assert [r["id"] for r in closed] == [pid]
    entry_fee = 0.0695 * 0.6 * 0.4 * 70                     # $1.1676
    assert closed[0]["pnl_usd"] == pytest.approx(67.75741 - 42.0 - entry_fee, abs=1e-4)
    # The withheld exit fee still counts toward the fees paid.
    assert closed[0]["fees_usd"] == pytest.approx(entry_fee + 0.14259, abs=1e-6)


# ────────────────────────────────────────────────────────────────────────
# 3. "No wallet" is not "no positions" for callers that retire stops.
# ────────────────────────────────────────────────────────────────────────


def test_get_positions_strict_raises_when_our_address_cannot_be_read(monkeypatch):
    def _boom():
        raise RuntimeError("no saved wallet")
    monkeypatch.setattr(polymarket_api.auth, "trading_address", _boom)

    async def _get(*a, **kw):
        raise AssertionError("the network must not be hit without an address")
    monkeypatch.setattr(polymarket_api, "_get", _get)

    assert _run(polymarket_api.get_positions()) == []
    with pytest.raises(RuntimeError):
        _run(polymarket_api.get_positions(strict=True))


def test_the_standing_tick_holds_fire_when_the_wallet_address_is_missing(monkeypatch):
    """With the old non-strict read, a missing address became an EMPTY
    holdings map, and every armed stop retired itself with the false note
    "you no longer hold this position"."""
    placed = []
    entries = _standing(monkeypatch, positions=[], placed=placed)

    async def _positions(*a, **kw):
        assert kw.get("strict") is True, "the standing tick must read strictly"
        raise RuntimeError("no saved wallet")
    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)

    _run(tt.standing_tick(allow_trading=True))
    assert placed == []
    assert entries[0]["armed"] is True, "the stop was retired on an unreadable wallet"
    assert "could not be read" in entries[0]["note"]


# ────────────────────────────────────────────────────────────────────────
# 4. A failed take-profit must not swallow the stop-loss.
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def c15_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "exit.db")
    db.init_db()
    c15._exit_fail_at.clear()
    c15._stop_retry_at.clear()
    return tmp_path


def _c15_pos(pid=1):
    return {"id": pid, "ticker": "0xabc", "direction": "up", "asset": "BTC",
            "filled_contracts": 10}


def _c15_wire(monkeypatch):
    calls = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "OID", "status": "live"}}

    monkeypatch.setattr(c15.polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(c15.db, "update_crypto15m_position", lambda *a, **k: None)
    monkeypatch.setattr(c15.db, "fetch_crypto15m_by_id",
                        lambda *a, **k: {"id": 1, "status": "exiting"})
    return calls


def test_the_stop_loss_is_exempt_from_the_shared_exit_cooldown(c15_fresh, monkeypatch):
    """A failed take-profit stamped a per-position cooldown that
    _place_exit_sell honoured for EVERY reason, so the stop-loss 20 s later
    submitted nothing — and _manage_position had already consumed the stop's
    own 90 s throttle. The position sat unguarded for the whole window."""
    calls = _c15_wire(monkeypatch)
    c15._exit_fail_at[1] = time.time()  # a take-profit just failed

    _run(c15._place_exit_sell(_c15_pos(), {}, exit_cents=45, reason="take_profit"))
    assert calls == [], "take-profit should still honour its cooldown"

    _run(c15._place_exit_sell(_c15_pos(), {}, exit_cents=45, reason="stop_loss"))
    assert len(calls) == 1 and calls[0]["action"] == "sell", \
        "the stop-loss was swallowed by the take-profit's cooldown"


# ────────────────────────────────────────────────────────────────────────
# 5. Paired mode counts both legs' taker fees against the cap.
# ────────────────────────────────────────────────────────────────────────


def _paired_asset(up_ask: float, down_ask: float) -> dict:
    return {
        "hasMarket": True, "favorite": "up", "inWindow": True,
        "upAsk": up_ask, "downAsk": down_ask, "ticker": "KXBTC15M-P1",
        "modelProb": 0.62, "secsLeft": 600, "book": {},
    }


# ────────────────────────────────────────────────────────────────────────
# 6. Paper mode: a resting sell reserves inventory, and cash is credited
#    only for contracts that were actually held.
# ────────────────────────────────────────────────────────────────────────

import terminal_paper as tp  # noqa: E402
from test_terminal_paper import _mock_market, _order, paper_db  # noqa: E402,F401


def test_two_resting_sells_cannot_both_be_written_against_one_holding(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    r0 = _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    assert r0["ok"]
    _mock_market(monkeypatch, bids=[(30.0, 100)])  # 45¢ sells rest above the bid
    r1 = _run(tp.place(_order(action="sell", priceCents=45, size=10, orderType="GTC")))
    assert r1["ok"] and r1["status"] == "resting"
    r2 = _run(tp.place(_order(action="sell", priceCents=45, size=10, orderType="GTC")))
    assert r2["ok"] is False
    assert "naked short" in r2["message"].lower()


def test_a_sell_that_outran_the_holding_does_not_credit_phantom_cash(paper_db, monkeypatch):
    """Legacy rows (or a race) can still leave two filled sells against one
    holding. The ledger must credit the contracts that existed, not the size
    on the row — the old code booked $1,014 of cash on a $1,004.50 account."""
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))  # cash 996
    for _ in range(2):
        tp._record({
            "condition_id": "0xabc", "title": "T", "side": "yes",
            "action": "sell", "order_type": "FAK", "requested_size": 10,
            "requested_price_cents": 50, "filled_size": 10,
            "avg_fill_price_cents": 50.0, "resting_size": 0, "fees_usd": 0.0,
            "cost_usd": 5.0, "status": "filled", "note": "", "filled_at": tp._now_iso(),
        })
    pf = _run(tp.portfolio({"terminal_paper_bankroll_usd": 1000.0}))
    # Bought 10 @ 40¢ ($4), sold the 10 that existed @ 50¢ ($5): +$1, cash $1,001.
    assert abs(pf["realizedPnlUsd"] - 1.0) < 1e-6
    assert abs(pf["cashUsd"] - 1001.0) < 1e-6, pf["cashUsd"]


def test_the_fill_sweep_cancels_a_resting_sell_once_the_position_is_gone(paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(40.0, 100)])
    _run(tp.place(_order(priceCents=40, size=10, orderType="FAK")))
    _mock_market(monkeypatch, bids=[(30.0, 100)])
    r1 = _run(tp.place(_order(action="sell", priceCents=45, size=10, orderType="GTC")))
    assert r1["status"] == "resting"
    # Close the position by hand at the touch while the sell still rests.
    _mock_market(monkeypatch, bids=[(44.0, 100)])
    # The market sell is refused (inventory reserved), so simulate the
    # position vanishing the way legacy data could: a filled sell row.
    tp._record({
        "condition_id": "0xabc", "title": "T", "side": "yes",
        "action": "sell", "order_type": "FAK", "requested_size": 10,
        "requested_price_cents": 44, "filled_size": 10,
        "avg_fill_price_cents": 44.0, "resting_size": 0, "fees_usd": 0.0,
        "cost_usd": 4.4, "status": "filled", "note": "", "filled_at": tp._now_iso(),
    })
    _mock_market(monkeypatch, bids=[(50.0, 100)])  # trades through 45¢
    assert _run(tp._fill_resting()) == 0
    with tp._conn() as conn:
        row = conn.execute("SELECT status, resting_size, note FROM terminal_paper_orders "
                           "WHERE id=?", (r1["localId"],)).fetchone()
    assert row["status"] == "canceled" and float(row["resting_size"]) == 0.0
    assert "no longer held" in row["note"]


def test_per_outcome_redeem_rows_book_a_settled_loss_at_zero():
    """/activity splits redemptions per outcome since 2026-08-10. Our outcome's
    REDEEM row paying $0 is a settled loss — not 'no evidence' (which left the
    position open forever) — and the OTHER outcome's row is never ours."""
    import trader
    pos = {"ticker": "0xabc", "direction": "yes", "created_at": "2026-09-01 00:00:00"}
    later = 1_790_000_000
    lost = [
        {"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 0, "usdcSize": 0, "timestamp": later},
        {"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 1, "usdcSize": 25.0, "timestamp": later},
    ]
    assert trader._exit_proceeds(pos, lost) == {"proceeds": 0.0, "kind": "redeemed"}
    won = [{"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 0, "usdcSize": 25.0, "timestamp": later},
           {"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 1, "usdcSize": 0, "timestamp": later}]
    assert trader._exit_proceeds(pos, won) == {"proceeds": 25.0, "kind": "redeemed"}
    # No row for our outcome at all: still no evidence.
    other = [{"conditionId": "0xabc", "type": "REDEEM", "outcomeIndex": 1, "usdcSize": 0, "timestamp": later}]
    assert trader._exit_proceeds(pos, other) is None
