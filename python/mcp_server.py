"""MCP server — let an AI agent (Claude, Cursor, Codex, …) drive this app.

The Model Context Protocol is how desktop AI clients call tools. This module
serves it from INSIDE the running backend, so an agent reads the same books,
places orders through the same gates and lands in the same ledgers as a click
in the app. There is no second trading path to drift out of sync.

HOW A CLIENT REACHES IT
    Two transports, one server:

    * Streamable HTTP on ``http://127.0.0.1:<mcp_port>/mcp`` for clients that
      speak it directly (Claude Code, Cursor, Codex, VS Code all do). JSON
      responses only — no SSE stream, nothing here ever pushes unprompted.
    * stdio, via ``krypt-polybot-us-backend --mcp`` (``mcp_stdio.py``): a tiny
      relay the client launches itself, for clients that only speak stdio
      (Claude Desktop). It finds this server through ``mcp/bridge.json`` and
      reads the token from the encrypted credential store, so the client's
      config file never holds a secret.
    * A plain HTTP API on the same listener (``/api/v1/tools``,
      ``/api/v1/tools/{name}``, ``/api/v1/openapi.json``) for scripts, n8n or
      LangChain. Off until ``mcp_http_enabled``; same Host/Origin/token
      checks; every call goes through mcp_tools.call like an MCP one.

    Every tool call, whichever door it came through, is streamed to the app
    as ``mcp:toolCall`` (whitelisted, scrubbed, throttled — see below).

    Both protocol eras are spoken on MCP: the initialize handshake and its
    session (2025-11-25 and earlier), and the stateless 2026-07-28 revision,
    where a client may open with ``server/discover`` and every request names
    its version in ``_meta`` (request_era).

WHO CAN TALK TO IT
    Loopback only, and only with a bearer token. Each of the user's named
    agents (mcp_agents.py) has its own token, minted on first use, stored like
    the wallet key (DPAPI → keychain → 600 file) and rotatable from the AI
    Agents page; the TOKEN decides which agent is calling. Requests carrying
    an ``Origin`` header are refused outright and ``Host`` must be a loopback
    name: that is the DNS-rebinding defence — a web page in your browser can
    reach 127.0.0.1, and without these checks it could reach your wallet.

WHAT AN AGENT CAN DO
    Off by default. With the server on, an agent can read everything, record
    forecasts and run backtests, and trade in the mode the user set
    (``mcp_trade_mode`` off | paper | live, and the agent's own mode): every
    order passes mcp_rails.vet — a forecast first, the edge after fees, caps,
    the agent's rules, the daily loss stop — and sells only what that agent
    opened. Live orders wait for the user's approval unless they turn that
    off. Further switches the user flips knowingly:

    * ``mcp_allow_live_switches`` — whole-account powers: engines on, live
      schedule arming, engine settings, flattening the wallet. Never arming a
      script live: only the user does that.
    * ``mcp_scripts_enabled`` — writing and backtesting strategy scripts.
      Agent-written code must pass script_audit.validate_agent (an allowlist),
      is saved disabled in shadow with needs_review set, and runs only after
      the user enables it; agent backtests run in a contained child process
      against an extract of the research data.

    Pausing engines is always allowed: getting out is never gated.

EVERY ACTION IS LOGGED
    Writes and refusals go to an audit log on the AI Agents page, with the
    client's name. Reads are counted per session, not logged one by one.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("krypt.mcp")

SERVER_NAME = "krypt-polybot-us"
# The built-in agent every pre-agents client is (mcp_agents.DEFAULT_ID).
DEFAULT_AGENT = "default"
# The revisions that open with an initialize handshake, newest first. A client
# asking for one of these gets it back; anything else gets our newest, and the
# client decides whether it can live with that.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# 2026-07-28 on: no handshake and no sessions. Every request names its version
# and the client's capabilities in _meta and is served on its own. We speak
# both eras: the clients most users run today still open with initialize.
MODERN_VERSIONS = ("2026-07-28",)
SUPPORTED_VERSIONS = MODERN_VERSIONS + PROTOCOL_VERSIONS
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_SERVER = "io.modelcontextprotocol/serverInfo"
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
# A modern list or discover result may be reused this long, and only under the
# same token: discover carries the agent's guide, which the user can edit
# while a client runs.
CACHE_HINT = {"ttlMs": 60_000, "cacheScope": "private"}
_CACHEABLE = ("server/discover", "tools/list", "prompts/list", "resources/list",
              "resources/templates/list")
# Methods whose name (or uri) a modern HTTP request repeats in Mcp-Name.
_NAME_FIELD = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
_B64_HEAD, _B64_TAIL = "=?base64?", "?="

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_HEADER_LINES = 100
IDLE_TIMEOUT_SEC = 120.0
AUDIT_MAX = 500
_KV_PREFIX = "mcp:"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CAPABILITIES = {"tools": {"listChanged": False}, "prompts": {"listChanged": False}}

INSTRUCTIONS = """\
You are connected to Krypt PolyBot US, a local Polymarket trading app, running
on the user's own machine with the user's own Polymarket US account.

Start with get_status: it says which of the user's agents you are, the mode
you trade in (off, paper or live — the user sets it, not you), your caps and
what is left of today, and whether the account is connected. get_my_agent has
the user's guide for you and your hard rules.

Conventions:
- A market is identified by its conditionId, which on Polymarket US is the
  market's slug (e.g. cpc-btc-updown-15m-2026-11-05-0030z). Find one with
  search_markets or discover_markets.
- Prices are CENTS from 1 to 99 (a 0.42 probability is 42). Sizes are
  shares; each pays 100¢ if its side wins and 0¢ otherwise.
- side is "yes" or "no" (the first or second outcome; for Up/Down markets,
  yes = Up). action is "buy" or "sell".
- Paper fills are walked out of the REAL order book, with real fees, into
  your own paper account (separate from the user's and the other agents').
- A null value means nobody could produce it. It is not zero, so do not
  treat it as zero.

The loop: research the market (rules text first), then record_forecast — your
own probability with a rationale — then preview_order, then place_order
citing that forecastId. A buy without a forecast is refused, and so is one
whose price does not beat your forecast by the minimum edge after fees. Your
forecasts are scored against the market's own price once markets resolve;
that record is what the user looks at before letting you trade live.

You sell and cancel only what YOU opened. Live orders usually wait for the
user's approval (get_orders approvalId). A refusal explains itself; do not
split an order to get under a cap.
"""


def instructions_for(agent_id: str = DEFAULT_AGENT) -> str:
    """The server's instructions, plus — for a named agent the user has
    customised — who it is, the user's guide and its hard rules."""
    try:
        import mcp_agents
        import mcp_rails
        cfg = _cfg()
        agent = mcp_agents.get(cfg, agent_id)
        return mcp_agents.instructions(INSTRUCTIONS, agent, mcp_rails.agent_rails(cfg, agent),
                                       mcp_rails.agent_mode(cfg, agent))
    except Exception as e:  # noqa: BLE001 — the base text is always right
        logger.debug("mcp: agent instructions failed: %s", e)
        return INSTRUCTIONS


# ── Rate limits ─────────────────────────────────────────────────────────────
# Per agent, under a shared ceiling. A call refused by its own agent's bucket
# does not count toward the shared one, so a runaway agent cannot spend the
# other agents' share.
RATE_WINDOW = 60.0
RATE_MAX = 120
RATE_GLOBAL_MAX = 480
_hits: list[float] = []
_agent_hits: dict[str, list[float]] = {}


def rate_refusal(agent_id: str = DEFAULT_AGENT) -> Optional[str]:
    now = time.monotonic()
    for k in [k for k, v in _agent_hits.items() if not v or now - v[-1] >= RATE_WINDOW]:
        del _agent_hits[k]
    mine = _agent_hits.setdefault(agent_id or DEFAULT_AGENT, [])
    mine[:] = [t for t in mine if now - t < RATE_WINDOW]
    mine.append(now)
    if len(mine) > RATE_MAX:
        return f"more than {RATE_MAX} calls in a minute from this agent"
    _hits[:] = [t for t in _hits if now - t < RATE_WINDOW]
    _hits.append(now)
    if len(_hits) > RATE_GLOBAL_MAX:
        return f"more than {RATE_GLOBAL_MAX} calls in a minute from all agents together"
    return None


# The text every rate-limit refusal starts with (mcp_tools.call writes it).
# The HTTP API maps it to 429; pinned by a test so a rewording cannot quietly
# turn a 429 into a 422.
RATE_LIMIT_TEXT = "Refused: Rate limit:"


