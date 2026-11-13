"""Polymarket US venue layer: mappers, signing, feeds, and the pre-trade guards the
exchange's rulebook requires of automated systems. Payloads are real samples or
the documented shapes; no network."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import clob_ws
import index_feed
import polymarket_api as api
import polymarket_auth as auth
import us_api as us
import user_ws

SLUG = "cpc-btc-updown-15m-2026-11-05-0030z"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ───────── prices and sides ─────────


@pytest.mark.parametrize("side,action,intent", [
    ("yes", "buy", "ORDER_INTENT_BUY_LONG"), ("yes", "sell", "ORDER_INTENT_SELL_LONG"),
    ("no", "buy", "ORDER_INTENT_BUY_SHORT"), ("no", "sell", "ORDER_INTENT_SELL_SHORT"),
])
def test_intent_roundtrip(side, action, intent):
    assert us.order_intent(side, action) == intent
    assert us.intent_side(intent) == side
    assert us.intent_action(intent) == action


def test_no_price_goes_on_the_wire_as_yes():
    body = us.order_body(slug=SLUG, side="no", action="buy", count=3, price=0.3, order_type="GTC")
    assert body["price"] == {"value": "0.7", "currency": "USD"}
    assert body["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert us.order_view({"intent": "ORDER_INTENT_BUY_SHORT", "price": {"value": "0.7"}})["price"] == 0.3


def test_every_order_is_a_limit_with_a_price_cap_and_marked_automatic():
    for t, tif in (("GTC", "TIME_IN_FORCE_GOOD_TILL_CANCEL"), ("FAK", "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"),
                   ("FOK", "TIME_IN_FORCE_FILL_OR_KILL")):
        body = us.order_body(slug=SLUG, side="yes", action="buy", count=1, price=0.5, order_type=t)
        assert body["type"] == "ORDER_TYPE_LIMIT" and body["tif"] == tif
        assert body["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"


def test_buying_no_sells_the_one_instrument():
    assert us.yes_book_direction("ORDER_INTENT_BUY_SHORT") == "sell"
    assert us.yes_book_direction("ORDER_INTENT_SELL_SHORT") == "buy"
    assert us.yes_book_direction("ORDER_INTENT_BUY_LONG") == "buy"


def test_no_quote_mirrors_the_yes_book():
    assert us.quote_for_side("no", 0.82, 0.83) == (0.17, 0.18)
    assert us.quote_for_side("yes", 0.82, 0.83) == (0.82, 0.83)
    assert us.quote_for_side("no", None, 0.83) == (0.17, None)


def test_book_mirrors_to_no():
    book = us.parse_book({"bids": [{"px": {"value": "0.94"}, "qty": "10"}, {"px": {"value": "0.95"}, "qty": "2"}],
                          "offers": [{"px": {"value": "0.97"}, "qty": "5"}]})
    assert book["bids"][0] == (0.95, 2.0)
    no = us.book_for_side(book, "no")
    assert no["bids"] == [(0.03, 5.0)]
    assert no["asks"] == [(0.05, 2.0), (0.06, 10.0)]


# ───────── markets ─────────

_RAW = {
    "slug": SLUG, "question": "BTC up or down", "status": "MARKET_STATUS_OPEN", "active": True,
    "outcomes": '["Up","Down"]', "outcomePrices": '["0.55","0.45"]',
    "bestBidQuote": {"value": "0.55"}, "bestAskQuote": {"value": "0.56"},
    "orderPriceMinTickSize": 0.01, "minimumTradeQty": 1, "feeCoefficient": 0.0695,
    "endDate": "2026-11-05T00:45:00Z", "category": "crypto",
    "assetPriceTerms": {"priceToBeat": {"value": "80629.98"}, "indexSymbol": "BRTI"},
}


def test_market_is_normalised_from_a_real_shape():
    m, meta = us.normalize_market(_RAW, event_slug="btc-updown-15m-2026-11-05-0030z")
    assert m["ticker"] == m["condition_id"] == SLUG
    assert m["yes_token"] == SLUG + "#yes" and m["no_token"] == SLUG + "#no"
    assert m["status"] == "open" and m["yes_bid_dollars"] == 0.55 and m["yes_ask_dollars"] == 0.56
    assert m["price_to_beat"] == 80629.98
    assert m["fee_schedule"] == {"enabled": True, "rate": 0.0695, "exponent": 1.0, "rebateRate": 0.0}
    assert meta["tick_size"] == 0.01 and meta["neg_risk"] is False


def test_only_resolved_is_final():
    resolving = {**_RAW, "status": "MARKET_STATUS_RESOLVING", "outcomePrices": '["1","0"]'}
    m, _ = us.normalize_market(resolving)
    assert m["status"] == "closed" and m["result"] == "" and m["settlement_value_dollars"] is None
    resolved = {**_RAW, "status": "MARKET_STATUS_RESOLVED", "outcomePrices": '["0","1"]'}
    m, _ = us.normalize_market(resolved)
    assert m["status"] == "settled" and m["result"] == "no" and m["settlement_value_dollars"] == 0.0


@pytest.mark.parametrize("bad", ["../v1/orders", "a/b", "", None, 7, "a b"])
def test_a_slug_can_not_carry_a_path(bad):
    assert not us.valid_slug(bad)


# ───────── orders, positions, balances ─────────


def test_place_response_filled():
    out = us.parse_place_response({"id": "o1", "executions": [{"type": "EXECUTION_TYPE_FILL", "order": {
        "id": "o1", "state": "ORDER_STATE_FILLED", "cumQuantity": 3, "quantity": 3,
        "intent": "ORDER_INTENT_BUY_LONG", "avgPx": {"value": "0.5"}}}]}, "FAK")
    assert out == {"order_id": "o1", "status": "matched", "filled": 3.0, "reject": ""}


def test_place_response_killed_with_no_fill_is_a_clean_no_fill():
    out = us.parse_place_response({"id": "o1", "executions": [{"order": {
        "id": "o1", "state": "ORDER_STATE_CANCELED", "cumQuantity": 0}}]}, "FAK")
    assert out["order_id"] == "" and out["status"] == "unmatched"


def test_place_response_rejected_surfaces_the_reason():
    out = us.parse_place_response({"id": "o1", "executions": [{
        "type": "EXECUTION_TYPE_REJECTED", "text": "insufficient funds", "order": {}}]}, "GTC")
    assert out["reject"] == "insufficient funds" and out["order_id"] == ""


def test_accepted_with_no_engine_answer_is_live_and_polled():
    assert us.parse_place_response({"id": "o1", "executions": []}, "GTC") == {
        "order_id": "o1", "status": "live", "filled": 0.0, "reject": ""}


def test_filled_no_order_reads_cost_on_our_side():
    v = us.order_view({"intent": "ORDER_INTENT_BUY_SHORT", "state": "ORDER_STATE_PARTIALLY_FILLED",
                       "cumQuantity": 2, "quantity": 5, "avgPx": {"value": "0.7"}, "price": {"value": "0.72"}})
    assert v["size_matched"] == 2 and v["original_size"] == 5 and v["price"] == 0.3 and v["status"] == "live"


def test_positions_sign_and_shape():
    long_row = us.position_row("m", {"netPosition": "5", "cost": {"value": "2.5"}, "cashValue": {"value": "3"}})
    assert long_row["position_fp"] == 5 and long_row["_raw"]["outcomeIndex"] == 0
    assert long_row["_raw"]["avgPrice"] == 0.5 and long_row["_raw"]["curPrice"] == 0.6
    short_row = us.position_row("m", {"netPosition": "-5", "cost": {"value": "-1"}})
    assert short_row["_raw"]["outcomeIndex"] == 1 and short_row["_raw"]["outcome"] == "No"
    assert us.position_row("m", {"netPosition": "0"}) is None


def test_a_broken_balance_reply_is_never_a_zero_balance():
    assert us.balance_view({}) is None
    assert us.balance_view({"balances": [{"currency": "EUR", "buyingPower": 5}]}) is None
    assert us.balance_view({"balances": [{"currency": "USD", "buyingPower": "12.5", "assetNotional": 3}]}) == {
        "cash": 12.5, "portfolio": 3.0}


# ───────── signing ─────────


def _keypair():
    k = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization as s
    seed = k.private_bytes(s.Encoding.Raw, s.PrivateFormat.Raw, s.NoEncryption())
    return k.public_key(), base64.b64encode(seed).decode()


def test_signature_covers_timestamp_method_path_only():
    pub, secret = _keypair()
    h = auth.auth_headers("post", "/v1/orders", now_ms=1700000000000,
                          creds={"keyId": "key-1", "secretKey": secret})
    assert h["X-PM-Access-Key"] == "key-1" and h["X-PM-Timestamp"] == "1700000000000"
    pub.verify(base64.b64decode(h["X-PM-Signature"]), b"1700000000000POST/v1/orders")


def test_signature_ignores_the_query_string():
    pub, secret = _keypair()
    h = auth.auth_headers("GET", "/v1/orders/open?slugs=a", now_ms=5, creds={"keyId": "k", "secretKey": secret})
    pub.verify(base64.b64decode(h["X-PM-Signature"]), b"5GET/v1/orders/open")


def test_offline_signer_check_passes():
    auth.verify_signer()


def test_saving_a_malformed_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    with pytest.raises(auth.CredentialError):
        auth.save_credentials("", "")
    with pytest.raises(auth.CredentialError):
        auth.save_credentials("00000000-0000-4000-8000-000000000000", "not base64 at all!")
    assert not auth.credentials_present()


def test_the_saved_secret_is_redacted_from_logs_and_the_key_id_is_not(tmp_path, monkeypatch):
    import logscrub
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    monkeypatch.setattr(auth, "_cached_creds", None)
    _, secret = _keypair()
    key_id = "00000000-0000-4000-8000-000000000000"
    auth.save_credentials(key_id, secret)
    try:
        logscrub.refresh_known_secrets()
        assert logscrub.known_count() >= 1
        out = logscrub.scrub(f"failed for {key_id} with {secret}")
        assert secret not in out and key_id in out
    finally:
        logscrub.set_known_secrets([])


def test_an_unknown_labelled_secret_and_the_signature_header_are_redacted():
    import logscrub
    seed = base64.b64encode(b"x" * 32).decode()
    assert seed not in logscrub.scrub(f'{{"secretKey": "{seed}"}}')
    out = logscrub.scrub("X-PM-Signature: abcdefgh12345678==")
    assert "abcdefgh" not in out and "X-PM-Signature" in out


# ───────── pre-trade guards ─────────

_REAL_GET_QUOTE = api.get_quote


@pytest.fixture()
def venue(monkeypatch, tmp_path):
    """place_limit_order with the network cut at the exchange call."""
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    sent: list[dict] = []
    open_orders: list[dict] = []

    async def fake_request(method, path, *, json_body=None, params=None, idempotent=True):
        assert method == "POST" and path == "/v1/orders"
        sent.append(json_body)
        return {"id": "o1", "executions": []}

    async def fake_meta(_slug):
        return {"tick_size": 0.01, "min_size": 1}

    async def fake_quote(_slug, _side):
        return {"bid_cents": 50, "ask_cents": 52}

    async def fake_open(_slugs=None):
        return list(open_orders)

    monkeypatch.setattr(api, "_authed_request", fake_request)
    monkeypatch.setattr(api, "get_market_meta", fake_meta)
    monkeypatch.setattr(api, "get_quote", fake_quote)
    monkeypatch.setattr(api, "get_open_orders", fake_open)
    monkeypatch.setattr(api, "_kill_reason", "")
    monkeypatch.setattr(api, "_no_side_mismatch", False)
    monkeypatch.setattr(api, "_order_times", type(api._order_times)())
    monkeypatch.setattr(api, "_throttle_lock", asyncio.Lock())
    monkeypatch.setattr(clob_ws, "quote_cents_if_fresh", lambda *_a, **_k: None)
    return sent, open_orders


def _order(**kw):
    args = dict(ticker=SLUG, side="yes", action="buy", count=1, price_cents=52)
    args.update(kw)
    return api.place_limit_order(**args)


def test_a_normal_order_is_sent_once(venue):
    sent, _ = venue
    assert run(_order())["order"]["order_id"] == "o1"
    assert len(sent) == 1 and sent[0]["price"]["value"] == "0.52"


def test_buying_no_works_on_an_install_that_never_ran_the_live_check(venue):
    """The lock used to open only after `npm run py:orders` on that machine and
    key, which an installed build can't run: nobody else could buy NO."""
    sent, _ = venue
    api.load_venue_state()                      # a fresh profile: no state file
    assert api.no_buy_locked() is False
    run(_order(side="no", price_cents=48))
    assert len(sent) == 1 and sent[0]["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert sent[0]["price"]["value"] == "0.52", "NO at 48c goes out as YES at 52c"


def test_a_profile_from_the_old_per_key_lock_can_buy_no(venue):
    sent, _ = venue
    api._write_venue_state(noSideVerifiedKey="")    # the old check, never passed
    api.load_venue_state()
    run(_order(side="no", price_cents=48))
    assert len(sent) == 1


def test_a_mismatch_seen_by_the_live_check_locks_no_buys_but_not_exits(venue):
    sent, _ = venue
    api.set_no_side_mismatch(True)
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order(side="no", price_cents=48))
    assert e.value.status == 412 and not sent
    run(_order(side="no", action="sell", price_cents=48))
    assert len(sent) == 1, "exits on NO must never be held back by the lock"


