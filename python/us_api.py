"""Pure translation between Polymarket US payloads and this app's shapes.

No I/O here, so every mapping that money depends on is unit-tested from real
response samples. `polymarket_api` does the network; the engines above it keep
reading the dict shapes they always have.

How the venue differs from what the engines assume, and how that is bridged:

  * One instrument per market, not a YES token and a NO token. Buying NO is the
    short side of the same book. Engines still think in `yes_token`/`no_token`,
    so each side gets a synthetic token `<slug>#yes` / `<slug>#no`.
  * The book is quoted on the YES side only. A NO quote is the mirror image:
    NO bid = 1 - YES ask, NO ask = 1 - YES bid.
  * Order prices are YES-side ("when you place an order, the price always refers
    to the YES side", docs concepts/orders). Engines pass the price of the side
    they hold, so a NO price is mirrored on the way out and back in. All of it
    goes through `wire_price` / `side_price`, the one place that convention
    lives.

Verified live against the public gateway on 2026-11-05: market, event, BBO, book,
settlement and index-price payloads. The authenticated shapes (orders, positions,
balances, private WS) come from the docs and the official SDK's type definitions
and have NOT been exercised against the real API yet; see docs/US_PORT.md.
"""
from __future__ import annotations

import math
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

GATEWAY = "https://gateway.polymarket.us"     # public market data
API_BASE = "https://api.polymarket.us"        # signed trading + portfolio
WS_BASE = "wss://api.polymarket.us"           # both feeds need signed headers
WS_MARKETS_PATH = "/v1/ws/markets"
WS_PRIVATE_PATH = "/v1/ws/private"

CURRENCY = "USD"
# Fee = theta x contracts x p x (1 - p), taker only at the base maker tier
# (docs fees.md, effective 2026-10-28). Each market also stamps feeCoefficient.
DEFAULT_TAKER_THETA = 0.0695

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")

_TIF = {
    "GTC": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
    "FAK": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
    "FOK": "TIME_IN_FORCE_FILL_OR_KILL",
}

_INTENT = {
    ("yes", "buy"): "ORDER_INTENT_BUY_LONG",
    ("yes", "sell"): "ORDER_INTENT_SELL_LONG",
    ("no", "buy"): "ORDER_INTENT_BUY_SHORT",
    ("no", "sell"): "ORDER_INTENT_SELL_SHORT",
}

_CATEGORY = {
    "crypto": "crypto", "sports": "sports", "politics": "politics",
    "finance": "economics", "economics": "economics",
    "culture": "entertainment", "weather": "climate", "climate": "climate",
    "macro": "economics", "tech": "world", "technology": "world",
    "science": "world", "world": "world", "geopolitics": "world",
}

_LIVE_STATES = {
    "ORDER_STATE_NEW", "ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_REPLACE",
    "ORDER_STATE_PENDING_CANCEL", "ORDER_STATE_PENDING_RISK",
    "ORDER_STATE_PARTIALLY_FILLED", "ORDER_STATE_REPLACED",
}
_DEAD_STATES = {"ORDER_STATE_CANCELED", "ORDER_STATE_EXPIRED", "ORDER_STATE_REJECTED"}


def valid_slug(slug: Any) -> bool:
    """A slug goes straight into a URL path, so it must not carry separators."""
    return isinstance(slug, str) and bool(_SLUG_RE.match(slug))


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def amount(v: Any) -> Optional[float]:
    """The numeric part of an Amount ({value, currency}) or a bare number."""
    if isinstance(v, dict):
        v = v.get("value")
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money(price: float) -> dict:
    """An Amount with no float noise: 0.55, not 0.55000000000000004."""
    return {"value": f"{price:.4f}".rstrip("0").rstrip(".") or "0", "currency": CURRENCY}


# ───────── sides and prices ─────────────────


def token(slug: str, side: str) -> str:
    return f"{slug}#{side.lower()}"


def parse_token(tok: Any) -> tuple[str, str]:
    """(slug, side) of a synthetic token; a bare slug reads as YES."""
    s = str(tok or "")
    slug, _, side = s.partition("#")
    return slug, ("no" if side == "no" else "yes")


def order_intent(side: str, action: str) -> str:
    return _INTENT[(side.lower(), action.lower())]


def order_tif(order_type: str) -> str:
    return _TIF[order_type.upper()]


