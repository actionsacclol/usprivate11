"""Backtest the imported Turbine strategy library on OUR recorded ticks.

Runs each importable strategy through replay.replay (the live entry gate over
recorded ticks, held to settlement, fee-adjusted) and ranks them by
risk-adjusted net edge on our own Polymarket data. Requires the trend/VWAP
fields to be present on the ticks — run crypto15m_backfill first, or the
VWAP/momentum rules match nothing (0 trades). Every Turbine strategy trades
15-minute windows, and the recorder only records the interval selected on the
Crypto tab: with no 15m windows recorded every strategy scans 0 windows, and
the report leads with NO DATA instead of ranking zeros.
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Optional

import replay
import turbine_import
from config import merge_with_defaults


def _tstat(trades: list[dict]) -> Optional[float]:
    pnls = [float(t["pnlUsd"]) for t in trades]
    n = len(pnls)
    if n < 2:
        return None
    mean = sum(pnls) / n
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    # Degenerate variance (e.g. an all-wins thin-margin sweep where every trade
    # pays ~the same tiny amount) explodes the t into a meaningless 1e17 — treat
    # it as unproven rather than "infinitely significant".
    if var <= 1e-6:
        return None
    t = mean / (math.sqrt(var) / math.sqrt(n))
    # Cap: beyond this, it's a low-variance artifact, not added confidence.
    return max(-20.0, min(20.0, t))


_MIN_N = 15  # below this the edge is anecdote, not a rankable result


def _rank_score(net_cents: float, t: Optional[float], n: int) -> float:
    """One-SE conservative lower bound on the edge: net − |net|/t. Tiny samples
    are pushed to the bottom (a +15¢ edge on n=3 is noise, not a #1 strategy),
    and no t → treated as unproven."""
    if n is None or n < _MIN_N:
        return -100.0 + net_cents * 0.001  # bottom, but keep a stable order
    if t is None or t == 0:
        return net_cents - abs(net_cents)  # ~0 or negative: unproven
    return net_cents - abs(net_cents) / abs(t)


def run(env: str = "mainnet", since_days: int = 90) -> list[dict]:
    imported, _skipped = turbine_import.import_all()
    out: list[dict] = []
    for item in imported:
        cfg = merge_with_defaults(dict(item["config"]))
        try:
            res = replay.replay(cfg, env=env, since_days=since_days)
        except Exception as e:
            out.append({**_meta(item), "error": str(e)[:120]})
            continue
        # replay truncates trades[] to the last 50 for the UI; its tStat is the
        # honest full-sample figure, so prefer it and only fall back to the local
        # recompute for older payloads that predate the field.
        t = res.get("tStat")
        if t is None:
            t = _tstat(res.get("trades") or [])
        net = float(res.get("netEvCentsPerContract") or 0.0)
        row = {
            **_meta(item),
            "interval": res.get("interval"),
            "n": res.get("n", 0),
            "winRate": res.get("winRate"),
            "netCentsPerContract": round(net, 3),
            "totalPnlUsd": res.get("totalPnlUsd"),
            "t": round(t, 2) if t is not None else None,
            "rankScore": round(_rank_score(net, t, res.get("n", 0)), 3),
            "windowsScanned": res.get("windowsScanned", 0),
        }
        if not row["windowsScanned"]:
            # Nothing was replayable — missing data, not a result. Carry the
            # reason (usually: these are 15m strategies and the recorder has
            # been on another interval) instead of a 0.00¢ "score".
            row["noData"] = (res.get("noData") or {}).get("message") or (
                (res.get("caveats") or ["no recorded windows"])[0])
        out.append(row)
    out.sort(key=lambda r: r.get("rankScore", -999), reverse=True)
    return out


def no_data_headline(ranked: list[dict]) -> Optional[str]:
    """The line that must lead the report when some or all strategies had
    nothing to replay — before any table that would read 0 trades as a
    ranking. None when every strategy scanned windows."""
    empty = [r for r in ranked if not r.get("error") and not r.get("windowsScanned")]
    if not empty:
        return None
    scope = "ALL" if len(empty) == len(ranked) else f"{len(empty)} of"
    return (f"NO DATA: {scope} {len(ranked)} strategies replayed 0 windows — "
            f"{empty[0]['noData']}")


def _meta(item: dict) -> dict:
    tb = item.get("turbine") or {}
    return {"name": item["name"], "asset": item["asset"],
            "archetype": item["archetype"], "turbineNetPnl": tb.get("netPnl"),
            "turbineWinPct": tb.get("winPct")}


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest the Turbine library on our data.")
    ap.add_argument("--env", default="mainnet")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()
    ranked = run(args.env, args.days)
    if args.json:
        print(json.dumps(ranked, indent=1))
        return
    headline = no_data_headline(ranked)
    if headline:
        print(f"\n{headline}")
    print(f"\n{'#':>2}  {'net¢/ct':>8} {'t':>6} {'n':>5} {'win%':>5}  {'arch':<16} name")
    print("-" * 92)
    for i, r in enumerate(ranked, 1):
        if r.get("error"):
            print(f"{i:>2}  {'ERR':>8}  {r['error']}")
            continue
        if not r.get("windowsScanned"):
            print(f"{i:>2}  {'NO DATA':>8} {'-':>6} {'-':>5} {'-':>5}  "
                  f"{r['archetype']:<16} {r['name']}")
            continue
        wr = f"{r['winRate']*100:.0f}" if r.get("winRate") is not None else "-"
        t = f"{r['t']:.1f}" if r.get("t") is not None else "-"
        print(f"{i:>2}  {r['netCentsPerContract']:>8.2f} {t:>6} {r['n']:>5} {wr:>5}  "
              f"{r['archetype']:<16} {r['name']}")
    with_data = [r for r in ranked if r.get("windowsScanned")]
    if not with_data:
        print(f"\n{len(ranked)} strategies · none had recorded windows to replay "
              f"on {args.days}d of {args.env} data — no verdict (see NO DATA above)")
        return
    n_sig = sum(1 for r in with_data if r.get("t") and abs(r["t"]) >= 2 and r["n"] >= 30 and r["netCentsPerContract"] > 0)
    print(f"\n{len(ranked)} strategies ({len(with_data)} with data) · {n_sig} significant "
          f"(+edge, |t|>=2, n>=30) on {args.days}d of {args.env} data")


if __name__ == "__main__":
    main()
