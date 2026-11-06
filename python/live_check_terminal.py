"""Live provider check for the manual terminal.

Deliberately NOT part of `npm run py:test`: it is slow and it depends on
somebody else's uptime, so a red run here should never block CI. Run it BY
HAND after touching anything in terminal.py / terminal_trade.py, because this
is the layer that catches what unit tests structurally cannot — a route that
vanished, a response shape that changed, a rate limit nobody documented.

    npm run py:live

Read-only. It never authenticates and never places an order.
"""
from __future__ import annotations

import asyncio
import sys
import time

import terminal as t

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results: list[tuple[str, str, str]] = []


def note(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))
    tag = {PASS: "  OK  ", FAIL: " FAIL ", WARN: " WARN "}[status]
    print(f"[{tag}] {name}" + (f"  -> {detail}" if detail else ""))


async def check_discover() -> str | None:
    """Every column must return rows AND be honest about what it filtered."""
    first_cid = None
    for column in ("volume", "closing", "new", "volatility"):
        started = time.monotonic()
        try:
            r = await t.discover(column, limit=10)
        except Exception as e:  # noqa: BLE001
            note(f"discover:{column}", FAIL, f"{type(e).__name__}: {e}")
            continue
        ms = (time.monotonic() - started) * 1000
        rows = r["rows"]
        if not rows:
            note(f"discover:{column}", WARN, r.get("note") or "empty")
            continue
        # Every row must carry a title and an id; anything else may honestly
        # be None, and a column where EVERY row is unpriced is a red flag
        # worth surfacing rather than a silent empty state.
        bad = [m for m in rows if not m.get("conditionId") or not m.get("title")]
        priced = [m for m in rows if m.get("probability") is not None]
        if bad:
            note(f"discover:{column}", FAIL, f"{len(bad)} row(s) missing id/title")
        elif not priced and column != "new":
            note(f"discover:{column}", WARN, "no row carried a price")
        elif not priced:
            # EXPECTED on 'new': a market listed seconds ago has no book, so
            # an unknown price is the correct answer rather than a gap in our
            # data. This branch exists because the check was wrong the first
            # time it ran and flagged honest behaviour as a defect.
            note(f"discover:{column}", PASS,
                 f"{len(rows)} rows, none priced yet (expected for just-listed), {ms:.0f}ms")
        else:
            note(f"discover:{column}", PASS,
                 f"{len(rows)} rows, {len(priced)} priced, {ms:.0f}ms")
        # Whatever the column, an unpriced row must NOT carry a fabricated
        # spread. This is the empty-book sentinel bug, guarded live.
        lying = [m for m in rows
                 if m.get("probability") is None and m.get("spread") is not None]
        if lying:
            note(f"discover:{column}:no-fake-spread", FAIL,
                 f"{len(lying)} unpriced row(s) still reported a spread")
        if first_cid is None:
            first_cid = rows[0]["conditionId"]
    return first_cid


async def check_market(cid: str) -> None:
    m = await t.market(cid)
    if not m:
        note("market", FAIL, "no market returned")
        return
    note("market", PASS, f"{m['title'][:56]} · {m['probability']}c")

    # Provenance must actually be populated — an unstamped row means the merge
    # silently stopped recording where numbers come from.
    if not m.get("sources"):
        note("market:provenance", FAIL, "no per-field sources recorded")
    else:
        note("market:provenance", PASS,
             ", ".join(f"{k}={v['source']}" for k, v in list(m["sources"].items())[:4]))


