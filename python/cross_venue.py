"""Cross-venue: the same question, priced on Polymarket and on Kalshi.

This is the one feature in the terminal that asserts something no provider
told us: that these two markets are the same question. Nothing in either API
says so — we are inferring it from words, numbers and dates. So the whole
module is built around one idea:

    A MATCH IS A CLAIM, AND A WRONG MATCH IS WORSE THAN NO PANEL.

A mismatched pair produces a large, confident, entirely fictional "edge", and
a fictional edge is exactly the kind of number somebody trades on. So:

* every match carries a confidence and the reasons behind it,
* the matched title is shown verbatim so the user can check it in one glance,
* below the confidence bar the spread is **withheld** — the pair is shown,
  labelled as unverified, with no number attached,
* and even a strong match carries the caveats that survive it: two markets
  can ask the same question and still settle on different sources or dates.

WHERE THE RECALL WENT (measured 2026-09-02, 60 real markets, see
tests/test_cross_venue_eval.py). The reported complaint was that the panel
found nothing for most markets, and it was true: 3 of 31 real counterparts,
a recall of 0.10 at perfect precision. The scoring was not the problem. The
RETRIEVAL was — the right Kalshi market was usually never a candidate at all:

* a Kalshi SERIES is titled "Professional Baseball Game" and a Polymarket
  question is titled "New York Mets vs. Tampa Bay Rays"; they share no word,
  so no amount of text similarity connects them,
* "BTC" and "Bitcoin" likewise,
* and the market sweep the matcher ran took the first 200 rows of a series,
  which on a daily strike ladder is one date's worth of strikes — so the
  right threshold on the right day was structurally unreachable.

So this module now retrieves three ways (see _candidate_series) and compares
STRUCTURALLY where both sides parse to (subject, date, threshold, direction),
falling back to the original text scorer for everything else. Two things about
the structured path are worth knowing before reading it:

* the settlement DAY is read in US Eastern from the venues' own stamps — never
  from the close time, which BOTH venues pad by days so a postponed fixture
  can still settle,
* and the comparable side is not always YES. Kalshi lists levels from either
  direction, so "below $75,999.99" is the NO leg of "above $76,000". That
  inversion is allowed, is applied to the quote and the chart alike, and is
  stated on the response — an inverted spread nobody was told about is the
  same fictional edge as a wrong pairing.

Read-only. Nothing here can place an order on either venue.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

import kalshi
import terminal as term
from terminal import _are_opposites, _now_iso, num

logger = logging.getLogger("krypt.crossvenue")

# Words that carry no matching signal. Deliberately short: over-stripping makes
# unrelated markets look similar, which is the failure that matters here.
_STOP = {
    "the", "and", "for", "will", "with", "that", "this", "from", "have", "has",
    "before", "after", "than", "any", "all", "who", "what", "when", "which",
    "does", "did", "are", "was", "were", "his", "her", "its", "their", "there",
    "market", "markets", "yes", "no", "not",
}

# Confidence thresholds. Tuned so a same-question pair clears STRONG and a
# same-topic-different-question pair lands in POSSIBLE, where no spread prints.
STRONG = 0.55
POSSIBLE = 0.38

# No match cache here on purpose: the series index is cached for 30 minutes and
# every per-lookup fetch rides terminal's TTL cache, so a repeat lookup is
# already nearly free. A second cache would only add a way for the panel to
# show a pairing that no longer exists.


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP}


# A year is context — both venues say it and neither means it as a price — so
# it is dropped before the figures are compared. But the test for "is this a
# year" has to be narrow, because the range a year lives in is also where real
# thresholds live. The rule used to be "any integer in 1900..2100", which ate
# the threshold in "ETH above $2,000": both sides then named no figure at all,
# nothing contradicted, and "$2,000" paired with "$2,100" at strong confidence
# with a fictional spread under it. That is the exact failure this module
# exists to prevent, so a number is now a year ONLY when it is written the way
# a year is written and nothing around it says otherwise.
_YEAR_MIN, _YEAR_MAX = 2000, 2040

# A currency sign in front of it: "$2000" is money however it is spelled.
_MONEY_BEFORE = re.compile(r"[$€£¥]\s*$")
# A unit or a comma-grouped continuation behind it: "2000 points", "2000k",
# "2,000,000". Anything with a unit attached is a quantity, not a date.
_UNIT_AFTER = re.compile(
    r"^\s*(?:[.,]\d|[kmb]\b|%|x\b|bps\b|dollars?\b|cents?\b|usd\b|pts\b"
    r"|points?\b|bn\b)",
    re.IGNORECASE)


def _is_year(text: str, start: int, end: int, raw: str, v: float) -> bool:
    """Whether the run of digits at [start:end] reads as a calendar year."""
    # Written as a figure rather than a year: "2,000" carries a thousands
    # separator, "2000.5" a decimal. A year is four bare digits and nothing
    # else, so anything the tokeniser swallowed alongside them disqualifies it.
    if len(raw) != 4 or not raw.isdigit():
        return False
    if not (_YEAR_MIN <= v <= _YEAR_MAX):
        return False
    if _MONEY_BEFORE.search(text[max(0, start - 3):start]):
        return False
    if _UNIT_AFTER.match(text[end:end + 12]):
        return False
    return True


def _numbers(text: str) -> set[str]:
    """Significant numbers, normalised.

    This is the single most important discriminator for threshold markets:
    "Bitcoin above $110,000" and "Bitcoin above $120,000" differ by nothing
    else a token comparison can see. Commas are stripped and a trailing ".0"
    normalised so 110,000 / 110000 / 110000.0 are one number.
    """
    s = text or ""
    out: set[str] = set()
    # A decimal point counts only when digits follow it, so a number ending a
    # sentence ("resolves by 2030.") is four bare digits and still reads as a
    # year rather than as the five-character token "2030.".
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", s):
        raw = m.group(0)
        v = num(raw.replace(",", ""))
        if v is None:
            continue
        if _is_year(s, m.start(), m.end(), raw, v):
            continue
        out.add(f"{v:g}")
    return out


def _close_ts(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


# ────────────────────────────────────────────────────────────────────────
# The settlement DAY, in the timezone both venues actually settle in
#
# Every dated product on both venues — "on September 3", "at 5pm EDT",
# tonight's game — is stated in US Eastern. Comparing UTC dates puts an 8pm ET
# fixture on the following day and makes a same-day pair look a day apart.
#
# There is no tz database on this machine (no `tzdata` wheel, and adding a
# dependency for one offset is not worth it), so the US rule is implemented
# directly: DST runs from 07:00 UTC on the second Sunday in March to 06:00 UTC
# on the first Sunday in November. That rule has been exact for US Eastern
# since 2007 and it is pinned by a test.
# ────────────────────────────────────────────────────────────────────────


def _nth_weekday_utc(year: int, month: int, nth: int) -> datetime:
    """The nth Sunday of a month, at 00:00 UTC."""
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    # weekday(): Monday=0 … Sunday=6
    first_sunday = 1 + ((6 - d.weekday()) % 7)
    return datetime(year, month, first_sunday + 7 * (nth - 1), tzinfo=timezone.utc)


def _et_offset_hours(dt: datetime) -> int:
    """-4 during Eastern Daylight Time, -5 otherwise."""
    start = _nth_weekday_utc(dt.year, 3, 2).replace(hour=7)
    end = _nth_weekday_utc(dt.year, 11, 1).replace(hour=6)
    return -4 if start <= dt < end else -5


def et_date(iso: Optional[str]) -> Optional[str]:
    """An ISO instant → the calendar date it falls on in US Eastern."""
    ts = _close_ts(iso)
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, timezone.utc)
    local = dt + timedelta(hours=_et_offset_hours(dt))
    return local.strftime("%Y-%m-%d")


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

# "26SEP02" inside a Kalshi ticker: the venue's own ET date for the event.
_KX_TICKER_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
# "-26SEP" with no day after it: a MONTHLY event, which is how Kalshi tickers
# the Fed meetings and the CPI prints.
_KX_TICKER_MONTH = re.compile(r"-(\d{2})([A-Z]{3})(?!\d)")
# "2026-09-02" inside a Polymarket slug: the fixture date, likewise ET.
_SLUG_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
# "on September 3", "by December 31, 2026", "Sep 3, 2026"
_TEXT_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b"
    r"(?:,?\s*(20\d{2}))?", re.IGNORECASE)


def kalshi_ticker_date(ticker: str) -> Optional[str]:
    """The ET date Kalshi stamped into an event ticker, or None.

    Preferred over every timestamp on the row: `close_time` is padded by days
    on both venues so a postponed fixture can still settle, and comparing two
    padded dates is what made same-day pairs look a week apart.
    """
    m = _KX_TICKER_DATE.search(str(ticker or "").upper())
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2).lower(), m.group(3)
    if mon not in _MONTHS:
        return None
    try:
        return f"20{yy}-{_MONTHS[mon]:02d}-{int(dd):02d}"
    except ValueError:
        return None


def kalshi_ticker_month(ticker: str) -> Optional[str]:
    """The month Kalshi stamped into a monthly event ticker → "YYYY-MM".

    Preferred over the close time for the same reason the day is: the close
    time is padded, and on a Fed market it is the only date the Polymarket
    side has. Reading Kalshi's own stamp means a monthly pairing does not
    depend on either venue's deadline at all.
    """
    up = str(ticker or "").upper()
    m = _KX_TICKER_MONTH.search(up)
    if not m:
        return None
    mon = m.group(2).lower()
    if mon not in _MONTHS:
        return None
    return f"20{m.group(1)}-{_MONTHS[mon]:02d}"


def text_date(text: str, *, year_hint: Optional[str] = None) -> Optional[str]:
    """A date written out in a question title → "YYYY-MM-DD", or None."""
    m = _TEXT_DATE.search(text or "")
    if not m:
        return None
    mon = _MONTHS[m.group(1).lower()[:3]]
    day = int(m.group(2))
    year = m.group(3) or (year_hint or "")[:4]
    if not year:
        return None
    try:
        datetime(int(year), mon, day)
    except ValueError:
        return None
    return f"{int(year):04d}-{mon:02d}-{day:02d}"


# ────────────────────────────────────────────────────────────────────────
# Vocabulary
#
# The two venues describe the same thing in different words, and the gap is
# not a similarity problem — it is a translation problem. "BTC" and "Bitcoin"
# share no characters; a Kalshi SERIES is called "Professional Baseball Game"
# and never says "Mets". Nothing a text scorer does can bridge that, which is
# why the recall failure was in RETRIEVAL, not in scoring: the right Kalshi
# market was usually never a candidate at all.
#
# So the tables below are deliberately small and literal. Each entry is a
# translation we have checked against the live API, not a guess — and a wrong
# guess is cheap rather than dangerous, because every candidate still has to
# survive the scoring and the confidence floor before anything is shown.
# ────────────────────────────────────────────────────────────────────────

# Question word → extra tokens to look for in Kalshi SERIES titles.
_ALIASES: dict[str, set[str]] = {
    # Crypto: Kalshi titles the series by ticker as often as by name.
    "bitcoin": {"btc"}, "btc": {"bitcoin"},
    "ethereum": {"eth"}, "eth": {"ethereum"},
    "solana": {"sol"}, "sol": {"solana"},
    "dogecoin": {"doge"}, "doge": {"dogecoin"},
    "ripple": {"xrp"}, "cardano": {"ada"},
    "hyperliquid": {"hype"}, "zcash": {"zec"},
    # Macro.
    "fed": {"fomc", "federal", "reserve"},
    "fomc": {"fed"},
    "federal": {"fed"},
    "inflation": {"cpi"}, "cpi": {"inflation"},
    "unemployment": {"jobless", "jobs", "payrolls"},
    "gdp": {"growth"},
    # Direction words, because Kalshi names its series after the shape of the
    # question ("How low will Bitcoin fall this year?") and Polymarket after
    # the event ("Will Bitcoin dip to $45,000…").
    "dip": {"low", "fall", "min", "lowest"},
    "drop": {"low", "fall", "min"},
    "crash": {"low", "fall", "min"},
    "peak": {"high", "max", "highest"},
    "reach": {"high", "max", "hit"},
    "hit": {"high", "max", "reach"},
    "above": {"over", "greater"},
    "over": {"above"},
    "below": {"under", "less"},
    "under": {"below"},
    # Competitions Polymarket spells as one word.
    "laliga": {"liga"},
    "uefa": {"champions"},
}

# A named topic → the Kalshi series that carry it. Reached for only when the
# question NAMES the topic; every ticker is still checked against the live
# series index before it costs a request, so a stale entry is inert.
_TOPIC_SERIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Season-long competition winners. The Kalshi series title is just the
    # competition ("LA LIGA"), so token overlap with a question about one club
    # is near zero and only the competition name can find it.
    (r"\bla\s?liga\b", ("KXLALIGA",)),
    (r"champions\s+league|\bucl\b", ("KXUCL",)),
    (r"premier\s+league\b", ("KXEPL",)),
    (r"\bserie\s?a\b", ("KXSERIEA",)),
    (r"\bbundesliga\b", ("KXBUNDESLIGA",)),
    (r"\bligue\s?1\b", ("KXLIGUE1",)),
    (r"europa\s+league", ("KXUEL",)),
    # Crypto extremes: "dip to X by <date>" is Kalshi's min/max-for-the-year
    # product, whose series title shares almost no words with the question.
    (r"\bbitcoin\b|\bbtc\b",
     ("KXBTCD", "KXBTC", "KXBTCMINY", "KXBTCMAXY")),
    (r"\bethereum\b|\beth\b",
     ("KXETHD", "KXETH", "KXETHMINY", "KXETHMAXY")),
    (r"\bsolana\b|\bsol\b", ("KXSOLD", "KXSOL")),
    (r"\bxrp\b|\bripple\b", ("KXXRPD", "KXXRP")),
    (r"\bdogecoin\b|\bdoge\b", ("KXDOGED", "KXDOGE")),
    # Macro.
    (r"\bfed\b|federal\s+reserve|\bfomc\b|interest\s+rate",
     ("KXFEDDECISION", "KXRATECUT")),
    (r"\bcpi\b|inflation", ("KXCPIYOY", "KXCPI", "KXCPICORE")),
    (r"unemployment\s+rate", ("KXUNRATE",)),
)

# Polymarket writes a fixture's league, both sides and the date into the SLUG
# ("mlb-nym-tb-2026-09-02", "lal-rso-cel-2026-09-03"). That is the only place
# a game market states its sport at all — the title is just two team names —
# and it is also the only trustworthy DATE, because both venues pad a game's
# close time by days. Kalshi's tickers are equally regular: KX + league +
# market family. So the join is table + convention, not fuzzy text.
_LEAGUE_PREFIX: dict[str, str] = {
    "mlb": "KXMLB", "nba": "KXNBA", "nfl": "KXNFL", "nhl": "KXNHL",
    "ncaaf": "KXNCAAF", "ncaab": "KXNCAAMB",
    "atp": "KXATP", "wta": "KXWTA",
    "epl": "KXEPL", "lal": "KXLALIGA", "ser": "KXSERIEA",
    "bun": "KXBUNDESLIGA", "fl1": "KXLIGUE1", "bra": "KXBRASILEIRO",
    "chi1": "KXCHLLDP", "arg": "KXARGPREMDIV", "mls": "KXMLS",
    "mex": "KXLIGAMX", "ucl": "KXUCL", "uel": "KXUEL",
    "por": "KXPRIMEIRA", "ned": "KXEREDIVISIE",
    # Polymarket US's own codes for the same competitions.
    "cfb": "KXNCAAF", "cbb": "KXNCAAMB", "sea": "KXSERIEA", "lg1": "KXLIGUE1",
    "lmx": "KXLIGAMX", "ere": "KXEREDIVISIE",
}

# The tail of the Polymarket slug names the market family. This is an
# ALLOW-LIST, and that is the whole point of it.
#
# It began as a default — an unrecognised tail fell through to the game
# winner — and a holdout run showed exactly what that costs: "Draw at
# halftime?" paired with Kalshi's full-time "Tie is the result", and "Neither
# team to score first?" paired with "Deportes Limache wins". Four confident,
# completely different questions, each with a real spread under it.
#
# So a tail we do not recognise ends the fixture route. Polymarket lists
# first-to-score, halftime-result, second-half-result and exact-score markets
# and Kalshi has families for some of them, but we have not checked how Kalshi
# words those legs — and an unchecked mapping here is a fictional edge, not a
# feature. Longest match first, so "first-half-spread" is never read as
# "spread".
_FIXTURE_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("first-half-spread", "spread", ("1HSPREAD",)),
    ("first-half-total", "total", ("1HTOTAL",)),
    ("1st-half-spread", "spread", ("1HSPREAD",)),
    ("1st-half-total", "total", ("1HTOTAL",)),
    ("first-half", "winner", ("1H",)),
    ("game-totals", "total", ("TOTAL", "GTOTAL")),
    ("total", "total", ("TOTAL", "GTOTAL")),
    ("handicap", "spread", ("SPREAD", "GSPREAD")),
    ("spread", "spread", ("SPREAD", "GSPREAD")),
    ("btts", "btts", ("BTTS",)),
)
_DEFAULT_FAMILIES = ("GAME", "MATCH")

# A moneyline leg: an empty tail, a team code ("fla", "tb", "nym") or the tie.
# Anything else with structure in it — "halftime-result-draw",
# "first-to-score-neither", "exact-score-1-0" — is a DIFFERENT QUESTION about
# the same fixture, and is refused above rather than guessed at.
_OUTCOME_CODE = re.compile(r"^[a-z0-9]{1,6}$")

_SLUG_FIXTURE = re.compile(
    r"^([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-(20\d{2}-\d{2}-\d{2})(?:-(.*))?$")

# A market slug starts with its contract type, ahead of the fixture. These two are
# the game's winner: `aec-nfl-lac-ten-2025-11-02` is two-way, and
# `atc-epl-mnu-ast-2026-11-08-draw` is one leg of three, named in its tail. Any
# other type on a fixture is a different question about that game.
_WINNER_CONTRACTS = ("aec", "atc")

# "8pt5" is how Polymarket writes 8.5 in a slug.
_SLUG_LINE = re.compile(r"(\d+)pt(\d+)")


def expand_tokens(tokens: set[str]) -> set[str]:
    """Question tokens plus every alias they translate to."""
    out = set(tokens)
    for t in tokens:
        out |= _ALIASES.get(t, set())
    return out


# ────────────────────────────────────────────────────────────────────────
# The structured question
#
# Most of both venues' volume is not prose — it is (subject, date, threshold,
# direction). When both sides parse to those four fields there is nothing to
# infer: they either agree or they do not, and saying so is a far stronger and
# far safer claim than "these titles share seven words".
#
# The text path is kept, unchanged, for everything that does not parse.
# ────────────────────────────────────────────────────────────────────────

_SCALE = {"k": 1_000.0, "m": 1_000_000.0, "bn": 1e9, "b": 1e9}

# A figure, with the multiplier Polymarket writes into titles ("76k", "$2.5m").
# The multiplier must not be followed by another letter, or "$45,000 by
# December 31" reads the "b" of "by" as billions and asks about a threshold of
# forty-five trillion — which then matches nothing, silently.
_NUM = r"\$?\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(k|m|bn|b)(?![a-z0-9]))?"

_ABOVE_WORDS = r"(?:above|over|greater\s+than|higher\s+than|at\s+least|more\s+than|exceed(?:s)?)"
_BELOW_WORDS = r"(?:below|under|less\s+than|lower\s+than|at\s+most|fewer\s+than)"
# "dip to", "fall to", "drop to" — a level TOUCHED at some point, which is the
# question Kalshi's min/max series asks in the other direction's words.
_TOUCH_DOWN = r"(?:dip(?:s)?\s+to|fall(?:s)?\s+to|drop(?:s)?\s+to|down\s+to)"
_TOUCH_UP = r"(?:reach(?:es)?|hit(?:s)?|rise(?:s)?\s+to|climb(?:s)?\s+to|up\s+to)"

_RE_BETWEEN = re.compile(
    rf"between\s+{_NUM}\s*(?:and|to|[-–])\s*{_NUM}", re.IGNORECASE)
_RE_BAND = re.compile(
    rf"{_NUM}\s*(?:°\s*f?)?\s*(?:[-–]|\bto\b)\s*{_NUM}", re.IGNORECASE)
_RE_ABOVE = re.compile(rf"{_ABOVE_WORDS}\s+{_NUM}", re.IGNORECASE)
_RE_BELOW = re.compile(rf"{_BELOW_WORDS}\s+{_NUM}", re.IGNORECASE)
# Kalshi writes the level first and the direction after: "$76,000 or above",
# "85° or below". Same claim, opposite word order, so it needs its own read.
_RE_ABOVE_POST = re.compile(
    rf"{_NUM}\s*(?:°\s*f?)?\s+or\s+(?:above|higher|more|greater|over)",
    re.IGNORECASE)
_RE_BELOW_POST = re.compile(
    rf"{_NUM}\s*(?:°\s*f?)?\s+or\s+(?:below|lower|less|fewer|under)",
    re.IGNORECASE)
_RE_TOUCH_DOWN = re.compile(rf"{_TOUCH_DOWN}\s+{_NUM}", re.IGNORECASE)
_RE_TOUCH_UP = re.compile(rf"{_TOUCH_UP}\s+{_NUM}", re.IGNORECASE)
_RE_OU = re.compile(rf"\bo\s*/\s*u\s+{_NUM}", re.IGNORECASE)

# "by December 31, 2026" — a DEADLINE, not a settlement instant. The two
# venues write the same cut-off from either side of midnight, so this is the
# one date form where a single day's difference is not a different question.
_RE_DEADLINE = re.compile(
    r"\b(?:by|before|prior\s+to)\s+(?:the\s+end\s+of\s+)?"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|20\d{2})",
    re.IGNORECASE)


def _scaled(digits: str, suffix: Optional[str]) -> Optional[float]:
    v = num(digits.replace(",", ""))
    if v is None:
        return None
    return v * _SCALE.get((suffix or "").lower(), 1.0)


def parse_threshold(text: str) -> Optional[tuple]:
    """A written question → ("above"|"below"|"between", value[, value]).

    Returns None when the text states no threshold, which is the honest answer
    for "Will X win the election?" and means the structural path simply does
    not apply.
    """
    s = text or ""
    m = _RE_BETWEEN.search(s) or _RE_BAND.search(s)
    if m:
        lo, hi = _scaled(m.group(1), m.group(2)), _scaled(m.group(3), m.group(4))
        if lo is not None and hi is not None and lo <= hi:
            return ("between", lo, hi)
    for rx, op in ((_RE_OU, "above"),
                   (_RE_ABOVE_POST, "above"), (_RE_BELOW_POST, "below"),
                   (_RE_ABOVE, "above"), (_RE_BELOW, "below"),
                   (_RE_TOUCH_UP, "above"), (_RE_TOUCH_DOWN, "below")):
        m = rx.search(s)
        if m:
            v = _scaled(m.group(1), m.group(2))
            if v is not None:
                return (op, v)
    return None


def _same_value(a: float, b: float) -> bool:
    """Two thresholds naming the same level.

    Kalshi writes a "greater than" strike one tick below the number it prints
    (floor_strike 75999.99 under the label "$76,000 or above"), so a whole-cent
    gap is the same level written two ways. Anything wider is a DIFFERENT
    threshold — the single most important discriminator in this module — and
    the tolerance stays far below the smallest real strike spacing on either
    venue.
    """
    return abs(a - b) <= 0.011 or abs(a - b) <= abs(a) * 1e-9


def compare_thresholds(pm_t: Optional[tuple],
                       kx_t: Optional[tuple]) -> Optional[str]:
    """→ "same" | "complement" | None (contradicts, or not comparable).

    "complement" is the case that matters: a Kalshi "below X" is the NO leg of
    a Polymarket "above X". It is a real match, but only if the module says so
    out loud and inverts the price it compares — see structural_match.
    """
    if not pm_t or not kx_t:
        return None
    if pm_t[0] == "between" or kx_t[0] == "between":
        if pm_t[0] == kx_t[0] and _same_value(pm_t[1], kx_t[1]) \
                and _same_value(pm_t[2], kx_t[2]):
            return "same"
        return None
    if not _same_value(pm_t[1], kx_t[1]):
        return None
    return "same" if pm_t[0] == kx_t[0] else "complement"


# The Fed is the one macro question both venues list every month, and they
# write the same five legs in different units: Polymarket says "increase by
# 50+ bps", Kalshi says "Hike >25bps". Those ARE the same contract — the Fed
# moves in 25bp steps — but no threshold rule can say so, because 50 and 25
# are different numbers. So the legs are read as legs.
_FED_LEGS = ("H0", "H25", "H26", "C25", "C26")


def fed_leg(text: str) -> Optional[str]:
    """A Fed-decision question → the leg it names, or None."""
    s = (text or "").lower()
    if not re.search(r"\bfed\b|federal reserve|fomc|interest rate", s):
        return None
    if re.search(r"no change|maintain|unchanged|hold rates|by 0\s*bps", s):
        return "H0"
    up = bool(re.search(r"increase|hike|raise", s))
    down = bool(re.search(r"decrease|cut|lower", s))
    if up == down:
        return None
    big = bool(re.search(
        r"5\d\+?\s*bps|\+\s*bps|>\s*25|(?:more|greater)\s+than\s+25|50\+", s))
    if re.search(r"\b25\s*bps\b", s) and not big:
        return "H25" if up else "C25"
    if big:
        return "H26" if up else "C26"
    return None


# The month a question names: "after the September 2026 meeting", "in December".
_RE_NAMED_MONTH = re.compile(
    r"\b(?:in|after|at|for|the|on)\s+(?:the\s+)?"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"(?:\s+(20\d{2}))?", re.IGNORECASE)
_RE_FOMC_SLUG = re.compile(r"-fomc-(20\d{2})-(\d{2})-\d{2}(?:-|$)")


def fed_meeting_month(pm: dict) -> Optional[str]:
    """The Fed meeting a question is about, "YYYY-MM", or None if it names none.

    Never read from the close time alone, which is padded: "Fed Rate Cut Greater
    Than 25bps?" asks about ANY meeting up to December 31 and closes on January 15,
    which once read as the January meeting and paired it with Kalshi's January leg
    at strong confidence. A question with no meeting of its own has no counterpart
    among Kalshi's per-meeting legs.
    """
    m = _RE_FOMC_SLUG.search(str(pm.get("slug") or "").lower())
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    title = str(pm.get("title") or "")
    if _RE_DEADLINE.search(title):
        return None
    close = str(pm.get("closeTime") or "")
    td = text_date(title, year_hint=close)
    if td:
        return td[:7]
    m = _RE_NAMED_MONTH.search(title)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()[:3]]
    year = m.group(2)
    if not year:
        closes = et_date(close)
        if not closes:
            return None
        # A meeting can't fall after the question closes, so a December meeting on
        # a question closing in January belongs to the year before.
        year = int(closes[:4]) - (1 if month > int(closes[5:7]) else 0)
    return f"{int(year):04d}-{month:02d}"


def kalshi_fed_leg(kx: dict) -> Optional[str]:
    """The leg of a KXFEDDECISION market, straight off its ticker."""
    tick = str(kx.get("conditionId") or "").upper()
    if "FEDDECISION" not in tick:
        return None
    leg = tick.rsplit("-", 1)[-1]
    return leg if leg in _FED_LEGS else None


def parse_fixture(pm: dict) -> Optional[dict]:
    """A Polymarket sports slug → {league, date, family, line, tail}.

    The slug is the only honest source for two facts a game market never puts
    in its title: which competition it is, and which day it is played. Both
    venues pad the close time by days so a postponed fixture can still settle,
    so the close time cannot supply the second one.
    """
    for key in ("slug", "eventSlug"):
        s = str(pm.get(key) or "").strip().lower()
        contract, _, rest = s.partition("-")
        if key == "slug" and contract not in _LEAGUE_PREFIX:
            typed = _SLUG_FIXTURE.match(rest)
            if contract in _WINNER_CONTRACTS:
                s = rest
            elif typed and typed.group(1) in _LEAGUE_PREFIX:
                # A total or a prop on a fixture we know. Its event slug IS the
                # bare fixture, so falling through to it would read this question
                # as the game's winner.
                return None
        m = _SLUG_FIXTURE.match(s)
        if not m:
            continue
        league = m.group(1)
        prefix = _LEAGUE_PREFIX.get(league)
        if not prefix:
            continue
        tail = m.group(5) or ""
        family, kind = None, None
        for needle, k, fams in _FIXTURE_FAMILIES:
            if needle in tail:
                family, kind = fams, k
                break
        if family is None:
            if tail and not _OUTCOME_CODE.match(tail):
                # A market on this fixture that we cannot name. Refusing here
                # is the whole allow-list: falling through to the game winner
                # paired "Draw at halftime?" with a full-time tie.
                return None
            family, kind = _DEFAULT_FAMILIES, "winner"
        line = None
        lm = _SLUG_LINE.search(tail)
        if lm:
            line = float(f"{lm.group(1)}.{lm.group(2)}")
        return {
            "league": league, "prefix": prefix, "date": m.group(4),
            "families": family, "kind": kind, "line": line, "tail": tail,
            "sides": (m.group(2), m.group(3)),
        }
    return None


# ── timed crypto up/down ─────────────────────────────────────────────────
#
# The family the owner actually trades, and the one the panel was quietest
# about. Both venues list it, and both state it exactly — there is nothing to
# infer here at all:
#
#   Polymarket  cpc-btc-updown-15m-2026-11-05-0015z   asset, length, START in UTC
#   Kalshi      KXBTC15M-26SEP030015-15               asset, and the window END in ET
#
# so the join is the window END instant. The stamp is the window's START, checked
# against the market's own `windowStart` and `windowEnd` on 2026-11-05.
#
# The two do NOT always line up, and that is the useful half of this: Kalshi lists
# 15-minute windows only, and opens one at a time. A question with no aligned
# window gets its own note saying exactly that, instead of the generic "no market
# looks like this" — see explain_no_match.
_SLUG_UPDOWN = re.compile(
    r"^(?:[a-z0-9]+-)?([a-z]+)-updown-(\d+)(m|h)-"
    r"(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})z$")

# Polymarket's slug code → the asset Kalshi names its series after.
_UPDOWN_ASSETS = {
    "btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP", "doge": "DOGE",
    "bnb": "BNB", "hype": "HYPE", "zec": "ZEC", "ada": "ADA", "bch": "BCH",
    "ltc": "LTC", "link": "LINK",
}

# The only window length Kalshi lists for this product.
_KALSHI_UPDOWN_MINUTES = 15


def parse_updown(pm: dict) -> Optional[dict]:
    """A Polymarket timed up/down market → {asset, minutes, startTs, endTs}.

    Read from the slug, which carries the window as a UTC start plus a length.
    The title says the same thing in words ("September 3, 12:00AM-12:15AM ET") but
    the slug needs no timezone reasoning at all.
    """
    m = _SLUG_UPDOWN.match(str(pm.get("slug") or "").strip().lower())
    if not m:
        return None
    asset = _UPDOWN_ASSETS.get(m.group(1))
    if not asset:
        return None
    minutes = int(m.group(2)) * (60 if m.group(3) == "h" else 1)
    try:
        start = int(datetime(*(int(g) for g in m.group(4, 5, 6, 7, 8)),
                             tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None     # shaped like a date, and is not one
    return {"asset": asset, "minutes": minutes, "startTs": start,
            "endTs": start + minutes * 60,
            "series": f"KX{asset}{_KALSHI_UPDOWN_MINUTES}M"}


def et_clock(iso_or_ts) -> Optional[str]:
    """An instant → "12:15AM ET", the way both venues write these windows."""
    ts = (float(iso_or_ts) if isinstance(iso_or_ts, (int, float))
          else _close_ts(iso_or_ts))
    if ts is None:
        return None
    dt = datetime.fromtimestamp(ts, timezone.utc)
    local = dt + timedelta(hours=_et_offset_hours(dt))
    hour = (local.hour % 12) or 12
    return f"{hour}:{local.minute:02d}{'AM' if local.hour < 12 else 'PM'} ET"


def updown_match(up: dict, kx: dict) -> Optional[dict]:
    """Pair a timed up/down market with the Kalshi window that ends with it."""
    if str(kx.get("seriesTicker") or "").upper() != up["series"]:
        return None
    kx_end = _close_ts(kx.get("resolvesAt") or kx.get("closeTime"))
    if kx_end is None:
        return None
    if up["minutes"] != _KALSHI_UPDOWN_MINUTES:
        return None
    if abs(kx_end - up["endTs"]) > 60:
        return None
    window = (f"{et_clock(up['startTs'])} to {et_clock(up['endTs'])}"
              .replace(" ET to", " to"))
    return {
        "side": "yes", "score": 0.9,
        "for": [
            f"Same asset and the same {up['minutes']}-minute window: "
            f"{window}. Kalshi states the window's end and Polymarket states "
            f"its start, and the two land on the same instant.",
            "Both resolve UP: Polymarket's YES is \"Up\", and Kalshi's YES is "
            "the price finishing above the target it fixed at the window's "
            "start.",
        ],
        "against": [
            "The two venues read the price from different index providers, so "
            "a window that ends within a few dollars of its start can settle "
            "differently on each.",
        ],
    }


async def explain_no_match(pm: dict) -> Optional[str]:
    """Why THIS question has no counterpart, when we can say something better
    than "no market looks like this".

    The generic note is true but useless on a family where the user knows
    perfectly well that both venues list something: what they need to hear is
    that the windows do not line up, and which one Kalshi is actually quoting.
    Uses only what the lookup already fetched — every call rides the same TTL
    cache — so this costs nothing extra.
    """
    up = parse_updown(pm)
    if not up:
        return None
    try:
        rows = await kalshi.events_in_series(up["series"])
    except Exception:  # noqa: BLE001
        return None
    open_ends = sorted(
        {_close_ts(r.get("resolvesAt") or r.get("closeTime")) for r in rows
         if r.get("probability") is not None}
        - {None})
    listed = (", ".join(et_clock(t) for t in open_ends[:3])
              if open_ends else None)
    window = f"{et_clock(up['startTs'])} to {et_clock(up['endTs'])}".replace(
        " ET to", " to")

    if not listed:
        return (
            f"Kalshi lists a {_KALSHI_UPDOWN_MINUTES}-minute {up['asset']} "
            f"up/down market, but it has no open window quoting right now, so "
            f"there is nothing to compare this {up['minutes']}-minute window "
            f"({window}) against.")
    if up["minutes"] != _KALSHI_UPDOWN_MINUTES:
        return (
            f"Kalshi lists {up['asset']} up/down in "
            f"{_KALSHI_UPDOWN_MINUTES}-minute windows only. This is a "
            f"{up['minutes']}-minute window ({window}), so no Kalshi market "
            f"asks the same question — the window Kalshi is quoting ends "
            f"{listed}. A price from a different window length is not a second "
            f"opinion on this one, so none is shown.")
    return (
        f"Kalshi lists {up['asset']} in {_KALSHI_UPDOWN_MINUTES}-minute "
        f"windows like this one, but opens one at a time: it is quoting the "
        f"window ending {listed}, not the one ending {et_clock(up['endTs'])}. "
        f"There is no counterpart for this window yet.")


def pm_settlement_date(pm: dict) -> tuple[Optional[str], bool]:
    """→ (YYYY-MM-DD in US Eastern, exact?).

    `exact` is False when the date came from the padded close time, which is
    the venue's deadline rather than the question's day.
    """
    fx = parse_fixture(pm)
    if fx:
        return fx["date"], True
    close = pm.get("closeTime")
    td = text_date(str(pm.get("title") or ""), year_hint=str(close or ""))
    if td:
        return td, True
    # NOTHING ELSE ON THE MARKET ROW IS A SETTLEMENT DATE. `startDate` is the
    # LISTING date, and reading it as one is what broke this in the app while
    # every test passed: the September 2026 Fed question was listed in May, so
    # it claimed a May settlement, missed Kalshi's September event by four
    # months, and the panel said "no Kalshi market looks like this question"
    # for a pair the matcher had found correctly the day before.
    #
    # The bug only reached the app because `compare()` was the one caller
    # passing the raw row through, and the evaluation exercised `find_match`
    # directly. The evaluation now goes through `compare()`.
    return et_date(close), False


def kx_settlement_date(kx: dict) -> tuple[Optional[str], bool]:
    """Same, for a Kalshi row. The event ticker's own stamp wins."""
    d = kalshi_ticker_date(str(kx.get("eventTicker")
                               or kx.get("conditionId") or ""))
    if d:
        return d, True
    d = et_date(kx.get("strikeDate"))
    if d:
        return d, True
    return et_date(kx.get("resolvesAt") or kx.get("closeTime")), False


