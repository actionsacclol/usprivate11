"""AI market analysis — the rules that make the panel safe to read.

The interesting tests here are not "does it call the API". They are the two
guarantees the feature rests on:

  1. The model is never given anywhere to put a number. Enforced by the
     response schema, so it is the provider's own structured-output layer
     that refuses, not a hope expressed in a prompt.
  2. Nothing about the user's account can reach a provider. Enforced by
     build_payload taking a market, so there is no argument to it through
     which a position could arrive.

The scrubber tests deliberately weigh both directions. A scrubber that eats
"CPI came in at 2.4%" would trade the panel's usefulness for a purity nobody
asked for, so the survival cases matter as much as the removal cases.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import types

import pytest

import ai_analysis as ai
import config
import terminal


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _fresh(host: str):
    """The gate's lock is created lazily and then bound to whichever loop
    first used it, so a test that reaches the lock has to start from an
    unbound one — every _run() here is a brand new loop."""
    gate = terminal._gates[host]
    gate._lock = None
    return gate


# ── 1. the structural no-number guarantee ───────────────────────────────

def test_schema_has_no_numeric_field():
    """The whole design. If a numeric property ever appears here, the panel
    can print a language model's fair-value estimate as though it were a
    quote, which is the exact failure cross_venue exists to prevent."""
    for name, spec in ai._SCHEMA["properties"].items():
        assert spec.get("type") != "number", f"{name} is numeric"
        assert spec.get("type") != "integer", f"{name} is numeric"
        if spec.get("type") == "array":
            assert spec.get("items", {}).get("type") == "string", name


def test_schema_is_closed():
    """additionalProperties:false is what makes the guarantee structural: the
    provider rejects an invented `probability` field before we see it."""
    assert ai._SCHEMA["additionalProperties"] is False
    assert set(ai._SCHEMA["required"]) == set(ai._SCHEMA["properties"])


def test_lean_is_a_closed_vocabulary():
    assert ai._SCHEMA["properties"]["lean"]["enum"] == ["cheap", "rich", "fair", "no_view"]


def test_reasoning_is_generated_before_the_conclusion():
    """Providers generate in schema order. Every field that is an ARGUMENT
    must come before `lean`, so the model reasons and then concludes rather
    than concluding and then rationalising."""
    order = list(ai._SCHEMA["properties"])
    assert order.index("reasoning") < order.index("lean")
    assert order.index("unknowns") < order.index("lean")


def test_system_prompt_forbids_a_numeric_estimate():
    assert "NEVER state a numeric probability" in ai._SYSTEM


# ── 2. the scrubber: figures the model made up ──────────────────────────

@pytest.mark.parametrize("text", [
    "I'd put it at around 70%.",
    "My estimate is 68c on this one.",
    "Fair value is closer to 55 cents.",
    "The probability is about 30%.",
    "There is roughly a 65% chance of that.",
    "It should be trading at 40c, not 25c.",
    "I would say 80% likely.",
])
def test_scrubber_removes_model_estimates(text):
    cleaned, removed = ai._strip_numeric_estimates(text)
    assert removed == 1
    assert cleaned == ""


@pytest.mark.parametrize("text", [
    "CPI came in at 2.4% in August.",
    "This resolves YES only above $110,000.",
    "The Fed meets on September 17.",
    "Three of the last four cuts were 25bps.",
    "The poll had him at 47 points with a 3 point margin of error.",
    "Turnout was 62% in the 2024 cycle.",
])
def test_scrubber_keeps_facts_about_the_world(text):
    """The direction that matters most. These are the market's numbers, not
    the model's guess, and a scrubber that eats them makes the panel useless."""
    cleaned, removed = ai._strip_numeric_estimates(text)
    assert removed == 0
    assert cleaned == text


def test_scrubber_is_sentence_scoped():
    """One bad clause must not cost the user a paragraph of good reading."""
    text = "The Fed meets on September 17. I'd put it at 70%. CPI was 2.4%."
    cleaned, removed = ai._strip_numeric_estimates(text)
    assert removed == 1
    assert "September 17" in cleaned
    assert "2.4%" in cleaned
    assert "70%" not in cleaned


def test_clean_list_counts_every_removal():
    items = ["My estimate is 60c.", "The FOMC meets Sept 17.", "Fair value is 40."]
    out, removed = ai._clean_list(items)
    assert removed == 2
    assert out == ["The FOMC meets Sept 17."]


