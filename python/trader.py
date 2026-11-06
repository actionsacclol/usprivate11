"""Live trading engine.

Bug fixes vs the original `polymarket_trader/trader.py`:

  1. **Resolution via direct API.** The old engine relied on the
     scanner's market table to mark positions resolved. If a market
     settled and aged out of the scanner's open-markets sync, the
     position would sit in `filled` forever and (incorrectly) count
     toward MAX_OPEN_POSITIONS. We now call
     `polymarket_api.fetch_market(ticker)` for any unresolved position
     whose tracker row is missing/stale, and additionally walk
     `get_settled_positions()` once per resolution cycle as a backstop
     for markets that rotated out before we noticed.

  2. **Position reconcile on startup.** When the trader starts (or
     restarts after a crash), `reconcile_positions_with_polymarket()`
     pulls the current Polymarket position list and:
       - marks any local "open" positions Polymarket doesn't acknowledge
         as `gone` (settled/expired/canceled while we were down)
       - imports any Polymarket positions we don't have a local row for
         (rare — manual UI trades), so they don't break our exposure
         math the next cycle.

  3. **Daily P&L stop-loss / take-profit.** New gates that pause new
     entries (without flatting) when today's realized P&L crosses a
     threshold. Configurable via `stop_loss_on_day` and
     `take_profit_on_day`.

  4. **Per-env separation.** `bot_positions.network` is now part of
     the dedup keys and counters, so flipping between demo and prod
     doesn't cross-contaminate the "is this signal already traded?"
     check.

  5. **Convergence trade source.** A new `signal_source='convergence'`
     pseudo-signal is emitted by the runner and routed through the
     same execution path.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import backtest
import clob_ws
import db
import instance_lock
import rules as rules_engine
from polymarket_api import (
    OrderStatusUnknownError, PolymarketAPIError, cancel_order, cancel_orders,
    fetch_market, fetch_markets_map,
    get_activity, get_balance,
    get_fills_for_order, get_market_meta, get_order, get_quote, get_positions,
    place_limit_order,
)
from polymarket_auth import credentials_present, get_env, trading_address

logger = logging.getLogger(__name__)


# ───────── balance cache ───────────────────────────────────────────────

# Keyed by Polymarket env so a demo<->production switch can never surface the
# wrong account's balance. The old single global cache could cross them if
# a post-switch refresh raced or failed and left a stale value behind.
_balance_cache: dict[str, dict] = {}


def last_balance_read_ok(env: str | None = None) -> bool:
    """True when the most recent balance read for env actually SUCCEEDED.
    Lets sizing distinguish a genuine $0 account (real zero — skip entries,
    tell the user to deposit) from a failed read served from cache (unknown —
    fall back to the pinned bankroll). Both look like cents==0 on a cold
    start, which previously made real-zero accounts size against phantom
    start_bankroll money: ~28 rejected 'not enough balance' orders/hour of
    error spam until funds arrived."""
    cached = _balance_cache.get(env or get_env())
    return bool(cached and cached.get("ok"))
# Per-env wall-clock of the last LOGGED balance-fetch failure, so a persistent
# outage logs once every few minutes instead of on every attempt.
_balance_fail_log_at: dict[str, float] = {}
_BALANCE_FAIL_LOG_EVERY = 300.0

# A failed /value read keeps the last-known portfolio so a blip doesn't show
# open positions as a loss. Only for this long, though. Cash keeps moving
# while a held portfolio doesn't (a fill debits cash and the frozen mark never
# adds the position back), so past this the total is a number nobody should
# act on: it is not persisted as a balance point and the daily stop won't
# flatten on it.
_PORTFOLIO_STALE_SEC = 300.0


def portfolio_stale(env: str | None = None) -> bool:
    """True while /value keeps failing: the latest read of it failed and the
    last good one is over _PORTFOLIO_STALE_SEC old, or there never was one.
    False when nothing is known to be wrong (including before any read)."""
    cached = _balance_cache.get(env or get_env())
    if not cached or cached.get("port_last_ok", True):
        return False
    ok_at = cached.get("port_ok_at")
    return ok_at is None or (time.monotonic() - ok_at) >= _PORTFOLIO_STALE_SEC


def wallet_status(env: str | None = None) -> dict:
    """The freshness and extra figures behind the wallet panel. readAt is the wall
    clock of the last SUCCESSFUL read, so a failing one can't look current."""
    cached = _balance_cache.get(env or get_env()) or {}
    return {"readOk": bool(cached.get("ok")), "readAt": cached.get("read_at"),
            **(cached.get("detail") or {})}


async def refresh_balance(cfg: dict, force: bool = False) -> tuple[int, int]:
    """Returns (cash_cents, portfolio_cents) for the ACTIVE env, reading
    from a per-env cache where we can."""
    env = get_env()
    interval = float(cfg.get("balance_poll_interval", 60))
    loop = asyncio.get_event_loop()
    now = loop.time()
    cached = _balance_cache.get(env)
    if not force and cached and (now - cached["at"]) < interval:
        return cached["cents"], cached["portfolio_cents"]
    port_ok_at = cached.get("port_ok_at") if cached else None
    try:
        data = await get_balance()
        cents = int(data.get("balance", 0))
        port_raw = data.get("portfolio_value")
        if port_raw is None:
            # Portfolio read failed (None = unknown, not a real zero). Keep the
            # last-known value so `total` doesn't dip to cash-only and show open
            # positions as a phantom session loss until /value recovers — for
            # _PORTFOLIO_STALE_SEC; after that portfolio_stale() says so.
            port = cached["portfolio_cents"] if cached else 0
        else:
            port = int(port_raw)
            port_ok_at = time.monotonic()
        _balance_cache[env] = {"cents": cents, "portfolio_cents": port, "at": now, "ok": True,
                               "port_ok_at": port_ok_at, "port_last_ok": port_raw is not None,
                               "read_at": time.time(), "detail": data.get("detail")}
        if port_raw is None and portfolio_stale(env):
            wt = time.time()
            if wt - _balance_fail_log_at.get(env, 0.0) >= _BALANCE_FAIL_LOG_EVERY:
                _balance_fail_log_at[env] = wt
                logger.warning(
                    "portfolio value (/value) unreadable for over "
                    f"{_PORTFOLIO_STALE_SEC / 60:.0f} min: the balance total is "
                    "not recorded and the daily stop won't flatten until it recovers")
        return cents, port
    except Exception as e:
        # Stamp the attempt time so the interval throttle above also applies to
        # FAILURES. Without this a persistent outage (e.g. all Polygon RPCs
        # unreachable) re-attempts the read AND re-logs on EVERY caller —
        # the crypto executor tick (~every 2.4s), the copy engine, and the
        # status poll — producing the thousands-of-lines "balance fetch failed"
        # spam and pointlessly hammering the (already-down) RPCs. We serve the
        # last-known balance so sizing/display keep working through the outage.
        prev_cents = cached["cents"] if cached else 0
        prev_port = cached["portfolio_cents"] if cached else 0
        _balance_cache[env] = {"cents": prev_cents, "portfolio_cents": prev_port, "at": now, "ok": False,
                               "port_ok_at": port_ok_at, "port_last_ok": False,
                               "read_at": cached.get("read_at") if cached else None,
                               "detail": cached.get("detail") if cached else None}
        wt = time.time()
        if wt - _balance_fail_log_at.get(env, 0.0) >= _BALANCE_FAIL_LOG_EVERY:
            _balance_fail_log_at[env] = wt
            # Paper with no key is normal: there is no balance to read.
            if credentials_present():
                logger.warning(f"balance fetch failed: {e}")
        return prev_cents, prev_port


# ───────── sizing ─────────────────────────────────────────────────────


def _edge_fraction(edge_pts: float, cfg: dict) -> float:
    """0..1 size ramp: 0 at/under `sizing_base_edge`, 1 at/over `sizing_max_edge`,
    linear between. Drives both % -of-balance and fixed-contract sizing — the bot
    bets nearer Min on weak signals and nearer Max on high-conviction ones."""
    lo_e = float(cfg["sizing_base_edge"])
    hi_e = float(cfg["sizing_max_edge"])
    if edge_pts <= lo_e:
        return 0.0
    if edge_pts >= hi_e or hi_e <= lo_e:
        return 1.0
    return (edge_pts - lo_e) / (hi_e - lo_e)


def _compute_position_usd(balance_usd: float, edge_pts: float, cfg: dict) -> float:
    """% -of-balance ('percent' mode) sizing: a bankroll fraction ramped from
    `min_size_fraction` to `max_size_fraction` by edge, capped by the $ hard cap."""
    lo_f = float(cfg["min_size_fraction"])
    hi_f = float(cfg["max_size_fraction"])
    frac = lo_f + _edge_fraction(edge_pts, cfg) * (hi_f - lo_f)
    return min(balance_usd * frac, float(cfg["hard_max_position_usd"]))


def _compute_target_usd(
    balance_usd: float, edge_pts: float, limit_cents: int, cfg: dict
) -> float:
    """Mode-aware target $ for a position. 'contracts' mode ramps a share count
    from `min_contracts` to `max_contracts` by edge (priced at the order price);
    'percent' mode uses the bankroll fraction. Both are capped by the $ hard cap
    (the per-trade safety ceiling that applies regardless of mode)."""
    if (cfg.get("sizing_mode") or "percent").lower() == "contracts":
        min_c = max(1, int(cfg.get("min_contracts", 5) or 5))
        max_c = max(min_c, int(cfg.get("max_contracts", min_c) or min_c))
        contracts = round(min_c + _edge_fraction(edge_pts, cfg) * (max_c - min_c))
        usd = contracts * max(1, int(limit_cents)) / 100.0
        return min(usd, float(cfg["hard_max_position_usd"]))
    return _compute_position_usd(balance_usd, edge_pts, cfg)


# ───────── live-book quotes (websocket accelerator) ──────────────────
#
# Every order path pays a REST quote immediately before placing: a cached
# market-meta lookup plus TWO CLOB /price round-trips (~150-400 ms) on the
# hot path, for a number `clob_ws.feed` already has, stamped when WE received
# it. These helpers let the engines read that number first.
#
# STRICT ACCELERATOR. The websocket is never a second authority:
#   • the feed answers only when the book is fresh, two-sided and free of the
#     empty-book sentinels (`clob_ws.quote_cents_if_fresh`);
#   • anything else is None, and None means "pay the REST quote", which is the
#     behaviour that existed before this code;
#   • the same band checks, caps and re-checks run on the price either way, so
#     nothing enters or exits on a websocket number that would not have entered
#     or exited on the REST one.
#
# Kill switch: KRYPT_WS_QUOTES=0 puts every engine back on REST-only quotes.

_WS_QUOTES_OFF = os.environ.get("KRYPT_WS_QUOTES", "1").strip().lower() in (
    "0", "off", "false", "no",
)

# (ticker, direction) -> outcome token id. Resolved once by `warm_books` (the
# meta fetch it needs is the same cached lookup get_quote does), so the
# order-path read below is pure dict work — no await, no network.
_ws_token: dict[tuple[str, str], str] = {}
_WS_TOKEN_MAX = 400
# Cap on the meta fetches one warm pass may pay for. Warming is an
# optimisation: an unresolved market simply keeps using REST and gets warmed
# on a later pass, which is far better than putting a Gamma page-load in front
# of the trading loop the first time a big candidate list appears.
_WS_WARM_FETCH_MAX = 8


def ws_quote_cents(ticker: str, direction: str) -> Optional[dict]:
    """{'bid_cents','ask_cents'} from the live feed for a market we have
    already warmed, or None. Sync and allocation-cheap — a caller replaces
    `await get_quote(...)` with `ws_quote_cents(...) or await get_quote(...)`."""
    if _WS_QUOTES_OFF:
        return None
    try:
        token = _ws_token.get((ticker, (direction or "").lower()))
        if not token:
            return None
        q = clob_ws.quote_cents_if_fresh(token)
        if q is None:
            return None
        return {"bid_cents": q[0], "ask_cents": q[1]}
    except Exception:
        return None


async def warm_books(pairs, *, limit: int = 120) -> int:
    """Subscribe the live feed to the outcome tokens of the (ticker, direction)
    markets an engine is about to act on, so the book is already there when the
    order path asks. Returns the number of tokens observed.

    Bounded on both axes: at most `limit` markets per pass and
    `_WS_WARM_FETCH_MAX` NEW meta fetches (already-resolved markets are free
    dict hits). Never raises — warming is best-effort by construction."""
    if _WS_QUOTES_OFF:
        return 0
    tokens: list[str] = []
    to_fetch: list[tuple[str, str]] = []
    try:
        for ticker, direction in list(pairs)[:limit]:
            key = (ticker, (direction or "").lower())
            if not ticker or key[1] not in ("yes", "no"):
                continue
            tok = _ws_token.get(key)
            if tok:
                tokens.append(tok)
            elif len(to_fetch) < _WS_WARM_FETCH_MAX:
                to_fetch.append(key)
        if to_fetch:
            metas = await asyncio.gather(
                *[get_market_meta(t) for t, _d in to_fetch], return_exceptions=True,
            )
            for (ticker, direction), meta in zip(to_fetch, metas):
                if not isinstance(meta, dict):
                    continue
                tok = meta.get("yes_token") if direction == "yes" else meta.get("no_token")
                if tok:
                    _ws_token[(ticker, direction)] = str(tok)
                    tokens.append(str(tok))
            cap_dict_size(_ws_token, _WS_TOKEN_MAX)
        if tokens:
            clob_ws.warm(*tokens)
    except Exception as e:
        logger.debug(f"warm_books: {e}")
    return len(tokens)


# ───────── order pricing ─────────────────────────────────────────────


