"""Polymarket US client: public reads + signed trading.

Two hosts:

  gateway.polymarket.us   public market data, events, search, index prices
  api.polymarket.us       signed trading, portfolio, balances, WebSockets

The rest of the bot (scanner, engines, terminal) reads the dicts this module
returns, keyed the way that code has always expected:

  market identifier  -> `ticker` / `condition_id`    the market slug here
  prices             -> `*_dollars` (0..1 fraction)
  sizes              -> `*_fp`

`us_api` holds the pure translation between those shapes and the venue's,
including how one YES-quoted instrument stands in for a YES and a NO token.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx

import polymarket_auth as auth
import us_api as us

logger = logging.getLogger(__name__)

GATEWAY = us.GATEWAY
API_BASE = us.API_BASE
# Market list pages: 500 verified live 2026-11-05.
MARKET_PAGE_MAX = 500
USER_AGENT = "krypt-polybot-us"
UNKNOWN_ID_PREFIX = "unknown-"
_AMBIGUOUS_LOOKUPS = 2
_AMBIGUOUS_LOOKUP_GAP_SEC = 1.0
_AMBIGUOUS_LOOKUP_TIMEOUT_SEC = 5.0

# Per-request timeout. Kept tight (12s) on purpose: the engines run in one
# serialized loop, so a hung endpoint blocks stop-loss / settlement / the other
# engines for timeout × retries. 12s is well above the typical <2s response, so
# legitimate calls still succeed, but a pathological hang now caps the loop stall
# at ~36s instead of ~79s. (Polymarket APIs normally answer in well under 2s.)
REQUEST_TIMEOUT = 12.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5

_pub_client: Optional[httpx.AsyncClient] = None

# Poisoned-pool self-heal. A transient network event (Wi-Fi drop, sleep/wake,
# ISP blip) can leave the shared client's pooled keepalive connections
# permanently dead on Windows — the Proactor loop keeps handing out half-open
# sockets, so EVERY host (Gamma, Data, CLOB, *and* all Polygon RPCs) then fails
# at once with empty errors and stays that way until the PROCESS is restarted.
# The user's symptom — "have to restart to fix RPC/connection errors" — is
# exactly this. We detect it with a consecutive *cross-endpoint* failure streak
# and rebuild the client (fresh connection pool), which is what a manual restart
# did, minus the restart. The streak resets on ANY successful response, so an
# endpoint-specific outage (e.g. Polygon RPCs regionally blocked while Polymarket
# is fine) keeps resetting it and never triggers a recycle — only a genuine
# "nothing gets through" pool poison does.
_net_fail_streak = 0
_NET_FAIL_RECYCLE = 5            # consecutive terminal net failures → rebuild pool
_last_client_recycle = 0.0
_CLIENT_RECYCLE_MIN_GAP = 20.0   # never rebuild more than ~once per 20s
_last_net_ok = 0.0               # monotonic of last response; 0 = none yet
_NET_STALL_SECS = 45.0           # only rebuild after a REAL stall this long

# Throttle for repeated "GET failed" lines. A burst produced dozens of identical
# lines/second; log at most one per endpoint per window and let the rest stay
# quiet. Per endpoint, not per URL: every market window has its own slug, so a
# network drop logged one line for each of them (16 a second, live 2026-11-07).
_get_fail_log_at: dict = {}
_GET_FAIL_LOG_GAP = 30.0


def _get_fail_key(url: str) -> str:
    return re.sub(r"/slug/.*$", "/slug/*", str(url).split("?", 1)[0])


def _log_get_failed(url: str, e: Exception) -> None:
    now = time.monotonic()
    key = _get_fail_key(url)
    last = _get_fail_log_at.get(key)
    if last is not None and now - last < _GET_FAIL_LOG_GAP:
        return
    _get_fail_log_at[key] = now
    # Include the exception TYPE: httpx network errors often stringify to ""
    # (thousands of reasonless 'GET failed' lines in user logs).
    logger.warning(f"GET failed {url}: {type(e).__name__}: {e}")


# ───────── is the exchange answering? ──────────
# For the alerts. Separate from the pool recycling above, which treats any
# response as healthy: a 500 is a working connection but not a working exchange.
_API_DOWN_FAILS = 3           # this many failures in a row, with no success between
_API_DOWN_SECS = 20.0         # and lasting this long, so one bad burst doesn't alert
_API_PROBE_IDLE_SECS = 45.0   # re-check when nothing else has talked to the exchange
_API_PROBE_FAILING_SECS = 8.0 # sooner while it is failing, to notice either way
_api_state: dict[str, dict] = {}
_api_probe_task: Optional[asyncio.Future] = None


def _api_host(url: str) -> Optional[str]:
    if url.startswith(GATEWAY):
        return "gateway"
    if url.startswith(us.API_BASE):
        return "api"
    return None


def _note_api(url: str, status: Optional[int] = None) -> None:
    """Record one attempt. `status` None means no response came back at all."""
    host = _api_host(url)
    if host is None:
        return
    now = time.monotonic()
    s = _api_state.setdefault(host, {"fails": 0, "firstFail": 0.0, "kind": None, "lastSeen": 0.0})
    s["lastSeen"] = now
    if status is None or status == 429 or status >= 500:
        if not s["fails"]:
            s["firstFail"] = now
        s["fails"] += 1
        s["kind"] = "net" if status is None else ("rate" if status == 429 else "server")
    else:
        s["fails"] = 0
        s["kind"] = None


def api_outages() -> dict:
    """{gateway|api: {kind: net|server|rate, sinceSec}} for hosts not answering now."""
    now = time.monotonic()
    return {host: {"kind": s["kind"], "sinceSec": int(now - s["firstFail"])}
            for host, s in _api_state.items()
            if s["fails"] >= _API_DOWN_FAILS and now - s["firstFail"] >= _API_DOWN_SECS}


def probe_api_if_idle() -> None:
    """A public read, so a quiet app still notices an outage and its end. Costs one
    unsigned request at most every _API_PROBE_IDLE_SECS when all is well."""
    global _api_probe_task
    if _api_probe_task is not None and not _api_probe_task.done():
        return
    s = _api_state.get("gateway") or {}
    wait = _API_PROBE_FAILING_SECS if s.get("fails") else _API_PROBE_IDLE_SECS
    if s.get("lastSeen") and time.monotonic() - s["lastSeen"] < wait:
        return
    _api_probe_task = asyncio.ensure_future(
        _get(f"{GATEWAY}/v1/markets", params={"limit": 1}, timeout=5.0, max_retries=1))


async def _get_client() -> httpx.AsyncClient:
    global _pub_client
    if _pub_client is None or _pub_client.is_closed:
        _pub_client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "KryptPolyBot/1.0",
            },
            # Peak fan-out is the 15m-crypto snapshot: 7 assets × 4 window
            # lookups = 28 concurrent GETs. A 20-slot pool forced the surplus 8
            # to block on pool acquisition and raise PoolTimeout under the
            # snapshot's tight fast-budget — a self-inflicted "failure" that
            # tripped the poison-pool rebuild. Size the pool above peak so
            # concurrency, not the pool, is the limit.
            limits=httpx.Limits(max_connections=48, max_keepalive_connections=24),
            follow_redirects=True,
        )
    return _pub_client


def _note_net_ok() -> None:
    """A response (any status) came back, so the transport/pool is healthy —
    clear the failure streak and mark the moment of last success."""
    global _net_fail_streak, _last_net_ok
    _net_fail_streak = 0
    _last_net_ok = time.monotonic()


async def _note_net_fail() -> None:
    """Record a terminal network failure. Once the streak shows every endpoint is
    failing (pool wedged) AND nothing has succeeded for a real stall window,
    rebuild the shared client's connection pool — the in-process equivalent of
    the restart users currently do by hand."""
    global _net_fail_streak, _last_client_recycle
    _net_fail_streak += 1
    if _net_fail_streak < _NET_FAIL_RECYCLE:
        return
    now = time.monotonic()
    # Only rebuild on a GENUINE stall: nothing has landed for _NET_STALL_SECS. A
    # slow route that still returns the occasional response keeps resetting
    # _last_net_ok and must KEEP its warm TLS connections — tearing them down on
    # a few fast-budget timeouts made the next burst pay a full handshake per
    # request, which is what turned a slow route into a self-amplifying storm.
    if _last_net_ok and (now - _last_net_ok) < _NET_STALL_SECS:
        return
    if now - _last_client_recycle < _CLIENT_RECYCLE_MIN_GAP:
        return
    _last_client_recycle = now
    _net_fail_streak = 0
    logger.warning(
        "network failures across all endpoints — rebuilding HTTP client "
        "(fresh connection pool) to clear a poisoned pool"
    )
    await _recycle_client()


# Other modules keep their own httpx pools (crypto15m's spot/indicator
# client), which poison the same way ours does — a Windows sleep/wake that
# wedges this pool wedges theirs too. They register a recycle callback here
# so ONE streak detector heals every pool at once; before this, crypto15m's
# client was only recycled by the watchdog after a ≥240s loop stall, and a
# fast-fail poison (no stall) left spot/indicators dead until app restart.
_extra_recycle_hooks: list = []


def register_recycle_hook(fn) -> None:
    """fn: async callable that closes/renews that module's HTTP client."""
    if fn not in _extra_recycle_hooks:
        _extra_recycle_hooks.append(fn)


