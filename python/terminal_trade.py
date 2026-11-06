"""Terminal — Act and Track.

This is the half that spends money, so it is written to a different standard
than the read side.

WHO DECIDES. The app never decides to act. Every order that leaves this module
traces to something the user did: a click, or an instruction they WROTE DOWN
(a stop loss, a take profit) that fires later. A standing instruction is gated
identically to a manual click, and every pending one is listed on one page and
cancellable there. No score, signal or strategy in this codebase can reach
``place()``.

COST BASIS. What a fill actually cost is NOT what we asked for. The requested
price ignores tick-snapping, partial fills and taker fees — every one of which
makes the naive number wrong IN THE USER'S FAVOUR, on every single row. So a
fill is reconciled against the venue's own record of the order, and a fill that
cannot be reconciled is stored ``unreconciled``, EXCLUDED from every aggregate
figure, and counted in a warning strip. Never guessed at.

Convention: AVERAGE COST, not FIFO — what a trader means by "I am up 2x on
this". The UI says so, so nobody has to guess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

import polymarket_api as api
import polymarket_auth as auth
import terminal as term
import us_api as us
from terminal import cents, num, _now_iso

logger = logging.getLogger("krypt.terminal.trade")

# A hand-placed order is capped here regardless of what the renderer sends.
# Assume the renderer is hostile: this is the last line, in the process that
# actually holds the key.
MAX_MANUAL_CONTRACTS = 5000
MAX_MANUAL_USD = 5000.0
# The UI shows a cost; main re-computes it and refuses if it drifted by more
# than this. A hostile or stale renderer cannot spend more than the number a
# human actually read.
CONFIRM_TOLERANCE_USD = 0.51
# How many consecutive refusals retire a standing instruction. A rejected sell
# must NOT silently disarm the stop protecting an open position — but it must
# not re-send an order every tick forever either.
MAX_STANDING_FAILURES = 3


class TerminalTradeError(Exception):
    """A refusal the user should read verbatim."""


# The event loop holds only a WEAK reference to a running task, so a bare
# ensure_future/create_task can be garbage-collected mid-flight (the hazard
# service.py:3228 documents). Here that silently dropped the post-order
# reconcile, leaving the fill you just paid for marked unreconciled and
# excluded from every total until something else happened to trigger a walk.
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> Optional[asyncio.Task]:
    """Fire-and-forget, but keep the reference until it finishes."""
    try:
        t = asyncio.ensure_future(coro)
    except RuntimeError:  # no running loop (CLI / tests)
        coro.close()
        return None
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def _net() -> str:
    try:
        return auth.get_env() or "mainnet"
    except Exception:
        return "mainnet"


def _conn():
    import db
    return db.get_db()


# ────────────────────────────────────────────────────────────────────────
# Fees
# ────────────────────────────────────────────────────────────────────────


def _fee_per_share(price_cents: float, category: Optional[str],
                   schedule: Optional[dict]) -> Optional[float]:
    """Taker fee per share, in dollars: feeCoefficient x p x (1 - p).

    From the market's own coefficient only. Returns None when the market carries
    none: an unknown fee is NOT a zero fee, and quoting a cost that silently omits
    it is exactly the "wrong in the user's favour" failure this module exists to
    prevent. `category` is unused; the venue states the coefficient per market.
    """
    p = max(0.0, min(1.0, float(price_cents) / 100.0))
    if isinstance(schedule, dict):
        if schedule.get("enabled") is False:
            return 0.0
        if schedule.get("enabled") and schedule.get("rate") is not None:
            try:
                return float(schedule["rate"]) * p * (1.0 - p)
            except (TypeError, ValueError):
                return None
    return None


def taker_fee_usd(price_cents: float, contracts: float,
                  schedule: Optional[dict]) -> Optional[float]:
    """The taker fee on one order: coefficient x contracts x p x (1 - p), rounded
    to the cent with banker's rounding, as the exchange charges it. In Decimal,
    because a tie is exactly where float noise would pick the wrong cent. None
    when the rate is unknown, 0.0 when the market charges none."""
    if _fee_per_share(price_cents, None, schedule) is None:
        return None
    if schedule.get("enabled") is False:
        return 0.0
    p = max(Decimal(0), min(Decimal(1), Decimal(str(price_cents)) / 100))
    fee = Decimal(str(schedule["rate"])) * Decimal(str(contracts)) * p * (1 - p)
    return float(fee.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


# ────────────────────────────────────────────────────────────────────────
# Order preview
# ────────────────────────────────────────────────────────────────────────


def _walk_book(levels: list[dict], want: float,
               limit_cents: float, is_buy: bool) -> tuple[Optional[float], Optional[float]]:
    """Walk resting levels up to `limit_cents` → (fillable_size, avg_cents).

    Returns (None, None) when there is no book to walk — an unknown fill is
    not a zero fill, and a ticket that shows "0 available" on a book it could
    not read is telling the user something false about the market.
    """
    if not levels:
        return None, None
    filled = 0.0
    cost = 0.0
    for lvl in levels:
        p = lvl.get("priceCents")
        sz = lvl.get("size")
        if p is None or sz is None:
            continue
        if is_buy and p > limit_cents:
            break
        if not is_buy and p < limit_cents:
            break
        take = min(float(sz), want - filled)
        if take <= 0:
            break
        filled += take
        cost += take * float(p)
        if filled >= want:
            break
    if filled <= 0:
        return 0.0, None
    return round(filled, 2), round(cost / filled, 3)


async def _available_cash_usd(paper: bool) -> Optional[float]:
    """What this account can actually spend, or None when we do not know.

    None is not zero and must never be rendered as a balance. The live figure
    is the SAME cached balance read the header shows (`trader.refresh_balance`
    fills it on the status poll), read out of the cache rather than forced, so
    a ticket keystroke never triggers a request. A cache entry whose last read
    FAILED carries a stale number under `ok: False`; that is reported as unknown,
    because a blocker built on a stale balance would refuse an order the account
    can afford.

    Paper cash is net of what the account's resting buys would still spend.
    A resting order is not a position, but it is a claim on the cash: without
    this, three resting buys that each fit the balance were all accepted, all
    filled, and left the account at minus a thousand dollars.
    """
    if paper:
        try:
            import terminal_paper
            import service
            pf = await terminal_paper.portfolio(dict(service.STATE.cfg or {}))
            reserved = terminal_paper.reserved_buy_usd()
        except Exception:  # noqa: BLE001 — no service (tests, CLI)
            return None
        cash = num((pf or {}).get("cashUsd"))
        return None if cash is None else round(cash - reserved, 2)
    try:
        import trader
        entry = trader._balance_cache.get(auth.get_env())
    except Exception:  # noqa: BLE001 — no trader module (tests, CLI)
        return None
    if not entry or not entry.get("ok"):
        return None
    c = num(entry.get("cents"))
    return None if c is None else round(c / 100.0, 2)


# ── the venue's rejections, in English ──────────────────────────────────
#
# The exchange's 400 texts are written for whoever wrote the exchange. "the balance
# is not enough" is true, unreadable, and says nothing about WHY — which in the
# owner's live test was "the buy you think filled is still resting on the book".
# The raw text stays on the order log row; the ticket gets the sentence that tells
# you what to do.


def _usd(v: Optional[float]) -> Optional[str]:
    return None if v is None else f"${v:,.2f}"


def translate_venue_error(raw: str, *, action: str,
                          cost_usd: Optional[float] = None,
                          balance_usd: Optional[float] = None) -> Optional[str]:
    """One exchange rejection → one plain sentence, or None to keep the raw text.

    Never invents a number: every figure below is one we independently know
    (the preview's cost, the cached balance).
    """
    low = (raw or "").lower()

    if any(w in low for w in ("restricted in your region", "geoblock", "jurisdiction",
                              "not eligible")):
        return (
            "Polymarket US refused this order because of where or who it came "
            "from. Nothing was placed. Check your account's status in the "
            "Polymarket US app.")

    if ("balance" in low and "not enough" in low) or "insufficient" in low \
            or "buying power" in low:
        if action == "sell":
            return (
                "The exchange says this account does not hold that position. The "
                "buy is still resting (see Working below), or its fill has not "
                "been booked yet — give it a moment and check Track.")
        cost = _usd(cost_usd)
        bal = _usd(balance_usd)
        if bal and cost:
            return f"Your buying power ({bal}) is below this order's cost ({cost})."
        if cost:
            return f"Your buying power is below this order's cost ({cost})."
        return "Your buying power is below this order's cost."

    return None


def _qty_step(min_size: Optional[float]) -> float:
    """The smallest step of quantity a market takes. minimumTradeQty is 1 or 0.01
    on every market seen, and it is the only statement of granularity there is:
    a market whose minimum is fractional takes hundredths, any other whole
    contracts. An unread minimum assumes whole contracts, the stricter reading."""
    return 0.01 if (min_size is not None and 0 < min_size < 1) else 1.0


def _floor_qty(size: float, min_size: Optional[float]) -> float:
    """`size` floored onto the market's quantity step. Down, never up: rounding up
    could buy more than was asked for or sell contracts that are not there."""
    step = _qty_step(min_size)
    return round(math.floor(float(size) / step + 1e-9) * step, 2)


def _wire_qty(size: float) -> float:
    """A quantity as it goes on the wire: an int when it is whole, so an order for
    5 contracts reads 5, not 5.0."""
    return int(size) if float(size).is_integer() else float(size)


def _book_behind(bk: dict, live_bid: Optional[float],
                 live_ask: Optional[float]) -> Optional[str]:
    """A warning when the book snapshot has visibly fallen behind the market.

    The snapshot can be 30 seconds old and a 15-minute window moves 10¢ in that
    time, so a fill estimate walked off it can be wrong in either direction. Only
    said when the two disagree, because a book that has not moved is not stale in
    any way that matters.
    """
    age = num(bk.get("ageSec"))
    if age is None or age < term.BOOK_STALE_NOTE_SEC:
        return None
    book_bid, book_ask = num(bk.get("bestBid")), num(bk.get("bestAsk"))
    moved = ((live_bid is not None and book_bid is not None and abs(live_bid - book_bid) >= 0.5)
             or (live_ask is not None and book_ask is not None and abs(live_ask - book_ask) >= 0.5))
    if not moved:
        return None
    return (
        f"The order book here is a {age:.0f}-second-old snapshot and the market has "
        f"moved since (now {_cents_word(live_bid)} bid, {_cents_word(live_ask)} ask), "
        f"so the fill estimate may be off. Your limit price still holds.")


def _cents_word(v: Optional[float]) -> str:
    return "no" if v is None else f"{v:g}¢"


async def preview(req: dict) -> dict:
    """Everything the user should know BEFORE the money leaves.

    Deliberately verbose. A terminal that shows a price without saying whether
    it came from the live book or a cached index is asking for trust, and
    trust is exactly what we are trying not to require.
    """
    cid = str(req.get("conditionId") or "").strip()
    side = "no" if str(req.get("side") or "yes").lower() == "no" else "yes"
    action = "sell" if str(req.get("action") or "buy").lower() == "sell" else "buy"
    req_price = num(req.get("priceCents"))
    req_size = num(req.get("size"))
    order_type = str(req.get("orderType") or "GTC").upper()
    # Paper mode needs no API key — practising BEFORE funding anything is the
    # entire point. Threaded through the request rather than string-matched on
    # the resulting message, so there is one source of truth for what blocks a
    # ticket and the UI can disable its button off the same list.
    paper = bool(req.get("paper"))

    out: dict = {
        "conditionId": cid, "side": side, "action": action,
        "priceCents": None, "requestedPriceCents": req_price or 0.0,
        "size": None, "requestedSize": req_size or 0.0,
        "costUsd": None, "fillableSize": None, "estimatedAvgCents": None,
        "slippageCents": None, "feeUsd": None,
        "maxPayoutUsd": None, "maxLossUsd": None,
        "tickSize": None, "minOrderSize": None,
        # The touch at the moment we priced this ticket. The ticket uses it to
        # say whether a limit would REST rather than fill, and `place()` uses
        # it as the reference price in the message a resting order gets back.
        "bestBidCents": None, "bestAskCents": None,
        "crossesNow": None,
        # What the account can actually spend. None = we do not know, which is
        # a different sentence from $0.00 and never blocks the ticket.
        "availableUsd": None,
        "warnings": [], "blockers": [],
    }

    if not cid:
        out["blockers"].append("No market selected.")
        return out
    # Above 0 and below 100, not 1 and 99: a market on the 0.1¢ tick trades 0.5¢.
    # The tick snap below keeps it on the grid and inside the tradable range.
    if req_price is None or not (0 < req_price < 100):
        out["blockers"].append(
            "Price must be above 0¢ and below 100¢. Polymarket does not accept an "
            "order at 0 or 100.")
        return out
    if req_size is None or req_size <= 0:
        out["blockers"].append("Size must be a positive number of contracts.")
        return out
    if order_type not in ("GTC", "FAK", "FOK"):
        out["blockers"].append(f"Unsupported order type {order_type}.")
        return out
    if req_size > MAX_MANUAL_CONTRACTS:
        out["blockers"].append(
            f"{req_size:,.0f} contracts is over the manual ceiling of "
            f"{MAX_MANUAL_CONTRACTS:,}. Split it, or use a script.")
        return out

    summary = await term.market(cid, include_raw=True)
    if not summary:
        out["blockers"].append(
            "Could not read this market. Refusing to send an order into a "
            "market we cannot price.")
        return out

    raw = summary.get("_raw") or {}
    tick = num(raw.get("orderPriceMinTickSize")) or 0.01
    min_size = num(raw.get("minimumTradeQty"))
    out["tickSize"] = tick
    out["minOrderSize"] = min_size

    snapped = round(api._snap_price_to_tick(req_price / 100.0, tick) * 100.0, 4)
    out["priceCents"] = snapped
    sized = _floor_qty(req_size, min_size)
    if sized <= 0:
        out["blockers"].append(
            f"{req_size:g} contracts is less than this market's smallest order "
            f"step of {_qty_step(min_size):g}.")
        return out
    if abs(sized - req_size) > 1e-9:
        out["warnings"].append(
            f"Size rounded down {req_size:g} → {sized:g}: this market takes "
            f"steps of {_qty_step(min_size):g} contracts.")
    req_size = sized
    out["size"] = req_size
    if abs(snapped - req_price) > 1e-6:
        out["warnings"].append(
            f"Price snapped {req_price:g}¢ → {snapped:g}¢ to match this "
            f"market's {tick:g} tick. The venue rejects anything else.")

    # ── hard state blockers ────────────────────────────────────────────
    if summary.get("status") in ("settled", "closed"):
        out["blockers"].append(
            "This market is closed. Nothing can be traded here.")
    if term.accepting_orders(raw) is False:
        out["blockers"].append(
            "This market is not accepting orders right now.")
    if action == "buy" and min_size is not None and req_size < min_size:
        out["blockers"].append(
            f"This market's minimum order is {min_size:g} contracts. "
            f"Raise your size — we will not silently buy more than you asked "
            f"for.")
    elif action == "sell" and min_size is not None and req_size < min_size:
        # A WARNING, not a blocker. On a buy you can order more; on a sell you
        # hold what you hold, and refusing outright would guarantee you stay
        # stuck. But you need to know the exit might bounce — a stop loss on a
        # sub-minimum holding is not a stop, and finding that out at the moment
        # it matters is the worst time.
        out["warnings"].append(
            f"You are selling {req_size:g} contracts but this market's minimum "
            f"order is {min_size:g}. Polymarket may refuse it — if "
            f"it does, this position can only be closed by letting it settle.")

    # ── walk the real book ─────────────────────────────────────────────
    bk = await term.book(cid, side)
    live_bid, live_ask = term.live_touch(summary, side)
    stale = None
    if bk.get("available"):
        # The touch comes from the market read, which the CDN does not cache; the
        # book's levels can be 30 seconds behind it. Only the depth comes from the
        # snapshot, and only when the market read had no quote does the touch.
        out["bestBidCents"] = live_bid if live_bid is not None else num(bk.get("bestBid"))
        out["bestAskCents"] = live_ask if live_ask is not None else num(bk.get("bestAsk"))
        # Does this price CROSS, or is it an offer? The single distinction the
        # owner's live test turned on: a buy at 14¢ against a 15¢ ask is not a
        # purchase, it is a bid nobody has to take.
        touch = out["bestAskCents"] if action == "buy" else out["bestBidCents"]
        if touch is not None:
            out["crossesNow"] = (snapped >= touch if action == "buy"
                                 else snapped <= touch)
        levels = bk["asks"] if action == "buy" else bk["bids"]
        stale = _book_behind(bk, live_bid, live_ask)
        if stale:
            out["warnings"].append(stale)
        fillable, avg = _walk_book(levels, req_size, snapped, action == "buy")
        out["fillableSize"] = fillable
        out["estimatedAvgCents"] = avg
        if avg is not None:
            out["slippageCents"] = round(avg - snapped, 3)
        if fillable is not None and fillable < req_size:
            msg = (
                f"Only {fillable:g} of {req_size:g} contracts are available at "
                f"{snapped:g}¢ or better.")
            if order_type == "FOK":
                # A WARNING, not a blocker. The book we just read is already a
                # moment old, and the venue handles this case cleanly: an
                # all-or-none order that crosses nothing comes back "unmatched",
                # which place_limit_order reports as a clean zero fill. Refusing
                # to submit made the outcome unreachable — the user could never
                # see the kill that a real FOK is FOR, and paper mode could not
                # simulate it either.
                out["warnings"].append(
                    msg + " An all-or-none order needs the whole size, so this "
                    "will most likely be killed with no fill — which costs you "
                    "nothing.")
            elif order_type == "FAK":
                out["warnings"].append(
                    msg + " The remainder will be killed, not rested.")
            else:
                out["warnings"].append(
                    msg + " The remainder will rest on the book.")
    else:
        out["warnings"].append(
            "Could not read the order book, so there is no fill estimate — "
            f"{bk.get('note') or 'the venue did not answer'}. The order is "
            "still valid; you are just sending it blind.")

    # ── cost, fee, payoff ──────────────────────────────────────────────
    # This has to be a number that can ACTUALLY happen. It used to be
    # `requested size × the average of the FILLABLE SLICE ONLY`, which
    # describes no possible outcome in either direction: on a book with 3 asks
    # at 60¢, an FAK buy of 100 @ 65¢ previewed $60.00 for an order that can
    # spend at most $1.80, and the GTC of the same shape previewed $60.00
    # against a true worst case of $64.85. The figure drives the on-screen
    # cost, the fee, the max loss, the MAX_MANUAL_USD ceiling, the remote
    # per-order cap and the confirmation gate, so a fantasy number there is
    # every one of those gates measuring the wrong thing.
    #
    #   FAK/FOK — the remainder is KILLED, so only the crossed slice trades,
    #             at the average the walk actually found.
    #   GTC     — the remainder RESTS, so the worst case is the crossed slice
    #             at the walked average plus the rest filling at your limit.
    #
    # A sell is the same arithmetic; the figure is proceeds, not cost.
    fillable = out["fillableSize"]
    avg = out["estimatedAvgCents"]
    if fillable is None or stale:
        # The book could not be read, or the market has moved away from the
        # snapshot (both warned about above). With no walk to lean on, price the
        # whole order at the limit: for a GTC that IS the worst case, and for a
        # FAK it is an upper bound. Never an understatement, which is the
        # direction that matters here: a FAK that crosses a live ask the snapshot
        # does not show used to preview $0 cost, $0 fee and $0 max loss, then fill.
        fill_size = req_size
        cost = round(req_size * snapped / 100.0, 2)
    else:
        crossed = min(float(fillable), req_size)
        rest = max(0.0, req_size - crossed)
        crossed_usd = crossed * (avg if avg is not None else snapped) / 100.0
        if order_type == "GTC":
            fill_size = req_size
            cost = round(crossed_usd + rest * snapped / 100.0, 2)
        else:
            fill_size = crossed
            cost = round(crossed_usd, 2)
    out["costUsd"] = cost

    # Fees are charged per share TRADED at the price actually paid, so both
    # follow the corrected cost rather than the requested size.
    fill_price = (round(cost * 100.0 / fill_size, 4) if fill_size > 0
                  else (avg if avg is not None else snapped))
    fee = taker_fee_usd(fill_price, fill_size, api._fee_schedule_from_gamma(raw))
    if fee is None:
        out["warnings"].append(
            "We could not determine this market's fee rate, so the cost above "
            "EXCLUDES fees. Taker fees on Polymarket run up to ~1.75¢ per "
            "share at even odds.")
    else:
        out["feeUsd"] = fee
        if order_type == "GTC":
            out["warnings"].append(
                "Resting (GTC) orders that fill as MAKER pay no fee. The fee "
                "shown is the taker case — the worst case for you.")

    if action == "buy":
        out["maxPayoutUsd"] = round(fill_size * 1.0, 2)
        out["maxLossUsd"] = round(cost + (out["feeUsd"] or 0.0), 2)
    else:
        out["maxPayoutUsd"] = cost
        # Selling contracts you hold: the loss is the upside you gave up — on
        # the contracts that actually leave, not on the ones a killed
        # remainder never sold.
        out["maxLossUsd"] = round(fill_size * 1.0 - cost, 2)

    if cost > MAX_MANUAL_USD:
        out["blockers"].append(
            f"${cost:,.2f} is over the ${MAX_MANUAL_USD:,.0f} per-order manual "
            f"ceiling.")

    # ── advisory: what the resolution report thinks ────────────────────
    spread = summary.get("spread")
    if spread is not None and spread > 10:
        out["warnings"].append(
            f"The spread is {spread:.1f}¢ wide. Getting out of this position "
            f"will cost meaningfully more than getting in.")
    depth = ((num(bk.get("bidDepthUsd")) or 0.0) + (num(bk.get("askDepthUsd")) or 0.0)
             if bk.get("available") else None)
    if depth is not None and depth < 2000:
        out["warnings"].append(
            f"Only ${depth:,.0f} of resting liquidity in this market.")
    hrs = summary.get("hoursToClose")
    if hrs is not None and 0 < hrs < 1:
        out["warnings"].append(
            f"This market closes in {hrs * 60:.0f} minutes.")

    # ── can we actually trade at all? ──────────────────────────────────
    if paper:
        out["warnings"].append(
            "Paper mode: no API key is needed and nothing reaches the exchange.")
    elif not auth.credentials_present():
        out["blockers"].append(
            "No Polymarket US API key is saved. Add one in Settings to trade "
            "live, or switch to Paper.")

    # ── can this account AFFORD it? ────────────────────────────────────
    #
    # The exchange already refuses an unaffordable buy, but it refuses AFTER
    # the user has reviewed and confirmed it. In the owner's live test three
    # orders were reviewed and sent against a $2.94 account before anything said
    # so. So the ticket says it first.
    #
    # A balance we could not read blocks NOTHING. Refusing an order because a
    # balance read failed would be the same lie in the other direction.
    avail = await _available_cash_usd(paper)
    out["availableUsd"] = avail
    if action == "buy":
        if avail is None:
            out["warnings"].append(
                "We could not read this account's balance, so nothing here "
                "checks whether you can afford this order. The exchange will.")
        elif cost is not None and cost > avail + 1e-9:
            out["blockers"].append(
                (f"Your paper account has ${avail:,.2f} not already held by "
                 f"resting buys; this order costs ${cost:,.2f}.") if paper else
                (f"Your buying power is ${avail:,.2f}; this order costs "
                 f"${cost:,.2f}."))

    return out


# ────────────────────────────────────────────────────────────────────────
# Place / cancel
# ────────────────────────────────────────────────────────────────────────


def _record_order(row: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO terminal_orders
               (condition_id, title, side, action, order_type,
                requested_size, requested_price_cents, sent_size,
                sent_price_cents, order_id, status, error, origin,
                standing_id, network)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["condition_id"], row.get("title") or "", row["side"],
             row["action"], row["order_type"], row["requested_size"],
             row["requested_price_cents"], row.get("sent_size"),
             row.get("sent_price_cents"), row.get("order_id"), row["status"],
             row.get("error"), row.get("origin") or "manual",
             row.get("standing_id"), _net()),
        )
        return int(cur.lastrowid or 0)


def _paper_mode_on() -> bool:
    """Is the terminal's PAPER switch on right now?

    Read from the live config rather than passed in, so every caller of
    `place()` inherits the check — the standing-instruction tick and the chat
    bots call it directly and used to bypass the RPC handler's paper gate.
    Asked through account_live, which also reads account_mode: before the
    first setConfig the config is the raw default, where only account_mode
    says Paper. Lazy import: service imports this module lazily too."""
    try:
        import service
        cfg = service.STATE.cfg or {}
    except Exception:  # noqa: BLE001 — no service (tests, CLI) means no paper
        return False
    from config import account_live
    return not account_live(cfg) or bool(cfg.get("terminalPaperMode"))


def _definite_refusal(e: BaseException) -> bool:
    """True when a failed send PROVABLY placed nothing.

    `place()` used to file every non-ambiguous failure under 'error', which
    the agent rails read as "may have reached the venue" — so a geoblocked
    user's refused buys held their spend cap and position slots forever. These
    cases cannot have placed an order:

      * a `ValueError` from the order builder — raised before anything is
        signed or sent (bad side/price/size/type);
      * a 4xx `PolymarketAPIError` — the exchange's own definitive "no": a
        refused account, insufficient buying power, minimum size, invalid tick,
        the engine-restart 425, plus this app's own pre-send refusals, which are
        4xx too (kill switch, throttle, collar, self-cross, the NO-buy lock);
      * a connection that was never established (refused/connect-timeout/
        pool-timeout), which `polymarket_api._post_outcome_ambiguous` also
        classes as "never sent".

    Everything else (a read timeout, a dropped reply, a 5xx, anything
    unexpected) stays ambiguous: the order may be on the book.
    """
    if isinstance(e, api.OrderStatusUnknownError):
        return False
    if isinstance(e, ValueError):
        # A reply that could not be decoded is not a pre-send validation error.
        return not isinstance(e, (json.JSONDecodeError, UnicodeError))
    if isinstance(e, api.PostOnlyModeError):
        return True
    if isinstance(e, api.PolymarketAPIError):
        st = int(getattr(e, "status", 0) or 0)
        # 408 is the server timing the request out — not a clean refusal.
        return 400 <= st < 500 and st != 408
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return False
    return isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout,
                          httpx.PoolTimeout))


def _refused(message: str) -> dict:
    return {
        "ok": False, "orderId": None, "status": None, "filledSize": None,
        "avgPriceCents": None, "localId": None, "message": message,
    }


def _reply_order(resp: Optional[dict]) -> Optional[dict]:
    """The order inside a placement reply: the newest execution carries its state.
    None when the reply has none, which is the exchange accepting the order and
    not answering inside its wait. That is unknown, not unfilled."""
    raw = (resp or {}).get("raw")
    execs = raw.get("executions") if isinstance(raw, dict) else None
    last = execs[-1] if isinstance(execs, list) and execs else None
    order = last.get("order") if isinstance(last, dict) else None
    return order if isinstance(order, dict) else None


def _matched_size(resp: Optional[dict]) -> Optional[float]:
    """How many contracts the venue says crossed, or None when it did not say.
    Unknown is not zero."""
    order = _reply_order(resp)
    return None if order is None else float(us.order_view(order)["size_matched"])


def _reply_avg_cents(resp: Optional[dict]) -> Optional[float]:
    """The average fill price of OUR side, in cents, once anything filled."""
    order = _reply_order(resp)
    if order is None:
        return None
    view = us.order_view(order)
    if not view["size_matched"] or not view["price"]:
        return None
    return round(float(view["price"]) * 100.0, 4)


async def place(req: dict, *, origin: str = "manual",
                standing_id: Optional[str] = None) -> dict:
    """Send one hand-placed order.

    Every gate here runs in the MAIN backend process, never only in the UI.
    A renderer that lies about `confirmUsd` gets refused, because the number
    is recomputed from the same book the preview used.
    """
    # Paper mode must be impossible to trade around, from ANY caller. Checked
    # first, before the preview touches the book.
    if _paper_mode_on():
        return _refused(
            "Paper mode is on, so no real order was sent. Turn it off in "
            "Settings if you meant to trade live.")
    pv = await preview(req)
    if pv["blockers"]:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": pv["blockers"][0],
        }

    confirm = num(req.get("confirmUsd"))
    if confirm is None:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": "Order refused: no confirmed cost was supplied.",
        }
    actual = pv["costUsd"]
    if actual is not None and abs(actual - confirm) > CONFIRM_TOLERANCE_USD:
        return {
            "ok": False, "orderId": None, "status": None, "filledSize": None,
            "avgPriceCents": None, "localId": None,
            "message": (
                f"Order refused: the book moved. You confirmed ${confirm:,.2f} "
                f"but this order would now cost ${actual:,.2f}. Re-check the "
                f"ticket and confirm again."),
        }

    cid = str(req["conditionId"])
    side = pv["side"]
    action = pv["action"]
    order_type = str(req.get("orderType") or "GTC").upper()
    size = _wire_qty(pv["size"])
    # The tick-snapped price goes out exactly as previewed. Near the extremes
    # a market trades in 0.1¢ ticks, and the confirmation, the ceilings and the
    # agent's edge-after-fee check all ran on THIS price. Rounding it to whole
    # cents here sent a 3.5¢ buy out at 4¢ (+14% on price) and, by banker's
    # rounding, a 2.5¢ one at 2¢.
    price_cents = round(float(pv["priceCents"]), 4)

    summary = await term.market(cid)
    title = (summary or {}).get("title") or ""

    base = {
        "condition_id": cid, "title": title, "side": side, "action": action,
        "order_type": order_type,
        "requested_size": pv["requestedSize"],
        "requested_price_cents": pv["requestedPriceCents"],
        "sent_size": size, "sent_price_cents": price_cents,
        "origin": origin, "standing_id": standing_id,
    }

    try:
        resp = await api.place_limit_order(
            ticker=cid, side=side, action=action, count=size,
            price_cents=price_cents, order_type=order_type,
            client_order_id=f"term_{uuid.uuid4().hex[:12]}",
        )
    except api.OrderStatusUnknownError as e:
        # Sent, but neither the reply nor a lookup says what became of it. The
        # id is a placeholder: the venue assigns ids and has no idempotency key,
        # so a lost reply loses the id too. The row keeps the placeholder so the
        # attempt stays visible; the user is told to look before resending.
        local = _record_order({**base, "order_id": e.order_id, "status": "unknown",
                               "error": str(e)})
        return {
            "ok": False, "orderId": e.order_id, "status": "unknown",
            "filledSize": None, "avgPriceCents": None, "localId": local,
            "message": (
                "Polymarket did not confirm this order and it could not be found "
                "yet. It may be resting, filled, or never have arrived. Check your "
                "resting orders and Track before sending it again."),
        }
    except Exception as e:  # noqa: BLE001 — every failure must leave a row
        # A terminal row on failure is mandatory: the exchange has no client-side
        # idempotency key, so if a response is lost AFTER the venue accepted
        # the order, a blind retry opens a SECOND position. Recording the
        # attempt is what lets the user see the ambiguity later.
        # The RAW venue text stays on the row (the order log shows it); the
        # message the user reads is the translated one, when we have a
        # translation. An untranslated rejection is passed through verbatim
        # rather than replaced by a vaguer sentence.
        #
        # A DEFINITE refusal (see `_definite_refusal`) is 'rejected': nothing
        # was placed, nothing filled, and a caller that books spend or slots
        # must release them. Only a failure that may have placed the order
        # keeps 'error' — the "check before resending" status.
        rejected = _definite_refusal(e)
        st = "rejected" if rejected else "error"
        local = _record_order({**base, "order_id": None, "status": st,
                               "error": f"{type(e).__name__}: {e}"})
        # This app's own refusal (kill switch, throttle, collar, self-cross, the
        # NO-buy lock) is already the sentence to read. Run through the translator
        # it could pick up a venue meaning it never had.
        plain = None if isinstance(e, api.LocalOrderRefusal) else translate_venue_error(
            str(e), action=action, cost_usd=pv.get("costUsd"),
            balance_usd=pv.get("availableUsd"))
        if not rejected:
            return {
                "ok": False, "orderId": None, "status": "error", "filledSize": None,
                "avgPriceCents": None, "localId": local,
                "message": plain or str(e),
            }
        http = int(getattr(e, "status", 0) or 0) if isinstance(
            e, api.PolymarketAPIError) else None
        return {
            "ok": False, "orderId": None, "status": "rejected",
            # Definite: the venue (or our own pre-send check) refused it, so
            # the fill is a known zero, not an unknown.
            "filledSize": 0.0,
            "avgPriceCents": None, "localId": local,
            "message": plain or str(e),
            "httpStatus": http or None,
            "geoblocked": api._is_region_refusal(http or 0, getattr(e, "body", "")),
        }

    order = (resp or {}).get("order") or {}
    oid = str(order.get("order_id") or "") or None
    status = str(order.get("status") or "") or "placed"

    if not oid and status.lower() in ("unmatched", "killed", "canceled", "cancelled"):
        local = _record_order({**base, "order_id": None, "status": "unmatched",
                               "error": None})
        return {
            "ok": True, "orderId": None, "status": "unmatched",
            "filledSize": 0.0, "avgPriceCents": None, "localId": local,
            "message": (
                "Nothing crossed — the order was killed with a zero fill. "
                "That is the fill-and-kill contract working, not a failure."),
        }

    local = _record_order({**base, "order_id": oid, "status": "placed",
                           "error": None})

    # Reconcile in the background. The figures on the Track screen come from
    # the venue's record of the order, not from this response.
    _spawn(_reconcile_soon(oid))

    # ── what actually happened, said in the right tense ────────────────
    #
    # "Order sent … fill details appear once it reconciles" is true of a fill
    # and a LIE of omission about a rest. In the owner's live test a buy of 5
    # YES at 14¢ against a 15¢ ask got that sentence, read as "bought", and
    # the next action was an attempt to sell a position that did not exist.
    # A resting order is not a position, and this is the one place that can
    # say so at the moment it matters.
    matched = _matched_size(resp)
    avg_cents = _reply_avg_cents(resp)
    status_l = status.lower()
    ref = pv.get("bestAskCents") if action == "buy" else pv.get("bestBidCents")
    ref_label = "ask" if action == "buy" else "bid"
    at = f"{avg_cents:g}¢" if avg_cents is not None else f"{price_cents:g}¢ or better"

    if matched is None:
        # The exchange took the order and did not answer inside its wait, or the
        # order was recovered after a lost reply. Saying "nothing has filled" here
        # would be a guess, and a user who believes it can double up.
        msg = (
            f"Order accepted: {action} {size} {side.upper()} at {price_cents:g}¢ "
            f"({order_type}). The exchange has not said yet whether it filled, so "
            f"do not count it as a position or resend it until Track shows what "
            f"happened.")
    elif matched <= 0 and status_l == "canceled":
        msg = (
            f"The exchange canceled this order at {price_cents:g}¢ without filling "
            f"any of it. Nothing changed hands.")
    elif matched <= 0 and order_type == "GTC":
        if ref is not None:
            msg = (
                f"Resting at {price_cents:g}¢ — the best {ref_label} is {ref:g}¢, "
                f"so nothing has filled. It is not a position until it fills; "
                f"cancel or reprice from the panel below.")
        else:
            msg = (
                f"Resting at {price_cents:g}¢ — nothing has filled, and we could "
                f"not read the touch. It is not a position until it fills; cancel "
                f"or reprice from the panel below.")
    elif matched < size:
        rest = round(size - matched, 4)
        if order_type == "GTC" and status_l != "canceled":
            msg = (
                f"Filled {matched:g} of {size} at {at}; the remaining {rest:g} is "
                f"RESTING at {price_cents:g}¢ and is not a position until it fills "
                f"— cancel or reprice it from the panel below.")
        else:
            msg = (
                f"Filled {matched:g} of {size} at {at}; the remaining {rest:g} was "
                f"killed, not rested — that is what fill-and-kill means.")
    else:
        msg = f"Filled {matched:g} {side.upper()} at {at}."

    return {
        "ok": True, "orderId": oid, "status": status,
        "filledSize": matched, "avgPriceCents": avg_cents, "localId": local,
        "message": msg,
    }


async def _reconcile_soon(order_id: Optional[str]) -> None:
    """Give the venue a moment to book the trade, then pull the real numbers."""
    await asyncio.sleep(3.0)
    try:
        await reconcile_fills()
    except Exception as e:  # noqa: BLE001
        logger.debug("terminal reconcile after %s failed: %s", order_id, e)
    # The ticket reads spendable cash from this cache, which otherwise waits out
    # the poll interval: a minute of offering money the order just spent.
    try:
        import service
        import trader
        await trader.refresh_balance(service.STATE.cfg or {}, force=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("balance refresh after %s failed: %s", order_id, e)


async def cancel(order_id: str) -> dict:
    oid = str(order_id or "").strip()
    if not oid:
        return {"ok": False, "message": "No order id."}
    if oid.startswith(api.UNKNOWN_ID_PREFIX):
        # A lost reply loses the id too, so there is nothing to cancel by. Telling
        # the user it is canceled, or that the venue refused, would both be false.
        return {"ok": False, "notCanceled": "no order id",
                "message": ("Polymarket never confirmed this order, so it has no id "
                            "to cancel by. If it reached the exchange it shows under "
                            "your resting orders; cancel it from there.")}
    # The API needs the order's market. Our own row has it, which saves a read; an
    # order placed elsewhere is looked up by the API module.
    market = ""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT condition_id FROM terminal_orders WHERE order_id=? LIMIT 1",
                (oid,)).fetchone()
        market = str(row["condition_id"]) if row else ""
    except Exception as e:  # noqa: BLE001 — the API module can still look it up
        logger.debug("terminal cancel: local row read failed for %s: %s", oid, e)
    try:
        resp = await api.cancel_order(oid, market=market)
    except api.PolymarketAPIError as e:
        # A refusal ("already filled", "no such order") is the venue's answer, not
        # a failed call. A status the venue did not give (a dropped connection, a
        # 5xx) says nothing either way, so it must not read as "not canceled".
        st = int(getattr(e, "status", 0) or 0)
        if 400 <= st < 500:
            why = str(getattr(e, "body", "") or e)
            return {"ok": False, "notCanceled": why,
                    "message": (f"Polymarket did not cancel order {oid[:10]}… "
                                f"({why}). If it filled, the fill shows on Track.")}
        return {"ok": False, "message": f"Cancel failed: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"Cancel failed: {e}"}
    # Only the order actually acknowledged counts. Taking any 200 on trust told
    # the user "Canceled" about an order that had FILLED, rewrote its row for good,
    # and let Reprice send a replacement on top of the fill.
    resp = resp if isinstance(resp, dict) else {}
    done = {str(x).lower() for x in (resp.get("canceled") or [])}
    if oid.lower() not in done:
        return {"ok": False, "notCanceled": "not acknowledged",
                "message": (f"Polymarket did not cancel order {oid[:10]}… "
                            f"(not acknowledged). If it filled, the fill shows on Track.")}
    with _conn() as conn:
        # A row reconcile already marked filled keeps that status: a cancel
        # can only take off what was still resting, never undo a fill.
        conn.execute(
            "UPDATE terminal_orders SET status='canceled' "
            "WHERE order_id=? AND COALESCE(status,'') != 'filled'", (oid,))
    # An AI agent's order is booked cancelled on the agent ledger HERE, so
    # every cancel path does it — the app's Cancel and Reprice, the phone's
    # /cancelall, the agent's own tools. Skipped, a cancelled agent buy held
    # its slot and spend, and a cancelled sell stranded the agent's shares.
    # A no-op (one DB read) for an order no agent placed.
    try:
        import mcp_rails
        await mcp_rails.note_cancelled(oid)
    except Exception as e:  # noqa: BLE001 — the row keeps counting in full
        logger.debug("terminal cancel: agent bookkeeping for %s failed: %s", oid, e)
    return {"ok": True, "message": f"Canceled order {oid[:10]}…"}


def _iso_or_none(v: Any) -> Optional[str]:
    """A venue timestamp (nanoseconds and all) as a plain ISO instant."""
    ts = us.parse_iso_epoch(v)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


async def resting_orders(condition_id: Optional[str] = None) -> dict:
    """Live resting orders, from the venue — tagged with WHO placed each one.

    A terminal must never blur what you did with what an engine did, so an
    order id we have no local row for is reported as 'external', not silently
    absorbed into your manual trades.

    `condition_id` narrows the list to one market AND turns on the touch read:
    each row gains `bestTouchCents`, the price this order would have to reach
    to fill (a buy needs the best ask, a sell the best bid). That is what
    turns "resting at 14¢" into "resting at 14¢, the ask is 15¢" — the
    sentence that tells the user why nothing has happened. It costs one market
    read, which is why it is only done for the one market the user is looking at
    and never on the Track-wide list.
    """
    want = str(condition_id or "").strip() or None
    try:
        rows = await api.get_open_orders([want] if want else None)
    except Exception as e:  # noqa: BLE001
        return {"orders": [], "fetchedAt": _now_iso(),
                "note": f"Could not read your open orders: {e}"}

    mine: dict[str, dict] = {}
    with _conn() as conn:
        for r in conn.execute(
            "SELECT order_id, condition_id, side, title, origin, standing_id "
            "FROM terminal_orders WHERE order_id IS NOT NULL"
        ):
            mine[str(r["order_id"])] = dict(r)

    out = []
    for o in rows:
        if not isinstance(o, dict):
            continue
        oid = str(o.get("id") or "")
        local = mine.get(oid)
        cid = str(o.get("marketSlug") or (local or {}).get("condition_id") or "")
        if want and cid != want:
            continue
        # The venue quotes every order on the YES side, so a NO order's price is
        # mirrored here. Its `intent` says which side it is, for any order.
        side = us.intent_side(o.get("intent"))
        yes_price = us.amount(o.get("price"))
        price = cents(us.side_price(side, yes_price))
        size = num(o.get("quantity"))
        filled = num(o.get("cumQuantity"))
        left = num(o.get("leavesQuantity"))
        out.append({
            "orderId": oid,
            "conditionId": cid,
            "title": (local or {}).get("title") or None,
            "side": side,
            "action": us.intent_action(o.get("intent")),
            "priceCents": price,
            "size": size,
            "filledSize": filled,
            # What is still on the book. None when it cannot be worked out —
            # an unknown remainder is not a remainder of zero.
            "remainingSize": (left if left is not None
                              else None if size is None or filled is None
                              else round(size - filled, 4)),
            # Filled in below for the one market we were asked about.
            "bestTouchCents": None,
            "createdAt": _iso_or_none(o.get("createTime") or o.get("insertTime")),
            "origin": (local or {}).get("origin") or "external",
            "source": "venue",
        })

    if want and out:
        # One market read, not one book per side: it is fresher than the book, and
        # both sides mirror from it.
        try:
            m = await term.market(want)
        except Exception as e:  # noqa: BLE001 — a missing touch is None
            logger.debug("resting-order touch read failed: %s", e)
            m = None
        if m:
            for r in out:
                bid, ask = term.side_touch(m, r["side"])
                r["bestTouchCents"] = ask if r["action"] == "buy" else bid

    return {"orders": out, "fetchedAt": _now_iso(),
            "note": None if out else (
                "No resting orders in this market." if want
                else "No resting orders.")}


# ────────────────────────────────────────────────────────────────────────
# Flatten — sell every open position in the account
# ────────────────────────────────────────────────────────────────────────
#
# The engines each flatten what THEY opened. This flattens what you HOLD,
# which is a different and larger set: positions opened by hand, by a script,
# by the copy engine, or before you ever installed this app.
#
# Three rules make it safe enough to put behind one button:
#
#   1. Every sell goes through `place()`. Not around it — through it. So the
#      paper gate, the manual ceilings, the confirmation-drift check and the
#      order-log row all apply exactly as they do to a click.
#   2. It never guesses. An unreadable account sells NOTHING, a market with no
#      bid is refused rather than sold blind, and a sub-minimum holding is
#      refused with the reason instead of being fired at the venue to bounce.
#   3. One failure does not stop the rest. A stuck position is the single most
#      likely thing here, and abandoning the other nineteen because of it is
#      the opposite of de-risking.


async def _flatten_positions() -> tuple[list[tuple[str, str, float]], int, int]:
    """(OPEN sellable positions, how many held nothing, how many had settled).

    Read the way `portfolio()` reads — `strict=True`, so a failed positions read
    RAISES rather than coming back empty. "You hold nothing" and "we could not
    tell" must never be the same answer to a function whose next step is selling.

    SETTLED POSITIONS ARE NOT SELLABLE, and this is where that has to be
    decided — before any book read, not after one fails. A resolved market has an
    empty book, and the exchange pays settlement out by itself: there is nothing
    to sell and nothing to claim. So they are dropped here and reported as one
    count with one sentence, rather than as 105 identical refusals.

    Settlement is read exactly the way `portfolio()` reads it — the same market
    summary — so the two screens can never disagree about which positions are
    still live. A lookup that fails
    leaves the position OPEN (unknown is not settled), and the book read then
    refuses it honestly, one row at a time.
    """
    rows = await api.get_positions(strict=True)
    held: list[tuple[str, str, float]] = []
    empty = 0
    for p in rows or []:
        cid = str(p.get("ticker") or "")
        signed = float(p.get("position_fp") or 0.0)
        if not cid:
            continue
        if signed == 0:
            empty += 1
            continue
        held.append((cid, "yes" if signed > 0 else "no", abs(signed)))

    if not held:
        return [], empty, 0

    settled: set[str] = set()
    try:
        for m in await term._summaries_for([cid for cid, _s, _z in held]):
            r = m.get("resolvedOutcome")
            # `status`, not just the outcome: a dead heat is settled with neither
            # side winning outright.
            if r is not None or m.get("status") == "settled":
                settled.add(str(m.get("conditionId") or ""))
            # Same permanent cache the ledger record reads, seeded from a
            # lookup we have already paid for.
            if r in ("yes", "no"):
                _resolution_remember(str(m.get("conditionId") or ""), str(r))
    except Exception as e:  # noqa: BLE001 — unknown, which means "still open"
        logger.debug("flatten: resolution lookup failed: %s", e)

    out = [t for t in held if t[0] not in settled]
    return out, empty, len(held) - len(out)


def _engine_tracked_markets() -> Optional[set[str]]:
    """Condition ids some ENGINE still has an open row for.

    Read-only, and best-effort: `None` means we could not tell, which is
    reported as unknown rather than as zero. Selling one of these from here is
    fine — each engine's positions reconcile books an externally-closed position
    as `sold` — but the user should be told it happened, because it is the one
    case where this button reaches past the terminal's own trades.
    """
    try:
        import db
        keys: set[str] = set()
        with db.get_db() as conn:
            for r in db.get_open_bot_positions(conn):
                keys.add(str(r.get("ticker") or ""))
            for r in db.get_open_crypto15m(conn, _net()):
                keys.add(str(r.get("ticker") or ""))
        keys.discard("")
        return keys
    except Exception as e:  # noqa: BLE001
        logger.debug("flatten: engine position read failed: %s", e)
        return None


async def _flatten_quote(cid: str, side: str,
                         size: float) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    """Price one exit → (request, preview, refusal).

    A refusal is a REASON, and the caller surfaces it verbatim. The order is a
    marketable FAK at the current best bid: a limit above the bid rests (and a
    resting exit is not an exit), and the walked average from the preview is
    what the confirmation gate in `place()` then re-checks.
    """
    bk = await term.book(cid, side)
    if not bk.get("available"):
        # The note is a finished SENTENCE, so it already ends in a full stop.
        # Appending our own gave "…not an empty book.. Nothing was sold…".
        why = str(bk.get("note") or "the venue did not answer").strip().rstrip(".")
        return None, None, (
            f"Could not read the order book — {why}. "
            f"Nothing was sold here rather than selling blind.")
    bid = num(bk.get("bestBid"))
    # The current bid, not the snapshot's: the snapshot can be 30s behind, and an
    # exit limited above where the market now is just gets killed. A market read
    # that fails falls back on the snapshot.
    summary = await term.market(cid)
    if summary:
        live_bid, _live_ask = term.side_touch(summary, side)
        if live_bid is not None or summary.get("ask") is not None or summary.get("bid") is not None:
            bid = live_bid
    if bid is None:
        return None, None, (
            "Nobody is bidding on this side right now, so there is nothing to "
            "sell into. Left open.")

    req: dict = {
        "conditionId": cid, "side": side, "action": "sell",
        "priceCents": bid, "size": size,
        "orderType": "FAK",
    }
    pv = await preview(req)
    if pv["blockers"]:
        return req, pv, pv["blockers"][0]
    min_size = pv.get("minOrderSize")
    if min_size is not None and size < min_size:
        # `preview` only WARNS here, because on a hand-placed sell you may want
        # to try anyway. A flatten is unattended, so a refusal the venue would
        # issue in a second is better issued here with the reason attached.
        return req, pv, (
            f"{size:g} contract(s) is below this market's minimum order of "
            f"{min_size:g}, so Polymarket would reject the sell. "
            f"This position can only be closed by letting it settle.")
    req["confirmUsd"] = pv.get("costUsd")
    return req, pv, None


def _flatten_partial_note(pv: Optional[dict]) -> Optional[str]:
    """The one preview warning that changes what the user still holds."""
    for w in ((pv or {}).get("warnings") or []):
        if "are available at" in w:
            return w
    return None


async def flatten_preview() -> dict:
    """What `flatten_wallet` would sell, and roughly for how much.

    Read-only — it prices, it does not send. Every figure is an ESTIMATE off a
    book that is already a moment old, and it says so; an unknown one is null
    so the caller prints an em dash rather than a confident number.
    """
    out: dict = {
        "ok": False, "positions": [], "count": 0, "skipped": 0,
        "settledSkipped": 0,
        "proceedsUsd": None, "engineTracked": None,
        "paper": _paper_mode_on(), "message": "",
    }
    try:
        targets, empty, settled = await _flatten_positions()
    except Exception as e:  # noqa: BLE001
        out["message"] = (
            f"Could not read your positions ({type(e).__name__}: {e}), so "
            f"there is nothing to show. Nothing has been sold.")
        return out

    out["skipped"] = empty
    out["settledSkipped"] = settled
    tracked = _engine_tracked_markets()
    proceeds = 0.0
    proceeds_known = True
    sellable = 0
    tracked_n = 0

    for cid, side, size in targets:
        try:
            _req, pv, refusal = await _flatten_quote(cid, side, size)
        except Exception as e:  # noqa: BLE001
            pv, refusal = None, (
                f"Could not price this market ({type(e).__name__}: {e}).")
        row = {
            "conditionId": cid, "side": side, "size": round(size, 2),
            "bidCents": (pv or {}).get("priceCents"),
            "fillableSize": (pv or {}).get("fillableSize"),
            "proceedsUsd": None if refusal else (pv or {}).get("costUsd"),
            "blocked": refusal,
            "partial": None if refusal else _flatten_partial_note(pv),
            "engineTracked": None if tracked is None else (cid in tracked),
        }
        out["positions"].append(row)
        if refusal:
            continue
        sellable += 1
        if tracked is not None and cid in tracked:
            tracked_n += 1
        if row["proceedsUsd"] is None:
            proceeds_known = False
        else:
            proceeds += float(row["proceedsUsd"])

    out["count"] = sellable
    out["engineTracked"] = None if tracked is None else tracked_n
    out["proceedsUsd"] = round(proceeds, 2) if proceeds_known else None
    out["ok"] = True
    # One sentence for the settled pile, appended to whatever the open
    # positions have to say. Never one refusal row each.
    settled_note = (
        f" {settled} settled position(s) were left out: a resolved market has "
        f"no book to sell into, and the exchange pays settlement automatically. "
        f"They are in the settled record, not in this list."
        if settled else "")
    if not targets and settled:
        out["message"] = (
            f"Nothing to sell — every position in the account is settled. All "
            f"{settled} of them: a resolved market has no book to sell into, and "
            f"the exchange pays settlement automatically. They are in the settled "
            f"record, not in this list.")
    elif not targets:
        out["message"] = "Nothing to sell — this account holds no open positions."
    elif not sellable:
        out["message"] = (
            f"{len(targets)} open position(s) held, none of them sellable "
            f"right now — see the reason on each." + settled_note)
    else:
        out["message"] = (
            f"{sellable} position(s) would be sold at the best bid. The "
            f"proceeds figure is an estimate off the book as it is now, not a "
            f"quote." + settled_note)
    return out


async def flatten_wallet(*, allow_trading: bool = True, origin: str = "manual",
                         reason: str = "flatten") -> dict:
    """Sell every open position in the account, one at a time.

    The name is historical; service.py and the agents call it.

    Counts: ``attempted`` is how many exits were sent or refused by us,
    ``sold`` and ``refused`` split those two ways (a venue rejection, and a
    fill-and-kill that crossed nothing, count as refused — the position is
    still open either way, which is the only thing
    the user cares about), and ``skipped`` is rows that held nothing to begin
    with. ``proceedsUsd`` is the ESTIMATE from the previews, null when any
    accepted sell could not be priced — the real number arrives when the fills
    reconcile against Polymarket's own record of each order.

    Serialised deliberately. There is no burst to be gained: the exchange rate
    limits us anyway, and firing twenty signed sells at once turns one bad
    read into twenty.
    """
    base: dict = {
        "ok": False, "attempted": 0, "sold": 0, "refused": 0, "skipped": 0,
        "settledSkipped": 0,
        "proceedsUsd": None, "engineTrackedSold": None, "reason": reason,
        "results": [], "message": "",
    }

    # `place()` refuses each of these on its own, which is the gate that
    # matters. Checking here too is about the MESSAGE: twenty identical "paper
    # mode is on" lines is a worse answer than one.
    if _paper_mode_on():
        return {**base, "message": (
            "Paper mode is on, so nothing was sold. Not one order left this "
            "machine. Turn paper mode off in Settings if you meant to flatten "
            "for real.")}
    if not allow_trading:
        return {**base, "message": (
            "Live trading is off, so nothing was sold. Flatten is held to the "
            "same switch as every other order.")}

    try:
        targets, empty, settled = await _flatten_positions()
    except Exception as e:  # noqa: BLE001
        return {**base, "message": (
            f"Could not read your positions ({type(e).__name__}: {e}), so "
            f"NOTHING was sold. Selling against positions we cannot read is "
            f"worse than waiting — try again in a moment.")}

    if not targets:
        return {**base, "ok": True, "skipped": empty, "settledSkipped": settled,
                "proceedsUsd": 0.0, "engineTrackedSold": 0,
                "message": (
                    f"Nothing to sell — every position in the account is "
                    f"settled. All {settled} of them are resolved markets, "
                    f"which have no book to sell into, and the exchange pays "
                    f"settlement automatically."
                    if settled else
                    "Nothing to sell — this account holds no open positions.")}

    tracked = _engine_tracked_markets()
    results: list[dict] = []
    sold = refused = tracked_sold = 0
    proceeds = 0.0
    proceeds_known = True

    for cid, side, size in targets:
        try:
            req, pv, refusal = await _flatten_quote(cid, side, size)
        except Exception as e:  # noqa: BLE001
            req, pv, refusal = None, None, (
                f"Could not price this market ({type(e).__name__}: {e}), so it "
                f"was left alone.")
        if refusal or req is None:
            refused += 1
            results.append({"conditionId": cid, "side": side,
                            "size": round(size, 2), "ok": False,
                            "message": refusal or "Refused."})
            continue

        res = await place(req, origin=origin)
        ok = bool(res.get("ok"))
        msg = str(res.get("message") or "")
        # A fill-and-kill that crossed nothing comes back ok with a zero fill:
        # the venue took the order and killed it, and the position is exactly
        # as open as before. That is not a sale.
        if ok and (str(res.get("status") or "").lower() == "unmatched"
                   or num(res.get("filledSize")) == 0):
            ok = False
            msg = (f"{msg} Nothing sold — this position is still open.").strip()
        if ok:
            sold += 1
            if tracked is not None and cid in tracked:
                tracked_sold += 1
            est = pv.get("costUsd") if pv else None
            if est is None:
                proceeds_known = False
            else:
                proceeds += float(est)
            # A killed remainder leaves the position PARTLY open, and a result
            # that only says "order sent" hides that.
            partial = _flatten_partial_note(pv)
            if partial:
                msg = f"{msg} {partial}".strip()
        else:
            refused += 1
        results.append({"conditionId": cid, "side": side,
                        "size": round(size, 2), "ok": ok, "message": msg})

    attempted = sold + refused
    proceeds_usd = round(proceeds, 2) if proceeds_known else None

    parts = [f"Sold {sold} of {attempted} open position(s) at the best bid"]
    parts.append(
        f" for about ${proceeds_usd:,.2f} (estimated from the book — the "
        f"exact figure lands when the fills reconcile)."
        if proceeds_usd is not None else
        " — the proceeds could not be estimated, so the total is unknown "
        "until the fills reconcile.")
    if refused:
        parts.append(
            f" {refused} could not be sold; each one says why, and those "
            f"positions are still open.")
    if empty:
        parts.append(f" {empty} position row(s) held nothing and were skipped.")
    if settled:
        parts.append(
            f" {settled} settled position(s) were left alone: a resolved "
            f"market has no book to sell into, and the exchange pays settlement "
            f"automatically.")
    if tracked is None:
        parts.append(
            " We could not read the engines' own position tables, so how many "
            "of these an engine was tracking is unknown — each engine's positions "
            "reconcile will book its position as sold on its next pass.")
    elif tracked_sold:
        parts.append(
            f" {tracked_sold} of the sold position(s) were being tracked by an "
            f"engine (main, copy, script or crypto); their positions reconcile "
            f"books an externally-closed position as sold, so nothing is lost "
            f"from their record.")

    logger.info("terminal: flatten (%s) sold %d/%d, %d refused",
                reason, sold, attempted, refused)

    return {
        "ok": refused == 0, "attempted": attempted, "sold": sold,
        "refused": refused, "skipped": empty, "settledSkipped": settled,
        "proceedsUsd": proceeds_usd,
        "engineTrackedSold": None if tracked is None else tracked_sold,
        "reason": reason, "results": results, "message": "".join(parts),
    }


# ────────────────────────────────────────────────────────────────────────
# Sell ONE position — the marketable exit behind the Market page's panel
# ────────────────────────────────────────────────────────────────────────
#
# WHY THIS EXISTS. The order ticket can already sell: type a price, type a
# size, send. But a hand-typed sell at the price you BOUGHT at rests on the
# book and never fills, which is exactly what happened in the first live test
# — "selling is a little wack… I tried to sell at the same price I bought at".
# A limit at or above the bid is not an exit, it is an offer.
#
# So this is the other shape of the same operation: you name a position and a
# slippage tolerance, and it prices a marketable fill-and-kill against the
# BIDS. Three rules keep it honest:
#
#   1. It walks the real book and NEVER sends a size that would leave a
#      resting remainder. If only part of the position fits inside your
#      slippage cap, the preview says how many contracts fit and at what
#      average, and the order is sized to exactly that. When the snapshot has
#      fallen behind the market its depth means nothing, so the whole position
#      goes and the fill-and-kill kills whatever finds no bid.
#   2. The limit IS `bestBid − maxSlippageCents`, on the market's tick, and the
#      best bid is the market's live one. A fill-and-kill takes every bid at or
#      above its limit, best first, so a low limit does not cost a good fill. A
#      limit at the snapshot's own bid did: it sold at 87¢ into an 81¢ market and
#      was killed, twice, with the price falling away (2026-11-06).
#   3. Every order goes through `place()` (live) or `terminal_paper.place()`
#      (paper). Not around them — through them, so the paper gate, the manual
#      ceilings, the confirmation-drift check and the order log all apply.

DEFAULT_SELL_SLIPPAGE_CENTS = 5
MAX_SELL_SLIPPAGE_CENTS = 50
# Past this share of the visible bid depth, "the book will still be there when
# the order lands" stops being a rounding error.
_DEPTH_WARN_FRACTION = 0.25

_SELL_ESTIMATE_NOTE = (
    "Every figure here is an estimate off the book as it is now, not a quote. "
    "The fill you actually get depends on what is still resting when the order "
    "lands.")


def _sell_blank(cid: str, side: str, paper: bool) -> dict:
    return {
        "conditionId": cid, "side": side, "sideLabel": None, "title": None,
        "paper": paper,
        "positionSize": None, "requestedSize": None,
        "size": None, "fillableSize": None,
        "bestBidCents": None, "limitCents": None,
        "estimatedAvgCents": None, "worstFillCents": None,
        "maxSlippageCents": DEFAULT_SELL_SLIPPAGE_CENTS,
        "proceedsUsd": None, "feeUsd": None, "netProceedsUsd": None,
        "avgCostCents": None, "basisSource": None, "costBasisUsd": None,
        "estimatedPnlUsd": None,
        "markCents": None, "settled": False, "resolvedOutcome": None,
        "minOrderSize": None, "tickSize": None,
        "warnings": [], "blockers": [], "note": _SELL_ESTIMATE_NOTE,
    }


async def _position_for(cid: str, side: str, paper: bool) -> Optional[dict]:
    """The one position row for this market + side, from the right book.

    Paper reads the PAPER ledger and live reads the account. They are never
    mixed: a paper number added to a real one is the single worst thing a
    simulator can do.
    """
    if paper:
        import terminal_paper
        cfg = None
        try:
            import service
            cfg = service.STATE.cfg
        except Exception:  # noqa: BLE001 — no service (tests, CLI)
            cfg = None
        pf = await terminal_paper.portfolio(cfg)
    else:
        pf = await portfolio()
    if pf.get("walletReadable") is False:
        # `portfolio()` degrades a failed positions read to an empty list plus a
        # note, which is right for a screen and catastrophic for a seller:
        # "we could not ask" would read as "you hold nothing to sell".
        raise TerminalTradeError(
            str(pf.get("unreconciledNote") or "the exchange did not answer"))
    for p in (pf.get("positions") or []):
        if (str(p.get("conditionId") or "") == cid
                and str(p.get("side") or "") == side):
            return p
    return None


async def sell_position_preview(req: dict) -> dict:
    """Price a marketable exit out of one position. Read-only.

    ``req``: ``conditionId``, ``side``, optional ``size`` (default: the whole
    position), optional ``maxSlippageCents`` (default 5), ``paper``.
    """
    cid = str(req.get("conditionId") or "").strip()
    side = "no" if str(req.get("side") or "yes").lower() == "no" else "yes"
    paper = bool(req.get("paper"))
    out = _sell_blank(cid, side, paper)

    slip = num(req.get("maxSlippageCents"))
    slip = (DEFAULT_SELL_SLIPPAGE_CENTS if slip is None
            else max(0.0, min(float(MAX_SELL_SLIPPAGE_CENTS), float(slip))))
    out["maxSlippageCents"] = slip

    if not cid:
        out["blockers"].append("No market selected.")
        return out

    # ── what do we actually hold? ──────────────────────────────────────
    try:
        pos = await _position_for(cid, side, paper)
    except TerminalTradeError as e:
        out["blockers"].append(
            f"Could not read your {'paper ' if paper else ''}positions — {e} "
            f"So what you hold here is unknown, not nothing. Nothing was "
            f"priced.")
        return out
    except Exception as e:  # noqa: BLE001 — unreadable is not empty
        out["blockers"].append(
            f"Could not read your {'paper ' if paper else ''}positions "
            f"({type(e).__name__}: {e}), so what you hold here is unknown, not "
            f"nothing. Nothing was priced.")
        return out

    held = float((pos or {}).get("size") or 0.0)
    if not pos or held <= 0:
        out["blockers"].append(
            f"You hold no {'paper ' if paper else ''}contracts on this side of "
            f"this market, so there is nothing to sell.")
        return out

    out["positionSize"] = round(held, 2)
    out["sideLabel"] = pos.get("sideLabel")
    out["title"] = pos.get("title")
    out["avgCostCents"] = pos.get("avgCostCents")
    out["basisSource"] = (
        "paper" if paper else pos.get("basisSource"))
    out["costBasisUsd"] = pos.get("costBasisUsd")
    out["markCents"] = pos.get("markCents")

    # ── settled markets pay out by themselves ──────────────────────────
    resolved = pos.get("resolvedOutcome")
    out["resolvedOutcome"] = resolved
    out["settled"] = bool(pos.get("settled")) or resolved is not None
    if out["settled"]:
        won = resolved == side
        out["blockers"].append(
            "This market has resolved. The exchange pays a won position "
            "automatically, and there is no book to sell into on a closed market."
            if won else
            "This market has resolved and this side lost, so there is nothing "
            "to sell — these contracts are worth $0.00.")
        return out

    # ── how much are we selling? ───────────────────────────────────────
    want_raw = num(req.get("size"))
    if want_raw is not None and want_raw <= 0:
        out["blockers"].append("Size must be a positive number of contracts.")
        return out
    requested = held if want_raw is None else float(want_raw)
    out["requestedSize"] = round(requested, 2)
    if requested > held + 1e-9:
        out["warnings"].append(
            f"You asked to sell {requested:g} contracts but hold {held:g}, so "
            f"this is sized to {held:g}. Polymarket has no naked short.")
        requested = held
    # ── market meta: tick, minimum, fee schedule ───────────────────────
    summary = await term.market(cid, include_raw=True)
    if not summary:
        out["blockers"].append(
            "Could not read this market. Refusing to send an order into a "
            "market we cannot price.")
        return out
    raw = summary.get("_raw") or {}
    tick = num(raw.get("orderPriceMinTickSize")) or 0.01
    min_size = num(raw.get("minimumTradeQty"))
    out["tickSize"] = tick
    out["minOrderSize"] = min_size
    if summary.get("status") in ("settled", "closed"):
        out["blockers"].append(
            "This market is closed. Nothing can be traded here.")
        return out
    if term.accepting_orders(raw) is False:
        out["blockers"].append(
            "This market is not accepting orders right now.")
        return out

    # ── how much of it can an order carry? ─────────────────────────────
    # Floored onto the market's step here and said out loud, rather than letting
    # a rounding rule we do not own decide how much of your position leaves.
    want = _floor_qty(requested, min_size)
    if want <= 0:
        out["blockers"].append(
            f"This position is {held:g} contract(s) — less than this market's "
            f"smallest order step of {_qty_step(min_size):g}. It can only be "
            f"closed by letting the market settle.")
        return out
    if want < requested - 1e-9:
        out["warnings"].append(
            f"This market takes steps of {_qty_step(min_size):g} contracts, so "
            f"{round(requested - want, 4):g} will be left behind.")
    out["size"] = float(want)

    # ── walk the bids ──────────────────────────────────────────────────
    bk = await term.book(cid, side)
    if not bk.get("available"):
        why = str(bk.get("note") or "the venue did not answer").strip().rstrip(".")
        out["blockers"].append(
            f"Could not read the order book — {why}. Nothing was priced here "
            f"rather than selling blind.")
        return out
    bids = bk.get("bids") or []
    book_bid = num(bk.get("bestBid"))
    live_bid, live_ask = term.live_touch(summary, side)
    stale = _book_behind(bk, live_bid, live_ask)
    # The market read is uncached and the book can be 30 seconds behind it. A
    # snapshot that has been left behind cannot price an exit, least of all in the
    # fast market where the exit matters.
    lagging = live_bid is not None and (bool(stale) or book_bid is None or not bids)
    best_bid = live_bid if lagging else book_bid
    if best_bid is None or (not lagging and not bids):
        out["blockers"].append(
            "Nobody is bidding on this side right now, so there is nothing to "
            "sell into.")
        return out
    out["bestBidCents"] = best_bid
    if stale:
        out["warnings"].append(stale)

    # The floor is rounded UP onto the tick: rounding it DOWN would quietly hand
    # the order a bigger slippage budget than the one the user picked. A live order
    # also has to stay inside the price collar, or the exit is refused outright.
    reach = slip if paper else min(slip, api.COLLAR_LIVE_CENTS)
    tick_cents = float(tick) * 100.0
    floor_cents = max(tick_cents, round(
        math.ceil((best_bid - reach) / tick_cents - 1e-9) * tick_cents, 4))
    if lagging:
        # No depth to trust, so every contract is priced at the limit: the worst
        # case, and the figure `preview()` reaches for the same order.
        sell_size, avg = float(want), floor_cents
        out["warnings"].append(
            f"The visible depth is out of date, so all {want:g} contracts are "
            f"priced at your {floor_cents:g}¢ limit, the worst case. The order "
            f"takes every bid at or above it, best first, and kills the rest.")
    else:
        fillable, avg = _walk_book(bids, float(want), floor_cents, False)
        if fillable is None:
            out["blockers"].append(
                "There is no book to walk on this side, so the fill cannot be "
                "estimated. Nothing was priced.")
            return out

        sell_size = _floor_qty(float(fillable), min_size)
        if sell_size <= 0:
            out["blockers"].append(
                f"Nothing is bidding within {slip:g}¢ of the best bid "
                f"({best_bid:g}¢), so nothing can be sold at that tolerance. Widen "
                f"the slippage, or wait for a bid.")
            return out
        if sell_size < float(fillable) - 1e-9:
            # Re-price at the size we will actually send: the average of a smaller
            # walk is not the average of a larger one.
            fillable, avg = _walk_book(bids, float(sell_size), floor_cents, False)
        if avg is None:
            out["blockers"].append(
                "The book could not be walked for this size, so there is no fill "
                "estimate. Nothing was priced.")
            return out

    out["fillableSize"] = float(sell_size)
    out["estimatedAvgCents"] = avg
    out["limitCents"] = floor_cents
    # Your limit IS your worst case on a marketable order: nothing can fill
    # below it, and the killed remainder costs nothing.
    out["worstFillCents"] = floor_cents

    if sell_size < want:
        out["warnings"].append(
            f"Only {sell_size:g} of {want:g} contracts fit within {slip:g}¢ of "
            f"the {best_bid:g}¢ bid, so the order is sized to {sell_size:g} — "
            f"nothing is left resting. The other "
            f"{want - sell_size:g} stay in your position.")

    visible = sum(float(l.get("size") or 0.0) for l in bids)
    if not lagging and visible > 0 and sell_size / visible > _DEPTH_WARN_FRACTION:
        out["warnings"].append(
            f"This sell eats {sell_size / visible * 100:.0f}% of the visible "
            f"bid depth. An order that size moves the book, so the real fill "
            f"is likely worse than this estimate.")

    if min_size is not None and sell_size < min_size:
        out["blockers"].append(
            f"{sell_size:g} contract(s) is below this market's minimum order "
            f"of {min_size:g}, so Polymarket would reject the sell. "
            f"This position can only be closed by letting it settle.")
        return out

    # ── money ──────────────────────────────────────────────────────────
    proceeds = round(sell_size * avg / 100.0, 2)
    out["proceedsUsd"] = proceeds
    if proceeds > MAX_MANUAL_USD:
        out["blockers"].append(
            f"${proceeds:,.2f} is over the ${MAX_MANUAL_USD:,.0f} per-order "
            f"manual ceiling. Sell it in pieces.")
        return out
    if sell_size > MAX_MANUAL_CONTRACTS:
        out["blockers"].append(
            f"{sell_size:,.0f} contracts is over the manual ceiling of "
            f"{MAX_MANUAL_CONTRACTS:,}. Sell it in pieces.")
        return out

    fee = taker_fee_usd(avg, sell_size, api._fee_schedule_from_gamma(raw))
    if fee is None:
        out["warnings"].append(
            "We could not determine this market's fee rate, so the proceeds "
            "above EXCLUDE fees and the P&L below is gross of them. Taker fees "
            "on Polymarket run up to ~1.75¢ per share at even odds.")
    else:
        out["feeUsd"] = fee
        out["netProceedsUsd"] = round(proceeds - fee, 2)

    avg_cost = num(out["avgCostCents"])
    if avg_cost is None:
        # Never zero, never break-even. A position with no basis has no P&L,
        # and the screen prints an em dash rather than a confident number.
        out["warnings"].append(
            "There is no cost basis for these contracts, so the profit or loss "
            "on this sell is unknown — not zero.")
    else:
        net = out["netProceedsUsd"]
        out["estimatedPnlUsd"] = round(
            (net if net is not None else proceeds)
            - sell_size * avg_cost / 100.0, 2)

    hrs = summary.get("hoursToClose")
    if hrs is not None and 0 < hrs < 1:
        out["warnings"].append(
            f"This market closes in {hrs * 60:.0f} minutes.")

    return out


def _sell_refused(message: str, pv: Optional[dict] = None) -> dict:
    return {
        "ok": False, "orderId": None, "status": None, "localId": None,
        "sentSize": None, "limitCents": None,
        "filledSize": None, "avgPriceCents": None, "proceedsUsd": None,
        "killedSize": None, "estimatedProceedsUsd": None,
        "message": message, "preview": pv,
    }


async def sell_position(req: dict, *, origin: str = "manual",
                        priced: Optional[dict] = None) -> dict:
    """Send ONE marketable fill-and-kill that closes (part of) a position.

    Same request as the preview, plus ``confirmUsd`` — the proceeds figure the
    user actually read. It is re-derived here and the order is refused if the
    book moved, exactly as `place()` does for the ticket: the confirmation is a
    contract about a number, not a checkbox.

    ``priced`` is a preview the caller took a moment ago and is acting on with no
    human in between (an agent's close). Pricing it again could only refuse an
    exit over a quote that ticked between two reads nobody looked at.
    """
    paper = bool(req.get("paper"))
    pv = priced if priced is not None else await sell_position_preview(req)
    if pv["blockers"]:
        return _sell_refused(pv["blockers"][0], pv)

    confirm = num(req.get("confirmUsd"))
    if confirm is None:
        return _sell_refused(
            "Sell refused: no confirmed proceeds figure was supplied.", pv)
    proceeds = pv["proceedsUsd"]
    if proceeds is not None and abs(proceeds - confirm) > CONFIRM_TOLERANCE_USD:
        return _sell_refused(
            f"Sell refused: the book moved. You confirmed ${confirm:,.2f} but "
            f"this sell would now fetch ${proceeds:,.2f}. Re-check the panel "
            f"and confirm again.", pv)

    size = float(pv["fillableSize"] or 0.0)
    order = {
        "conditionId": pv["conditionId"], "side": pv["side"], "action": "sell",
        "priceCents": pv["limitCents"], "size": size, "orderType": "FAK",
        "confirmUsd": proceeds, "paper": paper,
    }

    # Two distinct calls, never one with a flag. Paper cannot reach the
    # exchange and `place()` refuses outright while paper mode is on.
    if paper:
        import terminal_paper
        cfg = None
        try:
            import service
            cfg = service.STATE.cfg
        except Exception:  # noqa: BLE001
            cfg = None
        res = await terminal_paper.place(order, cfg)
    else:
        res = await place(order, origin=origin)

    filled = num(res.get("filledSize"))
    fill_avg = num(res.get("avgPriceCents"))
    got = (round(filled * fill_avg / 100.0, 2)
           if (filled is not None and fill_avg is not None) else None)
    killed = (round(size - filled, 2)
              if filled is not None else None)

    return {
        "ok": bool(res.get("ok")),
        "orderId": res.get("orderId"),
        "status": res.get("status"),
        "localId": res.get("localId"),
        "sentSize": size,
        "limitCents": pv["limitCents"],
        "filledSize": filled,
        "avgPriceCents": fill_avg,
        # What actually came back, when the venue told us. A live order
        # reconciles against Polymarket's own record a few seconds later, so
        # this is null until then rather than the estimate wearing a disguise.
        "proceedsUsd": got,
        "killedSize": killed,
        "estimatedProceedsUsd": proceeds,
        "message": str(res.get("message") or ""),
        "preview": pv,
    }


# ────────────────────────────────────────────────────────────────────────
# Reconcile — the venue's record of each order is the source of truth
# ────────────────────────────────────────────────────────────────────────
#
# There is no trade history to walk here. A Trade activity carries no order id,
# no side and no intent, so it cannot be tied to an order, or even to YES or NO.
# What IS exact is one order's own record, by id: how much filled, at what
# average, and what commission was collected. So a row in terminal_fills is one
# aggregate per order (`order:<id>`), for the orders this terminal placed.
#
# Anything else a position holds, from an engine or from the Polymarket US app,
# has no row here and never will. `portfolio()` falls back on the venue's average
# for those, and says so.

# Orders read per pass. Over it the result says `complete: False`, and the next
# pass gets to them: newest first, because those are the ones still moving.
_RECONCILE_MAX_ORDERS = 60
_RECONCILE_CONCURRENCY = 6
# How long a FAK fill waits on its market's row before its fee is left unknown.
_FEE_WAIT_HOURS = 24.0


def _order_fee_usd(view: dict, order_type: str, size: float, price_cents: float,
                   raw_market: Optional[dict]) -> Optional[float]:
    """What one order's fills paid in fees, or None when nobody can say.

      1. The venue's own figure, when it reports one above zero.
      2. A FAK or FOK can only ever take, so the fee curve applies. It needs the
         market's coefficient; without the market it is unknown for now.
      3. A GTC with nothing reported is UNKNOWABLE from what the venue returns.
         It may have rested and filled as a maker (0 at the base tier) or crossed
         on arrival (the curve), and the order record does not say which. Not 0
         and not the curve, because either would be a number nobody produced.
         The ledger carries NULL, and the totals say they are gross of it.
    """
    reported = num(view.get("fee_usd"))
    if reported is not None and reported > 0:
        return round(reported, 4)
    if str(order_type).upper() in ("FAK", "FOK") and raw_market is not None:
        return taker_fee_usd(price_cents, size, api._fee_schedule_from_gamma(raw_market))
    return None


async def reconcile_fills(limit: int = 0) -> dict:
    """Read every order we placed that is not final, and write what it filled.

    Idempotent: keyed on the order id. This is the only writer of cost basis in
    the terminal — nothing here trusts what we asked for.

    ``ok: False`` when an order could not be read, or there is no key to read it
    with: the agents' loss stop must not take a stale ledger for a fresh one. A
    404 is not a failure, it is the venue saying it has no such order.
    """
    empty = {"inserted": 0, "matched": 0, "skipped": 0, "corrected": 0,
             "unmapped": 0, "failed": 0, "unwound": 0, "scanned": 0}
    if not auth.credentials_present():
        return {**empty, "ok": False, "complete": False,
                "note": "No API key is saved, so your orders could not be read."}

    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, order_id, condition_id, title, side, action, order_type, "
            "origin, status, created_at, "
            "(julianday('now') - julianday(created_at)) * 24 AS age_hours "
            "FROM terminal_orders "
            "WHERE order_id IS NOT NULL AND reconciled=0 ORDER BY id DESC"
        ).fetchall()]

    # A lost reply loses the id too, so these have nothing to look up. They stay
    # 'unknown': the positions read is the only thing that can settle them.
    no_id = [r for r in rows if str(r["order_id"]).startswith(api.UNKNOWN_ID_PREFIX)]
    pending = [r for r in rows if not str(r["order_id"]).startswith(api.UNKNOWN_ID_PREFIX)]
    cap = min(int(limit), _RECONCILE_MAX_ORDERS) if limit else _RECONCILE_MAX_ORDERS
    batch = pending[:cap]
    complete = len(pending) <= cap

    slots = asyncio.Semaphore(_RECONCILE_CONCURRENCY)

    async def read(row: dict) -> tuple[dict, Optional[dict], Optional[str]]:
        async with slots:
            try:
                return row, (await api.get_order(str(row["order_id"])))["order"], None
            except api.PolymarketAPIError as e:
                if int(getattr(e, "status", 0) or 0) == 404:
                    return row, None, None
                return row, None, str(e)
            except Exception as e:  # noqa: BLE001 — unread is not unfilled
                return row, None, f"{type(e).__name__}: {e}"

    results = await asyncio.gather(*[read(r) for r in batch])
    errors = [why for _r, _v, why in results if why]
    missing = sum(1 for _r, view, why in results if view is None and not why)
    # Filled with no price is a reply we cannot book. Read as a failure, not as 0¢.
    for _r, view, _why in results:
        if view is not None and float(view.get("size_matched") or 0.0) > 0 \
                and not float(view.get("price") or 0.0) > 0:
            errors.append(f"order {view.get('id')} reports a fill with no price")

    # The coefficient, read once per market and only for orders that need the
    # curve. Not term.market(): that subscribes the live feed, which is right for
    # "the user opened this market" and wrong for a pass over old orders.
    raw_by_market: dict[str, dict] = {}
    want_markets = {
        str(row["condition_id"]) for row, view, _ in results
        if view is not None and float(view.get("size_matched") or 0.0) > 0
        and not (num(view.get("fee_usd")) or 0.0) > 0
        and str(row["order_type"]).upper() in ("FAK", "FOK")}
    if want_markets:
        try:
            for m in await term._summaries_for(sorted(want_markets)):
                raw_by_market[str(m["conditionId"])] = m.get("_raw") or {}
        except Exception as e:  # noqa: BLE001 — a miss is an unknown fee, retried
            logger.debug("reconcile: market lookup failed: %s", e)

    inserted = matched = unwound = 0
    fee_pending = 0
    net = _net()
    now = _now_iso()
    with _conn() as conn:
        known = {str(r["trade_id"]) for r in conn.execute(
            "SELECT trade_id FROM terminal_fills WHERE trade_id LIKE 'order:%'")}
        for row, view, why in results:
            if view is None:
                # The venue has no such order. Just after sending that can be
                # timing, so it is asked again; an hour on it is an answer, and
                # asking forever would crowd out orders that can still change. What
                # became of it stays unknown, which is what the row now says.
                if not why and float(row.get("age_hours") or 0.0) >= 1.0:
                    conn.execute(
                        "UPDATE terminal_orders SET status='unknown', reconciled=1, "
                        "reconciled_at=?, error=? WHERE id=?",
                        (now, "Polymarket has no record of this order id.", row["id"]))
                continue
            if float(view.get("size_matched") or 0.0) > 0 \
                    and not float(view.get("price") or 0.0) > 0:
                continue
            oid = str(row["order_id"])
            filled = float(view.get("size_matched") or 0.0)
            venue_status = str(view.get("status") or "")
            final = venue_status in ("matched", "canceled")
            order_type = str(row["order_type"] or "GTC").upper()
            fee: Optional[float] = None
            price = None

            if filled > 0:
                price = round(float(view.get("price") or 0.0) * 100.0, 4)
                fee = _order_fee_usd(view, order_type, filled, price,
                                     raw_by_market.get(str(row["condition_id"])))
                # When it filled. A FAK or FOK fills on arrival, so its creation
                # time is right. A GTC can fill any time after it rests and the
                # order record has no fill time, so the first pass that sees a
                # fill stamps it: approximate, and never rewritten below.
                filled_at = (str(view.get("created_at") or "") or now
                             if order_type in ("FAK", "FOK") else now)
                tid = f"order:{oid}"
                cost = round(filled * price / 100.0, 4)
                conn.execute(
                    """INSERT INTO terminal_fills
                       (trade_id, order_id, condition_id, title, side, action,
                        size, price_cents, fees_usd, cost_usd, origin, network,
                        filled_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trade_id) DO UPDATE SET
                         size = excluded.size,
                         price_cents = excluded.price_cents,
                         fees_usd = excluded.fees_usd,
                         cost_usd = excluded.cost_usd""",
                    # Side and action from our own row, not the reply: we asked
                    # for them, and a reply with no intent would read as YES.
                    (tid, oid, row["condition_id"], row["title"] or "",
                     row["side"], row["action"], filled, price, fee, cost,
                     row["origin"] or "manual", net, filled_at),
                )
                if tid not in known:
                    inserted += 1
            elif f"order:{oid}" in known:
                # The exchange no longer reports a fill it reported before: a busted
                # trade. Left in the ledger it inflates cost and later settles as a
                # phantom win or loss.
                conn.execute("DELETE FROM terminal_fills WHERE trade_id=?", (f"order:{oid}",))
                unwound += 1

            # A FAK fill whose market could not be read this pass stays open, so the
            # next one can fill the fee in rather than freeze it unknown. A parked
            # gateway answers with no rows rather than an error, so the missing row
            # is the test. Bounded, or a market that stopped answering would hold its
            # order open for good. A market that answers with no coefficient is
            # final: its fee is simply unknown.
            waiting_on_fee = (filled > 0 and fee is None and order_type in ("FAK", "FOK")
                              and str(row["condition_id"]) not in raw_by_market
                              and float(row.get("age_hours") or 0.0) < _FEE_WAIT_HOURS)
            if waiting_on_fee:
                fee_pending += 1
            done = final and not waiting_on_fee
            # A killed FAK keeps 'unmatched': the same fact in the order log's word.
            new_status = ("filled" if venue_status == "matched"
                          else row["status"] if row["status"] == "unmatched" and filled <= 0
                          else "canceled" if venue_status == "canceled"
                          else "placed" if venue_status == "live"
                          else row["status"])
            conn.execute(
                """UPDATE terminal_orders
                   SET filled_size=?, avg_fill_price_cents=?, fees_usd=?, cost_usd=?,
                       status=?, reconciled=?, reconciled_at=?
                   WHERE id=?""",
                (filled, price, fee,
                 round(filled * price / 100.0, 4) if price is not None else None,
                 new_status, 1 if done else 0, now if done else None, row["id"]),
            )
            if filled > 0:
                matched += 1

    # An AI agent's order booked 'unknown' (a lost reply) whose id now has fills
    # was placed after all: the agent ledger counts it from here on (loss stop,
    # sell allowance, cancel). DB only; never fails the pass.
    try:
        import mcp_rails
        mcp_rails.promote_from_ledger()
    except Exception as e:  # noqa: BLE001
        logger.debug("reconcile: agent unknown-order promotion failed: %s", e)

    # No silent caps: if the pass stopped early, SAY so. A truncated pass that
    # reports success reads as "everything is reconciled" when it is not.
    notes = []
    if errors:
        notes.append(f"{len(errors)} order(s) could not be read just now ({errors[0]}). "
                     f"They stay unreconciled and the next pass retries them.")
    if not complete:
        notes.append(
            f"Read {len(batch)} of {len(pending)} open orders. The next pass "
            f"continues; the rest stay unreconciled until it reaches them.")
    if fee_pending:
        notes.append(
            f"{fee_pending} fill(s) are waiting on their market's fee rate, which "
            f"could not be read. The next pass fills it in.")
    if missing:
        notes.append(f"Polymarket has no record of {missing} order(s) we thought we placed.")
    if unwound:
        notes.append(f"Polymarket no longer reports a fill on {unwound} order(s), so it was "
                     f"taken back out of your record.")
    if no_id:
        notes.append(
            f"{len(no_id)} order(s) lost their reply and never got an id, so they "
            f"cannot be looked up. Check your positions and resting orders.")
    return {**empty, "ok": not errors, "inserted": inserted, "matched": matched,
            "unwound": unwound, "skipped": missing + len(no_id), "scanned": len(batch),
            "complete": complete and not errors and not fee_pending,
            "note": " ".join(notes) or None}


# ────────────────────────────────────────────────────────────────────────
# Track
# ────────────────────────────────────────────────────────────────────────


def _ledger_basis() -> dict[tuple[str, str], dict]:
    """Average cost per (conditionId, side) from RECONCILED fills only.

    Average cost, not FIFO. A sell reduces size and leaves the average alone —
    which is the convention every trader means when they say "my basis".

    EVERY fill counts, whatever its `origin`. The user's account is the user's
    account: a position an agent opened cost real money out of the same balance
    a hand-placed one did, and filing it as "placed elsewhere" used to be a reason
    it had no basis at all. Origin decides which TRACK RECORD a trade belongs to;
    it never decides whether a position has a cost.
    """
    out: dict[tuple[str, str], dict] = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT condition_id, side, action, size, price_cents, "
            "COALESCE(fees_usd,0) fee, fees_usd raw_fee "
            "FROM terminal_fills ORDER BY filled_at ASC"
        ).fetchall()
    for r in rows:
        key = (str(r["condition_id"]), str(r["side"]))
        st = out.setdefault(key, {
            "size": 0.0, "cost": 0.0, "fees": 0.0, "fees_known": True,
            "realized": 0.0, "bought": 0.0, "closed": 0.0,
        })
        sz = float(r["size"] or 0.0)
        px = float(r["price_cents"] or 0.0)
        if r["raw_fee"] is None:
            st["fees_known"] = False
        else:
            st["fees"] += float(r["fee"])
        if str(r["action"]) == "buy":
            st["size"] += sz
            st["bought"] += sz
            st["cost"] += sz * px / 100.0
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                # `closed` separates "sold nothing" from "sold at exactly
                # break-even": a realised total of 0.00 means something very
                # different in each case, and the screen has to be able to
                # tell them apart.
                st["closed"] += close
                st["realized"] += close * (px / 100.0 - avg)
                st["cost"] -= close * avg
                st["size"] -= close
    return out


# ── resolution cache ────────────────────────────────────────────────────
#
# A RESOLVED market never un-resolves. Its outcome is therefore worth caching
# forever: the fill ledger reaches back over 580 markets, and re-asking the venue
# for every one of them on every Track refresh would be ~30 batched HTTP calls
# a poll, for an answer that cannot change. So a known outcome is written to
# app_kv once and read from there afterwards; only markets we have never seen
# resolved are asked about again, and those are re-asked at most every few
# minutes.
_RESOLVED_KV_PREFIX = "terminal:resolved:"
# How long a "still open / could not tell" answer is allowed to suppress the
# next lookup. Short, because that answer DOES change.
_RESOLUTION_MISS_TTL = 300.0


def _miss_expired(missed_at, now: float) -> bool:
    """May this market be looked up (again)? A market never looked up has no
    miss on record — and that is NOT a miss at monotonic zero. time.monotonic()
    counts from boot, so `.get(c, 0.0)` made every market look like a
    five-minute-old miss on any machine up for less than the TTL: every CI
    runner, and every user's PC for five minutes after boot, resolved nothing."""
    return missed_at is None or (now - float(missed_at)) > _RESOLUTION_MISS_TTL
_resolution_misses: dict[str, float] = {}
_resolution_mem: dict[str, str] = {}
# The richer half of the same cache, used only by the settled RECORD: the
# outcome plus the two things a record has to print and the outcome alone
# cannot supply — the venue's own outcome WORDS ("Up"/"Down", two team names)
# and when the market closed. Same permanence argument: a resolved market's
# labels and close time never change either.
_MARKET_KV_PREFIX = "terminal:market:"
_market_meta_mem: dict[str, dict] = {}
_meta_misses: dict[str, float] = {}
# The in-process half of the cache is SCOPED TO THE DATABASE it was read from.
# A factory reset, a different account or a test's temp DB moves the file under
# us, and a memory cache that outlived the move would answer questions about
# somebody else's markets — which is exactly the class of bug this whole
# module exists to avoid.
_resolution_scope: Optional[str] = None


def _resolution_scope_check() -> None:
    global _resolution_scope
    try:
        import db
        scope = str(db.db_path())
    except Exception:  # noqa: BLE001
        scope = ""
    if scope != _resolution_scope:
        _resolution_scope = scope
        _resolution_mem.clear()
        _resolution_misses.clear()
        _market_meta_mem.clear()
        _meta_misses.clear()


def _resolutions_cached() -> dict[str, str]:
    """Every outcome we have already learned, from memory then app_kv.

    One query, not one per market: the whole point of the cache is that a
    Track refresh over a 580-market ledger costs a single SELECT.
    """
    _resolution_scope_check()
    if _resolution_mem:
        return dict(_resolution_mem)
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT k, v FROM app_kv WHERE k LIKE ?",
                (_RESOLVED_KV_PREFIX + "%",),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — a cache miss is not an error
        logger.debug("resolution cache read failed: %s", e)
        return {}
    for r in rows:
        v = str(r[1] or "")
        if v in ("yes", "no"):
            _resolution_mem[str(r[0])[len(_RESOLVED_KV_PREFIX):]] = v
    return dict(_resolution_mem)


def _resolution_remember(cid: str, outcome: str) -> None:
    """Persist one settled outcome. Written once; never revisited."""
    if outcome not in ("yes", "no") or not cid:
        return
    _resolution_scope_check()
    if _resolution_mem.get(cid) == outcome:
        return
    _resolution_mem[cid] = outcome
    _resolution_misses.pop(cid, None)
    try:
        import db
        with _conn() as conn:
            db.kv_set(conn, _RESOLVED_KV_PREFIX + cid, outcome)
    except Exception as e:  # noqa: BLE001
        logger.debug("resolution cache write failed for %s: %s", cid, e)


async def _resolutions_for(condition_ids) -> dict[str, Optional[str]]:
    """conditionId → ``'yes'`` | ``'no'`` | ``None``.

    ``None`` means WE DO NOT KNOW — the market is still open, or the venue did
    not answer. It never means "lost". Everything that reads this has to carry the
    unknown through rather than settle it at zero.
    """
    ids = [str(c) for c in condition_ids if c]
    if not ids:
        return {}
    known = _resolutions_cached()
    out: dict[str, Optional[str]] = {c: known.get(c) for c in ids}
    now = time.monotonic()
    missing = [c for c in dict.fromkeys(ids)
               if out.get(c) is None
               and _miss_expired(_resolution_misses.get(c), now)]
    for i in range(0, len(missing), 20):
        chunk = missing[i:i + 20]
        try:
            summaries = await term._summaries_for(chunk)
        except Exception as e:  # noqa: BLE001 — a miss degrades to unknown
            logger.debug("resolution lookup failed: %s", e)
            summaries = []
        seen: set[str] = set()
        for m in summaries:
            cid = str(m.get("conditionId") or "")
            seen.add(cid)
            r = m.get("resolvedOutcome")
            if r in ("yes", "no"):
                _resolution_remember(cid, r)
                if cid in out:
                    out[cid] = r
            else:
                _resolution_misses[cid] = now
        for cid in chunk:
            if cid not in seen:
                _resolution_misses[cid] = now
    return out


# ── the richer market cache, for the settled RECORD only ────────────────


def _market_meta_cached() -> dict[str, dict]:
    """Every resolved market we have already described, in one SELECT."""
    _resolution_scope_check()
    if _market_meta_mem:
        return dict(_market_meta_mem)
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT k, v FROM app_kv WHERE k LIKE ?",
                (_MARKET_KV_PREFIX + "%",),
            ).fetchall()
    except Exception as e:  # noqa: BLE001 — a cache miss is not an error
        logger.debug("market meta cache read failed: %s", e)
        return {}
    for r in rows:
        try:
            v = json.loads(str(r[1] or ""))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, dict) and v.get("outcome") in ("yes", "no"):
            _market_meta_mem[str(r[0])[len(_MARKET_KV_PREFIX):]] = v
    return dict(_market_meta_mem)