async def _compute_limit_price_cents(
    ticker: str, direction: str, signal_price_cents: int, cfg: dict,
    book: Optional[dict] = None,
) -> int:
    """Pick a limit price (in cents = USDC×100) for the (yes/no) outcome
    token. `limit_cross` crosses the spread at the token's best ask;
    `limit_mid` rests at the bid/ask midpoint; `market` pays up to 99c.

    Pass `book` to receive the quote this used ({'ask_cents', 'bid_cents'}).
    The caller needs the best ASK to know whether the price it just chose
    RESTS (maker, no fee under takerOnly fee-v2) or crosses — `limit_mid` is
    a shipped order style whose orders always rest, and every one of them was
    charged a taker fee because nothing outside this function saw the ask.
    Left untouched when no quote was read (market style, or a failed fetch)."""
    direction = direction.lower()
    style = cfg.get("order_style", "limit_cross")
    if style == "market":
        # "Pay up to your max-entry cap" — NOT a bare 99c, which the band
        # re-check in execute_signal would then reject on every trade (the
        # default cap is well under 99), silently disabling market style.
        return max(1, min(99, int(cfg.get("max_entry_price_cents", 99) or 99)))
    try:
        # Live book first (see "live-book quotes" above): when the websocket
        # has a fresh two-sided book for this token the REST quote's two
        # /price round-trips are skipped entirely. `ws_quote_cents` returns
        # None for anything less than that, and then we quote exactly as
        # before — so the price this function can choose is unchanged.
        q = ws_quote_cents(ticker, direction) or await get_quote(ticker, direction)
        ask = q.get("ask_cents")
        bid = q.get("bid_cents")
        if book is not None:
            book["ask_cents"] = ask
            book["bid_cents"] = bid
        if style == "limit_mid" and ask is not None and bid is not None:
            return max(1, min(99, (ask + bid) // 2))
        if ask is not None and 1 <= ask <= 99:
            cap = int(cfg.get("max_entry_price_cents", 99) or 99)
            if ask > cap:
                # The live ask is already above the user's max-entry cap — return
                # it so the caller's band re-check skips the trade cleanly rather
                # than resting a non-crossing order we can't afford to fill.
                return ask
            # Cross HARDER than the bare ask so we actually take liquidity that
            # may have ticked away since the quote (on thin books the bare ask
            # often just rests), bounded by the user's cap so we never overpay.
            margin = int(cfg.get("cross_spread_fallback_offset", 2) or 0)
            return max(1, min(99, cap, ask + margin))
    except Exception as e:
        logger.warning(f"quote fetch failed for {ticker}: {e}")
    fallback = signal_price_cents + int(cfg.get("cross_spread_fallback_offset", 2))
    return max(1, min(99, fallback))


def _is_maker_limit(limit_cents: int, best_ask_cents) -> int:
    """1 when a buy limit of `limit_cents` RESTS against a best ask of
    `best_ask_cents` (maker → no fee under takerOnly fee-v2), else 0.
    An unknown ask is treated as a cross: charge the fee rather than
    flatter the ledger."""
    try:
        if best_ask_cents is None:
            return 0
        return 1 if int(limit_cents) < int(best_ask_cents) else 0
    except (TypeError, ValueError):
        return 0


# ───────── signal interpretation ─────────────────────────────────────


def _signal_cost_cents(signal: dict, source: str) -> tuple[str, int]:
    if source == "whale":
        direction = (signal.get("taker_side") or "yes").lower()
        price_frac = float(signal.get("price") or 0.0)
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    if source == "convergence":
        direction = (signal.get("direction") or "yes").lower()
        price_frac = float(signal.get("price") or 0.5)
        # convergence contracts cost is taker-side
        cents = max(1, min(99, int(round(price_frac * 100))))
        return direction, cents
    direction = (signal.get("direction") or "yes").lower()
    yes_frac = float(signal.get("price") or 0.0)
    yes_cents = max(1, min(99, int(round(yes_frac * 100))))
    if direction == "yes":
        return direction, yes_cents
    return direction, max(1, min(99, 100 - yes_cents))


def _compute_edge(signal: dict, source: str) -> float:
    conf = float(signal.get("confidence") or 0.0)
    if source == "whale":
        implied = float(signal.get("price") or 0.0) * 100
    elif source == "convergence":
        implied = float(signal.get("price") or 0.5) * 100
    else:
        direction = (signal.get("direction") or "yes").lower()
        yes = float(signal.get("price") or 0.0)
        implied = (yes if direction == "yes" else (1.0 - yes)) * 100
    # Clamp implied to [5,95] to match the scanner's floor/ceiling (scanner.py):
    # the stored `confidence` is computed against a clamped implied, so an
    # un-clamped implied here produced an inconsistent edge at price extremes,
    # skewing the min-edge gate.
    implied = max(5.0, min(implied, 95.0))
    return conf - implied


# Numeric fields the main-bot rule builder can compose over (mirrors
# config.BOT_RULE_FIELDS and the Settings UI). costCents is the direction-
# adjusted entry cost (what you actually pay), so a single rule-set works
# across whale/momentum/convergence regardless of yes/no side.
def _signal_rule_values(signal: dict, source: str) -> dict:
    _, cost_cents = _signal_cost_cents(signal, source)
    return {
        "confidence": float(signal.get("confidence") or 0.0),
        "edge": _compute_edge(signal, source),
        "costCents": cost_cents,
    }


def _days_until_close(close_time: str) -> Optional[float]:
    """Days from now until a market's close/resolution time (ISO8601). Returns None
    when the time is missing or unparseable, which callers treat as 'unknown' and
    do NOT gate on."""
    if not close_time:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            return (ct - datetime.now(timezone.utc)).total_seconds() / 86400.0
        except ValueError:
            continue
    return None


def _enrich_close_time(conn, sig: dict) -> None:
    """Attach the market's close_time to a signal for the resolution-horizon gate.
    Whale/momentum signal rows don't carry it; the markets table does."""
    if sig.get("close_time"):
        return
    m = db.get_market(conn, sig.get("ticker") or "")
    if m:
        sig["close_time"] = m.get("close_time") or ""


def should_trade(signal: dict, source: str, cfg: dict) -> tuple[bool, str]:
    conf = float(signal.get("confidence") or 0.0)
    edge = _compute_edge(signal, source)
    # Custom rule-set (rule builder): the user's composed conditions REPLACE the
    # built-in confidence/edge/price-bound thresholds below. Structural gates —
    # source on/off, momentum signal-type, and category allowlists — still apply.
    use_rules = bool(cfg.get("use_rules"))

    if source == "whale":
        if not cfg.get("trade_whales", False):
            return False, "whales disabled"
        if not use_rules:
            if conf < cfg["min_confidence_whale"]:
                return False, f"conf {conf:.1f} < {cfg['min_confidence_whale']}"
            if edge < cfg["min_edge_pts_whale"]:
                return False, f"edge {edge:.1f} < {cfg['min_edge_pts_whale']}"
    elif source == "momentum":
        if not cfg.get("trade_momentum", False):
            return False, "momentum disabled"
        if not use_rules:
            if conf < cfg["min_confidence_momentum"]:
                return False, f"conf {conf:.1f} < {cfg['min_confidence_momentum']}"
            if edge < cfg["min_edge_pts_momentum"]:
                return False, f"edge {edge:.1f} < {cfg['min_edge_pts_momentum']}"
        sig_type = (signal.get("signal_type") or "")
        allowed = set(cfg.get("allowed_momentum_signal_types", []))
        if sig_type not in allowed:
            return False, f"signal_type {sig_type!r} not allowed"
    elif source == "convergence":
        if not cfg.get("trade_convergence", False):
            return False, "convergence disabled"
        if not use_rules and conf < cfg["min_confidence_whale"]:
            return False, f"conf {conf:.1f} < {cfg['min_confidence_whale']}"

    # allowed_categories semantics:
    #   None        → trade all categories (no filter)
    #   list[str]   → only trade signals whose category is in the list
    #   []          → trade nothing (user explicitly selected zero categories)
    cat = (signal.get("category") or "").lower()
    allowed_cats = cfg.get("allowed_categories")
    if allowed_cats is not None:
        if not allowed_cats:
            return False, "no categories enabled"
        if cat not in {c.lower() for c in allowed_cats}:
            return False, f"category {cat!r} not in allowed set"

    # Per-source category allowlist (composes with the global filter via
    # AND). Lets one config run, e.g., whales only in crypto while
    # momentum only in sports — the backtested combined edge.
    src_key = {
        "whale": "allowed_whale_categories",
        "convergence": "allowed_whale_categories",
        "momentum": "allowed_momentum_categories",
    }.get(source)
    src_cats = cfg.get(src_key) if src_key else None
    if src_cats is not None:
        if not src_cats:
            return False, f"no {source} categories enabled"
        if cat not in {c.lower() for c in src_cats}:
            return False, f"category {cat!r} not in {source} set"

    # Resolution-horizon cap (structural — applies even in rules mode, like the
    # source/category gates above): skip markets that won't resolve for a long
    # time (e.g. multi-month politics markets tie up capital). 0 = off. close_time
    # is enriched onto the signal in scan_for_trades; missing/unparseable ->
    # not gated (fail-open — we'd rather trade than wrongly block on bad data).
    max_res_days = int(cfg.get("max_resolution_days", 0) or 0)
    if max_res_days > 0:
        days = _days_until_close(signal.get("close_time") or "")
        if days is not None and days > max_res_days:
            return False, f"resolves in ~{days:.0f}d > max {max_res_days}d"

    # Quality gate: either the user's composed rules, or the built-in
    # price-bound + confidence/edge thresholds applied above.
    if use_rules:
        return rules_engine.evaluate_rules(
            _signal_rule_values(signal, source), cfg.get("rules") or [])

    _, cost_cents = _signal_cost_cents(signal, source)
    if cost_cents < cfg["min_entry_price_cents"]:
        return False, f"entry {cost_cents}c < {cfg['min_entry_price_cents']}c"
    if cost_cents > cfg["max_entry_price_cents"]:
        return False, f"entry {cost_cents}c > {cfg['max_entry_price_cents']}c"
    return True, "ok"


# ───────── daily risk gate ──────────────────────────────────────────


def _today_pnl_balance_delta(env: str) -> float | None:
    """Today's P&L = current total balance − total balance at the
    first snapshot of today (UTC). This is the SAME number the
    dashboard's "Today P&L" tile shows, so the daily stop-loss /
    take-profit gates can never disagree with what the user sees.

    Returns None if there's no baseline yet (very first tick of the
    day) — caller should treat that as "unknown" and not gate.

    The previous implementation summed `pnl_usd` from `bot_positions`,
    which inherited any per-trade attribution bug and would falsely
    trigger the stop-loss when old/imported rows had garbage P&L
    (e.g. seeing −$4210 when actual session loss was −$5).
    """
    with db.get_db() as conn:
        first_today = db.first_snapshot_of_today(conn, env)
        if not first_today:
            return None
        # latest_snapshot is written by the periodic balance heartbeat,
        # so it's at most ~60s stale — fine precision for a daily stop.
        latest = db.latest_snapshot(conn, env)
        transfers = db.transfer_adjustment_today(conn, env)
    if not latest:
        return None
    today_baseline = float(first_today["total_usd"] or 0.0)
    today_total = float(latest["total_usd"] or 0.0)
    # Net out detected deposits/withdrawals: money moved in or out is not
    # trading P&L and must neither trip the daily stop nor mask real losses.
    return today_total - today_baseline - transfers


# A daily-limit breach must PERSIST before it gates. The balance heartbeat
# reads cash and position value at different moments, so around every
# settlement/exit the payout is in flight between ledgers for a minute or
# two and the account total transiently reads low (observed live 2026-07-03
# on the Kalshi sibling: "today pnl=$-9.67" inside a settlement gap on an
# account that was UP on the day — the stop blocked entries for ~2 minutes
# after every settlement). A genuine drawdown keeps breaching and gates
# ~3 minutes later; a money-in-flight dip self-heals first.
_DAY_RISK_PERSIST_SEC = 180.0
# (env, kind) -> monotonic first-breach. Keyed by env so an env switch can't
# transfer or destroy a live breach streak.
_day_risk_breach: dict = {}


def _breach_persists(env: str, kind: str, breached: bool) -> bool:
    now = time.monotonic()
    key = (env, kind)
    if not breached:
        _day_risk_breach[key] = None
        return False
    if _day_risk_breach.get(key) is None:
        _day_risk_breach[key] = now
    return (now - _day_risk_breach[key]) >= _DAY_RISK_PERSIST_SEC


def _is_blocked_by_daily_risk(cfg: dict, env: str) -> tuple[bool, str]:
    pnl = _today_pnl_balance_delta(env)
    if pnl is None:
        return False, ""
    sl = float(cfg.get("stop_loss_on_day", 0))
    tp = float(cfg.get("take_profit_on_day", 0))
    sl_hit = _breach_persists(env, "sl", sl < 0 and pnl <= sl)
    tp_hit = _breach_persists(env, "tp", tp > 0 and pnl >= tp)
    if sl_hit:
        return True, f"daily stop-loss hit (today pnl=${pnl:+.2f}, limit=${sl:+.2f})"
    if tp_hit:
        return True, f"daily take-profit hit (today pnl=${pnl:+.2f}, target=${tp:+.2f})"
    return False, ""


def _lifetime_loss_tripped_main(cfg: dict, env: str) -> tuple[bool, str]:
    """Durable cumulative realized-loss breaker for the MAIN (whale/momentum)
    engine, mirroring crypto15m/copy. The daily stop re-arms every UTC
    midnight, so a net-negative engine could bleed the daily cap forever
    with no ceiling — this was the one engine without a lifetime stop. Uses
    the wipe-surviving banked counter (db.lifetime_realized_pnl), scoped to
    whale/momentum rows only (external/copy rows are other engines' P&L).
    Trips at the absolute-$ override (if > 0) or the configured fraction of
    the starting bankroll. Returns (tripped, human reason)."""
    limit_usd = float(cfg.get("lifetime_loss_limit_usd") or 0.0)
    try:
        limit_pct = max(0.0, min(1.0, float(cfg.get("lifetime_loss_limit_pct", 0.0) or 0.0)))
    except (TypeError, ValueError):
        limit_pct = 0.0
    if limit_usd <= 0 and limit_pct <= 0:
        return False, ""
    with db.get_db() as conn:
        lifetime = db.lifetime_realized_pnl(conn, "main", env)
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
            f"main-engine lifetime loss limit reached — new entries paused; "
            f"adjust settings to resume (cumulative ${lifetime:+.2f}, "
            f"loss ${loss:.2f} >= cap ${cap:.2f})"
        )
    return False, ""


async def _maybe_flatten_on_daily_stop(cfg: dict, env: str) -> None:
    """When the daily STOP-LOSS (not the take-profit) has tripped and the user
    turned on `flatten_on_daily_stop`, market-sell every open position once to
    hard-cap the day's loss. Without this the daily stop only halts NEW entries
    while open positions keep running, so realized loss can blow past the limit
    the user set. No-op when: the toggle is off, trading is paused, it's a
    take-profit halt, or nothing is open (so it can't re-flatten every tick)."""
    from config import engine_on
    if not cfg.get("flatten_on_daily_stop") or not engine_on(cfg, "enable_trading"):
        return
    if portfolio_stale(env):
        # The day's P&L can't be known without the portfolio, and selling the
        # whole book is the one daily-risk action that can't be taken back.
        return
    pnl = _today_pnl_balance_delta(env)
    if pnl is None:
        return
    sl = float(cfg.get("stop_loss_on_day", 0))
    # Same persistence rule as the entry gate — flattening is the most
    # destructive daily-risk action, so a money-in-flight dip must never
    # market-sell the book.
    if not _breach_persists(env, "sl", sl < 0 and pnl <= sl):
        return  # only a PERSISTENT loss breach flattens; take-profit just halts new entries
    with db.get_db() as conn:
        n = conn.execute(
            """SELECT COUNT(*) FROM bot_positions
               WHERE resolved=0 AND status IN ('submitted','partial','filled')
                 AND network=?""",
            (env,),
        ).fetchone()[0]
    if not n:
        return  # already flat — nothing to do (and don't re-run cancel-all/sell)
    logger.warning(
        f"[daily-stop] flatten_on_daily_stop ON and loss limit hit "
        f"(today ${pnl:+.2f} <= ${sl:+.2f}) — closing {n} open position(s)"
    )
    try:
        res = await flatten_open_positions(cfg)
        logger.warning(f"[daily-stop] flatten result: {res}")
    except Exception as e:
        logger.error(f"[daily-stop] flatten failed: {e}", exc_info=True)


_DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _is_blocked_by_trading_hours(cfg: dict) -> tuple[bool, str]:
    """If trading_hours_enabled is True, only allow trading inside the
    configured weekly window. Outside it, scan_for_trades short-circuits."""
    if not cfg.get("trading_hours_enabled"):
        return False, ""
    from datetime import datetime, timedelta, timezone
    offset_min = int(cfg.get("trading_timezone_offset_min", 0) or 0)
    local_now = datetime.now(timezone.utc) + timedelta(minutes=offset_min)
    days = [d.lower() for d in (cfg.get("trading_days") or [])]
    weekday_short = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][local_now.weekday()]
    if weekday_short not in days:
        return True, f"trading-hours: {weekday_short} not in active days"
    try:
        s_h, s_m = (int(x) for x in str(cfg.get("trading_hours_start", "00:00")).split(":"))
        e_h, e_m = (int(x) for x in str(cfg.get("trading_hours_end", "23:59")).split(":"))
    except (ValueError, AttributeError):
        return False, ""
    cur_min = local_now.hour * 60 + local_now.minute
    start_min = s_h * 60 + s_m
    end_min = e_h * 60 + e_m
    in_window = (
        (start_min <= end_min and start_min <= cur_min <= end_min)
        # support overnight windows (e.g. 22:00 → 04:00)
        or (start_min > end_min and (cur_min >= start_min or cur_min <= end_min))
    )
    if not in_window:
        return True, (
            f"trading-hours: outside window "
            f"({local_now.strftime('%H:%M')} not in "
            f"{s_h:02d}:{s_m:02d}-{e_h:02d}:{e_m:02d})"
        )
    return False, ""


# ───────── execution ─────────────────────────────────────────────────


def entry_cap_block(
    cfg: dict, *,
    open_count: Callable[[], int],
    today_count: Callable[[], int],
    event_count: Callable[[], int],
    market_side_open: Callable[[], bool],
) -> Optional[str]:
    """The concurrency caps every main-engine entry must clear, in the order
    the engine checks them: max open, daily new, per event, one per
    market/side. Returns the skip reason, or None when the entry may go ahead.

    The counts are passed as callables so the live path keeps its lazy DB
    queries (the daily count is never read with "unlimited daily" on), and
    the backtest replay (replay.replay_main) can pass counts from its own
    simulated book. One gate for both, so the replay cannot trade what the
    engine would refuse."""
    n_open = open_count()
    if n_open >= cfg["max_open_positions"]:
        return f"MAX_OPEN_POSITIONS ({n_open}/{cfg['max_open_positions']} open)"
    # Honor the "unlimited daily" escape hatch — when on, the only
    # cap on per-day entries is `max_open_positions`. Useful for
    # users running aggressive presets who don't want to babysit
    # the counter all day.
    if not cfg.get("unlimited_daily_new_positions"):
        n_today = today_count()
        daily_cap = int(cfg["max_daily_new_positions"])
        if n_today >= daily_cap:
            return (
                f"MAX_DAILY_NEW_POSITIONS ({n_today}/{daily_cap} today). "
                f"Toggle 'Unlimited daily new positions' in Settings → "
                f"Concurrency to disable this cap."
            )
    per_event_cap = int(cfg.get("max_positions_per_event", 1) or 1)
    if event_count() >= per_event_cap:
        return f"event already at its max ({per_event_cap}) position(s)"
    if market_side_open():
        return "market/side already open"
    return None


