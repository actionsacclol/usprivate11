"""The per-host gate: fast, but still disciplined.

The gate used to hold its lock across the WHOLE round trip plus the min-gap
sleep, so every request to a host was strictly serial. A Discover load that
needs ~20 market calls paid 20 × (RTT + 0.12s) — five to eight seconds of
skeletons for work that overlaps almost perfectly. The lock now covers only
the spacing DECISION (next allowed start, sleep the gap, stamp the start) and
a per-host semaphore bounds how many requests are actually in flight.

Speed is the easy half. The hard half is that nothing the gate exists for may
be lost in the process, so this file pins both sides:

  • overlap        — N slow requests cost far less than N × latency
  • spacing        — request STARTS are still ≥ min_gap apart (cost discipline)
  • the park       — a 429 parks the host, and a request during the park fails
                     FAST rather than queueing behind a doomed retry
  • the park holds — an in-flight request that lands after a 429 must not
                     clear or shorten the park
  • serial hosts   — the AI providers stay strictly one-at-a-time, because
                     their SDKs run with max_retries=0 and this park is the
                     single retry authority
  • the counters   — every request that STARTS is counted, so the privacy
                     panel's numbers stay true
  • single flight  — two identical concurrent requests still cost one call

Wall-clock assertions are deliberately generous: Windows' timer granularity is
~15.6ms, so an `asyncio.sleep(0.01)` really takes ~16ms. The bounds below are
still several times smaller than the serial behaviour they guard against.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import terminal as t


def _run(coro):
    """A brand new loop per test: the gate's lock and semaphore are created
    lazily and then bound to whichever loop first used them."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ── stubs ───────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status: int = 200, payload=None) -> None:
        self.status_code = status
        self._payload = [] if payload is None else payload

    def json(self):
        return self._payload


class _Stub:
    """A stand-in HTTP client that records when each request started and how
    many were in flight at once."""

    def __init__(self, delay: float = 0.05, status=200) -> None:
        self.delay = delay
        self._status = status
        self.starts: list[float] = []
        self.count = 0
        self.inflight = 0
        self.peak = 0

    def _status_for(self, n: int) -> int:
        return self._status(n) if callable(self._status) else self._status

    async def get(self, url, params=None, timeout=None):
        n = self.count
        self.count += 1
        self.starts.append(time.perf_counter())
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.inflight -= 1
        return _Resp(self._status_for(n))


@pytest.fixture
def stub(monkeypatch):
    """Installs a fake client and hands the test the stub it will see."""
    def _install(**kw) -> _Stub:
        s = _Stub(**kw)

        async def _get_client():
            return s

        monkeypatch.setattr(t.api, "_get_client", _get_client)
        return s

    return _install


@pytest.fixture
def gate():
    """A reset gate, with every field put back afterwards — `_gates` is module
    state the rest of the suite shares."""
    touched: dict[str, dict] = {}

    def _get(host: str, *, min_gap=None, concurrency=None) -> t._Gate:
        g = t._gates[host]
        touched.setdefault(host, {**g.__dict__, "_lat": list(g._lat)})
        g._lock = None
        g._slots = None
        g.last = 0.0
        g.parked_until = 0.0
        g.calls = g.errors = g.cache_hits = g.cache_misses = 0
        if min_gap is not None:
            g.min_gap = min_gap
        if concurrency is not None:
            g.concurrency = concurrency
        return g

    _cache_before = dict(t._cache)
    t._cache.clear()
    t._inflight.clear()
    yield _get
    for host, snap in touched.items():
        g = t._gates[host]
        g.__dict__.update(snap)
        g._lock = None
        g._slots = None
    t._cache.clear()
    t._cache.update(_cache_before)
    t._inflight.clear()


def _params(i: int) -> dict:
    """Distinct params so each request is a distinct cache key — the point of
    these tests is concurrency, not coalescing."""
    return {"limit": 1, "n": i}


# ── 1. overlap: N slow requests no longer cost N × latency ──────────────


def test_eight_slow_requests_overlap_instead_of_queueing(gate, stub):
    """The regression this whole change exists for. Serially this is
    8 × (50ms + gap) ≈ half a second; with the lock released before the call
    it is roughly two waves of 50ms."""
    g = gate("gateway", min_gap=0.01, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        started = time.perf_counter()
        await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(8)
        ])
        return time.perf_counter() - started

    elapsed = _run(scenario())
    serial = 8 * (0.05 + 0.01)

    # ~170ms on the dev box (measured: 604ms before this change, 171ms after).
    # The bound is generous for a loaded CI box and for Windows' 15.6ms timer
    # granularity, and still less than half the serial behaviour it guards.
    assert s.count == 8
    assert elapsed < 0.28, (
        f"8 × 50ms requests took {elapsed * 1000:.0f}ms — the gate is still "
        f"serialising the round trip (serial would be ~{serial * 1000:.0f}ms)")
    assert elapsed < serial * 0.6
    assert s.peak > 1, "no two requests were ever in flight together"


