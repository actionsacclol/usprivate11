"""Tests for the durable drawdown circuit-breakers and fixed-sizing backstop.

Covers the money-safety fixes:

  - DB corruption recovery restores the newest backup instead of coming up
    with an empty schema (db.init_db).
  - A durable, wipe-surviving LIFETIME cumulative realized-loss guard that
    pauses new entries once total drawdown reaches a fraction of the bankroll
    (crypto15m + copy engines).
  - The daily-loss figure survives a mid-day history wipe (so clearing stats
    can't re-arm trading past the daily limit).
  - The crypto15m daily breaker includes the committed cost of still-open
    TODAY positions, so a settlement wave can't overshoot the cap.
  - 'fixed'-mode sizing can't put most of the account on a single bet.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import crypto15m_trader as ct
import db
from config import merge_with_defaults


# ───────── fixtures / helpers ──────────────────────────────────────────


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "guards.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


ENV = "mainnet"


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seed_c15(pnl=None, *, cost=0.0, status="settled", open_today=False,
             entry_cents=90, contracts=5):
    """Insert a crypto15m position. When pnl is not None it's resolved (with
    resolved_at = now). When open_today, it stays open (resolved=0)."""
    uid = uuid.uuid4().hex[:12]
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": "BTC", "series": "KXBTC15M", "ticker": f"T-{uid}",
            "side": "up", "direction": "yes", "target_contracts": contracts,
            "filled_contracts": contracts, "entry_limit_cents": entry_cents,
            "cost_usd": cost, "client_order_id": f"c-{uid}",
            "status": status, "network": ENV,
        })
        if pnl is not None:
            db.update_crypto15m_position(
                conn, pid, resolved=1, pnl_usd=pnl,
                outcome_correct=1 if pnl > 0 else 0,
            )
            conn.execute(
                "UPDATE crypto15m_positions SET resolved_at=datetime('now') WHERE id=?",
                (pid,),
            )
    return pid


def seed_copy(pnl=None, *, cost=0.0, status="filled"):
    uid = uuid.uuid4().hex[:12]
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, {
            "signal_source": "copy", "signal_id": abs(hash(uid)) % 1_000_000,
            "ticker": f"CT-{uid}", "direction": "yes", "target_contracts": 5,
            "limit_price_cents": 50, "filled_contracts": 5, "cost_usd": cost,
            "client_order_id": f"cp-{uid}", "status": status, "network": ENV,
        })
        if pnl is not None:
            db.update_bot_position(
                conn, pid, resolved=1, pnl_usd=pnl, resolved_at=_now_str(),
            )
    return pid


def base_cfg(**over):
    c = merge_with_defaults({})
    c["network"] = ENV
    c["start_bankroll_usd"] = 100.0
    c.update(over)
    return c


# ───────── Task 1: corruption recovery restores the backup ─────────────


def test_init_db_restores_backup_on_corruption(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(db, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(db, "_vault_dir", lambda: None)

    # A valid backup carrying a canary row.
    bdir = data_dir / "backups"
    bdir.mkdir()
    backup = bdir / "research-20260101.db"
    con = sqlite3.connect(str(backup))
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO app_kv (k, v) VALUES ('canary', 'restored')")
    con.commit()
    con.close()

    # A malformed main DB file.
    (data_dir / "krypt-polybot-us.db").write_bytes(b"\x00 definitely not sqlite " * 16)

    # Reproduce the bug's precondition: the one-shot restore probe already
    # latched True (any earlier DB access does this), so without the fix the
    # recreate comes up empty.
    monkeypatch.setattr(db, "_restore_checked", True, raising=False)

    db.init_db()

    with db.get_db() as conn:
        row = conn.execute("SELECT v FROM app_kv WHERE k='canary'").fetchone()
    assert row is not None and row[0] == "restored", "backup was not restored"
    # The corrupt file was quarantined aside, not left in place.
    assert list(data_dir.glob("krypt-polybot-us.db.corrupt-*"))


# ───────── Task 2: lifetime cumulative-loss guard ──────────────────────


def test_crypto15m_lifetime_guard_trips_at_pct(fresh_db):
    # Bankroll 100, limit 50% -> cap $50. Cumulative loss $60 -> tripped.
    seed_c15(-40.0)
    seed_c15(-20.0)
    cfg = base_cfg(crypto15m_lifetime_loss_limit_pct=0.5)
    tripped, why = ct._lifetime_loss_tripped(cfg, ENV)
    assert tripped and "lifetime loss limit" in why


def test_crypto15m_lifetime_guard_ok_below_cap(fresh_db):
    seed_c15(-30.0)  # loss $30 < $50 cap
    cfg = base_cfg(crypto15m_lifetime_loss_limit_pct=0.5)
    assert ct._lifetime_loss_tripped(cfg, ENV) == (False, "")


def test_crypto15m_lifetime_guard_usd_override(fresh_db):
    seed_c15(-15.0)
    # $10 absolute override takes precedence over the (much larger) pct cap.
    cfg = base_cfg(crypto15m_lifetime_loss_limit_pct=0.5,
                   crypto15m_lifetime_loss_limit_usd=10.0)
    tripped, _ = ct._lifetime_loss_tripped(cfg, ENV)
    assert tripped


def test_crypto15m_lifetime_guard_disabled(fresh_db):
    seed_c15(-500.0)
    cfg = base_cfg(crypto15m_lifetime_loss_limit_pct=0.0,
                   crypto15m_lifetime_loss_limit_usd=0.0)
    assert ct._lifetime_loss_tripped(cfg, ENV) == (False, "")


def test_crypto15m_lifetime_guard_needs_bankroll_for_pct(fresh_db):
    # No pinned bankroll and no recorded snapshot -> pct cap can't be sized,
    # so it stays disarmed (fail-safe: don't halt on unknown bankroll).
    seed_c15(-500.0)
    cfg = base_cfg(crypto15m_lifetime_loss_limit_pct=0.5)
    cfg["start_bankroll_usd"] = 0.0
    assert ct._lifetime_loss_tripped(cfg, ENV) == (False, "")


# ───────── wipe survival: counter can't be reset by clearing stats ─────


def test_lifetime_counter_survives_clear_trade_history(fresh_db):
    seed_c15(-60.0)
    cfg = base_cfg(crypto15m_lifetime_loss_limit_pct=0.5)
    assert ct._lifetime_loss_tripped(cfg, ENV)[0]

    db.clear_trade_history()

    # Positions table is now empty ...
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM crypto15m_positions").fetchone()[0] == 0
        # ... but the durable cumulative counter preserved the loss.
        assert db.lifetime_realized_pnl(conn, "crypto15m", ENV) == pytest.approx(-60.0)
    # So the breaker is STILL armed (can't be disarmed by wiping stats).
    assert ct._lifetime_loss_tripped(cfg, ENV)[0]


def test_lifetime_counter_survives_factory_reset(fresh_db):
    seed_copy(-80.0)
    with db.get_db() as conn:
        assert db.lifetime_realized_pnl(conn, "copy", ENV) == pytest.approx(-80.0)

    db.factory_reset()

    with db.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM bot_positions").fetchone()[0] == 0
        assert db.lifetime_realized_pnl(conn, "copy", ENV) == pytest.approx(-80.0)


def test_banking_is_cumulative_across_repeated_wipes(fresh_db):
    seed_c15(-30.0)
    db.clear_trade_history()
    seed_c15(-25.0)
    db.clear_trade_history()
    with db.get_db() as conn:
        assert db.lifetime_realized_pnl(conn, "crypto15m", ENV) == pytest.approx(-55.0)


# ───────── Task 3: daily figure survives a mid-day wipe ────────────────


def test_daily_pnl_survives_wipe(fresh_db):
    seed_copy(-40.0)  # resolved today
    with db.get_db() as conn:
        assert db.engine_today_pnl(conn, "copy", ENV) == pytest.approx(-40.0)

    db.clear_trade_history()

    with db.get_db() as conn:
        # The wipe folded today's realized loss into the durable day counter.
        assert db.engine_today_pnl(conn, "copy", ENV) == pytest.approx(-40.0)


def test_crypto15m_today_pnl_survives_wipe(fresh_db):
    seed_c15(-35.0)
    with db.get_db() as conn:
        assert db.crypto15m_today_pnl(conn, ENV) == pytest.approx(-35.0)
    db.clear_trade_history()
    with db.get_db() as conn:
        assert db.crypto15m_today_pnl(conn, ENV) == pytest.approx(-35.0)


# ───────── Task 4: daily breaker counts open TODAY exposure ────────────


def test_today_open_cost_counts_open_positions(fresh_db):
    seed_c15(open_today=True, status="filled", cost=30.0)
    seed_c15(open_today=True, status="filled", cost=12.0)
    seed_c15(-5.0)  # resolved — must NOT be counted as open exposure
    with db.get_db() as conn:
        assert db.crypto15m_today_open_cost(conn, ENV) == pytest.approx(42.0)


def test_today_open_cost_values_resting_notional(fresh_db):
    # A resting order has cost_usd=0 -> valued at contracts × limit.
    seed_c15(open_today=True, status="submitted", cost=0.0,
             entry_cents=80, contracts=5)  # 5 × 80c = $4.00
    with db.get_db() as conn:
        assert db.crypto15m_today_open_cost(conn, ENV) == pytest.approx(4.0)


# ───────── Task 5: fixed-sizing fraction-of-balance backstop ───────────


def test_fixed_sizing_caps_single_entry_fraction(fresh_db):
    # order_size huge vs a small balance, no explicit max-loss -> one entry
    # must not exceed 25% of balance (0.25 × $10 = $2.50 // $0.90 = 2).
    cfg = base_cfg(crypto15m_sizing_mode="fixed", crypto15m_max_loss_pct=0.0)
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=90, balance_usd=10.0, order_size=1000)
    assert n == 2


def test_fixed_sizing_small_order_unchanged(fresh_db):
    # Normal out-of-box size vs a healthy balance -> backstop doesn't bite.
    cfg = base_cfg(crypto15m_sizing_mode="fixed", crypto15m_max_loss_pct=0.0)
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=90, balance_usd=1000.0, order_size=5)
    assert n == 5


def test_balance_pct_mode_exempt_from_backstop(fresh_db):
    # balance_pct is already fraction-bounded; the fixed-mode backstop must not
    # clamp it. 50% of $100 at 50c = 100 contracts (not the 25% backstop's 50).
    cfg = base_cfg(crypto15m_sizing_mode="balance_pct",
                   crypto15m_balance_pct=0.5, crypto15m_max_loss_pct=0.0)
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=50, balance_usd=100.0, order_size=1)
    assert n == 100


def test_explicit_max_loss_still_applies(fresh_db):
    # An explicit max_loss_pct wins over the default backstop.
    cfg = base_cfg(crypto15m_sizing_mode="fixed", crypto15m_max_loss_pct=0.10)
    n = ct.compute_entry_contracts(
        cfg, entry_limit_cents=50, balance_usd=100.0, order_size=1000)
    assert n == 20  # 0.10 × 100 = $10 // 0.50


# ───────── main-engine lifetime cumulative-loss guard ───────────────────


def seed_main(pnl, *, source="whale"):
    uid = uuid.uuid4().hex[:12]
    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, {
            "signal_source": source, "signal_id": abs(hash(uid)) % 1_000_000,
            "ticker": f"MT-{uid}", "direction": "yes", "target_contracts": 5,
            "limit_price_cents": 50, "filled_contracts": 5, "cost_usd": 2.5,
            "client_order_id": f"mn-{uid}", "status": "filled", "network": ENV,
        })
        db.update_bot_position(
            conn, pid, resolved=1, pnl_usd=pnl, resolved_at=_now_str(),
        )
    return pid


def test_main_lifetime_guard_trips_at_pct(fresh_db):
    import trader
    seed_main(-40.0)
    seed_main(-20.0)   # loss $60 >= 50% of $100 bankroll
    cfg = base_cfg(lifetime_loss_limit_pct=0.5)
    tripped, why = trader._lifetime_loss_tripped_main(cfg, ENV)
    assert tripped and "lifetime loss limit" in why


def test_main_lifetime_guard_ignores_external_and_copy_rows(fresh_db):
    """The breaker gates BOT entries, so reconcile-imported 'external' rows
    (the 2026-07 double-import artifacts booked -$800 there) and copy-engine
    rows must not trip it."""
    import trader
    seed_main(-500.0, source="external")
    seed_copy(-500.0)
    cfg = base_cfg(lifetime_loss_limit_pct=0.5)
    assert trader._lifetime_loss_tripped_main(cfg, ENV) == (False, "")


def test_main_lifetime_guard_survives_history_wipe(fresh_db):
    import trader
    seed_main(-60.0)
    with db.get_db() as conn:
        db.bank_realized_pnl_before_wipe(conn)
        conn.execute("DELETE FROM bot_positions")
    cfg = base_cfg(lifetime_loss_limit_pct=0.5)
    tripped, _ = trader._lifetime_loss_tripped_main(cfg, ENV)
    assert tripped


def test_main_lifetime_guard_disabled(fresh_db):
    import trader
    seed_main(-500.0)
    cfg = base_cfg(lifetime_loss_limit_pct=0.0, lifetime_loss_limit_usd=0.0)
    assert trader._lifetime_loss_tripped_main(cfg, ENV) == (False, "")


# ───────── lifetime transfer adjustment (all-time P&L) ─────────────────


def test_transfer_adjustment_total_sums_all_days(fresh_db):
    # Withdrawals/deposits recorded across MULTIPLE days must all be excluded
    # from the all-time balance P&L — the daily key alone only protected
    # today's number, so any past withdrawal read as permanent lifetime loss.
    with db.get_db() as conn:
        db.kv_set(conn, f"transfers:{ENV}:2026-07-01", repr(-200.0))  # withdrawal
        db.kv_set(conn, f"transfers:{ENV}:2026-07-05", repr(50.0))    # deposit
        db.kv_set(conn, f"transfers:{ENV}:2026-07-10", repr(-25.0))   # withdrawal
        db.kv_set(conn, "transfers:othernet:2026-07-10", repr(-999.0))  # other env
        assert db.transfer_adjustment_total(conn, ENV) == pytest.approx(-175.0)


def test_transfer_adjustment_total_empty_and_garbage(fresh_db):
    with db.get_db() as conn:
        assert db.transfer_adjustment_total(conn, ENV) == 0.0
        db.kv_set(conn, f"transfers:{ENV}:2026-07-01", "not-a-number")
        db.kv_set(conn, f"transfers:{ENV}:2026-07-02", repr(-10.0))
        assert db.transfer_adjustment_total(conn, ENV) == pytest.approx(-10.0)


def test_transfer_recorded_per_run_and_adjusts_session(fresh_db):
    # A withdrawal detected while a bot run is active must also be booked
    # against THAT run, so session P&L nets it out.
    with db.get_db() as conn:
        db.insert_pnl_snapshot(conn, cash_usd=500.0, portfolio_usd=0.0,
                               realized_pnl_usd=0.0, wins=0, losses=0,
                               open_positions=0, env=ENV)
        run_id = db.start_bot_run(conn, env=ENV, cash_usd=500.0, portfolio_usd=0.0)
        # $200 vanishes with no trading activity to explain it -> withdrawal.
        moved = db.note_transfer_if_unexplainable(conn, ENV, 300.0, run_id=run_id)
        assert moved == pytest.approx(-200.0)
        assert db.transfer_adjustment_run(conn, ENV, run_id) == pytest.approx(-200.0)
        assert db.transfer_adjustment_today(conn, ENV) == pytest.approx(-200.0)
        # A second withdrawal accumulates on the same run key.
        db.insert_pnl_snapshot(conn, cash_usd=300.0, portfolio_usd=0.0,
                               realized_pnl_usd=0.0, wins=0, losses=0,
                               open_positions=0, env=ENV)
        moved = db.note_transfer_if_unexplainable(conn, ENV, 250.0, run_id=run_id)
        assert moved == pytest.approx(-50.0)
        assert db.transfer_adjustment_run(conn, ENV, run_id) == pytest.approx(-250.0)
        # Other runs / no-run reads are unaffected.
        assert db.transfer_adjustment_run(conn, ENV, run_id + 1) == 0.0
        assert db.transfer_adjustment_run(conn, ENV, 0) == 0.0


def test_a_withdrawal_is_not_a_trading_loss_in_the_runs_own_pnl(fresh_db):
    """Live 2026-11-08, run 2: $58.01 to $20.23 with $25.39 withdrawn was stored
    as −$37.78. The run lost $12.39."""
    with db.get_db() as conn:
        db.insert_pnl_snapshot(conn, cash_usd=41.40, portfolio_usd=0.0,
                               realized_pnl_usd=0.0, wins=0, losses=0,
                               open_positions=0, env=ENV)
        run_id = db.start_bot_run(conn, env=ENV, cash_usd=58.01, portfolio_usd=0.0)
        db.kv_set(conn, f"transfers_run:{ENV}:{run_id}", repr(-25.39))
        db.heartbeat_bot_run(conn, run_id, cash_usd=20.23, portfolio_usd=0.0)
        [row] = db.get_recent_runs(conn, env=ENV)
        assert row["pnl_usd"] == pytest.approx(-12.39)
        # A row stored before transfers were netted reads right as well.
        conn.execute("UPDATE bot_runs SET pnl_usd = end_total_usd - start_total_usd")
        [row] = db.get_recent_runs(conn, env=ENV)
        assert row["pnl_usd"] == pytest.approx(-37.78)
        assert db.run_pnl(conn, row) == pytest.approx(-12.39)
        # A run with no transfers is just its balance change.
        other = db.start_bot_run(conn, env=ENV, cash_usd=10.0, portfolio_usd=0.0)
        db.heartbeat_bot_run(conn, other, cash_usd=12.5, portfolio_usd=0.0)
        fresh = next(r for r in db.get_recent_runs(conn, env=ENV) if r["id"] == other)
        assert fresh["pnl_usd"] == pytest.approx(2.5) and db.run_pnl(conn, fresh) == pytest.approx(2.5)


# ── the all-time baseline survives snapshot pruning ──

def _snap_at(conn, total, days_ago, env=ENV):
    conn.execute(
        "INSERT INTO pnl_snapshots (at, network, cash_usd, portfolio_usd, total_usd) "
        "VALUES (datetime('now', ?), ?, ?, 0, ?)", (f"-{days_ago} days", env, total, total))


def test_alltime_baseline_survives_the_45_day_prune(fresh_db):
    with db.get_db() as conn:
        _snap_at(conn, 1000.0, 60)
        _snap_at(conn, 1500.0, 10)
    db.cleanup_old_data()
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pnl_snapshots WHERE network=?", (ENV,)).fetchone()[0] == 1
        # was 1500 (the oldest survivor): All-time and the auto bankroll moved
        assert db.earliest_pnl_total(conn, ENV) == pytest.approx(1000.0)
        assert db.effective_start_bankroll(conn, ENV) == pytest.approx(1000.0)


def test_the_baseline_is_pinned_by_the_first_snapshot(fresh_db):
    with db.get_db() as conn:
        db.insert_pnl_snapshot(conn, cash_usd=700, portfolio_usd=300, realized_pnl_usd=0,
                               wins=0, losses=0, open_positions=0, env=ENV)
        db.insert_pnl_snapshot(conn, cash_usd=900, portfolio_usd=300, realized_pnl_usd=0,
                               wins=0, losses=0, open_positions=0, env=ENV)
        conn.execute("DELETE FROM pnl_snapshots")   # every snapshot gone
        assert db.earliest_pnl_total(conn, ENV) == pytest.approx(1000.0)


def test_transfers_before_the_baseline_do_not_count(fresh_db):
    # The audit's case: a $1,000 deposit long ago, then the snapshots from
    # before it pruned away. The deposit is inside the baseline; subtracting
    # it again showed a flat account at −$1,000.
    with db.get_db() as conn:
        _snap_at(conn, 1500.0, 10)          # oldest surviving snapshot
        old = (datetime.now(timezone.utc) - timedelta(days=50)).strftime("%Y-%m-%d")
        new = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        db.kv_set(conn, f"transfers:{ENV}:{old}", "1000.0")
        db.kv_set(conn, f"transfers:{ENV}:{new}", "-25.0")
        assert db.transfer_adjustment_total(conn, ENV) == pytest.approx(-25.0)