async def check_book(cid: str) -> None:
    b = await t.book(cid, "yes")
    if not b["available"]:
        note("book", WARN, b.get("note") or "unavailable")
        return
    # One book, quoted on YES. NO is its mirror, and any other answer is a bug
    # that prices every NO order wrong.
    n = await t.book(cid, "no")
    if (b["bestAsk"] is not None and n["bestBid"] is not None
            and abs(round(100 - b["bestAsk"], 4) - n["bestBid"]) > 1e-6):
        note("book:no-mirror", FAIL, f"YES ask {b['bestAsk']} vs NO bid {n['bestBid']}")
    else:
        note("book:no-mirror", PASS, f"NO {n['bestBid']}/{n['bestAsk']}")
    # Ascending asks / descending bids, and no crossed book.
    asks = [l["priceCents"] for l in b["asks"]]
    bids = [l["priceCents"] for l in b["bids"]]
    if asks != sorted(asks):
        note("book:asks-sorted", FAIL, str(asks[:6]))
    elif bids != sorted(bids, reverse=True):
        note("book:bids-sorted", FAIL, str(bids[:6]))
    elif (b["bestBid"] is not None and b["bestAsk"] is not None
          and b["bestBid"] >= b["bestAsk"]):
        note("book:crossed", FAIL, f"bid {b['bestBid']} >= ask {b['bestAsk']}")
    else:
        note("book", PASS,
             f"{len(bids)}x{len(asks)} levels, {b['spreadCents']}c wide")


async def check_history(cid: str) -> None:
    for iv in ("1h", "1d", "max"):
        h = await t.price_history(cid, "yes", iv)
        pts = h["points"]
        if not pts:
            note(f"history:{iv}", WARN, h.get("note") or "no points")
            continue
        ts = [p["t"] for p in pts]
        if ts != sorted(ts) or len(set(ts)) != len(ts):
            # This is the one that silently breaks every charting library.
            note(f"history:{iv}", FAIL, "series not strictly ascending")
            continue
        out_of_range = [p for p in pts if not (0 <= p["p"] <= 100)]
        if out_of_range:
            note(f"history:{iv}", FAIL,
                 f"{len(out_of_range)} point(s) outside 0..100 — scaling is wrong")
            continue
        note(f"history:{iv}", PASS,
             f"{len(pts)} pts, {min(p['p'] for p in pts):.1f}..{max(p['p'] for p in pts):.1f}c"
             + (" [own feed]" if h["ownFeed"] else ""))


async def check_tape(cid: str) -> None:
    """The venue publishes no trades. The tape must say that, not look quiet."""
    tp = await t.tape(cid, 20)
    if tp["trades"]:
        note("tape", WARN, f"{len(tp['trades'])} trades: is there a public tape now?")
    elif not tp.get("note"):
        note("tape", FAIL, "an empty tape with no explanation reads as a dead market")
    else:
        note("tape", PASS, tp["note"][:70])


async def check_resolution(cid: str) -> None:
    r = await t.resolution_report(cid)
    if not r["checks"]:
        note("resolution", FAIL, "no checks ran")
        return
    # The score must be withheld below the minimum, and present above it.
    if r["resolvedCount"] < t.MIN_RESOLVED_CHECKS and r["score"] is not None:
        note("resolution:score-gate", FAIL,
             f"printed a score off only {r['resolvedCount']} resolved checks")
    elif r["resolvedCount"] >= t.MIN_RESOLVED_CHECKS and r["score"] is None:
        note("resolution:score-gate", FAIL,
             f"withheld a score despite {r['resolvedCount']} resolved checks")
    else:
        note("resolution:score-gate", PASS, r["scoreNote"])
    unresolved_but_scored = [c for c in r["checks"]
                             if not c["resolved"] and c["level"] != "unknown"]
    if unresolved_but_scored:
        note("resolution:unread-is-not-a-pass", FAIL,
             f"{len(unresolved_but_scored)} unread check(s) carry a verdict")
    else:
        note("resolution:unread-is-not-a-pass", PASS,
             f"{r['resolvedCount']}/{r['totalCount']} resolved")


