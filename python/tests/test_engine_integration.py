"""DB-backed integration tests for the trading engine.

These exercise the three functions where the nastiest historical bugs
lived (per trader.py's comments): order placement + the entry gates
(`execute_signal`), the reconcile/poll state machine (`poll_open_orders`),
and settlement P&L (`mark_resolved_positions`).

Strategy:
  - Real `db` module against a fresh temp SQLite file per test (so the
    actual schema + queries are under test, not a mock of them).
  - Every Polymarket network call is stubbed. trader.py imports these names
    into its own module namespace (`from polymarket_api import place_limit_order`),
    so the stubs are installed on the `trader` module, not on `polymarket_api`.
  - The engine functions are async; we drive them with `asyncio.run`.
"""
from __future__ import annotations

import asyncio
import itertools

import pytest

import db
import polymarket_api
import trader
from config import merge_with_defaults
from polymarket_api import PolymarketAPIError


# ───────── fixtures / helpers ──────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point db.get_db() at a brand-new SQLite file and create the schema."""
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


@pytest.fixture
def env_net(monkeypatch):
    """Pin the engine's notion of the active Polymarket env to 'mainnet'."""
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    return "mainnet"


@pytest.fixture
def cfg():
    c = merge_with_defaults({"account_mode": "live"})  # engines send only from a Live account
    c["network"] = "mainnet"
    return c


def run_async(coro):
    return asyncio.run(coro)


_ids = itertools.count(1)


def seed_position(**over) -> int:
    """Insert one bot_positions row with sane, unique defaults.

    Pass `created_at_offset_sec` (negative for the past) to backdate the
    row's created_at after insert.
    """
    n = next(_ids)
    row = {
        "signal_source": over.get("signal_source", "whale"),
        "signal_id": over.get("signal_id", n),
        "ticker": over.get("ticker", f"TCK-{n}"),
        "event_ticker": over.get("event_ticker", ""),
        "direction": over.get("direction", "yes"),
        "target_contracts": over.get("target_contracts", 10),
        "limit_price_cents": over.get("limit_price_cents", 50),
        "filled_contracts": over.get("filled_contracts", 0),
        "cost_usd": over.get("cost_usd", 0.0),
        "client_order_id": over.get("client_order_id", f"co-{n}"),
        "order_id": over.get("order_id"),
        "status": over.get("status", "filled"),
        "network": over.get("network", "mainnet"),
    }
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, row)
        if "created_at_offset_sec" in over:
            conn.execute(
                "UPDATE bot_positions SET created_at=datetime('now', ?) WHERE id=?",
                (f"{int(over['created_at_offset_sec'])} seconds", pid),
            )
    return pid


def whale_signal(**over) -> dict:
    s = {
        "id": 1, "ticker": "WHALE-1", "event_ticker": "", "title": "t",
        "category": "sports", "price": 0.60, "confidence": 80.0,
        "taker_side": "yes",
    }
    s.update(over)
    return s


def count_rows() -> int:
    with db.get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM bot_positions").fetchone()[0]


def fetch(pid: int) -> dict:
    with db.get_db() as conn:
        return db.fetch_position_by_id(conn, pid)


async def _stub_no_quote(_ticker, _side=None):
    # No live quote -> _compute_limit_price_cents falls back to signal+offset.
    return {"bid_cents": None, "ask_cents": None}


# ───────── execute_signal: entry gates ────────────────────────────────


def test_execute_skips_when_max_open_positions_hit(fresh_db, env_net, cfg):
    cfg["max_open_positions"] = 1
    seed_position(status="filled")  # one already-open position
    result = run_async(
        trader.execute_signal(whale_signal(id=100, ticker="NEW"), "whale", cfg, 1000.0)
    )
    assert result is None
    assert count_rows() == 1  # nothing new inserted


def test_execute_skips_when_daily_cap_hit(fresh_db, env_net, cfg):
    cfg["max_open_positions"] = 100
    cfg["unlimited_daily_new_positions"] = False
    cfg["max_daily_new_positions"] = 2
    seed_position(status="filled")
    seed_position(status="filled")
    result = run_async(
        trader.execute_signal(whale_signal(id=101, ticker="NEW"), "whale", cfg, 1000.0)
    )
    assert result is None
    assert count_rows() == 2


def test_execute_skips_second_position_in_same_event(fresh_db, env_net, cfg):
    cfg["max_positions_per_event"] = 1
    seed_position(status="filled", event_ticker="EVT-A", ticker="A-1")
    result = run_async(
        trader.execute_signal(
            whale_signal(id=102, ticker="A-2", event_ticker="EVT-A"),
            "whale", cfg, 1000.0,
        )
    )
    assert result is None


