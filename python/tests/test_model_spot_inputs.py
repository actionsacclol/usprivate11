"""The settlement model's spot input: it must be live, or the model sits the
window out.

Outside the final minute nothing else checked the spot. When every price feed
was down the snapshot served the last good REST price, marked "(stale)", for
as long as the outage lasted, and the model priced live windows off it.
"""
from __future__ import annotations

import asyncio
import time

import crypto15m
import crypto15m_trader as c15t
from config import merge_with_defaults


def _stub_board(monkeypatch, *, interval="15m"):
    """One live market per asset, a live book, a working sigma and an official
    strike. Returns the cfg. Every network edge is stubbed."""
    sec = crypto15m._INTERVAL_SEC[interval]

    async def _mkts(asset, iv, *, fast=False):
        ws = int(time.time()) // sec * sec
        return [{"ticker": f"{asset}-T", "yes_token": f"{asset}-Y", "no_token": f"{asset}-N",
                 "yes_bid": 0.5, "yes_bid_dollars": 0.5,
                 "window_start_epoch": ws, "window_close_epoch": ws + sec,
                 "close_time": "", "fee_schedule": None}]
    monkeypatch.setattr(crypto15m.polymarket_api, "fetch_crypto_updown", _mkts)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "start", lambda: None)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "observe", lambda *a: None)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "get_quote_cents",
                        lambda tok, max_age=12.0: {"bid_cents": 49, "ask_cents": 51})

    async def _ind(asset):
        return {"sigma1m": 0.001}
    monkeypatch.setattr(crypto15m, "asset_indicators", _ind)
    # No live push feed covers any asset: the REST layer is all there is.
    monkeypatch.setattr(crypto15m.spot_ws, "fresh_spots", lambda: {})
    monkeypatch.setattr(crypto15m.spot_ws, "spot", lambda a: None)
    monkeypatch.setattr(crypto15m.index_feed, "twap_at", lambda a, t: 100.0)
    monkeypatch.setattr(crypto15m, "_get_spot_client", lambda: None)
    monkeypatch.setattr(crypto15m, "_window_open", {})
    monkeypatch.setattr(crypto15m, "_snapshot_cache", {"at": 0.0, "data": None, "interval": None})
    return merge_with_defaults({
        "crypto15m_interval": interval,
        "crypto15m_arb_detect": False, "crypto15m_imbalance_detect": False,
        "crypto15m_indicator_detect": True, "crypto15m_ws_book": True,
    })


def _rest(prices):
    async def _ok(_client):
        return dict(prices)
    return [("coinbase", _ok)]


async def _down(_client):
    raise RuntimeError("feed down")


def _btc(snap):
    return next(a for a in snap["assets"] if a["asset"] == "BTC")


def test_the_model_does_not_run_on_a_stale_spot(monkeypatch):
    cfg = _stub_board(monkeypatch)
    every = {s["asset"]: 100.3 for s in crypto15m.SERIES}

    # Control: a fresh REST spot prices the model.
    monkeypatch.setattr(crypto15m, "_SPOT_SOURCES", _rest(every))
    monkeypatch.setattr(crypto15m, "_spot_cache", {"at": 0.0, "spots": {}, "source": "none"})
    fresh = _btc(asyncio.run(crypto15m.snapshot(cfg)))
    assert fresh["modelProb"] is not None

    # Every feed down: the last good price is still shown, but the model sits
    # out — a frozen spot makes a moving market look mispriced.
    monkeypatch.setattr(crypto15m, "_SPOT_SOURCES", [("cryptocompare", _down)])
    monkeypatch.setattr(crypto15m, "_spot_cache",
                        {"at": -1e12, "spots": dict(every), "source": "cryptocompare"})
    monkeypatch.setattr(crypto15m, "_snapshot_cache", {"at": 0.0, "data": None, "interval": None})
    snap = asyncio.run(crypto15m.snapshot(cfg))
    stale = _btc(snap)
    assert snap["spotSource"].endswith("(stale)")
    assert stale["spotUsd"] == 100.3
    assert stale["modelProb"] is None and stale["edgeNetCents"] is None
    assert stale["spotStale"] is True


def test_a_spot_fetch_timeout_is_a_stale_spot_too(monkeypatch):
    cfg = _stub_board(monkeypatch)
    every = {s["asset"]: 100.3 for s in crypto15m.SERIES}

    async def _hang():
        raise asyncio.TimeoutError()
    monkeypatch.setattr(crypto15m, "_fetch_spots", _hang)
    monkeypatch.setattr(crypto15m, "_spot_cache",
                        {"at": -1e12, "spots": dict(every), "source": "cryptocompare"})
    btc = _btc(asyncio.run(crypto15m.snapshot(cfg)))
    assert btc["spotUsd"] == 100.3 and btc["modelProb"] is None


def test_a_live_feed_asset_is_not_stale_when_rest_is(monkeypatch):
    # Only the assets the REST cache actually served are stale: one a live
    # push feed covers keeps its model.
    cfg = _stub_board(monkeypatch)
    every = {s["asset"]: 100.3 for s in crypto15m.SERIES}
    monkeypatch.setattr(crypto15m.spot_ws, "fresh_spots", lambda: {"BTC": 100.4})
    monkeypatch.setattr(crypto15m.spot_ws, "spot", lambda a: 100.4 if a == "BTC" else None)
    monkeypatch.setattr(crypto15m, "_SPOT_SOURCES", [("cryptocompare", _down)])
    monkeypatch.setattr(crypto15m, "_spot_cache",
                        {"at": -1e12, "spots": dict(every), "source": "cryptocompare"})
    snap = asyncio.run(crypto15m.snapshot(cfg))
    btc = _btc(snap)
    eth = next(a for a in snap["assets"] if a["asset"] == "ETH")
    assert btc["spotUsd"] == 100.4 and btc["modelProb"] is not None
    assert eth["modelProb"] is None
    assert btc["spotStale"] is False and eth["spotStale"] is True


def test_without_a_feed_the_fresh_rest_touch_prices_the_board(monkeypatch):
    """No API key means no live feed. The uncached REST read still gives a real bid
    and ask, so paper mode must not price off a mid with a spread of zero."""
    cfg = _stub_board(monkeypatch)
    sec = crypto15m._INTERVAL_SEC["15m"]

    async def _mkts(asset, iv, *, fast=False):
        ws = int(time.time()) // sec * sec
        return [{"ticker": f"{asset}-T", "yes_token": f"{asset}-Y", "no_token": f"{asset}-N",
                 "yes_bid_dollars": 0.62, "yes_ask_dollars": 0.64, "last_price_dollars": 0.63,
                 "quote_fresh": True, "window_start_epoch": ws, "window_close_epoch": ws + sec,
                 "close_time": "", "fee_schedule": None}]

    monkeypatch.setattr(crypto15m.polymarket_api, "fetch_crypto_updown", _mkts)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "get_quote_cents", lambda tok, max_age=12.0: None)
    snap = asyncio.run(crypto15m.snapshot(cfg))
    row = next(a for a in snap["assets"] if a["asset"] == "BTC")
    assert row["priceSource"] == "rest"
    assert row["upAsk"] == 0.64 and row["downAsk"] == 0.38
    assert row["yesBid"] == 0.62 and row["yesAsk"] == 0.64
