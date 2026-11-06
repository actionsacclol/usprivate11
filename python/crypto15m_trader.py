"""15-minute crypto LIVE executor.

A self-contained execution engine for the 15-min crypto strategy,
DELIBERATELY ISOLATED from the scanner trader (trader.py): it uses its
own `crypto15m_positions` table and never touches `bot_positions`, so it
cannot destabilise the tested whale/momentum engine.

Lifecycle (at most one open position per asset):
  ENTRY   in the last `time_delay` minutes, when one side is a strong
          favorite (>= entry threshold) and the delta filter passes
          (i.e. the monitor's `signal` is true), BUY `order_size`
          contracts of the favorite at ask + entryDiff.
  STOP    if the held side's price falls below `exit_threshold`, SELL to
          flatten — this is the capability the scanner engine lacks.
  SETTLE  otherwise hold to the 15-min settlement and book pnl from the
          binary outcome.

The executor is fully INDEPENDENT of the main bot's enable_trading switch.
It places REAL orders when `crypto15m_enabled` is on AND the Polymarket
wallet is connected. There is no paper mode — if the wallet isn't connected
the engine is a no-op (the Crypto tab still shows live market data).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import clob_ws
import crypto15m
import db
import polymarket_api
import rules as rules_engine
import trader

logger = logging.getLogger("crypto15m")


class _Unset:
    """Sentinel for "caller supplied no market" — distinct from `None`, which
    legitimately means "the market lookup returned nothing"."""
    __slots__ = ()

    def __repr__(self) -> str:      # pragma: no cover - debug aid only
        return "<unset>"


_UNSET = _Unset()

# Last tick's per-asset entry-block reasons — read by status() so the UI can
# answer "why isn't it trading" instead of the reasons dying in a local var.
_block_reasons: dict[str, str] = {}
# Engine-wide halt (daily cap / lifetime breaker) with a user-actionable
# message. Surfaced in status() so the Crypto tab can SHOW why the engine
# stopped — previously the halts were log-only and users found their bot
# "mysteriously" idle with the reason buried in the Logs page.
_halt_reason: str = ""

# position id -> last stop-loss sell attempt (no entry here means it hasn't failed).
# If the sell errors (exchange hiccup, empty book) the row stays 'filled' and the
# 4s manage tick would otherwise re-fire the exit order + log every tick. The
# FIRST attempt is always immediate; only RETRIES are throttled.
_stop_retry_at: dict = {}
_STOP_RETRY_SEC = 90
# Cooldown after an exit sell FAILS, keyed by position id.
#
# Only the stop-loss path was throttled (above, and on every attempt rather
# than on failure). Take-profit and sell-strength had nothing, so a
# deterministic refusal — a sub-minimum size, a stale allowance, a closed
# market — re-submitted a real order on every 4s tick: ~900 attempts an hour
# against the CLOB for an exit that can never succeed. Backing off only after
# a FAILURE leaves a working exit as fast as it ever was.
_exit_fail_at: dict = {}
_EXIT_RETRY_SEC = 60

# ───────── model-calibration drift monitor (the sniper's lifeline) ──────────
# The model edge exists ONLY while ">=97% predicted" keeps delivering ~that
# realized. Nothing else in the system watches that assumption — a regime
# change, a stale spot feed, or a drifted window-open strike would bleed
# quietly at high confidence. Rolling check over the last _CAL_WINDOW resolved
# windows where the model was sniper-confident; if the Wilson lower bound of
# the realized hit rate drops below break-even, model-mode entries auto-pause
# (and resume with hysteresis once calibration recovers).
_CAL_CACHE: dict = {"at": 0.0, "ok": True, "n": 0, "rate": None, "lb": None}
_CAL_CHECK_SEC = 300.0
_CAL_WINDOW = 40          # rolling windows considered
_CAL_MIN_N = 20           # below this, not enough evidence to pause
# Bounds are on the WILSON LOWER BOUND, which at n=40 sits well under the
# observed rate (a PERFECT 40/40 record only reaches LB≈0.937 — a bar above
# that would pause a flawless model forever). Break-even at ~93c entries +
# the fee-v2 taker fee (0.07·p·(1−p) ≈ 0.5c at 93c) is ~93.5% observed;
# 0.85 LB ≈ observed ~95% at n=40, so a record at/under ~90% observed (a real
# money-loser) pauses while a healthy 97%+ record clears with room.
# Resume: 0.90 was itself unreachable (wilson_lb(39,40)=0.8954 — only a
# literally perfect 40/40 cleared it, so one miss anywhere in the trailing
# 40 kept a recovered model paused for days). 0.88 resumes at 39/40
# (observed 97.5%, healthy) while 38/40 (LB 0.8597) stays paused —
# hysteresis preserved: pause at ≤37/40, resume at ≥39/40.
_CAL_PAUSE_LB = 0.85
_CAL_RESUME_LB = 0.88


def _wilson_lb(wins: int, n: int, z: float = 1.645) -> float:
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (center - rad) / denom)


def check_model_calibration(env: str, interval: str = "15m") -> dict:
    now = time.time()
    if now - _CAL_CACHE["at"] < _CAL_CHECK_SEC and _CAL_CACHE.get("interval") == interval:
        return dict(_CAL_CACHE)
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                # Judge the model ONLY on the window series it is trading.
                # Mixed-interval recording (5m days then back to 15m) must not
                # let 5m outcomes drive the 15m sniper's pause/resume — this is
                # the same interval scoping replay.replay applies.
                """SELECT t.ticker, t.model_prob, s.up_won
                   FROM crypto15m_ticks t
                   JOIN crypto15m_signals s
                     ON s.ticker = t.ticker AND s.network = t.network
                   WHERE s.resolved = 1 AND s.up_won IS NOT NULL
                     AND COALESCE(s.interval, '15m') = ?
                     AND t.network = ? AND t.model_prob IS NOT NULL
                     AND COALESCE(t.spot_source, '') != ?
                     AND t.observed_at >= datetime('now', '-3 days')
                     AND t.mins_left <= 5 AND t.mins_left >= 0.5
                     AND (t.model_prob >= 0.97 OR t.model_prob <= 0.03)
                   ORDER BY t.observed_at""",
                # Downloaded history isn't what the model saw live, so it can't
                # pause or resume a live engine.
                (interval, env, db.HISTORY_SOURCE),
            ).fetchall()
    except Exception:
        return dict(_CAL_CACHE)
    # last qualifying tick per window = the prediction the sniper would act on
    last_by_ticker: dict = {}
    for r in rows:
        last_by_ticker[r["ticker"]] = r
    recent = list(last_by_ticker.values())[-_CAL_WINDOW:]
    n = len(recent)
    wins = sum(
        1 for r in recent
        if (float(r["model_prob"]) >= 0.5) == bool(r["up_won"])
    )
    lb = _wilson_lb(wins, n)
    prev_ok = bool(_CAL_CACHE.get("ok", True))
    if n < _CAL_MIN_N:
        ok = True  # not enough evidence to pause on
    elif prev_ok:
        ok = lb >= _CAL_PAUSE_LB
    else:
        ok = lb >= _CAL_RESUME_LB
    if prev_ok and not ok:
        logger.warning(
            f"[crypto15m] MODEL CALIBRATION DEGRADED: {wins}/{n} recent "
            f"sniper-band predictions hit (LB {lb:.3f} < {_CAL_PAUSE_LB}) — "
            f"auto-pausing model-mode entries"
        )
    elif not prev_ok and ok:
        logger.info(f"[crypto15m] model calibration recovered (LB {lb:.3f}) — resuming")
    _CAL_CACHE.update({
        "at": now, "ok": ok, "n": n, "interval": interval,
        "rate": round(wins / n, 4) if n else None, "lb": round(lb, 4),
    })
    return dict(_CAL_CACHE)


# ───────── parlay per-hour schedule (generated; stored in app_kv) ──────────
# The parlay generator writes a schedule (UTC hour -> config override) to the
# durable kv store; arming is a separate kv flag. Kept OUT of the Electron
# config round-trip on purpose: the renderer only persists keys it knows, so
# a backend-generated schedule stored in config could be silently dropped on
# the next Settings save.
_PARLAY_KV_SCHEDULE = "crypto15m_parlay_schedule"
_PARLAY_KV_ARMED = "crypto15m_parlay_armed"
_parlay_cache: dict = {"at": 0.0, "armed": False, "schedule": None}
_PARLAY_REFRESH_SEC = 10.0


def parlay_state(force: bool = False) -> tuple[bool, Optional[dict]]:
    """(armed, schedule) from the durable kv store, cached ~10s. Armed is only
    True when a structurally valid schedule exists. A transient read failure
    returns the last known state rather than disarming mid-session."""
    now = time.time()
    if not force and now - _parlay_cache["at"] < _PARLAY_REFRESH_SEC:
        return _parlay_cache["armed"], _parlay_cache["schedule"]
    try:
        with db.get_db() as conn:
            armed = db.kv_get(conn, _PARLAY_KV_ARMED) == "1"
            raw = db.kv_get(conn, _PARLAY_KV_SCHEDULE)
        sched = json.loads(raw) if raw else None
        # An EMPTY hour_configs is not a valid schedule. It is a normal
        # generator outcome (MIN_WIN_RATE=1.0 seats nothing on a short or noisy
        # corpus), and treating it as valid used to arm a schedule that seats no
        # hours -> hour_override read {} as "no schedule" and returned the BASE
        # config -> every row was stamped strategy="parlay", which exempts it
        # from all four exit paths. Real money with no stop-loss. Require >=1 hour.
        hcs = sched.get("hour_configs") if isinstance(sched, dict) else None
        if not isinstance(sched, dict) or not isinstance(hcs, dict) or not hcs:
            sched = None
        _parlay_cache.update(
            {"at": now, "armed": bool(armed and sched), "schedule": sched}
        )
    except Exception as e:
        logger.debug(f"[crypto15m] parlay state read failed: {e}")
        _parlay_cache["at"] = now
    return _parlay_cache["armed"], _parlay_cache["schedule"]


def parlay_stale_reason(sched: Optional[dict]) -> Optional[str]:
    """Why a stored parlay schedule should be regenerated before it is
    trusted, or None. Shown wherever the schedule is (status, the parlay
    RPCs, the generator CLI).

    A schedule generated before its series moved to 60s-TWAP settlement
    (crypto15m.TWAP_SETTLEMENT_SINCE) was fit to windows that settled on a
    single print, so its floors and windows answer a different question. One
    generated before 2026-10-11 also qualified configs on its own holdout
    (see parlay_generator._qualifies), so its holdout figures were part of
    the selection rather than an out-of-sample check."""
    if not isinstance(sched, dict):
        return None
    interval = str(sched.get("interval") or "15m")
    gen = str(sched.get("generated_at") or "")[:10]
    since = crypto15m.twap_settlement_since(interval)
    if since and (not gen or gen < since):
        return (
            f"Generated {gen or 'at an unknown date'}, before {interval} windows "
            f"moved to 60s-TWAP settlement on {since}: it was fit to a settlement "
            "rule that no longer applies. Regenerate it before arming."
        )
    if not sched.get("holdout_untouched"):
        return (
            "Generated before 2026-10-11, when configs also had to clear the "
            "holdout to be seated, so its holdout figures were used for "
            "selection and are not an out-of-sample check. Regenerate it."
        )
    return None


# The app's own floor on an automated entry, carried over from the international
# venue. It is NOT a Polymarket US rule: markets there state their own minimumTradeQty
# (1 or 0.01), which place_limit_order enforces. Kept as a conservative default until
# small orders have been seen live.
_C15_MIN_CONTRACTS = 5

# asset -> last time the "balance funds only N contracts" skip was logged. An
# underfunded account re-hits that skip on EVERY evaluation tick of EVERY
# window (1,400+ identical lines/day in user logs, and each log line costs a
# frontend render) — say it once per asset per throttle window instead.
_underfunded_log_t: dict = {}
_UNDERFUNDED_LOG_SEC = 300

# The app's floor on notional, also carried over (the international venue rejected
# marketable BUY orders under $1; not established for Polymarket US). 5 contracts at
# 3c is only $0.15 and is skipped even though it clears the 5-contract
# minimum.
_MIN_ORDER_NOTIONAL_USD = 1.00

def _min_notional_ok(contracts: int, limit_cents: int) -> bool:
    return (int(contracts) * int(limit_cents)) / 100.0 >= _MIN_ORDER_NOTIONAL_USD


# ───────── pure decision helpers (unit-tested) ────────────────────────


def direction_for_favorite(favorite: str) -> str:
    """Map the displayed favorite side to the Polymarket contract side."""
    return "yes" if favorite == "up" else "no"


def _is_directional_rules(cfg: dict) -> bool:
    """Directional rule mode is on when custom rules are enabled AND a NO-side
    rule-set exists — then the rules pick the side (yes→up / no→down), the way
    the imported Turbine strategies express entryYes/entryNo."""
    return bool(cfg.get("crypto15m_use_rules")) and bool(cfg.get("crypto15m_rules_no"))


def _directional_rules_side(asset: dict, cfg: dict) -> tuple[Optional[str], str]:
    """Which side the YES/NO rule-sets select for this asset. YES-side rules
    (crypto15m_rules) → 'up'; NO-side rules (crypto15m_rules_no) → 'down'; None
    when neither matches. YES wins if both somehow match (deterministic)."""
    ok_y, _ = evaluate_rules(asset, cfg.get("crypto15m_rules") or [])
    if ok_y:
        return "up", "yes-rules matched"
    ok_n, _ = evaluate_rules(asset, cfg.get("crypto15m_rules_no") or [])
    if ok_n:
        return "down", "no-rules matched"
    return None, "no directional rule matched"


def _entry_side(asset: dict, cfg: dict) -> str:
    """The side we'd BUY for this asset: 'up' or 'down'. Directional rule mode
    lets the YES/NO rule-sets pick the side; otherwise favorite mode buys the
    favorite, contrarian buys the opposite (cheap) side, and model (sniper) mode
    buys whichever side the settlement model calls. Shared by the entry gates
    and the order path so they can't disagree."""
    if _is_directional_rules(cfg):
        return _directional_rules_side(asset, cfg)[0] or ""
    mode = (cfg.get("crypto15m_direction_mode") or "favorite").lower()
    if mode == "model":
        mp = asset.get("modelProb")
        if mp is None:
            return ""
        return "up" if float(mp) >= 0.5 else "down"
    fav = asset.get("favorite") or ""
    if mode == "contrarian":
        return "down" if fav == "up" else "up"
    return fav


def taker_order_type(cfg: dict) -> str:
    """CLOB order type for a 'cross the spread now' action (taker entry +
    maker→taker escalation). FAK (Fill-And-Kill) is marketable: it fills
    immediately against resting liquidity at-or-better than our capped price and
    KILLS the unfilled remainder instead of resting it — so a near-close cross
    either fills now or cleanly no-fills, rather than leaving an orphan GTC order
    that drifts and later mis-cancels (the main cause of 'fills fail'). Defaults
    on; set crypto15m_taker_fak=False to revert to the legacy resting GTC cross."""
    return "FAK" if cfg.get("crypto15m_taker_fak", True) else "GTC"


def entry_limit_cents(entry_cost: float, entry_diff: float) -> int:
    """Favorite ask in [0,1] crossed up by `entry_diff`, clamped 1..99¢."""
    cents = round((float(entry_cost) + float(entry_diff)) * 100)
    return max(1, min(99, int(cents)))


def maker_limit_cents(side: str, yes_bid, yes_ask, entry_cost: float) -> int:
    """Resting (maker) entry price: join the best bid on the side we're
    buying, so the order adds liquidity instead of crossing the spread.

    up/yes  -> yes best bid;  down/no -> no best bid = 1 - yes best ask.
    Falls back to 1¢ under the side's ask when the book is unknown."""
    bid = None
    if side == "up":
        bid = yes_bid
    elif side == "down" and yes_ask:
        bid = 1.0 - float(yes_ask)
    if not bid or bid <= 0:
        bid = max(0.01, float(entry_cost) - 0.01)
    return max(1, min(99, int(round(float(bid) * 100))))


def side_prob_from_market(market: Optional[dict], direction: str) -> Optional[float]:
    """Current probability (0..1) of the side we hold, from a market dict."""
    if not market:
        return None
    yes_bid = crypto15m._price_dollars(market, "yes_bid")
    yes_ask = crypto15m._price_dollars(market, "yes_ask")
    up = (yes_bid + yes_ask) / 2 if (yes_bid or yes_ask) else crypto15m._price_dollars(market, "last_price")
    up = max(0.0, min(1.0, up))
    return up if direction == "yes" else (1.0 - up)