async def _recycle_client() -> None:
    """Swap in a fresh client on the next request, then close the old one.
    Swapping to None FIRST means concurrent callers immediately get a clean
    client; the dead one is closed in the background (errors swallowed)."""
    global _pub_client
    old, _pub_client = _pub_client, None
    if old is not None and not old.is_closed:
        try:
            await old.aclose()
        except Exception:
            pass
    for hook in list(_extra_recycle_hooks):
        try:
            # Tolerate a sync hook: only await when it actually returns an
            # awaitable, so a plain function that returns None can't raise
            # "NoneType can't be awaited" and silently abort its own recycle.
            res = hook()
            if asyncio.iscoroutine(res) or asyncio.isfuture(res):
                await res
        except Exception:
            pass


async def close_clients() -> None:
    global _net_fail_streak
    _net_fail_streak = 0
    await _recycle_client()


class PolymarketAPIError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class LocalOrderRefusal(PolymarketAPIError):
    """This app refused the order before sending it (kill switch, throttle,
    collar, self-cross, unverified convention). Nothing reached the exchange, so
    it must not read like an HTTP error the venue returned."""

    def __str__(self) -> str:
        return str(self.body)


class EngineRestartError(PolymarketAPIError):
    """The exchange reported it is restarting. Nothing was placed."""


class PostOnlyModeError(PolymarketAPIError):
    """Kept for callers' except clauses: this venue has no such window."""


class UnsupportedMarketVersionError(PolymarketAPIError):
    """Kept for callers' except clauses: this venue has no protocol versions."""


class OrderStatusUnknownError(PolymarketAPIError):
    """POST /v1/orders failed without saying whether the exchange took the order
    (a timeout, a dropped connection, a 5xx), and looking for it did not settle
    it either. It may be resting, filled, or have never arrived.

    `order_id` is a placeholder: the venue assigns ids and has no client id or
    idempotency key, so a lost reply loses the id too. A subclass of
    PolymarketAPIError, so a caller that only knows "failed" treats it as a
    failure; a caller whose reconcile polls by id takes `response` and tracks
    it. Polling a placeholder 404s, which every caller answers by trusting the
    positions read rather than assuming a fill."""

    def __init__(self, order_id: str, cause: Exception):
        self.order_id = order_id
        super().__init__(
            int(getattr(cause, "status", 0) or 0),
            f"order status unknown: the send failed ({type(cause).__name__}: "
            f"{cause}) and the order could not be found yet. It may be live, "
            f"filled, or never have arrived")

    @property
    def response(self) -> dict:
        return {"order": {"order_id": self.order_id, "status": "unknown"},
                "raw": {"errorMsg": str(self.body), "unknown": True}}


class PositionsTooLargeError(RuntimeError):
    """Kept for callers' except clauses."""


# ───────── region ───────────────────────────
# Polymarket US publishes no region-check endpoint. Who may trade is settled by
# the account (identity verification in the app), so there is no verdict to cache
# here. A refusal the API actually returns still surfaces, via _geoblocked_at.

REGION_UNKNOWN_MSG = (
    "Polymarket US has no public region check. Eligibility is decided by your "
    "account's identity verification."
)
REGION_BLOCKED_MSG = (
    "Polymarket US refused this order because of where or who it came from. "
    "Check your account's status in the Polymarket US app."
)
REGION_CLOSE_ONLY_MSG = REGION_BLOCKED_MSG
REGION_REFUSED_MSG = REGION_BLOCKED_MSG
_REGION = {"status": "unknown", "country": "", "region": "", "message": REGION_UNKNOWN_MSG}
_geoblocked_at = 0.0


def geoblock_active() -> bool:
    return (time.time() - _geoblocked_at) < 1800.0


async def region_status(force: bool = False) -> dict:
    return dict(_REGION)


def region_cached() -> Optional[dict]:
    return {**_REGION, "ageSec": 0.0}


def region_order_refusal(action: str) -> Optional[str]:
    return None


def region_snapshot(refresh: bool = False) -> Optional[dict]:
    return region_cached()


def _is_region_refusal(status: int, body: Any) -> bool:
    if status not in (403, 451):
        return False
    s = str(body).lower()
    return any(w in s for w in ("region", "jurisdiction", "restricted", "not eligible", "geoblock"))


# ───────── exchange restart window ──────────

_restart_until = 0.0
_RESTART_DEFAULT_SEC = 30.0


def _note_restart(retry_after: Optional[float]) -> float:
    global _restart_until
    wait = retry_after if retry_after and retry_after > 0 else _RESTART_DEFAULT_SEC
    _restart_until = max(_restart_until, time.time() + wait)
    return wait


def trading_restriction() -> Optional[dict]:
    """{kind, untilEpoch, message} while orders are paused, else None."""
    now = time.time()
    if now < _restart_until:
        return {"kind": "restarting", "untilEpoch": _restart_until,
                "message": f"The exchange is unavailable; orders are paused for ~{int(_restart_until - now)}s."}
    return None


def _to_float(v, default: float = 0.0) -> float:
    return us.to_float(v, default)


def _json_list(v) -> list:
    return us._json_list(v)


