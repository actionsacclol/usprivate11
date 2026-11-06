# Krypt PolyBot — Release Audit

**Audited 2026-09-01** against `main` at `4f8404f` plus the 89 uncommitted files.
Companion to `STATE_OF_THE_BOT.md` (2026-08-26), which remains accurate; this document
covers what that one missed. Published copy with the same content:
https://claude.ai/code/artifact/26a9782d-5ff4-4dd4-8584-fe1fe730f399

| | |
|---|---|
| Python tests | **1,040 passing**, 21.9 s (`npm run py:test`) |
| Typecheck | **Clean** (both projects) |
| Version | `2.0.0` in package.json; installer built 2026-08-31 still says 2.0.0; **recommend 3.0.0** |
| Money-path bugs | **7 critical, 14 high**, none caught by the existing suite |
| Preset expectancy | **−2.42¢/contract** whale follow (n = 8,360), all presets negative, zero spread charged |

## AI-agent port: committed; the live agent order is still to run (2026-10-10)

The AI-agent revamp — named agents, money rails, forecast ledger, HTTP API, Autopilot, six AI
providers, the app-wide Paper | Live account, onboarding, the glass shell, Agent Hub — plus Perps
and the 2026 platform fixes are committed on `release-3.0.0`: `2076ffa` (backend), `f7b8544`
(app), the fixes below, then the docs. Verified on the committed tree: full pytest **1,974 passing
on Python 3.11.16 and 3.14**, `--selftest` PASS on both, vitest 27 files / 359, both `tsc`
projects clean.

Two real bugs found on the way, each fixed with tests that fail without the fix:

- **A deeply nested script crashed the backend** (`13752a1`). eth-account 0.14 — what CI and every
  installer it builds resolve — loads `py_ecc`, which sets the process-wide recursion limit to
  100,000. On 3.11, `ast.parse` of one 120 KB line of `1+1+…` then overflowed the C stack: the
  process died with no traceback, from the Scripts page or an agent's `validate_script`. Only the
  3.11 run with a freshly resolved venv saw it (the local 3.14 venv sits on 0.13.7, no `py_ecc`),
  as `test_a_tree_too_deep_to_WALK_is_refused_too` failing in the full run and passing alone. The
  limit is now pinned back to 1,000 before any script is parsed; signing is unaffected (selftest).
- **A new named agent could not connect** (`65d7c73`). Only Default ever got a token on its own;
  a named agent got one only from "New token" or the HTTP snippet. So add an agent → **Add** next
  to Claude Desktop → the relay found no token and said the app was closed. And the relay cached
  its token for life, so a rotation never reached a running client. Tokens are now minted for
  every agent whenever agents are on, and the relay re-reads the token on every call.

**Still to do before shipping agents: one real-money agent order.** Never exercised. Run
`python/live_agent_check.py` by hand with the app closed (`--dry-run` first: 21/21 on paper
against live books). It runs the real backend without Electron and the real stdio relay. A resting
buy far below the bid is approved and then cancelled ($0), a queued order is rejected ($0), and a
minimum-size buy is approved, filled and closed by the agent (spread and fees, about 5–10¢ on the
1¢-tick market the dry run picked). Cleanup restores the saved config and deletes the test agent's
token. The auto-mode classifier refused to start the backend on the real profile from an agent
session, so this one is the owner's to run or to allow. The three items under "Release readiness"
below (live manual order, push and tag, signing) still stand.

## Status — fix pass 1 (2026-09-01, later the same day)

Full suite **1,066 passing** (+26 new tests), typecheck clean, `npm run test:unit` 13/13.
Nothing committed. Fixed in this pass:

- Critical 1, 4, 5, 6, 7: paper gate inside `terminal_trade.place()` and in the standing tick;
  `_exit_was_sell` replaces the `"sell" in "sold"` test (test stubs corrected); `get_positions(strict=True)`
  for the standing tick; stop-loss exempt from the shared exit cooldown and strength/take-profit
  refusals fall through to the stop; paper resting sells reserve inventory, cash credited only for
  contracts held, fill sweep cancels a sell whose position is gone.
- Critical 2, 3: confirmation snapshot in `Market.tsx` (price moved → re-confirm); `usdExact` on
  every committed dollar figure.
- Critical 8 **reconsidered**: paired mode is a tilted bet plus a hedge by design and the existing
  tests pin a 98¢ pair as the canonical entry. The cap stays on price; the refusal text and the
  config comment now state the ~3.5¢ per-pair taker fee instead of calling the hedge free.
- High: 22 missing `DEFAULT_CONFIG` keys added and the preset preserve list widened (`ai*`,
  `remote*`, `terminal*`, lifetime loss limits) with an `e2e/default-config.test.mjs` guard;
  commit-on-blur money fields; header renders "—" when the backend is down; script/chat config
  patches allowlisted in `main.ts` (scripts may only turn live trading off; a paired chat account's
  `/resume` may turn it back on); `/pause` lands in `STATE.cfg` in snake_case; `/flatten` and
  `/cancelall` report what the wallet still holds; Telegram identity is `from.id`; Discord
  heartbeat tears down a dead socket and `_remote_sync` stops it unconditionally.
