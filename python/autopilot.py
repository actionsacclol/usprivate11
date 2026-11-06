"""Autopilot: the app runs one of the user's agents on a schedule, with the
user's own AI key.

Everything else that talks to a model in this app does it on a click, because
every call is billed to the user. Autopilot is the one deliberate exception,
and it is built so that it cannot become a surprise on a bill or in an
account (ported from kryptrader's autopilot.py):

  * **Off until switched on** (`autopilot_enabled`, only an explicit True),
    separate from the MCP server and from every agent permission.
  * **It is just another MCP client.** It is offered exactly the tools a
    connected Claude or Cursor sees (`mcp_tools.list_tools`) and calls every
    one through `mcp_tools.call(name, args, session)` with the session
    `{"client": "autopilot (<model>)", "agent": autopilot_agent_id}` — the
    same named-agent rules, forecast-before-buy gate, edge after fee, caps,
    daily loss stop, "sells only what it opened", the user's trade mode
    (paper unless the user set live) and, live, the same approval queue. The
    per-agent rate limit applies too. Autopilot adds no order path of its own.
  * **It runs AS a named agent.** Its guide shapes the prompt and its rules
    bind every order. A deleted or switched-off agent blocks Autopilot; it
    never falls back to Default (whose rules are usually looser).
  * **Three budgets, all checked here BEFORE a call is made:** runs per UTC
    day and tokens per UTC day (summed from `autopilot_runs`) before a run
    starts, tokens again before every model call, and tool calls per run
    before every batch of tools. A run that would cross a budget stops, and
    says so.
  * **Missing usage is an estimate, never zero.** A provider that reports no
    token counts for a turn (a local server can omit them) is charged an
    estimate from text length, and the run's report says the figures are
    estimates.
  * **A model that cannot call tools is refused BEFORE a run is recorded**
    — Autopilot does nothing except through tools.
  * **One run at a time**; switching it off stops a scheduled run at the next
    step; every run is recorded with model, steps, tokens, cost where a price
    is published, each tool it called, and its own report.

The key, provider and model are the AI analysis settings (ai_provider /
ai_model): one key, one bill. Anthropic and OpenAI run through their async
SDKs under the terminal gate (offline switch, 429 park, privacy counts);
OpenRouter, Gemini, Ollama and LM Studio through ai_providers.ToolChat.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import ai_analysis
import ai_providers
import db
import mcp_agents
import mcp_rails
import mcp_server
import mcp_tools

logger = logging.getLogger("krypt.autopilot")

_MAX_TOKENS = 4000
# Thinking counts toward the limit on Anthropic and is always on for its newest models.
_ANTHROPIC_MAX_TOKENS = 16000
_TIMEOUT_SEC = 120.0
# A single tool result fed back to the model. Past this the tail is cut and
# the model is told so.
_RESULT_CHARS = 12000

_LOCK = asyncio.Lock()


class _State:
    running: bool = False
    started_at: Optional[str] = None
    current_step: int = 0
    last_error: Optional[str] = None


STATE = _State()


SYSTEM = """You are Krypt Autopilot: one of the user's own named agents, run \
on a schedule inside their Krypt PolyBot US app (a Polymarket trading app on the \
user's own machine, with the user's own account). Nobody is watching this run \
live; the user reads your report afterwards.

{instructions}

