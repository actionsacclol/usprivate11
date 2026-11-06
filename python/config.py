"""Default Krypt PolyBot US configuration.

This dict is the source of truth for what the Python backend understands.
The Electron renderer's `TraderConfig` mirrors these keys (camelCase on
the JS side, snake_case here). Conversion is handled by `_camel_keys` in
the IPC layer.

Every knob the user sees in the UI is here. Add a new one? Add it here
first, mirror it in `shared/types.ts`, then surface it in
`src/pages/SettingsPage.tsx`.
"""
from __future__ import annotations
from typing import Any


import rules as rules_engine

# pylint: disable=invalid-name

# The recurring short-term crypto "Up/Down" series the bot can monitor/trade.
CRYPTO15M_ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# Composable entry-rule vocabulary for the crypto rule builder. Fields are
# crypto snapshot keys (the UI mirrors this list); ops are numeric comparisons.
C15_RULE_FIELDS = (
    "favoritePrice", "entryCost", "upProb", "bookImbalance",
    "arbEdgeCents", "deltaPct", "minsLeft",
    "hourUtc", "peersAgree", "marketBias",
    # underlying technical indicators (MACD/RSI on the 1-min underlying)
    "macd", "macdSignal", "macdHist", "macdCross", "rsi",
    # settlement-model vocabulary (spot-lag sniper): signed move from the
    # window open, both sides' real asks, realized 1-min vol, the model's
    # P(up), and its best fee-net edge in cents.
    "deltaSignedPct", "upAsk", "downAsk", "sigma1m", "modelProb", "edgeNetCents",
    # trend / VWAP vocabulary (Turbine strategy library). The *Pct relatives let
    # a rule gate on `> 0` (above VWAP / EMA above SMA / positive momentum)
    # without needing two raw fields.
    "vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
    "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
    "change5mPct", "change15mPct",
)
C15_RULE_OPS = (">=", "<=", ">", "<")

# Composable entry-rule vocabulary for the MAIN bot (whale/momentum/convergence).
# Fields are the numeric values trader._signal_rule_values builds per signal.
BOT_RULE_FIELDS = ("confidence", "edge", "costCents")

