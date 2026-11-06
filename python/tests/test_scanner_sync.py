"""Market sync must not be dead time on the master trading loop.

The universe refresh was the single longest thing `_scanner_and_trader_loop`
awaited: ~10 Gamma pages walked strictly serially with an unconditional 0.2s
sleep between them, then ~1,000 synchronous sqlite upserts on the event loop —
all of it blocking every OTHER engine (stop-loss, settlement, reconcile, copy).

These pin the three fixes: pipelined pages, upserts in a worker thread, and a
single-flight background task for the recurring pass.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import db
import polymarket_api as api
import scanner
import service


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "sync.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _slow_gamma(pages: int, per_page_sec: float, page_cap: int = 100):
    """Market list stand-in: `pages` pages, each costing `per_page_sec`."""
    total = pages * page_cap
    calls: list[int] = []

    async def _get(_url, params=None, **_kw):
        params = params or {}
        offset = int(params.get("offset") or 0)
        calls.append(offset)
        await asyncio.sleep(per_page_sec)
        n = max(0, min(api.MARKET_PAGE_MAX, total - offset))
        return {"markets": [{
            "question": f"market {offset + i}",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.5","0.5"]',
            "slug": f"market-{offset + i}",
            "status": "MARKET_STATUS_OPEN",
        } for i in range(n)]}

    return _get, calls


def test_sync_markets_writes_every_page(monkeypatch, fresh_db):
    get, _calls = _slow_gamma(pages=5, per_page_sec=0.0)
    monkeypatch.setattr(api, "_get", get)

    assert asyncio.run(scanner.sync_markets(max_pages=10)) == 500
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 500


def test_upserts_run_off_the_event_loop(monkeypatch, fresh_db):
    """The ~1,000 upserts must happen in a worker thread, not on the loop."""
    import threading

    get, _calls = _slow_gamma(pages=2, per_page_sec=0.0)
    monkeypatch.setattr(api, "_get", get)

    loop_thread: dict = {}

    real_write = scanner._write_markets_blocking

    def _spy(raw, now_iso):
        loop_thread["writer"] = threading.current_thread().ident
        return real_write(raw, now_iso)

    monkeypatch.setattr(scanner, "_write_markets_blocking", _spy)

    async def _run():
        loop_thread["loop"] = threading.current_thread().ident
        return await scanner.sync_markets(max_pages=5)

    assert asyncio.run(_run()) == 200
    assert loop_thread["writer"] != loop_thread["loop"], (
        "market upserts still ran on the event loop thread"
    )


def test_overlapping_sync_requests_run_one_sync(monkeypatch):
    """Single-flight: a sync that overruns its interval must not stack up."""
    started = {"n": 0}
    release = None

    async def _slow_sync(*, max_pages: int = 10) -> int:
        started["n"] += 1
        await release.wait()
        return 7

    monkeypatch.setattr(service.scanner, "sync_markets", _slow_sync)

    async def _run():
        nonlocal release
        release = asyncio.Event()
        service._market_sync_task = None

        assert service._start_market_sync() is True      # first one starts
        await asyncio.sleep(0)                           # let it reach the wait
        assert service._start_market_sync() is False     # second is refused
        assert service._start_market_sync() is False

        release.set()
        await service._market_sync_task
        # Once it's done a fresh one may start again.
        release = asyncio.Event()
        release.set()
        assert service._start_market_sync() is True
        await service._market_sync_task

    asyncio.run(_run())
    assert started["n"] == 2, f"expected 2 syncs (not 4), got {started['n']}"


def test_background_sync_failure_never_reaches_the_loop(monkeypatch):
    """A wedged/failing Gamma must not take the trading loop down with it."""
    async def _boom(*, max_pages: int = 10) -> int:
        raise RuntimeError("gamma is down")

    monkeypatch.setattr(service.scanner, "sync_markets", _boom)

    async def _run():
        service._market_sync_task = None
        assert service._start_market_sync() is True
        await service._market_sync_task          # must not raise
        assert service._market_sync_task.exception() is None

    asyncio.run(_run())


def test_a_slow_market_sync_does_not_hold_the_master_loop(monkeypatch):
    """The recurring sync is detached: a Gamma that takes seconds must not
    stop the loop from reaching the blocks that manage open positions."""
    import contextlib

    sync_started = asyncio.Event()
    reached_later_block = {"n": 0}

    calls = {"n": 0}

    async def _glacial_sync(*, max_pages: int = 10) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0                     # the awaited STARTUP sync: instant
        sync_started.set()
        await asyncio.sleep(30)          # the RECURRING one: a wedged Gamma
        return 0

    async def _emit(_name, _data=None):
        return None

    async def _noop(*a, **k):
        return 0

    monkeypatch.setattr(service, "emit_event", _emit)
    monkeypatch.setattr(service.scanner, "sync_markets", _glacial_sync)
    monkeypatch.setattr(service.scanner, "sync_events", _noop)
    # The crypto research recorder reads spot prices and Gamma.
    monkeypatch.setattr(service.crypto15m_record, "record_tick", _noop)
    monkeypatch.setattr(service.STATE, "auth_ok", False, raising=False)
    monkeypatch.setattr(service.STATE, "paused", False, raising=False)
    monkeypatch.setattr(service.STATE, "cfg", {
        "main_record_signals": False,
        "enable_trading": False,
        "market_refresh_interval": 0,
    }, raising=False)

    # Watch a block that runs AFTER the market sync: reaching it while the sync
    # is still in flight is exactly the property under test.
    real_block = service._block

    @contextlib.asynccontextmanager
    async def _counting_block(name, ceiling_sec=service._BLOCK_CEILING_SEC):
        async with real_block(name, ceiling_sec):
            yield
        if name == "tradeScan":
            reached_later_block["n"] += 1

    monkeypatch.setattr(service, "_block", _counting_block)

    async def _run():
        service._market_sync_task = None
        service._loop_stop = asyncio.Event()
        task = asyncio.create_task(service._scanner_and_trader_loop())
        await asyncio.sleep(0.3)
        # The sync is STILL running (30s) while the loop has moved on.
        still_pending = bool(
            service._market_sync_task and not service._market_sync_task.done()
        )
        service._loop_stop.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if service._market_sync_task:
            service._market_sync_task.cancel()
            with contextlib.suppress(BaseException):
                await service._market_sync_task
        service._market_sync_task = None
        return still_pending

    still_pending = asyncio.run(_run())

    assert sync_started.is_set(), "the recurring sync never started"
    assert still_pending, "the sync finished; the test's stub was not slow"
    assert reached_later_block["n"] >= 1, (
        "the loop never got past the market sync — it is still awaited inline"
    )


def test_sync_events_writes_the_venues_event_list(monkeypatch, fresh_db):
    """fetch_events returns a plain list here. Reading it as (rows, cursor) crashed
    the master loop's sync block on every pass, and the retry ran every tick."""
    async def _get(_url, params=None, **_kw):
        return {"events": [{"slug": "mls-winner", "title": "MLS Champion", "category": "sports",
                            "seriesSlug": "mls-2025"},
                           {"title": "no slug, skipped"}]}

    monkeypatch.setattr(api, "_get", _get)
    assert asyncio.run(scanner.sync_events()) == 1
    with db.get_db() as c:
        row = c.execute("SELECT event_ticker, series_ticker, title FROM events").fetchone()
    assert tuple(row) == ("mls-winner", "mls-2025", "MLS Champion")
