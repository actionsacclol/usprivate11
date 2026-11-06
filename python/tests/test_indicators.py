"""Tests for the pure technical-indicator math (indicators.py).

Deterministic, no network/DB — known-value checks for EMA/RSI plus
structural + cross-detection checks for MACD on constructed series.
"""
from __future__ import annotations

import math

import indicators as ind


# ───────── EMA ─────────────────────────────────────────────────────────


def test_ema_seed_and_recursion():
    # period-3 EMA of [1,2,3,4,5]: seed = SMA(1,2,3) = 2.0, k = 0.5
    #   -> 4*.5 + 2*.5 = 3.0 ; 5*.5 + 3*.5 = 4.0
    assert ind.ema_series([1, 2, 3, 4, 5], 3) == [2.0, 3.0, 4.0]


def test_ema_insufficient_history_empty():
    assert ind.ema_series([1, 2], 3) == []
    assert ind.ema_series([], 5) == []


# ───────── RSI ─────────────────────────────────────────────────────────


def test_rsi_all_gains_is_100():
    assert ind.rsi(list(range(1, 30)), period=14) == 100.0


def test_rsi_all_losses_is_0():
    assert ind.rsi(list(range(30, 1, -1)), period=14) == 0.0


def test_rsi_midrange_for_balanced_series():
    # alternating up/down of equal size sits near the 50 midline
    closes = []
    p = 100.0
    for i in range(40):
        p += 1.0 if i % 2 == 0 else -1.0
        closes.append(p)
    r = ind.rsi(closes, period=14)
    assert r is not None and 30.0 < r < 70.0


def test_rsi_insufficient_history_none():
    assert ind.rsi([1, 2, 3], period=14) is None


# ───────── MACD ────────────────────────────────────────────────────────


def test_macd_insufficient_history_none():
    # needs slow + signal = 26 + 9 = 35 points
    assert ind.macd(list(range(30))) is None


def test_macd_uptrend_positive_line():
    m = ind.macd([float(x) for x in range(60)])
    assert m is not None
    # a steady uptrend keeps the fast EMA above the slow EMA
    assert m["macd"] > 0
    assert m["cross"] in (-1, 0, 1)


def test_macd_cross_flag_fires_both_directions():
    # an oscillating series must produce both bullish (+1) and bearish (-1)
    # histogram crosses as the window slides across it.
    closes = [100.0 + 8.0 * math.sin(i / 4.0) for i in range(160)]
    seen = set()
    for end in range(40, len(closes)):
        m = ind.macd(closes[:end])
        if m:
            seen.add(m["cross"])
    assert 1 in seen and -1 in seen


def test_macd_cross_is_zero_in_steady_trend():
    # no sign flip on the final bar of a pure uptrend
    m = ind.macd([float(x) for x in range(80)])
    assert m is not None and m["cross"] == 0


# ───────── compute() bundle ────────────────────────────────────────────


_COMPUTE_KEYS = {
    "macd", "macdSignal", "macdHist", "macdCross", "rsi", "sigma1m",
    # trend / VWAP fields (Turbine strategy library)
    "vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
    "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
    "change5mPct", "change15mPct",
}


def test_compute_full_bundle_keys_present():
    out = ind.compute([100.0 + 5.0 * math.sin(i / 5.0) for i in range(120)])
    assert set(out) == _COMPUTE_KEYS
    assert out["macdHist"] is not None and out["rsi"] is not None
    # trend fields compute with 120 closes (full 1h VWAP + 15m change windows)
    assert out["vwap1h"] is not None and out["change15mPct"] is not None


def test_compute_insufficient_history_all_none():
    # No history at all → every field None (velocity1mPct needs only 2 points,
    # so [1,2,3] would legitimately populate it — use an empty series here).
    out = ind.compute([])
    assert set(out) == _COMPUTE_KEYS
    assert all(v is None for v in out.values())


def test_compute_ignores_non_numeric():
    # None / junk values are dropped, not crashed on
    closes = [100.0 + i for i in range(40)]
    closes_with_junk = []
    for i, c in enumerate(closes):
        closes_with_junk.append(c)
        if i % 10 == 0:
            closes_with_junk.append(None)
    out = ind.compute(closes_with_junk)
    assert out["rsi"] is not None