async def _get(
    url: str, params: dict | None = None,
    *, timeout: float | None = None, max_retries: int | None = None,
) -> Any:
    """GET with retry. `timeout`/`max_retries` override the module defaults for
    latency-sensitive callers (e.g. the crypto snapshot, which has its own hard
    per-asset budget and must fail FAST rather than burn it on retries — the
    difference between one card briefly showing 'no market' and the whole board
    hard-timing-out to red)."""
    retries = max_retries if max_retries is not None else MAX_RETRIES
    for attempt in range(1, retries + 1):
        try:
            client = await _get_client()
            # NOTE: httpx treats timeout=None as "disable timeout", so only pass
            # it through when an override was actually given; otherwise let the
            # client default (REQUEST_TIMEOUT) apply.
            if timeout is not None:
                resp = await client.get(url, params=params, timeout=timeout)
            else:
                resp = await client.get(url, params=params)
            _note_net_ok()  # a response came back → transport/pool is healthy
            _note_api(url, resp.status_code)
            if resp.status_code in (403, 451) and _api_host(url) \
                    and _is_region_refusal(resp.status_code, resp.text):
                global _geoblocked_at
                _geoblocked_at = time.time()
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", 2))
                await asyncio.sleep(min(wait, 10))
                continue
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            try:
                return resp.json() if resp.status_code < 400 else None
            except Exception:
                return None
        except httpx.PoolTimeout as e:
            # Local pool saturation (too many concurrent requests for
            # max_connections), NOT a network/transport failure. It must NOT
            # count toward the poisoned-pool streak — doing so let one crypto
            # snapshot burst self-amplify into a needless client rebuild that
            # dropped warm connections and stalled the next burst. Retrying frees
            # up as in-flight requests drain the pool.
            if attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            _log_get_failed(url, e)
            return None
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            _note_api(url)
            if attempt < retries:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            await _note_net_fail()  # terminal: count toward poisoned-pool recycle
            _log_get_failed(url, e)
            return None
    return None


async def _get_with_age(url: str, params: dict | None = None, *,
                        timeout: float | None = None) -> tuple[Any, Optional[float]]:
    """(json, seconds the CDN has held this reply), for reads where staleness
    changes the answer. The public BBO and book are cached for up to 30s
    (`Cache-Control: public, max-age=30`, measured 2026-11-05)."""
    try:
        client = await _get_client()
        kw = {"timeout": timeout} if timeout is not None else {}
        resp = await client.get(url, params=params, **kw)
        _note_net_ok()
        _note_api(url, resp.status_code)
        if resp.status_code >= 400:
            return None, None
        return resp.json(), _to_float(resp.headers.get("age"), 0.0)
    except (httpx.HTTPError, ValueError) as e:
        if isinstance(e, (httpx.NetworkError, httpx.TimeoutException)) \
                and not isinstance(e, httpx.PoolTimeout):
            _note_api(url)
        _log_get_failed(url, e)
        return None, None


# ───────── market normalisation + meta cache ────────────

# slug -> {yes_token, no_token, tick_size, min_size, slug, event_slug, question}.
_meta: dict[str, dict] = {}
_META_MAX = 8000  # cap so the 24/7 process can't grow this cache unbounded


def _fee_schedule_from_gamma(raw: dict) -> dict | None:
    """Per-market fee descriptor. (Name kept for its callers.)"""
    return us.fee_schedule(raw)


def _normalize_market(raw: dict, *, event_slug: str = "") -> dict | None:
    """Map a venue market into the dict the engines read; cache its trading meta."""
    parsed = us.normalize_market(raw, event_slug=event_slug)
    if parsed is None:
        return None
    market, meta = parsed
    _meta[market["ticker"]] = meta
    if len(_meta) > _META_MAX:
        # FIFO-evict: a re-derivable cache, get_market_meta re-fetches on a miss.
        for k in list(_meta.keys())[: len(_meta) - _META_MAX]:
            _meta.pop(k, None)
    return market


async def get_market_meta(ticker: str) -> dict | None:
    m = _meta.get(ticker)
    if m and m.get("yes_token"):
        return m
    await fetch_market(ticker)
    return _meta.get(ticker)


async def fetch_markets(status: str = "open", limit: int = 500, cursor: str = "",
                        series_ticker: str = "") -> tuple[list, str]:
    """Active markets, normalised. `cursor` is a numeric offset string."""
    page = max(1, min(int(limit), MARKET_PAGE_MAX))
    try:
        offset = int(cursor) if cursor else 0
    except (TypeError, ValueError):
        offset = 0
    data = await _get(f"{GATEWAY}/v1/markets", params={
        "limit": page, "offset": offset, "active": "true", "closed": "false",
    })
    rows = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], ""
    out = [m for m in (_normalize_market(r) for r in rows if not r.get("hidden")) if m]
    # active=true still lets the odd resolved row through.
    if status == "open":
        out = [m for m in out if m.get("status") == "open"]
    return out, (str(offset + len(rows)) if len(rows) >= page else "")


async def fetch_all_open_markets(max_pages: int = 10) -> list:
    out: list = []
    cursor = ""
    for _ in range(max(1, int(max_pages))):
        markets, cursor = await fetch_markets(limit=MARKET_PAGE_MAX, cursor=cursor)
        out.extend(markets)
        if not cursor:
            break
    return out


async def fetch_market(ticker: str) -> dict | None:
    """One market by slug, open or resolved."""
    if not us.valid_slug(ticker):
        return None
    data = await _get(f"{GATEWAY}/v1/market/slug/{ticker}")
    raw = data.get("market") if isinstance(data, dict) else None
    return _normalize_market(raw) if raw else None


async def fetch_markets_map(tickers, concurrency: int = 8) -> dict[str, dict]:
    uniq = [t for t in dict.fromkeys(tickers) if t]
    if not uniq:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(tk: str):
        async with sem:
            try:
                return tk, await fetch_market(tk)
            except Exception:
                return tk, None

    pairs = await asyncio.gather(*[_one(t) for t in uniq])
    return {tk: m for tk, m in pairs if m}


# The engines' interval names -> the venue's horizon in event slugs, verified live
# 2026-11-05. There is no 4h series. Assets: btc, eth, sol, xrp (and gold, silver,
# which settle on a single Chainlink spot print rather than a 60s average).
_UPDOWN_HORIZON = {"5m": "5m", "15m": "15m", "hourly": "1h"}
_CRYPTO_INTERVAL_SEC = {"5m": 300, "15m": 900, "hourly": 3600}
_updown_cache: dict = {}          # (asset, interval) -> {at, base, markets}
_UPDOWN_CACHE_TTL = 20.0          # refetch within a window at most every 20s


_FRESH_MARKET_TTL = 2.0
_fresh_market_cache: dict[str, tuple[float, dict]] = {}
# What the uncached read may replace on a cached board row. Timing and identity stay.
_FRESH_FIELDS = ("status", "yes_bid_dollars", "yes_ask_dollars", "last_price_dollars",
                 "no_price_dollars", "result", "settlement_value_dollars", "price_to_beat",
                 "settlement_price")


