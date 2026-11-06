"""Backtest a user strategy script over the recorded tick corpus.

Same data, same fill honesty as `replay.py` (real-book asks only, fee-v2
taker fees, optional quote-stability / latency toggles), but the entry
decision comes from the script's `decide(ctx)` instead of the built-in
gates. Windows are replayed in chronological close-time order so a stateful
script sees history the way it would live; `state` persists across the whole
run and `on_start` / `on_fill` / `on_settle` fire at the equivalent points.

Structurally order-free: this module never imports polymarket_api —
decide() returns data, the harness does the accounting. A backtest cannot
place an order by construction. (Scripts are full Python and could import an
HTTP client themselves; that is what the script audit is for. This guarantee
is about the harness, not about what arbitrary user code can reach.)

Hook calls here run TRACED (script_sandbox.call_budgeted(traced=True)) rather
than thread-bounded like the live engine: one run makes thousands of calls,
far too many to give each its own thread, and without an in-call budget a
single `while True:` in decide() would hang the backend's RPC forever.
"""
from __future__ import annotations

import json
import heapq
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

import backtest as bt
import crypto15m
import db as dbmod
import replay
import script_sandbox

# The app's per-order contract floor, mirrored from
# crypto15m_trader._C15_MIN_CONTRACTS. Duplicated as a literal ON PURPOSE:
# importing crypto15m_trader would pull polymarket_api in and break this
# module's "structurally order-free" guarantee. test_script_engine pins the
# two together so they cannot drift.
MIN_CONTRACTS = 5

# Hard wall-clock cap for one backtest run; hit it and the payload comes
# back partial + flagged rather than wedging the RPC until timeout.
MAX_RUN_SECS = 90.0
# Per-call ceiling for a traced hook. Generous next to the live engine's
# thread timeout because tracing costs ~10-30x, so the same script legitimately
# needs more wall-clock per call here than it does live.
DECIDE_BUDGET_MS = 500.0


# ── per-script asset scope ───────────────────────────────────────────────
# Lives HERE, in the order-free module, because both the live engine and the
# backtest must scope a script's coins identically, and script_engine already
# imports from this module (the reverse would be a cycle).


def parse_asset_scope(raw: Any) -> Optional[list]:
    """A script's own coin scope: None/empty = every coin the snapshot has.

    Deliberately NOT crypto15m_assets. That key belongs to the 15m Crypto
    tab's engine, and reading it here meant a preset applied over there (the
    5m preset pins ['BTC']) silently narrowed every script in the app, in the
    live engine AND in the backtest, with nothing in the Scripts UI hinting
    at it — a script would simply stop finding setups on five of six coins.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [p.strip() for p in raw.split(",")]
    if not isinstance(raw, (list, tuple, set)):
        return None
    out = sorted({str(a).strip().upper() for a in raw if str(a).strip()})
    return out or None


def script_allows_asset(scope: Optional[list], asset: str) -> bool:
    if not scope:
        return True
    return str(asset or "").upper() in scope


def sanitize_intent(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """Coerce whatever decide() returned into a clean order intent, or a
    human-readable rejection. Shared by backtest and the live engine so the
    two can never disagree on the contract."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"decide() returned {type(raw).__name__}, expected dict or None"
    side = str(raw.get("side") or "").lower()
    if side not in ("up", "down"):
        return None, f"intent side must be 'up' or 'down', got {raw.get('side')!r}"
    price = raw.get("price")
    if price != "ask":
        try:
            price = int(price)
        except (TypeError, ValueError):
            return None, "intent price must be \"ask\" or an integer 1-99 (cents)"
        if not (1 <= price <= 99):
            return None, f"intent price {price}c out of range 1-99"
    out: dict[str, Any] = {"side": side, "price": price}
    if raw.get("size") is not None:
        try:
            size = int(raw["size"])
        except (TypeError, ValueError):
            return None, "intent size must be an integer"
        if size < 1:
            return None, f"intent size {size} must be >= 1"
        out["size"] = size
    if raw.get("take_profit_pct") is not None:
        try:
            tpp = float(raw["take_profit_pct"])
        except (TypeError, ValueError):
            return None, "take_profit_pct must be a number (e.g. 0.15 = +15%)"
        if not (0.0 < tpp <= 10.0):
            return None, f"take_profit_pct {tpp} out of range (0, 10]"
        out["take_profit_pct"] = tpp
    if raw.get("stop_loss_cents") is not None:
        try:
            slc = int(raw["stop_loss_cents"])
        except (TypeError, ValueError):
            return None, "stop_loss_cents must be an integer 1-99"
        if not (1 <= slc <= 99):
            return None, f"stop_loss_cents {slc} out of range 1-99"
        out["stop_loss_cents"] = slc
    if raw.get("reason") is not None:
        out["reason"] = str(raw["reason"])[:200]
    return out, None


