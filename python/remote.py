"""Remote control — the shared brain behind the Discord and Telegram bots.

The pitch is simple: leave the app running on the PC, check on it and react to
it from a phone. The consequence is not simple — a chat message can now move
real money, so the security model has to be stated before any of it.

WHO CAN TALK TO IT
    An allowlist of account ids you paired yourself, and nothing else. Not
    "anyone in the server", not "anyone who guesses the bot name" — a message
    from an unlisted id is dropped before it is parsed, and the attempt is
    logged so you can see it happened.

    The bot TOKEN is not the security boundary. Tokens leak: they end up in
    screenshots, in pasted logs, in a git repo. The id allowlist is what
    actually protects you, which is why pairing is mandatory and why an
    unpaired bot answers nothing at all.

WHAT IT CAN DO
    Reading and de-risking (status, positions, P&L, pause, flatten, cancel)
    are available as soon as you pair. PLACING ORDERS is a separate switch,
    off by default, and even on it requires a fresh confirmation per order and
    obeys a size cap — because the realistic threat here is not a hacker, it
    is an unlocked phone. /flatten needs the same fresh confirmation even
    though it is a de-risk: it sells every position you own, and a fat finger
    should not be able to do that in one tap either. The size cap does NOT
    apply to it — a cap on getting out is a cap on safety.

    Every command still passes the same gates a click in the app does: live
    trading off means no order, paper mode means a paper order.

WHAT LEAVES THE MACHINE
    Your positions and P&L, to Discord's or Telegram's servers, because that
    is what a chat bot is. Both bots ship DISABLED and the setup screen says
    this plainly. It is a real trade-off and the user makes it knowingly.

EVERY COMMAND IS LOGGED
    Accepted, refused, or from a stranger — with who, what and when. A remote
    control with no audit trail is a way to be surprised by your own account.
    Strangers are one row each, with a count, in a log of their own, so they
    cannot push your commands out; past the rate limit a flood is dropped
    unrecorded, and a stranger is answered only once.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("krypt.remote")

# ── limits ──────────────────────────────────────────────────────────────
# Per-sender, per-minute. Generous for a human, useless for a script.
RATE_LIMIT_PER_MIN = 20
# A pending order confirmation dies after this. A "buy 50" left unconfirmed
# overnight must never be executable by whoever picks the phone up tomorrow.
CONFIRM_TTL_SEC = 90.0
# Pairing codes are short-lived by design: they are typed into a chat window.
PAIR_CODE_TTL_SEC = 300.0
# Wrong codes, from ANY sender, before the code is canceled. The rate limit is
# per sender, so on its own it gave N accounts N times the guesses.
PAIR_CODE_MAX_FAILS = 5
AUDIT_MAX = 500
# Strangers get a log of their own: one row per sender with a count. Sharing
# AUDIT_MAX with the owner let anyone who could DM the bot flush the owner's
# real commands out of the log.
STRANGER_AUDIT_MAX = 100

_KV_PREFIX = "remote:"


def _kv_get(key: str, default: Any) -> Any:
    try:
        import db
        with db.get_db() as conn:
            v = db.kv_get(conn, _KV_PREFIX + key)
        return json.loads(v) if v else default
    except Exception:
        return default


def _kv_put(key: str, value: Any) -> None:
    import db
    with db.get_db() as conn:
        db.kv_set(conn, _KV_PREFIX + key, json.dumps(value))


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts or _now(), timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


# ────────────────────────────────────────────────────────────────────────
# Pairing + the allowlist
# ────────────────────────────────────────────────────────────────────────


def paired(platform: str) -> list[dict]:
    """Accounts allowed to command this app on `platform`."""
    rows = _kv_get(f"paired:{platform}", [])
    return rows if isinstance(rows, list) else []


def is_allowed(platform: str, account_id: str) -> bool:
    aid = str(account_id or "").strip()
    if not aid:
        return False
    return any(str(p.get("id")) == aid for p in paired(platform))


def add_paired(platform: str, account_id: str, label: str = "") -> list[dict]:
    aid = str(account_id or "").strip()
    if not aid:
        raise ValueError("no account id")
    rows = [p for p in paired(platform) if str(p.get("id")) != aid]
    rows.append({"id": aid, "label": label or None, "pairedAt": _iso()})
    _kv_put(f"paired:{platform}", rows)
    audit(platform, aid, "pair", "accepted", f"paired as {label or aid}")
    return rows


def remove_paired(platform: str, account_id: str) -> list[dict]:
    aid = str(account_id or "").strip()
    rows = [p for p in paired(platform) if str(p.get("id")) != aid]
    _kv_put(f"paired:{platform}", rows)
    audit(platform, aid, "unpair", "accepted", "removed from the allowlist")
    return rows


def new_pair_code(platform: str) -> dict:
    """Mint a short-lived code the user types into the chat.

    This is how Telegram pairing works without the user having to hunt down
    their numeric id: they message the bot the code, the bot learns their chat
    id, and only then is that id allowed. Discord can use it too, though there
    the user id is easy to copy by hand.
    """
    import secrets
    code = f"{secrets.randbelow(1_000_000):06d}"
    _kv_put(f"paircode:{platform}", {"code": code, "expires": _now() + PAIR_CODE_TTL_SEC})
    return {"code": code, "expiresAt": _iso(_now() + PAIR_CODE_TTL_SEC),
            "ttlSec": int(PAIR_CODE_TTL_SEC)}


def consume_pair_code(platform: str, code: str, account_id: str,
                      label: str = "") -> tuple[bool, str]:
    """→ (ok, message). Single use, and expired codes are refused."""
    rec = _kv_get(f"paircode:{platform}", None)
    given = str(code or "").strip()
    if not isinstance(rec, dict) or not rec.get("code"):
        return False, "No pairing code is active. Generate one in the app first."
    if _now() > float(rec.get("expires") or 0):
        _kv_put(f"paircode:{platform}", None)
        return False, "That pairing code has expired. Generate a new one in the app."
    # Constant-time-ish compare; the code is short-lived and rate-limited, but
    # there is no reason to leak position information through timing.
    import hmac
    if not hmac.compare_digest(str(rec["code"]), given):
        fails = int(rec.get("fails") or 0) + 1
        if fails >= PAIR_CODE_MAX_FAILS:
            _kv_put(f"paircode:{platform}", None)
            return False, (
                f"That pairing code is not right. After {fails} wrong tries it "
                f"has been canceled. Generate a new one in the app.")
        _kv_put(f"paircode:{platform}", {**rec, "fails": fails})
        return False, "That pairing code is not right."
    _kv_put(f"paircode:{platform}", None)     # single use
    add_paired(platform, account_id, label)
    return True, (
        "Paired. This account can now control Krypt PolyBot US. "
        "Send /help to see what it can do.")


# ────────────────────────────────────────────────────────────────────────
# Rate limiting + audit
# ────────────────────────────────────────────────────────────────────────

# sender -> [timestamps]
_hits: dict[str, list[float]] = {}


def rate_ok(sender: str) -> bool:
    now = _now()
    if len(_hits) > 2000:
        # Every stranger's message comes through here, so senders who went
        # quiet are dropped rather than kept for the life of the process.
        for k in [k for k, v in _hits.items() if not v or now - v[-1] >= 60.0]:
            _hits.pop(k, None)
    q = [t for t in _hits.get(sender, []) if now - t < 60.0]
    if len(q) >= RATE_LIMIT_PER_MIN:
        _hits[sender] = q
        return False
    q.append(now)
    _hits[sender] = q
    return True


def audit(platform: str, sender: str, command: str, outcome: str,
          detail: str = "") -> None:
    """Record every command — including the refused ones.

    A refusal from an unpaired id is the most interesting row in this log:
    it means somebody found the bot. Dropping it silently would hide exactly
    the thing worth knowing. It goes to the strangers' log, though, so it
    cannot crowd the owner's own commands out of this one.
    """
    if outcome != "accepted" and not is_allowed(platform, str(sender)):
        _audit_stranger(platform, sender, command, outcome, detail)
        return
    rows = _kv_get("audit", [])
    if not isinstance(rows, list):
        rows = []
    rows.insert(0, {
        "at": _iso(), "platform": platform, "sender": str(sender)[:64],
        "command": str(command)[:120], "outcome": outcome,
        "detail": str(detail)[:300],
    })
    del rows[AUDIT_MAX:]
    try:
        _kv_put("audit", rows)
    except Exception:
        pass
    if outcome != "accepted":
        logger.info("remote %s/%s %s -> %s (%s)",
                    platform, str(sender)[:24], command, outcome, detail[:120])


def _audit_stranger(platform: str, sender: str, command: str, outcome: str,
                    detail: str = "") -> int:
    """One row per unpaired sender, with a count, in the strangers' own
    capped log. → how many messages this sender has now sent."""
    sid = str(sender)[:64]
    rows = _kv_get("audit:strangers", [])
    if not isinstance(rows, list):
        rows = []
    prev = next((r for r in rows if isinstance(r, dict)
                 and r.get("platform") == platform and r.get("sender") == sid), None)
    rows = [r for r in rows if r is not prev]
    count = int((prev or {}).get("count") or 0) + 1
    now = _iso()
    rows.insert(0, {
        "at": now, "firstAt": (prev or {}).get("firstAt") or now,
        "platform": platform, "sender": sid,
        "command": str(command)[:120], "outcome": outcome,
        "detail": str(detail)[:300], "count": count,
    })
    del rows[STRANGER_AUDIT_MAX:]
    try:
        _kv_put("audit:strangers", rows)
    except Exception:
        pass
    if count == 1:
        logger.info("remote %s/%s %s -> %s (%s)",
                    platform, sid[:24], command, outcome, str(detail)[:120])
    return count


def audit_log(limit: int = 100) -> list[dict]:
    """The owner's log and the strangers' log, newest first."""
    rows = _kv_get("audit", [])
    rows = rows if isinstance(rows, list) else []
    strangers = _kv_get("audit:strangers", [])
    if isinstance(strangers, list):
        rows = sorted(rows + strangers, key=lambda r: str(r.get("at") or ""),
                      reverse=True)
    return rows[: max(1, min(int(limit or 100), AUDIT_MAX + STRANGER_AUDIT_MAX))]


# ────────────────────────────────────────────────────────────────────────
# Pending order confirmations
# ────────────────────────────────────────────────────────────────────────

# sender -> {order, costUsd, expires, summary}
_pending: dict[str, dict] = {}


def _pending_for(sender: str) -> Optional[dict]:
    p = _pending.get(sender)
    if not p:
        return None
    if _now() > p["expires"]:
        _pending.pop(sender, None)
        return None
    return p


# ────────────────────────────────────────────────────────────────────────
# Formatting — plain text, because it has to read on a phone
# ────────────────────────────────────────────────────────────────────────


def _c(v: Any, suffix: str = "") -> str:
    """The em-dash rule, in chat. An unknown stays unknown here too — a bot
    that reports $0.00 for a balance it could not read is the same lie as a
    UI that does."""
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}{suffix}"
    return f"{v}{suffix}"


def _money(v: Any) -> str:
    return "—" if v is None else f"${float(v):,.2f}"


def _signed(v: Any) -> str:
    if v is None:
        return "—"
    f = float(v)
    return f"{'-' if f < 0 else '+'}${abs(f):,.2f}"


HELP_READ = """Krypt PolyBot US — commands

