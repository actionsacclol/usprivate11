"""Tests for the 15-minute crypto research logger + backtest.

Covers the pure strategy evaluation (favorite vs contrarian, price/delta
filters), the settled-outcome reader, the whole-window tick logger, and
the DB round-trip that feeds the CLI 15m backtest.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import backtest
import crypto15m
import crypto15m_record
import db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    dbfile = tmp_path / "krypt-test.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    return dbfile


def _sig(fav, fav_price, fav_won, *, entry_cost=None, delta_pct=0.1):
    return {
        "favorite": fav,
        "favorite_price": fav_price,
        "entry_cost": entry_cost if entry_cost is not None else fav_price,
        "fav_won": fav_won,
        "delta_pct": delta_pct,
        "mins_left": 7.0,
    }


# ───────── pure strategy evaluation ────────────────────────────────────


def test_favorite_mode_wins_count_as_correct():
    sigs = [_sig("up", 0.8, True), _sig("down", 0.8, True), _sig("up", 0.8, False)]
    r = backtest.crypto15m_eval(sigs, mode="favorite")
    assert r["n"] == 3
    assert r["wins"] == 2  # two favorites won
    # cost = entry_cost = 0.8 for all
    assert r["avg_cost"] == pytest.approx(0.8)


def test_contrarian_is_the_mirror_image():
    # Favorite at 0.8 that LOST -> contrarian (cheap side at 0.2) WON.
    sigs = [_sig("up", 0.8, False)]
    fav = backtest.crypto15m_eval(sigs, mode="favorite")
    con = backtest.crypto15m_eval(sigs, mode="contrarian")
    assert fav["wins"] == 0
    assert con["wins"] == 1
    assert con["avg_cost"] == pytest.approx(0.2)  # 1 - 0.8


def test_favorite_price_band_filters():
    sigs = [_sig("up", 0.60, True), _sig("up", 0.90, True)]
    r = backtest.crypto15m_eval(sigs, mode="favorite", min_fav=0.85)
    assert r["n"] == 1  # only the 0.90 favorite passes


def test_delta_filter_excludes_low_moves():
    sigs = [_sig("up", 0.8, True, delta_pct=0.0005),   # 0.05%
            _sig("up", 0.8, True, delta_pct=0.003)]    # 0.30%
    # require >= 0.20% underlying move -> only the second qualifies
    r = backtest.crypto15m_eval(sigs, mode="favorite", min_delta_pct=0.20)
    assert r["n"] == 1


def test_delta_filter_drops_none_delta():
    sigs = [_sig("up", 0.8, True, delta_pct=None)]
    assert backtest.crypto15m_eval(sigs, mode="favorite", min_delta_pct=0.1)["n"] == 0


def test_report_structure_and_collecting_verdict():
    sigs = [_sig("up", 0.8, True) for _ in range(5)]
    rep = backtest.crypto15m_report(sigs)
    assert rep["n"] == 5
    assert "favorite" in rep and "contrarian" in rep
    assert len(rep["favoriteSweep"]) == len(backtest._FAV_THRESHOLDS)
    assert "macdBreakdown" in rep
    assert "COLLECTING DATA" in rep["verdict"]  # <30 samples


def test_report_header_names_the_recorded_interval():
    """`backtest.py --c15 --interval 5m` used to print "15-minute crypto
    backtest" and "settled 15m signals" over a 5m corpus."""
    sigs = [_sig("up", 0.8, True) for _ in range(5)]
    text = backtest.format_crypto15m_report(
        backtest.crypto15m_report(sigs, interval="5m"), "test.db")
    head = "\n".join(text.splitlines()[:5])
    assert "recording interval: 5m" in head
    assert "settled 5m signals: 5" in head
    assert "15m" not in head and "15-minute" not in head
    assert "5m" in backtest.crypto15m_report(sigs, interval="5m")["verdict"]


# ───────── MACD breakdown (side-aware agree/conflict split) ─────────────


def _macd_sig(fav, fav_won, macd_hist):
    s = _sig(fav, 0.8, fav_won)
    s["macd_hist"] = macd_hist
    return s


