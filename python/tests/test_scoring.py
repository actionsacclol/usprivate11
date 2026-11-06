"""Golden tests for the whale + momentum scoring functions.

These two functions are the heart of the scanner ("calibrated on 22k+
resolved Polymarket whale trades"). They're pure, so we pin their output for
a handful of hand-computed inputs. If a refactor shifts a score, these
break loudly instead of silently changing what the bot trades.
"""
from __future__ import annotations

import scanner


# ───────── compute_whale_score ─────────────────────────────────────────


def test_whale_score_basic_yes_favorite():
    # implied = 70; yes & price>=0.65 -> +4.5; $3k -> +0.5 => 75.0
    score = scanner.compute_whale_score(
        dollar_value=3000, price=0.70, taker_side="yes"
    )
    assert score == 75.0


def test_whale_score_no_side_with_all_bonuses():
    # implied 55; no & >=0.50 -> +3.5; $12k -> +1; vol250k -> +1.5;
    # oi 60k -> +0.5; <=1 day -> +0.5; crypto -> +1.0  => edge 8 => 63.0
    score = scanner.compute_whale_score(
        dollar_value=12_000, price=0.55, taker_side="no",
        market_volume=300_000, open_interest=60_000,
        days_to_close=0.5, category="crypto",
    )
    assert score == 63.0


def test_whale_score_clamps_to_ceiling_97():
    # implied 95 + positive edge would exceed 97 -> clamped to 97.0
    score = scanner.compute_whale_score(
        dollar_value=30_000, price=0.95, taker_side="yes",
        market_volume=300_000, open_interest=60_000,
    )
    assert score == 97.0


def test_whale_score_clamps_to_floor_5():
    # implied 5, yes & deep underdog -> -6 -> below floor -> 5.0
    score = scanner.compute_whale_score(
        dollar_value=100, price=0.05, taker_side="yes"
    )
    assert score == 5.0


# ───────── compute_momentum_confidence ─────────────────────────────────


def test_momentum_confidence_trade_cluster_underdog():
    # implied 20; trade_cluster & implied<25 -> +18; count6 -> +1;
    # $6k -> +1; spike3 -> +1; move0.10 -> +1; vol60k -> +1; oi12k -> +0.5;
    # <=7 days -> +0.5  => edge 24 -> clamp 8 => 28.0
    score = scanner.compute_momentum_confidence(
        volume_spike_ratio=3.0, price_change_abs=0.10,
        trade_cluster_count=6, trade_cluster_dollars=6_000,
        market_volume=60_000, open_interest=12_000,
        days_to_close=5, price=0.20, direction="yes",
        signal_type="trade_cluster",
    )
    assert score == 28.0


def test_momentum_confidence_no_signal_is_implied_only():
    # direction no, price 0.70 -> implied (1-0.7)*100 = 30; no bonuses
    score = scanner.compute_momentum_confidence(
        volume_spike_ratio=0.0, price_change_abs=0.0,
        trade_cluster_count=0, trade_cluster_dollars=0,
        market_volume=0, open_interest=0,
        days_to_close=None, price=0.70, direction="no",
        signal_type="",
    )
    assert score == 30.0


def test_momentum_confidence_never_exceeds_ceiling():
    # Pile on every bonus on a near-certain favorite; must cap at 97.0
    score = scanner.compute_momentum_confidence(
        volume_spike_ratio=10.0, price_change_abs=0.50,
        trade_cluster_count=50, trade_cluster_dollars=50_000,
        market_volume=1_000_000, open_interest=500_000,
        days_to_close=0.5, price=0.95, direction="yes",
        signal_type="trade_cluster", category="crypto",
    )
    assert score == 97.0
