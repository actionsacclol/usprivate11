import { useEffect, useRef, useState } from 'react';
import { Bitcoin, Coins, FolderPlus, RefreshCw, RotateCcw, SlidersHorizontal, Zap } from 'lucide-react';
import { C15_PRESET_CORE } from '@shared/c15Presets';
import type {
  Crypto15mAsset, Crypto15mPosition,
  Crypto15mSizing, Crypto15mSnapshot, Crypto15mStatus, RuleCondition, TraderConfig,
} from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Empty, NameDialog, Page, RuleBuilder, Switch } from '../components/common';
import { TickerLink } from '../components/PolymarketTicker';
import { BacktestPanel } from '../components/BacktestPanel';
import { cls, fmtUsd } from '../utils/format';

// Tab refresh cadence. Kept at/under the backend snapshot() cache TTL (3s) so a
// freshly-computed snapshot reaches the UI quickly. Polling faster than that TTL
// is near-free: the expensive market-data fan-out is gated by the snapshot
// cache, so extra polls are cache hits — they don't add upstream load.
const POLL_MS = 2500;

// Module-level cache so switching away and back to this tab shows the
// last-known snapshot/status INSTANTLY instead of remounting to a "Loading…"
// state. The backend executor runs independently of this tab, so the data is
// never stale-by-much — and showing it immediately avoids the false "armed but
// not connected — paper-trading" flash while the first fetch is in flight.
let cachedSnap: Crypto15mSnapshot | null = null;
let cachedStatus: Crypto15mStatus | null = null;

function fmtSpot(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const dp = v >= 100 ? 2 : v >= 1 ? 4 : 6;
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: dp })}`;
}

function fmtDelta(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const dp = v >= 100 ? 2 : v >= 1 ? 3 : 5;
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: dp })}`;
}

function fmtMins(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${v.toFixed(1)}m`;
}

function pct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${Math.round(v * 100)}%`;
}

