"""15-minute crypto market monitor (READ-ONLY).

Powers the "15m Crypto" tab. Tracks Polymarket's seven 15-minute crypto
series and surfaces a momentum entry *signal* for each: in the last
`time_delay` minutes of a quarter, when one side is a strong favorite
(>= entry threshold) and the underlying has moved at least
`min_delta_pct` from the quarter's opening spot price.

This module NEVER places, modifies, or cancels an order — it only reads
public Polymarket market data and a public crypto spot feed. The spot price
(needed to compute delta) comes from CoinGecko because Polymarket's API only
exposes contract probabilities, not the live underlying price.

Every threshold is user-configurable from the 15m Crypto tab and
overridable via `crypto15m_*` config keys. The shipped defaults
(8-minute window, 85c favorite, 40c stop, 0.02 markup) are just one
common favorite-follow baseline — treat them as a starting point, not a
proven edge.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import httpx

import clob_ws
import indicators
import polymarket_api
import index_feed
import spot_ws
import us_api as us

logger = logging.getLogger(__name__)


# asset symbol, display series label, CoinGecko id. Polymarket US lists
# recurring "Up or Down" crypto markets (5m/15m/1h/1d) for each of these.
SERIES: list[dict[str, str]] = [
    {"asset": "BTC",  "series": "BTC-UPDOWN",  "cg": "bitcoin"},
    {"asset": "ETH",  "series": "ETH-UPDOWN",  "cg": "ethereum"},
    {"asset": "SOL",  "series": "SOL-UPDOWN",  "cg": "solana"},
    {"asset": "XRP",  "series": "XRP-UPDOWN",  "cg": "ripple"},
]

ALL_ASSETS = [s["asset"] for s in SERIES]


def asset_enabled(cfg: dict, asset: str) -> bool:
    """True if the executor may open NEW positions on `asset`. None/missing
    `crypto15m_assets` = all enabled; a list restricts to those symbols (an
    empty list disables every asset)."""
    raw = (cfg or {}).get("crypto15m_assets")
    if isinstance(raw, list):
        return asset.upper() in {str(a).upper() for a in raw}
    return True  # None / unset → all enabled

# Shipped defaults; each overridable via cfg["crypto15m_<key>"] (edited
# from the 15m Crypto tab). A favorite-follow baseline, not a proven edge.
# Entry band 95-98c: the only favorite-price band that didn't lose after
# fees across 531 recorded settled markets (small sample, unproven).
_DEFAULTS: dict[str, float] = {
    "time_delay_min": 8.0,    # only signal inside the last N minutes of the quarter
    "entry_threshold": 0.95,  # favorite side must be >= this probability
    "exit_threshold": 0.40,   # stop-loss: executor sells if the position falls to this
    "take_profit": 0.0,       # per-bet cash-out: sell when our side's bid reaches this (0 = off)
    "stop_loss_pct": 0.0,     # per-bet stop: sell when down >= this fraction of entry cost (0 = off)
    "take_profit_pct": 0.0,   # per-bet cash-out: sell when up >= this fraction of entry cost (0 = off)
    "entry_max": 0.98,        # skip already-decided contracts priced above this
    "min_delta_pct": 0.0,     # required |open - live|/open underlying move (0 = off)
    "entry_diff": 0.02,       # limit markup: how far through the spread the order crosses
}

# Spot price feeds, tried in order. CoinGecko's free tier rate-limits
# hard (429) and the monitor/executor/recorder all need spot, so we lead
# with Coinbase (per-asset) and fall back to CoinGecko. Results are cached
# briefly so the three callers share a single upstream request. CryptoCompare
# used to lead; since 2026 it answers 401 without an API key.
_COINBASE_URL = "https://api.coinbase.com/v2/prices/{sym}-USD/spot"
_COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
_SPOT_CACHE_TTL = 5.0  # seconds a fetched spot set is reused across callers.
# Drives how fast the live price + move% (deltaPct) update on the Crypto tab.
# 5s keeps the move% feeling live at about 12 requests a minute per coin on
# Coinbase; CoinGecko remains the fallback if it fails.
_INTERVAL_SEC = {"5m": 300, "15m": 900, "hourly": 3600}


def _interval(cfg: dict) -> str:
    iv = str(cfg.get("crypto15m_interval", "15m"))
    return iv if iv in _INTERVAL_SEC else "15m"

_spot_client: Optional[httpx.AsyncClient] = None

# Shared spot cache so the monitor poll, executor tick and research
# recorder don't each open a fresh upstream request every few seconds.
_spot_cache: dict = {"at": 0.0, "spots": {}, "source": "none"}

# (asset, window_start_epoch) -> opening spot observed for that 15-min window.
# (asset, window_start) -> (strike, source); source is "twap" (the official
# settlement index value at the open), "spot-avg" (our own 60s average of
# live spot ending at the open — a close stand-in) or "spot" (a single print;
# approximate since TWAP settlement).
_window_open: dict[tuple[str, int], tuple[float, str]] = {}


def _get_spot_client() -> httpx.AsyncClient:
    global _spot_client
    if _spot_client is None or _spot_client.is_closed:
        _spot_client = httpx.AsyncClient(
            timeout=8.0,
            headers={"Accept": "application/json", "User-Agent": "KryptPolyBot/1.0"},
        )
    return _spot_client


async def close_clients() -> None:
    global _spot_client
    if _spot_client and not _spot_client.is_closed:
        try:
            await _spot_client.aclose()
        except Exception:
            pass
    _spot_client = None


# Heal this pool whenever the shared detector rebuilds the main one: a poisoned
# spot/indicator pool fails FAST (bounded timeouts, no loop stall), so the
# watchdog never fired and modelProb/sigma silently vanished until app restart.
polymarket_api.register_recycle_hook(close_clients)


def _const(cfg: dict, key: str) -> float:
    try:
        return float(cfg.get(f"crypto15m_{key}", _DEFAULTS[key]))
    except (TypeError, ValueError):
        return _DEFAULTS[key]


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _price_dollars(m: dict, key: str) -> float:
    """Polymarket ships both integer-cents (`yes_bid`) and dollar
    (`yes_bid_dollars`) fields for prices; prefer the dollar form and
    fall back to cents/100."""
    d = m.get(f"{key}_dollars")
    if d is not None:
        return _to_float(d)
    return _to_float(m.get(key)) / 100.0


def _parse_close_epoch(close_time: str) -> Optional[float]:
    if not close_time:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            return ct.timestamp()
        except ValueError:
            continue
    return None


async def _spots_coinbase(client: httpx.AsyncClient) -> dict[str, float]:
    """Per-asset spot fallback. Coinbase now lists every tracked coin."""
    async def one(s: dict) -> tuple[str, Optional[float]]:
        try:
            r = await client.get(_COINBASE_URL.format(sym=s["asset"]))
            r.raise_for_status()
            amt = ((r.json() or {}).get("data") or {}).get("amount")
            return s["asset"], (float(amt) if amt is not None else None)
        except Exception:
            return s["asset"], None

    results = await asyncio.gather(*[one(s) for s in SERIES])
    return {a: px for a, px in results if px is not None}


async def _spots_coingecko(client: httpx.AsyncClient) -> dict[str, float]:
    """Original source, now last-resort (free tier rate-limits hard)."""
    ids = ",".join(s["cg"] for s in SERIES)
    # precision=full: by default it rounds, and XRP came back as 1.32 for 1.3252.
    resp = await client.get(_COINGECKO_URL, params={
        "ids": ids, "vs_currencies": "usd", "precision": "full"})
    resp.raise_for_status()
    data = resp.json()
    out: dict[str, float] = {}
    for s in SERIES:
        px = (data.get(s["cg"]) or {}).get("usd")
        if px is not None:
            out[s["asset"]] = float(px)
    return out


_SPOT_SOURCES = [
    ("coinbase", _spots_coinbase),
    ("coingecko", _spots_coingecko),
]


async def fetch_spots() -> tuple[dict[str, float], str]:
    """Every tracked asset's USD spot, with the source that supplied it.

    Returns ``({asset: usd}, source)``. Tries each feed in turn and caches
    the first non-empty result for `_SPOT_CACHE_TTL` seconds so the three
    callers (monitor, executor, recorder) share one upstream request. If
    every feed fails it serves the last good cache (marked "(stale)") so
    Spot/Open/Δ don't all blank out during a transient outage."""
    spots, source, _stale = await _fetch_spots()
    return spots, source


