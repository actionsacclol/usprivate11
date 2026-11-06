"""Paper trading for the manual terminal.

The point of paper mode is to be WRONG IN THE SAME PLACES the real thing is.
A simulator that fills your whole order at your limit price, instantly, for
free, does not teach you to trade — it teaches you that trading is easy, and
then the first real order behaves nothing like it. So every simplification
here is deliberate, conservative, and stated to the user.

What is simulated faithfully:

* **Fills come out of the REAL book.** We walk the live depth exactly as the
  order ticket's preview does. You get what was actually resting at your price
  and no more; the rest is unfilled, not quietly granted.
* **The price you get is the WALKED average**, not your limit. Sweeping three
  levels costs what sweeping three levels costs.
* **Fees are the real curve**, from the market's own fee coefficient and rounded
  the way the exchange rounds. An unknown fee stays unknown rather than becoming
  zero.
* **Tick size and minimum order size** are the venue's, so an order that the
  real exchange would reject is rejected here too.
* **You cannot sell what you do not hold.** Polymarket has no naked short and
  neither does this.
* **Settlement is exact.** A resolved market pays 100¢ or 0¢, because that is
  what a binary contract does — or the settlement value on a dead heat
  (``outcomePrices == ["0.5","0.5"]``: a cancelled game, a map that was never
  played). See ``split_resolution``.
* **Fees are part of what a position cost.** A taker buy's fee goes into the
  position's cost basis (so ``avgCostCents`` is fee-inclusive), and a sell's
  fee comes off its realised P&L. For every book, at all times:
  ``realised + unrealised == cash + position value − bankroll`` — the P&L the
  record shows is exactly the money the account made, net of every fee.

What is approximated, and why (all of this is surfaced in the UI):

* **The depth is a snapshot.** The public book can be 30 seconds old, so a fill
  walks what was resting then. The ticket says so when the market has moved.
* **Queue position.** We cannot know where in the queue a resting order would
  sit, so a resting order fills only when the market trades STRICTLY THROUGH
  its price — never merely to it. That is the pessimistic assumption: in real
  life you would sometimes fill at the touch. Erring the other way would
  manufacture fills that never happened, which is the exact lie this module
  exists to avoid. When it does fill, it fills at its OWN limit: a maker is
  paid its price, not the better one the book has since moved to.
* **Market impact.** Your paper order consumes real depth in the simulation
  but does not move the real book, so a large order is modelled slightly
  better than it would really be. The UI warns once an order eats a
  meaningful share of the visible depth.

Its own tables, its own totals. A paper number is never added to a real one.

Two paper ACCOUNTS share the table: ``manual`` (the ticket) and ``ai`` (an AI
agent over MCP). Every read and write is scoped to the account selected with
``using_book()``; the default is ``manual``, so every existing caller is
unchanged. An agent's paper record has to stand on its own, or nobody can tell
whether the agent is any good.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from typing import Iterator, Optional

import polymarket_api as api
import terminal as term
import terminal_trade as tt
import us_api as us
from terminal import num, _now_iso

logger = logging.getLogger("krypt.terminal.paper")

DEFAULT_BANKROLL_USD = 1000.0

# A paper order that would eat more than this share of the visible depth is
# flagged: past that point the "no market impact" simplification stops being a
# rounding error and starts being the whole result.
_IMPACT_WARN_FRACTION = 0.25

BOOKS = ("manual", "ai")
# A ContextVar rather than a parameter on every function: it follows the
# coroutine, so two concurrent requests (the ticket and an agent) can never
# read each other's account, and no existing call site had to change.
_BOOK: contextvars.ContextVar[str] = contextvars.ContextVar(
    "paper_book", default="manual")


def current_book() -> str:
    return _BOOK.get()


def valid_book(book: str) -> bool:
    """`manual`, `ai` (the Default agent), or `ai:<agent id>` — each named
    agent's own account (mcp_agents.paper_book)."""
    if book in BOOKS:
        return True
    if isinstance(book, str) and book.startswith("ai:"):
        import mcp_agents
        return mcp_agents.valid_id(book[3:])
    return False


def is_agent_book(book: str) -> bool:
    return book == "ai" or (isinstance(book, str) and book.startswith("ai:"))


