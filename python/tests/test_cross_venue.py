"""Cross-venue matching, pinned.

This is the one place in the terminal that asserts something no provider told
us: that two markets ask the same question. A wrong pairing produces a large,
confident, entirely fictional arbitrage — the exact kind of number somebody
acts on. Every test here guards a specific way that goes wrong.

Pure scoring only; no network.
"""
from __future__ import annotations

import pytest

import cross_venue as cv
import kalshi
import terminal as t


def _pm(title, close=None):
    return {"title": title, "closeTime": close}


def _kx(text, close=None):
    return {"matchText": text, "closeTime": close}


def _score(pm_title, kx_text, pm_close=None, kx_close=None):
    return cv.score_pair(_pm(pm_title, pm_close), _kx(kx_text, kx_close))[0]


# ────────────────────────────────────────────────────────────────────────
# Polarity — the guard that stops a fictional arbitrage.
# ────────────────────────────────────────────────────────────────────────


def test_opposite_direction_is_disqualified_outright():
    """Caught on a live run: "Fed INCREASE rates by 25bps" matched Kalshi's
    "CUT 25bps" at a 0.63 score and printed a 33c spread between two mutually
    exclusive markets. A word list of antonym pairs had increase/decrease but
    not increase/cut, which is why the rule is polarity, not pairs."""
    assert _score(
        "Will the Fed increase interest rates by 25 bps after the September meeting?",
        "Will the Federal Reserve Cut rates by 25bps at the September meeting Cut 25bps",
    ) == 0.0


def test_same_direction_still_matches():
    # Close times included because a real pairing has them on both sides, and
    # the date agreement is part of what makes it strong.
    s = _score(
        "Will the Fed decrease interest rates by 25 bps after the September 2026 meeting?",
        "Will the Federal Reserve Cut rates by 25bps at the September meeting Cut 25bps",
        "2026-09-16T00:00:00Z", "2026-09-17T00:00:00Z",
    )
    assert s >= cv.STRONG, f"a correct pairing scored only {s}"


@pytest.mark.parametrize("up,down", [
    ("increase", "cut"), ("hike", "cut"), ("raise", "lower"),
    ("above", "below"), ("rise", "fall"), ("over", "under"),
])
def test_direction_words_are_recognised_as_opposites(up, down):
    a = f"Will the rate {up} to 5 percent in March"
    b = f"Will the rate {down} to 5 percent in March"
    assert t._are_opposites(a, b), f"{up}/{down} was not caught"
    assert cv.score_pair(_pm(a), _kx(b))[0] == 0.0


def test_a_two_directional_title_claims_no_direction():
    # "Bitcoin Up or Down" names both, so it asserts nothing to contradict.
    assert t.polarity("Bitcoin Up or Down at 9:00PM") is None
    assert not t._are_opposites("Bitcoin Up or Down 9:00PM", "Bitcoin Up or Down 9:05PM")


def test_polarity_reads_a_single_direction():
    assert t.polarity("Will the Fed hike rates") == "up"
    assert t.polarity("Will the Fed cut rates") == "down"
    assert t.polarity("Who will be the next Pope") is None


# ────────────────────────────────────────────────────────────────────────
# Numbers — the discriminator for threshold markets.
# ────────────────────────────────────────────────────────────────────────


def test_different_thresholds_do_not_match():
    """"Bitcoin above $110,000" and "Bitcoin above $120,000" differ by nothing
    a token comparison can see, and pairing them would invent a huge spread.

    Note both titles also contain "30" (the day). Scoring on whether the two
    SHARE a number let that date carry the match — the rule has to be about
    what each side names that the other CONTRADICTS."""
    s = _score("Will Bitcoin be above $110,000 on August 30?",
               "Bitcoin above 120000 on August 30")
    assert s < cv.POSSIBLE, f"different thresholds scored {s}"


def test_a_shared_date_cannot_rescue_a_different_threshold():
    s = _score("Will Bitcoin be above $110,000 on Aug 30?",
               "Bitcoin price above 95000 on Aug 30")
    assert s == 0.0, f"a shared day-of-month carried a mismatched threshold ({s})"


def test_matching_thresholds_score_higher_than_none():
    same = _score("Will Bitcoin be above $110,000 on Aug 30?",
                  "Bitcoin price above 110000 on Aug 30")
    diff = _score("Will Bitcoin be above $110,000 on Aug 30?",
                  "Bitcoin price above 95000 on Aug 30")
    assert same > diff
    assert same >= cv.POSSIBLE


def test_years_are_not_treated_as_thresholds():
    # Both venues say the year; it carries no discriminating information, and
    # treating it as a threshold would make every dated pair "agree".
    assert cv._numbers("Will X happen in 2026?") == set()
    assert "25" in cv._numbers("Cut by 25 bps in 2026")


def test_numbers_normalise_across_formatting():
    assert cv._numbers("above $110,000") == cv._numbers("above 110000")


def test_a_price_that_looks_like_a_year_is_still_a_price():
    """The years filter used to drop every integer in 1900..2100, so the
    threshold in "ETH above $2,000" vanished, both sides named no figure at
    all, nothing contradicted, and a $100 gap paired at strong confidence with
    a fictional spread printed under it."""
    assert cv._numbers("Will ETH be above $2,000 on Sept 30?") == {"2000", "30"}
    assert cv._numbers("ETH above 2000 points") == {"2000"}
    assert cv._numbers("ETH above $2000") == {"2000"}


def test_a_hundred_dollar_threshold_gap_does_not_pair():
    s = _score("Will ETH be above $2,000?", "ETH above $2,100")
    assert s < cv.POSSIBLE, f"a $100 threshold gap scored {s}"


def test_a_year_beside_a_threshold_is_still_only_context():
    # The year drops, the threshold does not — one sentence, both rules.
    assert cv._numbers("Will ETH be above $2,000 by 2026?") == {"2000"}


@pytest.mark.parametrize("text", [
    "Will X happen in 2026?",
    "Resolves by 2030.",
    "Cut by 25 bps in 2026",
])
def test_bare_years_are_still_dropped(text):
    assert not {n for n in cv._numbers(text) if 2000 <= float(n) <= 2040}


def test_two_different_years_in_the_text_are_not_a_figure_mismatch():
    """Checked deliberately: the module's ONLY date-mismatch signal is
    closeTime, so a year written in the title is context on both sides and
    contradicts nothing. What separates "by 2026" from "by 2027" here is the
    token overlap and, on a real pair, the settlement dates."""
    a, b = "Fed decision by 2026", "Fed decision by 2027"
    assert cv._numbers(a) == cv._numbers(b) == set()
    # It still does not reach strong on words alone…
    assert _score(a, b) < cv.STRONG
    # …and with the settlement dates a real pair carries, it is penalised.
    assert _score(a, b, "2026-12-31T00:00:00Z", "2027-12-31T00:00:00Z") < cv.POSSIBLE


# ────────────────────────────────────────────────────────────────────────
# Dates — same question, different period.
# ────────────────────────────────────────────────────────────────────────