def test_macd_aligns_is_side_aware():
    # bullish hist + up favored = aligned; bullish + down favored = conflict
    assert backtest._macd_aligns(_macd_sig("up", True, 0.5)) is True
    assert backtest._macd_aligns(_macd_sig("up", True, -0.5)) is False
    assert backtest._macd_aligns(_macd_sig("down", True, -0.5)) is True
    assert backtest._macd_aligns(_macd_sig("down", True, 0.5)) is False
    # flat / missing histogram is not classifiable
    assert backtest._macd_aligns(_macd_sig("up", True, 0.0)) is None
    assert backtest._macd_aligns(_macd_sig("up", True, None)) is None


def test_macd_breakdown_buckets_and_excludes_missing():
    sigs = [
        _macd_sig("up", True, 0.5),     # agree
        _macd_sig("down", True, -0.3),  # agree
        _macd_sig("up", False, -0.5),   # conflict
        _macd_sig("up", True, 0.0),     # flat -> excluded
        _macd_sig("up", True, None),    # missing -> excluded
    ]
    mb = backtest.crypto15m_macd_breakdown(sigs)
    assert mb["nWithMacd"] == 3
    assert mb["agree"]["n"] == 2 and mb["agree"]["wins"] == 2
    assert mb["conflict"]["n"] == 1 and mb["conflict"]["wins"] == 0


def test_macd_fields_survive_db_roundtrip(fresh_db):
    row = {
        "ticker": "KXBTC15M-M", "asset": "BTC", "series": "KXBTC15M",
        "close_time": "2026-06-08T02:45:00Z", "mins_left": 7.0,
        "favorite": "up", "favorite_price": 0.8, "entry_cost": 0.8,
        "up_prob": 0.8, "delta_pct": 0.001, "network": "mainnet",
        "macd": 1.2, "macd_signal": 0.9, "macd_hist": 0.3,
        "macd_cross": 1, "rsi": 64.5,
    }
    with db.get_db() as conn:
        db.insert_crypto15m_signal(conn, row)
        db.resolve_crypto15m_signal(conn, "KXBTC15M-M", up_won=1)
        loaded = backtest.load_crypto15m_signals(conn)
    assert len(loaded) == 1
    s = loaded[0]
    assert s["macd_hist"] == pytest.approx(0.3)
    assert s["macd_cross"] == pytest.approx(1.0)
    assert s["rsi"] == pytest.approx(64.5)


# ───────── settled-outcome reader ──────────────────────────────────────


def test_settled_up_won_reads_result_and_value():
    assert crypto15m_record._settled_up_won({"result": "yes"}) is True
    assert crypto15m_record._settled_up_won({"result": "no"}) is False
    assert crypto15m_record._settled_up_won(
        {"status": "settled", "settlement_value_dollars": 1.0}) is True
    assert crypto15m_record._settled_up_won(
        {"status": "finalized", "settlement_value": 0}) is False
    # not settled yet
    assert crypto15m_record._settled_up_won({"status": "active", "result": ""}) is None


# ───────── DB round-trip (insert -> resolve -> load) ───────────────────


def test_signal_db_roundtrip(fresh_db):
    row = {
        "ticker": "KXBTC15M-X", "asset": "BTC", "series": "KXBTC15M",
        "close_time": "2026-06-08T02:45:00Z", "mins_left": 7.0,
        "favorite": "down", "favorite_price": 0.64, "entry_cost": 0.66,
        "up_prob": 0.36, "delta_pct": 0.001, "network": "mainnet",
    }
    with db.get_db() as conn:
        assert db.insert_crypto15m_signal(conn, row) is True
        assert db.insert_crypto15m_signal(conn, row) is False  # dedup on ticker
        counts = db.crypto15m_signal_counts(conn)
        assert counts == {"total": 1, "resolved": 0, "pending": 1}
        # not yet resolved -> backtest sees nothing
        assert backtest.load_crypto15m_signals(conn) == []
        db.resolve_crypto15m_signal(conn, "KXBTC15M-X", up_won=0)  # down won
        loaded = backtest.load_crypto15m_signals(conn)
    assert len(loaded) == 1
    s = loaded[0]
    assert s["favorite"] == "down" and s["fav_won"] is True  # down favorite, down won
    assert s["entry_cost"] == pytest.approx(0.66)


