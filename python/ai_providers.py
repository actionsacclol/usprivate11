"""The six AI providers: one capability table, one error table, one transport.

Anthropic and OpenAI keep their SDK paths for the analysis call and the
Autopilot loop (ai_analysis / autopilot); everything else about them — the
free model listing, the key check, the error wording — is here, next to the
four providers this module carries calls for: OpenRouter, Gemini, Ollama and
LM Studio.

Why this is not one "OpenAI-compatible" client pointed at four base URLs
(ported from kryptrader's ai_providers, where the reasoning was earned):

  * **Gemini** speaks its own REST API. A bad Gemini key comes back as HTTP
    **400** with `reason: API_KEY_INVALID`, not 401 — a status-code mapper
    reads that as "bad request". Web search (Google Search grounding) returns
    its sources as `groundingMetadata`, which the compatibility layer drops.
    The key goes in the `x-goog-api-key` header, never in a URL query string
    that could end up in a logged request line.
  * **Ollama** is called on its native `/api/chat`, not its `/v1` shim,
    because only the native API takes `options.num_ctx` and `format` (a JSON
    schema). Ollama's default context is a few thousand tokens and it
    truncates silently from the FRONT of the conversation when a prompt
    overflows — which drops the system prompt, the one telling the model it
    may not state a number. We set the window explicitly and refuse a prompt
    that would not fit rather than let it be cut.
  * **OpenRouter and LM Studio** really are Chat Completions servers, so they
    share one transport. They still differ in auth, web search, cost
    reporting and in what each failure means.

Rules this module holds:

  * No URLs or hosts from callers. Every base URL is a constant here. Callers
    name a provider id and a model id; a model id is shape-checked before it
    travels, and Gemini's — which lands in the URL PATH — may not contain a
    slash at all.
  * Local means local: the two local providers are reached on 127.0.0.1 (not
    "localhost", which on Windows can resolve to ::1 first while Ollama
    listens on IPv4 only), with the environment's proxy settings ignored so a
    corporate proxy cannot swallow a loopback call.
  * Redirects are never followed. httpx strips `Authorization` on a
    cross-origin redirect but knows nothing about `x-goog-api-key`.
  * Model listings and key checks use free endpoints only.
  * No attribution headers (OpenRouter's `HTTP-Referer` / `X-Title` would put
    this app's usage on a public leaderboard).
  * Every cloud host is a `terminal._HOSTS` entry, so the offline switch
    reaches it, a 429 parks it, and the privacy panel counts it. The two local
    servers are listed too (the panel names every host), but offline mode does
    not block them: a call to 127.0.0.1 never leaves the machine.
  * A key is never logged and never returned. It arrives as an argument from
    ai_secrets and leaves only in a request header.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

import terminal as term

logger = logging.getLogger("krypt.ai.providers")


class AiError(Exception):
    """Carries a message already written for a user, not a stack trace."""


# ---------------------------------------------------------------------------
# Hosts. Hardcoded, and registered in terminal._HOSTS for the privacy panel.
# ---------------------------------------------------------------------------

ANTHROPIC_BASE = "https://api.anthropic.com"
OPENAI_BASE = "https://api.openai.com"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com"
OLLAMA_BASE = "http://127.0.0.1:11434"
LMSTUDIO_BASE = "http://127.0.0.1:1234"

# Tests inject an httpx.MockTransport here. Nothing else sets it.
_TRANSPORT: Optional[httpx.BaseTransport] = None

PROVIDERS = ("anthropic", "openai", "openrouter", "gemini", "ollama", "lmstudio")

# The four this module carries calls for. Anthropic and OpenAI calls stay on
# their SDK paths; they are only here for the key check and the model listing.
HTTP_PROVIDERS = ("openrouter", "gemini", "ollama", "lmstudio")

# terminal._HOSTS entries for the four new hosts. anthropic/openai are
# declared in terminal.py itself (ai_analysis's SDK calls run under that gate).
_NEW_HOSTS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "base": OPENROUTER_BASE, "host": "openrouter.ai",
        "purpose": "AI analysis / Autopilot via OpenRouter — public market text "
                   "and agent tool results (only if chosen)",
        "min_gap": 0.5, "concurrency": 1,
    },
    "gemini": {
        "base": GEMINI_BASE, "host": "generativelanguage.googleapis.com",
        "purpose": "AI analysis / Autopilot via Google Gemini — public market "
                   "text and agent tool results (only if chosen)",
        "min_gap": 0.5, "concurrency": 1,
    },
    "ollama": {
        "base": OLLAMA_BASE, "host": "127.0.0.1:11434",
        "purpose": "Ollama on this machine — nothing leaves it (only if chosen)",
        "min_gap": 0.0, "concurrency": 1,
    },
    "lmstudio": {
        "base": LMSTUDIO_BASE, "host": "127.0.0.1:1234",
        "purpose": "LM Studio on this machine — nothing leaves it (only if chosen)",
        "min_gap": 0.0, "concurrency": 1,
    },
}


def _register_hosts() -> None:
    """Declare the new hosts to the terminal's gate table. Idempotent, and done
    at import so the privacy panel lists them from the first time the AI
    settings are opened."""
    for k, spec in _NEW_HOSTS.items():
        if k not in term._HOSTS:
            term._HOSTS[k] = dict(spec)
        if k not in term._gates:
            term._gates[k] = term._Gate(k, float(spec["min_gap"]),
                                        int(spec.get("concurrency", 1)))


_register_hosts()


# ---------------------------------------------------------------------------
# Capability table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Caps:
    label: str
    local: bool
    needs_key: bool
    # Whether the analysis can search the web, and by what documented
    # mechanism. A provider with none says False rather than pretending.
    web_search: bool
    web_search_how: str
    # "all" when every model we would send can call tools; "per-model" when it
    # depends on the model and tool_support() has to look.
    tools: str
    tools_how: str
    # How the analysis's closed, number-free response schema is enforced on
    # this provider. Whatever it says, ai_analysis re-checks the reply against
    # the schema and scrubs it — the provider is the first line, not the only.
    schema: str
    # Where token counts come from. Autopilot's daily token budget is enforced
    # from these; when a response carries none, the run estimates from text
    # length and says so in its report.
    tokens: str
    # "published" = our own price table; "provider" = the provider reports the
    # charge itself; "none" = no grounded price, so cost is None; "free" =
    # runs on this machine, there is no bill.
    pricing: str
    # "listed" = whatever the free listing endpoint returns, any shape-valid
    # id accepted. (Polybot never hardcodes a catalogue: a stale one is a 404
    # the user cannot diagnose.)
    catalogue: str
    models_endpoint: str


CAPS: dict[str, Caps] = {
    "anthropic": Caps(
        label="Anthropic", local=False, needs_key=True,
        web_search=True, web_search_how="Anthropic's server-side web_search tool",
        tools="all", tools_how="every Claude model calls tools",
        schema="strict JSON schema (output_config.format)",
        tokens="reported by Anthropic", pricing="published", catalogue="listed",
        models_endpoint="GET api.anthropic.com/v1/models (free)"),
    "openai": Caps(
        label="OpenAI", local=False, needs_key=True,
        web_search=True, web_search_how="the Responses API web_search tool",
        tools="all", tools_how="every GPT chat model calls tools",
        schema="strict JSON schema (Responses text.format)",
        tokens="reported by OpenAI", pricing="none", catalogue="listed",
        models_endpoint="GET api.openai.com/v1/models (free)"),
    "openrouter": Caps(
        label="OpenRouter", local=False, needs_key=True,
        web_search=True,
        web_search_how="OpenRouter's documented `web` plugin; citations come back "
                       "as url_citation annotations. Billed by OpenRouter per search.",
        tools="per-model",
        tools_how="the model listing's supported_parameters must include \"tools\"",
        schema="strict JSON schema (response_format), routed only to endpoints "
               "that support it (provider.require_parameters)",
        tokens="reported by OpenRouter", pricing="provider", catalogue="listed",
        models_endpoint="GET openrouter.ai/api/v1/models (public) + /key (free)"),
    "gemini": Caps(
        label="Google Gemini", local=False, needs_key=True,
        web_search=True, web_search_how="Google Search grounding (the google_search tool)",
        tools="per-model",
        tools_how="Gemini-family text models call functions; Gemma, embedding, "
                  "image, TTS and live-audio models do not (by model family)",
        schema="responseSchema (no numeric field) with web search off; with "
               "search on, the app's own schema check only",
        tokens="reported by Google", pricing="none", catalogue="listed",
        models_endpoint="GET generativelanguage.googleapis.com/v1beta/models (free)"),
    "ollama": Caps(
        label="Ollama", local=True, needs_key=False,
        web_search=False, web_search_how="none: a local model has no search",
        tools="per-model",
        tools_how="/api/show must list \"tools\" in the model's capabilities",
        schema="JSON schema via Ollama's `format` (grammar-constrained)",
        tokens="counted by Ollama on this machine (prompt_eval_count / eval_count)",
        pricing="free", catalogue="listed",
        models_endpoint="GET 127.0.0.1:11434/api/tags (installed models)"),
    "lmstudio": Caps(
        label="LM Studio", local=True, needs_key=False,
        web_search=False, web_search_how="none: a local model has no search",
        tools="per-model",
        tools_how="any chat model (LM Studio wraps models without native tool "
                  "support in its own tool format); embedding models cannot",
        schema="JSON schema via response_format (grammar-constrained)",
        tokens="counted by LM Studio on this machine (usage block)",
        pricing="free", catalogue="listed",
        models_endpoint="GET 127.0.0.1:1234/api/v0/models (downloaded models)"),
}


def capabilities(provider: str) -> dict:
    c = CAPS[provider]
    return {"label": c.label, "local": c.local, "needsKey": c.needs_key,
            "webSearch": c.web_search, "webSearchHow": c.web_search_how,
            "tools": c.tools, "toolsHow": c.tools_how, "schema": c.schema,
            "tokens": c.tokens, "pricing": c.pricing, "catalogue": c.catalogue,
            "modelsEndpoint": c.models_endpoint}


# USD per million tokens, (input, output), where the provider PUBLISHES them.
# Only Anthropic's are here. A model missing from this table has no cost
# estimate — never a guessed one.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# model -> (prompt tokens the list price covers, input, output past that).
PRICING_LONG_PROMPT: dict[str, tuple[int, float, float]] = {
    "claude-haiku-5-5": (100_000, 0.50, 2.50),
}


def cost_usd(model: str, in_tok: Optional[int], out_tok: Optional[int]) -> Optional[float]:
    p = PRICING.get(model)
    long = PRICING_LONG_PROMPT.get(model)
    if long and in_tok is not None and in_tok > long[0]:
        p = long[1:]
    if not p or in_tok is None or out_tok is None:
        return None
    return round((in_tok * p[0] + out_tok * p[1]) / 1_000_000, 6)


# ---------------------------------------------------------------------------
# Error table
# ---------------------------------------------------------------------------
#
# Kinds, per provider. Every message names the fix, because "HTTP 400" is not
# something a trader can act on. {model} and {detail} are filled in.

_SETTINGS = "Settings → AI analysis"


def _cloud_errors(label: str) -> dict[str, str]:
    return {
        "no_key": f"Set your {label} API key in {_SETTINGS} to use {label}.",
        "auth": f"{label} rejected the key. Check it in {_SETTINGS}.",
        "permission": f"That {label} key is valid but not allowed to use {{model}}.",
        "quota": f"{label} says this key is out of quota. {{detail}}",
        "rate": f"{label} rate-limited this key. Wait a moment and try again.",
        "parked": f"{label} returned a rate limit recently, so it is parked for a "
                  f"few seconds rather than retried into a harder limit.",
        "model": f"{label} has no model called {{model}}. Pick another in "
                 f"{_SETTINGS} (Test connection lists the ones your key can use).",
        "no_model": f"No {label} model is selected. Pick one in {_SETTINGS} "
                    f"(Test connection lists the ones your key can use).",
        "down": f"Could not reach {label}. Check the connection and try again.",
        "offline": term.OFFLINE_REASON,
        "timeout": f"{label} did not answer in time. Try again, or turn web search off.",
        "server": f"{label} had a server error ({{status}}). Try again shortly.",
        "refused": f"{label}'s model declined to answer this one.",
        "no_tools": f"{{model}} on {label} cannot call tools, and Autopilot works "
                    f"only through tools. Pick a tool-capable model in {_SETTINGS}.",
        "no_schema": f"{{model}} on {label} can't guarantee the response format the "
                     f"analysis needs (structured output). Pick another model.",
        "bad_response": f"{label} sent back something that isn't a model reply.",
        "other": f"{label} returned {{status}}: {{detail}}",
    }


def _local_errors(label: str) -> dict[str, str]:
    return {
        "no_key": "",
        "offline": term.OFFLINE_REASON,
        "refused": "The model declined to answer this one.",
        "server": f"{label} had an error ({{status}}): {{detail}}",
        "bad_response": f"{label} sent back something that isn't a model reply.",
        "other": f"{label} returned {{status}}: {{detail}}",
        "auth": f"{label} refused the request ({{status}}): {{detail}}",
        "permission": f"{label} refused the request ({{status}}): {{detail}}",
        "quota": f"{label} refused the request ({{status}}): {{detail}}",
        "rate": f"{label} is busy. Try again in a moment.",
        "parked": f"{label} is busy. Try again in a moment.",
        "memory": f"{label} couldn't load {{model}}: {{detail}}",
        "no_schema": f"{{model}} in {label} couldn't produce the structured "
                     f"response the analysis needs: {{detail}}",
        "timeout": f"{label} didn't finish in time. A large model on CPU can be "
                   f"slower than that; try a smaller one.",
    }


ERRORS: dict[str, dict[str, str]] = {
    "anthropic": {
        **_cloud_errors("Anthropic"),
        "auth": f"That Anthropic API key was rejected. Check it in {_SETTINGS}.",
    },
    "openai": {
        **_cloud_errors("OpenAI"),
        "auth": f"That OpenAI API key was rejected. Check it in {_SETTINGS}.",
    },
    "openrouter": {
        **_cloud_errors("OpenRouter"),
        "auth": f"OpenRouter rejected the key. Check it in {_SETTINGS}, or make a "
                "new one at openrouter.ai/keys.",
        # 402 is OpenRouter's "no credits left", distinct from a rate limit.
        "quota": "Your OpenRouter account is out of credits (or this key hit its "
                 "credit limit). Add credits at openrouter.ai, or pick a model "
                 "ending in :free.",
        # 403 on OpenRouter is its moderation layer, not a key problem.
        "permission": "OpenRouter's moderation refused this request: {detail}",
    },
    "gemini": {
        **_cloud_errors("Google Gemini"),
        # Gemini sends a bad key as HTTP 400, reason API_KEY_INVALID.
        "auth": f"Google rejected that Gemini API key. Check it in {_SETTINGS}, "
                "or make a new one at aistudio.google.com/apikey.",
        # {detail} carries Google's own reason: without it a gated 2.5 model
        # read as "no model called X" while the list still showed X.
        "model": "Google Gemini won't serve {model} to this key. {detail} "
                 "New Gemini keys can't use the 2.5 models; pick a current one "
                 "(gemini-3.8-flash) in " + _SETTINGS + ".",
        "permission": "That Gemini key can't use {model}: the Generative "
                      "Language API may not be enabled for its Google Cloud "
                      "project. {detail}",
        # 429 on Gemini is RESOURCE_EXHAUSTED: a per-minute or per-day quota.
        "quota": "Gemini quota is used up for this key (free-tier limits are per "
                 "minute and per day). Wait, or enable billing on the key's project.",
        "rate": "Gemini quota is used up for this key (free-tier limits are per "
                "minute and per day). Wait, or enable billing on the key's project.",
        "region": "Google says the Gemini API isn't available for this key's "
                  "location: {detail}",
    },
    "ollama": {
        **_local_errors("Ollama"),
        "down": "Ollama isn't running: start it with `ollama serve` (or open the "
                "Ollama app), then try again.",
        "model": "{model} isn't pulled in Ollama: run `ollama pull {model}`.",
        "no_model": "No Ollama model is selected. Pick one in " + _SETTINGS + ".",
        "no_tools": "{model} can't call tools in Ollama, and Autopilot works only "
                    "through tools. Pick a model that can (llama3.1, qwen3, "
                    "mistral-nemo...) and `ollama pull` it.",
        "tools_unknown": "Ollama didn't say whether {model} can call tools (it "
                         "reports capabilities from v0.6 on). Update Ollama, then "
                         "try again.",
        "context": "This prompt (~{detail} tokens) is too big for the context "
                   "window Krypt asks Ollama for. Nothing was sent.",
    },
    "lmstudio": {
        **_local_errors("LM Studio"),
        "down": "LM Studio's local server isn't running: in LM Studio open the "
                "Developer tab and start the server (or run `lms server start`).",
        "model": "LM Studio has no model called {model}. Download or load it in "
                 "LM Studio (or `lms get {model}`), then pick it in " + _SETTINGS + ".",
        "no_model": "No LM Studio model is selected. Pick one in " + _SETTINGS + ".",
        "no_models": "LM Studio is running but has no chat model downloaded. "
                     "Get one in LM Studio's Discover tab (or `lms get qwen2.5-7b-instruct`).",
        "no_tools": "{model} is an embedding model; it can't chat or call tools. "
                    "Pick a chat model in " + _SETTINGS + ".",
        "context": "{detail} Reload the model in LM Studio with a larger context "
                   "length, or pick a model that has one.",
    },
}


def error(provider: str, kind: str, model: Optional[str] = None,
          detail: str = "", status: Any = "") -> AiError:
    table = ERRORS.get(provider) or ERRORS["openrouter"]
    tmpl = table.get(kind) or table.get("other") or "{detail}"
    msg = tmpl.format(model=model or "that model", detail=detail or "", status=status)
    # An empty {detail} mid-sentence leaves a double space behind.
    return AiError(re.sub(r" {2,}", " ", msg).strip())


def _first_line(s: str) -> str:
    s = (s or "").strip()
    return s.splitlines()[0][:200] if s else ""


def _detail(data: Any, text: str) -> str:
    """The human part of an error body, whichever way this provider spells it:
    {"error": {"message"}} (OpenAI-style, Gemini, Anthropic), {"error": "..."}
    (Ollama), or a one-element list of either."""
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        err = data.get("error")
        msg = None
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status")
        elif isinstance(err, str):
            msg = err
        else:
            msg = data.get("message")
        if msg:
            return _first_line(str(msg))
    return _first_line(text or "")


def _gemini_reasons(data: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(data, list) and data:
        data = data[0]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        if err.get("status"):
            out.add(str(err["status"]))
        for d in err.get("details") or []:
            if isinstance(d, dict) and d.get("reason"):
                out.add(str(d["reason"]))
    return out


def classify(provider: str, status: int, data: Any, detail: str) -> str:
    """HTTP failure -> an error kind from the table. Per provider, because the
    same status means different things on different services."""
    low = (detail or "").lower()
    if provider == "gemini":
        reasons = _gemini_reasons(data)
        if "API_KEY_INVALID" in reasons or "api key not valid" in low or "api key expired" in low:
            return "auth"
        if status == 400 and "location" in low:
            return "region"
    if provider == "ollama":
        if "does not support tools" in low:
            return "no_tools"
        if status == 404 or ("not found" in low and "pull" in low):
            return "model"
        if "memory" in low:
            return "memory"
    if provider == "lmstudio":
        if "context" in low and ("length" in low or "overflow" in low or "exceed" in low):
            return "context"
        if status == 404 or ("model" in low and ("not found" in low or "no models loaded" in low
                                                 or "does not exist" in low)):
            return "model"
        if "failed to load" in low or "memory" in low:
            return "memory"
    if provider == "openrouter":
        if status == 400 and "not a valid model" in low:
            return "model"
        # require_parameters with no endpoint that can honour response_format.
        if "requested parameters" in low or "no endpoints found that" in low:
            return "no_schema"
    if status == 401:
        return "auth"
    if status == 402:
        return "quota"
    if status == 403:
        return "permission"
    if status == 404:
        return "model"
    if status == 429:
        return "rate"
    if status in (408, 504, 524):
        return "timeout"
    if status >= 500:
        return "server"
    return "other"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def stat_host(url: Any) -> str:
    """The host, plus the port when it is not the scheme's default. Both local
    servers are 127.0.0.1, and "Ollama on :11434" and "LM Studio on :1234" are
    different disclosures."""
    u = httpx.URL(str(url))
    return f"{u.host}:{u.port}" if u.port else (u.host or "")


def _gate_pre(provider: str, model: Optional[str]) -> Optional[Any]:
    """The terminal gate's rules for one request, checked BEFORE it is made:
    offline mode (cloud hosts only — a loopback call never leaves the
    machine) and a 429 park. Returns the gate for the counters."""
    gate = term._gates.get(provider)
    if not CAPS[provider].local and term.is_offline():
        raise error(provider, "offline", model)
    if gate is not None and gate.parked():
        raise error(provider, "parked", model)
    return gate


def _gate_post(gate: Any, ok: bool, ms: float, status: Optional[int] = None) -> None:
    if gate is None:
        return
    try:
        gate.calls += 1
        if not ok:
            gate.errors += 1
        gate.note_latency(ms)
        gate.last = time.monotonic()
        if status == 429:
            gate.park()
    except Exception:  # noqa: BLE001 — counting must never break a call
        pass


def _http(provider: str, method: str, url: str, *, headers: Optional[dict] = None,
          body: Optional[dict] = None, params: Optional[dict] = None,
          timeout: float = 30.0, model: Optional[str] = None) -> Any:
    """One request, with every failure turned into a sentence for the user."""
    local = CAPS[provider].local
    gate = _gate_pre(provider, model)
    t0 = time.monotonic()
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=3.0 if local else 10.0),
            transport=_TRANSPORT,
            # A proxy configured for the internet must not swallow a loopback
            # call; for cloud hosts the user's proxy settings are honoured.
            trust_env=not local,
            follow_redirects=False,
        ) as client:
            r = client.request(method, url, headers=headers, json=body, params=params)
    except httpx.ConnectTimeout as e:
        # A local server that does not accept within 3s is, for the user's
        # purposes, not running. A cloud one is unreachable.
        _gate_post(gate, False, (time.monotonic() - t0) * 1000)
        raise error(provider, "down", model) from e
    except httpx.TimeoutException as e:
        _gate_post(gate, False, (time.monotonic() - t0) * 1000)
        raise error(provider, "timeout", model) from e
    except httpx.HTTPError as e:
        _gate_post(gate, False, (time.monotonic() - t0) * 1000)
        raise error(provider, "down", model) from e

    _gate_post(gate, r.status_code < 400, (time.monotonic() - t0) * 1000, r.status_code)
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400:
        det = _detail(data, r.text)
        raise error(provider, classify(provider, r.status_code, data, det), model,
                    det, r.status_code)
    if 300 <= r.status_code < 400:
        # Not followed, by design (see the module docstring).
        raise error(provider, "other", model, "unexpected redirect", r.status_code)
    if data is None:
        raise error(provider, "bad_response", model)
    # OpenRouter can answer 200 with an error object in the body.
    if isinstance(data, dict) and isinstance(data.get("error"), (dict, str)) \
            and not data.get("choices") and not data.get("candidates") \
            and not data.get("message") and not data.get("data") and not data.get("models"):
        code = data["error"].get("code") if isinstance(data["error"], dict) else None
        det = _detail(data, "")
        st = code if isinstance(code, int) else 400
        raise error(provider, classify(provider, st, data, det), model, det, st)
    return data


def _headers(provider: str, key: Optional[str]) -> dict:
    if provider == "anthropic":
        return {"x-api-key": key or "", "anthropic-version": "2023-06-01"}
    if provider == "gemini":
        return {"x-goog-api-key": key or ""}
    if provider in ("openai", "openrouter"):
        return {"Authorization": f"Bearer {key or ''}"}
    return {}


# ---------------------------------------------------------------------------
# Models: shape, defaults, listing
# ---------------------------------------------------------------------------

# Suggestions shown before a listing has run. A listing is the real catalogue;
# a model outside the suggestions is fine if it is shape-valid. OpenAI has
# none on purpose: its ids move faster than this file (see ai_analysis).
SUGGESTED: dict[str, tuple] = {
    "anthropic": ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
    "openai": (),
    "openrouter": (
        "google/gemini-2.5-flash",
        "anthropic/claude-sonnet-5.5",
        "openai/gpt-5",
        "deepseek/deepseek-chat-v3.1",
        "meta-llama/llama-3.3-70b-instruct",
    ),
    # Google limits the 2.5 models to keys that already used them, so the
    # default is a model a NEW key can use. A saved 2.5 id is left alone.
    "gemini": ("gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview"),
    "ollama": ("llama3.1:8b", "qwen3:8b", "mistral-nemo"),
    # LM Studio names models after whoever packaged them; the empty id means
    # "whichever chat model LM Studio has loaded", resolved at call time.
    "lmstudio": (),
}
DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-opus-5-5",
    "openai": "",
    "openrouter": "google/gemini-2.5-flash",
    "gemini": "gemini-3.8-flash",
    "ollama": "llama3.1:8b",
    "lmstudio": "",
}

# A model id is data in a JSON body for most of these, but Gemini's goes in
# the URL path, so it gets the strictest shape: no slash, no colon, lowercase.
_MODEL_RE = {
    "anthropic": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$"),
    "openai": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}$"),
    "openrouter": re.compile(r"^[A-Za-z0-9~][A-Za-z0-9._:/@+-]{0,159}$"),
    "gemini": re.compile(r"^[a-z0-9][a-z0-9.-]{0,99}$"),
    "ollama": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"),
    "lmstudio": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$"),
}


def valid_model(provider: str, model: Any) -> bool:
    m = str(model or "").strip()
    rx = _MODEL_RE.get(provider)
    if not rx or not m or ".." in m or "//" in m:
        return False
    return bool(rx.match(m))


def clean_model(provider: str, model: Any) -> str:
    """A model id we are willing to send, or the provider's default. A stale
    or hand-edited setting degrades to a working call instead of a 404."""
    m = str(model or "").strip()
    if provider == "gemini" and m.startswith("models/"):
        m = m[len("models/"):]
    return m if valid_model(provider, m) else DEFAULT_MODEL.get(provider, "")


@dataclass
class ModelInfo:
    id: str
    tools: Optional[bool] = None
    context: Optional[int] = None
    loaded: Optional[bool] = None
    kind: Optional[str] = None
    # The most the model supports, for display only. `context` is the window
    # a request actually gets; the guards read only that.
    max_context: Optional[int] = None

    def public(self) -> dict:
        return {"id": self.id, "tools": self.tools, "context": self.context,
                "maxContext": self.max_context, "loaded": self.loaded}


# provider -> (fetched_at, models). Listings are free but not instant, and
# Autopilot's preflight reads them before every run.
_LISTED: dict[str, tuple[float, list[ModelInfo]]] = {}
_LIST_TTL = 600.0


def cached_models(provider: str) -> list[ModelInfo]:
    hit = _LISTED.get(provider)
    return list(hit[1]) if hit else []


def models_for(provider: str) -> list[str]:
    """The suggestions plus whatever the last free listing returned. Never a
    network call: the status poll reads it."""
    base = list(SUGGESTED.get(provider) or ())
    listed = [m.id for m in cached_models(provider)]
    return listed + [m for m in base if m not in listed] if listed else base


def list_models(provider: str, key: Optional[str] = None, *,
                fresh: bool = False) -> list[ModelInfo]:
    """The provider's own catalogue, from a free endpoint."""
    hit = _LISTED.get(provider)
    if hit and not fresh and time.time() - hit[0] < _LIST_TTL:
        return list(hit[1])
    out = _fetch_models(provider, key)
    _LISTED[provider] = (time.time(), out)
    return list(out)