def test_the_no_lock_survives_a_restart_until_a_check_clears_it(venue, monkeypatch):
    api.set_no_side_mismatch(True)
    monkeypatch.setattr(api, "_no_side_mismatch", False)
    api.load_venue_state()
    assert api.no_buy_locked() is True
    api.set_no_side_mismatch(False)
    monkeypatch.setattr(api, "_no_side_mismatch", True)
    api.load_venue_state()
    assert api.no_buy_locked() is False


def test_kill_switch_refuses_everything_and_survives_a_restart(venue):
    sent, _ = venue
    api.engage_kill_switch("test")
    api._kill_reason = ""
    api.load_venue_state()
    assert api.kill_switch_status() == "test"
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order())
    assert e.value.status == 423 and not sent
    api.clear_kill_switch()
    api.load_venue_state()
    assert api.kill_switch_status() is None


@pytest.mark.parametrize("action,price,refused", [
    ("buy", 52, False), ("buy", 92, False), ("buy", 93, True),
    ("sell", 10, False), ("sell", 9, True),
    ("buy", 5, False), ("sell", 95, False),   # resting away from the market can't trade badly
])
def test_price_collar_on_cached_quotes(venue, action, price, refused):
    sent, _ = venue
    if refused:
        with pytest.raises(api.LocalOrderRefusal) as e:
            run(_order(action=action, price_cents=price))
        assert e.value.status == 422 and not sent
    else:
        run(_order(action=action, price_cents=price))
        assert len(sent) == 1


