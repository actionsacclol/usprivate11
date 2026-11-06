"""AI market analysis — a language model reading a market, out loud.

WHAT THIS IS FOR. The terminal already tells you what a market IS: the price,
the book, the tape, who resolves it and on what source. What it cannot do is
read three paragraphs of resolution criteria and tell you the trap in the
wording, or name the two events between now and close that actually decide
it. That is reading comprehension over text, which is the one thing a
language model is unambiguously good at. So that is all this asks it to do.

THE RULE THAT SHAPES EVERYTHING BELOW: NO NUMBER.

This app refuses to print confident figures it cannot source. `cross_venue`
withholds the spread entirely below its confidence floor, because a
fictional edge is exactly the sort of number somebody trades on. A language
model will cheerfully produce "I estimate 68%" for any question you put to
it, with no calibration behind it, no backtest, and a fluency that reads
like authority. That number would be the least trustworthy figure anywhere
in this application and it would look like the most trustworthy.

So the model is not asked for one, and — more to the point — it is not GIVEN
ANYWHERE TO PUT ONE. The response schema has no numeric field. Not a
probability, not a fair value, not an edge. `additionalProperties: false`
means the provider's own structured-output enforcement rejects one before it
ever reaches us. That is a structural guarantee rather than a prompt
instruction, for the same reason `_HOSTS` is a fixed table rather than a URL
validator: the structural version cannot be talked out of it.

What the user gets instead is a LEAN from a four-word vocabulary — the
market looks cheap, rich, about right, or the model has no view — with the
reasoning that produced it. A direction and an argument, which a person can
weigh, rather than a decimal, which a person tends to just believe.

Free text is still free text, so a model can smuggle "roughly 70% likely"
into a sentence. `_strip_numeric_estimates` removes that sentence and the
panel reports how many it removed. Factual figures survive — "CPI came in at
2.4%", "resolves above $110,000" — because those are the market's numbers,
not the model's guess.

WHAT LEAVES THE MACHINE. Public market data only: the question, the rules
text, the current price, volume, close time and resolver. Never the wallet,
never a position, never a cost basis, never an order. `build_payload` is the
only thing that constructs the outbound body and it takes a market dict, not
an account. Every provider host is declared in `terminal._HOSTS` (the four
beyond Anthropic/OpenAI by `ai_providers` at import), so the offline switch
reaches the cloud ones, the 429 park applies, and the privacy panel counts
them like every other host.

SIX PROVIDERS, ONE GUARANTEE. Anthropic and OpenAI enforce `_SCHEMA` as a
strict structured output. OpenRouter (routed only to endpoints that honour
`response_format`), Gemini (`responseSchema`, with web search off), Ollama
(`format`) and LM Studio (`response_format`) are each asked for it by their
own documented mechanism — but "the provider promised" is not the whole of
the guarantee for any of them. Every reply, from every provider, then passes
`_conform`: a reply with a key outside the schema, a value of the wrong type,
or a NUMBER anywhere (a float, an int, in any field, at any depth) is
rejected outright and never shown. What survives is scrubbed sentence by
sentence. So a provider that ignores the schema can produce a failed
analysis, never a number on the panel.

COST. Every call is metered and the user pays their own provider. Analysis
is therefore explicitly on-demand — a button, never a poll, never a tick.
Nothing in this module is reachable from an engine loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

import ai_providers
import ai_secrets
import terminal as term
from terminal import num, _now_iso

logger = logging.getLogger("krypt.ai")

# anthropic | openai (SDK paths below) | openrouter | gemini | ollama |
# lmstudio (ai_providers). The id reaches a dispatch table, never a URL.
PROVIDERS = ai_providers.PROVIDERS
SDK_PROVIDERS = ("anthropic", "openai")

# Per the Anthropic SDK docs this is the current default model. The OpenAI
# side deliberately has no hardcoded guess: model ids there move faster than
# this file does, and shipping a stale one produces a 404 the user cannot
# diagnose. Settings offers a "load models" button that reads the provider's
# own /models listing, so the list always reflects what THEIR key can reach.
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_OPENAI_MODEL = ""

# These think more at a given effort than the models before them, and Anthropic
# measures them at medium ahead of their predecessors at high.
_MEDIUM_EFFORT_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}


def _effort_for(model: str) -> str:
    return "medium" if model in _MEDIUM_EFFORT_MODELS else "high"

# A hand-triggered analysis. Long enough for a slow reasoning turn, short
# enough that a hung provider does not look like a hung app.
_TIMEOUT_SEC = 90.0

# The four words. A model that cannot pick one says so with "no_view", which
# is a real answer and is rendered as one.
LEANS = ("cheap", "rich", "fair", "no_view")
CONFIDENCES = ("low", "medium", "high")

MAX_BULLETS = 6
MAX_RULES_CHARS = 6000


# ── the response contract ───────────────────────────────────────────────
# Ordered deliberately. Providers generate fields in schema order, so every
# field that constitutes an ARGUMENT is generated before `lean`, the field
# that states a conclusion. A model that writes its conclusion first then
# rationalises it is a worse instrument than one that reasons then concludes,
# and field order is the cheapest way to buy that.
#
# There is no numeric field here. That is the whole design; see the module
# docstring before adding one.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Two or three sentences: what this market actually turns on, "
                "and what event decides it."
            ),
        },
        "resolutionNotes": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Traps in the resolution wording — ambiguity, an edge case "
                "that settles counter-intuitively, a source that may not "
                "report in time. Empty if the wording is clean."
            ),
        },
        "drivers": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Concrete, dated events between now and close that would move "
                "this market. Name the date where there is one."
            ),
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "What you do NOT know that bears on this. Be specific and be "
                "honest; this list is rendered to the user verbatim and is "
                "the most useful thing on the panel."
            ),
        },
        "reasoning": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The argument behind your lean, one step per item. No "
                "probability estimates, no fair values."
            ),
        },
        "lean": {
            "type": "string",
            "enum": list(LEANS),
            "description": (
                "Your directional read RELATIVE TO THE QUOTED PRICE. 'cheap' "
                "= the market looks too low, 'rich' = too high, 'fair' = "
                "about right, 'no_view' = you genuinely cannot tell. "
                "'no_view' is a respected answer; guessing is not."
            ),
        },
        "confidence": {
            "type": "string",
            "enum": list(CONFIDENCES),
            "description": "How much weight your own lean deserves.",
        },
    },
    "required": [
        "summary", "resolutionNotes", "drivers",
        "unknowns", "reasoning", "lean", "confidence",
    ],
    "additionalProperties": False,
}


_SYSTEM = """\
You are reading a single prediction market on behalf of a trader who is \
looking at its price, its order book and its resolution rules on screen \
right now. Your job is reading comprehension, not forecasting.

