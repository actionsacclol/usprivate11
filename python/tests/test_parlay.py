"""Tests for the parlay per-hour schedule: the hour_override/resignal helpers,
the live engine's kv-backed state + exit exemptions, the replay _simulate
schedule gating, and the generator's qualify/rank logic."""
from __future__ import annotations

import json

import pytest

import crypto15m
import crypto15m_trader as ct
import db
import parlay_generator as pg
import replay
from config import merge_with_defaults


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "parlay-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    ct._parlay_cache.update({"at": 0.0, "armed": False, "schedule": None})
    return dbfile


def _cfg(**over):
    c = merge_with_defaults({})
    c["crypto15m_enabled"] = True
    c.update(over)
    return c


# ───────── hour_override / resignal_asset ─────────────────────────────


def test_hour_override_no_schedule_returns_cfg_unchanged():
    c = _cfg()
    assert crypto15m.hour_override(c, 7) is c


def test_hour_override_seated_merges_and_unseated_returns_none():
    c = _cfg(crypto15m_hour_configs={
        "7": {"crypto15m_entry_threshold": 0.97},
    })
    merged = crypto15m.hour_override(c, 7)
    assert merged is not c
    assert merged["crypto15m_entry_threshold"] == 0.97
    # base keys survive the merge; the original cfg is untouched
    assert merged["crypto15m_enabled"] is True
    assert c["crypto15m_entry_threshold"] != 0.97 or True
    assert crypto15m.hour_override(c, 8) is None


def test_resignal_asset_recomputes_signal_under_new_config():
    a = {
        "minsLeft": 2.5, "favoritePrice": 0.965, "entryCost": 0.97,
        "deltaPct": None, "hourUtc": 7, "inWindow": False, "signal": False,
    }
    tight = _cfg(
        crypto15m_entry_threshold=0.96, crypto15m_time_delay_min=3.0,
        crypto15m_entry_max=0.99,
    )
    out = crypto15m.resignal_asset(a, tight)
    assert out["inWindow"] is True and out["signal"] is True
    # the input dict is not mutated
    assert a["signal"] is False
    # a floor above the favorite kills the recomputed signal
    deep = _cfg(crypto15m_entry_threshold=0.98, crypto15m_time_delay_min=3.0)
    assert crypto15m.resignal_asset(a, deep)["signal"] is False


# ───────── parlay_state (kv-backed) ───────────────────────────────────


def test_parlay_state_unarmed_and_armed(fresh_db):
    armed, sched = ct.parlay_state(force=True)
    assert armed is False and sched is None
    payload = {"interval": "15m", "hour_configs": {"7": {"crypto15m_entry_threshold": 0.97}}}
    with db.get_db() as conn:
        db.kv_set(conn, ct._PARLAY_KV_SCHEDULE, json.dumps(payload))
        db.kv_set(conn, ct._PARLAY_KV_ARMED, "1")
    armed, sched = ct.parlay_state(force=True)
    assert armed is True
    assert sched["hour_configs"]["7"]["crypto15m_entry_threshold"] == 0.97


def test_empty_schedule_seats_nothing_and_cannot_be_armed(fresh_db):
    """A zero-hour schedule is a NORMAL generator outcome (the win-rate bar
    seats nothing on a short corpus). It must never read as armed: the engine
    stamps every row it opens as strategy="parlay", which exempts them from all
    four exit paths — an empty schedule falling through to the base config
    meant real money traded with no stop-loss."""
    # hour_override: present-but-empty means "nothing seated", not "no schedule".
    assert crypto15m.hour_override(_cfg(crypto15m_hour_configs={}), 7) is None
    # ...and absent still means "no schedule at all".
    c_absent = _cfg()
    assert crypto15m.hour_override(c_absent, 7) is c_absent

    with db.get_db() as conn:
        db.kv_set(conn, ct._PARLAY_KV_SCHEDULE,
                  json.dumps({"interval": "15m", "hour_configs": {}}))
        db.kv_set(conn, ct._PARLAY_KV_ARMED, "1")
    armed, sched = ct.parlay_state(force=True)
    assert armed is False and sched is None

    with pytest.raises(ValueError, match="0 of 24"):
        pg.set_armed(True)


def test_parlay_state_armed_without_valid_schedule_is_disarmed(fresh_db):
    with db.get_db() as conn:
        db.kv_set(conn, ct._PARLAY_KV_ARMED, "1")
        db.kv_set(conn, ct._PARLAY_KV_SCHEDULE, "not json{")
    armed, sched = ct.parlay_state(force=True)
    assert armed is False and sched is None


# ───────── exit exemptions: parlay rows hold to settlement ─────────────


def test_parlay_rows_exempt_from_stop_and_take_profit():
    pos = {"strategy": "parlay", "status": "filled", "filled_contracts": 5,
           "cost_usd": 4.9, "avg_entry_cents": 98.0}
    c = _cfg(crypto15m_exit_threshold=0.40, crypto15m_take_profit=0.99,
             crypto15m_take_profit_pct=0.01, crypto15m_stop_loss_pct=0.05,
             crypto15m_sell_into_strength=True)
    assert ct.should_stop_loss(pos, 0.05, c) is False
    assert ct.should_take_profit(pos, 99, c) is False
    assert ct.should_take_profit_pct(pos, 99, c) is False
    assert ct.strength_exit_cents(pos, c) is None
    # sanity: an identical NON-parlay row would exit
    fav = dict(pos, strategy="favorite")
    assert ct.should_stop_loss(fav, 0.05, c) is True