def _market_meta_remember(cid: str, entry: dict) -> None:
    """Persist one resolved market's outcome, labels and close time."""
    if not cid or entry.get("outcome") not in ("yes", "no"):
        return
    _resolution_scope_check()
    if _market_meta_mem.get(cid) == entry:
        return
    _market_meta_mem[cid] = entry
    _meta_misses.pop(cid, None)
    try:
        import db
        with _conn() as conn:
            db.kv_set(conn, _MARKET_KV_PREFIX + cid, json.dumps(entry))
    except Exception as e:  # noqa: BLE001
        logger.debug("market meta cache write failed for %s: %s", cid, e)


async def _market_meta_for(condition_ids) -> dict[str, dict]:
    """conditionId → ``{outcome, labels, closeTime, title}`` for the record.

    Everything already cached costs one SELECT. Only markets we have never
    seen RESOLVED are asked about, and an unresolved one is re-asked at most
    every few minutes — so the first time the settled table is opened on a
    580-market ledger it pays about fifteen batched market reads, and every
    time after that it pays none.

    A market we cannot describe is simply absent. Never guessed at, and never
    defaulted to "lost".
    """
    ids = list(dict.fromkeys(str(c) for c in condition_ids if c))
    if not ids:
        return {}
    want = set(ids)
    out: dict[str, dict] = {c: dict(v) for c, v in _market_meta_cached().items()
                            if c in want}
    now = time.monotonic()
    missing = [c for c in ids
               if c not in out
               and _miss_expired(_meta_misses.get(c), now)]
    for i in range(0, len(missing), 20):
        chunk = missing[i:i + 20]
        try:
            summaries = await term._summaries_for(chunk)
        except Exception as e:  # noqa: BLE001 — a miss degrades to unknown
            logger.debug("market meta lookup failed: %s", e)
            summaries = []
        seen: set[str] = set()
        for m in summaries:
            cid = str(m.get("conditionId") or "")
            seen.add(cid)
            labels = m.get("outcomes")
            if not (isinstance(labels, (list, tuple)) and len(labels) >= 2):
                labels = None
            entry = {
                "outcome": m.get("resolvedOutcome"),
                "labels": [str(labels[0]), str(labels[1])] if labels else None,
                "closeTime": m.get("closeTime") or None,
                "title": m.get("title") or None,
            }
            if entry["outcome"] in ("yes", "no"):
                _market_meta_remember(cid, entry)
                _resolution_remember(cid, str(entry["outcome"]))
                out[cid] = entry
            else:
                # Still open: worth showing the labels, not worth caching.
                _meta_misses[cid] = now
                out[cid] = entry
        for cid in chunk:
            if cid not in seen:
                _meta_misses[cid] = now
    return out