Do these things:
  - Explain what the market actually turns on, in plain language.
  - Read the resolution criteria adversarially. Prediction markets are lost \
far more often to wording than to being wrong about the world: a market can \
settle against you on a technicality you did not read. Name those.
  - Name the concrete, dated events between now and close that would move it.
  - State honestly what you do not know. You are a language model with a \
training cutoff, you cannot see the order book beyond the single quote you \
were given, and you may be reading a market about something that happened \
after you were trained. Say so, specifically.
  - Then give a directional lean relative to the quoted price, with the \
argument that produced it.

NEVER state a numeric probability, percentage likelihood, fair value or \
edge for this market. Not in any field, not in passing, not hedged. You have \
no calibration for this question and a number from you would read as \
authoritative when it is not. Give the direction and the argument; the \
person reading decides the size. Quoting a figure that is a FACT about the \
world is fine and expected — an inflation print, a strike price, a poll \
number, a date. The prohibition is on a figure that represents YOUR estimate \
of this market's outcome.

If the rules text is thin, or the question turns on something after your \
cutoff, prefer "no_view" and say why in unknowns. An honest "I cannot tell" \
is worth more here than a confident guess, and the person reading this can \
tell the difference.\
"""


# ── numeric-estimate scrubbing ──────────────────────────────────────────
# The schema stops a number becoming a FIELD. This stops one becoming a
# sentence. Deliberately narrow: it fires on a figure the model presents as
# its own read, and leaves figures that are facts about the world alone.
#
# Tested both ways in tests/test_ai_analysis.py — the tests that matter most
# are the ones asserting that legitimate figures SURVIVE, because a scrubber
# that eats "CPI came in at 2.4%" makes the panel useless in exchange for a
# purity nobody asked for.
_ESTIMATE_PATTERNS = [
    # "I'd put it at 70", "my estimate is 68c", "I'd say around 70%"
    r"\b(?:i\s*(?:'|’)?d|i\s+would|i)\s+(?:estimate|put|price|say|peg)\b[^.!?]*?\d",
    r"\bmy\s+(?:estimate|read|number|guess|view)\b[^.!?]*?\d",
    # "fair value is 68c", "fair value around 70"
    r"\bfair\s+(?:value|price)\b[^.!?]*?\d",
    # "probability is about 70%", "chance of roughly 65%", "odds are ~60"
    r"\b(?:probability|likelihood|chance|odds)\b[^.!?]{0,40}?"
    r"(?:is|are|of|at|around|about|near|roughly|~)\s*~?\s*\d",
    # "70% likely", "about a 65% chance", "60c fair"
    r"\d{1,3}\s*(?:%|¢|\bcents?\b)\s*(?:likely|probable|chance|fair|value)\b",
    # "should be trading at 70", "should be priced around 65"
    r"\bshould\s+be\s+(?:trading|priced|quoted)\b[^.!?]*?\d",
]
_ESTIMATE_RE = re.compile("|".join(_ESTIMATE_PATTERNS), re.IGNORECASE)

# Sentence splitter that keeps its delimiter, so rejoining is lossless.
_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]|[^.!?]+$")


def _strip_numeric_estimates(text: str) -> tuple[str, int]:
    """Remove sentences where the model priced the market itself.

    Returns ``(cleaned, removed_count)``. Operates per sentence so one bad
    clause does not cost the user a whole paragraph of good reading.
    """
    if not text or not _ESTIMATE_RE.search(text):
        return text, 0
    kept: list[str] = []
    removed = 0
    for sent in _SENTENCE_RE.findall(text):
        if sent.strip() and _ESTIMATE_RE.search(sent):
            removed += 1
            continue
        kept.append(sent)
    cleaned = "".join(kept).strip()
    # Never hand back an empty string where there was prose: an empty field
    # renders as nothing at all, which hides that anything was removed.
    if not cleaned and removed:
        return "", removed
    return cleaned, removed


def _clean_list(items: Any, limit: int = MAX_BULLETS) -> tuple[list[str], int]:
    out: list[str] = []
    removed = 0
    for raw in (items or [])[:limit]:
        if not isinstance(raw, str):
            continue
        cleaned, n = _strip_numeric_estimates(raw.strip())
        removed += n
        if cleaned:
            out.append(cleaned)
    return out, removed


# ── the reply must BE the schema ────────────────────────────────────────
# The schema is sent to every provider, but enforcement varies by provider and
# by model (a local model's grammar can be ignored by an old server, a Gemini
# call with search on carries none). So the parsed reply is checked against
# the schema HERE, for every provider alike, before anything is shaped.
# Deliberately strict: a key the schema does not have is a reply that did not
# follow the contract, and the obvious extra key a model invents is
# "probability". Rejecting the whole reply — rather than dropping that one
# key and showing the rest — means a model that tried to price the market
# gets nothing on the panel at all.

def _numeric_paths(v: Any, path: str = "") -> list[str]:
    """Every place a number sits in a parsed reply. bool is excluded here only
    because the type check below already rejects it where a string belongs."""
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [path or "(root)"]
    if isinstance(v, dict):
        out: list[str] = []
        for k, x in v.items():
            out += _numeric_paths(x, f"{path}.{k}" if path else str(k))
        return out
    if isinstance(v, list):
        out = []
        for i, x in enumerate(v):
            out += _numeric_paths(x, f"{path}[{i}]")
        return out
    return []


def _conform(data: dict) -> Optional[str]:
    """None when `data` fits `_SCHEMA`; otherwise what broke it.

    Missing keys are tolerated (a thin reply renders thin: an absent `lean`
    is "no_view"). Extra keys, numbers and wrong types are not."""
    props = _SCHEMA["properties"]
    nums = _numeric_paths(data)
    if nums:
        return f"it contained a number ({', '.join(nums[:3])})"
    extra = sorted(set(data) - set(props))
    if extra:
        return f"it added field(s) the format does not have ({', '.join(extra[:3])})"
    for name, spec in props.items():
        if name not in data or data[name] is None:
            continue
        v = data[name]
        if spec.get("type") == "string":
            if not isinstance(v, str):
                return f"{name} was not text"
            if "enum" in spec and v not in spec["enum"]:
                # Out-of-vocabulary leans are tolerated as "no_view" by
                # _shape; this branch only exists for types.
                continue
        elif spec.get("type") == "array":
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                return f"{name} was not a list of text"
    return None


# Reasoning models served locally or through OpenRouter (qwen3, deepseek-r1)
# often put their chain of thought in the reply, and that thought routinely
# contains a draft {...}. Thought is not the answer.
_THINK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.S | re.I)
_THINK_OPEN = re.compile(r"<(think|thinking|reasoning)>", re.I)
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)


def _strip_reasoning(text: str) -> str:
    out = _THINK.sub("", text or "")
    # An unclosed tag means the reply ran out mid-thought: there is no answer.
    m = _THINK_OPEN.search(out)
    if m:
        out = out[:m.start()]
    return out.strip()


def _extract_json(text: str) -> Optional[dict]:
    """First balanced {...} in a reply, parsed — only for a reply that is not
    already bare JSON (a local model that wrapped it in a fence or a think
    block). The result still has to pass _conform like any other."""
    if not text:
        return None
    body = _FENCE.sub("", _strip_reasoning(text)).strip()
    start = body.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(body)):
        ch = body[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(body[start:i + 1])
                except Exception:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _parse_reply(raw: str) -> Any:
    """The reply as JSON: verbatim when it is JSON (the structured-output
    case), else the first balanced object outside any reasoning block."""
    try:
        return json.loads(raw)
    except Exception:
        return _extract_json(raw)


# ── what we send ────────────────────────────────────────────────────────

def build_payload(market: dict) -> dict:
    """The public facts about one market. NOTHING about the user.

    This function takes a market and returns market data. It has no access to
    an account, a position or an order, which is the structural version of
    the promise the Settings screen makes — there is no path by which a
    position could reach a provider, not merely a policy against it.
    """
    m = market or {}
    # The rules text and the named resolver live on Gamma's raw payload, not
    # on the assembled summary — the same place resolution_report reads them.
    raw = m.get("_raw") or {}
    rules = str(
        m.get("resolutionCriteria") or raw.get("description") or m.get("description") or ""
    ).strip()
    if len(rules) > MAX_RULES_CHARS:
        # Truncation is stated rather than silent, so the model can tell the
        # user its reading was partial instead of confidently reasoning over
        # a document whose second half it never saw.
        rules = rules[:MAX_RULES_CHARS] + "\n\n[TRUNCATED — rules text continues]"
    return {
        "question": m.get("question") or m.get("title") or raw.get("question"),
        "outcomes": m.get("outcomes"),
        "priceCents": num(m.get("probability")),
        "bidCents": num(m.get("bid")),
        "askCents": num(m.get("ask")),
        "volume24hUsd": num(m.get("volume24hUsd")),
        "liquidityUsd": num(m.get("liquidityUsd")),
        "closeTime": m.get("closeTime"),
        "status": m.get("status"),
        "resolverName": m.get("resolverName") or raw.get("resolvedBy"),
        "resolutionSource": m.get("resolutionSource") or raw.get("resolutionSource"),
        "rulesText": rules or None,
        "negRisk": bool(m.get("negRisk") or raw.get("negRisk")),
    }


def _render_prompt(payload: dict, asked_at: str) -> str:
    """The payload as text. Explicit about missing fields, because a model
    shown a blank tends to invent one, and shown '(not available)' does not.
    """
    lines = [
        f"Analysis requested at {asked_at} (UTC).",
        "",
        "MARKET",
        f"  Question: {payload.get('question') or '(not available)'}",
    ]
    outcomes = payload.get("outcomes")
    if outcomes:
        lines.append(f"  Outcomes: {', '.join(str(o) for o in outcomes)}")

    price = payload.get("priceCents")
    if price is None:
        lines.append("  Current price: (not available — no quote could be read)")
    else:
        lines.append(
            f"  Current price: {price:.1f}c "
            f"(i.e. the market is paying {price:.1f}c for a contract that "
            f"settles at 100c if this resolves YES)"
        )
    bid, ask = payload.get("bidCents"), payload.get("askCents")
    if bid is not None and ask is not None:
        lines.append(f"  Best bid / ask: {bid:.1f}c / {ask:.1f}c")

    for label, key, fmt in (
        ("24h volume", "volume24hUsd", "${:,.0f}"),
        ("Liquidity", "liquidityUsd", "${:,.0f}"),
    ):
        v = payload.get(key)
        lines.append(f"  {label}: {fmt.format(v) if v is not None else '(not available)'}")

    lines += [
        f"  Closes: {payload.get('closeTime') or '(not available)'}",
        f"  Status: {payload.get('status') or '(not available)'}",
        f"  Resolver: {payload.get('resolverName') or '(not stated)'}",
        f"  Resolution source: {payload.get('resolutionSource') or '(not stated)'}",
    ]
    if payload.get("negRisk"):
        lines.append(
            "  This market is one leg of a mutually-exclusive group: exactly "
            "one leg in the event resolves YES."
        )

    rules = payload.get("rulesText")
    lines += ["", "RESOLUTION RULES (verbatim)", rules or "  (none published — say so in unknowns)"]
    return "\n".join(lines)


# ── the gate ────────────────────────────────────────────────────────────

class _Refused(Exception):
    """A refusal we can explain to the user, as opposed to a crash."""

    def __init__(self, reason: str, fix: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fix = fix


class _gate_call:
    """Run one provider call under `terminal`'s per-host gate.

    The SDKs do not go through `_fetch`, so everything `_fetch` provides has
    to be applied here explicitly or three shipped guarantees quietly stop
    covering the two hosts the user is most likely to care about: the offline
    switch, the 429 park, and the privacy panel's counters. A provider the
    kill switch does not reach would make that switch a lie — the same
    reasoning that put Kalshi in `_HOSTS` rather than giving it its own
    client.

    The SDK's own retry is disabled at construction (`max_retries=0`) so this
    park is the single retry authority. Two independent backoffs stacked on
    each other is how a rate limit turns into a hang.
    """

    def __init__(self, host: str) -> None:
        self.host = host
        self.gate = term._gates.get(host)
        self.started = 0.0

    async def __aenter__(self) -> "_gate_call":
        if term.is_offline():
            raise _Refused(
                term.OFFLINE_REASON,
                "Turn off offline mode in Settings → Data & privacy.",
            )
        g = self.gate
        if g is None:  # pragma: no cover — _HOSTS and PROVIDERS ship together
            raise _Refused(f"No host gate is registered for {self.host}.")
        if g.parked():
            raise _Refused(
                f"{self.host} returned a rate limit recently, so it is parked "
                f"for a few seconds rather than retried into a harder limit.",
                "Wait a moment and ask again.",
            )
        await g.lock().acquire()
        # __aexit__ never runs when __aenter__ raises, so everything between
        # the acquire and the return has to release the lock itself. The
        # min-gap sleep below is an await, and a cancelled await there — the
        # panel closed, the RPC timed out — would otherwise leave the host
        # lock held by a coroutine that no longer exists. Every later call
        # then waits on it forever: the feature is dead until restart.
        try:
            wait = g.min_gap - (time.monotonic() - g.last)
            if wait > 0:
                await asyncio.sleep(wait)
            self.started = time.monotonic()
            g.calls += 1
        except BaseException:
            g.lock().release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        g = self.gate
        if g is not None:
            g.last = time.monotonic()
            g.note_latency((g.last - self.started) * 1000.0)
            if exc is not None:
                g.errors += 1
                if _is_rate_limit(exc):
                    g.park()
            g.lock().release()
        return False


def _is_rate_limit(exc: BaseException) -> bool:
    """True for a 429 from either SDK, without importing either one.

    Both raise a typed RateLimitError and both carry `.status_code`; matching
    on either keeps this importable when neither package is installed, which
    is the normal state for a user who has not enabled the feature.
    """
    if type(exc).__name__ == "RateLimitError":
        return True
    return getattr(exc, "status_code", None) == 429


# ── providers ───────────────────────────────────────────────────────────

async def _close(client: Any) -> None:
    """Release an SDK client's HTTP connection pool.

    Both async SDKs open a pool at construction and hold it until closed. One
    client per analysis, never closed, is a socket and file-descriptor leak in
    a process the user leaves running for days. Failing to close is a leak,
    not a failed analysis, so it is logged rather than raised over the result
    the caller already has.
    """
    closer = getattr(client, "close", None)
    if closer is None:  # pragma: no cover — both SDKs expose close()
        return
    try:
        await closer()
    except Exception as e:  # noqa: BLE001
        logger.debug("ai: closing client failed: %s: %s", type(e).__name__, e)


def _require_key(provider: str) -> str:
    key = ai_secrets.load_key(provider)
    if not key:
        raise _Refused(
            f"No {ai_providers.CAPS[provider].label} API key is stored.",
            "Add one in Settings → AI analysis.",
        )
    return key


# ── provider settings (shared with autopilot) ───────────────────────────

def norm_provider(v: Any) -> str:
    p = str(v or "").strip().lower()
    return p if p in PROVIDERS else "anthropic"


def normalize_model(provider: str, model: Any) -> str:
    """The model id we would send: shape-checked, else the provider's default
    ("" for OpenAI and LM Studio, which means "pick one" / "the loaded one")."""
    return ai_providers.clean_model(norm_provider(provider), model)


def configured_model(cfg: dict) -> str:
    """`ai_model`, falling back to the pre-6.x per-provider key for a config
    that has not been through merge_with_defaults' migration."""
    cfg = cfg or {}
    provider = norm_provider(cfg.get("ai_provider"))
    raw = cfg.get("ai_model")
    if not raw:
        raw = cfg.get(f"ai_{provider}_model")
    return normalize_model(provider, raw)