async def _fetch_spots() -> tuple[dict[str, float], str, set[str]]:
    """`fetch_spots`, plus the assets whose price is that stale last-good
    cache. The source tag alone can't say which: live push feeds still
    overlay the assets they cover during a REST outage."""
    loop = asyncio.get_event_loop()
    now = loop.time()

    def _overlay(spots: dict, source: str,
                 stale: bool = False) -> tuple[dict[str, float], str, set[str]]:
        # Coinbase WS (live, sub-second) > (cached) REST. The overlay runs on
        # EVERY call, so even a 5s-cached REST snapshot serves live prices. The
        # settlement index is not an option here: its 1-second series is a
        # trailing 60s average, about 30s behind the market.
        cb = spot_ws.fresh_spots()
        rest_only = {a for a in spots if a not in cb}
        stale_assets = rest_only if stale else set()
        if not cb:
            return spots, source, stale_assets
        merged = {**spots, **cb}
        parts = ["coinbase-ws"]
        if rest_only:
            parts.append(source)
        return merged, "+".join(parts), stale_assets

    if _spot_cache["spots"] and (now - _spot_cache["at"]) < _SPOT_CACHE_TTL:
        return _overlay(dict(_spot_cache["spots"]), _spot_cache["source"])

    client = _get_spot_client()
    for name, fn in _SPOT_SOURCES:
        try:
            spots = await fn(client)
        except Exception as e:
            logger.debug(f"crypto15m spot source {name} failed: {e}")
            continue
        if spots:
            _spot_cache.update(at=now, spots=dict(spots), source=name)
            return _overlay(dict(spots), name)

    if _spot_cache["spots"]:  # every feed down — last good values, flagged stale
        return _overlay(dict(_spot_cache["spots"]), f"{_spot_cache['source']} (stale)",
                        stale=True)
    return _overlay({}, "unavailable")


# ───────── underlying technical indicators (detection-only) ───────────
#
# MACD/RSI on the underlying's 1-minute closes, surfaced as OPTIONAL rule
# fields + logged by the recorder so backtest.py can measure whether they add
# edge before they ever gate a trade. Candles come from Hyperliquid's public
# `candleSnapshot` (the same venue the source video used): keyless, US-reachable,
# and it lists every asset we track (incl. HYPE/BNB) — unlike CryptoCompare's
# 1-min history, which now 401s without an API key. One POST per asset, cached
# ~60s (candles are 1-minute, so polling faster only spends rate limit). Fully
# degradable: any failure leaves the indicator fields None, like a missing book.
# ── live-spot velocity ─────────────────────────────────────────────────
# Rolling live-spot history per asset, appended on every snapshot pass
# (~2-25s apart depending on caller cadence). Powers a REAL-TIME
# velocity1mPct: the candle-derived one (Hyperliquid REST 1-min bars behind
# a 60s cache) arrives 60-120s late — past the momentum edge's measured
# ~30s half-life, which is why the armed velocity gate kept missing live
# crossings (2026-07-17: BTC crossed |0.1%|/min 62x in 24h, probe fired 0).
_SPOT_HIST_KEEP_S = 360.0
_spot_hist: dict[str, "deque[tuple[float, float]]"] = {}


def _note_spot(asset: str, spot: Optional[float]) -> None:
    if spot is None:
        return
    dq = _spot_hist.setdefault(asset, deque())
    now = time.time()
    dq.append((now, float(spot)))
    while dq and now - dq[0][0] > _SPOT_HIST_KEEP_S:
        dq.popleft()


def live_velocity_pct(
    asset: str, spot: Optional[float],
    horizon_s: float = 60.0, tol_s: float = 35.0,
) -> Optional[float]:
    """(spot now / spot ~60s ago − 1)·100 from the rolling live-spot history.
    Reference = the newest sample at least `horizon_s` old, accepted up to
    horizon_s+tol_s stale. None until history covers the horizon (callers
    then fall back to the candle-derived velocity)."""
    if spot is None:
        return None
    dq = _spot_hist.get(asset)
    if not dq:
        return None
    now = time.time()
    ref_t = ref_p = None
    for t, p in reversed(dq):
        if now - t >= horizon_s:
            ref_t, ref_p = t, p
            break
    if ref_t is None or (now - ref_t) > horizon_s + tol_s or not ref_p or ref_p <= 0:
        return None
    return (float(spot) / ref_p - 1.0) * 100.0


_HYPERLIQUID_URL = "https://api.hyperliquid.xyz/info"
# Minutes of 1-min history to pull — enough warm-up for a stable MACD signal
# line (slow 26 + signal 9 = 35) with comfortable headroom.
_INDICATOR_LOOKBACK_MIN = 90
_INDICATOR_CACHE_TTL = 60.0
# asset -> {"at": loop_time, "data": {macdHist, macdCross, rsi, ...}}
_indicator_cache: dict[str, dict] = {}


