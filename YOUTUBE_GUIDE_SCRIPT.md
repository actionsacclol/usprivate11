# Krypt PolyBot — YouTube Setup & Guide Video Script

**Target length:** ~14–18 minutes. Covers setup, all three engines, the key
settings, and how to actually use it. Narration is written to be read aloud —
tweak the wording to your voice. `[VISUAL]` cues tell you what to show on screen.

> Two things to drop in your video description every time:
> - **Get started (deposit $20 + a first trade for up to $50 in trading credits):** https://polymarket.com?via=yuhgo
>   (this is the Dub partner link — it records the click, then forwards to the
>   `?r=yuhgo` referral, so both are in the chain. Paste it; never retype it as a
>   spoken "slash r equals" short form, which drops the Dub half.)
> - The bot is **free**. Follow **@YuhgoSlavia**.

> **Do not read the URL aloud.** The link is the Dub partner link
> (`polymarket.com?via=yuhgo`), which records the click and then forwards to the
> `?r=yuhgo` referral. A viewer typing a spoken "slash r equals yuhgo" lands on
> the referral directly and silently drops the Dub half. Point at the
> description every time; if you want something speakable, make a short Dub
> link and read that.

---

## 0. COLD OPEN / HOOK (0:00 – 0:25)

[VISUAL: fast montage — the dashboard with a green P&L number ticking up, a
couple of "filled" positions, the boss-fight widget, then the Krypt logo.]

> "This is a free desktop app that auto-trades Polymarket for you — whales,
> momentum, 15-minute crypto markets, and even copy-trading other people's
> wallets. In the next 15 minutes I'm going to take you from zero to a running
> bot, and walk through every setting that actually matters. Let's go."

---

## 1. WHAT IT IS (0:25 – 1:30)

[VISUAL: slow pan across the sidebar — Dashboard, Positions, Signals, Crypto,
Copy Trading, Strategies, History, Settings, Wallet.]

> "Quick overview so you know what you're looking at. Krypt PolyBot is a desktop
> app — it runs locally on your machine, your keys never leave your computer.
> There's a Python trading engine doing the work and a clean UI on top.
>
> It has **three independent engines**, and you turn each on separately:
> - The **main bot** follows two kinds of signals — big 'whale' trades and price
>   momentum — across all of Polymarket.
> - The **15-minute crypto** engine trades Polymarket's recurring 'will Bitcoin
>   go up or down' markets.
> - And **copy trading** mirrors any wallet you point it at — when they buy, you
>   buy; when they sell out, you sell.
>
> Important up front: **there is no paper-trading mode.** Every engine places
> real orders with real money the moment you turn it on. So we start small.
> I'll come back to safety at the end."

---

## 2. STEP 1 — GET A POLYMARKET ACCOUNT (1:30 – 3:00)

[VISUAL: the onboarding screen, Step 1 highlighted, then click through to
polymarket.com signup.]

> "The bot trades *through* a Polymarket account, so before anything you need
> one. If you don't have an account yet — and honestly even if you do, a fresh
> one is cleaner — use my link in the description. It's my referral, it costs
> you nothing, and if you deposit
> **twenty bucks** and make your first trade, Polymarket gives you **up to fifty
> dollars in trading credits** — that's their current new-user offer. It's trading
> credit, not instant cash, and the terms are Polymarket's so they can change. It
> also supports the channel, so thank you for using it.
>
> [VISUAL: Polymarket signup — click 'Continue with Google'.]
>
> Easiest way to sign up is **Continue with Google** — pick a Gmail, takes ten
> seconds. Confirm Polymarket works in your region first; if it's blocked where
> you are, that's a Polymarket thing, not the bot.
>
> [VISUAL: deposit screen.]
>
> Once you're in, **deposit some USDC.** Start with an amount you're genuinely
> okay losing — I'd say twenty to fifty bucks to learn on. Polymarket converts
> it into your account balance automatically.
>
> [VISUAL: Polymarket Settings → Trading → auto-redeem toggle.]
>
> One setting to flip on Polymarket itself: go to **Settings, Trading, and turn
> on Auto-Redeem.** That makes your winnings convert back to cash automatically
> instead of sitting as winning tickets. Do it now, you'll forget later."

