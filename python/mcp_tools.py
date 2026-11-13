"""The tools an AI agent gets over MCP — and the gates in front of them.

Every tool is a thin wrapper over the SAME function the app's own screens
call (``service._h_*`` or the module behind it). That is the whole design: an
agent cannot end up with a looser path than a click, and any gate added to the
app later applies here for free.

Gates, in one place so they cannot drift:

    read      with the server on. Market data, account, history, backtests of
              the built-in engines (replays never place orders).
    trade     the agent's effective mode (mcp_rails.agent_mode): off, paper
              (its OWN paper account) or live. Every order passes
              mcp_rails.vet — forecast first, edge after fee, caps, rules,
              loss stop, and it sells only what it opened.
    derisk    with the server on. Pausing engines. Getting out is never gated.
    switches  ``mcp_allow_live_switches``. Whole-account powers: switching
              engines on, arming scripts / the crypto schedule live, changing
              engine settings, flattening the account.
    scripts   ``mcp_scripts_enabled``. Writing / running strategy code, which
              executes as full Python in the process holding the API key.

A refusal is returned as a tool error with a sentence that says why and what
the USER would have to change — never silently, never as an empty result.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import mcp_rails
from mcp_rails import Refused

logger = logging.getLogger("krypt.mcp.tools")

# The session of the call being served: which client, and — set by the
# transport from the bearer token — which of the user's named agents. A
# ContextVar follows the coroutine, so two agents calling at once can never
# act as each other.
_SESSION: contextvars.ContextVar[dict] = contextvars.ContextVar("mcp_session", default={})


def _session() -> dict:
    return _SESSION.get()


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _cfg() -> dict:
    return mcp_rails.cfg()


def _agent(cfg: Optional[dict] = None) -> dict:
    return mcp_rails.require_agent(_session(), cfg or _cfg())


def _require(cfg: dict, gate: str) -> None:
    if gate == "switches" and not cfg.get("mcp_allow_live_switches"):
        raise Refused(
            "Whole-account changes by AI agents are OFF: switching engines on, "
            "arming scripts or schedules, changing engine settings, flattening "
            "the account. The user can allow them in Krypt PolyBot US → AI Agents "
            "→ Permissions → 'Whole-account powers'. Turning things OFF is "
            "always allowed.")
    if gate == "scripts" and _session().get("transport") == "autopilot":
        # Autopilot runs unattended on text other people wrote; running code
        # it was handed is the one power it never gets.
        raise Refused("Autopilot does not write or run strategy scripts.")
    if gate == "scripts" and not cfg.get("mcp_scripts_enabled"):
        raise Refused(
            "Writing and running strategy scripts is OFF for AI agents. The "
            "user can turn it on in Krypt PolyBot US → AI Agents → Permissions → "
            "'Write and run strategy scripts'. Reading the script API docs and "
            "the built-in backtests work without it.")


def _require_account_live(cfg: dict) -> None:
    """For whole-account actions that send real orders outside the agent rails
    (flatten): the user's permission, paper mode off, and a
    connected account."""
    import service
    from config import account_live
    _require(cfg, "switches")
    if not account_live(cfg):
        raise Refused(
            "The account is in PAPER MODE, so no real order can be sent from "
            "anywhere — including an agent. Only the user can switch the "
            "account to Live.")
    if not service.STATE.auth_ok:
        raise Refused("The account is not connected, so no order can be signed.")


def _trade_mode(cfg: dict, agent: dict, args: dict) -> str:
    """The mode this agent trades in. It is the user's to set, not the
    agent's: a `mode` argument may only confirm it."""
    mode = mcp_rails.agent_mode(cfg, agent)
    asked = str(args.get("mode") or "").lower() or None
    if mode == "off":
        raise Refused("Trading is OFF for agents. The user enables paper or live "
                      "trading in Krypt PolyBot US → AI Agents.")
    if asked not in (None, mode):
        raise Refused(f"You trade in {mode.upper()} mode here — the user sets it "
                      f"(the global mode and {agent['name']}'s own). Leave mode "
                      f"out or pass '{mode}'.")
    return mode


def _read_mode(cfg: dict, agent: dict, args: dict) -> str:
    """Reads may look at either account: the agent's own paper account, or the
    live account. Default: the mode it trades in (paper when trading is off)."""
    asked = str(args.get("mode") or "").lower()
    if asked in ("paper", "live"):
        return asked
    return "live" if mcp_rails.agent_mode(cfg, agent) == "live" else "paper"


def _cid(args: dict) -> str:
    cid = str(args.get("conditionId") or "").strip()
    if not cid:
        raise Refused("conditionId is required (find one with search_markets).")
    return cid


def _strip_private(v: Any, depth: int = 0) -> Any:
    """Drop `_raw`-style keys: provider payloads the UI never shows, which
    would only cost the agent context and invite it to read undocumented
    fields as if they were ours."""
    if depth > 12:
        return v
    if isinstance(v, dict):
        return {k: _strip_private(x, depth + 1) for k, x in v.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(v, list):
        return [_strip_private(x, depth + 1) for x in v]
    return v


def spent_today_usd() -> float:
    """What agents have committed to live BUYS since 00:00 UTC, all agents
    together (mcp_rails.spent_today). Unknown is not zero: an unreadable
    ledger reads as infinite, which blocks spending."""
    try:
        return round(mcp_rails.spent_today("live"), 2)
    except Exception as e:  # noqa: BLE001
        logger.warning("mcp: could not read today's AI spend: %s", e)
        return float("inf")


async def _set_cfg(patch: dict) -> None:
    """Change config the way the UI does: Electron persists it (and re-sanitises
    it — see sanitizeConfigPatch), and it lands in-process at once."""
    import service
    from config import merge_with_defaults
    await service.emit_event("mcp:configPatch", {"patch": patch})
    service.STATE.cfg = merge_with_defaults({**(service.STATE.cfg or {}), **patch})


def _push(text: str) -> None:
    """Tell the user's paired phone, if they have one. Best effort."""
    try:
        import remote
        t = asyncio.get_running_loop().create_task(remote.push(text))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    except Exception:  # noqa: BLE001
        pass


_bg: set = set()


# ────────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Tool:
    name: str
    title: str
    description: str
    props: dict
    required: tuple
    fn: Callable[[dict], Awaitable[Any]]
    kind: str  # read | paper | live | scripts | derisk | mixed
    read_only: bool = False
    destructive: bool = False


_TOOLS: dict[str, Tool] = {}


def tool(name: str, title: str, description: str, *, props: Optional[dict] = None,
         required: tuple = (), kind: str = "read", read_only: bool = False,
         destructive: bool = False):
    def deco(fn):
        _TOOLS[name] = Tool(name, title, description, props or {}, required, fn,
                            kind, read_only, destructive)
        return fn
    return deco


def has_tool(name: str) -> bool:
    return name in _TOOLS


def list_tools() -> list[dict]:
    out = []
    for t in _TOOLS.values():
        out.append({
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "inputSchema": {"type": "object", "properties": t.props,
                            "required": list(t.required),
                            "additionalProperties": False},
            "annotations": {
                "title": t.title,
                "readOnlyHint": t.read_only,
                "destructiveHint": t.destructive,
                "idempotentHint": t.read_only,
                # It talks to Polymarket: the world it acts on is open.
                "openWorldHint": True,
            },
        })
    return out


def _summ(args: dict) -> str:
    keep = ("mode", "conditionId", "side", "action", "size", "priceCents",
            "orderType", "forecastId", "fairValueCents", "approvalId", "id",
            "enabled", "live", "engines", "kind", "confirm", "markets", "days")
    bits = []
    for k in keep:
        if k in args:
            v = args[k]
            if k == "conditionId":
                v = str(v)[:12] + "…"
            bits.append(f"{k}={json.dumps(v, default=str)[:60]}")
    return " ".join(bits)


def _stream_mode(aid: str) -> Optional[str]:
    """The mode the calling agent trades in, for the tool-call stream only."""
    try:
        c = _cfg()
        return mcp_rails.agent_mode(c, mcp_rails.agent_for({"agent": aid}, c))
    except Exception:  # noqa: BLE001
        return None


async def call(name: str, args: dict, session: dict) -> dict:
    """Run one tool. Always returns an MCP CallToolResult, never raises.

    THE dispatcher: MCP over HTTP, the stdio relay (via HTTP), the plain HTTP
    API and Autopilot all land here, so the rate limit, every rail, the audit
    and the tool-call stream apply to all of them alike."""
    import time as _time
    import mcp_server
    started = _time.monotonic()
    args = args if isinstance(args, dict) else {}
    client = session.get("client") or "unknown client"
    aid = str(session.get("agent") or mcp_rails.DEFAULT_AGENT)
    if aid != mcp_rails.DEFAULT_AGENT:
        client = f"{client} · {aid}"
    t = _TOOLS.get(name)
    mode = _stream_mode(aid)

    async def _done(out: dict, outcome: str, reason: Optional[str] = None,
                    res: Any = None) -> dict:
        await mcp_server.emit_call(name=name if t is not None else "unknown", args=args,
                                   session=session, res=res, outcome=outcome,
                                   reason=reason, started=started, mode=mode)
        return out

    if t is None:
        return await _done({"content": [{"type": "text", "text": f"Refused: unknown tool: {name}"}],
                            "isError": True}, "refused", "unknown tool")
    over = mcp_server.rate_refusal(aid)
    if over:
        session["refused"] = session.get("refused", 0) + 1
        text = (f"{mcp_server.RATE_LIMIT_TEXT} {over}. Slow down and try again in a "
                f"minute; nothing was executed.")
        return await _done({"content": [{"type": "text", "text": text}], "isError": True},
                           "refused", f"Rate limit: {over}")
    unknown = set(args) - set(t.props)
    token = _SESSION.set(session)
    try:
        # Say what the tool does take: an agent that guessed `query` for `sql`
        # otherwise has to re-read the whole tool list to find out.
        takes = f"{name} takes: {', '.join(t.props) or 'no arguments'}"
        if unknown:
            raise Refused(f"unknown argument(s): {', '.join(sorted(unknown))}. {takes}")
        missing = [r for r in t.required if args.get(r) in (None, "")]
        if missing:
            raise Refused(f"missing required argument(s): {', '.join(missing)}. {takes}")
        result = await t.fn(args)
    except Refused as e:
        session["refused"] = session.get("refused", 0) + 1
        mcp_server.audit(client, name, "refused", f"{e} [{_summ(args)}]")
        return await _done({"content": [{"type": "text", "text": f"Refused: {e}"}],
                            "isError": True}, "refused", str(e))
    except Exception as e:  # noqa: BLE001
        logger.debug("mcp tool %s failed", name, exc_info=True)
        if not t.read_only:
            mcp_server.audit(client, name, "error", f"{type(e).__name__}: {e} [{_summ(args)}]")
        return await _done({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                            "isError": True}, "error", f"{name} failed ({type(e).__name__})")
    finally:
        _SESSION.reset(token)

    # A trading call can come back ok=False (the venue or a gate below us said
    # no). That is a failed action, and the agent must see it as one.
    failed = isinstance(result, dict) and result.get("ok") is False
    msg = result.get("message") if isinstance(result, dict) else None
    if t.read_only:
        session["reads"] = session.get("reads", 0) + 1
    else:
        session["writes"] = session.get("writes", 0) + 1
        mcp_server.audit(client, name, "failed" if failed else "accepted",
                         f"{_summ(args)}{(' — ' + str(msg)) if msg else ''}")
    text = json.dumps(_strip_private(result), default=str, ensure_ascii=False)
    out = {"content": [{"type": "text", "text": text}], "isError": bool(failed)}
    if failed:
        return await _done(out, "refused", str(msg or "failed"))
    return await _done(out, "ok", res=result)


# ────────────────────────────────────────────────────────────────────────
# Schema fragments
# ────────────────────────────────────────────────────────────────────────

_CID = {"type": "string", "description": "The market's conditionId: its slug on Polymarket US."}
_SIDE = {"type": "string", "enum": ["yes", "no"],
         "description": "yes = first outcome (Up for Up/Down markets), no = second."}
_MODE = {"type": "string", "enum": ["paper", "live"],
         "description": "Optional. The user sets which mode you trade in (see "
                        "get_status yourMode); pass it only to confirm it. On "
                        "get_portfolio and get_orders it picks the account: "
                        "paper = your own simulated account, live = the real "
                        "account. Tools that don't list it don't take it."}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500}
