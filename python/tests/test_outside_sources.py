"""The outside sources, as checked against the live services on 2026-11-05.

The Kalshi rows are shaped like the live ones. A wrong pairing shows a price gap that
isn't real, so most of these pin what must NOT match.
"""
from __future__ import annotations

import asyncio

import pytest

import ai_analysis
import ai_providers
import config
import crypto15m
import cross_venue as cv
import kalshi


def _leg(ticker, subtitle, event=None, probability=50.0):
    return {"conditionId": ticker, "subtitle": subtitle, "probability": probability,
            "eventTicker": event or ticker.rsplit("-", 1)[0], "matchText": f"{subtitle}",
            "closeTime": "2029-02-14T00:00:00Z"}


def _find(monkeypatch, pm, series, legs):
    rows = [{"ticker": series, "title": "Unrelated words"}]

    async def index():
        return rows, {}, True

    async def events(_ticker):
        return legs

    monkeypatch.setattr(cv, "_get_index", index)
    monkeypatch.setattr(kalshi, "events_in_series", events)
    return asyncio.run(cv.find_match(pm))


FED_LEGS = [
    _leg("KXFEDDECISION-27JAN-C25", "Cut 25bps"), _leg("KXFEDDECISION-27JAN-C26", "Cut >25bps"),
    _leg("KXFEDDECISION-26DEC-C25", "Cut 25bps"), _leg("KXFEDDECISION-26DEC-C26", "Cut >25bps"),
]


@pytest.mark.parametrize("title,leg", [
    ("Fed Rate Cut Greater Than 25bps?", "C26"),
    ("Fed Decision in December: 50+ bps Decrease", "C26"),
    ("Fed Decision in December: 25 bps Decrease", "C25"),
    ("Fed Decision in December: 25 bps Increase", "H25"),
    ("Fed Decision in December: No Change", "H0"),
])
def test_the_fed_legs_polymarket_us_writes(title, leg):
    assert cv.fed_leg(title) == leg


def test_a_cut_by_a_deadline_is_not_one_meeting(monkeypatch):
    """Live: this paired with "Cut 25bps" in January 2027 and showed a price gap,
    wrong on the size and on the meeting."""
    pm = {"slug": "cbpac-fed-gt25bps-2026-12-31", "title": "Fed Rate Cut Greater Than 25bps?",
          "closeTime": "2027-01-15T04:00:00Z"}
    assert cv.fed_meeting_month(pm) is None
    assert _find(monkeypatch, pm, "KXFEDDECISION", FED_LEGS) is None


def test_a_deadline_in_the_title_is_not_one_meeting():
    assert cv.fed_meeting_month({"title": "Fed cut 25 bps by December 31, 2026?",
                                 "closeTime": "2027-01-15T04:00:00Z"}) is None


def test_a_real_meeting_market_still_matches_its_own_leg(monkeypatch):
    pm = {"slug": "rdc-usfed-fomc-2026-12-09-cut25", "title": "Fed Decision in December: 25 bps Decrease",
          "closeTime": "2027-01-15T04:00:00Z"}
    hit = _find(monkeypatch, pm, "KXFEDDECISION", FED_LEGS)
    assert hit["market"]["conditionId"] == "KXFEDDECISION-26DEC-C25"
    assert hit["score"] >= cv.STRONG


def test_a_big_cut_never_pairs_with_the_small_one(monkeypatch):
    pm = {"slug": "rdc-usfed-fomc-2026-12-09-cut50", "title": "Fed Decision in December: 50+ bps Decrease",
          "closeTime": "2026-12-10T00:00:00Z"}
    hit = _find(monkeypatch, pm, "KXFEDDECISION", FED_LEGS)
    assert hit["market"]["conditionId"] == "KXFEDDECISION-26DEC-C26"


@pytest.mark.parametrize("title,close,want", [
    ("Will the Fed cut after the September 2026 meeting?", "2026-09-17T00:00:00Z", "2026-09"),
    ("Fed Decision in December: No Change", "2027-01-15T04:00:00Z", "2026-12"),
    ("Fed Decision in December: No Change", "2026-12-10T04:00:00Z", "2026-12"),
    ("Fed Rate Cut in 2026?", "2027-01-15T04:00:00Z", None),
])
def test_the_meeting_comes_from_the_question_not_the_padded_close(title, close, want):
    assert cv.fed_meeting_month({"title": title, "closeTime": close}) == want


NFL = [_leg("KXSB-27-SF", "San Francisco"), _leg("KXSB-27-LAC", "Los Angeles C"),
       _leg("KXSB-27-LAR", "Los Angeles R"), _leg("KXSB-27-NYG", "New York G"),
       _leg("KXSB-27-NYJ", "New York J")]
NBA = [_leg("KXNBA-27-TOR", "Toronto"), _leg("KXNBA-27-LAL", "Los Angeles L"),
       _leg("KXNBA-27-LAC", "Los Angeles C"), _leg("KXNBA-27-NYK", "New York")]


