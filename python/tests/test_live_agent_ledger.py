"""The live agent ledger books what the venue actually did.

Release audit 3.5 H3/H4/H5 and the "live order path" mediums: a venue refusal
is not an unknown, a fill-and-kill that missed sold nothing, a buy that filled
nothing holds no slot and no spend, an unknown that filled is seen, every
cancel path books the cancel, and every live outcome is announced. Every
venue answer here is the shape terminal_trade.place / sell_position really
return.
"""
from __future__ import annotations

import json

import pytest

import db
import mcp_server
import polymarket_api as api
import terminal_trade as tt
from test_mcp import (_agent_buy, _call, _fake_service, _forecast, _mock_book,
                      _priced, _run, _sells, _text)

_BUY = {"conditionId": "0xabc", "side": "yes", "action": "buy",
        "size": 10, "priceCents": 50}


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    import mcp_rails
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "ledger.db")
    db.init_db()
    mcp_server._sessions.clear()
    monkeypatch.setitem(mcp_rails._VENUE_SYNC, "at", None)

    async def _open(ids):
        return {str(c): None for c in ids}
    monkeypatch.setattr(tt, "_resolutions_for", _open)
    return tmp_path


def _live(monkeypatch, **cfg):
    base = {"mcp_trade_mode": "live", "mcp_live_approval": False,
            "mcp_max_order_usd": 0.0,
            "mcp_agents": [{"id": "default", "name": "Ada", "mode": "live"}]}
    base.update(cfg)
    svc = _fake_service(monkeypatch, **base)
    _mock_book(monkeypatch)
    return svc


def _venue(monkeypatch, *answers):
    """tt.place answering each call with the next shape; every order sent."""
    sent: list = []
    it = iter(answers)

    async def _place(order, *, origin="manual", standing_id=None):
        sent.append(dict(order))
        return dict(next(it))

    async def _no_reconcile(limit=0):
        return {"inserted": 0, "matched": 0}
    monkeypatch.setattr(tt, "place", _place)
    monkeypatch.setattr(tt, "reconcile_fills", _no_reconcile)
    return sent


def _pushes(monkeypatch):
    import remote
    out: list = []

    async def _push(text):
        out.append(text)
        return 1
    monkeypatch.setattr(remote, "push", _push)
    return out


_REJECTED = {"ok": False, "orderId": None, "status": "rejected", "filledSize": 0.0,
             "avgPriceCents": None, "localId": 7, "httpStatus": 403, "geoblocked": True,
             "message": "Trading is restricted in your region."}
_SENT = {"ok": True, "orderId": "0xgtc", "status": "live", "filledSize": None,
         "avgPriceCents": None, "localId": 8, "message": "Resting at 50¢"}
_UNMATCHED = {"ok": True, "orderId": None, "status": "unmatched", "filledSize": 0.0,
              "avgPriceCents": None, "localId": 9,
              "message": "Nothing crossed — the order was killed with a zero fill."}


def _rows(where="1=1"):
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM mcp_orders WHERE {where} ORDER BY id")]


# ────────────────────────────────────────────────────────────────────────
# H4 — a venue refusal is refused, not unknown
# ────────────────────────────────────────────────────────────────────────


def test_a_venue_refusal_holds_no_spend_no_slot_and_blocks_no_exit(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_max_positions=1, mcp_daily_spend_usd=8.5)
    _priced(monkeypatch, 5.0)
    sent = _venue(monkeypatch, _REJECTED, _REJECTED, _SENT)
    res = _call("place_order", {**_BUY, "forecastId": _forecast()})
    assert res["isError"] and "restricted in your region" in _text(res)
    [row] = _rows()
    assert row["status"] == "refused" and row["ok"] == 0 and row["committed_usd"] is None
    assert mcp_rails.spent_today("live") == 0.0
    assert _run(mcp_rails.live_held_markets()) == set()
    assert mcp_rails.live_exposure("0xabc", "default") == 0.0
    assert json.loads(_text(_call("get_orders", {"approvalId": row["id"]})))["status"] == "refused"

    # An exit the venue refused leaves every share sellable.
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6, cid="0xdef")
    res = _call("place_order", {"conditionId": "0xdef", "side": "yes", "action": "sell",
                                "size": 6, "priceCents": 45, "orderType": "FAK"})
    assert res["isError"]
    assert mcp_rails.live_net("0xdef", "yes", "default") == 6.0

    # And the caps still have room: $3 bought + $5 of $8.50 (the refusals
    # would have made it $18), one market (0xdef is held, so the one slot is
    # used — buy into it).
    res = _call("place_order", {**_BUY, "conditionId": "0xdef",
                                "forecastId": _forecast(cid="0xdef")})
    assert not res["isError"], _text(res)
    assert len(sent) == 3