# A residual smaller than this is a REMAINDER, not a bet. Selling 5 contracts
# back can leave hundredths behind (the venue nets in shares, not lots), and
# treating 0.08 leftover contracts as "a market you won" inflated the record by
# markets the user had already closed by selling. The P&L on those hundredths
# is still counted — it is real money — they just do not score.
#
# Half a contract is the line: on the author's real ledger on the old venue this
# threshold reproduced the exchange's own count of winning markets exactly.
_RECORD_MIN_CONTRACTS = 0.5


async def _ledger_record(basis: dict[tuple[str, str], dict]) -> dict:
    """Settle the WHOLE fill ledger, not just what the positions list still shows.

    THE BUG THIS EXISTS TO FIX. Realised P&L used to be summed over the positions
    still listed. But a settled position leaves that list, and settlement is not
    a trade, so it leaves no "sell" row either. Summing the list therefore dropped
    settled results: a real account with 453 winners reported "105 positions, 105
    losses, realised −$681.13".

    So the ledger is the unit of account. For every (market, side) we ever
    bought, the contracts still outstanding settle at exactly $1 if that side
    won and exactly $0 if it lost — whether or not the position is still listed,
    because a winner has already been PAID. A dead heat, which pays between the
    two, reads as an unknown outcome here rather than as a win or a loss.

    An unresolved market settles nothing; it realises from its sells only. A
    market whose outcome we cannot read is counted as UNKNOWN and left out of
    the total — never folded in as a loss.
    """
    cids = sorted({cid for cid, _ in basis})
    res = await _resolutions_for(cids)

    settled_pnl = 0.0
    any_settled = False
    fees = 0.0
    fees_known = True
    won: set[str] = set()
    lost: set[str] = set()
    unknown: set[str] = set()

    for (cid, side), v in basis.items():
        fees += float(v.get("fees") or 0.0)
        if not v.get("fees_known", True):
            fees_known = False
        size = float(v.get("size") or 0.0)
        if size <= 0.01:
            # Flat: every contract was sold back, so the outcome decided
            # nothing here. Its sells are already in `realized`.
            continue
        outcome = res.get(cid)
        if outcome is None:
            unknown.add(cid)
            continue
        payout = size if outcome == side else 0.0
        settled_pnl += payout - float(v.get("cost") or 0.0)
        any_settled = True
        if size >= _RECORD_MIN_CONTRACTS:
            (won if outcome == side else lost).add(cid)

    # Everything the outcome did not decide: sold back before it resolved, or
    # left with a remainder too small to have been worth claiming.
    closed_out = set(cids) - won - lost - unknown
    return {
        "settledPnlUsd": round(settled_pnl, 2) if any_settled else None,
        "anySettled": any_settled,
        "feesUsd": round(fees, 2) if fees_known else None,
        # A market can appear in BOTH when both sides were held to settlement
        # (a paired crypto entry does exactly that), so these do not sum to
        # the market count and are not presented as if they do.
        "wonMarkets": len(won),
        "lostMarkets": len(lost),
        "unknownMarkets": len(unknown),
        "closedOutMarkets": len(closed_out),
        "ledgerMarkets": len(cids),
        # How many markets were held on BOTH sides into settlement, so the
        # won/lost columns can be presented without pretending they add up.
        "bothSidesMarkets": len(won & lost),
    }


