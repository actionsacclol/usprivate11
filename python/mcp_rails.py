"""The money rails every AI agent order passes, in one place.

An agent is a language model with the user's money in reach, and a model can
be wrong, be talked into things, or loop. So nothing here trusts what it says
about itself. Every rule below is enforced in the backend, on every order, as
the named agent the request's TOKEN belongs to (mcp_agents) — never as the
client name it gives, and never as whatever its guide says.

THE RAILS (vet)
    * Mode. The global trade mode is the master (off | paper | live); an agent
      only trades live when the global mode AND its own mode say live, and the
      app's paper switch narrows live to paper for everyone.
    * No buy without a forecast. A buy must cite a fair value THIS agent
      recorded on THIS market within FORECAST_TTL_SEC, and the limit must beat
      it by the minimum edge AFTER Polymarket's taker fee. An unknown fee is
      not a zero fee: the edge cannot be checked, so the buy is refused.
    * The agent's own rules (mcp_agents.entry_blockers) — categories from
      Polymarket's own tags, price band, time to close, sides.
    * Money. Per order and per UTC day, all agents together AND each agent's
      own (which can only be stricter). Queued approvals count as spent.
    * Positions. Open markets across every active agent, and per agent; shares
      per market per agent.
    * Daily loss stop. Closes, settlements and fees realised today, every agent
      together. Open positions count once their market closes. An unreadable
      live ledger refuses buys.
    * Ownership. An agent sells or cancels only what IT opened: its paper book
      is its own account, and live it may sell only what its own orders
      actually FILLED (terminal_fills, by order id) — never what they asked
      for — less what it has already sold or set a live stop for.
    Exits are never capped, never blocked by a rule or the loss stop.

THE LIVE LEDGER (mcp_orders.status, live rows)
    NULL / approved  sent; a GTC may still rest, so a buy counts its full
                     size and cost, a sell its full size
    filled / partial a fill-and-kill whose fill is KNOWN (the rest was
                     killed): it counts what filled, nothing more
    unmatched        a fill-and-kill that crossed nothing: zero
    cancelled        cancelled at what it had filled (any cancel path, or
                     found cancelled on the venue)
    refused          the venue (or the ticket, before sending) definitely
                     placed nothing: counts nowhere, holds no slot
    unknown          may or may not be on the venue: counts in full (fail
                     closed) until reconcile finds it or the user settles it
                     (settle_unknown, RPC mcpSettleUnknown)
    void             an 'unknown' the user settled after Polymarket showed
                     no such order and no fill: counts nowhere

APPROVALS ("ask me first", on by default)
    A live agent order is queued, the user approves it in the app or by phone,
    and it is RE-VETTED at the moment of approval against the market as it is
    then. It counts against the daily cap while it waits and expires after
    APPROVAL_TTL_SEC.

ATOMICITY
    vet → send → record runs under one lock for ALL agents, so two parallel
    calls (or two agents, or two approvals clicked together) cannot both
    squeeze under a cap that fits one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import mcp_agents

logger = logging.getLogger("krypt.mcp.rails")

TRADE_MODES = ("off", "paper", "live")
# A forecast older than this no longer backs a buy: the market it argued
# against has moved on.
FORECAST_TTL_SEC = 1800
# A queued live order the user did not answer in this long is expired and
# spends nothing.
APPROVAL_TTL_SEC = 600
# A row still 'deciding' this long past its window means the backend died
# mid-approval; it reads as failed (and keeps counting, because the order may
# have reached the venue).
DECIDING_GRACE_SEC = 300

DEFAULT_AGENT = mcp_agents.DEFAULT_ID


class Refused(Exception):
    """A gate said no. Reported to the agent as a tool error, audited.

    `event` is the mcp:order announcement for a refusal the user must hear
    about (a live order the venue refused, or one whose fate is unknown). The
    caller announces it after releasing the money lock, then re-raises."""

    def __init__(self, message: str = "", event: Optional[dict] = None):
        super().__init__(message)
        self.event = event


_LOCK: Optional[tuple] = None


def money_lock() -> asyncio.Lock:
    """One lock per event loop (tests run several loops)."""
    global _LOCK
    loop = asyncio.get_running_loop()
    if _LOCK is None or _LOCK[0] is not loop:
        _LOCK = (loop, asyncio.Lock())
    return _LOCK[1]


# ────────────────────────────────────────────────────────────────────────
# Config, agent, mode
# ────────────────────────────────────────────────────────────────────────


def cfg() -> dict:
    import service
    from config import merge_with_defaults
    return merge_with_defaults(dict(service.STATE.cfg or {}))


def authed() -> bool:
    try:
        import service
        return bool(service.STATE.auth_ok)
    except Exception:  # noqa: BLE001
        return False


def trade_mode(c: dict) -> str:
    """The global agent trade mode, narrowed by the account mode: while the
    account is in Paper no real order can be sent from anywhere, so a global
    "live" is paper. It only ever narrows. (config.account_live reads both
    account_mode and the terminal_paper_mode derived from it, so this and
    every other paper gate agree.)"""
    from config import account_live
    m = str(c.get("mcp_trade_mode") or "off").lower()
    m = m if m in TRADE_MODES else "off"
    if m == "live" and not account_live(c):
        return "paper"
    return m


def rails(c: dict) -> dict:
    return {
        "maxOrderUsd": float(c.get("mcp_max_order_usd") or 0.0),
        "dailySpendUsd": float(c.get("mcp_daily_spend_usd") or 0.0),
        "maxOpenPositions": int(c.get("mcp_max_positions") or 0),
        "minEdgeCents": float(c.get("mcp_min_edge_cents") or 0.0),
        "dailyLossUsd": float(c.get("mcp_daily_loss_usd") or 0.0),
        "forecastTtlMin": FORECAST_TTL_SEC // 60,
    }


def agent_for(session: Optional[dict], c: dict) -> Optional[dict]:
    """The agent a call runs as: set by the transport from the token. A
    session with none (in-process callers, tests) is the Default agent."""
    return mcp_agents.get(c, str((session or {}).get("agent") or DEFAULT_AGENT))


def require_agent(session: Optional[dict], c: dict) -> dict:
    """The calling agent from THIS config read, or a refusal. An agent deleted
    or switched off since the call began is refused, never run as Default."""
    a = agent_for(session, c)
    if a is None:
        raise Refused("This agent no longer exists in Krypt PolyBot US. Nothing was done.")
    if not a.get("enabled", True):
        raise Refused(f"{a['name']} is switched off in Krypt PolyBot US. Nothing was done.")
    return a


def agent_mode(c: dict, agent: Optional[dict]) -> str:
    return mcp_agents.effective_mode(trade_mode(c), agent)


def agent_rails(c: dict, agent: Optional[dict]) -> dict:
    return mcp_agents.effective_rails(rails(c), agent)


def agent_name(agent: Optional[dict]) -> str:
    return (agent or {}).get("name") or "The agent"


def book_of(agent: Optional[dict]) -> str:
    return mcp_agents.paper_book((agent or {}).get("id") or DEFAULT_AGENT)


def _net() -> str:
    import terminal_trade
    return terminal_trade._net()


# ────────────────────────────────────────────────────────────────────────
# Time
# ────────────────────────────────────────────────────────────────────────


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def age_sec(created_at: Any) -> Optional[float]:
    if not created_at:
        return None
    try:
        s = str(created_at).replace("T", " ").replace("Z", "")[:19]
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


# ────────────────────────────────────────────────────────────────────────
# The agent order ledger (mcp_orders)
# ────────────────────────────────────────────────────────────────────────

_AGENT_SQL = "AND (? IS NULL OR agent_id=?) "
# A pending approval still inside its window. One expression for every rail
# that counts queued orders, so they cannot disagree about what "expired" is.
_LIVE_PENDING_SQL = "(status='pending' AND julianday(created_at) > julianday('now', ?))"


def _pending_cutoff() -> str:
    return f"-{int(APPROVAL_TTL_SEC)} seconds"


# A live row whose order is (or may be) on the venue under the id it holds:
# sent, or 'unknown' with an id (a lost reply; reconcile may still find it).
_LIVE_SENT_SQL = "(o.ok=1 OR (o.status='unknown' AND o.order_id IS NOT NULL))"
# Statuses whose fill is FINAL: nothing of the order can still fill, so it
# counts what it filled and no more (see THE LIVE LEDGER above).
_FINAL = ("filled", "partial", "unmatched", "cancelled")
_FINAL_SQL = "('filled','partial','unmatched','cancelled')"


def record(*, mode: str, client: str, agent_id: str, req: dict, ok: bool,
           message: str, title: str = "", forecast_id: Optional[int] = None,
           committed: Optional[float] = None, order_id: Optional[str] = None,
           paper_local_id: Optional[int] = None, filled: Optional[float] = None,
           avg: Optional[float] = None, status: Optional[str] = None) -> int:
    import db
    with db.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO mcp_orders
               (created_at, network, mode, client, agent_id, condition_id, title,
                side, action, order_type, size, price_cents, forecast_id,
                committed_usd, ok, order_id, paper_local_id, filled,
                avg_fill_cents, message, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now_sql(), _net(), mode, (client or "")[:80], agent_id or DEFAULT_AGENT,
             req.get("conditionId") or "", (title or "")[:300],
             req.get("side") or "", req.get("action") or "",
             req.get("orderType") or "GTC", float(req.get("size") or 0.0),
             float(req.get("priceCents") or 0.0), forecast_id,
             round(committed, 4) if committed is not None else None,
             1 if ok else 0, order_id, paper_local_id, filled, avg,
             (message or "")[:500], status),
        )
        return int(cur.lastrowid)


def spent_today(mode: str, exclude_id: Optional[int] = None,
                agent_id: Optional[str] = None) -> float:
    """What agents have committed to BUYS since 00:00 UTC in `mode`.

    Counted at the worst-case cost of every buy that went out, is waiting for
    approval (inside its window), is being approved right now, or errored in a
    way that may have reached the venue. A buy whose fate is final (killed,
    cancelled) counts what it filled — its committed_usd is cut to that when
    it is booked — and a refused or voided one counts nothing. Pessimistic on purpose: the cap is a
    ceiling, not an estimate. `agent_id` None = all agents together (the
    global cap); an id = that agent's own spend (its own cap). `exclude_id` is
    the approval being re-vetted, so it is not counted against itself."""
    import db
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(committed_usd), 0) FROM mcp_orders "
            "WHERE (ok=1 OR status IN ('deciding','unknown') OR " + _LIVE_PENDING_SQL + ") "
            "AND action='buy' AND mode=? AND network=? "
            "AND substr(created_at,1,10)=? AND id<>? " + _AGENT_SQL,
            (_pending_cutoff(), mode, _net(), today(),
             int(exclude_id) if exclude_id is not None else -1,
             agent_id, agent_id)).fetchone()
    return float(row[0] or 0.0)


def effective_status(r: Any) -> Optional[str]:
    st = r["status"]
    if st == "pending" and (age_sec(r["created_at"]) or 0) > APPROVAL_TTL_SEC:
        return "expired"
    if st == "deciding" and (age_sec(r["created_at"]) or 0) > APPROVAL_TTL_SEC + DECIDING_GRACE_SEC:
        return "failed"
    return st


def _effective_message(r: Any) -> str:
    if r["status"] == "deciding" and effective_status(r) == "failed":
        return ("approval never finished (the app stopped mid-send?) — check your "
                "Polymarket orders before approving anything like it again")
    return r["message"] or ""


def activity(limit: int = 100, agent_id: Optional[str] = None) -> list[dict]:
    import db
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM mcp_orders WHERE 1=1 " + _AGENT_SQL +
            "ORDER BY id DESC LIMIT ?", (agent_id, agent_id, int(limit))).fetchall()
    try:
        names = {a["id"]: a["name"] for a in mcp_agents.agents(cfg())}
    except Exception:  # noqa: BLE001
        names = {}
    return [{
        "id": int(r["id"]), "at": r["created_at"], "mode": r["mode"],
        "client": r["client"] or None, "agentId": r["agent_id"],
        # A deleted agent's rows keep their id; the name is gone with it.
        "agentName": names.get(r["agent_id"]),
        "conditionId": r["condition_id"], "title": r["title"] or None,
        "side": r["side"], "action": r["action"], "orderType": r["order_type"],
        "size": float(r["size"]), "priceCents": float(r["price_cents"]),
        "forecastId": r["forecast_id"], "committedUsd": r["committed_usd"],
        "ok": bool(r["ok"]), "orderId": r["order_id"], "filled": r["filled"],
        "avgFillCents": r["avg_fill_cents"], "message": _effective_message(r),
        "status": effective_status(r),
    } for r in rows]


def pending(limit: int = 50) -> list[dict]:
    return [a for a in activity(limit * 4) if a["status"] == "pending"][:limit]


# ────────────────────────────────────────────────────────────────────────
# Live ownership — what an agent may sell is what its own orders FILLED
# ────────────────────────────────────────────────────────────────────────


def _agent_standing(agent_id: Optional[str]) -> list[dict]:
    """The agent's live stops / take-profits (terminal_trade's standing list,
    tagged with agentId when an agent added them)."""
    import terminal_trade
    out = []
    for e in terminal_trade.standing_list():
        if e.get("origin") != "ai":
            continue
        aid = str(e.get("agentId") or DEFAULT_AGENT)
        if agent_id is None or aid == agent_id:
            out.append(e)
    return out


def live_net(cid: str, side: str, agent_id: str = DEFAULT_AGENT) -> float:
    """Shares of (cid, side) this agent may SELL live.

    Buys count what they actually FILLED on the venue's own ledger
    (terminal_fills by order id), never what they asked for: a resting or
    cancelled buy opened nothing, and counting it would let the agent sell
    the user's own shares from the same account. A sell whose fate is FINAL
    (a fill-and-kill whose fill is known, an unmatched one, a cancelled one)
    counts what it filled; a sell that could still fill (a resting GTC, a
    fill-and-kill whose fill the reply did not state) and an error that may
    have reached the venue count their full size. A live stop the agent set
    reserves what it still has to sell while armed, and what fired stops
    actually SOLD counts as sold (terminal_trade's durable sold record)."""
    import db
    import terminal_trade
    net = _net()
    with db.get_db() as conn:
        buys = conn.execute(
            "SELECT o.order_id, COALESCE(o.filled, 0) AS filled, "
            "  (SELECT COALESCE(SUM(f.size), 0) FROM terminal_fills f "
            "   WHERE f.order_id = o.order_id) AS ledger "
            "FROM mcp_orders o WHERE " + _LIVE_SENT_SQL + " AND o.mode='live' "
            "AND o.network=? AND o.action='buy' AND o.condition_id=? AND o.side=? "
            "AND o.agent_id=? AND o.order_id IS NOT NULL",
            (net, cid, side, agent_id)).fetchall()
        sells = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN o.status IN " + _FINAL_SQL + " "
            "  THEN MAX(COALESCE(o.filled, o.size), "
            "           (SELECT COALESCE(SUM(f.size), 0) FROM terminal_fills f "
            "            WHERE o.order_id IS NOT NULL AND f.order_id = o.order_id)) "
            "  ELSE o.size END), 0) "
            "FROM mcp_orders o WHERE (o.ok=1 OR o.status='unknown') AND o.mode='live' "
            "AND o.network=? AND o.action='sell' AND o.condition_id=? AND o.side=? "
            "AND o.agent_id=?", (net, cid, side, agent_id)).fetchone()
    stops = [e for e in _agent_standing(agent_id)
             if e.get("conditionId") == cid and e.get("side") == side
             and e.get("kind") in ("stopLoss", "takeProfit")]
    fired = terminal_trade.standing_sold_shares(cid, side, agent_id=agent_id,
                                                default_agent_id=DEFAULT_AGENT)
    armed = sum(terminal_trade.standing_remaining(e) for e in stops
                if e.get("armed") and not e.get("firedAt"))
    bought = sum(max(float(r["filled"] or 0.0), float(r["ledger"] or 0.0)) for r in buys)
    return max(0.0, round(bought - float(sells[0] or 0.0) - fired - armed, 6))