async def _fetch_closes(
    asset: str, client: httpx.AsyncClient, lookback_min: int
) -> tuple[list[float], list[float]]:
    """The last `lookback_min` one-minute (closes, volumes) for `asset` from
    Hyperliquid, oldest→newest. Volumes feed the VWAP field (TWAP fallback when
    absent). Empty lists on any error (caller degrades to None fields)."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {
        "type": "candleSnapshot",
        "req": {
            "coin": asset,
            "interval": "1m",
            "startTime": now_ms - int(lookback_min) * 60_000,
            "endTime": now_ms,
        },
    }
    resp = await client.post(_HYPERLIQUID_URL, json=body)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list):
        raise RuntimeError("unexpected candleSnapshot shape")
    closes: list[float] = []
    volumes: list[float] = []
    # DROP THE IN-PROGRESS CANDLE. Hyperliquid's candleSnapshot ends at `now`,
    # so the final row is the CURRENT minute, still forming — its "close" is
    # just the price a few seconds in. Including it made every momentum field
    # measure the elapsed fraction of a minute instead of a whole one:
    # velocity1mPct read +0.0000 at 23:24:02 where the completed minute was
    # -0.0062. Measured over a day, live |velocity1mPct| came out ~3x smaller
    # than the same statistic computed from completed candles (median 0.0075
    # vs 0.0241), so thresholds calibrated on real 1-minute moves — like the
    # momentum script's 0.1% — fired ~0.15% of the time instead of ~6.9%.
    # Also affects change5m/15m, MACD, RSI, EMA/SMA and VWAP, and made LIVE
    # rows incomparable with the backfilled historical ones.
    cutoff_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows = [r for r in rows
            if not (isinstance(r, dict) and r.get("T") is not None
                    and float(r["T"]) > cutoff_ms)]
    for r in rows:
        c = r.get("c") if isinstance(r, dict) else None
        if not c:
            continue
        try:
            closes.append(float(c))
        except (TypeError, ValueError):
            continue
        try:
            v = r.get("v")
            volumes.append(float(v) if v is not None else 0.0)
        except (TypeError, ValueError):
            volumes.append(0.0)
    return closes, volumes


async def asset_indicators(asset: str) -> dict:
    """MACD/RSI bundle for one asset's underlying, cached ~60s and shared by
    the monitor poll, executor tick and recorder. All-None on any failure or
    until there's enough candle history."""
    loop = asyncio.get_event_loop()
    now = loop.time()
    cached = _indicator_cache.get(asset)
    if cached and (now - cached["at"]) < _INDICATOR_CACHE_TTL:
        return cached["data"]
    try:
        closes, volumes = await _fetch_closes(asset, _get_spot_client(), _INDICATOR_LOOKBACK_MIN)
        data = indicators.compute(closes, volumes)
    except Exception as e:
        logger.debug(f"crypto15m indicators {asset} failed: {e}")
        data = indicators.compute([])  # all-None bundle
    _indicator_cache[asset] = {"at": now, "data": data}
    return data


def _blank_asset(entry: dict, spot: Optional[float], error: Optional[str] = None) -> dict:
    return {
        "asset": entry["asset"], "series": entry["series"],
        "spotUsd": spot, "open15mUsd": None, "deltaUsd": None, "deltaPct": None,
        "hasMarket": False, "ticker": None, "closeTime": None, "minsLeft": None,
        "upProb": None, "downProb": None, "favorite": None,
        "favoritePrice": None, "entryCost": None, "yesBid": None, "yesAsk": None,
        "inWindow": False, "signal": False, "openMarketCount": 0, "error": error,
        # timing + cross-asset correlation (filled in by snapshot()); exposed as
        # optional rule-builder fields, never forced gates.
        "hourUtc": None, "peersAgree": None, "marketBias": None,
        # underlying technical indicators (detection-only, optional rule fields).
        "macd": None, "macdSignal": None, "macdHist": None,
        "macdCross": None, "rsi": None,
        # trend / VWAP fields (Turbine strategy library; detection-only).
        "vwap1h": None, "ema12": None, "sma20": None, "sma50": None,
        "priceVsVwapPct": None, "ema12VsSma20Pct": None, "ema1VsSma5Pct": None,
        "velocity1mPct": None, "change5mPct": None, "change15mPct": None,
        # REAL-TIME CLOB book (WS feed) best bid/ask for the favorite side, in
        # cents — the live price the bot fills on, vs the Gamma mid above.
        "wsBid": None, "wsAsk": None,
        # which source priced the decision: "ws" (live book) or "gamma" (fallback)
        "priceSource": "gamma",
    }


def _friendly_market_error(e) -> str:
    """User-facing label for a failed/timed-out market fetch.

    The raw exception name ('TimeoutError') reads like an app bug, but the cause
    is almost always that Polymarket's Gamma API is unreachable or slow from the
    user's network/region (it's Cloudflare-fronted and geo-restricted) — while
    the SPOT price, a DIFFERENT host, still loads. Point the user at the real fix
    instead of a cryptic type name."""
    name = type(e).__name__
    msg = str(e).lower()
    if ("403" in msg or "451" in msg or "forbidden" in msg
            or "blocked" in msg or "unavailable for legal" in msg):
        return "Polymarket markets unavailable on your network/region"
    if (name in ("TimeoutError", "TimeoutException", "ConnectTimeout",
                 "ReadTimeout", "PoolTimeout", "ConnectError", "NetworkError",
                 "ConnectionError", "ReadError")
            or "timeout" in msg or "connect" in msg or "network" in msg
            or "unreachable" in msg or "temporarily unavailable" in msg):
        return "Polymarket API unreachable — check your connection"
    return f"Market data unavailable ({name})"


def hours_ok(cfg: dict, hour: Optional[int] = None) -> bool:
    """Is the (UTC) hour inside the configured trading window?

    [start, end) in UTC hours; start == end (or the full 0..24) means
    always-on; start > end wraps overnight (e.g. 22 -> 6)."""
    try:
        start = int(cfg.get("crypto15m_hours_start_utc", 0) or 0)
        end = int(cfg.get("crypto15m_hours_end_utc", 24) or 24)
    except (TypeError, ValueError):
        return True
    start, end = start % 24, (end % 24 if end != 24 else 24)
    if start == end or (start == 0 and end == 24):
        return True
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ───────── per-hour config schedule (the "parlay" overlay) ─────────────
# A schedule maps each UTC hour ("0".."23") to a partial crypto15m_* config
# override, so every clock hour can run the entry parameters that were
# individually optimized for that hour's data. An hour ABSENT from the dict
# is unseated — the engine does not open new positions during it.


