"""quote(): a script's resting bids on both sides of a crypto window, simulated.

decide() buys one side once. Making a market is a different shape: a bid on Up
and a bid on Down at the same time, re-priced as the book moves, held only while
the inventory stays inside a limit. When both sides fill for less than 100c
together, the pair pays $1 whichever way the window settles.

SHADOW ONLY, on purpose. The live test of 2026-11-07/08 found the resting bids it
placed filled when the price was falling through them and then lost. Before real
orders are cancelled and replaced every few seconds, a script has to show a
profit on fills of exactly that kind. So quote() never reaches the exchange: an
armed script's quote() is skipped, and the heartbeat says so.

THE FILL RULE. A simulated bid at P on a side fills when that side's best OFFER
is later seen at or below P. That is certain: a real resting bid would have been
matched by that offer. It is also pessimistic. A real bid at the touch is also
hit by ordinary sellers while the price stands still, and nothing in a snapshot
of the book proves that, so those fills are not counted. What is counted is the
fills where the market came through the bid, the adverse ones. A script that is
green here is green on its worst fills.

A bid placed on one tick can fill on a later tick, never the same one. A fill is
a maker fill: no fee. It is written to script_shadow_orders with order_type
POST, so the shadow ledger, its settlement, the daily loss stop and the Scripts
page all see it like any other shadow order.

THE RAILS, none of which the script can widen:
  * post-only: a bid at or above the side's offer is not placed;
  * no offer read on a side, no bid on it;
  * script_max_entry_cents and script_max_contracts, as for every script order;
  * script_quote_max_inventory: contracts held on one side of one window;
  * the exchange's minimum order (contracts and $1), never met by going over a cap.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable, Optional

import crypto15m_trader
import db

logger = logging.getLogger("krypt.script_quotes")

SIDES = ("up", "down")
ORDER_TYPE = "POST"
DEFAULT_MAX_INVENTORY = 20

# (script id, ticker) -> {"up": bid|None, "down": bid|None, "seen": epoch}
# bid = {"price": cents, "size": contracts, "since": epoch, "reason": str}
# In memory only: a restart cancels every simulated bid, as it would real ones.
_working: dict[tuple[str, str], dict] = {}


def max_inventory(cfg: dict) -> int:
    v = cfg.get("script_quote_max_inventory")
    return max(1, int(v)) if v else DEFAULT_MAX_INVENTORY


def sanitize(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    """quote() returns None (no bids on this window) or
    {"up": cents|None, "down": cents|None, "size": contracts, "reason": str}."""
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        return None, f"quote() returned {type(raw).__name__}, expected None or a dict"
    out: dict = {}
    for side in SIDES:
        v = raw.get(side)
        if v is None:
            out[side] = None
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not (1 <= v <= 99):
            return None, f"quote()[{side!r}] must be an integer from 1 to 99 (cents) or None"
        out[side] = v
    size = raw.get("size")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        return None, "quote()['size'] must be a positive integer"
    out["size"] = size
    out["reason"] = str(raw.get("reason") or "")[:200]
    return out, None


def _cents(frac: Any) -> Optional[int]:
    """A 0..1 offer in whole cents, rounded up: the price that would be paid."""
    try:
        f = float(frac)
    except (TypeError, ValueError):
        return None
    if not (0.0 < f < 1.0):
        return None
    return int(math.ceil(f * 100 - 1e-6))


def inventory(conn, sid: str, ticker: str, env: str) -> dict:
    """Contracts this script's simulated bids have bought on each side of a window."""
    out = {"up": 0, "down": 0, "upCostCents": None, "downCostCents": None}
    for r in conn.execute(
            "SELECT side, SUM(contracts) AS n, SUM(contracts * entry_cents) AS cost "
            "FROM script_shadow_orders WHERE script_id=? AND ticker=? AND network=? "
            "AND order_type=? AND contracts > 0 GROUP BY side", (sid, ticker, env, ORDER_TYPE)):
        side = str(r["side"])
        if side in SIDES and r["n"]:
            out[side] = int(r["n"])
            out[f"{side}CostCents"] = round(float(r["cost"]) / float(r["n"]), 2)
    return out


