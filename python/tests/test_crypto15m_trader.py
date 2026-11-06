"""Tests for the 15-min crypto executor (crypto15m_trader.py).

Pure decision helpers, plus a paper-mode lifecycle driven against a real
temp SQLite with `crypto15m.snapshot` and `polymarket_api.fetch_market`
stubbed (no network, no orders). Paper mode exercises the exact same
decision path as live — only fills are simulated.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import crypto15m
import crypto15m_trader as ct
import db
import polymarket_api
import trader
from config import merge_with_defaults


# ───────── fixtures ────────────────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "c15-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    # Per-position stop-loss retry throttle is keyed by row id, and ids restart
    # from 1 with every fresh DB — clear it so tests can't contaminate each other.
    ct._stop_retry_at.clear()
    return dbfile


@pytest.fixture
def env_net(monkeypatch):
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    return "mainnet"


@pytest.fixture
def cfg():
    c = merge_with_defaults({"account_mode": "live"})  # engines send only from a Live account
    c["network"] = "mainnet"
    c["crypto15m_enabled"] = True
    # Pin order size to 1 — these lifecycle tests assert single-contract fills
    # and P&L, independent of the shipped default (now 5, Polymarket's minimum).
    c["crypto15m_order_size"] = 1
    # Pin the legacy taker path: these lifecycle tests predate maker mode
    # (the shipped default) and assert instant crossed-fill behaviour.
    c["crypto15m_entry_style"] = "taker"
    return c


@pytest.fixture(autouse=True)
def _no_gamma(monkeypatch):
    """The resolution sweep looks every unsettled row's market up on Gamma.
    These test tickers don't exist there, so the answer is "not found" either
    way; give it without the round-trip. Tests that need a market stub it."""
    async def _not_found(_ticker):
        return None
    monkeypatch.setattr(polymarket_api, "fetch_market", _not_found)


def run_async(coro):
    return asyncio.run(coro)


def signal_asset(asset="BTC", favorite="up", entry_cost=0.86, signal=True, ticker="KXBTC15M-T1"):
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    up = 0.9 if favorite == "up" else 0.1
    return {
        "asset": asset, "series": f"KX{asset}15M", "spotUsd": 100.0,
        "open15mUsd": 100.0, "deltaUsd": 0.5, "hasMarket": True,
        "ticker": ticker, "closeTime": close, "minsLeft": 5.0,
        "upProb": up, "downProb": 1 - up,
        "favorite": favorite, "favoritePrice": 0.86, "entryCost": entry_cost,
        "yesBid": 0.85, "yesAsk": 0.87,
        "inWindow": True, "signal": signal, "openMarketCount": 1, "error": None,
    }


def _stub_snapshot(assets):
    async def _snap(_cfg):
        return {"assets": assets, "constants": {}, "fetchedAt": "",
                "spotOk": True, "spotSource": "stub"}
    return _snap


# ───────── pure decision helpers ───────────────────────────────────────


def test_direction_for_favorite():
    assert ct.direction_for_favorite("up") == "yes"
    assert ct.direction_for_favorite("down") == "no"


def test_entry_limit_cents_marks_up_and_clamps():
    assert ct.entry_limit_cents(0.86, 0.02) == 88
    assert ct.entry_limit_cents(0.98, 0.02) == 99  # 1.00 -> clamped to 99


def test_side_prob_from_market_respects_direction():
    m = {"yes_bid_dollars": 0.29, "yes_ask_dollars": 0.31}
    assert ct.side_prob_from_market(m, "yes") == pytest.approx(0.30)
    assert ct.side_prob_from_market(m, "no") == pytest.approx(0.70)
    assert ct.side_prob_from_market(None, "yes") is None


def test_should_enter_gate_matrix(cfg):
    a = signal_asset()
    assert ct.should_enter(a, cfg, has_open=False, open_count=0) == (True, "ok")
    assert ct.should_enter(a, cfg, has_open=True, open_count=0)[0] is False
    assert ct.should_enter(a, cfg, has_open=False, open_count=7)[0] is False
    assert ct.should_enter(signal_asset(signal=False), cfg, has_open=False, open_count=0) == (False, "no signal")

    off = dict(cfg)
    off["crypto15m_enabled"] = False
    assert ct.should_enter(a, off, has_open=False, open_count=0) == (False, "disabled")


def test_rsi_gate_is_direction_aware(cfg):
    cfg["crypto15m_min_rsi"] = 55.0
    up = signal_asset(favorite="up")   # buys Up -> needs RSI >= 55
    # no indicator data this tick -> fail closed (can't confirm)
    assert ct.should_enter(up, cfg, has_open=False, open_count=0)[0] is False
    assert ct.should_enter({**up, "rsi": 50.0}, cfg, has_open=False, open_count=0)[0] is False  # below
    assert ct.should_enter({**up, "rsi": 60.0}, cfg, has_open=False, open_count=0) == (True, "ok")
    # Down favorite buys Down -> MIRROR: needs RSI <= 100-55 = 45
    dn = signal_asset(favorite="down")
    assert ct.should_enter({**dn, "rsi": 40.0}, cfg, has_open=False, open_count=0) == (True, "ok")
    assert ct.should_enter({**dn, "rsi": 60.0}, cfg, has_open=False, open_count=0)[0] is False
    # 0 = off -> ignored even with an ugly RSI
    cfg["crypto15m_min_rsi"] = 0.0
    assert ct.should_enter({**up, "rsi": 5.0}, cfg, has_open=False, open_count=0) == (True, "ok")


def test_macd_gate_is_direction_aware_on_histogram(cfg):
    cfg["crypto15m_min_macd_hist"] = 0.5
    up = signal_asset(favorite="up")   # buys Up -> needs macdHist >= +0.5
    assert ct.should_enter(up, cfg, has_open=False, open_count=0)[0] is False                    # no data
    assert ct.should_enter({**up, "macdHist": 0.2}, cfg, has_open=False, open_count=0)[0] is False  # below
    assert ct.should_enter({**up, "macdHist": 1.0}, cfg, has_open=False, open_count=0) == (True, "ok")
    # Down favorite -> MIRROR: needs macdHist <= -0.5 (bearish)
    dn = signal_asset(favorite="down")
    assert ct.should_enter({**dn, "macdHist": -1.0}, cfg, has_open=False, open_count=0) == (True, "ok")
    assert ct.should_enter({**dn, "macdHist": -0.2}, cfg, has_open=False, open_count=0)[0] is False


def test_momentum_gate_validation_forces_detector_and_clamps():
    # A momentum floor forces indicator detection on (even if the user had it
    # off) and clamps RSI to 0–100; the macdHist magnitude is non-negative.
    c = merge_with_defaults({
        "crypto15mMinRsi": 150, "crypto15mMinMacdHist": 0.5,
        "crypto15mIndicatorDetect": False,
    })
    assert c["crypto15m_min_rsi"] == 100.0
    assert c["crypto15m_min_macd_hist"] == 0.5
    assert c["crypto15m_indicator_detect"] is True  # forced on by the floors
    # macdHist / macd remain composable rule fields
    from config import C15_RULE_FIELDS
    assert "macdHist" in C15_RULE_FIELDS and "macd" in C15_RULE_FIELDS


def test_evaluate_rules_and_of_conditions():
    a = signal_asset()  # favoritePrice 0.86, upProb 0.9, minsLeft 5
    # empty rule-set never enters
    assert ct.evaluate_rules(a, []) == (False, "no entry rules set")
    # single passing condition
    assert ct.evaluate_rules(a, [{"field": "favoritePrice", "op": ">=", "value": 0.8}])[0] is True
    # single failing condition
    ok, why = ct.evaluate_rules(a, [{"field": "favoritePrice", "op": ">=", "value": 0.9}])
    assert ok is False and "favoritePrice" in why
    # AND: all must hold — second fails -> reject
    assert ct.evaluate_rules(a, [
        {"field": "favoritePrice", "op": ">=", "value": 0.8},
        {"field": "minsLeft", "op": "<=", "value": 3},
    ])[0] is False
    # AND: both hold -> enter
    assert ct.evaluate_rules(a, [
        {"field": "favoritePrice", "op": ">=", "value": 0.8},
        {"field": "minsLeft", "op": "<=", "value": 8},
    ])[0] is True


def test_evaluate_rules_handles_missing_and_malformed():
    a = signal_asset()
    # field not present on the snapshot -> reject (don't trade on missing data)
    assert ct.evaluate_rules(a, [{"field": "arbEdgeCents", "op": ">=", "value": 2}]) == (
        False, "arbEdgeCents unavailable")
    # malformed condition is skipped, not fatal — only the valid one decides
    assert ct.evaluate_rules(a, [
        {"field": "favoritePrice"},                       # no op -> skipped
        {"field": "favoritePrice", "op": ">=", "value": 0.8},
    ])[0] is True


def test_should_enter_uses_rules_and_ignores_signal_flag(cfg):
    c = dict(cfg)
    c["crypto15m_use_rules"] = True
    c["crypto15m_rules"] = [{"field": "favoritePrice", "op": ">=", "value": 0.8}]
    # signal=False would normally block, but rules replace the favorite/signal gate
    a = signal_asset(signal=False)
    assert ct.should_enter(a, c, has_open=False, open_count=0)[0] is True
    # a still needs a favorite though
    nofav = signal_asset(signal=False)
    nofav["favorite"] = None
    assert ct.should_enter(nofav, c, has_open=False, open_count=0) == (False, "no favorite")
    # empty rule-set never trades even with the toggle on
    c["crypto15m_rules"] = []
    assert ct.should_enter(a, c, has_open=False, open_count=0) == (False, "no entry rules set")


def test_should_stop_loss(cfg):
    pos = {"status": "filled", "filled_contracts": 1}
    assert ct.should_stop_loss(pos, 0.30, cfg) is True   # below 0.40 exit
    assert ct.should_stop_loss(pos, 0.55, cfg) is False
    assert ct.should_stop_loss(pos, None, cfg) is False
    assert ct.should_stop_loss({"status": "submitted", "filled_contracts": 1}, 0.1, cfg) is False


def test_should_stop_loss_pct(cfg):
    # Entry: 10 contracts for $8.00 -> 80c/contract cost basis. Keep side_prob
    # ABOVE the default price stop (exit_threshold 0.40) so we isolate the % stop.
    pos = {"status": "filled", "filled_contracts": 10, "cost_usd": 8.00}

    cfg["crypto15m_stop_loss_pct"] = 0.0                    # off
    assert ct.should_stop_loss(pos, 0.45, cfg) is False     # down 43.75% but % stop OFF

    cfg["crypto15m_stop_loss_pct"] = 0.20                   # stop at -20% of entry
    assert ct.should_stop_loss(pos, 0.65, cfg) is False     # -18.75% -> not yet
    assert ct.should_stop_loss(pos, 0.63, cfg) is True      # -21.25% -> past the line
    assert ct.should_stop_loss(pos, 0.60, cfg) is True      # -25% -> stop

    # The cents/price stop still fires independently of the % stop.
    cfg["crypto15m_stop_loss_pct"] = 0.0
    assert ct.should_stop_loss(pos, 0.30, cfg) is True      # 30c < exit_threshold 40c

    # Guards.
    cfg["crypto15m_stop_loss_pct"] = 0.20
    assert ct.should_stop_loss(pos, None, cfg) is False
    assert ct.should_stop_loss(
        {"status": "filled", "filled_contracts": 0, "cost_usd": 8.0}, 0.60, cfg) is False

    # merge_with_defaults clamps it into the unit interval.
    assert merge_with_defaults({"crypto15m_stop_loss_pct": 9})["crypto15m_stop_loss_pct"] == 1.0


def test_stop_loss_pct_applies_across_timeframes(cfg):
    # The exit constants are global, so the % stop fires the same on 5m / 15m / 1h.
    pos = {"status": "filled", "filled_contracts": 10, "cost_usd": 8.00}
    cfg["crypto15m_stop_loss_pct"] = 0.20
    for interval in ("5m", "15m", "hourly"):
        cfg["crypto15m_interval"] = interval
        assert ct.should_stop_loss(pos, 0.60, cfg) is True, interval   # -25% -> stop


def test_should_take_profit_pct(cfg):
    # Entry: 10 contracts for $5.00 -> 50c/contract cost basis. Take-profit fires
    # on the REAL best bid once the whole position is worth >= pct above cost.
    pos = {"status": "filled", "filled_contracts": 10, "cost_usd": 5.00}

    cfg["crypto15m_take_profit_pct"] = 0.0                       # off
    assert ct.should_take_profit_pct(pos, 70, cfg) is False      # +40% but TP% OFF

    cfg["crypto15m_take_profit_pct"] = 0.20                      # cash out at +20%
    assert ct.should_take_profit_pct(pos, 55, cfg) is False      # +10% -> not yet
    assert ct.should_take_profit_pct(pos, 60, cfg) is True       # +20% -> exactly the line
    assert ct.should_take_profit_pct(pos, 66, cfg) is True       # +32% -> sell

    # Guards: no bid, wrong status, zero fill / cost.
    assert ct.should_take_profit_pct(pos, None, cfg) is False
    assert ct.should_take_profit_pct(
        {"status": "submitted", "filled_contracts": 10, "cost_usd": 5.0}, 90, cfg) is False
    assert ct.should_take_profit_pct(
        {"status": "filled", "filled_contracts": 0, "cost_usd": 5.0}, 90, cfg) is False
    assert ct.should_take_profit_pct(
        {"status": "filled", "filled_contracts": 10, "cost_usd": 0.0}, 90, cfg) is False

    # merge_with_defaults clamps it into the unit interval.
    assert merge_with_defaults({"crypto15m_take_profit_pct": 9})["crypto15m_take_profit_pct"] == 1.0


def test_take_profit_pct_applies_across_timeframes(cfg):
    # The exit constants are global, so the % take-profit fires the same on 5m/15m/1h.
    pos = {"status": "filled", "filled_contracts": 10, "cost_usd": 5.00}
    cfg["crypto15m_take_profit_pct"] = 0.20
    for interval in ("5m", "15m", "hourly"):
        cfg["crypto15m_interval"] = interval
        assert ct.should_take_profit_pct(pos, 66, cfg) is True, interval   # +32% -> sell


def test_directional_rules_pick_side(cfg):
    # Directional mode: YES-rules → buy up, NO-rules → buy down, neither → skip.
    cfg["crypto15m_use_rules"] = True
    cfg["crypto15m_rules"] = [{"field": "change5mPct", "op": ">", "value": 0.1}]
    cfg["crypto15m_rules_no"] = [{"field": "change5mPct", "op": "<", "value": -0.1}]
    assert ct._is_directional_rules(cfg) is True

    up = {"change5mPct": 0.3}
    down = {"change5mPct": -0.3}
    flat = {"change5mPct": 0.0}
    assert ct._directional_rules_side(up, cfg)[0] == "up"
    assert ct._directional_rules_side(down, cfg)[0] == "down"
    assert ct._directional_rules_side(flat, cfg)[0] is None
    # _entry_side agrees (so the order path buys the rule-selected side).
    assert ct._entry_side(up, cfg) == "up"
    assert ct._entry_side(down, cfg) == "down"


def test_directional_rules_gate_should_enter(cfg):
    cfg["crypto15m_enabled"] = True
    cfg["crypto15m_use_rules"] = True
    cfg["crypto15m_rules"] = [{"field": "change5mPct", "op": ">", "value": 0.1}]
    cfg["crypto15m_rules_no"] = [{"field": "change5mPct", "op": "<", "value": -0.1}]
    base = {"hasMarket": True, "favorite": "up", "minsLeft": 5.0,
            "upAsk": 0.55, "downAsk": 0.55, "hourUtc": 12}
    ok_up, _ = ct.should_enter({**base, "change5mPct": 0.3}, cfg, has_open=False, open_count=0)
    ok_dn, _ = ct.should_enter({**base, "change5mPct": -0.3}, cfg, has_open=False, open_count=0)
    ok_no, why = ct.should_enter({**base, "change5mPct": 0.0}, cfg, has_open=False, open_count=0)
    assert ok_up is True and ok_dn is True
    assert ok_no is False and "directional" in why


def test_turbine_import_maps_archetypes():
    import turbine_import as ti
    s = {"name": "T", "asset": "BTC", "archetype": "vwap_momentum",
         "indicators": {"lookbackMin": 5, "changePct": 0.1}, "priceBand": [0.25, 0.75], "size": 3}
    cfg = ti.import_strategy(s)
    assert cfg["crypto15m_use_rules"] is True
    assert cfg["crypto15m_assets"] == ["BTC"] and cfg["crypto15m_order_size"] == 3
    # yes-side = above VWAP AND positive momentum, gated into the price band
    fields = {c["field"] for c in cfg["crypto15m_rules"]}
    assert {"priceVsVwapPct", "change5mPct", "upAsk"} <= fields
    # stateful/both-side archetypes are skipped
    assert ti.import_strategy({"archetype": "panic_fade"}) is None
    # the shipped library imports cleanly
    imp, _ = ti.import_all()
    assert len(imp) >= 25


def test_compute_entry_contracts_fixed(cfg):
    # Fixed mode ignores balance and buys the configured order size.
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_order_size"] = 3
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=88, balance_usd=0, order_size=3) == 3
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=88, balance_usd=1000, order_size=3) == 3


def test_compute_entry_contracts_balance_pct(cfg):
    # 10% of $100 at 50c/contract -> 20 contracts; falls back to order
    # size when no balance is known.
    cfg["crypto15m_sizing_mode"] = "balance_pct"
    cfg["crypto15m_balance_pct"] = 0.10
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=50, balance_usd=100, order_size=1) == 20
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=50, balance_usd=0, order_size=4) == 4


def test_compute_entry_contracts_max_loss_cap(cfg):
    # A 5%-of-$100 cap at 50c funds 10 contracts max, regardless of mode.
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_max_loss_pct"] = 0.05
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=50, balance_usd=100, order_size=100) == 10
    # Budget can't fund even one contract -> 0 (caller skips the entry).
    assert ct.compute_entry_contracts(cfg, entry_limit_cents=90, balance_usd=10, order_size=5) == 0


def test_compute_entry_contracts_affordability_clamp(cfg):
    # Fixed mode, no max-loss: a single entry is now capped by the 25%-of-balance
    # backstop (Task 5) — tighter than the raw 98% affordability clamp — so one
    # bet can't swallow most of the account. 15 contracts at 99c would be $14.85;
    # 25% of $11.76 = $2.94 -> floor(2.94 / 0.99) = 2.
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_max_loss_pct"] = 0.0
    assert ct.compute_entry_contracts(
        cfg, entry_limit_cents=99, balance_usd=11.76, order_size=15) == 2
    # Can't afford even one contract -> skip.
    assert ct.compute_entry_contracts(
        cfg, entry_limit_cents=95, balance_usd=0.50, order_size=15) == 0
    # A balance that comfortably covers the full size (order_size well under 25%)
    # is left untouched.
    assert ct.compute_entry_contracts(
        cfg, entry_limit_cents=80, balance_usd=1000, order_size=15) == 15
    # No balance known (paper, no bankroll) -> fall back to fixed size, no clamp.
    assert ct.compute_entry_contracts(
        cfg, entry_limit_cents=99, balance_usd=0, order_size=15) == 15


# ───────── live executor lifecycle (no paper mode) ─────────────────────


def _capture_orders(monkeypatch):
    calls = []

    async def _place(**kw):
        calls.append(kw)
        return {"order": {"order_id": "ord-1", "status": "resting"}}

    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    return calls


def test_disabled_does_nothing(fresh_db, env_net, cfg, monkeypatch):
    cfg["crypto15m_enabled"] = False
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    out = run_async(ct.run_tick(cfg, authed=True))
    assert out == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "mainnet") == 0


def test_no_trade_without_auth(fresh_db, env_net, cfg, monkeypatch):
    # No paper mode: enabled but not connected = a complete no-op (can't place
    # real orders without a wallet).
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    out = run_async(ct.run_tick(cfg, authed=False))
    assert out == []
    assert calls == []
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "mainnet") == 0


def test_live_entry_places_real_order(fresh_db, env_net, cfg, monkeypatch):
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1 and calls[0]["action"] == "buy"
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "mainnet")[0]
    assert r["asset"] == "BTC" and r["direction"] == "yes"
    assert r["status"] == "submitted"           # real order, awaiting fill poll
    assert r["dry_run"] == 0
    assert r["entry_limit_cents"] == 88          # taker: 0.86 + 0.02 markup
    assert r["order_id"] == "ord-1"


def test_taker_order_type_default_and_toggle():
    assert ct.taker_order_type({}) == "FAK"                          # default on
    assert ct.taker_order_type({"crypto15m_taker_fak": True}) == "FAK"
    assert ct.taker_order_type({"crypto15m_taker_fak": False}) == "GTC"


def test_taker_entry_uses_fak(fresh_db, env_net, cfg, monkeypatch):
    # cfg default entry_style=taker + taker_fak on → marketable FAK (fill-now-or-kill)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert calls and calls[0]["order_type"] == "FAK"


def test_maker_entry_stays_gtc(fresh_db, env_net, cfg, monkeypatch):
    # A maker entry RESTS — it must remain GTC (FAK/FOK can't be post-only/resting).
    cfg["crypto15m_entry_style"] = "maker"
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert calls and calls[0]["order_type"] == "GTC"


def test_taker_fak_disabled_reverts_to_gtc(fresh_db, env_net, cfg, monkeypatch):
    cfg["crypto15m_taker_fak"] = False
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert calls and calls[0]["order_type"] == "GTC"


def test_independent_of_main_bot(fresh_db, env_net, cfg, monkeypatch):
    # The main bot's enable_trading switch must NOT gate the 15m executor —
    # the engine trades on its own crypto15m_enabled + auth.
    cfg["enable_trading"] = False
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "mainnet") == 1


def test_one_position_per_asset(fresh_db, env_net, cfg, monkeypatch):
    # Same signal two ticks running; the second must not double-enter.
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    _capture_orders(monkeypatch)

    async def _open(_ticker):  # mid-window, still favorite, not settled
        return {"yes_bid_dollars": 0.85, "yes_ask_dollars": 0.87, "status": "open", "result": ""}
    monkeypatch.setattr(polymarket_api, "fetch_market", _open)

    run_async(ct.run_tick(cfg, authed=True))
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "mainnet") == 1


def test_contrarian_mode_buys_the_underdog(fresh_db, env_net, cfg, monkeypatch):
    # Favorite is UP (favoritePrice 0.86) -> contrarian buys the cheap DOWN
    # (no) side at ~0.14 = 14c, plus the 2c entry markup = 16c.
    cfg["crypto15m_direction_mode"] = "contrarian"
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(favorite="up")]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert calls[0]["side"] == "no" and calls[0]["price_cents"] == 16
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "mainnet")[0]
    assert r["side"] == "down"
    assert r["direction"] == "no"
    assert r["entry_limit_cents"] == 16


def test_status_reports_enabled_and_authed(fresh_db, env_net, cfg, monkeypatch):
    async def _bal(_cfg, force=False):
        return 10_000, 0
    monkeypatch.setattr(trader, "refresh_balance", _bal)

    st = run_async(ct.status(cfg, authed=True))
    assert st["enabled"] is True
    assert st["trading"] is True       # enabled + connected = trading for real
    assert st["authed"] is True

    st = run_async(ct.status(cfg, authed=False))
    assert st["enabled"] is True
    assert st["trading"] is False      # no wallet -> not trading
    assert st["authed"] is False


# ───────── stuck-position lifecycle (slot leaks) ───────────────────────


def test_failed_entry_resolves_immediately_and_blocks_retry(fresh_db, env_net, cfg, monkeypatch):
    # Live entry fails (e.g. demo "exchange is paused" 409). The error row
    # must resolve at once (no leaked slot) and the SAME market must not
    # be retried next tick — but a new quarter (new ticker) may enter.
    cfg["crypto15m_live"] = True

    async def _paused(**kw):
        raise RuntimeError("HTTP 409: exchange is paused")
    monkeypatch.setattr(polymarket_api, "place_limit_order", _paused)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))

    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "mainnet") == 0  # slot freed
        r = dict(conn.execute("SELECT * FROM crypto15m_positions").fetchone())
    assert r["status"] == "error"
    assert r["resolved"] == 1

    # Same ticker again -> no second attempt, no new row.
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 1

    # Next quarter (new ticker) -> a fresh attempt is allowed.
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(ticker="KXBTC15M-T2")]))
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 2


def test_entry_with_unknown_outcome_is_tracked_by_its_order_id(fresh_db, env_net, cfg, monkeypatch):
    # The send failed ambiguously and a lookup could not find the order. It
    # may be live: an error row (resolved, no id) would forget it. A submitted
    # row with its id goes through the entry poll, which on a 404 books only
    # what the wallet holds.
    cfg["crypto15m_live"] = True
    oid = "0x" + "ef" * 32

    async def _lost(**kw):
        raise polymarket_api.OrderStatusUnknownError(oid, TimeoutError("read timed out"))
    monkeypatch.setattr(polymarket_api, "place_limit_order", _lost)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))

    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions").fetchone())
    assert (r["status"], r["order_id"], r["resolved"]) == ("submitted", oid, 0)


def test_exiting_position_settles_when_sell_never_fills(fresh_db, env_net, cfg, monkeypatch):
    # A stop-loss sell that never fills (empty book) must not pin the row
    # in 'exiting' forever: once the market settles, the still-held
    # contracts settle and the row resolves with the binary outcome.
    # A live filled YES position mid-exit (stop-loss sell resting, unfilled).
    pid = _seed_c15(
        asset="BTC", ticker="KXBTC15M-T1", direction="yes",
        status="exiting", filled_contracts=1, cost_usd=0.88,
        entry_limit_cents=88, exit_reason="stop_loss", exit_order_id="ord-x1",
    )["id"]

    canceled = []

    async def _order_unfilled(_kid):  # the sell shows zero fills
        return {"order": {"fill_count_fp": "0", "remaining_count_fp": "1"}}

    async def _cancel(kid):
        canceled.append(kid)

    async def _settled_no(_ticker):  # market settled AGAINST our YES side
        return {"result": "no", "status": "finalized"}

    monkeypatch.setattr(polymarket_api, "get_order", _order_unfilled)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "fetch_market", _settled_no)
    # No live signal this tick — we're testing the manage path only.
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        assert db.count_open_crypto15m(conn, "mainnet") == 0
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE id=?", (pid,)).fetchone())
    assert r["status"] == "settled"
    assert r["resolved"] == 1
    assert r["exit_reason"] == "stop_loss"
    assert r["outcome_correct"] == 0
    assert r["pnl_usd"] == pytest.approx(-0.88)  # lost the 88c cost
    assert canceled == ["ord-x1"]


def test_legacy_stuck_error_row_is_swept(fresh_db, env_net, cfg, monkeypatch):
    # Rows written before the fix: status='error', resolved=0. The manage
    # loop must resolve them so they stop occupying slots.
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BNB", "series": "KXBNB15M", "ticker": "KXBNB15M-OLD",
            "side": "up", "direction": "yes", "target_contracts": 12,
            "entry_limit_cents": 90, "client_order_id": "c1",
            "close_time": "2026-06-10T15:15:00Z", "confidence": 90,
            "network": "mainnet", "status": "error", "dry_run": False,
        })
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE id=?", (pid,)).fetchone())
    assert r["resolved"] == 1
    assert r["status"] == "error"


# ───────── maker entry mode ────────────────────────────────────────────


def test_maker_limit_cents_joins_the_bid():
    # up/yes joins the yes bid; down/no joins the no bid (= 1 - yes ask).
    assert ct.maker_limit_cents("up", 0.85, 0.87, 0.87) == 85
    assert ct.maker_limit_cents("down", 0.85, 0.87, 0.15) == 13
    # Unknown book falls back to 1c under the side's ask; clamped 1..99.
    assert ct.maker_limit_cents("up", None, None, 0.87) == 86
    assert ct.maker_limit_cents("up", None, None, 0.01) == 1


def test_live_maker_escalates_to_taker(fresh_db, env_net, cfg, monkeypatch):
    # Live: an unfilled resting maker at the cancel lead is canceled and crossed
    # as a taker at the current ask; the position fills on that taker order.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_entry_threshold"] = 0.5   # the 88c cross is still favored
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-L1", status="submitted", dry_run=False,
              target_contracts=2, entry_limit_cents=85, close_time=close, order_id="maker-1")

    state = {"canceled": False}

    async def _get_order(oid):
        if oid == "maker-1":
            if state["canceled"]:
                return _order(0, 0, 0.0, status="canceled")   # cancel took effect
            return _order(0, 2, 0.0, status="resting")        # maker never filled
        return _order(2, 0, 1.76, status="executed")          # taker fills 2 @ 88c

    async def _quote(_ticker, _side):
        return {"bid_cents": 86, "ask_cents": 88}

    async def _cancel(_oid):
        state["canceled"] = True
        return {"ok": True}

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "taker-1", "status": "executed"}}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert len(place_calls) == 1
    assert place_calls[0]["price_cents"] == 88 and place_calls[0]["count"] == 2
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "filled" and r["resolved"] == 0
    assert r["filled_contracts"] == 2
    assert r["cost_usd"] == pytest.approx(1.76)


def test_live_fast_escalate_before_cancel_lead(fresh_db, env_net, cfg, monkeypatch):
    # Fast escalate: a resting maker that's gone unfilled past
    # crypto15m_maker_fill_sec crosses to a taker fill NOW — even though the
    # cancel lead is far off (close is 14 min away). This is the fix for
    # "maker lags, price walks away, late higher-priced fill".
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_maker_cancel_min"] = 1.0      # cancel lead is far off
    cfg["crypto15m_maker_escalate"] = True
    cfg["crypto15m_maker_fill_sec"] = 20.0       # escalate after 20s unfilled
    cfg["crypto15m_entry_threshold"] = 0.5       # the 80c cross is still favored
    cfg["crypto15m_entry_max"] = 0.93
    close = (datetime.now(timezone.utc) + timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-FAST", status="submitted", dry_run=False,
              target_contracts=2, entry_limit_cents=78, close_time=close, order_id="maker-2")
    # Age the resting maker past the fast-fill window (created 60s ago).
    with db.get_db() as conn:
        conn.execute(
            "UPDATE crypto15m_positions SET created_at=datetime('now','-60 seconds') WHERE ticker=?",
            ("KXBTC15M-FAST",))

    state = {"canceled": False}

    async def _get_order(oid):
        if oid == "maker-2":
            if state["canceled"]:
                return _order(0, 0, 0.0, status="canceled")   # cancel took effect
            return _order(0, 2, 0.0, status="resting")        # maker still unfilled
        return _order(2, 0, 1.60, status="executed")          # taker fills 2 @ 80c

    async def _quote(_ticker, _side):
        return {"bid_cents": 78, "ask_cents": 80}

    async def _cancel(_oid):
        state["canceled"] = True
        return {"ok": True}

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "taker-2", "status": "executed"}}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert len(place_calls) == 1                            # crossed once (taker)
    assert place_calls[0]["price_cents"] == 80 and place_calls[0]["count"] == 2
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "filled" and r["resolved"] == 0
    assert r["filled_contracts"] == 2
    assert r["cost_usd"] == pytest.approx(1.60)


def _escalation_case(monkeypatch, cfg, *, ticker, taker_place, taker_get, wallet_after):
    """A maker that never filled, cancelled, then escalated to a taker cross at
    88c. `taker_place` is the place response's order dict, `taker_get` what
    get_order says about the taker (an Exception to raise, or a status), and
    `wallet_after` how many contracts the wallet holds once the taker is in."""
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_entry_threshold"] = 0.5   # the 88c cross is still favored
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    cfg["crypto15m_entry_max"] = 0.95
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker=ticker, status="submitted", dry_run=False,
              target_contracts=10, entry_limit_cents=80, close_time=close, order_id="maker-x")
    state = {"canceled": False, "placed": False}

    async def _get_order(oid):
        if oid == "maker-x":
            if state["canceled"]:
                return _order(0, 0, 0.0, status="canceled")      # cancel took effect
            return _order(0, 10, 0.0, status="resting")          # maker never filled
        if isinstance(taker_get, Exception):
            raise taker_get
        return {"order": taker_get}

    async def _quote(_t, _s):
        return {"bid_cents": 86, "ask_cents": 88}

    async def _cancel(_o):
        state["canceled"] = True
        return {"ok": True}

    async def _place(**kw):
        state["placed"] = True
        return {"order": taker_place}

    async def _positions(limit=500, user=None, **kw):
        n = wallet_after if state["placed"] else 0
        return [{"ticker": ticker, "position_fp": float(n)}] if n else []

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        return dict(conn.execute("SELECT * FROM crypto15m_positions WHERE ticker=?", (ticker,)).fetchone())


def test_live_escalate_404_books_what_the_wallet_holds(fresh_db, env_net, cfg, monkeypatch):
    # A marketable taker fills, then its order 404s (a fully-matched order
    # drops off the open-order endpoint). Booked from the wallet: 10 held.
    r = _escalation_case(monkeypatch, cfg, ticker="KXBTC15M-404",
                         taker_place={"order_id": "taker-x", "status": "live"},
                         taker_get=polymarket_api.PolymarketAPIError(404, "not found"), wallet_after=10)
    assert r["status"] == "filled" and r["resolved"] == 0   # NOT canceled/stranded
    assert r["filled_contracts"] == 10
    assert r["cost_usd"] == pytest.approx(8.80)              # 10 @ 88c (the cross)


def test_escalate_404_with_nothing_in_the_wallet_is_no_fill(fresh_db, env_net, cfg, monkeypatch):
    # A FAK that matched nothing also leaves the book (404). It used to be
    # booked as a full fill of contracts the wallet never held.
    r = _escalation_case(monkeypatch, cfg, ticker="KXBTC15M-404N",
                         taker_place={"order_id": "taker-x", "status": "live"},
                         taker_get=polymarket_api.PolymarketAPIError(404, "not found"), wallet_after=0)
    assert r["filled_contracts"] == 0
    assert r["status"] == "canceled"


def test_escalate_unmatched_fak_is_no_fill(fresh_db, env_net, cfg, monkeypatch):
    # place_limit_order's no-match FAK: no order id, status "unmatched" —
    # which contains "match". Audit repro booked 7 contracts at 96c from it.
    r = _escalation_case(monkeypatch, cfg, ticker="KXBTC15M-UNM",
                         taker_place={"order_id": "", "status": "unmatched"},
                         taker_get=polymarket_api.PolymarketAPIError(404, "not found"), wallet_after=0)
    assert r["filled_contracts"] == 0
    assert r["status"] == "canceled"


def test_escalate_partial_fak_books_only_the_part(fresh_db, env_net, cfg, monkeypatch):
    # "matched" with no order to poll: the FAK filled 4 of 10, the rest was killed.
    r = _escalation_case(monkeypatch, cfg, ticker="KXBTC15M-PART",
                         taker_place={"order_id": "", "status": "matched"},
                         taker_get=polymarket_api.PolymarketAPIError(404, "not found"), wallet_after=4)
    assert r["status"] == "filled"
    assert r["filled_contracts"] == 4
    assert r["cost_usd"] == pytest.approx(3.52)


def test_order_parsers_do_not_read_unmatched_as_a_fill():
    assert trader._parse_polymarket_order({"status": "unmatched", "size_matched": "0", "original_size": "5"})["status"] == "canceled"
    assert trader._parse_polymarket_order({"status": "ORDER_STATUS_MATCHED", "size_matched": "5", "original_size": "5"})["status"] == "executed"


def test_maker_full_fill_404_does_not_double_buy(fresh_db, env_net, cfg, monkeypatch):
    # H3: a resting maker that FULLY fills 404s on get_order. The poll confirms
    # the fill against the WALLET (on-chain holding), books it at its limit, and
    # NEVER lets escalation place a taker for the same contracts (2x-overspend).
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    cfg["crypto15m_maker_fill_sec"] = 1.0   # would fast-escalate if not booked
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-FF", status="submitted", dry_run=False,
              target_contracts=10, entry_limit_cents=80, close_time=close, order_id="maker-ff")
    with db.get_db() as conn:  # age past the fast-fill window
        conn.execute("UPDATE crypto15m_positions SET created_at=datetime('now','-60 seconds') WHERE ticker=?",
                     ("KXBTC15M-FF",))

    async def _get_order(_oid):
        raise polymarket_api.PolymarketAPIError(404, "fully matched")

    async def _positions(limit=500, user=None, **kw):
        # The wallet shows the maker actually filled (10 held) — what the 404 means.
        return [{"ticker": "KXBTC15M-FF", "position_fp": 10.0, "market_exposure_dollars": 8.0}]

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "x", "status": "live"}}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert place_calls == []        # maker already filled -> NO taker
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "filled" and r["resolved"] == 0
    assert r["filled_contracts"] == 10
    assert r["cost_usd"] == pytest.approx(8.0)   # 10 @ 80c maker limit


def test_entry_404_with_no_holding_marks_unfilled_not_phantom(fresh_db, env_net, cfg, monkeypatch):
    # Phantom-fill guard: a maker the exchange CANCELS at window close (or a FAK
    # that matched nothing) ALSO 404s on get_order. With nothing held on-chain it
    # must be booked UNFILLED (canceled) — never a fabricated full fill + P&L.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = False
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-NF", status="submitted", dry_run=False,
              target_contracts=10, entry_limit_cents=80, close_time=close, order_id="maker-nf")

    async def _get_order(_oid):
        raise polymarket_api.PolymarketAPIError(404, "not found")

    async def _positions(limit=500, user=None, **kw):
        return []   # wallet holds nothing — the order did NOT fill

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "x", "status": "live"}}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert place_calls == []        # nothing held -> no phantom taker either
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "canceled" and r["resolved"] == 1
    assert int(r["filled_contracts"] or 0) == 0
    assert r["pnl_usd"] is None                       # no fabricated P&L
    assert r["exit_reason"] == "unfilled_expired"


def test_escalate_respects_onchain_holding(fresh_db, env_net, cfg, monkeypatch):
    # H3 race: the maker reads as 'resting' but actually filled between poll and
    # cancel. The on-chain holding (get_positions) shows we hold the full target,
    # so escalation must buy NO taker on top.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    cfg["crypto15m_maker_fill_sec"] = 1.0
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-RACE", status="submitted", dry_run=False,
              target_contracts=10, entry_limit_cents=80, close_time=close, order_id="maker-r")
    with db.get_db() as conn:
        conn.execute("UPDATE crypto15m_positions SET created_at=datetime('now','-60 seconds') WHERE ticker=?",
                     ("KXBTC15M-RACE",))

    async def _get_order(_oid):
        return _order(0, 10, 0.0, status="resting")   # poll + re-read: still resting

    async def _cancel(_o):
        return {"ok": True}

    async def _positions(limit=500, user=None, **kw):
        return [{"ticker": "KXBTC15M-RACE", "position_fp": 10.0, "market_exposure_dollars": 8.0}]

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "t", "status": "live"}}

    async def _quote(_t, _s):
        return {"bid_cents": 86, "ask_cents": 88}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert place_calls == []        # already hold 10 on-chain -> no taker
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "filled"
    assert r["filled_contracts"] == 10
    assert r["cost_usd"] == pytest.approx(8.0)   # booked at the maker limit


def test_escalate_defers_when_cancel_unconfirmed(fresh_db, env_net, cfg, monkeypatch):
    # M7: the cancel doesn't take effect and the maker is STILL resting on an OPEN
    # market — don't stack a taker (both could fill = 2x buy). Defer to next tick.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    cfg["crypto15m_maker_fill_sec"] = 1.0
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-STUCK", status="submitted", dry_run=False,
              target_contracts=10, entry_limit_cents=80, close_time=close, order_id="maker-s")
    with db.get_db() as conn:
        conn.execute("UPDATE crypto15m_positions SET created_at=datetime('now','-60 seconds') WHERE ticker=?",
                     ("KXBTC15M-STUCK",))

    async def _get_order(_oid):
        return _order(0, 10, 0.0, status="resting")   # cancel never takes effect

    async def _cancel(_o):
        return {"ok": False}

    async def _positions(limit=500, user=None, **kw):
        return []

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "t", "status": "live"}}

    async def _quote(_t, _s):
        return {"bid_cents": 78, "ask_cents": 80}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert place_calls == []        # NO taker on the still-resting maker
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "submitted" and r["resolved"] == 0   # deferred, retry next tick


def test_rules_still_respect_trading_hours(cfg, monkeypatch):
    # M5: custom rules replace the favorite/signal EDGE but NOT the hard
    # trading-hours gate (which the rule vocabulary can't express as a wrapping
    # window). Outside hours, should_enter blocks even with rules on.
    cfg["crypto15m_use_rules"] = True
    cfg["crypto15m_rules"] = []
    monkeypatch.setattr(crypto15m, "hours_ok", lambda _cfg, hour=None: False)
    ok, why = ct.should_enter(signal_asset(), cfg, has_open=False, open_count=0)
    assert ok is False and "hours" in why


def test_no_maker_entry_inside_cancel_lead(cfg):
    # Fix: don't place a maker order already inside its cancel lead (it would be
    # born-expired and, repeated each tick, floods the book — the cancel-spam).
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_maker_cancel_min"] = 6.0
    a = signal_asset()  # minsLeft 5.0 -> inside a 6-min lead
    ok, why = ct.should_enter(a, cfg, has_open=False, open_count=0)
    assert ok is False and "cancel lead" in why
    # taker crosses the spread (fills at once) -> exempt
    cfg["crypto15m_entry_style"] = "taker"
    assert ct.should_enter(a, cfg, has_open=False, open_count=0)[0] is True
    # maker with room to fill is allowed
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_maker_cancel_min"] = 1.0
    assert ct.should_enter(a, cfg, has_open=False, open_count=0)[0] is True


def test_no_reentry_on_already_attempted_market(fresh_db, env_net, cfg, monkeypatch):
    # Fix: once we've placed an entry on a market this window, never re-enter it
    # — a canceled/unfilled maker used to free the asset and get retried next
    # tick on the SAME market.
    cfg["crypto15m_entry_style"] = "taker"  # isolate the attempted-ticker gate
    seeded = _seed_c15(asset="BTC", ticker="KXBTC15M-W1", status="canceled",
                       exit_reason="unfilled_expired", dry_run=True)
    with db.get_db() as conn:
        db.update_crypto15m_position(conn, seeded["id"], resolved=1)
        assert "KXBTC15M-W1" in db.crypto15m_attempted_tickers(conn, "mainnet")
    # the snapshot still offers the same hot market
    monkeypatch.setattr(crypto15m, "snapshot",
                        _stub_snapshot([signal_asset(ticker="KXBTC15M-W1")]))
    run_async(ct.run_tick(cfg, authed=False))
    with db.get_db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM crypto15m_positions WHERE ticker='KXBTC15M-W1'"
        ).fetchone()["c"]
    assert n == 1  # no second entry placed on the already-attempted market


def test_attempted_tickers_age_capped(fresh_db):
    # The attempted-ticker guard only protects the CURRENT 15-min window (each
    # window is a fresh ticker/conditionId), so the query is age-capped — it
    # must not scan/return months of position history on every 4s tick.
    old = _seed_c15(ticker="KXBTC15M-OLD", status="canceled", dry_run=True)
    _seed_c15(asset="ETH", series="KXETH15M", ticker="KXETH15M-NEW",
              status="canceled", dry_run=True)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE crypto15m_positions SET created_at=datetime('now','-7 hours') WHERE id=?",
            (old["id"],),
        )
        got = db.crypto15m_attempted_tickers(conn, "mainnet")
    assert "KXETH15M-NEW" in got          # fresh row still guards its window
    assert "KXBTC15M-OLD" not in got      # hours-old row aged out of the guard


def test_stop_loss_retry_throttled(fresh_db, env_net, cfg, monkeypatch):
    # A stop-loss sell that errors leaves the row 'filled' — without a throttle
    # the 4s manage tick re-fires the exit attempt forever. First attempt must
    # be immediate; retries wait out _STOP_RETRY_SEC (mirrors the copy engine).
    pos = {"id": 777, "ticker": "KXBTC15M-SL", "direction": "yes", "asset": "BTC",
           "status": "filled", "filled_contracts": 10, "cost_usd": 8.0, "dry_run": 0}

    async def _mkt(_ticker):  # open market, our side at ~30c -> below the 40c stop
        return {"yes_bid_dollars": 0.28, "yes_ask_dollars": 0.32, "status": "open", "result": ""}
    monkeypatch.setattr(polymarket_api, "fetch_market", _mkt)

    async def _no_chain(_pos):
        return None
    monkeypatch.setattr(ct, "_resolved_payout_from_chain", _no_chain)

    async def _no_bid(_ticker, _direction):
        return None
    monkeypatch.setattr(ct, "_best_bid_cents", _no_bid)

    calls = []

    async def _fake_stop(p, market, c):
        calls.append(p["id"])
        return None  # the sell attempt errored / didn't resolve the row
    monkeypatch.setattr(ct, "_place_stop_loss", _fake_stop)

    run_async(ct._manage_position(pos, cfg, "mainnet"))
    assert calls == [777]                   # first attempt fires immediately
    run_async(ct._manage_position(pos, cfg, "mainnet"))
    assert calls == [777]                   # retry inside the window is throttled
    ct._stop_retry_at[777] -= ct._STOP_RETRY_SEC + 1   # window elapsed
    run_async(ct._manage_position(pos, cfg, "mainnet"))
    assert calls == [777, 777]              # then the retry goes through


def test_live_maker_entry_places_resting_order_at_bid(fresh_db, env_net, cfg, monkeypatch):
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["price_cents"] == 85  # at the bid, NOT ask + markup
    assert calls[0]["action"] == "buy"


def test_live_entry_skips_when_real_ask_above_cap(fresh_db, env_net, cfg, monkeypatch):
    # Stale Gamma price let the bot enter favorites already past entry_max; the
    # real CLOB ask now gates them out BEFORE any order is placed (no cancel churn).
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_entry_max"] = 0.95
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    async def _hot(_t, _s):
        return {"bid_cents": 95, "ask_cents": 97}   # real favorite already at 97c

    monkeypatch.setattr(polymarket_api, "get_quote", _hot)
    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 0   # nothing placed
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "canceled" and r["resolved"] == 1
    assert r["exit_reason"] == "above_cap"


def test_live_maker_entry_skips_when_real_bid_above_cap_no_ask(fresh_db, env_net, cfg, monkeypatch):
    # The HYPE-97c bug: a one-sided book (real bid present, NO ask) used to slip
    # past the ask-only gate, then the maker re-priced to the above-cap bid and
    # FILLED there. The submit-price cap must now skip it instead.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_entry_max"] = 0.95
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    async def _one_sided(_t, _s):
        return {"bid_cents": 97, "ask_cents": None}   # bid past cap, no ask quoted

    monkeypatch.setattr(polymarket_api, "get_quote", _one_sided)
    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 0   # nothing placed — no fill at 97c
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "canceled" and r["resolved"] == 1
    assert r["exit_reason"] == "above_cap"


def test_live_maker_entry_places_when_real_bid_within_cap(fresh_db, env_net, cfg, monkeypatch):
    # Counterpart: a real bid AT the cap is still a legal entry — the maker rests
    # at the cap price and the cap guard must NOT skip it.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_entry_max"] = 0.95
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    async def _at_cap(_t, _s):
        return {"bid_cents": 95, "ask_cents": None}

    monkeypatch.setattr(polymarket_api, "get_quote", _at_cap)
    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["price_cents"] == 95   # rests exactly at the cap, not above


def test_favorite_floor_skips_crashed_favorite(fresh_db, env_net, cfg, monkeypatch):
    # The "entered at 5c, lost" bug: the snapshot favored UP (a favorite), but by
    # entry time UP has CRASHED to ~5c. Favorite mode must NOT chase it down —
    # the live price is far below entry_threshold, so skip instead of buying the
    # now-underdog.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_direction_mode"] = "favorite"
    cfg["crypto15m_entry_threshold"] = 0.60   # want a >=60% favorite
    cfg["crypto15m_entry_max"] = 0.98
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(favorite="up")]))
    calls = _capture_orders(monkeypatch)

    async def _crashed(_t, _s):
        return {"bid_cents": 4, "ask_cents": 6}   # UP collapsed to ~5c since snapshot

    monkeypatch.setattr(polymarket_api, "get_quote", _crashed)
    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []   # never bought the crashed favorite
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "canceled" and r["resolved"] == 1
    assert r["exit_reason"] == "favorite_flipped"


def test_favorite_floor_exempts_contrarian(fresh_db, env_net, cfg, monkeypatch):
    # Contrarian INTENDS to buy the cheap side, so the favorite-floor must not
    # apply — a ~5c entry on the underdog is the whole point.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_direction_mode"] = "contrarian"
    cfg["crypto15m_entry_threshold"] = 0.60
    cfg["crypto15m_entry_max"] = 0.98
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset(favorite="up")]))
    calls = _capture_orders(monkeypatch)

    async def _cheap(_t, _s):
        return {"bid_cents": 5, "ask_cents": 7}   # buying the cheap DOWN/underdog

    monkeypatch.setattr(polymarket_api, "get_quote", _cheap)
    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1   # contrarian still enters the cheap side
    assert calls[0]["side"] == "no"   # favorite was up -> contrarian buys down/no


def test_escalate_skips_when_favorite_crashed(fresh_db, env_net, cfg, monkeypatch):
    # Escalation sibling of the favorite-floor: don't COMPLETE an entry by
    # crossing into a side that crashed below entry_threshold since the maker.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_direction_mode"] = "favorite"
    cfg["crypto15m_entry_threshold"] = 0.60
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-CR", status="submitted", dry_run=False,
              target_contracts=10, entry_limit_cents=70, close_time=close, order_id="maker-cr")

    state = {"canceled": False}

    async def _get_order(_oid):
        return _order(0, 0, 0.0, status="canceled") if state["canceled"] \
            else _order(0, 10, 0.0, status="resting")

    async def _cancel(_o):
        state["canceled"] = True
        return {"ok": True}

    async def _positions(limit=500, user=None, **kw):
        return []

    async def _quote(_t, _s):
        return {"bid_cents": 4, "ask_cents": 6}   # favorite crashed to ~5c

    place_calls = []

    async def _place(**kw):
        place_calls.append(kw)
        return {"order": {"order_id": "t"}}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    assert place_calls == []   # never crossed into the crashed favorite
    with db.get_db() as conn:
        r = dict(conn.execute("SELECT * FROM crypto15m_positions WHERE asset='BTC'").fetchone())
    assert r["status"] == "canceled" and r["resolved"] == 1
    assert r["exit_reason"] == "unfilled_expired"


def test_live_entries_never_oversubscribe_the_balance(fresh_db, env_net, cfg, monkeypatch):
    # The 'not enough balance' wall: fixed order_size of 15 used to fire a
    # full-size order on EVERY asset, each rejected for insufficient funds. Now
    # the 25%-of-balance backstop clamps each entry below the full size and the
    # in-tick budget squeezes the second asset further — total committed stays
    # comfortably within the balance instead of N × full-size.
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_live"] = True
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_order_size"] = 15
    cfg["crypto15m_max_loss_pct"] = 0.0

    async def _bal(_cfg, force=False):
        return 6000, 0   # $60.00 cash
    monkeypatch.setattr(trader, "refresh_balance", _bal)

    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([
        signal_asset(asset="BTC", ticker="KXBTC15M-T1"),
        signal_asset(asset="ETH", ticker="KXETH15M-T1"),
    ]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    # First order clamped below the full size; total collateral within balance.
    assert calls, "expected at least one entry"
    assert calls[0]["count"] < 15
    committed = sum(c["count"] * c["price_cents"] / 100.0 for c in calls)
    assert committed <= 60.0 + 1e-6
    # The second asset is squeezed by the remaining budget, not a full clone.
    if len(calls) == 2:
        assert calls[1]["count"] <= calls[0]["count"]


# ───────── partial-fill handling (entry + stop-loss) ───────────────────
#
# Thin 15-min books fill in pieces. The executor must never (a) finalize an
# entry on a partial and strand the rest of the order, nor (b) resolve a
# position on a partial stop-loss sell and strand the unsold contracts.

_c15_seq = 0


def _seed_c15(**over) -> dict:
    """Insert one crypto15m position row directly, return the fetched dict.
    insert_crypto15m_position can't write the exit_* columns, so those are
    applied via a follow-up update."""
    global _c15_seq
    _c15_seq += 1
    n = _c15_seq
    row = {
        "asset": over.get("asset", "BTC"),
        "series": over.get("series", "KXBTC15M"),
        "ticker": over.get("ticker", f"KXBTC15M-T{n}"),
        "side": over.get("side", "up"),
        "direction": over.get("direction", "yes"),
        "target_contracts": over.get("target_contracts", 10),
        "filled_contracts": over.get("filled_contracts", 0),
        "cost_usd": over.get("cost_usd", 0.0),
        "entry_limit_cents": over.get("entry_limit_cents", 88),
        "client_order_id": f"co-{n}",
        "order_id": over.get("order_id"),
        "status": over.get("status", "submitted"),
        "exit_reason": over.get("exit_reason"),
        "close_time": over.get("close_time", ""),
        "network": over.get("network", "mainnet"),
        "dry_run": over.get("dry_run", False),
        "entry_order_type": over.get("entry_order_type"),
    }
    post = {k: over[k] for k in
            ("exit_order_id", "exit_filled_contracts", "proceeds_usd",
             "avg_entry_cents", "exit_limit_cents")
            if k in over}
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, row)
        if post:
            db.update_crypto15m_position(conn, pid, **post)
        return db.fetch_crypto15m_by_id(conn, pid)


def _order(filled, remaining, cost_dollars, status="resting") -> dict:
    """Build a CLOB OpenOrder mock. `cost_dollars` is the cumulative fill
    value; we back out the per-share price the parser multiplies by."""
    f = float(filled)
    price = (float(cost_dollars) / f) if f else 0.0
    clob_status = {"resting": "LIVE", "executed": "MATCHED"}.get(status, status)
    return {"order": {
        "size_matched": str(filled),
        "original_size": str(int(filled) + int(remaining)),
        "price": f"{price:.6f}",
        "status": clob_status,
    }}


def _future():
    return (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past():
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_partial_entry_keeps_polling_then_completes(fresh_db, env_net, cfg, monkeypatch):
    pos = _seed_c15(status="submitted", order_id="OID-E",
                    target_contracts=10, filled_contracts=0, close_time=_future())

    async def _get3(_kid):
        return _order(3, 7, 2.64)  # 3 of 10 filled @ 88c, 7 still resting
    monkeypatch.setattr(polymarket_api, "get_order", _get3)

    out = run_async(ct._poll_entry(pos, cfg))
    assert out is None  # still resting -> NOT finalized
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "submitted" and r["resolved"] == 0
    assert r["filled_contracts"] == 3                 # progress recorded
    assert r["cost_usd"] == pytest.approx(2.64)

    async def _get10(_kid):
        return _order(10, 0, 8.80, status="executed")  # the rest fills
    monkeypatch.setattr(polymarket_api, "get_order", _get10)
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    out = run_async(ct._poll_entry(r, cfg))
    assert out["status"] == "filled"
    assert out["filled_contracts"] == 10              # full count, not stuck at 3
    assert out["cost_usd"] == pytest.approx(8.80)


def test_partial_entry_then_expiry_keeps_filled_portion(fresh_db, env_net, cfg, monkeypatch):
    pos = _seed_c15(status="submitted", order_id="OID-E", target_contracts=10,
                    filled_contracts=3, cost_usd=2.64, close_time=_past())
    canceled = {"v": False}

    async def _cancel(_kid):
        canceled["v"] = True

    async def _get(_kid):
        return _order(3, 7, 2.64)  # 3 filled, remainder still resting at expiry
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "get_order", _get)

    out = run_async(ct._poll_entry(pos, cfg))
    assert canceled["v"] is True            # remainder canceled
    assert out["status"] == "filled"        # kept as a real (smaller) position
    assert out["filled_contracts"] == 3
    assert out["resolved"] == 0             # NOT discarded as 'canceled'
    assert out["exit_reason"] is None


def test_unfilled_entry_then_expiry_still_cancels(fresh_db, env_net, cfg, monkeypatch):
    # Regression: a truly-unfilled entry at expiry must still resolve as
    # canceled (the new partial-aware path must not break this).
    pos = _seed_c15(status="submitted", order_id="OID-E",
                    target_contracts=10, filled_contracts=0, close_time=_past())

    async def _cancel(_kid):
        pass

    async def _get(_kid):
        return _order(0, 10, 0)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "get_order", _get)

    out = run_async(ct._poll_entry(pos, cfg))
    assert out["status"] == "canceled"
    assert out["resolved"] == 1
    assert out["exit_reason"] == "unfilled_expired"


def test_exit_404_books_sale_not_settle_as_held(fresh_db, env_net, cfg, monkeypatch):
    # Regression (audit HIGH): a fully-filled stop-loss SELL drops off the open-
    # order endpoint (404). _poll_exit must reconcile against the wallet and BOOK
    # the sale as a loss — NOT swallow the 404 and let _settle_if_closed later
    # settle the already-sold contracts as a held settlement WIN.
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.00, exit_reason="stop_loss",
                    exit_order_id="OID-X", exit_limit_cents=20, exit_filled_contracts=0)

    async def _boom(_kid):
        raise polymarket_api.PolymarketAPIError(404, "order not found (fully matched)")

    async def _no_positions(*a, **k):
        return []   # wallet flat -> the whole position sold

    monkeypatch.setattr(polymarket_api, "get_order", _boom)
    monkeypatch.setattr(polymarket_api, "get_positions", _no_positions)

    out = run_async(ct._poll_exit(pos))
    assert out is not None and out["status"] == "exited"
    assert out["exit_filled_contracts"] == 10
    assert out["proceeds_usd"] == pytest.approx(2.0)    # 10 sold @ 20c
    # 2.0 - 8.0 gross, minus the exit taker fee the crossing sell paid
    # (0.0695 * 0.20 * 0.80 * 10 = $0.1112) -> a real fee-net LOSS.
    assert out["pnl_usd"] == pytest.approx(-6.0 - 0.1112)
    assert out["outcome_correct"] == 0


def test_partial_stop_loss_sell_stays_exiting(fresh_db, env_net, cfg, monkeypatch):
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80,
                    exit_order_id="OID-X", exit_filled_contracts=0)

    async def _get(_kid):
        return _order(3, 7, 0.93)  # sold 3 of 10, 7 still held
    monkeypatch.setattr(polymarket_api, "get_order", _get)

    out = run_async(ct._poll_exit(pos))
    assert out is None  # partial sell -> NOT resolved (would strand 7 contracts)
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "exiting" and r["resolved"] == 0
    assert r["exit_filled_contracts"] == 3
    assert r["proceeds_usd"] == pytest.approx(0.93)


def test_partial_stop_then_settlement_accounts_for_sold_portion(fresh_db, env_net, cfg, monkeypatch):
    # 10 held @ 0.88 = $8.80. Stop-loss already sold 3 for $0.93. The market
    # then settles YES (our side wins) -> only the 7 RESIDUAL contracts pay
    # out; the proceeds from the 3 already sold are credited; cost counted
    # once. (Old code settled all 10 and double-counted the sold 3.)
    pos = _seed_c15(status="exiting", direction="yes", target_contracts=10,
                    filled_contracts=10, cost_usd=8.80, exit_reason="stop_loss",
                    exit_order_id="OID-X", exit_filled_contracts=3, proceeds_usd=0.93)

    async def _settled(_ticker):
        return {"result": "yes", "status": "finalized"}
    monkeypatch.setattr(polymarket_api, "fetch_market", _settled)

    out = run_async(ct._settle_if_closed(pos))
    assert out["status"] == "settled" and out["resolved"] == 1
    assert out["settlement_usd"] == pytest.approx(7.0)          # 7 residual * $1, NOT 10
    # partial proceeds + residual settlement - cost, minus the synthesized
    # fee on the partial sale (3 sold @ 31c: 0.0695 * 0.31 * 0.69 * 3).
    assert out["pnl_usd"] == pytest.approx(0.93 + 7.0 - 8.80 - 0.0695 * 0.31 * 0.69 * 3)
    assert out["outcome_correct"] == 1


# ───────── fee-v2 maker/taker split (2026-08-18 audit) ─────────────────
#
# takerOnly fee-v2: only fills that CROSS the book pay. A resting GTC entry
# that fills is a maker fill (fee 0); the default entry style IS maker, so
# charging every fill the taker fee understated recorded P&L. exit_reason
# 'sell_strength' is the one resting (maker) exit path.


def test_order_fee_maker_fills_pay_nothing():
    taker = ct._order_fee_usd({}, 10, 50.0)
    assert taker == pytest.approx(0.0695 * 0.5 * 0.5 * 10)
    assert ct._order_fee_usd({}, 10, 50.0, maker=True) == 0.0


def test_entry_is_maker_reads_the_stamped_order_type():
    assert ct._entry_is_maker({"entry_order_type": "GTC"}) is True
    assert ct._entry_is_maker({"entry_order_type": "FAK"}) is False
    # Legacy rows (no stamp) keep their original taker-fee accounting.
    assert ct._entry_is_maker({}) is False
    assert ct._entry_is_maker({"entry_order_type": None}) is False


def test_exit_is_maker_only_for_sell_strength():
    assert ct._exit_is_maker({"exit_reason": "sell_strength"}) is True
    assert ct._exit_is_maker({"exit_reason": "stop_loss"}) is False
    assert ct._exit_is_maker({"exit_reason": "take_profit"}) is False
    assert ct._exit_is_maker({}) is False


def test_gtc_entry_fill_books_zero_fee(fresh_db, env_net, cfg, monkeypatch):
    # A resting maker (GTC) entry that fully fills must book fees_usd = 0;
    # the same fill on a FAK row books the taker fee.
    for ot, want_fee in (("GTC", 0.0), ("FAK", 0.0695 * 0.88 * 0.12 * 10)):
        pos = _seed_c15(status="submitted", order_id=f"OID-{ot}",
                        target_contracts=10, entry_order_type=ot)

        async def _get(_kid):
            return _order(10, 0, 8.80)  # filled 10, none resting, $8.80 cost

        async def _fills(_kid, fallback, filled, _pos=None):
            return 88.0, 8.80
        monkeypatch.setattr(polymarket_api, "get_order", _get)
        monkeypatch.setattr(ct, "_real_fill_cents", _fills)

        out = run_async(ct._poll_entry(pos, cfg))
        assert out is not None and out["status"] == "filled"
        assert out["fees_usd"] == pytest.approx(want_fee), ot


# ───────── UTC trading-hours filter ────────────────────────────────────


def test_hours_ok_default_is_always_on(cfg):
    for h in range(24):
        assert crypto15m.hours_ok(cfg, hour=h) is True


def test_hours_ok_simple_window(cfg):
    cfg["crypto15m_hours_start_utc"] = 6
    cfg["crypto15m_hours_end_utc"] = 12
    assert crypto15m.hours_ok(cfg, hour=6) is True
    assert crypto15m.hours_ok(cfg, hour=11) is True
    assert crypto15m.hours_ok(cfg, hour=12) is False
    assert crypto15m.hours_ok(cfg, hour=23) is False


def test_hours_ok_overnight_wrap(cfg):
    cfg["crypto15m_hours_start_utc"] = 22
    cfg["crypto15m_hours_end_utc"] = 6
    assert crypto15m.hours_ok(cfg, hour=23) is True
    assert crypto15m.hours_ok(cfg, hour=2) is True
    assert crypto15m.hours_ok(cfg, hour=6) is False
    assert crypto15m.hours_ok(cfg, hour=12) is False


# ───────── timing + cross-asset correlation rule fields ────────────────


def test_apply_cross_asset_agreement_and_bias():
    now = datetime(2026, 6, 26, 13, 30, tzinfo=timezone.utc).timestamp()
    assets = [
        {"asset": "BTC", "hasMarket": True, "favorite": "up"},
        {"asset": "ETH", "hasMarket": True, "favorite": "up"},
        {"asset": "SOL", "hasMarket": True, "favorite": "up"},
        {"asset": "XRP", "hasMarket": True, "favorite": "down"},
        {"asset": "DOGE", "hasMarket": False, "favorite": None},
    ]
    crypto15m._apply_cross_asset(assets, now)
    btc, xrp, doge = assets[0], assets[3], assets[4]
    # 3 up, 1 down -> bias (3-1)/4 = 0.5, same for everyone
    assert btc["marketBias"] == 0.5 and xrp["marketBias"] == 0.5
    assert btc["hourUtc"] == 13
    # BTC(up): 2 of the other 3 favorited coins agree -> 2/3
    assert btc["peersAgree"] == pytest.approx(0.6667, abs=1e-3)
    # XRP(down): none of the others agree -> 0
    assert xrp["peersAgree"] == 0.0
    # no-market asset has no peer view, but still gets the timing/bias gauges
    assert doge["peersAgree"] is None and doge["hourUtc"] == 13


def test_apply_cross_asset_singleton_has_no_peers():
    now = datetime(2026, 6, 26, 5, 0, tzinfo=timezone.utc).timestamp()
    assets = [{"asset": "BTC", "hasMarket": True, "favorite": "up"}]
    crypto15m._apply_cross_asset(assets, now)
    assert assets[0]["peersAgree"] is None      # nobody to agree with
    assert assets[0]["marketBias"] == 1.0        # the only favorite is up
    assert assets[0]["hourUtc"] == 5


def test_should_enter_rules_can_gate_on_correlation_and_timing(cfg):
    cfg["crypto15m_use_rules"] = True
    cfg["crypto15m_rules"] = [
        {"field": "peersAgree", "op": ">=", "value": 0.6},
        {"field": "hourUtc", "op": ">=", "value": 13},
    ]
    a = signal_asset()
    a.update({"peersAgree": 0.8, "hourUtc": 14})
    assert ct.should_enter(a, cfg, has_open=False, open_count=0)[0] is True
    a2 = signal_asset()
    a2.update({"peersAgree": 0.2, "hourUtc": 14})  # pack diverges -> blocked
    assert ct.should_enter(a2, cfg, has_open=False, open_count=0)[0] is False


def test_crypto15m_daily_loss_limit_halts_new_entries(fresh_db, env_net, cfg, monkeypatch):
    # Once today's realized crypto15m loss crosses the limit, no NEW entry opens
    # (open positions are still managed; this is the circuit-breaker).
    cfg["crypto15m_live"] = True
    cfg["crypto15m_daily_loss_limit"] = -50.0
    p = _seed_c15(asset="ETH", ticker="KXETH15M-LOSS", status="settled",
                  dry_run=False, filled_contracts=15, target_contracts=15)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE crypto15m_positions SET resolved=1, pnl_usd=-60.0, "
            "resolved_at=datetime('now') WHERE id=?", (p["id"],))
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 0  # halted — nothing placed
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM crypto15m_positions WHERE asset='BTC'").fetchone()[0]
    assert n == 0  # no new position for the signal asset


def test_crypto15m_daily_loss_limit_off_allows_entries(fresh_db, env_net, cfg, monkeypatch):
    # With the limit disabled (0), losses don't halt entries.
    cfg["crypto15m_live"] = True
    cfg["crypto15m_daily_loss_limit"] = 0.0
    p = _seed_c15(asset="ETH", ticker="KXETH15M-LOSS2", status="settled",
                  dry_run=False, filled_contracts=15, target_contracts=15)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE crypto15m_positions SET resolved=1, pnl_usd=-200.0, "
            "resolved_at=datetime('now') WHERE id=?", (p["id"],))
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1  # not halted — entry placed


# ───────── take-profit (per-bet cash-out + overall kill-switch) ────────


def test_should_take_profit(cfg):
    cfg["crypto15m_take_profit"] = 0.95
    pos = {"status": "filled", "filled_contracts": 1}
    assert ct.should_take_profit(pos, 96, cfg) is True     # bid 96c >= 95c target
    assert ct.should_take_profit(pos, 95, cfg) is True     # exactly at target
    assert ct.should_take_profit(pos, 94, cfg) is False    # below target
    assert ct.should_take_profit(pos, None, cfg) is False  # cold/empty book
    # only filled rows are eligible
    assert ct.should_take_profit({"status": "submitted", "filled_contracts": 1}, 99, cfg) is False
    # disabled (0) never triggers, even at 99c
    cfg["crypto15m_take_profit"] = 0.0
    assert ct.should_take_profit(pos, 99, cfg) is False


def test_take_profit_cashes_out_at_bid(fresh_db, env_net, cfg, monkeypatch):
    # A filled position whose side bid reaches the target is SOLD into the book
    # at that bid (status -> exiting, exit_reason=take_profit).
    cfg["crypto15m_take_profit"] = 0.95
    pid = _seed_c15(asset="BTC", ticker="KXBTC15M-TP", direction="yes",
                    status="filled", filled_contracts=1, cost_usd=0.86,
                    entry_limit_cents=86)["id"]

    async def _open(_ticker):  # unsettled market — settlement check must pass through
        return {"yes_bid_dollars": 0.96, "yes_ask_dollars": 0.97, "status": "open", "result": ""}

    async def _quote(_ticker, _side):  # our YES bid is 96c — above the 95c target
        return {"bid_cents": 96, "ask_cents": 97}

    async def _positions():
        return []

    monkeypatch.setattr(polymarket_api, "fetch_market", _open)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "get_positions", _positions)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert len(calls) == 1
    assert calls[0]["action"] == "sell" and calls[0]["side"] == "yes"
    assert calls[0]["price_cents"] == 96   # crosses at our best bid
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pid)
    assert r["status"] == "exiting"
    assert r["exit_reason"] == "take_profit"


def test_settlement_beats_take_profit(fresh_db, env_net, cfg, monkeypatch):
    # A market that's already settled in our favour pays full value — the
    # take-profit (which would sell at < $1) must NOT pre-empt settlement.
    cfg["crypto15m_take_profit"] = 0.95
    pid = _seed_c15(asset="BTC", ticker="KXBTC15M-TPS", direction="yes",
                    status="filled", filled_contracts=1, cost_usd=0.86)["id"]

    async def _settled_yes(_ticker):
        return {"result": "yes", "status": "finalized"}

    async def _book(_ticker):
        return {"yes": [[96, 50]], "no": []}

    monkeypatch.setattr(polymarket_api, "fetch_market", _settled_yes)
    monkeypatch.setattr(polymarket_api, "get_orderbook", _book)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []  # no exit sell — settlement paid out instead
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pid)
    assert r["status"] == "settled"
    assert r["exit_reason"] != "take_profit"


def test_run_tick_manages_open_when_disabled(fresh_db, env_net, cfg, monkeypatch):
    # Engine disabled but a position is open: it must still be managed to
    # settlement, never stranded — this is what lets the overall take-profit
    # kill-switch turn the engine off while open bets ride to settlement.
    cfg["crypto15m_enabled"] = False
    pid = _seed_c15(asset="BTC", ticker="KXBTC15M-OFF", direction="yes",
                    status="filled", filled_contracts=1, cost_usd=0.40)["id"]

    async def _settled_yes(_ticker):
        return {"result": "yes", "status": "finalized"}

    monkeypatch.setattr(polymarket_api, "fetch_market", _settled_yes)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert calls == []  # disabled -> opens NO new entries
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pid)
    assert r["status"] == "settled" and r["resolved"] == 1  # but managed the open one


def test_overall_take_profit_arms_baseline(fresh_db, env_net, cfg, monkeypatch):
    # Already up $30 when the target is set: the baseline captures it, so the
    # goal is +$50 FROM HERE — it must NOT fire instantly on all-time P&L.
    cfg["crypto15m_take_profit_total"] = 50.0
    p = _seed_c15(asset="ETH", ticker="KXETH15M-ARM", status="settled",
                  filled_contracts=15, target_contracts=15)
    with db.get_db() as conn:
        conn.execute("UPDATE crypto15m_positions SET resolved=1, pnl_usd=30.0, "
                     "resolved_at=datetime('now') WHERE id=?", (p["id"],))
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert cfg["crypto15m_enabled"] is True  # not fired — gain since arming is $0
    with db.get_db() as conn:
        assert float(db.kv_get(conn, "c15_tp_baseline:mainnet")) == pytest.approx(30.0)


def test_overall_take_profit_fires_and_disables(fresh_db, env_net, cfg, monkeypatch):
    # Armed earlier at $0; a settled +$60 win pushes the gain past the $50
    # target -> engine turns OFF, host callback fires, baseline is consumed.
    cfg["crypto15m_take_profit_total"] = 50.0
    with db.get_db() as conn:
        db.kv_set(conn, "c15_tp_baseline:mainnet", "0.0")
    p = _seed_c15(asset="ETH", ticker="KXETH15M-TP", status="settled",
                  filled_contracts=15, target_contracts=15)
    with db.get_db() as conn:
        conn.execute("UPDATE crypto15m_positions SET resolved=1, outcome_correct=1, "
                     "pnl_usd=60.0, resolved_at=datetime('now') WHERE id=?", (p["id"],))

    fired: dict = {}

    async def _cb(info):
        fired.update(info)
    monkeypatch.setattr(ct, "_auto_off_cb", _cb)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    assert cfg["crypto15m_enabled"] is False        # engine turned off
    assert calls == []                              # no new entry opened
    assert fired.get("reason") == "take_profit_total"
    assert fired.get("gained") == pytest.approx(60.0)
    with db.get_db() as conn:
        assert db.kv_get(conn, "c15_tp_baseline:mainnet") is None  # baseline consumed


def test_overall_take_profit_disabled_clears_stale_baseline(fresh_db, env_net, cfg, monkeypatch):
    # Turning the target off (0) clears any leftover baseline so a future
    # enable re-arms from the then-current P&L.
    cfg["crypto15m_take_profit_total"] = 0.0
    with db.get_db() as conn:
        db.kv_set(conn, "c15_tp_baseline:mainnet", "12.5")
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))

    with db.get_db() as conn:
        assert db.kv_get(conn, "c15_tp_baseline:mainnet") is None


# ───────── paired both-sides: concurrent placement + missed-leg chase ────


def _paired_asset(ticker="KXBTC15M-P1", model_prob=0.62):
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"asset": "BTC", "series": "KXBTC15M", "ticker": ticker,
            "closeTime": close, "modelProb": model_prob, "feeSchedule": None,
            "deltaUsd": 0.0, "minsLeft": 5.0}


def _mock_quotes(monkeypatch, *, yes_ask, no_asks):
    """get_quote returns yes_ask always; the no side yields successive values
    from `no_asks` so a chase re-quotes a moved book."""
    state = {"no": 0}

    async def _q(_ticker, side):
        if side == "yes":
            return {"ask_cents": yes_ask, "bid_cents": yes_ask - 1}
        i = min(state["no"], len(no_asks) - 1)
        state["no"] += 1
        return {"ask_cents": no_asks[i], "bid_cents": no_asks[i] - 1}

    monkeypatch.setattr(polymarket_api, "get_quote", _q)


def _mock_place(monkeypatch, *, fills):
    """place_limit_order records calls; fills(side, price_cents)->bool decides
    whether the FAK landed (order_id) or was killed (0 fill)."""
    calls = []

    async def _place(**kw):
        calls.append(kw)
        if fills(kw["side"], kw["price_cents"]):
            return {"order": {"order_id": f"ord-{len(calls)}", "status": "matched"}}
        return {"order": {"status": "killed"}}

    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    return calls


# ───────── streak sizing (win/loss-conditioned bet size) ───────────────


def _seed_settled_pnls(pnls, network="mainnet", dry_run=False):
    """Seed settled positions in chronological order (LAST item = most
    recent; the streak reader breaks resolved_at ties by id DESC)."""
    for pnl in pnls:
        p = _seed_c15(status="settled", network=network, dry_run=dry_run,
                      filled_contracts=5, target_contracts=5)
        with db.get_db() as conn:
            conn.execute(
                "UPDATE crypto15m_positions SET resolved=1, pnl_usd=?, "
                "resolved_at=datetime('now') WHERE id=?", (pnl, p["id"]))


def _streak_cfg(cfg, **over):
    cfg["crypto15m_streak_sizing"] = True
    cfg["crypto15m_streak_loss_pct"] = over.get("loss_pct", 20.0)
    cfg["crypto15m_streak_win_pct"] = over.get("win_pct", 0.0)
    cfg["crypto15m_streak_max_mult"] = over.get("max_mult", 4.0)
    return cfg


def test_streak_multiplier_off_by_default(fresh_db, env_net, cfg):
    _seed_settled_pnls([-5.0, -5.0, -5.0])
    assert ct.streak_multiplier(cfg, "mainnet") == 1.0  # feature off


def test_streak_multiplier_no_history(fresh_db, env_net, cfg):
    assert ct.streak_multiplier(_streak_cfg(cfg), "mainnet") == 1.0


def test_streak_multiplier_loss_ramp_compounds(fresh_db, env_net, cfg):
    _seed_settled_pnls([4.0, -5.0, -5.0])  # win, then 2 straight losses
    m = ct.streak_multiplier(_streak_cfg(cfg), "mainnet")
    assert m == pytest.approx(1.2 ** 2)  # the win before the run is ignored


def test_streak_multiplier_capped(fresh_db, env_net, cfg):
    _seed_settled_pnls([-5.0] * 12)
    m = ct.streak_multiplier(_streak_cfg(cfg), "mainnet")
    assert m == pytest.approx(4.0)  # 1.2**12 ≈ 8.9, held to the cap


def test_streak_multiplier_win_resets_to_base(fresh_db, env_net, cfg):
    _seed_settled_pnls([-5.0, -5.0, -5.0, 4.0])  # most recent bet WON
    m = ct.streak_multiplier(_streak_cfg(cfg), "mainnet")
    assert m == 1.0  # win_pct=0 -> (1+0)**1: any win snaps back to base


def test_streak_multiplier_win_press(fresh_db, env_net, cfg):
    _seed_settled_pnls([-5.0, 4.0, 4.0])  # 2 straight wins
    m = ct.streak_multiplier(_streak_cfg(cfg, win_pct=25.0), "mainnet")
    assert m == pytest.approx(1.25 ** 2)  # anti-martingale press


def test_streak_multiplier_scratch_breaks_run(fresh_db, env_net, cfg):
    _seed_settled_pnls([-5.0, -5.0, 0.0])  # most recent settled flat
    assert ct.streak_multiplier(_streak_cfg(cfg), "mainnet") == 1.0


def test_streak_multiplier_scoped_to_network_and_live(fresh_db, env_net, cfg):
    # losses on the OTHER network and dry-run rows must not ramp mainnet size
    _seed_settled_pnls([-5.0, -5.0], network="testnet")
    _seed_settled_pnls([-5.0, -5.0], dry_run=True)
    assert ct.streak_multiplier(_streak_cfg(cfg), "mainnet") == 1.0


def test_compute_entry_contracts_streak_scales_fixed(cfg):
    cfg["crypto15m_sizing_mode"] = "fixed"
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=50, balance_usd=1000, order_size=5, streak_mult=1.44)
    assert n == 7  # int(5 * 1.44)
    # a shrunken multiplier floors at 1 contract, never 0 (0 is "can't fund")
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=50, balance_usd=1000, order_size=5, streak_mult=0.1)
    assert n == 1


def test_compute_entry_contracts_streak_respects_risk_caps(cfg):
    # max_loss_pct still bounds the ramped size: 5% of $100 at 50c = 10 contracts
    cfg["crypto15m_sizing_mode"] = "fixed"
    cfg["crypto15m_max_loss_pct"] = 0.05
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=50, balance_usd=100, order_size=8, streak_mult=4.0)
    assert n == 10  # 8*4=32 wanted, risk cap holds it to 10


# ── the entry stamp must follow the FILL, not the submit (audit High, d) ──
# _entry_is_maker trusts entry_order_type, which survived a maker→taker
# escalation and a failed re-quote — so a crossed entry was recomputed as a
# free maker fill by every later fee pass (the wallet reconcile's settled
# branch above all), erasing the fee from pnl_usd and the loss breakers.


def test_escalation_restamps_the_entry_as_a_taker(fresh_db, env_net, cfg,
                                                  monkeypatch):
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_entry_threshold"] = 0.5
    cfg["crypto15m_maker_cancel_min"] = 15.0
    cfg["crypto15m_maker_escalate"] = True
    close = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _seed_c15(asset="BTC", ticker="KXBTC15M-ESC", status="submitted",
              dry_run=False, target_contracts=2, entry_limit_cents=85,
              close_time=close, order_id="maker-esc", entry_order_type="GTC")

    state = {"canceled": False}

    async def _get_order(oid):
        if oid == "maker-esc":
            if state["canceled"]:
                return _order(0, 0, 0.0, status="canceled")
            return _order(0, 2, 0.0, status="resting")
        return _order(2, 0, 1.76, status="executed")

    async def _quote(_ticker, _side):
        return {"bid_cents": 86, "ask_cents": 88}

    async def _cancel(_oid):
        state["canceled"] = True
        return {"ok": True}

    async def _place(**_kw):
        return {"order": {"order_id": "taker-esc", "status": "executed"}}

    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))

    run_async(ct.run_tick(cfg, authed=True))
    with db.get_db() as conn:
        r = dict(conn.execute(
            "SELECT * FROM crypto15m_positions WHERE ticker='KXBTC15M-ESC'"
        ).fetchone())
    assert r["filled_contracts"] == 2
    assert ct._entry_is_maker(r) is False, r["entry_order_type"]
    # A later recomputation (wallet reconcile) now charges the real fee.
    assert ct._order_fee_usd({}, 2, 88.0, maker=ct._entry_is_maker(r)) > 0.0


def test_a_maker_that_lands_on_the_ask_is_stamped_as_a_taker(fresh_db, env_net,
                                                             cfg, monkeypatch):
    """The re-quote returned no BID, so the maker keeps its stale snapshot
    price — which is at/above the live ask and crosses on arrival. The order
    still goes out as a GTC; the row must say the fill was a taker's."""
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_entry_threshold"] = 0.5
    a = signal_asset()
    a["yesBid"] = 0.90          # stale snapshot bid, above the live ask
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([a]))

    async def _quote(_ticker, _side):
        return {"bid_cents": None, "ask_cents": 88}   # half a book

    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1 and calls[0]["order_type"] == "GTC"
    assert calls[0]["price_cents"] == 90              # never re-priced
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "mainnet")[0]
    assert ct._entry_is_maker(r) is False, r["entry_order_type"]