# ── 3. nothing about the user leaves ────────────────────────────────────

_ACCOUNT_LEAKS = (
    "position", "wallet", "address", "balance", "cost", "pnl",
    "size", "contracts", "order", "fill", "key", "private",
)


def test_payload_carries_no_account_data():
    market = {
        "question": "Will the Fed cut in September?",
        "probability": 61.0,
        "_raw": {"description": "Resolves YES if..."},
        # Everything below is deliberately present on the input and must not
        # survive into what is sent.
        "myPosition": {"contracts": 500, "costBasisCents": 44.0},
        "walletAddress": "0xdeadbeef",
        "balanceUsd": 1234.56,
    }
    payload = ai.build_payload(market)
    blob = json.dumps(payload).lower()
    for token in _ACCOUNT_LEAKS:
        assert token not in blob, f"payload leaked {token!r}"
    assert payload["question"] == "Will the Fed cut in September?"


def test_payload_reads_rules_from_the_raw_gamma_row():
    """The rules text lives on _raw.description, the same place
    resolution_report reads it. Getting this wrong sends the model a market
    with no rules and it dutifully analyses the title alone."""
    payload = ai.build_payload({
        "question": "Q?", "_raw": {"description": "The real rules."},
    })
    assert payload["rulesText"] == "The real rules."


def test_payload_states_truncation_rather_than_hiding_it():
    payload = ai.build_payload({"question": "Q?", "_raw": {"description": "x" * 9000}})
    assert "TRUNCATED" in payload["rulesText"]


def test_prompt_names_missing_fields_instead_of_blanking_them():
    """A model shown a blank invents one; shown '(not available)' it does not."""
    text = ai._render_prompt(ai.build_payload({"question": "Q?"}), "2026-08-28T00:00:00Z")
    assert "(not available)" in text
    assert "(none published" in text


# ── 4. shaping and honest failure ───────────────────────────────────────

def _ok_payload(**over):
    base = {
        "summary": "It turns on the September FOMC meeting.",
        "resolutionNotes": ["A 50bps cut does not settle this leg YES."],
        "drivers": ["August CPI print on September 11."],
        "unknowns": ["Current CME FedWatch pricing."],
        "reasoning": ["Three of the last four cuts followed a soft print."],
        "lean": "cheap",
        "confidence": "medium",
    }
    base.update(over)
    return base


def test_shape_passes_a_good_response_through():
    out = ai._shape(_ok_payload(), "anthropic", "claude-opus-5", "T", False, [], 900.0)
    assert out["ok"] is True
    assert out["lean"] == "cheap"
    assert out["redactions"] == 0
    assert out["reason"] is None


def test_a_lean_with_no_surviving_argument_becomes_no_view():
    """If the scrubber removed every reasoning line, the lean has nothing
    behind it — which is precisely what this module exists not to show."""
    out = ai._shape(
        _ok_payload(reasoning=["I'd put it at 70%."]),
        "anthropic", "m", "T", False, [], 1.0)
    assert out["redactions"] == 1
    assert out["lean"] == "no_view"
    assert out["confidence"] == "low"


def test_an_unknown_lean_falls_back_to_no_view():
    out = ai._shape(_ok_payload(lean="definitely_buy"), "openai", "m", "T", False, [], 1.0)
    assert out["lean"] == "no_view"


def test_failure_and_success_share_one_shape():
    """The UI renders one contract. A failure that omitted keys would make
    every field on the panel an optional-chain."""
    ok = ai._shape(_ok_payload(), "anthropic", "m", "T", False, [], 1.0)
    bad = ai._fail("anthropic", "m", "T", "No key stored.", "Add one.")
    assert set(ok) == set(bad)
    assert bad["ok"] is False
    assert bad["reason"] and bad["fix"]


# ── 5. the gate actually covers the providers ───────────────────────────

def test_both_providers_are_declared_hosts():
    """A provider the offline switch does not reach would make that switch a
    lie, and the privacy panel's claim to name every host false."""
    for p in ai.PROVIDERS:
        assert p in terminal._HOSTS
        assert p in terminal._gates
        assert terminal._HOSTS[p]["host"]
        assert terminal._HOSTS[p]["purpose"]


