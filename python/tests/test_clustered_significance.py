"""Backtest significance counts correlated trades once (2026-10-11 audit).

Crypto up/down windows for ~7 coins close at the same moment and move
together, so seven trades at one close are close to ONE draw. The c15
backtest, the replay and the script backtester computed t over trades as if
each were independent, which inflates t by up to ~sqrt(7). They now share
backtest.cluster_robust_t, clustered by settlement close time (by event for
whale/momentum signals).

backtest.py's headline mean (per signal) was printed next to a t computed
over EVENT means — a different average. Each mean now sits next to its own t.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

import backtest as bt
import config
import db
import replay
import script_backtest


def _naive_t(xs):
    n = len(xs)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n))


# ───────── the shared helper ───────────────────────────────────────────


def test_singleton_clusters_give_the_ordinary_t():
    rng = random.Random(7)
    xs = [rng.gauss(0.1, 1.0) for _ in range(200)]
    g, t = bt.cluster_robust_t(xs, list(range(len(xs))))
    assert g == 200
    assert t == pytest.approx(_naive_t(xs))


def test_equal_clusters_give_the_t_over_cluster_totals():
    rng = random.Random(3)
    xs, keys = [], []
    for c in range(40):
        shock = rng.gauss(0.05, 1.0)
        for _ in range(7):
            xs.append(shock + rng.gauss(0, 0.1))
            keys.append(f"close-{c}")
    totals = [sum(x for x, k in zip(xs, keys) if k == f"close-{c}") for c in range(40)]
    g, t = bt.cluster_robust_t(xs, keys)
    assert g == 40
    assert t == pytest.approx(_naive_t(totals))
    # …and well below the naive t the correlated trades would claim.
    assert abs(t) < abs(_naive_t(xs)) / 2


def test_too_few_clusters_has_no_t():
    assert bt.cluster_robust_t([1.0, 2.0, 3.0], ["a", "a", "a"]) == (1, None)


# ───────── the replays report the clustered t ─────────────────────────


def _correlated_trades(n_closes=30, per_close=7):
    """Seven coins per close, all winning or all losing together."""
    rng = random.Random(11)
    trades = []
    for c in range(n_closes):
        won = rng.random() < 0.6
        close = f"2026-10-01T{c // 4:02d}:{(c % 4) * 15:02d}:00Z"
        for a in range(per_close):
            trades.append({
                "ticker": f"T{c}-{a}", "asset": f"A{a}", "won": won,
                "pnlUsd": 0.05 if won else -0.95, "at": close[:10] + " 00:00:00",
                "closeTime": close,
            })
    return trades


def test_replay_summary_t_is_clustered_by_close_time():
    trades = _correlated_trades()
    out = replay._summarize(trades, 1, 30, [])
    pnls = [t["pnlUsd"] for t in trades]
    assert out["nClusters"] == 30
    assert out["tStatNaive"] == pytest.approx(_naive_t(pnls), abs=1e-3)
    _g, want = bt.cluster_robust_t(pnls, [t["closeTime"] for t in trades])
    assert out["tStat"] == pytest.approx(want, abs=1e-3)
    assert abs(out["tStat"]) < abs(out["tStatNaive"]) / 2


def test_replay_trades_carry_their_close_time():
    """_simulate stamps each trade with its window's close for clustering."""
    cfg = config.merge_with_defaults({})
    cfg.update({
        "crypto15m_interval": "15m", "crypto15m_direction_mode": "favorite",
        "crypto15m_use_rules": False,        "crypto15m_time_delay_min": 2.0, "crypto15m_entry_threshold": 0.90,
        "crypto15m_entry_max": 0.99, "crypto15m_min_delta_pct": 0.0,
        "crypto15m_enabled": True,
    })
    by_window = {}
    for c, close in enumerate(("2026-10-01T00:15:00Z", "2026-10-01T00:30:00Z")):
        for asset in ("BTC", "ETH"):
            by_window[f"{asset}-{c}"] = [{
                "ticker": f"{asset}-{c}", "asset": asset, "up_won": 1,
                "sig_close": close, "observed_at": "2026-10-01 00:13:30",
                "mins_left": 1.5, "yes_bid": 0.93, "yes_ask": 0.95,
                "up_prob": 0.95, "up_ask": 0.95, "no_ask": 0.07,
                "delta_pct": 0.002, "spot_source": "rtds-ws",
            }]
    trades, n_windows, _ = replay._simulate(by_window, cfg)
    assert n_windows == 4 and len(trades) == 4
    assert sorted({t["closeTime"] for t in trades}) == [
        "2026-10-01T00:15:00Z", "2026-10-01T00:30:00Z"]


