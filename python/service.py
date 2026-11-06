"""Krypt PolyBot US backend service.

Spawned as a child process by the Electron main process. Communicates
exclusively via JSON-line stdin/stdout (no HTTP server, no shared
filesystem state besides the SQLite DB and the credential files in
<userData>).

Wire protocol — one JSON object per line:

  Command         {"type": "rpc", "id": "<uuid>", "method": "...", "params": {...}}
  Response (ok)   {"type": "rpc", "id": "<uuid>", "ok": true, "result": {...}}
  Response (err)  {"type": "rpc", "id": "<uuid>", "ok": false, "error": "..."}
  Event           {"type": "event", "name": "...", "data": {...}}
  Log             {"type": "log", "level": "INFO", "source": "...", "msg": "..."}

Methods (called from Electron main):
  ping
  setConfig          {config: {...}}
  setCredentials     {privateKey, apiCreds?}
  clearCredentials
  credentialStatus
  testCredentials
  account
  pnlSeries          {sinceHours?}
  positions          {filter?}
  signals            {filter?}
  scannerStats
  cancelAllOpen
  flatten
  runOnce            {action}                 -- syncMarkets|pollOrders|resolveAll|reconcilePositions|syncPositions|recomputePnl|reconcileFills|auditPnl
  pause              {paused: bool}
  shutdown
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# `--mcp` is the stdio relay an AI client launches (mcp_stdio.py). It must
# branch off HERE, before logging is wired: below this line every log record
# is mirrored to stdout, and on the relay stdout is the MCP protocol stream.
if __name__ == "__main__" and "--mcp" in sys.argv:
    import mcp_stdio
    sys.exit(mcp_stdio.main())

# `--backtest-child` runs ONE script backtest for an AI agent, in a process
# that never loads a credential (script_backtest_child.py). Same rule: off
# before logging, because stdout carries its one-line answer.
if __name__ == "__main__" and "--backtest-child" in sys.argv:
    import script_backtest_child
    sys.exit(script_backtest_child.main())

# Run as a script (dev and the frozen build alike) this file is `__main__`,
# not `service`. Every other module reaches the RUNNING backend with a lazy
# `import service` — the AI agents' tools and rails, the phone commands, the
# terminal's paper gate — and without this alias that import loads a SECOND
# copy of this file with its own empty STATE: default config, no wallet. The
# tests never saw it (they fake the module); the running app did.
if __name__ == "__main__":
    sys.modules.setdefault("service", sys.modules[__name__])

# ────────────────────────────────────────────────────────────────────
# Logging — every log line is mirrored to stdout (parent picks it up
# as a `log` event) AND written to a rotating file under
# <userData>/logs/backend.log so the user can grep them later.
# ────────────────────────────────────────────────────────────────────


_log_q: asyncio.Queue | None = None


class _StdoutHandler(logging.Handler):
    """Forward every log record up to the Electron parent as a JSON line."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        source = "backend"
        if record.name.startswith("trader"):
            source = "trader"
        elif record.name.startswith("scanner"):
            source = "whale" if "whale" in msg.lower()[:20] else "momentum"
        elif record.name.startswith("discord"):
            source = "discord"
        try:
            evt = {
                "type": "log",
                "level": record.levelname,
                "source": source,
                "msg": msg,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            sys.stdout.write(json.dumps(evt) + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def _setup_logging() -> None:
    log_dir_base = os.environ.get("KRYPT_POLYBOT_US_USERDATA")
    if log_dir_base:
        log_dir = Path(log_dir_base) / "logs"
    else:
        log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "backend.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = _StdoutHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # Credentials never reach a log line: one scrubber on BOTH handlers (the
    # file and the stdout stream the renderer's log view reads). Known values
    # are loaded at startup (_main) and after any credential change.
    try:
        import logscrub
        logscrub.install(root)
    except Exception:  # noqa: BLE001 — a missing scrubber is a selftest failure
        pass
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# ────────────────────────────────────────────────────────────────────
# Imports (after logging is wired so import-time messages flow up).
# ────────────────────────────────────────────────────────────────────


_setup_logging()
logger = logging.getLogger("service")


import db  # noqa: E402
import polymarket_api  # noqa: E402
import polymarket_auth  # noqa: E402
import scanner  # noqa: E402
import crypto15m  # noqa: E402
import trader  # noqa: E402
import crypto15m_trader  # noqa: E402
import crypto15m_record  # noqa: E402
import user_ws  # noqa: E402
import script_engine  # noqa: E402
import script_sandbox  # noqa: E402
import index_feed  # noqa: E402
import spot_ws  # noqa: E402
from config import DEFAULT_CONFIG, merge_with_defaults  # noqa: E402


def _iso_utc(s: Any) -> Any:
    """Normalise a SQLite naive-UTC timestamp ("YYYY-MM-DD HH:MM:SS") into
    a real ISO 8601 UTC string ("YYYY-MM-DDTHH:MM:SSZ").

    Why: SQLite's `datetime('now')` writes naive UTC strings without a Z.
    JavaScript's Date parser then interprets those strings as LOCAL time
    (Chrome behaviour), which made every "Started at" / "Resolved at"
    field show up 7 hours off for users in PT. Tagging with Z makes JS
    parse them correctly so `toLocaleString()` formats in the user's
    real local zone."""
    if not s:
        return s
    if not isinstance(s, str):
        return s
    s = s.strip()
    if not s:
        return s
    # Already ISO 8601 with timezone? Leave it alone.
    if s.endswith("Z") or "+" in s[10:] or s.count("-") > 2:
        return s.replace(" ", "T")
    # SQLite "YYYY-MM-DD HH:MM:SS[.ffffff]" → "YYYY-MM-DDTHH:MM:SSZ"
    return s.replace(" ", "T") + "Z"


# ────────────────────────────────────────────────────────────────────
# Service state
# ────────────────────────────────────────────────────────────────────


class State:
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    auth_ok: bool = False
    paused: bool = False  # external pause (different from enable_trading)
    last_whale_scan_at: str | None = None
    last_momentum_scan_at: str | None = None
    last_trade_scan_at: str | None = None
    started_at: str = ""
    # ID of the currently-open bot_runs row for this process.
    # Session P&L on the dashboard is computed against this run's
    # `start_total_usd` snapshot.
    active_run_id: int = 0
    # Unredeemed winnings watch — winning positions settle as ERC-1155 tokens the
    # bot never redeems itself, so if Polymarket's opt-in auto-redeem is OFF they
    # sit unredeemed and the tradable balance silently shrinks. Polled on a slow
    # cadence and surfaced in the account snapshot; `unredeemed_since` (wall clock)
    # is when winnings first went unredeemed, used to tell "auto-redeem is off"
    # (persists) from "briefly redeemable before the relayer clears it".
    unredeemed_winnings_usd: float = 0.0
    unredeemed_winnings_count: int = 0
    unredeemed_since: float = 0.0


STATE = State()


# ────────────────────────────────────────────────────────────────────
# Output helpers
# ────────────────────────────────────────────────────────────────────


_stdout_lock = asyncio.Lock()


async def _send(obj: dict) -> None:
    line = json.dumps(obj, default=str) + "\n"
    async with _stdout_lock:
        out = sys.stdout
        out.write(line)
        # `_main()` rebuilds stdout with line_buffering=True, so the trailing
        # newline already pushed the line through to the parent — a second
        # explicit flush was pure syscall overhead on a hot path. Any stream
        # that is NOT line-buffered (a test harness stream, or the raw
        # block-buffered pipe if anything writes before _main installs the
        # wrapper) still gets flushed, so no line can ever sit in a buffer.
        if not getattr(out, "line_buffering", False):
            out.flush()


async def emit_event(name: str, data: Any = None) -> None:
    await _send({"type": "event", "name": name, "data": data})


async def respond_ok(req_id: str, result: Any = None) -> None:
    await _send({"type": "rpc", "id": req_id, "ok": True, "result": result})


async def respond_err(req_id: str, msg: str) -> None:
    await _send({"type": "rpc", "id": req_id, "ok": False, "error": msg})


# ────────────────────────────────────────────────────────────────────
# Status / account aggregator
# ────────────────────────────────────────────────────────────────────


def _balance_persistable() -> bool:
    """Whether this account snapshot may be written down as a balance point
    (the P&L chart, the daily stop's input, transfer detection, the run row).
    Not while /value keeps failing: that total is today's cash plus a stale
    portfolio, and once persisted it becomes the reading the daily stop acts
    on and the baseline the next real reading is measured against. The
    snapshot still goes to the UI."""
    return bool(STATE.auth_ok) and not trader.portfolio_stale()


async def _build_account_snapshot() -> dict:
    env = polymarket_auth.get_env()
    cash_cents = port_cents = 0
    # Don't gate the balance read on auth_ok: when auth briefly drops (region
    # blip, key refresh) refresh_balance still returns the last-known cached
    # value (get_balance→None keeps the cache), so the snapshot shows the real
    # balance instead of collapsing to $0 / −100% ROI. A genuine never-authed
    # session just yields the (0,0) default, same as before.
    try:
        cents, port = await trader.refresh_balance(STATE.cfg, force=False)
        cash_cents = cents
        port_cents = port
    except Exception:
        pass
    cash_usd = cash_cents / 100.0
    port_usd = port_cents / 100.0
    # total trusts /value as the authoritative portfolio mark. A genuine /value
    # FAILURE is already handled upstream (get_balance returns None →
    # refresh_balance keeps the last-known value), so we don't second-guess it
    # here with a local cost-basis floor: that floor over-counts positions that
    # have SETTLED on-chain (/value correctly drops to 0) but whose local row
    # hasn't been marked resolved yet, inflating the balance until resolution
    # catches up.
    total = cash_usd + port_usd

    # Bankroll baseline: user-pinned value if > 0, else the earliest
    # snapshotted total for this env (so ROI is computed from the moment
    # they first connected), else current total (ROI = 0% on first tick).
    user_start = float(STATE.cfg.get("start_bankroll_usd", 0.0) or 0.0)
    if user_start > 0:
        baseline = user_start
        baseline_source = "user"
    else:
        with db.get_db() as conn:
            earliest = db.earliest_pnl_total(conn, env)
        if earliest and earliest > 0:
            baseline = earliest
            baseline_source = "auto"
        else:
            baseline = total if total > 0 else 0.0
            baseline_source = "live"
    # NOTE: roiPct is computed further down from the SAME transfer-adjusted,
    # earliest-snapshot baseline as the dollar all-time P&L (alltime_balance_*),
    # so the percent can never disagree in sign with the dollars. Computing it
    # here off `baseline` alone made the % collapse to +0.00% whenever
    # start_bankroll_usd was unset even though the dollar figure was correctly
    # negative.

    with db.get_db() as conn:
        stats_env = db.aggregate_stats(conn, env)
        # A just-filled entry or just-resolved settlement means the venue's
        # cash/value reads and our position ledger are momentarily out of
        # step — flag it so the UI can say "syncing" instead of flashing a
        # phantom one-position dip in the balance/P&L tiles.
        balance_syncing = db.recent_balance_transition(conn, env)
    wl = stats_env["wins"] + stats_env["losses"]
    wr = (stats_env["wins"] / wl * 100.0) if wl else 0.0
    open_cost = stats_env["open_cost"]
    unrealized = port_usd - open_cost

    # ── Balance-snapshot P&L (the simple truth) ──────────────────────
    # Position-derived P&L can drift if any per-trade attribution is
    # off, so we ALSO surface the raw "balance now − balance at
    # period-start" number, which is what the user actually cares about.
    with db.get_db() as conn:
        first_today = db.first_snapshot_of_today(conn, env)
        bankroll_baseline_snap = db.earliest_pnl_total(conn, env)
        active_run = db.get_active_run(conn, env) if STATE.active_run_id else None
        today_transfers = db.transfer_adjustment_today(conn, env)
        alltime_transfers = db.transfer_adjustment_total(conn, env)
        session_transfers = (
            db.transfer_adjustment_run(conn, env, int(active_run.get("id") or 0))
            if active_run else 0.0
        )

    today_balance_baseline = (
        float(first_today["total_usd"]) if first_today else None
    )
    # Net of detected deposits/withdrawals — kept identical to the daily-risk
    # gate's number (trader._today_pnl_balance_delta) so the tile and the stop
    # can never disagree.
    today_balance_pnl = (
        total - today_balance_baseline - today_transfers
        if today_balance_baseline is not None else 0.0
    )
    alltime_balance_baseline = (
        float(bankroll_baseline_snap)
        if bankroll_baseline_snap is not None else baseline
    )
    # Net of ALL detected deposits/withdrawals, not just today's — without
    # this every withdrawal read as a permanent lifetime trading loss (the
    # "negative all-time P&L" some users reported after moving funds out).
    alltime_balance_pnl = total - alltime_balance_baseline - alltime_transfers
    # ROI % is derived from the SAME transfer-adjusted, earliest-snapshot
    # baseline as the dollar figure above, so the percent can never disagree in
    # sign with the dollars. The old code divided (total - baseline) by a
    # separately-derived `baseline` that was 0 whenever start_bankroll_usd was
    # unset — the divide-by-zero guard then forced +0.00% even while the dollar
    # P&L was correctly negative (the reported bug). When there's genuinely no
    # baseline (fresh empty account) it still reads 0%, which is correct.
    roi = (
        (alltime_balance_pnl / alltime_balance_baseline * 100.0)
        if alltime_balance_baseline > 0 else 0.0
    )

    # ── Session P&L (the new headline number) ────────────────────────
    # current_total - start_total_of_this_bot_run. This is what the
    # user wants to see: "since I hit Start, how am I doing?"
    if active_run:
        session_baseline = float(active_run.get("start_total_usd") or 0.0)
        session_started_at = _iso_utc(
            active_run.get("started_at") or STATE.started_at
        )
        session_run_id = int(active_run.get("id") or 0)
    else:
        session_baseline = total
        session_started_at = STATE.started_at
        session_run_id = 0
    # Net of transfers detected during THIS run: a mid-run withdrawal must not
    # fake a deep-red session, and a mid-run deposit must not fake a green one.
    session_pnl = (
        total - session_baseline - session_transfers
        if session_baseline > 0 else 0.0
    )
    session_roi = (
        (session_pnl / session_baseline * 100.0)
        if session_baseline > 0 else 0.0
    )

    return {
        "cashUsd": cash_usd,
        "portfolioUsd": port_usd,
        "totalUsd": total,
        "wallet": trader.wallet_status(env),
        "balanceSyncing": balance_syncing,
        # CLOB rejected a recent order with its region-restriction 403 —
        # market data flows fine in this state, so without an explicit flag
        # the app looks healthy while every placement bounces.
        "tradingGeoblocked": polymarket_api.geoblock_active(),
        # Polymarket's proactive region check ({status, country, region,
        # checkedAt, message, ageSec} or None before the first one). A stale
        # verdict is re-checked in the background once a wallet is connected.
        "tradingRegion": polymarket_api.region_snapshot(refresh=bool(STATE.auth_ok)),
        # Matching-engine restart / post-restart post-only window (None when
        # trading normally).
        "tradingRestriction": polymarket_api.trading_restriction(),
        "userFeed": user_ws.feed.status(),
        "startBankrollUsd": baseline,
        "bankrollSource": baseline_source,
        "roiPct": roi,
        # Position-derived (computed from per-trade settlement). Kept for
        # transparency but no longer the headline number.
        "realizedPnlUsd": stats_env["realized_pnl"],
        # Balance-snapshot derived (the simple truth). These are now the
        # primary numbers shown on the dashboard / topbar.
        "todayPnlUsd": today_balance_pnl,
        "alltimePnlUsd": alltime_balance_pnl,
        "todayBaselineUsd": today_balance_baseline,
        "alltimeBaselineUsd": alltime_balance_baseline,
        # ── Session = current bot run ───────────────────────────────
        "sessionPnlUsd": session_pnl,
        "sessionRoiPct": session_roi,
        "sessionBaselineUsd": session_baseline,
        "sessionStartedAt": session_started_at,
        "sessionRunId": session_run_id,
        # Position-counter wins/losses (still useful for context)
        "todayWins": stats_env["today_wins"],
        "todayLosses": stats_env["today_losses"],
        # Unrealized = current portfolio mark − cost of open positions
        "unrealizedPnlUsd": unrealized,
        "openCostUsd": open_cost,
        # Unredeemed winnings (auto-redeem-off warning). `stale` = winnings have
        # persisted long enough that Polymarket auto-redeem is clearly not running,
        # so the dashboard should prompt the user to enable it / redeem.
        "unredeemedWinningsUsd": STATE.unredeemed_winnings_usd,
        "unredeemedWinningsCount": STATE.unredeemed_winnings_count,
        "unredeemedWinningsStale": bool(
            STATE.unredeemed_since
            and STATE.unredeemed_winnings_usd >= 1.0
            and (asyncio.get_event_loop().time() - STATE.unredeemed_since) >= 300.0
        ),
        "feesUsd": stats_env["fees"],
        "wins": stats_env["wins"],
        "losses": stats_env["losses"],
        "winRate": wr,
        "pendingCount": stats_env["pending"],
        "openCount": stats_env["open_filled"],
        "resolvedCount": stats_env["resolved_count"],
        "totalOpened": stats_env["total_opened"],
        "byNetwork": {
            "mainnet": {
                "wins": stats_env["wins"],
                "losses": stats_env["losses"],
                "realizedPnl": stats_env["realized_pnl"],
            },
        },
    }


# ────────────────────────────────────────────────────────────────────
# Position / signal serialisers (snake_case -> camelCase for JS side)
# ────────────────────────────────────────────────────────────────────


def _live_pnl_usd(r: dict) -> float | None:
    """Unrealized P&L for an OPEN filled position: held contracts valued at
    the live mark price minus cost. None for resolved rows (use realized
    pnl), unfilled rows, or rows without a mark yet."""
    if r.get("resolved"):
        return None
    mark = r.get("mark_price_cents")
    if mark is None:
        return None
    filled = int(r.get("filled_contracts") or 0)
    if filled <= 0:
        return None
    market_value = filled * float(mark) / 100.0
    return round(market_value - float(r.get("cost_usd") or 0.0), 2)


def _position_row_to_js(r: dict) -> dict:
    return {
        "id": int(r["id"]),
        "signalSource": r["signal_source"],
        "signalId": int(r["signal_id"]),
        "ticker": r["ticker"],
        "eventTicker": r.get("event_ticker") or "",
        "title": r.get("title") or "",
        "category": r.get("category") or "",
        "direction": r["direction"],
        "action": r.get("action") or "buy",
        "targetContracts": int(r.get("target_contracts") or 0),
        "limitPriceCents": int(r.get("limit_price_cents") or 0),
        "filledContracts": int(r.get("filled_contracts") or 0),
        "avgFillPriceCents": (
            float(r["avg_fill_price_cents"])
            if r.get("avg_fill_price_cents") is not None
            else None
        ),
        "costUsd": float(r.get("cost_usd") or 0),
        "feesUsd": float(r.get("fees_usd") or 0),
        "clientOrderId": r.get("client_order_id") or "",
        "orderId": r.get("order_id"),
        "status": r["status"],
        "confidence": float(r.get("confidence") or 0),
        "edgePts": float(r.get("edge_pts") or 0),
        "signalPriceCents": float(r.get("signal_price") or 0),
        "resolved": bool(r.get("resolved") or 0),
        "outcomeCorrect": (
            int(r["outcome_correct"])
            if r.get("outcome_correct") is not None
            else None
        ),
        "settlementUsd": (
            float(r["settlement_usd"])
            if r.get("settlement_usd") is not None
            else None
        ),
        "pnlUsd": float(r["pnl_usd"]) if r.get("pnl_usd") is not None else None,
        "markPriceCents": (
            float(r["mark_price_cents"])
            if r.get("mark_price_cents") is not None
            else None
        ),
        # Unrealized (mark-to-market) P&L for a still-open filled position:
        # what the held contracts are worth NOW at the live mark, minus what
        # we paid. None until it has filled + has a mark; resolved rows use
        # the realized `pnlUsd` instead.
        "livePnlUsd": _live_pnl_usd(r),
        "balanceBeforeUsd": (
            float(r["balance_before_usd"])
            if r.get("balance_before_usd") is not None
            else None
        ),
        "network": r.get("network") or "mainnet",
        "createdAt": _iso_utc(r.get("created_at")) or "",
        "lastUpdated": _iso_utc(r.get("last_updated")) or "",
        "resolvedAt": _iso_utc(r.get("resolved_at")),
        "error": r.get("error"),
    }


def _signal_row_to_js(r: dict, source: str, traded: bool) -> dict:
    if source == "whale":
        price_frac = float(r.get("price") or 0)
        price_c = int(round(price_frac * 100))
        return {
            "id": int(r["id"]),
            "source": "whale",
            "ticker": r["ticker"],
            "eventTicker": r.get("event_ticker") or "",
            "title": r.get("title") or r.get("ticker", ""),
            "category": r.get("category") or "",
            "direction": (r.get("taker_side") or "yes").lower(),
            "priceCents": price_c,
            "confidence": float(r.get("confidence") or 0),
            "edgePts": float(r.get("confidence") or 0) - price_c,
            "dollarValue": float(r.get("dollar_value") or 0),
            "createdAt": _iso_utc(r.get("created_at")) or "",
            "resolved": bool(r.get("resolved") or 0),
            "outcomeCorrect": (
                int(r["outcome_correct"])
                if r.get("outcome_correct") is not None
                else None
            ),
            "pnlEstimate": (
                float(r["pnl_estimate"])
                if r.get("pnl_estimate") is not None
                else None
            ),
            "traded": traded,
        }
    # momentum
    direction = (r.get("direction") or "yes").lower()
    price_frac = float(r.get("price") or 0)
    yes_c = int(round(price_frac * 100))
    cost_c = yes_c if direction == "yes" else max(0, 100 - yes_c)
    implied = yes_c if direction == "yes" else 100 - yes_c
    return {
        "id": int(r["id"]),
        "source": "momentum",
        "ticker": r["ticker"],
        "eventTicker": r.get("event_ticker") or "",
        "title": r.get("title") or r.get("ticker", ""),
        "category": r.get("category") or "",
        "direction": direction,
        "priceCents": cost_c,
        "confidence": float(r.get("confidence") or 0),
        "edgePts": float(r.get("confidence") or 0) - implied,
        "signalType": r.get("signal_type") or "",
        "createdAt": _iso_utc(r.get("created_at")) or "",
        "resolved": bool(r.get("resolved") or 0),
        "outcomeCorrect": (
            int(r["outcome_correct"])
            if r.get("outcome_correct") is not None
            else None
        ),
        "pnlEstimate": (
            float(r["pnl_estimate"])
            if r.get("pnl_estimate") is not None
            else None
        ),
        "traded": traded,
    }


# ────────────────────────────────────────────────────────────────────
# Background tasks (whale scanner, momentum scanner, trader, resolver)
# ────────────────────────────────────────────────────────────────────


_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None
# Watchdog: the scanner/trader loop stamps `_loop_heartbeat` each iteration; a
# separate task force-restarts the loop if the heartbeat goes stale, so open
# real-money positions never silently stop being managed (stop-loss / settlement
# / reconcile) if some await ever hangs. Every network call in the loop is
# already timeout-bounded, so a true freeze is unlikely — this is the backstop.
_watchdog_task: asyncio.Task | None = None
_loop_heartbeat: float = 0.0
# A healthy iteration is ~1s; even a heavy one (gated market sync) with bounded
# retries finishes well under this. 240s of zero progress = genuinely stuck.
_LOOP_STALL_SEC = 240.0
_WATCHDOG_CHECK_SEC = 20.0

# ── the two latency-critical engines run on their OWN tasks ─────────
# Copy and crypto15m used to be two of ~19 blocks awaited in sequence by the
# master loop, so a copy push (a followed wallet just sold) or a crypto tick
# (a 15-minute window about to close) waited behind the whale scan, the
# momentum scan, the position poll, the market sync and everything else — a
# 0-10s variable delay in front of the only two engines where latency is the
# edge. They now tick on their own cadence, with their own heartbeats, and are
# supervised by the same watchdog. `_loop_*` still names the MASTER loop.
_crypto_task: asyncio.Task | None = None
_crypto_heartbeat: float = 0.0
# These loops tick every 1-5s, so "no progress" is obvious far sooner than on
# the master loop (which legitimately has 120s blocks). Still generous next to
# the 60s per-block ceilings.
_ENGINE_STALL_SEC = 120.0

# Watchdog registry: (label, task global, heartbeat global, factory global,
# stall-threshold global). Everything is referenced BY NAME and resolved
# through globals() at check time so monkeypatching any of them (tests do)
# is honoured, and so a restart re-spawns the same coroutine the starter used.
_ENGINE_LOOPS: tuple[tuple[str, str, str, str, str], ...] = (
    ("trading", "_loop_task", "_loop_heartbeat",
     "_scanner_and_trader_loop", "_LOOP_STALL_SEC"),
    ("crypto15m", "_crypto_task", "_crypto_heartbeat",
     "_crypto15m_loop", "_ENGINE_STALL_SEC"),
)
# Exponential backoff per loop, so a loop that dies on every start (a bad
# config, a poisoned dependency) can't spin the watchdog restarting it 3× a
# minute forever. Cleared once a loop stays up for a minute.
_loop_restart_state: dict[str, dict] = {}
_LOOP_RESTART_BACKOFF_MAX = 60.0

_bg_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> None:
    """Run a coroutine as a detached background task so a slow call can never
    stall the trading loop (the balance refresh after a fill goes through a
    retry ladder that can take seconds — exactly when open positions need
    their management cadence). Errors are swallowed; a strong reference is
    kept so the task can't be garbage-collected mid-flight."""
    async def _quiet():
        try:
            await coro
        except Exception:
            pass
    try:
        t = asyncio.get_event_loop().create_task(_quiet())
    except RuntimeError:
        coro.close()
        return
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


def _whale_collection_on(cfg: dict) -> bool:
    """Whale/momentum collection never runs here: Polymarket US publishes no
    per-trader data or public trade history, so these scanners have nothing to
    read. Kept as one named gate because the master loop and the signal scripts
    both ask it."""
    return False


# ── market sync, off the master loop ────────────────────────────────
# A full sync is ~10 Gamma pages plus ~1,000 sqlite upserts. Nothing downstream
# needs it in the same tick (the markets table is read by the NEXT scan), so
# the recurring pass runs detached: a slow or wedged Gamma delays the next
# market refresh instead of delaying every trading engine. Single-flight — a
# sync that overruns its own interval must never stack up N copies fighting for
# the same write transaction.
_market_sync_task: asyncio.Task | None = None


def _start_market_sync(max_pages: int = 10) -> bool:
    """Start a background market sync. False if one is already in flight."""
    global _market_sync_task
    if _market_sync_task and not _market_sync_task.done():
        # A pending task belonging to a DEAD event loop (the process re-entered
        # asyncio.run, or a test drove the loop in its own) can never complete,
        # and treating it as in-flight would wedge the sync forever.
        try:
            alive = _market_sync_task.get_loop() is asyncio.get_event_loop()
        except RuntimeError:
            alive = True
        if alive:
            return False

    async def _run() -> None:
        try:
            cnt = await scanner.sync_markets(max_pages=max_pages)
            logger.debug(f"market sync: {cnt} markets")
        except Exception as e:
            logger.warning(f"market sync failed: {e}")

    try:
        _market_sync_task = asyncio.get_event_loop().create_task(_run())
    except RuntimeError:
        return False
    _bg_tasks.add(_market_sync_task)
    _market_sync_task.add_done_callback(_bg_tasks.discard)
    return True


# ── per-block instrumentation for the master loop ───────────────────
# The loop awaits ~18 engine blocks in sequence, so one slow block is dead time
# for every other engine. `_block` times each one, keeps the last measurement
# (milliseconds) so the UI/diagnostics can see WHICH block is expensive, and
# caps it with a generous ceiling: a single wedged await can no longer eat the
# whole 240s watchdog window and take every other engine down with it. A
# ceiling hit is a SKIP (logged, loop moves to the next block), never a crash —
# `_loop_watchdog` stays the outer guard for a loop that stops progressing at all.
_loop_block_ms: dict[str, float] = {}
_BLOCK_CEILING_SEC = 60.0     # generous: normal blocks finish in well under 1s
_BLOCK_SLOW_SEC = 5.0         # above this a block gets a WARNING, not DEBUG


@contextlib.asynccontextmanager
async def _block(name: str, ceiling_sec: float = _BLOCK_CEILING_SEC):
    """Time + ceiling one master-loop block. Swallows the ceiling timeout."""
    t0 = time.perf_counter()
    timed_out = False
    try:
        # asyncio.timeout (3.11+) lets the block body stay inline instead of
        # being refactored into a coroutine per block.
        async with asyncio.timeout(ceiling_sec):
            yield
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _loop_block_ms[name] = round(ms, 1)
        if timed_out:
            logger.warning(
                f"[loop] block '{name}' hit its {ceiling_sec:.0f}s ceiling and was "
                "skipped this tick (the loop continues with the next block)"
            )
        elif ms >= _BLOCK_SLOW_SEC * 1000.0:
            logger.warning(f"[loop] block '{name}' took {ms / 1000.0:.1f}s")
        else:
            logger.debug(f"[loop] block '{name}' {ms:.0f}ms")


async def _on_crypto15m_auto_off(info: dict) -> None:
    """The crypto15m engine hit its overall take-profit target and turned itself
    off (it already flipped crypto15m_enabled in STATE.cfg, which it shares by
    reference). Tell the host (Electron) so it PERSISTS crypto15m_enabled=False
    and reflects it in the UI toggle, and push a fresh snapshot so the dashboard
    updates at once."""
    try:
        STATE.cfg["crypto15m_enabled"] = False  # belt-and-suspenders
    except Exception:
        pass
    gained = float(info.get("gained") or 0.0)
    target = float(info.get("target") or 0.0)
    logger.info(
        f"crypto15m overall take-profit hit (gained ${gained:+.2f} >= "
        f"${target:.2f}); engine turned OFF"
    )
    await emit_event("crypto15m:autoOff", {
        "reason": info.get("reason") or "take_profit_total",
        "gained": gained,
        "target": target,
    })
    try:
        await emit_event("account:update", await _build_account_snapshot())
    except Exception:
        pass


async def _scanner_and_trader_loop() -> None:
    """Single coroutine that runs every periodic task on its own cadence."""
    global _loop_heartbeat
    # Stamp before the (potentially slow) initial sync so the watchdog has a
    # baseline; re-stamped each iteration below.
    _loop_heartbeat = asyncio.get_event_loop().time()
    last_whale = 0.0
    last_momentum = 0.0
    last_trade = 0.0
    last_poll = 0.0
    last_resolve = 0.0
    last_market_sync = 0.0
    last_event_sync = 0.0
    last_account_emit = 0.0
    last_pnl_persist = 0.0
    last_reconcile = 0.0
    last_external_exits = 0.0
    last_tp_sweep = 0.0
    last_auth_retry = 0.0
    last_crypto15m_record = 0.0
    last_script = 0.0
    last_redeem_check = 0.0  # unredeemed-winnings / auto-redeem-off watch
    last_cleanup = 0.0  # 0 => a DB-maintenance pass runs on the first loop tick
    # Standing instructions the USER wrote down (terminal stops / take-profits
    # / alerts). Evaluated on the same loop as everything else so there is one
    # place that can be paused, watchdogged and audited.
    last_standing = 0.0
    last_paper = 0.0
    last_remote = 0.0
    last_mcp = 0.0
    last_forecast = 0.0
    last_autopilot = 0.0

    # Make sure markets are populated before first whale scan. This ONE sync is
    # awaited (the first scan has nothing to scan without it); the recurring
    # ones below are detached. Pages are now pipelined, so this no longer costs
    # the ~3-5s of serial fetch + inter-page sleeps it used to.
    try:
        cnt = await scanner.sync_markets(max_pages=10)
        logger.info(f"Initial market sync: {cnt} markets")
        # Stamp it: without this the first loop iteration immediately fired a
        # SECOND full sync (last_market_sync was still 0).
        last_market_sync = asyncio.get_event_loop().time()
    except Exception as e:
        logger.warning(f"initial market sync failed: {e}")

    # Note: the eager startup reconcile already ran before _start_loop
    # was called. The 30s periodic reconcile below picks up from there.

    while not (_loop_stop and _loop_stop.is_set()):
        now = asyncio.get_event_loop().time()
        _loop_heartbeat = now   # watchdog liveness: proves the loop is progressing
        cfg = STATE.cfg

        if STATE.paused:
            await asyncio.sleep(1)
            continue

        # ── self-heal auth ────────────────────────────────────────
        # A transient blip (RPC/network/region-403, or a sleep/wake that killed
        # in-flight connections) can latch auth_ok=False, and nothing else
        # re-establishes it — so the dashboard nags "saved wallet could not
        # connect" until a manual Re-test. Quietly retry in the background every
        # 15s so it recovers on its own well before the user reaches for Test.
        try:
            async with _block("auth"):
                if (
                    not STATE.auth_ok
                    and polymarket_auth.credentials_present()
                    # A key it refused won't heal in seconds, but it can once the
                    # identity check finishes, so keep asking at a slower pace.
                    and now - last_auth_retry >= (300 if polymarket_api.key_rejected() else 15)
                ):
                    last_auth_retry = now
                    if await _establish_auth(retries=1):
                        STATE.auth_ok = True
                        logger.info("auth recovered (was transiently disconnected)")
                        await emit_event("backend:authChanged", {"authOk": True})
        except Exception as e:
            logger.debug(f"auth self-heal failed: {e}")

        # ── market sync ───────────────────────────────────────────
        # Detached (see _start_market_sync): the recurring universe refresh is
        # the single longest thing this loop used to await, and no engine needs
        # its result in the same tick. The clock is only stamped when a sync
        # actually STARTS, so one that overruns its interval re-arms as soon as
        # it finishes instead of being skipped for a whole period.
        try:
            async with _block("marketSync", 120.0):
                if now - last_market_sync >= float(cfg.get("market_refresh_interval", 300)):
                    if _start_market_sync(max_pages=10):
                        last_market_sync = now
                if now - last_event_sync >= 600:
                    await scanner.sync_events()
                    last_event_sync = now
        except Exception as e:
            logger.warning(f"sync error: {e}")

        collect_main = _whale_collection_on(cfg)

        # ── trader: scan for new trades ───────────────────────────
        try:
            async with _block("tradeScan"):
                if (
                    STATE.auth_ok
                    and now - last_trade >= float(cfg.get("trade_scan_interval", 20))
                ):
                    placed = await trader.scan_for_trades(cfg)
                    last_trade = now
                    STATE.last_trade_scan_at = datetime.now(timezone.utc).isoformat()
                    for row in placed:
                        js = _position_row_to_js(row)
                        await emit_event("position:new", js)
        except Exception as e:
            logger.error(f"trade scan error: {e}", exc_info=True)

        # ── trader: poll open orders ──────────────────────────────
        try:
            async with _block("orderPoll"):
                if (
                    STATE.auth_ok
                    and now - last_poll >= float(cfg.get("position_poll_interval", 30))
                ):
                    updated = await trader.poll_open_orders(cfg)
                    last_poll = now
                    # A detected fill moves both positions AND cash. Sync them right
                    # now instead of waiting for the 30s reconcile / 60s balance
                    # ticks: force the reconcile block below to run this same tick,
                    # and refresh + push balance. This is event-driven (bounded by
                    # fill frequency), so it adds no steady-state polling load.
                    if any(r.get("status") in ("filled", "partial") for r in updated):
                        last_reconcile = 0.0  # → reconcile block fires this tick
                        try:
                            await trader.refresh_balance(cfg, force=True)
                            await emit_event("account:update", await _build_account_snapshot())
                        except Exception as e:
                            logger.debug(f"post-fill balance refresh failed: {e}")
                    for row in updated:
                        js = _position_row_to_js(row)
                        await emit_event("position:update", js)
        except Exception as e:
            logger.error(f"poll error: {e}", exc_info=True)

        # ── unredeemed-winnings watch (auto-redeem-off detector) ──────
        # Winning positions settle as ERC-1155 tokens the bot never redeems on
        # its own; if Polymarket's opt-in auto-redeem is OFF they sit unredeemed
        # and the tradable balance silently shrinks. Poll settled positions on a
        # SLOW cadence (one /positions call every few minutes, cached for the
        # snapshot); the snapshot flags "stale" only once winnings PERSIST, which
        # distinguishes auto-redeem-off from a brief pre-relayer redeemable window.
        try:
            async with _block("unredeemed"):
                if STATE.auth_ok and now - last_redeem_check >= 180.0:
                    last_redeem_check = now
                    settled = await polymarket_api.get_settled_positions()
                    total = sum(
                        abs(float(p.get("position_fp") or 0)) * float(p.get("cur_price") or 0)
                        for p in settled
                    )
                    STATE.unredeemed_winnings_usd = round(total, 2)
                    STATE.unredeemed_winnings_count = len(settled)
                    if total >= 1.0:
                        if not STATE.unredeemed_since:
                            STATE.unredeemed_since = now
                    else:
                        STATE.unredeemed_since = 0.0
        except Exception as e:
            logger.debug(f"unredeemed-winnings check failed: {e}")

        # ── trader: periodic reconcile with /portfolio/positions ──
        # This is THE source of truth for "what's open right now".
        # Cheap (one HTTP call), so we run it every 30s. It rescues
        # rows our per-order poll missed AND resurrects rows previously
        # mis-marked as `gone`, AND imports manual UI trades.
        try:
            async with _block("reconcile"):
                if STATE.auth_ok and now - last_reconcile >= 30:
                    summary, changed = await trader.reconcile_positions_with_polymarket()
                    last_reconcile = now
                    if any(summary.values()):
                        logger.info(f"reconcile: {summary}")
                        await emit_event("backend:reconciled", summary)
                    for row in changed:
                        await emit_event(
                            "position:update", _position_row_to_js(row),
                        )
        except Exception as e:
            logger.debug(f"periodic reconcile failed: {e}")

        # ── external-exit detection (position sold directly on Polymarket) ──
        # Previously this ran ONLY on the manual Refresh button: a position the
        # user sold on the website stayed "open" here for days, pinning the
        # max-open/exposure caps (silent under-trading), and at resolution was
        # booked as a full settlement win/loss as if still held. Run it on a
        # slow cadence — it's activity-evidence-gated internally, so it never
        # guesses.
        try:
            async with _block("externalExits"):
                if STATE.auth_ok and now - last_external_exits >= 600:
                    last_external_exits = now
                    ext_rows = await trader.detect_external_exits(cfg)
                    for row in ext_rows:
                        await emit_event("position:update", _position_row_to_js(row))
                    if ext_rows:
                        logger.info(f"external exits: booked {len(ext_rows)} position(s)")
        except Exception as e:
            logger.debug(f"periodic external-exit sweep failed: {e}")

        # ── per-position take-profit sweep ────────────────────────
        # Sell any open winner whose live bid is now >= take_profit_pct above
        # cost, instead of holding to settlement. Cheap and self-gating (no-op
        # when take_profit_pct <= 0), so it's safe to run on the reconcile cadence.
        try:
            async with _block("takeProfit"):
                if STATE.auth_ok and now - last_tp_sweep >= 30:
                    last_tp_sweep = now
                    tp_rows = await trader.take_profit_sweep(cfg)
                    for row in tp_rows:
                        await emit_event("position:update", _position_row_to_js(row))
                    if tp_rows:
                        logger.info(f"take-profit: cashed out {len(tp_rows)} position(s)")
        except Exception as e:
            logger.debug(f"periodic take-profit sweep failed: {e}")

        # ── trader: resolution ────────────────────────────────────
        try:
            async with _block("resolution"):
                if (
                    STATE.auth_ok
                    and now - last_resolve >= float(cfg.get("resolution_check_interval", 300))
                ):
                    resolved_pos = await trader.mark_resolved_positions(cfg)
                    await scanner.resolve_alerts_from_markets()
                    await scanner.resolve_whales_from_markets()
                    last_resolve = now
                    for row in resolved_pos:
                        js = _position_row_to_js(row)
                        await emit_event("position:update", js)
        except Exception as e:
            logger.error(f"resolution error: {e}", exc_info=True)

        # ── 15-min crypto executor + its live feeds ───────────────
        # MOVED to `_crypto15m_loop` (its own task, its own 4s cadence). It sat
        # here behind ~10 other blocks, so a 15-minute window could close while
        # the loop was still in the market sync. The research recorder below
        # stays on this loop — it is passive and 25s-cadenced.

        # ── User-script executor (Scripts tab) ────────────────────
        # Runs each enabled user script's hooks and places qualifying intents
        # under the script money rails. Positions it opens are managed to
        # settlement by the crypto15m pass above, so a disabled script never
        # strands an open bet. Script errors auto-disable the script — they can
        # never take this loop down.
        #
        # NOT gated on scripts_live_enabled here. That switch means "scripts
        # may place REAL orders", and run_tick enforces exactly that: shadow
        # scripts tick regardless (they cannot reach the exchange), armed ones
        # are parked with a reason the Scripts page displays. Gating the whole
        # engine on it meant enabling a script did visibly nothing until the
        # user found a second switch. run_tick returns immediately when no
        # script is enabled, so the idle cost is one indexed SELECT.
        try:
            async with _block("scripts"):
                if (
                    STATE.auth_ok
                    and now - last_script >= float(cfg.get("script_poll_sec", 5))
                ):
                    await script_engine.run_tick(cfg, authed=STATE.auth_ok)
                    last_script = now
        except Exception as e:
            logger.error(f"script engine tick error: {e}", exc_info=True)

        # ── trade-tape socket lifecycle ───────────────────────────
        # Stays on the master loop even though the copy ENGINE moved off it:
        # two consumers ride this one socket — copy trading (wakeups + pushed
        # entries) and whale detection (every trade on the tape, ~1s old
        # instead of up to 120s). Either one wanting it keeps it up; only both
        # off takes it down. Reconciling it here keeps that decision in ONE
        # place and off the latency-critical copy tick. The watch set stays
        # copy's alone — with copy off it is empty, so `has_hits()` in the copy
        # loop behaves exactly as it did when the socket was stopped.

        # ── Copy-trading executor (Phase 3) ───────────────────────
        # MOVED to `_copy_loop` (its own task, woken directly by the trade
        # tape). Mirroring a followed wallet's SELL used to wait behind every
        # other block in the iteration; now the push wakes the copy loop.

        # ── 15-min crypto research logger (every ~25s) ────────────
        # Passively records each quarter's decision-point signal +
        # settled outcome so the strategy is backtestable, regardless of
        # whether the executor is enabled. Read-only; on by default.
        try:
            async with _block("crypto15mRecord"):
                if (
                    cfg.get("crypto15m_record_signals", True)
                    and now - last_crypto15m_record >= 25
                ):
                    await crypto15m_record.record_tick(cfg)
                    last_crypto15m_record = now
        except Exception as e:
            logger.debug(f"crypto15m record error: {e}")

        # ── DB maintenance (prune high-volume scanner tables) ─────
        # The whale feed + market poller insert millions of rows over
        # time; left unpruned the SQLite file balloons to multiple GB
        # (slow queries, slow startup, disk pressure). Runs hourly in a
        # worker thread; the deletes are batched, so pruning is cheap.
        #
        # The old comment here claimed maintenance "never blocks the trading
        # loop". That was wrong about the VACUUM half: it takes an EXCLUSIVE
        # lock, and every loop write during it blocks on busy_timeout=10000 and
        # then RAISES — on a large file the loop cannot record a fill, a stop
        # or a settlement for the whole run. run_maintenance now gates the
        # vacuum on db.is_trading_idle() and skips it (with a reason) whenever
        # anything is open; pruning still runs on the hourly timer either way.
        try:
            async with _block("dbMaintenance", 120.0):
                if now - last_cleanup >= float(cfg.get("db_cleanup_interval", 3600)):
                    summary = await asyncio.get_event_loop().run_in_executor(
                        None, db.run_maintenance
                    )
                    if summary.get("deleted") or summary.get("vacuumed"):
                        logger.info(
                            f"db maintenance: pruned {summary['deleted']} rows, "
                            f"vacuumed={summary['vacuumed']} "
                            f"(reclaimable {summary['reclaimable_mb']}MB)"
                        )
                    last_cleanup = now
        except Exception as e:
            logger.warning(f"db maintenance failed: {e}")

        # ── account snapshot push (every ~15s) ────────────────────
        try:
            async with _block("accountSnapshot"):
                if now - last_account_emit >= 15:
                    snap = await _build_account_snapshot()
                    if _balance_persistable():
                        with db.get_db() as conn:
                            # Persist a P&L point at most once a minute — the UI
                            # gets its fresh snapshot every 15s regardless, but
                            # 15s persistence quadruples pnl_snapshots growth for
                            # chart resolution nobody can see.
                            if now - last_pnl_persist >= 60:
                                # Deposits/withdrawals must not read as daily P&L
                                # (a $50 withdrawal = a phantom daily-stop breach;
                                # flatten_on_daily_stop would market-sell the book).
                                # Detect unexplainable balance jumps BEFORE this
                                # snapshot becomes the new baseline.
                                try:
                                    moved = db.note_transfer_if_unexplainable(
                                        conn,
                                        polymarket_auth.get_env(),
                                        snap["totalUsd"],
                                        run_id=int(STATE.active_run_id or 0),
                                    )
                                    if moved:
                                        kind = "deposit" if moved > 0 else "withdrawal"
                                        logger.warning(
                                            f"[balance] {kind} of ${abs(moved):.2f} detected — "
                                            "excluded from today/session/all-time P&L "
                                            "and the daily stop"
                                        )
                                except Exception as e:
                                    logger.debug(f"transfer detection failed: {e}")
                                db.insert_pnl_snapshot(
                                    conn,
                                    cash_usd=snap["cashUsd"],
                                    portfolio_usd=snap["portfolioUsd"],
                                    realized_pnl_usd=snap["realizedPnlUsd"],
                                    wins=snap["wins"], losses=snap["losses"],
                                    open_positions=snap["openCount"] + snap["pendingCount"],
                                    env=polymarket_auth.get_env(),
                                )
                                last_pnl_persist = now
                            # Heartbeat the active run so its end_balance/
                            # pnl/trade counts stay current. We pass CURRENT
                            # lifetime counters; SQL inside the heartbeat
                            # subtracts the run's start_trades_* baseline so
                            # the run row stores per-session deltas.
                            if STATE.active_run_id:
                                db.heartbeat_bot_run(
                                    conn, STATE.active_run_id,
                                    cash_usd=snap["cashUsd"],
                                    portfolio_usd=snap["portfolioUsd"],
                                    lifetime_trades=snap["totalOpened"],
                                    lifetime_wins=snap["wins"],
                                    lifetime_losses=snap["losses"],
                                )
                    await emit_event("account:update", snap)
                    last_account_emit = now
        except Exception as e:
            logger.debug(f"account snapshot error: {e}")

        # ── terminal standing instructions (~10s) ────────────────
        # A stop loss is an instruction the user wrote down, so it executes
        # without a click — but it passes the SAME preview and the same
        # live-trading gate a click would, and every pending one is listed and
        # cancellable on the terminal's Act page. Nothing here originates a
        # trade from a score or a signal.
        try:
            async with _block("standing"):
                if now - last_standing >= 10:
                    last_standing = now
                    import terminal_trade
                    # Standing instructions are the user's own manual
                    # orders, written down in advance; they fire regardless
                    # of the automation switch. Paper mode still blocks them.
                    # account_live, not terminal_paper_mode: `cfg` is the raw
                    # default until the first setConfig, and only its
                    # account_mode says Paper.
                    from config import account_live
                    res = await terminal_trade.standing_tick(
                        allow_trading=True, paper=not account_live(cfg))
                    for a in res.get("alerts") or []:
                        await emit_event("terminal:standing", a)
                        # And to the phone. This is the one thing worth pushing
                        # unprompted: a stop that fired, or worse, one that was
                        # BLOCKED from firing. Routine chatter would get the bot
                        # muted, and then it is worth nothing when it matters.
                        try:
                            import remote
                            await remote.push(
                                ("BLOCKED: " if a.get("blocked") else "FIRED: ")
                                + f"{a.get('kind')} on "
                                + f"{a.get('title') or a.get('conditionId')} at "
                                + f"{a.get('priceCents')}c "
                                + f"(trigger {a.get('triggerCents')}c). "
                                + (a.get("result") or ""))
                        except Exception:
                            pass
                        if a.get("blocked"):
                            logger.warning(
                                "Terminal stop for %s hit %.1fc but live trading is "
                                "off - nothing was sold.",
                                (a.get("title") or a.get("conditionId") or "?"),
                                float(a.get("priceCents") or 0.0))
        except Exception as e:
            logger.debug(f"standing-instruction tick error: {e}")

        # ── remote bots (~20s) ───────────────────────────────────
        # Keeps each transport matching config and re-attaches one that died.
        # Idempotent, so it doubles as the reconnect supervisor.
        try:
            async with _block("remote"):
                if now - last_remote >= 20:
                    last_remote = now
                    await _remote_sync()
        except Exception as e:
            logger.debug(f"remote sync error: {e}")

        # ── AI agent MCP server (~10s) ───────────────────────────
        # Retries a port that failed to bind; idempotent otherwise.
        try:
            async with _block("mcp"):
                if now - last_mcp >= 10:
                    last_mcp = now
                    await _mcp_sync()
        except Exception as e:
            logger.debug(f"mcp sync error: {e}")

        # ── paper book (~15s) ────────────────────────────────────
        # Fills resting paper orders once the market trades through them and
        # settles paper positions in markets that resolved. Runs regardless of
        # enable_trading — nothing here touches the exchange or any money.
        try:
            async with _block("paperBook"):
                if now - last_paper >= 15:
                    last_paper = now
                    import terminal_paper
                    await terminal_paper.tick()
                    # The AI agents' own paper stops / take-profits.
                    import mcp_tools
                    await mcp_tools.paper_standing_tick()
        except Exception as e:
            logger.debug(f"paper tick error: {e}")

        # ── agent forecasts (~5 min) ─────────────────────────────
        # Scores the fair values agents committed to once their markets
        # resolve. Public reads only, cached per market once resolved.
        try:
            async with _block("forecasts"):
                if now - last_forecast >= 300:
                    last_forecast = now
                    import forecast_ledger
                    await forecast_ledger.resolve_pending()
        except Exception as e:
            logger.debug(f"forecast resolve error: {e}")

        # ── Autopilot due-check (~30s) ───────────────────────────
        # Started as a background task, never awaited here: a run is minutes
        # of model round trips and must not hold up stops, rules or the
        # engines. The check itself is two small DB reads (no network).
        try:
            async with _block("autopilot"):
                if now - last_autopilot >= 30:
                    last_autopilot = now
                    acfg = merge_with_defaults(dict(STATE.cfg or {}))
                    if acfg.get("autopilot_enabled"):
                        import autopilot
                        if autopilot.due(acfg) and autopilot.blocked_reason(acfg) is None:
                            t = asyncio.create_task(autopilot.run_once(acfg, "schedule"))
                            _bg_tasks.add(t)
                            t.add_done_callback(_bg_tasks.discard)
        except Exception as e:
            logger.debug(f"autopilot schedule error: {e}")

        await asyncio.sleep(1)


# --------------------------------------------------------------------
# Latency-critical engines, each on its own task
# --------------------------------------------------------------------
#
# Everything below mirrors the master loop's contract exactly:
#   - re-reads STATE.cfg EVERY tick, so enabling/disabling an engine (or
#     changing its cadence) takes effect on the next tick with no restart,
#   - honours STATE.paused,
#   - wraps each unit of work in `_block(...)` so it gets the same timing, the
#     same slow-block warning and the same ceiling (a wedged await is a skipped
#     block, never a dead engine), reported under `loopBlocksMs` namespaced by
#     loop ("copy:tick", "crypto15m:tick", ...),
#   - stamps its OWN heartbeat so `_loop_watchdog` restarts THIS loop (and only
#     this loop) when it stops progressing,
#   - catches its own crashes and restarts itself with exponential backoff -
#     belt and braces with the watchdog, which also re-spawns a dead task.


async def _engine_supervisor(name: str, body, heartbeat_attr: str) -> None:
    """Run `body()` forever, restarting it on any crash with exponential
    backoff. A trading engine that dies silently is the failure mode this file
    is written against, so a crash is CRITICAL-logged; the heartbeat is
    re-stamped across the backoff sleep so the watchdog doesn't also fire while
    we are deliberately waiting."""
    delay = 1.0
    while not (_loop_stop and _loop_stop.is_set()):
        started = asyncio.get_event_loop().time()
        try:
            await body()
            return                      # clean exit: stop was requested
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if asyncio.get_event_loop().time() - started >= 60.0:
                delay = 1.0             # it ran fine for a while: fresh ladder
            logger.critical(
                f"[{name}] engine loop crashed ({e!r}) - restarting in "
                f"{delay:.0f}s", exc_info=True,
            )
            deadline = asyncio.get_event_loop().time() + delay
            while asyncio.get_event_loop().time() < deadline:
                if _loop_stop and _loop_stop.is_set():
                    return
                globals()[heartbeat_attr] = asyncio.get_event_loop().time()
                await asyncio.sleep(min(1.0, delay))
            delay = min(_LOOP_RESTART_BACKOFF_MAX, delay * 2)


async def _crypto15m_loop() -> None:
    """The 15-minute crypto executor on its own 4s cadence.

    Places REAL orders (no paper mode). Gated on auth, NOT on
    crypto15m_enabled: run_tick cheaply no-ops when the engine is off AND
    nothing is open, but still MANAGES open positions to settlement after the
    engine is turned off - a manual toggle or the overall take-profit
    kill-switch must never strand an open bet. New entries are opened only when
    crypto15m_enabled is on. Independent of the main bot's enable_trading."""
    await _engine_supervisor("crypto15m", _crypto15m_loop_body, "_crypto_heartbeat")


async def _crypto15m_loop_body() -> None:
    global _crypto_heartbeat
    _crypto_heartbeat = asyncio.get_event_loop().time()
    last_tick = 0.0
    while not (_loop_stop and _loop_stop.is_set()):
        now = asyncio.get_event_loop().time()
        _crypto_heartbeat = now
        cfg = STATE.cfg      # re-read every tick: no restart to change config

        if STATE.paused:
            await asyncio.sleep(1)
            continue

        # Live spot feed lifecycle (settlement model input). Reconcile the live
        # feeds against their toggles each pass - cheap no-ops when already in
        # the desired state. Kept up whenever enabled (not only when the 15m
        # engine trades): the recorder and Crypto tab read modelProb from the
        # same feeds: Coinbase WS for live spot, the settlement index for the
        # number a window actually settles on.
        try:
            async with _block("crypto15m:feeds"):
                try:
                    if bool(cfg.get("crypto15m_spot_ws", True)):
                        if not spot_ws.is_running():
                            spot_ws.start()
                    elif spot_ws.is_running():
                        await spot_ws.stop()
                except Exception as e:
                    logger.debug(f"spot_ws lifecycle: {e}")
                if not index_feed.is_running():
                    index_feed.start()
        except Exception as e:
            logger.debug(f"index feed lifecycle: {e}")

        if not STATE.auth_ok:
            # No wallet: nothing to place and nothing to manage. Idle cheaply
            # and pick up the moment auth self-heals on the master loop.
            await asyncio.sleep(1)
            continue

        # Our own order/fill events, pushed: a match or cancel on one of our
        # orders runs the next tick NOW instead of up to a poll later. The
        # tick's REST reads stay the authority on what actually filled.
        if user_ws.feed.wake is None:
            user_ws.feed.wake = asyncio.Event()
        if not user_ws.feed.is_running():
            user_ws.feed.start()
        woke = user_ws.feed.wake.is_set()
        if woke:
            user_ws.feed.wake.clear()

        try:
            async with _block("crypto15m:tick"):
                poll = float(cfg.get("crypto15m_poll_sec", 4))
                if now - last_tick >= poll or (woke and now - last_tick >= 0.5):
                    changed = await crypto15m_trader.run_tick(cfg, authed=STATE.auth_ok)
                    last_tick = now
                    if changed:
                        # A fill/exit/settlement just moved money - refresh the
                        # balance cache NOW instead of waiting out the poll, so
                        # the displayed total doesn't dip by one position while
                        # the ledgers catch up. Fire-and-forget: an inline await
                        # would stall this loop through the retry ladder during
                        # an API storm - exactly when open positions need their
                        # management cadence.
                        _fire_and_forget(trader.refresh_balance(cfg, force=True))
        except Exception as e:
            logger.error(f"crypto15m tick error: {e}", exc_info=True)

        await asyncio.sleep(1)


async def _start_loop() -> None:
    """Start every engine loop (master + crypto15m) and the
    watchdog that supervises them. `_loop_stop` is shared: one Event stops
    every loop, which is what factory-reset / clear-history / shutdown want."""
    global _loop_task, _loop_stop, _watchdog_task
    global _crypto_task
    if _loop_task and not _loop_task.done():
        return
    _loop_stop = asyncio.Event()
    _loop_task = asyncio.create_task(_scanner_and_trader_loop())
    _crypto_task = asyncio.create_task(_crypto15m_loop())
    _loop_restart_state.clear()
    if not _watchdog_task or _watchdog_task.done():
        _watchdog_task = asyncio.create_task(_loop_watchdog())


async def _stop_loop() -> None:
    """Stop the watchdog and every engine loop. Each is given the shared stop
    Event first (so it exits its own `while` cleanly), then a bounded wait,
    then a hard cancel — a task still pending at process exit is what produces
    'Task was destroyed but it is pending!' on stderr."""
    global _loop_task, _loop_stop, _watchdog_task, _crypto_task
    if _loop_stop:
        _loop_stop.set()
    if _watchdog_task:
        _watchdog_task.cancel()
        try:
            await asyncio.wait_for(_watchdog_task, timeout=5)
        except BaseException:
            pass
        _watchdog_task = None
    for _attr in ("_loop_task", "_crypto_task"):
        task = globals().get(_attr)
        if not task:
            continue
        try:
            await asyncio.wait_for(task, timeout=5)
        except BaseException:
            # Timed out, crashed, or was already cancelled — cancel it and
            # reap it so nothing is left pending.
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=2)
            except BaseException:
                pass
        globals()[_attr] = None


async def _restart_engine_loop(label: str, task_attr: str, factory_attr: str,
                               *, stalled: float | None = None) -> None:
    """Cancel (if needed) and re-spawn ONE engine loop.

    A restart is SAFE here: DB writes are synchronous, so a cancel can only land
    on a network await (never mid-write), and any order that reached the exchange
    is re-imported by the wallet-authoritative reconcile when the loop comes back
    up. A false restart (e.g. a legitimately slow iteration during a network
    outage) is harmless - it just re-enters the same idempotent loop."""
    task = globals().get(task_attr)
    if task is not None and not task.done():
        try:
            await emit_event(
                "backend:loopStalled",
                {"loop": label, "stalledSec": round(stalled or 0.0)},
            )
        except Exception:
            pass
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10)
        except BaseException:
            # Awaiting a cancelled task re-raises CancelledError (not an
            # Exception subclass) - swallow it (and any error) so the watchdog
            # PROCEEDS to restart instead of dying on the cancellation it issued.
            pass
    if _loop_stop and _loop_stop.is_set():
        return
    # A stall is almost always the network layer wedging (a transient blip /
    # sleep-wake leaves the shared HTTP pool handing out dead sockets so every
    # host fails at once until the PROCESS is restarted). Recycle the clients so
    # the restarted loop comes up on FRESH connection pools - this is what the
    # user's manual app-restart did. polymarket_api also self-heals via its
    # cross-endpoint failure streak; this is the belt-and-braces backstop and
    # also clears crypto15m's separate pool.
    for _name, _mod in (("polymarket_api", polymarket_api), ("crypto15m", crypto15m)):
        try:
            await _mod.close_clients()
        except Exception as e:
            logger.debug(f"watchdog client recycle ({_name}) failed: {e}")
    globals()[task_attr] = asyncio.create_task(globals()[factory_attr]())
    logger.info(f"{label} loop restarted by watchdog (HTTP clients recycled)")