DEFAULT_CONFIG: dict[str, Any] = {
    # ── Environment ────────────────────────────────────────────────────
    "network": "mainnet",          # the one live environment
    "enable_trading": False,       # main-bot switch: ON = place REAL orders (no paper mode)

    # ── Strategy toggles ──────────────────────────────────────────────
    "trade_whales": True,
    "trade_momentum": True,
    "trade_convergence": False,

    # ── Signal gates ──────────────────────────────────────────────────
    "min_edge_pts_whale": 5.0,
    "min_edge_pts_momentum": 5.0,
    "min_confidence_whale": 55.0,
    "min_confidence_momentum": 55.0,
    "min_entry_price_cents": 15,
    "max_entry_price_cents": 85,
    # Skip any market whose resolution (close time) is more than this many days
    # out — e.g. 30 means don't take bets that won't resolve for a month+ (long-
    # dated politics markets tie up capital for months). 0 = off (no time limit).
    "max_resolution_days": 0,
    "allowed_momentum_signal_types": ["trade_cluster"],
    "allowed_categories": None,
    # Per-source category allowlists (compose with the global
    # `allowed_categories` above via AND). None = no source-specific
    # filter. These let one config run two different edges at once —
    # e.g. whales only in crypto while momentum only in sports — which a
    # single global list can't express. Backtest-derived; see the
    # `krypt-edge` preset.
    "allowed_whale_categories": None,
    "allowed_momentum_categories": None,
    "contrarian_only": True,

    # Custom entry rules (rule builder). When `use_rules` is on, the composed
    # conditions REPLACE the confidence/edge/price-bound gates above; source
    # toggles, momentum signal-type, and category filters still apply.
    "use_rules": False,
    "rules": [],

    # ── Bet sizing ────────────────────────────────────────────────────
    # Two modes; both ramp from a Min to a Max by signal edge (bigger bets on
    # higher-conviction signals), and both are capped by `hard_max_position_usd`.
    #   "percent"   → bet a fraction of balance (min/max_size_fraction)
    #   "contracts" → bet a fixed share count (min/max_contracts; Polymarket
    #                 enforces a 5-share floor)
    "sizing_mode": "percent",
    "base_size_fraction": 0.03,        # legacy; not used by the sizing math
    "min_size_fraction": 0.02,         # percent mode: min fraction of balance
    "max_size_fraction": 0.06,         # percent mode: max fraction of balance
    "min_contracts": 5,                # contracts mode: min shares (>= 5)
    "max_contracts": 20,               # contracts mode: max shares
    "sizing_base_edge": 5.0,           # bet Min size at/under this edge (pts)
    "sizing_max_edge": 20.0,           # bet Max size at/over this edge (pts)
    "hard_max_position_usd": 50.0,
    "min_cash_reserve_fraction": 0.05,

    # ── Order placement ───────────────────────────────────────────────
    "order_style": "limit_cross",  # limit_cross | limit_mid | market
    "cross_spread_fallback_offset": 2,
    "order_expiration_sec": 300,   # cancel a still-unfilled resting order after
                                   # N s. Thin markets need time for a seller to
                                   # appear; 90s was too aggressive. null = GTC.

    # ── Concurrency / exposure ────────────────────────────────────────
    "max_open_positions": 25,
    "max_positions_per_event": 1,
    "max_daily_new_positions": 40,
    # When True, ignore `max_daily_new_positions` entirely. The bot is
    # then bounded only by `max_open_positions`. Useful for users
    # running aggressive presets who don't want a daily cap.
    "unlimited_daily_new_positions": False,
    "max_total_exposure_fraction": 0.75,

    # ── Loop cadence (seconds) ────────────────────────────────────────
    "trade_scan_interval": 20,
    "position_poll_interval": 30,
    "balance_poll_interval": 60,
    "resolution_check_interval": 300,
    "whale_scan_interval": 120,
    # Whale detection also listens to the public activity websocket (strict
    # accelerator; the REST poll above stays the authority and backstop).
    # Kill switch for that path only — no UI, env KRYPT_ACTIVITY_WS=0 is the
    # hard opt-out for the socket itself.
    "momentum_scan_interval": 90,
    "market_refresh_interval": 300,
    "db_cleanup_interval": 3600,   # prune high-volume scanner tables hourly

    # ── Signal freshness ──────────────────────────────────────────────
    "max_signal_age_sec": 120,

    # ── Risk / safety ─────────────────────────────────────────────────
    # 0 = auto-detect from the first observed total balance after the
    # user connects their Polymarket account. The user can pin a specific
    # value in Settings → Position sizing if they want a fixed baseline.
    "start_bankroll_usd": 0.0,
    "stop_loss_on_day": -50.0,    # halt new trades if today's P&L below
    "take_profit_on_day": 0.0,    # 0 = disabled
    # Per-position take-profit as a FRACTION above cost (0–1, e.g. 0.20 = sell a
    # position once its live best BID is worth >= 20% more than it cost). Unlike
    # take_profit_on_day (a daily aggregate that only halts NEW entries), this
    # actively SELLS an individual winner into the book instead of holding to
    # settlement. Percent auto-scales to entry price: a 93¢ entry can't reach
    # +20% so it naturally holds; a 55¢ entry cashes out on a move to ~66¢.
    # 0 = off (buy-and-hold-to-settlement, the historical behavior).
    "take_profit_pct": 0.0,
    # When the daily STOP-LOSS trips, also market-sell every open position to
    # hard-cap the day's loss (default OFF = only halt new entries; existing
    # positions ride to settlement, so realized loss can exceed the limit).
    "flatten_on_daily_stop": False,
    # Lifetime drawdown breaker for the MAIN engine (whale/momentum/external),
    # mirroring crypto15m/copy: the daily stop re-arms every UTC midnight, so a
    # net-negative engine can bleed the daily cap forever with no ceiling. Once
    # cumulative realized main-engine loss reaches the absolute-$ override
    # (if > 0) or this fraction of the starting bankroll, NEW entries pause
    # until the user changes the setting. Uses wipe-surviving banked counters.
    "lifetime_loss_limit_pct": 0.5,
    "lifetime_loss_limit_usd": 0.0,

    # ── Trading time windows ──────────────────────────────────────────
    # When `trading_hours_enabled` is true the bot only opens new trades
    # if the current local time falls within [trading_hours_start,
    # trading_hours_end] AND the current weekday is in trading_days.
    # `trading_timezone_offset_min` is added to UTC to compute "local";
    # 0 = UTC, -300 = US Eastern (EST), -240 = US Eastern (EDT), etc.
    # Days are abbreviations: mon, tue, wed, thu, fri, sat, sun.
    "trading_hours_enabled": False,
    "trading_hours_start": "00:00",   # inclusive, 24h "HH:MM"
    "trading_hours_end": "23:59",     # inclusive
    "trading_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
    "trading_timezone_offset_min": 0,  # minutes from UTC

    # ── Whale-tracker scoring (used by scanner) ───────────────────────
    "min_whale_usd": 2500.0,
    "min_whale_confidence": 30.0,
    "min_whale_edge": 2.0,
    "min_momentum_confidence": 0.0,
    "min_momentum_edge": 5.0,
    "min_entry_price_frac": 0.50,

    # ── Short-term crypto strategy (powers the "Crypto" tab) ───────────
    # Polymarket runs recurring "Up or Down" crypto markets (5m / 15m /
    # hourly windows) across BTC/ETH/SOL/XRP/DOGE/BNB/HYPE. A momentum
    # favorite-follow baseline (not a proven edge). All of these are
    # user-editable from the Crypto tab. The thresholds drive the
    # monitor's entry SIGNAL; the executor acts on them only when
    # crypto15m_enabled is on (see the independent live gate below).
    # `crypto15m_interval` picks which recurring window to monitor.
    "crypto15m_interval": "15m",       # "5m" | "15m" | "hourly"
    # Which crypto series the executor may open NEW positions on. None = all
    # of CRYPTO15M_ASSETS; a list restricts to those symbols (positions already
    # open on a now-disabled asset are still managed to settlement).
    "crypto15m_assets": None,
    # Up + Down ≠ $1 arbitrage detector. When on, the Crypto tab flags markets
    # whose Up + Down best-asks sum to under $1 (a market-neutral edge, gross of
    # fees). Detection only — surfaced for you, not auto-traded.
    "crypto15m_arb_detect": True,
    "crypto15m_arb_min_edge_cents": 1.0,  # flag when the gross edge >= this (cents)
    # Order-flow / book-imbalance signal (top-of-book bid-vs-ask pressure on the
    # Up token). Recorded for backtest + shown in the Crypto tab; does NOT drive
    # trades yet (record → paper → validate first).
    "crypto15m_imbalance_detect": True,
    "crypto15m_imbalance_levels": 3,   # order-book depth levels to sum
    # Optional ENTRY GATE: require the order-flow imbalance to CONFIRM the side
    # being bought before entering (opt-in; the signal is otherwise record-only
    # / display). The first of the composable signal gates.
    "crypto15m_imbalance_gate": False,
    "crypto15m_imbalance_gate_min": 0.2,  # confirmation strength, 0..1
    # Underlying technical indicators (MACD/RSI on the 1-min underlying price).
    # Recorded for backtest + exposed as optional rule fields (macd / macdSignal
    # / macdHist / macdCross / rsi); does NOT drive trades unless a user composes
    # a rule or turns on a momentum gate below.
    "crypto15m_indicator_detect": True,
    # Optional momentum-confirmation ENTRY GATES on the underlying (opt-in;
    # otherwise the indicators are record/display only). Each requires its
    # indicator confirm the side being bought before an entry is allowed, ANDed
    # on top of the favorite/signal gate (direction-aware, like the imbalance
    # gate): an Up bet needs rsi>=min_rsi & macdHist>=min_macd_hist; a Down bet
    # needs the mirror (rsi<=100-min_rsi & macdHist<=-min_macd_hist). Each 0 =
    # off. RSI is 0–100 (scale-free). macdHist is the signal-line histogram —
    # its SIGN is scale-free, so a small floor behaves sanely across BTC↔DOGE
    # (unlike the raw MACD line, whose scale tracks the coin's price).
    "crypto15m_min_rsi": 0.0,
    "crypto15m_min_macd_hist": 0.0,
    # Real-time CLOB book via the public WebSocket market feed. Surfaces +
    # records the REAL best bid/ask the bot fills on (vs the lagging Gamma mid).
    # Record/display only — does not drive the entry decision yet. Off = the
    # feed never connects and the bot behaves exactly as before.
    "crypto15m_ws_book": True,
    # User-composed entry rules (the rule builder). Each is {field, op, value};
    # all AND together. When crypto15m_use_rules is on, these REPLACE the
    # built-in favorite/signal gate (the side bought is still set by
    # crypto15m_direction_mode).
    "crypto15m_use_rules": False,
    "crypto15m_rules": [],
    # Optional NO-side rule-set (directional mode). When non-empty alongside
    # crypto15m_rules, the rules PICK the side: crypto15m_rules match → buy UP,
    # crypto15m_rules_no match → buy DOWN (instead of direction_mode choosing).
    # This is how the imported Turbine strategies express their entryYes/entryNo.
    "crypto15m_rules_no": [],
    "crypto15m_time_delay_min": 8.0,   # only act in the last N minutes of the window
    "crypto15m_entry_threshold": 0.95, # favorite side must be >= this probability
    "crypto15m_entry_max": 0.98,       # skip contracts already priced above this
    "crypto15m_exit_threshold": 0.40,  # stop-loss: sell if the held side falls to this
    # Per-bet take-profit: once the held side's best BID reaches this price
    # (fraction 0–1, e.g. 0.95 = 95¢) the executor CASHES OUT — crosses the
    # book to sell while it can still get the high price, instead of riding the
    # binary to settlement and risking a late reversal back to 0. Honest by
    # design: it triggers on the real best bid (what you can actually sell for),
    # not the lagged Gamma mid. 0 = off (hold to settlement / stop-loss only).
    "crypto15m_take_profit": 0.0,
    # Per-bet stop-loss as a FRACTION of entry cost (0–1, e.g. 0.20 = exit at -20%
    # from what the position cost). Applies to every timeframe (5m/15m/hourly) and
    # works alongside crypto15m_exit_threshold (the price stop) — whichever hits
    # first exits. 0 = off.
    "crypto15m_stop_loss_pct": 0.0,
    # Per-bet take-profit as a FRACTION above entry cost (0–1, e.g. 0.20 = cash
    # out at +20%). Mirror of crypto15m_stop_loss_pct on the win side: fires on
    # the real best BID (what you can actually sell for), so it only triggers
    # when the profit is genuinely realizable. Works alongside
    # crypto15m_take_profit (the absolute price target) — whichever hits first
    # sells. 0 = off.
    "crypto15m_take_profit_pct": 0.0,
    "crypto15m_min_delta_pct": 0.0,    # required |open-live|/open underlying move (0 = off)
    "crypto15m_entry_diff": 0.02,      # limit markup through the spread (taker style)
    # Entry style. Default "taker": cross the ask (+entry_diff markup) so the
    # order ALWAYS fills, paying the spread + Polymarket's taker fee. We default
    # to filling because an unfilled no-fee maker is a SKIPPED (often profitable)
    # trade — a filled trade beats a free non-fill. "maker" rests a limit at the
    # favorite's best bid (no spread, ~0 fee) but only fills if the market trades
    # down to it, and unfilled entries are canceled near close — choose it if you
    # would rather miss fills than pay the spread.
    "crypto15m_entry_style": "taker",   # "taker" (always fills) | "maker" (no-fee, may not fill)
    # Use a marketable FAK (Fill-And-Kill) order for taker entries + maker→taker
    # escalation: fills now against resting liquidity or kills the remainder,
    # instead of resting a GTC cross that can drift and mis-cancel. On by default;
    # False reverts to the legacy GTC cross. (Maker entries always rest as GTC.)
    "crypto15m_taker_fak": True,
    "crypto15m_maker_cancel_min": 1.0,  # cancel unfilled entries with < N min to close (0 = at close)
    # When a maker entry is still unfilled at the cancel lead, cross the spread
    # (taker) to grab the fill before close instead of abandoning it — capped at
    # entry_max so it never chases an already-decided contract.
    "crypto15m_maker_escalate": True,
    # Fast escalate: how long (seconds) to let a resting no-fee maker entry try
    # to fill before crossing to a taker fill. The old behavior only escalated
    # at the cancel lead, so a maker that didn't fill sat the whole window while
    # the price drifted, then took a late/worse entry. With this >0, if the
    # maker hasn't filled within N seconds we cross NOW (still near our entry,
    # still capped at entry_max). 0 = disable (cancel-lead escalation only).
    # Kept short on purpose: an unfilled maker that just dead-cancels is a SKIPPED
    # cycle (a missed, often-profitable trade), so we'd rather pay the fee and get
    # in. Granularity is bounded by crypto15m_poll_sec (~4s); the default of 4
    # means "try the no-fee maker for one poll, then cross to taker on the next".
    "crypto15m_maker_fill_sec": 4.0,
    # Only enter between these UTC hours (start == end, or 0..24 = always).
    # The collected data suggests US afternoon/evening (12-24 UTC) is the
    # losing stretch, but not yet significantly — off by default.
    "crypto15m_hours_start_utc": 0,
    "crypto15m_hours_end_utc": 24,
    # Executor (Phase 2): OFF by default, fully INDEPENDENT of the main bot.
    # crypto15m_enabled ON = places REAL orders when the wallet is connected
    # (there is no paper mode).
    "crypto15m_enabled": False,
    # Bet sizing. "fixed" buys `crypto15m_order_size` contracts every
    # entry; "balance_pct" instead spends `crypto15m_balance_pct` of the
    # account balance at the entry price. Either way, when
    # `crypto15m_max_loss_pct` > 0 the capital put at risk on a single bet
    # (contracts x price — the most a buy-only binary can lose) is capped
    # to that fraction of balance.
    "crypto15m_sizing_mode": "fixed",   # "fixed" | "balance_pct"
    "crypto15m_order_size": 5,          # contracts per entry (the app's floor is 5)
    "crypto15m_balance_pct": 0.02,      # fraction of balance per bet (balance_pct mode)
    "crypto15m_max_loss_pct": 0.0,      # cap risk/bet to this fraction of balance (0 = off)
    # Streak sizing: scale the NEXT bet by recent settled results, on top of
    # the base size above. Each consecutive loss multiplies size by
    # (1 + streak_loss_pct/100), compounding (20%: 2 losses -> 1.44x); each
    # consecutive win by (1 + streak_win_pct/100) (0 = a win resets to base;
    # negative shrinks). The multiplier is derived from settled positions on
    # this network at entry time and capped at streak_max_mult x base. This
    # reshapes VARIANCE, not edge — recorded 15m outcomes are serially
    # uncorrelated (2026-07-27 probe: P(win|loss)=75.5% vs P(win|win)=74.6%,
    # runs-test |z|<1.5 on all 7 assets) — so martingale-style settings
    # recover losses more often but lose bigger when a streak runs. Every
    # existing risk cap (max_loss %/bet, affordability, daily/lifetime loss
    # limits) still bounds the scaled size.
    "crypto15m_streak_sizing": False,
    "crypto15m_streak_loss_pct": 20.0,  # % size change per consecutive loss
    "crypto15m_streak_win_pct": 0.0,    # % size change per consecutive win
    "crypto15m_streak_max_mult": 4.0,   # hard cap, multiple of base size
    "crypto15m_max_concurrent": 7,
    # Daily loss circuit-breaker (independent of the main bot's stop_loss_on_day).
    # When today's realized crypto15m P&L drops to/below this (negative) number,
    # the executor stops opening NEW positions for the day; open ones are still
    # managed to settlement. 0 = disabled.
    "crypto15m_daily_loss_limit": -50.0,
    # Lifetime / cumulative realized-loss guard (durable — SURVIVES a stats
    # wipe). Every daily breaker resets at UTC midnight, so a net-negative
    # engine can bleed the daily cap every single day with no ceiling. This one
    # caps TOTAL drawdown: once cumulative realized crypto15m loss reaches
    # `crypto15m_lifetime_loss_limit_pct` of start_bankroll_usd (default 0.5 =
    # 50%), the executor stops opening NEW positions; open ones still ride to
    # settlement. The absolute-dollar override takes precedence when > 0.
    # pct 0 AND usd 0 = disabled. Re-arm by raising the limit in Settings.
    "crypto15m_lifetime_loss_limit_pct": 0.5,
    "crypto15m_lifetime_loss_limit_usd": 0.0,
    # Overall take-profit (the kill-switch). A POSITIVE dollar target measured
    # from the moment you ENABLE it (the engine snapshots realized crypto15m
    # P&L when the target is first set, then tracks the gain from that
    # baseline). Once the gain since enabling reaches this many dollars the
    # crypto engine TURNS ITSELF OFF — it stops opening NEW positions and flips
    # crypto15m_enabled off (the UI toggle reflects it); open positions are
    # still managed to settlement. 0 = disabled. Re-enabling re-arms a fresh
    # baseline from the current P&L.
    "crypto15m_take_profit_total": 0.0,
    "crypto15m_poll_sec": 4,
    "crypto15m_direction_mode": "favorite",  # "favorite" | "contrarian" | "model"
    # ── Settlement sniper (model direction mode) ─────────────────────
    # The terminal-spot model calls P(up at close) from LIVE spot + realized
    # 1-min vol; entries need model certainty ≥ min_prob AND fee-net edge vs
    # the (lagging) book ≥ min_edge_cents. Interval-agnostic: works on the
    # 5m / 15m / hourly windows alike.
    "crypto15m_model_min_prob": 0.97,
    "crypto15m_model_min_edge_cents": 2.0,
    # Allow final-minute strikes (3-sigma gate + live-WS-spot + runway guard).
    "crypto15m_model_final_minute": True,
    # Auto-pause model entries when the rolling hit rate's Wilson lower bound
    # drops below break-even (resumes with hysteresis).
    "crypto15m_model_autopause": True,
    # Divergence guard: outside the final minute, skip when the model claims
    # MORE fee-net edge than this against a live book (a live book that
    # disagrees this much usually knows the strike better than we do).
    # 0 = off.
    "crypto15m_model_max_book_gap_cents": 25.0,
    # Final-minute version of the guard. Books genuinely gap 20-67c in the
    # last minute (that lag is the FM snipe's edge), so this cap is looser —
    # but NOT absent: an unlimited exemption bought 1000 BTC-down shares at
    # 1c on a ~98c "edge" (user report 2026-07-16), a disagreement so extreme
    # it is almost always a fabricated strike or stale spot. 0 = off.
    "crypto15m_model_fm_max_book_gap_cents": 75.0,
    # Size UP to the venue's $1 marketable-order minimum instead of skipping
    # cheap-ask entries (a 13c signal at 5 contracts is $0.65 and gets
    # rejected). Off by default: it can place MORE contracts than
    # crypto15m_order_size (dollar risk stays ~$1).
    "crypto15m_autosize_to_min_notional": False,
    # Live Coinbase WebSocket spot feed (the model's low-latency price input;
    # REST chain stays as the per-asset fallback).
    "crypto15m_spot_ws": True,
    # Sell-into-strength: rest a GTC ask at this target the moment a position
    # fills (maker, catches inter-tick spikes); eligible only when the target
    # clears the entry price, so high-entry sniper bets are naturally exempt.
    "crypto15m_sell_into_strength": False,
    "crypto15m_sell_strength_cents": 80.0,
    # Passively log each quarter's signal + outcome so the 15m strategy is
    # backtestable (read-only, no orders). On by default — it's how anyone
    # builds data to test their own settings.
    "crypto15m_record_signals": True,
    # Collect whale/momentum signals even with trading OFF (the main-engine
    # backtest dataset). When enable_trading is ON, scanning stays on
    # regardless — the engine can't follow signals it never sees.
    "main_record_signals": True,
    # Your Polymarket US taker rebate tier, in % of taker fees rebated (0, 3, 5,
    # 10, 15 or 30, by 30-day taker volume; docs.polymarket.us/fees). Fee
    # ESTIMATES (edge gates, backtests) net it out; realised P&L does not.
    "taker_rebate_pct": 0.0,

    # ── User strategy scripts (Scripts tab) ───────────────────────────
    # Scripts themselves live in the user_scripts DB table (they're code,
    # not config); these are the engine master switch and the money rails
    # that apply to EVERY script. Scripts run as full Python (no language
    # sandbox) — these rails are the money boundary, and they are not
    # negotiable from inside a script.
    "scripts_live_enabled": False,      # master: scripts may place REAL orders
                                        # (shadow scripts tick regardless)
    "script_poll_sec": 5,               # engine cadence (shares the 3s-cached snapshot)
    # Wall-clock ceiling for ONE hook call. Scripts run untraced, so this
    # thread timeout is the only thing between a runaway hook and a frozen
    # master loop — and the loop, including every open position's exit pass,
    # waits this long behind the script. Raise only for a hook that genuinely
    # must do something slow.
    "script_hook_timeout_sec": 1.0,
    "script_max_entry_cents": 97,       # hard cap on any script submit price
    "script_max_contracts": 20,         # per-order size cap
    "script_max_open": 2,               # open positions per script
    "script_daily_loss_usd": 25.0,      # per-script realized daily loss → auto-disable (0 = off)
    "script_max_enabled": 10,           # concurrently enabled scripts (loop-starvation guard)
    # decide_market(): the general-market universe scanned per tick. Each
    # market costs one sandboxed hook call PER SCRIPT, so this is deliberately
    # small — raise it only if your scripts need a wider net.
    # Markets handed to decide_market per tick, highest 24h volume first. 20
    # was far too small: price and volume are correlated, so a strategy hunting
    # a specific price band never sees its targets — measured on the live board,
    # the best-ranked sports deep-favourite sat at rank 66 and the median at
    # 832. Each market costs one sandboxed hook call per script, so this trades
    # coverage against CPU; raise it if a script needs a wider net.
    # Manual terminal: one switch that stops it talking to ANY provider.
    # The privacy panel names every host verbatim; this is the control that
    # makes the claim checkable. The app still works with it on — degraded,
    # and honest about exactly what is missing.
    "terminal_offline_mode": False,
    # Manual terminal: route hand-placed orders to the PAPER simulator instead
    # of the exchange. Its own tables and its own totals — a paper number is
    # never added to a real one.
    # Desktop notification when a standing instruction fires. Deliberately a
    # DESKTOP notification and not a webhook: a webhook posts your positions to
    # someone else's server on every fire; this stays on the machine.
    "terminal_notifications": True,
    # ── AI market analysis (manual terminal) ─────────────────────────────
    # Off, and keyless, until the user says otherwise. On-demand only: a
    # button on the Market page, never a poll and never an engine tick, so
    # the user's provider bill is a function of clicks and nothing else.
    #
    # What leaves the machine is PUBLIC market data — question, rules text,
    # quote, volume, close time. Never the wallet, never a position. That is
    # enforced by ai_analysis.build_payload taking a market rather than an
    # account, not by a policy.
    #
    # The model is never asked for, and structurally cannot return, a
    # probability or fair value. It returns a directional lean from a fixed
    # four-word vocabulary plus its argument. See ai_analysis's docstring.
    "ai_enabled": False,
    # anthropic | openai | openrouter | gemini | ollama | lmstudio
    # (ai_providers.PROVIDERS). Keys live in ai_secrets, never here.
    "ai_provider": "anthropic",
    # ONE model id, for whichever provider is selected (the analysis and
    # Autopilot both use it). Replaces ai_anthropic_model / ai_openai_model,
    # migrated in _migrate_ai. Shape-checked per provider; a bad id falls to
    # that provider's default. OpenAI's default is "" on purpose — its ids
    # move faster than this file, so Settings loads the real list with the
    # user's own key — and LM Studio's "" means "the model it has loaded".
    "ai_model": "claude-opus-5-5",
    # Server-side web search. Materially better on event markets — without it
    # the model is answering from a training cutoff and says so — but it costs
    # extra per search and adds latency, so it is opt-in like everything else.
    "ai_web_search": False,
    # ── Autopilot: the app runs one of the user's agents on a timer, with
    # the user's own AI key (autopilot.py) ──────────────────────────────────
    # The ONE place an AI call happens without a click, so it is off and has
    # three budgets of its own, all checked before every call. Every tool it
    # calls goes through mcp_tools.call with every rail an MCP client has.
    "autopilot_enabled": False,
    "autopilot_interval_min": 60,
    "autopilot_max_runs_per_day": 12,
    "autopilot_daily_token_budget": 1_500_000,
    "autopilot_max_steps": 15,
    "autopilot_mission": "",
    # Which named agent (mcp_agents) Autopilot runs as: its guide shapes the
    # prompt and its rules bind every order. A deleted / switched-off agent
    # blocks Autopilot — it never falls back to Default.
    "autopilot_agent_id": "default",
    # ── remote control (Discord / Telegram bots) ─────────────────────────
    # Off by default, and stays off until the user pairs an account. What
    # leaves the machine is stated on the setup screen: positions and P&L, to
    # Discord's or Telegram's servers.
    "remote_enabled": False,
    "remote_discord_enabled": False,
    "remote_telegram_enabled": False,
    # A SECOND switch, because placing orders from a phone has a far bigger
    # blast radius than reading from one. Never rides along with remote_enabled.
    "remote_trading_enabled": False,
    # Hard ceiling on a single chat-placed order, enforced in the backend.
    "remote_max_order_usd": 100.0,
    # ── App-wide Paper | Live (account_mode) ─────────────────────────────
    # paper: the terminal, the phone, standing rules and every agent trade the
    # paper book, and the engines (main, crypto, copy, Perps, live scripts)
    # send nothing — their own switches are kept, the effective switch is
    # `switch AND account live` (engine_on). Only the user changes it; an
    # agent never can. A config without it is migrated in _migrate_account.
    "account_mode": "paper",
    # DERIVED from account_mode in _validate_config — never set on its own.
    # Matches account_mode here too: the backend runs on this raw dict until
    # the first setConfig lands, and a reader of this key must not see Live.
    "terminal_paper_mode": True,
    # The account's paper bankroll (the terminal's 'manual' paper book).
    "terminal_paper_bankroll_usd": 1000.0,
    # ── AI agents over MCP (Claude, Cursor, Codex, …) ────────────────────
    # A local MCP server so an AI client on THIS machine can read markets,
    # paper trade, backtest and — behind a second switch — trade for real.
    # Listens on 127.0.0.1 only, behind a bearer token. See mcp_server.py.
    "mcp_enabled": False,
    "mcp_port": 47635,
    # The master switch for what agents may trade: off | paper | live. Paper
    # fills come out of the real order book into each agent's own paper
    # account. Under live, only agents whose own mode is live trade real money
    # (mcp_agents.effective_mode). Never rides along with mcp_enabled.
    "mcp_trade_mode": "paper",
    # Hard ceilings on what agents may SPEND, all agents together, enforced in
    # the backend. Each named agent's own caps can only narrow these. Exits
    # are never capped: a cap on getting out is a cap on safety. 0 = no cap.
    "mcp_max_order_usd": 25.0,
    "mcp_daily_spend_usd": 100.0,
    # Open positions across every agent, and the buy-side edge an agent's own
    # forecast must clear after Polymarket's fee.
    "mcp_max_positions": 10,
    "mcp_min_edge_cents": 3.0,
    # Daily loss stop: realised today plus open losses at the bid, all agents.
    # At the limit, agent BUYS stop for the UTC day; exits never do.
    "mcp_daily_loss_usd": 50.0,
    # "Ask me first": a live agent order waits for the user's approval in the
    # app or by phone. On unless explicitly turned off.
    "mcp_live_approval": True,
    # The user's named agents (mcp_agents.py). None = just the built-in
    # Default agent, which holds the original token.
    "mcp_agents": None,
    # Whole-account powers an agent never has by default: switching engines
    # on, arming scripts or the crypto schedule live, changing engine settings
    # that spend, flattening the wallet, the Perps maker's live actions.
    "mcp_allow_live_switches": False,
    # Scripts run as full Python in the process holding the wallet key, so
    # letting an agent write and run them is its own switch.
    "mcp_scripts_enabled": False,
    # Each agent's own paper account — separate from yours.
    "mcp_paper_bankroll_usd": 1000.0,
    # The plain HTTP API (/api/v1) on the MCP listener, for scripts, n8n or
    # LangChain. Same token, same rails, same audit as MCP. Off by default.
    "mcp_http_enabled": False,
    "script_market_limit": 150,
    # 24h volume floor for the universe. Off: Polymarket US's market list has no
    # volume figure, so any floor above 0 empties the universe.
    "script_market_min_volume": 0.0,
    # Max real bid/ask spread a decide_market entry may cross, checked against
    # the live CLOB quote at submit (0 = off). The market rows a script sees
    # carry a single price, so this is the only honest place to gate it.
    "script_market_max_spread_cents": 2.0,

    # ── Discord ───────────────────────────────────────────────────────
    "stats_chart_window_hours": 168,
}