def test_providers_appear_in_the_privacy_panel():
    hosts = {row["host"] for row in terminal.health()["providers"]}
    assert {"api.anthropic.com", "api.openai.com"} <= hosts


def test_offline_mode_refuses_before_any_call():
    terminal.set_offline(True)
    try:
        with pytest.raises(ai._Refused) as e:
            _run(ai._gate_call("anthropic").__aenter__())
        assert "Offline mode" in e.value.reason
    finally:
        terminal.set_offline(False)


def test_a_parked_host_fails_fast():
    gate = terminal._gates["anthropic"]
    gate.park(30.0)
    try:
        with pytest.raises(ai._Refused) as e:
            _run(ai._gate_call("anthropic").__aenter__())
        assert "rate limit" in e.value.reason
    finally:
        gate.parked_until = 0.0


def test_a_cancelled_wait_does_not_strand_the_host_lock():
    """The gap between taking the lock and returning from __aenter__ contains
    an await. __aexit__ never runs when __aenter__ raises, so a cancellation
    there — the panel closed, the RPC timed out — used to leave the host lock
    held by a coroutine that no longer exists. Every later call then waited on
    it forever and the feature was dead until restart."""
    gate = _fresh("anthropic")
    old_gap, old_last = gate.min_gap, gate.last

    async def scenario():
        gate.min_gap = 5.0
        gate.last = time.monotonic()
        task = asyncio.ensure_future(ai._gate_call("anthropic").__aenter__())
        for _ in range(3):
            await asyncio.sleep(0)
        assert gate.lock().locked(), "the test never reached the min-gap wait"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not gate.lock().locked(), "a cancelled wait stranded the host lock"

        # The point of the fix: the NEXT call still gets in.
        gate.min_gap = 0.0
        async with ai._gate_call("anthropic"):
            pass
        assert not gate.lock().locked()

    try:
        _run(scenario())
    finally:
        gate.min_gap, gate.last = old_gap, old_last
        gate._lock = None


def _fake_anthropic(monkeypatch, closed: list, *, fail: bool = False):
    """A stand-in for the SDK, so the close is testable without the package."""
    class _Block:
        type = "text"
        text = json.dumps(_ok_payload())

    class _Resp:
        stop_reason = None
        model = "claude-test"
        content = [_Block()]

    class _Messages:
        async def create(self, **kwargs):
            if fail:
                raise RuntimeError("provider exploded")
            return _Resp()

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

        async def close(self):
            closed.append(True)

    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setattr(ai.ai_secrets, "load_key", lambda p: "sk-test", raising=False)


@pytest.mark.parametrize("fail", [False, True])
def test_the_sdk_client_is_closed_on_both_paths(monkeypatch, fail):
    """Each SDK client opens an HTTP connection pool at construction. One per
    analysis, never closed, is a socket leak in a process the user leaves
    running for days — and the failing path is the one that leaks fastest."""
    closed: list = []
    _fake_anthropic(monkeypatch, closed, fail=fail)
    _fresh("anthropic")
    try:
        if fail:
            with pytest.raises(RuntimeError):
                _run(ai._call_anthropic("prompt", "claude-test", False))
        else:
            got = _run(ai._call_anthropic("prompt", "claude-test", False))
            assert json.loads(got["raw"])["lean"] == "cheap"
        assert closed == [True], "the SDK client was never closed"
    finally:
        terminal._gates["anthropic"]._lock = None


@pytest.mark.parametrize("raw", ["null", '"just a sentence"', "[]", "42", "{oops"])
def test_a_reply_that_is_not_an_object_fails_honestly(monkeypatch, raw):
    """`null`, a bare string, a list and a number are all VALID JSON, so the
    parse succeeds and only the shape is wrong. _shape reads the reply as a
    mapping, so anything else raised straight out of analyze() — the one
    function that promises never to raise for an expected condition."""
    async def fake_call(prompt, model, web_search):
        return {"raw": raw, "model": "claude-test", "sources": []}

    monkeypatch.setattr(ai, "_call_anthropic", fake_call)
    out = _run(ai.analyze(
        {"question": "Will the Fed cut in September?"},
        {"ai_enabled": True, "ai_provider": "anthropic"}))
    assert out["ok"] is False
    assert "not valid JSON" in out["reason"]
    assert out["fix"]
    # Same contract as every other failure, so the panel has nothing to
    # optional-chain around.
    assert set(out) == set(ai._fail("anthropic", "m", "T", "r", None))