def test_a_maker_that_really_rests_is_still_a_maker(fresh_db, env_net, cfg,
                                                    monkeypatch):
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_entry_threshold"] = 0.5
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))

    async def _quote(_ticker, _side):
        return {"bid_cents": 85, "ask_cents": 88}

    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    calls = _capture_orders(monkeypatch)

    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1 and calls[0]["price_cents"] == 85   # joined the bid
    with db.get_db() as conn:
        r = db.get_open_crypto15m(conn, "mainnet")[0]
    assert ct._entry_is_maker(r) is True


def test_a_paper_account_opens_nothing_and_keeps_the_switch(fresh_db, env_net, cfg, monkeypatch):
    """Account mode gate: crypto15m_enabled stays as the user set it, the
    account is Paper, and no order is sent (config.engine_on)."""
    cfg["account_mode"] = "paper"
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([signal_asset()]))
    calls = _capture_orders(monkeypatch)
    run_async(ct.run_tick(cfg, authed=True))
    assert calls == [] and cfg["crypto15m_enabled"] is True
    # The backtest gate is account-agnostic: replays never send anything.
    assert ct.should_enter(signal_asset(), cfg, has_open=False, open_count=0) == (True, "ok")


def _paper_resting_entry(cfg, monkeypatch, *, minutes_to_close, filled=0):
    """A maker entry placed while Live (10 contracts at 85c) that is still
    resting when the account is Paper. Returns (row, orders placed, cancels)."""
    cfg["account_mode"] = "paper"
    cfg["crypto15m_entry_style"] = "maker"
    cfg["crypto15m_entry_threshold"] = 0.5
    cfg["crypto15m_maker_escalate"] = True
    close = (datetime.now(timezone.utc) + timedelta(minutes=minutes_to_close)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pos = _seed_c15(ticker="KXBTC15M-PAPER", status="submitted", target_contracts=10,
                    filled_contracts=filled, cost_usd=filled * 0.85,
                    entry_limit_cents=85, close_time=close, order_id="maker-p")
    state = {"canceled": False}

    async def _get_order(_oid):
        return _order(filled, 10 - filled, filled * 0.85,
                      status="canceled" if state["canceled"] else "resting")

    async def _cancel(oid):
        state["canceled"] = True
        cancels.append(oid)

    async def _quote(_t, _s):
        return {"bid_cents": 86, "ask_cents": 88}

    async def _wallet(**_kw):
        return []

    cancels: list = []
    monkeypatch.setattr(polymarket_api, "get_order", _get_order)
    monkeypatch.setattr(polymarket_api, "cancel_order", _cancel)
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)
    monkeypatch.setattr(polymarket_api, "get_positions", _wallet)
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([]))
    return pos, _capture_orders(monkeypatch), cancels