def evaluate_rules(asset: dict, rules: list) -> tuple[bool, str]:
    """Evaluate the crypto entry rule-set against an asset snapshot (fields are
    snapshot keys like favoritePrice / bookImbalance / minsLeft). Thin wrapper
    over the shared engine so crypto and the main bot evaluate rules identically."""
    return rules_engine.evaluate_rules(asset, rules)


# Final-minute sniper gates (model mode only). 0.9985 ≈ a 3-sigma distance
# between the projected close and the strike — the band where the terminal
# model has essentially decided the window while a stale quote lingers.
_FM_MIN_PROB = 0.9985


# ───────── paired both-sides mode (model-tilt ladder) ───────────────────
# The template shared by the profitable crypto up/down bots in the PRR
# trader reports (PBot-6, Bonereaper, Gabigol, …): buy BOTH sides when the
# combined cost is under the cap — the hedge leg floors the loss — and
# express direction as an ASYMMETRIC TILT toward the side the fair-value
# model says is underpriced. Every report shows dominant-side win rate
# climbing monotonically with the tilt ratio (78-96% at 3x+). This works on
# Polymarket because the up/down books are independent (unlike Kalshi's one
# complementary book); the model supplying the tilt is our terminal-spot
# Gaussian — the half we already had.


def _model_input_block(asset: dict, cfg: dict) -> str:
    """Why the settlement model can't be trusted for this asset right now, or
    "" when it can. Shared by every mode that sizes on the model (model and
    paired), so neither trades on an input the other refuses."""
    if cfg.get("crypto15m_model_autopause", True):
        cal = check_model_calibration(trader.get_env(), crypto15m._interval(cfg))
        if not cal.get("ok", True):
            return (
                f"model calibration degraded ({cal.get('rate') or 0:.0%} hit over "
                f"last {cal.get('n', 0)} windows) — auto-paused"
            )
    # The model prices distance to the strike; under TWAP settlement a
    # single boundary print is not the price to beat (recorded ticks
    # predating strike sources carry None and stay replayable).
    ss = asset.get("strikeSource")
    if ss is not None and ss not in crypto15m.STRIKE_SOURCES_OK:
        return "strike is approximate (no TWAP at the window open) — model sits out"
    return ""


def should_enter(asset: dict, cfg: dict, *, has_open: bool, open_count: int) -> tuple[bool, str]:
    if not cfg.get("crypto15m_enabled"):
        return False, "disabled"
    if has_open:
        return False, "already open"
    max_conc = int(cfg.get("crypto15m_max_concurrent", len(crypto15m.SERIES)))
    if open_count >= max_conc:
        return False, "max concurrent"
    if not asset.get("hasMarket"):
        return False, "no market"
    if asset.get("favorite") not in ("up", "down"):
        return False, "no favorite"
    mode = (cfg.get("crypto15m_direction_mode") or "favorite").lower()
    # Don't place a MAKER entry that's already inside its own cancel lead — it
    # would be abandoned before it could fill, and repeated every tick in the
    # final minute it floods the book with orders that cancel immediately.
    # Taker crosses the spread and fills at once, so it's exempt — and model
    # mode always crosses (the snipe NEEDS the fill), so it's exempt too.
    if mode != "model" and (cfg.get("crypto15m_entry_style") or "maker").lower() == "maker":
        cancel_min = float(cfg.get("crypto15m_maker_cancel_min", 0.0) or 0.0)
        mins_left = asset.get("minsLeft")
        if cancel_min > 0 and mins_left is not None and float(mins_left) <= cancel_min:
            return False, "inside maker cancel lead"
    # The trading-hours gate is a HARD time control and always applies — even
    # with custom rules. A user who set "only trade 13:00-21:00 UTC" expects that
    # honored regardless; rules replace only the favorite/signal EDGE, and the
    # rule vocabulary can't express a wrapping overnight window anyway. (Without
    # this, use_rules silently traded 24h.) The snapshot's hourUtc is passed
    # through so REPLAY evaluates each tick's recorded hour, not the wall
    # clock at backtest run time (live snapshots always stamp hourUtc, and
    # hours_ok falls back to now() when it's None).
    if not crypto15m.hours_ok(cfg, hour=asset.get("hourUtc")):
        return False, "outside trading hours"
    # Custom rule-set (rule builder): the user's composed conditions REPLACE the
    # built-in favorite/signal gate. The side bought still comes from
    # direction_mode; the rules decide WHEN to enter (and can gate on minsLeft
    # themselves, so the entry window is theirs to control).
    if cfg.get("crypto15m_use_rules"):
        # User rules replace the built-in gate but NOT the safety rails: a
        # naive rule-set could otherwise buy a 99c contract 30 seconds before
        # close (max-gamma). The final minute stays model-mode-only, and the
        # executable ask must respect the entry cap.
        ml = asset.get("minsLeft")
        if ml is not None and float(ml) < 1.0:
            return False, "final minute (custom rules are blocked here — model mode only)"
        if _is_directional_rules(cfg):
            # YES/NO rule-sets pick the side; enter only when one matches.
            side, why = _directional_rules_side(asset, cfg)
            if side is None:
                return False, why
        else:
            ok, why = evaluate_rules(asset, cfg.get("crypto15m_rules") or [])
            if not ok:
                return ok, why
        rside = _entry_side(asset, cfg)
        rask = asset.get("upAsk") if rside == "up" else (
            asset.get("downAsk") if rside == "down" else None
        )
        if rask and float(rask) > crypto15m._const(cfg, "entry_max"):
            return False, f"ask {float(rask)*100:.0f}c above the entry cap"
        return True, "ok"
    # Settlement sniper (model mode): its own gate, independent of the
    # favorite/signal machinery — enter only when the terminal-spot model
    # calls the outcome near-certain AND the (lagging) quote still leaves
    # fee-adjusted edge. The lag IS the edge: the model reads live spot while
    # the book prices the last snapshot.
    if mode == "model":
        mp = asset.get("modelProb")
        if mp is None:
            return False, "model unavailable (needs indicators + spot feed)"
        mp = float(mp)
        p_side = mp if mp >= 0.5 else 1.0 - mp
        blocked = _model_input_block(asset, cfg)
        if blocked:
            return False, blocked
        ml = asset.get("minsLeft")
        final_minute = ml is not None and 0.0 < float(ml) < 1.0
        if final_minute:
            # Settlement is a 60s TWAP, so inside the final minute most of the
            # average is already observed and the model firms up fast (it
            # prices only the unseen r/60 of it). Final-minute strikes are
            # allowed ONLY on near-3-sigma certainty, a LIVE WebSocket spot
            # for this asset (a cached REST spot can be ~14s stale — its
            # "certainty" is fiction), and enough runway for the order
            # round-trip. Other modes keep the hard final-minute block:
            # blind favorite-buying here is max-gamma.
            if not cfg.get("crypto15m_model_final_minute", True):
                return False, "final minute (disabled)"
            if not asset.get("spotLive"):
                return False, "final minute needs a live WS spot (asset not covered / feed cold)"
            if float(ml) < 0.1:  # < ~6s to close — no order round-trip runway
                return False, "too close to the close for an order round-trip"
            if p_side < _FM_MIN_PROB:
                return False, f"model {p_side:.4f} < {_FM_MIN_PROB} (3-sigma gate)"
        elif not asset.get("inWindow"):
            return False, "outside entry window"
        else:
            min_p = float(cfg.get("crypto15m_model_min_prob", 0.97) or 0.97)
            if p_side < min_p:
                return False, f"model {p_side:.3f} < {min_p:.2f}"
        # The executable ask must exist under the cap BEFORE we score the edge:
        # the edge has to be measured on the side we will actually BUY (the
        # mp≥0.5 side), not the best of both sides. asset["edgeNetCents"] is
        # max(up_edge, down_edge), so gating on it approved windows whose edge
        # lived on the OPPOSITE (cheap) side while we bought the likely-but-
        # negative-EV side — the near-arb corner where combined asks < 100c.
        ask = asset.get("upAsk") if mp >= 0.5 else asset.get("downAsk")
        if not ask or not (0.0 < float(ask) <= crypto15m._const(cfg, "entry_max")):
            return False, "no executable ask under the entry cap"
        ask_c = float(ask) * 100.0
        edge = round(
            p_side * 100.0 - ask_c - crypto15m._fee_cents(ask_c, asset.get("feeSchedule")),
            2,
        )
        min_e = float(cfg.get("crypto15m_model_min_edge_cents", 2.0) or 0.0)
        if edge < min_e:
            return False, f"net edge {edge}c < {min_e:.1f}c (traded side)"
        # Divergence guard (outside the final minute): a LIVE book that
        # disagrees with the model by more than the cap is more likely
        # exposing a bad model input (late strike, broken sigma, venue
        # basis) than offering free money — the 2026-07-03 50c-BTC loss
        # was a 40c "edge" against an actively-updating book. Final-minute
        # strikes get a LOOSER cap instead of a full exemption: books
        # genuinely gap 20-67c there (documented), but a user report
        # (2026-07-16: 1000 BTC-down shares bought at 1c on a ">=95%"
        # model call) showed the old unlimited exemption happily buys a
        # ~98c "edge" — a disagreement so extreme it is almost always a
        # fabricated strike or stale spot, not the lag. The FM cap admits
        # the whole documented gap range and refuses only the far tail.
        gap = float(cfg.get("crypto15m_model_max_book_gap_cents", 25.0) or 0.0)
        if final_minute:
            gap = float(cfg.get("crypto15m_model_fm_max_book_gap_cents", 75.0) or 0.0)
        if gap > 0 and edge > gap:
            return False, (
                f"model disagrees with a live book by {edge:.0f}c "
                f"(> {gap:.0f}c cap{' in the final minute' if final_minute else ''}) "
                f"— distrusting the model input"
            )
        return True, "ok"
    # Hard final-minute block for the favorite/signal path (Kalshi-bot
    # parity — the port dropped it). A 95c favorite 40 seconds before close
    # is a max-gamma bet: it can still settle $0 and the stop-loss provably
    # never fills there (books gap to $0 with no bids). Recorded-data check
    # (2026-07-05, real-book fills only): favorite entries in the final
    # minute are -1.2 to -3.7c/contract. The final minute belongs to model
    # mode's 3-sigma gate.
    _ml = asset.get("minsLeft")
    if _ml is not None and float(_ml) < 1.0:
        return False, "final minute (favorite entries blocked — model mode owns this band)"
    if not asset.get("signal"):
        return False, "no signal"
    # Optional book-imbalance confirmation gate (opt-in; otherwise the signal is
    # record-only). The Up-token order-flow must back the side we'd buy: buying
    # Up wants buy-pressure (imbalance ≥ +min); buying Down wants sell-pressure
    # on Up (imbalance ≤ −min).
    if cfg.get("crypto15m_imbalance_gate"):
        imb = asset.get("bookImbalance")
        if imb is None:
            return False, "imbalance gate: no book data"
        mn = float(cfg.get("crypto15m_imbalance_gate_min", 0.2) or 0.0)
        direction = direction_for_favorite(_entry_side(asset, cfg))
        if direction == "yes" and imb < mn:
            return False, f"imbalance {imb:+.2f} < +{mn:.2f}"
        if direction == "no" and imb > -mn:
            return False, f"imbalance {imb:+.2f} > -{mn:.2f}"
    # Optional DIRECTION-AWARE momentum confirmation on the UNDERLYING (RSI /
    # MACD-histogram from the 1-min Hyperliquid candles). Like the imbalance gate
    # above, the momentum must AGREE with the side we're buying — an Up bet needs
    # bullish momentum, a Down bet bearish: rsi >= min / macdHist >= min for Up,
    # and the mirror (rsi <= 100-min / macdHist <= -min) for Down. Each 0 = off.
    # macdHist (the signal-line histogram) is used, not the raw MACD line: its
    # SIGN is scale-free, so a floor of e.g. 0.5 behaves sanely across BTC and
    # DOGE alike. Fail-closed: a gate that's on but whose indicator isn't
    # populated this tick (detector off / too few candles) blocks the entry,
    # same posture as the imbalance gate.
    direction = direction_for_favorite(_entry_side(asset, cfg))
    min_rsi = float(cfg.get("crypto15m_min_rsi", 0) or 0.0)
    if min_rsi > 0:
        rsi_v = asset.get("rsi")
        if rsi_v is None:
            return False, "RSI gate: no indicator data"
        rsi_v = float(rsi_v)
        if direction == "yes" and rsi_v < min_rsi:
            return False, f"RSI {rsi_v:.0f} < {min_rsi:.0f}"
        if direction == "no" and rsi_v > (100.0 - min_rsi):
            return False, f"RSI {rsi_v:.0f} > {100.0 - min_rsi:.0f}"
    min_macd = float(cfg.get("crypto15m_min_macd_hist", 0) or 0.0)
    if min_macd > 0:
        mh = asset.get("macdHist")
        if mh is None:
            return False, "MACD gate: no indicator data"
        mh = float(mh)
        if direction == "yes" and mh < min_macd:
            return False, f"MACD hist {mh:+.4f} < +{min_macd:.4f}"
        if direction == "no" and mh > -min_macd:
            return False, f"MACD hist {mh:+.4f} > -{min_macd:.4f}"
    return True, "ok"


def should_stop_loss(position: dict, side_prob: Optional[float], cfg: dict) -> bool:
    """A filled position trips the stop-loss when the side we hold falls
    below `exit_threshold`. Only `filled` rows are eligible (not ones
    already submitting/exiting/exited)."""
    if side_prob is None:
        return False
    # Parlay entries are hold-to-settlement BY CONTRACT: the generator scored
    # every config with no exits (the outcome is then deterministic — win iff
    # the favorite settles correct), so the user's base stop/TP firing on them
    # live would trade something the schedule was never validated as.
    if (position.get("strategy") or "") == "parlay":
        return False
    if position.get("status") != "filled":
        return False
    if int(position.get("filled_contracts") or 0) <= 0:
        return False
    # User-script positions: the script's contract is hold-to-settlement
    # unless ITS intent set an exit — the engine-wide exit knobs must not
    # fire on them. Only the row-level sl_cents (set by the intent) applies.
    if position.get("script_id"):
        slc = position.get("sl_cents")
        return bool(slc) and (side_prob * 100.0) <= float(slc)
    # Price/probability stop: the held side has fallen to the exit threshold.
    if side_prob < crypto15m._const(cfg, "exit_threshold"):
        return True
    # Percent-of-entry stop: down >= X% from entry cost. 0 = off. Global across
    # timeframes (5m/15m/hourly). A binary contract's current mark ~ side_prob
    # dollars, so current value is filled*side_prob vs the cost_usd we paid.
    slp = _clamp01(crypto15m._const(cfg, "stop_loss_pct"))
    if slp > 0:
        filled = int(position.get("filled_contracts") or 0)
        cost = float(position.get("cost_usd") or 0.0)
        if cost > 0 and filled > 0:
            cur_value = filled * float(side_prob)
            if (cost - cur_value) / cost >= slp:
                return True
    return False


def strength_exit_cents(position: dict, cfg: dict) -> Optional[int]:
    """Sell-into-strength: the price of the resting GTC ask to park over a
    filled position, or None when the feature is off / the position isn't
    eligible. The SirMartingale pattern from the PRR reports (61% of its
    alpha was exits): sell winners INTO the pump at ~80c instead of riding
    to settlement — a resting ask catches spikes between engine ticks, pays
    no taker fee (maker), sidesteps the stop-loss-never-fills problem, and
    returns cash pre-settlement instead of stranding winnings unredeemed.
    Only positions whose entry is comfortably BELOW the target qualify —
    a 95c sniper entry has nothing to sell into at 80c."""
    if not cfg.get("crypto15m_sell_into_strength"):
        return None
    if position.get("script_id"):
        return None  # script positions exit only via their own intent's tp/sl
    if (position.get("strategy") or "") == "parlay":
        return None  # parlay rows hold to settlement (see should_stop_loss)
    if position.get("status") != "filled":
        return None
    if int(position.get("filled_contracts") or 0) <= 0:
        return None
    try:
        tgt = float(cfg.get("crypto15m_sell_strength_cents", 80.0) or 80.0)
    except (TypeError, ValueError):
        return None
    tgt = int(round(max(50.0, min(99.0, tgt))))
    entry_c = float(position.get("avg_entry_cents") or 0.0)
    if entry_c <= 0 or tgt < entry_c + 2.0:
        return None  # no real strength to sell into from this entry
    return tgt