# ───────── whole-window tick logger ────────────────────────────────────


def test_record_tick_logs_ticks_and_decision_points(fresh_db, monkeypatch):
    close = (datetime.now(timezone.utc) + timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def asset(name, mins_left):
        return {
            "asset": name, "series": f"KX{name}15M", "hasMarket": True,
            "ticker": f"KX{name}15M-T1", "closeTime": close,
            "minsLeft": mins_left, "favorite": "up", "favoritePrice": 0.7,
            "entryCost": 0.71, "upProb": 0.7, "deltaPct": 0.001,
            "spotUsd": 100.0, "open15mUsd": 99.9,
            "yesBid": 0.69, "yesAsk": 0.71,
        }

    # BTC mid-window (12m left), ETH inside the 8-min decision window.
    assets = [asset("BTC", 12.0), asset("ETH", 6.0)]

    async def _snap(_cfg):
        return {"assets": assets}
    monkeypatch.setattr(crypto15m, "snapshot", _snap)

    out = asyncio.run(crypto15m_record.record_tick({}))
    # A tick row for EVERY active market, a signals row only inside the
    # decision window.
    assert out["ticks"] == 2
    assert out["captured"] == 1
    with db.get_db() as conn:
        assert db.crypto15m_tick_count(conn) == 2
        tick = dict(conn.execute(
            "SELECT * FROM crypto15m_ticks WHERE asset='BTC'").fetchone())
        assert tick["yes_bid"] == pytest.approx(0.69)
        assert tick["yes_ask"] == pytest.approx(0.71)
        assert tick["spot"] == pytest.approx(100.0)
        sig_assets = [r["asset"] for r in conn.execute(
            "SELECT asset FROM crypto15m_signals").fetchall()]
        assert sig_assets == ["ETH"]

    # A second pass appends more ticks but dedups the decision point.
    out = asyncio.run(crypto15m_record.record_tick({}))
    assert out["ticks"] == 2
    assert out["captured"] == 0
    with db.get_db() as conn:
        assert db.crypto15m_tick_count(conn) == 4


def test_record_tick_captures_indicator_fields(fresh_db, monkeypatch):
    close = (datetime.now(timezone.utc) + timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    a = {
        "asset": "BTC", "series": "KXBTC15M", "hasMarket": True,
        "ticker": "KXBTC15M-IND", "closeTime": close, "minsLeft": 6.0,
        "favorite": "up", "favoritePrice": 0.7, "entryCost": 0.71, "upProb": 0.7,
        "deltaPct": 0.001, "spotUsd": 100.0, "open15mUsd": 99.9,
        "yesBid": 0.69, "yesAsk": 0.71,
        "macd": 1.1, "macdSignal": 0.8, "macdHist": 0.3, "macdCross": 1, "rsi": 61.0,
    }

    async def _snap(_cfg):
        return {"assets": [a]}
    monkeypatch.setattr(crypto15m, "snapshot", _snap)

    asyncio.run(crypto15m_record.record_tick({}))
    with db.get_db() as conn:
        tick = dict(conn.execute(
            "SELECT * FROM crypto15m_ticks WHERE asset='BTC'").fetchone())
        sig = dict(conn.execute(
            "SELECT * FROM crypto15m_signals WHERE asset='BTC'").fetchone())
    assert tick["macd_hist"] == pytest.approx(0.3) and tick["rsi"] == pytest.approx(61.0)
    assert sig["macd_cross"] == 1 and sig["macd_hist"] == pytest.approx(0.3)


# ───────── WS-book decision pricing ────────────────────────────────────


def test_ws_up_price_and_fav_ask():
    yes = {"bid_cents": 94, "ask_cents": 96}
    no = {"bid_cents": 4, "ask_cents": 6}
    # yes mid 0.95, (1 - no mid 0.05) = 0.95 -> averaged 0.95
    assert round(crypto15m._ws_up_price(yes, no), 4) == 0.95
    assert crypto15m._ws_fav_ask("up", yes, no) == 0.96
    assert crypto15m._ws_fav_ask("down", yes, no) == 0.06


def test_ws_up_price_rejects_unusable_book():
    assert crypto15m._ws_up_price(None, None) is None
    # crossed book (bid > ask) is rejected
    assert crypto15m._ws_mid({"bid_cents": 60, "ask_cents": 55}) is None
    # one-sided is rejected
    assert crypto15m._ws_mid({"bid_cents": 50, "ask_cents": None}) is None
    # a single sane side still yields a price
    assert round(crypto15m._ws_up_price({"bid_cents": 94, "ask_cents": 96}, None), 4) == 0.95


def _one_market_cfg(monkeypatch, *, ws_quotes, market=None):
    """Stub fetch_crypto_updown to one market + the WS feed quotes; gate off the
    extra detectors. Returns (cfg, now_epoch)."""
    now = 1_790_000_000.0

    async def _mkts(asset, interval, *, fast=False):
        return [{"ticker": "WS-T", "yes_token": "Y", "no_token": "N",
                 "yes_bid": 0.30, "yes_bid_dollars": 0.30,
                 "window_close_epoch": now + 300, "window_start_epoch": now - 300,
                 "close_time": "", **(market or {})}]
    monkeypatch.setattr(crypto15m.polymarket_api, "fetch_crypto_updown", _mkts)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "start", lambda: None)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "observe", lambda *a: None)
    monkeypatch.setattr(crypto15m.clob_ws.feed, "get_quote_cents",
                        lambda tok, max_age=12.0: ws_quotes.get(tok))
    from config import merge_with_defaults
    cfg = merge_with_defaults({
        "crypto15m_arb_detect": False, "crypto15m_imbalance_detect": False,
        "crypto15m_indicator_detect": False, "crypto15m_ws_book": True,
    })
    return cfg, now