def live_exposure(cid: str, agent_id: str, exclude_id: Optional[int] = None) -> float:
    """Shares this agent has in (or could still get into) one market, for its
    per-market cap. Errs HIGH: a buy counts its full size until its fate is
    known (then what it filled), queued and errored buys count in full, a
    sell takes off only what it is known to have filled. A refused order is
    not in it at all."""
    import db
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE "
            "  WHEN action='buy' AND status IN " + _FINAL_SQL + " THEN COALESCE(filled, 0) "
            "  WHEN action='buy' THEN size "
            "  WHEN ok=1 THEN -COALESCE(filled, 0) "
            "  ELSE 0 END), 0) "
            "FROM mcp_orders WHERE (ok=1 OR status IN ('deciding','unknown') OR "
            + _LIVE_PENDING_SQL + ") "
            "AND mode='live' AND network=? AND condition_id=? AND id<>? AND agent_id=?",
            (_pending_cutoff(), _net(), cid,
             int(exclude_id) if exclude_id is not None else -1, agent_id)).fetchone()
    return max(0.0, float(row[0] or 0.0))


def agent_order_ids(agent_id: Optional[str] = None) -> set[str]:
    """Venue order ids an agent (None = any) sent — an 'unknown' one with an
    id too: it may be on the book, and the agent must be able to cancel it."""
    import db
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT order_id FROM mcp_orders o WHERE " + _LIVE_SENT_SQL +
            " AND mode='live' AND network=? AND order_id IS NOT NULL " + _AGENT_SQL,
            (_net(), agent_id, agent_id)).fetchall()
    return {r[0] for r in rows}


def _held_candidates(agent_id: Optional[str]) -> set[str]:
    """Markets an agent (None = any) still holds or could still get into,
    from its own ledger: an order still resting or whose fate is unknown, a
    buy waiting for approval, or its own fills netting more than zero
    shares. A buy that is known to have filled nothing (refused, unmatched,
    cancelled unfilled) holds no slot; nor does a position it sold out of."""
    import db
    import terminal_trade
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT o.agent_id, o.condition_id, o.side, o.action, o.order_type, o.size, "
            "  o.filled, o.status, o.ok, o.order_id, "
            "  (SELECT COALESCE(SUM(f.size), 0) FROM terminal_fills f "
            "   WHERE o.order_id IS NOT NULL AND f.order_id = o.order_id) AS ledger "
            "FROM mcp_orders o "
            "WHERE (o.ok=1 OR o.status IN ('deciding','unknown') OR "
            "  (o.status='pending' AND julianday(o.created_at) > julianday('now', ?))) "
            "AND o.mode='live' AND o.network=? "
            "AND (? IS NULL OR o.agent_id=?)",
            (_pending_cutoff(), _net(), agent_id, agent_id)).fetchall()
    held: set[str] = set()
    net: dict[tuple, float] = {}
    for r in rows:
        cid, st = r["condition_id"], r["status"]
        got = max(float(r["filled"] or 0.0), float(r["ledger"] or 0.0))
        key = (r["agent_id"] or DEFAULT_AGENT, cid, r["side"])
        if r["action"] == "buy":
            if st in ("pending", "deciding", "unknown"):
                held.add(cid)          # may be on the book: fail closed
            elif st not in _FINAL and got < float(r["size"] or 0.0) - 1e-9:
                held.add(cid)          # a GTC that may still rest (or an unstated FAK fill)
            if r["ok"] or (st == "unknown" and r["order_id"]):
                net[key] = net.get(key, 0.0) + got
        elif r["ok"]:
            # Only what a sell is KNOWN to have sold takes a share off.
            net[key] = net.get(key, 0.0) - got
    for (aid, cid, side), n in net.items():
        if cid in held:
            continue
        n -= terminal_trade.standing_sold_shares(cid, side, agent_id=aid,
                                                 default_agent_id=DEFAULT_AGENT)
        if n > 1e-9:
            held.add(cid)
    return held


async def live_held_markets(agent_id: Optional[str] = None) -> set[str]:
    """Markets an agent (None = any) holds live, for the position caps: its
    own fills net more than zero shares there, or an order of its may still
    rest or fill (resting GTC, unknown fate, waiting for approval). It errs
    toward counting; a market that resolved drops out."""
    import terminal_trade
    mine = _held_candidates(agent_id)
    if not mine:
        return set()
    try:
        res = await terminal_trade._resolutions_for(list(mine))
    except Exception:  # noqa: BLE001 — fail closed: count them all
        return mine
    return {c for c in mine if res.get(c) not in ("yes", "no")}


# A preview may reuse a ledger read this recent; place, approve and every
# exit read it fresh. One read asks the venue about every order still open, and
# an agent previewing market after market would ask for each one — the owner's
# live test (60 previews) got the exchange refusing the app's own reads, and every
# market came back with no bid or ask.
PREVIEW_FILLS_MAX_AGE = 30.0
_FILLS: Optional[list] = None       # [loop, lock, monotonic time of the last good read]