def hour_override(cfg: dict, hour: Optional[int] = None) -> Optional[dict]:
    """The effective config for `hour` under cfg["crypto15m_hour_configs"].

    No schedule set (key absent/None) -> cfg unchanged. Schedule set and the
    hour seated -> a merged copy (override wins). Schedule set but the hour
    unseated -> None (= don't trade this hour). Never mutates cfg.

    An EMPTY schedule dict is "set, nothing seated" -> None for every hour, NOT
    "no schedule". Conflating the two fell through to the base config while the
    caller still stamped the rows as parlay, which exempts them from every exit."""
    hcs = cfg.get("crypto15m_hour_configs")
    if hcs is None or not isinstance(hcs, dict):
        return cfg
    if hour is None:
        hour = datetime.now(timezone.utc).hour
    ov = hcs.get(str(int(hour)))
    if not isinstance(ov, dict):
        return None
    merged = dict(cfg)
    merged.update(ov)
    return merged


def resignal_asset(asset: dict, cfg: dict) -> dict:
    """Recompute the config-derived snapshot fields (inWindow, signal) under a
    DIFFERENT config than the one the snapshot was built with — the per-hour
    override. The snapshot bakes the base config's window/threshold into
    `signal`, so an overlay that only re-gates should_enter would still be
    reading the OLD signal. Mirrors the snapshot/replay signal expression."""
    a = dict(asset)
    ml = a.get("minsLeft")
    in_window = ml is not None and float(ml) <= _const(cfg, "time_delay_min")
    fav_price = a.get("favoritePrice")
    entry_cost = a.get("entryCost")
    dp = a.get("deltaPct")
    a["inWindow"] = bool(in_window)
    a["signal"] = bool(
        in_window
        and fav_price is not None
        and float(fav_price) >= _const(cfg, "entry_threshold")
        and (entry_cost is None or float(entry_cost) <= _const(cfg, "entry_max"))
        and (dp is None or float(dp) >= _const(cfg, "min_delta_pct"))
        and hours_ok(cfg, hour=a.get("hourUtc"))
    )
    return a


# ───────── spot-lag settlement model (detection + 'model' direction mode) ──
#
# SETTLEMENT RULE: Polymarket US up/down windows resolve on a CF Benchmarks
# real-time index (BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI). The price to beat
# and the settlement value are each the simple average of the 1-second index
# prices in the 60 seconds before the boundary, and the exchange publishes the
# price to beat on the market when the window opens. Up wins when settlement is
# at or above it. Both ends are averages, on every horizon.
#
# The model: treat spot as Brownian with per-second variance (σ_1m·spot)²/60
# (σ_1m the realized 1-min return vol), with r seconds to the close and an
# L = 60s settlement average:
#   r ≥ L: the whole average is still ahead — mean = spot,
#          var = σ_s²·(r − L + L/3)        (the average of the last L seconds
#                                            of a path that starts r away)
#   r < L: (L − r)s of the average are ALREADY OBSERVED (`known_sum` /
#          `known_count` from the live 1-second ring) and only r seconds are
#          left: mean = ((L−r)·known_avg + r·spot)/L, var = σ_s²·r³/(3L²).
# So inside the final minute the outcome firms up much faster than a
# terminal-spot model thinks — and a late spike moves it far less (it only
# owns r/L of the average). The edge source is unchanged: the CLOB book lags
# live spot near the close.
#
# Strike: the price to beat the exchange publishes on the market. Before it
# appears, the settlement index candle for the open boundary (the same number).
# Without either we use our own 60s live-spot average ending at the open, and
# only then a single boundary print ("spot": the model entry gate refuses that
# one, since it is not the price to beat).

SETTLE_TWAP_SEC = 60.0
# Strike sources the model-direction entries may trade on.
STRIKE_SOURCES_OK = ("twap", "spot-avg")


def settle_twap_sec(interval: str) -> float:
    """Length of the settlement average. 60s on every US horizon."""
    return SETTLE_TWAP_SEC


def twap_settlement_since(interval: str) -> Optional[str]:
    """First UTC date a series settled on the average, for excluding older
    recorded data. None: this venue has only ever settled that way."""
    return None


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Polymarket US fee (docs.polymarket.us/fees; live markets stamp feeCoefficient):
# TAKER fee per share = rate × (p·(1−p))^exponent — a symmetric curve peaking at
# about 1.74c at 50c and collapsing at the extremes (~0.2c at 97c). Base-tier
# makers pay 0. Each market's actual schedule rides in on the snapshot via
# `fee_schedule`; these are the fallbacks.
FEE_RATE_CRYPTO = 0.0695
FEE_EXPONENT_CRYPTO = 1.0


def _fee_cents(price_cents: float, schedule: Optional[dict] = None) -> float:
    """Taker fee in cents/share at `price_cents`, using the market's own fee
    schedule when Gamma provided one (else the crypto-category curve)."""
    if schedule is not None and not schedule.get("enabled", True):
        return 0.0
    rate, exponent = FEE_RATE_CRYPTO, FEE_EXPONENT_CRYPTO
    if schedule is not None:
        # Explicit None checks: a schedule reporting rate 0 (or exponent 0)
        # means exactly that — an `or` here would silently re-charge 0.07 on
        # a market Gamma says is free.
        if schedule.get("rate") is not None:
            rate = float(schedule["rate"])
        if schedule.get("exponent") is not None:
            exponent = float(schedule["exponent"])
    # Clamp to [0, 1] only (backtest.polymarket_fee_per_contract does the
    # same) — the old 0.99 ceiling doubled the fee on 99.5c deep favorites,
    # making the live gate disagree with the backtest.
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    return rate * ((p * (1.0 - p)) ** exponent) * 100.0 * (1.0 - TAKER_REBATE)


# The user's Taker Rebate Program tier (fraction), set from config — see
# backtest.TAKER_REBATE. Kept here too so this module needs no backtest import.
TAKER_REBATE = 0.0


def set_taker_rebate(pct) -> None:
    global TAKER_REBATE
    try:
        TAKER_REBATE = max(0.0, min(30.0, float(pct or 0.0))) / 100.0
    except (TypeError, ValueError):
        TAKER_REBATE = 0.0