async def check_kalshi(cid: str | None) -> None:
    """The second venue. Its discovery path is fragile in a specific way — see
    kalshi.series_index() — so this checks the index is usable, not just that
    the host answered."""
    import cross_venue
    import kalshi

    started = time.monotonic()
    try:
        series, ok = await kalshi.series_index()
    except Exception as e:  # noqa: BLE001
        note("kalshi:series", FAIL, f"{type(e).__name__}: {e}")
        return
    secs = time.monotonic() - started
    if not ok or not series:
        note("kalshi:series", FAIL, "series index came back empty")
        return
    if len(series) < 1000:
        # A sharp drop means a category slug changed or a response shape moved.
        note("kalshi:series", WARN, f"only {len(series)} series — expected thousands")
    else:
        note("kalshi:series", PASS, f"{len(series):,} series in {secs:.1f}s")

    titled = [x for x in series if x.get("title")]
    if len(titled) != len(series):
        note("kalshi:series-titles", FAIL,
             f"{len(series) - len(titled)} series with no title (matching runs on titles)")

    # The events route is what matching actually runs on: it carries the
    # subject (the fixture, the strike date) that a bare market row does not,
    # and it is the one that stopped truncating the daily strike ladders.
    ev_rows = await kalshi.events_in_series("KXFEDDECISION")
    with_event = [m for m in ev_rows if m.get("eventTitle")]
    if not ev_rows:
        note("kalshi:events", FAIL, "KXFEDDECISION returned no open events")
    elif len(with_event) != len(ev_rows):
        note("kalshi:events", FAIL,
             f"{len(ev_rows) - len(with_event)} row(s) lost their event context "
             f"— matching runs on the event title")
    else:
        note("kalshi:events", PASS,
             f"{len(ev_rows)} market(s), all carrying their event")

    # A known, liquid series proves the market path and the candle path.
    markets = await kalshi.markets_in_series("KXFEDDECISION")
    priced = [m for m in markets if m.get("probability") is not None]
    if not markets:
        note("kalshi:markets", WARN, "KXFEDDECISION returned no open markets")
        return
    note("kalshi:markets", PASS if priced else WARN,
         f"{len(markets)} market(s), {len(priced)} priced")

    bad = [m for m in priced if not (0 < m["probability"] < 100)]
    if bad:
        note("kalshi:price-range", FAIL,
             f"{len(bad)} price(s) outside 1..99 — the cents/dollars read is wrong")
    else:
        note("kalshi:price-range", PASS, "all quotes inside 1..99¢")

    if priced:
        m = priced[0]
        now = int(time.time())
        pts = await kalshi.candles(
            m["conditionId"], start_ts=now - 86400, end_ts=now,
            period_interval=60, series_ticker=m.get("seriesTicker") or "")
        if not pts:
            note("kalshi:candles", WARN, "no candles for a liquid market")
        else:
            ts = [p["t"] for p in pts]
            if ts != sorted(ts) or len(set(ts)) != len(ts):
                note("kalshi:candles", FAIL, "series not strictly ascending")
            elif any(not (0 <= p["p"] <= 100) for p in pts):
                note("kalshi:candles", FAIL, "candle prices outside 0..100")
            else:
                note("kalshi:candles", PASS, f"{len(pts)} pts")

    # And the guard that matters most: opposite questions must never pair.
    score, _, _ = cross_venue.score_pair(
        {"title": "Will the Fed increase rates by 25 bps", "closeTime": None},
        {"matchText": "Will the Federal Reserve Cut rates by 25bps Cut 25bps"})
    note("kalshi:no-fictional-arb", PASS if score == 0.0 else FAIL,
         "opposite questions score 0" if score == 0.0
         else f"an opposite pairing scored {score}")

    if cid:
        cmp_ = await cross_venue.compare(cid, "1d")
        if cmp_.get("kalshi"):
            conf = cmp_["match"]["confidence"]
            spread = cmp_["spreadCents"]
            if conf != "strong" and spread is not None:
                note("kalshi:spread-gate", FAIL,
                     f"a {conf} match printed a spread of {spread}")
            else:
                note("kalshi:spread-gate", PASS, f"{conf} match, spread {spread}")
        else:
            note("kalshi:compare", PASS, "no counterpart for the sample market")

    await check_kalshi_match_rate()
    await check_kalshi_timed_crypto()