def test_per_event_cap_enforced_for_n_greater_than_one(fresh_db, env_net, cfg, monkeypatch):
    # M1: the cap is a real integer now, not just an ==1 special case. With a cap
    # of 2, the 3rd position in an event is rejected (the old code made any cap
    # >1 UNLIMITED).
    cfg["max_positions_per_event"] = 2
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    async def _place(**_kw):
        return {"order": {"order_id": "o", "status": "live"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)
    cfg["enable_trading"] = True

    seed_position(status="filled", event_ticker="EVT-B", ticker="B-1")
    seed_position(status="filled", event_ticker="EVT-B", ticker="B-2")
    # 3rd in the same event -> blocked.
    result = run_async(
        trader.execute_signal(
            whale_signal(id=110, ticker="B-3", event_ticker="EVT-B"),
            "whale", cfg, 1000.0,
        )
    )
    assert result is None


def test_execute_skips_duplicate_market_and_side(fresh_db, env_net, cfg):
    seed_position(status="filled", ticker="DUP", direction="yes")
    result = run_async(
        trader.execute_signal(
            whale_signal(id=103, ticker="DUP", taker_side="yes"), "whale", cfg, 1000.0
        )
    )
    assert result is None


def test_execute_skips_when_exposure_leaves_under_one_dollar(fresh_db, env_net, cfg, monkeypatch):
    # max exposure = 1000 * 0.75 = 750; seed 749.50 of open cost so only
    # $0.50 of headroom remains -> target size < $1 -> skip.
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)
    seed_position(status="filled", ticker="EXP-SEED", cost_usd=749.50)
    result = run_async(
        trader.execute_signal(
            whale_signal(id=104, ticker="EXP-NEW"), "whale", cfg, 1000.0
        )
    )
    assert result is None
    assert count_rows() == 1


