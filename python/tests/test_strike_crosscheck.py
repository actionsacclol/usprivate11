"""Gamma publishes each up/down window's official strike,
`eventMetadata.priceToBeat`, but only after the window closes (measured
2026-10-11: 20-30s after the close, never while the window trades). So it
can't price a live window; it checks the strike we captured. The recorder
compares the two when it resolves a window and logs a disagreement.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

import crypto15m_record
import db
import polymarket_api as api


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _closed_market(cid, *, meta):
    # The shape a closed up/down market comes back in from
    # /markets?condition_ids=…&closed=true (public GET, 2026-10-11): the
    # official strike rides on the embedded event, not the market.
    ev = {"slug": "btc-updown-15m-1791729000", "tags": []}
    if meta is not None:
        ev["eventMetadata"] = meta
    return {"conditionId": cid, "slug": "btc-updown-15m-1791729000",
            "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]',
            "clobTokenIds": '["1", "2"]', "closed": True,
            "umaResolutionStatus": "resolved", "events": [ev]}


def _signal(ticker, asset, strike, source):
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.get_db() as conn:
        db.insert_crypto15m_signal(conn, {
            "ticker": ticker, "asset": asset, "close_time": past, "mins_left": 6.0,
            "favorite": "up", "favorite_price": 0.7, "entry_cost": 0.71,
            "strike": strike, "strike_source": source, "network": "mainnet"})


def test_resolve_flags_a_captured_strike_the_official_one_disagrees_with(
        fresh_db, monkeypatch, caplog):
    _signal("0xbtc", "BTC", 83100.0, "spot-avg")     # 12 bps high
    _signal("0xeth", "ETH", 2500.05, "twap")        # 0.2 bps: agrees
    official = {"0xbtc": 83000.0, "0xeth": 2500.0}

    async def _fetch(ticker):
        return {"result": "yes", "price_to_beat": official[ticker]}
    monkeypatch.setattr(crypto15m_record.polymarket_api, "fetch_market", _fetch)

    with caplog.at_level(logging.WARNING, logger="crypto15m_record"):
        n = asyncio.run(crypto15m_record._resolve(datetime.now(timezone.utc).timestamp()))
    assert n == 2
    flagged = [r.getMessage() for r in caplog.records if "strike" in r.getMessage()]
    assert len(flagged) == 1
    assert "BTC" in flagged[0] and "spot-avg" in flagged[0] and "+12.0 bps" in flagged[0]


def test_resolve_without_an_official_strike_is_quiet(fresh_db, monkeypatch, caplog):
    _signal("0xold", "BTC", 83010.0, "twap")

    async def _fetch(ticker):
        return {"result": "no", "price_to_beat": None}
    monkeypatch.setattr(crypto15m_record.polymarket_api, "fetch_market", _fetch)
    with caplog.at_level(logging.WARNING, logger="crypto15m_record"):
        assert asyncio.run(crypto15m_record._resolve(datetime.now(timezone.utc).timestamp())) == 1
    assert not [r for r in caplog.records if "strike" in r.getMessage()]