def test_in_flight_requests_are_bounded_by_the_host_concurrency(gate, stub):
    """'Not serial' must not become 'unbounded'. Twelve requests, four slots."""
    gate("gateway", min_gap=0.0, concurrency=4)
    s = stub(delay=0.02)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(12)
        ])

    _run(scenario())
    assert s.count == 12
    assert s.peak == 4, f"peak concurrency was {s.peak}, expected the 4-slot bound"


# ── 2. spacing: starts are still min_gap apart ──────────────────────────


def test_request_starts_are_still_spaced_by_min_gap(gate, stub):
    """Cost discipline. Overlapping the round trips is fine; machine-gunning
    the provider is not, and that distinction is the entire reason the lock
    still exists at all."""
    gap = 0.02
    gate("gateway", min_gap=gap, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(6)
        ])

    _run(scenario())
    assert len(s.starts) == 6
    diffs = [b - a for a, b in zip(s.starts, s.starts[1:])]
    # 2ms of slack for the clock read between stamping the start and the
    # stubbed call actually beginning.
    assert all(d >= gap - 0.002 for d in diffs), (
        f"starts were {[round(d * 1000, 1) for d in diffs]}ms apart, "
        f"min_gap is {gap * 1000:.0f}ms")


# ── 3. the park still fails FAST ────────────────────────────────────────


def test_a_429_parks_the_host_and_the_next_request_fails_fast(gate, stub):
    """The bug the park exists to prevent: a discovery burst 429s the
    provider and the doomed retries starve the chart, which renders blank
    while a plain curl to the same endpoint returns 200."""
    g = gate("gateway", min_gap=0.01, concurrency=4)
    s = stub(delay=0.01, status=429)

    async def scenario():
        data, _ = await t._fetch("gateway", "/v1/markets", _params(0), ttl=0.0)
        assert data is None
        assert g.parked(), "a 429 did not park the host"

        started = time.perf_counter()
        again, _ = await t._fetch("gateway", "/v1/markets", _params(1), ttl=0.0)
        return again, time.perf_counter() - started

    again, elapsed = _run(scenario())
    assert again is None
    assert elapsed < 0.02, f"the parked call took {elapsed * 1000:.1f}ms — it queued"
    assert s.count == 1, "a parked host still made a request"
    assert g.errors == 1


def test_a_parked_host_fails_fast_even_with_every_slot_busy(gate, stub):
    """The park is checked BEFORE the semaphore. Checking it after would mean
    a parked host with four requests on the wire made the fifth caller WAIT to
    be told no — which is the starvation the park exists to prevent."""
    g = gate("gateway", min_gap=0.0, concurrency=4)
    stub(delay=0.1)

    async def scenario():
        busy = [
            asyncio.ensure_future(t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0))
            for i in range(4)
        ]
        await asyncio.sleep(0.02)      # let all four take their slots
        g.park(30.0)
        started = time.perf_counter()
        data, _ = await t._fetch("gateway", "/v1/markets", _params(99), ttl=0.0)
        elapsed = time.perf_counter() - started
        await asyncio.gather(*busy)
        return data, elapsed

    data, elapsed = _run(scenario())
    assert data is None
    assert elapsed < 0.02, f"waited {elapsed * 1000:.1f}ms for a slot to be told no"


def test_an_in_flight_response_does_not_clear_or_shorten_the_park(gate, stub):
    """With several requests in flight, a 429 lands while others are still on
    the wire. Their completion — success or not — must not unpark the host,
    and a second 429 must not pull the expiry back in."""
    g = gate("gateway", min_gap=0.0, concurrency=4)
    # First request 429s quickly; the rest succeed later.
    s = stub(delay=0.02, status=lambda n: 429 if n == 0 else 200)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(4)
        ])

    _run(scenario())
    assert s.count == 4
    assert g.parked(), "a successful in-flight response cleared the park"
    assert g.parked_until - time.monotonic() > t._PARK_SECS - 1.0

    # And a later, shorter park cannot shorten a longer one already running.
    far = g.parked_until
    g.park(0.5)
    assert g.parked_until == far, "a second 429 pulled the park expiry back in"


# ── 4. the AI providers stay strictly serial ────────────────────────────


def test_the_ai_hosts_are_declared_serial():
    """Both SDKs are constructed with max_retries=0 so this park is the single
    retry authority. Letting several calls run at once would give a rate limit
    several chances to compound before the park lands."""
    for host in ("anthropic", "openai"):
        assert t._HOSTS[host]["concurrency"] == 1
        assert t._gates[host].concurrency == 1