def books_in_use() -> list[str]:
    """Every paper account with a row, plus the two built-in ones."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT COALESCE(book,'manual') FROM terminal_paper_orders"
        ).fetchall()
    out = list(BOOKS)
    for r in rows:
        b = str(r[0] or "manual")
        if b not in out and valid_book(b):
            out.append(b)
    return out


@contextlib.contextmanager
def using_book(book: str) -> Iterator[None]:
    if not valid_book(book):
        raise ValueError(f"unknown paper book {book!r}")
    token = _BOOK.set(book)
    try:
        yield
    finally:
        _BOOK.reset(token)

# Book reads per paper tick. The venue allows about 5 per 10 seconds per IP, and
# the chart and ticket want that budget too, so the simulator spends it only on a
# resting order the market has already crossed. A paper fill a tick late costs
# nothing; a 429 on the live book costs the user their screen.
_MAX_BOOKS_PER_TICK = 2
# Market reads per paper tick. Those are cheap, but "cheap" times every market
# with a resting paper order is still a burst. Past this they take turns.
_MAX_TOUCHES_PER_TICK = 20
_touch_cursor = 0


def _conn():
    import db
    return db.get_db()


def _net() -> str:
    return tt._net()


# ────────────────────────────────────────────────────────────────────────
# Position / cash accounting
#
# Same average-cost convention as the real ledger, and deliberately the same
# shape, so the two screens are comparable. A SETTLE row is just a sell at
# 100c, 0c (or 50c on a 50-50 resolution), which keeps this to one code path.
#
# Fees: a buy's fee is CAPITALISED into the position's cost (avg cost is
# fee-inclusive, i.e. the break-even price); a sell's fee is charged to that
# sell's realised P&L. Cash moves by exactly the same amounts, so
#     realised + (value − open cost) == cash_delta + value
# holds for every book — the record never reads better than the cash did.
# Before 2026-10-12 the buy fee left cash but never touched cost, so
# realised + unrealised overstated P&L by every entry fee (audit H9).
# ────────────────────────────────────────────────────────────────────────


def _rows() -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_paper_orders "
            "WHERE filled_size > 0 AND COALESCE(book,'manual')=? "
            "ORDER BY COALESCE(filled_at, created_at) ASC", (current_book(),))]


def _book_state() -> tuple[dict[tuple[str, str], dict], float, bool]:
    """→ (positions by (conditionId, side), cash_delta, fees_all_known)."""
    pos: dict[tuple[str, str], dict] = {}
    cash = 0.0
    fees_known = True
    for r in _rows():
        key = (str(r["condition_id"]), str(r["side"]))
        st = pos.setdefault(key, {
            "size": 0.0, "cost": 0.0, "realized": 0.0,
            "title": r["title"] or "", "settled": False,
        })
        sz = float(r["filled_size"] or 0.0)
        px = float(r["avg_fill_price_cents"] or 0.0)
        fee = r["fees_usd"]
        if fee is None:
            fees_known = False
            fee = 0.0
        fee = float(fee)

        if str(r["action"]) == "buy":
            st["size"] += sz
            # The fee is part of what these contracts cost (see above).
            st["cost"] += sz * px / 100.0 + fee
            cash -= sz * px / 100.0 + fee
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                st["realized"] += close * (px / 100.0 - avg)
                st["cost"] -= close * avg
                st["size"] -= close
            # A fee paid is a fee lost, even on a sell that found nothing left
            # to close: cash pays it below, so realised has to say so too.
            st["realized"] -= fee
            # Credit cash for the contracts that were actually held, not for
            # the size on the row. A sell that outran the holding (two resting
            # sells against one position) used to credit both in full: a
            # phantom gain in cash that realised P&L never showed.
            cash += close * px / 100.0 - fee
            if str(r["order_type"]) == "SETTLE":
                st["settled"] = True
    return pos, cash, fees_known


def _held_gross(condition_id: str, side: str) -> float:
    """Contracts held right now, before any resting sell is counted."""
    pos, _, _ = _book_state()
    return float((pos.get((condition_id, side)) or {}).get("size") or 0.0)


def _reserved_by_resting_sells(condition_id: str, side: str) -> float:
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(resting_size), 0) FROM terminal_paper_orders "
            "WHERE condition_id=? AND side=? AND action='sell' "
            "AND status IN ('resting','partial') AND COALESCE(book,'manual')=?",
            (condition_id, side, current_book())).fetchone()
    return float(row[0] or 0.0)


def _held(condition_id: str, side: str) -> float:
    """Contracts available to SELL: held, minus what resting sells already
    claim. A resting sell reserves no inventory at the venue either — it is
    simply rejected on fill — but here two GTC sells against one holding used
    to both pass the naked-short check and both fill."""
    return max(0.0, _held_gross(condition_id, side)
               - _reserved_by_resting_sells(condition_id, side))


def _bankroll(cfg: Optional[dict]) -> float:
    key = ("mcp_paper_bankroll_usd" if is_agent_book(current_book())
           else "terminal_paper_bankroll_usd")
    v = num((cfg or {}).get(key))
    return v if v and v > 0 else DEFAULT_BANKROLL_USD


# ────────────────────────────────────────────────────────────────────────
# What a resolved market paid
# ────────────────────────────────────────────────────────────────────────


def split_resolution(m: Optional[dict]) -> bool:
    """Did this market resolve 50-50?

    The venue can settle a market between 0 and 1 — a cancelled or postponed
    game, a map or game of a series that was never played — and then both sides
    are paid their share, usually 50¢ each. ``terminal.summary_from_gamma`` only
    names a 100/0 outcome, so such a market reads ``status: "settled"`` with
    ``resolvedOutcome: None`` — which used to mean "never settles": paper cash
    stayed locked in a position nobody could sell, and the record never included
    it.

    Read from the summary's ``_raw`` row (``term._summaries_for`` keeps it).
    Only ``MARKET_STATUS_RESOLVED`` counts: a market merely awaiting settlement
    can show any pair of prices, 50/50 included, and settling that at 50¢ would
    be an invented payout. No ``_raw`` → False.
    """
    return _split_yes_cents(m) is not None


def _split_yes_cents(m: Optional[dict]) -> Optional[float]:
    """What YES pays on a split, in cents, or None when this market is not one."""
    if not isinstance(m, dict) or m.get("resolvedOutcome") is not None:
        return None
    raw = m.get("_raw")
    if not isinstance(raw, dict):
        return None
    result, yes_value = us._settlement_of(raw, us.outcome_prices(raw))
    if result or yes_value is None:
        return None
    return round(yes_value * 100.0, 4)


def payout_cents(m: Optional[dict], side: str) -> Optional[float]:
    """What one contract of `side` pays in this market: 100 / 0 for a decided
    market, the settlement value on a split, None while it is unresolved (or the
    market could not be read). Never a guess."""
    resolved = (m or {}).get("resolvedOutcome")
    if resolved in ("yes", "no"):
        return 100.0 if resolved == side else 0.0
    yes_cents = _split_yes_cents(m)
    if yes_cents is None:
        return None
    return yes_cents if side == "yes" else round(100.0 - yes_cents, 4)


def _resolution_word(m: dict) -> str:
    r = m.get("resolvedOutcome")
    if r in ("yes", "no"):
        return str(r).upper()
    yes_cents = _split_yes_cents(m)
    return "50-50" if yes_cents == 50.0 else f"as a split (YES paid {yes_cents:g}¢)"


# ────────────────────────────────────────────────────────────────────────
# Placing a simulated order
# ────────────────────────────────────────────────────────────────────────


def _record(row: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO terminal_paper_orders
               (condition_id, title, side, action, order_type, requested_size,
                requested_price_cents, filled_size, avg_fill_price_cents,
                resting_size, fees_usd, cost_usd, status, note, network,
                filled_at, book)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["condition_id"], row.get("title") or "", row["side"],
             row["action"], row["order_type"], row["requested_size"],
             row["requested_price_cents"], row.get("filled_size") or 0.0,
             row.get("avg_fill_price_cents"), row.get("resting_size") or 0.0,
             row.get("fees_usd"), row.get("cost_usd"), row["status"],
             row.get("note") or "", _net(),
             row.get("filled_at"), current_book()),
        )
        return int(cur.lastrowid or 0)


async def place(req: dict, cfg: Optional[dict] = None) -> dict:
    """Simulate one hand-placed order against the live book."""
    # Reuse the REAL ticket's preview so paper and live are refused for the
    # same reasons. `paper` tells it to skip the API key check — everything
    # else (closed market, bad price, sub-minimum size) still applies.
    pv = await tt.preview({**req, "paper": True})
    blockers = pv["blockers"]
    if blockers:
        return {"ok": False, "localId": None, "message": blockers[0],
                "filledSize": None, "avgPriceCents": None, "status": None}

    cid = str(req["conditionId"])
    side = pv["side"]
    action = pv["action"]
    order_type = str(req.get("orderType") or "GTC").upper()
    size = float(pv["size"] or 0.0)
    price = float(pv["priceCents"] or 0.0)

    summary = await term.market(cid, include_raw=True)
    title = (summary or {}).get("title") or ""

    # No naked shorts — Polymarket does not have them, so neither do we.
    if action == "sell":
        have = _held(cid, side)
        if have < size - 1e-9:
            return {
                "ok": False, "localId": None, "filledSize": None,
                "avgPriceCents": None, "status": None,
                "message": (
                    f"You hold {have:g} paper {side.upper()} contract(s), so "
                    f"you cannot sell {size:g}. Polymarket has no naked short "
                    f"and neither does paper mode."),
            }

    bk = await term.book(cid, side)
    if not bk.get("available"):
        return {
            "ok": False, "localId": None, "filledSize": None,
            "avgPriceCents": None, "status": None,
            "message": (
                "Could not read the order book, so there is nothing honest to "
                "simulate against. A paper fill invented without a book is "
                f"worse than no fill at all. ({bk.get('note') or 'no book'})"),
        }

    levels = bk["asks"] if action == "buy" else bk["bids"]
    fillable, avg = tt._walk_book(levels, size, price, action == "buy")
    fillable = fillable or 0.0
    warnings = list(pv.get("warnings") or [])

    # Impact check: past a quarter of the visible depth, "your order does not
    # move the book" stops being a rounding error.
    visible = sum(float(l["size"]) for l in levels) or 0.0
    if visible and fillable / visible > _IMPACT_WARN_FRACTION:
        warnings.append(
            f"This order takes {fillable / visible * 100:.0f}% of the visible "
            f"depth. Paper mode does not move the real book, so a live order "
            f"this size would fill worse than this.")

    note_bits: list[str] = []
    resting = 0.0
    status = "unfilled"

    if order_type == "FOK" and fillable < size - 1e-9:
        fillable, avg = 0.0, None
        status = "unfilled"
        note_bits.append(
            "All-or-none order killed: the book could not fill the whole size "
            "at your price.")
    elif order_type in ("FAK", "FOK"):
        status = "filled" if fillable >= size - 1e-9 else ("partial" if fillable > 0 else "unfilled")
        if fillable < size:
            note_bits.append(
                f"{size - fillable:g} contract(s) killed — nothing was resting "
                f"at your price.")
    else:  # GTC
        resting = round(size - fillable, 4)
        if fillable > 0 and resting > 0:
            status = "partial"
            note_bits.append(
                f"{fillable:g} crossed immediately; {resting:g} is resting on "
                f"the book and will fill only if the market trades through "
                f"{price:g}¢.")
        elif fillable > 0:
            status = "filled"
        else:
            status = "resting"
            note_bits.append(
                f"Nothing crossed at {price:g}¢, so the whole order is resting. "
                f"Paper mode cannot know your place in the queue, so it fills "
                f"a resting order only when the market trades STRICTLY through "
                f"your price — the pessimistic assumption.")

    fee = None
    if fillable > 0 and avg is not None:
        # The crossing part of any order is a TAKER fill. The resting part pays
        # nothing here and nothing when it fills later (it would be maker).
        fee = tt.taker_fee_usd(
            avg, fillable,
            api._fee_schedule_from_gamma((summary or {}).get("_raw") or {}))
        if fee is None:
            note_bits.append(
                "This market's fee rate is unknown, so the cost below excludes "
                "fees — exactly as the live ticket would tell you.")

    cost = round(fillable * avg / 100.0, 4) if (fillable > 0 and avg is not None) else None

    local = _record({
        "condition_id": cid, "title": title, "side": side, "action": action,
        "order_type": order_type, "requested_size": size,
        "requested_price_cents": price,
        "filled_size": fillable, "avg_fill_price_cents": avg,
        "resting_size": resting, "fees_usd": fee, "cost_usd": cost,
        "status": status, "note": " ".join(note_bits),
        "filled_at": _now_iso() if fillable > 0 else None,
    })

    if fillable > 0:
        msg = (f"Paper {action}: {fillable:g} {side.upper()} at "
               f"{avg:.1f}¢ average"
               + (f" (you asked for {price:g}¢)" if abs(avg - price) > 0.05 else "")
               + f" — ${cost:,.2f}.")
    else:
        msg = f"Paper order recorded with no fill. {note_bits[0] if note_bits else ''}"

    return {
        "ok": True, "localId": local, "status": status,
        "filledSize": fillable or 0.0, "avgPriceCents": avg,
        "message": msg.strip(), "warnings": warnings,
    }


async def cancel(local_id: int) -> dict:
    with _conn() as conn:
        r = conn.execute(
            "SELECT status FROM terminal_paper_orders WHERE id=? "
            "AND COALESCE(book,'manual')=?",
            (int(local_id), current_book())).fetchone()
        if not r:
            return {"ok": False, "message": "No such paper order."}
        if str(r["status"]) not in ("resting", "partial"):
            return {"ok": False, "message": "That paper order is not resting."}
        conn.execute(
            "UPDATE terminal_paper_orders SET status='canceled', resting_size=0 "
            "WHERE id=?", (int(local_id),))
    return {"ok": True, "message": "Paper order canceled."}


# ────────────────────────────────────────────────────────────────────────
# The background pass: fill resting orders, settle resolved markets
# ────────────────────────────────────────────────────────────────────────


async def tick() -> dict:
    """Advance the paper book. Cheap and safe to call every few seconds."""
    filled = await _fill_resting()
    settled = await _settle_resolved()
    return {"filled": filled, "settled": settled}


async def _fill_resting() -> int:
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM terminal_paper_orders "
            "WHERE status IN ('resting','partial') AND resting_size > 0")]
    if not rows:
        return 0

    # The market read decides whether anything crossed, and the book is read only
    # when something did, once per (market, side). A sweep that read the book for
    # every resting order would park the venue's book route on a 429 and starve
    # the REAL chart and book. Paper trading costs nothing; it must not cost the
    # terminal its rate budget either.
    touches = await _resting_touches({str(r["condition_id"]) for r in rows})
    books: dict[tuple[str, str], dict] = {}
    fetched = 0

    n = 0
    for r in rows:
        cid = str(r["condition_id"])
        side = str(r["side"])
        want = float(r["resting_size"] or 0.0)
        limit = float(r["requested_price_cents"] or 0.0)
        buy = str(r["action"]) == "buy"

        m = touches.get(cid)
        if not m:
            continue
        bid, ask = term.side_touch(m, side)
        touch = ask if buy else bid
        if touch is None:
            continue

        # STRICTLY through, never merely to. We cannot know queue position, so
        # filling at the touch would manufacture fills that may never have
        # happened to us. Pessimistic on purpose.
        crossed = (touch < limit) if buy else (touch > limit)
        if not crossed:
            continue

        key = (cid, side)
        if key not in books:
            if fetched >= _MAX_BOOKS_PER_TICK:
                # Bounded per tick. The rest are picked up on the next pass —
                # a paper fill arriving a few seconds later is a far smaller
                # problem than rate-limiting the live terminal.
                break
            fetched += 1
            books[key] = await term.book(cid, side)
        bk = books[key]
        if not bk.get("available"):
            continue

        levels = bk["asks"] if buy else bk["bids"]
        # The walk says how MUCH could have reached us, never at what price. A
        # resting order is the maker: whoever crossed it paid its limit. The
        # better price the book has since moved to was never ours, and handing
        # it over flattered every GTC strategy by the whole gap.
        got, _ = tt._walk_book(levels, want, limit, buy)
        got = got or 0.0
        if got <= 0:
            continue
        avg = limit

        if not buy:
            # Cannot sell what is no longer held. The position may have been
            # closed by another order since this sell was written; filling it
            # anyway would be the exact fabricated fill a simulator exists to
            # avoid. Cancel with a note rather than let it sit forever.
            with using_book(str(r.get("book") or "manual")):
                held_now = _held_gross(cid, side)
            if held_now <= 1e-9:
                with _conn() as conn:
                    conn.execute(
                        """UPDATE terminal_paper_orders
                           SET status='canceled', resting_size=0,
                               note = TRIM(COALESCE(note,'') || ?)
                           WHERE id=? AND status IN ('resting','partial')""",
                        (" Canceled: the position was no longer held when the "
                         "market traded through.", int(r["id"])))
                continue
            got = min(got, held_now)

        # A resting order that fills is a MAKER fill: no taker fee.
        prev_size = float(r["filled_size"] or 0.0)
        prev_avg = r["avg_fill_price_cents"]
        total = prev_size + got
        new_avg = (
            (prev_size * float(prev_avg) + got * avg) / total
            if prev_avg is not None and total > 0 else avg)
        remaining = round(want - got, 4)
        # An order that rested whole was written with no fee (nothing had
        # traded), and NULL reads as "fee rate unknown" everywhere a total is
        # built. A maker fee is known: it is zero. A crossed part whose taker
        # fee really was unknown keeps that unknown.
        fee = r.get("fees_usd")
        if fee is None and prev_size <= 0:
            fee = 0.0

        # Only if it is still resting. The row was read before the book was
        # awaited, and a cancel that landed in between was already told
        # "Paper order canceled." — filling it now would make that a lie.
        with _conn() as conn:
            cur = conn.execute(
                """UPDATE terminal_paper_orders
                   SET filled_size=?, avg_fill_price_cents=?, resting_size=?,
                       cost_usd=?, fees_usd=?, status=?,
                       filled_at=COALESCE(filled_at, ?),
                       note = TRIM(COALESCE(note,'') || ?)
                   WHERE id=? AND status IN ('resting','partial')""",
                (total, round(new_avg, 3), max(0.0, remaining),
                 round(total * new_avg / 100.0, 4), fee,
                 "filled" if remaining <= 1e-9 else "partial",
                 _now_iso(),
                 f" Market traded through {limit:g}¢, so {got:g} filled at "
                 f"{limit:g}¢ as maker (no fee).",
                 int(r["id"])),
            )
        if cur.rowcount:
            n += 1
    return n


async def _resting_touches(cids: set[str]) -> dict[str, dict]:
    """The current quote for the markets with resting paper orders, at most
    `_MAX_TOUCHES_PER_TICK` of them per pass. A market that could not be read is
    absent, and its orders simply wait for a later tick."""
    global _touch_cursor
    ordered = sorted(cids)
    if len(ordered) > _MAX_TOUCHES_PER_TICK:
        start = _touch_cursor % len(ordered)
        ordered = (ordered + ordered)[start:start + _MAX_TOUCHES_PER_TICK]
        _touch_cursor = start + _MAX_TOUCHES_PER_TICK
    try:
        return {m["conditionId"]: m for m in await term.marked_summaries(ordered)}
    except Exception as e:  # noqa: BLE001 — no read, no fill, try again next tick
        logger.debug("paper fill: market read failed: %s", e)
        return {}


async def _settle_resolved() -> int:
    """Pay out paper positions in markets that have actually resolved, in
    every paper account."""
    n = 0
    for book in books_in_use():
        with using_book(book):
            n += await _settle_resolved_book()
    return n


async def _settle_resolved_book() -> int:
    """Settle the CURRENT paper account.

    Written as a synthetic sell at exactly 100¢ or 0¢ — the settlement value on
    a split (split_resolution) — with order_type='SETTLE',
    so the average-cost and cash maths stay on one path. Orders still resting
    on a resolved market are canceled: there is no book left to fill them,
    and left resting a buy held its cash and a sell kept claiming contracts
    the settlement already paid out.
    """
    pos, _, _ = _book_state()
    open_keys = [(cid, side) for (cid, side), st in pos.items()
                 if st["size"] > 1e-9 and not st["settled"]]
    resting = _resting_markets()
    if not open_keys and not resting:
        return 0

    markets = {}
    try:
        cids = {cid for cid, _ in open_keys} | resting
        for m in await term._summaries_for(sorted(cids)):
            markets[m["conditionId"]] = m
    except Exception as e:  # noqa: BLE001
        logger.debug("paper settle lookup failed: %s", e)
        return 0

    n = 0
    for cid, side in open_keys:
        m = markets.get(cid)
        settle_cents = payout_cents(m, side)
        if m is None or settle_cents is None:
            continue
        size = pos[(cid, side)]["size"]
        _record({
            "condition_id": cid, "title": m.get("title") or "", "side": side,
            "action": "sell", "order_type": "SETTLE",
            "requested_size": size, "requested_price_cents": settle_cents,
            "filled_size": size, "avg_fill_price_cents": settle_cents,
            "resting_size": 0.0,
            # Settlement is not a trade: no fee.
            "fees_usd": 0.0,
            "cost_usd": round(size * settle_cents / 100.0, 4),
            "status": "settled",
            "note": (
                f"Market resolved {_resolution_word(m)} — this "
                f"side paid {settle_cents:g}¢ per contract. Exact, not a mark."),
            "filled_at": _now_iso(),
        })
        n += 1

    for cid in resting:
        m = markets.get(cid)
        if not m or payout_cents(m, "yes") is None:
            continue
        with _conn() as conn:
            conn.execute(
                """UPDATE terminal_paper_orders
                   SET status='canceled', resting_size=0,
                       note = TRIM(COALESCE(note,'') || ?)
                   WHERE condition_id=? AND COALESCE(book,'manual')=?
                     AND status IN ('resting','partial') AND resting_size > 0""",
                (f" Canceled: the market resolved "
                 f"{_resolution_word(m)} while this was resting.",
                 cid, current_book()))
    return n


def _resting_markets() -> set[str]:
    """conditionIds the CURRENT account has any resting order in."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT condition_id FROM terminal_paper_orders "
            "WHERE status IN ('resting','partial') AND resting_size > 0 "
            "AND COALESCE(book,'manual')=?", (current_book(),)).fetchall()
    return {str(r[0]) for r in rows}


