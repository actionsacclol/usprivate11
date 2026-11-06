# Krypt PolyBot — State of the Bot

**As of 2026-08-26.** Version `2.0.0` in `package.json`; the working tree carries roughly a
month of uncommitted work on top of it (see [Repository state](#repository-state)).

> **Later changes not covered below (2026-10):** AI agents over MCP (named agents, money
> rails, forecast-before-buy, approvals, HTTP API), Autopilot, six AI providers, and an
> app-wide **Paper | Live** account mode that replaces the terminal-only paper switch. In
> Paper the engines now send nothing, but they are still **not simulated**. See the
> [CHANGELOG](CHANGELOG.md) `[Unreleased]` section and [docs/GUIDE.md](docs/GUIDE.md).

Everything asserted here was checked against the code, not against the older
`AI_CONTEXT.md` / `PROS_AND_CONS.md`, both of which predate the terminal, paper trading,
Kalshi, and remote control and are stale in places. Where they disagree with this document,
this document was verified more recently.

---

## Contents

- [Snapshot](#snapshot)
- [What it is](#what-it-is)
- [How everything works](#how-everything-works)
  - [Process architecture](#process-architecture)
  - [The Polymarket integration](#the-polymarket-integration)
  - [The money path](#the-money-path)
  - [Persistence](#persistence)
- [Feature inventory](#feature-inventory)
  - [The terminal — you decide](#the-terminal--you-decide)
  - [The automation — it decides](#the-automation--it-decides)
  - [User scripting](#user-scripting)
  - [Remote control](#remote-control)
  - [App-level](#app-level)
- [Design principles the code actually enforces](#design-principles-the-code-actually-enforces)
- [Pros](#pros)
- [Cons](#cons)
- [What needs work](#what-needs-work)
- [Repository state](#repository-state)

---

## Snapshot

| | |
|---|---|
| Version | 2.0.0 (uncommitted work toward the next release) |
| Python tests | **1027 passing**, 21s (`npm run py:test`, run 2026-08-28) |
| TypeScript | **Clean** (`npm run typecheck`, both projects) |
| Python LOC | ~36,200 across 45 modules |
| TS/TSX LOC | ~20,200 across renderer + main + shared contracts |
| Renderer tests | 2 files (`e2e/arming.e2e.mjs`, `e2e/series.test.mjs`) — arming flow + chart geometry only |
| Live provider check | `npm run py:live`, deliberately outside CI |
| Packaging | electron-builder targets configured for **win (NSIS)**, **linux (AppImage/deb)**, **mac (dmg)** |
| Last commit | `4f8404f`, 2026-07-28 |
| Telemetry | **None.** Webhooks and the community leaderboard were deleted outright |

---

## What it is

A free, local-first desktop app for Polymarket. It is now **two products in one shell**, and
the split is the most important thing to understand about the current state:

1. **A manual terminal** — Discover / Market / Track. It answers *"what is going on, and what
   should I do about it?"* for **any** market you name. You place the orders.
2. **A set of automation engines** — whale/momentum, short-term crypto, copy trading, and your
   own Python scripts. They answer *"should I act, right now, on this one thing I am
   watching?"* and trade while you are away.

The sidebar is grouped along exactly that line (Terminal / Automation / Account / App),
because the two halves have genuinely different risk profiles and the app stopped being
readable when they were interleaved.

Everything trading-related ships **off by default**. Nothing places an order until you turn on
live trading *and* the specific engine's switch.

---

## How everything works

### Process architecture

```
┌─────────────────────────────────────────────────────────┐
│ Electron main (electron/main.ts, ipc.ts)                │
│  · owns settings.json, profiles, tray, autostart        │
│  · spawns + supervises the Python backend               │
│  · re-derives the cost of every manual order (gate)     │
└───────────────┬─────────────────────────────────────────┘
                │ JSON-RPC over newline-delimited stdin/stdout
                │ (no local HTTP server → no ports, no firewall)
┌───────────────▼─────────────────────────────────────────┐
│ Python backend (python/service.py)                      │
│  · one asyncio loop, 76-method _HANDLERS dispatch table │
│  · master loop ticks every enabled engine               │
│  · 4 websockets + REST with a per-host rate gate        │
│  · one SQLite WAL database                              │
└─────────────────────────────────────────────────────────┘
                ▲
                │ contextIsolation preload → window.krypt.*
┌───────────────┴─────────────────────────────────────────┐
│ React renderer (src/) — 22 pages, push-driven           │
│  · never calls Polymarket directly                      │
│  · subscribes to backend events; 4 polling timers left  │
└─────────────────────────────────────────────────────────┘
```

`PythonBackend` (`electron/system/python-backend.ts`) restarts the engine with exponential
back-off, re-pushes the last known config on every restart, and runs a startup reconcile so a
crash mid-fill doesn't permanently inflate the open-position count.

Two IPC contracts, deliberately kept apart:

- `shared/types.ts` — the engine's contract (config, positions, signals, scripts, profiles).
- `shared/market.ts` — the **terminal's** contract.

A hand-placed order has no signal behind it and must never be counted as an engine trade, or
the bot's win rate becomes a blend of what it decided and what you decided. The terminal even
has its own tables (`terminal_orders`, `terminal_fills`).

### The Polymarket integration

Three independent hosts, all read endpoints public:

| Host | Used for | Gotcha the code handles |
|---|---|---|
| `gamma-api.polymarket.com` | market/event metadata, tags, resolution | `outcomes` / `outcomePrices` / `clobTokenIds` arrive as **JSON-encoded strings**; markets have no `category` (derived from the parent event's tags); `/markets` defaults to `closed=false`, so a resolved market vanishes unless asked for explicitly |
| `data-api.polymarket.com` | public trade feed, `/positions`, `/value` | trade notional is `size * price` |
| `clob.polymarket.com` | book, price, midpoint, and all authenticated trading | empty-book sentinels (`bestBid: 0` / `bestAsk: 1`) must be scrubbed or they render as a confident "bid 0¢, ask 100¢" |

Plus Kalshi's public API (read-only second venue) and, if you enable them, Discord/Telegram.

**Internal model** — the code reads oddly without this:

- `ticker` internally means the on-chain **`conditionId`**. The two outcomes are ERC-1155
  tokens in `clobTokenIds`: index 0 = yes/up/first, index 1 = no/down/second. "Buy yes" is a
  BUY of token[0].
- **Prices are cents (1–99)** everywhere internally. Both USDC and outcome shares use 6
  decimals on the CLOB.
- `network` is always `"mainnet"` (Polygon). One environment; the field never varies.

**Auth is a Polygon private key**, implemented raw in `polymarket_auth.py`:

- **L1** — an EIP-712 `ClobAuth` message (domain version `"1"`) derives or creates the L2 API
  credentials.
- **L2** — HMAC-SHA256 headers over `timestamp + METHOD + path + body`. The body must be
  HMAC'd as the **exact bytes sent**, so it is serialised once and passed via httpx `content=`.
- **Orders** — EIP-712 CLOB **V2**: domain version `"2"`, 11-field `Order` struct, collateral
  is pUSD. Verified to recover to the wallet address in a local round-trip, and the frozen
  PyInstaller build self-tests the signer before shipping.

### The money path

There is no single `if` whose inversion turns a simulation into a trade. Every order passes:

```
intent (click / stop trigger / engine signal / script return)
  → config clamps (server-side, in config.py — money knobs clamped before they drive an order)
  → live-trading switch + per-engine switch
  → paper-mode branch (separate entry point; terminalPlace refuses outright while paper is on)
  → re-quote at submit (a stale snapshot ask buys proportionally more on a notional fill)
  → tick snap + minimum order size
  → main-process cost re-derivation (refuses if the book moved past what the human read)
  → EIP-712 sign → CLOB
  → fill poll → reconcile against the venue's OWN trades ledger
```

Cost basis is taken from the venue's ledger, not from what we asked for — the requested price
ignores tick-snapping, partial fills and taker fees, every one of which makes the naive number
wrong *in the user's favour*. A fill that can't be reconciled is stored `unreconciled`,
**excluded from every aggregate**, and counted in a warning strip. Convention is **average
cost, not FIFO**, and the UI says so.

### Persistence

`%APPDATA%/Krypt PolyBot/`

| File | Contents |
|---|---|
| `settings.json` | AppState — config, profiles, window bounds, flags |
| `credentials/wallet.mainnet.key` | Polygon private key, encrypted |
| `credentials/apicreds.mainnet.json` | derived CLOB creds, encrypted |
| `credentials/bot.{discord,telegram}.token` | chat bot tokens, same encrypted writer |
| `data/krypt-polybot.db` | one SQLite WAL database |
| `logs/backend.log` | rotating log |

Encryption backends, in order: **Windows DPAPI** → **OS keychain** (macOS Keychain / Linux
Secret Service via `keyring`, holding a Fernet master key) → **chmod-600 plaintext that logs
loudly about itself**. Files are transparently upgraded when a better backend appears.

---

## Feature inventory

### The terminal — you decide

| Screen | What it does |
|---|---|
| **Discover** | Four live columns (New / Closing soon / High volume / Resolving), filters, search, watchlist |
| **Market** | Probability chart on a bounded 0–100 axis, full book ladder, trade tape, order ticket, resolution-risk report, related markets, Kalshi overlay |
| **Track** | Positions, resting orders, standing instructions, fill ledger, and a log of every manual order *including the failures* |

Supporting machinery:

- **Resolution risk** — eleven checks on who decides, on what source, how ambiguous the
  wording is, whether the market still accepts orders, and whether a sibling contradicts it.
  Scored only over the checks that **actually resolved**; below four resolved checks it prints
  no score at all. An unread check is never counted as a pass.
- **Per-field provenance** — every number carries which provider produced it and when *we*
  received it, as a hoverable dot.
- **Own microstructure** — the CLOB websocket's locally-stamped quotes are recorded per market,
  giving real spread over time with no indexer lag. Gated on subscription; never fills a gap.
- **Standing instructions** — stop loss / take profit / price alerts. Triggers are
  **probability points, not percentages**. Each passes exactly the checks a manual order does.
  A refused stop stays **armed** and retries (bounded to three consecutive failures) rather
  than retiring itself; a stop blocked because live trading was off says so.
- **Paper trading** — a ticket-level PAPER/LIVE switch. Fills are walked out of the **real
  book** at the average you'd actually have paid, with the real fee-v2 curve, real tick sizes
  and minimums, no naked shorts, and exact 100¢/0¢ settlement. Resting orders fill only when
  the market trades **strictly through** the price, because queue position is unknowable and
  the pessimistic assumption is the honest one. No market impact — stated on Track and warned
  about on the ticket when an order eats more than a quarter of visible depth. Its own tables,
  its own totals.
- **Cross-venue (Kalshi)** — read-only, public endpoints, no key, no order path. A match is
  treated as a **claim**: both titles shown verbatim, reasons for *and against* listed, and the
  spread **withheld entirely** below the confidence floor, because a fictional edge on a
  mismatched pair is worse than no panel.
- **AI analysis** — Claude or an OpenAI model reads the market's resolution rules and reports
  what it turns on, the wording traps, what would move it, and what it does not know. It
  returns a **lean from a four-word vocabulary** (cheap / rich / about right / no view) with
  its reasoning, and **never a probability, fair value or edge**: the response schema has no
  numeric field and is closed, so the provider's own structured-output layer rejects one
  before the app sees it. A sentence that prices the market anyway is stripped and the count
  is shown. Public market text only — never the wallet or a position, enforced by
  `build_payload` taking a market rather than an account. Both provider hosts sit in
  `terminal._HOSTS`, so the offline switch, the 429 park and the privacy counters cover them.
  Off by default, bring your own key, on demand only — no scheduler and no engine path
  reaches it, because every call bills the user's own provider account.
- **Cost discipline** — per-host serial gate with minimum spacing, TTL caches sized to how fast
  each field actually changes, and a 429 park that fails *fast* rather than starving a
  high-value request. `terminalHealth` exposes live call/error/latency counts.

### The automation — it decides

| Engine | Modules | What it does |
|---|---|---|
| **Main (whale + momentum)** | `scanner.py` → `trader.py` | Scans the public trade feed for large taker buys and momentum/cluster signals, scores them heuristically, places CLOB limit orders, polls fills, resolves, reconciles on restart |
| **Short-term crypto** | `crypto15m.py` + `crypto15m_trader.py` | Polymarket's recurring "Up or Down" markets (5m / 15m / hourly, 7 assets) with favorite-follow / contrarian / model strategies, stop-loss and take-profit exits |
| **Copy trading** | `copy_trader.py` | Follows chosen wallets, mirrors entries at your own size, tracks exits, per-wallet filters |
| **User scripts** | `script_engine.py` | Your own Python strategies (below) |
| **Parlay scheduler** | `parlay_generator.py` | Per-hour crypto15m schedule mined from the recorded corpus with a holdout. When **armed it OWNS entries** — the sniper is off for seated hours |
| **Volume farming** | `volume_farm.py` | Paired self-crossing volume generation — **CLI only** (`npm run py:volume`), not wired into the app |

Live data arrives over four websockets — `clob_ws` (book), `spot_ws` (underlying spot),
`rtds_ws` (Polymarket real-time), `activity_ws` (trade activity) — with REST fallback.

Each engine has its own switch **on its own page**, its own risk breakers, and its own status
line. The app header now only *reports* ("N engines live" plus a per-engine Main/Crypto/Copy/
Scripts strip); it no longer carries the main engine's controls, which used to make the header
announce "Trading Paused" while three other engines were live.

### User scripting

Write a strategy as ordinary Python with hooks: `decide(ctx)`, `decide_market(ctx)`,
`manage(position, ctx)`, `decide_signal(signal)`, `supervise(app)`, plus
`on_start` / `on_fill` / `on_settle` and a persistent JSON `state` dict.

- **No language sandbox.** The old AST whitelist (no imports, no classes, curated builtins)
  fought every non-trivial strategy while never being a real boundary — CPython sandboxes are
  escapable, and the module said so. Scripts now run as full Python with real builtins.
- **`script_audit.py` replaced it**: a static AST scan that reports credential access, network
  egress, dynamic execution, subprocess/filesystem access — and flags **obfuscation as its own
  critical finding**, because a strategy script has no legitimate reason to base64-decode a
  blob or `exec()` anything. The Scripts page shows the findings before you arm it. It is
  honest about being a smoke detector, not a lock.
- **What is still guaranteed is liveness, not safety.** Every hook runs in a joined daemon
  thread with a hard wall-clock timeout (default 1s); a hook that overruns is abandoned and its
  script auto-disabled, so the master loop keeps cadence. The backtest path uses an in-call
  time budget instead, since it makes thousands of calls per run.
- **The money rails are unconditional** — `script_max_entry_cents`, `script_max_contracts`,
  `script_max_open`, `script_daily_loss_usd`, `script_max_enabled`, one-entry-per-window. Where
  a rail and an exchange minimum contradict, the entry is **refused, never rounded up past your
  cap**.
- **Shadow mode by default.** A new script records what it *would* have traded until you arm
  it, with an explicit confirmation.
- **Scripts are now independent of every other engine.** Three hidden couplings were removed:
  the crypto tab's per-coin checkboxes no longer narrow scripts (each script carries its own
  asset scope), the signal collector force-enables when an enabled script defines
  `decide_signal`, and the master switch now gates *real orders* rather than ticking.
- **Every script reports a real state** — `running` / `blocked` / `starting` / `error` / `off`
  plus one actionable sentence naming the gate, computed backend-side where the gates are
  applied so it can't drift.
- `script_backtest.py` is **structurally order-free** — it never imports `polymarket_api`.
- `script_docs.py` renders the in-app API docs and the AI context pack *from the live modules*,
  so the docs can't drift from the code.

### Remote control

Discord and Telegram bots, both **off by default**, sharing one brain (`remote.py`) so they
can't drift into different security rules.

- Read/de-risk: `/status`, `/positions`, `/pnl`, `/balance`, `/orders`, `/price`, `/pause`,
  `/resume`, `/flatten`, `/cancelall`. A standing instruction that fires — or was **blocked**
  from firing — is pushed unprompted.
- **The paired-id allowlist is the boundary, not the token.** Tokens leak into screenshots and
  pasted logs. A message from an unlisted id is dropped before it is parsed, and logged.
- Pairing is a six-digit code, single use, five-minute life.
- **Placing orders from chat is a second switch**, off by default, never enabled as a side
  effect. Even on: preview, fresh `/confirm` within 90 seconds, a backend-enforced per-order
  dollar cap, and the same paper mode a click obeys (the automation switch does not
  gate manual orders — the terminal is manual, the engines are not).
- Stale commands (older than two minutes) are acknowledged but **not run** — Telegram queues
  undelivered updates for ~24h and a fresh process starts at offset 0, so a `/flatten` sent at
  2am would otherwise execute when the PC woke at 9am.
- Every command is logged — accepted, refused, rate-limited, or from a stranger.
- Transport is hand-rolled on existing dependencies: Telegram long-polls over `httpx`, Discord
  runs a Gateway client on the `websockets` package the CLOB feed already pulls in. Both avoid
  needing a public endpoint on a home PC behind NAT.

### App-level

- **Data & privacy panel** (Settings) — names every host this machine contacts, what each is
  for, and live request/error/latency/cache counts read straight from the provider gate, plus
  **one switch that turns it all off**. With offline mode on every panel goes blank and says
  why, rather than showing something stale.
- **Command palette** — `Ctrl/Cmd+K` or `/`; `g` then `d`/`m`/`t`/`s`/`w` navigates. Every
  shortcut is inert while a field has focus.
- **Simple / Advanced UI modes** — the sidebar and the Crypto page both split, so a new user
  sees a short app and a power user sees all of it.
- **Profiles** with JSON export/import; **Accounts** for multiple wallets; **Backtest** page;
  **Visualizer** (Canvas orbital map); **Guide**; desktop notifications for standing
  instructions; Discord Rich Presence (carries no trade data).
- **Trade Visualizer** — signals spawn as outline orbs, open positions orbit, resolved trades
  fly to win/loss buckets. Nothing else free on Polymarket has it.

---

## Design principles the code actually enforces

These are worth stating separately because they are unusual, and because they are enforced in
code rather than aspired to in a README:

1. **Honest nulls.** A value nobody could produce is `null`, renders as an em dash, and is
   never coerced to `0` or a default. Where a value is missing, the row says **why** and what
   would fix it. A market with no 24h volume reads "—", not "$0.00". A position with no cost
   basis in your ledger reports P&L *unknown*, not break-even. Unknown P&L is not tinted green.
2. **A claim is labelled as a claim.** Cross-venue matches, resolution scores, and
   confidence-gated numbers are withheld rather than guessed. Below the confidence bar, no
   number is printed at all.
3. **The pessimistic assumption wins in simulation.** Paper fills err toward *fewer* fills,
   because manufacturing fills that never happened is the exact lie a simulator exists to
   avoid.
4. **A refusal must be loud.** Silent failures were treated as bugs across the board — a failed
   crypto exit that only wrote an `error` field, a stop that retired itself after being
   refused, a geoblocked wallet that looked completely healthy.
5. **Separate ledgers never mix.** Paper vs real, manual vs engine, shadow vs armed. A paper
   number is never added to a real one.
6. **No telemetry.** The four Discord webhooks and the community leaderboard were deleted
   outright — both sent trading activity to somebody else's server, which is the one thing a
   local-first app should not do quietly.

---

## Pros

**1. The honesty discipline is real and load-bearing.** Most trading tools round unknowns to
zero and call it a UI. This one propagates nulls through the whole stack, splits realised from
settled P&L, excludes unreconciled fills from aggregates, and refuses to print a cross-venue
spread it isn't confident in. That is a genuine differentiator and it is enforced by tests
(`test_terminal_honesty.py`).

**2. Zero infrastructure.** One installer, one SQLite file, no cloud account, no subscription,
no server. Python is bundled via PyInstaller so the user never touches it. Credentials are
encrypted at rest with the OS keystore.

**3. No telemetry at all, and the claim is checkable.** The privacy panel names every host with
live counters read from the actual request gate — a privacy claim you can't verify is
marketing, and this one is instrumented.

**4. The terminal + automation split is the right product shape.** Very few tools give you both
a discretionary terminal and a bot on the same data with the same order path, and the app is
disciplined about never blending the two track records.

**5. Paper trading that is wrong in the same places the real thing is.** Fills out of the real
book at the walked average, real fees, real minimums, pessimistic queue assumptions. Most
simulators teach you that trading is easy; this one deliberately doesn't.

**6. Strong money-path defences.** Server-side config clamps, re-quote at submit, main-process
cost re-derivation so a stale or hostile renderer can't overspend, unconditional script rails,
a second switch for chat trading, and everything off by default.

**7. Test coverage where the money is.** 1,027 pytest tests over the engine, sizing, P&L,
reconciliation, config, credentials, signing, the crypto executor, copy engine, loss guards,
settlement model, script rails, terminal honesty, paper fills, and cross-venue matching.

**8. `npm run py:live`.** A live provider check deliberately kept out of CI, because it is slow
and depends on somebody else's uptime — but it is the only layer that catches a vanished route
or a changed response shape. It found two real bugs on its first run.

**9. Docs generated from live modules.** `script_docs.py` renders the scripting API docs and AI
context pack from the modules themselves, so that surface cannot drift.

**10. A month of fixes driven by real accounts, not fixtures.** The changelog's Fixed section
reads like a list of things that only surface with real money: a settled market counted as
unrealised (a phantom -$229.12 on a real wallet), realised P&L that only counted sells, a
websocket resubscribe storm on 1,502 historical fills, worthless positions described as
claimable cash, opposite questions matched across venues. That is what a maturing product's bug
list looks like.

---

## Cons

**1. The strategies still have no proven edge.** They are heuristic point systems with
hand-tuned constants. The README says so plainly, and the author's own backtest put the default
presets net-*negative* after fees. This is the single most important limitation and no amount
of engineering elsewhere changes it.

**2. The engines have no paper mode.** Paper trading covers the **manual terminal only**.
Enabling an engine risks real money immediately. User scripts get shadow mode; the three
built-in engines don't. (This was previously mis-stated in eight places and has now been
scoped correctly, but the underlying gap remains.) *2026-10: the account-wide Paper mode now
stops the engines from sending anything, so turning one on in Paper is harmless, but they are
still not simulated: the gap between "waited in Paper" and "tested" remains.*

**3. Renderer and main-process test coverage is nearly absent.** 1,027 Python tests versus two JS
test files. A regression in IPC, the preload bridge, or UI state can ship unnoticed — and the
terminal put a *lot* of new logic in the renderer. The e2e suite isn't in CI either (it needs a
display).

**4. Several modules are very large.** `service.py` 3,551 lines, `db.py` 3,318,
`crypto15m_trader.py` 2,902, `trader.py` 2,842, `polymarket_api.py` 2,354. Individually
justified, collectively a real barrier to review and to anyone else contributing.

**5. Preset drift between backend and UI.** `config.py` still ships nine `STRATEGY_PRESETS`;
`electron/system/strategies.ts` exposes three. The extra six aren't reachable from the UI but
are still live in the backend and applicable via an imported profile. The old claim that the
two are "mirrored exactly, so there is no drift" is no longer true.

**6. Dead "coming soon" scaffolding.** `electron/ipc.ts` still carries `comingSoon`
preset-rejection logic that no preset now triggers, and `src/pages/MainEngine.tsx:310` still
renders a "Trade convergence (coming soon)" toggle whose scanner was never implemented.

**7. The Discord Rich Presence toggle is still a documented no-op.** `state:setEnableDiscordRpc`
(`electron/ipc.ts:136`) returns `ok()` and does nothing. A user who turns it off will believe
it worked. Flagged in the July doc; still true.

**8. Four polling timers survive in `AppStateProvider`** (credentials 6s, scanner stats 8s,
signals 12s, positions 12s) regardless of whether anything changed — on top of the backend's
own cadences and the terminal's polling.

**9. Volume farming is documented as an engine but isn't one.** `volume_farm.py` is reachable
only via `npm run py:volume`, and its runner is literally headed `TEMP side-strategy runner —
REMOVE-ME LATER`. Nothing in `service.py`, `ipc.ts`, or the UI references it.

**10. The wallet key is on the machine.** Encrypted with DPAPI/keychain, but decryptable by
that user account — and user scripts now run as **full unsandboxed Python inside the process
that holds the decrypted key**. The audit surfaces risk; it does not prevent it. A malicious
script you enable wins. This is a defensible trade-off, clearly documented, but it is the
sharpest edge in the product.

**11. No code signing or auto-update** on any platform. Intentional (no certificate), but it
means an unsigned installer and SmartScreen friction. Packaging itself is better wired than it
first appears: `build-installers.yml` builds Windows NSIS, Linux AppImage/deb and both macOS
architectures on native runners and publishes them to a per-run prerelease.

**12. Redemption is not automated.** A won position shows a `redeemable` badge and links out.
Understandable — that is custody surgery — but it means the loop isn't closed inside the app.

**13. `backgroundThrottling: false`** keeps the renderer fully active when hidden to tray, so
the Visualizer's RAF loop survives. Real CPU/GPU cost while nobody is looking.

---

## What needs work

Ordered by what would actually change outcomes.

### Now — before the next release

1. **Commit the working tree.** ~5,600 changed lines across 53 tracked files plus ~20 new
   modules sit uncommitted a month after the last commit. Tests and typecheck are green, so
   there is no technical reason for the risk. This is the top item on the list.
2. **Bump the version and cut the changelog.** `package.json` still says 2.0.0 while
   `[Unreleased]` now contains the terminal, paper trading, Kalshi, remote control, and the
   sandbox removal — comfortably a minor-version release on its own.
3. **Run `npm run py:live` and one live order smoke test.** The live check exists precisely
   because unit tests can't catch a changed response shape, and the one remaining pre-money gap
   has always been a single real order plus confirming the wallet's on-chain approvals are set
   (`check_trading_ready()` detects missing approvals but does not set them).
4. **Reconcile the stale docs.** `AI_CONTEXT.md` describes five engines and a language sandbox
   that no longer exists; `PROS_AND_CONS.md` is from July and predates half the product. Either
   regenerate them or point them here.

### Soon — correctness and trust

5. **Test the renderer.** The terminal moved substantial logic client-side and it is the least
   tested layer in the app. Start with the order ticket's preview math, the P&L splits on
   Track, and the null-rendering rules — the places where a silent regression re-introduces
   exactly the dishonesty the design forbids.
6. **Get e2e into CI** (`xvfb-run` on Linux). The arming flow is the one flow where a
   regression lets user code spend money without a confirmation.
7. **Resolve the preset drift.** Either restore the six backend presets to the UI, delete them,
   or generate `strategies.ts` from `config.py` so the two cannot diverge again.
8. **Delete the dead `comingSoon` path and the convergence toggle**, or implement
   `scan_convergence()`. Right now it is scaffolding that implies a feature.
9. **Make the Discord RPC toggle work or remove it.** A visible switch that does nothing is a
   small lie in an app whose whole design thesis is not telling small lies.

### Then — the actual product gaps

10. **Validate a strategy out-of-sample.** Still the open item that matters most. Every preset
    is in-sample until proven otherwise, and the app's credibility rests on this more than on
    any feature.
11. **A paper mode for the engines.** The terminal's simulator already walks real books,
    charges real fees, and settles exactly — that machinery is the hard part and it exists. The
    remaining work is routing engine intents through it into a separate ledger. This would
    remove the single biggest reason to distrust turning an engine on. *(2026-10: deliberately
    not done in the agent port — engines send nothing in Paper instead.)*
12. **A true flatten (sell-to-close) for the main engine.** "Cancel All" only cancels resting
    orders; there is no one-click exit from open positions.
13. **A backtest path for `decide_market()`.** The recorded tick corpus only covers crypto
    windows, so general-market scripts can only be validated in shadow mode.
14. **Exit simulation in shadow mode.** Shadow records entries and holds them to settlement, so
    a strategy that leans on `manage()` for its exits behaves differently once armed — which
    means shadow's track record misstates it in a way the user can't see.
15. **Reduce the polling.** Push the four `AppStateProvider` timers onto the existing event
    stream.
16. **Split the largest modules.** `service.py`, `db.py`, and `crypto15m_trader.py` are the
    three where size is now actively costing review quality.
17. **Automated redemption**, with its own design conversation about custody.
18. **Prove the mac/linux builds** or drop the targets rather than implying support.

---

## Repository state

```
Branch:       main
Last commit:  4f8404f "Close the three remaining audit should-fixes" (2026-07-28)
Working tree: 53 modified tracked files, ~5,600 insertions / ~2,400 deletions
              + ~20 untracked new modules
              + 3 deletions (leaderboard.py, webhook.py, test_leaderboard.py)
Tests:        1027 passing
Typecheck:    clean
```

New and uncommitted: `terminal.py`, `terminal_trade.py`, `terminal_paper.py`, `kalshi.py`,
`cross_venue.py`, `remote.py`, `bot_discord.py`, `bot_telegram.py`, `bot_secrets.py`,
`script_audit.py`, `live_check_terminal.py`, plus their tests, plus `e2e/series.test.mjs` and
`examples/smoke-test-one-trade.py`.

The frozen-build risk is covered: every new lazy-imported module (`terminal`, `terminal_trade`,
`terminal_paper`, `kalshi`, `cross_venue`, `remote`, `bot_secrets`, `bot_telegram`,
`bot_discord`, `script_audit`) is already listed in `krypt-polybot-backend.spec`'s
`hiddenimports`, and the build gate self-tests the signer.

---

## Verdict

The engineering has outrun the validation. The app is now genuinely unusual — a local-first,
zero-telemetry prediction-market terminal with an honest simulator, per-field provenance, a
second venue for reference, phone control, and a scripting platform — and the discipline in the
recent work (honest nulls, withheld claims, loud refusals, separate ledgers) is better than most
commercial trading software.

What it still can't tell you is whether any of its strategies make money. That gap, plus the
absence of a paper mode for the engines and the near-absence of renderer tests, is what stands
between "an impressive piece of software" and "something to trust with real money."

The immediate move is unglamorous: **commit the month of work, cut a release, and run the live
check.** Everything else on the list is downstream of that.