def sanitize_market_intent(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """decide_market() contract. Same shape as decide()'s intent except the
    side names the outcome token directly ('yes'/'no') rather than the
    crypto window's up/down, because a general market has no up/down."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"decide_market() returned {type(raw).__name__}, expected dict or None"
    side = str(raw.get("side") or "").lower()
    if side not in ("yes", "no"):
        return None, f"market intent side must be 'yes' or 'no', got {raw.get('side')!r}"
    price = raw.get("price")
    if price != "ask":
        try:
            price = int(price)
        except (TypeError, ValueError):
            return None, "intent price must be \"ask\" or an integer 1-99 (cents)"
        if not (1 <= price <= 99):
            return None, f"intent price {price}c out of range 1-99"
    out: dict[str, Any] = {"side": side, "price": price}
    if raw.get("size") is not None:
        try:
            size = int(raw["size"])
        except (TypeError, ValueError):
            return None, "intent size must be an integer"
        if size < 1:
            return None, f"intent size {size} must be >= 1"
        out["size"] = size
    if raw.get("sizeUsd") is not None:
        try:
            szu = float(raw["sizeUsd"])
        except (TypeError, ValueError):
            return None, "sizeUsd must be a number"
        if not (1.0 <= szu <= 100_000.0):
            return None, f"sizeUsd {szu} out of range [1, 100000]"
        out["sizeUsd"] = szu
    if raw.get("reason") is not None:
        out["reason"] = str(raw["reason"])[:200]
    return out, None


def _hours_to_close(close_time: Any) -> Optional[float]:
    """Hours until a market resolves, or None if unknown.

    Scripts cannot compute this themselves — the sandbox has no imports, so no
    datetime and no way to parse an ISO string. Without it a strategy meant for
    same-day games happily buys the NO side of "team X wins the 2027 finals" at
    98c and holds it for 18 months, which is a completely different trade.
    """
    s = str(close_time or "").strip()
    if not s:
        return None
    from datetime import datetime, timezone
    txt = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return round((dt - datetime.now(timezone.utc)).total_seconds() / 3600.0, 2)


def market_to_js(m: dict) -> dict:
    """A markets row as the dict handed to decide_market(). Prices are
    FRACTIONS 0..1 to match every other script field, and are the app's last
    cached quote — the live ask is fetched only if the script actually wants
    to trade, so scanning stays cheap."""
    def _f(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if 0.0 < f < 1.0 else None

    yes_bid = _f(m.get("yes_bid"))
    yes_ask = _f(m.get("yes_ask"))
    last = _f(m.get("last_price"))
    prev = _f(m.get("prev_price"))
    mid = None
    if yes_bid is not None and yes_ask is not None:
        mid = round((yes_bid + yes_ask) / 2.0, 4)
    elif last is not None:
        mid = last
    return {
        "ticker": m.get("ticker"), "eventTicker": m.get("event_ticker") or "",
        "title": m.get("title") or "", "category": m.get("category") or "",
        "slug": m.get("slug") or "",
        "closeTime": m.get("close_time") or "",
        "hoursToClose": _hours_to_close(m.get("close_time")),
        "status": m.get("status") or "",
        "yesBid": yes_bid, "yesAsk": yes_ask,
        "noBid": round(1.0 - yes_ask, 4) if yes_ask is not None else None,
        "noAsk": round(1.0 - yes_bid, 4) if yes_bid is not None else None,
        "yesMid": mid, "noMid": round(1.0 - mid, 4) if mid is not None else None,
        "lastPrice": last, "prevPrice": prev,
        "priceChange": (round(last - prev, 4)
                        if last is not None and prev is not None else None),
        # Gamma's spread at the last market sync, or None when the row carries
        # no real two-sided book: _normalize_market then stores the mid as both
        # bid and ask, and a naive subtraction would yield 0.0c, passing any
        # "spread <= Nc" gate on a book nobody checked. None makes that gate
        # fail closed. The LIVE spread is enforced at placement, where the CLOB
        # quote is fetched.
        "spreadCents": (round((yes_ask - yes_bid) * 100.0, 2)
                        if yes_bid is not None and yes_ask is not None
                        and yes_ask > yes_bid else None),
        "volume": float(m.get("volume") or 0.0),
        "volume24h": float(m.get("volume_24h") or 0.0),
        "openInterest": float(m.get("open_interest") or 0.0),
    }


def sanitize_manage(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """Coerce whatever manage() returned into a clean action, or a rejection.
    None / "hold" = keep holding; "sell" (or {"action":"sell"}) = flatten the
    whole position at the bid now; {"action":"update", take_profit_pct?,
    stop_loss_cents?} = retune the exit targets (0 clears one)."""
    if raw is None or raw == "hold":
        return None, None
    if raw == "sell":
        return {"action": "sell"}, None
    if not isinstance(raw, dict):
        return None, f"manage() returned {type(raw).__name__}, expected None/'hold'/'sell'/dict"
    action = str(raw.get("action") or "").lower()
    if action == "sell":
        return {"action": "sell"}, None
    if action != "update":
        return None, f"unknown manage action {raw.get('action')!r}"
    out: dict[str, Any] = {"action": "update"}
    if "take_profit_pct" in raw:
        try:
            tpp = float(raw["take_profit_pct"] or 0.0)
        except (TypeError, ValueError):
            return None, "take_profit_pct must be a number (0 clears it)"
        if not (0.0 <= tpp <= 10.0):
            return None, f"take_profit_pct {tpp} out of range [0, 10]"
        out["take_profit_pct"] = tpp
    if "stop_loss_cents" in raw:
        try:
            slc = int(raw["stop_loss_cents"] or 0)
        except (TypeError, ValueError):
            return None, "stop_loss_cents must be an integer 0-99 (0 clears it)"
        if not (0 <= slc <= 99):
            return None, f"stop_loss_cents {slc} out of range 0-99"
        out["stop_loss_cents"] = slc
    return out, None


def sanitize_signal_action(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """decide_signal() contract: None = skip; True (or {"follow": True,
    "sizeUsd"?}) = follow the signal's own side with follower economics.
    Scripts FILTER and SIZE signals — they can't flip sides (side-flipped
    fills aren't derivable from the recorded data, so they'd be
    unbacktestable)."""
    if raw is None or raw is False:
        return None, None
    if raw is True:
        return {"follow": True}, None
    if not isinstance(raw, dict):
        return None, f"decide_signal() returned {type(raw).__name__}, expected None/True/dict"
    if not raw.get("follow"):
        return None, None
    out: dict[str, Any] = {"follow": True}
    if raw.get("sizeUsd") is not None:
        try:
            sz = float(raw["sizeUsd"])
        except (TypeError, ValueError):
            return None, "sizeUsd must be a number"
        if not (1.0 <= sz <= 100_000.0):
            return None, f"sizeUsd {sz} out of range [1, 100000]"
        out["sizeUsd"] = sz
    return out, None


def signal_to_js(sig: dict, source: str) -> dict:
    """The dict handed to decide_signal(signal) — shared by the live engine
    and the backtest so they can never drift. Numeric gate fields come from
    the same builder the main engine's rules use."""
    import trader as trader_mod
    try:
        vals = trader_mod._signal_rule_values(sig, source)
    except Exception:
        vals = {}
    ts = str(sig.get("created_at") or "")
    hour = None
    try:
        hour = int(ts[11:13])
    except (ValueError, IndexError):
        pass
    price = sig.get("price")
    direction = (sig.get("direction") or "yes").lower()
    if source == "momentum" and direction == "no" and price is not None:
        price = 1.0 - float(price)   # cost of the signal's actual side
    return {
        "source": source,
        "ticker": sig.get("ticker") or "",
        "category": sig.get("category") or "",
        "title": sig.get("title") or sig.get("yes_sub_title") or "",
        "price": float(price) if price is not None else None,
        "side": sig.get("taker_side") or sig.get("direction") or "",
        "dollarValue": sig.get("dollar_value"),
        "confidence": vals.get("confidence"),
        "edgePts": vals.get("edge"),
        "costCents": vals.get("costCents"),
        "signalType": sig.get("signal_type") or "",
        "hourUtc": hour,
        "createdAt": ts,
    }