# ────────────────────────────────────────────────────────────────────────
# Reading the paper account
# ────────────────────────────────────────────────────────────────────────


async def portfolio(cfg: Optional[dict] = None) -> dict:
    pos, cash_delta, fees_known = _book_state()
    bankroll = _bankroll(cfg)

    open_keys = [(k, st) for k, st in pos.items() if st["size"] > 1e-9]
    marks: dict[str, dict] = {}
    if open_keys:
        try:
            for m in await term.marked_summaries([cid for (cid, _), _ in open_keys]):
                marks[m["conditionId"]] = m
        except Exception as e:  # noqa: BLE001
            logger.debug("paper portfolio mark lookup failed: %s", e)

    positions = []
    tot_val = 0.0
    tot_unreal = 0.0
    any_unmarked = False
    for (cid, side), st in sorted(pos.items(), key=lambda kv: -kv[1]["size"]):
        if st["size"] <= 1e-9:
            continue
        m = marks.get(cid)
        resolved = (m or {}).get("resolvedOutcome")
        payout = payout_cents(m, side)
        if payout is not None:
            # Decided (or split): the price IS the payout.
            mark = payout
            stamp = {"source": "gateway", "observedAt": _now_iso(), "ageSec": 0.0}
        else:
            p = (m or {}).get("probability")
            mark = p if side == "yes" else (None if p is None else round(100.0 - p, 2))
            stamp = ((m or {}).get("sources") or {}).get("probability")
        # Fee-inclusive: what each contract really cost, i.e. its break-even.
        avg = round(st["cost"] / st["size"] * 100.0, 3) if st["size"] > 0 else None
        exact_val = st["size"] * mark / 100.0 if mark is not None else None
        value = round(exact_val, 2) if exact_val is not None else None
        # value - cost, not size x (mark - rounded avg): the totals below must
        # tie out to the cash to the cent.
        pnl = round(exact_val - st["cost"], 2) if exact_val is not None else None
        if exact_val is not None:
            tot_val += exact_val
            tot_unreal += exact_val - st["cost"]
        else:
            any_unmarked = True
        positions.append({
            "conditionId": cid, "venue": "polymarket",
            "title": (m or {}).get("title") or st["title"] or None,
            "slug": (m or {}).get("slug"),
            "side": side, "size": round(st["size"], 2),
            "avgCostCents": avg,
            "costBasisUsd": round(st["cost"], 2),
            "markCents": mark, "valueUsd": value,
            "unrealizedPnlUsd": pnl,
            "realizedPnlUsd": round(st["realized"], 2),
            "settledPnlUsd": None,
            "status": (m or {}).get("status") or "unknown",
            "resolvedOutcome": resolved,
            "closeTime": (m or {}).get("closeTime"),
            "settled": payout is not None,
            # Settlement pays automatically, here and on the venue.
            "redeemable": False,
            "unreconciled": False, "unreconciledSize": None,
            "origin": "ai" if is_agent_book(current_book()) else "manual",
            # Where the mark really came from: the live read, or the listing's
            # quote with its age when that read failed.
            "sources": ({"probability": stamp} if (mark is not None and stamp) else {}),
            "unknowns": ([] if mark is not None else [{
                "field": "markCents",
                "reason": (
                    "No live price for this market right now, so this paper "
                    "position cannot be valued. Unknown, not zero."),
            }]),
        })

    realized = round(sum(st["realized"] for st in pos.values()), 2)
    cash = round(bankroll + cash_delta, 2)
    # Every total covers EVERY open position or it is unknown. A position
    # nobody can price used to drop out of the sums silently, which read as
    # "worth its cost" (zero P&L) - a number nobody measured. With nothing
    # open, the open P&L is exactly zero and equity is the cash.
    totals_known = not any_unmarked

    note = None
    if not fees_known:
        note = (
            "At least one paper fill happened in a market whose fee rate we "
            "could not read, so the totals below EXCLUDE those fees. The live "
            "ticket warns about the same markets.")

    return {
        "positions": positions,
        # The paper ledger is a local table: reading it either works or
        # raises. It is never degraded to an empty list, so an empty
        # `positions` here always means "you hold nothing".
        "walletReadable": True,
        "totalValueUsd": round(tot_val, 2) if totals_known else None,
        # Fee-inclusive, like each position's costBasisUsd.
        "totalCostUsd": round(sum(st["cost"] for st in pos.values()), 2),
        # realized + unrealized == equity - bankroll: both net of every fee.
        "totalUnrealizedPnlUsd": round(tot_unreal, 2) if totals_known else None,
        "realizedPnlUsd": realized,
        # Paper settles through a synthetic SELL row, so its settlement P&L is
        # already inside `realized` — there is no separate bucket to report.
        "settledPnlUsd": None,
        "cashUsd": cash,
        "bankrollUsd": bankroll,
        # The only number that answers "would I have made money?".
        "equityUsd": round(bankroll + cash_delta + tot_val, 2) if totals_known else None,
        "returnPct": (
            round((cash_delta + tot_val) / bankroll * 100.0, 2)
            if totals_known and bankroll > 0 else None),
        "unreconciledCount": 0,
        "unreconciledNote": note,
        "redeemableCount": 0, "redeemableValueUsd": None,
        "redeemableNote": None,
        "fetchedAt": _now_iso(),
        "costBasisConvention": "average",
    }