def _fetch_models(provider: str, key: Optional[str]) -> list[ModelInfo]:
    if CAPS[provider].needs_key and not key:
        raise error(provider, "no_key")
    if provider == "anthropic":
        data = _http(provider, "GET", f"{ANTHROPIC_BASE}/v1/models",
                     headers=_headers(provider, key), params={"limit": 100})
        return [ModelInfo(id=str(m.get("id")), tools=True)
                for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]
    if provider == "openai":
        data = _http(provider, "GET", f"{OPENAI_BASE}/v1/models",
                     headers=_headers(provider, key))
        return sorted((ModelInfo(id=str(m.get("id")), tools=True)
                       for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")),
                      key=lambda x: x.id)
    if provider == "openrouter":
        # Public: no key needed, so a listing can't fail on a bad key.
        data = _http(provider, "GET", f"{OPENROUTER_BASE}/models")
        out = []
        for m in data.get("data") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            params = m.get("supported_parameters")
            out.append(ModelInfo(
                id=str(m["id"]),
                tools=("tools" in params) if isinstance(params, list) else None,
                context=_int_or_none(m.get("context_length"))))
        return sorted(out, key=lambda x: x.id)
    if provider == "gemini":
        out, token = [], None
        for _ in range(5):
            params = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            data = _http(provider, "GET", f"{GEMINI_BASE}/v1beta/models",
                         headers=_headers(provider, key), params=params)
            for m in data.get("models") or []:
                if not isinstance(m, dict):
                    continue
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue
                mid = str(m.get("name") or "")
                mid = mid[len("models/"):] if mid.startswith("models/") else mid
                if valid_model("gemini", mid):
                    out.append(ModelInfo(id=mid, tools=_gemini_tools(mid),
                                         context=_int_or_none(m.get("inputTokenLimit"))))
            token = data.get("nextPageToken")
            if not token:
                break
        return out
    if provider == "ollama":
        data = _http(provider, "GET", f"{OLLAMA_BASE}/api/tags")
        out = []
        for m in data.get("models") or []:
            if isinstance(m, dict) and (m.get("model") or m.get("name")):
                fam = str(((m.get("details") or {}).get("family")) or "")
                out.append(ModelInfo(id=str(m.get("model") or m.get("name")),
                                     kind="embedding" if "bert" in fam else None))
        return out
    if provider == "lmstudio":
        try:
            data = _http(provider, "GET", f"{LMSTUDIO_BASE}/api/v0/models")
        except AiError:
            # Older LM Studio, or a build without the native REST API: the
            # OpenAI-compatible listing still names the models.
            data = _http(provider, "GET", f"{LMSTUDIO_BASE}/v1/models")
        out = []
        for m in data.get("data") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            kind = str(m.get("type") or "") or None
            is_embed = kind == "embeddings" or "embed" in str(m["id"]).lower()
            state = m.get("state")
            out.append(ModelInfo(
                id=str(m["id"]),
                tools=False if is_embed else True,
                # ONLY the window the model is loaded with; the architectural
                # max let prompts through that LM Studio then truncated.
                context=_int_or_none(m.get("loaded_context_length")),
                max_context=_int_or_none(m.get("max_context_length")),
                loaded=(state == "loaded") if state else None,
                kind="embedding" if is_embed else kind))
        return out
    raise AiError(f"Unknown AI provider: {provider}")


def _installed(provider: str, ids: list, model: str) -> bool:
    """Ollama treats an untagged name as `:latest`, and lists it that way."""
    if model in ids:
        return True
    return provider == "ollama" and ":" not in model and f"{model}:latest" in ids


def _int_or_none(v: Any) -> Optional[int]:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _gemini_tools(model: str) -> bool:
    """Function calling by model family. The listing does not say, so this is
    a family rule and is labelled as one in the capability table."""
    m = model.lower()
    if not m.startswith("gemini-"):
        return False            # gemma, learnlm, aqa, imagen...
    return not any(bad in m for bad in ("embedding", "image", "tts", "-live",
                                        "native-audio", "audio-dialog",
                                        "transcribe", "nano-banana"))


def _ollama_show(model: str) -> dict:
    return _http("ollama", "POST", f"{OLLAMA_BASE}/api/show",
                 body={"model": model}, model=model)


def resolve_model(provider: str, model: str) -> str:
    """The empty LM Studio id means "the chat model you have loaded". Resolved
    from its own listing; a local call that costs nothing."""
    if provider != "lmstudio" or model:
        return model
    models = [m for m in list_models("lmstudio") if m.kind != "embedding"]
    if not models:
        raise error("lmstudio", "no_models")
    loaded = [m for m in models if m.loaded]
    return (loaded or models)[0].id


def tool_support(provider: str, key: Optional[str], model: str) -> tuple[bool, str]:
    """(can this model call tools, why not). Asked BEFORE Autopilot starts, so
    a model that cannot use tools is refused up front with a reason rather
    than failing on its first turn with a run half-recorded."""
    if provider == "openai" and not model:
        return False, str(error("openai", "no_model"))
    if CAPS[provider].tools == "all":
        return True, ""
    try:
        model = resolve_model(provider, model)
        if provider == "ollama":
            info = _ollama_show(model)
            caps = info.get("capabilities")
            if not isinstance(caps, list):
                return False, str(error("ollama", "tools_unknown", model))
            if "tools" not in caps:
                return False, str(error("ollama", "no_tools", model))
            return True, ""
        if provider == "gemini":
            if not _gemini_tools(model):
                return False, str(error("gemini", "no_tools", model))
            return True, ""
        listed = {m.id: m for m in list_models(provider, key)}
        info = listed.get(model)
        if info is None:
            return False, str(error(provider, "model", model))
        if info.tools is False or (provider == "openrouter" and info.tools is None):
            return False, str(error(provider, "no_tools", model))
        return True, ""
    except AiError as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Key / connection check
# ---------------------------------------------------------------------------

def check(provider: str, key: Optional[str], model: Optional[str] = None) -> dict:
    """{ok, provider, message, models?, modelInfo?, tools?, webSearch?} using
    free endpoints only.

    `ok` means "an analysis on this provider with this model would get as far
    as the model": key accepted (cloud), server up and model present (local).
    Never a billed completion — a test button that costs money is one people
    stop pressing."""
    caps = CAPS[provider]
    if caps.needs_key and not key:
        return {"ok": False, "message": str(error(provider, "no_key")),
                "provider": provider}
    try:
        if provider == "openrouter":
            info = _http(provider, "GET", f"{OPENROUTER_BASE}/key",
                         headers=_headers(provider, key))
            d = info.get("data") if isinstance(info, dict) else None
            remaining = d.get("limit_remaining") if isinstance(d, dict) else None
            if isinstance(remaining, (int, float)) and remaining <= 0:
                return {"ok": False, "provider": provider,
                        "message": str(error(provider, "quota"))}
        models = list_models(provider, key, fresh=True)
    except AiError as e:
        return {"ok": False, "message": str(e), "provider": provider}

    ids = [m.id for m in models]
    sel = model if model is not None else DEFAULT_MODEL.get(provider, "")
    out: dict = {"ok": True, "provider": provider, "models": ids,
                 "modelInfo": [m.public() for m in models],
                 "webSearch": caps.web_search}

    if caps.local:
        chat = [m for m in models if m.kind != "embedding"]
        if not chat:
            out["ok"] = False
            out["message"] = (str(error("lmstudio", "no_models")) if provider == "lmstudio"
                              else "Ollama is running but has no models pulled: run "
                                   f"`ollama pull {DEFAULT_MODEL['ollama']}`.")
            return out
        if sel and not _installed(provider, ids, sel):
            out["ok"] = False
            out["message"] = (f"{caps.label} is running, but " + str(error(provider, "model", sel)))
            return out
        try:
            target = sel or resolve_model(provider, sel)
        except AiError as e:
            out["ok"] = False
            out["message"] = str(e)
            return out
        ok_tools, why = tool_support(provider, key, target)
        out["tools"] = ok_tools
        out["message"] = (f"{caps.label} is running with {len(chat)} model(s). "
                          f"{target}: " + ("can call tools, so Autopilot can use it."
                                           if ok_tools else why))
        return out

    if not sel:
        # OpenAI ships with no model on purpose; the key is fine, the setting
        # is not finished.
        out["ok"] = False
        out["reason"] = "model"
        out["message"] = (f"{caps.label} accepted the key. {len(ids)} model(s) "
                          f"available — pick one in {_SETTINGS}.")
        return out
    if ids and sel not in ids:
        # `ok` means the call would get as far as the model, and here it would not.
        out["ok"] = False
        out["reason"] = "model"     # the key is fine; the health row says so
        out["message"] = (f"{caps.label} accepted the key, but {sel} isn't among the "
                          f"{len(ids)} model(s) it can use. Pick one from the list "
                          f"in {_SETTINGS}.")
        return out
    note = ""
    if provider == "gemini" and sel.startswith("gemini-2.5"):
        note = (f" Note: Google serves the 2.5 models only to keys that used them "
                f"before; if the analysis says {sel} isn't available, pick "
                f"{DEFAULT_MODEL['gemini']}.")
    if caps.tools == "per-model":
        ok_tools, _why = tool_support(provider, key, sel)
        out["tools"] = ok_tools
        note += (" It can call tools, so Autopilot can use it." if ok_tools
                 else f" {sel} can't call tools, so Autopilot won't run on it.")
    else:
        out["tools"] = True
    out["message"] = f"{caps.label} accepted the key. {len(ids)} model(s) available.{note}"
    return out


# ---------------------------------------------------------------------------
# One-shot analysis call
# ---------------------------------------------------------------------------

_MAX_TOKENS = 4000
# Gemini 2.5+ spends "thinking" tokens out of maxOutputTokens; 4k of thought
# can leave nothing for the JSON. The ceiling is a ceiling, not a charge.
_GEMINI_MAX_TOKENS = 16384
# Ollama: the window we ask for, and the reply budget inside it. A prompt that
# does not fit is refused here, because Ollama would otherwise drop the FRONT
# of the conversation — the system prompt — without saying so.
_OLLAMA_CTX = 16384
_OLLAMA_PREDICT = 6144
_OLLAMA_AGENT_CTX = 32768


def est_tokens(obj: Any) -> int:
    """A deliberately pessimistic token estimate (~3.5 chars/token) for the
    two places a count must exist and the provider did not give one: the
    context guard, and an Autopilot turn with no usage block."""
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, default=str)
    return int(len(s) / 3.5) + 1


