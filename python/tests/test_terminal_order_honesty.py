"""What an order actually DID, said in the right tense.

Every case here is one the owner hit in a live test on 2026-09-03, in order:

  1. `buy 5 YES @ 14c GTC` against a 15c ask came back "Order sent … fill
     details appear once it reconciles". It never filled. Nothing in the app
     said it was still on the book, so it read as "bought".
  2. The attempt to sell that non-existent position came back
     "not enough balance / allowance: … balance: 0, order amount: 5000000",
     rendered raw on the ticket.
  3. Three buys costing $3.35, $5 and $7 were reviewed and sent against a
     $2.94 balance. Nothing checked, and the exchange's refusal was again the
     raw text.

Pure logic — no network, no clock, no sleeps.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt
import us_api as us


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "terminal.db")
    db.init_db()
    return tmp_path


def _market(monkeypatch, *, asks=(), bids=(), available=True, tick=0.01, min_qty=1):
    """A fee-free open market whose YES book holds exactly these levels."""
    async def _m(cid, include_raw=False):
        m = {"conditionId": cid, "title": "Elon tweets?", "category": "politics",
             "status": "open", "spread": 1.0, "liquidityUsd": 1e6,
             "hoursToClose": 50.0, "probability": 15.0,
             "bid": bids[0][0] if bids else None, "ask": asks[0][0] if asks else None,
             "_raw": {"orderPriceMinTickSize": tick, "minimumTradeQty": min_qty,
                      "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                      "feeCoefficient": 0}}
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    async def _b(cid, side="yes"):
        if not available:
            return {"available": False, "bids": [], "asks": [],
                    "bestBid": None, "bestAsk": None,
                    "note": "The exchange did not return a book for this market."}
        return {
            "available": True, "note": None,
            "asks": [{"priceCents": p, "size": s} for p, s in asks],
            "bids": [{"priceCents": p, "size": s} for p, s in bids],
            "bestAsk": asks[0][0] if asks else None,
            "bestBid": bids[0][0] if bids else None,
        }

    monkeypatch.setattr(tt.term, "market", _m)
    monkeypatch.setattr(tt.term, "book", _b)
    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)
    monkeypatch.setattr(tt, "_spawn", lambda coro: coro.close())


def _reply(oid, state="ORDER_STATE_NEW", cum=0.0, *, qty=5.0, avg=None, order_type="GTC",
           intent="ORDER_INTENT_BUY_LONG"):
    """What `api.place_limit_order` returns: POST /v1/orders parsed, raw kept. The
    newest execution carries the order's state; every price on it is YES-quoted."""
    order = {"id": oid, "marketSlug": "0xabc", "intent": intent, "state": state,
             "quantity": qty, "cumQuantity": cum, "leavesQuantity": qty - cum,
             "price": {"value": "0.14", "currency": "USD"}}
    if avg is not None:
        order["avgPx"] = {"value": avg, "currency": "USD"}
    raw = {"id": oid, "executions": [{"type": "EXECUTION_TYPE_TRADE", "order": order}]}
    p = us.parse_place_response(raw, order_type)
    return {"order": {"order_id": p["order_id"], "status": p["status"]}, "raw": raw}


def _no_answer(oid):
    """Accepted, and the engine did not answer inside the wait: an id, no order."""
    raw = {"id": oid, "executions": []}
    p = us.parse_place_response(raw, "GTC")
    return {"order": {"order_id": p["order_id"], "status": p["status"]}, "raw": raw}


def _balance(monkeypatch, usd):
    """Pin what the account can spend. `None` = we could not read it."""
    async def _avail(paper):
        return usd
    monkeypatch.setattr(tt, "_available_cash_usd", _avail)


# ────────────────────────────────────────────────────────────────────────
# 1. A placed order that RESTS says so.
# ────────────────────────────────────────────────────────────────────────


def test_a_resting_buy_is_not_reported_as_a_fill(fresh_db, monkeypatch):
    """The exact live case: buy 5 YES at 14c with the ask at 15c. The venue
    accepts it and it sits there. The message must name the reference price
    and refuse to call it a position."""
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0xdead")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["ok"] is True
    assert "Resting at 14¢" in r["message"], r["message"]
    assert "the best ask is 15¢" in r["message"], r["message"]
    assert "not a position until it fills" in r["message"]
    assert "cancel or reprice" in r["message"]
    # The sentence that started the whole mess must be gone.
    assert "reconciles" not in r["message"]


