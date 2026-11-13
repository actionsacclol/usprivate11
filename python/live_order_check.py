"""Live check of the Polymarket US order path, on the owner's own API key.

Answers what the docs and mocks can't:

  * the key is accepted, and balances, positions and open orders read
  * both WebSockets connect with our signature and deliver parseable frames
  * the exchange reads a NO order's price on the YES side (docs: "the price always
    refers to the YES side"). Seen on 2026-11-04, so the app buys NO without this
    check. If a run ever sees the opposite, it locks buying NO on this machine.
  * with --with-fill: real small fills, how they read back, and the sign of
    netPosition (undocumented; the app assumes negative means NO)

Deliberately NOT part of `npm run py:test`, and never run by CI. Run it BY HAND:

    npm run py:orders -- --dry-run      # no key, nothing signed or sent
    npm run py:orders                   # your key. Places one post-only order and
                                        # cancels it. Cost: $0
    npm run py:orders -- --with-fill    # also buys and sells the minimum size of
                                        # YES and NO. Cost: spreads + taker fees

Close the app first: it reads the result when it starts.

How the NO check costs nothing: the probe is post-only, at a price that rests under
either reading of the price, and at a level that was empty, on a slower market. Then
it only has to look at which side of the public book the order appears on.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true", help="throwaway profile, no key, nothing sent")
ap.add_argument("--with-fill", action="store_true", help="also place real small fills (real money)")
ap.add_argument("--max-usd", type=float, default=2.0, help="cap on any one fill (default $2)")
ap.add_argument("--userdata", help="app data folder, if not the default")
args = ap.parse_args()

scratch = tempfile.mkdtemp(prefix="krypt-us-ordercheck-")
os.environ["KRYPT_POLYBOT_US_VAULT"] = os.path.join(scratch, "vault")  # no DB here, but never the real one
if args.dry_run:
    os.environ["KRYPT_POLYBOT_US_USERDATA"] = os.path.join(scratch, "userdata")
else:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    if sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    os.environ["KRYPT_POLYBOT_US_USERDATA"] = args.userdata or os.path.join(base, "Krypt PolyBot US")

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clob_ws  # noqa: E402
import polymarket_api as api  # noqa: E402
import polymarket_auth as auth  # noqa: E402
import us_api as us  # noqa: E402
import user_ws  # noqa: E402

MCP_PORT = 47635
PROBE_MARGIN = 0.04
BOOK_POLL_SEC = 10
BOOK_POLL_MAX_SEC = 90     # the public book is cached for up to 30s

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def note(msg: str) -> None:
    print(f"        {msg}")


def app_is_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", MCP_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def probe_price(bid: float, ask: float, book: dict, tick: float) -> float | None:
    """A YES-side price that rests whichever way the exchange reads it, at a level
    empty on both sides. Read as YES it's an offer above the ask; read as the NO
    price it's a NO bid under the NO ask (1 - bid). None if there's no such price."""
    taken = {round(p, 4) for p, _ in book.get("bids") or []} | {round(p, 4) for p, _ in book.get("asks") or []}
    y = round(ask + PROBE_MARGIN, 4)
    top = round(1.0 - bid - PROBE_MARGIN, 4)
    while y <= top:
        if abs(y - (1.0 - y)) > tick and y not in taken and round(1.0 - y, 4) not in taken:
            return y
        y = round(y + tick, 4)
    return None


def depth_at(rows, price: float) -> float:
    return sum(q for p, q in rows or [] if abs(p - price) < 1e-6)


async def pick_probe_market() -> tuple[dict, dict, float] | None:
    """A slow market: not a short up/down window, its YES bid under 40c (so the two
    readings of the price land far apart), and with a free level to probe."""
    markets = await api.fetch_all_open_markets(max_pages=3)
    candidates = [m for m in markets
                  if "updown" not in (m.get("slug") or "") and m.get("status") == "open"
                  and float(m.get("min_size") or 1) <= 2]
    print(f"        {len(candidates)} candidate markets")
    for m in candidates[:150]:
        slug = m["slug"]
        bid, ask, _age = await api._read_bbo(slug)
        if bid is None or ask is None or not (0.08 <= bid <= 0.40) or ask - bid > 0.06:
            continue
        book = await api._read_book(slug)
        y = probe_price(bid, ask, book or {}, float(m.get("tick_size") or 0.01)) if book else None
        if y is not None:
            return m, book, y
    return None


async def check_key() -> None:
    print("\n1. The API key")
    if not auth.credentials_present():
        bad("no API key saved. Add it on the API Key page in the app, then run this again.")
        return
    note(f"key id {auth.get_key_id()}")
    try:
        bal = await api.get_balance()
        ok(f"balance read: ${bal['balance'] / 100:.2f} spendable, ${bal['portfolio_value'] / 100:.2f} in positions")
        ok(f"positions read: {len(await api.get_positions())} open")
        ok(f"open orders read: {len(await api.get_open_orders())} resting")
    except api.PolymarketAPIError as e:
        bad(f"HTTP {e.status}: {e.body}")


async def check_feeds(slug: str) -> None:
    print("\n2. WebSockets")
    user_ws.feed.start()
    tok = us.token(slug, "yes")
    clob_ws.feed.observe(tok)
    clob_ws.feed.start()
    deadline = time.monotonic() + 20
    quote = None
    while time.monotonic() < deadline and not (quote and user_ws.feed.connected):
        quote = clob_ws.feed.get_quote_cents(tok)
        await asyncio.sleep(0.5)
    if quote:
        ok(f"markets socket: live quote {quote} on {slug}")
    else:
        bad(f"markets socket: no quote in 20s ({clob_ws.feed.last_error or 'connected, no frames'})")
    if user_ws.feed.connected:
        ok("private socket: subscribed to our orders")
    else:
        bad(f"private socket: not connected ({user_ws.feed.last_error or 'no reason given'})")


async def check_no_side(market: dict, book: dict, y: float) -> None:
    """Rest one post-only BUY_SHORT, see where it lands in the public book, cancel."""
    print("\n3. How the exchange reads a NO order's price")
    slug = market["slug"]
    count = max(1, int(float(market.get("min_size") or 1) + 0.999))
    note(f"market {slug}, size {count}, YES-side price {y} on the wire")
    body = us.order_body(slug=slug, side="no", action="buy", count=count, price=round(1.0 - y, 4),
                         order_type="GTC", maker_only=True)
    if body["price"]["value"] != f"{y:.4f}".rstrip("0").rstrip("."):
        bad(f"order mapping sent {body['price']} for {y}; not sending it")
        return
    try:
        preview = await api._authed_request("POST", "/v1/order/preview", json_body={"request": body})
        note("preview: " + json.dumps(preview, default=str)[:400])
    except api.PolymarketAPIError as e:
        note(f"preview failed, HTTP {e.status}: {e.body}")
    before = await api._read_book(slug) or {}

    try:
        resp = await api._authed_request("POST", "/v1/orders", json_body=body, idempotent=False)
    except api.PolymarketAPIError as e:
        bad(f"probe order refused, HTTP {e.status}: {e.body}")
        return
    parsed = us.parse_place_response(resp, "GTC")
    oid = parsed["order_id"]
    if not oid or parsed["reject"] or parsed["filled"] or parsed["status"] != "live":
        bad(f"probe did not rest as expected: {parsed}; raw {json.dumps(resp, default=str)[:300]}")
        if oid:
            await api.cancel_order(oid, market=slug)
        return
    ok(f"probe resting as {oid}")
    verdict = "undetermined"
    try:
        order = (await api._authed_request("GET", f"/v1/order/{oid}")).get("order") or {}
        note(f"exchange's copy: intent={order.get('intent')} price={order.get('price')} state={order.get('state')}")
        started = time.monotonic()
        while time.monotonic() - started < BOOK_POLL_MAX_SEC:
            await asyncio.sleep(BOOK_POLL_SEC)
            now = await api._read_book(slug) or {}
            # Read as YES: selling YES at y, a new offer at y.
            as_yes = depth_at(now.get("asks"), y) - depth_at(before.get("asks"), y)
            # Read as the NO price: paying y for NO, which is a YES bid at 1-y.
            as_no = depth_at(now.get("bids"), 1.0 - y) - depth_at(before.get("bids"), 1.0 - y)
            note(f"{int(time.monotonic() - started)}s: offers at {y} +{as_yes:g}, "
                 f"bids at {round(1.0 - y, 4)} +{as_no:g}")
            if as_yes >= count and as_no < count:
                verdict = "yes-side"
                break
            if as_no >= count and as_yes < count:
                verdict = "no-side"
                break
    finally:
        await api.cancel_order(oid, market=slug)
        left = [o for o in await api.get_open_orders([slug]) if str(o.get("id")) == oid]
        if left:
            bad(f"probe {oid} still open after cancel. Cancel it on polymarket.us.")
        else:
            ok("probe cancelled")

    if verdict == "yes-side":
        api.set_no_side_mismatch(False)
        ok("NO orders are priced on the YES side, as the app sends them.")
    elif verdict == "no-side":
        api.set_no_side_mismatch(True)
        bad("NO orders are priced on the NO side. us_api.wire_price is wrong for this API: "
            "fix it before trading. Buying NO is now locked on this machine.")
    else:
        bad("couldn't tell where the probe landed (a busy level, or the book didn't update). "
            "Nothing was changed. Run it again.")


async def net_position(slug: str) -> tuple[float | None, int | None]:
    for row in await api.get_positions():
        if row["ticker"] == slug:
            return float(row["position_fp"]), int(row["_raw"]["outcomeIndex"])
    return None, None


async def flatten(slug: str, side: str, count: int) -> bool:
    """Sell it back at the bid. FAK with a real price, so a thin book can't hurt."""
    for _ in range(3):
        q = await api.get_quote(slug, side)
        if not q.get("bid_cents"):
            await asyncio.sleep(3)
            continue
        res = await api.place_limit_order(ticker=slug, side=side, action="sell", count=count,
                                          price_cents=q["bid_cents"], order_type="FAK")
        await asyncio.sleep(3)
        if (await net_position(slug))[0] is None:
            ok(f"closed: {res['order']['status']}")
            return True
    bad(f"{count} {side.upper()} on {slug} is STILL OPEN. Close it on polymarket.us.")
    return False


