"""Concurrency/round-trip regressions for the three trading engines.

Each test here pins a hot loop that used to be SERIAL — N wallets, N pending
orders, N open positions — and would silently go back to N × latency if someone
"simplified" the gather away. They assert wall-clock, so the stubs sleep a fixed
amount and the bound is generous (3-4× the ideal) to stay stable on a loaded CI
box while still failing hard on a serial regression.
"""
from __future__ import annotations

import asyncio
import itertools
import time

import pytest

import config
import db
import trader


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ───────── 1. copy tick: per-wallet /positions reads are gathered ─────────

_WALLETS = [f"0x{i:040x}" for i in range(1, 6)]


def _pos(ticker: str) -> dict:
    return {
        "ticker": ticker, "position_fp": 10.0, "market_exposure_dollars": 50.0,
        "cur_price": 0.5, "title": ticker, "event_ticker": "E",
    }


# ───────── 2. open-order poll: get_order calls are gathered ─────────

@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-speed.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


_ids = itertools.count(1)


def _seed(**over) -> int:
    n = next(_ids)
    row = {
        "signal_source": "whale", "signal_id": n, "ticker": f"SPD-{n}",
        "event_ticker": "", "direction": "yes", "target_contracts": 5,
        "limit_price_cents": 50, "filled_contracts": 0, "cost_usd": 0.0,
        "client_order_id": f"spd-co-{n}", "order_id": over.get("order_id"),
        "status": over.get("status", "submitted"), "network": "mainnet",
    }
    with db.get_db() as conn:
        return db.insert_bot_position(conn, row)


def test_pending_order_polls_run_concurrently(fresh_db, monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    trader._poll_failures.clear()
    pids = [_seed(order_id=f"OID-SPD-{i}") for i in range(20)]

    calls = []

    async def _no_positions(*_a, **_k):
        return []

    async def _slow_get_order(oid):
        calls.append(oid)
        await asyncio.sleep(0.03)
        return {"order": {"status": "MATCHED", "size_matched": "5",
                          "original_size": "5", "price": "0.60"}}

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _slow_get_order)

    cfg = config.merge_with_defaults({})
    cfg["network"] = "mainnet"
    t0 = time.perf_counter()
    updated = asyncio.run(trader.poll_open_orders(cfg))
    elapsed = time.perf_counter() - t0

    assert len(calls) == 20
    assert len(updated) == 20, "every pending order must still be reconciled"
    assert elapsed < 0.2, f"20 × 30ms order polls took {elapsed*1000:.0f}ms (serial?)"

    # ...and every row genuinely got written, not just returned.
    with db.get_db() as conn:
        for pid in pids:
            row = db.fetch_position_by_id(conn, pid)
            assert row["status"] == "filled", row
            assert row["filled_contracts"] == 5
            assert row["cost_usd"] == pytest.approx(3.0)


def test_one_failing_order_does_not_stop_the_rest(fresh_db, monkeypatch):
    """A single blown poll must still be isolated to its own row now that the
    calls are gathered — the exception is replayed in that order's turn."""
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    trader._poll_failures.clear()
    bad_oid = "OID-BAD"
    pid_bad = _seed(order_id=bad_oid)
    good = [_seed(order_id=f"OID-GOOD-{i}") for i in range(4)]

    async def _no_positions(*_a, **_k):
        return []

    async def _get_order(oid):
        if oid == bad_oid:
            raise RuntimeError("connection reset")
        return {"order": {"status": "MATCHED", "size_matched": "5",
                          "original_size": "5", "price": "0.60"}}

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _get_order)

    cfg = config.merge_with_defaults({})
    cfg["network"] = "mainnet"
    updated = asyncio.run(trader.poll_open_orders(cfg))

    assert len(updated) == 4
    with db.get_db() as conn:
        assert db.fetch_position_by_id(conn, pid_bad)["status"] == "submitted"
        for pid in good:
            assert db.fetch_position_by_id(conn, pid)["status"] == "filled"
    # The failure counter still ticked for the bad row only.
    assert trader._poll_failures.get(pid_bad) == 1


# ───────── 3. crypto15m: one market batch per tick, gathered manage ─────────

import crypto15m_trader as ct  # noqa: E402


def _seed_c15(n: int) -> int:
    with db.get_db() as conn:
        return db.insert_crypto15m_position(conn, {
            "asset": f"A{n}", "series": "S", "ticker": f"C15-{n}", "side": "up",
            "direction": "yes", "target_contracts": 10, "filled_contracts": 10,
            "entry_limit_cents": 50, "avg_entry_cents": 50.0, "cost_usd": 5.0,
            "client_order_id": f"c15-co-{n}", "order_id": f"c15-oid-{n}",
            "status": "filled", "network": "mainnet",
        })