def sized(price: int, want: Optional[int], ask: Optional[int], held: int, cap: int,
          cfg: dict) -> tuple[int, Optional[str]]:
    """(contracts, None) for a bid that may be placed, or (0, why not)."""
    max_cents = int(cfg.get("script_max_entry_cents") or 97)
    if price > max_cents:
        return 0, f"{price}c is over script_max_entry_cents {max_cents}c"
    if ask is None:
        return 0, "no live offer on this side, so nothing says the bid would rest"
    if price >= ask:
        return 0, f"post-only: {price}c would trade right away against the {ask}c offer"
    room = min(max(1, int(cfg.get("script_max_contracts") or 20)), cap - held)
    if room <= 0:
        return 0, f"holding {held} on this side, at the inventory limit of {cap}"
    size = min(int(want or max(1, int(cfg.get("crypto15m_order_size", 1) or 1))), room)
    # The exchange's floors. Met only inside the caps: refused, never rounded up past one.
    need = max(crypto15m_trader._C15_MIN_CONTRACTS, -(-100 // max(1, price)))
    if need > room:
        return 0, (f"the smallest order here is {need} contracts at {price}c and only "
                   f"{room} fit under your limits")
    return max(size, need), None


def forget(sid: str) -> None:
    for key in [k for k in _working if k[0] == sid]:
        del _working[key]


def drop_ended(sid: str, live_tickers: set) -> None:
    """Bids on a window that is no longer listed went with it."""
    for key in [k for k in _working if k[0] == sid and k[1] not in live_tickers]:
        del _working[key]


def _view(bid: Optional[dict], now: float) -> Optional[dict]:
    if not bid:
        return None
    return {"priceCents": bid["price"], "size": bid["size"],
            "ageSec": round(now - bid["since"], 1)}


def run_asset(sid: str, a: dict, cfg: dict, env: str, hook: Callable[[dict], Any],
              note: Callable[[str], None], portfolio: Optional[dict] = None) -> Optional[str]:
    """One tick of one script on one window: book the fills, ask the script what
    it wants resting, and make the simulated book match. Returns why the hook's
    answer was malformed, or None. Whatever the hook raises is the caller's."""
    ticker = str(a.get("ticker") or "")
    now = time.time()
    asks = {"up": _cents(a.get("upAsk")), "down": _cents(a.get("downAsk"))}
    stale_after = max(60.0, 3.0 * float(cfg.get("script_poll_sec") or 5))
    st = _working.get((sid, ticker))
    if st is None or now - st["seen"] > stale_after:
        # Nothing watched these bids for a while: they did not rest through it.
        st = _working[(sid, ticker)] = {"up": None, "down": None, "seen": now}
    st["seen"] = now

    for side in SIDES:
        bid = st[side]
        if bid and asks[side] is not None and asks[side] <= bid["price"]:
            with db.get_db() as conn:
                db.insert_script_shadow(conn, {
                    "script_id": sid, "source": "crypto", "asset": a.get("asset"),
                    "ticker": ticker, "side": side, "contracts": bid["size"],
                    "entry_cents": bid["price"], "order_type": ORDER_TYPE,
                    "reason": bid["reason"],
                    "note": (f"simulated maker fill: the {side} offer came down to "
                             f"{asks[side]}c after {now - bid['since']:.0f}s resting"),
                    "close_time": a.get("closeTime") or "", "network": env,
                })
            st[side] = None
            note("quote_fills")
            logger.info(f"[scripts] {sid[:8]} SHADOW quote filled {a.get('asset')} {side} "
                        f"x{bid['size']} @ {bid['price']}c (offer {asks[side]}c)")

    cap = max_inventory(cfg)
    with db.get_db() as conn:
        inv = inventory(conn, sid, ticker, env)
    ctx = dict(a)
    ctx["portfolio"] = portfolio or {}
    ctx["quotes"] = {
        "up": _view(st["up"], now), "down": _view(st["down"], now),
        "inventory": {"up": inv["up"], "down": inv["down"]},
        "avgCostCents": {"up": inv["upCostCents"], "down": inv["downCostCents"]},
        "maxInventory": cap,
    }
    note("quote")
    want, err = sanitize(hook(ctx))
    if err:
        return err

    for side in SIDES:
        price = want.get(side) if want else None
        cur = st[side]
        size, why = (0, None) if price is None else sized(
            price, want.get("size"), asks[side], inv[side], cap, cfg)
        if price is None or why:
            if cur:
                st[side] = None
                note("quote_cancels")
            if why:
                note("quote_declined")
                logger.debug(f"[scripts] {sid[:8]} quote {a.get('asset')} {side}: {why}")
            continue
        if cur and cur["price"] == price and cur["size"] == size:
            continue
        st[side] = {"price": price, "size": size, "since": now,
                    "reason": want.get("reason") or ""}
        note("quote_moves" if cur else "quote_places")
    return None