def should_take_profit(position: dict, bid_cents: Optional[int], cfg: dict) -> bool:
    """A filled position hits the per-bet take-profit when the held side's best
    BID reaches the configured cash-out price — i.e. we can sell the whole thing
    at/above that price right now. Triggers on the REAL book bid (what we can
    actually realize), not a lagged mid, so a take-profit only ever fires when
    the money is genuinely there. 0 (the default) is disabled. Only `filled`
    rows are eligible (not ones already submitting / exiting / exited)."""
    if position.get("script_id"):
        return False  # scripts express take-profit via tp_pct only
    if (position.get("strategy") or "") == "parlay":
        return False  # parlay rows hold to settlement (see should_stop_loss)
    target = crypto15m._const(cfg, "take_profit")
    if target <= 0 or bid_cents is None:
        return False
    if position.get("status") != "filled":
        return False
    if int(position.get("filled_contracts") or 0) <= 0:
        return False
    return (bid_cents / 100.0) >= target


def should_take_profit_pct(position: dict, bid_cents: Optional[int], cfg: dict) -> bool:
    """A filled position hits the percent take-profit when the whole thing is
    worth >= X% MORE than it cost at the current best BID. The win-side mirror of
    should_stop_loss's percent-of-entry rule: uses the real book bid (what we can
    actually realize), not a lagged mark, so it only fires when the profit is
    genuinely there. 0 (the default) is disabled. Only `filled` rows are
    eligible (not ones already submitting / exiting / exited)."""
    if (position.get("strategy") or "") == "parlay":
        return False  # parlay rows hold to settlement (see should_stop_loss)
    if position.get("script_id"):
        # Row-level override set by the script's intent; engine knob ignored.
        try:
            tpp = max(0.0, float(position.get("tp_pct") or 0.0))
        except (TypeError, ValueError):
            tpp = 0.0
    else:
        tpp = _clamp01(crypto15m._const(cfg, "take_profit_pct"))
    if tpp <= 0 or bid_cents is None:
        return False
    if position.get("status") != "filled":
        return False
    filled = int(position.get("filled_contracts") or 0)
    cost = float(position.get("cost_usd") or 0.0)
    if filled <= 0 or cost <= 0:
        return False
    cur_value = filled * (bid_cents / 100.0)
    return (cur_value - cost) / cost >= tpp


def _clamp01(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return max(0.0, min(1.0, f))


# Backstop fraction-of-balance cap for a single 'fixed'-mode entry when the user
# hasn't set an explicit crypto15m_max_loss_pct. Prevents one bet from consuming
# most of the account; well above any normal out-of-box order size.
_FIXED_SIZE_MAX_BALANCE_FRAC = 0.25

# How many settled rows the streak reader scans. The multiplier is capped at
# streak_max_mult (default 4x = 8 losses at +20%), so anything deeper than
# this can't change the result.
_STREAK_LOOKBACK_ROWS = 40


def streak_multiplier(cfg: dict, env: str) -> float:
    """Win/loss-conditioned size multiplier ("streak sizing"), 1.0 when off.

    Reads the current run of same-outcome settled positions on this network
    (most recent first; a scratch — pnl exactly 0 — ends the run) and scales:
    loss streak -> (1 + streak_loss_pct/100) ** n, win streak ->
    (1 + streak_win_pct/100) ** n, clamped to [0.1, streak_max_mult].

    Recomputed from the DB at every entry rather than kept in memory, so it
    survives restarts and never drifts from what actually settled. This
    reshapes variance only — recorded outcomes are serially uncorrelated —
    and the caller's risk caps still bound whatever this returns."""
    if not cfg.get("crypto15m_streak_sizing"):
        return 1.0
    loss_pct = float(cfg.get("crypto15m_streak_loss_pct", 20.0) or 0.0)
    win_pct = float(cfg.get("crypto15m_streak_win_pct", 0.0) or 0.0)
    cap = max(1.0, float(cfg.get("crypto15m_streak_max_mult", 4.0) or 4.0))
    try:
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT pnl_usd FROM crypto15m_positions "
                "WHERE resolved=1 AND network=? AND pnl_usd IS NOT NULL "
                "AND dry_run=0 "
                "ORDER BY resolved_at DESC, id DESC LIMIT ?",
                (env, _STREAK_LOOKBACK_ROWS),
            ).fetchall()
    except Exception as e:  # sizing must never block an entry
        logger.warning(f"[crypto15m] streak read failed, sizing flat: {e}")
        return 1.0
    streak = 0
    won: Optional[bool] = None
    for r in rows:
        pnl = float(r[0])
        if pnl == 0.0:
            break  # a scratch ends the run
        w = pnl > 0
        if won is None:
            won = w
        elif w != won:
            break
        streak += 1
    if streak == 0 or won is None:
        return 1.0
    step = win_pct if won else loss_pct
    mult = (1.0 + step / 100.0) ** streak
    return max(0.1, min(cap, mult))