def test_resting_orders_do_not_inflate_exposure_cap(fresh_db, env_net, cfg, monkeypatch):
    # Regression (audit HIGH): on Polymarket a resting limit order does NOT lock
    # wallet USDC, so balance_usd stays put while 'submitted' notional piles up.
    # The exposure cap must be measured against cash + FILLED cost only — counting
    # resting notional in the denominator (the old `cash + all-committed`) let
    # unfilled orders ratchet the cap to f/(1-f)x the account (0.75 -> 3x). With
    # $100 cash, total committed exposure must never exceed 0.75 * 100 = $75.
    cfg["enable_trading"] = True
    cfg["max_total_exposure_fraction"] = 0.75
    cfg["max_open_positions"] = 50
    cfg["unlimited_daily_new_positions"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    placed: list[dict] = []

    async def _place(**kw):
        placed.append(kw)
        return {"order": {"order_id": f"OID-{len(placed)}", "status": "resting"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)

    # $70 of RESTING notional (140 @ 50c, cost_usd=0 — not yet filled). Under the
    # old cash+committed denominator this inflated the cap to 0.75*(100+70)=$127.5.
    seed_position(status="submitted", ticker="REST", event_ticker="EVT-REST",
                  target_contracts=140, limit_price_cents=50, cost_usd=0.0,
                  order_id="OID-SEED")

    row = run_async(trader.execute_signal(
        whale_signal(id=300, ticker="NEW", event_ticker="EVT-NEW"),
        "whale", cfg, 100.0,
    ))
    # An order still places (there is ~$5 of real headroom), but only a small one.
    assert row is not None and row["status"] == "submitted"
    new_notional = row["target_contracts"] * row["limit_price_cents"] / 100.0
    assert new_notional <= 6.0, f"new order ${new_notional:.2f} blew past the headroom"
    with db.get_db() as conn:
        total_exposure = db.current_total_exposure_usd(conn, "mainnet")
    # Bounded at 75% of CASH (+$1 rounding slack); the old code reached ~$80-127.
    assert total_exposure <= 76.0, f"exposure ${total_exposure:.2f} exceeded 0.75*cash"


# ───────── daily stop-loss: optional flatten ──────────────────────────


def test_flatten_on_daily_stop_sells_when_enabled(fresh_db, env_net, cfg, monkeypatch):
    # flatten_on_daily_stop ON + loss limit tripped + open position -> flatten once.
    cfg["flatten_on_daily_stop"] = True
    cfg["enable_trading"] = True
    cfg["stop_loss_on_day"] = -10.0
    seed_position(status="filled", cost_usd=5.0, filled_contracts=10)
    called = {"n": 0}

    async def _fake_flatten(_cfg):
        called["n"] += 1
        return {"canceled": 0, "sold": 10, "proceedsUsd": 0.0}
    monkeypatch.setattr(trader, "flatten_open_positions", _fake_flatten)
    monkeypatch.setattr(trader, "_today_pnl_balance_delta", lambda env: -20.0)
    monkeypatch.setattr(trader, "_DAY_RISK_PERSIST_SEC", 0.0)
    trader._day_risk_breach.clear()

    run_async(trader._maybe_flatten_on_daily_stop(cfg, "mainnet"))
    assert called["n"] == 1
    trader._day_risk_breach.clear()


# ───────── per-position take-profit sweep ─────────────────────────────


def _install_sell_stubs(monkeypatch, *, bid_cents):
    """Stub the sell path so take_profit_sweep can flatten a row end-to-end:
    a live bid, an order that places, and a full fill at that bid."""
    async def _quote(_ticker, _side=None):
        return {"bid_cents": bid_cents, "ask_cents": (bid_cents or 0) + 1}

    async def _place(**_kw):
        return {"order": {"order_id": "OID-TP", "status": "matched"}}

    async def _confirm(_oid, qty, px):
        return qty, float(px)          # full fill at the crossed price

    async def _cancel(_oid):
        return True
    monkeypatch.setattr(trader, "get_quote", _quote)
    monkeypatch.setattr(trader, "place_limit_order", _place)
    monkeypatch.setattr(trader, "_confirm_sell", _confirm)
    monkeypatch.setattr(trader, "cancel_order", _cancel)


def test_take_profit_sweep_sells_winner_and_books_reason(fresh_db, env_net, cfg, monkeypatch):
    # 10 ct cost $5.00 (50c/ct). Bid at 80c -> position worth $8.00, +60% > the
    # +20% target -> sold, resolved green, tagged exit_reason='take_profit'.
    cfg["take_profit_pct"] = 0.20
    pid = seed_position(status="filled", cost_usd=5.0, filled_contracts=10, direction="yes")
    _install_sell_stubs(monkeypatch, bid_cents=80)

    closed = run_async(trader.take_profit_sweep(cfg))
    assert len(closed) == 1
    row = fetch(pid)
    assert row["resolved"] == 1 and row["closed_early"] == 1
    assert row["exit_reason"] == "take_profit"
    assert row["settlement_usd"] == pytest.approx(8.0)
    # 8 proceeds - 5 cost - entry fee - crossing sell's taker fee, at the venue's rate:
    # 0.0695*0.5*0.5*10 = 0.17375 entry, 0.0695*0.8*0.2*10 = 0.1112 sell.
    assert row["pnl_usd"] == pytest.approx(2.71505)


def test_take_profit_sweep_holds_position_below_target(fresh_db, env_net, cfg, monkeypatch):
    # 10 ct cost $9.00 (90c/ct). Bid 91c -> +1.1%, under the +20% target -> untouched.
    cfg["take_profit_pct"] = 0.20
    pid = seed_position(status="filled", cost_usd=9.0, filled_contracts=10, direction="yes")
    _install_sell_stubs(monkeypatch, bid_cents=91)

    closed = run_async(trader.take_profit_sweep(cfg))
    assert closed == []
    row = fetch(pid)
    assert row["resolved"] == 0 and row["closed_early"] == 0
    assert row["exit_reason"] is None


def test_take_profit_sweep_is_noop_when_disabled(fresh_db, env_net, cfg, monkeypatch):
    cfg["take_profit_pct"] = 0.0                     # off
    seed_position(status="filled", cost_usd=5.0, filled_contracts=10)
    _install_sell_stubs(monkeypatch, bid_cents=99)
    assert run_async(trader.take_profit_sweep(cfg)) == []


def test_recent_balance_transition_flags_fresh_fills_and_settlements(fresh_db, env_net):
    # Quiet book → not syncing.
    with db.get_db() as conn:
        assert db.recent_balance_transition(conn, "mainnet") is False
    # A just-created filled position → syncing (its debit may not be in the
    # cash read yet). Old positions must NOT flag.
    seed_position(status="filled", cost_usd=3.0, filled_contracts=5)
    with db.get_db() as conn:
        assert db.recent_balance_transition(conn, "mainnet") is True
        conn.execute("UPDATE bot_positions SET created_at = datetime('now', '-10 minutes')")
        assert db.recent_balance_transition(conn, "mainnet") is False
        # A just-resolved position → syncing (payout in flight).
        conn.execute("UPDATE bot_positions SET resolved_at = datetime('now')")
        assert db.recent_balance_transition(conn, "mainnet") is True
        conn.execute("UPDATE bot_positions SET resolved_at = datetime('now', '-10 minutes')")
        assert db.recent_balance_transition(conn, "mainnet") is False


def test_daily_stop_ignores_transient_settlement_gap(fresh_db, env_net, cfg, monkeypatch):
    # The balance heartbeat reads cash and position value at different
    # moments, so a settlement's payout is in flight for a minute or two and
    # the total transiently reads low. One breaching observation must NOT
    # gate (or flatten); a persistent one must; recovery clears the streak.
    monkeypatch.setattr(trader, "_DAY_RISK_PERSIST_SEC", 180.0)
    trader._day_risk_breach.clear()
    monkeypatch.setattr(trader, "_today_pnl_balance_delta", lambda env: -20.0)
    risk_cfg = {"stop_loss_on_day": -10.0, "take_profit_on_day": 0}
    assert trader._is_blocked_by_daily_risk(risk_cfg, "mainnet")[0] is False
    trader._day_risk_breach[("mainnet", "sl")] = trader.time.monotonic() - 181.0
    blocked, why = trader._is_blocked_by_daily_risk(risk_cfg, "mainnet")
    assert blocked is True and "stop-loss" in why
    monkeypatch.setattr(trader, "_today_pnl_balance_delta", lambda env: +0.3)
    assert trader._is_blocked_by_daily_risk(risk_cfg, "mainnet")[0] is False
    assert trader._day_risk_breach[("mainnet", "sl")] is None
    trader._day_risk_breach.clear()


def test_flatten_on_daily_stop_noop_when_toggle_off(fresh_db, env_net, cfg, monkeypatch):
    cfg["flatten_on_daily_stop"] = False         # opt-in OFF (default) -> no behavior change
    cfg["enable_trading"] = True
    cfg["stop_loss_on_day"] = -10.0
    seed_position(status="filled", cost_usd=5.0, filled_contracts=10)
    called = {"n": 0}

    async def _fake_flatten(_cfg):
        called["n"] += 1
        return {}
    monkeypatch.setattr(trader, "flatten_open_positions", _fake_flatten)
    monkeypatch.setattr(trader, "_today_pnl_balance_delta", lambda env: -20.0)

    run_async(trader._maybe_flatten_on_daily_stop(cfg, "mainnet"))
    assert called["n"] == 0


def test_flatten_not_triggered_by_take_profit(fresh_db, env_net, cfg, monkeypatch):
    # A daily TAKE-PROFIT halt must NOT flatten — only the loss stop does.
    cfg["flatten_on_daily_stop"] = True
    cfg["enable_trading"] = True
    cfg["stop_loss_on_day"] = -1000.0            # nowhere near tripping
    seed_position(status="filled", cost_usd=5.0, filled_contracts=10)
    called = {"n": 0}

    async def _fake_flatten(_cfg):
        called["n"] += 1
        return {}
    monkeypatch.setattr(trader, "flatten_open_positions", _fake_flatten)
    monkeypatch.setattr(trader, "_today_pnl_balance_delta", lambda env: +50.0)  # in profit

    run_async(trader._maybe_flatten_on_daily_stop(cfg, "mainnet"))
    assert called["n"] == 0


# ───────── execute_signal: placement paths ────────────────────────────


def test_execute_real_order_records_order_id(fresh_db, env_net, cfg, monkeypatch):
    cfg["enable_trading"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    calls: list[dict] = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "OID-1", "status": "resting"}}

    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(
        trader.execute_signal(whale_signal(id=201, ticker="LIVE"), "whale", cfg, 1000.0)
    )
    assert row["status"] == "submitted"
    assert row["order_id"] == "OID-1"
    assert len(calls) == 1
    assert calls[0]["count"] == 80
    assert calls[0]["price_cents"] == 62
    assert calls[0]["side"] == "yes"
    assert calls[0]["action"] == "buy"


def test_execute_api_error_is_persisted_as_error_row(fresh_db, env_net, cfg, monkeypatch):
    cfg["dry_run"] = False
    cfg["enable_trading"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    async def _place(**_kw):
        raise PolymarketAPIError(400, {"error": "insufficient_balance"})

    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(
        trader.execute_signal(whale_signal(id=202, ticker="BAD"), "whale", cfg, 1000.0)
    )
    assert row["status"] == "error"
    assert "HTTP 400" in row["error"]
    assert row["order_id"] is None


def test_execute_unknown_outcome_is_tracked_by_its_order_id(fresh_db, env_net, cfg, monkeypatch):
    # The send failed ambiguously and a lookup could not find the order. An
    # error row with no id would forget a live order; a submitted row with
    # the order's id is polled like any other.
    cfg["enable_trading"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)
    oid = "0x" + "ef" * 32

    async def _place(**_kw):
        raise polymarket_api.OrderStatusUnknownError(oid, TimeoutError("read timed out"))

    monkeypatch.setattr(trader, "place_limit_order", _place)
    row = run_async(
        trader.execute_signal(whale_signal(id=203, ticker="LOST"), "whale", cfg, 1000.0)
    )
    assert row["status"] == "submitted"
    assert row["order_id"] == oid


def test_execute_bumps_to_market_minimum_when_affordable(fresh_db, env_net, cfg, monkeypatch):
    # Fractional sizing on a small bankroll yields 4 contracts, below the
    # market's 5-share minimum. Rather than place a doomed sub-min order (which
    # lands as a stuck 'error' row), bump to 5 — it fits the risk budget — and
    # trade.
    cfg["dry_run"] = False
    cfg["enable_trading"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    async def _meta(_ticker):
        return {"min_size": 5}
    monkeypatch.setattr(trader, "get_market_meta", _meta)

    calls: list[dict] = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "OID-9", "status": "resting"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(
        trader.execute_signal(whale_signal(id=210, ticker="SMALL"), "whale", cfg, 50.0)
    )
    assert len(calls) == 1
    assert calls[0]["count"] == 5            # bumped from 4 to the market minimum
    assert row["status"] == "submitted"
    assert row["target_contracts"] == 5


def test_execute_skips_when_market_minimum_exceeds_budget(fresh_db, env_net, cfg, monkeypatch):
    # If meeting the market minimum would blow the risk budget, skip cleanly —
    # no order placed, no stuck 'error' row.
    cfg["dry_run"] = False
    cfg["enable_trading"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    async def _meta(_ticker):
        return {"min_size": 100}             # 100 @ 62c = $62, over the whole budget
    monkeypatch.setattr(trader, "get_market_meta", _meta)

    def _boom(**_kw):
        raise AssertionError("must not place a sub-min order over budget")
    monkeypatch.setattr(trader, "place_limit_order", _boom)

    row = run_async(
        trader.execute_signal(whale_signal(id=211, ticker="PRICEY"), "whale", cfg, 50.0)
    )
    assert row is None
    assert count_rows() == 0


def test_execute_skips_when_live_cross_exceeds_entry_cap(fresh_db, env_net, cfg, monkeypatch):
    # should_trade() passed the 60c signal, but by placement the book moved so
    # the live cross is 95c — above the user's 85c max_entry_price_cents. The
    # order must be skipped, never placed above the configured cap.
    cfg["dry_run"] = False
    cfg["enable_trading"] = True

    async def _moved_quote(_ticker, _side=None):
        return {"bid_cents": 90, "ask_cents": 95}  # live YES ask 95c > 85c cap

    monkeypatch.setattr(trader, "get_quote", _moved_quote)

    def _boom(**_kw):
        raise AssertionError("place_limit_order must not be called above the cap")

    monkeypatch.setattr(trader, "place_limit_order", _boom)

    row = run_async(
        trader.execute_signal(whale_signal(id=203, ticker="MOVED"), "whale", cfg, 1000.0)
    )
    assert row is None
    assert count_rows() == 0


# ───────── poll_open_orders: reconcile state machine ──────────────────


def test_poll_marks_order_filled_from_order_endpoint(fresh_db, env_net, cfg, monkeypatch):
    trader._poll_failures.clear()
    pid = seed_position(status="submitted", order_id="OID-9",
                        target_contracts=5, cost_usd=0.0)

    async def _no_positions(*_a, **_k):
        return []

    async def _get_order(_oid):
        # CLOB OpenOrder shape: 5 of 5 matched @ 0.60 -> cost $3, avg 60c.
        return {"order": {
            "status": "MATCHED", "size_matched": "5", "original_size": "5",
            "price": "0.60",
        }}

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _get_order)

    updated = run_async(trader.poll_open_orders(cfg))
    assert len(updated) == 1
    row = fetch(pid)
    assert row["status"] == "filled"
    assert row["filled_contracts"] == 5
    assert row["cost_usd"] == pytest.approx(3.0)
    assert row["avg_fill_price_cents"] == pytest.approx(60.0)


def test_poll_retires_order_to_gone_only_after_threshold(fresh_db, env_net, cfg, monkeypatch):
    trader._poll_failures.clear()
    pid = seed_position(status="submitted", order_id="OID-10",
                        target_contracts=5)

    async def _no_positions(*_a, **_k):
        return []

    async def _get_order_404(_oid):
        raise PolymarketAPIError(404, "not found")

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _get_order_404)

    # Below threshold (6): the row must survive a single bad poll.
    run_async(trader.poll_open_orders(cfg))
    assert fetch(pid)["status"] == "submitted"

    # Reach the failure threshold; only then does it retire to 'gone'.
    for _ in range(5):
        run_async(trader.poll_open_orders(cfg))
    assert fetch(pid)["status"] == "gone"


def test_poll_auto_cancels_stale_resting_order(fresh_db, env_net, cfg, monkeypatch):
    trader._poll_failures.clear()
    cfg["order_expiration_sec"] = 90
    pid = seed_position(status="submitted", order_id="OID-11",
                        target_contracts=5, created_at_offset_sec=-200)

    async def _no_positions(*_a, **_k):
        return []

    async def _resting(_oid):
        # CLOB OpenOrder shape: still live, nothing matched yet.
        return {"order": {
            "status": "LIVE", "size_matched": "0", "original_size": "5",
            "price": "0.50",
        }}

    cancels: list[str] = []

    async def _cancel(oid):
        cancels.append(oid)
        return {}

    monkeypatch.setattr(trader, "get_positions", _no_positions)
    monkeypatch.setattr(trader, "get_order", _resting)
    monkeypatch.setattr(trader, "cancel_order", _cancel)

    run_async(trader.poll_open_orders(cfg))
    assert cancels == ["OID-11"]
    assert fetch(pid)["status"] == "canceled"


# ───────── mark_resolved_positions: settlement P&L ────────────────────


def _settled_market(result: str):
    async def _fetch(_ticker):
        return {"result": result, "status": "finalized"}
    return _fetch


def test_resolve_winning_yes_position(fresh_db, env_net, cfg, monkeypatch):
    pid = seed_position(status="filled", ticker="RES-WIN", direction="yes",
                        filled_contracts=10, cost_usd=6.0)
    # mark_resolved_positions fetches via fetch_markets_map(), which calls
    # polymarket_api.fetch_market internally — stub it there, not on trader.
    monkeypatch.setattr(polymarket_api, "fetch_market", _settled_market("yes"))

    updated = run_async(trader.mark_resolved_positions(cfg))
    assert len(updated) == 1
    row = fetch(pid)
    assert bool(row["resolved"]) is True
    assert row["outcome_correct"] == 1
    assert row["settlement_usd"] == pytest.approx(10.0)
    # 10 payout - 6 cost - entry taker fee at avg 60c: 0.0695*0.6*0.4*10 = 0.1668
    assert row["pnl_usd"] == pytest.approx(3.8332)
    assert row["fees_usd"] == pytest.approx(0.1668)


def test_resolve_losing_no_position(fresh_db, env_net, cfg, monkeypatch):
    pid = seed_position(status="filled", ticker="RES-LOSS", direction="no",
                        filled_contracts=10, cost_usd=4.0)
    # YES wins -> a NO position pays 0. (Stub on polymarket_api: the resolve
    # path fetches through fetch_markets_map -> polymarket_api.fetch_market.)
    monkeypatch.setattr(polymarket_api, "fetch_market", _settled_market("yes"))

    run_async(trader.mark_resolved_positions(cfg))
    row = fetch(pid)
    assert row["outcome_correct"] == 0
    assert row["settlement_usd"] == pytest.approx(0.0)
    # -4 cost - entry fee (0.0695*0.4*0.6*10 = 0.1668):
    # the taker fee is paid win or lose.
    assert row["pnl_usd"] == pytest.approx(-4.1668)


def test_resolve_no_fill_row_closes_at_zero_with_null_outcome(fresh_db, env_net, cfg):
    # A canceled, never-filled row can't settle — closes flat, no market call.
    pid = seed_position(status="canceled", ticker="RES-NOFILL",
                        filled_contracts=0, cost_usd=0.0)
    run_async(trader.mark_resolved_positions(cfg))
    row = fetch(pid)
    assert bool(row["resolved"]) is True
    assert row["outcome_correct"] is None
    assert row["pnl_usd"] == pytest.approx(0.0)


def test_resolve_clamps_pnl_to_physical_bounds(fresh_db, env_net, cfg, monkeypatch):
    # Inject a corrupt (>1) payout; pnl must clamp to max possible profit
    # (filled - cost), never hallucinate a settlement larger than `filled`.
    pid = seed_position(status="filled", ticker="CLAMP", direction="yes",
                        filled_contracts=10, cost_usd=6.0)
    # Resolution fetches through fetch_markets_map -> polymarket_api.fetch_market.
    monkeypatch.setattr(polymarket_api, "fetch_market", _settled_market("yes"))
    monkeypatch.setattr(trader, "_market_yes_payout", lambda _m: 5.0)

    run_async(trader.mark_resolved_positions(cfg))
    row = fetch(pid)
    # clamped to filled - cost - entry fee (0.1668 at avg 60c)
    assert row["pnl_usd"] == pytest.approx(3.8332)
    assert row["settlement_usd"] == pytest.approx(10.0)  # clamped to filled


# ───────── reconcile: import-unknown guard ────────────────────────────
#
# After a Clear-History wipe there are no local rows to match, and
# Polymarket's portfolio keeps listing losing (worthless) tokens long after
# settlement. Importing those residual losers as fresh "open" positions made
# the next resolve pass book a pile of phantom losses — the user's "clear
# history marked all my open positions lost". The guard skips importing any
# position whose market has already settled.


def _live_pos(ticker: str, qty: float = 10.0, cost_cents: int = 600,
              cur_price: float = 0.0) -> dict:
    return {"ticker": ticker, "position": qty, "market_exposure": cost_cents,
            "title": ticker, "event_ticker": "", "category": "",
            "cur_price": cur_price}


def _open_market():
    async def _fetch(_ticker):
        return {"result": "", "status": "open", "settlement_value_dollars": None}
    return _fetch


def test_reconcile_skips_importing_settled_residual(fresh_db, env_net, monkeypatch):
    # Live position on an ALREADY-SETTLED market, no local row -> must NOT be
    # imported (it's a redeemable/worthless residual, not a live bet).
    async def _live(limit=1000):
        return [_live_pos("SETTLED-1")]
    monkeypatch.setattr(trader, "get_positions", _live)
    monkeypatch.setattr(trader, "fetch_market", _settled_market("no"))

    summary, changed = run_async(trader.reconcile_positions_with_polymarket())

    assert summary["imported_unknowns"] == 0
    assert changed == []
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM bot_positions WHERE ticker='SETTLED-1'"
        ).fetchone()[0]
    assert n == 0   # nothing imported -> no phantom loss to book


def test_resolved_guard_blocks_reimport_after_24h(fresh_db, env_net):
    # Regression (2026-07-07): a Polymarket 15-min ticker is UNIQUE per market
    # and resolves exactly once. The old guard only suppressed re-imports whose
    # resolved_at was within 24h, so a market that settled days ago got
    # re-imported as a fresh `external` row and re-booked as a second full loss
    # (163 imports / 127 distinct tickers, ~$333 phantom double-counted loss).
    # The guard must now block re-import for ANY prior resolution, not just <24h.
    seed_position(signal_source="external", ticker="OLD-15M", direction="yes",
                  status="filled", network=env_net)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE bot_positions SET resolved=1, pnl_usd=-22.2, "
            "resolved_at=datetime('now','-3 days') WHERE ticker='OLD-15M'"
        )
        conn.commit()
        # Old behaviour: within_hours=24 -> False (would re-import = the bug).
        assert db.recent_resolved_position_exists(
            conn, "OLD-15M", "yes", env_net, within_hours=24) is False
        # Fixed default (any prior resolution) -> True, re-import suppressed.
        assert db.recent_resolved_position_exists(
            conn, "OLD-15M", "yes", env_net) is True