async def day_pnl(today: str) -> dict:
    """Today's (UTC date `today`, "YYYY-MM-DD") P&L of the CURRENT paper
    account, for the agents' daily loss stop.

    Same fee convention as `portfolio` (see _book_state): a buy's fee is in
    the position's cost, a sell's fee comes off that sell. Realised: closes
    and settlements filled today against that fee-inclusive average cost,
    less every sell fee paid today. Unrealised: open positions at the price
    they could be SOLD at now — the bid, not the mid — minus their cost, fee
    included (an entry fee is an open loss until the position earns it back);
    a position nobody bids (or whose market could not be read) at its whole
    cost, because the stop must not fail open exactly when the book goes
    quiet. For a book whose whole history is today, realised + unrealised is
    exactly the portfolio's realised + unrealised, marked at the bid."""
    pos: dict[tuple[str, str], dict] = {}
    realized = 0.0
    for r in _rows():
        key = (str(r["condition_id"]), str(r["side"]))
        st = pos.setdefault(key, {"size": 0.0, "cost": 0.0, "settled": False})
        sz = float(r["filled_size"] or 0.0)
        px = float(r["avg_fill_price_cents"] or 0.0)
        fee = float(r["fees_usd"] or 0.0)
        is_today = str(r["filled_at"] or r["created_at"] or "")[:10] == today
        if str(r["action"]) == "buy":
            st["size"] += sz
            # Capitalised, exactly as _book_state does. Charging it to today's
            # realised as well would count it twice.
            st["cost"] += sz * px / 100.0 + fee
        else:
            avg = (st["cost"] / st["size"]) if st["size"] > 0 else None
            close = min(sz, st["size"])
            if avg is not None and close > 0:
                if is_today:
                    realized += close * (px / 100.0 - avg)
                st["cost"] -= close * avg
                st["size"] -= close
            if is_today:
                realized -= fee
            if str(r["order_type"]) == "SETTLE":
                st["settled"] = True

    open_keys = [(k, st) for k, st in pos.items() if st["size"] > 1e-9]
    marks: dict[str, dict] = {}
    if open_keys:
        try:
            for m in await term.marked_summaries([cid for (cid, _), _ in open_keys]):
                marks[m["conditionId"]] = m
        except Exception as e:  # noqa: BLE001 — unread marks count at full cost
            logger.debug("paper day_pnl mark lookup failed: %s", e)
    unreal = open_loss = unmarked_cost = 0.0
    unmarked: list[str] = []
    for (cid, side), st in open_keys:
        m = marks.get(cid) or {}
        payout = payout_cents(m, side)
        if payout is not None:
            # Resolved, so this is a result and not a mark. Counted as an open
            # loss, a winner could offset nothing, as on the live side.
            realized += st["size"] * payout / 100.0 - st["cost"]
            continue
        if side == "yes":
            bid = num(m.get("bid"))
        else:
            ask = num(m.get("ask"))
            bid = None if ask is None else 100.0 - ask
        if bid is None:
            unmarked.append(cid)
            unmarked_cost += st["cost"]
        else:
            u = st["size"] * bid / 100.0 - st["cost"]
            unreal += u
            open_loss += max(0.0, -u)
    return {"realizedUsd": round(realized, 4), "unrealizedUsd": round(unreal, 4),
            "openLossUsd": round(open_loss, 4),
            "unmarked": unmarked, "unmarkedCostUsd": round(unmarked_cost, 4)}