def test_crypto_tick_batches_markets_and_gathers_manage(fresh_db, monkeypatch):
    monkeypatch.setattr(ct.trader, "get_env", lambda: "mainnet")
    monkeypatch.setattr(ct, "_last_reconcile_t", time.time(), raising=False)
    pids = [_seed_c15(i) for i in range(7)]

    batch_calls = []
    single_calls = []

    async def _batch(tickers, concurrency=8):
        batch_calls.append(list(tickers))
        await asyncio.sleep(0.03)
        return {t: {"ticker": t} for t in tickers}

    async def _single(ticker):
        single_calls.append(ticker)
        return {"ticker": ticker}

    seen_pids = []

    async def _manage(pos, cfg, env, *, market=ct._UNSET):
        seen_pids.append(pos["id"])
        assert market == {"ticker": pos["ticker"]}, "batched market not passed through"
        await asyncio.sleep(0.03)
        return None

    monkeypatch.setattr(ct.polymarket_api, "fetch_markets_map", _batch)
    monkeypatch.setattr(ct.polymarket_api, "fetch_market", _single)
    monkeypatch.setattr(ct, "_manage_position", _manage)

    cfg = config.merge_with_defaults({"crypto15m_enabled": False})
    t0 = time.perf_counter()
    out = asyncio.run(ct.run_tick(cfg, authed=True))
    elapsed = time.perf_counter() - t0

    assert out == []
    assert len(batch_calls) == 1, f"expected ONE market batch per tick, got {batch_calls}"
    assert sorted(batch_calls[0]) == sorted(f"C15-{i}" for i in range(7))
    assert single_calls == [], "per-position fetch_market must not fire when batched"
    # Every position managed exactly once, no id twice.
    assert sorted(seen_pids) == sorted(pids)
    assert len(set(seen_pids)) == len(seen_pids)
    assert elapsed < 0.15, f"batch+gather took {elapsed*1000:.0f}ms (serial?)"


def test_manage_position_still_fetches_its_own_market_when_not_given(fresh_db, monkeypatch):
    """The `market=` parameter is optional — other callers and the existing
    tests must keep the old per-position fetch."""
    single_calls = []

    async def _single(ticker):
        single_calls.append(ticker)
        return None

    monkeypatch.setattr(ct.polymarket_api, "fetch_market", _single)
    monkeypatch.setattr(ct, "_resolved_payout_from_chain",
                        lambda _p: asyncio.sleep(0, result=None))
    pos = {"id": 1, "ticker": "T-X", "asset": "BTC", "direction": "yes",
           "status": "filled", "filled_contracts": 0, "cost_usd": 0.0,
           "dry_run": 0}
    asyncio.run(ct._manage_position(pos, config.merge_with_defaults({}), "mainnet"))
    assert single_calls == ["T-X"]


# ───────── 4. main scan: no forced balance refetch per entry ─────────

def _wire_scan(monkeypatch, *, start_usd: float, cost_usd: float, n_candidates: int):
    """Drive scan_for_trades past its gates with `n_candidates` accepted
    signals, each committing `cost_usd`. Returns (forced, executed)."""
    forced: list[bool] = []
    executed: list[str] = []

    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    monkeypatch.setattr(trader, "_is_blocked_by_daily_risk", lambda *a: (False, ""))
    monkeypatch.setattr(trader, "_lifetime_loss_tripped_main", lambda *a: (False, ""))
    monkeypatch.setattr(trader, "can_open_new_entries", lambda *a: (True, ""))
    monkeypatch.setattr(trader, "_is_blocked_by_trading_hours", lambda *a: (False, ""))
    monkeypatch.setattr(trader, "should_trade", lambda *a: (True, ""))

    sigs = [{"id": i, "ticker": f"SC-{i}", "event_ticker": "", "title": "t",
             "category": "sports", "price": 0.5, "confidence": 90.0,
             "taker_side": "yes"} for i in range(n_candidates)]
    monkeypatch.setattr(trader.db, "already_traded_signal_ids", lambda *a, **k: set())
    monkeypatch.setattr(trader.db, "fetch_tradeable_whale_signals",
                        lambda conn, **k: list(sigs))
    monkeypatch.setattr(trader.db, "fetch_tradeable_momentum_signals",
                        lambda conn, **k: [])

    async def _refresh(cfg, force: bool = False):
        forced.append(bool(force))
        return int(round(start_usd * 100)), 0

    async def _execute(sig, src, cfg, balance_usd):
        executed.append(sig["ticker"])
        return {"status": "submitted", "cost_usd": cost_usd,
                "target_contracts": 10, "limit_price_cents": 50,
                "ticker": sig["ticker"]}

    monkeypatch.setattr(trader, "refresh_balance", _refresh)
    monkeypatch.setattr(trader, "execute_signal", _execute)
    return forced, executed


def test_three_entries_force_exactly_one_balance_refresh(fresh_db, monkeypatch):
    forced, executed = _wire_scan(monkeypatch, start_usd=500.0, cost_usd=5.0,
                                  n_candidates=3)
    cfg = config.merge_with_defaults({"enable_trading": True, "trade_whales": True,
                                      "trade_momentum": False})
    rows = asyncio.run(trader.scan_for_trades(cfg))

    assert len(executed) == 3, executed
    assert len(rows) == 3
    # One cache-honouring read at the top of the cycle, one forced at the end.
    assert forced == [False, True], forced
    assert sum(1 for f in forced if f) == 1, "expected exactly one forced refresh"


def test_halt_still_fires_when_the_decremented_balance_crosses_five(fresh_db, monkeypatch):
    # $20 start, $8 per entry: 20 -> 12 -> 4, so the third candidate is never
    # executed even though the venue was never re-read mid-cycle.
    forced, executed = _wire_scan(monkeypatch, start_usd=20.0, cost_usd=8.0,
                                  n_candidates=3)
    cfg = config.merge_with_defaults({"enable_trading": True, "trade_whales": True,
                                      "trade_momentum": False})
    rows = asyncio.run(trader.scan_for_trades(cfg))

    assert len(executed) == 2, f"halt should have stopped the cycle: {executed}"
    assert len(rows) == 2
    assert sum(1 for f in forced if f) == 1