- Tests: `python/tests/test_release_fixes.py` (new), `test_remote.py` (+13), `test_copy_trader.py`.

## Status — fix pass 2 (2026-09-01)

Full suite **1,148 passing** (+82 over the audit baseline), typecheck clean, `npm run test:unit`
13/13. Nothing committed. Closed in this pass:

- **High:** ticket `costUsd` is now a reachable number (FAK/FOK = crossed slice at the walked
  average; GTC = crossed slice + remainder at the limit; fee/max-loss recomputed); the standing tick
  merges per instruction instead of blind-writing (cancel during a tick stays cancelled); wipes clear
  `script_shadow_orders` and stamp a reconcile floor so fills are not re-imported as external;
  maker/taker attribution fixed at all four sites (script `entry_maker` from the real ask or the
  exchange's `matched`; main engine writes `entry_maker`; copy positions carry the market category
  with `other`=0.05 fallback; crypto re-stamps `FAK` on escalation); the script notional cap refuses
  instead of shrinking below the floor; a timed-out hook's module is dropped and its late writes are
  never persisted.
- **Medium, Python:** cross-venue year filter no longer drops `$2,000`-style prices; Kalshi unit
  reads are field-named (a 1¢ quote survives); AI host lock released on cancellation, SDK clients
  closed, null replies fail honestly; `script_audit` refuses unparseable/unwalkable sources instead
  of raising; `_reconcile_soon` task held strongly; unattributable fill fee is `None`; one shadow
  "stale" rule; `dry_run=0` on crypto stats and the breaker; `idx_whale_time` index.
- **Medium, renderer:** NO-side bid/ask strip; Discover screener no longer rebuilds per keystroke,
  search debounced and sequenced; Track polls only the active tab; logs moved out of the main
  context (`useLogs()`); installing over an armed script requires confirmation; Remote countdown
  updater is pure.
- **Speed:** `get_balance` legs gathered (~100 ms → ~52 ms in the stub timing); one `signal:batch`
  per scan instead of one IPC hop per row; conditional stdout flush.
- **Presets:** UI is the single source; `config.py` mirror trimmed to the same three presets and a
  test fails on the first divergent key; Edge Stack whales aligned to SPORTS (the backend's
  documented decision); `new` badge removed; every description states its measured number; crypto
  preset descriptions and the sniper hint carry the 2026-09-01 re-measurement.
- **Release mechanics:** `package.json` and lock → **3.0.0**; `CHANGELOG.md` `[Unreleased]` cut
  into `[3.0.0] — 2026-09-01` with the duplicated headings merged and this session's fixes added;
  README sandbox box and roadmap bullets corrected; `ci.yml` runs `test:unit` + Vite build;
  `build-installers.yml` gated on a test job.

## Status — fix pass 3, speed and cleanups (2026-09-01)

Full suite **1,271 passing**, typecheck clean, `npm run test:unit` 22/22, `npm run test:e2e`
24/24 (it runs headless on this machine). Nothing committed. Done in this pass:

- **Engine quick wins:** copy wallet reads gathered (5 wallets 255 ms → 65 ms on stubs); open-order
  poll gathered on one DB connection (20 orders 620 ms → 111 ms); crypto manage pass uses one
  market batch per tick and gathers (7 positions 420 ms → 110 ms, zero per-position fetches);
  balance force-refresh once per scan cycle instead of per entry; `get_balance` legs gathered.
- **Master loop:** every block wrapped in a timed `asyncio.timeout` ceiling (60 s, 120 s for market
  sync) with per-block durations on `tradingStatus.loopBlocksMs`; market sync pipelines pages
  (10 live Gamma pages 2,675 ms → 291 ms), upserts run off the loop, and the recurring sync is a
  detached single-flight task; VACUUM gated on `db.is_trading_idle()`. Also restored
  `_fire_and_forget`, which was still called but had been deleted in the working tree (a latent
  `NameError` on crypto ticks).
- **Socket-first:** whale detection runs off `activity_ws` with a shared parser so the synthesised
  `trade_id` dedupes against the 120 s REST backstop (signal age ~120 s → ~1 s); copy entries are
  built from the push and drained before the Data-API read (`copy_trader.on_activity_trade` /
  `drain_pushed_entries`, every existing gate still applied, size anchor left to the poll); order
  paths read `clob_ws` quotes when fresh and sentinel-free before falling back to REST
  (`KRYPT_WS_QUOTES=0` kill switch; book cap 64 → 256); authed startup runs reconcile and balance
  together after cancel-all.
- **Terminal gate:** the host lock covers only request spacing; per-host in-flight semaphore (4 for
  gamma/clob/data, 1 for AI and Kalshi); single-flight dedupe of identical concurrent requests
  (20 Gamma calls 4,847 ms → 2,478 ms, now bound by `min_gap`, a cost-discipline knob).
- **Renderer/Electron:** credentials, signals and positions are push-driven with one 60 s
  reconcile; scanner stats still poll at 8 s (no backend event exists); all polling pauses while
  hidden (`shared/poll.ts`, 9 unit tests); `backgroundThrottling: false` removed and the
  Visualizer pauses its animation loop when hidden; the Discord Rich Presence toggle now works and
  is honoured at startup; `comingSoon` scaffolding and the convergence toggle removed
  (`trade_convergence` key kept — Python reads it).