def test_far_apart_settlement_dates_are_penalised():
    """A live run matched the Sep 2026 Fed question to Kalshi's Sep 2027 one:
    same words, different year, and the spread would have been noise."""
    near = _score("Fed decision at the September meeting",
                  "Fed decision in September",
                  "2026-09-16T00:00:00Z", "2026-09-17T00:00:00Z")
    far = _score("Fed decision at the September meeting",
                 "Fed decision in September",
                 "2026-09-16T00:00:00Z", "2027-09-16T00:00:00Z")
    assert near > far
    assert far < cv.STRONG, "a year-apart pairing reached strong confidence"


def test_a_missing_date_is_recorded_against_the_match():
    _, _, against = cv.score_pair(
        _pm("Fed decision in September", "2026-09-16T00:00:00Z"),
        _kx("Fed decision in September", None))
    assert any("settlement date" in a for a in against)


# ────────────────────────────────────────────────────────────────────────
# Confidence tiers and what they gate.
# ────────────────────────────────────────────────────────────────────────


def test_only_two_confidence_tiers_are_ever_shown():
    assert cv._confidence(0.9) == "strong"
    assert cv._confidence(cv.STRONG) == "strong"
    assert cv._confidence(cv.POSSIBLE) == "possible"
    # Anything below POSSIBLE is reported as NO match — see find_match.


def test_unrelated_markets_score_below_the_floor():
    """A live run paired "Pittsburgh Pirates vs San Diego Padres" with
    "Christian Walker's Next Team" on the strength of "san"+"diego"."""
    s = _score("Pittsburgh Pirates vs. San Diego Padres",
               "Christian Walker's Next Team San Diego")
    assert s < cv.POSSIBLE, f"unrelated markets scored {s}"


def test_an_empty_title_never_matches():
    assert _score("", "Fed decision in September") == 0.0
    assert _score("Fed decision in September", "") == 0.0


# ────────────────────────────────────────────────────────────────────────
# The Kalshi row shape.
# ────────────────────────────────────────────────────────────────────────


def test_kalshi_dollar_strings_become_cents():
    row = kalshi.market_row({
        "ticker": "KXTEST-1", "title": "Test?",
        "yes_bid_dollars": "0.4200", "yes_ask_dollars": "0.4400",
        "status": "active",
    })
    assert row["bid"] == 42.0 and row["ask"] == 44.0
    assert row["probability"] == 43.0
    assert row["venue"] == "kalshi"


def test_kalshi_legacy_cent_fields_still_read():
    row = kalshi.market_row({
        "ticker": "KXTEST-1", "title": "Test?",
        "yes_bid": 42, "yes_ask": 44, "status": "active",
    })
    assert row["bid"] == 42.0 and row["ask"] == 44.0


def test_a_one_cent_quote_survives():
    """`yes_bid: 1` is 1¢. Reading it as $1.00 made it 100¢, which the
    empty-book scrub then dropped — so a real, tradeable long-shot market
    reported as having no quote at all. The unit is in the field name; there
    was never anything to infer."""
    row = kalshi.market_row({
        "ticker": "KXTEST-1", "title": "Test?",
        "yes_bid": 1, "yes_ask": 2, "status": "active",
    })
    assert row["bid"] == 1.0 and row["ask"] == 2.0
    assert row["probability"] == 1.5


def test_a_one_dollar_quote_is_still_the_empty_book_sentinel():
    # The dollar field means what it says: $1.00 is 100¢, which is not a
    # price anyone could trade, so it is unknown rather than a quote.
    assert kalshi._from_dollars("1.0000") is None
    assert kalshi._from_dollars("0.0100") == 1.0
    assert kalshi._from_cents(1) == 1.0
    assert kalshi._from_cents(100) is None


def test_a_candle_reads_its_unit_from_the_written_value():
    # The candle tiers share a key name, so the unit has to come off the
    # value. A written decimal point is dollars; a bare integer is cents.
    assert kalshi._cents("0.9400") == 94.0
    assert kalshi._cents(94) == 94.0
    assert kalshi._cents(1) == 1.0, "a 1c candle became $1.00 and vanished"
    assert kalshi._cents("1.0000") is None


def test_kalshi_empty_book_is_not_a_quote():
    """Same rule as Polymarket: neither 0c nor 100c is a price anyone could
    trade, so an empty side is unknown rather than an extreme quote."""
    row = kalshi.market_row({
        "ticker": "KXTEST-1", "title": "Test?",
        "yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000",
        "status": "active",
    })
    assert row["bid"] is None and row["ask"] is None
    assert row["spread"] is None
    assert row["probability"] is None
    assert row["unknowns"], "an unpriced market gave no reason"


def test_kalshi_derives_a_missing_side_only_from_a_present_one():
    row = kalshi.market_row({
        "ticker": "KXTEST-1", "title": "Test?",
        "no_bid_dollars": "0.6000", "status": "active",
    })
    # yes_ask = 100 - no_bid = 40
    assert row["ask"] == 40.0
    # Nothing to derive the bid from, so it stays unknown — NOT 100.
    assert row["bid"] is None


def test_kalshi_candles_are_sorted_and_deduped():
    pts = kalshi.normalize_candles([
        {"end_period_ts": 200, "price": {"close": "0.50"}},
        {"end_period_ts": 100, "price": {"close": "0.40"}},
        {"end_period_ts": 200, "price": {"close_dollars": "0.5500"}},
    ])
    assert [p["t"] for p in pts] == [100, 200]
    assert pts[-1]["p"] == 55.0, "the later correction should win"


def test_kalshi_candles_fall_back_to_the_book_mid():
    # A period that quoted but never traded has no close; the mid of the book
    # is a real observation, not an invention.
    pts = kalshi.normalize_candles([
        {"end_period_ts": 10,
         "yes_bid": {"close": "0.40"}, "yes_ask": {"close": "0.44"}},
    ])
    assert pts == [{"t": 10, "p": 42.0}]


def test_kalshi_candles_skip_a_period_with_nothing_at_all():
    # No close, no book: no point. Never a gap-filled flat line.
    assert kalshi.normalize_candles([{"end_period_ts": 10}]) == []


# ────────────────────────────────────────────────────────────────────────
# The shared time grid.
# ────────────────────────────────────────────────────────────────────────


def test_bucketing_snaps_both_venues_onto_one_grid():
    pts = cv._bucket([{"t": 3661, "p": 50.0}, {"t": 3700, "p": 52.0}], 3600)
    # Both fall in the bucket starting at 3600; the last one wins.
    assert pts == {3600: 52.0}


def test_bucketing_drops_a_null_rather_than_carrying_it_forward():
    pts = cv._bucket([{"t": 0, "p": 50.0}, {"t": 3600, "p": None}], 3600)
    assert pts == {0: 50.0}, "a null price became a data point"


# ────────────────────────────────────────────────────────────────────────
# The settlement DAY
#
# Both venues state their dates in US Eastern, and there is no tz database on
# this machine, so the rule is implemented directly. If it is wrong, an 8pm
# fixture lands on tomorrow and every same-day pair looks a day apart.
# ────────────────────────────────────────────────────────────────────────