# ────────────────────────────────────────────────────────────────────────
# Tool-call stream (`mcp:toolCall`) — what the agents are doing, live
# ────────────────────────────────────────────────────────────────────────
#
# Every call through the one dispatcher (mcp_tools.call — MCP, the HTTP API,
# Autopilot) emits one event for the AI Agents page and the Hub. The event is
# built ONLY from whitelisted pieces: the tool name (shape-checked), a
# market id (a slug shape, nothing else), numbers that pass a range check,
# enums, and labels cleaned of anything credential-shaped. Free text an AI
# wrote — a search query, a rationale, a script body, a SQL statement — never
# appears, and every string that survives goes through logscrub.looks_secret.
#
# Cost: in-process, no network, no disk. A token bucket caps the push rate so
# a looping agent cannot turn the renderer into its log; what the bucket drops
# is counted, and the next event says how many.

TOOLCALL_EVENT = "mcp:toolCall"
EVENT_BURST = 12
EVENT_RATE_PER_SEC = 4.0
SEEN_MAX = 32
AGENT_SEEN_CLIENTS = 4

# A market's id on this venue is its slug, e.g. cpc-btc-updown-15m-2026-11-05-0030z.
_CID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_LABEL_STRIP = re.compile(r"[^A-Za-z0-9 ._()/+:@-]")
# A run this long with no separator is a credential's shape (our token is 43
# urlsafe chars after its prefix; provider keys are longer), not a word.
_LONG_RUN = re.compile(r"[A-Za-z0-9_]{28,}")
# Shorter runs are a credential when they mix case and digits the way random
# base64 does; no client name or model looks like that.
_RUN16 = re.compile(r"[A-Za-z0-9_]{16,}")
# Well-known key prefixes, ours (kp_) included. Case-folded.
_KEY_PREFIX = re.compile(r"(?i)(?:^|[^a-z0-9])(?:kp_|sk[-_]|pk[-_]|rk[-_]|ghp_|gho_|xox[abpr]-|hf_|aiza)")
_SAFE_COLUMNS = ("new", "closing", "volume", "volatility", "watchlist", "crypto_updown")
_PARLAY_ACTIONS = ("generate", "status", "arm", "disarm")
_STANDING_ACTIONS = ("list", "add", "cancel")