def _chat_endpoint(provider: str, key: Optional[str]) -> tuple[str, dict]:
    if provider == "openrouter":
        return f"{OPENROUTER_BASE}/chat/completions", _headers(provider, key)
    if provider == "lmstudio":
        return f"{LMSTUDIO_BASE}/v1/chat/completions", {}
    raise AiError(f"{provider} is not a Chat Completions provider")


# A listing older than this is re-read before the guard trusts it: models get
# loaded, unloaded and reloaded with a new context in LM Studio's UI.
_LMS_FRESH_SEC = 30.0
# What to assume when LM Studio won't say what window the model gets. Its
# default load context is commonly this small, and guessing high is the
# failure the guard exists to prevent.
_LMS_ASSUMED_CTX = 4096


def _lmstudio_window(model: str) -> tuple[Optional[int], str]:
    """(loaded context, why it is unknown). Re-lists when the cached entry is
    stale, missing, or not marked loaded."""
    def find() -> tuple[Optional[ModelInfo], Optional[float]]:
        hit = _LISTED.get("lmstudio")
        if not hit:
            return None, None
        return next((m for m in hit[1] if m.id == model), None), time.time() - hit[0]

    info, age = find()
    if (info is None or info.loaded is not True or info.context is None
            or age is None or age > _LMS_FRESH_SEC):
        try:
            list_models("lmstudio", fresh=True)
        except AiError:
            pass
        info, age = find()
    if info is not None and info.loaded and info.context:
        return info.context, ""
    return None, ("not_loaded" if info is not None and info.loaded is False else "unknown")