async def execute_signal(
    signal: dict, source: str, cfg: dict, balance_usd: float
) -> dict | None:
    """Place an order for a single signal. Returns the inserted bot_positions
    row dict on success (or dry-run), or None on skip/error.

    Serialised against the copy / crypto15m / script engines by `order_gate`:
    this whole function IS the check+place critical section — it reads the
    open-count, daily, per-event and exposure caps from the DB at the top and
    inserts the position row at the bottom, so another engine placing in
    between is exactly the race the gate exists to stop."""
    async with order_gate("main"):
        return await _execute_signal_locked(signal, source, cfg, balance_usd)


async def _execute_signal_locked(
    signal: dict, source: str, cfg: dict, balance_usd: float
) -> dict | None:
    direction, signal_cost_cents = _signal_cost_cents(signal, source)
    edge_pts = _compute_edge(signal, source)
    env = get_env()

    with db.get_db() as conn:
        blocked = entry_cap_block(
            cfg,
            open_count=lambda: db.count_open_bot_positions(conn, env),
            today_count=lambda: db.count_new_positions_today(conn, env),
            event_count=lambda: db.count_positions_in_event(
                conn, signal.get("event_ticker") or "", env),
            market_side_open=lambda: db.exists_position_in_market(
                conn, signal["ticker"], direction, env),
        )
        if blocked:
            logger.info(f"[skip] {signal['ticker']}: {blocked}")
            return None
        exposure = db.current_total_exposure_usd(conn, env)
        filled_exposure = db.current_filled_exposure_usd(conn, env)

    # Price first — 'contracts' sizing (and the $ risk caps) need it to convert
    # between contracts and dollars.
    submit_book: dict = {}
    limit_cents = await _compute_limit_price_cents(
        signal["ticker"], direction, signal_cost_cents, cfg, submit_book
    )
    # Re-apply the entry-price ceiling to the ACTUAL order price. should_trade()
    # gated the *signal* price, but the book can move between signal and
    # placement and limit_cross crosses the live spread — so the price we'd
    # actually pay can drift above the user's max. The max cap always applies
    # (never overpay). The MIN is the user's built-in band; custom rules replace
    # it, so a rules user can intentionally enter a cheap longshot.
    hi = int(cfg["max_entry_price_cents"])
    lo = 1 if bool(cfg.get("use_rules")) else int(cfg["min_entry_price_cents"])
    if limit_cents > hi:
        logger.info(
            f"[skip] {signal['ticker']}: order price {limit_cents}c above your "
            f"max-entry cap {hi}c (favorite ran past the cap since the signal)"
        )
        return None
    if limit_cents < lo:
        logger.info(
            f"[skip] {signal['ticker']}: order price {limit_cents}c below your "
            f"min-entry {lo}c (book moved since signal)"
        )
        return None

    # Size and the exposure/reserve caps are based on TOTAL bankroll, counting
    # each dollar ONCE: cash + the cost of positions that actually FILLED (that
    # money has already left the wallet). We deliberately do NOT add the notional
    # of still-resting orders: on Polymarket a resting limit order doesn't lock
    # wallet USDC, so that cash is still in `balance_usd` — adding it again would
    # let a burst of unfilled orders inflate the denominator and ratchet exposure
    # up to f/(1-f)x the account (3x at 0.75, ~5.7x at 0.85). Bankroll still stays
    # stable as positions fill (cash drops, filled_exposure rises by the same $),
    # so "6% per trade" / "75% max exposure" keep meaning % of the whole account —
    # not of a shrinking cash balance, which made every % silently undershoot.
    pending_notional = max(0.0, exposure - filled_exposure)
    total_bankroll = max(0.0, balance_usd) + max(0.0, filled_exposure)
    target_usd = _compute_target_usd(total_bankroll, edge_pts, limit_cents, cfg)
    max_exposure = total_bankroll * float(cfg["max_total_exposure_fraction"])
    # Subtract TOTAL committed (filled + resting) from the cap headroom, so
    # resting orders still count against the ceiling even though they're not in
    # the denominator.
    target_usd = min(target_usd, max(0.0, max_exposure - exposure))
    # Keep `reserve` (a fraction of total bankroll) as CASH. Spendable cash is the
    # wallet balance LESS what's already earmarked for resting orders (whose USDC
    # hasn't moved yet) LESS the reserve. (Filled cost has already left the wallet,
    # so it's not subtracted again — that's baked into balance_usd.)
    reserve = total_bankroll * float(cfg["min_cash_reserve_fraction"])
    spendable_cash = max(0.0, balance_usd) - pending_notional
    target_usd = min(target_usd, max(0.0, spendable_cash - reserve))
    # The absolute most we'd spend on this position across ALL risk limits —
    # used below to decide whether bumping to a market's minimum size is safe.
    risk_ceiling_usd = min(
        float(cfg["hard_max_position_usd"]),
        max(0.0, max_exposure - exposure),
        max(0.0, spendable_cash - reserve),
    )

    if target_usd < 1.0:
        logger.info(f"[skip] {signal['ticker']}: size ${target_usd:.2f} < $1")
        return None

    contracts = max(1, int(target_usd * 100 // limit_cents))

    # Polymarket enforces a per-market MINIMUM order size (usually 5 shares).
    # On a small bankroll the fractional sizing can land under it; rather than
    # place a doomed sub-min order (the CLOB rejects it → a stuck 'error' row,
    # and the signal misleadingly shows "traded"), bump UP to the minimum when
    # it still fits the risk budget, otherwise skip cleanly.
    min_size = 0
    try:
        meta = await get_market_meta(signal["ticker"])
        if meta:
            min_size = int(math.ceil(float(meta.get("min_size") or 0)))
    except Exception as e:
        logger.debug(f"min_size lookup failed for {signal['ticker']}: {e}")
    if min_size > contracts:
        bumped_cost = min_size * limit_cents / 100.0
        if bumped_cost <= risk_ceiling_usd + 1e-9:
            logger.info(
                f"[{source}] {signal['ticker']}: sizing {contracts}->{min_size} "
                f"to meet market minimum (${bumped_cost:.2f})"
            )
            contracts = min_size
        else:
            logger.info(
                f"[skip] {signal['ticker']}: market minimum {min_size} @ "
                f"{limit_cents}c = ${bumped_cost:.2f} exceeds risk budget "
                f"${risk_ceiling_usd:.2f}"
            )
            return None

    expected_cost_usd = contracts * limit_cents / 100.0
    client_order_id = f"krypt-{source}-{signal['id']}-{uuid.uuid4().hex[:8]}"

    logger.info(
        f"[{source}] {signal['ticker']} {direction} x{contracts} @ {limit_cents}c "
        f"= ${expected_cost_usd:.2f} conf={signal.get('confidence',0):.1f} edge={edge_pts:.1f}"
    )

    row = {
        "signal_source": source,
        "signal_id": signal["id"],
        "ticker": signal["ticker"],
        "event_ticker": signal.get("event_ticker", ""),
        "title": signal.get("title", ""),
        "category": signal.get("category", ""),
        "direction": direction,
        "action": "buy",
        "target_contracts": contracts,
        "limit_price_cents": limit_cents,
        "filled_contracts": 0,
        "cost_usd": 0.0,
        "client_order_id": client_order_id,
        "order_id": None,
        "status": "submitted",
        "confidence": signal.get("confidence", 0.0),
        "edge_pts": edge_pts,
        "signal_price": (signal.get("price") or 0.0) * 100,
        "balance_before_usd": balance_usd,
        "network": env,
        # Fee accounting. A limit strictly BELOW the best ask at submit rests
        # on the book, and a maker fill pays nothing under takerOnly fee-v2.
        # `order_style='limit_mid'` (a shipped option) always rests, yet
        # _position_fee_usd's entry_maker branch was dead code because nothing
        # here ever wrote the column — every mid-rested fill was charged a
        # taker fee it never paid, straight into pnl_usd and the loss
        # breakers. Unknown book (market style, failed quote) → taker, which
        # over-books the fee rather than flattering P&L.
        "entry_maker": _is_maker_limit(limit_cents, submit_book.get("ask_cents")),
    }

    # Defensive: the scan loop already returns early when trading is off, so
    # we should never reach here un-armed — but never record a phantom order if
    # we somehow do (there is no paper/dry-run mode). A Paper ACCOUNT is the
    # switch off: the engine sends nothing (config.engine_on).
    from config import engine_on
    if not engine_on(cfg, "enable_trading"):
        return None

    try:
        resp = await place_limit_order(
            ticker=signal["ticker"],
            side=direction,
            action="buy",
            count=contracts,
            price_cents=limit_cents,
            client_order_id=client_order_id,
        )
    except OrderStatusUnknownError as e:
        # Sent, but neither the reply nor a lookup says what became of it.
        # Keep its id on a submitted row: the order poll reads the venue (and
        # the wallet on a 404) instead of an error row forgetting a live order.
        logger.warning(f"[ORDER-UNKNOWN] {signal['ticker']}: {e}")
        resp = e.response
    except PolymarketAPIError as e:
        row["status"] = "error"
        row["error"] = f"HTTP {e.status}: {str(e.body)[:200]}"
        logger.error(f"[ORDER-FAIL] {signal['ticker']}: {row['error']}")
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            db.log_event(conn, pid, "error", note=row["error"])
            return db.fetch_position_by_id(conn, pid)
    except Exception as e:
        row["status"] = "error"
        row["error"] = f"{type(e).__name__}: {e}"
        logger.error(f"[ORDER-EXC] {signal['ticker']}: {row['error']}")
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            db.log_event(conn, pid, "error", note=row["error"])
            return db.fetch_position_by_id(conn, pid)

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    order_id = order.get("order_id") if isinstance(order, dict) else None
    row["order_id"] = order_id
    # The exchange's own answer overrides the pre-submit book: a `matched`
    # order crossed, whatever the quote said a moment earlier.
    if str((order.get("status") if isinstance(order, dict) else "") or "").lower() == "matched":
        row["entry_maker"] = 0

    with db.get_db() as conn:
        pid = db.insert_bot_position(conn, row)
        db.log_event(
            conn, pid, "placed", order_status=order.get("status"),
            note=f"order_id={order_id}",
        )
        return db.fetch_position_by_id(conn, pid)


# ───────── cross-engine order gate ──────────────────────────────────
#
# The main, copy, crypto15m and script engines now tick on SEPARATE tasks, so
# two of them can be inside their "check the shared caps, then place" region at
# the same instant. Every wallet-wide cap they honour — `max_total_exposure_fraction`,
# `min_cash_reserve_fraction`, the $5 funding halt, the per-market/per-side dedupe —
# is read from the DB (or a per-tick snapshot) and only becomes visible to the
# OTHER engines once the position row is inserted. Interleaved, both engines can
# read the same headroom and both place against it.
#
# ORDER_LOCK serialises exactly that region. Rules:
#   • ENTRIES only. Exits, stop-losses, take-profits and settlement must never
#     wait behind an entry — an exit that is late costs real money.
#   • Held across "cap check → place_limit_order → insert the row", never across
#     a fill poll, a sleep, or a manage pass. The longest thing inside is one
#     order POST plus (main engine) a price/min-size lookup.
#   • Re-entrant by task is NOT needed: no engine calls another's entry path.
ORDER_LOCK = asyncio.Lock()

# The lock must never be able to wedge every engine: if some call inside a
# holder's critical section hangs past this, waiters give up and proceed
# unserialised (which is exactly today's behaviour) rather than stop trading.
ORDER_LOCK_TIMEOUT_SEC = 30.0


@contextlib.asynccontextmanager
async def order_gate(engine: str = ""):
    """Serialise one engine's cap-check + order placement against the others.

    Yields True when it actually holds the lock, False when it gave up waiting.

    Fail-OPEN: a timeout waiting for the lock logs and runs the body anyway.
    Serialising entries is a safety improvement, not a precondition for
    trading, and a lock that could block placement forever would be worse than
    the race it fixes."""
    global ORDER_LOCK
    lock = ORDER_LOCK
    try:
        await asyncio.wait_for(lock.acquire(), timeout=ORDER_LOCK_TIMEOUT_SEC)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            f"[order-gate] {engine or 'engine'}: waited "
            f"{ORDER_LOCK_TIMEOUT_SEC:.0f}s for the cross-engine order lock — "
            "proceeding unserialised"
        )
        yield False
        return
    except RuntimeError:
        # An asyncio.Lock binds to the event loop the first time it is
        # CONTENDED, and refuses a different one afterwards. The service runs
        # one loop for the life of the process, but a re-entered asyncio.run
        # (or a test driving the loop in its own) would otherwise turn this
        # into a hard failure on the order path. Rebuild and carry on rather
        # than lose the order.
        logger.debug("[order-gate] lock bound to a stale event loop — rebuilding")
        ORDER_LOCK = lock = asyncio.Lock()
        await lock.acquire()
    try:
        yield True
    finally:
        try:
            lock.release()
        except RuntimeError:      # pragma: no cover - released twice
            pass


def shared_headroom_usd(cfg: dict, env: str, balance_usd: float) -> float:
    """Dollars this account may still commit under the caps EVERY engine shares
    (`max_total_exposure_fraction` + `min_cash_reserve_fraction`), read fresh
    from the DB.

    Same arithmetic as `execute_signal` / `crypto15m_trader.run_tick` compute for themselves — the point is that this
    one reads exposure at the moment of placement (inside `order_gate`), so an
    order another engine inserted a millisecond ago is already counted. Callers
    that snapshot their budget once per tick use it as a final re-check."""
    with db.get_db() as conn:
        exposure = db.current_total_exposure_usd(conn, env)
        filled_exposure = db.current_filled_exposure_usd(conn, env)
    pending_notional = max(0.0, exposure - filled_exposure)
    total_bankroll = max(0.0, balance_usd) + max(0.0, filled_exposure)
    reserve = total_bankroll * float(cfg.get("min_cash_reserve_fraction", 0.0) or 0.0)
    max_exposure = total_bankroll * float(
        cfg.get("max_total_exposure_fraction", 1.0) or 1.0
    )
    spendable_cash = max(0.0, balance_usd) - pending_notional
    return min(
        max(0.0, spendable_cash - reserve),
        max(0.0, max_exposure - exposure),
    )


def headroom_blocks(cfg: dict, env: str, balance_usd: float, cost_usd: float) -> str:
    """"" if `cost_usd` still fits the wallet-wide caps, else the reason it
    doesn't. Call it INSIDE `order_gate`, immediately before placing.

    Skipped when `balance_usd <= 0`: every engine treats a zero/unknown balance
    as "don't clamp" (a failed read must not look like an empty wallet), and
    this guard must not be the one place that changes that."""
    if balance_usd <= 0 or cost_usd <= 0:
        return ""
    try:
        headroom = shared_headroom_usd(cfg, env, balance_usd)
    except Exception as e:      # pragma: no cover - DB hiccup: fail open
        logger.debug(f"[order-gate] headroom re-check failed: {e}")
        return ""
    if cost_usd <= headroom + 1e-9:
        return ""
    return (
        f"account-wide cap: ${cost_usd:.2f} exceeds the ${headroom:.2f} still "
        f"available across all engines (exposure/reserve caps)"
    )


# ───────── main scan cycle ──────────────────────────────────────────


def can_open_new_entries(env: str) -> tuple[bool, str]:
    """Wallet mutual exclusion shared by ALL real-money ENTRY paths (main scan,
    crypto15m, copy). Claims the wallet by trading address; returns (False, reason)
    when a live sibling instance holds it, so a second backend on the same key
    backs off opening. Within one process all engines share instance_lock's holder
    id, so they never self-contend. Fail-OPEN (True) on any error so a lock glitch
    never blocks trading. Gate ENTRIES only — never the manage/exit paths."""
    try:
        addr = trading_address(env)
    except Exception:
        return True, ""
    if not addr:
        return True, ""
    if instance_lock.claim(addr):
        return True, ""
    other = instance_lock.foreign_holder(addr)
    return False, (
        f"account {addr[:6]}…{addr[-4:]} is already being traded by another "
        f"instance of this app (pid {other}); pausing new entries to avoid double orders."
    )


async def scan_for_trades(cfg: dict) -> list[dict]:
    """Single trading scan. Returns list of newly inserted/updated rows.

    Every silent-skip path now emits an INFO log the first time per
    minute it fires. Without this, the in-app log gives the user no
    explanation why the bot is sitting idle even though new signals
    are coming in — which was the bug behind "set max trades to 300
    but bot still won't place a trade".
    """
    global _last_scan_skip_log
    now_ts = time.time()

    def _skip_log(reason: str) -> None:
        last_cycle["skipReason"] = reason
        last_cycle["at"] = time.time()
        # Throttle to once per minute per reason so we don't spam logs.
        # Dedup on the stable prefix — reasons embed live values ("today
        # pnl=$-51.37") that change every snapshot, which defeats the dedup
        # and spams thousands of identical lines per blocked day (rotating
        # real errors out of the log window).
        key = reason.split(" (", 1)[0]
        last = _last_scan_skip_log.get(key, 0)
        if now_ts - last < 60:
            return
        _last_scan_skip_log[key] = now_ts
        logger.info(f"[skip-cycle] {reason}")

    if not cfg.get("enable_trading"):
        _skip_log("enable_trading=false (main-bot trading off in Settings)")
        return []
    from config import account_live
    if not account_live(cfg):
        _skip_log("account is in Paper (engines send nothing until it is Live)")
        return []

    env = get_env()
    blocked, reason = _is_blocked_by_daily_risk(cfg, env)
    if blocked:
        await _maybe_flatten_on_daily_stop(cfg, env)
        _skip_log(reason)
        return []
    tripped, reason = _lifetime_loss_tripped_main(cfg, env)
    if tripped:
        _skip_log(reason)
        return []
    # Wallet mutual exclusion (shared with the crypto15m + copy engines via
    # can_open_new_entries): if the SAME wallet is being traded by another
    # account/instance, only one backend opens new positions. Gate ENTRIES only.
    ok, reason = can_open_new_entries(env)
    if not ok:
        _skip_log(reason)
        return []
    blocked, reason = _is_blocked_by_trading_hours(cfg)
    if blocked:
        _skip_log(reason)
        return []

    cents, _ = await refresh_balance(cfg)
    balance_usd = cents / 100.0
    if balance_usd < 5.0:
        _skip_log(f"balance ${balance_usd:.2f} too low")
        return []

    candidates: list[tuple[dict, str]] = []
    fetched_w = fetched_m = 0
    # Only pay for the per-signal market lookup (close_time) when the resolution
    # cap is actually enabled.
    gate_resolution = int(cfg.get("max_resolution_days", 0) or 0) > 0
    with db.get_db() as conn:
        # In rules mode the custom rules REPLACE the built-in confidence/edge/price
        # thresholds (see should_trade). If we still passed the built-in
        # min_confidence here it would pre-filter candidates at the SQL level
        # BEFORE the rule engine ever sees them — so a user whose rules accept
        # low-confidence longshots would get "the bot never trades". Feed the full
        # signal set through in rules mode and let the rules gate it.
        use_rules = bool(cfg.get("use_rules"))
        if cfg.get("trade_whales"):
            seen = db.already_traded_signal_ids(conn, "whale", env)
            for sig in db.fetch_tradeable_whale_signals(
                conn,
                min_confidence=0.0 if use_rules else float(cfg["min_confidence_whale"]),
                max_age_sec=int(cfg["max_signal_age_sec"]),
                seen_ids=seen,
            ):
                fetched_w += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "whale"))
        if cfg.get("trade_momentum"):
            seen = db.already_traded_signal_ids(conn, "momentum", env)
            for sig in db.fetch_tradeable_momentum_signals(
                conn,
                min_confidence=0.0 if use_rules else float(cfg["min_confidence_momentum"]),
                max_age_sec=int(cfg["max_signal_age_sec"]),
                allowed_types=list(cfg.get("allowed_momentum_signal_types", [])),
                seen_ids=seen,
            ):
                fetched_m += 1
                if gate_resolution:
                    _enrich_close_time(conn, sig)
                candidates.append((sig, "momentum"))

    if not candidates:
        # Tell the user *why* we have nothing to trade. Most common
        # cases: both source toggles off, or the signal min_confidence
        # is set higher than anything coming through.
        bits = []
        if not cfg.get("trade_whales"):
            bits.append("whales OFF")
        if not cfg.get("trade_momentum"):
            bits.append("momentum OFF")
        if not bits:
            bits.append(
                f"no fresh signals match thresholds "
                f"(whale min_conf={cfg.get('min_confidence_whale')}, "
                f"momentum min_conf={cfg.get('min_confidence_momentum')}, "
                f"max_age={cfg.get('max_signal_age_sec')}s)"
            )
        _skip_log("no candidates: " + ", ".join(bits))
        return []

    candidates.sort(key=lambda c: _compute_edge(c[0], c[1]), reverse=True)
    # Warm the live book for the markets this cycle may order in, best edge
    # first. A signal stays a candidate until it ages out, so the book is
    # normally already live by the time one clears should_trade — and then the
    # order path prices off the feed instead of paying two /price legs. Bounded
    # and best-effort: an unwarmed market just quotes over REST as it always did.
    await warm_books(
        (sig["ticker"], _signal_cost_cents(sig, src)[0]) for sig, src in candidates
    )
    inserted: list[dict] = []
    filter_counts: dict[str, int] = {}
    entries_placed = 0
    for sig, src in candidates:
        ok, why = should_trade(sig, src, cfg)
        if not ok:
            # First rejection per (signal_id, source) is INFO so the
            # user can see why a fresh candidate was dropped.
            # Subsequent identical rejections within max_signal_age_sec
            # would otherwise re-log every 20s for the same signal
            # (since the signal stays in the candidate list until it
            # ages out) — that's the noise the user reported.
            key = (int(sig.get("id") or 0), src)
            last = _last_filter_log.get(key, 0.0)
            if (now_ts - last) >= float(cfg.get("max_signal_age_sec", 120)):
                logger.info(f"[filter] {sig['ticker']} {src}: {why}")
                _last_filter_log[key] = now_ts
                cap_dict_size(_last_filter_log)
            filter_counts[why] = filter_counts.get(why, 0) + 1
            continue
        try:
            row = await execute_signal(sig, src, cfg, balance_usd)
            if row:
                inserted.append(row)
        except Exception as e:
            row = None
            logger.error(f"[exec-fail] {sig['ticker']} {src}: {e}", exc_info=True)
        # Track the cash we just committed LOCALLY instead of forcing a fresh
        # balance read after every single entry — get_balance is two network
        # legs (pUSD + /value), so a 10-candidate cycle paid ~10 × 300-700 ms
        # purely to re-learn a number we already know. Same decrement the copy
        # engine uses; a status="error" row placed nothing, so it costs nothing.
        if row and (row.get("status") or "") in ("submitted", "filled", "partial"):
            committed = float(row.get("cost_usd") or 0.0)
            if committed <= 0:
                committed = (int(row.get("target_contracts") or 0)
                             * int(row.get("limit_price_cents") or 0) / 100.0)
            balance_usd = max(0.0, balance_usd - committed)
            entries_placed += 1
        # The halt now fires off the decremented figure — within one order of
        # the venue's own number, and it still stops the cycle dead.
        if balance_usd < 5.0:
            logger.info("[halt-cycle] balance now below $5")
            break

    # One authoritative read at the END of the cycle instead of one per entry:
    # re-syncs the cache (fees, partial fills, anything else that moved) so the
    # next cycle and the other engines start from the venue's real number.
    if entries_placed:
        await refresh_balance(cfg, force=True)

    # `inserted` also holds ORDER-FAIL rows — execute_signal records a
    # status="error" position so the rejection is visible in the UI/DB. Count
    # those SEPARATELY: a CLOB-rejected order was NOT placed. Lumping them into
    # "placed=N" made a wall of rejected orders (e.g. a signer/api-key
    # mismatch that fails every single order) read as successful trades on the
    # dashboard — the exact thing that hid 946 failed orders from a user.
    failed = sum(1 for r in inserted if (r.get("status") or "") == "error")
    placed = len(inserted) - failed
    last_cycle.update({
        "skipReason": None, "filterCounts": dict(filter_counts),
        "candidates": len(candidates), "placed": placed, "at": time.time(),
    })

    # Cycle summary so the user can see at a glance what happened. We
    # only emit it when there was *something* to consider — silent when
    # there are zero candidates, since the no-candidates skip log
    # already explained why.
    if candidates:
        rejected = sum(filter_counts.values())
        logger.info(
            f"[trade-cycle] candidates={len(candidates)} "
            f"(whale={fetched_w}, momentum={fetched_m}) "
            f"placed={placed} failed={failed} filtered={rejected}"
        )

    return inserted


