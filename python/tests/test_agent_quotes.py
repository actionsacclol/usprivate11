"""Agent and paper marks read the freshest quote there is, not a cached listing.

The market lists are cached at the CDN for up to 30 seconds, and a 15-minute window
moved 11c inside that on the day this was measured. The agent's forecast baseline,
record_forecast's edges, paper marks and the agents' daily loss stop all price off
this. So they read our own feed when it is fresh, else /v1/market/slug/{slug} (the
one public route the CDN does not cache, one request per market on purpose), and
keep the listing's quote only as a fallback that says where it came from and how
old it is.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import db
import forecast_ledger
import mcp_rails
import terminal
import terminal_paper as tp
from test_mcp import (_BUY, _agent_buy, _call, _fake_service, _forecast, _live,
                      _mock_book, _run, _text, fresh)  # noqa: F401 — fixture

_LISTED_AT = "2026-10-11T15:00:00Z"


def _listed_row(cid, *, bid=60.0, ask=62.0):
    src = {"source": "gateway", "observedAt": _LISTED_AT, "ageSec": 9.0}
    return {"conditionId": cid, "title": "T", "status": "open", "resolvedOutcome": None,
            "closeTime": "2030-01-01T00:00:00Z", "yesTokenId": f"{cid}#yes",
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "spread": ask - bid,
            "probability": (bid + ask) / 2,
            "sources": {k: dict(src) for k in ("bid", "ask", "mid", "spread", "probability")},
            "unknowns": []}


def _quote(bid=None, ask=None):
    """The quote fields of a venue market row, in dollars. An empty side is absent."""
    out: dict = {}
    if bid is not None:
        out["bestBidQuote"] = {"value": f"{bid / 100:.2f}", "currency": "USD"}
    if ask is not None:
        out["bestAskQuote"] = {"value": f"{ask / 100:.2f}", "currency": "USD"}
    return out


def _fake_gateway(monkeypatch, quotes, *, feed=None):
    """/v1/market/slug/{slug} served from `quotes` {slug: quote}; returns the call log."""
    calls: list = []

    async def _fetch(host, path, params=None, *, ttl=0.0, timeout=10.0):
        calls.append((host, path))
        slug = path.rsplit("/", 1)[-1]
        if host == "gateway" and path.startswith("/v1/market/slug/") and slug in quotes:
            return {"market": {"slug": slug, **quotes[slug]}}, terminal._now_iso()
        return None, None
    monkeypatch.setattr(terminal, "_fetch", _fetch)
    monkeypatch.setattr(terminal, "_live_quote", lambda tok: (feed or {}).get(tok))
    return calls


# ───────── terminal.clob_quotes ─────────────────────────────────────────


def test_marks_come_from_the_uncached_read(monkeypatch):
    rows = [_listed_row(c) for c in ("cpc-a", "cpc-b", "cpc-c")]
    calls = _fake_gateway(monkeypatch, {c: _quote(20, 22) for c in ("cpc-a", "cpc-b", "cpc-c")})
    asyncio.run(terminal.clob_quotes(rows))
    assert sorted(c[1] for c in calls) == [
        "/v1/market/slug/cpc-a", "/v1/market/slug/cpc-b", "/v1/market/slug/cpc-c"]
    for r in rows:
        assert (r["bid"], r["ask"], r["mid"], r["probability"]) == (20.0, 22.0, 21.0, 21.0)
        assert r["sources"]["bid"]["source"] == "gateway"
        assert r["sources"]["bid"]["observedAt"] != _LISTED_AT
        assert r["sources"]["probability"]["observedAt"] != _LISTED_AT


def test_a_fresh_feed_quote_costs_no_request(monkeypatch):
    rows = [_listed_row("cpc-a"), _listed_row("cpc-b")]
    calls = _fake_gateway(monkeypatch, {"cpc-b": _quote(30, 33)},
                          feed={"cpc-a#yes": {"bid_cents": 40.0, "ask_cents": 41.0}})
    asyncio.run(terminal.clob_quotes(rows))
    assert [c[1] for c in calls] == ["/v1/market/slug/cpc-b"]
    assert (rows[0]["bid"], rows[0]["ask"]) == (40.0, 41.0)
    assert rows[0]["sources"]["ask"]["source"] == "clob-ws"
    assert (rows[1]["bid"], rows[1]["ask"]) == (30.0, 33.0)


def test_an_empty_side_is_unknown_not_the_older_quote(monkeypatch):
    rows = [_listed_row("cpc-a")]
    _fake_gateway(monkeypatch, {"cpc-a": _quote(bid=20)})        # nobody offering
    asyncio.run(terminal.clob_quotes(rows))
    r = rows[0]
    assert r["bid"] == 20.0 and r["ask"] is None and r["mid"] is None
    assert any(u["field"] == "ask" for u in r["unknowns"])
    # No mid to mark at: the probability stays the listing's, and says so.
    assert r["probability"] == 61.0 and r["sources"]["probability"]["observedAt"] == _LISTED_AT


def test_an_unreadable_market_keeps_the_listed_quote_labelled_with_its_age(monkeypatch):
    rows = [_listed_row("cpc-a")]
    _fake_gateway(monkeypatch, {})
    asyncio.run(terminal.clob_quotes(rows))
    assert rows[0]["bid"] == 60.0
    assert rows[0]["sources"]["bid"]["observedAt"] == _LISTED_AT
    assert rows[0]["sources"]["bid"]["ageSec"] is not None


def test_a_settled_market_costs_no_read_its_price_is_the_payout(monkeypatch):
    settled = {**_listed_row("cpc-a"), "status": "settled", "resolvedOutcome": "yes"}
    calls = _fake_gateway(monkeypatch, {"cpc-a": _quote(20, 22)})
    asyncio.run(terminal.clob_quotes([settled]))
    assert calls == [] and settled["bid"] == 60.0


# ───────── the agent consumers ──────────────────────────────────────────


def _summaries_listed(monkeypatch, **kw):
    async def _summaries(ids):
        return [_listed_row(c, **kw) for c in ids]
    monkeypatch.setattr(terminal, "_summaries_for", _summaries)


def test_the_agents_live_loss_stop_marks_at_the_live_bid(fresh, monkeypatch):
    # Bought 10 YES at 50c. The listing's stale bid says 60c (a gain); the market
    # bids 20c: down $3.00.
    _live(monkeypatch)
    with db.get_db() as conn:
        _agent_buy(conn, oid="b1", size=10, filled_shares=10, cid="cpc-down")
    _summaries_listed(monkeypatch)
    _restore_quotes(monkeypatch)
    _fake_gateway(monkeypatch, {"cpc-down": _quote(20, 22)})
    d = _run(mcp_rails.day_loss("live", {}))
    assert d["openLossUsd"] == 3.0, d


_REAL_CLOB_QUOTES = terminal.clob_quotes


def _restore_quotes(monkeypatch):
    """`_mock_book` stubs the re-quote out; these tests are about that read."""
    monkeypatch.setattr(terminal, "clob_quotes", _REAL_CLOB_QUOTES)


def test_paper_marks_and_the_paper_loss_stop_use_the_live_quote(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)
    res = _call("place_order", {**_BUY, "mode": "paper", "orderType": "FAK",
                                "forecastId": _forecast()})
    assert not res["isError"], _text(res)           # 10 YES at 50c
    _summaries_listed(monkeypatch, bid=69.0, ask=71.0)
    _restore_quotes(monkeypatch)
    _fake_gateway(monkeypatch, {"0xabc": _quote(20, 22)})
    with tp.using_book("ai"):
        pf = _run(tp.portfolio({}))
        day = _run(tp.day_pnl(datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    pos = pf["positions"][0]
    assert pos["markCents"] == 21.0
    assert pos["sources"]["probability"]["observedAt"] != _LISTED_AT
    assert day["openLossUsd"] == 3.0                  # 10 x (50c - 20c bid)


def test_a_forecast_is_benchmarked_against_the_live_quote(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)

    async def _market(cid, include_raw=False):
        return _listed_row(cid)
    monkeypatch.setattr(terminal, "market", _market)
    _restore_quotes(monkeypatch)
    _fake_gateway(monkeypatch, {"0xabc": _quote(20, 22)})
    res = _call("record_forecast", {"conditionId": "0xabc", "fairValueCents": 40,
                                    "rationale": "The rules resolve on the official tally."})
    out = json.loads(_text(res))
    assert out["market"]["mid"] == 21.0 and out["market"]["source"] == "gateway"
    assert out["buyYes"]["atPriceCents"] == 22.0
    row = forecast_ledger.get(out["forecastId"])
    assert row["market_mid_cents"] == 21.0 and row["yes_ask_cents"] == 22.0


def test_a_fallback_quote_tells_the_agent_its_age(fresh, monkeypatch):
    _fake_service(monkeypatch)
    _mock_book(monkeypatch)

    async def _market(cid, include_raw=False):
        return _listed_row(cid)
    monkeypatch.setattr(terminal, "market", _market)
    _restore_quotes(monkeypatch)
    _fake_gateway(monkeypatch, {})
    res = _call("record_forecast", {"conditionId": "0xabc", "fairValueCents": 40,
                                    "rationale": "The rules resolve on the official tally."})
    out = json.loads(_text(res))
    assert out["market"]["source"] == "gateway"
    assert out["market"]["ageSec"] is not None


def test_a_feed_quote_says_how_old_it_is(monkeypatch):
    """The feed's quote is served for up to 12 seconds after it last heard about
    the book, and every one of them was stamped ageSec 0."""
    import time

    import clob_ws
    feed = type(clob_ws.feed)()
    feed._books["m#yes"] = {"bid": 0.40, "ask": 0.42, "ts": time.monotonic() - 7.0}
    monkeypatch.setattr(clob_ws, "feed", feed)
    assert 7.0 <= feed.quote_age_sec("m#yes") < 8.0
    assert feed.quote_age_sec("never-seen") is None

    row = terminal._overlay_live(_listed_row("m"))
    for field in ("bid", "ask", "mid", "probability"):
        src = row["sources"][field]
        assert 7.0 <= src["ageSec"] < 8.0, (field, src)
    assert (row["bid"], row["ask"]) == (40.0, 42.0)
    assert 6.0 <= terminal._age_sec(row["sources"]["bid"]["observedAt"]) <= 9.0

    [again] = _run(terminal.clob_quotes([_listed_row("m")]))
    assert again["sources"]["bid"]["source"] == "clob-ws"
    assert 6.0 <= again["sources"]["bid"]["ageSec"] <= 9.0