def _record_note(record: dict) -> Optional[str]:
    """One sentence saying what the win/loss counts are and are not."""
    settled = record["wonMarkets"] + record["lostMarkets"]
    if not settled and not record["unknownMarkets"]:
        return None
    parts = []
    if settled:
        parts.append(
            f"{record['wonMarkets']} won and {record['lostMarkets']} lost across "
            f"{record['ledgerMarkets']} market(s) in your fill ledger — counted "
            f"from the fills, so a winner that has already paid out still counts.")
    if record.get("bothSidesMarkets"):
        parts.append(
            f"{record['bothSidesMarkets']} of those are in BOTH columns because "
            f"both sides were held to settlement, so the two do not add up to "
            f"the market count.")
    if record["closedOutMarkets"]:
        parts.append(
            f"{record['closedOutMarkets']} more were closed by selling before "
            f"they resolved, so no outcome decided them.")
    if record["unknownMarkets"]:
        parts.append(
            f"{record['unknownMarkets']} market(s) have contracts outstanding "
            f"whose outcome we could not read — left OUT of both the counts and "
            f"the realised figure rather than assumed lost.")
    return " ".join(parts) or None


def _ledger_meta() -> dict[tuple[str, str], dict]:
    """Per (market, side): the title we saw and when we last traded it.

    The title is the one on the most recent fill — bare columns beside a
    `MAX()` are documented in SQLite to come from the row that produced the
    maximum. `last_at` is what the record sorts by when the venue never told
    us a close time, so a row still lands in the right place rather than at
    the bottom under a fabricated date.
    """
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT condition_id, side, MAX(filled_at) AS last_at, title "
                "FROM terminal_fills GROUP BY condition_id, side"
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        logger.debug("ledger meta read failed: %s", e)
        return {}
    return {(str(r["condition_id"]), str(r["side"])):
            {"lastFillAt": r["last_at"], "title": r["title"]}
            for r in rows}


