"""Whale + momentum scanners.

Both scanners share market sync, trade insertion, and category
resolution. Each scan cycle inserts new alerts/whales into the DB and
returns counts so the runner can surface them on the UI.

The scoring functions are direct ports of the originals (calibrated on
22k+ resolved Polymarket whale trades and 5k+ momentum clusters).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import db
import polymarket_api
from categorize import (
    CATEGORY_EDGE, POLYMARKET_CATEGORY_MAP, categorize_by_keywords, is_micro_market,
)

logger = logging.getLogger(__name__)


# ─── Constants tuned in the original scanners ─────────────────────────

MIN_VOLUME_24H = 50
MIN_VOLUME_SPIKE_RATIO = 2.0
MIN_PRICE_MOVE = 0.08
MIN_TRADE_CLUSTER_COUNT = 5
MIN_TRADE_CLUSTER_DOLLARS = 500
ALERT_COOLDOWN_MINUTES = 30
MOMENTUM_YES_MAX_YES_PRICE = 0.50
MOMENTUM_NO_MIN_YES_PRICE = 0.50
ALLOWED_MOMENTUM_SIGNALS = {"trade_cluster"}


def _to_float(v) -> float:
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _trade_time(ts) -> str | None:
    """A trade's own time as a `created_at` value ('YYYY-MM-DD HH:MM:SS' UTC,
    the column's own format), or None to let the row default to now.

    Rows built from the trade tape carry the time the trade happened, not the
    time we saw it: the REST page can be minutes old, and a row stamped "now"
    reads as fresh to the signal age gate and on the Signals page. Epoch
    milliseconds are accepted; a missing, zero or future time is not a time.

    The age gate compares against THIS machine's clock, so the server's trade
    time is moved onto it with the offset measured at sign-in: a PC clock
    running minutes fast would otherwise age every whale, the socket's ~1s-old
    ones included, past the gate."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    if t > 1e11:
        t /= 1000.0
    if t <= 0:
        return None
    t -= polymarket_api.auth.get_time_offset()   # server clock -> ours
    if t > datetime.now(timezone.utc).timestamp() + 60:
        return None
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_days_to_close(close_time: str) -> float | None:
    if not close_time:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            ct = datetime.strptime(close_time, fmt)
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            delta = (ct - datetime.now(timezone.utc)).total_seconds()
            return max(delta / 86400, 0)
        except ValueError:
            continue
    return None


# ─── category resolution ──────────────────────────────────────────────


async def _resolve_category(ticker: str, title: str = "", event_slug: str = "") -> str:
    # Polymarket markets carry their category via the parent event's tags,
    # which `polymarket_api` already resolves into `category` on the
    # normalised market dict. When a trade-discovered market has no category
    # yet, fall back to keyword inference on the title + event slug (match
    # titles like "Will Jordan win on 2026-06-22?" carry no sport keyword —
    # the `fifwc-…` slug is what identifies them).
    return categorize_by_keywords(title, slug=event_slug)


# ─── scoring ──────────────────────────────────────────────────────────


def compute_whale_score(
    dollar_value: float,
    price: float,
    taker_side: str,
    *,
    market_volume: float = 0.0,
    open_interest: float = 0.0,
    days_to_close: float | None = None,
    category: str = "",
) -> float:
    """Empirically-derived edge over market-implied probability."""
    implied = max(5, min(price * 100, 95))
    edge = 0.0
    if taker_side == "yes":
        if   price >= 0.90: edge += 0.5
        elif price >= 0.80: edge += 2
        elif price >= 0.65: edge += 4.5
        elif price >= 0.50: edge -= 1.5
        else:               edge -= 6
    else:
        if   price >= 0.80: edge -= 0.5
        elif price >= 0.65: edge += 0.5
        elif price >= 0.50: edge += 3.5
        elif price >= 0.35: edge -= 5
        else:               edge -= 8

    if dollar_value >= 25_000:   edge += 2
    elif dollar_value >= 10_000: edge += 1
    elif dollar_value >= 5_000:  edge += 1.5
    elif dollar_value >= 2_500:  edge += 0.5

    if market_volume >= 250_000:    edge += 1.5
    elif market_volume >= 50_000:   edge += 0.5
    elif 0 < market_volume < 1_000: edge -= 1.5

    if open_interest >= 50_000:
        edge += 0.5

    if days_to_close is not None and days_to_close > 0:
        if days_to_close <= 1: edge += 0.5
        elif days_to_close > 60: edge -= 0.5

    if category:
        edge += CATEGORY_EDGE.get(category, 0.0) * 0.5

    edge = max(-15, min(edge, 10))
    return max(5, min(round(implied + edge, 1), 97.0))


