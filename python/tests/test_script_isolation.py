"""An AI agent's script backtest runs in a child process (2026-10-11 audit):
no credentials, no userData, no secret environment, no network — and the
same result as running it in the backend."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import db
import script_backtest
import service


def _seed(path):
    db.init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with db.get_db() as conn:
        for c in range(4):
            close = now - timedelta(hours=2) + timedelta(minutes=15 * c)
            tk = f"BTC-{c}"
            conn.execute(
                "INSERT INTO crypto15m_signals (ticker, asset, resolved, up_won, network, close_time, interval) "
                "VALUES (?, 'BTC', 1, ?, 'mainnet', ?, '15m')",
                (tk, c % 2, close.strftime("%Y-%m-%dT%H:%M:%SZ")))
            for m in range(5, 0, -1):
                conn.execute(
                    "INSERT INTO crypto15m_ticks (ticker, asset, observed_at, mins_left, yes_bid, yes_ask, "
                    "up_prob, up_ask, no_ask, network) VALUES (?, 'BTC', ?, ?, 0.49, 0.5, 0.5, 0.5, 0.5, 'mainnet')",
                    (tk, (close - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"), float(m)))


CFG = config.merge_with_defaults({"crypto15m_interval": "15m"})

PROBE = '''
import os
def on_start(state):
    log("SECRET=" + repr(os.environ.get("FAKE_API_KEY")))
    log("USERDATA=" + repr(os.environ.get("KRYPT_POLYBOT_US_USERDATA")))

def decide(ctx):
    return {"side": "up", "price": "ask", "size": 1}
'''


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    f = tmp_path / "bt.db"
    monkeypatch.setattr(db, "db_path", lambda: f)
    _seed(f)
    return f


def test_the_child_has_no_secrets_and_no_userdata(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_API_KEY", "sekrit")
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path / "real-userdata"))
    res = asyncio.run(service._script_backtest_isolated(dict(CFG), PROBE, None, "mainnet", 2, db_file=str(seeded)))
    assert not res.get("scriptError"), res.get("scriptError")
    logs = "\n".join(res["scriptLogs"])
    assert "SECRET=None" in logs, logs
    assert "real-userdata" not in logs and "kp-backtest-" in logs, logs


def test_the_isolated_result_matches_the_in_process_one(seeded):
    inproc = script_backtest.run(dict(CFG), PROBE, env="mainnet", since_days=2)
    iso = asyncio.run(service._script_backtest_isolated(dict(CFG), PROBE, None, "mainnet", 2, db_file=str(seeded)))
    assert iso["n"] == inproc["n"] == 4
    assert iso["totalPnlUsd"] == pytest.approx(inproc["totalPnlUsd"])
    assert [t["ticker"] for t in iso["trades"]] == [t["ticker"] for t in inproc["trades"]]


def test_the_child_cannot_reach_the_network(seeded):
    code = "import urllib.request\n\ndef on_start(state):\n    urllib.request.urlopen('http://example.com', timeout=3)\n\ndef decide(ctx):\n    return None\n"
    res = asyncio.run(service._script_backtest_isolated(dict(CFG), code, None, "mainnet", 2, db_file=str(seeded)))
    assert "network is off" in str(res.get("scriptError")), res.get("scriptError")


def test_the_child_opens_the_db_read_only(seeded):
    code = ("import db\n\ndef on_start(state):\n    with db.get_db() as c:\n"
            "        c.execute(\"DELETE FROM crypto15m_ticks\")\n\ndef decide(ctx):\n    return None\n")
    res = asyncio.run(service._script_backtest_isolated(dict(CFG), code, None, "mainnet", 2, db_file=str(seeded)))
    assert "readonly" in str(res.get("scriptError")).lower().replace(" ", ""), res.get("scriptError")
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM crypto15m_ticks").fetchone()[0] == 20


def test_an_agent_backtest_goes_through_the_child(seeded, monkeypatch):
    calls = []
    real = service._script_backtest_isolated

    async def _spy(*a, **k):
        calls.append(a)
        return await real(*a, **k)
    monkeypatch.setattr(service, "_script_backtest_isolated", _spy)
    res = asyncio.run(service._h_script_backtest({"code": PROBE, "sinceDays": 2, "isolate": True}))
    assert calls and res["n"] == 4
    res = asyncio.run(service._h_script_backtest({"code": PROBE, "sinceDays": 2}))   # the user's own: in-process
    assert len(calls) == 1