async def refresh_fills(max_age: float = 0.0) -> bool:
    """Pull the venue's fill ledger before an ownership or loss decision;
    → whether it was read. A failure leaves the stored counts, which only
    ever UNDER-state a buy's fills — so it can block an exit, never widen
    one. The loss stop reads a failure as an unreadable ledger (vet).

    One read at a time: a caller that waited on another's read, or passes a
    `max_age` the last good read is inside, uses that read."""
    global _FILLS
    if not authed():
        return False
    import time
    import terminal_trade
    loop = asyncio.get_running_loop()
    if _FILLS is None or _FILLS[0] is not loop:
        _FILLS = [loop, asyncio.Lock(), None]
    st = _FILLS
    async with st[1]:
        if max_age > 0 and st[2] is not None and time.monotonic() - st[2] < max_age:
            return True
        try:
            res = await terminal_trade.reconcile_fills()
        except Exception as e:  # noqa: BLE001
            logger.debug("mcp: fill refresh failed: %s", e)
            return False
        # A ledger it could not read comes back as a result, not an exception.
        ok = not (isinstance(res, dict) and res.get("ok") is False)
        if ok:
            st[2] = time.monotonic()
            try:
                promote_from_ledger()
            except Exception as e:  # noqa: BLE001
                logger.debug("mcp: unknown-order promotion failed: %s", e)
            try:
                await sync_venue_orders()
            except Exception as e:  # noqa: BLE001
                logger.debug("mcp: venue order sync failed: %s", e)
        return ok


# ────────────────────────────────────────────────────────────────────────
# Keeping the live ledger true: fills found, cancels found, unknowns settled
# ────────────────────────────────────────────────────────────────────────


def _book_final(conn: Any, r: Any, filled: float, status: str,
                message: Optional[str] = None) -> None:
    """Close one live row at what it FILLED: nothing of it can fill any more.
    A buy's committed spend drops to the filled share of it (an upper bound —
    the limit and the fee at the limit), so a cancelled or killed buy stops
    holding the daily cap it never spent. Sent orders become ok=1 (an
    'unknown' found on the venue was sent)."""
    size = float(r["size"] or 0.0)
    # Not clamped to size: a marketable buy spends size x limit in dollars and
    # takes MORE shares when it fills below the limit (6 @ 20¢ -> 7.06 @ 17¢,
    # live 2026-10-12). The share count is what the agent holds; the spend is
    # never more than the order's own worst case, hence the capped ratio.
    filled = max(0.0, float(filled or 0.0))
    committed = r["committed_usd"]
    if r["action"] == "buy" and committed is not None and size > 0:
        committed = round(float(committed) * min(1.0, filled / size), 4)
    conn.execute(
        "UPDATE mcp_orders SET ok=1, status=?, filled=?, committed_usd=?, "
        "message=COALESCE(?, message) WHERE id=?",
        (status, round(filled, 6), committed,
         (message or None) and str(message)[:500], int(r["id"])))


def _fak_status(filled: float, size: float) -> str:
    if filled <= 1e-9:
        return "unmatched"
    return "filled" if filled >= size - 1e-9 else "partial"


def promote_from_ledger() -> int:
    """'unknown' live rows whose order id now has fills on the venue's own
    ledger: the order WAS placed. A fill-and-kill is closed at what filled; a
    GTC becomes a sent order (it may still rest — it keeps counting in full
    until it fills or is cancelled). Called after every successful fill
    reconcile; → rows promoted."""
    import db
    n = 0
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT o.*, (SELECT SUM(f.size) FROM terminal_fills f "
            "             WHERE f.order_id = o.order_id) AS ledger "
            "FROM mcp_orders o WHERE o.status='unknown' AND o.mode='live' "
            "AND o.network=? AND o.order_id IS NOT NULL", (_net(),)).fetchall()
        for r in rows:
            got = float(r["ledger"] or 0.0)
            if got <= 0:
                continue
            note = (f"found on Polymarket's ledger: {got:g} filled — "
                    + str(r["message"] or ""))
            if str(r["order_type"] or "GTC").upper() in ("FAK", "FOK"):
                _book_final(conn, r, got, _fak_status(got, float(r["size"] or 0.0)), note)
            else:
                conn.execute("UPDATE mcp_orders SET ok=1, status=NULL, filled=?, "
                             "message=? WHERE id=?",
                             (round(got, 6), note[:500], int(r["id"])))
            n += 1
    if n:
        logger.info("mcp: %d unknown agent order(s) found on the ledger", n)
    return n


def _venue_order(order: Any) -> Optional[dict]:
    """An order view (`polymarket_api.get_order`) → {state, matched} where state
    is resting | matched | cancelled, or None when it says nothing usable."""
    if not isinstance(order, dict) or not order:
        return None
    state = {"live": "resting", "matched": "matched",
             "canceled": "cancelled"}.get(str(order.get("status") or ""))
    if state is None:
        return None
    return {"state": state, "matched": _num(order.get("size_matched"))}


# Venue-side cancels (the user's other device, a market resolving, the
# engines' wallet-wide sweeps) never pass terminal_trade.cancel. A fill
# reconcile looks at most this many open agent orders, at most this often.
VENUE_SYNC_EVERY_SEC = 60.0
VENUE_SYNC_MIN_AGE_SEC = 60
VENUE_SYNC_MAX = 10
_VENUE_SYNC: dict = {"at": None}


async def sync_venue_orders(force: bool = False) -> int:
    """Ask the venue about the agents' open orders and book the ones that are
    no longer open: cancelled → cancelled at what filled; fully matched →
    filled. An order the venue cannot find (404) or answers unclearly is left
    as it is — a full match can drop off the open-order endpoint as well.
    → rows booked."""
    import time

    import db
    import polymarket_api as api
    now = time.monotonic()
    last = _VENUE_SYNC.get("at")
    if not force and last is not None and now - last < VENUE_SYNC_EVERY_SEC:
        return 0
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT o.*, (SELECT COALESCE(SUM(f.size), 0) FROM terminal_fills f "
            "             WHERE f.order_id = o.order_id) AS ledger "
            "FROM mcp_orders o WHERE " + _LIVE_SENT_SQL + " AND o.mode='live' "
            "AND o.network=? AND o.order_id IS NOT NULL "
            "AND (o.status IS NULL OR o.status IN ('approved','unknown')) "
            "AND julianday(o.created_at) < julianday('now', ?) "
            "ORDER BY o.id DESC LIMIT ?",
            (_net(), f"-{int(VENUE_SYNC_MIN_AGE_SEC)} seconds",
             VENUE_SYNC_MAX * 3)).fetchall()
    # A placeholder (a lost reply, which loses the id too) names nothing to ask about.
    todo = [r for r in rows
            if not str(r["order_id"]).startswith(api.UNKNOWN_ID_PREFIX)
            and max(float(r["filled"] or 0.0), float(r["ledger"] or 0.0))
            < float(r["size"] or 0.0) - 1e-9][:VENUE_SYNC_MAX]
    _VENUE_SYNC["at"] = now
    booked = 0
    for r in todo:
        try:
            resp = await api.get_order(str(r["order_id"]))
        except Exception as e:  # noqa: BLE001 — 404 or unreadable: leave it
            logger.debug("mcp: venue order %s unreadable: %s", r["order_id"], e)
            continue
        v = _venue_order((resp or {}).get("order") if isinstance(resp, dict) else None)
        if v is None:
            continue
        got = max(float(v["matched"] or 0.0), float(r["ledger"] or 0.0))
        with db.get_db() as conn:
            if v["state"] == "cancelled":
                _book_final(conn, r, got, "cancelled",
                            "cancelled on Polymarket — booked at what filled")
            elif v["state"] == "matched":
                _book_final(conn, r, float(r["size"] or 0.0) if v["matched"] is None else got,
                            "filled")
            elif r["status"] == "unknown":
                # Resting on the book: it was placed. A sent order now.
                conn.execute("UPDATE mcp_orders SET ok=1, status=NULL, filled=? "
                             "WHERE id=?", (round(got, 6), int(r["id"])))
            else:
                continue
        booked += 1
    return booked


