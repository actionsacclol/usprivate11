"""Gaps in the wipe, the sweep and the roll-ups.

Every one of these is a figure or a limit that is WRONG rather than missing,
which is the failure mode this codebase treats as the serious one: a "start my
stats fresh" that leaves a track record behind, a per-script cap that stops
counting rows it still holds, and a roll-up that counts simulated money as
real. None of them raise, none of them look broken on screen.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "wipe.db")
    db.init_db()
    return tmp_path


ENV = "mainnet"


def _shadow(conn, *, sid="s1", ticker="0xa", close_time="", resolved=0):
    return db.insert_script_shadow(conn, {
        "script_id": sid, "source": "signal", "signal_source": "market",
        "signal_id": None, "asset": "sports", "ticker": ticker, "side": "yes",
        "contracts": 10, "entry_cents": 60, "order_type": "GTC",
        "reason": "t", "close_time": close_time, "network": ENV,
    }) if not resolved else _shadow_resolved(conn, sid, ticker)


def _shadow_resolved(conn, sid, ticker):
    rid = db.insert_script_shadow(conn, {
        "script_id": sid, "source": "signal", "signal_source": "market",
        "signal_id": None, "asset": "sports", "ticker": ticker, "side": "yes",
        "contracts": 10, "entry_cents": 60, "order_type": "GTC",
        "reason": "t", "close_time": "", "network": ENV,
    })
    conn.execute("UPDATE script_shadow_orders SET resolved=1 WHERE id=?", (rid,))
    return rid


def _count_shadow() -> int:
    with db.get_db() as c:
        return c.execute("SELECT COUNT(*) FROM script_shadow_orders").fetchone()[0]


# ────────────────────────────────────────────────────────────────────────
# The wipe has to include the shadow ledger.
# ────────────────────────────────────────────────────────────────────────


def test_clear_trade_history_wipes_the_shadow_ledger(fresh_db):
    """A script's shadow orders ARE a track record — the Scripts tab renders
    them as W/L and P&L next to the live ones. Left behind by "start my stats
    fresh", the page showed a record from before the reset."""
    with db.get_db() as c:
        _shadow(c, ticker="0xa")
    assert _count_shadow() == 1
    db.clear_trade_history()
    assert _count_shadow() == 0


def test_factory_reset_wipes_the_shadow_ledger(fresh_db):
    with db.get_db() as c:
        _shadow(c, ticker="0xa")
    db.factory_reset()
    assert _count_shadow() == 0


def test_a_wipe_stamps_a_reconcile_floor(fresh_db):
    """The venue's trade history outlives the wipe, and the terminal's fill
    walk has no cursor of its own — so without this floor the next reconcile
    re-imports every historical fill stamped 'external', permanently."""
    with db.get_db() as c:
        assert db.terminal_reconcile_floor(c) is None
    db.clear_trade_history()
    with db.get_db() as c:
        floor = db.terminal_reconcile_floor(c)
    assert floor and floor.endswith("Z")


def test_factory_reset_stamps_the_floor_after_wiping_app_kv(fresh_db):
    """Ordering matters: factory_reset clears app_kv, so a floor written
    before the delete would be the first thing destroyed."""
    db.factory_reset()
    with db.get_db() as c:
        assert db.terminal_reconcile_floor(c) is not None


# ────────────────────────────────────────────────────────────────────────
# ...and the sweep has to include it too.
# ────────────────────────────────────────────────────────────────────────