def test_a_resting_sell_quotes_the_bid_not_the_ask(fresh_db, monkeypatch):
    """The 51c sell on the 5-minute window. A sell rests against the BID, so
    quoting the ask back at the user would explain nothing."""
    _market(monkeypatch, asks=[(55.0, 500)], bids=[(49.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0xbeef", intent="ORDER_INTENT_SELL_LONG")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "sell",
                       "priceCents": 51, "size": 5, "orderType": "GTC",
                       "confirmUsd": 2.55}))
    assert "Resting at 51¢" in r["message"], r["message"]
    assert "the best bid is 49¢" in r["message"], r["message"]


def test_a_resting_order_with_an_unreadable_book_does_not_invent_a_touch(
        fresh_db, monkeypatch):
    _market(monkeypatch, available=False)
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0x1")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert "Resting at 14¢" in r["message"]
    assert "could not read the touch" in r["message"]
    assert "not a position until it fills" in r["message"]


def _no_book(monkeypatch):
    async def _b(cid, side="yes"):
        return {"available": False, "bids": [], "asks": [], "bestBid": None, "bestAsk": None,
                "note": "The exchange did not return a book for this market."}
    monkeypatch.setattr(tt.term, "book", _b)


def test_a_touch_the_market_read_has_is_quoted_without_the_book(fresh_db, monkeypatch):
    """Live 2026-11-06: orders placed in a new window's first minutes, when its
    book isn't served yet, were told "we could not read the touch" although the
    market read had a live two-sided quote."""
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _no_book(monkeypatch)
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0x2")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    order = {"conditionId": "0xabc", "side": "yes", "action": "buy",
             "priceCents": 14, "size": 5, "orderType": "GTC"}
    pv = _run(tt.preview(order))
    assert (pv["bestBidCents"], pv["bestAskCents"], pv["crossesNow"]) == (13.0, 15.0, False)
    assert pv["fillableSize"] is None, "depth still needs the book"
    assert any("Could not read the order book" in w for w in pv["warnings"])
    r = _run(tt.place({**order, "confirmUsd": 0.70}))
    assert "the best ask is 15¢" in r["message"], r["message"]
    assert "could not read the touch" not in r["message"]


def test_a_post_only_ticket_rests_for_no_fee_or_is_refused(fresh_db, monkeypatch):
    """Post-only is how a bid is kept from ever paying the taker fee."""
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)
    sent = []

    async def _place(**kw):
        sent.append(kw)
        return _reply("0x3")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    order = {"conditionId": "0xabc", "side": "yes", "action": "buy",
             "priceCents": 14, "size": 5, "orderType": "GTC", "postOnly": True}
    pv = _run(tt.preview(order))
    assert pv["postOnly"] is True and not pv["blockers"]
    assert pv["feeUsd"] == 0.0 and pv["maxLossUsd"] == 0.70
    assert not any("taker case" in w for w in pv["warnings"])
    assert _run(tt.place({**order, "confirmUsd": 0.70}))["ok"] is True
    assert sent[0]["post_only"] is True

    # At the ask it would take: refused before anything is sent.
    crossing = _run(tt.preview({**order, "priceCents": 15}))
    assert any("Post-only" in b and "best ask of 15¢" in b for b in crossing["blockers"])
    r = _run(tt.place({**order, "priceCents": 15, "confirmUsd": 0.75}))
    assert r["ok"] is False and len(sent) == 1
    # A fill-and-kill cannot rest.
    assert any("Post-only needs a resting" in b for b in _run(tt.preview(
        {**order, "orderType": "FAK"}))["blockers"])
    # An ordinary ticket is sent the way it always was.
    _run(tt.place({**order, "postOnly": False, "confirmUsd": 0.70}))
    assert "post_only" not in sent[1]


