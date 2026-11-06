"""Autopilot: one of the user's agents on a timer, billed to the user's key.

Providers are faked (the SDKs by stand-in modules, the HTTP providers by an
httpx.MockTransport — no network). What is pinned: every tool goes through
mcp_tools.call as the named agent (so every rail applies), it stops at each of
its three budgets, missing usage is estimated and never zero, a model that
cannot call tools is refused before anything is recorded, a deleted or
switched-off agent blocks it, it stops when switched off, it never overlaps
itself, and every run is recorded with what it cost.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace as NS

import httpx
import pytest

import ai_analysis
import ai_providers as ap
import ai_secrets
import autopilot
import db
import mcp_server
import mcp_tools
import terminal
from config import merge_with_defaults
from test_mcp import _fake_service

OR_KEY = "sk-" + "or-v1-" + "0f" * 32
GEMINI_KEY = "AI" + "za" + "SyN0tAR3alG00gleK3yXXXXXXXXXXXXXXXX"
LMS_MODEL = "qwen2.5-7b-instruct"
CHAT = {
    "openrouter": ("POST", "/api/v1/chat/completions"),
    "lmstudio": ("POST", "/v1/chat/completions"),
    "ollama": ("POST", "/api/chat"),
    "gemini": ("POST", "/v1beta/models/gemini-3.8-flash:generateContent"),
}
HTTP_PROVIDERS = ("openrouter", "gemini", "ollama", "lmstudio")


def run(coro):
    return asyncio.run(coro)


# ── fakes ───────────────────────────────────────────────────────────────

def _usage(i=1000, o=200):
    return NS(input_tokens=i, output_tokens=o, cache_creation_input_tokens=0,
              cache_read_input_tokens=0)


def _tool_use(name, inp, uid="t1"):
    return NS(type="tool_use", name=name, input=inp, id=uid)


def _text(t):
    return NS(type="text", text=t)


class FakeAnthropic:
    """Replays a script of responses; records every request."""
    script: list = []
    calls: list = []
    hook = None

    def __init__(self, **kw):
        self.messages = self

    async def create(self, **kw):
        FakeAnthropic.calls.append(kw)
        if FakeAnthropic.hook:
            FakeAnthropic.hook()
        if not FakeAnthropic.script:
            return NS(content=[_text("done")], stop_reason="end_turn", usage=_usage(),
                      model="claude-opus-5")
        return FakeAnthropic.script.pop(0)

    async def close(self):
        pass


class Router:
    def __init__(self):
        self.routes: dict = {}
        self.requests: list = []

    def on(self, method, path, handler):
        self.routes[(method, path)] = handler

    def __call__(self, request):
        self.requests.append(request)
        h = self.routes.get((request.method, request.url.path))
        if h is None:
            return httpx.Response(404, json={"error": f"no route {request.url.path}"})
        if isinstance(h, list):
            h = h.pop(0) if len(h) > 1 else h[0]
        return h(request)

    def bodies(self, method, path):
        return [json.loads(r.content or b"{}") for r in self.requests
                if r.method == method and r.url.path == path]


def J(status, body):
    return lambda req: httpx.Response(status, json=body)


def _down(request):
    raise httpx.ConnectError("connection refused", request=request)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "autopilot.db")
    db.init_db()
    svc, events = _fake_service(
        monkeypatch, autopilot_enabled=True, mcp_trade_mode="off",
        ai_provider="anthropic", ai_model="claude-opus-5")
    mcp_server._hits.clear()
    mcp_server._agent_hits.clear()
    keys = {"anthropic": "sk-test", "openai": "sk-test", "openrouter": OR_KEY,
            "gemini": GEMINI_KEY}
    monkeypatch.setattr(ai_analysis, "read_key", lambda p: keys.get(p))
    monkeypatch.setattr(ai_secrets, "has_key", lambda p: p in keys)
    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = FakeAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    FakeAnthropic.script, FakeAnthropic.calls, FakeAnthropic.hook = [], [], None
    for g in ("anthropic", "openai", *HTTP_PROVIDERS):
        gate = terminal._gates[g]
        monkeypatch.setattr(gate, "min_gap", 0.0)
        gate._lock = None
        gate.parked_until = 0.0
    # The HTTP providers: a mocked transport, nothing on the network.
    r = Router()
    monkeypatch.setattr(ap, "_TRANSPORT", httpx.MockTransport(r))
    ap._LISTED.clear()
    r.on("GET", "/api/v0/models", J(200, {"data": [
        {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"},
        {"id": LMS_MODEL, "type": "llm", "state": "loaded",
         "loaded_context_length": 32768, "max_context_length": 32768}]}))
    autopilot.STATE.running = False
    autopilot.STATE.last_error = None
    yield NS(svc=svc, events=events, net=r)
    ap._LISTED.clear()
    for g in ("anthropic", "openai"):
        terminal._gates[g]._lock = None


def _cfg(env, **over):
    env.svc.STATE.cfg.update(over)
    return merge_with_defaults(dict(env.svc.STATE.cfg))


# ── the SDK path (Anthropic) ────────────────────────────────────────────

def test_a_run_calls_tools_through_mcp_tools_as_the_agent_and_is_recorded(env):
    FakeAnthropic.script = [
        NS(content=[_tool_use("get_status", {})], stop_reason="tool_use", usage=_usage()),
        NS(content=[_text("Looked at nothing worth trading.")], stop_reason="end_turn",
           usage=_usage(500, 100)),
    ]
    res = run(autopilot.run_once(_cfg(env), "manual"))
    assert res["ok"], res
    r = autopilot.runs(1)[0]
    assert r["status"] == "ok" and r["steps"] == 1 and r["agentId"] == "default"
    assert r["inputTokens"] == 1500 and r["outputTokens"] == 300
    assert r["costUsd"] is not None and r["costUsd"] > 0     # Opus has a published price
    assert r["tools"][0]["tool"] == "get_status" and r["tools"][0]["ok"]
    assert "worth trading" in r["summary"]
    # get_status answered AS the agent the run is for.
    assert '"agentId": "default"' in FakeAnthropic.calls[1]["messages"][-1]["content"][0]["content"]
    # Offered exactly the tools an MCP client sees.
    offered = {t["name"] for t in FakeAnthropic.calls[0]["tools"]}
    assert offered == {t["name"] for t in mcp_tools.list_tools()}
    assert len(offered) == len(mcp_tools.list_tools())


def test_the_mission_prompt_carries_polybots_agent_loop(env):
    run(autopilot.run_once(_cfg(env, autopilot_mission="Only NBA tonight."), "manual"))
    system = FakeAnthropic.calls[0]["system"][0]["text"]
    assert "record_forecast" in system and "forecastId" in system
    assert "Paper" in system and "USER sets" in system
    assert "Only NBA tonight." in system
    for name in ("get_status", "record_forecast", "place_order", "get_my_agent"):
        assert name in system
    assert f"YOUR TOOLS ({len(mcp_tools.list_tools())})" in system


def test_a_customised_agent_brings_its_guide_and_rules(env):
    cfg = _cfg(env, autopilot_agent_id="sam", mcp_agents=[
        {"id": "default", "name": "Default"},
        {"id": "sam", "name": "Sports Sam", "guide": "Only NBA favourites.",
         "rules": {"categoriesAllow": ["sports"]}}])
    run(autopilot.run_once(cfg, "manual"))
    system = FakeAnthropic.calls[0]["system"][0]["text"]
    assert "Sports Sam" in system and "Only NBA favourites." in system
    assert autopilot.runs(1)[0]["agentId"] == "sam"


def test_rails_apply_a_trade_is_refused_when_trading_is_off(env):
    FakeAnthropic.script = [
        NS(content=[_tool_use("place_order", {
            "conditionId": "0x" + "ab" * 32, "side": "yes", "action": "buy",
            "size": 5, "priceCents": 40})], stop_reason="tool_use", usage=_usage()),
    ]
    run(autopilot.run_once(_cfg(env), "manual"))
    r = autopilot.runs(1)[0]
    assert r["tools"][0]["tool"] == "place_order" and r["tools"][0]["ok"] is False
    assert "Refused" in r["tools"][0]["brief"]
    tool_result = FakeAnthropic.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True


def test_an_unknown_tool_is_an_error_not_a_crash(env):
    FakeAnthropic.script = [
        NS(content=[_tool_use("move_funds", {"to": "x"})], stop_reason="tool_use",
           usage=_usage()),
    ]
    res = run(autopilot.run_once(_cfg(env), "manual"))
    assert res["ok"]
    t = autopilot.runs(1)[0]["tools"][0]
    assert t == {"tool": "move_funds", "ok": False, "brief": "Refused: unknown tool: move_funds"}


def test_the_step_limit_ends_a_run(env):
    FakeAnthropic.script = [
        NS(content=[_tool_use("get_status", {}, f"t{i}")], stop_reason="tool_use",
           usage=_usage()) for i in range(10)]
    run(autopilot.run_once(_cfg(env, autopilot_max_steps=3), "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "steps" and r["steps"] == 3


def test_the_daily_token_budget_stops_a_run_mid_way(env):
    FakeAnthropic.script = [
        NS(content=[_tool_use("get_status", {}, f"t{i}")], stop_reason="tool_use",
           usage=_usage(30_000, 1_000)) for i in range(10)]
    run(autopilot.run_once(_cfg(env, autopilot_daily_token_budget=50_000), "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "budget"
    assert len(FakeAnthropic.calls) == 2
    # ...and the next run is refused before it costs anything.
    res = run(autopilot.run_once(_cfg(env), "manual"))
    assert not res["ok"] and "token budget" in res["message"]
    assert len(FakeAnthropic.calls) == 2


def test_missing_usage_is_estimated_never_zero(env):
    FakeAnthropic.script = [
        NS(content=[_tool_use("get_status", {})], stop_reason="tool_use",
           usage=NS(input_tokens=None, output_tokens=None)),
        NS(content=[_text("Done.")], stop_reason="end_turn", usage=None),
    ]
    run(autopilot.run_once(_cfg(env), "manual"))
    r = autopilot.runs(1)[0]
    assert r["inputTokens"] > 1000 and r["outputTokens"] > 0
    assert r["tokensEstimated"] is True and r["costUsd"] is None
    assert "estimates from text length" in r["summary"]


def test_runs_per_day_are_capped(env):
    assert run(autopilot.run_once(_cfg(env, autopilot_max_runs_per_day=1), "manual"))["ok"]
    res = run(autopilot.run_once(_cfg(env), "manual"))
    assert not res["ok"] and "limit 1" in res["message"]


def test_switching_it_off_stops_a_scheduled_run(env):
    def _off():
        env.svc.STATE.cfg["autopilot_enabled"] = False
    FakeAnthropic.hook = _off
    FakeAnthropic.script = [NS(content=[_tool_use("get_status", {})], stop_reason="tool_use",
                               usage=_usage()) for _ in range(3)]
    run(autopilot.run_once(_cfg(env), "schedule"))
    assert autopilot.runs(1)[0]["status"] == "stopped"
    assert len(FakeAnthropic.calls) == 1


def test_deleting_the_agent_mid_run_stops_it(env):
    cfg = _cfg(env, autopilot_agent_id="sam", mcp_agents=[
        {"id": "default", "name": "Default"}, {"id": "sam", "name": "Sam"}])

    def _delete():
        env.svc.STATE.cfg["mcp_agents"] = [{"id": "default", "name": "Default"}]
    FakeAnthropic.hook = _delete
    FakeAnthropic.script = [NS(content=[_tool_use("get_status", {})], stop_reason="tool_use",
                               usage=_usage()) for _ in range(3)]
    run(autopilot.run_once(cfg, "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "stopped" and len(FakeAnthropic.calls) == 1
    # The one tool call it did make was refused as a deleted agent, never run
    # as Default.
    assert r["tools"][0]["ok"] is False and "no longer exists" in r["tools"][0]["brief"]


def test_no_key_means_no_run(env, monkeypatch):
    monkeypatch.setattr(ai_secrets, "has_key", lambda p: False)
    res = run(autopilot.run_once(_cfg(env), "manual"))
    assert not res["ok"] and "key" in res["message"]
    assert FakeAnthropic.calls == [] and autopilot.runs(1) == []


def test_offline_blocks_a_cloud_provider_before_anything_is_recorded(env):
    terminal.set_offline(True)
    try:
        why = autopilot.blocked_reason(_cfg(env))
        assert why and "Offline mode" in why
        assert autopilot.blocked_reason(_cfg(env, ai_provider="ollama", ai_model="")) is None
    finally:
        terminal.set_offline(False)


def test_runs_never_overlap(env):
    async def _both():
        gate = asyncio.Event()
        orig = autopilot._loop_anthropic

        async def _slow(r, key):
            await gate.wait()
            return "ok", "ok"
        autopilot._loop_anthropic = _slow
        try:
            first = asyncio.create_task(autopilot.run_once(_cfg(env), "manual"))
            await asyncio.sleep(0.05)
            second = await autopilot.run_once(_cfg(env), "manual")
            gate.set()
            await first
            return second
        finally:
            autopilot._loop_anthropic = orig
    second = run(_both())
    assert not second["ok"] and "already running" in second["message"]


def test_schedule_respects_the_interval(env):
    cfg = _cfg(env)
    assert autopilot.due(cfg)                       # never ran
    run(autopilot.run_once(cfg, "schedule"))
    assert not autopilot.due(cfg)                   # just ran
    assert autopilot.next_due_at(_cfg(env, autopilot_enabled=False)) is None


def test_openai_runs_are_recorded_without_inventing_a_price(env, monkeypatch):
    replies = [
        NS(output=[NS(type="function_call", name="get_status", arguments="{}",
                      call_id="c1")], output_text="", usage=NS(input_tokens=800,
                                                                output_tokens=50)),
        NS(output=[NS(type="message")], output_text="Nothing to do.",
           usage=NS(input_tokens=900, output_tokens=60)),
    ]

    class FakeOpenAI:
        def __init__(self, **kw):
            self.responses = self

        async def create(self, **kw):
            return replies.pop(0)

        async def close(self):
            pass
    fake = types.ModuleType("openai")
    fake.AsyncOpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake)
    assert run(autopilot.run_once(_cfg(env, ai_provider="openai", ai_model="gpt-5.5"),
                                  "manual"))["ok"]
    r = autopilot.runs(1)[0]
    assert r["status"] == "ok" and r["steps"] == 1 and r["provider"] == "openai"
    assert r["costUsd"] is None                     # no published price → no number
    assert autopilot.usage_today()["costUsd"] is None


def test_openai_with_no_model_is_refused_up_front(env):
    res = run(autopilot.run_once(_cfg(env, ai_provider="openai", ai_model=""), "manual"))
    assert not res["ok"] and "No OpenAI model" in res["message"]
    assert autopilot.runs(1) == []


def test_budget_settings_are_clamped():
    cfg = merge_with_defaults({"autopilot_interval_min": 1,
                               "autopilot_daily_token_budget": 10 ** 12,
                               "autopilot_max_steps": 999,
                               "autopilot_mission": "x" * 5000,
                               "autopilot_enabled": "yes"})
    assert cfg["autopilot_interval_min"] == 15
    assert cfg["autopilot_daily_token_budget"] == 50_000_000
    assert cfg["autopilot_max_steps"] == 40
    assert len(cfg["autopilot_mission"]) == 2000
    assert cfg["autopilot_enabled"] is False


def test_an_agent_cannot_touch_autopilot_settings():
    assert mcp_tools.classify("autopilot_enabled") == "protected"
    assert mcp_tools.classify("autopilot_mission") == "protected"


def test_deleting_the_agent_autopilot_runs_as_blocks_it_never_falls_back(env):
    cfg = _cfg(env, autopilot_agent_id="gone1",
               mcp_agents=[{"id": "default", "name": "Default"}])
    assert cfg["autopilot_agent_id"] == "gone1"           # not repaired to Default
    why = autopilot.blocked_reason(cfg)
    assert why and "no longer exists" in why
    assert autopilot.status(cfg)["blockedReason"] == why
    res = run(autopilot.run_once(cfg, "manual"))
    assert res["ok"] is False and "no longer exists" in res["message"]
    assert autopilot.runs(5) == [] and FakeAnthropic.calls == []
    cfg = _cfg(env, autopilot_agent_id="sam1", mcp_agents=[
        {"id": "default", "name": "Default"}, {"id": "sam1", "name": "Sam", "enabled": False}])
    assert "switched off" in autopilot.blocked_reason(cfg)


def test_status_has_the_autopilot_status_shape(env):
    st = autopilot.status(_cfg(env, ai_provider="ollama", ai_model=""))
    for k in ("enabled", "running", "startedAt", "step", "lastError", "nextRunAt",
              "blockedReason", "today", "limits", "provider", "providerLabel", "model",
              "tokenAccounting", "toolCount", "agentId", "agentName"):
        assert k in st, k
    assert st["providerLabel"] == "Ollama" and "this machine" in st["tokenAccounting"]
    assert st["toolCount"] == len(mcp_tools.list_tools())
    assert set(st["today"]) == {"runs", "tokens", "costUsd"}


# ── the HTTP providers (ToolChat) ───────────────────────────────────────

def _reply(provider, text, usage=True):
    if provider in ("openrouter", "lmstudio"):
        out = {"choices": [{"message": {"role": "assistant", "content": text},
                            "finish_reason": "stop"}]}
        if usage:
            out["usage"] = {"prompt_tokens": 1200, "completion_tokens": 300}
            if provider == "openrouter":
                out["usage"]["cost"] = 0.002
        return out
    if provider == "ollama":
        out = {"message": {"role": "assistant", "content": text}, "done": True}
        if usage:
            out.update(prompt_eval_count=1200, eval_count=300)
        return out
    out = {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                           "finishReason": "STOP"}]}
    if usage:
        out["usageMetadata"] = {"promptTokenCount": 1200, "candidatesTokenCount": 250,
                                "thoughtsTokenCount": 50}
    return out


def _tool_turn(provider, name="get_status", usage=True, tokens=1000):
    if provider in ("openrouter", "lmstudio"):
        out = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": name, "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]}
        if usage:
            out["usage"] = {"prompt_tokens": tokens, "completion_tokens": 50, "cost": 0.001}
        return out
    if provider == "ollama":
        out = {"message": {"role": "assistant", "content": "",
                           "tool_calls": [{"function": {"name": name, "arguments": {}}}]},
               "done": True}
        if usage:
            out.update(prompt_eval_count=tokens, eval_count=50)
        return out
    return {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": name, "args": {}}, "thoughtSignature": "sig-abc"}]},
        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": tokens, "candidatesTokenCount": 50}}


def _tools_ok(net):
    net.on("POST", "/api/show", J(200, {"capabilities": ["completion", "tools"]}))
    net.on("GET", "/api/v1/models", J(200, {"data": [
        {"id": "google/gemini-2.5-flash", "supported_parameters": ["tools"],
         "context_length": 1048576},
        {"id": "some/no-tools-model", "supported_parameters": ["max_tokens"]}]}))


def _pcfg(env, provider, model=""):
    return _cfg(env, ai_provider=provider, ai_model=model)


@pytest.mark.parametrize("provider", HTTP_PROVIDERS)
def test_autopilot_runs_through_mcp_tools_on_every_provider(env, provider):
    _tools_ok(env.net)
    env.net.on(*CHAT[provider], [J(200, _tool_turn(provider)),
                                 J(200, _reply(provider, "Nothing worth trading."))])
    res = run(autopilot.run_once(_pcfg(env, provider), "manual"))
    assert res["ok"], res
    r = autopilot.runs(1)[0]
    assert r["provider"] == provider and r["status"] == "ok" and r["steps"] == 1
    assert r["tools"][0]["tool"] == "get_status" and r["tools"][0]["ok"]
    assert r["inputTokens"] == 2200 and r["outputTokens"] == 350
    if provider in ("ollama", "lmstudio"):
        assert r["costUsd"] == 0.0
    elif provider == "openrouter":
        assert r["costUsd"] == pytest.approx(0.003)
    else:
        assert r["costUsd"] is None
    bodies = env.net.bodies(*CHAT[provider])
    first, second = bodies[0], bodies[1]
    if provider == "gemini":
        names = {d["name"] for d in first["tools"][0]["functionDeclarations"]}
        assert second["contents"][1]["parts"][0]["thoughtSignature"] == "sig-abc"
        fr = second["contents"][2]["parts"][0]["functionResponse"]
        assert fr["name"] == "get_status" and "result" in fr["response"]
    else:
        names = {t["function"]["name"] for t in first["tools"]}
        assert second["messages"][-1]["role"] == "tool"
    assert names == {t["name"] for t in mcp_tools.list_tools()}
    if provider == "lmstudio":
        assert r["model"] == LMS_MODEL                # the loaded model, named


@pytest.mark.parametrize("provider,setup,needle", [
    ("ollama", lambda n: n.on("POST", "/api/show", J(200, {"capabilities": ["completion"]})),
     "can't call tools"),
    ("ollama", lambda n: n.on("POST", "/api/show", J(200, {"modelfile": "..."})),
     "Update Ollama"),
    ("ollama", lambda n: n.on("POST", "/api/show", J(404, {"error": "model 'llama3.1:8b' not found"})),
     "ollama pull"),
    ("ollama", lambda n: n.on("POST", "/api/show", _down), "ollama serve"),
    ("lmstudio", lambda n: n.on("GET", "/api/v0/models", J(200, {"data": [
        {"id": "nomic-embed", "type": "embeddings"}]})), "no chat model"),
])
def test_a_model_without_tools_is_refused_before_anything_is_recorded(
        env, provider, setup, needle):
    setup(env.net)
    res = run(autopilot.run_once(_pcfg(env, provider), "manual"))
    assert not res["ok"] and needle in res["message"], res["message"]
    assert autopilot.runs(1) == []                    # nothing recorded
    assert env.net.bodies(*CHAT[provider]) == []      # nothing sent to the model
    assert autopilot.STATE.last_error == res["message"]


def test_an_openrouter_model_without_tools_is_refused(env):
    _tools_ok(env.net)
    res = run(autopilot.run_once(_pcfg(env, "openrouter", "some/no-tools-model"), "manual"))
    assert not res["ok"] and "cannot call tools" in res["message"]
    assert autopilot.runs(1) == []


def test_a_gemma_model_on_gemini_is_refused_without_a_request(env):
    res = run(autopilot.run_once(_pcfg(env, "gemini", "gemma-3-27b-it"), "manual"))
    assert not res["ok"] and "cannot call tools" in res["message"]
    assert env.net.requests == []


def test_a_cloud_http_provider_without_a_key_is_blocked(env, monkeypatch):
    monkeypatch.setattr(ai_secrets, "has_key", lambda p: False)
    assert "No OpenRouter key" in autopilot.blocked_reason(_pcfg(env, "openrouter"))
    assert autopilot.blocked_reason(_pcfg(env, "ollama")) is None   # needs none


@pytest.mark.parametrize("provider", ("ollama", "openrouter", "gemini"))
def test_the_daily_token_budget_binds_on_every_provider(env, provider):
    _tools_ok(env.net)
    env.net.on(*CHAT[provider], [J(200, _tool_turn(provider, tokens=30_000))])  # forever
    run(autopilot.run_once(_cfg(env, ai_provider=provider, ai_model="",
                                autopilot_daily_token_budget=50_000), "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "budget"
    assert len(env.net.bodies(*CHAT[provider])) == 2   # 2 x 30,050 >= 50,000: stop
    res = run(autopilot.run_once(_pcfg(env, provider), "manual"))
    assert not res["ok"] and "token budget" in res["message"]


def test_the_step_limit_binds_on_a_local_provider(env):
    env.net.on(*CHAT["lmstudio"], [J(200, _tool_turn("lmstudio"))])
    run(autopilot.run_once(_cfg(env, ai_provider="lmstudio", ai_model="",
                                autopilot_max_steps=3), "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "steps" and r["steps"] == 3


def test_missing_usage_on_a_local_server_is_estimated_counted_and_said(env):
    _tools_ok(env.net)
    env.net.on(*CHAT["ollama"], [J(200, _tool_turn("ollama", usage=False)),
                                 J(200, _reply("ollama", "Done.", usage=False))])
    run(autopilot.run_once(_pcfg(env, "ollama"), "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "ok" and r["tokensEstimated"] is True
    assert r["inputTokens"] > 1000 and r["outputTokens"] > 0
    assert "estimates from text length" in r["summary"]


def test_an_overflowing_local_conversation_stops_instead_of_being_truncated(env, monkeypatch):
    _tools_ok(env.net)
    cfg = _pcfg(env, "ollama")
    r_ = autopilot._Run(cfg, "manual")
    opening = ap._OllamaToolChat(None, "llama3.1:8b", r_.system(), r_.tools(),
                                 "Run now. Today is 2026-10-06 12:00 UTC.").request_size()
    monkeypatch.setattr(ap, "_OLLAMA_AGENT_CTX", opening + ap._MAX_TOKENS + 40)
    env.net.on(*CHAT["ollama"], [J(200, _tool_turn("ollama"))])
    run(autopilot.run_once(cfg, "manual"))
    r = autopilot.runs(1)[0]
    assert r["status"] == "context" and r["steps"] == 1
    assert "outgrew" in r["summary"]
    assert len(env.net.bodies(*CHAT["ollama"])) == 1


def test_a_provider_error_mid_run_is_recorded_as_the_users_sentence(env):
    _tools_ok(env.net)
    env.net.on(*CHAT["ollama"], [J(200, _tool_turn("ollama")), _down])
    res = run(autopilot.run_once(_pcfg(env, "ollama"), "manual"))
    assert not res["ok"] and "`ollama serve`" in res["message"]
    assert autopilot.runs(1)[0]["status"] == "error"


def test_an_openrouter_choice_error_ends_the_run_as_an_error_and_still_counts(env):
    _tools_ok(env.net)
    env.net.on(*CHAT["openrouter"], [J(200, _tool_turn("openrouter")), J(200, {
        "choices": [{"finish_reason": "error",
                     "error": {"code": 502, "message": "Upstream provider went away"},
                     "message": {"role": "assistant", "content": ""}}],
        "usage": {"prompt_tokens": 700, "completion_tokens": 5}})])
    res = run(autopilot.run_once(_pcfg(env, "openrouter"), "manual"))
    assert not res["ok"] and "Upstream provider went away" in res["message"]
    r = autopilot.runs(1)[0]
    assert r["status"] == "error" and r["inputTokens"] == 1000 + 700


# ── Gemini tool schemas: every polybot tool converts ────────────────────

def _schema_problems(node, path="", root=True) -> list:
    out = []
    if not isinstance(node, dict):
        return out
    for k in node:
        if k not in ap._GEMINI_SCHEMA_KEYS:
            out.append(f"{path}: key {k}")
    if node.get("type") == "object" and not node.get("properties") and not root:
        out.append(f"{path}: OBJECT with no properties")
    for pk, pv in (node.get("properties") or {}).items():
        out += _schema_problems(pv, f"{path}.{pk}", False)
    if "items" in node:
        out += _schema_problems(node["items"], f"{path}[]", False)
    for i, br in enumerate(node.get("anyOf") or []):
        out += _schema_problems(br, f"{path}|{i}", False)
    return out


def test_every_mcp_tool_schema_is_one_gemini_accepts():
    """A nested open-ended object (update_config's `changes`) is a 400 for the
    WHOLE request on Gemini. Every tool must convert cleanly."""
    converted = {}
    for t in mcp_tools.list_tools():
        paths: list = []
        schema = ap.gemini_schema(t["inputSchema"], (), paths)
        assert _schema_problems(schema) == [], (t["name"], _schema_problems(schema))
        if paths:
            converted[t["name"]] = paths
    assert converted.get("update_config") == [("changes",)]


def test_gemini_decodes_a_string_patch_back_into_the_object_the_tool_expects(env):
    tool = next(t for t in mcp_tools.list_tools() if t["name"] == "update_config")
    chat = ap.open_tool_chat("gemini", GEMINI_KEY, "gemini-3.8-flash", "sys", [tool], "go")
    decl = chat.fn_tools[0]["functionDeclarations"][0]
    assert decl["parameters"]["properties"]["changes"]["type"] == "string"
    raw_args = {"changes": '{"scan_interval_sec": 30}'}
    env.net.on(*CHAT["gemini"], J(200, {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "update_config", "args": raw_args},
         "thoughtSignature": "sig"}]}, "finishReason": "STOP"}]}))
    turn = chat.send()
    assert turn.calls[0].args == {"changes": {"scan_interval_sec": 30}}
    assert turn.raw["parts"][0]["functionCall"]["args"]["changes"] == '{"scan_interval_sec": 30}'
