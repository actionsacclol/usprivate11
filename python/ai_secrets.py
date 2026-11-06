"""At-rest storage for the AI provider API keys.

An LLM API key is a credential with a bill attached: whoever holds it can
spend the user's money at the provider. So it is stored exactly the way the
wallet key and the chat-bot tokens are — through ``polymarket_auth``'s secret
writer, which picks the best backend available (Windows DPAPI → OS keychain →
a chmod-600 plaintext fallback that logs loudly about itself).

Deliberately NOT in settings.json or app_kv: those are plain files the user
copies around, pastes into support threads and syncs to cloud drives. A
leaked wallet key costs the user their funds; a leaked provider key costs
them a runaway bill on somebody else's rate card. Both belong behind the
same door.

Unlike the wallet key, this one is NOT a security boundary for the app: an
attacker holding it cannot touch the wallet, cannot place an order, and
cannot read a position. It buys them inference. That is worth saying plainly
so nobody over-rotates on it — but it is still the user's money, so it is
still encrypted.

The key is never sent back to the renderer. ``masked()`` returns a shape the
user can recognise on the Settings screen without the screen itself becoming
a place the key leaks from.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import polymarket_auth as auth

logger = logging.getLogger("krypt.ai.secrets")

# The set is fixed and closed. `provider` is never interpolated from anything
# user-supplied, so there is no path-traversal surface here — the same
# reasoning as bot_secrets._path.
#
# Only the CLOUD providers have a key. Ollama and LM Studio run on this
# machine and take none, so a key "saved" for one would be a secret sitting in
# the store for nothing — _path refuses them like any unknown name.
_VALID = ("openai", "anthropic", "openrouter", "gemini")


def _path(provider: str) -> Path:
    if provider not in _VALID:
        raise ValueError(f"unknown provider {provider!r}")
    return auth._credentials_dir() / f"ai.{provider}.key"


def save_key(provider: str, key: str) -> None:
    k = (key or "").strip()
    if not k:
        clear_key(provider)
        return
    auth._write_secret_bytes(_path(provider), k.encode("utf-8"))
    logger.info("ai: stored %s api key", provider)
    _refresh_scrubber()


def _refresh_scrubber() -> None:
    """The log scrubber redacts known secrets by literal value; a key it has
    never been told about is covered only by its shape rules. Re-read the
    store so the new key is redacted by value from the next log line on."""
    try:
        import logscrub
        logscrub.refresh_known_secrets()
    except Exception:  # noqa: BLE001 — upkeep must never fail a save
        pass


def load_key(provider: str) -> Optional[str]:
    p = _path(provider)
    if not p.exists():
        return None
    try:
        return auth._read_secret_bytes(p).decode("utf-8").strip() or None
    except Exception as e:  # noqa: BLE001
        logger.warning("ai: could not read %s key: %s", provider, e)
        return None


def has_key(provider: str) -> bool:
    return provider in _VALID and load_key(provider) is not None


def stores_key(provider: str) -> bool:
    """Whether this provider takes a key at all (the local ones do not)."""
    return provider in _VALID


def clear_key(provider: str) -> None:
    p = _path(provider)
    try:
        if p.exists():
            p.unlink()
            logger.info("ai: cleared %s api key", provider)
            _refresh_scrubber()
    except OSError as e:
        logger.warning("ai: could not clear %s key: %s", provider, e)


def masked(provider: str) -> Optional[str]:
    """A shape the user can recognise without the screen leaking the key."""
    if provider not in _VALID:
        return None
    k = load_key(provider)
    if not k:
        return None
    return f"{k[:6]}…{k[-4:]}" if len(k) > 14 else "…"