def wire_price(side: str, price: float) -> float:
    """The price of `side` as the exchange wants it: on the YES side."""
    p = 1.0 - price if side.lower() == "no" else price
    return round(p, 4)


def side_price(side: str, yes_price: Optional[float]) -> Optional[float]:
    """A YES-side price as seen from `side`."""
    if yes_price is None:
        return None
    return round(1.0 - yes_price, 4) if side.lower() == "no" else round(yes_price, 4)


def intent_side(intent: Any) -> str:
    return "no" if str(intent or "").endswith("_SHORT") else "yes"


def intent_action(intent: Any) -> str:
    return "sell" if "_SELL_" in str(intent or "") else "buy"


def yes_book_direction(intent: Any) -> str:
    """'buy' or 'sell' of the one instrument. Buying NO sells YES."""
    side, action = intent_side(intent), intent_action(intent)
    return action if side == "yes" else ("sell" if action == "buy" else "buy")


def quote_for_side(side: str, yes_bid: Optional[float],
                   yes_ask: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """(bid, ask) of `side` from the YES top of book."""
    if side.lower() == "no":
        return (side_price("no", yes_ask), side_price("no", yes_bid))
    return yes_bid, yes_ask


def crosses_own_orders(direction: str, yes_limit: float, open_orders: list[dict]) -> Optional[dict]:
    """The first of our own resting orders that this order would trade against,
    or None. `direction` is buy/sell of the YES instrument; `yes_limit` its
    YES-side price. Rulebook 5.x(q) prohibits matching your own orders."""
    for o in open_orders or []:
        px = amount(o.get("price"))
        if px is None:
            continue
        other = yes_book_direction(o.get("intent"))
        if other == direction:
            continue
        if (direction == "buy" and px <= yes_limit) or (direction == "sell" and px >= yes_limit):
            return o
    return None


# ───────── markets ──────────────────────────


def _json_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v:
        try:
            out = json.loads(v)
            return out if isinstance(out, list) else []
        except ValueError:
            return []
    return []


def fee_schedule(raw: dict) -> Optional[dict]:
    """Same descriptor the fee code reads: fee per share = rate x p(1-p),
    takers only. Maker rebates start at $10M of monthly maker volume, so the
    base tier is modeled as zero."""
    # null is "not stated", not "free": None sends the caller to its fallback rate.
    if not isinstance(raw, dict) or raw.get("feeCoefficient") is None:
        return None
    rate = to_float(raw.get("feeCoefficient"))
    if rate <= 0:
        return {"enabled": False}
    return {"enabled": True, "rate": rate, "exponent": 1.0, "rebateRate": 0.0}


def _settlement_of(raw: dict, prices: list) -> tuple[str, Optional[float]]:
    """(result, payout of YES) once the venue says the market is final. Only
    MARKET_STATUS_RESOLVED is final: RESOLVING is "past expiry, final settlement
    not yet published", and its prices are not an outcome."""
    if str(raw.get("status") or "") != "MARKET_STATUS_RESOLVED" or len(prices) < 2:
        return "", None
    yes = to_float(prices[0], -1.0)
    no = to_float(prices[1], -1.0)
    if yes == 1.0 and no == 0.0:
        return "yes", 1.0
    if yes == 0.0 and no == 1.0:
        return "no", 0.0
    # Dead heats and rule-based splits settle between 0 and 1.
    if 0.0 <= yes <= 1.0:
        return "", yes
    return "", None


def market_sides(raw: dict) -> tuple[Optional[dict], Optional[dict]]:
    """(YES side, NO side) of a raw market, read off the `long` flag: the only thing
    that says which side is which. `outcomes` can't: live rows ship `["No","Yes"]`
    while their prices stay YES-first."""
    yes_side: Optional[dict] = None
    no_side: Optional[dict] = None
    for s in raw.get("marketSides") or []:
        if not isinstance(s, dict):
            continue
        if s.get("long") is True and yes_side is None:
            yes_side = s
        elif s.get("long") is False and no_side is None:
            no_side = s
    return yes_side, no_side


def outcome_prices(raw: dict) -> list:
    """[YES, NO] when both sides are priced, read off marketSides. The docs deprecate
    `outcomePrices` in favour of marketSides, and the two agreed on all 541 fully priced
    rows checked on 2026-11-05. The old field is only read when the sides don't say."""
    yes_side, no_side = market_sides(raw)
    yes, no = (yes_side or {}).get("price"), (no_side or {}).get("price")
    if yes not in (None, "") and no not in (None, ""):
        return [yes, no]
    return _json_list(raw.get("outcomePrices"))


def _indicative_prices(raw: dict, prices: list) -> tuple[float, float]:
    """(YES, NO) prices for when there is no quote. The sides say which is which. When
    only one is priced, `outcomePrices` collapses to one entry holding THAT side's price,
    YES on 258 of 259 such rows and NO on the other (2026-11-05), so it can't say alone."""
    yes_side, no_side = market_sides(raw)
    yes = amount((yes_side or {}).get("price"))
    no = amount((no_side or {}).get("price"))
    if yes is not None or no is not None:
        return (yes if yes is not None else 1.0 - no,
                no if no is not None else 1.0 - yes)
    if len(prices) >= 2:
        return to_float(prices[0]), to_float(prices[1])
    if prices:
        no = to_float(prices[0])
        return 1.0 - no, no
    return 0.0, 1.0


def _outcome_labels(raw: dict, outcomes: list) -> list:
    """Outcome names, YES first, whatever order `outcomes` arrived in."""
    yes_side, no_side = market_sides(raw)
    labels = [str((s or {}).get("description") or "") for s in (yes_side, no_side)]
    if all(labels):
        return labels
    listed = [str(o) for o in outcomes]
    return ["Yes", "No"] if set(listed) == {"Yes", "No"} else listed


def normalize_market(raw: Any, *, event_slug: str = "") -> Optional[tuple[dict, dict]]:
    """(market dict the engines read, trading meta), or None when unusable."""
    if not isinstance(raw, dict):
        return None
    slug = raw.get("slug")
    if not valid_slug(slug):
        return None
    outcomes = _outcome_labels(raw, _json_list(raw.get("outcomes")))
    prices = outcome_prices(raw)
    bid = amount(raw.get("bestBidQuote"))
    ask = amount(raw.get("bestAskQuote"))
    yes_price, no_price = _indicative_prices(raw, prices)
    if bid is None and ask is None:
        bid = ask = yes_price
    elif bid is None:
        bid = ask
    elif ask is None:
        ask = bid
    mid = (bid + ask) / 2.0 if (bid or ask) else yes_price

    result, settlement = _settlement_of(raw, prices)
    state = str(raw.get("status") or "")
    if state == "MARKET_STATUS_RESOLVED":
        status = "settled"
    elif raw.get("closed") or state == "MARKET_STATUS_RESOLVING":
        status = "closed"
    elif state == "MARKET_STATUS_HALTED" or not raw.get("active", True) or raw.get("archived"):
        status = "inactive"
    else:
        status = "open"

    terms = raw.get("assetPriceTerms") if isinstance(raw.get("assetPriceTerms"), dict) else {}
    tick = to_float(raw.get("orderPriceMinTickSize"), 0.01) or 0.01
    min_size = to_float(raw.get("minimumTradeQty"), 1.0) or 1.0
    title = raw.get("question") or raw.get("title") or slug
    category = _CATEGORY.get(str(raw.get("category") or "").lower(), "")
    meta = {
        "yes_token": token(slug, "yes"),
        "no_token": token(slug, "no"),
        "neg_risk": False,
        "tick_size": tick,
        "min_size": min_size,
        "slug": slug,
        "event_slug": event_slug,
        "question": title,
        "outcomes": outcomes,
        "version": "",
    }
    market = {
        "ticker": slug,
        "condition_id": slug,
        "event_ticker": event_slug,
        "series_ticker": "",
        "title": title,
        "yes_sub_title": outcomes[0] if outcomes else "",
        "category": category,
        "status": status,
        "close_time": raw.get("endDate") or "",
        "slug": slug,
        "neg_risk": False,
        "yes_token": meta["yes_token"],
        "no_token": meta["no_token"],
        "tick_size": tick,
        "version": "",
        "fee_schedule": fee_schedule(raw),
        # The market list carries no volume; BBO has it per market (sharesTraded).
        "volume_fp": 0.0,
        "volume_24h_fp": 0.0,
        "open_interest_fp": 0.0,
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "last_price_dollars": mid,
        "no_price_dollars": no_price,
        "result": result,
        "settlement_value_dollars": settlement,
        "price_to_beat": amount(terms.get("priceToBeat")),
        "settlement_price": amount(terms.get("settlementPrice")),
        "index_symbol": terms.get("indexSymbol") or "",
        "min_size": min_size,
    }
    return market, meta


def parse_iso_epoch(v: Any) -> Optional[int]:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        # Trim sub-second digits fromisoformat can't read on older Pythons.
        s = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1), s)
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp())
    except (TypeError, ValueError):
        return None