_CONFIG = {"type": "object", "description":
           "Config keys to override for this run only (snake_case, as "
           "returned by get_status with include_config=true). Never saved."}
_SINCE = {"type": "integer", "minimum": 1, "maximum": 365,
          "description": "Days of recorded data to replay (default 60)."}


# ════════════════════════════════════════════════════════════════════════
# Status / account
# ════════════════════════════════════════════════════════════════════════


@tool("get_status", "Status and permissions",
      "Who you are (the user's named agent your token belongs to), what you "
      "may do right now — your trade mode (off / paper / live, set by the "
      "user), your rails (caps, minimum edge, loss stop) and what is left of "
      "today — account connection, which engines run, and paper/offline mode. "
      "Call this first. include_config=true adds the full engine "
      "configuration (snake_case keys). include_account=true adds the live "
      "account's cash, value and P&L. In account, roiPct and alltimePnlUsd "
      "are ALL-TIME for this install (measured from alltimeBaselineUsd), "
      "not today or this session; todayPnlUsd is the balance change since "
      "00:00 UTC, and sessionPnlUsd since the engines were last started. "
      "All are the whole account, not only your trades.",
      props={"include_config": {"type": "boolean"},
             "include_account": {"type": "boolean"}}, read_only=True)
async def _get_status(a: dict) -> dict:
    import service
    cfg = _cfg()
    agent = _agent(cfg)
    mode = mcp_rails.agent_mode(cfg, agent)
    loss = None
    if mode in ("paper", "live"):
        try:
            loss = await mcp_rails.day_loss(mode, cfg)
        except Exception as e:  # noqa: BLE001
            logger.debug("mcp status: loss read failed: %s", e)
    out = {
        "app": {"version": __import__("mcp_server")._app_version()},
        "now": __import__("mcp_server")._iso(),
        "you": {"agentId": agent["id"], "name": agent["name"],
                "note": "Call get_my_agent for your guide and rules."},
        "wallet": {"connected": bool(service.STATE.auth_ok)},
        "trading": {**mcp_rails.summary_for_status(cfg, agent),
                    "lossTodayUsd": (loss or {}).get("lossUsd"),
                    "lossUnreadable": bool((loss or {}).get("positionsUnreadable"))},
        "permissions": {
            "read": True, "backtests": True, "pauseEngines": True,
            "trade": mode,
            "wholeAccountChanges": bool(cfg.get("mcp_allow_live_switches")),
            "scripts": bool(cfg.get("mcp_scripts_enabled")),
        },
        "paperAccount": {"bankrollUsd": cfg.get("mcp_paper_bankroll_usd"),
                         "note": "Your own paper account, separate from the user's "
                                 "and from the other agents'."},
        "accountMode": cfg.get("account_mode"),
        "globalPaperMode": bool(cfg.get("terminal_paper_mode")),
        "offlineMode": bool(cfg.get("terminal_offline_mode")),
        "engines": {
            "main_engine": bool(cfg.get("enable_trading")),
            "crypto15m": bool(cfg.get("crypto15m_enabled")),
            "scripts_live": bool(cfg.get("scripts_live_enabled")),
            "paused": bool(service.STATE.paused),
        },
    }
    if a.get("include_account"):
        out["account"] = await service._h_account({})
    if a.get("include_config"):
        out["config"] = cfg
    return out


@tool("get_my_agent", "Who you are",
      "You are one of the user's own named agents. This returns your name, "
      "the user's guide for you (how they want you to think and decide), your "
      "hard rules in plain words (enforced on every order — an order outside "
      "them is refused), your effective rails, and your forecasting record: "
      "your Brier score against the market's own price on resolved markets, "
      "and your recent forecasts.",
      read_only=True)
async def _get_my_agent(_a: dict) -> dict:
    import forecast_ledger
    import mcp_agents
    cfg = _cfg()
    agent = _agent(cfg)
    mode = mcp_rails.agent_mode(cfg, agent)
    view = mcp_agents.public_view(agent, mcp_rails.agent_rails(cfg, agent), mode)
    sb = forecast_ledger.scoreboard(recent=60)
    view["record"] = next((r for r in sb["byAgent"] if r["agentId"] == agent["id"]), None)
    view["recentForecasts"] = [r for r in sb["recent"] if r["agentId"] == agent["id"]][:15]
    view["allAgents"] = {k: sb[k] for k in ("scoredMarkets", "minScored", "overall")}
    view["note"] = ("Your guide is how the user wants you to think. Your rules are "
                    "enforced by the app on every order whatever the guide says. "
                    "Your record is scored against the market's mid at the moment "
                    "you forecast; under minScored resolved markets it says "
                    "'too-few', and that is the honest answer.")
    return view


@tool("record_forecast", "Record your fair value",
      "Commit to YOUR probability that a market resolves YES, before you buy. "
      "A buy must cite the forecastId this returns (place_order forecastId), "
      "made by you on that market within the last 30 minutes, and the price "
      "must beat it by your minimum edge AFTER Polymarket's fee. Every "
      "forecast is kept and scored against the market's own mid at this "
      "moment once the market resolves — that is your record. "
      "fairValueCents is 1-99 (a 0.42 probability is 42). rationale: what "
      "drives your number and what the resolution rules require (a sentence "
      "at least). model: the model you run, once is enough.",
      props={"conditionId": _CID,
             "fairValueCents": {"type": "number", "minimum": 1, "maximum": 99},
             "rationale": {"type": "string", "minLength": 20, "maxLength": 2000},
             "model": {"type": "string", "maxLength": 80}},
      required=("conditionId", "fairValueCents", "rationale"))
async def _record_forecast(a: dict) -> dict:
    import forecast_ledger
    import terminal_trade
    cfg = _cfg()
    agent = _agent(cfg)
    try:
        fv = float(a.get("fairValueCents"))
    except (TypeError, ValueError):
        raise Refused("fairValueCents must be a number from 1 to 99.")
    if not (1 <= fv <= 99):
        raise Refused("fairValueCents must be between 1 and 99 — 0 or 100 is a "
                      "claim of certainty, not a price.")
    rationale = str(a.get("rationale") or "").strip()
    if len(rationale) < 20:
        raise Refused("Give a rationale (at least a sentence): what drives your "
                      "number and what the resolution rules require.")
    cid = _cid(a)
    m = await mcp_rails.market(cid)
    if not m:
        raise Refused("Polymarket has no market with that conditionId.")
    if m.get("resolvedOutcome") is not None or m.get("status") in ("closed", "settled"):
        raise Refused(f"This market is {m.get('status')}; a forecast now would be "
                      f"scored against an outcome that may already be known.")
    session = _session()
    model = str(a.get("model") or session.get("model") or "")[:80]
    if a.get("model"):
        # An agent names its model once; later forecasts stay the same seat.
        session["model"] = model
    fid = forecast_ledger.record(
        condition_id=cid, prob_yes=fv / 100.0, market=m, rationale=rationale,
        model=model, client=str(session.get("client") or "")[:80],
        agent_id=agent["id"], network=terminal_trade._net())
    yes_ask = m.get("ask")
    no_ask = None if m.get("bid") is None else round(100.0 - m["bid"], 2)

    def _edge(side: str, px: Optional[float]) -> Optional[dict]:
        if px is None:
            return None
        return {"atPriceCents": px, "edgeCentsAfterFee": mcp_rails.edge_cents(
            fv, side, px, mcp_rails.fee_cents(m, px))}

    # Say where the quote came from.
    stamp = ((m.get("sources") or {}).get("ask") or (m.get("sources") or {}).get("bid")
             or {})
    quote = {k: m.get(k) for k in ("bid", "ask", "mid", "probability")}
    quote.update(source=stamp.get("source"), ageSec=stamp.get("ageSec"))
    if stamp.get("source") == "gateway":
        quote["note"] = ("A public quote, not the live feed: a market list's row can be "
                         "up to 30s old, which matters most near the close.")

    return {
        "ok": True, "forecastId": fid, "fairValueCents": fv,
        "market": quote,
        "buyYes": _edge("yes", yes_ask),
        "buyNo": _edge("no", no_ask),
        "minEdgeCents": mcp_rails.agent_rails(cfg, agent)["minEdgeCents"],
        "validForMin": mcp_rails.FORECAST_TTL_SEC // 60,
        "note": ("Scored against the market mid at this moment once the market "
                 "resolves. A null side means nobody is offering it; a null edge "
                 "means the fee is unknown, and then no buy can be checked."),
    }


