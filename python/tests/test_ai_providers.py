"""ai_providers: OpenRouter, Gemini, Ollama, LM Studio (+ the free listings
for Anthropic/OpenAI), against a mocked httpx — nothing touches the network.

Pinned, per provider: each failure is a sentence the user can act on; keys go
in a header (never a URL), to the encrypted store (never settings.json) and
never into a log line; every request goes to a hardcoded host; a redirect is
never followed; a malformed model id never travels; Test connection uses free
endpoints only; local context windows are guarded instead of truncated.

The no-number guarantee for the analysis on these providers is pinned in
test_ai_analysis.py (section 7); Autopilot on them in test_autopilot.py.
"""
from __future__ import annotations

import json

import httpx
import pytest

import ai_analysis as ai
import ai_providers as ap
import ai_secrets
import terminal

OR_KEY = "sk-" + "or-v1-" + "0f" * 32
GEMINI_KEY = "AI" + "za" + "SyN0tAR3alG00gleK3yXXXXXXXXXXXXXXXX"
assert len(GEMINI_KEY) == 39
LMS_MODEL = "qwen2.5-7b-instruct"
CHAT = {
    "openrouter": ("POST", "/api/v1/chat/completions"),
    "lmstudio": ("POST", "/v1/chat/completions"),
    "ollama": ("POST", "/api/chat"),
    "gemini": ("POST", "/v1beta/models/gemini-3.8-flash:generateContent"),
}
PROVIDERS = ("openrouter", "gemini", "ollama", "lmstudio")
GOOD = json.dumps({"summary": "s", "resolutionNotes": [], "drivers": [], "unknowns": [],
                   "reasoning": ["r"], "lean": "fair", "confidence": "low"})


def J(status, body):
    return lambda req: httpx.Response(status, json=body)


def _down(request):
    raise httpx.ConnectError("connection refused", request=request)


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


KEYS = {"openrouter": OR_KEY, "gemini": GEMINI_KEY}


@pytest.fixture
def net(monkeypatch):
    r = Router()
    monkeypatch.setattr(ap, "_TRANSPORT", httpx.MockTransport(r))
    ap._LISTED.clear()
    monkeypatch.setattr(ai, "read_key", lambda p: KEYS.get(p))
    r.on("GET", "/api/v0/models", J(200, {"data": [
        {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"},
        {"id": LMS_MODEL, "type": "llm", "state": "loaded",
         "loaded_context_length": 32768, "max_context_length": 32768}]}))
    for g in ("anthropic", "openai", *PROVIDERS):
        terminal._gates[g].parked_until = 0.0
    yield r
    ap._LISTED.clear()


def reply(provider, text):
    if provider in ("openrouter", "lmstudio"):
        msg = {"role": "assistant", "content": text}
        if provider == "openrouter":
            msg["annotations"] = [{"type": "url_citation", "url_citation": {
                "url": "https://www.federalreserve.gov/x", "title": "Fed"}}]
        return {"choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "cost": 0.0021}}
    if provider == "ollama":
        return {"message": {"role": "assistant", "content": text}, "done": True,
                "prompt_eval_count": 1200, "eval_count": 300}
    return {"candidates": [{
        "content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP",
        "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.example/r1", "title": "fed.gov"}}]}}],
        "usageMetadata": {"promptTokenCount": 1200, "candidatesTokenCount": 250,
                          "thoughtsTokenCount": 50}}


def _call(provider, *, model="", web=False, schema=None):
    return ap.call(provider, KEYS.get(provider), ap.clean_model(provider, model),
                   "SYSTEM", "PROMPT", web, 30.0, schema=schema)


def _err(net, provider, handler) -> str:
    net.on(*CHAT[provider], handler)
    with pytest.raises(ap.AiError) as e:
        _call(provider)
    return str(e.value)


# ── the call itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize("provider", PROVIDERS)
def test_system_and_user_prompt_land_where_each_provider_reads_them(net, provider):
    net.on(*CHAT[provider], J(200, reply(provider, GOOD)))
    out = _call(provider)
    assert out["text"] == GOOD
    body = net.bodies(*CHAT[provider])[0]
    if provider == "gemini":
        assert body["systemInstruction"]["parts"][0]["text"] == "SYSTEM"
        assert body["contents"][0]["parts"][0]["text"] == "PROMPT"
    else:
        assert body["messages"][0] == {"role": "system", "content": "SYSTEM"}
        assert body["messages"][1] == {"role": "user", "content": "PROMPT"}
    # Gemini's thinking tokens are billed as output, so they count as output.
    assert out["inputTokens"] == 1200 and out["outputTokens"] == 300