def updown_event_slug(asset: str, horizon: str, window_start: int) -> str:
    """btc-updown-15m-2026-11-05-0015z. Market slugs differ per asset
    (cpc-btc-…, cpceth-eth-…), event slugs don't, so discovery goes via events."""
    stamp = datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%Y-%m-%d-%H%M")
    return f"{asset.lower()}-updown-{horizon}-{stamp}z"


# ───────── books and quotes ─────────────────


def _levels(rows: Any) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        px = amount(row.get("px"))
        qty = to_float(row.get("qty"), -1.0)
        if px is not None and qty > 0:
            out.append((px, qty))
    return out


def parse_book(market_data: Any) -> dict:
    """{'bids': [(px, qty)] best first, 'asks': [...]} of the YES book."""
    md = market_data if isinstance(market_data, dict) else {}
    return {
        "bids": sorted(_levels(md.get("bids")), key=lambda r: -r[0]),
        "asks": sorted(_levels(md.get("offers")), key=lambda r: r[0]),
        "state": md.get("state") or "",
    }


def book_for_side(book: dict, side: str) -> dict:
    """The book seen from `side`: NO bids are the mirror of YES asks."""
    if side.lower() != "no":
        return {"bids": list(book.get("bids") or []), "asks": list(book.get("asks") or [])}
    return {
        "bids": [(round(1.0 - p, 4), q) for p, q in (book.get("asks") or [])],
        "asks": [(round(1.0 - p, 4), q) for p, q in (book.get("bids") or [])],
    }


