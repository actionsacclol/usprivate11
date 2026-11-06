"""Settlement sniper (model direction mode) — the terminal-spot model, its
fee-net edge math, the should_enter gates (incl. final-minute), the Wilson
calibration guard, and the replay harness that backtests it all.

The model is interval-agnostic by construction: every test that exercises
time uses minutes-left values from the 5m, 15m, AND hourly regimes.
"""
from __future__ import annotations

import asyncio

import pytest

import crypto15m
import crypto15m_trader as c15t
import db
import indicators
import replay
import spot_ws
from config import merge_with_defaults


# ───────── terminal-spot model ─────────────────────────────────────────


def test_model_up_prob_sides_and_bounds():
    # Spot above the open → P(up) > 0.5; below → < 0.5; equal → 0.5.
    assert crypto15m.model_up_prob(101.0, 100.0, 0.001, 5.0) > 0.5
    assert crypto15m.model_up_prob(99.0, 100.0, 0.001, 5.0) < 0.5
    assert crypto15m.model_up_prob(100.0, 100.0, 0.001, 5.0) == pytest.approx(0.5)


def test_model_sharpens_as_close_approaches():
    # Same distance from the strike: certainty must grow as t shrinks —
    # this is what replaces Kalshi's print-accumulation on Polymarket.
    p_hourly = crypto15m.model_up_prob(100.5, 100.0, 0.001, 55.0)  # hourly window
    p_15m = crypto15m.model_up_prob(100.5, 100.0, 0.001, 10.0)
    p_5m = crypto15m.model_up_prob(100.5, 100.0, 0.001, 2.0)
    p_final = crypto15m.model_up_prob(100.5, 100.0, 0.001, 0.3)
    assert p_hourly < p_15m < p_5m < p_final
    assert p_final > 0.99


def test_model_up_prob_missing_inputs_none():
    assert crypto15m.model_up_prob(None, 100.0, 0.001, 5.0) is None
    assert crypto15m.model_up_prob(100.0, None, 0.001, 5.0) is None
    assert crypto15m.model_up_prob(100.0, 100.0, None, 5.0) is None
    assert crypto15m.model_up_prob(100.0, 100.0, 0.001, None) is None
    assert crypto15m.model_up_prob(100.0, 100.0, 0.0, 5.0) is None


def test_fee_cents_is_fee_v2_curve():
    # Polymarket US: feeCoefficient 0.0695 on crypto, rate x p(1-p) in cents/share.
    assert crypto15m._fee_cents(90.0) == pytest.approx(0.6255)
    assert crypto15m._fee_cents(50.0) == pytest.approx(1.7375)
    # The market's own schedule overrides the crypto fallback.
    assert crypto15m._fee_cents(50.0, {"enabled": True, "rate": 0.25, "exponent": 2.0}) \
        == pytest.approx(0.25 * 0.25 ** 2 * 100.0)
    # Explicitly fee-free market → 0.
    assert crypto15m._fee_cents(50.0, {"enabled": False}) == 0.0


def test_model_edge_picks_the_better_side_net_of_fee():
    # Model 98% up, up ask 90c: edge = 98 − 90 − 0.63 = +7.37.
    e = crypto15m.model_edge_net_cents(0.98, 0.90, 0.15)
    assert e == pytest.approx(7.37, abs=0.01)
    # Model 2% up (i.e. 98% down), down ask 90c: same edge on the down side.
    e2 = crypto15m.model_edge_net_cents(0.02, 0.15, 0.90)
    assert e2 == pytest.approx(7.37, abs=0.01)
    assert crypto15m.model_edge_net_cents(None, 0.9, 0.1) is None
    assert crypto15m.model_edge_net_cents(0.98, None, None) is None


def test_sigma1m_needs_history_and_measures_vol():
    assert indicators.sigma1m([100.0] * 10) is None          # too short
    flat = indicators.sigma1m([100.0] * 40)
    assert flat == pytest.approx(0.0)                        # no movement
    wiggly = indicators.sigma1m([100.0 + (i % 2) for i in range(40)])
    assert wiggly and wiggly > 0.001


# ───────── model direction + entry gates ───────────────────────────────


