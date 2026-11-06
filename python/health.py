"""Connection health: one row per connection, each with a fix the user can act on.

Every surface this app connects — the Polymarket US API key, the AI provider, the agent server and its HTTP API, Autopilot,
the phone bots, the market feeds — already reports its own state somewhere,
in its own words, on its own page. This module puts them in one list and
pairs every non-green row with the thing to do about it.

Two kinds of check, and the difference is the point:

  * **Local** checks read state this process already holds (is the listener
    up, is a token saved, did a bot connect, is a feed connected). Free,
    instant, safe to run whenever the panel opens.
  * **Network** checks prove a connection end to end: a signed CLOB read, a
    provider's free model-listing call, a loopback request to our own API.
    They run ONLY when the user clicks "Run checks" (`deep=True`), never on a
    poll and never on open: a signed read on a timer is account traffic the
    user did not ask for, and a provider call is billed or rate-limited
    against their key.

The builders here are pure: service.py gathers the state, these turn it into
rows. Every row's wording and status is testable without a socket.

Row shape (HealthRow): {id, label, status: ok|warn|fail|off, detail, fix,
action: {kind: nav|copy, ...} | null, network, tested}.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Optional

OK, WARN, FAIL, OFF = "ok", "warn", "fail", "off"

_PROVIDER_LABEL = {
    "anthropic": "Anthropic", "openai": "OpenAI", "openrouter": "OpenRouter",
    "gemini": "Gemini", "ollama": "Ollama", "lmstudio": "LM Studio",
}


def row(id: str, label: str, status: str, detail: str, fix: Optional[str] = None,
        action: Optional[dict] = None, network: bool = False,
        tested: bool = False) -> dict:
    """`network`: this check CAN touch the network (so the user knows why some
    rows wait for a click); `tested`: this result came from an end-to-end
    probe rather than local state."""
    return {"id": id, "label": label, "status": status, "detail": detail,
            "fix": fix, "action": action, "network": network, "tested": tested}


def error_text(e: BaseException) -> str:
    """An exception in words for a row's detail, never the class name alone."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "it timed out"
    if isinstance(e, (ConnectionError, OSError)) and not str(e).strip():
        return "the connection failed"
    try:
        import logscrub
        text = logscrub.scrub(str(e).strip())
    except Exception:  # noqa: BLE001
        text = ""
    return text[:140] or "an unexpected error (details are in the log)"


def nav(page: str, label: str) -> dict:
    return {"kind": "nav", "page": page, "label": label}


def copy(text: str, label: str = "Copy fix command") -> dict:
    # Only ever a command we built from our own values. Never a secret: this
    # text goes to the renderer, which never holds one.
    return {"kind": "copy", "text": text, "label": label}


def port_owner_command(port: int) -> str:
    if sys.platform == "win32":
        return f"netstat -ano | findstr :{int(port)}"
    return f"lsof -nP -iTCP:{int(port)} -sTCP:LISTEN"


# ── Polymarket wallet + CLOB credentials ────────────────────────────────────

def wallet_error_fix(err: BaseException) -> tuple[str, str]:
    """(detail, fix) for a failed signed check, by what actually went wrong."""
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    text = error_text(err)
    low = text.lower()
    if status == 401 or "401" in low or "unauthorized" in low or "invalid api key" in low:
        return ("Polymarket rejected the signed request (401).",
                "The API key was revoked or mistyped. Create a new one in the "
                "Polymarket US developer portal and save it under Settings.")
    if status == 403 or "403" in low or "forbidden" in low or "geoblock" in low \
            or "restricted" in low:
        return ("Polymarket refused the request (403).",
                "This key may not be allowed to trade from where you are, or the "
                "account may need attention. Check the account on polymarket.us.")
    if "timestamp" in low or "clock" in low or "skew" in low:
        return ("Polymarket says the request timestamp is off.",
                "Your computer clock is wrong. Turn on 'Set time automatically' in "
                "the system date & time settings, then run the check again.")
    if "not configured" in low or "no wallet" in low or "missing" in low:
        return ("No API key is saved.",
                "Add your Polymarket US API key under Settings.")
    if any(s in low for s in ("timeout", "timed out", "connect", "network",
                              "name resolution", "getaddrinfo", "ssl", "unreachable")):
        return ("Could not reach Polymarket.",
                "Check your internet connection, VPN or firewall, then run the "
                "check again.")
    return (f"The signed check failed: {text[:160]}",
            "Re-check the API key under Settings. If it persists, copy "
            "diagnostics from the Logs page.")


