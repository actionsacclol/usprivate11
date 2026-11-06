"""Download past crypto up/down windows from Polymarket US's public API, so a new
install can backtest without waiting days for the recorder.

Only unsigned public reads, and nothing here can place an order:

  * /v1/events/slug/{slug}       the window's strike and outcome
  * /v1/price-history            one-minute best ask for YES and NO
  * /v1/asset-prices/history     one-minute prices for the underlying

Rows go into the same tables the recorder fills, so replay and script backtests
read them unchanged, and they are marked `db.HISTORY_SOURCE`:

  * Replay says how many windows of a result came from here. They are coarser than
    recorded ones: one point a minute against ~25 seconds, and no order sizes.
  * The live model-calibration check ignores them, so old data can't pause or resume
    an engine.

A window the recorder already holds is skipped, because recorded data is the better
copy. The job is saved, so a download interrupted by closing the app carries on at the
next start. Measured retention, which Polymarket doesn't promise, is in docs/US_PORT.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

import crypto15m
import crypto15m_record
import db
import indicators
import polymarket_api as api
import polymarket_auth
import us_api as us

logger = logging.getLogger(__name__)

# How far back the venue had each series when measured, on BTC; a request is capped to this.
# ETH, SOL and XRP stop between 20 and 25 days. Those windows are still asked for, at one
# request each, because that depth may grow, and they count as unavailable.
MAX_DAYS = {"5m": 13, "15m": 40, "hourly": 40}
DEFAULT_DAYS = 7
DEFAULT_INTERVALS = ("15m", "hourly")

# The public limit is 20 requests a second per IP, and live trading shares it.
RATE_PER_SEC = 8.0
_WORKERS = 4
_REQUESTS_PER_WINDOW = 2

# The API returns at most 3600 candles a request.
_CANDLE_CHUNK_SEC = 3000 * 60
# A quote is stamped with the START of its minute but holds the price at the END of it.
# Measured against windows this app recorded: the two copies agree best at +60 seconds
# (median gap 2 cents, against 6 at face value). Read at face value, every backtest would
# trade on prices a minute before they existed.
_QUOTE_KNOWN_AFTER_SEC = 60
_LOOKBACK_MIN = crypto15m._INDICATOR_LOOKBACK_MIN

_KV_JOB = "history_download_job"
# This many windows in a row with no answer means an outage or the end of the history.
_MISS_STREAK = 30
_OUTAGE_WAIT_SEC = 30.0
_OUTAGE_MAX_WAITS = 20
_RESUME_DELAY_SEC = 20.0

_INDICATOR_KEYS = {
    "macd": "macd", "macdSignal": "macd_signal", "macdHist": "macd_hist", "macdCross": "macd_cross",
    "rsi": "rsi", "sigma1m": "sigma1m", "vwap1h": "vwap1h", "ema12": "ema12", "sma20": "sma20",
    "sma50": "sma50", "priceVsVwapPct": "price_vs_vwap_pct", "ema12VsSma20Pct": "ema12_vs_sma20_pct",
    "ema1VsSma5Pct": "ema1_vs_sma5_pct", "velocity1mPct": "velocity1m_pct",
    "change5mPct": "change5m_pct", "change15mPct": "change15m_pct",
}


def _idle_status() -> dict:
    return {
        "status": "idle", "total": 0, "done": 0, "saved": 0, "unavailable": 0,
        "days": None, "intervals": [], "message": None, "oldest": None,
    }


_status: dict = _idle_status()
_task: Optional[asyncio.Task] = None


def status() -> dict:
    """The current job. `etaSec` is what's left at the steady rate, or None when idle."""
    s = dict(_status)
    left = max(0, int(s["total"]) - int(s["done"]))
    s["etaSec"] = (round(left * _REQUESTS_PER_WINDOW / RATE_PER_SEC)
                   if s["status"] in ("running", "waiting") else None)
    return s


def clean_request(days: Any = None, intervals: Any = None) -> tuple[int, list[str]]:
    """A valid (days, intervals): unknown intervals dropped, days at least 1. The cap per
    interval is applied when the windows are planned."""
    try:
        d = int(days) if days is not None else DEFAULT_DAYS
    except (TypeError, ValueError):
        d = DEFAULT_DAYS
    d = max(1, min(d, max(MAX_DAYS.values())))
    wanted = list(intervals) if isinstance(intervals, (list, tuple)) else list(DEFAULT_INTERVALS)
    ivs = [i for i in MAX_DAYS if i in wanted]
    return d, ivs or list(DEFAULT_INTERVALS)