def model_up_prob(
    spot: Optional[float], strike: Optional[float],
    sigma_1m: Optional[float], mins_left: Optional[float],
    *, known_sum: float = 0.0, known_count: int = 0,
    settle_twap_sec: float = SETTLE_TWAP_SEC,
) -> Optional[float]:
    """P(up): probability the settlement value (the `settle_twap_sec` TWAP at
    the close; a single print when 0) is ≥ the strike. `known_sum` /
    `known_count` are the live 1-second prints already observed inside the
    settlement window — ignored outside it. See the block comment above."""
    if spot is None or strike is None or sigma_1m is None or mins_left is None:
        return None
    if spot <= 0 or strike <= 0 or sigma_1m <= 0:
        return None
    L = float(settle_twap_sec or 0.0)
    if L <= 0:
        # Legacy terminal-spot model (point-in-time settlement).
        t = max(0.05, float(mins_left))
        sd_abs = sigma_1m * math.sqrt(t) * spot
        if sd_abs <= 0:
            return None
        return max(0.0, min(1.0, _norm_cdf((spot - strike) / sd_abs)))
    r = max(0.0, float(mins_left) * 60.0)
    var_s = (sigma_1m * spot) ** 2 / 60.0       # per-second price variance
    if r >= L:
        mean = spot
        var = var_s * (r - L + L / 3.0)
    else:
        elapsed = L - r
        known_avg = (known_sum / known_count) if known_count > 0 else spot
        mean = (elapsed * known_avg + r * spot) / L
        var = var_s * r ** 3 / (3.0 * L * L)
    # Floor: a ~1-second residual so the very last instant is near-certain
    # rather than a division by zero.
    var = max(var, var_s / (3.0 * L * L))
    sd_abs = math.sqrt(var)
    if sd_abs <= 0:
        return None
    return max(0.0, min(1.0, _norm_cdf((mean - strike) / sd_abs)))


def model_edge_net_cents(
    up_prob: Optional[float], yes_ask: Optional[float], no_ask: Optional[float],
    fee_schedule: Optional[dict] = None,
) -> Optional[float]:
    """Best fee-adjusted cents of edge the model sees on EITHER side: buy-up
    edge = P(up)·100 − upAsk − fee, buy-down mirrored. Positive = the model
    thinks a side is underpriced net of the taker fee (each side's fee is
    evaluated at its own ask on the fee curve); which side is implied by
    upProb vs the asks. None when the model or both asks are unavailable."""
    if up_prob is None:
        return None
    edges = []
    if yes_ask and 0 < yes_ask < 1:
        ask_c = yes_ask * 100.0
        edges.append(up_prob * 100.0 - ask_c - _fee_cents(ask_c, fee_schedule))
    if no_ask and 0 < no_ask < 1:
        ask_c = no_ask * 100.0
        edges.append((1.0 - up_prob) * 100.0 - ask_c - _fee_cents(ask_c, fee_schedule))
    if not edges:
        return None
    return round(max(edges), 2)


# Without a ring print at the boundary, a window first observed later than
# this after its open gets NO strike: a late capture is not "approximately"
# the open, it's a different price — live incident 2026-07-03: an app
# (re)start 44s into a 5m BTC window captured a strike 32 points below the
# real open, which made the model ~2-sigma "certain" of UP on a
# coin-flip window and bought 50c contracts the live book was correctly
# pricing at 50/50 (it lost). Windows the ring can't cover sit out model
# entries rather than run on a fabricated strike.
_STRIKE_GRACE_SEC = 12.0


def _ring_avg(asset: str, end_epoch: float,
              min_count: int = 45) -> Optional[float]:
    """Our own average of live 1-second Coinbase prints over the 60s ending at
    `end_epoch`. None when too sparse to stand in for the official value."""
    try:
        total, count = spot_ws.window_partial(asset, end_epoch, now=end_epoch)
    except Exception:
        return None
    return total / count if count >= min_count else None


def _track_window_open(
    asset: str, window_start: int, spot: Optional[float],
    now_epoch: Optional[float] = None, published: Optional[float] = None,
) -> Optional[float]:
    """Record a window's strike (the price to beat) and return it.

    Priority:
      1. the value the exchange published on the market ("twap");
      2. the settlement index candle for the open boundary ("twap": the same
         number, available a moment before the market carries it);
      3. our own 60s average of Coinbase spot ending at the open ("spot-avg");
      4. the boundary print from the Coinbase ring, or the current spot within
         _STRIKE_GRACE_SEC of the open ("spot": approximate, and model entries
         refuse it);
      5. otherwise None for the window's whole life. A mid-window restart with
         nothing above sits out rather than fabricate a strike.
    A strike recorded from a fallback is upgraded the moment an official value
    shows up."""
    key = (asset, window_start)
    cur = _window_open.get(key)
    if published:
        _window_open[key] = (float(published), "twap")
        return float(published)
    if cur is not None and cur[1] == "twap":
        return cur[0]
    official = index_feed.twap_at(asset, window_start)
    if official is not None:
        _window_open[key] = (official, "twap")
    elif cur is None:
        avg = _ring_avg(asset, window_start)
        if avg is not None:
            _window_open[key] = (avg, "spot-avg")
        else:
            strike = spot_ws.sample_at(asset, window_start)
            if strike is None:
                if now_epoch is not None and (now_epoch - window_start) > _STRIKE_GRACE_SEC:
                    return None  # too late to know this window's open — sit it out
                strike = spot
            if strike is None:
                return None
            _window_open[key] = (strike, "spot")
    if len(_window_open) > 200:
        for old in sorted(_window_open, key=lambda k: k[1])[:50]:
            _window_open.pop(old, None)
    return _window_open[key][0]


def strike_source(asset: str, window_start: int) -> Optional[str]:
    rec = _window_open.get((asset, window_start))
    return rec[1] if rec else None


def known_partial(asset: str, close_epoch: float, now_epoch: float) -> tuple[float, int]:
    """Live 1-second prints already inside the settlement average
    [close − 60s, now], from the Coinbase ring (the index's own constituent).
    The index series itself can't serve here: each candle is already a 60s
    average, so summing them isn't the observed part of this window."""
    try:
        total, count = spot_ws.window_partial(asset, close_epoch, now=now_epoch)
    except Exception:
        return 0.0, 0
    return (total, count) if count > 0 else (0.0, 0)


def _ws_mid(q: Optional[dict]) -> Optional[float]:
    """Sane best-bid/ask mid (0..1) from a live WS quote, or None if the book is
    one-sided / crossed / out of range (so the caller falls back to Gamma)."""
    if not q:
        return None
    b, a = q.get("bid_cents"), q.get("ask_cents")
    if b is None or a is None or not (0 < b <= a <= 100):
        return None
    return (b + a) / 200.0