# How many live markets the match-rate sample looks at. Small on purpose: each
# one costs a handful of Kalshi calls, and this is a smoke test of recall, not
# the measurement — that lives in tests/test_cross_venue_eval.py against a
# frozen fixture, because a live number drifts by the hour as fixtures settle.
_SAMPLE = 12


async def check_kalshi_match_rate() -> None:
    """How often a live Polymarket market finds a Kalshi counterpart.

    The owner's report was "it doesn't find matches for most markets", and the
    unit tests structurally cannot catch that: they pin the RULES, and the
    failure was in RETRIEVAL. So this samples the real Discover columns and
    prints the rate, with each pairing's two titles side by side — because the
    only real check on a match is a human reading both.

    There is no FAIL threshold on the rate itself. Most Polymarket questions
    genuinely have no Kalshi counterpart, so a low rate can be perfectly
    honest; what is being watched is a rate that COLLAPSES (retrieval broke)
    and any pairing a reader would not accept.
    """
    import cross_venue

    rows: list[dict] = []
    seen: set[str] = set()
    for column in ("volume", "closing", "volatility"):
        try:
            r = await t.discover(column, limit=12)
        except Exception:  # noqa: BLE001
            continue
        for m in r.get("rows") or []:
            if m.get("conditionId") and m["conditionId"] not in seen:
                seen.add(m["conditionId"])
                rows.append(m)
    rows = rows[:_SAMPLE]
    if not rows:
        note("kalshi:match-rate", WARN, "no live markets to sample")
        return

    started = time.monotonic()
    matched, strong, inverted, failed = [], 0, 0, 0
    for m in rows:
        try:
            hit = await cross_venue.find_match(m)
        except Exception as e:  # noqa: BLE001
            failed += 1
            note("kalshi:match-rate", FAIL, f"{type(e).__name__}: {e}")
            continue
        if not hit:
            continue
        matched.append((m, hit))
        if hit["score"] >= cross_venue.STRONG:
            strong += 1
        if hit.get("side") == "no":
            inverted += 1
    secs = time.monotonic() - started

    if failed:
        return
    note("kalshi:match-rate",
         WARN if not matched else PASS,
         f"{len(matched)}/{len(rows)} sampled markets found a counterpart "
         f"({strong} strong, {inverted} against Kalshi's NO side) in "
         f"{secs:.0f}s")

    # Print both titles. A match is a CLAIM, and the check that matters is a
    # person reading the pair — not a number this script produced.
    for m, hit in matched:
        kx = hit["market"]
        tag = "NO " if hit.get("side") == "no" else ""
        print(f"           PM  {m.get('title', '')[:72]}")
        print(f"           KX  {tag}{(kx.get('eventTitle') or kx.get('title') or '')[:60]}"
              f" / {(kx.get('subtitle') or '')[:34]}"
              f"   [{hit['score']:.2f}]")

    # Every match must still be able to say why it is one.
    unreasoned = [m for m, hit in matched if not hit.get("for")]
    if unreasoned:
        note("kalshi:match-reasons", FAIL,
             f"{len(unreasoned)} match(es) carried no reasoning")
    elif matched:
        note("kalshi:match-reasons", PASS,
             "every match named its reasons and the side it compared")