/status      engines, account, connection
/positions   open positions and P&L
/pnl         today and all-time
/balance     cash and portfolio value
/orders      resting orders
/price <id>  price a market by condition id
/log         recent remote commands

Safety:
/pause       stop every engine AND the AI agents opening anything new
/resume      re-enable the engines you paused (not the agents)
/flatten     close EVERY open position (previews, then /confirm)
/cancelall   cancel every resting order

AI agents:
/agents      their trading mode, Autopilot, and orders waiting for you
/agents off  stop every AI agent and Autopilot from trading
/reject <n>  refuse a queued order (always allowed)

/whoami      show your id and what you are allowed to do
/help        this message"""

HELP_TRADE = """
Trading (only because you enabled it in the app):
/buy <id> <yes|no> <size> <price¢>
/sell <id> <yes|no> <size> <price¢>
/confirm     send the pending order
/cancel      discard the pending order
/approve <n> send an AI agent's queued live order (re-checked first)

Every order is previewed first and needs /confirm within 90s."""


# ────────────────────────────────────────────────────────────────────────
# The dispatcher
# ────────────────────────────────────────────────────────────────────────


async def handle(platform: str, sender_id: str, text: str, *,
                 label: str = "") -> str:
    """One inbound message → one reply. Transport-agnostic on purpose: the
    Discord and Telegram bots are thin shells around this, so the auth and
    gating rules cannot drift apart between them."""
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = raw.split()
    cmd = parts[0].lower().lstrip("/")
    # Discord mentions and Telegram's "/cmd@botname" both need stripping.
    cmd = cmd.split("@")[0]
    args = parts[1:]
    sender = f"{platform}:{sender_id}"

    # ── pairing is the ONLY thing an unpaired id may do ──────────────────
    if not is_allowed(platform, sender_id):
        if not rate_ok(sender):
            # A flood. Its row already shows the sender at the limit; writing
            # (and answering) every further message would hand a stranger a
            # database write and a reply per message.
            return ""
        if cmd in ("pair", "start") and args:
            ok, msg = consume_pair_code(platform, args[0], str(sender_id), label)
            audit(platform, sender_id, "pair",
                  "accepted" if ok else "refused", msg)
            return msg
        n = _audit_stranger(platform, sender_id, raw[:60], "unauthorised",
                            "sender is not on the allowlist")
        if n > 1:
            # The first reply told a real user how to pair. Any more would
            # only tell a stranger the bot is alive and listening.
            return ""
        return (
            "This bot is not paired with you. Open Krypt PolyBot US on your PC, "
            "go to Settings → Remote control, generate a pairing code, then "
            "send:  /pair <code>")

    if not rate_ok(sender):
        audit(platform, sender_id, cmd, "rate-limited")
        return "Slow down — too many commands in the last minute."

    try:
        reply = await _dispatch(platform, sender_id, cmd, args, raw)
        audit(platform, sender_id, raw[:60], "accepted")
        return reply
    except Exception as e:  # noqa: BLE001
        audit(platform, sender_id, raw[:60], "error", f"{type(e).__name__}: {e}")
        logger.debug("remote command failed: %s", e, exc_info=True)
        return f"That failed: {type(e).__name__}: {e}"


async def _cfg() -> dict:
    import service
    from config import merge_with_defaults
    return merge_with_defaults(dict(service.STATE.cfg or {}))


def trading_allowed(cfg: dict) -> bool:
    """Remote trading needs BOTH switches: remote enabled and remote TRADING
    enabled. Two switches, because the second one has a much bigger blast
    radius than the first and should not ride along with it."""
    return bool(cfg.get("remote_enabled")) and bool(cfg.get("remote_trading_enabled"))


async def _dispatch(platform: str, sender_id: str, cmd: str,
                    args: list[str], raw: str) -> str:
    cfg = await _cfg()

    if cmd in ("help", "start", "commands"):
        return HELP_READ + (HELP_TRADE if trading_allowed(cfg) else "")

    if cmd == "whoami":
        return (
            f"Platform: {platform}\n"
            f"Your id: {sender_id}\n"
            f"Paired: yes\n"
            f"Can place orders: "
            f"{'yes' if trading_allowed(cfg) else 'no (enable it in the app)'}\n"
            f"Live trading: {'on' if cfg.get('enable_trading') else 'off'}\n"
            f"Paper mode: {'on' if cfg.get('terminal_paper_mode') else 'off'}")

    if cmd == "log":
        rows = audit_log(12)
        if not rows:
            return "No remote commands recorded yet."
        return "Recent remote commands:\n" + "\n".join(
            f"{r['at'][11:19]} {r['platform']}/{str(r['sender'])[:10]} "
            f"{r['command']} → {r['outcome']}"
            + (f" (x{r['count']})" if (r.get("count") or 0) > 1 else "")
            for r in rows)

    # ── read ─────────────────────────────────────────────────────────────
    if cmd == "status":
        return await _status(cfg)
    if cmd in ("positions", "pos"):
        return await _positions()
    if cmd == "pnl":
        return await _pnl()
    if cmd in ("balance", "bal"):
        return await _balance()
    if cmd == "orders":
        return await _orders()
    if cmd == "price":
        if not args:
            return "Usage: /price <condition id>"
        return await _price(args[0])

    # ── de-risking: allowed for any paired account ───────────────────────
    if cmd == "pause":
        return await _pause()
    if cmd == "resume":
        return await _resume()
    if cmd == "flatten":
        return await _flatten(platform, sender_id)
    if cmd in ("cancelall", "cancel_all"):
        return await _cancel_all()

    # ── AI agents ────────────────────────────────────────────────────────
    if cmd == "agents":
        sub = (args[0].lower() if args else "")
        if sub in ("off", "stop"):
            # A de-risk, open to any paired account exactly like /pause.
            return await _agents_off()
        if sub in ("on", "live", "paper"):
            # Turning agents back on is not a de-risk, and going live has a
            # checklist in the app that a chat line cannot stand in for.
            return ("Agents can only be turned back on in the app "
                    "(AI Agents page). From here you can only stop them: "
                    "/agents off.")
        if sub:
            return "Usage: /agents  (status)  or  /agents off"
        return await _agents_status() + "\n\n" + _agents()
    if cmd in ("approve", "reject"):
        if not args or not args[0].lstrip("#").isdigit():
            return f"Usage: /{cmd} <number>  (send /agents for the list)"
        # Refusing is a de-risk, open to any paired account. Approving SENDS
        # a real order, so it needs the trading switch like /buy does.
        if cmd == "approve" and not trading_allowed(cfg):
            return ("Approving agent orders from chat needs remote trading on "
                    "(Settings → Remote control). You can still /reject, or "
                    "approve in the app.")
        import mcp_rails
        res = await mcp_rails.decide(int(args[0].lstrip("#")), cmd == "approve",
                                     via=f"phone:{platform}")
        return str(res.get("message") or ("Done." if res.get("ok") else "Not done."))

    # ── trading: the gated half ──────────────────────────────────────────
    if cmd in ("buy", "sell"):
        if not trading_allowed(cfg):
            return (
                "Placing orders from chat is turned off. Enable it in the app "
                "under Settings → Remote control if you want it — it is off by "
                "default on purpose.")
        return await _stage_order(cfg, platform, sender_id, cmd, args)
    if cmd == "confirm":
        # A pending FLATTEN confirms without the trading switch: it is a
        # de-risk, available to any paired account exactly like /pause. Only
        # an ENTRY needs the second switch.
        pend = _pending_for(f"{platform}:{sender_id}")
        if pend and pend.get("kind") == "flatten":
            return await _confirm_flatten(cfg, platform, sender_id)
        if not pend:
            # Say what is actually true. "Remote trading is off" in answer to a
            # confirm that had nothing to confirm sends the user to the wrong
            # switch.
            return "Nothing pending (or it expired). Send the command again."
        if not trading_allowed(cfg):
            return "Remote trading is off."
        return await _confirm_order(cfg, platform, sender_id)
    if cmd == "cancel":
        gone = _pending.pop(f"{platform}:{sender_id}", None)
        if gone:
            return ("Pending flatten discarded — nothing was sold."
                    if gone.get("kind") == "flatten"
                    else "Pending order discarded.")
        return "Nothing pending."

    return f"Unknown command '{cmd}'. Send /help."


# ────────────────────────────────────────────────────────────────────────
# Read commands
# ────────────────────────────────────────────────────────────────────────


def _agents() -> str:
    import mcp_rails
    rows = mcp_rails.pending(10)
    if not rows:
        return "No AI agent orders are waiting for approval."
    out = ["AI agent orders waiting for you (expire after "
           f"{mcp_rails.APPROVAL_TTL_SEC // 60} min):"]
    for r in rows:
        who = r.get("agentName") or r.get("agentId") or "agent"
        title = (r.get("title") or r["conditionId"][:12] + "…")[:48]
        cost = r.get("committedUsd")
        out.append(f"#{r['id']} {who}: {r['action'].upper()} {r['size']:g} "
                   f"{r['side'].upper()} @ {r['priceCents']:g}¢ — {title}"
                   + (f" (${cost:,.2f})" if cost is not None else ""))
    out.append("Reply /approve <n> or /reject <n>.")
    return "\n".join(out)


async def _status(cfg: dict) -> str:
    import service
    st = service.STATE
    engines = []
    for label, key in (("Master", "enable_trading"), ("Crypto", "crypto15m_enabled"),
                       ("Scripts", "scripts_live_enabled")):
        engines.append(f"{label} {'on' if cfg.get(key) else 'off'}")
    bal = await _balance()
    return (
        f"Krypt PolyBot US\n"
        f"Account: {'connected' if st.auth_ok else 'NOT connected'}\n"
        f"Engines: {' · '.join(engines)}\n"
        f"{_agents_line(cfg)}\n"
        f"Paper mode: {'on' if cfg.get('terminal_paper_mode') else 'off'}\n"
        f"{bal}")


async def _balance() -> str:
    import polymarket_api as api
    try:
        b = await api.get_balance()
    except Exception as e:  # noqa: BLE001
        # An unreadable balance is unknown, never $0.00.
        return f"Balance: unavailable ({type(e).__name__})"
    cash = b.get("balance")
    port = b.get("portfolio_value")
    return (f"Cash: {_money(cash / 100.0) if cash is not None else '—'} · "
            f"Portfolio: {_money(port / 100.0) if port is not None else '—'}")


async def _positions() -> str:
    import terminal_trade
    pf = await terminal_trade.portfolio()
    rows = pf.get("positions") or []
    if not rows:
        return "No open positions."
    out = [f"{len(rows)} position(s):"]
    for p in rows[:12]:
        title = (p.get("title") or p["conditionId"][:16])[:44]
        line = (f"{p['side'].upper()} {p['size']:g} · {title}\n"
                f"   mark {_c(p.get('markCents'))}¢ · "
                f"val {_money(p.get('valueUsd'))} · "
                f"P&L {_signed(p.get('unrealizedPnlUsd'))}")
        if p.get("unreconciled"):
            line += "  (no cost basis — excluded from totals)"
        out.append(line)
    if len(rows) > 12:
        out.append(f"…and {len(rows) - 12} more.")
    out.append(f"\nTotal value {_money(pf.get('totalValueUsd'))} · "
               f"unrealised {_signed(pf.get('totalUnrealizedPnlUsd'))}")
    if pf.get("unreconciledNote"):
        out.append(pf["unreconciledNote"])
    return "\n".join(out)


async def _pnl() -> str:
    import service
    snap = await service._build_account_snapshot()
    return (f"Session {_signed(snap.get('sessionPnlUsd'))}\n"
            f"Today   {_signed(snap.get('todayPnlUsd'))}\n"
            f"All time {_signed(snap.get('alltimePnlUsd'))}\n"
            f"Win rate {_c(snap.get('winRate'))}")


async def _orders() -> str:
    import terminal_trade
    r = await terminal_trade.resting_orders()
    rows = r.get("orders") or []
    if not rows:
        return r.get("note") or "No resting orders."
    out = [f"{len(rows)} resting order(s):"]
    for o in rows[:12]:
        out.append(
            f"{o['action']} {o['side'].upper()} {_c(o.get('size'))} @ "
            f"{_c(o.get('priceCents'))}¢ · {(o.get('title') or o['conditionId'][:16])[:38]}")
    return "\n".join(out)


async def _price(condition_id: str) -> str:
    import terminal
    m = await terminal.market(condition_id)
    if not m:
        return "No market with that condition id."
    return (f"{m['title'][:70]}\n"
            f"{_c(m.get('probability'))}¢  (bid {_c(m.get('bid'))} / "
            f"ask {_c(m.get('ask'))})\n"
            f"24h {_money(m.get('volume24hUsd'))} · closes {m.get('closeTime') or '—'}")


# ────────────────────────────────────────────────────────────────────────
# De-risking commands
# ────────────────────────────────────────────────────────────────────────


async def _set_cfg(patch: dict) -> None:
    """Change config the same way the UI does, so the app and the backend
    never disagree about what is on."""
    import service
    from config import merge_with_defaults
    # Electron persists it (and echoes it back through setConfig)…
    await service.emit_event("remote:configPatch", {"patch": patch})
    # …but that round-trip can fail silently, so land it in-process too.
    # The patch is camelCase and STATE.cfg is snake_case: a raw dict merge
    # only ADDS a dead `enableTrading` key and leaves `enable_trading` true,
    # so /pause did not actually stop anything. merge_with_defaults does the
    # camel→snake conversion and the clamps — exactly what _h_setConfig does.
    service.STATE.cfg = merge_with_defaults(
        {**(service.STATE.cfg or {}), **patch})


# What "stop the agents" writes. Trade mode OFF, not paper: /pause means
# nothing opens anything new, and an agent on paper still places (paper)
# orders and Autopilot still spends its AI budget. OFF still lets an agent
# read and forecast, and the stops/take-profits already written down keep
# firing (the standing ticks do not read the agent mode). Turning agents back
# on is left to the app: it is not a de-risk, and going live has a checklist.
AGENTS_OFF_PATCH = {"mcpTradeMode": "off", "autopilotEnabled": False}


def _live_agent_names(cfg: dict) -> list[str]:
    """Agents that would send REAL orders right now: the account Live, the
    global mode live, and the agent itself enabled and set live."""
    try:
        import mcp_agents
        import mcp_rails
        if mcp_rails.trade_mode(cfg) != "live":
            return []
        return [str(a.get("name") or a.get("id"))
                for a in mcp_agents.agents(cfg)
                if a.get("enabled", True) and a.get("mode") == "live"]
    except Exception:  # noqa: BLE001
        return []


def _agents_line(cfg: dict) -> str:
    mode = str(cfg.get("mcp_trade_mode") or "off").lower()
    live = _live_agent_names(cfg)
    if live:
        appr = ("each order waits for your approval"
                if cfg.get("mcp_live_approval", True) is not False
                else "approvals OFF")
        names = ", ".join(live[:3]) + (f" +{len(live) - 3}" if len(live) > 3 else "")
        what = f"LIVE ({names}; {appr})"
    elif mode == "off":
        what = "off"
    else:
        what = "paper"
    return (f"AI agents: {what} · Autopilot "
            f"{'on' if cfg.get('autopilot_enabled') else 'off'}")


async def stop_agents() -> dict:
    """Stop every AI agent and Autopilot from trading. → what was on before.
    Shared by /pause, /agents off and the app's pause RPC."""
    cfg = await _cfg()
    was = {"mode": str(cfg.get("mcp_trade_mode") or "off").lower(),
           "autopilot": bool(cfg.get("autopilot_enabled")),
           "live": _live_agent_names(cfg)}
    await _set_cfg(dict(AGENTS_OFF_PATCH))
    return was


