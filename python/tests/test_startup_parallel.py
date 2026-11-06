"""Authed startup must not walk its independent REST calls in single file.

Boot used to be: cancel-all-resting → reconcile positions → refresh balance,
each awaited in turn — three serial round trips before the loop's first tick
(measured as the bulk of the pre-tick startup cost).

The cancel is a genuine prerequisite (it takes stale GTC orders off the book
and releases their collateral, which both of the others read), so it stays
first. The reconcile and the balance read consume nothing of each other's
results, so they go out together.
"""
from __future__ import annotations

import asyncio
import time

import service


def _stub_trader(monkeypatch, delay=0.05, *, fail=None):
    """Three stubbed round trips that each cost `delay` and record when they
    ran, so a test can see the shape of the call graph, not just its duration."""
    calls: list[str] = []
    spans: dict[str, tuple[float, float]] = {}

    def _mark(name):
        t0 = time.perf_counter()

        def _done():
            spans[name] = (t0, time.perf_counter())
        return _done

    async def _cancel(_reason="x"):
        calls.append("cancel:start")
        done = _mark("cancel")
        await asyncio.sleep(delay)
        done()
        calls.append("cancel:done")
        if fail == "cancel":
            raise RuntimeError("cancel boom")
        return 3

    async def _reconcile():
        calls.append("reconcile:start")
        done = _mark("reconcile")
        await asyncio.sleep(delay)
        done()
        if fail == "reconcile":
            raise RuntimeError("reconcile boom")
        return {"rescued": 1}, [{"id": 1}]

    async def _balance(_cfg, force=False):
        calls.append("balance:start")
        done = _mark("balance")
        await asyncio.sleep(delay)
        done()
        if fail == "balance":
            raise RuntimeError("balance boom")
        return 1234, 5678

    monkeypatch.setattr(service.trader, "cancel_all_resting_orders", _cancel)
    monkeypatch.setattr(service.trader, "reconcile_positions_with_polymarket",
                        _reconcile)
    monkeypatch.setattr(service.trader, "refresh_balance", _balance)
    return calls, spans


def test_the_independent_startup_calls_overlap(monkeypatch):
    """Reconcile + balance must be in flight AT THE SAME TIME, behind the one
    call they both depend on. Overlap is the structural property; the wall clock
    below is the budget (kept loose because a 50ms `asyncio.sleep` on Windows
    rounds up to the ~15.6ms scheduler tick)."""
    calls, spans = _stub_trader(monkeypatch, delay=0.05)

    t0 = time.perf_counter()
    reconcile_res, balance_res = asyncio.run(service._startup_authed_sync())
    elapsed = time.perf_counter() - t0

    assert reconcile_res == ({"rescued": 1}, [{"id": 1}])
    assert balance_res == (1234, 5678)

    # The cancel is a prerequisite: both of the others start only after it ends.
    assert calls.index("cancel:done") < calls.index("reconcile:start")
    assert calls.index("cancel:done") < calls.index("balance:start")

    (r_start, r_end), (b_start, b_end) = spans["reconcile"], spans["balance"]
    assert r_start < b_end and b_start < r_end, (
        "reconcile and the balance read did not overlap — still serial"
    )

    # Two hops, not three: 3 x 50ms serial is ~150ms before any slop.
    assert elapsed < 0.145, (
        f"startup is still walking its round trips: {elapsed * 1000:.0f}ms"
    )


def test_a_failed_balance_does_not_lose_the_reconcile(monkeypatch):
    _stub_trader(monkeypatch, delay=0.0, fail="balance")
    reconcile_res, balance_res = asyncio.run(service._startup_authed_sync())
    assert reconcile_res == ({"rescued": 1}, [{"id": 1}])
    assert isinstance(balance_res, Exception)


def test_a_failed_reconcile_does_not_lose_the_balance(monkeypatch):
    _stub_trader(monkeypatch, delay=0.0, fail="reconcile")
    reconcile_res, balance_res = asyncio.run(service._startup_authed_sync())
    assert isinstance(reconcile_res, Exception)
    assert balance_res == (1234, 5678)


def test_a_failed_cancel_still_lets_startup_continue(monkeypatch):
    _stub_trader(monkeypatch, delay=0.0, fail="cancel")
    reconcile_res, balance_res = asyncio.run(service._startup_authed_sync())
    assert reconcile_res == ({"rescued": 1}, [{"id": 1}])
    assert balance_res == (1234, 5678)
