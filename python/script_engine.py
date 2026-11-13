"""Live execution engine for user strategy scripts.

Ticked from the service master loop like every other engine. Returned order
intents are sanitized, pushed through the money rails, and placed via the
same CLOB order path as the built-in engine, so script positions inherit the
existing lifecycle machinery (fill polling, settlement, reconcile,
never-strand-a-position) for free.

Four entry hooks, each landing in the table whose engine already manages
that kind of position — every row tagged with a `script_id` column:

  • decide(ctx)            crypto Up/Down windows, from the shared crypto15m
                           snapshot (already ~3s-cached, so scripts add no
                           extra API load) → crypto15m_positions
  • decide_market(market)  the top general markets by volume (politics,
                           sports, news; crypto windows excluded so the two
                           hooks can't both enter one window) → bot_positions
  • decide_signal(signal)  new whale/momentum signals → bot_positions
  • manage(position, ctx)  dynamic exits over open crypto positions

INDEPENDENT OF THE OTHER ENGINES. A script never needs the main bot, the
crypto15m engine, or copy-trading to be switched on:

  • it fetches its own crypto snapshot (the same 3s-cached one the Crypto tab
    uses), so decide() works with crypto15m_enabled OFF;
  • which coins it sees comes from ITS OWN `assets` scope, not the 15m Crypto
    tab's asset checkboxes — those used to silently filter every script, so
    applying a BTC-only preset there made every other script's decide() go
    quiet, live AND in backtest, with nothing saying why;
  • the whale/momentum collector is force-enabled whenever an enabled script
    defines decide_signal (see needs_signal_feed), so signal scripts don't
    depend on the main engine's recording toggle;
  • positions it opens are managed to settlement by passes that are gated on
    auth alone, never on an engine switch.

The ONE thing scripts share with the rest of the app is the wallet: entries
still need a connected account. Shadow scripts, which never touch the
exchange, run as soon as they're enabled — the `scripts_live_enabled` master
switch gates REAL orders only, exactly as its label says.

Safety rails (unchanged by the removal of the language sandbox — scripts are
full Python now, but the money path is not theirs to widen):
  • script_max_entry_cents  — hard cap on the submit price
  • script_max_contracts    — per-order size cap
  • script_max_open         — open positions per script
  • script_daily_loss_usd   — per-script realized daily loss → auto-disable
  • one entry attempt per script per market window (attempted = a position
    row exists for that script+ticker, including 0-contract refusal rows)

Any script exception / budget breach auto-disables the script and surfaces
in the UI — the engine loop itself never sees a script error.

HEARTBEAT: every `HEARTBEAT_SEC` each enabled script gets one line — ticks
run, hook calls, intents returned, orders, errors — to backend.log and to the
UI's Script log panel. Without it a correct script with no setup today looks
exactly like a dead one, since that panel otherwise only carries lines the
script itself emits via log().

SHADOW MODE (`user_scripts.dry_run`, ON for every new script): the script
runs against the same live ticks and through every rail above, but the
exchange call is replaced by a row in `script_shadow_orders` — what it WOULD
have sent. Those rows settle passively from the recorded window/signal
outcome (charged the same taker fee), so a script builds a real forward
track record before it is ever armed. Shadow scripts are measured against
their own ledger for the one-per-window rule, the open-position cap, and the
daily-loss breaker, so behavior matches live. Caveat, surfaced in the UI:
entries are simulated, EXITS are not — manage() sells and tp/sl triggers
need a live position, so shadow orders are always held to settlement.
"""
from __future__ import annotations

import copy
import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

from datetime import datetime, timezone

import categorize
import crypto15m
import crypto15m_trader
import db
import polymarket_api
import polymarket_auth
import script_quotes
import script_sandbox
import trader
from script_backtest import (
    market_to_js, parse_asset_scope, sanitize_intent, sanitize_manage,
    sanitize_market_intent, sanitize_signal_action, script_allows_asset,
    signal_to_js,
)

logger = logging.getLogger("krypt.script_engine")

# ── supervisor whitelist ─────────────────────────────────────────────────
# The ONLY config keys a supervise() hook may set. Everything else is
# rejected (and logged). Values are re-sanitized by merge_with_defaults when
# the patch round-trips through the settings store, so a script can never
# smuggle an out-of-range value past the normal config validation.
_SUPERVISE_KEYS: dict[str, str] = {
    # engine on/off switches — "dynamic engine switching"
    "enable_trading": "bool",
    "crypto15m_enabled": "bool",
    # crypto strategy posture
    "crypto15m_direction_mode": "mode",     # favorite | contrarian | model
    "crypto15m_entry_threshold": "float",
    "crypto15m_entry_max": "float",
    "crypto15m_min_delta_pct": "float",
    "crypto15m_time_delay_min": "float",
}
_SUPERVISE_MIN_INTERVAL_S = 30.0   # per-script patch rate limit
_last_patch_t: dict[str, float] = {}

# High-water marks for the signal stream (whale_trades.id / alerts.id).
# Initialized to the current MAX on first pass so scripts never replay the
# historical backlog as "new" signals.
_sig_marks: dict[str, Optional[int]] = {"whale": None, "momentum": None}

# Wall-clock ceiling for ONE hook call, and for the module body + on_start
# (which can legitimately do more setup work). Scripts are full Python and run
# untraced, so this thread timeout is the ONLY thing standing between a
# runaway hook and a frozen master loop: every hook runs in a joined daemon
# thread and a hook still alive at the deadline is abandoned (the thread
# leaks, logged) while the script is auto-disabled. One strike, because a
# script that hangs once will hang every tick.
#
# Overridable for the rare hook that must do something slow — but understand
# what you're raising: the master loop, including every open position's exit
# pass, waits this long behind the script.
HOOK_TIMEOUT_S = 1.0
COMPILE_TIMEOUT_S = 4.0
# Wall-clock ceiling for ONE script's whole decide_market scan. Per-call budgets
# bound each hook, not the scan: at the 150-market universe a script that sits
# near its per-call budget could block the master loop for 7.5s per tick, ahead
# of the crypto manage/exit pass. Over-budget scans resume next tick.
MARKET_SCAN_BUDGET_S = 2.0
STATE_SAVE_EVERY_S = 60.0
# Ceiling for a script's persisted `state` dict. Oversized state is dropped
# (see _save_states) rather than truncated into unparseable JSON.
STATE_MAX_BYTES = 64 * 1024

# ── heartbeat ────────────────────────────────────────────────────────────
# A working script that simply has no setup right now is INDISTINGUISHABLE
# from a broken one: it places nothing, and the Script log stays empty
# because that panel only carries lines the script itself emits via log() —
# which none of the shipped examples call. Two days of correct-but-silent
# running is exactly the failure mode a trading tool must not have.
#
# So the engine narrates itself: every HEARTBEAT_SEC each enabled script
# gets one line saying how many ticks ran, how many times each of its hooks
# was called, and how many intents came back. It goes to backend.log AND to
# the same `script:log` stream the UI panel renders, so "0 intents from 1,800
# calls" reads as healthy-but-quiet instead of as nothing at all.
HEARTBEAT_SEC = 60.0
# script_id -> counters for the current heartbeat interval
_hb: dict[str, dict[str, int]] = {}
# script_id -> when we last emitted (absent = emit on the very next tick, so
# a freshly started or freshly saved script confirms itself within one tick
# instead of leaving the panel empty for a minute)
_hb_last: dict[str, float] = {}

# script_id -> CompiledScript (compiled lazily, recompiled on hash change)
_compiled: dict[str, script_sandbox.CompiledScript] = {}
# positions already delivered to on_fill / on_settle this process, keyed
# "<table-tag><id>" because the two position tables have independent ids
_notified_fill: set[str] = set()
_notified_settle: set[str] = set()
# script ids already warned about an unpersistable (oversized) state
_state_oversize_warned: set[str] = set()
# names of enabled scripts currently starved by script_max_enabled
_last_starved: list[str] = []
_last_state_save = 0.0

# ── runtime status, for an honest Scripts page ───────────────────────────
# The page used to show only `enabled` + shadow/live, which is not what
# determines whether a script is actually doing anything: a script can read
# ENABLED and still be running zero times because the wallet is disconnected,
# because it's armed while the master switch is off, or because it's past the
# script_max_enabled cap. Every gate the tick applies records itself here, and
# the row handed to the UI says which one is in force.
#
# script_id -> {"ticks", "intents", "orders", "lastTickAt", "lastIntentAt",
#               "lastOrderAt", "gate"}
_status: dict[str, dict] = {}
# Union of hook names across the enabled scripts on the last tick — what
# needs_signal_feed() reads so the collector can be kept alive for
# decide_signal scripts without the main engine being on.
_enabled_hooks: set[str] = set()
_enabled_hooks_at = 0.0
# How long a hook census survives without a tick before we stop asserting it.
_HOOKS_TTL_S = 120.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stat(sid: str) -> dict:
    return _status.setdefault(sid, {
        "ticks": 0, "intents": 0, "orders": 0,
        "lastTickAt": None, "lastIntentAt": None, "lastOrderAt": None,
        "gate": None,
    })


def _gate(sid: str, reason: Optional[str]) -> None:
    """Record WHY a script did nothing this tick (None = it ran)."""
    _stat(sid)["gate"] = reason


def status_for(sid: str) -> dict:
    """Runtime counters for one script, for the Scripts page. Never raises."""
    return dict(_status.get(sid) or _stat(sid))


def needs_signal_feed() -> bool:
    """True when an enabled script defines decide_signal, so the service loop
    keeps the whale/momentum collector running even with the main engine's
    recording toggle and enable_trading both off.

    Without this a signal script was silently dead: decide_signal reads rows
    written by scan_whales/scan_momentum, which the loop only ran when the
    MAIN engine wanted them. Expires after _HOOKS_TTL_S so a stopped script
    engine stops holding the collector up."""
    if not _enabled_hooks or time.time() - _enabled_hooks_at > _HOOKS_TTL_S:
        return False
    return "decide_signal" in _enabled_hooks


# async event emitter injected by service.py (emit_event)
_emit: Optional[Callable] = None


def set_event_callback(cb: Callable) -> None:
    global _emit
    _emit = cb


async def _emit_event(name: str, data: Any) -> None:
    if _emit is None:
        return
    try:
        await _emit(name, data)
    except Exception:
        pass


# Scripts the app turned off since it started, for the alerts. `last_error` can't
# say this: it stays on the row forever, and an alert about last week's failure on
# a script the user has moved on from would never go away.
stopped_this_run: dict[str, str] = {}


def _disable(sid: str, reason: str) -> None:
    """Auto-disable a script and record why. Never raises.

    DROPS the compiled module too. It used to be left in `_compiled`, which
    meant (a) `_save_states` kept persisting the state of a disabled script —
    including a state dict an abandoned hook thread is still writing into, so
    the last GOOD snapshot was overwritten with a half-finished one — and
    (b) re-enabling handed back the SAME module object, with the runaway
    thread's scribbles still in `state` and its `on_start` never re-run.
    Dropping it makes a re-enable compile fresh from the row and reload the
    last persisted (good) state."""
    _compiled.pop(sid, None)
    script_quotes.forget(sid)
    stopped_this_run[sid] = reason
    try:
        with db.get_db() as conn:
            db.update_user_script(conn, sid, enabled=0, last_error=reason[:500])
        logger.warning(f"[scripts] disabled {sid[:8]}: {reason}")
    except Exception as e:
        logger.error(f"[scripts] failed to persist disable for {sid[:8]}: {e}")


