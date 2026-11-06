import { ExternalLink, FolderOpen, Globe, MessageCircle, RotateCcw } from 'lucide-react';
import { useApp } from '../state/AppStateProvider';
import { Card, Page, Section } from '../components/common';
import {
  POLYMARKET_DEVELOPER_URL, POLYMARKET_SIGNUP_URL, KRYPT_DISCORD, KRYPT_HOME, KRYPT_TOOLS, KRYPT_TRADER_PAGE,
} from '../utils/links';

export function AboutPage() {
  const { appVersion, backend } = useApp();

  const open = (url: string) => () => void window.krypt.app.openExternal(url);

  const showFolder = async (): Promise<void> => {
    const p = await window.krypt.app.getUserDataPath();
    await window.krypt.app.showItemInFolder(p);
  };

  return (
    <Page title="About" subtitle="Version, links, support, and credits.">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <div className="flex items-start gap-4">
            <div className="grid h-16 w-16 place-items-center rounded-2xl bg-krypt-glow shadow-krypt-strong">
              <span className="font-pixel text-sm">K</span>
            </div>
            <div>
              <div className="font-pixel text-sm">KRYPT POLYBOT</div>
              <div className="mt-1 text-sm text-krypt-muted">
                Free trading app for Polymarket US · v{appVersion}
              </div>
              <div className="mt-1 text-xs text-krypt-dim">
                Backend: {backend.status} · pid {backend.pid ?? '—'}
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button onClick={open(KRYPT_TRADER_PAGE)} className="krypt-btn-default">
                  <Globe className="h-4 w-4" /> krypt.cc/tools/polyus <ExternalLink className="h-3 w-3" />
                </button>
                <button onClick={open(KRYPT_DISCORD)} className="krypt-btn-default">
                  <MessageCircle className="h-4 w-4" /> Discord <ExternalLink className="h-3 w-3" />
                </button>
                <button onClick={showFolder} className="krypt-btn-default">
                  <FolderOpen className="h-4 w-4" /> Open data folder
                </button>
                <button
                  onClick={() => void window.krypt.state.resetOnboarding()}
                  className="krypt-btn-default"
                  title="Re-show the first-run setup walkthrough"
                >
                  <RotateCcw className="h-4 w-4" /> Replay onboarding
                </button>
              </div>
            </div>
          </div>
          <p className="mt-6 text-sm text-krypt-muted">
            Krypt PolyBot US is part of the <span className="text-white">Krypt</span> free
            tools suite — a collection of small, polished, no-bullshit Windows utilities
            we build because the existing options annoyed us. If you want to support
            development without paying anything, follow{' '}
            <a
              href="#"
              onClick={(e) => { e.preventDefault(); open(KRYPT_HOME)(); }}
              className="text-krypt-purple hover:underline"
            >
              krypt.cc
            </a>.
          </p>
        </Card>

        <Card>
          <div className="text-sm text-white">Quick links</div>
          <div className="mt-3 flex flex-col gap-1.5 text-sm">
            <LinkRow label="Polymarket US" onClick={open(POLYMARKET_SIGNUP_URL)} />
            <LinkRow label="Polymarket US developer page" onClick={open(POLYMARKET_DEVELOPER_URL)} />
            <LinkRow label="Krypt Tools homepage" onClick={open(KRYPT_TOOLS)} />
          </div>
        </Card>
      </div>

      <Section title="Risk &amp; disclosure">
        <Card>
          <div className="space-y-2.5 text-xs leading-relaxed text-krypt-muted">
            <p>
              <span className="text-white">Not advice.</span> Krypt PolyBot US and its
              strategies, signals, and scores are for informational and educational
              purposes only — not financial, investment, legal, or tax advice. The
              authors are not registered investment or trading advisors, broker-dealers,
              or fiduciaries, and using this software creates no such relationship.
            </p>
            <p>
              <span className="text-white">Real risk of loss.</span> In Live this app places
              real orders on Polymarket US with real money. In Paper nothing real is sent.
              Trading event contracts
              carries substantial risk and you can lose some or all of your funds.
              Automated trading can lose money quickly — including while you are
              away from your computer. Only trade with money you can afford to lose.
            </p>
            <p>
              <span className="text-white">Strategies are unproven.</span> The bundled
              strategies are heuristics with <span className="text-white">no proven,
              fee-adjusted edge</span>, are not validated out-of-sample, and carry no
              guarantee of profitability. Past or simulated performance does not
              indicate future results.
            </p>
            <p>
              <span className="text-white">Provided as-is.</span> The software is free
              and provided &quot;AS IS&quot;, without warranty of any kind. It may
              contain bugs that cause incorrect orders, missed orders, or inaccurate
              P&amp;L. To the maximum extent permitted by law, the authors and
              contributors accept no liability for any direct or indirect losses or
              damages arising from its use; your sole remedy is to stop using it.
            </p>
            <p>
              <span className="text-white">Your responsibility.</span> You alone are
              responsible for every order placed, for complying with Polymarket US&apos;s
              terms and exchange rules (they apply to orders placed by software too),
              for all applicable laws and taxes, and for the security of your{' '}
              <span className="text-white">API key</span> — anyone who has it can trade
              your account.
            </p>
            <p>
              <span className="text-white">No usage data.</span> Krypt PolyBot US
              sends no telemetry, statistics or usage data anywhere. The community
              leaderboard and the Discord webhooks that used to do so have been
              removed. Market data and trading go to Polymarket US. The crypto cards and
              engine also read public crypto prices from outside feeds such as Coinbase,
              and Kalshi is contacted only if you turn on its price comparison on a market
              page. The terminal&apos;s hosts are listed with live call counts in{' '}
              <span className="text-white">Settings &rarr; Data &amp; privacy</span>,
              where Offline mode stops the terminal&apos;s requests. The optional
              remote-control bots only send what you ask them for.
            </p>
            <p>
              <span className="text-white">Affiliation.</span> Krypt PolyBot US is independent
              and is <span className="text-white">not affiliated with, endorsed by, or
              sponsored by</span> Polymarket US, QCX or Discord.
            </p>
            <p className="text-krypt-dim">
              By downloading, building, or running this software you accept these terms
              and the full Disclaimer included with the project. If you do not agree, do
              not use it.
            </p>
          </div>
        </Card>
      </Section>

      <Section title="Credits">
        <Card>
          <p className="text-xs text-krypt-muted">
            UI built with <span className="text-white">Electron · React · Tailwind · Recharts</span> ·
            backend in <span className="text-white">Python (httpx + cryptography)</span> ·
            packaged with <span className="text-white">PyInstaller + electron-builder</span>.
            Brand &amp; tooling by{' '}
            <a
              href="#"
              onClick={(e) => { e.preventDefault(); open(KRYPT_HOME)(); }}
              className="text-krypt-purple hover:underline"
            >
              Krypt
            </a>.
          </p>
        </Card>
      </Section>
    </Page>
  );
}

function LinkRow({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center justify-between rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-2 text-left text-xs hover:border-krypt-borderHi hover:bg-white/5"
    >
      <span>{label}</span>
      <ExternalLink className="h-3.5 w-3.5 text-krypt-muted" />
    </button>
  );
}