class _Throttle:
    """Token bucket. `take()` False = drop this event; `suppressed` counts the
    drops so the next event that does go out can say so."""

    def __init__(self, burst: int = EVENT_BURST, rate: float = EVENT_RATE_PER_SEC,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.burst = burst
        self.rate = rate
        self.clock = clock
        self.tokens = float(burst)
        self.last = clock()
        self.suppressed = 0

    def take(self) -> bool:
        now = self.clock()
        self.tokens = min(float(self.burst), self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.suppressed += 1
        return False


_THROTTLE = _Throttle()
_EMIT: Optional[Callable[[str, Any], Awaitable[None]]] = None
# Clients seen this session, in memory only: who connected or called, when,
# how often, as which agent. Read by the onboarding "waiting for your agent"
# check (mcpSeen), which must not poll mcpStatus.
SEEN: dict[str, dict] = {}
# Per named agent, this session. Keyed by the agent id the TOKEN resolved to,
# so a card says which agent a client actually is, not which it claims to be.
AGENT_SEEN: dict[str, dict] = {}


def set_event_callback(fn: Optional[Callable[[str, Any], Awaitable[None]]]) -> None:
    """The running service hands us its event bus at startup. Without one
    (tests, the stdio relay) nothing is pushed."""
    global _EMIT
    _EMIT = fn


def _secretish(s: str) -> bool:
    try:
        import logscrub
        return logscrub.looks_secret(s)
    except Exception:  # noqa: BLE001
        # Fail CLOSED: with the scrubber missing (a packaging bug the selftest
        # exists to catch), a display field is blanked rather than trusted.
        return True


def _word_bad(w: str) -> bool:
    if _LONG_RUN.search(w) or _KEY_PREFIX.search(w):
        return True
    for run in _RUN16.findall(w):
        if any(c.isdigit() for c in run) and any(c.islower() for c in run) \
                and any(c.isupper() for c in run):
            return True
    # Word by word as well as whole: a known secret clipped into one word of a
    # longer label is caught as a fragment (logscrub.looks_secret).
    return _secretish(w)


def _bad(s: str) -> bool:
    return _secretish(s) or any(_word_bad(w) for w in s.split())


def safe_label(raw: Any, limit: int = 48) -> str:
    """A client or model name fit for display, or "". Agent-supplied (the MCP
    handshake's clientInfo, record_forecast's `model`, an HTTP User-Agent), so
    a label with anything credential-shaped in it is dropped whole."""
    s = _LABEL_STRIP.sub("", str(raw or "")).strip()[:limit].strip()
    if not s or _bad(s):
        return ""
    return s


def safe_condition_id(raw: Any) -> Optional[str]:
    """A market slug, or None. Anything else an agent typed where an id goes (a
    sentence) is dropped, and so is a known secret or anything credential-shaped:
    an agent echoing its token as a "conditionId" is caught here."""
    s = str(raw or "").strip()
    if not _CID_RE.match(s):
        return None
    return None if _secretish(s) else s


def safe_reason(text: Any, limit: int = 160) -> Optional[str]:
    """The first real line of a refusal, made safe for display. Refusals are
    our own sentences, but some quote the agent's input back, so each word is
    checked like a label."""
    lines = [ln.strip().lstrip("-").strip() for ln in str(text or "").splitlines()]
    lines = [ln for ln in lines if ln and ln.lower().rstrip(":") != "refused"]
    if not lines:
        return None
    first = lines[0]
    if first.lower().startswith("refused:"):
        first = first[len("refused:"):].strip()
    words = ["[redacted]" if _word_bad(w) else w for w in first.split()]
    out = " ".join(words)
    try:
        import logscrub
        out = logscrub.scrub(out)
    except Exception:  # noqa: BLE001
        return None
    if not out:
        return None
    return out if len(out) <= limit else out[:limit - 1].rstrip() + "…"


def safe_agent_name(raw: Any, limit: int = 40) -> str:
    """An agent's name fit for the stream. The USER wrote it, but it rides the
    same event as an injected model's fields: control characters stripped,
    length capped, blanked whole if it carries a known secret."""
    s = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:limit].strip()
    if not s or _secretish(s):
        return "Agent"
    return s


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _cents_ok(v: Any) -> Optional[float]:
    x = _num(v)
    return x if x is not None and 0 < x < 100 else None


def _size_ok(v: Any) -> Optional[float]:
    x = _num(v)
    return round(x, 4) if x is not None and 0 < x <= 1e7 else None


def _cents_txt(v: float) -> str:
    return f"{v:g}¢"


def _edge_txt(v: float) -> str:
    r = round(v) if abs(v) >= 1 else round(v, 1)
    return f"{'+' if v >= 0 else '−'}{abs(r):g}¢"


def summarize_call(name: str, args: Any, res: Any, mode: Optional[str]) -> dict:
    """The display fields for one call, built ONLY from whitelisted pieces.
    Pure (no I/O beyond the secret check), so the whitelist is unit-tested.
    `res` is the tool's result object (None unless the call succeeded)."""
    a = args if isinstance(args, dict) else {}
    r = res if isinstance(res, dict) else {}
    out: dict = {"summary": name.replace("_", " "), "conditionId": None,
                 "fairCents": None, "edgeCents": None, "midCents": None,
                 "side": None, "mode": None}
    cid = safe_condition_id(a.get("conditionId"))
    out["conditionId"] = cid
    tk = f" {cid[:10]}…" if cid else ""
    side = a.get("side") if a.get("side") in ("yes", "no") else None
    trade_mode = mode if mode in ("paper", "live") else None

    if name == "get_status":
        tm = ((r.get("trading") or {}).get("yourMode")
              if isinstance(r.get("trading"), dict) else None)
        out["summary"] = f"get_status · {tm}" if tm in ("off", "paper", "live") else "get_status"
    elif name == "discover_markets":
        col = a.get("column") if a.get("column") in _SAFE_COLUMNS else None
        rows = r.get("markets") if isinstance(r.get("markets"), list) else (
            r.get("rows") if isinstance(r.get("rows"), list) else None)
        out["summary"] = ("discover" + (f" {col}" if col else "")
                          + (f" · {len(rows)} markets" if rows is not None else ""))
    elif name == "search_markets":
        # The query is free text an AI wrote: never shown.
        rows = r.get("markets") if isinstance(r.get("markets"), list) else (
            r.get("results") if isinstance(r.get("results"), list) else None)
        out["summary"] = "search markets" + (f" · {len(rows)} found" if rows is not None else "")
    elif name == "record_forecast":
        fv = _cents_ok(a.get("fairValueCents"))
        out["fairCents"] = fv
        best = None
        for s, key in (("yes", "buyYes"), ("no", "buyNo")):
            leg = r.get(key)
            e = _num(leg.get("edgeCentsAfterFee")) if isinstance(leg, dict) else None
            if e is not None and abs(e) < 100 and (best is None or e > best[1]):
                best = (s, e)
        if best:
            out["side"], out["edgeCents"] = best[0], round(best[1], 2)
        # The mid the forecast was argued against, from OUR market read (not
        # the agent's input): what the scoreboard pairs it with.
        mkt = r.get("market") if isinstance(r.get("market"), dict) else {}
        out["midCents"] = _cents_ok(mkt.get("mid"))
        head = f"forecast{tk}" + (f" {_cents_txt(fv)}" if fv is not None else "")
        # A side nobody offers has no edge: said in words, never as 0¢.
        tail = (f"edge {_edge_txt(best[1])} {best[0].upper()}" if best
                else "no side offered" if r else None)
        out["summary"] = head + (f" · {tail}" if tail else "")
    elif name in ("place_order", "preview_order"):
        action = a.get("action") if a.get("action") in ("buy", "sell") else None
        size = _size_ok(a.get("size"))
        px = _cents_ok(a.get("priceCents"))
        out["side"] = side
        out["mode"] = trade_mode
        bits = [x for x in (action, f"{size:g}" if size else None,
                            side.upper() if side else None) if x]
        head = ("preview " if name == "preview_order" else "") + " ".join(bits)
        out["summary"] = f"{head}{tk}" + (f" @{_cents_txt(px)}" if px is not None else "")
        if name == "place_order" and r.get("pending"):
            aid = r.get("approvalId")
            out["summary"] += (f" · awaiting approval #{aid}"
                               if isinstance(aid, int) and not isinstance(aid, bool)
                               else " · awaiting approval")
        elif name == "place_order" and _size_ok(r.get("filledSize")) is not None:
            avg = _cents_ok(r.get("avgPriceCents"))
            out["summary"] += (f" · filled {_size_ok(r.get('filledSize')):g}"
                               + (f" @{avg:g}¢" if avg is not None else ""))
        elif name == "preview_order" and "wouldPlace" in r:
            out["summary"] += " · would place" if r.get("wouldPlace") else " · blocked"
    elif name == "close_position":
        size = _size_ok(a.get("size"))
        out["side"] = side
        out["mode"] = trade_mode
        out["summary"] = ("close" + (f" {size:g}" if size else "")
                          + (f" {side.upper()}" if side else "") + tk
                          + (" · preview" if a.get("preview_only") is True else ""))
    elif name in ("get_portfolio", "get_orders", "cancel_order", "cancel_all_orders",
                  "manage_standing_instructions"):
        out["mode"] = trade_mode if name != "get_portfolio" or a.get("mode") is None else (
            a.get("mode") if a.get("mode") in ("paper", "live") else None)
        act = a.get("action") if a.get("action") in _STANDING_ACTIONS else None
        out["summary"] = name.replace("_", " ") + (f" · {act}" if act else "")
    elif name == "parlay_schedule":
        act = a.get("action") if a.get("action") in _PARLAY_ACTIONS else None
        out["summary"] = "parlay schedule" + (f" {act}" if act else "")
    elif name.startswith("backtest_"):
        days = a.get("sinceDays")
        ok_days = isinstance(days, int) and not isinstance(days, bool) and 0 < days <= 365
        out["summary"] = name.replace("_", " ") + (f" · {days}d" if ok_days else "")
    elif name in ("get_market", "get_order_book", "get_price_history", "get_trade_tape",
                  "get_resolution_risk", "get_related_markets", "get_cross_venue"):
        out["summary"] = f"{name.replace('_', ' ')}{tk}"
    return out


def _ms() -> int:
    return int(time.time() * 1000)


def _note_seen(client: str, *, call: bool, agent_id: Optional[str] = None,
               model: Optional[str] = None) -> None:
    key = client or "unknown"
    e = SEEN.get(key)
    now = _ms()
    if e is None:
        if len(SEEN) >= SEEN_MAX:
            SEEN.pop(min(SEEN, key=lambda k: SEEN[k]["lastAt"]))
        e = SEEN[key] = {"client": key, "firstAt": now, "lastAt": now, "calls": 0,
                         "model": None, "agentId": None}
    e["lastAt"] = now
    if call:
        e["calls"] += 1
    if model:
        e["model"] = model
    try:
        import mcp_agents
        valid = bool(agent_id) and (agent_id == DEFAULT_AGENT or mcp_agents.valid_id(agent_id))
    except Exception:  # noqa: BLE001
        valid = False
    if not valid:
        return
    e["agentId"] = agent_id
    a = AGENT_SEEN.get(agent_id)
    if a is None:
        if len(AGENT_SEEN) >= SEEN_MAX:
            AGENT_SEEN.pop(min(AGENT_SEEN, key=lambda k: AGENT_SEEN[k]["lastAt"]))
        a = AGENT_SEEN[agent_id] = {"agentId": agent_id, "firstAt": now,
                                    "lastAt": now, "calls": 0, "clients": []}
    a["lastAt"] = now
    if call:
        a["calls"] += 1
    if key in a["clients"]:
        a["clients"].remove(key)
    a["clients"].insert(0, key)
    del a["clients"][AGENT_SEEN_CLIENTS:]


def seen() -> dict:
    """Clients seen this session. Memory only: no disk, no network.

    `clients[]` carries the contract's {client, agentId, at, called} and
    kryptrader's {firstAt, lastAt, calls, model}; `at` is the last time
    (epoch ms), `called` whether it called a tool rather than only
    connecting."""
    rows = []
    for e in sorted(SEEN.values(), key=lambda x: -x["lastAt"]):
        rows.append({**e, "at": e["lastAt"], "called": e["calls"] > 0})
    return {"clients": rows,
            "agents": sorted((dict(v, clients=list(v["clients"])) for v in AGENT_SEEN.values()),
                             key=lambda x: -x["lastAt"]),
            "now": _ms()}


def _event_agent(agent_id: Any) -> tuple[Optional[str], Optional[str]]:
    """(agent id, display name) for the stream. The id is ours (resolved from
    the token, shape-checked), the name the user's."""
    import mcp_agents
    aid = str(agent_id or DEFAULT_AGENT)
    if aid != DEFAULT_AGENT and not mcp_agents.valid_id(aid):
        return None, None
    try:
        a = mcp_agents.get(_cfg(), aid)
    except Exception:  # noqa: BLE001
        a = None
    return aid, (safe_agent_name(a["name"]) if a else None)


async def _publish(ev: dict) -> None:
    if _EMIT is None:
        return
    if not _THROTTLE.take():
        return
    ev["suppressed"] = _THROTTLE.suppressed
    _THROTTLE.suppressed = 0
    try:
        await _EMIT(TOOLCALL_EVENT, ev)
    except Exception:  # noqa: BLE001
        pass


def build_call_event(*, name: str, args: Any, session: dict, res: Any, outcome: str,
                     reason: Optional[str], started: float, mode: Optional[str]) -> dict:
    """One McpToolCallEvent. Pure apart from the secret checks (no I/O), so
    tests assert on it directly."""
    client = safe_label(session.get("client")) or "unknown"
    aid, aname = _event_agent(session.get("agent"))
    tool = name if _NAME_RE.match(name or "") else "unknown"
    fields = summarize_call(tool, args, res if outcome == "ok" else None, mode)
    model = safe_label(session.get("model"), 40) or None
    transport = session.get("transport")
    return {
        "v": 1, "kind": "call", "at": _ms(), "client": client,
        "transport": transport if isinstance(transport, str) and _NAME_RE.match(transport) else None,
        "model": model, "tool": tool,
        "outcome": outcome if outcome in ("ok", "refused", "error") else "error",
        "reason": safe_reason(reason) if reason else None,
        "durationMs": max(0, int((time.monotonic() - started) * 1000)),
        "agentId": aid, "agentName": aname,
        **fields,
    }


async def emit_call(*, name: str, args: Any, session: dict, res: Any, outcome: str,
                    reason: Optional[str], started: float, mode: Optional[str]) -> None:
    """Note the caller as seen and push one event. Never raises: the stream is
    a view, and a view must never break a call."""
    try:
        ev = build_call_event(name=name, args=args, session=session, res=res,
                              outcome=outcome, reason=reason, started=started, mode=mode)
        _note_seen(ev["client"], call=True, agent_id=ev["agentId"], model=ev["model"])
        await _publish(ev)
    except Exception as e:  # noqa: BLE001
        logger.debug("mcp: tool-call event skipped: %s", type(e).__name__)


async def emit_connect(session: dict) -> None:
    try:
        client = safe_label(session.get("client")) or "unknown"
        aid, aname = _event_agent(session.get("agent"))
        _note_seen(client, call=False, agent_id=aid)
        await _publish({"v": 1, "kind": "connect", "at": _ms(), "client": client,
                        "transport": None, "model": None, "tool": None,
                        "outcome": "ok", "reason": None, "durationMs": 0,
                        "summary": "connected", "conditionId": None, "fairCents": None,
                        "edgeCents": None, "midCents": None, "side": None, "mode": None,
                        "agentId": aid, "agentName": aname, "suppressed": 0})
    except Exception as e:  # noqa: BLE001
        logger.debug("mcp: connect event skipped: %s", type(e).__name__)


# ────────────────────────────────────────────────────────────────────────
# Prompts — ready-made workflows clients surface as slash commands
# ────────────────────────────────────────────────────────────────────────

PROMPTS: dict[str, dict] = {
    "analyze_market": {
        "title": "Analyze a market",
        "description": "Research one market end to end and say whether there is a trade.",
        "arguments": [{"name": "market", "description": "A conditionId, or words to search for.",
                       "required": True}],
        "text": (
            "Analyze this Polymarket market: {market}\n\n"
            "1. If it is not a conditionId, find it with search_markets.\n"
            "2. Read get_market (the rules text matters most), get_resolution_risk, "
            "get_order_book with include_micro, get_price_history (1w and 1d), "
            "get_trade_tape, get_related_markets and get_cross_venue.\n"
            "3. Give your own probability for each side with the reasoning, the "
            "evidence that would change it, and how confident you are.\n"
            "4. record_forecast with your YES probability and rationale. It "
            "returns the edge after fees at the price you could actually TRADE "
            "at (the ask to buy). Only call it a trade if the edge survives fees "
            "and spread.\n"
            "5. If it is a trade, preview_order citing the forecastId and show "
            "the ticket. Do not place anything unless I say so."),
    },
    "paper_trading_session": {
        "title": "Paper trading session",
        "description": "Scan markets, place paper trades with stops, and report.",
        "arguments": [
            {"name": "focus", "description": "Category or theme, e.g. politics, crypto, sports.",
             "required": False},
            {"name": "budget_usd", "description": "Paper dollars to deploy (default 100).",
             "required": False}],
        "text": (
            "Run a paper trading session{focus_clause}. Budget: {budget_usd} USD of "
            "your paper account.\n\n"
            "- get_status first. If yourMode is not paper, stop and tell me.\n"
            "- Use discover_markets (volume, closing, new, volatility) and "
            "search_markets to shortlist 5-10 candidates; research each the way "
            "analyze_market would and record_forecast on each.\n"
            "- Take only trades where your edge survives the spread and fees. Size "
            "each so no single market is more than a quarter of the budget.\n"
            "- For every position, add a stop-loss with "
            "manage_standing_instructions.\n"
            "- Finish with get_portfolio: a table of what you hold, why, your "
            "probability vs the price paid, and what would make you exit."),
    },
    "review_paper_performance": {
        "title": "Review paper performance",
        "description": "Grade the agent's paper record honestly.",
        "arguments": [],
        "text": (
            "Review your paper trading record. Use get_portfolio and get_orders "
            "(mode=paper), and get_my_agent for your forecast record against the "
            "market. Report realised and unrealised P&L, return on bankroll, "
            "win rate on settled positions, average entry vs settlement, fees "
            "paid, and your Brier score vs the market's. Say plainly whether there "
            "is evidence of an edge yet or whether the sample is too small to "
            "tell, and what you would change."),
    },
    "build_crypto_strategy": {
        "title": "Build a crypto strategy",
        "description": "Research the recorded corpus, write a script, backtest it.",
        "arguments": [{"name": "idea", "description": "A hypothesis to test (optional).",
                       "required": False}],
        "text": (
            "Build and test a strategy for the crypto Up/Down markets{idea_clause}.\n\n"
            "1. research_data with no sql for the inventory and schema, then SQL "
            "queries to test the hypothesis on the recorded ticks.\n"
            "2. get_script_docs, then write a script; validate_script until clean.\n"
            "3. backtest_script over the full corpus AND a recent slice. Report n, "
            "win rate, net EV per contract after fees and the t-stat; distrust "
            "anything with a small n or a result that only holds in one slice.\n"
            "4. Only if it holds up: save_script. It is saved in SHADOW and waits for "
            "me to review and enable it; only I can arm it live."),
    },
}


def list_prompts() -> list[dict]:
    return [{"name": k, "title": v["title"], "description": v["description"],
             "arguments": v["arguments"]} for k, v in PROMPTS.items()]


def get_prompt(name: str, args: dict) -> dict:
    p = PROMPTS.get(name)
    if p is None:
        raise KeyError(name)
    for a in p["arguments"]:
        if a.get("required") and not str(args.get(a["name"]) or "").strip():
            raise ValueError(f"missing argument: {a['name']}")
    focus = str(args.get("focus") or "").strip()
    idea = str(args.get("idea") or "").strip()
    text = p["text"].format(
        market=str(args.get("market") or "").strip(),
        budget_usd=str(args.get("budget_usd") or "100").strip(),
        focus_clause=f" focused on {focus}" if focus else "",
        idea_clause=f" around this idea: {idea}" if idea else "",
    )
    return {"description": p["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


# ────────────────────────────────────────────────────────────────────────
# Paths, token
# ────────────────────────────────────────────────────────────────────────


def _cred_dir() -> Path:
    import polymarket_auth as auth
    return auth._credentials_dir()


# ── Tokens: one per named agent ─────────────────────────────────────────────
# Each of the user's agents (mcp_agents) has its own bearer token in the
# encrypted store. Default's is the original `mcp.token`, so every client set
# up before named agents existed is Default, unchanged. The token a request
# carries is the ONLY thing that says which agent is calling: a client names
# itself in its handshake, and that is a label anyone can type.
#
# Decrypted tokens are cached in memory (a request would otherwise decrypt
# every agent's token to find its own). Every write goes through here and
# updates the cache in the same call.

DEFAULT_AGENT = "default"
# Keyed by (credentials dir, agent id): a different store (a factory reset,
# a test's temp dir) must never be answered from another store's cache.
_TOKENS: dict[tuple[str, str], Optional[str]] = {}


def _tkey(agent_id: str) -> tuple[str, str]:
    return (str(_cred_dir()), agent_id)


def _token_path(agent_id: str = DEFAULT_AGENT) -> Path:
    import mcp_agents
    return _cred_dir() / mcp_agents.token_file(agent_id)


def bridge_path() -> Path:
    """Where the stdio relay looks for the running server. Holds the port and
    pid only — never a token."""
    return _cred_dir().parent / "mcp" / "bridge.json"


def load_token(agent_id: str = DEFAULT_AGENT, fresh: bool = False) -> Optional[str]:
    """`fresh` skips the cache: the stdio relay is a separate, long-lived
    process, and a token minted or rotated by the app after it started has to
    reach it on its next call, not after the client is restarted."""
    key = _tkey(agent_id)
    if key in _TOKENS and not fresh:
        return _TOKENS[key]
    import polymarket_auth as auth
    try:
        p = _token_path(agent_id)
    except ValueError:
        return None
    tok: Optional[str] = None
    if p.exists():
        try:
            tok = auth._read_secret_bytes(p).decode("utf-8").strip() or None
        except Exception as e:  # noqa: BLE001
            logger.warning("mcp: could not read a token: %s", e)
            return None   # not cached: a transient read error must not stick
    _TOKENS[key] = tok
    return tok


def _new_token() -> str:
    return "kp_" + secrets.token_urlsafe(32)


def _write_token(agent_id: str, tok: str) -> None:
    import polymarket_auth as auth
    auth._write_secret_bytes(_token_path(agent_id), tok.encode("utf-8"))
    _TOKENS[_tkey(agent_id)] = tok
    try:
        import logscrub
        logscrub.add_known_secret(tok)   # never in a log line, from now on
    except Exception:  # noqa: BLE001
        pass


def ensure_token(agent_id: str = DEFAULT_AGENT) -> str:
    tok = load_token(agent_id)
    if tok:
        return tok
    tok = _new_token()
    _write_token(agent_id, tok)
    return tok


def ensure_tokens(cfg: dict) -> None:
    """A token for every agent in config, as soon as agents are on. A client
    set up from the app holds no token (the relay reads this one), so an agent
    saved a minute ago must already have it — otherwise the relay finds
    nothing and tells the user the app is closed."""
    import mcp_agents
    for a in mcp_agents.agents(cfg):
        try:
            ensure_token(a["id"])
        except Exception as e:  # noqa: BLE001 — one bad write must not stop the server
            logger.warning("mcp: could not create a token for agent %s: %s", a["id"], e)


def rotate_token(agent_id: str = DEFAULT_AGENT) -> str:
    """Mint a new token for one agent. Every client configured with its old
    one stops working — that is the point, it is what you do after a token
    leaks. The other agents' clients are not touched."""
    tok = _new_token()
    _write_token(agent_id, tok)
    for sid in [k for k, s in _sessions.items() if s.get("agent") == agent_id]:
        _sessions.pop(sid, None)
    audit("app", "rotate_token", "accepted",
          f"token rotated for agent {agent_id}; its clients disconnected")
    return tok


def delete_token(agent_id: str) -> None:
    """Forget a deleted agent's token. Never Default's: Default always
    exists, and its token is every pre-agents client's."""
    if agent_id == DEFAULT_AGENT:
        return
    try:
        p = _token_path(agent_id)
    except ValueError:
        return
    try:
        if p.exists():
            p.unlink()
    except OSError as e:
        logger.warning("mcp: could not delete a token file: %s", e)
    _TOKENS.pop(_tkey(agent_id), None)
    for sid in [k for k, s in _sessions.items() if s.get("agent") == agent_id]:
        _sessions.pop(sid, None)


def prune_tokens(cfg: dict) -> list[str]:
    """Delete the token files of agents no longer in config. Called only when
    the app saved an agent list on purpose (setConfig with pruneAgentTokens),
    never because the backend started on defaults — a token is deleted only
    when its agent is really gone."""
    import mcp_agents
    keep = {a["id"] for a in mcp_agents.agents(cfg)}
    gone = []
    try:
        for p in _cred_dir().glob("mcp_*.token"):
            aid = p.name[len("mcp_"):-len(".token")]
            if mcp_agents.valid_id(aid) and aid not in keep:
                delete_token(aid)
                gone.append(aid)
    except OSError as e:
        logger.warning("mcp: token prune failed: %s", e)
    return gone


def resolve_token(supplied: str, cfg: dict) -> Optional[dict]:
    """The agent whose token this is, or None. Only agents that exist in
    config can match — a deleted agent's token stops working at once, even
    before its file is pruned. Every candidate is compared in constant time."""
    import mcp_agents
    if not supplied:
        return None
    hit = None
    for a in mcp_agents.agents(cfg):
        tok = load_token(a["id"])
        if tok and hmac.compare_digest(supplied.encode(), tok.encode()) and hit is None:
            hit = a
    return hit


def masked_token(agent_id: str = DEFAULT_AGENT) -> Optional[str]:
    tok = load_token(agent_id)
    if not tok:
        return None
    return tok[:6] + "…" + tok[-4:]


# ────────────────────────────────────────────────────────────────────────
# Audit
# ────────────────────────────────────────────────────────────────────────


def _iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def audit(client: str, tool: str, outcome: str, detail: str = "") -> None:
    try:
        import db
        with db.get_db() as conn:
            raw = db.kv_get(conn, _KV_PREFIX + "audit")
            rows = json.loads(raw) if raw else []
            if not isinstance(rows, list):
                rows = []
            rows.insert(0, {"at": _iso(), "client": client, "tool": tool,
                            "outcome": outcome, "detail": detail[:300]})
            db.kv_set(conn, _KV_PREFIX + "audit", json.dumps(rows[:AUDIT_MAX]))
    except Exception as e:  # noqa: BLE001 — never let logging break a call
        logger.debug("mcp audit write failed: %s", e)


def audit_log(limit: int = 100) -> list[dict]:
    try:
        import db
        with db.get_db() as conn:
            raw = db.kv_get(conn, _KV_PREFIX + "audit")
        rows = json.loads(raw) if raw else []
        return rows[:max(1, int(limit))] if isinstance(rows, list) else []
    except Exception:  # noqa: BLE001
        return []


# ────────────────────────────────────────────────────────────────────────
# Sessions — who is connected, for the AI Agents page
# ────────────────────────────────────────────────────────────────────────

_sessions: dict[str, dict] = {}
_SESSION_MAX = 50
# Clients that told us they closed, and aren't connected any other way. In memory
# only. A client that dies without saying so can't be told apart from an idle one,
# since an agent is silent between tasks, so silence alone never counts as stopped.
_ended_clients: dict[str, str] = {}


def end_session(sid: str) -> None:
    """A client closed its session (DELETE; the stdio relay sends one on exit)."""
    s = _sessions.pop(sid, None)
    client = (s or {}).get("client")
    if not client or client == "unknown client":
        return
    if not any(o.get("client") == client for o in _sessions.values()):
        _ended_clients[client] = _iso()


def ended_clients() -> list[dict]:
    return [{"client": c, "at": at} for c, at in _ended_clients.items()]


def _session_for(sid: Optional[str], agent_id: str = DEFAULT_AGENT) -> dict:
    """The session a call runs in. A session belongs to the agent whose token
    opened it; a request on it with ANOTHER agent's token gets a fresh,
    anonymous session rather than the first agent's counters and model."""
    s = _sessions.get(sid or "")
    if s is not None and s.get("agent", DEFAULT_AGENT) != agent_id:
        s = None
        sid = None
    if s is None:
        s = {"id": sid or "", "client": "unknown client", "version": None,
             "protocol": None, "connectedAt": _iso(), "lastSeen": _iso(),
             "reads": 0, "writes": 0, "refused": 0, "agent": agent_id}
        if sid:
            _sessions[sid] = s
    s["lastSeen"] = _iso()
    return s


def _new_session(client_info: dict, protocol: str, agent_id: str = DEFAULT_AGENT) -> str:
    sid = secrets.token_hex(16)
    name = str((client_info or {}).get("title") or (client_info or {}).get("name")
               or "unknown client")[:60]
    _sessions[sid] = {
        "id": sid, "client": name,
        "version": str((client_info or {}).get("version") or "")[:30] or None,
        "protocol": protocol, "connectedAt": _iso(), "lastSeen": _iso(),
        "reads": 0, "writes": 0, "refused": 0, "agent": agent_id,
    }
    _ended_clients.pop(name, None)
    if len(_sessions) > _SESSION_MAX:
        oldest = sorted(_sessions.values(), key=lambda s: s["lastSeen"])
        for s in oldest[: len(_sessions) - _SESSION_MAX]:
            _sessions.pop(s["id"], None)
    return sid


# ────────────────────────────────────────────────────────────────────────
# JSON-RPC / MCP
# ────────────────────────────────────────────────────────────────────────


def _app_version() -> str:
    v = os.environ.get("KRYPT_APP_VERSION")
    if v:
        return v
    # The stdio relay is launched by the client, not by Electron, so it has no
    # version in its environment; the running app wrote it to the bridge.
    try:
        return str(json.loads(bridge_path().read_text(encoding="utf-8")).get("version") or "dev")
    except Exception:  # noqa: BLE001
        return "dev"


def _err(rid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _ok(rid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def server_info() -> dict:
    return {"name": SERVER_NAME, "title": "Krypt PolyBot US", "version": _app_version()}


def request_era(params: Any) -> tuple[Optional[str], Optional[dict]]:
    """(modern version, error) for one request's params.

    (None, None) is a legacy request: no version of its own (it lives in the
    session initialize opened), or a legacy one named. An error is the
    JSON-RPC error object to answer with: an unknown version gets the list of
    the ones we speak, so the client can retry with one."""
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or META_VERSION not in meta:
        return None, None
    v = meta[META_VERSION]
    if v in PROTOCOL_VERSIONS:
        return None, None
    if v not in MODERN_VERSIONS:
        return None, {"code": UNSUPPORTED_VERSION, "message": "Unsupported protocol version",
                      "data": {"supported": list(SUPPORTED_VERSIONS), "requested": str(v)}}
    if not isinstance(meta.get(META_CAPABILITIES), dict):
        return None, {"code": -32602, "message": f"_meta is missing {META_CAPABILITIES} "
                                                 f"(an object; {{}} declares none)"}
    return v, None


def modern_result(method: str, result: dict) -> dict:
    """A result as the modern revision shapes it: resultType on every one, who
    answered in _meta, and a cache hint on the lists."""
    out = {**result, "resultType": "complete",
           "_meta": {**(result.get("_meta") or {}), META_SERVER: server_info()}}
    if method in _CACHEABLE:
        out.update(CACHE_HINT)
    return out


def discover_result(instructions: str) -> dict:
    """server/discover: the versions we speak (both eras), what we serve, and
    the instructions initialize would have carried."""
    return modern_result("server/discover", {
        "supportedVersions": list(SUPPORTED_VERSIONS), "capabilities": CAPABILITIES,
        "instructions": instructions})


def encode_header_value(s: str) -> str:
    """A value fit for an HTTP header: as is when it is plain visible ASCII,
    else the revision's base64 form (which a value that merely looks like
    that form takes too, so it cannot be misread)."""
    plain = (bool(s) and s == s.strip()
             and all(c == "\t" or " " <= c <= "~" for c in s)
             and not (s.startswith(_B64_HEAD) and s.endswith(_B64_TAIL)))
    return s if plain else _B64_HEAD + base64.b64encode(s.encode("utf-8")).decode("ascii") + _B64_TAIL


def decode_header_value(v: str) -> Optional[str]:
    if not (v.startswith(_B64_HEAD) and v.endswith(_B64_TAIL)
            and len(v) >= len(_B64_HEAD) + len(_B64_TAIL)):
        return v
    try:
        return base64.b64decode(v[len(_B64_HEAD):-len(_B64_TAIL)], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def modern_headers(msg: dict) -> dict:
    """The headers a modern request repeats from its body over HTTP (the
    stdio relay sends these on the client's behalf)."""
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    out = {"MCP-Protocol-Version": str(meta.get(META_VERSION) or ""),
           "Mcp-Method": str(msg.get("method") or "")}
    field = _NAME_FIELD.get(str(msg.get("method")))
    if field and isinstance(params.get(field), str):
        out["Mcp-Name"] = encode_header_value(params[field])
    return out


def header_mismatch(headers: dict, msg: dict) -> Optional[str]:
    """Why a modern HTTP request's headers disagree with its body, or None.
    Something in front of us may route on the headers while we act on the
    body, so the revision has the two agree. `headers` keys are lower-case."""
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    want = [("MCP-Protocol-Version", meta.get(META_VERSION)), ("Mcp-Method", msg.get("method"))]
    field = _NAME_FIELD.get(str(msg.get("method")))
    if field:
        want.append(("Mcp-Name", params.get(field)))
    for name, value in want:
        got = headers.get(name.lower())
        if got is None:
            return f"the {name} header is missing"
        if name == "Mcp-Name":
            got = decode_header_value(got)
        if got != value:
            return f"the {name} header does not match the request body"
    return None


def _modern_session(params: dict, agent_id: str) -> dict:
    """A modern request's session, built from its own _meta and never kept:
    the revision has no sessions, and the next request may come from
    anywhere."""
    meta = params.get("_meta") or {}
    info = meta.get(META_CLIENT) if isinstance(meta.get(META_CLIENT), dict) else {}
    s = _session_for(None, agent_id)
    s["client"] = str(info.get("title") or info.get("name") or "unknown client")[:60]
    s["version"] = str(info.get("version") or "")[:30] or None
    s["protocol"] = meta.get(META_VERSION)
    _ended_clients.pop(s["client"], None)
    return s


async def handle_message(msg: Any, session_id: Optional[str],
                         agent_id: str = DEFAULT_AGENT) -> tuple[Optional[dict], Optional[str]]:
    """One JSON-RPC message → (response or None for a notification, new
    session id when this was an initialize). `agent_id` is the named agent the
    request's token belongs to (the transport resolved it)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "invalid request"), None
    method = msg.get("method")
    rid = msg.get("id")
    is_notification = "id" not in msg
    if not isinstance(method, str):
        # A response to something we sent. We never send requests, so ignore.
        return None, None
    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return (None if is_notification else _err(rid, -32602, "params must be an object")), None
    modern, bad = request_era(params)
    if bad is not None:
        return (None if is_notification else {"jsonrpc": "2.0", "id": rid, "error": bad}), None

    if method == "initialize":
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        sid = _new_session(params.get("clientInfo") or {}, version, agent_id)
        logger.info("mcp: %s connected as agent %s (protocol %s)",
                    safe_label(_sessions[sid]["client"]) or "unknown client", agent_id, version)
        # Connecting is news before any tool is called: onboarding's "waiting
        # for your agent" turns green on the handshake, not the first call.
        await emit_connect(_sessions[sid])
        return _ok(rid, {
            "protocolVersion": version,
            "capabilities": CAPABILITIES,
            "serverInfo": server_info(),
            "instructions": instructions_for(agent_id),
        }), sid

    if is_notification:
        return None, None  # notifications/initialized, cancelled, …

    def done(result: dict) -> tuple[dict, None]:
        return _ok(rid, modern_result(method, result) if modern else result), None

    if method == "server/discover":
        if not modern:
            return _err(rid, -32602, f"server/discover needs _meta with {META_VERSION} "
                                     f"and {META_CAPABILITIES}"), None
        # The modern client's first word, as initialize is a legacy one's.
        await emit_connect(_modern_session(params, agent_id))
        return _ok(rid, discover_result(instructions_for(agent_id))), None
    if method == "ping":
        return done({})
    if method == "tools/list":
        import mcp_tools
        return done({"tools": mcp_tools.list_tools()})
    if method == "tools/call":
        import mcp_tools
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            return _err(rid, -32602, "tools/call needs a name and an arguments object"), None
        if not mcp_tools.has_tool(name):
            return _err(rid, -32602, f"unknown tool: {name}"), None
        session = (_modern_session(params, agent_id) if modern
                   else _session_for(session_id, agent_id))
        # The rate limit, the rails, the audit and the tool-call stream all
        # live in the one dispatcher (the HTTP API and Autopilot use it too).
        return done(await mcp_tools.call(name, args, session))
    # Clients probe these even when we do not advertise them; an empty list is
    # a better answer than an error that some clients surface as "server
    # broken".
    if method == "resources/list":
        return done({"resources": []})
    if method == "resources/templates/list":
        return done({"resourceTemplates": []})
    if method == "prompts/list":
        return done({"prompts": list_prompts()})
    if method == "prompts/get":
        try:
            return done(get_prompt(str(params.get("name") or ""), params.get("arguments") or {}))
        except KeyError:
            return _err(rid, -32602, f"unknown prompt: {params.get('name')}"), None
        except ValueError as e:
            return _err(rid, -32602, str(e)), None
    return _err(rid, -32601, f"method not found: {method}"), None


async def handle_body(body: Any, session_id: Optional[str],
                      agent_id: str = DEFAULT_AGENT) -> tuple[Optional[Any], Optional[str]]:
    """A POST body is one message or a batch of them."""
    if isinstance(body, list):
        if not body:
            return _err(None, -32600, "empty batch"), None
        out, new_sid = [], None
        for m in body:
            r, sid = await handle_message(m, session_id, agent_id)
            new_sid = new_sid or sid
            if r is not None:
                out.append(r)
        return (out or None), new_sid
    return await handle_message(body, session_id, agent_id)


# ────────────────────────────────────────────────────────────────────────
# HTTP transport (loopback, hand-rolled on asyncio — no new dependency)
# ────────────────────────────────────────────────────────────────────────

_REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed",
            413: "Payload Too Large", 422: "Unprocessable Content",
            429: "Too Many Requests", 503: "Service Unavailable"}


class _HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def _read_request(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_SEC)
    except (asyncio.TimeoutError, ConnectionError):
        return None
    if not line:
        return None
    try:
        method, target, _ver = line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        raise _HttpError(400, "bad request line")
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADER_LINES + 1):
        h = await asyncio.wait_for(reader.readline(), 30)
        if h in (b"\r\n", b"\n", b""):
            break
        k, _, v = h.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    else:
        raise _HttpError(400, "too many headers")

    body = b""
    if headers.get("transfer-encoding", "").lower() == "chunked":
        while True:
            size_line = await asyncio.wait_for(reader.readline(), 30)
            size = int(size_line.split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            if len(body) + size > MAX_BODY_BYTES:
                raise _HttpError(413, "body too large")
            body += await asyncio.wait_for(reader.readexactly(size), 30)
            await reader.readline()
    else:
        n = int(headers.get("content-length") or 0)
        if n > MAX_BODY_BYTES:
            raise _HttpError(413, "body too large")
        if n:
            body = await asyncio.wait_for(reader.readexactly(n), 30)
    return {"method": method.upper(), "path": target.split("?", 1)[0],
            "headers": headers, "body": body}


def _host_ok(host: str) -> bool:
    h = host.strip().lower()
    if h.startswith("["):
        name = h[: h.find("]") + 1]
    else:
        name = h.split(":", 1)[0]
    return name in _LOOPBACK_HOSTS


def _authorized(headers: dict) -> Optional[dict]:
    """The named agent this request's bearer token belongs to, or None."""
    got = headers.get("authorization", "")
    if not got.lower().startswith("bearer "):
        return None
    return resolve_token(got[7:].strip(), _cfg())


def _cfg() -> dict:
    try:
        import mcp_rails
        return mcp_rails.cfg()
    except Exception:  # noqa: BLE001 — no service (tests, CLI): defaults
        from config import merge_with_defaults
        return merge_with_defaults({})


def _is_api(path: str) -> bool:
    p = path.rstrip("/")
    return p == API_PREFIX or p.startswith(API_PREFIX + "/")


def _json_resp(status: int, obj: Any, extra: Optional[dict] = None) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json", **(extra or {})}, json.dumps(
        obj, default=str, ensure_ascii=False).encode("utf-8")


async def _route(req: dict) -> tuple[int, dict, bytes]:
    h = req["headers"]
    api = _is_api(req["path"])

    def refuse(status: int, why: str, extra: Optional[dict] = None) -> tuple[int, dict, bytes]:
        # The API answers in JSON ({ok:false, error}); MCP keeps plain text.
        if api:
            return _json_resp(status, {"ok": False, "error": why}, extra)
        return status, extra or {}, why.encode("utf-8")

    # DNS rebinding / CSRF: a browser page can reach loopback. Browsers always
    # send Origin on a cross-origin POST; desktop MCP clients and scripts do
    # not. The MCP endpoint and the HTTP API pass the same checks in the same
    # order.
    if not _host_ok(h.get("host", "")):
        return refuse(403, "forbidden host")
    if h.get("origin"):
        return refuse(403, "browser requests are not accepted")
    if req["path"].rstrip("/") != "/mcp" and not api:
        return 404, {}, b"not found"
    agent = _authorized(h)
    if agent is None:
        return refuse(401, "unauthorized", {"WWW-Authenticate": 'Bearer realm="krypt-polybot-us"'})
    if not _running_wanted():
        return refuse(503, "AI agent access is turned off in Krypt PolyBot US")
    if not agent.get("enabled", True):
        return refuse(403, f"{safe_agent_name(agent['name'])} is switched off in Krypt PolyBot US")

    if api:
        status, obj, extra = await handle_api(req["method"], req["path"], req["body"], h,
                                              agent["id"])
        return _json_resp(status, obj, extra)

    if req["method"] == "DELETE":
        end_session(h.get("mcp-session-id", ""))
        return 200, {}, b""
    if req["method"] != "POST":
        # No server-initiated stream: nothing here ever pushes unprompted.
        return 405, {"Allow": "POST, DELETE"}, b"method not allowed"

    try:
        body = json.loads(req["body"].decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        out = _err(None, -32700, "parse error")
        return 200, {"Content-Type": "application/json"}, json.dumps(out).encode()

    # A modern request: its headers must repeat its body (checked before
    # anything runs), it has no session, and the revision fixes the HTTP
    # status of the errors a client tells the eras apart by.
    modern = bad = None
    if isinstance(body, dict) and "id" in body:
        modern, bad = request_era(body.get("params"))
        if bad is None and (modern or h.get("mcp-protocol-version") in MODERN_VERSIONS):
            why = header_mismatch(h, body)
            if why:
                out = _err(body.get("id"), HEADER_MISMATCH, f"Header mismatch: {why}")
                return 400, {"Content-Type": "application/json"}, json.dumps(out).encode()

    resp, new_sid = await handle_body(body, None if modern else h.get("mcp-session-id"),
                                      agent["id"])
    extra = {"Mcp-Session-Id": new_sid} if new_sid else {}
    if resp is None:
        return 202, extra, b""
    status = 200
    if bad is not None:
        status = 400
    elif modern and isinstance(resp, dict) and (resp.get("error") or {}).get("code") == -32601:
        status = 404
    data = json.dumps(resp, default=str, ensure_ascii=False).encode("utf-8")
    return status, {"Content-Type": "application/json", **extra}, data


# ────────────────────────────────────────────────────────────────────────
# Plain HTTP API (/api/v1) — for callers that do not speak MCP
# ────────────────────────────────────────────────────────────────────────
# Deliberately thin: it parses a request, hands the tool name and arguments to
# mcp_tools.call — the one dispatcher MCP and Autopilot use — and maps the
# result onto an HTTP status. Nothing here decides whether an order may
# happen; if it did, the two doors could disagree.

API_PREFIX = "/api/v1"
_TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_UA_CLEAN = re.compile(r"[^A-Za-z0-9 ._/()+:;,-]")


def http_client_name(user_agent: Optional[str]) -> str:
    """How an HTTP caller appears in the audit and the stream:
    `http:<user-agent>`. Self-declared, exactly like an MCP client's
    clientInfo, so it is a LABEL, never a security boundary. Cleaned and
    capped so a hostile header cannot forge log lines."""
    ua = _UA_CLEAN.sub("", str(user_agent or "")).strip()
    ua = re.sub(r"\s+", " ", ua)[:60].strip()
    return f"http:{ua or 'unknown'}"


def _api_err(status: int, msg: str, tool: Optional[str] = None,
             extra: Optional[dict] = None) -> tuple[int, dict, Optional[dict]]:
    body: dict = {"ok": False, "error": msg}
    if tool:
        body["tool"] = tool
    return status, body, extra


def _result_body(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _http_off_message() -> str:
    return ("The HTTP API is switched off. Turn it on in Krypt PolyBot US -> AI "
            "Agents -> HTTP API.")


async def handle_api(method: str, path: str, body: bytes, headers: dict,
                     agent_id: str = DEFAULT_AGENT) -> tuple[int, Any, Optional[dict]]:
    """One authenticated /api/v1 request -> (status, JSON body, extra headers).
    Host, Origin and token were already checked by _route, and the token named
    `agent_id` — the agent every call here runs as."""
    import mcp_rails
    import mcp_tools
    cfg = _cfg()
    if not cfg.get("mcp_http_enabled"):
        return _api_err(403, _http_off_message())
    route = path.split("?", 1)[0][len(API_PREFIX):].rstrip("/")

    if route == "/tools":
        if method != "GET":
            return _api_err(405, "GET only", extra={"Allow": "GET"})
        import mcp_agents
        agent = mcp_agents.get(cfg, agent_id)
        return 200, {"tools": mcp_tools.list_tools(),
                     "tradeMode": mcp_rails.agent_mode(cfg, agent),
                     "agent": ({"id": agent["id"], "name": agent["name"]} if agent else None),
                     "instructions": instructions_for(agent_id)}, None

    if route == "/openapi.json":
        if method != "GET":
            return _api_err(405, "GET only", extra={"Allow": "GET"})
        return 200, openapi_spec(cfg, _port or int(cfg.get("mcp_port") or 47635),
                                 instructions_for(agent_id)), None

    if route.startswith("/tools/"):
        name = route[len("/tools/"):]
        if not _TOOL_NAME_RE.fullmatch(name) or not mcp_tools.has_tool(name):
            return _api_err(404, f"Unknown tool: {name[:64]}")
        if method != "POST":
            return _api_err(405, "POST only", name, {"Allow": "POST"})
        try:
            args = json.loads(body) if body.strip() else {}
        except (ValueError, UnicodeDecodeError):
            return _api_err(400, "The body must be a JSON object of tool arguments.", name)
        if not isinstance(args, dict):
            return _api_err(400, "The body must be a JSON object of tool arguments.", name)
        session = {"client": http_client_name(headers.get("user-agent")),
                   "transport": "http", "agent": agent_id,
                   "reads": 0, "writes": 0, "refused": 0}
        # THE dispatcher: rate limit, trade mode, the forecast gate, caps,
        # approvals, the loss stop, the audit and the stream all live behind
        # this call, for HTTP exactly as for MCP.
        res = await mcp_tools.call(name, args, session)
        text = res["content"][0]["text"] if res.get("content") else ""
        if not res.get("isError"):
            return 200, {"ok": True, "tool": name, "result": _result_body(text)}, None
        if text.startswith(RATE_LIMIT_TEXT):
            return _api_err(429, text, name, {"Retry-After": str(int(RATE_WINDOW))})
        # Refused by a rail (or failed): the same words the MCP client reads.
        # A failed action may carry a structured result; keep it.
        out = _result_body(text)
        if isinstance(out, dict):
            return 422, {"ok": False, "tool": name,
                         "error": str(out.get("message") or out.get("error") or "refused"),
                         "result": out}, None
        return _api_err(422, text, name)

    return _api_err(404, "not found")


_API_ERROR_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean", "const": False},
                   "error": {"type": "string",
                             "description": "Why it was refused, in the same words an MCP client gets."},
                   "tool": {"type": "string"}},
    "required": ["ok", "error"],
}


def openapi_spec(cfg: dict, port: int, instructions: Optional[str] = None) -> dict:
    """OpenAPI 3.1, built from the SAME tool registry tools/list serves."""
    import mcp_tools

    def _err(desc: str) -> dict:
        return {"description": desc,
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}

    common = {
        "401": _err("Missing or wrong bearer token."),
        "403": _err("Foreign Host/Origin, the agent is switched off, or the HTTP API is off."),
        "404": _err("Unknown tool."),
        "422": _err("Refused by a rail (trade mode, forecast gate, caps, loss stop, "
                    "permissions...). Read `error` and act on it."),
        "429": _err("Rate limited. Nothing was executed; see Retry-After."),
    }
    paths: dict = {
        f"{API_PREFIX}/tools": {"get": {
            "operationId": "list_tools",
            "summary": "List the tools",
            "description": "Names, descriptions and JSON input schemas, the mode you "
                           "trade in, and which of the user's agents your token is.",
            "responses": {"200": {"description": "Tool list",
                                  "content": {"application/json": {"schema": {"type": "object"}}}},
                          "401": common["401"], "403": common["403"]},
        }},
    }
    for t in mcp_tools.list_tools():
        schema = t["inputSchema"]
        paths[f"{API_PREFIX}/tools/{t['name']}"] = {"post": {
            "operationId": t["name"],
            "summary": t["title"],
            "description": t["description"],
            "requestBody": {"required": bool(schema.get("required")),
                            "content": {"application/json": {"schema": schema}}},
            "responses": {
                "200": {"description": "The tool's result.",
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean", "const": True},
                                           "tool": {"type": "string"},
                                           "result": {}},
                            "required": ["ok", "result"]}}}},
                **common,
            },
            "x-krypt-read-only": t["annotations"]["readOnlyHint"],
            "x-krypt-destructive": t["annotations"]["destructiveHint"],
        }}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Krypt PolyBot US agent API",
            "version": _app_version(),
            "description": ((instructions or INSTRUCTIONS) + "\n\nEvery call runs through "
                            "the same rails as the MCP server: trade mode, a forecast "
                            "before every buy, minimum edge after fees, caps, approvals "
                            "and the daily loss stop. Loopback only."),
        },
        # Our own port on loopback; never a host taken from the request.
        "servers": [{"url": f"http://127.0.0.1:{int(port)}"}],
        "security": [{"bearer": []}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": {"Error": _API_ERROR_SCHEMA},
        },
        "paths": paths,
    }