def test_paper_never_escalates_a_resting_entry_into_a_real_buy(fresh_db, env_net, cfg, monkeypatch):
    # Inside the cancel lead a Live maker crosses to a taker BUY (see
    # test_live_maker_escalates_to_taker). In Paper that buy was still sent:
    # new real exposure from an account that promises to send nothing.
    cfg["crypto15m_maker_cancel_min"] = 15.0
    pos, orders, cancels = _paper_resting_entry(cfg, monkeypatch, minutes_to_close=5)
    run_async(ct.run_tick(cfg, authed=True))
    assert orders == []
    assert cancels == ["maker-p"]
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "canceled" and r["resolved"] == 1
    assert r["exit_reason"] == "paper_pulled"


def test_paper_pulls_a_resting_entry_and_keeps_what_filled(fresh_db, env_net, cfg, monkeypatch):
    # Far from the close nothing would escalate yet, but the resting maker
    # could still fill after the switch to Paper. It is pulled now; the 3
    # contracts it already bought are kept as a real, managed position.
    cfg["crypto15m_maker_cancel_min"] = 0.0
    cfg["crypto15m_maker_fill_sec"] = 0
    pos, orders, cancels = _paper_resting_entry(cfg, monkeypatch, minutes_to_close=14, filled=3)
    run_async(ct.run_tick(cfg, authed=True))
    assert orders == [] and cancels == ["maker-p"]
    with db.get_db() as conn:
        r = db.fetch_crypto15m_by_id(conn, pos["id"])
    assert r["status"] == "filled" and r["resolved"] == 0
    assert r["filled_contracts"] == 3


