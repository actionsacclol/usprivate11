# Polymarket US port

Forked from Krypt PolyBot at `1d92c62` (v3.5.0). This app trades the Polymarket US exchange, which is CFTC-regulated and has a different API, account system and market list from the international venue. It installs and runs beside the original: its own name, app ID, data folder, vault and MCP port (47635).

**Status: paper mode verified on live data. On the owner's real account, signing, account reads, both WebSockets, a resting order, and real YES and NO round trips have passed through the backend's API layer (`npm run py:orders -- --with-fill`, 2026-11-04). No order has gone through the app's own screens or an AI agent on a real account yet, so treat those, and the app's records of a fill, as untested.**

## What has been verified, and how

| Area | Evidence |
|---|---|
| Market list, search, books, price history, resolution text | Real backend over its RPC interface, against the live public API |
| Crypto board: strike | Matches the venue's `priceToBeat` on BTC, ETH, SOL and XRP |
| Crypto board: quotes | Compared with an uncached venue read, 35 seconds into a run |
| YES means Up | 100 of 100 settled 15-minute windows |
| Paper orders and portfolio | Real backend, real books |
| Kill switch | Real button in the real app; latches, survives a restart, clears |
| A rejected API key | A throwaway key returns the app's "rejected" message; the secret is on no log line |
| Every page | Opened in the real Electron app; screenshots reviewed for the main ones; no wording left over from the old venue |
| Signing with a real key | `npm run py:orders`, owner's account, 2026-11-04 |
| Balance, positions and open orders read | Same run; the account held no positions, so a held position's fields weren't read |
| Markets and private WebSockets connect | Same run: a live quote, and subscribed to our orders. No order event was checked |
| Resting order placed, then canceled | Same run: size 1 probe at $0, canceled |
| A NO order's price is on the YES side | Same run: the 0.201 probe appeared in the public book as an offer at 0.201, with no bid at 0.799. Buying NO unlocked for this key |
| Real fills, buying and closing YES and NO | `--with-fill`, owner's account: size 1 on a BTC hourly market, each order `matched` and each position closed |
| A held position's sign | Same run: YES reads as `netPosition` 1, NO as -1, as the app assumes |

## What has not been verified

These are written from the published docs and tested only against stubs:

- **The app's record of a fill:** the check confirms the exchange filled the order, not what the app's trade records, P&L and fee fields show.
- **Account activity:** field names.
- **Order events on the private WebSocket, and the authenticated market feed under load.**
- **Fees on resting orders.** When the venue reports no commission, the fee shows as unknown.
- **An order through the app's own screens.** An AI agent's orders on the real account have now run: the owner's Claude placed live buys and sells on 2026-11-05 and the app logged real fills. Nobody has yet checked those trades' records, P&L and fees against the exchange.

## Last full check, 2026-11-05

| Check | Result |
|---|---|
| Python tests | 2,407 passed on 3.14. CI's 3.11 last ran before the alerts: 2,282 passed |
| Backend selftest | Frozen build passes, from the 3.14 venv, including the history download |
| Live backend run over RPC, public data only | 46 of 46, last run 2026-11-04 |
| Live provider check (`npm run py:live`), public data only | 28 passed, 0 failed, 0 warnings |
| Every host the running backend looked up, no key saved | Polymarket: only `gateway.polymarket.us`. Also Coinbase, Hyperliquid and Kalshi |
| Real app, arming spec (`npm run test:e2e`) | 24 of 24 |
| Renderer tests (vitest) | 608 passed |
| Typecheck | Clean |