@pytest.mark.parametrize("slug,title,series,legs,want", [
    ("tec-nfl-champ-2027-02-14-w-saners", "Pro Football Champion 2027: San Francisco", "KXSB", NFL, "KXSB-27-SF"),
    ("tec-nfl-champ-2027-02-14-w-loscha", "Pro Football Champion 2027: Los Angeles C", "KXSB", NFL, "KXSB-27-LAC"),
    ("tec-nfl-champ-2027-02-14-w-newgia", "Pro Football Champion 2027: New York G", "KXSB", NFL, "KXSB-27-NYG"),
    ("tec-nba-champ-2027-06-18-w-tor", "2027 NBA Champion: Toronto Raptors", "KXNBA", NBA, "KXNBA-27-TOR"),
    ("tec-nba-champ-2027-06-18-w-lal", "2027 NBA Champion: LA Lakers", "KXNBA", NBA, "KXNBA-27-LAL"),
    ("tec-nba-champ-2027-06-18-w-nyk", "2027 NBA Champion: New York Knicks", "KXNBA", NBA, "KXNBA-27-NYK"),
])
def test_a_champion_finds_its_own_team(monkeypatch, slug, title, series, legs, want):
    hit = _find(monkeypatch, {"slug": slug, "title": title, "closeTime": "2027-02-20T00:00:00Z"}, series, legs)
    assert hit["market"]["conditionId"] == want
    assert hit["score"] >= cv.STRONG and hit["side"] == "yes"


def test_an_accent_does_not_hide_a_team(monkeypatch):
    pm = {"slug": "tec-nhl-champ-2027-06-18-w-mon", "title": "2027 Stanley Cup Champion: Montreal Canadiens"}
    hit = _find(monkeypatch, pm, "KXNHL", [_leg("KXNHL-27-MTL", "Montréal Canadiens")])
    assert hit["market"]["conditionId"] == "KXNHL-27-MTL"


def test_a_different_season_is_not_a_match(monkeypatch):
    pm = {"slug": "tec-nfl-champ-2028-02-14-w-saners", "title": "Pro Football Champion 2028: San Francisco"}
    assert _find(monkeypatch, pm, "KXSB", NFL) is None


def test_a_team_it_cannot_tell_apart_is_refused(monkeypatch):
    pm = {"slug": "tec-nba-champ-2027-06-18-w-xx", "title": "2027 NBA Champion: Los Angeles"}
    assert _find(monkeypatch, pm, "KXNBA", NBA) is None


def test_an_unpriced_leg_is_not_compared(monkeypatch):
    pm = {"slug": "tec-nfl-champ-2027-02-14-w-saners", "title": "Pro Football Champion 2027: San Francisco"}
    assert _find(monkeypatch, pm, "KXSB", [_leg("KXSB-27-SF", "San Francisco", probability=None)]) is None


def test_other_competitions_stay_out_of_the_lookup():
    assert cv.parse_season_winner({"slug": "tec-nfl-mvp-2027-02-11-w-tylsho", "title": "Pro Football MVP: Tyler"}) is None


def test_no_match_never_claims_kalshi_has_no_such_market():
    assert "may still exist" in cv.NO_MATCH_NOTE


def test_the_dead_price_feed_is_gone():
    assert [name for name, _ in crypto15m._SPOT_SOURCES] == ["coinbase", "coingecko"]


def test_the_last_resort_feed_asks_for_full_precision():
    seen = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ripple": {"usd": 1.3257172}}

    class _Client:
        async def get(self, _url, params=None):
            seen.update(params)
            return _Resp()

    got = asyncio.run(crypto15m._spots_coingecko(_Client()))
    assert seen["precision"] == "full"
    assert got["XRP"] == pytest.approx(1.3257172)


def test_the_default_model_and_its_prices():
    assert config.DEFAULT_CONFIG["ai_model"] == "claude-opus-5-5"
    assert ai_analysis.DEFAULT_ANTHROPIC_MODEL == "claude-opus-5-5"
    assert ai_providers.DEFAULT_MODEL["anthropic"] == "claude-opus-5-5"
    for model in ai_providers.SUGGESTED["anthropic"]:
        assert model in ai_providers.PRICING
    assert "anthropic/claude-sonnet-4.5" not in ai_providers.SUGGESTED["openrouter"]
    assert ai_providers.cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)


def test_a_long_haiku_prompt_is_costed_at_the_long_prompt_rate():
    assert ai_providers.cost_usd("claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert ai_providers.cost_usd("claude-haiku-5-5", 200_000, 0) == pytest.approx(0.10)


@pytest.mark.parametrize("model,effort", [
    ("claude-opus-5-5", "medium"), ("claude-sonnet-5-5", "medium"), ("claude-opus-5", "high"),
])
def test_effort_suits_the_model(model, effort):
    assert ai_analysis._effort_for(model) == effort