def needs_key(provider: str) -> bool:
    return ai_providers.CAPS[norm_provider(provider)].needs_key


def read_key(provider: str) -> Optional[str]:
    """The stored key, or None. Never for a local provider, which has none."""
    p = norm_provider(provider)
    if not ai_secrets.stores_key(p):
        return None
    return ai_secrets.load_key(p)


def ready(provider: str) -> bool:
    """Whether a call could be attempted: a key is stored, or none is needed.
    Whether a local server is actually up is check_provider's question — that
    needs a network call, and this is read on every status poll."""
    p = norm_provider(provider)
    return (not needs_key(p)) or ai_secrets.has_key(p)


async def _call_http(provider: str, prompt: str, model: str, web_search: bool) -> dict:
    """OpenRouter, Gemini, Ollama, LM Studio: ai_providers.call, off the loop,
    with `_SCHEMA` asked of the provider. ai_providers applies the terminal
    gate's offline/park/count rules itself (it is synchronous httpx)."""
    key = read_key(provider)
    if needs_key(provider) and not key:
        key = _require_key(provider)
    try:
        got = await asyncio.to_thread(
            ai_providers.call, provider, key, model, _SYSTEM, prompt, web_search,
            _TIMEOUT_SEC, schema=_SCHEMA, schema_name="market_analysis")
    except ai_providers.AiError as e:
        raise _Refused(str(e)) from e
    return {"raw": got.get("text") or "", "model": got.get("model") or model,
            "sources": got.get("citations") or [],
            "webSearchUsed": bool(got.get("webSearchUsed"))}