async def _with_fresh_current(markets: list[dict], *, fast: bool = False) -> list[dict]:
    """The board with its running window's quote and strike re-read uncached.

    The board comes from /v1/events/slug, which the CDN holds for 30s on top of our
    own 20s, so without the live feed the engine would price a 5-minute window off a
    quote up to 50s old. Skipped while the feed is fresh for that market."""
    import clob_ws  # local: clob_ws imports this module's constants

    now = time.time()
    out = list(markets)
    for i, m in enumerate(out):
        if not (m.get("window_start_epoch") or 0) <= now < (m.get("window_close_epoch") or 0):
            continue
        if clob_ws.quote_cents_if_fresh(m.get("yes_token")):
            continue
        slug = m.get("ticker") or ""
        at, fresh = _fresh_market_cache.get(slug, (0.0, None))
        if fresh is None or time.monotonic() - at >= _FRESH_MARKET_TTL:
            data = await _get(f"{GATEWAY}/v1/market/slug/{slug}",
                              timeout=2.0 if fast else None, max_retries=1 if fast else None)
            raw = data.get("market") if isinstance(data, dict) else None
            fresh = _normalize_market(raw, event_slug=m.get("event_ticker") or "") if raw else None
            if fresh:
                # Normalising fills a missing side with the other one. A real touch
                # needs both sides from the venue.
                fresh = {**fresh, "two_sided": None not in us.parse_market_quote(raw)}
                if len(_fresh_market_cache) > 64:
                    _fresh_market_cache.clear()
                _fresh_market_cache[slug] = (time.monotonic(), fresh)
        if fresh:
            out[i] = {**m, **{k: fresh.get(k) for k in _FRESH_FIELDS},
                      "quote_fresh": bool(fresh.get("two_sided"))}
    return out


async def fetch_crypto_updown(asset: str, interval: str = "15m", *, fast: bool = False) -> list[dict]:
    """The current and adjacent "Up or Down" windows for an asset. Timing comes
    from the market's own assetPriceTerms, falling back to the slug's window."""
    horizon = _UPDOWN_HORIZON.get(interval)
    if not horizon:
        return []
    sec = _CRYPTO_INTERVAL_SEC[interval]
    asset_l = asset.lower()
    base = (int(time.time()) // sec) * sec
    ck = (asset_l, interval)
    cached = _updown_cache.get(ck)
    if cached and cached["base"] == base and (time.monotonic() - cached["at"]) < _UPDOWN_CACHE_TTL:
        return await _with_fresh_current(cached["markets"], fast=fast)
    windows = (base, base + sec, base - sec, base + 2 * sec)
    slugs = [us.updown_event_slug(asset_l, horizon, ws) for ws in windows]
    datas = await asyncio.gather(*[
        _get(f"{GATEWAY}/v1/events/slug/{s}", timeout=4.0 if fast else None,
             max_retries=1 if fast else None)
        for s in slugs
    ])
    out: list[dict] = []
    for ws, slug, data in zip(windows, slugs, datas):
        event = data.get("event") if isinstance(data, dict) else None
        rows = (event or {}).get("markets") or []
        raw = rows[0] if rows and isinstance(rows[0], dict) else None
        if raw is None:
            continue
        norm = _normalize_market(raw, event_slug=slug)
        if not norm:
            continue
        terms = raw.get("assetPriceTerms") if isinstance(raw.get("assetPriceTerms"), dict) else {}
        start = us.parse_iso_epoch(terms.get("windowStart")) or ws
        end = us.parse_iso_epoch(terms.get("windowEnd")) or (ws + sec)
        norm = dict(norm)
        norm["window_start_epoch"] = start
        norm["window_close_epoch"] = end
        norm["interval_sec"] = end - start
        norm["close_time"] = datetime.fromtimestamp(end, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(norm)
    if out:
        _updown_cache[ck] = {"at": time.monotonic(), "base": base, "markets": out}
    elif cached and cached["base"] == base:
        out = cached["markets"]         # a transient miss keeps the last good board
    return await _with_fresh_current(out, fast=fast)


async def fetch_events(limit: int = 100, offset: int = 0, **_ignored) -> list:
    data = await _get(f"{GATEWAY}/v1/events", params={
        "limit": limit, "offset": offset, "active": "true", "closed": "false"})
    rows = data.get("events") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


async def fetch_series(series_ticker: str) -> dict | None:
    data = await _get(f"{GATEWAY}/v1/series", params={"slug": series_ticker})
    rows = data.get("series") if isinstance(data, dict) else None
    return rows[0] if isinstance(rows, list) and rows else None


# ───────── public tape: none on this venue ──
# The exchange publishes no per-trader data and no public REST tape: the trade
# stream is anonymous and behind a signed WebSocket. Whale tracking and copy
# trading have nothing to read, so these stay as inert seams for their callers.


def normalize_trade(t: dict) -> Optional[dict]:
    return None


def outcome_index_of(t: dict) -> Optional[int]:
    return None


def normalize_activity_trade(payload) -> Optional[dict]:
    return None


async def fetch_recent_trades(limit: int = 1000, *, min_size: float = 0.0) -> list:
    return []


async def web_market_url(ticker: str = "", *, slug: str = "", **_ignored) -> str:
    s = slug or ticker
    return f"https://polymarket.us/market/{s}" if us.valid_slug(s) else "https://polymarket.us"


# ───────── quotes ───────────────────────────


async def _read_bbo(slug: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(yes_bid, yes_ask, age_sec), from the one public read the CDN doesn't cache.
    /bbo is held for up to 30s: on a live 15-minute window it sat at 76c while this
    moved 76c -> 93c in 15s (2026-11-05). The order paths read the live feed first
    (clob_ws) and only land here without it."""
    if not us.valid_slug(slug):
        return None, None, None
    data, age = await _get_with_age(f"{GATEWAY}/v1/market/slug/{slug}")
    if not isinstance(data, dict):
        return None, None, None
    bid, ask = us.parse_market_quote(data.get("market"))
    if bid is None or ask is None:
        # An empty side here doesn't mean an empty book: 20 of the 40 newest markets
        # (2026-11-05) had neither quote on this row and a two-sided book. The book
        # route is CDN-cached, so its age comes along and widens the collar.
        book_data, book_age = await _get_with_age(f"{GATEWAY}/v1/markets/{slug}/book")
        if isinstance(book_data, dict):
            book = us.parse_book(book_data.get("marketData"))
            if bid is None and book["bids"]:
                bid = book["bids"][0][0]
            if ask is None and book["asks"]:
                ask = book["asks"][0][0]
            age = max(age or 0.0, book_age or 0.0)
        elif bid is None and ask is None:
            # No quote on the row and no book: nothing was read. Answering with an
            # age here said "an empty book, read just now", and a buy then went out
            # with no price to collar it against. A new window is exactly where
            # this happens: its row has no quotes and its book isn't served yet.
            return None, None, None
    return bid, ask, age


async def get_quote(ticker: str, side: str) -> dict:
    """{'bid_cents', 'ask_cents', 'age_sec'} for one side of a market."""
    bid, ask, age = await _read_bbo(ticker)
    b, a = us.quote_for_side(side, bid, ask)
    return {
        "bid_cents": us.bid_cents(b),
        "ask_cents": us.ask_cents(a),
        "age_sec": age,
    }


async def updown_arb_edge(ticker: str) -> Optional[dict]:
    """Always None: YES and NO are one book here, so they can't mis-sum."""
    return None


async def _read_book(slug: str) -> Optional[dict]:
    if not us.valid_slug(slug):
        return None
    data = await _get(f"{GATEWAY}/v1/markets/{slug}/book")
    if not isinstance(data, dict):
        return None
    return us.parse_book(data.get("marketData"))


async def book_imbalance(token_id: str, levels: int = 3) -> Optional[float]:
    """(bid depth - ask depth) / total over the top `levels`, from `token_id`'s side."""
    slug, side = us.parse_token(token_id)
    book = await _read_book(slug)
    if not book:
        return None
    view = us.book_for_side(book, side)
    bids = sum(q for _, q in view["bids"][:levels])
    asks = sum(q for _, q in view["asks"][:levels])
    return (bids - asks) / (bids + asks) if (bids + asks) > 0 else None


async def get_orderbook(ticker: str) -> dict:
    """{'yes': [[cents, size], ...], 'no': [...]}: each side's bids."""
    out: dict = {"yes": [], "no": []}
    book = await _read_book(ticker)
    if not book:
        return out
    for side in ("yes", "no"):
        out[side] = [[int(round(p * 100)), q] for p, q in us.book_for_side(book, side)["bids"]]
    return out


# ───────── signed requests ──────────────────


def _safe_body(body: Any) -> str:
    return str(body)[:300]


async def _authed_request(method: str, path: str, *, json_body: dict | list | None = None,
                          params: dict | list | None = None, idempotent: bool = True) -> Any:
    """Signed request to the trading API. The signature covers the path without
    its query string.

    `idempotent=False` (order placement) never re-fires on a timeout or 5xx: the
    API has no idempotency key, so a retry after a lost reply would place a second
    order. A 429 is safe to retry; the request was refused before it reached the
    matching engine, but it is not retried here either, so the caller's own pacing
    stays in charge of a placement."""
    method = method.upper()
    url = f"{us.API_BASE}{path}"
    attempts = MAX_RETRIES if idempotent else 1
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            headers = auth.auth_headers(method, path)
        except RuntimeError as e:
            raise PolymarketAPIError(401, str(e)) from e
        headers["Content-Type"] = "application/json"
        headers["User-Agent"] = USER_AGENT
        client = await _get_client()
        try:
            resp = await client.request(method, url, json=json_body, params=params, headers=headers)
            _note_api(url, resp.status_code)
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            last = e
            if not isinstance(e, httpx.PoolTimeout):
                _note_api(url)
            if idempotent and attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF * attempt)
                continue
            await _note_net_fail()
            raise
        _note_net_ok()
        status = resp.status_code
        if status < 400:
            if not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {}
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text[:300]
        if status == 429 and idempotent and attempt < attempts:
            await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue
        if status >= 500 and idempotent and attempt < attempts:
            await asyncio.sleep(RETRY_BACKOFF * attempt)
            continue
        if status == 503:
            _note_restart(_to_float(resp.headers.get("retry-after"), 0.0))
        if _is_region_refusal(status, body):
            global _geoblocked_at
            _geoblocked_at = time.time()
        raise PolymarketAPIError(status, body)
    if last:
        raise last
    raise PolymarketAPIError(0, "request failed")


async def _sync_clock_offset() -> None:
    """The API accepts timestamps within 30s of its own clock. Correct a drifted
    PC from the gateway's Date header."""
    try:
        client = await _get_client()
        resp = await client.get(f"{GATEWAY}/v1/markets", params={"limit": 1}, timeout=4.0)
        stamp = resp.headers.get("date")
        if stamp:
            offset = parsedate_to_datetime(stamp).timestamp() - time.time()
            auth.set_time_offset(offset if abs(offset) > 2.0 else 0.0)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"clock sync skipped: {e}")


# Polymarket US's last definite answer on the saved key: it refused it.
_key_rejected = False


def key_rejected() -> bool:
    return _key_rejected


async def ensure_api_creds() -> None:
    """Prove the saved key with one signed read. A saved file proves nothing: a key
    Polymarket US refuses must never read as connected. Raises on any failure."""
    global _key_rejected
    if not auth.credentials_present():
        raise PolymarketAPIError(401, "No Polymarket US API key is saved.")
    await _sync_clock_offset()
    try:
        await _authed_request("GET", "/v1/account/balances")
    except PolymarketAPIError as e:
        if e.status in (401, 403):
            _key_rejected = True
        raise
    _key_rejected = False


# ───────── account ──────────────────────────


async def get_balance() -> dict:
    """{'balance': spendable_cents, 'portfolio_value': cents or None}. A failed
    read RAISES, so the caller's cache serves the last-known value rather than a
    false $0 that would halt trading."""
    data = await _authed_request("GET", "/v1/account/balances")
    view = us.balance_view(data)
    if view is None:
        raise RuntimeError("balance response had no USD balance")
    # Valued from the positions, not this reply's assetNotional: that read 0 in all
    # 363 balance snapshots taken with positions open, so every fill looked like a
    # loss of its whole cost. An unreadable list is None, which the balance cache
    # answers with the last-known value.
    try:
        port = us.positions_value(await get_positions(strict=True))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"positions unreadable for the portfolio value: {e}")
        port = None
    return {"balance": int(round(view["cash"] * 100)),
            "portfolio_value": None if port is None else int(round(port * 100)),
            "detail": us.balance_detail(data)}