def _restart_allowed(label: str, now: float) -> float:
    """0.0 if `label` may be restarted right now, else the seconds still to
    wait. Exponential backoff so a loop that dies on every single start cannot
    spin the watchdog restarting it forever; the ladder resets once a loop has
    been up for a minute."""
    st = _loop_restart_state.get(label)
    if st is None:
        return 0.0
    if now < float(st.get("not_before", 0.0)):
        return float(st["not_before"]) - now
    return 0.0


_ALERTS_BOOT = time.monotonic()


def _note_restart(label: str, now: float) -> None:
    st = _loop_restart_state.get(label) or {"n": 0, "last": 0.0}
    # A restart more than a minute after the last one means the loop was
    # healthy in between - start the ladder over.
    st["n"] = (int(st.get("n", 0)) + 1) if (now - float(st.get("last", 0.0)) < 60.0) else 1
    st["last"] = now
    delay = 0.0 if st["n"] <= 1 else min(
        _LOOP_RESTART_BACKOFF_MAX, 2.0 ** (st["n"] - 1)
    )
    st["not_before"] = now + delay
    _loop_restart_state[label] = st
    if delay:
        logger.warning(
            f"{label} loop has restarted {st['n']}x in a minute - backing off "
            f"{delay:.0f}s before the next restart"
        )