# A template rather than an f-string: the Python half is itself full of braces.
_HTTP_SNIPPET = """\
# Krypt PolyBot US HTTP API - loopback only. The token below is a password: it
# IS this agent (__AGENT__). Every call runs through the same rails as MCP:
# trade mode, a forecast before every buy, minimum edge after fees, caps,
# approvals and the daily loss stop. Turn the HTTP API on under AI Agents.

# --- curl (in Windows PowerShell type curl.exe, not curl) ---
curl -s -H "Authorization: Bearer __TOKEN__" __BASE__/tools
curl -s -X POST -H "Authorization: Bearer __TOKEN__" -H "Content-Type: application/json" -d "{}" __BASE__/tools/get_status
# OpenAPI document, for LangChain / n8n import:
curl -s -H "Authorization: Bearer __TOKEN__" -o krypt-polybot-us-openapi.json __BASE__/openapi.json

# --- Python (pip install requests) ---
import requests

KRYPT = "__BASE__"
HEADERS = {"Authorization": "Bearer __TOKEN__", "User-Agent": "my-bot/1.0"}


def call(tool, **args):
    r = requests.post(f"{KRYPT}/tools/{tool}", json=args, headers=HEADERS, timeout=120)
    body = r.json()
    if not body.get("ok"):
        # A refusal (no forecast, over a cap, trading off...) arrives here with
        # the reason in plain words. Read it; do not retry blindly.
        raise RuntimeError(f"{tool} -> HTTP {r.status_code}: {body.get('error')}")
    return body["result"]


print(call("get_status"))
"""