def test_snapshot_decides_on_ws_book_when_fresh(monkeypatch):
    # Gamma says up=0.30 (down favored); the LIVE book says up≈0.95 (up favored).
    cfg, now = _one_market_cfg(monkeypatch, ws_quotes={
        "Y": {"bid_cents": 94, "ask_cents": 96},
        "N": {"bid_cents": 4, "ask_cents": 6},
    })
    out = asyncio.run(crypto15m._asset_snapshot(
        {"asset": "BTC", "series": "X"}, 100.0, cfg, now))
    assert out["priceSource"] == "ws"
    assert out["favorite"] == "up"                 # decided on the real book, not Gamma
    assert round(out["favoritePrice"], 2) == 0.95
    assert out["entryCost"] == 0.96                # real best ASK, not the mid
    assert out["wsAsk"] == 96 and out["wsBid"] == 94


def test_snapshot_falls_back_to_gamma_when_feed_cold(monkeypatch):
    cfg, now = _one_market_cfg(monkeypatch, ws_quotes={})   # feed returns None
    out = asyncio.run(crypto15m._asset_snapshot(
        {"asset": "ETH", "series": "X"}, 100.0, cfg, now))
    assert out["priceSource"] == "gamma"
    assert out["favorite"] == "down"               # Gamma up=0.30 -> down favored
    assert round(out["favoritePrice"], 2) == 0.70
    assert out["wsAsk"] is None


def test_gamma_fallback_prices_up_off_the_mid_not_the_bid(monkeypatch):
    """The market dict now carries Gamma's real bid/ask. The up probability is
    the mid; reading the bid would sit half a spread low and tilt every
    near-even window toward "down"."""
    cfg, now = _one_market_cfg(monkeypatch, ws_quotes={}, market={
        "yes_bid_dollars": 0.47, "yes_ask_dollars": 0.55,
        "last_price_dollars": 0.51,
    })
    out = asyncio.run(crypto15m._asset_snapshot(
        {"asset": "ETH", "series": "X"}, 100.0, cfg, now))
    assert out["priceSource"] == "gamma"
    assert out["upProb"] == 0.51 and out["favorite"] == "up"


# ───────── underlying indicator fetch/cache ────────────────────────────


