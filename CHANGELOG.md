# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Leftover shortcuts removed
The installed app no longer reloads on Ctrl+R, zooms on Ctrl+minus, goes full screen on F11 or hides
to the tray on Ctrl+W. Those came from a default menu the app never showed. Copy and paste still work.

### Links and Discord status
The app's page is now krypt.cc/tools/polyus everywhere: About, share text, the stats card and the
Discord status. The Discord status now appears if Discord opens after the app, and comes back after
Discord restarts; before, it needed an app restart. Its second button says "Join Discord", which is
where it goes.

### Wallet
Click the balance in the top bar or the account card in the sidebar to open your wallet. It reads
the exchange when it opens and again on Refresh, and says so if the read failed. Live also shows
money held by open orders and withdrawals on the way. Deposit and Withdraw give the steps for the
Polymarket US phone app, because Polymarket US offers no way to move money with an API key. Settings:
hide your balance, how often the real balance is checked, the paper starting balance and reset.

### Guide videos
The Guide page now opens with eleven short videos: a quickstart, connecting a Polymarket account,
connecting Claude or another AI over MCP, Autopilot, letting an AI trade real money, trading by
hand, writing a script, backtesting, ready-made strategies, stopping everything, and the Agent Hub
and phone control. They play offline, with captions burned in. The quickstart is also offered on the
first screen of onboarding. The written guide is still there, one click below the videos.

### The Krypt squad banner
The monster battle in the top right is gone. In its place is a green button that opens the Krypt
squad on Polymarket US.

### One click for Claude Code
Claude Code now has an Add button like the other apps, instead of a command to copy and paste. It
runs Claude Code's own setup command. Claude Code still asks before each tool it uses, because a
trade can't be undone.

### Outside sources checked
The Kalshi comparison no longer pairs a Fed question with a different size of cut or a different
meeting, and it now finds season, conference and division winners that it used to miss. When it
finds nothing it says so, without claiming Kalshi has no such market. The dead CryptoCompare price
feed is gone, CoinGecko prices keep full precision, and new installs default to Claude Opus 5.5.

### Checked against the Polymarket US API docs
Non-crypto positions were booked at the original venue's lower fee rates, overstating P&L. Every
market now uses the US rate, and rebate tiers are the US ones. The app no longer depends on the
fields Polymarket US has deprecated for settlement. Voided trades don't count as fills, halted
markets don't read as open, and the NO price is right on markets with one side quoted.

### A first screen that asks what you want to do
Onboarding now asks: let an AI trade, try a ready-made strategy, or trade yourself. It decides
where you land, and the AI setup only appears if you pick an AI.

### Download past prices
Backtest no longer starts empty. Download past crypto up/down markets from Polymarket US,
offered at the end of onboarding and on the Backtest page: up to 40 days of 15-minute and hourly
markets (about 3 weeks for ETH, SOL and XRP), or 13 days of 5-minute ones. Free public data, nothing is traded, and it carries on
after a restart. Downloaded markets have one price a minute and no order sizes, so backtest
results say how many came from here.

Results on fewer than 30 trades now say plainly that it's too few to mean anything, and scripts
hide the t-stat, which could read 20 on 5 trades.

### Paper balance
In Paper, the header, sidebar and Dashboard show your paper balance, not the real account's.
Your real balance stays in the tooltip if a key is saved. Paper money has no share buttons.

### Simple mode
Scripts and Backtest are now in Simple mode.

### Paper and Live
Paper and Live are now two buttons at the top left of the header, with the current side
lit, in place of a single pill you had to know was clickable. Live still opens the
checklist, and going back to Paper still asks first.

### Alerts

Problems now appear in the bottom-right corner, each saying what happened and what to
do, with an X to close it: Polymarket US unreachable or failing, a location refusal,
a rejected API key, the kill switch, paused trading, the AI agent server or an AI client
stopping, a crashed engine and more. An alert clears itself once the problem is fixed.

### Fixed

- A key Polymarket US rejected showed as connected. The check only looked for a saved
  key, and the Test button marked the key connected even after a 401. The app now proves
  the key with a signed read, and engines treat a rejected key as not connected.
- After the kill switch was cleared, the engines stayed paused with no way to resume
  them in the app. The paused alert now has a Resume button.

### Account setup guide

A step-by-step guide takes a new user through signing up with the invite link,
verifying their identity, adding funds, creating an API key and pasting it in. Once a
key connects, the Krypt squad page opens in the browser for a one-click join. Someone
with an account goes straight to the key. It is part of onboarding and is on the API Key page, with links from the
Dashboard, the Guide page and Settings. It remembers your place and can be skipped;
paper trading still needs no account.

### Polymarket US port (0.1.0)

Forked from Krypt PolyBot v3.5.0 to trade the Polymarket US exchange. Paper mode
is verified on live data. Signing, account reads, both WebSockets, a resting order
and real YES and NO fills have passed on a real account. Orders through the app's
own screens have not run yet. See `docs/US_PORT.md`.

- Added: Ed25519 API-key signing, a kill switch that survives restarts, order
  throttles, a price collar and a self-cross check.
- Removed, because the venue has no data or product for them: whale tracker, copy
  trading, trade tape, perps, Main Engine signals, paired mode, volume farming, the
  4-hour series and the Favorites Harvester preset.
- Everything below 3.5.0 describes the original app.

## [3.5.0] — 2026-10-12

AI agents are the front door: connect Claude Desktop, Claude Code, Cursor,
Codex, Windsurf or VS Code over MCP (or the HTTP API, or in-app Autopilot) and
let them trade any Polymarket market, on paper or live, inside the app's rails.
The full agent path has now run on real money: refusals, approval, a resting
order and its cancel, a fill, and the agent closing its own position.

### Added
- **A narrated setup video in onboarding** (2:09, captions burned in): sign up,
  export your key, connect the wallet, add Claude Desktop, go live on the AI
  Agents page with starter caps, and let it trade. Also on the Guide page.
- **Polymarket's region check, before anything is sent.** Testing the wallet
  asks Polymarket whether this connection may trade. A blocked region blocks
  Go live and every agent order; a close-only region blocks buys and keeps
  sells. The Wallet page, the header and the Dashboard say which.
- **Stop agents from anywhere.** The phone's `/pause` and new `/agents off`,
  the tray's "Stop AI agents", and a red Stop agents button in the header
  whenever agents are live on real money. The header now counts agents.
- **A desktop notification for every live agent order** (sent, filled,
  refused, outcome unknown), shown even with the window in the tray.
- **The Paper / Live badge by your balance is clickable**, through the same
  Go-live checklist and back-to-Paper confirm as Settings.
- **"Check with Polymarket"** on an agent order whose outcome is unknown: it
  books what really happened, so it stops holding the agent's caps.

### Fixed
- **Live stop-losses and take-profits sell.** A triggered stop sent a sell at
  the trigger price, which usually matched nothing, yet was reported as fired.
  It now sells at the bid within the stop's slippage; if nothing sells it
  stays armed and says it did not fire, and a partial fill keeps the rest.
  The 100-instruction list no longer deletes active stops when it fills up.
- **Orders that never filled no longer use up an agent's caps.** Refusals
  (region, balance, minimum size), unfilled fill-and-kills and cancelled
  orders held the daily spend and position slots, and a sell that missed was
  booked as sold, stranding the rest. Everything is now booked at what filled.
- **A marketable buy that fills below its limit is booked correctly.** It
  spends size × limit and takes more shares when the price is better (6 at
  20¢ filled 7.06 at 17¢ live); it was booked as 0.000007 shares and $0 spent.
- **Polymarket's $1.00 minimum for a marketable buy is checked up front**,
  with the size that works, instead of after approval.
- **Agent market reads survive Gamma's rate limit.** A 429 parked Gamma for
  30 seconds and failed every agent order in that window; the last good market
  details are reused (prices still come from the live book).
- **Paper P&L includes entry fees**, so the record shown before going live is
  net of every fee. Markets that resolve 50-50 pay 50¢ a share everywhere.
- **Updating with an AI client connected works.** The installer closes your
  own agent relays first (Claude Desktop, Cursor and others keep them open);
  after updating, fully quit and reopen your AI client so it reconnects.

### Security
- **Agent-written strategy scripts are contained.** Agent code must pass an
  allowlist (safe standard-library modules only, no introspection), is saved
  switched off for you to review, and only you can arm a script live. Agent
  backtests run in a restricted child process against a copy of the research
  data, with no access to your profile, wallet or database. Turning the
  scripts permission on asks first, and its description no longer overstates
  the protection.
- **The installer no longer adds a Windows Defender exclusion.**

### Known gaps
- No in-app update check yet: watch the release page for updates.
- Agents and engines stop when the computer sleeps.
- The engines (not agents) still price some exits in whole cents on markets
  that trade in tenths of a cent.

## [3.1.0] — 2026-10-11

### Fixed
- **Moved to Polymarket's Data API v2 before v1 is retired (2026-10-24).**
  Your balance, positions, exit booking, the copy engine, the whale feed
  and the trade tape all read v1. After the retirement the portfolio value
  would have frozen and then read $0 after a restart, so a buy would have
  looked like a loss and the daily stop would have acted on it. Every read
  now goes through v2, with its cursors walked to the end (a failed or
  partial walk raises rather than looking like positions were sold). Each
  v2 row is translated back to the shape the app stores; a test diffs that
  translation against live v1 and v2 data recorded side by side.
- **Trades the API couldn't label no longer count as "No".** The Data API
  marks some trades with outcome 999 ("could not be labeled"), at times 1
  in 10 of the newest trades on ordinary markets. They were read as No
  trades, in both directions, in the momentum scan, the whale backstop and
  the trade tape. They are now resolved from the token they traded, or
  dropped.
- **The crypto stop-loss decides on the live order book.** It read Gamma's
  outcome prices, which run 20–27¢ (up to 67¢) behind the book near the
  close: a collapsing position could go unstopped and a winner could be
  sold. It now uses the live quote (the websocket, else the REST books), as
  entries and take-profits already did, and falls back to Gamma only when
  there is no sane book.
- **A taker cross that filled nothing is no longer booked as a fill.** When
  a maker entry escalated to a taker, a fill-and-kill order that matched
  nothing reported "unmatched", which the check read as "matched", and an
  order that left the book (404) was assumed fully filled. Both booked
  contracts the wallet never held, which then flowed into P&L, the loss
  breakers and stop-loss sells. The fill is now what the wallet actually
  gained, including partial fills.
- **"All-time" P&L really is all-time again.** Balance snapshots older than
  45 days are pruned, and the all-time baseline was simply the oldest one
  left, so All-time P&L, ROI and the automatic bankroll behind the lifetime
  loss cap quietly became a rolling 45-day window. Detected deposits and
  withdrawals were never pruned, so one older than the baseline was
  subtracted twice: a flat account could show −$1,000. The first-ever
  balance is now pinned when it's first seen, and only transfers from that
  day on are netted out.
- **Strategy backtests can no longer peek at outcomes.** The script
  backtester replayed one window at a time, so when a script decided on one
  coin it had already been told how other coins closing at the same moment
  settled, and they move together. A script that copied those outcomes
  showed +2.75¢ a contract (t = 6.5) on the recorded 15-minute data; it now
  shows −3.12¢, like everything else. Every window now replays on one clock,
  outcomes reach the script only after the close (or the exit fill), and
  its open-position count is real. An honest script's result is unchanged
  (−2.07¢ on 8,201 trades before and after).
- **An AI agent's script backtests run in a separate process.** With the
  (off by default) permission to write and run scripts, an agent's backtest
  ran its code inside the backend, which holds your decrypted wallet key,
  and its result went straight back to the agent. Now it runs in a child
  process that never loads a credential: no app data folder, secret-looking
  environment variables removed, the trade database read-only, sockets
  refused, and a hard stop at the time limit. The tool is no longer marked
  read-only, so clients ask before running it and the audit log records it.
  Autopilot never gets the permission. The AI Agents page now says plainly
  that this is containment, not a sandbox: the code still runs as your
  Windows user.
- **An agent can't quietly rewrite a script you armed for real orders.**
  Saving new code over an armed script now needs whole-account powers, and
  even then the script goes back to shadow mode until it is armed again,
  with a phone notification. The script keeps your notes.
- **AI agents' money rails, tightened.**
  - An agent sending two exits at once, or setting a stop while an exit
    was in flight, could sell your own shares of the same market. Now it
    can only ever sell what its own orders bought.
  - Agents and Autopilot can no longer reset their own paper account, which
    erased their losses and lifted their daily loss stop. Resetting an
    agent's paper account is yours to do, from its card on the AI Agents
    page.
  - In paper mode, an agent's resting limit buys count toward its position
    and per-market limits, as they already did live.
  - An agent's live daily loss limit now sees fills that happened since the
    app last synced with Polymarket, so it can't keep buying after a
    resting order filled and put it past the limit.
  - Agents can no longer read the app's private settings, including a live
    phone-pairing code, through the research data query tool.
- **Paper trading behaves like the real thing.** A resting paper order fills
  at its own limit price, not the better price the market later moved to,
  and maker fills no longer show "fee rate unknown". Resting paper buys hold
  their cash, so you can't queue more than your paper balance covers.
- **Terminal and Track.**
  - Orders at sub-cent prices (near 0¢ or 100¢) go out at exactly the price
    you confirmed, not rounded to a whole cent.
  - Cancelling an order that had already filled says so, instead of
    reporting "Canceled" and marking the filled order cancelled.
  - A brief failure to look up a market no longer erases known trading
    fees, and when fees are unknown "Realised" is labelled as before fees
    instead of passing gross off as net.
  - Trades Polymarket reports as failed are no longer counted as fills.
  - Wallets with more than 2,500 fills now import their whole history over
    successive reconciles, instead of only ever the newest 2,500.
- **Crypto engine.**
  - The settlement sniper re-checks its edge at the live price just before
    buying, and never pays more than the price that still clears your Min
    net edge.
  - Paired mode sits out windows with an approximate strike price and
    pauses with the model's calibration guard, like the sniper.
  - Switching to Paper no longer lets the engine complete a pending buy: an
    entry still waiting to fill is cancelled ("pulled (Paper)"). Positions
    you already hold keep their stop-loss and take-profit.
- **Session-key wallets keep signing after a restart.** Before, every order
  was rejected until you clicked Test. If an order is still rejected for its
  signature, the app re-checks the wallet once and, only if the answer
  changes how the order must be signed, re-signs it once.
- **The Perps market maker no longer stacks quotes** when a cancel fails or
  goes unconfirmed. It arms the exchange's dead man's switch before placing
  anything, backs off on rate limits, and renews its trading key before it
  expires (or pulls its quotes while the key still works). Note that it
  renews the key on its own, signing with your wallet about once a week
  while it runs. The live path is still untested with real money.
- **Exit fees are charged once.** Positions you closed yourself on
  Polymarket, and copy trades closed after their shares were gone, paid the
  exit fee twice in their P&L.
- **Research data is archived, not deleted.** Crypto signals and ticks, whale
  and momentum signals older than 45–60 days now move into a separate
  research-archive.db beside the database, which is never pruned and
  survives a factory reset, and backtests read from it (about 2 MB a day at
  your recording pace).
- **Backtests that find nothing say why.** A replay with no recorded windows
  now leads with the interval it needs, the one being recorded and how to
  switch, instead of "Only 0 trades". The Backtest page labels each crypto
  strategy with its interval and warns before the run, and an empty result
  (there and on the Scripts page) shows "—", not a green 0.00¢ edge.
  The Settlement Sniper's +3.2¢/91% figure is marked as measured before the
  Aug 7 switch to TWAP settlement.
- **The Guide explains Today P&L correctly** (your balance change since
  00:00 UTC, net of detected deposits and withdrawals), and the Terminal's
  TODAY chip shows a dash before the day's first snapshot instead of +$0.00.
- **Your Terminal stop-losses and take-profits trigger on the live book.**
  They read Gamma's price whenever the app's own feed had no fresh quote,
  and Gamma runs 20–67¢ behind near a close: a collapsed position waited and
  a recovered one could be sold. Each check now reads the order book first.