def reserved_buy_usd() -> float:
    """Cash the CURRENT account's resting paper buys would still spend if they
    filled. A resting order is not a position, but it is a claim on the cash:
    without this, ten resting buys that each fit the balance could all fill."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(resting_size * requested_price_cents / 100.0), 0) "
            "FROM terminal_paper_orders WHERE action='buy' "
            "AND status IN ('resting','partial') AND COALESCE(book,'manual')=?",
            (current_book(),)).fetchone()
    return float(row[0] or 0.0)


def _resting_buys() -> dict[str, float]:
    """conditionId -> contracts the CURRENT account's resting buys could
    still add."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT condition_id, COALESCE(SUM(resting_size), 0) "
            "FROM terminal_paper_orders WHERE action='buy' "
            "AND status IN ('resting','partial') AND COALESCE(book,'manual')=? "
            "GROUP BY condition_id", (current_book(),)).fetchall()
    return {str(r[0]): float(r[1]) for r in rows if float(r[1] or 0.0) > 1e-9}


def open_markets() -> set[str]:
    """conditionIds the CURRENT paper account holds a position in or has a
    resting buy in. For the agents' position caps: a resting buy is a
    position it can still get, and the live caps count it the same way —
    otherwise any number of resting buys pass the cap and all fill later."""
    pos, _, _ = _book_state()
    return ({cid for (cid, _), st in pos.items() if st["size"] > 1e-9}
            | set(_resting_buys()))