def test_asset_indicators_computes_and_caches(monkeypatch):
    crypto15m._indicator_cache.clear()
    calls = {"n": 0}

    async def _fake(asset, client, limit):
        calls["n"] += 1
        return [100.0 + i for i in range(60)], [1.0] * 60
    monkeypatch.setattr(crypto15m, "_fetch_closes", _fake)

    async def run():
        first = await crypto15m.asset_indicators("BTC")
        second = await crypto15m.asset_indicators("BTC")  # within TTL -> cached
        return first, second
    first, second = asyncio.run(run())
    assert first["macdHist"] is not None and first["rsi"] is not None
    # trend fields compute too (60 closes = a full 1h VWAP window)
    assert first["vwap1h"] is not None and first["change5mPct"] is not None
    assert second == first
    assert calls["n"] == 1  # the second call was served from cache


def test_asset_indicators_degrades_to_all_none(monkeypatch):
    crypto15m._indicator_cache.clear()

    async def _boom(asset, client, limit):
        raise RuntimeError("histominute down")
    monkeypatch.setattr(crypto15m, "_fetch_closes", _boom)

    out = asyncio.run(crypto15m.asset_indicators("ETH"))
    # Every field degrades to None (macd/rsi/sigma AND the trend/VWAP fields).
    assert set(out) >= {"macd", "rsi", "sigma1m", "vwap1h", "change5mPct", "priceVsVwapPct"}
    assert all(v is None for v in out.values())