def test_a_filled_order_says_filled_at_the_exchanges_average(fresh_db, monkeypatch):
    """The reply carries the fill, so the sentence can state it rather than
    promise it later. The average is the exchange's, not the limit that was sent."""
    _market(monkeypatch, asks=[(13.0, 500)], bids=[(12.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0x2", "ORDER_STATE_FILLED", 5.0, avg="0.13")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["message"] == "Filled 5 YES at 13¢.", r["message"]
    assert r["filledSize"] == 5.0 and r["avgPriceCents"] == 13.0


def test_a_no_fill_reads_in_the_price_of_no_not_the_yes_quote_on_the_wire(
        fresh_db, monkeypatch):
    """Every price on the venue's order is YES-quoted. Buying NO at 30c comes back
    with an average of 0.70, and reading that as is would report a fill at 70c."""
    _market(monkeypatch, asks=[(71.0, 500)], bids=[(70.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0xn", "ORDER_STATE_FILLED", 5.0, avg="0.70",
                      intent="ORDER_INTENT_BUY_SHORT")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    r = _run(tt.place({"conditionId": "0xabc", "side": "no", "action": "buy",
                       "priceCents": 30, "size": 5, "orderType": "GTC",
                       "confirmUsd": 1.50}))
    assert r["message"] == "Filled 5 NO at 30¢.", r["message"]
    assert r["avgPriceCents"] == 30.0


def test_a_killed_fak_is_a_clean_no_fill(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0xk", "ORDER_STATE_CANCELED", order_type="FAK")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    # It crossed on the read, and the book had moved by the time it arrived.
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 15, "size": 5, "orderType": "FAK",
                       "confirmUsd": 0.75}))
    assert r["ok"] is True and r["status"] == "unmatched" and r["filledSize"] == 0.0
    assert "Nothing crossed" in r["message"]


def test_a_limit_order_the_exchange_canceled_is_not_called_resting(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0xc", "ORDER_STATE_CANCELED")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert "canceled this order" in r["message"] and "Resting" not in r["message"]
    assert r["filledSize"] == 0.0


def test_a_partial_fak_says_what_filled_and_what_was_killed(
        fresh_db, monkeypatch):
    """A marketable order that crossed 3 of 5 killed the other 2. Reported
    only because the VENUE said so — we never derive a fill from the book."""
    _market(monkeypatch, asks=[(14.0, 3)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0x3", "ORDER_STATE_CANCELED", 3.0, avg="0.14", order_type="FAK")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "FAK",
                       "confirmUsd": 0.42}))
    assert "Filled 3 of 5" in r["message"], r["message"]
    assert "remaining 2 was killed" in r["message"], r["message"]
    assert r["filledSize"] == 3


def test_a_partial_gtc_says_the_remainder_is_still_on_the_book(
        fresh_db, monkeypatch):
    """The other half of a partial: a limit order's remainder RESTS, so the
    sentence about killing it would be false."""
    _market(monkeypatch, asks=[(14.0, 3)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0x4", "ORDER_STATE_PARTIALLY_FILLED", 3.0, avg="0.14")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert "Filled 3 of 5" in r["message"], r["message"]
    assert "remaining 2 is RESTING" in r["message"], r["message"]
    assert "killed" not in r["message"]


def test_a_fill_size_the_venue_did_not_report_is_not_guessed():
    """No order on the reply is the exchange not having said. Reading that as zero
    is "nothing has filled", and a user who believes it can double up."""
    assert tt._matched_size(_no_answer("0xa")) is None
    assert tt._matched_size({"raw": {"errorMsg": "boom"}}) is None
    assert tt._matched_size({"raw": {}}) is None
    assert tt._matched_size(None) is None
    assert tt._matched_size(_reply("0xa")) == 0.0
    assert tt._matched_size(_reply("0xa", "ORDER_STATE_PARTIALLY_FILLED", 3.0)) == 3.0


def test_an_order_accepted_with_no_answer_is_not_called_resting(fresh_db, monkeypatch):
    """POST /v1/orders waits two seconds for the matching engine and may answer with
    an id and no executions. That order may already have crossed. Calling it
    "Resting, nothing has filled" sends the user to resend or to cancel a fill."""
    _market(monkeypatch, asks=[(14.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _no_answer("0xd1")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["ok"] is True and r["filledSize"] is None and r["orderId"] == "0xd1"
    assert "Resting" not in r["message"], r["message"]
    assert "nothing has filled" not in r["message"], r["message"]
    assert "has not said yet whether it filled" in r["message"], r["message"]
    assert "do not count it as a position" in r["message"]


def test_a_matched_buy_reports_the_shares_it_took(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(14.0, 3)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        return _reply("0xd2", "ORDER_STATE_PARTIALLY_FILLED", 3.0, avg="0.14")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["filledSize"] == 3.0
    assert "Filled 3 of 5" in r["message"], r["message"]


@pytest.mark.parametrize("asked", [3.5, 2.5, 97.5])
def test_a_sub_cent_price_goes_out_at_the_price_that_was_confirmed(
        fresh_db, monkeypatch, asked):
    """Near the extremes a market trades in 0.1c ticks. The confirmation, the
    ceilings and the agent's edge check all ran on the snapped price, and the
    order then went out rounded to whole cents: 3.5c as 4c, 97.5c as 98c, and
    (banker's rounding) 2.5c as 2c."""
    _market(monkeypatch, asks=[(99.9, 5000)], bids=[(0.1, 5000)], tick=0.001)
    _balance(monkeypatch, 5000.0)
    sent = []

    async def _place(**kw):
        sent.append(kw)
        return _reply("0xsub", qty=1000.0)

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    req = {"conditionId": "0xabc", "side": "yes", "action": "buy",
           "priceCents": asked, "size": 1000, "orderType": "GTC"}
    pv = _run(tt.preview(req))
    assert pv["priceCents"] == asked and not pv["blockers"], pv
    r = _run(tt.place({**req, "confirmUsd": pv["costUsd"]}))
    assert r["ok"], r["message"]
    assert sent[0]["price_cents"] == asked, \
        f"confirmed {asked}c, sent {sent[0]['price_cents']}c"
    assert f"Resting at {asked:g}¢" in r["message"], r["message"]
    log = tt.order_log()["orders"][0]
    assert log["sentPriceCents"] == asked


@pytest.mark.parametrize("side,want", [("yes", "0.035"), ("no", "0.965")])
def test_the_venue_call_sends_a_sub_cent_price_unrounded(monkeypatch, side, want):
    """place_limit_order snaps to the market's own tick, so a fractional cent on a
    0.001-tick market reaches the wire as is, mirrored to the YES side for NO."""
    import polymarket_api as api
    bodies = []

    async def _meta(ticker):
        return {"tick_size": 0.001, "min_size": 1}

    async def _no_quote(ticker, side):
        return None, None, 100.0

    async def _nothing_open(slugs=None):
        return []

    async def _no_wait():
        return None

    async def _post(method, path, *, json_body=None, **_kw):
        bodies.append((method, path, json_body))
        return {"id": "0xw", "executions": []}

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "_reference_quote", _no_quote)
    monkeypatch.setattr(api, "get_open_orders", _nothing_open)
    monkeypatch.setattr(api, "_throttle", _no_wait)
    monkeypatch.setattr(api, "_authed_request", _post)
    monkeypatch.setattr(api, "_no_side_mismatch", False)
    monkeypatch.setattr(api, "_kill_reason", "")
    monkeypatch.setattr(api, "trading_restriction", lambda: None)
    _run(api.place_limit_order(ticker="cpc-abc", side=side, action="buy", count=1000,
                               price_cents=3.5, order_type="GTC"))
    (method, path, body), = bodies
    assert (method, path) == ("POST", "/v1/orders")
    assert body["price"]["value"] == want


# ────────────────────────────────────────────────────────────────────────
# 2. The ticket knows what the account can spend.
# ────────────────────────────────────────────────────────────────────────


def test_a_buy_over_the_balance_is_blocked_before_it_is_sent(monkeypatch):
    """$2.94 of buying power, a $3.35 order. The exchange refuses this — the
    point is to say so before the user confirms it, in English."""
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 2.94)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 67, "size": 5,
                          "orderType": "FAK"}))
    assert pv["costUsd"] == 3.35
    assert pv["availableUsd"] == 2.94
    assert any("buying power is $2.94" in b and "costs $3.35" in b
               for b in pv["blockers"]), pv["blockers"]


def test_a_buy_inside_the_balance_is_not_blocked(monkeypatch):
    _market(monkeypatch, asks=[(14.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 2.94)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 14, "size": 5,
                          "orderType": "FAK"}))
    assert pv["costUsd"] == 0.70
    assert not pv["blockers"], pv["blockers"]


def test_an_unknown_balance_blocks_nothing_and_says_so(monkeypatch):
    """Refusing an order because a balance READ failed is the same lie in the
    other direction. It warns instead."""
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, None)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 67, "size": 5,
                          "orderType": "FAK"}))
    assert pv["availableUsd"] is None
    assert not pv["blockers"], pv["blockers"]
    assert any("could not read this account's balance" in w
               for w in pv["warnings"]), pv["warnings"]