def agents_stopped_text(was: dict) -> str:
    if was.get("mode") == "off" and not was.get("autopilot"):
        return "AI agents were already off."
    bits = []
    if was.get("live"):
        bits.append(f"were LIVE: {', '.join(was['live'])}")
    elif was.get("mode") != "off":
        bits.append(f"were on {was.get('mode')}")
    if was.get("autopilot"):
        bits.append("Autopilot was on")
    return (f"AI agents: trading OFF and Autopilot off ({'; '.join(bits)}). "
            "Stops and take-profits already set still fire. Turn agents "
            "back on in the app (AI Agents page).")


async def _agents_status() -> str:
    cfg = await _cfg()
    return _agents_line(cfg) + "\n/agents off stops them all."


async def _agents_off() -> str:
    return agents_stopped_text(await stop_agents())


async def _pause() -> str:
    cfg = await _cfg()
    was = {k: bool(cfg.get(k)) for k in
           ("enable_trading", "crypto15m_enabled", "scripts_live_enabled")}
    _kv_put("paused_snapshot", was)
    await _set_cfg({
        "enableTrading": False, "crypto15mEnabled": False,
        "scriptsLiveEnabled": False,
    })
    # With approvals off an agent places orders on its own: a pause that left
    # it running while saying "every engine paused" was the dangerous lie.
    agents = await stop_agents()
    on = [k for k, v in was.items() if v]
    return ("Every engine paused. "
            + (f"Was on: {', '.join(on)}. /resume puts those back."
               if on else "No engine was running.")
            + "\n" + agents_stopped_text(agents))


