"""The history download: public past windows into the tables backtests read.

The test that matters most is the timing one. The venue stamps a one-minute quote
with the start of its minute but gives the price at the end of it. Read at face
value, every backtest would trade on prices a minute before they existed.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

import config
import crypto15m_trader
import db
import history_download as hd
import polymarket_api as api
import replay

START = 1793858400          # 2026-11-05 06:00:00 UTC, on a quarter hour
END = START + 900
STRIKE = 80000.0


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "history.db")
    db.init_db()


@pytest.fixture(autouse=True)
def idle_job(monkeypatch):
    monkeypatch.setattr(hd, "_status", hd._idle_status())
    monkeypatch.setattr(hd, "_task", None)


def _points(ask=0.6, no_ask=0.42):
    return [{"timestamp": START + m * 60, "longPrice": ask, "shortPrice": no_ask} for m in range(16)]


def _minutes(price=STRIKE + 10):
    return {START + m * 60: (price + m, price + m + 0.5) for m in range(-90, 16)}


def _build(**over):
    kw = dict(asset="BTC", interval="15m", ticker="cpc-btc-updown-15m-x", series="BTC-UPDOWN",
              start=START, end=END, strike=STRIKE, up_won=True, points=_points(), minutes=_minutes(),
              fee_schedule=None, env="mainnet")
    kw.update(over)
    return hd.build_window(**kw)


def _at(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── timing ───────────────────────────────────────────────────────────────


def test_a_quote_is_timed_when_it_was_known_not_when_it_was_stamped():
    _, ticks = _build()
    assert ticks[0]["observed_at"] == _at(START + 60)   # stamped START, holds the end of that minute
    assert ticks[0]["mins_left"] == 14.0
    assert ticks[-1]["observed_at"] == _at(END - 60)
    assert all(_at(START) < t["observed_at"] < _at(END) for t in ticks)


def test_nothing_a_tick_is_priced_from_comes_from_after_it(monkeypatch):
    """Indicators and spot may use only what existed at the tick's time. A price spike in the
    minute after a tick must not change that tick."""
    calm = _minutes()
    spike = dict(calm)
    tick_minute = START + 300
    spike[tick_minute] = (calm[tick_minute][0], calm[tick_minute][1] * 2)   # that minute's close
    spike[tick_minute + 60] = (STRIKE * 3, STRIKE * 3)                      # and everything after
    for m in range(tick_minute + 120, END + 60, 60):
        spike[m] = (STRIKE * 3, STRIKE * 3)
    _, a = _build(minutes=calm)
    _, b = _build(minutes=spike)
    at = _at(tick_minute)
    ta = next(t for t in a if t["observed_at"] == at)
    tb = next(t for t in b if t["observed_at"] == at)
    for key in ("spot", "rsi", "macd", "sigma1m", "model_prob", "delta_signed_pct", "ema12", "sma20"):
        assert ta[key] == tb[key], key


def test_the_decision_point_is_where_the_recorder_would_capture_it():
    signal, _ = _build()
    assert signal["mins_left"] == 8.0           # the 15m lookback is 8 minutes
    assert signal["observed_at"] == _at(END - 480)


# ── quotes ───────────────────────────────────────────────────────────────


def test_the_quote_maps_to_the_book_fields():
    signal, ticks = _build(points=_points(ask=0.6, no_ask=0.42))
    t = ticks[0]
    assert (t["yes_ask"], t["up_ask"], t["no_ask"]) == (0.6, 0.6, 0.42)
    assert t["yes_bid"] == pytest.approx(0.58)
    assert t["up_prob"] == pytest.approx(0.59)
    assert signal["favorite"] == "up" and signal["entry_cost"] == 0.6
    assert signal["resolved"] == 1 and signal["up_won"] == 1


def test_crossed_or_empty_quotes_are_dropped_and_a_window_of_them_is_not_saved():
    assert _build(points=_points(ask=0.98, no_ask=0.01)) is None   # bid .99 over ask .98
    assert _build(points=_points(ask=1.0, no_ask=0.4)) is None     # no ask on YES
    assert _build(points=_points(ask=0.5, no_ask=0.0)) is None     # no bid on YES
    assert _build(points=[]) is None


def test_a_missing_spot_leaves_the_fields_unknown_rather_than_inventing_them():
    _, ticks = _build(minutes={})
    assert all(t["spot"] is None and t["delta_pct"] is None and t["model_prob"] is None for t in ticks)
    assert ticks[0]["yes_ask"] == 0.6


# ── what gets fetched ────────────────────────────────────────────────────


def test_the_plan_is_closed_windows_only_newest_first_skipping_held_ones():
    now = START + 901           # the 06:15 window has only just opened
    plan = hd.plan_windows(1, ["15m"], now, set(), assets=["BTC"])
    assert plan[0]["start"] == START
    assert all(w["start"] + w["sec"] <= now for w in plan)
    assert [w["start"] for w in plan] == sorted((w["start"] for w in plan), reverse=True)
    assert len(plan) == 95            # the 24 hours back from `now`
    held = {plan[0]["slug"]}
    assert plan[0]["slug"] not in {w["slug"] for w in hd.plan_windows(1, ["15m"], now, held, assets=["BTC"])}


def test_a_request_is_capped_to_what_the_venue_keeps():
    now = START + 901
    assert len(hd.plan_windows(99, ["hourly"], now, set(), assets=["BTC"])) <= 40 * 24 + 1
    assert len(hd.plan_windows(99, ["5m"], now, set(), assets=["BTC"])) <= 13 * 288 + 1


def test_a_bad_request_falls_back_to_the_default():
    assert hd.clean_request("x", "y") == (hd.DEFAULT_DAYS, list(hd.DEFAULT_INTERVALS))
    assert hd.clean_request(-5, ["nope"]) == (1, list(hd.DEFAULT_INTERVALS))
    assert hd.clean_request(9999, ["5m", "15m"]) == (40, ["5m", "15m"])


def test_held_windows_are_found_by_event_slug(fresh_db):
    signal, ticks = _build(ticker="cpceth-eth-updown-15m-2026-11-05-0600z", asset="ETH")
    with db.get_db() as conn:
        db.insert_history_window(conn, signal, ticks)
    assert hd.held_event_slugs("mainnet") == {"eth-updown-15m-2026-11-05-0600z"}


# ── storing ──────────────────────────────────────────────────────────────


def test_a_recorded_window_is_never_overwritten(fresh_db):
    with db.get_db() as conn:
        db.insert_crypto15m_signal(conn, {
            "ticker": "cpc-btc-updown-15m-x", "asset": "BTC", "series": "BTC-UPDOWN",
            "close_time": "2026-11-05T06:15:00Z", "mins_left": 8, "favorite": "up",
            "favorite_price": 0.7, "entry_cost": 0.7, "up_prob": 0.7, "interval": "15m",
        })
    signal, ticks = _build()
    with db.get_db() as conn:
        assert db.insert_history_window(conn, signal, ticks) is False
        row = conn.execute("SELECT favorite_price, strike_source FROM crypto15m_signals").fetchone()
        n = conn.execute("SELECT COUNT(*) FROM crypto15m_ticks").fetchone()[0]
    assert row["favorite_price"] == 0.7 and row["strike_source"] is None and n == 0


def test_a_window_with_recorded_ticks_is_left_alone(fresh_db):
    with db.get_db() as conn:
        db.insert_crypto15m_tick(conn, {"ticker": "cpc-btc-updown-15m-x", "asset": "BTC", "mins_left": 3.0})
    signal, ticks = _build()
    with db.get_db() as conn:
        assert db.insert_history_window(conn, signal, ticks) is False
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_signals").fetchone()[0] == 0


def _seed_recent(n=1, ask=0.95, up_won=True):
    """Downloaded windows inside replay's date range, with a fill-worthy ask."""
    now = int(datetime.now(timezone.utc).timestamp())
    base = (now // 900) * 900 - 900
    for i in range(n):
        s = base - i * 900
        signal, ticks = _build(
            ticker=f"cpc-btc-updown-15m-{i}", start=s, end=s + 900, up_won=up_won,
            points=[{"timestamp": s + m * 60, "longPrice": ask, "shortPrice": round(1 - ask + 0.01, 4)}
                    for m in range(15)],
            minutes={s + m * 60: (STRIKE * 1.01 + m % 3, STRIKE * 1.01 + m % 3) for m in range(-90, 16)},
        )
        with db.get_db() as conn:
            db.insert_history_window(conn, signal, ticks)


def test_replay_reads_a_downloaded_window_and_says_so(fresh_db):
    _seed_recent(3)
    cfg = config.merge_with_defaults({"crypto15m_interval": "15m"})
    out = replay.replay(cfg, env="mainnet", since_days=2)
    assert out["windowsScanned"] == 3
    assert any("3 of 3 windows are downloaded history" in c for c in out["caveats"])


def test_recorded_windows_get_no_download_caveat():
    assert replay.history_caveat({"a": [{"spot_source": "rtds-ws"}]}) is None
    assert "1 of 2" in replay.history_caveat({
        "a": [{"spot_source": "rtds-ws"}], "b": [{"spot_source": db.HISTORY_SOURCE}]})


def test_downloaded_spot_counts_as_live_spot_so_sniper_gates_can_fire():
    cfg = config.merge_with_defaults({})
    row = {"ticker": "x", "asset": "BTC", "mins_left": 2.0, "up_prob": 0.9, "yes_ask": 0.9, "up_ask": 0.9,
           "no_ask": 0.11, "yes_bid": 0.89, "spot_source": db.HISTORY_SOURCE, "observed_at": "2026-11-05 06:13:00"}
    assert replay.tick_to_asset(row, cfg, "2026-11-05T06:15:00Z")["spotLive"] is True


def test_downloaded_windows_cannot_pause_or_resume_a_live_engine(fresh_db, monkeypatch):
    """The drift monitor judges what the model saw live, and old quotes aren't that."""
    _seed_recent(40, ask=0.98, up_won=False)    # every sure thing lost: reads as a broken model
    monkeypatch.setattr(crypto15m_trader, "_CAL_CACHE", {"at": 0.0, "ok": True})
    out = crypto15m_trader.check_model_calibration("mainnet", "15m")
    assert out["ok"] is True and out["n"] == 0

    # The same rows as recorded ones would pause it, so the filter is what stopped that.
    with db.get_db() as conn:
        conn.execute("UPDATE crypto15m_ticks SET spot_source='rtds-ws'")
    monkeypatch.setattr(crypto15m_trader, "_CAL_CACHE", {"at": 0.0, "ok": True})
    out = crypto15m_trader.check_model_calibration("mainnet", "15m")
    assert out["ok"] is False and out["n"] == 40


# ── the job ──────────────────────────────────────────────────────────────


class _FakeVenue:
    """Answers like the gateway for the windows it knows."""

    def __init__(self, settled=True):
        self.calls = []
        self.settled = settled

    async def get(self, url, params=None, **_kw):
        self.calls.append(url)
        if "/v1/events/slug/" in url:
            slug = url.rsplit("/", 1)[-1]
            parts = slug.split("-")
            asset = parts[0]
            prefix = "cpc" if asset == "btc" else f"cpc{asset}"
            market = {
                "slug": f"{prefix}-{slug}", "active": False, "closed": self.settled,
                "assetPriceTerms": {"priceToBeat": {"value": str(STRIKE)}, "settlementPrice": {"value": str(STRIKE + 5)}},
            }
            return {"event": {"slug": slug, "markets": [market]}}
        if "/v1/price-history" in url:
            s = params["timestamp.startTimestamp"]
            return {"history": [{"timestamp": s + m * 60, "longPrice": 0.6, "shortPrice": 0.42} for m in range(6)]}
        if "/v1/asset-prices/history" in url:
            return {"series": [{"asset": {"symbol": "btc"}, "candles": [
                {"timestamp": params["from"], "open": "100", "close": "101", "tickCount": 60},
            ]}]}
        return None


def _patch_normalize(monkeypatch, settled=True):
    def norm(raw, event_slug=None):
        return {"slug": raw["slug"], "status": "settled" if settled else "open",
                "result": "yes" if settled else None, "price_to_beat": STRIKE,
                "settlement_value_dollars": 1.0}
    monkeypatch.setattr(api, "_normalize_market", norm)


def _run_job(monkeypatch, venue, windows):
    monkeypatch.setattr(api, "_get", venue.get)
    monkeypatch.setattr(hd, "RATE_PER_SEC", 100000.0)
    monkeypatch.setattr(hd, "plan_windows", lambda *_a, **_k: list(windows))

    async def go():
        hd.start(1, ["15m"])
        await hd._task
    asyncio.run(go())


def _window(i, asset="BTC"):
    s = START - i * 900
    return {"asset": asset, "interval": "15m", "slug": hd.us.updown_event_slug(asset, "15m", s), "start": s, "sec": 900}


def test_a_job_saves_windows_then_forgets_itself(fresh_db, monkeypatch):
    _patch_normalize(monkeypatch)
    _run_job(monkeypatch, _FakeVenue(), [_window(i) for i in range(5)])
    st = hd.status()
    assert (st["status"], st["saved"], st["done"], st["total"]) == ("done", 5, 5, 5)
    assert hd._load_job() is None
    with db.get_db() as conn:
        rows = conn.execute("SELECT strike_source, resolved, up_won FROM crypto15m_signals").fetchall()
    assert len(rows) == 5 and all(r["strike_source"] == db.HISTORY_SOURCE and r["resolved"] == 1 for r in rows)


def test_a_window_is_not_saved_without_its_spot_prices(fresh_db, monkeypatch):
    """It would stay incomplete: a held window is never fetched again."""
    _patch_normalize(monkeypatch)
    venue = _FakeVenue()
    real = venue.get

    async def no_candles(url, params=None, **kw):
        return None if "/v1/asset-prices/history" in url else await real(url, params=params, **kw)
    venue.get = no_candles
    monkeypatch.setattr(api, "api_outages", dict)
    _run_job(monkeypatch, venue, [_window(0)])
    assert hd.status()["saved"] == 0
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_signals").fetchone()[0] == 0


def test_an_unsettled_window_is_skipped_not_stored(fresh_db, monkeypatch):
    _patch_normalize(monkeypatch, settled=False)
    _run_job(monkeypatch, _FakeVenue(settled=False), [_window(0)])
    st = hd.status()
    assert (st["saved"], st["unavailable"], st["status"]) == (0, 1, "done")
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_signals").fetchone()[0] == 0


def test_running_out_of_history_ends_the_job_quietly(fresh_db, monkeypatch):
    async def nothing(*_a, **_k):
        return None
    monkeypatch.setattr(api, "api_outages", dict)
    venue = _FakeVenue()
    venue.get = nothing
    _run_job(monkeypatch, venue, [_window(i) for i in range(hd._END_STREAK + 50)])
    st = hd.status()
    assert st["status"] == "done"
    assert st["message"] == "Polymarket US has nothing older."
    assert hd._load_job() is None


def test_a_gap_in_the_venues_history_does_not_end_the_job(fresh_db, monkeypatch):
    """A stretch of missing windows with older ones behind it, as on 2026-11-05. Stopping
    there told the user nothing older existed and left a 7-day download with one day."""
    _patch_normalize(monkeypatch)
    monkeypatch.setattr(api, "api_outages", dict)
    # Twice the streak: a save still finishing can reset the count early in the gap.
    gap = range(5, 5 + hd._MISS_STREAK * 2)
    missing = {_window(i)["slug"] for i in gap}
    venue = _FakeVenue()
    real = venue.get

    async def gappy(url, params=None, **kw):
        if url.rsplit("/", 1)[-1] in missing:
            return None
        return await real(url, params=params, **kw)
    venue.get = gappy
    windows = [_window(i) for i in range(gap.stop + 5)]
    _run_job(monkeypatch, venue, windows)
    st = hd.status()
    assert (st["status"], st["message"]) == ("done", None)
    assert st["saved"] == len(windows) - len(missing)
    assert st["unavailable"] == len(missing)


def test_an_outage_stops_and_keeps_the_job_for_the_next_start(fresh_db, monkeypatch):
    async def nothing(*_a, **_k):
        return None
    monkeypatch.setattr(api, "api_outages", lambda: {"gateway": {"kind": "net", "sinceSec": 60}})
    monkeypatch.setattr(hd, "_OUTAGE_WAIT_SEC", 0.0)
    monkeypatch.setattr(hd, "_OUTAGE_MAX_WAITS", 1)
    venue = _FakeVenue()
    venue.get = nothing
    _run_job(monkeypatch, venue, [_window(i) for i in range(hd._MISS_STREAK * 4)])
    st = hd.status()
    assert st["status"] == "error" and "Couldn't reach" in st["message"]
    assert hd._load_job() == {"days": 1, "intervals": ["15m"]}


def test_an_interrupted_job_resumes_but_not_in_offline_mode(fresh_db, monkeypatch):
    hd._save_job({"days": 7, "intervals": ["hourly"]})
    assert hd.resume_if_pending({"terminal_offline_mode": True}) is False
    assert hd._task is None
    started = []
    monkeypatch.setattr(hd, "start", lambda d, i, delay=0.0: started.append((d, i, delay)))
    assert hd.resume_if_pending({}) is True
    assert started == [(7, ["hourly"], hd._RESUME_DELAY_SEC)]


def test_nothing_pending_means_nothing_resumes(fresh_db, monkeypatch):
    monkeypatch.setattr(hd, "start", lambda *_a, **_k: pytest.fail("started"))
    assert hd.resume_if_pending({}) is False


def test_cancel_forgets_the_job_and_keeps_what_was_saved(fresh_db, monkeypatch):
    _seed_recent(1)
    gate = {}

    async def go():
        gate["ev"] = asyncio.Event()

        async def hang(*_a, **_k):
            await gate["ev"].wait()
        monkeypatch.setattr(api, "_get", hang)
        monkeypatch.setattr(hd, "plan_windows", lambda *_a, **_k: [_window(9)])
        hd.start(1, ["15m"])
        await asyncio.sleep(0.05)
        return await hd.cancel()
    st = asyncio.run(go())
    assert st["status"] == "cancelled"
    assert hd._load_job() is None
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM crypto15m_signals").fetchone()[0] == 1
