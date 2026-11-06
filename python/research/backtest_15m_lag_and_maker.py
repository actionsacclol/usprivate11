"""Read-only backtest on recorded 15m ticks. Places nothing, writes nothing.

Forecasts: `model` (the app's spot/strike/volatility P(up), as recorded live at each
tick), and `blend` (50/50 model and market). The grid is fixed up front and every
cell is reported, so nothing is picked after seeing results. Confidence intervals
resample whole close times, because coins in one window move together.
"""
import collections
import os
import random
import sqlite3
from datetime import datetime, timezone

P = os.path.expandvars(r"%APPDATA%\Krypt PolyBot US\data\krypt-polybot-us.db")
c = sqlite3.connect(f"file:{P}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
THETA = 0.0695
random.seed(7)


def epoch(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T")).replace(
        tzinfo=timezone.utc).timestamp()


def fee_c(px_c):
    p = px_c / 100
    return 100 * THETA * p * (1 - p)


outcome = {}
for r in c.execute("SELECT ticker, close_time, up_won, asset FROM crypto15m_signals WHERE resolved=1"):
    outcome[r["ticker"]] = (r["up_won"], r["close_time"], r["asset"])

windows = collections.defaultdict(list)
for r in c.execute(
        "SELECT ticker, observed_at, mins_left, yes_bid, up_ask, no_ask, up_prob, model_prob, spot_source "
        "FROM crypto15m_ticks WHERE interval='15m' OR interval IS NULL ORDER BY observed_at"):
    if r["ticker"] in outcome and "stale" not in str(r["spot_source"]):
        windows[r["ticker"]].append(dict(r, t=epoch(r["observed_at"])))
closes = sorted({outcome[k][1] for k in windows})
SPLIT = closes[len(closes) // 2]
print(f"windows {len(windows)}, close times {len(closes)}, ticks {sum(map(len, windows.values()))}, "
      f"{closes[0][:16]} to {closes[-1][:16]}, base rate up {sum(outcome[k][0] for k in windows) / len(windows):.3f}")


def forecast(tick, kind):
    m, u = tick["model_prob"], tick["up_prob"]
    if m is None or u is None:
        return None
    return m if kind == "model" else 0.5 * m + 0.5 * u


def ci(rows):
    """rows: [(close, value)]. Mean and 95% interval, resampling close times."""
    if not rows:
        return None
    by = collections.defaultdict(list)
    for k, v in rows:
        by[k].append(v)
    keys = list(by)
    mean = sum(v for _, v in rows) / len(rows)
    means = []
    for _ in range(2000):
        pick = [by[random.choice(keys)] for _ in keys]
        n = sum(map(len, pick))
        means.append(sum(map(sum, pick)) / n if n else 0.0)
    means.sort()
    return mean, means[50], means[1949], len(rows), len(keys)


def fmt(res, unit=""):
    if res is None:
        return "no trades"
    m, lo, hi, n, k = res
    return f"{m:+7.2f}{unit} [{lo:+7.2f},{hi:+7.2f}] n={n:<4d} windows={k}"


print("\n=== A. Accuracy against the outcome, one tick per window at each time left")
print("    Brier difference from the market price; below 0 means better than the market")
for mins in (13, 10, 7, 5, 3, 1):
    rows = {"model": [], "blend": []}
    base = []
    for k, ticks in windows.items():
        usable = [x for x in ticks if x["up_prob"] is not None and x["model_prob"] is not None
                  and abs(x["mins_left"] - mins) <= 0.5]
        if not usable:
            continue
        x = min(usable, key=lambda y: abs(y["mins_left"] - mins))
        y, close, _ = outcome[k]
        mkt = (x["up_prob"] - y) ** 2
        base.append(mkt)
        for kind in rows:
            rows[kind].append((close, ((forecast(x, kind) - y) ** 2 - mkt) * 1000))
    print(f"  {mins:>2d} min left: market Brier {sum(base) / len(base):.4f} | "
          f"model {fmt(ci(rows['model']))} | blend {fmt(ci(rows['blend']))}  (x1000)")


def sides(x):
    """(side, price to take, price if resting) for each side with a quote."""
    out = []
    if x["up_ask"] is not None:
        out.append(("up", x["up_ask"] * 100, x["yes_bid"] * 100 if x["yes_bid"] is not None else None))
    if x["no_ask"] is not None:
        out.append(("down", x["no_ask"] * 100, 100 - x["up_ask"] * 100 if x["up_ask"] is not None else None))
    return out


def ask_of(x, side):
    v = x["up_ask"] if side == "up" else x["no_ask"]
    return None if v is None else v * 100


def bid_of(x, side):
    if side == "up":
        return None if x["yes_bid"] is None else x["yes_bid"] * 100
    return None if x["up_ask"] is None else 100 - x["up_ask"] * 100


def signal(ticks, kind, edge_min, *, resting=False, band=None):
    """First tick and side clearing the edge in this window, or None."""
    for i, x in enumerate(ticks):
        f = forecast(x, kind)
        if f is None or not (0.5 <= x["mins_left"] <= 14.0):
            continue
        for side, take, rest in sides(x):
            px = rest if resting else take
            if px is None or not (3 <= px <= 97):
                continue
            ps = (f if side == "up" else 1 - f) * 100
            fee = fee_c(px)
            if resting:
                e = ps - px
                only_rest = e < edge_min + fee
                if e >= edge_min and (band is None or band == only_rest):
                    return i, side, px, e
            elif ps - px - fee >= edge_min:
                return i, side, px, ps - px - fee
    return None


print("\n=== B. Taker: buy at the ask, hold to settlement, 1 contract, fee included, cents per contract")
print("    A limit at the signal's ask; if the ask has moved above it by the fill tick, no trade.")
print("    Ticks are ~25s apart, so lag 0 = a fast script, 1 tick = about the agent's 16-26s, 2 ticks = slow.")
summary = {}
for kind in ("model", "blend"):
    for emin in (1, 3, 5):
        for lag in (0, 1, 2):
            rows, early, late = [], [], []
            sig = miss = 0
            for k, ticks in windows.items():
                s = signal(ticks, kind, emin)
                if not s:
                    continue
                sig += 1
                i, side, px, _ = s
                if i + lag >= len(ticks):
                    miss += 1
                    continue
                now = ask_of(ticks[i + lag], side)
                if now is None or now > px:
                    miss += 1
                    continue
                y, close, _ = outcome[k]
                won = (y == 1) == (side == "up")
                v = (100 if won else 0) - now - fee_c(now)
                rows.append((close, v))
                (early if close < SPLIT else late).append((close, v))
            res = ci(rows)
            summary[(kind, emin, lag)] = res
            e, l = ci(early), ci(late)
            print(f"  {kind} edge>={emin} lag {lag}: {fmt(res, 'c')} missed {miss}/{sig} | "
                  f"1st half {e[0] if e else 0:+.2f}c, 2nd half {l[0] if l else 0:+.2f}c")


print("\n=== C. Maker: rest at the bid, hold to settlement, no fee, cents per contract")
print("    Filled only if a later tick shows the bid below or the ask at our price. Optimistic: no queue.")
print("    'only passes resting' = the bids the fee change newly allows; 'passes either way' = already allowed.")
for kind in ("model", "blend"):
    for emin in (1, 3):
        for band, label in ((True, "only passes resting"), (False, "passes either way  ")):
            rows = []
            sig = 0
            gap = []
            for k, ticks in windows.items():
                s = signal(ticks, kind, emin, resting=True, band=band)
                if not s:
                    continue
                sig += 1
                i, side, px, _ = s
                filled = False
                for x in ticks[i + 1:]:
                    a, b = ask_of(x, side), bid_of(x, side)
                    if (a is not None and a <= px) or (b is not None and b < px):
                        filled = True
                        break
                if not filled:
                    continue
                y, close, _ = outcome[k]
                won = (y == 1) == (side == "up")
                rows.append((close, (100 if won else 0) - px))
                gap.append(px)
            avg_px = sum(gap) / len(gap) if gap else 0
            print(f"  {kind} edge>={emin} {label}: {fmt(ci(rows), 'c')} filled {len(rows)}/{sig} at avg {avg_px:.1f}c")


print("\n=== D. What the delay costs: price of the chosen side after a signal, cents, edge>=3")
for kind in ("model", "blend"):
    moves = collections.defaultdict(list)
    for k, ticks in windows.items():
        s = signal(ticks, kind, 3)
        if not s:
            continue
        i, side, px, _ = s
        for lag in (1, 2):
            if i + lag < len(ticks):
                now = ask_of(ticks[i + lag], side)
                if now is not None:
                    moves[lag].append((outcome[k][1], now - px))
    for lag, rows in sorted(moves.items()):
        print(f"  {kind} {lag} tick(s) later: ask moved {fmt(ci(rows), 'c')}")