def test_a_4xx_error_is_refused_and_a_5xx_or_timeout_stays_unknown(fresh, monkeypatch):
    import mcp_rails
    for res, want in (
            ({"ok": False, "status": "error", "httpStatus": 400, "message": "x"}, "refused"),
            ({"ok": False, "status": "error", "httpStatus": 408, "message": "x"}, "unknown"),
            ({"ok": False, "status": "error", "message": "Request timed out"}, "unknown"),
            ({"ok": False, "status": "unknown", "orderId": "0x1", "message": "x"}, "unknown"),
            (_REJECTED, "refused"),
            ({"ok": False, "status": None, "message": "book moved"}, "refused")):
        assert mcp_rails.live_outcome(res, dict(_BUY))[1] == want, res


# ────────────────────────────────────────────────────────────────────────
# H3 — exits book what filled
# ────────────────────────────────────────────────────────────────────────


def test_an_unmatched_then_partial_close_keeps_the_rest_sellable(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    base = {"orderId": None, "localId": 1, "sentSize": 6, "limitCents": 45.0,
            "avgPriceCents": None, "proceedsUsd": None}
    sold, _ = _sells(monkeypatch, result={
        **base, "ok": True, "status": "unmatched", "filledSize": 0.0, "killedSize": 6.0,
        "message": "Nothing crossed — the order was killed with a zero fill."})
    res = _call("close_position", {"conditionId": "0xabc", "side": "yes"})
    assert sold == [6.0]
    row = _rows("action='sell'")[-1]
    assert row["status"] == "unmatched" and row["filled"] == 0.0
    assert mcp_rails.live_net("0xabc", "yes", "default") == 6.0, "a miss was booked as sold"

    _sells(monkeypatch, result={
        **base, "ok": True, "status": "matched", "orderId": "0xs2", "filledSize": 2.0,
        "killedSize": 4.0, "message": "Filled 2 of 6"})
    res = _call("close_position", {"conditionId": "0xabc", "side": "yes"})
    assert not res["isError"], _text(res)
    row = _rows("action='sell'")[-1]
    assert row["status"] == "partial" and row["filled"] == 2.0
    assert mcp_rails.live_net("0xabc", "yes", "default") == 4.0

    # The rest can still be closed.
    sold, _ = _sells(monkeypatch)
    res = _call("close_position", {"conditionId": "0xabc", "side": "yes"})
    assert not res["isError"], _text(res)
    assert sold == [4.0]
    assert mcp_rails.live_net("0xabc", "yes", "default") == 0.0


def test_a_fak_sell_through_place_order_books_its_fill(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _priced(monkeypatch, 2.7)
    _venue(monkeypatch, _UNMATCHED,
           {"ok": True, "orderId": "0xs", "status": "matched", "filledSize": 3.0,
            "avgPriceCents": None, "localId": 2, "message": "Filled 3 of 6"})
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    sell = {**_BUY, "action": "sell", "size": 6, "priceCents": 45, "orderType": "FAK"}
    assert not _call("place_order", dict(sell))["isError"]
    assert mcp_rails.live_net("0xabc", "yes", "default") == 6.0
    assert not _call("place_order", dict(sell))["isError"]
    assert mcp_rails.live_net("0xabc", "yes", "default") == 3.0


def test_a_resting_gtc_sell_still_counts_in_full(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _priced(monkeypatch, 2.7)
    _venue(monkeypatch, {**_SENT, "orderId": "0xsell"})
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    assert not _call("place_order", {**_BUY, "action": "sell", "size": 6,
                                     "priceCents": 60})["isError"]
    assert mcp_rails.live_net("0xabc", "yes", "default") == 0.0


# ────────────────────────────────────────────────────────────────────────
# H5 + medium — a buy that filled nothing frees its slot and its spend
# ────────────────────────────────────────────────────────────────────────


def test_a_zero_fill_buy_frees_its_slot_and_its_spend(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_max_positions=1, mcp_daily_spend_usd=6.0)
    _priced(monkeypatch, 5.0)
    sent = _venue(monkeypatch, _UNMATCHED, _SENT)
    res = _call("place_order", {**_BUY, "orderType": "FAK", "forecastId": _forecast()})
    assert not res["isError"], _text(res)
    [row] = _rows()
    assert row["status"] == "unmatched" and row["ok"] == 1 and row["committed_usd"] == 0.0
    assert mcp_rails.spent_today("live") == 0.0
    assert _run(mcp_rails.live_held_markets()) == set()
    # The one slot and the $6 are both still free.
    res = _call("place_order", {**_BUY, "conditionId": "0xdef",
                                "forecastId": _forecast(cid="0xdef")})
    assert not res["isError"], _text(res)
    assert len(sent) == 2
    assert _run(mcp_rails.live_held_markets()) == {"0xdef"}, "a resting GTC holds its slot"
    assert mcp_rails.spent_today("live") == 5.0


def test_a_partly_filled_fak_buy_holds_only_what_filled(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _priced(monkeypatch, 5.0)
    _venue(monkeypatch, {"ok": True, "orderId": "0xp", "status": "matched",
                         "filledSize": 4.0, "avgPriceCents": None, "localId": 1,
                         "message": "Filled 4 of 10"})
    assert not _call("place_order", {**_BUY, "orderType": "FAK",
                                     "forecastId": _forecast()})["isError"]
    [row] = _rows()
    assert row["status"] == "partial" and row["filled"] == 4.0
    assert row["committed_usd"] == 2.0
    assert mcp_rails.live_exposure("0xabc", "default") == 4.0
    assert _run(mcp_rails.live_held_markets()) == {"0xabc"}


def test_a_cancelled_or_sold_out_market_holds_no_slot(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xrest", size=10, filled_shares=0, cid="0xr")
        _agent_buy(conn, oid="0xgone", size=10, filled_shares=0, cid="0xg")
        _agent_buy(conn, oid="0xsold", size=6, filled_shares=6, cid="0xs")
        _agent_buy(conn, oid="0xheld", size=6, filled_shares=6, cid="0xh")
        conn.execute(
            "INSERT INTO mcp_orders (created_at, network, mode, agent_id, condition_id, "
            "side, action, order_type, size, price_cents, ok, order_id, filled, status) "
            "VALUES (datetime('now'),'mainnet','live','default','0xs','yes','sell','FAK',"
            "6,45,1,'0xs1',6,'filled')")

    async def _cancelled(oid, *, market=""):
        return {"canceled": [oid]}
    monkeypatch.setattr(tt.api, "cancel_order", _cancelled)
    assert _run(tt.cancel("0xgone"))["ok"]
    assert _run(mcp_rails.live_held_markets()) == {"0xr", "0xh"}
    assert mcp_rails.spent_today("live") == 5.0 + 3.0 + 3.0, "the cancelled buy still spends"


# ────────────────────────────────────────────────────────────────────────
# Medium — an 'unknown' that filled is seen, and promoted
# ────────────────────────────────────────────────────────────────────────


def test_an_unknown_order_that_filled_counts_and_is_promoted(fresh, monkeypatch):
    import mcp_rails
    import terminal as term
    _live(monkeypatch)
    _priced(monkeypatch, 5.0)
    oid = "0x" + "ab" * 32
    _venue(monkeypatch, {"ok": False, "orderId": oid, "status": "unknown",
                         "filledSize": None, "avgPriceCents": None, "localId": 3,
                         "message": "Polymarket did not confirm this order"})
    assert _call("place_order", {**_BUY, "forecastId": _forecast()})["isError"]
    [row] = _rows()
    assert row["status"] == "unknown" and row["order_id"] == oid
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, action, "
            "size, price_cents, fees_usd, origin, network, filled_at) "
            "VALUES ('t1',?,'0xabc','yes','buy',10,50,0,'ai','mainnet',?)",
            (oid, mcp_rails.today() + "T00:00:01Z"))
    # Seen before any promotion: sell allowance, cancel, loss stop.
    assert mcp_rails.live_net("0xabc", "yes", "default") == 10.0
    assert oid in mcp_rails.agent_order_ids("default")

    async def _marks(ids):
        return [{"conditionId": c, "bid": 40.0, "ask": 42.0, "resolvedOutcome": None}
                for c in ids]
    monkeypatch.setattr(term, "marked_summaries", _marks)
    d = _run(mcp_rails._live_day_pnl(mcp_rails.today()))
    assert round(d["openLossUsd"], 4) == 1.0, d

    assert mcp_rails.promote_from_ledger() == 1
    [row] = _rows()
    # 10 of 10 on the ledger: placed, and nothing of it is left to fill.
    assert row["ok"] == 1 and row["status"] == "filled" and row["filled"] == 10.0
    assert row["avg_fill_cents"] == 50.0
    assert mcp_rails.spent_today("live") == 5.0


def _sent_gtc(conn, oid, *, action="buy", size=10, filled=0, status=None):
    conn.execute(
        "INSERT INTO mcp_orders (created_at, network, mode, agent_id, condition_id, "
        "side, action, order_type, size, price_cents, committed_usd, ok, order_id, "
        "filled, status, message) VALUES (datetime('now'),'mainnet','live','default',"
        "'0xabc','yes',?,'GTC',?,50,?,1,?,?,?,'Resting at 50¢ — nothing has filled')",
        (action, size, 5.0 if action == "buy" else None, oid, filled, status))


def _ledger_fill(conn, trade, oid, size, price, action="buy"):
    conn.execute(
        "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, action, "
        "size, price_cents, origin, network, filled_at) "
        "VALUES (?,?,'0xabc','yes',?,?,?,'ai','mainnet',datetime('now'))",
        (trade, oid, action, size, price))


def test_a_resting_order_that_filled_later_is_booked_filled(fresh, monkeypatch):
    """Live test 2026-11-07: 125 filled GTC orders kept status NULL, filled 0 and
    'Resting … nothing has filled' for good. The venue sync skips a row whose
    ledger covers its size, and nothing else looked."""
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        _sent_gtc(conn, "0xrest")
        _ledger_fill(conn, "t1", "0xrest", 4, 50)
        _ledger_fill(conn, "t2", "0xrest", 6, 45)
    mcp_rails.promote_from_ledger()
    [row] = _rows()
    assert (row["status"], row["filled"], row["avg_fill_cents"]) == ("filled", 10.0, 47.0)
    assert row["message"] == "Filled 10 YES at 47¢."
    assert row["committed_usd"] == 5.0, "the cap keeps the order's worst case"


def test_a_partly_filled_resting_order_stays_open_at_its_ledger_fill(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        _sent_gtc(conn, "0xpart")
        _ledger_fill(conn, "t1", "0xpart", 4, 50)
    mcp_rails.promote_from_ledger()
    [row] = _rows()
    assert (row["status"], row["filled"], row["avg_fill_cents"]) == (None, 4.0, 50.0)
    assert "Resting" in row["message"]


def test_a_filled_resting_sell_comes_off_the_agents_exposure(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        _sent_gtc(conn, "0xbuy", filled=10, status="filled")
        _ledger_fill(conn, "t1", "0xbuy", 10, 50)
        _sent_gtc(conn, "0xsell", action="sell")
        _ledger_fill(conn, "t2", "0xsell", 10, 55, action="sell")
    before = mcp_rails.live_exposure("0xabc", "default")
    mcp_rails.promote_from_ledger()
    assert before == 10.0 and mcp_rails.live_exposure("0xabc", "default") == 0.0


def test_an_unknown_fak_is_closed_at_its_ledger_fill_on_reconcile(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO mcp_orders (created_at, network, mode, agent_id, condition_id, "
            "side, action, order_type, size, price_cents, committed_usd, ok, order_id, "
            "status) VALUES (datetime('now'),'mainnet','live','default','0xabc','yes',"
            "'buy','FAK',10,50,5.0,0,'0xu','unknown')")
        conn.execute(
            "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, action, "
            "size, price_cents, origin, network, filled_at) "
            "VALUES ('t1','0xu','0xabc','yes','buy',4,50,'ai','mainnet',datetime('now'))")
    mcp_rails.promote_from_ledger()
    [row] = _rows()
    assert (row["ok"], row["status"], row["filled"], row["committed_usd"]) == (
        1, "partial", 4.0, 2.0)


# ────────────────────────────────────────────────────────────────────────
# Settling an 'unknown' (user RPC mcpSettleUnknown)
# ────────────────────────────────────────────────────────────────────────


def _unknown_row(conn, *, oid=None, action="buy", cid="0xabc"):
    cur = conn.execute(
        "INSERT INTO mcp_orders (created_at, network, mode, agent_id, condition_id, "
        "side, action, order_type, size, price_cents, committed_usd, ok, order_id, "
        "status, message) VALUES (datetime('now','-5 minutes'),'mainnet','live',"
        "'default',?,'yes',?,'GTC',10,50,?,0,?,'unknown','send errored')",
        (cid, action, 5.0 if action == "buy" else None, oid))
    return int(cur.lastrowid)


def _account_trades(monkeypatch, *qtys, fail=None):
    """The signed activity list: one Trade per quantity, made just now."""
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _authed(method, path, *, params=None, **_kw):
        assert (method, path) == ("GET", "/v1/portfolio/activities")
        if fail:
            raise fail
        return {"activities": [
            {"type": "ACTIVITY_TYPE_TRADE",
             "trade": {"id": f"t{i}", "marketSlug": params["marketSlug"], "qtyDecimal": str(q),
                       "price": {"value": "0.5"}, "createTime": now}}
            for i, q in enumerate(qtys)]}
    monkeypatch.setattr(api, "_authed_request", _authed)


async def _no_orders(cid=None):
    return {"orders": [], "fetchedAt": "x", "note": "No resting orders in this market."}


def test_settling_an_unknown_the_venue_never_saw_voids_it(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch, mcp_max_positions=1)
    _venue(monkeypatch)
    with db.get_db() as conn:
        with_id = _unknown_row(conn, oid="0xlost")
        no_id = _unknown_row(conn, cid="0xdef")
    assert mcp_rails.spent_today("live") == 10.0
    assert _run(mcp_rails.live_held_markets()) == {"0xabc", "0xdef"}

    async def _404(oid):
        raise api.PolymarketAPIError(404, {"error": "not found"})
    monkeypatch.setattr(api, "get_order", _404)
    monkeypatch.setattr(tt, "resting_orders", _no_orders)
    _account_trades(monkeypatch)
    out = _run(mcp_rails.settle_unknown(with_id))
    assert out["ok"] and out["status"] == "void", out
    out = _run(mcp_rails.settle_unknown(no_id))
    assert out["ok"] and out["status"] == "void", out
    assert mcp_rails.spent_today("live") == 0.0
    assert _run(mcp_rails.live_held_markets()) == set()
    assert not _run(mcp_rails.settle_unknown(no_id))["ok"], "settled twice"


def test_a_placeholder_id_is_not_asked_about_and_never_voided_for_it(fresh, monkeypatch):
    """A lost reply loses the id too: what the row holds is a placeholder of ours.
    The API module answers one with a 404 without asking anybody, and that would
    read as "Polymarket has no such order" and void an order that may have filled."""
    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        rid = _unknown_row(conn, oid=api.UNKNOWN_ID_PREFIX + "abc")
    asked = []

    async def _order(oid):
        asked.append(oid)
        raise api.PolymarketAPIError(404, "not an order id")
    monkeypatch.setattr(api, "get_order", _order)
    monkeypatch.setattr(tt, "resting_orders", _no_orders)
    _account_trades(monkeypatch, 10)        # it did fill
    out = _run(mcp_rails.settle_unknown(rid))
    assert not out["ok"] and "traded more" in out["message"], out
    assert asked == []
    assert _rows()[0]["status"] == "unknown"


def test_a_trade_no_order_of_ours_explains_keeps_it_unknown(fresh, monkeypatch):
    """A trade activity names no order and no side, so the only comparison there
    is, is quantity: traded in the market against what our own orders filled."""
    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    monkeypatch.setattr(tt, "resting_orders", _no_orders)
    with db.get_db() as conn:
        rid = _unknown_row(conn)
        conn.execute(
            "INSERT INTO terminal_orders (condition_id, side, action, order_type, requested_size, "
            "requested_price_cents, order_id, status) "
            "VALUES ('0xabc','yes','buy','FAK',4,50,'known1','filled')")
        conn.execute(
            "INSERT INTO terminal_fills (trade_id, order_id, condition_id, side, action, size, "
            "price_cents, origin, network, filled_at) VALUES ('order:known1','known1','0xabc',"
            "'yes','buy',4,50,'manual','mainnet',datetime('now'))")

    _account_trades(monkeypatch, 4, 6)      # 6 more than the known order filled
    out = _run(mcp_rails.settle_unknown(rid))
    assert not out["ok"] and "traded more" in out["message"], out
    assert _rows()[0]["status"] == "unknown"

    _account_trades(monkeypatch, 4)         # every contract accounted for
    out = _run(mcp_rails.settle_unknown(rid))
    assert out["ok"] and out["status"] == "void", out


def test_a_trade_list_that_cannot_be_read_refuses_the_settle(fresh, monkeypatch):
    """`api.get_activity` swallows a failed read. Here that would be "no trades",
    and it would void an order that filled."""
    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    monkeypatch.setattr(tt, "resting_orders", _no_orders)
    with db.get_db() as conn:
        rid = _unknown_row(conn)
    _account_trades(monkeypatch, fail=api.PolymarketAPIError(500, "down"))
    out = _run(mcp_rails.settle_unknown(rid))
    assert not out["ok"] and "Could not read your trades" in out["message"], out
    assert _rows()[0]["status"] == "unknown"


def test_settling_refuses_while_the_venue_shows_something_that_could_be_it(
        fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        rid = _unknown_row(conn)

    async def _stray(cid=None):
        return {"orders": [{"orderId": "0xstray", "side": "yes", "action": "buy",
                            "origin": "external"}], "fetchedAt": "x", "note": None}
    monkeypatch.setattr(tt, "resting_orders", _stray)
    out = _run(mcp_rails.settle_unknown(rid))
    assert not out["ok"] and "0xstray" in out["message"]

    async def _unreadable(cid=None):
        return {"orders": [], "fetchedAt": "x", "note": "Could not read your open orders: x"}
    monkeypatch.setattr(tt, "resting_orders", _unreadable)
    assert not _run(mcp_rails.settle_unknown(rid))["ok"]
    assert _rows()[0]["status"] == "unknown"


def test_settling_an_unknown_found_resting_or_cancelled_books_it(fresh, monkeypatch):
    import time

    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    # The settle's own question, not the throttled background sweep's.
    monkeypatch.setitem(mcp_rails._VENUE_SYNC, "at", time.monotonic())
    with db.get_db() as conn:
        resting = _unknown_row(conn, oid="0xrest")
        gone = _unknown_row(conn, oid="0xcxl")

    async def _order(oid):
        if oid == "0xrest":
            return {"order": {"id": oid, "status": "live", "size_matched": 0.0,
                              "original_size": 10.0}}
        return {"order": {"id": oid, "status": "canceled", "size_matched": 3.0,
                          "original_size": 10.0}}
    monkeypatch.setattr(api, "get_order", _order)
    out = _run(mcp_rails.settle_unknown(resting))
    assert out["ok"] and out["status"] is None
    out = _run(mcp_rails.settle_unknown(gone))
    assert out["ok"] and out["status"] == "cancelled"
    rows = {r["id"]: r for r in _rows()}
    assert rows[resting]["ok"] == 1 and rows[resting]["committed_usd"] == 5.0
    assert rows[gone]["filled"] == 3.0 and rows[gone]["committed_usd"] == 1.5


def test_the_settle_rpc_is_registered():
    import service
    assert service._HANDLERS["mcpSettleUnknown"] is service._h_mcp_settle_unknown


# ────────────────────────────────────────────────────────────────────────
# Medium — every cancel path books the agent ledger
# ────────────────────────────────────────────────────────────────────────


def test_the_apps_cancel_books_an_agents_order(fresh, monkeypatch):
    _live(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xmine", size=10, filled_shares=4)

    async def _cancelled(oid, *, market=""):
        return {"canceled": [oid]}
    monkeypatch.setattr(tt.api, "cancel_order", _cancelled)
    assert _run(tt.cancel("0xmine"))["ok"]       # what the app's Cancel calls
    [row] = _rows()
    assert (row["status"], row["filled"], row["committed_usd"]) == ("cancelled", 4.0, 2.0)
    # A user's own order is not an agent's: nothing to book, nothing breaks.
    assert _run(tt.cancel("0xusers"))["ok"]


def test_the_phones_cancelall_books_an_agents_order(fresh, monkeypatch):
    import remote
    svc, _ = _live(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xsell", size=6, filled_shares=0)
        conn.execute("UPDATE mcp_orders SET action='sell', committed_usd=NULL")

    async def _engine_cancel(_p):
        return {"canceled": 0}
    svc._h_cancelAllOpen = _engine_cancel

    async def _resting(cid=None):
        return {"orders": [{"orderId": "0xsell"}], "fetchedAt": "x", "note": None}

    async def _cancelled(oid, *, market=""):
        return {"canceled": [oid]}

    async def _left():
        return 0
    monkeypatch.setattr(tt, "resting_orders", _resting)
    monkeypatch.setattr(tt.api, "cancel_order", _cancelled)
    monkeypatch.setattr(remote, "_wallet_orders_left", _left)
    _run(remote._cancel_all())
    [row] = _rows()
    assert row["status"] == "cancelled" and row["filled"] == 0.0


def test_a_venue_side_cancel_is_booked_at_reconcile(fresh, monkeypatch):
    import mcp_rails
    _live(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xvc", size=10, filled_shares=2)
        _agent_buy(conn, oid="0xlive", size=10, filled_shares=0)
        conn.execute("UPDATE mcp_orders SET created_at=datetime('now','-10 minutes')")
    asked = []

    async def _order(oid):
        asked.append(oid)
        st = "canceled" if oid == "0xvc" else "live"
        return {"order": {"id": oid, "status": st, "size_matched": 2.0,
                          "original_size": 10.0}}
    monkeypatch.setattr(api, "get_order", _order)
    assert _run(mcp_rails.refresh_fills())
    assert sorted(asked) == ["0xlive", "0xvc"]
    rows = {r["order_id"]: r for r in _rows()}
    assert rows["0xvc"]["status"] == "cancelled" and rows["0xvc"]["filled"] == 2.0
    assert rows["0xlive"]["status"] is None
    # Throttled: the next refresh does not ask again.
    assert _run(mcp_rails.refresh_fills())
    assert len(asked) == 2


# ────────────────────────────────────────────────────────────────────────
# Medium — every live outcome is announced, before the agent hears "no"
# ────────────────────────────────────────────────────────────────────────


def test_every_live_outcome_is_announced_refusals_and_unknown_included(fresh, monkeypatch):
    _svc, events = _live(monkeypatch)
    pushes = _pushes(monkeypatch)
    _priced(monkeypatch, 5.0)
    _venue(monkeypatch, _REJECTED,
           {"ok": False, "orderId": None, "status": "error", "filledSize": None,
            "avgPriceCents": None, "localId": 2, "message": "Request timed out"},
           _SENT)
    for _ in range(3):
        _call("place_order", {**_BUY, "forecastId": _forecast()})
    orders = [p for (n, p) in events if n == "mcp:order"]
    assert [o["status"] for o in orders] == ["refused", "unknown", "sent"]
    ev = orders[0]
    for k in ("mode", "status", "agentName", "title", "side", "action", "size",
              "price", "message", "reason", "conditionId"):
        assert k in ev, k
    assert ev["mode"] == "live" and ev["agentName"] == "Ada" and ev["title"] == "T"
    assert (ev["side"], ev["action"], ev["size"], ev["price"]) == ("yes", "buy", 10.0, 50.0)
    assert "REFUSED" in ev["message"] and "restricted in your region" in ev["reason"]
    assert len(pushes) == 3 and "UNKNOWN" in pushes[1]


def test_a_rail_refusal_is_announced_to_the_app_but_not_the_phone(fresh, monkeypatch):
    _svc, events = _live(monkeypatch)
    pushes = _pushes(monkeypatch)
    _priced(monkeypatch, 5.0)
    sent = _venue(monkeypatch)
    res = _call("place_order", dict(_BUY))            # no forecast
    assert res["isError"] and sent == []
    [ev] = [p for (n, p) in events if n == "mcp:order"]
    assert ev["status"] == "blocked" and "record_forecast" in ev["reason"]
    assert pushes == []


def test_an_approved_order_the_venue_refuses_is_announced(fresh, monkeypatch):
    import mcp_rails
    _svc, events = _live(monkeypatch, mcp_live_approval=True)
    _pushes(monkeypatch)
    _priced(monkeypatch, 5.0)
    _venue(monkeypatch, _REJECTED)
    rid = json.loads(_text(_call("place_order", {**_BUY, "forecastId": _forecast()})))[
        "approvalId"]
    assert not _run(mcp_rails.decide(rid, True))["ok"]
    statuses = [p.get("status") for (n, p) in events if n == "mcp:order"]
    assert statuses == ["pending", "refused"]
    assert mcp_rails.spent_today("live") == 0.0


def test_a_live_close_is_announced_whatever_happened(fresh, monkeypatch):
    _svc, events = _live(monkeypatch)
    _pushes(monkeypatch)
    _venue(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="0xb1", size=6, filled_shares=6)
    _sells(monkeypatch, result={"ok": False, "orderId": None, "status": "rejected",
                                "sentSize": 6, "limitCents": 45.0, "filledSize": 0.0,
                                "message": "Trading is restricted in your region."})
    _call("close_position", {"conditionId": "0xabc", "side": "yes"})
    [ev] = [p for (n, p) in events if n == "mcp:order"]
    assert ev["status"] == "refused" and ev["action"] == "sell"
    assert _rows("action='sell'")[-1]["status"] == "refused"


# ────────────────────────────────────────────────────────────────────────
# flatten_wallet — a fill-and-kill that crossed nothing sold nothing
# ────────────────────────────────────────────────────────────────────────


def test_flatten_counts_an_unmatched_exit_as_not_sold(monkeypatch):
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)

    async def _positions():
        return [("0xa", "yes", 5.0), ("0xb", "no", 5.0)], 0, 0
    monkeypatch.setattr(tt, "_flatten_positions", _positions)
    monkeypatch.setattr(tt, "_engine_tracked_markets", lambda: set())

    async def _quote(cid, side, size):
        return ({"conditionId": cid, "side": side, "action": "sell", "size": size,
                 "priceCents": 40, "orderType": "FAK", "confirmUsd": 2.0},
                {"costUsd": 2.0}, None)
    monkeypatch.setattr(tt, "_flatten_quote", _quote)
    answers = iter([dict(_UNMATCHED),
                    {"ok": True, "orderId": "0xs", "status": "matched", "filledSize": 5.0,
                     "avgPriceCents": None, "localId": 2, "message": "sold"}])

    async def _place(req, *, origin="manual", standing_id=None):
        return next(answers)
    monkeypatch.setattr(tt, "place", _place)
    out = _run(tt.flatten_wallet(allow_trading=True))
    assert (out["sold"], out["refused"], out["ok"]) == (1, 1, False)
    assert out["results"][0]["ok"] is False and "still open" in out["results"][0]["message"]
