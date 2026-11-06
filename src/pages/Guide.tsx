import {
  Activity, AlertTriangle, BarChart3, Bitcoin, BookOpen, Briefcase, CheckCircle2,
  Code2, Compass, ExternalLink, Eye, FlaskConical, KeyRound, Layers,
  Receipt, Scale, Shield, ShieldAlert, ShieldQuestion,
  Smartphone, Target, Twitter,
} from 'lucide-react';
import { Card, Page, Section } from '../components/common';
import { GuideVideoLibrary } from '../components/GuideVideo';
import { POLYMARKET_DEVELOPER_URL, POLYMARKET_SIGNUP_URL, POLYMARKET_SQUAD_URL } from '../utils/links';
import { followYuhgo, X_PROFILE } from '../utils/share';

export function GuidePage() {
  const open = (url: string) => () => void window.krypt.app.openExternal(url);

  return (
    <Page
      title="Guide"
      subtitle="A short video for everything in the app. Start with the first one."
    >
      <div className="mb-6">
        <GuideVideoLibrary />
      </div>

      <Card className="mb-6 border-krypt-purple/30 bg-gradient-to-br from-krypt-purple/15 via-krypt-glow/20 to-transparent">
        <div className="flex flex-col items-start gap-4 md:flex-row md:items-center">
          <div className="grid h-14 w-14 shrink-0 place-items-center rounded-2xl bg-krypt-glow shadow-krypt-strong">
            <KeyRound className="h-6 w-6 text-white" />
          </div>
          <div className="flex-1">
            <div className="text-sm font-semibold text-white">
              New to Polymarket US?
            </div>
            <p className="mt-1 text-xs text-krypt-muted">
              Paper trading needs no account. To trade Live, the API Key page takes you through it
              one step at a time: sign up, verify your identity, add funds and create your key, and
              the Krypt squad page opens once you connect. The sign-up button uses Krypt&apos;s invite link, so Krypt benefits when you use it.
            </p>
          </div>
          <div className="flex flex-col gap-2 shrink-0">
            <button onClick={open(POLYMARKET_SIGNUP_URL)} className="krypt-btn-primary">
              Sign up <ExternalLink className="h-3 w-3" />
            </button>
            <button onClick={open(POLYMARKET_SQUAD_URL)} className="krypt-btn-default">
              Join the Krypt squad <ExternalLink className="h-3 w-3" />
            </button>
            <button
              onClick={() => void followYuhgo()}
              className="krypt-btn-default"
              title={`Open ${X_PROFILE} on X`}
            >
              <Twitter className="h-4 w-4" /> Follow {X_PROFILE} <ExternalLink className="h-3 w-3" />
            </button>
          </div>
        </div>
      </Card>

      <details data-testid="guide-written" className="group/written">
        <summary className="mb-4 cursor-pointer select-none rounded-xl border border-krypt-border px-4 py-3 text-sm font-medium text-white hover:bg-white/[0.04]">
          Prefer to read? Open the written guide
        </summary>
      <Section title="At a glance">
        <div className="grid gap-4 md:grid-cols-3">
          <FeatureCard
            icon={Compass}
            title="A terminal, first"
            body="Discover, Market and Track let you look up any Polymarket US market, read its book and its resolution risk, and trade it by hand. The engines are optional; the terminal is the part you use every day."
          />
          <FeatureCard
            icon={Bitcoin}
            title="A crypto engine"
            body="Watches the 5-minute, 15-minute and 1-hour Up/Down markets on BTC, ETH, SOL and XRP. Its own page, its own switch, off by default."
          />
          <FeatureCard
            icon={Layers}
            title="Risk gates everywhere"
            body="Per-window dedupe, max open bets, daily loss stop, lifetime loss limit, and a minimum cash reserve. Agents and scripts have their own caps on top."
          />
        </div>
      </Section>

      <Section title="The terminal — Discover, Market, Track">
        <Card>
          <p className="mb-4 text-sm text-krypt-muted">
            A bot answers one question: should I act, right now, on this one
            thing I am watching. A terminal answers a different one:{' '}
            <span className="text-white">what is going on, and what should I do
            about it</span> — for any market you name, not just the ones an
            engine happened to see.
          </p>
          <ol className="space-y-4 text-sm">
            <Step n={1} title="Discover — what is happening" body={
              <>Four live columns (New, Closing soon, High volume, Resolving),
                filters, search, and a watchlist. Paste a market slug to jump
                straight to any market, even a settled one.
              </>
            } />
            <Step n={2} title="Market — is this one worth trading" body={
              <>Probability chart on a bounded 0&ndash;100 axis, the full order
                book, and an order ticket. Hover any
                number to see <span className="text-white">which provider it
                came from and how old it is</span>.
              </>
            } />
            <Step n={3} title="Track — what you actually did" body={
              <>Positions, resting orders, standing instructions, your fill
                ledger and a log of every manual order including the failures.
                Cost basis is <span className="text-white">average cost, not
                FIFO</span>, and it comes from Polymarket US&rsquo;s own fill
                history rather than from what we asked for.
              </>
            } />
          </ol>

          <div className="mt-5 border-t border-krypt-border pt-4">
            <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">
              The one rule worth knowing
            </div>
            <p className="text-xs leading-relaxed text-krypt-muted">
              A number nobody could produce shows as{' '}
              <span className="font-mono text-white">&mdash;</span>, never as
              zero, and the row tells you why. A market with no 24h volume reads
              &ldquo;unknown&rdquo;, not &ldquo;$0.00&rdquo;. A position whose
              contracts did not come through this app has{' '}
              <span className="text-white">no cost basis</span>, so its P&amp;L
              reads unknown rather than break-even &mdash; and it is left out of
              your totals rather than counted at a guess. The chart never draws
              a line across hours nobody traded.
            </p>
          </div>
        </Card>

        <div className="mt-4 grid gap-4 md:grid-cols-3">
          <FeatureCard
            icon={ShieldQuestion}
            title="Resolution risk"
            body="In crypto the danger is a rug. Here it is resolution: who decides, on what source, how ambiguous the wording is, whether a sibling market contradicts it. Eleven checks, scored only over the ones that actually resolved — and below four it prints no score at all."
          />
          <FeatureCard
            icon={Scale}
            title="The same question on Kalshi"
            body="Polymarket US and Kalshi list overlapping questions. Toggle Kalshi on the chart to see both lines, and the panel shows both books with the difference. Reference data only — this app reads Kalshi and never trades there."
          />
          <FeatureCard
            icon={Receipt}
            title="Standing instructions"
            body="Stop loss, take profit and price alerts you write down and forget. Triggers are probability points, not percentages. Every pending one is listed and cancellable on Track, and each passes the same checks a manual order does."
          />
        </div>
      </Section>

      <Section title="Setup">
        <Card>
          <ol className="space-y-4 text-sm">
            <Step
              n={1}
              title="Make a Polymarket US account"
              body={
                <>
                  Sign up and verify your identity in the{' '}
                  <button onClick={open(POLYMARKET_SIGNUP_URL)} className="text-krypt-purple hover:underline">Polymarket US</button>{' '}
                  app. It asks for your date of birth, address and Social Security number.
                  Fund the account there.
                </>
              }
            />
            <Step
              n={2}
              title="Create an API key"
              body={
                <>
                  On the{' '}
                  <button onClick={open(POLYMARKET_DEVELOPER_URL)} className="text-krypt-purple hover:underline">developer page</button>.
                  You get a Key ID and a Secret Key. Polymarket US shows the Secret Key only once.
                </>
              }
            />
            <Step
              n={3}
              title="Paste it on the API Key page"
              body={
                <>
                  Click <KeyRound className="inline h-3.5 w-3.5" /> API Key in the sidebar, paste
                  both, hit <span className="text-white">Save &amp; connect</span>. The app signs
                  each request locally and starts reading your balance.
                </>
              }
            />
            <Step
              n={4}
              title="Tune the settings"
              body={
                <>
                  <Bitcoin className="inline h-3.5 w-3.5" /> Crypto lets you set the entry window,
                  favorite threshold, sizing and loss stops. Its presets show how the knobs combine;
                  none of them has been measured on this exchange. Save your own version in{' '}
                  <Briefcase className="inline h-3.5 w-3.5" /> Profiles.
                </>
              }
            />
            <Step
              n={5}
              title="Start small"
              body={
                <>
                  In Paper the engine sends nothing &mdash; it is not simulated. Once the app is
                  Live, switching on the executor on the{' '}
                  <span className="text-white">Crypto</span> page sends real orders. Begin with a
                  small balance and low caps, and watch a few windows. Every engine has its own
                  switch on its own page &mdash; none of them starts another.
                </>
              }
            />
          </ol>
        </Card>
      </Section>

      <Section title="Test safely before risking real money">
        <Card className="border-krypt-win/25 bg-krypt-win/[0.03]">
          <div className="mb-4 flex items-start gap-3">
            <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-krypt-win/15 text-krypt-win">
              <Shield className="h-5 w-5" />
            </div>
            <p className="text-sm text-krypt-muted">
              Polymarket US trades <span className="text-white">real dollars</span> &mdash; there&apos;s
              no play-money exchange. So test without risking funds in one of these ways:
            </p>
          </div>
          <ol className="space-y-4 text-sm">
            <Step n={1} title="Paper trading (real markets, no money, no API key)" body={
              <>The app starts on <span className="text-white">Paper</span>{' '}
                (Settings &rarr; Account): the order ticket, your phone, standing
                stops and AI agents simulate instead of trading. Fills come out of the{' '}
                <span className="text-white">real order book</span> at the price
                you would actually have walked &mdash; ask for 400 contracts at
                99&cent; against a book whose best offer is 19&cent; and you get
                19&cent;, not 99&cent;. Real fees, real tick sizes, and exact
                settlement at 100&cent; or 0&cent;. It needs no API key at all, and
                its figures live on{' '}
                <span className="text-white">Track &rarr; Paper</span>, never
                mixed into your real P&amp;L.
              </>
            } />
            <Step n={2} title="A small balance" body={
              <>For a real-money smoke test, keep only an amount you can afford to lose in the
                account, set a low bet size and daily loss stop, go Live in Settings &rarr; Account,
                then switch on the executor on the{' '}
                <span className="text-white">Crypto</span> page. Judge results on
                the <span className="text-white">History</span> page before scaling up.
              </>
            } />
          </ol>
          <div className="mt-3 text-[11px] text-krypt-dim">
            Paper mode is deliberately pessimistic where it cannot be exact: a
            resting order fills only once the market trades <em>through</em>
            your price, because nobody can know your place in the queue. It also
            does not move the real book, so a large order would fill worse in
            reality &mdash; the ticket warns you when one eats a meaningful
            share of the visible depth. The strategies here are
            heuristics with no proven edge.
          </div>
        </Card>
      </Section>

      <Section title="Fees">
        <Card>
          <p className="text-xs leading-relaxed text-krypt-muted">
            On most markets the taker fee is{' '}
            <span className="font-mono text-white">0.0695 &times; contracts &times; p &times; (1 &minus; p)</span>,
            with a coefficient set per market. Makers at the base tier pay nothing. Taker rebates
            start at $250k of 30-day volume, maker rebates at $10M. Polymarket US sets all of these
            and can change them.
          </p>
        </Card>
      </Section>

      <Section title="How a trade flows">
        <Card>
          <FlowDiagram />
          <div className="mt-4 grid gap-3 text-xs text-krypt-muted md:grid-cols-3">
            <div>
              <div className="text-[11px] uppercase tracking-wider text-white">
                1. Watch
              </div>
              The crypto engine reads each open Up/Down window and the underlying&apos;s spot price
              every tick, and records them to the local DB. The window&apos;s strike is published by
              the market itself.
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider text-white">
                2. Filter &amp; size
              </div>
              The engine checks your entry window, favorite threshold, model and book gates,
              one bet per window, open-bet caps and daily loss stop. If an asset passes, your
              bet-size setting turns it into a number of contracts.
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wider text-white">
                3. Place &amp; track
              </div>
              The order goes in as a taker or a resting maker, depending on your entry style. Each
              window settles on the CF Benchmarks index (BRTI for BTC) as the 60-second average
              before the boundary; the app then reads the exchange&apos;s result and writes the final
              P&amp;L.
            </div>
          </div>
        </Card>
      </Section>

      <Section title="Setting cheat sheet">
        <Card>
          <div className="grid gap-x-8 gap-y-3 md:grid-cols-2">
            <SettingRow
              label="Entry window"
              hint="How long into the window (in minutes) an entry may happen. Later means less time for the price to turn, and a dearer favorite."
            />
            <SettingRow
              label="Favorite ≥ / Skip above"
              hint="The price band to buy in. Near 100¢ one loss costs many wins, and the fee is smallest there too."
            />
            <SettingRow
              label="Min move Δ"
              hint="How far spot must have moved off the strike before an entry counts."
            />
            <SettingRow
              label="Stop-loss"
              hint="Sell if the position's price falls to this. These markets can have an empty bid late in a window, so a stop may not fill."
            />
            <SettingRow
              label="Bet size by"
              hint="A fixed number of contracts, or dollars per bet. Below the minimum the entry is skipped and the reason shown."
            />
            <SettingRow
              label="Max open bets"
              hint="Hard ceiling on bets open at once."
            />
            <SettingRow
              label="Daily loss stop"
              hint="If today's losses reach this, no new entries until tomorrow."
            />
            <SettingRow
              label="Entry style"
              hint="Maker rests at the bid: no fee at the base tier, but it may not fill. Taker crosses the ask and pays the fee."
            />
          </div>
        </Card>
      </Section>

      <Section title="Reading the dashboard">
        <Card>
          <ul className="space-y-2 text-sm text-krypt-muted">
            <Row
              icon={BarChart3}
              label="Total Balance"
              body="Cash + portfolio (live mark-to-market). Updated from Polymarket US every snapshot."
            />
            <Row
              icon={Target}
              label="ROI"
              body="(Total - bankroll baseline) / baseline. The baseline is auto-detected from your first snapshot."
            />
            <Row
              icon={CheckCircle2}
              label="Today P&L"
              body="Your total balance now minus your total at the first snapshot of the UTC day (rolls over at 00:00 UTC), with detected deposits and withdrawals taken out. Open positions count at their live marks, so it moves before anything settles. The daily loss stop reads this same number."
            />
            <Row
              icon={Briefcase}
              label="Open Positions"
              body="Count of filled / partial bets that haven't settled yet."
            />
            <Row
              icon={Activity}
              label="Recent resolutions"
              body="The latest settled bets with their P&L. Pair this with the Positions page's Won / Lost tabs to see the whole story."
            />
          </ul>
        </Card>
      </Section>

      <Section title="When P&amp;L looks weird">
        <Card>
          <div className="mb-3 flex items-start gap-3 rounded-lg border border-krypt-warn/30 bg-krypt-warn/5 p-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-krypt-warn" />
            <div className="text-xs text-krypt-muted">
              <span className="text-white">Today P&L moves with your open positions.</span>
              {' '}It is a change in your balance, not a sum of <span className="text-white">settled</span> bets,
              so a position marked down shows as a loss before it settles. A deposit or
              withdrawal is taken out only when it is bigger than trading could explain
              (roughly the cost of your open positions); a smaller one reads as a gain or
              loss for the day, and the daily stop sees it too.
            </div>
          </div>
          <div className="text-sm text-krypt-muted">
            If something looks broken (wrong wins/losses, stuck positions, etc.):
          </div>
          <ul className="mt-2 space-y-1.5 text-sm text-krypt-muted">
            <li>
              <span className="text-white">Positions → Reconcile</span> — re-syncs
              local rows with Polymarket US's live position list.
            </li>
            <li>
              <span className="text-white">Positions → Resolve Now</span> — forces
              one resolution pass for any settled markets.
            </li>
            <li>
              <span className="text-white">Positions → Recompute P&amp;L</span> —
              wipes locally-stored P&L for resolved trades and rebuilds from each
              market's settlement value. Use after upgrading or if the dashboard
              shows obviously wrong wins/losses.
            </li>
            <li>
              <span className="text-white">Restart</span> button (top bar) — bounces
              the Python backend if it's stuck.
            </li>
          </ul>
        </Card>
      </Section>

      <Section title="Writing your own strategy (Scripts)">
        <Card>
          <p className="text-sm text-krypt-muted">
            The <span className="text-white">Scripts</span> page runs your own Python strategy
            alongside the built-in engines. You write one or more hooks, backtest them against the
            past markets you downloaded or this app recorded, then arm the script to trade under caps you set.
            Scripts are <span className="text-white">fully independent</span> — none of the other
            engines has to be switched on, and each script picks its own coins.
          </p>

          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <FeatureCard
              icon={Code2}
              title="decide(ctx)"
              body="Called once per coin per tick while a crypto Up/Down window is open. Return None to pass, or an intent like {'side': 'up', 'price': 'ask'} to buy a side."
            />
            <FeatureCard
              icon={BarChart3}
              title="decide_market(market)"
              body="Called each tick for the highest-volume general Polymarket US markets — politics, sports, news. You get the title, category, both sides of the book, spread and volume, and return an intent sized in contracts or dollars."
            />
            <FeatureCard
              icon={Activity}
              title="manage(position, ctx)"
              body="Called every tick for each open position. Return 'sell' to flatten at the bid now, or retune take-profit / stop-loss — this is how you build trailing stops."
            />
            <FeatureCard
              icon={Layers}
              title="supervise(app)"
              body="Called once per tick to change whitelisted config — switch strategy posture, or turn an engine off when conditions sour. Scripts may disable engines, never enable them."
            />
          </div>

          <div className="mt-4 text-xs text-krypt-muted">
            <span className="text-white">ctx</span> carries around 40 fields per tick: real order-book
            asks and bids, the underlying's spot price and realized volatility, MACD / RSI / VWAP /
            EMA / SMA, the model's probability and fee-net edge, minutes left in the window, and your
            own portfolio (balance, open positions, today's P&amp;L). Keep memory between ticks in the
            persistent <span className="text-white">state</span> dict, and use{' '}
            <span className="text-white">on_start</span> /{' '}
            <span className="text-white">on_fill</span> /{' '}
            <span className="text-white">on_settle</span> for lifecycle events. The page's{' '}
            <span className="text-white">AI context pack</span> button copies a single prompt
            containing the whole field vocabulary, the return contract, and your actual rails — paste
            it into any assistant to have it write the script for you.
          </div>
        </Card>

        <Card className="mt-4">
          <ul className="space-y-2">
            <Row
              icon={Shield}
              label="Hard rails"
              body="Max entry price, max contracts per order, max open positions per script, a per-script daily-loss breaker that auto-disables on a bad day, and one entry attempt per market window. These apply to every script — a script can never place an order larger than your cap."
            />
            <Row
              icon={Eye}
              label="Shadow first"
              body="Every new script starts in shadow mode: it runs on live ticks and records the orders it would have placed, settled from the real outcome with fees charged, without sending anything to the exchange. Arming it for real orders is a separate, confirmed step. Shadow simulates entries only — exits are held to settlement."
            />
            <Row
              icon={FlaskConical}
              label="Backtest first"
              body="Backtest over past markets before arming. The report ships its own caveats: resting limits aren't modeled as maker fills, exit fills are optimistic (these markets often have no bid at all), and the rails aren't simulated — so live is a subset of what you see."
            />
            <Row
              icon={AlertTriangle}
              label="Errors fail closed"
              body="Any exception, malformed return, or hook that overruns its time limit disables that script and shows you why. A broken script never takes the rest of the app down with it."
            />
            <Row
              icon={Activity}
              label="It tells you when it isn't running"
              body="Each script shows its real state — running, blocked, starting, error — with the reason. 'Enabled' alone was never the answer: a script can be enabled and doing nothing because no API key is saved, or because it is armed while the master switch is off. The status line names the gate."
            />
          </ul>
        </Card>

        <Card className="mt-4 border-krypt-loss/30 bg-krypt-loss/5">
          <div className="flex items-start gap-3">
            <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-krypt-loss" />
            <div className="text-xs leading-relaxed text-krypt-muted">
              <div className="text-sm font-medium text-white">Read any script you did not write</div>
              <p className="mt-1">
                Scripts run as <span className="text-white">full Python</span>, with the same
                privileges as the trading engine, in the process that holds your decrypted API
                key. There is no language sandbox — the old one blocked imports and most of the
                standard library, which fought every real strategy while never being a genuine
                security boundary.
              </p>
              <p className="mt-2">
                Instead the app <span className="text-white">audits</span> every script and shows
                you what it found: network access, filesystem writes, subprocesses, environment or
                credential-shaped names, and dynamic execution. Obfuscation is itself reported as
                critical, because a static scan cannot see through it. The findings are on the
                script's <span className="text-white">Risk</span> tab, and a flagged script says so
                again when you go to arm it.
              </p>
              <p className="mt-2">
                Treat that as a smoke detector, <span className="text-white">not a lock</span>. Read
                anything an AI generated or another person sent you before enabling it. The money
                rails above still apply to every script and cannot be raised from inside one.
              </p>
            </div>
          </div>
        </Card>
      </Section>

      <Section title="Remote control from your phone">
        <Card>
          <div className="mb-4 flex items-start gap-3">
            <div className="grid h-10 w-10 shrink-0 place-items-center rounded-xl bg-krypt-purple/15 text-krypt-purple">
              <Smartphone className="h-5 w-5" />
            </div>
            <p className="text-sm text-krypt-muted">
              Leave the app running on your PC and check on it &mdash; or react
              to it &mdash; from a Discord or Telegram bot you create yourself.
              Both ship <span className="text-white">off</span>, and answer
              nobody until you pair an account.
            </p>
          </div>
          <ol className="space-y-4 text-sm">
            <Step n={1} title="Make the bot" body={
              <>Telegram: message <span className="font-mono text-white">@BotFather</span>,
                send <span className="font-mono text-white">/newbot</span>, copy the
                token. Discord: create an application at discord.com/developers, add
                a Bot, copy its token, and{' '}
                <span className="text-white">turn on the MESSAGE CONTENT intent</span>{' '}
                &mdash; without it the bot receives empty messages and every command
                silently does nothing.
              </>
            } />
            <Step n={2} title="Paste the token" body={
              <>On the <span className="text-white">Remote</span> page. It is
                checked against the platform before it is stored, so a typo fails
                there rather than as a bot that never connects. Stored encrypted,
                the same way your API key is &mdash; never in settings.json.
              </>
            } />
            <Step n={3} title="Pair yourself" body={
              <>Generate a six-digit code and send{' '}
                <span className="font-mono text-white">/pair 123456</span> to your
                bot. Discord also accepts your user id pasted directly. This is the
                part that matters: <span className="text-white">only paired accounts
                are answered</span>, and everyone else is refused before the command
                is even read.
              </>
            } />
          </ol>

          <div className="mt-5 border-t border-krypt-border pt-4">
            <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">
              What you can send
            </div>
            <p className="text-xs leading-relaxed text-krypt-muted">
              <span className="font-mono text-white">/status /positions /pnl
              /balance /orders /price</span> to read, and{' '}
              <span className="font-mono text-white">/pause /resume /flatten
              /cancelall</span> to de-risk. A standing instruction that fires
              &mdash; or one that was <span className="text-white">blocked</span>{' '}
              from firing &mdash; is pushed to you unprompted. Nothing else is,
              because a bot that pings on every tick gets muted and is then worth
              nothing when it matters.
            </p>
          </div>

          <div className="mt-4 rounded-xl border border-krypt-warn/30 bg-krypt-warn/5 p-4">
            <div className="flex items-center gap-2 text-krypt-warn">
              <ShieldAlert className="h-4 w-4" />
              <div className="text-sm font-semibold">Two things to be clear about</div>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-krypt-muted">
              <span className="text-white">Your positions and P&amp;L travel to
              Discord&rsquo;s or Telegram&rsquo;s servers.</span> That is what a
              chat bot is. Everything else in this app stays on your machine, and
              both hosts appear in Settings &rarr; Data &amp; privacy with live
              call counts so you can check that.
            </p>
            <p className="mt-2 text-xs leading-relaxed text-krypt-muted">
              <span className="text-white">Placing orders from chat is a separate
              switch</span>, off by default, and it never turns on with the one
              above. Even on, every order is previewed, needs a fresh{' '}
              <span className="font-mono text-white">/confirm</span> within 90
              seconds, obeys a per-order dollar cap, and in Paper trades the
              paper book like every other order. The realistic risk here is not a
              hacker &mdash; it is an unlocked phone.
            </p>
          </div>
        </Card>
      </Section>

      <Section title="Tips">
        <Card>
          <ul className="space-y-2 text-sm text-krypt-muted">
            <li>
              <BookOpen className="mr-2 inline h-3.5 w-3.5" />
              Run on a small balance for at least a few hundred settled windows before trusting a
              setting. The Profiles page lets you compare settings.
            </li>
            <li>
              <BookOpen className="mr-2 inline h-3.5 w-3.5" />
              The History page's Daily P&L chart says more about your settings than any single
              trade does.
            </li>
          </ul>
        </Card>
      </Section>
      </details>
    </Page>
  );
}

// ───────── tiny helpers ────────────────────────────────────────────────────

function FeatureCard({
  icon: Icon, title, body,
}: { icon: React.ComponentType<{ className?: string }>; title: string; body: string }) {
  return (
    <Card>
      <div className="flex items-center gap-2">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-krypt-purple/15 text-krypt-purple">
          <Icon className="h-4 w-4" />
        </div>
        <div className="text-sm font-semibold text-white">{title}</div>
      </div>
      <p className="mt-3 text-xs leading-relaxed text-krypt-muted">{body}</p>
    </Card>
  );
}

function Step({
  n, title, body,
}: { n: number; title: string; body: React.ReactNode }) {
  return (
    <li className="flex items-start gap-3">
      <div className="grid h-7 w-7 shrink-0 place-items-center rounded-full border border-krypt-purple/40 bg-krypt-purple/10 text-xs font-semibold text-krypt-purple">
        {n}
      </div>
      <div>
        <div className="text-sm font-medium text-white">{title}</div>
        <p className="mt-0.5 text-xs leading-relaxed text-krypt-muted">{body}</p>
      </div>
    </li>
  );
}

function SettingRow({ label, hint }: { label: string; hint: string }) {
  return (
    <div>
      <div className="font-mono text-[11px] text-krypt-purple">{label}</div>
      <div className="mt-0.5 text-xs leading-relaxed text-krypt-muted">{hint}</div>
    </div>
  );
}

function Row({
  icon: Icon, label, body,
}: { icon: React.ComponentType<{ className?: string }>; label: string; body: string }) {
  return (
    <li className="flex items-start gap-3">
      <Icon className="mt-0.5 h-4 w-4 shrink-0 text-krypt-purple" />
      <div className="text-xs">
        <span className="text-white">{label}</span>
        <span className="ml-2 text-krypt-muted">{body}</span>
      </div>
    </li>
  );
}

function FlowDiagram() {
  return (
    <svg viewBox="0 0 720 130" className="w-full text-krypt-muted">
      {[
        { x: 20, label: 'Market data', sub: 'book · spot' },
        { x: 175, label: 'Engine', sub: 'window · model' },
        { x: 330, label: 'Trader', sub: 'filter · size · place' },
        { x: 485, label: 'Order book', sub: 'taker · maker' },
        { x: 620, label: 'Resolver', sub: 'settlement P&L' },
      ].map((n, i) => (
        <g key={i}>
          <rect
            x={n.x} y={35} width={90} height={60} rx={10}
            className="fill-krypt-surface2 stroke-krypt-border" strokeWidth={1}
          />
          <text x={n.x + 45} y={62} textAnchor="middle" className="fill-white text-[11px] font-semibold">
            {n.label}
          </text>
          <text x={n.x + 45} y={78} textAnchor="middle" className="fill-krypt-muted text-[10px]">
            {n.sub}
          </text>
        </g>
      ))}
      {[110, 265, 420, 575].map((x, i) => (
        <g key={i}>
          <line x1={x} y1={65} x2={x + 65} y2={65} stroke="currentColor" strokeWidth={1.5} markerEnd="url(#arrow)" />
        </g>
      ))}
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L10,5 L0,10 z" fill="currentColor" />
        </marker>
      </defs>
    </svg>
  );
}
