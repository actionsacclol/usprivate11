import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Power, ShieldCheck, Target, Timer, TrendingUp } from 'lucide-react';
import { Page, Card } from '../components/common';
import { useToast } from '../state/ToastProvider';
import { useApp } from '../state/AppStateProvider';
import { cls, fmtUsd } from '../utils/format';
import type { ScriptApiDocs, TraderConfig, UserScript } from '../../shared/types';
import type { PageId } from '../App';

type Preset = ScriptApiDocs['presets'][number];
type Risk = 'low' | 'medium' | 'high';

const RISK_LABELS: Record<Risk, string> = {
  low: 'Cautious', medium: 'Standard', high: 'Aggressive',
};

// ───────── crypto quick-starts (built-in 15m engine, not scripts) ─────────
//
// One-click config bundles for the recurring crypto Up/Down series. Applied
// via config.update (a pure crypto15m* patch — applyStrategy can't be used,
// it deliberately PRESERVES crypto15m keys). Loading a bundle never enables
// the engine: the live switch is its own explicit, confirmed act below.
//
// Honesty contract (same as the audit presets): favorite-follow has NO
// proven long-term edge after fees — these are training wheels with small
// fixed size and a -$10 daily breaker, for learning how the windows move.
// Occasionally a favorite-follow run prints; treat that as variance until
// the user's own recorded data (Backtest tab) says otherwise.

interface CryptoQuickstart {
  id: string;
  name: string;
  interval: NonNullable<TraderConfig['crypto15mInterval']>;
  blurb: string;
  patch: Partial<TraderConfig>;
}

/** Shared late-window favorite-follow shape: buy the 70%+ favorite in the
 *  closing stretch of the window (e.g. the last ~6-7 min on 15m), capped at
 *  92c, tiny fixed size, hold to settlement, -$10 daily breaker. */
const QS_BASE: Partial<TraderConfig> = {
  crypto15mDirectionMode: 'favorite',
  crypto15mUseRules: false,
  crypto15mEntryThreshold: 0.70,
  crypto15mEntryMax: 0.92,
  crypto15mSizingMode: 'fixed',
  crypto15mOrderSize: 5,
  crypto15mMaxConcurrent: 2,
  crypto15mDailyLossLimit: -10,
  // Hold to settlement — free, and the simplest behavior to reason about
  // while learning. Stops/TPs are all off.
  crypto15mExitThreshold: 0,
  crypto15mStopLossPct: 0,
  crypto15mTakeProfit: 0,
  crypto15mTakeProfitPct: 0,
};

const CRYPTO_QUICKSTARTS: CryptoQuickstart[] = [
  {
    id: 'qs-5m',
    name: '5-minute markets',
    interval: '5m',
    blurb: 'Fastest feedback loop — a result every 5 minutes. Enters the 70%+ favorite in the last ~2.5 minutes when the spot has actually moved.',
    patch: {
      ...QS_BASE,
      crypto15mInterval: '5m',
      crypto15mTimeDelayMin: 2.5,
      crypto15mMinDeltaPct: 0.0005,
    },
  },
  {
    id: 'qs-15m',
    name: '15-minute markets',
    interval: '15m',
    blurb: 'The classic. Enters the 70%+ favorite in the last ~7 minutes once the underlying has moved — the band that sometimes runs hot.',
    patch: {
      ...QS_BASE,
      crypto15mInterval: '15m',
      crypto15mTimeDelayMin: 7,
      crypto15mMinDeltaPct: 0.001,
    },
  },
  {
    id: 'qs-1h',
    name: 'Hourly markets',
    interval: 'hourly',
    blurb: 'Slower and calmer — one result an hour. Enters the 70%+ favorite in the last ~25 minutes behind a bigger required move.',
    patch: {
      ...QS_BASE,
      crypto15mInterval: 'hourly',
      crypto15mTimeDelayMin: 25,
      crypto15mMinDeltaPct: 0.002,
    },
  },
];

/** A quick-start counts as "loaded" when its identifying knobs match the
 *  live config (users tweaking knobs in Advanced clears the highlight). */
function quickstartLoaded(q: CryptoQuickstart, cfg: TraderConfig | null): boolean {
  if (!cfg) return false;
  return cfg.crypto15mInterval === q.interval
    && cfg.crypto15mDirectionMode === 'favorite'
    && Number(cfg.crypto15mEntryThreshold) === Number(q.patch.crypto15mEntryThreshold)
    && Number(cfg.crypto15mTimeDelayMin) === Number(q.patch.crypto15mTimeDelayMin);
}

