import { useCallback, useEffect, useRef, useState } from 'react';
import { TitleBar } from './components/TitleBar';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { GlassBackdrop } from './components/glass/GlassBackdrop';
import { AppStateProvider, useApp } from './state/AppStateProvider';
import { ToastProvider, useToast } from './state/ToastProvider';
import { GuideHost } from './components/tour/GuideHost';
import { PageGuard, usePersistedPage } from './components/PageGuard';
import { autoOnboarding } from '@shared/onboarding';
import { AlertStack } from './components/AlertStack';
import { agentCallEvent, agentEvent, publishActivity } from './state/activity';
import { agentOrderMessage, agentOrderToast } from '@shared/agentOrderNotice';
import { useWindowVisibility } from './state/visibility';
import { startAmbientClock } from './state/ambient';
import { kickGlassWatchdog, startGlassWatchdog } from './components/glass/glassQuality';
import { DashboardPage } from './pages/Dashboard';
import { SettingsPage } from './pages/Settings';
import { PositionsPage } from './pages/Positions';
import { HistoryPage } from './pages/History';
import { ProfilesPage } from './pages/Profiles';
import { LogsPage } from './pages/Logs';
import { ApiKeysPage } from './pages/ApiKeys';
import { AboutPage } from './pages/About';
import { GuidePage } from './pages/Guide';
import { VisualizerPage } from './pages/Visualizer';
import { Crypto15mPage } from './pages/Crypto15m';
import { AccountsPage } from './pages/Accounts';
import { BacktestPage } from './pages/Backtest';
import { TerminalPage } from './pages/Terminal';
import { ScriptsPage } from './pages/Scripts';
import { StrategiesPage } from './pages/Strategies';
import { DiscoverPage } from './pages/Discover';
import { MarketPage } from './pages/Market';
import { TrackPage } from './pages/Track';
import { RemotePage } from './pages/Remote';
import { AiAgentsPage } from './pages/AiAgents';
import { CommandPalette, useTerminalHotkeys } from './components/terminal/CommandPalette';

export type PageId =
  | 'dashboard' | 'positions' | 'history'
  | 'profiles' | 'settings' | 'api' | 'logs' | 'guide' | 'about'
  | 'visualizer' | 'crypto15m' | 'accounts' | 'backtest'
  | 'terminal' | 'scripts' | 'strategies'
  // The manual terminal: Discover -> Market -> Track. Deliberately three
  // separate surfaces rather than tabs inside one, because each answers a
  // different question and the user moves between them constantly.
  | 'discover' | 'market' | 'track' | 'remote' | 'agents';

/** The surfaces visible in simple mode; everything else needs Advanced. */
export const SIMPLE_PAGES: readonly PageId[] = [
  // The front door: the user's own agents and the hub that shows them.
  'agents', 'visualizer',
  'dashboard', 'strategies', 'scripts', 'backtest', 'positions', 'api', 'settings',
  // The terminal is the point of simple mode too: a user who is not running
  // the automation still wants to look things up and trade by hand.
  'discover', 'market', 'track', 'remote', 'guide',
];

export default function App() {
  return (
    <ToastProvider>
      <AppStateProvider>
        <Shell />
      </AppStateProvider>
    </ToastProvider>
  );
}

