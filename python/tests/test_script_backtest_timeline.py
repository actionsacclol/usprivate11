"""The script backtester replays every window on ONE clock (2026-10-11 audit).

It used to replay windows one after another in close order, so when a
script decided on one coin's window, on_settle and the portfolio P&L had
already told it how the other coins closing at the same moment settled.
Crypto up/down windows closing together move together, so a script that
only copied a same-close peer's outcome showed a large phantom edge
(+6.1c/contract, t~9.7, against a -3.2c control). An outcome must reach the
script only once the replay clock passes that window's close."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import db
import script_backtest

CFG = {"crypto15m_interval": "15m", "script_max_entry_cents": 97,
       "script_max_contracts": 20, "crypto15m_order_size": 1}


def _seed(tmp_path, monkeypatch, n_closes=6):
    """`n_closes` 15-minute windows, each with a BTC and an ETH market that
    close together and settle the same way (alternating up/down). Ticks
    every minute for the last 10 minutes; the up ask sits at 50c."""
    dbfile = tmp_path / "bt.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with db.get_db() as conn:
        for c in range(n_closes):
            close = now - timedelta(hours=2) + timedelta(minutes=15 * c)
            up = 1 if c % 2 == 0 else 0
            for asset in ("BTC", "ETH"):
                tk = f"{asset}-{c}"
                conn.execute(
                    "INSERT INTO crypto15m_signals (ticker, asset, resolved, up_won, network, close_time, interval) "
                    "VALUES (?, ?, 1, ?, 'mainnet', ?, '15m')",
                    (tk, asset, up, close.strftime("%Y-%m-%dT%H:%M:%SZ")))
                for m in range(10, 0, -1):
                    at = close - timedelta(minutes=m)
                    conn.execute(
                        "INSERT INTO crypto15m_ticks (ticker, asset, observed_at, mins_left, yes_bid, yes_ask, "
                        "up_prob, up_ask, no_ask, network) VALUES (?, ?, ?, ?, 0.49, 0.5, 0.5, 0.5, 0.5, 'mainnet')",
                        (tk, asset, at.strftime("%Y-%m-%d %H:%M:%S"), float(m)))


PEEKER = '''
def on_settle(pos, state):
    state[pos["ticker"].split("-")[1]] = pos["won"] == (pos["side"] == "up")

def decide(ctx):
    # bet only when a same-close peer's outcome is already known
    up = state.get(ctx["ticker"].split("-")[1])
    if up is None:
        return None
    return {"side": "up" if up else "down", "price": "ask", "size": 1}

def on_start(state):
    pass
'''


def _run(code):
    return script_backtest.run(dict(CFG), code, env="mainnet", since_days=2)


def test_a_same_close_peer_outcome_is_not_known_before_the_close(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    # The peeker needs a seed trade to ever learn anything: take every BTC
    # window up, and ETH only on what it has been told.
    code = PEEKER.replace(
        '    up = state.get(ctx["ticker"].split("-")[1])',
        '    if ctx.get("asset") == "BTC":\n'
        '        return {"side": "up", "price": "ask", "size": 1}\n'
        '    up = state.get(ctx["ticker"].split("-")[1])')
    res = _run(code)
    if res.get("scriptError"):
        pytest.fail(res["scriptError"])
    eth = [t for t in res["trades"] if t["asset"] == "ETH"]
    # every ETH tick comes before its BTC peer settles: nothing to copy
    assert eth == [], eth


def test_outcomes_still_arrive_after_the_close(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    code = '''
def on_settle(pos, state):
    state["settled"] = state.get("settled", 0) + 1

def decide(ctx):
    if ctx.get("asset") != "BTC":
        return None
    return {"side": "up", "price": "ask", "size": 1, "reason": str(state.get("settled", 0))}
'''
    res = _run(code)
    assert not res.get("scriptError"), res.get("scriptError")
    btc = [t for t in res["trades"] if t["asset"] == "BTC"]
    assert len(btc) == 6
    assert sum(1 for t in btc if t["won"]) == 3     # alternating up/down
    assert res["totalPnlUsd"] == pytest.approx(sum(t["pnlUsd"] for t in btc), abs=0.01)


def test_portfolio_pnl_moves_only_at_settlement(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, n_closes=2)
    # Enter BTC on its first tick; on every later tick before the close,
    # record what the portfolio says. It must not have moved yet.
    code = '''
def decide(ctx):
    seen = state.setdefault("seen", [])
    seen.append((ctx["ticker"], ctx["portfolio"]["totalPnlUsd"], ctx["portfolio"]["openCount"]))
    if ctx["ticker"] == "BTC-0":
        return {"side": "up", "price": "ask", "size": 1}
    if ctx["ticker"] == "ETH-1" and ctx["minsLeft"] <= 1.5:
        log(repr([s for s in seen if s[0] == "ETH-0"]))
        log(repr([s for s in seen if s[0] == "ETH-1"][:1]))
    return None
'''
    res = _run(code)
    assert not res.get("scriptError"), res.get("scriptError")
    logs = "\n".join(res["scriptLogs"])
    # while BTC-0 was open, the ETH-0 ticks saw no P&L and one open position
    assert "('ETH-0', 0.0, 1)" in logs, logs
    # by the next window, BTC-0 has settled (a win at 50c: +0.5 less the fee)
    assert "('ETH-1', 0.0" not in logs, logs