async def _wallet_sides() -> Optional[set[tuple[str, str]]]:
    """(market, side) the account still holds, or None if we could not read it.

    None is not "holds nothing". A record that quietly claimed every winner
    had paid out because the positions call timed out would be exactly the class
    of lie this module exists to prevent, so the rows say unknown.
    """
    try:
        rows = await api.get_positions(strict=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("settled record: positions read failed: %s", e)
        return None
    out: set[tuple[str, str]] = set()
    for p in rows or []:
        cid = str(p.get("ticker") or "")
        signed = float(p.get("position_fp") or 0.0)
        if cid and abs(signed) > 0.01:
            out.add((cid, "yes" if signed >= 0 else "no"))
    return out


async def settled_record() -> dict:
    """The record: every market the FILL LEDGER has ever settled.

    THE SCREEN THIS EXISTS FOR. "Show settled" used to list the settled positions
    still on the account, and that list is a survivorship filter: a real account
    with 453 wins and 117 losses showed "105 losses" and not one win — every row
    true, the whole table a lie by omission.

    This reads the ledger instead. For every (market, side) we ever bought,
    the contracts still outstanding settle at exactly $1 on the winning
    outcome and exactly $0 on the loser, whether or not the position is still
    listed — a winner has already been PAID.

    Four outcomes, and no fifth:

      ``won``       held the winning side into settlement
      ``lost``      held the losing side into settlement
      ``closedOut`` sold back before it resolved (or left a residual too small
                    to have been worth claiming) — no outcome decided it
      ``unknown``   contracts outstanding, and we could not read the outcome.
                    Never folded in as a loss.

    A market held on BOTH sides into settlement is won on one row and lost on
    the other; both carry ``bothSides`` so the screen can say so instead of
    quietly counting it twice. The counts here are the same sets
    `portfolio()` reports as `wonMarkets` / `lostMarkets`, by construction.
    """
    basis = _ledger_basis()
    meta = _ledger_meta()
    cids = sorted({cid for cid, _ in basis})
    if not cids:
        return {
            "rows": [], "totals": {
                "won": 0, "lost": 0, "bothSides": 0, "closedOut": 0,
                "unknown": 0, "markets": 0, "settledMarkets": 0, "rows": 0,
                "pnlUsd": None, "pnlMissingRows": 0,
                "feesUsd": None, "feesKnown": True, "unknownRows": 0,
            },
            "walletReadable": True, "fetchedAt": _now_iso(),
            "note": ("Your fill ledger is empty, so there is no record to "
                     "show. Press Reconcile fills to import your trade "
                     "history from Polymarket."),
        }

    market = await _market_meta_for(cids)
    wallet = await _wallet_sides()

    rows: list[dict] = []
    won: set[str] = set()
    lost: set[str] = set()
    unknown: set[str] = set()
    fees_total = 0.0
    fees_all_known = True
    pnl_total = 0.0
    pnl_rows = 0
    unknown_rows = 0

    for (cid, side), v in basis.items():
        m = meta.get((cid, side)) or {}
        mk = market.get(cid) or {}
        labels = mk.get("labels")
        res = mk.get("outcome") if mk.get("outcome") in ("yes", "no") else None

        size = float(v.get("size") or 0.0)
        cost = float(v.get("cost") or 0.0)
        realized = float(v.get("realized") or 0.0)
        fees = float(v.get("fees") or 0.0)
        fees_known = bool(v.get("fees_known", True))
        fees_total += fees
        if not fees_known:
            fees_all_known = False

        flat = size <= 0.01
        payout: Optional[float]
        gross: Optional[float]
        if flat:
            # Every contract was sold back. The outcome decided nothing here,
            # so there is no payout — only what the sells banked.
            outcome, payout = "closedOut", None
            gross = realized
        elif res is None:
            outcome, payout, gross = "unknown", None, None
            unknown.add(cid)
            unknown_rows += 1
        else:
            payout = round(size if res == side else 0.0, 2)
            gross = realized + payout - cost
            if size >= _RECORD_MIN_CONTRACTS:
                outcome = "won" if res == side else "lost"
                (won if res == side else lost).add(cid)
            else:
                # A residual of hundredths is a remainder, not a bet: it does
                # not score. Its money still counts.
                outcome = "closedOut"

        pnl = (None if (gross is None or not fees_known)
               else round(gross - fees, 2))
        if pnl is not None:
            pnl_total += pnl
            pnl_rows += 1

        rows.append({
            "conditionId": cid,
            "title": m.get("title") or mk.get("title") or None,
            "side": side,
            # The venue's own word for the side held — "Up", "Over", a team
            # name. null falls back to YES/NO in the UI, never to a guess.
            "sideLabel": (str(labels[0 if side == "yes" else 1])
                          if labels else None),
            # Contracts still outstanding when the market settled. Zero means
            # it was closed by selling.
            "contracts": round(size, 2),
            "avgCostCents": (round(cost / size * 100.0, 3)
                             if size > 0.01 else None),
            "costUsd": round(cost, 2),
            # $1 a contract on the winning outcome, $0 on the loser. null when
            # nothing settled here.
            "payoutUsd": payout,
            "pnlUsd": pnl,
            "feesUsd": round(fees, 2) if fees_known else None,
            "feesKnown": fees_known,
            # What the sells alone banked, so a closed-out row can say where
            # its number came from.
            "soldPnlUsd": round(realized, 2),
            "outcome": outcome,
            "resolvedOutcome": res,
            "resolvedOutcomeLabel": (str(labels[0 if res == "yes" else 1])
                                     if (labels and res) else None),
            "settledAt": mk.get("closeTime") or None,
            # Still listed on the account. null = we could not read the
            # positions, which is not the same as "no".
            "inWallet": (None if wallet is None else (cid, side) in wallet),
            "bothSides": False,
            "_sort": str(mk.get("closeTime") or m.get("lastFillAt") or ""),
        })

    both = won & lost
    for r in rows:
        r["bothSides"] = r["conditionId"] in both
    # Same arithmetic as `_ledger_record`, so the two can never disagree.
    closed_out = set(cids) - won - lost - unknown

    # Newest first, by the market's own close time where the venue gave us
    # one and the last fill otherwise — never by a date we invented.
    rows.sort(key=lambda r: (r["_sort"], r["conditionId"]), reverse=True)
    for r in rows:
        r.pop("_sort", None)

    parts: list[str] = [
        f"{len(won)} won and {len(lost)} lost across {len(cids)} market(s) in "
        f"your fill ledger. This is the RECORD, not your open positions: a winner "
        f"that has already paid out still counts here, and it left the Positions "
        f"tab when it paid."]
    if both:
        parts.append(
            f"{len(both)} of those are in BOTH columns because both sides were "
            f"held to settlement, so the two do not add up to the market count "
            f"— each is shown once per side and labelled.")
    if closed_out:
        parts.append(
            f"{len(closed_out)} more were closed by selling before they "
            f"resolved, so no outcome decided them.")
    if unknown:
        parts.append(
            f"{len(unknown)} market(s) have contracts outstanding whose "
            f"outcome we could not read — left OUT of the counts and the "
            f"total rather than assumed lost.")

    return {
        "rows": rows,
        "totals": {
            "won": len(won), "lost": len(lost), "bothSides": len(both),
            "closedOut": len(closed_out), "unknown": len(unknown),
            "markets": len(cids),
            # What the toggle counts: markets that are DONE, deduplicated.
            "settledMarkets": len(won | lost | closed_out),
            "rows": len(rows),
            # Net of the fees the ledger knows, over the rows that have a
            # number. null when not one row does.
            "pnlUsd": round(pnl_total, 2) if pnl_rows else None,
            # How many rows that total could not include.
            "pnlMissingRows": len(rows) - pnl_rows,
            "feesUsd": round(fees_total, 2) if fees_all_known else None,
            "feesKnown": fees_all_known,
            "unknownRows": unknown_rows,
        },
        "walletReadable": wallet is not None,
        "fetchedAt": _now_iso(),
        "note": " ".join(parts),
    }


async def portfolio() -> dict:
    """Positions with a cost basis, and a stamp saying where each one came from.

    TWO sources of basis, in this order:

      1. OUR FILL LEDGER — every reconciled fill of an order placed from this
         terminal. Average cost, fee-exclusive, computed here.
      2. THE VENUE'S OWN AVERAGE — the cost on the signed positions read, used
         when our ledger cannot cover the size (an engine's position, one opened
         in the Polymarket US app, or a wiped ledger). Stamped ``data-api``, the
         renderer's name for "the exchange's record of this account", so the
         screen can say so.

    Only when NEITHER can answer is the basis unknown — and then it stays
    unknown: never zero, never break-even, and the row is left out of the
    totals and counted in a warning strip.

    Every outcome question — which side you hold, which side won — is decided
    by the OUTCOME INDEX (0 = the market's first outcome), never by the label
    text, so an "Up or Down" market and a named-team market are read exactly
    as reliably as a Yes/No one.
    """
    try:
        raw_positions = await api.get_positions()
    except Exception as e:  # noqa: BLE001
        return {
            "positions": [], "totalValueUsd": None, "totalCostUsd": None,
            "totalUnrealizedPnlUsd": None, "realizedPnlUsd": None,
            "realizedFeesUsd": None, "realizedPnlNetUsd": None,
            "settledPnlUsd": None, "venueCashPnlUsd": None,
            "walletPnlUsd": None,
            "unreconciledCount": 0,
            # An empty `positions` here means "we could not ask", NOT "you
            # hold nothing" — and the two are opposite instructions to
            # anything downstream that is about to sell. Flagged rather than
            # left for a caller to infer from a note's wording.
            "walletReadable": False,
            "unreconciledNote": f"Could not read your positions: {e}",
            "redeemableCount": 0, "redeemableValueUsd": None,
            "redeemableNote": None, "settledWorthlessCount": 0,
            "venueBasisCount": 0,
            "wonMarkets": None, "lostMarkets": None,
            "unknownResolutionMarkets": None, "closedOutMarkets": None,
            "ledgerMarkets": None, "recordNote": None,
            "cashUsd": None, "fetchedAt": _now_iso(),
            "costBasisConvention": "average",
        }

    basis = _ledger_basis()

    # Resolve every held market ONCE, batched. Without this every position
    # rendered as permanently "open": a settled market showed a live mark and no
    # outcome, which is the single most misleading thing this screen could do.
    # Prediction markets settle to exactly 0 or 1 (a dead heat aside), so once we
    # know the outcome the P&L is EXACT rather than a mark.
    market_by_id: dict[str, dict] = {}
    try:
        for m in await term._summaries_for(
                [str(p.get("ticker") or "") for p in raw_positions]):
            market_by_id[m["conditionId"]] = m
            # Seed the long-lived resolution cache from the summaries we
            # already paid for, so the ledger pass never re-asks for these.
            if m.get("resolvedOutcome") in ("yes", "no"):
                _resolution_remember(str(m["conditionId"]), str(m["resolvedOutcome"]))
    except Exception as e:  # noqa: BLE001 — a miss degrades to "unknown"
        logger.debug("terminal portfolio market lookup failed: %s", e)

    positions: list[dict] = []
    unreconciled = 0
    venue_basis_rows = 0
    worthless_settled = 0
    # Settlement of what is still listed. Kept for the side-by-side against
    # Polymarket's own `cashPnl`, which has the same scope — it is NOT the realised
    # total, because the list cannot see a winner that has already paid out.
    wallet_settled_total = 0.0
    any_wallet_settled = False
    # Settled contracts our ledger does NOT cover, valued off the venue's own
    # average. Zero whenever the ledger covers the holding; this exists so a
    # position with no fills in our ledger still settles.
    venue_extra_settled = 0.0
    any_venue_extra = False
    venue_pnl_total = 0.0
    any_venue_pnl = False
    tot_val = tot_cost = tot_pnl = 0.0
    any_reconciled = False

    for p in raw_positions:
        cid = str(p.get("ticker") or "")
        if not cid:
            continue
        signed = float(p.get("position_fp") or 0.0)
        # `position_fp` is signed off the OUTCOME INDEX (0 → +, 1 → −), which
        # is the only outcome identity that means the same thing on a Yes/No,
        # an Up/Down and a named-team market.
        side = "yes" if signed >= 0 else "no"
        size = abs(signed)
        mark = cents(p.get("cur_price"))
        b = basis.get((cid, side))

        # The venue's own row for this position, when we have it.
        vraw = p.get("_raw") if isinstance(p.get("_raw"), dict) else {}
        v_avg = num(vraw.get("avgPrice"))            # 0..1
        v_initial = num(vraw.get("initialValue"))    # USD, fee-exclusive
        v_cash_pnl = num(vraw.get("cashPnl"))
        v_realized = num(vraw.get("realizedPnl"))
        m = market_by_id.get(cid)
        status = (m or {}).get("status") or "unknown"
        resolved = (m or {}).get("resolvedOutcome")
        close_time = (m or {}).get("closeTime")
        # `status`, not just the outcome: a dead heat is settled with neither
        # side winning outright, and pays what the summary's probability says.
        settled = resolved is not None or status == "settled"

        outcome_labels = (m or {}).get("outcomes")
        if not (isinstance(outcome_labels, (list, tuple))
                and len(outcome_labels) >= 2):
            outcome_labels = None
        # The market's word for the side actually held — "Up", "Over", a team
        # name. Shown as-is, because "YES" on an Up-or-Down market is a
        # translation the user never asked for. A position row carries no label of
        # its own, only Yes or No by the sign of its size.
        side_label = (str(outcome_labels[0 if side == "yes" else 1]) if outcome_labels
                      else str(vraw.get("outcome") or "").strip() or None)
        resolved_label = None
        if resolved is not None and outcome_labels:
            resolved_label = str(outcome_labels[0 if resolved == "yes" else 1])

        if settled:
            # The outcome is known, so the mark is not an estimate: this side
            # is worth exactly 100¢ or exactly 0¢. `resolved` and `side` are
            # both index-derived, so this comparison is vocabulary-proof.
            if resolved is not None:
                mark = 100.0 if resolved == side else 0.0
            else:
                paid_yes = num((m or {}).get("probability"))
                mark = (None if paid_yes is None
                        else paid_yes if side == "yes" else round(100.0 - paid_yes, 2))
            status = "settled"
        elif not mark:
            # A position row with no cash value reads as a price of 0. On a market
            # still open that is no figure at all, not a position worth nothing: the
            # market's own price marks it, and without one the mark is unknown.
            quoted = num((m or {}).get("probability"))
            mark = (None if quoted is None
                    else quoted if side == "yes" else round(100.0 - quoted, 2))

        unknowns: list[dict] = []
        sources: dict[str, dict] = {
            "probability": {"source": "data-api", "observedAt": _now_iso(), "ageSec": 0.0},
        }

        avg_cents = None
        cost_usd = None
        basis_source = None
        pnl = None          # UNREALISED — open positions only
        settle_pnl = None   # REALISED at settlement — decided, final
        ledger_covered = round(min(size, b["size"]), 2) if b else 0.0
        unrec_size = None

        if b and b["size"] > 0.01 and b["size"] >= size - 0.01:
            # Our own ledger covers the whole holding. Best case: the number
            # was computed here, from the venue's own fills, and we can say so.
            avg_cents = round(b["cost"] / b["size"] * 100.0, 3)
            cost_usd = round(size * avg_cents / 100.0, 2)
            basis_source = "ledger"
            sources["avgCost"] = {"source": "ledger", "observedAt": _now_iso(),
                                  "ageSec": 0.0}
        elif v_avg:
            # (Not 0: a row with no cost figure reads as an average of 0, and a free
            # position would report its whole value as profit.)
            # The ledger cannot cover it — an engine's position, one opened in the
            # Polymarket US app, or one whose fills a wipe removed. The venue keeps
            # its own cost for this exact account, so USE IT and stamp where it
            # came from, rather than showing a position with no basis at all.
            avg_cents = round(v_avg * 100.0, 3)
            cost_usd = (round(v_initial, 2) if v_initial is not None
                        else round(size * avg_cents / 100.0, 2))
            basis_source = "data-api"
            venue_basis_rows += 1
            sources["avgCost"] = {"source": "data-api", "observedAt": _now_iso(),
                                  "ageSec": 0.0}
            if b and b["size"] > 0.01:
                unrec_size = round(size - ledger_covered, 2)
                unknowns.append({
                    "field": "avgCostCents",
                    "reason": (
                        f"Your fill ledger only covers {ledger_covered:g} of "
                        f"{size:g} contracts here, so the average shown is "
                        f"Polymarket's own for this account, not ours."),
                })
        else:
            unrec_size = size
            unknowns.append({
                "field": "avgCostCents",
                "reason": (
                    "Neither your fill ledger nor Polymarket's own position "
                    "record has an average price for these contracts, so there "
                    "is nothing to measure the P&L against. Not zero: unknown."),
            })

        if avg_cents is not None and mark is not None:
            # A SETTLED market has no unrealised anything — the outcome is
            # decided and the number is final. Booking it as "unrealised" left an
            # account holding 105 resolved losers permanently showing a -$229
            # UNREALISED loss on positions that could never move again, while
            # "realised" showed -$17 because the ledger only counts SELLS.
            delta = round(size * (mark - avg_cents) / 100.0, 2)
            if settled:
                settle_pnl = delta
            else:
                pnl = delta

        if mark is None:
            unknowns.append({
                "field": "markCents",
                "reason": (
                    "No current price for this position, and no posted outcome "
                    "to settle it against."
                    if status in ("closed", "unknown") else
                    "Polymarket returned no current price for this position."),
            })
        elif settled:
            # Say WHERE an exact number came from, same as any other field.
            sources["mid"] = {"source": "venue", "observedAt": _now_iso(), "ageSec": 0.0}

        value = round(size * mark / 100.0, 2) if mark is not None else None
        if value is None:
            unknowns.append({
                "field": "valueUsd",
                "reason": "Value needs a current price, and there is none.",
            })

        # Nothing on this venue needs claiming: settlement pays automatically.
        # What can still be listed is a loser, worth nothing and waiting to clear.
        if settled and value is not None and value <= 0.005:
            worthless_settled += 1

        row_unreconciled = basis_source is None
        if not row_unreconciled:
            any_reconciled = True
            if value is not None:
                tot_val += value
            if cost_usd is not None:
                tot_cost += cost_usd
            if pnl is not None:
                tot_pnl += pnl
            if settle_pnl is not None:
                wallet_settled_total += settle_pnl
                any_wallet_settled = True
                # The ledger pass below settles what OUR fills cover. Anything
                # this position holds beyond that is settled here instead, off
                # the venue's average, so the two together cover the holding
                # exactly once.
                uncovered = size - ledger_covered
                if uncovered > 0.01 and avg_cents is not None and mark is not None:
                    venue_extra_settled += (
                        uncovered * (mark - avg_cents) / 100.0)
                    any_venue_extra = True
        else:
            unreconciled += 1

        if v_cash_pnl is not None:
            venue_pnl_total += v_cash_pnl
            any_venue_pnl = True

        positions.append({
            "conditionId": cid, "venue": "polymarket",
            "title": p.get("title") or (m or {}).get("title") or None,
            "slug": (m or {}).get("slug"),
            "side": side, "size": round(size, 2),
            # The venue's own word for the side held — "Up", "Over", a team
            # name. null falls back to YES/NO in the UI.
            "sideLabel": side_label,
            "avgCostCents": avg_cents, "costBasisUsd": cost_usd,
            # Where the average above came from, so the screen never presents
            # somebody else's arithmetic as ours.
            "basisSource": basis_source,
            "markCents": mark, "valueUsd": value,
            "unrealizedPnlUsd": pnl,
            # Anything this row has already banked: earlier partial sells from
            # the ledger, plus its settlement if the market has resolved.
            "realizedPnlUsd": (
                None if (b is None and settle_pnl is None)
                else round((b["realized"] if b else 0.0) + (settle_pnl or 0.0), 2)),
            "settledPnlUsd": settle_pnl,
            # Polymarket's OWN P&L for this position, carried so the two can be
            # reconciled on screen instead of argued about.
            "venueCashPnlUsd": v_cash_pnl,
            "venueRealizedPnlUsd": v_realized,
            "status": status,
            "resolvedOutcome": resolved,
            "resolvedOutcomeLabel": resolved_label,
            "outcomeLabels": ([str(outcome_labels[0]), str(outcome_labels[1])]
                              if outcome_labels else None),
            "closeTime": close_time,
            # Always False. Settlement pays out automatically here, and the key
            # stays because the renderer reads it.
            "redeemable": False,
            "settled": settled,
            "unreconciled": row_unreconciled,
            "unreconciledSize": unrec_size,
            "sources": sources,
            "unknowns": unknowns,
        })

    positions.sort(key=lambda r: -(r.get("valueUsd") or 0.0))

    # ── realised ──────────────────────────────────────────────────────────
    # Realised = what the ledger banked on SELLS, plus what EVERY settled
    # market in the ledger decided — not just the ones still listed.
    #
    # This is the whole repair. A winner that paid out is gone from the list and
    # has no sell row, so both of the old terms missed it; what was left was the
    # losers, which is why an account of 105 resolved losers reported "realised
    # −$681.13" while it had actually won 453 markets.
    record = await _ledger_record(basis)
    ledger_realized = sum(v["realized"] for v in basis.values()) if basis else None
    settled_total = record["settledPnlUsd"]
    if any_venue_extra:
        settled_total = round((settled_total or 0.0) + venue_extra_settled, 2)
    any_settled_pnl = record["anySettled"] or any_venue_extra
    any_sells = any(v.get("closed", 0.0) > 0 for v in basis.values())
    if ledger_realized is None:
        realized = None
    elif record["unknownMarkets"] and not (any_sells or any_settled_pnl):
        # Everything we hold is a market whose outcome we could not read.
        # $0.00 would be a claim that nothing has happened; it is not that we
        # know the answer is zero, it is that we do not know the answer.
        realized = None
    else:
        realized = round((ledger_realized or 0.0) + (settled_total or 0.0), 2)

    # Fees are charged on the way in AND the way out, and neither the venue's
    # average nor our basis includes them — so `realized` above is the same gross
    # figure Polymarket's own P&L quotes, and the number that actually moved
    # through the account is `realized` minus these.
    fees_total = record["feesUsd"]
    realized_net = (None if (realized is None or fees_total is None)
                    else round(realized - fees_total, 2))

    # get_balance reports CENTS ({'balance', 'portfolio_value'}), and it RAISES
    # rather than returning 0 on a read failure — precisely so a transient blip
    # cannot flash a funded account to $0. Keep that: an unreadable balance is
    # None here, and the UI prints an em dash.
    cash = None
    try:
        bal = await api.get_balance()
        cash_cents = num((bal or {}).get("balance"))
        cash = round(cash_cents / 100.0, 2) if cash_cents is not None else None
    except Exception:
        cash = None

    parts: list[str] = []
    if unreconciled:
        parts.append(
            f"{unreconciled} position(s) have no cost basis at all — neither "
            f"your fill ledger nor Polymarket's own position record has an "
            f"average price for them. They are shown, but deliberately left "
            f"OUT of the totals rather than counted at a guessed price.")
    if venue_basis_rows:
        parts.append(
            f"{venue_basis_rows} position(s) are priced off Polymarket's own "
            f"average for this account rather than your fill ledger — they were "
            f"not placed from this screen, or their fills were cleared. Those "
            f"figures are the venue's, and are marked as such.")
    note = " ".join(parts) or None

    return {
        "positions": positions,
        # The exchange answered. An empty list above therefore really does mean
        # "you hold nothing" — see the failure branch at the top.
        "walletReadable": True,
        "totalValueUsd": round(tot_val, 2) if any_reconciled else None,
        "totalCostUsd": round(tot_cost, 2) if any_reconciled else None,
        # Open positions only. A settled row cannot be unrealised.
        "totalUnrealizedPnlUsd": round(tot_pnl, 2) if any_reconciled else None,
        "realizedPnlUsd": realized,
        # Trading fees across the whole ledger, and what is left after them.
        # `realizedPnlUsd` is gross so it stays comparable with the venue's own
        # figures; this is the number that reached the bank balance.
        "realizedFeesUsd": fees_total,
        "realizedPnlNetUsd": realized_net,
        # The settlement half of `realizedPnlUsd`, split out so the screen can
        # say how much of your record came from settlement.
        "settledPnlUsd": settled_total if any_settled_pnl else None,
        # Polymarket's OWN total P&L across the same positions. Shown next to
        # ours so the two can be reconciled rather than trusted: they should
        # agree, and where they do not the user can see it instead of guessing
        # which screen is lying.
        "venueCashPnlUsd": round(venue_pnl_total, 2) if any_venue_pnl else None,
        # OUR figure for the same still-listed scope the venue's `cashPnl`
        # covers. This — not `settledPnlUsd` — is what that comparison must
        # use, now that `settledPnlUsd` spans the whole ledger.
        "walletPnlUsd": (round(wallet_settled_total + tot_pnl, 2)
                         if (any_wallet_settled or any_reconciled) else None),
        # ── the record, from the LEDGER, not the positions list ────────────
        # A market counts as won when we held the winning side into
        # settlement, whether or not it is still listed. A market can
        # land in both columns when both sides were held to settlement, so
        # these deliberately do not sum to `ledgerMarkets`.
        "wonMarkets": record["wonMarkets"],
        "lostMarkets": record["lostMarkets"],
        "unknownResolutionMarkets": record["unknownMarkets"],
        "bothSidesMarkets": record["bothSidesMarkets"],
        "closedOutMarkets": record["closedOutMarkets"],
        "ledgerMarkets": record["ledgerMarkets"],
        "recordNote": _record_note(record),
        "unreconciledCount": unreconciled,
        "unreconciledNote": note,
        # How many rows are priced off the venue's average rather than ours.
        "venueBasisCount": venue_basis_rows,
        # Resolved, worthless, and still listed. Nothing needs claiming on this
        # venue; the three redeemable keys stay because the renderer reads them.
        "settledWorthlessCount": worthless_settled,
        "redeemableCount": 0,
        "redeemableValueUsd": 0.0,
        "redeemableNote": None,
        "cashUsd": cash,
        "fetchedAt": _now_iso(),
        "costBasisConvention": "average",
    }


def fills(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 500))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM terminal_fills ORDER BY filled_at DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": str(r["trade_id"]),
            "conditionId": str(r["condition_id"]),
            "title": r["title"] or None,
            "side": str(r["side"]), "action": str(r["action"]),
            "size": float(r["size"]),
            "priceCents": float(r["price_cents"]),
            "feeUsd": (float(r["fees_usd"]) if r["fees_usd"] is not None else None),
            "costUsd": (float(r["cost_usd"]) if r["cost_usd"] is not None else None),
            "ts": str(r["filled_at"]),
            "reconciled": True,
            "origin": str(r["origin"] or "unknown"),
            "source": "ledger",
        })
    return {"fills": out, "fetchedAt": _now_iso(),
            "note": None if out else (
                "No fills recorded yet. This ledger is written from "
                "Polymarket's own trade history, not from what we requested.")}