# ── 15-minute crypto strategy presets ─────────────────────────────────
# Config bundles for the crypto15m executor, first tuned on 531 settled
# 15-min markets (fee-aware replay, 2026-06). Re-measured 2026-09-01 on
# the full recorded corpus (zero spread charged): favorite-follow
# -2.06c/contract (n=10,373, t=-5.3), contrarian -3.80c (t=-9.7); the
# 95-100c band is +0.10c (t=0.3 before clustering by close, which only
# widens it), statistically zero and negative once
# the half-spread is added; the 5-minute corpus (Jul 22-Aug 31, a clean
# out-of-sample window) is negative in every band. These are example
# configurations, not edges. Paper-trade before going live.
CRYPTO15M_PRESETS: list[dict[str, Any]] = [
    {
        "id": "c15-favorite",
        "name": "Deep Favorite",
        "tagline": "Only the deepest favorites: buy at 95-98c.",
        "description": (
            "Buy the favorite only when it is already >=95c. The June "
            "replay of 531 markets showed +2.8c/contract on 16 wins; the "
            "full corpus (2026-09-01, n=10,373) measures favorite-follow "
            "at -2.06c/contract and the 95-100c band at +0.10c (t=0.3, "
            "unclustered) — statistically zero, and negative once the "
            "half-spread is added. One loss at 97c wipes out ~35 wins. Example "
            "configuration, not an edge. Paper-trade first."
        ),
        "config": {
            "crypto15m_direction_mode": "favorite",
            "crypto15m_entry_threshold": 0.95,
            "crypto15m_entry_max": 0.98,
            "crypto15m_exit_threshold": 0.40,
            "crypto15m_entry_style": "taker",
        },
    },
    {
        "id": "c15-contrarian",
        "name": "Contrarian Fade",
        "tagline": "Fade extreme favorites — buy the cheap underdog.",
        "description": (
            "When a side is an extreme favorite (>=90c), buy the CHEAP "
            "opposite side, betting the 15-minute move reverts before "
            "close. Low win rate, high payoff (longshot); holds to "
            "settlement, no stop. An early, small sample read break-even "
            "(+1.0c/contract, n=64); the full corpus (2026-09-01, n=10,373) "
            "measures -3.80c/contract (t=-9.7), the worst number in the "
            "corpus. Not recommended. Paper-trade hard."
        ),
        "config": {
            "crypto15m_direction_mode": "contrarian",
            "crypto15m_entry_threshold": 0.90,
            "crypto15m_entry_max": 0.98,
            "crypto15m_exit_threshold": 0.0,
            "crypto15m_entry_style": "taker",
        },
    },
]