def exposure(condition_id: str) -> float:
    """Contracts the CURRENT account has in one market, both sides, or could
    still get there: what it holds before any resting sell (shares are held
    until a sell FILLS) plus what its resting buys could add. For the agents'
    per-market cap, so it errs high like the live count (mcp_rails.live_exposure)."""
    pos, _, _ = _book_state()
    held = sum(st["size"] for (cid, _), st in pos.items() if cid == condition_id)
    return max(0.0, held) + _resting_buys().get(condition_id, 0.0)


def held(condition_id: str, side: str) -> float:
    """Contracts the CURRENT paper account may sell (net of resting sells)."""
    return _held(condition_id, side)


def orders(limit: int = 100) -> dict:
    limit = max(1, min(int(limit or 100), 500))
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM terminal_paper_orders "
            "WHERE COALESCE(book,'manual')=? ORDER BY created_at DESC "
            "LIMIT ?", (current_book(), limit)).fetchall()
    return {
        "orders": [{
            "localId": int(r["id"]),
            "conditionId": str(r["condition_id"]),
            "title": r["title"] or None,
            "side": str(r["side"]), "action": str(r["action"]),
            "orderType": str(r["order_type"]),
            "requestedSize": float(r["requested_size"]),
            "requestedPriceCents": float(r["requested_price_cents"]),
            "filledSize": float(r["filled_size"] or 0.0),
            "avgFillPriceCents": (float(r["avg_fill_price_cents"])
                                  if r["avg_fill_price_cents"] is not None else None),
            "restingSize": float(r["resting_size"] or 0.0),
            "feesUsd": (float(r["fees_usd"]) if r["fees_usd"] is not None else None),
            "costUsd": (float(r["cost_usd"]) if r["cost_usd"] is not None else None),
            "status": str(r["status"]),
            "note": r["note"] or None,
            "createdAt": str(r["created_at"]),
            "filledAt": r["filled_at"] or None,
        } for r in rows],
        "fetchedAt": _now_iso(),
    }


def reset() -> dict:
    """Wipe the paper account back to the starting bankroll."""
    with _conn() as conn:
        book = current_book()
        n = int(conn.execute(
            "SELECT COUNT(*) FROM terminal_paper_orders "
            "WHERE COALESCE(book,'manual')=?", (book,)).fetchone()[0])
        conn.execute("DELETE FROM terminal_paper_orders "
                     "WHERE COALESCE(book,'manual')=?", (book,))
    return {"cleared": n,
            "message": f"Paper account reset — {n} simulated order(s) cleared."}
