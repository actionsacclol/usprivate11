import { useEffect, useState } from 'react';
import { FlaskConical, Play } from 'lucide-react';
import {
  Area, AreaChart, Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import {
  C15_PRESET_CORE, intervalDataWarning, noDataHeadline, replayInterval,
} from '@shared/c15Presets';
import type { CollectionStats, Crypto15mBacktest, TraderConfig } from '@shared/types';
import { Card, Page, Switch } from '../components/common';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';
import { HistoryDownload } from '../components/HistoryDownload';
import { TooFewTrades } from '../components/TooFewTrades';
import { historyEstimate, startHistoryDownload } from '../utils/historyDownload';

/** Crypto strategy presets replayable WITHOUT touching the live config — the
 * backtest RPC merges these over current settings for the run only. The
 * replay-relevant core of each strategy is shared with the Crypto tab presets
 * (shared/c15Presets.ts) — retune numbers there; only page-specific extras and
 * intentional divergences live here. */
const C15_STRATS: { id: string; name: string; patch: Partial<TraderConfig> }[] = [
  { id: 'current', name: 'My current settings', patch: {} },
  {
    id: 'sniper', name: 'Settlement Sniper',
    patch: {
      ...C15_PRESET_CORE.sniper,
      // INTENTIONALLY DIVERGENT from the Crypto tab preset, which drifted to
      // modelMinProb 0.9985 / delay 2 / entryMax 0.99 — this page kept the
      // looser replay numbers. Unifying them is a behavior change; don't.
      crypto15mModelMinProb: 0.97, crypto15mTimeDelayMin: 5, crypto15mEntryMax: 0.97,
    },
  },
  {
    // Replays against the 5-MINUTE dataset only (crypto15mInterval in the
    // patch selects which recorded window series the backtest fills against).
    id: 'sniper-5m', name: '5m Sniper (BTC)',
    patch: { ...C15_PRESET_CORE['sniper-5m'] },
  },
  {
    id: 'favorite', name: 'Deep Favorite',
    // timeDelayMin is Backtest-only (the Crypto tab preset never set it).
    patch: { ...C15_PRESET_CORE.favorite, crypto15mTimeDelayMin: 6 },
  },
  {
    id: 'contrarian', name: 'Contrarian fade',
    // timeDelayMin is Backtest-only (the Crypto tab preset never set it).
    patch: { ...C15_PRESET_CORE.contrarian, crypto15mTimeDelayMin: 8 },
  },
];

const WINDOWS = [7, 14, 30, 60];

export function BacktestPage() {
  const [stratSel, setStratSel] = useState('current');
  const [days, setDays] = useState(30);
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<Crypto15mBacktest | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { config, state, backend } = useApp();
  const profiles = state?.customProfiles ?? [];
  const [coll, setColl] = useState<CollectionStats | null>(null);
  const [showData, setShowData] = useState(false);
  const [historyKey, setHistoryKey] = useState(0);

  const loadCollection = async () => {
    try {
      const c = await window.krypt.trading.collection();
      if (c) setColl(c);
    } catch { /* backend down */ }
  };
  // Again once the engine is up: the app can reopen on this page before it is.
  const backendUp = backend?.status === 'running';
  useEffect(() => { void loadCollection(); }, [backendUp]);

  const toggleC15Collection = async (on: boolean) => {
    await window.krypt.config.update({ crypto15mRecordSignals: on });
    void loadCollection();
  };

  const [exporting, setExporting] = useState(false);
  const exportData = async () => {
    setExporting(true);
    try {
      await window.krypt.trading.exportData();
    } finally {
      setExporting(false);
    }
  };

  const sliceProfile = (cfg: TraderConfig): Partial<TraderConfig> => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(cfg)) {
      if (k.startsWith('crypto15m')) out[k] = v;
    }
    // Never let a replayed profile arm anything.
    delete out.crypto15mEnabled;
    return out as Partial<TraderConfig>;
  };

  const resolvePatch = (): Partial<TraderConfig> => {
    if (stratSel.startsWith('profile:')) {
      const prof = profiles.find((pr) => pr.id === stratSel.slice(8));
      return prof ? sliceProfile(prof.config) : {};
    }
    if (stratSel.startsWith('preset:')) {
      return C15_STRATS.find((st) => st.id === stratSel.slice(7))?.patch ?? {};
    }
    return {}; // current settings
  };

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await window.krypt.crypto15m.backtest({ sinceDays: days, config: resolvePatch() });
      setRes(r);
      if (!r) setErr('Engine not running — start the app backend first.');
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
      void loadCollection();
    }
  };

  // Which recorded window series each crypto choice replays, and whether any
  // of it exists. The recorder only records the interval selected on the
  // Crypto tab, so on a machine recording 5m every 15m preset has nothing to
  // replay — say so BEFORE the run, not as a 0-trade "result" after it.
  // The recorder's interval IS the live setting, so it stands in until the
  // config has loaded (else "current settings" would read as 15m).
  const liveInterval = config?.crypto15mInterval ?? coll?.c15.recordingInterval;
  const byInterval = coll?.c15.byInterval;
  const c15OptionLabel = (name: string, patch: Partial<TraderConfig>) => {
    const iv = replayInterval(patch, liveInterval);
    const empty = byInterval ? (byInterval[iv]?.resolved ?? 0) === 0 : false;
    return `${name} · ${iv}${empty ? ' (no data recorded)' : ''}`;
  };
  const pickedInterval = replayInterval(resolvePatch(), liveInterval);
  const c15DataWarning = intervalDataWarning(
    pickedInterval,
    coll?.c15,
    config?.crypto15mRecordSignals ?? true,
  );

  return (
    <Page
      title="Backtest"
      subtitle="Test a strategy on past crypto up/down markets, with the same entry rules as live and Polymarket US fees, and see WHEN a result happens, not just how big it is. Download the history below. The app also records the interval picked on the Crypto tab while it's open."
    >
      <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Setup</div>}>
        <div className="flex flex-wrap items-end gap-4">
          <Field label="Strategy / profile">
            <select
              value={stratSel}
              onChange={(e) => setStratSel(e.target.value)}
              className="rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5 text-xs text-white outline-none focus:border-krypt-purple/60"
            >
              <option value="current">{c15OptionLabel('Current settings', {})}</option>
              <optgroup label="Presets">
                {C15_STRATS.filter((st) => st.id !== 'current').map((st) => (
                  <option key={st.id} value={`preset:${st.id}`}>{c15OptionLabel(st.name, st.patch)}</option>
                ))}
              </optgroup>
              {profiles.length > 0 && (
                <optgroup label="My profiles">
                  {profiles.map((pr) => (
                    <option key={pr.id} value={`profile:${pr.id}`}>
                      {c15OptionLabel(pr.name, sliceProfile(pr.config))}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          </Field>
          <Field label="Window">
            <Chips
              options={WINDOWS.map((d) => [String(d), `${d}d`] as [string, string])}
              value={String(days)}
              onPick={(v) => setDays(Number(v))}
            />
          </Field>
          <button
            onClick={() => void run()}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-4 py-2 text-xs font-semibold text-krypt-purple transition-colors hover:bg-krypt-purple/20 disabled:opacity-50"
          >
            {busy ? <FlaskConical className="h-3.5 w-3.5 animate-pulse" /> : <Play className="h-3.5 w-3.5" />}
            {busy ? 'Replaying…' : 'Run backtest'}
          </button>
        </div>
        {c15DataWarning && (
          <>
            <p className="mt-2 text-xs leading-relaxed text-krypt-warn">{c15DataWarning}</p>
            <button
              type="button"
              data-testid="history-download-interval"
              onClick={() => {
                void startHistoryDownload(7, [pickedInterval]).then(() => setHistoryKey((k) => k + 1));
              }}
              className="mt-2 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-3 py-1.5 text-xs font-semibold text-krypt-purple transition-colors hover:bg-krypt-purple/20"
            >
              Or download 7 days of {pickedInterval} markets now · about {historyEstimate(7, [pickedInterval])}
            </button>
          </>
        )}
        {err && <p className="mt-2 text-xs text-krypt-loss">{err}</p>}
      </Card>

      <div className="mt-4">
        <Card header={<div className="text-xs uppercase tracking-wider text-krypt-muted">Data</div>}>
          <HistoryDownload refreshKey={historyKey} onChange={() => void loadCollection()} />
          <div className="mt-3 rounded-lg bg-krypt-surface2/50 p-3">
            <Switch
              checked={config?.crypto15mRecordSignals ?? true}
              onChange={(v) => void toggleC15Collection(v)}
              label="Collect crypto up/down data"
              description="Records every watched window's ticks and outcome while the app is open — no trading, no orders, works with the crypto executor fully off. This is the dataset crypto backtests run on."
            />
            <p className="mt-1.5 text-[11px] text-krypt-dim">
              {coll ? `${coll.c15.windows.toLocaleString()} windows · ${coll.c15.ticks.toLocaleString()} ticks` : '…'}
              {coll?.c15.downloaded ? ` · ${coll.c15.downloaded.toLocaleString()} windows downloaded` : ''}
              {coll?.c15.lastAt ? ` · last ${coll.c15.lastAt.slice(5, 16)} UTC` : ''}
            </p>
          </div>
          <button
            onClick={() => void exportData()}
            disabled={exporting}
            className="mr-2 mt-3 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim transition-colors hover:text-white disabled:opacity-50"
          >
            {exporting ? 'Exporting…' : 'Export CSV (all collected data)'}
          </button>
          <button
            onClick={() => setShowData((v) => !v)}
            className="mt-3 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-dim transition-colors hover:text-white"
          >
            {showData ? 'Hide collected data' : 'View collected data'}
          </button>
          {showData && coll && (
            <div className="mt-3">
              <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">Latest crypto windows</div>
              <table className="w-full text-[11px]">
                <thead><tr className="text-left text-krypt-dim">
                  <th className="py-0.5 pr-2 font-normal">Asset</th>
                  <th className="py-0.5 pr-2 font-normal">Closes</th>
                  <th className="py-0.5 pr-2 font-normal">Favorite</th>
                  <th className="py-0.5 font-normal">Result</th>
                </tr></thead>
                <tbody>
                  {coll.c15.recent.map((r) => (
                    <tr key={r.ticker} className="border-t border-krypt-border/50">
                      <td className="py-1 pr-2 font-mono text-white">{r.asset}</td>
                      <td className="py-1 pr-2 text-krypt-dim">{r.close_time?.slice(5, 16)}</td>
                      <td className="py-1 pr-2 text-krypt-dim">
                        {r.favorite ?? '—'}{r.favorite_price != null ? ` @ ${Math.round(r.favorite_price * 100)}¢` : ''}
                      </td>
                      <td className="py-1">
                        {!r.resolved ? <span className="text-krypt-dim">open</span>
                          : r.up_won ? <span className="text-krypt-win">UP won</span>
                            : <span className="text-krypt-loss">DOWN won</span>}
                      </td>
                    </tr>
                  ))}
                  {coll.c15.recent.length === 0 && (
                    <tr><td colSpan={4} className="py-2 text-krypt-dim">Nothing yet. Download past prices above, or leave the app open with collection on.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {res && res.windowsScanned === 0 && (
        <Card className="mt-4">
          <div className="py-4 text-center">
            {/* Nothing was replayable: lead with the cause, never with a
                0-trade number. The backend puts the specific reason first in
                caveats (and, on current backends, in noData). */}
            <div className="text-sm font-semibold text-white">{noDataHeadline(res.noData)}</div>
            {res.noData?.recordingInterval && res.noData.reason !== 'assets' && (
              <div className="mt-1.5 flex flex-wrap justify-center gap-1.5">
                <span className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  strategy needs {res.noData.interval}
                </span>
                <span className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {res.noData.recordingOn === false ? 'collection off' : `recording ${res.noData.recordingInterval}`}
                </span>
              </div>
            )}
            {res.caveats?.length ? (
              <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-krypt-dim">{res.caveats[0]}</p>
            ) : (
              <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-krypt-dim">
                There are no past markets to test on yet. Download some under Data above: it takes
                about 15 minutes for 7 days. The app also records the interval selected on the Crypto
                tab while it&apos;s open, no trading needed.
              </p>
            )}
          </div>
        </Card>
      )}
      {res && res.windowsScanned > 0 && (
        <>
          {res.interval && (
            <div className="mt-4">
              <span className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                replayed {res.interval} windows
              </span>
            </div>
          )}
          <TooFewTrades n={res.n} />
          <div className="mt-4 grid grid-cols-2 gap-2 text-xs sm:grid-cols-6">
            <Stat label="Trades" value={`${res.n}`} sub={`${res.windowsScanned} windows scanned`} />
            <Stat label="Win rate" value={res.n ? `${(res.winRate * 100).toFixed(1)}%` : '—'} />
            {/* No trades = no edge to show: a green "0.00¢" reads as a verdict. */}
            <Stat label="Edge / contract" value={res.n ? `${res.netEvCentsPerContract.toFixed(2)}¢` : '—'} tone={!res.n ? undefined : res.netEvCentsPerContract >= 0 ? 'good' : 'bad'} />
            <Stat label="Total P&L" value={res.n ? fmtUsd(res.totalPnlUsd, { sign: true }) : '—'} tone={!res.n ? undefined : res.totalPnlUsd >= 0 ? 'good' : 'bad'} />
            <Stat label="Max drawdown" value={fmtUsd(res.maxDrawdownUsd)} tone="bad" />
            <Stat label="Days traded" value={`${res.byDay.length}`} />
          </div>

          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <Card header={<ChartHead title="Equity over time" hint="Cumulative P&L in trade order — a healthy strategy climbs steadily; one big lucky step means one event carried it." />}>
              <div className="h-48">
                <ResponsiveContainer>
                  <AreaChart data={res.equity.map((e, i) => ({ i, at: e.at ? e.at.slice(5, 16) : String(i), value: e.value }))}>
                    <XAxis dataKey="at" tick={{ fontSize: 9 }} minTickGap={40} stroke="#555" />
                    <YAxis tick={{ fontSize: 9 }} width={40} stroke="#555" />
                    <Tooltip contentStyle={{ background: '#111', border: '1px solid #333', fontSize: 11 }} labelStyle={{ color: '#FFFFFF' }} itemStyle={{ color: '#FFFFFF' }} formatter={(v: number) => fmtUsd(v, { sign: true })} />
                    <Area dataKey="value" stroke="#a78bfa" fill="#a78bfa22" strokeWidth={1.5} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </Card>

            <Card header={<ChartHead title="P&L by hour of day (UTC)" hint="The 'first 12 hours print, last 12 lose' check — green bars earn, red bars bleed. Thin bars (low n) are noise, not signal." />}>
              <div className="h-48">
                <ResponsiveContainer>
                  <BarChart data={res.byHourUtc}>
                    <XAxis dataKey="hour" tick={{ fontSize: 9 }} stroke="#555" />
                    <YAxis tick={{ fontSize: 9 }} width={40} stroke="#555" />
                    <Tooltip
                      contentStyle={{ background: '#111', border: '1px solid #333', fontSize: 11 }} labelStyle={{ color: '#FFFFFF' }} itemStyle={{ color: '#FFFFFF' }}
                      formatter={(v: number, _n, item: any) => [`${fmtUsd(v, { sign: true })} (${item?.payload?.wins}/${item?.payload?.n})`, 'P&L']}
                    />
                    <Bar dataKey="pnlUsd">
                      {res.byHourUtc.map((b) => (
                        <Cell key={b.hour} fill={b.pnlUsd >= 0 ? '#34d399' : '#f87171'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          </div>

          <div className="mt-4">
            <Card header={<ChartHead title="P&L by day" hint="The 'prints one day, sucks the next' check — consistency across days matters more than the total." />}>
              <div className="h-40">
                <ResponsiveContainer>
                  <BarChart data={res.byDay.map((d) => ({ ...d, label: d.day.slice(5) }))}>
                    <XAxis dataKey="label" tick={{ fontSize: 9 }} stroke="#555" />
                    <YAxis tick={{ fontSize: 9 }} width={40} stroke="#555" />
                    <Tooltip
                      contentStyle={{ background: '#111', border: '1px solid #333', fontSize: 11 }} labelStyle={{ color: '#FFFFFF' }} itemStyle={{ color: '#FFFFFF' }}
                      formatter={(v: number, _n, item: any) => [`${fmtUsd(v, { sign: true })} (${item?.payload?.wins}/${item?.payload?.n})`, 'P&L']}
                    />
                    <Bar dataKey="pnlUsd">
                      {res.byDay.map((d) => (
                        <Cell key={d.day} fill={d.pnlUsd >= 0 ? '#34d399' : '#f87171'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          </div>

          {Object.keys(res.byAsset).length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {Object.entries(res.byAsset).map(([a, st]) => (
                <span key={a} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
                  {a} {st.wins}/{st.n} <span className={st.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>{fmtUsd(st.pnlUsd, { sign: true })}</span>
                </span>
              ))}
            </div>
          )}

          <ul className="mt-3 space-y-0.5">
            {res.caveats.map((c, i) => (
              <li key={i} className="text-[10px] leading-relaxed text-krypt-warn/80">⚠ {c}</li>
            ))}
          </ul>
        </>
      )}
    </Page>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wide text-krypt-dim">{label}</div>
      {children}
    </div>
  );
}

function Chips({ options, value, onPick }: {
  options: [string, string][]; value: string; onPick: (v: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {options.map(([v, label]) => (
        <button
          key={v}
          onClick={() => onPick(v)}
          className={cls(
            'rounded-md border px-2.5 py-1 text-xs transition-colors',
            v === value
              ? 'border-krypt-purple/60 bg-krypt-purple/15 text-krypt-purple'
              : 'border-krypt-border bg-krypt-surface2 text-krypt-dim hover:text-white',
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'good' | 'bad' }) {
  return (
    <div className="rounded-lg bg-krypt-surface2/60 p-2">
      <div className="text-[10px] uppercase tracking-wide text-krypt-dim">{label}</div>
      <div className={cls('font-mono text-sm', tone === 'good' ? 'text-krypt-win' : tone === 'bad' ? 'text-krypt-loss' : 'text-white')}>{value}</div>
      {sub && <div className="text-[10px] text-krypt-dim">{sub}</div>}
    </div>
  );
}

function ChartHead({ title, hint }: { title: string; hint: string }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wider text-krypt-muted">{title}</div>
      <div className="mt-0.5 text-[10px] normal-case tracking-normal text-krypt-dim">{hint}</div>
    </div>
  );
}
