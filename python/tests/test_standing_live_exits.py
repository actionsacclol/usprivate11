"""Live stop-loss / take-profit exits, and the venue-refusal status, pinned
against the shapes the real code returns (RELEASE_AUDIT_3.5 C1, H4, H6).

The earlier standing tests mocked `place()` as `{"ok": True, "message":
"sold"}` — a shape `place()` never returns. Here the stop runs through the
REAL `preview()` and `place()`; only `polymarket_api.place_limit_order` (and
the market/book reads) are stubbed, and they return exactly what that client
returns: an unmatched FAK is `{"order": {"order_id": "", "status": "unmatched"},
...}`, a matched one carries the order, with its cumQuantity, in the reply.

* C1: a triggered stop must exit MARKETABLY (bid less the slippage budget, on
  the tick) and must only be marked fired when shares actually sold.
* H4: a definite venue refusal (4xx, region 403, pre-send ValueError) is
  'rejected', not the ambiguous 'error'.
* H6: the instruction cap never evicts an armed instruction, and what fired
  stops sold survives the list.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import polymarket_api as api
import terminal as term
import terminal_trade as tt
from test_terminal_order_honesty import _reply

CID = "0xstop"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "standing.db")
    db.init_db()
    # `place()` fires a reconcile 3 s later; in a test it outlives the loop.
    monkeypatch.setattr(tt, "_spawn", lambda coro: (coro.close(), None)[1])
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    return tmp_path


# ────────────────────────────────────────────────────────────────────────
# A live market: a summary + raw meta, a book, a position.
# ────────────────────────────────────────────────────────────────────────


def _market_env(monkeypatch, *, bids, asks=None, tick=0.01, prob=None,
                held=100.0, side="yes"):
    """Bids/asks are (priceCents, size) for the token being SOLD (`side`)."""
    asks = asks or []
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    if prob is None:
        prob = best_bid if best_bid is not None else 30.0

    async def _market(cid, include_raw=False):
        m = {"conditionId": cid, "title": "Stop market", "category": "crypto",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": prob if side == "yes" else 100 - prob,
             "_raw": {"orderPriceMinTickSize": tick, "minimumTradeQty": 1,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    def _lvls(rows):
        return [{"priceCents": p, "size": s, "notionalUsd": p * s / 100.0}
                for p, s in rows]

    async def _book(cid, side="yes"):
        return {"available": True, "bids": _lvls(bids), "asks": _lvls(asks),
                "bestBid": best_bid, "bestAsk": best_ask, "note": None}

    async def _positions(*a, **kw):
        signed = held if side == "yes" else -held
        return [{"ticker": CID, "position_fp": signed}] if held else []

    async def _cash(paper):
        return None

    async def _as_read(rows):
        return rows

    monkeypatch.setattr(term, "market", _market)
    monkeypatch.setattr(term, "book", _book)
    monkeypatch.setattr(term, "clob_quotes", _as_read)
    monkeypatch.setattr(api, "get_positions", _positions)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)
    monkeypatch.setattr(tt, "_available_cash_usd", _cash)


def _venue_replies(monkeypatch, replies):
    """`api.place_limit_order` answering with the real client's shapes, one
    per call (the last repeats). An Exception instance is raised instead."""
    sent: list[dict] = []

    async def _place_limit_order(**kw):
        sent.append(kw)
        r = replies[min(len(sent), len(replies)) - 1]
        if isinstance(r, BaseException):
            raise r
        return r

    monkeypatch.setattr(api, "place_limit_order", _place_limit_order)
    return sent


UNMATCHED = _reply("0xkilled", "ORDER_STATE_CANCELED", order_type="FAK",
                   intent="ORDER_INTENT_SELL_LONG")


def _matched(shares: float, oid: str = "0xfill", *, of: float = 0.0):
    """A sell that crossed `shares`. `of` is the order's size when that is more, in
    which case the fill-and-kill's remainder was killed."""
    return _reply(oid, "ORDER_STATE_CANCELED" if of else "ORDER_STATE_FILLED", float(shares),
                  qty=float(of or shares), avg="0.25", order_type="FAK",
                  intent="ORDER_INTENT_SELL_LONG")