async def _call_anthropic(prompt: str, model: str, web_search: bool) -> dict:
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover — packaged build always has it
        raise _Refused(
            "The anthropic package is not installed in this build.",
            "Reinstall the app, or `pip install anthropic` when running from source.",
        ) from e

    client = anthropic.AsyncAnthropic(
        api_key=_require_key("anthropic"),
        timeout=_TIMEOUT_SEC,
        max_retries=0,  # our 429 park is the single retry authority
    )
    kwargs: dict[str, Any] = {
        "model": model or DEFAULT_ANTHROPIC_MODEL,
        # Thinking counts toward this, and an answer cut off here is unreadable JSON.
        "max_tokens": 16000,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": _effort_for(model or DEFAULT_ANTHROPIC_MODEL),
            "format": {"type": "json_schema", "schema": _SCHEMA},
        },
    }
    if web_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]

    try:
        async with _gate_call("anthropic"):
            resp = await client.messages.create(**kwargs)
    finally:
        await _close(client)

    if getattr(resp, "stop_reason", None) == "refusal":
        detail = getattr(resp, "stop_details", None)
        raise _Refused(
            "The model declined to answer this one"
            + (f" ({detail.category})" if getattr(detail, "category", None) else "")
            + ".",
            "This is the provider's own safety filter, not the app.",
        )

    text = next(
        (b.text for b in resp.content if getattr(b, "type", None) == "text"), None
    )
    if not text:
        raise _Refused("The model returned no text to read.")
    sources = _anthropic_sources(resp)
    return {"raw": text, "model": getattr(resp, "model", model), "sources": sources}