def compute_entry_contracts(
    cfg: dict, *, entry_limit_cents: int, balance_usd: float, order_size: int,
    balance_known: bool = False, streak_mult: float = 1.0,
) -> int:
    """Contracts to buy for one entry.

    fixed mode      -> `order_size` contracts.
    balance_pct mode-> spend `crypto15m_balance_pct` of balance at the
                       entry price (floored to whole contracts).
    `streak_mult` (streak sizing, see streak_multiplier) scales the base
    count BEFORE the risk caps below, so a martingale ramp can never bypass
    them — a scaled bet still can't exceed max_loss_pct or the balance.
    Either way, if `crypto15m_max_loss_pct` > 0 the capital at risk
    (contracts x price — the most a buy-only binary can lose) is held to
    that fraction of balance. Returns 0 when the risk budget can't fund a
    single contract, so the caller skips the entry rather than overspend.

    A balance of 0 is ambiguous: a failed read served 0 long before a real
    account ever could. `balance_known=True` (the last balance read actually
    succeeded) makes 0 authoritative — return 0 so the caller SKIPS instead
    of sizing the full fixed order against money that isn't there (a genuinely
    empty account otherwise spammed ~28 rejected 'not enough balance' orders
    an hour until funds arrived). Unknown-balance keeps the historical
    fall-back to the fixed `order_size`."""
    price = max(0.01, int(entry_limit_cents) / 100.0)  # $/contract
    bal = max(0.0, float(balance_usd or 0.0))
    if balance_known and bal <= 0.0:
        return 0
    mode = (cfg.get("crypto15m_sizing_mode") or "fixed").lower()

    if mode == "balance_pct" and bal > 0:
        pct = _clamp01(cfg.get("crypto15m_balance_pct", 0.02))
        contracts = int((bal * pct) // price)
    else:
        contracts = max(1, int(order_size))  # fixed, or %-mode without a balance

    # Streak sizing scales the BASE count; a shrunken multiplier never floors
    # a fundable fixed-mode bet below 1 contract (a 0 here means "skip", which
    # is reserved for the risk caps below deciding the bet can't be funded).
    if streak_mult != 1.0 and contracts > 0:
        contracts = max(1, int(contracts * streak_mult))

    max_loss = _clamp01(cfg.get("crypto15m_max_loss_pct", 0.0))
    if max_loss > 0 and bal > 0:
        contracts = min(contracts, int((bal * max_loss) // price))
    elif mode != "balance_pct" and bal > 0:
        # Fixed sizing with NO explicit per-bet cap (crypto15m_max_loss_pct=0):
        # still never let a SINGLE entry risk more than a sane fraction of the
        # account. Without this backstop a large order_size on a small balance
        # could put up to the 98% affordability cap on one bet. balance_pct mode
        # is already fraction-bounded by design, so it's exempt. For normal
        # out-of-box sizes (a few contracts vs a healthy balance) 25% is far
        # above the bet, so default behavior is unchanged.
        contracts = min(contracts, int((bal * _FIXED_SIZE_MAX_BALANCE_FRAC) // price))

    # Hard affordability cap. A buy-only binary locks contracts × price as
    # collateral, so never size past what the balance can actually fund (less a
    # small buffer for rounding/fees). Without this, fixed mode happily orders
    # the full size on a balance that can't cover it and EVERY placement is
    # rejected for insufficient balance. Returns 0 → the caller skips the entry
    # rather than overspend.
    if bal > 0:
        contracts = min(contracts, int((bal * 0.98) // price))

    return max(0, contracts)


async def _bankroll_usd(cfg: dict, authed: bool) -> float:
    """Balance to size against. Uses live cash when connected (cached by
    `refresh_balance`); a CONFIRMED zero is returned as a real 0.0 (sizing
    must skip, not spend the pinned bankroll that isn't there). Only an
    unknown balance (read failed) falls back to `start_bankroll_usd`."""
    if authed:
        try:
            cents, _port = await trader.refresh_balance(cfg, force=False)
            if cents > 0:
                return cents / 100.0
            if trader.last_balance_read_ok():
                return 0.0
        except Exception:
            pass
    return max(0.0, float(cfg.get("start_bankroll_usd", 0.0) or 0.0))


# ───────── serialization for the UI ───────────────────────────────────


def _iso(s):
    if not s or not isinstance(s, str):
        return s
    s = s.strip()
    if not s:
        return s
    if s.endswith("Z") or "+" in s[10:]:
        return s.replace(" ", "T")
    return s.replace(" ", "T") + "Z"


def _pos_to_js(r: dict) -> dict:
    def _f(k):
        return float(r[k]) if r.get(k) is not None else None
    # A held position whose 15-min window has CLOSED but isn't booked yet is
    # "settling", not open — Polymarket's UI resolves it on-chain immediately
    # while the public data/Gamma APIs we read lag by up to a couple minutes.
    # Surfacing this lets the UI say "Resolving…" instead of misleading "open".
    close_ep = crypto15m._parse_close_epoch(r.get("close_time") or "")
    settling = bool(
        r.get("status") == "filled" and not r.get("resolved")
        and close_ep is not None
        and datetime.now(timezone.utc).timestamp() >= close_ep
    )
    return {
        "id": int(r["id"]),
        "asset": r["asset"], "series": r.get("series") or "",
        "ticker": r.get("ticker") or "",
        "side": r.get("side") or "", "direction": r.get("direction") or "",
        "targetContracts": int(r.get("target_contracts") or 0),
        "filledContracts": int(r.get("filled_contracts") or 0),
        "entryLimitCents": int(r.get("entry_limit_cents") or 0),
        "avgEntryCents": _f("avg_entry_cents"),
        "costUsd": float(r.get("cost_usd") or 0.0),
        "status": r.get("status") or "",
        "exitReason": r.get("exit_reason"),
        "exitLimitCents": int(r["exit_limit_cents"]) if r.get("exit_limit_cents") is not None else None,
        "proceedsUsd": _f("proceeds_usd"),
        "confidence": float(r.get("confidence") or 0.0),
        "entryDeltaUsd": _f("entry_delta_usd"),
        "outcomeCorrect": int(r["outcome_correct"]) if r.get("outcome_correct") is not None else None,
        "settlementUsd": _f("settlement_usd"),
        "pnlUsd": _f("pnl_usd"),
        "resolved": bool(r.get("resolved") or 0),
        "settling": settling,
        "dryRun": bool(r.get("dry_run") or 0),
        "closeTime": _iso(r.get("close_time")) or "",
        "network": r.get("network") or "mainnet",
        "createdAt": _iso(r.get("created_at")) or "",
        "resolvedAt": _iso(r.get("resolved_at")),
        "error": r.get("error"),
        "strategy": r.get("strategy") or "",
        "feesUsd": float(r.get("fees_usd") or 0.0) + float(r.get("exit_fees_usd") or 0.0),
    }


# ───────── entry ──────────────────────────────────────────────────────


async def _open_entry(a: dict, cfg: dict, env: str, balance_usd: float) -> Optional[dict]:
    """One single-leg entry, serialised against the main / copy / script engines
    by `trader.order_gate`. run_tick snapshots `budget_usd` (the wallet-wide
    exposure + reserve headroom) once per tick, so the locked body also
    re-reads that headroom from the DB right before placing."""
    async with trader.order_gate("crypto15m"):
        return await _open_entry_locked(a, cfg, env, balance_usd)


async def _open_entry_locked(
    a: dict, cfg: dict, env: str, balance_usd: float
) -> Optional[dict]:
    mode = (cfg.get("crypto15m_direction_mode") or "favorite").lower()
    favorite = a.get("favorite")
    fav_price = float(a.get("favoritePrice") or 0.0)
    side = _entry_side(a, cfg)
    if mode == "model":
        mp = a.get("modelProb")
        # NOTE: modelProb == 0.0 is a REAL probability (certain down) — a
        # truthiness check (`not mp`) would silently drop the strongest down
        # snipes. Only None means "model unavailable".
        if mp is None or side not in ("up", "down"):
            return None
        side_ask = a.get("upAsk") if side == "up" else a.get("downAsk")
        entry_cost = float(side_ask or a.get("entryCost") or fav_price or 0.5)
        conf = (float(mp) if side == "up" else 1.0 - float(mp)) * 100.0
    elif mode == "contrarian":
        # Fade the extreme favorite: buy the CHEAP opposite (underdog) side,
        # betting the 15-min move reverts before close. We approximate the
        # underdog price as 1 - favorite mid (we don't have its ask).
        entry_cost = max(0.01, 1.0 - fav_price)
        conf = entry_cost * 100.0
    else:
        entry_cost = float(a.get("entryCost") or fav_price)
        conf = fav_price * 100.0
    direction = direction_for_favorite(side)
    # Model mode ALWAYS crosses: the snipe exists because the quote is stale —
    # a maker joining that stale book would rest unfilled while it corrects.
    style = "taker" if mode == "model" else (cfg.get("crypto15m_entry_style") or "maker").lower()
    ticker = a.get("ticker")
    coid = f"krypt-c15-{a['asset']}-{uuid.uuid4().hex[:8]}"
    if style == "maker":
        limit_cents = maker_limit_cents(side, a.get("yesBid"), a.get("yesAsk"), entry_cost)
    else:
        limit_cents = entry_limit_cents(entry_cost, crypto15m._const(cfg, "entry_diff"))

    # entry_max is the HARD price cap — "skip already-decided favorites priced
    # above this". The snapshot that got us here is a lagging Gamma mid, so a
    # favorite that was under the cap at snapshot time may have run past it by
    # now. Two complementary gates enforce the cap; a skip records a 0-contract
    # `canceled` row, which also marks the ticker attempted so we don't
    # re-quote/re-skip it every tick for the rest of the window.
    max_cents = int(round(crypto15m._const(cfg, "entry_max") * 100))

    def _skip_entry(reason: str, exit_reason: str = "above_cap") -> None:
        logger.info(f"[crypto15m] skip {a.get('asset')}: {reason}")
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, {
                "asset": a["asset"], "series": a["series"], "ticker": ticker,
                "side": side, "direction": direction, "target_contracts": 0,
                "entry_limit_cents": limit_cents, "client_order_id": coid,
                "close_time": a.get("closeTime") or "", "confidence": conf,
                "network": env, "status": "canceled", "dry_run": 0,
                "error": reason,
            })
            _mark_resolved(conn, pid, status="canceled", exit_reason=exit_reason)

    # Re-price off the REAL CLOB top-of-book (the snapshot is a lagging mid).
    real_bid = real_ask = None
    try:
        rq = await polymarket_api.get_quote(ticker, direction)
        real_bid, real_ask = rq.get("bid_cents"), rq.get("ask_cents")
    except Exception as e:
        logger.debug(f"[crypto15m] entry quote {a.get('asset')}: {e}")
    # Gate 1 — already-decided favorite: its real ASK is past the cap, so
    # don't even rest a maker that could only fill on an adverse reversal.
    if real_ask and real_ask > max_cents:
        _skip_entry(
            f"real ask {real_ask}c > entry_max {max_cents}c "
            f"(already-decided; snapshot showed {limit_cents}c)"
        )
        return None
    # Gate 1b — FAVORITE FLOOR: the side must still be priced like a favorite at
    # the LIVE book. The snapshot that picked this side lags, and in a tight
    # late-window entry the favorite can CRASH before our maker fills — without
    # this, the re-price below chases the now-underdog's bid down to a few cents
    # (the "entered at 5c, lost" bug). Use the live mid as the probability.
    # Contrarian mode INTENDS to buy the cheap side, so it's exempt — and so
    # is model mode: buying certainty the lagging book still prices under the
    # threshold IS the snipe (the floor would kill exactly the best entries).
    # Directional rule-sets are exempt too: the rules chose the side (often a
    # ~50c momentum entry at window open), so "priced like a favorite" is the
    # wrong test — the floor canceled every cheap-side rules entry as
    # "favorite flipped" (live 2026-07-16). Their price safety is Gate 1/2's
    # entry_max cap, which still applies.
    if mode not in ("contrarian", "model") and not _is_directional_rules(cfg):
        thr_cents = int(round(crypto15m._const(cfg, "entry_threshold") * 100))
        if real_bid is not None and real_ask is not None:
            live_fav = (real_bid + real_ask) // 2
        else:
            live_fav = real_ask if real_ask is not None else real_bid
        if live_fav is not None and live_fav < thr_cents:
            _skip_entry(
                f"{side} now ~{live_fav}c < entry_threshold {thr_cents}c "
                f"(favorite flipped/crashed since snapshot)",
                exit_reason="favorite_flipped",
            )
            return None
    # Reset the resting price to the live book: maker joins the real bid,
    # taker crosses THROUGH the real ask (cap-bounded by Gate 2 below).
    if style == "maker" and real_bid and 1 <= real_bid <= 99:
        limit_cents = real_bid
    elif style != "maker" and real_ask and 1 <= real_ask <= 99:
        # A bare-ask limit barely crosses — if the ask ticks up even 1c between
        # this quote and the order landing, it rests instead of filling, and the
        # detection lag on these fast markets then mis-marks the rest-then-fill
        # as 'canceled'. Cross WITH the configured entry_diff markup so the order
        # sweeps the ask and actually fills (this is what entry_diff is for in
        # taker style). Still hard-capped at entry_max by Gate 2 below, so we
        # never pay above the cap.
        markup = max(0, int(round(crypto15m._const(cfg, "entry_diff") * 100)))
        limit_cents = min(max_cents, real_ask + markup)

    # Gate 2 — authoritative cap on the ACTUAL submit price. This is what the
    # old ask-only gate leaked through and let fills land above entry_max:
    #   • maker re-priced to a real bid above the cap (the 97c HYPE fill)
    #   • quote returned no ask, so Gate 1 couldn't fire and limit_cents stayed
    #     at a stale above-cap snapshot price the exchange then rejects (the
    #     wall of 0/15 `error` rows)
    #   • a taker markup that pushes the limit over the cap
    if limit_cents > max_cents:
        _skip_entry(
            f"entry {limit_cents}c > entry_max {max_cents}c"
            + ("" if real_ask else " (no live ask; snapshot price)")
        )
        return None

    # Gate 3 — model mode: the EDGE must survive the re-price, not just the
    # cap. should_enter scored it on the snapshot ask, which can be seconds
    # old, and the snipe exists only because the book is about to correct:
    # by the re-quote it may have, and the taker markup then pays up to
    # entry_diff on top (audit probe: a 0.975 call gated on a 90c snapshot
    # sent a 98c FAK into a 96c book, -0.64c of edge at the limit against a
    # 2c floor). A FAK fills at or under its limit, so the limit is capped at
    # the highest price that still clears the min net edge; when even the
    # live ask is past that, the edge is gone. This is should_enter's own
    # refusal on a fresher price, so like it the skip doesn't spend the
    # window's one attempt: the model firms up toward the close and the edge
    # can come back.
    if mode == "model":
        min_e = float(cfg.get("crypto15m_model_min_edge_cents", 2.0) or 0.0)
        fs = a.get("feeSchedule")

        def _edge_at(c: int) -> float:
            return round(conf - c - crypto15m._fee_cents(c, fs), 2)

        edge_cap = limit_cents
        while edge_cap >= 1 and _edge_at(edge_cap) < min_e:
            edge_cap -= 1
        ask_now = real_ask if real_ask else int(math.ceil(round(entry_cost * 100.0, 6)))
        if edge_cap < max(1, ask_now):
            why = (f"edge gone at the live ask: {_edge_at(ask_now)}c at {ask_now}c "
                   f"< {min_e:.1f}c (snapshot ask {entry_cost * 100:.0f}c)")
            logger.info(f"[crypto15m] skip {a.get('asset')}: {why}")
            _block_reasons[a.get("asset") or "?"] = why
            return None
        limit_cents = edge_cap

    streak_mult = streak_multiplier(cfg, env)
    if streak_mult != 1.0:
        logger.info(
            f"[crypto15m] streak sizing x{streak_mult:.2f} on {a.get('asset')} "
            f"(from settled win/loss run)"
        )
    order_size = compute_entry_contracts(
        cfg,
        entry_limit_cents=limit_cents,
        balance_usd=balance_usd,
        order_size=max(1, int(cfg.get("crypto15m_order_size", 1))),
        balance_known=trader.last_balance_read_ok(),
        streak_mult=streak_mult,
    )
    if order_size < 1:
        logger.info(
            f"[crypto15m] skip {a.get('asset')}: sizing yielded 0 contracts "
            f"(risk budget too small at {limit_cents}c)"
        )
        return None
    # Skip CLEANLY when the affordable count can't fund the app's floor. Only relevant when we actually sized against a
    # balance (balance_usd > 0) — i.e. the affordability clamp is what shrank it.
    if balance_usd > 0 and order_size < _C15_MIN_CONTRACTS:
        _asset = str(a.get("asset"))
        if time.time() - _underfunded_log_t.get(_asset, 0.0) >= _UNDERFUNDED_LOG_SEC:
            _underfunded_log_t[_asset] = time.time()
            logger.info(
                f"[crypto15m] skip {a.get('asset')}: balance funds only {order_size} "
                f"contract(s) at {limit_cents}c, below the {_C15_MIN_CONTRACTS}-contract "
                f"floor (add funds or lower other risk caps)"
            )
        return None
    if balance_usd > 0 and not _min_notional_ok(order_size, limit_cents):
        # Opt-in: size UP to the venue's $1 minimum instead of skipping. Cheap
        # asks are the momentum engine's best entries (2026-07-17: three 13-17c
        # signals skipped inside 10 minutes while an 18c fill paid +96c/ct),
        # and the added contracts keep dollar risk ~= $1 — LESS than a normal
        # mid-price entry, never more than the configured size costs at 80c.
        if cfg.get("crypto15m_autosize_to_min_notional") and limit_cents > 0:
            bumped = int(math.ceil(100.0 / float(limit_cents)))
            if _min_notional_ok(bumped, limit_cents):
                logger.info(
                    f"[crypto15m] {a.get('asset')}: sized up {order_size}->{bumped} "
                    f"contracts at {limit_cents}c to clear the $1 order minimum"
                )
                order_size = bumped
        if not _min_notional_ok(order_size, limit_cents):
            logger.info(
                f"[crypto15m] skip {a.get('asset')}: {order_size}x{limit_cents}c is under "
                f"the exchange's $1 marketable-order minimum"
            )
            return None
    # Hard order-time guard for model mode: an order landing after the close
    # tick buys a settled market. 10s covers the POST round-trip + matching.
    if mode == "model":
        ce = crypto15m._parse_close_epoch(a.get("closeTime") or "")
        if ce is not None and (ce - datetime.now(timezone.utc).timestamp()) < 10.0:
            _skip_entry("under 10s to close — no order round-trip runway",
                        exit_reason="too_late")
            return None

    # Stamp WHICH strategy opened this row so per-strategy P&L is computable
    # (otherwise sniper results are indistinguishable from favorite-follow).
    ml = a.get("minsLeft")
    strategy = "rules" if cfg.get("crypto15m_use_rules") else mode
    if mode == "model" and ml is not None and float(ml) < 1.0:
        strategy = "model_fm"  # final-minute strike — its own risk profile
    if cfg.get("_parlay"):
        strategy = "parlay"  # per-hour schedule entry — separable P&L + exits
    # Maker rests (must be GTC); taker crosses now → FAK so it fills immediately
    # or kills cleanly instead of resting an orphan that drifts and mis-cancels.
    # Stamped on the row because it decides the FEE: a resting GTC that fills
    # is a maker fill (no fee under takerOnly fee-v2), a FAK/FOK pays taker.
    entry_ot = "GTC" if style == "maker" else taker_order_type(cfg)
    # …but a GTC is only a MAKER if it actually rests. The re-price above joins
    # the live bid ONLY when the re-quote returned one; when it didn't, the
    # maker keeps the (lagging) snapshot price, which can sit at or above the
    # live ask and cross the instant it lands. That fill is a taker fill, and
    # stamping it 'GTC' made every downstream fee computation
    # (_entry_is_maker) book it free. Submit GTC either way — resting the
    # remainder is still what we want — but record what the fill really is.
    # The stamp is therefore the FEE fact ("did this cross?"), not a copy of
    # the submitted order type — they differ here, and again when
    # crypto15m_taker_fak is off and the taker path itself submits a GTC.
    entry_crosses = (style != "maker") or (
        real_ask is not None and 1 <= int(real_ask) <= 99
        and limit_cents >= int(real_ask))
    entry_ot_row = "FAK" if entry_crosses else "GTC"
    # Final wallet-wide cap re-check inside the order gate (see _open_entry's
    # docstring). No-ops when the balance is unknown/zero — the whole engine
    # already treats that as "don't clamp".
    _cap_why = trader.headroom_blocks(
        cfg, env, balance_usd, order_size * limit_cents / 100.0
    )
    if _cap_why:
        # Log and retry next tick rather than recording an attempt: the cap is
        # transient (another engine's resting order), not a property of this
        # market, so it must not burn the one shot this window gets.
        logger.info(f"[crypto15m] skip {a.get('asset')}: {_cap_why}")
        _block_reasons[a.get("asset") or "?"] = _cap_why
        return None
    row = {
        "asset": a["asset"], "series": a["series"], "ticker": ticker,
        "side": side, "direction": direction,
        "target_contracts": order_size, "entry_limit_cents": limit_cents,
        "client_order_id": coid, "close_time": a.get("closeTime") or "",
        "confidence": conf, "strategy": strategy,
        "entry_delta_usd": a.get("deltaUsd"), "network": env,
        "entry_order_type": entry_ot_row,
    }
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=direction, action="buy",
            count=order_size, price_cents=limit_cents, client_order_id=coid,
            order_type=entry_ot,
        )
    except polymarket_api.OrderStatusUnknownError as e:
        # Sent, outcome unknown: keep its id on a submitted row. The entry
        # poll reads the order, and on a 404 books only what the wallet holds.
        logger.warning(f"[crypto15m] entry {a['asset']}: {e}")
        resp = e.response
    except Exception as e:
        # A failed entry holds nothing — mark it resolved IMMEDIATELY so it
        # can't occupy an executor slot forever (live bug: demo's
        # "exchange is paused" 409s left error rows pinning max_concurrent).
        # Re-entry on the same market is blocked via crypto15m_errored_tickers.
        row.update({"status": "error", "error": str(e)[:200], "dry_run": False})
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, row)
            _mark_resolved(conn, pid, status="error")
            logger.error(f"[crypto15m] entry order failed {a['asset']}: {e}")
            return db.fetch_crypto15m_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    oid = order.get("order_id") if isinstance(order, dict) else None
    ostatus = (order.get("status") if isinstance(order, dict) else "") or ""
    if not oid and ostatus.lower() in ("unmatched", "killed"):
        # A marketable FAK crossed NOTHING (no resting liquidity at/under our
        # price) — a clean zero-fill, the expected outcome on a thin/late book,
        # not a failure. Record a quiet 0-contract skip (no error) so the Crypto
        # tab doesn't show a scary 0/5 'error' row. The ticker is marked attempted
        # for the window (one entry attempt per market, by design). To fill these,
        # widen the entry window so the book is deeper when we cross.
        _skip_entry("FAK crossed no liquidity (0 fill)", exit_reason="no_liquidity")
        return None
    if not oid:
        # No order id back from the exchange — we can't poll, escalate, or settle
        # this row, so it would sit 'submitted' forever and pin a slot. Mark it
        # error/resolved (re-entry is blocked for the window via attempted_tickers).
        row.update({"status": "error", "error": "no order_id from exchange", "dry_run": False})
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, row)
            _mark_resolved(conn, pid, status="error")
            logger.error(f"[crypto15m] entry {a['asset']}: no order_id returned; marked error")
            return db.fetch_crypto15m_by_id(conn, pid)
    row.update({"status": "submitted", "order_id": oid, "dry_run": False})
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, row)
        logger.info(f"[live] entry {a['asset']} {direction} x{order_size} @ {limit_cents}c")
        return db.fetch_crypto15m_by_id(conn, pid)


# ───────── position management ────────────────────────────────────────


# V2 orders no longer stamp fee_rate_bps (the struct dropped the field, fees
# are set at match time) — but the protocol still CHARGES the crypto taker fee.
# Fall back to the crypto schedule rather than recording 0: 2026-07-16 audit
# found fees_usd=0 on 372/372 settled fills, overstating lifetime crypto15m
# P&L by ~0.97c/contract (sign-flipping it).
_V2_CRYPTO_FEE_BPS = 695.0  # the venue's 0.0695 taker coefficient


def _order_fee_usd(
    order: dict, filled: int, price_cents: float | None, *, maker: bool = False,
) -> float:
    """Taker fee for this order's filled portion: rate × p·(1−p) × size.
    Uses the order's own fee_rate_bps when the venue reports one (legacy V1
    orders did); V2 orders report none, so the crypto fee-v2 schedule applies
    (this engine only trades the crypto up/down series).

    `maker=True` = the fill matched from a RESTING order (a GTC entry that
    filled without escalating, or the sell-into-strength ask being lifted):
    fee-v2 is takerOnly, so those fills pay NOTHING. 2026-08-18 audit: the
    default entry style IS maker, so charging every fill the taker fee
    understated recorded P&L by the same ~1.7c/contract the 2026-07-16 audit
    fixed in the other direction."""
    try:
        if maker or not filled or not price_cents:
            return 0.0
        bps = float((order or {}).get("fee_rate_bps") or 0)
        if bps <= 0:
            bps = _V2_CRYPTO_FEE_BPS
        p = max(0.0, min(1.0, float(price_cents) / 100.0))
        return round(bps / 10_000.0 * p * (1.0 - p) * int(filled), 6)
    except (TypeError, ValueError):
        return 0.0


def _entry_is_maker(pos: dict) -> bool:
    """True when this row's entry RESTED (maker → no fee under takerOnly
    fee-v2). Legacy rows (no entry_order_type recorded) keep taker-fee
    accounting.

    `entry_order_type` is written as the FEE fact rather than a copy of the
    submitted CLOB order type: an entry that crossed is stamped 'FAK' even
    when the order itself went out as a GTC (a maker whose re-quote failed and
    landed at/above the ask, a taker cross with crypto15m_taker_fak off), and
    a maker that ESCALATES is re-stamped at escalation time. Without that,
    'GTC' outlived the cross and every later fee recomputation booked a taker
    fill as free."""
    return (pos.get("entry_order_type") or "").upper() == "GTC"


def _exit_is_maker(pos: dict) -> bool:
    """The one resting (maker) exit path is sell-into-strength; every other
    exit crosses the book and pays the taker fee."""
    return (pos.get("exit_reason") or "") == "sell_strength"


def _fees_paid_usd(pos: dict, *extra) -> float:
    """Taker fees attributable to this position: the recorded entry/exit fee
    columns plus any fee computed in the current booking step. Every terminal
    pnl_usd must be net of this — the 2026-07-16 audit found NO site
    subtracted fees (and fees_usd itself was always 0), overstating recorded
    crypto15m P&L by ~0.97c/contract and flipping its lifetime sign."""
    total = float(pos.get("fees_usd") or 0.0) + float(pos.get("exit_fees_usd") or 0.0)
    for f in extra:
        total += float(f or 0.0)
    return total


def _mark_resolved(conn, pid: int, **fields) -> None:
    db.update_crypto15m_position(conn, pid, resolved=1, **fields)
    conn.execute(
        "UPDATE crypto15m_positions SET resolved_at=datetime('now') WHERE id=?", (pid,)
    )


async def _real_fill_cents(
    order_id: Optional[str], fallback_cents: Optional[float], filled: int,
    pos: Optional[dict] = None,
) -> tuple[Optional[float], Optional[float]]:
    """(avg_entry_cents, cost_usd) from the CLOB trades endpoint — the ACTUAL
    execution prices. The order object's `price` is the LIMIT (and the
    404-wallet rescue used entry_limit_cents), so every fill was booked at
    limit: chase==0 on 75/75 audited fills, real slippage invisible, cost
    overstated by the taker markup. Falls back to `fallback_cents` when the
    trades endpoint has nothing yet (it can lag a fresh fill) — the fallback
    is logged by callers so the source stays auditable. `pos` narrows the
    venue's walk to this position's market and lifetime."""
    fills = []
    if order_id:
        try:
            fills = await polymarket_api.get_fills_for_order(
                order_id, market=str((pos or {}).get("ticker") or ""),
                after=trader._fills_after((pos or {}).get("created_at")))
        except Exception as e:
            logger.debug(f"[crypto15m] fills lookup {order_id}: {e}")
    tot, qty = 0.0, 0
    for f in fills or []:
        pf = trader._parse_polymarket_fill(f)
        c, n = pf.get("price_cents"), int(pf.get("count") or 0)
        if c and n > 0:
            tot += float(c) * n
            qty += n
    if qty > 0 and filled > 0:
        avg = tot / qty
        return avg, round(avg / 100.0 * filled, 6)
    if fallback_cents is None or filled <= 0:
        return None, None
    return float(fallback_cents), round(float(fallback_cents) / 100.0 * filled, 6)


async def _poll_entry(pos: dict, cfg: dict) -> Optional[dict]:
    pid, kid = pos["id"], pos.get("order_id")
    if not kid:
        return None
    target = int(pos.get("target_contracts") or 0)
    limit_c = int(pos.get("entry_limit_cents") or 0)
    parsed = None
    try:
        resp = await polymarket_api.get_order(kid)
        order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
        parsed = trader._parse_polymarket_order(order)
    except polymarket_api.PolymarketAPIError as e:
        if e.status == 404 and target > 0:
            # A 404 on the open-order endpoint means the order is no longer
            # RESTING — which is EITHER a full match OR a cancel/expiry. The old
            # code assumed a full match and booked `target` as filled; but a
            # maker the exchange auto-cancels at window close, OR a FAK taker
            # that only partially matched, ALSO 404s — so that fabricated a
            # position (and later its settlement P&L) on contracts never bought,
            # and the wallet reconcile only ever repairs UNDER-counts, never this
            # over-count. Trust the wallet instead: book ONLY what we actually
            # hold on-chain. If nothing is held it was a no-fill cancel — mark it
            # unfilled and let reconcile_filled_from_chain promote it later if the
            # fill was merely lagging the positions API (under-count is safe).
            held = await _onchain_held(pos.get("ticker"), pos.get("direction"))
            booked = min(held, target) if held >= 1 else 0
            avg_c = cost_usd = None
            if booked:
                avg_c, cost_usd = await _real_fill_cents(kid, float(limit_c), booked, pos)
            with db.get_db() as conn:
                if booked:
                    db.update_crypto15m_position(
                        conn, pid, status="filled",
                        filled_contracts=booked,
                        cost_usd=cost_usd,
                        avg_entry_cents=avg_c,
                        fees_usd=_order_fee_usd({}, booked, avg_c,
                                                maker=_entry_is_maker(pos)),
                    )
                    logger.info(
                        f"[crypto15m] entry 404 → account holds {pos.get('asset')} "
                        f"x{booked} @ {avg_c:.0f}c "
                        f"({'real fills' if avg_c != float(limit_c) else 'limit fallback'}, "
                        f"limit {limit_c}c)"
                    )
                else:
                    _mark_resolved(conn, pid, status="canceled", exit_reason="unfilled_expired")
                    logger.info(
                        f"[crypto15m] entry 404, nothing held {pos.get('asset')} — "
                        f"unfilled (reconcile recovers a lagged fill if it landed)"
                    )
                return db.fetch_crypto15m_by_id(conn, pid)
        logger.debug(f"[crypto15m] entry poll {kid}: {e}")
    except Exception as e:
        logger.debug(f"[crypto15m] entry poll {kid}: {e}")

    filled = int(parsed.get("filled") or 0) if parsed else 0
    remaining = int(parsed.get("remaining") or 0) if parsed else 0

    # Fully filled (remaining == 0) -> lock it in as a real position. A
    # partial whose remainder is already gone (exchange-canceled at close)
    # also lands here and is kept at whatever filled. Booked at the REAL
    # execution prices from the trades endpoint (the order's `price` is the
    # limit; booking it hid chase/slippage and overstated cost).
    if filled > 0 and remaining <= 0:
        avg_c, cost_usd = await _real_fill_cents(kid, parsed["avg_cents"], filled, pos)
        with db.get_db() as conn:
            db.update_crypto15m_position(
                conn, pid, status="filled",
                filled_contracts=filled,
                cost_usd=cost_usd,
                avg_entry_cents=avg_c,
                fees_usd=_order_fee_usd(order, filled, avg_c,
                                        maker=_entry_is_maker(pos)),
            )
            return db.fetch_crypto15m_by_id(conn, pid)

    # Partial fill with the rest still resting: record progress but KEEP
    # polling (status stays 'submitted') so later fills are counted too.
    # Finalizing now would strand whatever fills the remainder, and a later
    # stop-loss would then sell fewer contracts than we actually hold.
    if filled > 0 and filled != int(pos.get("filled_contracts") or 0):
        avg_c, cost_usd = await _real_fill_cents(kid, parsed["avg_cents"], filled, pos)
        with db.get_db() as conn:
            db.update_crypto15m_position(
                conn, pid,
                filled_contracts=filled,
                cost_usd=cost_usd,
                avg_entry_cents=avg_c,
                fees_usd=_order_fee_usd(order, filled, avg_c,
                                        maker=_entry_is_maker(pos)),
            )

    # Escalate when either (a) the no-fee maker has rested too long without
    # filling (fast escalate — cross now, before the price drifts), or (b) we're
    # inside the cancel lead / the 15-min market has closed. If part of the order
    # DID fill, KEEP those contracts as a (smaller) real position.
    # In Paper (account not Live) nothing may become NEW real exposure: an
    # entry still resting from Live is pulled at once instead of being left
    # to fill, and is never crossed into a taker buy. Whatever already
    # filled is kept and managed like any open position (see run_tick).
    from config import account_live
    paper = not account_live(cfg)
    fast = _maker_fill_timed_out(pos, cfg)
    if paper or fast or _entry_expired(pos, cfg):
        if paper:
            logger.info(
                f"[crypto15m] paper: pulling the resting {pos.get('asset')} entry "
                f"(Paper completes no real buy)"
            )
        elif fast:
            logger.info(
                f"[crypto15m] fast-escalate {pos.get('asset')}: maker unfilled "
                f"after {cfg.get('crypto15m_maker_fill_sec')}s -> crossing to taker"
            )
        try:
            await polymarket_api.cancel_order(kid)
        except Exception as e:
            logger.debug(f"[crypto15m] escalate cancel {kid}: {e}")
        # Re-read once to capture any fill that landed between our poll and the
        # cancel, AND to confirm the maker is actually off the book before we
        # cross. If the cancel silently failed and the maker is STILL resting,
        # placing a taker for the full remainder risks BOTH filling (2x buy) —
        # so defer and retry next tick.
        m_filled, m_cost_cents, m_avg = filled, None, None
        cancel_confirmed = False
        try:
            resp2 = await polymarket_api.get_order(kid)
            order2 = (resp2.get("order") if isinstance(resp2, dict) else resp2) or {}
            p2 = trader._parse_polymarket_order(order2)
            if int(p2.get("filled") or 0) >= m_filled:
                m_filled = int(p2["filled"])
                m_cost_cents = int(p2["cost_cents"])
                m_avg = p2["avg_cents"]
            st = (p2.get("status") or "").lower()
            # Off the book if it's canceled or has nothing left resting.
            cancel_confirmed = (
                st in ("canceled", "cancelled")
                or int(p2.get("remaining") or 0) <= 0
            )
        except polymarket_api.PolymarketAPIError as e:
            if e.status == 404:
                cancel_confirmed = True   # 404 == off the book (matched or canceled)
            else:
                logger.debug(f"[crypto15m] escalate re-read {kid}: {e}")
        except Exception:
            pass

        # AUTHORITATIVE GUARD: never escalate past what we already hold on-chain.
        # A maker that filled (in part or full) between our poll and cancel shows
        # up in the wallet's positions even when get_order 404s, so the taker only
        # ever buys the genuine shortfall — closing the 2x-overspend race.
        held = await _onchain_held(pos.get("ticker"), pos.get("direction"))
        if held > m_filled:
            # On-chain holding is authoritative and larger than get_order
            # reported, so its cost is stale — a resting maker fills at its
            # limit, so recompute cost for the full held qty.
            m_filled = held
            m_cost_cents = held * limit_c
            m_avg = float(limit_c)

        remaining = target - m_filled

        # If there's STILL a shortfall but the maker cancel wasn't confirmed, the
        # resting order could yet fill — don't stack a taker on top (the 2x buy).
        # Defer and retry next tick. Only relevant while the market is OPEN: at/
        # after close no taker is placed (escalation is gated below), so there's
        # no double-buy to avoid and we fall through to resolve the remainder.
        # When the on-chain holding already covers the target, remaining is 0 and
        # we book it below without escalating.
        if remaining > 0 and not cancel_confirmed and not _market_closed(pos):
            logger.info(
                f"[crypto15m] escalate deferred {pos.get('asset')}: maker cancel "
                f"unconfirmed (still resting); retry next tick"
            )
            return None

        # Maker→taker escalation: cross the spread for whatever didn't rest-fill
        # so the entry actually lands before close, instead of abandoning it.
        # SCRIPT rows are exempt: a script that submitted an explicit numeric
        # price authorized a maker at THAT price, not a taker cross at the
        # engine's own entry_max — escalating would silently bypass
        # script_max_entry_cents. A script that wants a taker fill uses a
        # price:"ask" (FAK) intent instead. If the maker didn't fill, leave the
        # remainder to resolve unfilled.
        t_filled, t_cost_cents = 0, 0
        if (remaining > 0 and not paper
                and cfg.get("crypto15m_maker_escalate", True)
                and not pos.get("script_id")
                and not _market_closed(pos)):
            tk = await _escalate_to_taker(pos, cfg, remaining, already=m_filled)
            if tk:
                t_filled = int(tk.get("filled") or 0)
                t_cost_cents = int(tk.get("cost_cents") or 0)

        total_filled = m_filled + t_filled
        with db.get_db() as conn:
            if total_filled > 0:
                upd = {"status": "filled", "filled_contracts": total_filled}
                cost_cents = (m_cost_cents or 0) + t_cost_cents
                if cost_cents > 0:
                    upd["cost_usd"] = cost_cents / 100.0
                    upd["avg_entry_cents"] = cost_cents / total_filled
                elif m_avg is not None:
                    upd["avg_entry_cents"] = m_avg
                # Fee split (2026-08-18 audit — this path booked NO fee at
                # all): the maker portion that rest-filled pays nothing, the
                # escalated taker portion pays fee-v2 at its own avg price.
                if t_filled > 0:
                    upd["fees_usd"] = round(
                        float(pos.get("fees_usd") or 0.0)
                        + _order_fee_usd({}, t_filled,
                                         t_cost_cents / t_filled), 6)
                    # …and RE-STAMP the entry as a taker. The submit-time
                    # 'GTC' survived escalation, so every later recomputation
                    # keyed off _entry_is_maker (the wallet reconcile's
                    # settled branch, any re-poll) read this crossed entry as
                    # a free maker fill and wiped the fee out of pnl_usd —
                    # which is exactly what the loss breakers count.
                    upd["entry_order_type"] = "FAK"
                db.update_crypto15m_position(conn, pid, **upd)
            else:
                _mark_resolved(conn, pid, status="canceled",
                               exit_reason="paper_pulled" if paper else "unfilled_expired")
            return db.fetch_crypto15m_by_id(conn, pid)
    return None


async def _onchain_held(ticker: Optional[str], direction: Optional[str]) -> int:
    """Contracts of (ticker, direction) we actually hold on-chain right now,
    from the wallet's live positions. Used so maker→taker escalation never
    re-buys a maker that filled between our poll and cancel."""
    if not ticker:
        return 0
    try:
        positions = await polymarket_api.get_positions(limit=500)
    except Exception:
        return 0
    for p in positions:
        if p.get("ticker") != ticker:
            continue
        qty = float(p.get("position_fp") or 0.0)
        if direction == "yes" and qty > 0:
            return int(round(qty))
        if direction == "no" and qty < 0:
            return int(round(abs(qty)))
    return 0


def _age_seconds(ts) -> Optional[float]:
    """Seconds since a SQLite UTC timestamp string ('YYYY-MM-DD HH:MM:SS')."""
    if not ts:
        return None
    s = str(ts).replace("T", " ").split(".")[0]
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _maker_fill_timed_out(pos: dict, cfg: dict) -> bool:
    """Fast escalate: True once a resting maker entry has gone unfilled longer
    than `crypto15m_maker_fill_sec`. The cue to cross to a taker fill NOW —
    while the price is still near our entry — instead of waiting for the cancel
    lead and getting a late, worse fill. 0 disables (cancel-lead escalation only).
    """
    wait = float(cfg.get("crypto15m_maker_fill_sec", 0) or 0)
    if wait <= 0:
        return False
    age = _age_seconds(pos.get("created_at"))
    return age is not None and age >= wait


def _entry_expired(pos: dict, cfg: dict) -> bool:
    """An unfilled entry is abandoned within `crypto15m_maker_cancel_min`
    minutes of its market's close (0 = only once the market has closed)."""
    close_epoch = crypto15m._parse_close_epoch(pos.get("close_time") or "")
    if close_epoch is None:
        return False
    lead = max(0.0, float(cfg.get("crypto15m_maker_cancel_min", 0.0) or 0.0)) * 60.0
    return datetime.now(timezone.utc).timestamp() >= close_epoch - lead


def _market_closed(pos: dict) -> bool:
    """Has the 15-min window actually closed? (vs `_entry_expired` = inside the
    earlier cancel lead). No order — maker OR taker — can fill after close."""
    close_epoch = crypto15m._parse_close_epoch(pos.get("close_time") or "")
    if close_epoch is None:
        return False
    return datetime.now(timezone.utc).timestamp() >= close_epoch


async def _escalate_to_taker(
    pos: dict, cfg: dict, remaining: int, *, already: Optional[int] = None,
) -> Optional[dict]:
    """Maker→taker escalation: cross the spread for the unfilled remainder so it
    actually fills before close, capped at `entry_max` so we never chase an
    already-decided contract. Returns the parsed fill, or None if it couldn't
    place / the ask is too rich. LIVE path only (paper simulates in-place)."""
    ticker, direction = pos.get("ticker"), pos.get("direction")
    if not ticker:
        return None
    try:
        q = await polymarket_api.get_quote(ticker, direction)
        ask = q.get("ask_cents")
    except Exception as e:
        logger.debug(f"[crypto15m] escalate quote failed {pos.get('asset')}: {e}")
        ask = None
    if not ask or ask < 1:
        return None
    cross = min(99, int(ask))
    max_cents = int(round(crypto15m._const(cfg, "entry_max") * 100))
    if cross > max_cents:
        logger.info(
            f"[crypto15m] skip escalate {pos.get('asset')}: ask {cross}c > entry_max {max_cents}c"
        )
        return None
    # Favorite FLOOR (same as the entry gate): in favorite mode, never complete
    # an entry by crossing into a side that has CRASHED below entry_threshold
    # since the maker was placed — that's the "fill at a few cents" path through
    # escalation. Contrarian intends the cheap side, so it's exempt.
    if (cfg.get("crypto15m_direction_mode") or "favorite").lower() != "contrarian":
        thr_cents = int(round(crypto15m._const(cfg, "entry_threshold") * 100))
        if cross < thr_cents:
            logger.info(
                f"[crypto15m] skip escalate {pos.get('asset')}: ask {cross}c < "
                f"entry_threshold {thr_cents}c (favorite crashed since entry)"
            )
            return None
    coid = f"{pos.get('client_order_id')}-tk"
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=direction, action="buy",
            count=remaining, price_cents=cross, client_order_id=coid,
            order_type=taker_order_type(cfg),
        )
    except polymarket_api.OrderStatusUnknownError as e:
        # Sent, outcome unknown: the confirm below polls the id and lets the
        # wallet decide on a 404, exactly as for a clean placement.
        logger.warning(f"[crypto15m] escalate {pos.get('asset')}: {e}")
        resp = e.response
    except Exception as e:
        logger.warning(f"[crypto15m] escalate place failed {pos.get('asset')}: {e}")
        return None
    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    oid = order.get("order_id") if isinstance(order, dict) else None

    def _filled(n: int) -> dict:
        return {
            "filled": n, "cost_cents": n * cross, "avg_cents": float(cross),
            "status": "executed", "place_count": n, "remaining": 0,
        }

    held_before = (int(pos.get("target_contracts") or 0) - remaining) if already is None else int(already)

    async def _from_wallet(why: str) -> Optional[dict]:
        # What the cross actually bought is what the wallet gained. A FAK
        # that matched nothing, or matched only part, also leaves the open-
        # order endpoint (404) or reads "matched" — booking the whole
        # remainder off either fabricated contracts the wallet never held,
        # which then settled into P&L, counted toward the loss breakers and
        # got stop-loss SELLS for tokens that weren't there. The positions
        # API can lag a fill by a moment, so read it a few times.
        for i in range(3):
            got = max(0, min(remaining, await _onchain_held(ticker, direction) - held_before))
            if got > 0:
                logger.info(f"[live] escalate fill {pos.get('asset')} x{got} @ ~{cross}c ({why}; wallet)")
                return _filled(got)
            if i < 2:
                await asyncio.sleep(0.8)
        logger.info(f"[live] escalate {pos.get('asset')}: {why} but the wallet gained nothing — no fill")
        return None

    # Confirm the fill RELIABLY. A marketable taker fills ~instantly, but a
    # fully-matched order then 404s on the open-order endpoint, and so does a
    # FAK that matched only part or nothing. The order endpoint is read first;
    # if it can't say, the wallet decides (never the status string alone).
    if oid:
        for _ in range(3):
            try:
                r2 = await polymarket_api.get_order(oid)
                o2 = (r2.get("order") if isinstance(r2, dict) else r2) or {}
                parsed = trader._parse_polymarket_order(o2)
                f = int(parsed.get("filled") or 0)
                if f > 0:
                    logger.info(f"[live] escalate fill {pos.get('asset')} x{f} @ ~{cross}c")
                    return parsed
                if parsed.get("status") in ("canceled", "cancelled"):
                    return parsed
            except polymarket_api.PolymarketAPIError as e:
                if e.status == 404:
                    return await _from_wallet("order gone from the book")
            except Exception as e:
                logger.debug(f"[crypto15m] escalate poll {pos.get('asset')}: {e}")
            await asyncio.sleep(0.6)
    # No order-endpoint confirmation. The place response's status is exactly
    # "matched" (or "delayed": the crypto taker delay) only when something may
    # have filled — "unmatched" (which CONTAINS "matched") and "killed" mean
    # nothing did. Either way the amount comes from the wallet.
    status = str(order.get("status") or "").strip().lower()
    if status in ("matched", "delayed"):
        return await _from_wallet(f"placed={status}")
    return None


async def _best_bid_cents(ticker: Optional[str], direction: Optional[str]) -> Optional[int]:
    """Highest resting bid (cents) on our side's book — what we could sell into
    right now. None if the book is empty/unavailable (caller falls back)."""
    if not ticker or not direction:
        return None
    try:
        # Not the book: public /book can be 30s old, and this prices an exit.
        return (await polymarket_api.get_quote(ticker, direction)).get("bid_cents")
    except Exception:
        return None


async def _live_side_prob(ticker: Optional[str], direction: Optional[str]) -> Optional[float]:
    """The held side's probability (0..1) from the LIVE book: the websocket
    quote when it's fresh, else the REST books (our ask is 100 minus the
    other side's best bid). None when neither gives a sane two-sided book.

    The stop-loss used to decide on Gamma's `outcomePrices`, which sit
    20-27c (and up to 67c) behind the book near the close: a collapsed
    position went unstopped, and a winner on a stale-low mark got sold
    (2026-07-16: three of the first six momentum fills stopped, one 19s
    after entry, with the book 20c higher). The entry and take-profit paths
    already priced off the book; this is the stop's equivalent, with the
    same sanity rule as the entry (crypto15m._ws_mid)."""
    if not ticker or direction not in ("yes", "no"):
        return None
    try:
        meta = await polymarket_api.get_market_meta(ticker)
    except Exception:
        meta = None
    if not meta:
        return None
    yt, nt = meta.get("yes_token"), meta.get("no_token")
    clob_ws.warm(yt, nt)
    yes_q = no_q = None
    try:
        yes_q = clob_ws.feed.get_quote_cents(yt)
        no_q = clob_ws.feed.get_quote_cents(nt)
    except Exception:
        pass
    up = crypto15m._ws_up_price(yes_q, no_q)
    if up is None:
        try:
            # Not the book: public /book can be 30s old.
            up = crypto15m._ws_up_price(await polymarket_api.get_quote(ticker, "yes"), None)
        except Exception:
            up = None
    if up is None:
        return None
    return up if direction == "yes" else (1.0 - up)


async def _place_exit_sell(
    pos: dict, cfg: dict, *, exit_cents: int, reason: str
) -> Optional[dict]:
    """Cross the book to SELL the whole filled position at `exit_cents`, tagging
    the row with `reason` (stop_loss | take_profit). Shared by the stop-loss and
    take-profit paths — both flatten the same way; only the trigger differs."""
    pid, ticker, direction = pos["id"], pos["ticker"], pos["direction"]
    filled = int(pos.get("filled_contracts") or 0)
    exit_cents = max(1, min(99, int(exit_cents)))

    # Back off after a recent failure — see _EXIT_RETRY_SEC. The position stays
    # open and the next tick past the cooldown tries again; what this stops is
    # re-submitting a doomed order every 4 seconds.
    #
    # The STOP-LOSS is exempt. It is the safety exit, it has its own
    # _STOP_RETRY_SEC throttle in _manage_position, and this cooldown is
    # shared per position: a failed take-profit at t=0 used to make the stop
    # at t=20 submit nothing at all while still consuming its 90 s throttle.
    if reason != "stop_loss" and time.time() - _exit_fail_at.get(pid, 0.0) < _EXIT_RETRY_SEC:
        return None

    coid = f"krypt-c15x-{pos['asset']}-{uuid.uuid4().hex[:8]}"
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=direction, action="sell",
            count=filled, price_cents=exit_cents, client_order_id=coid,
        )
    except Exception as e:
        _exit_fail_at[pid] = time.time()
        trader.cap_dict_size(_exit_fail_at)
        # LOG it. This wrote an `error` field to the row and said nothing
        # anywhere else — no log line, and the UI styles on `status`, not on
        # `error`, so a failed exit was invisible while the position stayed
        # open. trader.py has always logged the equivalent.
        logger.warning(
            "[live] %s sell FAILED for %s x%d @ %dc — position still open: %s",
            reason.replace("_", "-").upper(), pos.get("asset"), filled,
            exit_cents, str(e)[:160],
        )
        with db.get_db() as conn:
            db.update_crypto15m_position(conn, pid, error=f"{reason} sell failed: {str(e)[:160]}")
            return db.fetch_crypto15m_by_id(conn, pid)

    _exit_fail_at.pop(pid, None)
    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    with db.get_db() as conn:
        db.update_crypto15m_position(
            conn, pid, status="exiting", exit_reason=reason,
            exit_client_order_id=coid,
            exit_order_id=order.get("order_id") if isinstance(order, dict) else None,
            exit_limit_cents=exit_cents,
        )
        logger.info(
            f"[live] {reason.replace('_', '-').upper()} sell "
            f"{pos['asset']} x{filled} @ {exit_cents}c"
        )
        return db.fetch_crypto15m_by_id(conn, pid)


