import { startTransition, useEffect, useRef, useState } from 'react';
import {
  BarChart3, Bitcoin, Bot, BookOpen, Briefcase, Code2, FlaskConical, Folder,
  Compass, Info, KeyRound, LayoutDashboard, LineChart, ListChecks, Orbit, Receipt, Settings,
  Share2, Smartphone, SquareTerminal, Target, Users, Wallet,
} from 'lucide-react';
import { navTourId } from './tour/tourSteps';
import { useGuide } from './tour/useTour';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';
import { paperView, PAPER_BADGE_TITLE } from '../utils/balance';
import { HIDDEN_AMOUNT, useHideBalance } from '../utils/hideBalance';
import { WalletDialog } from './WalletDialog';
import { KryptSprite } from './KryptSprite';
import { FlexStatsCard } from './FlexStatsCard';
import { GlassPanel } from './glass/GlassPanel';
import { GlassLens } from './glass/GlassSegmented';
import { GLASS_TINTS } from './glass/glassPresets';
import { useGlideLens } from './glass/useGlideLens';
import type { PageId } from '../App';

type Tier = 'simple' | 'advanced';
type NavItem = {
  id: PageId;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  tier: Tier;
};

/**
 * The sidebar, grouped.
 *
 * It used to be twenty-two flat entries in one list, which stopped being
 * readable somewhere around the point the terminal landed: "Track" sat two
 * rows from "Bot positions" with nothing saying one is yours and the other is
 * the engine's, and "Discover" sat next to "Strategies" as though they were
 * the same kind of thing.
 *
 * The split that matters is WHO IS DECIDING. Terminal pages are ones you drive
 * by hand. Automation pages configure something that trades while you are
 * away. Keeping those apart is the same distinction the whole app is built
 * on, so the navigation may as well say it out loud.
 *
 * `tier: 'simple'` entries show in both modes; 'advanced' only in the full
 * cockpit. A group whose items are all hidden renders no header — so simple
 * mode does not show empty section labels.
 */
/** Exported for the tour's test: its stops must follow this order, so a tab
 *  added here without one fails the suite rather than going unexplained.
 *  `label` names the group in the tour; `title` is what the sidebar shows
 *  (null = no header). */
export const NAV_GROUPS: { title: string | null; label: string; hint?: string; items: NavItem[] }[] = [
  {
    // The front door. The user's own AI agents reading markets, committing
    // forecasts and trading on paper — and the hub where they are seen
    // working — come first, because that is the part of the app with a
    // scoreboard behind it. The bundled strategies below measured net
    // negative in the offline backtest.
    title: 'Agents',
    label: 'Agents',
    hint: 'Your own AI agents: forecast first, paper by default',
    items: [
      { id: 'agents', label: 'AI Agents', icon: Bot, tier: 'simple' },
      { id: 'visualizer', label: 'Agent Hub', icon: Orbit, tier: 'simple' },
    ],
  },
  {
    // Ungrouped on purpose: the overview belongs to neither half.
    title: null,
    label: 'Overview',
    items: [
      { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard, tier: 'simple' },
    ],
  },
  {
    title: 'Terminal',
    label: 'Terminal',
    hint: 'You decide, and you place the order',
    items: [
      { id: 'discover', label: 'Discover', icon: Compass, tier: 'simple' },
      { id: 'market', label: 'Market', icon: LineChart, tier: 'simple' },
      { id: 'track', label: 'Track', icon: Receipt, tier: 'simple' },
    ],
  },
  {
    title: 'Automation',
    label: 'Automation',
    hint: 'Engines that trade while you are away',
    items: [
      { id: 'strategies', label: 'Strategies', icon: Target, tier: 'simple' },
      { id: 'crypto15m', label: 'Crypto', icon: Bitcoin, tier: 'advanced' },
      { id: 'scripts', label: 'Scripts', icon: Code2, tier: 'simple' },
      // Named "Bot positions" rather than "Positions" so it reads distinctly
      // next to the terminal's Track screen: this is the ENGINE's own ledger.
      { id: 'positions', label: 'Bot positions', icon: Briefcase, tier: 'simple' },
      { id: 'backtest', label: 'Backtest', icon: FlaskConical, tier: 'simple' },
      { id: 'terminal', label: 'Cockpit HUD', icon: SquareTerminal, tier: 'advanced' },
    ],
  },
  {
    title: 'Account',
    label: 'Account',
    items: [
      { id: 'api', label: 'API Key', icon: KeyRound, tier: 'simple' },
      { id: 'history', label: 'History', icon: BarChart3, tier: 'advanced' },
      { id: 'profiles', label: 'Profiles', icon: Folder, tier: 'advanced' },
      { id: 'accounts', label: 'Accounts', icon: Users, tier: 'advanced' },
    ],
  },
  {
    title: 'App',
    label: 'App',
    items: [
      { id: 'remote', label: 'Remote', icon: Smartphone, tier: 'simple' },
      { id: 'settings', label: 'Settings', icon: Settings, tier: 'simple' },
      { id: 'logs', label: 'Logs', icon: ListChecks, tier: 'advanced' },
      // The Guide IS the onboarding document, so it has to be reachable in the
      // DEFAULT mode — it was 'advanced' and therefore hidden from exactly the
      // users who needed it.
      { id: 'guide', label: 'Guide', icon: BookOpen, tier: 'simple' },
      { id: 'about', label: 'About', icon: Info, tier: 'advanced' },
    ],
  },
];

