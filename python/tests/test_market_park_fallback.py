"""market() keeps answering from the last good summary while the gateway is parked."""
import asyncio

import terminal as term


def _summary(cid):
    return {"conditionId": cid, "title": "T", "yesTokenId": f"{cid}#yes", "noTokenId": f"{cid}#no",
            "status": "open", "tickSize": 0.01, "minOrderSize": 1.0, "_raw": {}}


def test_a_parked_gateway_falls_back_to_last_good(monkeypatch):
    term._last_market.clear()

    async def ok(slug):
        return _summary(slug)

    async def parked(slug):
        return None

    async def nothing(ids):
        return []

    monkeypatch.setattr(term, "subscribe", lambda *a, **k: None)
    monkeypatch.setattr(term, "_overlay_live", lambda s: None)
    monkeypatch.setattr(term, "_summaries_for", nothing)
    monkeypatch.setattr(term, "_summary_live", ok)

    async def no_book(slug):
        return None, None

    monkeypatch.setattr(term, "_read_book", no_book)
    first = asyncio.run(term.market("cpc-abc", include_raw=True))
    assert first and "metaStaleSec" not in first

    monkeypatch.setattr(term, "_summary_live", parked)
    again = asyncio.run(term.market("cpc-abc", include_raw=True))
    assert again and again["conditionId"] == "cpc-abc" and again["metaStaleSec"] >= 0


def test_no_fallback_for_unknown_or_expired(monkeypatch):
    term._last_market.clear()

    async def parked(slug):
        return None

    async def nothing(ids):
        return []

    monkeypatch.setattr(term, "_summary_live", parked)
    monkeypatch.setattr(term, "_summaries_for", nothing)
    assert asyncio.run(term.market("cpc-never")) is None

    term._last_market["cpc-old"] = (term.time.monotonic() - term._LAST_MARKET_TTL - 1, _summary("cpc-old"))
    assert asyncio.run(term.market("cpc-old")) is None