def check_wallet(status: dict, auth_ok: bool, *, account_live: bool,
                 probed: bool = False, probe_ok: Optional[bool] = None,
                 probe_error: Optional[BaseException] = None) -> dict:
    """`status` is polymarket_auth.credentials_status() (never secret
    material); `account_live` is config.account_live."""
    label = "Polymarket US account"
    open_wallet = nav("settings", "Open Settings")
    if status.get("metaError"):
        return row("wallet", label, FAIL,
                   f"The account settings file is unreadable: {status['metaError'][:120]}",
                   "Re-save the API key under Settings.", open_wallet)
    if not status.get("hasWalletKey"):
        if not account_live:
            # Not a fault: Paper reads Polymarket's public books and needs no key.
            return row("wallet", label, OK,
                       "No API key saved — the account is in Paper, which does not need one.",
                       "Add an API key under Settings when you want to go live.",
                       open_wallet)
        return row("wallet", label, FAIL, "No API key saved, and the account is Live.",
                   "Add your Polymarket US API key under Settings, or "
                   "switch the account back to Paper.", open_wallet)
    addr = status.get("addressPreview") or "key"
    if status.get("keyStoredUnencrypted"):
        return row("wallet", label, WARN,
                   f"{addr}: the private key is stored UNENCRYPTED on disk.",
                   "This machine has no DPAPI/keychain backend. Re-save the key once "
                   "one is available, and keep this folder private.", open_wallet,
                   network=True)
    if probed:
        if probe_ok:
            return row("wallet", label, OK,
                       f"{addr}: signed read succeeded"
                       + ("" if status.get("hasApiCreds") else " (API credentials derived)")
                       + ".", network=True, tested=True)
        detail, fix = wallet_error_fix(probe_error or RuntimeError("unknown"))
        return row("wallet", label, FAIL, detail, fix, open_wallet, network=True, tested=True)
    if not account_live:
        return row("wallet", label, OK,
                   f"{addr} saved. The account is in Paper, so nothing is signed "
                   f"with it; Run checks tests it.", network=True)
    if auth_ok:
        return row("wallet", label, OK,
                   f"{addr} connected and verified. Run checks to test it again.",
                   network=True)
    return row("wallet", label, WARN, f"{addr} saved but not verified yet.",
               "Run checks to test it end to end, or re-save it under Settings.",
               open_wallet, network=True)


# ── AI providers ────────────────────────────────────────────────────────────

