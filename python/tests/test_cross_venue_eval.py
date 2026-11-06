"""The cross-venue matcher, measured — not asserted about in the abstract.

`test_cross_venue.py` pins the individual rules. This file answers the only
question the user actually asked: *does it find the Kalshi market when one
exists, and does it stay quiet when one does not?*

IT RUNS THROUGH `compare()`, WHICH IS THE RPC ENTRY POINT, and that is not a
detail. An earlier version of this file called `find_match` directly and
scored 1.00 while the shipped app found nothing for the September 2026 Fed
question: `compare()` was the one caller that fetched the market with its raw
Gamma row attached, the matcher read Gamma's `startDate` — the LISTING date,
four months before settlement — as the settlement day, and every internal
function still behaved perfectly on the dicts this file was handing it. An
evaluation that does not exercise the real entry point measures the harness.

Two frozen fixtures:

* ``cross_venue_truth.json`` — real Polymarket questions pulled from the
  Discover columns on 2026-09-02/03, each hand-labelled with the ONE Kalshi
  market that asks the same question, or with ``null``. The labels are
  deliberately conservative: a near miss (Kalshi's 15-minute BTC window
  against Polymarket's 5-minute one, "by Dec 31" against "at this event") is
  labelled ``null``, because pairing those would be exactly the confident
  wrong claim this module exists to prevent. Around half have no counterpart
  at all, which is the honest shape of the two venues' overlap.
* ``cross_venue_kalshi_snapshot.json.gz`` — the Kalshi side of that same
  moment: the full series index plus the open events, with their markets, of
  every series the retrieval routes reach. Public, read-only data.

Frozen on purpose. Measured against the live API these numbers drift by the
hour as fixtures settle and 15-minute windows roll, and a bar that moves is
not a bar.

The recorded result on 2026-09-02/03, over these same questions:

    retrieval                       recall   precision
    series titles + market sweep     0.10      1.00      (live, 3 / 31)
    this module                      1.00      1.00      (frozen, 33 / 33)

Two labelled counterparts are excluded from recall: the Getafe and Flamengo
markets had no quote left when the snapshot was taken, and a market with no
resting order is skipped by design rather than compared. They are still
required not to produce some OTHER match.

The bars below sit well under the achieved figures, and deliberately so:
these rules were developed against this fixture, so a perfect score on it is
not evidence of a perfect matcher. It is a floor to regress against, and the
live check (`npm run py:live`) is what samples fresh markets.

PRECISION IS THE ONE THAT MUST NOT SLIP: a missed match is a quiet panel, a
wrong match is a fictional arbitrage with a number under it.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import pathlib

import pytest

import cross_venue as cv
import kalshi
import terminal as t

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# Set from the measured run, with room to move. Raise them if the matcher
# improves; do NOT lower them to make a change pass.
MIN_RECALL = 0.80
MIN_PRECISION = 1.00

# Gamma puts the day a market was LISTED on every row. It is not a settlement
# date and never was — the September 2026 Fed question carries May 2026 — and
# reading it as one is what silently broke the shipped panel. Every case is
# handed one, so that regression cannot pass this file again.
_LISTING_DATE = "2026-05-13T21:23:15.981Z"


@pytest.fixture(scope="module")
def truth() -> dict:
    with open(FIXTURES / "cross_venue_truth.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def snapshot() -> dict:
    with gzip.open(FIXTURES / "cross_venue_kalshi_snapshot.json.gz",
                   "rt", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def frozen_venues(monkeypatch, snapshot):
    """Serve the recorded read instead of the network, on BOTH sides.

    Patched at the boundary functions only, so everything above them —
    `compare`, `find_match`, the row normalisation, the strike fields, the
    event join, the spread gate — is the real code path and not a stub.
    """
    rows = [dict(r, settlementSources=[], contractUrl=None)
            for r in snapshot["series"]]

    async def series_index():
        return rows, True

    async def events_in_series(series_ticker: str, limit: int = 200):
        out = []
        for ev in snapshot["events"].get(str(series_ticker).upper(), []):
            for raw in ev.get("markets") or []:
                if raw.get("mve_collection_ticker"):
                    continue
                row = kalshi.market_row(raw, "2026-09-02T00:00:00Z", ev)
                if row:
                    row["seriesTicker"] = (row["seriesTicker"]
                                           or str(series_ticker).upper())
                    out.append(row)
        return out

    async def candles(*a, **k):
        return []

    async def price_history(*a, **k):
        return {"points": []}

    monkeypatch.setattr(kalshi, "series_index", series_index)
    monkeypatch.setattr(kalshi, "events_in_series", events_in_series)
    monkeypatch.setattr(kalshi, "candles", candles)
    monkeypatch.setattr(t, "price_history", price_history)
    monkeypatch.setattr(t, "is_offline", lambda: False)
    monkeypatch.setattr(cv, "_index", None)
    yield
    monkeypatch.setattr(cv, "_index", None)


def _pm(case: dict) -> dict:
    return {
        "conditionId": case["conditionId"],
        "title": case["title"],
        "slug": case["slug"],
        "eventSlug": case["eventSlug"],
        "closeTime": case["closeTime"],
        "category": case["category"],
        "probability": 50.0,
        "bid": 49.0, "ask": 51.0, "spread": 2.0,
        "volume24hUsd": None, "liquidityUsd": None,
        # See _LISTING_DATE. Handed to the matcher on purpose.
        "_raw": {"startDate": _LISTING_DATE, "slug": case["slug"]},
    }


def run_compare(case: dict, monkeypatch) -> dict:
    async def market(cid, **kw):
        return _pm(case)

    monkeypatch.setattr(t, "market", market)
    return asyncio.run(cv.compare(case["conditionId"], "1d"))


def quoted_tickers(snapshot: dict) -> set[str]:
    """Every Kalshi market in the snapshot that actually had a two-sided
    quote. A market with no resting order cannot be compared to anything —
    the matcher skips it by design — so a labelled counterpart that was
    unquoted at capture time is not a fair test of RETRIEVAL, and it is
    excluded from the recall denominator rather than quietly counted."""
    out = set()
    for events in snapshot["events"].values():
        for ev in events:
            for raw in ev.get("markets") or []:
                row = kalshi.market_row(raw, "2026-09-02T00:00:00Z", ev)
                if row and row["probability"] is not None:
                    out.add(row["conditionId"])
    return out


@pytest.fixture(scope="function")
def measured(truth, snapshot, monkeypatch) -> dict:
    """Run every case through `compare()` and tally it."""
    quoted = quoted_tickers(snapshot)
    tp = fp = fn = tn = 0
    detail = []
    for c in truth["cases"]:
        out = run_compare(c, monkeypatch)
        found = (out.get("kalshi") or {}).get("ticker")
        want = c["expectedKalshiTicker"]
        scorable = (want is None) or (want in quoted)
        if want and found == want:
            why = "found"
        elif want and found:
            why = "wrong-candidate-preferred"
        elif want and not found:
            why = "not-found" if scorable else "counterpart-unquoted"
        elif found:
            why = "fictional-match"
        else:
            why = "correctly-absent"
        if scorable:
            if why == "found":
                tp += 1
            elif why in ("wrong-candidate-preferred", "fictional-match"):
                fp += 1
            elif why == "not-found":
                fn += 1
            else:
                tn += 1
        elif why == "wrong-candidate-preferred":
            # Kalshi lists the question but has no price on it. Answering with
            # a DIFFERENT market is still a fictional match.
            fp += 1
        detail.append({"case": c, "result": out, "outcome": why,
                       "found": found, "scorable": scorable})
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "detail": detail,
        "recall": tp / (tp + fn) if (tp + fn) else 0.0,
        "precision": tp / (tp + fp) if (tp + fp) else 1.0,
    }


def test_the_fixture_is_a_real_and_balanced_sample(truth):
    cases = truth["cases"]
    assert len(cases) >= 60
    have = [c for c in cases if c["expectedKalshiTicker"]]
    # "No Kalshi equivalent" is the common answer, and a set that did not
    # contain plenty of them would measure nothing about false positives.
    assert 20 <= len(have) <= len(cases) - 20
    assert all(c["why"] for c in cases), "every label states its reasoning"


def test_recall_clears_the_bar(measured):
    assert measured["recall"] >= MIN_RECALL, (
        f"recall fell to {measured['recall']:.3f}; misses: "
        + "; ".join(d["case"]["title"][:40] for d in measured["detail"]
                    if d["outcome"] in ("not-found", "wrong-candidate-preferred")))


def test_precision_never_slips(measured):
    """A missed match is a quiet panel. A wrong match is a confident,
    entirely fictional edge with a price under it, which is the failure this
    whole module is built around."""
    bad = [d for d in measured["detail"]
           if d["outcome"] in ("fictional-match", "wrong-candidate-preferred")]
    assert measured["precision"] >= MIN_PRECISION, (
        "precision dropped: "
        + "; ".join(f"{d['case']['title'][:40]} -> {d['found']}" for d in bad))


def test_nothing_is_invented_for_a_question_kalshi_does_not_list(measured):
    """About half the set has no counterpart. Every one of them must come back
    as no match — not as the nearest thing on the exchange."""
    invented = [d for d in measured["detail"]
                if d["outcome"] == "fictional-match"]
    assert not invented, "; ".join(
        f"{d['case']['title'][:50]} -> {d['found']}" for d in invented)


def test_the_listing_date_is_never_read_as_a_settlement_date(measured):
    """The regression that reached the owner. Every case above is handed a
    Gamma row whose `startDate` is May 2026; if that is read as the settlement
    day again, the September Fed question loses its Kalshi event by four
    months and this file's recall collapses. Named explicitly so the failure
    says what it is."""
    fed = [d for d in measured["detail"]
           if "Fed increase interest rates by 25 bps" in d["case"]["title"]]
    assert fed, "the Fed regression case is missing from the fixture"
    for d in fed:
        assert d["found"] == d["case"]["expectedKalshiTicker"], (
            f"the September 2026 Fed question found {d['found']!r} — the "
            f"listing date is being read as a settlement date again")


def test_every_reported_match_carries_its_reasoning(measured):
    for d in measured["detail"]:
        m = d["result"].get("match")
        if not m:
            continue
        assert m["for"], f"{d['case']['title']}: a match with no reasons for it"
        assert d["result"]["kalshi"]["ticker"]
        assert m["comparedSides"]["polymarket"] == "yes"
        assert m["comparedSides"]["kalshi"] in ("yes", "no")


def test_a_match_against_the_no_side_says_so(measured):
    """An inverted comparison is allowed — Kalshi lists plenty of levels from
    the other direction — but never silently."""
    for d in measured["detail"]:
        m = d["result"].get("match")
        if m and m["comparedSides"]["kalshi"] == "no":
            assert any("NO side" in a for a in m["against"]), (
                f"{d['case']['title']}: compared the NO side without saying so")
            assert "NO on" in (d["result"]["kalshi"]["subtitle"] or "")


def test_a_spread_only_ever_prints_under_a_strong_match(measured):
    for d in measured["detail"]:
        m = d["result"].get("match")
        if m and m["confidence"] != "strong":
            assert d["result"]["spreadCents"] is None, (
                f"{d['case']['title']}: a {m['confidence']} match printed a "
                f"spread")


def test_a_family_we_understand_gets_a_note_that_says_what_is_different(measured):
    """"No Kalshi market looks like this question" is true and useless on the
    timed crypto markets: the user knows both venues list Bitcoin up/down. The
    note has to say the windows do not line up, and which one Kalshi is
    actually quoting."""
    checked = 0
    for d in measured["detail"]:
        want = d["case"].get("expectedNoteContains")
        if not want:
            continue
        checked += 1
        note = d["result"].get("note") or ""
        assert d["result"]["kalshi"] is None, (
            f"{d['case']['title']}: expected no match")
        assert want in note, f"{d['case']['title']}: note was {note!r}"
        assert note != cv.NO_MATCH_NOTE, (
            f"{d['case']['title']}: fell back to the generic note")
    assert checked >= 4, "the timed-crypto note cases are missing"