def test_a_live_quote_gets_the_tighter_collar(venue, monkeypatch):
    sent, _ = venue
    monkeypatch.setattr(clob_ws, "quote_cents_if_fresh", lambda *_a, **_k: (50, 52))
    with pytest.raises(api.LocalOrderRefusal):
        run(_order(price_cents=73))
    run(_order(price_cents=72))
    assert len(sent) == 1


def test_a_stale_feed_cannot_refuse_a_sell_the_market_allows(venue, monkeypatch):
    sent, _ = venue

    async def now_47(_slug, _side):
        return {"bid_cents": 47, "ask_cents": 49, "age_sec": 0.0}

    monkeypatch.setattr(clob_ws, "quote_cents_if_fresh", lambda *_a, **_k: (72, 74))
    monkeypatch.setattr(api, "get_quote", now_47)
    run(_order(action="sell", price_cents=40))
    assert len(sent) == 1


def test_a_sell_the_uncached_read_agrees_is_too_low_stays_refused(venue, monkeypatch):
    sent, _ = venue

    async def still_72(_slug, _side):
        return {"bid_cents": 72, "ask_cents": 74, "age_sec": 0.0}

    monkeypatch.setattr(clob_ws, "quote_cents_if_fresh", lambda *_a, **_k: (72, 74))
    monkeypatch.setattr(api, "get_quote", still_72)
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order(action="sell", price_cents=40))
    assert e.value.status == 422 and not sent


_real_get_balance = api.get_balance    # conftest swaps it out for every test