def check_ai(provider: str, *, active: bool, has_key: bool, can_probe: bool,
             probed: bool = False, probe: Optional[dict] = None,
             probe_error: Optional[BaseException] = None, local: bool = False,
             idle: bool = False, needed: bool = False) -> dict:
    """`idle`: a saved key for a provider that is NOT selected, on a deep run;
    it is deliberately not contacted. `needed`: something is waiting on this
    provider right now (Autopilot on and using it); only then is a missing key
    a problem."""
    name = _PROVIDER_LABEL.get(provider, provider.title())
    rid = f"ai:{provider}"
    if local:
        label = f"{name} (on this machine)" + (" (active)" if active else "")
        if not probed:
            return row(rid, label, OK,
                       "Selected. Run checks to see whether its local server answers.",
                       network=True)
    else:
        label = f"{name} API key" + (" (active)" if active else "")
    if not has_key:
        if active and needed:
            return row(rid, label, WARN, f"No {name} key saved, and Autopilot is on.",
                       "Autopilot needs it to run. Add it under Settings -> AI.",
                       nav("settings", "Open Settings"))
        if active:
            return row(rid, label, OFF, "Not set up.",
                       "Optional: AI analysis and Autopilot use it. Add a key under "
                       "Settings -> AI when you want them.",
                       nav("settings", "Open Settings"))
        return row(rid, label, OFF, "Not configured.")
    if not can_probe:
        return row(rid, label, OK,
                   "Key saved (encrypted). This build cannot test it without a "
                   "billed call, so it is not tested here.")
    if idle:
        return row(rid, label, OK,
                   "Key saved (encrypted). Not tested: Run checks contacts only the "
                   "provider selected under Settings -> AI.")
    if not probed:
        return row(rid, label, OK, "Key saved. Run checks to test it.", network=True)
    if probe_error is not None:
        return row(rid, label, FAIL,
                   (f"Could not reach {name}: " if local else "Could not test the key: ")
                   + error_text(probe_error),
                   "Check your connection and run the check again.",
                   nav("settings", "Open Settings"), network=True, tested=True)
    probe = probe or {}
    msg = str(probe.get("message") or "")
    if probe.get("ok"):
        models = probe.get("models") or []
        extra = f" {len(models)} models available." if models else ""
        return row(rid, label, OK,
                   (msg or (f"{name} is running." if local else f"{name} accepted the key.")) + extra,
                   network=True, tested=True)
    if local:
        return row(rid, label, FAIL, msg or f"{name} didn't answer.",
                   f"Start {name} and make sure the selected model is loaded there, "
                   f"then run the check again.",
                   nav("settings", "Open Settings"), network=True, tested=True)
    if probe.get("reason") == "model":
        return row(rid, label, FAIL, msg, "Pick a model from the list under Settings -> AI.",
                   nav("settings", "Open Settings"), network=True, tested=True)
    return row(rid, label, FAIL, msg or f"{name} rejected the key.",
               f"Paste a fresh {name} key under Settings -> AI.",
               nav("settings", "Open Settings"), network=True, tested=True)


# ── MCP server and HTTP API ─────────────────────────────────────────────────

def check_mcp(st: dict) -> dict:
    """`st` is mcp_server.status() (+ `lastSeen` {client, at} from seen()).
    Local only."""
    port = st.get("port")
    label = "MCP server"
    if not st.get("enabled"):
        return row("mcp", label, OFF, "Switched off.",
                   "Turn on agent access on the AI Agents page to let agents connect.")
    if not st.get("running"):
        return row("mcp", label, FAIL,
                   st.get("lastError") or f"Not listening on 127.0.0.1:{port}.",
                   f"Another program may own port {port}. Find it with the command "
                   f"below, or pick another port on the AI Agents page.",
                   copy(port_owner_command(port or 0)))
    if not st.get("hasToken"):
        return row("mcp", label, FAIL, "Listening, but no token is saved.",
                   "Switch agent access off and on again to create one.")
    ag = st.get("agents") or []
    agents_txt = ""
    if ag:
        with_tok = sum(1 for a in ag if a.get("hasToken"))
        seen_n = sum(1 for a in ag if a.get("lastSeenAt"))
        agents_txt = (f" Agents: {len(ag)}, {with_tok} with a token, "
                      f"{seen_n} seen this session.")
    seen = st.get("lastSeen")
    if not seen:
        return row("mcp", label, WARN,
                   f"Listening on 127.0.0.1:{port}. No client has connected this session."
                   + agents_txt,
                   "Connect a client from the AI Agents page (Claude Desktop, Cursor, "
                   "Windsurf, Codex), then restart that client.")
    return row("mcp", label, OK,
               f"Listening on 127.0.0.1:{port}. Last client: {seen['client']}." + agents_txt)


