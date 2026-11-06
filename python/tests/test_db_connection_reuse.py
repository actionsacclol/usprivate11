"""`db.get_db()` reuses one connection per worker thread.

Every one of these asserts a property the 148 `with db.get_db() as conn:` call
sites already relied on and must keep: commit on a clean exit, rollback on any
exception, an isolated connection per thread, and a fresh connection whenever
`db_path()` moves. The reuse itself is the only new behaviour.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "reuse.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.close_thread_connection()
    db.init_db()
    yield dbfile
    db.close_thread_connection()


# ── reuse ──────────────────────────────────────────────────────────────


def test_two_calls_on_one_thread_share_the_connection():
    with db.get_db() as a:
        first = a
    with db.get_db() as b:
        assert b is first


def test_a_different_thread_gets_a_different_connection():
    seen: dict[str, int] = {}

    def grab(key):
        def run():
            with db.get_db() as conn:
                seen[key] = id(conn)
            db.close_thread_connection()
        return run

    with db.get_db() as conn:
        main = id(conn)
    t = threading.Thread(target=grab("worker"))
    t.start()
    t.join()
    assert seen["worker"] != main


def test_a_db_path_change_invalidates_the_cache(tmp_path, monkeypatch):
    with db.get_db() as conn:
        first = conn
    other = tmp_path / "other.db"
    monkeypatch.setattr(db, "db_path", lambda: other)
    with db.get_db() as conn:
        assert conn is not first
        # ...and it really is pointed at the new file
        conn.execute("CREATE TABLE probe(x)")
    assert other.exists()
    with db.get_db() as conn:
        conn.execute("SELECT * FROM probe").fetchall()


def test_reused_connection_keeps_its_pragmas_and_row_factory():
    with db.get_db() as conn:
        pass
    with db.get_db() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        row = conn.execute("SELECT 1 AS n").fetchone()
        assert isinstance(row, sqlite3.Row) and row["n"] == 1


def test_close_thread_connection_then_get_db_reopens():
    with db.get_db() as conn:
        first = conn
    db.close_thread_connection()
    with db.get_db() as conn:
        assert conn is not first
        conn.execute("SELECT 1").fetchone()
    db.close_thread_connection()
    db.close_thread_connection()   # idempotent


# ── the semantics the call sites depend on ─────────────────────────────


def _rows(name="reuse_probe"):
    with db.get_db() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]


@pytest.fixture
def probe():
    with db.get_db() as conn:
        conn.execute("CREATE TABLE reuse_probe(x INTEGER)")
    return "reuse_probe"


def test_commit_on_clean_exit(probe):
    with db.get_db() as conn:
        conn.execute("INSERT INTO reuse_probe VALUES (1)")
    db.close_thread_connection()      # nothing cached can be hiding the row
    assert _rows() == 1


def test_rollback_on_exception(probe):
    with pytest.raises(ValueError):
        with db.get_db() as conn:
            conn.execute("INSERT INTO reuse_probe VALUES (1)")
            raise ValueError("boom")
    assert _rows() == 0


def test_rollback_on_base_exception(probe):
    """CancelledError is a BaseException and this DB is driven from asyncio.
    A cached connection is never closed, so an aborted block that left its
    writes in an open transaction would have them committed by the NEXT block
    on this thread — the one bug reuse could plausibly introduce."""
    with pytest.raises(asyncio.CancelledError):
        with db.get_db() as conn:
            conn.execute("INSERT INTO reuse_probe VALUES (1)")
            raise asyncio.CancelledError()
    with db.get_db() as conn:
        conn.execute("INSERT INTO reuse_probe VALUES (2)")     # commits
    db.close_thread_connection()
    with db.get_db() as conn:
        assert [r[0] for r in conn.execute("SELECT x FROM reuse_probe")] == [2]


def test_an_aborted_block_leaves_no_open_transaction(probe):
    with pytest.raises(ValueError):
        with db.get_db() as conn:
            conn.execute("INSERT INTO reuse_probe VALUES (1)")
            raise ValueError
    with db.get_db() as conn:
        assert conn.in_transaction is False


def test_nested_blocks_get_independent_connections(probe):
    """A helper that opens its own block while its caller holds one must keep
    today's semantics: a separate connection, committing and rolling back on
    its own boundary. (The tree has no such nesting — the discipline scan pins
    that at zero — but `is_trading_idle(conn=None)` is one call away from it.)

    The inner block writing while the OUTER one holds a write transaction is
    an ordinary SQLite lock conflict and behaves exactly as it does today, so
    the nesting checked here is read-inside-write, which is what would occur.
    """
    with db.get_db() as outer:
        outer.execute("INSERT INTO reuse_probe VALUES (1)")
        with db.get_db() as inner:
            assert inner is not outer
            assert inner.execute("SELECT COUNT(*) FROM reuse_probe").fetchone()[0] == 0
            with pytest.raises(ValueError):
                with db.get_db() as innermost:
                    assert innermost is not inner and innermost is not outer
                    raise ValueError
        # both inner connections are closed; the outer one is untouched
        assert outer.in_transaction is True
    db.close_thread_connection()
    assert [r[0] for r in _all()] == [1]


def _all():
    with db.get_db() as conn:
        return conn.execute("SELECT x FROM reuse_probe ORDER BY x").fetchall()


# ── loop-thread safety ─────────────────────────────────────────────────


def test_the_event_loop_thread_never_shares_a_connection():
    """Two blocks that hold a transaction open across an `await` still exist
    (tests/test_db_connection_discipline.py pins them). Until they are gone, a
    thread with a running loop must get a private connection every time, or one
    coroutine's block exit would commit another's half-written rows."""
    async def main():
        with db.get_db() as a:
            first = a
        with db.get_db() as b:
            return first is b
    assert asyncio.run(main()) is False


def test_starting_a_loop_drops_a_connection_cached_before_it():
    """init_db() runs on the main thread at startup and caches a connection;
    that same thread then becomes the event-loop thread. Nothing would ever
    hand the connection back, and the open handle would block the corrupt-file
    quarantine's rename/unlink on Windows."""
    with db.get_db() as conn:
        assert conn is getattr(db._local, "conn")
    asyncio.run(_touch())
    assert getattr(db._local, "conn", None) is None