def test_eastern_time_is_read_without_a_timezone_database():
    # 9pm EDT on Sep 2 is 01:00 UTC on Sep 3 — and it is still Sep 2.
    assert cv.et_date("2026-09-03T01:00:00Z") == "2026-09-02"
    assert cv.et_date("2026-09-03T05:00:00Z") == "2026-09-03"


@pytest.mark.parametrize("iso,day", [
    # DST starts 07:00 UTC on the second Sunday in March 2026 (Mar 8).
    ("2026-03-08T06:59:00Z", "2026-03-08"),   # still EST, 01:59
    ("2026-03-08T07:00:00Z", "2026-03-08"),   # EDT, 03:00
    # …and ends 06:00 UTC on the first Sunday in November (Nov 1).
    ("2026-11-01T05:30:00Z", "2026-11-01"),
    ("2026-01-01T04:59:00Z", "2025-12-31"),   # EST puts this in the old year
])
def test_the_us_daylight_saving_rule(iso, day):
    assert cv.et_date(iso) == day


@pytest.mark.parametrize("ticker,day", [
    ("KXMLBGAME-26SEP021840NYMTB", "2026-09-02"),
    ("KXBTCD-26SEP0221", "2026-09-02"),
    ("KXHIGHMIA-26SEP02", "2026-09-02"),
    ("KXBTCMINY-27JAN01", "2027-01-01"),
    ("KXFEDDECISION-26SEP", None),        # a month, not a day
    ("KXUCL-27", None),
])
def test_kalshi_stamps_its_own_date_into_the_event_ticker(ticker, day):
    """Preferred over every timestamp on the row: BOTH venues pad a game's
    close time by days so a postponed fixture can still settle, and comparing
    two padded dates is what buried every sports pairing."""
    assert cv.kalshi_ticker_date(ticker) == day


def test_a_date_written_out_in_the_question_is_read():
    assert cv.text_date("Will X be above $76,000 on September 3?",
                        year_hint="2026-09-03T16:00:00Z") == "2026-09-03"
    assert cv.text_date("by December 31, 2026") == "2026-12-31"
    assert cv.text_date("no date here", year_hint="2026") is None


# ────────────────────────────────────────────────────────────────────────
# Thresholds, read from either venue's wording.
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,want", [
    ("Will the price of Bitcoin be above $76,000 on September 3?",
     ("above", 76000.0)),
    ("$76,000 or above", ("above", 76000.0)),        # Kalshi's word order
    ("85 or below", ("below", 85.0)),
    ("Over 8.5 runs scored", ("above", 8.5)),
    ("Tampa Bay wins by over 1.5 runs", ("above", 1.5)),
    ("Celta Vigo wins the 1H by more than 1.5 goals", ("above", 1.5)),
    ("San Francisco Giants vs. Pittsburgh Pirates: O/U 8.5", ("above", 8.5)),
    ("Will the highest temperature in Miami be between 88-89F on September 2?",
     ("between", 88.0, 89.0)),
    ("$86,000 to 86,249.99", ("between", 86000.0, 86249.99)),
    ("Below $45,000.00", ("below", 45000.0)),
    ("Will Bitcoin hit 150k this year?", ("above", 150000.0)),
    ("Who will win the election?", None),
])
def test_thresholds_are_read_from_the_wording(text, want):
    assert cv.parse_threshold(text) == want


def test_a_multiplier_letter_must_not_eat_the_next_word():
    """"$45,000 by December 31" read the "b" of "by" as billions, asked about
    a threshold of forty-five trillion, and matched nothing — silently, which
    is the worst way for a matcher to fail."""
    assert cv.parse_threshold(
        "Will Bitcoin dip to $45,000 by December 31, 2026?") == ("below", 45000.0)


def test_the_same_level_written_two_ways_is_one_level():
    """Kalshi's "greater" strike sits one tick under the number it prints:
    floor_strike 75999.99 under the label "$76,000 or above"."""
    assert cv.compare_thresholds(("above", 76000.0), ("above", 75999.99)) == "same"


def test_a_hundred_dollar_gap_is_never_the_same_level():
    assert cv.compare_thresholds(("above", 2000.0), ("above", 2100.0)) is None
    assert cv.compare_thresholds(("above", 76000.0), ("above", 76250.0)) is None


def test_the_same_level_from_opposite_sides_is_a_complement():
    assert cv.compare_thresholds(("above", 76000.0),
                                 ("below", 75999.99)) == "complement"


def test_a_band_matches_only_the_identical_band():
    assert cv.compare_thresholds(("between", 88.0, 89.0),
                                 ("between", 88.0, 89.0)) == "same"
    assert cv.compare_thresholds(("between", 88.0, 89.0),
                                 ("between", 86.0, 87.0)) is None
    # A band is not the complement of anything, so it never inverts.
    assert cv.compare_thresholds(("between", 88.0, 89.0),
                                 ("above", 88.0)) is None


# ────────────────────────────────────────────────────────────────────────
# The Fed, read as legs rather than as numbers.
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,leg", [
    ("Will there be no change in Fed interest rates after the September 2026 "
     "meeting?", "H0"),
    ("Will the Fed increase interest rates by 25 bps after the September 2026 "
     "meeting?", "H25"),
    ("Will the Fed decrease interest rates by 25 bps after the September 2026 "
     "meeting?", "C25"),
    ("Will the Fed increase interest rates by 50+ bps after the September "
     "2026 meeting?", "H26"),
    ("Will the Fed decrease interest rates by 50+ bps after the September "
     "2026 meeting?", "C26"),
    ("Will the Fed increase interest rates by 50 bps?", "H26"),
    ("Who will be the next Fed chair?", None),
    ("Will Bitcoin be above $76,000?", None),
])
def test_fed_legs_are_read_from_the_question(text, leg):
    """Polymarket writes "50+ bps" and Kalshi writes "Hike >25bps". They are
    the same contract — the Fed moves in 25bp steps — but no threshold rule
    can say so, because 50 and 25 are different numbers."""
    assert cv.fed_leg(text) == leg


def test_the_kalshi_fed_leg_comes_off_its_ticker():
    assert cv.kalshi_fed_leg({"conditionId": "KXFEDDECISION-26SEP-H25"}) == "H25"
    assert cv.kalshi_fed_leg({"conditionId": "KXBTCD-26SEP0317-T1"}) is None


def test_opposite_fed_legs_never_structurally_match():
    """The pinned failure: "increase 25bps" paired with "Cut 25bps" and
    printed a 33c spread between two mutually exclusive markets."""
    pm = _pm("Will the Fed increase interest rates by 25 bps after the "
             "September 2026 meeting?", "2026-09-16T00:00:00Z")
    kx = {"conditionId": "KXFEDDECISION-26SEP-C25",
          "matchText": "Fed decision in Sep 2026? Cut 25bps",
          "subtitle": "Cut 25bps", "eventTicker": "KXFEDDECISION-26SEP"}
    assert cv.structural_match(pm, kx) is None


