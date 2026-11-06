"""Tests for the fee-aware signal backtest math (backtest.py)."""
from __future__ import annotations

import pytest

import backtest as bt


# Every category pays the venue's one rate, 0.0695.
def sig(cost, correct, confidence=60.0, source="whale"):
    return {"source": source, "confidence": confidence, "cost": cost,
            "correct": correct, "category": "crypto"}


def sig_ev(cost, correct, event, confidence=60.0, source="whale"):
    return {"source": source, "confidence": confidence, "cost": cost,
            "correct": correct, "category": "crypto", "event": event}


# ───────── fee model: fee v2, per-category taker rate × P·(1−P) ─────────


def test_fee_is_rate_times_p_one_minus_p():
    # 0.0695: peaks at 50c, zero at the poles.
    assert bt.polymarket_fee_per_contract(0.50, category="crypto") == pytest.approx(0.017375)
    assert bt.polymarket_fee_per_contract(0.0, category="crypto") == 0.0
    assert bt.polymarket_fee_per_contract(1.0, category="crypto") == 0.0
    # Uncategorised pays the same, as in trader.py.
    assert bt.polymarket_fee_per_contract(0.50) == pytest.approx(0.017375)


def test_fee_is_symmetric_and_vanishes_at_extremes():
    # The fee is symmetric in P: a 90c favorite pays the same as a 10c dog,
    # and far less than a 50c coin-flip.
    fee = lambda p: bt.polymarket_fee_per_contract(p, category="crypto")  # noqa: E731
    assert fee(0.9) == pytest.approx(fee(0.1))
    assert fee(0.9) < fee(0.5)
    assert fee(0.97) == pytest.approx(0.0695 * 0.97 * 0.03)


def test_fee_respects_category_and_override():
    # No category is cheaper on this venue; a forced rate still wins.
    assert bt.polymarket_fee_per_contract(0.50, category="sports") == pytest.approx(0.017375)
    assert bt.polymarket_fee_per_contract(0.50, category="geopolitics") == pytest.approx(0.017375)
    assert bt.polymarket_fee_per_contract(0.50, fee_coeff=0.05) == pytest.approx(0.0125)


# ───────── per-contract P&L ────────────────────────────────────────────


def test_net_pnl_win_subtracts_fee():
    # win at 30c: gross +0.70, fee 0.0695·0.30·0.70=0.014595 -> +0.685405
    assert bt.net_pnl_per_contract(0.30, True, category="crypto") == pytest.approx(0.685405)


def test_net_pnl_loss_adds_fee_to_loss():
    assert bt.net_pnl_per_contract(0.30, False, category="crypto") == pytest.approx(-0.314595)


# ───────── signal cost (side priced correctly) ────────────────────────


def test_signal_cost_whale_uses_taker_price():
    assert bt.signal_cost({"price": 0.62}, "whale") == pytest.approx(0.62)


def test_signal_cost_momentum_no_side_inverts():
    assert bt.signal_cost({"price": 0.30, "direction": "no"}, "momentum") == pytest.approx(0.70)
    assert bt.signal_cost({"price": 0.30, "direction": "yes"}, "momentum") == pytest.approx(0.30)


# ───────── aggregation ─────────────────────────────────────────────────


def test_fair_odds_lose_exactly_the_fee():
    # 50/50 at fair price 0.50: gross EV is zero, so net EV == -fee
    # (0.0695 × 0.50 × 0.50 = 0.017375).
    s = bt.summarize([sig(0.50, True), sig(0.50, False)])
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["gross_ev"] == pytest.approx(0.0)
    assert s["fee_ev"] == pytest.approx(0.017375)
    assert s["net_ev"] == pytest.approx(-0.017375)


def test_real_edge_survives_fees():
    # 10 signals at 0.50, 6 wins / 4 losses -> gross EV +0.10, net +0.082625
    signals = [sig(0.50, True) for _ in range(6)] + [sig(0.50, False) for _ in range(4)]
    s = bt.summarize(signals)
    assert s["n"] == 10
    assert s["gross_ev"] == pytest.approx(0.10)
    assert s["fee_ev"] == pytest.approx(0.017375)
    assert s["net_ev"] == pytest.approx(0.082625)
    assert s["t"] > 0


def test_empty_summary_is_zeroed():
    s = bt.summarize([])
    assert s["n"] == 0 and s["net_ev"] == 0.0


# ───────── threshold sweep ─────────────────────────────────────────────


def test_threshold_sweep_filters_by_confidence():
    signals = [sig(0.5, True, confidence=60), sig(0.5, True, confidence=80)]
    rows = bt.threshold_sweep(signals, [50, 70, 90])
    by_th = {r["threshold"]: r["n"] for r in rows}
    assert by_th == {50: 2, 70: 1, 90: 0}


# ───────── clustered (per-event) significance ─────────────────────────