def _model_cfg(**over) -> dict:
    base = {
        "crypto15mEnabled": True,
        "crypto15mDirectionMode": "model",
        "crypto15mModelAutopause": False,   # calibration tested separately
        "crypto15mModelMinProb": 0.97,
        "crypto15mModelMinEdgeCents": 2.0,
        "crypto15mTimeDelayMin": 8,
    }
    base.update(over)
    return merge_with_defaults(base)


def _asset(**over) -> dict:
    a = {
        "asset": "BTC", "hasMarket": True, "favorite": "up",
        "minsLeft": 5.0, "inWindow": True, "modelProb": 0.99,
        "edgeNetCents": 5.0, "upAsk": 0.90, "downAsk": 0.12,
        "spotLive": True,
    }
    a.update(over)
    return a


def test_entry_side_follows_model():
    cfg = _model_cfg()
    assert c15t._entry_side(_asset(modelProb=0.99), cfg) == "up"
    assert c15t._entry_side(_asset(modelProb=0.01), cfg) == "down"
    assert c15t._entry_side(_asset(modelProb=None), cfg) == ""


def test_model_entry_ok_at_high_prob_and_edge():
    ok, why = c15t.should_enter(_asset(), _model_cfg(), has_open=False, open_count=0)
    assert ok, why


def test_model_entry_blocked_below_min_prob():
    ok, why = c15t.should_enter(_asset(modelProb=0.90), _model_cfg(),
                                has_open=False, open_count=0)
    assert not ok and "model 0.9" in why


def test_model_entry_blocked_on_thin_edge():
    # Edge is measured on the side we actually BUY (mp≥0.5 → up), from its real
    # ask — NOT the stored best-of-both-sides edgeNetCents. A 97.5c up-ask under
    # a 0.99 model is only ~1.3c net edge → below the 2c floor.
    ok, why = c15t.should_enter(_asset(upAsk=0.975), _model_cfg(),
                                has_open=False, open_count=0)
    assert not ok and "net edge" in why


def test_model_entry_edge_is_traded_side_not_best_side():
    # Near-arb window: model 0.97 up, up-ask 96c (traded side, ~0c edge) but the
    # CHEAP down side (1c) carries the +edge. The gate must NOT pass on the down
    # side's edge while buying the negative-EV up side.
    a = _asset(modelProb=0.97, upAsk=0.96, downAsk=0.01, edgeNetCents=2.9)
    ok, why = c15t.should_enter(a, _model_cfg(), has_open=False, open_count=0)
    assert not ok and "net edge" in why


def test_model_entry_blocked_when_model_unavailable():
    ok, why = c15t.should_enter(_asset(modelProb=None), _model_cfg(),
                                has_open=False, open_count=0)
    assert not ok and "model unavailable" in why


def test_model_certain_down_zero_prob_is_not_dropped():
    # modelProb == 0.0 is CERTAIN DOWN — a truthiness bug would treat it as
    # missing. p_side = 1.0, so it must pass the prob gate.
    ok, why = c15t.should_enter(_asset(modelProb=0.0, downAsk=0.90, upAsk=0.05),
                                _model_cfg(), has_open=False, open_count=0)
    assert ok, why


def test_perfect_record_never_pauses_and_can_resume():
    # Guard against the KT bug reappearing here: a perfect record's Wilson
    # LB is n/(n+z²) — 0.937 max inside the 40-window cap — so both bars
    # must sit below what a flawless model can actually reach.
    for n in range(c15t._CAL_MIN_N, c15t._CAL_WINDOW + 1):
        assert c15t._wilson_lb(n, n) >= c15t._CAL_PAUSE_LB, f"perfect {n}/{n} would pause"
    assert c15t._wilson_lb(c15t._CAL_WINDOW, c15t._CAL_WINDOW) >= c15t._CAL_RESUME_LB
    # The resume bar must be reachable WITHOUT a literally perfect trailing
    # window: one miss in 40 (97.5% observed — healthy) must resume, while
    # 38/40 stays below resume (hysteresis intact).
    assert c15t._wilson_lb(c15t._CAL_WINDOW - 1, c15t._CAL_WINDOW) >= c15t._CAL_RESUME_LB
    assert c15t._wilson_lb(38, 40) < c15t._CAL_RESUME_LB
    # …while a genuinely degraded record (90% observed at n=40) still pauses.
    assert c15t._wilson_lb(36, 40) < c15t._CAL_PAUSE_LB


# ───────── strike integrity (mid-window restart guard) ──────────────────