def test_a_sell_is_never_blocked_on_cash(monkeypatch):
    """A sell brings money IN. Gating it on the cash balance would trap a
    position in exactly the account that most needs to close it."""
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 0.0)

    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "sell", "priceCents": 66, "size": 5,
                          "orderType": "FAK"}))
    assert not pv["blockers"], pv["blockers"]


def test_the_preview_reports_the_touch_and_whether_it_crosses(monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)

    rests = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                             "action": "buy", "priceCents": 14, "size": 5,
                             "orderType": "GTC"}))
    assert rests["bestAskCents"] == 15.0
    assert rests["bestBidCents"] == 13.0
    assert rests["crossesNow"] is False

    crosses = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                               "action": "buy", "priceCents": 15, "size": 5,
                               "orderType": "GTC"}))
    assert crosses["crossesNow"] is True


def test_an_unreadable_book_leaves_crosses_now_unknown(monkeypatch):
    _market(monkeypatch, available=False)
    _balance(monkeypatch, 50.0)
    pv = _run(tt.preview({"conditionId": "0xabc", "side": "yes",
                          "action": "buy", "priceCents": 14, "size": 5,
                          "orderType": "GTC"}))
    assert pv["crossesNow"] is None, "an unread book claimed the order rests"
    assert pv["bestAskCents"] is None


