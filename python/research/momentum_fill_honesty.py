"""Fill-honesty stress test for the armed BTC 15m Price Momentum strategy.

Re-runs the exact live probe config (imported from probe_btc_momentum) through
replay.replay under a grid of quote-staleness / signal-to-fill-latency
assumptions. The baseline cell (0, 0) is the historical fill-at-recorded-ask
convention that produced +27.95c/ct — the question is how much of that edge
survives when fills require a quote that didn't JUST move (stale-quote guard)
and a realistic submit-to-match delay.

Read-only against the funded default account's DB (safe while the probe runs).

    python research/momentum_fill_honesty.py [--days 90] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("KRYPT_POLYBOT_US_USERDATA", r"C:\Users\maxwe\AppData\Roaming\Krypt PolyBot US")

import replay
from config import merge_with_defaults
from probe_btc_momentum import CONFIG

# (stable_secs, latency_secs) cells. Stability is a lower bound from ~4-25s
# tick spacing, so 5s effectively means "the previous recorded tick showed the
# same ask". Latency 1.5s ~ CLOB gateway p95 + one engine loop; 3s = harsh.
GRID = [
    (0, 0.0),      # baseline: the convention the +27.95c figure used
    (0, 1.5),
    (0, 3.0),
    (5, 0.0),
    (15, 0.0),
    (30, 0.0),
    (5, 1.5),      # realistic-conservative pair
    (15, 1.5),
    (30, 3.0),     # harshest
]


def run(days: int) -> list[dict]:
    out = []
    for stable, latency in GRID:
        cfg = merge_with_defaults(dict(CONFIG))
        cfg["replay_min_quote_stable_secs"] = stable
        cfg["replay_latency_secs"] = latency
        cfg["replay_latency_slip_tol_cents"] = 1.0
        res = replay.replay(cfg, env="mainnet", since_days=days)
        out.append({
            "stableSecs": stable,
            "latencySecs": latency,
            "n": res["n"],
            "winRate": res["winRate"],
            "netCentsPerContract": res["netEvCentsPerContract"],
            "totalPnlUsd": res["totalPnlUsd"],
            "tStat": res["tStat"],
            "latencyMisses": res["fillModel"]["latencyMisses"],
            "windowsScanned": res["windowsScanned"],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", help="also write full grid to this path")
    args = ap.parse_args()
    rows = run(args.days)
    print(f"\nBTC 15m momentum fill-honesty grid ({args.days}d, mainnet)")
    print(f"{'stable_s':>8} {'lat_s':>6} {'n':>5} {'win%':>6} {'net c/ct':>9} "
          f"{'total$':>8} {'t':>6} {'misses':>7}")
    print("-" * 62)
    for r in rows:
        wr = f"{r['winRate'] * 100:.0f}" if r["n"] else "-"
        t = f"{r['tStat']:.1f}" if r["tStat"] is not None else "-"
        print(f"{r['stableSecs']:>8g} {r['latencySecs']:>6g} {r['n']:>5} {wr:>6} "
              f"{r['netCentsPerContract']:>9.2f} {r['totalPnlUsd']:>8.2f} {t:>6} "
              f"{r['latencyMisses']:>7}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