def _anthropic_sources(resp: Any) -> list[dict]:
    """URLs the server-side web search actually read, so a claim grounded in
    news can be checked rather than taken on faith."""
    out: list[dict] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        # A search ERROR is an object, not a list. Branch before indexing.
        if not isinstance(content, list):
            continue
        for item in content:
            url = getattr(item, "url", None)
            if url:
                out.append({"url": url, "title": getattr(item, "title", None)})
    return out[:8]


async def _call_openai(prompt: str, model: str, web_search: bool) -> dict:
    try:
        from openai import AsyncOpenAI
    except ImportError as e:  # pragma: no cover
        raise _Refused(
            "The openai package is not installed in this build.",
            "Reinstall the app, or `pip install openai` when running from source.",
        ) from e

    if not model:
        raise _Refused(
            "No OpenAI model is selected.",
            "Pick one in Settings → AI analysis (press Load models).",
        )

    client = AsyncOpenAI(
        api_key=_require_key("openai"),
        timeout=_TIMEOUT_SEC,
        max_retries=0,
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "input": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "market_analysis",
                "strict": True,
                "schema": _SCHEMA,
            }
        },
    }
    if web_search:
        kwargs["tools"] = [{"type": "web_search"}]

    try:
        async with _gate_call("openai"):
            resp = await client.responses.create(**kwargs)
    finally:
        await _close(client)

    text = getattr(resp, "output_text", None)
    if not text:
        raise _Refused("The model returned no text to read.")
    return {"raw": text, "model": getattr(resp, "model", model), "sources": []}