def http_snippet(port: int, token: str, agent_name: str = "Default") -> str:
    base = f"http://127.0.0.1:{int(port)}{API_PREFIX}"
    name = safe_agent_name(agent_name).replace("\n", " ")
    return (_HTTP_SNIPPET.replace("__BASE__", base).replace("__AGENT__", name)
            .replace("__TOKEN__", token))


async def _serve_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            try:
                req = await _read_request(reader)
            except _HttpError as e:
                await _write(writer, e.status, {}, str(e).encode(), close=True)
                return
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
                return
            if req is None:
                return
            try:
                status, headers, body = await _route(req)
            except Exception as e:  # noqa: BLE001
                logger.warning("mcp: request failed: %s", e, exc_info=True)
                status, headers, body = 200, {"Content-Type": "application/json"}, json.dumps(
                    _err(None, -32603, f"internal error: {type(e).__name__}")).encode()
            close = req["headers"].get("connection", "").lower() == "close"
            await _write(writer, status, headers, body, close=close)
            if close:
                return
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _write(writer: asyncio.StreamWriter, status: int, headers: dict,
                 body: bytes, *, close: bool) -> None:
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
    hdrs = {"Content-Length": str(len(body)), "Cache-Control": "no-store",
            "Connection": "close" if close else "keep-alive", **headers}
    hdrs.setdefault("Content-Type", "text/plain; charset=utf-8")
    lines += [f"{k}: {v}" for k, v in hdrs.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()


# ────────────────────────────────────────────────────────────────────────
# Lifecycle
# ────────────────────────────────────────────────────────────────────────

_server: Optional[asyncio.base_events.Server] = None
_port: Optional[int] = None
_last_error: Optional[str] = None
_want = False


def _running_wanted() -> bool:
    return _want


def is_running() -> bool:
    return _server is not None


def _write_bridge(port: int) -> None:
    p = bridge_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"port": port, "pid": os.getpid(),
                               "version": os.environ.get("KRYPT_APP_VERSION") or "dev",
                               "startedAt": _iso()}), encoding="utf-8")
    os.replace(tmp, p)