def test_rate_limit_detection_needs_no_sdk_import():
    class RateLimitError(Exception):
        pass

    class Other(Exception):
        status_code = 429

    assert ai._is_rate_limit(RateLimitError())
    assert ai._is_rate_limit(Other())
    assert not ai._is_rate_limit(ValueError("nope"))


# ── 6. config: off, and clamped ─────────────────────────────────────────

def test_ships_off_and_keyless():
    d = config.DEFAULT_CONFIG
    assert d["ai_enabled"] is False
    assert d["ai_web_search"] is False
    # One model key now; OpenAI still has no hardcoded default (the per-
    # provider default for an empty id is "").
    assert "ai_openai_model" not in d and "ai_anthropic_model" not in d
    assert config.merge_with_defaults({"ai_provider": "openai"})["ai_model"] == ""
    assert d["autopilot_enabled"] is False


def test_unknown_provider_falls_back():
    """The provider name reaches a dispatch table, so it is clamped."""
    cfg = config._validate_config({**config.DEFAULT_CONFIG, "ai_provider": "../../etc"})
    assert cfg["ai_provider"] == "anthropic"


def test_model_id_is_bounded():
    cfg = config._validate_config({**config.DEFAULT_CONFIG, "ai_model": "x" * 500})
    # Out of shape -> the provider's default, never a 500-char id on the wire.
    assert cfg["ai_model"] == "claude-opus-5-5"
    cfg = config._validate_config({**config.DEFAULT_CONFIG, "ai_provider": "gemini",
                                   "ai_model": "../../v1/files"})
    assert cfg["ai_model"] == "gemini-3.8-flash"


def test_analysis_is_refused_while_disabled():
    """A stored key is not consent to start sending market text to a third
    party. Both switches have to be on."""
    out = _run(ai.analyze({"question": "Q?"}, {"ai_enabled": False}))
    assert out["ok"] is False


# ── 7. six providers, one no-number guarantee ───────────────────────────
# The four HTTP providers run against a mocked httpx transport (no network).
# Each is asked for _SCHEMA by its own mechanism, and each reply — whatever
# the provider did with that request — passes _conform before _shape.

import httpx  # noqa: E402

import ai_providers as ap  # noqa: E402

_HTTP_CHAT = {
    "openrouter": ("POST", "/api/v1/chat/completions"),
    "lmstudio": ("POST", "/v1/chat/completions"),
    "ollama": ("POST", "/api/chat"),
    "gemini": ("POST", "/v1beta/models/gemini-3.8-flash:generateContent"),
}
_LMS = "qwen2.5-7b-instruct"
HTTP_PROVIDERS = ("openrouter", "gemini", "ollama", "lmstudio")