def test_paper_keeps_the_stop_on_a_real_position(fresh_db, env_net, cfg, monkeypatch):
    # The other half of the rule: a position that already holds real
    # contracts keeps its stop-loss in Paper (a sell only shrinks exposure;
    # pausing it would leave a live bet unguarded until settlement).
    cfg["account_mode"] = "paper"
    calls = _stop_case(monkeypatch, gamma_side=0.60, ws={"Y": (20, 24), "N": (76, 80)})
    run_async(ct._manage_position(_filled(906), cfg, "mainnet"))
    assert calls == [906]


# ── the stop decides on the live book, not Gamma's lagging mark ──

def _stop_case(monkeypatch, *, gamma_side, ws=None, rest=None):
    """A filled YES position; Gamma says our side is at `gamma_side`; the live
    feed quotes `ws` ({token: (bid, ask)} or None); the uncached REST quote is
    `rest` ((yes_bid, yes_ask) cents or None). Returns the stop calls."""
    async def _mkt(_ticker):
        return {"yes_bid_dollars": gamma_side, "yes_ask_dollars": gamma_side, "status": "open", "result": ""}
    monkeypatch.setattr(polymarket_api, "fetch_market", _mkt)

    async def _meta(_t):
        return {"yes_token": "Y", "no_token": "N"}
    monkeypatch.setattr(polymarket_api, "get_market_meta", _meta)
    monkeypatch.setattr(ct.clob_ws, "warm", lambda *a: None)

    def _q(token, *a, **k):
        if not ws or token not in ws:
            return None
        b, a_ = ws[token]
        return {"bid_cents": b, "ask_cents": a_}
    monkeypatch.setattr(ct.clob_ws.feed, "get_quote_cents", _q)

    async def _quote(_t, _side):
        bid, ask = rest or (None, None)
        return {"bid_cents": bid, "ask_cents": ask}
    monkeypatch.setattr(polymarket_api, "get_quote", _quote)

    async def _no_chain(_pos):
        return None
    monkeypatch.setattr(ct, "_resolved_payout_from_chain", _no_chain)
    calls = []

    async def _fake_stop(p, market, c):
        calls.append(p["id"])
        return None
    monkeypatch.setattr(ct, "_place_stop_loss", _fake_stop)
    return calls