def crypto15m_preset_config(preset_id: str) -> dict[str, Any] | None:
    for p in CRYPTO15M_PRESETS:
        if p["id"] == preset_id:
            return dict(p["config"])
    return None


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch.lower() if ch.isupper() else ch)
    return "".join(out)


def _as_float(v: Any, default: float) -> float:
    """Best-effort float, falling back to `default` on junk/NaN."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f  # NaN guard


def _clampf(v: Any, lo: float, hi: float, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _clampi(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


_FRACTION_KEYS = [
    "base_size_fraction", "min_size_fraction", "max_size_fraction",
    "min_cash_reserve_fraction", "max_total_exposure_fraction",
]
_UNIT_KEYS = [
    "crypto15m_entry_threshold", "crypto15m_entry_max", "crypto15m_exit_threshold",
    "crypto15m_take_profit", "crypto15m_stop_loss_pct", "crypto15m_take_profit_pct",
    "crypto15m_min_delta_pct", "crypto15m_entry_diff", "min_entry_price_frac",
    "take_profit_pct",
]


def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Clamp money-critical knobs to sane ranges BEFORE they can drive an
    order. The renderer is trusted-ish, but a malformed value (e.g. a
    size fraction of 5.0) must never reach the sizing path uncorrected."""
    d = DEFAULT_CONFIG

    # ── Legacy paper-mode migration ───────────────────────────────────
    # Paper/dry-run was removed: each engine's enable switch now means "trade
    # for real". DISARM anyone who was in paper so an upgrade can't silently
    # flip them live. A user who was already LIVE stays armed.
    #   • main bot: was paper if dry_run was True -> turn enable_trading OFF.
    #   • crypto15m / copy: "enabled" used to mean paper; only "*_live" meant
    #     real. So enabled-without-live was paper -> turn the engine OFF.
    if "dry_run" in cfg:
        if bool(cfg.get("dry_run")):
            cfg["enable_trading"] = False
        cfg.pop("dry_run", None)
    if "crypto15m_live" in cfg:
        if cfg.get("crypto15m_enabled") and not cfg.get("crypto15m_live"):
            cfg["crypto15m_enabled"] = False
        cfg.pop("crypto15m_live", None)

    for k in _FRACTION_KEYS:
        cfg[k] = _clampf(cfg.get(k), 0.0, 1.0, d[k])
    for k in _UNIT_KEYS:
        cfg[k] = _clampf(cfg.get(k), 0.0, 1.0, d[k])
    if cfg["min_size_fraction"] > cfg["max_size_fraction"]:
        cfg["min_size_fraction"] = cfg["max_size_fraction"]
    if cfg.get("sizing_mode") not in ("percent", "contracts"):
        cfg["sizing_mode"] = d["sizing_mode"]
    cfg["min_contracts"] = _clampi(cfg.get("min_contracts"), 1, 1_000_000, d["min_contracts"])
    cfg["max_contracts"] = _clampi(cfg.get("max_contracts"), 1, 1_000_000, d["max_contracts"])
    cfg["max_contracts"] = max(cfg["min_contracts"], cfg["max_contracts"])

    cfg["hard_max_position_usd"] = _clampf(cfg.get("hard_max_position_usd"), 0.0, 1e9, d["hard_max_position_usd"])
    cfg["min_entry_price_cents"] = _clampi(cfg.get("min_entry_price_cents"), 1, 99, d["min_entry_price_cents"])
    cfg["max_entry_price_cents"] = _clampi(cfg.get("max_entry_price_cents"), 1, 99, d["max_entry_price_cents"])
    if cfg["min_entry_price_cents"] > cfg["max_entry_price_cents"]:
        cfg["min_entry_price_cents"], cfg["max_entry_price_cents"] = (
            cfg["max_entry_price_cents"], cfg["min_entry_price_cents"],
        )
    # Main-bot custom entry rules (rule builder).
    cfg["use_rules"] = bool(cfg.get("use_rules", False))
    cfg["rules"] = rules_engine.sanitize_rules(cfg.get("rules"), BOT_RULE_FIELDS)
    cfg["max_open_positions"] = _clampi(cfg.get("max_open_positions"), 0, 100_000, d["max_open_positions"])
    cfg["max_resolution_days"] = _clampi(cfg.get("max_resolution_days"), 0, 100_000, d["max_resolution_days"])
    cfg["max_daily_new_positions"] = _clampi(cfg.get("max_daily_new_positions"), 0, 100_000, d["max_daily_new_positions"])
    cfg["max_positions_per_event"] = _clampi(cfg.get("max_positions_per_event"), 1, 100_000, d["max_positions_per_event"])
    # stop-loss must be <= 0, take-profit >= 0 (a positive stop / negative target is nonsense).
    # A user who types a positive "max daily loss" (e.g. 100) means -100, NOT
    # "disable" — coerce positives to their negative magnitude so a stray sign
    # can never silently turn the circuit-breaker OFF. 0 still = disabled.
    cfg["stop_loss_on_day"] = _clampf(
        -abs(_as_float(cfg.get("stop_loss_on_day"), d["stop_loss_on_day"])),
        -1e9, 0.0, d["stop_loss_on_day"],
    )
    cfg["take_profit_on_day"] = _clampf(cfg.get("take_profit_on_day"), 0.0, 1e9, d["take_profit_on_day"])
    cfg["flatten_on_daily_stop"] = bool(cfg.get("flatten_on_daily_stop", d["flatten_on_daily_stop"]))

    # ── Loop cadences ─────────────────────────────────────────────────
    # Every periodic task gates on `now - last >= interval`. A 0/negative value
    # makes that always-true → the section fires every loop tick (API hammering →
    # rate-limit/ban on a real-money account); a non-numeric string makes the
    # comparison raise so the section silently never runs. Clamp all of them to
    # sane positive bounds. (crypto15m_maker_fill_sec already
    # clamped below.)
    for _k, _lo, _hi in [
        ("crypto15m_poll_sec", 2, 3600),
        ("trade_scan_interval", 5, 86_400),
        ("position_poll_interval", 5, 86_400),
        ("balance_poll_interval", 5, 86_400),
        ("max_signal_age_sec", 10, 86_400),
        ("order_expiration_sec", 10, 86_400),
        ("resolution_check_interval", 10, 86_400),
        ("whale_scan_interval", 10, 86_400),
        ("momentum_scan_interval", 10, 86_400),
        ("market_refresh_interval", 30, 86_400),
        ("db_cleanup_interval", 60, 604_800),
    ]:
        cfg[_k] = _clampi(cfg.get(_k), _lo, _hi, d[_k])

    cfg["crypto15m_enabled"] = bool(cfg.get("crypto15m_enabled", False))
    cfg["crypto15m_order_size"] = _clampi(cfg.get("crypto15m_order_size"), 1, 10_000, d["crypto15m_order_size"])
    cfg["crypto15m_max_concurrent"] = _clampi(cfg.get("crypto15m_max_concurrent"), 1, 50, d["crypto15m_max_concurrent"])
    # Loss limit must be <= 0; same positive-magnitude coercion as the main
    # bot's stop-loss so a positive entry can't disable the circuit-breaker.
    cfg["crypto15m_daily_loss_limit"] = _clampf(
        -abs(_as_float(cfg.get("crypto15m_daily_loss_limit"), d["crypto15m_daily_loss_limit"])),
        -1e9, 0.0, d["crypto15m_daily_loss_limit"],
    )
    # Lifetime cumulative-loss guard: pct in [0,1] of the start bankroll; the
    # absolute-$ override is a non-negative dollar amount (0 = use the pct).
    cfg["crypto15m_lifetime_loss_limit_pct"] = _clampf(
        cfg.get("crypto15m_lifetime_loss_limit_pct"), 0.0, 1.0,
        d["crypto15m_lifetime_loss_limit_pct"],
    )
    cfg["crypto15m_lifetime_loss_limit_usd"] = _clampf(
        cfg.get("crypto15m_lifetime_loss_limit_usd"), 0.0, 1e9,
        d["crypto15m_lifetime_loss_limit_usd"],
    )
    cfg["lifetime_loss_limit_pct"] = _clampf(
        cfg.get("lifetime_loss_limit_pct"), 0.0, 1.0, d["lifetime_loss_limit_pct"],
    )
    cfg["lifetime_loss_limit_usd"] = _clampf(
        cfg.get("lifetime_loss_limit_usd"), 0.0, 1e9, d["lifetime_loss_limit_usd"],
    )
    # Overall take-profit target must be >= 0 (a positive dollar goal; 0 = off).
    # Same family as the main bot's take_profit_on_day.
    cfg["crypto15m_take_profit_total"] = _clampf(
        cfg.get("crypto15m_take_profit_total"), 0.0, 1e9, d["crypto15m_take_profit_total"],
    )
    if cfg.get("crypto15m_sizing_mode") not in ("fixed", "balance_pct"):
        cfg["crypto15m_sizing_mode"] = d["crypto15m_sizing_mode"]
    cfg["crypto15m_balance_pct"] = _clampf(cfg.get("crypto15m_balance_pct"), 0.0, 1.0, d["crypto15m_balance_pct"])
    cfg["crypto15m_max_loss_pct"] = _clampf(cfg.get("crypto15m_max_loss_pct"), 0.0, 1.0, d["crypto15m_max_loss_pct"])
    # Streak sizing: per-step % changes bounded to sane compounding inputs
    # (-90% halts a runaway shrink before 0; +300%/step is already extreme);
    # the cap must be at least 1x or it would shrink the BASE size.
    cfg["crypto15m_streak_sizing"] = bool(
        cfg.get("crypto15m_streak_sizing", d["crypto15m_streak_sizing"]))
    cfg["crypto15m_streak_loss_pct"] = _clampf(
        cfg.get("crypto15m_streak_loss_pct"), -90.0, 300.0, d["crypto15m_streak_loss_pct"])
    cfg["crypto15m_streak_win_pct"] = _clampf(
        cfg.get("crypto15m_streak_win_pct"), -90.0, 300.0, d["crypto15m_streak_win_pct"])
    cfg["crypto15m_streak_max_mult"] = _clampf(
        cfg.get("crypto15m_streak_max_mult"), 1.0, 100.0, d["crypto15m_streak_max_mult"])
    # Entry window can't exceed the traded window's length: 5 min on the 5m
    # series, 15 on 15m, 60 on hourly. The old flat 15-min ceiling silently
    # rewrote an hourly strategy's 30-minute entry window to 15 — half the
    # strategy unreachable with no error anywhere.
    _iv_raw = str(cfg.get("crypto15m_interval", d["crypto15m_interval"]))
    _iv_max_min = {"5m": 5.0, "15m": 15.0, "hourly": 60.0}.get(_iv_raw, 15.0)
    cfg["crypto15m_time_delay_min"] = _clampf(
        cfg.get("crypto15m_time_delay_min"), 0.0, _iv_max_min,
        min(float(d["crypto15m_time_delay_min"]), _iv_max_min))
    if cfg.get("crypto15m_direction_mode") not in ("favorite", "contrarian", "model"):
        cfg["crypto15m_direction_mode"] = d["crypto15m_direction_mode"]
    cfg["crypto15m_model_min_prob"] = _clampf(
        cfg.get("crypto15m_model_min_prob"), 0.5, 1.0, d["crypto15m_model_min_prob"])
    cfg["crypto15m_model_min_edge_cents"] = _clampf(
        cfg.get("crypto15m_model_min_edge_cents"), 0.0, 50.0,
        d["crypto15m_model_min_edge_cents"])
    cfg["crypto15m_model_final_minute"] = bool(
        cfg.get("crypto15m_model_final_minute", d["crypto15m_model_final_minute"]))
    cfg["crypto15m_model_max_book_gap_cents"] = _clampf(
        cfg.get("crypto15m_model_max_book_gap_cents"), 0.0, 100.0,
        d["crypto15m_model_max_book_gap_cents"])
    cfg["crypto15m_model_fm_max_book_gap_cents"] = _clampf(
        cfg.get("crypto15m_model_fm_max_book_gap_cents"), 0.0, 100.0,
        d["crypto15m_model_fm_max_book_gap_cents"])
    cfg["crypto15m_model_autopause"] = bool(
        cfg.get("crypto15m_model_autopause", d["crypto15m_model_autopause"]))
    cfg["crypto15m_spot_ws"] = bool(cfg.get("crypto15m_spot_ws", d["crypto15m_spot_ws"]))
    cfg["crypto15m_sell_into_strength"] = bool(
        cfg.get("crypto15m_sell_into_strength", d["crypto15m_sell_into_strength"]))
    cfg["crypto15m_sell_strength_cents"] = _clampf(
        cfg.get("crypto15m_sell_strength_cents"), 50.0, 99.0,
        d["crypto15m_sell_strength_cents"])
    cfg["main_record_signals"] = bool(cfg.get("main_record_signals", d["main_record_signals"]))
    # AI analysis. The provider name reaches a dispatch table, so an unknown
    # value falls back to the default rather than being passed along.
    cfg["ai_enabled"] = bool(cfg.get("ai_enabled", d["ai_enabled"]))
    cfg["ai_web_search"] = bool(cfg.get("ai_web_search", d["ai_web_search"]))
    import ai_providers as _aip
    _p = str(cfg.get("ai_provider") or "").strip().lower()
    cfg["ai_provider"] = _p if _p in _aip.PROVIDERS else d["ai_provider"]
    # A model id is echoed into a request body (and Gemini's into a URL
    # path): shape-checked per provider, else that provider's default, so a
    # hand-edited config degrades to a working call instead of a 404.
    cfg["ai_model"] = _aip.clean_model(cfg["ai_provider"], cfg.get("ai_model"))
    # Autopilot. Clamped like kryptrader's; only an explicit True turns it on,
    # so a mangled value never starts spending the user's key on a timer.
    cfg["autopilot_enabled"] = cfg.get("autopilot_enabled", False) is True
    cfg["autopilot_interval_min"] = _clampi(
        cfg.get("autopilot_interval_min"), 15, 1440, d["autopilot_interval_min"])
    cfg["autopilot_max_runs_per_day"] = _clampi(
        cfg.get("autopilot_max_runs_per_day"), 1, 96, d["autopilot_max_runs_per_day"])
    cfg["autopilot_daily_token_budget"] = _clampi(
        cfg.get("autopilot_daily_token_budget"), 50_000, 50_000_000,
        d["autopilot_daily_token_budget"])
    cfg["autopilot_max_steps"] = _clampi(
        cfg.get("autopilot_max_steps"), 3, 40, d["autopilot_max_steps"])
    cfg["autopilot_mission"] = str(cfg.get("autopilot_mission") or "")[:2000]
    import mcp_agents as _mag
    _aid = str(cfg.get("autopilot_agent_id") or "").strip().lower()
    # Shape only. NOT repaired to Default when that agent is gone: Autopilot
    # refuses to run as a deleted agent (autopilot.blocked_reason) rather than
    # quietly running as one with different — usually looser — rules.
    cfg["autopilot_agent_id"] = (_aid if (_aid == _mag.DEFAULT_ID or _mag.valid_id(_aid))
                                 else _mag.DEFAULT_ID)
    if cfg.get("crypto15m_entry_style") not in ("maker", "taker"):
        cfg["crypto15m_entry_style"] = d["crypto15m_entry_style"]
    cfg["crypto15m_taker_fak"] = bool(cfg.get("crypto15m_taker_fak", True))
    if cfg.get("crypto15m_interval") not in ("5m", "15m", "hourly"):
        cfg["crypto15m_interval"] = d["crypto15m_interval"]
    aw = cfg.get("crypto15m_assets")
    if isinstance(aw, list):
        valid = {a.upper() for a in CRYPTO15M_ASSETS}
        cfg["crypto15m_assets"] = [
            a.upper() for a in aw if isinstance(a, str) and a.upper() in valid
        ]
    elif aw is not None:
        cfg["crypto15m_assets"] = None
    cfg["crypto15m_arb_detect"] = bool(cfg.get("crypto15m_arb_detect", True))
    cfg["crypto15m_arb_min_edge_cents"] = _clampf(
        cfg.get("crypto15m_arb_min_edge_cents"), 0.0, 100.0, d["crypto15m_arb_min_edge_cents"],
    )
    cfg["crypto15m_imbalance_detect"] = bool(cfg.get("crypto15m_imbalance_detect", True))
    cfg["crypto15m_imbalance_levels"] = _clampi(
        cfg.get("crypto15m_imbalance_levels"), 1, 20, d["crypto15m_imbalance_levels"],
    )
    cfg["crypto15m_imbalance_gate"] = bool(cfg.get("crypto15m_imbalance_gate", False))
    cfg["crypto15m_imbalance_gate_min"] = _clampf(
        cfg.get("crypto15m_imbalance_gate_min"), 0.0, 1.0, d["crypto15m_imbalance_gate_min"],
    )
    if cfg["crypto15m_imbalance_gate"]:  # the gate needs the imbalance computed
        cfg["crypto15m_imbalance_detect"] = True
    cfg["crypto15m_indicator_detect"] = bool(cfg.get("crypto15m_indicator_detect", True))
    # Direction-aware momentum-confirmation floors (RSI / MACD-histogram on the
    # underlying). Each 0 = off. RSI clamps to 0–100; macdHist magnitude is
    # non-negative (the direction-aware check applies the sign). A floor that's
    # set needs the indicators computed, so force the detector on.
    cfg["crypto15m_min_rsi"] = _clampf(cfg.get("crypto15m_min_rsi"), 0.0, 100.0, d["crypto15m_min_rsi"])
    cfg["crypto15m_min_macd_hist"] = _clampf(cfg.get("crypto15m_min_macd_hist"), 0.0, 1e9, d["crypto15m_min_macd_hist"])
    if cfg["crypto15m_min_rsi"] > 0 or cfg["crypto15m_min_macd_hist"] > 0:
        cfg["crypto15m_indicator_detect"] = True
    cfg["crypto15m_ws_book"] = bool(cfg.get("crypto15m_ws_book", True))
    # Rule builder: sanitize the composed conditions, then auto-enable any
    # detector a rule depends on.
    cfg["crypto15m_use_rules"] = bool(cfg.get("crypto15m_use_rules", False))
    clean_rules = rules_engine.sanitize_rules(cfg.get("crypto15m_rules"), C15_RULE_FIELDS)
    cfg["crypto15m_rules"] = clean_rules
    clean_rules_no = rules_engine.sanitize_rules(cfg.get("crypto15m_rules_no"), C15_RULE_FIELDS)
    cfg["crypto15m_rules_no"] = clean_rules_no
    if cfg["crypto15m_use_rules"]:
        # Auto-enable the optional detectors any field in EITHER rule-set needs.
        rule_fields = {c["field"] for c in clean_rules} | {c["field"] for c in clean_rules_no}
        if "bookImbalance" in rule_fields:
            cfg["crypto15m_imbalance_detect"] = True
        if "arbEdgeCents" in rule_fields:
            cfg["crypto15m_arb_detect"] = True
        if rule_fields & {
            "macd", "macdSignal", "macdHist", "macdCross", "rsi", "sigma1m",
            "vwap1h", "ema12", "sma20", "sma50", "priceVsVwapPct",
            "ema12VsSma20Pct", "ema1VsSma5Pct", "velocity1mPct",
            "change5mPct", "change15mPct",
        }:
            cfg["crypto15m_indicator_detect"] = True
    cfg["crypto15m_maker_cancel_min"] = _clampf(cfg.get("crypto15m_maker_cancel_min"), 0.0, 15.0, d["crypto15m_maker_cancel_min"])
    cfg["crypto15m_maker_escalate"] = bool(cfg.get("crypto15m_maker_escalate", d["crypto15m_maker_escalate"]))
    cfg["crypto15m_maker_fill_sec"] = _clampf(cfg.get("crypto15m_maker_fill_sec"), 0.0, 900.0, d["crypto15m_maker_fill_sec"])
    cfg["crypto15m_hours_start_utc"] = _clampi(cfg.get("crypto15m_hours_start_utc"), 0, 24, d["crypto15m_hours_start_utc"])
    cfg["crypto15m_hours_end_utc"] = _clampi(cfg.get("crypto15m_hours_end_utc"), 0, 24, d["crypto15m_hours_end_utc"])

    cfg["taker_rebate_pct"] = _clampf(cfg.get("taker_rebate_pct"), 0.0, 30.0, 0.0)

    # User-script engine: master switch + money rails (applied to every
    # script — clamp so a mangled config can't silently widen them).
    cfg["scripts_live_enabled"] = bool(cfg.get("scripts_live_enabled", False))
    cfg["script_poll_sec"] = _clampi(cfg.get("script_poll_sec"), 2, 3600, d["script_poll_sec"])
    cfg["script_hook_timeout_sec"] = _clampf(
        cfg.get("script_hook_timeout_sec"), 0.05, 30.0,
        d["script_hook_timeout_sec"])
    cfg["script_max_entry_cents"] = _clampi(
        cfg.get("script_max_entry_cents"), 1, 99, d["script_max_entry_cents"])
    cfg["script_max_contracts"] = _clampi(
        cfg.get("script_max_contracts"), 1, 10_000, d["script_max_contracts"])
    cfg["script_max_open"] = _clampi(cfg.get("script_max_open"), 1, 100, d["script_max_open"])
    cfg["script_daily_loss_usd"] = _clampf(
        cfg.get("script_daily_loss_usd"), 0.0, 1e6, d["script_daily_loss_usd"])
    cfg["script_max_enabled"] = _clampi(
        cfg.get("script_max_enabled"), 1, 50, d["script_max_enabled"])
    cfg["terminal_offline_mode"] = bool(cfg.get("terminal_offline_mode", False))
    cfg["terminal_notifications"] = bool(cfg.get("terminal_notifications", True))
    cfg["remote_enabled"] = bool(cfg.get("remote_enabled", False))
    cfg["remote_discord_enabled"] = bool(cfg.get("remote_discord_enabled", False))
    cfg["remote_telegram_enabled"] = bool(cfg.get("remote_telegram_enabled", False))
    cfg["remote_trading_enabled"] = bool(cfg.get("remote_trading_enabled", False))
    cfg["remote_max_order_usd"] = _clampf(
        cfg.get("remote_max_order_usd"), 0.0, 10_000.0, d["remote_max_order_usd"])
    cfg["mcp_enabled"] = bool(cfg.get("mcp_enabled", False))
    cfg["mcp_port"] = _clampi(cfg.get("mcp_port"), 1024, 65535, d["mcp_port"])
    tm = str(cfg.get("mcp_trade_mode") or "").lower()
    cfg["mcp_trade_mode"] = tm if tm in ("off", "paper", "live") else d["mcp_trade_mode"]
    cfg["mcp_max_order_usd"] = _clampf(
        cfg.get("mcp_max_order_usd"), 0.0, 100_000.0, d["mcp_max_order_usd"])
    cfg["mcp_daily_spend_usd"] = _clampf(
        cfg.get("mcp_daily_spend_usd"), 0.0, 1_000_000.0, d["mcp_daily_spend_usd"])
    cfg["mcp_max_positions"] = _clampi(
        cfg.get("mcp_max_positions"), 0, 500, d["mcp_max_positions"])
    cfg["mcp_min_edge_cents"] = _clampf(
        cfg.get("mcp_min_edge_cents"), 0.0, 50.0, d["mcp_min_edge_cents"])
    cfg["mcp_daily_loss_usd"] = _clampf(
        cfg.get("mcp_daily_loss_usd"), 0.0, 1_000_000.0, d["mcp_daily_loss_usd"])
    # Only an explicit False turns approvals off: an unreadable value must not
    # let live agent orders through unasked.
    cfg["mcp_live_approval"] = cfg.get("mcp_live_approval") is not False
    import mcp_agents
    cfg["mcp_agents"] = mcp_agents.validate_agents(
        cfg.get("mcp_agents"), cfg["mcp_trade_mode"])
    cfg["mcp_allow_live_switches"] = cfg.get("mcp_allow_live_switches") is True
    cfg["mcp_scripts_enabled"] = bool(cfg.get("mcp_scripts_enabled", False))
    cfg["mcp_paper_bankroll_usd"] = _clampf(
        cfg.get("mcp_paper_bankroll_usd"), 1.0, 1e7, d["mcp_paper_bankroll_usd"])
    cfg["mcp_http_enabled"] = cfg.get("mcp_http_enabled") is True
    # Paper unless it says exactly "live": an unreadable value must not send
    # real orders. terminal_paper_mode is DERIVED, so the terminal, phone and
    # agent gates (which read it) and the engine gate (account_live) agree.
    cfg["account_mode"] = "live" if str(cfg.get("account_mode") or "").lower() == "live" else "paper"
    cfg["terminal_paper_mode"] = cfg["account_mode"] == "paper"
    cfg["terminal_paper_bankroll_usd"] = _clampf(
        cfg.get("terminal_paper_bankroll_usd"), 1.0, 1e7,
        d["terminal_paper_bankroll_usd"])
    cfg["script_market_limit"] = _clampi(
        cfg.get("script_market_limit"), 0, 200, d["script_market_limit"])
    cfg["script_market_min_volume"] = _clampf(
        cfg.get("script_market_min_volume"), 0.0, 1e12,
        d["script_market_min_volume"])
    cfg["script_market_max_spread_cents"] = _clampf(
        cfg.get("script_market_max_spread_cents"), 0.0, 99.0,
        d["script_market_max_spread_cents"])
    return cfg