How to run:
- Start with get_status. It says which agent you are and the mode you trade \
in: off, paper or live. The USER sets that mode, never you. Paper — your own \
simulated account, filled from the real order book — is the default; you \
trade real money only if the user has set you live, and live orders usually \
wait for the user's approval. Never ask for live, never try to change it.
- Work the user's mission below. Be selective: a few markets examined \
properly (rules text first) beats many skimmed.
- Before ANY buy: record_forecast with your own fair value and a rationale, \
then preview_order, then place_order citing that forecastId. A buy without a \
forecast of yours on that market from the last 30 minutes is refused, and so \
is one whose price does not beat your forecast by the minimum edge after \
Polymarket's fee.
- Record a forecast for every market you form a view on, including ones you \
decide not to trade. Those forecasts are scored against the market once it \
resolves; that record is how the user learns whether you are any good.
- You sell and cancel only what you opened. A refusal explains itself: do not \
retry around it, and never split an order to get under a cap.
- Do not switch engines on, change settings, write or arm scripts, or flatten \
the account unless the mission explicitly asks for it; those need the user's \
separate permission and are refused without it.
- You have at most {max_steps} tool calls in this run. Stop early when you \
are done.
- Finish with a short plain-text report: what you looked at, forecasts \
recorded (your fair value vs the market), orders placed or queued for \
approval and why, and anything the user should look at.

YOUR TOOLS ({tool_count}): {tool_names}