- **Hygiene:** Python floor raised to 3.11 (`asyncio.timeout`); `scripts/python-utils.mjs` now
  checks the interpreter version and names what it found; `whale_activity_ws` default added.

## Status — fix pass 4, final structural items (2026-09-01)

Full suite **1,334 passing**, typecheck clean, `npm run test:unit` 22/22, `npm run test:e2e`
24/24, `npm run py:live` **23 passed, 0 failed** against the real providers (including the new
gate concurrency check: 6 requests in 843 ms vs ~2.2 s serial). Done in this pass:

- **Wallet-wide flatten.** `terminal_trade.flatten_wallet()` sells every held position as a
  marketable FAK at the best bid through `place()` (every gate applies; paper mode refuses once);
  `flatten_preview()` prices it read-only; `terminalFlattenWallet` RPC; a "Flatten all" control
  with a confirmation panel on Track; `/flatten` from chat is two-step (preview → `/confirm`) and
  runs the main-engine flatten then the wallet flatten; `/cancelall` also cancels terminal resting
  orders.
- **Copy and crypto on their own loops.** `_copy_loop` / `_crypto15m_loop` with their own cadence,
  heartbeat and crash-restart supervision; the watchdog restarts only the stalled loop;
  `loopBlocksMs` namespaced per loop; the activity socket wakes the copy loop directly. A
  cross-engine `trader.ORDER_LOCK` (`order_gate()`) wraps cap-check + place for all four order
  paths with a fresh exposure re-read inside the lock, so two engines can no longer both pass a
  shared cap. Fails open after 30 s, never wedges the order path.
- **DB connections.** `get_db()` reuses one connection per worker thread (1.6 ms → 0.004 ms on
  executor threads). Loop-thread reuse is deliberately still off: a new guard test
  (`tests/test_db_connection_discipline.py`) scans every module for a transaction held open
  across an `await`, and it found two — `scanner.process_whale_trades` and
  `trader.poll_open_orders` — both now fixed (category lookups and expired-order cancels run
  between short transactions). The allow-list is empty; enabling loop-thread reuse is the next
  measurable win (~6 ms/tick) once a cursor-lifetime rule is added to the guard.
- Python floor 3.11 enforced by the setup script.

Not done, by decision: engine paper mode (a product feature, not a release blocker);
shadow-by-default for presets (declined by the author). Line-ending normalisation happens at
commit time via `.gitattributes`.

## Status — Track ledger repair (2026-09-02)

Diagnosed against a copy of the author's real database and the venue's own position and trade
records (read-only). Two root bugs in `terminal_trade.reconcile_fills`, both "outcome identity
read as a word instead of an index":

- **Side from the outcome label.** `"yes" if outcome == "yes" else "no"` is right only on binary
  markets; outcome 0 is "Up", "Over" or a team name elsewhere. 745 of 1,502 fills sat on the
  wrong side key, so buys never met their positions: 55 of 105 positions had no basis and the
  rest were netted across keys. Fixed: side comes from `outcomeIndex` via the token id
  (`_side_from_token`), unknown stays unknown.
- **Maker legs read from the taker's point of view.** `/data/trades` reports each match as the
  taker saw it; our leg as maker is in `maker_orders` (different token, price, sometimes
  direction), and `maker_oids` stringified dicts so our own maker orders never matched a local
  row. Fixed: `_our_leg` picks our half via `trader_side` + `maker_address`; maker fills carry
  the venue-stated $0 fee.
- Reconcile now re-derives existing rows (idempotent; second pass corrects 0) and reports
  `corrected` / `unmapped`. Basis is origin-blind; the venue's `avgPrice` is a
  provenance-stamped fallback. Real wallet after one reconcile: basis 105/105 (was 50/105),
  settled P&L −$633.73 vs the venue's −$633.96 (was −$229.12), per-position mean |Δ| $0.003.
- **Redeemable** now requires the venue flag AND a positive value — Polymarket flags losing
  tokens redeemable too (all 105 badges were on $0.00 positions).
- **"Lost most trades"** was structural, not a bug: the Positions tab is the wallet, a redeemed
  winner leaves it and a loser stays as dust. Settled worthless positions are hidden behind a
  "Show settled (N)" toggle with a note that the wallet is not a win rate.
- **Origin column removed** from Positions and the fill ledger (field kept in the DB).
- Tests: `tests/test_track_real_cases.py` (28, from redacted venue JSON: Up/Down, Over/Under,
  named outcomes, Yes/No, the maker leg, the repair path). Suite 1,362 passing; typecheck,
  unit 22/22, e2e 24/24. Committed as `c3a856c` on `release-3.0.0`.

### Follow-up the same day: realised P&L settled only what the wallet still held