async def check_kalshi_timed_crypto() -> None:
    """The timed crypto up/down family, end to end through `compare()`.

    This is the family the owner trades, and the one where a near miss is
    hardest to see: Polymarket lists 5- and 15-minute windows, Kalshi lists
    15-minute windows only and opens one at a time, and on a quiet quarter
    hour every one of those prices looks like every other. Two invariants,
    both checkable whatever is open at the time:

      * a reported match must be the SAME window to the second, and
      * a no-match must say which window Kalshi is actually quoting, instead
        of the generic "no market looks like this question".
    """
    import cross_venue

    live: dict[str, dict] = {}
    for column in ("new", "closing", "volatility"):
        try:
            r = await t.discover(column, limit=100)
        except Exception:  # noqa: BLE001
            continue
        for m in r.get("rows") or []:
            up = cross_venue.parse_updown(m)
            if up and m.get("probability") is not None:
                live.setdefault(f"{up['minutes']}:{m['slug']}", m)
    if not live:
        note("kalshi:timed-crypto", WARN, "no timed up/down markets listed now")
        return

    picks: list[dict] = []
    for minutes in (15, 5):
        for key in sorted(live):
            if key.startswith(f"{minutes}:"):
                picks.append(live[key])
                break
    if not picks:
        note("kalshi:timed-crypto", WARN, "no readable timed window to sample")
        return

    matched = generic = bad_window = 0
    for m in picks:
        up = cross_venue.parse_updown(m)
        res = await cross_venue.compare(m["conditionId"], "1d")
        kx = res.get("kalshi")
        if kx:
            # The window end is already on the response — no second fetch, and
            # no chance of racing a window that closes between the two calls.
            kx_end = cross_venue._close_ts(kx.get("closeTime"))
            end = t._hours_to(kx.get("closeTime"))
            same = kx_end is not None and abs(kx_end - up["endTs"]) <= 60
            matched += 1
            if not same:
                bad_window += 1
                note("kalshi:timed-crypto", FAIL,
                     f"{m['title']} paired with {kx['ticker']}, which does not "
                     f"end at the same instant")
            print(f"           {m['title'][:58]}")
            print(f"           -> {kx['ticker']}  pm={res['polymarket']['probability']}"
                  f"  kx={kx['probability']}  spread={res['spreadCents']}"
                  + (f"  ({end:.2f}h to close)" if end is not None else ""))
        else:
            n = res.get("note") or ""
            if n == cross_venue.NO_MATCH_NOTE or not n:
                generic += 1
                note("kalshi:timed-crypto", FAIL,
                     f"{m['title']} fell back to the generic note")
            else:
                print(f"           {m['title'][:58]}")
                print(f"           -> {n[:150]}")
    if not bad_window and not generic:
        note("kalshi:timed-crypto", PASS,
             f"{len(picks)} window(s) checked, {matched} paired to the same "
             f"window, {len(picks) - matched} explained by window length")


async def check_search() -> None:
    r = await t.search("will", limit=5)
    note("search:text", PASS if r["rows"] else WARN,
         f"{len(r['rows'])} hit(s)" + ("" if r["rows"] else f" — {r['note']}"))


async def check_backoff() -> None:
    """Prove a parked host fails FAST instead of holding the queue.

    This is the failure that blanked the chart on the crypto build: a
    discovery burst 429'd the provider, and the doomed retries starved a
    high-value request while a plain curl to the same endpoint returned 200.
    """
    gate = t._gates["gateway"]
    gate.park(2.0)
    started = time.monotonic()
    data, _ = await t._fetch("gateway", "/v1/markets", {"limit": 1}, ttl=0.0)
    elapsed = (time.monotonic() - started) * 1000
    gate.parked_until = 0.0
    if data is not None:
        note("backoff:park", FAIL, "a parked host still made a request")
    elif elapsed > 50:
        note("backoff:park", FAIL, f"parked call took {elapsed:.0f}ms — it queued")
    else:
        note("backoff:park", PASS, f"failed fast in {elapsed:.1f}ms")


async def check_cache() -> None:
    key_before = t._gates["gateway"].calls
    await t._fetch("gateway", "/v1/markets", {"limit": 2}, ttl=30.0)
    mid = t._gates["gateway"].calls
    await t._fetch("gateway", "/v1/markets", {"limit": 2}, ttl=30.0)
    after = t._gates["gateway"].calls
    if mid == key_before:
        note("cache", WARN, "first call was already cached")
    elif after != mid:
        note("cache", FAIL, "identical request went to the network twice")
    else:
        note("cache", PASS, "second identical request served from cache")