def test_openrouter_search_uses_the_web_plugin_and_keeps_citations(net):
    net.on(*CHAT["openrouter"], J(200, reply("openrouter", GOOD)))
    out = _call("openrouter", web=True)
    assert net.bodies(*CHAT["openrouter"])[0]["plugins"] == [{"id": "web", "max_results": 5}]
    assert out["webSearchUsed"] and out["citations"][0]["url"].startswith("https://www.federal")
    assert out["costUsd"] == pytest.approx(0.0021)


def test_gemini_search_is_google_grounding_with_its_own_citations(net):
    net.on(*CHAT["gemini"], J(200, reply("gemini", GOOD)))
    out = _call("gemini", web=True)
    assert net.bodies(*CHAT["gemini"])[0]["tools"] == [{"google_search": {}}]
    assert out["citations"] == [{"title": "fed.gov", "url": "https://vertexaisearch.example/r1"}]


# ── errors a user can act on ────────────────────────────────────────────

def test_openrouter_rejected_key(net):
    assert "OpenRouter rejected the key" in _err(
        net, "openrouter", J(401, {"error": {"code": 401, "message": "User not found."}}))


def test_openrouter_out_of_credits_is_not_called_a_rate_limit(net):
    msg = _err(net, "openrouter", J(402, {"error": {"code": 402, "message": "Insufficient credits"}}))
    assert "out of credits" in msg and ":free" in msg


def test_openrouter_error_inside_a_200(net):
    assert "OpenRouter rejected the key" in _err(
        net, "openrouter", J(200, {"error": {"code": 401, "message": "No auth"}}))


def test_openrouter_with_no_endpoint_for_the_schema_says_so(net):
    msg = _err(net, "openrouter", J(404, {"error": {
        "code": 404, "message": "No endpoints found that can handle the requested parameters."}}))
    assert "structured output" in msg


