"""Tests for DB retention / compaction (db.cleanup_old_data + run_maintenance).

The whale feed and market poller insert millions of rows over time; these
guard the pruning that keeps the SQLite file from ballooning to multi-GB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "maint.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_OLD = "2000-01-01 00:00:00"


def _add_trade(c, tid: str, when: str) -> None:
    c.execute(
        "INSERT INTO trades (trade_id,ticker,event_ticker,count_fp,yes_price,"
        "no_price,taker_side,dollar_value,category,created_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, "KX", "E", 1.0, 50, 50, "yes", 100.0, "crypto", when),
    )


def test_cleanup_prunes_old_keeps_recent(fresh_db):
    now = _now()
    with db.get_db() as c:
        _add_trade(c, "old1", _OLD)
        _add_trade(c, "old2", _OLD)
        _add_trade(c, "fresh", now)
        c.execute(
            "INSERT INTO market_snapshots (ticker,volume,volume_24h,open_interest,"
            "yes_bid,last_price,snapshot_at) VALUES (?,?,?,?,?,?,?)",
            ("KX", 100, 50, 10, 50, 50, _OLD),
        )
        c.execute(
            "INSERT INTO market_snapshots (ticker,volume,volume_24h,open_interest,"
            "yes_bid,last_price,snapshot_at) VALUES (?,?,?,?,?,?,?)",
            ("KX", 100, 50, 10, 50, 50, now),
        )

    deleted = db.cleanup_old_data()

    with db.get_db() as c:
        trades = [r["trade_id"] for r in c.execute("SELECT trade_id FROM trades")]
        snaps = c.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    assert trades == ["fresh"]           # the two old trades pruned
    assert snaps == 1                    # the old snapshot pruned
    assert deleted >= 3


def test_batched_delete_clears_a_large_backlog(fresh_db):
    # More rows than one batch (50k would be slow; shrink the batch instead).
    import db as _db
    with db.get_db() as c:
        for i in range(250):
            _add_trade(c, f"t{i}", _OLD)
    n = _db._delete_batched("created_time < ?", (_now(),), "trades", batch=100)
    assert n == 250
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_run_maintenance_compacts_when_forced(fresh_db):
    with db.get_db() as c:
        for i in range(1000):
            _add_trade(c, f"t{i}", _OLD)
    summary = db.run_maintenance(force_vacuum=True)
    assert summary["vacuumed"] is True
    assert summary["deleted"] >= 1000
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def _seed_history(c) -> None:
    """Seed one row in every table clear_trade_history should wipe, plus a
    keeper row (a public market trade) it must preserve."""
    db.insert_bot_position(c, {
        "signal_source": "whale", "signal_id": "s1", "ticker": "KX-A",
        "direction": "yes", "target_contracts": 10, "limit_price_cents": 55,
        "client_order_id": "coid-a", "status": "filled",
    })
    db.insert_crypto15m_position(c, {
        "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-W1",
        "side": "up", "direction": "yes", "target_contracts": 5,
        "entry_limit_cents": 50, "client_order_id": "coid-c", "status": "filled",
    })
    db.start_bot_run(c, env="mainnet", cash_usd=100.0, portfolio_usd=0.0)
    db.insert_pnl_snapshot(
        c, cash_usd=100.0, portfolio_usd=0.0, realized_pnl_usd=5.0,
        wins=1, losses=0, open_positions=1, env="mainnet",
    )
    c.execute(
        "INSERT INTO daily_stats (day, network) VALUES (?,?)",
        ("2026-06-27", "mainnet"),
    )
    c.execute(
        "INSERT INTO order_events (position_id, kind, note) VALUES (?,?,?)",
        (1, "placed", "seeded"),
    )
    _add_trade(c, "keepme", _now())  # public market cache — must survive


def test_clear_trade_history_wipes_history_keeps_feed(fresh_db):
    with db.get_db() as c:
        _seed_history(c)

    summary = db.clear_trade_history()

    assert "_errors" not in summary
    with db.get_db() as c:
        for t in (
            "bot_positions", "crypto15m_positions", "bot_runs",
            "pnl_snapshots", "daily_stats", "order_events",
        ):
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
        # The public market feed (trades) is preserved — only per-user
        # history is wiped.
        kept = [r["trade_id"] for r in c.execute("SELECT trade_id FROM trades")]
        assert kept == ["keepme"]
    # Reported counts reflect the rows actually deleted.
    assert summary["bot_positions"] == 1
    assert summary["crypto15m_positions"] == 1


def test_clear_trade_history_on_empty_db_is_noop(fresh_db):
    summary = db.clear_trade_history()
    assert "_errors" not in summary
    assert all(v == 0 for v in summary.values())


def test_factory_reset_wipes_crypto15m_positions(fresh_db):
    # L8: the "nuke everything" reset previously LEFT crypto15m rows behind, so
    # stale crypto wins/losses survived and open crypto rows kept occupying slots.
    with db.get_db() as c:
        db.insert_crypto15m_position(c, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": "T",
            "side": "up", "direction": "yes", "target_contracts": 5,
            "entry_limit_cents": 80, "client_order_id": "c1", "status": "filled",
            "network": "mainnet",
        })
    summary = db.factory_reset()
    assert summary.get("crypto15m_positions") == 1
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 0


# ───────── unresolved-signal + events + order_events pruning, pnl series ────
# (ported from krypt-trader v3.0.0 maintenance upgrades)


def test_unresolved_alerts_and_whales_age_out(fresh_db):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO alerts (ticker, title, signal_type, direction, price,
                                   confidence, resolved, created_at)
               VALUES ('OLD-A', 't', 'trade_cluster', 'yes', 0.4, 60, 0,
                       datetime('now', '-60 days'))"""
        )
        conn.execute(
            """INSERT INTO whale_trades (trade_id, ticker, title, taker_side,
                                         count_fp, price, dollar_value, confidence,
                                         resolved, created_at)
               VALUES ('OLD-W', 'OLD-W-T', 't', 'yes', 100, 0.5, 5000, 70, 0,
                       datetime('now', '-60 days'))"""
        )
        # Fresh unresolved rows must survive.
        conn.execute(
            """INSERT INTO alerts (ticker, title, signal_type, direction, price,
                                   confidence, resolved)
               VALUES ('NEW-A', 't', 'trade_cluster', 'yes', 0.4, 60, 0)"""
        )

    db.cleanup_old_data()

    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alerts WHERE ticker='OLD-A'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM whale_trades WHERE trade_id='OLD-W'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alerts WHERE ticker='NEW-A'").fetchone()[0] == 1