# ────────────────────────────────────────────────────────────────────────
# 3. The exchange's rejections, in English.
# ────────────────────────────────────────────────────────────────────────


def test_a_sell_rejection_explains_the_resting_buy():
    msg = tt.translate_venue_error("insufficient position to sell", action="sell")
    assert msg is not None
    assert "does not hold that position" in msg
    assert "still resting" in msg
    assert "check Track" in msg


def test_a_buy_rejection_names_both_numbers_when_we_know_them():
    msg = tt.translate_venue_error("insufficient buying power", action="buy",
                                   cost_usd=3.35, balance_usd=2.94)
    assert msg == "Your buying power ($2.94) is below this order's cost ($3.35)."


def test_a_buy_rejection_with_no_known_balance_still_names_the_cost():
    msg = tt.translate_venue_error("insufficient buying power", action="buy", cost_usd=3.35)
    assert "below this order's cost ($3.35)" in msg
    assert "$2.94" not in msg, "a balance we never read was invented"


def test_a_region_or_eligibility_refusal_is_said_plainly():
    msg = tt.translate_venue_error("Trading restricted in your region", action="buy")
    assert "where or who it came from" in msg and "Nothing was placed" in msg


def test_an_unknown_rejection_is_passed_through_verbatim():
    """A sentence we cannot translate is shown as-is. Replacing it with
    something vaguer would destroy the only evidence the user has."""
    assert tt.translate_venue_error("some new venue error", action="buy") is None


def test_the_translated_message_reaches_the_ticket_and_the_raw_text_the_log(
        fresh_db, monkeypatch):
    """Both halves of the contract in one test: the user reads English, the
    order log keeps the venue's own words."""
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 2.94)

    async def _place(**kw):
        raise tt.api.PolymarketAPIError(400, "insufficient buying power for this order")

    monkeypatch.setattr(tt.api, "place_limit_order", _place)

    # Sent past the preview's own balance blocker on purpose — this is the
    # placement error path, not the preview one.
    monkeypatch.setattr(tt, "preview", _passthrough_preview(monkeypatch))
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 67, "size": 5, "orderType": "FAK",
                       "confirmUsd": 3.35}))
    assert r["ok"] is False
    assert r["message"] == "Your buying power ($2.94) is below this order's cost ($3.35)."
    log = tt.order_log(10)["orders"]
    assert log and "insufficient buying power" in (log[0]["error"] or ""), \
        "the venue's own words were lost"


@pytest.mark.parametrize("said", [
    "Kill switch is on (too many rejects). Nothing was sent.",
    "Price collar: buying at 67c is more than 5c above the 60c ask. Nothing was sent.",
    # Words the translator would otherwise claim as a venue meaning.
    "Insufficient buying power checks were skipped, so this was refused. Nothing was sent.",
])
def test_the_apps_own_refusal_reaches_the_ticket_as_written(fresh_db, monkeypatch, said):
    """It is already the sentence to read: no "HTTP 422" the venue never sent, and
    no translation into something the venue never said."""
    _market(monkeypatch, asks=[(67.0, 500)], bids=[(66.0, 500)])
    _balance(monkeypatch, 50.0)

    async def _place(**kw):
        raise tt.api.LocalOrderRefusal(422, said)

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 67, "size": 5, "orderType": "FAK",
                       "confirmUsd": 3.35}))
    assert r["ok"] is False and r["status"] == "rejected" and r["filledSize"] == 0.0
    assert r["message"] == said