The tests stub the exchange for every signed path. A Windows installer was built locally on 2026-11-05 (121 MB, from the 3.14 venv rather than CI's 3.11). The unpacked app ran on clean profiles, walked as a new user with no key: the first-run choices, a real download, closing and reopening mid-download, paper orders moving the balance, and backtests on the result. The installer itself has not been run. Mac and Linux builds have not been tried.

## Public history, measured 2026-11-05

These are spot checks, not a documented retention policy. `python/history_download.py` caps its requests to them, and says so when the venue runs out sooner.

| Data | How far back |
|---|---|
| 15-minute and hourly up/down windows: one-minute bid and ask, plus outcome | BTC: 43 days readable, gone by 45. ETH, SOL and XRP: 20 days readable, gone by 25 |
| 5-minute windows | All four coins: 13 days readable, gone by 14 |
| Spot from `/v1/asset-prices/history` | One-second candles at 40 days; 15-second candles at 60 days; gone by 90 |
| Settled non-crypto markets: one-minute quotes and outcome | Back to at least January 2026, on 8 sampled markets |

No public history of trades or book depth exists, so downloaded data can't show how much size was available.

**Timing.** `/v1/price-history` stamps a quote with the start of its minute but gives the price at the end of it. Compared with windows the app recorded, the two copies agree best at +60 seconds: median gap 2 cents, against 6 at face value. The download times each quote at the end of its minute, because at face value a backtest would trade on prices a minute before they existed.

**Cross-check against 8 recorded windows, after that fix:** same outcome on all 8, identical strikes, asks within 2 cents at the median with no lean toward cheaper prices, spot within 0.01% at the median. That is 8 windows from one morning, so it shows the mapping is right, not that a strategy's results will match. Quotes where the bid is above the ask (3 of 128) are dropped.

**Gaps.** A week's download on 2026-11-05 got 3,289 of 3,352 windows. The other 63 fell in two stretches, on Oct 29 and Oct 31, across all coins, where the venue has no strike or no quotes. Of those that had settled, 16 went up and 15 down, so skipping them doesn't favour a side.

**What the data can't do:** one price a minute against about one every 25 seconds, and no order sizes. VWAP comes from one-minute prices with no volume. A strategy can look better on it than it would trade, so a replay's first caveat says how many of its windows were downloaded. Downloaded rows carry `spot_source` and `strike_source` of `venue-history`, and the live model-calibration check ignores them.

## Checked against docs.polymarket.us, 2026-11-05

Every call the shipped app makes to Polymarket goes to `gateway.polymarket.us` or `api.polymarket.us`, and no shipped code or bundle contains `polymarket.com`. The dev scripts in `python/research/` do name the old hosts; they are not in the build.

Compared with the docs: the hosts, signing, every endpoint and parameter used, order fields and states, positions, activity, balances, both WebSockets, candles and price history. Fixed in the check:

- **Fees.** Non-crypto positions were booked at the original venue's rates: 0.05 sports, 0.04 politics and finance, 0 geopolitics. All are 0.0695 now. Rebate tiers are the US ones, 0 to 30%.
- **Deprecated fields.** `outcomePrices` decided settlement. The docs replaced it with `marketSides[]` and announced removal, though it is still sent. The app reads the sides first and gives the same result without the old fields, for open, resolved and split markets (`test_us_docs_conformance.py`).
- **NO price.** With one side priced, 258 of 600 live rows, the NO price read as the YES price.
- **Voided trades.** `TRADE_STATE_BUSTED` and `TRADE_STATE_REJECTED` rows no longer count as fills. **Halted markets** no longer read as open.

Known gaps, all small: engine positions in Table Tennis (0.10) are booked at the standard rate, because those rows don't carry the market's own rate. Fee estimates skip the venue's rounding to the cent, which can round a very small fill to $0. Gold and silver up/down markets (listed 2026-10-22) appear in Discover like any market but have no crypto card. 5-minute windows exist live but aren't in the docs, so they could change unannounced.

The docs disagree on some points: weekly maintenance is Thursday 6-8 AM ET on the hours page and 2-6 AM in the changelog, and the crypto FAQ still calls live markets preprod-only.

The venue changes often, and gave three days' notice when it deprecated fields in June. Subscribe to `https://docs.polymarket.us/changelog/rss.xml`.

## Outside sources, checked 2026-11-05

Read-only, against the live services and through the app's own code, with no keys. A 401 from an AI or chat service only proves the route answers, so calls with real keys are untested.

| Source | Used for | Result |
|---|---|---|
| Coinbase (socket, REST) | Live crypto price, then the first REST price | Working, all 4 coins |
| CoinGecko | Last-resort price | Working, now at full precision (XRP rounded to 2 decimals before) |
| CryptoCompare | Removed | Answers 401 without a key |
| Hyperliquid | Live indicators | Working. Downloaded history uses the settlement index, so the two differ slightly |
| Kalshi | Opt-in comparison on a market page | Working, see below |

**Kalshi matching.** The Fed question "cut greater than 25bps by Dec 31" paired with Kalshi's January "Cut 25bps" as a strong match with a price gap. A Fed question now has to name its own meeting, and the text scorer can't pair Fed legs. Season winners were missed because Kalshi words them differently and pads its close dates by about two years, so the slug now looks them up (`_SEASON_WINNERS`). On live data all 282 such markets were checked: 281 paired, with a median gap of 0.5c and none sharing a Kalshi leg, and the last paired once accents were handled. The December Fed meeting's 5 legs paired correctly. The no-match note no longer says Kalshi has no such market. NFL games were already right. BTC 15-minute windows have not been seen matching live: Kalshi had no window open at 3 AM ET.

**Claude.** New installs default to `claude-opus-5-5`. A saved model is kept. Analysis uses medium effort on the 5.5 models and allows 16,000 tokens, since thinking counts toward the limit. The autopilot allows 16,000 on Anthropic, and a turn cut off by the limit now ends as an error, not as a success. Not added: the server-side `fallbacks` opt-in for a declined request. The app says the model declined. Add it when a key is available to test it, since a wrong parameter would fail every call.

## Claude Code's Add button

Added 2026-11-05. It runs `claude mcp add --scope user`, because `.claude.json` is the live state of every running session and the app shouldn't write it. Clicked in the packaged app with the real `claude` on a scratch `CLAUDE_CONFIG_DIR`: `claude mcp list` reported Connected, the relay listed 36 tools, the app showed Claude Code as connected, and Add again and Remove both worked. Not run: a real Claude Code session asking a model to use the tools, or macOS. The npm `claude.cmd` launch is tested with a stand-in script.

Krypt Studio and Krypt Editor also add their server to Claude Code's `permissions.allow`. This app doesn't, on purpose: their edits can be undone and an order can't, so Claude Code keeps asking before each tool. The entry holds the path of the app that added it, so moving from an unpacked build to an installed one needs Add pressed again.

## Guide videos

`tools/guide-videos` makes them and its README is the how-to. `videos.json` holds every line, click and highlight;
`capture.mjs` films the real app on a throwaway profile; `tts.py` voices it with Kokoro through Krypt Studio's analyzer;
`render.py` builds the video. `tools/setup-video` is the original app's single ElevenLabs video and isn't used here.
Rerun a video when its screen changes. Nobody has checked the voice's pronunciation by machine: Studio has no speech-to-text
model, so a person has to listen.

## What was removed, and why

| Feature | Reason |
|---|---|
| Whale tracker, copy trading | The venue publishes no per-trader data |
| Trade tape | The venue has no public trade feed |
| Perps | Not offered on this venue |
| Main Engine signals | They were built on the whale and momentum data above |
| Paired mode, volume farming | Paired mode relied on separate YES and NO books. Wash trades are prohibited by rule 5.2(p)/(q) |
| 4-hour crypto series | Not listed here |
| Favorites Harvester preset | It needed 24h volume and game-time closes, neither of which exists here, so it could never fire. Its evidence also came from the other venue |
| Setup video | It showed the original app |

Left in place as dead code: `scan_whales` and `scan_momentum` in `scanner.py`. Nothing calls them.

## Venue facts, measured 2026-11-05 UTC

**Hosts and signing**

- Public data: `https://gateway.polymarket.us`. Signed requests: `https://api.polymarket.us`.
- WebSockets: `wss://api.polymarket.us/v1/ws/markets` and `/v1/ws/private`, signed at the handshake.
- Each signed request carries `X-PM-Access-Key`, `X-PM-Timestamp` (ms) and `X-PM-Signature`: base64 Ed25519 over `timestamp + METHOD + path`, with no body or query string. The window is 30 seconds.
- There is no idempotency key, so a POST is never retried.

**Markets**

- Each market has one book, quoted on YES. NO is its mirror. The app's token IDs (`<slug>#yes`, `<slug>#no`) are its own invention.
- `marketSides[].long` identifies YES. `outcomes` can arrive reversed (19 of 107 rows in one page) while prices stay YES-first.
- `outcomePrices` has only one entry on 81 of 300 live rows, and that entry holds the NO price.
- The market list has no volume, liquidity or open interest figures. These read as 0 in the local table, so nothing may gate on them. Three things did, and each emptied or disabled something: a sync filter, a default script setting, and a cleanup job that deleted every market after each sync.
- **An empty quote on the market row does not mean an empty book.** On 20 of the 40 newest markets, `bestBidQuote` and `bestAskQuote` were null while the book was two-sided. Anything that needs a price has to fall back to the book.
- Sports `close_time` is the exchange's settlement deadline, usually days after the game. It says nothing about when the game starts.
- The 5-contract and $1 minimums are this app's own floor. Markets state their own `minimumTradeQty`, 1 or 0.01.

**Freshness**

- `/bbo`, `/book` and `/v1/events/slug` are CDN-cached with `max-age=30`. `/v1/market/slug/{slug}` and asset prices are not cached. In one test `/bbo` sat at 76¢ while the market route moved from 76¢ to 93¢ in 15 seconds.
- With no API key there is no live feed. The running crypto window is re-read uncached instead (`priceSource: "rest"`), but other markets can be up to 30 seconds behind, and the app says so where it matters.

**Crypto windows**

- Slugs follow `{asset}-updown-{5m|15m|1h|1d}-YYYY-MM-DD-HHMMz`.
- The 1-second index candle at T-1 equals `priceToBeat` exactly.
- The index is a trailing 60-second mean and lags Coinbase by about 39 seconds, so the index is the settlement reference and the Coinbase feed is live spot.
- YES won if and only if `settlementPrice >= priceToBeat`. The market text says ties settle Up.

**Limits and rules**

- `/v1/markets` pages hold at most 500 rows; `limit=1000` returns a 500 error.
- One session reported a limit of about 5 requests per 10 seconds on `/bbo` and `/book`. I could not reproduce it: 30 markets in about 5 seconds all returned 200 on the market, BBO and book routes. Treat it as unconfirmed.
- The rate limit is 25 requests a second per IP, shared across API keys (`api-reference/rate-limits`). The Orders page still says 20 per key. The app stays under both.
- Rulebook 5.2(i) expects throttles, collars and kill switches. All three are in `polymarket_api.py`.
- Sign-up needs identity verification with an SSN, and funding happens in the Polymarket US app.

## Pre-trade guards

All of these run on the way to the exchange in `place_limit_order`, and refuse locally when they trigger:

- **Kill switch:** latches, and survives a restart.
- **Throttle:** limits how many orders can go out.
- **Price collar:** refuses a price too far through the touch. It allows 20¢ against a fresh quote and 40¢ against one that may be up to 30 seconds old, so it catches fat fingers rather than ordinary slippage. A buy is refused if no price can be read at all. A sell still goes, so a failed read can't trap anyone in a position.
- **Self-cross check.**
- **NO-buy lock**, until the live check passes.

## Behaviour that differs from the original

- No tape, 24h volume or liquidity. 24h change appears only in Discover's Volatile column.
- No redemption step, and no neg-risk check, so the resolution report has 10 checks instead of 11.
- The sibling-contradiction threshold is 0.9.
- The fills ledger covers orders placed from the terminal.
- An order in an unknown state settles only if there is no unexplained trade.
- Paper book reads are capped at 2 per tick and can be up to 30 seconds old. A paper fill can therefore differ from the current market (seen once: filled at 80¢ with the market at 74¢), and the app shows a warning when that happens.
- Kalshi matching found 51 of 70 games in one check, with a largest gap of 3.5 points.

## Account setup guide

`src/components/AccountSetup.tsx` is a step-by-step guide, shown in onboarding right after the disclaimer and on the API Key page. It also has entry points on the Dashboard, the Guide page and Settings. The steps are: sign up, verify identity, fund, create a key, paste it. Someone with an account only does the last two. It remembers the current step in `localStorage`, holds no credentials, and never leaves Paper. Links open in the person's own browser.

The invite and squad links belong to the author. The copy says so, and says any Polymarket US account works.

**Squad "auto-join".** The first time a key connects on a machine, by either path, the app opens the squad page in the browser, and the person clicks join there. It can't join for them: the docs publish no squad API, and a key's documented scope is trading, portfolio and WebSocket. Nothing on the account changes until they click, and the app can't tell whether they did. The copy doesn't push someone with an account towards a second one, since the invite is for new sign-ups.

Where each statement comes from, read 2026-11-04 (no account was created to check them):

| Statement | Source |
|---|---|
| "Join yuhgo", code YUHGO, $25 to both, Continue with Google or Apple | The invite page |
| "Join Krypt on Polymarket" | The squad page, which has no other text |
| A squad has a chat | The owner, 2026-11-04. No public page describes squads |
| A code must be applied during sign-up; $25 each after sign-up, onboarding and a $10 deposit | docs.polymarket.us/faqs/refer-a-friend-faqs.md |
| App first, then identity check, developer page, Secret Key shown once, support address | docs.polymarket.us quickstart |

**Not confirmed, so the copy doesn't state them:** the labels on the squad page, whether joining needs a finished identity check (the guide opens it after the key connects, so this shouldn't matter), the labels on the developer page, how deposits work, whether the invite page creates the account or hands off to the phone app, and Android availability from an official source.