def compute_momentum_confidence(
    *,
    volume_spike_ratio: float,
    price_change_abs: float,
    trade_cluster_count: int,
    trade_cluster_dollars: float,
    market_volume: float,
    open_interest: float,
    days_to_close: float | None,
    price: float,
    direction: str,
    signal_type: str = "",
    category: str = "",
) -> float:
    if direction == "yes":
        implied = price * 100
    else:
        implied = (1.0 - price) * 100
    implied = max(5, min(implied, 95))

    edge = 0.0

    if signal_type == "trade_cluster":
        if implied < 15:   edge += 25
        elif implied < 25: edge += 18
        elif implied < 35: edge += 11
        elif implied < 50: edge += 7
        else:              edge += 3

    if trade_cluster_count >= 20:    edge += 2
    elif trade_cluster_count >= 10:  edge += 1.5
    elif trade_cluster_count >= 5:   edge += 1
    elif trade_cluster_count >= 3:   edge += 0.5
    if trade_cluster_dollars >= 5_000:
        edge += 1

    if volume_spike_ratio >= 5:    edge += 1.5
    elif volume_spike_ratio >= 3:  edge += 1
    elif volume_spike_ratio >= 2:  edge += 0.5

    if price_change_abs >= 0.15:    edge += 1.5
    elif price_change_abs >= 0.08:  edge += 1
    elif price_change_abs >= 0.05:  edge += 0.5

    if market_volume >= 250_000:    edge += 2
    elif market_volume >= 50_000:   edge += 1
    elif market_volume >= 10_000:   edge += 0.5
    elif 0 < market_volume < 1_000: edge -= 1.5

    if open_interest >= 50_000:    edge += 1
    elif open_interest >= 10_000:  edge += 0.5

    if days_to_close is not None and days_to_close > 0:
        if days_to_close <= 1:   edge += 1
        elif days_to_close <= 7: edge += 0.5
        elif days_to_close > 60: edge -= 0.5

    if category:
        edge += CATEGORY_EDGE.get(category, 0.0) * 0.5

    edge = max(-12, min(edge, 8))
    return max(5, min(round(implied + edge, 1), 97.0))


# ─── market sync ──────────────────────────────────────────────────────