def test_a_lost_reply_stays_unknown_and_never_prints_the_placeholder(fresh_db, monkeypatch):
    """The exchange has no client id, so a lost reply loses the id too. It may be
    resting or filled. That is never a clean failure, and resending can double it."""
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _balance(monkeypatch, 50.0)
    placeholder = tt.api.UNKNOWN_ID_PREFIX + "abc"
    spawned = []
    monkeypatch.setattr(tt, "_spawn", lambda coro: (spawned.append(1), coro.close()))

    async def _place(**kw):
        raise tt.api.OrderStatusUnknownError(placeholder, RuntimeError("read timeout"))

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    r = _run(tt.place({"conditionId": "0xabc", "side": "yes", "action": "buy",
                       "priceCents": 14, "size": 5, "orderType": "GTC",
                       "confirmUsd": 0.70}))
    assert r["ok"] is False and r["status"] == "unknown" and r["filledSize"] is None
    assert placeholder not in r["message"]
    assert "before sending it again" in r["message"]
    row = tt.order_log(1)["orders"][0]
    assert row["status"] == "unknown"
    assert spawned == [], "a reconcile was started for an id the exchange never gave"


def _passthrough_preview(monkeypatch):
    """`preview` with its balance BLOCKER downgraded, so a test can drive the
    placement path the exchange's own refusal takes."""
    real = tt.preview

    async def _pv(req):
        out = await real(req)
        out["blockers"] = [b for b in out["blockers"] if "buying power" not in b]
        return out
    return _pv


# ────────────────────────────────────────────────────────────────────────
# 4. Working orders, in the shape the panel reads.
# ────────────────────────────────────────────────────────────────────────


def _open_orders(monkeypatch, rows):
    """GET /v1/orders/open."""
    asked = []

    async def _req(method, path, *, params=None, **kw):
        asked.append((method, path, params))
        return {"orders": rows}
    monkeypatch.setattr(tt.api, "_authed_request", _req)
    return asked


def _venue_order(oid, *, slug="0xabc", intent="ORDER_INTENT_BUY_LONG", price="0.14",
                 qty=5.0, cum=0.0, left=None):
    return {"id": oid, "marketSlug": slug, "intent": intent,
            "price": {"value": price, "currency": "USD"},
            "quantity": qty, "cumQuantity": cum,
            "leavesQuantity": (qty - cum) if left is None and qty is not None else left,
            "state": "ORDER_STATE_NEW", "createTime": "2026-09-03T12:00:00Z"}


def test_working_rows_carry_the_remainder_and_the_touch(fresh_db, monkeypatch):
    """What the Market page's Working row prints: 0 of 5 filled, best ask
    15¢. Both come from the venue; neither is derived from our own request."""
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    asked = _open_orders(monkeypatch, [_venue_order("0xdead")])
    tt._record_order({
        "condition_id": "0xabc", "title": "Elon tweets?", "side": "yes",
        "action": "buy", "order_type": "GTC", "requested_size": 5,
        "requested_price_cents": 14, "sent_size": 5, "sent_price_cents": 14,
        "order_id": "0xdead", "status": "placed",
    })

    r = _run(tt.resting_orders("0xabc"))
    assert asked == [("GET", "/v1/orders/open", [("slugs", "0xabc")])]
    o = r["orders"][0]
    assert o["size"] == 5 and o["filledSize"] == 0
    assert o["remainingSize"] == 5
    assert o["priceCents"] == 14.0
    assert o["bestTouchCents"] == 15.0, "a buy's touch must be the best ASK"
    assert o["side"] == "yes" and o["origin"] == "manual"
    assert o["source"] == "venue" and o["title"] == "Elon tweets?"
    assert o["createdAt"] == "2026-09-03T12:00:00Z"


def test_a_working_no_order_reads_on_the_no_side(fresh_db, monkeypatch):
    """The venue quotes every order on the YES side. A NO sell at 51c rests with a
    wire price of 0.49, and its touch is the NO bid: 100 less the YES ask."""
    _market(monkeypatch, asks=[(55.0, 500)], bids=[(49.0, 500)])
    _open_orders(monkeypatch, [_venue_order("0xbeef", intent="ORDER_INTENT_SELL_SHORT",
                                            price="0.49", cum=1.0)])

    o = _run(tt.resting_orders("0xabc"))["orders"][0]
    assert o["side"] == "no" and o["action"] == "sell"
    assert o["priceCents"] == 51.0
    assert o["bestTouchCents"] == 45.0
    assert o["remainingSize"] == 4
    assert o["origin"] == "external", "no row of ours, and it says so"


