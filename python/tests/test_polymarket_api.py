"""Tests for polymarket_api: Gamma market normalisation, the public trades
feed direction logic, and CLOB quote reads.

These pin the parsing that the scanner/trader depend on — Gamma ships
`outcomes`/`outcomePrices`/`clobTokenIds` as JSON-encoded strings, and the
trades feed must be folded into the engine's internal yes/no model.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import polymarket_api as api


def _gamma_raw(**over) -> dict:
    raw = {
        "conditionId": "0xbook", "question": "q", "slug": "s",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.41", "0.59"]),
        "clobTokenIds": json.dumps(["1", "2"]),
    }
    raw.update(over)
    return raw


@pytest.mark.parametrize("bid,ask", [
    (0, 1),            # both sides empty: Gamma's placeholders, not prices
    (None, 0.064),     # bid side empty: Gamma leaves bestBid out entirely
    (0.999, 1),        # ask side empty
    (0.55, 0.50),      # crossed: a garbled pair, not a book
])
def test_normalize_market_falls_back_to_mid_without_a_two_sided_book(bid, ask):
    """Anything short of a real two-sided book keeps the old reading, mid on
    both sides, so no consumer sees a spread that only half exists."""
    over = {"bestAsk": ask}
    if bid is not None:
        over["bestBid"] = bid
    m = api._normalize_market(_gamma_raw(**over))
    assert m["yes_bid_dollars"] == 0.41 and m["yes_ask_dollars"] == 0.41


def test_normalize_market_missing_condition_id_is_none():
    assert api._normalize_market({"question": "no id"}) is None
    assert api._normalize_market(None) is None


def _v2(rows, cursor=None):
    """A Data API v2 body: the `{data, pagination}` envelope."""
    return {"data": rows, "pagination": {"has_more": cursor is not None, "next_cursor": cursor}}


def test_snap_price_to_tick():
    # 0.01 tick: round to the nearest cent.
    assert api._snap_price_to_tick(0.376, 0.01) == 0.38
    # 0.1 tick: a cent-granular price must snap to a dime.
    assert api._snap_price_to_tick(0.37, 0.1) == 0.4
    # 0.001 tick: finer rounding.
    assert api._snap_price_to_tick(0.6234, 0.001) == 0.623
    # Clamp into [tick, 1 - tick].
    assert api._snap_price_to_tick(0.97, 0.1) == 0.9
    assert api._snap_price_to_tick(0.02, 0.1) == 0.1


# ───────── on-chain balance read: parallel RPC race ───────────────────


_TOK = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
_ADDR = "0x7a4043DE8fa7C458eD701Ebe487E7203eeF72197"


# ───────── poisoned-pool self-heal: rebuild client on a failure streak ─────────


def test_net_fail_streak_recycles_client(monkeypatch):
    # A cross-endpoint failure streak (every host down = pool wedged) must rebuild
    # the shared client, so the bot recovers WITHOUT the user restarting the app.
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api, "_last_net_ok", 0.0)  # genuine poison: no success recorded
    # Time well past the throttle gap so the recycle isn't suppressed.
    monkeypatch.setattr(api.time, "monotonic", lambda: 10_000.0)

    async def _run():
        for _ in range(api._NET_FAIL_RECYCLE - 1):
            await api._note_net_fail()
        assert recycled["n"] == 0       # not yet at threshold
        await api._note_net_fail()      # crosses threshold → rebuild
        assert recycled["n"] == 1

    asyncio.run(_run())


def test_net_ok_resets_streak_so_one_bad_endpoint_never_recycles(monkeypatch):
    # The streak resets on ANY success, so an endpoint-specific outage (one host
    # flaky while others answer) keeps clearing it and never triggers a recycle.
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api, "_last_net_ok", 0.0)
    monkeypatch.setattr(api.time, "monotonic", lambda: 10_000.0)

    async def _run():
        for _ in range(api._NET_FAIL_RECYCLE * 3):
            await api._note_net_fail()
            api._note_net_ok()          # a healthy call interleaves each failure
        assert recycled["n"] == 0

    asyncio.run(_run())


def test_recycle_throttled_within_gap(monkeypatch):
    # Even past the threshold, a second rebuild is suppressed inside the min-gap so
    # a genuine multi-minute outage can't churn the client every cycle.
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    clock = {"t": 10_000.0}
    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api, "_last_net_ok", 0.0)  # genuine poison: no success recorded
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])

    async def _run():
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 1                       # first rebuild fired
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 1                       # still throttled
        clock["t"] += api._CLIENT_RECYCLE_MIN_GAP + 1   # gap elapses
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 2                       # now allowed again

    asyncio.run(_run())


def test_recent_success_blocks_recycle(monkeypatch):
    # A slow route that still lands the occasional response must KEEP its warm
    # connections: even a full failure streak must NOT rebuild the pool while a
    # success is recent (within _NET_STALL_SECS). This is what stops a burst of
    # fast-budget timeouts from tearing down TLS and self-amplifying into a storm.
    recycled = {"n": 0}

    async def _fake_recycle():
        recycled["n"] += 1

    clock = {"t": 10_000.0}
    monkeypatch.setattr(api, "_recycle_client", _fake_recycle)
    monkeypatch.setattr(api, "_net_fail_streak", 0)
    monkeypatch.setattr(api, "_last_client_recycle", 0.0)
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])

    async def _run():
        api._note_net_ok()                              # a response just landed
        for _ in range(api._NET_FAIL_RECYCLE * 2):
            await api._note_net_fail()
        assert recycled["n"] == 0                        # recent success → no rebuild
        clock["t"] += api._NET_STALL_SECS + 1           # now a real stall
        for _ in range(api._NET_FAIL_RECYCLE):
            await api._note_net_fail()
        assert recycled["n"] == 1                        # nothing landed for the stall → rebuild

    asyncio.run(_run())


# ───────── _authed_request: stale-creds self-heal ─────────────────────


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


def _wire_authed(monkeypatch, responses, state):
    class _Client:
        async def request(self, *a, **k):
            r = responses[state["i"]]
            state["i"] += 1
            return r

    async def _noop_ensure():
        return None

    async def _fake_client():
        return _Client()

    monkeypatch.setattr(api, "ensure_api_creds", _noop_ensure)
    monkeypatch.setattr(api, "_get_client", _fake_client)
    monkeypatch.setattr(api.auth, "l2_headers", lambda *a, **k: {})
    monkeypatch.setattr(api.auth, "clear_api_creds",
                        lambda env=None: state.__setitem__("cleared", state["cleared"] + 1))


# ───────── place_limit_order: CLOB V2 order-type plumbing ──────────────


def _stub_order_path(monkeypatch, captured):
    """Stub everything place_limit_order touches except the body assembly, and
    capture the POST body so we can assert the V2 order-type fields."""
    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222",
                "tick_size": 0.01, "min_size": 5, "neg_risk": False}

    async def _ensure():
        return None

    async def _authed(method, path, *, json_body=None, idempotent=True, **_kw):
        captured["body"] = json_body
        return {"orderID": "oid-1", "status": "matched", "success": True}

    monkeypatch.setattr(api, "get_market_meta", _meta)
    monkeypatch.setattr(api, "ensure_api_creds", _ensure)
    monkeypatch.setattr(api.auth, "get_api_creds", lambda: {"apiKey": "ak"})
    monkeypatch.setattr(api.auth, "create_signed_order",
                        lambda **kw: {"salt": "1", "maker": "0x", "signature": "0x"})
    # A deposit wallet distinct from the signer: place_limit_order refuses to sign
    # an order whose maker would be a raw EOA.
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0xDeposit")
    monkeypatch.setattr(api.auth, "get_address", lambda: "0xEOAsigner")
    monkeypatch.setattr(api, "_authed_request", _authed)


# ───────── place_limit_order: the venue-facing safety branches ────────────


def _window(monkeypatch, *, restart=0.0, post_only=0.0):
    now = time.time()
    monkeypatch.setattr(api, "_restart_until", now + restart if restart else 0.0)
    monkeypatch.setattr(api, "_post_only_until", now + post_only if post_only else 0.0)


# ───────── verify_order_signer_live: the one-click signer smoke test ──────────


def _wire_signer_probe(monkeypatch, *, sig_type=3, place=None):
    """Stub the deps of verify_order_signer_live and start from an empty cache.
    `place` is the fake place_limit_order (callable or raises)."""
    monkeypatch.setattr(api, "_signer_check_cache", {})
    monkeypatch.setattr(api.auth, "get_signature_type", lambda: sig_type)
    monkeypatch.setattr(api.auth, "get_address", lambda: "0xEOAsigner")
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0xDepositWallet")

    async def _markets(limit=50):
        return [{"ticker": "0xc", "yes_token": "111", "status": "open",
                 "yes_bid_dollars": 0.55}], ""

    async def _meta(_t):
        return {"yes_token": "111", "no_token": "222", "tick_size": 0.01, "min_size": 5}

    monkeypatch.setattr(api, "fetch_markets", _markets)
    monkeypatch.setattr(api, "get_market_meta", _meta)
    if place is not None:
        monkeypatch.setattr(api, "place_limit_order", place)


# ── get_balance: the two legs run concurrently ──────────────────────────────
# The on-chain/CLOB cash read and the Data API /value read are independent.
# They used to run back-to-back, so every account snapshot, fill and sizing
# call paid the sum of both latencies. These pin that they are gathered and
# that the failure semantics of each leg survived the change.

def _wire_balance(monkeypatch, *, cash, value, delay=0.0):
    """Stub both legs of get_balance in deposit-wallet mode.

    `cash` is dollars (or an Exception to raise / None for "all RPCs dead");
    `value` is the raw /value payload (or an Exception to raise).
    """
    monkeypatch.setattr(api.auth, "get_funder", lambda: "0x" + "1" * 40)

    async def _pusd(_addr):
        if delay:
            await asyncio.sleep(delay)
        if isinstance(cash, Exception):
            raise cash
        return cash
    monkeypatch.setattr(api, "_pusd_balance_usd", _pusd)

    async def _get(url, params=None, **_kw):
        if delay:
            await asyncio.sleep(delay)
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(api, "_get", _get)