THE USER'S MISSION
{mission}"""

DEFAULT_MISSION = (
    "Look through markets closing in the next few days. Pick the ones where "
    "you can actually reason about the outcome, read their resolution rules, "
    "and record honest forecasts. Trade only where your edge after fees clears "
    "the minimum, and keep sizes small."
)


def run_agent(cfg: dict) -> Optional[dict]:
    """The named agent Autopilot runs as, or None if it no longer exists."""
    return mcp_agents.get(cfg, str(cfg.get("autopilot_agent_id") or mcp_agents.DEFAULT_ID))


def _live_cfg() -> dict:
    """The config as it is NOW (switching off mid-run must be seen)."""
    try:
        return mcp_rails.cfg()
    except Exception:  # noqa: BLE001
        return {}


# ── Persistence ─────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def usage_today() -> dict:
    with db.get_db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens),0) AS i, "
            "COALESCE(SUM(output_tokens),0) AS o, SUM(cost_usd) AS c, "
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unpriced "
            "FROM autopilot_runs WHERE substr(started_at,1,10)=?", (_today(),)).fetchone()
    runs_ = int(r["runs"] or 0)
    return {
        "runs": runs_,
        "tokens": int(r["i"] or 0) + int(r["o"] or 0),
        # A day with any unpriced run has no honest dollar total.
        "costUsd": (round(float(r["c"]), 4) if r["c"] is not None and not r["unpriced"]
                    else None),
    }


def last_started_at() -> Optional[str]:
    with db.get_db() as conn:
        r = conn.execute("SELECT MAX(started_at) FROM autopilot_runs").fetchone()
    return r[0] if r and r[0] else None


def runs(limit: int = 30) -> list[dict]:
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM autopilot_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        try:
            tools = json.loads(r["tool_log"] or "[]")
        except ValueError:
            tools = []
        out.append({
            "id": int(r["id"]), "startedAt": r["started_at"], "finishedAt": r["finished_at"],
            "trigger": r["trigger"], "agentId": r["agent_id"],
            "provider": r["provider"], "model": r["model"],
            "status": r["status"], "steps": int(r["steps"] or 0),
            "inputTokens": r["input_tokens"], "outputTokens": r["output_tokens"],
            "tokensEstimated": bool(r["tokens_estimated"]),
            "costUsd": r["cost_usd"], "summary": r["summary"] or "",
            "error": r["error"], "tools": tools,
        })
    return out


# ── Budgets / schedule ──────────────────────────────────────────────────────

def _limits(cfg: dict) -> dict:
    return {
        "intervalMin": int(cfg.get("autopilot_interval_min") or 60),
        "maxRunsPerDay": int(cfg.get("autopilot_max_runs_per_day") or 12),
        "dailyTokenBudget": int(cfg.get("autopilot_daily_token_budget") or 1_500_000),
        "maxSteps": int(cfg.get("autopilot_max_steps") or 15),
    }


def _provider(cfg: dict) -> str:
    return ai_analysis.norm_provider(cfg.get("ai_provider"))


def _model(cfg: dict) -> str:
    return ai_analysis.configured_model(cfg)


def blocked_reason(cfg: dict) -> Optional[str]:
    """Why a run cannot start now, or None. No network: the scheduler and the
    status poll read it on the event loop."""
    lim = _limits(cfg)
    provider = _provider(cfg)
    caps = ai_providers.CAPS[provider]
    agent = run_agent(cfg)
    if agent is None:
        return ("The agent Autopilot runs as no longer exists. Pick one under "
                "AI Agents → Autopilot → Run as agent.")
    if not agent.get("enabled", True):
        return f"Autopilot runs as {agent['name']}, which is switched off."
    if not ai_analysis.ready(provider):
        return f"No {caps.label} key — set it under Settings → AI analysis."
    try:
        import terminal
        if not caps.local and terminal.is_offline():
            return (f"Offline mode is on, so Autopilot cannot reach {caps.label}. "
                    f"Turn it off in Settings → Data & privacy.")
    except Exception:  # noqa: BLE001
        pass
    u = usage_today()
    if u["runs"] >= lim["maxRunsPerDay"]:
        return f"Already ran {u['runs']} times today (limit {lim['maxRunsPerDay']})."
    if u["tokens"] >= lim["dailyTokenBudget"]:
        return f"Today's token budget is spent ({u['tokens']:,} of {lim['dailyTokenBudget']:,})."
    return None


def tool_refusal(cfg: dict) -> Optional[str]:
    """Why this provider/model cannot run Autopilot at all, or None.

    Blocking — for a per-model provider it is a local call or a free listing —
    so it is NOT part of blocked_reason(). run_once() and the Run-now handler
    ask it off the loop, before anything is recorded or billed."""
    provider = _provider(cfg)
    ok, why = ai_analysis.tool_support(provider, cfg.get("ai_model"))
    if ok:
        return None
    return why or (f"{ai_providers.CAPS[provider].label} can't call tools with this "
                   f"model, and Autopilot works only through tools.")


def _parse(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def next_due_at(cfg: dict) -> Optional[float]:
    if not cfg.get("autopilot_enabled"):
        return None
    last = _parse(last_started_at())
    if last is None:
        return datetime.now(timezone.utc).timestamp()
    return last + _limits(cfg)["intervalMin"] * 60


def due(cfg: dict) -> bool:
    nd = next_due_at(cfg)
    return (nd is not None and datetime.now(timezone.utc).timestamp() >= nd
            and not STATE.running)


def status(cfg: dict) -> dict:
    """kryptrader's AutopilotStatus shape (minus `runs`, which the RPC adds)."""
    nd = next_due_at(cfg)
    provider = _provider(cfg)
    agent = run_agent(cfg)
    return {
        "enabled": bool(cfg.get("autopilot_enabled")),
        "running": STATE.running,
        "startedAt": STATE.started_at,
        "step": STATE.current_step,
        "lastError": STATE.last_error,
        "nextRunAt": (datetime.fromtimestamp(nd, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
                      if nd else None),
        "blockedReason": blocked_reason(cfg) if cfg.get("autopilot_enabled") else None,
        "today": usage_today(),
        "limits": _limits(cfg),
        "provider": provider,
        "providerLabel": ai_providers.CAPS[provider].label,
        "model": _model(cfg),
        # Where the token budget's numbers come from on this provider.
        "tokenAccounting": ai_providers.CAPS[provider].tokens,
        "toolCount": len(mcp_tools.list_tools()),
        "agentId": str(cfg.get("autopilot_agent_id") or mcp_agents.DEFAULT_ID),
        "agentName": (agent or {}).get("name"),
        "mission": str(cfg.get("autopilot_mission") or "").strip() or DEFAULT_MISSION,
        "missionIsDefault": not str(cfg.get("autopilot_mission") or "").strip(),
    }


# ── The run ─────────────────────────────────────────────────────────────────

def _clip(text: str) -> str:
    if len(text) <= _RESULT_CHARS:
        return text
    return text[:_RESULT_CHARS] + f"\n[... {len(text) - _RESULT_CHARS} more characters cut]"


def _cost(model: str, i: int, o: int, cache_write: int = 0,
          cache_read: int = 0) -> Optional[float]:
    # Cache writes bill at 1.25x input and reads at 0.1x on Anthropic.
    eff_in = i + int(cache_write * 1.25) + int(cache_read * 0.1)
    return ai_providers.cost_usd(model, eff_in, o)


def _first_user() -> str:
    return ("Run now. Today is "
            + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + ".")


class _Run:
    def __init__(self, cfg: dict, trigger: str):
        self.cfg = cfg
        self.trigger = trigger
        self.provider = _provider(cfg)
        self.model = _model(cfg)
        self.lim = _limits(cfg)
        self.in_tok = 0
        self.out_tok = 0
        self.cost: Optional[float] = 0.0
        # True once any turn's tokens were estimated rather than reported.
        self.estimated = False
        self.steps = 0
        self.tool_log: list[dict] = []
        self.agent_id = str(cfg.get("autopilot_agent_id") or mcp_agents.DEFAULT_ID)
        self.rid: Optional[int] = None
        # The agent id rides the session into mcp_tools.call, exactly as a
        # token-resolved id does for a connected client: same rules, same
        # records. `model` is what record_forecast stamps on the forecast.
        self.session: dict = {"client": f"autopilot ({self.model})",
                              "agent": self.agent_id, "transport": "autopilot",
                              "model": self.model}
        self.budget_left = self.lim["dailyTokenBudget"] - usage_today()["tokens"]

    def set_model(self, model: str) -> None:
        self.model = model
        self.session["client"] = f"autopilot ({model})"
        self.session["model"] = model

    def system(self) -> str:
        mission = str(self.cfg.get("autopilot_mission") or "").strip() or DEFAULT_MISSION
        tools = mcp_tools.list_tools()
        return SYSTEM.format(
            instructions=mcp_server.instructions_for(self.agent_id).strip(),
            max_steps=self.lim["maxSteps"], tool_count=len(tools),
            tool_names=", ".join(t["name"] for t in tools), mission=mission)

    def tools(self) -> list[dict]:
        return [{"name": t["name"], "description": t["description"],
                 "inputSchema": t["inputSchema"]} for t in mcp_tools.list_tools()]

    def add_usage(self, i: int, o: int, cost: Optional[float],
                  estimated: bool = False) -> None:
        self.in_tok += int(i or 0)
        self.out_tok += int(o or 0)
        self.estimated = self.estimated or estimated
        self.cost = None if (self.cost is None or cost is None) else self.cost + cost
        # Written as it is spent, so a crash mid-run still counts against the
        # day's budget instead of vanishing from it.
        if self.rid is not None:
            try:
                with db.get_db() as conn:
                    conn.execute(
                        "UPDATE autopilot_runs SET input_tokens=?, output_tokens=?, "
                        "tokens_estimated=?, steps=? WHERE id=?",
                        (self.in_tok, self.out_tok, int(self.estimated), self.steps,
                         self.rid))
            except Exception as e:  # noqa: BLE001
                logger.debug("[autopilot] usage write failed: %s", e)

    def over_budget(self) -> bool:
        return self.in_tok + self.out_tok >= self.budget_left

    async def tool(self, name: str, args: Any) -> tuple[str, bool]:
        self.steps += 1
        STATE.current_step = self.steps
        args = args if isinstance(args, dict) else {}
        if not mcp_tools.has_tool(name):
            text, is_err = f"Refused: unknown tool: {name}", True
        else:
            # mcp_tools.call is the one dispatcher: it applies the per-agent
            # rate limit itself, exactly as for an MCP or HTTP client.
            res = await mcp_tools.call(name, args, self.session)
            text = res["content"][0]["text"] if res.get("content") else ""
            is_err = bool(res.get("isError"))
        self.tool_log.append({"tool": name, "ok": not is_err, "brief": text[:160]})
        return _clip(text), is_err

    def should_stop(self) -> Optional[str]:
        live = _live_cfg()
        if live:
            if self.trigger == "schedule" and not live.get("autopilot_enabled"):
                return "stopped"
            agent = mcp_agents.get(live, self.agent_id)
            if agent is None or not agent.get("enabled", True):
                return "stopped"
        if self.over_budget():
            return "budget"
        return None


def _sdk_error(provider: str, e: BaseException, model: str) -> ai_providers.AiError:
    """An SDK exception as the user's sentence, from the same error table the
    HTTP providers use."""
    name = type(e).__name__
    if isinstance(e, ai_analysis._Refused):
        return ai_providers.AiError(e.reason)
    if name in ("APIConnectionError",):
        return ai_providers.error(provider, "down", model)
    if name in ("APITimeoutError",):
        return ai_providers.error(provider, "timeout", model)
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    detail = ai_providers._detail(body, str(getattr(e, "message", "") or e))
    if isinstance(status, int):
        return ai_providers.error(
            provider, ai_providers.classify(provider, status, body, detail),
            model, detail, status)
    return ai_providers.AiError(f"{ai_providers.CAPS[provider].label}: {name}: {detail}")


async def _loop_anthropic(run: _Run, key: str) -> tuple[str, str]:
    import anthropic
    # One client per run, closed however the run ends. max_retries=0: the
    # terminal gate's 429 park is the single retry authority.
    client = anthropic.AsyncAnthropic(api_key=key, timeout=_TIMEOUT_SEC, max_retries=0)
    try:
        return await _anthropic_turns(run, client)
    finally:
        await ai_analysis._close(client)


async def _anthropic_turns(run: _Run, client: Any) -> tuple[str, str]:
    tools = [{"name": t["name"], "description": t["description"],
              "input_schema": t["inputSchema"]} for t in run.tools()]
    # The system prompt and tool list are identical on every turn: cache them,
    # so the growing conversation is the only thing billed in full.
    system = [{"type": "text", "text": run.system(), "cache_control": {"type": "ephemeral"}}]
    messages: list = [{"role": "user", "content": _first_user()}]
    final = ""
    while True:
        stop = run.should_stop()
        if stop:
            return final or "Stopped before finishing.", stop
        try:
            async with ai_analysis._gate_call("anthropic"):
                resp = await client.messages.create(
                    model=run.model, max_tokens=_ANTHROPIC_MAX_TOKENS, system=system,
                    thinking={"type": "adaptive"}, tools=tools, messages=messages)
        except Exception as e:  # noqa: BLE001 — every failure becomes a sentence
            raise _sdk_error("anthropic", e, run.model) from e
        u = getattr(resp, "usage", None)
        cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        i = getattr(u, "input_tokens", None)
        o = getattr(u, "output_tokens", None)
        est = i is None or o is None
        if est:
            # Never zero: a turn with no usage is charged an estimate.
            i = ai_providers.est_tokens({"s": system, "m": messages, "t": tools}) if i is None else i
            o = ai_providers.est_tokens(str(getattr(resp, "content", ""))) if o is None else o
        i, o = int(i), int(o)
        run.add_usage(i + cw + cr, o, None if est else _cost(run.model, i, o, cw, cr), est)
        content = list(getattr(resp, "content", None) or [])
        texts = [b.text for b in content if getattr(b, "type", None) == "text"]
        if texts:
            final = "\n".join(texts).strip()
        calls = [b for b in content if getattr(b, "type", None) == "tool_use"]
        stop_reason = getattr(resp, "stop_reason", None)
        if stop_reason == "refusal":
            return final or "The model declined to continue.", "error"
        if stop_reason == "max_tokens":
            return final or "The model ran out of room before finishing this turn.", "error"
        if stop_reason != "tool_use" or not calls:
            return final, "ok"
        if run.steps + len(calls) > run.lim["maxSteps"]:
            return final or "Reached the tool-call limit for one run.", "steps"
        # Thinking and tool_use blocks go back exactly as received.
        messages.append({"role": "assistant", "content": content})
        results = []
        for c in calls:
            text, is_err = await run.tool(c.name, c.input)
            results.append({"type": "tool_result", "tool_use_id": c.id,
                            "content": text, "is_error": is_err})
        messages.append({"role": "user", "content": results})


async def _loop_openai(run: _Run, key: str) -> tuple[str, str]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=key, timeout=_TIMEOUT_SEC, max_retries=0)
    try:
        return await _openai_turns(run, client)
    finally:
        await ai_analysis._close(client)