@tool("get_portfolio", "Open positions",
      "Open positions with cost basis, mark and unrealised P&L. mode=paper "
      "(default unless you trade live) is YOUR paper account; mode=live is the "
      "real account — everything in it, whoever placed it; only what YOUR "
      "orders filled is yours to sell (sellableShares). reconcile=true (live) "
      "first pulls fresh fills from Polymarket's own ledger. realizedPnlUsd is "
      "before fees: sales plus settlements, across the whole ledger. "
      "settledPnlUsd is its settlement part, and realizedPnlNetUsd is it after "
      "fees (null while any fee is unknown). None of them is the cash change: "
      "cash also moves with open positions, deposits and withdrawals.",
      props={"mode": _MODE, "reconcile": {"type": "boolean"}}, read_only=True)
async def _get_portfolio(a: dict) -> dict:
    cfg = _cfg()
    agent = _agent(cfg)
    if _read_mode(cfg, agent, a) == "paper":
        import terminal_paper
        with terminal_paper.using_book(mcp_rails.book_of(agent)):
            return await terminal_paper.portfolio(cfg)
    import terminal_trade
    if a.get("reconcile"):
        await terminal_trade.reconcile_fills()
    pf = await terminal_trade.portfolio()
    for p in pf.get("positions") or []:
        p["sellableShares"] = mcp_rails.live_net(
            str(p.get("conditionId") or ""), str(p.get("side") or ""), agent["id"])
    return pf


@tool("get_orders", "Your orders and approvals",
      "mode=paper: your recent paper orders, with fills and notes. "
      "mode=live: YOUR resting orders on Polymarket (other orders in the "
      "account are the user's or another agent's) and your orders waiting for "
      "the user's approval. approvalId: the status of one order place_order "
      "queued for approval (pending, approved, rejected, expired, failed).",
      props={"mode": _MODE, "conditionId": _CID, "limit": _LIMIT,
             "approvalId": {"type": "integer", "minimum": 1}},
      read_only=True)
async def _get_orders(a: dict) -> dict:
    cfg = _cfg()
    agent = _agent(cfg)
    if a.get("approvalId") is not None:
        return mcp_rails.order_status(a["approvalId"], agent["id"])
    if _read_mode(cfg, agent, a) == "paper":
        import terminal_paper
        with terminal_paper.using_book(mcp_rails.book_of(agent)):
            return terminal_paper.orders(int(a.get("limit") or 100))
    import terminal_trade
    mine = mcp_rails.agent_order_ids(agent["id"])
    res = await terminal_trade.resting_orders(a.get("conditionId") or None)
    return {"orders": [o for o in res.get("orders") or [] if o.get("orderId") in mine],
            "awaitingApproval": [r for r in mcp_rails.pending()
                                 if r["agentId"] == agent["id"]],
            "note": "Only orders you placed are shown.",
            "fetchedAt": res.get("fetchedAt")}


@tool("get_trade_history", "Trade history",
      "Live-account history. kind=fills: the venue's own fill ledger; ts is the "
      "last trade's time when tsExact is true, otherwise only when a reconcile "
      "saw the fill. A null feeUsd is not yet known, not free. liquidity is "
      "the exchange's own word on each fill: maker (your order was resting; "
      "no fee), taker (it crossed), mixed, or null when not read yet. "
      "kind=order_log: every order sent from this app including failures, "
      "with origin (ai = you). kind=settled: resolved positions and the real "
      "win/loss record; totals.pnlUsd is after fees and covers only finished "
      "markets, and pnlMissingRows counts the rows it leaves out.",
      props={"kind": {"type": "string", "enum": ["fills", "order_log", "settled"]},
             "limit": _LIMIT},
      required=("kind",), read_only=True)
async def _get_trade_history(a: dict) -> dict:
    import terminal_trade
    kind = a["kind"]
    limit = int(a.get("limit") or 100)
    if kind == "fills":
        return terminal_trade.fills(limit)
    if kind == "order_log":
        return terminal_trade.order_log(limit)
    if kind == "settled":
        return await terminal_trade.settled_record()
    raise Refused("kind must be fills, order_log or settled.")


# Measured 2026-11-13 on every resolved 15-minute window then recorded (3,106
# windows in 777 time slots over 10 days; intervals resample whole slots). The
# test agent of 2026-11-07/08 had copied these figures into its forecasts.
MODEL_CAVEAT = (
    "modelProb, edgeNetCents and modelEdgePts come from the app's own spot "
    "model, and it is NOT a better forecast than the market price: on 3,106 "
    "recorded 15-minute windows its Brier score was worse than the mid's, and "
    "buying whenever edgeNetCents was 2 or more won 46% of the time at an "
    "average 46c ask (the model had said 52%), -1.6c a share after the fee "
    "(95% interval -3.3 to +0.1). Larger values did no better. Do not take "
    "it as an edge or copy it into a forecast.")


@tool("get_engine_activity", "Engine activity",
      "What the automated engines are doing. kind=positions: main-engine "
      "positions. kind=signals: signals from before the venue had none (normally "
      "empty). kind=pnl: P&L "
      "series. kind=scanner: scanner stats. kind=crypto15m: the short-term "
      "crypto engine's status. " + MODEL_CAVEAT,
      props={"kind": {"type": "string", "enum": [
          "positions", "signals", "pnl", "scanner", "crypto15m"]},
             "limit": _LIMIT},
      required=("kind",), read_only=True)
async def _get_engine_activity(a: dict) -> Any:
    import service
    kind = a["kind"]
    limit = int(a.get("limit") or 100)
    if kind == "positions":
        return {"positions": await service._h_positions({"limit": limit})}
    if kind == "signals":
        return {"signals": await service._h_signals({"limit": limit})}
    if kind == "pnl":
        return {"series": await service._h_pnlSeries({"sinceHours": 168})}
    if kind == "scanner":
        return await service._h_scannerStats({})
    if kind == "crypto15m":
        return await service._h_crypto15mStatus({})
    raise Refused("unknown kind")


# ════════════════════════════════════════════════════════════════════════
# Market data
# ════════════════════════════════════════════════════════════════════════


@tool("discover_markets", "Discover markets",
      "A live list of markets. column: new, closing (closing soon), volume "
      "(high 24h volume), volatility, watchlist (the user's starred "
      "markets), or crypto_updown — a live snapshot of Polymarket's recurring "
      "crypto Up-or-Down markets (5m / 15m / hourly / 4h; BTC, ETH, SOL, XRP, "
      "DOGE, BNB, HYPE, ZEC) with window timing, spot, prices and indicators. "
      + MODEL_CAVEAT + " "
      "filters is optional, e.g. {\"minVolume24hUsd\": 10000, "
      "\"categories\": [\"politics\"]}.",
      props={"column": {"type": "string", "enum": [
          "new", "closing", "volume", "volatility", "watchlist", "crypto_updown"]},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100},
             "filters": {"type": "object"}},
      required=("column",), read_only=True)
async def _discover(a: dict) -> dict:
    import service
    if a["column"] == "crypto_updown":
        return await service._h_crypto15m({})
    service._sync_terminal_offline()
    return await service._h_terminal_discover(
        {"column": a["column"], "limit": a.get("limit") or 25,
         "filters": a.get("filters")})