def _add_stop(size=100, trigger=40.0, side="yes", **extra):
    item = {"kind": "stopLoss", "conditionId": CID, "side": side,
            "triggerCents": trigger, "size": size, **extra}
    return tt.standing_add(item)


def _entry(iid):
    return next(e for e in tt.standing_list() if e["id"] == iid)


# ────────────────────────────────────────────────────────────────────────
# C1 — the exit price
# ────────────────────────────────────────────────────────────────────────


def test_exit_limit_is_bid_less_slippage_on_the_tick():
    assert tt._stop_exit_limit(28.0, 5.0, 0.01) == 23.0
    # Rounded UP onto the grid: never more slippage than the budget allows.
    assert tt._stop_exit_limit(28.0, 2.5, 0.01) == 26.0
    # A 0.1¢-tick market keeps its fractional price.
    assert tt._stop_exit_limit(3.5, 1.25, 0.001) == 2.3
    # Zero slippage is the bid itself, never above it.
    assert tt._stop_exit_limit(28.0, 0.0, 0.01) == 28.0
    assert tt._stop_exit_limit(28.05, 0.0, 0.01) == 28.0


def test_exit_limit_never_goes_outside_the_markets_ticks():
    assert tt._stop_exit_limit(3.0, 10.0, 0.01) == 1.0
    # A 0.1c-tick market trades down to 0.1c, and a stop must be able to reach it.
    assert tt._stop_exit_limit(0.5, 5.0, 0.001) == 0.1
    assert tt._stop_exit_limit(99.5, 0.0, 0.01) == 99.0


def test_a_triggered_stop_sells_at_the_bid_less_slippage_not_at_the_trigger(monkeypatch):
    """The C1 bug: the sell went out AT the 40¢ trigger while the bid sat at
    28¢, so it could never match."""
    _market_env(monkeypatch, bids=[(28.0, 500)], asks=[(30.0, 500)])
    sent = _venue_replies(monkeypatch, [_matched(100)])
    st = _add_stop(size=100, trigger=40.0, maxSlippageCents=3)
    r = _run(tt.standing_tick(allow_trading=True))
    assert len(sent) == 1
    assert sent[0]["price_cents"] == 25.0, "the exit was not priced off the bid"
    assert sent[0]["order_type"] == "FAK" and sent[0]["action"] == "sell"
    e = _entry(st["id"])
    assert e["firedAt"] and e["armed"] is False and e["retiredReason"] == "fired"
    assert e["soldSize"] == 100
    assert r["fired"] == 1 and r["alerts"][0]["ok"] is True
    assert r["alerts"][0]["blocked"] is False


def test_the_default_slippage_applies_when_none_was_written(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    sent = _venue_replies(monkeypatch, [_matched(100)])
    _add_stop()
    _run(tt.standing_tick(allow_trading=True))
    assert sent[0]["price_cents"] == 28.0 - tt.DEFAULT_SELL_SLIPPAGE_CENTS


def test_the_exit_snaps_to_a_fine_tick(monkeypatch):
    _market_env(monkeypatch, bids=[(3.5, 500)], tick=0.001, prob=3.6)
    sent = _venue_replies(monkeypatch, [_matched(100)])
    _add_stop(trigger=5.0, maxSlippageCents=1.25)
    _run(tt.standing_tick(allow_trading=True))
    assert sent[0]["price_cents"] == 2.3


# ────────────────────────────────────────────────────────────────────────
# C1 — fired only when shares sold
# ────────────────────────────────────────────────────────────────────────


def test_an_unmatched_stop_stays_armed_and_counts_a_failure(monkeypatch):
    """The real unmatched shape is ok=True / status 'unmatched' / fill 0.
    It used to be marked fired and announced 'Stop loss fired'."""
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [UNMATCHED])
    st = _add_stop()
    r = _run(tt.standing_tick(allow_trading=True))
    e = _entry(st["id"])
    assert e["armed"] is True, "a stop that sold nothing disarmed itself"
    assert e["firedAt"] is None
    assert e["failures"] == 1
    assert "did NOT fire" in e["note"] and "still armed" in e["note"]
    assert r["fired"] == 0
    a = r["alerts"][0]
    assert a["blocked"] is True and a["ok"] is False and a["soldSize"] == 0.0
    # Nothing was recorded as sold.
    assert tt.standing_sold_shares(CID, "yes", origin=None) == 0.0