async def _touch():
    with db.get_db() as conn:
        conn.execute("SELECT 1").fetchone()


def test_loop_thread_still_commits_and_rolls_back(probe):
    async def main():
        with db.get_db() as conn:
            conn.execute("INSERT INTO reuse_probe VALUES (5)")
        try:
            with db.get_db() as conn:
                conn.execute("INSERT INTO reuse_probe VALUES (6)")
                raise ValueError
        except ValueError:
            pass
    asyncio.run(main())
    assert [r[0] for r in _all()] == [5]


# ── maintenance paths that need the file to themselves ─────────────────


def test_vacuum_succeeds_after_this_thread_has_used_its_connection(probe):
    with db.get_db() as conn:
        conn.executemany(
            "INSERT INTO reuse_probe VALUES (?)", [(i,) for i in range(500)])
    with db.get_db() as conn:
        conn.execute("DELETE FROM reuse_probe")
    # left deliberately unread: an outstanding cursor pins a WAL read snapshot
    with db.get_db() as conn:
        cur = conn.execute("SELECT * FROM reuse_probe")
        cur.fetchone()
    db.vacuum()
    with db.get_db() as conn:
        conn.execute("INSERT INTO reuse_probe VALUES (1)")
    db.close_thread_connection()
    assert _rows() == 1


def test_run_maintenance_survives_a_cached_connection(probe):
    with db.get_db() as conn:
        conn.execute("INSERT INTO reuse_probe VALUES (1)")
    out = db.run_maintenance(force_vacuum=True)
    assert out["vacuumed"] is True, out
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reuse_probe").fetchone()[0] == 1


def test_worker_thread_reuses_across_calls_and_still_isolates():
    """The whole point of the change: a `run_in_executor` worker pays the
    ~2 ms connect once, not once per call site."""
    result: dict = {}

    def work():
        ids = []
        for _ in range(3):
            with db.get_db() as conn:
                ids.append(id(conn))
                conn.execute("SELECT 1").fetchone()
        result["ids"] = ids
        result["reuse_ok"] = db._reuse_ok()
        db.close_thread_connection()

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert result["reuse_ok"] is True
    assert len(set(result["ids"])) == 1