async def _place_stop_loss(pos: dict, market: Optional[dict], cfg: dict) -> Optional[dict]:
    direction = pos["direction"]
    # Price the exit to cross: best bid on our side flattens immediately.
    exit_cents = await _best_bid_cents(pos["ticker"], direction)
    if exit_cents is None:
        sp = side_prob_from_market(market, direction) or 0.0
        exit_cents = int(round(sp * 100)) - 2
    return await _place_exit_sell(pos, cfg, exit_cents=exit_cents, reason="stop_loss")


async def _book_exit_from_chain(pos: dict) -> Optional[dict]:
    """Reconcile an exit whose SELL order 404'd. A 404 on the open-order endpoint
    means the order left the book — it FULLY MATCHED (the normal case for a stop
    that crosses the bid) or was canceled at close. We must NOT swallow it: if the
    row stays 'exiting', _settle_if_closed later settles the ALREADY-SOLD contracts
    as if still held, booking a stop-loss LOSS as a settlement WIN. Trust the
    wallet — book the contracts that actually left it at the price we crossed
    (exit_limit_cents; a crossing sell fills at-or-better, same proceeds convention
    as the non-404 path)."""
    pid = pos["id"]
    filled = int(pos.get("filled_contracts") or 0)
    already = int(pos.get("exit_filled_contracts") or 0)
    held = await _onchain_held(pos.get("ticker"), pos.get("direction"))
    sold_total = max(already, filled - held)
    if sold_total <= 0:
        return None  # nothing left the wallet (no-fill cancel) — settlement handles the held qty
    exit_cents = int(pos.get("exit_limit_cents") or 0)
    proceeds = sold_total * exit_cents / 100.0
    with db.get_db() as conn:
        if held <= 0:
            # The crossing sell paid the taker fee even though the order 404'd
            # before we could parse it — synthesize at the booked exit price.
            # (A lifted sell-into-strength ask was a MAKER fill: no fee.)
            exit_fee = _order_fee_usd({}, sold_total, float(exit_cents),
                                      maker=_exit_is_maker(pos))
            pnl = proceeds - float(pos.get("cost_usd") or 0.0) - _fees_paid_usd(pos, exit_fee)
            _mark_resolved(
                conn, pid, status="exited",
                exit_filled_contracts=sold_total, proceeds_usd=proceeds,
                exit_fees_usd=round(float(pos.get("exit_fees_usd") or 0.0) + exit_fee, 6),
                pnl_usd=pnl, outcome_correct=1 if pnl > 0 else 0,
            )
            logger.info(
                f"[crypto15m] exit 404 -> account flat; booked {pos.get('asset')} "
                f"sold x{sold_total} @ ~{exit_cents}c pnl=${pnl:+.2f}"
            )
        else:
            db.update_crypto15m_position(
                conn, pid, exit_filled_contracts=sold_total, proceeds_usd=proceeds,
            )
        return db.fetch_crypto15m_by_id(conn, pid)