def test_cleanup_prunes_old_ticks(fresh_db):
    with db.get_db() as conn:
        db.insert_crypto15m_tick(conn, {"ticker": "T-OLD", "asset": "BTC"})
        db.insert_crypto15m_tick(conn, {"ticker": "T-NEW", "asset": "BTC"})
        old = (datetime.now(timezone.utc)
               - timedelta(days=db._C15_TICKS_KEEP_DAYS + 1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE crypto15m_ticks SET observed_at=? WHERE ticker='T-OLD'", (old,))
    db.cleanup_old_data()
    with db.get_db() as conn:
        rows = [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM crypto15m_ticks").fetchall()]
    assert rows == ["T-NEW"]


# ───────── per-interval datasets (5m / 15m / hourly) ───────────────────


def _seed_window(conn, ticker, asset, interval, up_won, *, ask=0.92):
    """One resolved window with a real-book tick, ready for replay."""
    db.insert_crypto15m_signal(conn, {
        "ticker": ticker, "asset": asset, "series": f"{asset}-UPDOWN",
        "close_time": "2026-07-09T00:15:00Z", "mins_left": 1.5,
        "favorite": "up", "favorite_price": ask, "entry_cost": ask,
        "up_prob": ask, "delta_pct": 0.002, "interval": interval,
    })
    db.resolve_crypto15m_signal(conn, ticker, up_won)
    db.insert_crypto15m_tick(conn, {
        "ticker": ticker, "asset": asset, "mins_left": 1.5,
        "yes_bid": ask - 0.02, "yes_ask": ask, "up_prob": ask,
        "spot": 100.0, "open_spot": 99.8, "delta_pct": 0.002,
        "ws_bid": ask - 0.02, "ws_ask": ask, "up_ask": ask,
        "no_ask": round(1.0 - ask + 0.02, 4), "spot_source": "rtds-ws",
        "interval": interval,
    })


def _replay_cfg(interval):
    import config
    cfg = config.merge_with_defaults({})
    cfg.update({
        "crypto15m_interval": interval,
        "crypto15m_direction_mode": "favorite", "crypto15m_use_rules": False,
        "crypto15m_time_delay_min": 2.0,
        "crypto15m_entry_threshold": 0.90, "crypto15m_entry_max": 0.99,
        "crypto15m_min_delta_pct": 0.0005,
    })
    return cfg


def test_replay_separates_interval_datasets(fresh_db):
    """A 5m backtest must never fill against 15m rows (and vice versa) —
    a user who switches the Crypto-tab interval accumulates both."""
    import replay
    with db.get_db() as conn:
        _seed_window(conn, "0xfive", "BTC", "5m", up_won=1)
        _seed_window(conn, "0xfifteen", "ETH", "15m", up_won=0)
    r5 = replay.replay(_replay_cfg("5m"), env="mainnet", since_days=60)
    r15 = replay.replay(_replay_cfg("15m"), env="mainnet", since_days=60)
    assert r5["interval"] == "5m" and r15["interval"] == "15m"
    assert (r5["windowsScanned"], r5["n"], r5["wins"]) == (1, 1, 1)
    assert (r15["windowsScanned"], r15["n"], r15["wins"]) == (1, 1, 0)
    assert list(r5["byAsset"]) == ["BTC"]
    assert list(r15["byAsset"]) == ["ETH"]


def test_replay_treats_legacy_null_interval_as_15m(fresh_db):
    """Rows recorded before the interval column shipped are all 15m."""
    import replay
    with db.get_db() as conn:
        _seed_window(conn, "0xlegacy", "BTC", "15m", up_won=1)
        conn.execute("UPDATE crypto15m_signals SET interval=NULL")
    assert replay.replay(_replay_cfg("15m"), env="mainnet",
                         since_days=60)["windowsScanned"] == 1
    assert replay.replay(_replay_cfg("5m"), env="mainnet",
                         since_days=60)["windowsScanned"] == 0


def _seed_tp_window(conn, ticker, asset, interval, up_won, *, entry_ask, exit_bid):
    """A resolved window with an ENTRY tick then a later tick whose bid pumped up,
    so a percent take-profit would sell before settlement."""
    db.insert_crypto15m_signal(conn, {
        "ticker": ticker, "asset": asset, "series": f"{asset}-UPDOWN",
        "close_time": "2026-07-09T00:15:00Z", "mins_left": 2.5,
        "favorite": "up", "favorite_price": entry_ask, "entry_cost": entry_ask,
        "up_prob": entry_ask, "delta_pct": 0.01, "interval": interval,
    })
    db.resolve_crypto15m_signal(conn, ticker, up_won)
    db.insert_crypto15m_tick(conn, {
        "ticker": ticker, "asset": asset, "mins_left": 2.5,
        "yes_bid": entry_ask - 0.02, "yes_ask": entry_ask, "up_prob": entry_ask,
        "spot": 100.0, "open_spot": 99.0, "delta_pct": 0.01,
        "ws_bid": entry_ask - 0.02, "ws_ask": entry_ask, "up_ask": entry_ask,
        "no_ask": round(1.0 - entry_ask + 0.02, 4), "spot_source": "rtds-ws",
        "interval": interval,
    })
    db.insert_crypto15m_tick(conn, {
        "ticker": ticker, "asset": asset, "mins_left": 1.0,
        "yes_bid": exit_bid, "yes_ask": min(0.99, exit_bid + 0.02), "up_prob": exit_bid,
        "spot": 101.0, "open_spot": 99.0, "delta_pct": 0.02,
        "ws_bid": exit_bid, "ws_ask": min(0.99, exit_bid + 0.02), "up_ask": min(0.99, exit_bid + 0.02),
        "no_ask": round(1.0 - exit_bid, 4), "spot_source": "rtds-ws",
        "interval": interval,
    })
    # Order the two ticks: entry first, pumped-bid tick second.
    conn.execute("UPDATE crypto15m_ticks SET observed_at=datetime('now','-3 minutes') "
                 "WHERE ticker=? AND mins_left=2.5", (ticker,))
    conn.execute("UPDATE crypto15m_ticks SET observed_at=datetime('now','-1 minutes') "
                 "WHERE ticker=? AND mins_left=1.0", (ticker,))


def _tp_cfg():
    import config
    cfg = config.merge_with_defaults({})
    cfg.update({
        "crypto15m_interval": "15m", "crypto15m_direction_mode": "favorite",
        "crypto15m_use_rules": False,        "crypto15m_time_delay_min": 3.0, "crypto15m_entry_threshold": 0.55,
        "crypto15m_entry_max": 0.99, "crypto15m_min_delta_pct": 0.0005,
    })
    return cfg


def test_replay_simulates_percent_take_profit(fresh_db):
    """A percent take-profit sells into the pumped bid instead of holding a
    favorite that LOST to settlement — turning a settlement loss into a realized
    win in replay (the whole point of the feature)."""
    import replay
    with db.get_db() as conn:
        _seed_tp_window(conn, "0xtp", "BTC", "15m", up_won=0, entry_ask=0.60, exit_bid=0.80)

    # TP OFF: favorite lost, held to settlement -> a loss.
    off = _tp_cfg(); off["crypto15m_take_profit_pct"] = 0.0
    r_off = replay.replay(off, env="mainnet", since_days=60)
    assert r_off["n"] == 1 and r_off["wins"] == 0
    assert r_off["totalPnlUsd"] < 0
    assert r_off["trades"][0]["exitReason"] == "settlement"

    # TP ON at +20%: bid pumped from 60c to 80c (+33%) crosses it -> sold green.
    on = _tp_cfg(); on["crypto15m_take_profit_pct"] = 0.20
    r_on = replay.replay(on, env="mainnet", since_days=60)
    assert r_on["n"] == 1 and r_on["wins"] == 1
    assert r_on["totalPnlUsd"] > 0
    assert r_on["trades"][0]["exitReason"] == "take_profit"


def test_recorder_stamps_interval_and_scales_lookback(fresh_db):
    """The recorder follows crypto15m_interval: rows are stamped with it and
    the decision-point lookback sits at the same relative spot per window
    (2.5 min for 5m — an 8-min lookback would capture 5m windows at birth)."""
    snap = {"assets": [{
        "hasMarket": True, "ticker": "0xa", "asset": "BTC",
        "series": "BTC-UPDOWN", "closeTime": "2026-07-09T00:05:00Z",
        "minsLeft": 4.0, "favorite": "up", "favoritePrice": 0.9,
        "entryCost": 0.9, "upProb": 0.9,
    }], "spotSource": "rtds-ws"}
    # 4 min left: outside the 5m decision window (2.5) but a tick still logs.
    assert crypto15m_record._capture(snap, "mainnet", "5m") == 0
    assert crypto15m_record._capture_ticks(snap, "mainnet", "5m") == 1
    snap["assets"][0]["minsLeft"] = 2.0  # inside
    assert crypto15m_record._capture(snap, "mainnet", "5m") == 1
    with db.get_db() as conn:
        sig = conn.execute("SELECT interval FROM crypto15m_signals").fetchone()
        tick = conn.execute("SELECT interval FROM crypto15m_ticks").fetchone()
    assert sig["interval"] == "5m"
    assert tick["interval"] == "5m"


# ───────── live-spot velocity (2026-07-17: candle velocity was 60-120s stale) ──


def test_live_velocity_needs_history(monkeypatch):
    crypto15m._spot_hist.clear()
    assert crypto15m.live_velocity_pct("BTC", 100.0) is None      # no history
    t = [1000.0]
    monkeypatch.setattr(crypto15m.time, "time", lambda: t[0])
    crypto15m._note_spot("BTC", 100.0)
    t[0] += 30                                                     # only 30s old
    assert crypto15m.live_velocity_pct("BTC", 100.2) is None


def test_live_velocity_computes_over_60s(monkeypatch):
    crypto15m._spot_hist.clear()
    t = [1000.0]
    monkeypatch.setattr(crypto15m.time, "time", lambda: t[0])
    crypto15m._note_spot("BTC", 100.0)      # t=1000
    t[0] = 1030.0
    crypto15m._note_spot("BTC", 100.05)     # t=1030
    t[0] = 1065.0
    # newest sample >=60s old is t=1000 (100.0): +0.15% over the horizon
    v = crypto15m.live_velocity_pct("BTC", 100.15)
    assert v == pytest.approx(0.15, abs=1e-6)


def test_live_velocity_rejects_stale_reference(monkeypatch):
    crypto15m._spot_hist.clear()
    t = [1000.0]
    monkeypatch.setattr(crypto15m.time, "time", lambda: t[0])
    crypto15m._note_spot("BTC", 100.0)
    t[0] = 1000.0 + 60 + 36    # reference now older than horizon+tol (95s)
    assert crypto15m.live_velocity_pct("BTC", 101.0) is None


def test_live_velocity_history_is_pruned(monkeypatch):
    crypto15m._spot_hist.clear()
    t = [1000.0]
    monkeypatch.setattr(crypto15m.time, "time", lambda: t[0])
    for i in range(200):
        t[0] = 1000.0 + i * 10
        crypto15m._note_spot("BTC", 100.0 + i)
    dq = crypto15m._spot_hist["BTC"]
    assert all(t[0] - ts <= crypto15m._SPOT_HIST_KEEP_S for ts, _p in dq)