def _filled(pid):
    ct._stop_retry_at.pop(pid, None)
    return {"id": pid, "ticker": "0xstop", "direction": "yes", "asset": "BTC",
            "status": "filled", "filled_contracts": 10, "cost_usd": 6.0, "dry_run": 0}


def test_stop_fires_when_the_book_collapsed_but_gamma_has_not(fresh_db, env_net, cfg, monkeypatch):
    # Gamma still says 60c; the live book is 20/24: the position is dying.
    calls = _stop_case(monkeypatch, gamma_side=0.60, ws={"Y": (20, 24), "N": (76, 80)})
    run_async(ct._manage_position(_filled(901), cfg, "mainnet"))
    assert calls == [901]


def test_stop_holds_a_winner_on_a_stale_low_gamma_mark(fresh_db, env_net, cfg, monkeypatch):
    # Gamma lags at 30c (below the 40c stop) while the book is 70/72.
    calls = _stop_case(monkeypatch, gamma_side=0.30, ws={"Y": (70, 72), "N": (28, 30)})
    run_async(ct._manage_position(_filled(902), cfg, "mainnet"))
    assert calls == []


def test_stop_reads_the_rest_quote_when_the_feed_is_cold(fresh_db, env_net, cfg, monkeypatch):
    # No live quote: the REST quote is YES 70/72, so the mid is 71.
    calls = _stop_case(monkeypatch, gamma_side=0.30, ws=None, rest=(70, 72))
    run_async(ct._manage_position(_filled(903), cfg, "mainnet"))
    assert calls == []
    calls = _stop_case(monkeypatch, gamma_side=0.60, ws=None, rest=(18, 20))
    run_async(ct._manage_position(_filled(904), cfg, "mainnet"))
    assert calls == [904]