async def get_positions(limit: int = 1000, *, settlement_status: str | None = None,
                        paginate: bool = True, user: str | None = None,
                        strict: bool = False) -> list[dict]:
    """Our positions in the shape the reconciler reads. Any read failure RAISES:
    an outage must never look like a flat account, because reconcilers act on
    "not held". `user` is refused rather than ignored: this venue can't read
    anyone else's positions."""
    if user:
        raise RuntimeError("Polymarket US has no public per-trader data")
    if not auth.credentials_present():
        if strict:
            raise RuntimeError("No Polymarket US API key is saved.")
        return []
    out: list[dict] = []
    cursor = ""
    page = max(1, min(int(limit), 200))
    for _ in range(40 if paginate else 1):
        params: dict = {"limit": page}
        if cursor:
            params["cursor"] = cursor
        data = await _authed_request("GET", "/v1/portfolio/positions", params=params)
        if not isinstance(data, dict) or not isinstance(data.get("positions", {}), dict):
            raise RuntimeError("positions fetch returned an unexpected shape")
        for slug, p in (data.get("positions") or {}).items():
            row = us.position_row(slug, p)
            if row:
                out.append(row)
        cursor = str(data.get("nextCursor") or "")
        if data.get("eof") or not cursor:
            return out
    if paginate:
        raise RuntimeError("positions fetch: no end after 40 pages")
    return out


async def get_settled_positions(limit: int = 1000) -> list[dict]:
    """Always empty: settlement pays out automatically, nothing awaits redeeming."""
    return []


async def get_activity(limit: int = 500, *, user: str | None = None,
                       market: str = "") -> list[dict]:
    """Recent trades and resolutions, newest first, in the reconcilers' shape."""
    if user or not auth.credentials_present():
        return []
    out: list[dict] = []
    cursor = ""
    want = max(1, int(limit))
    for _ in range(10):
        params: dict = {"limit": min(want, 200), "sortOrder": "SORT_ORDER_DESCENDING"}
        if market:
            params["marketSlug"] = market
        if cursor:
            params["cursor"] = cursor
        try:
            data = await _authed_request("GET", "/v1/portfolio/activities", params=params)
        except PolymarketAPIError:
            # Nothing read at all is a failure, not "no activity". A later page
            # failing keeps what's in hand: a missing row only delays a booking.
            if not out:
                raise
            return out
        rows = data.get("activities") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return out
        out.extend(r for r in (us.activity_row(a) for a in rows) if r)
        cursor = str(data.get("nextCursor") or "")
        if data.get("eof") or not cursor or len(out) >= want:
            return out[:want]
    return out[:want]