async def _loop_watchdog() -> None:
    """Force-restart any engine loop whose heartbeat goes stale (or whose task
    ended), so open real-money positions never silently stop being managed if
    some await hangs.

    Watches every loop - master, crypto15m - independently: a
    stalled copy loop restarts the copy loop only, leaving the master loop's
    reconcile/settlement passes and the crypto engine untouched."""
    while not (_loop_stop and _loop_stop.is_set()):
        try:
            await asyncio.sleep(_WATCHDOG_CHECK_SEC)
        except asyncio.CancelledError:
            break
        if _loop_stop and _loop_stop.is_set():
            break
        for label, task_attr, hb_attr, factory_attr, stall_attr in _ENGINE_LOOPS:
            if _loop_stop and _loop_stop.is_set():
                break
            task = globals().get(task_attr)
            if not task:
                continue
            now = asyncio.get_event_loop().time()
            # Two failure modes BOTH demand a restart, so open real-money
            # positions never silently stop being managed:
            #   (A) the loop TASK ENDED (task.done()) - an unhandled
            #       BaseException (MemoryError, a bug outside the per-block
            #       try/except, a stray cancellation) ran the coroutine to
            #       completion, leaving that engine dead forever.
            #   (B) the loop is ALIVE but its heartbeat went stale - an await
            #       wedged (usually a poisoned HTTP pool).
            stalled: float | None = None
            if task.done():
                try:
                    _exc = task.exception()
                except BaseException:
                    _exc = None
                reason = f"{label} loop task ended unexpectedly ({_exc!r})"
            else:
                hb = float(globals().get(hb_attr) or 0.0)
                if hb <= 0:
                    continue
                stalled = now - hb
                if stalled < float(globals()[stall_attr]):
                    continue
                reason = f"{label} loop stalled {stalled:.0f}s with no heartbeat"
            wait = _restart_allowed(label, now)
            if wait > 0:
                logger.warning(f"{reason} - restart held off {wait:.0f}s (backoff)")
                continue
            logger.critical(f"{reason} - restarting it")
            _note_restart(label, now)
            try:
                await _restart_engine_loop(
                    label, task_attr, factory_attr, stalled=stalled,
                )
            except Exception as e:
                logger.error(f"{label} loop restart failed: {e}", exc_info=True)


# ────────────────────────────────────────────────────────────────────
# RPC handlers
# ────────────────────────────────────────────────────────────────────


async def _h_ping(_p: dict) -> dict:
    return {"pong": True, "ts": datetime.now(timezone.utc).isoformat()}


async def _h_setConfig(p: dict) -> dict:
    cfg = merge_with_defaults(p.get("config") or {})
    STATE.cfg = cfg
    if p.get("pruneAgentTokens"):
        # Only when the app saved an agent list on purpose: a backend that
        # started on defaults must never delete a real agent's token.
        import mcp_server
        gone = mcp_server.prune_tokens(cfg)
        if gone:
            logger.info(f"mcp: removed {len(gone)} deleted agent token(s)")
    # Fee estimates everywhere net out the user's taker-rebate tier.
    import backtest as _bt
    _bt.set_taker_rebate(cfg.get("taker_rebate_pct"))
    crypto15m.set_taker_rebate(cfg.get("taker_rebate_pct"))
    # Surface the master flags so the user can confirm the renderer's
    # toggle state actually made it into the backend. Without this log
    # there was no way to tell "config never arrived" apart from "it
    # arrived but enable_trading=false in the payload".
    logger.info(
        f"setConfig applied: enable_trading={cfg.get('enable_trading')} "
        f"crypto15m={cfg.get('crypto15m_enabled')} "
        f"trade_whales={cfg.get('trade_whales')} "
        f"trade_momentum={cfg.get('trade_momentum')} "
        f"max_open={cfg.get('max_open_positions')} "
        f"max_daily="
        f"{'∞' if cfg.get('unlimited_daily_new_positions') else cfg.get('max_daily_new_positions')} "
        f"stop_loss={cfg.get('stop_loss_on_day')} "
        f"env={cfg.get('network')}"
    )
    new_env = cfg.get("network", "mainnet")
    prev_env = polymarket_auth.get_env()
    polymarket_auth.set_env(new_env)

    # When the active env flips, re-load the matching cred file and
    # re-verify auth so the rest of the bot doesn't keep using stale
    # cached keys for the previous env.
    if new_env != prev_env:
        polymarket_auth.reset_credential_cache()
        if polymarket_auth.credentials_present(new_env):
            STATE.auth_ok = await _establish_auth()
        else:
            STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})

        # Close out the previous env's run and open a new one for the
        # incoming env so session P&L benchmarks against the right
        # starting balance.
        try:
            if STATE.active_run_id:
                with db.get_db() as conn:
                    db.end_bot_run(conn, STATE.active_run_id)
                STATE.active_run_id = 0
            if STATE.auth_ok:
                cents, port = await trader.refresh_balance(STATE.cfg, force=True)
                with db.get_db() as conn:
                    stats = db.aggregate_stats(conn, new_env)
                    STATE.active_run_id = db.start_bot_run(
                        conn, env=new_env,
                        cash_usd=cents / 100.0,
                        portfolio_usd=port / 100.0,
                        lifetime_trades=int(stats.get("total_opened") or 0),
                        lifetime_wins=int(stats.get("wins") or 0),
                        lifetime_losses=int(stats.get("losses") or 0),
                    )
                logger.info(
                    f"Bot run #{STATE.active_run_id} started after env switch "
                    f"(env={new_env})"
                )
        except Exception as e:
            logger.warning(f"could not roll bot_run on env switch: {e}")
    try:
        await _mcp_sync()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mcp sync failed: {e}")
    return {"ok": True}


async def _h_setCredentials(p: dict) -> dict:
    """Save a Polymarket US API key: the key id and its Ed25519 secret, both from
    the developer portal. No order is placed and nothing leaves the machine here;
    the check against the exchange is _establish_auth below."""
    p = p or {}
    key_id = str(p.get("keyId") or "").strip()
    secret_key = str(p.get("secretKey") or "").strip()
    env = p.get("env")  # None → current network
    polymarket_auth.save_credentials(key_id, secret_key, env)
    polymarket_auth.reset_credential_cache()
    polymarket_api.load_venue_state()

    # A new key may be a DIFFERENT account. Reset session state so the old account
    # can't bleed into the new one: a stale balance cache would size the new
    # account's trades against the OLD balance, and a bot_run baselined on the old
    # account's starting total would show a fabricated session P&L (and could trip
    # the daily stop). Mirror the env-switch path in _h_setConfig.
    active_env = env or polymarket_auth.get_env()
    active_env = env or polymarket_auth.get_env()
    try:
        trader._balance_cache.pop(active_env, None)  # type: ignore[attr-defined]
    except Exception:
        pass
    if active_env in (None, polymarket_auth.get_env()):
        try:
            STATE.auth_ok = (
                await _establish_auth()
                if polymarket_auth.credentials_present(active_env) else False
            )
            await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})
        except Exception as e:
            logger.warning(f"auth re-verify after account change failed: {e}")
        try:
            if STATE.active_run_id:
                with db.get_db() as conn:
                    db.end_bot_run(conn, STATE.active_run_id)
                STATE.active_run_id = 0
            if STATE.auth_ok:
                cents, port = await trader.refresh_balance(STATE.cfg, force=True)
                env_now = polymarket_auth.get_env()
                with db.get_db() as conn:
                    stats = db.aggregate_stats(conn, env_now)
                    STATE.active_run_id = db.start_bot_run(
                        conn, env=env_now,
                        cash_usd=cents / 100.0, portfolio_usd=port / 100.0,
                        lifetime_trades=int(stats.get("total_opened") or 0),
                        lifetime_wins=int(stats.get("wins") or 0),
                        lifetime_losses=int(stats.get("losses") or 0),
                    )
                logger.info(
                    f"Bot run #{STATE.active_run_id} started after account change "
                    f"(env={env_now})"
                )
        except Exception as e:
            logger.warning(f"could not roll bot_run on account change: {e}")

    status = polymarket_auth.credentials_status_all()
    await emit_event("credentials:changed", status)
    return status


async def _h_clearCredentials(p: dict) -> dict:
    p = p or {}
    env = p.get("env")
    polymarket_auth.clear_credentials(env)
    if env in (None, polymarket_auth.get_env()):
        STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": False})
    status = polymarket_auth.credentials_status_all()
    await emit_event("credentials:changed", status)
    return status


async def _h_credentialStatus(_p: dict) -> dict:
    return polymarket_auth.credentials_status_all()




async def _establish_auth(retries: int = 3) -> bool:
    """Verify the saved API key by reading the account with it, with retries so a
    single transient blip can't latch auth as 'disconnected'. A key the exchange
    refuses (401/403) is a definite answer and isn't retried."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            polymarket_auth.prime_credentials(sync_time=True)
            await polymarket_api.ensure_api_creds()
            return True
        except polymarket_api.PolymarketAPIError as e:
            last_exc = e
            logger.warning(f"auth verify attempt {attempt}/{retries} failed: {e}")
            if e.status in (401, 403):
                break
        except Exception as e:
            last_exc = e
            logger.warning(f"auth verify attempt {attempt}/{retries} failed: {e}")
        if attempt < retries:
            await asyncio.sleep(1.5 * attempt)
    logger.warning(f"auth verify failed after {retries} attempts: {last_exc}")
    return False


async def _h_testCredentials(p: dict) -> dict:
    """Verify the saved API key against the exchange and read the balance."""
    p = p or {}
    target_env = polymarket_auth.get_env()
    if not polymarket_auth.credentials_present(target_env):
        raise RuntimeError("API key not set")

    polymarket_auth.reset_credential_cache()
    polymarket_auth.prime_credentials(sync_time=True)
    # The trading-readiness preflight: a signed balance read.
    ready = await polymarket_api.check_trading_ready()
    signer_ok = ready.get("signerOk")
    why = (ready.get("issues") or ["could not connect to Polymarket US"])[0]
    if not ready.get("address") or signer_ok is False:
        # A definite no. Only this may flip the state to disconnected.
        STATE.auth_ok = False
        await emit_event("backend:authChanged", {"authOk": False})
        logger.warning(f"trading-readiness: {why}")
        raise RuntimeError(why)
    if signer_ok is not True:
        # No signed request got an answer: the key is untested, so this neither
        # proves it nor condemns it, and whatever was known before stands.
        logger.warning(f"trading-readiness: this API key is untested: {why}")
        raise RuntimeError(why)
    STATE.auth_ok = True
    await emit_event("backend:authChanged", {"authOk": True})
    for msg in ready.get("issues", []):
        logger.warning(f"trading-readiness: {msg}")
    # A signed read is NOT an accepted order: only `npm run py:orders` tests that.
    logger.info(
        "trading-readiness: Polymarket US accepted a request signed with this "
        "API key. Order placement is a separate check (npm run py:orders)."
    )
    if not ready.get("issues"):
        logger.info(
            "trading-readiness: API key connected; balance looks OK"
        )
    return {
        "env": target_env,
        "balanceUsd": ready.get("balanceUsd", 0.0),
        "ready": ready.get("ok", False),
        "approvalsOk": ready.get("approvalsOk"),
        "signerOk": ready.get("signerOk"),
        "issues": ready.get("issues", []),
        # The subset under which no order can work (Go live blocks on these).
        "fatalIssues": ready.get("fatalIssues", []),
        # Polymarket's region verdict for this connection, every wallet type.
        "region": ready.get("region"),
        "address": ready.get("address", ""),
    }


async def _h_account(_p: dict) -> dict:
    return await _build_account_snapshot()


async def _h_accountRefresh(_p: dict) -> dict:
    """The wallet's Refresh: read the exchange now instead of waiting out the balance
    interval. A failed read keeps the last numbers and the snapshot's wallet.readOk
    says so. Without a key there is nothing to read."""
    if polymarket_auth.credentials_present():
        await trader.refresh_balance(STATE.cfg, force=True)
    return await _build_account_snapshot()


async def _h_pnlSeries(p: dict) -> list:
    hours = int((p or {}).get("sinceHours", 168))
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.get_pnl_snapshots(conn, since_hours=hours, env=env)
    return [
        {
            "at": _iso_utc(r["at"]),
            "cashUsd": float(r["cash_usd"] or 0),
            "portfolioUsd": float(r["portfolio_usd"] or 0),
            "totalUsd": float(r["total_usd"] or 0),
            "realizedPnlUsd": float(r["realized_pnl_usd"] or 0),
            "openPositions": int(r["open_positions"] or 0),
        }
        for r in rows
    ]


async def _h_positions(p: dict) -> list:
    f = p or {}
    status = f.get("status")
    resolved = f.get("resolved")
    src = f.get("signalSource")
    limit = int(f.get("limit") or 500)

    sql = "SELECT * FROM bot_positions WHERE 1=1"
    args: list = []
    if status:
        placeholders = ",".join("?" for _ in status)
        sql += f" AND status IN ({placeholders})"
        args.extend(status)
    if resolved is not None:
        sql += " AND resolved = ?"
        args.append(1 if resolved else 0)
    if src:
        sql += " AND signal_source = ?"
        args.append(src)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)

    with db.get_db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_position_row_to_js(dict(r)) for r in rows]


async def _h_signals(p: dict) -> list:
    f = p or {}
    src = f.get("source")
    min_conf = float(f.get("minConfidence") or 0)
    limit = int(f.get("limit") or 200)

    out: list[dict] = []
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        if src in (None, "whale"):
            rows = conn.execute(
                """SELECT * FROM whale_trades
                   WHERE confidence >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (min_conf, limit),
            ).fetchall()
            seen = db.already_traded_signal_ids(conn, "whale", env)
            for r in rows:
                d = dict(r)
                out.append(_signal_row_to_js(d, "whale", int(d["id"]) in seen))
        if src in (None, "momentum"):
            rows = conn.execute(
                """SELECT * FROM alerts
                   WHERE confidence >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (min_conf, limit),
            ).fetchall()
            seen = db.already_traded_signal_ids(conn, "momentum", env)
            for r in rows:
                d = dict(r)
                out.append(_signal_row_to_js(d, "momentum", int(d["id"]) in seen))
    out.sort(key=lambda s: s["createdAt"], reverse=True)
    return out[:limit]


async def _h_scannerStats(_p: dict) -> dict:
    with db.get_db() as conn:
        markets = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE status IN ('active','open')"
        ).fetchone()[0]
        wt = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN discord_sent=1 THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS wins
               FROM whale_trades"""
        ).fetchone()
        al = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE WHEN discord_sent=1 THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) AS resolved,
                      SUM(CASE WHEN outcome_correct=1 THEN 1 ELSE 0 END) AS wins
               FROM alerts"""
        ).fetchone()

    def wr(d) -> dict:
        total = int(d["total"] or 0)
        resolved = int(d["resolved"] or 0)
        wins = int(d["wins"] or 0)
        return {
            "total": total,
            "sent": int(d["sent"] or 0),
            "resolved": resolved,
            "winRate": (wins / resolved * 100.0) if resolved else 0.0,
        }

    return {
        "whales": wr(wt),
        "momentum": wr(al),
        "marketsTracked": int(markets or 0),
        "lastWhaleScanAt": STATE.last_whale_scan_at,
        "lastMomentumScanAt": STATE.last_momentum_scan_at,
        "lastTradeScanAt": STATE.last_trade_scan_at,
    }


async def _h_cancelAllOpen(_p: dict) -> dict:
    if not STATE.auth_ok:
        raise RuntimeError("not authenticated")
    n = await trader.cancel_all_open()
    return {"canceled": n}


async def _h_killSwitch(p: dict) -> dict:
    """The kill switch the exchange's rulebook requires of automated systems
    (rule 5.2(i)): refuse every new order, pause the engines and the AI agents, and
    pull every resting order on the account. It holds across a restart until
    cleared. It sells nothing: open positions stay open."""
    reason = str((p or {}).get("reason") or "kill switch pressed")
    polymarket_api.engage_kill_switch(reason)
    STATE.paused = True
    out: dict = {"engaged": True, "reason": reason, "canceled": 0}
    try:
        import remote
        await remote.stop_agents()
        out["agentsStopped"] = True
    except Exception as e:  # noqa: BLE001
        logger.warning("kill switch: could not stop the AI agents: %s", e)
        out["agentsStopped"] = False
    if polymarket_auth.credentials_present():
        try:
            out["canceled"] = len(await polymarket_api.cancel_all_venue_orders())
        except Exception as e:  # noqa: BLE001
            out["cancelError"] = f"{type(e).__name__}: {e}"
            logger.error(f"kill switch: cancel-all failed: {e}")
    await emit_event("backend:killSwitch", {"engaged": True, "reason": reason})
    return out


async def _h_killSwitchClear(_p: dict) -> dict:
    """Allow new orders again. Engines stay paused until the user resumes them."""
    polymarket_api.clear_kill_switch()
    await emit_event("backend:killSwitch", {"engaged": False, "reason": None})
    return {"engaged": False}


def _stopped_scripts() -> list[dict]:
    """Scripts the app turned off this run and the user hasn't turned back on."""
    import script_engine
    if not script_engine.stopped_this_run:
        return []
    with db.get_db() as conn:
        rows = {str(r["id"]): r for r in db.list_user_scripts(conn)}
    out = []
    for sid, reason in list(script_engine.stopped_this_run.items()):
        r = rows.get(sid)
        if r is None or r.get("enabled"):
            script_engine.stopped_this_run.pop(sid, None)
            continue
        out.append({"id": sid, "name": r.get("name"), "reason": reason})
    return out