# Throttle for `[skip-cycle]` reasons — reuse one logger per minute so
# the in-app log stays readable even when the trader scans every 20s.
_last_scan_skip_log: dict[str, float] = {}


def cap_dict_size(d: dict, cap: int = 2000) -> None:
    """FIFO-evict the oldest entries once `d` outgrows `cap` (dicts are
    insertion-ordered). A 24/7
    process must not let these module-level throttle/dedupe maps grow
    unbounded. Worst case an evicted key re-logs or re-retries once —
    harmless next to unbounded growth. Shared by the copy and crypto15m
    engines for their own throttle maps."""
    if len(d) > cap:
        for _k in list(d.keys())[: len(d) - cap]:
            d.pop(_k, None)

# Last scan cycle's outcome — cycle-level skip reason (hours/daily-stop/...)
# and per-gate rejection counts. The "why isn't it trading" panel reads these;
# before this they were computed every cycle and thrown away.
last_cycle: dict = {"skipReason": None, "filterCounts": {}, "candidates": 0, "placed": 0, "at": None}

# Throttle for `[filter]` reject lines (per signal id+source). Without
# this, fresh signals that fail a gate (`edge 0.5 < 3`, `entry 86c >
# 85c`, etc.) re-log every 20s for as long as they're under the
# `max_signal_age_sec` window — which fills the in-app log with the
# same line 6+ times per signal. Now we log the first rejection at
# INFO and skip the rest until the signal ages out.
_last_filter_log: dict[tuple[int, str], float] = {}

# Throttle for `[reconcile] skip-import` lines (per ticker+side).
# Polymarket keeps reporting cash-settlement positions for 5–10 minutes
# after we've already booked them locally, so the same skip-import
# line otherwise repeats every 30s reconcile cycle.
_last_skip_import_log: dict[tuple[str, str], float] = {}


# ───────── reconciliation / poll ────────────────────────────────────


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_polymarket_order(order: dict) -> dict:
    """Parse a CLOB OpenOrder (`GET /data/order/{id}`) into the bot's common
    fill shape. Polymarket order fields: `original_size`, `size_matched`,
    `price` (0..1), `status` (ORDER_STATUS_LIVE|MATCHED|CANCELED|…).

    A fully-matched order usually drops off the open-order endpoint (404),
    in which case the caller falls back to the position-aggregate rescue —
    same two-tier strategy the engine has always used.
    """
    filled = int(round(_f(order.get("size_matched"))))
    place_count = int(round(_f(order.get("original_size"))))
    price = _f(order.get("price"))                 # 0..1 USDC per share
    cost_cents = int(round(filled * price * 100))
    raw_status = (order.get("status") or "").lower()
    # Compared exactly, never by substring: "unmatched" CONTAINS "match", and
    # a FAK that crossed nothing is not a fill. The SDKs prefix the enum.
    st = raw_status.removeprefix("order_status_")
    if st in ("unmatched", "killed"):
        status = "canceled"
    elif st == "matched":
        status = "executed"
    elif st in ("canceled", "cancelled", "invalid") or st.startswith(("canceled_", "cancelled_")):
        status = "canceled"
    elif st == "live":
        status = "resting"
    else:
        status = raw_status or "resting"
    remaining = max(0, place_count - filled)
    avg_cents = (cost_cents / filled) if filled else None
    return {
        "filled": filled,
        "cost_cents": cost_cents,
        "avg_cents": avg_cents,
        "status": status,
        "place_count": place_count,
        "remaining": remaining,
    }