def _ws_up_price(yes_q: Optional[dict], no_q: Optional[dict]) -> Optional[float]:
    """Real 'up' probability (0..1) from the live CLOB book, or None if unusable.
    Averages the YES mid and (1 − NO mid) when both are sane for a tighter, less
    one-sided estimate; else uses whichever side is sane."""
    ym, nm = _ws_mid(yes_q), _ws_mid(no_q)
    if ym is not None and nm is not None:
        return max(0.0, min(1.0, (ym + (1.0 - nm)) / 2.0))
    if ym is not None:
        return ym
    if nm is not None:
        return max(0.0, min(1.0, 1.0 - nm))
    return None


def _ws_fav_ask(favorite: str, yes_q: Optional[dict], no_q: Optional[dict]) -> Optional[float]:
    """Real cost to BUY the favorite side = its best ASK (fraction), or None."""
    q = yes_q if favorite == "up" else no_q
    a = (q or {}).get("ask_cents")
    if a is None or not (0 < a <= 100):
        return None
    return a / 100.0


async def _asset_snapshot(entry: dict, spot: Optional[float], cfg: dict, now_epoch: float,
                          *, spot_stale: bool = False) -> dict:
    asset, series = entry["asset"], entry["series"]
    interval = _interval(cfg)
    # The settlement model only runs on a spot that is live. A frozen last-good
    # price during a feed outage reads as a mispriced book and buys it.
    model_spot_ok = not spot_stale
    _note_spot(asset, spot)  # feed the live-velocity rolling history
    try:
        # fast=True: one quick Gamma attempt so a slow/blocked endpoint degrades
        # to a friendly "unreachable" inside the per-asset budget rather than
        # burning ~40s of retries and hard-timing-out the whole board.
        markets = await polymarket_api.fetch_crypto_updown(asset, interval, fast=True)
    except Exception as e:
        return _blank_asset(entry, spot, _friendly_market_error(e))

    # The active contract is the soonest-closing window still in the future.
    # Close timing comes from the slug-derived window (the Gamma endDate on
    # these markets is a far-future placeholder, so we never trust it).
    candidates: list[tuple[float, dict]] = []
    for m in markets:
        ce = float(m.get("window_close_epoch") or 0)
        if ce <= now_epoch:
            continue
        candidates.append((ce, m))

    out = _blank_asset(entry, spot)
    out["openMarketCount"] = len(candidates)
    if not candidates:
        return out

    candidates.sort(key=lambda x: x[0])
    close_epoch, m = candidates[0]

    window_start = int(m.get("window_start_epoch") or (close_epoch - _INTERVAL_SEC[interval]))
    open15m = _track_window_open(asset, window_start, spot, now_epoch,
                                 published=m.get("price_to_beat"))
    delta = abs(open15m - spot) if (open15m is not None and spot is not None) else None
    delta_pct = (delta / open15m) if (delta is not None and open15m) else None

    # ── Price the market for the ENTRY DECISION. Prefer the live feed (it needs an
    # API key), then the uncached REST read, over the cached public board row. The
    # executor still does a final re-price + cap at order time.
    yt, nt = m.get("yes_token"), m.get("no_token")
    ws_yes = ws_no = None
    if cfg.get("crypto15m_ws_book", True):
        try:
            clob_ws.feed.observe(yt, nt)
            clob_ws.feed.start()
            ws_yes = clob_ws.feed.get_quote_cents(yt)
            ws_no = clob_ws.feed.get_quote_cents(nt)
        except Exception as e:
            logger.debug(f"crypto15m ws book {asset}: {e}")

    # No live feed (no API key, or it dropped): the running window's quote was just
    # re-read uncached, so it is a real touch, as good as the feed for this tick.
    rest_priced = False
    if ws_yes is None and ws_no is None and m.get("quote_fresh"):
        rb, ra = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
        if rb and ra and 0.0 < rb <= ra < 1.0:
            ws_yes = {"bid_cents": us.bid_cents(rb), "ask_cents": us.ask_cents(ra)}
            ws_no = {"bid_cents": us.bid_cents(1.0 - ra), "ask_cents": us.ask_cents(1.0 - rb)}
            rest_priced = True

    up_ws = _ws_up_price(ws_yes, ws_no)
    if up_ws is not None:
        up = up_ws
        price_source = "rest" if rest_priced else "ws"
        yes_bid = (ws_yes.get("bid_cents") / 100.0) if (ws_yes and ws_yes.get("bid_cents")) else up
        yes_ask = (ws_yes.get("ask_cents") / 100.0) if (ws_yes and ws_yes.get("ask_cents")) else up
    else:
        # Public fallback: YES is Up (100 of 100 settled windows, 2026-11-05), and its
        # mid is the up probability. This row can be up to 50s old.
        up = max(0.0, min(1.0, _price_dollars(m, "last_price")
                          or _price_dollars(m, "yes_bid")))
        price_source = "gamma"
        yes_bid = up
        yes_ask = up
    down = 1.0 - up
    favorite = "up" if up >= down else "down"
    fav_price = up if favorite == "up" else down

    # Cost to BUY the favorite = its real best ASK when we have the live book
    # (what you actually pay); else the Gamma mid (refined at order time anyway).
    entry_cost = _ws_fav_ask(favorite, ws_yes, ws_no)
    if entry_cost is None:
        entry_cost = max(0.0, min(1.0, fav_price))
    # Real best bid/ask for the favorite side (display + recorded).
    fav_q = ws_yes if favorite == "up" else ws_no
    if fav_q:
        out["wsBid"] = fav_q.get("bid_cents")
        out["wsAsk"] = fav_q.get("ask_cents")
    out["priceSource"] = price_source

    mins_left = (close_epoch - now_epoch) / 60.0
    hour_utc = datetime.fromtimestamp(now_epoch, timezone.utc).hour
    in_window = mins_left <= _const(cfg, "time_delay_min")
    signal = (
        in_window
        and hours_ok(cfg)
        and fav_price >= _const(cfg, "entry_threshold")
        and entry_cost <= _const(cfg, "entry_max")
        and (delta_pct is None or delta_pct >= _const(cfg, "min_delta_pct"))
    )

    # Signed move from the window open: the model's distance-to-strike in %
    # terms (deltaPct is the ABS version kept for the legacy gate/rules).
    delta_signed_pct = (
        ((spot - open15m) / open15m)
        if (spot is not None and open15m) else None
    )

    out.update({
        "open15mUsd": open15m, "deltaUsd": delta, "deltaPct": delta_pct,
        "deltaSignedPct": round(delta_signed_pct, 6) if delta_signed_pct is not None else None,
        "strikeUsd": open15m,
        "strikeSource": strike_source(asset, window_start),
        "settleTwapUsd": index_feed.twap(asset),
        "hasMarket": True, "ticker": m.get("ticker"),
        "closeTime": m.get("close_time"), "minsLeft": round(mins_left, 2),
        "upProb": round(up, 4), "downProb": round(down, 4),
        "favorite": favorite, "favoritePrice": round(fav_price, 4),
        "entryCost": round(entry_cost, 4),
        "yesBid": round(yes_bid, 4) if yes_bid else None,
        "yesAsk": round(yes_ask, 4) if yes_ask else None,
        "inWindow": in_window, "signal": signal, "hourUtc": hour_utc,
    })

    # Both sides' asks are always exposed — the settlement model and rule
    # builder need them regardless of the arb detector toggle.
    _up_ask_c = (ws_yes or {}).get("ask_cents")
    _dn_ask_c = (ws_no or {}).get("ask_cents")
    if _up_ask_c:
        out["upAsk"] = round(_up_ask_c / 100.0, 4)
    if _dn_ask_c:
        out["downAsk"] = round(_dn_ask_c / 100.0, 4)

    # Up + Down ≠ $1 arbitrage detection (market-neutral edge). Detection only.
    # Prefer the live WS asks (no REST) when both are fresh; else REST fallback.
    if cfg.get("crypto15m_arb_detect", True):
        thresh = float(cfg.get("crypto15m_arb_min_edge_cents", 1.0) or 0.0)
        if _up_ask_c and _dn_ask_c:
            edge_c = round(100.0 - (_up_ask_c + _dn_ask_c), 1)
            out["arbEdgeCents"] = edge_c
            out["arbSignal"] = edge_c >= thresh
        else:
            try:
                arb = await polymarket_api.updown_arb_edge(m.get("ticker"))
            except Exception:
                arb = None
            if arb:
                out["upAsk"] = arb["upAsk"]
                out["downAsk"] = arb["downAsk"]
                out["arbEdgeCents"] = arb["edgeCents"]
                out["arbSignal"] = arb["edgeCents"] >= thresh

    # Order-flow / book-imbalance signal on the Up token (record → paper →
    # live: surfaced + logged for backtest, does NOT influence entries yet).
    if cfg.get("crypto15m_imbalance_detect", True):
        try:
            imb = await polymarket_api.book_imbalance(
                m.get("yes_token"), int(cfg.get("crypto15m_imbalance_levels", 3) or 3)
            )
        except Exception:
            imb = None
        out["bookImbalance"] = imb

    # Underlying MACD/RSI (record → rule field; does NOT drive entries unless a
    # user composes a rule on macdHist/macdCross/rsi). Cached ~60s per asset.
    if cfg.get("crypto15m_indicator_detect", True):
        try:
            # Hard cap the (non-critical, 60s-cached) indicator fetch so a slow
            # Hyperliquid call can't, on top of the Gamma fetch, blow the whole
            # asset-snapshot budget (the "crypto engine timeout"). Degrades to
            # all-None and the cache retries next pass.
            ind = await asyncio.wait_for(asset_indicators(asset), 4.0)
        except Exception:
            ind = {}
        out["macd"] = ind.get("macd")
        out["macdSignal"] = ind.get("macdSignal")
        out["macdHist"] = ind.get("macdHist")
        out["macdCross"] = ind.get("macdCross")
        out["rsi"] = ind.get("rsi")
        out["sigma1m"] = ind.get("sigma1m")
        # Trend / VWAP fields (for the Turbine strategy library). Detection-only,
        # recorded per tick + exposed as optional rule-builder fields; they never
        # gate a trade unless a user composes a rule on them.
        for _k in ("vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
                   "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
                   "change5mPct", "change15mPct"):
            out[_k] = ind.get(_k)
        # velocity1mPct: prefer the LIVE spot-feed computation once the
        # rolling history covers the 60s horizon — the candle-derived value
        # above is 60-120s stale (1-min bars + 60s indicator cache), past the
        # momentum edge's ~30s half-life. Falls back to the candle value
        # until history warms up, so the field never regresses to None.
        _lv = live_velocity_pct(asset, spot)
        if _lv is not None:
            out["velocity1mPct"] = round(_lv, 4)
        # Whether this asset's spot came from the live Coinbase feed THIS instant.
        # The final-minute sniper gate reads this off the snapshot (not the feed
        # module) so replay can reconstruct the same decision from recorded ticks.
        out["spotLive"] = spot_ws.spot(asset) is not None
        # Spot-vs-strike settlement model (detection + the 'model' direction
        # mode): P(up at close) from LIVE spot while the market quote lags.
        # Recorded by the tick logger + exposed to the rule builder so users
        # can gate on `edgeNetCents >= X` once their data supports it.
        _L = settle_twap_sec(interval)
        _ks, _kc = known_partial(asset, close_epoch, now_epoch) if _L > 0 else (0.0, 0)
        out["spotStale"] = spot_stale
        mp = model_up_prob(spot if model_spot_ok else None, open15m, ind.get("sigma1m"),
                           mins_left, known_sum=_ks, known_count=_kc, settle_twap_sec=_L)
        out["settleKnownSec"] = _kc
        out["modelProb"] = round(mp, 4) if mp is not None else None
        out["feeSchedule"] = m.get("fee_schedule")
        out["edgeNetCents"] = model_edge_net_cents(
            mp, out.get("upAsk"), out.get("downAsk"), m.get("fee_schedule"),
        )
    return out


