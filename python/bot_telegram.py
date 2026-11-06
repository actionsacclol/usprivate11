"""Telegram bot — long-polling transport for remote.py.

Long polling, not webhooks, and that is the whole reason this works: a
webhook needs a public HTTPS endpoint, and the machine running this app is a
home PC behind NAT. `getUpdates` is an outbound request, so it needs no port
forwarding, no tunnel and no third-party relay.

The transport is deliberately thin. It knows how to read messages and send
text; every decision about WHO may speak and WHAT they may do lives in
remote.py, so the two bots cannot drift into different security rules.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import bot_secrets
import remote
import terminal

logger = logging.getLogger("krypt.remote.telegram")

API = "https://api.telegram.org"

# Telegram holds the request open until something arrives or this elapses, so
# a long timeout is CHEAPER than a short one: it is one idle connection rather
# than a poll every few seconds.
_LONG_POLL_SEC = 50
_HTTP_TIMEOUT = _LONG_POLL_SEC + 15

# Commands older than this are acknowledged but NOT run.
#
# Telegram queues undelivered updates for about 24 hours, and a fresh process
# starts from offset 0 — so without this, a "/flatten" sent at 2am while the PC
# was asleep would EXECUTE when the machine woke at 9am, closing positions on a
# seven-hour-old instruction the user had long since stopped meaning.
#
# Discord does not have this problem: its session lives in memory, so a restart
# re-IDENTIFYs and receives no backlog at all.
_MAX_COMMAND_AGE_SEC = 120.0

_task: Optional[asyncio.Task] = None
_stop = False
_last_error: Optional[str] = None
_username: Optional[str] = None
_offset = 0


def is_running() -> bool:
    return _task is not None and not _task.done()


def last_error() -> Optional[str]:
    return _last_error


def bot_username() -> Optional[str]:
    return _username


def has_token() -> bool:
    return bot_secrets.has_token("telegram")


async def _client():
    import polymarket_api as api
    return await api._get_client()


async def _call(method: str, params: Optional[dict] = None,
                timeout: float = 20.0) -> Optional[dict]:
    token = bot_secrets.load_token("telegram")
    if not token:
        return None
    started = time.monotonic()
    try:
        client = await _client()
        r = await client.get(f"{API}/bot{token}/{method}",
                             params=params or {}, timeout=timeout)
        data = r.json()
        terminal.note_external_call(
            "telegram", ok=True, ms=(time.monotonic() - started) * 1000)
    except Exception as e:  # noqa: BLE001
        terminal.note_external_call("telegram", ok=False)
        raise RuntimeError(f"{type(e).__name__}: {e}") from e
    if not isinstance(data, dict) or not data.get("ok"):
        desc = (data or {}).get("description") if isinstance(data, dict) else None
        raise RuntimeError(desc or "Telegram rejected the request")
    return data.get("result")


async def send(chat_id: str, text: str) -> None:
    """Outbound. Chunked because Telegram hard-caps a message at 4096 chars and
    a long position list would otherwise silently fail to send."""
    body = text or ""
    while body:
        chunk, body = body[:3900], body[3900:]
        await _call("sendMessage", {
            "chat_id": chat_id, "text": chunk,
            "disable_web_page_preview": "true",
        })


async def verify_token(token: str) -> dict:
    """Check a token before saving it, so the setup screen can fail loudly
    instead of leaving a bot that silently never connects."""
    import polymarket_api as api
    try:
        client = await api._get_client()
        r = await client.get(f"{API}/bot{(token or '').strip()}/getMe", timeout=15.0)
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Could not reach Telegram: {e}"}
    if not isinstance(data, dict) or not data.get("ok"):
        return {"ok": False,
                "message": (data or {}).get("description")
                or "Telegram rejected that token."}
    me = data.get("result") or {}
    return {"ok": True, "username": me.get("username"),
            "message": f"Connected as @{me.get('username')}."}


def is_stale(msg: dict) -> tuple[bool, float]:
    """→ (stale, age_seconds) for one Telegram message.

    A message with NO date is not assumed stale: an unknown age is unknown,
    and refusing on it would make the bot unusable against any payload shape
    that omits the field.
    """
    sent_at = float(msg.get("date") or 0)
    if not sent_at:
        return False, 0.0
    age = time.time() - sent_at
    return age > _MAX_COMMAND_AGE_SEC, age


async def process_updates(updates: list) -> None:
    """Handle one batch. Extracted from the poll loop so the staleness rule —
    the one that decides whether a seven-hour-old /flatten runs — is testable
    without spinning a network loop."""
    global _offset
    # Tell each chat ONCE per batch that its old messages were skipped. A day
    # offline can queue dozens; replying to each would be its own kind of
    # unusable.
    warned_stale: set[str] = set()

    for u in updates:
        # Advance the offset FIRST. A command that throws must not be
        # re-delivered on the next poll — a crashing /flatten would otherwise
        # retry forever.
        _offset = max(_offset, int(u.get("update_id", 0)) + 1)
        msg = u.get("message") or {}
        text = msg.get("text") or ""
        chat = msg.get("chat") or {}
        frm = msg.get("from") or {}
        chat_id = str(chat.get("id") or "")
        # The IDENTITY is the sender, never the chat. In a group the chat id
        # is shared by every member, so pairing one would authorise all of
        # them — including anyone invited later. `chat_id` stays the reply
        # target only.
        user_id = str(frm.get("id") or "")
        if not text or not chat_id or not user_id:
            continue

        # Stale-command guard. Already acknowledged above (the offset moved),
        # so it will not come back — it just does not run.
        stale, age = is_stale(msg)
        if stale:
            remote.audit("telegram", user_id, text[:60], "stale",
                         f"{age:.0f}s old — not executed")
            if chat_id not in warned_stale:
                warned_stale.add(chat_id)
                try:
                    await send(chat_id, (
                        f"Ignored a command sent {int(age // 60)} minute(s) "
                        f"ago — this app was not running then. Nothing was "
                        f"executed. Send it again if you still want it."))
                except Exception:
                    pass
            continue

        label = frm.get("username") or frm.get("first_name") or ""
        try:
            reply = await remote.handle("telegram", user_id, text, label=label)
            if reply:
                await send(chat_id, reply)
        except Exception as e:  # noqa: BLE001
            logger.debug("telegram handler failed: %s", e, exc_info=True)
            try:
                await send(chat_id, f"That failed: {type(e).__name__}")
            except Exception:
                pass


async def _poll_loop() -> None:
    global _last_error, _username
    backoff = 1.0
    while not _stop:
        try:
            if _username is None:
                me = await _call("getMe", timeout=15.0)
                _username = (me or {}).get("username")
                logger.info("telegram bot connected as @%s", _username)

            updates = await _call(
                "getUpdates",
                {"offset": _offset, "timeout": _LONG_POLL_SEC,
                 # Only message updates; we never need edits, reactions or
                 # channel posts, and not asking for them keeps the surface
                 # (and the payload) small.
                 "allowed_updates": '["message"]'},
                timeout=_HTTP_TIMEOUT)
            _last_error = None
            backoff = 1.0

            await process_updates(updates or [])
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            _last_error = str(e)[:200]
            logger.info("telegram poll error: %s", _last_error)
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)


async def start() -> bool:
    global _task, _stop, _last_error, _username
    if is_running():
        return True
    if not has_token():
        _last_error = "No bot token saved."
        return False
    _stop = False
    _username = None
    _last_error = None
    _task = asyncio.ensure_future(_poll_loop())
    remote.register_sender("telegram", send)
    return True


async def stop() -> None:
    global _task, _stop
    _stop = True
    remote.unregister_sender("telegram")
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