def test_reconcile_imports_open_external_position(fresh_db, env_net, monkeypatch):
    # Counterpart: a live position on an OPEN market with no local row IS a
    # genuine holding and must still be imported as an unresolved external row.
    async def _live(limit=1000):
        return [_live_pos("OPEN-1", qty=10, cost_cents=600, cur_price=0.80)]
    monkeypatch.setattr(trader, "get_positions", _live)
    monkeypatch.setattr(trader, "fetch_market", _open_market())

    summary, changed = run_async(trader.reconcile_positions_with_polymarket())

    assert summary["imported_unknowns"] == 1
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT signal_source, status, resolved, mark_price_cents "
            "FROM bot_positions WHERE ticker='OPEN-1'"
        ).fetchone()
    assert row is not None
    assert row["signal_source"] == "external"
    assert row["status"] == "filled"
    assert int(row["resolved"]) == 0
    # Live mark seeded from cur_price (0.80) so unrealized P&L shows at once.
    assert row["mark_price_cents"] == 80.0


def test_reconcile_skips_crypto15m_owned_position(fresh_db, env_net, monkeypatch):
    # H4: a live wallet position owned by the crypto15m engine (separate table,
    # so it never matches a bot_positions row) must NOT be adopted as a duplicate
    # `external` main-bot row — that double-counts P&L/exposure and double-books
    # at settlement.
    with db.get_db() as conn:
        db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": "C15-OWNED",
            "side": "up", "direction": "yes", "target_contracts": 10,
            "filled_contracts": 10, "entry_limit_cents": 80, "cost_usd": 8.0,
            "client_order_id": "c15-1", "status": "filled", "network": "mainnet",
        })

    async def _live(limit=1000):
        return [_live_pos("C15-OWNED", qty=10, cur_price=0.85)]
    monkeypatch.setattr(trader, "get_positions", _live)
    monkeypatch.setattr(trader, "fetch_market", _open_market())

    summary, changed = run_async(trader.reconcile_positions_with_polymarket())

    assert summary["imported_unknowns"] == 0
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM bot_positions WHERE ticker='C15-OWNED'"
        ).fetchone()[0]
    assert n == 0   # never imported as an external duplicate