def test_events_table_is_pruned(fresh_db):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO events (event_ticker, title, last_updated)
               VALUES ('OLD-EV', 'old', datetime('now', '-45 days'))"""
        )
        conn.execute(
            """INSERT INTO events (event_ticker, title, last_updated)
               VALUES ('NEW-EV', 'new', datetime('now'))"""
        )
    db.cleanup_old_data()
    with db.get_db() as conn:
        rows = {r[0] for r in conn.execute("SELECT event_ticker FROM events").fetchall()}
    assert rows == {"NEW-EV"}


def test_order_events_are_pruned(fresh_db):
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO order_events (position_id, kind, created_at)
               VALUES (1, 'poll', datetime('now', '-45 days'))"""
        )
        conn.execute(
            "INSERT INTO order_events (position_id, kind) VALUES (2, 'poll')"
        )
    db.cleanup_old_data()
    with db.get_db() as conn:
        rows = [r[0] for r in conn.execute("SELECT position_id FROM order_events").fetchall()]
    assert rows == [2]


def test_pnl_snapshots_query_downsamples(fresh_db):
    with db.get_db() as conn:
        for i in range(500):
            conn.execute(
                """INSERT INTO pnl_snapshots (at, network, cash_usd,
                                              portfolio_usd, total_usd)
                   VALUES (datetime('now', ?), 'mainnet', 100, 0, ?)""",
                (f"-{500 - i} minutes", 100.0 + i),
            )
    with db.get_db() as conn:
        rows = db.get_pnl_snapshots(conn, since_hours=24, env="mainnet", max_points=100)
    assert 0 < len(rows) <= 101
    # Newest point is always kept exact (the chart's live tip).
    assert rows[-1]["total_usd"] == pytest.approx(599.0)
    # Old rows outside the window are excluded via the indexable predicate.
    with db.get_db() as conn:
        rows_1h = db.get_pnl_snapshots(conn, since_hours=1, env="mainnet", max_points=0)
    assert 55 <= len(rows_1h) <= 62  # ~one per minute for the last hour