def _fills_after(created_at) -> Optional[int]:
    """Unix seconds to start an order's fill walk (`get_fills_for_order`'s
    `after`): an hour before its row was written. The row is written after
    the order is sent, so a fill can land before it, and the venue's clock is
    not ours; the slack only costs a slightly longer walk. None when the
    timestamp can't be read (the walk is then not narrowed by time)."""
    try:
        t = datetime.strptime(str(created_at)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return int(t.replace(tzinfo=timezone.utc).timestamp()) - 3600


def _parse_polymarket_fill(f: dict, default_side: str = "") -> dict:
    """Parse one CLOB trade (from `polymarket_api.get_fills_for_order`) into
    {count, side, price_cents}. The fill carries the bought token's price
    directly (`price_cents`); price_cents is None if it can't be resolved to
    a sane 1..99c value (caller skips those)."""
    count = int(round(_f(f.get("count_fp") if f.get("count") is None else f.get("count"))))
    side = str(f.get("side") or default_side).lower()
    cents = f.get("price_cents")
    if cents in (None, ""):
        # fall back to the yes/no fraction fields
        frac = f.get("yes_price_dollars") if side == "yes" else f.get("no_price_dollars")
        cents = int(round(_f(frac) * 100)) if frac is not None else None
    try:
        price_cents: Optional[int] = int(cents) if cents not in (None, "") else None
    except (TypeError, ValueError):
        price_cents = None
    if price_cents is not None and not (1 <= price_cents <= 99):
        price_cents = None
    return {"count": count, "side": side, "price_cents": price_cents}


def _parse_polymarket_position(p: dict) -> dict:
    try:
        qty_signed = float(p.get("position_fp") or 0)
    except (TypeError, ValueError):
        qty_signed = 0.0
    filled = int(abs(round(qty_signed)))
    side = "yes" if qty_signed > 0 else ("no" if qty_signed < 0 else "")
    try:
        exposure_usd = float(p.get("market_exposure_dollars") or 0)
    except (TypeError, ValueError):
        exposure_usd = 0.0
    try:
        fees_usd = float(p.get("fees_paid_dollars") or 0)
    except (TypeError, ValueError):
        fees_usd = 0.0
    cost_cents = int(round(exposure_usd * 100))
    avg_cents = (cost_cents / filled) if filled else None
    return {
        "filled": filled,
        "cost_cents": cost_cents,
        "avg_cents": avg_cents,
        "fees_usd": fees_usd,
        "side": side,
        "status": "executed",
        "place_count": filled,
        "remaining": 0,
    }


def _db_status_from_order(parsed: dict, target: int) -> str:
    s = parsed["status"]
    filled = parsed["filled"]
    if s == "executed" or (filled >= target and target > 0):
        return "filled"
    if s in ("canceled", "cancelled"):
        return "canceled" if filled == 0 else "partial"
    if filled > 0:
        return "partial"
    if s == "resting":
        return "submitted"
    return "submitted"


# Track per-position consecutive poll failures so a single bad
# response from Polymarket doesn't prematurely retire a row. Map is keyed
# by `bot_positions.id` and reset on any successful update.
_poll_failures: dict[int, int] = {}
_POLL_FAILURE_THRESHOLD = 6  # ~6 cycles (1+ minute) before marking gone


async def poll_open_orders(cfg: dict) -> list[dict]:
    """Reconcile pending orders with Polymarket. Returns list of updated rows.

    Two-tier strategy:
      1. Per-order via `/portfolio/orders/{id}` (preferred — gives us
         exact filled_contracts and cost_cents for THIS order).
      2. Position-aggregate via `/portfolio/positions` (rescue path —
         used only when the order endpoint can't tell us what
         happened, e.g. transient 404, missing order id, etc.). The
         aggregate is safe to attribute to a single local row only
         when there is exactly one unresolved local row claiming this
         ticker+direction; otherwise we leave the row alone.

    We **never** mark a row `gone` on a single failed poll — that was
    the bug that left the user with 16 positions on Polymarket but only 2
    visible in the dashboard. A row only retires after
    `_POLL_FAILURE_THRESHOLD` consecutive failures.
    """
    with db.get_db() as conn:
        pending = db.get_pending_bot_positions(conn)
    if not pending:
        return []

    # Pre-fetch the position aggregate ONCE per cycle for the rescue path.
    pos_by_key: dict[tuple[str, str], dict] = {}
    try:
        live_positions = await get_positions(limit=1000)
        for lp in live_positions:
            qty = 0.0
            for k in ("position_fp", "position"):
                v = lp.get(k)
                if v in (None, "", 0):
                    continue
                try:
                    f = float(v)
                    if f != 0:
                        qty = f
                        break
                except (TypeError, ValueError):
                    pass
            if qty == 0:
                continue
            tkr = lp.get("ticker") or ""
            side = "yes" if qty > 0 else "no"
            pos_by_key[(tkr, side)] = lp
    except Exception as e:
        logger.debug(f"poll: get_positions for rescue failed: {e}")

    updated: list[dict] = []

    def _bump_failure(pid: int) -> int:
        n = _poll_failures.get(pid, 0) + 1
        _poll_failures[pid] = n
        # Success pops the counter, but rows that go 'gone' never succeed —
        # cap so those orphaned counters can't accumulate forever.
        cap_dict_size(_poll_failures)
        return n

    def _clear_failure(pid: int) -> None:
        _poll_failures.pop(pid, None)

    def _try_rescue_from_position_aggregate(pos: dict, conn) -> dict | None:
        """If exactly one of OUR unresolved local rows claims this
        ticker+side, attribute the live position aggregate to it.
        Returns the updated row or None.

        `conn` is the pass-wide connection (see the loop below) — this helper
        used to open two of its own per call, on top of the caller's.

        We skip the DB write entirely when nothing material changed
        since the last poll — without that guard, every 30-second poll
        cycle re-touches every "filled" pending row, which fires a
        a `position:update` event on the renderer
        even though absolutely nothing happened. That's the duplicate
        spam the user reported.
        """
        live_p = pos_by_key.get((pos["ticker"], pos["direction"]))
        if not live_p:
            return None
        siblings = conn.execute(
            """SELECT id FROM bot_positions
                WHERE ticker = ? AND direction = ?
                  AND network = ?
                  AND resolved = 0
                  AND status NOT IN ('gone','canceled','expired','error','dry_run')""",
            (pos["ticker"], pos["direction"], pos["network"]),
        ).fetchall()
        if len(siblings) != 1:
            return None  # ambiguous — would risk double-counting
        # Tolerant qty + cost extraction (Polymarket has shipped two
        # different field naming conventions for these over time).
        qty = 0.0
        for k in ("position_fp", "position"):
            v = live_p.get(k)
            if v in (None, "", 0):
                continue
            try:
                f = float(v)
                if f != 0:
                    qty = abs(f)
                    break
            except (TypeError, ValueError):
                pass
        cost_cents = 0.0
        v = live_p.get("market_exposure")
        if v not in (None, "", 0):
            try:
                cost_cents = abs(float(v))
            except (TypeError, ValueError):
                pass
        if cost_cents == 0:
            v = live_p.get("market_exposure_dollars")
            if v not in (None, ""):
                try:
                    cost_cents = abs(float(v) * 100.0)
                except (TypeError, ValueError):
                    pass
        if qty <= 0:
            return None
        # Same defense as the main reconcile path: if Polymarket omitted
        # cost on this response, keep whatever we already had locally.
        # Otherwise we'd zero out a perfectly good cost basis on every
        # 30-second poll, which downstream code reads as "no fill".
        local_cost_usd = float(pos.get("cost_usd") or 0.0)
        local_avg_cents = pos.get("avg_fill_price_cents")
        new_cost_usd = (
            cost_cents / 100.0 if cost_cents > 0 else local_cost_usd
        )
        new_avg_cents = (
            (cost_cents / qty) if (qty and cost_cents > 0)
            else local_avg_cents
        )
        new_filled = int(round(qty))

        # No-op short-circuit: if status, fill count, and cost basis
        # match what's already stored, don't write the row again. This
        # is what kills the duplicate-position
        # update spam (one fire per cycle per filled row).
        cur_filled = int(pos.get("filled_contracts") or 0)
        cur_cost_usd = float(pos.get("cost_usd") or 0.0)
        if (
            pos.get("status") == "filled"
            and cur_filled == new_filled
            and abs(cur_cost_usd - new_cost_usd) < 0.005
        ):
            return None

        db.update_bot_position(
            conn, pos["id"], status="filled",
            filled_contracts=new_filled,
            cost_usd=new_cost_usd,
            avg_fill_price_cents=new_avg_cents,
        )
        db.log_event(
            conn, pos["id"], "poll",
            note="rescued via /portfolio/positions",
        )
        row = db.fetch_position_by_id(conn, pos["id"])
        conn.commit()   # per-row durability, exactly as the old get_db() block
        return row

    # Poll every pending order's status CONCURRENTLY. This used to be a serial
    # `await get_order(kid)` inside the loop, so a 20-order backlog cost
    # 20 × per-request latency (seconds) before the FIRST row was written.
    # Bounded at 8 in flight — the same cap polymarket_api.fetch_markets_map
    # uses — so a big backlog stays friendly to the CLOB's rate limits.
    # A failure is captured and re-raised at that order's turn in the loop
    # below, so the per-order handling (404 rescue, failure counters, order of
    # writes) is exactly what it was: one bad order still can't stop the rest.
    _order_sem = asyncio.Semaphore(8)

    async def _poll_one(pid, kid: str):
        async with _order_sem:
            try:
                return pid, await get_order(kid)
            except Exception as e:
                return pid, e

    _to_poll = [
        (p["id"], p.get("order_id")) for p in pending
        if p.get("order_id") and p["status"] not in ("dry_run", "filled")
    ]
    order_resps: dict = dict(
        await asyncio.gather(*[_poll_one(pid, kid) for pid, kid in _to_poll])
    )

    # One connection for the whole write pass instead of one per branch (plus
    # two more inside the rescue helper). Every block still commits at its own
    # boundary, so a later order raising can't roll back an earlier row's
    # update — and no write transaction is ever held open across an await
    # (tests/test_db_connection_discipline.py fails the build if one is).
    # Expired resting orders are collected here and cancelled AFTER the pass.
    expired: list[tuple[int, str, float]] = []
    with db.get_db() as conn:
        for pos in pending:
            kid = pos.get("order_id")
            if pos["status"] == "dry_run":
                continue

            # Rows that are already `status='filled'` should never go down
            # the order-poll path. They're in the pending bucket only
            # because cost_usd is missing — the order endpoint can't help
            # us with that anyway (it returns the original placement, which
            # may show filled=0 long after the position has been working).
            # Calling update_bot_position with that historical data
            # overwrites our local filled_contracts to 0, which then trips
            # mark_resolved_positions's "no fill" branch and silently kills
            # the row. That was the death loop behind "9 positions appear,
            # then disappear on a cycle". Use the position aggregate ONLY.
            if pos["status"] == "filled":
                row = _try_rescue_from_position_aggregate(pos, conn)
                if row:
                    _clear_failure(pos["id"])
                    updated.append(row)
                else:
                    # Couldn't reconfirm via aggregate either — leave the
                    # row alone, don't bump failure (we don't want to mark
                    # gone something Polymarket might still hold).
                    _clear_failure(pos["id"])
                continue

            if not kid:
                # No order id (very rare placement glitch). Try the rescue
                # path before retiring the row.
                row = _try_rescue_from_position_aggregate(pos, conn)
                if row:
                    _clear_failure(pos["id"])
                    updated.append(row)
                    continue
                # No rescue available — count this as a failure but don't
                # retire until repeated.
                n = _bump_failure(pos["id"])
                if n >= _POLL_FAILURE_THRESHOLD:
                    db.update_bot_position(
                        conn, pos["id"], status="gone",
                        error=f"missing order_id ({n} cycles)",
                    )
                    db.log_event(conn, pos["id"], "poll", note=f"no id × {n} -> gone")
                    row = db.fetch_position_by_id(conn, pos["id"])
                    conn.commit()
                    if row:
                        updated.append(row)
                    _clear_failure(pos["id"])
                continue

            try:
                if pos["id"] in order_resps:
                    resp = order_resps[pos["id"]]
                    if isinstance(resp, Exception):
                        raise resp     # replayed in this order's own context
                else:
                    # Not prefetched — should not happen, every pending row
                    # with an order id is gathered above. Never await inside
                    # this transaction (tests/test_db_connection_discipline);
                    # skip this cycle and let the next poll pick it up.
                    logger.debug(f"order poll {kid}: not prefetched, retrying next cycle")
                    continue
                order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
                parsed = _parse_polymarket_order(order)
            except PolymarketAPIError as e:
                # 404s are common for very fresh orders or after Polymarket
                # rolls them — try the rescue path before marking gone, and
                # still gate on consecutive failures.
                row = _try_rescue_from_position_aggregate(pos, conn)
                if row:
                    _clear_failure(pos["id"])
                    updated.append(row)
                    continue
                n = _bump_failure(pos["id"])
                logger.debug(
                    f"order poll {kid}: HTTP {e.status} (failure #{n})"
                )
                if e.status == 404 and n >= _POLL_FAILURE_THRESHOLD:
                    db.update_bot_position(
                        conn, pos["id"], status="gone",
                        error=f"order 404 × {n}",
                    )
                    db.log_event(conn, pos["id"], "poll", note=f"404 × {n} -> gone")
                    row = db.fetch_position_by_id(conn, pos["id"])
                    conn.commit()
                    if row:
                        updated.append(row)
                    _clear_failure(pos["id"])
                continue
            except Exception as e:
                _bump_failure(pos["id"])
                logger.warning(f"order poll {kid}: {e}")
                continue

            # Successful poll — clear any prior failure streak.
            _clear_failure(pos["id"])

            db_status = _db_status_from_order(parsed, pos["target_contracts"])
            # If the order endpoint says "filled" but reports 0 contracts,
            # try the rescue path before assuming it's gone.
            if db_status == "filled" and parsed["filled"] == 0:
                rescued = _try_rescue_from_position_aggregate(pos, conn)
                if rescued:
                    updated.append(rescued)
                    continue
                db_status = "gone"

            fields: dict = {
                "status": db_status,
                "filled_contracts": parsed["filled"],
            }
            # A partial fill whose remainder was CANCELED frees the unfilled
            # cash: collapse target_contracts to the filled qty so
            # current_total_exposure_usd stops valuing the dead remainder's
            # notional (target × limit) as committed capital — otherwise
            # exposure over-counts and needlessly blocks new entries until the
            # position resolves. A still-RESTING partial keeps its target, so
            # its genuinely-working remainder stays counted.
            if db_status == "partial" and parsed["status"] in ("canceled", "cancelled"):
                fields["target_contracts"] = parsed["filled"]
            if parsed["avg_cents"] is not None:
                fields["avg_fill_price_cents"] = parsed["avg_cents"]
            if parsed["cost_cents"]:
                fields["cost_usd"] = parsed["cost_cents"] / 100.0
            if parsed.get("fees_usd"):
                fields["fees_usd"] = parsed["fees_usd"]
            elif parsed["filled"] and parsed["avg_cents"] is not None:
                # V2 orders never report a fee, but takers still pay one —
                # synthesize fee-v2 at the real fill price so P&L and the
                # dashboard's fee total stop pretending trading is free.
                fields["fees_usd"] = _position_fee_usd({
                    **pos,
                    "filled_contracts": parsed["filled"],
                    "avg_fill_price_cents": parsed["avg_cents"],
                    "fees_usd": 0.0,
                })
            db.update_bot_position(conn, pos["id"], **fields)
            db.log_event(
                conn, pos["id"], "poll",
                order_status=parsed["status"],
                filled_contracts=parsed["filled"],
                fill_cost_cents=parsed["cost_cents"] or None,
            )
            row = db.fetch_position_by_id(conn, pos["id"])
            conn.commit()
            if row:
                updated.append(row)

            # Auto-cancel only if still resting and unfilled.
            if (
                db_status == "submitted"
                and cfg.get("order_expiration_sec") is not None
                and parsed["filled"] == 0
                and kid
            ):
                age = conn.execute(
                    "SELECT (julianday('now')-julianday(created_at))*86400 "
                    "FROM bot_positions WHERE id=?", (pos["id"],),
                ).fetchone()
                age_sec = float(age[0]) if age and age[0] is not None else 0.0
                if age_sec > float(cfg["order_expiration_sec"]):
                    expired.append((int(pos["id"]), str(kid), age_sec))

    # Auto-cancel the expired resting orders OUTSIDE the write pass — the
    # cancel is a network round trip — and record each outcome in its own
    # short transaction.
    for pid, kid, age_sec in expired:
        try:
            await cancel_order(kid)
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pid, status="canceled",
                    error=f"auto-canceled after {age_sec:.0f}s",
                )
                db.log_event(conn, pid, "cancel", note=f"age {age_sec:.0f}s")
                r = db.fetch_position_by_id(conn, pid)
            if r:
                updated.append(r)
        except PolymarketAPIError as e:
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pid, status="gone",
                    error=f"cancel 404: {str(e.body)[:100]}",
                )
                db.log_event(conn, pid, "cancel", note="404 -> gone")
                r = db.fetch_position_by_id(conn, pid)
            if r:
                updated.append(r)
        except Exception as e:
            logger.warning(f"cancel exception {kid}: {e}")

    return updated


# ───────── resolution / settlement ──────────────────────────────────


def _market_yes_payout(market: dict | None) -> Optional[float]:
    """Return the YES-side $/contract payout if the market is definitively
    settled, else None. Looks at `result` ("yes"/"no") then a settlement value.

    Both `settlement_value_dollars` (from fetch_market) and `settlement_value`
    (the markets-table cache, which db.upsert_market writes FROM
    settlement_value_dollars) are DOLLARS in [0, 1] — NOT cents. The old `/100`
    "legacy cents" conversion would mis-settle a winner (1.0) as a loss (0.01)
    on any row that has settlement_value without a `result`.
    """
    if not market:
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return 1.0
    if result == "no":
        return 0.0
    status = (market.get("status") or "").lower()
    if status not in ("settled", "finalized", "determined"):
        return None
    sv = market.get("settlement_value_dollars")
    if sv is None:
        sv = market.get("settlement_value")
    if sv is None:
        return None
    try:
        f = float(sv)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= f <= 1.0):
        # Out of the dollars range → don't guess an outcome from a junk value.
        logger.warning(f"[settle] ignoring out-of-range settlement_value={sv!r}")
        return None
    return f


# Slow-retry lane for mark_resolved_positions. Some unresolved rows can never
# settle (delisted markets, junk settlement values) — without this they get
# re-fetched every 300s resolver cycle forever. Rows older than 7 days that
# still yield no decisive outcome are only re-attempted every ~6 hours.
# In-memory only (a restart just retries once), keyed by bot_positions.id.
_resolve_retry_at: dict[int, float] = {}
_RESOLVE_SLOW_AGE_SEC = 7 * 86400
_RESOLVE_SLOW_RETRY_SEC = 6 * 3600


def _row_age_sec(created_at) -> float:
    """Age of a SQLite `datetime('now')` timestamp (UTC) in seconds; 0.0 when
    unparseable so a malformed row keeps the old every-cycle behavior."""
    try:
        dt = datetime.fromisoformat(str(created_at)).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (TypeError, ValueError):
        return 0.0


def _fee_coeff_for(pos: dict) -> float:
    """Taker rate for a bot_positions row: backtest.taker_fee_rate, the same for
    every category on this venue. These rows don't carry the market's own
    feeCoefficient, so a Table Tennis position (0.10) is booked at the standard
    rate. The Terminal's ledger reads the market's own rate and the venue's fee."""
    return backtest.taker_fee_rate(pos.get("category"))


def _position_fee_usd(pos: dict) -> float:
    """Taker fee attributable to a bot_positions row's ENTRY fill.

    2026-08-18 fee audit: V2 orders carry no venue-reported fee
    (fees_paid_dollars is always 0), so every main-engine / copy / script
    market position was booked GROSS while the crypto15m engine, the
    backtester and the script shadow ledger were all fee-net. Synthesize the
    fee-v2 amount (rate × p·(1−p) × contracts, category-rated) at the entry
    price whenever the venue reported nothing. Rows stamped entry_maker=1
    (a resting limit that filled as a maker under takerOnly fee-v2) pay 0;
    legacy rows (NULL) are assumed taker — the main engine always crosses.

    The full venue fee, never net of the taker-rebate tier: Polymarket pays
    the rebate back separately (daily, in pUSD), and the crypto engine's
    realised fee (crypto15m_trader._order_fee_usd) never netted it, so with
    a tier set the two engines booked the same fill differently."""
    if pos.get("entry_maker"):
        return 0.0
    reported = float(pos.get("fees_usd") or 0.0)
    if reported > 0:
        return reported
    filled = int(pos.get("filled_contracts") or 0)
    if filled <= 0:
        return 0.0
    avg_c = pos.get("avg_fill_price_cents")
    cost = float(pos.get("cost_usd") or 0.0)
    price = (float(avg_c) / 100.0) if avg_c else (cost / filled if cost > 0 else 0.0)
    if not (0.0 < price < 1.0):
        return 0.0
    return round(
        backtest.venue_fee_per_contract(price, _fee_coeff_for(pos)) * filled, 6)


def _sell_fee_usd(pos: dict, sold: int, price_cents: float | None) -> float:
    """Taker fee for a crossing SELL of `sold` contracts at `price_cents`
    (category-rated fee-v2, the full venue fee like _position_fee_usd).
    Settlement/redemption is free — only use this for exits that actually
    cross the book."""
    if not sold or not price_cents:
        return 0.0
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    return round(
        backtest.venue_fee_per_contract(p, _fee_coeff_for(pos)) * int(sold), 6)