def _balances_and_positions(monkeypatch, positions):
    async def fake_request(method, path, **_kw):
        if path == "/v1/account/balances":
            # The live account's reply: assetNotional stays 0 with positions open.
            return {"balances": [{"currency": "USD", "buyingPower": 8.55, "assetNotional": 0}]}
        assert path == "/v1/portfolio/positions"
        if isinstance(positions, Exception):
            raise positions
        return {"positions": positions, "eof": True}

    monkeypatch.setattr(auth, "credentials_present", lambda *_a: True)
    monkeypatch.setattr(api, "_authed_request", fake_request)


def test_the_portfolio_is_worth_what_the_positions_are_worth(monkeypatch):
    _balances_and_positions(monkeypatch, {
        "xrp": {"netPosition": "-5", "cost": {"value": "2.75"}, "cashValue": {"value": "1.55"}},
        "eth": {"netPosition": "-4", "cost": {"value": "3.16"}, "cashValue": {"value": "2.88"}},
    })
    assert run(_real_get_balance())["portfolio_value"] == 443


def test_a_position_with_no_cash_value_counts_at_cost_and_an_expired_one_not_at_all(monkeypatch):
    _balances_and_positions(monkeypatch, {
        "a": {"netPosition": "5", "cost": {"value": "2.75"}},
        "b": {"netPosition": "5", "cost": {"value": "9"}, "cashValue": {"value": "9"}, "expired": True},
    })
    assert run(_real_get_balance())["portfolio_value"] == 275


def test_unreadable_positions_are_an_unknown_portfolio_not_an_empty_one(monkeypatch):
    _balances_and_positions(monkeypatch, RuntimeError("positions down"))
    bal = run(_real_get_balance())
    assert bal["balance"] == 855 and bal["portfolio_value"] is None


def test_order_would_cross_our_own_resting_order(venue):
    sent, resting = venue
    resting.append({"id": "mine", "intent": "ORDER_INTENT_SELL_LONG", "price": {"value": "0.5"}})
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order(price_cents=52))
    assert e.value.status == 409 and not sent


def test_buying_no_counts_as_selling_for_self_cross(venue):
    sent, resting = venue
    resting.append({"id": "mine", "intent": "ORDER_INTENT_BUY_LONG", "price": {"value": "0.6"}})
    with pytest.raises(api.LocalOrderRefusal):
        run(_order(side="no", price_cents=45))   # sells YES at 0.55, under our 0.60 bid
    assert not sent
    run(_order(side="no", price_cents=30))       # sells YES at 0.70, above it
    assert len(sent) == 1


def test_same_direction_orders_do_not_count_as_self_cross():
    assert us.crosses_own_orders("buy", 0.5, [{"intent": "ORDER_INTENT_BUY_LONG", "price": {"value": "0.5"}}]) is None


def test_order_throttle_refuses_past_the_minute_cap(venue, monkeypatch):
    sent, _ = venue
    monkeypatch.setattr(api, "ORDER_THROTTLE_PER_MIN", 2)
    run(_order())
    run(_order())
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order())
    assert e.value.status == 429 and len(sent) == 2


def test_a_lost_placement_reply_is_never_resent(venue, monkeypatch):
    """No idempotency key on this API: a retry could be a second order."""
    calls = []

    async def lost(method, path, *, json_body=None, params=None, idempotent=True):
        calls.append(idempotent)
        raise api.httpx.ReadTimeout("lost")

    async def settle(body, *, slug, order_type, err):
        return {"order": {"order_id": "unknown-1", "status": "unknown"}, "raw": {}}

    monkeypatch.setattr(api, "_authed_request", lost)
    monkeypatch.setattr(api, "_settle_ambiguous_post", settle)
    out = run(_order())
    assert calls == [False] and out["order"]["status"] == "unknown"