def _clear_bridge() -> None:
    try:
        p = bridge_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            # Only remove OUR file: a second backend (shouldn't exist, but the
            # instance lock is per wallet) must not lose its bridge to us.
            if int(data.get("pid") or 0) == os.getpid():
                p.unlink()
    except Exception:  # noqa: BLE001
        pass


async def start(port: int) -> None:
    global _server, _port, _last_error
    if _server is not None and _port == port:
        return
    await stop()
    ensure_token()
    try:
        _server = await asyncio.start_server(_serve_conn, host="127.0.0.1", port=port)
    except OSError as e:
        _server = None
        _last_error = (f"Could not listen on 127.0.0.1:{port} — {e.strerror or e}. "
                       f"Another program is using that port; pick a different "
                       f"one on the AI Agents page.")
        logger.warning("mcp: %s", _last_error)
        return
    _port = port
    _last_error = None
    _write_bridge(port)
    logger.info("mcp: listening on http://127.0.0.1:%d/mcp", port)


async def stop() -> None:
    global _server, _port
    srv, _server = _server, None
    if srv is not None:
        srv.close()
        try:
            await asyncio.wait_for(srv.wait_closed(), 2.0)
        except Exception:  # noqa: BLE001
            pass
        logger.info("mcp: stopped")
    _port = None
    _sessions.clear()
    _clear_bridge()


