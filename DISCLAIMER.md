# Disclaimer & Risk Notice

**Read this before using Krypt PolyBot US with real money.**

Krypt PolyBot US is free, open-source, experimental software for placing trades on
[Polymarket US](https://polymarket.us). By downloading, building, or running it, you
acknowledge and accept everything below.

## Not financial advice
Krypt PolyBot US, its strategies, scores, and any documentation are for
informational and educational purposes only. Nothing here is financial,
investment, legal, or tax advice. The authors are **not** registered investment
advisors, commodity trading advisors, futures commission merchants, broker-dealers,
or fiduciaries of any kind, and nothing in this project creates such a relationship.

## Risk of loss
Trading event contracts involves substantial risk. **You can lose some or all
of the money in your account.** Automated trading can lose money quickly and at
scale, including while you are away from your computer. Only trade with money
you can afford to lose entirely.

## The strategies are unproven
The bundled strategies (the short-term crypto presets and example scripts) are
**heuristics**. They:

- were carried over from an app built for a different exchange, where the presets
  measured net negative on recorded data;
- have **not** been measured on Polymarket US;
- carry **no guarantee of profitability**.

Any performance figures, "edge" scores, or calibration claims are illustrative
and are **not** a promise of future results. Past or simulated performance does
not indicate future performance. Fee estimates in the app can differ from what the
exchange charges, because fees are set per market and can change.

## No warranty
The software is provided "AS IS", without warranty of any kind, as stated in the
[LICENSE](./LICENSE). It may contain bugs that cause incorrect orders, missed
orders, or inaccurate P&L. The authors are not liable for any losses, damages,
or claims arising from its use.

## Your responsibilities
You are solely responsible for:

- Complying with [Polymarket US](https://polymarket.us)'s terms and exchange rulebook,
  including its rules on automated trading and wash trades. Trading rules still apply
  to orders placed by software. **Confirm that automated trading with your account is
  permitted before enabling it.**
- Your own account eligibility. Polymarket US accounts are for people who pass its
  identity check.
- Complying with all laws and regulations that apply to you.
- The security of your API key and the machine you run this on. Anyone with the
  Secret Key can trade your account. If you think it has been exposed, revoke it at
  <https://polymarket.us/developer>.
- Every order the software places on your behalf.

## Start small — the engines have no practice mode
**Every engine places real orders with real money the moment it is enabled while the
app is Live.** All engines ship **OFF by default**. Before turning any engine on,
understand exactly what it does, and begin with a small amount you can afford to lose
entirely.

The app has one **Paper | Live** account mode, and a new install starts on Paper.
In Paper, orders you place by hand, from the phone, by standing stops and by AI
agents are simulated, with fills walked out of the real order book and no API key
required. Without a key that book comes from public quotes the exchange caches for
up to 30 seconds, so a simulated fill can differ from what a live order would get.
**The engines are not simulated.** In Paper the crypto and live-script engines send
nothing; once the app is Live, each one you have switched on trades for real. Do not
mistake an engine that waited quietly in Paper for one that has been tested. (User
scripts have their own separate *shadow mode*, which is a paper mode in all but name.)

## AI agents trade on your behalf
If you connect an AI agent or switch on Autopilot, **every order it places is an
order you placed.** Language models are often wrong, can be confidently wrong, and
can be talked into things by text they read. The app enforces caps, a daily loss
stop, approvals and other rules in its backend, but those limit the damage; they do
not make an agent's decisions good. No model has a proven edge. Keep agents on
paper until their scored record says otherwise, and keep live caps small.

## No usage data leaves your machine
Krypt PolyBot US sends **no** usage data, telemetry or statistics anywhere.

The app talks to Polymarket US, public crypto-price feeds and Kalshi (for read-only
price comparison), and to nobody else — unless you switch on one of these yourself:
the optional Discord/Telegram remote-control bots, which necessarily send what you ask
them for to that chat platform; AI analysis or Autopilot, which send market text (and,
for Autopilot, whatever its agent reads) to the AI provider you chose; or an AI agent
client, which sends whatever the agent reads, including your positions and balance,
to that client's provider. Every outbound host is listed with live call counts in
**Settings → Data & privacy**, with one switch that turns market data off.

## No affiliation
Krypt PolyBot US is an independent project and is **not affiliated with, endorsed
by, or sponsored by** Polymarket, Polymarket US, Discord, or any data provider. Your
use of those services is governed by their own terms, and you are responsible for
complying with them. This fork carries no referral or affiliate links.

## Limitation of liability
To the maximum extent permitted by law, the authors and contributors shall not
be liable for any direct, indirect, incidental, special, consequential, or
exemplary damages — including, without limitation, trading losses, lost profits,
missed or erroneous orders, loss of funds, data loss, or actions taken by
Polymarket US — arising out of or relating to your use of (or inability to use)
this software, even if advised of the possibility of such damages. Your sole
remedy is to stop using the software.

If you do not agree with any of the above, do not use this software.
