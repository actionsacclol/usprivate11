"""Terminal provider layer — read side.

The bot answers "should I act, right now, on this one thing I am watching?".
The terminal answers "what is going on, and what should I do about it?" — for
ANY market the user names, not just the ones an engine happened to watch.

Three rules govern everything below.

1. THE ONE RULE. A value nobody could produce is ``None``. It is never
   coerced to 0, to a default, or to a plausible-looking guess. Where a value
   is missing the row also carries WHY, and what would fix it.

2. PROVENANCE PER FIELD. Every number carries which provider produced it and
   when WE received it. The merge priority is:
     1. our own authoritative read (the signed API, for our own orders)
     2. our own live feed (clob_ws, stamped on local arrival)
     3. the source that is best at that specific field
     4. None
   A provider returning ``None`` must NEVER erase a better provider's answer,
   which is why the merge goes through ``_Row.put`` and not a dict spread.

3. COST DISCIPLINE. A bot makes a handful of requests; a terminal polls four
   columns, a chart, a book and a portfolio forever. Every outbound call goes
   through a per-host gate that spaces request STARTS by a minimum gap and
   bounds how many may be in flight at once, a TTL cache sized to how fast
   that field actually changes, single-flight coalescing so two identical
   concurrent requests cost one, and a 429 park that fails FAST rather than
   holding the queue while a doomed call retries.

Trading lives in ``terminal_trade.py``. This module never spends money.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import polymarket_api as api
import us_api as us
from categorize import categorize_by_keywords

logger = logging.getLogger("krypt.terminal")

# ────────────────────────────────────────────────────────────────────────
# Hosts. The renderer names an ENTITY, a column and an interval — never a
# URL and never a host. This table is the ONLY place a hostname appears, so
# there is no input path that can point an outbound request somewhere else.
# That is the structural fix for SSRF; a URL validator is the fragile one.
# ────────────────────────────────────────────────────────────────────────

# ``min_gap`` is the minimum spacing between request STARTS, and
# ``concurrency`` is how many requests to that host may be in flight at once.
# They are different controls and both matter: the gap is the cost discipline
# (we never machine-gun a provider), the concurrency is the latency (a page
# that needs 20 metadata calls should not pay 20 round trips end to end).
# Anything not fetched through ``_fetch`` stays at 1 — see the notes below.
_HOSTS: dict[str, dict[str, Any]] = {
    # The same host twice on purpose: it meters its routes very differently, and
    # health() folds them back into one row. Measured 2026-11-05 from one IP:
    # /bbo and /book take about five requests, then answer 429 with Retry-After
    # ~10 for everything on those routes, while /v1/markets, /v1/market/slug and
    # /v1/price-history stayed 200 at 10/s. One shared gate would either crawl
    # everywhere or park the market list because a screen asked for a book.
    "gateway": {
        "base": api.GATEWAY,
        "host": "gateway.polymarket.us",
        "purpose": "Market data: lists, market pages, order books, search, price history",
        # The retail allowance is 25/s per IP, shared with the engines.
        "min_gap": 0.08,
        "concurrency": 6,
    },
    "gateway_book": {
        "base": api.GATEWAY,
        "host": "gateway.polymarket.us",
        "purpose": "Market data: order book depth and traded totals",
        "min_gap": 2.2,
        "concurrency": 1,
    },
    # Declared for the privacy panel, like the chat bots below: signed calls go
    # through polymarket_api, never _fetch, and only exist once a key is saved.
    "api": {
        "base": api.API_BASE,
        "host": "api.polymarket.us",
        "purpose": "Your orders, positions and balance (signed, only with an API key)",
        "min_gap": 0.0,
        "concurrency": 1,
    },
    # A SECOND VENUE, read-only. Listed here rather than given its own client
    # so it inherits everything: the per-host gate, the TTL cache, the 429
    # park, the privacy panel's call counts, and — most importantly — the
    # offline switch. A provider the kill switch does not reach would make
    # that switch a lie.
    #
    # Concurrency stays at 1. Kalshi is a courtesy read of somebody else's
    # public API for a single comparison row — one at a time is polite, and
    # cross-venue is never the call the user is waiting on.
    "kalshi": {
        "base": "https://api.elections.kalshi.com/trade-api/v2",
        "host": "api.elections.kalshi.com",
        "purpose": "Kalshi public market data (cross-venue comparison)",
        "min_gap": 0.20,
        "concurrency": 1,
    },
    # The chat bots do NOT fetch through _fetch (they need POSTs and a
    # websocket), but the privacy panel claims to name every host this machine
    # contacts — so they are declared here and their calls are counted through
    # note_external_call(). A panel that quietly omitted the two hosts your
    # positions are sent to would be worse than no panel.
    "discord": {
        "base": "https://discord.com/api/v10",
        "host": "discord.com",
        "purpose": "Discord bot — your commands and replies (only if enabled)",
        "min_gap": 0.0,
        "concurrency": 1,
    },
    "telegram": {
        "base": "https://api.telegram.org",
        "host": "api.telegram.org",
        "purpose": "Telegram bot — your commands and replies (only if enabled)",
        "min_gap": 0.0,
        "concurrency": 1,
    },
    # The AI analysis providers. Like the chat bots these do NOT fetch through
    # _fetch — the SDKs own their own transport, and both need POSTs with a
    # body. They are declared here anyway, and ai_analysis runs every call
    # inside this gate explicitly, so all four properties still hold: the
    # offline switch reaches them, a 429 parks them, the privacy panel counts
    # them, and they queue behind a minimum gap.
    #
    # Concurrency stays at 1, and deliberately so: ai_analysis holds
    # ``gate.lock()`` across its whole SDK call (it cannot use ``_fetch`` —
    # both need a POST with a body), so these two hosts remain strictly
    # serialised. Both SDKs are built with ``max_retries=0``, which makes this
    # park the single retry authority; letting several calls run at once would
    # hand a rate limit several chances to compound before the park lands.
    #
    # Both ship with no key and the feature off. What is sent is PUBLIC market
    # data only — the question, the rules text, the quote, volume and close
    # time. Never the wallet, never a position. See ai_analysis.build_payload,
    # which takes a market and has no access to an account.
    "anthropic": {
        "base": "https://api.anthropic.com/v1",
        "host": "api.anthropic.com",
        "purpose": "AI market analysis — public market text (only if enabled)",
        "min_gap": 0.5,
        "concurrency": 1,
    },
    "openai": {
        "base": "https://api.openai.com/v1",
        "host": "api.openai.com",
        "purpose": "AI market analysis — public market text (only if enabled)",
        "min_gap": 0.5,
        "concurrency": 1,
    },
}

# TTLs, tuned to how fast each thing ACTUALLY changes. A discovery list moves
# far slower than a top-of-book, and paying for a list every 3s when it turns
# over hourly is how a terminal gets itself rate-limited out of the data it
# actually needs.
TTL_DISCOVER = 25.0
# /v1/market/slug/{slug} is the one public read the CDN does not cache: on a live
# 15-minute window its quote moved 3c ahead of /book. Short, so that stays true.
TTL_MARKET = 3.0
# /bbo, /book and the lists are cached at the CDN for up to 30s, so asking more
# often than this mostly pays the book route's tight limit for the same bytes.
TTL_BOOK = 10.0
TTL_HISTORY = 45.0
TTL_EVENT = 30.0

_CDN_MAX_AGE = 30.0
# A book snapshot at least this old says so in its note.
BOOK_STALE_NOTE_SEC = 5.0

_PARK_SECS = 30.0            # a 429 with no usable Retry-After sits out this long
_PARK_MAX_SECS = 120.0       # the longest a Retry-After is believed for
_CACHE_MAX = 600             # bounded: this process runs for days

# ── the off switch (privacy panel, guide 8) ─────────────────────────────
# A privacy claim you cannot verify is marketing. The panel names every host
# verbatim and shows live call counts; this is the control that makes it
# actionable. Enforced HERE, at the single choke point every terminal request
# passes through, rather than per-caller — a screen that forgot to check would
# otherwise quietly keep talking.
_offline = False


def set_offline(v: bool) -> None:
    global _offline
    _offline = bool(v)


def is_offline() -> bool:
    return _offline


OFFLINE_REASON = (
    "Offline mode is on, so nothing was requested from any provider. Turn it "
    "off in Settings → Data & privacy to load live market data."
)


class _Gate:
    """Per-host pacing: a minimum gap between request STARTS, a bound on how
    many requests may be in flight at once, and a 429 park.

    The two controls are separate on purpose, and the split is what makes a
    terminal usable. The lock covers ONLY the spacing decision — work out the
    next allowed start, sleep the gap, stamp the start — and is released
    before the HTTP call. Holding it across the round trip as well made every
    request to a host strictly serial, so a Discover load needing ~20
    calls cost 20 × (RTT + min_gap): five to eight seconds of staring at
    skeletons for work that overlaps almost perfectly. The semaphore is what
    keeps "not serial" from becoming "unbounded"; ``min_gap`` still governs
    the starts, so the cost discipline is unchanged — we simply stop waiting
    for each answer before asking the next question.

    Parking is checked BEFORE either primitive is taken. A parked host
    therefore fails instantly instead of queueing behind a retry — the bug
    this exists to prevent is a Discover refresh 429ing the provider and
    starving the chart, which then renders blank while a plain curl to the
    same endpoint returns 200.
    """

    def __init__(self, key: str, min_gap: float, concurrency: int = 1) -> None:
        self.key = key
        self.min_gap = min_gap
        self.concurrency = max(1, int(concurrency))
        self._lock: Optional[asyncio.Lock] = None
        self._slots: Optional[asyncio.Semaphore] = None
        self.last = 0.0
        self.parked_until = 0.0
        self.calls = 0
        self.errors = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self._lat: list[float] = []

    def lock(self) -> asyncio.Lock:
        # Created lazily so importing this module never touches a loop.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def slot(self) -> asyncio.Semaphore:
        # Same lazy construction as the lock, for the same reason.
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.concurrency)
        return self._slots

    def parked(self) -> bool:
        return time.monotonic() < self.parked_until

    def park(self, secs: float = _PARK_SECS) -> None:
        # Never SHORTENS an existing park. With several requests in flight a
        # burst of 429s arrives together, and the second one must not be able
        # to pull the expiry back in — nor may a request that was already on
        # the wire when the park landed clear it on the way out.
        until = time.monotonic() + secs
        if until <= self.parked_until:
            return
        self.parked_until = until
        logger.info("terminal: parked %s for %.0fs after 429", self.key, secs)

    def note_latency(self, ms: float) -> None:
        self._lat.append(ms)
        if len(self._lat) > 50:
            del self._lat[: len(self._lat) - 50]

    def latency_ms(self) -> Optional[float]:
        if not self._lat:
            return None
        s = sorted(self._lat)
        return round(s[len(s) // 2], 1)


_gates: dict[str, _Gate] = {
    k: _Gate(k, float(v["min_gap"]), int(v.get("concurrency", 1)))
    for k, v in _HOSTS.items()
}

# key -> (expires_at_monotonic, value, observed_at_iso)
_cache: dict[str, tuple[float, Any, str]] = {}

# key -> the fetch already in flight for that key. Single-flight, and NOT
# optional: the old gate deduped identical concurrent requests only as a side
# effect of serialising them — the second one took the lock after the first
# had filled the cache and never reached the network. Now that the lock is
# released before the HTTP call, that dedup has to be explicit or it is lost.
# It is not hypothetical: Discover's "volume" and "volatility" columns send
# byte-identical params, as does a text search, so one page load would
# otherwise pay three times for one answer.
_inflight: dict[str, "asyncio.Future"] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cache_key(host: str, path: str, params: Optional[dict]) -> str:
    return f"{host}{path}?{json.dumps(params or {}, sort_keys=True, default=str)}"


def _cache_get(key: str) -> Optional[tuple[Any, str]]:
    hit = _cache.get(key)
    if not hit:
        return None
    expires, value, observed = hit
    if time.monotonic() > expires:
        _cache.pop(key, None)
        return None
    return value, observed


def _cache_put(key: str, value: Any, ttl: float, observed: Optional[str] = None) -> str:
    observed = observed or _now_iso()
    if len(_cache) >= _CACHE_MAX:
        # FIFO-evict the oldest third. Every cached value is re-derivable, so
        # dropping cold entries costs a round-trip, never correctness.
        for k in list(_cache.keys())[: _CACHE_MAX // 3]:
            _cache.pop(k, None)
    _cache[key] = (time.monotonic() + ttl, value, observed)
    return observed


async def _fetch(
    host: str, path: str, params: Optional[dict] = None, *,
    ttl: float = TTL_MARKET, timeout: float = 10.0,
) -> tuple[Any, Optional[str]]:
    """GET through the cache + per-host gate.

    Returns ``(data, observed_at_iso)``. ``data`` is ``None`` on every failure
    mode — parked host, network error, 4xx, 5xx — and the caller turns that
    into an honest ``None`` field with a reason, never into a zero.

    ``observed_at`` is when the CONTENT was produced as far as we can tell, not
    when it reached us: a CDN copy is backdated by its ``Age`` header, so every
    ``ageSec`` downstream is honest without each caller having to know.
    """
    spec = _HOSTS.get(host)
    gate = _gates.get(host)
    if not spec or not gate:
        return None, None

    # Checked BEFORE the cache: serving a cached answer while the user
    # believes the app is offline is its own small lie, and the whole point of
    # the switch is that it is verifiable.
    if _offline:
        return None, None

    key = _cache_key(host, path, params)
    hit = _cache_get(key)
    if hit is not None:
        gate.cache_hits += 1
        return hit[0], hit[1]
    gate.cache_misses += 1

    # Fail fast while parked — do NOT queue behind the gate. Checked before
    # the semaphore too, or a parked host with four requests already on the
    # wire would make the fifth one WAIT to be told no.
    if gate.parked():
        return None, None

    # Single-flight. There is no await between the lookup and the registration
    # below, so this is atomic for the loop: exactly one caller per key ever
    # becomes the leader.
    pending = _inflight.get(key)
    if pending is not None:
        gate.cache_hits += 1
        return await asyncio.shield(pending)

    # The request is a TASK, and every caller — the one that created it
    # included — waits on a SHIELD of it. So a caller that walks away (panel
    # closed, RPC timed out) cancels only its own wait: the request the other
    # callers are riding on survives, and none of them is handed a ``None``
    # for a value the provider was about to give us. `_inflight` holds the
    # strong reference that keeps the task from being collected mid-flight.
    task: asyncio.Future = asyncio.ensure_future(
        _fetch_now(gate, spec, host, path, params, key, ttl=ttl, timeout=timeout))
    _inflight[key] = task

    def _finished(done: "asyncio.Future") -> None:
        _inflight.pop(key, None)
        if not done.cancelled():
            # Retrieve it: this task can outlive its caller, and an unretrieved
            # exception on one is only ever noise in the log.
            exc = done.exception()
            if exc is not None:
                logger.debug("terminal fetch %s%s raised: %s", host, path, exc)

    task.add_done_callback(_finished)
    return await asyncio.shield(task)


async def _fetch_now(
    gate: _Gate, spec: dict, host: str, path: str, params: Optional[dict],
    key: str, *, ttl: float, timeout: float,
) -> tuple[Any, Optional[str]]:
    """One real request: pace it, make it, count it, cache it.

    The caller has already resolved the cache, the offline switch and the
    park. This is the only place that touches the network.
    """
    async with gate.slot():
        # A park can have landed while we waited for a slot.
        if gate.parked():
            return None, None

        # ── the spacing decision, and ONLY the spacing decision ─────────
        # Held for the length of a clock read plus at most min_gap; the HTTP
        # call happens after this block, so requests overlap on the wire
        # while their STARTS stay min_gap apart.
        async with gate.lock():
            if gate.parked():
                return None, None
            wait = gate.min_gap - (time.monotonic() - gate.last)
            if wait > 0:
                await asyncio.sleep(wait)
            if gate.parked():   # …or while we were sleeping out the gap
                return None, None
            started = time.monotonic()
            gate.last = started
            # Counted at the moment it starts, not when it finishes: the
            # privacy panel's promise is "requests this machine made", and a
            # call that is still in flight was still made.
            gate.calls += 1

        url = f"{spec['base']}{path}"
        try:
            client = await api._get_client()
            resp = await client.get(url, params=params, timeout=timeout)
        except Exception as e:  # noqa: BLE001 — every transport failure is a None field
            gate.errors += 1
            logger.debug("terminal fetch %s%s failed: %s", host, path, e)
            return None, None

        gate.note_latency((time.monotonic() - started) * 1000.0)

        if resp.status_code == 429:
            gate.errors += 1
            gate.park(_retry_after(resp))
            return None, None
        if resp.status_code >= 400:
            gate.errors += 1
            return None, None
        try:
            data = resp.json()
        except Exception:
            gate.errors += 1
            return None, None

    # A CDN copy expires at the CDN's clock, not ours: holding a 28-second-old
    # answer for another full TTL would serve it past twice its promised age.
    age = _cdn_age(resp)
    if age:
        ttl = min(ttl, max(1.0, _CDN_MAX_AGE - age))
    observed = _cache_put(key, data, ttl, _iso_seconds_ago(age))
    return data, observed


def _retry_after(resp: Any) -> float:
    """How long to park for a 429: what the host said, within reason."""
    try:
        secs = float((getattr(resp, "headers", None) or {}).get("retry-after"))
    except (TypeError, ValueError):
        return _PARK_SECS
    return max(1.0, min(secs, _PARK_MAX_SECS))


def _cdn_age(resp: Any) -> float:
    """Seconds the CDN says this response already sat in its cache."""
    try:
        return max(0.0, float((getattr(resp, "headers", None) or {}).get("age")))
    except (TypeError, ValueError):
        return 0.0


def _iso_seconds_ago(age: float) -> str:
    return datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def note_external_call(host_key: str, *, ok: bool = True,
                       ms: Optional[float] = None) -> None:
    """Count a request made OUTSIDE _fetch (the chat bots).

    Without this the privacy panel would list Discord and Telegram at zero
    calls forever while cheerfully relaying positions to both.
    """
    g = _gates.get(host_key)
    if not g:
        return
    g.calls += 1
    if not ok:
        g.errors += 1
    if ms is not None:
        g.note_latency(ms)


def health() -> dict:
    """Live per-host call/error/latency counts. Powers the privacy panel: the
    user can see exactly which hosts this machine talks to and how often."""
    rows: dict[str, dict] = {}
    for k, spec in _HOSTS.items():
        g = _gates[k]
        parked = None
        if g.parked():
            remaining = g.parked_until - time.monotonic()
            parked = datetime.fromtimestamp(
                time.time() + remaining, timezone.utc
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        row = rows.get(spec["host"])
        if row is None:
            rows[spec["host"]] = {
                "host": spec["host"],
                "purpose": spec["purpose"],
                "calls": g.calls,
                "errors": g.errors,
                "latencyMs": g.latency_ms(),
                "parkedUntil": parked,
                "cacheHits": g.cache_hits,
                "cacheMisses": g.cache_misses,
            }
            continue
        # A second gate on a host already listed. The panel names hosts, so its
        # calls count against that row.
        row["calls"] += g.calls
        row["errors"] += g.errors
        row["cacheHits"] += g.cache_hits
        row["cacheMisses"] += g.cache_misses
        row["latencyMs"] = row["latencyMs"] if row["latencyMs"] is not None else g.latency_ms()
        row["parkedUntil"] = max(filter(None, [row["parkedUntil"], parked]), default=None)
    out = list(rows.values())
    return {
        "providers": out,
        "offline": _offline,
        "fetchedAt": _now_iso(),
    }


# ────────────────────────────────────────────────────────────────────────
# Honest coercion helpers
#
# The whole point: these return None where the usual helpers return 0.0.
# ────────────────────────────────────────────────────────────────────────


def num(v: Any) -> Optional[float]:
    """float(v), or None. An unparseable value is UNKNOWN, not zero."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def cents(v: Any) -> Optional[float]:
    """A 0..1 probability as 0..100 cents, or None."""
    f = num(v)
    if f is None:
        return None
    return round(f * 100.0, 2)