@tool("search_markets", "Search markets",
      "Full-text search across open Polymarket markets. Returns conditionIds.",
      props={"query": {"type": "string"},
             "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
      required=("query",), read_only=True)
async def _search(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_search(
        {"query": a["query"], "limit": a.get("limit") or 20})


@tool("get_market", "Market details",
      "One market: title, rules/description, probability, bid/ask, spread, "
      "volume, liquidity, close time, status and resolution.",
      props={"conditionId": _CID}, required=("conditionId",), read_only=True)
async def _get_market(a: dict) -> dict:
    import service
    import terminal
    service._sync_terminal_offline()
    m = await terminal.market(_cid(a), include_raw=True)
    if not m:
        return {"market": None}
    # The summary leaves the venue's row out, and the settlement rules with it,
    # so every agent told to read the rules first was handed none. A null here
    # means the venue sent none.
    rules = str((m.get("_raw") or {}).get("description") or "").strip()
    return {"market": {**terminal._strip_raw(m), "rules": rules or None}}


@tool("get_order_book", "Order book",
      "The order book for one side, in cents and contracts, plus "
      "microstructure (imbalance, depth) when include_micro is true. The "
      "levels are a snapshot the exchange can serve up to 30 seconds old; "
      "for the price right now use get_market, whose bid and ask say where "
      "they came from and how old they are (sources.bid.ageSec).",
      props={"conditionId": _CID, "side": _SIDE,
             "include_micro": {"type": "boolean"}},
      required=("conditionId",), read_only=True)
async def _get_book(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    p = {"conditionId": _cid(a), "side": a.get("side") or "yes"}
    out = await service._h_terminal_book(p)
    if a.get("include_micro"):
        out = {**out, "micro": await service._h_terminal_micro(p)}
    return out


@tool("get_price_history", "Price history",
      "Probability history for one side. interval: 1h, 6h, 1d, 1w or max.",
      props={"conditionId": _CID, "side": _SIDE,
             "interval": {"type": "string", "enum": ["1h", "6h", "1d", "1w", "max"]}},
      required=("conditionId",), read_only=True)
async def _get_history(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_history(
        {"conditionId": _cid(a), "side": a.get("side") or "yes",
         "interval": a.get("interval") or "1d"})


@tool("get_resolution_risk", "Resolution risk",
      "The app's resolution-risk report: ambiguous wording, oracle and "
      "deadline traps, each check with its evidence. Scored only over checks "
      "that actually resolved.",
      props={"conditionId": _CID}, required=("conditionId",), read_only=True)
async def _get_resolution(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_resolution({"conditionId": _cid(a)})


@tool("get_related_markets", "Related markets",
      "Other markets in the same event or on the same topic.",
      props={"conditionId": _CID, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
      required=("conditionId",), read_only=True)
async def _get_related(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_related(
        {"conditionId": _cid(a), "limit": a.get("limit") or 15})


@tool("get_cross_venue", "Kalshi comparison",
      "The matching Kalshi market, if one is found, with the reasons for and "
      "against the match. The spread is withheld below the confidence floor.",
      props={"conditionId": _CID}, required=("conditionId",), read_only=True)
async def _get_cross(a: dict) -> dict:
    import service
    service._sync_terminal_offline()
    return await service._h_terminal_cross_venue({"conditionId": _cid(a)})


@tool("manage_watchlist", "Watchlist",
      "The user's starred markets (shared with the app's Discover screen). "
      "action=list, add (conditionId, optional note) or remove (conditionId).",
      props={"action": {"type": "string", "enum": ["list", "add", "remove"]},
             "conditionId": _CID, "note": {"type": "string"}},
      required=("action",))
async def _watchlist(a: dict) -> dict:
    import service
    act = a["action"]
    p: dict = {"action": act}
    if act in ("add", "remove"):
        p["conditionId"] = _cid(a)
        if act == "add":
            m = (await service._h_terminal_market(
                {"conditionId": p["conditionId"]})).get("market") or {}
            p["title"] = m.get("title")
            p["note"] = a.get("note")
    return await service._h_terminal_watchlist(p)




# ════════════════════════════════════════════════════════════════════════
# Trading
# ════════════════════════════════════════════════════════════════════════

_ORDER_PROPS = {
    "mode": _MODE, "conditionId": _CID, "side": _SIDE,
    "action": {"type": "string", "enum": ["buy", "sell"]},
    "size": {"type": "number", "exclusiveMinimum": 0,
             "description": "Shares (contracts)."},
    "priceCents": {"type": "number", "minimum": 1, "maximum": 99,
                   "description": "Limit price in cents."},
    "orderType": {"type": "string", "enum": ["GTC", "FAK", "FOK"],
                  "description": "GTC rests on the book (default); FAK fills "
                                 "what it can and kills the rest; FOK all or "
                                 "nothing."},
    "postOnly": {"type": "boolean",
                 "description": "GTC only. The order may rest and may never take: "
                                "if it would trade right away it is refused and "
                                "nothing is sent. A fill is always a maker fill and "
                                "pays no fee, so a post-only buy's edge is checked "
                                "with no fee against its own minimum (get_status "
                                "rails.minEdgeRestingCents). Resting bids tend to "
                                "fill when the price is moving against them."},
    "forecastId": {"type": "integer", "minimum": 1,
                   "description": "Required for a buy: the id record_forecast "
                                  "returned for this market (yours, < 30 min old)."},
}
_ORDER_REQ = ("conditionId", "side", "action", "size", "priceCents")


@tool("preview_order", "Preview an order",
      "Run every check place_order would — your forecast and the edge after "
      "Polymarket's fee, your caps, rules, the loss stop, what you may sell — "
      "and price the order against the live book WITHOUT sending it: cost, "
      "fee, what is fillable now and at what average, slippage, tick and "
      "minimum size. wouldPlace=false comes with the blockers. On a buy, "
      "forecast says how old your forecast is and how far the YES mid has "
      "moved since you recorded it (null when either price was not read).",
      props=_ORDER_PROPS, required=_ORDER_REQ, read_only=True)
async def _preview(a: dict) -> dict:
    cfg = _cfg()
    agent = _agent(cfg)
    mode = _trade_mode(cfg, agent, a)
    v = await mcp_rails.vet(a, cfg, mode, agent,
                            fills_max_age=mcp_rails.PREVIEW_FILLS_MAX_AGE)
    pv = v.preview or {}
    return {
        "mode": mode, "wouldPlace": not v.blockers,
        "blockers": v.blockers, "warnings": v.warnings,
        "edgeCentsAfterFee": v.edge,
        "postOnly": bool(v.req.get("postOnly")),
        "forecast": v.forecast,
        ("costUsd" if v.req["action"] == "buy" else "proceedsUsd"): v.cost,
        "approvalNeeded": mode == "live" and bool(cfg.get("mcp_live_approval", True)),
        "ticket": {k: pv.get(k) for k in (
            "size", "priceCents", "fillableSize", "estimatedAvgCents",
            "slippageCents", "feeUsd", "maxPayoutUsd", "maxLossUsd", "tickSize",
            "minOrderSize", "bestBidCents", "bestAskCents", "crossesNow",
            "availableUsd")},
    }


@tool("place_order", "Place an order",
      "Send an order in the mode the user set for you (get_status yourMode). "
      "Paper fills against the REAL book into your own paper account; a GTC "
      "remainder rests and fills only if the market later trades through it. "
      "Live sends a real, signed order from the user's account — and unless the "
      "user turned approvals off it is QUEUED for their approval (check it "
      "with get_orders approvalId). A buy needs forecastId and must clear your "
      "minimum edge after fees and your caps; a sell may only sell what your "
      "own orders filled. Preview first.",
      props=_ORDER_PROPS, required=_ORDER_REQ, kind="trade", destructive=True)
async def _place(a: dict) -> dict:
    cfg = _cfg()
    agent = _agent(cfg)
    _trade_mode(cfg, agent, a)
    return await mcp_rails.place(a, _session())


@tool("cancel_order", "Cancel an order",
      "Cancel one of YOUR resting orders. Live: the venue orderId (only orders "
      "you placed). Paper: the localId from get_orders.",
      props={"mode": _MODE, "orderId": {"type": "string"},
             "localId": {"type": "integer"}},
      kind="trade")
async def _cancel(a: dict) -> dict:
    cfg = _cfg()
    agent = _agent(cfg)
    if a.get("localId") is not None and not a.get("orderId"):
        import terminal_paper
        with terminal_paper.using_book(mcp_rails.book_of(agent)):
            return await terminal_paper.cancel(int(a["localId"]))
    oid = str(a.get("orderId") or "").strip()
    if not oid:
        raise Refused("Pass orderId (live) or localId (paper).")
    if oid not in mcp_rails.agent_order_ids(agent["id"]):
        if oid in mcp_rails.agent_order_ids():
            raise Refused("That order was placed by another of the user's agents; an "
                          "agent cancels only its own.")
        raise Refused("That order was not placed by you; it is the user's.")
    import terminal_trade
    res = await terminal_trade.cancel(oid)
    if res.get("ok"):
        try:
            await mcp_rails.note_cancelled(oid)
        except Exception as e:  # noqa: BLE001 — the row keeps counting in full
            logger.debug("mcp cancel bookkeeping failed for %s: %s", oid, e)
    return res


@tool("close_position", "Close a position",
      "Exit (part of) a position YOU opened with one marketable fill-and-kill "
      "limited maxSlippageCents (default 5) below the live best bid. It fills "
      "every bid at or above that limit, best first, and kills the rest. size "
      "defaults to everything you may sell there. preview_only=true prices it "
      "without sending. Exits are never capped and never wait for approval.",
      props={"mode": _MODE, "conditionId": _CID, "side": _SIDE,
             "size": {"type": "number", "exclusiveMinimum": 0},
             "maxSlippageCents": {"type": "number", "minimum": 0, "maximum": 20},
             "preview_only": {"type": "boolean"}},
      required=("conditionId", "side"), kind="trade", destructive=True)
async def _close(a: dict) -> dict:
    import terminal_paper
    import terminal_trade
    cfg = _cfg()
    agent = _agent(cfg)
    mode = _trade_mode(cfg, agent, a)
    cid, side = _cid(a), str(a.get("side") or "").lower()
    if side not in ("yes", "no"):
        raise Refused("side must be 'yes' or 'no'.")
    req: dict = {"conditionId": cid, "side": side, "paper": mode == "paper"}
    if a.get("maxSlippageCents") is not None:
        req["maxSlippageCents"] = a["maxSlippageCents"]
    if mode == "paper" and a.get("size") is not None:
        req["size"] = a["size"]
    book = mcp_rails.book_of(agent) if mode == "paper" else "manual"
    client = str(_session().get("client") or "")
    async with mcp_rails.money_lock():
        if mode == "live":
            # Checked under the lock, with the send and the record: a sell
            # counts against the allowance only once it is recorded, so two
            # closes in parallel (or a close beside a sell or a new stop) both
            # used to see the whole allowance, and the second sold the user's
            # shares.
            await mcp_rails.refresh_fills()
            mine = mcp_rails.live_net(cid, side, agent["id"])
            want = float(a["size"]) if a.get("size") is not None else mine
            if mine <= 0 or want > mine + 1e-9:
                raise Refused(
                    f"Your own filled live buys of {side.upper()} here leave {mine:g} "
                    f"shares you may sell; the rest of this position is the user's "
                    f"(or another agent's).")
            req["size"] = want
        with terminal_paper.using_book(book):
            pv = await terminal_trade.sell_position_preview(req)
            if a.get("preview_only"):
                return pv
            if pv["blockers"]:
                raise Refused(pv["blockers"][0])
            req["confirmUsd"] = pv.get("proceedsUsd") or 0.0
            try:
                res = await terminal_trade.sell_position(req, origin="ai", priced=pv)
            except Exception as e:  # noqa: BLE001
                # As execute_live: the sell may or may not be on the venue.
                # Booked 'unknown' below, so it keeps counting against the
                # agent's allowance — with no row, a second close would sell
                # the same shares again, and they would be the user's.
                logger.warning("mcp: close send errored: %s: %s", type(e).__name__, e)
                res = {"ok": False, "status": "error",
                       "message": (f"send errored ({type(e).__name__}); it may or may "
                                   f"not have reached Polymarket — check your open "
                                   f"orders before retrying")}
        ev = None
        if mode == "live":
            # Booked at what it FILLED (mcp_rails.record_live_exit): an
            # unmatched or partial exit leaves the rest the agent's to sell.
            ev = mcp_rails.record_live_exit(
                client=client, agent_id=agent["id"], cid=cid, side=side,
                want=req.get("size"), res=res,
                title=str((pv or {}).get("title") or ""))
        else:
            sent = {"conditionId": cid, "side": side, "action": "sell",
                    "size": float(res.get("sentSize") or req.get("size") or 0.0),
                    "priceCents": float(res.get("limitCents") or 0.0), "orderType": "FAK"}
            mcp_rails.record(mode=mode, client=client, agent_id=agent["id"], req=sent,
                             ok=bool(res.get("ok")), message=str(res.get("message") or ""),
                             order_id=res.get("orderId"),
                             paper_local_id=res.get("localId"),
                             filled=res.get("filledSize"), avg=res.get("avgPriceCents"))
    if ev is not None:
        # Every live exit outcome is told — a refused or unknown one too.
        await mcp_rails.announce_order(ev)
    return res


@tool("cancel_all_orders", "Cancel all your orders",
      "Cancel every resting order YOU placed: live (on Polymarket) and in your "
      "paper account. Other orders on the account are the user's and are not "
      "touched.",
      kind="trade", destructive=True)
async def _cancel_all(_a: dict) -> dict:
    import db
    import terminal_paper
    import terminal_trade
    cfg = _cfg()
    agent = _agent(cfg)
    done, failed, filled = 0, 0, 0
    refused: dict[str, str] = {}
    for oid in sorted(mcp_rails.agent_order_ids(agent["id"])):
        with db.get_db() as conn:
            r = conn.execute(
                "SELECT status FROM terminal_orders WHERE order_id=? "
                "ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
        # A 'filled' row is still tried: reconcile marks an order filled on
        # its FIRST fill, so a partly filled GTC may still have a remainder.
        if r is not None and str(r["status"]) in ("canceled", "unmatched"):
            continue
        res = await terminal_trade.cancel(oid)
        if res.get("ok"):
            done += 1
            try:
                await mcp_rails.note_cancelled(oid)
            except Exception:  # noqa: BLE001
                pass
        elif res.get("notCanceled") and r is not None and str(r["status"]) in ("filled", "matched"):
            # 'matched' is the venue's own word for a fill-and-kill that crossed
            # (live 2026-10-12: the agent's close came back matched).
            # The venue refused because nothing of it is left on the book: it
            # filled. Not a failure, and not a cancel either.
            filled += 1
        else:
            failed += 1
            if res.get("notCanceled"):
                refused[oid] = str(res["notCanceled"])
    paper_done = 0
    with terminal_paper.using_book(mcp_rails.book_of(agent)):
        for o in terminal_paper.orders(500)["orders"]:
            if o["status"] in ("resting", "partial"):
                if (await terminal_paper.cancel(o["localId"])).get("ok"):
                    paper_done += 1
    return {"ok": failed == 0, "liveCancelled": done, "liveFailed": failed,
            "liveAlreadyFilled": filled, "liveRefused": refused,
            "paperCancelled": paper_done,
            "message": (f"Cancelled {done} live and {paper_done} paper order(s)"
                        + (f"; {failed} live cancel(s) failed" if failed else "")
                        + (f"; {filled} had already filled" if filled else "")
                        + ".")}


@tool("flatten_wallet", "Sell everything",
      "Close the main engine's positions AND sell every position on the live "
      "account at market — the user's too. A whole-account power: it needs the "
      "user's permission for whole-account changes. confirm=false (default) "
      "only prices it; confirm=true sends. Not capped: it is a way out.",
      props={"confirm": {"type": "boolean"}}, kind="switches", destructive=True)
async def _flatten(a: dict) -> dict:
    import service
    import terminal_trade
    if not a.get("confirm"):
        return await terminal_trade.flatten_preview()
    cfg = _cfg()
    agent = _agent(cfg)
    _require_account_live(cfg)
    r = await service._h_flatten({})
    fw = await terminal_trade.flatten_wallet(
        allow_trading=True, origin="ai", reason=f"AI agent {agent['name']} flatten")
    _push(f"AI agent {agent['name']} flattened the account — {fw.get('message')}")
    return {**fw, "mainEngineClosed": int(r.get("closed") or 0)}


@tool("manage_standing_instructions", "Stops, take-profits, alerts",
      "Standing instructions on YOUR positions, checked every few seconds "
      "while the app runs, in the mode you trade in (paper: your paper "
      "account; live: the real account, and only up to the shares your own "
      "orders filled — an armed live stop reserves them). action=list shows "
      "yours. action=add needs kind (stopLoss | takeProfit | alertAbove | "
      "alertBelow), conditionId, side, triggerCents (a probability point "
      "1–99, not a percent move) and size for stops/take-profits. "
      "action=cancel needs id.",
      props={"mode": _MODE,
             "action": {"type": "string", "enum": ["list", "add", "cancel"]},
             "kind": {"type": "string", "enum": [
                 "stopLoss", "takeProfit", "alertAbove", "alertBelow"]},
             "conditionId": _CID, "side": _SIDE,
             "triggerCents": {"type": "number", "minimum": 1, "maximum": 99},
             "size": {"type": "number", "exclusiveMinimum": 0},
             "note": {"type": "string"}, "id": {"type": "string"}},
      required=("action",), kind="trade")
async def _standing(a: dict) -> dict:
    import terminal_trade
    cfg = _cfg()
    agent = _agent(cfg)
    act = a["action"]
    mode = (_read_mode(cfg, agent, a) if act == "list"
            else _trade_mode(cfg, agent, a))
    if mode == "paper":
        return _paper_standing(a, agent)
    mine = mcp_rails._agent_standing(agent["id"])
    if act == "list":
        return {"instructions": mine}
    if act == "cancel":
        if not a.get("id"):
            raise Refused("id is required.")
        if a["id"] not in {e["id"] for e in mine}:
            raise Refused("No such instruction of yours.")
        return {"ok": True, "instructions": [
            e for e in terminal_trade.standing_cancel(str(a["id"]))
            if e.get("origin") == "ai" and str(e.get("agentId") or "default") == agent["id"]]}
    if act != "add":
        raise Refused("action must be list, add or cancel.")
    kind = str(a.get("kind") or "")
    item = {k: a.get(k) for k in ("kind", "conditionId", "side", "triggerCents", "size")}
    item["note"] = (f"[AI {agent['name']}] " + str(a.get("note") or "")).strip()
    item["origin"] = "ai"
    # A live stop reserves shares like a sell spends them, so its check and
    # add take the same lock: a stop added while a close of the same shares
    # was at the venue passed the check, and fired later on the user's shares.
    async with mcp_rails.money_lock():
        if kind in ("stopLoss", "takeProfit"):
            await mcp_rails.refresh_fills()
            have = mcp_rails.live_net(_cid(a), str(a.get("side") or ""), agent["id"])
            if float(a.get("size") or 0.0) > have + 1e-9:
                raise Refused(f"You may set a live stop on at most {have:g} shares here — "
                              f"what your own filled buys hold and no other stop "
                              f"already reserves.")
        try:
            entry = terminal_trade.standing_add(item)
        except terminal_trade.TerminalTradeError as e:
            raise Refused(str(e))
        _tag_standing(entry["id"], agent["id"])
    return {"ok": True, "instruction": {**entry, "agentId": agent["id"]}}


def _tag_standing(entry_id: str, agent_id: str) -> None:
    """Stamp which agent owns a live standing instruction, so its stops count
    against that agent's sell allowance (mcp_rails.live_net)."""
    import terminal_trade
    rows = terminal_trade.standing_list()
    for e in rows:
        if e.get("id") == entry_id:
            e["agentId"] = agent_id
    terminal_trade._standing_put(rows)


# No tool resets an agent's paper account. Its paper orders are both the
# record the user judges it by before letting it trade live and what the
# paper loss stop is rebuilt from (mcp_rails.day_loss), so an agent that
# could wipe them erased its losses and bought again the same day. Resetting
# it is the user's (service._h_mcp_paper with reset).


# ════════════════════════════════════════════════════════════════════════
# Engines
# ════════════════════════════════════════════════════════════════════════

_ENGINE_KEYS = {
    "main_engine": "enableTrading",
    "crypto15m": "crypto15mEnabled",
    "scripts_live": "scriptsLiveEnabled",
}


@tool("pause_all_engines", "Pause every engine",
      "Turn off every automated engine (main, crypto, live scripts). "
      "Always allowed. Does not cancel resting orders or sell anything.",
      kind="derisk")
async def _pause_all(_a: dict) -> dict:
    await _set_cfg({v: False for v in _ENGINE_KEYS.values()})
    _push("AI agent paused every engine.")
    return {"ok": True, "message": "All engines are off."}


@tool("set_engines", "Turn engines on or off",
      "Switch automated engines. engines maps main_engine / crypto15m / "
      "scripts_live to true or false. main_engine has no signal source on "
      "Polymarket US (no public trade tape), so it opens nothing there. "
      "Turning one OFF is always "
      "allowed; turning one ON needs the user's permission for whole-account changes, and the "
      "engine then trades with the user's own risk settings, not your caps.",
      props={"engines": {"type": "object", "properties": {
          k: {"type": "boolean"} for k in _ENGINE_KEYS},
          "additionalProperties": False}},
      required=("engines",), kind="mixed", destructive=True)
async def _set_engines(a: dict) -> dict:
    want = a.get("engines") or {}
    bad = [k for k in want if k not in _ENGINE_KEYS]
    if bad:
        raise Refused(f"unknown engine(s): {', '.join(bad)}")
    if any(bool(v) for v in want.values()):
        _require(_cfg(), "switches")
    await _set_cfg({_ENGINE_KEYS[k]: bool(v) for k, v in want.items()})
    if any(bool(v) for v in want.values()):
        _push("AI agent changed engines: " + ", ".join(
            f"{k} {'ON' if v else 'off'}" for k, v in want.items()))
    return {"ok": True, "engines": (await _get_status({}))["engines"]}


# ── Config classification ───────────────────────────────────────────────
# Every DEFAULT_CONFIG key is placed deliberately in exactly one class, and a
# test fails on a new key nobody placed (test_every_config_key_is_classified).
# An unclassified key is refused at runtime too, so forgetting fails closed.
#
#   protected  never agent-writable, whatever the permissions say: the agents'
#              own permissions and caps (mcp_*), the phone (remote_*), AI keys
#              and models (ai_*), Autopilot's budget (autopilot_*), the account
#              mode and its paper bankroll, the network, the fee tier, polling
#              and feeds, the research recorders, the script sandbox's budget.
#   live       starts an engine placing real orders, or moves a money rail.
#   settings   strategy posture: what to look for and how to enter.
#
# Polybot has ONE permission for both live and settings keys
# (mcp_allow_live_switches): a "posture" change to an engine that is running
# changes what it buys with the user's money just as surely as a size change.
# The only change free of it is switching an ENGINE off (ENGINE_OFF_SAFE):
# a False on any other key can loosen a rail (flatten_on_daily_stop,
# trading_hours_enabled, crypto15m_model_autopause, ...).

PROTECTED_PREFIXES = ("mcp_", "remote_", "ai_", "autopilot_")
PROTECTED = frozenset({
    "network", "account_mode", "terminal_paper_mode", "terminal_paper_bankroll_usd",
    "terminal_offline_mode", "terminal_notifications", "taker_rebate_pct",
    "script_hook_timeout_sec", "stats_chart_window_hours",
    # Polling and feeds: an agent has no business throttling the app's eyes.
    "trade_scan_interval", "position_poll_interval", "balance_poll_interval",
    "resolution_check_interval", "whale_scan_interval", "momentum_scan_interval",
    "market_refresh_interval", "db_cleanup_interval", "crypto15m_poll_sec",
    "script_poll_sec",     "crypto15m_ws_book",
    "crypto15m_spot_ws",     # The recorders. Turning them off would quietly stop collecting the data
    # the agent's own backtests and scoreboard are judged on.
    "crypto15m_record_signals", "main_record_signals",
})

LIVE = frozenset({
    "enable_trading", "trade_whales", "trade_momentum", "trade_convergence",
    "crypto15m_enabled", "scripts_live_enabled",     "sizing_mode", "base_size_fraction", "min_size_fraction", "max_size_fraction",
    "min_contracts", "max_contracts", "hard_max_position_usd", "min_cash_reserve_fraction",
    "max_open_positions", "max_positions_per_event", "max_daily_new_positions",
    "unlimited_daily_new_positions", "max_total_exposure_fraction", "start_bankroll_usd",
    "stop_loss_on_day", "take_profit_on_day", "take_profit_pct", "flatten_on_daily_stop",
    "lifetime_loss_limit_pct", "lifetime_loss_limit_usd", "trading_hours_enabled",
    "trading_hours_start", "trading_hours_end", "trading_days",
    "trading_timezone_offset_min", "max_resolution_days",
    "crypto15m_sizing_mode", "crypto15m_order_size", "crypto15m_balance_pct",
    "crypto15m_max_loss_pct", "crypto15m_streak_sizing", "crypto15m_streak_loss_pct",
    "crypto15m_streak_win_pct", "crypto15m_streak_max_mult", "crypto15m_max_concurrent",
    "crypto15m_daily_loss_limit", "crypto15m_lifetime_loss_limit_pct",
    "crypto15m_lifetime_loss_limit_usd", "crypto15m_take_profit_total",
    "crypto15m_autosize_to_min_notional", "crypto15m_stop_loss_pct",
    "crypto15m_model_autopause",
                                "script_max_entry_cents", "script_max_contracts", "script_max_open",
    "script_quote_max_inventory", "script_daily_loss_usd", "script_max_enabled", "script_market_max_spread_cents",
})

SETTINGS = frozenset({
    "min_edge_pts_whale", "min_edge_pts_momentum", "min_confidence_whale",
    "min_confidence_momentum", "min_entry_price_cents", "max_entry_price_cents",
    "allowed_momentum_signal_types", "allowed_categories", "allowed_whale_categories",
    "allowed_momentum_categories", "contrarian_only", "use_rules", "rules",
    "sizing_base_edge", "sizing_max_edge", "order_style", "cross_spread_fallback_offset",
    "order_expiration_sec", "max_signal_age_sec", "min_whale_usd", "min_whale_confidence",
    "min_whale_edge", "min_momentum_confidence", "min_momentum_edge", "min_entry_price_frac",
    "crypto15m_interval", "crypto15m_assets", "crypto15m_arb_detect",
    "crypto15m_arb_min_edge_cents", "crypto15m_imbalance_detect", "crypto15m_imbalance_levels",
    "crypto15m_imbalance_gate", "crypto15m_imbalance_gate_min", "crypto15m_indicator_detect",
    "crypto15m_min_rsi", "crypto15m_min_macd_hist", "crypto15m_use_rules", "crypto15m_rules",
    "crypto15m_rules_no", "crypto15m_time_delay_min", "crypto15m_entry_threshold",
    "crypto15m_entry_max", "crypto15m_exit_threshold", "crypto15m_take_profit",
    "crypto15m_take_profit_pct", "crypto15m_min_delta_pct", "crypto15m_entry_diff",
    "crypto15m_entry_style", "crypto15m_taker_fak", "crypto15m_maker_cancel_min",
    "crypto15m_maker_escalate", "crypto15m_maker_fill_sec", "crypto15m_hours_start_utc",
    "crypto15m_hours_end_utc", "crypto15m_direction_mode", "crypto15m_model_min_prob",
    "crypto15m_model_min_edge_cents", "crypto15m_model_final_minute",
    "crypto15m_model_max_book_gap_cents", "crypto15m_model_fm_max_book_gap_cents",
    "crypto15m_sell_into_strength",
    "crypto15m_sell_strength_cents",
                        "script_market_limit", "script_market_min_volume",
})

# Switching one of these OFF needs no permission: it only ever trades less.
ENGINE_OFF_SAFE = frozenset({
    "enable_trading", "trade_whales", "trade_momentum", "trade_convergence",
    "crypto15m_enabled", "scripts_live_enabled", })


def classify(key: str) -> str:
    """'protected' | 'live' | 'settings' | 'unknown'."""
    if key in PROTECTED or key.startswith(PROTECTED_PREFIXES):
        return "protected"
    if key in LIVE:
        return "live"
    if key in SETTINGS:
        return "settings"
    return "unknown"


def _snake_to_camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _same_type(default: Any, value: Any) -> bool:
    if isinstance(default, bool) or isinstance(value, bool):
        return isinstance(default, bool) and isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float))
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if isinstance(default, dict):
        return isinstance(value, dict)
    return False


@tool("update_config", "Change engine settings",
      "Change any engine setting: strategy thresholds, sizing, risk limits, "
      "crypto assets and posture, script rails, and so on. changes maps snake_case keys (see get_status "
      "include_config) to new values of the same type. Values are clamped to "
      "the app's safe ranges and the effective values come back. Switching "
      "an engine OFF (enable_trading, crypto15m_enabled, "
      "scripts_live_enabled, trade_*: false) is always "
      "allowed; anything else needs the user's permission for whole-account "
      "changes. Never changeable by an agent: the account's Paper/Live mode "
      "and paper bankroll, your own permissions and caps (mcp_*), remote "
      "control, AI and Autopilot settings, the network, polling, feeds and "
      "the data recorders. Backtest a change before making it.",
      props={"changes": {"type": "object"}}, required=("changes",),
      kind="mixed", destructive=True)
async def _update_config(a: dict) -> dict:
    from config import DEFAULT_CONFIG, _camel_to_snake
    changes = a.get("changes") or {}
    if not isinstance(changes, dict) or not changes:
        raise Refused("changes must be a non-empty object.")
    patch: dict = {}
    asked: dict = {}
    safe_only = True
    for k, v in changes.items():
        sk = _camel_to_snake(k) if any(c.isupper() for c in k) else k
        cls = classify(sk)
        if cls == "protected":
            if sk in ("account_mode", "terminal_paper_mode"):
                raise Refused("Only the user switches the account between Paper and "
                              "Live; an agent never can.")
            raise Refused(f"'{sk}' cannot be changed by an agent.")
        if sk not in DEFAULT_CONFIG:
            raise Refused(f"unknown setting '{sk}' (see get_status include_config).")
        if cls == "unknown":
            # A key nobody classified is refused, not guessed at.
            raise Refused(f"'{sk}' cannot be changed by an agent.")
        if not _same_type(DEFAULT_CONFIG[sk], v):
            raise Refused(f"'{sk}' takes a {type(DEFAULT_CONFIG[sk]).__name__}, "
                          f"not {type(v).__name__}.")
        ck = _snake_to_camel(sk)
        if _camel_to_snake(ck) != sk:
            raise Refused(f"'{sk}' cannot be patched from here; ask the user.")
        if not (v is False and sk in ENGINE_OFF_SAFE):
            safe_only = False
        patch[ck] = v
        asked[sk] = v
    if not safe_only:
        _require(_cfg(), "switches")
    await _set_cfg(patch)
    after = _cfg()
    eff = {sk: after.get(sk) for sk in asked}
    clamped = {sk: {"asked": asked[sk], "effective": eff[sk]}
               for sk in asked if asked[sk] != eff[sk]}
    if not safe_only:
        _push("AI agent changed settings: " + ", ".join(
            f"{k}={json.dumps(v, default=str)[:40]}" for k, v in eff.items()))
    return {"ok": True, "effective": eff, **({"clamped": clamped} if clamped else {})}


# ════════════════════════════════════════════════════════════════════════
# Backtests and research data
# ════════════════════════════════════════════════════════════════════════


@tool("backtest_crypto15m", "Backtest the crypto engine",
      "Replay the short-term crypto engine over recorded ticks through its "
      "live entry gates, fee-aware. config overrides keys for this run only.",
      props={"config": _CONFIG, "sinceDays": _SINCE}, read_only=True)
async def _bt_c15(a: dict) -> dict:
    import service
    return await service._h_c15_backtest(
        {"config": a.get("config") or {}, "sinceDays": a.get("sinceDays") or 60})


@tool("backtest_main_engine", "Backtest the main engine",
      "Replay recorded whale/momentum signals through the main engine's live "
      "gates with follower economics. config overrides keys for this run only.",
      props={"config": _CONFIG, "sinceDays": _SINCE}, read_only=True)
async def _bt_main(a: dict) -> dict:
    import service
    return await service._h_main_backtest(
        {"config": a.get("config") or {}, "sinceDays": a.get("sinceDays") or 60})


@tool("backtest_script", "Backtest a strategy script",
      "Backtest a strategy script over the recorded tick corpus — pass code "
      "(see get_script_docs) or the id of a saved script. Never places "
      "orders. Needs the user's scripts permission, because the code runs: "
      "in a separate process, on a read-only copy of the recorded data. Code "
      "you write must stay inside the agent-script rules (plain computation; "
      "imports only from math, statistics, datetime, collections, itertools, "
      "functools, re, json, random, bisect, heapq, decimal, fractions).",
      props={"code": {"type": "string"}, "id": {"type": "string"},
             "config": _CONFIG, "sinceDays": _SINCE,
             "assets": {"type": "array", "items": {"type": "string"}}},
      # NOT read-only: it runs code the agent wrote, so clients ask before it
      # runs and the audit log records it like any other action
      kind="scripts")
async def _bt_script(a: dict) -> dict:
    import service
    _require(_cfg(), "scripts")
    if not a.get("code") and not a.get("id"):
        raise Refused("pass code or id.")
    code = a.get("code")
    if code:
        _refuse_critical(code)
    p = {"code": code, "id": a.get("id"), "config": a.get("config") or {},
         "sinceDays": a.get("sinceDays") or 60, "isolate": True}
    if a.get("assets") is not None:
        p["assets"] = a["assets"]
    return await service._h_script_backtest(p, agent=_agent_label())


@tool("parlay_schedule", "Per-hour crypto schedule",
      "The crypto engine's per-hour schedule, mined from the recorded corpus "
      "with a holdout. action=generate mines one (save=true stores it "
      "UNARMED); status shows the stored one; arm makes it OWN the crypto "
      "engine's entries (seated hours trade their generated settings, "
      "unseated hours do not trade); disarm hands entries back. save and arm "
      "need the user's permission for whole-account changes.",
      props={"action": {"type": "string", "enum": ["generate", "status", "arm", "disarm"]},
             "interval": {"type": "string", "enum": ["5m", "15m", "1h"]},
             "sinceDays": _SINCE,
             "holdoutDays": {"type": "integer", "minimum": 1, "maximum": 90},
             "minWinRate": {"type": "number", "minimum": 0, "maximum": 1},
             "minTrades": {"type": "integer", "minimum": 1},
             "save": {"type": "boolean"}},
      required=("action",), kind="mixed")
async def _parlay(a: dict) -> dict:
    import service
    act = a["action"]
    if act == "status":
        return await service._h_c15_parlay_status({})
    if act in ("arm", "disarm"):
        if act == "arm":
            _require(_cfg(), "switches")
        res = await service._h_c15_parlay_arm({"armed": act == "arm"})
        if res.get("error"):
            raise Refused(res["error"])
        if act == "arm":
            _push("AI agent ARMED the crypto parlay schedule.")
        return {"ok": True, **res}
    save = bool(a.get("save"))
    if save:
        _require(_cfg(), "switches")
    p = {k: a[k] for k in ("interval", "sinceDays", "holdoutDays",
                           "minWinRate", "minTrades") if k in a}
    p["save"] = save
    return await service._h_c15_parlay_generate(p)


_SQL_MAX_ROWS = 2000
_SQL_TIME_BUDGET_SEC = 20.0
# The app's own key-value state, not recorded data. The live remote-control
# pairing code is in it (remote.new_pair_code): an agent that could read it,
# even one LIKE at a time, could pair a stranger's chat account as a
# controller of the app.
_SQL_PRIVATE_TABLES = frozenset({"app_kv"})


def _query_ro(sql: str, limit: int) -> dict:
    """Run one read-only statement against the app's own SQLite DB.

    Three locks, any one sufficient: the connection is opened mode=ro, it is
    `query_only`, and an authorizer refuses every action that is not a read
    (ATTACH included, so it cannot reach some other file on disk). The same
    authorizer refuses any read of a private table's columns, wherever in
    the statement it is. A progress handler stops a runaway query at the
    time budget."""
    import sqlite3
    import time as _t
    import db
    path = db.db_path()
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only = ON")
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                   sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
        private: list = []

        def _auth(action, arg1, *_args):
            if (action == sqlite3.SQLITE_READ
                    and str(arg1 or "").lower() in _SQL_PRIVATE_TABLES):
                private.append(str(arg1).lower())
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
        conn.set_authorizer(_auth)
        deadline = _t.monotonic() + _SQL_TIME_BUDGET_SEC
        conn.set_progress_handler(lambda: 1 if _t.monotonic() > deadline else 0, 10_000)
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        except (sqlite3.DatabaseError, sqlite3.Warning) as e:
            if private:
                raise Refused(f"{private[0]} is the app's own private state (remote "
                              f"control, audit logs, internal settings), not recorded "
                              f"data, so it cannot be queried.")
            if isinstance(e, sqlite3.OperationalError) and "interrupted" in str(e).lower():
                raise Refused(f"query stopped at the {_SQL_TIME_BUDGET_SEC:.0f}s "
                              f"budget — narrow it with WHERE / LIMIT.")
            raise Refused(f"SQL error: {e}. Only one read-only SELECT is allowed.")
        return {"columns": cols, "rows": [list(r) for r in rows[:limit]],
                "rowCount": min(len(rows), limit), "truncated": len(rows) > limit}
    finally:
        conn.close()


# What a column list cannot say: units, and which side a price is for. Two
# columns one letter apart in crypto15m_ticks hold different units for
# different sides, and a query that mixed them read as an 8% edge.
_SCHEMA_NOTES = {
    "crypto15m_ticks": (
        "yes_bid, yes_ask, up_prob, up_ask and no_ask are fractions from 0 to 1. "
        "yes_* and up_* are the Up (YES) side; no_ask is the Down (NO) side. "
        "ws_bid and ws_ask are CENTS (1 to 99) and are for the FAVOURITE side at "
        "that tick: the Up side when up_prob >= 0.5, otherwise the Down side. Do "
        "not compare ws_* with yes_* without converting both the unit and the "
        "side. edge_net_cents is cents; model_prob is a fraction, and neither "
        "has beaten the market price (see discover_markets). observed_at "
        "is UTC. Outcomes: join crypto15m_signals on ticker (up_won)."),
    "all tables": "Timestamps are UTC. A NULL means the value was not read; it is not zero.",
}


def _schema() -> list[dict]:
    import db
    with db.get_db() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
            if str(r[0]).lower() not in _SQL_PRIVATE_TABLES]
        return [{"table": t, "columns": [
            f"{c[1]} {c[2]}".strip() for c in conn.execute(f'PRAGMA table_info("{t}")')]}
            for t in tables]


@tool("research_data", "Query the recorded data",
      "The app records market data for research: crypto Up/Down ticks with "
      "spot, book and indicators; whale and momentum signals; trades; fills; "
      "resolutions. With no sql: an inventory of what is recorded plus the "
      "full schema. With sql: run ONE read-only SQLite SELECT and get up to "
      "`limit` rows (default 200, max 2000; 20s budget) — test your own "
      "hypotheses here before backtesting or trading them. "
      "include_crypto_history adds the crypto engine's resolved trades.",
      props={"sql": {"type": "string", "description":
                     "One read-only SQLite SELECT. Leave it out for the inventory "
                     "and schema."},
             "limit": {"type": "integer", "minimum": 1, "maximum": _SQL_MAX_ROWS},
             "include_crypto_history": {"type": "boolean"}}, read_only=True)
async def _research(a: dict) -> dict:
    import service
    if a.get("sql"):
        limit = max(1, min(_SQL_MAX_ROWS, int(a.get("limit") or 200)))
        return await asyncio.to_thread(_query_ro, str(a["sql"]), limit)
    out = {"collection": await service._h_collection_stats({}),
           "schema": await asyncio.to_thread(_schema),
           "notes": _SCHEMA_NOTES}
    if a.get("include_crypto_history"):
        out["cryptoHistory"] = await service._h_c15_history({"limit": 200})
    return out


# ════════════════════════════════════════════════════════════════════════
# Scripts
# ════════════════════════════════════════════════════════════════════════


def _agent_label() -> str:
    """Who is writing: stamped on the script row as `agent:<label>`."""
    s = _session()
    return str(s.get("agent") or s.get("client") or "agent")[:60]


def _refuse_critical(code: str, *, agent_rules: bool = True) -> None:
    """An agent's script is refused unless it stays inside the agent-script
    allowlist (script_audit.validate_agent: plain computation, a small set
    of pure stdlib imports, no dunders, no getattr/open/eval...). The
    critical-finding audit runs first so its plainer wording leads. There is
    no human reading agent code before it is backtested, so nothing here is
    "shown for review" — it is refused."""
    import script_audit
    import service
    clean = service._sanitize_script_code(code)
    rep = script_audit.audit(clean)
    if not rep.get("ok"):
        crit = [f for f in rep.get("findings") or [] if f.get("severity") == "critical"]
        lines = "; ".join(f"line {f.get('line')}: {f.get('message')}" for f in crit[:5])
        raise Refused(
            "The script audit found critical issues, and scripts written by "
            f"an agent with critical findings are refused: {lines or rep.get('summary')}")
    bad = script_audit.validate_agent(clean) if agent_rules else []
    if bad:
        raise Refused(
            "Scripts written by an AI agent may only use plain computation: "
            "imports from " + ", ".join(sorted(script_audit.AGENT_MODULES))
            + "; no dunder names or '__' strings, no private (_x) attributes, no "
            "getattr/setattr/vars/globals/eval/exec/open/str.format. Fix: "
            + "; ".join(bad[:6]))


@tool("get_script_docs", "Script API docs",
      "The strategy-script API (hooks, ctx fields, order helpers, rails) "
      "rendered from the live modules, plus a context pack with examples.",
      read_only=True)
async def _script_docs(_a: dict) -> dict:
    import service
    return {"docs": await service._h_script_api_docs({}),
            "contextPack": await service._h_script_context_pack({})}


@tool("list_scripts", "List scripts",
      "Saved strategy scripts with their state (enabled, shadow or live), "
      "stats and last error. With id + include_shadow, also that script's "
      "shadow (would-have-traded) orders.",
      props={"id": {"type": "string"}, "include_shadow": {"type": "boolean"},
             "limit": _LIMIT}, read_only=True)
async def _list_scripts(a: dict) -> dict:
    import service
    out = {"scripts": await service._h_scripts_list({})}
    if a.get("id") and a.get("include_shadow"):
        out["shadowOrders"] = await service._h_script_shadow_orders(
            {"id": a["id"], "limit": a.get("limit") or 100})
    return out


@tool("validate_script", "Validate a script",
      "Check script code without saving or running it: errors, warnings and "
      "the security audit.",
      props={"code": {"type": "string"}}, required=("code",), read_only=True)
async def _validate(a: dict) -> dict:
    import script_audit
    import service
    out = dict(await service._h_script_validate({"code": a["code"]}))
    # What save_script / backtest_script will hold THIS caller's code to.
    out["agentRuleErrors"] = script_audit.validate_agent(
        service._sanitize_script_code(a["code"]))
    return out


@tool("save_script", "Save a script",
      "Create or update a strategy script (pass id to update). A script you "
      "save, or any script whose code you change, lands DISABLED and in "
      "SHADOW mode and waits for the user to review it: it cannot run until "
      "the user enables it from the Scripts page. Needs the scripts "
      "permission; code outside the agent-script rules is refused.",
      props={"code": {"type": "string"}, "id": {"type": "string"},
             "name": {"type": "string"}, "description": {"type": "string"}},
      required=("code",), kind="scripts")
async def _save_script(a: dict) -> dict:
    import db
    import service
    cfg = _cfg()
    _require(cfg, "scripts")
    _refuse_critical(a["code"])
    sid = str(a.get("id") or "")
    existing = None
    if sid:
        with db.get_db() as conn:
            existing = db.get_user_script(conn, sid)
    # A script armed for real orders was armed for the code the user read.
    # ARMED means dry_run=0, enabled or not: a user-armed script the daily-loss
    # breaker or a hook error switched off is still armed, and re-enabling it
    # would put new code straight onto real money. Rewriting one needs
    # whole-account powers, and the new code goes back to SHADOW either way.
    armed = bool(existing) and not bool(existing.get("dry_run", 1))
    code_changed = bool(existing) and (service._sanitize_script_code(existing.get("code"))
                                       != service._sanitize_script_code(a["code"]))
    if armed and code_changed:
        _require(cfg, "switches")
    p = {"code": a["code"], "id": sid,
         "name": a.get("name") or "", "description": a.get("description") or "",
         # an existing script keeps the user's notes
         "notes": (str(existing.get("notes") or "") if existing
                   else "Written by an AI agent over MCP.")}
    # The service forces agent code off, into shadow and needs-review, and
    # tells the engine; the explicit disarm below is the belt for the armed
    # case, so the ledger and the phone say what happened.
    out = await service._h_script_save(p, agent=_agent_label())
    if armed and code_changed:
        await service._h_script_set_dry_run({"id": sid, "dryRun": True},
                                            agent=_agent_label())
        name = str(existing.get("name") or sid)
        _push(f"AI agent rewrote live script {name}: it is back in SHADOW and "
              f"switched off until you review it.")
        out = dict(out, disarmed=True, note=(
            "This script was armed for real orders. Its new code is in SHADOW, "
            "switched off, until the user reviews it and arms it again."))
    elif existing is None or code_changed:
        out = dict(out, note=(
            "Saved DISABLED, in shadow. The user has to review this code and "
            "enable it from the Scripts page before it runs."))
    return {"ok": True, **out}


@tool("set_script_state", "Enable / disable a script",
      "enabled=true runs a saved script in SHADOW mode (it logs what it would "
      "trade) — only for code the user has already reviewed: a script you "
      "wrote or changed waits for the user to enable it themselves. "
      "enabled=false / live=false (back to shadow) are always allowed. Arming "
      "a script for real orders (live=true) is the user's alone and is "
      "refused.",
      props={"id": {"type": "string"}, "enabled": {"type": "boolean"},
             "live": {"type": "boolean"}},
      required=("id",), kind="mixed", destructive=True)
async def _script_state(a: dict) -> dict:
    import db
    import service
    sid = str(a["id"])
    cfg = _cfg()
    if a.get("live"):
        raise Refused(
            "Only the user can arm a script for real orders, from the Scripts "
            "page. You can run it in shadow (enabled=true, once the user has "
            "reviewed its code) and show them the shadow record.")
    if a.get("enabled"):
        _require(cfg, "scripts")
        with db.get_db() as conn:
            row = db.get_user_script(conn, sid)
        if not row:
            raise Refused("script not found")
        if row.get("needs_review"):
            raise Refused(
                "An AI agent wrote this script's code and the user has not "
                "reviewed it yet. Ask the user to read it and enable it from "
                "the Scripts page.")
        # the user's own code: no critical findings; an agent's: its rules too
        _refuse_critical(str(row.get("code") or ""),
                         agent_rules=str(row.get("author") or "").startswith("agent"))
    out: dict = {"ok": True}
    if "enabled" in a:
        out.update(await service._h_script_set_enabled(
            {"id": sid, "enabled": bool(a["enabled"])}, agent=_agent_label()))
    if "live" in a:     # only live=false reaches here: back to shadow
        out.update(await service._h_script_set_dry_run(
            {"id": sid, "dryRun": True}, agent=_agent_label()))
    return out


# ════════════════════════════════════════════════════════════════════════
# Stops / take-profits / alerts on the AGENT'S paper account
#
# The live standing instructions refuse to trade in paper mode (by design:
# they are written against real positions). The agent's paper account needs
# its own, so they live here, under their own kv key, and exit through the
# same `sell_position` path the close_position tool uses — real book, real
# fees, into the "ai" paper book. Ticked by the service's paper-book block.
# ════════════════════════════════════════════════════════════════════════

_PAPER_STANDING_KV = "mcp:paper_standing"
_PAPER_STANDING_MAX = 200


def _ps_load() -> list[dict]:
    import db
    with db.get_db() as conn:
        raw = db.kv_get(conn, _PAPER_STANDING_KV)
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        rows = []
    return rows if isinstance(rows, list) else []


def _ps_retired(r: dict) -> bool:
    """Done for good: it fired (a stop that sold, or could not, or an alert).
    Everything else is still watching a price and is never evicted."""
    return bool(isinstance(r, dict) and r.get("firedAt"))


def _ps_save(rows: list[dict]) -> None:
    """Write the list, pruning only RETIRED entries, oldest first, when it is
    over the cap. It used to write `rows[:200]`, and adds go on the front, so
    the 201st add silently dropped the OLDEST entry whatever it was — an armed
    stop on an open paper position included. The cap is enforced where
    instructions are added, by refusing the add (as the live list does)."""
    import db
    rows = [r for r in rows if isinstance(r, dict)]
    over = len(rows) - _PAPER_STANDING_MAX
    if over > 0:
        retired = sorted((r for r in rows if _ps_retired(r)),
                         key=lambda r: str(r.get("firedAt") or r.get("createdAt") or ""))
        drop = {id(r) for r in retired[:over]}
        rows = [r for r in rows if id(r) not in drop]
    with db.get_db() as conn:
        db.kv_set(conn, _PAPER_STANDING_KV, json.dumps(rows))


# The only fields the TICK owns. Everything else on an instruction belongs to
# the agent's add/cancel, which runs concurrently with the tick's awaits.
_PS_TICK_FIELDS = ("firedAt", "result")


def _ps_merge_save(worked: list[dict]) -> list[dict]:
    """Write a tick's results back WITHOUT clobbering an add or cancel the
    agent made while the tick was awaiting markets and sells — the paper twin
    of terminal_trade._standing_merge_put. It used to write back the list it
    read at the top, which dropped an instruction added mid-tick and
    resurrected one cancelled mid-tick.

    Re-read, and merge per instruction: one that no longer exists is not
    re-added; one added meanwhile is kept as written; the tick's own fields
    land on the survivors; `armed` only ever moves True → False."""
    by_id = {str(r.get("id")): r for r in worked if isinstance(r, dict)}
    merged: list[dict] = []
    for cur in _ps_load():
        if not isinstance(cur, dict):
            continue
        e = by_id.get(str(cur.get("id")))
        if e is None:
            merged.append(cur)
            continue
        out = dict(cur)
        for k in _PS_TICK_FIELDS:
            if k in e:
                out[k] = e[k]
        if not e.get("armed"):
            out["armed"] = False
        merged.append(out)
    _ps_save(merged)
    return merged


def _ps_owner(r: dict) -> str:
    # Entries from before named agents belong to Default.
    return str(r.get("agentId") or mcp_rails.DEFAULT_AGENT)


def _paper_standing(a: dict, agent: dict) -> dict:
    import uuid
    import terminal_trade
    act = a["action"]
    rows = _ps_load()
    mine = [r for r in rows if _ps_owner(r) == agent["id"]]
    if act == "list":
        return {"instructions": mine}
    if act == "cancel":
        if not a.get("id"):
            raise Refused("id is required.")
        if a["id"] not in {r.get("id") for r in mine}:
            raise Refused("No such instruction of yours.")
        rows = [r for r in rows if r.get("id") != a["id"]]
        _ps_save(rows)
        return {"ok": True, "instructions": [r for r in rows
                                             if _ps_owner(r) == agent["id"]]}
    if act != "add":
        raise Refused("action must be list, add or cancel.")
    kind = str(a.get("kind") or "")
    if kind not in terminal_trade._VALID_KINDS:
        raise Refused("kind must be stopLoss, takeProfit, alertAbove or alertBelow.")
    trig = a.get("triggerCents")
    if not isinstance(trig, (int, float)) or not (0 < trig < 100):
        raise Refused("triggerCents is a probability point between 0 and 100.")
    size = a.get("size")
    if kind in ("stopLoss", "takeProfit") and (not isinstance(size, (int, float)) or size <= 0):
        raise Refused("a stop or take-profit needs the number of contracts to close.")
    # Refuse rather than evict: an armed stop is never dropped to make room.
    active = sum(1 for r in rows if not _ps_retired(r))
    if active >= _PAPER_STANDING_MAX:
        raise Refused(
            f"The paper stop/take-profit list is full: {active} instructions are "
            f"still watching a price (the limit is {_PAPER_STANDING_MAX}). Cancel "
            f"ones you no longer need (standing action='list', then "
            f"action='cancel'), then add this again.")
    entry = {
        "id": f"ps_{uuid.uuid4().hex[:10]}", "conditionId": _cid(a),
        "side": "no" if str(a.get("side") or "yes").lower() == "no" else "yes",
        "kind": kind, "triggerCents": round(float(trig), 2),
        "size": float(size) if kind in ("stopLoss", "takeProfit") else None,
        "note": a.get("note") or None, "armed": True, "agentId": agent["id"],
        "createdAt": __import__("mcp_server")._iso(),
        "firedAt": None, "result": None,
    }
    rows.insert(0, entry)
    _ps_save(rows)
    return {"ok": True, "instruction": entry}


async def paper_standing_tick() -> int:
    """Evaluate the agent's armed paper instructions once. → how many fired."""
    import terminal as term
    import terminal_paper
    import terminal_trade
    rows = _ps_load()
    live = [r for r in rows if r.get("armed") and not r.get("firedAt")]
    if not live:
        return 0
    fired = 0
    touched: list[dict] = []
    marks: dict[str, Any] = {}
    for e in live:
        cid = e["conditionId"]
        if cid not in marks:
            try:
                # Marked off the live book: Gamma's price can sit tens of
                # cents off it, which fires (or holds) a stop on a stale number.
                marks[cid] = await term.market(cid)
                if marks[cid]:
                    await term.clob_quotes([marks[cid]])
            except Exception:  # noqa: BLE001 — an unreadable market waits
                marks[cid] = None
        m = marks[cid]
        if not m or m.get("probability") is None:
            continue
        p = float(m["probability"])
        price = p if e["side"] == "yes" else round(100.0 - p, 2)
        trig = float(e["triggerCents"])
        kind = e["kind"]
        hit = ((kind in ("stopLoss", "alertBelow") and price <= trig)
               or (kind in ("takeProfit", "alertAbove") and price >= trig))
        if not hit:
            continue
        # Cancelled while this tick was awaiting the book: it must not fire.
        if str(e.get("id")) not in {str(r.get("id")) for r in _ps_load()
                                    if isinstance(r, dict)}:
            continue
        e["firedAt"] = __import__("mcp_server")._iso()
        e["armed"] = False
        touched.append(e)
        fired += 1
        if kind.startswith("alert"):
            e["result"] = f"Price reached {price:g}¢ (trigger {trig:g}¢)."
            continue
        req = {"conditionId": cid, "side": e["side"], "size": e["size"], "paper": True}
        try:
            book = mcp_rails.book_of({"id": _ps_owner(e)})
        except ValueError:
            e["result"] = "Trigger hit, but the agent that set it is not valid; not sold."
            continue
        with terminal_paper.using_book(book):
            pv = await terminal_trade.sell_position_preview(req)
            if pv["blockers"]:
                e["result"] = f"Trigger hit at {price:g}¢; not sold: {pv['blockers'][0]}"
                continue
            req["confirmUsd"] = pv.get("proceedsUsd") or 0.0
            res = await terminal_trade.sell_position(req, origin="ai")
        e["result"] = f"Trigger hit at {price:g}¢ — {res.get('message')}"
    if touched:
        # Merge, never blind-write: adds/cancels made during the awaits above
        # are on disk, not in `rows`.
        _ps_merge_save(touched)
    return fired