After the repair above the owner still saw "105 losses" and realised −$681.13. Ground truth
from the venue's activity feed (1,984 rows: 1,502 TRADE, 454 REDEEM): buys $7,365.02, sells
$3,393.75, redemptions $3,906.65 across 453 winning markets, wallet value $0.00 → realised
**−$64.62**, record **453 won / 104 lost**. The ledger was complete (1,502 fills, sizes agree to
0.000 contracts); the unit of account was wrong: `portfolio()` summed settlement over the
wallet's current positions, and a redeemed winner leaves the wallet with no sell row, so 479
ledger markets including every winner had no settlement term. Fixed: `_ledger_record()` settles
every market the ledger has fills for ($1/$0 per contract by `outcomeIndex`), resolutions cached
permanently per DB in `app_kv` (first refresh ~30 batched Gamma calls, then 0); Track leads with
the net figure and a ledger won/lost strip. Real wallet: gross +$11.87, fees $76.14, net
**−$64.27** (residual $0.35 = maker/taker fee attribution on 17 of 626 keys); 453 won / 117 lost
where 13 are paired both-sides markets counted in both columns and labelled as such. Tests:
`tests/test_track_ledger_record.py` (11). Suite 1,373 passing; typecheck, unit 22/22, e2e 24/24.
Committed as `06ef92b`.

## CI on Linux: two real bugs the developer machine hid (2026-09-03)

The first pushes to GitHub failed on Ubuntu with 22 tests that pass on Windows on both 3.11 and
3.14. Two mechanisms, both real product bugs, neither reproducible on a long-running dev box:

- **The test suite was reading the real vault.** `db.db_path()` restores the latest vault
  backup when the DB file is missing, and the conftest never isolated `KRYPT_POLYBOT_VAULT`, so
  the scratch DB silently arrived with a full schema (and the author's data) here and stayed
  empty on a clean machine ("no such table: bot_positions"). Fixed (`f63dd88`): the vault is
  scratch-scoped, the default scratch DB gets its schema at session start, and the main thread's
  cached connection is dropped after every test.
- **A market never looked up read as a five-minute-old miss.** `_resolutions_for()` /
  `_market_meta_for()` used `now - misses.get(cid, 0.0) > TTL` on `time.monotonic()`, which
  counts from boot. On any machine up for under 300 s — every CI runner, and every user's PC for
  five minutes after boot — nothing was ever looked up: settled record all "unknown", realised
  P&L null. Fixed (`8423dbe`): a missing entry means "never asked"; the failure-log throttle in
  `polymarket_api` had the same default. The two watchdog tests stamped a stale heartbeat as
  `loop.time() − 999`, negative on a fresh VM and read as "never stamped"; now a tiny positive
  stamp. Regression test: `tests/test_fresh_boot_clock.py`.
- Earlier the same day: CI's Node 20 could not import `.ts` from the unit tests (local Node 24
  strips types by default); CI now runs Node 22 with `--experimental-strip-types` (`eb42bf7`).

- **The mac and Linux frozen builds failed their self-test** with `PackageNotFoundError:
  py_ecc`. A fresh resolution of `eth-account>=0.13` installs eth-account 0.14 + eth-keyfile
  0.10, which import `py_ecc`, and `py_ecc` reads its own version from dist-info the spec never
  copied; the Windows build box on eth-account 0.13.7 never had py_ecc. Fixed (`1ecd74e`): the
  spec copies metadata for the whole eth/py_ecc/clob stack (guarded per package) and collects
  the py_ecc / eth_keyfile submodules. Reproduced and verified on Windows by freezing with a
  venv resolved fresh from requirements.txt: FAIL before, PASS after. Note the CI installers on
  all three platforms will ship eth-account 0.14 while the locally built Windows installer the
  owner live-tested carries 0.13.7; the self-test signs an order with both.

Lesson recorded: a green suite on a long-running developer machine proves less than it looks;
run on a fresh VM (CI) before calling a release ready — and freeze with a freshly resolved venv,
not the one that has been upgraded in place for months.

## Live test follow-up 2: a resting buy read as a purchase (2026-09-03)