def check_http(st: dict, *, probed: bool = False, probe: Optional[dict] = None) -> dict:
    label = "HTTP API"
    if not st.get("enabled"):
        return row("http", label, OFF, "Agent access is off, so the HTTP API is too.",
                   network=True)
    if not st.get("httpEnabled"):
        return row("http", label, OFF, "Switched off.",
                   "Turn on the HTTP API on the AI Agents page for scripts, n8n or LangChain.",
                   network=True)
    if not st.get("running"):
        return row("http", label, FAIL, "The listener it shares with MCP is not running.",
                   "Fix the MCP server row first.", network=True)
    if not probed:
        return row("http", label, OK,
                   f"Served at 127.0.0.1:{st.get('port')}/api/v1. Run checks to call it.",
                   network=True)
    probe = probe or {}
    if probe.get("ok"):
        return row("http", label, OK,
                   f"Answered on loopback: {probe.get('tools')} tools listed.",
                   network=True, tested=True)
    code = probe.get("status")
    if code == 401:
        return row("http", label, FAIL, "The server refused its own token (401).",
                   "Rotate the agent's token on the AI Agents page, then copy the "
                   "HTTP snippet again.", network=True, tested=True)
    if code == 403:
        return row("http", label, FAIL, "The server refused the call (403).",
                   "The agent used for the check may be switched off, or the HTTP API "
                   "was switched off a moment ago.", network=True, tested=True)
    return row("http", label, FAIL,
               f"No answer on 127.0.0.1:{st.get('port')}: {probe.get('error') or code}.",
               "A firewall or security tool may be blocking loopback connections. "
               "Allow Krypt PolyBot US, or check the port with the command below.",
               copy(port_owner_command(st.get("port") or 0)), network=True, tested=True)


def _socket_error(e: BaseException) -> str:
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, ConnectionResetError):
        return "the connection was reset"
    return error_text(e)