def _lmstudio_problem(model: str, need: int) -> Optional[str]:
    """The `context` error's detail when `need` tokens would not fit the
    window LM Studio has (or may have) for `model`, else None."""
    ctx, why = _lmstudio_window(model)
    if ctx is not None:
        if need > ctx:
            return (f"LM Studio has {model} loaded with a {ctx:,}-token context; this "
                    f"prompt needs ~{need:,} with room for the reply.")
        return None
    if need <= _LMS_ASSUMED_CTX:
        return None
    if why == "not_loaded":
        return (f"{model} isn't loaded in LM Studio, so the context it would be "
                f"loaded with is unknown (LM Studio's default is often "
                f"{_LMS_ASSUMED_CTX:,} tokens); this prompt needs ~{need:,}. Load it "
                f"in LM Studio first.")
    return (f"LM Studio didn't report the context {model} is loaded with (its "
            f"/api/v0 listing does, from LM Studio 0.3.6; update if yours is "
            f"older); this prompt needs ~{need:,}, more than the "
            f"{_LMS_ASSUMED_CTX:,} it may have.")


def _lmstudio_guard(model: str, tokens: int, reply: int) -> None:
    why = _lmstudio_problem(model, tokens + reply)
    if why:
        raise error("lmstudio", "context", model, why)