# ── model discovery ─────────────────────────────────────────────────────

async def list_models(provider: str) -> list[str]:
    """What THIS key can actually reach.

    Hardcoding a model list guarantees it goes stale and produces a 404 the
    user cannot diagnose. Asking the provider costs one cheap call and is
    always right.
    """
    if provider not in PROVIDERS:
        raise _Refused(f"Unknown provider {provider!r}.")
    key = _require_key(provider) if needs_key(provider) else None
    # Free listing endpoints for all six, through ai_providers (httpx, off
    # the loop): one place for the key header, the error wording and the
    # terminal gate's offline/park/count rules.
    try:
        models = await asyncio.to_thread(ai_providers.list_models, provider, key,
                                         fresh=True)
    except ai_providers.AiError as e:
        raise _Refused(str(e)) from e
    return [m.id for m in models if m.kind != "embedding"]


def check_provider(provider: str, cfg: Optional[dict] = None) -> dict:
    """Settings' "Test connection": {ok, provider, model, message, models?,
    modelInfo?, tools?, webSearch?, reason?}. Free endpoints only (a model
    listing, and OpenRouter's key-info endpoint) — never a billed call.
    Blocking; call it off the loop."""
    p = str(provider or "").strip().lower()
    if p not in PROVIDERS:
        return {"ok": False, "provider": p,
                "message": f"Unknown AI provider: {p or '—'}"}
    cfg = cfg or {}
    model = (configured_model(cfg) if norm_provider(cfg.get("ai_provider")) == p
             else ai_providers.DEFAULT_MODEL[p])
    res = ai_providers.check(p, read_key(p), model)
    res.setdefault("provider", p)
    res["model"] = model
    return res


