"""Gamma's real top of book, end to end through the market table.

`_normalize_market` used to store the mid as both bid and ask, so every reader
of the markets table saw a zero spread. It now keeps Gamma's bestBid/bestAsk
when the book is two-sided. These pin what that means for the readers: a
decide_market() script sees the real spread, and the momentum scanner keeps
measuring the mid rather than a bid that sits half a spread lower.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import db
import polymarket_api as api
import scanner
from script_backtest import market_to_js


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "markets.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _gamma(cid: str, **over) -> dict:
    raw = {
        "question": "Will it?", "slug": cid,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.41", "0.59"]),
        "endDate": "2099-01-01T00:00:00Z", "active": True, "closed": False,
    }
    raw.update(over)
    return raw


def _sync(*raws) -> None:
    """The market sync path: normalise, then write in the worker the scanner uses."""
    markets = [api._normalize_market(r) for r in raws]
    scanner._write_markets_blocking(markets, "2026-10-11T00:00:00Z")


def test_script_market_ctx_sees_the_real_spread(fresh_db):
    _sync(_gamma("0xtwo", bestBidQuote={"value": "0.40"}, bestAskQuote={"value": "0.42"}),
          _gamma("0xone", bestAskQuote={"value": "0.42"}))           # no bids resting
    with db.get_db() as conn:
        two = market_to_js(db.get_market(conn, "0xtwo"))
        one = market_to_js(db.get_market(conn, "0xone"))
    assert two["yesBid"] == 0.40 and two["yesAsk"] == 0.42
    assert two["spreadCents"] == pytest.approx(2.0)
    # A one-sided book is not a spread: the gate still fails closed.
    assert one["spreadCents"] is None


def _momentum_once(monkeypatch, *, bid, ask, mid, prev_mid):
    """One momentum pass over a single market whose previous snapshot had
    bid == ask == prev_mid (the old storage). Returns the alerts inserted."""
    with db.get_db() as conn:
        db.upsert_market(conn, {
            "ticker": "0xmom", "title": "Will it?", "status": "open",
            "close_time": "2099-01-01T00:00:00Z", "volume": 5000.0,
            "volume_24h": 900.0, "open_interest": 100.0,
            "yes_bid": bid, "yes_ask": ask, "last_price": mid,
        })
        # The scan compares against the SECOND most recent snapshot.
        for _ in range(2):
            db.save_snapshot(conn, "0xmom", {
                "volume": 5000.0, "volume_24h": 900.0, "open_interest": 100.0,
                "yes_bid": prev_mid, "last_price": prev_mid,
            })

    async def _no_trades(limit=1000):
        return []

    monkeypatch.setattr(api, "fetch_recent_trades", _no_trades)
    cfg = {"allowed_momentum_signal_types": ["price_move"], "contrarian_only": False}
    _n, rows = asyncio.run(scanner.scan_momentum(cfg))
    return rows


def test_momentum_does_not_read_half_the_spread_as_a_price_move(fresh_db, monkeypatch):
    # The mid has not moved; only the stored bid is now the real one, 10c under.
    rows = _momentum_once(monkeypatch, bid=0.30, ask=0.50, mid=0.40, prev_mid=0.40)
    assert rows == []


def test_momentum_still_fires_on_a_real_mid_move(fresh_db, monkeypatch):
    rows = _momentum_once(monkeypatch, bid=0.49, ask=0.51, mid=0.50, prev_mid=0.40)
    assert [(r["signal_type"], r["direction"]) for r in rows] == [("price_move", "yes")]
    assert rows[0]["price"] == pytest.approx(0.50)
