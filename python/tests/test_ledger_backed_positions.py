"""Positions the app traded are booked from its fill ledger.

An agent's or the Terminal's order is recorded in terminal_fills. The wallet
reconcile then imports the holding into bot_positions as `external`, and the
status page's wins, P&L and fees are summed from those rows. In the live test of
2026-11-07 to 11-09 the rows were booked from the wallet's share count and cost
alone, and every figure on the status page was wrong:

  * 39 positions that had been SOLD were paid out as if held to settlement
    (+$34.02 that never existed): the venue's trade rows carry no buy/sell
    side, so the exit sweep could not see a sale;
  * winners were booked as total losses when the exit sweep ran before the
    resolver: the venue reports a settlement with no cash figure, and that was
    read as a $0 payout (a +$7.89 win booked as -$3.98);
  * every row was charged an estimated taker fee ($9.41 against $1.66 real).

Every figure below is from that run's database export.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import alerts
import db
import polymarket_api
import trader
import us_api as us


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "ledger-backed.db")
    db.init_db()
    monkeypatch.setattr(trader, "get_env", lambda: "mainnet")
    monkeypatch.setattr(trader, "_unknown_positions", {})
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


_n = iter(range(1, 10_000))


def _imported(ticker, *, qty, cost, direction="yes", source="external", order_id=None):
    """The row the wallet reconcile writes for a holding it finds."""
    n = next(_n)
    with db.get_db() as conn:
        return db.insert_bot_position(conn, {
            "signal_source": source, "signal_id": n, "ticker": ticker,
            "direction": direction, "category": "crypto", "action": "buy",
            "target_contracts": qty, "limit_price_cents": int(round(cost * 100 / qty)),
            "filled_contracts": qty, "avg_fill_price_cents": cost * 100 / qty,
            "cost_usd": cost, "client_order_id": f"ext-{n}", "order_id": order_id,
            "status": "filled", "network": "mainnet",
        })


def _fill(ticker, action, size, price_cents, fee=0.0, side="yes", at="2026-11-07 15:50:00"):
    n = next(_n)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, action, "
            "size, price_cents, fees_usd, origin, network, filled_at, filled_at_exact) "
            "VALUES (?,?,?,?,?,?,?,?,'ai','mainnet',?,1)",
            (f"t{n}", f"o{n}", ticker, side, action, size, price_cents, fee,
             f"{at[:-2]}{n % 60:02d}"))


def _row(pid):
    with db.get_db() as conn:
        return dict(db.fetch_position_by_id(conn, pid))


def _wallet(monkeypatch, *held):
    """A healthy positions read: something else is held, `held` too."""
    async def _positions(limit=1000):
        return ([{"ticker": "something-else", "position_fp": 5.0}]
                + [{"ticker": t, "position_fp": q} for t, q in held])
    monkeypatch.setattr(trader, "get_positions", _positions)


def _activity(monkeypatch, *rows):
    async def _get(limit=1000, **_kw):
        return [r for r in rows if r]
    monkeypatch.setattr(trader, "get_activity", _get)


def _settles(monkeypatch, result):
    async def _fetch(_ticker):
        return {"result": result, "status": "finalized"}
    monkeypatch.setattr(polymarket_api, "fetch_market", _fetch)


def _venue_settlement(slug, net):
    """A settlement as Polymarket US reports it: the position closed, no cash."""
    return us.activity_row({
        "type": "ACTIVITY_TYPE_POSITION_RESOLUTION",
        "positionResolution": {"marketSlug": slug, "tradeId": "r1",
                               "beforePosition": {"netPosition": str(net)},
                               "updateTime": "2026-11-07T16:00:05Z"}})


# ────────────────────────────────────────────────────────────────────────
# A position sold through the app is closed at its sale price
# ────────────────────────────────────────────────────────────────────────


def test_a_position_sold_through_the_app_is_not_paid_out_as_if_held(fresh, monkeypatch):
    """SOL 15:45Z: 6 bought at 40c, sold at 71c for +$1.86. Booked WIN +$4.80."""
    slug = "cpcsol-sol-updown-15m-2026-11-07-1545z"
    pid = _imported(slug, qty=6, cost=2.40)
    _fill(slug, "buy", 6, 40)
    _fill(slug, "sell", 6, 71)
    _wallet(monkeypatch)                       # gone from the wallet
    _activity(monkeypatch)                     # the venue's rows name no sale
    closed = _run(trader.detect_external_exits({}))
    assert [r["id"] for r in closed] == [pid]
    row = _row(pid)
    assert row["resolved"] == 1 and row["closed_early"] == 1
    assert row["pnl_usd"] == pytest.approx(1.86)
    assert row["settlement_usd"] == pytest.approx(4.26) and row["cost_usd"] == pytest.approx(2.40)
    assert row["fees_usd"] == 0.0 and row["outcome_correct"] == 1

    # The market then settles Up. The sale stands: nothing is paid out twice.
    _settles(monkeypatch, "yes")
    assert _run(trader.mark_resolved_positions({})) == []
    assert _row(pid)["pnl_usd"] == pytest.approx(1.86)


def test_a_resting_buy_pays_no_invented_taker_fee(fresh, monkeypatch):
    """Position #73: 47 at 40c, a maker fill the venue charged $0.00 for.
    Booked with a $0.78 fee."""
    slug = "cpceth-eth-updown-15m-2026-11-09-0100z"
    pid = _imported(slug, qty=47, cost=18.80)
    _fill(slug, "buy", 47, 40, fee=0.0)
    _settles(monkeypatch, "no")
    _run(trader.mark_resolved_positions({}))
    row = _row(pid)
    assert row["fees_usd"] == 0.0 and row["pnl_usd"] == pytest.approx(-18.80)
    assert row["outcome_correct"] == 0 and row["entry_maker"] == 1


def test_the_venues_own_fee_is_booked_when_there_was_one(fresh, monkeypatch):
    slug = "cpcxrp-xrp-updown-15m-2026-11-07-0500z"
    pid = _imported(slug, qty=5, cost=2.95)
    _fill(slug, "buy", 5, 59, fee=0.08)
    _settles(monkeypatch, "yes")
    _run(trader.mark_resolved_positions({}))
    row = _row(pid)
    assert row["fees_usd"] == pytest.approx(0.08)
    assert row["pnl_usd"] == pytest.approx(5.0 - 2.95 - 0.08) and row["outcome_correct"] == 1


def test_a_fee_not_read_yet_is_estimated_not_called_free(fresh, monkeypatch):
    slug = "cpcbtc-btc-updown-15m-2026-11-07-0700z"
    pid = _imported(slug, qty=10, cost=6.0)
    _fill(slug, "buy", 10, 60, fee=None)
    _settles(monkeypatch, "yes")
    _run(trader.mark_resolved_positions({}))
    assert _row(pid)["fees_usd"] == pytest.approx(0.0695 * 0.6 * 0.4 * 10)


def test_part_sold_part_held_to_settlement(fresh, monkeypatch):
    slug = "cpcxrp-xrp-updown-15m-2026-11-07-0515z"
    pid = _imported(slug, qty=15, cost=7.80)        # the wallet's last count
    _fill(slug, "buy", 20, 52, fee=0.08)
    _fill(slug, "sell", 5, 59)
    _settles(monkeypatch, "yes")
    _run(trader.mark_resolved_positions({}))
    row = _row(pid)
    # 5 x (0.59 - 0.52) on the sale, 15 x (1 - 0.52) at settlement, less the fee.
    assert row["pnl_usd"] == pytest.approx(0.35 + 7.20 - 0.08)
    assert row["outcome_correct"] == 1 and row["closed_early"] == 0
    assert row["settlement_usd"] - row["cost_usd"] - row["fees_usd"] == pytest.approx(row["pnl_usd"])


# ────────────────────────────────────────────────────────────────────────
# A settlement with no cash figure is not a $0 payout
# ────────────────────────────────────────────────────────────────────────


def test_a_winner_is_not_booked_as_a_loss_when_the_exit_sweep_runs_first(fresh, monkeypatch):
    """BTC 06:00Z, position #5: a +$7.89 win booked as redeemed for $0.00."""
    slug = "cpc-btc-updown-15m-2026-11-07-0600z"
    pid = _imported(slug, qty=12, cost=4.11)
    _fill(slug, "buy", 12, 34.25)
    _wallet(monkeypatch)                                  # settled: no longer held
    _activity(monkeypatch, _venue_settlement(slug, 12))
    assert _run(trader.detect_external_exits({})) == []
    assert _row(pid)["resolved"] == 0, "the resolver books it from the market's result"

    _settles(monkeypatch, "yes")
    _run(trader.mark_resolved_positions({}))
    row = _row(pid)
    assert row["pnl_usd"] == pytest.approx(7.89) and row["outcome_correct"] == 1


