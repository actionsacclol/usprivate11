"""Shared SSL context for outbound WebSocket (wss://) connections.

Frozen (PyInstaller) builds — macOS especially — ship a Python whose OpenSSL
default verify paths don't exist on the host, so `websockets.connect` fails
every wss handshake with SSLCertVerificationError (unable to get local issuer
certificate) while httpx REST calls keep working (httpx verifies against the
bundled certifi CA store). Build the websocket context from that same certifi
bundle so both stacks trust the same roots on every platform.
"""
from __future__ import annotations

import ssl
from typing import Optional

_ctx: Optional[ssl.SSLContext] = None


def client_context() -> Optional[ssl.SSLContext]:
    """certifi-backed client SSLContext, cached after first build. Returns
    None when certifi is unavailable — passing ssl=None to websockets.connect
    keeps its stock default-context behavior, so this can only improve
    verification, never break it."""
    global _ctx
    if _ctx is None:
        try:
            import certifi  # httpx dependency — present wherever httpx works
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return None
    return _ctx
