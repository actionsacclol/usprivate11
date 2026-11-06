"""The crypto15m engine runs on its OWN task, off the master loop.

It used to be one of ~19 blocks the master loop awaited in sequence, so a crypto
tick (a 15-minute window about to close) waited behind the position poll and the
market sync: a 0-10 s variable delay in front of the one engine where latency IS
the edge.

What is pinned here:
  1. the loops tick INDEPENDENTLY: a blocked master iteration does not stop
     the crypto loop,
  2. the cross-engine order lock: two engines racing one shared cap place
     exactly one order,
  3. a crashed engine loop restarts itself, heartbeat and all,
  4. the watchdog restarts only the loop that stalled,
  5. disabling an engine in config idles its loop instead of needing a restart,
  6. shutdown reaps every engine task (nothing left pending).
"""
from __future__ import annotations

# Timing note: on Windows, Python 3.11's event-loop clock ticks every 15.6 ms,
# so asyncio.sleep(0.01) returns immediately and a "poll 400 times" loop burns
# through in zero seconds. Every sleep here is >= 0.02 s and the stall
# thresholds are 0.5 s so a heartbeat stamped every ~30 ms is never "stale".

import asyncio
import contextlib

import pytest

import clob_ws
import db
import index_feed
import service
import spot_ws
import trader


@pytest.fixture(autouse=True)
def _fresh_order_lock():
    """Every test here drives its own `asyncio.run`, and an asyncio.Lock binds
    to the loop that first CONTENDS it. Hand each test a clean one so the order
    gate isn't testing an artefact of test ordering."""
    trader.ORDER_LOCK = asyncio.Lock()
    yield
    trader.ORDER_LOCK = asyncio.Lock()


# ───────── shared harness ────────────────────────────────────────────

async def _quiet_emit(_name, _data=None):
    return None


def _silence_master(monkeypatch):
    """Neuter everything the master loop would reach for over the network."""
    async def _zero(*a, **k):
        return 0

    monkeypatch.setattr(service, "emit_event", _quiet_emit)
    monkeypatch.setattr(service.scanner, "sync_markets", _zero)
    monkeypatch.setattr(service.scanner, "sync_events", _zero)
    # The crypto research recorder reads spot prices and Gamma.
    monkeypatch.setattr(service.crypto15m_record, "record_tick", _zero)
    # No real sockets from a unit test. The loops' lifecycle blocks (spot and
    # index feeds, the book feed via warm_books) would otherwise open live
    # connections; the
    # websockets library's tasks then outlive the test and wedge asyncio.run's
    # shutdown on Python 3.11 (observed: transfer_data / close_connection
    # tasks still pending after the reap).
    for mod in (spot_ws, index_feed):
        monkeypatch.setattr(mod, "start", lambda *a, **k: None)
        monkeypatch.setattr(mod, "stop", _zero)
        monkeypatch.setattr(mod, "is_running", lambda: False)
    monkeypatch.setattr(clob_ws, "warm", lambda *a, **k: None)
    monkeypatch.setattr(clob_ws.feed, "start", lambda *a, **k: None)
    monkeypatch.setattr(clob_ws.feed, "stop", _zero)
    monkeypatch.setattr(service.STATE, "auth_ok", False, raising=False)
    monkeypatch.setattr(service.STATE, "paused", False, raising=False)


@contextlib.contextmanager
def _loop_globals():
    """Save/restore the module globals these tests drive directly, so a failure
    can never leak a live task or a stale heartbeat into another test."""
    keep = {
        k: getattr(service, k)
        for k in ("_loop_task", "_crypto_task", "_loop_stop", "_loop_heartbeat",
                  "_crypto_heartbeat", "_watchdog_task")
    }
    try:
        yield
    finally:
        for k, v in keep.items():
            setattr(service, k, v)
        service._loop_restart_state.clear()


async def _reap(*tasks) -> None:
    for t in tasks:
        if t is None:
            continue
        t.cancel()
    for t in tasks:
        if t is None:
            continue
        with contextlib.suppress(BaseException):
            await t
    # Sweep anything the loops spawned (feeds, detached syncs) so asyncio.run's
    # shutdown never waits on a task that ignores cancellation. asyncio.wait
    # with a timeout never awaits a stuck task itself.
    me = asyncio.current_task()
    strays = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
    for t in strays:
        t.cancel()
    if strays:
        await asyncio.wait(strays, timeout=5)