## Wallet

`src/components/WalletDialog.tsx`, opened from the top bar's balance and the sidebar card. Refresh calls the `accountRefresh` RPC, a forced `GET /v1/account/balances`. A failed read keeps the last numbers and `account.wallet.readOk` says so.

**Withdrawals and deposits can't be done in the app.** The retail API has no endpoint for either (`docs.polymarket.us/api-reference/introduction`), and `learn/deposits/withdraw-funds/overview` says withdrawals happen in Polymarket's app. `partners/funding/transfers` is firm-only: it needs a three-party funding agreement and a retail key can't call it. So the buttons show steps, and a key can't move money out of an account.

**Not verified on a real account:** `openOrders` and `pendingWithdrawals` come from the docs' schema. The owner's account has not shown a pending withdrawal, so the unit of its `balance` field is assumed to be dollars, like `buyingPower`.

## Discord status

`electron/system/discord.ts`, on by default with a switch in Settings. It uses the suite's shared Discord application, named `krypt.cc`, so Discord shows "Playing krypt.cc" above `Trading on Polymarket US` and `krypt.cc/tools/polyus`. No balance or trade is ever sent. It retries every 30 seconds while Discord is closed.

That application has no uploaded art (`/oauth2/applications/<id>/assets` is empty), and the real client drops the `krypt` image key from its reply, so Discord falls back to the application's icon and the hover text may not show. Upload an asset named `krypt` to show its own image.