def test_stop_falls_back_to_gamma_without_a_sane_book(fresh_db, env_net, cfg, monkeypatch):
    # One-sided books everywhere: no live price, so Gamma decides (30c → stop).
    calls = _stop_case(monkeypatch, gamma_side=0.30, ws={"Y": (None, 40)}, rest=(None, None))
    run_async(ct._manage_position(_filled(905), cfg, "mainnet"))
    assert calls == [905]


def test_live_side_prob_for_a_no_position(monkeypatch):
    async def _meta(_t):
        return {"yes_token": "Y", "no_token": "N"}
    monkeypatch.setattr(polymarket_api, "get_market_meta", _meta)
    monkeypatch.setattr(ct.clob_ws, "warm", lambda *a: None)
    monkeypatch.setattr(ct.clob_ws.feed, "get_quote_cents",
                        lambda t, *a, **k: {"Y": {"bid_cents": 20, "ask_cents": 22}, "N": {"bid_cents": 78, "ask_cents": 80}}[t])
    assert run_async(ct._live_side_prob("0xm", "no")) == pytest.approx(0.79)
    assert run_async(ct._live_side_prob("0xm", "yes")) == pytest.approx(0.21)


# ── the sniper re-checks its edge at the live ask, not only the cap ──

def _sniper(cfg, monkeypatch, *, model_prob, snap_ask, live_ask):
    """Model mode with the gate fed a `snap_ask` snapshot and the order path's
    re-quote returning `live_ask` (cents). Returns the captured orders."""
    cfg["crypto15m_direction_mode"] = "model"
    cfg["crypto15m_model_autopause"] = False
    a = signal_asset(ticker="KXBTC15M-SNIPE")
    a.update({"modelProb": model_prob, "upAsk": snap_ask, "downAsk": 0.12,
              "spotLive": True, "feeSchedule": None})
    assert ct.should_enter(a, cfg, has_open=False, open_count=0) == (True, "ok")
    monkeypatch.setattr(crypto15m, "snapshot", _stub_snapshot([a]))

    async def _q(_t, _s):
        return {"bid_cents": live_ask - 1, "ask_cents": live_ask}
    monkeypatch.setattr(polymarket_api, "get_quote", _q)
    return _capture_orders(monkeypatch)