export function Crypto15mPage() {
  const { config } = useApp();
  const [snap, setSnap] = useState<Crypto15mSnapshot | null>(cachedSnap);
  const [status, setStatus] = useState<Crypto15mStatus | null>(cachedStatus);
  const [loading, setLoading] = useState(cachedSnap === null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);
  // In-flight guard: a stalled backend must not stack poll requests.
  const inFlight = useRef(false);

  async function load() {
    const api = window.krypt?.crypto15m;
    if (!api) {
      setErr('15m crypto API unavailable (restart the app after this update).');
      setLoading(false);
      return;
    }
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const [s, st] = await Promise.all([api.snapshot(), api.status()]);
      cachedSnap = s;
      cachedStatus = st;
      setSnap(s);
      setStatus(st);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    timer.current = window.setInterval(() => void load(), POLL_MS) as unknown as number;
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, []);

  async function patchAndReload(patch: Partial<TraderConfig>) {
    setBusy(true);
    try {
      await window.krypt.config.update(patch);
      await load();
    } catch (e: any) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(next: boolean) {
    if (next && !window.confirm(
      'Enable the crypto executor?\n\nIn Live it places REAL orders from your Polymarket US '
      + 'account whenever an API key is saved. In Paper it sends nothing.',
    )) return;
    await patchAndReload({ crypto15mEnabled: next });
  }

  // Simple vs Advanced UI. Simple (the default for new users) shows presets +
  // the handful of money knobs; Advanced shows every setting. Persisted per
  // machine — it's a view preference, not trading config.
  const [uiMode, setUiMode] = useState<'simple' | 'advanced'>(
    () => (localStorage.getItem('c15UiMode') === 'advanced' ? 'advanced' : 'simple'),
  );
  const setMode = (m: 'simple' | 'advanced') => {
    setUiMode(m);
    localStorage.setItem('c15UiMode', m);
  };

  const live = snap?.assets.filter((a) => a.signal).length ?? 0;
  const enabled = !!config?.crypto15mEnabled;
  const authed = !!status?.authed;
  const trading = !!status?.trading;
  const mode: 'OFF' | 'LIVE' | 'WAITING' = !enabled ? 'OFF' : (trading ? 'LIVE' : 'WAITING');
  const openPos = status?.open ?? [];
  const recentPos = (status?.recent ?? []).filter((p) => p.resolved);

  return (
    <Page
      title="Crypto"
      subtitle="Polymarket US Up/Down markets on BTC, ETH, SOL and XRP: 5-minute, 15-minute and 1-hour windows. Each settles on the CF Benchmarks index (BRTI for BTC) as the 60-second average before the window boundary, against a strike the market publishes. Tune the entry window, favorite threshold, delta filter and stop-loss below."
      actions={
        <>
          <div className="inline-flex overflow-hidden rounded-md border border-krypt-border">
            {(['simple', 'advanced'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                title={m === 'simple'
                  ? 'Presets and the essential money knobs only'
                  : 'Every setting the engine has'}
                className={cls(
                  'px-3 py-1.5 text-xs capitalize transition-colors',
                  uiMode === m
                    ? 'bg-krypt-glow text-white'
                    : 'bg-krypt-surface2 text-krypt-muted hover:text-white',
                )}
              >
                {m}
              </button>
            ))}
          </div>
          <button
            onClick={() => void load()}
            className="inline-flex items-center gap-2 rounded-md border border-krypt-border bg-krypt-surface2 px-3 py-1.5 text-xs text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh
          </button>
        </>
      }
    >
      {/* ── Executor control ───────────────────────────────────── */}
      <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <div className="min-w-[260px] flex-1">
            <Switch
              checked={enabled}
              disabled={busy}
              onChange={(v) => void toggleEnabled(v)}
              label="Enable crypto executor"
              description={
                mode === 'LIVE'
                  ? 'LIVE — placing real orders on your Polymarket US account.'
                  : mode === 'WAITING'
                    ? 'Enabled — will trade once your API key is saved.'
                    : 'Off — monitor only.'
              }
            />
          </div>
          <ModePill mode={mode} />
          <div className="flex items-center gap-4 text-xs">
            <KV label="Size" value={`${status?.orderSize ?? 1}c`} />
            <KV label="Max open" value={`${status?.maxConcurrent ?? 7}`} />
            <KV label="Open" value={`${status?.stats.openCount ?? 0}`} />
            <KV label="W / L" value={`${status?.stats.wins ?? 0} / ${status?.stats.losses ?? 0}`} />
          </div>
        </div>
        {status && enabled && !authed && (
          <div className="mt-2 text-[11px] text-krypt-warn">
            Enabled, but Polymarket US isn't connected — add your API key on the
            <span className="text-krypt-muted"> API Key</span> page to start trading.
          </div>
        )}
        {enabled && status?.modelCalibration && !status.modelCalibration.ok && (
          <div className="mt-2 rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-[11px] leading-relaxed text-krypt-loss">
            ⛔ <span className="font-semibold">Model calibration degraded</span> — high-confidence
            calls hit {Math.round((status.modelCalibration.rate ?? 0) * 100)}% over the last{' '}
            {status.modelCalibration.n} windows, and the statistical floor on that record
            ({Math.round((status.modelCalibration.lb ?? 0) * 100)}%) is below the bar the sniper
            needs to stay armed. Model entries are auto-paused and resume as newer windows
            restore calibration.
          </div>
        )}
        {enabled && (status?.byStrategy?.length ?? 0) > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {status!.byStrategy!.map((st) => (
              <span key={st.strategy} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim" title={st.fees_usd > 0 ? `fees $${st.fees_usd.toFixed(2)}` : undefined}>
                {st.strategy} {st.wins}/{st.n}{' '}
                <span className={st.pnl_usd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
                  {st.pnl_usd >= 0 ? '+' : ''}${st.pnl_usd.toFixed(2)}
                </span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Circuit-breaker halt: the engine is deliberately paused (daily cap /
          lifetime loss). Without this banner the reason lived only in the
          Logs page and users reported the bot "mysteriously stopped". */}
      {status?.haltReason && (
        <div className="mb-4 rounded-lg border border-krypt-warn/50 bg-krypt-warn/10 px-3 py-2 text-xs text-krypt-warn">
          <span className="font-semibold">Engine paused by circuit-breaker: </span>
          {status.haltReason}
        </div>
      )}

      {/* ── Strategy settings (editable) + spot status ─────────── */}
      <StrategySettings
        config={config}
        liveSignals={live}
        spotSource={snap?.spotSource ?? 'coinbase'}
        spotOk={snap?.spotOk ?? true}
        hoursOk={snap?.hoursOk ?? true}
        sizing={status?.sizing ?? null}
        uiMode={uiMode}
      />

      {uiMode === 'advanced' && <Crypto15mRules config={config} />}

      {uiMode === 'advanced' && <BacktestPanel />}

      {err && (
        <div className="mb-4 rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
          {err}
        </div>
      )}

      <MarketControls config={config} busy={busy} onPatch={patchAndReload} />

      {/* Market-data outage banner: spot loads from a different host, so when
          EVERY card is market-less with a connectivity error it's the exchange's
          market data that's unreachable. One clear callout beats four red cards. */}
      {snap && snap.assets.length > 0
        && snap.assets.every((a) => !a.hasMarket)
        && snap.assets.some((a) => /unreachable|blocked/i.test(a.error || '')) && (
        <div className="mb-4 rounded-lg border border-krypt-warn/40 bg-krypt-warn/10 px-3 py-2 text-xs text-krypt-warn">
          <strong>Polymarket US market data is unreachable.</strong> Spot prices come from a
          different source, so they still show. Your account and funds are unaffected.
        </div>
      )}

      {!snap && loading ? (
        <Empty title="Loading crypto markets…" description="Fetching Polymarket US markets and spot prices." />
      ) : snap && snap.assets.length === 0 ? (
        <Empty title="No data" description="Could not load any crypto series." />
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {snap?.assets.map((a) => <AssetCard key={a.series} a={a} />)}
        </div>
      )}

      {/* ── Positions ──────────────────────────────────────────── */}
      {(openPos.length > 0 || recentPos.length > 0) && (
        <div className="mt-6 space-y-4">
          {openPos.length > 0 && <PositionsTable title="Open positions" rows={openPos} />}
          {recentPos.length > 0 && <PositionsTable title="Recent (resolved)" rows={recentPos.slice(0, 20)} />}
        </div>
      )}
    </Page>
  );
}

const C15_INTERVALS: Array<{ id: '5m' | '15m' | 'hourly'; label: string }> = [
  { id: '5m', label: '5 min' },
  { id: '15m', label: '15 min' },
  { id: 'hourly', label: '1 hour' },
];
const C15_ALL_ASSETS = ['BTC', 'ETH', 'SOL', 'XRP'];

function MarketControls({
  config, busy, onPatch,
}: {
  config?: TraderConfig | null;
  busy: boolean;
  onPatch: (p: Partial<TraderConfig>) => Promise<void> | void;
}) {
  const interval = config?.crypto15mInterval ?? '15m';
  const enabledAssets = config?.crypto15mAssets ?? C15_ALL_ASSETS;
  const toggleAsset = (s: string) => {
    const cur = config?.crypto15mAssets ?? [...C15_ALL_ASSETS];
    const next = cur.includes(s) ? cur.filter((x) => x !== s) : [...cur, s];
    void onPatch({ crypto15mAssets: next });
  };
  return (
    <div className="mb-4 flex flex-col gap-3 rounded-xl border border-krypt-border bg-krypt-surface p-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-center gap-2">
        <span className="text-[11px] uppercase tracking-wider text-krypt-dim">Window</span>
        <div className="inline-flex overflow-hidden rounded-lg border border-krypt-border">
          {C15_INTERVALS.map((iv) => (
            <button
              key={iv.id}
              disabled={busy}
              onClick={() => void onPatch({ crypto15mInterval: iv.id })}
              className={cls(
                'px-3 py-1 text-xs transition-colors',
                interval === iv.id
                  ? 'bg-krypt-glow text-white'
                  : 'bg-krypt-surface2 text-krypt-muted hover:text-white',
              )}
            >
              {iv.label}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="mr-1 text-[11px] uppercase tracking-wider text-krypt-dim">Trade</span>
        {C15_ALL_ASSETS.map((s) => (
          <button
            key={s}
            disabled={busy}
            onClick={() => toggleAsset(s)}
            className={cls(
              'rounded-md border px-2 py-0.5 text-[11px] font-semibold transition-colors',
              enabledAssets.includes(s)
                ? 'border-krypt-purple/40 bg-krypt-purple/10 text-white'
                : 'border-krypt-border bg-krypt-surface2 text-krypt-dim line-through',
            )}
            title={enabledAssets.includes(s) ? `Trading ${s} — click to disable` : `${s} disabled — click to enable`}
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

// Signal fields the crypto rule builder exposes (keys mirror
// config.C15_RULE_FIELDS and the crypto snapshot).
const C15_RULE_FIELDS = [
  { key: 'favoritePrice', label: 'Favorite price (0–1)', dflt: 0.9 },
  { key: 'entryCost', label: 'Entry cost (0–1)', dflt: 0.9 },
  { key: 'upProb', label: 'Up probability (0–1)', dflt: 0.5 },
  { key: 'bookImbalance', label: 'Order-flow imbalance (−1…1)', dflt: 0.3 },
  { key: 'arbEdgeCents', label: 'Arb edge (¢)', dflt: 2 },
  { key: 'deltaPct', label: 'Underlying move % / vol (0–1)', dflt: 0.0 },
  { key: 'minsLeft', label: 'Minutes to close', dflt: 8 },
  { key: 'hourUtc', label: 'Hour of day (UTC, 0–23)', dflt: 13 },
  { key: 'peersAgree', label: 'Peer agreement (0–1)', dflt: 0.6 },
  { key: 'marketBias', label: 'Market up-bias (−1…1)', dflt: 0.0 },
  { key: 'macd', label: 'MACD line (underlying, scales w/ price)', dflt: 0.0 },
  { key: 'macdSignal', label: 'MACD signal line (underlying)', dflt: 0.0 },
  { key: 'macdHist', label: 'MACD histogram (underlying)', dflt: 0.0 },
  { key: 'macdCross', label: 'MACD cross (+1 up / −1 down)', dflt: 1 },
  { key: 'rsi', label: 'RSI (underlying, 0–100)', dflt: 70 },
  { key: 'deltaSignedPct', label: 'Signed move from open (fraction)', dflt: 0.0 },
  { key: 'upAsk', label: 'Up token ask (0–1)', dflt: 0.9 },
  { key: 'downAsk', label: 'Down token ask (0–1)', dflt: 0.9 },
  { key: 'sigma1m', label: '1-min realized vol (fraction)', dflt: 0.001 },
  { key: 'modelProb', label: 'Model P(up) (0–1)', dflt: 0.97 },
  { key: 'edgeNetCents', label: 'Model net edge (¢)', dflt: 2 },
];

// Illustrative example rule-sets that wire the new underlying-MACD/RSI fields
// into a strategy. These are LEARNING TEMPLATES, not proven edges — nothing
// here is backtested yet. They're one-directional by nature: the rules decide
// WHEN to enter, but the side bought still follows "Direction mode" (favorite),
// so each pairs a side-selecting condition (upProb) with a momentum/RSI gate.
// Load one, watch it in the Crypto tab, and run the 15m backtest before trusting it.
const C15_EXAMPLE_STRATEGIES: {
  id: string; name: string; blurb: string; rules: RuleCondition[];
}[] = [
  {
    id: 'macd-up-momentum',
    name: 'MACD up-momentum (longs)',
    blurb:
      'Enter Up only when Up is favored (upProb ≥ 0.55) and the underlying’s 1-min MACD is bullish (histogram > 0), in the last 6 minutes. A trend-confirmation long. Best on the 15-min window (on 5-min, “≤6 min” covers the whole window — lower it). Use with Direction = favorite; lower Favorite≥ so the rule, not the floor, drives.',
    rules: [
      { field: 'upProb', op: '>=', value: 0.55 },
      { field: 'macdHist', op: '>', value: 0 },
      { field: 'minsLeft', op: '<=', value: 6 },
    ],
  },
  {
    id: 'fav-not-overbought',
    name: 'Strong favorite, not overbought',
    blurb:
      'Buy strong favorites (≥ 0.92) but skip when the underlying RSI is extended (≥ 78) — a crude exhaustion filter to avoid chasing a stretched move into settlement.',
    rules: [
      { field: 'favoritePrice', op: '>=', value: 0.92 },
      { field: 'rsi', op: '<=', value: 78 },
    ],
  },
];

function Crypto15mRules({ config }: { config?: TraderConfig | null }) {
  const [loading, setLoading] = useState<string | null>(null);
  const loadExample = async (ex: typeof C15_EXAMPLE_STRATEGIES[number]) => {
    setLoading(ex.id);
    try {
      await window.krypt.config.update({ crypto15mUseRules: true, crypto15mRules: ex.rules });
    } finally {
      setLoading(null);
    }
  };
  return (
    <div className="mb-4">
      <div className="mb-3 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3">
        <div className="mb-1 text-[11px] font-bold uppercase tracking-wider text-krypt-muted">
          Example strategies — learning templates
        </div>
        <p className="mb-2 text-xs text-krypt-dim">
          One-click rule-sets that show how to wire the underlying MACD/RSI signals into an entry.
          These are <span className="font-semibold text-krypt-warn">illustrations, not proven edges</span> —
          nothing here is backtested. Load one, watch it on the cards, then run the 15m backtest
          (<span className="font-mono">python backtest.py</span>) before trusting it with money.
        </p>
        <div className="flex flex-col gap-2">
          {C15_EXAMPLE_STRATEGIES.map((ex) => (
            <div key={ex.id} className="flex items-start justify-between gap-3 rounded-md border border-krypt-border bg-krypt-surface px-3 py-2">
              <div>
                <div className="text-sm font-semibold text-white">{ex.name}</div>
                <div className="text-xs text-krypt-dim">{ex.blurb}</div>
              </div>
              <button
                onClick={() => void loadExample(ex)}
                disabled={loading === ex.id}
                className="shrink-0 rounded-md border border-krypt-accent/50 bg-krypt-accent/10 px-3 py-1 text-xs font-semibold text-krypt-accent hover:bg-krypt-accent/20 disabled:opacity-50"
              >
                {loading === ex.id ? 'Loading…' : 'Load'}
              </button>
            </div>
          ))}
        </div>
      </div>
      <RuleBuilder
        config={config}
        fields={C15_RULE_FIELDS}
        useRulesKey="crypto15mUseRules"
        rulesKey="crypto15mRules"
        label="Custom entry rules (advanced)"
        description="Compose your own entry from any signal — every condition must pass (AND). Replaces the favorite/threshold gate; the side bought still follows Direction mode."
        tip={
          <>Tip: <span className="font-mono">Hour of day</span> gates by UTC time (e.g. ≥13 and &lt;21
          for the US session); <span className="font-mono">Peer agreement ≥ 0.6</span> trades only when
          the other coins lean the same way (momentum), <span className="font-mono">≤ 0.3</span> only when
          this one diverges; <span className="font-mono">MACD histogram &gt; 0</span> = bullish underlying
          momentum, <span className="font-mono">RSI</span> flags overbought/oversold.</>
        }
      />
    </div>
  );
}

function ModePill({ mode }: { mode: 'OFF' | 'WAITING' | 'LIVE' }) {
  const sty =
    mode === 'LIVE'
      ? 'border-krypt-loss/50 bg-krypt-loss/15 text-krypt-loss'
      : mode === 'WAITING'
        ? 'border-krypt-warn/50 bg-krypt-warn/15 text-krypt-warn'
        : 'border-krypt-border bg-krypt-surface2 text-krypt-muted';
  return (
    <span className={cls('rounded-full border px-3 py-1 text-[11px] font-bold uppercase tracking-wider', sty)}>
      {mode}
    </span>
  );
}

function KV({ label, value, accent }: { label: string; value: string; accent?: 'good' | 'bad' }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-krypt-dim">
      {label}
      <span className={cls('font-mono', accent === 'good' ? 'text-krypt-win' : accent === 'bad' ? 'text-krypt-loss' : 'text-white')}>
        {value}
      </span>
    </span>
  );
}

// ─── Editable strategy settings ────────────────────────────────────────

const C15_DEFAULTS = {
  directionMode: 'favorite' as 'favorite' | 'contrarian',
  timeDelayMin: 8,
  entryThreshold: 0.95,
  entryMax: 0.98,
  exitThreshold: 0.4,
  takeProfit: 0,
  stopLossPct: 0,
  takeProfitPct: 0,
  takeProfitTotal: 0,
  minRsi: 0,
  minMacdHist: 0,
  minDeltaPct: 0,
  entryDiff: 0.02,
  entryStyle: 'maker' as 'maker' | 'taker',
  makerCancelMin: 1,
  hoursStartUtc: 0,
  hoursEndUtc: 24,
  orderSize: 5,
  maxConcurrent: 7,
};

// The app's own floor on an automated crypto entry (not a Polymarket US rule).
// The bot refuses anything below this (rather than silently buying more than
// you set), so a configured size under 5 simply won't trade.
const MIN_CONTRACTS = 5;

// One-click starting points. None of them has been measured on this exchange:
// the original app's replay figures came from a different venue, with different
// fees and a different settlement index, so they are not shown here. Validate on
// the Backtest page as your own recording grows before arming anything.
// Replay-relevant cores are shared with the Backtest strategy list via
// shared/c15Presets.ts — retune numbers there; this file only adds
// execution-only knobs and the intentionally divergent sniper override.
const C15_PRESETS: { id: string; name: string; hint: string; patch: Partial<TraderConfig> }[] = [
  {
    id: 'sniper', name: 'Settlement Sniper',
    hint: 'Only strikes in the FINAL 2 MINUTES, when the spot-vs-strike model is at least 99.85% sure and the book still leaves at least 2¢ after the taker fee. Not measured on this exchange yet: the final 60 seconds are averaged into the settlement price, so the model is an estimate right up to the boundary. Raising "Model certainty" back down turns this into early-window model-following. Backtest it on your own recording first.',
    patch: {
      ...C15_PRESET_CORE.sniper,
      // INTENTIONALLY DIVERGENT from the Backtest sniper preset (0.97 / delay
      // 5 / entryMax 0.97): this tab shipped the tighter 3σ retune and the
      // two pages drifted. Unifying them is a behavior change; don't.
      crypto15mModelMinProb: 0.9985, crypto15mTimeDelayMin: 2, crypto15mEntryMax: 0.99,
      // Execution-only knobs (not part of the shared replay core):
      crypto15mIndicatorDetect: true,
      crypto15mSpotWs: true,
      crypto15mEntryStyle: 'taker', crypto15mExitThreshold: 0,
    },
  },
  {
    id: 'sniper-5m', name: '5m Sniper (BTC)',
    hint: 'Switches the engine to the 5-MINUTE BTC series (recording follows). In the final 2 minutes, when spot has moved at least 0.05% off the strike, buy the 90–99¢ favorite and hold to settlement. Not measured on this exchange yet, and at these prices one loss costs many wins. Let the recorder collect several days of 5m data and backtest this preset here BEFORE arming.',
    patch: {
      ...C15_PRESET_CORE['sniper-5m'],
      // Execution-only knobs (not part of the shared replay core):
      crypto15mExitThreshold: 0, crypto15mStopLossPct: 0, crypto15mTakeProfit: 0,
      crypto15mEntryStyle: 'taker', crypto15mTakerFak: true,
      crypto15mSpotWs: true,
    },
  },
  {
    id: 'favorite', name: 'Deep Favorite',
    hint: 'Buy 95–98¢ favorites, hold to settle. Not measured on this exchange yet. A high win rate can still lose once the fee and the rare full loss are counted. Kept as a baseline to experiment against, not a recommendation.',
    // Extras beyond the shared core are execution-only knobs.
    patch: { ...C15_PRESET_CORE.favorite, crypto15mExitThreshold: 0.4, crypto15mEntryStyle: 'taker' },
  },
  // 2026-07-28: pruned Contrarian Fade, Momentum (Δ-confirmed), and MACD
  // Trend from the quick-preset row. Their building blocks remain available in
  // Advanced mode (direction mode, Min move Δ, the rule builder) and the
  // Backtest page still replays the contrarian strategy for comparison.
  {
    id: 'fav-90-95', name: 'Favorite 90–95¢',
    hint: 'Buy a 90–95¢ favorite with a 40¢ stop. Not measured on this exchange yet. Kept for comparison; validate before arming.',
    patch: { crypto15mInterval: '15m', crypto15mDirectionMode: 'favorite', crypto15mEntryThreshold: 0.90, crypto15mEntryMax: 0.95, crypto15mExitThreshold: 0.4, crypto15mEntryStyle: 'taker', crypto15mUseRules: false },
  },
];

function StrategySettings({
  config, liveSignals, spotSource, spotOk, hoursOk, sizing, uiMode,
}: {
  config: TraderConfig | null;
  liveSignals: number;
  spotSource: string;
  spotOk: boolean;
  hoursOk: boolean;
  sizing: Crypto15mSizing | null;
  uiMode: 'simple' | 'advanced';
}) {
  const sizingMode = config?.crypto15mSizingMode ?? 'fixed';
  // The entry window can span the whole window of the watched series.
  const windowMaxMin = ({ '5m': 5, '15m': 15, hourly: 60 } as const)[
    config?.crypto15mInterval ?? '15m'] ?? 15;
  const [savingPreset, setSavingPreset] = useState<string | null>(null);
  const [saveProfileOpen, setSaveProfileOpen] = useState(false);
  const toast = useToast();
  const update = async (patch: Partial<TraderConfig>) => {
    try { await window.krypt.config.update(patch); } catch { /* surfaced elsewhere */ }
  };
  const saveProfile = async (name: string): Promise<void> => {
    setSaveProfileOpen(false);
    const r = await window.krypt.profiles.save(name);
    if (r.ok) toast.success(r.message || 'Saved crypto profile');
    else toast.error(r.message || 'Failed to save profile');
  };
  const num = (k: keyof TraderConfig, d: number) => {
    const v = config?.[k] as number | undefined;
    return typeof v === 'number' && !Number.isNaN(v) ? v : d;
  };
  const dir = (config?.crypto15mDirectionMode ?? C15_DEFAULTS.directionMode);
  const entryStyle = (config?.crypto15mEntryStyle ?? C15_DEFAULTS.entryStyle);

  const applyPreset = async (p: typeof C15_PRESETS[number]) => {
    setSavingPreset(p.id);
    try { await window.krypt.config.update(p.patch); } finally { setSavingPreset(null); }
  };

  const resetDefaults = () => void update({
    crypto15mDirectionMode: C15_DEFAULTS.directionMode,
    crypto15mTimeDelayMin: C15_DEFAULTS.timeDelayMin,
    crypto15mEntryThreshold: C15_DEFAULTS.entryThreshold,
    crypto15mEntryMax: C15_DEFAULTS.entryMax,
    crypto15mExitThreshold: C15_DEFAULTS.exitThreshold,
    crypto15mStopLossPct: C15_DEFAULTS.stopLossPct,
    crypto15mMinRsi: C15_DEFAULTS.minRsi,
    crypto15mMinMacdHist: C15_DEFAULTS.minMacdHist,
    crypto15mMinDeltaPct: C15_DEFAULTS.minDeltaPct,
    crypto15mEntryDiff: C15_DEFAULTS.entryDiff,
    crypto15mEntryStyle: C15_DEFAULTS.entryStyle,
    crypto15mMakerCancelMin: C15_DEFAULTS.makerCancelMin,
    crypto15mHoursStartUtc: C15_DEFAULTS.hoursStartUtc,
    crypto15mHoursEndUtc: C15_DEFAULTS.hoursEndUtc,
  });

  return (
    <div className="mb-4 rounded-xl border border-krypt-border bg-krypt-surface p-4">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-krypt-muted">
          <SlidersHorizontal className="h-3.5 w-3.5" /> Strategy settings
        </span>
        <span className="text-[11px] text-krypt-dim">changes apply live</span>
        <div className="ml-auto flex items-center gap-3">
          {liveSignals > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full bg-krypt-win/10 px-2 py-0.5 text-[11px] font-semibold text-krypt-win">
              <Zap className="h-3 w-3" /> {liveSignals} live signal{liveSignals === 1 ? '' : 's'}
            </span>
          )}
          {!hoursOk && (
            <span className="inline-flex items-center gap-1 rounded-full bg-krypt-warn/10 px-2 py-0.5 text-[11px] font-semibold text-krypt-warn">
              outside trading hours
            </span>
          )}
          <span className={cls('inline-flex items-center gap-1.5 text-[11px]', spotOk ? 'text-krypt-muted' : 'text-krypt-warn')}>
            <span className={cls('h-1.5 w-1.5 rounded-full', spotOk ? 'bg-krypt-win' : 'bg-krypt-warn')} />
            spot: {spotSource}{spotOk ? '' : ' (down)'}
          </span>
        </div>
      </div>

      {/* Quick presets */}
      <div className="mb-3 flex flex-wrap gap-2">
        {C15_PRESETS.map((p) => (
          <button
            key={p.id}
            onClick={() => void applyPreset(p)}
            disabled={savingPreset !== null}
            title={p.hint}
            className="rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white disabled:opacity-50"
          >
            {p.name}
          </button>
        ))}
        <button
          onClick={() => setSaveProfileOpen(true)}
          title="Save these crypto settings as a profile (Profiles → Crypto market)"
          className="ml-auto inline-flex items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-muted transition-colors hover:border-krypt-purple/40 hover:text-white"
        >
          <FolderPlus className="h-3 w-3" /> Save as profile
        </button>
        <button
          onClick={resetDefaults}
          className="inline-flex items-center gap-1 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-[11px] text-krypt-dim transition-colors hover:border-krypt-warn/40 hover:text-krypt-warn"
        >
          <RotateCcw className="h-3 w-3" /> Reset
        </button>
      </div>

      <NameDialog
        open={saveProfileOpen}
        title="Save crypto profile"
        label="Saves your current 15m-crypto settings as a profile (find it under Profiles → Crypto market)."
        placeholder="Profile name"
        confirmLabel="Save"
        onSubmit={(name) => void saveProfile(name)}
        onClose={() => setSaveProfileOpen(false)}
      />

      {config?.crypto15mUseRules && (
        <div className="mb-3 rounded-lg border border-krypt-warn/40 bg-krypt-warn/10 px-3 py-2 text-[11px] leading-relaxed text-krypt-warn">
          <span className="font-semibold">Custom entry rules are ON</span> — the conditions in
          “Custom entry rules (advanced)” decide entry. The <span className="font-mono">Entry window</span> and
          {' '}<span className="font-mono">Min move Δ</span> here are <span className="font-semibold">ignored</span>;
          {' '}<span className="font-mono">Favorite≥</span> and <span className="font-mono">Skip above</span> still apply
          as a safety floor/cap.
          {/* The off-switch lives HERE, not only in the (Advanced-only) rule
              builder: Simple is the default mode, so this banner used to tell
              a user to flip a toggle that their mode never renders. */}
          <button
            onClick={() => void update({ crypto15mUseRules: false })}
            className="ml-1.5 rounded border border-krypt-warn/50 px-1.5 py-0.5 font-semibold underline-offset-2 transition-colors hover:bg-krypt-warn/20"
          >
            Turn rules off
          </button>
          {' '}to trade by these knobs instead.
        </div>
      )}

      {uiMode === 'simple' && (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
            <SelectField
              label="Direction" value={dir}
              options={[['favorite', 'Favorite-follow'], ['contrarian', 'Contrarian fade'], ['model', 'Model — settlement sniper']]}
              hint="Buy the favorite, fade it, or let the spot-vs-open model call the side (needs Detect MACD/RSI + live spot feed on)."
              onCommit={(v) => void update({ crypto15mDirectionMode: v as 'favorite' | 'contrarian' | 'model' })}
            />
            <NumField
              label="Entry window" suffix="min" min={1} max={windowMaxMin} step={1}
              value={num('crypto15mTimeDelayMin', C15_DEFAULTS.timeDelayMin)}
              hint="Only act inside the last N minutes of the window."
              onCommit={(v) => void update({ crypto15mTimeDelayMin: v })}
            />
            <NumField
              label="Favorite ≥" suffix="¢" min={1} max={99} step={1}
              value={Math.round(num('crypto15mEntryThreshold', C15_DEFAULTS.entryThreshold) * 100)}
              hint="The favorite side must be at least this likely to enter."
              onCommit={(v) => void update({ crypto15mEntryThreshold: v / 100 })}
            />
            <NumField
              label="Skip above" suffix="¢" min={1} max={99} step={1}
              value={Math.round(num('crypto15mEntryMax', C15_DEFAULTS.entryMax) * 100)}
              hint="Don't pay more than this — too little room left to profit."
              onCommit={(v) => void update({ crypto15mEntryMax: v / 100 })}
            />
            <NumField
              label="Stop-loss" suffix="¢" min={0} max={99} step={1}
              value={Math.round(num('crypto15mExitThreshold', C15_DEFAULTS.exitThreshold) * 100)}
              hint="Executor sells if the held side falls to this price. 0 = hold to settlement."
              onCommit={(v) => void update({ crypto15mExitThreshold: v / 100 })}
            />
            <SelectField
              label="Bet size by" value={sizingMode}
              options={[['fixed', 'Fixed contracts'], ['balance_pct', '% of balance']]}
              hint="Buy a fixed number of contracts, or spend a % of your balance each bet."
              onCommit={(v) => void update({ crypto15mSizingMode: v as 'fixed' | 'balance_pct' })}
            />
            {sizingMode === 'balance_pct' ? (
              <NumField
                label="Per bet" suffix="% bal" min={0.1} max={100} step={0.1}
                value={+(num('crypto15mBalancePct', 0.02) * 100).toFixed(2)}
                hint="Spend this % of your balance on each entry."
                onCommit={(v) => void update({ crypto15mBalancePct: v / 100 })}
              />
            ) : (
              <NumField
                label="Bet size" suffix="ct" min={MIN_CONTRACTS} max={1000} step={1}
                value={num('crypto15mOrderSize', C15_DEFAULTS.orderSize)}
                hint={`Contracts per bet. Minimum is ${MIN_CONTRACTS}.`}
                onCommit={(v) => void update({ crypto15mOrderSize: Math.round(v) })}
              />
            )}
            <NumField
              label="Max loss / bet" suffix="% bal" min={0} max={100} step={0.5}
              value={+(num('crypto15mMaxLossPct', 0) * 100).toFixed(2)}
              hint="Never risk more than this % of balance on one bet. 0 = off."
              onCommit={(v) => void update({ crypto15mMaxLossPct: v / 100 })}
            />
            <NumField
              label="Max open bets" min={1} max={50} step={1}
              value={num('crypto15mMaxConcurrent', C15_DEFAULTS.maxConcurrent)}
              hint="Most positions open at once (across all coins)."
              onCommit={(v) => void update({ crypto15mMaxConcurrent: Math.round(v) })}
            />
            <NumField
              label="Daily loss stop" suffix="$" min={0} max={100000} step={5}
              value={Math.abs(num('crypto15mDailyLossLimit', -50))}
              hint="Stop opening new bets for the day once you're down this much. 0 = off."
              onCommit={(v) => void update({ crypto15mDailyLossLimit: -Math.abs(v) })}
            />
            <NumField
              label="Take-profit (total)" suffix="$" min={0} max={1000000} step={1}
              value={num('crypto15mTakeProfitTotal', C15_DEFAULTS.takeProfitTotal)}
              hint="Turn the engine off once you're up this much. 0 = off."
              onCommit={(v) => void update({ crypto15mTakeProfitTotal: v })}
            />
          </div>
          <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">
            Pick a preset above, tune the direction and entry band, set your bet
            size and safety caps, flip the executor on — that&apos;s it.
            Take-profits, streak sizing, indicator gates, model tuning, custom
            rules, and backtesting live in{' '}
            <span className="font-semibold text-krypt-muted">Advanced</span> (top right).
          </p>
        </>
      )}

      {uiMode === 'advanced' && (
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        <SelectField
          label="Direction" value={dir}
          options={[['favorite', 'Favorite-follow'], ['contrarian', 'Contrarian fade'], ['model', 'Model — settlement sniper']]}
          hint="Buy the favorite, fade it, or let the spot-vs-open model call the side (needs Detect MACD/RSI + live spot feed on)."
          onCommit={(v) => void update({ crypto15mDirectionMode: v as 'favorite' | 'contrarian' | 'model' })}
        />
        {dir === 'model' && (
          <>
            <NumField
              label="Model certainty ≥" suffix="%" min={50} max={100} step={0.5}
              value={+((config?.crypto15mModelMinProb ?? 0.97) * 100).toFixed(1)}
              hint="Enter only when the model calls the side at least this likely."
              onCommit={(v) => void update({ crypto15mModelMinProb: v / 100 })}
            />
            <NumField
              label="Min net edge" suffix="¢" min={0} max={50} step={0.5}
              value={config?.crypto15mModelMinEdgeCents ?? 2}
              hint="Model probability minus the ask minus the taker fee must clear this."
              onCommit={(v) => void update({ crypto15mModelMinEdgeCents: v })}
            />
            <NumField
              label="Max book gap" suffix="¢" min={0} max={100} step={1}
              value={config?.crypto15mModelMaxBookGapCents ?? 25}
              hint="Safety guard: outside the final minute, skip when the model claims MORE edge than this against the live book — a live book that disagrees that much usually knows the open price better than we do (bad strike after a restart, etc). 0 = off."
              onCommit={(v) => void update({ crypto15mModelMaxBookGapCents: v })}
            />
            <SelectField
              label="Final-minute strikes" value={(config?.crypto15mModelFinalMinute ?? true) ? 'on' : 'off'}
              options={[['on', 'On (3-sigma gate)'], ['off', 'Off']]}
              hint="Allow entries inside the last 60s when the model is near-certain, the spot feed is live, and there's order runway."
              onCommit={(v) => void update({ crypto15mModelFinalMinute: v === 'on' })}
            />
            <SelectField
              label="Calibration guard" value={(config?.crypto15mModelAutopause ?? true) ? 'on' : 'off'}
              options={[['on', 'Auto-pause on drift'], ['off', 'Off']]}
              hint="Pause model entries when recent high-confidence calls stop landing; resumes when calibration recovers."
              onCommit={(v) => void update({ crypto15mModelAutopause: v === 'on' })}
            />
          </>
        )}
        <NumField
          label="Entry window" suffix="min" min={1} max={windowMaxMin} step={1}
          value={num('crypto15mTimeDelayMin', C15_DEFAULTS.timeDelayMin)}
          hint="Only act inside the last N minutes of the window."
          onCommit={(v) => void update({ crypto15mTimeDelayMin: v })}
        />
        <NumField
          label="Favorite ≥" suffix="¢" min={1} max={99} step={1}
          value={Math.round(num('crypto15mEntryThreshold', C15_DEFAULTS.entryThreshold) * 100)}
          hint="The favorite side must be at least this likely to enter."
          onCommit={(v) => void update({ crypto15mEntryThreshold: v / 100 })}
        />
        <NumField
          label="Skip above" suffix="¢" min={1} max={99} step={1}
          value={Math.round(num('crypto15mEntryMax', C15_DEFAULTS.entryMax) * 100)}
          hint="Don't pay more than this — too little room left to profit."
          onCommit={(v) => void update({ crypto15mEntryMax: v / 100 })}
        />
        <NumField
          label="Min move Δ" suffix="%" min={0} max={50} step={0.05}
          value={+(num('crypto15mMinDeltaPct', C15_DEFAULTS.minDeltaPct) * 100).toFixed(2)}
          hint="Required underlying move from the 15-min open (CoinGecko spot). 0 = off."
          onCommit={(v) => void update({ crypto15mMinDeltaPct: v / 100 })}
        />
        <NumField
          label="Stop-loss" suffix="¢" min={0} max={99} step={1}
          value={Math.round(num('crypto15mExitThreshold', C15_DEFAULTS.exitThreshold) * 100)}
          hint="Executor sells if the held side falls to this price. 0 = hold to settlement."
          onCommit={(v) => void update({ crypto15mExitThreshold: v / 100 })}
        />
        <NumField
          label="Take-profit" suffix="¢" min={0} max={99} step={1}
          value={Math.round(num('crypto15mTakeProfit', C15_DEFAULTS.takeProfit) * 100)}
          hint="Cash out a bet when our side's real best bid reaches this price (e.g. 95¢) — locks in the win instead of riding to settlement. 0 = off."
          onCommit={(v) => void update({ crypto15mTakeProfit: v / 100 })}
        />
        <SelectField
          label="Sell into strength" value={(config?.crypto15mSellIntoStrength ?? false) ? 'on' : 'off'}
          options={[['off', 'Off'], ['on', 'On (resting ask)']]}
          hint="On fill, rest a sell order at the target below so any price spike lifts it — fills as a maker (no fee) and catches moves between engine ticks. Only bets entered below the target qualify, so high-priced sniper entries are unaffected."
          onCommit={(v) => void update({ crypto15mSellIntoStrength: v === 'on' })}
        />
        {config?.crypto15mSellIntoStrength && (
          <NumField
            label="Strength target" suffix="¢" min={50} max={99} step={1}
            value={config?.crypto15mSellStrengthCents ?? 80}
            hint="The resting sell price. The PRR winners' pattern sells winners at ~80¢ instead of riding to settlement."
            onCommit={(v) => void update({ crypto15mSellStrengthCents: v })}
          />
        )}
        <NumField
          label="Stop-loss %" suffix="%" min={0} max={100} step={1}
          value={Math.round(num('crypto15mStopLossPct', C15_DEFAULTS.stopLossPct) * 100)}
          hint="Sell once a position is down this % from what it cost (e.g. 20 = exit at −20%). Applies to every timeframe (5m/15m/1h) and works alongside the cents Stop-loss above — whichever hits first exits. 0 = off."
          onCommit={(v) => void update({ crypto15mStopLossPct: Math.max(0, Math.min(100, Math.round(v))) / 100 })}
        />
        <NumField
          label="Take-profit %" suffix="%" min={0} max={100} step={1}
          value={Math.round(num('crypto15mTakeProfitPct', C15_DEFAULTS.takeProfitPct) * 100)}
          hint="Cash out once a position is up this % from what it cost (e.g. 20 = sell at +20%), measured on the real best bid. Works alongside the cents Take-profit above — whichever hits first sells. 0 = off."
          onCommit={(v) => void update({ crypto15mTakeProfitPct: Math.max(0, Math.min(100, Math.round(v))) / 100 })}
        />
        {/* Entered as a positive amount and negated on commit: the old
            min/max={-100000,0} field clamped a typed "50" to 0 = OFF, silently
            disabling the cap the user thought they just set. */}
        <NumField
          label="Daily loss limit" suffix="$" min={0} max={100000} step={5}
          value={Math.abs(num('crypto15mDailyLossLimit', -50))}
          hint="Pause NEW crypto entries for the rest of the UTC day once today's realized P&L (plus open exposure) is down this much. Open positions are still managed. 0 = off."
          onCommit={(v) => void update({ crypto15mDailyLossLimit: -Math.abs(v) })}
        />
        <NumField
          label="Lifetime loss limit" suffix="% bankroll" min={0} max={100} step={5}
          value={Math.round(num('crypto15mLifetimeLossLimitPct', 0.5) * 100)}
          hint="Circuit-breaker: pause the engine once CUMULATIVE realized crypto loss reaches this % of your starting bankroll (survives history wipes). Default 50%. Raise it (or set 0 = off) to resume a tripped engine — this is the setting the 'lifetime loss limit reached' pause refers to."
          onCommit={(v) => void update({ crypto15mLifetimeLossLimitPct: Math.max(0, Math.min(100, Math.round(v))) / 100 })}
        />
        <NumField
          label="Lifetime limit $" suffix="$ (overrides %)" min={0} max={1000000} step={10}
          value={num('crypto15mLifetimeLossLimitUsd', 0)}
          hint="Absolute-dollar version of the lifetime breaker. When > 0 it takes precedence over the % setting. 0 = use the %."
          onCommit={(v) => void update({ crypto15mLifetimeLossLimitUsd: Math.max(0, v) })}
        />
        <SelectField
          label="Entry style" value={entryStyle}
          options={[['maker', 'Rest at bid (maker)'], ['taker', 'Cross spread (taker)']]}
          hint="Maker rests a limit at the bid: no spread paid and no fee at the base tier, but it may not fill. Taker crosses the ask: fills if the book is there, pays the spread plus the taker fee (0.0695 × contracts × p × (1−p) on most markets)."
          onCommit={(v) => void update({ crypto15mEntryStyle: v as 'maker' | 'taker' })}
        />
        {entryStyle === 'maker' ? (
          <>
            <NumField
              label="Cancel unfilled" suffix="min left" min={0} max={15} step={0.5}
              value={num('crypto15mMakerCancelMin', C15_DEFAULTS.makerCancelMin)}
              hint="Give up on a resting entry this many minutes before the market closes. 0 = keep it until close."
              onCommit={(v) => void update({ crypto15mMakerCancelMin: v })}
            />
            <SelectField
              label="If still unfilled"
              value={config?.crypto15mMakerEscalate === false ? 'off' : 'on'}
              options={[['on', 'Cross to taker'], ['off', 'Cancel it']]}
              hint="At the cancel lead, a maker that hasn't filled rests no better — cross the spread (capped at 'Skip above') to actually get filled before close, or just cancel."
              onCommit={(v) => void update({ crypto15mMakerEscalate: v === 'on' })}
            />
          </>
        ) : (
          <NumField
            label="Entry markup" suffix="¢" min={0} max={20} step={1}
            value={Math.round(num('crypto15mEntryDiff', C15_DEFAULTS.entryDiff) * 100)}
            hint="How far through the spread the limit order crosses to get filled."
            onCommit={(v) => void update({ crypto15mEntryDiff: v / 100 })}
          />
        )}
        <NumField
          label="Trade from" suffix="UTC h" min={0} max={24} step={1}
          value={num('crypto15mHoursStartUtc', C15_DEFAULTS.hoursStartUtc)}
          hint="Only enter between these UTC hours (collected data leans positive 00–12 UTC, negative in the US session). Same start/end or 0–24 = always."
          onCommit={(v) => void update({ crypto15mHoursStartUtc: Math.round(v) })}
        />
        <NumField
          label="…until" suffix="UTC h" min={0} max={24} step={1}
          value={num('crypto15mHoursEndUtc', C15_DEFAULTS.hoursEndUtc)}
          hint="End of the UTC entry window. A start later than the end wraps overnight (e.g. 22 → 6)."
          onCommit={(v) => void update({ crypto15mHoursEndUtc: Math.round(v) })}
        />
        <SelectField
          label="Bet size by" value={sizingMode}
          options={[['fixed', 'Fixed contracts'], ['balance_pct', '% of balance']]}
          hint="Buy a fixed number of contracts, or spend a % of your balance each bet."
          onCommit={(v) => void update({ crypto15mSizingMode: v as 'fixed' | 'balance_pct' })}
        />
        {sizingMode === 'balance_pct' ? (
          <NumField
            label="Per bet" suffix="% bal" min={0.1} max={100} step={0.1}
            value={+(num('crypto15mBalancePct', 0.02) * 100).toFixed(2)}
            hint="Spend this % of your balance on each entry (contracts = budget ÷ price)."
            onCommit={(v) => void update({ crypto15mBalancePct: v / 100 })}
          />
        ) : (
          <NumField
            label="Order size" suffix="ct" min={MIN_CONTRACTS} max={1000} step={1}
            value={num('crypto15mOrderSize', C15_DEFAULTS.orderSize)}
            hint={`Contracts per entry. Minimum is ${MIN_CONTRACTS}.`}
            onCommit={(v) => void update({ crypto15mOrderSize: Math.round(v) })}
          />
        )}
        <NumField
          label="Max loss / bet" suffix="% bal" min={0} max={100} step={0.5}
          value={+(num('crypto15mMaxLossPct', 0) * 100).toFixed(2)}
          hint="Never risk more than this % of balance on one bet (it's bought outright, so cost = max loss). 0 = off."
          onCommit={(v) => void update({ crypto15mMaxLossPct: v / 100 })}
        />
        <SelectField
          label="Streak sizing" value={config?.crypto15mStreakSizing ? 'on' : 'off'}
          options={[['off', 'Off'], ['on', 'On']]}
          hint="Scale the next bet by your settled win/loss run: each consecutive loss multiplies size by (1 + on-loss %), each win by (1 + on-win %). Reshapes variance (martingale-style recovery vs bigger drawdowns) — it does NOT add edge; recorded outcomes are streak-free. Max loss/bet and balance caps still apply."
          onCommit={(v) => void update({ crypto15mStreakSizing: v === 'on' })}
        />
        {config?.crypto15mStreakSizing && (
          <>
            <NumField
              label="On loss" suffix="%/loss" min={-90} max={300} step={5}
              value={num('crypto15mStreakLossPct', 20)}
              hint="Size change per consecutive loss, compounding: +20 → 1.2× after one loss, 1.44× after two. Negative shrinks after losses instead."
              onCommit={(v) => void update({ crypto15mStreakLossPct: v })}
            />
            <NumField
              label="On win" suffix="%/win" min={-90} max={300} step={5}
              value={num('crypto15mStreakWinPct', 0)}
              hint="Size change per consecutive win. 0 = any win resets to base size. Positive presses winning streaks (anti-martingale); negative banks down after wins."
              onCommit={(v) => void update({ crypto15mStreakWinPct: v })}
            />
            <NumField
              label="Streak cap" suffix="× base" min={1} max={100} step={0.5}
              value={num('crypto15mStreakMaxMult', 4)}
              hint="Hard ceiling on the streak multiplier. At +20%/loss a 4× cap is reached after ~8 straight losses; the ramp holds there until the streak breaks."
              onCommit={(v) => void update({ crypto15mStreakMaxMult: v })}
            />
          </>
        )}
        <NumField
          label="Max concurrent" min={1} max={50} step={1}
          value={num('crypto15mMaxConcurrent', C15_DEFAULTS.maxConcurrent)}
          hint="Most open 15-min positions at once (across the 7 assets)."
          onCommit={(v) => void update({ crypto15mMaxConcurrent: Math.round(v) })}
        />
        <NumField
          label="Take-profit (total)" suffix="$" min={0} max={1000000} step={1}
          value={num('crypto15mTakeProfitTotal', C15_DEFAULTS.takeProfitTotal)}
          hint="Turn the crypto engine OFF once realized P&L gained since you set this reaches $X. Open bets still ride to settlement. 0 = off."
          onCommit={(v) => void update({ crypto15mTakeProfitTotal: v })}
        />
        <SelectField
          label="Order-flow gate" value={config?.crypto15mImbalanceGate ? 'on' : 'off'}
          options={[['off', 'Off'], ['on', 'Require confirm']]}
          hint="Optional entry gate: only enter when the order-book imbalance backs the side being bought (buy-pressure for Up, sell-pressure for Down). The first composable signal gate — off by default until you've validated the signal on recorded data."
          onCommit={(v) => void update({ crypto15mImbalanceGate: v === 'on' })}
        />
        <NumField
          label="Gate confirm" suffix="imb" min={0} max={1} step={0.05}
          value={num('crypto15mImbalanceGateMin', 0.2)}
          hint="Minimum |imbalance| (0–1) needed to confirm an entry. Higher = stricter. Only used when the order-flow gate is on."
          onCommit={(v) => void update({ crypto15mImbalanceGateMin: v })}
        />
        <NumField
          label="Min RSI" suffix="0–100" min={0} max={100} step={1}
          value={num('crypto15mMinRsi', 0)}
          hint="Direction-aware momentum gate: an Up bet needs RSI ≥ this; a Down bet needs RSI ≤ (100 − this). Pair with a wide Entry window to enter early only on strong momentum. Needs Detect MACD/RSI (auto-enabled). 0 = off."
          onCommit={(v) => void update({ crypto15mMinRsi: Math.round(v), ...(v > 0 ? { crypto15mIndicatorDetect: true } : {}) })}
        />
        <NumField
          label="Min MACD" suffix="|hist|" min={0} max={100000} step={0.1}
          value={num('crypto15mMinMacdHist', 0)}
          hint="Direction-aware: an Up bet needs MACD histogram ≥ this; a Down bet needs ≤ −this. Uses the signal-line histogram — its sign is scale-free across assets, unlike the raw MACD line. Needs Detect MACD/RSI (auto-enabled). 0 = off."
          onCommit={(v) => void update({ crypto15mMinMacdHist: v, ...(v > 0 ? { crypto15mIndicatorDetect: true } : {}) })}
        />
      </div>
      )}

      <SizingPreview sizing={sizing} mode={sizingMode} orderSize={config?.crypto15mOrderSize ?? 5} />
      {dir === 'contrarian' && (
        <p className="mt-2 text-[11px] text-krypt-warn/90">
          Contrarian: when a side is an extreme favorite (≥ threshold) the executor buys the CHEAP opposite side — a low-win, high-payoff longshot. Set stop-loss to 0 to hold to settlement.
        </p>
      )}
    </div>
  );
}

function SizingPreview({ sizing, mode, orderSize }: {
  sizing: Crypto15mSizing | null;
  mode: 'fixed' | 'balance_pct';
  orderSize: number;
}) {
  if (!sizing) return null;
  const { estContracts, estCostUsd, estPriceCents, balanceUsd, balancePct, maxLossPct, streakMult, note } = sizing;
  const known = balanceUsd > 0;
  // Distinguish WHY it's below the minimum: the balance can't fund it (the
  // affordability clamp shrank the count below your configured size) vs. your
  // configured size is genuinely too small. Don't tell someone with size=20 to
  // "raise their size" when the real problem is a $2.95 balance.
  const belowMin = estContracts >= 1 && estContracts < MIN_CONTRACTS;
  const balanceLimited = known && (mode === 'balance_pct' || estContracts < orderSize);
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-krypt-border bg-krypt-surface2 px-3 py-2 text-[11px]">
      <span className="inline-flex items-center gap-1.5 font-semibold uppercase tracking-wider text-krypt-dim">
        <Coins className="h-3.5 w-3.5" /> Per bet
      </span>
      <span className="font-mono text-sm text-white">≈ {fmtUsd(estCostUsd)}</span>
      <span className="text-krypt-muted">
        {estContracts} contract{estContracts === 1 ? '' : 's'} @ ~{estPriceCents}¢
      </span>
      {mode === 'balance_pct' && (
        <span className="text-krypt-dim">
          {(balancePct * 100).toFixed(1)}% of {known ? fmtUsd(balanceUsd) : 'balance'}
        </span>
      )}
      {maxLossPct > 0 && (
        <span className="text-krypt-dim">
          max risk {(maxLossPct * 100).toFixed(1)}%{known ? ` · ${fmtUsd(balanceUsd * maxLossPct)}` : ''}
        </span>
      )}
      {streakMult != null && streakMult !== 1 && (
        <span className={streakMult > 1 ? 'text-krypt-warn' : 'text-krypt-dim'}>
          streak ×{streakMult.toFixed(2)}
        </span>
      )}
      {known && <span className="ml-auto text-krypt-dim">balance {fmtUsd(balanceUsd)}</span>}
      {note && <span className="w-full text-krypt-warn">{note}</span>}
      {belowMin && (
        <span className="w-full text-krypt-loss">
          {balanceLimited
            ? `Your ${fmtUsd(balanceUsd)} balance only funds ${estContracts} contract${estContracts === 1 ? '' : 's'} at ~${estPriceCents}¢ — below the ${MIN_CONTRACTS}-contract minimum, so orders won't place. Add funds to trade.`
            : `Below the ${MIN_CONTRACTS}-contract minimum — orders won't place. Raise your order size to ${MIN_CONTRACTS}+ contracts.`}
        </span>
      )}
    </div>
  );
}

export function NumField({
  label, value, onCommit, suffix, min, max, step, hint,
}: {
  label: string;
  value: number;
  onCommit: (v: number) => void;
  suffix?: string;
  min: number;
  max: number;
  step: number;
  hint?: string;
}) {
  const [text, setText] = useState(String(value));
  // Re-sync when the committed value changes elsewhere (preset/reset).
  useEffect(() => { setText(String(value)); }, [value]);

  const commit = () => {
    const parsed = Number(text);
    if (Number.isNaN(parsed)) { setText(String(value)); return; }
    const clamped = Math.min(max, Math.max(min, parsed));
    setText(String(clamped));
    if (clamped !== value) onCommit(clamped);
  };

  return (
    <label className="block rounded-lg border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5" title={hint}>
      <div className="flex items-center justify-between text-[9px] uppercase tracking-wider text-krypt-dim">
        <span>{label}</span>
        {suffix && <span className="text-krypt-dim/70">{suffix}</span>}
      </div>
      <input
        type="number"
        inputMode="decimal"
        value={text}
        min={min}
        max={max}
        step={step}
        onChange={(e) => setText(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
        className="mt-0.5 w-full bg-transparent font-mono text-sm text-white outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none"
      />
    </label>
  );
}

export function SelectField({
  label, value, options, onCommit, hint,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onCommit: (v: string) => void;
  hint?: string;
}) {
  return (
    <label className="block rounded-lg border border-krypt-border bg-krypt-surface2 px-2.5 py-1.5" title={hint}>
      <div className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</div>
      <select
        value={value}
        onChange={(e) => onCommit(e.target.value)}
        className="mt-0.5 w-full cursor-pointer bg-transparent font-mono text-sm text-white outline-none"
      >
        {options.map(([v, lbl]) => (
          <option key={v} value={v} className="bg-krypt-surface text-white">{lbl}</option>
        ))}
      </select>
    </label>
  );
}

type CardState = 'signal' | 'window' | 'watching' | 'idle';

function cardState(a: Crypto15mAsset): CardState {
  if (a.signal) return 'signal';
  if (a.inWindow) return 'window';
  if (a.hasMarket) return 'watching';
  return 'idle';
}

function AssetCard({ a }: { a: Crypto15mAsset }) {
  const state = cardState(a);
  const ring = cls(
    a.enabled === false && 'opacity-50',
    state === 'signal'
      ? 'border-krypt-win/60 shadow-[0_0_22px_rgba(34,197,94,0.18)]'
      : state === 'window'
        ? 'border-krypt-warn/50'
        : 'border-krypt-border',
  );

  const upFav = a.favorite === 'up';
  const downFav = a.favorite === 'down';

  return (
    <div className={cls('krypt-card flex flex-col gap-3 border', ring)}>
      <div className="flex items-center gap-2">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-krypt-surface2 text-krypt-purple">
          <Bitcoin className="h-4 w-4" />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold text-white">{a.asset}</div>
          <div className="font-mono text-[10px] text-krypt-dim">{a.series}</div>
        </div>
        <div className="ml-auto">
          <StatePill state={state} />
        </div>
      </div>

      {a.error ? (
        <div className="rounded-md border border-krypt-loss/30 bg-krypt-loss/5 px-2 py-1.5 text-[11px] text-krypt-loss">
          {a.error}
        </div>
      ) : !a.hasMarket ? (
        <div className="py-2 text-center text-xs text-krypt-dim">No open contract right now.</div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2">
            <SideBox label="UP" prob={a.upProb} fav={upFav} good />
            <SideBox label="DOWN" prob={a.downProb} fav={downFav} />
          </div>
          <div className="flex items-center justify-between text-xs">
            <span className="text-krypt-muted">
              closes in <span className="font-mono text-white">{fmtMins(a.minsLeft)}</span>
            </span>
            <span className="text-krypt-muted">
              entry <span className="font-mono text-white">{pct(a.entryCost)}</span>
            </span>
          </div>
          {a.arbEdgeCents != null && (
            <div className={cls(
              'flex items-center justify-between rounded-md border px-2 py-1 text-[11px]',
              a.arbSignal
                ? 'border-krypt-win/50 bg-krypt-win/10 text-krypt-win'
                : 'border-krypt-border bg-krypt-surface2 text-krypt-dim',
            )}>
              <span>Up+Down asks</span>
              <span className="font-mono">
                {a.upAsk != null && a.downAsk != null
                  ? `${Math.round(a.upAsk * 100)}¢ + ${Math.round(a.downAsk * 100)}¢`
                  : '—'}
                {a.arbSignal && <span className="ml-2 font-semibold">ARB +{a.arbEdgeCents}¢</span>}
              </span>
            </div>
          )}
          {a.modelProb != null && (
            <div
              title="The app's own spot model against the price, after the fee. On 3,106 recorded 15-minute windows the model was not a better forecast than the market price: buying when this showed +2¢ or more won 46% at an average 46¢ ask and lost about 1.6¢ a share. Read it as a disagreement, not an edge."
              className={cls(
              'flex items-center justify-between rounded-md border px-2 py-1 text-[11px]',
              'border-krypt-border bg-krypt-surface2 text-krypt-dim',
            )}>
              <span>Model vs price</span>
              <span className="font-mono">
                {Math.round(a.modelProb * 100)}%{a.modelProb >= 0.5 ? '↑' : '↓'}
                {a.edgeNetCents != null && (
                  <span className="ml-2 text-krypt-muted">
                    {a.edgeNetCents >= 0 ? '+' : ''}{a.edgeNetCents.toFixed(1)}¢
                  </span>
                )}
                {!a.spotLive && <span className="ml-2 text-krypt-dim">rest spot</span>}
              </span>
            </div>
          )}
          {a.bookImbalance != null && (
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-krypt-muted">Order-flow (Up book)</span>
              <span className={cls(
                'font-mono',
                a.bookImbalance > 0.15 ? 'text-krypt-win'
                  : a.bookImbalance < -0.15 ? 'text-krypt-loss'
                    : 'text-krypt-dim',
              )}>
                {a.bookImbalance > 0 ? '+' : ''}{a.bookImbalance.toFixed(2)}
                {a.bookImbalance > 0.15 ? ' buy' : a.bookImbalance < -0.15 ? ' sell' : ''}
              </span>
            </div>
          )}
          {a.peersAgree != null && (
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-krypt-muted">Peers agree ({a.favorite})</span>
              <span className={cls(
                'font-mono',
                a.peersAgree >= 0.6 ? 'text-krypt-win'
                  : a.peersAgree <= 0.3 ? 'text-krypt-loss'
                    : 'text-krypt-dim',
              )}>
                {Math.round(a.peersAgree * 100)}%
              </span>
            </div>
          )}
          {a.macdHist != null && (
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-krypt-muted">MACD (underlying)</span>
              <span className={cls(
                'font-mono',
                a.macdHist > 0 ? 'text-krypt-win' : a.macdHist < 0 ? 'text-krypt-loss' : 'text-krypt-dim',
              )}>
                {a.macdHist > 0 ? 'bull' : a.macdHist < 0 ? 'bear' : 'flat'}
                {a.macdCross === 1 ? ' ↑' : a.macdCross === -1 ? ' ↓' : ''}
                {a.rsi != null ? ` · RSI ${Math.round(a.rsi)}` : ''}
              </span>
            </div>
          )}
          {a.wsAsk != null && a.favoritePrice != null && (() => {
            const refC = Math.round(a.favoritePrice * 100);
            const gap = a.wsAsk - refC; // real ask − public mid, the divergence
            return (
              <div className="flex items-center justify-between text-[11px]">
                <span className="text-krypt-muted">
                  Real book {a.priceSource === 'ws'
                    ? <span className="text-krypt-win">● deciding</span>
                    : <span className="text-krypt-dim">(ref)</span>}
                </span>
                <span className={cls(
                  'font-mono',
                  Math.abs(gap) >= 5 ? 'text-krypt-loss' : 'text-krypt-dim',
                )}>
                  {a.wsAsk}¢ <span className="text-krypt-muted">vs {refC}¢ public</span>
                  {` (${gap >= 0 ? '+' : ''}${gap}¢)`}
                </span>
              </div>
            );
          })()}
        </>
      )}

      <div className="-mx-5 -mb-5 mt-1 grid grid-cols-3 gap-px border-t border-krypt-border bg-krypt-border/40 text-center text-[11px]">
        <Foot label="Spot" value={fmtSpot(a.spotUsd)} />
        <Foot label="Open" value={fmtSpot(a.open15mUsd)} />
        <Foot label="Δ move" value={fmtDelta(a.deltaUsd)} />
      </div>
    </div>
  );
}

function StatePill({ state }: { state: CardState }) {
  if (state === 'signal') {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-krypt-win/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-krypt-win">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-krypt-win shadow-[0_0_8px_currentColor]" />
        Signal
      </span>
    );
  }
  if (state === 'window') {
    return (
      <span className="rounded-full bg-krypt-warn/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-krypt-warn">
        In window
      </span>
    );
  }
  if (state === 'watching') {
    return (
      <span className="rounded-full bg-white/5 px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-muted">
        watching
      </span>
    );
  }
  return <span className="text-[10px] uppercase tracking-wider text-krypt-dim">idle</span>;
}

function SideBox({
  label, prob, fav, good,
}: { label: string; prob: number | null; fav: boolean; good?: boolean }) {
  return (
    <div
      className={cls(
        'rounded-lg border px-2 py-1.5 text-center',
        fav
          ? good
            ? 'border-krypt-win/40 bg-krypt-win/10'
            : 'border-krypt-loss/40 bg-krypt-loss/10'
          : 'border-krypt-border bg-krypt-surface2',
      )}
    >
      <div className={cls('text-[10px] uppercase tracking-wider', fav ? (good ? 'text-krypt-win' : 'text-krypt-loss') : 'text-krypt-dim')}>
        {label}{fav ? ' ★' : ''}
      </div>
      <div className="font-mono text-lg text-white">{pct(prob)}</div>
    </div>
  );
}

function Foot({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-krypt-surface px-2 py-2">
      <div className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</div>
      <div className="mt-0.5 font-mono text-white">{value}</div>
    </div>
  );
}

// Short, table-friendly labels for a position's terminal reason. The full
// explanation rides in the hover tooltip so the Status column stays narrow —
// "canceled" alone left users thinking the bot was broken when an entry was
// actually skipped for a concrete (and usually expected) reason.
const C15_SKIP_LABELS: Record<string, { label: string; tip: string }> = {
  above_cap: { label: 'above cap', tip: 'Favorite was priced above your Skip-above cap — skipped (too expensive to enter).' },
  no_liquidity: { label: 'no fill', tip: 'Taker crossed but found no resting liquidity at/under your price — 0 fill. Enter earlier (wider window) or use maker.' },
  favorite_flipped: { label: 'flipped', tip: 'Favorite fell below your Favorite≥ floor before the order landed — skipped.' },
  unfilled_expired: { label: 'expired', tip: "Order didn't fill before the window closed." },
  paper_pulled: { label: 'pulled (Paper)', tip: 'The account went to Paper while this entry was still waiting to fill, so it was canceled. Paper completes no real buy.' },
  stop_loss: { label: 'stopped', tip: 'Stop-loss sold to flatten the position.' },
  take_profit: { label: 'took profit', tip: 'Take-profit cashed out — sold the position into the book at your target price.' },
  settlement_reconciled: { label: 'settled', tip: 'Booked from the exchange\'s settlement after a missed/mis-marked fill.' },
  paper_removed: { label: 'removed', tip: 'Legacy paper position retired.' },
};

function PositionsTable({ title, rows }: { title: string; rows: Crypto15mPosition[] }) {
  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold uppercase tracking-[0.16em] text-krypt-muted">{title}</h3>
      <div className="overflow-hidden rounded-xl border border-krypt-border">
        <table className="krypt-table">
          <thead>
            <tr>
              <th>Asset</th>
              <th>Side</th>
              <th>Status</th>
              <th>Contracts</th>
              <th>Entry</th>
              <th>Cost</th>
              <th>P&amp;L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => <PositionRow key={p.id} p={p} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PositionRow({ p }: { p: Crypto15mPosition }) {
  const entryC = p.avgEntryCents ?? p.entryLimitCents;
  const reason = p.exitReason ? C15_SKIP_LABELS[p.exitReason] : undefined;
  return (
    <tr>
      <td><TickerLink ticker={p.ticker} env={p.network} label={p.asset} /></td>
      <td>
        <span className={cls(
          'rounded-md px-1.5 py-0.5 text-[10px] font-semibold uppercase',
          p.side === 'up' ? 'bg-krypt-win/10 text-krypt-win' : 'bg-krypt-loss/10 text-krypt-loss',
        )}>
          {p.side || p.direction}
        </span>
      </td>
      <td className="text-xs text-krypt-muted">
        {p.settling ? (
          <span className="text-krypt-warn" title="Window closed — waiting on the exchange to settle it.">
            resolving…
          </span>
        ) : reason ? (
          <span
            className={p.status === 'canceled' || p.status === 'error' ? 'text-krypt-dim' : undefined}
            title={reason.tip}
          >
            {reason.label}
          </span>
        ) : p.status === 'error' ? (
          <span className="text-krypt-loss" title={p.error || 'Order error'}>error</span>
        ) : (
          <>{p.status}</>
        )}
      </td>
      <td className="font-mono text-xs">{p.filledContracts}/{p.targetContracts}</td>
      <td className="font-mono text-xs">{entryC ? `${Math.round(entryC)}¢` : '—'}</td>
      <td className="font-mono text-xs text-krypt-muted">{fmtUsd(p.costUsd)}</td>
      <td className={cls(
        'font-mono text-xs',
        p.pnlUsd === null ? 'text-krypt-dim' : p.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss',
      )}>
        {p.pnlUsd === null ? '—' : fmtUsd(p.pnlUsd, { sign: true })}
      </td>
    </tr>
  );
}