def _exit_step(
    script: "script_sandbox.CompiledScript", t2: dict, p: dict, cfg: dict,
    portfolio: dict,
) -> Optional[tuple[float, str]]:
    """One post-entry tick of the exit layer for open position `p`: static
    tp/sl triggers (retunable by manage) checked first, then the script's
    manage() hook. Sells fill into that tick's REAL bid with both taker fees
    charged. Returns (pnl_per_contract, reason) when it exits, else None.
    Raises ScriptError if manage() dies or returns garbage (matches live,
    where that auto-disables the script)."""
    side, cost = p["side"], p["cost"]
    if side == "up":
        bid = t2.get("yes_bid")
    else:
        ya = t2.get("yes_ask")
        bid = (1.0 - float(ya)) if ya is not None else None
    if bid is not None:
        bid = float(bid)
        if not (0.0 < bid < 1.0):
            bid = None

    def _sell(reason: str) -> tuple[float, str]:
        exit_fee = bt.polymarket_fee_per_contract(bid, category="crypto")
        return (bid - cost) - p["entry_fee"] - exit_fee, reason

    if bid is not None:
        if p["want_sell"]:
            return _sell("script_exit")   # manage() said sell; first real bid
        if p["sl_cents"] and bid * 100.0 <= float(p["sl_cents"]):
            return _sell("stop_loss")
        if p["tp_pct"] and bid >= cost * (1.0 + float(p["tp_pct"])):
            return _sell("take_profit")
    if not p["has_manage"]:
        return None
    ctx = replay.tick_to_asset(t2, cfg, p["close_iso"])
    ctx["portfolio"] = portfolio
    pos = {
        "ticker": p["ticker"], "asset": p["asset"], "side": side,
        "contracts": p["size"], "avgEntryCents": round(cost * 100, 1),
        "curBidCents": round(bid * 100, 1) if bid is not None else None,
        "minsLeft": ctx.get("minsLeft"),
        "tpPct": p["tp_pct"], "slCents": p["sl_cents"],
        "unrealizedPct": round((bid - cost) / cost, 4) if bid is not None and cost > 0 else None,
    }
    act_raw = script.call("manage", pos, ctx, budget_ms=DECIDE_BUDGET_MS)
    act, err = sanitize_manage(act_raw)
    if err:
        raise script_sandbox.ScriptError(f"manage(): {err}")
    if act is None:
        return None
    if act["action"] == "sell":
        if bid is not None:
            return _sell("script_exit")
        p["want_sell"] = True   # no executable bid this tick — sell at the next one
    else:  # update
        if "take_profit_pct" in act:
            p["tp_pct"] = act["take_profit_pct"] or None
        if "stop_loss_cents" in act:
            p["sl_cents"] = act["stop_loss_cents"] or None
    return None