def _http_reply(provider: str, text: str) -> dict:
    if provider in ("openrouter", "lmstudio"):
        return {"choices": [{"message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 200}}
    if provider == "ollama":
        return {"message": {"role": "assistant", "content": text}, "done": True,
                "prompt_eval_count": 900, "eval_count": 200}
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 200}}


class _Net:
    def __init__(self):
        self.requests: list = []
        self.routes: dict = {}

    def __call__(self, request):
        self.requests.append(request)
        h = self.routes.get((request.method, request.url.path))
        if h is None:
            return httpx.Response(404, json={"error": f"no route {request.url.path}"})
        return h(request)

    def bodies(self, method, path):
        return [json.loads(r.content or b"{}") for r in self.requests
                if r.method == method and r.url.path == path]


@pytest.fixture
def net(monkeypatch):
    n = _Net()
    monkeypatch.setattr(ap, "_TRANSPORT", httpx.MockTransport(n))
    ap._LISTED.clear()
    keys = {"openrouter": "sk-" + "or-v1-" + "0f" * 32, "gemini": "AI" + "za" + "X" * 35}
    monkeypatch.setattr(ai, "read_key", lambda p: keys.get(p))
    n.routes[("GET", "/api/v0/models")] = lambda r: httpx.Response(200, json={"data": [
        {"id": _LMS, "type": "llm", "state": "loaded", "loaded_context_length": 32768}]})
    for g in HTTP_PROVIDERS:
        terminal._gates[g].parked_until = 0.0
    yield n
    ap._LISTED.clear()


def _analyze_http(net, provider: str, reply_obj, **cfg) -> dict:
    text = reply_obj if isinstance(reply_obj, str) else json.dumps(reply_obj)
    net.routes[_HTTP_CHAT[provider]] = lambda r: httpx.Response(
        200, json=_http_reply(provider, text))
    return _run(ai.analyze(
        {"question": "Will the Fed cut in September?", "probability": 61.0,
         "_raw": {"description": "Resolves YES if the target range is lowered."}},
        {"ai_enabled": True, "ai_provider": provider, "ai_model": "", **cfg}))


def _no_numeric_types(schema) -> bool:
    if isinstance(schema, dict):
        if schema.get("type") in ("number", "integer"):
            return False
        return all(_no_numeric_types(v) for v in schema.values())
    if isinstance(schema, list):
        return all(_no_numeric_types(v) for v in schema)
    return True


@pytest.mark.parametrize("provider", HTTP_PROVIDERS)
def test_every_http_provider_is_asked_for_the_closed_schema(net, provider):
    out = _analyze_http(net, provider, _ok_payload())
    assert out["ok"] is True and out["lean"] == "cheap", out
    body = net.bodies(*_HTTP_CHAT[provider])[0]
    if provider in ("openrouter", "lmstudio"):
        rf = body["response_format"]
        assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
        sent = rf["json_schema"]["schema"]
        assert sent["additionalProperties"] is False
        if provider == "openrouter":
            # Never routed to an upstream that would ignore response_format.
            assert body["provider"]["require_parameters"] is True
    elif provider == "ollama":
        sent = body["format"]
        assert sent["additionalProperties"] is False
    else:
        gen = body["generationConfig"]
        assert gen["responseMimeType"] == "application/json"
        sent = gen["responseSchema"]
        assert sent["propertyOrdering"][-2:] == ["lean", "confidence"]
    assert _no_numeric_types(sent)
    assert set(sent["properties"]) == set(ai._SCHEMA["properties"])


@pytest.mark.parametrize("provider", HTTP_PROVIDERS)
def test_an_invented_probability_field_is_rejected_on_every_provider(net, provider):
    out = _analyze_http(net, provider, {**_ok_payload(), "probability": 0.7})
    assert out["ok"] is False
    assert "broke the response format" in out["reason"]
    assert out["lean"] is None and out["summary"] is None and out["reasoning"] == []


@pytest.mark.parametrize("provider", HTTP_PROVIDERS)
@pytest.mark.parametrize("bad", [
    {"summary": 70},
    {"reasoning": ["Soft print.", 0.68]},
    {"lean": "cheap", "confidence": 3},
    {"unknowns": {"fairValueCents": 41}},
    {"probability": "seventy percent"},
])
def test_a_number_or_extra_field_anywhere_is_rejected_on_every_provider(net, provider, bad):
    out = _analyze_http(net, provider, {**_ok_payload(), **bad})
    assert out["ok"] is False and "broke the response format" in out["reason"]
    assert set(out) == set(ai._fail("x", "m", "T", "r", None))


@pytest.mark.parametrize("provider", HTTP_PROVIDERS)
def test_a_numeric_estimate_in_prose_is_scrubbed_on_every_provider(net, provider):
    out = _analyze_http(net, provider, _ok_payload(
        reasoning=["Three of the last four cuts followed a soft print.",
                   "I'd put it at around 70%."]))
    assert out["ok"] is True and out["redactions"] == 1
    assert all("70%" not in r for r in out["reasoning"])


@pytest.mark.parametrize("provider", ("ollama", "lmstudio", "openrouter"))
def test_a_draft_number_inside_reasoning_is_not_the_answer(net, provider):
    """A local reasoning model can think out loud with a draft object; the
    answer is what follows the thought, and it still passes _conform."""
    draft = json.dumps({"summary": "draft", "probability": 0.7})
    text = (f"<think>first try: {draft}</think>\n```json\n"
            f"{json.dumps(_ok_payload())}\n```")
    out = _analyze_http(net, provider, text)
    assert out["ok"] is True and out["summary"] != "draft"


def test_gemini_with_search_skips_response_schema_but_still_rejects_numbers(net):
    """Gemini models before 3 refuse responseSchema alongside the search tool,
    so with search on the app's own check is the guarantee — and it holds."""
    out = _analyze_http(net, "gemini", {**_ok_payload(), "fairValue": 55},
                        ai_web_search=True)
    body = net.bodies(*_HTTP_CHAT["gemini"])[0]
    assert body["tools"] == [{"google_search": {}}]
    assert "responseSchema" not in body["generationConfig"]
    assert out["ok"] is False and "broke the response format" in out["reason"]


@pytest.mark.parametrize("provider", ("ollama", "lmstudio"))
def test_a_local_model_never_claims_it_searched(net, provider):
    out = _analyze_http(net, provider, _ok_payload(), ai_web_search=True)
    assert out["ok"] is True and out["webSearch"] is False


def test_sdk_providers_pass_the_same_schema_check(monkeypatch):
    """Anthropic and OpenAI enforce the schema themselves; the check runs
    anyway, so a provider-side regression can never put a number on screen."""
    async def fake_call(prompt, model, web_search):
        return {"raw": json.dumps({**_ok_payload(), "fairValueCents": 41}),
                "model": "m", "sources": []}

    for name, prov in (("_call_anthropic", "anthropic"), ("_call_openai", "openai")):
        monkeypatch.setattr(ai, name, fake_call)
        out = _run(ai.analyze({"question": "Q?"},
                              {"ai_enabled": True, "ai_provider": prov, "ai_model": "gpt-x"}))
        assert out["ok"] is False and "broke the response format" in out["reason"]


def test_a_cloud_http_provider_without_a_key_never_calls_out(net, monkeypatch):
    monkeypatch.setattr(ai, "read_key", lambda p: None)
    monkeypatch.setattr(ai.ai_secrets, "load_key", lambda p: None)
    for provider in ("openrouter", "gemini"):
        out = _analyze_http(net, provider, _ok_payload())
        assert out["ok"] is False and "key" in out["reason"].lower()
    assert [r for r in net.requests if r.method == "POST"] == []


def test_offline_mode_stops_cloud_providers_but_not_loopback(net):
    terminal.set_offline(True)
    try:
        out = _analyze_http(net, "openrouter", _ok_payload())
        assert out["ok"] is False and "Offline mode" in out["reason"]
        assert net.bodies(*_HTTP_CHAT["openrouter"]) == []
        # 127.0.0.1 never leaves the machine.
        assert _analyze_http(net, "ollama", _ok_payload())["ok"] is True
    finally:
        terminal.set_offline(False)


def test_legacy_model_keys_migrate_to_ai_model():
    c = config.merge_with_defaults({"aiProvider": "openai", "aiOpenaiModel": "gpt-5.5",
                                    "aiAnthropicModel": "claude-sonnet-5"})
    assert c["ai_model"] == "gpt-5.5"
    assert "ai_openai_model" not in c and "ai_anthropic_model" not in c
    c = config.merge_with_defaults({"ai_provider": "anthropic",
                                    "ai_anthropic_model": "claude-sonnet-5"})
    assert c["ai_model"] == "claude-sonnet-5"
    # A config that already names ai_model is never rewritten by the legacy key.
    c = config.merge_with_defaults({"ai_provider": "openai", "ai_model": "gpt-a",
                                    "ai_openai_model": "gpt-b"})
    assert c["ai_model"] == "gpt-a"
    # Anthropic's default never travels to another provider.
    assert config.merge_with_defaults({"ai_provider": "gemini"})["ai_model"] == "gemini-3.8-flash"


def test_status_reports_every_provider_and_never_a_key():
    st = ai.status(config.merge_with_defaults({"ai_provider": "ollama"}))
    assert st["provider"] == "ollama" and st["hasKey"] is True       # needs none
    assert st["keys"]["ollama"] == {"present": False, "masked": None, "needsKey": False}
    assert set(st["providers"]) == set(ai.PROVIDERS)
    assert st["capabilities"]["gemini"]["webSearch"] is True
    assert st["capabilities"]["ollama"]["webSearch"] is False
    assert "sk-" not in json.dumps(st)


def test_new_hosts_are_declared_for_the_privacy_panel():
    hosts = {row["host"] for row in terminal.health()["providers"]}
    assert {"openrouter.ai", "generativelanguage.googleapis.com",
            "127.0.0.1:11434", "127.0.0.1:1234"} <= hosts