def test_a_settlement_row_with_no_cash_is_no_evidence_for_any_row(fresh):
    slug = "some-market"
    pos = {"ticker": slug, "direction": "yes", "created_at": "2026-11-07 00:00:00"}
    assert trader._exit_proceeds(pos, [_venue_settlement(slug, 10)]) is None
    # A row that DOES state a $0 payout for our outcome is still a settled loss.
    stated = {"conditionId": slug, "type": "REDEEM", "outcomeIndex": 0, "usdcSize": 0,
              "timestamp": time.time()}
    assert trader._exit_proceeds(pos, [stated]) == {"proceeds": 0.0, "kind": "redeemed"}


# ────────────────────────────────────────────────────────────────────────
# What the ledger does not own keeps its old booking
# ────────────────────────────────────────────────────────────────────────


def test_an_engines_own_order_is_not_booked_from_the_ledger(fresh, monkeypatch):
    slug = "engine-market"
    pid = _imported(slug, qty=10, cost=6.0, source="main", order_id="0xengine")
    _fill(slug, "buy", 10, 60, fee=0.0)
    _settles(monkeypatch, "yes")
    _run(trader.mark_resolved_positions({}))
    assert _row(pid)["pnl_usd"] == pytest.approx(10 - 6.0 - 0.0695 * 0.6 * 0.4 * 10)