async def check_fill(side: str) -> None:
    print(f"\n{'4' if side == 'yes' else '5'}. A real fill: buy and sell {side.upper()}")
    markets = await api.fetch_crypto_updown("btc", "hourly")
    cur = [m for m in markets if m["status"] == "open" and m["window_close_epoch"] - time.time() > 600]
    if not cur:
        bad("no open BTC hourly window with 10 minutes left; try again later")
        return
    slug = cur[0]["slug"]
    count = max(1, int(float(cur[0].get("min_size") or 1) + 0.999))
    ask = (await api.get_quote(slug, side)).get("ask_cents")
    if not ask or ask * count / 100.0 > args.max_usd:
        bad(f"{side} ask {ask}c x{count} is over the ${args.max_usd} cap, or missing")
        return
    if (await net_position(slug))[0] is not None:
        bad(f"you already hold {slug}; not touching it")
        return
    res = await api.place_limit_order(ticker=slug, side=side, action="buy", count=count,
                                      price_cents=ask, order_type="FAK")
    note(f"{slug}: bought at up to {ask}c, reply {res['order']}")
    await asyncio.sleep(3)
    net, index = await net_position(slug)
    if net is None:
        note("no position shows: nothing filled (the 30s-old quote moved). Not a failure; run again.")
        return
    want = 0 if side == "yes" else 1
    if index == want:
        ok(f"position reads back as {side.upper()}: netPosition {net:g}")
    else:
        bad(f"bought {side.upper()} but netPosition {net:g} reads as the other side. "
            "us_api.position_row has the sign wrong; don't trade until it's fixed.")
    if await flatten(slug, side, int(abs(net))) and index != want:
        note("closed anyway")