# ───────── 1. the loops tick independently ───────────────────────────


def test_the_crypto_loop_keeps_ticking_while_the_master_loop_is_blocked(monkeypatch):
    _silence_master(monkeypatch)
    monkeypatch.setattr(service.STATE, "auth_ok", True, raising=False)

    async def _glacial_sync(*a, **k):
        await asyncio.sleep(2.0)
        return 0
    monkeypatch.setattr(service.scanner, "sync_markets", _glacial_sync)

    # Everything else the master loop would do while authed.
    async def _nothing(*a, **k):
        return []
    for name in ("scan_for_trades", "poll_open_orders", "take_profit_sweep",
                 "detect_external_exits", "mark_resolved_positions"):
        monkeypatch.setattr(service.trader, name, _nothing)

    async def _reconcile():
        return {}, []
    monkeypatch.setattr(
        service.trader, "reconcile_positions_with_polymarket", _reconcile)

    ticks = {"n": 0}

    async def _crypto_tick(_cfg, authed=False):
        ticks["n"] += 1
        return []
    monkeypatch.setattr(service.crypto15m_trader, "run_tick", _crypto_tick)

    monkeypatch.setattr(service.STATE, "cfg", {
        "enable_trading": False,
        "crypto15m_poll_sec": 0,
        "crypto15m_spot_ws": False,
    }, raising=False)

    async def _run():
        service._loop_stop = asyncio.Event()
        master = asyncio.create_task(service._scanner_and_trader_loop())
        crypto = asyncio.create_task(service._crypto15m_loop())
        await asyncio.sleep(2.4)
        service._loop_stop.set()
        await _reap(master, crypto)

    with _loop_globals():
        asyncio.run(_run())

    assert ticks["n"] >= 2, f"crypto ticked only {ticks['n']}x"


def test_each_loop_reports_its_own_blocks_namespaced(monkeypatch):
    """`loopBlocksMs` is the "which block is expensive" diagnostic. With more than
    one loop it has to say WHICH loop, or the numbers are meaningless."""
    _silence_master(monkeypatch)
    monkeypatch.setattr(service.STATE, "auth_ok", True, raising=False)

    async def _tick(_cfg, authed=False):
        return []
    monkeypatch.setattr(service.crypto15m_trader, "run_tick", _tick)
    monkeypatch.setattr(service.STATE, "cfg", {
        "enable_trading": False,
        "crypto15m_poll_sec": 0,
        "crypto15m_spot_ws": False,
    }, raising=False)

    service._loop_block_ms.clear()

    async def _run():
        service._loop_stop = asyncio.Event()
        tasks = [
            asyncio.create_task(service._scanner_and_trader_loop()),
            asyncio.create_task(service._crypto15m_loop()),
        ]
        # Wait for every loop to report at least once instead of a fixed sleep.
        want = {"crypto15m:tick", "crypto15m:feeds", "marketSync"}
        for _ in range(500):
            if want <= set(service._loop_block_ms):
                break
            await asyncio.sleep(0.02)
        service._loop_stop.set()
        await _reap(*tasks)

    with _loop_globals():
        asyncio.run(_run())

    seen = set(service._loop_block_ms)
    assert "crypto15m:tick" in seen and "crypto15m:feeds" in seen
    assert "marketSync" in seen, "the master loop's blocks are not namespaced"


# ───────── 2. the cross-engine order lock ────────────────────────────

def test_two_engines_racing_one_shared_cap_place_exactly_one_order():
    """Interleaved, both engines read the same headroom before either row
    exists. `trader.order_gate` is what stops both from placing."""
    placed: list[str] = []
    exposure = {"usd": 0.0}
    cap = 10.0
    cost = 10.0

    async def _engine(name: str, *, gated: bool):
        async def _body():
            # check the shared cap …
            if exposure["usd"] + cost > cap + 1e-9:
                return
            await asyncio.sleep(0.05)     # … the order round trip …
            exposure["usd"] += cost       # … and the row the OTHER engine reads
            placed.append(name)
        if gated:
            async with trader.order_gate(name):
                await _body()
        else:
            await _body()

    async def _race(gated: bool):
        placed.clear()
        exposure["usd"] = 0.0
        await asyncio.gather(
            _engine("scripts", gated=gated), _engine("crypto15m", gated=gated))
        return list(placed)

    # Control: unserialised, BOTH engines pass the cap and both place.
    assert len(asyncio.run(_race(False))) == 2

    # Gated: exactly one order, and the loser saw the winner's exposure.
    won = asyncio.run(_race(True))
    assert len(won) == 1, won
    assert won[0] in ("scripts", "crypto15m")