def tool_support(provider: str, model: Any) -> tuple[bool, str]:
    """Whether Autopilot can run on this provider/model — it works only
    through tools. Blocking (a local call or a free listing)."""
    p = norm_provider(provider)
    return ai_providers.tool_support(p, read_key(p), normalize_model(p, model))


# ── the entry point ─────────────────────────────────────────────────────

def is_enabled(cfg: dict) -> bool:
    """Whether the feature may talk to a provider at all.

    Exposed so a caller can skip expensive setup work it would only throw
    away — the REASON it is off still comes from analyze(), so the wording
    lives in exactly one place.
    """
    return bool((cfg or {}).get("ai_enabled", False))


async def analyze(market: dict, cfg: dict) -> dict:
    """Analyse one market. Returns the panel's contract, always.

    Never raises for an expected condition. A missing key, offline mode, a
    parked host, a provider outage and a malformed response all come back as
    ``ok: false`` with a reason the UI renders and a fix the user can act on
    — the same convention as every other honest-null path in the terminal.
    """
    asked_at = _now_iso()
    cfg = cfg or {}
    # Checked HERE rather than only at the RPC edge. A stored key is not
    # consent to start sending market text to a third party, and this module
    # is the thing that would do the sending — so it owns the rule.
    if not cfg.get("ai_enabled", False):
        return _fail(
            str(cfg.get("ai_provider") or "anthropic"), "", asked_at,
            "AI analysis is turned off.",
            "Turn it on in Settings → AI analysis.")
    provider = norm_provider(cfg.get("ai_provider"))
    # What the setting asks for AND the provider can do: a local model has no
    # search, and "web search on" above its answer would be a lie.
    web_search = bool(cfg.get("ai_web_search", False)) and \
        ai_providers.CAPS[provider].web_search
    model = configured_model(cfg)

    payload = build_payload(market)
    if not payload.get("question"):
        return _fail(provider, model, asked_at,
                     "That market has no question text to read.", None)

    prompt = _render_prompt(payload, asked_at)
    started = time.monotonic()
    try:
        if provider == "anthropic":
            got = await _call_anthropic(prompt, model, web_search)
        elif provider == "openai":
            got = await _call_openai(prompt, model, web_search)
        else:
            got = await _call_http(provider, prompt, model, web_search)
            web_search = bool(got.get("webSearchUsed"))
    except _Refused as e:
        return _fail(provider, model, asked_at, e.reason, e.fix)
    except asyncio.TimeoutError:
        return _fail(provider, model, asked_at,
                     f"{provider} did not answer within {_TIMEOUT_SEC:.0f}s.",
                     "Try again, or pick a faster model.")
    except Exception as e:  # noqa: BLE001 — every provider failure is a reason, not a crash
        logger.warning("ai: %s call failed: %s: %s", provider, type(e).__name__, e)
        return _fail(provider, model, asked_at,
                     f"{provider} returned an error: {type(e).__name__}.",
                     _hint_for(e))

    data = _parse_reply(got.get("raw") or "")
    # `null`, a bare string and a list are all VALID JSON, so the parse
    # succeeds and only the shape is wrong. _shape reads `data` as a mapping,
    # so anything else raised out of analyze() — the one function that
    # promises never to raise for an expected condition. A reply we cannot
    # read is a reply we cannot read, whichever way it is malformed.
    if not isinstance(data, dict):
        logger.warning("ai: %s returned unparseable JSON", provider)
        return _fail(provider, model, asked_at,
                     "The model's reply was not valid JSON.",
                     "Try again — this is usually transient.")

    # The schema check, for every provider alike. A reply that carries a
    # number anywhere, or a field the format does not have, is not shown.
    broke = _conform(data)
    if broke:
        logger.warning("ai: %s reply rejected: %s", provider, broke)
        return _fail(provider, got.get("model") or model, asked_at,
                     f"The model's reply broke the response format ({broke}), "
                     f"so none of it was shown. This panel never shows a "
                     f"model's own number.",
                     "Try again, or pick a model that supports structured output.")

    return _shape(data, provider, got.get("model") or model, asked_at,
                  web_search, got.get("sources") or [],
                  round((time.monotonic() - started) * 1000.0))