async def check_trading_ready() -> dict:
    """Preflight for the UI: is the key accepted, is there money, is market data
    reachable? Never raises. There is no live test of order placement here:
    the order preview endpoint is what the live check script uses. `signerOk` is
    about the balance read: True when Polymarket US accepted that signed request,
    False when it rejected the key, None when no signed request got an answer."""
    global _key_rejected
    signer_ok: Optional[bool] = None
    issues: list[str] = []
    fatal: list[str] = []

    def _fatal(msg: str) -> None:
        issues.append(msg)
        fatal.append(msg)

    address = auth.get_key_id()
    balance_usd = 0.0
    if not address:
        _fatal("No Polymarket US API key is saved.")
    else:
        await _sync_clock_offset()
        try:
            view = us.balance_view(await _authed_request("GET", "/v1/account/balances"))
            signer_ok = True
            _key_rejected = False
            if view is None:
                _fatal("Polymarket US answered, but with no USD balance on this account.")
            else:
                balance_usd = view["cash"]
                if balance_usd <= 0:
                    _fatal("This account has no buying power. Deposit in the Polymarket US app.")
        except PolymarketAPIError as e:
            if e.status in (401, 403):
                signer_ok = False
                _key_rejected = True
                _fatal("Polymarket US rejected this API key. Check the Key ID and Secret, "
                       "and that identity verification is finished in the app.")
            else:
                issues.append(f"Could not read the account balance: {e}")
        except Exception as e:  # noqa: BLE001
            issues.append(f"Could not reach the trading API: {type(e).__name__}")
    probe = await _get(f"{GATEWAY}/v1/markets", params={"limit": 1}, timeout=4.0, max_retries=1)
    if not probe:
        issues.append("Polymarket US market data is unreachable from your network.")
    return {
        "ok": not issues, "address": address, "balanceUsd": balance_usd,
        "approvalsOk": True, "signerOk": signer_ok, "issues": issues,
        "fatalIssues": fatal, "region": dict(_REGION),
    }


# ───────── orders / fills ───────────────────


async def get_order(order_id: str) -> dict:
    """One order by id -> {'order': view}. A placeholder id (see
    OrderStatusUnknownError) 404s without a request, sending the caller to its
    "trust the positions read" path."""
    if str(order_id or "").startswith(UNKNOWN_ID_PREFIX):
        raise PolymarketAPIError(404, "the exchange never gave this order an id")
    data = await _authed_request("GET", f"/v1/order/{order_id}")
    order = data.get("order") if isinstance(data, dict) else None
    if not isinstance(order, dict):
        raise PolymarketAPIError(404, f"no order {order_id}")
    return {"order": us.order_view(order)}


async def get_fills_for_order(order_id: str, limit: int = 200, *, market: str = "",
                              asset_id: str = "", after: Optional[int] = None) -> list[dict]:
    """Our fills on one order, as one aggregate leg at the average price. Trade
    activity carries no order id here, so the order's own cumQuantity and avgPx
    are the truth. [] means "unknown", which callers treat as no information."""
    try:
        view = (await get_order(order_id))["order"]
    except PolymarketAPIError:
        return []
    qty = float(view.get("size_matched") or 0)
    if qty <= 0:
        return []
    price = float(view.get("price") or 0)
    return [{
        "order_id": str(order_id),
        "action": view.get("action") or "buy",
        "count_fp": qty,
        "price_cents": int(round(price * 100)),
        "yes_price_dollars": price,
        "no_price_dollars": 1.0 - price,
        "side": "",
        "role": "",
        "asset_id": us.token(view.get("market") or "", view.get("side") or "yes"),
        "trade_id": str(order_id),
    }]


async def get_fills_since(after_ts_unix: int, limit: int = 200) -> list[dict]:
    return []


async def get_open_orders(slugs: Optional[list[str]] = None) -> list[dict]:
    """Our resting orders, raw. `slugs` narrows it; repeated query params."""
    params = [("slugs", s) for s in (slugs or [])] or None
    data = await _authed_request("GET", "/v1/orders/open", params=params)
    rows = data.get("orders") if isinstance(data, dict) else None
    if rows is None:
        raise PolymarketAPIError(0, "open orders reply had no orders list")
    return rows if isinstance(rows, list) else []


def _snap_price_to_tick(price: float, tick: float) -> float:
    """Nearest tick, inside the tradable range, with no float noise."""
    tick = tick if tick and tick > 0 else 0.01
    decimals = max(0, int(round(-math.log10(tick)))) if tick < 1 else 0
    snapped = round(round(price / tick) * tick, decimals)
    return max(tick, min(1.0 - tick, snapped))


_VALID_ORDER_TYPES = ("GTC", "FOK", "FAK")


def _post_outcome_ambiguous(e: BaseException) -> bool:
    """Might the exchange have taken this order even though we got an error?
    A timeout or connection loss, or a 5xx. Never a 4xx: that is a refusal."""
    if isinstance(e, httpx.TransportError):
        return True
    if isinstance(e, PolymarketAPIError):
        return int(getattr(e, "status", 0) or 0) >= 500
    return False


def _is_latency_stopgap(body: Any) -> bool:
    """"Global Rate Limit Exceeded" on an order means it wasn't processed within
    5 seconds and was rejected: a clean refusal, not an account rate limit."""
    return "global rate limit exceeded" in str(body).lower()


async def _settle_ambiguous_post(body: dict, *, slug: str, order_type: str, err: Exception) -> dict:
    """After an ambiguous send, look for the order among our open orders by its
    exact terms. Found -> tracked as placed. Not found -> OrderStatusUnknownError,
    never a clean "failed": a resting order the app forgot about is exactly what
    this prevents. A placement that already filled won't be listed as open, so
    "not found" stays unknown, and positions settle it."""
    want_px = us.amount(body.get("price"))
    want_qty = float(body.get("quantity") or 0)
    for attempt in range(_AMBIGUOUS_LOOKUPS):
        try:
            rows = await asyncio.wait_for(get_open_orders([slug]), timeout=_AMBIGUOUS_LOOKUP_TIMEOUT_SEC)
            for o in rows:
                if (o.get("intent") == body.get("intent")
                        and us.amount(o.get("price")) == want_px
                        and float(o.get("quantity") or 0) == want_qty):
                    view = us.order_view(o)
                    logger.warning(f"[order] ambiguous send ({err}) but the exchange has "
                                   f"order {view['id']}: status={view['status']}")
                    return {"order": {"order_id": view["id"], "status": view["status"]},
                            "raw": {"recovered": True}}
        except Exception as le:  # noqa: BLE001 — a failed lookup is "not found yet"
            logger.debug(f"[order] lookup after ambiguous send: {le}")
        if attempt + 1 < _AMBIGUOUS_LOOKUPS:
            await asyncio.sleep(_AMBIGUOUS_LOOKUP_GAP_SEC)
    oid = f"{UNKNOWN_ID_PREFIX}{uuid.uuid4().hex}"
    logger.warning(f"[order] ambiguous send ({type(err).__name__}: {err}); status unknown ({oid})")
    raise OrderStatusUnknownError(oid, err) from err