interface SidebarProps {
  page: PageId;
  setPage: (p: PageId) => void;
}

export function Sidebar({ page, setPage }: SidebarProps) {
  const { config, account, paper, backend, state } = useApp();
  const { startTour } = useGuide();
  // Which book every number in the account card is. Live only on the exact
  // word: anything else (missing, loading) is Paper, the side that cannot
  // spend money.
  const live = config?.accountMode === 'live';
  // In Paper this card shows the paper book, in Live the real account.
  const book = paperView(paper);
  const money = (v: number | null): string => (v === null ? '—' : fmtUsd(v));
  const [acct, setAcct] = useState('Default');
  const [showStats, setShowStats] = useState(false);
  const [walletOpen, setWalletOpen] = useState(false);
  const hideBalance = useHideBalance();
  useEffect(() => { window.krypt.accounts.current().then(setAcct).catch(() => {}); }, []);

  // Keep the active item on screen. Now that the nav scrolls, navigating to a
  // page in a lower group (or jumping there from the command palette) left the
  // highlight below the fold — the sidebar looked like nothing was selected.
  const navRef = useRef<HTMLElement>(null);
  useEffect(() => {
    navRef.current
      ?.querySelector(`[data-page="${page}"]`)
      ?.scrollIntoView?.({ block: 'nearest' });
  }, [page]);
  const simple = state?.uiMode === 'simple';
  const groups = NAV_GROUPS
    .map((g) => ({ ...g, items: simple ? g.items.filter((n) => n.tier === 'simple') : g.items }))
    .filter((g) => g.items.length > 0);

  // The active-page pill: ONE live-glass lens that glides between items
  // (motion values, no React render per frame) instead of each item lighting
  // its own background. It sits inside the nav's scroll content, so it
  // scrolls with the item it marks. The pill follows its own key, set
  // synchronously on click, while the page swap renders as a transition:
  // mounting a heavy page is 100-200 ms of React work, and done urgently it
  // froze the first frames of the glide so the lens teleported.
  const pillRef = useRef<HTMLDivElement | null>(null);
  const [lensKey, setLensKey] = useState<PageId>(page);
  useEffect(() => { setLensKey(page); }, [page]);
  const go = (id: PageId): void => {
    setLensKey(id);
    startTransition(() => setPage(id));
  };
  useGlideLens(navRef, pillRef, lensKey, 'y', [simple]);

  // The panel is live glass, and a backdrop-filter makes its element the
  // containing block for `fixed` descendants — so the stats card this sidebar
  // opens renders as a SIBLING of the glass, not inside it, or it would lay
  // itself out inside a 240px column.
  return (
    <>
    <GlassPanel
      preset="chrome"
      display="block"
      tint={GLASS_TINTS.chrome}
      className="h-full w-60 shrink-0 border-r border-white/[0.06]"
    >
    <aside className="flex h-full w-full flex-col">
      <div className="px-4 py-4">
        <div className="flex items-center gap-2">
          <KryptSprite size={38} title="Krypt" />
          <div className="min-w-0">
            <div className="font-pixel text-[10px] uppercase tracking-[0.18em] text-white/90">
              Krypt
            </div>
            <button
              onClick={() => setShowStats(true)}
              className="group flex items-center gap-1 text-xs text-krypt-muted transition-colors hover:text-white"
              title="Open your shareable stats card"
            >
              <Share2 className="h-3 w-3 text-krypt-purple opacity-80 transition-opacity group-hover:opacity-100" />
              <span className="underline-offset-2 group-hover:underline">
                {acct === 'Default' ? 'Krypt Stats' : `${acct} · Stats`}
              </span>
            </button>
          </div>
          <button
            type="button"
            onClick={startTour}
            data-tour="tour-info"
            aria-label="Take the app tour"
            title="Take the app tour"
            className="ml-auto grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-white/10 text-krypt-muted transition-colors hover:border-krypt-purple/50 hover:text-white"
          >
            <Compass className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      {/* Scrolls: with five groups the advanced list is taller than most
          windows, and a nav you cannot reach the bottom of is worse than a
          long one. */}
      <nav ref={navRef} className="relative flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-2">
        {groups.map((group, gi) => (
          <div key={group.title ?? `g${gi}`} className={cls(gi > 0 && 'mt-3')}>
            {group.title && (
              <div
                className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-krypt-dim"
                title={group.hint}
              >
                {group.title}
              </div>
            )}
            <div className="flex flex-col gap-0.5">
              {group.items.map(({ id, label, icon: Icon }) => {
                const active = lensKey === id;
                return (
                  <button
                    key={id}
                    data-page={id}
                    data-glass-key={id}
                    data-tour={navTourId(id)}
                    onClick={() => go(id)}
                    aria-current={active ? 'page' : undefined}
                    className={cls(
                      'group relative z-[1] flex items-center gap-3 rounded-[10px] px-3 py-2 text-sm transition-colors duration-200',
                      active
                        ? 'text-white'
                        : 'text-krypt-muted hover:bg-white/[0.035] hover:text-white',
                    )}
                  >
                    <Icon className={cls('h-4 w-4 transition-colors duration-200', active && 'text-sky-300 drop-shadow-[0_0_6px_rgba(56,189,248,0.6)]')} />
                    <span>{label}</span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}

        {/* Idle mascot lounging in whatever space is left. `min-h-0` on a
            flex child that may be squeezed to nothing, so it yields to the
            nav rather than forcing it to scroll. */}
        {/* Only on windows tall enough to have spare space: below that it was
            squeezed to a clipped pair of ears behind the account card. */}
        <div className="relative hidden min-h-0 flex-1 select-none items-end justify-center pb-3 pt-4 [@media(min-height:960px)]:flex">
          <div className="pointer-events-none absolute bottom-2 h-3 w-16 rounded-[100%] bg-krypt-pink/25 blur-md" />
          <KryptSprite size={64} pet title="krypt" className="relative" />
        </div>
        <GlassLens lensRef={pillRef} />
      </nav>

      {/* The Agent Hub used to be a pinned "Live Visualizer" button here; it
          lives in the AGENTS group now, next to the page that connects the
          agents it shows. */}
      <div className="shrink-0 border-t border-white/[0.06] px-3 py-3" data-tour="account">
        {/* Nested live glass: inside the sidebar's own glass it refracts the
            panel above it rather than the aurora, but the rim and sheen are
            what make the balance read as an object you could pick up. */}
        <GlassPanel preset="card" tint={GLASS_TINTS.card} className="rounded-xl transition-colors hover:bg-white/[0.04]">
        <button
          type="button"
          onClick={() => setWalletOpen(true)}
          data-testid="wallet-open-sidebar"
          title="Open your wallet: refresh the balance, deposit, withdraw, settings"
          className="block w-full rounded-xl p-3 text-left"
        >
          <div className="flex items-center gap-2 text-xs text-krypt-muted">
            <KeyRound className="h-3.5 w-3.5" />
            <span className="uppercase tracking-wider">Account</span>
            {/* Always on screen, so nobody reads a paper balance as money or
                forgets the app is Live. */}
            <span
              data-testid="account-mode-pill"
              title={live
                ? 'LIVE: your real Polymarket US account. Orders spend real money.'
                : 'PAPER: real Polymarket US order books, imaginary money. The terminal, phone and agents trade paper, and the engines send nothing.'}
              className={cls(
                'ml-auto rounded-full border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
                live
                  ? 'border-krypt-loss/50 bg-krypt-loss/15 text-krypt-loss'
                  : 'border-krypt-warn/50 bg-krypt-warn/15 text-krypt-warn',
              )}
            >
              {live ? 'live' : 'paper'}
            </span>
          </div>
          <div data-testid="sidebar-balance" title={live ? undefined : PAPER_BADGE_TITLE} className="mt-1 font-mono text-lg tabular-nums text-white [text-shadow:0_0_18px_rgba(59,130,246,0.35)]">
            {hideBalance ? HIDDEN_AMOUNT : live ? fmtUsd(account?.totalUsd) : money(book.totalUsd)}
          </div>
          <div className="text-[11px] text-krypt-dim">
            {hideBalance
              ? 'Balance hidden'
              : live
              ? <>cash {fmtUsd(account?.cashUsd)} · port {fmtUsd(account?.portfolioUsd)}</>
              : <>cash {money(book.cashUsd)} · port {money(book.portfolioUsd)}</>}
          </div>
          <div className="mt-2 flex items-center justify-between text-[11px] text-krypt-muted">
            <span className="flex items-center gap-1"><Wallet className="h-3 w-3" /> Open wallet</span>
            <span className={cls(
              'h-1.5 w-1.5 rounded-full',
              backend.status === 'running' ? 'bg-krypt-win text-krypt-win shadow-[0_0_8px_currentColor]' : 'bg-krypt-warn',
            )} />
          </div>
        </button>
        </GlassPanel>
      </div>
    </aside>
    </GlassPanel>

    {showStats && <FlexStatsCard onClose={() => setShowStats(false)} />}
    {walletOpen && <WalletDialog onClose={() => setWalletOpen(false)} onNav={setPage} />}
    </>
  );
}