def test_solo_signals_have_per_event_t_equal_to_per_signal_t():
    # No event labels -> each signal is its own event -> t_event == t.
    signals = [sig(0.50, i < 6) for i in range(10)]
    s = bt.summarize(signals)
    assert s["n_events"] == 10
    assert s["t_event"] == pytest.approx(s["t"])


def test_clustering_deflates_correlated_significance():
    # 10 markets, 5 perfectly-correlated signals each (one match -> one
    # outcome). 6 markets win. The per-signal t treats all 50 as independent;
    # the per-event t sees only 10 real bets and is much smaller.
    clustered, independent = [], []
    for e in range(10):
        won = e < 6
        for _ in range(5):
            clustered.append(sig_ev(0.50, won, f"E{e}"))
    for i, x in enumerate(clustered):
        independent.append(sig_ev(x["cost"], x["correct"], f"solo{i}"))

    c = bt.summarize(clustered)
    ind = bt.summarize(independent)
    assert c["n"] == 50 and c["n_events"] == 10
    assert ind["n_events"] == 50
    # Same 50 outcomes -> identical per-signal t and net_ev...
    assert c["t"] == pytest.approx(ind["t"])
    assert c["net_ev"] == pytest.approx(ind["net_ev"])
    # ...but correlation collapses the honest (per-event) significance.
    assert abs(c["t_event"]) < abs(ind["t_event"])
    assert ind["t_event"] == pytest.approx(ind["t"])  # independent -> equal


def test_verdict_inconclusive_when_too_few_events():
    # 100 signals but only 5 markets — not enough independent outcomes to trust,
    # even though the per-signal sample looks large.
    signals = [sig_ev(0.50, e < 3, f"E{e}") for e in range(5) for _ in range(20)]
    rep = bt.build_report(signals, bt.DEFAULT_FEE_COEFF)
    assert "INCONCLUSIVE" in rep["verdict"]
    assert "events" in rep["verdict"]


# ───────── verdict guardrails ──────────────────────────────────────────


def test_verdict_flags_small_samples_inconclusive():
    s = bt.summarize([sig(0.5, True)])
    assert "INCONCLUSIVE" in bt.verdict(s, [])


def test_verdict_calls_negative_when_net_negative():
    # large fair-odds sample -> net negative (just fees), significant
    signals = [sig(0.5, i % 2 == 0) for i in range(200)]
    rep = bt.build_report(signals, bt.DEFAULT_FEE_COEFF)
    assert "NEGATIVE" in rep["verdict"]


def test_taker_rebate_tier_nets_fee_estimates():
    import backtest as bt
    import crypto15m
    base = bt.polymarket_fee_per_contract(0.5, category="crypto")
    try:
        bt.set_taker_rebate(15)
        crypto15m.set_taker_rebate(15)
        assert bt.polymarket_fee_per_contract(0.5, category="crypto") == pytest.approx(base * 0.85)
        assert crypto15m._fee_cents(50.0) == pytest.approx(crypto15m.FEE_RATE_CRYPTO * 0.25 * 100 * 0.85)
        bt.set_taker_rebate(99)          # clamped to the top tier
        assert bt.TAKER_REBATE == pytest.approx(0.3)
    finally:
        bt.set_taker_rebate(0)
        crypto15m.set_taker_rebate(0)
    assert bt.polymarket_fee_per_contract(0.5, category="crypto") == pytest.approx(base)


# ───────── spread: a follower never gets the print ─────────────────────
# Audit 2026-10-11: the whale/momentum report charged zero spread, so every
# number assumed the follower filled at the whale's own post-impact print.


def test_report_charges_a_one_cent_spread_by_default():
    signals = [sig(0.50, True) for _ in range(6)] + [sig(0.50, False) for _ in range(4)]
    rep = bt.build_report(signals, None)
    assert rep["spread_cents"] == 1.0
    # paid 51c: gross (6·0.49 − 4·0.51)/10 = +0.09, fee at 51c
    fee = bt.polymarket_fee_per_contract(0.51, category="crypto")
    assert rep["overall"]["gross_ev"] == pytest.approx(0.09)
    assert rep["overall"]["net_ev"] == pytest.approx(0.09 - fee)


def test_report_prints_a_spread_sweep():
    signals = [sig(0.50, i % 3 != 0) for i in range(60)]
    rep = bt.build_report(signals, None)
    by = {r["spread_cents"]: r["net_ev"] for r in rep["spread_sweep"]}
    assert sorted(by) == [0.0, 1.0, 2.0]
    assert by[0.0] > by[1.0] > by[2.0]
    assert by[1.0] == pytest.approx(rep["overall"]["net_ev"])
    text = bt.format_report(rep, "test")
    assert "SPREAD / SLIPPAGE SWEEP" in text
    assert "+ 1¢ spread" in text


def test_fading_pays_the_spread_on_the_other_side():
    # Faded cost is (1 − 0.30) + 1¢ = 71¢, not 70¢ − 1¢.
    f = bt.summarize_fade([sig(0.30, False)], spread_cents=1.0)
    assert f["avg_cost"] == pytest.approx(0.71)