def test_window_open_rejected_when_first_seen_late():
    # Live incident 2026-07-03: an app restart 44s into a 5m window captured
    # a strike 32 points below the real open → fake 2-sigma certainty → a
    # 50c coin-flip bought as a "97.5%" snipe. A late first look must yield
    # NO strike for that window's whole life.
    crypto15m._window_open.clear()
    start = 1_790_000_000
    assert crypto15m._track_window_open("BTC", start, 62508.6, start + 44.0) is None
    # …and it stays None on later ticks of the same window.
    assert crypto15m._track_window_open("BTC", start, 62540.0, start + 120.0) is None


def test_window_open_captured_within_grace_then_sticky():
    crypto15m._window_open.clear()
    start = 1_790_000_000
    assert crypto15m._track_window_open("BTC", start, 62540.0, start + 5.0) == 62540.0
    # Once captured in time, later reads return the SAME strike regardless of when.
    assert crypto15m._track_window_open("BTC", start, 63000.0, start + 200.0) == 62540.0
    crypto15m._window_open.clear()


# ───────── model/book divergence guard ──────────────────────────────────


def test_divergence_guard_blocks_too_good_midwindow_edge():
    # Model 99.9% up vs a book asking 50c = a 48c "edge" mid-window. A live
    # book that disagrees that much knows the strike better than we do.
    a = _asset(modelProb=0.999, upAsk=0.50, downAsk=0.52, edgeNetCents=48.0,
               minsLeft=3.0)
    ok, why = c15t.should_enter(a, _model_cfg(), has_open=False, open_count=0)
    assert not ok and "disagrees" in why
    # Setting the cap to 0 disables the guard.
    ok2, why2 = c15t.should_enter(
        a, _model_cfg(crypto15mModelMaxBookGapCents=0), has_open=False, open_count=0)
    assert ok2, why2


def test_divergence_guard_exempts_final_minute():
    # Books genuinely gap 20-67c in the final minute (documented lag edge);
    # the FM path keeps its own 3-sigma + live-spot gates instead.
    a = _asset(modelProb=0.9990, upAsk=0.50, downAsk=0.52, edgeNetCents=48.0,
               minsLeft=0.5, spotLive=True)
    ok, why = c15t.should_enter(a, _model_cfg(), has_open=False, open_count=0)
    assert ok, why


# ───────── paired both-sides mode (model-tilt ladder) ───────────────────


def _paired_cfg(**over):
    base = {"crypto15mPairedMode": True}
    base.update(over)
    return _model_cfg(**base)


def test_strength_exit_eligibility():
    cfg_on = merge_with_defaults({"crypto15mSellIntoStrength": True,
                                  "crypto15mSellStrengthCents": 80})
    pos = {"status": "filled", "filled_contracts": 10, "avg_entry_cents": 50.0}
    # Paired-style 50c entry → rest the ask at 80c.
    assert c15t.strength_exit_cents(pos, cfg_on) == 80
    # Sniper-style 95c entry: nothing to sell into at 80c → ineligible.
    assert c15t.strength_exit_cents(dict(pos, avg_entry_cents=95.0), cfg_on) is None
    # Entry just under the target (79c) still ineligible — needs 2c of room.
    assert c15t.strength_exit_cents(dict(pos, avg_entry_cents=79.0), cfg_on) is None
    # Feature off / not filled / no contracts → None.
    assert c15t.strength_exit_cents(pos, merge_with_defaults({})) is None
    assert c15t.strength_exit_cents(dict(pos, status="exiting"), cfg_on) is None
    assert c15t.strength_exit_cents(dict(pos, filled_contracts=0), cfg_on) is None


def test_strength_exit_target_clamped():
    pos = {"status": "filled", "filled_contracts": 10, "avg_entry_cents": 20.0}
    cfg = merge_with_defaults({"crypto15mSellIntoStrength": True,
                               "crypto15mSellStrengthCents": 300})
    # Config validation clamps to 99 before it ever reaches the helper — and
    # the helper clamps defensively too.
    assert c15t.strength_exit_cents(pos, cfg) == 99