def merge_with_defaults(user: dict[str, Any]) -> dict[str, Any]:
    """Apply user overrides on top of DEFAULT_CONFIG.

    Accepts either snake_case or camelCase keys (the Electron renderer
    sends camelCase; the Python codebase reads snake_case). Always
    returns a fully populated config dict — missing keys back-fill from
    defaults. Unknown keys are preserved (forward-compat) but ignored by
    the running code.
    """
    out = dict(DEFAULT_CONFIG)
    given: set[str] = set()
    for k, v in (user or {}).items():
        if k in out:
            out[k] = v
            given.add(k)
            continue
        sk = _camel_to_snake(k)
        out[sk] = v
        given.add(sk)
    _migrate_mcp(out, given)
    _migrate_account(out, given)
    _migrate_ai(out, given)
    return _validate_config(out)


def _migrate_ai(out: dict[str, Any], given: set[str]) -> None:
    """Configs from before the six providers had one model key per SDK
    provider (`ai_anthropic_model`, `ai_openai_model`). Carried into the one
    `ai_model` for the provider the user has selected — only when `ai_model`
    was not given, so a config that already says what it wants is never
    rewritten — and the legacy keys are dropped."""
    if "ai_model" not in given:
        prov = str(out.get("ai_provider") or "anthropic").strip().lower()
        legacy = out.get(f"ai_{prov}_model") if prov in ("anthropic", "openai") else None
        if isinstance(legacy, str) and legacy.strip():
            out["ai_model"] = legacy.strip()
        elif prov != "anthropic":
            # The anthropic default must not travel to another provider.
            out["ai_model"] = ""
    out.pop("ai_anthropic_model", None)
    out.pop("ai_openai_model", None)