def _hint_for(exc: BaseException) -> Optional[str]:
    name = type(exc).__name__
    if name == "AuthenticationError":
        return "The stored API key was rejected. Re-enter it in Settings → AI analysis."
    if name in ("APIConnectionError", "APITimeoutError"):
        return "Check your connection, then try again."
    if _is_rate_limit(exc):
        return "You hit the provider's rate limit. Wait a moment and try again."
    return None


def _fail(provider: str, model: str, asked_at: str,
          reason: str, fix: Optional[str]) -> dict:
    return {
        "ok": False, "provider": provider, "model": model,
        "askedAt": asked_at, "reason": reason, "fix": fix,
        "webSearch": False, "sources": [], "redactions": 0, "latencyMs": None,
        "summary": None, "resolutionNotes": [], "drivers": [],
        "unknowns": [], "reasoning": [], "lean": None, "confidence": None,
    }


def _shape(data: dict, provider: str, model: str, asked_at: str,
           web_search: bool, sources: list[dict], latency_ms: float) -> dict:
    """Validate, scrub, and stamp. The schema already guarantees the shape;
    this guarantees the CONTENT rule the schema cannot express."""
    summary, r1 = _strip_numeric_estimates(str(data.get("summary") or "").strip())
    notes, r2 = _clean_list(data.get("resolutionNotes"))
    drivers, r3 = _clean_list(data.get("drivers"))
    unknowns, r4 = _clean_list(data.get("unknowns"))
    reasoning, r5 = _clean_list(data.get("reasoning"))

    lean = data.get("lean")
    if lean not in LEANS:
        lean = "no_view"
    conf = data.get("confidence")
    if conf not in CONFIDENCES:
        conf = "low"

    # A lean whose whole argument was scrubbed is a lean with nothing behind
    # it, which is precisely the thing this module exists not to show.
    if lean != "no_view" and not reasoning:
        lean, conf = "no_view", "low"

    return {
        "ok": True,
        "provider": provider,
        "model": model,
        "askedAt": asked_at,
        "latencyMs": latency_ms,
        "webSearch": bool(web_search),
        "sources": sources,
        "summary": summary or None,
        "resolutionNotes": notes,
        "drivers": drivers,
        "unknowns": unknowns,
        "reasoning": reasoning,
        "lean": lean,
        "confidence": conf,
        "redactions": r1 + r2 + r3 + r4 + r5,
        "reason": None,
        "fix": None,
    }


def status(cfg: dict) -> dict:
    """What Settings and the Market panel need to render the feature's state
    without either of them re-deriving the gates."""
    cfg = cfg or {}
    provider = norm_provider(cfg.get("ai_provider"))
    caps = ai_providers.CAPS[provider]
    return {
        "enabled": bool(cfg.get("ai_enabled", False)),
        "provider": provider,
        # The model the analysis (and Autopilot) would send. "" = none chosen
        # (OpenAI) or "whichever model LM Studio has loaded".
        "model": configured_model(cfg),
        # What the setting asks for AND the provider can do.
        "webSearch": bool(cfg.get("ai_web_search", False)) and caps.web_search,
        "webSearchSetting": bool(cfg.get("ai_web_search", False)),
        "offline": term.is_offline(),
        # Whether an analysis could be attempted on the SELECTED provider: a
        # key is stored, or none is needed (local).
        "hasKey": ready(provider),
        # Per provider: stored? (never the key — a recognisable mask only).
        # Local providers: present False, needsKey False.
        "keys": {
            p: {"present": ai_secrets.has_key(p), "masked": ai_secrets.masked(p),
                "needsKey": ai_providers.CAPS[p].needs_key}
            for p in PROVIDERS
        },
        # Suggestions plus whatever the last free listing returned. Never a
        # network call — this is polled.
        "models": {p: ai_providers.models_for(p) for p in PROVIDERS},
        "providers": list(PROVIDERS),
        "capabilities": {p: ai_providers.capabilities(p) for p in PROVIDERS},
        # USD per million tokens, ONLY where a price is published.
        "prices": {m: {"inPerMTok": i, "outPerMTok": o}
                   for m, (i, o) in ai_providers.PRICING.items()},
    }
