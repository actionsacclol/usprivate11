"""Discord bot — Gateway transport for remote.py.

Hand-rolled on the `websockets` dependency the CLOB feed already pulls in,
rather than adding discord.py. The reasons are practical: this needs exactly
two things from Discord (receive a DM, send a DM), discord.py is a large
dependency to freeze into an installer for that, and the codebase already
contains several hand-written websocket clients built the same way.

Why the Gateway at all: the other way to receive commands is an HTTP
interactions endpoint, which needs a public HTTPS URL. The machine running
this is a home PC behind NAT, so an outbound websocket is the only option that
works without a tunnel.

The transport is thin. Who may speak and what they may do is remote.py's job,
so this bot and the Telegram one cannot drift into different rules.

SETUP the user has to do (surfaced in the app):
  * create an application at discord.com/developers, add a Bot
  * copy the bot TOKEN
  * enable the MESSAGE CONTENT intent — without it Discord delivers empty
    message bodies and every command silently does nothing
  * DM the bot, or invite it to a private server
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import bot_secrets
import remote
import terminal

logger = logging.getLogger("krypt.remote.discord")

API = "https://discord.com/api/v10"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"

# Gateway opcodes we care about.
_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RESUME = 6
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10
_OP_HEARTBEAT_ACK = 11

# GUILD_MESSAGES(1<<9) + DIRECT_MESSAGES(1<<12) + MESSAGE_CONTENT(1<<15).
# MESSAGE_CONTENT is privileged and must ALSO be ticked in the Discord
# developer portal; without it every message arrives with an empty body.
_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)

# How long a heartbeat may go un-ACKed before we call the socket dead, as a
# multiple of the gateway's own heartbeat interval. One missed ACK can be
# timing; a whole interval past that is a link that is gone.
_ACK_GRACE = 1.5

_task: Optional[asyncio.Task] = None
_stop = False
_last_error: Optional[str] = None
_self_id: Optional[str] = None
# A live gateway SESSION, not just a live task — see is_running().
_connected = False
# chat/user id -> DM channel id. Opening a DM is a POST, so cache it.
_dm_channels: dict[str, str] = {}


def _task_alive() -> bool:
    """The supervisor loop is up. Used by start() so a momentary disconnect
    never spawns a second _run() alongside the first."""
    return _task is not None and not _task.done()


def is_running() -> bool:
    # A live TASK is not a live BOT. A half-open socket receives nothing and
    # raises nothing, so the old task-only check reported "connected" forever
    # while every command was quietly lost.
    return _task_alive() and _connected


def last_error() -> Optional[str]:
    return _last_error


def has_token() -> bool:
    return bot_secrets.has_token("discord")


async def _client():
    import polymarket_api as api
    return await api._get_client()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "KryptPolyBot (https://krypt.cc, 2.0)"}


async def verify_token(token: str) -> dict:
    """Check a token before saving it. Also the only place that can tell the
    user their token is fine but the MESSAGE CONTENT intent is not — which is
    otherwise an invisible failure."""
    t = (token or "").strip()
    if not t:
        return {"ok": False, "message": "No token given."}
    try:
        client = await _client()
        r = await client.get(f"{API}/users/@me", headers=_headers(t), timeout=15.0)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Could not reach Discord: {e}"}
    if r.status_code == 401:
        return {"ok": False, "message": "Discord rejected that token."}
    if r.status_code >= 400:
        return {"ok": False, "message": f"Discord returned {r.status_code}."}
    me = r.json() if r.content else {}
    return {"ok": True, "username": me.get("username"),
            "message": (
                f"Connected as {me.get('username')}. Make sure the MESSAGE "
                f"CONTENT intent is enabled for this bot in the Discord "
                f"developer portal, or it will receive empty messages.")}


async def _dm_channel(user_id: str) -> Optional[str]:
    if user_id in _dm_channels:
        return _dm_channels[user_id]
    token = bot_secrets.load_token("discord")
    if not token:
        return None
    client = await _client()
    r = await client.post(f"{API}/users/@me/channels", headers=_headers(token),
                          content=json.dumps({"recipient_id": str(user_id)}),
                          timeout=15.0)
    if r.status_code >= 400:
        raise RuntimeError(f"could not open a DM ({r.status_code})")
    cid = str((r.json() or {}).get("id") or "")
    if cid:
        _dm_channels[user_id] = cid
    return cid or None


async def send(user_id: str, text: str) -> None:
    """Outbound, always as a DM. Chunked to Discord's 2000-character limit —
    a long position list would otherwise be rejected wholesale."""
    token = bot_secrets.load_token("discord")
    if not token:
        return
    channel = await _dm_channel(str(user_id))
    if not channel:
        return
    client = await _client()
    body = text or ""
    while body:
        chunk, body = body[:1900], body[1900:]
        started = time.monotonic()
        r = await client.post(
            f"{API}/channels/{channel}/messages", headers=_headers(token),
            content=json.dumps({"content": chunk}), timeout=15.0)
        terminal.note_external_call(
            "discord", ok=r.status_code < 400,
            ms=(time.monotonic() - started) * 1000)


async def _send_to_channel(channel_id: str, text: str) -> None:
    """Reply where the message came from, so a command sent in a private
    server is answered there rather than in a DM the user is not looking at."""
    token = bot_secrets.load_token("discord")
    if not token:
        return
    client = await _client()
    body = text or ""
    while body:
        chunk, body = body[:1900], body[1900:]
        started = time.monotonic()
        r = await client.post(
            f"{API}/channels/{channel_id}/messages", headers=_headers(token),
            content=json.dumps({"content": chunk}), timeout=15.0)
        terminal.note_external_call(
            "discord", ok=r.status_code < 400,
            ms=(time.monotonic() - started) * 1000)


async def _heartbeat(ws, interval_ms: int, state: dict) -> None:
    """Discord closes the socket if we miss heartbeats. `seq` is shared with
    the receive loop through `state`, which is why it is a dict and not a
    local — sending a stale sequence number makes Discord invalidate the
    session.

    It is also the liveness check. A TCP connection can go half-open: sends
    keep succeeding into a socket nobody is reading and recv() simply never
    returns, so a missing ACK is the ONLY evidence the link is dead.
    """
    interval = max(0.001, interval_ms / 1000.0)
    try:
        while True:
            await asyncio.sleep(interval)
            silent = time.monotonic() - float(state.get("ackedAt") or 0.0)
            if silent > interval * _ACK_GRACE:
                raise RuntimeError(
                    f"gateway stopped ACKing heartbeats ({silent:.0f}s silent)")
            await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": state.get("seq")}))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        # Never die unobserved. Closing the socket makes the receive loop's
        # pending recv() raise, so the outer loop tears down and reconnects
        # instead of blocking on a dead link for good. `fatal` survives to
        # the outer handler so last_error() names the real cause rather than
        # the "connection closed" it produces.
        state["fatal"] = str(e)
        logger.info("discord heartbeat: %s", e)
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


def _wanted() -> bool:
    """Does the user still want this bot on?

    Checked on the reconnect path only. The supervisor stops a bot it can SEE
    running, and is_running() is now false while we are disconnected — so a
    "turn Discord off" that lands mid-reconnect would otherwise leave this
    loop retrying forever. An unreadable config means keep trying: a bad read
    must never silently switch the bot off.
    """
    try:
        import service
        from config import merge_with_defaults
        cfg = merge_with_defaults(dict(service.STATE.cfg or {}))
        return bool(cfg.get("remote_enabled")) and bool(
            cfg.get("remote_discord_enabled"))
    except Exception:  # noqa: BLE001
        return True


async def _run() -> None:
    global _last_error, _self_id, _connected
    import websockets
    import ws_ssl

    backoff = 1.0
    session_id: Optional[str] = None
    resume_url: Optional[str] = None
    state: dict = {"seq": None}

    while not _stop:
        token = bot_secrets.load_token("discord")
        if not token:
            _last_error = "No bot token saved."
            return
        url = resume_url or GATEWAY
        hb: Optional[asyncio.Task] = None
        try:
            # ping_interval=None because Discord's own op-1 heartbeat is the
            # liveness check that matters — it carries the sequence number and
            # is what Discord uses to decide we are alive. _heartbeat watches
            # its ACKs, which is the websocket-level check this replaces.
            async with websockets.connect(
                url, ping_interval=None, open_timeout=20, close_timeout=5,
                max_size=2 ** 20, ssl=ws_ssl.client_context(),
            ) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if hello.get("op") != _OP_HELLO:
                    raise RuntimeError("no HELLO from the gateway")
                interval = int((hello.get("d") or {}).get("heartbeat_interval") or 41250)
                # Seed the ACK clock at connect: the first check must measure
                # silence since the socket opened, not since 1970.
                state["ackedAt"] = time.monotonic()
                state.pop("fatal", None)
                hb = asyncio.ensure_future(_heartbeat(ws, interval, state))

                if session_id and resume_url:
                    await ws.send(json.dumps({
                        "op": _OP_RESUME,
                        "d": {"token": token, "session_id": session_id,
                              "seq": state.get("seq")},
                    }))
                else:
                    await ws.send(json.dumps({
                        "op": _OP_IDENTIFY,
                        "d": {"token": token, "intents": _INTENTS,
                              "properties": {"os": "windows", "browser": "krypt",
                                             "device": "krypt"}},
                    }))

                _last_error = None
                backoff = 1.0

                while not _stop:
                    raw = await ws.recv()
                    msg = json.loads(raw)
                    op = msg.get("op")
                    if msg.get("s") is not None:
                        state["seq"] = msg["s"]

                    if op == _OP_HEARTBEAT_ACK:
                        # The one signal that says the link is still two-way.
                        state["ackedAt"] = time.monotonic()
                        continue
                    if op == _OP_HEARTBEAT:
                        await ws.send(json.dumps(
                            {"op": _OP_HEARTBEAT, "d": state.get("seq")}))
                        continue
                    if op == _OP_RECONNECT:
                        break                    # resume on the next loop
                    if op == _OP_INVALID_SESSION:
                        # Cannot resume — drop the session and re-identify.
                        session_id, resume_url = None, None
                        state["seq"] = None
                        await asyncio.sleep(2.0)
                        break
                    if op != _OP_DISPATCH:
                        continue

                    ev = msg.get("t")
                    d = msg.get("d") or {}
                    if ev == "READY":
                        _self_id = str((d.get("user") or {}).get("id") or "")
                        session_id = d.get("session_id")
                        ru = d.get("resume_gateway_url")
                        resume_url = f"{ru}/?v=10&encoding=json" if ru else None
                        _connected = True
                        logger.info("discord bot ready as %s",
                                    (d.get("user") or {}).get("username"))
                        continue
                    if ev == "RESUMED":
                        _connected = True
                        continue
                    if ev != "MESSAGE_CREATE":
                        continue

                    author = d.get("author") or {}
                    uid = str(author.get("id") or "")
                    # Ignore ourselves and every other bot: two of these in one
                    # channel would otherwise answer each other forever.
                    if not uid or uid == _self_id or author.get("bot"):
                        continue
                    text = d.get("content") or ""
                    if not text:
                        continue
                    channel_id = str(d.get("channel_id") or "")
                    label = author.get("username") or ""
                    try:
                        reply = await remote.handle(
                            "discord", uid, text, label=label)
                        if reply and channel_id:
                            await _send_to_channel(channel_id, reply)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("discord handler failed: %s", e, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # A watchdog close surfaces here as "connection closed"; the
            # heartbeat's own reason is the one worth reporting.
            _last_error = str(state.get("fatal") or e)[:200]
            logger.info("discord gateway error: %s", _last_error)
        finally:
            _connected = False
            if hb:
                hb.cancel()
                try:
                    await hb
                except (asyncio.CancelledError, Exception):
                    pass
        if _stop or not _wanted():
            break
        await asyncio.sleep(backoff)
        backoff = min(60.0, backoff * 2)


async def start() -> bool:
    global _task, _stop, _last_error
    # _task_alive, not is_running: a supervisor that is mid-reconnect is
    # still running, and starting a second one would leave two gateways.
    if _task_alive():
        return True
    if not has_token():
        _last_error = "No bot token saved."
        return False
    try:
        import websockets  # noqa: F401
    except Exception:
        _last_error = "The websockets library is missing from this build."
        return False
    _stop = False
    _last_error = None
    _task = asyncio.ensure_future(_run())
    remote.register_sender("discord", send)
    return True


async def stop() -> None:
    global _task, _stop, _connected
    _stop = True
    _connected = False
    remote.unregister_sender("discord")
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