async def sync(cfg: dict) -> dict:
    """Start or stop to match config. Idempotent; called on every config
    change and on the master loop's timer, so a port change or a server that
    failed to bind is retried without a restart."""
    global _want
    _want = bool(cfg.get("mcp_enabled"))
    if _want:
        await start(int(cfg.get("mcp_port") or 47635))
        ensure_tokens(cfg)
    else:
        await stop()
    return status(cfg)


def agents_status(cfg: dict) -> list[dict]:
    """One row per named agent for the AI Agents page: mode, rails, today's
    spend, token presence (never the token), connected sessions."""
    import mcp_agents
    import mcp_rails
    out = []
    for a in mcp_agents.agents(cfg):
        mode = mcp_rails.agent_mode(cfg, a)
        try:
            spent = mcp_rails.spent_today(mode, agent_id=a["id"]) if mode != "off" else 0.0
        except Exception:  # noqa: BLE001
            spent = None
        out.append({
            "id": a["id"], "name": a["name"], "emoji": a["emoji"], "color": a["color"],
            "enabled": a["enabled"], "configuredMode": a["mode"], "mode": mode,
            "rails": mcp_rails.agent_rails(cfg, a),
            "rulesInWords": mcp_agents.rules_summary(a, mcp_rails.agent_rails(cfg, a), mode),
            "serverName": mcp_agents.server_name(a),
            "spentTodayUsd": None if spent is None else round(spent, 2),
            "hasToken": load_token(a["id"]) is not None,
            "tokenMasked": masked_token(a["id"]),
            "sessions": sum(1 for s in _sessions.values() if s.get("agent") == a["id"]),
            # This session, memory only (seen()): when the token was last
            # used, how often, through which clients.
            "lastSeenAt": (AGENT_SEEN.get(a["id"]) or {}).get("lastAt"),
            "calls": (AGENT_SEEN.get(a["id"]) or {}).get("calls", 0),
            "clients": list((AGENT_SEEN.get(a["id"]) or {}).get("clients") or []),
        })
    return out