async def _resume() -> str:
    was = _kv_get("paused_snapshot", None)
    if not isinstance(was, dict) or not any(was.values()):
        return ("Nothing to resume — no engines were on when you paused. "
                "Turn them on in the app.")
    patch = {
        "enableTrading": bool(was.get("enable_trading")),
        "crypto15mEnabled": bool(was.get("crypto15m_enabled")),
        "scriptsLiveEnabled": bool(was.get("scripts_live_enabled")),
    }
    await _set_cfg(patch)
    _kv_put("paused_snapshot", None)
    on = [k for k, v in was.items() if v]
    return (f"Resumed: {', '.join(on)}. AI agents stay off until you turn "
            "them on in the app.")


async def _wallet_positions_left() -> Optional[int]:
    """How many positions the WHOLE wallet still holds — the same read
    /positions does. None when it could not be read: an unknown count stays
    unknown rather than being reported as zero."""
    try:
        import terminal_trade
        pf = await terminal_trade.portfolio()
        return len(pf.get("positions") or [])
    except Exception:  # noqa: BLE001
        return None


async def _wallet_orders_left() -> Optional[int]:
    """Same idea for resting orders — the read /orders does."""
    try:
        import terminal_trade
        r = await terminal_trade.resting_orders()
        return len(r.get("orders") or [])
    except Exception:  # noqa: BLE001
        return None