def test_the_same_fed_leg_in_a_different_year_does_not_match():
    pm = _pm("Will the Fed increase interest rates by 25 bps after the "
             "September 2026 meeting?", "2026-09-16T00:00:00Z")
    kx = {"conditionId": "KXFEDDECISION-27SEP-H25",
          "matchText": "Fed decision in Sep 2027? Hike 25bps",
          "subtitle": "Hike 25bps", "eventTicker": "KXFEDDECISION-27SEP",
          "closeTime": "2027-09-16T00:00:00Z"}
    assert cv.structural_match(pm, kx) is None


# ────────────────────────────────────────────────────────────────────────
# Structural matching, and the NO side.
# ────────────────────────────────────────────────────────────────────────


def _kx_threshold(ticker, event, subtitle, match_text, close=None):
    return {"conditionId": ticker, "eventTicker": event, "subtitle": subtitle,
            "matchText": match_text, "strikeType": None,
            "floorStrike": None, "capStrike": None, "closeTime": close}


def test_a_threshold_pair_that_agrees_on_all_four_fields_clears_strong():
    pm = _pm("Will the price of Bitcoin be above $76,000 on September 3?",
             "2026-09-03T16:00:00Z")
    kx = _kx_threshold(
        "KXBTCD-26SEP0317-T75999.99", "KXBTCD-26SEP0317", "$76,000 or above",
        "BTC price on Sep 3, 2026 at 5pm EDT? $76,000 or above")
    v = cv.structural_match(pm, kx)
    assert v and v["side"] == "yes"
    assert v["score"] >= cv.STRONG
    assert any("76000" in f for f in v["for"])


def test_the_next_days_strike_is_a_different_question():
    pm = _pm("Will the price of Bitcoin be above $76,000 on September 3?",
             "2026-09-03T16:00:00Z")
    kx = _kx_threshold(
        "KXBTCD-26SEP0417-T75999.99", "KXBTCD-26SEP0417", "$76,000 or above",
        "BTC price on Sep 4, 2026 at 5pm EDT? $76,000 or above")
    assert cv.structural_match(pm, kx) is None


def test_the_no_side_is_matched_but_never_silently():
    """Kalshi lists plenty of levels from the other direction. Comparing
    against its NO price is correct; doing it without saying so would flip the
    sign of the spread with nothing on screen to explain it."""
    pm = _pm("Will the price of Bitcoin be above $76,000 on September 3?",
             "2026-09-03T16:00:00Z")
    kx = _kx_threshold(
        "KXBTC-26SEP0317-T76000", "KXBTC-26SEP0317", "$75,999.99 or below",
        "BTC price range on Sep 3, 2026 $75,999.99 or below")
    v = cv.structural_match(pm, kx)
    assert v and v["side"] == "no"
    assert any("NO side" in a for a in v["against"])


def test_a_deadline_may_be_written_from_either_side_of_midnight():
    """"by December 31, 2026" and a Kalshi strike date of January 1, 2027 are
    one cut-off, not two questions a day apart. The "by" is what says so."""
    pm = _pm("Will Bitcoin dip to $45,000 by December 31, 2026?",
             "2027-01-01T05:00:00Z")
    kx = _kx_threshold(
        "KXBTCMINY-27JAN01-45000.00", "KXBTCMINY-27JAN01", "Below $45,000.00",
        "How low will Bitcoin get in 2026? Below $45,000.00")
    v = cv.structural_match(pm, kx)
    assert v and v["side"] == "yes"
    assert any("cut-off" in a for a in v["against"])


def test_the_same_threshold_on_a_different_subject_does_not_match():
    pm = _pm("Will the price of Bitcoin be above $76,000 on September 3?",
             "2026-09-03T16:00:00Z")
    kx = _kx_threshold(
        "KXETHD-26SEP0317-T75999.99", "KXETHD-26SEP0317", "$76,000 or above",
        "Ethereum price on Sep 3, 2026 $76,000 or above")
    assert cv.structural_match(pm, kx) is None


# ────────────────────────────────────────────────────────────────────────
# Fixtures: the league, the day and the side, read off the slug.
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug,league,date,kind,line", [
    ("mlb-nym-tb-2026-09-02", "mlb", "2026-09-02", "winner", None),
    ("mlb-sf-pit-2026-09-02-total-8pt5", "mlb", "2026-09-02", "total", 8.5),
    ("lal-rso-cel-2026-09-03-first-half-spread-away-1pt5",
     "lal", "2026-09-03", "spread", 1.5),
    ("chi1-cdl-cdu-2026-08-01-draw", "chi1", "2026-08-01", "winner", None),
    ("atp-harri-tsitsip-2026-09-02", "atp", "2026-09-02", "winner", None),
])
def test_a_fixture_slug_names_its_league_day_and_market(slug, league, date,
                                                        kind, line):
    fx = cv.parse_fixture({"slug": slug, "eventSlug": slug})
    assert fx and fx["league"] == league
    assert fx["date"] == date and fx["kind"] == kind and fx["line"] == line


def test_a_league_we_do_not_map_is_simply_not_a_fixture():
    assert cv.parse_fixture({"slug": "setkameua-se-ruslan-2026-09-03"}) is None
    assert cv.parse_fixture({"slug": "bitcoin-above-76k-on-september-3-2026"}) is None


@pytest.mark.parametrize("title,slug,claim", [
    ("New York Mets vs. Tampa Bay Rays", "mlb-nym-tb-2026-09-02",
     "New York Mets"),
    ("US Open ATP: Lloyd Harris vs Stefanos Tsitsipas",
     "atp-harri-tsitsip-2026-09-02", "Lloyd Harris"),
    ("Will CR Flamengo win on 2026-09-02?", "bra-fla-mir-2026-09-02-fla",
     "CR Flamengo"),
    ("Will CD Limache vs. CD Nublense end in a draw?",
     "chi1-cdl-cdu-2026-08-01-draw", "tie draw"),
    ("San Francisco Giants vs. Pittsburgh Pirates: O/U 8.5",
     "mlb-sf-pit-2026-09-02-total-8pt5", "over 8.5"),
    ("Spread: Tampa Bay Rays (-1.5)", "mlb-nym-tb-2026-09-02-spread-away-1pt5",
     "Tampa Bay Rays over 1.5"),
    ("1st Half Spread: RC Celta de Vigo (-1.5)",
     "lal-rso-cel-2026-09-03-first-half-spread-away-1pt5",
     "RC Celta de Vigo over 1.5"),
])
def test_the_polymarket_yes_claim_is_made_explicit(title, slug, claim):
    """Polymarket never labels its YES: the title is two team names and the
    reader is expected to know YES means the first one. Saying it out loud is
    what makes the Kalshi leg safe to pick."""
    pm = {"title": title, "slug": slug, "eventSlug": slug}
    fx = cv.parse_fixture(pm)
    assert fx is not None
    assert cv.pm_yes_claim(pm, fx) == claim