def bid_cents(price: Optional[float]) -> Optional[int]:
    """A bid in whole cents, rounded DOWN. Sub-cent ticks exist (0.1c): rounding to
    nearest could put a marketable sell above the bid it means to hit."""
    return None if price is None else int(math.floor(price * 100 + 1e-9))


def ask_cents(price: Optional[float]) -> Optional[int]:
    """An ask in whole cents, rounded UP, for the same reason."""
    return None if price is None else int(math.ceil(price * 100 - 1e-9))


def parse_market_quote(market: Any) -> tuple[Optional[float], Optional[float]]:
    """(yes_bid, yes_ask) off a market object from GET /v1/market/slug/{slug}."""
    m = market if isinstance(market, dict) else {}
    return amount(m.get("bestBidQuote")), amount(m.get("bestAskQuote"))


def parse_bbo(market_data: Any) -> tuple[Optional[float], Optional[float]]:
    md = market_data if isinstance(market_data, dict) else {}
    return amount(md.get("bestBid")), amount(md.get("bestAsk"))


# ───────── orders ───────────────────────────


def order_body(*, slug: str, side: str, action: str, count: float, price: float,
               order_type: str, maker_only: bool = False) -> dict:
    """A limit order. Every order this app sends carries a price cap, including
    the marketable ones, which go as limit + IOC/FOK, so a thin book can never
    fill past the caller's price. manualOrderIndicator is AUTOMATIC: this is
    software placing the order."""
    body: dict = {
        "marketSlug": slug,
        "intent": order_intent(side, action),
        "type": "ORDER_TYPE_LIMIT",
        "price": money(wire_price(side, price)),
        "quantity": count,
        "tif": order_tif(order_type),
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        # Wait briefly for the matching engine's answer, so a fill is known here
        # rather than a poll later.
        "synchronousExecution": True,
        "maxBlockTime": "2",
    }
    if maker_only:
        body["participateDontInitiate"] = True
    return body


def _state_status(state: Any) -> str:
    s = str(state or "")
    if s == "ORDER_STATE_FILLED":
        return "matched"
    if s in _DEAD_STATES:
        return "canceled"
    if s in _LIVE_STATES:
        return "live"
    return s.lower() or "live"