## Alerts

`src/components/AlertStack.tsx` shows what is wrong right now in the bottom-right corner, with what to do and an X to close it. Each alert is a condition rebuilt on every 10-second poll (`python/alerts.py`), so it goes away when the problem does. Closing one keeps it closed until the problem has been gone for a minute and comes back, or gets worse. Toasts stay for one-off events.

Polling sends nothing signed and calls no AI provider. It adds at most one unsigned public read, every 45 seconds when the exchange is otherwise quiet and every 8 seconds while it is failing.

| Alert | How it was exercised |
|---|---|
| Fresh install on Paper raises nothing | Real app, past every grace period |
| Can't reach Polymarket US | Real app, with all traffic sent to a dead proxy |
| API key rejected | Real app, a random key, Polymarket US's real answer |
| Kill switch, then paused with Resume | Real app, real buttons |
| AI agent server can't start | Real app, with the port really taken |
| AI client disconnected | Real app and the real `--mcp` relay |
| Engine crashed, with Restart | Real app, engine process killed until respawning stopped |
| Exchange errors or rate limiting, location refusal, order pause, daily limit, account held elsewhere, stale balance, script stopped, engine loop crashing, Autopilot, chat bots, price feeds | Tests only |

**Limits.** A client that dies without closing its connection looks the same as an idle one, because agents are silent between tasks, so only a clean close raises an alert. Clients on the 2026-07-28 MCP revision have no session to close, so they raise none. The location alert depends on the wording of Polymarket US's refusal, which has not been seen on a real one. A plain 403 raises the key alert instead.

## Known leftovers

- Keys kept for compatibility: the `"wallet"` status key, the `flatten_wallet` tool name, and page ID `visualizer`.
- "THE WHALE" is still a boss name.
- `docs/GUIDE.md` still describes the original app. The in-app Guide page is ported.
- The crypto cards keep the original's Paper behaviour: the engine sends nothing in Paper. The owner chose to leave this.
- The Agent Hub failed to open once in a scripted run, within 7 seconds. It did not recur in later runs, including first loads in Advanced (3.0 seconds).

## Sources

- docs.polymarket.us
- The `polymarket-us` package on PyPI
- The Polymarket US rulebook
- Live public API reads, as dated above