def test_the_event_code_is_recovered_from_a_kalshi_ticker():
    assert cv.event_code("KXMLBGAME-26SEP021840NYMTB") == "NYMTB"
    assert cv.event_code("KXCHLLDPGAME-26SEP02DEPNUB") == "DEPNUB"
    assert cv.event_code("KXBTCD-26SEP0221") == ""


def _leg(ticker, sub):
    return {"conditionId": ticker, "subtitle": sub, "title": sub}


def test_a_leg_is_only_picked_when_it_beats_the_runner_up():
    legs = [_leg("A-NYM", "New York M"), _leg("A-TB", "Tampa Bay")]
    picked = cv.pick_leg("New York Mets", legs)
    assert picked and picked[0]["conditionId"] == "A-NYM"


def test_a_tie_between_two_legs_is_answered_with_nothing():
    """Half of a fixture pairing is not half right. Comparing "Mets win" to
    "Rays win" gives two real prices, a large spread and an inverted claim."""
    legs = [_leg("A-X", "Real Madrid"), _leg("A-Y", "Barcelona")]
    assert cv.pick_leg("Getafe", legs) is None


def test_a_figure_decides_between_legs_that_read_identically():
    legs = [_leg("T-8", "Over 7.5 runs scored"),
            _leg("T-9", "Over 8.5 runs scored"),
            _leg("T-10", "Over 9.5 runs scored")]
    picked = cv.pick_leg("over 8.5", legs)
    assert picked and picked[0]["conditionId"] == "T-9"


def test_a_leg_must_beat_its_own_siblings_before_it_is_shown():
    """Caught on the live fixture: a question about Getafe scored Barcelona,
    Real Madrid and Atletico identically, the Getafe leg happened to be
    unquoted, and the panel would have shown Barcelona's price under a
    question about Getafe."""
    def cand(ticker, score):
        return {"market": {"conditionId": ticker,
                           "eventTicker": "KXLALIGA-27"}, "score": score}
    cands = [cand("KXLALIGA-27-BAR", 0.42), cand("KXLALIGA-27-RMA", 0.42),
             cand("KXLALIGA-27-ATM", 0.40)]
    assert not cv._beats_its_siblings(cands[0], cands)
    # The same leg, once the question actually names it, is fine.
    cands[0]["score"] = 0.60
    assert cv._beats_its_siblings(cands[0], cands)


def test_a_lone_market_has_no_siblings_to_beat():
    cand = {"market": {"conditionId": "X-1", "eventTicker": "X"}, "score": 0.4}
    assert cv._beats_its_siblings(cand, [cand])


# ────────────────────────────────────────────────────────────────────────
# Retrieval — the half that was actually broken.
# ────────────────────────────────────────────────────────────────────────


def test_aliases_translate_the_question_into_kalshi_vocabulary():
    tok = cv.expand_tokens(cv._tokens("Will Bitcoin dip to $45,000?"))
    assert "btc" in tok, "BTC and Bitcoin share no characters"
    assert {"low", "fall"} <= tok, "'dip' has to reach 'How low will it fall'"


_FAKE_SERIES = [
    {"ticker": "KXMLBGAME", "title": "Professional Baseball Game",
     "category": "Sports"},
    {"ticker": "KXMLBTOTAL", "title": "Pro Baseball Total Points",
     "category": "Sports"},
    {"ticker": "KXLALIGA", "title": "LA LIGA", "category": "Sports"},
    {"ticker": "KXBTCD", "title": "Bitcoin price Above/below",
     "category": "Crypto"},
    {"ticker": "KXBTCMINY", "title": "How low will Bitcoin fall this year?",
     "category": "Crypto"},
    {"ticker": "KXHIGHMIA", "title": "Highest temperature in Miami",
     "category": "Climate and Weather"},
]


def _fake_index():
    inv: dict[str, set[int]] = {}
    for i, r in enumerate(_FAKE_SERIES):
        for tok in cv._tokens(r["title"]):
            inv.setdefault(tok, set()).add(i)
    return _FAKE_SERIES, inv


@pytest.mark.parametrize("pm,want", [
    # The fixture route: nothing in "New York Mets vs. Tampa Bay Rays" says
    # baseball, and nothing in "Professional Baseball Game" says Mets. Only
    # the slug's league code connects them.
    ({"title": "New York Mets vs. Tampa Bay Rays",
      "slug": "mlb-nym-tb-2026-09-02"}, "KXMLBGAME"),
    ({"title": "San Francisco Giants vs. Pittsburgh Pirates: O/U 8.5",
      "slug": "mlb-sf-pit-2026-09-02-total-8pt5"}, "KXMLBTOTAL"),
    # The named-topic route: "LA LIGA" shares no word with a question about
    # one club.
    ({"title": "Will Levante win the 2026-27 LALIGA Championship?",
      "slug": "will-levante-win"}, "KXLALIGA"),
    ({"title": "Will Bitcoin dip to $45,000 by December 31, 2026?",
      "slug": "bitcoin-dip-45k"}, "KXBTCMINY"),
    # The token route still carries everything that does say its own words.
    ({"title": "Will the highest temperature in Miami be between 88-89F on "
               "September 2?", "slug": "highest-temperature-in-miami"},
     "KXHIGHMIA"),
])
def test_every_retrieval_route_finds_its_series(pm, want):
    rows, inv = _fake_index()
    got = [s["ticker"] for s in cv._candidate_series(pm, rows, inv)]
    assert want in got, f"{pm['title'][:40]} reached {got}"


def test_a_table_entry_that_does_not_exist_costs_nothing():
    """Every ticker a table proposes is checked against the live series index
    before it costs a request, so a stale entry is inert rather than a wasted
    round trip against a 404."""
    rows, inv = _fake_index()
    pm = {"title": "Will Arsenal win the Premier League?", "slug": "x"}
    got = [s["ticker"] for s in cv._candidate_series(pm, rows, inv)]
    assert "KXEPL" not in got     # not in this index, so never proposed


# ────────────────────────────────────────────────────────────────────────
# The comparison itself, when the comparable side is Kalshi's NO.
# ────────────────────────────────────────────────────────────────────────