def _get_compiled(row: dict, cfg: dict) -> Optional[script_sandbox.CompiledScript]:
    """Compiled module for a script row, (re)compiling on code change.
    Compile failure disables the script. Restores persisted state."""
    sid = str(row["id"])
    want = script_sandbox.code_hash(str(row.get("code") or ""))
    cur = _compiled.get(sid)
    if cur is not None and cur.hash == want and not cur.abandoned.is_set():
        return cur
    if cur is not None and cur.abandoned.is_set():
        # Retired module (a hook overran and its thread was abandoned with the
        # state dict in hand). Recompile from the row so the fresh module gets
        # the last GOOD persisted state, not the runaway thread's copy.
        _compiled.pop(sid, None)
    if str(row.get("author") or "").startswith("agent"):
        # Agent-written code is held to the agent allowlist wherever it runs,
        # not only where it was saved: an older row, or a rule tightened since.
        import script_audit
        bad = script_audit.validate_agent(str(row.get("code") or ""))
        if bad:
            _compiled.pop(sid, None)
            _disable(sid, "agent-written code breaks the agent-script rules: " + bad[0])
            return None
    saved_state: dict = {}
    try:
        saved_state = json.loads(row.get("state_json") or "{}")
        if not isinstance(saved_state, dict):
            saved_state = {}
    except Exception:
        saved_state = {}
    try:
        # Module body executes during construction; bound it (and on_start) on
        # a worker thread so a hang there can't freeze the master loop.
        mod = _run_bounded(
            lambda: script_sandbox.CompiledScript(
                sid, str(row.get("code") or ""), state=saved_state),
            _compile_timeout(cfg), f"script-{sid[:8]}-compile")
        _call_hook(mod, "on_start", cfg, mod.state)
    except (Exception, script_sandbox.ScriptBudgetExceeded) as e:  # + budget (BaseException)
        _compiled.pop(sid, None)
        _disable(sid, f"compile/on_start failed: {e}")
        return None
    _compiled[sid] = mod
    return mod