def _main_engine_open() -> Optional[int]:
    """Positions the MAIN engine still has an open row for.

    A count for the preview only — `_h_flatten` is what actually closes them,
    and it reports its own number. `None` when the table could not be read.
    """
    try:
        import db
        with db.get_db() as conn:
            return int(db.count_open_bot_positions(conn))
    except Exception:  # noqa: BLE001
        return None


# Both halves of the de-risk go through the app's OWN paths rather than calling
# trader or the CLOB directly. That is deliberate: it means the remote cannot
# end up with a looser path than the button in the app — same auth check, same
# paper gate, same order log, and any future gate added there applies here for
# free.
#
# /flatten used to reach only the MAIN engine's `bot_positions` while
# /positions showed the whole wallet, so it could truthfully say "closed 1"
# while you were still holding three. It now closes the main engine AND sells
# the rest of the wallet through `terminal_trade.flatten_wallet` — and, because
# that spends money on every position you own, it is two-step exactly like a
# chat order.


async def _flatten(platform: str, sender_id: str) -> str:
    """Step one: price it, and hold it pending a /confirm."""
    import terminal_trade
    main_open = _main_engine_open()
    pv = await terminal_trade.flatten_preview()

    if not pv.get("ok"):
        # An unreadable wallet is NOT an empty one, and must not stage a
        # flatten whose scope nobody can see.
        return (f"{pv.get('message') or 'Could not read your account.'} "
                f"Nothing was staged — try /flatten again in a moment.")

    sellable = int(pv.get("count") or 0)
    blocked = [p for p in (pv.get("positions") or []) if p.get("blocked")]
    if not sellable and not (main_open or 0):
        # Keep the old honesty: only say this when we actually KNOW.
        tail = ""
        if blocked:
            tail = (f" {len(blocked)} position(s) are held but cannot be sold "
                    f"right now — /positions shows them.")
        if main_open is None:
            tail += (" The bot's own positions could not be read, so "
                     "check /status.")
        return "Nothing open to close." + tail

    lines = ["Flatten — this closes EVERYTHING, not just one engine:"]
    if pv.get("paper"):
        # Better said now than after a /confirm that sells nothing.
        lines.append("PAPER MODE IS ON — no order would leave this "
                     "machine. Turn it off in the app first.")
    lines.append(
        f"Bot: {main_open} open position(s) will be closed."
        if main_open is not None else
        "Bot: its open positions will be closed (count unreadable).")
    lines.append(
        f"Account: {sellable} position(s) sold at the best bid, "
        f"about {_money(pv.get('proceedsUsd'))} back.")
    if pv.get("engineTracked"):
        lines.append(f"({pv['engineTracked']} of those are engine-tracked; "
                     f"their reconcile books them as sold.)")
    for p in blocked[:5]:
        lines.append(f"! {str(p['conditionId'])[:12]}… {p['side'].upper()}: "
                     f"{p['blocked']}")
    if len(blocked) > 5:
        lines.append(f"! …and {len(blocked) - 5} more that cannot be sold.")
    lines.append(
        "Sold at MARKET, not at a limit — your remote order cap is an entry "
        "guard and does not apply to getting out. The proceeds figure is an "
        "estimate off the book as it is now.")
    lines.append(f"\nSend /confirm within {int(CONFIRM_TTL_SEC)}s, or /cancel.")

    _pending[f"{platform}:{sender_id}"] = {
        "kind": "flatten", "expires": _now() + CONFIRM_TTL_SEC,
    }
    return "\n".join(lines)


