"""Tests for the market-WS trade-print recording path (2026-07-16).

Covers parse_trade, the feed's trade buffering and drain semantics (bounded,
return-and-clear), the quote frames, and the clob_trades DB round-trip that the
recorder loop performs.
"""
from __future__ import annotations

import pytest

import clob_ws
import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


_TRADE_MSG = {"trade": {
    "marketSlug": "cpc-btc-updown-15m-2026-11-05-0130z",
    "price": {"value": "0.2400", "currency": "USD"},
    "quantity": {"value": "6.2083", "currency": "USD"},
    "tradeTime": "2026-11-05T01:32:20Z",
}}


def test_parse_trade_shape():
    row = clob_ws.parse_trade(_TRADE_MSG)
    assert row is not None
    assert row["price"] == pytest.approx(0.24)
    assert row["size"] == pytest.approx(6.2083)
    assert row["side"] is None                  # trades are anonymous here
    assert row["source_ts_ms"] > 0
    assert row["ingest_ts"] > 0


def test_parse_trade_rejects_garbage():
    assert clob_ws.parse_trade({}) is None
    assert clob_ws.parse_trade({"trade": {"marketSlug": "x", "price": {"value": "abc"}}}) is None
    assert clob_ws.parse_trade({"trade": {"marketSlug": "x", "price": {"value": "1.5"}}}) is None
    # missing optional fields still parse
    row = clob_ws.parse_trade({"trade": {"marketSlug": "x", "price": {"value": "0.5"}}})
    assert row is not None and row["size"] is None and row["source_ts_ms"] is None


def test_dispatch_buffers_and_drain_clears():
    feed = clob_ws.ClobMarketFeed()
    feed._dispatch(dict(_TRADE_MSG))
    assert len(feed._trades) == 1
    assert feed.drain_trades()[0]["market"] == _TRADE_MSG["trade"]["marketSlug"]
    assert feed.drain_trades() == []          # drained = cleared


def test_trade_buffer_is_bounded():
    feed = clob_ws.ClobMarketFeed()
    for i in range(feed._TRADES_MAX + 50):
        feed._dispatch({"trade": {**_TRADE_MSG["trade"], "price": {"value": str(0.1 + (i % 2) * 0.1)}}})
    assert len(feed._trades) == feed._TRADES_MAX


def test_a_lite_frame_updates_both_sides_of_the_quote():
    feed = clob_ws.ClobMarketFeed()
    feed._dispatch({"marketDataLite": {"marketSlug": "m", "bestBid": {"value": "0.23"},
                                       "bestAsk": {"value": "0.24"}}})
    assert feed.get_quote_cents("m#yes") == {"bid_cents": 23, "ask_cents": 24}
    assert feed.get_quote_cents("m#no") == {"bid_cents": 76, "ask_cents": 77}


def test_a_subcent_quote_rounds_away_from_the_touch():
    feed = clob_ws.ClobMarketFeed()
    feed._dispatch({"marketDataLite": {"marketSlug": "m", "bestBid": {"value": "0.695"},
                                       "bestAsk": {"value": "0.705"}}})
    assert feed.get_quote_cents("m#yes") == {"bid_cents": 69, "ask_cents": 71}


def test_clob_trades_db_roundtrip(fresh_db):
    rows = [clob_ws.parse_trade(_TRADE_MSG),
            clob_ws.parse_trade({"trade": {"marketSlug": "y", "price": {"value": "0.61"}}})]
    with db.get_db() as conn:
        n = db.insert_clob_trades(conn, rows, network="mainnet")
    assert n == 2
    with db.get_db() as conn:
        got = conn.execute(
            "SELECT asset_id, price, source_ts_ms, network "
            "FROM clob_trades ORDER BY id").fetchall()
    assert len(got) == 2
    assert got[0]["price"] == pytest.approx(0.24)
    assert got[0]["network"] == "mainnet"
    assert got[1]["source_ts_ms"] is None


def test_insert_clob_trades_skips_invalid(fresh_db):
    with db.get_db() as conn:
        db.insert_clob_trades(conn, [{"asset_id": None, "price": 0.5},
                                     {"asset_id": "ok", "price": 0.5}])
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM clob_trades").fetchone()["c"]
    assert n == 1