async def mark_resolved_positions(cfg: dict) -> list[dict]:
    """Resolve filled positions whose markets have settled.

    Each local position row is resolved **independently** based on:
      - its own `filled_contracts` and `cost_usd` (what we actually paid)
      - the binary outcome of the market (`result` from the public
        `/markets/{ticker}` endpoint)

    P&L formula (per row):
        settlement_usd = filled * our_payout    # our_payout ∈ {0, 1} for binary
        pnl_usd        = settlement_usd - cost_usd

    Why per-row instead of grouping by ticker?

    Polymarket's `/portfolio/positions?settlement_status=settled` reports the
    *cumulative lifetime* `realized_pnl_dollars` for a (ticker, env)
    pair. Two pitfalls when we tried to use it:

      1. It includes P&L from old historical trades on the same ticker —
         attributing that to today's local rows was wrong.
      2. With multiple local rows on the same ticker, prorating by cost
         smeared the lifetime aggregate evenly when it should have been
         per-row based on each fill's relationship to settlement.

    The market's settlement value is shared between demo and prod (same
    underlying event), so we can use the public endpoint regardless of
    `network`. No-fill rows (canceled/expired/gone with filled=0)
    always resolve at $0 with NULL outcome (didn't actually trade).
    """
    with db.get_db() as conn:
        unresolved = db.get_unresolved_bot_positions(conn)
    if not unresolved:
        return []

    env = get_env()
    by_env = [p for p in unresolved if p["network"] == env]
    if not by_env:
        return []

    # 1) Close the easy cases first: rows that never filled cannot have
    # any settlement — they're done. We deliberately key off
    # filled_contracts ONLY, not cost_usd. Reason: rescued/imported
    # external positions sometimes come back from Polymarket with a real
    # qty but a zero `market_exposure` field (the API occasionally
    # omits cost on certain shapes). Those rows still represent live
    # exposure and must wait for true market settlement — not get
    # silently auto-closed as "no fill". That bug was making 9 open
    # positions vanish from the dashboard a few seconds after the
    # reconcile rescued them.
    updated: list[dict] = []
    needs_market: list[dict] = []
    for pos in by_env:
        filled = int(pos["filled_contracts"] or 0)
        if filled <= 0:
            with db.get_db() as conn:
                db.update_bot_position(
                    conn, pos["id"], resolved=1, outcome_correct=None,
                    pnl_usd=0.0, settlement_usd=0.0,
                )
                conn.execute(
                    "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                    (pos["id"],),
                )
                db.log_event(conn, pos["id"], "resolve", note="no fill -> closed")
                r = db.fetch_position_by_id(conn, pos["id"])
            if r:
                updated.append(r)
            continue
        if time.monotonic() < _resolve_retry_at.get(pos["id"], 0.0):
            continue  # slow lane: recently confirmed still unsettleable
        needs_market.append(pos)

    if not needs_market:
        return updated

    # 2) Fetch market data for unique tickers, then resolve each row
    # individually. We re-fetch from the API rather than trust the local
    # markets cache (which earlier code paths could have populated with
    # stale settlement_value=0 rows).
    # Fetch every market concurrently (bounded), then upsert them all under ONE
    # connection — the old code awaited each fetch serially AND opened a fresh
    # DB connection per ticker.
    market_cache = await fetch_markets_map(list({p["ticker"] for p in needs_market}))
    if market_cache:
        try:
            with db.get_db() as conn:
                for ticker, m in market_cache.items():
                    db.upsert_market(
                        conn,
                        {
                            "ticker": ticker,
                            "event_ticker": m.get("event_ticker", ""),
                            "title": m.get("title", ""),
                            "yes_sub_title": m.get("yes_sub_title", ""),
                            "status": m.get("status", ""),
                            "close_time": m.get("close_time", ""),
                            "volume": m.get("volume_fp", 0),
                            "volume_24h": m.get("volume_24h_fp", 0),
                            "open_interest": m.get("open_interest_fp", 0),
                            "yes_bid": m.get("yes_bid_dollars", 0),
                            "yes_ask": m.get("yes_ask_dollars", 0),
                            "last_price": m.get("last_price_dollars", 0),
                            "result": m.get("result", ""),
                            "settlement_value": m.get("settlement_value_dollars"),
                        },
                    )
        except Exception:
            pass

    for pos in needs_market:
        market = market_cache.get(pos["ticker"])
        yes_payout = _market_yes_payout(market)
        if yes_payout is None:
            # Not yet settled — try again next cycle. Rows stuck >7 days are
            # near-certainly permanently unsettleable (delisted market) —
            # park them in the slow lane so they don't re-fetch every cycle.
            if _row_age_sec(pos.get("created_at")) > _RESOLVE_SLOW_AGE_SEC:
                _resolve_retry_at[pos["id"]] = time.monotonic() + _RESOLVE_SLOW_RETRY_SEC
                cap_dict_size(_resolve_retry_at)
            continue

        direction = pos["direction"]
        filled = int(pos["filled_contracts"] or 0)
        cost_usd = float(pos["cost_usd"] or 0.0)
        fee_usd = _position_fee_usd(pos)
        our_payout = yes_payout if direction == "yes" else (1.0 - yes_payout)
        settlement_usd = filled * our_payout
        pnl_usd = settlement_usd - cost_usd - fee_usd

        # Physical bounds clamp.
        # Each $1 binary contract pays at most $1 and costs at most $1
        # (plus the entry fee either way).
        # If the math says we made more than `filled` dollars in profit on
        # `filled` contracts, the inputs are corrupt — log loudly and clamp
        # so a single buggy row can't make the whole dashboard hallucinate.
        max_settlement = float(filled)                  # max possible payout
        max_pnl = max_settlement - cost_usd - fee_usd   # max possible profit
        min_pnl = -cost_usd - fee_usd                   # max possible loss
        if pnl_usd > max_pnl + 0.01 or pnl_usd < min_pnl - 0.01:
            logger.warning(
                f"[resolve-clamp] {pos['ticker']} pnl=${pnl_usd:+.2f} "
                f"outside physical bounds (filled={filled}, cost=${cost_usd:.2f}, "
                f"settlement=${settlement_usd:.2f}); clamping. "
                f"This usually means filled_contracts or cost_usd is corrupt — "
                f"run Reconcile Fills."
            )
            pnl_usd = max(min_pnl, min(max_pnl, pnl_usd))
            settlement_usd = pnl_usd + cost_usd + fee_usd

        if our_payout >= 0.99:
            correct: Optional[int] = 1
        elif our_payout <= 0.01:
            correct = 0
        elif pnl_usd > 0.05:
            correct = 1
        elif pnl_usd < -0.05:
            correct = 0
        else:
            correct = None

        with db.get_db() as conn:
            fields = dict(resolved=1, outcome_correct=correct,
                          settlement_usd=settlement_usd, pnl_usd=pnl_usd)
            # Persist a synthesized fee so the dashboard's feesUsd aggregate
            # reflects reality instead of the venue's structural $0.
            if fee_usd > 0 and not float(pos.get("fees_usd") or 0.0):
                fields["fees_usd"] = fee_usd
            db.update_bot_position(conn, pos["id"], **fields)
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now') WHERE id=?",
                (pos["id"],),
            )
            label = "WIN" if correct == 1 else ("LOSS" if correct == 0 else "CLOSED")
            db.log_event(
                conn, pos["id"], "resolve",
                note=f"{label} pnl=${pnl_usd:+.2f} ({filled}c @ {our_payout:.2f})",
            )
            r = db.fetch_position_by_id(conn, pos["id"])
        if r:
            updated.append(r)
            logger.info(
                f"[resolve] {pos['ticker']} {direction}: {label} "
                f"pnl=${pnl_usd:+.2f} ({filled}c @ {our_payout:.2f}, "
                f"cost=${cost_usd:.2f})"
            )

    return updated


# ───────── manual reconciliation helpers ────────────────────────────


async def reconcile_fills_from_polymarket() -> dict:
    """For every local position with a `order_id`, pull its
    authoritative fill list from Polymarket and rewrite `filled_contracts`,
    `cost_usd`, `avg_fill_price_cents`. Then reset+rerun the resolver.

    This is the recovery action for any position rows that ended up with
    inflated filled/cost numbers from older buggy poll paths. After this
    runs, `pnl_usd` is computed from authoritative per-order fills and
    the market's binary settlement, with no aggregation across rows.

    Returns {fills_reconciled, pnl_cleared, pnl_recomputed}.
    """
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, order_id, ticker, direction, status, created_at
               FROM bot_positions
               WHERE network=? AND order_id IS NOT NULL
                 AND status NOT IN ('dry_run','submitted')""",
            (env,),
        ).fetchall()
        targets = [dict(r) for r in rows]

    fills_reconciled = 0
    for pos in targets:
        kid = pos["order_id"]
        try:
            fills = await get_fills_for_order(
                kid, market=pos["ticker"], after=_fills_after(pos.get("created_at")))
        except Exception as e:
            logger.debug(f"fills fetch failed for {kid}: {e}")
            continue

        # Defensive client-side filter — some Polymarket API versions
        # silently ignore `order_id` and return ALL fills, which would
        # let one local position swallow every fill on the account.
        # Always re-filter by the explicit order_id field on each fill.
        ours = [f for f in fills if str(f.get("order_id") or "") == str(kid)]
        if not ours and fills:
            logger.warning(
                f"[reconcile] /portfolio/fills returned {len(fills)} fills "
                f"but none matched order_id={kid} — Polymarket may have rotated "
                f"old fills out. Skipping this position."
            )
            continue

        # Only count BUY fills toward cost (the bot doesn't sell, but be
        # defensive in case Polymarket includes settlement fills too).
        ours = [
            f for f in ours
            if str(f.get("action") or "buy").lower() == "buy"
        ]

        total_filled = 0
        total_cost_cents = 0
        for f in ours:
            fp = _parse_polymarket_fill(f, default_side=pos["direction"])
            n = fp["count"]
            if n <= 0:
                continue
            price_cents = fp["price_cents"]
            if price_cents is None:
                logger.warning(
                    f"[reconcile] unreadable price on fill order={kid} "
                    f"side={fp['side']}; skipping fill"
                )
                continue
            total_filled += n
            total_cost_cents += n * price_cents

        if total_filled == 0 and total_cost_cents == 0:
            continue

        avg_cents = (total_cost_cents / total_filled) if total_filled else None
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"],
                filled_contracts=total_filled,
                cost_usd=total_cost_cents / 100.0,
                avg_fill_price_cents=avg_cents,
            )
            db.log_event(
                conn, pos["id"], "reconcile-fills",
                filled_contracts=total_filled,
                fill_cost_cents=total_cost_cents,
            )
        fills_reconciled += 1

    # Now reset+rerun the resolver against authoritative fill data.
    with db.get_db() as conn:
        cleared = conn.execute(
            """UPDATE bot_positions
               SET resolved=0, outcome_correct=NULL,
                   pnl_usd=NULL, settlement_usd=NULL,
                   resolved_at=NULL
               WHERE network=?
                 AND status IN ('filled','partial','expired','canceled','gone','error')
                 AND resolved=1
                 -- Never re-resolve positions SOLD before settlement (flatten /
                 -- external-exit / copy-exit): they hold the true realized-sale
                 -- pnl; re-resolving would re-book them at binary settlement value.
                 AND COALESCE(closed_early,0)=0""",
            (env,),
        ).rowcount
    resolved = await mark_resolved_positions({})
    return {
        "fills_reconciled": fills_reconciled,
        "pnl_cleared": int(cleared or 0),
        "pnl_recomputed": len(resolved),
    }


async def audit_pnl(limit: int = 200) -> dict:
    """Recompute pnl for every resolved position from its stored
    filled/cost and the market's current settlement, and return a
    breakdown of any rows where the stored value disagrees with the
    fresh recompute. Useful for diagnosing where realized P&L is off.
    """
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd,
                      pnl_usd, settlement_usd, outcome_correct,
                      category, fees_usd, avg_fill_price_cents, entry_maker
               FROM bot_positions
               WHERE network=? AND resolved=1
               ORDER BY resolved_at DESC LIMIT ?""",
            (env, int(limit)),
        ).fetchall()
        rows = [dict(r) for r in rows]

    flagged: list[dict] = []
    sum_stored = 0.0
    sum_recompute = 0.0
    for r in rows:
        market = await fetch_market(r["ticker"])
        yes_payout = _market_yes_payout(market)
        if yes_payout is None:
            continue
        direction = r["direction"]
        filled = int(r["filled_contracts"] or 0)
        cost = float(r["cost_usd"] or 0)
        our = yes_payout if direction == "yes" else (1.0 - yes_payout)
        settle = filled * our
        fee = _position_fee_usd(r)
        pnl_fresh = settle - cost - fee
        # clamp to physical bounds (entry fee is paid win or lose)
        pnl_fresh = max(-cost - fee, min(filled - cost - fee, pnl_fresh))
        pnl_stored = float(r["pnl_usd"] or 0)
        sum_stored += pnl_stored
        sum_recompute += pnl_fresh
        if abs(pnl_stored - pnl_fresh) > 0.05:
            flagged.append({
                "id": r["id"],
                "ticker": r["ticker"],
                "direction": direction,
                "filled": filled,
                "cost": round(cost, 2),
                "settlement_payout": round(our, 2),
                "stored_pnl": round(pnl_stored, 2),
                "fresh_pnl": round(pnl_fresh, 2),
                "delta": round(pnl_fresh - pnl_stored, 2),
            })
    return {
        "checked": len(rows),
        "flagged": len(flagged),
        "sum_stored_pnl": round(sum_stored, 2),
        "sum_recompute_pnl": round(sum_recompute, 2),
        "delta": round(sum_recompute - sum_stored, 2),
        "samples": flagged[:20],
    }


async def recompute_pnl_from_polymarket() -> dict:
    """Lighter version of `reconcile_fills_from_polymarket`: only resets
    pnl/outcome and reruns the resolver. Useful when you trust the
    locally-stored filled/cost but want fresh settlement attribution.
    """
    env = get_env()
    with db.get_db() as conn:
        cleared = conn.execute(
            """UPDATE bot_positions
               SET resolved=0, outcome_correct=NULL,
                   pnl_usd=NULL, settlement_usd=NULL,
                   resolved_at=NULL
               WHERE network=?
                 AND status IN ('filled','partial','expired','canceled','gone','error')
                 AND resolved=1
                 -- Never re-resolve positions SOLD before settlement (flatten /
                 -- external-exit / copy-exit): they hold the true realized-sale
                 -- pnl; re-resolving would re-book them at binary settlement value.
                 AND COALESCE(closed_early,0)=0""",
            (env,),
        ).rowcount
    rows = await mark_resolved_positions({})
    return {"cleared": int(cleared or 0), "recomputed": len(rows)}


# ───────── startup reconciler ──────────────────────────────────────