async def _confirm_flatten(cfg: dict, platform: str, sender_id: str) -> str:
    """Step two: close the main engine, then sell the rest of the wallet."""
    key = f"{platform}:{sender_id}"
    if not _pending_for(key):
        return "Nothing pending (or it expired). Send /flatten again."
    _pending.pop(key, None)

    import service
    import terminal_trade
    r = await service._h_flatten({})
    n = int(r.get("closed") or 0)
    fw = await terminal_trade.flatten_wallet(
        allow_trading=True,   # manual; not held to the automation switch
        origin="manual", reason=f"remote /flatten ({platform})")

    lines = [f"Closed {n} bot position(s).",
             fw.get("message") or "The account flatten reported nothing."]
    for row in (fw.get("results") or []):
        if not row.get("ok"):
            lines.append(f"! {str(row['conditionId'])[:12]}… "
                         f"{str(row['side']).upper()}: {row.get('message')}")
    if len(lines) > 8:
        del lines[8:]
        lines.append("…see /positions for the rest.")

    left = await _wallet_positions_left()
    if left is None:
        lines.append("Could not re-read the account — check /positions.")
    elif left > 0:
        lines.append(f"{left} position(s) are still open — /positions says "
                     f"which, and why they could not be sold.")
    else:
        lines.append("Nothing else open in the account.")
    return "\n".join(lines)