def test_an_unmatched_stop_gives_up_after_max_failures_and_says_so(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    sent = _venue_replies(monkeypatch, [UNMATCHED])
    st = _add_stop()
    for _ in range(tt.MAX_STANDING_FAILURES):
        _run(tt.standing_tick(allow_trading=True))
    e = _entry(st["id"])
    assert len(sent) == tt.MAX_STANDING_FAILURES
    assert e["armed"] is False and e["retiredReason"] == "failed"
    assert "Did NOT fire" in e["note"] and "giving up" in e["note"]
    assert not e.get("soldSize")
    _run(tt.standing_tick(allow_trading=True))
    assert len(sent) == tt.MAX_STANDING_FAILURES, "a retired stop re-sent"
    # And it can be re-armed: it never sold anything.
    tt.standing_set_armed(st["id"], True)
    e = _entry(st["id"])
    assert e["armed"] is True and e["firedAt"] is None and e["failures"] == 0
    _run(tt.standing_tick(allow_trading=True))
    assert len(sent) == tt.MAX_STANDING_FAILURES + 1


def test_a_partial_fill_books_what_sold_and_keeps_the_rest_armed(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 40)])
    sent = _venue_replies(monkeypatch, [_matched(40, "0xp1", of=100), _matched(60, "0xp2")])
    st = _add_stop(size=100)
    r = _run(tt.standing_tick(allow_trading=True))
    e = _entry(st["id"])
    assert e["armed"] is True and e["firedAt"] is None
    assert e["soldSize"] == 40
    assert tt.standing_remaining(e) == 60
    assert "STILL ARMED" in e["note"]
    a = r["alerts"][0]
    assert a["ok"] is True and a["partial"] is True and a["soldSize"] == 40
    assert a["remainingSize"] == 60
    assert r["fired"] == 0
    # Durable record holds the 40 already.
    assert tt.standing_sold_shares(CID, "yes", origin=None) == 40

    # Next tick sells only the remainder, and then it has fired.
    _run(tt.standing_tick(allow_trading=True))
    assert sent[1]["count"] == 60, "re-sold the part that already filled"
    e = _entry(st["id"])
    assert e["firedAt"] and e["armed"] is False and e["soldSize"] == 100
    rec = tt.standing_sold_records()[st["id"]]
    assert rec["soldSize"] == 100 and rec["orderIds"] == ["0xp1", "0xp2"]
    assert rec["firedAt"]


def test_a_refused_stop_stays_armed_with_the_rejected_status(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [api.PolymarketAPIError(
        400, {"error": "insufficient position"})])
    st = _add_stop()
    r = _run(tt.standing_tick(allow_trading=True))
    e = _entry(st["id"])
    assert e["armed"] is True and e["failures"] == 1
    assert r["alerts"][0]["status"] == "rejected"
    assert r["alerts"][0]["blocked"] is True


def test_cancel_of_a_fired_stop_keeps_its_sold_size(monkeypatch):
    """mcp_rails.live_net subtracts what fired stops sold; deleting the
    fired entry used to delete that, letting an agent sell the shares twice."""
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [_matched(100)])
    st = _add_stop()
    _run(tt.standing_tick(allow_trading=True))
    tt.standing_cancel(st["id"])
    assert all(e["id"] != st["id"] for e in tt.standing_list())
    assert tt.standing_sold_shares(CID, "yes", origin=None) == 100
    assert tt.standing_sold_shares(CID, "no", origin=None) == 0
    # Owner filter: a manual stop is not an agent's.
    assert tt.standing_sold_shares(CID, "yes", origin="ai") == 0


def test_sold_shares_filter_by_agent(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [_matched(100)])
    st = _add_stop(origin="ai")
    rows = tt.standing_list()
    for e in rows:
        if e["id"] == st["id"]:
            e["agentId"] = "agent_a"
    tt._standing_put(rows)
    _run(tt.standing_tick(allow_trading=True))
    assert tt.standing_sold_shares(CID, "yes", agent_id="agent_a") == 100
    assert tt.standing_sold_shares(CID, "yes", agent_id="default",
                                   default_agent_id="default") == 0