# ── season-long winners ──────────────────────────────
#
# Polymarket US writes "Pro Football Champion 2027: San Francisco" and Kalshi calls
# the series "Super Bowl", or writes "Pro Basketball" where the question says NBA.
# Both venues also pad their close dates, by about two years on Kalshi's side, so
# neither the words nor the dates can connect a pair that lists the same price.
# The slug can: it names the league, the competition and the year, and each entry
# here was read against Kalshi's live series on 2026-11-05.
_SLUG_SEASON_WINNER = re.compile(
    r"^tec-([a-z0-9]+)-([a-z0-9]+)-(20\d{2})-\d{2}-\d{2}-w-([a-z0-9]+)$")
_SEASON_WINNERS: dict[tuple[str, str], str] = {
    ("nfl", "champ"): "KXSB",
    ("nba", "champ"): "KXNBA",
    ("nhl", "champ"): "KXNHL",
    ("mlb", "ws"): "KXMLB",
    ("nfl", "afcchamp"): "KXNFLAFCCHAMP",
    ("nfl", "nfcchamp"): "KXNFLNFCCHAMP",
    ("nba", "eastconf"): "KXNBAEAST",
    ("nba", "westconf"): "KXNBAWEST",
    ("nhl", "eastconf"): "KXNHLEAST",
    ("nhl", "westconf"): "KXNHLWEST",
    ("nhl", "atldiv"): "KXNHLATLANTIC",
    ("nhl", "metdiv"): "KXNHLMETROPOLITAN",
    ("nhl", "cendiv"): "KXNHLCENTRAL",
    ("nhl", "pacdiv"): "KXNHLPACIFIC",
    **{("nfl", f"{conf}{div}"): f"KXNFL{conf.upper()}{div.upper()}"
       for conf in ("afc", "nfc") for div in ("east", "north", "south", "west")},
}