async def _cancel_all() -> str:
    """Cancel the main engine's resting orders AND the terminal's.

    Two separate ledgers, so two separate cancels: the engine cancels what it
    placed, and the terminal's own resting orders (a stop that rested, a GTC
    you left on the book) are cancelled through the same call the app's Cancel
    button uses. Reported separately — a single merged number would hide which
    half failed.
    """
    import service
    import terminal_trade
    r = await service._h_cancelAllOpen({})
    n = int(r.get("canceled") or 0)

    rest = await terminal_trade.resting_orders()
    rows = [o for o in (rest.get("orders") or []) if o.get("orderId")]
    term_ok = term_fail = 0
    for o in rows:
        res = await terminal_trade.cancel(str(o["orderId"]))
        if res.get("ok"):
            term_ok += 1
        else:
            term_fail += 1

    lines = [f"Canceled {n} bot resting order(s)."]
    if rows:
        lines.append(f"Canceled {term_ok} of {len(rows)} remaining resting "
                     f"order(s) (manual, standing, crypto or script)."
                     + (f" {term_fail} refused." if term_fail else ""))
    elif rest.get("note") and "No resting orders" not in str(rest.get("note")):
        # An unreadable order list is not an empty one.
        lines.append(f"The rest of your resting orders could not be read: "
                     f"{rest['note']}")

    left = await _wallet_orders_left()
    if left is None:
        lines.append("Could not re-read the account — check /orders.")
    elif left > 0:
        lines.append(f"{left} resting order(s) are still on the book — "
                     f"/orders says which.")
    else:
        lines.append("No resting orders left.")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Trading — staged, confirmed, capped
