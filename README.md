<div align="center">

<img src="resources/krypt.png" alt="Krypt PolyBot US" width="108" />

# Krypt PolyBot US

**A free, open-source desktop app for trading on Polymarket US.**

A fork of Krypt PolyBot, ported to the Polymarket US exchange: your own AI agents under hard rails, a manual trading terminal, a short-term crypto engine and your own Python strategies. Your API key stays on your machine.

[![CI](https://github.com/scripflipped/krypt-polybot-us/actions/workflows/ci.yml/badge.svg)](https://github.com/scripflipped/krypt-polybot-us/actions/workflows/ci.yml)
&nbsp;[![License: MIT](https://img.shields.io/badge/License-MIT-6366F1)](LICENSE)
&nbsp;[![Windows 10 | 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)](#download)
&nbsp;[![Status: beta](https://img.shields.io/badge/status-beta-F59E0B)](#download)
&nbsp;[![Price: free](https://img.shields.io/badge/Price-free-EC4899)](#download)
&nbsp;[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/muzFKR657F)
&nbsp;[![Website](https://img.shields.io/badge/web-krypt.cc-A855F7?logo=googlechrome&logoColor=white)](https://krypt.cc)

</div>

---

[Polymarket US](https://polymarket.us) is a CFTC-regulated US exchange, separate from the original Polymarket. This fork trades there and only there. You sign in with an API key, not a wallet. The key and every trade stay in a local database on your machine. The app talks to Polymarket US, public crypto-price feeds and Kalshi (read-only), and sends nothing about you or your trading back to its authors (see the [Disclaimer](DISCLAIMER.md)).

> [!WARNING]
> **This app can place real orders on your Polymarket US account, and trading carries real financial risk.** The bundled crypto presets have **no proven edge**. They were carried over from the original app, where they measured net negative on recorded data, and they have not been measured on this exchange. This is **not financial advice**, and you are responsible for following Polymarket US's terms and rulebook. The app starts on **Paper** and every engine ships **off**. Paper covers your own orders and the AI agents, but the engines are **not simulated**: in Paper they send nothing, and once you go Live, a switched-on engine trades for real. Please read the full [**Disclaimer**](DISCLAIMER.md).

## What differs from the original

| | Original Krypt PolyBot | This fork |
|---|---|---|
| Exchange | Polymarket (Polygon, on-chain settlement) | Polymarket US (CFTC-regulated, account-based) |
| Sign-in | Wallet private key | API key: Key ID + Secret Key |
| Market ids | `0x` condition ids | Slugs |
| Short-term crypto | 5m, 15m, 1h and 4h | 5m, 15m and 1h for BTC, ETH, SOL and XRP. They settle on the CF Benchmarks index (BRTI for BTC) as the 60-second average before the window boundary |
| Fees | Original venue's schedule | Taker fee of 0.0695 x contracts x p x (1-p) on most markets, set per market. Base-tier makers pay nothing |

**Not in this fork:**

- **Perps.** There is no such product on this exchange.
- **Copy trading and the whale tracker.** This exchange publishes no per-trader data.
- **Main engine and Signals.** Their inputs read a public trade tape this exchange doesn't have.
- **Paired both-sides mode on the crypto engine.** Here it would be a wash trade under the exchange's rulebook.
- **Volume farmer, deposit wallets, allowances, redemption, neg-risk groups and region banners.** None of them apply. Eligibility is decided by your account.

## What it does

- **AI agents** — connect Claude Desktop, Claude Code, Cursor, Codex, Windsurf or VS Code over MCP, with no token in any config file. Build named agents, each with your own guide, rules and caps. A buy needs the agent's own forecast to beat the price after fees; live orders wait for your approval by default; an agent can only sell what it opened. A scoreboard checks whether each agent beats the market.
- **Autopilot** — the app runs one of your agents on a schedule with your own AI key, under the same rails, with daily run and token budgets.
- **Paper | Live** — one switch for the whole app. Paper trades the real order books with imaginary money and needs no API key; the engines send nothing until you go Live. Without a key the books come from public quotes, which the exchange caches for up to 30 seconds.
- **Trading terminal** — search or browse markets, then work one from a full market page: probability chart, order book, your position and an order ticket. A number nobody could produce renders as `—`, never as a `0` you might trade on.
- **Resolution risk** — who resolves a market, on what source, how ambiguous the wording is, and whether a related market contradicts it.
- **Cross-venue prices** — the same question priced on Kalshi next to Polymarket US. Read-only, and a weak match shows nothing.
- **Standing rules** — stop loss, take profit and price alerts in probability points, all on one page.
- **Short-term crypto** — the Up/Down markets listed above, with a configurable strategy and its own live switch.
- **Strategy scripts** — write your own Python strategies, backtest them on past markets you download or the app records, and run them in shadow mode before real money. Hard money rails cap what any script can spend.
- **AI analysis** — have a model read a market's resolution rules and point out the traps: Anthropic, OpenAI, OpenRouter, Gemini, or a local model through Ollama or LM Studio. It gives a reading, never a made-up probability.
- **Remote control** — check on the bot, or trade, from your phone over a Discord or Telegram bot you own. Paired to your account only; trading from chat is off by default and every order needs a confirmation code.
- **Local-first** — your key and trade history live under `%APPDATA%/Krypt PolyBot US/`. **No telemetry.** **Settings → Data & privacy** lists every outbound host with live call counts and one switch to turn them off.

The app's own **Guide** page covers each feature. `docs/` still describes the original app and has not been ported.

## Safe by default

- **The app starts on Paper** — nothing real is sent until you go Live from a checklist that tests your API key and lists everything that will start spending.
- **Every engine starts off** — crypto and live scripts each have their own switch, and nothing trades until you turn one on.
- **Breakers on the engines** — daily stop-loss / take-profit, a lifetime drawdown breaker, trading-hours windows and a pause-everything switch (also from your phone with `/pause`).
- **Order checks in the backend** — a kill switch, an order throttle, a price collar and a self-cross refusal.- **Your API key is encrypted at rest** on Windows (DPAPI), tied to your user account, and only ever used to sign requests locally.
- **New scripts start in shadow mode** — they record what they *would* have traded until you explicitly arm them.
- **A fee-aware backtest** (`npm run py:backtest`) measures the net-of-fee result on *your own* recorded data. Run it before trusting any setting.
- **AI agents are fenced in by the backend, not by their prompt** — trade mode, caps, a daily loss stop, forecast-before-buy and approvals are enforced on every order, whatever the agent says. Agents start on paper, and whole-account powers are a separate switch, off by default.
- **Remote trading is a separate switch**, off by default and separate from reading.
- **Missing data is shown as missing.** A position with no known cost basis reports unknown P&L and is left out of your totals rather than counted at a guess.

## Download

<a href="../../releases"><img src="https://img.shields.io/badge/Download%20for%20Windows-Krypt%20PolyBot%20US-A855F7?style=for-the-badge&logo=windows&logoColor=white" alt="Download Krypt PolyBot US" /></a>

Grab the latest installer from the [**Releases**](../../releases) page and run it. It starts on Paper, so you can connect an agent and try everything with no account. To trade for real, get an API key (below), open **API Key**, paste your Key ID and Secret Key, and go to **Settings → Account → Go live**. Every engine starts off, so nothing trades until you turn one on. Releases also carry **unsigned Linux (AppImage/deb) and macOS (dmg, Intel + Apple Silicon)** builds, which get less testing than the Windows one.

> Builds aren't code-signed yet, so the first launch may show a SmartScreen prompt — click **More info → Run anyway**. The full source is right here if you'd rather build it yourself.

### Opening it on macOS

The mac builds are not notarized (Apple charges $99/yr for the certificate that would make this unnecessary), so the first launch needs one manual approval:

1. Open the `.dmg` and drag **Krypt PolyBot US** to Applications.
2. Launch it once. macOS refuses — pick **Done**.
3. **System Settings → Privacy & Security**, scroll to Security: *"Krypt PolyBot US was blocked"* → **Open Anyway**, then confirm.

## Getting an API key

The app walks you through this during onboarding, and again on its **API Key** page.

1. Sign up at <https://polymarket.us/join/yuhgo>. This is Krypt's invite link, so Krypt benefits when you use it. The page offers $25 to you and $25 to Krypt, on Polymarket US's terms. A code can only be applied during sign-up. Any Polymarket US account works with this app.
2. Verify your identity in the Polymarket US phone app. It asks for your date of birth, address and SSN.
3. Create a key at <https://polymarket.us/developer>. You get a Key ID and a Secret Key, and the Secret Key is shown only once.
4. Paste it into the app. Once it connects, the app opens the [Krypt squad](https://polymarket.us/squad/join/0xOHjNIfwLBVzmovbjml?referrer=yuhgo) page in your browser, once, so you can join. That link credits Krypt as your referrer.

Polymarket US accounts are for people who pass its identity check. Trading rules still apply to orders placed by software.

## Build from source

```bash
git clone https://github.com/scripflipped/krypt-polybot-us.git
cd krypt-polybot-us
npm install
npm run dev      # auto-creates python/.venv on first run (~30s)
npm run dist     # build the installer for your OS into /release
```

Requires [Node.js](https://nodejs.org) 22+ and [Python](https://python.org) 3.11+ on PATH. Development runs on macOS/Linux/Windows. `npm run dist` builds the installer for the OS you're on (the build aborts if the bundled backend fails its self-test); CI builds Windows, Linux (AppImage/deb) and macOS (dmg, both architectures) installers and attaches them to a GitHub Release.

## Links

- **Website** — [krypt.cc](https://krypt.cc) · more free tools at [krypt.cc/tools](https://krypt.cc/tools)
- **Support and community** — [Discord](https://discord.gg/muzFKR657F)
- **Security** — [SECURITY.md](SECURITY.md) · **Contributing** — [CONTRIBUTING.md](CONTRIBUTING.md)

## License

Released under the [MIT License](LICENSE) — free to use, fork and share. Please don't rebrand and resell it. Provided **as-is, with no warranty**; see the [Disclaimer](DISCLAIMER.md).

<div align="center"><sub>Built by the Krypt team · <a href="https://krypt.cc">krypt.cc</a></sub></div>
