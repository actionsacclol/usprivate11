"""Kalshi — a second venue, read-only.

Why this exists: Polymarket and Kalshi list overlapping questions, and seeing
the SAME question priced on both is useful context — a second opinion from a
different pool of traders. It needs no trust, because the user can open the
Kalshi page and check the number themselves.

This is REFERENCE DATA. The app reads Kalshi and never trades on it, and the
difference between the two prices is presented as information about the two
markets, not as an opportunity.

Three rules specific to a second venue.

1. **NEVER BLUR THE VENUES.** Kalshi is a CFTC-regulated US exchange;
   Polymarket is not the same thing legally, and their fees, settlement
   guarantees and resolution sources differ. Every row carries its venue, and
   the comparison panel states the difference rather than implying the two
   numbers are interchangeable.

2. **A MATCH IS A CLAIM, NOT A FACT.** Nothing tells us these two markets ask
   the same question; we are inferring it from words and dates. So a match
   carries a confidence and the matched title verbatim, and below the
   confidence bar we refuse to print a spread at all — a made-up "edge" on a
   mismatched pair is worse than no panel.

3. **PUBLIC ENDPOINTS ONLY.** No key, no auth, no orders. Everything here is
   the read side of a venue we do not trade on from this app.

Requests ride the SAME gate as the Polymarket providers (see terminal._HOSTS),
so the offline switch, the 429 park and the privacy panel's counters all cover
Kalshi automatically.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import terminal as term
from terminal import _fetch, _now_iso, _sentinel_free, num

logger = logging.getLogger("krypt.kalshi")

# The series index is one broad fetch, cached hard and pulled only when the
# user actually opens a cross-venue panel.
TTL_MARKET = 20.0
TTL_CANDLES = 120.0



def _from_cents(v: Any) -> Optional[float]:
    """A field Kalshi names in whole cents (`yes_bid: 94`). Never rescaled.

    The unit is in the field name, so there is nothing to infer: `1` here is
    1¢. Reading it as $1.00 turned a genuine penny quote into 100¢, which
    `_sentinel_free` then dropped as an empty book — a real, tradeable market
    reported as unpriced.
    """
    n = num(v)
    if n is None:
        return None
    return _sentinel_free(round(n, 2))


def _from_dollars(v: Any) -> Optional[float]:
    """A field Kalshi names in dollars (`yes_bid_dollars: "0.9400"`)."""
    n = num(v)
    if n is None:
        return None
    return _sentinel_free(round(n * 100.0, 2))


def _cents(v: Any) -> Optional[float]:
    """A field whose unit is NOT in its name — the candle tiers, where the
    same key carries "0.94" from the historical tier and 94 from the live one.

    Here the unit genuinely has to be read off the value, so read the thing
    that actually distinguishes them: a written decimal point. "0.94" and
    "1.0000" are dollars; a bare 1 or 94 is cents. The old test — "≤1.5 means
    dollars" — had no way to tell 1¢ from $1.00 and always chose $1.00.

    Neither 0 nor 100 is a real quote, so the same empty-book scrub applies as
    on Polymarket.
    """
    n = num(v)
    if n is None:
        return None
    written_decimal = isinstance(v, str) and "." in v
    if abs(n) <= 1.5 and (written_decimal or not float(n).is_integer()):
        n *= 100.0
    return _sentinel_free(round(n, 2))


def _iso(v: Any) -> Optional[str]:
    return v if (v and isinstance(v, str)) else None


def _hours_to(close_iso: Optional[str]) -> Optional[float]:
    return term._hours_to(close_iso)


def _strike(v: Any) -> Optional[float]:
    """A Kalshi strike, as a number. Distinct from a price: never scaled and
    never scrubbed — 0 and 100 are perfectly good strikes."""
    return num(v)


def market_row(m: dict, observed_at: Optional[str] = None,
               event: Optional[dict] = None) -> Optional[dict]:
    """One Kalshi market → our MarketSummary shape, with venue='kalshi'.

    ``event`` is the market's parent event when we already have it (the
    ``/events?with_nested_markets`` route). It carries the two things a market
    row on its own does NOT: the fixture or subject in words ("New York M vs
    Tampa Bay") and the settlement MOMENT (``strike_date``). Both are load
    bearing for cross-venue matching — see events_in_series.
    """
    if not isinstance(m, dict):
        return None
    ticker = str(m.get("ticker") or "").strip()
    if not ticker:
        return None
    ev = event if isinstance(event, dict) else {}

    # Every quote field on a market row names its own unit, so each is read
    # with the helper for that unit and nothing is inferred from the value.
    yes_bid = _from_cents(m.get("yes_bid"))
    if yes_bid is None:
        yes_bid = _from_dollars(m.get("yes_bid_dollars"))
    yes_ask = _from_cents(m.get("yes_ask"))
    if yes_ask is None:
        yes_ask = _from_dollars(m.get("yes_ask_dollars"))
    last = _from_cents(m.get("last_price"))
    if last is None:
        last = _from_dollars(m.get("last_price_dollars"))

    # The two sides are one book mirrored, but only derive a missing side from
    # a PRESENT counterpart — deriving from a None manufactures a 100¢ quote
    # out of an empty side, which is the exact lie we are avoiding.
    no_bid = _from_cents(m.get("no_bid")) or _from_dollars(m.get("no_bid_dollars"))
    no_ask = _from_cents(m.get("no_ask")) or _from_dollars(m.get("no_ask_dollars"))
    if yes_ask is None and no_bid is not None:
        yes_ask = _sentinel_free(round(100.0 - no_bid, 2))
    if yes_bid is None and no_ask is not None:
        yes_bid = _sentinel_free(round(100.0 - no_ask, 2))

    spread = (round(yes_ask - yes_bid, 2)
              if (yes_bid is not None and yes_ask is not None) else None)
    mid = (round((yes_ask + yes_bid) / 2.0, 2)
           if spread is not None else None)
    prob = mid if mid is not None else last

    close_time = _iso(m.get("close_time")) or _iso(m.get("expiration_time"))
    status = str(m.get("status") or "").lower()
    if status in ("finalized", "settled"):
        norm_status = "settled"
    elif status in ("closed",):
        norm_status = "closed"
    elif status in ("active", "open", "initialized"):
        norm_status = "open"
    else:
        norm_status = "unknown"

    result = str(m.get("result") or "").lower()
    resolved = "yes" if result == "yes" else ("no" if result == "no" else None)

    vol24 = num(m.get("volume_24h"))
    if vol24 is None:
        vol24 = num(m.get("volume_24h_fp"))
    vol = num(m.get("volume")) or num(m.get("volume_fp"))
    liq = num(m.get("liquidity_dollars"))
    if liq is None:
        lc = num(m.get("liquidity"))
        liq = round(lc / 100.0, 2) if lc is not None else None

    obs = observed_at or _now_iso()
    stamp = {"source": "kalshi", "observedAt": obs, "ageSec": term._age_sec(obs)}
    sources = {}
    for field, val in (("probability", prob), ("bid", yes_bid), ("ask", yes_ask),
                       ("spread", spread), ("mid", mid), ("volume24h", vol24),
                       ("liquidity", liq), ("closeTime", close_time),
                       ("status", norm_status)):
        if val is not None:
            sources[field] = dict(stamp)

    unknowns = []
    if prob is None:
        unknowns.append({
            "field": "probability",
            "reason": ("Kalshi has no quote on this market — neither side of "
                       "the book has a resting order."),
        })

    title = (str(m.get("title") or "").strip()
             or str(m.get("subtitle") or "").strip() or ticker)
    sub = str(m.get("yes_sub_title") or m.get("subtitle") or "").strip()

    event_ticker = str(m.get("event_ticker") or ev.get("event_ticker") or "")
    event_title = str(ev.get("title") or "").strip()
    event_sub = str(ev.get("sub_title") or "").strip()

    # WHEN this actually settles. `close_time` is not it: Kalshi pads a game's
    # close by days so a postponed fixture can still settle, and Polymarket
    # pads its own end date the same way. Comparing two padded dates makes a
    # same-day pair look a week apart, which is what buried every sports
    # pairing. `strike_date` (threshold events) and `expected_expiration_time`
    # (fixtures) are the venue's own statement of the settlement moment.
    resolves_at = (_iso(ev.get("strike_date"))
                   or _iso(m.get("expected_expiration_time"))
                   or _iso(m.get("expiration_time"))
                   or close_time)

    # The matchable text, richest first. The event title is where the SUBJECT
    # lives on Kalshi ("New York M vs Tampa Bay", "BTC price on Sep 3, 2026");
    # the market title is often only the leg ("Tampa Bay wins").
    parts = [p for p in (event_title, event_sub, title, sub) if p]
    seen: set[str] = set()
    match_bits: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        match_bits.append(p)

    return {
        "conditionId": ticker,          # the terminal's entity id, per venue
        "venue": "kalshi",
        "title": title,
        # Kalshi splits the question across title + the YES leg's subtitle
        # ("Fed decision in September" / "25 bps decrease"). Matching needs
        # both, so carry the combined form explicitly.
        "subtitle": sub or None,
        "noSubtitle": str(m.get("no_sub_title") or "").strip() or None,
        "matchText": " ".join(match_bits).strip(),
        "eventTicker": event_ticker or None,
        "eventTitle": event_title or None,
        "eventSubtitle": event_sub or None,
        # The structured question, when Kalshi states one. These are the
        # fields the structural matcher compares instead of guessing at words.
        "strikeType": str(m.get("strike_type") or "").lower() or None,
        "floorStrike": _strike(m.get("floor_strike")),
        "capStrike": _strike(m.get("cap_strike")),
        "strikeDate": _iso(ev.get("strike_date")),
        "resolvesAt": resolves_at,
        "slug": ticker,
        "eventSlug": event_ticker or None,
        "seriesTicker": str(m.get("series_ticker")
                            or ev.get("series_ticker") or "") or None,
        "category": str(m.get("category") or "").lower() or None,
        "status": norm_status,
        "outcomes": ["Yes", "No"],
        "probability": prob,
        "bid": yes_bid, "ask": yes_ask, "spread": spread, "mid": mid,
        "volume24hUsd": vol24,
        "volumeTotalUsd": vol,
        "liquidityUsd": liq,
        # Kalshi publishes no field equivalent to Gamma's oneDay/oneHour price
        # change. It could be approximated from `previous_price`, but that is a
        # different measurement over a different window, and quietly labelling
        # it "24h change" beside a Polymarket row that means something precise
        # is exactly the venue-blurring this module refuses to do.
        "priceChange24h": None,
        "priceChange1h": None,
        "closeTime": close_time,
        "hoursToClose": _hours_to(close_time),
        "negRisk": None,
        "tickSize": None,
        "minOrderSize": None,
        "yesTokenId": None, "noTokenId": None,
        "resolvedOutcome": resolved,
        "sources": sources,
        "unknowns": unknowns,
    }


async def market(ticker: str) -> Optional[dict]:
    t = str(ticker or "").strip().upper()
    if not t:
        return None
    data, observed = await _fetch("kalshi", f"/markets/{t}", None, ttl=TTL_MARKET)
    if not isinstance(data, dict):
        return None
    raw = data.get("market") if isinstance(data.get("market"), dict) else data
    return market_row(raw, observed)


# Kalshi groups markets under a SERIES (the question family: "Fed decision",
# "Bitcoin price above/below"). These are the categories the series list is
# published under; there is no "all series" call.
CATEGORIES = [
    "Economics", "Politics", "Financials", "Crypto", "Climate and Weather",
    "Companies", "Sports", "Entertainment", "Health",
    "Science and Technology", "World",
]

TTL_SERIES = 1800.0


async def series_index() -> tuple[list[dict], bool]:
    """Every Kalshi SERIES, with its title and settlement sources.

    This is the index cross-venue matching runs against, and choosing it over
    a market sweep is the whole reason the feature is affordable. Measured
    2026-08-25:

      * `/markets?status=open` is useless for discovery — the first 4,000 rows
        were 4,000 auto-generated multi-leg parlays, with every real market
        behind them.
      * A full `/events?with_nested_markets` sweep reaches everything but
        costs ~11,900 events / ~101,000 markets and 20+ seconds, and its fixed
        ordering put far-dated markets first: a 14-page sample held 312 Fed
        markets and not one of the near-dated ones a trader would actually
        compare.
      * The series list is ~11,800 SHORT rows in ~4 seconds, and a series
        title *is* the question — which is exactly what we are matching on.

    So: match the question against series titles, then pull markets only from
    the handful of series that look right.
    """
    rows: list[dict] = []
    ok_any = False
    for cat in CATEGORIES:
        data, _ = await _fetch(
            "kalshi", "/series", {"category": cat}, ttl=TTL_SERIES, timeout=20.0)
        if not isinstance(data, dict):
            continue
        ok_any = True
        for sx in data.get("series") or []:
            if not isinstance(sx, dict):
                continue
            ticker = str(sx.get("ticker") or "").strip()
            title = str(sx.get("title") or "").strip()
            if not ticker or not title:
                continue
            rows.append({
                "ticker": ticker,
                "title": title,
                "category": (str(sx.get("category") or cat)).strip(),
                # The venue's own statement of what it settles on. Shown in
                # the comparison panel: two markets can ask the same question
                # and still settle on different sources, and that difference
                # is the thing a price gap most often actually is.
                "settlementSources": [
                    str(x.get("name") or x.get("url") or "")
                    for x in (sx.get("settlement_sources") or [])
                    if isinstance(x, dict)
                ],
                "contractUrl": str(sx.get("contract_url") or "") or None,
            })
    return rows, ok_any


TTL_EVENTS = 300.0


async def events_in_series(series_ticker: str, limit: int = 200) -> list[dict]:
    """Open markets in one series, WITH their event context — one call.

    This replaced `markets_in_series` as the matcher's retrieval step, for two
    reasons measured against the live API on 2026-09-02:

    * **The subject is on the EVENT, not the market.** `/markets` gives
      "Tampa Bay wins" and nothing else — no opponent, no date, no sport. The
      event gives "New York M vs Tampa Bay / NYM vs TB (Sep 2)". Matching a
      Polymarket question against the market row alone was hopeless for every
      fixture-shaped market, which is most of what both venues list.
    * **`/markets` truncates on the series that matter most.** A daily strike
      series (KXBTCD) has ~200 markets PER DATE; a flat `limit=200` sweep came
      back holding one date's ladder and nothing else, so the right strike on
      the right day was simply never a candidate. Events are few and each
      carries its own ladder nested, so nothing is cut off.

    Same cost as the old call — one request — and the same gate.
    """
    st = str(series_ticker or "").strip().upper()
    if not st:
        return []
    data, observed = await _fetch(
        "kalshi", "/events",
        {"series_ticker": st, "status": "open",
         "with_nested_markets": "true", "limit": int(limit)},
        ttl=TTL_EVENTS, timeout=20.0)
    if not isinstance(data, dict):
        return []
    out: list[dict] = []
    for ev in data.get("events") or []:
        if not isinstance(ev, dict):
            continue
        for raw in ev.get("markets") or []:
            if not isinstance(raw, dict):
                continue
            if raw.get("mve_collection_ticker"):
                continue
            row = market_row(raw, observed, ev)
            if row:
                row["seriesTicker"] = row["seriesTicker"] or st
                out.append(row)
    return out


async def markets_in_series(series_ticker: str, limit: int = 200) -> list[dict]:
    """Open markets in one series, normalised. Small and cheap.

    NOT the matcher's path — that is `events_in_series`, which carries the
    event context and does not truncate a strike ladder. This is the plain
    market route, kept because the live check uses it to prove the `/markets`
    endpoint and its quote fields independently of the events one.
    """
    st = str(series_ticker or "").strip().upper()
    if not st:
        return []
    data, observed = await _fetch(
        "kalshi", "/markets",
        {"series_ticker": st, "status": "open", "limit": int(limit)},
        ttl=TTL_MARKET, timeout=20.0)
    if not isinstance(data, dict):
        return []
    out = []
    for raw in data.get("markets") or []:
        if isinstance(raw, dict) and raw.get("mve_collection_ticker"):
            continue
        row = market_row(raw, observed)
        if row:
            row["seriesTicker"] = row["seriesTicker"] or st
            out.append(row)
    return out


# ────────────────────────────────────────────────────────────────────────
# Price history
# ────────────────────────────────────────────────────────────────────────


def _candle_price(c: dict, group: str, key: str) -> Optional[float]:
    """Historical tier: {"price": {"close": "0.94"}}. Live tier:
    {"price": {"close_dollars": "0.9400"}}. The `_dollars` key says its unit;
    the bare one does not, so only that one goes through the inferring reader.
    """
    d = c.get(group)
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None:
        return _from_dollars(d.get(f"{key}_dollars"))
    return _cents(v)


def normalize_candles(raw: list) -> list[dict]:
    """→ ascending [{t, p}] in cents, duplicates collapsed.

    Kalshi emits the same period stamp twice in one response; a repeat is a
    correction, so the LAST record for a stamp wins. Never gap-filled: a
    period with no trades simply produces no point.
    """
    by_ts: dict[int, float] = {}
    for c in raw or []:
        if not isinstance(c, dict):
            continue
        ts = num(c.get("end_period_ts"))
        if ts is None or ts <= 0:
            continue
        # Prefer the traded close; fall back to the book mid, which is what
        # Kalshi reports for a period that quoted but did not trade.
        p = _candle_price(c, "price", "close")
        if p is None:
            p = _candle_price(c, "price", "mean")
        if p is None:
            bid = _candle_price(c, "yes_bid", "close")
            ask = _candle_price(c, "yes_ask", "close")
            if bid is not None and ask is not None:
                p = round((bid + ask) / 2.0, 2)
        if p is None:
            continue
        by_ts[int(ts)] = p
    return [{"t": t, "p": by_ts[t]} for t in sorted(by_ts)]


async def candles(ticker: str, *, start_ts: int, end_ts: int,
                  period_interval: int = 60,
                  series_ticker: str = "") -> list[dict]:
    """Price history for one Kalshi market, in cents.

    Two tiers hold candles and which one has a given market depends on
    Kalshi's rolling cutoff, so both are tried rather than guessed at. The
    endpoint 400s without BOTH timestamps.
    """
    t = str(ticker or "").strip().upper()
    if not t:
        return []
    series = (series_ticker or "").strip().upper() or t.split("-")[0]
    params = {
        "start_ts": int(start_ts), "end_ts": int(end_ts),
        "period_interval": int(period_interval),
    }
    for path in (f"/series/{series}/markets/{t}/candlesticks",
                 f"/historical/markets/{t}/candlesticks"):
        data, _ = await _fetch("kalshi", path, params, ttl=TTL_CANDLES,
                               timeout=20.0)
        if isinstance(data, dict) and isinstance(data.get("candlesticks"), list):
            pts = normalize_candles(data["candlesticks"])
            if pts:
                return pts
    return []


def web_url(ticker: str, event_ticker: Optional[str] = None) -> str:
    """The public Kalshi page for a market, so the user can check us."""
    ev = (event_ticker or str(ticker or "").split("-")[0]).strip().lower()
    return f"https://kalshi.com/markets/{ev}" if ev else "https://kalshi.com"


# ────────────────────────────────────────────────────────────────────────
# The venue difference — stated, never implied away
# ────────────────────────────────────────────────────────────────────────

VENUE_NOTE = (
    "Kalshi is a CFTC-regulated US exchange; Polymarket is not the same thing "
    "legally. Fees, settlement guarantees and the resolution source are each "
    "venue's own, so two markets asking the same question can still settle "
    "differently."
)

# What a price difference between two venues is NOT, by itself. Written as
# context on the number rather than as trading advice: this is reference data,
# and presenting a gap as an opportunity would be the app forming a view.
SPREAD_CAVEATS = [
    "The two markets may resolve on different sources, or on different dates.",
    "This app does not trade on Kalshi. These figures are reference data — a "
    "second opinion on the same question, nothing more.",
    "Kalshi quotes are exchange prices in cents; Polymarket quotes are on-chain "
    "book prices. Neither is a fill until somebody takes it.",
]
