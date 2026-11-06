"""Settlement index feed: the numbers Polymarket US crypto windows settle on.

Crypto up/down windows settle on a CF Benchmarks real-time index (BRTI for BTC,
ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI). A window's price to beat and its settlement
value are each "the simple average of the 1-second prices in the 60 seconds before
the boundary".

The public GET /v1/asset-prices/history 1-second series is ALREADY that average:
each candle (`tickCount: 60`) is the trailing 60-second mean. Verified 2026-11-05 on
30 windows across BTC/ETH/SOL: the candle stamped T-1 equals the published
priceToBeat at T exactly, while averaging 60 candles misses by up to $17.50 on BTC.

So this feed gives the settlement value directly, and must not be used as a live
spot price: measured the same day, its moves trail Coinbase spot by 35-40 seconds at
peak correlation (a 60s mean, as expected) and it sits 2.9 bps (sd) from spot, but
only 0.19 bps from Coinbase's own trailing 60s mean. The live spot comes from spot_ws.

Fully degradable: when polling fails the ring ages out, `twap` goes None, and the
strike falls back to what the market itself published.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from typing import Optional

import polymarket_api as api
import us_api as us

logger = logging.getLogger("index_feed")

ASSETS = ("BTC", "ETH", "SOL", "XRP")
_URL = f"{us.GATEWAY}/v1/asset-prices/history"
_BUCKET = "ASSET_PRICE_BUCKET_1S"
_POLL_SEC = 1.0
_BACKFILL_SEC = 1800            # on start: covers the strike of any window in progress
_MAX_CANDLES = 3600             # the endpoint's cap per request
_RING_SEC = 4000                # longest window here is 1h
_FRESH_SEC = 6.0
SETTLE_WINDOW_SEC = 60


def parse_series(body) -> dict[str, list[tuple[int, float]]]:
    """{'BTC': [(second, value), ...]} from a history reply. Pure."""
    out: dict[str, list[tuple[int, float]]] = {}
    rows = body.get("series") if isinstance(body, dict) else None
    for s in rows or []:
        if not isinstance(s, dict):
            continue
        asset = s.get("asset") if isinstance(s.get("asset"), dict) else {}
        sym = str(asset.get("symbol") or "").upper()
        if not sym:
            continue
        points: list[tuple[int, float]] = []
        for c in s.get("candles") or []:
            try:
                px = float(c.get("close"))
                sec = int(c.get("timestamp"))
            except (AttributeError, TypeError, ValueError):
                continue
            if px > 0:
                points.append((sec, px))
        out[sym] = points
    return out


class _Feed:
    def __init__(self) -> None:
        self.rings: dict[str, collections.OrderedDict] = {}
        self.last_sec: dict[str, int] = {}
        self.last_ok_at = 0.0
        self.last_error = ""
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    def ingest(self, series: dict[str, list[tuple[int, float]]]) -> int:
        added = 0
        for sym, points in series.items():
            ring = self.rings.setdefault(sym, collections.OrderedDict())
            last = self.last_sec.get(sym, 0)
            for sec, px in sorted(points):
                if sec > last:
                    ring[sec] = px
                    last = sec
                    added += 1
            while len(ring) > _RING_SEC:
                ring.popitem(last=False)
            self.last_sec[sym] = last
        return added

    def latest(self, asset: str, now: Optional[float] = None) -> Optional[float]:
        a = (asset or "").upper()
        ring = self.rings.get(a)
        sec = self.last_sec.get(a)
        if not ring or sec is None:
            return None
        t = time.time() if now is None else now
        return ring[sec] if (t - sec) <= _FRESH_SEC else None

    def value_at(self, asset: str, boundary: float) -> Optional[float]:
        """The settlement value of a boundary: the candle stamped one second
        before it. Exact or nothing: a neighbouring second is a different number."""
        ring = self.rings.get((asset or "").upper())
        return ring.get(int(boundary) - 1) if ring else None

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def connected(self) -> bool:
        return self.is_running() and (time.time() - self.last_ok_at) <= _FRESH_SEC

    def start(self) -> None:
        if not self.is_running():
            self._stop = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def poll_once(self) -> None:
        now = int(time.time())
        known = [self.last_sec.get(a, 0) for a in ASSETS]
        oldest = min(known) if all(known) else now - _BACKFILL_SEC
        start = max(oldest + 1, now - _MAX_CANDLES + 1)
        params = [("assets", f"crypto:{a.lower()}") for a in ASSETS]
        params += [("from", start), ("bucket", _BUCKET)]
        body = await api._get(_URL, params=params, timeout=3.0, max_retries=1)
        if not isinstance(body, dict):
            self.last_error = "no reply from the index endpoint"
            return
        self.ingest(parse_series(body))
        self.last_ok_at = time.time()
        self.last_error = ""

    async def _run(self) -> None:
        while not self._stop:
            t0 = time.monotonic()
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a bad poll must not kill the feed
                self.last_error = f"{type(e).__name__}: {e}"
                logger.debug(f"index poll: {self.last_error}")
            await asyncio.sleep(max(0.05, _POLL_SEC - (time.monotonic() - t0)))


_feed = _Feed()


def start() -> None:
    _feed.start()


async def stop() -> None:
    await _feed.stop()


def is_connected() -> bool:
    return _feed.connected()


def is_running() -> bool:
    return _feed.is_running()


def twap(asset: str) -> Optional[float]:
    """What would settle if a window closed right now. Not a spot price."""
    return _feed.latest(asset)


def twap_at(asset: str, boundary: float) -> Optional[float]:
    """The official value at a window boundary: its strike at the open, its
    settlement at the close. None when this second isn't in the ring."""
    return _feed.value_at(asset, boundary)


def fresh_assets() -> list[str]:
    return [a for a in ASSETS if _feed.latest(a) is not None]


def stats() -> dict:
    return {
        "enabled": True,
        "connected": _feed.connected(),
        "transport": "poll",
        "spotSources": {},
        "basisBps": {},
        "assets": fresh_assets(),
        "twapAssets": fresh_assets(),
        "lastMsgAgeSec": round(max(0.0, time.time() - _feed.last_ok_at), 1) if _feed.last_ok_at else None,
        "lastError": _feed.last_error,
    }