def _no_side_compare(monkeypatch):
    """A `compare()` run whose match is against the Kalshi NO side."""
    import asyncio

    pm = {
        "conditionId": "0xpm", "title": "Will BTC be above $76,000 on Sep 3?",
        "probability": 60.0, "bid": 59.0, "ask": 61.0, "spread": 2.0,
        "closeTime": "2026-09-03T16:00:00Z",
        "volume24hUsd": 1.0, "liquidityUsd": 1.0,
    }
    kx = {
        "conditionId": "KXBTC-26SEP0317-T76000", "venue": "kalshi",
        "title": "Bitcoin price range on Sep 3, 2026?",
        "subtitle": "$75,999.99 or below",
        "eventTitle": "BTC price range on Sep 3, 2026 at 5pm EDT?",
        "eventTicker": "KXBTC-26SEP0317", "eventSlug": "KXBTC-26SEP0317",
        "probability": 35.0, "bid": 34.0, "ask": 36.0, "spread": 2.0,
        "closeTime": "2026-09-03T21:00:00Z",
        "volume24hUsd": 1.0, "liquidityUsd": 1.0, "seriesTicker": "KXBTC",
    }

    async def market(cid, **kw):
        return pm

    async def find_match(_pm):
        return {"market": kx, "score": 0.78, "side": "no",
                "for": ["same level"],
                "against": ["Compared against the NO side of the Kalshi market"],
                "series": {"ticker": "KXBTC"}}

    async def price_history(*a, **k):
        return {"points": [{"t": 1000, "p": 60.0}]}

    async def candles(*a, **k):
        return [{"t": 1000, "p": 35.0}]

    monkeypatch.setattr(t, "market", market)
    monkeypatch.setattr(t, "is_offline", lambda: False)
    monkeypatch.setattr(t, "price_history", price_history)
    monkeypatch.setattr(cv, "find_match", find_match)
    monkeypatch.setattr(kalshi, "candles", candles)
    return asyncio.run(cv.compare("0xpm", "1d"))


def test_a_no_side_match_inverts_every_number_it_shows(monkeypatch):
    """Kalshi's NO price is 100 minus its YES price, and so is the whole
    history line. Showing the YES numbers under a NO-side claim would put a
    confident, inverted spread on screen."""
    out = _no_side_compare(monkeypatch)
    k = out["kalshi"]
    assert k["side"] == "no"
    assert k["probability"] == 65.0          # 100 - 35
    assert (k["bid"], k["ask"]) == (64.0, 66.0)   # 100 - ask, 100 - bid
    assert out["series"][0]["k"] == 65.0      # the chart line too
    assert out["match"]["comparedSides"] == {"polymarket": "yes",
                                             "kalshi": "no"}
    assert "NO on" in (k["subtitle"] or "")


def test_the_spread_note_says_which_side_it_compared(monkeypatch):
    out = _no_side_compare(monkeypatch)
    # 65 (Kalshi NO) - 60 (Polymarket YES)
    assert out["spreadCents"] == 5.0
    assert "NO side" in out["spreadNote"]


def test_a_yes_side_match_is_left_exactly_as_it_was(monkeypatch):
    import asyncio

    pm = {"conditionId": "0xpm", "title": "Fed cuts 25bps in September?",
          "probability": 40.0, "bid": 39.0, "ask": 41.0, "spread": 2.0,
          "closeTime": "2026-09-16T00:00:00Z",
          "volume24hUsd": None, "liquidityUsd": None}
    kx = {"conditionId": "KXFEDDECISION-26SEP-C25", "venue": "kalshi",
          "title": "Will the Federal Reserve Cut rates by 25bps?",
          "subtitle": "Cut 25bps", "eventTitle": "Fed decision in Sep 2026?",
          "eventTicker": "KXFEDDECISION-26SEP", "eventSlug": "KXFEDDECISION-26SEP",
          "probability": 44.0, "bid": 43.0, "ask": 45.0, "spread": 2.0,
          "closeTime": "2026-09-16T18:59:00Z", "seriesTicker": "KXFEDDECISION",
          "volume24hUsd": None, "liquidityUsd": None}

    async def market(cid, **kw):
        return pm

    async def find_match(_pm):
        return {"market": kx, "score": 0.9, "side": "yes",
                "for": ["same leg"], "against": [],
                "series": {"ticker": "KXFEDDECISION"}}

    async def price_history(*a, **k):
        return {"points": [{"t": 1000, "p": 40.0}]}

    async def candles(*a, **k):
        return [{"t": 1000, "p": 44.0}]

    monkeypatch.setattr(t, "market", market)
    monkeypatch.setattr(t, "is_offline", lambda: False)
    monkeypatch.setattr(t, "price_history", price_history)
    monkeypatch.setattr(cv, "find_match", find_match)
    monkeypatch.setattr(kalshi, "candles", candles)
    import asyncio as _a
    out = _a.run(cv.compare("0xpm", "1d"))
    assert out["kalshi"]["side"] == "yes"
    assert out["kalshi"]["probability"] == 44.0
    assert out["series"][0]["k"] == 44.0
    assert out["match"]["comparedSides"] == {"polymarket": "yes",
                                             "kalshi": "yes"}
    assert out["spreadCents"] == 4.0


# ────────────────────────────────────────────────────────────────────────
# The Kalshi row now carries its event and its strike.
# ────────────────────────────────────────────────────────────────────────


def test_a_market_row_takes_the_subject_from_its_event():
    """A Kalshi market row on its own says "Tampa Bay wins" and nothing else —
    no opponent, no date, no sport. Everything a question could be matched on
    lives on the EVENT."""
    row = kalshi.market_row(
        {"ticker": "KXMLBGAME-26SEP021840NYMTB-TB", "title": "Tampa Bay wins",
         "yes_sub_title": "Tampa Bay", "status": "active",
         "yes_bid": 40, "yes_ask": 42,
         "expected_expiration_time": "2026-09-03T01:40:00Z",
         "close_time": "2026-09-05T22:40:00Z"},
        None,
        {"event_ticker": "KXMLBGAME-26SEP021840NYMTB",
         "title": "New York M vs Tampa Bay", "sub_title": "NYM vs TB (Sep 2)",
         "series_ticker": "KXMLBGAME"})
    assert "New York M vs Tampa Bay" in row["matchText"]
    assert "Tampa Bay wins" in row["matchText"]
    assert row["eventTicker"] == "KXMLBGAME-26SEP021840NYMTB"
    # The settlement MOMENT, not the padded close: the game is on Sep 2 and
    # Kalshi's close_time sits three days later so a postponement can settle.
    assert row["resolvesAt"] == "2026-09-03T01:40:00Z"
    assert row["closeTime"] == "2026-09-05T22:40:00Z"


def test_a_market_row_carries_the_strike_kalshi_states():
    row = kalshi.market_row(
        {"ticker": "KXHIGHMIA-26SEP02-B88.5", "title": "88-89 degrees?",
         "yes_sub_title": "88 to 89", "status": "active",
         "strike_type": "between", "floor_strike": 88, "cap_strike": 89,
         "yes_bid": 1, "yes_ask": 3},
        None,
        {"event_ticker": "KXHIGHMIA-26SEP02",
         "title": "Highest temperature in Miami on Sep 2, 2026?",
         "strike_date": "2026-09-03T05:00:00Z"})
    assert (row["strikeType"], row["floorStrike"], row["capStrike"]) == (
        "between", 88.0, 89.0)
    assert row["strikeDate"] == "2026-09-03T05:00:00Z"
    assert cv._kx_strike_threshold(row) == ("between", 88.0, 89.0)


def test_the_row_shape_is_unchanged_when_there_is_no_event():
    row = kalshi.market_row({"ticker": "KXTEST-1", "title": "Test?",
                             "yes_bid": 42, "yes_ask": 44, "status": "active"})
    assert row["eventTicker"] is None and row["strikeType"] is None
    assert row["matchText"] == "Test?"


