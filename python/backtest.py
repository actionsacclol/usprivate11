"""Fee-aware signal backtest for Krypt PolyBot US.

Answers the only question that matters before hardening anything else:
**do the scanner's signals have positive expectancy after Polymarket fees?**

It replays every *resolved* signal the scanners recorded (`whale_trades`
and `alerts`), computes the per-contract P&L of having followed it at the
recorded entry price given the binary outcome, **subtracts an estimate of
the Polymarket trading fee**, and reports net edge with a significance check
and a confidence-threshold sweep.

This is a realized-signal backtest, not an academic one: it measures the
net-of-fee edge of the signals this bot actually produces. It does NOT
prove forward performance (no look-ahead controls, the scoring is the
same one that generated the signals), but it is the honest first cut at
"does this make money after costs."

    python backtest.py                 # auto-find your Krypt PolyBot US DB
    python backtest.py --db PATH       # explicit DB
    python backtest.py --fee 0.0695    # override the taker-fee rate coefficient
    python backtest.py --spread 2      # cents paid over each signal's price (default 1)
    python backtest.py --demo          # synthetic data, to see the format

FEE MODEL (docs.polymarket.us/fees, effective 2026-10-28, checked 2026-11-05):
TAKERS pay rate × P·(1−P) per contract, a symmetric curve peaking at mid prices
and vanishing at the extremes. The rate is 0.0695 on every market except Table
Tennis (0.10). Makers pay nothing; maker rebates start at $10M of maker volume.
Every market stamps its own feeCoefficient, which wins wherever a caller has the
market; records that carry only a category use 0.0695. --fee forces a single rate
coefficient for everything.
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

# Polymarket US TAKER rate: one coefficient across categories (docs fees.md, effective
# 2026-10-28; 200 of 200 live markets stamped it on 2026-11-05). Fee per contract =
# rate × P·(1−P), symmetric, peaking at 50c. Table Tennis alone is 0.10, and no category
# here can name it, so a market's own feeCoefficient beats this table wherever we have one.
US_TAKER_RATE = 0.0695
CATEGORY_TAKER_FEE: dict[str, float] = {
    category: US_TAKER_RATE for category in (
        "crypto", "sports", "finance", "politics", "mentions", "tech", "economics",
        "culture", "weather", "other", "geopolitics",
    )
}
# The bot's own category ids (categorize.py) that are not names on the fee
# schedule, mapped to the schedule entry that covers them. Without this every
# climate / entertainment / world signal missed the table and was charged
# the wrong rate in the backtests (and quoted "fee unknown" in the Terminal).
CATEGORY_FEE_ALIAS: dict[str, str] = {
    "climate": "weather",
    "entertainment": "culture",
    "world": "other",
}
# The ONE fallback for a category the schedule doesn't know (blank, or a label
# we can't map), shared by the live accounting (trader.py) and the backtests.
FALLBACK_FEE_CATEGORY = "other"
DEFAULT_FEE_RATE = CATEGORY_TAKER_FEE[FALLBACK_FEE_CATEGORY]
# Back-compat alias: `fee_coeff` params mean "the rate coefficient".
DEFAULT_FEE_COEFF = DEFAULT_FEE_RATE


def fee_category(category) -> Optional[str]:
    """The fee-schedule category `category` falls under, or None when it is
    blank or not one we know (callers that must not guess check for None)."""
    cat = str(category or "").strip().lower()
    cat = CATEGORY_FEE_ALIAS.get(cat, cat)
    return cat if cat in CATEGORY_TAKER_FEE else None


def taker_fee_rate(category) -> float:
    """Taker fee-v2 rate for `category`, with the shared fallback."""
    return CATEGORY_TAKER_FEE[fee_category(category) or FALLBACK_FEE_CATEGORY]


# ───────── fee + per-signal economics (pure) ──────────────────────────


# Taker rebates (docs fees.md): a weekly rebate of 3-30% of the taker fees paid, by
# 30-day taker volume, starting at $250K. Set from the user's tier
# (`taker_rebate_pct`) so every FORWARD-LOOKING fee estimate — live edge gates,
# backtests, replays — nets it out. Realised P&L stays on fees actually paid:
# Polymarket US pays the rebate separately, as income.
TAKER_REBATE_TIERS = (0.0, 3.0, 5.0, 10.0, 15.0, 30.0)
TAKER_REBATE = 0.0


def set_taker_rebate(pct) -> None:
    global TAKER_REBATE
    try:
        TAKER_REBATE = max(0.0, min(TAKER_REBATE_TIERS[-1], float(pct or 0.0))) / 100.0
    except (TypeError, ValueError):
        TAKER_REBATE = 0.0


def venue_fee_per_contract(
    price: float, fee_coeff: Optional[float] = None, category: str = "",
) -> float:
    """What Polymarket CHARGES per contract on a taker fill at `price`
    (dollars, 0..1): rate × P·(1−P), the full fee-v2 curve. Realised P&L
    books this one: the taker rebate is paid back separately, once a day in
    pUSD, so it is income on top, not a smaller fee at match time.
    `fee_coeff` forces a rate; otherwise the category sets it (taker_fee_rate)."""
    p = max(0.0, min(1.0, float(price)))
    rate = float(fee_coeff) if fee_coeff is not None else taker_fee_rate(category)
    return rate * p * (1.0 - p)


def polymarket_fee_per_contract(
    price: float, fee_coeff: Optional[float] = None, category: str = "",
) -> float:
    """Per-contract Polymarket TAKER fee at execution price `price` for a
    FORWARD-LOOKING estimate (edge gates, backtests, replays): the venue fee
    net of the user's taker-rebate tier. Realised accounting uses
    venue_fee_per_contract instead."""
    return venue_fee_per_contract(price, fee_coeff, category) * (1.0 - TAKER_REBATE)


def net_pnl_per_contract(
    cost: float, correct: bool, fee_coeff: Optional[float] = None,
    category: str = "",
) -> float:
    """Net P&L of one $1 binary contract bought at `cost` (the price paid
    for the chosen side), settled at $1 if `correct` else $0, minus the
    trading fee."""
    gross = (1.0 - cost) if correct else (-cost)
    return gross - polymarket_fee_per_contract(cost, fee_coeff, category)


def signal_cost(row: dict, source: str) -> float:
    """Price paid (0..1) for the side the signal points at."""
    price = float(row.get("price") or 0.0)
    if source == "whale":
        # whale_trades.price is already the taker side's execution price.
        return max(0.0, min(1.0, price))
    # momentum: alerts.price is the YES price; cost depends on direction.
    direction = (row.get("direction") or "yes").lower()
    cost = price if direction == "yes" else (1.0 - price)
    return max(0.0, min(1.0, cost))


# ───────── aggregation (pure) ─────────────────────────────────────────


def _cluster_key(s: dict, idx: int):
    """Grouping key for clustered significance. A labelled event groups all its
    signals; a blank/absent event makes the signal its own cluster (unique key)
    so unlabelled data degrades gracefully to the per-signal t rather than
    collapsing every blank into one giant fake cluster."""
    ev = s.get("event")
    return ev if ev else ("\0__solo__", idx)


def _event_t(nets: list[float], keys: list) -> tuple[int, float, float]:
    """The EVENT-weighted view: each event's mean return counts once.
    Returns (n_events, mean of the event means, t across the event means).
    Its mean is not the per-signal headline (an event with 30 signals counts
    as much as one with 1), so its t belongs next to that mean, not next to
    the per-signal net_ev. Equals the per-signal figures when every signal is
    its own event."""
    groups: dict = defaultdict(list)
    for net, k in zip(nets, keys):
        groups[k].append(net)
    means = [sum(v) / len(v) for v in groups.values()]
    ne = len(means)
    if ne == 0:
        return 0, 0.0, 0.0
    m = sum(means) / ne
    if ne < 2:
        return ne, m, 0.0
    var = sum((x - m) ** 2 for x in means) / (ne - 1)
    se = math.sqrt(var) / math.sqrt(ne)
    return ne, m, ((m / se) if se > 0 else 0.0)


def cluster_robust_t(values: list[float], keys: list) -> tuple[int, Optional[float]]:
    """t-statistic of the plain (per-trade) mean of `values`, with a
    cluster-robust standard error: each cluster's deviations from the mean
    are summed, and the variance is taken over those cluster sums (CR1, with
    the G/(G−1) small-sample factor). Trades in one cluster (signals on one
    event, crypto windows closing at the same moment, ~7 coins that move
    together) count as ONE draw, not seven. Returns (n_clusters, t); t is
    None with fewer than two clusters or no variance. With every trade its
    own cluster this is exactly the ordinary t; with equal-size clusters it
    equals the t across cluster totals.

    The one significance helper for the backtest, the crypto replays and the
    script backtester."""
    n = len(values)
    if n < 2:
        return len(set(keys)), None
    mean = sum(values) / n
    sums: dict = defaultdict(float)
    for v, k in zip(values, keys):
        sums[k] += v - mean
    g = len(sums)
    if g < 2:
        return g, None
    var_mean = g / (g - 1) * sum(s * s for s in sums.values()) / (n * n)
    if var_mean <= 1e-18:
        return g, None
    return g, mean / math.sqrt(var_mean)


def _paid(cost: float, spread_cents: float) -> float:
    """What a follower pays: the recorded price plus `spread_cents` of
    spread/slippage, capped at 99¢ (a 100¢ contract can only lose)."""
    return min(0.99, float(cost) + spread_cents / 100.0) if spread_cents else float(cost)


def summarize(signals: list[dict], fee_coeff: Optional[float] = None,
              spread_cents: float = 0.0) -> dict:
    """Aggregate net-of-fee economics for a list of signals.

    `spread_cents` is added to every recorded price before fees: a follower
    buys at the ask after the signal printed, never at the print itself.

    Each signal dict needs: cost (float), correct (bool), confidence (float);
    optionally `event` for clustered significance (the event ticker, or a
    crypto window's close time). Two means, each with its own t:
      * `net_ev` (the headline): mean net P&L per contract over SIGNALS,
        and `t_cluster`, its t with an event-clustered standard error
        (cluster_robust_t) — the one to trust, because signals on one market
        are not independent bets. `t` is the same mean's naive per-signal t.
      * `net_ev_event`: the mean over EVENTS (each event's mean counts once),
        and `t_event`, the t across those event means.
    |t| < 2 ≈ noise."""
    n = len(signals)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": 0.0, "gross_ev": 0.0,
                "fee_ev": 0.0, "net_ev": 0.0, "se": 0.0, "t": 0.0,
                "total_net": 0.0, "avg_cost": 0.0, "net_roi": 0.0,
                "n_events": 0, "t_event": 0.0, "net_ev_event": 0.0,
                "t_cluster": 0.0}

    nets, grosses, fees, keys = [], [], [], []
    wins = 0
    costs = []
    for i, s in enumerate(signals):
        cost, correct = _paid(s["cost"], spread_cents), bool(s["correct"])
        costs.append(cost)
        if correct:
            wins += 1
        grosses.append((1.0 - cost) if correct else (-cost))
        cat = str(s.get("category") or "")
        fees.append(polymarket_fee_per_contract(cost, fee_coeff, cat))
        nets.append(net_pnl_per_contract(cost, correct, fee_coeff, cat))
        keys.append(_cluster_key(s, i))

    mean_net = sum(nets) / n
    var = sum((x - mean_net) ** 2 for x in nets) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    se = sd / math.sqrt(n) if n > 0 else 0.0
    avg_cost = sum(costs) / n
    n_events, mean_event, t_event = _event_t(nets, keys)
    _g, t_cluster = cluster_robust_t(nets, keys)
    return {
        "n": n, "wins": wins, "win_rate": wins / n,
        "gross_ev": sum(grosses) / n,
        "fee_ev": sum(fees) / n,
        "net_ev": mean_net,
        "se": se,
        "t": (mean_net / se) if se > 0 else 0.0,
        "t_cluster": t_cluster if t_cluster is not None else 0.0,
        "total_net": sum(nets),
        "avg_cost": avg_cost,
        "net_roi": (mean_net / avg_cost) if avg_cost > 0 else 0.0,
        "n_events": n_events,
        "net_ev_event": mean_event, "t_event": t_event,
    }


def threshold_sweep(
    signals: list[dict], thresholds: list[float], fee_coeff: Optional[float] = None,
    spread_cents: float = 0.0,
) -> list[dict]:
    """Net economics when filtered to confidence >= each threshold —
    shows whether the score actually selects for edge."""
    out = []
    for th in thresholds:
        sub = [s for s in signals if s["confidence"] >= th]
        out.append({"threshold": th, **summarize(sub, fee_coeff, spread_cents)})
    return out


PRICE_BUCKETS = [
    (0.00, 0.30, "<30c"), (0.30, 0.50, "30-50c"), (0.50, 0.70, "50-70c"),
    (0.70, 0.85, "70-85c"), (0.85, 1.01, "85c+"),
]


def _price_label(cost: float) -> str:
    for lo, hi, label in PRICE_BUCKETS:
        if lo <= cost < hi:
            return label
    return "?"


def summarize_fade(signals: list[dict], fee_coeff: Optional[float] = None,
                   spread_cents: float = 0.0) -> dict:
    """What you'd net by betting the OPPOSITE side of each signal (cost
    becomes 1-cost, win becomes loss), paying the same `spread_cents` on the
    opposite side. Approximate — the other side's real spread can differ —
    but a strong tell for whether the signal is anti-predictive."""
    faded = [{"cost": 1.0 - s["cost"], "correct": (not s["correct"]),
              "confidence": s["confidence"], "category": s.get("category", ""),
              "event": s.get("event", "")} for s in signals]
    return summarize(faded, fee_coeff, spread_cents)


def group_summaries(signals: list[dict], key, fee_coeff: Optional[float] = None,
                    spread_cents: float = 0.0) -> dict:
    groups: dict[str, list] = {}
    for s in signals:
        groups.setdefault(key(s), []).append(s)
    return {k: summarize(v, fee_coeff, spread_cents) for k, v in groups.items()}


def format_breakdown(signals: list[dict], fee_coeff: float,
                     spread_cents: float = 0.0) -> str:
    L = ["", "════════════ BREAKDOWN ════════════"]
    for src in ("whale", "momentum"):
        sub = [s for s in signals if s["source"] == src]
        if not sub:
            continue
        L.append("")
        L.append(f"── {src.upper()}  (n={len(sub)}) ──")
        L.append("  by entry price:            (t clustered by event)")
        pb = group_summaries(sub, lambda s: _price_label(s["cost"]), fee_coeff, spread_cents)
        for _, _, label in PRICE_BUCKETS:
            if label in pb:
                r = pb[label]
                L.append(f"    {label:<7} n={r['n']:<5} ev={r['n_events']:<4} win {r['win_rate']*100:4.0f}%  "
                         f"net {_money(r['net_ev'])} (t={r['t_cluster']:+.1f})")
        cb = group_summaries([s for s in sub if s["category"]], lambda s: s["category"],
                             fee_coeff, spread_cents)
        if cb:
            L.append("  by category (top 6 by n):    (t clustered by event)")
            for cat, r in sorted(cb.items(), key=lambda kv: -kv[1]["n"])[:6]:
                L.append(f"    {cat:<13} n={r['n']:<5} ev={r['n_events']:<4} win {r['win_rate']*100:4.0f}%  "
                         f"net {_money(r['net_ev'])} (t={r['t_cluster']:+.1f})")
        follow = summarize(sub, fee_coeff, spread_cents)["net_ev"]
        fade = summarize_fade(sub, fee_coeff, spread_cents)
        L.append(f"  FADE (bet opposite): net {_money(fade['net_ev'])} (t={fade['t_cluster']:+.1f} clustered)   "
                 f"vs follow {_money(follow)}")
    return "\n".join(L)


def verdict(overall: dict, sweep: list[dict]) -> str:
    # Significance is judged on the EVENT-CLUSTERED t of the headline mean
    # (t_cluster), never the per-signal t: many signals settle on one market
    # outcome, so the per-signal t over-counts independent bets and turns
    # clustered noise into "significant". t_event belongs to a different mean
    # (net_ev_event), so it is not the test for net_ev.
    if overall["n"] < 30:
        return ("INCONCLUSIVE — too few resolved signals (need ~100+). "
                "Run the bot longer to accumulate data.")
    if overall.get("n_events", overall["n"]) < 10:
        return (f"INCONCLUSIVE — {overall['n']} signals but only "
                f"{overall.get('n_events', 0)} distinct events. The edge rests on too "
                f"few independent outcomes to trust; need ~30+ events.")
    best = max(
        (r for r in sweep if r["n"] >= 30 and r.get("n_events", 0) >= 10),
        key=lambda r: r["net_ev"], default=None,
    )
    if overall["net_ev"] > 0 and overall["t_cluster"] > 2:
        return (f"POSITIVE net-of-fee edge, significant across {overall['n_events']} "
                f"events (event-clustered t={overall['t_cluster']:+.1f}). Worth hardening.")
    if best and best["net_ev"] > 0 and best["t_cluster"] > 2:
        return (f"Net edge appears only when filtered to confidence >= {best['threshold']:.0f} "
                f"(net +${best['net_ev']:.4f}/contract, event-clustered t={best['t_cluster']:.1f}). "
                f"Tighten the gate — but beware in-sample overfitting to that threshold.")
    if overall["net_ev"] > 0:
        return (f"Marginally positive but WITHIN NOISE (event-clustered t={overall['t_cluster']:+.1f}, "
                f"need >2). The per-signal t={overall['t']:+.1f} looks better only because it "
                f"over-counts clustered signals as independent. Not distinguishable from zero.")
    return ("NEGATIVE after fees. The signals do not have a net-of-fee edge as configured — "
            "fix the strategy/fees before trading real money.")


# ───────── data loading ───────────────────────────────────────────────


def _with_archive(conn: sqlite3.Connection, table: str) -> str:
    """`table` plus the rows retention moved into the research archive beside
    this DB file (db.research_source), so a report covers the whole recorded
    corpus rather than the last 45-60 days."""
    import db
    try:
        return db.research_source(conn, table)
    except sqlite3.Error:
        return table


def load_resolved_signals(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    out: list[dict] = []
    for src, table in (("whale", "whale_trades"), ("momentum", "alerts")):
        try:
            rows = conn.execute(
                f"SELECT * FROM {_with_archive(conn, table)} "
                f"WHERE resolved=1 AND outcome_correct IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for raw in rows:
            r = dict(raw)
            out.append({
                "source": src,
                "confidence": float(r.get("confidence") or 0.0),
                "cost": signal_cost(r, src),
                "correct": int(r["outcome_correct"]) == 1,
                "category": r.get("category") or "",
                # Event identity for clustered significance: many signals share
                # ONE market outcome (30 whale prints on one match settle
                # together), so they are NOT independent draws. `event_ticker`
                # groups them; title is the fallback, and a blank means "treat
                # this signal as its own event" (see _cluster_key).
                "event": r.get("event_ticker") or r.get("title") or "",
            })
    return out


def resolve_db_path(arg_db: Optional[str]) -> Path:
    if arg_db:
        return Path(arg_db)
    env = os.environ.get("KRYPT_POLYBOT_US_USERDATA")
    if env:
        return Path(env) / "data" / "krypt-polybot-us.db"
    appdata = os.environ.get("APPDATA")  # Windows default install location
    if appdata:
        p = Path(appdata) / "Krypt PolyBot US" / "data" / "krypt-polybot-us.db"
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "data" / "krypt-polybot-us.db"


# ───────── report ─────────────────────────────────────────────────────


# A follower never gets the whale's own print: it buys at the ask after the
# print moved the book. The report used to charge zero spread, which flattered
# every number by roughly a cent per contract (RELEASE_AUDIT 2026-09-01).
# Same 1¢ the in-app replay (replay.replay_main) charges.
DEFAULT_SPREAD_CENTS = 1.0
SPREAD_SWEEP_CENTS = (0.0, 1.0, 2.0)


def build_report(signals: list[dict], fee_coeff: float,
                 spread_cents: float = DEFAULT_SPREAD_CENTS) -> dict:
    thresholds = [50, 55, 60, 65, 70, 75, 80, 85, 90]
    overall = summarize(signals, fee_coeff, spread_cents)
    by_source = {
        src: summarize([s for s in signals if s["source"] == src], fee_coeff, spread_cents)
        for src in ("whale", "momentum")
    }
    sweep = threshold_sweep(signals, thresholds, fee_coeff, spread_cents)
    spread_sweep = [
        {"spread_cents": sc, **summarize(signals, fee_coeff, sc)}
        for sc in sorted({*SPREAD_SWEEP_CENTS, float(spread_cents)})
    ]
    return {
        "overall": overall, "by_source": by_source, "sweep": sweep,
        "spread_cents": spread_cents, "spread_sweep": spread_sweep,
        "verdict": verdict(overall, sweep), "fee_coeff": fee_coeff,
        "notes": [
            "Models entry → hold-to-settlement only. Per-position take-profit "
            "(take_profit_pct) is NOT simulated here: main-bot markets record a "
            "single entry price per signal with no post-entry price series, so "
            "there's no way to know when a bid would have crossed the target. The "
            "crypto backtest (replay) DOES model take-profit from recorded ticks; "
            "for the main bot, watch live exit_reason='take_profit' rows in History.",
        ],
    }


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):.4f}"


def format_report(report: dict, source_label: str) -> str:
    o = report["overall"]
    L = []
    L.append("═══ Krypt PolyBot US — fee-aware signal backtest ═══")
    L.append(f"source: {source_label}")
    _fc = report.get("fee_coeff")
    L.append(
        "fee model: per-category taker fee, rate × P·(1−P)"
        + (f" (forced rate {float(_fc):.4f})" if _fc is not None
           else " ('other' 0.05 when uncategorised)")
    )
    L.append(f"entry: the recorded signal price + {report.get('spread_cents', 0.0):g}¢ "
             "spread/slippage (--spread)")
    n_w = sum(1 for k in ('whale',) for _ in [0]) if False else report['by_source']['whale']['n']
    n_m = report['by_source']['momentum']['n']
    L.append(f"resolved signals: {o['n']}  (whale {n_w}, momentum {n_m})")
    L.append("")
    if o["n"] == 0:
        L.append("No resolved signals found. Run the bot (even in demo/dry-run) long")
        L.append("enough for signals to settle, then re-run this backtest.")
        return "\n".join(L)

    L.append("OVERALL (1 contract per signal)")
    L.append(f"  win rate            {o['win_rate']*100:5.1f}%")
    L.append(f"  gross edge/contract {_money(o['gross_ev'])}")
    L.append(f"  fees/contract       {_money(-o['fee_ev'])}")
    L.append(f"  NET edge/contract   {_money(o['net_ev'])} per signal   "
             f"t = {o['t_cluster']:+.1f} clustered by event ({o['n_events']} events)"
             f"   [per-signal t = {o['t']:+.1f}, over-counts]")
    L.append(f"                      {_money(o['net_ev_event'])} per event    "
             f"t = {o['t_event']:+.1f} across event means")
    L.append(f"  net ROI on cost     {o['net_roi']*100:+.1f}%")
    L.append(f"  total net P&L       {_money(o['total_net'])}")
    L.append("  → trust the clustered t: signals on one market settle together,")
    L.append("    so they are not independent bets. Per-signal t runs high on noise.")
    L.append("    The per-event line weights every event once, however many signals it printed.")
    L.append("")
    L.append("BY SOURCE")
    for src in ("whale", "momentum"):
        s = report["by_source"][src]
        if s["n"] == 0:
            L.append(f"  {src:<9} (none)")
            continue
        L.append(f"  {src:<9} n={s['n']:<5} ev={s['n_events']:<4} win {s['win_rate']*100:4.0f}%  "
                 f"net {_money(s['net_ev'])}  t {s['t_cluster']:+.1f}  "
                 f"(t/sig {s['t']:+.1f})")
    L.append("")
    L.append("CONFIDENCE THRESHOLD SWEEP")
    L.append("  conf≥   n     events  win    net/contract   t(clustered)  (t/sig)")
    for r in report["sweep"]:
        if r["n"] == 0:
            continue
        L.append(f"  {r['threshold']:>4.0f}   {r['n']:<6}{r['n_events']:<7} {r['win_rate']*100:4.0f}%   "
                 f"{_money(r['net_ev']):>9}    {r['t_cluster']:+.1f}          ({r['t']:+.1f})")
    if report.get("spread_sweep"):
        L.append("")
        L.append("SPREAD / SLIPPAGE SWEEP (all signals; ¢ added to every entry)")
        L.append("  spread  net/contract   t(clustered)")
        for r in report["spread_sweep"]:
            L.append(f"  {r['spread_cents']:>4g}¢   {_money(r['net_ev']):>9}      {r['t_cluster']:+.1f}")
    L.append("")
    L.append(f"VERDICT: {report['verdict']}")
    for note in report.get("notes", []):
        L.append("")
        L.append(f"NOTE: {note}")
    return "\n".join(L)


# ───────── demo data (synthetic, for showing the format) ──────────────


def demo_signals(n: int = 4000, seed: int = 42) -> list[dict]:
    """Synthetic resolved signals with a small genuine edge, so the report
    runs end-to-end. NOT real data — labeled clearly in the CLI."""
    import random
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        src = "whale" if rng.random() < 0.6 else "momentum"
        cost = rng.uniform(0.18, 0.88)
        # A small real edge that grows with the model's confidence:
        edge = rng.gauss(0.012, 0.035)            # mean +1.2pp, noisy
        true_prob = max(0.02, min(0.98, cost + edge))
        correct = rng.random() < true_prob
        confidence = max(5.0, min(97.0, true_prob * 100 + rng.gauss(0, 6)))
        out.append({"source": src, "confidence": confidence, "cost": cost,
                    "correct": correct, "category": ""})
    return out


# ───────── 15-minute crypto strategy backtest ─────────────────────────
# Replays the passively-collected `crypto15m_signals` (decision-point
# favorite + price + delta, plus the settled outcome) so users can see
# which 15m settings actually have a net-of-fee edge on their own data.


def load_crypto15m_signals(conn: sqlite3.Connection, interval: str = "15m") -> list[dict]:
    conn.row_factory = sqlite3.Row
    try:
        # One window series per report — mixed 5m/15m/hourly rows pooled into a
        # single win-rate/edge table would tune thresholds against contaminated
        # numbers (5m decision points have a different favorite-price
        # distribution). Pre-column rows are all 15m, hence the COALESCE.
        rows = conn.execute(
            f"""SELECT * FROM {_with_archive(conn, "crypto15m_signals")}
               WHERE resolved=1 AND up_won IS NOT NULL
                 AND COALESCE(interval, '15m') = ?""",
            (interval,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict] = []
    for raw in rows:
        r = dict(raw)
        fav = (r.get("favorite") or "").lower()
        if fav not in ("up", "down"):
            continue
        up_won = int(r["up_won"]) == 1
        fav_won = (fav == "up" and up_won) or (fav == "down" and not up_won)
        try:
            fav_price = float(r.get("favorite_price") or 0.0)
        except (TypeError, ValueError):
            fav_price = 0.0
        try:
            entry_cost = float(r.get("entry_cost") or fav_price)
        except (TypeError, ValueError):
            entry_cost = fav_price
        dp = r.get("delta_pct")

        def _num(key):
            v = r.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        out.append({
            "favorite": fav,
            "favorite_price": max(0.0, min(1.0, fav_price)),
            "entry_cost": max(0.0, min(1.0, entry_cost)),
            "fav_won": fav_won,
            "delta_pct": float(dp) if dp is not None else None,
            "mins_left": float(r["mins_left"]) if r.get("mins_left") is not None else None,
            "macd_hist": _num("macd_hist"),
            "macd_cross": _num("macd_cross"),
            "rsi": _num("rsi"),
            # The significance cluster: every coin's window closing at the
            # same moment (~7 per close) settles on one correlated move.
            "close_time": str(r.get("close_time") or ""),
        })
    return out


def crypto15m_eval(
    signals: list[dict], *, mode: str = "favorite", min_fav: float = 0.0,
    max_fav: float = 1.0, min_delta_pct: float = 0.0,
    fee_coeff: Optional[float] = None,
) -> dict:
    """Apply a 15m strategy variant and return its net-of-fee economics.

    `mode`: 'favorite' buys the favorite at its cost; 'contrarian' buys the
    cheap opposite side (cost ≈ 1 − favorite price, approximate — ignores
    the spread). Filters on the favorite price band and the underlying
    delta. Reuses summarize() so the stats match the main backtest."""
    built: list[dict] = []
    for s in signals:
        fp = s["favorite_price"]
        if fp < min_fav or fp > max_fav:
            continue
        if min_delta_pct > 0:
            dp = s["delta_pct"]
            if dp is None or dp * 100.0 < min_delta_pct:
                continue
        if mode == "contrarian":
            # Price the cheap side symmetrically with the favorite: `1 - fp` is
            # the opposite side's MID, but the favorite path pays its real ASK
            # (entry_cost), which sits a half-spread above its own mid. Charging
            # contrarian the mid while charging favorite the ask made contrarian
            # look cheaper than it fills. Add back the favorite's observed
            # half-spread (entry_cost - fp) as a symmetric proxy for the cheap
            # side's own spread.
            spread = max(0.0, float(s["entry_cost"]) - fp) if s.get("entry_cost") else 0.0
            cost = max(0.0, min(1.0, (1.0 - fp) + spread))
            correct = not s["fav_won"]
        else:
            cost = s["entry_cost"]
            correct = s["fav_won"]
        built.append({"cost": cost, "correct": correct, "confidence": fp * 100.0,
                      "category": "crypto", "event": s.get("close_time") or ""})
    return summarize(built, fee_coeff)


def _macd_aligns(s: dict) -> Optional[bool]:
    """Does the underlying MACD histogram point the SAME way as the favored
    side? True = bullish hist + up favored (or bearish + down), False =
    conflict, None = no MACD recorded or a flat histogram. This is the
    side-aware cut: if MACD adds information, favorite-follow should fare
    better on the 'aligned' subset than the 'conflict' subset."""
    mh = s.get("macd_hist")
    if mh is None or mh == 0:
        return None
    return (s.get("favorite") == "up") == (mh > 0)


def crypto15m_macd_breakdown(
    signals: list[dict], fee_coeff: Optional[float] = None
) -> dict:
    """Favorite-follow economics split by whether the underlying MACD agrees
    with the favored side — the honest first read on whether MACD helps."""
    # classifiable = a non-flat histogram (a flat 0.0 is recorded but has no
    # direction), so nWithMacd == agree + conflict by construction.
    classifiable = [s for s in signals if _macd_aligns(s) is not None]
    agree = [s for s in classifiable if _macd_aligns(s) is True]
    conflict = [s for s in classifiable if _macd_aligns(s) is False]
    return {
        "nWithMacd": len(classifiable),
        "agree": crypto15m_eval(agree, mode="favorite", fee_coeff=fee_coeff),
        "conflict": crypto15m_eval(conflict, mode="favorite", fee_coeff=fee_coeff),
    }


_FAV_THRESHOLDS = [50, 60, 70, 80, 85, 90, 95]
_DELTA_THRESHOLDS = [0.0, 0.05, 0.10, 0.20, 0.40]
# Disjoint favorite-price bands: a ">= threshold" sweep can hide a losing
# band behind a winning tail (e.g. 85-90c negative while 95c+ positive),
# and the executor's entry settings ARE a band (entry_threshold..entry_max).
_FAV_BANDS = [(50, 70), (70, 80), (80, 85), (85, 90), (90, 95), (95, 100)]


def crypto15m_report(signals: list[dict], fee_coeff: Optional[float] = None,
                     interval: str = "15m") -> dict:
    """Structured crypto up/down backtest for the CLI. `interval` names the
    recorded window series `signals` came from (load_crypto15m_signals), so
    the report never calls a 5m corpus "15m"."""
    favorite = {"mode": "favorite", **crypto15m_eval(signals, mode="favorite", fee_coeff=fee_coeff)}
    contrarian = {"mode": "contrarian", **crypto15m_eval(signals, mode="contrarian", fee_coeff=fee_coeff)}
    fav_sweep = [
        {"threshold": th, "mode": "favorite",
         **crypto15m_eval(signals, mode="favorite", min_fav=th / 100.0, fee_coeff=fee_coeff)}
        for th in _FAV_THRESHOLDS
    ]
    delta_sweep = [
        {"minDeltaPct": d, "mode": "favorite",
         **crypto15m_eval(signals, mode="favorite", min_delta_pct=d, fee_coeff=fee_coeff)}
        for d in _DELTA_THRESHOLDS
    ]
    band_sweep = [
        {"minFav": lo, "maxFav": hi, "mode": "favorite",
         **crypto15m_eval(signals, mode="favorite", min_fav=lo / 100.0,
                          max_fav=hi / 100.0, fee_coeff=fee_coeff)}
        for lo, hi in _FAV_BANDS
    ]
    # Best variant with a usable sample.
    candidates = [favorite, contrarian, *fav_sweep, *delta_sweep, *band_sweep]
    eligible = [c for c in candidates if c.get("n", 0) >= 30]
    best = max(eligible, key=lambda c: c["net_ev"], default=None)
    # The winner is the max over ~20 variants; its t-stat is a MAXIMUM over many
    # comparisons and so is upward-biased. Record how many variants competed so
    # the verdict can hold it to a Bonferroni-adjusted significance bar.
    n_variants = len(eligible)
    return {
        "n": len(signals),
        "interval": interval,
        "favorite": favorite,
        "contrarian": contrarian,
        "favoriteSweep": fav_sweep,
        "deltaSweep": delta_sweep,
        "bandSweep": band_sweep,
        "macdBreakdown": crypto15m_macd_breakdown(signals, fee_coeff=fee_coeff),
        "best": best,
        "fee_coeff": fee_coeff,
        "verdict": _crypto15m_verdict(len(signals), favorite, contrarian, best,
                                      n_variants, interval),
    }


def _bonferroni_t(k: int) -> float:
    """Two-sided normal quantile z_{1-0.025/k} — the significance bar for the
    BEST of k comparisons (Bonferroni). A plain t>2 bar has a ~5% false-positive
    rate PER variant, so the max over ~20 variants clears t>2 by luck alone far
    more than 5% of the time. Short table covers the counts this sweep emits."""
    for kk, z in ((1, 1.96), (2, 2.24), (3, 2.39), (5, 2.58),
                  (8, 2.73), (12, 2.87), (20, 3.02), (30, 3.14)):
        if k <= kk:
            return z
    return 3.3


def _crypto15m_verdict(n: int, favorite: dict, contrarian: dict,
                       best: dict | None, n_variants: int = 1,
                       interval: str = "15m") -> str:
    if n < 30:
        return (f"COLLECTING DATA — {n} settled {interval} signals so far (need ~100+). "
                "Leave the app open; the recorder logs every window automatically.")
    t_bar = _bonferroni_t(n_variants)
    # The t clustered by close time: the ~7 coins closing together move
    # together, so the per-signal t counts one draw about seven times.
    if best and best["net_ev"] > 0 and best["t_cluster"] > t_bar:
        mode = best.get("mode", "favorite")
        th = best.get("threshold")
        dd = best.get("minDeltaPct")
        lo, hi = best.get("minFav"), best.get("maxFav")
        if th:
            tag = f"favorite-follow ≥ {th}¢"
        elif dd:
            tag = f"favorite-follow, Δ ≥ {dd}%"
        elif lo is not None and hi is not None:
            tag = f"favorite-follow {lo}–{hi}¢"
        else:
            tag = "favorite-follow" if mode == "favorite" else "contrarian-fade"
        return (f"Net edge found: {tag} → +{best['net_ev']*100:.1f}¢/contract "
                f"(t={best['t_cluster']:.1f} clustered by close time > {t_bar:.1f} "
                f"bar, best of {n_variants} variants, n={best['n']} over "
                f"{best['n_events']} closes). Still in-sample — paper-trade it.")
    if max(favorite["net_ev"], contrarian["net_ev"]) > 0:
        return (f"Marginally positive but WITHIN NOISE (best of {n_variants} "
                f"variants clears no t > {t_bar:.1f} bar). Keep collecting — "
                "not yet distinguishable from zero.")
    return (f"NEGATIVE after fees so far. No {interval} variant shows a net edge on your "
            "data yet — keep collecting and try the delta filter.")


def format_crypto15m_report(report: dict, source_label: str) -> str:
    fc = report.get("fee_coeff")
    iv = report.get("interval") or "15m"
    L = [f"═══ Krypt PolyBot US — crypto up/down backtest: {iv} windows ═══",
         f"source: {source_label}",
         f"recording interval: {iv} (only windows recorded while the Crypto tab "
         f"watched {iv}; pick another with --interval)",
         "fee model: taker fee rate × P·(1−P) "
         + (f"(forced rate {float(fc):.4f})" if fc is not None else "(crypto 0.07)"),
         f"settled {iv} signals: {report['n']}", ""]
    if report["n"] == 0:
        L.append(f"No settled {iv} signals yet. Leave the app open with {iv} selected on")
        L.append("the Crypto tab — the recorder logs each window's favorite + outcome")
        L.append("automatically — then re-run this.")
        return "\n".join(L)

    def line(label: str, r: dict) -> str:
        return (f"  {label:<22} n={r['n']:<5} win {r['win_rate']*100:4.0f}%  "
                f"net {_money(r['net_ev'])}/contract (t={r['t_cluster']:+.1f}, "
                f"{r['n_events']} closes)")

    L.append("OVERALL (1 contract per signal; t clustered by close time —")
    L.append("         the coins closing together move together)")
    L.append(line("favorite-follow", report["favorite"]))
    L.append(line("contrarian-fade", report["contrarian"]))
    L.append("")
    L.append("FAVORITE-FOLLOW by minimum favorite price")
    L.append("  fav≥   n      win    net/contract   t")
    for r in report["favoriteSweep"]:
        if r["n"] == 0:
            continue
        L.append(f"  {r['threshold']:>4}¢  {r['n']:<6} {r['win_rate']*100:4.0f}%   "
                 f"{_money(r['net_ev']):>9}     {r['t_cluster']:+.1f}")
    L.append("")
    L.append("FAVORITE-FOLLOW by favorite-price band (disjoint)")
    L.append("  band      n      win    net/contract   t")
    for r in report["bandSweep"]:
        if r["n"] == 0:
            continue
        L.append(f"  {r['minFav']:>2}-{r['maxFav']:<3}¢  {r['n']:<6} {r['win_rate']*100:4.0f}%   "
                 f"{_money(r['net_ev']):>9}     {r['t_cluster']:+.1f}")
    L.append("")
    L.append("FAVORITE-FOLLOW by minimum underlying delta")
    L.append("  Δ≥     n      win    net/contract   t")
    for r in report["deltaSweep"]:
        if r["n"] == 0:
            continue
        L.append(f"  {r['minDeltaPct']:>4}%  {r['n']:<6} {r['win_rate']*100:4.0f}%   "
                 f"{_money(r['net_ev']):>9}     {r['t_cluster']:+.1f}")
    mb = report.get("macdBreakdown")
    if mb and mb.get("nWithMacd"):
        L.append("")
        L.append(f"FAVORITE-FOLLOW by underlying MACD ({mb['nWithMacd']} signals w/ MACD)")
        L.append("  MACD vs side   n      win    net/contract   t")
        for label, r in (("agrees", mb["agree"]), ("conflicts", mb["conflict"])):
            if r["n"] == 0:
                continue
            L.append(f"  {label:<12}  {r['n']:<6} {r['win_rate']*100:4.0f}%   "
                     f"{_money(r['net_ev']):>9}     {r['t_cluster']:+.1f}")
    L.append("")
    L.append(f"VERDICT: {report['verdict']}")
    return "\n".join(L)


def main() -> int:
    # Windows consoles default to cp1252; the report uses box-drawing and
    # math glyphs. Force UTF-8 so the CLI never dies on an encode error.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Fee-aware Krypt PolyBot US signal backtest")
    ap.add_argument("--db", help="path to krypt-polybot-us.db (default: auto-detect)")
    ap.add_argument("--fee", type=float, default=None,
                    help="force a single taker-fee rate coefficient for everything "
                         "(default: per-category schedule, 'other' 0.05 when unknown)")
    ap.add_argument("--demo", action="store_true",
                    help="run on synthetic data instead of your DB")
    ap.add_argument("--spread", type=float, default=DEFAULT_SPREAD_CENTS,
                    help="cents of spread/slippage added to every whale/momentum entry "
                         f"(default {DEFAULT_SPREAD_CENTS:g}; a 0/1/2 sweep is always printed)")
    ap.add_argument("--breakdown", action="store_true",
                    help="also break down by entry-price, category, and a fade check")
    ap.add_argument("--c15", action="store_true",
                    help="backtest the recurring crypto up/down strategy (crypto15m_signals)")
    ap.add_argument("--interval", choices=("5m", "15m", "hourly"), default="15m",
                    help="which recorded window series --c15 reports on (default 15m); "
                         "rows are stamped with the interval they were recorded from")
    args = ap.parse_args()

    if args.c15:
        db_path = resolve_db_path(args.db)
        if not db_path.exists():
            print(f"DB not found at: {db_path}")
            return 1
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            sigs = load_crypto15m_signals(conn, interval=args.interval)
        finally:
            conn.close()
        print(format_crypto15m_report(
            crypto15m_report(sigs, args.fee, interval=args.interval), str(db_path)))
        return 0

    if args.demo:
        print("⚠  SYNTHETIC DEMO DATA — not your real signals.\n")
        signals = demo_signals()
        report = build_report(signals, args.fee, args.spread)
        print(format_report(report, "synthetic demo (4000 signals)"))
        if args.breakdown:
            print(format_breakdown(signals, args.fee, args.spread))
        return 0

    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"DB not found at: {db_path}")
        print("Pass --db <path>, or run `python backtest.py --demo` to see the format.")
        return 1
    # Read-only — never mutate the user's trade history.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        signals = load_resolved_signals(conn)
    finally:
        conn.close()
    report = build_report(signals, args.fee, args.spread)
    print(format_report(report, str(db_path)))
    if args.breakdown:
        print(format_breakdown(signals, args.fee, args.spread))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