def _phone_paused(cfg: dict) -> list[str]:
    """What /pause turned off, unless it's been turned back on some other way."""
    import remote
    was = remote._kv_get("paused_snapshot", None)
    if not isinstance(was, dict):
        return []
    names = {"enable_trading": "Trading", "crypto15m_enabled": "Crypto engine",
             "scripts_live_enabled": "Live scripts"}
    if any(cfg.get(k) for k in names):
        return []
    return [names[k] for k in names if was.get(k)]


async def _h_alerts(_p: dict) -> dict:
    """What's wrong right now (alerts.py), polled by the corner stack. Local state
    plus at most one unsigned public read: no signed request and no AI provider call,
    so polling is neither account traffic nor billed. One failed source must not hide
    the other alerts, so each is read on its own."""
    import alerts
    import mcp_server
    from config import account_live, engine_on
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    live = account_live(cfg)
    offline = bool(cfg.get("terminal_offline_mode"))
    armed = any(engine_on(cfg, k) for k in ("enable_trading", "crypto15m_enabled",
                                            "scripts_live_enabled"))
    if not offline:
        polymarket_api.probe_api_if_idle()

    def _safe(label: str, fn, default):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"alerts: {label} unavailable: {e}")
            return default

    def _armed_block(fn) -> str | None:
        if not armed:
            return None
        blocked, why = fn()
        return (why or "limit was reached") if blocked else None

    def _lock() -> str | None:
        if not (armed and live):
            return None
        free, why = trader.can_open_new_entries(trader.get_env())
        return None if free else (why or "Another copy of the app is trading this account.")

    env = trader.get_env()
    parts = await asyncio.gather(_health_ai(False, cfg), _health_agents(False, cfg),
                                 asyncio.to_thread(_health_local, cfg), return_exceptions=True)
    rows = [r for part in parts if isinstance(part, list) for r in part]
    now = time.monotonic()
    snapshot = {
        "uptimeSec": now - _ALERTS_BOOT,
        "offline": offline,
        "outages": polymarket_api.api_outages(),
        "regionRefused": polymarket_api.geoblock_active(),
        "hasKey": polymarket_auth.credentials_present(),
        "authOk": bool(STATE.auth_ok),
        "keyRejected": polymarket_api.key_rejected(),
        "accountLive": live,
        "killReason": polymarket_api.kill_switch_status(),
        "paused": bool(STATE.paused),
        "phonePaused": _safe("phone pause", lambda: _phone_paused(cfg), []),
        "restriction": polymarket_api.trading_restriction(),
        "dailyRisk": _safe("daily limit", lambda: _armed_block(
            lambda: trader._is_blocked_by_daily_risk(cfg, env)), None),
        "accountLock": _safe("account lock", _lock, None),
        "balanceStale": live and bool(STATE.auth_ok)
        and _safe("balance", trader.portfolio_stale, False),
        "healthRows": rows,
        "endedClients": mcp_server.ended_clients() if cfg.get("mcp_enabled") else [],
        "stoppedScripts": _safe("scripts", _stopped_scripts, []),
        "crashingLoops": [label for label, st in _loop_restart_state.items()
                          if int(st.get("n", 0)) >= 3 and now - float(st.get("last", 0.0)) < 120.0],
    }
    return {"alerts": alerts.build(snapshot)}


async def _h_killSwitchStatus(_p: dict) -> dict:
    reason = polymarket_api.kill_switch_status()
    return {"engaged": bool(reason), "reason": reason}


async def _h_flatten(_p: dict) -> dict:
    if not STATE.auth_ok:
        raise RuntimeError("not authenticated")
    r = await trader.flatten_open_positions(STATE.cfg)
    # Resolved positions changed in the DB — nudge the renderer to refetch.
    try:
        await emit_event("backend:reconciled", {"flattened": r["sold"]})
    except Exception:
        pass
    return {"closed": r["canceled"] + r["sold"], **r}


async def _h_runOnce(p: dict) -> dict:
    action = (p or {}).get("action")
    if action == "syncMarkets":
        cnt = await scanner.sync_markets(max_pages=10)
        return {"summary": f"Synced {cnt} markets"}
    if action == "pollOrders":
        upd = await trader.poll_open_orders(STATE.cfg)
        return {"summary": f"Polled, {len(upd)} updates"}
    if action == "resolveAll":
        rp = await trader.mark_resolved_positions(STATE.cfg)
        ra = await scanner.resolve_alerts_from_markets()
        rw = await scanner.resolve_whales_from_markets()
        return {"summary": f"Positions:{len(rp)} alerts:{ra} whales:{rw}"}
    if action == "reconcilePositions":
        s, changed = await trader.reconcile_positions_with_polymarket()
        for row in changed:
            await emit_event("position:update", _position_row_to_js(row))
        return {
            "summary": (
                f"Reconciled — rescued {s.get('rescued', 0)}, "
                f"resurrected {s.get('resurrected', 0)}, "
                f"imported {s.get('imported_unknowns', 0)}"
            ),
        }
    if action == "syncPositions":
        # User-triggered "Refresh" on the Positions tab: poll working orders
        # (pending → filled), reconcile against the live Polymarket wallet
        # (settle / rescue / import), AND detect positions exited DIRECTLY on
        # Polymarket (sold/redeemed off-app) so they stop showing as open. One
        # click brings OPEN + PENDING rows in line with the chain instead of
        # waiting for the 30s periodic loop.
        polled = await trader.poll_open_orders(STATE.cfg)
        s, changed = await trader.reconcile_positions_with_polymarket()
        exited = await trader.detect_external_exits(STATE.cfg)
        emitted: set = set()
        for row in [*polled, *changed, *exited]:
            try:
                js = _position_row_to_js(row)
            except Exception:
                continue
            if js.get("id") in emitted:
                continue
            emitted.add(js.get("id"))
            await emit_event("position:update", js)
        # Cash moves on the same actions (a fill debits it, a sale/redeem
        # credits it), so force a balance read + push an account snapshot —
        # otherwise the headline balance lags up to balance_poll_interval (60s)
        # behind the positions the user just refreshed in one click.
        try:
            await trader.refresh_balance(STATE.cfg, force=True)
            await emit_event("account:update", await _build_account_snapshot())
        except Exception as e:
            logger.debug(f"syncPositions balance refresh failed: {e}")
        return {
            "summary": (
                f"Synced — {len(polled)} order update(s), "
                f"rescued {s.get('rescued', 0)}, resurrected {s.get('resurrected', 0)}, "
                f"imported {s.get('imported_unknowns', 0)}, "
                f"closed {len(exited)} exited externally"
            ),
        }
    if action == "recomputePnl":
        s = await trader.recompute_pnl_from_polymarket()
        return {
            "summary": f"Re-resolved {s['recomputed']} of {s['cleared']} positions from Polymarket",
        }
    if action == "reconcileFills":
        s = await trader.reconcile_fills_from_polymarket()
        return {
            "summary": (
                f"Reconciled {s['fills_reconciled']} orders from Polymarket fills, "
                f"re-resolved {s['pnl_recomputed']} of {s['pnl_cleared']}"
            ),
        }
    if action == "auditPnl":
        s = await trader.audit_pnl(200)
        worst_lines = []
        for x in s.get("samples", [])[:8]:
            worst_lines.append(
                f"  {x['ticker']} {x['direction']}: stored ${x['stored_pnl']:+.2f} → fresh ${x['fresh_pnl']:+.2f} (Δ ${x['delta']:+.2f})"
            )
        msg = (
            f"Audited {s['checked']} resolved positions: {s['flagged']} flagged. "
            f"Sum stored=${s['sum_stored_pnl']:+.2f} vs fresh=${s['sum_recompute_pnl']:+.2f} "
            f"(Δ ${s['delta']:+.2f})"
        )
        if worst_lines:
            msg += "\n" + "\n".join(worst_lines)
        return {"summary": msg, "audit": s}
    raise ValueError(f"unknown action: {action}")


async def _h_pause(p: dict) -> dict:
    """Pause the engine loop. Pausing also stops the AI agents and Autopilot
    (trade mode off, persisted like the phone's /pause): they run on the MCP
    server and Autopilot's own task, not this loop, so a pause that only froze
    the loop let them keep trading — with approvals off, unseen. Unpausing
    does NOT turn them back on; that is the AI Agents page's call."""
    STATE.paused = bool((p or {}).get("paused", False))
    out: dict = {"paused": STATE.paused}
    if STATE.paused:
        try:
            import remote
            was = await remote.stop_agents()
            out["agentsStopped"] = True
            out["message"] = "Engines paused. " + remote.agents_stopped_text(was)
        except Exception as e:  # noqa: BLE001
            logger.warning("pause: could not stop the AI agents: %s", e)
            out["agentsStopped"] = False
            out["message"] = ("Engines paused, but the AI agents could NOT be "
                              f"stopped ({type(e).__name__}). Turn them off on "
                              "the AI Agents page.")
    return out


def _run_row_to_js(r: dict) -> dict:
    """Serialise a `bot_runs` row for the renderer."""
    return {
        "id": int(r["id"]),
        "network": r.get("network") or "mainnet",
        "startedAt": _iso_utc(r.get("started_at")) or "",
        "endedAt": _iso_utc(r.get("ended_at")),
        "startCashUsd": float(r.get("start_cash_usd") or 0),
        "startPortfolioUsd": float(r.get("start_portfolio_usd") or 0),
        "startTotalUsd": float(r.get("start_total_usd") or 0),
        "endCashUsd": (
            float(r["end_cash_usd"])
            if r.get("end_cash_usd") is not None else None
        ),
        "endPortfolioUsd": (
            float(r["end_portfolio_usd"])
            if r.get("end_portfolio_usd") is not None else None
        ),
        "endTotalUsd": (
            float(r["end_total_usd"])
            if r.get("end_total_usd") is not None else None
        ),
        "pnlUsd": float(r.get("pnl_usd") or 0),
        "tradesOpened": int(r.get("trades_opened") or 0),
        "tradesWon": int(r.get("trades_won") or 0),
        "tradesLost": int(r.get("trades_lost") or 0),
        "isActive": r.get("ended_at") is None,
    }


async def _h_botRuns(p: dict) -> dict:
    """Return the most recent bot_runs rows for the History → Run
    History tab. Optional params: env, limit."""
    env = (p or {}).get("env")
    limit = int((p or {}).get("limit") or 100)
    with db.get_db() as conn:
        rows = db.get_recent_runs(conn, env=env, limit=limit)
        active = (
            db.get_active_run(conn, polymarket_auth.get_env())
            if STATE.active_run_id else None
        )
    return {
        "runs": [_run_row_to_js(r) for r in rows],
        "activeRunId": STATE.active_run_id,
        "activeRun": _run_row_to_js(active) if active else None,
    }


async def _h_shutdown(_p: dict) -> dict:
    t = asyncio.create_task(_shutdown())
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return {"shutting_down": True}


async def _h_factoryReset(_p: dict) -> dict:
    """Wipe ALL trade history, bot runs, P&L snapshots, signals, and
    scanner state from the local DB. Public Polymarket market cache is
    preserved (no PII, just public data).

    The trader loop is STOPPED first — without this, a reconcile
    cycle running mid-wipe will re-INSERT the live Polymarket positions
    we just deleted, which is exactly what made earlier reset
    attempts look like nothing happened. The active bot run (if any)
    is closed; in-memory snapshot state is cleared so the dashboard
    returns to a true zero. The loop is restarted and the next
    reconcile re-imports live positions as fresh `external` rows.
    """
    logger.warning("factory reset: STARTING — pausing trader loop")
    await _stop_loop()
    # A history download would refill the tables being wiped.
    try:
        import history_download
        await history_download.cancel()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"factoryReset: history download: {e}")

    if STATE.active_run_id:
        try:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
        except Exception as e:
            logger.warning(f"factoryReset: end_bot_run: {e}")
        STATE.active_run_id = 0

    summary = await asyncio.to_thread(db.factory_reset)
    deleted_total = sum(
        v for k, v in summary.items()
        if not k.startswith("_") and isinstance(v, int) and v > 0
    )
    if summary.get("_errors"):
        logger.error(
            f"factory reset: PARTIAL — deleted {deleted_total} rows, "
            f"errors={summary['_errors']}"
        )
    else:
        logger.warning(
            f"factory reset: COMPLETE — deleted {deleted_total} rows "
            f"({summary})"
        )

    # Reset in-memory bookkeeping. Forcing a fresh balance pull means
    # the next bot_run starts with the user's *current* balance as the
    # session baseline.
    STATE.last_whale_scan_at = ""
    STATE.last_momentum_scan_at = ""
    STATE.last_trade_scan_at = ""

    if STATE.auth_ok:
        try:
            cents, port = await trader.refresh_balance(STATE.cfg, force=True)
            env = polymarket_auth.get_env()
            with db.get_db() as conn:
                stats = db.aggregate_stats(conn, env)
                STATE.active_run_id = db.start_bot_run(
                    conn, env=env,
                    cash_usd=cents / 100.0,
                    portfolio_usd=port / 100.0,
                    lifetime_trades=int(stats.get("total_opened") or 0),
                    lifetime_wins=int(stats.get("wins") or 0),
                    lifetime_losses=int(stats.get("losses") or 0),
                )
            logger.info(
                f"Bot run #{STATE.active_run_id} started after factory reset "
                f"(env={env}, "
                f"start_total=${(cents + port) / 100:.2f})"
            )
        except Exception as e:
            logger.warning(f"factoryReset: post-reset run start: {e}")

    # Tell the frontend to drop its caches and refetch everything.
    await emit_event("data:reset", {"summary": summary})
    snap = await _build_account_snapshot()
    await emit_event("account:update", snap)

    # Resume the trader/scanner loop so reconcile re-imports live
    # Polymarket positions as fresh `external` rows on the next tick.
    await _start_loop()
    logger.info("factory reset: trader loop resumed")

    return {"ok": True, "deleted": summary}


async def _h_clearHistory(_p: dict) -> dict:
    """Wipe trade history / P&L / sessions ONLY — keep all settings, the
    wallet credentials, and the live signal feed. This is the targeted
    "reset my stats" button on the History page (vs. `factoryReset`, which
    also drops scanner/signal state).

    Same loop-pause discipline as factory reset: a reconcile cycle running
    mid-wipe would re-INSERT live Polymarket positions, so the loop is
    stopped, the active run closed, the history cleared, then the loop and a
    fresh session run are restarted with the user's current balance as the
    new baseline. Open Polymarket positions are re-imported as `external`
    rows on the next tick — only the resolved *history* is actually gone.
    """
    logger.warning("clear history: STARTING — pausing trader loop")
    await _stop_loop()

    if STATE.active_run_id:
        try:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
        except Exception as e:
            logger.warning(f"clearHistory: end_bot_run: {e}")
        STATE.active_run_id = 0

    summary = await asyncio.to_thread(db.clear_trade_history)
    deleted_total = sum(
        v for k, v in summary.items()
        if not k.startswith("_") and isinstance(v, int) and v > 0
    )
    if summary.get("_errors"):
        logger.error(
            f"clear history: PARTIAL — deleted {deleted_total} rows, "
            f"errors={summary['_errors']}"
        )
    else:
        logger.warning(
            f"clear history: COMPLETE — deleted {deleted_total} rows "
            f"({summary})"
        )

    # Re-baseline the session on the user's current balance, exactly like a
    # factory reset, so the dashboard's session P&L starts from zero.
    if STATE.auth_ok:
        try:
            cents, port = await trader.refresh_balance(STATE.cfg, force=True)
            env = polymarket_auth.get_env()
            with db.get_db() as conn:
                stats = db.aggregate_stats(conn, env)
                STATE.active_run_id = db.start_bot_run(
                    conn, env=env,
                    cash_usd=cents / 100.0,
                    portfolio_usd=port / 100.0,
                    lifetime_trades=int(stats.get("total_opened") or 0),
                    lifetime_wins=int(stats.get("wins") or 0),
                    lifetime_losses=int(stats.get("losses") or 0),
                )
            logger.info(
                f"Bot run #{STATE.active_run_id} started after clear history "
                f"(env={env}, start_total=${(cents + port) / 100:.2f})"
            )
        except Exception as e:
            logger.warning(f"clearHistory: post-clear run start: {e}")

    # Tell the frontend to drop its caches and refetch everything.
    await emit_event("data:reset", {"summary": summary})
    snap = await _build_account_snapshot()
    await emit_event("account:update", snap)

    await _start_loop()
    logger.info("clear history: trader loop resumed")

    return {"ok": True, "deleted": summary}


async def _h_crypto15m(_p: dict) -> dict:
    return await crypto15m.snapshot(STATE.cfg)


async def _h_crypto15mStatus(_p: dict) -> dict:
    return await crypto15m_trader.status(STATE.cfg, authed=STATE.auth_ok)




async def _h_c15_history(p: dict) -> dict:
    """Resolved 15m trades (with strategy attribution) for the History page."""
    limit = min(1000, max(1, int((p or {}).get("limit") or 200)))
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.recent_crypto15m_resolved(conn, env, limit=limit)
    return {"rows": [crypto15m_trader._pos_to_js(r) for r in rows]}


async def _h_c15_backtest(p: dict) -> dict:
    """Replay the CURRENT (or supplied) 15m config over recorded ticks using
    the live entry gates. Read-heavy — run off the event loop."""
    import replay
    live = STATE.cfg or {}
    cfg = dict(live)
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or polymarket_auth.get_env())
    # What the recorder is collecting RIGHT NOW (the live config, before the
    # preset patch) — so a 15m preset run on a 5m-recording machine can say
    # "you're recording 5m; select 15m on the Crypto tab to collect it".
    return await asyncio.to_thread(
        replay.replay, cfg, env=env, since_days=since,
        recording_interval=crypto15m._interval(live),
        recording_on=bool(live.get("crypto15m_record_signals", True)),
    )


async def _h_c15_parlay_generate(p: dict) -> dict:
    """Run the per-hour parlay generator over the recorded corpus. Saves the
    resulting schedule to the durable kv store (UNARMED — arming is a
    separate explicit call) unless save=False."""
    import parlay_generator
    p = p or {}
    sched = await asyncio.to_thread(
        lambda: parlay_generator.generate(
            env=str(p.get("env") or polymarket_auth.get_env()),
            interval=str(p.get("interval") or "15m"),
            since_days=int(p.get("sinceDays") or 60),
            holdout_days=int(p.get("holdoutDays") or parlay_generator.DEFAULT_HOLDOUT_DAYS),
            min_win_rate=float(p.get("minWinRate") or parlay_generator.DEFAULT_MIN_WIN_RATE),
            min_trades=int(p.get("minTrades") or parlay_generator.DEFAULT_MIN_TRADES),
        )
    )
    saved = p.get("save", True)
    if saved:
        parlay_generator.save_schedule(sched)
    return {"schedule": sched, "saved": bool(saved)}


async def _h_c15_parlay_status(_p: dict) -> dict:
    """The stored parlay schedule + armed flag (fresh read, not the cache),
    and why it should be regenerated when it is stale."""
    armed, sched = crypto15m_trader.parlay_state(force=True)
    return {"armed": armed, "schedule": sched,
            "staleReason": crypto15m_trader.parlay_stale_reason(sched)}


async def _h_c15_parlay_arm(p: dict) -> dict:
    """Arm/disarm the stored schedule for live trading. While armed, the
    parlay schedule OWNS crypto15m entries (seated hours trade their
    generated config; unseated hours don't trade at all)."""
    import parlay_generator
    try:
        parlay_generator.set_armed(bool((p or {}).get("armed")))
    except ValueError as e:
        # Zero-seated schedule: stays disarmed, and the renderer gets the why.
        armed, sched = crypto15m_trader.parlay_state(force=True)
        return {"armed": armed, "hasSchedule": bool(sched), "error": str(e)}
    armed, sched = crypto15m_trader.parlay_state(force=True)
    return {"armed": armed, "hasSchedule": bool(sched),
            "staleReason": crypto15m_trader.parlay_stale_reason(sched)}


async def _h_main_backtest(p: dict) -> dict:
    """Replay recorded whale/momentum signals through the live should_trade
    gates with follower economics."""
    import replay
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    return await asyncio.to_thread(replay.replay_main, cfg, since_days=since)


async def _h_collection_stats(_p: dict) -> dict:
    """Inventory of the passively collected research data — what the Backtest
    page's "view data" area shows. Counts + spans + a recent sample per
    stream, all cheap indexed queries."""
    env = polymarket_auth.get_env()

    def _q() -> dict:
        with db.get_db() as conn:
            c15 = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(observed_at) a,
                          MAX(observed_at) b
                   FROM crypto15m_signals WHERE network=?""", (env,)
            ).fetchone()
            ticks = conn.execute(
                "SELECT COUNT(*) FROM crypto15m_ticks WHERE network=?", (env,)
            ).fetchone()[0]
            # Per-interval split: the recorder only records the interval
            # selected on the Crypto tab, so a 15m preset on a 5m-recording
            # machine has nothing to replay — the Backtest page warns BEFORE
            # the run from this. Legacy NULL-interval rows are 15m (as replay).
            by_iv = conn.execute(
                """SELECT COALESCE(interval, '15m') iv, COUNT(*) n,
                          SUM(CASE WHEN resolved = 1 AND up_won IS NOT NULL
                                   THEN 1 ELSE 0 END) r
                   FROM crypto15m_signals WHERE network=?
                   GROUP BY iv""", (env,)
            ).fetchall()
            recent_c15 = conn.execute(
                """SELECT ticker, asset, favorite, favorite_price, up_won,
                          resolved, close_time
                   FROM crypto15m_signals WHERE network=?
                   ORDER BY close_time DESC LIMIT 12""", (env,)
            ).fetchall()
            downloaded = conn.execute(
                "SELECT COUNT(*) FROM crypto15m_signals WHERE network=? AND strike_source=?",
                (env, db.HISTORY_SOURCE),
            ).fetchone()[0]
            wh = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(created_at) a,
                          MAX(created_at) b FROM whale_trades"""
            ).fetchone()
            al = conn.execute(
                """SELECT COUNT(*) n, SUM(resolved) r, MIN(created_at) a,
                          MAX(created_at) b FROM alerts"""
            ).fetchone()
            cats = conn.execute(
                """SELECT category, COUNT(*) n FROM whale_trades
                   GROUP BY category ORDER BY n DESC LIMIT 6"""
            ).fetchall()
            recent_main = conn.execute(
                """SELECT ticker, category, taker_side, price, dollar_value,
                          outcome_correct, resolved, created_at
                   FROM whale_trades ORDER BY id DESC LIMIT 12"""
            ).fetchall()
        return {
            "c15": {
                "windows": int(c15["n"] or 0),
                "resolved": int(c15["r"] or 0),
                "ticks": int(ticks or 0),
                "downloaded": int(downloaded or 0),
                "firstAt": c15["a"], "lastAt": c15["b"],
                "recent": [dict(r) for r in recent_c15],
                "byInterval": {
                    str(r["iv"]): {"windows": int(r["n"] or 0),
                                   "resolved": int(r["r"] or 0)}
                    for r in by_iv
                },
                "recordingInterval": crypto15m._interval(STATE.cfg or {}),
            },
            "main": {
                "whales": int(wh["n"] or 0), "whalesResolved": int(wh["r"] or 0),
                "alerts": int(al["n"] or 0), "alertsResolved": int(al["r"] or 0),
                "firstAt": wh["a"] or al["a"], "lastAt": wh["b"] or al["b"],
                "topCategories": [dict(r) for r in cats],
                "recent": [dict(r) for r in recent_main],
            },
            "collecting": {
                "c15": bool((STATE.cfg or {}).get("crypto15m_record_signals", True)),
                "main": True,
            },
        }
    return await asyncio.to_thread(_q)


