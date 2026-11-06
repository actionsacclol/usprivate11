"""Every agent fair value, kept until the market resolves, then scored.

The question this answers is the only one that matters before anybody lets an
agent size a position: *does it beat the price?* Not "is it usually right" — a
model that says 90% on every market priced at 90¢ is usually right and has no
edge at all. The benchmark is the market's own mid at the moment the forecast
was made, and the score is the Brier score of each against what actually
happened.

Whose numbers these are. The app itself never prices a market — the AI
analysis panel is number-free by construction (ai_analysis.py). The numbers
here are the AGENTS' own: an agent must commit to a fair value before it may
buy (mcp_tools.vet), and this ledger is how its claim gets checked. It is a
measurement of somebody else's number, never an estimate the app makes.

Rules this module holds to:

  * The comparison is PAIRED. A forecast is scored against the mid that existed
    when it was made, never a later one — scoring a 2pm forecast against the
    5pm price would hand the market the afternoon's news for free, or take it
    away.
  * A forecast made when there was no two-sided quote has no market to beat.
    Its market Brier is None, it is counted in the agent-only score, and it is
    excluded from the head-to-head rather than compared against an invented
    50¢.
  * One forecast per market per agent is scored. Re-asking five times on one
    market must not count as five pieces of evidence. It is the first one a
    buy that went out cited (the one the edge gate let the money in on), else
    the most recent: nothing recorded after the trade can replace the number
    the trade was argued on.
  * "The agent beat the market" is a claim that needs a sample. Under
    MIN_SCORED the verdict is "too-few", and inside two standard errors it is
    "indistinguishable".

Outcomes come from the same resolution reader the Track screen and the paper
book use (terminal_trade._resolutions_for), so the three can never disagree
about what a market paid.

A market UMA resolves 50-50 (a cancelled game, a series game never played:
both tokens redeem for 50c) is scored with outcome 0.5 — the YES payout it
actually made, read by the same rule the paper book settles it with
(terminal_paper.split_resolution). Brier is a proper score for any outcome in
[0, 1], so (p - 0.5)^2 against (mid - 0.5)^2 is still a fair paired
comparison: a fair value is a price of the YES token, and that token paid
50c. Excluding splits instead would leave them pending forever and quietly
drop the markets where a confident forecast was most wrong about what the
contract would pay. In the reliability table a split counts as half a YES.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

import db

logger = logging.getLogger("krypt.forecast")

SOURCES = ("mcp",)

# Below this many paired, settled markets the scoreboard declines to say who is
# better. Brier differences on prediction markets are small and noisy.
MIN_SCORED = 30

# How many pending markets one resolver pass looks up.
RESOLVE_BATCH = 200

# A 50-50 split is invisible to terminal_trade._resolutions_for (it knows only
# 100/0), so a pending market the resolver has already failed to decide on an
# earlier pass is re-read for one. At most this often per market: a market
# with no close time can sit pending for months, and each check is a Gamma read.
SPLIT_RECHECK_SEC = 900.0
_split_checked: dict[str, float] = {}


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _cents_or_none(v: Any) -> Optional[float]:
    """Strictly inside 0..100, or None. 0 and 100 are settled outcomes, not
    prices."""
    x = _f(v)
    if x is None or x <= 0 or x >= 100:
        return None
    return x


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def record(*, condition_id: str, prob_yes: float, market: Optional[dict] = None,
           rationale: str = "", model: str = "", client: str = "",
           agent_id: Optional[str] = None, network: str = "mainnet",
           source: str = "mcp") -> int:
    """Store one forecast with the market as it stood at that moment.

    `market` is a terminal MarketSummary row (bid/ask/mid in cents for YES).
    Its absent fields stay absent: a market with no bid has no mid, and the
    row says so."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    p = _f(prob_yes)
    if p is None or not (0.01 <= p <= 0.99):
        raise ValueError("probability must be between 0.01 and 0.99")
    cid = str(condition_id or "").strip()
    if not cid:
        raise ValueError("conditionId required")
    m = market or {}
    with db.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO ai_forecasts
               (created_at, network, condition_id, title, source, model, client,
                prob_yes, market_mid_cents, yes_bid_cents, yes_ask_cents,
                close_time, rationale, agent_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_now(), network or "", cid, str(m.get("title") or "")[:300], source,
             str(model or "")[:80], str(client or "")[:80], round(p, 4),
             _cents_or_none(m.get("mid")), _cents_or_none(m.get("bid")),
             _cents_or_none(m.get("ask")), m.get("closeTime"),
             str(rationale or "")[:2000], str(agent_id or "") or "default"),
        )
        return int(cur.lastrowid)