# ───────── balance cache (force vs cached read) ────────────────────────


def test_refresh_balance_caches_and_force_refreshes(monkeypatch):
    trader._balance_cache.clear()
    seq = iter([
        {"balance": 1000, "portfolio_value": 100},
        {"balance": 1200, "portfolio_value": 150},
    ])

    async def fake_balance():
        return next(seq)

    monkeypatch.setattr(trader, "get_balance", fake_balance)
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    cfg = {"balance_poll_interval": 60}

    # First forced read fetches the live balance.
    assert run_async(trader.refresh_balance(cfg, force=True)) == (1000, 100)
    # A non-forced read within the poll interval returns the cached value.
    assert run_async(trader.refresh_balance(cfg, force=False)) == (1000, 100)
    # A forced read fetches again (next upstream value).
    assert run_async(trader.refresh_balance(cfg, force=True)) == (1200, 150)


def test_refresh_balance_keeps_last_portfolio_when_value_read_fails(monkeypatch):
    # /value failure surfaces as portfolio_value=None — total must NOT collapse
    # to cash-only (which would show open positions as a phantom loss).
    trader._balance_cache.clear()
    seq = iter([
        {"balance": 5597, "portfolio_value": 454},   # healthy: cash + positions
        {"balance": 5135, "portfolio_value": None},   # /value down right after a fill
    ])

    async def fake_balance():
        return next(seq)

    monkeypatch.setattr(trader, "get_balance", fake_balance)
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    cfg = {"balance_poll_interval": 0}  # always re-fetch

    assert run_async(trader.refresh_balance(cfg, force=True)) == (5597, 454)
    # cash updates to the post-fill value, but portfolio is held at last-known
    # (454) instead of dropping to 0 → total stays sane, no phantom loss.
    assert run_async(trader.refresh_balance(cfg, force=True)) == (5135, 454)


