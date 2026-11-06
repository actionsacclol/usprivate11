"""When /value keeps failing, the portfolio is unknown, not frozen.

A failed /value read keeps the last-known portfolio, so one blip doesn't
show open positions as a loss. It used to keep it forever. Cash keeps moving
while that number doesn't (a fill debits cash and the frozen mark never adds
the position back), so the account total drifts into a loss that never
happened, and that total is what the daily stop reads. These pin the bound:
a blip is tolerated, a streak past five minutes makes the portfolio unknown,
an unknown portfolio is never persisted as a balance point, and the daily
stop never flattens the book on one.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import service
import trader
from config import merge_with_defaults


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(trader, "_balance_cache", {})
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")


def _reads(monkeypatch, *values):
    """get_balance answers with these portfolio values in turn (None = the
    /value leg failed); cash is always $100."""
    it = iter(values)

    async def _gb():
        return {"balance": 10_000, "portfolio_value": next(it)}

    monkeypatch.setattr(trader, "get_balance", _gb)


def _refresh():
    return asyncio.run(trader.refresh_balance({"balance_poll_interval": 60}, force=True))


def _age_last_good_read(seconds: float) -> None:
    trader._balance_cache["mainnet"]["port_ok_at"] -= seconds


def test_a_value_blip_keeps_the_last_portfolio(monkeypatch):
    _reads(monkeypatch, 5_000, None)
    _refresh()
    assert _refresh() == (10_000, 5_000)
    assert trader.portfolio_stale("mainnet") is False


def test_value_failing_past_the_window_makes_the_portfolio_unknown(monkeypatch):
    _reads(monkeypatch, 5_000, None, None, 4_000)
    _refresh()
    _refresh()
    _age_last_good_read(trader._PORTFOLIO_STALE_SEC + 1)
    # still shown (the last real read is the best number there is to show)…
    assert _refresh() == (10_000, 5_000)
    # …but not as a current one
    assert trader.portfolio_stale("mainnet") is True
    assert _refresh() == (10_000, 4_000)
    assert trader.portfolio_stale("mainnet") is False


def test_a_portfolio_never_read_is_unknown(monkeypatch):
    _reads(monkeypatch, None)
    _refresh()
    assert trader.portfolio_stale("mainnet") is True


def test_no_balance_point_is_persisted_on_an_unknown_portfolio(monkeypatch):
    monkeypatch.setattr(service.STATE, "auth_ok", True, raising=False)
    _reads(monkeypatch, 5_000, None)
    _refresh()
    assert service._balance_persistable() is True
    _refresh()
    _age_last_good_read(trader._PORTFOLIO_STALE_SEC + 1)
    assert service._balance_persistable() is False


def test_the_daily_stop_never_flattens_on_an_unknown_portfolio(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "stale.db")
    db.init_db()
    with db.get_db() as conn:
        db.insert_bot_position(conn, {
            "signal_source": "whale", "signal_id": 1, "ticker": "T", "direction": "yes",
            "target_contracts": 10, "limit_price_cents": 50, "filled_contracts": 10,
            "cost_usd": 5.0, "client_order_id": "c-1", "status": "filled",
            "network": "mainnet",
        })
    cfg = merge_with_defaults({"flatten_on_daily_stop": True, "enable_trading": True,
                               "stop_loss_on_day": -10.0})
    flattened = {"n": 0}

    async def _flatten(_cfg):
        flattened["n"] += 1
        return {}

    monkeypatch.setattr(trader, "flatten_open_positions", _flatten)
    monkeypatch.setattr(trader, "_today_pnl_balance_delta", lambda env: -20.0)
    monkeypatch.setattr(trader, "_DAY_RISK_PERSIST_SEC", 0.0)
    trader._day_risk_breach.clear()
    _reads(monkeypatch, None)
    _refresh()                       # /value has never answered
    try:
        asyncio.run(trader._maybe_flatten_on_daily_stop(cfg, "mainnet"))
    finally:
        trader._day_risk_breach.clear()
    assert flattened["n"] == 0
