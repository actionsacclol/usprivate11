import { useEffect, useState } from 'react';
import { AlertTriangle, ArrowRight, CheckCircle2, RefreshCw, Sparkles, TrendingDown, TrendingUp } from 'lucide-react';
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { PnlPoint } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Card, Empty, Page, ShareableStat, StatCard } from '../components/common';
import { cls, fmtPct, fmtRelative, fmtUsd } from '../utils/format';
import { WhyNotTrading } from '../components/WhyNotTrading';
import { PAPER_BADGE_TITLE, paperView, realBalanceOf } from '../utils/balance';
import type { PageId } from '../App';

interface DashboardProps {
  onNav: (p: PageId) => void;
}

export function DashboardPage({ onNav }: DashboardProps) {
  const { account, paper, positions, backend, credentials, config, refresh } = useApp();
  const toast = useToast();
  const [series, setSeries] = useState<PnlPoint[]>([]);
  const [restarting, setRestarting] = useState(false);

  // The Python engine has died (or was stopped). While it's down nothing
  // trades, positions don't reconcile, and balances go stale — so we surface
  // a one-click recovery card instead of letting it hide in the status list.
  const engineDown = backend.status === 'crashed' || backend.status === 'stopped';

  const restartEngine = async (): Promise<void> => {
    setRestarting(true);
    toast.info('Restarting engine…');
    try {
      await window.krypt.backend.restart();
      // Give the child process a moment to come up, then re-pull its state.
      window.setTimeout(() => { void refresh.backend(); }, 1200);
    } catch (e: any) {
      toast.error(`Restart failed: ${e?.message || e}`);
    } finally {
      window.setTimeout(() => setRestarting(false), 1200);
    }
  };

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const s = await window.krypt.data.pnlSeries(168);
        if (mounted) setSeries(s);
      } catch { /* noop */ }
    };
    void load();
    const i = window.setInterval(load, 30000);
    return () => {
      mounted = false;
      window.clearInterval(i);
    };
  }, []);

  // Truly active positions: have real exposure on the exchange right now.
  // Excludes dry-runs and any failed/aborted lifecycle states that
  // never produced fills (canceled, expired, gone, error).
  const openPos = positions.filter(
    (p) => !p.resolved
      && p.status !== 'dry_run'
      && (p.status === 'filled' || p.status === 'partial' || p.status === 'submitted'),
  );
  const recentResolved = positions
    .filter((p) => p.resolved)
    .slice(0, 5);

  const issues: { label: string; tone: 'warn' | 'bad' | 'good'; cta?: { label: string; page: PageId } }[] = [];
  const keySaved = !!credentials?.hasWalletKey;
  const paperAccount = config?.accountMode !== 'live';
  if (!keySaved) {
    // There is no dry-run mode any more: Paper is the account mode, and it
    // needs no API key at all. Only Live is blocked without one.
    issues.push(paperAccount
      ? {
        label: 'Paper: no account needed. Set one up before you go live.',
        tone: 'warn',
        cta: { label: 'Set up account', page: 'api' },
      }
      : {
        label: 'Live, but no API key is saved — nothing can trade until one is',
        tone: 'bad',
        cta: { label: 'Set up account', page: 'api' },
      });
  } else if (!backend.authOk) {
    issues.push({
      label: 'The saved API key could not connect to Polymarket US',
      tone: 'bad',
      cta: { label: 'Re-test', page: 'api' },
    });
  }
  // 'crashed'/'stopped' get the prominent recovery card below, so only the
  // transient 'restarting' state needs a line in the status list here.
  if (!engineDown && backend.status !== 'running' && backend.status !== 'starting') {
    issues.push({
      label: `Backend is ${backend.status}${backend.lastError ? ` — ${backend.lastError}` : ''}`,
      tone: 'bad',
    });
  }
  if (issues.length === 0) {
    issues.push({ label: 'Everything looks good. Bot is online and watching.', tone: 'good' });
  }

  // Headline numbers are SESSION-BASED (the user's preferred model):
  //   session P&L  = total balance now − total balance at bot run start
  // We also surface today + all-time deltas (snapshot-based) for context,
  // and demote the noisy position-derived realized P&L to a footnote.
  const sessionPnl = account?.sessionPnlUsd ?? 0;
  const sessionBaseline = account?.sessionBaselineUsd ?? null;
  const sessionRoi = account?.sessionRoiPct ?? 0;
  const sessionStartedAt = account?.sessionStartedAt;
  const alltimePnl = account?.alltimePnlUsd ?? 0;
  const realizedPos = account?.realizedPnlUsd ?? 0;
  const alltimeBaseline = account?.alltimeBaselineUsd ?? null;
  const paperMode = !!config && config.accountMode !== 'live';
  const book = paperView(paper);
  const money = (v: number | null, opts?: { sign?: boolean }): string => (v === null ? '—' : fmtUsd(v, opts));
  const realBalance = realBalanceOf(account, paper);

  return (
    <Page title="Dashboard" subtitle="Live snapshot of your portfolio and bot health.">
      {engineDown && (
        <div className="mb-4 flex flex-col gap-3 rounded-xl border border-krypt-loss/50 bg-krypt-loss/10 p-4 sm:flex-row sm:items-center">
          <AlertTriangle className="h-6 w-6 shrink-0 text-krypt-loss" />
          <div className="flex-1">
            <div className="text-sm font-semibold text-krypt-loss">
              Trading engine is {backend.status === 'stopped' ? 'stopped' : 'offline'}
            </div>
            <div className="mt-0.5 text-xs text-krypt-muted">
              No trades, reconciliation, or balance updates are running while the
              engine is down.
              {backend.lastError ? ` Last error: ${backend.lastError}` : ''}
            </div>
          </div>
          <button
            onClick={restartEngine}
            disabled={restarting}
            className="krypt-btn-primary shrink-0 disabled:opacity-50"
          >
            <RefreshCw className={cls('h-4 w-4', restarting && 'animate-spin')} />
            {restarting ? 'Restarting…' : 'Restart engine'}
          </button>
        </div>
      )}
      {paperMode ? (
        <div className="grid gap-4 md:grid-cols-3">
          <StatCard
            label="Total Balance"
            value={(
              <span className="inline-flex items-center gap-2">
                {money(book.totalUsd)}
                <span
                  data-testid="paper-pill"
                  title={PAPER_BADGE_TITLE}
                  className="rounded-full border border-krypt-warn/50 bg-krypt-warn/10 px-2 py-0.5 font-sans text-[10px] font-semibold uppercase tracking-wider text-krypt-warn"
                >
                  Paper
                </span>
              </span>
            )}
            hint={`cash ${money(book.cashUsd)} · positions ${money(book.portfolioUsd)}`
              + `${realBalance === null ? '' : ` · real account ${fmtUsd(realBalance)}`}`}
          />
          <StatCard
            label="Paper P&L"
            value={money(book.pnlUsd, { sign: true })}
            hint={book.pnlUsd === null || book.bankrollUsd === null
              ? 'not read yet'
              : `${fmtUsd(book.bankrollUsd)} → ${fmtUsd(book.totalUsd ?? 0)} · ${fmtPct(book.roiPct ?? 0)}`}
            accent={book.pnlUsd === null ? undefined : book.pnlUsd >= 0 ? 'good' : 'bad'}
          />
          <StatCard
            label="Open Positions"
            value={paper ? `${paper.openPositions}` : '—'}
            hint={paper ? `${paper.restingOrders ?? 0} resting ${paper.restingOrders === 1 ? 'order' : 'orders'} · paper` : 'not read yet'}
          />
        </div>
      ) : (
      <>
      <div className="grid gap-4 md:grid-cols-3">
        <ShareableStat
          label="Total Balance"
          value={fmtUsd(account?.totalUsd)}
          hint={account?.balanceSyncing
            ? '⏳ syncing — an order just filled or settled; the venue reflects it in a moment'
            : `cash ${fmtUsd(account?.cashUsd)} · port ${fmtUsd(account?.portfolioUsd)}`}
          shareText={`Krypt PolyBot US balance: ${fmtUsd(account?.totalUsd)} `
            + `(${fmtUsd(alltimePnl, { sign: true })} since I started). `
            + `Free Polymarket US trading app by @YuhgoSlavia · krypt.cc/tools/polyus`}
        />
        <ShareableStat
          label="Session P&L"
          value={fmtUsd(sessionPnl, { sign: true })}
          hint={
            account?.balanceSyncing
              ? '⏳ syncing — a fill/settlement is landing; this number self-corrects in a moment'
              : sessionBaseline
                ? `${fmtUsd(sessionBaseline)} → ${fmtUsd(account?.totalUsd)} · ${fmtPct(sessionRoi)} · since ${fmtRelative(sessionStartedAt)}`
                : 'session baseline pending'
          }
          accent={account?.balanceSyncing ? undefined : sessionPnl >= 0 ? 'good' : 'bad'}
          shareText={`This session on Krypt PolyBot US: ${sessionPnl >= 0 ? '+' : ''}${fmtUsd(sessionPnl)} `
            + `(${fmtPct(sessionRoi)} ROI). `
            + `Free Polymarket US trading app by @YuhgoSlavia · krypt.cc/tools/polyus`}
        />
        <StatCard
          label="Open Positions"
          value={`${openPos.length}`}
          hint={`${account?.pendingCount ?? 0} pending · ${account?.openCount ?? 0} filled`}
        />
      </div>

      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <ShareableStat
          label="All-time P&L"
          value={fmtUsd(alltimePnl, { sign: true })}
          hint={
            alltimeBaseline
              ? `${fmtUsd(alltimeBaseline)} → ${fmtUsd(account?.totalUsd)} · balance delta`
              : 'pending baseline'
          }
          accent={alltimePnl >= 0 ? 'good' : 'bad'}
          shareText={`All-time on Krypt PolyBot US: ${alltimePnl >= 0 ? '+' : ''}${fmtUsd(alltimePnl)} `
            + `(${fmtPct(account?.roiPct ?? 0)}). `
            + `Free Polymarket US trading app by @YuhgoSlavia · krypt.cc/tools/polyus`}
        />
        <ShareableStat
          label="Win Rate"
          value={(account?.wins ?? 0) + (account?.losses ?? 0) > 0
            ? `${(account?.winRate ?? 0).toFixed(1)}%`
            : '—'}
          hint={`${account?.wins ?? 0}W / ${account?.losses ?? 0}L · pos-derived realized ${fmtUsd(realizedPos, { sign: true })}`}
          shareText={`Krypt PolyBot US win rate: ${(account?.winRate ?? 0).toFixed(1)}% `
            + `(${account?.wins ?? 0}W / ${account?.losses ?? 0}L). `
            + `Free Polymarket US trading app by @YuhgoSlavia · krypt.cc/tools/polyus`}
        />
      </div>
      </>
      )}

      <div className="mt-6 grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2" header={
          <div className="flex items-center justify-between">
            <div>
              <div className="text-xs uppercase tracking-wider text-krypt-muted">Equity curve</div>
              <div className="mt-0.5 text-sm text-white">
                Last {Math.min(168, Math.floor(((Date.now() - new Date(series[0]?.at ?? Date.now()).getTime()) / 3600000) || 168))}h
              </div>
            </div>
            <div className="text-xs text-krypt-muted">
              {series.length} samples
            </div>
          </div>
        }>
          {series.length < 3 ? (
            <Empty
              title="Not enough data yet"
              description="The equity curve fills in as the bot runs. Come back in a few cycles."
            />
          ) : (
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={series}>
                  <defs>
                    <linearGradient id="fillGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#3B82F6" stopOpacity={0.55} />
                      <stop offset="100%" stopColor="#3B82F6" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis
                    dataKey="at"
                    hide
                  />
                  <YAxis
                    domain={['dataMin - 5', 'dataMax + 5']}
                    tickFormatter={(v) => `$${Math.round(v)}`}
                    width={50}
                    tick={{ fill: '#71717A', fontSize: 11 }}
                    axisLine={false}
                    tickLine={false}
                  />
                  <Tooltip
                    contentStyle={{
                      background: '#171722',
                      border: '1px solid rgba(255,255,255,0.1)',
                      borderRadius: 8,
                      fontSize: 12,
                    }}
                    formatter={(v: number) => [`$${v.toFixed(2)}`, 'Equity']}
                    labelFormatter={(v) => fmtRelative(String(v))}
                  />
                  <Area
                    type="monotone"
                    dataKey="totalUsd"
                    stroke="#3B82F6"
                    strokeWidth={2}
                    fill="url(#fillGrad)"
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </Card>

        <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Status</div>}>
          <div className="flex flex-col gap-2">
            {issues.map((i, idx) => (
              <div
                key={idx}
                className={cls(
                  'flex items-start gap-2 rounded-lg border p-2 text-xs',
                  i.tone === 'good' && 'border-krypt-win/30 bg-krypt-win/5 text-krypt-win',
                  i.tone === 'warn' && 'border-krypt-warn/30 bg-krypt-warn/5 text-krypt-warn',
                  i.tone === 'bad' && 'border-krypt-loss/30 bg-krypt-loss/5 text-krypt-loss',
                )}
              >
                {i.tone === 'good' ? (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                )}
                <div className="flex-1">{i.label}</div>
                {i.cta && (
                  <button
                    onClick={() => onNav(i.cta!.page)}
                    className="rounded-md border border-current px-2 py-0.5 text-[11px] hover:bg-current/10"
                  >
                    {i.cta.label}
                  </button>
                )}
              </div>
            ))}
          </div>
          <div className="mt-4">
            <WhyNotTrading />
          </div>
        </Card>
      </div>

      <div className="mt-6">
        <Card header={
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-krypt-pink" />
              <span className="text-sm text-white">Recent resolutions</span>
            </div>
            <button onClick={() => onNav('history')} className="text-xs text-krypt-muted hover:text-white">
              View all <ArrowRight className="ml-1 inline h-3 w-3" />
            </button>
          </div>
        }>
          {recentResolved.length === 0 ? (
            <Empty
              title="No resolved trades yet"
              description="Wins and losses show up here as markets settle."
            />
          ) : (
            <div className="flex flex-col divide-y divide-krypt-border">
              {recentResolved.map((p) => (
                <div key={p.id} className="flex items-center gap-3 py-2 text-sm">
                  {p.outcomeCorrect === 1 ? (
                    <TrendingUp className="h-4 w-4 text-krypt-win" />
                  ) : p.outcomeCorrect === 0 ? (
                    <TrendingDown className="h-4 w-4 text-krypt-loss" />
                  ) : (
                    <span className="h-4 w-4" />
                  )}
                  <span className="font-mono text-xs text-krypt-muted">{p.ticker}</span>
                  <span className="ml-auto truncate text-xs text-krypt-muted">{p.title}</span>
                  <span className={cls(
                    'min-w-[60px] text-right font-mono text-sm',
                    (p.pnlUsd ?? 0) >= 0 ? 'text-krypt-win' : 'text-krypt-loss',
                  )}>
                    {fmtUsd(p.pnlUsd, { sign: true })}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </Page>
  );
}