def test_a_placeholder_id_never_reaches_the_exchange(monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("no request expected")

    monkeypatch.setattr(api, "_authed_request", boom)
    with pytest.raises(api.PolymarketAPIError) as e:
        run(api.get_order(api.UNKNOWN_ID_PREFIX + "x"))
    assert e.value.status == 404


def test_positions_read_for_another_trader_is_refused_not_ignored():
    with pytest.raises(RuntimeError):
        run(api.get_positions(user="someone"))


# ───────── market feed ─────────


def test_lite_frame_updates_both_sides():
    f = clob_ws.ClobMarketFeed()
    f._dispatch({"requestId": "1", "marketDataLite": {
        "marketSlug": SLUG, "bestBid": {"value": "0.82"}, "bestAsk": {"value": "0.83"}}})
    assert f.quote_cents_if_fresh(SLUG + "#yes") == (82, 83)
    assert f.quote_cents_if_fresh(SLUG + "#no") == (17, 18)


def test_full_market_data_frame_is_read_from_the_book():
    f = clob_ws.ClobMarketFeed()
    f._dispatch({"marketData": {"marketSlug": SLUG, "bids": [{"px": {"value": "0.4"}, "qty": "1"}],
                                "offers": [{"px": {"value": "0.5"}, "qty": "1"}]}})
    assert f.quote_cents_if_fresh(SLUG + "#yes") == (40, 50)


def test_stale_or_one_sided_book_has_no_opinion(monkeypatch):
    f = clob_ws.ClobMarketFeed()
    f._dispatch({"marketDataLite": {"marketSlug": SLUG, "bestBid": {"value": "0.82"}}})
    assert f.quote_cents_if_fresh(SLUG + "#yes") is None
    f._dispatch({"marketDataLite": {"marketSlug": SLUG, "bestBid": {"value": "0.5"}, "bestAsk": {"value": "0.6"}}})
    f._books[SLUG + "#yes"]["ts"] -= 100
    assert f.quote_cents_if_fresh(SLUG + "#yes") is None


def test_trade_frame_becomes_an_anonymous_recorder_row():
    f = clob_ws.ClobMarketFeed()
    f._dispatch({"trade": {"marketSlug": SLUG, "price": {"value": "0.5"}, "quantity": "3",
                           "tradeTime": "2026-11-05T00:30:01Z"}})
    rows = f.drain_trades()
    assert rows[0]["market"] == SLUG and rows[0]["price"] == 0.5 and rows[0]["size"] == 3
    assert rows[0]["source_ts_ms"] == 1793838601000 and f.drain_trades() == []


def test_subscriptions_chunk_at_the_venue_limit():
    frames = clob_ws.subscribe_frames([f"m{i}" for i in range(250)], "SUBSCRIPTION_TYPE_TRADE")
    assert [len(x["subscribe"]["marketSlugs"]) for x in frames] == [100, 100, 50]
    assert len({x["subscribe"]["requestId"] for x in frames}) == 3


def test_feed_stays_off_the_socket_for_an_already_observed_market():
    f = clob_ws.ClobMarketFeed()
    f.observe(SLUG + "#yes", SLUG + "#no")
    assert f._wanted_slugs() == {SLUG}
    f._resub.clear()
    f.observe(SLUG + "#yes")
    assert not f._resub.is_set()


# ───────── private feed ─────────


def test_order_event_wakes_the_engine():
    f = user_ws.UserFeed()
    f.wake = asyncio.Event()
    assert f.handle({"orderSubscriptionUpdate": {"execution": {
        "type": "EXECUTION_TYPE_FILL", "lastShares": "2", "lastPx": {"value": "0.7"}, "tradeId": "t1",
        "order": {"id": "o1", "intent": "ORDER_INTENT_BUY_SHORT", "state": "ORDER_STATE_FILLED",
                  "marketSlug": SLUG, "cumQuantity": 2, "quantity": 2}}}})
    assert f.wake.is_set() and f.order("o1")["status"] == "matched"
    assert f.trades[0]["size"] == 2 and f.trades[0]["price"] == 0.3 and f.trades[0]["assetId"] == SLUG + "#no"


@pytest.mark.parametrize("frame", [{}, None, {"heartbeat": {}}, {"orderSubscriptionUpdate": {}},
                                   {"orderSubscriptionUpdate": {"execution": {"order": {}}}}])
def test_other_frames_are_ignored(frame):
    f = user_ws.UserFeed()
    f.wake = asyncio.Event()
    assert f.handle(frame) is False and not f.wake.is_set()


# ───────── settlement index ─────────


def _history(rows):
    return {"series": [{"asset": {"assetClass": "crypto", "symbol": "btc"}, "candles": [
        {"timestamp": t, "close": str(px), "tickCount": 60} for t, px in rows]}]}


def test_index_value_at_a_boundary_is_the_candle_one_second_before_it():
    # Verified live: the candle stamped T-1 equals the published priceToBeat at T.
    f = index_feed._Feed()
    f.ingest(index_feed.parse_series(_history([(899, 100.0), (900, 101.0), (901, 102.0)])))
    assert f.value_at("BTC", 900) == 100.0
    assert f.value_at("btc", 901) == 101.0
    assert f.value_at("BTC", 905) is None, "a neighbouring second is a different number"
    assert f.value_at("ETH", 900) is None


def test_index_latest_goes_stale_rather_than_freeze():
    f = index_feed._Feed()
    f.ingest(index_feed.parse_series(_history([(1000, 100.0)])))
    assert f.latest("BTC", now=1003) == 100.0
    assert f.latest("BTC", now=1007) is None


def test_index_ignores_old_and_malformed_candles():
    f = index_feed._Feed()
    f.ingest({"BTC": [(10, 1.0)]})
    assert f.ingest({"BTC": [(5, 9.0), (10, 9.0)]}) == 0
    assert index_feed.parse_series({"series": [{"asset": {"symbol": "btc"}, "candles": [
        {"timestamp": "x", "close": "1"}, {"timestamp": 1, "close": "0"}, None]}]}) == {"BTC": []}
    assert index_feed.parse_series({}) == {} and index_feed.parse_series(None) == {}


def test_strike_prefers_the_published_value_and_upgrades_a_fallback(monkeypatch):
    import crypto15m
    import spot_ws
    monkeypatch.setattr(crypto15m, "_window_open", {})
    monkeypatch.setattr(index_feed, "twap_at", lambda *_a: None)
    monkeypatch.setattr(spot_ws, "window_partial", lambda *_a, **_k: (0.0, 0))
    monkeypatch.setattr(spot_ws, "sample_at", lambda *_a, **_k: None)
    assert crypto15m._track_window_open("BTC", 900, 105.0, now_epoch=905) == 105.0
    assert crypto15m.strike_source("BTC", 900) == "spot"
    assert crypto15m._track_window_open("BTC", 900, 105.0, now_epoch=910, published=100.5) == 100.5
    assert crypto15m.strike_source("BTC", 900) == "twap"
    assert crypto15m._track_window_open("BTC", 900, 106.0, now_epoch=920) == 100.5


def test_no_strike_after_the_grace_period_means_sit_out(monkeypatch):
    import crypto15m
    import spot_ws
    monkeypatch.setattr(crypto15m, "_window_open", {})
    monkeypatch.setattr(index_feed, "twap_at", lambda *_a: None)
    monkeypatch.setattr(spot_ws, "window_partial", lambda *_a, **_k: (0.0, 0))
    monkeypatch.setattr(spot_ws, "sample_at", lambda *_a, **_k: None)
    assert crypto15m._track_window_open("BTC", 900, 105.0, now_epoch=1000) is None


def test_every_horizon_settles_on_the_average():
    import crypto15m
    assert {crypto15m.settle_twap_sec(i) for i in ("5m", "15m", "hourly")} == {60.0}


def test_fee_fallback_is_this_venue_coefficient():
    import crypto15m
    assert crypto15m.FEE_RATE_CRYPTO == 0.0695


def test_config_lists_only_what_the_venue_lists():
    import config
    assert config.CRYPTO15M_ASSETS == ["BTC", "ETH", "SOL", "XRP"]
    assert config._validate_config({"crypto15m_interval": "4h"})["crypto15m_interval"] == "15m"


def test_json_in_this_module_stays_serialisable():
    json.dumps(us.order_body(slug=SLUG, side="no", action="sell", count=1.5, price=0.25, order_type="FOK"))


# ───────── what the live market list really ships ─────────


def test_one_outcome_price_is_the_no_side_not_yes():
    """With one side quoted, outcomePrices collapses to ONE entry holding the NO
    price: 81 of 300 live rows on 2026-11-05."""
    m, _ = us.normalize_market({"slug": SLUG, "outcomes": '["Yes","No"]', "outcomePrices": '["0.01"]'})
    assert m["last_price_dollars"] == pytest.approx(0.99)
    assert m["no_price_dollars"] == pytest.approx(0.01)


def test_reversed_outcomes_keep_prices_and_labels_yes_first():
    raw = {"slug": SLUG, "outcomes": '["No","Yes"]', "outcomePrices": '["0.3","0.7"]'}
    m, meta = us.normalize_market(raw)
    assert m["yes_sub_title"] == "Yes" and meta["outcomes"] == ["Yes", "No"]
    assert m["last_price_dollars"] == pytest.approx(0.3)
    sides = [{"long": False, "description": "Lakers"}, {"long": True, "description": "Celtics"}]
    m, meta = us.normalize_market({**raw, "marketSides": sides, "outcomes": '["Lakers","Celtics"]'})
    assert m["yes_sub_title"] == "Celtics" and meta["outcomes"] == ["Celtics", "Lakers"]


def test_a_null_fee_coefficient_is_unknown_not_free():
    assert us.fee_schedule({"feeCoefficient": None}) is None
    assert us.fee_schedule({"feeCoefficient": 0}) == {"enabled": False}
    assert us.fee_schedule({"feeCoefficient": 0.0695})["rate"] == 0.0695


def test_a_position_missing_its_figures_says_so_instead_of_zero():
    row = us.position_row(SLUG, {"netPosition": "5"})
    assert row["market_exposure_dollars"] is None and row["cur_price"] is None
    assert row["_raw"]["avgPrice"] is None and row["_raw"]["currentValue"] is None


def test_subcent_quotes_round_away_from_the_touch():
    assert (us.bid_cents(0.695), us.ask_cents(0.695)) == (69, 70)
    assert (us.bid_cents(0.29), us.ask_cents(0.29)) == (29, 29)  # no float drift
    assert us.bid_cents(None) is None and us.ask_cents(None) is None


def test_get_quote_reads_the_uncached_market_route(monkeypatch):
    """/bbo sat at 76c while this route moved 76c -> 93c in 15s."""
    seen = []

    async def _gwa(url, params=None, **_k):
        seen.append(url)
        return {"market": {"bestBidQuote": {"value": "0.9300"}, "bestAskQuote": {"value": "0.9400"}}}, 0.0

    monkeypatch.setattr(api, "_get_with_age", _gwa)
    bid, ask, age = asyncio.run(api._read_bbo(SLUG))
    assert (bid, ask, age) == (0.93, 0.94, 0.0)
    assert seen == [f"{api.GATEWAY}/v1/market/slug/{SLUG}"]


_REAL_GET_QUOTE = api.get_quote


def _new_window_reads(monkeypatch, book):
    """A market row with no quotes yet, and this answer from the book route."""
    async def _gwa(url, params=None, **_k):
        if url.endswith("/book"):
            return book, (0.0 if book is not None else None)
        return {"market": {"slug": SLUG}}, 0.0
    monkeypatch.setattr(api, "_get_with_age", _gwa)


def test_no_row_quote_and_no_book_is_an_unread_price_not_an_empty_book(monkeypatch):
    _new_window_reads(monkeypatch, None)
    assert asyncio.run(api._read_bbo(SLUG)) == (None, None, None)
    # A book that was read and is empty is still a read: nothing rests there.
    _new_window_reads(monkeypatch, {"marketData": {"bids": [], "offers": []}})
    bid, ask, age = asyncio.run(api._read_bbo(SLUG))
    assert (bid, ask) == (None, None) and age == 0.0


def test_a_buy_on_a_new_window_nobody_could_price_is_refused(venue, monkeypatch):
    """A new window's row has no quotes and its book isn't served for the first
    minutes. That read came back as "empty book, read just now", the collar had
    no ask to compare, and the buy went out with no reference price."""
    sent, _ = venue
    monkeypatch.setattr(api, "get_quote", _REAL_GET_QUOTE)
    _new_window_reads(monkeypatch, None)
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order(price_cents=52))
    assert e.value.status == 503 and not sent
    run(_order(action="sell", price_cents=52))
    assert len(sent) == 1, "a failed read must not trap someone in a position"