def _epoch(ts: Any) -> float:
    """Epoch seconds from a tick's observed_at ('YYYY-MM-DD HH:MM:SS') or a
    window's close ('YYYY-MM-DDTHH:MM:SSZ'); +inf when unparseable, which
    settles that window last (never early)."""
    txt = str(ts or "").strip().replace("T", " ")
    try:
        return datetime.strptime(txt[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return math.inf


def run(cfg: dict, code: str, *, assets: Optional[list] = None,
        env: str = "mainnet", since_days: int = 60) -> dict:
    """Backtest `code` over recorded resolved windows. Returns the standard
    Crypto15mBacktest payload extended with scriptLogs / scriptError /
    intent-rejection counters.

    `assets` is the SCRIPT's own coin scope (None = all). It is not read from
    cfg, so a backtest replays the same universe the live engine would give
    this script no matter how the 15m Crypto tab is currently configured."""
    cfg = dict(cfg)
    scope = parse_asset_scope(assets)
    contracts = max(1, int(cfg.get("crypto15m_order_size") or 1))
    max_entry_cents = int(cfg.get("script_max_entry_cents") or 97)
    max_contracts = max(1, int(cfg.get("script_max_contracts") or 20))
    stable_secs = float(cfg.get("replay_min_quote_stable_secs") or 0.0)
    latency_secs = float(cfg.get("replay_latency_secs") or 0.0)
    slip_tol = float(cfg.get("replay_latency_slip_tol_cents", 1.0) or 0.0) / 100.0
    latency_misses = 0
    interval = crypto15m._interval(cfg)

    try:
        script = script_sandbox.CompiledScript(
            "backtest", code, traced=True)
    except script_sandbox.SCRIPT_FAILURES as e:
        return {"scriptError": str(e), "n": 0, "wins": 0, "winRate": 0.0,
                "netEvCentsPerContract": 0.0, "totalPnlUsd": 0.0,
                "tStat": None, "maxDrawdownUsd": 0.0, "contracts": contracts,
                "windowsScanned": 0, "byAsset": {}, "equity": [],
                "byHourUtc": [], "byDay": [], "trades": [],
                "caveats": [f"Script failed to compile: {e}"],
                "interval": interval, "scriptLogs": []}

    with dbmod.get_db() as conn:
        # A window older than retention reads the research archive too.
        ticks = dbmod.research_source(conn, "crypto15m_ticks", since_days)
        sigs = dbmod.research_source(conn, "crypto15m_signals", since_days)
        rows = conn.execute(
            f"""SELECT t.*, s.up_won, s.close_time AS sig_close
               FROM {ticks} t
               JOIN {sigs} s
                 ON s.ticker = t.ticker AND s.network = t.network
               WHERE s.resolved = 1 AND s.up_won IS NOT NULL
                 AND t.network = ?
                 AND COALESCE(s.interval, '15m') = ?
                 AND t.observed_at >= datetime('now', ?)
               ORDER BY t.ticker, t.observed_at""",
            (env, interval, f"-{int(since_days)} days"),
        ).fetchall()

    by_window: dict[str, list[dict]] = {}
    for r in rows:
        by_window.setdefault(r["ticker"], []).append(dict(r))
    # Chronological close-time order: a stateful script must see windows the
    # way live time delivers them, not in ticker-sort order.
    windows = sorted(
        by_window.items(),
        key=lambda kv: str(kv[1][0].get("sig_close") or ""),
    )

    script_error: Optional[str] = None
    rejected_intents = 0
    rail_refusals = 0
    partial = False
    trades: list[dict] = []
    n_windows = 0
    started = time.perf_counter()
    # Simulated portfolio handed to the script (documented as approximate:
    # balance isn't modeled and open positions are within-window only).
    run_pnl = 0.0
    day_pnl: dict[str, float] = {}

    try:
        script.call("on_start", script.state, budget_ms=DECIDE_BUDGET_MS)
    except script_sandbox.SCRIPT_FAILURES as e:
        script_error = str(e)

    # A script with no decide() has nothing to replay against crypto windows.
    # Remember WHY the panel is empty — otherwise the caveat below blames the
    # user's recorded data ("no resolved windows, let data accumulate") for what
    # is really "this hook isn't backtestable", which sends people off to fix a
    # recorder that was working fine.
    no_window_hook = "decide" not in script.hooks
    if no_window_hook:
        windows = []   # signal-only / market-only / supervisor-only script

    # ONE TIMELINE across every window. Replaying windows one after another
    # leaked the future: by the time a script decided on one coin's window,
    # on_settle and the portfolio P&L had already told it how the other coins
    # closing at the same moment settled (~7 per close, and they move
    # together), and a stateful script carried later prices into earlier
    # decisions. A probe that only read peer outcomes scored +6.1c/contract
    # (t~9.7) against a -3.2c control (2026-10-11 audit). Now every tick of
    # every window is replayed in observed order; an outcome reaches the
    # script (on_settle, todayPnlUsd/totalPnlUsd) only once the clock passes
    # that window's close, or the tick its exit filled.
    wins: dict[str, dict] = {}
    timeline: list[tuple[float, str, str, int]] = []
    for ticker, ticks in windows:
        if not script_allows_asset(scope, str(ticks[0].get("asset") or "")):
            continue
        close_iso = str(ticks[0].get("sig_close") or "")
        wins[ticker] = {
            "ticks": ticks, "close_iso": close_iso, "close_at": _epoch(close_iso),
            "up_won": int(ticks[0].get("up_won") or 0), "done": False, "pos": None,
            "seen": False,
        }
        for k, t in enumerate(ticks):
            timeline.append((_epoch(t.get("observed_at")), str(t.get("observed_at") or ""), ticker, k))
    timeline.sort()
    pending: list[tuple[float, int, str]] = []   # (settle_at, seq, ticker)
    seq = 0
    open_count = 0

    def _portfolio(day: str) -> dict:
        return {
            "balanceUsd": None, "openCount": open_count, "openPositions": [],
            "todayPnlUsd": round(day_pnl.get(day, 0.0), 4),
            "totalPnlUsd": round(run_pnl, 4),
        }

    def _settle(ticker: str, at: float, *, hooks: bool) -> None:
        """Deliver a position's outcome: on_settle, then the P&L the
        portfolio shows from here on."""
        nonlocal run_pnl, script_error, open_count
        w = wins[ticker]
        p = w["pos"]
        if not p or p.get("settled"):
            return
        p["settled"] = True
        open_count -= 1
        pnl_usd = p["pnl_ct"] * p["size"]
        pos_lite = {
            "ticker": ticker, "asset": p["asset"], "side": p["side"],
            "costCents": round(p["cost"] * 100, 1), "contracts": p["size"],
            "won": bool(p["won"]), "pnlUsd": round(pnl_usd, 4),
            "exitReason": p["exit_reason"], "reason": p["reason"],
        }
        if hooks and not script_error:
            try:
                script.call("on_settle", pos_lite, script.state,
                            budget_ms=DECIDE_BUDGET_MS)
            except script_sandbox.SCRIPT_FAILURES as e:
                script_error = str(e)
        trades.append({
            "ticker": ticker, "asset": p["asset"], "side": p["side"],
            "costCents": round(p["cost"] * 100, 1),
            "minsLeft": p["mins_left"], "won": bool(p["won"]),
            "pnlUsd": round(pnl_usd, 4), "contracts": p["size"],
            "exitReason": p["exit_reason"], "at": p["at"],
            # the significance cluster (replay._summarize): windows closing together
            "closeTime": p["close_iso"],
        })
        run_pnl += pnl_usd
        sday = (datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%d")
                if math.isfinite(at) else p["at"][:10])
        day_pnl[sday] = day_pnl.get(sday, 0.0) + pnl_usd

    for now, observed, ticker, i in timeline:
        if script_error:
            break
        if time.perf_counter() - started > MAX_RUN_SECS:
            partial = True
            break
        while pending and pending[0][0] <= now:
            at, _, tk = heapq.heappop(pending)
            _settle(tk, at, hooks=True)
        w = wins[ticker]
        ticks = w["ticks"]
        t = ticks[i]
        if not w["seen"]:
            w["seen"] = True
            n_windows += 1
        day = observed[:10]
        p = w["pos"]
        if p is not None:
            # the open position's exit layer, from the tick after its fill
            if p.get("settled") or not p["exits"] or i <= p["fill_idx"]:
                continue
            try:
                ex = _exit_step(script, t, p, cfg, _portfolio(day))
            except script_sandbox.SCRIPT_FAILURES as e:
                script_error = str(e)
                break
            if ex is not None:
                p["pnl_ct"], p["exit_reason"] = ex
                p["won"] = p["pnl_ct"] > 0
                _settle(ticker, now, hooks=True)
            continue
        if w["done"]:
            continue
        close_iso = w["close_iso"]
        asset = replay.tick_to_asset(t, cfg, close_iso)
        ctx = dict(asset)
        ctx["portfolio"] = _portfolio(day)
        try:
            raw = script.call("decide", ctx, budget_ms=DECIDE_BUDGET_MS)
        except script_sandbox.SCRIPT_FAILURES as e:
            script_error = str(e)
            break
        if raw is None:
            continue
        intent, err = sanitize_intent(raw)
        if err:
            rejected_intents += 1
            if rejected_intents <= 5:
                script._log_lines.append(f"[harness] rejected intent: {err}")
            continue
        side = intent["side"]
        ask = replay._exec_ask(t, side)
        if ask is None:
            # No REAL executable book this tick (Gamma fallback = phantom).
            # A later tick may have one — same convention as live/replay.
            continue
        ask_cents = ask * 100.0
        if intent["price"] != "ask":
            # Live refuses a numeric-price intent whose LIMIT exceeds the
            # entry cap up front (consuming the window), regardless of the
            # ask — mirror that here rather than checking only the fill ask.
            limit_c = float(intent["price"])
            if limit_c > max_entry_cents:
                rail_refusals += 1
                w["done"] = True
                continue
            if ask_cents > limit_c:
                # Resting limit under the ask: maker fills are NOT modeled
                # (same as replay) — the limit only "fills" when the ask is
                # at/under it, i.e. the order would have crossed.
                continue
        # ── the intent is live from here: it consumes the window (one
        # entry attempt per market per window, mirroring the engine) ──
        if ask_cents > max_entry_cents:
            rail_refusals += 1
            w["done"] = True
            continue
        if stable_secs > 0 and replay._quote_stable_secs(ticks, i, side) < stable_secs:
            continue
        cost, fill_idx = float(ask), i
        if latency_secs > 0:
            filled = replay._latency_fill(
                ticks, i, side, cost, latency_secs, slip_tol)
            if filled is None:
                latency_misses += 1
                continue
            cost, fill_idx = filled
        size = min(int(intent.get("size") or contracts), max_contracts)
        fee = bt.polymarket_fee_per_contract(cost, category="crypto")
        won = w["up_won"] if side == "up" else (1 - w["up_won"])
        tp_pct = intent.get("take_profit_pct")
        sl_cents = intent.get("stop_loss_cents")
        w["done"] = True
        w["pos"] = p = {
            "ticker": ticker, "asset": asset.get("asset"), "side": side,
            "cost": cost, "size": size, "fill_idx": fill_idx,
            "entry_fee": fee, "tp_pct": tp_pct, "sl_cents": sl_cents,
            "want_sell": False, "has_manage": "manage" in script.hooks,
            "exits": bool("manage" in script.hooks or tp_pct or sl_cents),
            "close_iso": close_iso, "reason": intent.get("reason"),
            "mins_left": asset.get("minsLeft"), "at": observed,
            # held to settlement unless the exit layer sells first
            "won": won, "pnl_ct": (1.0 - cost - fee) if won else (-cost - fee),
            "exit_reason": "settlement",
        }
        open_count += 1
        try:
            # Live delivers on_fill at fill time with the outcome still
            # UNKNOWN (won/pnl are None until settlement), so a script that
            # reads pos["won"] in on_fill must not see it here.
            script.call("on_fill", {
                "ticker": ticker, "asset": p["asset"], "side": side,
                "costCents": round(cost * 100, 1), "contracts": size,
                "won": None, "pnlUsd": None, "exitReason": None,
                "reason": p["reason"],
            }, script.state, budget_ms=DECIDE_BUDGET_MS)
        except script_sandbox.SCRIPT_FAILURES as e:
            script_error = str(e)
        seq += 1
        heapq.heappush(pending, (w["close_at"], seq, ticker))

    # Positions still open when the replay ended settle now — in close order,
    # through the script's hooks unless it died (their P&L still counts).
    while pending:
        at, _, tk = heapq.heappop(pending)
        _settle(tk, at, hooks=not partial)

    caveats = [
        *[c for c in [replay.history_caveat(by_window)] if c],
        f"Replayed the {interval} window series only (the interval this config trades).",
        "Entries fill at the recorded REAL-book ask (taker, fee curve 0.0695·P·(1−P)); Gamma-fallback prices are treated as no quote, exactly like live.",
        "Resting limits below the ask are NOT modeled as maker fills — an intent only fills when the recorded ask crosses its price.",
        "EXIT FILLS ARE OPTIMISTIC: take-profit / stop-loss / manage() sells fill "
        "into the recorded bid, but that bid can collapse to the model mid when "
        "the real CLOB book is empty. Live, these 15m markets frequently have NO "
        "bid and gap straight to $0 — so scripted stops/exits realize far less "
        "than shown here. Treat capped-downside backtests with suspicion.",
        "script_max_open and the per-script daily-loss breaker are NOT simulated: "
        "live, concurrent entries are capped and a losing day auto-disables the "
        "script, so live is a rail-limited SUBSET of these trades.",
        "Size is the intent size capped at script_max_contracts; live, the $1 "
        f"min-notional / {MIN_CONTRACTS}-contract floor can push "
        "cheap-ask entries larger — or REFUSE the entry outright when meeting "
        "that floor would breach your cap.",
        f"Ticks are ~4-25s apart over {since_days} days of app uptime only; decide() sees those ticks, not every live quote.",
        "In-sample: a script tuned against this panel is fit to the past. Watch it run before arming live.",
    ]
    if rejected_intents:
        caveats.insert(0, (
            f"{rejected_intents} intent(s) were malformed and skipped — see the "
            "script log for the first few reasons."
        ))
    if rail_refusals:
        caveats.append(
            f"{rail_refusals} entr(y/ies) refused by the safety rails "
            f"(ask above script_max_entry_cents={max_entry_cents}c) — the same "
            "refusal happens live."
        )
    if stable_secs > 0:
        caveats.append(
            f"Quote-stability filter ON (≥{stable_secs:g}s unchanged ask).")
    if latency_secs > 0:
        caveats.append(
            f"Signal-to-fill latency ON ({latency_secs:g}s, {latency_misses} misses).")
    if partial:
        caveats.insert(0, (
            f"PARTIAL RESULT: the run hit the {MAX_RUN_SECS:.0f}s wall-clock cap "
            f"after {n_windows} windows — narrow sinceDays or simplify the script."
        ))
    if script_error:
        caveats.insert(0, (
            f"Script DIED mid-run: {script_error} — results cover only the "
            "windows before the error. Live, this same error would auto-disable "
            "the script."
        ))
    not_backtestable = None
    if no_window_hook:
        hooks = sorted(script.hooks) or ["(none)"]
        not_backtestable = (
            f"NOT BACKTESTABLE: this script defines {', '.join(hooks)} but no "
            "decide(ctx). The recorded corpus is crypto Up/Down windows only, "
            "so decide_market() / decide_signal() / supervise() have nothing to "
            "replay against — that is why there are 0 trades, NOT a lack of "
            "recorded data. Validate this script in SHADOW mode instead."
        )
        caveats.insert(0, not_backtestable)
    no_data = None
    if not no_window_hook and n_windows == 0:
        # Shared with the Backtest page: says which interval this needs, what
        # IS recorded, and how to record the right one. _summarize puts it
        # FIRST (above even the missing-ctx-fields note) — it is the reason
        # for the zero.
        no_data = replay.empty_replay_report(
            interval, since_days, env=env,
            windows_for_interval=len(by_window), assets=scope,
        )
    ctx_fields = set(script_sandbox.find_ctx_fields(code))
    if ctx_fields & replay.MODEL_FIELDS:
        _n_old, pre_cav = replay.pre_twap_model_caveat(trades, interval)
        if pre_cav:
            caveats.insert(0, pre_cav)
    missing = sorted(ctx_fields - replay._DERIVABLE)
    if missing:
        caveats.insert(0, (
            "Script reads ctx fields not recorded in ticks (None during "
            f"backtest, live-only): {', '.join(missing)}"
        ))

    if "manage" in script.hooks:
        caveats.append(
            "manage() is simulated per recorded tick (~25s apart): live it runs "
            "every ~5s, so live exits can react faster than this replay shows."
        )
    if "supervise" in script.hooks:
        caveats.append(
            "supervise() is NOT simulated — config/engine switching has no "
            "backtestable counterfactual. Only its decide/manage effects show here."
        )

    out = replay._summarize(
        trades, contracts, n_windows, caveats,
        empty_reason=not_backtestable or (no_data["message"] if no_data else None),
    )
    if no_data:
        out["noData"] = no_data
    # _summarize prepends its own "Only 0 trades — too few for a verdict" line.
    # When the script simply isn't backtestable, that reads as a data problem,
    # so hoist the explanation above it — it is the reason for the zero.
    if no_window_hook:
        cav = list(out.get("caveats") or [])
        lead = [c for c in cav if c.startswith("NOT BACKTESTABLE")]
        if lead:
            out["caveats"] = lead + [c for c in cav if not c.startswith("NOT BACKTESTABLE")]
    out["interval"] = interval
    out["scriptError"] = script_error
    out["fillModel"] = {
        "minQuoteStableSecs": stable_secs,
        "latencySecs": latency_secs,
        "latencySlipTolCents": slip_tol * 100.0,
        "latencyMisses": latency_misses,
    }
    # Whale/momentum signal replay (decide_signal hook) — separate economics,
    # reported as its own payload beside the crypto result.
    if "decide_signal" in script.hooks and not script_error:
        try:
            out["signalResult"] = _replay_signals(script, cfg, since_days=since_days)
        except script_sandbox.SCRIPT_FAILURES as e:
            out["signalResult"] = {"scriptError": str(e), "n": 0, "caveats": [
                f"decide_signal died: {e}"]}
    out["scriptLogs"] = script.drain_logs()[-200:]
    return out


def _replay_signals(script: "script_sandbox.CompiledScript", cfg: dict, *,
                    since_days: int = 60, slippage_cents: float = 1.0) -> dict:
    """Replay recorded whale prints + momentum alerts through the script's
    decide_signal(signal) filter with follower economics — the same model as
    the main-engine backtest (entry at signal price + slippage, category
    fee-v2, outcome from the recorded resolution)."""
    fixed_usd = float(cfg.get("fixed_trade_usd") or 5.0)
    # Same rails the live signal-follow path enforces (script_engine._place_
    # signal_follow), so the backtest can't show trades live would refuse or
    # sizes live would cap.
    sig_max_entry_cents = float(cfg.get("script_max_entry_cents") or 97)
    sig_max_contracts = max(1, int(cfg.get("script_max_contracts") or 20))
    # Live refuses entirely when the exchange floor exceeds the user's cap
    # (it never rounds the cap UP), so the backtest must show no trades either.
    cap_below_floor = MIN_CONTRACTS > sig_max_contracts
    rail_skips = 0
    with dbmod.get_db() as conn:
        # Whales/alerts leave the live DB after 45 days, inside the 60-day
        # window; the research archive holds the rest.
        whales = conn.execute(
            f"""SELECT * FROM {dbmod.research_source(conn, "whale_trades", since_days)}
               WHERE resolved=1
               AND outcome_correct IS NOT NULL
               AND created_at >= datetime('now', ?)""",
            (f"-{int(since_days)} days",),
        ).fetchall()
        alerts = conn.execute(
            f"""SELECT * FROM {dbmod.research_source(conn, "alerts", since_days)}
               WHERE resolved=1
               AND outcome_correct IS NOT NULL
               AND created_at >= datetime('now', ?)""",
            (f"-{int(since_days)} days",),
        ).fetchall()
    merged = sorted(
        [(dict(r), "whale") for r in whales] + [(dict(r), "momentum") for r in alerts],
        key=lambda x: str(x[0].get("created_at") or ""),
    )
    trades: list[dict] = []
    scanned = 0
    rejected = 0
    for sig, source in merged:
        scanned += 1
        signal = signal_to_js(sig, source)
        raw = script.call("decide_signal", dict(signal),
                          budget_ms=DECIDE_BUDGET_MS)
        act, err = sanitize_signal_action(raw)
        if err:
            rejected += 1
            continue
        if act is None:
            continue
        price = signal.get("price")
        if price is None or not (0.0 < float(price) < 1.0):
            continue
        cost = min(0.99, float(price) + slippage_cents / 100.0)
        if cost * 100.0 > sig_max_entry_cents:
            # Live refuses ask > script_max_entry_cents → no trade at all.
            rail_skips += 1
            continue
        if cap_below_floor:
            rail_skips += 1
            continue
        spend = float(act.get("sizeUsd") or fixed_usd)
        n_ct = max(1, int(round(spend / max(cost, 0.01))))
        # Live sizing: floor of MIN_CONTRACTS, capped at script_max_contracts.
        n_ct = min(max(n_ct, MIN_CONTRACTS), sig_max_contracts)
        fee = bt.polymarket_fee_per_contract(cost, category=str(sig.get("category") or ""))
        won = bool(sig.get("outcome_correct"))
        pnl_ct = (1.0 - cost - fee) if won else (-cost - fee)
        trades.append({
            "ticker": sig.get("ticker"), "asset": sig.get("category") or source,
            "side": signal.get("side") or "?",
            "costCents": round(cost * 100, 1), "minsLeft": None,
            "won": won, "pnlUsd": round(pnl_ct * n_ct, 4),
            "contracts": n_ct, "at": sig.get("created_at"),
            # the significance cluster (replay._summarize): one event settles together
            "event": sig.get("event_ticker") or "",
        })
    caveats = [
        f"Follower economics: entry at the signal price +{slippage_cents:.0f}c slippage — live fills on fast markets can be worse.",
        "decide_signal() FILTERS the signal's own side (side-flipping isn't backtestable from recorded data).",
        f"Entry-price cap (script_max_entry_cents={sig_max_entry_cents:g}c) and size "
        f"(floor {MIN_CONTRACTS}, cap script_max_contracts={sig_max_contracts}) "
        "match live; but script_max_open and the per-script daily-loss breaker "
        "are NOT simulated.",
        "In-sample: signals were only recorded while the app was running.",
    ]
    if rail_skips:
        caveats.insert(0, f"{rail_skips} signal(s) skipped: ask above the entry cap (refused live too).")
    if rejected:
        caveats.insert(0, f"{rejected} malformed decide_signal return(s) skipped.")
    return replay._summarize(trades, 5, scanned, caveats)