---

## 3. STEP 2 — CONNECT YOUR WALLET (3:00 – 4:30)

[VISUAL: the app's Wallet page.]

> "Back in the bot, go to the **Wallet** page. The bot needs two things from
> Polymarket: your **private key** and your **deposit-wallet address**.
>
> [VISUAL: Polymarket → export private key flow; copy deposit address.]
>
> In Polymarket, export your private key — it's usually under your account
> details — and copy your deposit-wallet address. Big warning: **your private
> key is your money.** Never share it, never paste it anywhere except this app,
> and only use a wallet with funds you can afford to lose.
>
> [VISUAL: paste both into the Wallet page, click Save & connect, then Test.]
>
> Paste both in, hit **Save and connect**, then **Test**. If it's good, you'll
> see your balance and a 'ready to trade' confirmation. If it says connected but
> *not* ready — usually that's zero balance or missing approvals — it'll tell
> you exactly what's wrong. Fund the account or wait for approvals and re-test."

---

## 4. DASHBOARD TOUR (4:30 – 5:45)

[VISUAL: Dashboard.]

> "This is your home base. Up top: **total balance**, your **session P&L** —
> that's profit since you launched the bot — and **ROI**. Below that, **open
> positions** and an all-time view.
>
> [VISUAL: point at the status card.]
>
> The status card on the right is your at-a-glance health check — is the engine
> online, is a wallet connected, is auto-trading on or paused. If the backend
> ever crashes, a big **Restart engine** card shows up right here so you can
> recover with one click.
>
> [VISUAL: top bar — Start Trading button, Pause, Restart.]
>
> And the top bar is your master controls for the main bot: **Start Trading**
> turns it on — and yes, it'll warn you it's real money — and that same button
> becomes **Pause**, which is your emergency stop. There's also a **Restart**
> button for the engine; remember that one, you'll need it after changing
> settings sometimes."

---

## 5. THE MAIN BOT — SETTINGS THAT MATTER (5:45 – 9:00)

[VISUAL: Settings page, scroll slowly through each section as you talk.]

> "Let's tune the main bot. Settings page. I'll hit the ones that actually move
> the needle.
>
> **Signal sources.** Up top you choose what the bot follows: **whale signals**
> — big taker orders, smart-money-style flow — and **momentum** — price and
> volume divergences. Turn on whichever you want. Convergence is marked 'coming
> soon,' so ignore it for now.
>
> [VISUAL: confidence + edge fields.]
>
> **Confidence and edge thresholds.** Every signal has a confidence score and an
> 'edge' — basically how mispriced the bot thinks it is. Raise these to trade
> less often but higher quality; lower them to trade more. The 'edge' is the gap
> between the bot's confidence and the market's price.
>
> [VISUAL: entry price band.]
>
> **Entry price band.** Min and max entry price in cents. The bot won't buy a
> contract priced outside this band. Default is roughly 15 to 85 cents — that
> skips the dirt-cheap longshots and the near-certain favorites.
>
> [VISUAL: bet sizing toggle.]
>
> **Bet sizing** — this is important. Two modes:
> - **Percent of balance:** you set a min and max percentage, and the bot sizes
>   bigger when its edge is stronger.
> - **Fixed contracts:** a min and max number of contracts, again scaled by edge.
>
> Both are capped by your **hard max position** — an absolute dollar ceiling per
> trade. Set that to something sane so one trade can't blow up your day.
>
> [VISUAL: concurrency + risk.]
>
> **Risk rails** — these are the ones that keep you safe:
> - **Max open positions** and **max daily new positions** — caps on how much
>   it's juggling.
> - **Max positions per event** — keep this at 1 so the bot doesn't pile three
>   bets onto the same game.
> - **Max daily loss** — type a positive number, like 50, and the bot **halts
>   for the day** once you're down fifty bucks. Use this. It's your circuit
>   breaker.
> - **Max total exposure** and **cash reserve** — how much of your bankroll it
>   can deploy and how much it always keeps in cash.
>
> [VISUAL: trading hours.]
>
> And there's a **trading hours** window if you only want it active certain
> hours. Optional.
>
> One note: everything here applies *live* — change a setting and it takes
> effect on the next scan. No save button needed."

---

## 6. THE 15-MINUTE CRYPTO ENGINE (9:00 – 11:30)

[VISUAL: Crypto page, the strategy settings grid.]

> "Now the fun one — the **15-minute crypto** engine. Polymarket runs these
> rolling 'will Bitcoin, Ethereum, etc. be up or down at the end of this 15
> minutes' markets, and this engine trades them. It's totally independent of the
> main bot — its own on switch, its own settings.
>
> [VISUAL: the two preset chips — Deep Favorite, Contrarian Fade.]
>
> Two quick presets to start from: **Deep Favorite** and **Contrarian Fade**.
> Let me explain the difference, because it's the core decision.
>
> [VISUAL: Direction dropdown.]
>
> **Direction mode:**
> - **Favorite-follow** buys the side that's *winning* — if Bitcoin is clearly
>   heading up, it buys 'up.' You're betting the trend holds for the last few
>   minutes.
> - **Contrarian fade** does the opposite — it buys the *cheap* losing side,
>   betting on a last-minute reversal.
>
> [VISUAL: Favorite threshold + Skip above.]
>
> **Favorite ≥** is your threshold — how strong the favorite has to be before
> you'll touch it, say 75%. **Skip above** is a ceiling — don't pay more than,
> say, 92 cents for an already-decided contract. Together those are your price
> band for favorites.
>
> Heads up — and this is a real thing — the bot now **double-checks the live
> price** right before it buys. If you set 'favorite 75' and the favorite
> *crashes* below that in the last few seconds, it skips instead of accidentally
> buying the now-cheap loser. So your threshold is honored at fill time, not just
> when the signal fired.
>
> [VISUAL: Entry window + Entry style.]
>
> **Entry window** — only act in the last N minutes of each 15-minute window.
> **Entry style** is maker versus taker:
> - **Rest at bid (maker)** posts a limit order at the bid — no fee, but it only
>   fills if the market comes to you.
> - **Cross to taker** crosses the spread and fills immediately, but pays the fee.
>
> And there's a nice middle ground: rest as a maker first, and **if it's still
> unfilled, cross to taker** automatically so you don't miss the trade.
>
> [VISUAL: Bet size by, Order size, Max concurrent.]
>
> **Sizing** here is the same idea — fixed contracts or percent of balance — with
> a **max concurrent** cap. Quick gotcha: Polymarket has a **5-contract minimum**
> on these markets, so if your balance is too small to fund 5 contracts, the bot
> tells you right in the 'per bet' preview and won't place a doomed order. If you
> see that, you need more balance — not a smaller size.
>
> [VISUAL: stop-loss field + daily loss.]
>
> There's a **stop-loss** if a position turns against you, and its own **daily
> loss** circuit breaker, separate from the main bot. Set both."

---

## 7. COPY TRADING (11:30 – 13:00)

[VISUAL: Copy Trading page.]

> "Third engine — **copy trading.** This mirrors other wallets. You paste in a
> Polymarket wallet address — maybe a trader you respect — and when they open a
> position, the bot opens the same one. When *all* the wallets you follow have
> closed it, you close too.
>
> [VISUAL: paste an address, show the wallet appearing with its position count.]
>
> Add as many wallets as you want; you'll see each one's open positions and
> value right here.
>
> [VISUAL: sizing toggle + the risk fields.]
>
> **Sizing** is your call, not theirs — **fixed dollars per copy** or **percent
> of your balance.** So if a whale drops ten grand, you're not forced to; you
> mirror the *trade*, sized to *your* bankroll. Then the guard rails: a **minimum
> trade size** so you skip their tiny noise trades, **max open copies**, a price
> cap, and its own **daily loss** limit.
>
> Copied positions show up on your Positions page tagged 'copy,' with live P&L,
> same as everything else. One tip — don't paste your *own* wallet; the bot
> ignores it anyway, but just don't."

---

## 8. STRATEGIES & PROFILES (13:00 – 13:45)

[VISUAL: Strategies page, then Profiles.]

> "If all those settings feel like a lot, the **Strategies** page has ready-made
> presets — Conservative, Balanced, High-Risk for the main bot. Click one, it
> loads the gates and sizing for you, and you tweak from there. Applying a preset
> never flips your live switches or wipes your followed wallets, so it's safe to
> experiment.
>
> And **Profiles** let you save your own full config and switch between them — a
> 'cautious' setup and an 'aggressive' setup, for example. Save, name, load."

---

## 9. WATCHING IT WORK (13:45 – 15:00)

[VISUAL: Positions, then Signals, then History.]

> "Three pages for monitoring:
>
> **Positions** — everything open and recent, with **live unrealized P&L** on the
> open ones so you can see how you're doing in real time, plus won/lost on the
> settled ones.
>
> **Signals** — the live feed of every whale and momentum signal as the scanners
> find them. If a signal says **'in position,'** that means the bot already holds
> that market — it's *not* a missed trade, it's the per-event cap doing its job.
>
> **History** — your track record. Per-session rollups and the full settled trade
> ledger, with win-rate, P&L, ROI. There's also a **Clear history** button if you
> want to reset your stats fresh — it keeps your settings and wallet, just wipes
> the numbers.
>
> [VISUAL: Logs page briefly.]
>
> And if you ever want to know *why* the bot did or didn't do something, the
> **Logs** page shows you exactly — every skip reason, every order, in plain
> English."

---

## 10. SAFETY & BEST PRACTICES (15:00 – 16:30)

[VISUAL: top bar Pause button; the 'no paper mode' warning in onboarding.]

> "Okay — the responsible part, watch this bit.
>
> **One: there's no paper mode.** Every engine trades real money when it's on.
> All three start **off** by default — you flip each one yourself.
>
> **Two: start small.** Fund it with a little, run one engine, watch it for a
> session, *then* scale. Don't dump your savings in on day one.
>
> **Three: use your daily-loss limits.** Set them on every engine. That's the
> number that stops a bad day from becoming a disaster.
>
> **Four: the Pause button** in the top bar is your kill switch for the main bot;
> each engine's enable toggle stops that engine.
>
> **Five — and this one trips people up:** if you change settings or update the
> app, sometimes you need to hit **Restart** on the engine for it to fully take
> effect. If something seems off, restart first.
>
> This is auto-trading. It can lose money. It makes no promises. You're
> responsible for your own trades and for whether Polymarket is legal where you
> are. Trade what you can afford to lose."

---

## 11. OUTRO / CTA (16:30 – 17:15)

[VISUAL: back to the dashboard with a position or two running; Krypt logo end
card with the link.]

> "That's the whole thing — three engines, your rules, your money, running on
> autopilot. It's completely free.
>
> If you're starting fresh, grab your Polymarket account with my link in the
> description. Deposit twenty and
> make a trade for **up to fifty in trading credits** — it really does help the
> channel.
>
> Drop a comment with your settings or your P&L, follow **@YuhgoSlavia** for
> updates, and I'll see you in the next one. Go make some money — carefully."

[END CARD: Link + @YuhgoSlavia + "Free Polymarket auto-trader"]

---

### Optional cut-down (if you want a tighter ~8-min version)
Keep sections **0, 2, 3, 5, 6, 10, 11**. Drop the deep dives on copy trading,
strategies, and the monitoring pages — mention them in one sentence ("there's
also copy-trading and full history tracking, I'll do a deep-dive on those
separately") and link a follow-up video.
