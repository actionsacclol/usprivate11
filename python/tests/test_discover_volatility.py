"""The volatility column, and the honest-null rule that governs its ranking.

The venue reports no price change at all, so the move is measured here from price
history for the most-traded page. The trap this file exists to pin: a market whose
history could not be read. Reading that as 0.0 would rank it as the CALMEST on the
board — a confident claim ("this market barely moved") made about a market nobody
measured. So an unknown move sorts to the end, stays readable, and is counted out
loud in the note.
"""
from __future__ import annotations

import asyncio

import pytest

import terminal as t


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _market(slug="cpc-a", q="Will something happen?"):
    """A minimal venue market row. Only the fields this file cares about."""
    return {
        "slug": slug, "question": q,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.40", "0.60"]',
        "active": True, "closed": False, "status": "MARKET_STATUS_OPEN",
        "bestBidQuote": {"value": "0.40"}, "bestAskQuote": {"value": "0.42"},
    }


# ── the field itself ────────────────────────────────────────────────────

def test_a_listing_carries_no_change_and_says_so():
    """The em-dash rule: a blank must carry its reason, and it is not a zero."""
    s = t.summary_from_gamma(_market())
    assert s["priceChange24h"] is None and s["priceChange1h"] is None
    reasons = {u["field"]: u["reason"] for u in s["unknowns"]}
    assert "priceChange24h" in reasons


def _measured(monkeypatch, points):
    async def _hist(slug, side, interval, fidelity, *, ttl=None):
        return [{"t": i, "p": p} for i, p in enumerate(points)]
    monkeypatch.setattr(t, "_history_points", _hist)
    row = t.summary_from_gamma(_market())
    _run(t._measure_move(row))
    return row


def test_a_move_is_measured_in_cents_and_keeps_its_sign(monkeypatch):
    """A fall is as real as a rise. Only the RANKING takes an absolute value."""
    assert _measured(monkeypatch, [40.0, 96.45])["priceChange24h"] == pytest.approx(56.45)
    assert _measured(monkeypatch, [90.0, 27.0])["priceChange24h"] == pytest.approx(-63.0)


def test_a_measured_move_says_where_it_came_from_and_drops_its_unknown(monkeypatch):
    row = _measured(monkeypatch, [40.0, 50.0])
    assert row["sources"]["priceChange24h"]["source"] == "gateway"
    assert not any(u["field"] == "priceChange24h" for u in row["unknowns"])


def test_zero_change_is_a_real_measurement(monkeypatch):
    """A market that genuinely did not move is NOT the same as one nobody read."""
    row = _measured(monkeypatch, [40.0, 40.0])
    assert row["priceChange24h"] == 0.0
    assert not any(u["field"] == "priceChange24h" for u in row["unknowns"])


def test_no_history_leaves_the_move_unknown(monkeypatch):
    row = _measured(monkeypatch, [])
    assert row["priceChange24h"] is None
    assert any(u["field"] == "priceChange24h" for u in row["unknowns"])


# ── the ranking ─────────────────────────────────────────────────────────

def _discover_volatility(monkeypatch, moves):
    """`moves` is [(slug, title, points)]."""
    by_slug = {slug: pts for slug, _q, pts in moves}

    async def _markets(params, ttl=0.0):
        return [t.summary_from_gamma(_market(slug, q)) for slug, q, _p in moves], "2026-08-30T00:00:00Z"

    async def _hist(slug, side, interval, fidelity, *, ttl=None):
        return [{"t": i, "p": p} for i, p in enumerate(by_slug[slug])]
    monkeypatch.setattr(t, "_gateway_markets", _markets)
    monkeypatch.setattr(t, "_history_points", _hist)
    monkeypatch.setattr(t, "_overlay_live", lambda _r: None)
    monkeypatch.setattr(t, "_enrich_stats_soon", lambda *_a, **_k: None)
    return _run(t.discover("volatility", limit=50))


def test_ranked_by_absolute_move(monkeypatch):
    """A market that fell 40c is exactly as interesting as one that rose 40c."""
    out = _discover_volatility(monkeypatch, [
        ("cpc-a", "small riser", [40.0, 45.0]),
        ("cpc-b", "big faller", [90.0, 50.0]),
        ("cpc-c", "big riser", [10.0, 52.0]),
    ])
    assert [r["title"] for r in out["rows"]] == ["big riser", "big faller", "small riser"]


def test_unknown_move_sorts_last_but_is_kept(monkeypatch):
    out = _discover_volatility(monkeypatch, [
        ("cpc-a", "unknown mover", []),
        ("cpc-b", "calm", [40.0, 41.0]),
    ])
    titles = [r["title"] for r in out["rows"]]
    assert titles == ["calm", "unknown mover"], "an unknown move was ranked as calm"
    assert len(out["rows"]) == 2, "an unrankable row was dropped instead of listed"


def test_note_counts_the_unrankable(monkeypatch):
    out = _discover_volatility(monkeypatch, [
        ("cpc-a", "a", [10.0, 40.0]),
        ("cpc-b", "b", []),
        ("cpc-c", "c", []),
    ])
    assert "2 market(s) here have no measured move" in out["note"]


def test_no_movers_at_all_blames_the_data_not_the_board(monkeypatch):
    """The failure mode this whole module exists to prevent: a confident claim
    about Polymarket made after measuring nothing."""
    out = _discover_volatility(monkeypatch, [("cpc-a", "a", []), ("cpc-b", "b", [])])
    assert "failed read" in out["note"]
    assert "not a calm board" in out["note"]


def test_zero_movers_rank_below_real_movers(monkeypatch):
    """A genuine 0.0 is rankable and belongs above an unknown, not below it."""
    out = _discover_volatility(monkeypatch, [
        ("cpc-a", "unknown", []),
        ("cpc-b", "flat", [40.0, 40.0]),
        ("cpc-c", "mover", [40.0, 60.0]),
    ])
    assert [r["title"] for r in out["rows"]] == ["mover", "flat", "unknown"]


# ── the column swap ─────────────────────────────────────────────────────

def test_resolving_column_is_gone():
    out = _run(t.discover("resolving", limit=5))
    assert out["rows"] == []
    assert "Unknown column" in out["note"]


def test_volatility_is_a_known_column(monkeypatch):
    out = _discover_volatility(monkeypatch, [("cpc-a", "a", [40.0, 50.0])])
    assert out["column"] == "volatility"
    assert "Unknown column" not in (out["note"] or "")