# ────────────────────────────────────────────────────────────────────────
# H4 — 'rejected' vs the ambiguous 'error'
# ────────────────────────────────────────────────────────────────────────


def _sell_req():
    return {"conditionId": CID, "side": "yes", "action": "sell",
            "priceCents": 25.0, "size": 10, "orderType": "FAK", "confirmUsd": 2.8}


def _status_row(local_id):
    with db.get_db() as conn:
        return conn.execute("SELECT status, error FROM terminal_orders WHERE id=?",
                            (local_id,)).fetchone()


@pytest.mark.parametrize("exc", [
    api.PolymarketAPIError(403, {"error": "Trading restricted in your region, "
                                          "please refer to available regions"}),
    api.PolymarketAPIError(400, {"error": "insufficient buying power"}),
    api.PolymarketAPIError(400, "requested size 3 is below this market's minimum of 5"),
    api.LocalOrderRefusal(423, "Kill switch is on (manual). Nothing was sent."),
    api.EngineRestartError(425, "matching engine restarting"),
    api.PostOnlyModeError(503, "post-only mode"),
    ValueError("price_cents must be inside 0..100, got 0"),
])
def test_a_definite_refusal_is_rejected(monkeypatch, exc):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [exc])
    res = _run(tt.place(_sell_req()))
    assert res["ok"] is False
    assert res["status"] == "rejected"
    assert res["filledSize"] == 0.0 and res["orderId"] is None
    assert res["localId"]
    assert _status_row(res["localId"])["status"] == "rejected"
    assert set(res) >= {"ok", "orderId", "status", "filledSize", "avgPriceCents",
                        "localId", "message", "httpStatus", "geoblocked"}


def test_a_region_block_is_flagged_and_explained(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [api.PolymarketAPIError(
        403, {"error": "Trading restricted in your region"})])
    res = _run(tt.place(_sell_req()))
    assert res["status"] == "rejected" and res["httpStatus"] == 403
    assert res["geoblocked"] is True
    assert "where or who it came from" in res["message"]
    assert "Nothing was placed" in res["message"]


@pytest.mark.parametrize("exc", [
    api.PolymarketAPIError(502, "bad gateway"),
    api.PolymarketAPIError(408, "request timeout"),
    RuntimeError("something unexpected after send"),
])
def test_an_ambiguous_failure_stays_error(monkeypatch, exc):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [exc])
    res = _run(tt.place(_sell_req()))
    assert res["ok"] is False and res["status"] == "error"
    assert res["filledSize"] is None
    assert _status_row(res["localId"])["status"] == "error"


def test_a_read_timeout_after_send_stays_error(monkeypatch):
    import httpx
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [httpx.ReadTimeout("timed out")])
    res = _run(tt.place(_sell_req()))
    assert res["status"] == "error"


def test_a_connection_never_made_is_rejected(monkeypatch):
    import httpx
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [httpx.ConnectError("connection refused")])
    res = _run(tt.place(_sell_req()))
    assert res["status"] == "rejected"


def test_status_unknown_is_unchanged(monkeypatch):
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [api.OrderStatusUnknownError(
        "0xhash", api.PolymarketAPIError(502, "bad gateway"))])
    res = _run(tt.place(_sell_req()))
    assert res["status"] == "unknown" and res["orderId"] == "0xhash"


def test_a_clean_unmatched_reply_is_still_ok_with_zero_fill(monkeypatch):
    """Unchanged: the real FAK kill shape `place()` returns, which the stop
    tick must read as 'sold nothing'."""
    _market_env(monkeypatch, bids=[(28.0, 500)])
    _venue_replies(monkeypatch, [UNMATCHED])
    res = _run(tt.place(_sell_req()))
    assert res["ok"] is True and res["status"] == "unmatched"
    assert res["filledSize"] == 0.0


# ────────────────────────────────────────────────────────────────────────
# H6 — the cap never evicts a live instruction
# ────────────────────────────────────────────────────────────────────────


