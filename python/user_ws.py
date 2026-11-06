"""Our own order events, pushed: wss://api.polymarket.us/v1/ws/private.

A match or cancel on one of our orders wakes the engine tick now rather than up
to a poll later. The tick's REST reads stay the authority on what actually filled;
this only says "look now". Signed handshake, then SUBSCRIPTION_TYPE_ORDER.

NOT yet exercised against the live socket (it needs a real key). Frames are parsed
per the docs and the official SDK types. If the feed never connects, the engines
just keep their normal poll cadence.
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from typing import Optional

import polymarket_auth as auth
import us_api as us

logger = logging.getLogger("user_ws")

WS_URL = us.WS_BASE + us.WS_PRIVATE_PATH
_HEARTBEAT_SEC = 20.0
_SILENT_SEC = 90.0
_RECONNECT_MAX = 60.0
_ORDERS_MAX = 2000


class UserFeed:
    def __init__(self) -> None:
        self.connected = False
        self.wake: Optional[asyncio.Event] = None
        self.orders: dict[str, dict] = {}            # order id -> latest order event
        self.trades: collections.deque = collections.deque(maxlen=500)
        self.events = 0
        self.last_event_at = 0.0
        self.last_error = ""
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    # --- message handling (pure, unit-tested) ---

    def handle(self, msg) -> bool:
        """Apply one frame. True when it concerned one of OUR orders (and the wake
        event was set). Everything on this socket is ours: it is account-scoped."""
        if not isinstance(msg, dict):
            return False
        update = msg.get("orderSubscriptionUpdate")
        execution = update.get("execution") if isinstance(update, dict) else None
        if not isinstance(execution, dict):
            return False
        view = us.order_view(execution.get("order"))
        oid = view["id"]
        if not oid:
            return False
        self.orders[oid] = {
            "status": view["status"], "type": execution.get("type"),
            "sizeMatched": view["size_matched"], "originalSize": view["original_size"],
            "price": view["price"], "assetId": us.token(view["market"], view["side"]),
            "at": time.time(),
        }
        if len(self.orders) > _ORDERS_MAX:
            for k in list(self.orders)[:500]:
                self.orders.pop(k, None)
        shares = us.to_float(execution.get("lastShares"))
        if shares > 0:
            self.trades.append({
                "id": execution.get("tradeId"), "orders": [oid], "status": view["status"],
                "side": view["action"].upper(), "size": shares,
                "price": us.side_price(view["side"], us.amount(execution.get("lastPx"))),
                "assetId": us.token(view["market"], view["side"]), "at": time.time(),
            })
        self.events += 1
        self.last_event_at = time.time()
        if self.wake is not None:
            self.wake.set()
        return True

    def order(self, order_id: str) -> Optional[dict]:
        return self.orders.get(str(order_id))

    def status(self) -> dict:
        return {
            "running": self.is_running(),
            "connected": self.connected,
            "events": self.events,
            "lastEventAgoSec": (time.time() - self.last_event_at) if self.last_event_at else None,
            "lastError": self.last_error,
        }

    # --- lifecycle ---

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

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
        self.connected = False

    async def _run(self) -> None:
        import websockets
        import ws_ssl
        attempt = 0
        while not self._stop:
            if not auth.credentials_present():
                await asyncio.sleep(5)
                continue
            try:
                headers = auth.auth_headers("GET", us.WS_PRIVATE_PATH)
                async with websockets.connect(
                    WS_URL, extra_headers=headers, ping_interval=20,
                    open_timeout=10, close_timeout=5, ssl=ws_ssl.client_context(),
                ) as ws:
                    await ws.send(json.dumps({"subscribe": {
                        "requestId": "orders", "subscriptionType": "SUBSCRIPTION_TYPE_ORDER"}}))
                    self.connected = True
                    self.last_error = ""
                    attempt = 0
                    last_beat = 0.0
                    last_msg = time.time()
                    while not self._stop:
                        if time.time() - last_beat >= _HEARTBEAT_SEC:
                            await ws.send(json.dumps({"heartbeat": {}}))
                            last_beat = time.time()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            if time.time() - last_msg > _SILENT_SEC:
                                break
                            continue
                        last_msg = time.time()
                        try:
                            self.handle(json.loads(raw))
                        except (ValueError, TypeError):
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — degradable by design
                self.last_error = f"{type(e).__name__}: {e}"
                logger.debug(f"user_ws: {self.last_error}")
            finally:
                self.connected = False
            if self._stop:
                break
            attempt = min(attempt + 1, 6)
            await asyncio.sleep(min(_RECONNECT_MAX, 2 ** attempt))


feed = UserFeed()