- **Orders.**
  - A send that times out or gets a server error is looked up by its order
    hash. If it still can't be found, the entry keeps the order's id as
    "unknown" so it can be reconciled, instead of being forgotten while it
    might be resting.
  - Looking up an order's fills finds maker fills, books our own side of
    the trade, reads every page, and skips failed trades.
  - A crossing order that comes back "delayed" shows as matching, not as
    resting with nothing filled, and scripts book it as a taker fill.
  - The order body no longer carries an extra `taker` field, so it matches
    Polymarket's own SDK exactly. Signatures are unchanged.
  - Deposit wallets read their real exchange approvals instead of assuming
    them whenever the balance is above zero.
  - Markets on Polymarket's coming protocol version are refused before
    signing, with a plain reason, until the app can sign for them.
  - The self-test now checks signatures against fixed known-good values,
    and the signing library is held to the version they were checked on.
- **Market data.**
  - Markets keep Gamma's real best bid and ask instead of the midpoint on
    both sides, so scripts see a real spread.
  - The whale backstop asks Polymarket for whale-sized trades only (each poll
    used to cover under a minute of a five-minute-old tape), and whale
    alerts carry the trade's own time, so a late find no longer looks fresh.
  - If your portfolio value can't be read for five minutes it is treated as
    unknown, not frozen: no balance points are saved and the daily stop
    won't act on it.
  - A followed wallet too large to read in full is named on the Copy page
    and re-checked every 10 minutes, instead of being re-read every tick.
  - The live book feed treats an empty side as empty, and adds new markets
    to the open connection instead of reconnecting for each one.
- **Crypto engine.**
  - The model sits out when the spot price is stale, and an hourly market
    (which settles on Binance) needs Binance's own price.
  - The price feed returns to PolyBolt after routine reconnects and when the
    old RTDS fallback stops answering.
  - The recorder checks each captured strike against Polymarket's official
    one once the window closes, and logs any mismatch.
- **AI agents.**
  - Forecasts, paper marks and both daily loss stops are priced from the
    live order book in one batched read, with Gamma only as a labelled
    fallback.
  - If a live close fails after it may have reached Polymarket, it is
    recorded as unknown, so a second close can't sell your shares.
  - The forecast scoreboard scores the forecast a buy actually cited, not a
    later rewrite of it.
  - The MCP server speaks the current protocol revision (2026-07-28)
    alongside the older handshake current clients use. Agents connected
    through the relay get their own guide and rules, and a deleted agent
    is told so instead of "the app is closed".
- **Paper trading.**
  - The backend reads Paper, not Live, before your settings first load.
  - A paper order cancelled while fills were being processed stays
    cancelled, and resting paper orders are cancelled when their market
    resolves, releasing their cash.
- **Polymarket's books card** counts "All" from the wallet's first trade (the
  first stretch of trading was dropped), and fees paid show as a positive
  amount.
- **Remote control.** Strangers messaging the bot get their own log, one row
  per sender with a count, so they can't push your commands out of the log.
  A stranger is answered once, and five wrong pairing codes from anyone
  cancel the code.
- **Backtests.**
  - The main-engine replay applies the engine's own entry caps (open, daily,
    per event, one per market), so a hot event can't stack trades the
    engine would refuse.
  - Significance is clustered by settlement time (windows that close
    together move together), and each average is shown next to its own t.
  - Model-mode replays name trades from before the Aug 7 TWAP switch, and the
    parlay generator chooses on its training data only.
  - Every measured number in the crypto presets names its data and dates.
  - The whale backtest charges a 1¢ spread by default and prints a 0/1/2¢
    sweep; trend fields no longer peek up to a minute ahead; the crypto
    backtest header names the recorded interval.
- **Fees.** Realised fees charge the full venue fee when a rebate tier is set
  (Polymarket pays rebates separately). Climate, world and entertainment
  markets use the schedule's weather, other and culture rates, and the
  Terminal prices them instead of calling the fee unknown.
- **Perps.** The inventory cap counts the whole quote ladder and fills it
  hasn't seen yet. An unreadable account is an error instead of an empty
  one, and Flatten says what it couldn't close.
- **A deeply nested strategy script could crash the backend.** Builds with
  eth-account 0.14 (every CI-built installer) load `py_ecc`, which raises
  Python's recursion limit to 100,000 for the whole process. On Python 3.11
  one long line of `1+1+…` then overflowed the stack while the script was
  being checked, and the backend died with no error. The limit is now put
  back before any script is parsed, and such a script is refused with a
  reason, as it always was meant to be.
- **Crypto up/down model now matches how the markets actually settle.**
  Since 2026-08-07 these markets resolve on Chainlink's 60-second TWAP
  (Up when TWAP(close) ≥ TWAP(open)), not on a single print. The model now
  takes as its strike the official TWAP point at the window open, recovered
  from the feed's 2-minute replay even after a late start. In the final
  minute it prices only the unseen part of the closing average: a late spike
  can no longer make it "certain" of a window whose average is mostly in.
  Model entries refuse a strike that is only a spot print.
- **Price feed moved off RTDS before its shutdown.** Polymarket deprecated
  RTDS price topics on 2026-09-23 (removal about a month later). Prices now
  come from PolyBolt, using the CLOB API key, with RTDS as fallback.
  - Measured: PolyBolt's Chainlink spot runs about 20 seconds behind real
    time, so it is never served as live spot. The live spot is the Pyth pin
    (lag about 0s), served in Chainlink terms via a self-calibrating basis
    (+0.3 to +0.65 bps), so it can't bias calls against a Chainlink strike.
  - The official TWAP is streamed for every asset.
- **"Hourly" crypto markets were never found.** Their slugs are ET-dated with
  the year (`bitcoin-up-or-down-october-5-2026-6am-et`), and they settle on
  the Binance 1-hour candle, not on Chainlink. Both are now handled: Binance
  spot, a candle-open strike, and the point-in-time model.
- **Session-key signers.** Orders signed by a deposit wallet's session key
  now carry the envelope the wallet requires. They were previously sent bare
  and rejected, even though the key check passed.
- **Per-outcome redemptions.** Since 2026-08-10 `/activity` reports a
  redemption per outcome. A $0 REDEEM row for our side is now booked as the
  settled loss it is, instead of leaving the position open forever.
- **Exchange restarts.** A `425` restart or the 2-minute post-restart
  post-only window now pauses orders with a clear reason and a top-bar
  badge, instead of a stream of opaque rejections. Resting orders go out
  post-only during the window.

### Added
- **Agent Hub: the Penthouse.** Only the agents you set up, each at its own
  desk of five monitors in a top-floor unit over the city at night:
  - its live tool calls, refusals with the rail that refused them;
  - the last market it touched, with its fair value against the mid and the
    edge after fees;
  - its forecast record from the scoreboard;
  - its activity over the last 30 minutes;
  - its mode, caps, rules and orders.

  An eight-screen video wall carries the account, open positions, signals,
  every agent call (approvals too), settled trades, the time and the agents'
  global rails. The agents (the same rats, drawn clean, in hoodies and
  headsets) act out each real call as it lands, and smoke and drink energy
  drinks between calls; a switched-off agent sleeps at its desk. One empty
  can per 20 calls this session and one butt per refusal. Click a desk to
  sit in; Cinematic cuts to whichever agent just called. Walk it in first
  person: E at a desk leans in over the agent's shoulder, and the elevator
  is the way out. A purple haze lies over the city below, with low glow.
- **4-hour crypto windows** (`btc-updown-4h-*`) and **ZEC** as an asset.
- **Taker rebate tier** setting. Fee estimates (edge gates, backtests) net
  out the Taker Rebate Program; realised P&L still shows fees paid.
- **Live order/fill push (user WebSocket).** Wakes the crypto engine the
  moment one of our orders is matched or cancelled; REST stays
  authoritative.
- **Batched cancels.** Cancel-all now uses one `DELETE /orders` request
  instead of one request per order.
- **Data API v2.**
  - **Polymarket's books** on Track: the wallet's P&L as Polymarket computes
    it, including the maker/taker rebates and rewards it pays separately.
  - **Settlement ETAs** on positions that are mid-resolution.
  - **Top holders with P&L** on each market.
  - **Biggest winners** with one-click Follow on Copy Trading.
- **Full-text market search** via Polymarket's `/public-search`.
- `py_clob_client_v2` floor raised to 1.1.0 (the July order-response change).
- **Perps — a market maker for Polymarket Perps.** A new **Perps** page
  (Automation → Perps) lays out like the Crypto tab: an engine switch, presets,
  simple/advanced settings, market selection, per-market cards and a market
  board. The engine rests post-only bids and asks around each market's mid to
  build maker volume and earn the $75k/day liquidity-rewards pool.
  - **Choosing markets.** Auto-pick by expected reward share (thin books) or by
    volume (deep books), or tick markets by hand. The board scores every market
    the way the rewards program does: the pool, everyone else's
    reward-weighted depth near mid, and your estimated share for your quote
    size. It also shows spread, volume, open interest, funding and the mid's
    gap to the index. The board works without an account; unlinked, the cards
    preview the quotes the engine would place.
  - **Tuning.** Quote distance (0 = join the touch), size, a 1–3 level ladder,
    an inventory cap with skew toward unloading, a requote band that keeps
    queue priority, pull-on-fast-move, pull on index dislocation, leverage and
    margin mode.
  - **Safety.** Daily and lifetime loss stops and a daily volume target. A
    per-market kill switch trips when maker fills average worse than −X bps one
    minute later. The exchange-side auto-cancel is re-armed every 15s, so
    quotes come down within a minute if the app dies. The engine only touches
    orders it placed, and never closes positions on its own; Flatten does that.
  - **Rewards eligibility bar.** Your 7-day maker volume against the 1%-of-
    exchange threshold the program requires before it pays anything.
  - **Account.** Linking takes one wallet signature, which authorises a
    7-day trading key stored encrypted next to the wallet key. Not available
    from the US or Canada. Presets (Reward Farmer, Volume Builder, Two-Tier
    Ladder, Cautious) are starting shapes, not tuned strategies.
  - **Live prices.** A WebSocket feed of best bid/offer and trades wakes the
    engine the moment a quoted market's mid moves, and only that market is
    requoted from cached state (at most every 200ms). Full ticks still
    re-sync orders and positions every `perpsPollSec`; REST is the fallback
    whenever the feed is down or stale. Cards show whether a quote came from
    the live feed or REST.
  - **Backtest and optimizer** (Advanced). Replays the exchange's real trade
    tape, up to 30 days, through the engine's own quote planner. History is
    backfilled from REST once, cached and paced under the per-IP rate limit
    so the live engine is never starved, and topped up by the live feed's
    recorder. Optimize sweeps quote distance × size × skew (up to 36 runs);
    one click applies a row. Results show net P&L/day, P&L per $1M of volume
    (what volume costs), estimated rewards, markout, fees and funding, with
    per-market equity curves. The fill model is deliberately conservative and
    its assumptions are listed under the results.
  - **Perps profiles.** Save from the Perps tab or Profiles → Perps. Applying
    a profile never switches the engine on, and main-engine profiles and
    strategy presets never touch Perps settings.
- **AI agents: your own named AI traders over MCP.** A new **AI Agents**
  page, now the page the app opens on, runs a local Model Context Protocol
  server inside the backend. Claude Desktop, Claude Code, Cursor, Codex,
  Windsurf, VS Code or any MCP client reads the same books, places orders
  through the same gates and lands in the same ledgers as a click.
  - **Named agents.** Up to 12, each with a name, a guide (your own words to
    the model) and hard rules: categories from Polymarket's own tags, an
    entry-price band, time to close, sides, a minimum edge, max shares in one
    market, max open positions, and its own per-order and daily caps. Start
    from a template (Careful Researcher, Sports Value, Crypto Forecaster,
    Contrarian, Closing-Soon Grinder) or a blank one; import,
    export (no token, no id) and duplicate. The guide unlocks nothing: the
    rules are enforced on every order whatever it says, and they can only
    narrow the global rails, never loosen them.
  - **One token per agent.** The token decides which agent is calling, on
    MCP, the stdio relay and the HTTP API alike. A switched-off or deleted
    agent is refused at once. The built-in Default agent keeps the original
    token, so an existing client setup keeps working.
  - **Trade mode: Read only | Paper | Live.** One master switch for every
    agent, Paper by default. Read only refuses every order. Paper fills
    against the real Polymarket book with imaginary money, in each agent's
    own paper account ($1,000 each by default, separate from yours), and
    settles on the real outcome. Under Live, only the agents you set to live
    spend real USDC, and only while the app itself is Live. A `mode`
    argument on a tool can confirm the mode, never choose it.
  - **No buy without a forecast.** A buy must cite a fair value that agent
    recorded on that market (`record_forecast`) in the last 30 minutes, and
    the price must beat it by the minimum edge after Polymarket's fee
    (default 3¢). An unknown fee is not a zero fee: the buy is refused.
    Every forecast is scored against the market's own mid at the moment it
    was made, once the market resolves. **Do your agents beat the market?**
    shows paired Brier scores per agent and declines a verdict under 30
    resolved markets. The app's own AI analysis stays number-free; only the
    agents' numbers are recorded.
  - **Money rails, all agents together.** Max per order ($25), max spend per
    UTC day ($100), max open positions (10), and a daily loss stop ($50:
    realised today plus open losses at the bid; gains elsewhere never offset
    a loss; unreadable live positions pause buys). Exits are never capped or
    blocked. Vet, send and record run under one lock, so parallel calls
    cannot both slip under a cap that fits one.
  - **Ask me first.** On by default. A live agent order waits on the AI
    Agents page (with a desktop notification) or on your paired phone
    (`/agents`, `/approve n`, `/reject n`) until you answer. It is re-checked
    against the market at approval, counts against the daily cap while it
    waits, and expires after 10 minutes. Approving by phone needs remote
    trading on; rejecting never does.
  - **Ownership.** An agent sells or cancels only what it opened: in paper,
    its own book; live, what its own orders actually filled. It cannot close
    your positions or another agent's.
  - **Permissions, both off by default.** *Whole-account powers*: switching
    engines on, arming scripts or the crypto schedule live, engine settings
    that spend, flattening the wallet, and the Perps account's live actions.
    *Write and run strategy scripts*: any script of an agent's with a
    critical audit finding is refused outright. Turning things off and
    pausing engines is always allowed, and pausing (from an agent or the
    phone's `/pause`) now stops the Perps maker too. Agents never change the
    account mode, credentials, their own caps, AI keys, remote control or
    Autopilot. Every config key is classified; an unclassified one is
    refused.
  - **40 tools, 4 prompts.** Market discovery, search, book, history, tape,
    resolution risk and Kalshi; watchlist; any trader's positions and
    activity; portfolio and orders; order preview, place, cancel and close;
    stops, take-profits and alerts; engine pause and switch; engine settings
    via `update_config` (typed, clamped); backtests (crypto, main engine,
    scripts); the per-hour parlay schedule; read-only SQL over the recorded
    research data; the script API; the Perps market maker (`perps`); and
    `get_my_agent` / `record_forecast`. The prompts (analyze a market, paper
    session, review performance, build a crypto strategy) show up as slash
    commands in clients that support them. Rate limited to 120 calls a
    minute per agent and 480 overall.
  - **One-click setup per agent.** Add writes that agent's entry into Claude
    Desktop, Cursor, Codex or Windsurf's own config (timestamped backup,
    atomic write; an unparseable file is left alone); copy-paste for Claude
    Code and VS Code. Entries run `krypt-polybot-backend --mcp --agent <id>`,
    a stdio relay that reads that agent's token from the encrypted store:
    no secret in any client config. A client shows as connected on its
    first call.
  - **HTTP API**, off by default (*Enable the HTTP API*):
    `GET /api/v1/tools`, `POST /api/v1/tools/{name}` and
    `GET /api/v1/openapi.json` on the same listener, with the same token,
    rails and audit, for scripts, n8n or LangChain. Its curl/Python snippet
    is the one place a token leaves the app, and it goes straight to the
    clipboard.
  - **Loopback only, browsers refused.** `127.0.0.1:<port>`, bearer token
    stored like the wallet key, and any request with an `Origin` header or
    a non-loopback `Host` is refused (DNS-rebinding defence). The renderer
    never receives a token.
  - **Watch it work.** Agent activity lists every order an agent attempted,
    refused ones with the reason, and every other action it took. A live
    stream of tool calls (no raw arguments or free text) feeds the page and
    the Agent Hub. Agent orders carry `origin='ai'` ("AI agent" in Track).
  - **Connections.** One panel checks the wallet, the AI provider, the MCP
    server, the HTTP API, Autopilot, Discord/Telegram and the price feeds.
  - **Secrets never reach the logs.** Every log line passes a scrubber that
    knows the wallet key, CLOB credentials, the Perps key, agent and bot
    tokens and AI keys, plus the usual shapes (`sk-`, `AIza`, PEM blocks,
    auth headers).
  - Frozen-build `--selftest` now fails if the tools, the loopback guard,
    the HTTP API's off state, the stream whitelist, the scrubber, the
    account gate or the config classification regress.