def status(cfg: Optional[dict] = None) -> dict:
    import mcp_rails
    import mcp_tools
    from config import merge_with_defaults
    cfg = merge_with_defaults(cfg or {})
    try:
        pend = mcp_rails.pending()
    except Exception:  # noqa: BLE001
        pend = []
    return {
        "enabled": bool(cfg.get("mcp_enabled")),
        "running": is_running(),
        "port": _port or int(cfg.get("mcp_port") or 47635),
        "url": f"http://127.0.0.1:{_port or int(cfg.get('mcp_port') or 47635)}/mcp",
        "lastError": _last_error,
        "hasToken": load_token() is not None,
        "tokenMasked": masked_token(),
        "tradeMode": mcp_rails.trade_mode(cfg),
        "rails": mcp_rails.rails(cfg),
        "approvals": bool(cfg.get("mcp_live_approval", True)),
        "agents": agents_status(cfg),
        "pendingApprovals": pend,
        "sessions": sorted(_sessions.values(), key=lambda s: s["lastSeen"], reverse=True),
        "toolCount": len(mcp_tools.list_tools()),
        "spentTodayUsd": mcp_tools.spent_today_usd(),
        # lossTodayUsd is filled in by service (it reads marks, which is async).
        "lossTodayUsd": None,
        "httpEnabled": bool(cfg.get("mcp_http_enabled")),
        "httpUrl": f"http://127.0.0.1:{_port or int(cfg.get('mcp_port') or 47635)}{API_PREFIX}",
        "accountMode": cfg.get("account_mode"),
        "audit": audit_log(50),
        "fetchedAt": _iso(),
    }