def _write_markets_blocking(raw: list, now_iso: str) -> int:
    """Filter + upsert the market universe. Runs in a WORKER THREAD.

    ~1,000 synchronous `upsert_market` calls in one write transaction is tens
    of milliseconds of blocking sqlite work, and on the event loop that is dead
    time for every other engine in the master loop. sqlite3 connections are
    per-thread here (`db.get_db()` connects on entry with the default
    `check_same_thread=True`), so the connection is opened INSIDE this function
    — i.e. on the thread that uses it — and closed before it returns. The whole
    universe still lands in ONE transaction, so a reader never sees a half-
    written market table.
    """
    count = 0
    with db.get_db() as conn:
        for m in raw:
            ticker = m.get("ticker", "")
            slug = m.get("slug", "")
            if not ticker or is_micro_market(slug):
                continue
            close_time = m.get("close_time", "")
            if close_time and close_time < now_iso:
                continue
            # No volume gate: this venue's market list carries no volume figure.
            event_ticker = m.get("event_ticker", "")
            db.upsert_market(
                conn,
                {
                    "ticker": ticker,
                    "event_ticker": event_ticker,
                    "series_ticker": "",
                    "slug": slug,
                    "title": m.get("title", "") or m.get("yes_sub_title", "") or ticker,
                    "yes_sub_title": m.get("yes_sub_title", ""),
                    "category": m.get("category", ""),
                    "status": m.get("status", "open"),
                    "close_time": close_time,
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
            count += 1
    return count


async def sync_markets(*, max_pages: int = 10) -> int:
    raw = await polymarket_api.fetch_all_open_markets(max_pages=max_pages)
    if not raw:
        return 0
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return await asyncio.get_running_loop().run_in_executor(
        None, _write_markets_blocking, raw, now_iso
    )


async def sync_events() -> int:
    events = await polymarket_api.fetch_events(limit=200)
    count = 0
    with db.get_db() as conn:
        for e in events:
            slug = e.get("slug") or ""
            if not slug:
                continue
            db.upsert_event(
                conn,
                {
                    "event_ticker": slug,
                    "series_ticker": e.get("seriesSlug") or "",
                    "title": e.get("title") or "",
                    "sub_title": e.get("subtitle") or "",
                    "category": e.get("category") or "",
                    "status": "open",
                },
            )
            count += 1
    return count


# ─── whale scanner ────────────────────────────────────────────────────


def whale_candidate(t: dict, cfg: dict) -> dict | None:
    """Is ONE normalised trade a whale? Returns the enriched candidate or None.

    The size/entry/micro-market gate for BOTH sources: the 120s REST poll and
    the activity-websocket push run the same trade dict through this, so the
    socket can never admit a trade the poll would have rejected (or vice
    versa). Cheap and synchronous — the socket handler calls it inline on every
    frame before it spends a task on one.
    """
    if not isinstance(t, dict):
        return None
    min_usd = float(cfg.get("min_whale_usd", 2500))
    min_entry = float(cfg.get("min_entry_price_frac", 0.50))
    count_fp = _to_float(t.get("count_fp", 0))
    yes_p = _to_float(t.get("yes_price_dollars", 0))
    no_p = _to_float(t.get("no_price_dollars", 0))
    side = t.get("taker_side", "")
    price = yes_p if side == "yes" else no_p
    dollar = count_fp * price
    if dollar < min_usd:
        return None
    if price < min_entry:
        return None
    ticker = t.get("ticker", "")
    if not ticker or is_micro_market(t.get("slug", "")):
        return None
    return {
        "raw": t,
        "ticker": ticker,
        "count_fp": count_fp,
        "price": price,
        "dollar": dollar,
        "taker_side": side,
    }


async def scan_whales(cfg: dict) -> tuple[int, list[dict]]:
    """Scan recent trades for whale-sized takers and insert new whales.

    Returns (count, new_rows) where `new_rows` are the freshly-inserted
    DB rows (used for live-pushing them up to the UI).

    This is the RECONCILIATION BACKSTOP for the activity-websocket push (see
    `process_whale_trades`): same cadence as ever, and the socket's inserts are
    invisible to it beyond `whale_trade_exists` returning True for a trade it
    already wrote.

    It asks for whale-sized trades only. An unfiltered page is the last ~30-50
    seconds of all trading, so a 120s poll saw a sliver of each interval and
    missed most whales the socket did not catch.
    """
    raw = await polymarket_api.fetch_recent_trades(
        limit=1000, min_size=float(cfg.get("min_whale_usd", 2500)))
    if not raw:
        return 0, []
    return await process_whale_trades(cfg, raw)


async def process_whale_trades(
    cfg: dict, raw: list[dict]
) -> tuple[int, list[dict]]:
    """Score + insert whales from a batch of ALREADY-NORMALISED trades.

    The one place a whale row is ever written. The REST poll passes its 1,000
    trades; the activity websocket passes a batch of one, ~1s after execution
    instead of up to 120s later. Identical scoring, identical row, and the
    `trade_id` dedupe (`whale_trade_exists` + the UNIQUE index) makes whichever
    source arrives second a no-op.
    """
    candidates: list[dict] = []
    for t in raw or []:
        c = whale_candidate(t, cfg)
        if c is not None:
            candidates.append(c)

    if not candidates:
        return 0, []

    new_rows: list[dict] = []

    # Pre-fetch markets we don't already have locally OUTSIDE the write lock.
    # The old loop did `await fetch_market(...)` while holding the DB connection
    # open, keeping a write transaction alive across a (possibly slow) Gamma
    # call. whale_trade_exists / get_market are cheap reads, so scout here and
    # fetch the misses concurrently; the write loop below stays lock-only.
    with db.get_db() as conn:
        need = {
            e["ticker"] for e in candidates
            if e["raw"].get("trade_id")
            and e["ticker"]
            and not db.whale_trade_exists(conn, e["raw"]["trade_id"])
            and not db.get_market(conn, e["ticker"])
        }
    prefetched = await polymarket_api.fetch_markets_map(list(need)) if need else {}

    # Phase 1, one short transaction: adopt the prefetched markets and read
    # what scoring needs. No awaits in here.
    staged: list[dict] = []
    with db.get_db() as conn:
        for entry in candidates:
            t = entry["raw"]
            trade_id = t.get("trade_id", "")
            if not trade_id or db.whale_trade_exists(conn, trade_id):
                continue
            ticker = entry["ticker"]
            mkt = db.get_market(conn, ticker)
            if not mkt:
                api_mkt = prefetched.get(ticker)
                if api_mkt:
                    event_tk = api_mkt.get("event_ticker", "")
                    db.upsert_market(
                        conn,
                        {
                            "ticker": ticker,
                            "event_ticker": event_tk,
                            "series_ticker": "",
                            "slug": api_mkt.get("slug", ""),
                            "title": api_mkt.get("title", ""),
                            "yes_sub_title": api_mkt.get("yes_sub_title", ""),
                            "category": api_mkt.get("category", ""),
                            "status": api_mkt.get("status", "open"),
                            "close_time": api_mkt.get("close_time", ""),
                            "volume": api_mkt.get("volume_fp", 0),
                            "volume_24h": api_mkt.get("volume_24h_fp", 0),
                            "open_interest": api_mkt.get("open_interest_fp", 0),
                            "yes_bid": api_mkt.get("yes_bid_dollars", 0),
                            "yes_ask": api_mkt.get("yes_ask_dollars", 0),
                            "last_price": api_mkt.get("last_price_dollars", 0),
                            "result": api_mkt.get("result", ""),
                            "settlement_value": api_mkt.get("settlement_value_dollars"),
                        },
                    )
                    mkt = db.get_market(conn, ticker)

            title = ""
            event_ticker = ""
            close_time = ""
            mkt_vol = oi = 0.0
            cat = ""
            yes_sub = ""
            if mkt:
                title = mkt.get("title", "") or mkt.get("yes_sub_title", "")
                yes_sub = mkt.get("yes_sub_title", "")
                event_ticker = mkt.get("event_ticker", "")
                close_time = mkt.get("close_time", "")
                mkt_vol = _to_float(mkt.get("volume", 0))
                oi = _to_float(mkt.get("open_interest", 0))
                cat = mkt.get("category", "")

            staged.append({
                "entry": entry, "trade_id": trade_id, "ticker": ticker,
                "title": title, "yes_sub": yes_sub, "event_ticker": event_ticker,
                "close_time": close_time, "mkt_vol": mkt_vol, "oi": oi, "cat": cat,
            })

    # Phase 2: category lookups may hit Gamma. Do them with NO transaction
    # open — holding one across an await stalls every other writer for the
    # round trip (guarded by tests/test_db_connection_discipline.py).
    missing = [s for s in staged if not s["cat"]]
    if missing:
        cats = await asyncio.gather(*[
            _resolve_category(s["ticker"], s["title"], s["event_ticker"])
            for s in missing
        ])
        for s, c in zip(missing, cats):
            s["cat"] = c

    # Phase 3: score and insert in a second short transaction. Re-check
    # existence — the socket path and the REST backstop can stage one trade.
    with db.get_db() as conn:
        for s in staged:
            entry, trade_id, ticker = s["entry"], s["trade_id"], s["ticker"]
            title, yes_sub, event_ticker = s["title"], s["yes_sub"], s["event_ticker"]
            close_time, mkt_vol, oi, cat = s["close_time"], s["mkt_vol"], s["oi"], s["cat"]
            if db.whale_trade_exists(conn, trade_id):
                continue

            days_left = _parse_days_to_close(close_time)
            confidence = compute_whale_score(
                dollar_value=entry["dollar"],
                price=entry["price"],
                taker_side=entry["taker_side"],
                market_volume=mkt_vol,
                open_interest=oi,
                days_to_close=days_left,
                category=cat,
            )

            data = {
                "trade_id": trade_id,
                "ticker": ticker,
                "event_ticker": event_ticker,
                "title": title or ticker,
                "yes_sub_title": yes_sub,
                "category": cat,
                "taker_side": entry["taker_side"],
                "count_fp": entry["count_fp"],
                "price": entry["price"],
                "dollar_value": entry["dollar"],
                "market_volume": mkt_vol,
                "open_interest": oi,
                "confidence": confidence,
                "created_at": _trade_time(entry["raw"].get("timestamp")),
            }
            wid = db.insert_whale_trade(conn, data)
            if wid:
                row = conn.execute(
                    "SELECT * FROM whale_trades WHERE id=?", (wid,)
                ).fetchone()
                if row:
                    new_rows.append(dict(row))

    return len(new_rows), new_rows


# ─── momentum scanner ─────────────────────────────────────────────────


async def scan_momentum(cfg: dict) -> tuple[int, list[dict]]:
    with db.get_db() as conn:
        markets = db.get_active_markets(conn, min_volume=MIN_VOLUME_24H, limit=500)

    if not markets:
        return 0, []

    recent_trades = await polymarket_api.fetch_recent_trades(limit=1000)
    trades_by_ticker: dict[str, list[dict]] = defaultdict(list)
    for t in recent_trades:
        ticker = t.get("ticker", "")
        if ticker and not is_micro_market(t.get("slug", "")):
            trades_by_ticker[ticker].append(t)

    new_alerts: list[dict] = []
    contrarian_only = bool(cfg.get("contrarian_only", True))
    allowed_signals = set(cfg.get("allowed_momentum_signal_types", ["trade_cluster"]))

    with db.get_db() as conn:
        # Insert new global trades
        for trade in recent_trades:
            tid = trade.get("trade_id", "")
            if tid and not db.trade_exists(conn, tid):
                count_fp = _to_float(trade.get("count_fp", 0))
                yes_p = _to_float(trade.get("yes_price_dollars", 0))
                no_p = _to_float(trade.get("no_price_dollars", 0))
                taker = trade.get("taker_side", "")
                dollar = count_fp * (yes_p if taker == "yes" else no_p)
                db.insert_trade(
                    conn,
                    {
                        "trade_id": tid,
                        "ticker": trade.get("ticker", ""),
                        "count_fp": count_fp,
                        "yes_price": yes_p,
                        "no_price": no_p,
                        "taker_side": taker,
                        "dollar_value": dollar,
                        "created_time": trade.get("created_time", ""),
                    },
                )

    with db.get_db() as conn:
        # Bulk-load the prior snapshot for every market up front (one query)
        # instead of a get_previous_snapshot() call per market — that was a
        # ~500× N+1 against the multi-million-row market_snapshots table.
        prev_snaps = db.get_previous_snapshots_bulk(
            conn, [m["ticker"] for m in markets]
        )
        for market in markets:
            ticker = market["ticker"]
            if is_micro_market(market.get("slug", "")):
                continue

            vol_24h = _to_float(market.get("volume_24h", 0))
            # The market's price is its mid (last_price). yes_bid is Gamma's
            # real best bid now, half a spread lower, and it jumps whenever one
            # resting order is pulled, so comparing bids reads noise (and the
            # spread itself) as a price move.
            cur_price = _to_float(market.get("last_price", 0)) or _to_float(
                market.get("yes_bid", 0)
            )
            oi = _to_float(market.get("open_interest", 0))
            close_time = market.get("close_time", "")
            days_left = _parse_days_to_close(close_time)

            prev = prev_snaps.get(ticker)
            prev_vol = _to_float(prev.get("volume_24h", 0)) if prev else 0
            prev_price = (
                _to_float(prev.get("last_price", 0) or prev.get("yes_bid", 0))
                if prev
                else 0
            )

            db.save_snapshot(conn, ticker, market)

            vol_spike = (vol_24h / prev_vol) if prev_vol > 10 else 0
            price_change = (cur_price - prev_price) if prev_price > 0 else 0

            ticker_trades = trades_by_ticker.get(ticker, [])
            # A cluster is as old as the newest trade it was read from; the
            # REST page behind it can be minutes old (see fetch_recent_trades).
            cluster_at = _trade_time(max(
                (_to_float(t.get("timestamp")) for t in ticker_trades), default=0))
            yes_trades = [t for t in ticker_trades if t.get("taker_side") == "yes"]
            no_trades = [t for t in ticker_trades if t.get("taker_side") == "no"]
            yes_count = len(yes_trades)
            no_count = len(no_trades)
            yes_dollars = sum(
                _to_float(t.get("count_fp", 0))
                * _to_float(t.get("yes_price_dollars", 0))
                for t in yes_trades
            )
            no_dollars = sum(
                _to_float(t.get("count_fp", 0))
                * _to_float(t.get("no_price_dollars", 0))
                for t in no_trades
            )

            if yes_count > no_count:
                cluster_dir = "yes"
                cluster_count = yes_count
                cluster_dollars = yes_dollars
            elif no_count > yes_count:
                cluster_dir = "no"
                cluster_count = no_count
                cluster_dollars = no_dollars
            else:
                cluster_dir = "yes" if price_change >= 0 else "no"
                cluster_count = max(yes_count, no_count)
                cluster_dollars = max(yes_dollars, no_dollars)

            signals: list[str] = []
            if vol_spike >= MIN_VOLUME_SPIKE_RATIO and vol_24h >= MIN_VOLUME_24H:
                signals.append("volume_spike")
            if abs(price_change) >= MIN_PRICE_MOVE:
                signals.append("price_move")
            if (
                cluster_count >= MIN_TRADE_CLUSTER_COUNT
                and cluster_dollars >= MIN_TRADE_CLUSTER_DOLLARS
            ):
                signals.append("trade_cluster")
            if not signals:
                continue

            # Per-signal direction: a trade_cluster's direction is its OWN
            # cluster_dir (who the takers hit), a price_move's is the price
            # direction, and a direction-less volume_spike borrows the price
            # move when one co-fired else the cluster. The old code overwrote
            # `direction` with the price-move direction for ALL signals whenever
            # a price_move co-fired, so a trade_cluster alert could be emitted
            # pointing OPPOSITE its own cluster (and mis-score contrarian gates).
            price_dir = "yes" if price_change > 0 else "no"
            spike_dir = price_dir if "price_move" in signals else cluster_dir

            for stype in signals:
                if stype == "trade_cluster":
                    direction = cluster_dir
                elif stype == "price_move":
                    direction = price_dir
                else:  # volume_spike — no inherent side
                    direction = spike_dir
                if stype not in allowed_signals:
                    continue
                if contrarian_only:
                    if direction == "yes" and cur_price > MOMENTUM_YES_MAX_YES_PRICE:
                        continue
                    if direction == "no" and cur_price < MOMENTUM_NO_MIN_YES_PRICE:
                        continue
                if db.recent_alert_exists(
                    conn, ticker, stype, direction, ALERT_COOLDOWN_MINUTES
                ):
                    continue

                category = market.get("category", "") or categorize_by_keywords(
                    market.get("title", "") or market.get("yes_sub_title", "")
                )
                total_vol = _to_float(market.get("volume", 0))

                confidence = compute_momentum_confidence(
                    volume_spike_ratio=vol_spike,
                    price_change_abs=abs(price_change),
                    trade_cluster_count=cluster_count,
                    trade_cluster_dollars=cluster_dollars,
                    market_volume=total_vol,
                    open_interest=oi,
                    days_to_close=days_left,
                    price=cur_price,
                    direction=direction,
                    signal_type=stype,
                    category=category,
                )
                title = (
                    market.get("title", "")
                    or market.get("yes_sub_title", "")
                    or ticker
                )
                alert_data = {
                    "ticker": ticker,
                    "event_ticker": market.get("event_ticker", ""),
                    "title": title,
                    "yes_sub_title": market.get("yes_sub_title", ""),
                    "category": category,
                    "signal_type": stype,
                    "direction": direction,
                    "volume_24h": vol_24h,
                    "price": cur_price,
                    "price_change": price_change * 100,
                    "confidence": confidence,
                    "created_at": cluster_at if stype == "trade_cluster" else None,
                }
                alert_id = db.insert_alert(conn, alert_data)
                row = conn.execute(
                    "SELECT * FROM alerts WHERE id=?", (alert_id,)
                ).fetchone()
                if row:
                    new_alerts.append(dict(row))

    return len(new_alerts), new_alerts


# ─── resolution checkers (signals only) ──────────────────────────────


async def resolve_alerts_from_markets() -> int:
    """Mark resolved alerts based on market settlement values."""
    with db.get_db() as conn:
        unresolved = db.get_unresolved_alerts(conn, days=30)
    if not unresolved:
        return 0

    tickers = list({a["ticker"] for a in unresolved if a["ticker"]})
    # Fetch every market concurrently (bounded) instead of a serial await-chain
    # with a per-ticker sleep — one hung endpoint used to stall the whole pass.
    markets = await polymarket_api.fetch_markets_map(tickers)
    resolved = 0
    with db.get_db() as conn:
        for a in unresolved:
            market = markets.get(a["ticker"])
            if not market:
                continue
            status = (market.get("status") or "").lower()
            result = (market.get("result") or "").lower()
            if status not in ("determined", "finalized", "settled") or not result:
                continue
            if a["direction"] == "yes":
                correct = result == "yes"
                pnl = (1.0 - a["price"]) if correct else (-a["price"])
            else:
                correct = result == "no"
                pnl = (a["price"]) if correct else (-(1.0 - a["price"]))
            db.mark_alert_resolved(
                conn, a["id"], correct, 1.0 if correct else 0.0, pnl
            )
            resolved += 1
    return resolved


async def resolve_whales_from_markets() -> int:
    with db.get_db() as conn:
        unresolved = db.get_unresolved_whale_trades(conn, days=30)
    if not unresolved:
        return 0
    tickers = list({t["ticker"] for t in unresolved if t["ticker"]})
    # Concurrent bounded fetch instead of a serial per-ticker await + sleep.
    markets = await polymarket_api.fetch_markets_map(tickers)
    resolved = 0
    with db.get_db() as conn:
        for t in unresolved:
            market = markets.get(t["ticker"])
            if not market:
                continue
            status = (market.get("status") or "").lower()
            result = (market.get("result") or "").lower()
            if status not in ("determined", "finalized", "settled") or not result:
                continue
            if t["taker_side"] == "yes":
                correct = result == "yes"
                pnl = (
                    (1.0 - t["price"]) * t["dollar_value"]
                    if correct
                    else -t["dollar_value"]
                )
            else:
                correct = result == "no"
                pnl = (
                    (t["price"]) * t["dollar_value"]
                    if correct
                    else -t["dollar_value"]
                )
            db.mark_whale_resolved(
                conn, t["id"], correct, 1.0 if correct else 0.0, pnl
            )
            resolved += 1
    return resolved