function Shell() {
  // Where the app opens: the last page, else AI Agents (the front door) — and
  // never a page that crashed (state/lastPage.ts, PageGuard).
  const [page, setPage, onPageCrash] = usePersistedPage();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const { state } = useApp();
  const toast = useToast();
  // Minimized or in the tray: pause the backdrop's animation and the Agent
  // Hub's render loop (state/visibility.ts).
  useWindowVisibility();
  // Decorative loops on one ~15 Hz clock instead of every vsync (ambient.ts),
  // and the Auto glass watchdog that drops to frost on a machine that can't
  // hold its frame rate with live glass (glassQuality.ts).
  useEffect(() => {
    startAmbientClock();
    startGlassWatchdog();
    const onDemote = (): void => toast.info(
      'Glass effects switched to Frost to keep the app smooth on this machine. '
      + 'Change it in Settings → Appearance.');
    window.addEventListener('krypt:glass-demoted', onDemote);
    return () => window.removeEventListener('krypt:glass-demoted', onDemote);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { kickGlassWatchdog(); }, [page]);

  // Ctrl/Cmd+K anywhere, `/` to search, `g d|m|t|s|w` to jump. All inert while
  // a field has focus. `setPage`/`setPaletteOpen` are stable, so the listener
  // is bound once rather than rebound on every render.
  const openPalette = useCallback(() => setPaletteOpen(true), []);
  useTerminalHotkeys({ onPalette: openPalette, onNav: setPage });

  // Agent pushes that already reach this window, mirrored onto the activity
  // bus (renderer memory only) so the Agent Hub can replay the last few
  // minutes when it opens. An agent order (a live one waiting for approval
  // above all) can land while the user is on any page, so it is toasted
  // app-wide. Optional-chained: an older preload has neither channel.
  useEffect(() => {
    // Two shapes arrive: {mode, message, approvalId?} and a superset with the
    // order's fields and its outcome (status). A live outcome also raises a
    // desktop notification — in main (notifyAgentOrder), so it shows while
    // the window is hidden. Defensive: any field may be missing.
    const offOrder = window.krypt.mcp?.onOrder?.((d) => {
      const message = agentOrderMessage(d);
      publishActivity(() => agentEvent(message ? { ...d, message } : d));
      const t = agentOrderToast(d);
      if (t) toast.push(t.text, t.tone, t.ms);
    });
    const offCalls = window.krypt.mcp?.onToolCall?.((d) => publishActivity(() => agentCallEvent(d)));
    return () => { offOrder?.(); offCalls?.(); };
  }, [toast]);

  // SWITCHING to simple mode while parked on a cockpit-only page would leave
  // the sidebar with no active item — fall back to the front door. Only on
  // the mode TRANSITION: explicit navigation to a cockpit page (e.g. the
  // Strategies page's "Review & arm" jump to Scripts) must work in simple
  // mode too.
  const uiMode = state?.uiMode ?? 'advanced';
  const prevMode = useRef(uiMode);
  useEffect(() => {
    if (uiMode === 'simple' && prevMode.current === 'advanced'
        && !SIMPLE_PAGES.includes(page)) {
      setPage('agents');
    }
    prevMode.current = uiMode;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- transition-only
  }, [uiMode]);

  // Onboarding (first run, once after an update, and replay), the app tour
  // and its one-time offer all live in GuideHost; see there for why the modal
  // is not keyed straight off acceptedDisclaimer.
  const auto = autoOnboarding(state);

  return (
    <GuideHost needsOnboarding={auto === 'first'} needsUpdateOnboarding={auto === 'update'} setPage={setPage}>
      <div className="flex h-full w-full flex-col">
        {/* Fixed z-0 and FIRST, so the row below paints over it without
            becoming a stacking context. */}
        <GlassBackdrop />
        <TitleBar />
        <div className="relative flex h-[calc(100%-2.25rem)] w-full">
          <Sidebar page={page} setPage={setPage} />
          <main className="relative flex flex-1 flex-col overflow-hidden">
            <TopBar onNav={setPage} />
            {/* Only the page is guarded: the TopBar's controls must survive
                any page crashing. */}
            <div className="flex-1 overflow-hidden">
              <PageGuard page={page} setPage={setPage} onCrash={onPageCrash}>
                <PageRouter page={page} setPage={setPage} />
              </PageGuard>
            </div>
          </main>
        </div>
        <AlertStack onNav={setPage} />
        <CommandPalette
          open={paletteOpen}
          onClose={() => setPaletteOpen(false)}
          onNav={setPage}
        />
      </div>
    </GuideHost>
  );
}

function PageRouter({ page, setPage }: { page: PageId; setPage: (p: PageId) => void }) {
  switch (page) {
    case 'dashboard': return <DashboardPage onNav={setPage} />;
    case 'positions': return <PositionsPage />;
    case 'history': return <HistoryPage />;
    case 'profiles': return <ProfilesPage />;
    case 'settings': return <SettingsPage onNav={setPage} />;
    case 'api': return <ApiKeysPage />;
    case 'logs': return <LogsPage />;
    case 'guide': return <GuidePage />;
    case 'about': return <AboutPage />;
    case 'visualizer': return <VisualizerPage />;
    case 'terminal': return <TerminalPage />;
    case 'crypto15m': return <Crypto15mPage />;
    case 'accounts': return <AccountsPage />;
    case 'backtest': return <BacktestPage />;
    case 'scripts': return <ScriptsPage />;
    case 'strategies': return <StrategiesPage onNav={setPage} />;
    case 'discover': return <DiscoverPage onNav={setPage} />;
    case 'market': return <MarketPage onNav={setPage} />;
    case 'track': return <TrackPage onNav={setPage} />;
    case 'remote': return <RemotePage />;
    case 'agents': return <AiAgentsPage onNav={setPage} />;
    default: return <AiAgentsPage onNav={setPage} />;
  }
}