def _migrate_account(out: dict[str, Any], given: set[str]) -> None:
    """A config from before account_mode existed. Paper if it had paper mode
    on; otherwise an EXISTING config (one that carries any setting at all)
    is live — its engines sent real orders yesterday and must not silently
    stop today — and a fresh one (nothing given) is paper.

    The renderer's settings store owns the stricter half of this rule (it
    knows whether the disclaimer was accepted) and always sends accountMode
    once it knows the key; this is what a config WITHOUT it means here.
    Never applied when account_mode was given."""
    if "account_mode" in given:
        return
    if not given:
        out["account_mode"] = "paper"
    elif "terminal_paper_mode" in given:
        out["account_mode"] = "paper" if out.get("terminal_paper_mode") is True else "live"
    else:
        out["account_mode"] = "live"


def account_live(cfg: dict[str, Any]) -> bool:
    """True when the ACCOUNT is live: real orders may leave this app at all.

    Narrowing only: a validated config says it twice (account_mode, and the
    derived terminal_paper_mode), and either one saying paper is paper. A raw
    dict that never went through merge_with_defaults and names neither key
    (tests, CLI tools) keeps the pre-account-mode meaning: live."""
    m = cfg.get("account_mode")
    if m is not None and str(m).lower() != "live":
        return False
    return not cfg.get("terminal_paper_mode")