async def _poll_exit(pos: dict) -> Optional[dict]:
    pid, kid = pos["id"], pos.get("exit_order_id")
    if not kid:
        return None
    try:
        resp = await polymarket_api.get_order(kid)
        order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
        parsed = trader._parse_polymarket_order(order)
    except polymarket_api.PolymarketAPIError as e:
        if e.status == 404:
            # 404 = the sell left the open-order book (fully matched, or canceled at
            # close). Do NOT swallow it — reconcile against the wallet and book the
            # sale, else _settle_if_closed re-settles the sold contracts as held.
            return await _book_exit_from_chain(pos)
        logger.debug(f"[crypto15m] exit poll {kid}: {e}")
        return None
    except Exception as e:
        logger.debug(f"[crypto15m] exit poll {kid}: {e}")
        return None
    sold = int(parsed.get("filled") or 0)
    remaining = int(parsed.get("remaining") or 0)
    proceeds = parsed["cost_cents"] / 100.0  # cumulative sell value from Polymarket

    # Fully sold -> the stop-loss flattened the whole position. Resolve.
    if sold > 0 and remaining <= 0:
        exit_fee = _order_fee_usd(order, sold, parsed["avg_cents"],
                                  maker=_exit_is_maker(pos))
        pnl = (proceeds - float(pos.get("cost_usd") or 0.0)
               - float(pos.get("fees_usd") or 0.0) - exit_fee)
        with db.get_db() as conn:
            _mark_resolved(
                conn, pid, status="exited",
                exit_filled_contracts=sold, proceeds_usd=proceeds,
                exit_fees_usd=exit_fee,
                pnl_usd=pnl, outcome_correct=1 if pnl > 0 else 0,
            )
            return db.fetch_crypto15m_by_id(conn, pid)

    # Partial sell with contracts still resting: record what's sold so far
    # but stay 'exiting'. The unsold remainder is still held and will either
    # finish selling on a later poll or ride to settlement, where
    # _settle_if_closed credits this partial's proceeds and only settles the
    # residual. Resolving now would strand the unsold contracts.
    if sold > 0 and sold != int(pos.get("exit_filled_contracts") or 0):
        with db.get_db() as conn:
            db.update_crypto15m_position(
                conn, pid, exit_filled_contracts=sold, proceeds_usd=proceeds,
            )
    return None


async def _settle_if_closed(pos: dict, *, market=_UNSET) -> Optional[dict]:
    """An 'exiting' position whose market settled while the stop-loss sell
    sat UNFILLED (thin/empty books — routine on demo) still holds its
    contracts: Polymarket cancels resting orders at close and the binary
    settles. Book that settlement, or the row stays 'exiting' forever and
    pins an executor slot (live bug: 5 such rows froze the engine).

    `market` may be supplied by the caller (run_tick batches ONE
    fetch_markets_map for every open ticker per tick); omitted, it falls back
    to the original per-position fetch so other callers/tests are unaffected."""
    if market is _UNSET:
        try:
            market = await polymarket_api.fetch_market(pos["ticker"])
        except Exception:
            return None
    if market is None:
        return None
    payout = trader._market_yes_payout(market) if market else None
    if payout is None:
        return None

    kid = pos.get("exit_order_id")
    if kid:
        try:
            await polymarket_api.cancel_order(kid)  # best-effort tidy-up
        except Exception:
            pass

    filled = int(pos.get("filled_contracts") or 0)
    cost_usd = float(pos.get("cost_usd") or 0.0)
    sold = int(pos.get("exit_filled_contracts") or 0)        # already sold via the stop
    partial_proceeds = float(pos.get("proceeds_usd") or 0.0)  # proceeds from that sale
    residual = max(0, filled - sold)                          # still held at settlement
    our = payout if pos["direction"] == "yes" else (1.0 - payout)
    settlement = residual * our
    # A partial sale that was recorded without its fee (the partial-sell path
    # doesn't parse fees) still paid one — synthesize it at the realized price.
    synth_exit_fee = 0.0
    if sold > 0 and not float(pos.get("exit_fees_usd") or 0.0):
        synth_exit_fee = _order_fee_usd({}, sold, partial_proceeds / sold * 100.0,
                                        maker=_exit_is_maker(pos))
    pnl = partial_proceeds + settlement - cost_usd - _fees_paid_usd(pos, synth_exit_fee)
    correct = 1 if our >= 0.99 else (0 if our <= 0.01 else (1 if pnl > 0.01 else 0))
    with db.get_db() as conn:
        fields = dict(
            exit_reason=pos.get("exit_reason") or "settlement",
            outcome_correct=correct, settlement_usd=settlement, pnl_usd=pnl,
        )
        if synth_exit_fee:
            fields["exit_fees_usd"] = round(synth_exit_fee, 6)
        _mark_resolved(conn, pos["id"], status="settled", **fields)
        logger.info(f"[crypto15m] exiting->settled {pos['asset']} pnl=${pnl:+.2f} (sell never filled)")
        return db.fetch_crypto15m_by_id(conn, pos["id"])


async def _resolved_payout_from_chain(pos: dict) -> Optional[float]:
    """Fallback settlement for the fast crypto 'Up/Down' markets when Gamma's
    `result`/`status` fields lag the on-chain settlement (which otherwise
    leaves a settled position stuck 'open').

    Once our shares are redeemable the market HAS settled — but redeemable does
    NOT imply a win (losing tokens are redeemable for $0 too). So we book the
    outcome from the held token's actual settled price (`cur_price` ≈ 1 = our
    side won, ≈ 0 = lost), never from the mere existence of a redeemable
    balance. Returns the YES-side payout, or None if not found / not yet a
    decisive 0-or-1 settlement (in which case the Gamma path confirms it later).
    """
    try:
        positions = await polymarket_api.get_positions()
    except Exception:
        return None
    direction = pos.get("direction")
    for p in positions:
        if (p.get("ticker") or "") != pos.get("ticker"):
            continue
        if not p.get("redeemable"):
            continue
        held_side = "yes" if float(p.get("position_fp") or 0) > 0 else "no"
        if held_side != direction:
            continue
        held = max(0.0, min(1.0, float(p.get("cur_price") or 0.0)))
        if 0.01 < held < 0.99:
            return None  # not a decisive 0/1 settlement yet — let Gamma confirm
        # held ≈ 1 → our side won; held ≈ 0 → our side lost.
        return held if direction == "yes" else (1.0 - held)
    return None


async def _manage_position(
    pos: dict, cfg: dict, env: str, *, market=_UNSET
) -> Optional[dict]:
    # Legacy paper rows (from before paper mode was removed) are not real
    # on-chain holdings — retire them instead of polling a non-existent order.
    if pos.get("dry_run"):
        with db.get_db() as conn:
            _mark_resolved(conn, pos["id"], status="canceled", exit_reason="paper_removed")
            return db.fetch_crypto15m_by_id(conn, pos["id"])

    status = pos.get("status")

    if status == "submitted":
        return await _poll_entry(pos, cfg)
    if status == "exiting":
        row = await _poll_exit(pos)
        if row:
            return row
        return await _settle_if_closed(pos, market=market)
    if status == "error" and int(pos.get("filled_contracts") or 0) <= 0:
        # Legacy stuck rows from before failed entries were auto-resolved.
        with db.get_db() as conn:
            _mark_resolved(conn, pos["id"], status="error")
            return db.fetch_crypto15m_by_id(conn, pos["id"])
    if status != "filled":
        return None

    pid = pos["id"]
    direction = pos["direction"]
    filled = int(pos.get("filled_contracts") or 0)
    cost_usd = float(pos.get("cost_usd") or 0.0)

    # Each open position needs ITS OWN market (the snapshot's "active"
    # market may have already rolled to the next quarter). run_tick supplies
    # it from ONE batched fetch_markets_map covering every open ticker; the
    # per-position fetch below is the fallback for any other caller.
    if market is _UNSET:
        try:
            market = await polymarket_api.fetch_market(pos["ticker"])
        except Exception:
            market = None

    # 1) settlement (terminal) takes priority.
    payout = trader._market_yes_payout(market) if market else None
    if payout is None:
        # Gamma's `result` lags on-chain settlement for these fast markets;
        # settle from the redeemable shares' settled price.
        payout = await _resolved_payout_from_chain(pos)
    if payout is not None:
        our = payout if direction == "yes" else (1.0 - payout)
        settlement = filled * our
        pnl = settlement - cost_usd - _fees_paid_usd(pos)
        correct = 1 if our >= 0.99 else (0 if our <= 0.01 else (1 if pnl > 0.01 else 0))
        with db.get_db() as conn:
            _mark_resolved(
                conn, pid, status="settled",
                exit_reason=(pos.get("exit_reason") or "settlement"),
                outcome_correct=correct, settlement_usd=settlement, pnl_usd=pnl,
            )
            return db.fetch_crypto15m_by_id(conn, pid)

    # 1.4) sell-into-strength — rest a GTC ask at the strength target the
    # moment the position is filled. Unlike the polling take-profit below, a
    # RESTING ask is lifted by any spike between engine ticks, fills as a
    # maker (no fee), and dies harmlessly with the window if never reached
    # (_poll_exit's wallet reconcile books whichever way it went). Eligible
    # only when the target clears the entry — sniper entries above the
    # target are naturally exempt.
    strength_c = strength_exit_cents(pos, cfg)
    if strength_c is not None:
        row = await _place_exit_sell(
            pos, cfg, exit_cents=strength_c, reason="sell_strength"
        )
        if row is not None and row.get("status") == "exiting":
            return row
        # A refused or cooled-down strength ask must NOT end the tick here:
        # that skipped the take-profit and stop-loss below for a full cooldown.

    # 1.5) per-bet take-profit — cash out while our side's bid is still high,
    # rather than riding a winning binary to settlement and risking a late
    # reversal back to 0. Below settlement (a resolved market pays full value,
    # better than selling at the target) but above stop-loss. Two triggers, an
    # absolute price target (take_profit) and a percent-above-cost (take_profit_pct);
    # whichever fires first sells. Only touches the book when either is enabled,
    # so it adds no load when both are off.
    # Script rows carry their own row-level tp_pct (set via a decide()/manage()
    # intent), so evaluate take-profit for them even when the engine's global
    # knobs are off — otherwise the advertised take_profit_pct field is a
    # silent no-op unless the user also armed the built-in engine's TP.
    if (crypto15m._const(cfg, "take_profit") > 0
            or crypto15m._const(cfg, "take_profit_pct") > 0
            or pos.get("tp_pct")):
        bid_cents = await _best_bid_cents(pos["ticker"], direction)
        if (should_take_profit(pos, bid_cents, cfg)
                or should_take_profit_pct(pos, bid_cents, cfg)):
            row = await _place_exit_sell(
                pos, cfg, exit_cents=bid_cents, reason="take_profit"
            )
            if row is not None and row.get("status") == "exiting":
                return row
            # Same as above: a failed take-profit still leaves the stop armed.

    # 2) stop-loss — place a REAL exit sell (the engine only runs when authed).
    # Decided on the live book; Gamma's lagging mark only when there is none.
    # The book is read only when a stop is armed at all: every stop rule
    # fires at a price of 0, so one that doesn't is off for this row.
    side_prob = None
    if should_stop_loss(pos, 0.0, cfg):
        side_prob = await _live_side_prob(pos.get("ticker"), direction)
    if side_prob is None:
        side_prob = side_prob_from_market(market, direction)
    if should_stop_loss(pos, side_prob, cfg):
        if time.time() - _stop_retry_at.get(pid, 0.0) < _STOP_RETRY_SEC:
            return None  # a recent attempt errored — don't re-fire every 4s tick
        _stop_retry_at[pid] = time.time()
        trader.cap_dict_size(_stop_retry_at)
        return await _place_stop_loss(pos, market, cfg)

    return None


# ───────── wallet reconcile (missed-fill recovery) ─────────────────────


async def reconcile_filled_from_chain(
    cfg: dict, env: str, *, lookback_sql: str = "-45 minutes"
) -> list[dict]:
    """Authoritative wallet reconcile for fills the polling missed.

    These fast 15-min markets lag on the open-order/trades endpoints, so an entry
    can FILL on-chain while every poll still reports it resting — the engine then
    writes it off as 'canceled' (0 fill, $0) even though the contracts are in the
    wallet, and the cost, the loss, and the settlement all go unbooked. The wallet
    is the one source that aggregates EVERY fill regardless of which order id
    matched (resting maker OR escalation taker), so it gets the final word: any
    canceled/error 0-fill row whose contracts we actually hold is promoted to its
    true state — re-opened for management if still live, or booked from the held
    token's settled value if it already resolved. Returns the rows it corrected.
    `lookback_sql` is a SQLite datetime modifier ('-45 minutes' per tick; widen it
    for a one-off backfill)."""
    try:
        with db.get_db() as conn:
            candidates = db.crypto15m_reconcile_candidates(conn, env, lookback_sql)
    except Exception as e:
        logger.debug(f"[crypto15m] reconcile candidate query failed: {e}")
        return []
    if not candidates:
        return []

    try:
        positions = await polymarket_api.get_positions(limit=500)
    except Exception as e:
        logger.debug(f"[crypto15m] reconcile positions fetch failed: {e}")
        return []
    by_ticker: dict[str, dict] = {}
    for p in positions:
        tk = p.get("ticker")
        if tk:
            by_ticker[tk] = p

    fixed: list[dict] = []
    for pos in candidates:
        ticker = pos.get("ticker")
        direction = pos.get("direction")
        if not ticker:
            continue
        wp = by_ticker.get(ticker)
        if not wp:
            continue  # not held: genuinely unfilled, or already redeemed off-wallet
        qty = float(wp.get("position_fp") or 0.0)
        held = qty if direction == "yes" else -qty   # NO holdings are signed negative
        if held < 1:
            continue  # wrong side / nothing held

        # Only promote when the held position is cleanly attributable to THIS row
        # (exactly one row on the market, none already booked a fill). 15m markets
        # have a unique conditionId per window so this is the norm; skipping the
        # rare ambiguous case beats splitting one wallet position across rows.
        try:
            with db.get_db() as conn:
                counts = db.crypto15m_ticker_counts(conn, ticker, env)
        except Exception:
            continue
        if counts["total"] != 1 or counts["filled_rows"] > 0:
            logger.warning(
                f"[crypto15m] reconcile: {pos.get('asset')} holds {held:.0f} on "
                f"{ticker[:12]}… but {counts['total']} rows / {counts['filled_rows']} "
                f"already filled — skipping to avoid double-count (review manually)"
            )
            continue

        held_n = int(round(held))
        exposure = float(wp.get("market_exposure_dollars") or 0.0)   # cost basis ($)
        cost_usd = round(exposure, 4)
        avg_cents = max(1, min(99, int(round((exposure / held) * 100)))) if held > 0 else None
        token_price = max(0.0, min(1.0, float(wp.get("cur_price") or 0.0)))  # held-token value
        redeemable = bool(wp.get("redeemable"))

        with db.get_db() as conn:
            if not redeemable:
                # Still live → re-open as a real filled position so the normal
                # manage path stop-losses / settles it.
                db.update_crypto15m_position(
                    conn, pos["id"], status="filled", resolved=0, resolved_at=None,
                    exit_reason=None, error=None,
                    filled_contracts=held_n, cost_usd=cost_usd, avg_entry_cents=avg_cents,
                )
                logger.warning(
                    f"[crypto15m] reconcile RECOVERED {pos.get('asset')} {direction} "
                    f"x{held_n} @ ~{avg_cents}c (was {pos.get('status')}/"
                    f"{pos.get('exit_reason')}; now managed to settlement)"
                )
            else:
                # Already settled on-chain → book from the held token's settled
                # value (≈1 = our side won, ≈0 = lost). Synthesize the entry
                # fee at the reconstructed price — unless the row's entry was
                # a resting GTC, in which case the missed fill was a maker
                # fill and paid nothing.
                entry_fee = _order_fee_usd({}, held_n, float(avg_cents or 0),
                                           maker=_entry_is_maker(pos))
                settlement = held_n * token_price
                pnl = settlement - cost_usd - entry_fee
                correct = (1 if token_price >= 0.99
                           else (0 if token_price <= 0.01 else (1 if pnl > 0.01 else 0)))
                db.update_crypto15m_position(
                    conn, pos["id"], status="settled", resolved=1,
                    exit_reason="settlement_reconciled", error=None,
                    filled_contracts=held_n, cost_usd=cost_usd, avg_entry_cents=avg_cents,
                    fees_usd=round(entry_fee, 6),
                    settlement_usd=round(settlement, 4), pnl_usd=round(pnl, 4),
                    outcome_correct=correct,
                )
                conn.execute(
                    "UPDATE crypto15m_positions SET resolved_at=datetime('now') "
                    "WHERE id=? AND resolved_at IS NULL",
                    (pos["id"],),
                )
                logger.warning(
                    f"[crypto15m] reconcile BOOKED {pos.get('asset')} {direction} "
                    f"x{held_n} cost=${cost_usd:.2f} pnl=${pnl:+.2f} "
                    f"(was {pos.get('status')}/{pos.get('exit_reason')}; filled but mis-marked)"
                )
            row = db.fetch_crypto15m_by_id(conn, pos["id"])
            if row:
                fixed.append(row)
    return fixed


# ───────── tick / status ──────────────────────────────────────────────


_last_heartbeat_t = 0.0
_last_halt_log_t = 0.0
_last_lifetime_halt_log_t = 0.0
_last_reconcile_t = 0.0
_last_reconcile_ok_t = 0.0   # last SUCCESSFUL pass — drives the adaptive lookback


def _lifetime_loss_tripped(cfg: dict, env: str) -> tuple[bool, str]:
    """Durable cumulative realized-loss guard. The daily breaker resets every UTC
    midnight, so a net-negative engine bleeds the daily cap forever with no
    ceiling. This uses a counter that SURVIVES a stats wipe (db.lifetime_realized_pnl):
    once cumulative realized loss reaches the absolute-$ override (if > 0) or the
    configured fraction of the starting bankroll, pause NEW entries. Returns
    (tripped, human reason)."""
    limit_usd = float(cfg.get("crypto15m_lifetime_loss_limit_usd") or 0.0)
    limit_pct = _clamp01(cfg.get("crypto15m_lifetime_loss_limit_pct", 0.0))
    if limit_usd <= 0 and limit_pct <= 0:
        return False, ""
    with db.get_db() as conn:
        lifetime = db.lifetime_realized_pnl(conn, "crypto15m", env)
        if limit_usd > 0:
            cap = limit_usd
        else:
            bankroll = db.effective_start_bankroll(
                conn, env, cfg.get("start_bankroll_usd", 0.0))
            if bankroll <= 0:
                return False, ""  # no bankroll baseline yet — can't size a % cap
            cap = bankroll * limit_pct
    loss = -lifetime  # positive when the engine is net down
    if cap > 0 and loss >= cap:
        return True, (
            f"lifetime loss limit reached — engine paused; adjust settings to "
            f"resume (cumulative ${lifetime:+.2f}, loss ${loss:.2f} >= "
            f"cap ${cap:.2f})"
        )
    return False, ""

# Optional callback the host (service.py) registers so the engine can ask it to
# durably turn crypto trading OFF when the overall take-profit target is hit
# (persist the toggle + notify the UI). Kept as a hook to avoid importing
# service.py here (it imports us). Signature: async (info: dict) -> None.
_auto_off_cb = None


def set_auto_off_callback(cb) -> None:
    """Register the host's 'turn the engine off' callback (see _auto_off_cb)."""
    global _auto_off_cb
    _auto_off_cb = cb


def _heartbeat(open_count: int) -> None:
    """Once-a-minute proof-of-life so the log shows the executor keeps ticking
    regardless of whether the Crypto tab is open (it runs in the backend loop)."""
    global _last_heartbeat_t
    import time as _t
    now = _t.time()
    if now - _last_heartbeat_t >= 60:
        _last_heartbeat_t = now
        logger.info(f"[crypto15m] executor alive (open positions: {open_count})")


