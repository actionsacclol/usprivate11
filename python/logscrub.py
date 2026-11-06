"""Keep credentials out of the logs.

The backend holds an exchange API secret (Ed25519), chat-bot tokens, AI provider
keys and one bearer token per AI agent. Users paste their logs into a chat window when something
breaks, so a log line is the most likely way a secret escapes this app — and
it escapes to exactly the people trying to help.

Two independent passes, because each catches what the other misses:

  * **By value.** The literal secrets currently stored are redacted wherever
    they appear, whatever the surrounding text. Exact, and it catches a value
    logged by a library we do not control.
  * **By shape.** Discord / Telegram token patterns, AI provider keys with a
    known prefix (sk-…, AIza…), our own agent token prefix (kp_…), secret keys
    written next to a label that says so, PEM private-key blocks, Authorization
    and X-PM-* headers are redacted even when the value is one we have never seen — a key pasted
    into the wrong box, or a second account's.

Deliberately NOT scrubbed: market slugs, order ids and bare UUIDs, which include
the API key's ID (public, sent on every request, and needed to tell keys apart).
They are the most useful thing in a trading log, and the secret half is caught by
value anyway.

This module runs inside the log pipeline: nothing here may raise, and nothing
here touches the network.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

REDACTED = "[redacted]"

# Discord bot token: base64ish id . timestamp . hmac
_DISCORD = re.compile(r"\b[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,40}\b")
# Telegram bot token: <numeric id>:<35 char secret>
_TELEGRAM = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,45}\b")
# Any PEM private key, however it got into a string.
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
# Authorization-style headers, in case a client ever logs its own request.
# Two patterns, because `Authorization: Bearer <token>` puts the SCHEME between
# the header name and the value: a "name, colon, one word" rule would match
# only the word "Bearer" and leave the token in the clear.
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|x-api-key|x-pm-signature)"
    r"\s*[:=]\s*\S.*")
_SCHEME_TOKEN = re.compile(r"(?i)\b(?:bearer|bot)\s+[A-Za-z0-9._\-]{8,}")
# Telegram puts the token in the URL PATH, so a logged request line leaks it.
_TG_URL = re.compile(r"(api\.telegram\.org/bot)[^/\s]+")
# A Discord webhook's credential IS its last path segment.
_DISCORD_WEBHOOK_URL = re.compile(
    r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_\-]+")
# AI provider keys with a recognisable prefix. `sk-` covers OpenRouter
# (sk-or-v1-…), Anthropic (sk-ant-…) and OpenAI (sk-proj-… / sk-…); Google
# API keys are AIza + 35 characters.
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{19,}")
_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])")
# Our own agent bearer token: "kp_" + 43 urlsafe characters.
_AGENT_TOKEN = re.compile(r"(?i)\bkp_[A-Za-z0-9_\-]{20,}")
# A 32-byte hex value written next to a word that says it is a key. A bare
# 0x+64-hex is a conditionId as often as a key, so the label is required.
_LABELLED_HEX_KEY = re.compile(
    r"(?i)((?:private[_ ]?key|priv[_ ]?key|secret|passphrase|seed)\W{0,4})"
    r"(?:0x)?[0-9a-f]{64}\b")
# The exchange's secret is base64 of 32 or 64 bytes. Bare, that shape is too common
# to redact (the label is required), and the real one is caught by value.
_LABELLED_B64_KEY = re.compile(
    r"(?i)((?:private[_ ]?key|secret[_ ]?key|api[_ ]?secret|seed)[\"']?\W{0,4})"
    r"[A-Za-z0-9+/_\-]{40,}={0,2}")

# Order matters: the header rule consumes the rest of the line, so the
# specific token shapes get their chance first.
_SHAPES = (_PEM, _DISCORD, _TELEGRAM, _SK_KEY, _GOOGLE_KEY, _AGENT_TOKEN,
           _SCHEME_TOKEN, _AUTH_HEADER)

# Literal values to redact. Refreshed when a secret is saved or cleared.
_values: list[str] = []


def set_known_secrets(values: Iterable[str]) -> None:
    """Register the literal secrets currently stored, longest first so a
    substring of one cannot mask a longer match. Values under 12 characters
    are ignored: redacting a short common string would shred every log line."""
    global _values
    _values = sorted({v.strip() for v in values if isinstance(v, str) and len(v.strip()) >= 12},
                     key=len, reverse=True)


def known_count() -> int:
    return len(_values)


def _read(path) -> str:
    import polymarket_auth as auth
    # upgrade=False: reading for the scrubber must never rewrite a file.
    return auth._read_secret_bytes(path, upgrade=False).decode("utf-8", "replace").strip()


def _api_key_secret(text: str) -> list[str]:
    """The secret half of the stored {keyId, secretKey}. The whole file as one
    string would never match a log line, and the key id isn't a secret."""
    import json
    try:
        data = json.loads(text)
    except ValueError:
        return [text]   # unreadable as JSON: redact it whole rather than not at all
    secret = data.get("secretKey") if isinstance(data, dict) else None
    return [secret] if isinstance(secret, str) else []