# Minutes in one window of each supported interval, for `timeFracLeft`.
_INTERVAL_MINUTES = {"5m": 5.0, "15m": 15.0, "1h": 60.0, "hourly": 60.0}


def derive_script_fields(a: dict, interval: str = "15m") -> None:
    """Add the convenience fields scripts read, IN PLACE.

    Called from BOTH the live snapshot and the backtest's tick_to_asset, so
    these can never drift between the two — which is the whole reason they
    live here instead of being recomputed at each call site. Every field is a
    pure function of values already present in the dict (no per-window memory,
    no clock), so live and replay produce identical results from identical
    inputs. Anything missing stays None: a script comparing a None it didn't
    check crashes and gets disabled, so silence is never an option.
    """
    yb, ya = a.get("yesBid"), a.get("yesAsk")
    a["spreadCents"] = (
        round((float(ya) - float(yb)) * 100.0, 2)
        if yb is not None and ya is not None else None
    )

    # Signed disagreement between the app's model and the book's own mid, in
    # percentage points: > 0 = the model thinks UP is underpriced.
    mp, up = a.get("modelProb"), a.get("upProb")
    a["modelEdgePts"] = (
        round((float(mp) - float(up)) * 100.0, 2)
        if mp is not None and up is not None else None
    )

    # The favorite's executable ask in cents — every script otherwise rewrites
    # the same up/down branch, and gets it wrong when the book is one-sided.
    fav = a.get("favorite")
    fav_ask = a.get("upAsk") if fav == "up" else (
        a.get("downAsk") if fav == "down" else None)
    a["favoriteAskCents"] = (
        round(float(fav_ask) * 100.0, 1) if fav_ask is not None else None)

    # Fraction of the window still to run, 0..1. Lets one script work across
    # the 5m / 15m / hourly series without hard-coding minute thresholds.
    ml = a.get("minsLeft")
    span = _INTERVAL_MINUTES.get(str(interval or "15m"))
    a["timeFracLeft"] = (
        round(max(0.0, min(1.0, float(ml) / span)), 4)
        if ml is not None and span else None
    )


