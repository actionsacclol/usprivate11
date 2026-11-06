# Krypt PolyBot — Guide

The detail behind the [README](../README.md): how the app is built, what each feature does and
refuses to do, and where your data lives. Start with the README's [Disclaimer](../DISCLAIMER.md)
warning if you have not read it.

## How it works

```
┌─────────────────────────────┐        JSON-RPC over stdio        ┌──────────────────────────┐
│  Electron + React renderer  │ ────────────────────────────────▶ │   Python backend (child)  │
│  (dashboard, settings, …)   │ ◀────────────────────────────────  │  scanners + trade engine  │
└──────────────┬──────────────┘     events / responses / logs      └────────────┬─────────────┘
               │ contextBridge (preload)                                          │
               ▼                                                                   ▼
        window.krypt.* IPC                                   Polymarket API · CoinGecko · SQLite
```

The renderer never talks to Polymarket directly — only through `window.krypt.*` IPC. The Python backend runs
the scanners and trading engine, signs Polymarket CLOB orders locally with your wallet key (EIP-712),
and persists to a local SQLite DB.

## Paper | Live

One switch for the whole app, in **Settings → Account**.

- **Paper** (the default on a new install): Polymarket's real order books, imaginary money, no wallet
  needed. Your orders from the terminal and the phone, standing stops and every AI agent trade the paper
  book. Fills are walked out of the real book at the price you would actually have got, with Polymarket's
  real fees, and never mix into your real P&L. **The engines (main, crypto, copy trading, Perps, live
  scripts) send nothing.** Their switches are kept and they wait for Live. They are not simulated: to try
  an engine without money, use its backtest, and for a script, shadow mode.
- **Go live** is a checklist with the real state of each step: your wallet connected and tested, every
  switch that will start spending real money, and a tick to confirm. Each engine keeps its own switch and
  the agents their own trade mode; those can only narrow the account mode, never widen it.
- **Back to Paper** stops every real order at once, including the standing stops, take-profits and engine
  exits that manage your real positions. The positions themselves stay open on Polymarket, so the app asks
  first.

Upgrading from an older version: a terminal that was in paper mode comes up Paper, an existing setup comes
up Live with every engine's switch where you left it, and a fresh install starts in Paper. Only you can
change the account mode; an AI agent never can.

## Custom strategy scripts

The **Scripts** tab lets you write a strategy in plain Python, backtest it on the ticks the app has
already recorded, and arm it to trade — without touching the codebase or rebuilding anything.

A script defines one or more hooks. Every hook is optional except that you need at least one:

| Hook | When it runs | What it does |
| --- | --- | --- |
| `decide(ctx)` | once per coin per tick, while a crypto Up/Down window is open | return an order intent to buy a side |
| `decide_market(market)` | once per tick per top general market by volume | trade politics, sports, news — anything on Polymarket |
| `manage(position, ctx)` | every tick, per open position | `"sell"` to flatten now, or retune take-profit / stop-loss (trailing stops) |
| `decide_signal(signal)` | on each new whale / momentum signal | filter and size which signals to follow |
| `supervise(app)` | once per tick | flip **whitelisted** config (e.g. strategy posture, or switch an engine off) |
| `on_start` / `on_fill` / `on_settle` | lifecycle | keep cross-tick memory in the persistent `state` dict |

```python
# krypt-script v1
# name: Late Favorite Follow
# description: Buys the favorite in the last 3 minutes when it is 80-95c with a real book.

def decide(ctx):
    if ctx["minsLeft"] is None or ctx["minsLeft"] > 3.0:
        return None
    fav = ctx["favorite"]
    if fav not in ("up", "down"):
        return None
    ask = ctx["upAsk"] if fav == "up" else ctx["downAsk"]
    if ask is None:
        return None          # no real order book this tick — never trade a phantom quote
    if 0.80 <= ask <= 0.95:
        return {"side": fav, "price": "ask", "reason": "late favorite"}
    return None
```