def test_min_notional_guard():
    # Polymarket rejects marketable BUYs under $1 notional even above the
    # 5-contract minimum (live 2026-07-04: 5×3c = $0.15 → HTTP 400). The
    # guard must catch cheap legs BEFORE placement so a failed hedge can
    # never strand a filled dominant.
    assert not c15t._min_notional_ok(5, 3)      # $0.15 — the live failure
    assert not c15t._min_notional_ok(10, 7)     # $0.70 — the other live failure
    assert not c15t._min_notional_ok(5, 19)     # $0.95 — just under
    assert c15t._min_notional_ok(5, 20)         # $1.00 exactly
    assert c15t._min_notional_ok(5, 50)         # normal paired leg


def test_final_minute_needs_live_ws_spot():
    a = _asset(minsLeft=0.5, modelProb=0.999, spotLive=False)
    ok, why = c15t.should_enter(a, _model_cfg(), has_open=False, open_count=0)
    assert not ok and "live WS spot" in why


def test_final_minute_three_sigma_gate():
    # Inside the last minute 0.99 is NOT enough — needs ~3-sigma certainty.
    a = _asset(minsLeft=0.5, modelProb=0.99)
    ok, why = c15t.should_enter(a, _model_cfg(), has_open=False, open_count=0)
    assert not ok and "3-sigma" in why
    a2 = _asset(minsLeft=0.5, modelProb=0.9990)
    ok2, why2 = c15t.should_enter(a2, _model_cfg(), has_open=False, open_count=0)
    assert ok2, why2


def test_final_minute_runway_guard():
    a = _asset(minsLeft=0.05, modelProb=0.9999)
    ok, why = c15t.should_enter(a, _model_cfg(), has_open=False, open_count=0)
    assert not ok and "round-trip" in why


def test_final_minute_disabled_by_config():
    a = _asset(minsLeft=0.5, modelProb=0.9999)
    cfg = _model_cfg(crypto15mModelFinalMinute=False)
    ok, why = c15t.should_enter(a, cfg, has_open=False, open_count=0)
    assert not ok and "final minute" in why


def test_rules_blocked_in_final_minute():
    # Custom rules must never buy the max-gamma final minute — model mode only.
    cfg = merge_with_defaults({
        "crypto15mEnabled": True, "crypto15mUseRules": True,
        "crypto15mRules": [{"field": "minsLeft", "op": "<=", "value": 15}],
    })
    a = _asset(minsLeft=0.5)
    ok, why = c15t.should_enter(a, cfg, has_open=False, open_count=0)
    assert not ok and "model mode only" in why


def test_non_model_modes_still_blocked_from_final_minute_entries():
    # favorite mode has no final-minute path: inWindow is irrelevant because
    # `signal` (built upstream) is what gates it; here signal=False blocks.
    cfg = merge_with_defaults({"crypto15mEnabled": True})
    a = _asset(minsLeft=0.5)
    a["signal"] = False
    ok, why = c15t.should_enter(a, cfg, has_open=False, open_count=0)
    assert not ok


# ───────── calibration guard ───────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "model-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _seed_windows(n_wins: int, n_losses: int) -> None:
    """Seed resolved windows where the model was sniper-confident UP; wins
    settle up, losses settle down."""
    with db.get_db() as conn:
        for i in range(n_wins + n_losses):
            tk = f"W{i}"
            won = i < n_wins
            conn.execute(
                """INSERT INTO crypto15m_signals
                   (ticker, asset, resolved, up_won, network, close_time)
                   VALUES (?, 'BTC', 1, ?, 'mainnet', '2026-01-01T00:00:00Z')""",
                (tk, 1 if won else 0),
            )
            conn.execute(
                """INSERT INTO crypto15m_ticks
                   (ticker, asset, mins_left, model_prob, network)
                   VALUES (?, 'BTC', 2.0, 0.99, 'mainnet')""",
                (tk,),
            )


def test_wilson_lb_basic():
    assert c15t._wilson_lb(0, 0) == 0.0
    assert c15t._wilson_lb(40, 40) > 0.9
    assert c15t._wilson_lb(20, 40) < 0.5


def test_calibration_pauses_on_degraded_hit_rate(fresh_db):
    c15t._CAL_CACHE.update({"at": 0.0, "ok": True})
    _seed_windows(n_wins=25, n_losses=15)   # 62.5% — way below break-even
    cal = c15t.check_model_calibration("mainnet")
    assert cal["ok"] is False
    assert cal["n"] == 40