@pytest.mark.parametrize("slug,title", [
    ("chi1-cdl-cdu-2026-08-01-halftime-result-draw",
     "CD Limache vs. CD Nublense: Draw at halftime?"),
    ("chi1-cdl-cdu-2026-08-01-second-half-result-home",
     "CD Limache to win the second half?"),
    ("chi1-cdl-cdu-2026-08-01-first-to-score-neither",
     "CD Limache vs. CD Nublense: Neither team to score first?"),
    ("chi1-cdl-cdu-2026-08-01-exact-score-1-0",
     "Exact Score: CD Limache 1 - 0 CD Nublense?"),
])
def test_a_fixture_market_we_cannot_name_is_refused(slug, title):
    """Found on a holdout run, and it is the reason the family table is an
    allow-list rather than a default. Falling through to the game winner paired
    "Draw at halftime?" with Kalshi's FULL-TIME "Tie is the result", and
    "Neither team to score first?" with "Deportes Limache wins" — four
    different questions, each with a real spread under it.

    Kalshi does list first-half and first-goal families. Until we have checked
    how it words those legs, the honest answer is no panel."""
    assert cv.parse_fixture({"slug": slug, "eventSlug": slug, "title": title}) is None


@pytest.mark.parametrize("tail", ["", "-fla", "-cdl", "-draw", "-tb"])
def test_a_moneyline_leg_is_still_recognised(tail):
    slug = f"chi1-cdl-cdu-2026-08-01{tail}"
    fx = cv.parse_fixture({"slug": slug, "eventSlug": slug})
    assert fx and fx["kind"] == "winner"


# ────────────────────────────────────────────────────────────────────────
# The listing date is not a settlement date.
#
# The regression that reached the owner: `compare()` fetched the market with
# its raw Gamma row attached, the matcher read `startDate` as the settlement
# day, and the September 2026 Fed question claimed to settle in May — four
# months from Kalshi's event, so the panel said "no Kalshi market looks like
# this question" for a pair it had matched correctly the day before.
# ────────────────────────────────────────────────────────────────────────


def test_gammas_listing_date_is_never_read_as_a_settlement_date():
    pm = {
        "title": "Will the Fed increase interest rates by 25 bps after the "
                 "September 2026 meeting?",
        "slug": "will-the-fed-increase-interest-rates-by-25-bps-after-the-"
                "september-2026-meeting-649",
        "closeTime": "2026-09-16T00:00:00Z",
        # Gamma puts this on every row. It is when the market was LISTED.
        "_raw": {"startDate": "2026-05-13T21:23:15.981Z"},
    }
    day, exact = cv.pm_settlement_date(pm)
    assert day.startswith("2026-09"), (
        f"the listing date leaked into the settlement date: {day}")
    assert not exact, "a padded close time must not claim to be exact"


def test_the_fed_question_still_matches_with_a_raw_row_attached():
    pm = {
        "title": "Will the Fed increase interest rates by 25 bps after the "
                 "September 2026 meeting?",
        "closeTime": "2026-09-16T00:00:00Z",
        "_raw": {"startDate": "2026-05-13T21:23:15.981Z"},
    }
    kx = {"conditionId": "KXFEDDECISION-26SEP-H25",
          "matchText": "Fed decision in Sep 2026? Hike 25bps",
          "subtitle": "Hike 25bps", "eventTicker": "KXFEDDECISION-26SEP"}
    v = cv.structural_match(pm, kx)
    assert v and v["side"] == "yes" and v["score"] >= cv.STRONG


# ────────────────────────────────────────────────────────────────────────
# Timed crypto up/down — the family the owner actually trades.
#
# Both venues state the window exactly, so there is nothing to infer:
#   Polymarket  cpc-btc-updown-15m-2026-09-03-0400z   asset, length, window START in UTC
#   Kalshi      KXBTC15M-26SEP030015-15     asset, and the window END in ET
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("slug,asset,minutes,end", [
    ("cpc-btc-updown-15m-2026-09-03-0400z", "BTC", 15, 1788408900),
    ("cpc-eth-updown-5m-2026-09-04-0355z", "ETH", 5, 1788494400),
    ("cpc-doge-updown-1h-2026-09-03-0400z", "DOGE", 60, 1788411600),
])
def test_a_timed_updown_slug_states_its_whole_window(slug, asset, minutes, end):
    up = cv.parse_updown({"slug": slug})
    assert up["asset"] == asset and up["minutes"] == minutes
    assert up["endTs"] == end
    assert up["series"] == f"KX{asset}15M"


@pytest.mark.parametrize("slug", [
    "mlb-nym-tb-2026-09-02",
    "bitcoin-above-76k-on-september-3-2026",
    "cpc-wat-updown-5m-2026-09-04-0355z",          # an asset we do not map
])
def test_anything_else_is_not_a_timed_window(slug):
    assert cv.parse_updown({"slug": slug}) is None


@pytest.mark.parametrize("slug", [
    "btc-updown-15m-1788408000",                # the epoch stamp is not this venue's
    "cpc-btc-updown-15m-2026-13-03-0400z",      # shaped like a date, and is not one
])
def test_a_slug_that_only_looks_timed_is_not_a_window(slug):
    assert cv.parse_updown({"slug": slug}) is None


def test_the_event_slug_of_a_window_reads_the_same_as_its_market():
    a = cv.parse_updown({"slug": "cpc-btc-updown-15m-2026-09-03-0400z"})
    assert a == cv.parse_updown({"slug": "btc-updown-15m-2026-09-03-0400z"})
    assert a["startTs"] == 1788408000


# The contract type comes first on a market slug and only the winners are the game.
@pytest.mark.parametrize("slug,event,league,date", [
    ("aec-nfl-lac-ten-2025-11-02", "nfl-lac-ten-2025-11-02", "nfl", "2025-11-02"),
    ("atc-epl-mnu-ast-2026-11-08-draw", "epl-mnu-ast-2026-11-08", "epl", "2026-11-08"),
    ("aec-cfb-ala-aub-2026-11-28", "cfb-ala-aub-2026-11-28", "cfb", "2026-11-28"),
    ("aec-sea-mil-int-2026-11-08", "sea-mil-int-2026-11-08", "sea", "2026-11-08"),
])
def test_a_game_winner_market_is_its_fixture(slug, event, league, date):
    fx = cv.parse_fixture({"slug": slug, "eventSlug": event})
    assert (fx["league"], fx["date"], fx["kind"]) == (league, date, "winner")


@pytest.mark.parametrize("slug,event", [
    ("tsc-nfl-lac-ten-2025-11-02-41pt5", "nfl-lac-ten-2025-11-02"),
    ("tec-nfl-lac-ten-2025-11-02-over", "nfl-lac-ten-2025-11-02"),
    ("aachc-nfl-lac-ten-2025-11-02", "nfl-lac-ten-2025-11-02"),
])
def test_another_contract_on_a_fixture_is_not_its_winner(slug, event):
    """A total or a prop carries the bare fixture as its event slug. Falling through
    to it would compare this question with Kalshi's game winner."""
    assert cv.parse_fixture({"slug": slug, "eventSlug": event}) is None


