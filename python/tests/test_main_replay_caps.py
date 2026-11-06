"""The main-engine replay applies the live entry caps.

Audit 2026-10-11: replay.replay_main simulated one trade per accepted signal
and ignored max_positions_per_event, max_open_positions, the daily cap and
the one-position-per-market/side dedupe. A hot event prints dozens of whale
signals that settle on ONE outcome; the live engine takes one of them (the
default per-event cap is 1), the replay took them all, so a single event
could carry, or sink, the whole result.

Both now go through trader.entry_cap_block, the engine's own gate.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
import replay
import trader
from config import merge_with_defaults


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "main-replay.db")
    db.init_db()


# 01:00 UTC three days ago: every seeded print lands on the same UTC day.
_T0 = (datetime.now(timezone.utc) - timedelta(days=3)).replace(
    hour=1, minute=0, second=0, microsecond=0)


def _ts(hours: float) -> str:
    return (_T0 + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _whale(n: int, ticker: str, event: str, at_h: float, resolved_h: float,
           correct: bool, side: str = "yes", price: float = 0.5) -> None:
    resolved_at = (_T0 + timedelta(hours=resolved_h)).isoformat()
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO whale_trades (trade_id, ticker, event_ticker, title,
                   category, taker_side, price, dollar_value, confidence,
                   resolved, outcome_correct, resolved_at, created_at)
               VALUES (?, ?, ?, 't', 'sports', ?, ?, 5000, 80, 1, ?, ?, ?)""",
            (f"W{n}", ticker, event, side, price, 1 if correct else 0,
             resolved_at, _ts(at_h)),
        )


def _cfg(**over):
    cfg = merge_with_defaults({
        "trade_whales": True, "trade_momentum": False,
        "min_confidence_whale": 0.0, "min_edge_pts_whale": -100.0,
        "min_entry_price_cents": 1, "max_entry_price_cents": 99,
        "max_open_positions": 25, "max_positions_per_event": 1,
        "unlimited_daily_new_positions": True,
    })
    cfg.update(over)
    return cfg


def test_a_hot_event_trades_once_while_its_position_is_open(fresh_db):
    # Ten whale prints on one match, all before it settles: live takes ONE.
    for i in range(10):
        _whale(i, f"M{i % 3}", "EV-HOT", at_h=i * 0.1, resolved_h=5, correct=False)
    # A different event in the same span is not blocked by the first one.
    _whale(50, "OTHER", "EV-OTHER", at_h=0.5, resolved_h=5, correct=True)
    res = replay.replay_main(_cfg(), since_days=30)
    assert res["windowsScanned"] == 11
    assert res["n"] == 2
    assert sorted(t["ticker"] for t in res["trades"]) == ["M0", "OTHER"]


def test_the_event_frees_up_once_its_market_resolves(fresh_db):
    _whale(1, "M1", "EV", at_h=0, resolved_h=2, correct=True)
    _whale(2, "M2", "EV", at_h=1, resolved_h=6, correct=True)   # blocked: M1 open
    _whale(3, "M3", "EV", at_h=3, resolved_h=6, correct=True)   # M1 settled at 2h
    res = replay.replay_main(_cfg(), since_days=30)
    assert [t["ticker"] for t in res["trades"]] == ["M1", "M3"]


def test_one_position_per_market_side_and_the_max_open_cap(fresh_db):
    # Per-event cap lifted: the market/side dedupe still holds a repeat print...
    _whale(1, "A", "", at_h=0, resolved_h=9, correct=True)
    _whale(2, "A", "", at_h=1, resolved_h=9, correct=True)
    # ...and max_open (2) stops the third distinct market.
    _whale(3, "B", "", at_h=2, resolved_h=9, correct=True)
    _whale(4, "C", "", at_h=3, resolved_h=9, correct=True)
    res = replay.replay_main(_cfg(max_positions_per_event=99, max_open_positions=2),
                             since_days=30)
    assert [t["ticker"] for t in res["trades"]] == ["A", "B"]


def test_the_daily_cap_counts_entries_per_utc_day(fresh_db):
    for i in range(4):
        _whale(i, f"D{i}", "", at_h=i * 0.25, resolved_h=0.1 + i * 0.25, correct=True)
    res = replay.replay_main(
        _cfg(unlimited_daily_new_positions=False, max_daily_new_positions=2,
             max_positions_per_event=99), since_days=30)
    assert res["n"] == 2


def test_the_live_engine_and_the_replay_share_one_gate():
    """The skip reasons are the engine's, word for word."""
    cfg = _cfg()
    why = trader.entry_cap_block(
        cfg, open_count=lambda: 0, today_count=lambda: 0,
        event_count=lambda: 1, market_side_open=lambda: False)
    assert why == "event already at its max (1) position(s)"
    assert trader.entry_cap_block(
        cfg, open_count=lambda: 0, today_count=lambda: 0,
        event_count=lambda: 0, market_side_open=lambda: False) is None