/** Rewrite ONLY the two parameter lines the presets declare for this purpose.
 *  Exact-prefix match — the preset code marks them "<- set by the Strategies
 *  page" and the backend keeps those lines stable. */
function parameterize(code: string, bankroll: number, risk: Risk): string {
  return code
    .replace(/^BANKROLL_USD = .*$/m, `BANKROLL_USD = ${bankroll.toFixed(1)}   # <- set by the Strategies page`)
    .replace(/^RISK = .*$/m, `RISK = "${risk}"        # <- set by the Strategies page (low | medium | high)`);
}

function installedRisk(code: string): Risk | null {
  const m = /^RISK = "(low|medium|high)"/m.exec(code);
  return m ? (m[1] as Risk) : null;
}

/** The Strategies page: one-click presets, each an ordinary krypt-script the
 *  backend serves (so code can never drift from the shipped engine). Install
 *  puts it in SHADOW mode — it builds a forward track record on live ticks
 *  without spending money; arming stays an explicit act on the Scripts page,
 *  behind the same confirmation every script gets. */
export function StrategiesPage({ onNav }: { onNav: (p: PageId) => void }) {
  const toast = useToast();
  const { config } = useApp();
  const [docs, setDocs] = useState<ScriptApiDocs | null>(null);
  const [scripts, setScripts] = useState<UserScript[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(() => {
    window.krypt.scripts.docs().then(setDocs).catch(() => {});
    window.krypt.scripts.list().then((r) => setScripts(r.scripts || [])).catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    const off = window.krypt.scripts.onStatus(() => refresh());
    return off;
  }, [refresh]);

  // Set while an install would replace the code of an ARMED script, which is
  // the same act as arming: it decides what spends real money next. Held here
  // until the user says so out loud.
  const [swap, setSwap] = useState<
    { preset: Preset; bankroll: number; risk: Risk; existing: UserScript } | null>(null);

  const doInstall = async (preset: Preset, bankroll: number, risk: Risk) => {
    setBusy(preset.id);
    try {
      const existing = scripts.find((s) => s.name === preset.name);
      const code = parameterize(preset.code, bankroll, risk);
      const res = await window.krypt.scripts.save(
        existing ? { id: existing.id, code, name: preset.name } : { code, name: preset.name },
      );
      const saved: UserScript | undefined = res?.script;
      if (!saved) {
        toast.error(`Could not install: ${(res?.errors || []).join('; ') || 'unknown error'}`);
        return;
      }
      const wasOff = !saved.enabled;
      if (wasOff) await window.krypt.scripts.setEnabled(saved.id, true);
      // Say what this click actually did — including that armed code was
      // replaced under a running script, which is the part that spends money.
      if (!existing) {
        toast.success(`${preset.name} installed in shadow mode — it now builds a track record without spending money`);
      } else if (existing.dryRun) {
        toast.success(`${preset.name}: shadow code replaced — still in shadow, still spending nothing.`);
      } else {
        toast.warn(`${preset.name}: ARMED code replaced${wasOff ? ' and re-enabled' : ''} — the next time it fires it runs this version with real money.`);
      }
      refresh();
    } catch (e) {
      toast.error(`Install failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  const install = async (preset: Preset, bankroll: number, risk: Risk) => {
    const existing = scripts.find((s) => s.name === preset.name);
    // Re-installing over a script that is ARMED (dryRun === false) hot-swaps
    // the code that places real orders — with no arm step in between, because
    // the script is already past it. Ask first; a shadow script has nothing to
    // lose and installs straight through.
    if (existing && existing.dryRun === false) {
      setSwap({ preset, bankroll, risk, existing });
      return;
    }
    await doInstall(preset, bankroll, risk);
  };

  return (
    <Page
      title="Strategies"
      subtitle="One-click strategies with honest expectations. Every install starts in shadow mode — it trades on paper against the live market until you review its record and arm it yourself."
    >
      <div className="mb-4 flex items-start gap-2 rounded-lg border border-krypt-border bg-krypt-surface p-3 text-xs text-krypt-muted">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-krypt-purple" />
        <p>
          No strategy here is a money printer — each card says honestly what to expect, including
          how it loses. Start small, watch what it does, and form your own view before sizing up.
        </p>
      </div>

      <CryptoQuickstartSection />

      <h2 className="mb-2 mt-8 text-sm font-semibold uppercase tracking-wider text-krypt-muted">
        Script presets — shadow-first
      </h2>
      <p className="mb-3 text-xs text-krypt-muted">
        Script-based strategies. Any figures on a card come from the international Polymarket venue,
        not from Polymarket US. Installing puts them in shadow mode: they trade on paper against the
        live market until you review the record and arm them yourself.
      </p>

      <div className="grid gap-4 xl:grid-cols-2">
        {(docs?.presets || []).map((p) => (
          <PresetCard
            key={p.id}
            preset={p}
            installed={scripts.find((s) => s.name === p.name) || null}
            busy={busy === p.id}
            defaultBankroll={Math.max(p.bankrollMin, Math.round(Number(config?.startBankrollUsd ?? 0) / 4) || p.bankrollMin * 2)}
            onInstall={install}
            onReview={() => onNav('scripts')}
          />
        ))}
        {docs && !docs.presets?.length && (
          <Card><p className="text-sm text-krypt-muted">No presets available — the backend predates them. Update the app.</p></Card>
        )}
      </div>

      {swap && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onMouseDown={() => setSwap(null)}>
          <div
            className="w-full max-w-md rounded-xl border border-krypt-loss/50 bg-krypt-surface p-5 shadow-krypt-soft"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 text-krypt-loss">
              <AlertTriangle className="h-5 w-5" />
              <span className="font-semibold text-white">Replace the code of an ARMED script?</span>
            </div>
            <ul className="mt-3 list-disc space-y-1.5 pl-5 text-xs text-krypt-muted">
              <li>
                <span className="text-white/90">“{swap.existing.name}” is ARMED</span> — it places
                real orders with real money, and it{' '}
                {swap.existing.enabled ? 'is running right now' : 'will be re-enabled by this install'}.
              </li>
              <li>
                Installing overwrites its code in place. There is no second arm step: the next time
                it fires, the version you are installing now is the one spending money.
              </li>
              <li>
                Its record so far was made by the OLD code
                {swap.existing.stats
                  ? ` (${swap.existing.stats.wins}W/${swap.existing.stats.losses}L, ${fmtUsd(swap.existing.stats.pnlUsd, { sign: true })})`
                  : ''}
                {' '}— it does not describe what you are about to run.
              </li>
              <li>
                Bankroll {fmtUsd(swap.bankroll)} · risk {RISK_LABELS[swap.risk].toLowerCase()}.
              </li>
            </ul>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setSwap(null)}
                className="rounded-md border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-white"
              >
                Cancel — leave the armed script alone
              </button>
              <button
                onClick={() => {
                  const s = swap;
                  setSwap(null);
                  void doInstall(s.preset, s.bankroll, s.risk);
                }}
                className="rounded-md border border-krypt-loss/60 bg-krypt-loss/15 px-3 py-1.5 text-xs font-semibold text-krypt-loss"
              >
                Replace the armed code
              </button>
            </div>
          </div>
        </div>
      )}
    </Page>
  );
}

function CryptoQuickstartSection() {
  const toast = useToast();
  const { config } = useApp();
  const [confirmOn, setConfirmOn] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const live = !!config?.crypto15mEnabled;

  const load = async (q: CryptoQuickstart) => {
    setBusy(q.id);
    try {
      await window.krypt.config.update(q.patch);
      toast.success(`${q.name} settings loaded${live
        ? ' — the engine is LIVE and now trades this window'
        : '. The engine is still off — start it below when you are ready.'}`);
    } catch (e) {
      toast.error(`Could not load settings: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  // Risk-OFF is one click; going LIVE takes the confirmation modal below —
  // same house rule as arming a script.
  const setLive = async (on: boolean) => {
    try {
      await window.krypt.config.update({ crypto15mEnabled: on });
      if (on) toast.warn('Crypto engine is LIVE — it places real orders from now on.');
      else toast.success('Crypto engine stopped.');
    } catch (e) {
      toast.error(`Could not switch the engine: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  return (
    <section>
      <h2 className="mb-2 text-sm font-semibold uppercase tracking-wider text-krypt-muted">
        Crypto quick-start — training wheels
      </h2>
      <p className="mb-3 text-xs text-krypt-muted">
        One click loads sensible settings for a crypto Up/Down series: buy the 70%+ favorite late in
        the window (never above 92c), 5 contracts a trade, hold to settlement, and stop for the day
        at −$10. <span className="text-krypt-warn">Honest label:</span> favorite-follow has no proven
        long-term edge after fees — this is for learning the markets with small size. Runs can print;
        treat that as variance until your own Backtest data says otherwise.
      </p>

      <div className="grid gap-4 lg:grid-cols-3">
        {CRYPTO_QUICKSTARTS.map((q) => {
          const loaded = quickstartLoaded(q, config);
          return (
            <Card
              key={q.id}
              header={(
                <div className="flex items-center gap-2">
                  <Timer className="h-4 w-4 text-krypt-purple" />
                  <span className="font-medium text-white">{q.name}</span>
                  {loaded && (
                    <span className="ml-auto rounded-full bg-krypt-purple/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-krypt-purple">
                      Loaded
                    </span>
                  )}
                </div>
              )}
            >
              <p className="text-sm text-krypt-muted">{q.blurb}</p>
              <div className="mt-3 flex items-center justify-between">
                <span className="text-[11px] text-krypt-dim">
                  70%+ favorite · last {q.patch.crypto15mTimeDelayMin} min · 5 contracts · −$10/day stop
                </span>
                <button
                  disabled={busy === q.id}
                  onClick={() => load(q)}
                  className={cls(
                    'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                    loaded
                      ? 'border border-krypt-border bg-krypt-surface2 text-krypt-muted hover:text-white'
                      : 'bg-krypt-purple/80 text-white hover:bg-krypt-purple',
                  )}
                >
                  {busy === q.id ? 'Loading…' : loaded ? 'Reload defaults' : 'Load settings'}
                </button>
              </div>
            </Card>
          );
        })}
      </div>

      <div className="mt-3 flex items-center gap-3 rounded-lg border border-krypt-border bg-krypt-surface p-3">
        <Power className={cls('h-4 w-4 shrink-0', live ? 'text-krypt-win' : 'text-krypt-dim')} />
        <div className="min-w-0 flex-1 text-xs">
          <span className={cls('font-medium', live ? 'text-krypt-win' : 'text-white/80')}>
            Crypto engine {live ? 'LIVE' : 'off'}
          </span>
          <span className="text-krypt-muted">
            {' '}— {live
              ? `placing real orders on the ${config?.crypto15mInterval || '15m'} series. Stopping is instant.`
              : 'loading settings changes nothing until you start it. Starting places real orders with real money when the app is Live; in Paper the engines send nothing.'}
          </span>
        </div>
        <button
          onClick={() => (live ? void setLive(false) : setConfirmOn(true))}
          className={cls(
            'shrink-0 rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
            live
              ? 'bg-krypt-loss/20 text-krypt-loss hover:bg-krypt-loss/30'
              : 'bg-krypt-win/15 text-krypt-win hover:bg-krypt-win/25',
          )}
        >
          {live ? 'Stop engine' : 'Start engine…'}
        </button>
      </div>

      {confirmOn && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4">
          <div className="w-full max-w-md rounded-xl border border-krypt-border bg-krypt-surface p-5 shadow-krypt-soft">
            <div className="flex items-center gap-2 text-krypt-warn">
              <AlertTriangle className="h-5 w-5" />
              <span className="font-semibold text-white">Start the crypto engine?</span>
            </div>
            <ul className="mt-3 list-disc space-y-1.5 pl-5 text-xs text-krypt-muted">
              <li>It places <span className="text-white/90">real orders with real money</span> the moment it starts in Live. In Paper the engines send nothing.</li>
              <li>Favorite-follow has <span className="text-white/90">no proven long-term edge after fees</span>. Expect to pay for the lesson; the −$10 daily breaker caps how much per day.</li>
              <li>Winning contracts pay $1.00, losing ones $0.00 — a 70c favorite that loses costs the full 70c.</li>
              <li>Stopping the engine is always one click, no confirmation.</li>
            </ul>
            <div className="mt-4 flex justify-end gap-2">
              <button
                onClick={() => setConfirmOn(false)}
                className="rounded-md border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-white"
              >
                Cancel
              </button>
              <button
                onClick={() => { setConfirmOn(false); void setLive(true); }}
                className="rounded-md bg-krypt-win/20 px-3 py-1.5 text-xs font-medium text-krypt-win hover:bg-krypt-win/30"
              >
                Start trading live
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

function PresetCard({
  preset, installed, busy, defaultBankroll, onInstall, onReview,
}: {
  preset: Preset;
  installed: UserScript | null;
  busy: boolean;
  defaultBankroll: number;
  onInstall: (p: Preset, bankroll: number, risk: Risk) => void;
  onReview: () => void;
}) {
  const [bankroll, setBankroll] = useState<number>(defaultBankroll);
  const [risk, setRisk] = useState<Risk>((installed && installedRisk(installed.code)) || 'medium');

  const shadow = installed?.dryRun !== false;
  const stats = installed ? (shadow ? installed.shadowStats : installed.stats) : null;

  return (
    <Card
      header={(
        <div className="flex items-center gap-2">
          <Target className="h-4 w-4 text-krypt-purple" />
          <span className="font-medium text-white">{preset.name}</span>
          {installed && (
            <span className={cls(
              'ml-auto rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider',
              shadow ? 'bg-krypt-purple/15 text-krypt-purple' : 'bg-krypt-win/15 text-krypt-win',
            )}
            >
              {shadow ? 'Shadow' : 'LIVE'}
            </span>
          )}
        </div>
      )}
    >
      <p className="text-sm text-krypt-muted">{preset.tagline}</p>

      <div className="mt-3 space-y-2 text-xs">
        <div className="flex items-start gap-2">
          <TrendingUp className="mt-0.5 h-3.5 w-3.5 shrink-0 text-krypt-win" />
          <span className="text-krypt-muted"><span className="text-white/80">What to expect:</span> {preset.expected}</span>
        </div>
        <div className="flex items-start gap-2">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-krypt-warn" />
          <span className="text-krypt-muted"><span className="text-white/80">How it loses:</span> {preset.risks}</span>
        </div>
      </div>

      {installed && (
        <div className="mt-3 rounded-md border border-krypt-border bg-krypt-surface2/50 p-2 text-xs text-krypt-muted">
          {stats && stats.n > 0 ? (
            <>
              <span className="text-white/80">{shadow ? 'Shadow record' : 'Live record'}:</span>{' '}
              {stats.wins}W / {stats.losses}L over {stats.n} entries ·{' '}
              <span className={stats.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
                {fmtUsd(stats.pnlUsd, { sign: true })}
              </span>
              {shadow && ' (simulated — no money spent)'}
            </>
          ) : (
            <>No {shadow ? 'shadow ' : ''}entries yet — {installed.status.detail || 'waiting for a matching market.'}</>
          )}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wider text-krypt-muted">
          Bankroll to allocate
          <div className="flex items-center gap-1">
            <span className="text-sm text-krypt-muted">$</span>
            <input
              type="number"
              min={preset.bankrollMin}
              value={bankroll}
              onChange={(e) => setBankroll(Math.max(0, Number(e.target.value)))}
              className="w-24 rounded-md border border-krypt-border bg-krypt-surface2 px-2 py-1.5 font-mono text-sm text-white outline-none focus:border-krypt-purple/60"
            />
          </div>
        </label>

        <label className="flex flex-col gap-1 text-[11px] uppercase tracking-wider text-krypt-muted">
          Risk
          <div className="inline-flex overflow-hidden rounded-md border border-krypt-border">
            {(['low', 'medium', 'high'] as const).map((r) => (
              <button
                key={r}
                onClick={() => setRisk(r)}
                className={cls(
                  'px-2.5 py-1.5 text-xs transition-colors',
                  risk === r ? 'bg-krypt-glow text-white' : 'bg-krypt-surface2 text-krypt-muted hover:text-white',
                )}
              >
                {RISK_LABELS[r]}
              </button>
            ))}
          </div>
        </label>

        <div className="ml-auto flex items-center gap-2">
          {installed && (
            <button
              onClick={onReview}
              className="rounded-md border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted transition-colors hover:text-white"
              title="Open the Scripts page to inspect the code, its risk audit, and the arm switch"
            >
              Review &amp; arm
            </button>
          )}
          <button
            disabled={busy || bankroll < preset.bankrollMin}
            onClick={() => onInstall(preset, bankroll, risk)}
            title={bankroll < preset.bankrollMin ? `Needs at least $${preset.bankrollMin}` : undefined}
            className={cls(
              'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
              busy || bankroll < preset.bankrollMin
                ? 'cursor-not-allowed bg-krypt-surface2 text-krypt-dim'
                : 'bg-krypt-purple/80 text-white hover:bg-krypt-purple',
            )}
          >
            {busy ? 'Working…' : installed ? 'Update settings' : 'Install (shadow)'}
          </button>
        </div>
      </div>
    </Card>
  );
}