def test_calibration_ok_with_high_hit_rate(fresh_db):
    c15t._CAL_CACHE.update({"at": 0.0, "ok": True})
    _seed_windows(n_wins=40, n_losses=0)
    cal = c15t.check_model_calibration("mainnet")
    assert cal["ok"] is True


def test_calibration_insufficient_evidence_never_pauses(fresh_db):
    c15t._CAL_CACHE.update({"at": 0.0, "ok": True})
    _seed_windows(n_wins=2, n_losses=8)     # awful, but n=10 < min evidence
    cal = c15t.check_model_calibration("mainnet")
    assert cal["ok"] is True


def test_model_entry_blocked_when_calibration_paused(fresh_db):
    c15t._CAL_CACHE.update({"at": 0.0, "ok": True})
    _seed_windows(n_wins=25, n_losses=15)
    cfg = _model_cfg(crypto15mModelAutopause=True)
    ok, why = c15t.should_enter(_asset(), cfg, has_open=False, open_count=0)
    assert not ok and "calibration degraded" in why
    c15t._CAL_CACHE.update({"at": 0.0, "ok": True})  # don't leak to other tests


# ───────── spot_ws (Coinbase feed) ─────────────────────────────────────


def test_spot_ws_parses_ticker_and_serves_fresh():
    c = spot_ws._Client()
    c.connected = True
    c.handle_message({
        "channel": "ticker",
        "events": [{"tickers": [{"product_id": "BTC-USD", "price": "50123.5"}]}],
    })
    assert c.spot("BTC") == pytest.approx(50123.5)
    assert c.fresh_spots() == {"BTC": pytest.approx(50123.5)}
    # Unknown products and junk prices are ignored.
    c.handle_message({
        "channel": "ticker",
        "events": [{"tickers": [{"product_id": "PEPE-USD", "price": "1"},
                                {"product_id": "ETH-USD", "price": "junk"}]}],
    })
    assert c.spot("PEPE") is None
    assert c.spot("ETH") is None


def test_spot_ws_staleness_gate():
    c = spot_ws._Client()
    c.connected = True
    c.prices["BTC"] = (0.0, 50000.0)   # epoch-old timestamp → stale
    assert c.spot("BTC") is None
    assert c.fresh_spots() == {}


def test_spot_ws_sampler_one_print_per_second_and_window_partial():
    c = spot_ws._Client()
    now = 1_000_000.0
    c.prices["BTC"] = (now, 100.0)
    c._sample_once(now)
    c._sample_once(now + 0.4)          # same wall second → no duplicate
    c.prices["BTC"] = (now + 1, 101.0)
    c._sample_once(now + 1.0)
    assert len(c.samples["BTC"]) == 2
    close = now + 30                   # window closing 30s from first sample
    total, count = c.window_partial("BTC", close, now=now + 2)
    assert count == 2 and total == pytest.approx(201.0)
    # Prints outside [close-60, close) don't count.
    total2, count2 = c.window_partial("BTC", now - 120, now=now + 2)
    assert count2 == 0


# ───────── rtds_ws (Polymarket RTDS / Chainlink oracle feed) ────────────


def _rtds_update(symbol: str, value, ts_ms: int = 1_783_117_568_000) -> dict:
    # Exact live envelope observed 2026-07-03 (connection_id omitted — unused).
    return {
        "topic": "crypto_prices_chainlink", "type": "update",
        "timestamp": ts_ms + 948,
        "payload": {"symbol": symbol, "value": value, "timestamp": ts_ms,
                    "full_accuracy_value": "62597344603030672500000"},
    }


# ───────── replay (backtest == live gates) ─────────────────────────────