def test_the_cap_refuses_rather_than_evicting_an_armed_stop():
    first = _add_stop()
    for _ in range(tt._STANDING_MAX - 1):
        _add_stop()
    assert len(tt.standing_list()) == tt._STANDING_MAX
    with pytest.raises(tt.TerminalTradeError, match="active standing instructions"):
        _add_stop()
    ids = {e["id"] for e in tt.standing_list()}
    assert first["id"] in ids, "the oldest armed stop was evicted"
    assert len(ids) == tt._STANDING_MAX


def test_the_cap_prunes_only_retired_entries_oldest_first():
    rows = []
    for i in range(tt._STANDING_MAX):
        rows.append(_add_stop())
    # Retire three: two fired (older and newer), one failed out.
    all_rows = tt.standing_list()
    by_id = {e["id"]: e for e in all_rows}
    by_id[rows[0]["id"]].update(firedAt="2026-01-01T00:00:00+00:00",
                               armed=False, retiredReason="fired", soldSize=100)
    by_id[rows[1]["id"]].update(firedAt="2026-03-01T00:00:00+00:00",
                               armed=False, retiredReason="failed")
    by_id[rows[2]["id"]].update(firedAt="2026-02-01T00:00:00+00:00",
                               armed=False, retiredReason="fired", soldSize=7)
    tt._standing_put(all_rows)

    new = _add_stop()
    ids = {e["id"] for e in tt.standing_list()}
    assert new["id"] in ids
    assert len(ids) == tt._STANDING_MAX
    assert rows[0]["id"] not in ids, "the OLDEST retired entry should go first"
    assert rows[1]["id"] in ids and rows[2]["id"] in ids
    # Its sold size outlived it.
    assert tt.standing_sold_records()[rows[0]["id"]]["soldSize"] == 100
    assert tt.standing_sold_shares(CID, "yes", origin=None) == 100

    # Two more adds prune the next-oldest retired ones, never an armed stop.
    _add_stop()
    _add_stop()
    ids = {e["id"] for e in tt.standing_list()}
    assert rows[2]["id"] not in ids and rows[1]["id"] not in ids
    assert all(r["id"] in ids for r in rows[3:]), "an armed stop was evicted"
    assert tt.standing_sold_shares(CID, "yes", origin=None) == 107
    with pytest.raises(tt.TerminalTradeError):
        _add_stop()


def test_a_disarmed_unfired_stop_is_not_pruned():
    """Disarmed is not retired: the user (or a blocked trigger) can re-arm it."""
    rows = [_add_stop() for _ in range(tt._STANDING_MAX)]
    tt.standing_set_armed(rows[0]["id"], False)
    with pytest.raises(tt.TerminalTradeError):
        _add_stop()
    assert rows[0]["id"] in {e["id"] for e in tt.standing_list()}


def test_a_legacy_fired_stop_is_archived_from_the_order_log():
    """A stop that fired before sold sizes were recorded has only its order
    rows. Deleting it must still leave those shares counted."""
    legacy = {"id": "st_legacy", "conditionId": CID, "side": "yes",
              "kind": "stopLoss", "triggerCents": 40.0, "size": 12,
              "createdAt": "2026-01-01T00:00:00+00:00", "armed": False,
              "firedAt": "2026-01-02T00:00:00+00:00", "note": None,
              "origin": "ai", "agentId": "agent_a"}
    tt._standing_put([legacy])
    tt._record_order({"condition_id": CID, "side": "yes", "action": "sell",
                      "order_type": "FAK", "requested_size": 12,
                      "requested_price_cents": 40, "sent_size": 12,
                      "sent_price_cents": 40, "order_id": "0xold",
                      "status": "placed", "origin": "ai",
                      "standing_id": "st_legacy"})
    tt._record_order({"condition_id": CID, "side": "yes", "action": "sell",
                      "order_type": "FAK", "requested_size": 12,
                      "requested_price_cents": 40, "sent_size": 12,
                      "sent_price_cents": 40, "status": "unmatched",
                      "origin": "ai", "standing_id": "st_legacy"})
    # Listed: read through the fallback.
    assert tt.standing_sold_shares(CID, "yes", agent_id="agent_a") == 12
    tt.standing_cancel("st_legacy")
    assert tt.standing_list() == []
    assert tt.standing_sold_shares(CID, "yes", agent_id="agent_a") == 12