async def _h_history_start(p: dict) -> dict:
    """Download past crypto windows from the public API, in the background."""
    import history_download
    if (STATE.cfg or {}).get("terminal_offline_mode"):
        return {**history_download.status(), "status": "error",
                "message": "Offline mode is on in Settings, so nothing can be downloaded."}
    return history_download.start(p.get("days"), p.get("intervals"))


async def _h_history_status(_p: dict) -> dict:
    import history_download
    return history_download.status()


async def _h_history_cancel(_p: dict) -> dict:
    import history_download
    return await history_download.cancel()


async def _h_export_research(_p: dict) -> dict:
    """Dump the collected research data as CSVs the user can analyze anywhere.
    Written under data/exports/<stamp>/; the renderer reveals the folder."""
    import csv

    def _dump() -> dict:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_dir = os.path.join(os.path.dirname(str(db.db_path())), "exports", stamp)
        os.makedirs(out_dir, exist_ok=True)
        tables = ["crypto15m_signals", "crypto15m_ticks", "crypto15m_positions",
                  "whale_trades", "alerts"]
        files = []
        with db.get_db() as conn:
            for t in tables:
                rows = conn.execute(f"SELECT * FROM {t}").fetchall()
                path = os.path.join(out_dir, f"{t}.csv")
                with open(path, "w", newline="", encoding="utf-8") as f:
                    if rows:
                        w = csv.DictWriter(f, fieldnames=list(dict(rows[0]).keys()))
                        w.writeheader()
                        for r in rows:
                            w.writerow(dict(r))
                    else:
                        f.write("")
                files.append(path)
        return {"dir": out_dir, "files": files}
    return await asyncio.to_thread(_dump)


async def _h_trading_status(_p: dict) -> dict:
    """Ordered gate checklist for both engines — the "why isn't it trading"
    panel. Each row: {id, label, state: ok|blocked|off, reason}. The first
    blocked row is the answer."""
    cfg = STATE.cfg
    env = trader.get_env()
    main: list[dict] = []

    def gate(gid: str, label: str, ok: bool, reason: str = "", off: bool = False) -> None:
        main.append({
            "id": gid, "label": label,
            "state": "off" if off else ("ok" if ok else "blocked"),
            "reason": reason if not ok else "",
        })

    gate("paused", "Engine not paused", not STATE.paused, "paused by user")
    from config import account_live as _account_live
    has_key = polymarket_auth.credentials_present()
    gate("auth", "Polymarket US API key", bool(STATE.auth_ok),
         "Polymarket US rejected this key — check it on the API Key page" if has_key
         else "no key saved — Paper doesn't need one, Live does",
         off=not has_key and not _account_live(cfg))
    enabled = bool(cfg.get("enable_trading"))
    gate("master", "Trading enabled", enabled, "master switch is OFF", off=not enabled)
    gate("account", "Account is Live", _account_live(cfg),
         "the account is in Paper — engines send nothing until you switch it to Live")
    # A diagnostics endpoint must never take the panel down with it.
    try:
        blocked, why = trader._is_blocked_by_daily_risk(cfg, env)
        gate("dailyRisk", "Daily stop/take-profit", not blocked, why or "")
    except Exception:
        gate("dailyRisk", "Daily stop/take-profit", True)
    try:
        hblocked, hwhy = trader._is_blocked_by_trading_hours(cfg)
        gate("hours", "Trading hours", not hblocked, hwhy or "")
    except Exception:
        gate("hours", "Trading hours", True)
    try:
        lok, lwhy = trader.can_open_new_entries(env)
        gate("walletLock", "Account not traded elsewhere", lok, lwhy or "")
    except Exception:
        gate("walletLock", "Account not traded elsewhere", True)
    lc = dict(getattr(trader, "last_cycle", {}) or {})
    if lc.get("skipReason"):
        gate("cycle", "Last scan cycle", False, str(lc.get("skipReason")))
    else:
        gate("cycle", "Last scan cycle", True)

    c15 = await crypto15m_trader.status(cfg, authed=STATE.auth_ok)
    return {
        "main": main,
        # Last measured wall-clock cost (ms) of every timed block across ALL
        # THREE engine loops, namespaced: bare names are the master loop,
        # "copy:*" is the copy loop, "crypto15m:*" the crypto loop. The master
        # loop still runs ~17 blocks in sequence, so this remains the answer to
        # "which block is making the rest of that loop wait" — and the copy /
        # crypto entries now prove those two engines are NOT waiting on it.
        "loopBlocksMs": dict(_loop_block_ms),
        "mainFilterCounts": lc.get("filterCounts") or {},
        "mainCandidates": lc.get("candidates") or 0,
        "mainPlaced": lc.get("placed") or 0,
        "c15": {
            "enabled": c15.get("enabled"),
            "live": c15.get("trading"),
            "authed": bool(STATE.auth_ok),
            "env": env,
            "blockReasons": c15.get("blockReasons") or {},
        },
    }


# ────────────────────────────────────────────────────────────────────
# User strategy scripts (Scripts tab)
# ────────────────────────────────────────────────────────────────────


def _sanitize_script_code(raw: Any) -> str:
    """Scrub script text arriving from the renderer.

    Text pasted out of a browser, PDF or chat window can carry LONE SURROGATES
    (\\udc80-\\udcff) — what a smart quote turns into when it loses its encoding
    on the way in. Python stores them happily and SQLite accepts them, but the
    moment the row is serialized back to the renderer `.encode("utf-8")` raises
    `UnicodeEncodeError: surrogates not allowed`, killing the RPC. The renderer
    only sees `error invoking remote method "scripts:validate"`, which points
    nowhere near the real cause.

    Replacing them here means the worst case is an ordinary syntax error with a
    line number, which the editor can actually show the user.
    """
    return str(raw or "").encode("utf-8", "replace").decode("utf-8", "replace")


def _script_status_js(r: dict) -> dict:
    """The one thing the Scripts page needs and never had: whether this script
    is ACTUALLY running, and if not, which gate is holding it.

    The page used to show `enabled` plus shadow/live, neither of which answers
    the question. A script can read ENABLED and be doing nothing because the
    wallet is disconnected, because it's armed while the master switch is off,
    because it's past the concurrency cap, or because its code failed to
    compile — and the user's only clue was an empty ledger. Every gate is
    evaluated here, in the process that applies them, so the line can't drift
    from the truth the way a re-derivation in the renderer would.
    """
    import script_engine
    sid = str(r.get("id") or "")
    st = script_engine.status_for(sid)
    cfg = STATE.cfg or {}
    enabled = bool(r.get("enabled"))
    shadow = bool(r.get("dry_run", 1))

    if r.get("needs_review"):
        state, detail = ("off",
                         "Written by an AI agent and not yet reviewed. Read the "
                         "code, then flip Enabled to approve it and start it in "
                         "shadow.")
    elif not enabled:
        state, detail = "off", "Not enabled — flip Enabled to start it."
    elif not STATE.auth_ok:
        state, detail = ("blocked",
                         "No API key saved — add one on the API Key page.")
    elif not shadow and not __import__("config").account_live(cfg):
        state, detail = ("blocked",
                         "Armed for real orders, but the account is in Paper — "
                         "it is not running. Switch the account to Live, or set "
                         "this script back to Shadow.")
    elif not shadow and not cfg.get("scripts_live_enabled"):
        state, detail = ("blocked",
                         'Armed for real orders, but the "Scripts live" master '
                         "switch is off — it is not running. Turn the master "
                         "switch on, or set this script back to Shadow.")
    elif st.get("gate") == "over_cap":
        state, detail = ("blocked",
                         f"Past the {int(cfg.get('script_max_enabled') or 10)}-script "
                         "concurrency cap — disable another script or raise the cap.")
    elif st.get("gate") == "compile_failed":
        state, detail = "error", "Its code failed to load — see the error above."
    elif not st.get("ticks"):
        state, detail = ("starting",
                         "Enabled and waiting for its first tick (~5s).")
    else:
        ticks = int(st.get("ticks") or 0)
        orders = int(st.get("orders") or 0)
        noun = "shadow order" if shadow else "order"
        detail = (f"Running in {'shadow' if shadow else 'LIVE'} — {ticks:,} tick(s), "
                  f"{orders:,} {noun}{'' if orders == 1 else 's'}")
        if st.get("lastIntentAt"):
            detail += f", last signal {_iso_utc(st['lastIntentAt'])}"
        else:
            detail += ", no setup found yet (a selective strategy can go days)"
        state = "running"
    return {
        "state": state, "detail": detail,
        "ticks": int(st.get("ticks") or 0),
        "intents": int(st.get("intents") or 0),
        "orders": int(st.get("orders") or 0),
        "lastTickAt": st.get("lastTickAt"),
        "lastIntentAt": st.get("lastIntentAt"),
        "lastOrderAt": st.get("lastOrderAt"),
    }


def _script_row_js(r: dict, stats: dict | None = None,
                   shadow_stats: dict | None = None) -> dict:
    import script_audit
    import script_backtest
    code = _sanitize_script_code(r.get("code"))
    return {
        "id": r.get("id"), "name": r.get("name"),
        "description": r.get("description") or "",
        # Scrubbed on the way OUT as well: a row saved before the sanitizer
        # existed would otherwise still take down scriptsList.
        "code": code,
        "enabled": bool(r.get("enabled")),
        # The script's OWN coin scope (null = all). Never crypto15m_assets.
        "assets": script_backtest.parse_asset_scope(r.get("assets")),
        "audit": script_audit.audit(code),
        "status": _script_status_js(r),
        # dry_run defaults ON: a script never places a real order until the
        # user consciously arms it (and pre-2.0 rows upgrade INTO shadow).
        "dryRun": bool(r.get("dry_run", 1)),
        # Who last changed the code ("user", or "agent:<name>"), and whether
        # an agent's code is still waiting for the user's first look.
        "author": str(r.get("author") or "") or "user",
        "needsReview": bool(r.get("needs_review")),
        "shadowStats": (shadow_stats or {}).get(str(r.get("id"))) or None,
        "notes": r.get("notes") or "",
        "lastError": r.get("last_error"),
        "lastErrorAt": _iso_utc(r.get("last_error_at")),
        "createdAt": _iso_utc(r.get("created_at")),
        "updatedAt": _iso_utc(r.get("updated_at")),
        "stats": (stats or {}).get(str(r.get("id"))) or None,
    }


async def _h_scripts_list(_p: dict) -> dict:
    env = polymarket_auth.get_env()
    with db.get_db() as conn:
        rows = db.list_user_scripts(conn)
        stats = db.script_live_stats(conn, env)
        shadow = db.script_shadow_stats(conn, env)
    return {"scripts": [_script_row_js(r, stats, shadow) for r in rows]}


async def _h_script_set_dry_run(p: dict, *, agent: str | None = None) -> dict:
    """Arm a script for real orders (dryRun=False) or return it to shadow.

    Arming is the moment a script can spend money, so it re-validates first
    and refuses code that doesn't pass — the same gate as enabling.

    Only the USER arms. `agent` is set by the MCP tools (never by the RPC
    dispatcher, which passes params only): an agent may return a script to
    shadow, never arm one. A script an agent wrote must be reviewed (enabled
    by the user) before it can be armed."""
    sid = str(p.get("id") or "")
    dry = bool(p.get("dryRun"))
    if agent and not dry:
        raise ValueError("Only the user can arm a script for real orders, from "
                         "the Scripts page.")
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        if not dry:
            if row.get("needs_review"):
                raise ValueError("An AI agent wrote this script's code and it has not "
                                 "been reviewed. Read it, flip Enabled to approve it, "
                                 "then arm it.")
            errors = script_sandbox.validate(str(row.get("code") or ""))
            if errors:
                raise ValueError("script does not validate: " + errors[0])
            errors = _agent_code_errors(row)
            if errors:
                raise ValueError("this agent-written script breaks the agent-script "
                                 "rules: " + errors[0])
        db.update_user_script(conn, sid, dry_run=1 if dry else 0)
        row = db.get_user_script(conn, sid)
        stats = db.script_live_stats(conn, polymarket_auth.get_env())
        shadow = db.script_shadow_stats(conn, polymarket_auth.get_env())
    logger.warning(
        f"[scripts] {row.get('name')} ({sid[:8]}) is now "
        f"{'SHADOW (no real orders)' if dry else 'ARMED FOR REAL ORDERS'}")
    return {"script": _script_row_js(row or {}, stats, shadow)}


async def _h_script_shadow_orders(p: dict) -> dict:
    """The shadow ledger for one script — what it would have traded."""
    with db.get_db() as conn:
        rows = db.list_script_shadow(conn, str(p.get("id") or ""),
                                     int(p.get("limit") or 200))
    return {"orders": [{
        "id": r.get("id"), "source": r.get("source"),
        "asset": r.get("asset"), "ticker": r.get("ticker"),
        "side": r.get("side"), "contracts": r.get("contracts"),
        "entryCents": r.get("entry_cents"), "orderType": r.get("order_type"),
        "reason": r.get("reason") or "", "refused": bool(r.get("refused")),
        "note": r.get("note") or "",
        "resolved": bool(r.get("resolved")),
        "won": (None if r.get("outcome_correct") is None
                else bool(r.get("outcome_correct"))),
        "pnlUsd": r.get("pnl_usd"),
        "at": _iso_utc(r.get("created_at")),
    } for r in rows]}


def _is_agent_author(row: dict | None) -> bool:
    return str((row or {}).get("author") or "").startswith("agent")


def _agent_code_errors(row: dict | None) -> list[str]:
    """The agent-script allowlist errors for a row an AI agent wrote ([] for
    the user's own code, which is full Python by design)."""
    if not _is_agent_author(row):
        return []
    import script_audit
    return script_audit.validate_agent(_sanitize_script_code((row or {}).get("code")))


async def _push_script_off(sid: str, reason: str) -> None:
    """Tell the engine and the Scripts page that a script stopped: the
    compiled module is dropped (a re-enable compiles the row fresh) and the
    page hears it at once instead of on its next poll."""
    try:
        import script_engine
        script_engine.forget(sid)
    except Exception:  # noqa: BLE001
        pass
    try:
        await emit_event("script:status", {"id": sid, "enabled": False, "lastError": reason})
    except Exception:  # noqa: BLE001
        pass


async def _h_script_save(p: dict, *, agent: str | None = None) -> dict:
    """Create or update a script. Validation runs in the script's declared
    trust mode; an invalid script still SAVES (so work isn't lost) but comes
    back with the errors and is force-disabled.

    `agent` names the AI agent saving it (set by mcp_tools, never by the RPC
    dispatcher, which passes params only - the renderer's save is always the
    user's). Code an agent writes or changes must pass the agent allowlist
    (script_audit.validate_agent) and lands DISABLED, in SHADOW and marked
    needs_review: it runs nowhere in this process until the user enables it
    from the Scripts page, and only the user can arm it again."""
    sid = str(p.get("id") or "").strip() or os.urandom(8).hex()
    code = _sanitize_script_code(p.get("code"))
    meta = script_sandbox.parse_header(code)
    name = str(p.get("name") or "").strip() or meta.get("name") or "Untitled script"
    desc = str(p.get("description") or "").strip() or meta.get("description") or ""
    import script_audit
    if agent:
        bad = script_audit.validate_agent(code)
        if bad:
            raise ValueError("Scripts written by an AI agent may only use plain "
                             "computation (see get_script_docs). Refused: "
                             + "; ".join(bad[:5]))
    with db.get_db() as conn:
        existing = db.get_user_script(conn, sid)
        errors = script_sandbox.validate(code)
        audit = script_audit.audit(code)
        # ARMED scripts disarm themselves when their code changes and the new
        # code has a critical audit finding. Consent to spend real money is
        # given for code the user actually read: the AI-paste and
        # backtest-autosave flows both land here, so without this a pasted
        # rewrite would hot-swap straight into a live, wallet-connected script.
        # Clean code doesn't trip it — this fires on the case that matters.
        code_changed = bool(existing) and str(existing.get("code") or "") != code
        force_off = (code_changed and bool(existing.get("enabled"))
                     and not audit["ok"])
        # An agent's NEW code (a new script, or a rewrite of any script —
        # including a disabled one still marked armed, dry_run=0) is inert
        # until the user has looked at it: off, shadow, needs review.
        agent_code = bool(agent) and (existing is None or code_changed)
        was_armed = bool(existing) and not bool(existing.get("dry_run", 1))
        was_enabled = bool(existing) and bool(existing.get("enabled"))
        db.upsert_user_script(conn, {
            "id": sid, "name": name, "description": desc, "code": code,
            "notes": str(p.get("notes") or ""),
        })
        if agent_code:
            db.update_user_script(conn, sid, enabled=0, dry_run=1, needs_review=1,
                                  author=f"agent:{str(agent)[:60]}")
        elif code_changed and not agent:
            # The user changed the code in their own editor: it is theirs now.
            db.update_user_script(conn, sid, author="user", needs_review=0)
        if errors:
            # A script that no longer validates must not keep trading.
            db.update_user_script(conn, sid, enabled=0,
                                  last_error="; ".join(errors[:3])[:500])
        elif force_off:
            db.update_user_script(
                conn, sid, enabled=0,
                last_error=f"Code changed and the risk audit flagged "
                           f"{audit['critical']} critical issue(s) — disabled "
                           "pending your review. Read the Risk tab, then "
                           "re-enable.")
        else:
            db.update_user_script(conn, sid, last_error=None)
        row = db.get_user_script(conn, sid)
    disarmed = force_off or (agent_code and (was_armed or was_enabled))
    if agent_code and existing is not None:
        logger.warning(
            f"[scripts] AI agent {agent} rewrote {name} ({sid[:8]}): disabled, "
            f"back in SHADOW, waiting for your review"
            f"{' (it was ARMED for real orders)' if was_armed else ''}")
        await _push_script_off(sid, "Rewritten by an AI agent: disabled and in "
                                    "shadow until you review it.")
    elif force_off:
        await _push_script_off(sid, "Code changed with critical audit findings.")
    return {"script": _script_row_js(row or {}), "errors": errors,
            "warnings": _script_warnings(code), "audit": audit,
            "disarmed": disarmed}


def _script_warnings(code: str) -> list[str]:
    import replay
    missing = sorted(set(script_sandbox.find_ctx_fields(code)) - replay._DERIVABLE)
    warnings = []
    if missing:
        warnings.append(
            "Reads ctx fields that are LIVE-ONLY (None during backtests): "
            + ", ".join(missing))
    return warnings


async def _h_script_delete(p: dict) -> dict:
    sid = str(p.get("id") or "")
    with db.get_db() as conn:
        db.delete_user_script(conn, sid)
    # Release the compiled module too — otherwise a deleted script's code
    # (and its captured state) stays resident for the life of the process.
    try:
        import script_engine
        script_engine.forget(sid)
    except Exception:
        pass
    return {"ok": True}


async def _h_script_set_enabled(p: dict, *, agent: str | None = None) -> dict:
    """Enable / disable a script. The USER enabling a script an AI agent
    wrote is the review step: it clears needs_review (they flipped it on in
    the Scripts page, with the code in front of them). An agent (`agent` set
    by mcp_tools, never by the RPC dispatcher) can never enable code that is
    still waiting for that review."""
    sid = str(p.get("id") or "")
    enabled = bool(p.get("enabled"))
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        if enabled:
            if agent and row.get("needs_review"):
                raise ValueError("An AI agent wrote this script and the user has not "
                                 "reviewed it yet. Only the user can enable it, from "
                                 "the Scripts page.")
            errors = script_sandbox.validate(str(row.get("code") or ""))
            if errors:
                raise ValueError("script does not validate: " + errors[0])
            errors = _agent_code_errors(row)
            if errors:
                raise ValueError("this agent-written script breaks the agent-script "
                                 "rules (edit it to make it yours, or delete it): "
                                 + errors[0])
            if agent:
                db.update_user_script(conn, sid, enabled=1, last_error=None)
            else:
                db.update_user_script(conn, sid, enabled=1, last_error=None,
                                      needs_review=0)
        else:
            db.update_user_script(conn, sid, enabled=0)
        row = db.get_user_script(conn, sid)
    return {"script": _script_row_js(row or {})}


async def _h_script_set_assets(p: dict) -> dict:
    """Set a script's OWN coin scope (null/[] = every coin).

    This replaces the crypto15m_assets coupling: which coins a script sees is
    now its own property, so the 15m Crypto tab's asset checkboxes (and any
    preset applied there) can never silently starve a script again."""
    import script_backtest
    sid = str(p.get("id") or "")
    scope = script_backtest.parse_asset_scope(p.get("assets"))
    with db.get_db() as conn:
        row = db.get_user_script(conn, sid)
        if not row:
            raise ValueError("script not found")
        db.update_user_script(
            conn, sid, assets=json.dumps(scope) if scope else None)
        row = db.get_user_script(conn, sid)
    return {"script": _script_row_js(row or {})}


async def _h_script_validate(p: dict) -> dict:
    """Validate code (editor button + the AI-output import path). Returns
    parsed metadata so the import flow can prefill name/description."""
    import script_audit
    code = _sanitize_script_code(p.get("code"))
    errors = script_sandbox.validate(code)
    meta = script_sandbox.parse_header(code)
    return {
        "ok": not errors, "errors": errors,
        "warnings": _script_warnings(code),
        # The risk audit rides along with every validate so the editor can
        # show findings as you type — this is what replaced the sandbox, and
        # a review tool nobody looks at is worth nothing.
        "audit": script_audit.audit(code),
        "name": meta.get("name") or "", "description": meta.get("description") or "",
        "hasHeader": bool(meta.get("version")),
        "ctxFields": script_sandbox.find_ctx_fields(code),
    }


async def _h_script_backtest(p: dict, *, agent: str | None = None) -> dict:
    """Backtest a script (by id, or raw code from the editor) over the
    recorded tick corpus. Never places orders — script_backtest is
    structurally order-free.

    Where the code came from decides where it runs. The user's own code runs
    here, in-process, as it always has. Code an AI agent wrote — passed by
    an agent (`agent`, set by mcp_tools only) or a saved row an agent last
    changed, even when the USER clicks Backtest on it — runs in the
    isolated child, and agent code must pass the agent allowlist first."""
    import script_audit
    import script_backtest
    cfg = dict(STATE.cfg or {})
    patch = (p or {}).get("config") or {}
    if isinstance(patch, dict):
        cfg.update(patch)
    cfg = merge_with_defaults(cfg)
    since = int((p or {}).get("sinceDays") or 60)
    env = str((p or {}).get("env") or polymarket_auth.get_env())
    code = _sanitize_script_code(p.get("code"))
    assets = (p or {}).get("assets")
    isolate = bool((p or {}).get("isolate")) or bool(agent)
    row = None
    if p.get("id"):
        with db.get_db() as conn:
            row = db.get_user_script(conn, str(p.get("id") or ""))
    if not code:
        if not row:
            raise ValueError("script not found")
        # Rows saved BEFORE the scrub existed can still hold surrogates.
        code = _sanitize_script_code(row.get("code"))
        if assets is None:
            assets = row.get("assets")
    agent_code = bool(agent and p.get("code")) or (
        _is_agent_author(row) and code == _sanitize_script_code((row or {}).get("code")))
    if agent_code:
        isolate = True
        bad = script_audit.validate_agent(code)
        if bad:
            raise ValueError("Scripts written by an AI agent may only use plain "
                             "computation (see get_script_docs). Refused: "
                             + "; ".join(bad[:5]))
    if isolate:
        return await _script_backtest_isolated(cfg, str(code), assets, env, since)
    return await asyncio.to_thread(
        script_backtest.run, cfg, str(code),
        assets=assets, env=env, since_days=since,
    )


# Environment variables a backtest child never inherits.
_SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH", re.I)
# Environment variables that locate the user's profile. The child gets its
# own empty stand-ins, so no app path logic can lead it to the real one.
_PROFILE_ENV = ("KRYPT_POLYBOT_US_USERDATA", "KRYPT_POLYBOT_US_VAULT", "APPDATA",
                "LOCALAPPDATA", "USERPROFILE", "HOME")