def _us_game(slug, long_team, code):
    """A real game winner's fields: its question never says which team YES is."""
    return {"slug": slug, "eventSlug": None, "closeTime": "2026-11-19T00:00:00Z",
            "title": "Who will win in the upcoming basketball event PHI 76ers vs DET "
                     "Pistons scheduled for November 5, 2026 at 12:00 AM UTC?",
            "yesTeam": long_team, "yesTeamCode": code}


def _kx_leg(event, code, name, probability):
    return {"conditionId": f"{event}-{code}", "eventTicker": event, "title": f"{name} wins",
            "subtitle": name, "probability": probability}


_PHIDET = "KXNBAGAME-26NOV04PHIDET"


@pytest.mark.parametrize("team,code,want", [
    ("Philadelphia 76ers", "phi", "-PHI"),
    ("Detroit Pistons", "det", "-DET"),
])
def test_a_game_winner_pairs_with_the_leg_of_the_team_yes_stands_for(team, code, want):
    """Pairing the other leg would show two real prices and an inverted claim."""
    pm = _us_game("aec-nba-phi-det-2026-11-04", team, code)
    fx = cv.parse_fixture(pm)
    legs = [_kx_leg(_PHIDET, "PHI", "Philadelphia", 60.0), _kx_leg(_PHIDET, "DET", "Detroit", 40.0)]
    v = cv._fixture_verdict(pm, fx, legs)
    assert v and v["market"]["conditionId"].endswith(want)


def test_a_nickname_alone_still_finds_its_leg_by_the_code():
    """Some rows name a team "Senators", which shares no word with "Ottawa"."""
    pm = _us_game("aec-nhl-ott-buf-2026-11-04", "Senators", "ott")
    event = "KXNHLGAME-26NOV04OTTBUF"
    v = cv._fixture_verdict(pm, cv.parse_fixture(pm), [
        _kx_leg(event, "OTT", "Ottawa", 55.0), _kx_leg(event, "BUF", "Buffalo", 45.0)])
    assert v and v["market"]["conditionId"].endswith("-OTT")


def test_a_team_that_names_neither_leg_is_refused_not_guessed():
    pm = _us_game("aec-nba-phi-det-2026-11-04", "Seattle Supersonics", "sea")
    fx = cv.parse_fixture(pm)
    assert cv._fixture_verdict(pm, fx, [
        _kx_leg(_PHIDET, "PHI", "Philadelphia", 60.0),
        _kx_leg(_PHIDET, "DET", "Detroit", 40.0)]) is None


def test_with_no_team_on_the_row_a_game_winners_question_claims_nothing():
    pm = _us_game("aec-nba-phi-det-2026-11-04", None, None)
    assert cv.pm_yes_claim(pm, cv.parse_fixture(pm)) is None


def test_the_eastern_clock_reads_the_way_both_venues_write_these():
    assert cv.et_clock("2026-09-03T04:15:00Z") == "12:15AM ET"
    assert cv.et_clock(1788408000) == "12:00AM ET"
    assert cv.et_clock("2026-09-03T16:00:00Z") == "12:00PM ET"


def _kx_window(ticker, series, close):
    return {"conditionId": ticker, "seriesTicker": series,
            "eventTicker": ticker.rsplit("-", 1)[0],
            "subtitle": "Target Price: $77,677.78",
            "matchText": "BTC 15 min target", "resolvesAt": close,
            "closeTime": close, "probability": 84.5}


def test_a_window_that_ends_with_kalshis_window_is_the_same_question():
    up = cv.parse_updown({"slug": "cpc-btc-updown-15m-2026-09-03-0400z"})
    kx = _kx_window("KXBTC15M-26SEP030015-15", "KXBTC15M",
                    "2026-09-03T04:15:00Z")
    v = cv.updown_match(up, kx)
    assert v and v["side"] == "yes" and v["score"] >= cv.STRONG
    assert any("12:00AM to 12:15AM ET" in f for f in v["for"])


def test_a_five_minute_window_ending_at_the_same_instant_is_not_a_match():
    """The sharpest near miss on either venue. Polymarket's 12:10-12:15 window
    ENDS exactly when Kalshi's 12:00-12:15 window ends, and on a quiet fifteen
    minutes the two prices look alike — so an end-only join would pair them and
    print a spread between two different questions."""
    up = cv.parse_updown({"slug": "cpc-btc-updown-5m-2026-09-03-0410z"})
    assert up["endTs"] == 1788408900          # the same instant
    kx = _kx_window("KXBTC15M-26SEP030015-15", "KXBTC15M",
                    "2026-09-03T04:15:00Z")
    assert cv.updown_match(up, kx) is None


def test_a_fifteen_minute_window_kalshi_is_not_quoting_is_not_a_match():
    up = cv.parse_updown({"slug": "cpc-btc-updown-15m-2026-09-03-0430z"})   # ends 12:45
    kx = _kx_window("KXBTC15M-26SEP030015-15", "KXBTC15M",
                    "2026-09-03T04:15:00Z")
    assert cv.updown_match(up, kx) is None


def test_the_asset_has_to_match_too():
    up = cv.parse_updown({"slug": "cpc-btc-updown-15m-2026-09-03-0400z"})
    kx = _kx_window("KXETH15M-26SEP030015-15", "KXETH15M",
                    "2026-09-03T04:15:00Z")
    assert cv.updown_match(up, kx) is None


def test_a_timed_window_is_routed_to_its_kalshi_series():
    rows, inv = _fake_index()
    rows = rows + [{"ticker": "KXBTC15M", "title": "Bitcoin price up down",
                    "category": "Crypto"}]
    inv = dict(inv)
    for tok in cv._tokens("Bitcoin price up down"):
        inv.setdefault(tok, set()).add(len(rows) - 1)
    pm = {"title": "Bitcoin Up or Down - September 3, 12:00AM-12:15AM ET",
          "slug": "cpc-btc-updown-15m-2026-09-03-0400z"}
    got = [s["ticker"] for s in cv._candidate_series(pm, rows, inv)]
    assert "KXBTC15M" in got


def test_structural_matching_owns_the_timed_family_outright():
    """A near-miss window is the failure mode with the sharpest teeth here,
    because the prices genuinely do look alike. So the text scorer never gets
    a vote: an up/down market matches on the window or not at all."""
    pm = {"title": "Bitcoin Up or Down - September 3, 12:10AM-12:15AM ET",
          "slug": "cpc-btc-updown-5m-2026-09-03-0410z", "closeTime": "2026-09-03T04:15:00Z"}
    kx = _kx_window("KXBTC15M-26SEP030015-15", "KXBTC15M",
                    "2026-09-03T04:15:00Z")
    assert cv.structural_match(pm, kx) is None