async def reconcile_positions_with_polymarket() -> tuple[dict, list[dict]]:
    """Fix our local view of the world to match what Polymarket shows.

    Source of truth: `/portfolio/positions`. For each Polymarket position
    with non-zero qty, we look for ANY local row matching
    (ticker, direction, env) — including rows previously marked `gone`
    or `canceled` by older buggy poll code:

      - Found, unresolved, active status (submitted/partial/filled):
        → just sync `filled_contracts` / `cost_usd`.
      - Found, unresolved, terminal status (gone/canceled/expired/error):
        → **resurrect** as `filled` (status=filled, error=null). This is
        the fix for "dashboard says 2 open but Polymarket shows 16".
      - Found, but already resolved: this is a brand-new round on the
        same ticker → import as a new `external` row.
      - No matching row at all: → import as new `external` row.

    Local rows that aren't in Polymarket's live list are left alone — let
    `mark_resolved_positions` decide if the market settled. Aggressive
    orphan-marking was the regression that hid live positions.

    Returns (summary, changed_rows). `changed_rows` is the list of
    positions the caller should emit `position:update`/`position:new`
    events for so the renderer's React state stays in sync.
    """
    summary = {
        "closed_orphans": 0,
        "imported_unknowns": 0,
        "rescued": 0,
        "resurrected": 0,
    }
    changed: list[dict] = []
    env = get_env()

    try:
        live = await get_positions(limit=1000)
    except Exception as e:
        logger.warning(f"reconcile: get_positions failed: {e}")
        return summary, changed

    def _signed_qty(p: dict) -> float:
        """Quantity (signed: + YES, − NO) — try every field Polymarket has
        used over time. The original codebase relied on `position_fp`
        but recent responses sometimes only populate `position`. We
        accept either."""
        for k in ("position_fp", "position"):
            v = p.get(k)
            if v in (None, "", 0):
                continue
            try:
                f = float(v)
                if f != 0:
                    return f
            except (TypeError, ValueError):
                pass
        return 0.0

    def _cost_cents(p: dict) -> float:
        """Cost basis in cents — `market_exposure` is cents (int),
        `market_exposure_dollars` is a USD string. Try both."""
        v = p.get("market_exposure")
        if v not in (None, "", 0):
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
        v = p.get("market_exposure_dollars")
        if v not in (None, ""):
            try:
                return abs(float(v) * 100.0)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _cur_price_cents(p: dict) -> Optional[float]:
        """Live price of the HELD outcome in cents (0..100), from the Data
        API `curPrice`. Stored as the position's mark so the UI can show
        unrealized P&L. Returns None for an absent/0/out-of-range price so we
        never overwrite a good mark with a junk value."""
        try:
            f = float(p.get("cur_price"))
        except (TypeError, ValueError):
            return None
        if not (0.0 < f <= 1.0):
            return None
        return round(f * 100.0, 2)

    # Diagnostic: if Polymarket returns positions but very few have non-
    # zero qty, the user (and their support helper) need to know that
    # so we can debug. Log raw counts + a sample of the first row's
    # keys so we can confirm the API shape on the spot.
    nonzero_count = sum(1 for p in live if _signed_qty(p) != 0)
    if live and nonzero_count == 0:
        sample_keys = list(live[0].keys())
        logger.warning(
            f"reconcile: Polymarket returned {len(live)} positions but ZERO "
            f"have non-zero qty — likely a field-name change. "
            f"sample keys = {sample_keys}"
        )
    elif live:
        # Demoted to DEBUG: this fires every 30s reconcile cycle and
        # otherwise dominates the in-app log. The dashboard's account
        # tile already shows live position counts.
        logger.debug(
            f"reconcile: Polymarket returned {len(live)} positions, "
            f"{nonzero_count} with non-zero qty"
        )

    live_by_key: dict[tuple[str, str], dict] = {}
    for p in live:
        qty = _signed_qty(p)
        if qty == 0:
            continue
        side = "yes" if qty > 0 else "no"
        live_by_key[(p.get("ticker") or "", side)] = p

    # Pull EVERY local row for this env (any status), then bucket the
    # unresolved ones by (ticker, side). We deliberately include `gone`/
    # `canceled` rows so we can resurrect them.
    with db.get_db() as conn:
        all_local = conn.execute(
            "SELECT * FROM bot_positions WHERE network = ?", (env,),
        ).fetchall()
        # (ticker, side) owned by the SIBLING crypto15m engine (separate table).
        # The main-bot reconcile must not adopt those as `external` rows — see
        # the import-unknown branch.
        crypto15m_keys = db.crypto15m_owned_keys(conn, env)
    local_by_key: dict[tuple[str, str], list] = {}
    for r in all_local:
        pos = dict(r)
        if pos.get("resolved"):
            continue  # resolved rows aren't candidates — those are done
        if pos.get("status") == "dry_run":
            continue
        local_by_key.setdefault(
            (pos["ticker"], pos["direction"]), [],
        ).append(pos)

    for (ticker, side), live_p in live_by_key.items():
        rows = local_by_key.get((ticker, side), [])
        qty = abs(_signed_qty(live_p))
        cost_cents = _cost_cents(live_p)
        if qty <= 0:
            continue

        # The live qty is the COMBINED on-chain position across every engine
        # holding this (ticker, side). If more than one local row matches (e.g.
        # a main-bot row + a copy row, or the crypto15m engine also holds it),
        # we can't safely attribute the whole qty to one row — doing so inflates
        # local exposure and double-books at settlement. Leave them as-is and
        # let each engine manage its own row. (Mirrors the per-order poll guard.)
        siblings = len(rows) + (1 if (ticker, side) in crypto15m_keys else 0)
        if siblings > 1:
            key = (ticker, side)
            last = _last_skip_import_log.get(key, 0.0)
            if (time.time() - last) >= 600:
                logger.info(
                    f"[reconcile] skip-merge {ticker} {side}: {siblings} local rows "
                    f"share this market (multi-engine) — leaving each to self-manage"
                )
                _last_skip_import_log[key] = time.time()
                cap_dict_size(_last_skip_import_log)
            continue

        if rows:
            # Prefer an active-status row, fall back to a terminal one
            # (gone/canceled) which we'll resurrect.
            active = next(
                (r for r in rows if r["status"] in ("submitted", "partial", "filled")),
                None,
            )
            target = active or rows[0]
            was_terminal = target["status"] in (
                "gone", "canceled", "expired", "error",
            )
            # If Polymarket's response omitted market_exposure (cost),
            # don't clobber a perfectly good local cost with a zero.
            # Same for avg_fill_price. Otherwise the position looks
            # like a "free" trade and downstream P&L math goes haywire.
            local_cost_usd = float(target.get("cost_usd") or 0.0)
            local_avg_cents = target.get("avg_fill_price_cents")
            new_cost_usd = (
                cost_cents / 100.0 if cost_cents > 0 else local_cost_usd
            )
            new_avg_cents = (
                (cost_cents / qty) if (qty and cost_cents > 0)
                else local_avg_cents
            )
            new_filled = int(round(qty))

            # Live mark price (Polymarket's current price for the held
            # outcome) → drives unrealized P&L on open positions in the UI.
            cur_price = _cur_price_cents(live_p)
            cur_mark = target.get("mark_price_cents")
            mark_moved = (
                cur_price is not None
                and (cur_mark is None or abs(float(cur_mark) - cur_price) >= 1.0)
            )

            # Skip a no-op DB write so the renderer doesn't get a
            # `position:update` flood every 30s for unchanged rows.
            # We still write whenever the row is being resurrected
            # from a terminal state (status flipped) or the mark price
            # moved ≥1c (so live P&L stays current) — that's the whole
            # point of the reconcile path.
            cur_filled = int(target.get("filled_contracts") or 0)
            cur_cost_usd = float(target.get("cost_usd") or 0.0)
            if (
                not was_terminal
                and target.get("status") == "filled"
                and cur_filled == new_filled
                and abs(cur_cost_usd - new_cost_usd) < 0.005
                and not mark_moved
            ):
                continue

            with db.get_db() as conn:
                db.update_bot_position(
                    conn, target["id"], status="filled",
                    filled_contracts=new_filled,
                    cost_usd=new_cost_usd,
                    avg_fill_price_cents=new_avg_cents,
                    error=None if was_terminal else target.get("error"),
                    **({"mark_price_cents": cur_price}
                       if cur_price is not None else {}),
                )
                db.log_event(
                    conn, target["id"], "reconcile",
                    note=(
                        "resurrected from "
                        f"{target['status']} via /portfolio/positions"
                    ) if was_terminal else "rescued via /portfolio/positions",
                )
                row = db.fetch_position_by_id(conn, target["id"])
            if row:
                changed.append(row)
            if was_terminal:
                summary["resurrected"] += 1
                logger.info(
                    f"[reconcile] RESURRECTED #{target['id']} "
                    f"({ticker} {side}, was {target['status']}) "
                    f"qty={qty:.0f} cost=${cost_cents / 100:.2f}"
                )
            else:
                summary["rescued"] += 1
        else:
            # SIBLING-ENGINE GUARD: a live position owned by the crypto15m engine
            # (which lives in a SEPARATE table, so it never matches a bot_positions
            # row) must NOT be adopted as an `external` main-bot row — that would
            # double-count its open/win/loss/realized P&L and exposure, and
            # double-book it at settlement (both engines paying out the same
            # on-chain position). The crypto15m executor manages it on its own.
            if (ticker, side) in crypto15m_keys:
                key = (ticker, side)
                last = _last_skip_import_log.get(key, 0.0)
                if (time.time() - last) >= 600:
                    logger.info(
                        f"[reconcile] skip-import {ticker} {side}: owned by the "
                        f"crypto15m engine (not a main-bot position)"
                    )
                    _last_skip_import_log[key] = time.time()
                    cap_dict_size(_last_skip_import_log)
                continue
            # ALREADY-SETTLED GUARD: never import a position whose market has
            # already resolved. Polymarket's `/portfolio/positions` keeps
            # listing losing (now-worthless) tokens long after settlement, and
            # winners auto-redeem out of the list. With no local row to match
            # (e.g. right after a Clear-History wipe, which drops the resolved
            # rows the dedupe below relies on), importing those residual losers
            # as fresh "open" positions made the next resolve pass book a pile
            # of phantom losses — the user's "clear history marked all my open
            # positions lost". A genuinely live position is on an UNSETTLED
            # market, so gate the import on that. Check the local markets cache
            # first (it survives a history wipe — no network), fall back to a
            # fetch only when the cached row is missing/stale.
            with db.get_db() as conn:
                _mrow = db.get_market(conn, ticker)
            settled = _mrow is not None and _market_yes_payout(_mrow) is not None
            if not settled:
                try:
                    _mkt = await fetch_market(ticker)
                    settled = _market_yes_payout(_mkt) is not None
                except Exception:
                    # FAIL CLOSED: can't verify the market is live → don't
                    # import this pass. During a partial outage, importing a
                    # residual settled token as a fresh "open" position books
                    # a phantom loss at the next resolve pass (the 2026-07-07
                    # drain pattern). A genuinely live position is re-offered
                    # on every 30s reconcile, so skipping costs one cycle,
                    # not the position.
                    continue
            if settled:
                key = (ticker, side)
                last = _last_skip_import_log.get(key, 0.0)
                if (time.time() - last) >= 600:
                    logger.info(
                        f"[reconcile] skip-import {ticker} {side}: market "
                        f"already settled (residual holding, not a live bet)"
                    )
                    _last_skip_import_log[key] = time.time()
                    cap_dict_size(_last_skip_import_log)
                continue
            # CRITICAL DEDUPE: Polymarket's `/portfolio/positions` keeps
            # listing markets we've already settled locally for several
            # minutes after `mark_resolved_positions` paid out the row
            # (cash settlement is async on Polymarket's side). Importing
            # those as fresh `external` positions caused the
            #   resolve → reimport → resolve → reimport
            # ghost loop the user reported as "open count flickers
            # 10→6 then back". It also burned through the daily-new
            # quota with reconcile-only rows.
            #
            # If we resolved a (ticker, direction, env) trio in the
            # last 24h, treat the lingering Polymarket position as the
            # tail of that already-booked trade — skip the import.
            with db.get_db() as conn:
                if db.recent_resolved_position_exists(
                    conn, ticker, side, env,
                ):
                    # Throttle to once per 10 min per (ticker, side) —
                    # otherwise this line would repeat every 30s
                    # reconcile cycle for as long as Polymarket keeps the
                    # already-booked position around (5-10 min after
                    # cash settlement). The user sees ~5 of these per
                    # cycle * many cycles = unreadable log.
                    key = (ticker, side)
                    last = _last_skip_import_log.get(key, 0.0)
                    if (time.time() - last) >= 600:
                        logger.info(
                            f"[reconcile] skip-import {ticker} {side}: "
                            f"already resolved within 24h "
                            f"(Polymarket cash-settlement still pending)"
                        )
                        _last_skip_import_log[key] = time.time()
                        cap_dict_size(_last_skip_import_log)
                    continue
            # No unresolved local row matches — but we may already have
            # RESOLVED rows for this market from earlier rounds. That's
            # fine; this is a brand-new round on the same market and
            # gets its own row. We just need a unique signal_id +
            # client_order_id (the table has UNIQUE(signal_source,
            # signal_id, network) and UNIQUE(client_order_id)
            # constraints — using constants for those was why every
            # import past the first one silently failed).
            now_ms = int(time.time() * 1000)
            # Hash gives a stable signal_id per (ticker, side, ms). The
            # millisecond suffix means re-running reconcile in a tight
            # loop never collides.
            signal_id = (
                abs(hash((ticker, side, now_ms))) % 2_000_000_000
            )
            client_id = f"ext-{ticker}-{side}-{now_ms}"
            try:
                with db.get_db() as conn:
                    new_id = db.insert_bot_position(conn, {
                        "signal_source": "external",
                        "signal_id": signal_id,
                        "ticker": ticker,
                        "event_ticker": live_p.get("event_ticker") or "",
                        "title": live_p.get("title") or ticker,
                        "category": live_p.get("category") or "",
                        "direction": side,
                        "action": "buy",
                        "target_contracts": int(round(qty)),
                        "limit_price_cents": (
                            int(round(cost_cents / qty)) if qty else 0
                        ),
                        "filled_contracts": int(round(qty)),
                        "avg_fill_price_cents": (
                            (cost_cents / qty) if qty else None
                        ),
                        "cost_usd": cost_cents / 100.0,
                        "client_order_id": client_id,
                        "order_id": None,
                        "status": "filled",
                        "confidence": 0.0,
                        "edge_pts": 0.0,
                        "signal_price": 0.0,
                        "network": env,
                    })
                    # Seed the live mark price so unrealized P&L shows
                    # immediately, not only after the next reconcile.
                    _mark = _cur_price_cents(live_p)
                    if _mark is not None:
                        db.update_bot_position(
                            conn, new_id, mark_price_cents=_mark,
                            _stamp_last_updated=False,
                        )
                    row = db.fetch_position_by_id(conn, new_id)
                if row:
                    changed.append(row)
                summary["imported_unknowns"] += 1
                logger.info(
                    f"[reconcile] imported external #{new_id} "
                    f"{ticker} {side} qty={qty:.0f} "
                    f"cost=${cost_cents / 100:.2f}"
                )
            except Exception as e:
                # Bubble import failures up to WARNING so they're
                # visible in the in-app log. Silent debug-level
                # logging is what masked this bug for so long.
                logger.warning(
                    f"[reconcile] import failed for {ticker} {side}: {e}",
                )

    return summary, changed


# ───────── external-exit detection ─────────────────────────────────