def test_cleanup_prunes_settled_shadow_orders_and_keeps_open_ones(fresh_db):
    """Never swept at all before this: a script left in shadow mode grew the
    table forever. Unresolved rows are KEPT — one still holds a slot in the
    per-script open cap, so deleting it would silently free the cap."""
    old = (datetime.now(timezone.utc) - timedelta(days=90)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.get_db() as c:
        old_done = _shadow_resolved(c, "s1", "0xold")
        old_open = _shadow(c, ticker="0xopen")
        _shadow_resolved(c, "s1", "0xrecent")
        c.execute("UPDATE script_shadow_orders SET created_at=? WHERE id IN (?,?)",
                  (old, old_done, old_open))

    db.cleanup_old_data()

    with db.get_db() as c:
        left = {r["ticker"] for r in c.execute(
            "SELECT ticker FROM script_shadow_orders")}
    assert left == {"0xopen", "0xrecent"}


# ────────────────────────────────────────────────────────────────────────
# One definition of "stale", or the open cap is not a cap.
# ────────────────────────────────────────────────────────────────────────


def test_an_old_row_that_can_still_settle_keeps_its_cap_slot(fresh_db):
    """The two definitions disagreed: the cap aged a row out 7 days after it
    was CREATED, while the settler retires one 7 days after it CLOSES. A
    shadow order on a market three weeks out therefore stopped counting a
    fortnight before it could possibly settle, and the per-script open cap
    could be exceeded without limit."""
    future = (datetime.now(timezone.utc) + timedelta(days=21)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc)
           - timedelta(days=db.SHADOW_STALE_DAYS + 1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.get_db() as c:
        rid = _shadow(c, ticker="0xlong", close_time=future)
        c.execute("UPDATE script_shadow_orders SET created_at=? WHERE id=?",
                  (old, rid))
        assert db.count_open_script_shadow(c, "s1", ENV) == 1
        # And the settler agrees it is not stale — that is the whole point.
        assert db.resolve_script_shadow(c, lambda *_: 0.0, market_results={}) == 0
        assert db.count_open_script_shadow(c, "s1", ENV) == 1


def test_a_row_the_settler_can_never_reach_still_ages_out(fresh_db):
    """The other side of the same rule: a row with no close time at all is
    invisible to the settler's backstop, so the cap has to age it out itself
    or it holds a slot for the rest of time."""
    old = (datetime.now(timezone.utc)
           - timedelta(days=db.SHADOW_STALE_DAYS + 1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    with db.get_db() as c:
        rid = _shadow(c, ticker="0xnoclose", close_time="")
        assert db.count_open_script_shadow(c, "s1", ENV) == 1
        c.execute("UPDATE script_shadow_orders SET created_at=? WHERE id=?",
                  (old, rid))
        assert db.count_open_script_shadow(c, "s1", ENV) == 0


# ────────────────────────────────────────────────────────────────────────
# Simulated money is not money.
# ────────────────────────────────────────────────────────────────────────


def _c15(conn, *, dry_run: int, pnl: float, ticker: str):
    conn.execute(
        """INSERT INTO crypto15m_positions
             (asset, series, ticker, side, direction, target_contracts,
              filled_contracts, entry_limit_cents, client_order_id, status,
              network, dry_run, resolved, outcome_correct, pnl_usd,
              resolved_at)
           VALUES ('BTC','S',?,'up','yes',10,10,60,?,'settled',?,?,1,?,?,
                   datetime('now'))""",
        (ticker, f"co_{ticker}_{dry_run}", ENV, dry_run,
         1 if pnl > 0 else 0, pnl),
    )


def test_crypto15m_stats_ignores_dry_run_rows(fresh_db):
    """Its two siblings (strategy stats, resolved history) both filter
    dry_run; this one did not, so a simulated row counted as a real win and
    real realized P&L — and this figure drives the take-profit kill-switch."""
    with db.get_db() as c:
        _c15(c, dry_run=0, pnl=-2.0, ticker="0xreal")
        _c15(c, dry_run=1, pnl=50.0, ticker="0xfake")
        stats = db.crypto15m_stats(c, ENV)
    assert stats["total"] == 1
    assert stats["wins"] == 0 and stats["losses"] == 1
    assert stats["realizedPnlUsd"] == -2.0


def test_the_loss_breaker_ignores_dry_run_rows(fresh_db):
    """A SIMULATED win must not pay down a real drawdown (nor a simulated
    loss trip a real breaker)."""
    with db.get_db() as c:
        _c15(c, dry_run=0, pnl=-30.0, ticker="0xreal")
        _c15(c, dry_run=1, pnl=1000.0, ticker="0xfake")
        assert db.lifetime_realized_pnl(c, "crypto15m", ENV) == -30.0


# ────────────────────────────────────────────────────────────────────────
# Speed: the Signals read must not scan.
# ────────────────────────────────────────────────────────────────────────


def test_the_signals_whale_read_walks_an_index(fresh_db):
    """service._h_signals reads whale_trades newest-first every poll. With no
    index on created_at that is a full scan plus a temporary B-tree sort."""
    with db.get_db() as c:
        plan = " ".join(str(r[-1]) for r in c.execute(
            """EXPLAIN QUERY PLAN
               SELECT * FROM whale_trades WHERE confidence >= ?
               ORDER BY created_at DESC LIMIT ?""", (0.0, 200)))
    assert "idx_whale_time" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan
