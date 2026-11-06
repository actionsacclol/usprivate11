import { useState } from 'react';
import { Bot, ExternalLink, Gauge, Globe, SlidersHorizontal, Users, Wallet } from 'lucide-react';
import type { PageId } from '../App';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { ConfirmDialog, ShareButton } from './common';
import { POLYMARKET_SQUAD_URL } from '../utils/links';
import { cls, fmtPct, fmtUsd } from '../utils/format';
import { PAPER_BADGE_TITLE, paperView, realBalanceOf } from '../utils/balance';
import { GlassPanel } from './glass/GlassPanel';
import { GlassSegmented } from './glass/GlassSegmented';
import { GLASS_TINTS } from './glass/glassPresets';
import { GoLiveAccountDialog, useBackToPaper } from './AccountModePanel';
import { agentsLiveOnLive, backToPaperPatch, liveAgents } from '../utils/goLive';
import { userMessage } from '../utils/errors';
import { HIDDEN_AMOUNT, useHideBalance } from '../utils/hideBalance';
import { WalletDialog } from './WalletDialog';

/** The independent engines, in sidebar order. Each owns its own page and
 *  its own on/off switch — none of them implies any of the others. */
const ENGINES: { key: 'crypto' | 'scripts'; label: string }[] = [
  { key: 'crypto', label: 'Crypto' },
  { key: 'scripts', label: 'Scripts' },
];

/** The app-wide header.
 *
 *  It deliberately carries NO engine controls: each engine's switch lives on
 *  its own page, and the header only REPORTS. What stays here is genuinely
 *  global: the backend process status, account totals, and a per-engine
 *  live/off summary. */
export function TopBar({ onNav }: { onNav?: (p: PageId) => void } = {}) {
  const { config, account, paper, backend, state, refresh } = useApp();
  const uiMode = state?.uiMode ?? 'advanced';
  const [goLiveOpen, setGoLiveOpen] = useState(false);
  const toPaper = useBackToPaper();
  const toast = useToast();
  const [stopAsk, setStopAsk] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [walletOpen, setWalletOpen] = useState(false);
  const hideBalance = useHideBalance();

  const live: Record<string, boolean> = {
    crypto: !!config?.crypto15mEnabled,
    scripts: !!config?.scriptsLiveEnabled,
  };
  // In Paper the engines send nothing whatever their switches say (the
  // backend gates them: effective = switch AND account live). The switches
  // are kept, so this says so instead of flipping them.
  const paperAccount = config?.accountMode !== 'live';
  const switchedOn = ENGINES.filter((e) => live[e.key]).length;
  const liveCount = paperAccount ? 0 : switchedOn;

  // The AI agents are not an engine switch, but with approvals off they place
  // real orders on their own — the header used to say "All engines paused"
  // while they did. Real money = the app Live AND an agent set live
  // (agentsLiveOnLive, the same test as the AI Agents page's red bar).
  // Autopilot is one of those agents, named when it is on.
  const agentsReal = agentsLiveOnLive(config);
  const agentsPaper = !agentsReal && !!config && config.mcpTradeMode !== 'off'
    && (!!config.mcpEnabled || !!config.autopilotEnabled);
  const agentNames = liveAgents(config).map((a) => a.name);
  const agentsTitle = agentsReal
    ? `AI agents LIVE on real money: ${agentNames.join(', ')}`
      + `${config?.mcpLiveApproval === false ? ' — approvals OFF, they order without asking' : ' — each order waits for your approval'}`
      + `${config?.autopilotEnabled ? '. Autopilot is on.' : '.'} Click to stop them.`
    : agentsPaper
    ? `AI agents trade on paper${config?.autopilotEnabled ? ' (Autopilot on)' : ''} — no real money. Go live for them on the AI Agents page.`
    : 'AI agents: trading off.';

  const stopAgents = async (): Promise<void> => {
    setStopping(true);
    try {
      await window.krypt.config.update(backToPaperPatch());
      await refresh?.state?.().catch(() => undefined);
      toast.success('AI agents are back on paper: no more real-money orders from them.');
      setStopAsk(false);
    } catch (e) {
      toast.error(`Could not stop the agents: ${userMessage(e)}`);
    } finally {
      setStopping(false);
    }
  };

  // The backend-down snapshot is a placeholder whose numbers were never
  // measured (see AccountSnapshot.backendDown), and a missing field is not a
  // zero either. Both render as an em dash with no tint — the same rule the
  // terminal screens follow.
  const measured = !!account && !account.backendDown;
  const num = (v: number | null | undefined): number | null =>
    (measured && typeof v === 'number' && !Number.isNaN(v) ? v : null);
  const paperBook = paperView(paper);
  const balance = paperAccount ? paperBook.totalUsd : num(account?.totalUsd);
  const pnl = paperAccount ? paperBook.pnlUsd : num(account?.sessionPnlUsd);
  const roi = paperAccount ? paperBook.roiPct : num(account?.sessionRoiPct ?? account?.roiPct);
  const unknownHint = paperAccount ? PAPER_UNKNOWN_HINT : UNKNOWN_HINT;
  const realBalance = realBalanceOf(account, paper);

  return (
    // Live glass across the top of the content, rim-only (chrome preset).
    <GlassPanel
      preset="chrome"
      display="flex"
      tint={GLASS_TINTS.chrome}
      className="relative z-20 shrink-0 items-center gap-4 border-b border-white/[0.06] px-6 py-3"
    >
      {/* Paper | Live: the account mode, two buttons, first thing in the header,
          so which side you're on and how to change it are both obvious. A click
          goes through the same gates as Settings: the Go-live checklist one way,
          the back-to-Paper confirm the other. Clicking the side you're on does
          nothing, and neither side lights up until the config has loaded. */}
      <div
        role="group"
        aria-label="Paper or Live"
        data-testid="account-mode-switch"
        className="inline-flex shrink-0 overflow-hidden rounded-lg border border-white/[0.12] text-[11px] font-bold uppercase tracking-wider"
      >
        <button
          type="button"
          data-testid="mode-paper"
          aria-pressed={!!config && paperAccount}
          disabled={!config || toPaper.busy}
          title={paperAccount
            ? PAPER_BADGE_TITLE
            : 'LIVE: orders spend the real dollars in your Polymarket US account.\n\nClick to switch back to Paper.'}
          onClick={() => { if (!paperAccount) void toPaper.ask(); }}
          className={cls(
            'px-3 py-1.5 transition-colors',
            config && paperAccount ? 'bg-emerald-500/20 text-emerald-200' : 'text-krypt-muted hover:text-white',
          )}
        >
          Paper
        </button>
        <button
          type="button"
          data-testid="mode-live"
          aria-pressed={!!config && !paperAccount}
          disabled={!config || toPaper.busy}
          title={paperAccount
            ? 'Click to take the app live. A checklist comes first. Your AI agents go live separately, on the AI Agents page.'
            : 'LIVE: orders spend the real dollars in your Polymarket US account.'}
          onClick={() => { if (paperAccount) setGoLiveOpen(true); }}
          className={cls(
            'px-3 py-1.5 transition-colors',
            config && !paperAccount ? 'bg-rose-500/25 text-rose-200' : 'text-krypt-muted hover:text-white',
          )}
        >
          Live
        </button>
      </div>

      {/* Simple = 5-page shell with one-click Strategies; Advanced = the full
          cockpit. Right beside the mode switch so nobody misses that the
          other mode exists. A view preference (persisted in settings.json) —
          it never touches trading config or any engine switch. */}
      <GlassSegmented
        className="shrink-0"
        value={uiMode}
        onChange={(m) => void window.krypt.state.setUiMode(m)}
        options={[
          { value: 'simple', label: 'Simple', icon: <Gauge className="h-3.5 w-3.5" />,
            title: 'Everyday pages: AI agents, markets, strategies, scripts, backtests, your account and settings' },
          { value: 'advanced', label: 'Advanced', icon: <SlidersHorizontal className="h-3.5 w-3.5" />,
            title: 'Every page and engine the app has' },
        ]}
      />

      <div className="flex min-w-0 items-center gap-3">
        <div className="flex min-w-0 items-baseline gap-2">
          <h1 className="glass-title truncate whitespace-nowrap text-lg font-semibold">
            {paperAccount && switchedOn > 0
              ? 'Paper: engines send nothing'
              : agentsReal
              ? (liveCount === 0
                ? 'AI agents live'
                : `${liveCount} engine${liveCount === 1 ? '' : 's'} + AI agents live`)
              : liveCount === 0
              ? 'All engines paused'
              : `${liveCount} engine${liveCount === 1 ? '' : 's'} live`}
          </h1>
          <span
            className={cls(
              'hidden shrink-0 whitespace-nowrap text-xs xl:inline',
              backend.status === 'running' ? 'text-krypt-muted' : 'text-krypt-warn',
            )}
          >
            · {backend.status === 'running' ? 'Backend online' : `Backend ${backend.status}`}
          </span>
        </div>

        {/* Always visible while agents can spend real money — at any window
            width, unlike the chips below — because with approvals off nothing
            else on screen says they are trading. */}
        {agentsReal && (
          <button
            type="button"
            data-testid="topbar-stop-agents"
            title={agentsTitle}
            disabled={stopping}
            onClick={() => setStopAsk(true)}
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-loss/60 bg-krypt-loss/15 px-2.5 py-1 text-[11px] font-semibold text-krypt-loss transition-colors hover:border-krypt-loss hover:bg-krypt-loss/25"
          >
            <Bot className="h-3.5 w-3.5" />
            Stop agents
          </button>
        )}

        {/* The exchange is restarting or post-only: market data still flows and
            nothing else on screen would say why orders are bouncing. */}
        {account?.tradingRestriction && (
          <span
            className="flex shrink-0 items-center gap-1.5 rounded-full border border-krypt-warn/50 bg-krypt-warn/10 px-2.5 py-1 text-[11px] font-medium text-krypt-warn"
            title={account.tradingRestriction.message}
          >
            <Globe className="h-3.5 w-3.5" />
            {account.tradingRestriction.kind === 'restarting' ? 'Exchange restarting' : 'Post-only mode'}
          </span>
        )}
        {/* Which engines, specifically — each links nowhere, it just reports. */}
        {/* Only with room to spare: at a 1380px window these chips were what
            pushed the headline onto three lines under the stats. The headline
            already says how many are live. With the Agents chip, 2xl (1536px)
            truncated the headline again, hence 1760px. */}
        <div className="hidden items-center gap-1.5 min-[1760px]:flex">
          {ENGINES.map((e) => (
            <span
              key={e.key}
              title={live[e.key] && paperAccount
                ? `${e.label} engine is switched on, but the app is in Paper, so it sends nothing. Go live in Settings → Paper | Live.`
                : `${e.label} engine ${live[e.key] ? 'live' : 'off'} — switch it on its own page`}
              className={cls(
                'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
                live[e.key] && paperAccount
                  ? 'bg-krypt-warn/15 text-krypt-warn'
                  : live[e.key]
                  ? 'bg-krypt-win/15 text-krypt-win'
                  : 'bg-krypt-surface2 text-krypt-dim',
              )}
            >
              {e.label}
            </span>
          ))}
          {/* The AI agents, styled like the engines: green when they can spend
              real money (a click asks to stop them), amber on paper. */}
          <button
            type="button"
            data-testid="topbar-agents-chip"
            title={agentsTitle}
            disabled={!agentsReal}
            onClick={() => setStopAsk(true)}
            className={cls(
              'rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide',
              agentsReal
                ? 'bg-krypt-win/15 text-krypt-win hover:bg-krypt-win/25'
                : agentsPaper
                ? 'cursor-default bg-krypt-warn/15 text-krypt-warn'
                : 'cursor-default bg-krypt-surface2 text-krypt-dim',
            )}
          >
            Agents
          </button>
        </div>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <button
          type="button"
          onClick={() => void window.krypt.app.openExternal(POLYMARKET_SQUAD_URL)}
          data-testid="topbar-squad"
          title="Opens the Krypt squad page in your browser. Click join there. This link credits Krypt as your referrer."
          className="group flex shrink-0 items-center gap-2 rounded-xl border border-krypt-win/50 bg-krypt-win/10 px-3 py-1.5 text-left transition hover:border-krypt-win hover:bg-krypt-win/20"
        >
          <Users className="h-4 w-4 shrink-0 text-krypt-win" />
          <span className="leading-tight">
            <span className="block whitespace-nowrap text-xs font-semibold text-white">Join the Krypt squad</span>
            <span className="block whitespace-nowrap text-[10px] text-krypt-muted group-hover:text-white/80">on Polymarket US</span>
          </span>
          <ExternalLink className="h-3 w-3 shrink-0 text-krypt-muted group-hover:text-white" />
        </button>
        <div className="hidden items-center gap-3 px-3 md:flex">
          <button
            type="button"
            onClick={() => setWalletOpen(true)}
            data-testid="wallet-open-topbar"
            title="Open your wallet: refresh the balance, deposit, withdraw, settings"
            className="-my-1 flex items-center gap-2 rounded-lg border border-transparent px-2 py-1 transition-colors hover:border-white/[0.15] hover:bg-white/[0.06]"
          >
            <Wallet className="h-4 w-4 text-krypt-muted" />
            <Stat label={paperAccount ? 'Paper balance' : 'Balance'} value={hideBalance ? HIDDEN_AMOUNT : balance === null ? DASH : fmtUsd(balance)}
              title={hideBalance ? undefined : balance === null ? unknownHint : paperAccount
                ? `${PAPER_BADGE_TITLE}${realBalance === null ? '' : ` Your real account: ${fmtUsd(realBalance)}.`}`
                : undefined} />
          </button>
          <Stat
            label={paperAccount ? 'Paper P&L' : 'Session P&L'}
            value={hideBalance ? HIDDEN_AMOUNT : pnl === null ? DASH : fmtUsd(pnl, { sign: true })}
            title={pnl === null ? unknownHint : paperAccount ? 'Since this paper book started.' : undefined}
            // No green tint on a number we do not have.
            color={hideBalance ? undefined : pnl === null ? 'text-krypt-dim' : pnl >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}
          />
          <Stat
            label="ROI"
            value={hideBalance ? HIDDEN_AMOUNT : roi === null ? DASH : fmtPct(roi)}
            title={roi === null ? unknownHint : undefined}
            color={hideBalance ? undefined : roi === null ? 'text-krypt-dim' : roi >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}
          />
          {/* Nothing to share when there is nothing to report — a boast built
              from placeholder zeros is worse than no button. */}
          {/* Nor from imaginary money. */}
          {!hideBalance && !paperAccount && balance !== null && pnl !== null && roi !== null && (
            <ShareButton
              size="xs"
              text={
                `Krypt PolyBot US: ${fmtUsd(balance)} balance · `
                + `${fmtUsd(pnl, { sign: true })} this session · `
                + `${fmtPct(roi)} ROI. `
                + `Free Polymarket US trading app by @YuhgoSlavia · krypt.cc/tools/polyus`
              }
            />
          )}
        </div>
      </div>
      {walletOpen && <WalletDialog onClose={() => setWalletOpen(false)} onNav={onNav} />}
      {toPaper.dialog}
      {goLiveOpen && <GoLiveAccountDialog onClose={() => setGoLiveOpen(false)} onNav={onNav} />}
      <ConfirmDialog
        open={stopAsk}
        danger
        title="Stop your AI agents trading real money?"
        body={(
          <>
            <p>
              Puts every AI agent back on paper{config?.autopilotEnabled ? ', Autopilot included' : ''}: no
              more real-money orders from {agentNames.length ? agentNames.join(', ') : 'any agent'}.
            </p>
            <p className="mt-2">
              The app stays Live for you and the engines. Positions the agents opened and any
              orders still resting stay as they are — the stops already set on them still fire.
              Go live for agents again on the AI Agents page.
            </p>
          </>
        )}
        confirmLabel={stopping ? 'Stopping…' : 'Stop agents'}
        onConfirm={() => { if (!stopping) void stopAgents(); }}
        onClose={() => setStopAsk(false)}
      />
    </GlassPanel>
  );
}

const DASH = '—';
const PAPER_UNKNOWN_HINT = 'The paper balance has not been read yet: the trading engine is starting or stopped.';
const UNKNOWN_HINT =
  'Nobody could produce this value — the backend is not running, so no balance was read.';

function Stat({
  label, value, color, title,
}: { label: string; value: string; color?: string; title?: string }) {
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="text-[10px] uppercase tracking-wider text-krypt-muted">
        {label}
      </span>
      <span className={cls('font-mono text-sm', color || 'text-white')} title={title}>
        {value}
      </span>
    </div>
  );
}