# ────────────────────────────────────────────────────────────────────────


async def _stage_order(cfg: dict, platform: str, sender_id: str,
                       action: str, args: list[str]) -> str:
    """Preview an order and hold it pending a /confirm.

    Two steps on purpose. The realistic threat is not a hacker, it is a phone
    left unlocked on a table — and a single fat-fingered `/buy` should not be
    able to spend anything.
    """
    if len(args) < 4:
        return (f"Usage: /{action} <condition id> <yes|no> <size> <price¢>\n"
                f"e.g. /{action} 0xabc… yes 10 55")
    cid, side, size_s, price_s = args[0], args[1].lower(), args[2], args[3]
    if side not in ("yes", "no"):
        return "Side must be yes or no."
    try:
        size = float(size_s)
        price = float(price_s)
    except ValueError:
        return "Size and price must be numbers."

    order = {
        "conditionId": cid, "side": side, "action": action,
        "priceCents": price, "size": size, "orderType": "FAK",
        "paper": bool(cfg.get("terminal_paper_mode")),
    }
    import terminal_trade
    pv = await terminal_trade.preview(order)
    if pv["blockers"]:
        return "Refused: " + pv["blockers"][0]

    cost = pv.get("costUsd")
    cap = float(cfg.get("remote_max_order_usd") or 0.0)
    if cost is not None and cap > 0 and cost > cap:
        return (f"Refused: ${cost:,.2f} is over your remote order cap of "
                f"${cap:,.2f}. Raise it in the app if you meant it.")

    _pending[f"{platform}:{sender_id}"] = {
        "order": order, "costUsd": cost, "expires": _now() + CONFIRM_TTL_SEC,
    }
    lines = [
        f"{'PAPER ' if order['paper'] else ''}{action.upper()} "
        f"{pv.get('size') or size:g} {side.upper()} @ {pv.get('priceCents') or price:g}¢",
        f"Cost {_money(cost)} · fee {_money(pv.get('feeUsd'))}",
        f"Fillable now: {_c(pv.get('fillableSize'))} · "
        f"est. avg {_c(pv.get('estimatedAvgCents'))}¢",
    ]
    for w in (pv.get("warnings") or [])[:3]:
        lines.append(f"! {w}")
    lines.append(f"\nSend /confirm within {int(CONFIRM_TTL_SEC)}s, or /cancel.")
    return "\n".join(lines)


async def _confirm_order(cfg: dict, platform: str, sender_id: str) -> str:
    key = f"{platform}:{sender_id}"
    p = _pending_for(key)
    if not p:
        return "Nothing pending (or it expired). Send the order again."
    _pending.pop(key, None)

    import terminal_paper
    import terminal_trade
    order = p["order"]
    order["confirmUsd"] = p["costUsd"] or 0.0

    if order.get("paper"):
        res = await terminal_paper.place(order, cfg)
        return ("PAPER: " + res.get("message", "done")) if res.get("ok") \
            else ("Refused: " + res.get("message", "?"))

    # A chat order is a manual order: paper mode, the remote's own trading
    # switch, the per-order cap and place()'s gates apply — the automation
    # master switch does not.
    res = await terminal_trade.place(order, origin="manual")
    return res.get("message", "done") if res.get("ok") \
        else ("Refused: " + res.get("message", "?"))


# ────────────────────────────────────────────────────────────────────────
# Outbound push
# ────────────────────────────────────────────────────────────────────────

# Set by the bots at startup: platform -> async (account_id, text) -> None
_senders: dict[str, Callable] = {}


def register_sender(platform: str, fn: Callable) -> None:
    _senders[platform] = fn


def unregister_sender(platform: str) -> None:
    _senders.pop(platform, None)


async def push(text: str) -> int:
    """Send a line to every paired account on every connected platform.

    Used for the things you would want to know without asking: a standing
    instruction firing, an engine turning itself off. Never for routine chatter
    — a bot that pings on every tick gets muted, and then it is worth nothing
    when it matters.
    """
    sent = 0
    for platform, fn in list(_senders.items()):
        for p in paired(platform):
            try:
                await fn(str(p["id"]), text)
                sent += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("remote push to %s failed: %s", platform, e)
    return sent


def status() -> dict:
    """What the Settings screen renders."""
    import bot_discord
    import bot_telegram
    return {
        "discord": {
            "connected": bot_discord.is_running(),
            "lastError": bot_discord.last_error(),
            "paired": paired("discord"),
            "hasToken": bot_discord.has_token(),
        },
        "telegram": {
            "connected": bot_telegram.is_running(),
            "lastError": bot_telegram.last_error(),
            "paired": paired("telegram"),
            "hasToken": bot_telegram.has_token(),
            "botUsername": bot_telegram.bot_username(),
        },
        "audit": audit_log(25),
        "fetchedAt": _iso(),
    }
