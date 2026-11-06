"""The engine EXIT paths, pinned.

An exit failing is worse than an entry failing: you are already in the
position. These tests guard the two ways that goes wrong — an exit that
silently gives up, and an exit that retries so hard it becomes a problem of
its own.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import crypto15m_trader as c15
import db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "exit.db")
    db.init_db()
    c15._exit_fail_at.clear()
    c15._stop_retry_at.clear()
    return tmp_path


def _pos(pid=1):
    return {"id": pid, "ticker": "0xabc", "direction": "up", "asset": "BTC",
            "filled_contracts": 10}


def _wire(monkeypatch, fail):
    """Stub the order submit + the DB writes _place_exit_sell performs."""
    calls = []

    async def _place(**kw):
        calls.append(kw)
        if fail:
            raise RuntimeError("size below the market minimum")
        return {"order": {"order_id": "OID", "status": "live"}}

    monkeypatch.setattr(c15.polymarket_api, "place_limit_order", _place)
    monkeypatch.setattr(c15.db, "update_crypto15m_position",
                        lambda *a, **k: None)
    monkeypatch.setattr(c15.db, "fetch_crypto15m_by_id",
                        lambda *a, **k: {"id": 1})
    return calls


def test_a_failed_exit_backs_off_instead_of_retrying_every_tick(fresh, monkeypatch):
    """Take-profit and sell-strength had NO throttle: a deterministic refusal
    re-submitted a real order on every 4s tick — roughly 900 attempts an hour
    against the CLOB for an exit that can never succeed."""
    calls = _wire(monkeypatch, fail=True)
    for _ in range(5):
        _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="take_profit"))
    assert len(calls) == 1, f"a doomed exit was re-submitted {len(calls)} times"


def test_the_backoff_expires_so_the_exit_is_retried(fresh, monkeypatch):
    calls = _wire(monkeypatch, fail=True)
    _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="take_profit"))
    # Pretend the cooldown elapsed — the position is still open, so it MUST
    # try again rather than give up on the exit entirely.
    c15._exit_fail_at[1] = time.time() - c15._EXIT_RETRY_SEC - 1
    _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="take_profit"))
    assert len(calls) == 2


def test_the_backoff_is_per_position(fresh, monkeypatch):
    calls = _wire(monkeypatch, fail=True)
    _run(c15._place_exit_sell(_pos(1), {}, exit_cents=50, reason="stop_loss"))
    _run(c15._place_exit_sell(_pos(2), {}, exit_cents=50, reason="stop_loss"))
    assert len(calls) == 2, "one position's failure blocked another's exit"


def test_a_successful_exit_clears_the_backoff(fresh, monkeypatch):
    calls = _wire(monkeypatch, fail=True)
    _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="take_profit"))
    assert 1 in c15._exit_fail_at
    _wire(monkeypatch, fail=False)
    c15._exit_fail_at[1] = time.time() - c15._EXIT_RETRY_SEC - 1
    _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="take_profit"))
    assert 1 not in c15._exit_fail_at, "the cooldown survived a successful exit"


def test_a_failed_exit_is_logged(fresh, monkeypatch, caplog):
    """It wrote an `error` field to the row and said nothing anywhere else —
    no log line, and the UI styles rows on `status`, not on `error`. So a
    failed exit was invisible while the position stayed open."""
    _wire(monkeypatch, fail=True)
    with caplog.at_level("WARNING"):
        _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="stop_loss"))
    assert any("sell FAILED" in r.message or "sell FAILED" in r.getMessage()
               for r in caplog.records), "a failed exit produced no log line"


def test_a_successful_exit_is_not_throttled(fresh, monkeypatch):
    calls = _wire(monkeypatch, fail=False)
    for _ in range(3):
        _run(c15._place_exit_sell(_pos(), {}, exit_cents=50, reason="take_profit"))
    assert len(calls) == 3, "a working exit got delayed by the failure cooldown"


# ────────────────────────────────────────────────────────────────────────
# Cost discipline in the paper simulator.
# ────────────────────────────────────────────────────────────────────────


def _paper_rows(monkeypatch, n_orders, n_markets):
    """n resting paper orders spread across n_markets distinct markets, with the
    market having traded through every one of them."""
    import terminal_paper as tp
    rows = [{
        "id": i, "condition_id": f"0x{i % n_markets}", "side": "yes",
        "action": "buy", "resting_size": 10.0, "requested_price_cents": 50.0,
        "filled_size": 0.0, "avg_fill_price_cents": None,
    } for i in range(n_orders)]

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, *a):
            return rows if sql.strip().startswith("SELECT") else None
    monkeypatch.setattr(tp, "_conn", lambda: _Conn())

    fetched = []

    async def _book(cid, side="yes"):
        fetched.append((cid, side))
        return {"available": True, "bestAsk": 40.0, "bestBid": 38.0,
                "asks": [], "bids": []}

    async def _marks(ids):
        return [{"conditionId": c, "bid": 38.0, "ask": ask} for c in ids]
    ask = 40.0
    monkeypatch.setattr(tp.term, "book", _book)
    monkeypatch.setattr(tp.term, "marked_summaries", _marks)
    return tp, fetched


def test_paper_fetches_one_book_per_market_not_per_order():
    """Ten resting orders on one market used to mean ten identical book reads
    every tick. Paper trading costs nothing; it must not cost the terminal its
    rate budget either."""
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        tp, fetched = _paper_rows(mp, n_orders=10, n_markets=1)
        _run(tp._fill_resting())
        assert len(fetched) == 1, f"fetched the same book {len(fetched)} times"
    finally:
        mp.undo()


def test_paper_book_fetches_are_capped_per_tick():
    """Unbounded, enough resting paper orders would park the book route on a 429
    (about 5 reads per 10 seconds per IP) and starve the REAL chart and book."""
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        tp, fetched = _paper_rows(mp, n_orders=60, n_markets=60)
        _run(tp._fill_resting())
        assert len(fetched) <= tp._MAX_BOOKS_PER_TICK, \
            f"{len(fetched)} book fetches in one tick"
    finally:
        mp.undo()


def test_a_resting_order_the_market_has_not_crossed_costs_no_book_read():
    """The book route is the scarce one, and most resting orders are not crossed
    on most ticks. The market read says so without spending it."""
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        tp, fetched = _paper_rows(mp, n_orders=5, n_markets=5)

        async def _marks(ids):
            return [{"conditionId": c, "bid": 58.0, "ask": 60.0} for c in ids]
        mp.setattr(tp.term, "marked_summaries", _marks)
        _run(tp._fill_resting())
        assert fetched == []
    finally:
        mp.undo()


def test_more_resting_markets_than_a_tick_reads_take_turns():
    """A cap with no rotation would leave the markets past it never checked."""
    import pytest as _p
    mp = _p.MonkeyPatch()
    try:
        tp, _fetched = _paper_rows(mp, n_orders=30, n_markets=30)
        mp.setattr(tp, "_touch_cursor", 0)
        seen = []

        async def _marks(ids):
            seen.extend(ids)
            return []
        mp.setattr(tp.term, "marked_summaries", _marks)
        _run(tp._fill_resting())
        assert len(seen) == tp._MAX_TOUCHES_PER_TICK
        _run(tp._fill_resting())
        assert len(set(seen)) == 30, "the markets past the cap were never read"
    finally:
        mp.undo()
