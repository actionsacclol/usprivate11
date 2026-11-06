"""The trend backfill must only see candles that had closed at the tick.

Audit 2026-10-11 (latent): crypto15m_backfill grouped ticks by minute and
ended its candle slice at the candle the tick sat IN. A 1-minute candle
stamped HH:MM closes at HH:MM+1, so a tick at HH:MM:05 was given a close
from up to 60s in its future: velocity1mPct / change5mPct / priceVsVwapPct
on backfilled rows knew where the price was going. The slice now ends at
the last candle that had closed when the tick was recorded.

No network: the candle fetch is stubbed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import crypto15m_backfill as bf
import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "backfill.db")
    db.init_db()


# A tick 5 seconds into 12:30, two days ago.
_TICK = (datetime.now(timezone.utc) - timedelta(days=2)).replace(
    hour=12, minute=30, second=5, microsecond=0)
_MINUTE_S = int(_TICK.replace(second=0).timestamp())


def _series(minute_s: int) -> dict[int, tuple[float, float]]:
    """Flat at 100 for two hours, then the candle the tick sits in (and the
    next) close at 200: a jump nobody watching at HH:MM:05 could have seen."""
    out = {}
    for k in range(-120, 3):
        t = minute_s + 60 * k
        out[t] = (200.0 if t >= minute_s else 100.0, 1.0)
    return out


def _seed_tick():
    with db.get_db() as conn:
        db.insert_crypto15m_tick(conn, {"ticker": "T1", "asset": "BTC",
                                        "spot": 100.0, "network": "mainnet"})
        conn.execute("UPDATE crypto15m_ticks SET observed_at=?",
                     (_TICK.strftime("%Y-%m-%d %H:%M:%S"),))


def _row():
    with db.get_db() as conn:
        return dict(conn.execute(
            "SELECT velocity1m_pct, change5m_pct, price_vs_vwap_pct FROM crypto15m_ticks"
        ).fetchone())


def test_bulk_backfill_uses_only_closed_candles(fresh_db, monkeypatch):
    _seed_tick()

    async def _fake_series(asset, client, start_ms, end_ms):
        return _series(_MINUTE_S)

    monkeypatch.setattr(bf, "_fetch_series_coinbase", _fake_series)
    res = asyncio.run(bf.backfill_bulk("mainnet", since_days=7))
    assert res["rowsUpdated"] == 1
    row = _row()
    assert row["velocity1m_pct"] == pytest.approx(0.0)
    assert row["change5m_pct"] == pytest.approx(0.0)
    assert row["price_vs_vwap_pct"] == pytest.approx(0.0)


def test_per_group_backfill_asks_for_closed_candles_only(fresh_db, monkeypatch):
    _seed_tick()
    asked: list[int] = []

    async def _fake_fetch(asset, client, end_ms):
        asked.append(end_ms)
        series = _series(_MINUTE_S)
        keep = [series[t] for t in sorted(series)
                if end_ms // 1000 - 90 * 60 <= t <= end_ms // 1000]
        return [c for c, _v in keep], [v for _c, v in keep]

    monkeypatch.setitem(bf._SOURCES, "coinbase", _fake_fetch)
    res = asyncio.run(bf.backfill("mainnet", since_days=7, source="coinbase"))
    assert res["rowsUpdated"] == 1
    # the newest candle requested opened a minute before the tick's minute
    assert asked == [(_MINUTE_S - 60) * 1000]
    assert _row()["velocity1m_pct"] == pytest.approx(0.0)
