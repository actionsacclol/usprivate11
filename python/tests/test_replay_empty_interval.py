"""An empty replay must read as missing data, never as a verdict.

Audit 2026-10-11: the recorder only records the interval selected on the
Crypto tab. The owner's has been on 5m since July, so four of the five crypto
presets on the Backtest page (Settlement Sniper, Paired + Tilt, Deep Favorite,
Contrarian fade: all 15m) and all 33 Turbine strategies (15m) replayed zero
windows. The result then led with "Only 0 trades — far too few for a verdict",
and the real reason ("No resolved 15m windows…") sat second, where the
Backtest page never shows it.

These tests pin that the reason comes FIRST, names the interval the strategy
needs and the one being recorded, and says how to record the right one.
"""
from __future__ import annotations

import asyncio
import io
import sys

import pytest

import config
import db
import replay

VERDICT = "far too few for a verdict"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "empty-interval.db")
    db.init_db()


def _seed(ticker, asset, interval, up_won=1, ask=0.95):
    """One resolved window with one real-book tick (observed now)."""
    with db.get_db() as conn:
        db.insert_crypto15m_signal(conn, {
            "ticker": ticker, "asset": asset, "series": f"{asset}-UPDOWN",
            "close_time": "2026-10-09T00:15:00Z", "mins_left": 1.5,
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


def _seed_5m_corpus(n=6):
    for i in range(n):
        _seed(f"0x5m{i}", "BTC", "5m")


def _cfg(interval, **over):
    cfg = config.merge_with_defaults({})
    cfg.update({
        "crypto15m_interval": interval,
        "crypto15m_direction_mode": "favorite", "crypto15m_use_rules": False,
        "crypto15m_time_delay_min": 2.0,
        "crypto15m_entry_threshold": 0.90, "crypto15m_entry_max": 0.99,
        "crypto15m_min_delta_pct": 0.0,
    })
    cfg.update(over)
    return cfg


# ── replay.replay ───────────────────────────────────────────────────────────


def test_a_15m_strategy_on_a_5m_corpus_leads_with_the_reason(fresh_db):
    _seed_5m_corpus()
    res = replay.replay(_cfg("15m"), env="mainnet", since_days=30,
                        recording_interval="5m", recording_on=True)
    assert res["windowsScanned"] == 0 and res["n"] == 0
    first = res["caveats"][0]
    assert first.startswith("No resolved 15m windows recorded"), first
    assert "not a result" in first
    # Which interval is being recorded, what IS there, and how to fix it.
    assert "currently 5m" in first
    assert "5m (6 windows)" in first
    assert "select “15 min” on the Crypto tab" in first
    # Switching the interval also moves the live engine — the hint must say so.
    assert "crypto engine trades" in first
    # 0 trades is not presented as a (tiny-sample) verdict anywhere.
    assert not any(VERDICT in c for c in res["caveats"]), res["caveats"]


def test_the_result_carries_a_structured_no_data_reason(fresh_db):
    _seed_5m_corpus(3)
    res = replay.replay(_cfg("15m"), env="mainnet", since_days=30,
                        recording_interval="5m", recording_on=True)
    nd = res["noData"]
    assert nd["reason"] == "interval"
    assert nd["interval"] == "15m" and nd["recordingInterval"] == "5m"
    assert nd["recorded"] == {"5m": 3}
    assert nd["message"] == res["caveats"][0]


def test_without_a_known_recorder_it_still_names_what_is_recorded(fresh_db):
    """CLI callers (turbine_backtest) don't know the live config: fall back
    to what the database holds."""
    _seed_5m_corpus(2)
    res = replay.replay(_cfg("15m"), env="mainnet", since_days=30)
    first = res["caveats"][0]
    assert first.startswith("No resolved 15m windows recorded"), first
    assert "5m (2 windows)" in first and "select “15 min” on the Crypto tab" in first
    assert res["noData"]["reason"] == "interval"


def test_recording_the_right_interval_but_nothing_yet(fresh_db):
    res = replay.replay(_cfg("15m"), env="mainnet", since_days=30,
                        recording_interval="15m", recording_on=True)
    first = res["caveats"][0]
    assert res["noData"]["reason"] == "none-yet"
    assert "collecting 15m windows now" in first, first
    assert not any(VERDICT in c for c in res["caveats"])


def test_collection_off_is_named_first(fresh_db):
    _seed_5m_corpus(2)
    res = replay.replay(_cfg("15m"), env="mainnet", since_days=30,
                        recording_interval="5m", recording_on=False)
    first = res["caveats"][0]
    assert res["noData"]["reason"] == "collection-off"
    assert "collection is OFF" in first and "select “15 min” on the Crypto tab" in first


def test_a_coin_filter_that_empties_the_run_is_not_blamed_on_the_interval(fresh_db):
    _seed("0xeth15", "ETH", "15m")
    res = replay.replay(_cfg("15m", crypto15m_assets=["BTC"]), env="mainnet",
                        since_days=30, recording_interval="15m", recording_on=True)
    assert res["noData"]["reason"] == "assets"
    first = res["caveats"][0]
    assert "none for the coins this strategy trades (BTC)" in first, first
    assert "select" not in first


def test_a_real_small_sample_still_gets_the_low_n_caveat(fresh_db):
    """Windows WERE scanned: 0-29 trades is a (thin) result — keep the
    anecdote warning, and no noData block."""
    _seed("0xbtc15", "BTC", "15m")
    res = replay.replay(_cfg("15m"), env="mainnet", since_days=30,
                        recording_interval="15m", recording_on=True)
    assert res["windowsScanned"] == 1
    assert VERDICT in res["caveats"][0]
    assert "noData" not in res


def test_main_engine_replay_with_no_signals_leads_with_the_reason(fresh_db):
    res = replay.replay_main(config.merge_with_defaults({}), since_days=14)
    first = res["caveats"][0]
    assert first.startswith("No resolved whale or momentum signals"), first
    assert not any(VERDICT in c for c in res["caveats"])


# ── the app's own path: the Backtest page's preset RPC ───────────────────────

# The replay-relevant part of the Backtest page's 15m presets
# (shared/c15Presets.ts + src/pages/Backtest.tsx), camelCase as the renderer
# sends them.
BACKTEST_PAGE_15M_PRESETS = {
    "sniper": {"crypto15mInterval": "15m", "crypto15mDirectionMode": "model",
               "crypto15mUseRules": False, "crypto15mModelMinEdgeCents": 2,
               "crypto15mModelFinalMinute": True, "crypto15mModelMinProb": 0.97,
               "crypto15mTimeDelayMin": 5, "crypto15mEntryMax": 0.97},
    "paired": {"crypto15mInterval": "15m", "crypto15mDirectionMode": "model",
               "crypto15mUseRules": False, "crypto15mPairedMode": True,
               "crypto15mTimeDelayMin": 15, "crypto15mEntryMax": 0.97},
    "favorite": {"crypto15mInterval": "15m", "crypto15mDirectionMode": "favorite",
                 "crypto15mUseRules": False, "crypto15mEntryThreshold": 0.95,
                 "crypto15mEntryMax": 0.98, "crypto15mTimeDelayMin": 6},
    "contrarian": {"crypto15mInterval": "15m", "crypto15mDirectionMode": "contrarian",
                   "crypto15mUseRules": False, "crypto15mEntryThreshold": 0.90,
                   "crypto15mTimeDelayMin": 8},
}


@pytest.fixture
def svc_on_5m(fresh_db, monkeypatch):
    import polymarket_auth
    import service
    monkeypatch.setattr(polymarket_auth, "get_env", lambda: "mainnet")
    monkeypatch.setattr(service.STATE, "cfg", config.merge_with_defaults(
        {"crypto15m_interval": "5m", "crypto15m_record_signals": True}))
    return service


@pytest.mark.parametrize("preset", sorted(BACKTEST_PAGE_15M_PRESETS))
def test_backtest_page_15m_presets_on_a_5m_machine_say_why(svc_on_5m, preset):
    _seed_5m_corpus(4)
    res = asyncio.run(svc_on_5m._h_c15_backtest(
        {"config": BACKTEST_PAGE_15M_PRESETS[preset], "sinceDays": 30}))
    assert res["interval"] == "15m" and res["windowsScanned"] == 0
    first = res["caveats"][0]
    assert first.startswith("No resolved 15m windows recorded"), first
    assert "currently 5m" in first and "select “15 min” on the Crypto tab" in first
    assert res["noData"]["recordingInterval"] == "5m"


def test_collection_stats_split_windows_by_interval(svc_on_5m):
    _seed_5m_corpus(3)
    _seed("0xbtc15", "BTC", "15m")
    st = asyncio.run(svc_on_5m._h_collection_stats({}))
    assert st["c15"]["recordingInterval"] == "5m"
    assert st["c15"]["byInterval"] == {
        "5m": {"windows": 3, "resolved": 3},
        "15m": {"windows": 1, "resolved": 1},
    }


# ── the Scripts tab's backtest shares the same summary ───────────────────────


def test_script_backtest_on_the_wrong_interval_leads_with_the_reason(fresh_db):
    import script_backtest
    _seed_5m_corpus(2)
    code = "def decide(ctx):\n    return None\n"
    res = script_backtest.run(
        {**_cfg("15m"), "script_max_entry_cents": 97, "script_max_contracts": 20},
        code, env="mainnet", since_days=30)
    first = res["caveats"][0]
    assert first.startswith("No resolved 15m windows recorded"), first
    assert "5m (2 windows)" in first
    assert not any(VERDICT in c for c in res["caveats"])


# ── Turbine library (CLI) ────────────────────────────────────────────────────


def test_turbine_strategies_pin_the_15m_interval():
    import turbine_import
    imported, _ = turbine_import.import_all()
    assert imported and all(
        it["config"]["crypto15m_interval"] == "15m" for it in imported)


def test_turbine_backtest_reports_no_data_instead_of_ranking_zeros(fresh_db, monkeypatch):
    import turbine_backtest
    _seed_5m_corpus(3)
    ranked = turbine_backtest.run("mainnet", 30)
    assert ranked and all(r["windowsScanned"] == 0 for r in ranked)
    assert all(r["interval"] == "15m" for r in ranked)
    assert all("No resolved 15m windows" in r["noData"] for r in ranked)
    head = turbine_backtest.no_data_headline(ranked)
    assert head.startswith(f"NO DATA: ALL {len(ranked)} strategies replayed 0 windows")

    out = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["turbine_backtest", "--days", "30"])
    monkeypatch.setattr(sys, "stdout", out)
    turbine_backtest.main()
    text = out.getvalue()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].startswith("NO DATA: ALL"), lines[0]
    # No row prints a 0.00¢ "edge" and the footer gives no verdict.
    assert "    0.00 " not in text
    assert "no verdict" in lines[-1], lines[-1]