def test_sniper_skips_when_the_book_corrected_before_the_order(fresh_db, env_net, cfg, monkeypatch):
    # Audit probe: a 0.975 call gated on a 90c snapshot (+6.9c) while the book
    # has already moved to 96c, where the edge is +1.2c, under the 2c floor.
    # It used to send a 98c FAK, at -0.64c.
    calls = _sniper(cfg, monkeypatch, model_prob=0.975, snap_ask=0.90, live_ask=96)
    run_async(ct.run_tick(cfg, authed=True))
    assert calls == []
    assert "edge gone" in ct._block_reasons.get("BTC", "")
    # A transient, like should_enter's own refusal, so the window's one attempt
    # is not spent: the next tick on a book back at 90c still trades.
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 0

    async def _back(_t, _s):
        return {"bid_cents": 89, "ask_cents": 90}
    monkeypatch.setattr(polymarket_api, "get_quote", _back)
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1 and calls[0]["price_cents"] == 92   # plenty of edge: full markup


def test_sniper_limit_never_pays_past_the_min_edge(fresh_db, env_net, cfg, monkeypatch):
    # Live ask 94c still clears the floor, but ask + 2c markup = 96c would not.
    # The FAK is capped at 95c, the highest price that keeps 2c of edge.
    calls = _sniper(cfg, monkeypatch, model_prob=0.975, snap_ask=0.90, live_ask=94)
    run_async(ct.run_tick(cfg, authed=True))
    assert len(calls) == 1 and calls[0]["price_cents"] == 95
    px = calls[0]["price_cents"]
    assert 97.5 - px - crypto15m._fee_cents(px) >= 2.0