async def run_tick(cfg: dict, *, authed: bool) -> list[dict]:
    """One execution pass: manage open positions, then (if enabled) consider new
    entries. Returns the list of changed position rows. Places REAL orders — no
    paper mode. Open positions are managed to settlement whenever the wallet is
    connected, EVEN IF the engine is disabled, so a bet is never stranded after
    the engine is turned off (manual toggle or the overall take-profit
    kill-switch). New entries are opened only when crypto15m_enabled is on."""
    if not authed:
        return []
    # Effective switch: the user's switch AND a live account. In Paper the
    # engine creates no new real exposure: it opens nothing, and an entry
    # still resting from Live is pulled rather than filled or escalated
    # (_poll_entry). Positions that already hold real contracts keep their
    # stop-loss / take-profit sells and settle as usual: a Paper switch must
    # not strand a live 15-minute bet without the stop guarding it, and a
    # sell only ever shrinks what the wallet holds.
    from config import engine_on
    enabled = engine_on(cfg, "crypto15m_enabled")
    env = trader.get_env()

    # Authoritative wallet reconcile (throttled to ~1/min): recover any entry that
    # FILLED on-chain but was mis-marked canceled/error because the order/trades
    # endpoints lagged. Runs FIRST so a recovered-live row is picked up by
    # get_open_crypto15m and managed in this same tick. The candidate query is
    # DB-only and the wallet fetch only fires when there's something to recover.
    global _last_reconcile_t, _last_reconcile_ok_t
    import time as _t
    if _t.time() - _last_reconcile_t >= 60:
        _last_reconcile_t = _t.time()
        # Adaptive lookback: the 6h floor matches the external-import shield
        # (db.crypto15m_owned_keys) so a fill mis-marked canceled is reclaimed
        # for as long as it's shielded — a 45-minute floor let it age out of
        # reclaim while still shielded, leaving its on-chain loss unbooked (the
        # daily/lifetime breakers then under-count and a bleeding engine trades
        # past its caps). After an outage longer than that, widen to cover the
        # whole gap since the last SUCCESSFUL pass (capped 48h; first pass after
        # boot covers the full cap — candidates are re-checked idempotently, so
        # the wide sweep is safe).
        if _last_reconcile_ok_t > 0:
            gap_min = max(360.0, (_t.time() - _last_reconcile_ok_t) / 60.0 + 15.0)
        else:
            gap_min = 48 * 60.0  # unknown downtime before this boot
        lookback = f"-{min(int(gap_min), 48 * 60)} minutes"
        try:
            await reconcile_filled_from_chain(cfg, env, lookback_sql=lookback)
            _last_reconcile_ok_t = _t.time()
        except Exception as e:
            logger.warning(f"[crypto15m] reconcile pass failed: {e}")

    # Pull open positions FIRST — we manage them to settlement whenever the
    # wallet is connected, even with the engine disabled (so nothing is stranded
    # after a turn-off). The snapshot (for NEW entries) is fetched later, only
    # when enabled, so a disabled-but-idle engine pays no cost.
    with db.get_db() as conn:
        open_positions = db.get_open_crypto15m(conn, env)

    # Disabled AND nothing open → genuine no-op (no snapshot, no work).
    if not enabled and not open_positions:
        return []

    open_by_asset = {p["asset"]: p for p in open_positions}
    open_count = len(open_positions)
    _heartbeat(open_count)

    updated: list[dict] = []

    # ONE Gamma batch per tick covering every open ticker, then manage the
    # positions concurrently. This was a serial pass with an UNCACHED
    # fetch_market inside each _manage_position: 7 open positions = 7 round
    # trips per 4 s tick (~105 Gamma calls/minute), every one of them ahead of
    # the exit it might trigger. fetch_markets_map bounds its own burst at 8.
    markets_by_ticker: dict[str, dict] = {}
    _tickers = [p["ticker"] for p in open_positions if p.get("ticker")]
    if _tickers:
        try:
            markets_by_ticker = await polymarket_api.fetch_markets_map(_tickers)
        except Exception as e:
            # Batch miss degrades to "no market this tick" — identical to the
            # old per-position fetch failing, which set market=None.
            logger.debug(f"[crypto15m] batch market fetch failed: {e}")

    # One coroutine per position id. Dedupe defensively: each coroutine mutates
    # its OWN bot_crypto15m row, so two coroutines must never share an id (the
    # query can't return one twice, but a future join could).
    _seen_pids: set = set()
    _manage_targets: list[dict] = []
    for pos in open_positions:
        if pos["id"] in _seen_pids:
            continue
        _seen_pids.add(pos["id"])
        _manage_targets.append(pos)

    async def _manage_one(p: dict) -> Optional[dict]:
        try:
            return await _manage_position(
                p, cfg, env, market=markets_by_ticker.get(p.get("ticker") or ""))
        except Exception as e:
            logger.warning(f"[crypto15m] manage {p.get('asset')} failed: {e}")
            return None

    for row in await asyncio.gather(*[_manage_one(p) for p in _manage_targets]):
        if row:
            updated.append(row)

    # Daily loss circuit-breaker: once today's realized crypto15m P&L crosses the
    # limit, stop opening NEW positions for the day (open ones are still managed
    # above). Independent of the main bot's stop_loss_on_day.
    global _halt_reason
    loss_limit = float(cfg.get("crypto15m_daily_loss_limit") or 0.0)
    if loss_limit < 0:
        with db.get_db() as conn:
            today_pnl = db.crypto15m_today_pnl(conn, env)          # wipe-robust
            today_open = db.crypto15m_today_open_cost(conn, env)   # open exposure
        # Count the committed cost of still-open TODAY positions as potential
        # loss so a wave of settlements can't overshoot the cap by up to
        # max_concurrent bets before the breaker engages.
        today_figure = today_pnl - today_open
        if today_figure <= loss_limit:
            global _last_halt_log_t
            _halt_reason = (
                f"Daily loss limit hit (today realized ${today_pnl:+.2f} + open "
                f"exposure ${today_open:.2f} = ${today_figure:+.2f} ≤ "
                f"${loss_limit:+.2f}) — new entries paused until UTC midnight. "
                f"Adjust 'Daily loss limit' to resume sooner."
            )
            tnow = datetime.now(timezone.utc).timestamp()
            if tnow - _last_halt_log_t >= 60:
                _last_halt_log_t = tnow
                logger.warning(
                    f"[crypto15m] daily loss limit hit (today realized "
                    f"${today_pnl:+.2f} + open exposure ${today_open:.2f} => "
                    f"${today_figure:+.2f} <= ${loss_limit:+.2f}) — pausing NEW "
                    f"entries for the day"
                )
            return updated

    # Lifetime / cumulative realized-loss circuit-breaker (durable, survives a
    # history wipe). Independent of the daily cap above; caps TOTAL drawdown so a
    # net-negative engine can't bleed the daily limit every day forever.
    _lt_trip, _lt_why = _lifetime_loss_tripped(cfg, env)
    if _lt_trip:
        global _last_lifetime_halt_log_t
        _halt_reason = (
            f"{_lt_why} Raise or disable 'Lifetime loss limit' in the risk "
            f"settings below to resume."
        )
        tnow = datetime.now(timezone.utc).timestamp()
        if tnow - _last_lifetime_halt_log_t >= 60:
            _last_lifetime_halt_log_t = tnow
            logger.warning(f"[crypto15m] {_lt_why}")
        return updated
    _halt_reason = ""

    # Overall take-profit kill-switch (only while enabled — it's what turns the
    # engine OFF). Realized crypto15m P&L GAINED since the target was enabled is
    # tracked against a baseline captured the first tick after it's set and
    # persisted in app_kv (so a restart can't reset the count). Once the gain
    # reaches the target we flip crypto15m_enabled off (open positions keep being
    # managed above) and ask the host to persist the toggle + notify the UI.
    bl_key = f"c15_tp_baseline:{env}"
    tp_total = float(cfg.get("crypto15m_take_profit_total") or 0.0)
    if enabled and tp_total > 0:
        with db.get_db() as conn:
            realized = db.crypto15m_stats(conn, env)["realizedPnlUsd"]
            bl_raw = db.kv_get(conn, bl_key)
            if bl_raw is None:
                db.kv_set(conn, bl_key, repr(realized))
                baseline = realized
                logger.info(
                    f"[crypto15m] overall take-profit armed: +${tp_total:.2f} "
                    f"from baseline ${realized:+.2f}"
                )
            else:
                try:
                    baseline = float(bl_raw)
                except (TypeError, ValueError):
                    baseline = realized
        gained = realized - baseline
        if gained >= tp_total:
            with db.get_db() as conn:
                db.kv_delete(conn, bl_key)  # consumed — a future enable re-arms
            cfg["crypto15m_enabled"] = False  # stop now (STATE.cfg is passed by ref)
            logger.info(
                f"[crypto15m] overall take-profit HIT (gained ${gained:+.2f} "
                f">= ${tp_total:.2f}) — turning the crypto engine OFF"
            )
            if _auto_off_cb is not None:
                try:
                    await _auto_off_cb(
                        {"reason": "take_profit_total", "gained": gained, "target": tp_total}
                    )
                except Exception as e:
                    logger.debug(f"[crypto15m] auto-off callback failed: {e}")
            return updated
    elif tp_total <= 0:
        # Target disabled — clear any stale baseline so a future enable re-arms
        # from the then-current P&L, not a months-old snapshot.
        with db.get_db() as conn:
            if db.kv_get(conn, bl_key) is not None:
                db.kv_delete(conn, bl_key)

    # Past this point we OPEN new positions — only when the engine is enabled.
    if not enabled:
        return updated

    # The snapshot powers NEW entries only (managing uses the batched
    # fetch_markets_map above). It's bounded + resilient; on failure we skip new
    # entries this tick. Fetched here so a disabled engine never pays its cost.
    try:
        snap = await crypto15m.snapshot(cfg)
    except Exception as e:
        logger.warning(f"[crypto15m] snapshot unavailable this tick; managed open positions only: {e}")
        snap = {"assets": []}
    assets = {a["asset"]: a for a in snap.get("assets", [])}

    # Fresh block-reason slate every tick that reaches the entry section —
    # stale reasons from a previous window would mislead the diagnostics panel.
    _block_reasons.clear()

    with db.get_db() as conn:
        # Markets we've already placed an entry on this window — one attempt
        # each. Supersedes the errored-only set: a canceled/unfilled maker must
        # NOT be retried on the same market (that was the cancel-spam bug).
        attempted_tickers = db.crypto15m_attempted_tickers(conn, env)

    # LIVE always needs the balance so we never size an order we can't
    # collateralize (the engine only runs when authed).
    balance_usd = await _bankroll_usd(cfg, bool(authed))
    # Shared-wallet budget: crypto15m trades the SAME wallet as the main + copy
    # engines, so honor the same cash-reserve + total-exposure caps they enforce,
    # counting positions already open across every bot_positions engine (main/copy).
    # Without this, crypto15m sized against gross cash and could deploy past the
    # labeled caps — and into the cash reserve — the rest of the app respects. For a
    # crypto15m-only user (no other-engine positions, default reserve=0 /
    # exposure=1.0) this EQUALS gross cash, so single-engine sizing is unchanged.
    with db.get_db() as conn:
        _exposure = db.current_total_exposure_usd(conn, env)
        _filled_exposure = db.current_filled_exposure_usd(conn, env)
    _pending_notional = max(0.0, _exposure - _filled_exposure)
    _total_bankroll = max(0.0, balance_usd) + max(0.0, _filled_exposure)
    _reserve = _total_bankroll * float(cfg.get("min_cash_reserve_fraction", 0.0) or 0.0)
    _max_exposure = _total_bankroll * float(cfg.get("max_total_exposure_fraction", 1.0) or 1.0)
    _spendable_cash = max(0.0, balance_usd) - _pending_notional
    budget_usd = min(max(0.0, _spendable_cash - _reserve), max(0.0, _max_exposure - _exposure))

    # Wallet mutual exclusion: don't OPEN new entries if another account/instance
    # is trading the same wallet (the manage/exit passes above already ran, so open
    # positions are still handled). Fail-open. Shares the lock with the main + copy
    # engines; within one process they never self-contend.
    _ok_open, _lock_why = trader.can_open_new_entries(env)
    if not _ok_open:
        logger.info(f"[crypto15m] {_lock_why}")
        return updated

    # Armed parlay schedule: each entry runs under its UTC hour's generated
    # config (unseated hours don't trade). It REPLACES the entry config while
    # armed — the schedule was validated as a whole, so mixing in the user's
    # base entry mode would trade something never backtested.
    parlay_armed, parlay_sched = parlay_state()
    parlay_cfg = None
    parlay_block = ""
    if parlay_armed and parlay_sched:
        want_iv = str(parlay_sched.get("interval") or "15m")
        have_iv = crypto15m._interval(cfg)
        if have_iv != want_iv:
            # A schedule tuned on 15m windows must never fire on 5m/hourly
            # ones — armed + mismatched means NO entries, not base entries.
            parlay_block = (
                f"parlay: schedule is for {want_iv} windows; Crypto tab is on {have_iv}"
            )
        else:
            parlay_cfg = {**cfg, "crypto15m_hour_configs": parlay_sched["hour_configs"]}

    for sym, a in assets.items():
        if sym in open_by_asset:
            continue
        if not crypto15m.asset_enabled(cfg, sym):
            continue  # asset toggled off in the Crypto tab — don't open new positions
        if a.get("ticker") in attempted_tickers:
            continue  # already attempted this market this window — one shot each
        eff_cfg = cfg
        if parlay_armed and parlay_cfg is None:
            _block_reasons[a.get("asset") or sym or "?"] = parlay_block
            continue
        if parlay_cfg is not None:
            merged = crypto15m.hour_override(parlay_cfg, a.get("hourUtc"))
            if merged is None:
                _block_reasons[a.get("asset") or sym or "?"] = "parlay: hour not in schedule"
                continue
            merged["_parlay"] = True
            eff_cfg = merged
            # The snapshot baked the BASE config's window/threshold into
            # `signal` — recompute it under this hour's config.
            a = crypto15m.resignal_asset(a, eff_cfg)
        _restr = polymarket_api.trading_restriction()
        if _restr is not None:
            # Matching-engine restart / post-only window: every entry here is
            # a taker or would be refused — say so instead of logging errors.
            _block_reasons[a.get("asset") or sym or "?"] = _restr["message"]
            continue
        ok, _why = should_enter(a, eff_cfg, has_open=False, open_count=open_count)
        if not ok:
            # Surface the block reason instead of discarding it — this is
            # what the "why isn't it trading" panel reads per asset.
            _block_reasons[a.get("asset") or sym or "?"] = _why
            continue
        try:
            one = await _open_entry(a, eff_cfg, env, budget_usd)
            rows = [one] if one else []
            if rows:
                open_count += 1
            for row in rows:
                updated.append(row)
                # Decrement the in-tick budget by the collateral this entry
                # committed. The account balance settles asynchronously, so after
                # the first placement the fetched balance is stale — without this
                # the next asset sizes against money already committed and gets
                # rejected (the wall of 'not enough balance' errors). For a
                # resting maker, cost_usd is still 0, so fall back to the notional
                # locked (contracts × limit).
                if row.get("status") in ("submitted", "filled", "partial"):
                    committed = float(row.get("cost_usd") or 0.0)
                    if committed <= 0:
                        committed = (int(row.get("target_contracts") or 0)
                                     * int(row.get("entry_limit_cents") or 0) / 100.0)
                    budget_usd = max(0.0, budget_usd - committed)
        except Exception as e:
            logger.warning(f"[crypto15m] entry {sym} failed: {e}")

    return updated


async def _sizing_preview(cfg: dict, authed: bool) -> dict:
    """A worked example of the current sizing settings for the UI, so the
    user can see roughly how much each bet costs before placing one."""
    mode = (cfg.get("crypto15m_sizing_mode") or "fixed").lower()
    balance_pct = _clamp01(cfg.get("crypto15m_balance_pct", 0.02))
    max_loss_pct = _clamp01(cfg.get("crypto15m_max_loss_pct", 0.0))
    order_size = max(1, int(cfg.get("crypto15m_order_size", 1)))
    bal = await _bankroll_usd(cfg, bool(authed))

    # Representative entry price: the favorite threshold (the cheap side
    # for contrarian); maker rests ~1¢ under it, taker crosses by the markup.
    thr = crypto15m._const(cfg, "entry_threshold")
    base = (1.0 - thr) if (cfg.get("crypto15m_direction_mode") == "contrarian") else thr
    if (cfg.get("crypto15m_entry_style") or "maker") == "maker":
        est_price_cents = max(1, min(99, int(round(base * 100)) - 1))
    else:
        est_price_cents = entry_limit_cents(base, crypto15m._const(cfg, "entry_diff"))
    streak_mult = streak_multiplier(cfg, trader.get_env())
    est_contracts = compute_entry_contracts(
        cfg, entry_limit_cents=est_price_cents, balance_usd=bal, order_size=order_size,
        streak_mult=streak_mult,
    )
    est_cost = est_contracts * est_price_cents / 100.0

    note = ""
    if mode == "balance_pct" and bal <= 0:
        note = "No balance yet — using fixed order size. Connect Polymarket or set a start bankroll to size by %."
    elif max_loss_pct > 0 and bal > 0 and est_contracts < 1:
        note = f"Max-loss budget too small to fund a contract at ~{est_price_cents}c."

    return {
        "mode": mode,
        "balancePct": balance_pct,
        "maxLossPct": max_loss_pct,
        "balanceUsd": bal,
        "estPriceCents": est_price_cents,
        "estContracts": est_contracts,
        "estCostUsd": est_cost,
        "streakMult": streak_mult,
        "note": note,
    }


async def status(cfg: dict, *, authed: bool = False) -> dict:
    """Snapshot of the executor's positions + stats for the UI."""
    env = trader.get_env()
    with db.get_db() as conn:
        open_rows = db.get_open_crypto15m(conn, env)
        recent = db.recent_crypto15m(conn, env, limit=40)
        stats = db.crypto15m_stats(conn, env)
        by_strategy = db.crypto15m_strategy_stats(conn, env)
    p_armed, p_sched = parlay_state()
    parlay_js = {"armed": p_armed, "hasSchedule": bool(p_sched)}
    if p_sched:
        try:
            parlay_js["hours"] = sorted(
                int(h) for h in (p_sched.get("hour_configs") or {})
            )
        except (TypeError, ValueError):
            parlay_js["hours"] = []
        parlay_js["generatedAt"] = p_sched.get("generated_at")
        parlay_js["staleReason"] = parlay_stale_reason(p_sched)
    return {
        "enabled": bool(cfg.get("crypto15m_enabled")),
        "trading": __import__("config").engine_on(cfg, "crypto15m_enabled") and bool(authed),
        "authed": bool(authed),
        "parlay": parlay_js,
        "haltReason": _halt_reason,
        "blockReasons": dict(_block_reasons),
        "byStrategy": by_strategy,
        "modelCalibration": dict(_CAL_CACHE),
        "orderSize": int(cfg.get("crypto15m_order_size", 1)),
        "maxConcurrent": int(cfg.get("crypto15m_max_concurrent", len(crypto15m.SERIES))),
        "sizing": await _sizing_preview(cfg, authed),
        "env": env,
        "stats": stats,
        "open": [_pos_to_js(r) for r in open_rows],
        "recent": [_pos_to_js(r) for r in recent],
    }
