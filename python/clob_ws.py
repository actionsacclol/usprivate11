"""Real-time market feed for Polymarket US.

Keeps a live best-bid/best-ask cache for observed markets. It matters more here
than on most venues: the public REST BBO and book are CDN-cached for up to 30
seconds, so without this feed the order paths price off data up to 30s old. The
feed needs a signed handshake, so it only runs once an API key is saved.

Protocol (docs api-reference/websocket/markets, and the official SDK):
  url   wss://api.polymarket.us/v1/ws/markets, X-PM-* headers on the handshake
  sub   {"subscribe": {"requestId": ..., "subscriptionType":
         "SUBSCRIPTION_TYPE_MARKET_DATA_LITE" | "SUBSCRIPTION_TYPE_TRADE",
         "marketSlugs": [...up to 100...]}}
  msgs  {"requestId", "subscriptionType", "marketDataLite": {marketSlug, bestBid,
         bestAsk, ...}} and {"trade": {marketSlug, price, quantity, tradeTime, ...}}
  beat  {"heartbeat": {}}

Checked on a real account by `npm run py:orders`. Any failure leaves
`get_quote_cents` returning None, so callers fall back to REST and behaviour is
never worse than without it.

The token interface is unchanged for callers: `<slug>#yes` and `<slug>#no` are
served from the one YES-quoted book, with NO mirrored.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Optional

import polymarket_auth as auth
import us_api as us

logger = logging.getLogger("clob_ws")

WS_URL = us.WS_BASE + us.WS_MARKETS_PATH
_HEARTBEAT_SEC = 20.0
_SILENT_SEC = 60.0             # no frame at all for this long -> cycle the socket
_STALE_AFTER = 12.0            # a book older than this is treated as missing
_RECONNECT_BASE = 1.0
_RECONNECT_MAX = 30.0
_SUB_CHUNK = 100               # the venue's per-subscription market limit
_MAX_DEAD_SUBS = 50            # expired slugs tolerated before re-opening clean
_NO_KEY_SLEEP_SEC = 5.0


# ───────── pure message parsing (unit-tested, no I/O) ─────────


def _order_cents(c) -> Optional[int]:
    """A price usable on the order path, or None: exchange orders live strictly
    inside 0..100, so 0 and 100 mean nothing rests there."""
    if c is None:
        return None
    try:
        v = int(c)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < 100 else None


def parse_lite(msg: dict) -> Optional[tuple[str, Optional[float], Optional[float]]]:
    """(slug, yes_bid, yes_ask) from a marketDataLite or full marketData frame."""
    md = msg.get("marketDataLite") or msg.get("marketData")
    if not isinstance(md, dict):
        return None
    slug = md.get("marketSlug")
    if not slug:
        return None
    if "bestBid" in md or "bestAsk" in md:
        bid, ask = us.parse_bbo(md)
    else:
        book = us.parse_book(md)
        bid = book["bids"][0][0] if book["bids"] else None
        ask = book["asks"][0][0] if book["asks"] else None
    return str(slug), bid, ask


def parse_trade(msg: dict) -> Optional[dict]:
    """A trade frame -> recorder row, or None if unusable. Trades here are
    anonymous: side and intent, no account."""
    tr = msg.get("trade")
    if not isinstance(tr, dict):
        return None
    slug = tr.get("marketSlug")
    price = us.amount(tr.get("price"))
    if not slug or price is None or not (0.0 < price < 1.0):
        return None
    size = us.amount(tr.get("quantity"))
    src = us.parse_iso_epoch(tr.get("tradeTime"))
    return {
        "asset_id": us.token(str(slug), "yes"), "price": price, "size": size,
        "side": None,
        "source_ts_ms": src * 1000 if src else None, "ingest_ts": time.time(),
        "market": str(slug), "tx": None,
    }


def subscribe_frames(slugs: list[str], kind: str, start_id: int = 0) -> list[dict]:
    frames = []
    for i in range(0, len(slugs), _SUB_CHUNK):
        frames.append({"subscribe": {
            "requestId": f"{kind.lower()}-{start_id + i}",
            "subscriptionType": kind,
            "marketSlugs": slugs[i:i + _SUB_CHUNK],
        }})
    return frames


def _iter_messages(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [m for m in data if isinstance(m, dict)]
    return []


# ───────── live feed (one resilient connection) ───────────


class ClobMarketFeed:
    _TRADES_MAX = 5000
    # Long enough to outlive the longest window a caller is still watching.
    _TOKEN_TTL = 2 * 3600.0
    _BOOKS_MAX = 256

    def __init__(self) -> None:
        # token -> {"bid": float|None, "ask": float|None, "ts": monotonic}
        self._books: dict[str, dict] = {}
        self._tokens: dict[str, float] = {}     # token -> last observed (monotonic)
        self._subscribed: set[str] = set()      # slugs on the open socket
        self._task: Optional[asyncio.Task] = None
        self._resub = asyncio.Event()
        self._stop = False
        self._trades: list[dict] = []
        self._req = 0
        self.connected = False
        self.last_error = ""

    # --- read side (sync, safe from anywhere) ---

    def get_quote_cents(self, token_id: Optional[str], max_age: float = _STALE_AFTER) -> Optional[dict]:
        """{'bid_cents','ask_cents'} for a token, or None if unknown/stale
        (caller then falls back to its REST path)."""
        if not token_id:
            return None
        b = self._books.get(str(token_id))
        if not b or (time.monotonic() - b["ts"]) > max_age:
            return None
        bid, ask = b.get("bid"), b.get("ask")
        return {
            "bid_cents": us.bid_cents(bid),
            "ask_cents": us.ask_cents(ask),
        }

    def quote_age_sec(self, token_id: Optional[str]) -> Optional[float]:
        """Seconds since the feed last heard about this token's book, or None."""
        b = self._books.get(str(token_id)) if token_id else None
        return max(0.0, time.monotonic() - b["ts"]) if b else None

    def quote_cents_if_fresh(self, token_id: Optional[str],
                             max_age: float = _STALE_AFTER) -> Optional[tuple[int, int]]:
        """(bid_cents, ask_cents) for the ORDER path: fresh, two-sided, uncrossed,
        no empty-book sentinels. None means "no opinion", and the caller pays its
        REST quote, so the feed is a strict accelerator, never a second authority."""
        q = self.get_quote_cents(token_id, max_age=max_age)
        if not q:
            return None
        bid = _order_cents(q.get("bid_cents"))
        ask = _order_cents(q.get("ask_cents"))
        if bid is None or ask is None or bid > ask:
            return None
        return bid, ask

    def _set_market(self, slug: str, yes_bid: Optional[float], yes_ask: Optional[float]) -> None:
        now = time.monotonic()
        self._books[us.token(slug, "yes")] = {"bid": yes_bid, "ask": yes_ask, "ts": now}
        no_bid, no_ask = us.quote_for_side("no", yes_bid, yes_ask)
        self._books[us.token(slug, "no")] = {"bid": no_bid, "ask": no_ask, "ts": now}

    def _dispatch(self, msg: dict) -> None:
        parsed = parse_lite(msg)
        if parsed:
            self._set_market(*parsed)
            return
        row = parse_trade(msg)
        if row:
            self._trades.append(row)
            if len(self._trades) > self._TRADES_MAX:
                del self._trades[: len(self._trades) - self._TRADES_MAX]
            return
        if msg.get("error") or msg.get("code"):
            self.last_error = str(msg)[:200]

    def drain_trades(self) -> list[dict]:
        out, self._trades = self._trades, []
        return out

    # --- subscription management ---

    def _wanted_slugs(self) -> set[str]:
        return {us.parse_token(t)[0] for t in self._tokens}

    def observe(self, *token_ids: Optional[str]) -> None:
        """Register interest; cheap to call every snapshot. Only a genuinely new
        market (or an expiry) touches the socket."""
        now = time.monotonic()
        grew = False
        for t in token_ids:
            if not t:
                continue
            t = str(t)
            if t not in self._tokens:
                grew = True
            self._tokens[t] = now
        expired = [t for t, ts in self._tokens.items() if now - ts > self._TOKEN_TTL]
        for t in expired:
            del self._tokens[t]
            self._books.pop(t, None)
        if grew or expired:
            if len(self._books) > self._BOOKS_MAX:
                for aid in list(self._books):
                    if aid not in self._tokens:
                        self._books.pop(aid, None)
            self._resub.set()

    # --- connection lifecycle ---

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.is_running():
            self._stop = False
            self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        self._resub.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _send_subs(self, ws, slugs: list[str]) -> None:
        if not slugs:
            return
        for kind in ("SUBSCRIPTION_TYPE_MARKET_DATA_LITE", "SUBSCRIPTION_TYPE_TRADE"):
            for frame in subscribe_frames(slugs, kind, self._req):
                await ws.send(json.dumps(frame))
        self._req += len(slugs)

    async def _run(self) -> None:
        import websockets  # local import: the feed is optional, keep import lazy
        import ws_ssl

        backoff = _RECONNECT_BASE
        while not self._stop:
            if not self._tokens or not auth.credentials_present():
                await asyncio.sleep(_NO_KEY_SLEEP_SEC if self._tokens else 0.5)
                continue
            try:
                headers = auth.auth_headers("GET", us.WS_MARKETS_PATH)
                async with websockets.connect(
                    WS_URL, extra_headers=headers, ping_interval=20,
                    open_timeout=10, close_timeout=5, ssl=ws_ssl.client_context(),
                ) as ws:
                    want = sorted(self._wanted_slugs())
                    self._resub.clear()
                    await self._send_subs(ws, want)
                    self._subscribed = set(want)
                    backoff = _RECONNECT_BASE
                    self.last_error = ""
                    self.connected = True
                    await self._recv_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — degradable by design
                self.last_error = f"{type(e).__name__}: {e}"
                if self.connected:
                    # One line per drop. Retries that never connect stay quiet.
                    why = f"; {type(e.__cause__).__name__}: {e.__cause__}" if e.__cause__ else ""
                    logger.warning(f"clob_ws: disconnected ({self.last_error}{why}); reconnecting")
                else:
                    logger.debug(f"clob_ws connection error: {self.last_error}")
            finally:
                self.connected = False
            if self._stop:
                break
            await asyncio.sleep(backoff)
            backoff = min(_RECONNECT_MAX, backoff * 2)

    async def _recv_loop(self, ws) -> None:
        last_beat = time.monotonic()
        last_msg = time.monotonic()
        while not self._stop:
            if self._resub.is_set():
                self._resub.clear()
                want = self._wanted_slugs()
                add = sorted(want - self._subscribed)
                await self._send_subs(ws, add)
                self._subscribed |= set(add)
                # Expired windows stay subscribed until enough pile up: resubscribing
                # is cheap on reconnect, and they send nothing once closed.
                if len(self._subscribed - want) > _MAX_DEAD_SUBS:
                    return
            now = time.monotonic()
            if now - last_beat >= _HEARTBEAT_SEC:
                await ws.send(json.dumps({"heartbeat": {}}))
                last_beat = now
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                if time.monotonic() - last_msg > _SILENT_SEC:
                    return
                continue
            last_msg = time.monotonic()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            for msg in _iter_messages(raw):
                self._dispatch(msg)


# Module singleton.
feed = ClobMarketFeed()


# ───────── order-path helpers (module level, degradable) ───────────


def quote_cents_if_fresh(token_id: Optional[str],
                         max_age: float = _STALE_AFTER) -> Optional[tuple[int, int]]:
    """See `ClobMarketFeed.quote_cents_if_fresh`. Never raises: every failure mode
    is None, and None means the caller pays its usual REST quote."""
    try:
        return feed.quote_cents_if_fresh(token_id, max_age=max_age)
    except Exception:
        return None


def warm(*token_ids: Optional[str]) -> None:
    """Subscribe to these so their book is live when an order path asks for it,
    starting the socket if it isn't running. Any failure leaves callers on REST."""
    try:
        if not any(token_ids):
            return
        feed.observe(*token_ids)
        import websockets  # noqa: F401
        feed.start()
    except Exception as e:
        logger.debug(f"clob_ws warm skipped: {e}")