def _seed_replay_window(ticker: str, up_won: int, *, model_prob=0.99,
                        edge=5.0, yes_ask=0.90, mins_left=4.0,
                        spot_source="coinbase-ws", up_ask=None) -> None:
    """Seed one resolved window with a single tick. `up_ask` mirrors the
    recorder's REAL-book-only column; it defaults to yes_ask (live book was
    up) — pass up_ask=... explicitly (or None via sentinel below) to model a
    Gamma-fallback tick with no executable up quote."""
    if up_ask is None:
        up_ask = yes_ask
    elif up_ask == "gamma":  # sentinel: WS cold, yes_ask is the phantom mid
        up_ask = None
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO crypto15m_signals
               (ticker, asset, resolved, up_won, network, close_time)
               VALUES (?, 'BTC', 1, ?, 'mainnet', '2026-01-01T00:15:00Z')""",
            (ticker, up_won),
        )
        conn.execute(
            """INSERT INTO crypto15m_ticks
               (ticker, asset, mins_left, yes_bid, yes_ask, up_prob, no_ask,
                model_prob, edge_net_cents, spot_source, up_ask, network)
               VALUES (?, 'BTC', ?, ?, ?, 0.9, ?, ?, ?, ?, ?, 'mainnet')""",
            (ticker, mins_left, yes_ask - 0.02, yes_ask, 1.0 - yes_ask + 0.02,
             model_prob, edge, spot_source, up_ask),
        )


def test_replay_model_mode_trades_and_nets_fees(fresh_db):
    cfg = _model_cfg()
    _seed_replay_window("RW1", up_won=1)
    _seed_replay_window("RW2", up_won=0)
    res = replay.replay(cfg, env="mainnet", since_days=60)
    assert res["windowsScanned"] == 2
    assert res["n"] == 2
    assert res["wins"] == 1
    # Per contract (fee v2: 0.07·0.9·0.1 = 0.0063) — win: 1 − 0.90 − 0.0063
    # = +0.0937; loss: −0.9063.
    per_ct = 0.0937 - 0.9063
    assert res["totalPnlUsd"] == pytest.approx(per_ct * res["contracts"], abs=0.02)


def test_replay_one_trade_per_window(fresh_db):
    cfg = _model_cfg()
    _seed_replay_window("RW3", up_won=1)
    with db.get_db() as conn:   # second qualifying tick in the SAME window
        conn.execute(
            """INSERT INTO crypto15m_ticks
               (ticker, asset, mins_left, yes_ask, up_prob, model_prob,
                edge_net_cents, spot_source, up_ask, network)
               VALUES ('RW3', 'BTC', 3.0, 0.91, 0.9, 0.99, 5.0,
                       'coinbase-ws', 0.91, 'mainnet')""",
        )
    res = replay.replay(cfg, env="mainnet", since_days=60)
    assert res["n"] == 1


def test_replay_refuses_phantom_gamma_book(fresh_db):
    """A tick whose yes_ask is the Gamma-fallback mid (CLOB WS cold: no
    up_ask, no ws_ask) must NOT fill — live can't trade a phantom quote.
    This is the 2026-07 paired/sniper backtest bug: Gamma sits 20-67c off
    the real book near close, so phantom fills flattered every strategy."""
    cfg = _model_cfg()
    _seed_replay_window("RW6", up_won=1, up_ask="gamma")
    res = replay.replay(cfg, env="mainnet", since_days=60)
    assert res["n"] == 0 and res["windowsScanned"] == 1


def test_replay_gate_blocks_low_prob(fresh_db):
    cfg = _model_cfg()
    _seed_replay_window("RW4", up_won=1, model_prob=0.80)
    res = replay.replay(cfg, env="mainnet", since_days=60)
    assert res["n"] == 0 and res["windowsScanned"] == 1


def test_replay_missing_rule_fields_fail_closed_with_caveat(fresh_db):
    cfg = merge_with_defaults({
        "crypto15mEnabled": True, "crypto15mUseRules": True,
        "crypto15mRules": [{"field": "bookImbalance", "op": ">=", "value": 0.5}],
    })
    _seed_replay_window("RW5", up_won=1)
    res = replay.replay(cfg, env="mainnet", since_days=60)
    assert res["n"] == 0
    assert any("bookImbalance" in c for c in res["caveats"])


def test_fm_extreme_divergence_refused():
    # User report 2026-07-16: 1000 BTC-down shares bought at 1c in the final
    # minute on a near-certain model call — a ~98c "edge" that is almost
    # always a fabricated strike / stale spot, not the documented 20-67c lag.
    # The FM path used to be fully exempt from the divergence guard; it now
    # has its own looser cap (default 75c).
    ok, why = c15t.should_enter(
        _asset(minsLeft=0.5, modelProb=0.001, downAsk=0.01, upAsk=0.99),
        _model_cfg(),
        has_open=False, open_count=0,
    )
    assert not ok and "final minute" in why and "distrusting" in why


def test_fm_documented_gap_range_still_allowed():
    # A 3-sigma call against a 40c ask (~53c net edge) is inside the
    # documented 20-67c final-minute gap band — the snipe the FM path exists
    # for. The new cap must NOT block it.
    ok, why = c15t.should_enter(
        _asset(minsLeft=0.5, modelProb=0.999, upAsk=0.40),
        _model_cfg(),
        has_open=False, open_count=0,
    )
    assert ok, why


def test_fm_gap_cap_configurable_off():
    # 0 disables the FM cap (restores the old unlimited exemption for anyone
    # who explicitly wants it).
    ok, why = c15t.should_enter(
        _asset(minsLeft=0.5, modelProb=0.001, downAsk=0.01, upAsk=0.99),
        _model_cfg(crypto15mModelFmMaxBookGapCents=0),
        has_open=False, open_count=0,
    )
    assert ok, why


# ───────── TWAP settlement (2026-08-07+) ──────────────────────────────


def _pb(channel, symbol, value, ts_ms, *, snapshot=False, source=None):
    point = {"value": value, "timestamp": ts_ms}
    if snapshot:
        payload = {"symbol": symbol, "data": [point]}
    else:
        payload = {"symbol": symbol, **point}
    if source:
        payload["source"] = source
    if channel.endswith("twap"):
        payload["window_seconds"] = 60
    m = {"v": 1, "channel": channel, "seq": 1, "ts": ts_ms, "payload": payload}
    if snapshot:
        m["snapshot"] = True
    return m


def test_twap_model_matches_terminal_far_out_and_firms_up_in_the_final_minute():
    s = 0.001
    # 10 minutes out the average is all future: close to the terminal model
    # (variance r−40 vs r seconds).
    twap10 = crypto15m.model_up_prob(100.2, 100.0, s, 10.0)
    term10 = crypto15m.model_up_prob(100.2, 100.0, s, 10.0, settle_twap_sec=0)
    assert twap10 > term10 and abs(twap10 - term10) < 0.03
    # 30s left, spot just above the strike: the TWAP model is more certain
    # because half the average is already in (and it was above too).
    known = sum([100.1] * 30)
    p_twap = crypto15m.model_up_prob(100.1, 100.0, s, 0.5, known_sum=known, known_count=30)
    p_term = crypto15m.model_up_prob(100.1, 100.0, s, 0.5, settle_twap_sec=0)
    assert p_twap > p_term > 0.5


def test_a_late_spike_cannot_flip_a_mostly_observed_average():
    s = 0.001
    # 50s of the settlement average sat at 99.9 (below the 100 strike); with
    # 10s left spot spikes to 100.3. Terminal model: near-certain UP. TWAP
    # model: the average is ~99.97 — still DOWN.
    known = sum([99.9] * 50)
    p_twap = crypto15m.model_up_prob(100.3, 100.0, s, 10 / 60, known_sum=known, known_count=50)
    p_term = crypto15m.model_up_prob(100.3, 100.0, s, 10 / 60, settle_twap_sec=0)
    assert p_term > 0.99
    assert p_twap < 0.05


def test_model_entries_refuse_an_approximate_strike(monkeypatch):
    import crypto15m_trader as ct
    monkeypatch.setattr(crypto15m, "hours_ok", lambda cfg, hour=None: True)
    cfg = {"crypto15m_enabled": True, "crypto15m_direction_mode": "model",
           "crypto15m_model_autopause": False, "crypto15m_max_concurrent": 7}
    asset = {"hasMarket": True, "favorite": "up", "modelProb": 0.999, "minsLeft": 3.0,
             "inWindow": True, "upAsk": 0.9, "downAsk": 0.12, "edgeNetCents": 8.0}
    ok, why = ct.should_enter({**asset, "strikeSource": "spot"}, cfg, has_open=False, open_count=0)
    assert ok is False and "approximate" in why
    # The official TWAP strike passes this gate (whatever later gates decide).
    _ok2, why2 = ct.should_enter({**asset, "strikeSource": "twap"}, cfg, has_open=False, open_count=0)
    assert "approximate" not in why2


def test_settlement_rule_per_series():
    assert crypto15m.settle_twap_sec("5m") == 60.0
    assert crypto15m.settle_twap_sec("15m") == 60.0
    assert crypto15m.settle_twap_sec("hourly") == 60.0     # every series on this venue
    assert "binance" not in crypto15m.STRIKE_SOURCES_OK