def engine_on(cfg: dict[str, Any], switch: str) -> bool:
    """The EFFECTIVE engine switch: the user's own switch AND a live account.
    In Paper the engines send nothing, and the user's switch is left exactly
    as they set it — going live again restores what they had."""
    return bool(cfg.get(switch)) and account_live(cfg)


def _migrate_mcp(out: dict[str, Any], given: set[str]) -> None:
    """Configs from before named agents had one live switch
    (`mcp_live_trading_enabled`) and let the agent pick paper or live per
    call. Carried forward, never widened: a user who had live trading on keeps
    agents able to trade live (now behind approvals by default) and keeps the
    whole-account powers that switch used to grant; everyone else is paper.
    Only applied when the new key was not given — a config that already says
    what it wants is never rewritten."""
    legacy_live = out.get("mcp_live_trading_enabled") is True
    if "mcp_trade_mode" not in given:
        out["mcp_trade_mode"] = "live" if legacy_live else DEFAULT_CONFIG["mcp_trade_mode"]
    if "mcp_allow_live_switches" not in given and legacy_live:
        out["mcp_allow_live_switches"] = True
    if "mcp_daily_spend_usd" not in given and "mcp_daily_limit_usd" in given:
        out["mcp_daily_spend_usd"] = out.get("mcp_daily_limit_usd")
    out.pop("mcp_live_trading_enabled", None)
    out.pop("mcp_daily_limit_usd", None)