def order_log(limit: int = 100) -> dict:
    """Every manual order attempt, including the ones that failed.

    Failures are kept on purpose: an order that errored after the venue may
    have accepted it is exactly the row you need when a position appears that
    you do not remember opening.
    """
    limit = max(1, min(int(limit or 100), 500))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM terminal_orders ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return {
        "orders": [{
            "localId": int(r["id"]),
            "conditionId": str(r["condition_id"]),
            "title": r["title"] or None,
            "side": str(r["side"]), "action": str(r["action"]),
            "orderType": str(r["order_type"]),
            "requestedSize": float(r["requested_size"]),
            "requestedPriceCents": float(r["requested_price_cents"]),
            "sentSize": (float(r["sent_size"]) if r["sent_size"] is not None else None),
            "sentPriceCents": (float(r["sent_price_cents"])
                               if r["sent_price_cents"] is not None else None),
            "orderId": r["order_id"] or None,
            "status": str(r["status"]),
            "error": r["error"] or None,
            "filledSize": (float(r["filled_size"]) if r["filled_size"] is not None else None),
            "avgFillPriceCents": (float(r["avg_fill_price_cents"])
                                  if r["avg_fill_price_cents"] is not None else None),
            "feesUsd": (float(r["fees_usd"]) if r["fees_usd"] is not None else None),
            "reconciled": bool(r["reconciled"]),
            "origin": str(r["origin"]),
            "standingId": r["standing_id"] or None,
            "createdAt": str(r["created_at"]),
        } for r in rows],
        "fetchedAt": _now_iso(),
    }