# ───────── pre-trade risk controls ──────────
# Rulebook 5.2(i): "Participants utilizing automated trading systems must implement
# pre-trade risk controls including order throttles, price collars, and kill
# switches." These sit here because every engine's order passes through this file.

ORDER_THROTTLE_PER_SEC = 8          # paced: a burst waits for a slot
ORDER_THROTTLE_PER_MIN = 120        # refused: a runaway loop stops here
THROTTLE_MAX_WAIT_SEC = 3.0
COLLAR_LIVE_CENTS = 20.0            # past the touch, against a fresh live quote
COLLAR_CACHED_CENTS = 40.0          # against a REST quote up to 30s old
COLLAR_FRESH_SEC = 5.0              # a REST reply younger than this counts as live
_order_times: collections.deque = collections.deque()
_throttle_lock = asyncio.Lock()
_kill_reason = ""
# The exchange reads a NO order's price on the YES side. Its docs say so, the
# owner's live check saw it on the wire (2026-11-04), and real NO buys have filled
# at or under their limit since. That is a fact about the venue, not about a key or
# a machine, so NO buys are open on every install. (They used to need the live
# check per key, which an installed build can't run, so nobody else could buy NO.)
# This latch is what is left: the live check sets it if it ever sees the exchange
# read the price the other way, because a marketable NO buy could then pay far
# above the price it was given. A wrong reading on a SELL of NO only leaves it
# unfilled, so exits are never held back.
_no_side_mismatch = False


# Both safety latches survive a restart: a kill switch that a crash-respawn quietly
# cleared would not be one.
def _venue_state_file():
    return auth._credentials_dir() / "venue_state.json"


def _read_venue_state() -> dict:
    try:
        data = json.loads(_venue_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_venue_state(**changes) -> None:
    state = {**_read_venue_state(), **changes}
    f = _venue_state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, f)


def load_venue_state() -> None:
    """Restore the latches. Runs at startup and on a key change."""
    global _kill_reason, _no_side_mismatch
    state = _read_venue_state()
    _kill_reason = str(state.get("killSwitch") or "")
    _no_side_mismatch = state.get("noSideMismatch") is True


def engage_kill_switch(reason: str = "kill switch") -> None:
    """Refuse every new order until cleared. Resting orders are the caller's to
    cancel (cancel_all_venue_orders)."""
    global _kill_reason
    _kill_reason = reason or "kill switch"
    _write_venue_state(killSwitch=_kill_reason)
    logger.warning(f"[kill-switch] engaged: {_kill_reason}")


def clear_kill_switch() -> None:
    global _kill_reason
    _kill_reason = ""
    _write_venue_state(killSwitch="")


def kill_switch_status() -> Optional[str]:
    return _kill_reason or None


def set_no_side_mismatch(value: bool) -> None:
    """Only the live check calls this, with what it observed on the wire: True
    locks NO buys, False clears the lock."""
    global _no_side_mismatch
    _no_side_mismatch = bool(value)
    _write_venue_state(noSideMismatch=_no_side_mismatch)


def no_buy_locked() -> bool:
    return _no_side_mismatch


async def _throttle() -> None:
    now = time.monotonic()
    async with _throttle_lock:
        while _order_times and now - _order_times[0] > 60.0:
            _order_times.popleft()
        if len(_order_times) >= ORDER_THROTTLE_PER_MIN:
            raise LocalOrderRefusal(429, f"Order throttle: more than {ORDER_THROTTLE_PER_MIN} "
                                         f"orders in a minute. Nothing was sent.")
        in_last_sec = sum(1 for t in _order_times if now - t <= 1.0)
        if in_last_sec >= ORDER_THROTTLE_PER_SEC:
            wait = 1.0 - (now - _order_times[-ORDER_THROTTLE_PER_SEC])
            if wait > THROTTLE_MAX_WAIT_SEC:
                raise LocalOrderRefusal(429, "Order throttle: too many orders queued. Nothing was sent.")
            await asyncio.sleep(max(0.0, wait))
        _order_times.append(time.monotonic())