def _openrouter_cost(usage: Any) -> Optional[float]:
    """OpenRouter reports its own charge for the call, in USD credits. Absent,
    the cost is None, not 0."""
    if isinstance(usage, dict):
        c = usage.get("cost")
        if isinstance(c, (int, float)) and c >= 0:
            return round(float(c), 6)
    return None


def call(provider: str, key: Optional[str], model: str, system: str, prompt: str,
         web_search: bool, timeout: float, *, schema: Optional[dict] = None,
         schema_name: str = "response") -> dict:
    """One analysis. Returns {text, model, citations, inputTokens,
    outputTokens, costUsd, webSearchUsed, schemaEnforced}.

    `schema`, when given, is asked of the provider as a structured-output
    constraint by whatever mechanism it documents (CAPS[p].schema).
    `schemaEnforced` says whether it was: the caller re-checks the reply
    against the schema either way, because "the provider promised" is the
    first line of a guarantee, not the whole of it."""
    caps = CAPS[provider]
    if caps.needs_key and not key:
        raise error(provider, "no_key")
    web = bool(web_search and caps.web_search)
    model = resolve_model(provider, model)
    if not model:
        raise error(provider, "no_model")
    if provider in ("openrouter", "lmstudio"):
        out = _call_chat(provider, key, model, system, prompt, web, timeout,
                         schema, schema_name)
    elif provider == "ollama":
        out = _call_ollama(model, system, prompt, timeout, schema)
    elif provider == "gemini":
        out = _call_gemini(key, model, system, prompt, web, timeout, schema)
    else:
        raise AiError(f"{provider} is not called through this module")
    out["model"] = model
    return out