def _sentinel_free(c: Optional[float]) -> Optional[float]:
    """Drop a 0¢ / 100¢ quote: a placeholder for an empty book.

    Nobody can rest an order at either extreme, so neither is ever a real quote.
    Passing them through produced a specific, confident lie on every unlisted
    market: bid 0¢, ask 100¢, and a 100¢ spread presented as fact. Caught by the
    live check, not by any unit test, because it needed a market that genuinely
    has no book.
    """
    if c is None:
        return None
    if c <= 0.0 or c >= 100.0:
        return None
    return c


def _jlist(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _iso(v: Any) -> Optional[str]:
    if not v or not isinstance(v, str):
        return None
    return v


def _hours_to(close_iso: Optional[str]) -> Optional[float]:
    # Through us_api: the venue's timestamps carry nanoseconds.
    ts = us.parse_iso_epoch(close_iso)
    if ts is None:
        return None
    return round((ts - time.time()) / 3600.0, 3)


# ────────────────────────────────────────────────────────────────────────
# The merge
# ────────────────────────────────────────────────────────────────────────

# Lower is better. A worse provider never overwrites a better one, and no
# provider ever overwrites anything with None.
PRIO_OWN_AUTHED = 0     # our authed read of our own state
PRIO_OWN_FEED = 1       # our websocket tape, local arrival timestamp
PRIO_BEST_SOURCE = 2    # whichever public source is best at this field
PRIO_FALLBACK = 3       # an indicative value we would rather not rely on


class _Row:
    """Accumulates a market row with per-field provenance.

    ``put`` is the only way in. It drops ``None`` (so a failed provider cannot
    blank a good answer) and drops a worse-priority write (so an indicative
    last price cannot stomp a real top-of-book).
    """

    def __init__(self) -> None:
        self.vals: dict[str, Any] = {}
        self.sources: dict[str, dict] = {}
        self._prio: dict[str, int] = {}
        self.unknowns: list[dict] = []

    def put(self, field: str, value: Any, source: str, prio: int,
            observed_at: Optional[str] = None) -> None:
        if value is None:
            return
        if field in self._prio and self._prio[field] <= prio:
            return
        self.vals[field] = value
        self._prio[field] = prio
        obs = observed_at or _now_iso()
        self.sources[field] = {
            "source": source,
            "observedAt": obs,
            "ageSec": _age_sec(obs),
        }

    def get(self, field: str) -> Any:
        return self.vals.get(field)

    def unknown(self, field: str, reason: str) -> None:
        """Record WHY a field is missing. 'No data' is useless; write the
        sentence that tells the user what would fix it."""
        if field in self.vals:
            return
        if any(u["field"] == field for u in self.unknowns):
            return
        self.unknowns.append({"field": field, "reason": reason})


def _age_sec(observed_at: Optional[str]) -> Optional[float]:
    if not observed_at:
        return None
    try:
        dt = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return round((datetime.now(timezone.utc) - dt).total_seconds(), 1)


# ────────────────────────────────────────────────────────────────────────
# Venue market → MarketSummary
# ────────────────────────────────────────────────────────────────────────


_market_sides = us.market_sides


def _yes_team(raw: dict) -> tuple[Optional[str], Optional[str]]:
    """(name, code) of the team YES stands for on a game market, or (None, None).

    A game winner's question reads "Who will win ... ATL Hawks vs WAS Wizards" and
    never says which of the two YES is. The long side's `team` does, on every one of
    the 61 game winners in a live list.
    """
    yes_side, _ = _market_sides(raw)
    team = yes_side.get("team") if yes_side else None
    if not isinstance(team, dict):
        return None, None
    return (str(team.get("name") or "").strip() or None,
            str(team.get("abbreviation") or "").strip() or None)


def accepting_orders(raw: dict) -> Optional[bool]:
    """Whether the venue says this market takes orders. None when it said
    nothing either way: an unread flag is not a yes."""
    status = str(raw.get("status") or "")
    if not status and raw.get("closed") is None and raw.get("active") is None:
        return None
    if raw.get("closed") or raw.get("archived") or raw.get("active") is False:
        return False
    if status and status != "MARKET_STATUS_OPEN":
        return False
    flags = [s.get("tradable") for s in _market_sides(raw) if s]
    if flags and not any(flags):
        return False
    return True


# The venue's category -> the ids Discover's filter chips send. `summary.category`
# is what those chips compare against, and a name the chips don't know is a row
# no filter can ever keep.
_FILTER_CATEGORY = {
    "macro": "economics", "technology": "tech", "climate": "weather",
    "science": "other",
}
# What categorize_by_keywords answers when the venue named nothing.
_KEYWORD_CATEGORY = {
    "entertainment": "culture", "climate": "weather", "world": "other",
}
# The venue's category -> the agents' canonical ids (mcp_agents.CATEGORIES).
_AGENT_CATEGORY = {
    **us._CATEGORY,
    "macro": "economics", "technology": "world", "geopolitics": "world",
    "climate": "climate",
}


def venue_category(raw: Any) -> Optional[str]:
    """The agents' category id for a raw market, from the category the venue
    states. None when it stated none: a keyword guess here would let an agent
    told "Sports only" buy into something that merely mentions a team."""
    if not isinstance(raw, dict):
        return None
    return _AGENT_CATEGORY.get(str(raw.get("category") or "").strip().lower())


def _title_of(raw: dict, slug: str) -> str:
    """What this market asks, in words.

    On a multi-outcome event `question` is the EVENT's title and `title` is the
    leg ("Major League Soccer Champion" / "Nashville SC"), so the question alone
    names all 21 legs identically. A question that ends in "?" is already whole;
    `title` there is a label ("Set 3 Winner").
    """
    question = str(raw.get("question") or "").strip()
    title = str(raw.get("title") or "").strip()
    if question and title and not question.endswith("?") \
            and title.lower() not in question.lower():
        return f"{question}: {title}"
    return question or title or slug


def _close_time(raw: dict) -> Optional[str]:
    """When the question closes. A timed market's window end, not its `endDate`,
    which runs 30 minutes past the window and is the venue's deadline for
    settling it: cross_venue pairs windows on this instant."""
    terms = raw.get("assetPriceTerms")
    window_end = terms.get("windowEnd") if isinstance(terms, dict) else None
    return _iso(window_end) or _iso(raw.get("endDate"))


def _indicative_yes_cents(raw: dict) -> Optional[float]:
    """A YES price for a market with no two-sided quote, or None.

    `marketSides[].price` is that side's ask, and it survives when the other
    side of the book is empty. `outcomePrices` does NOT serve here: with one
    side quoted it collapses to a single entry holding the NO price
    (`["0.01"]` on a market bid at 99c), and with two it is the two asks, so
    its second entry is not a NO mid.
    """
    yes_side, no_side = _market_sides(raw)
    yes_px = cents(us.amount((yes_side or {}).get("price")))
    if yes_px is not None:
        return yes_px
    no_px = cents(us.amount((no_side or {}).get("price")))
    return None if no_px is None else round(100.0 - no_px, 2)


def summary_from_gamma(raw: dict, observed_at: Optional[str] = None,
                       event_slug: str = "") -> Optional[dict]:
    """Build a MarketSummary from one venue market object, preserving nulls.

    The name is historical; every caller reads it, so it stayed. `event_slug` is
    the parent event when the caller knows it: a market fetched by slug carries
    no link to its event.

    Status and settlement come from `polymarket_api._normalize_market`, the same
    reading the engines use. Its NUMBERS are not used: that normaliser coerces a
    missing quote to a value, which is exactly the lie this module exists to
    avoid.
    """
    if not isinstance(raw, dict):
        return None
    slug = raw.get("slug")
    if not us.valid_slug(slug):
        return None

    # A later read by slug must not wipe the event link an earlier list read
    # learned, so what the meta cache already knows is passed back in.
    event_slug = event_slug or str((api._meta.get(slug) or {}).get("event_slug") or "")
    normalized = api._normalize_market(raw, event_slug=event_slug)
    if normalized is None:
        return None

    row = _Row()
    bid = _sentinel_free(cents(us.amount(raw.get("bestBidQuote"))))
    ask = _sentinel_free(cents(us.amount(raw.get("bestAskQuote"))))
    row.put("bid", bid, "gateway", PRIO_BEST_SOURCE, observed_at)
    row.put("ask", ask, "gateway", PRIO_BEST_SOURCE, observed_at)

    settled = normalized["status"] == "settled"
    if not settled and (bid is None or ask is None):
        row.unknown(
            "bid" if bid is None else "ask",
            "Polymarket US's market list gives no price for this side. Open the "
            "market to read its order book.",
        )

    resolved_outcome = normalized.get("result") or None
    settlement = normalized.get("settlement_value_dollars")
    if settled and settlement is not None:
        # The payout, not a quote: a resolved market has no book.
        row.put("probability", cents(settlement), "gateway", PRIO_BEST_SOURCE, observed_at)
    elif bid is not None and ask is not None:
        row.put("probability", round((bid + ask) / 2.0, 2), "local",
                PRIO_BEST_SOURCE, observed_at)
    else:
        row.put("probability", _indicative_yes_cents(raw), "gateway",
                PRIO_FALLBACK, observed_at)
    if row.get("probability") is None:
        row.unknown(
            "probability",
            "No quote on either side of the book and no last price from the "
            "exchange. It is usually unpriced until somebody quotes it.",
        )

    row.unknown(
        "volume24h",
        "Polymarket US publishes traded totals per market, not a 24h figure. "
        "It is unknown here, not zero.",
    )
    row.unknown(
        "liquidity",
        "Polymarket US publishes no liquidity figure. Open the book to see the "
        "depth that actually exists.",
    )
    row.unknown(
        "priceChange24h",
        "The market list carries no price change. Discover's Volatile column "
        "measures it from price history for the most-traded markets.",
    )

    close_time = _close_time(raw)
    row.put("closeTime", close_time, "gateway", PRIO_BEST_SOURCE, observed_at)
    if close_time is None:
        row.unknown(
            "closeTime",
            "This market has no end date — common for markets that resolve on "
            "an event rather than a date.",
        )

    if not raw.get("status") and raw.get("active") is None and raw.get("closed") is None:
        status = "unknown"
    else:
        status = normalized["status"]
    row.put("status", status, "gateway", PRIO_BEST_SOURCE, observed_at)

    venue_cat = str(raw.get("category") or "").strip().lower()
    if venue_cat:
        category = _FILTER_CATEGORY.get(venue_cat, venue_cat)
    else:
        guess = categorize_by_keywords(str(raw.get("question") or ""), str(slug))
        category = _KEYWORD_CATEGORY.get(guess, guess) if guess else ""

    bid, ask = row.get("bid"), row.get("ask")
    spread = round(ask - bid, 2) if (bid is not None and ask is not None) else None
    mid = round((ask + bid) / 2.0, 2) if (bid is not None and ask is not None) else None
    row.put("spread", spread, "local", PRIO_BEST_SOURCE, observed_at)
    row.put("mid", mid, "local", PRIO_BEST_SOURCE, observed_at)
    if spread is None and not settled:
        row.unknown(
            "spread",
            "A spread needs BOTH a bid and an ask. One side is unknown, so "
            "there is no honest spread to show — half a spread is not one.",
        )

    yes_side, no_side = _market_sides(raw)
    labels = [str((s or {}).get("description") or "") for s in (yes_side, no_side)]
    if not all(labels):
        # No side labels, so `outcomes` is the only text left. Its order can be
        # reversed, which only the Yes/No pair can be corrected for.
        listed = [str(o) for o in _jlist(raw.get("outcomes"))]
        labels = (["Yes", "No"] if set(listed) == {"Yes", "No"}
                  else listed if len(listed) >= 2 else [])

    yes_team, yes_team_code = _yes_team(raw)
    summary = {
        "conditionId": str(slug),
        "venue": "polymarket",
        "title": _title_of(raw, str(slug)),
        "slug": slug,
        "eventSlug": event_slug or None,
        "category": category or None,
        "status": status,
        "outcomes": labels[:2] if labels else None,
        "probability": row.get("probability"),
        "bid": row.get("bid"),
        "ask": row.get("ask"),
        "spread": row.get("spread"),
        "mid": row.get("mid"),
        "volume24hUsd": None,
        "volumeTotalUsd": None,
        "liquidityUsd": None,
        "priceChange24h": None,
        "priceChange1h": None,
        # Contracts, not dollars, and lifetime, not 24h. Filled from the book's
        # stats when one has been read; see _stats_apply.
        "sharesTraded": None,
        "openInterest": None,
        "closeTime": close_time,
        "hoursToClose": _hours_to(close_time),
        "gameStartTime": _iso(raw.get("gameStartTime")),
        "yesTeam": yes_team,
        "yesTeamCode": yes_team_code,
        "negRisk": False,
        "tickSize": num(raw.get("orderPriceMinTickSize")),
        "minOrderSize": num(raw.get("minimumTradeQty")),
        "yesTokenId": us.token(slug, "yes"),
        "noTokenId": us.token(slug, "no"),
        "resolvedOutcome": resolved_outcome,
        "sources": row.sources,
        "unknowns": row.unknowns,
        # Kept off the TS contract on purpose — internal, for the risk report.
        "_raw": raw,
    }
    _stats_apply(summary)
    return summary


# ── traded totals ───────────────────────────────────────────────────────
# The market list has no volume. The book's `stats` does: lifetime contracts,
# lifetime dollars and open interest. Every book we read leaves them here, so a
# market row can show them without paying the book route's tight limit again.

_STATS_TTL = 300.0
_STATS_MAX = 500
_stats: dict[str, tuple[float, dict]] = {}


def _book_stats(md: Any) -> dict:
    st = md.get("stats") if isinstance(md, dict) else None
    st = st if isinstance(st, dict) else {}
    return {
        "sharesTraded": num(st.get("sharesTraded")),
        "notionalTradedUsd": us.amount(st.get("notionalTraded")),
        "openInterest": num(st.get("openInterest")),
        "lastTradeCents": cents(us.amount(st.get("lastTradePx"))),
        "lastTradeAt": _iso(st.get("lastTradeSetTime")),
    }


def _stats_put(slug: str, stats: dict, observed: Optional[str]) -> None:
    if len(_stats) >= _STATS_MAX and slug not in _stats:
        for k in list(_stats)[: _STATS_MAX // 3]:
            _stats.pop(k, None)
    _stats[slug] = (time.monotonic(), {**stats, "observedAt": observed or _now_iso()})


def _stats_get(slug: str) -> Optional[dict]:
    hit = _stats.get(slug)
    if not hit:
        return None
    if time.monotonic() - hit[0] > _STATS_TTL:
        _stats.pop(slug, None)
        return None
    return hit[1]


def _stats_apply(summary: dict) -> None:
    stats = _stats_get(str(summary.get("conditionId") or ""))
    if not stats:
        return
    obs = stats["observedAt"]
    stamp = {"source": "gateway", "observedAt": obs, "ageSec": _age_sec(obs)}
    for key, field in (("notionalTradedUsd", "volumeTotalUsd"),
                       ("sharesTraded", "sharesTraded"),
                       ("openInterest", "openInterest")):
        if stats.get(key) is not None:
            summary[field] = stats[key]
            summary["sources"][field] = dict(stamp)


def _strip_raw(s: dict) -> dict:
    out = dict(s)
    out.pop("_raw", None)
    return out


# ────────────────────────────────────────────────────────────────────────
# Our own live feed — the unfair advantage
#
# clob_ws already decodes the exchange's market websocket and keeps a live
# top-of-book per token, stamped when WE received it. What it does NOT keep
# is a time series. This does: a bounded per-token ring of (localTs, bid,
# ask) samples, which gives us real spread over time and quote lifetime for
# markets no chart provider indexes at that resolution.
#
# GATED ON SUBSCRIPTION. Recording every token Polymarket lists is a memory
# leak with a UI attached — the screen the user has open names what to
# record, and a token that stops being named ages out.
# ────────────────────────────────────────────────────────────────────────

_MICRO_MAX = 900              # samples per token (~15 min at 1/s)
_MICRO_TTL = 20 * 60.0        # a token unnamed for this long stops recording
_MICRO_PERIOD = 1.0           # sampling period, seconds

# token_id -> {"samples": [(ts, bid, ask)], "last_named": monotonic,
#              "condition": str, "side": 'yes'|'no'}
_micro: dict[str, dict] = {}
_micro_task: Optional[asyncio.Task] = None


def subscribe(condition_id: str, yes_token: Optional[str], no_token: Optional[str]) -> None:
    """Name the tokens the open screen cares about. Idempotent and cheap —
    call it on every poll; only a genuinely new token costs anything."""
    try:
        import clob_ws
    except Exception:
        return
    now = time.monotonic()
    for side, tok in (("yes", yes_token), ("no", no_token)):
        if not tok:
            continue
        entry = _micro.get(tok)
        if entry is None:
            _micro[tok] = {"samples": [], "last_named": now,
                           "condition": condition_id, "side": side}
        else:
            entry["last_named"] = now
    clob_ws.feed.observe(yes_token, no_token)
    # Only start the socket if the optional dependency is actually present.
    # clob_ws.feed.start() spawns a task that raises ModuleNotFoundError deep
    # inside _run() when it is not, which surfaces as an unretrieved-exception
    # warning on every subscribe and never recovers. Degrade quietly instead:
    # everything else on the page works, we just have no tape of our own.
    try:
        import websockets  # noqa: F401
    except Exception:
        return
    clob_ws.feed.start()
    _ensure_recorder()


def _ensure_recorder() -> None:
    global _micro_task
    if _micro_task is None or _micro_task.done():
        try:
            _micro_task = asyncio.ensure_future(_record_loop())
        except RuntimeError:
            _micro_task = None


async def _record_loop() -> None:
    """Sample the live top-of-book once a second for every named token.

    Never invents a sample. When the feed has no fresh quote for a token the
    period simply produces nothing — a flat synthetic level in an illiquid
    market is how somebody buys a corpse.
    """
    while True:
        await asyncio.sleep(_MICRO_PERIOD)
        if not _micro:
            continue
        now_mono = time.monotonic()
        now_ts = time.time()
        for tok in list(_micro.keys()):
            entry = _micro[tok]
            if now_mono - entry["last_named"] > _MICRO_TTL:
                _micro.pop(tok, None)
                continue
            # Through _live_quote, so the 0c/100c empty-book sentinels are
            # scrubbed BEFORE they enter the tape. A recorded 100c ask is a
            # permanent lie: it survives in the median spread long after the
            # book it came from filled in.
            q = _live_quote(tok)
            if not q:
                continue    # no fresh quote -> no sample. Never a filled gap.
            bid, ask = q.get("bid_cents"), q.get("ask_cents")
            if bid is None and ask is None:
                continue
            samples: list = entry["samples"]
            samples.append((now_ts, bid, ask))
            if len(samples) > _MICRO_MAX:
                del samples[: len(samples) - _MICRO_MAX]


async def stop_recorder() -> None:
    """Cancel the microstructure sampler. Everything else the backend starts is
    cancelled on shutdown; this was not, so a clean exit left a task running on
    a closing loop."""
    global _micro_task
    global _enrich_task
    t, _micro_task = _micro_task, None
    e, _enrich_task = _enrich_task, None
    _micro.clear()
    for task in (t, e):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def micro_series(condition_id: str, side: str = "yes") -> dict:
    """Our own microstructure for one market: spread over time, built from
    locally-timestamped quotes. Empty until the screen has been open a moment,
    and it says so rather than pretending."""
    for tok, entry in _micro.items():
        if entry["condition"] == condition_id and entry["side"] == side:
            samples = list(entry["samples"])
            pts = [
                {"t": int(ts), "bid": b, "ask": a,
                 "spread": (round(a - b, 2) if (a is not None and b is not None) else None)}
                for ts, b, a in samples
            ]
            spreads = [p["spread"] for p in pts if p["spread"] is not None]
            return {
                "conditionId": condition_id,
                "side": side,
                "points": pts,
                "medianSpreadCents": (
                    round(sorted(spreads)[len(spreads) // 2], 2) if spreads else None),
                "sampleCount": len(pts),
                "source": "clob-ws",
                "note": (
                    None if len(pts) >= 10 else
                    "Recording started when you opened this market — the series "
                    "fills in as quotes arrive."
                ),
            }
    return {
        "conditionId": condition_id, "side": side, "points": [],
        "medianSpreadCents": None, "sampleCount": 0, "source": "clob-ws",
        "note": "Not recording this market yet. It starts when the page opens.",
    }


def _live_quote(token_id: Optional[str]) -> Optional[dict]:
    """Top-of-book from our own feed, or None when it is stale/absent.

    The SAME 0¢/100¢ sentinel scrub the REST quotes get applies here. Caught by
    driving the real app: on a market whose book had no asks at all, the feed
    reported an ask of 100¢, which produced a confident "spread 1.0¢" in the
    header AND a 1.0¢ median in our own microstructure panel — while the book
    right next to it correctly said "no resting asks". Scrubbing here rather
    than at each call site means no future reader of the feed can reintroduce
    it.
    """
    if not token_id:
        return None
    try:
        import clob_ws
    except Exception:
        return None
    try:
        q = clob_ws.feed.get_quote_cents(token_id)
    except Exception:
        return None
    if not q:
        return None
    bid = _sentinel_free(num(q.get("bid_cents")))
    ask = _sentinel_free(num(q.get("ask_cents")))
    if bid is None and ask is None:
        return None
    return {"bid_cents": bid, "ask_cents": ask}


def _overlay_live(summary: dict) -> dict:
    """Let our own feed win over a REST quote where it is fresh.

    This is merge priority 1 beating priority 2 — a quote stamped when we
    received it, which no REST read can match. The feed needs an API key, so
    without one nothing here fires and the REST quote stands.
    """
    q = _live_quote(summary.get("yesTokenId"))
    if not q:
        return summary
    bid, ask = num(q.get("bid_cents")), num(q.get("ask_cents"))
    if bid is None and ask is None:
        return summary
    obs = _now_iso()
    stamp = {"source": "clob-ws", "observedAt": obs, "ageSec": 0.0}
    if bid is not None:
        summary["bid"] = bid
        summary["sources"]["bid"] = dict(stamp)
    if ask is not None:
        summary["ask"] = ask
        summary["sources"]["ask"] = dict(stamp)
    b, a = summary.get("bid"), summary.get("ask")
    if b is not None and a is not None:
        summary["spread"] = round(a - b, 2)
        summary["mid"] = round((a + b) / 2.0, 2)
        summary["sources"]["spread"] = {"source": "local", "observedAt": obs, "ageSec": 0.0}
        summary["sources"]["mid"] = {"source": "local", "observedAt": obs, "ageSec": 0.0}
        summary["probability"] = summary["mid"]
        summary["sources"]["probability"] = {
            "source": "clob-ws", "observedAt": obs, "ageSec": 0.0}
        summary["unknowns"] = [u for u in summary.get("unknowns", [])
                               if u["field"] not in ("spread", "probability")]
    return summary


# ────────────────────────────────────────────────────────────────────────
# Filters
#
# NUMERIC filters SKIP a row whose metric has not arrived — a threshold on a
# quantity nobody produced is not a judgement about that row.
# CATEGORICAL filters EXCLUDE it — the user named a set, and "unknown" is
# not a member of it.
#
# Both behaviours are pinned by tests. Pure function: no network, no clock
# beyond what the rows already carry.
# ────────────────────────────────────────────────────────────────────────

_NUMERIC_FILTERS: list[tuple[str, str, str]] = [
    # (filter key, row field, comparison)
    ("minVolume24hUsd", "volume24hUsd", "min"),
    ("maxVolume24hUsd", "volume24hUsd", "max"),
    ("minLiquidityUsd", "liquidityUsd", "min"),
    ("minProbability", "probability", "min"),
    ("maxProbability", "probability", "max"),
    ("minHoursToClose", "hoursToClose", "min"),
    ("maxHoursToClose", "hoursToClose", "max"),
]


def apply_filters(rows: list[dict], filters: Optional[dict]) -> tuple[list[dict], int, int]:
    """→ (kept, skipped_for_unknown, excluded).

    The two counters are surfaced in the UI. A filter that silently drops rows
    it could not evaluate reads as "there is nothing here", which is a lie
    about the market rather than about our data.
    """
    if not filters:
        return list(rows), 0, 0

    kept: list[dict] = []
    skipped = 0
    excluded = 0

    cats = [str(c).lower() for c in (filters.get("categories") or []) if c]
    search = str(filters.get("search") or "").strip().lower()

    for r in rows:
        # ── categorical: unknown is EXCLUDED ────────────────────────────
        if cats:
            rc = r.get("category")
            if rc is None or str(rc).lower() not in cats:
                excluded += 1
                continue
        if search:
            hay = f"{r.get('title') or ''} {r.get('slug') or ''}".lower()
            if search not in hay:
                excluded += 1
                continue

        # ── numeric: unknown is SKIPPED ─────────────────────────────────
        skip = False
        for fkey, field, comp in _NUMERIC_FILTERS:
            bound = filters.get(fkey)
            if bound is None:
                continue
            val = r.get(field)
            if val is None:
                skip = True
                break
            if comp == "min" and val < bound:
                skip = True
                break
            if comp == "max" and val > bound:
                skip = True
                break
        if skip:
            skipped += 1
            continue
        kept.append(r)

    return kept, skipped, excluded


# ────────────────────────────────────────────────────────────────────────
# Discover
# ────────────────────────────────────────────────────────────────────────

_DISCOVER_PAGE = 100
# Discover's Volatile column reads this many price histories on a cold load.
_VOLATILITY_TOP = 40
_MOVE_TTL = 300.0
# Traded totals read per Discover refresh, in the background. The book route
# allows about one read every two seconds, and a column of 40 must not wait 90
# seconds on it.
_ENRICH_PER_PASS = 3


def _listable(raw: Any) -> bool:
    """Whether a row from the active list belongs on a board. `active=true` still
    returns hidden rows and rows the venue has already marked RESOLVED."""
    return (isinstance(raw, dict) and not raw.get("hidden")
            and str(raw.get("status") or "") != "MARKET_STATUS_RESOLVED")


async def _gateway_markets(params: dict, ttl: float = TTL_DISCOVER) -> tuple[list[dict], Optional[str]]:
    data, observed = await _fetch("gateway", "/v1/markets", params, ttl=ttl)
    rows = data.get("markets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return [], observed
    out = []
    for raw in rows:
        if not _listable(raw):
            continue
        s = summary_from_gamma(raw, observed)
        if s:
            out.append(s)
    return out, observed


def _active_page(**order: Any) -> dict:
    return {"limit": _DISCOVER_PAGE, "active": "true", "closed": "false", **order}


async def _measure_move(row: dict) -> None:
    """Fill one row's 24h and 1h move from its price history. A market with too
    little history keeps None: unknown, not flat."""
    points = await _history_points(row["conditionId"], "yes", "INTERVAL_1D", 60, ttl=_MOVE_TTL)
    if len(points) < 2:
        return
    last = points[-1]
    stamp = {"source": "gateway", "observedAt": _now_iso(), "ageSec": 0.0}
    row["priceChange24h"] = round(last["p"] - points[0]["p"], 2)
    row["sources"]["priceChange24h"] = dict(stamp)
    row["unknowns"] = [u for u in row["unknowns"] if u["field"] != "priceChange24h"]
    hour_ago = [p for p in points if p["t"] <= last["t"] - 3600]
    if hour_ago:
        row["priceChange1h"] = round(last["p"] - hour_ago[-1]["p"], 2)
        row["sources"]["priceChange1h"] = dict(stamp)


_enrich_task: Optional[asyncio.Task] = None


async def _enrich_stats(slugs: list[str]) -> None:
    for slug in slugs:
        await _read_book(slug)


def _enrich_stats_soon(rows: list[dict]) -> None:
    """Start reading traded totals for a few rows that lack them. Never awaited
    by the column: the rows show them on a later refresh."""
    global _enrich_task
    if _offline or _gates["gateway_book"].parked():
        return
    if _enrich_task is not None and not _enrich_task.done():
        return
    wanted = [str(r["conditionId"]) for r in rows
              if _stats_get(str(r["conditionId"])) is None][:_ENRICH_PER_PASS]
    if wanted:
        _enrich_task = asyncio.ensure_future(_enrich_stats(wanted))


async def discover(column: str, limit: int = 40,
                   filters: Optional[dict] = None) -> dict:
    """One Discover column, honestly filtered."""
    limit = max(1, min(int(limit or 40), 100))
    note: Optional[str] = None
    rows: list[dict] = []
    observed: Optional[str] = None

    if column == "watchlist":
        entries = watchlist_list()
        ids = [e["conditionId"] for e in entries]
        rows = await _summaries_for(ids)
        # "You starred nothing" and "we could not load what you starred" are
        # different facts, and telling the user the first when the second is
        # true makes them think they lost their own list.
        if rows:
            note = None
        elif not ids:
            note = "Nothing on your watchlist yet — star a market to add it."
        else:
            note = (
                f"You have {len(ids)} market(s) on your watchlist, but none "
                f"could be loaded just now. This is a provider failure, not an "
                f"empty list — your watchlist is intact."
            )
        if rows and len(rows) < len(ids):
            note = (
                f"{len(ids) - len(rows)} of your {len(ids)} watched market(s) "
                f"could not be loaded right now. They are still on the list."
            )

    elif column == "new":
        rows, observed = await _gateway_markets(
            _active_page(orderBy="start_date", orderDirection="desc"))
        rows.sort(key=lambda r: str((r.get("_raw") or {}).get("startDate") or ""), reverse=True)
        if rows:
            note = (
                "Just-listed markets usually have no book yet, so their price and "
                "spread read as unknown until somebody quotes them. That is the "
                "market being empty, not our data being missing."
            )

    elif column == "closing":
        # Minute precision: an instant that changes every second is a new cache
        # key, here and at the CDN, on every refresh.
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        rows, observed = await _gateway_markets(_active_page(
            orderBy="end_date", orderDirection="asc",
            endDateMin=now.strftime("%Y-%m-%dT%H:%M:%SZ")))
        # A finished 15-minute window keeps its later `endDate` for half an hour,
        # so it survives the filter above with its close already behind it.
        rows = [r for r in rows
                if r.get("hoursToClose") is None or r["hoursToClose"] > 0]
        # A row with no end date cannot be ranked by time-to-close. It is not
        # dropped — it is sorted to the end and still readable.
        rows.sort(key=lambda r: (r.get("hoursToClose") is None,
                                 r.get("hoursToClose") if r.get("hoursToClose") is not None else 0))
        if rows:
            note = (
                "Sports close dates are the exchange's deadline to settle, usually "
                "days after the game itself."
            )

    elif column == "volume":
        # The venue's own ranking is kept as it comes. It is the only ranking
        # that covers every row, and re-sorting on the few totals known so far
        # would reorder the column each time another one arrived.
        rows, observed = await _gateway_markets(
            _active_page(orderBy="volume", orderDirection="desc"))
        if rows:
            note = (
                "Ordered by Polymarket's own volume ranking. It publishes no 24h "
                "volume, and lifetime totals fill in a few markets at a time."
            )

    elif column == "volatility":
        # Biggest MOVER, not biggest number. Ranked on the absolute 24h price
        # change, because a market that fell 40c is exactly as interesting as
        # one that rose 40c — the direction is the user's read to make, and
        # signing the rank would bury half the movers.
        #
        # The venue reports no price change at all, so it is measured here from
        # price history, for the most-traded page only: the route is cheap but
        # not free, and there are over 20,000 markets.
        rows, observed = await _gateway_markets(
            _active_page(orderBy="volume", orderDirection="desc"))
        measured = rows[:_VOLATILITY_TOP]
        await asyncio.gather(*[_measure_move(r) for r in measured])
        # An unknown move sorts to the END and is still readable. Treating it
        # as 0.0 would rank it as the calmest market on the board, which is a
        # judgement nobody made.
        rows.sort(key=lambda r: (r.get("priceChange24h") is None,
                                 -abs(r.get("priceChange24h") or 0.0)))
        unranked = sum(1 for r in rows if r.get("priceChange24h") is None)
        movers = len(rows) - unranked
        if movers:
            note = (
                "Ranked by how far the price moved in 24h, up or down — the "
                f"direction is yours to read. Only the {len(measured)} most-traded "
                "markets are measured, so a quiet market can have moved more."
                + (f" {unranked} market(s) here have no measured move and are "
                   f"listed last; that is unknown, not flat." if unranked else "")
            )
        elif rows:
            note = (
                f"No price history could be read for the {len(measured)} "
                f"most-traded markets, so nothing can be ranked by movement. "
                f"That is a failed read, not a calm board."
            )

    else:
        return {
            "column": column, "rows": [], "fetchedAt": _now_iso(),
            "skippedForUnknown": 0, "excluded": 0,
            "note": f"Unknown column '{column}'.",
        }

    if not rows and note is None:
        g = _gates["gateway"]
        note = (
            OFFLINE_REASON if _offline else
            "Polymarket is rate-limiting us right now — this column will refill "
            "automatically shortly."
            if g.parked() else
            "The market list came back empty. That is a provider result, not "
            "a filter result."
        )

    kept, skipped, excluded = apply_filters(rows, filters)
    kept = kept[:limit]
    for r in kept:
        _overlay_live(r)
    if column == "volume":
        _enrich_stats_soon(kept)

    # The offline reason OVERRIDES any column-specific note, because those are
    # explanations for an empty column and offline mode is the actual reason.
    # Left unguarded, "New" said "just-listed markets have no book yet" and
    # "Resolving" said "none of the 0 most recently closed markets is awaiting
    # settlement" — both plausible-sounding claims about Polymarket made after
    # asking nobody. That is the exact failure this whole module exists to
    # prevent, one level up.
    if _offline and not kept:
        note = OFFLINE_REASON

    return {
        "column": column,
        "rows": [_strip_raw(r) for r in kept],
        "fetchedAt": observed or _now_iso(),
        "skippedForUnknown": skipped,
        "excluded": excluded,
        "note": note,
    }


_SLUG_BATCH = 40


async def _summaries_for(condition_ids: Iterable[str]) -> list[dict]:
    """Fetch several markets by slug in one read per 40.

    `/v1/markets?slug=a&slug=b` answers for open and resolved markets alike, so
    a settled market stays on the watchlist and a portfolio of N markets is not
    N round trips. Its quotes can be up to 30s old (the CDN caches this route);
    `clob_quotes` re-reads them where a mark depends on it. Rows come back in the
    order asked for, which is what keeps a watchlist in the user's order.
    """
    ids = list(dict.fromkeys(str(c) for c in condition_ids if us.valid_slug(str(c))))
    out: list[dict] = []
    for i in range(0, len(ids), _SLUG_BATCH):
        chunk = ids[i:i + _SLUG_BATCH]
        data, observed = await _fetch(
            "gateway", "/v1/markets", {"slug": chunk, "limit": len(chunk)}, ttl=TTL_MARKET)
        rows = data.get("markets") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        wanted = set(chunk)
        for raw in rows:
            s = summary_from_gamma(raw, observed)
            if s and s["conditionId"] in wanted:
                out.append(s)
    place = {c: i for i, c in enumerate(ids)}
    out.sort(key=lambda r: place[r["conditionId"]])
    return out


def side_touch(summary: dict, side: str) -> tuple[Optional[float], Optional[float]]:
    """(bid, ask) in cents for one side, off a market summary. The book is quoted
    on YES, so what sells NO is what buys YES: NO's bid is 100 less YES's ask."""
    bid, ask = num(summary.get("bid")), num(summary.get("ask"))
    if str(side).lower() == "no":
        return (None if ask is None else round(100.0 - ask, 4),
                None if bid is None else round(100.0 - bid, 4))
    return bid, ask


_MARKET_URL = re.compile(r"polymarket\.us/(?:[\w.-]+/)*([\w.-]+)", re.IGNORECASE)


def _pasted_slug(query: str) -> Optional[str]:
    """The slug in a pasted market or event link, or a query shaped like a slug.
    A bare word is a text search."""
    m = _MARKET_URL.search(query)
    if m:
        return m.group(1)
    return query if ("-" in query and us.valid_slug(query)) else None


async def _summary_live(slug: str) -> Optional[dict]:
    """One market by slug. The one public read the CDN does not cache, so its
    top of book is current where /bbo and /book can be 30s behind."""
    if not us.valid_slug(slug):
        return None
    data, observed = await _fetch("gateway", f"/v1/market/slug/{slug}", ttl=TTL_MARKET)
    raw = data.get("market") if isinstance(data, dict) else None
    return summary_from_gamma(raw, observed) if isinstance(raw, dict) else None


async def search(query: str, limit: int = 30) -> dict:
    """Search by text, or resolve a pasted slug or polymarket.us link directly.

    A user who pastes a link or a slug means that market, not a text match, so
    try the exact lookup first: a market, then an event's markets.
    """
    q = str(query or "").strip()
    if not q:
        return {"column": "new", "rows": [], "fetchedAt": _now_iso(),
                "skippedForUnknown": 0, "excluded": 0, "note": "Type something to search."}

    pasted = _pasted_slug(q)
    if pasted:
        one = await _summary_live(pasted)
        rows = [one] if one else (await _event_markets(pasted))[:limit]
        if rows:
            for r in rows:
                _overlay_live(r)
            return {
                "column": "new", "rows": [_strip_raw(r) for r in rows],
                "fetchedAt": _now_iso(), "skippedForUnknown": 0, "excluded": 0,
                "note": None,
            }

    # Polymarket's own full-text search covers every active market, not just
    # the top-volume page; its events embed full market objects. The local
    # filter below stays as the fallback when search is unreachable or empty.
    found, observed = await _fetch(
        "gateway", "/v1/search", {"query": q, "limit": 20}, ttl=TTL_DISCOVER)
    hits: list[dict] = []
    for ev in (found or {}).get("events") or [] if isinstance(found, dict) else []:
        if not isinstance(ev, dict):
            continue
        for raw in ev.get("markets") or []:
            if not _listable(raw) or raw.get("closed") or not raw.get("active", True):
                continue
            row = summary_from_gamma(raw, observed, event_slug=str(ev.get("slug") or ""))
            if row:
                hits.append(row)
            if len(hits) >= limit:
                break
        if len(hits) >= limit:
            break
    if hits:
        for r in hits:
            _overlay_live(r)
        return {
            "column": "volume", "rows": [_strip_raw(r) for r in hits],
            "fetchedAt": observed or _now_iso(), "skippedForUnknown": 0, "excluded": 0,
            "note": None,
        }

    rows, observed = await _gateway_markets(
        _active_page(orderBy="volume", orderDirection="desc"), ttl=TTL_DISCOVER)
    ql = q.lower()
    hits = [r for r in rows
            if ql in f"{r.get('title') or ''} {r.get('slug') or ''}".lower()][:limit]
    for r in hits:
        _overlay_live(r)
    return {
        "column": "volume",
        "rows": [_strip_raw(r) for r in hits],
        "fetchedAt": observed or _now_iso(),
        "skippedForUnknown": 0, "excluded": 0,
        "note": (
            None if hits else
            "No match in the actively-traded markets. Search covers open "
            "markets, not the full archive — paste a market's link or slug for "
            "an exact market."
        ),
    }


# ────────────────────────────────────────────────────────────────────────
# Entity page
# ────────────────────────────────────────────────────────────────────────


# The last good summary per market, for market() to fall back on while the
# gateway is parked. Bounded; a copy goes out so callers can't mutate the stash.
_LAST_MARKET_MAX = 500
_LAST_MARKET_TTL = 600.0
_last_market: dict[str, tuple[float, dict]] = {}


def _last_market_put(cid: str, s: dict) -> None:
    if len(_last_market) >= _LAST_MARKET_MAX and cid not in _last_market:
        for k in list(_last_market)[: _LAST_MARKET_MAX // 3]:
            _last_market.pop(k, None)
    _last_market[cid] = (time.monotonic(), dict(s))


def _last_market_get(cid: str) -> Optional[dict]:
    hit = _last_market.get(cid)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age > _LAST_MARKET_TTL:
        _last_market.pop(cid, None)
        return None
    s = dict(hit[1])
    s["metaStaleSec"] = round(age, 1)
    return s


async def market(condition_id: str, *, include_raw: bool = False) -> Optional[dict]:
    """One market, fully assembled, open or resolved. Also names its tokens to
    the live feed so the microstructure series starts recording while the page
    is open."""
    cid = str(condition_id or "").strip()
    if not cid:
        return None
    s = await _summary_live(cid)
    if s:
        _last_market_put(cid, s)
    else:
        # Gateway parked after a 429 (or briefly down): the market's metadata —
        # tick, minimum size, fees, status — barely moves. Found live 2026-10-12
        # on the old venue: a 30s park failed an agent's order preview mid-test.
        s = _last_market_get(cid)
        if s is None:
            return None
    subscribe(cid, s.get("yesTokenId"), s.get("noTokenId"))
    _overlay_live(s)
    if s.get("status") != "settled" and (s.get("bid") is None or s.get("ask") is None):
        bid, ask, observed = await _book_top(cid)
        if observed:
            _put_quote(s, bid, ask, "gateway", observed)
    return s if include_raw else _strip_raw(s)


def _levels(levels: Iterable[tuple[float, float]], *, descending: bool) -> list[dict]:
    """Normalise one side of a book, already seen from the side asked for, into
    cumulative levels.

    Providers emit duplicate price levels in a single response; collapse them
    (sum the size) and sort, or every cumulative figure downstream is wrong.
    """
    by_price: dict[float, float] = {}
    for px, qty in levels or []:
        p = round(float(px) * 100.0, 2)
        if qty <= 0:
            continue
        by_price[p] = by_price.get(p, 0.0) + float(qty)

    out: list[dict] = []
    cum = 0.0
    for p in sorted(by_price, reverse=descending):
        sz = by_price[p]
        cum += sz
        out.append({
            "priceCents": p,
            "size": round(sz, 2),
            "cumulative": round(cum, 2),
            "notionalUsd": round(sz * p / 100.0, 2),
        })
    return out


async def _read_book(slug: str) -> tuple[Optional[dict], Optional[str]]:
    """The raw `marketData` of one book, through the book route's own gate. Every
    read also refreshes the market's traded totals."""
    data, observed = await _fetch("gateway_book", f"/v1/markets/{slug}/book", ttl=TTL_BOOK)
    md = data.get("marketData") if isinstance(data, dict) else None
    if not isinstance(md, dict):
        return None, observed
    _stats_put(slug, _book_stats(md), observed)
    return md, observed


async def _book_top(slug: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """(best bid ¢, best ask ¢, observedAt) of the YES book. The market row leaves
    both quotes empty on many thin markets whose book is two-sided (20 of the 40
    newest, 2026-11-05), so an empty row side has to be checked here."""
    md, observed = await _read_book(slug)
    if md is None:
        return None, None, None
    parsed = us.parse_book(md)
    bids, asks = parsed["bids"], parsed["asks"]
    return (_sentinel_free(cents(bids[0][0])) if bids else None,
            _sentinel_free(cents(asks[0][0])) if asks else None,
            observed or _now_iso())


async def book(condition_id: str, side: str = "yes") -> dict:
    """Full depth for one side of a market.

    The venue has one book, quoted on YES. NO is its mirror: NO bids are YES asks
    at 100 minus the price, so the two sides answer from one request.

    ``available: false`` means the venue returned nothing. That is NOT an empty
    book, and the UI has to be able to tell them apart — a market with no
    resting orders and a market we could not read look identical otherwise.
    """
    side = "no" if str(side).lower() == "no" else "yes"
    slug = str(condition_id or "")
    empty = {
        "conditionId": condition_id, "tokenSide": side,
        "bids": [], "asks": [], "bestBid": None, "bestAsk": None,
        "spreadCents": None, "midCents": None,
        "bidDepthUsd": None, "askDepthUsd": None,
        "available": False, "fetchedAt": _now_iso(), "source": "gateway",
        "ageSec": None, "state": None, "stats": None,
        "note": None,
    }

    if not us.valid_slug(slug):
        empty["note"] = "That is not a Polymarket US market slug, so there is no book to read."
        return empty

    md, observed = await _read_book(slug)
    if md is None:
        g = _gates["gateway_book"]
        empty["note"] = (
            OFFLINE_REASON if _offline else
            "Polymarket is rate-limiting book reads — the book will refresh in "
            "a few seconds." if g.parked() else
            "Polymarket did not return a book for this market. This is a failed "
            "read, not an empty book."
        )
        return empty

    parsed = us.parse_book(md)
    seen = us.book_for_side(parsed, side)
    bids = _levels(seen["bids"], descending=True)
    asks = _levels(seen["asks"], descending=False)
    best_bid = bids[0]["priceCents"] if bids else None
    best_ask = asks[0]["priceCents"] if asks else None
    spread = round(best_ask - best_bid, 2) if (best_bid is not None and best_ask is not None) else None
    mid = round((best_ask + best_bid) / 2.0, 2) if spread is not None else None
    age = _age_sec(observed)
    state = str(parsed.get("state") or "") or None

    note = None
    if not bids and not asks:
        note = "The book is genuinely empty — nobody has a resting order here."
    elif best_bid is None:
        note = "No resting bids. You can offer, but nothing is there to hit."
    elif best_ask is None:
        note = "No resting asks. You can bid, but nothing is there to lift."
    if state and state != "MARKET_STATE_OPEN":
        pretty = state.replace("MARKET_STATE_", "").replace("_", " ").lower()
        note = " ".join(filter(None, [
            f"The exchange has this market's book {pretty}, so orders may not be "
            f"accepted.", note]))
    # This route sits behind a CDN that caches it for up to 30 seconds. A
    # snapshot that old is a different book on a fast market (measured: a 15m
    # window 3c away from its uncached quote), so it says how old it is.
    if age is not None and age >= BOOK_STALE_NOTE_SEC:
        note = " ".join(filter(None, [
            note,
            f"This snapshot is {age:.0f}s old: Polymarket serves the book through "
            f"a cache of up to 30 seconds. The market's own bid and ask are newer.",
        ]))

    return {
        "conditionId": condition_id, "tokenSide": side,
        "bids": bids, "asks": asks,
        "bestBid": best_bid, "bestAsk": best_ask,
        "spreadCents": spread, "midCents": mid,
        "bidDepthUsd": round(sum(l["notionalUsd"] for l in bids), 2) if bids else None,
        "askDepthUsd": round(sum(l["notionalUsd"] for l in asks), 2) if asks else None,
        "available": True,
        "fetchedAt": observed or _now_iso(),
        "source": "gateway",
        "ageSec": age,
        "state": state,
        "stats": _book_stats(md),
        "note": note,
    }


# ────────────────────────────────────────────────────────────────────────
# Marks
#
# A list read's bid and ask can be 30s old, because the CDN caches that route,
# and on a 15-minute window that is cents. Anything that MARKS a position or
# BENCHMARKS a forecast reads the market by slug, which the CDN does not cache.
# ────────────────────────────────────────────────────────────────────────


async def _gateway_tops(slugs: list[str]) -> dict[str, tuple]:
    """slug → (best bid ¢, best ask ¢, observedAt), YES side, for every market
    that answered. A side with no resting orders is None. One request per market
    on purpose: the batched list is the cached route this exists to avoid. Only a
    row with an empty side costs a second read, of the book."""
    async def one(slug: str) -> tuple[str, Optional[dict], Optional[str]]:
        data, observed = await _fetch("gateway", f"/v1/market/slug/{slug}", ttl=TTL_MARKET)
        raw = data.get("market") if isinstance(data, dict) else None
        return slug, raw if isinstance(raw, dict) else None, observed

    out: dict[str, tuple] = {}
    for slug, raw, observed in await asyncio.gather(*[one(s) for s in slugs]):
        if raw is not None:
            out[slug] = (_sentinel_free(cents(us.amount(raw.get("bestBidQuote")))),
                         _sentinel_free(cents(us.amount(raw.get("bestAskQuote")))),
                         observed or _now_iso())

    async def fill(slug: str) -> None:
        bid, ask, observed = await _book_top(slug)
        if observed:
            out[slug] = (bid, ask, observed)

    await asyncio.gather(*[fill(s) for s, q in list(out.items()) if q[0] is None or q[1] is None])
    return out


def _put_quote(row: dict, bid: Optional[float], ask: Optional[float],
               source: str, observed: str) -> None:
    """Overwrite a row's quote with a fresh read of the top of book. An empty
    side is an answer (nobody is resting there), so it replaces the older number
    with None rather than leaving a price nobody would trade at."""
    sources = row.setdefault("sources", {})
    known = {f for f, v in (("bid", bid), ("ask", ask)) if v is not None}
    if len(known) == 2:
        known |= {"spread", "mid", "probability"}
    unknowns = row["unknowns"] = [u for u in row.get("unknowns", []) if u["field"] not in known]
    stamp = {"source": source, "observedAt": observed, "ageSec": _age_sec(observed)}
    row["bid"], row["ask"] = bid, ask
    for field, v in (("bid", bid), ("ask", ask)):
        sources[field] = dict(stamp)
        if v is None and not any(u["field"] == field for u in unknowns):
            unknowns.append({"field": field, "reason": (
                f"The live book has no resting {'bids' if field == 'bid' else 'offers'} "
                f"on this market.")})
    if bid is not None and ask is not None:
        row["spread"] = round(ask - bid, 2)
        row["mid"] = round((ask + bid) / 2.0, 2)
        row["probability"] = row["mid"]
        for field in ("spread", "mid", "probability"):
            sources[field] = dict(stamp)
    else:
        # No mid on the book. The probability stays whatever it was, labelled as
        # such in `sources`; a spread or mid would be half a quote.
        row["spread"] = row["mid"] = None


async def clob_quotes(rows: list[dict]) -> list[dict]:
    """Re-quote MarketSummary rows from the freshest read there is, in place.

    Our own feed first when it is fresh (no request at all), then one uncached
    read for every remaining market. A row that read fails for keeps the quote it
    has, whose `sources` entry says how old it is. Settled rows are left alone:
    their price is the payout."""
    need: dict[str, list[dict]] = {}
    for r in rows:
        slug = r.get("conditionId")
        if not slug or r.get("resolvedOutcome") is not None or r.get("status") == "settled":
            continue
        q = _live_quote(r.get("yesTokenId"))
        if q and q.get("bid_cents") is not None and q.get("ask_cents") is not None:
            _put_quote(r, q["bid_cents"], q["ask_cents"], "clob-ws", _now_iso())
            continue
        need.setdefault(str(slug), []).append(r)
    if need:
        for slug, (bid, ask, observed) in (await _gateway_tops(list(need))).items():
            for r in need.get(slug, []):
                _put_quote(r, bid, ask, "gateway", observed)
    return rows


async def marked_summaries(condition_ids: Iterable[str]) -> list[dict]:
    """`_summaries_for`, re-quoted from the freshest read — for marks."""
    return await clob_quotes(await _summaries_for(condition_ids))


# Interval → (venue interval, fidelity minutes). Fidelity is the bucket width;
# asking for 1-minute buckets over "max" returns a payload nobody needs, and the
# venue refuses it outright (400 on INTERVAL_ALL at fidelity 1).
_INTERVAL_SPEC = {
    "1h": ("INTERVAL_1H", 1),
    "6h": ("INTERVAL_6H", 5),
    "1d": ("INTERVAL_1D", 10),
    "1w": ("INTERVAL_1W", 60),
    "max": ("INTERVAL_ALL", 180),
}


def _dedupe_points(points: list[dict]) -> list[dict]:
    """Sort ascending and collapse duplicate timestamps.

    Providers really do emit the same bucket twice in one response, and every
    charting library throws on a non-ascending series. Normalise at the source
    so no downstream renderer has to know.
    """
    by_t: dict[int, float] = {}
    for p in points:
        t = p.get("t")
        v = p.get("p")
        if t is None or v is None:
            continue
        try:
            by_t[int(t)] = float(v)
        except (TypeError, ValueError):
            continue
    return [{"t": t, "p": by_t[t]} for t in sorted(by_t)]


def _history_price(row: dict, side: str) -> Optional[float]:
    """One history sample as cents of `side`.

    `longPrice` and `shortPrice` are each side's ask (.23 and .839 on one row, so
    they don't sum to 1), which puts a thin market's "probability" at its offer.
    The mid of the two is the same figure the market row calls its probability.
    With one side missing, the side present is all there is.
    """
    yes = cents(row.get("longPrice"))
    no = cents(row.get("shortPrice"))
    if yes is not None and no is not None:
        mid_yes = round((yes + 100.0 - no) / 2.0, 2)
    elif yes is not None:
        mid_yes = yes
    elif no is not None:
        mid_yes = round(100.0 - no, 2)
    else:
        return None
    return round(100.0 - mid_yes, 2) if side == "no" else mid_yes


async def _history_points(slug: str, side: str, interval: str, fidelity: int,
                          *, ttl: float = TTL_HISTORY) -> list[dict]:
    data, _ = await _fetch(
        "gateway", "/v1/price-history",
        {"symbol": slug, "fidelity": fidelity, "fixedInterval": interval}, ttl=ttl)
    points: list[dict] = []
    if isinstance(data, dict):
        for h in data.get("history") or []:
            if not isinstance(h, dict):
                continue
            t, p = h.get("timestamp"), _history_price(h, side)
            if t is None or p is None:
                continue
            points.append({"t": t, "p": p})
    return _dedupe_points(points)


async def price_history(condition_id: str, side: str = "yes",
                        interval: str = "1d") -> dict:
    """Probability over time, 0..100.

    The venue's series is SAMPLED: one point per bucket whether or not anything
    traded, so a flat stretch means nobody moved the quote, not that a trade
    printed there.

    Falls back to our OWN tape when the venue index has nothing. The rule that
    matters: *any* real data beats none. A threshold that discards our own
    samples because there are only two of them, then reports "no data" while
    holding the only tape that exists, is the worst possible outcome — so a
    thin series is returned and LABELLED thin.
    """
    side = "no" if str(side).lower() == "no" else "yes"
    iv = interval if interval in _INTERVAL_SPEC else "1d"
    venue_iv, fidelity = _INTERVAL_SPEC[iv]
    slug = str(condition_id or "")

    base = {
        "conditionId": condition_id, "side": side, "interval": iv,
        "points": [], "source": "gateway", "ownFeed": False,
        "thin": False, "note": None,
    }

    if not us.valid_slug(slug):
        base["note"] = "That is not a Polymarket US market slug, so there is no price history to read."
        return base

    points = await _history_points(slug, side, venue_iv, fidelity)

    if points:
        base["points"] = points
        base["thin"] = len(points) < 5
        if base["thin"]:
            base["note"] = (
                f"Only {len(points)} price point(s) in this window — the market "
                "is new or barely quoted. This is real data, not a smoothed guess."
            )
        return base

    # No venue history. Do we hold a tape of our own?
    own = micro_series(condition_id, side)
    own_pts = [
        {"t": p["t"], "p": (p["ask"] + p["bid"]) / 2.0}
        for p in own["points"]
        if p.get("bid") is not None and p.get("ask") is not None
    ]
    own_pts = _dedupe_points(own_pts)
    if own_pts:
        base.update({
            "points": own_pts, "source": "clob-ws", "ownFeed": True,
            "thin": len(own_pts) < 30,
            "note": (
                "Polymarket has no indexed history for this market at this "
                f"resolution. These {len(own_pts)} point(s) are our own live "
                "feed, timestamped when we received each quote."
            ),
        })
        return base

    g = _gates["gateway"]
    base["note"] = (
        OFFLINE_REASON if _offline else
        "Polymarket is rate-limiting us — history will load shortly."
        if g.parked() else
        "No price history exists for this market yet. Leave this page open and "
        "our own feed will start building one from live quotes."
    )
    return base


TAPE_NOTE = (
    "Polymarket US does not publish a public trade feed, so there are no "
    "individual trades to show here. The book and the price chart are the public "
    "record of this market."
)


async def tape(condition_id: str, limit: int = 40) -> dict:
    """Recent public trades for one market: always none on this venue.

    It publishes no public tape, no trader identities and no holders. The signed
    websocket's trade stream needs an API key and feeds the engines; a tape built
    from it would only cover the time this app happened to be connected.
    """
    return {
        "conditionId": condition_id, "trades": [], "fetchedAt": _now_iso(),
        "note": TAPE_NOTE,
    }


# ────────────────────────────────────────────────────────────────────────
# Resolution risk — the "security panel" of a prediction market
#
# In crypto the danger is a rug. Here it is RESOLUTION: who decides, on what
# source, how ambiguous the wording is, whether a sibling market contradicts
# this one. Almost nobody surfaces this well, and it is where the value is.
#
# Every check reports `resolved` — whether it could be RUN. The score is the
# average over the checks that actually resolved, it says so in words, and
# below MIN_RESOLVED it refuses to print a score at all. An unread check is
# never silently a pass.
# ────────────────────────────────────────────────────────────────────────

MIN_RESOLVED_CHECKS = 4

_LEVEL_SCORE = {"ok": 100.0, "watch": 55.0, "risk": 10.0}

# Wording that has historically driven settlement disputes: it hands the resolver
# judgement rather than a fact to look up.
_AMBIGUOUS = [
    "at the discretion", "reasonable", "generally", "approximately", "about ",
    "widely reported", "consensus", "credible", "substantially", "significant",
    "in the opinion", "deemed", "may be", "or similar", "etc.",
]

_STRONG_SOURCES = [
    "associated press", "reuters", "bloomberg", "espn", "official",
    "sec.gov", ".gov", "nasa", "noaa", "cftc", "federalreserve",
]


def _check(cid: str, label: str, level: str, detail: str, resolved: bool = True) -> dict:
    return {"id": cid, "label": label, "level": level, "detail": detail,
            "resolved": resolved}


async def resolution_report(condition_id: str) -> dict:
    """Ten checks on how this market will actually settle."""
    s = await market(condition_id, include_raw=True)
    if not s:
        return {
            "conditionId": condition_id, "checks": [], "resolvedCount": 0,
            "totalCount": 0, "score": None,
            "scoreNote": "Could not read this market, so no check could run.",
            "resolverName": None, "resolutionSource": None,
            "fetchedAt": _now_iso(),
        }

    raw: dict = s.get("_raw") or {}
    checks: list[dict] = []
    desc = str(raw.get("description") or "")
    question = str(raw.get("question") or "")
    terms = raw.get("assetPriceTerms") if isinstance(raw.get("assetPriceTerms"), dict) else {}
    index_symbol = str(terms.get("indexSymbol") or "").strip()
    named = sorted(k for k in _STRONG_SOURCES if k in desc.lower())
    res_source = index_symbol or (named[0] if named else "")

    # 1 — is there a named resolution source at all? The venue has no field for
    #     it except on price markets, so the rules text is searched for one.
    if index_symbol:
        checks.append(_check(
            "source", "Resolution source named", "ok",
            f"Settles on the {index_symbol} price index."))
    elif named:
        checks.append(_check(
            "source", "Resolution source named", "ok",
            f"The rules name {', '.join(named[:3])}."))
    elif desc:
        checks.append(_check(
            "source", "Resolution source named", "watch",
            "The rules name none of the sources that historically settle "
            "cleanly. Read them before sizing up."))
    else:
        checks.append(_check(
            "source", "Resolution source named", "unknown",
            "Polymarket returned no description for this market.", resolved=False))

    # 2 — are the rules written down, and are they substantial?
    if desc:
        n = len(desc)
        checks.append(_check(
            "rules", "Rules text present",
            "ok" if n >= 200 else "watch",
            f"{n} characters of rules." + (
                "" if n >= 200 else
                " That is short for a market that has to settle on a fact — "
                "short rules are where disputes come from.")))
    else:
        checks.append(_check(
            "rules", "Rules text present", "risk",
            "This market ships with no rules text at all. Nothing states how "
            "it settles."))

    # 3 — ambiguous wording
    blob = f"{question} {desc}".lower()
    found = [w.strip() for w in _AMBIGUOUS if w in blob]
    if desc or question:
        checks.append(_check(
            "ambiguity", "Wording is unambiguous",
            "ok" if not found else ("risk" if len(found) >= 3 else "watch"),
            "No judgement-language found in the question or rules."
            if not found else
            f"Judgement language: {', '.join(sorted(set(found))[:6])}. "
            "Each phrase hands the resolver a decision instead of a fact."))
    else:
        checks.append(_check(
            "ambiguity", "Wording is unambiguous", "unknown",
            "No text to read.", resolved=False))

    # 4 — settlement date
    close = s.get("closeTime")
    hrs = s.get("hoursToClose")
    venue_status = str(raw.get("status") or "")
    if close and hrs is not None:
        if venue_status == "MARKET_STATUS_RESOLVED":
            checks.append(_check(
                "settlement", "Settlement date set", "ok",
                f"Closed {close}, and the exchange has settled it."))
        elif hrs > 0:
            checks.append(_check(
                "settlement", "Settlement date set", "ok",
                f"Closes {close} — {hrs:.1f}h away."))
        else:
            checks.append(_check(
                "settlement", "Settlement date set",
                "watch" if hrs > -168 else "risk",
                f"Closed {abs(hrs):.0f}h ago and has not posted an outcome. "
                "Long gaps between close and settlement usually mean the "
                "exchange is waiting on a source."))
    else:
        checks.append(_check(
            "settlement", "Settlement date set", "unknown",
            "No end date on this market — it resolves on an event, not a "
            "date, so there is no schedule to check against.", resolved=False))

    # 5 — the exchange's own settlement state. It settles markets itself and pays
    #     out automatically; there is no oracle vote to watch and nothing to redeem.
    if venue_status == "MARKET_STATUS_RESOLVED":
        checks.append(_check(
            "oracle", "Settlement status", "ok",
            "Settled by the exchange. Payouts are automatic."))
    elif venue_status == "MARKET_STATUS_RESOLVING":
        checks.append(_check(
            "oracle", "Settlement status", "watch",
            "Past expiry: the exchange has not published the final settlement "
            "yet, so the price you see is not an outcome."))
    else:
        checks.append(_check(
            "oracle", "Settlement status", "unknown",
            "Nothing to report until trading ends.", resolved=False))

    # 6 — is it still accepting orders?
    accepting = accepting_orders(raw)
    if accepting is None:
        checks.append(_check(
            "accepting", "Accepting orders", "unknown",
            "Polymarket did not report this market's status.", resolved=False))
    else:
        checks.append(_check(
            "accepting", "Accepting orders",
            "ok" if accepting else "watch",
            "The exchange is accepting orders." if accepting else
            "Orders are NOT being accepted right now, so any price you see is "
            "historical, not tradable."))

    # 7 — spread sanity. A very wide spread means the printed probability is
    #     an opinion, not a price.
    spread = s.get("spread")
    if spread is None:
        checks.append(_check(
            "spread", "Tradable spread", "unknown",
            "This market has settled, so there is no spread left to judge."
            if venue_status == "MARKET_STATUS_RESOLVED" else
            "One side of the book is empty, so there is no spread to judge.",
            resolved=False))
    else:
        checks.append(_check(
            "spread", "Tradable spread",
            "ok" if spread <= 3 else ("watch" if spread <= 10 else "risk"),
            f"{spread:.1f}¢ wide." + (
                "" if spread <= 3 else
                " At that width the mid is not a price anyone can actually "
                "trade at.")))

    # 8 and 9 read the book: the venue publishes no liquidity or volume figure
    # anywhere else. Skipped on a resolved market, which has no book to read.
    b = (await book(str(condition_id))
         if venue_status != "MARKET_STATUS_RESOLVED" else {"available": False})
    stats = b.get("stats") or {}

    # 8 — real liquidity behind the quote
    if b.get("available"):
        depth = (b.get("bidDepthUsd") or 0.0) + (b.get("askDepthUsd") or 0.0)
        checks.append(_check(
            "liquidity", "Liquidity present",
            "ok" if depth >= 20000 else ("watch" if depth >= 2000 else "risk"),
            f"${depth:,.0f} resting in the book." + (
                "" if depth >= 20000 else
                " Thin books move on small orders, and exiting can cost more "
                "than entering.")))
    else:
        checks.append(_check(
            "liquidity", "Liquidity present", "unknown",
            "This market has settled, so no book is left to measure."
            if venue_status == "MARKET_STATUS_RESOLVED" else
            "The book could not be read, so its depth is unknown.",
            resolved=False))

    # 9 — has anyone traded it recently? The venue gives the time of the last
    #     trade and a lifetime total, never a 24h figure.
    since = _hours_to(stats.get("lastTradeAt"))
    if since is None:
        checks.append(_check(
            "activity", "Recent activity", "unknown",
            "This market has settled, so there is no trading left to measure."
            if venue_status == "MARKET_STATUS_RESOLVED" else
            "Polymarket did not report a last trade for this market, so this "
            "is unknown, not zero.", resolved=False))
    else:
        ago = abs(since)
        total = stats.get("notionalTradedUsd")
        checks.append(_check(
            "activity", "Recent activity",
            "ok" if ago <= 24 else ("watch" if ago <= 168 else "risk"),
            f"Last traded {ago:.1f}h ago."
            + (f" ${total:,.0f} traded in total." if total is not None else "")))

    # 10 — does a sibling market in the same event contradict this one?
    checks.append(await _sibling_check(s))

    resolved = [c for c in checks if c["resolved"]]
    total_checks = len(checks)
    if len(resolved) >= MIN_RESOLVED_CHECKS:
        score = round(sum(_LEVEL_SCORE.get(c["level"], 50.0) for c in resolved)
                      / len(resolved), 1)
        note = (f"averaged over the {len(resolved)} of {total_checks} checks that "
                f"resolved")
    else:
        score = None
        note = (f"only {len(resolved)} of {total_checks} checks resolved — fewer than "
                f"the {MIN_RESOLVED_CHECKS} needed for a score to mean "
                f"anything, so none is shown")

    return {
        "conditionId": condition_id,
        "checks": checks,
        "resolvedCount": len(resolved),
        "totalCount": total_checks,
        "score": score,
        "scoreNote": note,
        # The exchange itself settles every market; it publishes no named resolver.
        "resolverName": None,
        "resolutionSource": res_source or None,
        "fetchedAt": _now_iso(),
    }


# slug -> the event it was verified to belong to. A market read by slug carries
# no link to its event, so this is learned, then kept for the process's life.
_EVENT_OF_MAX = 2000
_event_of: dict[str, str] = {}


def _event_slug_candidates(slug: str) -> list[str]:
    """Event slugs a market slug could belong to, most likely first.

    The market slug is `<type>-<event>[-<leg>]`: cpc-btc-updown-15m-2026-11-05-0030z
    in btc-updown-15m-2026-11-05-0030z, tec-mls-winner-2026-11-07-vwh in
    mls-winner-2026-11-07. It is a shape seen in every event sampled, not a
    documented rule, so a candidate only counts once the event says it holds the
    market.
    """
    body = slug.split("-")[1:]
    return ["-".join(body[:len(body) - cut]) for cut in range(3) if len(body) - cut >= 2]


async def _resolve_event_slug(s: dict) -> Optional[str]:
    if s.get("eventSlug"):
        return s["eventSlug"]
    slug = str(s.get("conditionId") or "")
    if slug in _event_of:
        return _event_of[slug]
    for cand in _event_slug_candidates(slug):
        data, _ = await _fetch("gateway", f"/v1/events/slug/{cand}", ttl=TTL_EVENT)
        ev = data.get("event") if isinstance(data, dict) else None
        if isinstance(ev, dict) and any(
                isinstance(m, dict) and m.get("slug") == slug for m in ev.get("markets") or []):
            if len(_event_of) >= _EVENT_OF_MAX:
                for k in list(_event_of)[: _EVENT_OF_MAX // 3]:
                    _event_of.pop(k, None)
            _event_of[slug] = cand
            return cand
    return None


async def _event_markets(event_slug: Optional[str]) -> list[dict]:
    if not event_slug or not us.valid_slug(event_slug):
        return []
    data, observed = await _fetch("gateway", f"/v1/events/slug/{event_slug}", ttl=TTL_EVENT)
    ev = data.get("event") if isinstance(data, dict) else None
    if not isinstance(ev, dict):
        return []
    out = []
    for raw in ev.get("markets") or []:
        if isinstance(raw, dict) and raw.get("hidden"):
            continue
        s = summary_from_gamma(raw, observed, event_slug=event_slug)
        if s:
            out.append(s)
    return out


# How alike two titles must be before a price gap between them can be called a
# contradiction. The venue has no flag for mutually exclusive legs, and "Will
# Alice win the 2026 Alaska Senate election?" and "Will Bob win ..." share 78% of
# their words while being supposed to disagree. Only a near-verbatim repeat of a
# question is evidence that one of two prices is wrong.
_SAME_QUESTION_OVERLAP = 0.9


async def _sibling_check(s: dict) -> dict:
    event_slug = await _resolve_event_slug(s)
    sibs = [m for m in await _event_markets(event_slug)
            if m.get("conditionId") != s.get("conditionId")]
    if not sibs:
        return _check(
            "sibling", "No contradicting sibling", "unknown",
            "No other markets found in this event to cross-check against.",
            resolved=False)
    mine = s.get("probability")
    if mine is None:
        return _check(
            "sibling", "No contradicting sibling", "unknown",
            "This market has no price, so there is nothing to contradict.",
            resolved=False)
    # A sibling asking a near-identical question at a very different price is
    # the single most useful contradiction signal there is.
    worst = None
    for m in sibs:
        p = m.get("probability")
        if p is None:
            continue
        if _title_overlap(s.get("title") or "", m.get("title") or "") >= _SAME_QUESTION_OVERLAP:
            gap = abs(p - mine)
            if worst is None or gap > worst[0]:
                worst = (gap, m)
    if worst is None:
        return _check(
            "sibling", "No contradicting sibling", "ok",
            f"{len(sibs)} sibling market(s) in this event, none asking a "
            "near-identical question.")
    gap, m = worst
    return _check(
        "sibling", "No contradicting sibling",
        "ok" if gap <= 5 else ("watch" if gap <= 15 else "risk"),
        f"“{m['title'][:70]}” is priced {gap:.1f}¢ away on a near-identical "
        f"question." + ("" if gap <= 5 else
                        " One of the two is wrong, and it might be this one."))


async def related(condition_id: str, limit: int = 25) -> dict:
    """The other markets in this market's event, priced.

    The resolution report already computes these to look for a contradiction
    and then throws the list away. Surfacing it is nearly free and it is the
    closest thing to cross-venue arbitrage available inside one venue: a
    sibling asking a near-identical question at a very different price means
    one of the two is wrong, and it might be the one you are about to trade.
    """
    s = await market(condition_id, include_raw=True)
    if not s:
        return {"conditionId": condition_id, "eventSlug": None, "rows": [],
                "sumProbability": None, "negRisk": None,
                "fetchedAt": _now_iso(),
                "note": "Could not read this market."}

    event_slug = await _resolve_event_slug(s)
    sibs = await _event_markets(event_slug)
    mine = s.get("probability")
    rows = []
    for m in sibs:
        if m.get("conditionId") == s.get("conditionId"):
            continue
        p = m.get("probability")
        overlap = _title_overlap(s.get("title") or "", m.get("title") or "")
        # Only a near-identical question can "contradict". Two different legs
        # of one event are SUPPOSED to disagree.
        gap = (abs(p - mine)
               if (p is not None and mine is not None
                   and overlap >= _SAME_QUESTION_OVERLAP)
               else None)
        rows.append({
            **_strip_raw(m),
            "titleOverlap": round(overlap, 2),
            "gapCents": (round(gap, 1) if gap is not None else None),
            "contradicts": bool(gap is not None and gap > 15),
        })

    rows.sort(key=lambda r: (
        r["gapCents"] is None, -(r["gapCents"] or 0.0),
        -(r.get("probability") or 0.0)))
    rows = rows[:limit]

    # Our own market IS one of the legs — and _event_markets already returns it,
    # so `sibs + [s]` counted it twice: a Fed group whose five legs really summed
    # to 100.7c was reported as 135.2c, i.e. "35c mispriced". Dedupe by market so
    # every leg counts exactly once.
    by_id: dict[str, float] = {}
    for m in list(sibs) + [s]:
        p = m.get("probability")
        if p is not None and m.get("conditionId"):
            by_id[str(m["conditionId"])] = p
    # math.fsum, not sum(): the naive sum differs by an ulp between Python
    # 3.11 and 3.12+ (compensated summation), which flips the rounding below.
    total = round(math.fsum(by_id.values()), 1) if len(by_id) >= 2 else None

    note = None
    if not rows:
        note = (
            "No other markets in this event to cross-check against."
            if event_slug else
            "Could not work out which event this market belongs to, so there "
            "is nothing to compare it with.")
    return {
        "conditionId": condition_id,
        "eventSlug": event_slug,
        "rows": rows,
        "sumProbability": total,
        # Always False here. The venue has no neg-risk; the key stays because the
        # renderer reads it.
        "negRisk": False,
        "fetchedAt": _now_iso(),
        "note": note,
    }


# Word pairs that make two otherwise-identical questions OPPOSITES rather
# than duplicates. Token overlap cannot see this on its own: "Fed will
# INCREASE rates by 25bps" and "Fed will DECREASE rates by 25bps" share 83% of
# their words and mean the reverse of each other.
_ANTONYMS = [
    ("increase", "decrease"), ("up", "down"), ("above", "below"),
    ("over", "under"), ("higher", "lower"), ("more", "less"),
    ("win", "lose"), ("yes", "no"), ("rise", "fall"), ("gain", "drop"),
    ("before", "after"), ("positive", "negative"),
]


# Direction words, by the polarity they claim. A word LIST of antonym pairs is
# too fragile for this: it had increase/decrease but not increase/cut, and a
# live cross-venue run then matched "Fed INCREASE rates by 25bps" to Kalshi's
# "CUT 25bps" with high confidence and printed a 33c spread — a completely
# fictional arbitrage on two markets that are mutually exclusive. Comparing
# POLARITY generalises the check instead of chasing pairs.
_POLARITY_UP = {
    "increase", "increases", "increased", "hike", "hikes", "raise", "raises",
    "rise", "rises", "above", "over", "higher", "more", "gain", "gains",
    "exceed", "exceeds", "up", "upward", "positive", "win", "wins", "yes",
}
_POLARITY_DOWN = {
    "decrease", "decreases", "decreased", "cut", "cuts", "lower", "lowers",
    "fall", "falls", "below", "under", "drop", "drops", "less", "fewer",
    "down", "downward", "negative", "lose", "loses",
}


def polarity(text: str) -> Optional[str]:
    """'up' | 'down' | None — the direction a question claims.

    None when the text carries BOTH directions ("Bitcoin Up or Down") or
    neither: in both cases there is no directional claim to contradict, and
    guessing one would create false opposites.
    """
    w = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    up, down = w & _POLARITY_UP, w & _POLARITY_DOWN
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return None


def _are_opposites(a: str, b: str) -> bool:
    """True when two titles differ by an opposing term.

    Caught live: the related-markets panel flagged "increase by 25 bps" as
    contradicting "decrease by 25 bps" at a 33c gap. They are mutually
    exclusive legs of one event — the gap is the market working correctly, not
    a mispricing, and calling it a contradiction is a confident wrong claim in
    exactly the panel that exists to be trusted.
    """
    # Polarity first: it is the general rule, and the pair list below only
    # catches what it misses.
    pa, pb = polarity(a), polarity(b)
    if pa and pb and pa != pb:
        return True
    wa = set(re.findall(r"[a-z0-9]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9]+", b.lower()))
    for x, y in _ANTONYMS:
        if (x in wa and y in wb) or (y in wa and x in wb):
            # Only opposites if the OTHER one does not also carry the term
            # (e.g. both titles containing "up" and "down" is not a signal).
            if not ((x in wa and y in wa) or (x in wb and y in wb)):
                return True
    return False


def _title_overlap(a: str, b: str) -> float:
    """Crude token-overlap similarity, 0..1. Good enough to spot two phrasings
    of the same question inside one event; deliberately not a fuzzy matcher.

    Returns 0 for titles that are OPPOSITES, so a mutually-exclusive pair is
    never read as two phrasings of the same question."""
    if _are_opposites(a, b):
        return 0.0
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower()) if len(w) > 2}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower()) if len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


# ────────────────────────────────────────────────────────────────────────
# Watchlist — durable, in the app's own DB
# ────────────────────────────────────────────────────────────────────────

_WATCH_KEY = "terminal:watchlist"
_WATCH_MAX = 200


def _kv_json(key: str, default: Any) -> Any:
    try:
        import db
        with db.get_db() as conn:
            v = db.kv_get(conn, key)
        if not v:
            return default
        return json.loads(v)
    except Exception:
        return default


def _kv_put(key: str, value: Any) -> None:
    import db
    with db.get_db() as conn:
        db.kv_set(conn, key, json.dumps(value))


def watchlist_list() -> list[dict]:
    entries = _kv_json(_WATCH_KEY, [])
    return entries if isinstance(entries, list) else []


def watchlist_add(condition_id: str, title: Optional[str] = None,
                  note: Optional[str] = None) -> list[dict]:
    cid = str(condition_id or "").strip()
    if not cid:
        return watchlist_list()
    entries = [e for e in watchlist_list() if e.get("conditionId") != cid]
    entries.insert(0, {
        "conditionId": cid, "title": title or None,
        "addedAt": _now_iso(), "note": note or None,
    })
    entries = entries[:_WATCH_MAX]
    _kv_put(_WATCH_KEY, entries)
    return entries


def watchlist_remove(condition_id: str) -> list[dict]:
    cid = str(condition_id or "").strip()
    entries = [e for e in watchlist_list() if e.get("conditionId") != cid]
    _kv_put(_WATCH_KEY, entries)
    return entries