# Hard per-operation bounds so one slow/hung upstream fetch can't stall the
# whole snapshot — which would freeze the Crypto tab AND the executor tick that
# shares this call. Comfortably under the renderer's 30s RPC timeout.
_SPOT_FETCH_TIMEOUT = 6.0
_ASSET_SNAPSHOT_TIMEOUT = 10.0


def _apply_cross_asset(assets: list[dict], now_epoch: float) -> None:
    """Compute timing + cross-asset correlation fields across the whole snapshot
    and inject them per-asset (so the rule builder can gate on them):

      hourUtc     current UTC hour 0-23 (also stamped on blank/errored assets)
      marketBias  market-wide directional lean = (#up - #down) / #withFavorite,
                  range -1..1; same for every asset (a breadth gauge)
      peersAgree  fraction of the OTHER favorited assets whose favorite matches
                  this asset's, range 0..1 — the correlation gate: high = the
                  pack agrees (herding/momentum), low = this coin diverges.

    These are OPTIONAL rule fields, never forced gates — assets can and do
    diverge, so the user decides whether agreement helps or hurts their edge."""
    hour_utc = datetime.fromtimestamp(now_epoch, timezone.utc).hour
    favs = [a.get("favorite") for a in assets
            if a.get("hasMarket") and a.get("favorite") in ("up", "down")]
    n_up = favs.count("up")
    n_down = favs.count("down")
    total = n_up + n_down
    market_bias = round((n_up - n_down) / total, 4) if total else None

    for a in assets:
        a["hourUtc"] = hour_utc
        a["marketBias"] = market_bias
        a["peersAgree"] = None
        fav = a.get("favorite")
        if not a.get("hasMarket") or fav not in ("up", "down") or total <= 1:
            continue
        # peers = the other favorited assets (exclude self once)
        peers = total - 1
        same = (n_up - 1) if fav == "up" else (n_down - 1)
        a["peersAgree"] = round(same / peers, 4) if peers else None


_snapshot_cache: dict = {"at": 0.0, "data": None, "interval": None}
_SNAPSHOT_TTL = 3.0


async def snapshot(cfg: dict) -> dict:
    """Build the full monitor snapshot for all seven series. Resilient:
    a failure OR a timeout on the spot feed or any single series degrades that
    field rather than failing/hanging the whole call.

    Cached for ~3s so the 4-second executor poll and the open Crypto tab SHARE
    one snapshot instead of each firing the full ~7-calls-per-series fan-out —
    halving the API load (a 429 hazard). 3s is well inside the executor's poll
    interval and a 15-min window, so the slight staleness is immaterial."""
    now_epoch = datetime.now(timezone.utc).timestamp()
    cached = _snapshot_cache.get("data")
    # The cache must be interval-scoped: on an interval switch (e.g. applying
    # the 5m preset) a cached 15m snapshot served for up to TTL seconds would
    # be recorded/stamped as 5m data — permanent cross-labeling the per-interval
    # backtest separation exists to prevent.
    if (
        cached is not None
        and _snapshot_cache.get("interval") == _interval(cfg)
        and (now_epoch - _snapshot_cache.get("at", 0.0)) < _SNAPSHOT_TTL
    ):
        return cached
    try:
        spots, spot_source, stale = await asyncio.wait_for(_fetch_spots(), _SPOT_FETCH_TIMEOUT)
    except Exception as e:
        logger.debug(f"crypto15m spot fetch failed/timeout: {e}")
        spots = dict(_spot_cache.get("spots") or {})
        spot_source = f"{_spot_cache.get('source', '?')} (stale)" if spots else "unavailable"
        stale = set(spots)
    spot_ok = bool(spots)

    async def _bounded(entry: dict) -> dict:
        try:
            return await asyncio.wait_for(
                _asset_snapshot(entry, spots.get(entry["asset"]), cfg, now_epoch,
                                spot_stale=entry["asset"] in stale),
                _ASSET_SNAPSHOT_TIMEOUT,
            )
        except Exception as e:
            return _blank_asset(entry, spots.get(entry["asset"]), _friendly_market_error(e))

    results = await asyncio.gather(*[_bounded(s) for s in SERIES])
    assets: list[dict] = []
    for s, r in zip(SERIES, results):
        a = r if not isinstance(r, Exception) else _blank_asset(s, spots.get(s["asset"]), _friendly_market_error(r))
        a["enabled"] = asset_enabled(cfg, s["asset"])
        assets.append(a)

    _apply_cross_asset(assets, now_epoch)
    # Script convenience fields last: they read values the per-asset build and
    # the cross-asset pass have both already filled in.
    for a in assets:
        derive_script_fields(a, _interval(cfg))

    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "spotOk": spot_ok,
        "spotSource": spot_source,
        "hoursOk": hours_ok(cfg),
        "constants": {
            "timeDelayMin": _const(cfg, "time_delay_min"),
            "entryThreshold": _const(cfg, "entry_threshold"),
            "exitThreshold": _const(cfg, "exit_threshold"),
            "entryMax": _const(cfg, "entry_max"),
            "minDeltaPct": _const(cfg, "min_delta_pct"),
            "entryDiff": _const(cfg, "entry_diff"),
            "directionMode": str(cfg.get("crypto15m_direction_mode", "favorite")),
            "entryStyle": str(cfg.get("crypto15m_entry_style", "maker")),
            "hoursStartUtc": int(cfg.get("crypto15m_hours_start_utc", 0) or 0),
            "hoursEndUtc": int(cfg.get("crypto15m_hours_end_utc", 24) or 24),
        },
        "assets": assets,
    }
    _snapshot_cache["at"] = now_epoch
    _snapshot_cache["data"] = result
    _snapshot_cache["interval"] = _interval(cfg)
    return result