def get(forecast_id: int) -> Optional[dict]:
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM ai_forecasts WHERE id=?", (int(forecast_id),)).fetchone()
    return dict(row) if row else None


def _pending_with_checked(conn, limit: int) -> list[tuple[str, bool]]:
    """(conditionId, looked up on an earlier pass?) for pending markets."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # A NULL close time is looked up anyway: better one wasted read than a
    # forecast that can never be scored because the record lacked a date.
    rows = conn.execute(
        """SELECT condition_id, MIN(COALESCE(last_checked_at, '')) AS lc
           FROM ai_forecasts
           WHERE outcome IS NULL
             AND (close_time IS NULL
                  OR replace(substr(close_time, 1, 19), ' ', 'T') <= ?)
           GROUP BY condition_id ORDER BY lc ASC LIMIT ?""",
        (now, int(limit)),
    ).fetchall()
    return [(r["condition_id"], bool(r["lc"])) for r in rows]


async def _splits_for(condition_ids: list[str]) -> set[str]:
    """The ones among these markets that resolved 50-50. A market that could
    not be read is simply not in the set (still unknown, never a split)."""
    import terminal as term
    import terminal_paper
    now = time.monotonic()
    ids = [c for c in dict.fromkeys(str(c) for c in condition_ids if c)
           # None = never checked; NOT a check at monotonic zero.
           if _split_checked.get(c) is None
           or now - _split_checked[c] > SPLIT_RECHECK_SEC]
    out: set[str] = set()
    for i in range(0, len(ids), 20):
        chunk = ids[i:i + 20]
        for c in chunk:
            _split_checked[c] = now
        try:
            rows = await term._summaries_for(chunk)
        except Exception as e:  # noqa: BLE001 — a miss stays pending
            logger.debug("forecast: split lookup failed: %s", e)
            continue
        for m in rows:
            if terminal_paper.split_resolution(m):
                out.add(str(m.get("conditionId") or ""))
    return out


async def fetch_outcomes(condition_ids: list[str],
                         split_candidates: Optional[list[str]] = None,
                         ) -> dict[str, Optional[float]]:
    """{conditionId: YES payout — 1, 0, or 0.5 on a 50-50 split — or None
    while unresolved/unreadable}.

    `split_candidates`: markets worth a second read for a split when the
    100/0 reader cannot decide them (resolve_pending passes the ones already
    pending on an earlier pass)."""
    import terminal_trade
    res = await terminal_trade._resolutions_for(condition_ids)
    out: dict[str, Optional[float]] = {
        c: (1.0 if o == "yes" else 0.0 if o == "no" else None)
        for c, o in res.items()}
    undecided = [c for c in (split_candidates or []) if out.get(c) is None]
    if undecided:
        for c in await _splits_for(undecided):
            out[c] = 0.5
    return out


async def resolve_pending() -> int:
    """Look up outcomes for forecasts whose markets have closed. Returns how
    many forecast rows were resolved."""
    with db.get_db() as conn:
        pending = _pending_with_checked(conn, RESOLVE_BATCH)
    cids = [c for c, _ in pending]
    if not cids:
        return 0
    # Only a market an earlier pass already failed to decide gets the extra
    # split read: almost every market decides 100/0 on its first pass.
    outcomes = await fetch_outcomes(cids, [c for c, seen in pending if seen])
    stamp = _now()
    n = 0
    with db.get_db() as conn:
        for c in cids:
            o = outcomes.get(c)
            if o is None:
                conn.execute(
                    "UPDATE ai_forecasts SET last_checked_at=? "
                    "WHERE condition_id=? AND outcome IS NULL", (stamp, c))
                continue
            cur = conn.execute(
                "UPDATE ai_forecasts SET outcome=?, resolved_at=?, last_checked_at=? "
                "WHERE condition_id=? AND outcome IS NULL", (o, stamp, stamp, c))
            n += cur.rowcount or 0
    if n:
        logger.info("forecast: resolved %d forecast(s)", n)
    return n


# ── Scoring ─────────────────────────────────────────────────────────────────

def _agent_key(r: dict) -> str:
    return str(r.get("agent_id") or "") or "default"


def _latest_per_market(rows: list[dict], per_agent: bool = True,
                       cited: frozenset = frozenset()) -> list[dict]:
    """One row per market (per agent): the first a buy cited (`cited` holds
    those forecast ids), else the latest."""
    seen: dict[tuple, dict] = {}
    for r in rows:   # rows arrive oldest-first, so the last write wins...
        k = (_agent_key(r) if per_agent else "", r["condition_id"])
        # ...unless money already went in on the one kept.
        if k in seen and seen[k].get("id") in cited:
            continue
        seen[k] = r
    return list(seen.values())


def _cited(conn) -> frozenset:
    """Forecast ids cited by an agent buy that went out (or may have: a send
    that errored). A refused or still-pending buy spent nothing and pins
    nothing."""
    try:
        rows = conn.execute(
            "SELECT DISTINCT forecast_id FROM mcp_orders WHERE action='buy' "
            "AND forecast_id IS NOT NULL AND (ok=1 OR status='unknown')").fetchall()
    except Exception as e:  # noqa: BLE001 — a scoreboard without the pin beats none
        logger.debug("forecast: cited ids unreadable: %s", e)
        return frozenset()
    return frozenset(int(r[0]) for r in rows)


def _verdict(n: int, mean: Optional[float], se: Optional[float]) -> str:
    if n < MIN_SCORED or mean is None:
        return "too-few"
    # se == 0 with a nonzero mean is a perfectly CONSISTENT difference, the
    # most decisive case there is — not an undefined one.
    if se is None or mean == 0 or abs(mean) < 2 * se:
        return "indistinguishable"
    # The difference is agent minus market, so negative means the agent's
    # squared error was smaller.
    return "agent-better" if mean < 0 else "market-better"


def score(rows: list[dict]) -> dict:
    """Score already-resolved, already-deduplicated forecasts. Pure."""
    ai_sq: list[float] = []
    diffs: list[float] = []
    mkt_sq: list[float] = []
    for r in rows:
        o = _f(r.get("outcome"))
        p = _f(r.get("prob_yes"))
        if o is None or p is None:
            continue
        a = (p - o) ** 2
        ai_sq.append(a)
        mid = _cents_or_none(r.get("market_mid_cents"))
        if mid is not None:
            m = (mid / 100.0 - o) ** 2
            mkt_sq.append(m)
            diffs.append(a - m)

    n_pair = len(diffs)
    mean = se = None
    if n_pair:
        mean = sum(diffs) / n_pair
        if n_pair > 1:
            var = sum((d - mean) ** 2 for d in diffs) / (n_pair - 1)
            se = math.sqrt(var / n_pair)
    brier_mkt = (sum(mkt_sq) / len(mkt_sq)) if mkt_sq else None
    brier_paired = (brier_mkt + mean) if (brier_mkt is not None and mean is not None) else None
    skill = None
    if brier_mkt and brier_paired is not None:
        # Brier skill vs the market: >0 means the agent's error was smaller.
        skill = 1.0 - brier_paired / brier_mkt
    return {
        "n": len(ai_sq),
        "nPaired": n_pair,
        "brierAgent": round(sum(ai_sq) / len(ai_sq), 4) if ai_sq else None,
        "brierAgentPaired": round(brier_paired, 4) if brier_paired is not None else None,
        "brierMarket": round(brier_mkt, 4) if brier_mkt is not None else None,
        "skill": round(skill, 4) if skill is not None else None,
        "diffMean": round(mean, 5) if mean is not None else None,
        "diffSe": round(se, 5) if se is not None else None,
        "verdict": _verdict(n_pair, mean, se),
    }


def buckets(rows: list[dict], width: float = 0.1) -> list[dict]:
    """Reliability table: of the times an agent said ~30%, how often was it
    YES? (A 50-50 split counts as half a YES: `hitRate` is the mean payout.)
    Empty buckets are omitted rather than drawn at zero."""
    out: dict[int, list] = {}
    for r in rows:
        o = _f(r.get("outcome"))
        p = _f(r.get("prob_yes"))
        if o is None or p is None:
            continue
        k = min(int(p / width), int(1 / width) - 1)
        out.setdefault(k, []).append((p, o))
    res = []
    for k in sorted(out):
        pts = out[k]
        res.append({
            "lo": round(k * width, 2), "hi": round((k + 1) * width, 2),
            "n": len(pts),
            "meanForecast": round(sum(p for p, _ in pts) / len(pts), 4),
            "hitRate": round(sum(o for _, o in pts) / len(pts), 4),
        })
    return res


def by_agent(all_rows: list[dict], cited: frozenset = frozenset()) -> list[dict]:
    """One row per named agent: its own paired score on one forecast per
    resolved market (_latest_per_market). Pure.

    What the user compares when deciding which agent goes live: two agents on
    the same model with different guides are two records, and one agent
    driven from two clients is one."""
    groups: dict[str, list[dict]] = {}
    for r in all_rows:   # oldest first
        groups.setdefault(_agent_key(r), []).append(r)
    out = []
    for aid, rows in groups.items():
        resolved = _latest_per_market([r for r in rows if r.get("outcome") is not None],
                                      cited=cited)
        models = sorted({(r.get("model") or "").strip() for r in rows} - {""})
        clients = sorted({(r.get("client") or "").strip() for r in rows} - {""})
        out.append({
            "agentId": aid,
            "total": len(rows),
            "pending": sum(1 for r in rows if r.get("outcome") is None),
            "lastAt": rows[-1].get("created_at"),
            "models": models[:6], "clients": clients[:6],
            **score(resolved),
        })
    out.sort(key=lambda f: f["lastAt"] or "", reverse=True)
    return out


def _row(r: dict) -> dict:
    o = _f(r.get("outcome"))
    p = _f(r.get("prob_yes"))
    mid = _cents_or_none(r.get("market_mid_cents"))
    return {
        "id": int(r["id"]),
        "createdAt": r.get("created_at"),
        "conditionId": r["condition_id"],
        "title": r.get("title") or None,
        "model": r.get("model") or None,
        "client": r.get("client") or None,
        "agentId": _agent_key(r),
        "fairValueCents": round(p * 100, 1) if p is not None else None,
        "marketMidCents": mid,
        "outcome": o,
        "resolvedAt": r.get("resolved_at"),
        "brierAgent": round((p - o) ** 2, 4) if (o is not None and p is not None) else None,
        "brierMarket": round((mid / 100 - o) ** 2, 4) if (o is not None and mid is not None) else None,
    }


def scoreboard(recent: int = 40) -> dict:
    with db.get_db() as conn:
        all_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM ai_forecasts ORDER BY id ASC").fetchall()]
        cited = _cited(conn)
    resolved = [r for r in all_rows if r.get("outcome") is not None]
    # The headline counts each MARKET once even when two agents forecast it;
    # the per-agent rows each count it for themselves.
    overall_rows = _latest_per_market(resolved, per_agent=False, cited=cited)
    return {
        "totalForecasts": len(all_rows),
        "pending": sum(1 for r in all_rows if r.get("outcome") is None),
        "resolved": len(resolved),
        "scoredMarkets": len(overall_rows),
        "minScored": MIN_SCORED,
        "overall": score(overall_rows),
        "buckets": buckets(overall_rows),
        "byAgent": by_agent(all_rows, cited),
        "recent": [_row(r) for r in reversed(all_rows[-recent:])],
    }