def order_view(order: Any) -> dict:
    """A venue Order in the fields the engines parse (`size_matched`,
    `original_size`, `price` of OUR side, `status`). `price` is the average fill
    once anything filled, so cost = size_matched x price holds."""
    o = order if isinstance(order, dict) else {}
    side = intent_side(o.get("intent"))
    filled = to_float(o.get("cumQuantity"))
    avg = amount(o.get("avgPx"))
    limit = amount(o.get("price"))
    px = avg if (filled > 0 and avg) else limit
    return {
        "id": str(o.get("id") or ""),
        "status": _state_status(o.get("state")),
        "size_matched": filled,
        "original_size": to_float(o.get("quantity")),
        "price": side_price(side, px) if px is not None else 0.0,
        "side": side,
        "action": intent_action(o.get("intent")),
        "market": o.get("marketSlug") or "",
        "fee_usd": amount(o.get("commissionNotionalTotalCollected")) or 0.0,
        "created_at": o.get("createTime") or o.get("insertTime") or "",
    }


def parse_place_response(resp: Any, order_type: str) -> dict:
    """{order_id, status, filled, reject} from POST /v1/orders ({id, executions}).
    The newest execution carries the order's state. `reject` is the venue's
    reason when it refused the order outright."""
    r = resp if isinstance(resp, dict) else {}
    oid = str(r.get("id") or "")
    execs = [e for e in (r.get("executions") or []) if isinstance(e, dict)]
    last = execs[-1] if execs else {}
    order = last.get("order") if isinstance(last.get("order"), dict) else {}
    if str(last.get("type") or "") == "EXECUTION_TYPE_REJECTED" or \
            str(order.get("state") or "") == "ORDER_STATE_REJECTED":
        reason = last.get("text") or last.get("orderRejectReason") or "order rejected"
        return {"order_id": "", "status": "rejected", "filled": 0.0, "reject": str(reason)}
    if not order:
        # Accepted, no answer from the engine inside maxBlockTime: poll by id.
        return {"order_id": oid, "status": "live", "filled": 0.0, "reject": ""}
    view = order_view(order)
    status = view["status"]
    filled = view["size_matched"]
    # A killed marketable order with nothing filled is a clean no-fill, reported
    # with no id: callers take their no-fill path rather than poll it.
    if order_type.upper() in ("FAK", "FOK") and status == "canceled" and filled <= 0:
        return {"order_id": "", "status": "unmatched", "filled": 0.0, "reject": ""}
    return {"order_id": oid or view["id"], "status": status, "filled": filled, "reject": ""}


def is_no_liquidity(msg: Any) -> bool:
    s = str(msg or "").lower()
    return "no match" in s or "no liquidity" in s or "would not match" in s


# ───────── account ──────────────────────────


def position_row(slug: str, p: Any) -> Optional[dict]:
    """A venue position in the shape the reconcilers read: signed `position_fp`,
    YES positive and NO negative. The docs don't state the sign of netPosition
    ("long or short"); negative meaning NO is inferred, and is the first thing
    the live check confirms."""
    if not isinstance(p, dict):
        return None
    net = to_float(p.get("netPositionDecimal"), to_float(p.get("netPosition")))
    if net == 0:
        return None
    meta = p.get("marketMetadata") if isinstance(p.get("marketMetadata"), dict) else {}
    size = abs(net)
    cost = amount(p.get("cost"))
    value = amount(p.get("cashValue"))
    realized = amount(p.get("realized")) or 0.0
    # cost and cash value are cash, so per-share figures don't depend on which
    # side the venue quotes them on.
    # A figure the venue didn't send stays None: a 0.0 here reads as a free entry
    # or a worthless holding.
    avg = abs(cost) / size if cost is not None else None
    cur = abs(value) / size if value is not None else None
    index = 0 if net > 0 else 1
    return {
        "ticker": slug,
        "event_ticker": meta.get("eventSlug") or "",
        "title": meta.get("title") or slug,
        "category": "",
        "position_fp": net,
        "market_exposure_dollars": abs(cost) if cost is not None else None,
        "fees_paid_dollars": amount(p.get("fees")) or 0.0,
        # Settlement pays out automatically; there is nothing to redeem.
        "redeemable": False,
        "cur_price": cur,
        "_raw": {
            "conditionId": slug,
            "slug": slug,
            "title": meta.get("title") or slug,
            "outcome": "Yes" if index == 0 else "No",
            "outcomeIndex": index,
            "size": size,
            "avgPrice": avg,
            "curPrice": cur,
            "initialValue": abs(cost) if cost is not None else None,
            "currentValue": abs(value) if value is not None else None,
            "cashPnl": (abs(value) - abs(cost)) if (value is not None and cost is not None) else 0.0,
            "realizedPnl": realized,
            "expired": bool(p.get("expired")),
        },
    }