async def _reference_quote(slug: str, side: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """(bid_cents, ask_cents, collar_cents): the live feed if fresh, else REST,
    with a collar wide enough for how old that REST reply can be. All None means
    the read failed, which is not the same as a book with nothing resting."""
    try:
        import clob_ws  # local: clob_ws imports this module's constants
        live = clob_ws.quote_cents_if_fresh(us.token(slug, side))
    except Exception:  # noqa: BLE001
        live = None
    if live:
        return float(live[0]), float(live[1]), COLLAR_LIVE_CENTS
    q = await get_quote(slug, side)
    bid, ask = q.get("bid_cents"), q.get("ask_cents")
    age = q.get("age_sec")
    if bid is None and ask is None and age is None:
        return None, None, None
    collar = COLLAR_LIVE_CENTS if age is not None and age <= COLLAR_FRESH_SEC else COLLAR_CACHED_CENTS
    return (float(bid) if bid is not None else None,
            float(ask) if ask is not None else None, collar)


def collar_refusal(action: str, price_cents: float, bid: Optional[float], ask: Optional[float],
                   collar: float) -> Optional[str]:
    """Why this price is too far through the touch, or None. Only the aggressive
    direction is collared: a resting order away from the market can't trade badly."""
    if action == "buy" and ask is not None and price_cents > ask + collar:
        return (f"Price collar: buying at {price_cents:g}c is more than {collar:g}c above "
                f"the {ask:g}c ask. Nothing was sent.")
    if action == "sell" and bid is not None and price_cents < bid - collar:
        return (f"Price collar: selling at {price_cents:g}c is more than {collar:g}c below "
                f"the {bid:g}c bid. Nothing was sent.")
    return None


async def place_limit_order(*, ticker: str, side: str, action: str, count: int, price_cents: float,
                            client_order_id: Optional[str] = None, order_type: str = "GTC",
                            post_only: bool = False) -> dict:
    """Send one limit order for a side of a market.

    `price_cents` is the price of `side`: buying NO at 30 means paying 30c for NO.
    It is mirrored to the YES side on the wire (us_api.wire_price).

    `order_type`: "GTC" rests; "FAK"/"FOK" cross and kill the remainder, sent as a
    limit + IOC/FOK, so `price_cents` is a hard cap either way.

    `post_only` (GTC only) sends the order as participate-don't-initiate: the
    exchange rests it or rejects it, and never lets it take. A maker fill pays no
    fee, so this is how a resting order is guaranteed not to pay one.

    `client_order_id` is local correlation only, stored on our row and never sent:
    the API has no client id or idempotency key. So a lost reply can't be retried
    blindly, and an ambiguous send ends in `_settle_ambiguous_post`."""
    side = side.lower()
    action = (action or "buy").lower()
    order_type = (order_type or "GTC").upper()
    if order_type not in _VALID_ORDER_TYPES:
        raise ValueError(f"order_type must be one of {_VALID_ORDER_TYPES}, got {order_type}")
    if side not in ("yes", "no"):
        raise ValueError(f"side must be yes|no, got {side}")
    if action not in ("buy", "sell"):
        raise ValueError(f"action must be buy|sell, got {action}")
    if post_only and order_type != "GTC":
        raise ValueError(f"post_only needs a GTC order, got {order_type}")
    if not (0 < price_cents < 100):
        raise ValueError(f"price_cents must be inside 0..100, got {price_cents}")
    if count <= 0:
        raise ValueError(f"count must be positive, got {count}")
    if not us.valid_slug(ticker):
        raise PolymarketAPIError(404, f"not a market slug: {ticker!r}")

    if _kill_reason:
        raise LocalOrderRefusal(423, f"Kill switch is on ({_kill_reason}). Nothing was sent.")
    if side == "no" and action == "buy" and _no_side_mismatch:
        raise LocalOrderRefusal(
            412, "Buying NO is locked: the live check saw the exchange price a NO order "
                 "differently from how this app sends it. Update the app. Nothing was sent.")
    restriction = trading_restriction()
    if restriction is not None:
        raise EngineRestartError(425, restriction["message"])

    meta = await get_market_meta(ticker)
    if not meta:
        raise PolymarketAPIError(404, f"no market {ticker}")
    tick = float(meta.get("tick_size") or 0.01)
    min_size = float(meta.get("min_size") or 0.0)
    price = _snap_price_to_tick(price_cents / 100.0, tick)
    # Respect the user's size: never silently buy more than requested. A sell of
    # less than the minimum is still tried, since refusing would strand a holding.
    if action == "buy" and min_size > 0 and count < min_size:
        raise PolymarketAPIError(
            400, f"requested size {count} is below this market's minimum of {min_size:g} "
                 f"contracts. Raise your order size to trade here.")

    bid, ask, collar = await _reference_quote(ticker, side)
    if collar is None and action == "buy":
        # Sells still go: a read failure must not trap someone in a position.
        raise LocalOrderRefusal(
            503, "Price collar: couldn't read a current price for this market. Nothing was sent.")
    why = collar_refusal(action, price * 100.0, bid, ask, collar or 0.0)
    if why and action == "sell":
        # The feed can be 12 seconds old and these markets move 17c in that time.
        # Before keeping someone in a position, ask the read the CDN doesn't cache.
        q = await get_quote(ticker, side)
        fresh_bid = q.get("bid_cents")
        if fresh_bid is not None:
            age = q.get("age_sec")
            wide = age is None or age > COLLAR_FRESH_SEC
            why = collar_refusal(action, price * 100.0, float(fresh_bid), None,
                                 COLLAR_CACHED_CENTS if wide else COLLAR_LIVE_CENTS)
    if why:
        raise LocalOrderRefusal(422, why)
    if post_only:
        # Refused here with the price that would have been hit. The exchange
        # refuses it too, for a touch that moved after this read.
        touch = ask if action == "buy" else bid
        crosses = touch is not None and (
            price * 100.0 >= touch - 1e-9 if action == "buy" else price * 100.0 <= touch + 1e-9)
        if crosses:
            raise LocalOrderRefusal(
                422, f"Post-only: {price * 100.0:g}¢ would trade right away against the best "
                     f"{'ask' if action == 'buy' else 'bid'} of {touch:g}¢, and a post-only "
                     f"order may only rest. Nothing was sent.")

    body = us.order_body(slug=ticker, side=side, action=action, count=count, price=price,
                         order_type=order_type, maker_only=post_only)
    direction = us.yes_book_direction(body["intent"])
    try:
        mine = await get_open_orders([ticker])
    except Exception as e:  # noqa: BLE001 — a failed read must not block a stop-loss
        logger.warning(f"[self-cross] open orders unreadable, sending anyway: {e}")
        mine = []
    hit = us.crosses_own_orders(direction, float(body["price"]["value"]), mine)
    if hit:
        raise LocalOrderRefusal(
            409, f"This order would trade against your own resting order {hit.get('id')} on this "
                 f"market, which exchange rules prohibit. Cancel that order first. Nothing was sent.")

    await _throttle()
    try:
        resp = await _authed_request("POST", "/v1/orders", json_body=body, idempotent=False)
    except PolymarketAPIError as e:
        if _is_latency_stopgap(e.body):
            raise PolymarketAPIError(429, "The exchange was too busy to process this order in "
                                          "time and rejected it. Nothing was placed.") from e
        if order_type in ("FOK", "FAK") and us.is_no_liquidity(e.body):
            return {"order": {"order_id": "", "status": "unmatched"}, "raw": {"errorMsg": str(e)}}
        if e.status in (400, 422):
            logger.warning(f"[order-payload] rejected: {ticker} {side}/{action} type={order_type} "
                           f"price={price} size={count} tick={tick}: {_safe_body(e.body)}")
        if _post_outcome_ambiguous(e):
            return await _settle_ambiguous_post(body, slug=ticker, order_type=order_type, err=e)
        raise
    except httpx.TransportError as e:
        return await _settle_ambiguous_post(body, slug=ticker, order_type=order_type, err=e)
    parsed = us.parse_place_response(resp, order_type)
    if parsed["reject"]:
        if order_type in ("FOK", "FAK") and us.is_no_liquidity(parsed["reject"]):
            return {"order": {"order_id": "", "status": "unmatched"}, "raw": resp}
        if post_only:
            raise PolymarketAPIError(
                400, f"The exchange did not place this post-only order ({parsed['reject']}). "
                     f"It would have traded right away. Nothing was placed.")
        raise PolymarketAPIError(400, parsed["reject"])
    return {"order": {"order_id": parsed["order_id"], "status": parsed["status"]}, "raw": resp}


async def preview_order(*, ticker: str, side: str, action: str, count: int, price_cents: float,
                        order_type: str = "GTC") -> dict:
    """The exchange's reading of an order, without placing it. For the live check."""
    body = us.order_body(slug=ticker, side=side, action=action, count=count,
                         price=price_cents / 100.0, order_type=order_type)
    return await _authed_request("POST", "/v1/order/preview", json_body={"request": body})


async def cancel_order(order_id: str, *, market: str = "") -> dict:
    """Cancel one order. The API needs its market, which is looked up when the
    caller doesn't have it. Idempotent to retry: a repeat just 404s."""
    slug = market
    if not slug:
        slug = (await get_order(order_id))["order"].get("market") or ""
    if not us.valid_slug(slug):
        raise PolymarketAPIError(404, f"no market for order {order_id}")
    await _authed_request("POST", f"/v1/order/{order_id}/cancel", json_body={"marketSlug": slug})
    return {"canceled": [order_id]}


async def cancel_orders(order_ids: list[str]) -> dict:
    """Cancel several; {'canceled': [...], 'not_canceled': {id: why}}."""
    ids = [str(i) for i in dict.fromkeys(order_ids or []) if i]
    canceled: list[str] = []
    not_canceled: dict[str, str] = {}
    sem = asyncio.Semaphore(8)

    async def _one(oid: str) -> None:
        async with sem:
            try:
                await cancel_order(oid)
                canceled.append(oid)
            except Exception as e:  # noqa: BLE001 — reported per order
                not_canceled[oid] = str(e)

    await asyncio.gather(*[_one(i) for i in ids])
    return {"canceled": canceled, "not_canceled": not_canceled}


async def cancel_all_venue_orders(slugs: Optional[list[str]] = None) -> list[str]:
    """Every open order, or those on `slugs`. An empty list means all."""
    data = await _authed_request("POST", "/v1/orders/open/cancel",
                                 json_body={"slugs": list(slugs or [])})
    ids = data.get("canceledOrderIds") if isinstance(data, dict) else None
    return [str(i) for i in ids] if isinstance(ids, list) else []