def test_gemini_bad_key_is_a_400_and_still_reads_as_a_bad_key(net):
    msg = _err(net, "gemini", J(400, {"error": {
        "code": 400, "message": "API key not valid. Please pass a valid API key.",
        "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}))
    assert "Google rejected that Gemini API key" in msg


def test_gemini_quota(net):
    msg = _err(net, "gemini", J(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                 "message": "Quota exceeded"}}))
    assert "quota" in msg.lower() and "per day" in msg


def test_a_429_parks_the_host_and_the_next_call_fails_fast(net):
    _err(net, "gemini", J(429, {"error": {"code": 429, "message": "slow down"}}))
    assert terminal._gates["gemini"].parked()
    n = len(net.requests)
    with pytest.raises(ap.AiError) as e:
        _call("gemini")
    assert "parked" in str(e.value) and len(net.requests) == n


def test_gemini_refusal_is_said_not_shown_as_an_empty_analysis(net):
    assert "declined" in _err(net, "gemini", J(200, {
        "candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}))


def test_ollama_not_running(net):
    msg = _err(net, "ollama", _down)
    assert "Ollama isn't running" in msg and "`ollama serve`" in msg


def test_ollama_model_not_pulled(net):
    msg = _err(net, "ollama", J(404, {"error": 'model "llama3.1:8b" not found, try pulling it first'}))
    assert msg == "llama3.1:8b isn't pulled in Ollama: run `ollama pull llama3.1:8b`."


def test_lmstudio_not_running(net):
    net.on("GET", "/api/v0/models", _down)
    net.on("GET", "/v1/models", _down)
    assert "LM Studio's local server isn't running" in _err(net, "lmstudio", _down)


def test_lmstudio_with_nothing_downloaded(net):
    net.on("GET", "/api/v0/models", J(200, {"data": []}))
    assert "no chat model downloaded" in _err(net, "lmstudio", J(200, reply("lmstudio", GOOD)))


def test_lmstudio_context_too_small_is_refused_before_sending(net):
    net.on("GET", "/api/v0/models", J(200, {"data": [
        {"id": LMS_MODEL, "type": "llm", "state": "loaded", "loaded_context_length": 2048}]}))
    msg = _err(net, "lmstudio", J(200, reply("lmstudio", GOOD)))
    assert "2,048-token context" in msg and "larger context length" in msg
    assert net.bodies(*CHAT["lmstudio"]) == []


def test_lmstudio_guard_ignores_the_architectural_max_of_an_unloaded_model(net):
    net.on("GET", "/api/v0/models", J(200, {"data": [
        {"id": LMS_MODEL, "type": "llm", "state": "not-loaded", "max_context_length": 131072}]}))
    net.on(*CHAT["lmstudio"], J(200, reply("lmstudio", GOOD)))
    with pytest.raises(ap.AiError) as e:
        # A real analysis prompt is a few thousand characters of rules text.
        ap.call("lmstudio", None, LMS_MODEL, "SYSTEM", "rules " * 400, False, 30.0)
    assert "isn't loaded" in str(e.value) and net.bodies(*CHAT["lmstudio"]) == []


def test_ollama_context_is_set_explicitly_and_overflow_refused(net, monkeypatch):
    net.on(*CHAT["ollama"], J(200, reply("ollama", GOOD)))
    _call("ollama")
    assert net.bodies(*CHAT["ollama"])[0]["options"]["num_ctx"] == ap._OLLAMA_CTX
    monkeypatch.setattr(ap, "_OLLAMA_CTX", 1000)
    monkeypatch.setattr(ap, "_OLLAMA_PREDICT", 999)
    with pytest.raises(ap.AiError) as e:
        _call("ollama")
    assert "too big" in str(e.value)
    assert len(net.bodies(*CHAT["ollama"])) == 1


# ── keys, hosts and model ids ───────────────────────────────────────────

def test_the_gemini_key_goes_in_a_header_never_the_url(net):
    net.on(*CHAT["gemini"], J(200, reply("gemini", GOOD)))
    _call("gemini")
    req = next(r for r in net.requests if r.method == "POST")
    assert req.headers["x-goog-api-key"] == GEMINI_KEY
    assert GEMINI_KEY not in str(req.url) and "key=" not in str(req.url)


def test_openrouter_sends_the_key_and_no_attribution_headers(net):
    net.on(*CHAT["openrouter"], J(200, reply("openrouter", GOOD)))
    _call("openrouter")
    req = next(r for r in net.requests if r.method == "POST")
    assert req.headers["authorization"] == f"Bearer {OR_KEY}"
    assert "http-referer" not in req.headers and "x-title" not in req.headers


def test_local_providers_send_no_credentials(net):
    for provider in ("ollama", "lmstudio"):
        net.on(*CHAT[provider], J(200, reply(provider, GOOD)))
        _call(provider)
    for r in net.requests:
        assert "authorization" not in r.headers and "x-goog-api-key" not in r.headers


def test_every_request_goes_to_a_hardcoded_host(net):
    for provider in PROVIDERS:
        net.on(*CHAT[provider], J(200, reply(provider, GOOD)))
        _call(provider)
        ai.check_provider(provider, {"ai_provider": provider})
    hosts = {ap.stat_host(r.url) for r in net.requests}
    assert hosts <= {"openrouter.ai", "generativelanguage.googleapis.com",
                     "127.0.0.1:11434", "127.0.0.1:1234"}, hosts


def test_a_redirect_is_never_followed_with_a_key_on_it(net):
    net.on(*CHAT["gemini"], lambda req: httpx.Response(
        302, headers={"location": "https://evil.example.com/steal"}))
    with pytest.raises(ap.AiError):
        _call("gemini")
    assert all(r.url.host != "evil.example.com" for r in net.requests)


@pytest.mark.parametrize("provider,bad", [
    ("gemini", "../../v1/files"),
    ("gemini", "gemini-2.5-flash/../x"),
    ("gemini", "gemini-2.5-flash:generateContent?key=x"),
    ("openrouter", "http://evil.example.com/x"),
    ("openrouter", "a" * 300),
    ("ollama", "llama3\nHost: evil"),
    ("anthropic", "claude/../../x"),
    ("lmstudio", " "),
])
def test_a_malformed_model_id_never_travels(provider, bad):
    assert ai.normalize_model(provider, bad) == ap.DEFAULT_MODEL[provider]


@pytest.mark.parametrize("provider,good", [
    ("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
    ("gemini", "models/gemini-2.5-pro"),
    ("ollama", "hf.co/bartowski/Llama-3.2-3B-Instruct-GGUF:Q4_K_M"),
    ("lmstudio", "lmstudio-community/qwen2.5-7b-instruct"),
    ("anthropic", "claude-sonnet-5"),
    ("openai", "gpt-5.5"),
])
def test_real_model_ids_are_accepted(provider, good):
    assert ai.normalize_model(provider, good) == good.replace("models/", "")


def test_keys_go_to_the_encrypted_store_and_local_providers_take_none(monkeypatch):
    import polymarket_auth as auth
    saved = {}
    monkeypatch.setattr(auth, "_write_secret_bytes", lambda p, b: saved.__setitem__(p.name, b))
    ai_secrets.save_key("openrouter", OR_KEY)
    ai_secrets.save_key("gemini", GEMINI_KEY)
    assert saved == {"ai.openrouter.key": OR_KEY.encode(), "ai.gemini.key": GEMINI_KEY.encode()}
    for local in ("ollama", "lmstudio"):
        assert not ai_secrets.stores_key(local) and not ai_secrets.has_key(local)
        with pytest.raises(ValueError):
            ai_secrets.save_key(local, "anything")


def test_no_ai_key_has_a_settings_json_home():
    from config import DEFAULT_CONFIG, merge_with_defaults
    assert not [k for k in DEFAULT_CONFIG
                if k.startswith(("ai_", "autopilot_")) and ("key" in k or "token" in k)
                and k != "autopilot_daily_token_budget"]
    assert merge_with_defaults({"ai_provider": OR_KEY})["ai_provider"] == "anthropic"


def test_the_new_keys_are_redacted_by_shape():
    import logscrub
    logscrub.set_known_secrets([])
    try:
        out = logscrub.scrub(f"calling with {OR_KEY} then {GEMINI_KEY}")
        assert OR_KEY not in out and GEMINI_KEY not in out
    finally:
        logscrub.set_known_secrets([])


# ── check_provider: free endpoints only ─────────────────────────────────

def _billed(net) -> list:
    return [r for r in net.requests
            if "chat" in r.url.path or "generateContent" in r.url.path
            or r.url.path.endswith("/messages") or r.url.path.endswith("/responses")]


def test_check_openrouter_ok_lists_models_and_tool_support(net):
    net.on("GET", "/api/v1/key", J(200, {"data": {"label": "x", "limit_remaining": 4.2}}))
    net.on("GET", "/api/v1/models", J(200, {"data": [
        {"id": "google/gemini-2.5-flash", "supported_parameters": ["tools", "max_tokens"],
         "context_length": 1048576},
        {"id": "some/no-tools-model", "supported_parameters": ["max_tokens"]}]}))
    res = ai.check_provider("openrouter", {"ai_provider": "openrouter"})
    assert res["ok"] is True and res["tools"] is True
    assert res["models"] == ["google/gemini-2.5-flash", "some/no-tools-model"]
    assert "accepted the key" in res["message"] and _billed(net) == []
    # Cached for the Settings dropdown, without a network call.
    assert "some/no-tools-model" in ai.status({"ai_provider": "openrouter"})["models"]["openrouter"]


def test_check_openrouter_bad_key_and_empty_credit(net):
    net.on("GET", "/api/v1/key", J(401, {"error": {"code": 401, "message": "No auth"}}))
    res = ai.check_provider("openrouter", {"ai_provider": "openrouter"})
    assert res["ok"] is False and "OpenRouter rejected the key" in res["message"]
    net.on("GET", "/api/v1/key", J(200, {"data": {"limit_remaining": 0}}))
    res = ai.check_provider("openrouter", {"ai_provider": "openrouter"})
    assert res["ok"] is False and "credits" in res["message"]


def test_check_gemini_lists_only_text_models_and_strips_the_prefix(net):
    net.on("GET", "/v1beta/models", J(200, {"models": [
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]}]}))
    res = ai.check_provider("gemini", {"ai_provider": "gemini"})
    assert res["ok"] is True and res["models"] == ["gemini-3.8-flash", "gemma-3-27b-it"]
    assert {m["id"]: m["tools"] for m in res["modelInfo"]} == {
        "gemini-3.8-flash": True, "gemma-3-27b-it": False}
    assert net.requests[0].headers["x-goog-api-key"] == GEMINI_KEY and _billed(net) == []


def test_test_connection_fails_when_the_configured_model_is_not_listed(net):
    net.on("GET", "/v1beta/models", J(200, {"models": [
        {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]}))
    res = ai.check_provider("gemini", {"ai_provider": "gemini", "ai_model": "gemini-9-imaginary"})
    assert res["ok"] is False and res["reason"] == "model"
    assert "gemini-9-imaginary" in res["message"] and "isn't among" in res["message"]


def test_check_ollama_down_not_pulled_and_ok(net):
    net.on("GET", "/api/tags", _down)
    res = ai.check_provider("ollama", {"ai_provider": "ollama"})
    assert res["ok"] is False and "`ollama serve`" in res["message"]
    net.on("GET", "/api/tags", J(200, {"models": [{"model": "qwen3:8b", "details": {}}]}))
    res = ai.check_provider("ollama", {"ai_provider": "ollama", "ai_model": "llama3.1:8b"})
    assert res["ok"] is False and "`ollama pull llama3.1:8b`" in res["message"]
    net.on("POST", "/api/show", J(200, {"capabilities": ["completion", "tools"]}))
    res = ai.check_provider("ollama", {"ai_provider": "ollama", "ai_model": "qwen3:8b"})
    assert res["ok"] is True and res["tools"] is True and "Autopilot can use it" in res["message"]
    assert _billed(net) == []


def test_check_ollama_untagged_name_means_latest(net):
    net.on("GET", "/api/tags", J(200, {"models": [{"model": "mistral-nemo:latest"}]}))
    net.on("POST", "/api/show", J(200, {"capabilities": ["completion"]}))
    res = ai.check_provider("ollama", {"ai_provider": "ollama", "ai_model": "mistral-nemo"})
    assert res["ok"] is True and res["tools"] is False and "can't call tools" in res["message"]


def test_check_lmstudio_falls_back_to_the_openai_listing(net):
    net.on("GET", "/api/v0/models", J(404, {"error": "Unexpected endpoint"}))
    net.on("GET", "/v1/models", J(200, {"data": [{"id": LMS_MODEL, "object": "model"}]}))
    res = ai.check_provider("lmstudio", {"ai_provider": "lmstudio"})
    assert res["ok"] is True and res["models"] == [LMS_MODEL] and _billed(net) == []


def test_check_anthropic_and_openai_use_the_free_listing(net, monkeypatch):
    monkeypatch.setattr(ai, "read_key", lambda p: "sk-test")
    net.on("GET", "/v1/models", J(401, {"type": "error", "error": {
        "type": "authentication_error", "message": "invalid x-api-key"}}))
    res = ai.check_provider("anthropic", {"ai_provider": "anthropic"})
    assert res["ok"] is False and "That Anthropic API key was rejected" in res["message"]
    assert net.requests[0].headers["x-api-key"] == "sk-test"
    net.on("GET", "/v1/models", J(200, {"data": [{"id": "claude-opus-5-5"}, {"id": "x"}]}))
    res = ai.check_provider("anthropic", {"ai_provider": "anthropic"})
    assert res["ok"] is True and "claude-opus-5-5" in res["models"]
    # OpenAI ships with no model: the key is fine, the setting is unfinished.
    res = ai.check_provider("openai", {"ai_provider": "openai", "ai_model": ""})
    assert res["ok"] is False and res["reason"] == "model" and "pick one" in res["message"]
    assert _billed(net) == []


def test_check_without_a_key_makes_no_request(net, monkeypatch):
    monkeypatch.setattr(ai, "read_key", lambda p: None)
    res = ai.check_provider("gemini", {})
    assert res["ok"] is False and "Settings" in res["message"] and net.requests == []


def test_check_an_unknown_provider():
    res = ai.check_provider("http://evil.example.com")
    assert res["ok"] is False and "Unknown" in res["message"]


def test_offline_mode_blocks_a_cloud_check_with_the_offline_sentence(net):
    terminal.set_offline(True)
    try:
        res = ai.check_provider("openrouter", {"ai_provider": "openrouter"})
        assert res["ok"] is False and "Offline mode" in res["message"]
        assert net.requests == []
    finally:
        terminal.set_offline(False)