def _backtest_child_env(tmp: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV.search(k)}
    for k in _PROFILE_ENV:
        d = os.path.join(tmp, "profile", k.lower())
        os.makedirs(d, exist_ok=True)
        env[k] = d
    env["PYTHONIOENCODING"] = "utf-8"
    if not getattr(sys, "frozen", False):
        # Dev / tests: the venv's site-packages ride on PYTHONPATH because the
        # child is started from the BASE interpreter (_backtest_child_cmd).
        paths = [str(Path(__file__).resolve().parent)]
        paths += [p for p in sys.path if p and "site-packages" in p]
        env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _backtest_child_cmd() -> list[str]:
    """The child's command line. The frozen backend is one process (a
    PyInstaller onedir build runs Python in-process). A Windows venv's
    python.exe is a LAUNCHER that starts the base interpreter as a second
    process, which the child's job object (one process, no children) refuses,
    so dev and tests start the base interpreter directly."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--backtest-child"]
    exe = sys.executable
    base = getattr(sys, "_base_executable", None)
    if sys.platform == "win32" and base and sys.prefix != sys.base_prefix:
        exe = base
    return [exe, str(Path(__file__).resolve()), "--backtest-child"]


async def _script_backtest_isolated(
    cfg: dict, code: str, assets, env: str, since: int, *, db_file: str | None = None,
) -> dict:
    """Run a script backtest in a child backend process with no credentials
    (script_backtest_child.py): how an AI agent's backtest_script runs the
    code it wrote. Killed if it outlives the backtest's own wall-clock cap.

    The child never sees the real database: `db_file` (default: the app's
    DB) is only the SOURCE of a read-only extract built here, in the child's
    own temporary directory. On Windows the child runs in a Job Object that
    forbids it starting any process."""
    import shutil
    import subprocess
    import tempfile

    import script_backtest
    import script_backtest_child
    cmd = _backtest_child_cmd()
    tmp = tempfile.mkdtemp(prefix="kp-backtest-")
    job = None
    try:
        src = Path(db_file) if db_file else db.db_path()
        extract = Path(tmp) / "data" / "backtest-extract.db"
        await asyncio.to_thread(
            script_backtest_child.build_extract, src, extract, int(since), str(env),
            src.with_name(db.RESEARCH_ARCHIVE_NAME))
        child_env = _backtest_child_env(tmp)
        # the engine's settings, without anything the backtest has no use for
        safe_cfg = {k: v for k, v in cfg.items()
                    if not k.startswith(("mcp_", "remote_", "ai_", "autopilot_"))}
        payload = json.dumps({
            "db": str(extract), "cfg": safe_cfg, "code": code,
            "assets": assets, "env": env, "sinceDays": since,
        }, default=str).encode("utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=child_env, cwd=tmp,
            creationflags=flags,
        )
        # The child blocks reading stdin before it runs anything, so it is
        # contained before a line of the script can execute.
        try:
            job = script_backtest_child.contain(proc.pid)
        except OSError as e:
            logger.warning(f"[scripts] backtest child not contained in a job object: {e}")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(payload), timeout=script_backtest.MAX_RUN_SECS + 60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"scriptError": "the backtest ran past its time limit and was stopped",
                    "n": 0, "trades": [], "caveats": ["Stopped: over the time limit."]}
        try:
            return json.loads(out.decode("utf-8"))
        except ValueError:
            tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
            return {"scriptError": "the isolated backtest exited without a result"
                                   + (f": {' | '.join(tail)}" if tail else ""),
                    "n": 0, "trades": [], "caveats": []}
    finally:
        script_backtest_child.release(job)
        shutil.rmtree(tmp, ignore_errors=True)


async def _h_script_context_pack(_p: dict) -> dict:
    import script_docs
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    env = polymarket_auth.get_env()
    text = await asyncio.to_thread(script_docs.build_context_pack, cfg, env)
    return {"text": text}


async def _h_script_api_docs(_p: dict) -> dict:
    """Structured script-API reference for the in-app docs panel. Rendered
    FROM the live modules (same sources as the context pack) so the panel can
    never drift from what the engine/backtester actually do."""
    import replay
    import script_docs
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    return {
        "contract": script_docs.CONTRACT_DOC,
        "fields": [
            {"name": n, "doc": d, "backtestable": n in replay._DERIVABLE}
            for n, d in script_docs.FIELD_DOCS.items()
        ],
        "injected": list(script_sandbox.INJECTED_GLOBALS),
        "hookTimeoutSec": float(cfg.get("script_hook_timeout_sec") or 1.0),
        "rails": {
            "maxEntryCents": int(cfg.get("script_max_entry_cents") or 97),
            "maxContracts": int(cfg.get("script_max_contracts") or 20),
            "maxOpen": int(cfg.get("script_max_open") or 2),
            "dailyLossUsd": float(cfg.get("script_daily_loss_usd") or 25.0),
            "defaultOrderSize": int(cfg.get("crypto15m_order_size") or 1),
        },
        "examples": [
            {"name": "Late Favorite Follow", "code": script_docs.EXAMPLE_SIMPLE},
            {"name": "Momentum Confirm (stateful)", "code": script_docs.EXAMPLE_STATEFUL},
            {"name": "Trailing Exit (manage hook)", "code": script_docs.EXAMPLE_MANAGE},
            {"name": "Night Shift (supervisor)", "code": script_docs.EXAMPLE_SUPERVISOR},
        ],
        # One-click Strategy presets (Simple mode's Strategies page). Served
        # from the backend so the code can never drift from the shipped
        # engine — same reason the examples live here.
        "presets": script_docs.PRESETS,
    }


async def _h_polymarketUrl(p: dict) -> dict:
    url = await polymarket_api.web_market_url(
        event_ticker=str(p.get("eventTicker") or ""),
        ticker=str(p.get("ticker") or ""),
        env=str(p.get("env") or "mainnet"),
    )
    return {"url": url}


# ────────────────────────────────────────────────────────────────────
# Terminal (manual)
#
# The read side never spends money; the trade side is gated in THIS process,
# never only in the UI. Note what these handlers accept: a condition id, a
# column, a side, an interval. No handler accepts a URL or a host — the host
# table lives in terminal.py and nothing user-supplied reaches it. That is the
# structural fix for SSRF; a URL validator is the fragile one.
# ────────────────────────────────────────────────────────────────────


async def _h_terminal_discover(p: dict) -> dict:
    import terminal
    return await terminal.discover(
        str(p.get("column") or "volume"),
        int(p.get("limit") or 40),
        p.get("filters") or None,
    )


async def _h_terminal_search(p: dict) -> dict:
    import terminal
    return await terminal.search(str(p.get("query") or ""), int(p.get("limit") or 30))


async def _h_terminal_market(p: dict) -> dict:
    import terminal
    m = await terminal.market(str(p.get("conditionId") or ""))
    return {"market": m}


async def _h_terminal_book(p: dict) -> dict:
    import terminal
    return await terminal.book(
        str(p.get("conditionId") or ""), str(p.get("side") or "yes"))


async def _h_terminal_history(p: dict) -> dict:
    import terminal
    return await terminal.price_history(
        str(p.get("conditionId") or ""),
        str(p.get("side") or "yes"),
        str(p.get("interval") or "1d"),
    )


async def _h_terminal_resolution(p: dict) -> dict:
    import terminal
    return await terminal.resolution_report(str(p.get("conditionId") or ""))


async def _h_terminal_related(p: dict) -> dict:
    import terminal
    return await terminal.related(
        str(p.get("conditionId") or ""), int(p.get("limit") or 25))


async def _h_terminal_cross_venue(p: dict) -> dict:
    """Compare this Polymarket market with its Kalshi counterpart.

    Read-only on both venues. The first call warms Kalshi's series index
    (~11,800 rows, a few seconds) and later ones are nearly free — which is
    why this is opt-in from the market page rather than part of any poll.
    """
    import cross_venue
    return await cross_venue.compare(
        str(p.get("conditionId") or ""), str(p.get("interval") or "1d"))


async def _h_terminal_micro(p: dict) -> dict:
    import terminal
    return terminal.micro_series(
        str(p.get("conditionId") or ""), str(p.get("side") or "yes"))


async def _h_terminal_health(_p: dict) -> dict:
    import terminal
    return terminal.health()


async def _h_terminal_watchlist(p: dict) -> dict:
    import terminal
    action = str(p.get("action") or "list")
    cid = str(p.get("conditionId") or "")
    if action == "add":
        entries = terminal.watchlist_add(cid, p.get("title"), p.get("note"))
    elif action == "remove":
        entries = terminal.watchlist_remove(cid)
    else:
        entries = terminal.watchlist_list()
    return {"entries": entries}


async def _h_terminal_preview(p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.preview(p.get("order") or {})


async def _h_terminal_place(p: dict) -> dict:
    """Send a hand-placed order.

    Gated on the SAME live-trading switch as every engine. A terminal that
    would place an order while the app says PAUSED is lying about its own
    state, and the user would have no way to stop it.
    """
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    # Paper mode must be impossible to trade around. If the UI still sends a
    # live order while the user believes they are on paper, that is the single
    # worst bug this feature could have — so main refuses it outright rather
    # than trusting the renderer to have switched.
    if cfg.get("terminal_paper_mode"):
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": (
                "Paper mode is on, so no real order was sent. Turn it off in "
                "Settings if you meant to trade live."),
        }
    # The terminal is MANUAL. `enable_trading` is the automation master
    # switch (the engines); a hand-placed order is gated on paper mode, the
    # wallet, and the manual ceilings — not on whether the bots are running.
    if not STATE.auth_ok:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": "No API key is saved, so no order can be signed.",
        }
    res = await terminal_trade.place(p.get("order") or {})
    if res.get("ok"):
        logger.info(
            "terminal: manual order %s", res.get("message") or res.get("orderId"))
    return res


async def _h_terminal_flatten_wallet(p: dict) -> dict:
    """Sell every open position in the wallet — or, with ``preview``, price it.

    The preview half is read-only, so it is not gated: showing a user what
    they hold and what it would fetch spends nothing. The half that SENDS is
    gated exactly like `_h_terminal_place` — paper mode, the live-trading
    switch, a connected wallet — because one button that closes everything is
    still one button that signs orders.
    """
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    if (p or {}).get("preview"):
        return await terminal_trade.flatten_preview()

    refuse = {"ok": False, "attempted": 0, "sold": 0, "refused": 0,
              "skipped": 0, "proceedsUsd": None, "engineTrackedSold": None,
              "reason": "flatten", "results": []}
    if cfg.get("terminal_paper_mode"):
        return {**refuse, "message": (
            "Paper mode is on, so nothing was sold. Turn it off in Settings "
            "if you meant to flatten for real.")}
    if not STATE.auth_ok:
        return {**refuse, "message": (
            "No API key is saved, so no sell can be signed.")}
    res = await terminal_trade.flatten_wallet(
        allow_trading=True, origin="manual", reason=str(
            (p or {}).get("reason") or "flatten"))
    logger.info("terminal: flatten account — %s", res.get("message"))
    try:
        await emit_event("backend:reconciled", {"flattened": res.get("sold")})
    except Exception:
        pass
    return res


async def _h_terminal_sell_preview(p: dict) -> dict:
    """Price a one-click exit out of one position. Read-only, so ungated:
    showing a user what a position would fetch spends nothing.

    The `paper` flag is forced from the CONFIG, never trusted from the
    renderer — the panel must price the same book the Sell button will send
    against, and a renderer that disagreed with Settings about which mode it
    is in would show paper numbers over a live sell.
    """
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    req = dict(p.get("order") or {})
    req["paper"] = bool(cfg.get("terminal_paper_mode"))
    return await terminal_trade.sell_position_preview(req)


async def _h_terminal_sell_position(p: dict) -> dict:
    """Send the one-click exit. Gated exactly like `_h_terminal_place`.

    Paper mode is not a refusal here — it is a ROUTE: the panel's button says
    "Sell paper" and the order goes to the simulator. What must never happen
    is the two disagreeing, so the mode is read from the config in main and
    the renderer's flag is ignored.
    """
    import terminal_trade
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    paper = bool(cfg.get("terminal_paper_mode"))
    req = dict(p.get("order") or {})
    req["paper"] = paper
    if not paper and not STATE.auth_ok:
        return {
            "ok": False, "orderId": None, "status": None, "localId": None,
            "sentSize": None, "limitCents": None, "filledSize": None,
            "avgPriceCents": None, "proceedsUsd": None, "killedSize": None,
            "estimatedProceedsUsd": None, "preview": None,
            "message": "No API key is saved, so no sell can be signed.",
        }
    res = await terminal_trade.sell_position(req)
    if res.get("ok"):
        logger.info("terminal: position sell — %s", res.get("message"))
    return res


async def _h_terminal_paper_place(p: dict) -> dict:
    """Simulate a hand-placed order. Needs no wallet and no live-trading
    switch: the entire point is to practise before funding anything."""
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    return await terminal_paper.place(p.get("order") or {}, cfg)


async def _h_terminal_paper_portfolio(_p: dict) -> dict:
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    return await terminal_paper.portfolio(cfg)


async def _h_terminal_paper_orders(p: dict) -> dict:
    import terminal_paper
    return terminal_paper.orders(int(p.get("limit") or 100))


async def _h_terminal_paper_cancel(p: dict) -> dict:
    import terminal_paper
    return await terminal_paper.cancel(int(p.get("localId") or 0))


async def _h_terminal_paper_reset(_p: dict) -> dict:
    import terminal_paper
    return terminal_paper.reset()


async def _h_terminal_cancel(p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.cancel(str(p.get("orderId") or ""))


async def _h_terminal_orders(p: dict) -> dict:
    """Resting orders. With a `conditionId` the list is narrowed to that
    market and each row carries the touch it would have to reach to fill —
    what the Market page's working-order rows show. Without one it is the
    Track-wide list, unchanged and with no extra book reads."""
    import terminal_trade
    cid = str((p or {}).get("conditionId") or "").strip() or None
    return await terminal_trade.resting_orders(cid)


async def _h_terminal_portfolio(_p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.portfolio()


async def _h_terminal_settled(_p: dict) -> dict:
    """The settled RECORD from the fill ledger — deliberately its own call.

    Kept off `terminalPortfolio` so the 5 s positions poll stays a wallet read
    and never pays for the ledger walk; this one runs when the user opens the
    settled table.
    """
    import terminal_trade
    return await terminal_trade.settled_record()


async def _h_terminal_fills(p: dict) -> dict:
    import terminal_trade
    return terminal_trade.fills(int(p.get("limit") or 100))


async def _h_terminal_order_log(p: dict) -> dict:
    import terminal_trade
    return terminal_trade.order_log(int(p.get("limit") or 100))


async def _h_terminal_reconcile(_p: dict) -> dict:
    import terminal_trade
    return await terminal_trade.reconcile_fills()


async def _h_terminal_standing(p: dict) -> dict:
    import terminal_trade
    action = str(p.get("action") or "list")
    if action == "add":
        try:
            terminal_trade.standing_add(p.get("item") or {})
        except terminal_trade.TerminalTradeError as e:
            return {"instructions": terminal_trade.standing_list(), "error": str(e)}
    elif action == "cancel":
        terminal_trade.standing_cancel(str(p.get("id") or ""))
    elif action == "arm":
        terminal_trade.standing_set_armed(
            str(p.get("id") or ""), bool(p.get("armed")))
    return {"instructions": terminal_trade.standing_list(), "error": None}


# ────────────────────────────────────────────────────────────────────
# Remote control (Discord / Telegram)
#
# The bots let a phone command this app, so the rules live in ONE place
# (remote.py) and both transports are thin shells around it. These handlers
# only do setup: tokens, pairing, and turning the transports on and off.
# ────────────────────────────────────────────────────────────────────


async def _remote_sync() -> dict:
    """Start or stop each bot to match config. Idempotent — safe to call on
    every config change and on a timer."""
    import bot_discord
    import bot_telegram
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    on = bool(cfg.get("remote_enabled"))
    want_d = on and bool(cfg.get("remote_discord_enabled"))
    want_t = on and bool(cfg.get("remote_telegram_enabled"))
    if want_d and not bot_discord.is_running():
        await bot_discord.start()
    elif not want_d:
        # Unconditional: is_running() is False while the gateway reconnects,
        # and stop() is idempotent — a config change mid-reconnect used to
        # skip this and leave the reconnect loop alive.
        await bot_discord.stop()
    if want_t and not bot_telegram.is_running():
        await bot_telegram.start()
    elif not want_t and bot_telegram.is_running():
        await bot_telegram.stop()
    import remote
    return remote.status()


async def _h_remote_status(_p: dict) -> dict:
    import remote
    st = remote.status()
    import bot_secrets
    st["discord"]["tokenMasked"] = bot_secrets.masked("discord")
    st["telegram"]["tokenMasked"] = bot_secrets.masked("telegram")
    return st


async def _h_remote_set_token(p: dict) -> dict:
    """Verify a token BEFORE storing it, so a typo fails on the setup screen
    rather than as a bot that silently never connects."""
    import bot_discord
    import bot_secrets
    import bot_telegram
    platform = str(p.get("platform") or "")
    token = str(p.get("token") or "").strip()
    if platform not in ("discord", "telegram"):
        return {"ok": False, "message": f"Unknown platform {platform!r}."}
    if not token:
        bot_secrets.clear_token(platform)
        await _remote_sync()
        return {"ok": True, "message": "Token cleared."}
    mod = bot_discord if platform == "discord" else bot_telegram
    res = await mod.verify_token(token)
    if not res.get("ok"):
        return {"ok": False, "message": res.get("message") or "Rejected."}
    bot_secrets.save_token(platform, token)
    await _remote_sync()
    return {"ok": True, "message": res.get("message") or "Saved.",
            "username": res.get("username")}


async def _h_remote_pair_code(p: dict) -> dict:
    import remote
    return remote.new_pair_code(str(p.get("platform") or "telegram"))


async def _h_remote_unpair(p: dict) -> dict:
    import remote
    rows = remote.remove_paired(
        str(p.get("platform") or ""), str(p.get("id") or ""))
    return {"paired": rows}


async def _h_remote_add_paired(p: dict) -> dict:
    """Add an id by hand — the Discord path, where the user can copy their own
    user id out of the client in two clicks."""
    import remote
    try:
        rows = remote.add_paired(
            str(p.get("platform") or ""), str(p.get("id") or ""),
            str(p.get("label") or ""))
    except ValueError as e:
        return {"paired": remote.paired(str(p.get("platform") or "")),
                "error": str(e)}
    return {"paired": rows, "error": None}


async def _h_remote_sync(_p: dict) -> dict:
    return await _remote_sync()


# ── AI agents (MCP) ──────────────────────────────────────────────────


async def _mcp_sync() -> dict:
    import mcp_server
    return await mcp_server.sync(merge_with_defaults(dict(STATE.cfg or {})))


async def _h_mcp_status(_p: dict) -> dict:
    import mcp_rails
    import mcp_server
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    st = mcp_server.status(cfg)
    # Today's agent loss (every agent together, in the trade mode in force):
    # what the daily loss stop compares against. It marks open positions at
    # the bid, so it can read books — bounded, and None when unreadable.
    mode = mcp_rails.trade_mode(cfg)
    if mode in ("paper", "live"):
        try:
            loss = await asyncio.wait_for(mcp_rails.day_loss(mode, cfg), 8.0)
            st["lossTodayUsd"] = (None if loss.get("positionsUnreadable")
                                  else loss.get("lossUsd"))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"mcp status: loss read failed: {e}")
    return st


async def _h_mcp_sync(_p: dict) -> dict:
    return await _mcp_sync()


def _agent_id_arg(p: dict) -> str:
    """An agent id from the renderer, validated, never defaulted to another
    agent by accident: absent means Default, malformed is refused."""
    import mcp_agents
    aid = str((p or {}).get("agentId") or mcp_agents.DEFAULT_ID)
    if aid != mcp_agents.DEFAULT_ID and not mcp_agents.valid_id(aid):
        raise ValueError("bad agent id")
    return aid


async def _h_mcp_rotate_token(p: dict) -> dict:
    import mcp_agents
    import mcp_server
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    aid = _agent_id_arg(p)
    if mcp_agents.get(cfg, aid) is None:
        raise ValueError("no such agent")
    mcp_server.rotate_token(aid)
    return mcp_server.status(cfg)


async def _h_mcp_http_snippet(p: dict) -> dict:
    """curl + Python for the HTTP API, carrying one agent's token. The ONLY
    RPC whose answer holds a token: Electron main puts the text on the
    clipboard and hands the renderer {ok} — the renderer never sees it."""
    import mcp_agents
    import mcp_server
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    aid = _agent_id_arg(p)
    agent = mcp_agents.get(cfg, aid)
    if agent is None:
        raise ValueError("no such agent")
    token = await asyncio.to_thread(mcp_server.ensure_token, aid)
    return {"text": mcp_server.http_snippet(int(cfg.get("mcp_port") or 47635), token,
                                            agent["name"])}


async def _h_mcp_seen(_p: dict) -> dict:
    """Agent clients seen this session. Memory only: no disk, no network, so
    onboarding may poll it."""
    import mcp_server
    return mcp_server.seen()


async def _h_mcp_agent_close_paper(p: dict) -> dict:
    """The USER closes one agent's paper positions at the bid (e.g. after
    deleting or switching it off). Not an MCP tool: no agent can reach it."""
    import mcp_rails
    return await mcp_rails.close_agent_paper(_agent_id_arg(p))


async def _h_paper_status(_p: dict) -> dict:
    """The ACCOUNT's paper book (the terminal's 'manual' book) for the
    Paper | Live panel."""
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    with terminal_paper.using_book("manual"):
        pf = await terminal_paper.portfolio(cfg)
        resting = sum(1 for o in terminal_paper.orders(500)["orders"]
                      if o["status"] in ("resting", "partial"))
    try:
        has_wallet = bool(polymarket_auth.credentials_present())
    except Exception:  # noqa: BLE001
        has_wallet = False
    return {
        "accountMode": cfg.get("account_mode"),
        "bankrollUsd": pf.get("bankrollUsd"),
        "nextBankrollUsd": cfg.get("terminal_paper_bankroll_usd"),
        "cashUsd": pf.get("cashUsd"),
        "equityUsd": pf.get("equityUsd"),
        "realizedPnlUsd": pf.get("realizedPnlUsd"),
        "openPositions": len(pf.get("positions") or []),
        "restingOrders": resting,
        "hasWalletKey": has_wallet,
        "fetchedAt": pf.get("fetchedAt"),
    }


async def _h_paper_reset(_p: dict) -> dict:
    """Wipe the account's paper book back to its bankroll. Agents' own paper
    books are untouched (mcpPaper resets those)."""
    import terminal_paper
    with terminal_paper.using_book("manual"):
        res = terminal_paper.reset()
    return {"ok": True, **res}


# ── Connection health ────────────────────────────────────────────────────
# One list of every connection with a fix for each broken one. `deep` is set
# only by the "Run checks" click: the only path here that touches the network
# (a signed CLOB read, the selected AI provider's free key check, a loopback
# call to our own HTTP API). The open-time run reads local state only.


async def _health_wallet(deep: bool, cfg: dict) -> dict:
    import health
    from config import account_live
    st = await asyncio.to_thread(polymarket_auth.credentials_status)
    live = account_live(cfg)
    if not (deep and st.get("hasWalletKey")):
        return health.check_wallet(st, STATE.auth_ok, account_live=live)
    try:
        # The SAME test the wallet screen runs, so green here means what a
        # green "Test" there means.
        await asyncio.wait_for(_h_testCredentials({}), 45.0)
    except Exception as e:  # noqa: BLE001
        return health.check_wallet(st, STATE.auth_ok, account_live=live, probed=True,
                                   probe_ok=False, probe_error=e)
    return health.check_wallet(st, STATE.auth_ok, account_live=live, probed=True,
                               probe_ok=True)


async def _health_ai(deep: bool, cfg: dict) -> list:
    """Guarded throughout: the provider check is ai_analysis.check_provider
    (Settings' "Test connection", free endpoints only) when this build has
    it, else ai_providers.check, else no probe at all. Only the SELECTED
    provider is ever contacted."""
    import health
    try:
        import ai_providers as prov_mod  # type: ignore
    except Exception:  # noqa: BLE001
        prov_mod = None
    import ai_analysis
    import ai_secrets
    providers = list(getattr(prov_mod, "PROVIDERS", None) or getattr(ai_analysis, "PROVIDERS", ()))
    active = str(cfg.get("ai_provider") or (providers[0] if providers else "anthropic"))
    # The free key/server check, whichever shape this build has:
    # check_provider(provider, cfg) or check(provider, key, model).
    check = None
    _cp = (getattr(ai_analysis, "check_provider", None)
           or getattr(prov_mod, "check_provider", None))
    if callable(_cp):
        # Settings' own "Test connection" (free endpoints only), so a green
        # row here means what a green test there means.

        def check(p, c):  # noqa: F811
            return _cp(p, c)
    elif prov_mod is not None and callable(getattr(prov_mod, "check", None)):
        _ck = prov_mod.check

        def check(p, c):  # noqa: F811
            return _ck(p, ai_secrets.load_key(p) if ai_secrets.has_key(p) else None,
                       str(c.get("ai_model") or "") or None)
    can_probe = check is not None

    def _is_local(p: str) -> bool:
        nk = getattr(ai_analysis, "needs_key", None) or getattr(prov_mod, "needs_key", None)
        if callable(nk):
            return not nk(p)
        caps = getattr(prov_mod, "capabilities", None)
        if callable(caps):
            return not caps(p).get("needsKey", True)
        return False
    out = []
    for prov in providers:
        try:
            local = _is_local(prov)
        except Exception:  # noqa: BLE001
            local = False
        if local and prov != active:
            continue
        try:
            has = True if local else bool(ai_secrets.has_key(prov))
        except Exception:  # noqa: BLE001
            has = False
        needed = prov == active and bool(cfg.get("autopilot_enabled"))
        if not (deep and has and can_probe):
            out.append(health.check_ai(prov, active=prov == active, has_key=has,
                                       can_probe=can_probe, local=local, needed=needed))
            continue
        if prov != active:
            # Only the provider selected in Settings is ever contacted.
            out.append(health.check_ai(prov, active=False, has_key=True, can_probe=True,
                                       local=local, idle=True))
            continue
        try:
            import inspect
            if inspect.iscoroutinefunction(check):
                res = await asyncio.wait_for(check(prov, cfg), 30.0)
            else:
                res = await asyncio.wait_for(asyncio.to_thread(check, prov, cfg), 30.0)
            out.append(health.check_ai(prov, active=True, has_key=True, can_probe=True,
                                       probed=True, local=local,
                                       probe=res if isinstance(res, dict) else {}))
        except Exception as e:  # noqa: BLE001
            out.append(health.check_ai(prov, active=True, has_key=True, can_probe=True,
                                       probed=True, probe_error=e, local=local))
    return out


async def _health_agents(deep: bool, cfg: dict) -> list:
    import health
    import mcp_agents
    import mcp_server
    st = await asyncio.to_thread(mcp_server.status, cfg)
    seen = mcp_server.seen().get("clients") or []
    st["lastSeen"] = {"client": seen[0]["client"], "at": seen[0]["at"]} if seen else None
    rows = [health.check_mcp(st)]
    probe = None
    if deep and st.get("enabled") and st.get("httpEnabled") and st.get("running"):
        # Probe as an agent the listener will accept: the first switched-on
        # agent with a token (a switched-off agent's token would read as a
        # broken API).
        token = None
        for a in mcp_agents.agents(cfg):
            if a.get("enabled", True):
                token = await asyncio.to_thread(mcp_server.load_token, a["id"])
                if token:
                    break
        probe = (await health.probe_http(int(st["port"]), token) if token
                 else {"ok": False, "status": 401, "error": "no token"})
    rows.append(health.check_http(st, probed=probe is not None, probe=probe))
    return rows


def _clob_feed_stats() -> dict | None:
    try:
        import clob_ws
        f = clob_ws.feed
        running = f.is_running() and f.connected
        now = time.monotonic()
        ages = [now - float(b.get("ts") or 0.0) for b in list(getattr(f, "_books", {}).values())]
        fresh = sum(1 for a in ages if a < 60)
        return {"connected": running, "books": fresh,
                "lastMsgAgeSec": round(min(ages), 1) if ages and running else None}
    except Exception:  # noqa: BLE001
        return None


def _health_local(cfg: dict) -> list:
    import health
    import remote
    from config import engine_on
    rows = []
    try:
        import autopilot  # type: ignore
        ap = autopilot.status(cfg)
    except Exception:  # noqa: BLE001
        ap = None
    rows.append(health.check_autopilot(ap))
    rs = remote.status()
    for which in ("discord", "telegram"):
        d = rs.get(which) or {}
        rows.append(health.check_remote(
            which, enabled=bool(cfg.get("remote_enabled")) and bool(cfg.get(f"remote_{which}_enabled")),
            has_token=bool(d.get("hasToken")), paired=bool(d.get("paired")),
            bot={"connected": bool(d.get("connected")), "running": bool(d.get("connected")),
                 "lastError": d.get("lastError"),
                 "botName": d.get("botUsername")}))
    needed = any(engine_on(cfg, k) for k in ("enable_trading", "crypto15m_enabled",
                                             "copy_enabled", "scripts_live_enabled"))
    rows.append(health.check_feed(_clob_feed_stats(), needed=needed))
    try:
        import spot_ws
        spot = spot_ws.stats()
    except Exception:  # noqa: BLE001
        spot = None
    rows.append(health.check_feed(spot, needed=engine_on(cfg, "crypto15m_enabled"),
                                  rid="spot", label="Crypto spot feed"))
    return rows


async def _h_health_check(p: dict) -> dict:
    import health
    deep = (p or {}).get("deep") is True
    cfg = merge_with_defaults(dict(STATE.cfg or {}))

    async def _guard(label: str, coro) -> list:
        # One broken check must not blank the panel: it becomes its own row.
        try:
            r = await coro
            return r if isinstance(r, list) else [r]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[health] {label} check failed: {type(e).__name__}: {e}")
            return [health.row(label.lower().replace(" ", "-"), label, health.FAIL,
                               f"The check itself failed: {type(e).__name__}.",
                               "Copy diagnostics from the Logs page if this persists.")]

    async def _local() -> list:
        return await asyncio.to_thread(_health_local, cfg)

    parts = await asyncio.gather(
        _guard("Wallet", _health_wallet(deep, cfg)),
        _guard("AI providers", _health_ai(deep, cfg)),
        _guard("AI agents", _health_agents(deep, cfg)),
        _guard("Local services", _local()),
    )
    rows = [r for part in parts for r in part]
    if deep:
        bad = [r["id"] for r in rows if r["status"] in (health.FAIL, health.WARN)]
        logger.info(f"[health] checks run: {len(rows)} rows, attention: {bad or 'none'}")
    return {"checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "deep": deep, "rows": rows, "counts": health.summarize(rows)}


async def _h_mcp_paper(p: dict) -> dict:
    """One agent's paper account, for the AI Agents page."""
    import mcp_agents
    import terminal_paper
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    with terminal_paper.using_book(mcp_agents.paper_book(_agent_id_arg(p))):
        if (p or {}).get("reset"):
            terminal_paper.reset()
        return {"portfolio": await terminal_paper.portfolio(cfg),
                "orders": terminal_paper.orders(int((p or {}).get("limit") or 50))}


async def _h_mcp_decide(p: dict) -> dict:
    """The user's answer to a live agent order waiting for approval."""
    import mcp_rails
    try:
        rid = int((p or {}).get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "message": "No such agent order."}
    return await mcp_rails.decide(rid, bool((p or {}).get("approve")), via="app")


async def _h_mcp_settle_unknown(p: dict) -> dict:
    """The user settles one live agent order booked 'unknown': Polymarket is
    asked what became of it, and it is booked at that (mcp_rails.settle_unknown)."""
    import mcp_rails
    return await mcp_rails.settle_unknown((p or {}).get("id"))


async def _h_mcp_activity(p: dict) -> dict:
    """Every agent order (paper and live, sent, refused, queued) and the
    approvals waiting, newest first."""
    import mcp_rails
    limit = max(1, min(500, int((p or {}).get("limit") or 100)))
    return {"orders": mcp_rails.activity(limit), "pending": mcp_rails.pending()}


async def _h_ai_scoreboard(p: dict) -> dict:
    """The agents' forecast record against the market's own price."""
    import forecast_ledger
    return forecast_ledger.scoreboard(recent=max(1, min(200, int((p or {}).get("recent") or 40))))


async def _h_remote_test(_p: dict) -> dict:
    """Push a line to every paired account, so the user can confirm the round
    trip works before relying on it."""
    import remote
    n = await remote.push(
        "Test from Krypt PolyBot US. If you can read this, remote control is "
        "working. Send /help for commands.")
    return {"sent": n,
            "message": (f"Sent to {n} paired account(s)." if n else
                        "Nothing sent — no paired accounts, or no bot is "
                        "connected.")}


async def _h_ai_status(_p: dict) -> dict:
    import ai_analysis
    return ai_analysis.status(merge_with_defaults(dict(STATE.cfg or {})))


async def _h_ai_analyze(p: dict) -> dict:
    """Analyse one market with the configured provider.

    On-demand only — there is no scheduler, no tick and no engine path that
    reaches this, because every call spends the user's own provider credit.

    The ai_enabled gate lives in ai_analysis.analyze, next to the code that
    would do the sending, rather than being re-asserted here.
    """
    import ai_analysis
    import terminal
    cfg = STATE.cfg or {}
    # Ask before fetching. With the feature off the market read is a wasted
    # Gamma call against the terminal's own rate budget, and "AI analysis is
    # turned off" is the more actionable of the two reasons anyway. The
    # message itself still comes from analyze(), so it is defined once.
    if not ai_analysis.is_enabled(cfg):
        return await ai_analysis.analyze({}, cfg)
    cid = str(p.get("conditionId") or "").strip()
    m = await terminal.market(cid, include_raw=True) if cid else None
    if not m:
        return ai_analysis._fail(
            str(cfg.get("ai_provider") or "anthropic"), "", terminal._now_iso(),
            "That market could not be read, so there was nothing to analyse.",
            None)
    return await ai_analysis.analyze(m, cfg)


async def _h_ai_set_key(p: dict) -> dict:
    """Verify a key BEFORE storing it, so a typo fails on the setup screen
    rather than as a feature that silently never works. Same rule as the
    chat-bot tokens."""
    import ai_analysis
    import ai_secrets
    provider = str(p.get("provider") or "").strip().lower()
    key = str(p.get("key") or "").strip()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "provider": provider, "hasKey": False,
                "message": f"Unknown provider {provider!r}."}
    if not ai_secrets.stores_key(provider):
        # Ollama / LM Studio run on this machine and take no key; a key
        # "saved" for one would be a secret sitting in the store for nothing.
        return {"ok": False, "provider": provider, "hasKey": False,
                "message": f"{provider} runs on this machine and takes no key."}
    if not key:
        ai_secrets.clear_key(provider)
        logger.info(f"[ai] {provider} API key cleared")
        return {"ok": True, "provider": provider, "hasKey": False,
                "message": "Key cleared.", "models": [], "masked": None}

    previous = ai_secrets.load_key(provider)
    ai_secrets.save_key(provider, key)
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    # The key check on a FREE endpoint (OpenRouter's /key, the others' model
    # listing): never a billed call. `reason == "model"` means the key is
    # fine and only the selected model is not one it can use.
    res = await asyncio.to_thread(ai_analysis.check_provider, provider, cfg)
    if not res.get("ok") and res.get("reason") != "model":
        # Put back whatever worked before rather than leaving the user with
        # neither the old key nor a working new one.
        if previous:
            ai_secrets.save_key(provider, previous)
        else:
            ai_secrets.clear_key(provider)
        return {"ok": False, "provider": provider,
                "hasKey": ai_secrets.has_key(provider),
                "message": res.get("message") or f"{provider} rejected that key."}
    # Never the key itself, only that one was set.
    logger.info(f"[ai] {provider} API key saved")
    models = res.get("models") or []
    return {
        "ok": True,
        "provider": provider,
        "hasKey": True,
        "message": f"Key verified. {len(models)} models available.",
        "models": models,
        "masked": ai_secrets.masked(provider),
    }