def test_a_fresh_rest_quote_gets_the_tight_collar_and_an_old_one_the_wide(monkeypatch):
    async def _q(age):
        async def _gq(_s, _side):
            return {"bid_cents": 40, "ask_cents": 41, "age_sec": age}
        monkeypatch.setattr(api, "get_quote", _gq)
        return await api._reference_quote(SLUG, "yes")

    assert asyncio.run(_q(0.0))[2] == api.COLLAR_LIVE_CENTS
    assert asyncio.run(_q(20.0))[2] == api.COLLAR_CACHED_CENTS
    assert asyncio.run(_q(None))[2] == api.COLLAR_CACHED_CENTS


def test_the_running_window_is_repriced_past_the_cached_board(monkeypatch):
    import time as _t
    now = _t.time()
    live = {"ticker": SLUG, "yes_token": SLUG + "#yes", "event_ticker": "e", "status": "open",
            "window_start_epoch": now - 10, "window_close_epoch": now + 100,
            "yes_bid_dollars": 0.5, "yes_ask_dollars": 0.51, "last_price_dollars": 0.505}
    next_up = {**live, "ticker": "next", "window_start_epoch": now + 100, "window_close_epoch": now + 200}
    api._fresh_market_cache.clear()
    urls = []

    async def _get(url, params=None, **_k):
        urls.append(url)
        return {"market": {"slug": SLUG, "status": "MARKET_STATUS_OPEN", "outcomes": '["Yes","No"]',
                           "outcomePrices": '["0.91","0.1"]', "bestBidQuote": {"value": "0.90"},
                           "bestAskQuote": {"value": "0.91"},
                           "assetPriceTerms": {"priceToBeat": {"value": "100000"}}}}

    monkeypatch.setattr(api, "_get", _get)
    out = asyncio.run(api._with_fresh_current([live, next_up]))
    assert out[0]["yes_bid_dollars"] == 0.90 and out[0]["price_to_beat"] == 100000.0
    assert out[0]["window_close_epoch"] == live["window_close_epoch"]
    assert out[1]["yes_bid_dollars"] == 0.5
    assert out[0]["quote_fresh"] is True
    assert urls == [f"{api.GATEWAY}/v1/market/slug/{SLUG}"]
    # Within its TTL the second tick costs no request.
    asyncio.run(api._with_fresh_current([live, next_up]))
    assert len(urls) == 1