def _call_chat(provider: str, key: Optional[str], model: str, system: str, prompt: str,
               web: bool, timeout: float, schema: Optional[dict],
               schema_name: str) -> dict:
    url, headers = _chat_endpoint(provider, key)
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "max_tokens": _MAX_TOKENS,
    }
    if schema is not None:
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": schema_name, "strict": True, "schema": schema}}
    if provider == "openrouter":
        body["usage"] = {"include": True}
        if schema is not None:
            # Route only to an upstream that honours response_format; one
            # that would silently ignore it is never chosen.
            body["provider"] = {"require_parameters": True}
        if web:
            body["plugins"] = [{"id": "web", "max_results": 5}]
    if provider == "lmstudio":
        _lmstudio_guard(model, est_tokens(system + prompt), _MAX_TOKENS)
    data = _http(provider, "POST", url, headers=headers, body=body,
                 timeout=timeout, model=model)
    choice = ((data.get("choices") or [None])[0]) or {}
    if not isinstance(choice, dict):
        raise error(provider, "bad_response", model)
    if isinstance(choice.get("error"), dict):
        det = _detail(choice, "")
        raise error(provider, classify(provider, 400, choice, det), model, det, 400)
    msg = choice.get("message") or {}
    if choice.get("finish_reason") == "content_filter" and not msg.get("content"):
        raise error(provider, "refused", model)
    citations = []
    for ann in msg.get("annotations") or []:
        if isinstance(ann, dict) and ann.get("type") == "url_citation":
            uc = ann.get("url_citation") or ann
            url_ = uc.get("url") if isinstance(uc, dict) else None
            if url_:
                citations.append({"title": str(uc.get("title") or url_), "url": str(url_)})
    usage = data.get("usage") or {}
    content = msg.get("content")
    if isinstance(content, list):     # a few servers send content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return {
        "text": str(content or "").strip(),
        "citations": citations,
        "inputTokens": _int_or_none(usage.get("prompt_tokens")),
        "outputTokens": _int_or_none(usage.get("completion_tokens")),
        "costUsd": _openrouter_cost(usage) if provider == "openrouter" else 0.0,
        "webSearchUsed": web,
        "schemaEnforced": schema is not None,
    }


def _call_ollama(model: str, system: str, prompt: str, timeout: float,
                 schema: Optional[dict]) -> dict:
    need = est_tokens(system + prompt)
    if need + _OLLAMA_PREDICT > _OLLAMA_CTX:
        raise error("ollama", "context", model, f"{need:,}")
    body: dict = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": _OLLAMA_CTX, "num_predict": _OLLAMA_PREDICT},
    }
    if schema is not None:
        # Ollama >= 0.5 constrains generation to this JSON schema.
        body["format"] = schema
    data = _http("ollama", "POST", f"{OLLAMA_BASE}/api/chat", body=body,
                 timeout=timeout, model=model)
    msg = data.get("message") or {}
    return {
        "text": str(msg.get("content") or "").strip(),
        "citations": [],
        "inputTokens": _int_or_none(data.get("prompt_eval_count")),
        "outputTokens": _int_or_none(data.get("eval_count")),
        "costUsd": 0.0,
        "webSearchUsed": False,
        "schemaEnforced": schema is not None,
    }


_GEMINI_REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION",
                    "IMAGE_SAFETY"}


def _gemini_url(model: str) -> str:
    # The model is in the PATH. clean_model already shape-checked it; this is
    # the second check, at the one place it matters.
    if not valid_model("gemini", model):
        raise error("gemini", "model", model)
    return f"{GEMINI_BASE}/v1beta/models/{model}:generateContent"


def _gemini_text(cand: dict) -> str:
    parts = ((cand.get("content") or {}).get("parts")) or []
    return "".join(p.get("text", "") for p in parts
                   if isinstance(p, dict) and not p.get("thought")).strip()


def _gemini_usage(data: dict) -> tuple[Optional[int], Optional[int]]:
    u = data.get("usageMetadata") or {}
    i = _int_or_none(u.get("promptTokenCount"))
    o = _int_or_none(u.get("candidatesTokenCount"))
    t = _int_or_none(u.get("thoughtsTokenCount"))
    if o is not None or t is not None:
        o = (o or 0) + (t or 0)          # thinking is billed as output
    return i, o