def plan_windows(days: int, intervals: list[str], now: float, have: set[str],
                 assets: Optional[list[str]] = None) -> list[dict]:
    """Every closed window in range that isn't held already, newest first, so backtests
    have recent data early. `have` holds event slugs."""
    out: list[dict] = []
    for iv in intervals:
        sec = api._CRYPTO_INTERVAL_SEC[iv]
        horizon = api._UPDOWN_HORIZON[iv]
        reach = min(days, MAX_DAYS[iv]) * 86400
        ws = (int(now) // sec) * sec - sec
        oldest = int(now) - reach
        while ws >= oldest:
            for asset in assets or crypto15m.ALL_ASSETS:
                slug = us.updown_event_slug(asset, horizon, ws)
                if slug not in have:
                    out.append({"asset": asset, "interval": iv, "slug": slug, "start": ws, "sec": sec})
            ws -= sec
    out.sort(key=lambda w: -w["start"])
    return out


def held_event_slugs(env: str) -> set[str]:
    """Event slugs of the windows already stored. A market slug is a prefix plus the event
    slug: cpceth-eth-updown-15m-… holds eth-updown-15m-…"""
    with db.get_db() as conn:
        rows = conn.execute("SELECT ticker FROM crypto15m_signals WHERE network=?", (env,)).fetchall()
    return {str(r["ticker"]).split("-", 1)[-1] for r in rows}


def _quote(v: Any) -> Optional[float]:
    """A display price, or None when that side of the book was empty (0 or 1)."""
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if 0.0 < p < 1.0 else None


def build_window(*, asset: str, interval: str, ticker: str, series: str, start: int, end: int,
                 strike: float, up_won: bool, points: list[dict], minutes: dict[int, tuple[float, float]],
                 fee_schedule: Optional[dict], env: str) -> Optional[tuple[dict, list[dict]]]:
    """The signal row and tick rows for one settled window, or None with no usable quote.

    `minutes` maps a minute's start to its (open, close). A tick is timed when its quote
    was known (see _QUOTE_KNOWN_AFTER_SEC) and indicators read only the closes of minutes
    finished by then, so a tick uses nothing from after its own time."""
    twap = crypto15m.settle_twap_sec(interval)
    ticks: list[dict] = []
    for p in sorted(points, key=lambda x: x.get("timestamp") or 0):
        try:
            ts = int(p["timestamp"]) + _QUOTE_KNOWN_AFTER_SEC
        except (KeyError, TypeError, ValueError):
            continue
        if ts <= start or ts >= end:
            continue
        ask, no_ask = _quote(p.get("longPrice")), _quote(p.get("shortPrice"))
        if ask is None or no_ask is None:
            continue
        bid = 1.0 - no_ask
        if bid > ask:                   # the two sides weren't read at one moment
            continue
        minute = ts - ts % 60
        spot = minutes[minute][0] if minute in minutes else None
        mins_left = (end - ts) / 60.0
        closes = [minutes[m][1] for m in range(minute - _LOOKBACK_MIN * 60, minute, 60) if m in minutes]
        ind = indicators.compute(closes)
        prob = crypto15m.model_up_prob(spot, strike, ind.get("sigma1m"), mins_left, settle_twap_sec=twap)
        row = {
            "ticker": ticker, "asset": asset, "observed_at": _sql_time(ts),
            "mins_left": round(mins_left, 4),
            "yes_bid": round(bid, 4), "yes_ask": ask, "up_ask": ask, "no_ask": no_ask,
            "up_prob": round((ask + bid) / 2.0, 4),
            "spot": spot, "open_spot": strike, "strike": strike,
            "delta_pct": abs(spot - strike) / strike if spot is not None else None,
            "delta_signed_pct": round((spot - strike) / strike, 6) if spot is not None else None,
            "model_prob": round(prob, 4) if prob is not None else None,
            "edge_net_cents": crypto15m.model_edge_net_cents(prob, ask, no_ask, fee_schedule),
            "spot_source": db.HISTORY_SOURCE, "strike_source": db.HISTORY_SOURCE,
            "network": env, "interval": interval,
        }
        for key, col in _INDICATOR_KEYS.items():
            row[col] = ind.get(key)
        ticks.append(row)
    if not ticks:
        return None
    # The same decision point the recorder captures: the first look within the lookback.
    lookback = crypto15m_record._capture_lookback_min(interval)
    d = next((t for t in ticks if t["mins_left"] <= lookback), ticks[-1])
    up = d["up_prob"] >= 0.5
    signal = {
        "ticker": ticker, "asset": asset, "series": series,
        "close_time": datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "observed_at": d["observed_at"], "mins_left": d["mins_left"],
        "favorite": "up" if up else "down",
        "favorite_price": d["up_prob"] if up else round(1.0 - d["up_prob"], 4),
        "entry_cost": d["yes_ask"] if up else d["no_ask"],
        "up_prob": d["up_prob"], "delta_pct": d["delta_pct"], "open_spot": strike, "obs_spot": d["spot"],
        "macd": d["macd"], "macd_signal": d["macd_signal"], "macd_hist": d["macd_hist"],
        "macd_cross": d["macd_cross"], "rsi": d["rsi"], "strike": strike,
        "model_prob": d["model_prob"], "edge_net_cents": d["edge_net_cents"],
        "network": env, "interval": interval, "strike_source": db.HISTORY_SOURCE,
        "resolved": 1, "up_won": 1 if up_won else 0, "settled_at": _sql_time(end),
    }
    return signal, ticks


def _sql_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _Throttle:
    """Spaces requests evenly across every worker."""

    def __init__(self, per_sec: float) -> None:
        self._gap = 1.0 / per_sec
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            pause = self._next - now
            self._next = max(now, self._next) + self._gap
        if pause > 0:
            await asyncio.sleep(pause)


class _Minutes:
    """One-minute prices per asset, fetched in chunks for all assets at once. That is far
    cheaper than asking per window, and a chunk is only fetched once."""

    def __init__(self, throttle: _Throttle) -> None:
        self._throttle = throttle
        self._series: dict[str, dict[int, tuple[float, float]]] = {}
        self._chunks: set[int] = set()
        self._lock = asyncio.Lock()

    async def _load(self, chunk: int) -> None:
        async with self._lock:
            if chunk in self._chunks:
                return
            await self._throttle.wait()
            start = chunk * _CANDLE_CHUNK_SEC
            data = await api._get(f"{api.GATEWAY}/v1/asset-prices/history", params={
                "assets": [f"crypto:{a.lower()}" for a in crypto15m.ALL_ASSETS],
                "from": start, "to": min(start + _CANDLE_CHUNK_SEC, int(time.time())),
                "bucket": "ASSET_PRICE_BUCKET_1M",
            }, timeout=15.0, max_retries=2)
            if not isinstance(data, dict):
                return                  # no answer: the next window asks again
            for series in data.get("series") or []:
                asset = str((series.get("asset") or {}).get("symbol") or "").upper()
                book = self._series.setdefault(asset, {})
                for c in series.get("candles") or []:
                    try:
                        if int(c.get("tickCount") or 0) > 0:
                            book[int(c["timestamp"])] = (float(c["open"]), float(c["close"]))
                    except (KeyError, TypeError, ValueError):
                        continue
            self._chunks.add(chunk)

    async def around(self, asset: str, start: int, end: int) -> Optional[dict[int, tuple[float, float]]]:
        """The asset's one-minute prices, or None when a chunk got no answer. A window saved
        without them would stay incomplete, because a held window is never fetched again."""
        lo = start - _LOOKBACK_MIN * 60
        for chunk in range(lo // _CANDLE_CHUNK_SEC, end // _CANDLE_CHUNK_SEC + 1):
            if chunk not in self._chunks:
                await self._load(chunk)
            if chunk not in self._chunks:
                return None
        return self._series.get(asset.upper(), {})


async def _fetch_window(w: dict, env: str, throttle: _Throttle, minutes: _Minutes) -> str:
    """Download and store one window: 'saved', 'unavailable' (the venue has nothing usable
    for it) or 'miss' (no answer at all, which may be an outage)."""
    await throttle.wait()
    data = await api._get(f"{api.GATEWAY}/v1/events/slug/{w['slug']}", timeout=10.0, max_retries=2)
    event = data.get("event") if isinstance(data, dict) else None
    rows = (event or {}).get("markets") or []
    raw = rows[0] if rows and isinstance(rows[0], dict) else None
    if raw is None:
        return "miss"
    norm = api._normalize_market(raw, event_slug=w["slug"])
    up_won = crypto15m_record._settled_up_won(norm or {})
    strike = (norm or {}).get("price_to_beat")
    if up_won is None or not strike or not norm.get("slug"):
        return "unavailable"
    terms = raw.get("assetPriceTerms") if isinstance(raw.get("assetPriceTerms"), dict) else {}
    start = us.parse_iso_epoch(terms.get("windowStart")) or w["start"]
    end = us.parse_iso_epoch(terms.get("windowEnd")) or (w["start"] + w["sec"])
    await throttle.wait()
    hist = await api._get(f"{api.GATEWAY}/v1/price-history", params={
        "symbol": norm["slug"], "timestamp.startTimestamp": start, "timestamp.endTimestamp": end,
        "fidelity": 1,
    }, timeout=10.0, max_retries=2)
    points = hist.get("history") if isinstance(hist, dict) else None
    if not points:
        return "unavailable"
    series = await minutes.around(w["asset"], start, end)
    if series is None:
        return "miss"
    entry = next((s for s in crypto15m.SERIES if s["asset"] == w["asset"]), {})

    def _store() -> bool:
        built = build_window(
            asset=w["asset"], interval=w["interval"], ticker=norm["slug"], series=entry.get("series", ""),
            start=start, end=end, strike=float(strike), up_won=up_won, points=points, minutes=series,
            fee_schedule=us.fee_schedule(raw), env=env,
        )
        if built is None:
            return False
        with db.get_db() as conn:
            return db.insert_history_window(conn, built[0], built[1])

    return "saved" if await asyncio.to_thread(_store) else "unavailable"


def _save_job(job: Optional[dict]) -> None:
    with db.get_db() as conn:
        if job is None:
            db.kv_delete(conn, _KV_JOB)
        else:
            db.kv_set(conn, _KV_JOB, json.dumps(job))


def _load_job() -> Optional[dict]:
    with db.get_db() as conn:
        raw = db.kv_get(conn, _KV_JOB)
    try:
        job = json.loads(raw) if raw else None
    except ValueError:
        return None
    return job if isinstance(job, dict) else None


async def _run(days: int, intervals: list[str], delay: float = 0.0) -> None:
    global _status
    try:
        if delay:
            await asyncio.sleep(delay)
        env = polymarket_auth.get_env()
        have = await asyncio.to_thread(held_event_slugs, env)
        queue = deque(plan_windows(days, intervals, time.time(), have))
        _status.update(total=len(queue), done=0, status="running")
        throttle = _Throttle(RATE_PER_SEC)
        minutes = _Minutes(throttle)
        misses = {"streak": 0, "waits": 0, "stop": None}
        lock = asyncio.Lock()

        async def worker() -> None:
            while queue and misses["stop"] is None:
                w = queue.popleft()
                result = await _fetch_window(w, env, throttle, minutes)
                if result != "miss":
                    misses["streak"] = 0
                    misses["waits"] = 0
                    _status["status"] = "running"
                    _status["message"] = None
                    _status["done"] += 1
                    _status["saved" if result == "saved" else "unavailable"] += 1
                    if result == "saved":
                        old = _status["oldest"]
                        _status["oldest"] = min(old, w["start"]) if old else w["start"]
                    continue
                misses["streak"] += 1
                if misses["streak"] < _MISS_STREAK:
                    _status["done"] += 1
                    _status["unavailable"] += 1
                    continue
                async with lock:
                    if misses["stop"] is not None or misses["streak"] < _MISS_STREAK:
                        queue.appendleft(w)
                        continue
                    if not api.api_outages():
                        # The venue answers, it just has nothing this old. That's the end.
                        misses["stop"] = "ended"
                        return
                    misses["waits"] += 1
                    if misses["waits"] > _OUTAGE_MAX_WAITS:
                        misses["stop"] = "outage"
                        return
                    _status["status"] = "waiting"
                    _status["message"] = "Can't reach Polymarket US right now. It will carry on by itself."
                    queue.appendleft(w)
                    await asyncio.sleep(_OUTAGE_WAIT_SEC)
                    misses["streak"] = 0

        await asyncio.gather(*[worker() for _ in range(_WORKERS)])
        if misses["stop"] == "outage":
            _status.update(status="error", message=(
                "Couldn't reach Polymarket US, so the download stopped. It tries again next time "
                "the app starts, or you can start it again here."))
            return                      # the saved job resumes at the next start
        _status["done"] = _status["total"]
        _status.update(status="done", message=(
            "Polymarket US has nothing older." if misses["stop"] == "ended" else None))
        await asyncio.to_thread(_save_job, None)
        logger.info(f"history download finished: {_status['saved']} windows saved")
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001: a download must never take the backend with it
        logger.warning(f"history download failed: {e}")
        _status.update(status="error", message="The download stopped on an error. See Logs for details.")


def start(days: Any = None, intervals: Any = None, *, delay: float = 0.0) -> dict:
    """Start in the background, or report the job already running."""
    global _status, _task
    if _task is not None and not _task.done():
        return status()
    d, ivs = clean_request(days, intervals)
    _save_job({"days": d, "intervals": ivs})
    _status = _idle_status()
    _status.update(status="running", days=d, intervals=ivs)
    _task = asyncio.create_task(_run(d, ivs, delay), name="history_download")
    return status()


async def cancel() -> dict:
    """Stop and forget the job. Windows already saved stay."""
    global _task
    _save_job(None)
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _status.update(status="cancelled", message=None)
    _task = None
    return status()


def resume_if_pending(cfg: dict) -> bool:
    """Called at startup: carry on with a job the last run didn't finish. After a pause,
    so it doesn't compete with startup. Not in offline mode, which promises no network."""
    if (cfg or {}).get("terminal_offline_mode"):
        return False
    job = _load_job()
    if not job:
        return False
    start(job.get("days"), job.get("intervals"), delay=_RESUME_DELAY_SEC)
    return True