async def check_concurrency() -> None:
    """Prove the gate overlaps round trips against the REAL provider.

    The gate's lock covers only the spacing decision — next allowed start,
    sleep the gap, stamp it — and is released before the HTTP call, with a
    semaphore bounding how many are in flight. Held across the round trip (as
    it once was) a Discover load needing ~20 market calls cost 20 × (RTT +
    0.12s). Unit tests pin this with a stub; only this check sees a real RTT.
    """
    gate = t._gates["gateway"]
    n = 6
    before = gate.calls
    # Measure THIS burst's round trips, not the running median. The median is
    # over the last 50 calls to the host, so every check that ran earlier
    # moves it — and when the provider is warm and fast those samples drag the
    # "what serial would have cost" estimate down until a correctly parallel
    # run looks like a failure. The comparison has to be against the RTTs we
    # actually just paid.
    lat_before = len(gate._lat)
    started = time.monotonic()
    await asyncio.gather(*[
        t._fetch("gateway", "/v1/markets", {"limit": 1, "offset": i}, ttl=0.0)
        for i in range(n)
    ])
    elapsed = (time.monotonic() - started) * 1000
    made = gate.calls - before
    burst = sorted(gate._lat[lat_before:])
    rtt = burst[len(burst) // 2] if burst else (gate.latency_ms() or 0.0)
    serial = n * (rtt + gate.min_gap * 1000)   # what it used to cost
    floor = (n - 1) * gate.min_gap * 1000      # the spacing we deliberately keep
    # What overlapping SHOULD cost: the spacing floor, which is deliberate and
    # irreducible, plus ONE round trip rather than n of them. The old bound was
    # a ratio of `serial`, and on a fast link that ratio slid underneath the
    # floor — 6 requests on a 32ms provider cannot finish sooner than 632ms
    # however perfectly they overlap, but 0.75 x serial asked for 685ms and
    # then failed a correct run for taking 736ms. The budget is now absolute,
    # and it still catches the regression it exists for: a gate holding its
    # lock across the round trip pays ~913ms here, well past it.
    budget = floor + rtt + max(150.0, rtt * 2.0)
    if made != n:
        note("concurrency", WARN, f"{made} of {n} requests actually went out")
    elif elapsed > budget:
        note("concurrency", FAIL,
             f"{n} requests took {elapsed:.0f}ms, over a budget of "
             f"{budget:.0f}ms — serial would be ~{serial:.0f}ms, and the "
             f"spacing floor alone is {floor:.0f}ms at rtt {rtt:.0f}ms")
    else:
        note("concurrency", PASS,
             f"{n} requests in {elapsed:.0f}ms (serial ~{serial:.0f}ms, "
             f"min_gap floor {floor:.0f}ms, rtt {rtt:.0f}ms)")


async def main() -> int:
    print("=== terminal live provider check ===\n")
    cid = await check_discover()
    print()
    if cid:
        await check_market(cid)
        await check_book(cid)
        await check_history(cid)
        await check_tape(cid)
        await check_resolution(cid)
    else:
        note("entity checks", FAIL, "no market id to test against")
    print()
    await check_kalshi(cid)
    print()
    await check_search()
    await check_cache()
    await check_concurrency()
    await check_backoff()

    print("\n--- provider cost ---")
    for p in t.health()["providers"]:
        print(f"  {p['host']:<34} calls={p['calls']:<4} errors={p['errors']:<3} "
              f"median={p['latencyMs']}ms  cache {p['cacheHits']}/{p['cacheHits'] + p['cacheMisses']}")

    fails = [r for r in _results if r[1] == FAIL]
    warns = [r for r in _results if r[1] == WARN]
    print(f"\nRESULT: {len(_results) - len(fails) - len(warns)} passed, "
          f"{len(warns)} warned, {len(fails)} FAILED")
    try:
        await t.api.close_clients()
    except Exception:
        pass
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
