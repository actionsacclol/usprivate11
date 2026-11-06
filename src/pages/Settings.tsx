import { useEffect, useState } from 'react';
import { ArrowRight, Compass, KeyRound, PlayCircle, RefreshCw, RotateCcw, Sparkles } from 'lucide-react';
import type { TraderConfig } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import type { TerminalHealth } from '@shared/market';
import type { PageId } from '../App';
import { Card, Page, Section, Switch } from '../components/common';
import { Val } from '../components/terminal/TerminalBits';
import { AccountModePanel } from '../components/AccountModePanel';
import { AiSettings } from '../components/AiSettings';
import { GlassSegmented } from '../components/glass/GlassSegmented';
import { useGuide } from '../components/tour/useTour';
import {
  glassQualityState, setGlassQuality, subscribe as subscribeGlass,
  type GlassQuality, type GlassQualityState,
} from '../components/glass/glassQuality';
import { cls } from '../utils/format';

export function SettingsPage({ onNav }: { onNav?: (p: PageId) => void } = {}) {
  const { config, refresh, state, backend } = useApp();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  /** Restart the Python backend PROCESS. Deliberately app-level: it takes
   *  every engine down and back up — main, crypto, copy-trading, scripts and
   *  the scanners — so it does not belong on any one engine's page, where it
   *  would read as scoped to that engine. It lived in the app header until the
   *  header stopped carrying controls. */
  const restartBackend = async (): Promise<void> => {
    toast.info('Restarting backend — every engine stops and restarts…');
    await window.krypt.backend.restart();
    setTimeout(() => void refresh.backend(), 1000);
  };

  if (!config) return <Page title="Settings"><div className="text-krypt-muted">Loading…</div></Page>;

  const update = async <K extends keyof TraderConfig>(key: K, value: TraderConfig[K]): Promise<void> => {
    try {
      await window.krypt.config.update({ [key]: value } as Partial<TraderConfig>);
      await refresh.state();
    } catch (e: any) {
      toast.error(`${e?.message || e}`);
    }
  };

  return (
    <Page
      title="Settings"
      subtitle="App-level preferences: Paper | Live, the AI provider, startup behavior, notifications, and data. Each trading engine has its own page and its own on/off switch: Crypto and Scripts."
    >
      <Section
        title="Account"
        description="Paper | Live for the whole app. Paper is the default and needs no API key; Live is a deliberate checklist."
      >
        <AccountModePanel onNav={onNav} />
      </Section>

      <Section
        title="Backend"
        description="The Python process every engine runs inside. Restarting it stops and restarts ALL of them — it is not scoped to any single engine, which is why it lives here rather than on an engine page."
      >
        <Card>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
            <button onClick={() => void restartBackend()} className="krypt-btn-default">
              <RefreshCw className="h-4 w-4" /> Restart backend
            </button>
            <div className="text-[11px] text-krypt-dim">
              Status:{' '}
              <span className={backend.status === 'running' ? 'text-krypt-win' : 'text-krypt-warn'}>
                {backend.status}
              </span>
              {backend.status === 'running'
                ? ' — open positions keep their management passes; a restart briefly interrupts them.'
                : ' — a restart usually clears a stuck backend.'}
            </div>
          </div>
        </Card>
      </Section>

      <Section
        title="Appearance"
        description="How the glass renders. Live bends and frosts the page behind it; Frost keeps the look without the bending, for slower machines. Auto uses Live and steps down by itself if this machine can't hold its frame rate."
      >
        <GlassQualityCard />
      </Section>

      <Section title="Getting started">
        <GettingStartedCard onNav={onNav} />
      </Section>

      <Section title="App preferences">
        <Card>
          <div className="grid gap-2 md:grid-cols-2">
            <Switch
              label="Start with Windows"
              description="Launch Krypt PolyBot US at login (silent if Start Minimized is on)."
              checked={!!state?.startWithWindows}
              onChange={(v) => window.krypt.state.setStartWithWindows(v).then(refresh.state)}
            />
            <Switch
              label="Start minimized to tray"
              description="If autostarted, hide to tray on launch. Open from the tray icon."
              checked={!!state?.startMinimized}
              onChange={(v) => window.krypt.state.setStartMinimized(v).then(refresh.state)}
            />
            {/* The setting existed and was persisted for months with nothing
                reading it and no way to reach it. It is now wired to the real
                client — turning it off disconnects Rich Presence. */}
            <Switch
              label="Show Krypt PolyBot US on your Discord profile"
              description="Rich Presence: your Discord status reads “Auto-trading on Polymarket”. It never says which markets, which side, or how much. Off = the app does not connect to Discord at all."
              checked={state?.enableDiscordRpc !== false}
              onChange={(v) => window.krypt.state.setEnableDiscordRpc(v).then(refresh.state)}
            />
          </div>
        </Card>
      </Section>

      <Section
        title="Manual terminal"
        description="How the order ticket on a market page behaves."
      >
        <Card>
          <Switch
            label="Desktop notification when a standing instruction fires"
            description="Tells you even when the window is not focused — which is exactly when a stop matters. A desktop notification, not a webhook: it never leaves this machine."
            checked={config.terminalNotifications !== false}
            onChange={(v) => void update('terminalNotifications', v)}
          />
          <p className="mt-3 text-[11px] text-krypt-dim">
            Paper or real money for the ticket follows the account mode at the top of this page:
            in Paper the ticket is labelled PAPER and the backend refuses a live order outright,
            so a stale screen cannot spend real money behind your back.
          </p>
        </Card>
      </Section>

      <AiSettings config={config} update={update} />

      <Section
        title="Autopilot"
        description="Runs one of your agents on a schedule with the AI provider above."
      >
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <Sparkles className="h-4 w-4 text-krypt-purple" />
            <div className="min-w-0 flex-1 text-xs text-krypt-muted">
              Autopilot is {config.autopilotEnabled
                ? <span className="text-white">on</span>
                : 'off'}. It lives with your agents — its schedule, budget, mission and runs are on
              the AI Agents page, next to the rails and approvals that bind it.
            </div>
            {onNav && (
              <button type="button" className="krypt-btn-default text-xs" onClick={() => onNav('agents')} data-testid="settings-open-autopilot">
                Open AI Agents <ArrowRight className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        </Card>
      </Section>

      <DataPrivacy
        offline={!!config.terminalOfflineMode}
        onToggle={(v) => void update('terminalOfflineMode', v)}
      />

      <DangerZone busy={busy} setBusy={setBusy} />
    </Page>
  );
}

/**
 * The privacy panel (terminal guide §8).
 *
 * If the pitch is "we don't know what you trade", it has to be CHECKABLE. So
 * this names every host verbatim, says what each one is for, shows live
 * call/error/latency counts straight from the provider gate, and gives one
 * switch that turns the lot off.
 *
 * It also fixes a specific smell: those counters existed for a while with no
 * reader. A failure you count but never surface is not a safety mechanism.
 */
function DataPrivacy({
  offline, onToggle,
}: { offline: boolean; onToggle: (v: boolean) => void }) {
  const [health, setHealth] = useState<TerminalHealth | null>(null);

  useEffect(() => {
    let alive = true;
    let busy = false;
    async function tick() {
      if (busy) return;
      busy = true;
      try {
        const h = await window.krypt.terminal.health();
        if (alive) setHealth(h);
      } catch { /* leave the last-known counts up */
      } finally { busy = false; }
    }
    void tick();
    const id = window.setInterval(tick, 5000);
    return () => { alive = false; window.clearInterval(id); };
  }, []);

  const providers = health?.providers ?? [];
  const totalCalls = providers.reduce((n, p) => n + p.calls, 0);
  const totalErrors = providers.reduce((n, p) => n + p.errors, 0);
  const hits = providers.reduce((n, p) => n + p.cacheHits, 0);
  const misses = providers.reduce((n, p) => n + p.cacheMisses, 0);
  const cachePct = hits + misses > 0 ? Math.round((hits / (hits + misses)) * 100) : null;

  return (
    <Section
      title="Data & privacy"
      description="Every host this machine contacts for the terminal, and what each is used for."
    >
      <Card>
        <p className="mb-4 text-xs text-krypt-muted">
          There is no Krypt server in this path. Your machine talks to
          Polymarket US directly, and nothing about what you look at or trade is
          sent anywhere else. The counts below are live, so you can check that
          rather than take our word for it.
        </p>

        {providers.length === 0 ? (
          <div className="rounded-lg border border-dashed border-krypt-border p-4 text-center text-xs text-krypt-dim">
            No provider activity recorded yet — open Discover or a market and
            these counters start moving.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                  <th className="pb-2">Host</th>
                  <th className="pb-2">Used for</th>
                  <th className="pb-2 text-right">Requests</th>
                  <th className="pb-2 text-right">Errors</th>
                  <th className="pb-2 text-right">Median</th>
                  <th className="pb-2 text-right">Served from cache</th>
                </tr>
              </thead>
              <tbody>
                {providers.map((p) => {
                  const seen = p.cacheHits + p.cacheMisses;
                  return (
                    <tr key={p.host} className="border-b border-krypt-border/50 last:border-0">
                      <td className="py-2 font-mono text-xs text-white">
                        {p.host}
                        {p.parkedUntil && (
                          <span
                            className="ml-2 rounded-full border border-krypt-warn/40 px-1.5 py-0.5 text-[9px] uppercase tracking-wider text-krypt-warn"
                            title={`Rate-limited us; paused until ${p.parkedUntil}. Requests fail fast instead of queueing.`}
                          >
                            paused
                          </span>
                        )}
                      </td>
                      <td className="py-2 text-xs text-krypt-muted">{p.purpose}</td>
                      <td className="py-2 text-right font-mono text-krypt-muted">
                        {p.calls.toLocaleString()}
                      </td>
                      <td className={cls('py-2 text-right font-mono',
                        p.errors > 0 ? 'text-krypt-warn' : 'text-krypt-dim')}
                      >
                        {p.errors.toLocaleString()}
                      </td>
                      <td className="py-2 text-right font-mono text-krypt-muted">
                        <Val v={p.latencyMs} fmt={(n) => `${n.toFixed(0)}ms`} />
                      </td>
                      <td className="py-2 text-right font-mono text-krypt-muted">
                        {seen ? `${Math.round((p.cacheHits / seen) * 100)}%` : '—'}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {providers.length > 0 && (
          <div className="mt-3 text-[11px] text-krypt-dim">
            {totalCalls.toLocaleString()} request(s) this session,{' '}
            {totalErrors.toLocaleString()} error(s)
            {cachePct !== null && <> · {cachePct}% served from cache without a request</>}.
          </div>
        )}

        <div className="mt-4 border-t border-krypt-border pt-4">
          <Switch
            label="Offline mode — contact no providers"
            description="Stops every terminal request. Discover, charts, books and the tape go blank and say why; your watchlist, fill ledger and the trading engines are untouched."
            checked={offline}
            onChange={onToggle}
          />
          {offline && (
            <div className="mt-3 rounded-lg border border-krypt-warn/30 bg-krypt-warn/5 p-3 text-xs text-krypt-warn">
              Offline mode is on. The terminal is showing you nothing rather
              than showing you something stale — every panel explains which
              data is missing and why.
            </div>
          )}
        </div>
      </Card>
    </Section>
  );
}

function DangerZone({
  busy, setBusy,
}: { busy: boolean; setBusy: (b: boolean) => void }) {
  const toast = useToast();
  // Electron disables window.prompt() — it always returns null silently —
  // so the previous "type RESET to confirm" flow could never succeed and
  // the user just saw nothing happen. We use an in-app modal that
  // captures the typed phrase via a real <input>.
  const [showModal, setShowModal] = useState(false);
  const [phrase, setPhrase] = useState('');

  const openModal = (): void => {
    setPhrase('');
    setShowModal(true);
  };

  const cancel = (): void => {
    setShowModal(false);
    setPhrase('');
  };

  const confirm = async (): Promise<void> => {
    if (phrase.trim() !== 'RESET') {
      toast.error('Type RESET (uppercase) to confirm.');
      return;
    }
    setShowModal(false);
    setBusy(true);
    try {
      const r = await window.krypt.app.factoryReset();
      if (r.ok) {
        const summary = (r.data as { deleted?: Record<string, number> })?.deleted || {};
        const detail = Object.entries(summary)
          .filter(([k, v]) => !k.startsWith('_') && (v as number) > 0)
          .map(([k, v]) => `${k}: ${v}`)
          .join(', ');
        toast.success(detail
          ? `Wiped — ${detail}`
          : (r.message || 'Local data cleared (nothing to delete)'));
      } else {
        toast.error(r.message || 'Reset failed');
      }
    } catch (e: any) {
      toast.error(`${e?.message || e}`);
    } finally {
      setBusy(false);
      setPhrase('');
    }
  };

  return (
    <>
      <Section title="Danger zone">
        <Card>
          <div className="space-y-3 rounded-xl border border-rose-500/40 bg-rose-500/5 p-4">
            <div>
              <div className="text-sm font-semibold text-rose-300">
                Full reset
              </div>
              <p className="text-xs text-krypt-muted mt-1">
                Wipes all locally stored trading history, P&amp;L snapshots,
                bot runs. Your API key, profiles, and settings are
                preserved. Live Polymarket US positions will be re-imported on the
                next reconcile cycle. Useful for clearing inconsistent state
                from old builds before live testing.
              </p>
            </div>
            <button
              onClick={openModal}
              disabled={busy}
              className="inline-flex items-center gap-2 rounded-lg border border-rose-500/60 bg-rose-500/15 px-4 py-2 text-sm font-semibold text-rose-200 hover:bg-rose-500/25 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <RotateCcw className="h-4 w-4" />
              Full reset
            </button>
          </div>
        </Card>
      </Section>

      {showModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
          onClick={cancel}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-rose-500/50 bg-krypt-panel p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 className="text-lg font-semibold text-rose-300">
              Confirm full reset
            </h3>
            <div className="mt-3 space-y-2 text-sm text-krypt-muted">
              <p>This will permanently delete:</p>
              <ul className="ml-5 list-disc space-y-1">
                <li>all bot positions and trade history</li>
                <li>all bot runs (session P&amp;L)</li>
                <li>all P&amp;L snapshots</li>
              </ul>
              <p className="pt-2">
                Your API key, profiles, and settings are <strong>kept</strong>.
                Live Polymarket US positions will be re-imported on the next
                reconcile cycle.
              </p>
            </div>
            <label className="mt-4 block text-xs font-semibold uppercase tracking-wider text-krypt-muted">
              Type <span className="text-rose-300">RESET</span> to confirm
            </label>
            <input
              autoFocus
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void confirm();
                if (e.key === 'Escape') cancel();
              }}
              className="mt-1 w-full rounded-lg border border-rose-500/40 bg-black/30 px-3 py-2 text-sm font-mono text-rose-100 placeholder-krypt-muted/50 outline-none focus:border-rose-400"
              placeholder="RESET"
            />
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={cancel}
                className="rounded-lg border border-white/10 bg-white/5 px-4 py-2 text-sm font-semibold text-krypt-muted hover:bg-white/10"
              >
                Cancel
              </button>
              <button
                onClick={() => void confirm()}
                disabled={phrase.trim() !== 'RESET'}
                className="rounded-lg border border-rose-500/60 bg-rose-500/20 px-4 py-2 text-sm font-semibold text-rose-100 hover:bg-rose-500/30 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Wipe everything
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}


/**
 * Replay onboarding and the app tour. Both are UI only: a replay opens the
 * first-run setup from step 1 without resetting the disclaimer, any setting,
 * key or profile, or the page you are on, and nothing in it writes until you
 * act in a step, exactly as on first run (Onboarding.tsx lists every write).
 */
export function GettingStartedCard({ onNav }: { onNav?: (p: PageId) => void } = {}) {
  const { replayOnboarding, startTour } = useGuide();
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-[220px] flex-1">
          <div className="text-sm font-medium text-white">Setup and tour</div>
          <div className="text-xs text-krypt-muted">
            See the first-run setup again, or the tour of every page. Replaying changes nothing
            unless you act in a step.
          </div>
        </div>
        {onNav && (
          <button type="button" onClick={() => onNav('api')} className="krypt-btn-default" data-testid="settings-account-setup">
            <KeyRound className="h-4 w-4" /> Polymarket US account
          </button>
        )}
        <button type="button" onClick={startTour} className="krypt-btn-default" data-testid="settings-tour">
          <Compass className="h-4 w-4" /> Take the tour
        </button>
        <button type="button" onClick={replayOnboarding} className="krypt-btn-default" data-testid="settings-replay-onboarding">
          <PlayCircle className="h-4 w-4" /> Replay onboarding
        </button>
      </div>
    </Card>
  );
}

/** Settings → Appearance: Auto | Live | Frost, and what is actually in effect
 *  (Auto may have stepped down; the OS may be asking for less transparency). */
function GlassQualityCard() {
  const [st, setSt] = useState<GlassQualityState>(glassQualityState);
  useEffect(() => subscribeGlass(() => setSt(glassQualityState())), []);
  const now = st.reducedTransparency
    ? 'Frost — your system asks apps to reduce transparency.'
    : st.quality === 'auto' && st.autoDemoted
      ? 'Frost — Auto stepped down because this machine could not hold its frame rate with Live glass. Pick Auto again to re-measure.'
      : st.live ? 'Live glass.' : 'Frost.';
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-4">
        <GlassSegmented<GlassQuality>
          value={st.quality}
          onChange={(q) => setGlassQuality(q)}
          options={[
            { value: 'auto', label: 'Auto', title: 'Live, stepping down to Frost only if this machine needs it' },
            { value: 'live', label: 'Live', title: 'Always the full liquid glass' },
            { value: 'frost', label: 'Frost', title: 'No refraction: the lightest on the GPU' },
          ]}
        />
        <div className="text-[11px] text-krypt-muted">
          Now: <span className={st.live ? 'text-sky-300' : 'text-krypt-warn'}>{now}</span>
        </div>
      </div>
    </Card>
  );
}