def test_an_ai_host_runs_one_call_at_a_time(gate):
    """The real check: ai_analysis holds `gate.lock()` across its whole SDK
    call, so raising _fetch's concurrency must not have made these overlap."""
    import ai_analysis as ai

    g = gate("anthropic", min_gap=0.0, concurrency=1)
    state = {"inflight": 0, "peak": 0, "done": 0}

    async def one():
        async with ai._gate_call("anthropic"):
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
            await asyncio.sleep(0.02)
            state["inflight"] -= 1
            state["done"] += 1

    async def scenario():
        await asyncio.gather(*[one() for _ in range(4)])

    _run(scenario())
    assert state["done"] == 4
    assert state["peak"] == 1, "two AI provider calls were in flight at once"
    assert g.calls == 4


# ── 5. the counters the privacy panel reads ─────────────────────────────


def test_every_request_that_starts_is_counted(gate, stub):
    """The panel's claim is 'requests this machine made'. A call still in
    flight was still made, and one that failed on the wire was still made."""
    g = gate("gateway", min_gap=0.0, concurrency=4)
    s = stub(delay=0.01)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(7)
        ])

    _run(scenario())
    assert s.count == 7
    assert g.calls == 7, f"{s.count} requests were made but {g.calls} counted"
    assert g.errors == 0
    assert g.latency_ms() is not None


def test_a_transport_failure_is_still_a_call_and_an_error(gate, monkeypatch):
    g = gate("gateway", min_gap=0.0, concurrency=4)

    class _Boom:
        async def get(self, url, params=None, timeout=None):
            raise RuntimeError("socket died")

    async def _get_client():
        return _Boom()

    monkeypatch.setattr(t.api, "_get_client", _get_client)

    async def scenario():
        return await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(3)
        ])

    out = _run(scenario())
    assert out == [(None, None)] * 3
    assert g.calls == 3
    assert g.errors == 3
    assert not g.parked(), "a transport failure is not a rate limit"


def test_health_reports_the_calls_the_gate_counted(gate, stub):
    g = gate("gateway", min_gap=0.0, concurrency=4)
    # The book route is a second gate on the same host, and its row counts both.
    gate("gateway_book")
    stub(delay=0.0)

    async def scenario():
        await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", _params(i), ttl=0.0) for i in range(5)
        ])

    _run(scenario())
    row = next(r for r in t.health()["providers"]
               if r["host"] == t._HOSTS["gateway"]["host"])
    assert row["calls"] == 5 == g.calls
    assert row["errors"] == 0


# ── 6. identical concurrent requests still cost one call ────────────────


def test_identical_concurrent_requests_are_coalesced(gate, stub):
    """Discover's `volume` and `volatility` columns send byte-identical Gamma
    params, and so does a text search. The old gate deduped them only as a
    side effect of serialising; now it is explicit, or one page load pays
    three times for one answer."""
    g = gate("gateway", min_gap=0.0, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        return await asyncio.gather(*[
            t._fetch("gateway", "/v1/markets", {"limit": 1}, ttl=30.0) for _ in range(3)
        ])

    out = _run(scenario())
    assert s.count == 1, f"{s.count} identical concurrent requests hit the network"
    assert all(o == out[0] for o in out), "the coalesced callers got different answers"
    assert out[0][0] == []
    assert g.calls == 1
    assert g.cache_hits == 2


def test_a_cancelled_caller_does_not_strand_the_others(gate, stub):
    """The panel closes mid-request. The shared in-flight future is shielded,
    so the caller that walked away cannot cancel the request the others are
    riding on — and the leader always resolves it, so nobody hangs."""
    gate("gateway", min_gap=0.0, concurrency=4)
    s = stub(delay=0.05)

    async def scenario():
        first = asyncio.ensure_future(
            t._fetch("gateway", "/v1/markets", {"limit": 7}, ttl=30.0))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(
            t._fetch("gateway", "/v1/markets", {"limit": 7}, ttl=30.0))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        return await asyncio.wait_for(second, timeout=2.0)

    data, observed = _run(scenario())
    assert data == []
    assert observed is not None
    assert s.count == 1
    assert not t._inflight, "the in-flight registry leaked an entry"


def test_the_inflight_registry_is_empty_after_a_failure(gate, monkeypatch):
    """A leaked entry would make every later request for that key await a
    future nobody will ever resolve."""
    gate("gateway", min_gap=0.0, concurrency=4)

    class _Boom:
        async def get(self, url, params=None, timeout=None):
            raise RuntimeError("socket died")

    async def _get_client():
        return _Boom()

    monkeypatch.setattr(t.api, "_get_client", _get_client)
    assert _run(t._fetch("gateway", "/v1/markets", {"limit": 3}, ttl=30.0)) == (None, None)
    assert not t._inflight


# ── 7. the switches above the gate still come first ─────────────────────


def test_offline_still_short_circuits_before_any_slot_is_taken(gate, stub):
    g = gate("gateway", min_gap=0.0, concurrency=4)
    s = stub(delay=0.0)
    t.set_offline(True)
    try:
        assert _run(t._fetch("gateway", "/v1/markets", _params(0), ttl=0.0)) == (None, None)
    finally:
        t.set_offline(False)
    assert s.count == 0
    assert g.calls == 0