async def settle_unknown(row_id: Any) -> dict:
    """The USER settles one live agent order booked 'unknown' (RPC
    mcpSettleUnknown). It is not an agent tool.

    'unknown' fails closed: it holds the daily cap, a position slot, and
    (a sell) the agent's sell allowance, because the order may be on the
    venue. This asks Polymarket and books what it finds:

      * an order id the venue knows: resting → sent; cancelled or matched →
        closed at what filled (its fills on the ledger, or the venue's own
        matched size, whichever is more);
      * an id the venue cannot find with no fill on the ledger → 'void';
      * no id (the send blew up before one came back, which on this venue is
        every lost reply: it assigns the id, so what we hold is a placeholder):
        'void' only when the account's trades and the open-order list read
        cleanly and neither shows an order or fill in that market this app
        cannot attribute — anything that could be it refuses the settle and
        says what to look at.

    Never guesses: an unreadable ledger or order list refuses."""
    import db
    import polymarket_api as api
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        return {"ok": False, "message": "No such agent order."}
    async with money_lock():
        with db.get_db() as conn:
            r = conn.execute("SELECT * FROM mcp_orders WHERE id=?", (rid,)).fetchone()
        if r is None or r["mode"] != "live":
            return {"ok": False, "message": f"No live agent order #{rid}."}
        if r["status"] != "unknown":
            return {"ok": False, "message": f"Agent order #{rid} is not unknown "
                                            f"({effective_status(r) or 'sent'})."}
        if (r["network"] or "") != _net():
            return {"ok": False, "message": f"Agent order #{rid} was sent on another "
                                            f"network; switch to it to settle it."}
        if not authed():
            return {"ok": False, "message": "Add your API key first: settling asks "
                                            "Polymarket what became of the order."}
        if not await refresh_fills():
            return {"ok": False, "message": "Could not read Polymarket's fill ledger, so "
                                            "nothing was changed. Try again in a moment."}
        with db.get_db() as conn:
            r = conn.execute("SELECT * FROM mcp_orders WHERE id=?", (rid,)).fetchone()
        if r is None or r["status"] != "unknown":
            # The refresh just found it (its fills, or the venue's answer).
            return {"ok": True, "status": effective_status(r) if r else None,
                    "message": f"Agent order #{rid} was found on Polymarket and is "
                               f"booked at what it filled."}
        oid = r["order_id"]
        # A placeholder names nothing on the venue. Asking about it would read as
        # "no such order" and void an order that may well have filled.
        if oid and not str(oid).startswith(api.UNKNOWN_ID_PREFIX):
            gone = False
            v = None
            try:
                resp = await api.get_order(str(oid))
                v = _venue_order((resp or {}).get("order") if isinstance(resp, dict) else None)
            except api.PolymarketAPIError as e:
                if int(getattr(e, "status", 0) or 0) != 404:
                    return {"ok": False, "message": f"Polymarket did not answer about order "
                                                    f"{str(oid)[:10]}… ({e}). Nothing changed."}
                gone = True
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"Could not ask Polymarket about order "
                                                f"{str(oid)[:10]}… ({type(e).__name__}). "
                                                f"Nothing changed."}
            ledger = _ledger_fill(oid)
            with db.get_db() as conn:
                if v is not None and v["state"] == "resting":
                    conn.execute("UPDATE mcp_orders SET ok=1, status=NULL, filled=?, "
                                 "message=? WHERE id=?",
                                 (round(max(ledger, float(v["matched"] or 0.0)), 6),
                                  "settled by the user: it is resting on Polymarket",
                                  rid))
                    st, msg = None, "It is resting on Polymarket's book: booked as sent."
                elif v is not None:
                    got = max(ledger, float(v["matched"] or 0.0))
                    st = "cancelled" if v["state"] == "cancelled" else "filled"
                    _book_final(conn, r, got, st,
                                f"settled by the user: {st} on Polymarket, {got:g} filled")
                    msg = f"Polymarket has it {st} with {got:g} filled: booked at that."
                elif gone and ledger <= 0:
                    _void(conn, rid, "settled by the user: Polymarket has no such order "
                                     "and no fill")
                    st, msg = "void", ("Polymarket has no such order and no fill: it "
                                       "counts nowhere now.")
                elif gone:
                    # Off the open-order endpoint with fills: matched in full,
                    # or a remainder that is no longer on the book either way.
                    st = ("filled" if ledger >= float(r["size"] or 0.0) - 1e-9
                          else "cancelled")
                    _book_final(conn, r, ledger, st,
                                f"settled by the user: off the book, {ledger:g} filled")
                    msg = f"Off Polymarket's book with {ledger:g} filled: booked at that."
                else:
                    return {"ok": False, "message": "Polymarket's answer about this order "
                                                    "was unclear. Nothing changed."}
        else:
            stray = await _unattributed_activity(r)
            if stray:
                return {"ok": False, "message": stray}
            with db.get_db() as conn:
                _void(conn, rid, "settled by the user: Polymarket shows no order and no "
                                 "fill from it")
            st, msg = "void", ("Polymarket shows no such order and no fill: it counts "
                               "nowhere now.")
    logger.info("mcp: unknown agent order #%s settled → %s", rid, st)
    return {"ok": True, "status": st, "message": f"Agent order #{rid}: {msg}"}


def _void(conn: Any, rid: int, message: str) -> None:
    conn.execute("UPDATE mcp_orders SET ok=0, status='void', filled=0, "
                 "committed_usd=NULL, message=? WHERE id=?", (message[:500], rid))


def _ledger_fill(order_id: str) -> float:
    import db
    with db.get_db() as conn:
        v = conn.execute("SELECT COALESCE(SUM(size), 0) FROM terminal_fills "
                         "WHERE order_id=?", (str(order_id),)).fetchone()[0]
    return float(v or 0.0)


# How far before an order's send time its fills are looked for: our clock and the
# venue's are not the same clock.
_SETTLE_CLOCK_SLACK_SEC = 120.0


async def _account_trades_since(slug: str, since_ts: Optional[float]) -> float:
    """Contracts this account traded in this market since `since_ts`, from the
    venue's activity list. Raises when it cannot say: `api.get_activity` swallows
    a failed read and returns what it has, and an empty list read as "no trades"
    here would void an order that filled."""
    import polymarket_api as api
    import us_api as us
    total = 0.0
    cursor = ""
    for _ in range(10):
        params: dict = {"limit": 200, "sortOrder": "SORT_ORDER_DESCENDING",
                        "marketSlug": slug}
        if cursor:
            params["cursor"] = cursor
        data = await api._authed_request("GET", "/v1/portfolio/activities", params=params)
        rows = data.get("activities") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise api.PolymarketAPIError(0, "the activity list came back unreadable")
        for a in rows:
            row = us.activity_row(a)
            if not row or row["type"] != "TRADE" or row.get("conditionId") != slug:
                continue
            if since_ts is not None and float(row["timestamp"]) < since_ts:
                return total
            total += float(row["size"])
        cursor = str(data.get("nextCursor") or "")
        if not cursor:
            return total
    raise api.PolymarketAPIError(0, "the activity list is longer than we read")