def test_script_backtest_clusters_by_close_time(tmp_path, monkeypatch):
    dbfile = tmp_path / "bt.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with db.get_db() as conn:
        for c in range(5):
            close = now - timedelta(hours=3) + timedelta(minutes=15 * c)
            for asset in ("BTC", "ETH", "SOL"):
                tk = f"{asset}-{c}"
                conn.execute(
                    "INSERT INTO crypto15m_signals (ticker, asset, resolved, up_won, network, close_time, interval) "
                    "VALUES (?, ?, 1, ?, 'mainnet', ?, '15m')",
                    (tk, asset, c % 2, close.strftime("%Y-%m-%dT%H:%M:%SZ")))
                at = close - timedelta(minutes=2)
                conn.execute(
                    "INSERT INTO crypto15m_ticks (ticker, asset, observed_at, mins_left, yes_bid, yes_ask, "
                    "up_prob, up_ask, no_ask, network) VALUES (?, ?, ?, 2.0, 0.49, 0.5, 0.5, 0.5, 0.5, 'mainnet')",
                    (tk, asset, at.strftime("%Y-%m-%d %H:%M:%S")))
    code = 'def decide(ctx):\n    return {"side": "up", "price": "ask", "size": 1}\n'
    res = script_backtest.run({"crypto15m_interval": "15m", "script_max_entry_cents": 97,
                               "script_max_contracts": 20, "crypto15m_order_size": 1},
                              code, env="mainnet", since_days=2)
    assert res["n"] == 15
    assert res["nClusters"] == 5


# ───────── backtest.py: each mean next to its own t ────────────────────


def _sig(cost, correct, event):
    return {"source": "whale", "confidence": 60.0, "cost": cost,
            "correct": correct, "category": "sports", "event": event}


def test_summary_reports_both_means_with_matching_t():
    # One busy event (9 winning signals) and one quiet loser: per signal the
    # follow looks great, per event it is a coin flip.
    signals = [_sig(0.5, True, "BUSY") for _ in range(9)] + [_sig(0.5, False, "QUIET")]
    s = bt.summarize(signals)
    fee = bt.polymarket_fee_per_contract(0.5, category="sports")
    assert s["net_ev"] == pytest.approx((9 * 0.5 - 0.5) / 10 - fee)
    assert s["net_ev_event"] == pytest.approx(0.0 - fee)
    assert s["n_events"] == 2
    _g, t_cl = bt.cluster_robust_t(
        [bt.net_pnl_per_contract(x["cost"], x["correct"], category="sports") for x in signals],
        [x["event"] for x in signals])
    assert s["t_cluster"] == pytest.approx(t_cl)


def test_the_verdict_tests_the_headline_mean_with_its_own_t():
    # 40 events, 5 signals each, 25 events won. Inside an event the outcomes
    # are identical, so the per-signal t is ~sqrt(5) too high; the verdict
    # must not call a significant edge on it.
    signals = []
    for e in range(40):
        signals += [_sig(0.5, e < 25, f"E{e}") for _ in range(5)]
    rep = bt.build_report(signals, None)
    o = rep["overall"]
    assert o["t"] > 2 > o["t_cluster"]
    assert "POSITIVE" not in rep["verdict"]
    assert "clustered" in rep["verdict"]


def test_c15_backtest_clusters_signals_by_close_time():
    sigs = []
    for c in range(12):
        won = c % 3 != 0
        for _ in range(7):
            sigs.append({"favorite": "up", "favorite_price": 0.9, "entry_cost": 0.9,
                         "fav_won": won, "delta_pct": 0.01, "mins_left": 3.0,
                         "macd_hist": None, "macd_cross": None, "rsi": None,
                         "close_time": f"2026-10-01T{c:02d}:15:00Z"})
    r = bt.crypto15m_eval(sigs, mode="favorite")
    assert r["n"] == 84 and r["n_events"] == 12
    assert abs(r["t_cluster"]) < abs(r["t"]) / 2
