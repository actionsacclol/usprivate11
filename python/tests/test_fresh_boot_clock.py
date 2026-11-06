"""A freshly booted machine has a SMALL monotonic clock, pinned.

time.monotonic() counts from boot. Every `.get(key, 0.0)` default against a
monotonic "last miss" timestamp therefore reads as "missed a moment ago" for
the first TTL seconds after boot — on every CI runner, and on every user's
PC for the first minutes of a session. The ledger resolved nothing in that
window and the whole settled record read "unknown". Found because GitHub's
runners are fresh VMs and the local dev box had been up for days.
"""
from __future__ import annotations

import asyncio

import terminal_trade as tt


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_a_market_never_looked_up_is_looked_up_even_seconds_after_boot(monkeypatch):
    monkeypatch.setattr(tt.time, "monotonic", lambda: 12.0)   # booted 12 s ago
    tt._resolution_misses.clear()
    calls: list[list[str]] = []

    async def _summaries(ids):
        calls.append(list(ids))
        return [{"conditionId": c, "status": "settled", "resolvedOutcome": "yes",
                 "outcomes": ["Up", "Down"], "closeTime": "2026-01-01T00:00:00Z",
                 "title": "T", "slug": None, "probability": 50.0} for c in ids]
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt, "_resolutions_cached", lambda: {})
    monkeypatch.setattr(tt, "_resolutions_remember", lambda *a, **k: None, raising=False)

    out = _run(tt._resolutions_for(["0xfresh"]))
    assert calls == [["0xfresh"]], "a never-seen market was treated as a recent miss"
    assert out.get("0xfresh") == "yes"


def test_a_real_recent_miss_is_still_throttled(monkeypatch):
    monkeypatch.setattr(tt.time, "monotonic", lambda: 12.0)
    tt._resolution_misses.clear()
    tt._resolution_misses["0xmissed"] = 10.0          # missed 2 s ago
    calls: list[list[str]] = []

    async def _summaries(ids):
        calls.append(list(ids))
        return []
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt, "_resolutions_cached", lambda: {})

    _run(tt._resolutions_for(["0xmissed"]))
    assert calls == [], "a miss two seconds old was retried"


def test_miss_expiry_rule():
    assert tt._miss_expired(None, 5.0) is True
    assert tt._miss_expired(0.0, 5.0) is False           # a REAL miss at t=0
    assert tt._miss_expired(0.0, tt._RESOLUTION_MISS_TTL + 1.0) is True