async def _h_ai_list_models(p: dict) -> dict:
    """{ok, provider, models: [id], modelInfo: [{id, tools, context,
    maxContext, loaded}], message?}. Free listing endpoints only; local
    providers say honestly that their server is not running."""
    import ai_analysis
    import ai_providers
    provider = str(p.get("provider") or "").strip().lower()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "provider": provider,
                "message": f"Unknown provider {provider!r}.", "models": [], "modelInfo": []}
    try:
        models = await ai_analysis.list_models(provider)
    except Exception as e:  # noqa: BLE001
        reason = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
        return {"ok": False, "provider": provider, "message": reason,
                "models": [], "modelInfo": []}
    info = [m.public() for m in ai_providers.cached_models(provider) if m.id in models]
    return {"ok": True, "provider": provider, "models": models, "modelInfo": info}


async def _h_ai_check_provider(p: dict) -> dict:
    """Settings' "Test connection". The renderer names a provider id; the
    host, the key and the endpoint are all chosen here. Free endpoints only
    (model listings), so pressing it never bills the user."""
    import ai_analysis
    provider = str((p or {}).get("provider") or "").strip().lower()
    if provider not in ai_analysis.PROVIDERS:
        return {"ok": False, "provider": provider,
                "message": f"Unknown AI provider: {provider or '—'}"}
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    res = await asyncio.to_thread(ai_analysis.check_provider, provider, cfg)
    logger.info(f"[ai] connection check {provider}: {'ok' if res.get('ok') else 'failed'}")
    return res


async def _h_autopilot_status(_p: dict) -> dict:
    """kryptrader's AutopilotStatus: the run state, budgets, today's usage,
    the agent it runs as, and the last 30 runs."""
    import autopilot
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    st = await asyncio.to_thread(autopilot.status, cfg)
    st["runs"] = await asyncio.to_thread(autopilot.runs, 30)
    return st


async def _h_autopilot_run_now(_p: dict) -> dict:
    """A run on the user's click. Started in the background: the renderer gets
    an answer now and watches the run on the status poll. Every refusal —
    deleted/disabled agent, missing key, a budget, a model that cannot call
    tools — comes back HERE as the reason, before anything is recorded."""
    import autopilot
    cfg = merge_with_defaults(dict(STATE.cfg or {}))
    if autopilot.STATE.running:
        return {"ok": False, "message": "Autopilot is already running."}
    why = autopilot.blocked_reason(cfg)
    if not why:
        why = await asyncio.to_thread(autopilot.tool_refusal, cfg)
    if why:
        autopilot.STATE.last_error = why
        return {"ok": False, "message": why}
    t = asyncio.create_task(autopilot.run_once(cfg, "manual"))
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return {"ok": True, "message": "Autopilot run started."}


_HANDLERS = {
    "ping": _h_ping,
    "aiStatus": _h_ai_status,
    "aiAnalyze": _h_ai_analyze,
    "aiSetKey": _h_ai_set_key,
    "aiListModels": _h_ai_list_models,
    "aiCheckProvider": _h_ai_check_provider,
    "autopilotStatus": _h_autopilot_status,
    "autopilotRunNow": _h_autopilot_run_now,
    "crypto15m": _h_crypto15m,
    "crypto15mStatus": _h_crypto15mStatus,
    "tradingStatus": _h_trading_status,
    "c15History": _h_c15_history,
    "c15Backtest": _h_c15_backtest,
    "c15ParlayGenerate": _h_c15_parlay_generate,
    "c15ParlayStatus": _h_c15_parlay_status,
    "c15ParlayArm": _h_c15_parlay_arm,
    "mainBacktest": _h_main_backtest,
    "collectionStats": _h_collection_stats,
    "historyStart": _h_history_start,
    "historyStatus": _h_history_status,
    "historyCancel": _h_history_cancel,
    "exportResearch": _h_export_research,
    "scriptsList": _h_scripts_list,
    "scriptSave": _h_script_save,
    "scriptDelete": _h_script_delete,
    "scriptSetEnabled": _h_script_set_enabled,
    "scriptSetAssets": _h_script_set_assets,
    "scriptSetDryRun": _h_script_set_dry_run,
    "scriptShadowOrders": _h_script_shadow_orders,
    "scriptValidate": _h_script_validate,
    "scriptBacktest": _h_script_backtest,
    "scriptContextPack": _h_script_context_pack,
    "scriptApiDocs": _h_script_api_docs,
    "polymarketUrl": _h_polymarketUrl,
    "terminalDiscover": _h_terminal_discover,
    "terminalSearch": _h_terminal_search,
    "terminalMarket": _h_terminal_market,
    "terminalBook": _h_terminal_book,
    "terminalHistory": _h_terminal_history,
    "terminalResolution": _h_terminal_resolution,
    "terminalMicro": _h_terminal_micro,
    "terminalRelated": _h_terminal_related,
    "terminalCrossVenue": _h_terminal_cross_venue,
    "remoteStatus": _h_remote_status,
    "remoteSetToken": _h_remote_set_token,
    "remotePairCode": _h_remote_pair_code,
    "remoteAddPaired": _h_remote_add_paired,
    "remoteUnpair": _h_remote_unpair,
    "remoteSync": _h_remote_sync,
    "mcpStatus": _h_mcp_status,
    "mcpSync": _h_mcp_sync,
    "mcpRotateToken": _h_mcp_rotate_token,
    # No RPC returns a bare token: the renderer never holds one. The HTTP
    # snippet (which must carry it) goes backend → main → clipboard.
    "mcpHttpSnippet": _h_mcp_http_snippet,
    "mcpSeen": _h_mcp_seen,
    "mcpAgentClosePaper": _h_mcp_agent_close_paper,
    "paperStatus": _h_paper_status,
    "paperReset": _h_paper_reset,
    "healthCheck": _h_health_check,
    "mcpPaper": _h_mcp_paper,
    "mcpDecide": _h_mcp_decide,
    "mcpSettleUnknown": _h_mcp_settle_unknown,
    "mcpActivity": _h_mcp_activity,
    "aiScoreboard": _h_ai_scoreboard,
    "remoteTest": _h_remote_test,
    "terminalHealth": _h_terminal_health,
    "terminalWatchlist": _h_terminal_watchlist,
    "terminalPreview": _h_terminal_preview,
    "terminalPlace": _h_terminal_place,
    "terminalCancel": _h_terminal_cancel,
    "terminalOrders": _h_terminal_orders,
    "terminalPortfolio": _h_terminal_portfolio,
    "terminalSettled": _h_terminal_settled,
    "terminalFills": _h_terminal_fills,
    "terminalOrderLog": _h_terminal_order_log,
    "terminalReconcile": _h_terminal_reconcile,
    "terminalStanding": _h_terminal_standing,
    "terminalFlattenWallet": _h_terminal_flatten_wallet,
    "terminalSellPreview": _h_terminal_sell_preview,
    "terminalSellPosition": _h_terminal_sell_position,
    "terminalPaperPlace": _h_terminal_paper_place,
    "terminalPaperPortfolio": _h_terminal_paper_portfolio,
    "terminalPaperOrders": _h_terminal_paper_orders,
    "terminalPaperCancel": _h_terminal_paper_cancel,
    "terminalPaperReset": _h_terminal_paper_reset,
    "setConfig": _h_setConfig,
    "setCredentials": _h_setCredentials,
    "clearCredentials": _h_clearCredentials,
    "credentialStatus": _h_credentialStatus,
    "testCredentials": _h_testCredentials,
    "account": _h_account,
    "accountRefresh": _h_accountRefresh,
    "pnlSeries": _h_pnlSeries,
    "positions": _h_positions,
    "signals": _h_signals,
    "scannerStats": _h_scannerStats,
    "cancelAllOpen": _h_cancelAllOpen,
    "killSwitch": _h_killSwitch,
    "killSwitchClear": _h_killSwitchClear,
    "killSwitchStatus": _h_killSwitchStatus,
    "alerts": _h_alerts,
    "flatten": _h_flatten,
    "runOnce": _h_runOnce,
    "pause": _h_pause,
    "shutdown": _h_shutdown,
    "botRuns": _h_botRuns,
    "factoryReset": _h_factoryReset,
    "clearHistory": _h_clearHistory,
}


def _sync_terminal_offline() -> None:
    """Push the user's offline-mode switch into the terminal provider layer.

    Done on dispatch rather than only on setConfig so the switch is honoured
    even if the backend restarted, or the config arrived by some path that did
    not go through setConfig. Cheap: a dict read and a bool assignment.
    """
    try:
        import terminal
        cfg = STATE.cfg or {}
        terminal.set_offline(bool(cfg.get("terminal_offline_mode", False)))
    except Exception:
        pass


# RPCs after which the credential store holds a different set of secrets:
# the log scrubber re-reads it so a new key is redacted from the next line on.
_SECRET_CHANGING_RPCS = frozenset({
    "setCredentials", "clearCredentials", "testCredentials", "aiSetKey",
    "remoteSetToken", "mcpRotateToken", "mcpHttpSnippet", "factoryReset",
})


def _refresh_log_scrubber() -> None:
    try:
        import logscrub
        logscrub.refresh_known_secrets()
    except Exception:  # noqa: BLE001 — never let scrubber upkeep break an RPC
        pass


async def _dispatch_request(req: dict) -> None:
    rid = req.get("id", "")
    method = req.get("method", "")
    params = req.get("params") or {}
    if method.startswith("terminal"):
        _sync_terminal_offline()
    h = _HANDLERS.get(method)
    if not h:
        await respond_err(rid, f"unknown method: {method}")
        return
    try:
        result = await h(params)
        if method in _SECRET_CHANGING_RPCS:
            _refresh_log_scrubber()
        await respond_ok(rid, result)
    except Exception as e:
        # The parent process surfaces the failure via the RPC response
        # (toast / inline error). Logging at WARNING here is just noise —
        # demote so the renderer log view stays clean. Real bugs still
        # show up in backend.log under <userData>/logs/.
        logger.debug(
            f"RPC {method} failed: {e}\n{traceback.format_exc(limit=3)}"
        )
        await respond_err(rid, f"{type(e).__name__}: {e}")


# ────────────────────────────────────────────────────────────────────
# Reader / shutdown
# ────────────────────────────────────────────────────────────────────


async def _stdin_reader() -> None:
    loop = asyncio.get_event_loop()

    def _readline() -> str:
        return sys.stdin.readline()

    while True:
        line = await loop.run_in_executor(None, _readline)
        if not line:
            await asyncio.sleep(0.1)
            await _shutdown()
            return
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue
        if req.get("type") != "rpc":
            continue
        # Keep a strong reference: the event loop only holds a WEAK ref to
        # tasks, so a bare create_task can be garbage-collected mid-flight —
        # silently dropping the RPC (the host's ping watchdog would then
        # force-restart a perfectly healthy backend).
        t = asyncio.create_task(_dispatch_request(req))
        _bg_tasks.add(t)
        t.add_done_callback(_bg_tasks.discard)


_shutting_down = False


async def _shutdown() -> None:
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    try:
        await _stop_loop()
    except Exception:
        pass
    # Cancel any resting GTC orders we placed BEFORE we tear down the API
    # clients. Otherwise a live-but-unfilled order left on Polymarket's book
    # by this process can fill later, unmanaged, and get re-imported by a
    # future reconcile with no risk controls. Main engine only (bot_positions);
    # crypto15m uses marketable FAK and never rests.
    try:
        if STATE.auth_ok:
            n = await trader.cancel_all_resting_orders("shutdown")
            if n:
                logger.info(f"Shutdown: canceled {n} resting order(s)")
    except Exception as e:
        logger.warning(f"shutdown cancel-resting failed: {e}")
    try:
        import bot_discord
        import bot_telegram
        await bot_discord.stop()
        await bot_telegram.stop()
    except Exception:
        pass
    try:
        import mcp_server
        await mcp_server.stop()
    except Exception:
        pass
    try:
        import terminal
        await terminal.stop_recorder()
    except Exception:
        pass
    try:
        await spot_ws.stop()
    except Exception:
        pass
    try:
        await index_feed.stop()
    except Exception:
        pass
    # Finalise this session's bot_run so the History → Run History
    # tab shows it as a completed (not still-running) row.
    try:
        if STATE.active_run_id:
            with db.get_db() as conn:
                db.end_bot_run(conn, STATE.active_run_id)
            STATE.active_run_id = 0
    except Exception:
        pass
    try:
        await polymarket_api.close_clients()
    except Exception:
        pass
    try:
        await crypto15m.close_clients()
    except Exception:
        pass
    try:
        await user_ws.feed.stop()
    except Exception:
        pass
    try:
        import clob_ws
        await clob_ws.feed.stop()
    except Exception:
        pass
    # Release the wallet instance-lock we claimed for entry gating. Nothing
    # ever released it, so every restart (update, crash-watchdog respawn)
    # made the NEW process wait out the full 120s staleness window before
    # entering — on the 5m series that's a whole missed window per restart.
    try:
        import instance_lock
        for env_name in ("mainnet", "testnet"):
            try:
                addr = polymarket_auth.trading_address(env_name)
                if addr:
                    instance_lock.release(addr)
            except Exception:
                pass
    except Exception:
        pass
    await emit_event("backend:shutdown", {})
    sys.stdout.flush()
    # Allow a tick for the parent to drain stdout, then exit.
    await asyncio.sleep(0.1)
    os._exit(0)


# ────────────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────────────


async def _startup_authed_sync() -> tuple[Any, Any]:
    """The authed startup round trips, in the fewest serial hops that are safe.

    Order is NOT free here:
      • cancel-all-resting FIRST, alone. Cancelling a resting GTC both takes it
        off the book and releases its collateral, so the two calls after it
        depend on it having finished — the reconcile would otherwise inherit a
        still-resting stale order as an unmanaged external position, and the
        balance would report collateral that is about to come back as cash.
      • reconcile + refresh_balance are independent of EACH OTHER (one reads
        /portfolio/positions and writes local rows, the other reads /balance +
        /value; neither consumes the other's result), so they go out together
        — four serial round trips at startup become three.

    Returns the two gathered results (an exception instance instead of a value
    for whichever failed); the caller keeps each one's own error handling, so a
    failed balance can't swallow the reconcile's events or vice versa.
    """
    try:
        n = await trader.cancel_all_resting_orders("startup stale-order sweep")
        if n:
            logger.info(f"Startup: canceled {n} stale resting order(s)")
    except Exception as e:
        logger.warning(f"startup cancel-resting failed: {e}")

    reconcile_res, balance_res = await asyncio.gather(
        trader.reconcile_positions_with_polymarket(),
        trader.refresh_balance(STATE.cfg, force=True),
        return_exceptions=True,
    )
    return reconcile_res, balance_res


