"""Model-mode backtests say when they mix model eras (2026-10-11 audit).

On 2026-08-07 the 15m series moved to Chainlink's 60s-TWAP settlement (the 5m
series on 2026-08-14). Ticks recorded before that carry a model_prob from the
old terminal-spot model, for windows that settled on a single print. A
model-mode replay over a long window used to pool both eras silently.

The recorded model_prob is not recomputed (a pre-switch window really did
settle on one print, and the 1-second prints the TWAP model needs in the
final minute were never recorded), so the replay counts and names the
pre-switch trades instead.
"""
from __future__ import annotations

import pytest

import crypto15m
import db
import replay
import script_backtest
from config import merge_with_defaults


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "twap-era.db")
    db.init_db()


def _seed(ticker: str, observed_at: str, close: str, up_won: int = 1) -> None:
    with db.get_db() as conn:
        conn.execute(
            """INSERT INTO crypto15m_signals
               (ticker, asset, resolved, up_won, network, close_time, interval)
               VALUES (?, 'BTC', 1, ?, 'mainnet', ?, '15m')""",
            (ticker, up_won, close),
        )
        conn.execute(
            """INSERT INTO crypto15m_ticks
               (ticker, asset, observed_at, mins_left, yes_bid, yes_ask, up_prob,
                no_ask, model_prob, edge_net_cents, spot_source, up_ask, network,
                interval)
               VALUES (?, 'BTC', ?, 4.0, 0.88, 0.90, 0.9, 0.12, 0.99, 5.0,
                       'rtds-ws', 0.90, 'mainnet', '15m')""",
            (ticker, observed_at),
        )


def _seed_both_eras():
    _seed("OLD", "2026-08-01 00:11:00", "2026-08-01T00:15:00Z")
    _seed("NEW", "2026-09-01 00:11:00", "2026-09-01T00:15:00Z")


def _model_cfg():
    return merge_with_defaults({
        "crypto15mEnabled": True, "crypto15mDirectionMode": "model",
        "crypto15mModelAutopause": False, "crypto15mModelMinProb": 0.97,
        "crypto15mModelMinEdgeCents": 2.0, "crypto15mTimeDelayMin": 8,
    })


def test_a_favorite_replay_has_no_model_caveat(fresh_db):
    _seed_both_eras()
    cfg = merge_with_defaults({
        "crypto15mEnabled": True, "crypto15mDirectionMode": "favorite",
        "crypto15mEntryThreshold": 0.85, "crypto15mEntryMax": 0.95,
        "crypto15mTimeDelayMin": 8, "crypto15mMinDeltaPct": 0.0,
    })
    res = replay.replay(cfg, env="mainnet", since_days=3650)
    assert res["n"] == 2
    assert res["preTwapModelTrades"] == 0
    assert not any("terminal-spot model" in c for c in res["caveats"])