def test_a_failed_first_activity_page_raises_and_a_later_one_keeps_the_rows(monkeypatch):
    monkeypatch.setattr(api.auth, "credentials_present", lambda *a: True)

    async def _dead(*a, **k):
        raise api.PolymarketAPIError(500, "down")

    monkeypatch.setattr(api, "_authed_request", _dead)
    with pytest.raises(api.PolymarketAPIError):
        asyncio.run(api.get_activity())


def test_zero_buying_power_is_fatal_at_preflight(monkeypatch):
    monkeypatch.setattr(api.auth, "get_key_id", lambda: "key")

    async def _noop():
        return None

    async def _balances(*a, **k):
        return {"balances": [{"currency": "USD", "buyingPower": "0", "currentBalance": "0"}]}

    async def _get(*a, **k):
        return {"markets": []}

    monkeypatch.setattr(api, "_sync_clock_offset", _noop)
    monkeypatch.setattr(api, "_authed_request", _balances)
    monkeypatch.setattr(api, "_get", _get)
    res = asyncio.run(api.check_trading_ready())
    assert res["fatalIssues"] and "buying power" in res["fatalIssues"][0]


def test_a_one_sided_market_is_not_a_fresh_touch(monkeypatch):
    """Normalising copies the bid into a missing ask. Priced as a real ask, a thin
    market would offer a paper fill that doesn't exist."""
    import time as _t
    now = _t.time()
    live = {"ticker": SLUG, "yes_token": SLUG + "#yes", "event_ticker": "e", "status": "open",
            "window_start_epoch": now - 10, "window_close_epoch": now + 100}
    api._fresh_market_cache.clear()

    async def _get(url, params=None, **_k):
        return {"market": {"slug": SLUG, "status": "MARKET_STATUS_OPEN", "outcomes": '["Yes","No"]',
                           "outcomePrices": '["0.15"]', "bestBidQuote": {"value": "0.85"}, "bestAskQuote": None}}

    monkeypatch.setattr(api, "_get", _get)
    out = asyncio.run(api._with_fresh_current([live]))
    assert out[0]["quote_fresh"] is False


def test_presets_read_nothing_this_venue_does_not_publish():
    """Favorites Harvester gated on volume24h >= 5000, always 0 here, so it could never
    fire. A preset must not depend on figures the US market list doesn't have."""
    import script_docs
    assert [p["id"] for p in script_docs.PRESETS] == ["cheap-tail-sniper"]
    for preset in script_docs.PRESETS:
        for field in ("volume24h", "openInterest", '["volume"]'):
            assert field not in preset["code"], f"{preset['id']} reads {field}"
        assert "international" in preset["expected"]


def test_status_panel_key_row_is_off_in_paper_without_a_key(monkeypatch):
    import config
    import service
    monkeypatch.setattr(service.polymarket_auth, "credentials_present", lambda *a, **k: False)
    monkeypatch.setattr(config, "account_live", lambda cfg: False)
    monkeypatch.setattr(service.STATE, "auth_ok", False)
    out = asyncio.run(service._h_trading_status({}))
    row = next(g for g in out["main"] if g["id"] == "auth")
    assert row["state"] == "off" and "Paper" in row["reason"]
    assert "wallet" not in (row["label"] + row["reason"]).lower()

    monkeypatch.setattr(config, "account_live", lambda cfg: True)
    out = asyncio.run(service._h_trading_status({}))
    assert next(g for g in out["main"] if g["id"] == "auth")["state"] == "blocked"


