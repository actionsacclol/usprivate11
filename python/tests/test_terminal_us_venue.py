"""What the terminal reads off Polymarket US, pinned against the traps seen live.

Every raw row here copies a shape the public gateway returned on 2026-11-05. The
fields look like plain data and several of them are not: `outcomes` can be reversed
while the prices stay YES-first, `outcomePrices` can hold one entry, `question` can
be the event's title on every leg, and list responses are up to 30 seconds old.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import terminal as t


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


SIDES = [
    {"description": "Lakers", "long": True, "tradable": True},
    {"description": "Celtics", "long": False, "tradable": True},
]


def _raw(**over):
    raw = {
        "slug": "aec-nba-lal-bos-2026-11-05", "question": "Lakers vs. Celtics",
        "active": True, "closed": False, "status": "MARKET_STATUS_OPEN",
        "orderPriceMinTickSize": 0.01, "minimumTradeQty": 1, "feeCoefficient": 0.0695,
        "bestBidQuote": {"value": "0.40", "currency": "USD"},
        "bestAskQuote": {"value": "0.42", "currency": "USD"},
        "outcomePrices": '["0.41", "0.59"]',
    }
    raw.update(over)
    return raw


# ── the fields that lie ─────────────────────────────────────────────────


def test_reversed_outcomes_do_not_flip_a_settled_market():
    """19 of 107 Yes/No rows in one live page shipped `["No","Yes"]` while their prices
    stayed YES-first. Zipping the two called a market that settled NO a YES win."""
    m = t.summary_from_gamma(_raw(
        status="MARKET_STATUS_RESOLVED", active=False, closed=True,
        outcomes='["No", "Yes"]', outcomePrices='["0", "1"]'))
    assert m["resolvedOutcome"] == "no"
    assert m["outcomes"] == ["Yes", "No"]


def test_side_labels_come_off_the_long_flag_not_the_list_order():
    m = t.summary_from_gamma(_raw(
        marketSides=list(reversed(SIDES)), outcomes='["Celtics", "Lakers"]'))
    assert m["outcomes"] == ["Lakers", "Celtics"]


def test_one_price_is_not_a_settlement():
    m = t.summary_from_gamma(_raw(
        status="MARKET_STATUS_RESOLVED", active=False, closed=True, outcomePrices='["1"]'))
    assert m["resolvedOutcome"] is None


def test_a_row_with_one_price_still_reads_its_quote():
    m = t.summary_from_gamma(_raw(outcomePrices='["0.41"]'))
    assert (m["bid"], m["ask"]) == (40.0, 42.0)
    assert m["probability"] == 41.0


@pytest.mark.parametrize("question,title,want", [
    # A multi-outcome event: every leg carries the event's title as its question.
    ("Major League Soccer Champion", "Nashville SC", "Major League Soccer Champion: Nashville SC"),
    # A whole question, and a `title` that only labels it.
    ("Will Nashville SC win?", "Set 3 Winner", "Will Nashville SC win?"),
    ("Lakers vs. Celtics", "Lakers vs. Celtics", "Lakers vs. Celtics"),
    ("", "Nashville SC", "Nashville SC"),
])
def test_every_leg_of_an_event_gets_a_title_of_its_own(question, title, want):
    assert t.summary_from_gamma(_raw(question=question, title=title))["title"] == want


def test_the_market_id_is_the_slug_and_the_tokens_are_derived_from_it():
    m = t.summary_from_gamma(_raw())
    assert m["conditionId"] == "aec-nba-lal-bos-2026-11-05"
    assert m["yesTokenId"] == "aec-nba-lal-bos-2026-11-05#yes"
    assert m["noTokenId"] == "aec-nba-lal-bos-2026-11-05#no"


def test_the_team_yes_stands_for_comes_off_the_long_side():
    m = t.summary_from_gamma(_raw(marketSides=[
        {"description": "Celtics", "long": False, "team": {"name": "Boston Celtics", "abbreviation": "bos"}},
        {"description": "Lakers", "long": True, "team": {"name": "Los Angeles Lakers", "abbreviation": "lal"}},
    ]))
    assert (m["yesTeam"], m["yesTeamCode"]) == ("Los Angeles Lakers", "lal")


def test_a_market_with_no_team_has_none_rather_than_a_guess():
    m = t.summary_from_gamma(_raw(marketSides=SIDES))
    assert m["yesTeam"] is None and m["yesTeamCode"] is None


def test_there_is_no_volume_or_liquidity_on_a_listing_and_it_says_so():
    m = t.summary_from_gamma(_raw())
    assert m["volume24hUsd"] is None and m["liquidityUsd"] is None
    assert m["negRisk"] is False
    fields = {u["field"] for u in m["unknowns"]}
    assert {"volume24h", "liquidity"} & fields or "volume24hUsd" in fields


@pytest.mark.parametrize("over,want", [
    ({}, True),
    ({"status": "MARKET_STATUS_RESOLVING"}, False),
    ({"status": "MARKET_STATUS_RESOLVED"}, False),
    ({"closed": True}, False),
    ({"active": False}, False),
    ({"archived": True}, False),
    ({"marketSides": [{"long": True, "tradable": False}, {"long": False, "tradable": False}]}, False),
    ({"marketSides": [{"long": True, "tradable": True}, {"long": False, "tradable": False}]}, True),
])
def test_accepting_orders_reads_the_venues_status(over, want):
    assert t.accepting_orders(_raw(**over)) is want


def test_a_market_that_says_nothing_about_orders_is_not_a_yes():
    assert t.accepting_orders({"slug": "x"}) is None


@pytest.mark.parametrize("category,want", [
    ("sports", "sports"), ("Crypto", "crypto"), ("", None), ("no-such", None), (None, None),
])
def test_an_agents_category_comes_from_what_the_venue_states(category, want):
    """A keyword guess would let an agent told "Sports only" buy something that merely
    mentions a team."""
    assert t.venue_category({"category": category, "question": "Will the Lakers ban crypto?"}) == want


def test_a_settled_row_never_reaches_a_board():
    assert t._listable(_raw())
    assert not t._listable(_raw(status="MARKET_STATUS_RESOLVED"))
    assert not t._listable(_raw(hidden=True))


# ── one book, two sides ─────────────────────────────────────────────────


def _book_reply(monkeypatch, md):
    asked = []

    async def _fetch(host, path, params=None, *, ttl=0.0, timeout=10.0):
        asked.append((host, path))
        return ({"marketData": md} if md is not None else None), t._now_iso()
    monkeypatch.setattr(t, "_fetch", _fetch)
    return asked


MD = {"bids": [{"px": {"value": "0.40"}, "qty": "7"}, {"px": {"value": "0.39"}, "qty": "2"}],
      "offers": [{"px": {"value": "0.42"}, "qty": "9"}],
      "state": "MARKET_STATE_OPEN"}


def test_the_yes_book_reads_as_quoted(monkeypatch):
    asked = _book_reply(monkeypatch, MD)
    b = _run(t.book("cpc-a", "yes"))
    assert asked == [("gateway_book", "/v1/markets/cpc-a/book")]
    assert (b["bestBid"], b["bestAsk"], b["spreadCents"], b["midCents"]) == (40.0, 42.0, 2.0, 41.0)
    assert b["available"] is True and b["source"] == "gateway" and b["tokenSide"] == "yes"


def test_the_no_book_is_the_mirror_of_the_same_request(monkeypatch):
    asked = _book_reply(monkeypatch, MD)
    b = _run(t.book("cpc-a", "no"))
    assert asked == [("gateway_book", "/v1/markets/cpc-a/book")]
    assert (b["bestBid"], b["bestAsk"]) == (58.0, 60.0)
    assert (b["bids"][0]["size"], b["asks"][0]["size"]) == (9.0, 7.0)
    assert [lv["priceCents"] for lv in b["asks"]] == [60.0, 61.0]
    assert b["tokenSide"] == "no"


def test_a_failed_read_is_not_an_empty_book(monkeypatch):
    _book_reply(monkeypatch, None)
    b = _run(t.book("cpc-a"))
    assert b["available"] is False and "failed read" in b["note"]

    _book_reply(monkeypatch, {"bids": [], "offers": []})
    b = _run(t.book("cpc-a"))
    assert b["available"] is True and "genuinely empty" in b["note"]
    assert b["bestBid"] is None and b["bestAsk"] is None


def test_something_that_is_not_a_slug_costs_no_request(monkeypatch):
    asked = _book_reply(monkeypatch, MD)
    b = _run(t.book("0xnot/a slug"))
    assert asked == [] and b["available"] is False and "not a Polymarket US market" in b["note"]


@pytest.mark.parametrize("side,bid,ask", [("yes", 40.0, 42.0), ("no", 58.0, 60.0)])
def test_a_summarys_touch_mirrors_the_same_way(side, bid, ask):
    assert t.side_touch({"bid": 40.0, "ask": 42.0}, side) == (bid, ask)


def test_an_empty_side_stays_empty_when_mirrored():
    assert t.side_touch({"bid": 40.0, "ask": None}, "no") == (None, 60.0)


# ── the chart ───────────────────────────────────────────────────────────


def _history_reply(monkeypatch, rows):
    asked = []

    async def _fetch(host, path, params=None, *, ttl=0.0, timeout=10.0):
        asked.append((host, path, params))
        return {"history": rows}, t._now_iso()
    monkeypatch.setattr(t, "_fetch", _fetch)
    return asked


def test_price_history_asks_the_way_the_venue_requires(monkeypatch):
    asked = _history_reply(monkeypatch, [])
    _run(t.price_history("cpc-a", "yes", "1d"))
    (_host, path, params), = asked
    assert path == "/v1/price-history"
    assert params == {"symbol": "cpc-a", "fidelity": 10, "fixedInterval": "INTERVAL_1D"}


def test_each_side_of_the_chart_reads_its_own_price(monkeypatch):
    rows = [{"timestamp": i, "longPrice": 0.40 + i / 100, "shortPrice": 0.60 - i / 100}
            for i in range(6)]
    _history_reply(monkeypatch, rows)
    yes = _run(t.price_history("cpc-a", "yes", "1d"))
    no = _run(t.price_history("cpc-a", "no", "1d"))
    assert yes["points"][0]["p"] == pytest.approx(40.0)
    assert no["points"][0]["p"] == pytest.approx(60.0)
    assert yes["source"] == "gateway" and yes["thin"] is False


def test_a_short_series_is_returned_and_labelled_thin(monkeypatch):
    _history_reply(monkeypatch, [{"timestamp": 1, "longPrice": 0.4, "shortPrice": 0.6}])
    r = _run(t.price_history("cpc-a"))
    assert len(r["points"]) == 1 and r["thin"] is True and "Only 1 price point" in r["note"]


# ── the resolution report on a settled market ───────────────────────────


def test_a_settled_market_says_it_settled_instead_of_blaming_a_failed_read(monkeypatch):
    """Its book is not read at all, so "could not be read" would be untrue."""
    raw = _raw(status="MARKET_STATUS_RESOLVED", active=False, closed=True,
               outcomePrices='["0", "1"]', description="Settles on the BRTI price index.",
               bestBidQuote=None, bestAskQuote=None)

    async def _market(_id, include_raw=False, **_kw):
        s = t.summary_from_gamma(raw)
        return {**s, "_raw": raw} if include_raw else s

    async def _no_book(*a, **kw):
        raise AssertionError("a settled market has no book to read")

    async def _no_siblings(_s):
        return t._check("sibling", "Sibling markets", "unknown", "none", resolved=False)

    monkeypatch.setattr(t, "market", _market)
    monkeypatch.setattr(t, "book", _no_book)
    monkeypatch.setattr(t, "_sibling_check", _no_siblings)
    r = _run(t.resolution_report("aec-nba-lal-bos-2026-11-05"))
    by_id = {c["id"]: c for c in r["checks"]}
    for check in ("spread", "liquidity", "activity"):
        assert by_id[check]["level"] == "unknown" and "settled" in by_id[check]["detail"], check
        assert by_id[check]["resolved"] is False, check


# ── no public tape ──────────────────────────────────────────────────────


def test_the_tape_is_empty_and_says_why(monkeypatch):
    async def _no_network(*a, **kw):
        raise AssertionError("the tape has nothing to fetch")
    monkeypatch.setattr(t, "_fetch", _no_network)
    r = _run(t.tape("cpc-a"))
    assert r["trades"] == [] and r["conditionId"] == "cpc-a"
    assert "does not publish a public trade feed" in r["note"]


# ── the CDN, and the two gates ──────────────────────────────────────────


class _Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = {} if payload is None else payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _client(monkeypatch, resp):
    calls = []

    class _C:
        async def get(self, url, params=None, timeout=None):
            calls.append(url)
            return resp

    async def _get():
        return _C()
    monkeypatch.setattr(t.api, "_get_client", _get)
    return calls


@pytest.fixture
def gates():
    """`_gates` and `_cache` are module state the rest of the suite shares."""
    saved = {k: (g.parked_until, g.last, g.min_gap) for k, g in t._gates.items()}
    for g in t._gates.values():
        g.parked_until, g.last, g.min_gap, g._lock, g._slots = 0.0, 0.0, 0.0, None, None
    t._cache.clear()
    yield t._gates
    for k, g in t._gates.items():
        g.parked_until, g.last, g.min_gap = saved[k]
        g._lock, g._slots = None, None
    t._cache.clear()


def test_a_cdn_copy_is_stamped_with_when_it_was_made_not_when_it_arrived(monkeypatch, gates):
    calls = _client(monkeypatch, _Resp(payload={"markets": []}, headers={"age": "20"}))
    _data, observed = _run(t._fetch("gateway", "/v1/markets", {"limit": 1}))
    assert len(calls) == 1
    assert 19.0 <= t._age_sec(observed) <= 25.0


def test_an_uncached_reply_has_no_age_to_subtract(monkeypatch, gates):
    _client(monkeypatch, _Resp(payload={"market": {}}))
    _data, observed = _run(t._fetch("gateway", "/v1/market/slug/cpc-a"))
    assert t._age_sec(observed) <= 5.0


def test_a_429_parks_for_as_long_as_the_host_said(monkeypatch, gates):
    _client(monkeypatch, _Resp(status=429, headers={"retry-after": "7"}))
    data, _ = _run(t._fetch("gateway", "/v1/markets"))
    assert data is None
    left = gates["gateway"].parked_until - time.monotonic()
    assert 5.0 < left <= 7.5


def test_a_429_with_no_figure_parks_for_the_default(monkeypatch, gates):
    _client(monkeypatch, _Resp(status=429))
    _run(t._fetch("gateway", "/v1/markets"))
    assert gates["gateway"].parked()


def test_the_book_route_has_its_own_budget(monkeypatch, gates):
    """`/book` and `/bbo` allow about 5 reads per 10 seconds per IP, far below the rest
    of the gateway. Sharing a gate, one burst of book reads would park every market
    list and chart along with them."""
    calls = _client(monkeypatch, _Resp(status=429, headers={"retry-after": "10"}))
    _run(t._fetch("gateway_book", "/v1/markets/cpc-a/book"))
    assert gates["gateway_book"].parked() and not gates["gateway"].parked()
    assert len(calls) == 1

    _client(monkeypatch, _Resp(payload={"markets": []}))
    data, _ = _run(t._fetch("gateway", "/v1/markets"))
    assert data == {"markets": []}, "the rest of the gateway was parked with the book"


def test_the_book_gate_is_paced_for_that_budget():
    assert t._HOSTS["gateway_book"]["min_gap"] >= 2.0
    assert t._HOSTS["gateway_book"]["base"] == t._HOSTS["gateway"]["base"]


def test_a_parked_host_fails_fast_with_no_request(monkeypatch, gates):
    calls = _client(monkeypatch, _Resp(payload={}))
    gates["gateway"].park(30.0)
    assert _run(t._fetch("gateway", "/v1/markets"))[0] is None
    assert calls == []


def test_health_names_each_host_once_and_counts_both_gates_under_it():
    """The privacy panel names hosts. The book route is a second gate, not a second
    host, and nothing here talks to the old ones."""
    h = t.health()
    rows = h["hosts"] if "hosts" in h else h["providers"]
    names = [r["host"] for r in (rows.values() if isinstance(rows, dict) else rows)]
    assert names.count("gateway.polymarket.us") == 1
    assert "api.polymarket.us" in names
    assert not [n for n in names if n in (
        "gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com")]


def _row_without_quotes_book_two_sided(monkeypatch):
    """Live on 20 of the 40 newest markets, 2026-11-05: `bestBidQuote` and `bestAskQuote`
    are null while the book holds a 12c bid and a 90c ask."""
    slug = "tsc-setkameua-shcden-napole-2026-11-06"
    raw = _raw(slug=slug, bestBidQuote=None, bestAskQuote=None, outcomePrices='["0.90", "0.88"]')
    book = {"marketData": {"marketSlug": slug,
                           "bids": [{"px": {"value": "0.12"}, "qty": "245"}],
                           "offers": [{"px": {"value": "0.90"}, "qty": "259"}]}}

    async def _fetch(_host, path, params=None, *, ttl=0, timeout=0):
        return (book if path.endswith("/book") else {"market": raw}), t._now_iso()

    monkeypatch.setattr(t, "_fetch", _fetch)
    monkeypatch.setattr(t, "subscribe", lambda *a, **k: None)
    monkeypatch.setattr(t, "_overlay_live", lambda *_a: None)
    return slug


def test_an_empty_row_quote_is_read_off_the_book(monkeypatch):
    slug = _row_without_quotes_book_two_sided(monkeypatch)
    m = _run(t.market(slug))
    assert (m["bid"], m["ask"]) == (12.0, 90.0)
    assert m["spread"] == 78.0
    assert not [u for u in m["unknowns"] if u["field"] in ("bid", "ask", "spread")]


def test_marks_read_the_book_when_the_row_has_no_quote(monkeypatch):
    slug = _row_without_quotes_book_two_sided(monkeypatch)
    bid, ask, _at = _run(t._gateway_tops([slug]))[slug]
    assert (bid, ask) == (12.0, 90.0)


def test_a_really_empty_book_stays_unknown(monkeypatch):
    slug = _row_without_quotes_book_two_sided(monkeypatch)

    async def _fetch(_host, path, params=None, *, ttl=0, timeout=0):
        if path.endswith("/book"):
            return {"marketData": {"bids": [], "offers": []}}, t._now_iso()
        return {"market": _raw(slug=slug, bestBidQuote=None, bestAskQuote=None)}, t._now_iso()

    monkeypatch.setattr(t, "_fetch", _fetch)
    m = _run(t.market(slug))
    assert m["bid"] is None and m["ask"] is None
    assert {"bid", "ask"} <= {u["field"] for u in m["unknowns"]}