async def main() -> int:
    print(f"Polymarket US order check ({'dry run' if args.dry_run else 'LIVE'})")
    print(f"profile: {os.environ['KRYPT_POLYBOT_US_USERDATA']}")
    if not args.dry_run and app_is_running():
        print("The app seems to be running (its port is open). Close it first.")
        return 2
    auth.verify_signer()
    ok("request signing works offline")

    picked = await pick_probe_market()
    if not picked:
        bad("no market suitable for the probe right now")
        return 1
    market, book, y = picked
    if args.dry_run:
        print("\nWould probe:")
        note(f"market {market['slug']}: {market['title']}")
        note(json.dumps(us.order_body(slug=market["slug"], side="no", action="buy", count=1,
                                      price=round(1.0 - y, 4), order_type="GTC", maker_only=True)))
        print("\nDry run: nothing signed or sent.")
        return 0

    try:
        await check_key()
        if failures:
            return 1
        await check_feeds(market["slug"])
        # The order path's own guards still apply to the fills below.
        api.load_venue_state()
        if api.kill_switch_status():
            bad(f"the app's kill switch is on: {api.kill_switch_status()}")
            return 1
        await check_no_side(market, book, y)
        if args.with_fill:
            await check_fill("yes")
            if not api.no_buy_locked():
                await check_fill("no")
            else:
                note("skipping the NO fill: buying NO is locked")
    finally:
        await user_ws.feed.stop()
        await clob_ws.feed.stop()
        await api.close_clients()

    print()
    if failures:
        print(f"{len(failures)} problem(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