- **Paper | Live for the whole app.** One account mode, in Settings →
  Account, replaces the manual terminal's paper switch. **Paper** is the
  default for a new install: Polymarket's real order books, imaginary money,
  no wallet needed. The terminal, the phone, standing stops and every AI
  agent trade the paper book, and the engines (main, crypto, copy trading,
  Perps, live scripts) send nothing. Their switches are kept; they wait for
  Live. The engines are not simulated in Paper. **Go live** is a checklist
  with the real state of each step: the wallet tested on a click, every
  switch that will start spending, and an explicit tick. Going back to
  Paper asks first, because it also pauses the standing stops, take-profits
  and engine exits that manage real positions (the positions stay open on
  Polymarket). PAPER pills in the sidebar, top bar and dashboard. Only you
  can change it; an agent never can.
- **Six AI providers.** Anthropic, OpenAI, OpenRouter, Google Gemini, and
  two that run on your own machine with no key: Ollama and LM Studio. One
  provider and one model (Settings → AI provider) serve the market analysis
  and Autopilot. Every analysis reply is checked before it is shown: an
  extra key, a wrong type or a number anywhere rejects the whole reply, for
  every provider.
- **Autopilot: the app runs one of your agents on a schedule.** Off until
  you switch it on, and it uses your own AI key. It is just another MCP
  client: the same tools, named-agent rules, forecast gate, caps, loss stop,
  trade mode and approval queue. It adds no order path of its own. It runs
  as a named agent; a deleted or switched-off agent stops it, and it never
  falls back to Default. Three budgets are checked before every call: runs
  per UTC day (12), tokens per UTC day (1.5M) and tool calls per run (15).
  A provider that reports no token counts is charged an estimate, never
  zero. Every 60 minutes by default, or **Run now**; a quickstart walks
  through the first run. Every run is recorded with model, steps, tokens,
  each tool it called and its own report.
- **Onboarding and an app tour.** A first-run wizard: welcome, the
  disclaimer, connect an agent (or pick Autopilot), then start on paper or
  connect a wallet, and an optional Autopilot setup. Nothing changes unless
  you act in a step. It shows once per revision, so an existing user sees
  it once after this update as "New in this version". Settings → Getting
  started has **Take the tour** and **Replay onboarding**.
- **A liquid-glass look, and the Agent Hub.** Title bar, sidebar, top bar,
  cards, switches and dialogs are glass over an aurora backdrop. Every
  decorative animation runs on one ~15 Hz clock and pauses while the window
  is hidden (idle compositor frames 240/s → 15/s). Settings → Appearance:
  **Auto** (full glass, stepping down to Frost only if this machine needs
  it), **Live**, or **Frost** (no refraction, the lightest on the GPU).
  Fonts are bundled, so the app no longer calls Google Fonts. The **Agent
  Hub** (replacing the Live Visualizer) shows your agents at work in three 3D
  scenes, loaded only when you open it: the **Casino**, the **High Stakes**
  poker room (forecasts as chip bets, a WHO CALLED IT? leaderboard) and
  the **Race Track** (each trade runs in its price lane).