def test_backup_research_creates_and_reuses_daily_copy(fresh_db):
    dest = db.backup_research()
    assert dest is not None
    import os
    assert os.path.exists(dest)
    # Second call same day returns the existing file, no duplicate.
    assert db.backup_research() == dest


def test_factory_reset_takes_backup_first(fresh_db):
    import os
    with db.get_db() as c:
        db.insert_crypto15m_position(c, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": "T-BAK",
            "side": "up", "direction": "yes", "target_contracts": 5,
            "entry_limit_cents": 80, "client_order_id": "cb", "status": "filled",
            "network": "mainnet",
        })
    db.factory_reset()
    bdir = os.path.join(os.path.dirname(str(db.db_path())), "backups")
    backups = [f for f in os.listdir(bdir) if f.startswith("research-")]
    assert backups, "factory_reset must snapshot the DB before wiping"
    # The backup preserves the wiped research row.
    import sqlite3 as _sq
    bconn = _sq.connect(os.path.join(bdir, backups[0]))
    try:
        n = bconn.execute(
            "SELECT COUNT(*) FROM crypto15m_positions WHERE ticker='T-BAK'"
        ).fetchone()[0]
    finally:
        bconn.close()
    assert n == 1


# ───────── update-survival vault (restore when the DB file is gone) ─────


def _make_marker_db(path) -> None:
    import sqlite3 as _sq
    conn = _sq.connect(str(path))
    try:
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES ('survived')")
        conn.commit()
    finally:
        conn.close()


def _marker_survived(path) -> bool:
    import sqlite3 as _sq
    conn = _sq.connect(str(path))
    try:
        return conn.execute("SELECT v FROM marker").fetchone()[0] == "survived"
    finally:
        conn.close()


def test_db_restores_from_local_backups_when_missing(tmp_path, monkeypatch):
    ud = tmp_path / "userdata"
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(ud))
    monkeypatch.setenv("KRYPT_POLYBOT_US_VAULT", str(tmp_path / "vault"))
    bdir = ud / "data" / "backups"
    bdir.mkdir(parents=True)
    _make_marker_db(bdir / "research-20990101.db")
    monkeypatch.setattr(db, "_restore_checked", False)
    p = db.db_path()
    assert p.exists() and _marker_survived(p)


def test_db_restores_from_vault_after_full_userdata_wipe(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    monkeypatch.setenv("KRYPT_POLYBOT_US_VAULT", str(vault))
    # 1) A nightly snapshot gets mirrored into the vault…
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path / "old-userdata"))
    snap = tmp_path / "research-20990101.db"
    _make_marker_db(snap)
    db._mirror_to_vault(str(snap))
    assert (vault / "default" / "research-latest.db").exists()
    # 2) …then userData is wiped (fresh dir): db_path() salvages the vault copy.
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path / "new-userdata"))
    monkeypatch.setattr(db, "_restore_checked", False)
    p = db.db_path()
    assert p.exists() and _marker_survived(p)


def test_db_restore_prefers_newest_local_backup_over_vault(tmp_path, monkeypatch):
    ud = tmp_path / "userdata"
    vault = tmp_path / "vault"
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(ud))
    monkeypatch.setenv("KRYPT_POLYBOT_US_VAULT", str(vault))
    bdir = ud / "data" / "backups"
    bdir.mkdir(parents=True)
    _make_marker_db(bdir / "research-20990101.db")
    (vault / "default").mkdir(parents=True)
    import sqlite3 as _sq
    conn = _sq.connect(str(vault / "default" / "research-latest.db"))
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('vault-older')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "_restore_checked", False)
    p = db.db_path()
    assert _marker_survived(p)  # local backup won, not the vault copy


