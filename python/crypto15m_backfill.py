"""Backfill the trend/VWAP tick fields for HISTORICAL crypto15m_ticks.

The trend indicator columns (price_vs_vwap_pct, change5m_pct, ema12_vs_sma20_pct,
…) are NULL on every row recorded before they existed, so a rule over them fails
closed in replay (0 trades). This recomputes them by re-fetching 1-minute
candles up to the last one that had CLOSED when each tick was recorded — the
same math the live path uses (crypto15m.asset_indicators), never a candle
from the tick's future — and writes them back.

Efficient + resumable: only rows still missing the fields are processed, grouped
by (asset, minute) so ONE candle fetch serves every tick in that minute; a
bounded `--limit` lets it run in chunks. Additive UPDATE of the new columns only
— never touches trades/positions/outcomes.

Usage:
  python crypto15m_backfill.py [--env mainnet] [--days N] [--limit N] [--loop]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

import db as dbmod
import indicators

logger = logging.getLogger("crypto15m_backfill")

_HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
# Coinbase Exchange public candles. Long 1-min history (works on weeks-old
# dates, unlike Hyperliquid's ~3-4 day retention) and US-reachable, covering all
# 7 coins incl. HYPE/BNB. Default source so the EXISTING recorded dataset — too
# old for Hyperliquid — can be backfilled. One request per (asset,minute) group
# (90 candles << the 300/request cap), same shape as the Hyperliquid path.
_COINBASE_URL = "https://api.exchange.coinbase.com/products/{}-USD/candles"
_LOOKBACK_MIN = 90  # enough for a 60-min VWAP + a stable MACD warm-up

# snapshot key (from indicators.compute) -> tick column
_KEY_TO_COL = {
    "vwap1h": "vwap1h", "ema12": "ema12", "sma20": "sma20", "sma50": "sma50",
    "priceVsVwapPct": "price_vs_vwap_pct", "ema12VsSma20Pct": "ema12_vs_sma20_pct",
    "ema1VsSma5Pct": "ema1_vs_sma5_pct", "velocity1mPct": "velocity1m_pct",
    "change5mPct": "change5m_pct", "change15mPct": "change15m_pct",
}


def _epoch_ms(observed_at: str) -> Optional[int]:
    try:
        dt = datetime.strptime(observed_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _last_closed_candle_ms(minute: str) -> Optional[int]:
    """Open time (epoch ms) of the newest 1-minute candle that had CLOSED
    when a tick in `minute` ('YYYY-MM-DD HH:MM') was recorded: the one ending
    at that minute's start. The candle the tick sits in closes up to 60s
    after the tick, so ending the slice there handed every backfilled row a
    price from its future (audit 2026-10-11). Live reads that candle's
    partial close instead; a completed candle is the nearest causal match."""
    start = _epoch_ms(minute + ":00")
    return None if start is None else start - 60_000


async def _fetch_candles_hyperliquid(asset: str, client: httpx.AsyncClient, end_ms: int) -> tuple[list[float], list[float]]:
    """(closes, volumes) for the 90 minutes ending at end_ms, oldest→newest.
    Hyperliquid only retains ~3-4 days of 1-min candles."""
    body = {"type": "candleSnapshot", "req": {
        "coin": asset, "interval": "1m",
        "startTime": end_ms - _LOOKBACK_MIN * 60_000, "endTime": end_ms}}
    resp = await client.post(_HYPERLIQUID_URL, json=body, timeout=15.0)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        return [], []
    closes, vols = [], []
    for r in rows:
        if not isinstance(r, dict) or not r.get("c"):
            continue
        # Never a candle that opened after end_ms (it closed in the future).
        if isinstance(r.get("t"), (int, float)) and r["t"] > end_ms:
            continue
        try:
            closes.append(float(r["c"]))
        except (TypeError, ValueError):
            continue
        try:
            vols.append(float(r.get("v")) if r.get("v") is not None else 0.0)
        except (TypeError, ValueError):
            vols.append(0.0)
    return closes, vols


async def _fetch_candles_coinbase(asset: str, client: httpx.AsyncClient, end_ms: int) -> tuple[list[float], list[float]]:
    """(closes, volumes) for the 90 minutes ending at end_ms, oldest→newest, from
    Coinbase Exchange. Rows are [time, low, high, open, close, volume],
    newest-first — we sort ascending and take close/volume. One 429 retry."""
    end_s = end_ms // 1000
    start_iso = datetime.fromtimestamp(end_s - _LOOKBACK_MIN * 60, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(end_s, tz=timezone.utc).isoformat()
    url = _COINBASE_URL.format(asset)
    params = {"granularity": 60, "start": start_iso, "end": end_iso}
    headers = {"User-Agent": "KryptPolyBot/1.0"}
    for attempt in (1, 2):
        resp = await client.get(url, params=params, headers=headers, timeout=15.0)
        if resp.status_code == 429 and attempt == 1:
            await asyncio.sleep(1.0)
            continue
        if resp.status_code == 404:
            return [], []  # coin not listed on Coinbase (e.g. a delisted symbol)
        resp.raise_for_status()
        break
    rows = resp.json()
    if not isinstance(rows, list):
        return [], []
    # Ascending by time, and never a candle that opened after end_s (it
    # closed in the future).
    rows = sorted((r for r in rows if isinstance(r, list) and len(r) >= 6
                   and not (isinstance(r[0], (int, float)) and r[0] > end_s)),
                  key=lambda r: r[0])
    closes, vols = [], []
    for r in rows:
        try:
            closes.append(float(r[4]))
        except (TypeError, ValueError):
            continue
        try:
            vols.append(float(r[5]))
        except (TypeError, ValueError):
            vols.append(0.0)
    return closes, vols


_SOURCES = {"coinbase": _fetch_candles_coinbase, "hyperliquid": _fetch_candles_hyperliquid}

_COINBASE_MAX = 300  # candles per request (granularity 60 -> 300 min = 5h)


async def _fetch_series_coinbase(asset: str, client: httpx.AsyncClient,
                                 start_ms: int, end_ms: int) -> dict[int, tuple[float, float]]:
    """Fetch the ENTIRE 1-min candle series for [start_ms, end_ms] once, paged at
    300 candles/request, and return {minute_epoch_s: (close, volume)}. This is the
    bulk path: one coin's whole history in ~(span/5h) calls instead of one 90-min
    window per tick-minute (which refetched ~89 min of overlap every step)."""
    url = _COINBASE_URL.format(asset)
    headers = {"User-Agent": "KryptPolyBot/1.0"}
    out: dict[int, tuple[float, float]] = {}
    step = _COINBASE_MAX * 60  # seconds per page
    cur = start_ms // 1000
    end_s = end_ms // 1000
    while cur <= end_s:
        chunk_end = min(cur + step, end_s)
        params = {"granularity": 60,
                  "start": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
                  "end": datetime.fromtimestamp(chunk_end, tz=timezone.utc).isoformat()}
        rows = None
        for attempt in (1, 2, 3):
            resp = await client.get(url, params=params, headers=headers, timeout=20.0)
            if resp.status_code == 429:
                await asyncio.sleep(0.5 * attempt)
                continue
            if resp.status_code == 404:
                return out  # coin not on Coinbase
            resp.raise_for_status()
            rows = resp.json()
            break
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, list) and len(r) >= 6:
                    try:
                        out[int(r[0])] = (float(r[4]), float(r[5]))
                    except (TypeError, ValueError):
                        pass
        cur = chunk_end + 60
        await asyncio.sleep(0.12)  # stay under Coinbase public rate limit
    return out


def _slice_trailing(series: dict[int, tuple[float, float]], end_min_s: int,
                    lookback_min: int = _LOOKBACK_MIN) -> tuple[list[float], list[float]]:
    """The 90 candles ending at end_min_s (inclusive), oldest->newest, from the
    prefetched series — mirrors the live 90-min fetch window."""
    closes, vols = [], []
    for t in range(end_min_s - lookback_min * 60, end_min_s + 1, 60):
        cv = series.get(t)
        if cv is not None:
            closes.append(cv[0])
            vols.append(cv[1])
    return closes, vols


async def backfill_bulk(env: str = "mainnet", since_days: int = 90,
                        concurrency: int = 4) -> dict:
    """Bulk Coinbase backfill: fetch each coin's full 1-min history once, then
    compute every pending (asset,minute) group locally. Orders-of-magnitude fewer
    HTTP calls than the per-group path for a large historical dataset."""
    groups = _pending_groups(env, since_days, limit=10_000_000)
    if not groups:
        return {"groups": 0, "rowsUpdated": 0, "fetchErrors": 0, "noData": 0, "assets": 0}

    # span needed per asset (mid-minute epochs), padded by the lookback window
    per_asset: dict[str, list[tuple[str, int]]] = {}
    for (asset, minute) in groups:
        end_ms = _last_closed_candle_ms(minute)
        if end_ms is None:
            continue
        per_asset.setdefault(asset, []).append((minute, end_ms))

    sem = asyncio.Semaphore(concurrency)
    updated = errors = nodata = 0
    all_updates: list[tuple[dict, list[int]]] = []

    async def do_asset(client: httpx.AsyncClient, asset: str, items: list[tuple[str, int]]):
        nonlocal errors, nodata
        ends = [e for _m, e in items]
        start_ms = min(ends) - (_LOOKBACK_MIN + 2) * 60_000
        end_ms = max(ends) + 60_000
        async with sem:
            try:
                series = await _fetch_series_coinbase(asset, client, start_ms, end_ms)
            except Exception as e:
                logger.debug(f"bulk series {asset}: {e}")
                errors += len(items)
                return
        for minute, end_ms_i in items:
            end_min_s = end_ms_i // 1000   # the last CLOSED candle's open time
            closes, vols = _slice_trailing(series, end_min_s)
            data = indicators.compute(closes, vols)
            sets = {col: data.get(k) for k, col in _KEY_TO_COL.items()}
            if all(v is None for v in sets.values()):
                nodata += 1
                continue
            all_updates.append((sets, groups[(asset, minute)]))

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[do_asset(client, a, items) for a, items in per_asset.items()])

    with dbmod.get_db() as conn:
        for sets, ids in all_updates:
            cols_sql = ", ".join(f"{c}=?" for c in sets)
            q = "?, " * (len(ids) - 1) + "?"
            conn.execute(f"UPDATE crypto15m_ticks SET {cols_sql} WHERE id IN ({q})",
                         list(sets.values()) + ids)
            updated += len(ids)
    return {"groups": len(groups), "rowsUpdated": updated, "fetchErrors": errors,
            "noData": nodata, "assets": len(per_asset)}


def _pending_groups(env: str, since_days: int, limit: int) -> dict[tuple[str, str], list[int]]:
    """(asset, minute) -> [tick ids] for rows still missing the fields."""
    with dbmod.get_db() as conn:
        rows = conn.execute(
            """SELECT id, asset, observed_at FROM crypto15m_ticks
                WHERE network=? AND price_vs_vwap_pct IS NULL
                  AND observed_at >= datetime('now', ?)
                ORDER BY observed_at DESC""",
            (env, f"-{int(since_days)} days"),
        ).fetchall()
    groups: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        minute = str(r["observed_at"])[:16]  # YYYY-MM-DD HH:MM
        key = (str(r["asset"]), minute)
        if key not in groups and len(groups) >= limit:
            continue
        groups.setdefault(key, []).append(int(r["id"]))
    return groups


async def backfill(env: str = "mainnet", since_days: int = 90, limit: int = 500,
                   concurrency: int = 6, source: str = "coinbase") -> dict:
    groups = _pending_groups(env, since_days, limit)
    if not groups:
        return {"groups": 0, "rowsUpdated": 0, "fetchErrors": 0, "noData": 0}
    fetch = _SOURCES.get(source, _fetch_candles_coinbase)
    sem = asyncio.Semaphore(concurrency)

    async def fetch_group(client: httpx.AsyncClient, key: tuple[str, str]):
        asset, minute = key
        end_ms = _last_closed_candle_ms(minute)
        if end_ms is None:
            return ("skip", key)
        async with sem:
            try:
                closes, vols = await fetch(asset, client, end_ms)
            except Exception as e:
                logger.debug(f"backfill fetch {asset} {minute}: {e}")
                return ("err", key)
        data = indicators.compute(closes, vols)
        sets = {col: data.get(k) for k, col in _KEY_TO_COL.items()}
        if all(v is None for v in sets.values()):
            return ("nodata", key)  # outside Hyperliquid's retention
        return ("ok", sets, groups[key])

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[fetch_group(client, k) for k in groups])

    updated = errors = nodata = 0
    with dbmod.get_db() as conn:  # writes serialized in one connection
        for r in results:
            tag = r[0]
            if tag == "err":
                errors += 1
            elif tag == "nodata":
                nodata += 1
            elif tag == "ok":
                _, sets, ids = r
                cols_sql = ", ".join(f"{c}=?" for c in sets)
                q = "?, " * (len(ids) - 1) + "?"
                conn.execute(
                    f"UPDATE crypto15m_ticks SET {cols_sql} WHERE id IN ({q})",
                    list(sets.values()) + ids,
                )
                updated += len(ids)
    return {"groups": len(groups), "rowsUpdated": updated,
            "fetchErrors": errors, "noData": nodata}


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Backfill trend/VWAP tick fields from Hyperliquid.")
    ap.add_argument("--env", default="mainnet")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=500, help="max (asset,minute) groups per run")
    ap.add_argument("--source", default="coinbase", choices=list(_SOURCES),
                    help="candle source (coinbase = long history; hyperliquid = ~3-4d)")
    ap.add_argument("--per-group", action="store_true",
                    help="legacy per-(asset,minute) fetch instead of the bulk fetch-once path")
    ap.add_argument("--loop", action="store_true", help="repeat until nothing pending")
    args = ap.parse_args()

    # Bulk path: one fetch of each coin's full history, all groups computed locally.
    if not args.per_group and args.source == "coinbase":
        res = asyncio.run(backfill_bulk(args.env, args.days))
        print(f"BULK: {res}")
        return

    total = {"groups": 0, "rowsUpdated": 0, "fetchErrors": 0, "noData": 0}
    while True:
        res = asyncio.run(backfill(args.env, args.days, args.limit, source=args.source))
        for k in total:
            total[k] += res.get(k, 0)
        print(f"pass: {res}")
        if not args.loop or res["groups"] == 0:
            break
    print(f"TOTAL: {total}")


if __name__ == "__main__":
    main()