def refresh_known_secrets() -> None:
    """Re-read every secret the app holds from the credential store. Cheap;
    called at startup and after any credential change. Listed from the store
    rather than from config: the scrubber must cover a token whatever config
    says, including one whose agent was just deleted."""
    try:
        import polymarket_auth as auth
        d = auth._credentials_dir()
        found: list[str] = []
        if d.exists():
            for p in d.iterdir():
                name = p.name
                if not p.is_file() or name.endswith((".tmp", ".bak")):
                    continue
                try:
                    if name.startswith("apikey.") and name.endswith(".key"):
                        found += _api_key_secret(_read(p))
                    elif name.endswith((".token", ".key")):
                        # mcp.token, mcp_<id>.token, bot.<platform>.token,
                        # ai.<provider>.key
                        found.append(_read(p))
                except Exception:   # noqa: BLE001 — one unreadable file is not fatal
                    continue
        set_known_secrets(found)
    except Exception:   # noqa: BLE001 — never let scrubber upkeep break logging
        pass


def add_known_secret(value: str) -> None:
    """Register one new secret without re-reading the store (a token just
    minted)."""
    set_known_secrets([*_values, value])


def looks_secret(text: str) -> bool:
    """True when `text` carries any part of a known secret, in ANY case, or a
    secret's shape.

    scrub() matches known values literally, which is right for a log line we
    wrote ourselves. It is not enough for a string an AI agent chose: a tool
    may lower-case a conditionId, so an agent that echoed its own bearer token
    where an id goes would defeat a literal match. This folds case, and also
    catches a FRAGMENT (12+ chars) of a known value, because a display field
    gets clipped and a clipped token is still most of a token."""
    if not text:
        return False
    if scrub(text) != text:
        return True
    low = text.lower()
    for v in _values:
        vl = v.lower()
        if vl in low:
            return True
        if len(low) >= 12 and low in vl:
            return True
    return False


def scrub(text: str) -> str:
    if not text:
        return text
    out = text
    for v in _values:
        if v in out:
            out = out.replace(v, REDACTED)
    for pattern in _SHAPES:
        if pattern is _AUTH_HEADER:
            # Keep the header NAME so the line stays diagnosable.
            out = pattern.sub(lambda m: f"{m.group(1)}: {REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    out = _LABELLED_HEX_KEY.sub(lambda m: m.group(1) + REDACTED, out)
    out = _LABELLED_B64_KEY.sub(lambda m: m.group(1) + REDACTED, out)
    # Both keep the identifying prefix and drop only the secret tail.
    out = _TG_URL.sub(r"\1" + REDACTED, out)
    out = _DISCORD_WEBHOOK_URL.sub(r"\1" + REDACTED, out)
    return out


class ScrubFilter(logging.Filter):
    """Attach to every HANDLER (a filter on a logger does not see records
    propagated from its children), so the file and the stdout stream are both
    covered by one thing rather than each remembering to scrub."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Format here so lazy %s args are interpolated before the scrub; a
            # secret passed as an arg would otherwise slip through.
            msg = record.getMessage()
            cleaned = scrub(msg)
            if cleaned != msg:
                record.msg = cleaned
                record.args = ()
            if record.exc_info and not record.exc_text:
                # Tracebacks quote arguments too.
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            elif record.exc_text:
                record.exc_text = scrub(record.exc_text)
        except Exception:   # noqa: BLE001
            pass
        return True


def install(*loggers: logging.Logger) -> ScrubFilter:
    """Put one ScrubFilter on every handler of the given loggers (root by
    default). Idempotent."""
    f = ScrubFilter()
    for lg in loggers or (logging.getLogger(),):
        for h in lg.handlers:
            if not any(isinstance(x, ScrubFilter) for x in h.filters):
                h.addFilter(f)
    return f