def test_the_order_gate_fails_open_rather_than_blocking_trading(monkeypatch):
    """A lock that could wedge every engine would be worse than the race it
    fixes, so a waiter that times out proceeds unserialised."""
    monkeypatch.setattr(trader, "ORDER_LOCK_TIMEOUT_SEC", 0.05)
    ran = {"n": 0}

    async def _run():
        async def _hog():
            async with trader.order_gate("hog"):
                await asyncio.sleep(0.4)

        async def _waiter():
            async with trader.order_gate("waiter") as serialised:
                ran["n"] += 1
                return serialised

        hog = asyncio.create_task(_hog())
        await asyncio.sleep(0.02)
        serialised = await _waiter()
        await hog
        return serialised

    serialised = asyncio.run(_run())
    assert ran["n"] == 1, "the waiter must still place its order"
    assert serialised is False, "…and must report that it ran unserialised"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-loop-split.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _seed_position(n: int, *, contracts: int, cents: int, status: str) -> int:
    with db.get_db() as conn:
        return db.insert_bot_position(conn, {
            "signal_source": "whale", "signal_id": 900_000 + n,
            "ticker": f"LOCK-{n}", "event_ticker": "", "direction": "yes",
            "target_contracts": contracts, "limit_price_cents": cents,
            "filled_contracts": contracts if status == "filled" else 0,
            "cost_usd": contracts * cents / 100.0 if status == "filled" else 0.0,
            "client_order_id": f"lock-co-{n}", "order_id": f"oid-{n}",
            "status": status, "network": trader.get_env(),
        })


def test_the_headroom_recheck_sees_the_other_engines_fresh_order(fresh_db):
    """Scripts and crypto snapshot their budget once per tick. The re-check inside
    the gate reads exposure from the DB at the moment of placement, so an order
    the other engine inserted a millisecond ago already counts against it."""
    env = trader.get_env()
    cfg = {"max_total_exposure_fraction": 0.5, "min_cash_reserve_fraction": 0.0}

    # $100 cash, 50% exposure cap, nothing open → $50 of headroom.
    assert trader.shared_headroom_usd(cfg, env, 100.0) == pytest.approx(50.0)
    assert trader.headroom_blocks(cfg, env, 100.0, 40.0) == ""

    # Another engine just placed a $45 resting order.
    _seed_position(1, contracts=90, cents=50, status="submitted")
    why = trader.headroom_blocks(cfg, env, 100.0, 40.0)
    assert why and "account-wide cap" in why


def test_the_headroom_recheck_never_clamps_on_an_unknown_balance(fresh_db):
    """Every engine treats balance<=0 as "unknown, don't clamp" (a failed read
    must not look like an empty wallet). This guard must not be the one place
    that changes that."""
    env = trader.get_env()
    cfg = {"max_total_exposure_fraction": 0.01, "min_cash_reserve_fraction": 0.9}
    assert trader.headroom_blocks(cfg, env, 0.0, 500.0) == ""


# ───────── 3. a crashed loop restarts itself ─────────────────────────

def test_a_crashed_crypto_loop_restarts_and_its_heartbeat_resumes(monkeypatch):
    starts = {"n": 0}

    async def _flaky_body():
        starts["n"] += 1
        if starts["n"] == 1:
            raise RuntimeError("crypto engine blew up")
        while not (service._loop_stop and service._loop_stop.is_set()):
            service._crypto_heartbeat = asyncio.get_event_loop().time()
            await asyncio.sleep(0.02)
    monkeypatch.setattr(service, "_crypto15m_loop_body", _flaky_body)

    async def _run():
        service._loop_stop = asyncio.Event()
        service._crypto_heartbeat = 0.0
        task = asyncio.create_task(service._crypto15m_loop())
        for _ in range(400):              # up to ~4s; the first backoff is 1s
            await asyncio.sleep(0.02)
            if starts["n"] >= 2 and service._crypto_heartbeat > 0:
                break
        alive = not task.done()
        stamped = service._crypto_heartbeat
        service._loop_stop.set()
        await _reap(task)
        return alive, stamped

    with _loop_globals():
        alive, stamped = asyncio.run(_run())

    assert starts["n"] >= 2, "the crypto loop did not restart after crashing"
    assert stamped > 0, "its heartbeat never resumed"
    assert alive, "the supervising task died with the loop body"