def test_no_restore_when_db_already_exists(tmp_path, monkeypatch):
    ud = tmp_path / "userdata"
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(ud))
    monkeypatch.setenv("KRYPT_POLYBOT_US_VAULT", str(tmp_path / "vault"))
    (ud / "data").mkdir(parents=True)
    live = ud / "data" / "krypt-polybot-us.db"
    _make_marker_db(live)
    bdir = ud / "data" / "backups"
    bdir.mkdir(parents=True)
    import sqlite3 as _sq
    conn = _sq.connect(str(bdir / "research-20990101.db"))
    conn.execute("CREATE TABLE marker (v TEXT)")
    conn.execute("INSERT INTO marker VALUES ('backup-must-not-clobber')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(db, "_restore_checked", False)
    assert _marker_survived(db.db_path())


# ── VACUUM must not stall the trading loop ────────────────────────────
# VACUUM takes an EXCLUSIVE lock for its whole run. Every write the loop
# attempts meanwhile blocks on busy_timeout=10000 and then RAISES — so on a
# large file the loop is not "slowed", it is dead for the duration: no fill,
# no stop, no settlement gets recorded. It must therefore wait for a moment
# when nothing is open anywhere.


def _open_main_position(c) -> None:
    db.insert_bot_position(c, {
        "signal_source": "whale", "signal_id": "s9", "ticker": "KX-OPEN",
        "direction": "yes", "target_contracts": 10, "limit_price_cents": 55,
        "client_order_id": "coid-open", "status": "filled",
    })


def _open_crypto15m_position(c) -> None:
    db.insert_crypto15m_position(c, {
        "asset": "BTC", "series": "KXBTC15M", "ticker": "KXBTC15M-OPEN",
        "side": "up", "direction": "yes", "target_contracts": 5,
        "entry_limit_cents": 50, "client_order_id": "coid-c-open",
        "status": "filled",
    })


def test_is_trading_idle_on_an_empty_db(fresh_db):
    idle, why = db.is_trading_idle()
    assert idle is True and why == ""


@pytest.mark.parametrize("seed,expect", [
    (_open_main_position, "main-engine"),
    (_open_crypto15m_position, "crypto15m"),
])
def test_is_trading_idle_sees_each_engine(fresh_db, seed, expect):
    with db.get_db() as c:
        seed(c)
    idle, why = db.is_trading_idle()
    assert idle is False
    assert expect in why


def test_is_trading_idle_sees_a_resting_terminal_order(fresh_db):
    with db.get_db() as c:
        c.execute(
            "INSERT INTO terminal_orders (condition_id,side,action,order_type,"
            "requested_size,requested_price_cents,order_id,status,reconciled) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("0xabc", "yes", "buy", "GTC", 10.0, 55.0, "oid-1", "placed", 0),
        )
    idle, why = db.is_trading_idle()
    assert idle is False
    assert "terminal" in why

    # Once it is reconciled it is no longer resting → idle again.
    with db.get_db() as c:
        c.execute("UPDATE terminal_orders SET reconciled=1")
    assert db.is_trading_idle()[0] is True


def test_settled_rows_do_not_block_the_vacuum(fresh_db):
    with db.get_db() as c:
        _open_main_position(c)
        c.execute("UPDATE bot_positions SET resolved=1")
        _open_crypto15m_position(c)
        c.execute("UPDATE crypto15m_positions SET resolved=1")
    assert db.is_trading_idle()[0] is True


def test_run_maintenance_skips_vacuum_while_a_position_is_open(fresh_db):
    with db.get_db() as c:
        for i in range(1000):
            _add_trade(c, f"v{i}", _OLD)
        _open_main_position(c)

    ran = {"vacuum": 0}
    real_vacuum = db.vacuum
    db.vacuum = lambda: ran.__setitem__("vacuum", ran["vacuum"] + 1)
    try:
        summary = db.run_maintenance(force_vacuum=True)
    finally:
        db.vacuum = real_vacuum

    assert ran["vacuum"] == 0, "VACUUM ran while a position was open"
    assert summary["vacuumed"] is False
    assert "main-engine" in summary["vacuumSkipped"]
    # Pruning is NOT gated — it stays on the hourly timer either way.
    assert summary["deleted"] >= 1000
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_run_maintenance_vacuums_once_the_position_closes(fresh_db):
    with db.get_db() as c:
        _open_main_position(c)
    assert db.run_maintenance(force_vacuum=True)["vacuumed"] is False

    with db.get_db() as c:
        c.execute("UPDATE bot_positions SET status='settled', resolved=1")
    summary = db.run_maintenance(force_vacuum=True)
    assert summary["vacuumed"] is True
    assert summary["vacuumSkipped"] == ""


# ───────── research archive (retention moves research rows, never drops them) ─

_RESEARCH = {   # table -> the column its age cutoff reads
    "crypto15m_signals": "observed_at",
    "crypto15m_ticks": "observed_at",
    "whale_trades": "created_at",
    "alerts": "created_at",
}


def _archived(table: str) -> list[dict]:
    import sqlite3 as _sq
    conn = _sq.connect(str(db.research_archive_path()))
    conn.row_factory = _sq.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


def _days_ago(n: float) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


def _seed_research(c, when: str, tag: str) -> None:
    """One row in each archived research table, all stamped `when`."""
    c.execute(
        "INSERT INTO crypto15m_signals (ticker, asset, observed_at, close_time, favorite,"
        " favorite_price, entry_cost, resolved, up_won, interval, network)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f"C15-{tag}", "BTC", when, when, "up", 0.7, 0.71, 1, 1, "15m", "mainnet"),
    )
    c.execute(
        "INSERT INTO crypto15m_ticks (ticker, asset, observed_at, mins_left, yes_bid,"
        " yes_ask, model_prob, interval, network) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"C15-{tag}", "BTC", when, 4.0, 0.69, 0.71, 0.8, "15m", "mainnet"),
    )
    c.execute(
        "INSERT INTO whale_trades (trade_id, ticker, taker_side, price, confidence,"
        " resolved, outcome_correct, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"W-{tag}", f"M-{tag}", "yes", 0.4, 70, 1, 1, when),
    )
    c.execute(
        "INSERT INTO alerts (ticker, signal_type, direction, price, confidence,"
        " resolved, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"A-{tag}", "trade_cluster", "yes", 0.4, 60, 0, when),
    )


def test_retention_moves_research_rows_into_the_archive(fresh_db):
    """Retention used to DELETE research rows past 60/45 days, and the 7
    rotating nightlies kept them only a week longer: the corpus every strategy
    verdict came from was lost for good. Now it is moved, column for column."""
    with db.get_db() as c:
        _seed_research(c, _OLD, "old")
        _seed_research(c, _now(), "new")

    db.cleanup_old_data()

    with db.get_db() as c:
        for t in _RESEARCH:
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 1, t
    for t, tcol in _RESEARCH.items():
        assert [r[tcol] for r in _archived(t)] == [_OLD], t
    sig = _archived("crypto15m_signals")[0]
    assert (sig["ticker"], sig["up_won"], sig["interval"]) == ("C15-old", 1, "15m")
    assert _archived("crypto15m_ticks")[0]["model_prob"] == pytest.approx(0.8)
    assert _archived("whale_trades")[0]["trade_id"] == "W-old"
    assert _archived("alerts")[0]["ticker"] == "A-old"   # unresolved ones too


def test_archiving_again_never_duplicates(fresh_db):
    with db.get_db() as c:
        _seed_research(c, _OLD, "old")
        tick = dict(c.execute("SELECT * FROM crypto15m_ticks").fetchone())
    db.cleanup_old_data()
    # A restored backup brings an already-archived row back into the live DB.
    with db.get_db() as c:
        cols = ", ".join(tick)
        c.execute(f"INSERT INTO crypto15m_ticks ({cols}) VALUES ({', '.join('?' * len(tick))})",
                  tuple(tick.values()))
    # ...and until the next pass, a reader counts it once.
    with db.get_db() as c:
        src = db.research_source(c, "crypto15m_ticks")
        assert c.execute(f"SELECT COUNT(*) FROM {src} t").fetchone()[0] == 1
    db.cleanup_old_data()
    assert len(_archived("crypto15m_ticks")) == 1
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM crypto15m_ticks").fetchone()[0] == 0


def test_factory_reset_keeps_the_archive_and_reused_ids_archive_as_new_rows(fresh_db):
    with db.get_db() as c:
        c.execute("INSERT INTO alerts (ticker, created_at) VALUES ('A-before', ?)", (_OLD,))
    db.cleanup_old_data()
    db.factory_reset()          # clears sqlite_sequence, so alert ids restart at 1
    assert [r["ticker"] for r in _archived("alerts")] == ["A-before"]
    with db.get_db() as c:
        c.execute("INSERT INTO alerts (ticker, created_at) VALUES ('A-after', ?)",
                  ("2001-01-01 00:00:00",))
        assert c.execute("SELECT id FROM alerts").fetchone()[0] == 1
    db.cleanup_old_data()
    assert [r["ticker"] for r in _archived("alerts")] == ["A-before", "A-after"]


def test_the_archive_gains_columns_the_live_table_gains(fresh_db):
    with db.get_db() as c:
        _seed_research(c, _OLD, "a")
    db.cleanup_old_data()
    with db.get_db() as c:
        c.execute("ALTER TABLE crypto15m_ticks ADD COLUMN future_feature REAL")
        c.execute("INSERT INTO crypto15m_ticks (ticker, asset, observed_at, future_feature)"
                  " VALUES ('C15-b', 'ETH', ?, 1.5)", (_OLD,))
    db.cleanup_old_data()
    rows = {r["ticker"]: r for r in _archived("crypto15m_ticks")}
    assert rows["C15-b"]["future_feature"] == pytest.approx(1.5)
    assert rows["C15-a"]["future_feature"] is None


def test_research_rows_stay_when_the_archive_cannot_take_them(fresh_db):
    db.research_archive_path().mkdir()      # a directory where the file goes
    with db.get_db() as c:
        _seed_research(c, _OLD, "old")
        _add_trade(c, "old-trade", _OLD)
    db.cleanup_old_data()
    with db.get_db() as c:
        for t in _RESEARCH:
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 1, t
        # The rest of retention still ran.
        assert c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


def test_a_research_backlog_moves_in_batches(fresh_db):
    with db.get_db() as c:
        for i in range(250):
            c.execute("INSERT INTO crypto15m_ticks (ticker, asset, observed_at)"
                      " VALUES (?, 'BTC', ?)", (f"T{i}", _OLD))
    with db._research_archive() as ac:
        n = db._archive_batched(ac, "observed_at < ?", (_now(),), "crypto15m_ticks", batch=100)
    assert n == 250
    assert len(_archived("crypto15m_ticks")) == 250
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM crypto15m_ticks").fetchone()[0] == 0


def test_backtest_and_replay_readers_see_archived_rows(fresh_db):
    import sqlite3 as _sq

    import backtest
    import replay
    from config import merge_with_defaults

    with db.get_db() as c:
        _seed_research(c, _days_ago(70), "c15")     # past the 60-day tick/signal retention
        _seed_research(c, _days_ago(50), "whale")   # past the 45-day whale/alert retention
        c.execute("DELETE FROM crypto15m_signals WHERE ticker='C15-whale'")
        c.execute("DELETE FROM crypto15m_ticks WHERE ticker='C15-whale'")
        c.execute("DELETE FROM whale_trades WHERE trade_id='W-c15'")
    db.cleanup_old_data()
    with db.get_db() as c:
        for t in _RESEARCH:
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t

    # backtest.py's CLI: a read-only connection to the DB file.
    conn = _sq.connect(f"file:{fresh_db}?mode=ro", uri=True)
    try:
        assert len(backtest.load_crypto15m_signals(conn, interval="15m")) == 1
        assert [s["source"] for s in backtest.load_resolved_signals(conn)] == ["whale"]
        # ...and a read-only reader gets the archive read-only too.
        with pytest.raises(_sq.OperationalError):
            conn.execute("DELETE FROM research_archive.whale_trades")
    finally:
        conn.close()
    # In-app replays: a window past retention reads the archive; one inside it
    # can't hold archived rows and stays on the live table alone.
    assert list(replay.load_windows("mainnet", "15m", since_days=90)) == ["C15-c15"]
    assert replay.load_windows("mainnet", "15m", since_days=60) == {}
    assert replay.replay_main(merge_with_defaults({}), since_days=60)["windowsScanned"] == 1
    with db.get_db() as c:
        assert db.research_source(c, "crypto15m_ticks", 60) == "crypto15m_ticks"


def test_maintenance_keeps_markets_the_venue_lists_without_volume(fresh_db):
    """Polymarket US's market list has no volume, so every row is 0. A dust sweep on
    volume deleted the whole table right after each sync."""
    with db.get_db() as c:
        db.upsert_market(c, {"ticker": "open-one", "title": "t", "status": "open",
                             "close_time": "2099-01-01T00:00:00Z", "volume": 0, "volume_24h": 0})
    db.cleanup_old_data()
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM markets").fetchone()[0] == 1