async def _main() -> None:
    # Force stdout/stdin to UTF-8 (Windows defaults to cp1252).
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    STATE.started_at = datetime.now(timezone.utc).isoformat()
    db.init_db()
    # Let the crypto engine ask us to durably turn it off when its overall
    # take-profit target is hit (persist the toggle + update the UI).
    crypto15m_trader.set_auto_off_callback(_on_crypto15m_auto_off)
    # Script engine events (auto-disable, log lines) ride the same bus.
    script_engine.set_event_callback(emit_event)
    # Agent tool calls → the AI Agents page / Hub (mcp:toolCall), throttled
    # and whitelisted in mcp_server; and every stored secret into the log
    # scrubber before the first line that could carry one.
    import mcp_server as _mcp_server
    _mcp_server.set_event_callback(emit_event)
    _refresh_log_scrubber()
    logger.info("Krypt PolyBot US backend starting")

    # ── Serve RPCs from HERE on. ──────────────────────────────────────
    # The reader used to be awaited LAST, after credential migration +
    # auth verify + the eager reconcile — so every early RPC (the host's
    # setConfig push, a user opening a page right after launch) buffered
    # unanswered in the pipe for the whole boot sequence. On a slow disk /
    # flaky network that stretch exceeded the host's 30s RPC timeout
    # ("RPC timeout" right after installing a build). The DB is
    # initialized above, handlers tolerate auth-not-yet-ready
    # (STATE.auth_ok is False until _establish_auth lands), and an early
    # setConfig now applies BEFORE the trading loop starts instead of
    # after — strictly better ordering.
    stdin_task = asyncio.create_task(_stdin_reader(), name="stdin_reader")

    # ── Migrate legacy creds BEFORE we tell the frontend we're ready. ──
    # The dashboard's first credential read happens immediately after
    # `backend:ready`, so if migration runs after the event the user sees
    # a flash of "API keys not configured" even though they have keys
    # saved.
    active_env = STATE.cfg.get("network", "mainnet")
    try:
        polymarket_auth.set_env(active_env)
    except Exception:
        pass
    try:
        if polymarket_auth.migrate_legacy_credentials(active_env):
            logger.info(f"Migrated legacy credentials → {active_env}")
    except Exception as e:
        logger.warning(f"legacy credential migration failed: {e}")

    # If credentials are already saved (returning user), prime + verify.
    # Resilient: retries and treats cred-derivation (not the flaky balance RPC)
    # as the connectivity signal, so a transient blip doesn't latch "disconnected".
    if polymarket_auth.credentials_present(active_env):
        STATE.auth_ok = await _establish_auth()
        if STATE.auth_ok:
            logger.info("Saved credentials verified")

    # Emit ready + auth + credentials together so the renderer hydrates
    # in a consistent order and never has to guess.
    await emit_event("backend:ready", {"startedAt": STATE.started_at})
    # A history download the last run didn't finish carries on by itself.
    try:
        import history_download
        history_download.resume_if_pending(STATE.cfg or {})
    except Exception as e:  # noqa: BLE001: optional, and never worth failing a boot
        logger.warning(f"history download resume failed: {e}")
    await emit_event("backend:authChanged", {"authOk": STATE.auth_ok})
    try:
        await emit_event(
            "credentials:changed", polymarket_auth.credentials_status_all(),
        )
    except Exception:
        pass

    # The kill switch and the NO-side check outlive a restart. A latched kill
    # switch also keeps the engines paused, or they'd spin on refused orders.
    try:
        polymarket_api.load_venue_state()
        if polymarket_api.kill_switch_status():
            STATE.paused = True
            logger.warning(f"kill switch still engaged: {polymarket_api.kill_switch_status()}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"could not restore venue safety state: {e}")

    # ── Reconcile open positions BEFORE we open the run + start the
    # loop. This way the run's `start_*` snapshot already reflects all
    # truly-open Polymarket positions (rescued/resurrected/imported), and
    # the dashboard hydrates with the correct open-position count
    # instead of flashing "2 open" while it waits for the next periodic
    # reconcile. ──────────────────────────────────────────────────────
    # First, cancel any GTC orders still resting from a PREVIOUS run that
    # exited uncleanly (crash / lost placement response / hard kill). If we
    # didn't, the reconcile below would inherit them as unmanaged external
    # positions with no risk controls the moment they fill. Main engine only;
    # crypto15m FAK orders never rest.
    reconcile_res: Any = None
    balance_res: Any = None
    if STATE.auth_ok:
        reconcile_res, balance_res = await _startup_authed_sync()

    if STATE.auth_ok:
        try:
            if isinstance(reconcile_res, BaseException):
                raise reconcile_res
            summary, changed = reconcile_res
            if any(summary.values()):
                logger.info(f"Eager startup reconcile: {summary}")
            await emit_event("backend:reconciled", summary)
            for row in changed:
                await emit_event("position:update", _position_row_to_js(row))
        except Exception as e:
            logger.warning(f"eager startup reconcile failed: {e}")

    # ── Open a fresh bot_run row for this session. ────────────────────
    # We anchor session P&L (the headline number on the dashboard) to
    # the run's `start_total_usd`, so this needs to happen before the
    # scanner loop starts pushing snapshots. The lifetime baselines below are
    # read AFTER the reconcile above so a startup settlement counts once.
    if STATE.auth_ok:
        try:
            if isinstance(balance_res, BaseException):
                raise balance_res
            cents, port = balance_res
            env_now = polymarket_auth.get_env()
            with db.get_db() as conn:
                stats = db.aggregate_stats(conn, env_now)
                STATE.active_run_id = db.start_bot_run(
                    conn,
                    env=env_now,
                    cash_usd=cents / 100.0,
                    portfolio_usd=port / 100.0,
                    # Capture lifetime baselines so per-run trade counts
                    # show DELTA, not lifetime, on the run history page.
                    lifetime_trades=int(stats.get("total_opened") or 0),
                    lifetime_wins=int(stats.get("wins") or 0),
                    lifetime_losses=int(stats.get("losses") or 0),
                )
            logger.info(
                f"Bot run #{STATE.active_run_id} started "
                f"(env={env_now}, "
                f"start_total=${(cents + port) / 100.0:.2f})"
            )
        except Exception as e:
            logger.warning(f"could not open bot_run: {e}")

    await _start_loop()
    try:
        await stdin_task
    except Exception as e:
        logger.error(f"reader crashed: {e}")
    finally:
        await _shutdown()


# The official SDK's hash for its documented createOrders example (verified
# against polymarket-client 0.12's msgpack + keccak path).


def _selftest() -> int:
    """Frozen-build smoke test: confirm the core deps AND the deposit-wallet
    order signer are importable + functional in THIS build. Prints a short
    report; exit 0 = all good, 1 = something's missing (e.g. the PyInstaller
    bundle dropped a lazy-imported signing dependency → the app couldn't sign an order).
    Run:  krypt-polybot-us-backend(.exe) --selftest"""
    # Always in a throwaway profile. Without one the DB path falls back to
    # <bundle>/data, finds no DB, and restores the user's newest snapshot
    # from the survival vault INTO THE BUNDLE — which the installer build
    # then shipped (3.1.0's first build carried an 825 MB research DB). The
    # selftest needs no user data; it must never read or write any.
    import shutil
    import tempfile
    iso = tempfile.mkdtemp(prefix="kp-selftest-profile-")
    os.environ["KRYPT_POLYBOT_US_USERDATA"] = os.path.join(iso, "userdata")
    os.environ["KRYPT_POLYBOT_US_VAULT"] = os.path.join(iso, "vault")
    try:
        return _selftest_checks()
    finally:
        try:
            db.close_thread_connection()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(iso, ignore_errors=True)


def _selftest_checks() -> int:
    checks: list[tuple[str, bool, str]] = []

    def _try(name: str, fn) -> None:
        try:
            fn()
            checks.append((name, True, ""))
        except Exception as e:  # noqa: BLE001 — report every failure mode
            checks.append((name, False, f"{type(e).__name__}: {e}"))

    _try("import httpx", lambda: __import__("httpx"))
    _try("import cryptography", lambda: __import__("cryptography"))
    _try("Ed25519 request signing (offline)", polymarket_auth.verify_signer)
    _try("import us_api + order mapping", lambda: __import__("us_api").order_body(
        slug="selftest", side="no", action="buy", count=1, price=0.5, order_type="GTC"))
    # Crypto-engine deps: a frozen build that drops these passes signing but the
    # 15m engine (real-time book + MACD/RSI) silently breaks for users.
    _try("import websockets (CLOB WS feed)", lambda: __import__("websockets"))
    _try("import clob_ws", lambda: __import__("clob_ws"))
    _try("import indicators + compute", lambda: __import__("indicators").compute([1.0, 2.0]))
    # Sniper/backtest deps: spot_ws feeds the model live spot; replay is
    # lazy-imported by the backtest RPCs, so a bundle that drops it fails
    # only when the user presses "Run backtest" — catch it here instead.
    _try("import spot_ws (Coinbase spot feed)", lambda: __import__("spot_ws"))
    _try("import index_feed (settlement index)", lambda: __import__("index_feed").parse_series({}))
    _try("import replay (backtest engine)", lambda: __import__("replay"))
    # Parlay generator is lazy-imported by the c15Parlay* RPCs — same failure
    # mode as replay: a bundle that drops it only breaks when the user
    # generates/arms a schedule.
    _try("import parlay_generator", lambda: __import__("parlay_generator"))
    _try("import user_ws (order feed)", lambda: __import__("user_ws").UserFeed().handle({}))
    _try("model math (settlement sniper)", lambda: (
        __import__("crypto15m").model_up_prob(101.0, 100.0, 0.001, 5.0)))

    # User scripts: prove the frozen bundle can compile+run one, that a
    # runaway loop is still killed on the traced (backtest) path, and that the
    # risk audit — which is what replaced the language sandbox — actually
    # fires. A bundle whose audit silently returns nothing would leave the
    # Scripts page reporting "nothing alarming" about anything at all.
    def _script_runtime_check() -> None:
        ss = __import__("script_sandbox")
        good = ("def decide(ctx):\n"
                "    return {\"side\": \"up\", \"price\": \"ask\"}\n")
        mod = ss.CompiledScript("selftest", good)
        intent = mod.call("decide", {"minsLeft": 1.0})
        assert intent and intent.get("side") == "up", "decide() lost its intent"
        # Full Python: an import is now perfectly valid code.
        assert not ss.validate("import os\ndef decide(ctx):\n    return None"), \
            "validator rejected an import (scripts are unsandboxed now)"
        assert ss.validate("def nope(ctx):\n    return None\n"), \
            "validator accepted a script with no entry hook"
        try:
            bad = ss.CompiledScript(
                "selftest2",
                "def decide(ctx):\n"
                "    while True:\n        pass\n", traced=True)
            bad.call("decide", {}, budget_ms=50.0)
            raise AssertionError("budget did not kill a busy loop")
        except ss.ScriptBudgetExceeded:
            pass

    def _audit_check() -> None:
        sa = __import__("script_audit")
        clean = sa.audit(
            "def decide(ctx):\n    return None\n")
        assert clean["ok"] and not clean["findings"], \
            "audit flagged a plain strategy"
        evil = sa.audit(
            "import requests\n"
            "import polymarket_auth\n"
            "def decide(ctx):\n"
            "    requests.post('https://x.example', json=polymarket_auth.get_credentials())\n"
            "    return None\n")
        assert not evil["ok"] and evil["critical"] >= 3, \
            f"audit missed an exfiltration script: {evil}"
        assert "exfiltration" in evil["categories"], \
            "audit missed the wallet+network combination rule"

    _try("user scripts (compile / run / runaway guard)", _script_runtime_check)
    _try("script risk audit", _audit_check)
    _try("import script_backtest", lambda: __import__("script_backtest"))
    _try("import script_engine", lambda: __import__("script_engine"))
    _try("import script_audit", lambda: __import__("script_audit"))
    _try("import script_docs", lambda: __import__("script_docs"))

    # Manual terminal: both modules are LAZY-imported by their RPC handlers,
    # so PyInstaller's static analysis misses them and a bundle that dropped
    # them would fail only when the user opens the terminal. Catch it here.
    _try("import terminal (provider layer)", lambda: __import__("terminal"))
    _try("import terminal_trade (manual orders)",
         lambda: __import__("terminal_trade"))
    _try("import terminal_paper (paper trading)",
         lambda: __import__("terminal_paper"))
    _try("import kalshi (second venue, read-only)", lambda: __import__("kalshi"))
    _try("import remote (chat command brain)", lambda: __import__("remote"))
    _try("import bot_telegram", lambda: __import__("bot_telegram"))
    _try("import bot_discord", lambda: __import__("bot_discord"))
    _try("import bot_secrets", lambda: __import__("bot_secrets"))

    def _remote_auth_check() -> None:
        """The gate that decides whether a stranger can move your money.

        An unpaired sender must reach NO command, and remote trading must
        require BOTH switches. If either regresses the build stops here — this
        is not something to discover from a user's chat log.
        """
        r = __import__("remote")
        assert not r.is_allowed("discord", "999999"),             "an unpaired id was allowed"
        assert not r.is_allowed("telegram", ""), "an empty id was allowed"
        assert not r.trading_allowed({"remote_enabled": True}),             "remote trading allowed without its own switch"
        assert not r.trading_allowed({"remote_trading_enabled": True}),             "remote trading allowed while remote itself is off"
        assert r.trading_allowed(
            {"remote_enabled": True, "remote_trading_enabled": True})
        # An unknown platform must not be able to name a token file path.
        bs = __import__("bot_secrets")
        try:
            bs._path("../../evil")
            raise AssertionError("bot_secrets accepted an arbitrary platform")
        except ValueError:
            pass

    _try("remote auth + trading gate", _remote_auth_check)
    _try("import cross_venue", lambda: __import__("cross_venue"))

    def _cross_venue_polarity_check() -> None:
        """The guard that stops a fictional arbitrage.

        Caught live: "Fed INCREASE rates by 25bps" matched Kalshi's "CUT
        25bps" at high confidence and printed a 33c spread between two
        mutually exclusive markets. If the polarity rule regresses, that comes
        straight back — so the build stops here.
        """
        t = __import__("terminal")
        assert t.polarity("Fed will increase rates") == "up"
        assert t.polarity("Fed will cut rates") == "down"
        assert t.polarity("Bitcoin Up or Down") is None,             "a two-directional title must claim no direction"
        assert t._are_opposites(
            "Will the Fed increase rates by 25 bps",
            "Will the Federal Reserve Cut rates by 25bps"),             "increase/cut was not recognised as opposite"
        cv = __import__("cross_venue")
        score, _, _ = cv.score_pair(
            {"title": "Will the Fed increase rates by 25 bps", "closeTime": None},
            {"matchText": "Will the Federal Reserve Cut rates by 25bps Cut 25bps"})
        assert score == 0.0, f"opposite questions scored {score}"

    _try("cross-venue polarity guard", _cross_venue_polarity_check)

    # AI analysis: ai_analysis is lazy-imported by its RPC handlers, and it in
    # turn lazy-imports the two SDKs — so PyInstaller sees nothing statically
    # and a collect_all miss would ship a build whose only symptom is the
    # panel reporting "the anthropic package is not installed" on a machine
    # where it plainly is. The SDKs are checked BY NAME rather than through
    # ai_analysis, because ai_analysis imports fine without either of them.
    _try("import ai_secrets", lambda: __import__("ai_secrets"))
    _try("import ai_analysis", lambda: __import__("ai_analysis"))
    # The four HTTP providers and Autopilot are lazy-imported by RPC handlers
    # and the master loop only — a frozen build that dropped either would ship
    # a Settings screen that errors and a schedule that never fires.
    _try("import ai_providers", lambda: __import__("ai_providers"))
    _try("import autopilot", lambda: __import__("autopilot"))
    _try("import anthropic SDK", lambda: __import__("anthropic"))
    _try("import openai SDK", lambda: __import__("openai"))

    def _ai_no_estimate_check() -> None:
        """The rule the AI panel rests on, proven in THIS build.

        The model must never return a probability or a fair value, and the
        guarantee is structural: a closed schema with no numeric field, so
        the provider's own structured-output layer refuses one. If a numeric
        property is ever added, a language model's made-up price starts
        rendering next to real quotes — so the build stops here.
        """
        ai = __import__("ai_analysis")
        assert ai._SCHEMA.get("additionalProperties") is False,             "AI response schema is open — the model could invent a probability field"
        for name, spec in ai._SCHEMA["properties"].items():
            assert spec.get("type") not in ("number", "integer"),                 f"AI response schema exposes a numeric field: {name}"
        assert ai._SCHEMA["properties"]["lean"]["enum"] == [
            "cheap", "rich", "fair", "no_view"], "AI lean vocabulary changed"
        # The scrubber, both directions. A model estimate goes; a fact stays.
        gone, n = ai._strip_numeric_estimates("I'd put it at 70%.")
        assert n == 1 and not gone, "model estimate survived the scrubber"
        kept, n = ai._strip_numeric_estimates("CPI came in at 2.4% in August.")
        assert n == 0 and kept, "the scrubber ate a fact about the world"
        # Nothing about the account may reach a provider.
        payload = ai.build_payload({
            "question": "Q?", "_raw": {"description": "rules"},
            "myPosition": {"contracts": 500}, "walletAddress": "0xdead",
        })
        blob = __import__("json").dumps(payload).lower()
        for token in ("position", "wallet", "contracts", "balance"):
            assert token not in blob, f"AI payload leaked {token!r}"
        # Every provider, not just the two with strict SDK schemas: the reply
        # check rejects a number anywhere and any field the schema lacks.
        assert ai._conform({"lean": "cheap", "probability": "high"}),             "a reply with an extra field passed the schema check"
        assert ai._conform({"reasoning": ["x"], "lean": "cheap", "confidence": "low",
                            "summary": 0.7}), "a numeric field passed the schema check"
        assert ai._conform({"drivers": ["a", 70]}), "a number in a list passed the schema check"
        assert ai._conform({"summary": "s", "lean": "fair"}) is None,             "a clean reply failed the schema check"
        assert set(ai.PROVIDERS) == {"anthropic", "openai", "openrouter", "gemini",
                                     "ollama", "lmstudio"}, "AI provider set changed"
        # The schema the HTTP providers are asked for is the same closed one.
        gs = __import__("ai_providers").gemini_schema(ai._SCHEMA)
        for name, spec in gs["properties"].items():
            assert spec.get("type") not in ("number", "integer"),                 f"Gemini response schema exposes a numeric field: {name}"

    _try("AI analysis: no-estimate + no-account-leak", _ai_no_estimate_check)

    def _terminal_honesty_check() -> None:
        """The honest-null rule, proven in THIS build.

        These are the two behaviours the whole terminal rests on, and both
        have failed silently before in other products: a missing metric
        coerced to zero, and a filter that hides the row it could not
        evaluate. If either regresses, the build stops here.
        """
        t = __import__("terminal")
        assert t.num("") is None and t.num(None) is None and t.num("x") is None,             "num() coerced an unknown to a number"
        assert t.num(0) == 0.0, "num() lost a real zero"
        assert t.cents(0.42) == 42.0, "cents() mis-scaled a probability"

        rows = [
            {"volume24hUsd": 100.0, "category": "crypto", "title": "a"},
            {"volume24hUsd": None, "category": "crypto", "title": "b"},
            {"volume24hUsd": 5000.0, "category": None, "title": "c"},
        ]
        kept, skipped, excluded = t.apply_filters(rows, {"minVolume24hUsd": 50})
        assert len(kept) == 2 and skipped == 1 and excluded == 0,             f"numeric filter must SKIP an unknown, got {kept=} {skipped=}"
        kept, skipped, excluded = t.apply_filters(rows, {"categories": ["crypto"]})
        assert len(kept) == 2 and excluded == 1,             "categorical filter must EXCLUDE an unknown category"

        # A duplicate timestamp from a provider must never reach a chart.
        pts = t._dedupe_points(
            [{"t": 2, "p": 2.0}, {"t": 1, "p": 1.0}, {"t": 2, "p": 3.0}])
        assert [x["t"] for x in pts] == [1, 2], "history not sorted/collapsed"

    _try("terminal honest-null + filter rules", _terminal_honesty_check)

    def _mcp_check() -> None:
        """The AI-agent server's safety rails, proven in THIS build.

        A frozen build that dropped mcp_tools would answer every agent with
        "unknown tool"; one where the loopback checks regressed would let a
        web page in the browser reach the wallet.
        """
        srv = __import__("mcp_server")
        tools = __import__("mcp_tools")
        rails = __import__("mcp_rails")
        agents = __import__("mcp_agents")
        __import__("mcp_stdio")
        __import__("forecast_ledger")
        listed = {t["name"]: t for t in tools.list_tools()}
        for name in ("get_status", "get_my_agent", "record_forecast",
                     "place_order", "backtest_crypto15m", "save_script",
                     "pause_all_engines", "update_config", "research_data",
                     "close_position"):
            assert name in listed, f"MCP tool missing: {name}"
        assert len(listed) <= 40, "over 40 MCP tools — some clients drop the rest"
        assert "forecastId" in listed["place_order"]["inputSchema"]["properties"], \
            "place_order lost its forecast citation"
        assert srv._host_ok("127.0.0.1:47635") and srv._host_ok("localhost")
        assert not srv._host_ok("evil.example:47635"), "DNS-rebinding guard regressed"
        cfg = {"mcp_allow_live_switches": False, "mcp_scripts_enabled": False}
        for gate in ("switches", "scripts"):
            try:
                tools._require(cfg, gate)
            except tools.Refused:
                continue
            raise AssertionError(f"MCP '{gate}' gate is open with its switch off")
        # The global mode is the master, and the paper switch narrows live.
        a = agents.default_agent("live")
        assert agents.effective_mode("paper", a) == "paper"
        assert rails.trade_mode({"mcp_trade_mode": "live",
                                 "terminal_paper_mode": True}) == "paper"
        # An agent's rules can only narrow the global rails.
        loose = {**a, "rules": agents.clean_rules({"minEdgeCents": 0, "maxOrderUsd": 1e6})}
        er = agents.effective_rails({"maxOrderUsd": 25.0, "dailySpendUsd": 100.0,
                                     "minEdgeCents": 3.0}, loose)
        assert er["maxOrderUsd"] == 25.0 and er["minEdgeCents"] == 3.0, \
            "an agent's rules widened a global rail"

    _try("MCP server: tools, loopback guard, gates", _mcp_check)

    def _agent_doors_check() -> None:
        """The HTTP API, the tool-call stream, the log scrubber, health and
        the account gate, proven in THIS build. Each module is lazy-imported,
        so a bundle that dropped one would fail only when a user reached it."""
        srv = __import__("mcp_server")
        ls = __import__("logscrub")
        hl = __import__("health")
        tools = __import__("mcp_tools")
        cfgmod = __import__("config")
        # The HTTP API is OFF until the user turns it on: 403, nothing runs.
        code, body, _ = asyncio.run(srv.handle_api("GET", "/api/v1/tools", b"", {}, "default"))
        assert code == 403 and body.get("ok") is False, "HTTP API answered while switched off"
        # The stream carries whitelisted fields only: never a raw argument.
        marker = "selftest free text must not leak"
        ev = srv.build_call_event(
            name="record_forecast",
            args={"conditionId": marker, "rationale": marker, "model": marker + " kp_" + "Q7x" * 15,
                  "fairValueCents": 42},
            session={"client": "selftest", "agent": "default"}, res={"ok": True},
            outcome="ok", reason=None, started=time.monotonic(), mode="paper")
        assert marker not in json.dumps(ev), "tool-call stream forwarded a raw argument"
        assert ev["conditionId"] is None, "a non-conditionId passed the shape check"
        ev = srv.build_call_event(
            name="search_markets", args={"query": marker}, session={"client": "selftest"},
            res={"markets": []}, outcome="ok", reason=None, started=time.monotonic(), mode=None)
        assert marker not in json.dumps(ev), "search query reached the stream"
        # Secrets never reach a log line.
        tok = "kp_" + "Zx9" * 14
        assert tok not in ls.scrub(f"Authorization: Bearer {tok}"), "bearer token logged"
        assert ls.looks_secret(tok.upper()), "case-folded token not recognised"
        assert ls.scrub("conditionId 0x" + "ab" * 32) == "conditionId 0x" + "ab" * 32, \
            "the scrubber ate a conditionId"
        # Agents never flip the account; Paper switches every engine off.
        assert tools.classify("account_mode") == "protected"
        assert tools.classify("terminal_paper_mode") == "protected"
        unclassified = [k for k in cfgmod.DEFAULT_CONFIG if tools.classify(k) == "unknown"]
        assert not unclassified, f"unclassified config keys: {unclassified[:5]}"
        paper = cfgmod.merge_with_defaults({"account_mode": "paper", "enable_trading": True})
        assert paper["terminal_paper_mode"] is True and not cfgmod.engine_on(paper, "enable_trading")
        assert hl.check_http({"enabled": True, "httpEnabled": False})["status"] == hl.OFF

    _try("agent doors: HTTP API off, stream whitelist, log scrub, account gate",
         _agent_doors_check)
    _try("import health", lambda: __import__("health"))
    _try("import alerts", lambda: __import__("alerts"))
    _try("import history_download", lambda: __import__("history_download"))
    _try("import logscrub", lambda: __import__("logscrub"))

    def _isolated_backtest_check() -> None:
        # An agent's backtest runs in a child of THIS executable
        # (--backtest-child): it must start, read a DB, and refuse the network.
        # The DB is a fresh one with the app's real schema, in the selftest's
        # throwaway profile (_selftest isolates it): a hand-made table broke
        # as soon as the backtester read one more column.
        db.init_db()
        db.close_thread_connection()
        f = str(db.db_path())
        code = "\n".join([
            "def on_start(state):",
            "    import socket",
            "    socket.create_connection(('example.com', 80))",
            "",
            "def decide(ctx):",
            "    return None",
            "",
        ])
        res = asyncio.run(_script_backtest_isolated(
            merge_with_defaults({}), code, None, "mainnet", 7, db_file=f))
        err = str(res.get("scriptError") or "")
        assert "network is off" in err, f"child did not refuse the network: {err or res}"

    _try("agent backtest runs isolated (child process, no network)", _isolated_backtest_check)

    ok = all(c[1] for c in checks)
    print("=== krypt-polybot-us backend selftest ===")
    for name, passed, err in checks:
        print(f"  [{'OK  ' if passed else 'FAIL'}] {name}{('  -> ' + err) if err else ''}")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