def test_exposure_counts_committed_notional_of_submitted_orders(fresh_db, env_net):
    # A just-placed order sits 'submitted' with cost_usd=0; exposure must still
    # count its committed notional (target x limit) or a burst of placements in
    # one scan blows past the exposure / cash-reserve caps.
    seed_position(status="submitted", cost_usd=0.0, target_contracts=10, limit_price_cents=50)
    seed_position(status="filled", cost_usd=4.0, target_contracts=10, limit_price_cents=60)
    with db.get_db() as conn:
        exp = db.current_total_exposure_usd(conn, "mainnet")
    # 10*50c committed (submitted, cost still 0) + 4.00 real cost (filled) = 9.00
    assert exp == pytest.approx(9.0)


def test_flatten_sells_open_positions_and_books_pnl(fresh_db, env_net, cfg, monkeypatch):
    # Flatten must actually EXIT filled positions (market-sell + book realized
    # P&L), not just cancel resting orders.
    pid = seed_position(status="filled", direction="yes", filled_contracts=10,
                        cost_usd=5.0, limit_price_cents=50, order_id="o-buy")

    async def _quote(_ticker, _side):
        return {"bid_cents": 70, "ask_cents": 72}

    async def _place(**kw):
        assert kw["action"] == "sell" and kw["count"] == 10
        return {"order": {"order_id": "sell-1", "status": "live"}}

    async def _get_order(_oid):
        return {"order": {"size_matched": "10", "original_size": "10",
                          "price": "0.70", "status": "MATCHED"}}

    monkeypatch.setattr(trader, "get_quote", _quote)
    monkeypatch.setattr(trader, "place_limit_order", _place)
    monkeypatch.setattr(trader, "get_order", _get_order)

    res = run_async(trader.flatten_open_positions(cfg))

    assert res["sold"] == 10
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM bot_positions WHERE id=?", (pid,)).fetchone())
    assert r["resolved"] == 1 and r["closed_early"] == 1
    # 10*0.70 - 5.00 cost - entry fee (0.0695*0.5*0.5*10 = 0.17375) - crossing
    # sell's taker fee (0.0695*0.7*0.3*10 = 0.14595).
    assert r["pnl_usd"] == pytest.approx(1.6803)
    assert r["settlement_usd"] == pytest.approx(7.0)