def parse_season_winner(pm: dict) -> Optional[dict]:
    """A season-long winner market we can look up → {series, event, team, code}."""
    m = _SLUG_SEASON_WINNER.match(str(pm.get("slug") or "").strip().lower())
    if not m:
        return None
    series = _SEASON_WINNERS.get((m.group(1), m.group(2)))
    title = str(pm.get("title") or "")
    if not series or ":" not in title:
        return None
    team = title.rsplit(":", 1)[1].strip()
    if not team:
        return None
    return {"series": series, "event": f"{series}-{m.group(3)[2:]}",
            "team": team, "code": m.group(4)}


def _plain_name(name: str) -> str:
    """Case and accents folded: Kalshi writes "Montréal", Polymarket "Montreal"."""
    kept = (c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return "".join(kept).casefold()


def _season_winner_leg(team: str, code: str, legs: list[dict]) -> Optional[dict]:
    """The one leg naming this team, or None. Names first ("Toronto Raptors" and
    "Toronto", "Los Angeles C"), then the slug's code, and only when that leaves
    exactly one: a wrong team here is a real price under the wrong name."""
    name = _plain_name(team)
    named = [k for k in legs
             if (sub := _plain_name(str(k.get("subtitle") or "")))
             and (name.startswith(sub) or sub.startswith(name))]
    if len(named) == 1:
        return named[0]
    by_code = [k for k in (named or legs)
               if str(k.get("conditionId") or "").rsplit("-", 1)[-1].lower() == code]
    return by_code[0] if len(by_code) == 1 else None


def _season_winner_verdict(sw: dict, markets: list[dict]) -> Optional[dict]:
    """Match a season-long winner to the same team's leg of the same season."""
    legs = [k for k in markets if k.get("eventTicker") == sw["event"]
            and k.get("probability") is not None]
    leg = _season_winner_leg(sw["team"], sw["code"], legs)
    if not leg:
        return None
    return {
        "market": leg, "score": 0.88, "side": "yes",
        "for": [
            f"Same competition and season: this market's slug and Kalshi's "
            f"{sw['event']} name the same one.",
            f"Same team: this market's \"{sw['team']}\" is Kalshi's "
            f"\"{leg.get('subtitle')}\".",
        ],
        "against": [
            "Each venue writes its own rules, so an unusual ending, like a "
            "cancelled season, could settle differently.",
        ],
    }


def _subject_tokens(text: str) -> set[str]:
    """Tokens minus the words every threshold question contains, so what is
    left is the SUBJECT: the asset, the city, the teams.

    Alias-expanded, because the two venues genuinely disagree on the name:
    Kalshi's event reads "BTC price on Sep 3" where Polymarket's reads "the
    price of Bitcoin", and without the translation those two share no subject
    word at all and the structural path never fires.
    """
    drop = {"price", "above", "below", "over", "under", "between", "highest",
            "lowest", "maximum", "minimum", "temperature", "will", "get",
            "high", "low", "range", "rate", "rates", "value", "level"}
    return expand_tokens(
        {t for t in _tokens(text) if t not in drop and not t.isdigit()})


def structural_match(pm: dict, kx: dict) -> Optional[dict]:
    """Compare two markets as (subject, date, threshold, direction).

    Returns None when either side does not parse — that is not a rejection,
    it just means the text path is the only one that applies. When it does
    return, it says WHICH side of the Kalshi book it compared, because a
    Kalshi "below X" is the NO leg of a Polymarket "above X" and quietly
    comparing YES to YES there would print an inverted, fictional spread.
    """
    pm_text = str(pm.get("title") or "")
    kx_text = str(kx.get("matchText") or kx.get("title") or "")

    # ── timed crypto up/down: both venues state the window exactly ───────
    up = parse_updown(pm)
    if up:
        return updown_match(up, kx)

    # ── the Fed, read as legs rather than as numbers ──────────────────
    leg = fed_leg(pm_text)
    if leg:
        klg = kalshi_fed_leg(kx)
        if not klg:
            return None
        if klg != leg:
            return None
        pm_month = fed_meeting_month(pm)
        kx_month = kalshi_ticker_month(str(kx.get("eventTicker")
                                           or kx.get("conditionId") or ""))
        if not kx_month:
            kd, _ = kx_settlement_date(kx)
            kx_month = (kd or "")[:7]
        if not pm_month or pm_month != kx_month:
            return None
        return {
            "side": "yes", "score": 0.9,
            "for": [
                f"Both name the same Fed meeting ({pm_month}) and the same "
                f"decision ({kx.get('subtitle') or klg}).",
                "Matched on the decision itself, not on wording: Polymarket "
                "writes the size in basis points and Kalshi writes it as a "
                "band, so the two titles never share the same figure.",
            ],
            "against": [],
        }

    # ── threshold questions ──────────────────────────────────────────
    pm_t = parse_threshold(pm_text)
    kx_t = parse_threshold(str(kx.get("subtitle") or "")) \
        or _kx_strike_threshold(kx) \
        or parse_threshold(kx_text)
    rel = compare_thresholds(pm_t, kx_t)
    if not rel:
        return None

    subj_pm, subj_kx = _subject_tokens(pm_text), _subject_tokens(kx_text)
    shared = subj_pm & subj_kx
    if not shared:
        return None

    pd, pd_exact = pm_settlement_date(pm)
    kd, kd_exact = kx_settlement_date(kx)
    if not pd or not kd:
        return None

    against: list[str] = []
    if pd != kd:
        d0, d1 = sorted((pd, kd))
        one_day = (datetime.fromisoformat(d1)
                   - datetime.fromisoformat(d0)).days <= 1
        if not one_day:
            return None
        if _RE_DEADLINE.search(pm_text) and kd > pd:
            # A deadline written from either side of midnight. "by December
            # 31, 2026" and Kalshi's strike date of January 1, 2027 are the
            # same cut-off, not two questions a day apart — and the "by" is
            # what says so, so this never applies to "on September 3".
            for_deadline = True
        elif pd_exact or kd_exact:
            # Two dates both stated outright, and different: different
            # questions. This is the rule that keeps a Sep 3 crypto strike
            # away from the Sep 4 one.
            return None
        else:
            for_deadline = False
            against.append(
                f"Neither venue states the settlement day outright; the two "
                f"close dates are {pd} and {kd}.")
        if for_deadline:
            against.append(
                f"Polymarket writes the deadline as {pd} and Kalshi settles "
                f"it at {kd} — the same cut-off from either side of midnight, "
                f"but check the exact hour if the price is close.")

    for_ = [
        f"Same subject: {', '.join(sorted(shared)[:4])}.",
        f"Same settlement day ({pd}) in US Eastern, which is the timezone "
        f"both venues state their dates in.",
        _threshold_phrase(pm_t, kx_t),
    ]

    if rel == "complement":
        return {
            "side": "no", "score": 0.78, "for": for_,
            "against": against + [
                "Compared against the NO side of the Kalshi market: Kalshi "
                "lists this level from the other direction, so its NO price "
                "is the one that answers this question. The two venues may "
                "still treat the boundary value itself differently.",
            ],
        }
    return {"side": "yes", "score": 0.9, "for": for_, "against": against}


def _threshold_phrase(pm_t: tuple, kx_t: tuple) -> str:
    def fmt(t: tuple) -> str:
        if t[0] == "between":
            return f"between {t[1]:g} and {t[2]:g}"
        return f"{t[0]} {t[1]:g}"
    if pm_t[0] == kx_t[0]:
        return f"Both ask the same threshold: {fmt(pm_t)}."
    return (f"The same level from opposite sides: this market asks "
            f"{fmt(pm_t)}, Kalshi's asks {fmt(kx_t)}.")


def _kx_strike_threshold(kx: dict) -> Optional[tuple]:
    """The threshold Kalshi states in its own structured fields."""
    st = str(kx.get("strikeType") or "").lower()
    floor, cap = kx.get("floorStrike"), kx.get("capStrike")
    if st in ("greater", "greater_or_equal") and floor is not None:
        return ("above", float(floor))
    if st in ("less", "less_or_equal") and cap is not None:
        return ("below", float(cap))
    if st == "between" and floor is not None and cap is not None:
        return ("between", float(floor), float(cap))
    return None


def score_pair(pm: dict, kx: dict) -> tuple[float, list[str], list[str]]:
    """→ (score 0..1, reasons for, reasons against).

    Pure and side-effect free, so the whole matching rule is unit testable
    without touching either venue.
    """
    pm_text = f"{pm.get('title') or ''}"
    kx_text = kx.get("matchText") or kx.get("title") or ""

    for_: list[str] = []
    against: list[str] = []

    # ── hard disqualifier: opposite questions ────────────────────────────
    # "will rise above" vs "will fall below" share almost every word.
    if _are_opposites(pm_text, kx_text):
        return 0.0, [], ["The two questions are phrased as opposites."]

    pt, kt = _tokens(pm_text), _tokens(kx_text)
    if not pt or not kt:
        return 0.0, [], ["One of the two has no matchable text."]
    overlap = len(pt & kt) / len(pt | kt)
    shared = sorted(pt & kt)[:6]
    if shared:
        for_.append(f"Shares {len(pt & kt)} key term(s): {', '.join(shared)}.")

    score = overlap

    # ── numbers must not CONTRADICT ──────────────────────────────────────
    # Scored on disagreement, not agreement. Testing "do they share a number"
    # was defeated by any figure both titles happen to contain: "Bitcoin above
    # $110,000 on Aug 30" and "Bitcoin above $95,000 on Aug 30" both name 30,
    # so both scored as agreeing and the threshold check — the single most
    # important discriminator here — did nothing. What matters is whether each
    # side names a figure the other one does not.
    pn, kn = _numbers(pm_text), _numbers(kx_text)
    shared = pn & kn
    only_pm, only_kx = pn - kn, kn - pn
    if only_pm and only_kx:
        # Each names a figure the other contradicts: same topic, different
        # threshold. Fatal, not a nudge.
        score -= 0.45
        against.append(
            f"Different figures: this market says "
            f"{', '.join(sorted(only_pm))} where Kalshi's says "
            f"{', '.join(sorted(only_kx))}.")
    elif shared and not (only_pm or only_kx):
        score += 0.18
        for_.append(f"Both name the same figure(s): {', '.join(sorted(shared))}.")
    elif only_pm:
        score -= 0.10
        against.append(
            f"This market names a figure ({', '.join(sorted(only_pm))}) that "
            f"the Kalshi market does not.")
    elif only_kx:
        score -= 0.10
        against.append(
            f"The Kalshi market names a figure ({', '.join(sorted(only_kx))}) "
            f"that this one does not.")

    # ── settlement dates should be close ─────────────────────────────────
    pts, kts = _close_ts(pm.get("closeTime")), _close_ts(kx.get("closeTime"))
    if pts and kts:
        days = abs(pts - kts) / 86400.0
        if days <= 2:
            score += 0.12
            for_.append(f"Both settle within {days * 24:.0f}h of each other.")
        elif days <= 14:
            for_.append(f"Settlement dates are {days:.0f} days apart.")
        else:
            score -= 0.22
            against.append(
                f"Settlement dates are {days:.0f} days apart — these are "
                f"probably different periods of the same question.")
    else:
        against.append("One of the two has no settlement date to compare.")

    return max(0.0, min(1.0, score)), for_, against


def _confidence(score: float) -> str:
    """Only two tiers are ever shown. Anything below POSSIBLE is reported as
    no match at all — see find_match."""
    return "strong" if score >= STRONG else "possible"


# ────────────────────────────────────────────────────────────────────────
# Index + search
#
# Kalshi publishes no text search (checked: /search, /search/series and a
# `query` on /events all return nothing), and sweeping its markets is
# unaffordable — ~101,000 rows, 20s, with a fixed ordering that buries the
# near-dated markets anyone would actually compare. So retrieval is two
# stages, and the first stage is the cached SERIES index:
#
#   1. work out which of the ~12,000 series could hold this question,
#   2. pull the EVENTS of only those few and score their markets properly.
#
# Stage 2 is events rather than markets, and that change is load-bearing —
# see kalshi.events_in_series. Stage 1 is _candidate_series, and it is where
# the recall actually came from.
# ────────────────────────────────────────────────────────────────────────

# How many candidate series to expand into events. Each costs one small
# fetch, so this is the feature's per-lookup price — and every fetch rides
# terminal's TTL cache and the kalshi gate, so a second look at the same
# market page is free.
_SERIES_FANOUT = 6
# Of those, how many the blind token route may contribute. The fixture and
# named-topic routes know what they are asking for; the token route is a
# guess, so it does not get to crowd them out.
_TOKEN_FANOUT = 3

# (built_at, rows, token -> row indices)
_index: tuple[float, list[dict], dict[str, set[int]]] | None = None


async def _get_index() -> tuple[list[dict], dict[str, set[int]], bool]:
    global _index
    now = time.monotonic()
    if _index and now - _index[0] < kalshi.TTL_SERIES:
        return _index[1], _index[2], True

    rows, ok = await kalshi.series_index()
    if not rows:
        return [], {}, ok

    inv: dict[str, set[int]] = {}
    for i, r in enumerate(rows):
        for tok in _tokens(r.get("title") or ""):
            inv.setdefault(tok, set()).add(i)
    _index = (now, rows, inv)
    return rows, inv, ok


def _series_candidates(pm_tokens: set[str], rows: list[dict],
                       inv: dict[str, set[int]]) -> list[dict]:
    """Series whose TITLE overlaps the question, best first."""
    counts: dict[int, int] = {}
    for tok in pm_tokens:
        for i in inv.get(tok, ()):
            counts[i] = counts.get(i, 0) + 1
    scored = []
    for i, n in counts.items():
        st = rows[i]
        stoks = _tokens(st.get("title") or "")
        if not stoks:
            continue
        # Coverage of the SERIES title by the question. A series titled
        # "Fed decision" is fully covered by a question about the Fed decision;
        # scoring by Jaccard would punish it for the question being longer.
        cover = len(pm_tokens & stoks) / len(stoks)
        if n >= 1 and cover >= 0.5:
            scored.append((cover, n, st))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [st for _, _, st in scored[:_TOKEN_FANOUT]]


def _candidate_series(pm: dict, rows: list[dict],
                      inv: dict[str, set[int]]) -> list[dict]:
    """Which Kalshi series could possibly hold this question, best first.

    Three routes, in order of how much they actually know:

    1. **The fixture route.** A Polymarket sports slug states the league and
       the date outright, and Kalshi's tickers are KX + league + family. That
       is a lookup, not a guess, and it is the route that took sports from
       unmatchable to exact.
    2. **The named-topic route.** A handful of question families where the
       Kalshi series title shares no words with the question at all — "LA
       LIGA" for a question about Levante, "How low will Bitcoin fall this
       year?" for "Will Bitcoin dip to $45,000".
    3. **The token route**, as before, but over alias-expanded tokens so BTC
       reaches Bitcoin and "dip" reaches "fall".

    Every ticker either route proposes is checked against the live series
    index first, so a stale table entry costs nothing and fetches nothing.
    """
    by_ticker = {r["ticker"]: r for r in rows}
    picked: list[dict] = []
    seen: set[str] = set()

    def add(ticker: str) -> None:
        r = by_ticker.get(ticker)
        if r and ticker not in seen:
            seen.add(ticker)
            picked.append(r)

    title = str(pm.get("title") or "")

    fx = parse_fixture(pm)
    if fx:
        for fam in fx["families"]:
            add(fx["prefix"] + fam)

    up = parse_updown(pm)
    if up:
        # The slug names the asset outright, and Kalshi names the series after
        # it. Nothing about "Bitcoin Up or Down - September 3, 12:00AM-12:15AM
        # ET" and "Bitcoin price up down" would have found each other on words.
        add(up["series"])

    sw = parse_season_winner(pm)
    if sw:
        add(sw["series"])

    for pattern, tickers in _TOPIC_SERIES:
        if re.search(pattern, title, re.IGNORECASE):
            for t in tickers:
                add(t)

    pt = expand_tokens(_tokens(title))
    for sx in _series_candidates(pt, rows, inv):
        if sx["ticker"] not in seen:
            seen.add(sx["ticker"])
            picked.append(sx)

    return picked[:_SERIES_FANOUT]


# ────────────────────────────────────────────────────────────────────────
# Event, then leg
#
# Kalshi splits a question the way an exchange does: an EVENT is the contest
# ("New York M vs Tampa Bay", "La Liga Champion 2026-27") and a MARKET is one
# leg of it ("Tampa Bay wins", "Levante"). Polymarket lists each leg as its
# own market with the whole question in the title.
#
# So matching is two decisions, and they have very different failure modes.
# Getting the EVENT wrong shows an unrelated market. Getting the LEG wrong is
# worse: the titles look right, the prices are real, and the spread between
# "Mets win" and "Rays win" is a large, confident, completely inverted number.
# That is why a leg is only accepted when it beats the runner-up outright.
# ────────────────────────────────────────────────────────────────────────

_RE_WILL_WIN = re.compile(r"^will\s+(.+?)\s+(?:win|beat|defeat)\b", re.IGNORECASE)
_RE_WHO_WILL_WIN = re.compile(r"^who\s+will\s+win\b", re.IGNORECASE)
_RE_LABEL = re.compile(
    r"^(?:\d?(?:st|nd)?\s*half\s+)?(?:game\s+)?"
    r"(?:spread|handicap|total|moneyline)\s*:\s*(.+?)\s*[\(:]", re.IGNORECASE)
_RE_VS = re.compile(r"^(.*?)\s+vs\.?\s+(.*)$", re.IGNORECASE)


def pm_yes_claim(pm: dict, fx: dict) -> Optional[str]:
    """What a Polymarket fixture market's YES actually claims, in words.

    Polymarket's YES is never labelled: the title is "New York Mets vs. Tampa
    Bay Rays" and the reader is expected to know YES means the first-named
    side. Making that explicit here is the whole reason the leg can be picked
    safely — and why an inverted pairing is an error rather than a coin flip.
    """
    title = str(pm.get("title") or "").strip()
    tail = fx.get("tail") or ""
    line = fx.get("line")

    if "draw" in tail or re.search(r"\bdraw\b|\btie\b", title, re.IGNORECASE):
        return "tie draw"
    if fx["kind"] == "total":
        return f"over {line:g}" if line is not None else None
    if fx["kind"] == "spread":
        m = _RE_LABEL.match(title)
        team = m.group(1) if m else None
        if not team:
            return None
        return f"{team} over {line:g}" if line is not None else team

    # The venue states it. Reading it from the words is not possible on a game
    # winner: "Who will win ... PHI 76ers vs DET Pistons" names no YES at all.
    named = str(pm.get("yesTeam") or "").strip()
    if named:
        return f"{named} {pm.get('yesTeamCode') or ''}".strip()
    # Without it this question names both teams and no YES. "vs" below would read
    # the sentence up to the first team as a claim, and that is a guess.
    if _RE_WHO_WILL_WIN.match(title):
        return None

    m = _RE_WILL_WIN.match(title)
    if m:
        return m.group(1)
    # "US Open ATP: Lloyd Harris vs Stefanos Tsitsipas" — the part before the
    # colon is the competition, not a side.
    head = title
    if ":" in head and " vs" not in head.split(":", 1)[0].lower():
        head = head.split(":", 1)[1].strip()
    m = _RE_VS.match(head)
    if m:
        return m.group(1).strip()
    return None


def _leg_score(claim: str, kx: dict) -> float:
    """How well one Kalshi leg answers the Polymarket YES claim.

    Names carry a point each and the figure carries three, because within one
    event the figure is the whole question: every leg of "Total Runs" says
    "Over N runs scored" and only N tells them apart. A figure that
    CONTRADICTS is scored below zero, never merely unrewarded.
    """
    kt = _tokens(str(kx.get("subtitle") or kx.get("title") or ""))
    # A leg's ticker ends in its side's code and the venues share most of them:
    # "Senators" and "Ottawa" have no word in common, ott and OTT do.
    code = str(kx.get("conditionId") or "").rsplit("-", 1)[-1]
    if code.isalpha():
        kt |= _tokens(code)
    ct = _tokens(claim)
    if not ct or not kt:
        return 0.0
    score = float(len(ct & kt))
    cn = _numbers(claim)
    kn = _numbers(str(kx.get("subtitle") or kx.get("title") or ""))
    if cn and kn:
        score += 3.0 if cn & kn else -4.0
    return score


def pick_leg(claim: str, legs: list[dict]) -> Optional[tuple[dict, float, float]]:
    """The one leg this claim names, or None when it is not clear-cut."""
    scored = sorted(((_leg_score(claim, k), k) for k in legs),
                    key=lambda x: -x[0])
    if not scored or scored[0][0] <= 0:
        return None
    best, runner = scored[0], (scored[1] if len(scored) > 1 else (0.0, None))
    if best[0] - runner[0] < 1.0:
        return None     # two legs answer it equally well: refuse to guess
    return best[1], best[0], runner[0]


_STRIP_DIGITS = re.compile(r"^\d+")


def event_code(ticker: str) -> str:
    """The side codes Kalshi packs into an event ticker after the date.

    "KXMLBGAME-26SEP021840NYMTB" → "NYMTB"; "KXCHLLDPGAME-26SEP02DEPNUB" →
    "DEPNUB". Polymarket writes the same two sides into its slug, and for most
    leagues the two venues use the same abbreviations — which turns "which of
    tonight's fifteen games is this" from a guess into a lookup.
    """
    up = str(ticker or "").upper()
    m = _KX_TICKER_DATE.search(up)
    if not m:
        return ""
    return _STRIP_DIGITS.sub("", up[m.end():])


def _event_score(pm_title: str, sides: tuple, legs: list[dict]) -> float:
    """How strongly one Kalshi event answers "which fixture is this?"."""
    code = event_code(str(legs[0].get("eventTicker") or ""))
    hits = 0
    for s in sides:
        s = (s or "").upper()
        if len(s) >= 3 and (s in code or s[:3] in code):
            hits += 1
    ev_text = " ".join(str(legs[0].get(k) or "")
                       for k in ("eventTitle", "eventSubtitle"))
    # The side codes are the strong signal; the words are the tie-break, and
    # comparing the WHOLE Polymarket title (both sides) rather than just the
    # YES claim is what separates "New York M vs Tampa Bay" from "New York Y
    # vs Los Angeles A" on a night when both are playing.
    return 2.0 * hits + len(_tokens(pm_title) & _tokens(ev_text))


def _fixture_verdict(pm: dict, fx: dict, markets: list[dict]) -> Optional[dict]:
    """Match a Polymarket game market to one Kalshi leg of the same fixture.

    Two decisions, each required to be decisive on its own:

    * **Which fixture** — the slug's league and date, then the side codes.
    * **Which leg** — the Polymarket YES claim against each leg's subtitle.

    A tie at either step is answered with None. Half of a fixture pairing is
    not half right: comparing "Mets win" to "Rays win" produces two real
    prices, a large spread and a completely inverted claim.
    """
    claim = pm_yes_claim(pm, fx)
    if not claim:
        return None

    priced = [k for k in markets if k.get("probability") is not None]
    dates = [fx["date"]]
    # Polymarket's slug date is normally the fixture's own date, but a market
    # cloned from an earlier round can carry a stale one (seen live: a Sep 2
    # Chilean fixture slugged 2026-08-01). The close time, read in Eastern, is
    # the fallback — never the first choice, because for most sports markets
    # BOTH venues pad it by days.
    close_day = et_date(pm.get("closeTime"))
    if close_day and close_day not in dates:
        dates.append(close_day)

    for date in dates:
        same_day = [k for k in priced
                    if kalshi_ticker_date(str(k.get("eventTicker") or "")) == date]
        if not same_day:
            continue
        by_event: dict[str, list[dict]] = {}
        for k in same_day:
            by_event.setdefault(str(k.get("eventTicker") or ""), []).append(k)

        title = str(pm.get("title") or "")
        ranked = sorted(
            ((_event_score(title, fx["sides"], legs), tk, legs)
             for tk, legs in by_event.items()), key=lambda x: -x[0])
        top = ranked[0]
        if top[0] <= 0:
            continue
        if len(ranked) > 1 and top[0] - ranked[1][0] < 1.0:
            continue    # two fixtures fit equally well: refuse to pick one

        picked = pick_leg(claim, top[2])
        if not picked:
            continue
        leg, best, runner = picked
        against = [
            "Kalshi and Polymarket may void or push a postponed fixture on "
            "different rules, so a delayed game can still settle differently.",
        ]
        if date != fx["date"]:
            against.append(
                f"Polymarket's slug dates this {fx['date']}; the fixture "
                f"matched is Kalshi's on {date}, which is the day this market "
                f"closes.")
        return {
            "market": leg, "score": 0.88,
            "for": [
                f"Same fixture on the same day: Polymarket's slug says "
                f"{fx['league'].upper()} on {date}, and this is Kalshi's "
                f"\"{leg.get('eventTitle')}\" for that date.",
                f"Polymarket's YES here is \"{claim}\"; the Kalshi leg "
                f"compared is \"{leg.get('subtitle') or leg.get('title')}\" — "
                f"it outscored the next leg {best:g} to {runner:g}, so the "
                f"side is not a guess.",
            ],
            "against": against,
            "side": "yes",
        }
    return None


async def find_match(pm: dict) -> Optional[dict]:
    """Best Kalshi candidate for a Polymarket market, or None."""
    rows, inv, _ = await _get_index()
    if not rows:
        return None
    if not _tokens(pm.get("title") or ""):
        return None

    series = _candidate_series(pm, rows, inv)
    if not series:
        return None

    pool: list[tuple[dict, dict]] = []       # (market, series row)
    for sx in series:
        try:
            markets = await kalshi.events_in_series(sx["ticker"])
        except Exception as e:  # noqa: BLE001
            logger.debug("cross-venue: series %s failed: %s", sx["ticker"], e)
            continue
        for kx in markets:
            pool.append((kx, sx))

    # ── the fixture route first: it knows the league and the day outright ──
    fx = parse_fixture(pm)
    if fx:
        for sx in series:
            legs = [k for k, s in pool if s["ticker"] == sx["ticker"]]
            v = _fixture_verdict(pm, fx, legs)
            if v:
                v["series"] = sx
                return v

    # A season-long winner is a lookup too. Text can't help where it fails.
    sw = parse_season_winner(pm)
    if sw:
        sx = next((s for s in series if s["ticker"] == sw["series"]), None)
        v = _season_winner_verdict(sw, [k for k, s in pool if s is sx]) if sx else None
        if v:
            v["series"] = sx
        return v

    # A timed up/down window either lines up to the second or it does not.
    # There is no wording on either side that could make a 5-minute window and
    # a 15-minute one the same question, so the text scorer is not allowed to
    # try — a near-miss window is the failure mode with the sharpest teeth
    # here, because the prices genuinely do look alike. A Fed decision is the
    # same: "Cut 25bps" and "Cut >25bps" read almost alike and exclude each other.
    structural_only = (parse_updown(pm) is not None
                       or fed_leg(str(pm.get("title") or "")) is not None)

    cands: list[dict] = []
    for kx, sx in pool:
        if kx.get("probability") is None:
            continue        # an unpriced market cannot be compared to anything
        sv = structural_match(pm, kx)
        if sv:
            cands.append({
                "market": kx, "score": sv["score"], "for": list(sv["for"]),
                "against": list(sv["against"]), "series": sx,
                "side": sv["side"],
            })
            continue
        if structural_only:
            continue
        score, for_, against = score_pair(pm, kx)
        if score > 0:
            cands.append({
                "market": kx, "score": round(score, 3), "for": for_,
                "against": against, "series": sx, "side": "yes",
            })

    for cand in sorted(cands, key=lambda c: -c["score"]):
        if cand["score"] < POSSIBLE:
            break
        if _beats_its_siblings(cand, cands):
            return cand
    # Below POSSIBLE we report NO match rather than a bad one. A weak pairing
    # shown next to a real price invites the user to squint at two unrelated
    # markets; "no counterpart" is both truer and more useful.
    return None


# How far a leg must beat the other legs of its own Kalshi event.
_SIBLING_MARGIN = 0.08


def _beats_its_siblings(cand: dict, cands: list[dict]) -> bool:
    """Whether this leg is clearly the one the question names.

    The legs of one Kalshi event differ ONLY by the entity — twenty rows that
    all read "Will <club> win La Liga?" — so a text scorer that likes one
    likes them all. Caught on this fixture: a question about Getafe scored
    Barcelona, Real Madrid and Atletico identically at 0.40, the correct leg
    happened to be unquoted, and the panel would have shown Barcelona's price
    under a question about Getafe.

    So a leg only counts when it beats its own siblings. If the question does
    not distinguish them, the honest answer is that we cannot either.
    """
    ev = str(cand["market"].get("eventTicker") or "")
    if not ev:
        return True
    mine = cand["market"].get("conditionId")
    sibs = [c["score"] for c in cands
            if str(c["market"].get("eventTicker") or "") == ev
            and c["market"].get("conditionId") != mine]
    return not sibs or cand["score"] - max(sibs) >= _SIBLING_MARGIN


# ────────────────────────────────────────────────────────────────────────
# The comparison
# ────────────────────────────────────────────────────────────────────────

# Interval → (seconds per bucket, lookback seconds, Kalshi period_interval min)
_BUCKETS = {
    "1h": (60, 3600, 1),
    "6h": (300, 6 * 3600, 5),
    "1d": (3600, 24 * 3600, 60),
    "1w": (3600, 7 * 24 * 3600, 60),
    "max": (86400, 90 * 24 * 3600, 1440),
}


def _bucket(points: list[dict], size: int) -> dict[int, float]:
    """Snap a series onto a shared grid. Both venues already bucket their own
    history; snapping to a common bucket START acknowledges that rather than
    inventing anything. The LAST observation in a bucket wins."""
    out: dict[int, float] = {}
    for p in points:
        t, v = p.get("t"), p.get("p")
        if t is None or v is None:
            continue
        out[int(t) - (int(t) % size)] = float(v)
    return out


NO_MATCH_NOTE = (
    "The app found no Kalshi market asking this question. Kalshi names many "
    "markets differently, so one may still exist there, but a guessed pairing "
    "could show a price gap that isn't real.")


async def compare(condition_id: str, interval: str = "1d") -> dict:
    """Both venues' prices and history for one Polymarket market."""
    iv = interval if interval in _BUCKETS else "1d"
    bucket_s, lookback_s, kx_period = _BUCKETS[iv]

    pm = await term.market(condition_id)
    if not pm:
        return _empty(condition_id, iv, "Could not read this Polymarket market.")

    if term.is_offline():
        return _empty(condition_id, iv, term.OFFLINE_REASON)

    match = await find_match(pm)
    if not match:
        # We can only say what the lookup found, not what Kalshi lists: it names
        # many markets differently. Where we can say what is actually different —
        # which window Kalshi is quoting, and why this one has no twin — we say it.
        return _empty(
            condition_id, iv, await explain_no_match(pm) or NO_MATCH_NOTE)

    kx = match["market"]
    confidence = _confidence(match["score"])
    side = match.get("side") or "yes"

    # Prices. Compare like with like — which is NOT always YES against YES.
    # Kalshi lists plenty of levels from the other direction ("$75,999.99 or
    # below" where Polymarket asks "above $76,000"), and there the answer to
    # the Polymarket question is Kalshi's NO price. Inverting it is correct;
    # inverting it silently would be the same fictional-edge failure as a
    # wrong pairing, so the side is stamped on the response and named in the
    # note, and the history line below is inverted with it.
    pm_p = pm.get("probability")
    kx_p = kx.get("probability")
    kx_bid, kx_ask = kx.get("bid"), kx.get("ask")
    kx_leg = kx.get("subtitle")
    if side == "no":
        kx_p = None if kx_p is None else round(100.0 - kx_p, 2)
        kx_bid, kx_ask = (
            None if kx.get("ask") is None else round(100.0 - kx["ask"], 2),
            None if kx.get("bid") is None else round(100.0 - kx["bid"], 2))
        kx_leg = (f"NO on \"{kx.get('subtitle') or kx.get('title')}\""
                  if (kx.get("subtitle") or kx.get("title")) else "NO")

    spread = None
    if confidence != "strong":
        spread_note = (
            "No spread is shown: this pairing is not certain enough, and a "
            "price difference between two markets that may not be the same "
            "question is a meaningless number. Read both titles above and "
            "decide for yourself.")
    elif pm_p is None or kx_p is None:
        spread_note = (
            "One of the two venues has no price right now, so there is no "
            "difference to compute.")
    else:
        spread = round(kx_p - pm_p, 2)
        which = ("Kalshi's NO side prices this"
                 if side == "no" else "Kalshi prices this")
        spread_note = (
            f"{which} {abs(spread):.1f}¢ "
            f"{'higher' if spread > 0 else 'lower'} than Polymarket."
            if abs(spread) >= 0.5 else
            f"{which} the same, to within half a cent.")

    # History, snapped to a shared grid.
    now = int(time.time())
    pm_hist = await term.price_history(condition_id, "yes", iv)
    kx_pts = await kalshi.candles(
        kx["conditionId"], start_ts=now - lookback_s, end_ts=now,
        period_interval=kx_period,
        series_ticker=kx.get("seriesTicker") or "")

    if side == "no":
        # The candles are Kalshi's YES history; the panel is comparing NO.
        kx_pts = [{"t": p["t"], "p": round(100.0 - p["p"], 2)} for p in kx_pts]

    pm_b = _bucket(pm_hist.get("points") or [], bucket_s)
    kx_b = _bucket(kx_pts, bucket_s)
    grid = sorted(set(pm_b) | set(kx_b))
    # A bucket where a venue did not trade is a HOLE for that venue, not a
    # carried-forward price. The chart breaks its line there, same rule as
    # everywhere else.
    series = [{"t": t, "p": pm_b.get(t), "k": kx_b.get(t)} for t in grid]
    both = sum(1 for r in series if r["p"] is not None and r["k"] is not None)

    hist_note = None
    if not kx_pts:
        hist_note = (
            "Kalshi has no candle history for this market at this resolution, "
            "so only the Polymarket line is drawn.")
    elif both == 0:
        hist_note = (
            "The two venues have no overlapping periods in this window, so "
            "the lines never appear side by side. Try a longer interval.")
    elif both < len(grid) * 0.25:
        hist_note = (
            f"The venues overlap in only {both} of {len(grid)} periods — the "
            f"thinner market simply did not trade in the rest.")

    return {
        "conditionId": condition_id,
        "interval": iv,
        "polymarket": {
            "title": pm.get("title"), "probability": pm_p,
            "bid": pm.get("bid"), "ask": pm.get("ask"),
            "spread": pm.get("spread"), "closeTime": pm.get("closeTime"),
            "volume24hUsd": pm.get("volume24hUsd"),
            "liquidityUsd": pm.get("liquidityUsd"),
        },
        "kalshi": {
            "ticker": kx["conditionId"],
            "title": kx.get("eventTitle") or kx.get("title"),
            "subtitle": kx_leg,
            "probability": kx_p,
            "bid": kx_bid, "ask": kx_ask,
            "spread": kx.get("spread"), "closeTime": kx.get("closeTime"),
            "volume24hUsd": kx.get("volume24hUsd"),
            "liquidityUsd": kx.get("liquidityUsd"),
            "url": kalshi.web_url(kx["conditionId"], kx.get("eventSlug")),
            "side": side,
        },
        "match": {
            "confidence": confidence,
            "score": match["score"],
            "for": match["for"],
            "against": match["against"],
            # Which two things were actually compared. Never inferred by the
            # reader: a NO-side comparison is a different claim from a YES-side
            # one, and the difference is the sign of the spread.
            "comparedSides": {"polymarket": "yes", "kalshi": side},
        },
        "spreadCents": spread,
        "spreadNote": spread_note,
        "series": series,
        "overlapPeriods": both,
        "historyNote": hist_note,
        "venueNote": kalshi.VENUE_NOTE,
        "caveats": kalshi.SPREAD_CAVEATS,
        "fetchedAt": _now_iso(),
        "note": None,
    }


def _empty(condition_id: str, interval: str, note: str) -> dict:
    return {
        "conditionId": condition_id, "interval": interval,
        "polymarket": None, "kalshi": None, "match": None,
        "spreadCents": None, "spreadNote": None,
        "series": [], "overlapPeriods": 0, "historyNote": None,
        "venueNote": kalshi.VENUE_NOTE, "caveats": kalshi.SPREAD_CAVEATS,
        "fetchedAt": _now_iso(), "note": note,
    }