def test_the_supervisor_keeps_the_heartbeat_fresh_across_its_backoff(monkeypatch):
    """The backoff sleep must not look like a stall, or the watchdog would
    restart a loop that is already deliberately waiting."""
    async def _always_crashes():
        raise RuntimeError("nope")
    monkeypatch.setattr(service, "_crypto15m_loop_body", _always_crashes)

    async def _run():
        service._loop_stop = asyncio.Event()
        service._crypto_heartbeat = 0.0
        task = asyncio.create_task(service._crypto15m_loop())
        await asyncio.sleep(0.3)          # mid-backoff
        stamped = service._crypto_heartbeat
        service._loop_stop.set()
        await _reap(task)
        return stamped

    with _loop_globals():
        stamped = asyncio.run(_run())
    assert stamped > 0


# ───────── 4. the watchdog restarts only the stalled loop ────────────

def test_the_watchdog_restarts_only_the_stalled_loop(monkeypatch):
    monkeypatch.setattr(service, "_WATCHDOG_CHECK_SEC", 0.02)
    monkeypatch.setattr(service, "_LOOP_STALL_SEC", 0.5)
    monkeypatch.setattr(service, "_ENGINE_STALL_SEC", 0.5)
    monkeypatch.setattr(service, "emit_event", _quiet_emit)

    async def _no_close():
        return None
    monkeypatch.setattr(service.polymarket_api, "close_clients", _no_close)
    monkeypatch.setattr(service.crypto15m, "close_clients", _no_close)

    respawns = {"master": 0, "crypto": 0}

    async def _fresh_crypto():
        respawns["crypto"] += 1
        while not (service._loop_stop and service._loop_stop.is_set()):
            service._crypto_heartbeat = asyncio.get_event_loop().time()
            await asyncio.sleep(0.02)
    monkeypatch.setattr(service, "_crypto15m_loop", _fresh_crypto)

    async def _fresh_master():
        respawns["master"] += 1
        await asyncio.sleep(60)
    monkeypatch.setattr(service, "_scanner_and_trader_loop", _fresh_master)

    async def _run():
        loop = asyncio.get_event_loop()
        service._loop_stop = asyncio.Event()

        async def _healthy():
            while not (service._loop_stop and service._loop_stop.is_set()):
                service._loop_heartbeat = loop.time()
                await asyncio.sleep(0.02)

        async def _wedged():
            await asyncio.sleep(60)       # alive, never stamps its heartbeat

        master = loop.create_task(_healthy())
        stuck = loop.create_task(_wedged())
        service._loop_task, service._crypto_task = master, stuck
        service._loop_heartbeat = loop.time()
        service._crypto_heartbeat = 1e-6   # stamped at the dawn of time: stale anywhere

        wd = loop.create_task(service._loop_watchdog())
        for _ in range(400):
            await asyncio.sleep(0.02)
            if service._crypto_task is not stuck and respawns["crypto"] >= 1:
                break
        out = (service._crypto_task is not stuck, service._loop_task is master)
        service._loop_stop.set()
        await _reap(wd, master, stuck, service._crypto_task)
        return out

    with _loop_globals():
        replaced, master_kept = asyncio.run(_run())

    assert replaced, "the stalled crypto loop was not restarted"
    assert master_kept, "a healthy loop was restarted too"
    assert respawns["crypto"] >= 1 and respawns["master"] == 0