- **The Casino is a casino you can walk and play.** An open hall
  replaces the old corridor-of-rooms floor: the doors and foyer, a carpet
  loop round a table-games pit, a sportsbook with a video wall and betting
  windows, a glass high-limit salon (where the real orders leave for
  POLYMARKET), the cashier cage, a slot hall, a round bar and the VIP lounge.
  The house crew still stages only what really happened in the app.
  - **Play on foot** (Walk, then E at a table). Blackjack (six decks, the
    dealer stands on all 17s, 3:2, double, one split; your cards land on the
    3D felt, and the table suggests basic strategy), European roulette (the
    ball drops into the real pocket on the 3D wheel), slot machines (the 3D
    reels stop on your line; 94% return to player, computed exactly from the
    strip and printed on the machine), and the cashier. **Play chips only**:
    1,000 to start, a top-up under 50, kept in this browser profile. They
    never touch your wallet, an order or the backend.
  - **The sportsbook is Polymarket.** Its board and windows show Polymarket's
    real sports markets at their live prices and odds: games in the next week
    first, at most two markets per match, read through the backend every 90 s
    while the casino is open. A play-chip ticket pays at the price it was
    written at, and settles only on Polymarket's real resolution (checked
    every 5 minutes while the casino is open).
  - **Your agents play between jobs.** Your enabled named agents come in as
    guests, each with its own chip stack, and play blackjack (basic
    strategy), roulette and the slots; a broke agent walks to the cage for a
    refill. A real tool call still pulls an agent off its game to the table
    that call belongs to, and it walks back to its game after.
  - **A PS2-era look on foot, and a casino of mob rats.** Walking the floor,
    the 3D frame is drawn at about 480 lines with hard pixels, 15-bit colour
    and an ordered dither, no anti-aliasing, and a smoky haze; the HUD and
    the tables' panels stay sharp. The isometric overview stays clean: full
    resolution and anti-aliased. Everyone on the floor (the house crew, your agents,
    and Bluey the pit boss) is the same low-poly, flat-shaded rat in a
    fedora and a pinstripe suit, the role still in its colour (your agents
    wear cream fedoras banded in theirs). About half of them smoke, with
    glowing embers and real smoke; Bluey has a cigar. The neon glow is
    turned down.
  - **A hotbar on foot: a cigarette, a beer, vodka and a revolver.** Keys
    1–5 pick (Q steps through); hold F, or click once the mouse is
    captured, to use. Hold a drag on the cigarette and let go to blow the
    smoke out ahead of you; it burns down, and a fresh one gets lit. The
    beer and the vodka go to your lips while you hold, and the buzz adds up
    (a meter shows it): the view sways, you see double, you don't walk
    straight, and too much blacks you out — you come to by the door. The
    revolver fires six, kicks, flashes the room and leaves a hole where it
    lands; R reloads. Nobody gets hurt: point it at a rat and it puts its
    paws up; fire and the whole room does, the one it nearly hit yells
    about its whiskers, and Bluey yells from the podium. All of it is
    props, like the play chips: nothing here touches the wallet, an order
    or the backend.
  - **The Velvet Tail, a gentlemen's club behind the front wall.** A pair
    of padded doors under a pink neon sign, across the pit from the
    High-Limit salon, swing open as you walk up into a room of 23 × 18 m
    under a 5 m ceiling, kept dim and red. In the middle is a round main
    stage with a pink underglow and marquee bulbs, chairs ringing it, and a
    runway back to the curtain. A ring of stage lights overhead works the
    dancers, and there's a go-go podium either side. The Tailfeathers dance
    on four poles in sequinned two-pieces with beaded fringe belts, pearls,
    bangles, heels and feathered headbands, working through spins, climbs,
    sways and hooks. Down the left is the cigar lounge: sofas, club chairs,
    patrons who applaud a good spin, and the humidor. The VIP booth is back
    right, and DJ Gramps is on his riser back left. Down the right is a
    long backlit bar: walk up, press E, and order a cocktail for play chips
    (a Dry Martini, Old Fashioned, Manhattan, Cosmo, Negroni, or the house
    special). The bartender shakes it, and it lands in a new sixth hotbar
    slot, in its own glass with its garnish, to drink like the beer and the
    vodka, each with its own kick. Dot, Knuckles and Gramps can be talked
    to. Fire the revolver in there and the record scratches to a stop,
    everyone freezes with their paws up, and Knuckles has words (he has
    some if you get up on the stage, too). On foot only: the overview is
    still the hall.
  - **Jumping, and stages you can get up on.** Space jumps when you're on
    foot (in High Stakes too), about three quarters of a metre, with a dip
    and a thud on landing. The club's stages, podiums and DJ riser are
    raised floors: they're walls from below, but jump onto them and you're
    standing up there, and walking off the edge drops you back down. Walls
    are still walls.
  - **You sober up half again as fast** as before.
  - **Talking, PS2-style.** Speech bubbles are hard-edged beveled boxes
    with a stepped tail and bold, hard-shadowed text, coloured by mood, and
    they wipe open the way PS2 windows did. A conversation is a box along
    the bottom with the speaker's name on a tab, a blinking ▶ on your
    choice and a ▼ when a line waits for you. The "Talk to…" and "Play…"
    prompts use the same box, with the key as a button glyph.
  - **Walk away from anything.** Press WASD or an arrow key mid-
    conversation, at a table, at the bar's menu, a sportsbook station or
    the cashier, and it ends on the spot: you're already walking, and
    mouse-look comes back. (A key you're still holding from walking up
    doesn't count.) Esc ends it too and leaves the cursor free until you
    click. The games' own keys moved off WASD and the arrows to make room:
    blackjack is H hit, Space stand, X double, P split, − + for chips;
    roulette, the slots and the sportsbook use − + for chips or stakes,
    the wheel, and , . to step; the bar's menu uses 1–6 or the wheel.
  - **In fullscreen, the controls sit in the bottom-right corner**, clear
    of the hotbar.
  - **The Velvet Tail's backstage has a crew.** Three rats in nothing but
    red high-tops (red, white, a black swoosh) dance one routine in sync
    across the back of the stage, under spots that change colour on the
    beat. Moves run eight beats each at 120 to the minute: raise the roof,
    an arm wave that ripples down the line, the running man, the floss, a
    kick line, jump-claps, spins, a shuffle where the whole line slides,
    a body roll and dabs. Twice a run they strut one mark along, the end
    one looping round behind, and the room applauds at the end of each
    run. They freeze with their paws up if a gun goes off.
  - **The patrons round the stage sit in chairs.** They were sitting on
    nothing.
  - **The round bar in the hall serves.** Lou works the well, drifting
    round it and turning to whoever's ordering. Walk up between the stools
    and press E for the same cocktail menu as the club, or talk to him.
    The BAR sign over it turns all the way round.
  - **The Old Fashioned's glass** is a proper heavy tumbler (real walls, a
    thick base, one big rock of ice), with the twist of orange on its rim.
    Every garnish now sits on its own glass's rim and moves with it.
  - **The games are played on the tables, not in a panel.** Sit down and
    the camera frames the table, which is now the interface; the pop-up
    panels are gone, leaving one thin line of keys along the bottom.
    - *Blackjack:* your chips fan out on the felt. Click one to add it to
      the betting circle (click the stack to take one back). Lit acrylic
      plaques along the rail (DEAL, HIT, STAND, DOUBLE, SPLIT) light up
      when they do something. The cards land where the dealer deals them,
      totals float over them with basic strategy's call, and your stack is
      paid beside it and slides to you, or is swept away.
    - *Roulette:* pick a chip off the rail and click the felt itself, on a
      number, a colour, a dozen, an even-money box or one of the new 2-to-1
      column bets. The chips stack where you put them (UNDO and CLEAR are
      on the rail). SPIN turns the camera to the wheel to watch the ball
      drop; then a marker sits on the winning number, winners are paid and
      slide to you, and losers go to the wheel.
    - *Slots:* the machine fills the view. Its own meter shows CREDITS,
      BET and a WIN that counts up, beside its pay table. Its buttons
      (BET−, BET+, SPIN) are on the deck, and the lever really pulls. The
      reels show three big symbols each, the shape they're meant to be
      (they were a thin drum carrying all 22, squashed five to one), and
      the payline is two arrows at the window's edges, no longer a bar
      across the symbol it marks.
    - *The sportsbook* has a wall of eight monitors in place of the one
      board that paged through markets: YOUR BETS (every ticket, the open
      ones marked to the live price) and seven screens of five markets
      each, in a fixed order, so nothing scrolls away before you've read
      it. Sit at a window or the kiosk and you get a betting station of
      three monitors: the markets on the left (click one), the bet slip in
      the middle (a side, the stake, PLACE TICKET), and your bets on the
      right. The sportsbook is drawn sharp, not pixelated, while you're in
      it or at a station, and so are the cashier and the bar.
    - *The cashier and the bar* are screens in the world: a ledger on the
      cage's counter with a brass bell to ring for a refill, and the bar's
      menu card. You click them directly.
    - Hover anything you can click and it lights up, with a tooltip. The
      wheel steps your chip size, and the frame is sharper while you play.
  - **Sound.** Jazz plays while the Casino is open: "Lobby Time", "Bossa
    Antigua" and "Backbay Lounge" in the hall, and "Smooth Lovin" and "Bass
    Walker" in the club, all by Kevin MacLeod under CC BY 4.0. Each track
    is credited on screen as it starts (and in
    `src/assets/audio/CREDITS.md`). The tracks are bundled with the app
    (about 7 MB), so nothing streams. The two rooms are mixed by where you
    stand: through the club's doors its music is a muffled thump that opens
    up as they swing, and from inside the club the hall goes muffled
    instead. Everything else is synthesised: the revolver, its reload and
    dry click, the lighter, a drag and the exhale, swigs, the vodka cough,
    a fresh bottle, cards, chips, the roulette ball, the slot reels and
    bells (your agents' games too, from where they play), the doors,
    applause, and a squeak of "voice" under every speech bubble. A speaker
    button by Walk (or M) turns it all off, and the setting is remembered.
- **High Stakes' chairs face the table.** Each club chair had its backrest
  on the felt side; the backrest is now behind the player, who sits on the
  cushion.

### Changed
- **Main Engine presets are now example settings.** Edge Stack, Crypto Whale
  and Krypt Aggressive moved out of the top of the page into a collapsed
  **Example settings** section, because every one measured negative on the
  full recorded corpus. Each card now shows its measured result in plain
  sight (a new `measured` field, mirrored in `config.py` and pinned by
  `test_presets.py`). The button reads **Load example**, and the card reads
  **Loaded** while your settings match. Loading one still never turns the
  engine on. The Guide's "pick a preset" step is now "tune the settings".
- **The app opens on AI Agents**, then on the last page you used (a page
  that crashed is forgotten). The sidebar's Agents group comes first.
- **Existing setups keep trading as they did.** A settings file from before
  the account mode comes up Paper if the terminal was in paper mode, Live
  for an existing setup otherwise (every engine's own switch stands), and
  Paper for a fresh install. The terminal's paper switch is now derived from
  the account mode and can't disagree with it.
- **One AI model setting.** `aiAnthropicModel` / `aiOpenaiModel` became one
  `aiModel` for the selected provider, migrated on load.
- **Profiles never carry agent, AI, remote, Autopilot or account settings.**
  Snapshots, apply, export and import all leave them alone.
- **Pre-release agent builds migrate forward, never wider.** The single
  *Allow live trading* switch became trade mode Live with whole-account
  powers on, and approvals now on by default; `mcpDailyLimitUsd` became
  `mcpDailySpendUsd`. The window never receives a token any more: the
  token can't be revealed on screen, and a copied client setup or HTTP
  snippet goes to the clipboard straight from the app's main process.

## [3.0.0] — 2026-09-01

### Removed
- **The four Discord webhooks and the anonymous community leaderboard are
  gone.** Both sent your trading activity to somebody else's server, which is
  the one thing a local-first app should not do quietly: a webhook posted your
  positions to a Discord channel on every fire, and the leaderboard posted your
  P&L to Krypt's own endpoint on a timer.

  Deleted outright — `webhook.py`, `leaderboard.py`, their config keys
  (`eventWebhookUrl` / `statsWebhookUrl` / `whaleWebhookUrl` /
  `momentumWebhookUrl` / `enableDiscord` / `statsPushInterval`), the Settings
  section, the schedulers, and the "anonymous usage data" clauses in the
  DISCLAIMER and About page. **The app now sends no telemetry, statistics or
  usage data anywhere.**

  Unaffected and deliberately kept: Discord **Rich Presence** (carries no trade
  data) and the new Discord/Telegram **remote-control bots** (off by default,
  answer only accounts you paired, and send only what you ask them for). The
  user-script risk audit now flags the remote/bot modules by name in place of
  the deleted webhook sender.

### Added
- **Working orders live in the position panel, and the ticket fills by
  default.** A Limit buy below the ask rests on the book; the app used to say
  only "Order sent", which read like a purchase. The panel under the ticket
  now lists every working order of yours for the open market with size,
  filled, remaining and the best touch, plus Cancel and Reprice to fill. A
  placement that rests says so, with the touch price, and that it is not a
  position until it fills. The ticket defaults to Marketable, shows your
  available balance, warns when a limit would rest, and blocks a buy that
  costs more than the wallet holds before you ever review it. The exchange's
  rejections ("not enough balance / allowance", the $1 marketable minimum)
  are translated into plain sentences on the ticket; the raw text stays in
  the order log.
- **A position panel under the order ticket, with a sell that actually goes
  through.** A limit sell at your buy price rests above the bid and never
  fills. The Market page now shows your position in the open market (wallet
  or paper, per side, with cost basis, mark and P&L) and a Sell that walks
  the bids for your size, caps the slippage you allow (1, 2, 5 or 10¢ below
  the best bid), sizes the order to what fits inside that cap, and sends one
  fill-and-kill. The preview you confirm is frozen; the result says what
  filled, at what average, for how much, and what was killed. Resolved
  markets, an unreadable book and sub-minimum sizes refuse with the reason.
  The ticket's own sell now seeds from the best bid instead of the ask.
- **AI analysis on any market — and deliberately no number attached.** A
  "Read this market" button on the Market page sends the question, the
  published rules text and the current quote to Anthropic or OpenAI, and asks
  it to do the one thing a language model is unambiguously good at: read three
  paragraphs of resolution criteria adversarially and say what they actually
  mean. What it turns on, the traps in the wording, the dated events between
  now and close that would move it, and — given equal weight on screen —
  **what the model does not know**.

  **It never returns a probability, a fair value or an edge.** Not hedged, not
  in passing. A model will produce "68%" for any question you put to it, with
  no calibration behind it and a fluency that reads like authority; that
  number would be the least trustworthy figure in this application and would
  look like the most trustworthy. So it is not asked for one, and the response
  schema **gives it nowhere to put one** — `additionalProperties: false` over
  a set of fields that contains no numeric type, which means the provider's
  own structured-output layer rejects an invented `probability` before we ever
  see it. Structural, like `_HOSTS` is structural: a validator can be talked
  out of it, a missing field cannot.

  What you get instead is a **lean from a four-word vocabulary** — looks cheap
  / looks rich / looks about right / no view — with the argument that produced
  it. The schema orders every reasoning field *before* the conclusion, because
  providers generate in schema order and a model that concludes first and
  rationalises after is a worse instrument. `no_view` is a real answer and
  renders as one.

  Free text is still free text, so a sentence that prices the market anyway is
  removed and **the panel says how many it removed** — a silent redaction is
  its own small dishonesty. The scrubber is deliberately narrow and tested in
  both directions: "I'd put it at 70%" goes, "CPI came in at 2.4%" stays,
  because those are the market's numbers rather than the model's guess. A lean
  whose entire argument was scrubbed is downgraded to `no_view` rather than
  shown standing on nothing.

  **What leaves the machine:** public market data only — question, rules text,
  quote, volume, close time. Never the wallet, never a position, never a cost
  basis, never an order. That is enforced by `build_payload` taking a market
  and having no access to an account, not by a policy against it, and it is
  covered by a test that fails if any account-shaped key appears in the
  outbound body.

  **Both providers ride the terminal's existing gate.** `api.anthropic.com`
  and `api.openai.com` are declared in `terminal._HOSTS` like every other
  host, so the offline switch reaches them, a 429 parks them, they queue
  behind a minimum gap, and the privacy panel counts them — the same reasoning
  that put Kalshi in the table rather than giving it its own client. The SDKs'
  own retry is disabled (`max_retries=0`) so our park is the single retry
  authority; two independent backoffs stacked on each other is how a rate
  limit turns into a hang.

  Off by default and keyless, with **two switches**, because a stored key is
  not consent to start sending market text to a third party. Keys are stored
  through the same encrypted writer as the wallet key — never in
  `settings.json`, never read back to the screen — and **verified against the
  provider before being stored**, so a typo fails on the Settings screen
  rather than as a button that silently never works. The model list is read
  from the provider with your own key rather than hardcoded, so it is always
  what that key can actually reach.

  On demand only: no scheduler, no tick, no engine path reaches it, because
  every call bills the user's own provider account. Optional server-side web
  search grounds it in current sources and lists what it read; with it off the
  panel says plainly that anything recent is outside what the model can see.

- **A manual terminal, on top of the automation.** Every engine still works
  exactly as before; what is new is three screens that answer a question a bot
  never asks. A bot answers *"should I act, right now, on this one thing I am
  watching?"*. A terminal answers *"what is going on, and what should I do
  about it?"* — for **any** Polymarket market you name, not only the ones an
  engine happened to observe.
  - **Discover** — four live columns (New / Closing soon / High volume /
    Resolving), filters, search, and a watchlist.
  - **Market** — probability chart on a bounded 0–100 axis, full order-book
    ladder, the public trade tape, an order ticket, and a **resolution-risk
    report**.
  - **Track** — positions, resting orders, standing instructions, your fill
    ledger and a log of every manual order including the failures.

  The terminal keeps its own contract (`shared/market.ts`) and its own tables
  (`terminal_orders`, `terminal_fills`), deliberately separate from the
  engine's. A hand-placed order has no signal behind it and must never be
  counted as an engine trade, or the bot's win-rate becomes a mix of what it
  decided and what you decided.

- **Honest nulls, everywhere in the terminal.** A value nobody could produce is
  `null`, renders as an em dash, and is never coerced to `0` or to a default.
  Where a value is missing the row also says **why**, and what would fix it.
  Concretely: a market with no 24h volume reads "—", not "$0.00"; a position
  whose contracts did not come through this app has **no cost basis**, so its
  P&L reads unknown rather than break-even; the chart never invents a candle
  for a period with no trades.

- **Resolution risk — the "security panel" of a prediction market.** In crypto
  the danger is a rug; here it is *resolution*. Eleven checks on who decides,
  on what source, how ambiguous the wording is, whether the market is still
  accepting orders, and whether a sibling market in the same event contradicts
  it. The score is averaged over the checks that **actually resolved** and says
  so in words; below four resolved checks it prints no score at all. An unread
  check is never silently counted as a pass.

- **Per-field provenance.** Every number carries which provider produced it and
  when *we* received it, shown as a dot you can hover. A terminal that shows a
  price without saying whether it came from our own live feed or a cached
  third-party index is asking for trust, and trust is what we are trying not to
  require.

- **Our own microstructure.** The CLOB websocket feed already decodes quotes
  with local arrival timestamps; the terminal now records them into a per-market
  series, giving real spread over time with no indexer lag. Gated on
  subscription — the market you have open names what to record — and it never
  fills a gap.

- **Manual orders, gated in the main process.** A ticket that shows the
  tick-snapped price, the size the venue will actually accept, what is fillable
  at your limit, estimated slippage from walking the real book, the taker fee,
  and max payout/loss. The confirmation is a contract about a *number*: the
  backend re-derives the cost and refuses the order if the book moved, so a
  stale or hostile renderer cannot spend more than the figure a human read.
  Manual trading is independent of the automation master switch: paper mode,
  the wallet and the manual ceilings gate it, the bots' switch does not.

- **Standing instructions** (stop loss / take profit / price alerts). The one
  thing that acts without a click — because you wrote it down. Triggers are
  **probability points**, not percentages, and each one passes exactly the
  checks a manual order does. Every pending instruction is listed and
  cancellable on one page, and a stop that could not fire because live trading
  was off tells you so instead of failing silently.

- **Cost basis from the venue's ledger, not from what we asked for.** Fills are
  reconciled against Polymarket's own trade history. A fill that cannot be
  reconciled is stored `unreconciled`, **excluded from every aggregate figure**,
  and counted in a warning strip — never guessed at. The convention is
  **average cost, not FIFO**, and the UI says so.

- **Cost discipline.** Every terminal request goes through a per-host serial
  gate with a minimum gap, a TTL cache sized to how fast that field actually
  changes, and a 429 park that fails *fast* rather than letting doomed retries
  starve a high-value request. The resolution report is opt-in with its price
  stated on the button (~3 extra calls). `terminalHealth` exposes live
  per-host call/error/latency counts.

- **`npm run py:live`** — a live provider check against the real Polymarket
  APIs. Deliberately outside `npm run py:test`: it is slow and depends on
  somebody else's uptime, so it must never block CI, but it is the layer that
  catches a vanished route or a changed response shape. It found two real bugs
  on its first run.

- **Remote control from your phone — Discord and Telegram bots.** Leave the app
  running on the PC, check on it and react to it from anywhere. Both bots are
  **off by default** and answer nobody until you pair an account.

  Read and de-risk from chat: `/status`, `/positions`, `/pnl`, `/balance`,
  `/orders`, `/price`, plus `/pause`, `/resume`, `/flatten` and `/cancelall`.
  A standing instruction that fires — or one that was BLOCKED from firing —
  is pushed to you unprompted, because that is the one thing worth waking a
  phone for.

  **The security model, stated plainly:**
  - **The paired-id allowlist is the boundary, not the token.** Tokens leak —
    into screenshots, into pasted logs, into repos. A message from an
    unlisted id is dropped before it is parsed, and logged so you can see
    somebody found your bot.
  - **Pairing is code-based** (a six-digit code, single use, five-minute
    life), which is also how Telegram works without you hunting down your
    numeric chat id. Discord additionally accepts a pasted user id.
  - **Tokens are stored like the wallet key** — through the same encrypted
    writer, never in `settings.json`, never sent back to the screen — and are
    **verified against the platform before being stored**, so a typo fails on
    the setup screen instead of as a bot that silently never connects.
  - **Placing orders from chat is a SECOND switch**, off by default and never
    turned on as a side effect of enabling remote. Even on, every order is
    previewed, needs a fresh `/confirm` within 90 seconds, obeys a per-order
    dollar cap enforced in the backend, and still passes the same live-trading
    switch and paper mode a click in the app does. The realistic threat here
    is not a hacker; it is an unlocked phone.
  - **Every command is logged** — accepted, refused, rate-limited or from a
    stranger — and shown on the Remote page.
  - `/pause` and `/resume` persist through the normal settings store, so a
    pause from your phone survives a restart rather than quietly re-arming.

  **What leaves the machine:** your positions and P&L, to Discord's or
  Telegram's servers. That is what a chat bot is; the setup screen says so
  before any switch, and both hosts now appear in the privacy panel with live
  call counts, so the panel's claim to name every host stays true.

  Built on the dependencies already present — Telegram long-polls over `httpx`
  (no webhook, so no public endpoint and no port forwarding on a home PC), and
  Discord runs a hand-rolled Gateway client on the `websockets` dependency the
  CLOB feed already pulls in, rather than freezing `discord.py` into the
  installer for two API calls.

- **Cross-venue: the same question priced on Kalshi.** Polymarket and Kalshi
  list overlapping questions, and seeing both — with the gap between them — is
  the most directly useful thing a prediction-market terminal can show. A
  toggle on the chart overlays Kalshi's line on the same bounded 0-100 axis
  (no rescaling needed: both are probabilities in cents), and a panel shows
  both books side by side with a link out so you can check it yourself.

  **Reference data only.** The app reads Kalshi and never trades there:
  public endpoints, no key, no auth, no order path — every call is a GET
  through the shared provider gate, so the offline switch, the 429 park and
  the privacy panel's counters all cover it automatically. The price
  difference is presented as information about the two markets, not as an
  opportunity; forming a view on it is the operator's job, not the app's.

  **A match is a claim, not a fact.** Nothing in either API says two markets
  ask the same question — it is inferred from words, numbers and dates, and a
  wrong pairing produces a large, confident, entirely fictional arbitrage. So:
  both matched titles are shown verbatim, the reasons for AND against are
  listed (the "against" reasons stay visible even on a strong match), anything
  below the confidence floor is reported as **no counterpart** rather than a
  bad guess, and **the spread is withheld entirely unless the match is
  strong**.

  Finding the counterpart cheaply took three attempts, and the first two are
  recorded in `kalshi.series_index()` because they are not obvious: Kalshi's
  `/markets` feed is useless for discovery (the first 4,000 open rows were
  4,000 auto-generated parlays), and a full events sweep costs ~101,000
  markets and 20+ seconds while still burying the near-dated markets anyone
  would actually compare. Matching against ~11,800 SERIES titles — a series
  title *is* the question family — costs ~4 seconds, cached for 30 minutes.

- **Related markets** on every market page. The resolution report already
  worked these out to look for a contradiction and then threw the list away.
  Now the other legs of the event are listed and priced, one click from the
  market you are on — the closest thing to cross-venue arbitrage available
  inside a single venue. For a mutually-exclusive (neg-risk) group it also
  shows what the legs **sum** to: exactly one wins, so a coherent group sits
  near 100c and one well away from it is mispriced or partly unpriced.

- **Command palette and keyboard shortcuts.** `Ctrl/Cmd+K` (or `/`) opens a
  palette that searches markets and jumps to any screen; `g` then `d` / `m` /
  `t` / `s` / `w` navigates without opening it. Every shortcut is inert while a
  field has focus — a terminal that hijacks a keystroke mid-search is worse
  than one with no shortcuts at all.

- **Desktop notification when a standing instruction fires**, so a stop that
  triggers while the window is unfocused still reaches you; a stop that was
  BLOCKED from firing is marked critical, because that is the one you must
  see. Deliberately a desktop notification rather than a webhook: a webhook
  would post your positions to somebody else's server on every fire, and this
  never leaves the machine. Toggleable in Settings.

- **`npm run test:unit`** — renderer-side unit tests for the pure chart
  geometry, using Node 24's native TypeScript support, so no test framework or
  bundler was added. The chart's gap-breaking logic was the last untested
  piece of the terminal and exactly the kind that fails invisibly: a line
  bridging a six-hour hole in the tape looks completely normal and draws a
  trend that never happened.

- **Paper trading for the manual terminal.** A ticket-level PAPER/LIVE switch
  (Settings -> Manual terminal). Paper needs no wallet and no live-trading
  switch, because practising before funding anything is the whole point.

  The design rule is that a simulator is only worth having if it is **wrong in
  the same places the real thing is**. So:
  - **Fills come out of the real book**, at the average you would actually have
    walked — never your limit, never more size than was resting. Ask for 400 at
    99c against a book whose best offer is 19c and you get 19c, not 99c.
  - **Real fees**, from each market's own fee-v2 schedule. An unknown fee stays
    unknown rather than quietly becoming zero.
  - **Resting orders are pessimistic.** We cannot know queue position, so a
    resting order fills only when the market trades *strictly through* its
    price. Real life would sometimes fill you at the touch; erring the other way
    would invent fills that never happened.
  - **No naked shorts**, real tick sizes and minimums, and **exact settlement**
    at 100c or 0c when a market resolves.
  - The one simplification it cannot make honestly — no market impact — is
    stated on the Track screen and warned about on the ticket whenever an order
    eats more than a quarter of the visible depth.

  Paper lives in its own table with its own totals, and `terminalPlace` refuses
  outright while paper mode is on, so a stale screen cannot spend real money
  behind your back. The two paths never share an entry point: there is no
  single `if` whose inversion turns a simulation into a trade.

- **Data & privacy panel** (Settings). Names every host this machine contacts,
  what each is used for, and live request/error/latency/cache counts read
  straight from the provider gate — plus **one switch that turns it all off**.
  The app still works with offline mode on: every panel goes blank and says
  exactly why, rather than showing you something stale. A privacy claim you
  cannot check is marketing, and counters nothing reads are not a safety
  mechanism.

- **Settled positions are now settled.** Every position previously rendered as
  permanently open — a market that had resolved still showed a live mark and no
  outcome, which is the most misleading thing the Track screen could do. A
  decided market now marks at **exactly 100¢ or 0¢** (a binary contract is
  worth exactly $1 or $0 once the outcome is posted, which is the one place
  this domain beats crypto: the P&L is exact, not an estimate), is labelled
  won/lost, and shows a **redeemable** badge when the money is yours but
  unclaimed. Redemption itself is deliberately not automated — that is custody
  surgery and needs its own design conversation, so the UI links out.

  Knowing the outcome still does not mean knowing the P&L: a settled position
  with no cost basis in your ledger reports unknown, not a guess.

- **Fill reconcile now walks your whole trade history.** It made a single
  un-paginated call, so it only ever saw the newest page — any position opened
  before that window could never be reconciled and stayed `unreconciled`
  forever, excluded from every total with no way to backfill it. It now follows
  the CLOB's cursor to the end, and when it hits the page cap it **says so**
  instead of reporting success over a truncated pass.

- **Script risk audit** (`python/script_audit.py`), which replaces the sandbox
  with review rather than restriction. Every script is statically scanned on
  save, on validate and before arming, and the findings appear on a new
  **Risk** tab with line numbers: network egress, the app's own credential
  modules, credential-shaped identifiers and strings, dynamic execution,
  subprocesses, filesystem writes, environment and interpreter introspection.
  Two touches that matter:
  - **Obfuscation is critical in its own right.** A static scan cannot see
    through base64 blobs, `getattr` with a computed name, or `exec` — so
    "this script is hiding what it does" is reported as exactly that.
  - **Wallet + network together** produces a single synthesized critical
    finding naming the combination as the shape of a key-stealing script,
    instead of leaving the user to assemble it from a list.
  A flagged script repeats its findings in the arm confirmation, and an
  *enabled* script whose code changes into a critical finding is auto-disabled
  pending review. The audit never blocks execution, and its clean verdict says
  "nothing alarming recognized", never "safe".

- **Streak sizing for the crypto engine (win/loss-conditioned bet size).** New
  optional sizing layer on the Crypto tab: each consecutive loss scales the
  next bet by *(1 + on-loss %)* (compounding — +20%/loss → 1.44× after two),
  each consecutive win by *(1 + on-win %)* (0 = any win resets to base), with
  a hard **Streak cap** (× base). The current run is derived from settled
  positions on the active network at entry time, so it survives restarts and
  never counts dry-run or other-network rows; a scratch (P&L exactly $0)
  breaks the run. The multiplier is applied *before* the existing risk caps —
  max loss/bet, affordability, daily/lifetime loss limits all still bound the
  scaled size — and the per-bet preview shows the live multiplier (e.g.
  "streak ×1.44"). Fair warning, stated in the UI too: recorded 15m outcomes
  are serially uncorrelated (P(win|prev loss) 75.5% vs P(win|prev win) 74.6%,
  runs-test |z| < 1.5 on all 7 assets over 9,402 windows), so this reshapes
  variance — steadier recovery in exchange for bigger streak drawdowns — it
  does not add edge.
- **Shadow ledger view on the Scripts page.** Shadow mode recorded every order a
  script would have placed, and the backend served them — but nothing in the UI
  ever called that endpoint, so the only visible trace of a shadow run was a
  W/L summary in the sidebar. There is now a **Shadow** tab listing each
  would-be order: time, market, side, size, entry price, win/loss/open,
  simulated P&L, and the script's own reason — plus rail refusals and a header
  showing settled count and total simulated P&L. Refreshes on selection and
  polls while open.

### Changed
- **Kalshi comparisons now find the counterpart.** On sixty real Polymarket
  questions the matcher found 3 of the 31 that have one; it now finds all of
  them, with no false matches on a further 81. The misses were retrieval,
  not scoring: the right Kalshi series was never a candidate. Sports
  fixtures now route through the league and date in Polymarket's own slug
  to Kalshi's series tickers, crypto and macro through a named-topic table,
  and everything else through alias-expanded search; markets that parse to
  a subject, date, strike and direction match on those, and when the
  comparable Kalshi side is NO the panel says so and inverts the quote
  openly. Fifteen-minute crypto windows match on the exact window end;
  five-minute windows, which Kalshi does not list, say which window Kalshi
  is quoting instead of the generic "no market looks like this". A match is
  still a claim: both titles verbatim, reasons for and against, and no
  spread below the confidence floor.
- **The terminal is independent of the automation switch.** A hand-placed
  order, a standing stop or take-profit, a chat order and the wallet flatten
  used to refuse with "Live trading is off" whenever the engines' master
  switch was off. That switch runs the bots; the terminal is manual. Manual
  orders are now gated on paper mode, the wallet, and the manual ceilings —
  and on nothing else. The ticket no longer shows a "trading off" state.
- **Edge Stack follows whales in SPORTS only**, matching the backend's
  documented decision (the crypto-whale leg reversed to −3.9¢/contract on a
  bigger sample). The UI list and the backend mirror had drifted; a test now
  fails on the first divergent key. The `new` badge on Crypto Whale is gone
  and every preset description states its measured number: on the full
  recorded corpus (2026-09-01, zero spread charged) whale-follow is
  −2.42¢/contract, momentum −1.78¢, the 15-minute favorite −2.06¢ and the
  contrarian −3.80¢. They are example configurations, not edges.
- **Config patches from scripts and chat are allowlisted in the main
  process** (engine switches and the crypto posture keys, typed). A script
  can only turn live trading OFF; a paired chat account's `/resume` may turn
  it back on, because the pairing allowlist is the boundary.
- **Paired-mode refusals state the taker fees on the pair.** The cap stays
  on price by design (a tilted bet plus a hedge), but the hedged portion pays
  both legs' fees win or lose, and the log no longer implies otherwise.
- **CI runs the renderer unit tests and the Vite build**, and the installer
  workflow is gated on the same checks as CI.

- **The sidebar is grouped by who is deciding.** It had grown to twenty-two
  flat entries and stopped being readable: "Track" sat two rows from "Bot
  positions" with nothing saying one is yours and the other is the engine's,
  and "Discover" sat beside "Strategies" as though they were the same kind of
  thing. It now splits into **Terminal** (you decide and you place the order),
  **Automation** (engines that trade while you are away), **Account** and
  **App**, with Dashboard ungrouped above them because the overview belongs to
  neither half.

  A group whose items are all hidden renders no header, so simple mode shows
  four short sections rather than empty labels. The nav scrolls — five groups
  is taller than most windows in advanced mode — and the active item scrolls
  itself into view, so navigating to a lower group (or jumping there from the
  command palette) no longer leaves the sidebar looking like nothing is
  selected.
- **Scripts no longer need any other engine running.** Three hidden couplings
  made an enabled script look alive while something on a different page kept
  it from ever trading, with nothing connecting the two:
  - **Coins.** `decide()` was filtered by `crypto15m_assets` — the *15m Crypto*
    tab's per-coin checkboxes — in the live engine *and* in the backtest. So
    applying the 5m preset over there (it pins `['BTC']`) silently narrowed
    every script in the app to one coin. A script now carries its **own** asset
    scope, editable from its status line, defaulting to every coin; the crypto
    engine's setting no longer reaches it in either path.
  - **Signals.** `decide_signal()` reads rows written by the whale/momentum
    scanners, which only ran when the *main* engine wanted them. With
    `main_record_signals` and `enable_trading` both off, a signal script sat
    enabled and permanently starved. The collector is now force-enabled
    whenever an enabled script defines that hook.
  - **The master switch.** `scripts_live_enabled` gated the whole engine, so
    enabling a script did visibly nothing until the user found a second switch
    on the same page. It now means what its label says: **shadow scripts tick
    as soon as you enable them**; it gates *real orders*. An armed script with
    the switch off is parked (never diverted into the shadow ledger, which
    would corrupt the track record that ledger exists to build) and says so.
- **The app header no longer carries the main engine's controls.** "Start
  Trading" and the "Live Trading / Trading Paused" headline were both written
  when the main engine was the only engine, and both read `enableTrading`
  alone — so the header announced **"Trading Paused" while the crypto engine,
  copy-trading and armed scripts were all live**, and its Start button looked
  like a global "run the bot". Each engine's switch now lives on its own page:
  the main engine's is at the top of **Main Engine**, scoped and labelled
  "Start this engine", with its own status line and a confirmation that names
  the other engines as unaffected. The header now only REPORTS, and reports
  the truth — "N engines live" plus a per-engine live/off strip (Main, Crypto,
  Copy, Scripts).
- **Restart backend moved to Settings.** It restarts the Python *process*, so
  it takes every engine down and back up — it was never a main-engine control
  and putting it on an engine page would have implied it was scoped to that
  engine. It sits under a new **Backend** section on the app-level Settings
  page, next to the process status, and says plainly what it affects.
- **Every script shows whether it is actually running.** The sidebar's
  `ENABLED`/`off` answered the wrong question — a script can read ENABLED and
  run zero times because the wallet is disconnected, because it is armed while
  the master switch is off, because it is past the concurrency cap, or because
  its code will not load. Each script now reports a real state (`running` /
  `blocked` / `starting` / `error` / `off`) plus one actionable sentence naming
  the gate, computed backend-side where the gates are applied so it cannot
  drift, alongside tick/intent/order counts and when it last found a setup.
- **Scripts run as full Python — the language sandbox is gone.** The AST
  whitelist (no imports, no classes, curated builtins, no underscore
  attributes) fought every non-trivial strategy while never being a real
  security boundary; the module said so itself. Scripts now execute with the
  real builtins and may import anything. `on_start`/hook containment is
  unchanged in effect: each hook runs in a joined worker thread with a hard
  wall-clock timeout (`script_hook_timeout_sec`, default 1s), and a hook that
  overruns is abandoned and its script auto-disabled, so the engine loop keeps
  its cadence. The backtest, which makes thousands of calls per run, keeps an
  in-call time budget instead of a thread each. **The money rails are
  untouched** and still apply to every script.

- **Crypto tab: Simple / Advanced modes.** A toggle at the top of the Crypto
  page (persisted per machine, Simple by default) now splits the UI. Simple
  shows the quick presets, the knobs that decide what it trades — direction,
  entry window, favorite ≥, skip above, stop-loss — and the money essentials
  (bet size, max loss/bet, max open bets, daily loss stop, take-profit total),
  with a one-line pointer to Advanced. Advanced is the full existing UI: model
  tuning, take-profits, streak sizing, indicator gates, custom rules, and the
  backtest panel. Three quick presets were pruned from the row — Contrarian
  Fade, Momentum (Δ-confirmed), and MACD Trend — all measured break-even to
  negative or were never validated; their building blocks remain in Advanced
  (direction mode, Min move Δ, rule builder) and the Backtest page still
  replays contrarian for comparison.
- **Per-engine pages: Strategies + Settings merged into "Main Engine".** The
  app has three trading engines but only Crypto and Copy Trading had their
  own pages — the main whale/momentum engine's knobs lived on a generic
  "Settings" page while its presets sat on a separate "Strategies" page,
  which made it easy to read Settings as app-wide. The sidebar's
  **Strategies** entry is now **Main Engine**: strategy presets at the top,
  every whale/momentum knob below them (environment/auto-trading, signal
  sources, categories, gates, custom rules, sizing, order placement,
  concurrency & risk, trading hours, loop cadence, scanner thresholds), plus
  the Reset and Save-as-Profile actions. **Settings** now holds only
  app-level things: startup preferences, Discord webhooks, and the
  danger-zone full reset.
- **Main Engine: six strategy presets cut to three.** Sports Underdogs, Crowd
  Contrarian, and Krypt Conservative are gone — two near-identical
  contrarian/momentum bundles and a sizing-only variant made the choice harder
  than it is. Edge Stack, Crypto Whale, and Krypt Aggressive remain, and every
  knob the removed presets set is still reachable in the settings below them.
  A preset card also now reads **Active** only while your settings still match
  it: edit any knob and it goes back to **Apply**, instead of claiming a preset
  is running when you have since tuned away from it.

### Fixed
- **The macOS and Linux installers could not be built.** Their frozen backend
  failed its own self-test because the packaged signer stack reads package
  metadata at import and one package's metadata was never bundled. All three
  platforms now build from the same spec and pass the offline signing test.
- **For five minutes after a reboot, the settled record resolved nothing.**
  The ledger's "don't re-ask Gamma about a market that just came back empty"
  throttle measured age on the monotonic clock, which counts from boot, and
  treated a market it had never asked about as one it had missed at time
  zero. On a machine up for less than the throttle's five minutes — every
  fresh PC session, and every CI runner — every market was skipped, the
  record read "unknown" throughout and realised P&L was blank. Never-asked
  now means asked. Found because GitHub's runners are freshly booted VMs.
- **Track filed most of your fills on the wrong side, so cost basis and P&L
  were wrong.** A fill's side was derived from the outcome's *word*, which is
  "Yes" only on binary markets — on every Up/Down, Over/Under and team-name
  market outcome 0 is called something else, so buys never met the positions
  they paid for. Maker fills were also read from the taker's point of view.
  Sides now come from the outcome index via the token id, our leg of a maker
  match is read from the venue's `maker_orders`, and one press of Reconcile
  fills repairs the existing ledger (it is idempotent). On the author's
  wallet: cost basis on 105 of 105 positions instead of 50, and per-position
  P&L within cents of Polymarket's own figure.
- **Realised P&L counted only the losers.** It summed settlement over the
  positions still in the wallet — and a redeemed winner leaves the wallet,
  with no sell row because a redemption is an on-chain claim. Every winning
  market was missing and the total read as the sum of the losses. Realised
  P&L now settles every market the fill ledger has fills for, at $1 per
  contract on the winning outcome and $0 on the loser, whether or not you
  still hold it; resolutions are cached permanently. Track leads with the net
  figure and shows a won / lost record from the ledger.
- **"Redeemable" was shown on worthless positions.** Polymarket flags losing
  tokens redeemable too. The badge now needs the flag AND a positive value.
- **"Show settled" only ever showed losses.** The Positions tab is the
  wallet, which holds resolved losers as dust and never the redeemed winners.
  The toggle now opens the ledger's settled record — every settled market
  with side, cost, payout, P&L and won / lost / closed out — and settled dust
  is hidden by default with a note that the wallet is not a win rate.
- **Flatten listed every settled position and refused each one.** A resolved
  market has no order book. Flatten now skips resolved positions before any
  book read and reports them as one count.
- **The Origin column is gone from Track.** Every trade the engines placed
  read "Placed elsewhere". The field stays in the database for the separate
  engine and manual track records.
- **A standing stop or take-profit could send a REAL order while paper mode
  was on.** The paper check lived only in the RPC handler; the standing tick
  and the chat bots called the placement function directly. The check now
  lives inside `terminal_trade.place()` and again in the standing tick, so
  every caller inherits it. A stop that triggers in paper mode says so and
  retires, exactly like the live-trading-off case.
- **The order confirmation could change under you, and the changed number was
  what got sent.** The preview refetched on every 4-second market poll while
  the confirmation panel was open. The confirmation now snapshots the preview
  you read and sends that; if a newer preview differs by more than 2¢ the
  panel drops back to un-confirmed and says the price moved.
- **Committed dollar figures were K-rounded.** A $1,249 order confirmed as
  "$1.2K". Every figure you commit to or are owed — ticket cost, the
  confirmation sentence, Track's cash, value, cost basis and paper bankroll —
  now prints exactly.
- **The exit taker fee was never charged on externally-closed positions.**
  The check was `"sell" in kind` and the exit finder returns `"sold"`, which
  never matched, so every hand-closed main-engine and copy position booked
  P&L without the crossing-sell fee — and the loss breakers ran on it. Two
  tests had stubbed `"SELL"`, a value production never emits.
- **Every armed stop was retired when the wallet address could not be
  read.** An unreadable address returned an empty position list, which the
  standing tick took as "you no longer hold this" and disarmed everything.
  The tick now reads strictly and holds fire on an unreadable wallet.
- **A failed take-profit swallowed the crypto stop-loss.** One shared
  per-position cooldown meant a refused take-profit at t=0 made the stop at
  t=20 submit nothing while still consuming its own 90-second throttle. The
  stop-loss is exempt from that cooldown, and a refused strength ask or
  take-profit no longer ends the tick before the stop is evaluated.
- **Paper mode allowed a naked short through resting sells and credited
  phantom cash.** Two GTC sells of 10 against 10 contracts both filled and
  cash read $1,014.00 on a $1,004.50 account. A resting sell now reserves
  inventory, cash is credited only for contracts actually held, and the fill
  sweep cancels a sell whose position is gone.
- **The order ticket's cost described no possible outcome.** It was
  requested size × the average of only the fillable slice. FAK/FOK now
  preview what can actually be spent; GTC previews the worst case with the
  remainder resting at the limit.
- **Applying a main-engine preset reset your lifetime loss limits and AI
  settings.** 22 config keys were missing from the Electron defaults, so a
  preset apply dropped them and the backend fell back to its own defaults
  (lifetime USD cap → off). All 168 keys now have defaults, guarded by a
  unit test, and preset apply preserves the loss limits and every `ai*`,
  `remote*` and `terminal*` key.
- **Money fields were saved on every keystroke, and an empty chat-order cap
  became "no cap".** Backspacing 100 to type 50 persisted 0 (unlimited) in
  between. The cap, the paper bankroll and the model id commit on blur or
  Enter and never persist a blank as zero.
- **The header showed a confident green $0.00 when the backend was down.**
  It now shows an em dash with no tint, and the share button is hidden.
- **`/pause` from a phone paused nothing in-process.** The patch was
  camelCase and the live config is snake_case. It now lands in both.
- **`/flatten` and `/cancelall` said "Nothing open to close" while the
  wallet held positions.** They only ever covered the main engine; they now
  say how many crypto, copy, script and manual positions or orders remain.
- **In a Telegram group, pairing authorised every member.** The identity
  was the group's id. It is now the sender's.
- **The Discord bot could become a zombie that reported "connected".**
  Heartbeat ACKs were never read. A gateway that stops acknowledging is torn
  down and reconnected, and the status reflects it.
- **A cancelled stop-loss could be resurrected, armed, by a lost update**
  between the tick's read and its write-back. The tick now merges per
  instruction against the current list.
- **"Start my stats fresh" missed shadow orders and re-imported terminal
  fills as external.** Both wipe correctly now.
- **Fees were misattributed at four sites** — script entries never counted
  as maker, `limit_mid` main-engine entries were charged taker, copied
  positions paid the crypto rate on every category, and a crypto entry that
  escalated to taker kept its maker stamp.
- **A script's notional cap could shrink an order below the exchange
  floor**, which then bounced at the venue. The rail now refuses instead.
- **A timed-out script hook kept writing into its state after being
  disabled**, and re-enabling reused the same object.
- **Signals were pushed one IPC hop per row.** A scan now emits one batch.

- **Track reported past history wrongly — two compounding bugs.** Measured on
  a real wallet holding 105 resolved positions:

  1. **A settled market was counted as UNREALISED.** Once a market resolves the
     outcome is decided; there is nothing unrealised about it, redeemed or not.
     The wallet showed a permanent **-$229.12 "unrealised"** loss on positions
     that could never move again.
  2. **Realised P&L only ever counted SELLS.** A resolved market pays a
     redemption, not a fill, so settlement was invisible to the fill ledger —
     and anything held to settlement (which the crypto engine always does)
     contributed nothing. Realised read **-$17.62** against a true -$246.74.

  Settled positions now book their P&L as **realised**, `unrealizedPnlUsd`
  covers open positions only, and the realised total is ledger sells **plus**
  settlements. A new `settledPnlUsd` splits out how much came from settlement
  rather than selling, and the positions table shows whichever figure the row
  actually has instead of an em dash. Same wallet now reads: unrealised $0.00
  (nothing is open), realised **-$246.74**, of which **-$229.12** settled.

  A settled position with no cost basis still reports **no** P&L — knowing the
  outcome is not the same as knowing what it cost you.
- **Reconciling fills triggered a websocket resubscribe storm.** The fee
  lookup called `terminal.market()` once per fill, inside the DB transaction —
  and `market()` has a side effect: it subscribes that market's tokens to the
  live CLOB feed. Right for "the user opened this market", very wrong for
  "replay 1,500 historical fills". Measured on a real wallet: it would have
  registered hundreds of dead tokens and forced a resubscribe on each. Markets
  are now resolved once, batched, before the loop — verified at **0 tokens
  registered** and 60 batched Gamma calls for 1,502 fills, with the SQLite
  connection no longer held open across network I/O.
- **Paper trading could rate-limit the real terminal.** `_fill_resting` fetched
  one order book per resting ORDER every 15 seconds, unbounded — ten orders on
  one market meant ten identical CLOB calls, and enough of them would park the
  CLOB on a 429 and starve the live chart and book. Now one fetch per (market,
  side), capped per tick. Paper trading costs nothing; it must not cost the
  terminal its rate budget either.
- **Opening Discover cost ten requests instead of five.** The poll effect and
  the filter effect both fired on mount, and the filter timer landed at 350ms —
  before the first batch had populated the TTL cache. The filter effect now
  skips its first run.
- **The microstructure recorder was never stopped.** Every other background
  task is cancelled on shutdown; this one kept sampling on a closing loop.
- **A failed crypto exit was completely silent.** `_place_exit_sell` wrote an
  `error` field to the position row and did nothing else — no log line at all,
  while `trader.py` has always logged the equivalent, and the Crypto page
  styles rows on `status` rather than `error`. So a stop-loss or take-profit
  whose sell was refused left the position open with no trace anywhere the
  user looks. It now logs at WARNING, naming the asset, size, price and reason.
- **Take-profit and sell-strength exits retried on every 4-second tick.** Only
  the stop-loss path was throttled; the other two had nothing, so a
  deterministic refusal (sub-minimum size, stale allowance, closed market)
  re-submitted a real order roughly 900 times an hour. All three exit reasons
  now share a 60-second cooldown that applies **only after a failure**, so a
  working exit is as fast as it ever was.
- **The sell path had no allowance refresh.** `refresh_balance_allowance()`
  only ever refreshed `asset_type=COLLATERAL`, which covers BUYS. A sell is
  validated against the **CONDITIONAL** allowance for the specific outcome
  token being sold, and nothing refreshed that — so the exact spurious "not
  enough balance / allowance" rejection that refresh exists to prevent could
  still bounce an **exit**. An exit failing is worse than an entry failing:
  you are already in the position, and a stop loss that cannot sell is not a
  stop. `refresh_token_allowance(token_id)` now runs before every sell,
  best-effort and non-fatal (refusing to sell because a *cache refresh* failed
  would be its own way of trapping somebody).
- **The minimum-size guard was buy-only, silently.** A sub-minimum SELL was
  signed and sent with no check and no warning, to be rejected by the CLOB.
  Kept as an asymmetry — on a buy you can order more, on a sell you hold what
  you hold, and hard-blocking would guarantee the user stays stuck — but it is
  no longer silent: the order ticket warns that Polymarket may refuse it and
  that the position could then only be closed by letting it settle, and the
  backend logs the same at WARNING.
- **A refused stop loss retired itself.** `standing_tick` set `armed = False`
  regardless of whether the sell succeeded, so a rejected exit left the
  position open and quietly deleted the stop protecting it — the worst
  outcome available. A refusal now keeps the instruction **armed** and retries,
  bounded to three consecutive failures before it gives up and says exactly
  why, and every refusal is reported as `blocked` so it reaches the desktop
  notification and the phone push.
- **A geoblocked connection looked completely healthy.** The backend has
  always reported `tradingGeoblocked` in the account snapshot and **nothing in
  the UI ever rendered it** — so with a blocked IP the market data flows, the
  balance reads, every engine says "live", and every order bounces with no
  explanation anywhere. `check_trading_ready()` even returns `ok: true,
  issues: []` in that state. The header now shows an "Orders geoblocked"
  badge. Found by running the app against a real wallet whose IP was blocked.
- **Worthless positions were described as claimable cash.** Polymarket flags
  losing tokens `redeemable: true` as well as winning ones, so a wallet
  holding 105 expired losers was told they were "ready to redeem — claim them
  on Polymarket to turn them back into cash". They are worth exactly $0.00.
  The wording now comes off the position VALUE rather than the flag, and
  reports what the redeemable set is actually worth.
- **"There is no paper mode" was still claimed in eight places** — the README
  warning, DISCLAIMER, onboarding, About, the Main Engine and Crypto pages,
  Strategies and the marketing boilerplate. It stopped being true the moment
  paper trading shipped. Every one is now scoped to what remains true: **the
  engines have no paper mode; the manual terminal does, and it covers
  hand-placed orders only.** Saying it flatly was false; saying "the app has a
  paper mode" without the qualifier would have been worse, because somebody
  would enable an engine expecting it to be simulated.
- **A stale chat command could execute on wake.** Telegram queues undelivered
  updates for ~24 hours and a fresh process starts from offset 0, so a
  `/flatten` sent at 2am while the PC was asleep would EXECUTE when the machine
  woke at 9am — closing positions on a seven-hour-old instruction. Commands
  older than two minutes are now acknowledged but not run, and the sender is
  told once why nothing happened. (Discord is unaffected: its session lives in
  memory, so a restart re-identifies and receives no backlog.)
- **Empty-book sentinels were being reported as prices.** Gamma returns
  `bestBid: 0` / `bestAsk: 1` for a market with no resting orders, and the CLOB
  websocket does the same. Polymarket only accepts orders between 1¢ and 99¢, so
  neither extreme is a price anyone could trade — but passing them through
  produced a confident **"bid 0¢, ask 100¢, spread 100¢"** on every freshly
  listed market, and a fake 1.0¢ spread that then poisoned our own
  microstructure median. Both are now scrubbed at the source. Caught by driving
  the real app and by the live check; no fixture would have produced it.
- **A watchlisted market vanished once it settled.** Gamma's `/markets`
  defaults to `closed=false`, so looking a market up by condition id returns
  nothing after it resolves. The entry was still stored, it just stopped
  rendering — and the column then said *"Nothing on your watchlist yet"*, which
  reads as the app having lost your list. Closed markets are now retried
  explicitly, and an unloadable list says that instead of claiming to be empty.
- **Empty columns explained themselves with the wrong reason.** With offline
  mode on, "New" said *"just-listed markets usually have no book yet"* and
  "Resolving" said *"none of the 0 most recently closed markets is awaiting
  settlement"* — both confident claims about Polymarket, made after asking
  nobody. The real reason now wins over any column-specific note.
- **A neg-risk group's legs were summed with our own market counted twice.**
  `_event_markets` already returns the market you are looking at, and the sum
  added it again — so a Fed group whose five legs really summed to 100.7c was
  reported as **135.2c**, i.e. "35c mispriced". It looked plausible, which is
  exactly why it survived a screenshot review.
- **A stop loss could fire against a position you no longer hold.** Nothing
  checked the wallet before selling, so a stop on a position closed by hand
  (or settled, or exited on Polymarket directly) sent an order the venue
  rejected, logged it as an error, and — because the instruction stayed armed
  — did it again on every tick, forever. It now reads holdings once per pass:
  a missing position **retires** the instruction with an explanation, a
  smaller-than-written position sells only what is actually there and says so,
  and a wallet read that FAILS holds fire and stays armed rather than guessing
  (an unreadable wallet is not the same as an empty one).
- **Opposite questions were matched to each other across venues.** A live
  cross-venue run paired Polymarket's "Fed will INCREASE rates by 25bps" with
  Kalshi's "CUT 25bps" at high confidence and printed a 33c spread between two
  mutually exclusive markets — the exact fictional edge the whole module is
  built to prevent. The antonym word list had increase/decrease but not
  increase/cut. Direction is now read as a POLARITY (up/down) rather than
  matched pair by pair, which generalises the rule instead of chasing words;
  with it, that question correctly routes to Kalshi's "Hike 25bps" leg.
- **A shared date could carry a mismatched threshold.** Number agreement was
  scored on whether the two titles share ANY figure, so "Bitcoin above
  $110,000 on Aug 30" and "Bitcoin above $95,000 on Aug 30" both "agreed" on
  the 30 and paired up. Scoring now looks at what each side names that the
  other CONTRADICTS.
- **"Increase by 25bps" was flagged as contradicting "decrease by 25bps".**
  The sibling check compared titles by word overlap, which cannot tell an
  opposite from a rephrasing — those two share 83% of their words. It reported
  their 33c gap as a resolution RISK, in the panel whose entire job is to be
  trusted. Opposing terms now score zero similarity, and a neg-risk group is
  never checked for contradictions at all: its legs are mutually exclusive by
  construction, so disagreement is the design, and the group sum is the test
  that actually means something.
- **A non-Element keyboard event target crashed the shortcut handler.**
  `e.target` can be `window` or `document`, neither of which has `.closest` —
  calling it threw inside the listener and silently killed every shortcut.
- **An all-or-none order the book could not fill was blocked instead of sent.**
  The venue kills an uncrossable FOK cleanly and reports it as a zero fill,
  which costs nothing — refusing to submit made that outcome unreachable, and
  paper mode could not simulate the kill an FOK exists for. It is a warning now.
- **Unknown P&L was tinted green.** `?? 0` in the accent test made a null
  render as a gain, which is the same lie as printing $0.00 — in colour.

- **A script order could fill LARGER than the size rail.** A marketable buy on
  Polymarket is filled against its *notional* budget (count x limit), not its
  contract count, so a limit above the touch buys proportionally more. Caught
  on a live account (2026-08-03): a request for `5 @ 31c` came back as **7
  filled @ 21c**, because the snapshot ask (30c) was stale against a real book
  at 20c. Two fixes — the crypto entry path now **re-quotes at submit** like
  the general-market path already did, and every entry path bounds the
  notional (`contracts x limit <= script_max_contracts x ask`) so a fill
  anywhere between the ask and the limit stays inside the advertised cap.
- **decide_market() shadow orders never settled, which silently retired the
  script.** Found on a live account (2026-08-03): five sports shadow orders
  whose games closed on 07-27 were still open a week later. The settle path
  read the local `markets` table, which cannot answer — it never records an
  outcome (0 of 2,282 rows had `result`, `settlement_value` or
  `status='resolved'`) and closed markets drop out of it entirely on the next
  `sync_markets`, so the row was gone. The working resolvers
  (`resolve_alerts_from_markets`) have always fetched from the live API; this
  one now does the same. The damage was not just a missing track record: open
  shadow rows count against `script_max_open`, which gates **every** hook, so
  five stuck rows permanently retired the script while the UI showed a clean
  "0W / 0L / $0.00" — indistinguishable from "found no setups". A backstop now
  retires any shadow order still unresolvable 7 days after its close time,
  booking it as UNKNOWN (excluded from win/loss and P&L) rather than guessing.

- **Scripts now refuse orders the balance cannot fund.** The built-in crypto
  engine has always skipped when the balance is short; the script path sent
  the order anyway and let the exchange bounce it, leaving a terminal `error`
  row that reads like a malfunction. Unaffordable entries are now refused
  before the exchange call and recorded as ordinary refusals with the reason
  (`needs $4.50 but the balance is $0.97`), keeping $0.25 back for fees.
  Shadow is unaffected — it spends nothing, so a flat balance must not stop it
  building a track record.

- **An armed parlay schedule that seated zero hours traded with every exit
  disabled.** A schedule with an empty `hour_configs` passed validation and read
  as armed. Downstream, "empty" was indistinguishable from "no schedule", so the
  engine fell through to your *base* config for every hour — while still
  stamping each position `strategy="parlay"`, which exempts it from all four
  exit paths (stop-loss, cents take-profit, %-take-profit, sell-into-strength).
  Real money, no stop-loss. A zero-hour schedule is a normal generator outcome
  (the win-rate bar seats nothing on a short or noisy corpus), so this was
  reachable by ordinary use. Empty schedules are now rejected at arm time, seat
  no hours at run time, and replay as zero trades instead of quietly
  backtesting the base config and printing a healthy-looking summary.
- **A script could switch off its own CPU budget with `try/except`.** The
  sandbox's per-call step/time budget is delivered as an exception raised inside
  the script's own frame — so `except Exception:` swallowed it, and because
  CPython drops the trace function as soon as a trace callback raises, the
  budget was then gone for the rest of that call (measured: 3,000,001 loop
  iterations, no exception). The budget error is no longer part of the
  `Exception` hierarchy, a call that comes back having lost its tracer is
  rejected outright, and bare `except:` / `except BaseException:` are refused at
  save time. A `decide_market` scan is additionally bounded as a whole, so no
  script can hold the engine loop ahead of the crypto exit pass.
- **Shadow orders from `decide_market()` could never settle.** They are keyed to
  a market rather than a signal, and settlement only knew how to resolve crypto
  windows and signal follows — so they stayed open forever: no track record, and
  they permanently consumed the per-script open-position cap, which gates *every*
  hook that script has. A shadow market script quietly stopped trading while the
  arm-confirmation modal still showed a clean "0W / 0L / $0.00" — read as "no
  losses" right before arming it with real money. Market shadow orders now settle
  from the market's own recorded outcome, and a shadow order that has not settled
  in a week no longer holds a slot.
- **A failed general-market order was retried forever.** When the exchange
  rejected a `decide_market()` entry (most likely "not enough balance"), the
  engine returned without recording anything — and since "already attempted" is
  read from the position table, the identical order went back to the exchange
  every poll. It now writes a terminal `error` row and cools the market off, the
  way the crypto path already did. This also matters when a response is *lost*
  after the CLOB accepted the order: the retry would have opened a second real
  position, and the CLOB has no client-side idempotency key.
- **A script in shadow mode could still retune the live engine.** `supervise()`
  is the one hook that reaches outside the script — it rewrites the crypto
  engine's entry threshold, entry max, min delta, entry window and direction
  mode, and can switch engines off. It ran regardless of shadow mode, so a
  script the UI badged "shadow" could change real settings while the user
  believed they had green-lit a simulation. It is now skipped entirely in
  shadow (the heartbeat says so), enforced inside the hook runner rather than
  at the call site, and both the docs and the arm dialog now state it.
- **The spread rail had no control.** `script_market_max_spread_cents` — the cap
  on how wide a book a `decide_market()` entry may cross — was enforced by the
  engine but existed nowhere in the UI, while the script docs and the shipped
  `sports-favourite-bias.py` example both told you to tune it "in Settings".
  It is now a **Max spread** rail on the Scripts page alongside the others, and
  both docs point at it. (Hand-editing `settings.json` would not have worked:
  the running app rewrites it from memory.)
- **Simple mode told you to flip a toggle it never showed.** When custom entry
  rules are on, the Crypto tab's warning banner says the entry knobs are being
  ignored and to turn the toggle off "below" — but the rule builder that holds
  that toggle is Advanced-only, and Simple is the default mode for every
  upgrading install. The banner now carries its own **Turn rules off** button.
- **Applying a Main Engine preset wiped settings it does not own.** A preset is
  built from the full default config, so applying one reset your Discord webhook
  URLs (unrecoverable from inside the app) and every Scripts money rail,
  including the live-arm switch. Presets now preserve the Scripts engine, the
  webhook/Discord preferences, and — as before — the independent crypto and
  copy-trading engines.
- **`decide_market` scanned 20 markets, not the 150 the notes claimed.** The
  Electron default config is pushed to the backend on every boot, so its stale
  `scriptMarketLimit: 20` silently overrode the intended Python default on every
  install — leaving the new hook looking at ~2% of the live board.
- **Every momentum/trend indicator was computed against an unfinished candle.**
  Hyperliquid's `candleSnapshot` runs to `now`, so the final row is the CURRENT
  minute, still forming — its "close" is just the price a few seconds in. It was
  fed straight into the indicator series, so `velocity1mPct` measured *the
  elapsed fraction of a minute* rather than a whole one (observed: `+0.0000`
  at 23:24:02 where the completed minute was `-0.0062`). Across a day of live
  ticks `|velocity1mPct|` came out **~3x smaller** than the same statistic on
  completed candles (median 0.0075 vs 0.0241), so a 0.1% threshold fired on
  **0.15%** of ticks instead of ~6.9% — which is why the BTC momentum script
  took no trades live *or* in backtest. Also skewed `change5mPct`/`change15mPct`,
  MACD, RSI, EMA/SMA and VWAP, and made live rows incomparable with the
  backfilled historical ones they are replayed against. The in-progress candle
  is now dropped.
- **The backtester blamed your recorded data for its own limitation.** Running a
  backtest on a script that defines only `decide_market()` (or `decide_signal()`
  / `supervise()`) reported *"No resolved 15m windows recorded in the last 30
  days — let data accumulate first"*, which sent users off to fix a recorder that
  was working perfectly. The real reason is that the recorded corpus is crypto
  Up/Down windows, so a script with no `decide(ctx)` has nothing to replay
  against. It now says **NOT BACKTESTABLE**, names the hooks the script actually
  defines, and points at shadow mode — and that explanation is hoisted above the
  generic "only 0 trades" line, since it is the reason for the zero. The
  original message is kept for its one valid case: a `decide()` script with
  genuinely no recorded windows.
- **Every market's `category` was blank, so all category gating silently failed.**
  Gamma's `/markets` list embeds the parent event but with an EMPTY `tags` array
  (verified: every row `events=1, tags=0`), and `_normalize_market` had no
  fallback despite this module documenting one — so 1,542 of 1,542 stored
  markets had `category=""`. Whale/momentum allowlists and any `decide_market()`
  script filtering on category matched nothing. Now falls back to keyword
  inference on the title/slug, which resolves ~92% (341 sports, 173 politics,
  75 crypto … on the live board).
- **`decide_market()`'s universe was ranked by LIFETIME volume**, so the top-20
  it handed to scripts was entirely long-dated novelty markets ("will X win the
  2028 election", $30-65M lifetime, priced 0-4c, years from resolving) and
  contained zero tradeable events. Now ranked by **24h volume**, and the default
  universe size is 150 rather than 20 — price and volume correlate, so a
  strategy hunting a price band needs depth to find its targets.
- **`spreadCents` reported a fake 0.0c on every market.** The market list gives
  one price, stored as both bid and ask, so a script gating on "spread ≤ Nc"
  passed everything while believing it had checked the book. It is now `None`
  unless a real two-sided book exists (so such a gate fails closed), and the
  real spread is enforced at submit against the live CLOB quote via the new
  `script_market_max_spread_cents` (default 2c).
- **Added `market["hoursToClose"]`** — the sandbox has no imports, so scripts
  could not parse `closeTime` and had no way to tell a game starting in an hour
  from a futures market resolving in 2027. Hold-to-settlement strategies were
  silently able to lock capital for months.
- **Category filters could silently override the Settings UI.** The trader ANDs a
  global allowlist (`allowed_categories`, which the Categories panel writes) with
  per-engine ones (`allowed_whale_categories` / `allowed_momentum_categories`).
  Those per-engine lists were written **only by strategy presets** and had no UI
  control at all — so after applying a preset, enabling a category in Settings
  appeared to do nothing while the backend logged
  `whale: category 'sports' not in whale set`. They are now editable under
  Categories → *Per-engine refinement*, defaulting to "Any" (no extra
  restriction), with copy explaining that presets set them.
- **One click in the Categories panel could halt all trading.** The picker's
  toggle flipped between "allow all" (`null`) and an **empty list**, which the
  trader reads as *trade nothing* — so pressing "Pick specific" stopped the bot
  until a category was chosen, with no warning. Clicking a category now
  deselects just that one, the button only ever offers the safe direction
  ("Allow all"), and an empty selection shows an explicit warning. The section
  copy now states both meanings instead of only "Off = allow all".
- **Pasting a script could kill the validate/save RPC with no usable error.**
  Text pasted from a browser, PDF or chat window can carry *lone surrogates*
  (`\udc80`-`\udcff`) — what a smart quote becomes when its encoding is
  mis-detected in transit. Python holds them fine, but serializing the response
  back to the renderer raises `UnicodeEncodeError: surrogates not allowed`, so
  the call died and the UI showed only
  `error invoking remote method "scripts:validate"`, pointing nowhere near the
  cause. Script text is now scrubbed at every entry point (validate, save,
  backtest) and on the way out, so the worst case is an ordinary syntax error
  with a line number the editor can show.
- **The market scanner only ever saw 100 markets.** Gamma silently caps a page at
  100 rows no matter what `limit` you ask for, and `fetch_markets` emitted a
  next-cursor only when `len(data) >= limit` — with callers requesting 500, the
  condition `100 >= 500` was never true, so the cursor was always empty and
  `fetch_all_open_markets` returned a single page regardless of `max_pages`. That
  capped `scanner.sync_markets()` — and therefore the `markets` table that the
  whale, momentum and `decide_market()` paths all read — at the top 100 markets
  by 24h volume, roughly 8% of what Polymarket lists. Verified live: the same
  call now returns **1,000** markets instead of 100.
- **A small but tradable wallet was told it couldn't trade.** The trading-readiness
  check demanded a flat $5, warning *"add at least $5 so the bot can place its
  minimum 5-contract order"* — but what an order costs is price-dependent:
  `max(orderMinSize shares, ceil($1 / price)) x price`. At 20c or below the $1
  notional binds and the minimum order is exactly **$1.00**; only at 99c does it
  reach ~$4.95. So a $1.64 balance, which can trade any market priced up to 32c,
  was told it needed to add funds before placing any order at all. The check now
  reports the real floor and what the balance can actually do — a hard stop below
  $1, a "can't verify the wallet pairing yet" note below $1.50 (the live signer
  probe's threshold), and otherwise the highest price it can trade at. Verified
  against live markets: `orderMinSize` really is 5 shares on the crypto Up/Down
  windows, so the 5-share rule itself is Polymarket's and stands.

### Documentation
- **The Guide and README now cover the terminal**, paper trading, Kalshi
  cross-venue and remote control. Before this they mentioned none of them —
  grepped, literally zero hits — so several releases of work had no route in
  for a new user.
- **The Guide is reachable in simple mode.** It was `advanced`-tier, which
  meant the page that explains the app was hidden in the app's DEFAULT mode.
  Found by trying to screenshot it and landing on Wallet instead.

## [2.0.0] — 2026-07-25

### Added
- **Custom strategy scripts** — write your own strategy in Python on the new **Scripts**
  tab, backtest it against your own recorded ticks, and arm it to trade. Hooks:
  `decide(ctx)` (per coin, per tick, on open crypto Up/Down windows),
  `manage(position, ctx)` (dynamic exits and trailing stops over your open
  positions), `decide_signal(signal)` (filter and size which whale/momentum
  signals to follow), `supervise(app)` (change whitelisted config — e.g. switch
  strategy posture, or turn an engine off), plus `on_start` / `on_fill` /
  `on_settle` and a persistent `state` dict for cross-tick memory. `ctx` exposes
  ~40 per-tick fields: real book asks/bids, spot and realized volatility,
  MACD/RSI/VWAP/EMA/SMA, model probability and fee-net edge, window timing, and
  your live portfolio.
- **Shadow (dry-run) mode for scripts** — every new script starts in shadow: it runs
  against the same live ticks and through every safety rail, but the exchange call
  is replaced by a recorded entry in a separate shadow ledger. Those orders settle
  passively from the real window/signal outcome with the same taker fee charged, so
  a script builds a genuine forward track record before it can ever spend money.
  Shadow scripts are measured against their own ledger for the one-entry-per-window
  rule, the open-position cap, and the daily-loss breaker, so behavior matches live.
  Arming a script for real orders is an explicit confirmed action that re-validates
  the code first. Shadow simulates **entries only** — exits need a real position, so
  shadow orders are held to settlement; the UI says so before you arm.
- **Per-script safety rails**, applied to sandboxed AND trusted scripts alike —
  max entry price, max contracts per order, max open positions per script, a
  per-script daily realized-loss breaker that auto-disables, and a cap on how
  many scripts run concurrently. One entry attempt per market window per script.
  Any script exception, malformed return, or CPU-budget breach auto-disables
  that script and surfaces the reason; the engine loop never sees a script error.
- **`decide_market(market)` — scripts can now trade any Polymarket market**, not just
  the crypto Up/Down windows. Each tick the hook is called with the highest-volume
  general markets (title, category, both sides of the book, spread, volume, open
  interest, price change), and returns the same style of order intent, sized in
  contracts or dollars. Entries go through every existing rail and land in the main
  position table, so fill polling, resolution, and reconcile handle them for free.
  One entry per market per script. The universe size is a setting
  (`script_market_limit`, default 150, `0` disables it) because each market costs a
  sandboxed hook call per script; crypto windows are excluded so `decide()` and
  `decide_market()` can't both enter the same window. Not simulated in backtests —
  the recorded corpus only covers crypto windows, so validate it in shadow mode.
- **New script ctx fields** — `spreadCents` (book width), `modelEdgePts` (signed
  model-vs-book disagreement in percentage points), `favoriteAskCents` (the
  favorite side's real executable ask), and `timeFracLeft` (0..1 through the
  window, so one script can serve the 5m, 15m and hourly series without
  hard-coded minute thresholds). All four are computed by a single helper shared
  by the live snapshot and the backtest replay, and are pinned by tests to be
  backtestable and identical in both — a ctx field can never mean one thing in a
  backtest and another with real money.
- **Script sandbox** — AST validation (no imports, classes, dunder/frame access;
  every free name must resolve to a curated builtin) plus a line/wall-clock
  execution budget and a thread backstop, so a busy loop or an uninterruptible
  C-level op can't wedge the engine. **Trusted mode** ("full Python") skips the
  language sandbox — never the money rails — and says so before you can enable it.
- **Script backtester** — replays a script over your recorded resolved windows
  with both taker fees charged, and ships its own honesty caveats (maker fills
  not modeled, exit fills flagged optimistic, rails not simulated, in-sample).
- **Editor autocomplete and inline validation** — the script editor now completes
  `ctx["…"]` / `market["…"]` / `position["…"]` / `signal["…"]` field names (with their
  documentation inline) from the same vocabulary the backend actually serves, and
  offers hook skeletons at the start of a line. Errors and live-only-field warnings
  are underlined in place as you type, produced by the *same* backend validator that
  gates saving and enabling — not a second copy of the rules in the UI.
- **Script import / export** — save a strategy to a `.py` file or load one from disk.
  Imported scripts arrive disabled, in shadow mode, and still have to pass the
  validator before they can be enabled: importing is never arming.
- **AI context pack** — one copy-paste prompt containing the field vocabulary,
  return contract, your actual rails, and your data inventory, rendered *from*
  the live sandbox/engine modules so it can't drift from what really runs.

### Fixed
- **Database corruption recovery was broken on Windows** — `get_db()` ran its
  `PRAGMA` statements outside the try/finally that closes the connection, so a
  corrupt DB (power loss, AV lock, disk error) leaked an open handle. On Windows
  that made both the rename and the delete in the quarantine path fail with
  "process cannot access the file", so `init_db()`'s recovery could not run and
  the frozen backend crash-looped into its 5-strike give-up — bricking the app on
  every launch, which is the exact scenario that recovery path exists to prevent.
- **`script_max_contracts` was not the hard cap it is advertised as** — when the
  user's cap was below the exchange's 5-contract per-order floor, both the crypto
  entry path and the signal-follow path raised the order to the floor, placing up
  to 5x the size the rail allows. Both now refuse the entry (and record why)
  instead of overshooting a cap the UI calls hard. The script backtester's signal
  replay mirrored the old behavior and was updated to match, with a test pinning
  the two together.
- **`on_fill` / `on_settle` never fired for signal follows** — the lifecycle scan
  only looked at crypto-window positions, so a script built on `decide_signal`
  (whose positions land in the main position table) never saw its own fills or
  settlements, despite the hooks being documented without qualification. Both
  tables are now scanned, with the delivery key namespaced per table so their
  independent id sequences can't suppress each other's notifications.
- **A large script `state` silently wiped itself** — the persist path truncated
  the serialized state at 64KB, and a JSON string cut mid-value doesn't parse, so
  the next load reset the strategy's memory to `{}` *and* the write had already
  destroyed the last good snapshot. Oversized state is now dropped (keeping the
  previous good copy) with a warning telling you to trim it.
- **Enabled scripts over the concurrency cap ran silently** — scripts past
  `script_max_enabled` were dropped from the pass while still showing as enabled
  in the UI. The engine now names them in the log so a script that isn't running
  can't look like one that is.
- Deleting a script now releases its compiled module from memory instead of
  leaving it resident for the life of the process.

## [1.1.1] — 2026-07-12

### Fixed
- **macOS: live price feeds no longer fail SSL verification** — the frozen backend's
  Python has no usable system cert store on macOS, so every `wss://` connection
  (Coinbase spot, Polymarket RTDS, CLOB book) died with CERTIFICATE_VERIFY_FAILED
  while REST kept working. WebSocket handshakes now verify against the same bundled
  certifi CA store httpx uses.
- **WebSocket reconnect loop could die permanently** — after ~17 hours of continuous
  connection failures the exponential backoff overflowed (`2.0 ** 1024`) and killed the
  reconnect task for the rest of the run. The exponent is now clamped.
- **Reasonless "GET failed" log lines** — httpx network errors often stringify to
  empty; failures now log the exception type so outages are diagnosable from user logs.
- **"Invalid order payload" rejections now log the exact order parameters** (price,
  size, tick, neg_risk, token, signature type) so the cause can be identified from a
  user log.
- **Underfunded-balance skip spam** — the crypto engine logged "balance funds only N
  contracts" on every evaluation tick (1,400+ identical lines/day); now throttled to
  once per asset per 5 minutes.
- **Withdrawals no longer read as permanent lifetime loss** — 1.1.0's transfer
  detection excluded deposits/withdrawals from *today's* P&L only; the all-time
  figure kept the raw balance delta, so any withdrawal showed as negative lifetime
  P&L forever (in the app and in leaderboard profiles). All-time P&L now nets out
  every recorded transfer, across all days.
- **Session P&L is also transfer-adjusted** — a mid-run withdrawal faked a deep-red
  session, and a mid-run deposit faked a green one (session P&L gates the anonymous
  leaderboard post, so a deposit could publish inflated numbers). Transfers detected
  during a run are now booked against that run and netted out of the session figure.
- **"Wallet key not configured" warning spam** — the api-cred warning pair repeated
  every poll (~2,900 lines/day) for users who simply hadn't connected a wallet yet;
  now logged once per 10 minutes with a pointer to Settings.

### Added
- **Copy trading: "Copy repeat buys" toggle** — when a followed wallet adds to a
  position you already copied (at least "Min trade to copy" worth), the bot places
  another copy at your normal size. Off by default (previous behavior: one copy per
  market/side).
- **macOS DMG ships a "HOW TO USE.txt"** explaining Gatekeeper's "damaged and can't
  be opened" message for unsigned apps and the one-line `xattr` fix.

## [1.1.0] — 2026-07-10

### Fixed (long-run reliability audit, 2026-07-09)
- **Copy engine could mass-sell every copy during a partial API outage** — a failed
  followed-wallet /positions read returned an empty list, indistinguishable from "the whale
  sold everything", so a Data-API 403/429 stretch dumped the whole copy book at the bid.
  Position reads now distinguish failure from genuinely-flat, and the exit pass skips any
  tick where a wallet read failed (retried 30s later with real data).
- **A withdrawal no longer reads as a daily trading loss** — "Today P&L" is a raw balance
  delta, so moving $50 out permanently "breached" the daily stop (and with flatten-on-stop
  enabled, market-sold the book ~3 minutes later), while a deposit masked real losses.
  Balance jumps larger than anything trading could explain are now detected as transfers,
  excluded from the daily P&L / daily stop, and logged.
- **Losses can no longer evade the crypto15m loss breakers after a long outage** — the
  chain reconcile that rescues fills mis-marked "canceled" only looked back 45 minutes; the
  window now widens to cover the whole gap since the last successful pass (up to 48h).
- **Trading geoblock is now surfaced** — when Polymarket rejects orders with its
  region-restriction 403 (market data keeps flowing, so the app previously looked healthy
  while every placement bounced), the Dashboard shows a persistent banner until orders
  succeed again.
- **Research-signal resolution can no longer wedge** — settlement lookups were capped per
  pass by *successes*, so a backlog of unresolvable signals burned hundreds of requests per
  pass and, at 200, permanently stopped resolution — silently starving the sniper's
  calibration auto-pause below its minimum sample (the gate then never fires). Attempts are
  now capped, and signals with no settled outcome 3 days after close are parked.
- **Live-book subscriptions no longer grow forever** — the CLOB websocket never forgot a
  token (~4,000 new per day on the 5m series); expired-window tokens now age out, keeping
  the subscription frame at the size of the live market set.
- **External-exit detection now runs automatically** (every 10 min, not just on the manual
  Refresh) — a position sold directly on polymarket.com previously stayed "open" in the app
  for days, pinning position caps and booking phantom settlement P&L.
- Model-calibration auto-pause and the CLI `--c15` report are interval-scoped like the
  backtest (5m data can't drive the 15m sniper's pause/resume); the snapshot cache is
  interval-keyed so switching windows can't cross-label recorded rows; the entry-window
  clamp respects the traded window's length (hourly strategies can use up to 60 min).
- **The app stays responsive for multi-day runs** — log lines are batched (each line
  re-rendered the entire UI, 24/7, even minimized to tray), canvas dashboards pause drawing
  while the window is hidden, window drags no longer write settings to disk per pixel,
  stalled-backend polls no longer stack, and UI calls fail fast when the backend restarts
  instead of hanging until their timeouts.
- crypto15m's spot/indicator connection pool now heals together with the main pool when a
  poisoned-pool rebuild fires (previously only an app restart recovered it); a one-shot
  config push after a backend crash-restart is retried (a single missed RPC left the engine
  running on defaults — trading off — until a setting was touched); the Terminal page no
  longer mislabels 5m/hourly positions as "15m", drops the minus sign on loss tooltips, or
  corrupts its chart when switching between live and idle modes.
- **The main (whale/momentum) engine now has a lifetime drawdown breaker** like crypto15m
  and copy: the daily stop re-arms every UTC midnight, so a net-negative engine could bleed
  the daily cap forever. New `lifetime_loss_limit_pct` (default 50% of starting bankroll) /
  `lifetime_loss_limit_usd` pause new entries once cumulative realized main-engine loss
  reaches the cap; uses wipe-surviving counters and deliberately excludes reconcile-imported
  "external" rows.
- **A genuinely empty account no longer trades phantom money** — a confirmed $0 balance
  used to fall back to the pinned start bankroll for sizing, producing ~28 rejected
  "not enough balance" orders an hour until funds arrived; a real zero now cleanly skips
  entries (an *unknown* balance still falls back, unchanged).
- **Reconcile fails closed during partial outages** — when the settled-market check itself
  errors, the residual-token import is skipped for that pass instead of imported as a fresh
  open position (one phantom loss per residual per outage, the 2026-07-07 drain pattern).
- **Restarts no longer pause entries for ~2 minutes** — the wallet instance-lock is released
  on shutdown instead of waiting out its 120-second staleness window (a whole missed window
  on the 5m series, every update or watchdog respawn).
- **Shutdown cancel-all now also sweeps crypto15m maker-style resting entries** (taker FAK
  never rests, but maker GTC entries could fill unmanaged after the app closed); resting
  sell-into-strength asks are deliberately left working.
- **Corrupt settings can no longer wipe out the survival vault** — a corrupt settings.json
  now falls back primary → .bak → vault (each parse-validated), the vault mirror is written
  atomically, and a session running on built-in defaults never mirrors those defaults over
  the vault's last good copy.
- Long-run hygiene: unbounded in-memory registries are size-capped; the copy engine's
  seen-baseline is pruned/capped and only rewritten when it changes (was re-serialized every
  30s forever); hot-path re-entry guards query recent rows instead of full-scanning
  ever-growing position tables; permanently-unsettleable positions retry every ~6h instead
  of every 5 minutes; the crypto15m stop-loss retry is throttled to 90s like the copy
  engine's; and the shared crypto15m preset definitions now live in one module used by both
  the Crypto tab and the Backtest page, so retuning can't silently diverge them.

### Added
- **Per-interval research datasets — 5-minute and hourly strategies are now backtestable** —
  the research recorder has always followed the Crypto-tab interval (5m / 15m / hourly), but
  every recorded row landed in one undifferentiated pool, so switching intervals silently
  mixed datasets and a "5m backtest" would happily fill against 15-minute windows. Signals
  and ticks are now stamped with the interval they were recorded from (existing rows are all
  15m and are migrated as such), and the backtest replays ONLY the window series the tested
  config trades — with an explicit caveat when the selected interval has no recorded data
  yet. To backtest a 5m or hourly strategy: pick that Window on the Crypto tab, let the
  recorder accumulate a few days of markets, then run the Backtest page as usual.
- **"5m Sniper (BTC)" preset** — a starting point for the 5-minute series, modeled on a
  publicly documented, Polymarket-verified profitable bot (PRR "BTC5MScour": +$26.5k over
  27 days, 95.8% win rate — final 60–120 seconds, spot ≥0.05% past the open, buy the
  90–99¢ favorite, hold to settlement, never touch 40–90¢). Available both as a one-click
  config on the Crypto tab and as a replay-only strategy on the Backtest page. It ships
  UNVALIDATED for this app: the equivalent trade measured zero edge on the 15-minute series,
  and the reference bot wins on sub-second execution — record your own 5m data and backtest
  before arming.
- **Update-survival vault** — settings and the recorded research database now outlive
  anything that removes the app's data folder (an uninstall with the "delete app data" box
  ticked, a reinstall gone wrong, a userData reset). Settings are mirrored on every save and
  the newest nightly DB snapshot on every maintenance pass to
  `%LOCALAPPDATA%\Krypt PolyBot Vault\<account>` — a location installers and uninstallers
  never touch. On startup, a missing `settings.json` is restored from its rolling `.bak` or
  the vault, and a missing database is restored from the newest local backup or the vault
  mirror. Recorded backtest data is the evidence every strategy verdict rests on; it should
  never be one update away from gone. (The NSIS installer itself has always preserved user
  data on update/uninstall — the vault covers everything else.)

### Fixed
- **World Cup markets were categorized as "world", hiding the bot's biggest whale edge** —
  Gamma's World Cup events carry no mapped tag and match titles like "Will Jordan win on
  2026-06-22?" contain no sport keyword, so 1,064 soccer whale signals landed in the "world"
  bucket. Splitting them out on 12 days of data (4,152 resolved whale signals): soccer whales
  at 50–85¢ entries are **+13.8¢/contract** (52 matches, per-event t=+3.6) while true world
  markets are flat and soccer *momentum* is toxic (−16.9¢/ct). Category resolution now checks
  event-slug prefixes (`fifwc-`, league prefixes, Wimbledon) and more Gamma tag slugs.
- **Main-engine presets re-tuned on the 50× larger sample** — "Crypto Whale" (+9.3¢ on n=36 in
  June) reversed to **−3.9¢/ct (n=143)** and is now marked not-recommended; "Sports Momentum"
  held up (+5.9¢/ct, n=598) and its description cites the new numbers; "Edge Stack" swaps its
  whale leg from crypto/exotics/entertainment to sports (soccer-led). Through the live
  `should_trade` gates with 1¢ slippage: Edge Stack +2.0¢/ct (n=887), Sports Momentum
  +4.6¢/ct (n=340). All still in-sample; the soccer component fades after the World Cup final.
- **Backtest phantom-fill bug (the "sniper is negative EV" mystery)** — the replay backtester
  mapped the recorded `yes_ask` onto the executable up-token ask, but that column silently
  collapses to the lagging Gamma mid whenever the CLOB WebSocket is cold. Since Gamma sits
  20–67¢ off the real book near close *and that lag is the strategy's edge*, the backtest was
  filling at phantom prices in both directions: it flattered the Paired preset (+$66 entirely on
  sub-$1 combined asks the real book never offered — real-book median pair cost is 101¢) and it
  buried the Settlement Sniper. Replay now refuses Gamma-fallback quotes exactly like the live
  engine does; the recorder stores a new real-book-only `up_ask` column so future data is exact,
  and legacy ticks are only trusted when the favorite's real WS quote was captured alongside.
- **Settlement Sniper preset shipped with self-defeating thresholds** — its "neutral" defaults
  (certainty 0.5, edge 0¢) made the early entry branch consume every window minutes before close
  as weak model-following (measured −1.4¢/contract on real books). Re-tuned to the validated
  final-2-minutes 3σ config (certainty 0.9985, edge ≥2¢, cap 99¢): +8.5¢/contract, 81% win rate,
  −$4.38 max drawdown on the real-book replay (n=42, Jul 3–5 panel — still small/in-sample).
- **Favorite entries are blocked in the final minute** (restoring the Kalshi bot's guard the port
  dropped): a 95¢ favorite seconds before close is a max-gamma bet the stop-loss provably can't
  save (books gap to $0 with no bids). Real-book replay measured final-minute favorite entries at
  −1.2 to −3.7¢/contract. The final minute belongs to model mode's 3σ gate.
- **Preset hints now cite real-book numbers** — the earlier "edge pockets" (Favorite 90–95¢
  "~99% win", Deep Favorite "didn't lose") were measured on Gamma mids and did not survive the
  real-book correction; each preset now states its honest measurement.

### Added
- **Fee-aware signal backtest** (`npm run py:backtest`, `python/backtest.py`) — replays resolved
  signals and reports net-of-fee edge with a significance (t-stat) check, a confidence-threshold
  sweep, and per-category / per-entry-price breakdowns plus a "fade" check. Opens the local DB
  read-only. `--demo` runs on synthetic data; `--breakdown` adds the slices.
- **Test suite + CI** — 80+ pytest tests (`npm run py:test`) covering signal scoring, position
  sizing, the `should_trade` gates, order/position parsing, settlement P&L, the order→DB status
  machine, the reconcile/poll paths, and the 15-minute executor. GitHub Actions (`.github/workflows/ci.yml`)
  runs typecheck + tests on every push and pull request.
- **15-minute crypto module** — a "15m Crypto" tab that monitors Polymarket's seven 15-minute crypto
  series (BTC/ETH/SOL/XRP/DOGE/HYPE/BNB) with a configurable momentum strategy (multi-source spot
  for the underlying delta), plus an optional paper/live executor with a stop-loss exit. Off by
  default; live orders need their own explicit "Real orders (LIVE)" switch, independent of the main
  bot. Every threshold — entry window, favorite level, delta filter, stop-loss, markup, size — is
  editable from the tab. Defaults tuned on 531 recorded settled markets: entry only at 95–98¢, the
  one favorite-price band that didn't lose after fees (small sample — not a proven edge).
- **Maker entry mode (default)** — instead of crossing the spread and paying Polymarket's full taker
  fee (which the replay shows turned every sub-95¢ band negative), the executor rests its entry
  limit at the favorite's best bid: no spread paid, ~zero maker fee. The trade-off is honest — a
  resting order may never fill, and unfilled entries are canceled near close (configurable lead).
  Paper mode simulates resting fills truthfully: an entry only "fills" if the market actually
  trades down to its level. The old crossing behaviour remains as the "taker" style.
- **UTC trading-hours filter** — optionally restrict 15-minute entries to a UTC window (supports
  overnight wrap). Off by default; the collected data leans positive 00–12 UTC and negative during
  the US session, but not yet significantly.
- **15-minute signal logging + CLI backtest** — the app passively logs each quarter's decision-point
  signal (favorite side, price, underlying delta) and its settled outcome to `crypto15m_signals`
  (read-only, no orders, on by default); `npm run py:backtest15m` replays them fee-aware
  (favorite-follow vs contrarian, favorite-price band and delta sweeps, with t-stats). It also logs
  a whole-window tick series (`crypto15m_ticks`, bid/ask + spot every ~25s, pruned after 45 days)
  for studying entry timing, quote staleness, and maker-fill behaviour.
- **Strategy presets** — `Edge Stack`, `Crypto Whale`, and `Sports Momentum` for the main engine
  (data-derived from the author's signal history, ranked by a risk-adjusted backtest, marked
  experimental/in-sample); two 15-minute starting points (Deep Favorite, Contrarian Fade) and a
  favorite/contrarian direction mode for the 15-minute executor.
- **Open-source release scaffolding** — MIT `LICENSE`, rewritten `README`, `DISCLAIMER`, `SECURITY`,
  `CONTRIBUTING`, `.gitattributes`, and a hardened `.gitignore`.

### Security
- **Credentials are now encrypted at rest** on Windows via the OS keystore (DPAPI) instead of
  plaintext; existing plaintext files are upgraded automatically.
- **Server-side config validation** clamps money-critical knobs (sizing fractions, entry prices,
  stop-loss/take-profit, position caps) to sane ranges before they can drive an order.
- **Electron hardening** — `openExternal` restricted to http(s)/mailto, window-open + navigation
  guards (external links go to the OS browser, no in-app navigation), and a Content-Security-Policy
  on packaged builds.
- **15-minute delta is now percentage-based** (`crypto15m_min_delta_pct`); the old absolute-USD
  threshold couldn't work across assets (BTC vs DOGE).

### Notes
- The bundled strategies are heuristics and do **not** yet account for Polymarket fees in their entry
  logic. A fee-aware backtest of the scanners' signals found the default configuration net-negative
  after fees; only filtered slices (e.g. whales in crypto, momentum in sports) backtested positive,
  and only in-sample. **Run the backtest and paper-trade before risking money.** See
  [DISCLAIMER.md](./DISCLAIMER.md).
- The 15-minute presets are unvalidated — the bot has no settled 15-minute history yet.
