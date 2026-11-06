import { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Sparkles, TrendingDown, TrendingUp } from 'lucide-react';
import { Area, AreaChart, Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { BotPosition, PnlPoint } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { Card, Page, ShareButton } from '../components/common';
import { GlassSegmented } from '../components/glass/GlassSegmented';
import { cls, fmtUsd } from '../utils/format';
import { HUB_VIEWS, loadHubView, saveHubView, type HubViewId } from '../hub/views';
import { hudNumbers, balanceOf } from '../hub/data';
import { EMPTY_POT, addPot, countPot } from '../hub/pot';
import { subject } from '../hub/text';

// The Agent Hub (page id 'visualizer', kept so routing and the remembered page
// still work). Every view (Casino, High Stakes, Race Track) is a lazy
// three.js scene in its own chunk; this page only frames it, so three.js is
// not in the startup bundle. Nothing here runs a render loop or grows a
// per-event structure: the old 2D canvas kept an orb list that only its own
// loop culled. The pots below count settlements directly (hub/pot.ts).
//
// Read-only like the scenes: the one backend call on this page is the equity
// series it already showed (`data.pnlSeries`, a local SQLite read).

/** A count the backend fills with 0 when it has nothing is shown as a count;
 *  a RATE over nothing is not 0%, it's absent. */
function rate(v: number | null | undefined, n: number | null | undefined): string {
  return typeof n === 'number' && n > 0 ? pct(v) : '—';
}

function pct(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? `${v.toFixed(1)}%` : '—';
}

function count(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString('en-US') : '—';
}

/** One colour per order source, matching the scenes' crew colours. */
const SOURCE_COLOR: Record<string, string> = {
  crypto: '#38BDF8',
  script: '#A78BFA',
  external: '#F59E0B',
};

function sourceColor(s: string | null | undefined): string {
  return SOURCE_COLOR[(s ?? '').startsWith('script:') ? 'script' : s ?? ''] ?? '#94A3B8';
}

const TOOLTIP = {
  labelStyle: { color: '#A1A1AA' },
  itemStyle: { color: '#FFFFFF' },
  contentStyle: { background: '#171722', border: '1px solid #ffffff14', borderRadius: 6, fontSize: 11 },
};

export function VisualizerPage() {
  const { positions, account } = useApp();
  const [view, setViewState] = useState<HubViewId>(loadHubView);
  const setView = (v: HubViewId) => { setViewState(v); saveHubView(v); };
  const hubView = HUB_VIEWS.find((v) => v.id === view) ?? HUB_VIEWS[0];
  const Scene = hubView.scene;
  const hud = hudNumbers(account);

  const seenPosRef = useRef<Map<number, boolean>>(new Map());
  const [pot, setPot] = useState(EMPTY_POT);
  const sessionRunId = account?.sessionRunId ?? 0;
  const lastRunIdRef = useRef<number>(sessionRunId);
  useEffect(() => {
    if (sessionRunId !== lastRunIdRef.current) {
      lastRunIdRef.current = sessionRunId;
      setPot(EMPTY_POT);
      seenPosRef.current = new Map();
    }
  }, [sessionRunId]);

  const [series, setSeries] = useState<PnlPoint[]>([]);
  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const s = await window.krypt.data.pnlSeries(24);
        if (mounted) setSeries(s);
      } catch { /* backend down: the card says "collecting data…" */ }
    };
    void load();
    const i = window.setInterval(load, 30_000);
    return () => { mounted = false; window.clearInterval(i); };
  }, []);

  const hourlyBars = useMemo(() => {
    const now = Date.now();
    const bins: { hour: string; wins: number; losses: number; pnl: number }[] = [];
    for (let h = 11; h >= 0; h--) {
      const t = new Date(now - h * 3600_000);
      bins.push({ hour: `${t.getHours()}h`, wins: 0, losses: 0, pnl: 0 });
    }
    for (const p of positions) {
      if (!p.resolved || !p.resolvedAt) continue;
      const ts = new Date(p.resolvedAt).getTime();
      const hoursAgo = Math.floor((now - ts) / 3600_000);
      if (hoursAgo < 0 || hoursAgo > 11) continue;
      const slot = bins[11 - hoursAgo];
      if (!slot) continue;
      if (p.outcomeCorrect === 1) slot.wins += 1;
      else if (p.outcomeCorrect === 0) slot.losses += 1;
      slot.pnl += p.pnlUsd ?? 0;
    }
    return bins;
  }, [positions]);

  const hotMarkets = useMemo(() => {
    const cutoff = Date.now() - 24 * 3600_000;
    const acc = new Map<string, { wins: number; losses: number; pnl: number; title: string }>();
    for (const p of positions) {
      if (!p.resolved || !p.resolvedAt) continue;
      if (new Date(p.resolvedAt).getTime() < cutoff) continue;
      const cur = acc.get(p.ticker) || { wins: 0, losses: 0, pnl: 0, title: p.title };
      if (p.outcomeCorrect === 1) cur.wins += 1;
      else if (p.outcomeCorrect === 0) cur.losses += 1;
      cur.pnl += p.pnlUsd ?? 0;
      acc.set(p.ticker, cur);
    }
    return Array.from(acc.entries())
      .map(([id, v]) => ({ id, ...v }))
      .sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl))
      .slice(0, 6);
  }, [positions]);

  // The pots: every settlement THIS SESSION, counted once (hub/pot.ts).
  const sessionStartedAt = account?.sessionStartedAt;
  useEffect(() => {
    const t0 = sessionStartedAt ? Date.parse(sessionStartedAt) : NaN;
    const d = countPot(seenPosRef.current, positions, Number.isFinite(t0) ? t0 : null);
    if (d.wins || d.losses) setPot((prev) => addPot(prev, d));
  }, [positions, sessionStartedAt]);

  const sessionSeries = useMemo(() => {
    if (!sessionStartedAt) return series;
    const t0 = new Date(sessionStartedAt).getTime();
    const filtered = series.filter((p) => new Date(p.at).getTime() >= t0 - 60_000);
    return filtered.length >= 2 ? filtered : series;
  }, [series, sessionStartedAt]);

  // Glass segmented switch: the lens glides between views over the labels.
  const viewToggle = (
    <GlassSegmented
      value={view}
      onChange={setView}
      lensTint="linear-gradient(90deg, rgba(29,78,216,0.42), rgba(59,130,246,0.36) 50%, rgba(56,189,248,0.42))"
      options={HUB_VIEWS.map((v) => ({
        value: v.id, label: v.label, icon: <v.icon className="h-3.5 w-3.5" />,
      }))}
    />
  );

  return (
    <Page
      title="Agent Hub"
      subtitle={hubView.blurb}
      actions={
        <>
          {viewToggle}
          <ShareButton
            size="xs"
            text={
              `Krypt PolyBot US session${hud.pnl !== null ? ` P&L: ${fmtUsd(hud.pnl, { sign: true })}` : ''} `
              + `(${pot.wins}W / ${pot.losses}L). `
              + 'Free Polymarket US auto-trader by @YuhgoSlavia · krypt.cc/tools/polyus'
            }
          />
        </>
      }
    >
      <div className="space-y-4">
        <Suspense
          fallback={
            <div className="grid h-[calc(100vh-250px)] min-h-[460px] place-items-center rounded-2xl border border-krypt-border bg-[#03050c]">
              <div className="animate-pulse font-pixel text-xs text-krypt-purple">Loading {hubView.label}…</div>
            </div>
          }
        >
          {/* keyed by view: switching unmounts the old scene, which disposes
              its engine and hands back its WebGL context */}
          <Scene key={view} />
        </Suspense>

        <div className="grid grid-cols-2 gap-2">
          <BucketTile tone="good" label="Wins pot · this session" count={pot.wins} pnl={pot.winPnl} icon={TrendingUp} />
          <BucketTile tone="bad" label="Losses pot · this session" count={pot.losses} pnl={pot.lossPnl} icon={TrendingDown} />
        </div>

        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Equity (session)</div>}>
            <div className="font-mono text-lg text-white">{fmtUsd(balanceOf(account))}</div>
            <div className={cls(
              'text-xs',
              hud.pnl === null ? 'text-krypt-dim' : hud.pnl >= 0 ? 'text-krypt-win' : 'text-krypt-loss',
            )}>
              {fmtUsd(hud.pnl, { sign: true })} session &middot;{' '}
              <span className="text-krypt-dim">{hud.roi === null ? '—' : `${hud.roi >= 0 ? '+' : ''}${hud.roi.toFixed(2)}%`}</span>
            </div>
            <div className="mt-2 h-20">
              {sessionSeries.length < 3 ? (
                <div className="grid h-full place-items-center text-[11px] text-krypt-dim">collecting data…</div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={sessionSeries}>
                    <defs>
                      <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#3B82F6" stopOpacity={0.7} />
                        <stop offset="100%" stopColor="#3B82F6" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <Tooltip
                      {...TOOLTIP}
                      formatter={(v: number) => [`$${v.toFixed(2)}`, 'Equity']}
                      labelFormatter={() => ''}
                    />
                    <Area type="monotone" dataKey="totalUsd" stroke="#3B82F6" strokeWidth={1.5} fill="url(#sparkGrad)" />
                  </AreaChart>
                </ResponsiveContainer>
              )}
            </div>
          </Card>

          <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">P&amp;L per hour (12h)</div>}>
            <div className="h-24">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={hourlyBars}>
                  <Tooltip
                    {...TOOLTIP}
                    formatter={(v: number, n: string) => {
                      if (n === 'pnl') return [`$${v.toFixed(2)}`, 'pnl'];
                      return [v, n];
                    }}
                  />
                  <XAxis dataKey="hour" hide />
                  <YAxis hide />
                  <Bar dataKey="pnl" fill="#3B82F6" radius={[2, 2, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-2 grid grid-cols-2 gap-2 text-[11px] text-krypt-muted">
              <Mini label="Today wins" value={count(account?.todayWins)} tone="good" />
              <Mini label="Today losses" value={count(account?.todayLosses)} tone="bad" />
              <Mini label="Win rate" value={pct(hud.winRate)} />
              <Mini label="Open" value={count(hud.open)} />
            </div>
          </Card>

        </div>
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted"><Sparkles className="mr-1 inline h-3 w-3" />Latest trades</div>}>
          {positions.length === 0 && <div className="py-3 text-center text-[11px] text-krypt-dim">no trades yet</div>}
          {positions.slice(0, 8).map((p) => (
            <TradeRow key={p.id} p={p} />
          ))}
        </Card>
      </div>
    </Page>
  );
}