`ctx` carries ~50 fields per tick — book asks/bids, the underlying's spot and realized volatility, MACD /
RSI / VWAP / EMA / SMA indicators, model probability and fee-net edge, time left in the window, and your
own `portfolio` (balance, open positions, today's P&L). Convenience fields save you the fiddly parts:
`spreadCents` (book width), `modelEdgePts` (signed model-vs-book disagreement), `favoriteAskCents` (the
favorite's real executable ask), and `timeFracLeft` (0..1 through the window — write one script that
works on the 5m, 15m *and* hourly series). Every derived field is computed by a single shared helper used
by both the live engine and the backtester, so a field can never quietly mean two different things.

**Safety rails.** Every script runs behind caps you set on the Scripts page: max entry price, max
contracts per order, max open positions per script, a per-script daily-loss breaker that auto-disables
on a bad day, and a cap on how many scripts run at once. One entry attempt per market window per
script. Any exception, malformed return, or hook that overruns its wall-clock limit auto-disables that
script and surfaces the reason in the UI — the engine loop never sees a script error. Scripts also
cannot place an order the rails would refuse: where a cap and the exchange's own minimum contradict
each other, the entry is **refused**, never rounded up past your cap.

**Full Python, audited — not sandboxed.** Scripts are ordinary Python: import what you like, use the
standard library. There is no language sandbox (the old AST whitelist blocked most useful code while
never being a real security boundary). Instead every script is statically scanned and the findings are
shown on its **Risk** tab — network access, filesystem writes, subprocesses, credential-shaped names,
dynamic execution, and obfuscation, which is flagged as critical in its own right. A flagged script
says so again when you arm it. **Read any script you did not write**, especially one an AI generated:
it runs in the process holding your decrypted wallet key. See [SECURITY.md](../SECURITY.md).

**Independent of the other engines.** A script never needs the main bot, the crypto engine or
copy-trading switched on. It fetches its own market snapshot, picks its own coins (the 15m Crypto
tab's asset checkboxes do not touch it), and the whale/momentum collector is kept running for any
script that reads signals. Shadow scripts start ticking the moment you enable one — the "Scripts
live" master switch gates *real orders*, not the engine. Each script shows whether it is actually
running and, if not, which gate is holding it.

**Beyond crypto.** `decide_market(market)` hands your script the highest-volume general Polymarket
markets each tick — politics, sports, news — with title, category, both sides of the book, spread, volume
and open interest. It buys through the same rails and lands in the same position table as the built-in
engine, so fills, resolution, and reconciliation are handled for you. The universe size is a setting
(`Markets/tick`, default 150, `0` disables it) because each market costs one sandboxed hook call per
script. Crypto Up/Down windows are excluded — `decide()` already owns those. Note this hook is **not
simulated in backtests** (the recorded corpus only covers crypto windows), so validate it in shadow mode.

**Shadow mode.** Every new script starts in **shadow**: it runs against the same live ticks and through
every rail, but the exchange call is replaced by a recorded entry — the order it *would* have sent. Those
shadow orders settle from the real window outcome with the same taker fee charged, so a script builds a
genuine *forward* track record before it ever spends money. Arming it for real orders is an explicit,
confirmed action. The account's Paper mode only stops an armed script's real orders; shadow is how a
script is paper traded, and it exists precisely where the risk is highest: code you (or an AI) just wrote. Note that shadow simulates **entries only** — exits
need a real position, so shadow orders are held to settlement; a strategy that leans on `manage()` for
its exits will behave differently once live.

**Backtesting.** Backtest any script over your recorded windows before arming it. The report is
deliberately pessimistic and ships its own caveats — resting limits are not modeled as maker fills, exit
fills into a recorded bid are flagged as optimistic (these markets frequently have *no* bid and gap to
$0), and `script_max_open` and the daily-loss breaker are not simulated, so live is a rail-limited subset
of what the backtest shows. It is also in-sample by construction: a script tuned on this panel is fit to
the past. Watch it run before arming it.

**Writing them.** The editor autocompletes `ctx[…]` field names with their documentation inline and
offers hook skeletons, and underlines errors as you type using the same validator that gates saving —
so the editor can never disagree with the engine. Scripts import and export as plain `.py` files;
imported ones arrive disabled and in shadow mode, and still have to validate before they can run.

**Writing scripts with an AI.** The Scripts page generates a copy-paste context pack — the full field
vocabulary, the return contract, your actual rails, and your data inventory — rendered *from* the live
sandbox and engine modules, so it cannot drift from what really runs. Paste it into any assistant, paste
the script back, and the validator checks it before it can be enabled.

> [!WARNING]
> **Scripts run as full Python — there is no language sandbox.** What the app does instead is a
> **static risk audit**: before you arm a script it reports credential access, network egress, dynamic
> execution, subprocess and filesystem access, and flags obfuscation as critical in its own right. That
> is a smoke detector, not a lock — a script you enable runs with the same privileges as the trading
> engine, in the process holding your decrypted wallet key. Read what you arm. The **money rails**
> (entry cap, contract cap, open cap, daily loss, one entry per window) are unconditional and are the
> actual boundary on what a script can spend.

## AI analysis

A **Read this market** button on any market page. It sends the question, the published resolution rules
and the current quote to the AI provider you pick (**Anthropic**, **OpenAI**, **OpenRouter**, **Google
Gemini**, or a model on your own machine through **Ollama** or **LM Studio**) and asks for a close
reading: what the market turns on, the traps in the wording, the dated events between now and close that
would move it, and what the model does not know.

**It will not give you a number, and that is the design.**

A language model will produce "68%" for any question you ask it, with no calibration behind it and a
fluency that reads like authority. That figure would be the least trustworthy number in this app and
would look like the most trustworthy — the same fictional-edge failure the
[Kalshi cross-venue](../README.md#what-it-does) panel refuses to print. So the model is never asked for one, and the
JSON schema it must answer in **has no numeric field at all** (`additionalProperties: false`), which
means the provider's own structured-output layer rejects an invented `probability` before the app ever
sees it. A prompt instruction can be talked out of it; a missing field cannot. Providers enforce a schema
in different ways, so the app checks every reply itself as well: an extra field, a wrong type or a numeric
value anywhere rejects the whole reply, whichever provider sent it.

The only numbers the app records about a market's chances are an **AI agent's own** forecasts, and those
are scored against the market (see [AI agents](#ai-agents-mcp)). The app never makes one.

What you get instead:

| | |
| --- | --- |
| **Lean** | `looks cheap` / `looks rich` / `looks about right` / `no view` — relative to the quoted price. `no view` is a real answer, not a failure |
| **Reasoning** | the argument behind the lean, generated *before* the conclusion (schema field order), so it reasons then concludes rather than concluding then rationalising |
| **Resolution wording** | the traps — an edge case that settles counter-intuitively, a source that may not report in time |
| **What would move it** | concrete, dated events between now and close |
| **What it does not know** | given equal weight on screen, because on a panel like this the honest list of gaps is the most useful thing on it |

If the model slips a price into a sentence anyway, that sentence is removed and **the panel tells you how
many it removed** — a silent redaction would be its own small dishonesty. The scrubber is narrow on
purpose: `"I'd put it at 70%"` goes, `"CPI came in at 2.4%"` stays, because those are the market's
numbers rather than the model's guess.

**What leaves your machine:** the market's question, its rules text, the quote, volume and close time —
public data anyone can read on Polymarket. **Never your wallet, your positions, your cost basis or your
orders.** That is enforced by the payload builder taking a market and having no access to an account, and
covered by a test that fails if any account-shaped key appears in the outbound body.

Every provider is declared in the terminal's host table, so a 429 parks it and the **Data & privacy panel
counts it** with live call/error/latency figures like every other host. The **offline switch stops the
cloud providers**; Ollama and LM Studio run on your own machine and need no key.

**Setup** (Settings → AI provider): pick a provider, paste an API key (cloud providers only), choose a
model, turn the feature on. One provider and one model serve both this panel and Autopilot. The key is
verified against the provider *before* it is stored — a typo fails there rather than as a button that
never works — and is kept encrypted alongside your wallet key, never in `settings.json` and never read
back to the screen. The model list is read from the provider with your own key, so it is always what that
key can actually reach.

Two switches, deliberately: a stored key is not consent to start sending market text to a third party.
Nothing polls — every call is one click and is billed to **your** provider account. Optional server-side
web search grounds the reading in current sources and lists what it read; with it off, the panel says
plainly that anything recent is outside what the model can see.

## Remote control

Optional Discord and Telegram bots, so the app can run on your PC while you watch it from a phone.
Both ship **disabled** and answer **nobody** until you pair an account.

```
/status /positions /pnl /balance /orders /price   read
/pause /resume /flatten /cancelall                de-risk
/buy /sell /confirm                               only if you enable remote trading
/agents /approve n /reject n                      AI agent orders waiting for you (approve needs remote trading)
```

**Setup.** Telegram: message `@BotFather`, `/newbot`, copy the token. Discord: create an application at
discord.com/developers, add a Bot, copy its token, and **enable the MESSAGE CONTENT intent** — without it
the bot receives empty messages and every command silently does nothing. Paste the token on the **Remote**
page (it is verified against the platform *before* it is stored), then generate a six-digit code and send
`/pair 123456` to your bot. Discord also accepts your user id pasted directly.

**The security model:**

- **The paired-id allowlist is the boundary, not the token.** Tokens leak — into screenshots, pasted logs,
  repos. A message from an unlisted id is dropped before it is parsed, and logged so you can see somebody
  found your bot.
- **Tokens are stored like the wallet key** — same encrypted writer, never in `settings.json`, never sent
  back to the UI.
- **Placing orders from chat is a second switch**, off by default, and it never turns on with the first.
  Even on, every order is previewed, needs a fresh `/confirm` within 90 seconds, obeys a per-order dollar
  cap enforced in the backend, and in Paper it trades the paper book like every other order.
- **Stale commands never execute.** Telegram queues undelivered updates for ~24h, so a `/flatten` sent
  while the PC was asleep is acknowledged but **not run** when it wakes.
- **Every command is logged** — accepted, refused, rate-limited or from a stranger.

**What leaves your machine:** your positions and P&L, to Discord's or Telegram's servers. That is what a
chat bot is. Both hosts appear in **Settings → Data & privacy** with live call counts, alongside the
Polymarket and Kalshi hosts and a switch that turns all outbound market data off.

## AI agents (MCP)

Let an AI client on your computer — **Claude Desktop, Claude Code, Cursor, Codex, Windsurf, VS Code** —
use the app through the Model Context Protocol, or let **Autopilot** run one for you inside the app. An
agent reads the same books, places orders through the same gates and lands in the same ledgers as a
click. **AI Agents** is the page the app opens on.

**Setup:** **AI Agents** → turn on **Allow AI agents** → click **Add** next to your client (Claude Desktop,
Cursor, Codex and Windsurf are one click; Claude Code and VS Code get a snippet to paste) → restart the
client. Keep Krypt PolyBot open while the agent works.

**Named agents.** Make up to 12 (a careful researcher, a sports specialist) and see which one is any good.
Each has a **guide** (your own words to the model), **rules** (categories from Polymarket's own tags, an
entry-price band, time to close, sides, a minimum edge, max shares in one market, max open positions, its
own per-order and daily caps) and **its own token**: the token decides which agent is calling. Start from
a template or a blank card; import, export (no token, no id) or duplicate one. The guide is text for a
model and unlocks nothing. The rules are enforced on every order, and they can only make the global rails
stricter, never looser.

**Trade mode**, one master switch for every agent:

| Mode | What agents can do |
| --- | --- |
| **Read only** | Read markets and record forecasts. Every order is refused |
| **Paper** (default) | Fill against the real Polymarket book with imaginary money, each in **its own** paper account ($1,000 by default, separate from yours), settled on the real outcome |
| **Live** | The agents you set to live spend real USDC; every other agent stays on paper. Only while the app itself is [Live](#paper--live) |

**Go live** on the AI Agents page checks your wallet, your agent's record, the caps, approvals and the
account mode on one screen, and offers **Use starter caps** ($5 per order, $20 a day, 3 positions, a $10
loss stop) for a first week.

**The rails**, enforced in the backend as the agent the token belongs to, whatever the agent says:

- **No buy without a forecast.** The agent records its own fair value for the market (`record_forecast`).
  A buy must cite one it made in the last 30 minutes, and the price must beat it by the **minimum edge
  after Polymarket's fee** (default 3¢). An unknown fee is not a zero fee: the buy is refused.
- **Caps, all agents together:** $25 per order, $100 spend per UTC day, 10 open positions (defaults; 0 =
  no cap). An agent's own caps can only be lower.
- **Daily loss stop** ($50): losses realised today plus open losses at the bid. Gains elsewhere never
  offset a loss, and live positions that can't be read pause buys. At the limit, agent buys stop for the
  UTC day.
- **Ask me before each live order**, on by default. A live order waits on the AI Agents page (with a
  desktop notification) or on your paired phone (`/approve n`, `/reject n`). It is re-checked against the
  market when you approve, counts against the daily cap while it waits, and expires after 10 minutes.
- **Ownership.** An agent sells or cancels only what it opened: its own paper book, or what its own live
  orders actually filled. It cannot close your positions or another agent's.
- **Exits are never capped or blocked**, and pausing engines is always allowed.

**Permissions, both off by default:** **Whole-account powers** (switching engines on, arming scripts or the
crypto schedule live, engine settings that spend, flattening the wallet, the Perps account's live actions)
and **Write and run strategy scripts** (an agent's script with a critical security finding is refused
outright). An agent can never change the account mode, credentials, its own caps, AI keys, remote control
or Autopilot.

**Do your agents beat the market?** Every forecast is kept and, once the market resolves, scored against
the market's own mid at the moment the forecast was made (Brier score, one forecast per agent per market).
The scoreboard shows each agent against the market and gives no verdict under 30 resolved markets, because
before that nobody can tell.

**Autopilot** runs one of your named agents on a schedule (every 60 minutes by default, or **Run now**) with
the provider from Settings → AI provider and your own key. It is just another client: the same tools,
rules, forecast gate, caps, loss stop and approvals. It is off until you switch it on, and three budgets
are checked before every call: runs per UTC day (12), tokens per UTC day (1.5M) and tool calls per run
(15). A provider that reports no token counts is charged an estimate, never zero. A deleted or
switched-off agent stops it; it never falls back to Default.

**HTTP API**, off by default. **Enable the HTTP API** opens `GET /api/v1/tools`,
`POST /api/v1/tools/{name}` and `GET /api/v1/openapi.json` on the same port, for scripts, n8n or
LangChain, with the same token, rails and audit. **Copy curl + Python** puts an example with the chosen
agent's token on your clipboard. It is the one place a token leaves the app.

**Security:** the server listens on `127.0.0.1` only, needs a token, and refuses any request from a web
page (an `Origin` header) or with a non-loopback `Host`. Client configs hold **no token**: they launch a
small relay that reads that agent's token from the encrypted store. Calls are rate-limited (120 a minute
per agent, 480 overall). Every order attempt, refusal and other action is listed under **Agent activity**,
the **Connections** panel checks the wallet, AI provider, server, HTTP API, Autopilot, chat bots and price
feeds, and secrets are scrubbed from every log line.

**What leaves your machine:** whatever the agent reads — including your positions and balance — goes to
that AI client's provider. Start on paper: no model has a proven edge, and the scoreboard is how you find
out.

## Agent Hub

Three 3D scenes of what the app is really doing, under **Agent Hub** in the sidebar. They read and never
write: nothing in the hub can place an order or change a setting.

- **Casino** — a casino hall at night. The house crew stages your real signals, orders and wins, and each
  AI agent walks to the table of every real tool call it makes. **Walk** puts you on the floor in first
  person (WASD, mouse to look, Shift to run, the doors or Esc to leave). Press **E** at a table to sit down
  and play blackjack, roulette or the slots, or at the cashier for your stack. You play on the table itself:
  click chips to bet and the lit plaques to act at blackjack, click the felt to place chips at roulette,
  press the slot machine's buttons or pull its lever, and click the sportsbook terminal or the cashier's
  ledger. These are **play chips**: 1,000 to
  start, a top-up when you drop under 50, kept in this computer's browser profile, worth nothing and never
  connected to your wallet. The **sportsbook** board is Polymarket's real sports markets at their live
  prices (refreshed every 90 s while the casino is open); a play-chip ticket pays at the price you took
  and settles on Polymarket's real result. Your enabled agents come in as guests and play the tables
  with their own stacks between jobs.

  On foot you have a hotbar: **1–5** picks empty paws, a cigarette, a beer, vodka or a revolver (**Q**
  steps through), and holding **F** (or clicking, once the mouse is captured) uses it. Smoke and let go
  to exhale, drink and watch the buzz meter (too much and you black out and wake by the door), or fire
  the revolver (**R** reloads): it leaves bullet holes and the crowd puts its paws up, but nobody gets
  hurt. They're props, like the chips.

  Across the pit from the High-Limit salon, a door in the front wall leads to **The Velvet Tail**, a
  gentlemen's club with a round stage, dancers on four poles, a cigar lounge, a DJ and a long bar. Walk up
  to the bar and press **E** to order a cocktail for play chips: it goes in your hotbar's sixth slot.
  **Space** jumps, and you can jump up onto the stages.

  The Casino has **sound**: jazz in the hall and slower jazz in the club, mixed by where you stand, plus
  sound effects for the games, the doors, the hotbar and the crowd. The speaker button next to **Walk** (or
  **M**) turns it off, and the setting is remembered. The music is by Kevin MacLeod (incompetech.com),
  licensed CC BY 4.0, credited on screen as each track starts, and bundled with the app.
- **High Stakes** — your forecasters at a poker table: each one's fair value on a market as its bet,
  scored when the market settles.
- **Race Track** — every trade races in its price lane until it settles.

## Polymarket Perps (market maker)

The **Perps** tab quotes post-only bids and asks around each chosen market's mid on Polymarket Perps.
It earns from the spread and from the exchange's liquidity rewards ($75k/day split across ~90 markets).

- **Market board** — every market with mid, spread, funding, 7-day volume, competing depth near mid,
  and an estimate of what your quote would earn from the rewards pool.
- **Risk controls** — inventory cap and skew, pull on fast moves or index dislocation, a one-minute
  markout kill per market, daily and lifetime loss limits, and the exchange's auto-cancel as a dead
  man's switch: if the app dies, its quotes are pulled within about a minute.
- **Backtest** — replays the maker over the exchange's trade history (up to 30 days), with a grid
  search over the main knobs.
- **Positions are never closed for you.** Stopping the engine pulls quotes; **Flatten** closes positions.

Be realistic: an account earns no rewards until its 7-day maker share reaches 1% of exchange maker
volume (several million dollars a week), and the backtests so far measure the default settings
net-negative. The live order path has not yet been exercised end to end — start with one market and
small size.

## Engine reliability

1. **Resolution via direct API.** Unresolved positions are reconciled against the Gamma market
   (`closed` + `outcomePrices`) and your on-chain positions directly, so a settled market no longer
   sits in `filled` forever and blocks new trades.
2. **Startup reconciliation.** On launch, `reconcile_positions_with_polymarket()` rebuilds the local book
   from your live Polymarket positions, so a crash mid-fill doesn't permanently inflate the count.
3. **Idempotent signing.** CLOB orders are EIP-712 signed locally and deduped by signal, so a restart
   never double-trades the same signal.

## Project layout

```
krypt-polybot/
├─ electron/             Main process + preload + system modules
├─ python/               Consolidated backend + tests/
│    ├─ terminal.py        provider layer: honest nulls, per-field provenance, rate gate
│    ├─ terminal_trade.py  manual orders, fill reconcile, standing instructions
│    ├─ terminal_paper.py  paper simulator (fills walked out of the real book)
│    ├─ kalshi.py          second venue, read-only
│    ├─ cross_venue.py     same-question matching + comparison
│    ├─ ai_analysis.py     AI market reading — no numeric estimate, by schema
│    ├─ ai_secrets.py      provider API keys, same encrypted writer as the wallet
│    ├─ remote.py          chat command brain: pairing, gating, audit
│    ├─ ai_providers.py    the six AI providers (cloud + Ollama / LM Studio)
│    ├─ mcp_server.py      AI agent server (MCP + HTTP API)   mcp_tools.py  its 39 tools
│    ├─ mcp_agents.py      named agents and their rules       mcp_rails.py  money rails + approvals
│    ├─ forecast_ledger.py agents' forecasts, scored vs the market   mcp_stdio.py  client relay
│    ├─ autopilot.py       runs one agent on a schedule, with budgets
│    ├─ health.py          the Connections checks             logscrub.py   secret scrubber for logs
│    ├─ perps_maker.py     Perps market maker (+ perps_api / perps_board / perps_backtest)
│    ├─ bot_discord.py     Gateway transport      bot_telegram.py  long-poll transport
│    └─ scanner/trader/crypto15m/service  the automation
├─ src/                  React UI (terminal, dashboard, settings, 15m crypto, etc.)
├─ shared/types.ts       IPC contract shared by main + renderer
├─ shared/market.ts      the TERMINAL contract — deliberately separate from types.ts
├─ resources/            Icons (krypt.png, krypt.ico)
├─ scripts/              Node-based build helpers (no PowerShell needed)
├─ build/installer.nsh   NSIS extras
└─ package.json          Electron + electron-builder config
```

## Quick start (development)

**Prerequisites:** [Node.js](https://nodejs.org) 22+ and [Python](https://python.org) 3.11+ on PATH
(`py`, `python`, or `python3`). Windows is required to build the packaged installer; development runs
on macOS/Linux/Windows.

```bash
git clone https://github.com/scripflipped/krypt-polybot.git
cd krypt-polybot
npm install
npm run dev          # `predev` auto-creates python/.venv and installs backend deps (~30s, one-time)
```

Then in the app: the first-run wizard starts you on **Paper** (real order books, imaginary money, no wallet
needed). To trade for real: **Wallet** → paste your Polygon wallet private key (the one holding your USDC)
→ it derives Polymarket CLOB API credentials and shows your balance → **Settings → Account → Go live**.
Every engine starts **off**. In Paper an engine you switch on sends nothing; once the app is Live it places
**real orders**.

Bootstrap just the Python venv without launching the app: `npm run py:setup`.

## Build a Windows installer

```bash
npm run dist         # builds the renderer, bundles the Python backend (PyInstaller), runs electron-builder
```

Artifacts land in `release/` (e.g. `Krypt PolyBot-Setup-2.0.0.exe`). Note: builds are currently
**unsigned**, so Windows SmartScreen / antivirus may warn on first run.

The build aborts if the PyInstaller bundle fails its `--selftest` (imports every runtime dep and
signs an order offline), so a backend missing a dependency can never reach an installer.

<details>
<summary>Two things that break this build locally on Windows</summary>

- **`build/` must be present.** `package.json` points at `build/installer.nsh`, `build/icon.png`, and
  `build/HOW TO USE.txt`. They are tracked, but if your working tree is missing them the NSIS step
  fails with *"cannot find specified resource"*. Restore with `git checkout -- build/`.
- **`ERROR: Cannot create symbolic link : A required privilege is not held by the client`** —
  electron-builder extracts its `winCodeSign` toolset, which contains two macOS `.dylib` symlinks.
  Creating symlinks on Windows needs `SeCreateSymbolicLinkPrivilege`, so the extraction fails and
  takes the build with it, even though those files are irrelevant to a Windows build. Fix by enabling
  **Developer Mode** (Settings → System → For developers) or building from an elevated shell. CI is
  unaffected — GitHub's `windows-latest` runners have the privilege.

</details>

## Testing & backtesting

```bash
npm run py:test                      # Python backend tests (pytest, 1,000+)
npm run typecheck                    # TypeScript (renderer + electron)
npm test                             # renderer + electron unit tests (vitest)
npm run test:e2e                     # launches the real app; covers the script arming flow
npm run py:backtest                  # fee-aware net-of-fee edge over YOUR resolved signals
npm run py:backtest -- --breakdown   #   + per-category / per-entry-price breakdown
npm run py:backtest -- --demo        # synthetic data, to see the report format
```

`test:e2e` launches the built app in a throwaway profile and asserts the flows that decide whether
user code can spend real money: a new script is shadow + disabled, arming needs an explicit
confirmation (and cancelling doesn't arm), *dis*arming is immediate, code that fails validation can't
be armed, and a script the risk audit flags says so in the arm confirmation. CI runs typecheck + the Python tests on every
push and pull request; the e2e suite is local-only for now (it needs a display, or `xvfb-run` on
Linux). **Run the backtest on your own data before trusting any strategy** — it tells you whether the scanners' signals are
net-positive *after* Polymarket fees (see [Strategies & honesty](#strategies--honesty)).

## Strategies & honesty

The strategies are **heuristic point systems with hand-tuned constants**, not validated models, and they
have **no proven, out-of-sample, fee-adjusted edge**. The entry/sizing logic does **not** fully model
Polymarket's taker fees — which can erode small edges. The built-in engines are **not simulated** in
Paper (they simply send nothing), so enabling one on a Live account risks real money. Treat the example
settings as examples, backtest them on your own
resolved signals (`npm run py:backtest`), start with a small amount, and form your own view before
sizing up. The one exception is **user scripts**, which start in
[shadow mode](#custom-strategy-scripts) and record what they *would* have traded until you arm them.

## Project status & roadmap

**Beta.** A polished, tested app — but **not** a finished "trust-it-with-real-money" product.

Done:
- [x] **Fee-aware backtest harness** (`npm run py:backtest`) — measures the net-of-fee edge of the
  scanners' signals. Run it on your own data before trusting any strategy. (On the full recorded
  corpus, 2026-09-01, every preset is net-*negative* after fees — whale-follow −2.42¢/contract,
  momentum −1.78¢, and the harness charges no spread — so the presets are example configurations,
  not edges. Each one says so in its description.)
- [x] **Test suite + CI** — 1,000+ pytest tests over the trading engine, sizing, P&L, reconciliation,
  config validation, credential encryption, the short-term crypto executor, the copy engine, the
  script risk audit / money rails, the terminal, and paper trading.
- [x] **Custom strategy scripts** — Python hooks with a static risk audit, a backtester, and per-script
  safety rails.
- [x] **Encrypted credentials at rest** (Windows DPAPI) and **server-side config validation** (clamps
  money-critical knobs before they can drive an order).
- [x] **15-minute crypto** monitor + live executor (off by default) with a stop-loss exit and a
  **percentage-based** delta filter.

Intentionally skipped:
- Code signing & auto-update (no certificate; distribute the unsigned installer or run from source).

Still open:
- [ ] **Validate a strategy forward** — confirm any preset is +EV *out-of-sample*, not just in-sample.
- [ ] A true "flatten" (sell-to-close) for the main engine (today's "Cancel All" cancels resting orders).
- [ ] Broader Electron/UI test coverage — `npm run test:e2e` now covers the script arming flow, but
  the rest of the UI is still untested, and e2e is not yet wired into CI.
- [ ] **A backtest path for `decide_market()`** — the recorded tick corpus only covers crypto windows,
  so general-market scripts can only be validated in shadow mode today.
- [ ] **Exit simulation in shadow mode** — shadow records entries and holds them to settlement, so a
  strategy that leans on `manage()` for its exits behaves differently once armed.

## File locations (Windows)

| What | Where |
| --- | --- |
| Settings | `%APPDATA%/Krypt PolyBot/settings.json` |
| Credentials | `%APPDATA%/Krypt PolyBot/credentials/` |
| Bot tokens | `%APPDATA%/Krypt PolyBot/credentials/bot.{discord,telegram}.token` (encrypted) |
| AI provider keys | `%APPDATA%/Krypt PolyBot/credentials/ai.<provider>.key` (encrypted; Ollama and LM Studio need none) |
| AI agent tokens | `%APPDATA%/Krypt PolyBot/credentials/mcp.token` (the Default agent) and `mcp_<id>.token` per named agent (encrypted) |
| MCP relay pointer | `%APPDATA%/Krypt PolyBot/mcp/bridge.json` (port + pid, no secret) |
| Database | `%APPDATA%/Krypt PolyBot/data/krypt-polybot.db` |
| Logs | `%APPDATA%/Krypt PolyBot/logs/backend.log` |

## Security

Krypt PolyBot stores your **Polygon wallet private key** (and the CLOB API credentials derived from it)
**locally**; they are never sent to any server other than Polymarket. On Windows they are **encrypted at
rest with the OS keystore (DPAPI)** — tied to your user account, with no key stored on disk in plaintext.
On non-Windows dev builds they fall back to plaintext. Anyone with your private key controls that wallet's
funds, so use a dedicated trading wallet funded only with what you intend to trade.

**Chat bot tokens, AI provider keys and AI agent tokens** are stored the same way, in the same directory,
and are never written to `settings.json`. If a bot token leaks, the paired-id allowlist still stops a stranger from issuing
commands — but revoke the token anyway (BotFather / the Discord developer portal) and unpair from the
**Remote** page.

**Outbound hosts** are listed with live call counts in **Settings → Data & privacy**, along with a single
switch that stops the terminal contacting any provider. To report a vulnerability, see
[SECURITY.md](../SECURITY.md).