async def _openai_turns(run: _Run, client: Any) -> tuple[str, str]:
    tools = [{"type": "function", "name": t["name"], "description": t["description"],
              "parameters": t["inputSchema"], "strict": False} for t in run.tools()]
    items: list = [{"role": "user", "content": _first_user()}]
    system = run.system()
    final = ""
    while True:
        stop = run.should_stop()
        if stop:
            return final or "Stopped before finishing.", stop
        try:
            async with ai_analysis._gate_call("openai"):
                resp = await client.responses.create(
                    model=run.model, instructions=system, input=items, tools=tools,
                    max_output_tokens=_MAX_TOKENS)
        except Exception as e:  # noqa: BLE001
            raise _sdk_error("openai", e, run.model) from e
        u = getattr(resp, "usage", None)
        i = getattr(u, "input_tokens", None)
        o = getattr(u, "output_tokens", None)
        est = i is None or o is None
        if est:
            i = ai_providers.est_tokens({"s": system, "m": items, "t": tools}) if i is None else i
            o = ai_providers.est_tokens(str(getattr(resp, "output", ""))) if o is None else o
        # No published GPT price here, so cost is None — never a guess.
        run.add_usage(int(i), int(o), None, est)
        if getattr(resp, "output_text", None):
            final = resp.output_text.strip()
        output = list(getattr(resp, "output", None) or [])
        calls = [it for it in output if getattr(it, "type", None) == "function_call"]
        if not calls:
            return final, "ok"
        if run.steps + len(calls) > run.lim["maxSteps"]:
            return final or "Reached the tool-call limit for one run.", "steps"
        # Reasoning items must travel back with the calls they produced.
        items += output
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
            except ValueError:
                args = {}
            text, _err = await run.tool(c.name, args)
            items.append({"type": "function_call_output", "call_id": c.call_id,
                          "output": text})