def _call_gemini(key: Optional[str], model: str, system: str, prompt: str,
                 web: bool, timeout: float, schema: Optional[dict]) -> dict:
    gen: dict = {"maxOutputTokens": _GEMINI_MAX_TOKENS}
    enforced = False
    if schema is not None and not web:
        # Controlled generation. Not combined with the search tool: Gemini
        # models before 3 reject a responseSchema alongside tools, and an
        # analysis that 400s whenever search is on is worse than one whose
        # schema the app checks itself (which it does either way).
        rs = gemini_schema(schema)
        if isinstance(rs.get("properties"), dict):
            # Gemini orders properties alphabetically unless told; the schema's
            # order is "argue first, conclude last" and that order matters.
            rs["propertyOrdering"] = list(rs["properties"])
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = rs
        enforced = True
    body: dict = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen,
    }
    if web:
        body["tools"] = [{"google_search": {}}]
    data = _http("gemini", "POST", _gemini_url(model), headers=_headers("gemini", key),
                 body=body, timeout=timeout, model=model)
    block = (data.get("promptFeedback") or {}).get("blockReason")
    cands = data.get("candidates") or []
    cand = cands[0] if cands and isinstance(cands[0], dict) else {}
    text = _gemini_text(cand)
    if block or (not text and cand.get("finishReason") in _GEMINI_REFUSALS):
        raise AiError(f"Gemini declined to analyse this market "
                      f"({block or cand.get('finishReason')}).")
    citations = []
    for ch in ((cand.get("groundingMetadata") or {}).get("groundingChunks")) or []:
        web_ = ch.get("web") if isinstance(ch, dict) else None
        if isinstance(web_, dict) and web_.get("uri"):
            citations.append({"title": str(web_.get("title") or web_["uri"]),
                              "url": str(web_["uri"])})
    i, o = _gemini_usage(data)
    return {"text": text, "citations": citations, "inputTokens": i, "outputTokens": o,
            "costUsd": None, "webSearchUsed": web, "schemaEnforced": enforced}


# ---------------------------------------------------------------------------
# Tool conversations, for Autopilot
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Turn:
    text: str = ""
    calls: list = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Optional[float] = None
    refusal: bool = False
    # True when the provider sent no usage and the counts are our estimate.
    estimated: bool = False
    raw: Any = None
    # A sentence when the turn FAILED even though the HTTP call succeeded (an
    # error inside a 200, a reply cut off before it said anything). Carried
    # rather than raised so the loop counts the tokens it cost first: a failed
    # generation is still billed against the daily budget.
    failed: Optional[str] = None


def _args(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    try:
        parsed = json.loads(v or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class ToolChat:
    """One Autopilot conversation on one provider. send() is blocking; the
    caller runs it off the event loop."""

    provider = ""

    def __init__(self, key: Optional[str], model: str, system: str,
                 tools: list[dict], first_user: str):
        self.key = key
        self.model = model
        self.system = system
        self.tools = tools
        self.first_user = first_user

    def send(self) -> Turn:  # pragma: no cover - abstract
        raise NotImplementedError

    def add_results(self, turn: Turn, results: list[tuple]) -> None:  # pragma: no cover
        raise NotImplementedError

    def request_size(self) -> int:  # pragma: no cover
        raise NotImplementedError

    def context_problem(self) -> Optional[str]:
        """A sentence when the next request would not fit the model's window,
        else None. Checked before every turn: the local servers truncate
        rather than refuse, and what they cut first is the system prompt."""
        return None

    def _usage(self, i: Optional[int], o: Optional[int], reply: Any) -> tuple[int, int, bool]:
        if i is None or o is None:
            return (i if i is not None else self.request_size(),
                    o if o is not None else est_tokens(reply), True)
        return i, o, False


def _fn_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description") or "",
        "parameters": t.get("inputSchema") or {"type": "object", "properties": {}}}}
        for t in tools]


class _ChatCompletionsToolChat(ToolChat):
    def __init__(self, provider: str, *a, **kw):
        super().__init__(*a, **kw)
        self.provider = provider
        self.url, self.headers = _chat_endpoint(provider, self.key)
        self.messages: list = [{"role": "system", "content": self.system},
                               {"role": "user", "content": self.first_user}]
        self.fn_tools = _fn_tools(self.tools)

    def _body(self) -> dict:
        body = {"model": self.model, "messages": self.messages, "tools": self.fn_tools,
                "max_tokens": _MAX_TOKENS}
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
        return body

    def request_size(self) -> int:
        return est_tokens(self._body())

    def context_problem(self) -> Optional[str]:
        need = self.request_size() + _MAX_TOKENS
        if self.provider == "lmstudio":
            why = _lmstudio_problem(self.model, need)
            return str(error("lmstudio", "context", self.model, why)) if why else None
        ctx = next((m.context for m in cached_models("openrouter")
                    if m.id == self.model), None)
        if ctx and need > ctx:
            return (f"The conversation (~{need:,} tokens) outgrew {self.model}'s "
                    f"{ctx:,}-token context window.")
        return None

    def send(self) -> Turn:
        data = _http(self.provider, "POST", self.url, headers=self.headers,
                     body=self._body(), timeout=120.0, model=self.model)
        choice = ((data.get("choices") or [None])[0]) or {}
        if not isinstance(choice, dict):
            raise error(self.provider, "bad_response", self.model)
        msg = choice.get("message") or {}
        if not isinstance(msg, dict):
            msg = {}
        calls = []
        for n, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            if fn.get("name"):
                calls.append(ToolCall(id=str(tc.get("id") or f"call_{n}"),
                                      name=str(fn["name"]), args=_args(fn.get("arguments"))))
        usage = data.get("usage") or {}
        i, o, est = self._usage(_int_or_none(usage.get("prompt_tokens")),
                                _int_or_none(usage.get("completion_tokens")), msg)
        text = str(msg.get("content") or "").strip()
        failed = None
        if isinstance(choice.get("error"), dict) or choice.get("finish_reason") == "error":
            # OpenRouter reports an upstream failure mid-generation INSIDE a
            # 200. Read as "no tool calls" it would record the run "ok" with a
            # blank report.
            det = _detail(choice, "") or "the model stopped with an error"
            failed = str(error(self.provider, classify(self.provider, 400, choice, det),
                               self.model, det, 400))
        elif choice.get("finish_reason") == "length" and not calls and not text:
            failed = (f"{CAPS[self.provider].label}: {self.model} ran out of output "
                      f"tokens before replying. Try again, or pick a model with a "
                      f"larger output limit.")
        return Turn(
            text=text, calls=[] if failed else calls,
            input_tokens=i, output_tokens=o, estimated=est, raw=msg,
            cost=(_openrouter_cost(usage) if self.provider == "openrouter" else 0.0),
            refusal=(choice.get("finish_reason") == "content_filter" and not calls),
            failed=failed)

    def add_results(self, turn: Turn, results: list[tuple]) -> None:
        msg = dict(turn.raw or {})
        keep = {"role": "assistant", "content": msg.get("content") or "",
                "tool_calls": msg.get("tool_calls") or []}
        # Some reasoning models on OpenRouter need their reasoning handed back
        # with the calls it produced.
        if self.provider == "openrouter" and msg.get("reasoning_details"):
            keep["reasoning_details"] = msg["reasoning_details"]
        self.messages.append(keep)
        for call_, text, _is_err in results:
            self.messages.append({"role": "tool", "tool_call_id": call_.id, "content": text})


class _OllamaToolChat(ToolChat):
    provider = "ollama"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.messages: list = [{"role": "system", "content": self.system},
                               {"role": "user", "content": self.first_user}]
        self.fn_tools = _fn_tools(self.tools)

    def _body(self) -> dict:
        return {"model": self.model, "messages": self.messages, "tools": self.fn_tools,
                "stream": False,
                "options": {"num_ctx": _OLLAMA_AGENT_CTX, "num_predict": _MAX_TOKENS}}

    def request_size(self) -> int:
        return est_tokens(self._body())

    def context_problem(self) -> Optional[str]:
        need = self.request_size() + _MAX_TOKENS
        if need > _OLLAMA_AGENT_CTX:
            return (f"The conversation (~{need:,} tokens) outgrew the "
                    f"{_OLLAMA_AGENT_CTX:,}-token window Krypt asks Ollama for. "
                    f"Ollama would silently drop the start of it, instructions "
                    f"included, so the run stopped here.")
        return None

    def send(self) -> Turn:
        data = _http("ollama", "POST", f"{OLLAMA_BASE}/api/chat", body=self._body(),
                     timeout=180.0, model=self.model)
        msg = data.get("message") or {}
        calls = []
        for n, tc in enumerate(msg.get("tool_calls") or []):
            fn = (tc or {}).get("function") or {}
            if fn.get("name"):
                calls.append(ToolCall(id=str(tc.get("id") or f"call_{n}"),
                                      name=str(fn["name"]), args=_args(fn.get("arguments"))))
        i, o, est = self._usage(_int_or_none(data.get("prompt_eval_count")),
                                _int_or_none(data.get("eval_count")), msg)
        return Turn(text=str(msg.get("content") or "").strip(), calls=calls,
                    input_tokens=i, output_tokens=o, estimated=est, cost=0.0, raw=msg)

    def add_results(self, turn: Turn, results: list[tuple]) -> None:
        msg = dict(turn.raw or {})
        msg["role"] = "assistant"
        self.messages.append(msg)
        for call_, text, _is_err in results:
            self.messages.append({"role": "tool", "content": text, "tool_name": call_.name})