async def probe_http(port: int, token: str, timeout: float = 3.0) -> dict:
    """GET /api/v1/tools on our own loopback listener, with our own token.
    Hand-rolled over a socket so it needs no HTTP client and cannot be pointed
    anywhere but 127.0.0.1: the host is a constant, the port is ours."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(port)), timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "error": _socket_error(e)}
    try:
        head = (f"GET /api/v1/tools HTTP/1.1\r\nHost: 127.0.0.1:{int(port)}\r\n"
                f"Authorization: Bearer {token}\r\nUser-Agent: krypt-health-check\r\n"
                f"Connection: close\r\n\r\n")
        w.write(head.encode("latin-1"))
        await w.drain()
        raw = await asyncio.wait_for(r.read(4 * 1024 * 1024), timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "error": _socket_error(e)}
    finally:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass
    hdr, _, body = raw.partition(b"\r\n\r\n")
    try:
        code = int(hdr.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return {"ok": False, "status": None, "error": "malformed response"}
    if code != 200:
        return {"ok": False, "status": code, "error": f"HTTP {code}"}
    try:
        n = len(json.loads(body).get("tools") or [])
    except (ValueError, AttributeError):
        return {"ok": False, "status": code, "error": "unreadable body"}
    return {"ok": True, "status": code, "tools": n}


# ── Autopilot ───────────────────────────────────────────────────────────────

def check_autopilot(st: Optional[dict]) -> dict:
    """`st` is autopilot.status(cfg), or None in a build without Autopilot."""
    label = "Autopilot"
    if st is None:
        return row("autopilot", label, OFF, "Not available in this build.")
    if not st.get("enabled"):
        return row("autopilot", label, OFF, "Switched off.")
    lim = st.get("limits") or {}
    today = st.get("today") or {}
    try:
        runs_left = max(0, int(lim.get("maxRunsPerDay") or 0) - int(today.get("runs") or 0))
        tok_left = max(0, int(lim.get("dailyTokenBudget") or 0) - int(today.get("tokens") or 0))
        budget = f"{runs_left} runs and {tok_left:,} tokens left today (UTC)."
    except (TypeError, ValueError):
        budget = ""
    blocked = st.get("blockedReason")
    if blocked:
        keyish = "key" in str(blocked).lower()
        fix = ("Add the key under Settings -> AI." if keyish
               else "It resumes at 00:00 UTC, or raise the budget in the Autopilot panel.")
        return row("autopilot", label, WARN, f"Blocked: {str(blocked)[:160]}", fix,
                   nav("settings", "Open Settings") if keyish else None)
    if st.get("lastError"):
        return row("autopilot", label, WARN,
                   f"Last run failed: {str(st['lastError'])[:160]}. {budget}".strip(),
                   "Open the run log in the Autopilot panel for the step that failed.")
    nxt = st.get("nextRunAt")
    when = ("On, running now." if st.get("running")
            else f"On, next run {nxt} UTC." if nxt else "On.")
    return row("autopilot", label, OK, f"{when} {budget}".strip())


# ── Remote bots ─────────────────────────────────────────────────────────────

def check_remote(which: str, *, enabled: bool, has_token: bool, paired: bool,
                 bot: dict) -> dict:
    """`bot` is the transport's status() ({running, connected, botName,
    lastError}). Local only: it reports the connection the bot already holds."""
    name = which.title()
    rid = f"remote:{which}"
    label = f"{name} bot"
    open_remote = nav("remote", "Open Remote")
    if not enabled:
        return row(rid, label, OFF, "Switched off.")
    if not has_token:
        return row(rid, label, FAIL, "Enabled, but no bot token is saved.",
                   f"Paste the {name} bot token on the Remote page.", open_remote)
    if not paired:
        how = ("Enter your Discord user id" if which == "discord"
               else "Send the pairing code to your bot from Telegram")
        return row(rid, label, WARN, "Token saved, but not paired with your account.",
                   f"{how} on the Remote page.", open_remote)
    if bot.get("connected"):
        nm = f" as {bot['botName']}" if bot.get("botName") else ""
        return row(rid, label, OK, f"Connected{nm} and paired.")
    err = bot.get("lastError")
    if not bot.get("running"):
        return row(rid, label, FAIL, err or "Not running.",
                   "Switch it off and on again on the Remote page.", open_remote)
    return row(rid, label, WARN, err or "Connecting...",
               "If it does not connect within a minute, check the token on the "
               "Remote page" + (" and that the bot has the Message Content intent."
                                if which == "discord" else "."), open_remote)


# ── Market feeds ────────────────────────────────────────────────────────────

def check_feed(stats: Optional[dict], *, needed: bool, rid: str = "feed",
               label: str = "Polymarket US live feed") -> dict:
    """The Polymarket US market websocket. `stats`: {connected, books,
    lastMsgAgeSec} or None when the build cannot tell. `needed`: something
    that relies on live books (an engine, the terminal) is on."""
    if stats is None:
        return row(rid, label, OFF if not needed else WARN,
                   "No live-feed status in this build; prices come from REST polling.")
    if stats.get("connected"):
        age = stats.get("lastMsgAgeSec")
        if age is not None and age > 60:
            return row(rid, label, WARN,
                       f"Connected but silent for {age:.0f}s; quotes may be stale.",
                       "It reconnects by itself. If this persists, restart Krypt PolyBot US.")
        books = stats.get("books")
        return row(rid, label, OK,
                   "Connected" + (f", {books} live books." if books is not None else "."))
    if not needed:
        return row(rid, label, OFF, "Idle: nothing that needs live books is on.")
    return row(rid, label, WARN, "Not connected; prices are coming from REST polling.",
               "It retries by itself. A VPN or firewall blocking wss:// connections "
               "is the usual cause.")


def summarize(rows: list[dict]) -> dict:
    """Counts by status, for a badge."""
    out = {OK: 0, WARN: 0, FAIL: 0, OFF: 0}
    for r in rows:
        if r.get("status") in out:
            out[r["status"]] += 1
    return out