function Mini({ label, value, tone }: { label: string; value: string; tone?: 'good' | 'bad' | 'purple' | 'pink' }) {
  const cn = (() => {
    switch (tone) {
      case 'good': return 'text-krypt-win';
      case 'bad': return 'text-krypt-loss';
      case 'purple': return 'text-krypt-purple';
      case 'pink': return 'text-krypt-pink';
      default: return 'text-white';
    }
  })();
  return (
    <div className="rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-krypt-muted">{label}</div>
      <div className={cls('font-mono text-sm', cn)}>{value}</div>
    </div>
  );
}

interface BucketTileProps {
  tone: 'good' | 'bad';
  label: string;
  count: number;
  pnl: number;
  icon: React.ComponentType<{ className?: string }>;
}

function BucketTile({ tone, label, count: n, pnl, icon: Icon }: BucketTileProps) {
  const bg = tone === 'good'
    ? 'border-krypt-win/50 bg-krypt-win/10 text-krypt-win'
    : 'border-krypt-loss/50 bg-krypt-loss/10 text-krypt-loss';
  return (
    <div className={cls('flex items-center gap-3 rounded-xl border px-4 py-3 backdrop-blur', bg)}>
      <div className={cls(
        'grid h-9 w-9 place-items-center rounded-lg',
        tone === 'good' ? 'bg-krypt-win/20' : 'bg-krypt-loss/20',
      )}>
        <Icon className="h-4 w-4" />
      </div>
      <div className="flex-1">
        <div className="text-[10px] uppercase tracking-wider opacity-70">{label}</div>
        <div className="font-mono text-base">{n} · {fmtUsd(pnl, { sign: true })}</div>
      </div>
    </div>
  );
}

function TradeRow({ p }: { p: BotPosition }) {
  const tone = p.resolved
    ? p.outcomeCorrect === 1 ? 'text-krypt-win'
    : p.outcomeCorrect === 0 ? 'text-krypt-loss'
    : 'text-krypt-muted'
    : 'text-krypt-muted';
  return (
    <div className="flex items-center gap-2 border-t border-krypt-border py-1.5 text-xs first:border-t-0">
      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: sourceColor(p.signalSource) }} title={p.signalSource} />
      <span className="truncate text-krypt-muted" title={p.title}>{subject(p.ticker, p.title)}</span>
      <span className={cls('ml-auto min-w-[60px] text-right font-mono', tone)}>
        {p.resolved ? fmtUsd(p.pnlUsd, { sign: true }) : '—'}
      </span>
    </div>
  );
}