# JSON Schema keys Gemini's schema (an OpenAPI subset) accepts. Anything else —
# additionalProperties, $schema, default — is a 400 for the whole request, so
# it is dropped rather than sent.
_GEMINI_SCHEMA_KEYS = {"type", "format", "description", "nullable", "enum", "properties",
                       "required", "items", "minItems", "maxItems", "minimum", "maximum",
                       "anyOf", "title", "minLength", "maxLength", "pattern"}


_JSON_ARG_NOTE = " Send this as a JSON object encoded in a string, e.g. \"{\\\"key\\\": 1}\"."


def gemini_schema(s: Any, _path: tuple = (), _json_paths: Optional[list] = None) -> Any:
    """JSON Schema -> the OpenAPI subset Gemini's `parameters`/`responseSchema`
    accept.

    An open-ended object ({"type": "object"} with no properties — update_config's
    `changes`) is a 400 for the WHOLE request on Gemini. Such a node is
    declared as a STRING holding JSON instead, its path recorded in
    `_json_paths`, and the Gemini tool chat decodes it back to an object
    before the tool sees it — so the handlers and their validation are
    unchanged."""
    if not isinstance(s, dict):
        return s
    out: dict = {}
    for k, v in s.items():
        if k not in _GEMINI_SCHEMA_KEYS:
            continue
        if k == "type" and isinstance(v, list):
            kinds = [t for t in v if t != "null"]
            out["type"] = kinds[0] if kinds else "string"
            if "null" in v:
                out["nullable"] = True
        elif k == "properties" and isinstance(v, dict):
            out[k] = {pk: gemini_schema(pv, _path + (pk,), _json_paths)
                      for pk, pv in v.items()}
        elif k == "items":
            out[k] = gemini_schema(v, _path + ("[]",), _json_paths)
        elif k == "anyOf" and isinstance(v, list):
            out[k] = [gemini_schema(x) for x in v]
        elif k == "enum":
            # Gemini takes string enums only.
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = v
        else:
            out[k] = v
    if out.get("type") == "object" and "required" in out and "properties" in out:
        out["required"] = [r for r in out["required"] if r in out["properties"]]
    if _path and out.get("type") == "object" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)
        out["type"] = "string"
        out["description"] = (str(out.get("description") or "").rstrip()
                              + _JSON_ARG_NOTE).strip()
        if _json_paths is not None:
            _json_paths.append(_path)
    return out


def _decode_json_args(args: Any, path: tuple) -> None:
    """Undo gemini_schema's object-as-string at `path`, in place. A string
    that is not a JSON object is left as it is, for the tool's own validation
    to refuse in its own words."""
    if not path:
        return
    head, rest = path[0], path[1:]
    if head == "[]":
        if isinstance(args, list):
            for i, item in enumerate(args):
                if rest:
                    _decode_json_args(item, rest)
                elif isinstance(item, str):
                    args[i] = _json_object_or(item)
        return
    if not isinstance(args, dict) or head not in args:
        return
    if rest:
        _decode_json_args(args[head], rest)
    elif isinstance(args[head], str):
        args[head] = _json_object_or(args[head])


def _json_object_or(s: str) -> Any:
    try:
        v = json.loads(s)
    except (TypeError, ValueError):
        return s
    return v if isinstance(v, dict) else s


class _GeminiToolChat(ToolChat):
    provider = "gemini"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.contents: list = [{"role": "user", "parts": [{"text": self.first_user}]}]
        decls = []
        # tool name -> argument paths declared as JSON-in-a-string.
        self.json_args: dict[str, list] = {}
        for t in self.tools:
            d = {"name": t["name"], "description": t.get("description") or ""}
            paths: list = []
            params = gemini_schema(t.get("inputSchema") or {}, (), paths)
            if paths:
                self.json_args[t["name"]] = paths
            # An object with no properties is a 400 on Gemini; a tool that
            # takes no arguments simply declares none.
            if params.get("properties"):
                d["parameters"] = params
            decls.append(d)
        self.fn_tools = [{"functionDeclarations": decls}] if decls else []

    def _body(self) -> dict:
        body = {"systemInstruction": {"parts": [{"text": self.system}]},
                "contents": self.contents,
                "generationConfig": {"maxOutputTokens": _GEMINI_MAX_TOKENS}}
        if self.fn_tools:
            body["tools"] = self.fn_tools
        return body

    def request_size(self) -> int:
        return est_tokens(self._body())

    def send(self) -> Turn:
        data = _http("gemini", "POST", _gemini_url(self.model),
                     headers=_headers("gemini", self.key), body=self._body(),
                     timeout=120.0, model=self.model)
        cands = data.get("candidates") or []
        cand = cands[0] if cands and isinstance(cands[0], dict) else {}
        content = cand.get("content") or {"role": "model", "parts": []}
        calls = []
        for n, p in enumerate(content.get("parts") or []):
            fc = p.get("functionCall") if isinstance(p, dict) else None
            if isinstance(fc, dict) and fc.get("name"):
                # A copy: the part itself goes back to Gemini untouched (see
                # add_results), so decoding must not rewrite it.
                args = copy.deepcopy(_args(fc.get("args")))
                for path in self.json_args.get(str(fc["name"]), ()):
                    _decode_json_args(args, path)
                calls.append(ToolCall(id=str(fc.get("id") or f"call_{n}"),
                                      name=str(fc["name"]), args=args))
        i, o = _gemini_usage(data)
        i, o, est = self._usage(i, o, content)
        blocked = bool((data.get("promptFeedback") or {}).get("blockReason")) or (
            cand.get("finishReason") in _GEMINI_REFUSALS and not calls)
        return Turn(text=_gemini_text(cand), calls=calls, input_tokens=i,
                    output_tokens=o, estimated=est, cost=None, refusal=blocked,
                    raw=content)

    def add_results(self, turn: Turn, results: list[tuple]) -> None:
        # The model's content goes back exactly as received: Gemini 2.5+/3
        # attach thought signatures to function-call parts and reject a
        # follow-up that drops them.
        content = dict(turn.raw or {})
        content["role"] = "model"
        self.contents.append(content)
        parts = []
        for call_, text, is_err in results:
            resp = {"error": text} if is_err else {"result": text}
            fr: dict = {"name": call_.name, "response": resp}
            if call_.id and not call_.id.startswith("call_"):
                fr["id"] = call_.id
            parts.append({"functionResponse": fr})
        self.contents.append({"role": "user", "parts": parts})


def open_tool_chat(provider: str, key: Optional[str], model: str, system: str,
                   tools: list[dict], first_user: str) -> ToolChat:
    model = resolve_model(provider, model)
    if provider in ("openrouter", "lmstudio"):
        return _ChatCompletionsToolChat(provider, key, model, system, tools, first_user)
    if provider == "ollama":
        return _OllamaToolChat(key, model, system, tools, first_user)
    if provider == "gemini":
        return _GeminiToolChat(key, model, system, tools, first_user)
    raise AiError(f"{provider} has no tool loop here")