# ────────────────────────────────────────────────────────────────────────
# Standing instructions
#
# The one category of action that fires without a click — and it is still
# user-directed, because the user WROTE IT DOWN. That is the entire point of
# a stop loss, so it is not a contradiction. Two rules keep it honest:
#   * it is gated identically to a manual click (same preview, same blockers,
#     same live-trading switch), and
#   * every pending instruction is listed on one page and cancellable there.
#
# Triggers are PROBABILITY POINTS, not percentages. On a bounded 0..100
# quantity a "10% stop" is ambiguous and a "10 point stop" is not.
# ────────────────────────────────────────────────────────────────────────

_STANDING_KEY = "terminal:standing"
_STANDING_MAX = 100
_VALID_KINDS = ("stopLoss", "takeProfit", "alertAbove", "alertBelow")
_STOP_KINDS = ("stopLoss", "takeProfit")

# Durable record of what fired stops actually SOLD, keyed by instruction id.
# The instruction list is bounded and the user can delete a fired entry, but
# the shares a stop sold must stay spoken for afterwards: the agent rails
# (`mcp_rails.live_net`) subtract them from what an agent may still sell, and
# losing them let an agent sell shares a stop had already sold — drawing on
# the user's own position. One compact row per stop that sold anything.
_STANDING_SOLD_KEY = "terminal:standing:sold"


def standing_list() -> list[dict]:
    entries = term._kv_json(_STANDING_KEY, [])
    return entries if isinstance(entries, list) else []


def _standing_retired(e: dict) -> bool:
    """Done for good: fired, failed out, or found nothing to sell. A stop the
    user (or a blocked trigger) merely disarmed is NOT retired — it can be
    re-armed, so it is never pruned."""
    return bool(isinstance(e, dict) and e.get("firedAt"))


def _standing_active_count(entries: list[dict]) -> int:
    return sum(1 for e in entries if isinstance(e, dict) and not _standing_retired(e))


def standing_remaining(e: dict) -> float:
    """Contracts a stop/take-profit still has to sell: its size less what
    earlier partial fills already sold."""
    size = float(num(e.get("size")) or 0.0)
    sold = float(num(e.get("soldSize")) or 0.0)
    return max(0.0, round(size - sold, 6))


def standing_sold_records() -> dict[str, dict]:
    recs = term._kv_json(_STANDING_SOLD_KEY, {})
    return recs if isinstance(recs, dict) else {}