def _run_bounded(fn: Callable[[], Any], timeout_s: float, name: str,
                 on_abandon: Optional[Callable[[], None]] = None) -> Any:
    """Run fn() in a joined daemon thread with a hard wall-clock timeout and
    surface its return/exception. A callable that blocks past the timeout —
    a runaway loop, a blocking network call, an un-interruptible C-level op —
    is ABANDONED (thread leaks, logged) so the engine master loop keeps
    running, and we raise ScriptBudgetExceeded so the caller auto-disables the
    script. Scripts are full Python with no in-thread trace budget, so this is
    the only containment there is.

    `on_abandon` fires (before the raise) when the thread is still alive at
    the timeout. There is no way to kill it, so the callback's job is to mark
    what it is still touching as untrustworthy — see CompiledScript.abandoned:
    the module's `state` dict keeps taking writes from that thread forever,
    and persisting it would checkpoint a half-finished mutation."""
    import threading
    result: dict[str, Any] = {}

    def _run() -> None:
        try:
            result["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — ScriptError/Budget/anything
            result["error"] = e

    t = threading.Thread(target=_run, daemon=True, name=name)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        if on_abandon is not None:
            try:
                on_abandon()
            except Exception:
                pass
        raise script_sandbox.ScriptBudgetExceeded(
            f"{name} still running after {timeout_s:.2f}s (thread abandoned)")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _hook_timeout(cfg: dict) -> float:
    try:
        return max(0.05, min(30.0, float(cfg.get("script_hook_timeout_sec")
                                         or HOOK_TIMEOUT_S)))
    except (TypeError, ValueError):
        return HOOK_TIMEOUT_S


def _compile_timeout(cfg: dict) -> float:
    return max(COMPILE_TIMEOUT_S, _hook_timeout(cfg) * 4.0)


def _call_hook(mod: script_sandbox.CompiledScript, hook: str, cfg: dict,
               *args: Any) -> Any:
    """Call a hook with a wall-clock backstop. Scripts are full Python and run
    untraced, so this thread timeout is the whole containment story: a hook
    that blocks past it is abandoned and the script auto-disabled, and the
    service master loop keeps its cadence regardless."""
    return _run_bounded(
        lambda: mod.call(hook, *args),
        _hook_timeout(cfg), f"script-{mod.script_id[:8]}-{hook}",
        on_abandon=mod.abandoned.set)


def _script_daily_pnl(conn, sid: str, env: str) -> float:
    """Realized P&L today across BOTH position tables (crypto windows +
    main-market signal copies) — the per-script daily-loss breaker's input."""
    total = 0.0
    for table, stamp in (("crypto15m_positions", "COALESCE(resolved_at, last_updated)"),
                         ("bot_positions", "COALESCE(resolved_at, last_updated)")):
        try:
            row = conn.execute(
                f"""SELECT COALESCE(SUM(pnl_usd), 0) FROM {table}
                    WHERE script_id=? AND network=? AND resolved=1
                      AND date({stamp}) = date('now')""",
                (sid, env),
            ).fetchone()
            total += float(row[0] or 0.0)
        except Exception:
            pass
    return total


def _count_open_for_script(conn, sid: str, env: str) -> int:
    n = 0
    for table in ("crypto15m_positions", "bot_positions"):
        try:
            n += conn.execute(
                f"""SELECT COUNT(*) FROM {table}
                    WHERE script_id=? AND network=? AND resolved=0""",
                (sid, env),
            ).fetchone()[0]
        except Exception:
            pass
    return n


def _open_crypto_for_script(conn, sid: str, env: str) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM crypto15m_positions
           WHERE script_id=? AND network=? AND resolved=0""",
        (sid, env),
    ).fetchall()
    return [dict(r) for r in rows]


def _already_attempted(conn, sid: str, ticker: str, *,
                       shadow: bool = False) -> bool:
    """One entry attempt per script per market window. Shadow scripts check
    their own ledger so the rule behaves identically in both modes (and so
    arming a script live isn't blocked by its shadow history)."""
    if shadow:
        return db.shadow_already_attempted(conn, sid, ticker)
    return conn.execute(
        "SELECT 1 FROM crypto15m_positions WHERE script_id=? AND ticker=? LIMIT 1",
        (sid, ticker),
    ).fetchone() is not None


def _shadow_fee(cost: float, category: str) -> float:
    """Taker fee per contract for a simulated fill — the same fee the live
    path books (the full venue fee, not net of the rebate tier), so shadow
    P&L isn't flattered by ignoring fees and compares like for like."""
    import backtest as bt
    return bt.venue_fee_per_contract(cost, category=category)


async def _settle_shadow_orders() -> None:
    """Book simulated outcomes for shadow orders whose window/signal has
    since resolved. Cheap: only unresolved rows are scanned.

    decide_market() rows need a LIVE API lookup — the local `markets` table
    never records an outcome and drops closed markets on the next sync, so a
    table-only settle could never fire and those rows stayed open forever,
    eating the script's open-position cap (which gates every hook). Only
    tickers we still can't settle are fetched, so a quiet board costs nothing.
    """
    try:
        with db.get_db() as conn:
            pending = [str(r[0]) for r in conn.execute(
                """SELECT DISTINCT ticker FROM script_shadow_orders
                   WHERE resolved=0 AND contracts > 0
                     AND signal_source='market' AND ticker IS NOT NULL
                     AND close_time != ''
                     AND close_time < strftime('%Y-%m-%dT%H:%M:%SZ','now')
                   LIMIT 60""").fetchall()]
        results: dict[str, str] = {}
        if pending:
            markets = await polymarket_api.fetch_markets_map(pending)
            for tk, m in (markets or {}).items():
                status = str((m or {}).get("status") or "").lower()
                res = str((m or {}).get("result") or "").lower()
                # Same gate the whale/alert resolvers use — a market is only
                # settled once the exchange says so, not merely past its close.
                if status in ("determined", "finalized", "settled") and res in ("yes", "no"):
                    results[str(tk)] = res
        with db.get_db() as conn:
            n = db.resolve_script_shadow(conn, _shadow_fee, market_results=results)
        if n:
            logger.info(f"[scripts] settled {n} shadow order(s)")
    except Exception as e:
        logger.debug(f"[scripts] shadow settle: {e}")


def _record_refusal(sid: str, a: dict, side: str, env: str, reason: str,
                    *, shadow: bool = False) -> None:
    """0-contract canceled row: marks the ticker attempted for this script's
    window and shows the refusal in the position history."""
    if shadow:
        with db.get_db() as conn:
            db.insert_script_shadow(conn, {
                "script_id": sid, "source": "crypto", "asset": a.get("asset"),
                "ticker": a.get("ticker"), "side": side, "contracts": 0,
                "entry_cents": 0, "refused": 1, "note": reason[:300],
                "close_time": a.get("closeTime") or "", "network": env,
            })
        return
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, {
            "asset": a["asset"], "series": a.get("series") or "",
            "ticker": a["ticker"], "side": side,
            "direction": crypto15m_trader.direction_for_favorite(side),
            "target_contracts": 0, "entry_limit_cents": 1,
            "client_order_id": f"krypt-scr-{uuid.uuid4().hex[:12]}",
            "close_time": a.get("closeTime") or "", "confidence": 0.0,
            "network": env, "status": "canceled", "dry_run": 0,
            "error": reason, "strategy": f"script:{sid[:8]}",
            "script_id": sid,
        })
        crypto15m_trader._mark_resolved(conn, pid, status="canceled",
                                        exit_reason="script_refused")


# Cash held back from any entry so a taker fee can never bounce the order.
_CASH_BUFFER_USD = 0.25


def _notional_capped(contracts: int, limit_cents: int, ask_cents: int,
                     size_cap: int, *, floor: int = 1) -> Optional[int]:
    """Shrink an order so the FILLED size cannot breach script_max_contracts.

    A marketable buy on Polymarket is filled against its NOTIONAL budget
    (count x limit), not its contract count — so when the fill price comes in
    BELOW the limit, you receive proportionally more contracts than you asked
    for. Confirmed live 2026-08-03 on two orders:

        requested 5 @ 31c = $1.55 budget -> filled 7 @ 21c ($1.47)
        requested 5 @ 32c = $1.60 budget -> filled 5 @ 31c ($1.55)

    The Scripts page advertises `script_max_contracts` as a hard cap, and with
    a stale quote it was not one: the first order took 40% more size than
    requested. Bound the budget by what it could buy at the current ask —
        contracts x limit <= size_cap x ask
    — so a fill anywhere between the ask and the limit still lands inside the
    cap.

    Returns the capped count, or **None when the cap and the exchange's own
    floors contradict each other** — the caller must then REFUSE the entry.
    The 1c cross markup makes that collision routine, not exotic: at
    script_max_contracts=5 and a 60c ask the budget rounds down to 4 contracts
    (under the 5-contract market minimum), and at cap 6 with a 17c ask to
    $0.90 (under the $1 marketable-order minimum). Both used to be SENT and
    bounced at the venue as `error` rows. Rounding back UP would place more
    size than the user's hard rail allows, so the money rail's answer is the
    same as everywhere else in this file: refused, never rounded up past your
    cap. `floor` is the caller's exchange minimum (contracts).
    """
    if ask_cents <= 0 or limit_cents <= 0 or contracts <= 0:
        return contracts
    affordable = (size_cap * ask_cents) // limit_cents
    capped = min(int(contracts), int(affordable))
    if capped >= contracts:
        return contracts           # no shrink — nothing to check
    if capped < max(1, int(floor)):
        return None
    if not crypto15m_trader._min_notional_ok(capped, limit_cents):
        return None
    return capped


# CLOB order statuses that mean the order is RESTING on the book (it did not
# cross). "matched" is the crossing one; "unmatched"/"killed" belong to
# marketable orders that found nothing and bought nothing at all.
_RESTING_ORDER_STATUS = ("live", "open")
# `delayed` is a marketable order held for the venue's matching delay: it
# crossed, so it fills as a taker, exactly like `matched`.
_CROSSED_ORDER_STATUS = ("matched", "delayed")


def _entry_was_maker(limit_cents: int, best_ask_cents: Optional[int],
                     order_status: Any) -> int:
    """1 when this entry RESTED — a maker fill, which pays NOTHING under
    takerOnly fee-v2 — else 0.

    Two witnesses, either decisive, taker wins ties:
      • the BEST ASK at submit: a limit at or above it crosses;
      • the exchange's own status on the placement response: `matched` and
        `delayed` mean it crossed, `live` means it is sitting on the book.
    Unknown on both → 0. Over-booking a fee understates P&L, which is the
    safe direction for the loss breakers this number feeds; under-booking one
    flatters every script's ledger.
    """
    st = str(order_status or "").strip().lower()
    if st in _CROSSED_ORDER_STATUS:
        return 0
    if best_ask_cents is not None and int(limit_cents) >= int(best_ask_cents):
        return 0
    if best_ask_cents is not None and int(limit_cents) < int(best_ask_cents):
        return 1
    return 1 if st in _RESTING_ORDER_STATUS else 0


def _notional_cap_refusal(limit_cents: int, ask_cents: int, size_cap: int,
                          floor: int) -> str:
    """Human reason for a `_notional_capped` refusal (None return)."""
    affordable = (size_cap * ask_cents) // limit_cents if limit_cents > 0 else 0
    return (
        f"script_max_contracts {size_cap} at {limit_cents}c (ask {ask_cents}c) "
        f"funds only {affordable} contract(s) = "
        f"${affordable * limit_cents / 100.0:.2f}, under the app's minimum "
        f"({floor} contracts / "
        f"${crypto15m_trader._MIN_ORDER_NOTIONAL_USD:.2f}) — refused rather "
        f"than sized above your cap"
    )


async def _place_intent(s: dict, a: dict, intent: dict, cfg: dict,
                        env: str, balance_usd: Optional[float] = None) -> Optional[dict]:
    """Push a sanitized intent through the rails and place the order.
    Returns the inserted position row (any status) or None on a quiet skip.

    A script in SHADOW mode runs every rail identically and then records what
    it WOULD have sent instead of sending it — so the only difference between
    a shadow run and a live one is the exchange call itself."""
    sid = str(s["id"])
    shadow = bool(s.get("dry_run"))
    side = intent["side"]
    direction = crypto15m_trader.direction_for_favorite(side)
    ticker = a.get("ticker")
    max_cents = int(cfg.get("script_max_entry_cents") or 97)

    if intent["price"] == "ask":
        # RE-QUOTE before crossing. The snapshot ask is up to 3s old and can be
        # far staler when the book moves — observed live 2026-08-03: a snapshot
        # ask of 30c against a real book at 20c, which set the limit 50% above
        # the touch. Because a marketable buy is filled against its NOTIONAL
        # budget (count x limit), a limit that high buys proportionally MORE
        # contracts than requested: 5 @ 31c ($1.55) came back as 7 filled @ 21c.
        # The general-market path already re-quotes at submit; this one didn't.
        ask = None
        try:
            q = await polymarket_api.get_quote(ticker, direction)
            qa = q.get("ask_cents")
            if qa and 1 <= int(qa) <= 99:
                ask = int(qa) / 100.0
        except Exception as e:
            logger.debug(f"[scripts] entry re-quote {ticker}: {e}")
        if ask is None:
            # Fall back to the snapshot only in shadow, where nothing is spent
            # and refusing would silently starve the ledger.
            snap_ask = a.get("upAsk") if side == "up" else a.get("downAsk")
            if not (shadow and snap_ask and 0.0 < float(snap_ask) < 1.0):
                return None  # no REAL executable book — retry a later tick
            ask = float(snap_ask)
        # Cross WITH a 1c markup so a 1-tick ask move between quote and submit
        # still fills (mirrors the built-in taker path's entry_diff).
        limit_cents = min(max_cents + 1, int(round(float(ask) * 100)) + 1)
        order_type = "FAK"
        ask_cents = max(1, int(round(float(ask) * 100)))
    else:
        limit_cents = int(intent["price"])
        order_type = "GTC"
        ask_cents = limit_cents

    if limit_cents > max_cents:
        _record_refusal(sid, a, side, env,
                        f"entry {limit_cents}c > script_max_entry_cents {max_cents}c", shadow=shadow)
        return None

    size_cap = max(1, int(cfg.get("script_max_contracts") or 20))
    contracts = min(
        int(intent.get("size") or max(1, int(cfg.get("crypto15m_order_size", 1)))),
        size_cap,
    )
    floor = crypto15m_trader._C15_MIN_CONTRACTS
    # The exchange's per-order minimum is a hard floor, and so is the user's
    # cap. When they contradict each other, REFUSE — raising the order to the
    # exchange floor would place up to 5x the size the Scripts page advertises
    # as a hard rail. Same policy as the min-notional branch below.
    if floor > size_cap:
        _record_refusal(sid, a, side, env,
                        f"app minimum {floor} contracts > "
                        f"script_max_contracts {size_cap}", shadow=shadow)
        return None
    if contracts < floor:
        contracts = floor
    if not crypto15m_trader._min_notional_ok(contracts, limit_cents):
        needed = int(-(-100 // max(1, limit_cents)))  # ceil($1 / price)
        # The exchange's $1 minimum is a hard floor. If meeting it would exceed
        # the script's own size cap, refuse rather than silently overriding the
        # rail the user set — the cap is advertised as hard.
        if needed > size_cap:
            _record_refusal(sid, a, side, env,
                            f"${1} min-notional needs {needed} contracts at "
                            f"{limit_cents}c > script_max_contracts {size_cap}", shadow=shadow)
            return None
        contracts = max(contracts, needed)

    capped = _notional_capped(contracts, limit_cents, ask_cents, size_cap,
                              floor=floor)
    if capped is None:
        _record_refusal(sid, a, side, env,
                        _notional_cap_refusal(limit_cents, ask_cents,
                                              size_cap, floor), shadow=shadow)
        return None
    contracts = capped

    # Affordability. The built-in crypto engine skips when the balance can't
    # fund an order; this path did not, so an unaffordable intent was SENT and
    # bounced by the exchange, landing as a terminal `error` row that reads
    # like a malfunction. Refuse it here instead, with a reason the ledger and
    # the Scripts page can both show.
    cost = contracts * limit_cents / 100.0
    if (not shadow and balance_usd is not None
            and cost > balance_usd - _CASH_BUFFER_USD):
        _record_refusal(
            sid, a, side, env,
            f"needs ${cost:.2f} but the balance is ${balance_usd:.2f} "
            f"(keeping ${_CASH_BUFFER_USD:.2f} for fees)", shadow=shadow)
        return None

    if shadow:
        # Every rail above has already run. Record the order we WOULD have
        # sent and stop here — no exchange call, no live position row.
        with db.get_db() as conn:
            shid = db.insert_script_shadow(conn, {
                "script_id": sid, "source": "crypto", "asset": a.get("asset"),
                "ticker": ticker, "side": side, "contracts": contracts,
                "entry_cents": limit_cents, "order_type": order_type,
                "reason": str(intent.get("reason") or "")[:200],
                "tp_pct": intent.get("take_profit_pct"),
                "sl_cents": intent.get("stop_loss_cents"),
                "close_time": a.get("closeTime") or "", "network": env,
            })
        logger.info(
            f"[scripts] {sid[:8]} SHADOW entry {a['asset']} {direction} "
            f"x{contracts} @ {limit_cents}c "
            f"({intent.get('reason') or 'no reason'})")
        return {"id": shid, "shadow": True, "ticker": ticker,
                "asset": a.get("asset"), "side": side,
                "target_contracts": contracts}

    coid = f"krypt-scr-{a['asset']}-{uuid.uuid4().hex[:8]}"
    row = {
        "asset": a["asset"], "series": a.get("series") or "",
        "ticker": ticker, "side": side, "direction": direction,
        "target_contracts": contracts, "entry_limit_cents": limit_cents,
        "client_order_id": coid, "close_time": a.get("closeTime") or "",
        "confidence": float(a.get("favoritePrice") or 0.0) * 100.0,
        "network": env, "strategy": f"script:{sid[:8]}", "script_id": sid,
        "tp_pct": intent.get("take_profit_pct"),
        "sl_cents": intent.get("stop_loss_cents"),
        # Fee accounting: a GTC (numeric-price rest) that fills is a maker
        # fill — no taker fee; price:"ask" (FAK) pays it.
        "entry_order_type": order_type,
    }
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=direction, action="buy",
            count=contracts, price_cents=limit_cents,
            client_order_id=coid, order_type=order_type,
        )
    except polymarket_api.OrderStatusUnknownError as e:
        # Sent, outcome unknown: keep its id on a submitted row so the entry
        # poll tracks it, like any placed order.
        logger.warning(f"[scripts] {sid[:8]} entry {a['asset']}: {e}")
        resp = e.response
    except Exception as e:
        row.update({"status": "error", "error": str(e)[:200], "dry_run": 0})
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, row)
            crypto15m_trader._mark_resolved(conn, pid, status="error")
        logger.error(f"[scripts] {sid[:8]} entry failed {a['asset']}: {e}")
        return None

    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    oid = order.get("order_id") if isinstance(order, dict) else None
    ostatus = ((order.get("status") if isinstance(order, dict) else "") or "").lower()
    if not oid and ostatus in ("unmatched", "killed"):
        _record_refusal(sid, a, side, env, "FAK crossed no liquidity (0 fill)")
        return None
    if not oid:
        row.update({"status": "error", "error": "no order_id from exchange",
                    "dry_run": 0})
        with db.get_db() as conn:
            pid = db.insert_crypto15m_position(conn, row)
            crypto15m_trader._mark_resolved(conn, pid, status="error")
        return None
    row.update({"status": "submitted", "order_id": oid, "dry_run": 0})
    with db.get_db() as conn:
        pid = db.insert_crypto15m_position(conn, row)
        logger.info(
            f"[scripts] {sid[:8]} entry {a['asset']} {direction} "
            f"x{contracts} @ {limit_cents}c ({intent.get('reason') or 'no reason'})"
        )
        return db.fetch_crypto15m_by_id(conn, pid)


def _pos_lite(r: dict) -> dict:
    """Compact position dict handed to on_fill / on_settle hooks.

    Handles BOTH position tables: crypto windows (`crypto15m_positions`, which
    carry asset/side/avg_entry_cents) and main-market signal follows
    (`bot_positions`, which carry title/direction/avg_fill_price_cents). The
    hook contract is one shape, so the column differences are mapped here.
    """
    avg = r.get("avg_entry_cents")
    if avg is None:
        avg = r.get("avg_fill_price_cents")
    return {
        "id": r.get("id"), "ticker": r.get("ticker"), "asset": r.get("asset"),
        "side": r.get("side") or r.get("direction"),
        "contracts": r.get("filled_contracts") or 0,
        "avgEntryCents": avg,
        "status": r.get("status"), "pnlUsd": r.get("pnl_usd"),
        "exitReason": r.get("exit_reason"), "won": r.get("outcome_correct"),
    }


def _snake_to_camel(k: str) -> str:
    parts = k.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _sanitize_supervise(raw) -> tuple[dict, list[str]]:
    """(clean snake-key patch limited to the whitelist, rejection notes)."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict) or not isinstance(raw.get("set"), dict):
        return {}, ["supervise() must return None or {\"set\": {key: value}}"]
    patch: dict = {}
    notes: list[str] = []
    for k, v in raw["set"].items():
        kind = _SUPERVISE_KEYS.get(str(k))
        if kind is None:
            notes.append(f"key '{k}' is not supervisable (ignored)")
            continue
        try:
            if kind == "bool":
                # Engine on/off switches are DISABLE-ONLY from a script: turning
                # an engine OFF is a risk-off action, but turning one ON would
                # deploy the account through an engine that has none of the
                # per-script money rails — an escalation past the script's own
                # caps. Ignore enable attempts; allow disables.
                if bool(v):
                    notes.append(f"'{k}': scripts may turn engines off, not on "
                                 "(ignored)")
                    continue
                patch[str(k)] = False
            elif kind == "float":
                fv = float(v)
                if fv != fv or fv in (float("inf"), float("-inf")):
                    notes.append(f"'{k}': non-finite value (ignored)")
                    continue
                patch[str(k)] = fv
            elif kind == "mode":
                mv = str(v).lower()
                if mv not in ("favorite", "contrarian", "model"):
                    notes.append(f"'{k}' must be favorite|contrarian|model (ignored)")
                    continue
                patch[str(k)] = mv
        except (TypeError, ValueError):
            notes.append(f"'{k}' got a non-{kind} value (ignored)")
    return patch, notes


async def _run_supervise(s: dict, mod: script_sandbox.CompiledScript,
                         cfg: dict, app: dict, *, shadow: bool = False) -> None:
    """One supervise() call: sanitize the returned patch against the
    whitelist, drop no-ops, rate-limit, and hand the survivors to the host
    (which persists them + pushes the merged config back). Every applied
    patch is audit-logged.

    Refused outright in SHADOW mode. Shadow means "this script cannot touch
    anything real", and supervise() is the one hook that reaches outside the
    script: it rewrites the live engine's entry threshold / entry max / min
    delta / entry window / direction mode and can switch engines off. A script
    the UI badges "shadow" retuning the real engine is a confused deputy — the
    user green-lit a simulation. Gated HERE rather than only at the call site so
    a future caller cannot reintroduce it."""
    sid = str(s["id"])
    if shadow:
        _hb_note(sid, "supervise_skipped_shadow")
        return
    raw = _call_hook(mod, "supervise", cfg, dict(app))
    patch, notes = _sanitize_supervise(raw)
    for n in notes:
        logger.info(f"[scripts] {sid[:8]} supervise: {n}")
    if not patch:
        return
    # Drop keys already at the requested value — a script asserting steady
    # state every tick must not spam config writes.
    patch = {k: v for k, v in patch.items() if cfg.get(k) != v}
    if not patch:
        return
    if time.time() - _last_patch_t.get(sid, 0.0) < _SUPERVISE_MIN_INTERVAL_S:
        return
    _last_patch_t[sid] = time.time()
    camel = {_snake_to_camel(k): v for k, v in patch.items()}
    logger.warning(
        f"[scripts] {s.get('name')} ({sid[:8]}) SUPERVISOR config change: {patch}"
    )
    await _emit_event("script:configPatch", {
        "id": sid, "name": s.get("name"), "patch": camel,
    })


async def _manage_pass(s: dict, mod: script_sandbox.CompiledScript, cfg: dict,
                       env: str, assets_by_ticker: dict, portfolio: dict) -> None:
    """Run manage(position, ctx) over this script's open FILLED crypto
    positions. 'sell' rides the engine's shared exit machinery
    (_place_exit_sell → exit-fill polling → booking); 'update' retunes the
    row-level tp/sl the exit ladder already honors."""
    sid = str(s["id"])
    with db.get_db() as conn:
        rows = _open_crypto_for_script(conn, sid, env)
    for pos in rows:
        if pos.get("status") != "filled" or int(pos.get("filled_contracts") or 0) <= 0:
            continue
        a = assets_by_ticker.get(str(pos.get("ticker")))
        ctx = dict(a) if a else {
            "ticker": pos.get("ticker"), "asset": pos.get("asset"),
            "hasMarket": False, "minsLeft": None,
        }
        ctx["portfolio"] = portfolio
        bid_cents = None
        if a:
            if pos.get("side") == "up":
                yb = a.get("yesBid")
                bid_cents = round(float(yb) * 100, 1) if yb else None
            else:
                ya = a.get("yesAsk")
                bid_cents = round((1.0 - float(ya)) * 100, 1) if ya else None
        entry_c = float(pos.get("avg_entry_cents") or pos.get("entry_limit_cents") or 0)
        pos_lite = {
            "ticker": pos.get("ticker"), "asset": pos.get("asset"),
            "side": pos.get("side"),
            "contracts": int(pos.get("filled_contracts") or 0),
            "avgEntryCents": entry_c, "curBidCents": bid_cents,
            "minsLeft": ctx.get("minsLeft"),
            "tpPct": pos.get("tp_pct"), "slCents": pos.get("sl_cents"),
            "unrealizedPct": round((bid_cents - entry_c) / entry_c, 4)
            if bid_cents is not None and entry_c > 0 else None,
        }
        _hb_note(sid, "manage")
        act_raw = _call_hook(mod, "manage", cfg, pos_lite, ctx)
        act, err = sanitize_manage(act_raw)
        if err:
            raise script_sandbox.ScriptError(f"manage(): {err}")
        if act is None:
            continue
        _hb_note(sid, "manage_intents")
        if act["action"] == "sell":
            exit_cents = await crypto15m_trader._best_bid_cents(
                pos.get("ticker"), pos.get("direction"))
            if exit_cents is None and bid_cents is not None:
                exit_cents = int(bid_cents)
            if exit_cents is None:
                logger.info(f"[scripts] {sid[:8]} manage sell {pos.get('asset')}: "
                            "no bid anywhere — retry next tick")
                continue
            await crypto15m_trader._place_exit_sell(
                pos, cfg, exit_cents=int(exit_cents), reason="script_exit")
        else:  # update
            fields = {}
            if "take_profit_pct" in act:
                fields["tp_pct"] = act["take_profit_pct"] or None
            if "stop_loss_cents" in act:
                fields["sl_cents"] = act["stop_loss_cents"] or None
            if fields:
                with db.get_db() as conn:
                    db.update_crypto15m_position(conn, int(pos["id"]), **fields)


def _fetch_new_signals(env: str) -> list[tuple[dict, str]]:
    """New whale/momentum signals since the last pass (high-water marks).
    First pass just baselines the marks — scripts never replay the backlog."""
    out: list[tuple[dict, str]] = []
    try:
        with db.get_db() as conn:
            for table, source in (("whale_trades", "whale"), ("alerts", "momentum")):
                mark = _sig_marks[source]
                if mark is None:
                    row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
                    _sig_marks[source] = int(row[0] or 0)
                    continue
                rows = conn.execute(
                    f"""SELECT * FROM {table} WHERE id > ?
                        ORDER BY id ASC LIMIT 50""",
                    (mark,),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    _sig_marks[source] = max(_sig_marks[source] or 0, int(d["id"]))
                    out.append((d, source))
    except Exception as e:
        logger.debug(f"[scripts] signal fetch: {e}")
    return out


async def _place_signal_follow(s: dict, sig: dict, source: str, act: dict,
                               cfg: dict, env: str,
                               balance_usd: Optional[float] = None) -> Optional[dict]:
    """Follow a signal on ITS side (scripts filter/size, never side-flip):
    a bot_positions row tagged script:<id>, managed to settlement by the
    main engine's poll/reconcile machinery like any copy row."""
    sid = str(s["id"])
    ticker = str(sig.get("ticker") or "")
    direction = str(sig.get("taker_side") or sig.get("direction") or "").lower()
    if not ticker or direction not in ("yes", "no"):
        return None
    max_cents = int(cfg.get("script_max_entry_cents") or 97)
    ask = None
    try:
        q = await polymarket_api.get_quote(ticker, direction)
        ask = q.get("ask_cents")
    except Exception as e:
        logger.debug(f"[scripts] signal quote {ticker}: {e}")
    if not ask or not (1 <= int(ask) <= 99):
        return None
    limit_cents = int(ask)
    if limit_cents > max_cents:
        return None
    spend = float(act.get("sizeUsd") or cfg.get("fixed_trade_usd") or 5.0)
    size_cap = max(1, int(cfg.get("script_max_contracts") or 20))
    floor = crypto15m_trader._C15_MIN_CONTRACTS
    # Same contradiction as the crypto entry path: never round a user's cap UP
    # to the exchange minimum — that would breach the advertised hard rail.
    if floor > size_cap:
        logger.info(
            f"[scripts] {sid[:8]} skips {source} signal {ticker}: app "
            f"minimum {floor} contracts > script_max_contracts {size_cap}")
        return None
    contracts = int(spend // (limit_cents / 100.0))
    contracts = min(max(contracts, floor), size_cap)
    capped = _notional_capped(contracts, limit_cents, int(ask), size_cap,
                              floor=floor)
    if capped is None:
        logger.info(
            f"[scripts] {sid[:8]} skips {source} signal {ticker}: "
            + _notional_cap_refusal(limit_cents, int(ask), size_cap, floor))
        return None
    contracts = capped

    cost = contracts * limit_cents / 100.0
    if (not bool(s.get("dry_run")) and balance_usd is not None
            and cost > balance_usd - _CASH_BUFFER_USD):
        logger.info(
            f"[scripts] {sid[:8]} skips {source} signal {ticker}: needs "
            f"${cost:.2f} but the balance is ${balance_usd:.2f}")
        return None

    if bool(s.get("dry_run")):
        with db.get_db() as conn:
            shid = db.insert_script_shadow(conn, {
                "script_id": sid, "source": "signal", "signal_source": source,
                "signal_id": int(sig.get("id") or 0),
                "asset": sig.get("category") or source, "ticker": ticker,
                "side": direction, "contracts": contracts,
                "entry_cents": limit_cents, "order_type": "GTC",
                "reason": str(sig.get("title") or "")[:200], "network": env,
            })
        logger.info(
            f"[scripts] {sid[:8]} SHADOW follows {source} signal: {ticker} "
            f"{direction} x{contracts} @ {limit_cents}c")
        return {"id": shid, "shadow": True, "ticker": ticker,
                "direction": direction, "target_contracts": contracts}

    coid = f"krypt-scrs-{uuid.uuid4().hex[:10]}"
    # bot_positions is UNIQUE(signal_source, signal_id, network). whale_trades.id
    # and alerts.id are independent sequences, and every enabled script is handed
    # the same signal list — so a bare source of "script" collides whenever two
    # scripts follow one signal, or a whale id equals an alert id. Scope the
    # source to this script + stream so the key is always unique.
    row = {
        "signal_source": f"script:{sid[:8]}:{source}",
        "signal_id": int(sig.get("id") or 0),
        "ticker": ticker, "event_ticker": sig.get("event_ticker") or "",
        "title": sig.get("title") or ticker,
        "category": sig.get("category") or "",
        "direction": direction, "action": "buy",
        "target_contracts": contracts, "limit_price_cents": limit_cents,
        "client_order_id": coid, "confidence": 0.0, "edge_pts": 0.0,
        "signal_price": float(limit_cents), "network": env,
        "script_id": sid,
        "entry_maker": 0,  # signal follows cross the book (taker)
    }
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=direction, action="buy",
            count=contracts, price_cents=limit_cents, client_order_id=coid,
        )
    except polymarket_api.OrderStatusUnknownError as e:
        # Sent, outcome unknown: track it by its id like any placed order.
        logger.warning(f"[scripts] {sid[:8]} signal follow {ticker}: {e}")
        resp = e.response
    except Exception as e:
        logger.error(f"[scripts] {sid[:8]} signal follow {ticker} failed: {e}")
        return None
    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    oid = order.get("order_id") if isinstance(order, dict) else None
    if not oid:
        return None
    row.update({"status": "submitted", "order_id": oid})
    try:
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            logger.info(
                f"[scripts] {sid[:8]} follows {source} signal: {ticker} "
                f"{direction} x{contracts} @ {limit_cents}c"
            )
            return db.fetch_position_by_id(conn, pid)
    except Exception as e:
        # The order is LIVE on the exchange. Never let it go untracked: shout so
        # reconcile/the user can find it (it will otherwise re-import as external).
        logger.critical(
            f"[scripts] {sid[:8]} signal order {oid} PLACED but not recorded "
            f"({ticker} {direction} x{contracts} @ {limit_cents}c): {e}"
        )
        return None


# Horizon for the "closing soon" half of the universe. Wider than any
# built-in example script's own close-time gate, so the slice supplies
# candidates rather than pre-filtering for one strategy's taste.
_SOON_HOURS = 48


def _market_universe(cfg: dict) -> list[dict]:
    """The general-market slice handed to decide_market(), highest volume
    first. Crypto Up/Down windows are excluded — decide() already owns those,
    and letting both hooks see them would double-enter the same window.

    Ranking by volume alone put two kinds of untradeable row at the top, and
    between them they filled the whole universe (measured on the live DB,
    2026-07-26 — a Sports script saw 0 qualifying markets for two days):

      • DEAD ROWS. scanner.py skips any market whose close_time has passed,
        so those rows keep the price, volume and category they carried at
        their last sighting — forever. 1,120 of 2,557 rows still marked
        'open' had already closed, and 104 of them sat in the top 200 by
        volume, squatting the ranking with frozen numbers. They are excluded
        here; a row with no close_time at all is kept, since "unknown" is not
        the same as "expired".

      • LONG-DATED FUTURES. The busiest markets are the ones that resolve
        furthest out ("who wins the 2027 final") — 121 of the top 200 were
        more than 36h from closing. Volume rank alone therefore never
        surfaces an imminent event, which is the only thing an event-driven
        strategy can trade. So half the budget is reserved for the busiest
        markets CLOSING WITHIN `_SOON_HOURS`, and that half is returned
        first: when a script hits its open-position cap partway through the
        list, the entry that cannot wait for the next tick should be the one
        already considered.
    """
    limit = int(cfg.get("script_market_limit") or 0)
    if limit <= 0:
        return []
    min_vol = float(cfg.get("script_market_min_volume") or 0.0)
    # Order by 24h volume, NOT lifetime volume. get_active_markets() sorts by
    # lifetime `volume`, which is dominated by long-dated novelty markets
    # ("will X win the 2028 election", $30-65M lifetime, priced 0-4c and years
    # from resolving). Those filled the entire top-20 universe, so nothing
    # tradeable today was ever handed to a script — verified on the live DB,
    # 0 of the top 20 were sports while 209 live sports markets existed. 24h
    # volume is what "currently active" actually means.
    #
    # close_time is a fixed-width UTC string ('2026-07-26T17:30:00Z'), so the
    # comparisons below are plain lexicographic ones against strftime's output
    # in the same shape. The handful of rows carrying milliseconds sort a hair
    # early within their own second, which no horizon this coarse can notice.
    live = """SELECT * FROM markets
              WHERE status IN ('active','open') AND volume_24h >= ?
                AND (close_time = ''
                     OR close_time > strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) """
    soon_budget = limit // 2
    try:
        with db.get_db() as conn:
            busiest = [dict(r) for r in conn.execute(
                live + "ORDER BY volume_24h DESC, close_time ASC LIMIT ?",
                (min_vol, max(limit * 4, 50)),
            ).fetchall()]
            closing = [dict(r) for r in conn.execute(
                live + """AND close_time != ''
                          AND close_time <= strftime('%Y-%m-%dT%H:%M:%SZ',
                                                     'now', ?)
                          ORDER BY volume_24h DESC, close_time ASC LIMIT ?""",
                (min_vol, f"+{_SOON_HOURS} hours", max(soon_budget * 4, 50)),
            ).fetchall()] if soon_budget else []
    except Exception as e:
        logger.debug(f"[scripts] market universe: {e}")
        return []

    out: list[dict] = []
    seen: set[str] = set()

    def _take(rows: list[dict], upto: int) -> None:
        for r in rows:
            if len(out) >= upto:
                return
            ticker = str(r.get("ticker") or "")
            if ticker in seen:
                continue
            if categorize.is_micro_market(r.get("slug") or ""):
                continue
            seen.add(ticker)
            out.append(r)

    _take(closing, soon_budget)
    # Whatever the closing-soon slice left unspent goes back to the general
    # one, so a quiet board still fills the budget instead of shrinking it.
    _take(busiest, limit)
    return out


# (script_id, ticker) -> time before which the market is not offered again.
#
# The crypto path writes a 0-contract refusal row so a rails-refused window
# counts as "attempted" and is never retried. The market path had no such
# marker, so a market the rails always refuse — a 99c favourite against a 97c
# cap — was re-evaluated on EVERY 5s tick: a fresh CLOB quote fetched and an
# identical log line written, forever. Observed live at ~12 lines/minute for
# one market, which alone buries the heartbeat this module emits.
#
# A cooldown rather than a permanent mark, because unlike a 15-minute crypto
# window a general market lives for days: a price that breaches the cap now
# can easily be inside it in an hour, and one refusal should not retire the
# market for good.
_market_cooldown: dict[tuple[str, str], float] = {}
MARKET_REFUSAL_COOLDOWN_S = 600.0


def _cool_off(sid: str, ticker: str, reason: str) -> None:
    """Log a rails refusal ONCE and stop offering that market for a while."""
    _market_cooldown[(sid, ticker)] = time.time() + MARKET_REFUSAL_COOLDOWN_S
    logger.info(
        f"[scripts] {sid[:8]} refuses market {ticker}: {reason} — not "
        f"reconsidered for {MARKET_REFUSAL_COOLDOWN_S / 60:.0f}m")


def _fail_market_entry(sid: str, ticker: str, row: dict, err: str,
                       env: str) -> None:
    """Record a FAILED general-market entry the way the crypto path does: a
    terminal status='error' position row, plus a cooldown.

    The row is what makes _market_already_attempted() true, so the same intent
    is not re-sent on the next poll; the cooldown is the backstop for the case
    where the insert itself fails. Both matter because the exchange may have
    ACCEPTED an order whose response we lost — a blind retry would then open a
    second real position (the CLOB has no idempotency key for us)."""
    _market_cooldown[(sid, ticker)] = time.time() + MARKET_REFUSAL_COOLDOWN_S
    logger.error(f"[scripts] {sid[:8]} market entry {ticker} failed: {err}")
    try:
        with db.get_db() as conn:
            pid = db.insert_bot_position(
                conn, {**row, "status": "error", "error": err[:200]})
            db.log_event(conn, pid, "error", note=err[:200])
    except Exception as e:  # noqa: BLE001 — never let bookkeeping raise into the loop
        logger.error(f"[scripts] {sid[:8]} could not record {ticker} failure: {e}")


def _in_cooldown(sid: str, ticker: str) -> bool:
    until = _market_cooldown.get((sid, ticker))
    if until is None:
        return False
    if time.time() >= until:
        del _market_cooldown[(sid, ticker)]
        return False
    return True


def _market_already_attempted(conn, sid: str, ticker: str, *,
                              shadow: bool = False) -> bool:
    """One entry per market per script — the general-market analogue of the
    one-per-window rule, and the thing that stops a script from re-buying the
    same market on every tick for as long as its condition holds."""
    if shadow:
        return db.shadow_already_attempted(conn, sid, ticker)
    return conn.execute(
        """SELECT 1 FROM bot_positions
           WHERE script_id=? AND ticker=? LIMIT 1""",
        (sid, ticker),
    ).fetchone() is not None


async def _place_market_intent(s: dict, m: dict, intent: dict, cfg: dict,
                               env: str,
                               balance_usd: Optional[float] = None) -> Optional[dict]:
    """Place a decide_market() intent on a general Polymarket market.

    Lands in bot_positions tagged script:<id>, so the main engine's existing
    fill polling, resolution, and reconcile machinery manages it exactly like
    a signal follow. Every rail that applies to a crypto entry applies here.
    """
    sid = str(s["id"])
    shadow = bool(s.get("dry_run"))
    ticker = str(m.get("ticker") or "")
    side = intent["side"]
    if not ticker:
        return None
    max_cents = int(cfg.get("script_max_entry_cents") or 97)
    size_cap = max(1, int(cfg.get("script_max_contracts") or 20))
    floor = crypto15m_trader._C15_MIN_CONTRACTS
    if floor > size_cap:
        _cool_off(sid, ticker, f"app minimum {floor} contracts > "
                               f"script_max_contracts {size_cap}")
        return None

    # A resting limit can use the script's own price; crossing needs the REAL
    # book, fetched only now that the script has actually asked to trade.
    # `best_ask_cents` is the best ASK at submit, kept separately from the
    # submitted limit — it is the ONLY thing that decides maker vs taker, and
    # conflating the two (ask_cents was assigned limit_cents on both branches)
    # made the `entry_maker` stamp below unreachable, so a resting script
    # limit was booked with a taker fee it never paid. None = book unknown.
    best_ask_cents: Optional[int] = None
    if intent["price"] == "ask":
        try:
            q = await polymarket_api.get_quote(ticker, side)
            ask = q.get("ask_cents")
            bid = q.get("bid_cents")
        except Exception as e:
            logger.debug(f"[scripts] market quote {ticker}: {e}")
            return None
        if not ask or not (1 <= int(ask) <= 99):
            return None      # no executable book — retry a later tick
        # Spread gate, enforced HERE because this is the only place a real
        # two-sided book exists — the market rows scripts see carry a single
        # Gamma price. A wide book eats the entire edge of any thin-margin
        # strategy before it starts. 0 = off.
        max_spread = float(cfg.get("script_market_max_spread_cents") or 0.0)
        if max_spread > 0 and bid:
            spread = int(ask) - int(bid)
            if spread > max_spread:
                _cool_off(sid, ticker,
                          f"spread {spread}c > script_market_max_spread_cents "
                          f"{max_spread:g}c")
                return None
        limit_cents = int(ask)
        ask_cents = int(ask)
        best_ask_cents = int(ask)     # limit == ask: this CROSSES (taker)
    else:
        limit_cents = int(intent["price"])
        ask_cents = limit_cents
        # No book is read on this path (a resting limit doesn't need one), so
        # the ask stays unknown and the exchange's own order status below is
        # what decides maker vs taker.
    if limit_cents > max_cents:
        _cool_off(sid, ticker, f"entry {limit_cents}c > "
                               f"script_max_entry_cents {max_cents}c")
        return None

    if intent.get("size") is not None:
        contracts = int(intent["size"])
    else:
        spend = float(intent.get("sizeUsd") or cfg.get("fixed_trade_usd") or 5.0)
        contracts = int(spend // (limit_cents / 100.0))
    contracts = min(max(contracts, floor), size_cap)
    if (contracts * limit_cents) / 100.0 < crypto15m_trader._MIN_ORDER_NOTIONAL_USD:
        needed = int(-(-100 // max(1, limit_cents)))
        if needed > size_cap:
            _cool_off(sid, ticker,
                      f"$1 min-notional needs {needed} contracts > "
                      f"script_max_contracts {size_cap}")
            return None
        contracts = max(contracts, needed)

    capped = _notional_capped(contracts, limit_cents, ask_cents, size_cap,
                              floor=floor)
    if capped is None:
        _cool_off(sid, ticker,
                  _notional_cap_refusal(limit_cents, ask_cents, size_cap, floor))
        return None
    contracts = capped

    cost = contracts * limit_cents / 100.0
    if (not shadow and balance_usd is not None
            and cost > balance_usd - _CASH_BUFFER_USD):
        _cool_off(sid, ticker,
                  f"needs ${cost:.2f} but the balance is ${balance_usd:.2f}")
        return None

    if shadow:
        with db.get_db() as conn:
            shid = db.insert_script_shadow(conn, {
                "script_id": sid, "source": "signal",
                "signal_source": "market", "signal_id": None,
                "asset": m.get("category") or "", "ticker": ticker,
                "side": side, "contracts": contracts,
                "entry_cents": limit_cents, "order_type": "GTC",
                "reason": str(intent.get("reason") or "")[:200],
                "close_time": m.get("close_time") or "", "network": env,
            })
        logger.info(
            f"[scripts] {sid[:8]} SHADOW market entry {ticker} {side} "
            f"x{contracts} @ {limit_cents}c "
            f"({intent.get('reason') or 'no reason'})")
        return {"id": shid, "shadow": True, "ticker": ticker, "side": side,
                "target_contracts": contracts}

    coid = f"krypt-scrm-{uuid.uuid4().hex[:10]}"
    row = {
        # bot_positions is UNIQUE(signal_source, signal_id, network) and a
        # market entry has no signal id, so key it by this script + market.
        "signal_source": f"script:{sid[:8]}:market",
        "signal_id": abs(hash(ticker)) % (10 ** 9),
        "ticker": ticker, "event_ticker": m.get("event_ticker") or "",
        "title": m.get("title") or ticker,
        "category": m.get("category") or "",
        "direction": side, "action": "buy",
        "target_contracts": contracts, "limit_price_cents": limit_cents,
        "client_order_id": coid, "confidence": 0.0, "edge_pts": 0.0,
        "signal_price": float(limit_cents), "network": env,
        "script_id": sid,
        # Fee accounting, finalised from the exchange's response below. It
        # used to be `1 if limit_cents < ask_cents`, comparing the limit
        # against ITSELF (ask_cents was assigned limit_cents on both
        # branches), so it was 0 on every order ever placed here and a
        # resting script limit was booked with a taker fee it never paid.
        "entry_maker": 0,
    }
    try:
        resp = await polymarket_api.place_limit_order(
            ticker=ticker, side=side, action="buy",
            count=contracts, price_cents=limit_cents, client_order_id=coid,
        )
    except polymarket_api.OrderStatusUnknownError as e:
        # Sent, outcome unknown: track it by its id like any placed order.
        logger.warning(f"[scripts] {sid[:8]} market entry {ticker}: {e}")
        resp = e.response
    except Exception as e:
        # Record the failure and cool the market off. Returning bare used to
        # leave NO trace: _market_already_attempted() reads bot_positions and
        # _market_cooldown is only written by pre-exchange rail refusals, so the
        # identical intent went back to the exchange every poll (5s) forever —
        # and if the order actually landed and only the response was lost, the
        # retry opened a SECOND real position.
        _fail_market_entry(sid, ticker, row, f"{type(e).__name__}: {e}", env)
        return None
    order = (resp.get("order") if isinstance(resp, dict) else None) or resp or {}
    oid = order.get("order_id") if isinstance(order, dict) else None
    if not oid:
        _fail_market_entry(sid, ticker, row, "no order_id from exchange", env)
        return None
    row.update({
        "status": "submitted", "order_id": oid,
        "entry_maker": _entry_was_maker(
            limit_cents, best_ask_cents,
            (order.get("status") if isinstance(order, dict) else "")),
    })
    try:
        with db.get_db() as conn:
            pid = db.insert_bot_position(conn, row)
            logger.info(
                f"[scripts] {sid[:8]} market entry {ticker} {side} "
                f"x{contracts} @ {limit_cents}c "
                f"({intent.get('reason') or 'no reason'})")
            return db.fetch_position_by_id(conn, pid)
    except Exception as e:
        # The order is LIVE. Shout so reconcile/the user can find it.
        logger.critical(
            f"[scripts] {sid[:8]} market order {oid} PLACED but not recorded "
            f"({ticker} {side} x{contracts} @ {limit_cents}c): {e}")
        return None


async def _notify_lifecycle(scripts_by_id: dict[str, dict], env: str,
                            cfg: dict) -> None:
    """Deliver on_fill / on_settle for script positions whose lifecycle moved
    since we last looked (managed elsewhere — we only observe).

    Covers BOTH position tables: crypto windows (`crypto15m_positions`, run by
    the crypto15m manage pass) and main-market signal follows
    (`bot_positions`, run by the main engine's poll/reconcile). The hooks are
    documented without qualification, so a script that only defines
    decide_signal must still see its own fills and settlements.
    """
    rows: list[tuple[str, dict]] = []
    for table, tag in (("crypto15m_positions", "c"), ("bot_positions", "b")):
        try:
            with db.get_db() as conn:
                found = conn.execute(
                    f"""SELECT * FROM {table}
                       WHERE script_id IS NOT NULL AND network=?
                         AND (
                           (resolved=0 AND filled_contracts > 0)
                           OR (resolved=1 AND datetime(COALESCE(resolved_at, last_updated))
                               >= datetime('now', '-1 hour'))
                         )""",
                    (env,),
                ).fetchall()
            rows.extend((tag, dict(r)) for r in found)
        except Exception as e:
            logger.debug(f"[scripts] lifecycle scan {table}: {e}")
    for tag, r in rows:
        sid = str(r.get("script_id") or "")
        s = scripts_by_id.get(sid)
        if not s or not s.get("enabled"):
            continue
        mod = _compiled.get(sid)
        if mod is None:
            continue
        # The two tables have independent id sequences, so the seen-key must
        # be namespaced — a bare id would let crypto row 5 permanently
        # suppress signal row 5's notifications (and vice versa).
        key = f"{tag}{int(r.get('id') or 0)}"
        try:
            if (r.get("filled_contracts") or 0) > 0 and key not in _notified_fill:
                _notified_fill.add(key)
                _call_hook(mod, "on_fill", cfg, _pos_lite(r), mod.state)
            if r.get("resolved") and key not in _notified_settle:
                _notified_settle.add(key)
                _call_hook(mod, "on_settle", cfg, _pos_lite(r), mod.state)
        except script_sandbox.SCRIPT_FAILURES as e:
            _disable(sid, str(e))
            await _emit_event("script:status",
                             {"id": sid, "enabled": False, "lastError": str(e)})
    # Bound the in-memory sets across long uptimes.
    if len(_notified_fill) > 5000:
        _notified_fill.clear()
    if len(_notified_settle) > 5000:
        _notified_settle.clear()


def forget(sid: str) -> None:
    """Drop every process-level trace of a script. Called when one is deleted
    so its compiled module (arbitrary user code holding whatever it captured)
    is released promptly instead of lingering for the life of the process."""
    _compiled.pop(sid, None)
    _last_patch_t.pop(sid, None)
    _state_oversize_warned.discard(sid)
    _hb.pop(sid, None)
    _hb_last.pop(sid, None)
    _status.pop(sid, None)
    script_quotes.forget(sid)
    for key in [k for k in _market_cooldown if k[0] == sid]:
        del _market_cooldown[key]


def _save_states() -> None:
    global _last_state_save
    if time.time() - _last_state_save < STATE_SAVE_EVERY_S:
        return
    _last_state_save = time.time()
    for sid, mod in list(_compiled.items()):
        if mod.abandoned.is_set():
            # An overrun hook thread is still mutating this state. Whatever it
            # writes next is not a checkpoint of anything — keep the last good
            # row. (_disable normally drops the module here too; this is the
            # belt to that suspenders.)
            continue
        try:
            # Snapshot FIRST. json.dumps walks the live dict, so a background
            # thread adding a key mid-walk raised RuntimeError into the
            # catch-all below and the save was silently skipped forever. A
            # copy fails fast and loudly (one log line), and a clean copy
            # serializes without racing anyone.
            try:
                snapshot = copy.deepcopy(dict(mod.state))
            except RuntimeError as e:      # changed size during iteration
                logger.warning(
                    f"[scripts] {sid[:8]} state changed while saving ({e}); "
                    "keeping the previous snapshot")
                continue
            blob = json.dumps(snapshot)
            # DROP an oversized state — never truncate it. A JSON string cut
            # mid-value does not parse, so the next load would silently reset
            # the strategy's memory to {} AND the write would have destroyed
            # the last good snapshot on the way. Keeping the previous row is
            # strictly better, and the warning tells the user to trim state.
            if len(blob) > STATE_MAX_BYTES:
                if sid not in _state_oversize_warned:
                    _state_oversize_warned.add(sid)
                    logger.warning(
                        f"[scripts] {sid[:8]} state is {len(blob) // 1024}KB "
                        f"(limit {STATE_MAX_BYTES // 1024}KB) — not persisted; "
                        "it will not survive a restart until you trim it")
                continue
            _state_oversize_warned.discard(sid)
            with db.get_db() as conn:
                db.update_user_script(conn, sid, state_json=blob)
        except (TypeError, ValueError):
            pass  # non-JSON state — skipped, documented limitation
        except Exception as e:
            logger.debug(f"[scripts] state save {sid[:8]}: {e}")


async def _drain_logs(sid: str, mod: script_sandbox.CompiledScript) -> None:
    lines = mod.drain_logs()
    if lines:
        await _emit_event("script:log", {"id": sid, "lines": lines[-40:]})


def _hb_note(sid: str, key: str, n: int = 1) -> None:
    """Count one thing that happened to a script this interval.

    Doubles as the feed for the Scripts page's status line: the heartbeat
    counters reset every interval, so the two "when did this script last
    actually do something" facts a user asks for — last intent, last order —
    are accumulated here instead, where every call site already reports.
    """
    d = _hb.setdefault(sid, {})
    d[key] = d.get(key, 0) + n
    if key.endswith("_intents"):
        st = _stat(sid)
        st["intents"] += n
        st["lastIntentAt"] = _now_iso()
    elif key == "placed":
        st = _stat(sid)
        st["orders"] += n
        st["lastOrderAt"] = _now_iso()


def _heartbeat_line(hb: dict[str, int], *, shadow: bool, open_count: int,
                    day_pnl: float) -> str:
    """One human-readable line describing an interval of engine activity.

    Only hooks the script actually defines get a segment, so a decide()-only
    script doesn't have to read past three "×0" entries to find its own.
    """
    def _n(count: int, singular: str) -> str:
        return f"{count:,} {singular}" + ("" if count == 1 else "s")

    parts = [_n(hb.get("ticks", 0), "tick")]
    for hook in ("decide", "decide_market", "decide_signal", "manage"):
        calls = hb.get(hook, 0)
        if not calls and not hb.get(f"{hook}_hooked"):
            continue
        got = hb.get(f"{hook}_intents", 0)
        noun = "action" if hook == "manage" else "intent"
        parts.append(f"{hook} ×{calls:,} → {_n(got, noun)}")
    placed = hb.get("placed", 0)
    refused = hb.get("refused", 0)
    seg = _n(placed, "shadow order" if shadow else "order")
    if refused:
        # "declined", not "refused by rails": an intent also lands here when
        # there is simply no executable book yet, which is not a rail.
        seg += f", {refused:,} declined"
    parts.append(seg)
    if hb.get("quote") or hb.get("quote_hooked"):
        # Simulated resting bids: how many were placed, re-priced and pulled, and
        # how many the market came through.
        parts.append(
            f"quote ×{hb.get('quote', 0):,} → {hb.get('quote_places', 0):,} placed, "
            f"{hb.get('quote_moves', 0):,} moved, {hb.get('quote_cancels', 0):,} pulled, "
            f"{_n(hb.get('quote_fills', 0), 'fill')}")
    if hb.get("quote_skipped_armed"):
        # quote() is simulated only. An armed script's bids are not sent.
        parts.append("quote skipped (shadow only: put the script back in shadow to run it)")
    if hb.get("supervise_skipped_shadow"):
        # Say it rather than look broken: supervise() is the one hook that
        # reaches the live engine, so shadow skips it entirely.
        parts.append("supervise skipped (shadow)")
    errs = hb.get("errors", 0)
    if errs:
        parts.append(f"{errs:,} ERROR" + ("" if errs == 1 else "S"))
    parts.append(f"{open_count} open, today {day_pnl:+.2f} USD")
    stamp = datetime.now().strftime("%H:%M:%S")
    return f"[{stamp}] " + " · ".join(parts)


async def _maybe_heartbeat(s: dict, mod: script_sandbox.CompiledScript, *,
                           shadow: bool, open_count: int,
                           day_pnl: float) -> None:
    """Emit this script's heartbeat if the interval has elapsed, then reset.
    Never raises — observability must not be able to break a trading pass."""
    sid = str(s["id"])
    try:
        now = time.time()
        if now - _hb_last.get(sid, 0.0) < HEARTBEAT_SEC:
            return
        _hb_last[sid] = now
        hb = _hb.pop(sid, {})
        # Which hooks exist, so the line lists them even at zero calls.
        for hook in ("decide", "decide_market", "decide_signal", "manage"):
            if hook in mod.hooks:
                hb.setdefault(f"{hook}_hooked", 1)
        if "quote" in mod.hooks and shadow:
            hb.setdefault("quote_hooked", 1)
        line = _heartbeat_line(hb, shadow=shadow, open_count=open_count,
                               day_pnl=day_pnl)
        name = str(s.get("name") or sid[:8])
        logger.info(f"[scripts] {name} ({sid[:8]}) {line}")
        await _emit_event("script:log", {"id": sid, "lines": [line]})
    except Exception as e:
        logger.debug(f"[scripts] heartbeat {sid[:8]}: {e}")


async def run_tick(cfg: dict, *, authed: bool) -> None:
    """One engine pass: for each enabled script × asset, ask decide() and
    place qualifying intents. Called from the service master loop; every
    failure path is contained per-script.

    The `scripts_live_enabled` master switch gates REAL ORDERS, not the whole
    engine. A shadow script places nothing on the exchange by construction, so
    it runs the moment the user enables it — the old behavior (master switch
    off = nothing ticks at all) meant a user who enabled a script and watched
    an empty shadow ledger for an hour had no way to tell a selective strategy
    from a switch they hadn't found."""
    if not authed:
        _enabled_hooks.clear()
        return
    with db.get_db() as conn:
        scripts = db.list_user_scripts(conn)
    # Code an AI agent wrote never runs here until the user has reviewed it
    # (needs_review is cleared by the USER enabling it). The save path also
    # forces such rows off; this is the belt for any path that didn't.
    for s in scripts:
        if s.get("enabled") and s.get("needs_review"):
            _gate(str(s["id"]), "needs_review")
    enabled = [s for s in scripts if s.get("enabled") and not s.get("needs_review")]
    # Real orders need the master switch AND a live account (config.engine_on).
    live_ok = __import__("config").engine_on(cfg, "scripts_live_enabled")
    # An ARMED script with the master switch off must not trade. Park it with
    # a recorded reason instead of running it — running it would either place
    # orders the switch forbids, or silently divert them into the shadow
    # ledger, which would corrupt the very track record the ledger exists for.
    if not live_ok:
        for s in enabled:
            if not s.get("dry_run"):
                _gate(str(s["id"]), "master_off")
        enabled = [s for s in enabled if s.get("dry_run")]
    max_enabled = int(cfg.get("script_max_enabled") or 10)
    # Over the concurrency cap the tail is dropped (oldest-first ordering, so
    # the choice is at least stable). Say so — a script that shows "enabled"
    # in the UI while silently never running is the worst possible failure
    # mode for a trading tool. Logged only when the dropped set changes.
    if len(enabled) > max_enabled:
        global _last_starved
        dropped = [str(s.get("name") or s["id"]) for s in enabled[max_enabled:]]
        if dropped != _last_starved:
            _last_starved = dropped
            logger.warning(
                f"[scripts] {len(dropped)} enabled script(s) over the "
                f"script_max_enabled cap ({max_enabled}) are NOT running: "
                f"{', '.join(dropped)} — disable one or raise the cap")
        for s in enabled[max_enabled:]:
            _gate(str(s["id"]), "over_cap")
        enabled = enabled[:max_enabled]
    elif _last_starved:
        _last_starved = []
    if not enabled:
        _enabled_hooks.clear()
        return
    env = polymarket_auth.get_env()
    scripts_by_id = {str(s["id"]): s for s in scripts}

    # Lifecycle notifications first (fills/settlements since last tick).
    try:
        await _notify_lifecycle(scripts_by_id, env, cfg)
    except Exception as e:
        logger.debug(f"[scripts] lifecycle notify: {e}")

    # Book any shadow orders whose window/signal has resolved, so a shadow
    # script's daily-loss breaker and open-count see current numbers below.
    await _settle_shadow_orders()

    # Compile everything FIRST, so the hook census below is complete before
    # any per-hook work is decided. It also fixes an ordering bug the lazy
    # market universe worked around: on the very first tick nothing was
    # compiled yet, so "does any script want X" was unanswerable.
    compiled: list[tuple[dict, script_sandbox.CompiledScript]] = []
    for s in enabled:
        mod = _get_compiled(s, cfg)
        if mod is None:
            _gate(str(s["id"]), "compile_failed")
            await _emit_event("script:status", {
                "id": str(s["id"]), "enabled": False,
                "lastError": "compile failed (see script page)"})
            continue
        compiled.append((s, mod))
    if not compiled:
        _enabled_hooks.clear()
        return

    global _enabled_hooks_at
    _enabled_hooks.clear()
    for _s, _m in compiled:
        _enabled_hooks.update(_m.hooks)
    _enabled_hooks_at = time.time()

    # The crypto snapshot is a ~7-calls-per-series fan-out. Only pay for it
    # when some enabled script actually consumes crypto windows — a
    # market-only or signal-only script has no business driving that traffic,
    # and before scripts were decoupled from the crypto engine this was
    # hidden behind "the crypto engine is polling anyway".
    wants_crypto = bool(_enabled_hooks & {"decide", "manage", "quote"})
    assets: list[dict] = []
    if wants_crypto:
        snap = await crypto15m.snapshot(cfg)
        assets = [a for a in (snap.get("assets") or [])
                  if a.get("hasMarket") and a.get("ticker")]
    assets_by_ticker = {str(a.get("ticker")): a for a in assets}
    # Fetched once per tick, shared by every signal script.
    new_signals = _fetch_new_signals(env) if "decide_signal" in _enabled_hooks else []
    # The general-market universe is loaded LAZILY, on the first script that
    # actually defines decide_market, and then reused for the rest of this
    # tick. Computing it up front would either cost a query every tick for
    # everyone, or miss the first tick (scripts aren't compiled yet here).
    _markets_cache: list[dict] | None = None

    def markets_for_tick() -> list[dict]:
        nonlocal _markets_cache
        if _markets_cache is None:
            _markets_cache = _market_universe(cfg)
        return _markets_cache
    balance_usd = None
    try:
        cents, _port = await trader.refresh_balance(cfg, force=False)
        if cents > 0 or trader.last_balance_read_ok():
            balance_usd = round(cents / 100.0, 2)
    except Exception:
        pass

    for s, mod in compiled:
        sid = str(s["id"])
        shadow = bool(s.get("dry_run"))
        scope = parse_asset_scope(s.get("assets"))
        # Per-script daily loss circuit breaker (both position tables).
        # A shadow script is measured against its OWN simulated ledger, so it
        # hits the same breaker at the same point a live run would.
        try:
            with db.get_db() as conn:
                if shadow:
                    day_pnl = db.script_shadow_daily_pnl(conn, sid, env)
                    open_count = db.count_open_script_shadow(conn, sid, env)
                    open_crypto = []
                else:
                    day_pnl = _script_daily_pnl(conn, sid, env)
                    open_count = _count_open_for_script(conn, sid, env)
                    open_crypto = _open_crypto_for_script(conn, sid, env)
            # 0 explicitly means "off" (documented), so only default when the
            # key is missing — `or 25.0` would silently re-arm a user-set 0.
            _lc = cfg.get("script_daily_loss_usd")
            loss_cap = float(_lc) if _lc is not None else 25.0
            if loss_cap > 0 and day_pnl <= -loss_cap:
                reason = (f"daily loss cap hit ({day_pnl:.2f} <= "
                          f"-{loss_cap:.2f} USD) — re-enable tomorrow or raise the cap")
                _disable(sid, reason)
                await _emit_event("script:status",
                                 {"id": sid, "enabled": False, "lastError": reason})
                continue
        except Exception as e:
            logger.debug(f"[scripts] risk query {sid[:8]}: {e}")
            continue

        _hb_note(sid, "ticks")
        st = _stat(sid)
        st["ticks"] += 1
        st["lastTickAt"] = _now_iso()
        st["gate"] = None
        portfolio = {
            "balanceUsd": balance_usd,
            "openCount": open_count,
            "openPositions": [_pos_lite(p) for p in open_crypto],
            "todayPnlUsd": round(day_pnl, 2),
        }

        # ── manage(): dynamic exits over open crypto positions ────────
        try:
            if "manage" in mod.hooks and open_crypto:
                await _manage_pass(s, mod, cfg, env, assets_by_ticker, portfolio)
        except script_sandbox.SCRIPT_FAILURES as e:
            _disable(sid, str(e))
            await _emit_event("script:status",
                             {"id": sid, "enabled": False, "lastError": str(e)})
            await _drain_logs(sid, mod)
            continue

        # ── decide(): crypto window entries ───────────────────────────
        max_open = int(cfg.get("script_max_open") or 2)
        died = False
        if "decide" in mod.hooks:
            for a in assets:
                if open_count >= max_open:
                    break
                if not script_allows_asset(scope, str(a.get("asset") or "")):
                    continue
                try:
                    with db.get_db() as conn:
                        if _already_attempted(conn, sid, str(a.get("ticker")),
                                              shadow=shadow):
                            continue
                except Exception:
                    continue
                ctx = dict(a)
                ctx["portfolio"] = portfolio
                try:
                    _hb_note(sid, "decide")
                    raw = _call_hook(mod, "decide", cfg, ctx)
                except script_sandbox.SCRIPT_FAILURES as e:
                    _hb_note(sid, "errors")
                    _disable(sid, str(e))
                    await _emit_event("script:status",
                                     {"id": sid, "enabled": False, "lastError": str(e)})
                    died = True
                    break
                if raw is None:
                    continue
                _hb_note(sid, "decide_intents")
                intent, err = sanitize_intent(raw)
                if err:
                    _disable(sid, f"malformed intent: {err}")
                    await _emit_event("script:status",
                                     {"id": sid, "enabled": False,
                                      "lastError": f"malformed intent: {err}"})
                    died = True
                    break
                try:
                    # Cross-engine order gate: a script order competes for the
                    # same wallet caps as the main/copy/crypto engines, which
                    # now tick on their own tasks. _place_intent IS the
                    # check+place region, so it is serialised whole.
                    async with trader.order_gate("script"):
                        placed = await _place_intent(
                            s, a, intent, cfg, env, balance_usd)
                except Exception as e:
                    _hb_note(sid, "errors")
                    logger.error(f"[scripts] place {sid[:8]} {a.get('asset')}: {e}")
                    continue
                if placed is not None:
                    _hb_note(sid, "placed")
                    open_count += 1
                else:
                    _hb_note(sid, "refused")
        if died:
            await _drain_logs(sid, mod)
            continue

        # ── quote(): resting bids on both sides, simulated ────────────
        # Shadow only (see script_quotes). An armed script's quote() is not
        # run: its bids would have to be real, and nothing sends those yet.
        if "quote" in mod.hooks and not shadow:
            _hb_note(sid, "quote_skipped_armed")
        elif "quote" in mod.hooks:
            watched: set = set()
            for a in assets:
                if not script_allows_asset(scope, str(a.get("asset") or "")):
                    continue
                watched.add(str(a.get("ticker")))
                try:
                    err = script_quotes.run_asset(
                        sid, a, cfg, env,
                        lambda c: _call_hook(mod, "quote", cfg, c),
                        lambda key: _hb_note(sid, key), portfolio)
                except script_sandbox.SCRIPT_FAILURES as e:
                    err = str(e)
                if err:
                    _hb_note(sid, "errors")
                    _disable(sid, f"quote: {err}")
                    await _emit_event("script:status",
                                     {"id": sid, "enabled": False,
                                      "lastError": f"quote: {err}"})
                    died = True
                    break
            script_quotes.drop_ended(sid, watched)
        if died:
            await _drain_logs(sid, mod)
            continue

        # ── decide_market(): general Polymarket markets ───────────────
        if "decide_market" in mod.hooks:
            # The scan is synchronous inside the master loop: every market is
            # one timeout-bounded hook call, so the worst case is
            # universe_size × HOOK_TIMEOUT_S (150 × 1s) of blocked loop per
            # script per tick — which would stall the crypto manage/exit pass
            # it. Bound the SCAN, not just each call: stop early and pick up
            # where the budget ran out. Markets are volume-ranked and the
            # universe is stable tick to tick, so a slow script degrades to
            # scanning fewer markets per tick instead of delaying every exit.
            scan_deadline = time.monotonic() + MARKET_SCAN_BUDGET_S
            scanned = 0
            for m in markets_for_tick():
                if open_count >= max_open:
                    break
                if time.monotonic() > scan_deadline:
                    logger.info(
                        f"[scripts] {sid[:8]} decide_market scan hit its "
                        f"{MARKET_SCAN_BUDGET_S:g}s budget after {scanned} "
                        "markets — remainder deferred to the next tick")
                    break
                scanned += 1
                # Cooldown first: it is a dict lookup, and skipping here also
                # skips the CLOB quote the rails would otherwise re-fetch for
                # a market they are certain to refuse again.
                if _in_cooldown(sid, str(m.get("ticker"))):
                    continue
                try:
                    with db.get_db() as conn:
                        if _market_already_attempted(
                                conn, sid, str(m.get("ticker")), shadow=shadow):
                            continue
                except Exception:
                    continue
                mctx = market_to_js(m)
                mctx["portfolio"] = portfolio
                try:
                    _hb_note(sid, "decide_market")
                    raw = _call_hook(mod, "decide_market", cfg, mctx)
                except script_sandbox.SCRIPT_FAILURES as e:
                    _hb_note(sid, "errors")
                    _disable(sid, str(e))
                    await _emit_event("script:status",
                                     {"id": sid, "enabled": False, "lastError": str(e)})
                    died = True
                    break
                if raw is None:
                    continue
                _hb_note(sid, "decide_market_intents")
                intent, err = sanitize_market_intent(raw)
                if err:
                    _disable(sid, f"malformed decide_market intent: {err}")
                    await _emit_event("script:status",
                                     {"id": sid, "enabled": False,
                                      "lastError": f"malformed decide_market intent: {err}"})
                    died = True
                    break
                try:
                    async with trader.order_gate("script"):
                        placed = await _place_market_intent(
                            s, m, intent, cfg, env, balance_usd)
                except Exception as e:
                    _hb_note(sid, "errors")
                    logger.error(
                        f"[scripts] market place {sid[:8]} {m.get('ticker')}: {e}")
                    continue
                if placed is not None:
                    _hb_note(sid, "placed")
                    open_count += 1
                else:
                    _hb_note(sid, "refused")
        if died:
            await _drain_logs(sid, mod)
            continue

        # ── decide_signal(): whale/momentum signal follows ────────────
        if "decide_signal" in mod.hooks and new_signals:
            for sig, source in new_signals:
                if open_count >= max_open:
                    break
                try:
                    _hb_note(sid, "decide_signal")
                    raw = _call_hook(mod, "decide_signal", cfg, signal_to_js(sig, source))
                except script_sandbox.SCRIPT_FAILURES as e:
                    _hb_note(sid, "errors")
                    _disable(sid, str(e))
                    await _emit_event("script:status",
                                     {"id": sid, "enabled": False, "lastError": str(e)})
                    died = True
                    break
                act, err = sanitize_signal_action(raw)
                if err:
                    _hb_note(sid, "errors")
                    _disable(sid, f"malformed decide_signal return: {err}")
                    died = True
                    break
                if act is None:
                    continue
                _hb_note(sid, "decide_signal_intents")
                try:
                    async with trader.order_gate("script"):
                        placed = await _place_signal_follow(
                            s, sig, source, act, cfg, env, balance_usd)
                except Exception as e:
                    _hb_note(sid, "errors")
                    logger.error(f"[scripts] signal follow {sid[:8]}: {e}")
                    continue
                if placed is not None:
                    _hb_note(sid, "placed")
                    open_count += 1
                else:
                    _hb_note(sid, "refused")
        if died:
            await _drain_logs(sid, mod)
            continue

        # ── supervise(): whitelisted config / engine switching ────────
        # Skipped in shadow — see _run_supervise, which enforces it.
        try:
            if "supervise" in mod.hooks and shadow:
                _hb_note(sid, "supervise_skipped_shadow")
            elif "supervise" in mod.hooks:
                app = {
                    "hourUtc": datetime.now(timezone.utc).hour,
                    "balanceUsd": balance_usd,
                    "scriptTodayPnlUsd": round(day_pnl, 2),
                    "engines": {
                        "mainTrading": bool(cfg.get("enable_trading")),
                        "whales": bool(cfg.get("trade_whales")),
                        "momentum": bool(cfg.get("trade_momentum")),
                        "crypto15m": bool(cfg.get("crypto15m_enabled")),
                        "copy": bool(cfg.get("copy_enabled")),
                    },
                    "config": {k: cfg.get(k) for k in _SUPERVISE_KEYS},
                    "assets": [{
                        "asset": a.get("asset"), "minsLeft": a.get("minsLeft"),
                        "sigma1m": a.get("sigma1m"), "modelProb": a.get("modelProb"),
                        "favoritePrice": a.get("favoritePrice"),
                    } for a in assets],
                }
                await _run_supervise(s, mod, cfg, app, shadow=shadow)
        except script_sandbox.SCRIPT_FAILURES as e:
            _hb_note(sid, "errors")
            _disable(sid, str(e))
            await _emit_event("script:status",
                             {"id": sid, "enabled": False, "lastError": str(e)})

        await _drain_logs(sid, mod)
        await _maybe_heartbeat(s, mod, shadow=shadow, open_count=open_count,
                               day_pnl=day_pnl)
    _save_states()