def test_the_watchdog_respawns_a_loop_whose_task_simply_ended(monkeypatch):
    """A task that ran to completion (an unhandled BaseException outside the
    per-block try/except) leaves that engine dead forever with no recovery."""
    monkeypatch.setattr(service, "_WATCHDOG_CHECK_SEC", 0.02)
    monkeypatch.setattr(service, "_ENGINE_STALL_SEC", 999.0)
    monkeypatch.setattr(service, "emit_event", _quiet_emit)

    async def _no_close():
        return None
    monkeypatch.setattr(service.polymarket_api, "close_clients", _no_close)
    monkeypatch.setattr(service.crypto15m, "close_clients", _no_close)

    spawned = {"n": 0}

    async def _fresh_crypto():
        spawned["n"] += 1
        while not (service._loop_stop and service._loop_stop.is_set()):
            service._crypto_heartbeat = asyncio.get_event_loop().time()
            await asyncio.sleep(0.02)
    monkeypatch.setattr(service, "_crypto15m_loop", _fresh_crypto)

    async def _run():
        loop = asyncio.get_event_loop()
        service._loop_stop = asyncio.Event()
        service._loop_task = None

        async def _dies():
            return None
        dead = loop.create_task(_dies())
        await asyncio.sleep(0.02)
        service._crypto_task = dead
        service._crypto_heartbeat = loop.time()

        wd = loop.create_task(service._loop_watchdog())
        for _ in range(400):
            await asyncio.sleep(0.02)
            if service._crypto_task is not dead:
                break
        replaced = service._crypto_task is not dead
        await asyncio.sleep(0.05)     # let the respawned loop actually start
        service._loop_stop.set()
        await _reap(wd, service._crypto_task)
        return replaced

    with _loop_globals():
        replaced = asyncio.run(_run())
    assert replaced and spawned["n"] >= 1


# ───────── 5. config changes need no restart ─────────────────────────


def test_the_crypto_loop_idles_without_auth_and_resumes_when_it_lands(monkeypatch):
    _silence_master(monkeypatch)
    ticks = {"n": 0}

    async def _tick(_cfg, authed=False):
        ticks["n"] += 1
        return []
    monkeypatch.setattr(service.crypto15m_trader, "run_tick", _tick)
    monkeypatch.setattr(service.STATE, "cfg", {
        "crypto15m_poll_sec": 0,
        "crypto15m_spot_ws": False,
    }, raising=False)
    monkeypatch.setattr(service.STATE, "auth_ok", False, raising=False)

    async def _run():
        service._loop_stop = asyncio.Event()
        task = asyncio.create_task(service._crypto15m_loop())
        await asyncio.sleep(0.5)
        before = ticks["n"]
        service.STATE.auth_ok = True
        await asyncio.sleep(1.2)
        after = ticks["n"]
        service._loop_stop.set()
        await _reap(task)
        return before, after

    with _loop_globals():
        before, after = asyncio.run(_run())

    assert before == 0, "no wallet — nothing to place and nothing to manage"
    assert after >= 1, "the engine did not pick auth up without a restart"


# ───────── 6. shutdown reaps every loop ──────────────────────────────

def test_stop_loop_reaps_every_engine_task(monkeypatch):
    """`_stop_loop` runs on factory reset, clear-history and shutdown. A task
    still pending at process exit is what prints 'Task was destroyed but it is
    pending!' — and, worse, keeps trading through a factory reset."""
    async def _idle():
        while not (service._loop_stop and service._loop_stop.is_set()):
            await asyncio.sleep(0.02)
    monkeypatch.setattr(service, "_scanner_and_trader_loop", _idle)
    monkeypatch.setattr(service, "_crypto15m_loop", _idle)

    async def _run():
        await service._start_loop()
        tasks = [service._loop_task, service._crypto_task]
        assert all(t is not None and not t.done() for t in tasks)
        await service._stop_loop()
        cleared = (service._loop_task, service._crypto_task, service._watchdog_task)
        leftovers = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        return tasks, cleared, leftovers

    with _loop_globals():
        tasks, cleared, leftovers = asyncio.run(_run())

    assert all(t.done() for t in tasks), "a loop task survived _stop_loop"
    assert cleared == (None, None, None), (
        "_stop_loop must clear the task handles so _start_loop can restart")
    assert leftovers == [], f"tasks left pending after shutdown: {leftovers}"


def test_stop_loop_cancels_a_loop_that_ignores_the_stop_event(monkeypatch):
    """A wedged loop must not hold shutdown open past its grace window."""
    async def _wedged():
        await asyncio.sleep(300)
    monkeypatch.setattr(service, "_scanner_and_trader_loop", _wedged)
    monkeypatch.setattr(service, "_crypto15m_loop", _wedged)
    # Shrink the grace window so the test doesn't sit out three 5s waits.
    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(aw, timeout=None):
        return await real_wait_for(aw, 0.05 if (timeout or 0) >= 2 else timeout)
    monkeypatch.setattr(service.asyncio, "wait_for", _fast_wait_for)

    async def _run():
        await service._start_loop()
        tasks = [service._loop_task, service._crypto_task]
        await service._stop_loop()
        return tasks

    with _loop_globals():
        tasks = asyncio.run(_run())

    assert all(t.done() for t in tasks)
