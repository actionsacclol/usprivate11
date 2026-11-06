"""Cross-process advisory lock keyed by the trading wallet address.

Krypt PolyBot US supports multiple accounts, each launched as its own process with
its own data dir + credentials. If a user imports the SAME wallet private key
into two accounts, two backends would trade ONE on-chain wallet at once —
racing orders and double exposure that neither side's risk caps can see (each
reads only its own DB). This module lets each backend claim the wallet by
address; a second live backend on the same address backs off NEW entries until
the first exits.

Time-based heartbeat (no fragile cross-platform PID-liveness needed): the holder
re-stamps the lock every scan, and a lock whose stamp is older than `_STALE_SEC`
is treated as abandoned (a crashed holder) and reclaimable.

Fail-OPEN by design: any lock I/O problem returns "you hold it", so a filesystem
quirk can never BLOCK real-money trading. The worst case is the pre-existing
no-mutual-exclusion behavior — never a lockout that strands open positions.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# A holder must re-stamp within this window or it's considered crashed. Generous
# vs the scan cadence so a brief stall never hands the wallet to a sibling.
_STALE_SEC = 120.0

# Unique per process — a bare pid can be reused across reboots, which would let a
# stale lock look like it's "ours".
_HOLDER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _lock_dir() -> Path:
    # A SHARED location across accounts/instances on the machine (per-account
    # userData dirs differ, so the lock can't live there).
    return Path(tempfile.gettempdir()) / "krypt-polybot-us-locks"


def _lock_file(key: str) -> Path:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _lock_dir() / f"{h}.lock"


def claim(key: str, *, stale_sec: float = _STALE_SEC) -> bool:
    """Try to hold the lock for `key` (the trading wallet address). Returns True
    if we now hold it (freshly acquired or heartbeat-refreshed) — safe to trade —
    or False if another LIVE process currently holds it. Fail-open (True) on any
    I/O error so a lock glitch never blocks trading."""
    if not key:
        return True
    try:
        f = _lock_file(key)
        now = time.time()
        holder = None
        ts = 0.0
        if f.exists():
            try:
                data = json.loads(f.read_text("utf-8"))
                holder = data.get("holder")
                ts = float(data.get("ts") or 0.0)
            except Exception:
                holder, ts = None, 0.0  # corrupt -> treat as free
        if holder and holder != _HOLDER_ID and (now - ts) < stale_sec:
            return False  # another live instance owns this wallet — don't trade it
        # Free / stale / already ours -> (re)claim with a fresh heartbeat.
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".lock.tmp")
        tmp.write_text(
            json.dumps({"holder": _HOLDER_ID, "ts": now, "pid": os.getpid()}),
            "utf-8",
        )
        tmp.replace(f)
        return True
    except Exception as e:
        logger.debug(f"instance_lock claim error (fail-open): {e}")
        return True


def foreign_holder(key: str, *, stale_sec: float = _STALE_SEC) -> str:
    """If another live process holds `key`, return its pid (for logging); else "".
    Read-only — never claims."""
    if not key:
        return ""
    try:
        f = _lock_file(key)
        if not f.exists():
            return ""
        data = json.loads(f.read_text("utf-8"))
        holder = data.get("holder")
        ts = float(data.get("ts") or 0.0)
        if holder and holder != _HOLDER_ID and (time.time() - ts) < stale_sec:
            return str(data.get("pid") or holder)
        return ""
    except Exception:
        return ""


def release(key: str) -> None:
    """Best-effort release on clean shutdown / account switch, so a sibling can
    claim immediately instead of waiting out the staleness window. Only removes a
    lock we actually hold."""
    if not key:
        return
    try:
        f = _lock_file(key)
        if f.exists():
            data = json.loads(f.read_text("utf-8"))
            if data.get("holder") == _HOLDER_ID:
                f.unlink()
    except Exception:
        pass