async def _loop_generic(run: _Run, key: str) -> tuple[str, str]:
    """OpenRouter, Gemini, Ollama, LM Studio. Same order of checks as the two
    SDK loops — stop/budget, call, account, refusal, done, step limit, tools —
    so a budget means the same thing whichever provider is spending it."""
    try:
        chat = await asyncio.to_thread(
            ai_providers.open_tool_chat, run.provider, key or None, run.model,
            run.system(), run.tools(), _first_user())
    except ai_providers.AiError:
        raise
    # LM Studio's empty id is "the loaded model"; record the one that ran.
    run.set_model(chat.model)
    final = ""
    while True:
        stop = run.should_stop()
        if stop:
            return final or "Stopped before finishing.", stop
        # Before the call, not after: Ollama and LM Studio truncate an
        # oversized conversation instead of refusing it, and the first thing
        # they cut is the system prompt with the rules in it.
        too_big = chat.context_problem()
        if too_big:
            return (final + "\n\n" if final else "") + too_big, "context"
        turn = await asyncio.to_thread(chat.send)
        run.add_usage(turn.input_tokens, turn.output_tokens, turn.cost, turn.estimated)
        if turn.failed:
            # After add_usage, so the failed generation still counts against
            # the day's budget; raised, so the run is "error" with the
            # provider's sentence instead of "ok" with a blank report.
            raise ai_providers.AiError(turn.failed)
        if turn.text:
            final = turn.text
        if turn.refusal:
            return final or "The model declined to continue.", "error"
        if not turn.calls:
            return final, "ok"
        if run.steps + len(turn.calls) > run.lim["maxSteps"]:
            return final or "Reached the tool-call limit for one run.", "steps"
        results = []
        for c in turn.calls:
            text, is_err = await run.tool(c.name, c.args)
            results.append((c, text, is_err))
        chat.add_results(turn, results)