async def _unattributed_activity(r: Any) -> Optional[str]:
    """Why an id-less 'unknown' cannot be settled yet — an order resting in its
    market and side, or a trade in its market since it was sent, that this app
    cannot tie to any order of its own — or None when the venue shows nothing
    that could be it. An unreadable read is a reason too.

    A trade activity has no order id, no side and no intent, so no single trade
    can be tied to an order. What can be compared is the quantity: everything this
    account traded in the market since the send, against what the orders we did
    get an id for filled. More traded than that is a fill nobody here can explain.
    An engine or the Polymarket US app trading the same market reads the same way;
    that fails closed, because the alternative is voiding an order that filled."""
    import db
    import terminal_trade
    rest = await terminal_trade.resting_orders(r["condition_id"])
    note = str(rest.get("note") or "")
    if note.startswith("Could not"):
        return f"Could not read your open orders ({note}). Nothing changed."
    for o in rest.get("orders") or []:
        if (o.get("origin") == "external" and o.get("side") == r["side"]
                and o.get("action") == r["action"]):
            return ("An order this app did not record is resting in that market "
                    f"(order {str(o.get('orderId') or '')[:10]}…). It may be this one: "
                    "check it on Polymarket, cancel it if you do not want it, then "
                    "settle again.")
    try:
        since = datetime.strptime(str(r["created_at"])[:19].replace("T", " "),
                                  "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        since_ts = since.timestamp() - _SETTLE_CLOCK_SLACK_SEC
    except ValueError:
        since_ts = None
    try:
        traded = await _account_trades_since(str(r["condition_id"]), since_ts)
    except Exception as e:  # noqa: BLE001 — unread is not "no trades"
        return (f"Could not read your trades in that market ({e}). Nothing changed.")
    # Fills of orders sent since then. An older order's aggregate can hold fills
    # from before the window, and counting those could hide the stray fill.
    with db.get_db() as conn:
        known = conn.execute(
            "SELECT COALESCE(SUM(f.size), 0) FROM terminal_fills f "
            "JOIN terminal_orders o ON o.order_id = f.order_id "
            "WHERE f.condition_id=? AND f.network=? "
            "AND (? IS NULL OR strftime('%s', o.created_at) >= ?)",
            (r["condition_id"], _net(), since_ts,
             None if since_ts is None else str(int(since_ts)))).fetchone()[0]
    if traded > float(known or 0.0) + 1e-6:
        return ("This account traded more in that market since the order was sent than "
                "this app's orders account for. It may be this one, so it stays unknown "
                "— check Track and your Polymarket history.")
    return None


# ────────────────────────────────────────────────────────────────────────
# Daily loss stop
# ────────────────────────────────────────────────────────────────────────


def _agent_books(c: dict) -> list[str]:
    import terminal_paper
    books = {book_of(a) for a in mcp_agents.agents(c)}
    books |= {b for b in terminal_paper.books_in_use() if terminal_paper.is_agent_book(b)}
    return sorted(books)


async def day_loss(mode: str, c: dict) -> dict:
    """Today's agent P&L (UTC), every agent together. The stop (`lossUsd`) counts
    what is realised today: closes, settlements and fees. Open positions are
    still valued here for display, but a mark is not a result until the market
    closes. So the spend cap bounds what goes OUT in a day, and this bounds what
    has been LOST so far, which can lag what is at risk.

    Live, the agents' positions are replayed from their own fills on the venue's
    ledger; a read that fails sets `positionsUnreadable`, and the vet then refuses
    buys (exits are never blocked)."""
    d = {"realizedUsd": 0.0, "unrealizedUsd": 0.0, "openLossUsd": 0.0,
         "unmarked": [], "unmarkedCostUsd": 0.0}
    day = today()
    if mode == "paper":
        import terminal_paper
        for b in _agent_books(c):
            with terminal_paper.using_book(b):
                p = await terminal_paper.day_pnl(day)
            d["realizedUsd"] += p["realizedUsd"]
            d["unrealizedUsd"] += p["unrealizedUsd"]
            d["openLossUsd"] += p["openLossUsd"]
            d["unmarked"] += p["unmarked"]
            d["unmarkedCostUsd"] += p["unmarkedCostUsd"]
    else:
        try:
            d = await _live_day_pnl(day)
        except Exception as e:  # noqa: BLE001
            logger.debug("mcp: live loss read failed: %s", e)
            d["positionsUnreadable"] = True
    d["lossUsd"] = round(max(0.0, -d["realizedUsd"]), 2)
    for k in ("realizedUsd", "unrealizedUsd", "openLossUsd", "unmarkedCostUsd"):
        d[k] = round(float(d[k]), 2)
    return d


async def _live_day_pnl(day: str) -> dict:
    """Replay every agent's own live fills (all agents together) by average
    cost; realised P&L is what was closed today, fees paid today count as
    lost, and what is still open is valued at the bid. A market that resolved
    is realised at its payout while its close date is today, win or lose, and
    drops out after (that result was yesterday's)."""
    import db
    import terminal as term
    with db.get_db() as conn:
        fills = conn.execute(
            "SELECT f.condition_id, f.side, f.action, f.size, f.price_cents, "
            "f.fees_usd, f.filled_at FROM terminal_fills f "
            # An 'unknown' order with an id counts too: a lost reply is
            # exactly when a fill can land unseen, and the stop must see it.
            "WHERE f.order_id IN (SELECT order_id FROM mcp_orders o WHERE "
            + _LIVE_SENT_SQL + " AND mode='live' AND network=? "
            "  AND order_id IS NOT NULL) "
            "ORDER BY f.filled_at ASC", (_net(),)).fetchall()
    pos: dict[tuple, dict] = {}
    realized = 0.0
    for f in fills:
        key = (f["condition_id"], f["side"])
        st = pos.setdefault(key, {"size": 0.0, "cost": 0.0})
        sz, px = float(f["size"] or 0.0), float(f["price_cents"] or 0.0)
        fee = float(f["fees_usd"] or 0.0)
        is_today = str(f["filled_at"] or "")[:10] == day
        if f["action"] == "buy":
            st["size"] += sz
            st["cost"] += sz * px / 100.0
            if is_today:
                realized -= fee
        else:
            avg = st["cost"] / st["size"] if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                if is_today:
                    realized += close * (px / 100.0 - avg) - fee
                st["cost"] -= close * avg
                st["size"] -= close
    open_keys = [(k, st) for k, st in pos.items() if st["size"] > 1e-9]
    marks: dict[str, dict] = {}
    if open_keys:
        for m in await term.marked_summaries([cid for (cid, _), _ in open_keys]):
            marks[m["conditionId"]] = m
    unreal = open_loss = unmarked_cost = 0.0
    unmarked: list[str] = []
    for (cid, side), st in open_keys:
        m = marks.get(cid) or {}
        resolved = m.get("resolvedOutcome")
        if resolved in ("yes", "no"):
            if str(m.get("closeTime") or "")[:10] != day:
                continue
            # Paid out, so this is money, not a mark. Counted as an open loss
            # it could only ever hurt: a settled winner offset nothing and the
            # stop read $6.13 on a $5.71 day (2026-11-06).
            realized += st["size"] * (100.0 if resolved == side else 0.0) / 100.0 - st["cost"]
            continue
        elif side == "yes":
            bid = _num(m.get("bid"))
        else:
            ask = _num(m.get("ask"))
            bid = None if ask is None else 100.0 - ask
        if bid is None:
            unmarked.append(cid)
            unmarked_cost += st["cost"]
        else:
            u = st["size"] * bid / 100.0 - st["cost"]
            unreal += u
            open_loss += max(0.0, -u)
    return {"realizedUsd": realized, "unrealizedUsd": unreal, "openLossUsd": open_loss,
            "unmarked": unmarked, "unmarkedCostUsd": unmarked_cost}


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


# ────────────────────────────────────────────────────────────────────────
# Market reads
# ────────────────────────────────────────────────────────────────────────


async def market(cid: str) -> Optional[dict]:
    """One market for an agent, quoted from the live book: its bid/ask are
    a forecast's benchmark and the edges the agent is shown."""
    import terminal as term
    m = await term.market(cid, include_raw=True)
    if m:
        await term.clob_quotes([m])
    return m


def category(m: Optional[dict]) -> Optional[str]:
    """The market's category from the one Polymarket states, or None. Its tags
    are empty on most markets, so they say nothing here."""
    import terminal as term
    return term.venue_category((m or {}).get("_raw") or {})


def fee_cents(m: Optional[dict], price_cents: float) -> Optional[float]:
    """Polymarket's taker fee per share at this price, in cents, or None when
    it is unknown — from the market's own fee coefficient."""
    import polymarket_api
    import terminal_trade
    sched = polymarket_api._fee_schedule_from_gamma((m or {}).get("_raw") or {})
    ps = terminal_trade._fee_per_share(price_cents, (m or {}).get("category"), sched)
    return None if ps is None else ps * 100.0


def edge_cents(fair_yes_c: float, side: str, price_c: float,
               fee_c: Optional[float]) -> Optional[float]:
    """What the forecast says one share of this order is worth, after fee."""
    if fee_c is None:
        return None
    p_side = fair_yes_c if side == "yes" else 100.0 - fair_yes_c
    return round(p_side - price_c - fee_c, 2)


# ────────────────────────────────────────────────────────────────────────
# Vet
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Vetted:
    req: dict
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    market: Optional[dict] = None
    preview: Optional[dict] = None
    forecast_id: Optional[int] = None
    edge: Optional[float] = None
    cost: Optional[float] = None


def order_req(args: dict) -> dict:
    side = str(args.get("side") or "").lower()
    action = str(args.get("action") or "").lower()
    if side not in ("yes", "no"):
        raise Refused("side must be 'yes' or 'no'.")
    if action not in ("buy", "sell"):
        raise Refused("action must be 'buy' or 'sell'.")
    ot = str(args.get("orderType") or "GTC").upper()
    if ot not in ("GTC", "FAK", "FOK"):
        raise Refused("orderType must be GTC, FAK or FOK.")
    cid = str(args.get("conditionId") or "").strip()
    if not cid:
        raise Refused("conditionId is required (find one with search_markets).")
    return {"conditionId": cid, "side": side, "action": action,
            "size": _num(args.get("size")), "priceCents": _num(args.get("priceCents")),
            "orderType": ot}


async def vet(args: dict, c: dict, mode: str, agent: dict, *,
              forecast_grace: float = 0.0, exclude_id: Optional[int] = None,
              fills_max_age: float = 0.0) -> Vetted:
    """Every agent rail, for both preview and place. Live orders get the
    terminal's own gates on top (terminal_trade.place re-previews and
    refuses on its own).

    A vet is only a promise while nothing else spends in between: place and
    decide hold money_lock() from this call until the order is sent and
    recorded. preview_order does not, because it spends nothing — and it
    passes `fills_max_age`, since only a decision that spends needs the
    ledger read this instant."""
    import terminal_paper
    import terminal_trade
    aid = agent["id"]
    name = agent_name(agent)
    rules = agent.get("rules") or {}
    req = order_req(args)
    v = Vetted(req=req)
    grails = rails(c)
    r = mcp_agents.effective_rails(grails, agent)

    if mode == "off":
        v.blockers.append("Trading is off for agents. The user enables paper or live "
                          "trading in Krypt PolyBot US → AI Agents.")
        return v
    if req["size"] is None or req["size"] <= 0:
        v.blockers.append("size must be a positive number of shares")
    if req["priceCents"] is None or not (1 <= req["priceCents"] <= 99):
        v.blockers.append("priceCents must be between 1 and 99")
    if v.blockers:
        return v
    if mode == "live":
        # Polymarket's own region verdict (cached, no network): a blocked
        # region refuses every order, close-only refuses buys. Refused here,
        # before anything is signed — the venue would answer 403 anyway.
        try:
            import polymarket_api
            why = polymarket_api.region_order_refusal(req["action"])
        except Exception as e:  # noqa: BLE001 — no verdict refuses nothing
            logger.debug("mcp vet: region read failed: %s", e)
            why = None
        if why:
            v.blockers.append(why)
            return v

    try:
        v.market = await market(req["conditionId"])
    except Exception as e:  # noqa: BLE001
        logger.debug("mcp vet: market read failed: %s", e)
    if not v.market:
        v.blockers.append(f"Could not read market {req['conditionId'][:14]}… from Polymarket.")
        return v
    if v.market.get("resolvedOutcome") is not None or v.market.get("status") in (
            "closed", "settled", "inactive"):
        v.blockers.append(f"This market is {v.market.get('status')}, not open for trading.")
        return v

    # The ticket's own preview: tick snap, minimum size, closed market, the
    # walked fill and fee, and (paper) the agent's own cash.
    paper = mode == "paper"
    if paper:
        with terminal_paper.using_book(book_of(agent)):
            v.preview = await terminal_trade.preview({**req, "paper": True})
    else:
        if req["action"] == "buy" and authed():
            # The ticket reads a balance cached for up to a minute. An agent can
            # buy twice inside that, so its second buy must see the first's debit.
            import trader
            await trader.refresh_balance(c, force=True)
        v.preview = await terminal_trade.preview({**req, "paper": False})
    pv = v.preview
    v.blockers.extend(pv.get("blockers") or [])
    v.warnings.extend(pv.get("warnings") or [])
    # What the venue would actually accept, after tick snap.
    size = float(pv.get("size") or req["size"])
    px = float(pv.get("priceCents") or req["priceCents"])
    req["size"], req["priceCents"] = size, px
    fee_c = fee_cents(v.market, px)

    if req["action"] == "buy":
        fee_usd = (fee_c or 0.0) * size / 100.0
        v.cost = round(size * px / 100.0 + fee_usd, 4)
        f = None
        fid = args.get("forecastId")
        try:
            import forecast_ledger
            f = forecast_ledger.get(int(fid)) if fid is not None else None
        except (TypeError, ValueError):
            f = None
        # Another agent's forecast is answered exactly like one that does not
        # exist: otherwise any agent could walk the ids and read which markets
        # the others are working. Each agent argues its own trades.
        if f is not None and str(f.get("agent_id") or DEFAULT_AGENT) != aid:
            f = None
        if f is None:
            v.blockers.append("A buy needs forecastId from record_forecast on this "
                              "market. Commit to a fair value first.")
        else:
            v.forecast_id = int(f["id"])
            age = age_sec(f.get("created_at"))
            if f["condition_id"] != req["conditionId"]:
                v.blockers.append(f"Forecast {fid} is for a different market.")
            elif age is None or age > FORECAST_TTL_SEC + forecast_grace:
                v.blockers.append(f"Forecast {fid} is older than "
                                  f"{FORECAST_TTL_SEC // 60} minutes. Re-read the "
                                  f"market and record a new one.")
            else:
                v.edge = edge_cents(float(f["prob_yes"]) * 100.0, req["side"], px, fee_c)
                if v.edge is None:
                    v.blockers.append(
                        "Polymarket publishes no fee rate this app can read for "
                        "this market, so the edge after fee cannot be checked. "
                        "No trade.")
                elif v.edge < r["minEdgeCents"]:
                    whose = (f" ({name}'s rules)" if r["minEdgeCents"] > grails["minEdgeCents"]
                             else "")
                    v.blockers.append(
                        f"Your forecast ({float(f['prob_yes']) * 100:g}¢ YES) gives "
                        f"{v.edge:+.2f}¢ per share on {req['side'].upper()} at {px:g}¢ "
                        f"after fees; the minimum is {r['minEdgeCents']:g}¢{whose}. "
                        f"No trade.")
        v.blockers.extend(mcp_agents.entry_blockers(
            agent, side=req["side"], price_cents=px,
            category=category(v.market) if (
                rules.get("categoriesAllow") or rules.get("categoriesDeny")) else None,
            hours_left=mcp_agents.hours_to_close(v.market.get("closeTime"))))
        if grails["maxOrderUsd"] > 0 and v.cost > grails["maxOrderUsd"]:
            v.blockers.append(f"${v.cost:,.2f} (fees included) is over the agent "
                              f"per-order cap of ${grails['maxOrderUsd']:,.2f}. Do not "
                              f"split the order to get under it.")
        elif r["maxOrderUsd"] > 0 and v.cost > r["maxOrderUsd"]:
            v.blockers.append(f"${v.cost:,.2f} (fees included) is over {name}'s "
                              f"per-order cap of ${r['maxOrderUsd']:,.2f}.")
        # The global cap binds every agent together; an agent's own cap binds
        # its own spend. Both under the money lock, so neither races.
        spent = spent_today(mode, exclude_id=exclude_id)
        if grails["dailySpendUsd"] > 0 and spent + v.cost > grails["dailySpendUsd"] + 1e-9:
            v.blockers.append(f"Would bring today's agent spend to "
                              f"${spent + v.cost:,.2f}, over the daily cap of "
                              f"${grails['dailySpendUsd']:,.2f}. It resets at 00:00 UTC.")
        elif rules.get("dailySpendUsd") is not None and r["dailySpendUsd"] > 0:
            mine = spent_today(mode, exclude_id=exclude_id, agent_id=aid)
            if mine + v.cost > r["dailySpendUsd"] + 1e-9:
                v.blockers.append(f"Would bring {name}'s spend today to "
                                  f"${mine + v.cost:,.2f}, over {name}'s daily cap of "
                                  f"${r['dailySpendUsd']:,.2f}.")
        cid = req["conditionId"]
        if paper:
            # The shared cap counts the agents that can still act: a deleted or
            # switched-off agent's frozen paper positions must not block the
            # others for good (they still count toward the loss stop).
            active = {book_of(a) for a in mcp_agents.agents(c) if a.get("enabled", True)}
            held: set = set()
            for b in active:
                with terminal_paper.using_book(b):
                    held |= terminal_paper.open_markets()
            # Resting buys count toward every cap, as they do live: a paper
            # record the agent built past its caps says nothing about live.
            with terminal_paper.using_book(book_of(agent)):
                mine_mk = terminal_paper.open_markets()
                reserved = terminal_paper.reserved_buy_usd()
                have_here = terminal_paper.exposure(cid)
            # The preview's figure is already net of resting buys (the ticket
            # reserves them too); taking them off again would refuse an agent
            # twice the reservation short.
            avail = _num(pv.get("availableUsd"))
            if avail is not None and avail < v.cost - 1e-9:
                v.blockers.append(
                    f"{name}'s free paper cash is ${max(0.0, avail):,.2f} "
                    f"(resting buys hold ${reserved:,.2f}); this costs ${v.cost:,.2f}.")
        else:
            held = await live_held_markets()
            mine_mk = (await live_held_markets(aid)
                       if rules.get("maxOpenPositions") is not None else set())
            have_here = live_exposure(cid, aid, exclude_id)
        cap = grails["maxOpenPositions"]
        if cap > 0 and cid not in held and len(held) >= cap:
            v.blockers.append(f"Agents already hold {len(held)} open markets "
                              f"(cap {cap}).")
        own_cap = rules.get("maxOpenPositions")
        if own_cap is not None and cid not in mine_mk and len(mine_mk) >= own_cap:
            v.blockers.append(
                f"{name}'s rules: no positions — it records forecasts only." if own_cap == 0
                else f"{name}'s rules: at most {own_cap} open position"
                     f"{'s' if own_cap != 1 else ''}; it holds {len(mine_mk)}.")
        per_mkt = rules.get("maxSharesPerMarket")
        if per_mkt is not None and have_here + size > per_mkt + 1e-9:
            v.blockers.append(
                f"{name}'s rules: at most {per_mkt} shares in one market; it has "
                f"{have_here:g} here and this buy adds {size:g}.")
        # Live, the loss is replayed from the agents' fills, which only a
        # reconcile writes: a GTC buy that filled since the last one was
        # invisible here, and the agent kept buying past the stop. Pull them
        # first, as a sell does; a ledger that cannot be pulled is unreadable.
        # (Not connected is refused below on its own.)
        stale = not paper and authed() and not await refresh_fills(fills_max_age)
        dl = await day_loss(mode, c)
        if stale or dl.get("positionsUnreadable"):
            v.blockers.append(
                "Daily loss stop: the agents' open live positions could not be read, "
                "so today's loss is unknown. No new buys until they can be; selling "
                "to exit is still allowed.")
        elif r["dailyLossUsd"] > 0 and dl["lossUsd"] >= r["dailyLossUsd"]:
            v.blockers.append(
                f"Daily loss stop: agents are down ${dl['lossUsd']:,.2f} today (UTC) "
                f"in {mode}, at or over the ${r['dailyLossUsd']:,.2f} limit. No new "
                f"buys until tomorrow; selling to exit is still allowed.")
    else:
        v.cost = _num(pv.get("costUsd"))
        if paper:
            with terminal_paper.using_book(book_of(agent)):
                have = terminal_paper.held(req["conditionId"], req["side"])
            if size > have + 1e-9:
                v.blockers.append(f"{name}'s paper account holds {have:g} "
                                  f"{req['side'].upper()} here; cannot sell {size:g}. "
                                  f"An agent sells only what it opened.")
        else:
            await refresh_fills(fills_max_age)
            have = live_net(req["conditionId"], req["side"], aid)
            if size > have + 1e-9:
                v.blockers.append(
                    f"{name}'s own live buys of {req['side'].upper()} here have "
                    f"{have:g} filled shares it has not sold or set a stop for; it "
                    f"can only sell what it opened. Unfilled or cancelled buys open "
                    f"nothing, and the rest is the user's — or another agent's.")

    if not paper and not authed():
        v.blockers.append("No API key is saved, so no order can be signed.")
    return v


# ────────────────────────────────────────────────────────────────────────
# Place / approve / execute
# ────────────────────────────────────────────────────────────────────────


def _title(v: Vetted) -> str:
    return str((v.market or {}).get("title") or "")


def _clip(s: str, n: int = 60) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _g(x: Any) -> str:
    n = _num(x)
    return "?" if n is None else f"{n:g}"


_LINE_VERB = {"refused": "REFUSED", "unknown": "UNKNOWN — check Polymarket",
              "blocked": "not sent", "unmatched": "nothing filled"}


def _order_line(name: str, req: dict, title: str, text: str,
                status: Optional[str] = None) -> str:
    """One human line for an agent order outcome: the toast, the phone push
    and the OS notification all read it."""
    head = (f"{name}: LIVE {req.get('action') or '?'} {_g(req.get('size'))} "
            f"{str(req.get('side') or '?').upper()} @ {_g(req.get('priceCents'))}¢"
            + (f" in \"{_clip(title)}\"" if title else ""))
    verb = _LINE_VERB.get(status or "")
    return f"{head} — {verb + ': ' if verb else ''}{text}"


def order_event(*, status: str, agent_id: Optional[str], agent_name: str, req: dict,
                title: str, reason: str, ok: bool, line: Optional[str] = None,
                order_id: Optional[str] = None, filled: Optional[float] = None,
                approval_id: Optional[int] = None, mode: str = "live") -> dict:
    """The mcp:order payload for one live agent order outcome.

    `status`: pending (queued for approval) | sent (on the venue; a GTC may
    rest) | filled | partial | unmatched (a fill-and-kill's known outcome) |
    refused (the venue or the ticket placed nothing) | unknown (may be on the
    venue — check before retrying) | blocked (a rail refused it; nothing was
    sent). `message` is the one human line; `reason` the venue's or the
    rail's own words."""
    ev = {
        "kind": "order", "mode": mode, "status": status, "ok": bool(ok),
        "agentId": agent_id, "agentName": agent_name, "title": title or None,
        "conditionId": req.get("conditionId"), "side": req.get("side"),
        "action": req.get("action"), "orderType": req.get("orderType"),
        "size": _num(req.get("size")), "price": _num(req.get("priceCents")),
        "orderId": order_id, "filled": filled, "reason": reason,
        "message": line or _order_line(agent_name, req, title, reason, status),
    }
    if approval_id is not None:
        ev["approvalId"] = approval_id
    return ev


def live_outcome(res: dict, req: dict) -> tuple[bool, Optional[str], Optional[float]]:
    """What one terminal_trade.place / sell_position answer means for the
    agent ledger → (ok, ledger status, filled).

      * 'rejected' (a definite venue or pre-send refusal), a 4xx 'error', or
        a refusal with no status (the ticket's own gate) → 'refused', nothing
        placed, fill 0;
      * 'unknown', or an 'error' that may have reached the venue (5xx, 408,
        a timeout) → 'unknown', fill None;
      * sent: 'unmatched' (a fill-and-kill that crossed nothing) → fill 0; a
        fill-and-kill whose fill the reply states → 'filled' / 'partial' at
        that fill; anything else (a GTC that may rest, an unstated fill) →
        None, counted in full until its fate is known."""
    ok = bool(res.get("ok"))
    st = str(res.get("status") or "").lower() or None
    filled = _num(res.get("filledSize"))
    if not ok:
        if st == "unknown":
            return False, "unknown", None
        if st == "error":
            http = res.get("httpStatus")
            if isinstance(http, int) and 400 <= http < 500 and http != 408:
                return False, "refused", 0.0
            return False, "unknown", None
        return False, "refused", 0.0
    size = float(_num(req.get("size")) or 0.0)
    ot = str(req.get("orderType") or "GTC").upper()
    if st == "unmatched":
        return True, "unmatched", 0.0
    if ot in ("FAK", "FOK") and filled is not None:
        return True, _fak_status(filled, size), filled
    return True, None, filled


async def place(args: dict, session: dict) -> dict:
    """place_order for one agent: vet, then paper fill, queue for approval,
    or send live — all under the money lock. Telling the app and the phone
    happens after the lock is released — for a live refusal too, before the
    agent gets its error."""
    c = cfg()
    agent = require_agent(session, c)
    mode = agent_mode(c, agent)
    if args.get("mode") not in (None, mode):
        raise Refused(f"You trade in {mode.upper()} mode here — the user sets it "
                      f"(global mode and {agent['name']}'s own). Leave mode out or "
                      f"pass '{mode}'.")
    client = str(session.get("client") or "")
    try:
        async with money_lock():
            out, announce = await _place_locked(args, c, mode, client, agent)
    except Refused as e:
        if e.event:
            await announce_order(e.event)
        raise
    if announce:
        await _announce(*announce)
    return out


async def _place_locked(args: dict, c: dict, mode: str, client: str,
                        agent: dict) -> tuple[dict, Optional[tuple]]:
    import terminal_paper
    aid = agent["id"]
    v = await vet(args, c, mode, agent)
    req = v.req
    if v.blockers:
        record(mode=mode, client=client, agent_id=aid, req=req, ok=False,
               message=v.blockers[0], title=_title(v), forecast_id=v.forecast_id)
        ev = (order_event(status="blocked", agent_id=aid, agent_name=agent_name(agent),
                          req=req, title=_title(v), reason=v.blockers[0], ok=False)
              if mode == "live" else None)
        raise Refused(v.blockers[0] if len(v.blockers) == 1
                      else "\n- " + "\n- ".join(v.blockers), event=ev)

    if mode == "paper":
        with terminal_paper.using_book(book_of(agent)):
            res = await terminal_paper.place(req, c)
        filled = _num(res.get("filledSize")) or 0.0
        avg = _num(res.get("avgPriceCents"))
        committed = None
        if res.get("ok") and req["action"] == "buy":
            if req["orderType"] == "GTC" and res.get("status") in ("resting", "partial"):
                # The resting part can still spend: count the whole order.
                committed = v.cost
            else:
                # A killed remainder spends nothing: count what filled, fees in.
                fee_ps = (v.cost - req["size"] * req["priceCents"] / 100.0) / req["size"]
                committed = round(filled * ((avg or 0.0) / 100.0 + fee_ps), 4)
        record(mode=mode, client=client, agent_id=aid, req=req, ok=bool(res.get("ok")),
               message=str(res.get("message") or ""), title=_title(v),
               forecast_id=v.forecast_id, committed=committed,
               paper_local_id=res.get("localId"), filled=filled, avg=avg)
        if not res.get("ok"):
            raise Refused(str(res.get("message") or "Paper order refused."))
        out = {"mode": "paper", "agent": agent["name"], "edgeCentsAfterFee": v.edge, **res}
        return out, ("paper", f"{agent['name']}: {res.get('message')}")

    if c.get("mcp_live_approval", True):
        rid = record(mode=mode, client=client, agent_id=aid, req=req, ok=False,
                     message="waiting for the user's approval", title=_title(v),
                     forecast_id=v.forecast_id,
                     committed=v.cost if req["action"] == "buy" else None,
                     status="pending")
        msg = (f"{agent['name']} wants to {req['action'].upper()} {req['size']:g} "
               f"{req['side'].upper()} @ {req['priceCents']:g}¢ in \"{_clip(_title(v))}\" "
               f"(${(v.cost or 0.0):,.2f}) — approval #{rid}")
        logger.info("mcp: %s", msg)
        ev = order_event(status="pending", agent_id=aid, agent_name=agent_name(agent),
                         req=req, title=_title(v), reason="waiting for the user's approval",
                         ok=True, line=msg, approval_id=rid)
        return ({"mode": "live", "ok": True, "pending": True, "approvalId": rid,
                 "message": ("Queued for the user's approval. Nothing has been sent. "
                             f"Check it with get_orders approvalId={rid}; it expires in "
                             f"{APPROVAL_TTL_SEC // 60} minutes.")},
                ("approval", msg, rid, ev))
    out, ev = await execute_live(v, mode, client, aid, agent_name=agent_name(agent))
    return out, ("live", ev["message"], None, ev)


def _agent_display(agent_id: Optional[str]) -> str:
    try:
        return agent_name(mcp_agents.get(cfg(), str(agent_id or DEFAULT_AGENT)))
    except Exception:  # noqa: BLE001
        return "The agent"


async def execute_live(v: Vetted, mode: str, client: str, agent_id: str,
                       row_id: Optional[int] = None,
                       agent_name: Optional[str] = None) -> tuple[dict, dict]:
    """Send one vetted order through the ticket's own place() and record what
    came back (see THE LIVE LEDGER) → (result, mcp:order event). Raises
    Refused on a refusal or an unknown outcome, already recorded, with the
    event attached for the caller to announce."""
    import db
    import terminal_trade
    req = v.req
    name = agent_name or _agent_display(agent_id)
    title = _title(v)
    order = {**req, "confirmUsd": (v.preview or {}).get("costUsd") or 0.0}
    committed = v.cost if req["action"] == "buy" else None
    try:
        res = await terminal_trade.place(order, origin="ai")
    except Exception as e:  # noqa: BLE001
        # The send itself blew up, so the order may or may not be on the
        # venue. 'unknown' keeps counting against the daily spend and the sell
        # allowance (fail closed) and says to look before trying again.
        why = (f"send errored ({type(e).__name__}); it may or may not have reached "
               f"Polymarket — check your open orders before retrying")
        if row_id is None:
            record(mode=mode, client=client, agent_id=agent_id, req=req, ok=False,
                   message=why, title=title, forecast_id=v.forecast_id,
                   committed=committed, status="unknown")
        else:
            with db.get_db() as conn:
                conn.execute("UPDATE mcp_orders SET ok=0, status='unknown', "
                             "committed_usd=?, message=? WHERE id=?",
                             (committed, why, row_id))
        logger.warning("mcp: live send errored: %s: %s", type(e).__name__, e)
        raise Refused(why, event=order_event(
            status="unknown", agent_id=agent_id, agent_name=name, req=req, title=title,
            reason=why, ok=False, approval_id=row_id))
    ok, st, filled = live_outcome(res, req)
    text = str(res.get("message") or "")
    # What a buy still holds of the daily cap: its whole worst case while it
    # may rest or its fate is unknown, what filled once that is final, and
    # nothing when nothing was placed.
    if committed is not None:
        if st == "refused":
            committed = None
        elif st in _FINAL and req["size"]:
            committed = round(committed * min(1.0, float(filled or 0.0) / float(req["size"])), 4)
    if row_id is None:
        record(mode=mode, client=client, agent_id=agent_id, req=req, ok=ok,
               message=text, title=title, forecast_id=v.forecast_id,
               committed=committed, order_id=res.get("orderId"), filled=filled,
               status=st)
    else:
        with db.get_db() as conn:
            conn.execute(
                "UPDATE mcp_orders SET ok=?, status=?, committed_usd=?, order_id=?, "
                "filled=?, message=? WHERE id=?",
                (1 if ok else 0, st or ("approved" if ok else "failed"),
                 committed, res.get("orderId"), filled, text[:500], row_id))
    ev_status = st or ("sent" if ok else "refused")
    ev = order_event(status=ev_status, agent_id=agent_id, agent_name=name, req=req,
                     title=title, reason=text, ok=ok, order_id=res.get("orderId"),
                     filled=filled, approval_id=row_id,
                     line=_order_line(name, req, title, text, ev_status))
    if not ok:
        raise Refused(f"Polymarket/terminal refused: {text}", event=ev)
    return ({"mode": "live", "ok": True, "orderId": res.get("orderId"),
             "status": res.get("status"), "filledSize": filled,
             "message": res.get("message")}, ev)


def record_live_exit(*, client: str, agent_id: str, cid: str, side: str,
                     want: Optional[float], res: dict, title: str = "") -> dict:
    """Book one live close_position (a marketable fill-and-kill sell) at what
    it actually did → the mcp:order event to announce after the lock.

    An unmatched or partial exit books what FILLED, so the rest stays the
    agent's to sell; a venue refusal books nothing; only an outcome that may
    have reached the venue unseen ('unknown') counts its full size."""
    sent = {"conditionId": cid, "side": side, "action": "sell",
            "size": float(_num(res.get("sentSize")) or want or 0.0),
            "priceCents": float(_num(res.get("limitCents")) or 0.0), "orderType": "FAK"}
    ok, st, filled = live_outcome(res, sent)
    if not ok and st == "refused" and res.get("status") in (None, ""):
        # The ticket's own gate (book moved, preview blocker): never sent.
        st, filled = None, None
    text = str(res.get("message") or "")
    record(mode="live", client=client, agent_id=agent_id, req=sent, ok=ok, message=text,
           title=title, order_id=res.get("orderId"), filled=filled,
           avg=_num(res.get("avgPriceCents")), status=st)
    name = _agent_display(agent_id)
    ev_status = st or ("sent" if ok else "refused")
    return order_event(status=ev_status, agent_id=agent_id, agent_name=name, req=sent,
                       title=title, reason=text, ok=ok, order_id=res.get("orderId"),
                       filled=filled,
                       line=_order_line(name, sent, title, text, ev_status))


async def decide(row_id: int, approve: bool, via: str = "app") -> dict:
    """The user's answer to a pending live order. Approval RE-VETS against the
    market as it is now — the agent asked minutes ago. The whole answer runs
    under the money lock: two approvals clicked together re-vet one after
    the other, each seeing what the other spent."""
    async with money_lock():
        out, ev = await _decide_locked(int(row_id), bool(approve), via)
    if ev:
        await announce_order(ev)
    return out


async def _decide_locked(row_id: int, approve: bool, via: str) -> tuple[dict, Optional[dict]]:
    import db
    with db.get_db() as conn:
        r = conn.execute("SELECT * FROM mcp_orders WHERE id=?", (row_id,)).fetchone()
        if r is None or r["status"] != "pending":
            return {"ok": False, "message": f"No pending agent order #{row_id}."}, None
        if effective_status(r) == "expired":
            conn.execute("UPDATE mcp_orders SET status='expired' WHERE id=?", (row_id,))
            return {"ok": False, "message": f"Agent order #{row_id} expired; nothing was sent."}, None
        if not approve:
            conn.execute("UPDATE mcp_orders SET status='rejected', message=? WHERE id=?",
                         (f"rejected by the user ({via})", row_id))
            logger.info("mcp: approval #%s rejected via %s", row_id, via)
            return {"ok": True, "message": f"Rejected agent order #{row_id}."}, None
        if (r["network"] or "") != _net():
            why = "queued on another network; nothing was sent"
            conn.execute("UPDATE mcp_orders SET status='failed', message=? WHERE id=?",
                         (why, row_id))
            return {"ok": False, "message": f"Agent order #{row_id} was {why}."}, None
        # 'deciding' still counts toward today's spend; the re-vet excludes
        # this row by id instead.
        conn.execute("UPDATE mcp_orders SET status='deciding' WHERE id=?", (row_id,))
    sent = False
    aid = r["agent_id"] or DEFAULT_AGENT
    args = {"conditionId": r["condition_id"], "side": r["side"],
            "action": r["action"], "size": r["size"],
            "priceCents": r["price_cents"], "orderType": r["order_type"],
            "forecastId": r["forecast_id"]}
    try:
        c = cfg()
        # Re-vetted as the agent that asked, under its rules as they are NOW.
        agent = mcp_agents.get(c, aid)
        mode = agent_mode(c, agent)
        v = None
        if agent is None:
            blockers = ["the agent that asked for it no longer exists"]
        elif not agent.get("enabled", True):
            blockers = [f"{agent['name']} is switched off"]
        elif mode != "live":
            blockers = ([f"agent trading is now {trade_mode(c)}, not live"]
                        if trade_mode(c) != "live"
                        else [f"{agent['name']} is now on paper, not live"])
        else:
            v = await vet(args, c, mode, agent, forecast_grace=APPROVAL_TTL_SEC,
                          exclude_id=row_id)
            blockers = v.blockers
        if blockers or v is None:
            with db.get_db() as conn:
                conn.execute("UPDATE mcp_orders SET status='failed', message=? WHERE id=?",
                             ("approved, but refused on re-check: " + blockers[0], row_id))
            return {"ok": False, "message": "Approved, but it no longer passes: " + blockers[0]}, None
        sent = True
        try:
            res, ev = await execute_live(v, mode, r["client"] or "", aid, row_id=row_id,
                                         agent_name=agent_name(agent))
        except Refused as e:
            return {"ok": False, "message": str(e)}, e.event
        logger.info("mcp: approval #%s approved via %s", row_id, via)
        return {"ok": True, "message": f"Approved #{row_id}: {res.get('message')}"}, ev
    except Exception as e:  # noqa: BLE001
        # Anything else used to leave the row 'deciding' for good. Close it
        # with what we know: before the send nothing reached the venue; after
        # it, we cannot say.
        if sent:
            st, why = "unknown", (f"approval errored while sending ({type(e).__name__}); "
                                  f"check your Polymarket orders before retrying")
        else:
            st, why = "failed", (f"approval errored during the re-check "
                                 f"({type(e).__name__}); nothing was sent")
        logger.warning("mcp: approval #%s errored: %s: %s", row_id, type(e).__name__, e)
        try:
            with db.get_db() as conn:
                conn.execute("UPDATE mcp_orders SET status=?, message=? "
                             "WHERE id=? AND status='deciding'", (st, why, row_id))
        except Exception:  # noqa: BLE001
            pass
        ev = None
        if sent:
            ev = order_event(status="unknown", agent_id=aid, agent_name=_agent_display(aid),
                             req=args, title=str(r["title"] or ""), reason=why, ok=False,
                             approval_id=row_id)
        return {"ok": False, "message": f"Agent order #{row_id}: {why}."}, ev


def order_status(row_id: Any, agent_id: str) -> dict:
    import db
    try:
        rid = int(row_id)
    except (TypeError, ValueError):
        raise Refused("approvalId must be the number place_order returned.")
    with db.get_db() as conn:
        r = conn.execute("SELECT * FROM mcp_orders WHERE id=?", (rid,)).fetchone()
    # Another agent's orders are answered exactly like an id that does not
    # exist, so ids can't be walked to read them.
    if r is None or (r["agent_id"] or DEFAULT_AGENT) != agent_id:
        raise Refused(f"No agent order #{rid}.")
    return {"approvalId": rid,
            "status": effective_status(r) or ("sent" if r["ok"] else "refused"),
            "orderId": r["order_id"], "filled": r["filled"],
            "avgFillCents": r["avg_fill_cents"], "message": _effective_message(r)}


async def note_cancelled(order_id: str) -> None:
    """Book a cancelled agent order at what it actually filled — from EVERY
    cancel path (terminal_trade.cancel calls this: the app's Cancel and
    Reprice, the phone's /cancelall, the agent's own cancel tools). A no-op
    for an order no agent placed, or one already booked final.

    A cancelled buy stops holding its slot and the daily cap beyond what it
    filled. A cancelled sell stops counting its full size only once its
    fills are KNOWN; if the ledger cannot be read it keeps counting in full,
    because a sell that filled unseen and was forgotten would let the agent
    sell the same shares twice."""
    import db
    oid = str(order_id or "").strip()
    if not oid:
        return

    def _row():
        with db.get_db() as conn:
            return conn.execute(
                "SELECT o.*, (SELECT SUM(f.size) FROM terminal_fills f "
                "             WHERE f.order_id=o.order_id) AS ledger "
                "FROM mcp_orders o WHERE " + _LIVE_SENT_SQL + " AND o.mode='live' "
                "AND o.network=? AND o.order_id=? "
                "AND (o.status IS NULL OR o.status IN ('approved','unknown')) "
                "ORDER BY o.id DESC LIMIT 1", (_net(), oid)).fetchone()
    if _row() is None:
        return
    await refresh_fills()
    r = _row()
    if r is None:
        return
    known = r["ledger"] if r["ledger"] is not None else r["filled"]
    if known is None and r["action"] == "sell" and not authed():
        return
    filled = max(float(r["filled"] or 0.0), float(known or 0.0))
    with db.get_db() as conn:
        _book_final(conn, r, filled, "cancelled")


# ────────────────────────────────────────────────────────────────────────
# Telling the app and the phone
# ────────────────────────────────────────────────────────────────────────


async def announce_order(ev: dict) -> None:
    """Announce one live agent order outcome (an order_event)."""
    await _announce("approval" if ev.get("status") == "pending" else "live",
                    str(ev.get("message") or ""), ev.get("approvalId"), ev)


async def _announce(kind: str, msg: str, rid: Optional[int] = None,
                    ev: Optional[dict] = None) -> None:
    """mcp:order to the app (a toast, the AI Agents page, an OS notification)
    and, for live, the paired phone. `ev` (order_event) rides along whole. A
    rail's own refusal ('blocked': nothing was sent) goes to the app only."""
    logger.info("mcp: %s", msg)
    payload: dict = {"mode": "live" if kind in ("live", "approval") else "paper",
                     "message": msg}
    if rid is not None:
        payload["approvalId"] = rid
    if ev:
        payload.update(ev)
        payload["message"] = msg or ev.get("message") or ""
    try:
        import service
        await service.emit_event("mcp:order", payload)
    except Exception:  # noqa: BLE001
        pass
    if kind == "paper" or (ev or {}).get("status") == "blocked":
        return
    try:
        import remote
        if kind == "approval":
            await remote.push(
                f"[AI AGENT] {msg}\nApprove in the app, or reply: approve {rid}  "
                f"(reject {rid} to refuse; expires in {APPROVAL_TTL_SEC // 60} min)")
        else:
            await remote.push(f"[AI AGENT] {msg}")
    except Exception:  # noqa: BLE001
        pass


def summary_for_status(c: dict, agent: Optional[dict]) -> dict:
    """The rails block get_status and the AI Agents page show."""
    r = agent_rails(c, agent)
    mode = agent_mode(c, agent)
    spent = spent_today(mode) if mode in ("paper", "live") else 0.0
    mine = (spent_today(mode, agent_id=(agent or {}).get("id"))
            if mode in ("paper", "live") else 0.0)
    return {
        "tradeMode": trade_mode(c), "yourMode": mode,
        "approvals": bool(c.get("mcp_live_approval", True)),
        "rails": r,
        "spentTodayUsd": round(spent, 2), "yourSpentTodayUsd": round(mine, 2),
        "note": ("Money caps apply to BUYS (fees included) per UTC day, all agents "
                 "together and yours on top; 0 = no cap. Exits are never capped."),
    }


async def close_agent_paper(agent_id: Any) -> dict:
    """Sell one agent's open PAPER positions at the bid, for the USER.

    A deleted or switched-off agent cannot sell what it holds (every tool
    refuses it), and its positions would sit in its paper book for good,
    spending the shared position cap and the loss stop. This is the way out.
    It is the user's action (service RPC mcpAgentClosePaper); it is not an MCP
    tool, so no agent can call it, for itself or for another agent.

    Paper only — it touches that agent's paper book and nothing else, whatever
    the account mode. Each position is one marketable fill-and-kill walked out
    of the REAL bids (terminal_trade.sell_position's paper path, real fees);
    depth that runs out, or a side nobody bids, stays open and is reported,
    never closed at an invented price."""
    import terminal_paper
    import terminal_trade
    aid = str(agent_id or "").strip().lower() or DEFAULT_AGENT
    if aid != DEFAULT_AGENT and not mcp_agents.valid_id(aid):
        return {"ok": False, "message": "No such agent.", "closed": [], "open": []}
    book = mcp_agents.paper_book(aid)
    client = "app (closed by the user)"
    closed: list[dict] = []
    left: list[dict] = []
    async with money_lock():
        with terminal_paper.using_book(book):
            pf = await terminal_paper.portfolio(cfg())
            for p in pf.get("positions") or []:
                if p.get("settled") or float(p.get("size") or 0.0) <= 1e-9:
                    continue
                cid, side, size = str(p["conditionId"]), str(p["side"]), float(p["size"])
                req = {"conditionId": cid, "side": side, "paper": True, "size": size,
                       "maxSlippageCents": terminal_trade.MAX_SELL_SLIPPAGE_CENTS}
                try:
                    pv = await terminal_trade.sell_position_preview(req)
                    if pv.get("blockers"):
                        left.append({"conditionId": cid, "side": side, "size": size,
                                     "reason": str(pv["blockers"][0])})
                        continue
                    req["confirmUsd"] = pv.get("proceedsUsd") or 0.0
                    res = await terminal_trade.sell_position(req, origin="manual")
                except Exception as e:  # noqa: BLE001 — one market must not stop the rest
                    logger.debug("close paper: %s %s failed: %s", cid, side, e)
                    left.append({"conditionId": cid, "side": side, "size": size,
                                 "reason": f"could not price it ({type(e).__name__})"})
                    continue
                filled = _num(res.get("filledSize")) or 0.0
                record(mode="paper", client=client, agent_id=aid,
                       req={"conditionId": cid, "side": side, "action": "sell",
                            "size": float(res.get("sentSize") or size),
                            "priceCents": float(res.get("limitCents") or 0.0),
                            "orderType": "FAK"},
                       ok=bool(res.get("ok")) and filled > 0,
                       message=f"closed by the user: {res.get('message') or ''}",
                       title=str(p.get("title") or ""),
                       paper_local_id=res.get("localId"), filled=filled,
                       avg=_num(res.get("avgPriceCents")))
                if filled > 0:
                    closed.append({"conditionId": cid, "side": side, "size": filled,
                                   "avgPriceCents": _num(res.get("avgPriceCents"))})
                if filled < size - 1e-9:
                    left.append({"conditionId": cid, "side": side,
                                 "size": round(size - filled, 4),
                                 "reason": str(res.get("message") or "nobody is bidding it")})
    if not closed and not left:
        msg = "Nothing to close: this agent holds no paper positions."
    elif left:
        msg = (f"Closed {len(closed)} position(s); {len(left)} could not be closed in "
               f"full (no bid deep enough). Try again when the market trades.")
    else:
        msg = f"Closed {len(closed)} paper position(s) at the bid."
    if closed:
        await _announce("paper", f"{msg} ({aid})")
    return {"ok": not left, "message": msg, "closed": closed, "open": left}