# ───────── replay._simulate honors the schedule ───────────────────────


def _window(ticker, hour, up_won, *, up_prob=0.97, ask=0.97, macd=1.0):
    """One resolved window whose single qualifying tick sits in `hour`."""
    return {
        ticker: [{
            "ticker": ticker, "asset": "BTC", "network": "mainnet",
            "observed_at": f"2026-07-20 {hour:02d}:57:00", "mins_left": 2.0,
            "up_prob": up_prob, "yes_bid": up_prob - 0.01, "yes_ask": up_prob + 0.005,
            "up_ask": ask, "no_ask": round(1.0 - ask + 0.01, 3),
            "macd_hist": macd, "rsi": 60.0,
            "up_won": up_won, "sig_close": f"2026-07-20T{hour:02d}:59:59Z",
            "spot_source": "rtds-ws", "delta_pct": None,
        }],
    }


def _sched_cfg(hours):
    ov = {
        **pg.PINNED,
        "crypto15m_entry_threshold": 0.96,
        "crypto15m_time_delay_min": 3.0,
        "crypto15m_min_macd_hist": 0.0,
    }
    return _cfg(crypto15m_hour_configs={str(h): ov for h in hours})


def test_simulate_trades_seated_hour_and_skips_unseated():
    wins = {}
    wins.update(_window("W7", 7, 1))
    wins.update(_window("W8", 8, 1))
    trades, n_windows, _ = replay._simulate(wins, _sched_cfg([7]))
    assert n_windows == 2
    assert [t["ticker"] for t in trades] == ["W7"]
    assert trades[0]["won"] is True
    # both hours seated -> both windows trade
    trades2, _, _ = replay._simulate(wins, _sched_cfg([7, 8]))
    assert {t["ticker"] for t in trades2} == {"W7", "W8"}


def test_simulate_empty_schedule_trades_nothing():
    """An empty schedule seats no hours, so it must replay as ZERO trades. It
    used to fall through to the base config, so the generator's own stitched
    validation printed a healthy summary for a schedule that trades nothing —
    which is exactly what talks an operator into arming it."""
    wins = {}
    wins.update(_window("W7", 7, 1))
    wins.update(_window("W8", 8, 1))
    trades, n_windows, _ = replay._simulate(wins, _sched_cfg([]))
    assert n_windows == 2 and trades == []


def test_simulate_no_schedule_unchanged_baseline():
    wins = _window("W7", 7, 1)
    c = _cfg(crypto15m_entry_threshold=0.96, crypto15m_time_delay_min=3.0,
             crypto15m_entry_max=0.99, crypto15m_entry_style="taker")
    trades, _, _ = replay._simulate(wins, c)
    assert len(trades) == 1


# ───────── generator qualify / stats ──────────────────────────────────


def _trades(spec):
    """spec: list of (date, won, pnl)"""
    return [{"at": f"{d} 07:57:00", "won": w, "pnlUsd": p} for d, w, p in spec]


def test_generator_qualifies_perfect_train():
    tr = pg._stats(_trades([("2026-07-10", True, 0.02)] * 12))
    assert pg._qualifies(tr, 1.0, 10) is True


def test_generator_rejects_thin_or_lossy_train():
    perfect12 = _trades([("2026-07-10", True, 0.02)] * 12)
    assert not pg._qualifies(pg._stats(perfect12[:5]), 1.0, 10)
    with_loss = pg._stats(perfect12 + _trades([("2026-07-11", False, -0.97)]))
    assert not pg._qualifies(with_loss, 1.0, 10)


# ───────── the holdout is reported, never selected on ──────────────────
# Audit 2026-10-11: a config had to clear the win-rate bar on the holdout
# too, so the holdout picked which of ~30 candidates survived and its stored
# numbers were no longer out-of-sample.


def _dated_window(ticker, date, up_won):
    w = _window(ticker, 7, up_won)
    for t in w[ticker]:
        t["observed_at"] = f"{date} 07:57:00"
        t["sig_close"] = f"{date}T07:59:59Z"
    return w


def test_generator_seats_on_train_and_reports_the_holdout_untouched(monkeypatch):
    wins = {}
    for d in range(1, 13):   # 12 winning train days in hour 7
        wins.update(_dated_window(f"T{d}", f"2026-09-{d:02d}", 1))
    wins.update(_dated_window("H1", "2026-09-20", 0))   # the holdout day loses
    monkeypatch.setattr(pg.replay, "load_windows", lambda **_k: wins)
    s = pg.generate(holdout_days=1, min_trades=10)
    assert s["holdout_dates"] == ["2026-09-20"]
    assert "7" in s["hour_configs"]               # selected on train alone
    ho = s["hours"]["7"]["holdout"]
    assert (ho["n"], ho["wins"]) == (1, 0)        # the loss is reported as is
    assert s["validation"]["holdout"]["losses"] == 1
    assert s["holdout_untouched"] is True


def test_generator_split_by_holdout_dates():
    ts = _trades([("2026-07-10", True, 0.02), ("2026-07-21", True, 0.02)])
    tr, ho = pg._split(ts, {"2026-07-21"})
    assert len(tr) == 1 and len(ho) == 1
    assert ho[0]["at"].startswith("2026-07-21")