def _legacy_sold(instruction_id: str) -> float:
    """What a stop that fired BEFORE sold sizes were recorded sold, by the rule
    the agent rails used for it: every order it sent that was not a known
    zero-fill. Errs high, which is the safe direction for a sell allowance."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(sent_size, requested_size)), 0) "
                "FROM terminal_orders WHERE standing_id=? "
                "AND status NOT IN ('unmatched', 'rejected')",
                (str(instruction_id),)).fetchone()
        return float(row[0] or 0.0)
    except Exception as ex:  # noqa: BLE001
        logger.debug("standing: legacy sold read for %s failed: %s", instruction_id, ex)
        return 0.0


def _record_stop_sale(e: dict, sold: float, order_id: Optional[str],
                      confirmed: bool, *, final: bool) -> None:
    """Add `sold` contracts to the durable record of instruction `e`."""
    if sold <= 0:
        return
    recs = standing_sold_records()
    iid = str(e.get("id") or "")
    r = recs.get(iid) or {
        "id": iid, "conditionId": e.get("conditionId"), "side": e.get("side"),
        "kind": e.get("kind"), "origin": e.get("origin") or "manual",
        "agentId": e.get("agentId"), "soldSize": 0.0, "orderIds": [],
        "confirmed": True, "firedAt": None,
    }
    r["soldSize"] = round(float(r.get("soldSize") or 0.0) + float(sold), 6)
    if order_id and order_id not in r["orderIds"]:
        r["orderIds"].append(order_id)
    r["confirmed"] = bool(r.get("confirmed", True)) and bool(confirmed)
    if e.get("agentId") and not r.get("agentId"):
        r["agentId"] = e.get("agentId")
    r["updatedAt"] = _now_iso()
    if final:
        r["firedAt"] = r["updatedAt"]
    recs[iid] = r
    term._kv_put(_STANDING_SOLD_KEY, recs)


def _archive_before_drop(e: dict) -> None:
    """A fired stop is leaving the list (pruned or deleted). Make sure what it
    sold survives it. Entries that fired before sold sizes were recorded have
    no record yet, so one is written from the order log."""
    if not isinstance(e, dict) or e.get("kind") not in _STOP_KINDS:
        return
    if not e.get("firedAt") and not float(num(e.get("soldSize")) or 0.0):
        return
    iid = str(e.get("id") or "")
    if not iid or iid in standing_sold_records():
        return
    if e.get("retiredReason") is None and e.get("soldSize") is None:
        sold, confirmed = _legacy_sold(iid), False
    else:
        sold, confirmed = float(num(e.get("soldSize")) or 0.0), True
    _record_stop_sale(e, sold, None, confirmed, final=bool(e.get("firedAt")))


def standing_sold_shares(cid: str, side: str, *, agent_id: Optional[str] = None,
                         default_agent_id: Optional[str] = None,
                         origin: Optional[str] = "ai") -> float:
    """Contracts of (cid, side) that standing stops/take-profits have SOLD.

    Durable: reads the sold record, which outlives the bounded instruction
    list and a user deleting a fired entry. A stop that fired before sold
    sizes were recorded and is still listed falls back to its order log.
    `agent_id` filters by owner; a record with no agentId belongs to
    `default_agent_id`. `origin=None` counts every stop."""
    cid, side = str(cid), str(side)

    def _match(x: dict) -> bool:
        if str(x.get("conditionId") or "") != cid or str(x.get("side") or "") != side:
            return False
        if origin is not None and (x.get("origin") or "manual") != origin:
            return False
        if agent_id is not None and (x.get("agentId") or default_agent_id) != agent_id:
            return False
        return True

    total = 0.0
    recs = standing_sold_records()
    for r in recs.values():
        if isinstance(r, dict) and _match(r):
            total += float(r.get("soldSize") or 0.0)
    for e in standing_list():
        if (isinstance(e, dict) and e.get("kind") in _STOP_KINDS
                and e.get("firedAt") and str(e.get("id")) not in recs
                and e.get("retiredReason") is None and e.get("soldSize") is None
                and _match(e)):
            total += _legacy_sold(str(e.get("id")))
    return round(total, 6)


def _standing_put(entries: list[dict]) -> None:
    """Write the list, pruning only RETIRED entries, oldest first, when it is
    over the cap. An armed or merely disarmed instruction is never evicted —
    the cap is enforced where instructions are added, by refusing the add.
    (It used to write `entries[:100]`, which silently dropped the OLDEST entry
    whatever it was, armed stop on an open position included.)"""
    entries = [e for e in entries if isinstance(e, dict)]
    over = len(entries) - _STANDING_MAX
    if over > 0:
        retired = sorted(
            (e for e in entries if _standing_retired(e)),
            key=lambda e: str(e.get("firedAt") or e.get("createdAt") or ""))
        drop = {id(e) for e in retired[:over]}
        for e in retired[:over]:
            _archive_before_drop(e)
        entries = [e for e in entries if id(e) not in drop]
    term._kv_put(_STANDING_KEY, entries)


# Only these fields belong to the TICK. Everything else on an instruction
# belongs to the user, and the tick must never write it back from a copy it
# read before the user touched it.
_TICK_FIELDS = ("lastCheckedAt", "firedAt", "note", "failures", "soldSize",
                "retiredReason")


def _standing_merge_put(worked: list[dict]) -> list[dict]:
    """Write a tick's results back WITHOUT clobbering what the user did meanwhile.

    The tick reads the whole instruction list, then awaits the book, the
    positions and possibly an order — seconds of network — and the RPC handlers
    (`standing_add` / `standing_cancel` / `standing_set_armed`) run as
    concurrent tasks the whole time. A blind write-back of the list read at the
    top therefore RESURRECTED an instruction cancelled mid-tick — armed, with
    a live stop attached to it — and re-armed one the user had just disarmed.

    So re-read, and merge per instruction:
      * an instruction that no longer exists is NOT re-added;
      * one added meanwhile is left exactly as the user wrote it;
      * the tick's own fields are applied to the rows that survived;
      * `armed` only ever moves True → False. The tick never arms anything, so
        a user disarm during the tick stays a disarm.
    """
    by_id = {str(e.get("id")): e for e in worked if isinstance(e, dict)}
    merged: list[dict] = []
    for cur in standing_list():
        if not isinstance(cur, dict):
            continue
        e = by_id.get(str(cur.get("id")))
        if e is None:
            merged.append(cur)
            continue
        out = dict(cur)
        for k in _TICK_FIELDS:
            if k in e:
                out[k] = e[k]
        if not e.get("armed"):
            out["armed"] = False
        merged.append(out)
    _standing_put(merged)
    return merged


def standing_add(item: dict) -> dict:
    kind = str(item.get("kind") or "")
    if kind not in _VALID_KINDS:
        raise TerminalTradeError(f"Unknown instruction kind '{kind}'.")
    cid = str(item.get("conditionId") or "").strip()
    if not cid:
        raise TerminalTradeError("No market selected.")
    trigger = num(item.get("triggerCents"))
    if trigger is None or not (0 < trigger < 100):
        raise TerminalTradeError(
            "The trigger is a PROBABILITY POINT between 0 and 100 — not a "
            "percentage move.")
    size = num(item.get("size"))
    slip = None
    if kind in _STOP_KINDS:
        if size is None or size <= 0:
            raise TerminalTradeError(
                "A stop or take-profit has to say how many contracts to close.")
        if size > MAX_MANUAL_CONTRACTS:
            raise TerminalTradeError(
                f"Size is over the {MAX_MANUAL_CONTRACTS:,} manual ceiling.")
        # How far below the best bid the exit may reach. A stop that may not
        # cross the spread is not a stop, so there is always a budget.
        raw_slip = num(item.get("maxSlippageCents"))
        slip = (DEFAULT_SELL_SLIPPAGE_CENTS if raw_slip is None
                else max(0.0, min(float(MAX_SELL_SLIPPAGE_CENTS), float(raw_slip))))
    else:
        size = None

    entries = standing_list()
    if _standing_active_count(entries) >= _STANDING_MAX:
        # Never make room by deleting a live instruction: that silently
        # removed the oldest armed stop protecting an open position.
        raise TerminalTradeError(
            f"You already have {_STANDING_MAX} active standing instructions, "
            f"which is the most this app keeps. Cancel ones you no longer need "
            f"and add this one again — nothing was added.")

    entry = {
        "id": f"st_{uuid.uuid4().hex[:10]}",
        "conditionId": cid,
        "title": item.get("title") or None,
        "side": "no" if str(item.get("side") or "yes").lower() == "no" else "yes",
        "kind": kind,
        "triggerCents": round(trigger, 2),
        "size": size,
        "maxSlippageCents": slip,
        "createdAt": _now_iso(),
        "armed": True,
        "lastCheckedAt": None,
        "firedAt": None,
        "note": item.get("note") or None,
        # Who wrote it down: an agent's stop fires as an `ai` order, so the
        # order log and Track never attribute it to the user.
        "origin": "ai" if item.get("origin") == "ai" else "manual",
    }
    entries.insert(0, entry)
    _standing_put(entries)
    return entry


def standing_cancel(instruction_id: str) -> list[dict]:
    iid = str(instruction_id or "")
    keep: list[dict] = []
    for e in standing_list():
        if e.get("id") == iid:
            # Deleting a fired stop must not delete what it sold.
            _archive_before_drop(e)
            continue
        keep.append(e)
    _standing_put(keep)
    return keep


def standing_set_armed(instruction_id: str, armed: bool) -> list[dict]:
    entries = standing_list()
    for e in entries:
        if e.get("id") == str(instruction_id):
            e["armed"] = bool(armed)
            if armed and e.get("retiredReason") == "failed":
                # A stop that gave up after repeated non-fills is re-armable:
                # nothing it was written for has been sold yet (or only part).
                e["firedAt"] = None
                e["failures"] = 0
                e["retiredReason"] = None
    _standing_put(entries)
    return entries


def _stop_exit_limit(ref_bid_cents: float, slip_cents: float,
                     tick: Optional[float]) -> float:
    """The limit for a stop's marketable fill-and-kill: the reference bid less
    the slippage budget, on the market's tick grid.

    Rounded UP onto the grid, so the order never reaches further below the bid
    than the budget allows; never above the bid itself; and never below one
    tick. A FAK sells to every resting bid at or above this price, each at ITS
    price, so the limit is the worst case, not the expected fill.
    """
    tc = float(tick or 0.01) * 100.0
    if tc <= 0:
        tc = 1.0
    lo = tc
    hi = 100.0 - tc
    bid = float(ref_bid_cents)
    floor = bid - max(0.0, float(slip_cents))
    limit = math.ceil(floor / tc - 1e-9) * tc
    top = math.floor(bid / tc + 1e-9) * tc
    limit = min(limit, top)
    return round(max(lo, min(hi, limit)), 4)


def _stop_fill(res: dict, sent: float) -> tuple[float, bool]:
    """How many contracts a stop's sell actually sold → (sold, confirmed).

    A refusal, a kill with no fill, or a reported fill of zero sold nothing.
    An accepted order whose fill size the venue did not report is assumed to
    have sold the size sent — re-sending on that guess would risk selling the
    same contracts twice — and is marked unconfirmed until it reconciles.
    """
    if not res.get("ok"):
        return 0.0, True
    filled = num(res.get("filledSize"))
    status = str(res.get("status") or "").lower()
    if status in ("unmatched", "killed", "canceled", "cancelled", "rejected"):
        return max(0.0, float(filled or 0.0)), True
    if filled is None:
        return float(sent), False
    return max(0.0, min(float(filled), float(sent))), True


async def standing_tick(*, allow_trading: bool = True, paper: bool = False) -> dict:
    """Evaluate every armed instruction once.

    A stop loss is an instruction the user wrote down, so it executes under
    exactly the rules a manual click passes: paper mode blocks it, the positions
    must be readable, and every order goes through `place()`. It is NOT held
    to the automation master switch — the terminal is manual, the engines are
    not. `allow_trading=False` remains available to a caller that wants to
    hold fire explicitly.
    """
    # Belt and braces: the caller passes the paper switch, and we ALSO read
    # it, so a caller that forgets cannot send a real sell from paper mode.
    paper = bool(paper) or _paper_mode_on()
    entries = standing_list()
    if not entries:
        return {"checked": 0, "fired": 0, "alerts": []}

    checked = 0
    fired = 0
    alerts: list[dict] = []
    by_market: dict[str, Optional[dict]] = {}

    # Read the positions ONCE per tick. A stop that fires against a position you
    # no longer hold is not harmless: the venue rejects it, the attempt lands
    # in the order log as an error, and — worse — the instruction stays armed
    # and does it again on the next tick. `None` means the read FAILED, which
    # is not the same as holding nothing, so we hold fire rather than guess.
    holdings: Optional[dict[tuple[str, str], float]] = None
    if any(e.get("armed") and not e.get("firedAt")
           and e.get("kind") in _STOP_KINDS for e in entries):
        try:
            holdings = {}
            for pos in await api.get_positions(strict=True):
                signed = float(pos.get("position_fp") or 0.0)
                if signed == 0:
                    continue
                key = (str(pos.get("ticker") or ""),
                       "yes" if signed > 0 else "no")
                holdings[key] = holdings.get(key, 0.0) + abs(signed)
        except Exception as e:  # noqa: BLE001
            logger.debug("standing tick: position read failed: %s", e)
            holdings = None

    for e in entries:
        if not e.get("armed") or e.get("firedAt"):
            continue
        cid = str(e.get("conditionId") or "")
        if cid not in by_market:
            try:
                m = await term.market(cid)
                # A stop sells real shares, so it reads the freshest quote there
                # is. A row that read fails for keeps the quote it has.
                if m:
                    await term.clob_quotes([m])
                by_market[cid] = m
            except Exception:
                by_market[cid] = None
        m = by_market.get(cid)
        e["lastCheckedAt"] = _now_iso()
        checked += 1
        if not m:
            continue

        side = e.get("side") or "yes"
        # Price the side the instruction is actually about. A NO position is
        # measured on the NO price, not on 100 minus a YES mid we may not have.
        p = m.get("probability")
        if p is None:
            continue
        price = p if side == "yes" else round(100.0 - p, 2)
        trig = float(e["triggerCents"])
        kind = e["kind"]

        hit = (
            (kind == "stopLoss" and price <= trig)
            or (kind == "takeProfit" and price >= trig)
            or (kind == "alertAbove" and price >= trig)
            or (kind == "alertBelow" and price <= trig)
        )
        if not hit:
            continue

        if kind in ("alertAbove", "alertBelow"):
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "alert"
            alerts.append({
                "id": e["id"], "conditionId": cid,
                "title": m.get("title"), "kind": kind,
                "priceCents": price, "triggerCents": trig,
            })
            fired += 1
            continue

        if paper:
            # A standing instruction is written against a REAL position, and
            # paper mode means "send nothing real". Say so and retire it,
            # exactly like the live-trading-off case below. It used to fall
            # straight through to place(), which had no paper check — a real
            # FAK sell signed while the user believed they were on paper.
            e["note"] = (
                f"Trigger hit at {price:g}¢ but paper mode is on, so nothing "
                f"was sold. Turn paper mode off and re-arm.")
            e["armed"] = False
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True, "result": e["note"],
            })
            continue

        if not allow_trading:
            # Do NOT silently swallow this. An unfired stop is exactly the
            # thing a user must be told about.
            e["note"] = (
                f"Trigger hit at {price:g}¢ but live trading is off, so "
                f"nothing was sold. Re-arm after enabling trading.")
            e["armed"] = False
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True,
            })
            continue

        # ── do we still hold this? ─────────────────────────────────────
        # What is left to sell: a partial fill on an earlier tick already
        # sold part of the instruction, and only the rest is still armed.
        want = standing_remaining(e)
        prior_sold = float(num(e.get("soldSize")) or 0.0)
        if holdings is None:
            # Could not read the positions. Selling blind is worse than waiting:
            # leave it armed and try again next tick.
            e["note"] = (
                f"Trigger hit at {price:g}¢ but your positions could not be "
                f"read just now, so nothing was sold. Still armed — this will "
                f"retry.")
            continue
        have = holdings.get((cid, side), 0.0)
        if have <= 0:
            # The position is gone (sold by hand, closed elsewhere, settled).
            # Disarm rather than re-attempt an order the venue will reject
            # every tick from now until the heat death of the universe.
            e["armed"] = False
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "fired" if prior_sold > 0 else "noPosition"
            e["note"] = (
                f"Trigger hit at {price:g}¢, but you no longer hold this "
                f"{side.upper()} position — nothing to sell, so the "
                f"instruction has been retired.")
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True, "result": e["note"],
            })
            continue
        short_note = None
        if have < want:
            # Sell what is actually there, and SAY that the instruction was
            # written against a bigger position than now exists.
            short_note = (
                f"Instruction was for {want:g} contract(s) but only {have:g} "
                f"are held — sold {have:g}.")
            want = have
        # Onto the market's quantity step, and down: rounding a fractional holding
        # UP would try to sell contracts that are not there.
        want = _floor_qty(want, num(m.get("minOrderSize")))
        if want <= 0:
            e["armed"] = False
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "fired" if prior_sold > 0 else "noPosition"
            e["note"] = (
                f"Trigger hit at {price:g}¢, but less than this market's smallest "
                f"order step is left to sell, so the instruction has been "
                f"retired. The rest can only be "
                f"closed by letting the market settle.")
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "blocked": True, "result": e["note"],
            })
            continue

        # ── a MARKETABLE exit ──────────────────────────────────────────
        # A sell limited AT the trigger matches nothing once the price has
        # crossed it: the bid is already below. Price off the bid instead,
        # down to the instruction's slippage budget, on the market's tick —
        # the same exit the Sell panel walks. The bid is the live book's when
        # the preview could read it, else the quote the trigger fired on.
        slip = num(e.get("maxSlippageCents"))
        slip = (DEFAULT_SELL_SLIPPAGE_CENTS if slip is None
                else max(0.0, min(float(MAX_SELL_SLIPPAGE_CENTS), float(slip))))
        yes_bid, yes_ask = num(m.get("bid")), num(m.get("ask"))
        quote_bid = (yes_bid if side == "yes"
                     else (round(100.0 - yes_ask, 4) if yes_ask is not None else None))
        ref_bid = quote_bid if quote_bid is not None else float(price)
        limit = _stop_exit_limit(ref_bid, slip, None)
        req = {
            "conditionId": cid, "side": side, "action": "sell",
            "priceCents": limit, "size": want, "orderType": "FAK",
        }
        pv = await preview(req)
        book_bid = num((pv or {}).get("bestBidCents"))
        tick = num((pv or {}).get("tickSize"))
        if book_bid is not None or tick is not None:
            better = _stop_exit_limit(
                book_bid if book_bid is not None else ref_bid, slip, tick)
            if abs(better - limit) > 1e-9:
                req["priceCents"] = limit = better
                pv = await preview(req)
        req["confirmUsd"] = (pv or {}).get("costUsd")
        res = await place(req, origin=e.get("origin") or "manual",
                          standing_id=e["id"])
        sold, confirmed = _stop_fill(res, want)
        venue_msg = str(res.get("message") or "")

        if sold > 0:
            total_sold = round(prior_sold + sold, 6)
            e["soldSize"] = total_sold
            e["failures"] = 0
            remaining = round(want - sold, 6)
            final = remaining <= 1e-9
            # Durable BEFORE anything else: these contracts are gone whatever
            # happens to the instruction from here.
            try:
                _record_stop_sale(e, sold, res.get("orderId"), confirmed,
                                  final=final)
            except Exception as ex:  # noqa: BLE001
                logger.warning("standing: could not record stop sale %s: %s",
                               e.get("id"), ex)
            unconf = ("" if confirmed else
                      " The venue did not report the fill size, so this assumes "
                      "the whole order filled — Track shows the real fill once "
                      "it reconciles.")
            if final:
                e["firedAt"] = _now_iso()
                e["armed"] = False
                e["retiredReason"] = "fired"
                e["note"] = " ".join(x for x in (
                    short_note,
                    f"Sold {sold:g} contract(s) at {limit:g}¢ or better "
                    f"(trigger {trig:g}¢).{unconf}", venue_msg) if x)
                fired += 1
            else:
                e["note"] = " ".join(x for x in (
                    short_note,
                    f"Partly fired: sold {sold:g} of {want:g} at {limit:g}¢ or "
                    f"better; the other {remaining:g} found no bid within "
                    f"{slip:g}¢ and is STILL ARMED — it will retry.{unconf}",
                    venue_msg) if x)
            alerts.append({
                "id": e["id"], "conditionId": cid, "title": m.get("title"),
                "kind": kind, "priceCents": price, "triggerCents": trig,
                "result": e["note"], "ok": True, "blocked": False,
                "partial": not final, "soldSize": sold,
                "remainingSize": max(0.0, remaining),
                "limitCents": limit, "status": res.get("status"),
            })
            continue

        # ── nothing sold: the stop did NOT fire ────────────────────────
        # A FAK that matched nothing comes back ok=True / 'unmatched'. That
        # used to be read as success: the stop was marked fired, disarmed and
        # announced as "Stop loss fired" while the position rode on
        # unprotected. Any outcome that sold nothing keeps it ARMED.
        #
        # Bounded, though — a deterministic refusal (sub-minimum size, a
        # region block, market closed) would otherwise re-send an order every
        # tick forever. After MAX_STANDING_FAILURES it retires itself and
        # says exactly why, which is a state the user can see and act on.
        if res.get("ok"):
            why = (f"the sell at {limit:g}¢ (bid less {slip:g}¢ slippage) "
                   f"matched nothing")
        elif res.get("status") == "unknown":
            why = "Polymarket did not confirm the sell"
        else:
            why = "the sell was refused"
        fails = int(e.get("failures") or 0) + 1
        e["failures"] = fails
        tail = " ".join(x for x in (short_note, venue_msg) if x)
        if fails >= MAX_STANDING_FAILURES:
            e["armed"] = False
            e["firedAt"] = _now_iso()
            e["retiredReason"] = "fired" if prior_sold > 0 else "failed"
            e["note"] = (
                f"Did NOT fire: {why}. Tried {fails} times and nothing sold — "
                f"giving up so it stops re-sending. Your position is still "
                f"open; sell it from the Market page, or re-arm this. "
                f"Last result: {tail}").strip()
        else:
            e["note"] = (
                f"Trigger hit at {price:g}¢ but the stop did NOT fire: {why}, "
                f"so nothing was sold (attempt {fails} of "
                f"{MAX_STANDING_FAILURES}) — still armed, will retry. "
                f"{tail}").strip()
        alerts.append({
            "id": e["id"], "conditionId": cid, "title": m.get("title"),
            "kind": kind, "priceCents": price, "triggerCents": trig,
            "result": e["note"], "ok": False, "blocked": True,
            "soldSize": 0.0, "limitCents": limit, "status": res.get("status"),
        })

    _standing_merge_put(entries)
    return {"checked": checked, "fired": fired, "alerts": alerts}