def positions_value(rows: list[dict]) -> float:
    """What open positions (position_row) are worth in USD, at the venue's own cash
    value. A row without one counts at its cost: the money is still in the position,
    and leaving it out would read as a loss of all of it. An expired row is left out,
    because its payout arrives as cash."""
    total = 0.0
    for row in rows:
        raw = row.get("_raw") or {}
        if raw.get("expired"):
            continue
        value = raw.get("currentValue")
        total += float(value if value is not None else raw.get("initialValue") or 0.0)
    return total


def balance_view(data: Any) -> Optional[dict]:
    """{cash, portfolio} in USD from GET /v1/account/balances. Spendable cash is
    buyingPower: open orders already reserve part of currentBalance. None when
    the reply has no USD row, so a read failure never looks like a $0 balance."""
    rows = (data or {}).get("balances") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    usd = next((b for b in rows if isinstance(b, dict)
                and str(b.get("currency") or CURRENCY).upper() == CURRENCY), None)
    if usd is None or ("buyingPower" not in usd and "currentBalance" not in usd):
        return None
    cash = to_float(usd.get("buyingPower"), to_float(usd.get("currentBalance")))
    return {"cash": cash, "portfolio": to_float(usd.get("assetNotional"))}


def balance_detail(data: Any) -> dict:
    """What the wallet shows beside the totals, from the same reply. A field the
    reply doesn't carry stays None rather than reading as $0."""
    rows = (data or {}).get("balances") if isinstance(data, dict) else None
    usd = next((b for b in rows if isinstance(b, dict)
                and str(b.get("currency") or CURRENCY).upper() == CURRENCY), None) if isinstance(rows, list) else None
    if usd is None:
        return {"openOrdersUsd": None, "pendingWithdrawalCount": 0, "pendingWithdrawalsUsd": None}
    pending = [w for w in (usd.get("pendingWithdrawals") or []) if isinstance(w, dict)]
    amounts = [amount(w.get("balance")) for w in pending]
    return {
        "openOrdersUsd": amount(usd.get("openOrders")),
        "pendingWithdrawalCount": len(pending),
        # Unknown as a whole if any row has no amount: a partial sum would understate it.
        "pendingWithdrawalsUsd": sum(a for a in amounts if a is not None)
        if pending and all(a is not None for a in amounts) else None,
    }


def activity_row(a: Any) -> Optional[dict]:
    """A venue activity in the fields the reconcilers read. A TRADE stays a
    TRADE; a POSITION_RESOLUTION reads as a REDEEM, what settlement was on the
    old venue. Funding rows carry no market and are dropped."""
    if not isinstance(a, dict):
        return None
    t = str(a.get("type") or "")
    if t == "ACTIVITY_TYPE_TRADE" and isinstance(a.get("trade"), dict):
        tr = a["trade"]
        # The exchange voided it: it never moved money or a position.
        if str(tr.get("state") or "") in ("TRADE_STATE_BUSTED", "TRADE_STATE_REJECTED"):
            return None
        qty = abs(to_float(tr.get("qtyDecimal"), to_float(tr.get("qty"))))
        px = amount(tr.get("price")) or 0.0
        return {
            "type": "TRADE",
            "id": str(tr.get("id") or ""),
            "conditionId": tr.get("marketSlug") or "",
            "size": qty,
            "price": px,
            "usdcSize": qty * px,
            "timestamp": parse_iso_epoch(tr.get("createTime")) or 0,
            "realizedPnl": amount(tr.get("realizedPnl")) or 0.0,
            "isAggressor": bool(tr.get("isAggressor")),
        }
    if t == "ACTIVITY_TYPE_POSITION_RESOLUTION" and isinstance(a.get("positionResolution"), dict):
        r = a["positionResolution"]
        before = r.get("beforePosition") if isinstance(r.get("beforePosition"), dict) else {}
        net = to_float(before.get("netPositionDecimal"), to_float(before.get("netPosition")))
        return {
            "type": "REDEEM",
            "id": str(r.get("tradeId") or ""),
            "conditionId": r.get("marketSlug") or "",
            "size": abs(net),
            "outcomeIndex": 0 if net >= 0 else 1,
            "timestamp": parse_iso_epoch(r.get("updateTime")) or 0,
        }
    return None