def _created_epoch(ts) -> float:
    """SQLite UTC timestamp string → epoch seconds; 0.0 if unparseable (so the
    caller's time filter degrades to 'include everything' rather than dropping
    valid events)."""
    if not ts:
        return 0.0
    s = str(ts).replace("T", " ").split(".")[0].replace("Z", "").strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _exit_proceeds(pos: dict, activity: list[dict]) -> Optional[dict]:
    """Cash the wallet realized exiting `pos`, from /activity. Matches the
    position's market (conditionId) and outcome (yes→index 0, no→index 1),
    counting only events at/after entry. Returns {proceeds, kind} or None when
    there's no SELL/REDEEM evidence (caller then leaves the row open).

    `proceeds` is NET of the exit's taker fee: a SELL row's usdcSize is the
    cash that landed, the venue having already withheld rate·p·(1−p) per
    share (a maker sell has nothing withheld). Callers must not charge
    `_sell_fee_usd` on top. When there were SELLs the result also carries
    `sell_gross` (Σ size·price) and `sell_fee` (what the venue withheld)."""
    ticker = pos.get("ticker")
    want_index = 0 if pos.get("direction") == "yes" else 1
    since = _created_epoch(pos.get("created_at")) - 3600.0  # 1h skew buffer
    sell_usdc = 0.0
    sell_gross = 0.0
    redeem_usdc = 0.0
    # Since 2026-08-10 /activity reports a redemption PER OUTCOME (one row per
    # token burned; the losing outcome's row pays usdcSize 0). A REDEEM row for
    # OUR outcome is therefore definitive even at $0 — it's a settled loss,
    # not "no evidence".
    redeemed_ours = False
    for ev in activity:
        if (ev.get("conditionId") or "") != ticker:
            continue
        try:
            ts = float(ev.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < since:
            continue
        idx = ev.get("outcomeIndex")
        try:
            idx = int(idx) if idx is not None else None
        except (TypeError, ValueError):
            idx = None
        if idx is not None and idx != want_index:
            continue  # different outcome of the same market — not our token
        etype = (ev.get("type") or "").upper()
        try:
            usdc = float(ev.get("usdcSize") or 0.0)
        except (TypeError, ValueError):
            usdc = 0.0
        if etype == "TRADE" and (ev.get("side") or "").upper() == "SELL":
            sell_usdc += usdc
            try:
                gross = float(ev.get("size") or 0.0) * float(ev.get("price") or 0.0)
            except (TypeError, ValueError):
                gross = 0.0
            # No size/price on the row: take the cash as gross (no fee known).
            sell_gross += gross if gross > 0 else usdc
        elif etype == "REDEEM":
            redeem_usdc += usdc
            if idx == want_index:
                redeemed_ours = True
    total = sell_usdc + redeem_usdc
    if total <= 0:
        if redeemed_ours:
            return {"proceeds": 0.0, "kind": "redeemed"}
        return None
    out = {"proceeds": total, "kind": "redeemed" if redeem_usdc >= sell_usdc else "sold"}
    if sell_usdc > 0:
        out["sell_gross"] = round(sell_gross, 6)
        out["sell_fee"] = round(max(0.0, sell_gross - sell_usdc), 6)
    return out


async def detect_external_exits(cfg: dict) -> list[dict]:
    """Resolve local OPEN positions the user exited DIRECTLY on Polymarket.

    `reconcile_positions_with_polymarket` only UPDATES positions still held in
    the wallet — it never closes one that has LEFT the wallet. So a position you
    SELL on Polymarket itself (or that settles and redeems) just vanishes from
    /positions and would show 'open' in the app forever. This sweep finds
    unresolved filled rows no longer held on-chain and books them from the
    wallet's /activity history: a SELL is a discretionary close at its real
    proceeds, a REDEEM is a settlement payout. Returns the rows it closed.

    SAFETY: requires a healthy positions response (≥1 non-zero holding) before
    closing anything — a transient empty/garbled response must not mass-resolve
    every open position. Skips markets with multiple local rows or crypto15m
    ownership (can't attribute one wallet exit to one row), and leaves a gone
    position OPEN when there's no SELL/REDEEM evidence (never guesses)."""
    env = get_env()
    try:
        live = await get_positions()
    except Exception as e:
        logger.debug(f"[exits] positions fetch failed: {e}")
        return []
    held_keys: set = set()
    for p in live:
        try:
            qty = float(p.get("position_fp") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0:
            continue
        held_keys.add((p.get("ticker") or "", "yes" if qty > 0 else "no"))
    if not held_keys:
        # No live holdings at all — likely a transient/garbled API response, not
        # a genuinely empty wallet. Don't mass-close; let a later pass with real
        # data handle it.
        logger.debug("[exits] no live holdings returned — skipping exit sweep (guard)")
        return []

    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bot_positions WHERE network=? AND resolved=0 "
            "AND status IN ('filled','partial')",
            (env,),
        ).fetchall()]
        crypto15m_keys = db.crypto15m_owned_keys(conn, env)

    by_key: dict[tuple[str, str], list] = {}
    for r in rows:
        by_key.setdefault((r["ticker"], r["direction"]), []).append(r)

    candidates: list[dict] = []
    for key, group in by_key.items():
        if key in held_keys or key in crypto15m_keys:
            continue
        if len(group) != 1:
            logger.info(
                f"[exits] skip {key[0]} {key[1]}: {len(group)} local rows share this "
                f"market — can't attribute one wallet exit; review manually"
            )
            continue
        candidates.append(group[0])
    if not candidates:
        return []

    try:
        activity = await get_activity(limit=1000)
    except Exception as e:
        logger.debug(f"[exits] activity fetch failed: {e}")
        return []

    changed: list[dict] = []
    for pos in candidates:
        info = _exit_proceeds(pos, activity)
        if info is None:
            logger.info(
                f"[exits] {pos['ticker']} {pos['direction']} no longer held but no "
                f"SELL/REDEEM found in recent activity — leaving open for review"
            )
            continue
        cost = float(pos.get("cost_usd") or 0.0)
        proceeds = round(info["proceeds"], 4)
        # Only the entry fee is left to charge: a manual SELL's taker fee was
        # already withheld from the /activity cash, and a REDEEM settles free.
        # (Subtracting _sell_fee_usd here as well charged that fee twice.)
        fee = _position_fee_usd(pos)
        pnl = round(proceeds - cost - fee, 4)
        correct = 1 if pnl > 0.05 else (0 if pnl < -0.05 else None)
        with db.get_db() as conn:
            fields = dict(resolved=1, outcome_correct=correct,
                          settlement_usd=proceeds, pnl_usd=pnl)
            # The withheld exit fee still counts toward the fees paid.
            fee += float(info.get("sell_fee") or 0.0)
            if fee > 0 and not float(pos.get("fees_usd") or 0.0):
                fields["fees_usd"] = round(fee, 6)
            db.update_bot_position(conn, pos["id"], **fields)
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now'), closed_early=1 "
                "WHERE id=?",
                (pos["id"],),
            )
            db.log_event(
                conn, pos["id"], "external_exit",
                note=f"{info['kind']} on Polymarket: proceeds=${proceeds:.2f} pnl=${pnl:+.2f}",
            )
            row = db.fetch_position_by_id(conn, pos["id"])
        if row:
            changed.append(row)
        logger.info(
            f"[exits] booked #{pos['id']} {pos['ticker']} {pos['direction']} as "
            f"{info['kind']} (proceeds=${proceeds:.2f}, pnl=${pnl:+.2f})"
        )
    return changed


# ───────── flatten / cancel-all ────────────────────────────────────


async def cancel_all_resting_orders(reason: str = "cancel-all") -> int:
    """Cancel every app-tracked *resting* main-engine order for the active
    wallet + network and mark each canceled in the DB. Returns the count
    successfully canceled.

    Scope is deliberately narrow and conservative:
      - Only `bot_positions` rows with status in ('submitted','partial') and
        resolved=0 — i.e. orders WE placed that could still be live on the
        book. Filled/settled/gone rows are never touched, so we never cancel
        something that already matched.
      - crypto15m ENTRY orders are also swept: taker mode uses marketable FAK
        that never rests, but maker-style entries rest as GTC and could fill
        unmanaged after shutdown. Resting crypto15m EXIT asks (sell-into-
        strength) are deliberately left alone — one filling while the app is
        down closes the position at our own price, and the chain reconcile /
        external-exit sweep books it on the next boot.
      - A 404 (order already off the book — filled or previously canceled)
        leaves the DB row untouched: the reconcile that follows a lifecycle
        cancel will classify it (e.g. import a real fill) instead of us
        mislabeling a filled order as canceled.

    Used at lifecycle boundaries (clean shutdown, startup stale-order sweep)
    so an unclean exit can't leave GTC orders resting on Polymarket that fill
    later unmanaged, and also backs the user-facing "cancel all" RPC.
    """
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, order_id FROM bot_positions
               WHERE status IN ('submitted','partial')
                 AND resolved=0 AND network=?""",
            (get_env(),),
        ).fetchall()
    # crypto15m maker-style GTC entries (0 fills so far). Cancel on the book;
    # only mark the ROW canceled when nothing filled — a partial stays as-is
    # for the wallet reconcile to classify (never mislabel a fill).
    with db.get_db() as conn:
        c15_rows = conn.execute(
            """SELECT id, order_id, filled_contracts FROM crypto15m_positions
               WHERE status IN ('submitted','partial') AND resolved=0
                 AND order_id IS NOT NULL AND network=?""",
            (get_env(),),
        ).fetchall()
    # One batched DELETE /orders for everything (was one request per order —
    # a shutdown with many resting orders spent seconds on round-trips).
    ids = [r["order_id"] for r in rows if r["order_id"]] + [r["order_id"] for r in c15_rows]
    if not ids:
        return 0
    res = await cancel_orders(ids)
    done = res["canceled"]
    for oid, why in res["not_canceled"].items():
        # e.g. already filled / already gone: leave the row for reconcile to
        # reclassify. Never lose a fill.
        logger.warning(f"cancel_all_resting ({reason}): {oid}: {why}")
    canceled = 0
    with db.get_db() as conn:
        for r in rows:
            if r["order_id"] and str(r["order_id"]) in done:
                db.update_bot_position(conn, r["id"], status="canceled", error=reason)
                db.log_event(conn, r["id"], "cancel", note=reason)
                canceled += 1
        for r in c15_rows:
            if str(r["order_id"]) not in done:
                continue
            if not int(r["filled_contracts"] or 0):
                conn.execute(
                    """UPDATE crypto15m_positions
                          SET status='canceled', error=?, last_updated=datetime('now')
                        WHERE id=? AND COALESCE(filled_contracts,0)=0""",
                    (reason, r["id"]),
                )
            canceled += 1
    return canceled


async def cancel_all_open() -> int:
    """User-triggered 'cancel all resting orders' RPC (thin wrapper)."""
    return await cancel_all_resting_orders("user cancel-all")


async def _confirm_sell(oid: str | None, qty: int, px: int) -> tuple[int, float]:
    """(sold_qty, avg_price_cents) for a sell order. A 404 says nothing about a
    fill, so it counts as no fill: the position stays held and the next sweep reads
    the wallet. (An order whose reply was lost has a placeholder id, which also
    404s; assuming a sale there would drop a position still held.)"""
    if not oid:
        return 0, 0.0
    for _ in range(4):
        try:
            resp = await get_order(oid)
            order = (resp.get("order") if isinstance(resp, dict) else resp) or {}
            parsed = _parse_polymarket_order(order)
            f = int(parsed.get("filled") or 0)
            if f > 0:
                return f, float(parsed.get("avg_cents") or px)
            if parsed.get("status") in ("canceled", "cancelled"):
                return 0, 0.0
        except PolymarketAPIError as e:
            if e.status == 404:
                return 0, 0.0
        except Exception:
            pass
        await asyncio.sleep(0.6)
    return 0, 0.0


async def flatten_open_positions(cfg: dict) -> dict:
    """Hard exit: cancel resting orders AND market-sell every open filled
    position, booking realized P&L from the sale. The main engine is otherwise
    buy-and-hold-to-settlement, so this is the manual 'cut everything now'
    button — and the only way to actually act on a daily stop-loss.
    Returns {canceled, sold, proceedsUsd}."""
    env = get_env()
    canceled = await cancel_all_open()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd
               FROM bot_positions
               WHERE status='filled' AND resolved=0 AND network=? AND filled_contracts > 0""",
            (env,),
        ).fetchall()
        open_filled = [dict(r) for r in rows]

    sold_total = 0
    proceeds_total = 0.0
    for pos in open_filled:
        sold, proceeds = await _liquidate_position(pos, cfg, reason="flatten")
        sold_total += sold
        proceeds_total += proceeds
    return {"canceled": canceled, "sold": sold_total, "proceedsUsd": round(proceeds_total, 2)}


async def take_profit_sweep(cfg: dict) -> list[dict]:
    """Per-position take-profit: SELL any open position whose live best BID is now
    worth >= take_profit_pct ABOVE its cost, instead of holding to settlement. The
    win-side complement of the daily-aggregate take_profit_on_day (which only
    halts NEW entries — it never sells an existing winner). Percent auto-scales to
    entry price: a near-$1 favorite can't reach the target so it naturally holds;
    a mid-priced entry cashes out on a modest move. Returns the rows it closed
    (for position:update emits). No-op when take_profit_pct <= 0, so it adds no
    book load when off."""
    try:
        pct = float(cfg.get("take_profit_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pct = 0.0
    pct = max(0.0, min(1.0, pct))
    if pct <= 0:
        return []
    env = get_env()
    with db.get_db() as conn:
        rows = conn.execute(
            """SELECT id, ticker, direction, filled_contracts, cost_usd
               FROM bot_positions
               WHERE status='filled' AND resolved=0 AND network=? AND filled_contracts > 0""",
            (env,),
        ).fetchall()
        open_filled = [dict(r) for r in rows]
    closed: list[dict] = []
    for pos in open_filled:
        qty = int(pos["filled_contracts"] or 0)
        cost = float(pos["cost_usd"] or 0.0)
        if qty <= 0 or cost <= 0:
            continue
        # Gate on the REAL best bid (what we can actually sell for), not a lagged
        # mark — a take-profit must only fire when the profit is genuinely there.
        try:
            q = await get_quote(pos["ticker"], pos["direction"])
            bid = q.get("bid_cents")
        except Exception:
            bid = None
        if not bid or int(bid) <= 0:
            continue
        cur_value = qty * (int(bid) / 100.0)
        if (cur_value - cost) / cost < pct:
            continue
        sold, _proceeds = await _liquidate_position(pos, cfg, reason="take_profit")
        if sold > 0:
            with db.get_db() as conn:
                row = db.fetch_position_by_id(conn, pos["id"])
            if row:
                closed.append(row)
    return closed


async def _liquidate_position(pos: dict, cfg: dict, *, reason: str) -> tuple[int, float]:
    """Cross the book to SELL a filled position and book the row on the result.
    Shared by the manual flatten and the take-profit sweep — both exit the same
    way; only the trigger and the `reason` (written to exit_reason and the event
    log) differ. Returns (contracts_sold, proceeds_usd).

    A single cross only takes top-of-book depth, so a large position in a thin
    book PARTIALLY fills. Retry the remainder (cancelling each resting remainder
    first) until fully out or the book runs dry — instead of booking the whole row
    closed on a partial fill, which used to orphan the unsold shares on-chain and
    mis-book pnl (proceeds for the sold slice vs the FULL cost basis => phantom
    loss). Cross DOWN to the current bid each attempt (1c floor = dump if there's
    no bid). A full exit resolves the row with true proceeds vs full cost basis; a
    partial exit shrinks the row to what's still held so exposure stays correct
    and the remainder rides to the next sweep or to settlement."""
    ticker, direction = pos["ticker"], pos["direction"]
    qty = int(pos["filled_contracts"] or 0)
    cost = float(pos["cost_usd"] or 0.0)
    if qty <= 0:
        return 0, 0.0
    remaining = qty
    pos_sold = 0
    pos_proceeds = 0.0
    for _attempt in range(6):
        if remaining <= 0:
            break
        try:
            q = await get_quote(ticker, direction)
            bid = q.get("bid_cents")
        except Exception:
            bid = None
        sell_px = max(1, min(99, int(bid) if bid else 1))
        coid = f"krypt-{reason[:4]}-{pos['id']}-{uuid.uuid4().hex[:6]}"
        try:
            resp = await place_limit_order(
                ticker=ticker, side=direction, action="sell",
                count=remaining, price_cents=sell_px, client_order_id=coid,
            )
        except Exception as e:
            logger.warning(f"[{reason}] sell {ticker} failed: {e}")
            break
        order = (resp.get("order") if isinstance(resp, dict) else None) or {}
        oid = order.get("order_id")
        sold, avg_cents = await _confirm_sell(oid, remaining, sell_px)
        if sold <= 0:
            # Nothing crossed (no bid / illiquid). Cancel any resting order so it
            # can't fill later unexpectedly, and stop — the remainder stays held
            # and is retried on the next sweep.
            if oid:
                try:
                    await cancel_order(oid)
                except Exception:
                    pass
            logger.warning(f"[{reason}] {ticker}: sell didn't fill; still holding {remaining}")
            break
        pos_sold += sold
        pos_proceeds += sold * avg_cents / 100.0
        remaining -= sold
        if remaining > 0 and oid:
            # A resting remainder locks shares and would fill unpredictably —
            # cancel it before re-crossing at the new (lower) bid.
            try:
                await cancel_order(oid)
            except Exception:
                pass

    if pos_sold <= 0:
        return 0, 0.0
    avg_c = pos_proceeds * 100.0 / pos_sold
    if remaining <= 0:
        # Fully exited — book the whole row resolved with the TRUE total proceeds
        # against the full cost basis, net of the entry fee and the crossing
        # sell's taker fee (this sweep always crosses at the bid).
        fee = _position_fee_usd(pos) + _sell_fee_usd(pos, pos_sold, avg_c)
        pnl = pos_proceeds - cost - fee
        correct = 1 if pnl > 0.05 else (0 if pnl < -0.05 else None)
        with db.get_db() as conn:
            fields = dict(resolved=1, outcome_correct=correct,
                          settlement_usd=pos_proceeds, pnl_usd=pnl,
                          exit_reason=reason)
            if fee > 0 and not float(pos.get("fees_usd") or 0.0):
                fields["fees_usd"] = round(fee, 6)
            db.update_bot_position(conn, pos["id"], **fields)
            conn.execute(
                "UPDATE bot_positions SET resolved_at=datetime('now'), closed_early=1 WHERE id=?",
                (pos["id"],),
            )
            db.log_event(conn, pos["id"], reason,
                         note=f"sold {pos_sold} @ ~{avg_c:.0f}c pnl=${pnl:+.2f}")
        logger.info(f"[{reason}] {ticker} sold {pos_sold} @ ~{avg_c:.0f}c pnl=${pnl:+.2f}")
    else:
        # Only a PARTIAL exit was possible (thin book). Do NOT resolve — reduce the
        # row to what we still hold and shrink its cost basis proportionally, so
        # exposure stays correct and the unsold shares aren't orphaned. The
        # remainder rides to the next sweep or to settlement.
        sold_cost = cost * (pos_sold / qty)
        with db.get_db() as conn:
            db.update_bot_position(
                conn, pos["id"],
                filled_contracts=remaining,
                cost_usd=round(max(0.0, cost - sold_cost), 6),
            )
            db.log_event(conn, pos["id"], f"{reason}_partial",
                         note=f"sold {pos_sold}/{qty} @ ~{avg_c:.0f}c; holding {remaining}")
        logger.warning(
            f"[{reason}] {ticker} PARTIAL sold {pos_sold}/{qty} @ ~{avg_c:.0f}c; still holding {remaining}"
        )
    return pos_sold, pos_proceeds