Facts from the order log and the venue: the owner's `buy 5 YES @ 14¢ GTC` on an Elon Musk
market is resting, 0 filled (ask 16¢); the app said only "Order sent"; the sell that followed
was refused by the exchange ("balance: 0" of the token) and the raw 400 was shown. Three other
buys ($3.35, $5, $7) were let through to "not enough balance" rejections on a **$2.94** wallet.
Fixed (`25f8acf`): the position panel lists working orders for the open market
(`resting_orders(condition_id)` with `remainingSize` + `bestTouchCents`) with Cancel and Reprice
to fill; `place()` reports a rest ("Resting at 14¢ — the best ask is 16¢ … not a position until
it fills"), partial fills and kills; the ticket defaults to Marketable (was Limit/GTC), shows
`availableUsd`, warns on a non-crossing limit, and `preview()` blocks a buy whose cost exceeds
the known balance (unknown balance warns, never blocks); `translate_venue_error()` maps the
CLOB's rejection texts to plain sentences (raw text kept in the order log). Fixed in passing:
`resting_orders()` reported every NO order as YES. Verified against the live resting order:
the row reads buy 5 YES @14¢, 0 of 5 filled, best touch 16¢. 25 new tests; suite 1,554.

## Live test follow-up: selling (2026-09-03)

Buying worked in the owner's live test; a sell placed at the buy price rested above the bid and
never filled. Added a **position panel** under the Market-page ticket (`Market.tsx`
`PositionPanel`) showing the wallet or paper position for the open market with a **Sell** per
side: `terminal_trade.sell_position_preview()` walks the bids for the size, floors the limit at
best bid minus a chosen slippage (1/2/5/10¢, default 5), sizes the order to what fits inside the
cap, and `sell_position()` sends one FAK through `place()` (paper routes to the paper ledger).
Preview frozen on confirm; result reports filled / avg / proceeds / killed. Ticket sells now seed
from the best bid (they seeded from the ask and kept the buy price on a flip). `portfolio()`
carries `walletReadable` so a failed read is never shown as an empty position. Verified end to
end on a live book in paper mode on a data copy (buy 5 @ 50¢ → preview at the 49¢ bid → sell
filled 5 @ 49¢ → realised −$0.11). Known edge: sub-cent longshots (bid 0.1¢) cannot be sold by
this path because `place()` sends whole cents — the panel refuses with the reason. Committed
`0a6be66`; 25 new tests; suite 1,529.

## Release readiness (2026-09-03)

Verified on CI's interpreter for the first time: the full suite passes on **Python 3.11.16** as
well as the local 3.14 venv. Two 3.11-only differences found and fixed (`336dd5b`):
`terminal.related` summed probabilities with `sum()`, which differs by an ulp from 3.12+'s
compensated summation and flipped a one-decimal rounding — now `math.fsum`; and
`tests/test_loop_split.py` drove the real engine loops with copy enabled, opening live
websockets from a unit test whose library tasks wedged `asyncio.run`'s shutdown on 3.11 — the
helper now stubs all four socket modules and every sleep is above Windows 3.11's 15.6 ms
event-loop tick. CHANGELOG `[3.0.0]` carries every user-facing change through the Kalshi rebuild.

**Ready to ship once the owner does three things nothing here can do:** (1) the live order —
one real manual order through the new build, watched, then closed with Flatten all; (2)
fast-forward `main` to `release-3.0.0`, tag `v3.0.0`, push — CI's new gates (`test:unit`, Vite
build, the installer workflow's test job) and the Linux/mac builds have never run on GitHub,
so the first push is the real cross-platform build; (3) decide whether to ship the unsigned
Windows installer as-is (SmartScreen warns) or sign it.

## Status — Kalshi matching and Track follow-ups (2026-09-02)

- **Cross-venue recall.** On 60 real Polymarket questions the matcher found 3 of 31 counterparts;
  it now finds 29 of 29 with no false matches, and a holdout of 81 more markets produced 9
  matches, all correct. The misses were retrieval: the right Kalshi series was never a candidate
  for 23 of 28. `cross_venue.py` now routes sports fixtures via the league + date in Polymarket's
  slug to `KX{LEAGUE}{FAMILY}` tickers (allow-listed family table), crypto/macro via a
  named-topic table, the rest via alias-expanded search; structured markets match on subject,
  ET date, strike and direction, with explicit YES/NO inversion (`comparedSides`,
  `kalshi.side`) carried through price, bid, ask and history and labelled on the Market page.
  `tests/test_cross_venue_eval.py` runs a fixture-driven evaluation (recall ≥ 0.80, precision
  = 1.00) over a frozen Kalshi snapshot; `py:live` gained `kalshi:events` and
  `kalshi:match-rate` (5 of 12 fresh markets matched; the other 7 have no Kalshi listing).
  Not done: a full Kalshi event index (~15 s, ~8 MB) — the league table covers the common
  leagues; unlisted leagues correctly report no counterpart.
- **Follow-up: the app still said "no matches".** Reproduced through the real RPC path
  (`compare()`), not the harness: `compare()` was the only caller fetching the market with
  `include_raw`, and `pm_settlement_date` read Gamma's `startDate` — the LISTING date — as the
  settlement day, so the September 2026 Fed market (listed in May) never met Kalshi's
  `KXFEDDECISION-26SEP` event. The evaluation had called `find_match` directly and never went
  through `compare()`. Fixed: no raw read, Fed month from Kalshi's ticker stamp, evaluation
  drives `compare()` end to end with a listing-date regression test. Timed crypto: Kalshi lists
  15-minute up/down windows (`KX{ASSET}15M-…`); aligned windows now match on the exact window
  end (structural only), and 5-minute or not-yet-quoted windows get a family-specific note.
  69 cases through `compare()`: recall 1.00, precision 1.00; `py:live` 27/27, 7 of 12 fresh
  markets paired. Lesson recorded: verify claims through the app's own path.
- **Flatten skips settled positions.** The preview listed all 105 resolved positions, each
  refused with "could not read the order book" (closed markets have no book). Resolved
  positions are dropped before any book read and reported as one `settledSkipped` count; with
  no open positions the control says there is nothing to sell.
- **"Show settled" is the ledger's record.** The wallet only holds resolved losers, so the toggle
  showed 105 losses and no wins. It now renders `terminalSettled`: every settled ledger market
  with side, contracts, cost, payout, P&L, outcome (won / lost / closed out / unknown), an
  in-wallet badge, both-sides markets flagged once, totals that agree with `portfolio()` by
  construction. Suite 1,484 passing; typecheck, unit 22/22, e2e 24/24; `py:live` 26/26.

**Owner's decision, 2026-09-01: the terminal is independent of the automation switch.** The
ticket showed "trading off" and every manual path (hand-placed order, standing stop/take-profit,
chat order, wallet flatten) refused whenever `enable_trading` was off. That switch runs the
engines; the terminal is manual. Manual orders are now gated on paper mode, the wallet and the
manual ceilings only (`service._h_terminal_place`, `_h_terminal_flatten_wallet`, the standing
tick call, `remote._confirm_order`, `Market.tsx`, `Track.tsx`). The `allow_trading` parameter on
`standing_tick` / `flatten_wallet` stays (default True) for a caller that wants to hold fire.


## Verdict

Fix the money path, relabel the presets, then cut 3.0.0. Three things earn "critical": a standing
stop or take-profit places a real order while paper mode is on; the manual order ticket keeps
re-quoting behind the confirmation panel so the confirmed dollar figure is not the one the user
read; and the exit fee on externally closed positions is never charged because of a string test
that can never match.

## Ship-blockers (critical)

1. **Standing instructions bypass paper mode.** `terminal_trade.py:1356` calls `place()` directly;
   the paper gate exists only at `service.py:2767` and `remote.py:605`. A stop armed before
   flipping to paper signs a real FAK sell when it triggers.
2. **Confirmation cost mutates under the user.** `src/pages/Market.tsx:912-929, 1085-1090`. The
   preview request is memoised on the market object, which the 4 s poll (`:71`) replaces every
   tick, so the preview refetches while the confirmation panel is open. Confirm sends whatever
   the panel says now. The backend gate (`terminal_trade.py:378-393`) only checks the sent number
   against its own recompute.
3. **`usd()` K-rounds at $1,000 on the confirmation line.** `TerminalBits.tsx:43-49`,
   `shared/market.ts:845-853`. $1,249 confirms as "$1.2K".
4. **`"sell" in "sold"` is false: exit taker fee never charged.** `trader.py:2516`,
   `copy_trader.py:526`; `_exit_proceeds` (`trader.py:2428`) returns only `"redeemed"`/`"sold"`.
   `tests/test_copy_trader.py:200,235` stub `"SELL"` and certify the dead branch.
5. **Every armed stop retired when the wallet address is unreadable.**
   `polymarket_api.py:1980-1983` returns `[]` instead of raising; `terminal_trade.py:1324` then
   disarms with "you no longer hold this position."
6. **Crypto stop-loss swallowed, retry throttle consumed.** `crypto15m_trader.py:2336` stamps
   `_stop_retry_at` before `_place_exit_sell:2015` refuses on its own `_exit_fail_at` cooldown.
   Same pattern at `:2305` with sell-into-strength (60 s of no exit evaluation).
7. **Paper mode allows a naked short via resting sells.** `terminal_paper.py:120` credits cash
   outside the held-size guard; `:185` `_held()` ignores fully-resting sells. Proven: two GTC
   sells of 10 against 10 held → cash $1,014.00 instead of $1,004.50.
8. **Paired crypto cap guarantees a loss.** Both legs taker FAK (`crypto15m_trader.py:1262`),
   fee 0.07·p(1−p) each = 3.5¢/pair ATM → max profitable combined 96.5¢. Default and clamp are
   100 (`config.py:405, 1064-1066`; `c15Presets.ts:37`); the gate at `crypto15m_trader.py:456`
   never subtracts fees.

## High

- **Preset apply wipes risk settings.** 22 of 168 `TraderConfig` keys are missing from
  `settings-store.ts` `DEFAULT_CONFIG`; 11 are not covered by the preserve list at
  `ipc.ts:203-214`: `sizingMode, minContracts, maxContracts, takeProfitPct, lifetimeLossLimitPct,
  lifetimeLossLimitUsd, aiEnabled, aiProvider, aiWebSearch, aiAnthropicModel, aiOpenaiModel`.
  A user's lifetime USD cap silently returns to 0 (off).
- **No main-process order gate exists.** `ipc.ts:904` is a bare forward; claimed at
  `preload.ts:149`, `shared/market.ts:329-332`, and in STATE_OF_THE_BOT.
- **Chat-order cap written per keystroke; empty = 0 = no cap.** `Remote.tsx:157-158`; same
  pattern `Settings.tsx:114, 249, 258`.
- **Ticket `costUsd` describes no possible outcome.** `terminal_trade.py:280-282` uses requested
  size × average of the fillable slice only.
- **Maker/taker misattribution at four sites.** `script_engine.py:1264` (never maker),
  `trader.py:1542` (`entry_maker` never written so `limit_mid` charged taker),
  `copy_trader.py:456` (empty category → 0.07 crypto rate), `crypto15m_trader.py:1569`.
- **Lost update resurrects a cancelled stop.** `terminal_trade.py:1225` read → `:1397` blind
  write across seconds of awaits; `standing_cancel:1201` has no lock/version.
- **Remote `/pause` merges camelCase into snake_case cfg** (`remote.py:521-522`) so the loop
  still reads `enable_trading` true; only lands via the Electron round-trip (`main.ts:292`)
  whose failure is swallowed. `/flatten`/`/cancelall` (`remote.py:563,570`) cover only
  `bot_positions` while `/positions` shows the whole wallet.
- **Telegram groups: `chat.id` used as identity** (`bot_telegram.py:159,182`), so any member is
  authorised once the group is paired.
- **Discord bot zombie.** `bot_discord.py:50` heartbeat ACK never read; `:199-200`
  `ping_interval=None`. Half-open socket → "connected" forever.
- **Data wipe misses `script_shadow_orders`** (`db.py:938-953, 1040-1052`) and terminal fill
  reconcile re-walks history with no cursor, stamping everything `external` (`terminal_trade.py:599`).
- **Abandoned hook thread corrupts persisted script state.** `script_engine.py:252,255,1379,1399`.
- **Script notional cap shrinks below the exchange floor.** `script_engine.py:498` integer
  division on the ask path: max 5 @ 60¢ → 4 contracts; max 6 @ 17¢ → $0.90.
- **Script/chat config patches persisted unsanitised** in main (`main.ts:283-318` →
  `settings-store.ts:417`); the comment claims re-sanitising.
- **TopBar prints green $0.00 when the backend is down.** `TopBar.tsx:132-147`, `ipc.ts:511-520`.

## Medium

- `Market.tsx:346-351` Bid/Ask/Spread strip is always YES quotes even on the NO side.
- `Track.tsx:626` `?? 0` prints "+$0.00" for unknown realised P&L; `:250,259,722` null outcome
  renders "settled lost".
- `Track.tsx:44-74` polls all seven endpoints every 12 s regardless of tab.
- `Discover.tsx:122-141` `filters` dep defeats `filtersRef`; `:170` search un-debounced.
- `AppStateProvider.tsx:340-368` logs in context value re-render everything every 150 ms;
  `credentialsAll` missing from deps.
- `Strategies.tsx:149-157` install over an armed script hot-swaps live code without confirmation.
- `Remote.tsx:204-214` `setCode` inside another setter's updater.
- `cross_venue.py:76` years filter drops 1900–2100 → "$2,000" vs "$2,100" scores 0.80.
- `ai_analysis.py:403` host lock leaks on cancellation; `:457-583` SDK clients never closed;
  `:658` null reply raises.
- `script_audit.py:390`, `script_sandbox.py:152` only `SyntaxError` caught from `ast.parse`.
- `kalshi.py:62` genuine 1¢ quote read as $1.00 then discarded as sentinel.
- `terminal_trade.py:455` unreferenced `ensure_future`; `:695` unattributable fill fee 0.0 not null.
- `db.py:2055` vs `:2194` two "stale" definitions; `:2566`, `:2374` lack `dry_run=0` filter.
- `terminal_paper.py:294` cancel races the fill sweep's blind UPDATE.
- Known and still true: Discord RPC toggle no-op (`ipc.ts:136`); comingSoon + convergence toggle
  dead (`MainEngine.tsx:310`); four polling timers (`AppStateProvider.tsx:315`); `config.py`
  `STRATEGY_PRESETS` is read only inside `config.py` itself, so the 9-preset Python list is dead
  prose with stale performance claims (`config.py:713,736,763,800`).

## Speed

Master loop `service.py:615-1165` is one coroutine awaiting ~18 blocks sequentially, then
`sleep(1)`, no per-block timeout, watchdog at 240 s. Measured: `get_db()` 2.06 ms/call (fresh
connect + 4 PRAGMAs) vs 0.012 ms reused, across 148 call sites.

**Quick wins (< 1 h, low risk)**

| Fix | Where | Gain |
|---|---|---|
| Gather per-wallet `get_positions` | `copy_trader.py:323-326` | 0.8–2 s per 5 s tick @ 5 wallets |
| Gather `get_order` poll, one DB connection | `trader.py:1344,1387,1210` | 2–6 s → ~0.3 s @ 20 pending |
| Batch `fetch_markets_map` + gather `_manage_position` | `crypto15m_trader.py:2594,2274,2167` | 1.4 s → 0.25 s per tick; −90 Gamma calls/min |
| Drop `refresh_balance(force=True)` per entry, decrement locally | `trader.py:949` (pattern `copy_trader.py:966-970`) | 300–700 ms per order |
| Gather pUSD + `/value` legs | `polymarket_api.py:1576,1612` | 150–400 ms per balance read |
| Index `whale_trades(created_at DESC)` | `db.py:575-577`, `service.py:1645` | scan+sort → index walk |
| `signal:batch` + drop double flush | `service.py:723-747, 203-208` | 40 IPC hops → 1 per scan |

**Structural (next release)**

- Split copy + crypto15m into own task loops; `wait_for` per block; per-block duration logging.
- Move `fetch_all_open_markets` (10 pages × 0.2 s sleep) + `scanner.sync_markets` (~1,000 sync
  upserts) off the loop (`polymarket_api.py:558-569`, `scanner.py:201-236`). −3–5 s startup.
- Build copy entries from the `activity_ws` payload instead of re-reading the Data API
  (`service.py:1067-1090`). 5–12 s → ~2 s.
- Detect whales from `activity_ws` instead of the 120 s REST poll (`scanner.py:269`).
- Read quotes from `clob_ws.feed` before REST (`copy_trader.py:416`, `trader.py:186`); raise the
  64-token eviction cap (`clob_ws.py:279-283`).
- `VACUUM` (`db.py:3236`) takes an exclusive lock; loop writes raise after 10 s. Gate on idle.
- Don't touch: import time, `init_db`, crypto snapshot, `_rpc_first`, quote legs, IPC splitter.

## Strategies

Backtest run offline on the 644 MB local corpus (`backtest.py`, read-only). Harness charges
fee-v2 but **zero spread and zero slippage** (`backtest.py:89-109`); real ≈ 1–2¢ worse.

| Run | n | win | net ¢/ct | t |
|---|---|---|---|---|
| whale + momentum | 9,751 | 75.8% | **−2.33** | −3.2 |
| whale only | 8,360 | 78% | **−2.42** | −3.2 |
| momentum only | 1,391 | 60% | −1.78 | +1.1 |
| c15 15m favorite-follow | 10,373 | 77% | **−2.06** | −5.3 |
| c15 15m contrarian | 10,373 | 23% | **−3.80** | −9.7 |
| c15 5m (clean OOS Jul 22–Aug 31) | 7,167 | 76% | **−3.26** | −6.9 |
| whale fade | 8,360 | — | +1.07 | +1.4 |

Live ledgers agree at tiny n: main 13 positions −$43.25; crypto 9 positions −$3.14.

Break-even at typical entries (fee = rate·p(1−p), taker, +1¢ half spread): Edge Stack whale @70¢
needs +2.05 pts of alpha; Crypto Whale @60¢ +2.68; momentum @40¢ +2.20; c15 favorite @95¢ +0.83;
contrarian @10¢ +1.13 (12% relative).

**Why:** edge is a hand lookup with no fitted information (`scanner.py:79-125`); backtest scores
the whale's own post-impact price (`backtest.py:103-105`) while live fills at ask+2¢ after up to
120 s; trade_cluster grants +25/+18/+11 edge for implied < 15/25/35¢ (`scanner.py:150-154`);
edge clamped to +10/+8 so `sizing_max_edge=20` is dead and Edge Hunter (`min_edge 12`) can never
fire; early exits are full round trips. UI `strategies.ts` and Python `config.py` presets
disagree on routing and labels.

**What would change the sign:** post maker via `order_style='limit_mid'` (`trader.py:190-191`),
≈ +2.3¢ swing → whale follow ≈ −0.1¢; shadow-test the fade; fix paired cap; retire contrarian;
relabel main presets as "example configuration" with the measured number. (Shadow-by-default for
presets was proposed and **declined by the author on 2026-09-01** — do not re-propose.)

**Validation gate before any default:** anchored walk-forward ≥ 4 folds; replay with
`replay_latency_secs ≥ 120` and `replay_min_quote_stable_secs > 0` (both default off,
`replay.py:435-437`); per-event t > 3; positive at 1.5¢ assumed spread; parlay holdout must stop
filtering on itself (`parlay_generator.py:96-107`).

## Release mechanics

**OK:** all 13 new lazy modules in `krypt-polybot-backend.spec` hiddenimports; requirements match
imports; build scripts clean of deleted modules; no secrets in tree; smoke-test example inert;
deleted webhook/leaderboard keys gone; arming e2e current.

**Gaps:**
- Bump `package.json` to **3.0.0** (Removed section + script security-model change). About reads
  `app.getVersion()`.
- Cut `CHANGELOG.md [Unreleased]`: eight subsections with duplicated `Added`/`Fixed` headings.
- `README.md:207-214, 425-426` still describe the deleted "Sandboxed vs Trusted" mode.
- `ci.yml` runs typecheck + pytest only: add `test:unit`, vite build, e2e via xvfb.
  `build-installers.yml` publishes on push to main with no test gate.
- Local venv is Python 3.14.6; CI pins 3.11; `python-utils.mjs` has no pin.
- `.gitattributes` never applied: `git add --renormalize .` in its own commit.
- `YOUTUBE_GUIDE_SCRIPT.md` predates the terminal and claims no paper mode.
- `crypto15m_rules_no` (`config.py:243`) has no UI; its YES twin does.

## Order of work

1. Paper-mode gate inside `terminal_trade.place()` + test.
2. Freeze the confirmation snapshot; exact-dollar formatter on ticket + confirmation.
3. Fix `"sell"`/`"sold"` and the two test stubs; re-run loss-breaker tests.
4. Raise in `get_positions` on address failure; stamp crypto stop retry only after submit.
5. Reserve inventory for resting paper sells; cash credit inside the held guard.
6. Complete `DEFAULT_CONFIG`; widen preserve list; paired cap clamp; debounce money fields.
7. Remote: snake_case pause patch; flatten/cancelall scope; Telegram `from.id`; Discord ACK + ping.
8. Relabel presets; single source for `strategies.ts`. (Shadow-by-default: declined by the author.)
9. Speed quick wins, one commit each.
10. Version, changelog, README, CI gate, renormalize, `py:live`, one real order.
11. Next release: split the loop, whales from socket, engine paper mode.

## Method

Baseline typecheck + pytest on the working tree. Five parallel reviews: Python money path,
renderer/Electron, backend performance (timed locally), preset expectancy (backtest on local
corpus), release mechanics. Ten top claims re-verified by hand in the source. No source files were
modified; two throwaway proof tests were run and deleted.
