"""Gamma market pagination.

Gamma caps every page at 100 rows regardless of the requested `limit`.
fetch_markets used to emit a next-cursor only when `len(data) >= limit`, and
callers request up to 500 — so `100 >= 500` was never true, the cursor was
always empty, and fetch_all_open_markets returned ONE page no matter what
`max_pages` said. That silently capped scanner.sync_markets() (and therefore
the markets table the whale, momentum and decide_market paths all read) at
100 markets out of ~1,200 available.
"""
from __future__ import annotations

import asyncio

import pytest

import polymarket_api as api


def _fake_gamma(total: int, page_cap: int = 100):
    """Stand-in for the market list: honours `offset`, never returns more than page_cap."""
    calls: list[int] = []

    async def _get(url, params=None, **_kw):
        params = params or {}
        offset = int(params.get("offset") or 0)
        want = int(params.get("limit") or page_cap)
        n = max(0, min(page_cap, want, total - offset))
        calls.append(offset)
        return {"markets": [{
            "question": f"market {offset + i}",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.5","0.5"]',
            "slug": f"market-{offset + i}",
            "status": "MARKET_STATUS_OPEN",
        } for i in range(n)]}

    return _get, calls


def test_fetch_all_open_markets_reaches_past_the_first_page(monkeypatch):
    get, _calls = _fake_gamma(total=1250, page_cap=api.MARKET_PAGE_MAX)
    monkeypatch.setattr(api, "_get", get)

    out = asyncio.run(api.fetch_all_open_markets(max_pages=10))
    assert len(out) == 1250, f"expected every market, got {len(out)}"


def test_pagination_stops_on_a_short_final_page(monkeypatch):
    get, calls = _fake_gamma(total=1250, page_cap=api.MARKET_PAGE_MAX)
    monkeypatch.setattr(api, "_get", get)
    out = asyncio.run(api.fetch_all_open_markets(max_pages=10))
    assert len(out) == 1250
    assert calls == [0, 500, 1000]


def test_max_pages_still_bounds_the_walk(monkeypatch):
    get, _calls = _fake_gamma(total=10_000, page_cap=api.MARKET_PAGE_MAX)
    monkeypatch.setattr(api, "_get", get)
    assert len(asyncio.run(api.fetch_all_open_markets(max_pages=3))) == 1500


def test_exactly_one_full_page_then_empty(monkeypatch):
    get, calls = _fake_gamma(total=500, page_cap=api.MARKET_PAGE_MAX)
    monkeypatch.setattr(api, "_get", get)
    assert len(asyncio.run(api.fetch_all_open_markets(max_pages=5))) == 500
    assert calls == [0, 500]


def test_an_oversized_request_is_clamped_to_the_real_page(monkeypatch):
    seen: dict = {}

    async def _get(url, params=None, **_kw):
        seen.update(params or {})
        return {"markets": []}

    monkeypatch.setattr(api, "_get", _get)
    asyncio.run(api.fetch_markets(limit=5000))
    assert seen["limit"] == api.MARKET_PAGE_MAX


def test_a_resolved_row_in_the_open_list_is_dropped(monkeypatch):
    async def _get(url, params=None, **_kw):
        return {"markets": [
            {"slug": "open-one", "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]',
             "status": "MARKET_STATUS_OPEN"},
            {"slug": "done-one", "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]',
             "status": "MARKET_STATUS_RESOLVED", "closed": True},
        ]}

    monkeypatch.setattr(api, "_get", _get)
    rows, _cursor = asyncio.run(api.fetch_markets())
    assert [m["ticker"] for m in rows] == ["open-one"]


@pytest.mark.parametrize("total,expected", [(0, 0), (1, 1), (99, 99), (100, 100)])
def test_small_result_sets(monkeypatch, total, expected):
    get, _ = _fake_gamma(total=total, page_cap=api.MARKET_PAGE_MAX)
    monkeypatch.setattr(api, "_get", get)
    assert len(asyncio.run(api.fetch_all_open_markets(max_pages=5))) == expected