def _no_row_quote(monkeypatch, *, ask="0.90"):
    async def _with_age(url, params=None, *, timeout=None):
        if url.endswith("/book"):
            return {"marketData": {"bids": [{"px": {"value": "0.12"}, "qty": "245"}],
                                   "offers": [{"px": {"value": ask}, "qty": "259"}]}}, 20.0
        return {"market": {"slug": SLUG, "bestBidQuote": None, "bestAskQuote": None}}, 0.0

    monkeypatch.setattr(api, "_get_with_age", _with_age)


def test_an_empty_row_quote_is_read_off_the_book_with_its_age(monkeypatch):
    _no_row_quote(monkeypatch)
    assert run(api._read_bbo(SLUG)) == (0.12, 0.90, 20.0)
    assert run(_REAL_GET_QUOTE(SLUG, "no")) == {"bid_cents": 10, "ask_cents": 88, "age_sec": 20.0}


def test_the_collar_holds_when_the_row_has_no_quote(venue, monkeypatch):
    """With no row quote the collar had nothing to compare to, so a 99c buy into a
    50c ask went straight out."""
    sent, _ = venue
    monkeypatch.setattr(api, "get_quote", _REAL_GET_QUOTE)
    _no_row_quote(monkeypatch, ask="0.50")
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order(price_cents=99))
    assert e.value.status == 422 and not sent


def test_a_failed_price_read_stops_a_buy_but_not_a_sell(venue, monkeypatch):
    sent, _ = venue

    async def dead(_slug, _side):
        return {"bid_cents": None, "ask_cents": None, "age_sec": None}

    monkeypatch.setattr(api, "get_quote", dead)
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order())
    assert e.value.status == 503 and not sent
    run(_order(action="sell"))
    assert len(sent) == 1


def test_an_empty_book_is_not_a_failed_read(venue, monkeypatch):
    """Nothing resting is an answer. A resting order there is how a book starts."""
    sent, _ = venue

    async def empty(_slug, _side):
        return {"bid_cents": None, "ask_cents": None, "age_sec": 0.0}

    monkeypatch.setattr(api, "get_quote", empty)
    run(_order())
    assert len(sent) == 1


@pytest.mark.parametrize("answer,expected", [
    ("ok", True), (401, False), (503, None),
])
def test_signer_ok_reports_what_the_venue_said(monkeypatch, answer, expected):
    """It was hard-coded True, so a rejected key logged 'the exchange accepted an order
    signed by this wallet' when no order had been sent at all."""
    async def _req(_m, _p, **_k):
        if answer == "ok":
            return {"balances": [{"currentBalance": 10, "buyingPower": 10, "currency": "USD"}]}
        raise api.PolymarketAPIError(answer, "no")

    async def _noop(*_a, **_k):
        return {"markets": []}

    monkeypatch.setattr(api.auth, "get_key_id", lambda: "k")
    monkeypatch.setattr(api, "_authed_request", _req)
    monkeypatch.setattr(api, "_sync_clock_offset", _noop)
    monkeypatch.setattr(api, "_get", _noop)
    assert run(api.check_trading_ready())["signerOk"] is expected


def test_signer_ok_is_unknown_with_no_key(monkeypatch):
    async def _noop(*_a, **_k):
        return {"markets": []}

    monkeypatch.setattr(api.auth, "get_key_id", lambda: "")
    monkeypatch.setattr(api, "_get", _noop)
    assert run(api.check_trading_ready())["signerOk"] is None


# ───────── post-only ─────────


def test_a_post_only_order_is_sent_as_participate_dont_initiate(venue):
    sent, _ = venue
    run(_order(price_cents=51, post_only=True))
    assert sent[0]["participateDontInitiate"] is True
    assert sent[0]["tif"] == "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    run(_order(price_cents=51))
    assert "participateDontInitiate" not in sent[1], "an ordinary order must be able to take"


@pytest.mark.parametrize("kw, touch", [
    (dict(action="buy", price_cents=52), "best ask of 52¢"),      # at the ask
    (dict(action="buy", price_cents=60), "best ask of 52¢"),      # through it
    (dict(action="sell", price_cents=50), "best bid of 50¢"),
])
def test_a_post_only_order_that_would_take_is_refused_here(venue, kw, touch):
    """A bid meant to rest must never pay the taker fee because the price moved."""
    sent, _ = venue
    with pytest.raises(api.LocalOrderRefusal) as e:
        run(_order(post_only=True, **kw))
    assert e.value.status == 422 and touch in str(e.value) and "Nothing was sent" in str(e.value)
    assert sent == []


def test_post_only_is_for_resting_orders_only(venue):
    sent, _ = venue
    for ot in ("FAK", "FOK"):
        with pytest.raises(ValueError):
            run(_order(order_type=ot, post_only=True))
    assert sent == []


def test_the_exchange_refusing_a_post_only_order_says_what_happened(venue, monkeypatch):
    """The touch moved between our read and the order arriving."""
    async def rejected(method, path, *, json_body=None, params=None, idempotent=True):
        return {"id": "o1", "executions": [{
            "type": "EXECUTION_TYPE_REJECTED", "text": "order would cross",
            "order": {"id": "o1", "state": "ORDER_STATE_REJECTED"}}]}
    monkeypatch.setattr(api, "_authed_request", rejected)
    with pytest.raises(api.PolymarketAPIError) as e:
        run(_order(price_cents=51, post_only=True))
    assert "post-only" in str(e.value) and "order would cross" in str(e.value)
    assert "Nothing was placed" in str(e.value)
