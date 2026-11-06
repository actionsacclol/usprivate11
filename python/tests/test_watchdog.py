"""Trading-loop watchdog: a stalled loop (stale heartbeat) must be force-
restarted so open positions never stop being managed."""
import asyncio

import pytest

import service


def test_loop_watchdog_restarts_stalled_loop(monkeypatch):
    # Tiny thresholds so the test runs in well under a second.
    monkeypatch.setattr(service, "_WATCHDOG_CHECK_SEC", 0.02)
    monkeypatch.setattr(service, "_LOOP_STALL_SEC", 0.05)

    async def _noop_emit(*a, **k):
        return None
    monkeypatch.setattr(service, "emit_event", _noop_emit)

    restarts = {"n": 0}

    async def _dummy_loop():
        # The restarted (healthy) loop: keeps its heartbeat fresh until stopped.
        restarts["n"] += 1
        while not (service._loop_stop and service._loop_stop.is_set()):
            service._loop_heartbeat = asyncio.get_event_loop().time()
            await asyncio.sleep(0.01)
    monkeypatch.setattr(service, "_scanner_and_trader_loop", _dummy_loop)

    async def _run():
        loop = asyncio.get_event_loop()
        service._loop_stop = asyncio.Event()

        async def _stuck():
            await asyncio.sleep(60)   # running but NEVER stamps the heartbeat

        stuck = loop.create_task(_stuck())
        service._loop_task = stuck
        service._loop_heartbeat = 1e-6   # stamped at the dawn of time: stale anywhere

        wd = loop.create_task(service._loop_watchdog())
        # Poll for the restart instead of a fixed sleep: 0.25s was usually
        # plenty (check 0.02s / stall 0.05s) but flaked on starved CI runners —
        # and a missed window then blocked on the stuck task's 60s sleep.
        for _ in range(400):          # up to ~4s
            await asyncio.sleep(0.01)
            if service._loop_task is not stuck and restarts["n"] >= 1:
                break
        new = service._loop_task

        service._loop_stop.set()
        wd.cancel()
        await asyncio.gather(wd, new, stuck, return_exceptions=True)
        return stuck, new

    stuck, new = asyncio.run(_run())
    assert stuck.cancelled() or stuck.done()   # the stalled loop was cancelled
    assert new is not stuck                     # replaced by a fresh task
    assert restarts["n"] >= 1                   # the loop actually restarted


def test_loop_watchdog_leaves_healthy_loop_alone(monkeypatch):
    monkeypatch.setattr(service, "_WATCHDOG_CHECK_SEC", 0.02)
    monkeypatch.setattr(service, "_LOOP_STALL_SEC", 0.05)

    spawned = {"n": 0}

    async def _dummy_loop():
        spawned["n"] += 1
        while not (service._loop_stop and service._loop_stop.is_set()):
            await asyncio.sleep(0.01)
    monkeypatch.setattr(service, "_scanner_and_trader_loop", _dummy_loop)

    async def _run():
        loop = asyncio.get_event_loop()
        service._loop_stop = asyncio.Event()

        async def _healthy():
            # progressing normally: stamps the heartbeat continuously
            while not (service._loop_stop and service._loop_stop.is_set()):
                service._loop_heartbeat = asyncio.get_event_loop().time()
                await asyncio.sleep(0.005)

        healthy = loop.create_task(_healthy())
        service._loop_task = healthy
        service._loop_heartbeat = loop.time()

        wd = loop.create_task(service._loop_watchdog())
        await asyncio.sleep(0.2)
        same = service._loop_task is healthy
        service._loop_stop.set()
        wd.cancel()
        await asyncio.gather(wd, healthy, return_exceptions=True)
        return same

    same = asyncio.run(_run())
    assert same                  # healthy loop NOT restarted
    assert spawned["n"] == 0     # _scanner_and_trader_loop never re-spawned


# ── per-block ceilings ────────────────────────────────────────────────
# The master loop awaits ~18 engine blocks in sequence. Before `_block` a
# single wedged await ate the whole 240s watchdog window, so every OTHER
# engine (stop-loss, settlement, reconcile) stopped running too. A ceiling
# turns that into "one block skipped this tick".


def test_block_ceiling_skips_hung_block_and_next_block_still_runs():
    ran: list[str] = []

    async def _tick():
        # A wedged block: a 60s await under a 0.05s ceiling.
        try:
            async with service._block("hung", 0.05):
                await asyncio.sleep(60)
                ran.append("hung")
        except Exception as e:                     # must NOT look like a crash
            ran.append(f"raised:{e!r}")
        # …and the block AFTER it still runs, in the same tick.
        try:
            async with service._block("next", 5.0):
                await asyncio.sleep(0)
                ran.append("next")
        except Exception as e:
            ran.append(f"raised:{e!r}")

    asyncio.run(_tick())

    assert ran == ["next"]                          # hung skipped, next ran
    assert service._loop_block_ms["hung"] >= 40      # ~50ms ceiling, recorded
    assert service._loop_block_ms["hung"] < 5_000    # cut off, not run to 60s
    assert "next" in service._loop_block_ms


def test_block_records_duration_and_propagates_real_errors():
    async def _tick():
        with pytest.raises(ValueError):
            async with service._block("boom", 5.0):
                raise ValueError("engine blew up")

    asyncio.run(_tick())
    # A real error still reaches the block's own except clause (which logs it),
    # and the timing was recorded either way.
    assert "boom" in service._loop_block_ms