async def run_once(cfg: dict, trigger: str = "schedule") -> dict:
    """One Autopilot session. Returns {ok, message, run?}."""
    if _LOCK.locked() or STATE.running:
        return {"ok": False, "message": "Autopilot is already running."}
    why = blocked_reason(cfg)
    if not why:
        why = await asyncio.to_thread(tool_refusal, cfg)
    if why:
        STATE.last_error = why
        return {"ok": False, "message": why}
    async with _LOCK:
        STATE.running = True
        STATE.started_at = _now()
        STATE.current_step = 0
        run = _Run(cfg, trigger)
        with db.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO autopilot_runs (started_at, trigger, agent_id, provider, "
                "model, status) VALUES (?,?,?,?,?, 'running')",
                (STATE.started_at, trigger, run.agent_id, run.provider, run.model))
            run.rid = int(cur.lastrowid)
        rid = run.rid
        summary, status_, error = "", "error", None
        try:
            key = ai_analysis.read_key(run.provider) or ""
            loop = {"openai": _loop_openai,
                    "anthropic": _loop_anthropic}.get(run.provider, _loop_generic)
            summary, status_ = await loop(run, key)
            STATE.last_error = None
        except ai_providers.AiError as e:
            error = str(e)
            STATE.last_error = error
        except Exception as e:  # noqa: BLE001
            logger.warning("[autopilot] run %s failed: %s: %s", rid, type(e).__name__, e)
            error = f"{type(e).__name__}: {e}"
            STATE.last_error = error
        finally:
            if run.estimated:
                summary = ((summary or "").rstrip() + "\n\n[Token counts for this run "
                           "are estimates from text length: "
                           f"{ai_providers.CAPS[run.provider].label} did not report "
                           "usage for every turn. The daily budget counted the "
                           "estimates.]").strip()
            with db.get_db() as conn:
                conn.execute(
                    "UPDATE autopilot_runs SET finished_at=?, model=?, status=?, steps=?, "
                    "input_tokens=?, output_tokens=?, tokens_estimated=?, cost_usd=?, "
                    "summary=?, tool_log=?, error=? WHERE id=?",
                    (_now(), run.model, status_ if error is None else "error", run.steps,
                     run.in_tok, run.out_tok, int(run.estimated),
                     round(run.cost, 6) if run.cost is not None else None,
                     (summary or "")[:6000], json.dumps(run.tool_log[:80]), error, rid))
            STATE.running = False
            STATE.current_step = 0
        logger.info("[autopilot] run %s %s: %d steps, %d tokens",
                    rid, status_ if error is None else "error", run.steps,
                    run.in_tok + run.out_tok)
        try:
            import service
            head = (error or summary or "").strip().splitlines()
            await service.emit_event("mcp:order", {
                "mode": "action",
                "message": f"Autopilot run {'failed' if error else 'finished'}: "
                           + (head[0][:160] if head else f"{run.steps} steps")})
        except Exception:  # noqa: BLE001
            pass
        latest = runs(1)
        return {"ok": error is None, "message": error or "Run finished.",
                "run": latest[0] if latest else None}