def test_a_working_sells_touch_is_the_bid(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(55.0, 500)], bids=[(49.0, 500)])
    _open_orders(monkeypatch, [_venue_order("0xb", intent="ORDER_INTENT_SELL_LONG", price="0.51")])
    assert _run(tt.resting_orders("0xabc"))["orders"][0]["bestTouchCents"] == 49.0


def test_other_markets_are_filtered_out(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _open_orders(monkeypatch, [_venue_order("a"), _venue_order("b", slug="0xother")])
    r = _run(tt.resting_orders("0xabc"))
    assert [o["orderId"] for o in r["orders"]] == ["a"]


def test_the_account_wide_list_reads_no_market(fresh_db, monkeypatch):
    """Track's list must not pay for a read per order. No condition id,
    no touch — and null there means "not read", not "no touch"."""
    async def _boom(*a, **kw):
        raise AssertionError("the account-wide list read a market")
    monkeypatch.setattr(tt.term, "book", _boom)
    monkeypatch.setattr(tt.term, "market", _boom)
    asked = _open_orders(monkeypatch, [_venue_order("a")])
    r = _run(tt.resting_orders())
    assert asked == [("GET", "/v1/orders/open", None)]
    assert r["orders"][0]["bestTouchCents"] is None


def test_an_unreadable_size_leaves_the_remainder_unknown(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    row = _venue_order("a", qty=None, cum=0.0)
    row["leavesQuantity"] = None
    _open_orders(monkeypatch, [row])
    o = _run(tt.resting_orders("0xabc"))["orders"][0]
    assert o["remainingSize"] is None, "an unknown size became a remainder of 0"


def test_an_empty_market_list_says_which_emptiness_it_means(
        fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(15.0, 500)], bids=[(13.0, 500)])
    _open_orders(monkeypatch, [])
    assert _run(tt.resting_orders("0xabc"))["note"] == \
        "No resting orders in this market."
    assert _run(tt.resting_orders())["note"] == "No resting orders."


def test_an_unreadable_list_does_not_read_as_no_orders(fresh_db, monkeypatch):
    async def _down(method, path, **kw):
        raise tt.api.PolymarketAPIError(500, "down")
    monkeypatch.setattr(tt.api, "_authed_request", _down)
    r = _run(tt.resting_orders())
    assert r["orders"] == [] and r["note"].startswith("Could not read your open orders")


# ────────────────────────────────────────────────────────────────────────
# 5. A cancel is what the venue says it was.
# ────────────────────────────────────────────────────────────────────────


def _order_row(oid, status, reconciled=0):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO terminal_orders (condition_id, title, side, action, "
            "order_type, requested_size, requested_price_cents, order_id, "
            "status, origin, network, reconciled) "
            "VALUES ('0xabc','T','yes','buy','GTC',10,50,?,?,'manual','mainnet',?)",
            (oid, status, reconciled))


def _row_status(oid):
    with db.get_db() as conn:
        return conn.execute("SELECT status FROM terminal_orders WHERE order_id=?",
                            (oid,)).fetchone()["status"]


def _venue_cancel(monkeypatch, reply):
    """`reply` is what api.cancel_order returns, or raises. Returns the calls."""
    calls = []

    async def _cancel(oid, *, market=""):
        calls.append((oid, market))
        if isinstance(reply, BaseException):
            raise reply
        return reply
    monkeypatch.setattr(tt.api, "cancel_order", _cancel)
    return calls


def test_a_refused_cancel_is_not_reported_as_canceled(fresh_db, monkeypatch):
    """An order that already filled answers with a refusal. It used to read
    "Canceled order …" and overwrite the reconciled 'filled' row with 'canceled',
    for good — and the Reprice button sends its replacement only after a cancel
    that says ok."""
    _order_row("0xfilled", "filled", reconciled=1)
    calls = _venue_cancel(monkeypatch,
                          tt.api.PolymarketAPIError(400, "order already filled"))
    r = _run(tt.cancel("0xfilled"))
    assert r["ok"] is False, r
    assert "order already filled" in r["message"]
    assert r["notCanceled"] == "order already filled"
    assert _row_status("0xfilled") == "filled"
    assert calls == [("0xfilled", "0xabc")], "the exchange needs the order's market"


def test_a_failed_cancel_call_does_not_say_the_exchange_refused(fresh_db, monkeypatch):
    """A 5xx or a dropped connection says nothing about the order, and the Reprice
    path reads `notCanceled` as the venue's own answer."""
    _order_row("0xlive", "placed")
    _venue_cancel(monkeypatch, tt.api.PolymarketAPIError(502, "bad gateway"))
    r = _run(tt.cancel("0xlive"))
    assert r["ok"] is False and "notCanceled" not in r
    assert r["message"].startswith("Cancel failed")
    assert _row_status("0xlive") == "placed"


def test_a_cancel_the_venue_did_not_acknowledge_is_not_a_cancel(fresh_db, monkeypatch):
    _order_row("0xlive", "placed")
    _venue_cancel(monkeypatch, {"canceled": []})
    r = _run(tt.cancel("0xlive"))
    assert r["ok"] is False
    assert _row_status("0xlive") == "placed"


def test_a_confirmed_cancel_marks_the_row(fresh_db, monkeypatch):
    _order_row("0xlive", "placed")
    _venue_cancel(monkeypatch, {"canceled": ["0xlive"]})
    r = _run(tt.cancel("0xlive"))
    assert r["ok"] is True
    assert _row_status("0xlive") == "canceled"


def test_cancelling_a_partly_filled_remainder_keeps_the_fill(fresh_db, monkeypatch):
    """Reconcile marks an order filled on its first fill. Cancelling what is
    left of it takes the remainder off the book; it does not undo the fill."""
    _order_row("0xpart", "filled", reconciled=1)
    _venue_cancel(monkeypatch, {"canceled": ["0xpart"]})
    assert _run(tt.cancel("0xpart"))["ok"] is True
    assert _row_status("0xpart") == "filled"


def test_a_placeholder_id_is_not_sent_to_the_exchange(fresh_db, monkeypatch):
    """A lost reply loses the id too. There is nothing to cancel by, and neither
    "canceled" nor "refused" would be true."""
    oid = tt.api.UNKNOWN_ID_PREFIX + "abc"
    _order_row(oid, "unknown")
    calls = _venue_cancel(monkeypatch, {"canceled": [oid]})
    r = _run(tt.cancel(oid))
    assert r["ok"] is False and calls == []
    assert _row_status(oid) == "unknown"


# ────────────────────────────────────────────────────────────────────────
# The per-market quantity rules are the only minimum this venue documents.
# ────────────────────────────────────────────────────────────────────────


def _pv(**over):
    req = {"conditionId": "0xabc", "side": "yes", "action": "buy",
           "size": 5, "priceCents": 19.0, "orderType": "FAK"}
    req.update(over)
    return _run(tt.preview(req))


def test_there_is_no_dollar_floor_on_a_marketable_buy(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)])
    _balance(monkeypatch, 50.0)
    pv = _pv(size=1)
    assert pv["costUsd"] == 0.19
    assert not pv["blockers"], pv["blockers"]