def test_a_holding_the_ledger_never_bought_keeps_the_wallet_booking(fresh, monkeypatch):
    pid = _imported("bought-in-polymarkets-app", qty=10, cost=4.0)
    _settles(monkeypatch, "no")
    _run(trader.mark_resolved_positions({}))
    assert _row(pid)["pnl_usd"] == pytest.approx(-4.0 - 0.0695 * 0.4 * 0.6 * 10)


# ────────────────────────────────────────────────────────────────────────
# A holding on a market the app can't look up
# ────────────────────────────────────────────────────────────────────────


def _unknown_holding(monkeypatch):
    async def _live(limit=1000):
        return [{"ticker": "crossd-0f35de12cd7d7602", "position": 1243.0,
                 "market_exposure": 100, "title": "", "event_ticker": "", "category": ""}]

    async def _no_market(_ticker):
        return None
    monkeypatch.setattr(trader, "get_positions", _live)
    monkeypatch.setattr(trader, "fetch_market", _no_market)


def test_a_holding_on_an_unknown_market_is_not_imported(fresh, monkeypatch):
    """Six `crossd-*` holdings at $1.00 each were imported, charged an invented
    $0.07 fee each and counted as six losses. Nobody was told."""
    _unknown_holding(monkeypatch)
    summary, changed = _run(trader.reconcile_positions_with_polymarket())
    assert summary["imported_unknowns"] == 0 and changed == []
    with db.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bot_positions").fetchone()[0] == 0
    assert trader.unknown_positions() == [], "one miss can be a failed read"

    _run(trader.reconcile_positions_with_polymarket())
    [seen] = trader.unknown_positions()
    assert (seen["ticker"], seen["side"], seen["qty"], seen["costUsd"]) == (
        "crossd-0f35de12cd7d7602", "yes", 1243.0, 1.0)


def test_an_unknown_holding_raises_an_alert_with_something_to_do(fresh, monkeypatch):
    _unknown_holding(monkeypatch)
    _run(trader.reconcile_positions_with_polymarket())
    _run(trader.reconcile_positions_with_polymarket())
    [a] = alerts.build({"unknownPositions": trader.unknown_positions()})
    assert a["id"] == "unknown-positions" and a["severity"] == "error"
    assert "crossd-0f35de12cd7d7602" in a["detail"] and "$1.00" in a["detail"]
    assert "new API key" in a["fix"] and a["action"]["kind"] == "nav"
    assert alerts.build({"unknownPositions": []}) == []


def test_an_unknown_holding_is_forgotten_once_it_is_gone(fresh, monkeypatch):
    _unknown_holding(monkeypatch)
    _run(trader.reconcile_positions_with_polymarket())
    _run(trader.reconcile_positions_with_polymarket())
    for e in trader._unknown_positions.values():
        e["lastSeen"] -= trader._UNKNOWN_POSITION_TTL_SEC + 1
    assert trader.unknown_positions() == []
