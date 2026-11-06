"""At-rest storage for the chat bots' tokens.

A bot token is a credential: whoever holds it can speak as your bot. So it is
stored the same way the wallet key is — through ``polymarket_auth``'s secret
writer, which picks the best backend available (Windows DPAPI → OS keychain →
a chmod-600 plaintext fallback that logs loudly about itself).

Deliberately NOT in settings.json or app_kv: those are plain files the user
copies around, pastes into support threads and syncs to cloud drives.

Note the token is not the security boundary anyway — the paired-id allowlist
in ``remote.py`` is. This is defence in depth, not the defence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import polymarket_auth as auth

logger = logging.getLogger("krypt.remote.secrets")

_VALID = ("discord", "telegram")


def _path(platform: str) -> Path:
    if platform not in _VALID:
        raise ValueError(f"unknown platform {platform!r}")
    # Fixed names from a fixed set — `platform` is never interpolated from
    # anything user-supplied, so there is no path-traversal surface here.
    return auth._credentials_dir() / f"bot.{platform}.token"


def save_token(platform: str, token: str) -> None:
    t = (token or "").strip()
    if not t:
        clear_token(platform)
        return
    auth._write_secret_bytes(_path(platform), t.encode("utf-8"))
    logger.info("remote: stored %s bot token", platform)


def load_token(platform: str) -> Optional[str]:
    p = _path(platform)
    if not p.exists():
        return None
    try:
        return auth._read_secret_bytes(p).decode("utf-8").strip() or None
    except Exception as e:  # noqa: BLE001
        logger.warning("remote: could not read %s token: %s", platform, e)
        return None


def has_token(platform: str) -> bool:
    return load_token(platform) is not None


def clear_token(platform: str) -> None:
    p = _path(platform)
    try:
        if p.exists():
            p.unlink()
            logger.info("remote: cleared %s bot token", platform)
    except OSError as e:
        logger.warning("remote: could not clear %s token: %s", platform, e)


def masked(platform: str) -> Optional[str]:
    """A shape the user can recognise without the screen leaking the token."""
    t = load_token(platform)
    if not t:
        return None
    return f"{t[:4]}…{t[-4:]}" if len(t) > 12 else "…"