def test_a_buy_under_the_markets_minimum_is_blocked_not_raised(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)], min_qty=5)
    _balance(monkeypatch, 50.0)
    pv = _pv(size=3)
    assert any("minimum order is 5" in b for b in pv["blockers"]), pv["blockers"]
    assert pv["size"] == 3, "bought more than was asked for"


def test_a_market_with_a_fractional_step_keeps_the_fraction(fresh_db, monkeypatch):
    """minimumTradeQty is 1 or 0.01. On the second, 1.25 contracts is a real order."""
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)], min_qty=0.01)
    _balance(monkeypatch, 50.0)
    pv = _pv(size=1.25)
    assert pv["size"] == 1.25 and not pv["blockers"], pv
    assert not any("rounded" in w for w in pv["warnings"])


def test_a_whole_contract_market_rounds_a_fraction_down_and_says_so(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)])
    _balance(monkeypatch, 50.0)
    pv = _pv(size=2.7)
    assert pv["size"] == 2
    assert any("Size rounded down" in w for w in pv["warnings"]), pv["warnings"]


def test_a_size_below_the_markets_step_is_blocked(fresh_db, monkeypatch):
    _market(monkeypatch, asks=[(19.0, 500)], bids=[(18.0, 500)])
    _balance(monkeypatch, 50.0)
    pv = _pv(size=0.4)
    assert any("smallest order step" in b for b in pv["blockers"]), pv["blockers"]