# ───────── cancel_all_resting_orders: lifecycle cancel ────────────────


def test_cancel_all_resting_cancels_tracked_orders(fresh_db, env_net, cfg, monkeypatch):
    # Two app-tracked resting orders (a fully-resting submit + a partial),
    # plus rows that MUST be left alone: filled, resolved, and one with no
    # order_id. Only the two resting orders should be canceled on the book.
    resting = seed_position(status="submitted", order_id="OID-A", target_contracts=5)
    partial = seed_position(status="partial", order_id="OID-B",
                            target_contracts=8, filled_contracts=3)
    filled = seed_position(status="filled", order_id="OID-C", filled_contracts=10)
    no_oid = seed_position(status="submitted", order_id=None, target_contracts=4)
    # A resolved row that still (spuriously) reads 'submitted' must be excluded
    # by the resolved=0 guard.
    resolved = seed_position(status="submitted", order_id="OID-D", target_contracts=6)
    with db.get_db() as conn:
        conn.execute("UPDATE bot_positions SET resolved=1 WHERE id=?", (resolved,))

    cancels: list[str] = []

    async def _cancel_batch(ids):
        cancels.extend(ids)
        return {"canceled": set(ids), "not_canceled": {}}

    monkeypatch.setattr(trader, "cancel_orders", _cancel_batch)

    n = run_async(trader.cancel_all_resting_orders("shutdown"))

    assert n == 2
    assert sorted(cancels) == ["OID-A", "OID-B"]          # one batched request
    assert fetch(resting)["status"] == "canceled"
    assert fetch(partial)["status"] == "canceled"
    assert fetch(resting)["error"] == "shutdown"
    # Untouched rows keep their original status; no phantom cancels.
    assert fetch(filled)["status"] == "filled"
    assert fetch(no_oid)["status"] == "submitted"
    assert fetch(resolved)["status"] == "submitted"


def test_cancel_all_resting_survives_api_error_without_losing_fill(
    fresh_db, env_net, cfg, monkeypatch,
):
    # If the exchange refuses a cancel (order already off the book — e.g. it
    # just filled), the helper must NOT relabel the row canceled; leave it
    # for reconcile.
    a = seed_position(status="submitted", order_id="OID-A", target_contracts=5)
    b = seed_position(status="submitted", order_id="OID-B", target_contracts=5)

    async def _cancel_batch(ids):
        return {"canceled": {"OID-B"}, "not_canceled": {"OID-A": "order not found"}}

    monkeypatch.setattr(trader, "cancel_orders", _cancel_batch)

    n = run_async(trader.cancel_all_resting_orders("shutdown"))

    assert n == 1                                   # only OID-B succeeded
    assert fetch(a)["status"] == "submitted"        # 404 row left for reconcile
    assert fetch(b)["status"] == "canceled"


# ── maker/taker attribution on the main engine (audit High, site b) ──────
# `order_style='limit_mid'` is a shipped option whose orders REST, and a maker
# fill pays nothing under takerOnly fee-v2. _position_fee_usd has read
# `entry_maker` since the 2026-08-18 fee audit, but execute_signal never wrote
# the column, so the branch was dead and every rested fill was charged a taker
# fee it never paid — into pnl_usd and the loss breakers.


def _quote_stub(bid, ask):
    async def _q(_ticker, _side=None):
        return {"bid_cents": bid, "ask_cents": ask}
    return _q


def test_a_rested_limit_mid_entry_is_booked_as_a_maker(fresh_db, env_net, cfg,
                                                       monkeypatch):
    cfg["enable_trading"] = True
    cfg["order_style"] = "limit_mid"
    monkeypatch.setattr(trader, "get_quote", _quote_stub(58, 64))

    async def _place(**_kw):
        return {"order": {"order_id": "OID-MID", "status": "live"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(trader.execute_signal(
        whale_signal(id=301, ticker="MID"), "whale", cfg, 1000.0))
    assert row["limit_price_cents"] == 61          # (58+64)//2, under the ask
    assert row["entry_maker"] == 1
    filled = dict(row, filled_contracts=10, cost_usd=6.1,
                  avg_fill_price_cents=61)
    assert trader._position_fee_usd(filled) == 0.0


def test_a_crossing_entry_is_still_booked_as_a_taker(fresh_db, env_net, cfg,
                                                     monkeypatch):
    cfg["enable_trading"] = True
    cfg["order_style"] = "limit_cross"
    monkeypatch.setattr(trader, "get_quote", _quote_stub(58, 64))

    async def _place(**_kw):
        return {"order": {"order_id": "OID-X", "status": "matched"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(trader.execute_signal(
        whale_signal(id=302, ticker="CROSS"), "whale", cfg, 1000.0))
    assert row["limit_price_cents"] >= 64          # crosses the ask
    assert row["entry_maker"] == 0
    filled = dict(row, filled_contracts=10, cost_usd=6.6,
                  avg_fill_price_cents=66)
    assert trader._position_fee_usd(filled) > 0.0


def test_a_matched_response_overrides_a_maker_priced_order(fresh_db, env_net,
                                                           cfg, monkeypatch):
    """The book can move between quote and submit. If the exchange says the
    order MATCHED, it crossed — charge the fee whatever the quote implied."""
    cfg["enable_trading"] = True
    cfg["order_style"] = "limit_mid"
    monkeypatch.setattr(trader, "get_quote", _quote_stub(58, 64))

    async def _place(**_kw):
        return {"order": {"order_id": "OID-R", "status": "matched"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(trader.execute_signal(
        whale_signal(id=303, ticker="RACE"), "whale", cfg, 1000.0))
    assert row["entry_maker"] == 0


def test_an_unquotable_market_is_assumed_to_cross(fresh_db, env_net, cfg,
                                                  monkeypatch):
    """No book -> the price is a blind fallback cross. Assume taker: over-book
    the fee rather than flatter the ledger."""
    cfg["enable_trading"] = True
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)

    async def _place(**_kw):
        return {"order": {"order_id": "OID-N", "status": "resting"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)

    row = run_async(trader.execute_signal(
        whale_signal(id=304, ticker="NOBOOK"), "whale", cfg, 1000.0))
    assert row["entry_maker"] == 0


def test_a_paper_account_sends_nothing_from_the_main_engine(fresh_db, env_net, cfg, monkeypatch):
    """Account mode gate: enable_trading stays ON as the user set it, the
    account is Paper, and neither the scan nor execute sends an order."""
    cfg["enable_trading"] = True
    cfg["account_mode"] = "paper"
    monkeypatch.setattr(trader, "get_quote", _stub_no_quote)
    calls: list[dict] = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "OID-1", "status": "resting"}}
    monkeypatch.setattr(trader, "place_limit_order", _place)
    assert run_async(trader.scan_for_trades(cfg)) == []
    row = run_async(trader.execute_signal(whale_signal(id=901, ticker="P"), "whale", cfg, 1000.0))
    assert row is None and calls == [] and cfg["enable_trading"] is True
