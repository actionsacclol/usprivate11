import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { Footprints, Maximize2, Minimize2, Route, Undo2, Volume2, VolumeX } from 'lucide-react';
import { useApp } from '../state/AppStateProvider';
import { hubSignals, readAutopilot, readMarkets, readSportsMarkets, type HubAutopilot, type HubData, type HubSportsbook } from './data';
import type { MarketRow } from './casino/sportsbook';
import {
  REPLAY_WINDOW_MS, activityBus, replayPlan, type ActivityRecord,
} from '../state/activity';
import { loadSavedBacktests, type SavedBacktest } from './library';
import { PaydayTracker, type PaydayEvent } from './payday';
import { subject } from './text';
import { isWindowVisible, onWindowVisibility } from '../state/visibility';
import { GlassButton } from '../components/glass/GlassButton';
import { GlassPanel } from '../components/glass/GlassPanel';

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// The frame every Agent Hub scene lives in: the page canvas, the control bar,
// and Fullscreen. There is deliberately no recorder (one was built and
// dropped), so Fullscreen's only job is a clean, chrome-free frame: controls
// fade out after a moment of stillness, and so does the cursor.
//
// The hub is read-only. Scenes see `useApp()` state plus the activity bus
// (state/activity.ts: past-tense events other pages published after a real
// action) and never call `window.krypt`. The stage itself only LISTENS and
// READS, and every call is classified by what it costs:
//
//   call                        cost                               rate
//   saved backtests (library)   none yet in PolyBot (no strategy   -
//                               library with saved verdicts)
//   autopilot.status()          in-memory + local SQLite + a       every 20 s, Casino  
//                               key-file exists check; no network  only, while mounted
//   ai.scoreboard()             one local SQLite read (ai_forecasts) on mount + every 60 s,
//    (PokerView, via `extra`)   + pure scoring; no network         High Stakes only
//   terminal.discover() ×2      network: Polymarket US's market data,    every 90 s, Casino
//    (the sportsbook board)     through the backend; read only      only, while visible
//   terminal.market() each      network, one read per open play-   every 5 min, Casino
//    (sportsbook tickets)       chip ticket (≤ 10); read only      only, while visible
//   scripts.onLog (push)        none: the backend pushes it anyway listener only
//   activity bus                none: renderer memory              listener only
//    (incl. agent tool calls — `mcp:toolCall`, pushed anyway, mirrored by App.tsx)
//
// Deliberately NOT read here: `mcpStatus` (runs the agent day-loss check,
// which fetches Polymarket marks or the live portfolio when trade mode is on),
// `trading.status` (can refresh the Polymarket balance through a 60 s cache),
// `mcpActivity` (fetches Polymarket marks for the paper book). Nothing here can
// place an order, change a setting, or start a job that costs CPU or API
// quota: a visualiser that quietly ran the Coin Optimizer every 20 s (as the
// simulation build's did) is a background cost the user never asked for.

/** What a scene engine must offer the stage. Both scenes composite their
 *  HUD into one 2D canvas, so the stage only ever hands them a canvas. */
export interface HubSceneEngine {
  fps: number;
  start(): void;
  /** Pause the render loop; start() resumes it. */
  stop(): void;
  ingest(d: HubData): void;
  setOutput(canvas: HTMLCanvasElement, w: number, h: number, mode: 'page' | 'cinema'): void;
  /** A real event worth celebrating (see payday.ts); the amount is real. */
  payday(ev: PaydayEvent): void;
  /** A past-tense event from the activity bus; `replay` = it happened
   *  before this scene opened (catch-up). */
  activity(rec: ActivityRecord, replay: boolean): void;
  /** A running script's log() lines, for scenes that show them. */
  scriptLog?(name: string | null, lines: string[]): void;
  setTour(on: boolean): void;
  resetView(): void;
  onTourChange: ((on: boolean) => void) | null;
  onFocusChange: ((label: string | null) => void) | null;
  /** First person (walk.ts), for scenes you can stand in. A camera only:
   *  walking reads nothing and changes nothing the hub doesn't already. */
  setWalk?(on: boolean): void;
  onWalkChange?: ((on: boolean) => void) | null;
  /** Esc, offered to the scene first (a conversation ends before the walk
   *  does). True = it was taken. */
  escape?(): boolean;
  /** The markets the scene waits on (the Casino's open tickets), and their
   *  latest read. */
  marketsWanted?(): string[];
  marketsRead?(rows: MarketRow[]): void;
  /** Scenes with sound (the Casino): on or off, remembered. */
  soundMuted?(): boolean;
  setSoundMuted?(m: boolean): void;
  dispose(): void;
}

interface Props {
  create: (canvas: HTMLCanvasElement, w: number, h: number) => HubSceneEngine;
  hint: string;
  loading: string;
  tourLabel: string;
  tourTitle: string;
  frameBg: string;
  /** Read Autopilot's status (cheap, see above) for the bridge. */
  wantsAutopilot?: boolean;
  /** Read Polymarket's sports markets for the sportsbook (network, see above). */
  wantsSportsbook?: boolean;
  /** The scene's own walk keys (the Casino's hotbar), added to the hint shown
   *  on the frame as a walk starts. Not to the line above the frame: it's full,
   *  and a longer one wraps the buttons onto a second row. */
  walkKeys?: string;
  /** Scene-specific inputs a view reads itself (cheap, listed above), merged
   *  into every ingest. */
  extra?: Partial<HubData>;
  /** Scene-specific controls, in the bar above the frame. */
  controls?: ReactNode;
}

const AUTOPILOT_POLL_MS = 20_000;
const SPORTSBOOK_POLL_MS = 90_000;
const TICKETS_POLL_MS = 5 * 60_000;

const IDLE_HIDE_MS = 2_200;

/** A HUD drawn before the webfonts land bakes a fallback face into the first
 *  frames; wait briefly for them. */
async function fontsReady(): Promise<void> {
  const loads = [
    '700 40px "Chakra Petch"', '600 20px "Chakra Petch"', '600 20px "JetBrains Mono"',
    '500 20px "JetBrains Mono"', '400 20px "Press Start 2P"',
  ].map((f) => document.fonts.load(f).catch(() => []));
  await Promise.race([Promise.all(loads), new Promise((r) => setTimeout(r, 1500))]);
}

function dpr(): number {
  return Math.min(2, window.devicePixelRatio || 1);
}

export function HubStage({ create, hint, loading, tourLabel, tourTitle, frameBg, wantsAutopilot, wantsSportsbook, walkKeys, extra, controls: sceneControls }: Props) {
  const { positions, account, config } = useApp();
  const signals = useMemo(() => hubSignals(positions), [positions]);
  const createRef = useRef(create);
  const paydayRef = useRef(new PaydayTracker());
  const [library, setLibrary] = useState<SavedBacktest[]>([]);
  const [autopilot, setAutopilot] = useState<HubAutopilot | null | undefined>(undefined);
  const [sportsbook, setSportsbook] = useState<HubSportsbook | null | undefined>(undefined);

  const wrapRef = useRef<HTMLDivElement>(null);
  const pageCanvas = useRef<HTMLCanvasElement>(null);
  const fullCanvas = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<HubSceneEngine | null>(null);
  const mountedRef = useRef(true);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const [tour, setTour] = useState(false);
  const [fps, setFps] = useState(0);
  const [full, setFull] = useState(false);
  const [walk, setWalk] = useState(false);
  const [canWalk, setCanWalk] = useState(false);
  /** null = this scene has no sound. */
  const [muted, setMuted] = useState<boolean | null>(null);
  const [walkHint, setWalkHint] = useState(false);
  const [controls, setControls] = useState(true);
  const hideTimer = useRef<number>(0);
  const hovering = useRef(false);

  const pageSize = useCallback((): [number, number] => {
    const el = wrapRef.current;
    return [Math.max(64, (el?.clientWidth ?? 800) * dpr()), Math.max(64, (el?.clientHeight ?? 500) * dpr())];
  }, []);

  // ── engine lifecycle ─────────────────────────────────────────────────────
  useEffect(() => {
    mountedRef.current = true;
    let engine: HubSceneEngine | null = null;
    let cancelled = false;
    void fontsReady().then(() => {
      if (cancelled || !pageCanvas.current) return;
      try {
        const [w, h] = pageSize();
        engine = createRef.current(pageCanvas.current, w, h);
      } catch (e) {
        setError(errText(e));
        return;
      }
      engine.onFocusChange = (r) => { if (mountedRef.current) setFocused(r); };
      engine.onTourChange = (on) => { if (mountedRef.current) setTour(on); };
      engine.onWalkChange = (on) => { if (mountedRef.current) setWalk(on); };
      setCanWalk(typeof engine.setWalk === 'function');
      setMuted(engine.setSoundMuted ? !!engine.soundMuted?.() : null);
      engineRef.current = engine;
      // Dev-only handle for poking the scene from DevTools; never in a build.
      if (import.meta.env.DEV) (window as unknown as { __agentHub?: HubSceneEngine }).__agentHub = engine;
      // An app that starts in the tray opens the hub hidden: don't render
      // until someone can see it (the visibility effect below starts it).
      if (isWindowVisible()) engine.start();
      setReady(true);
    });
    const fpsI = window.setInterval(() => setFps(engineRef.current?.fps ?? 0), 500);
    return () => {
      cancelled = true;
      mountedRef.current = false;
      window.clearInterval(fpsI);
      engine?.dispose();
      engineRef.current = null;
      // A remount (StrictMode, Fast Refresh) builds a NEW engine; the
      // effects that point it at a canvas key off `ready`, so it must flip.
      setReady(false);
    };
  }, [pageSize]);

  // Minimized or closed to the tray: stop rendering. Each frame is a 2048²
  // shadow map, a 4x-MSAA HalfFloat target and bloom, and with Electron's
  // backgroundThrottling off nothing else would ever stop it (main.ts).
  useEffect(() => {
    if (!ready) return;
    const apply = (visible: boolean) => {
      const e = engineRef.current;
      if (!e) return;
      if (visible) e.start();
      else e.stop();
    };
    apply(isWindowVisible()); // a change between engine creation and now
    return onWindowVisibility(apply);
  }, [ready]);

  // The saved backtest results: one cached file read (library.ts), never a
  // re-run.
  useEffect(() => {
    let alive = true;
    void loadSavedBacktests().then((rows) => { if (alive) setLibrary(rows); });
    return () => { alive = false; };
  }, []);

  // Autopilot's status for the bridge: a cheap read (no network), at a
  // modest rate, only for a scene that shows it and only while it's open.
  useEffect(() => {
    if (!wantsAutopilot) return;
    let alive = true;
    const pull = async () => {
      try {
        const s = await readAutopilot();
        // undefined: this build has no Autopilot call, so the bridge stays as
        // it would with Autopilot off rather than polling nothing forever
        if (alive) setAutopilot(s === undefined ? null : s);
        if (s === undefined) window.clearInterval(i);
      } catch {
        if (alive) setAutopilot(null); // backend down: the bridge says nothing
      }
    };
    void pull();
    const i = window.setInterval(pull, AUTOPILOT_POLL_MS);
    return () => { alive = false; window.clearInterval(i); };
  }, [wantsAutopilot]);

  // The sportsbook's board: Polymarket's sports markets, read through the
  // backend (the same call as the Discover page), only while someone can see
  // the casino. Open play-chip tickets read their own market now and then, to
  // settle on the real result.
  useEffect(() => {
    if (!wantsSportsbook || !ready) return;
    let alive = true;
    const board = async () => {
      if (!isWindowVisible()) return;
      try {
        const s = await readSportsMarkets();
        if (!alive) return;
        if (s === undefined) { window.clearInterval(bi); return; }
        setSportsbook(s);
      } catch {
        if (alive) setSportsbook(null);
      }
    };
    const tickets = async () => {
      const ids = engineRef.current?.marketsWanted?.() ?? [];
      if (!ids.length || !isWindowVisible()) return;
      const rows = await readMarkets(ids).catch(() => undefined);
      if (alive && rows) engineRef.current?.marketsRead?.(rows);
    };
    void board();
    const first = window.setTimeout(() => { void tickets(); }, 8_000);
    const bi = window.setInterval(board, SPORTSBOOK_POLL_MS);
    const ti = window.setInterval(tickets, TICKETS_POLL_MS);
    return () => { alive = false; window.clearTimeout(first); window.clearInterval(bi); window.clearInterval(ti); };
  }, [wantsSportsbook, ready]);

  // Every app-state change goes in; the engine diffs, samples and queues.
  useEffect(() => {
    engineRef.current?.ingest({
      signals, positions, account, config, library, autopilot,
      ...(sportsbook !== undefined ? { sportsbook } : {}), ...extra,
    });
  }, [signals, positions, account, config, library, autopilot, sportsbook, extra, ready]);

  // The activity bus: what just happened elsewhere in the app, replayed as
  // quick beats (oldest first) so opening the hub after a backtest shows the
  // casino doing it; then everything new, live. Keyed on `ready` because a
  // remount builds a fresh engine that needs its own catch-up.
  const scriptNames = useRef(new Map<string, string>());
  useEffect(() => {
    if (!ready) return;
    const seen = new Set<number>();
    const deliver = (rec: ActivityRecord, replay: boolean) => {
      if (seen.has(rec.seq)) return;
      seen.add(rec.seq);
      if (rec.ev.kind === 'script' && rec.ev.id && rec.ev.name) scriptNames.current.set(rec.ev.id, rec.ev.name);
      engineRef.current?.activity(rec, replay);
    };
    const recent = activityBus.recent(REPLAY_WINDOW_MS);
    for (const r of recent) if (r.ev.kind === 'script' && r.ev.id && r.ev.name) scriptNames.current.set(r.ev.id, r.ev.name);
    const timers = replayPlan(recent).map(({ rec, delayMs }) => window.setTimeout(() => deliver(rec, true), delayMs));
    const off = activityBus.subscribe((rec) => deliver(rec, false));
    // A script's own log() lines: pushed by the backend whether or not
    // anyone listens, and too chatty for the bus's ring, so live only.
    const offLog = window.krypt?.scripts?.onLog?.((d) => {
      try {
        engineRef.current?.scriptLog?.(scriptNames.current.get(d.id) ?? null, Array.isArray(d.lines) ? d.lines : []);
      } catch { /* a bad line never breaks the scene */ }
    });
    return () => {
      off();
      offLog?.();
      timers.forEach((t) => window.clearTimeout(t));
    };
  }, [ready]);

  // PAYDAY comes from the data, never a button: a big real settlement or a
  // new +$100 session step (payday.ts). The tracker runs even before the
  // engine is up, so opening the page primes it and old wins don't fire.
  useEffect(() => {
    const ev = paydayRef.current.check(positions, account, (p) => subject(p.ticker, p.title), performance.now());
    if (ev) engineRef.current?.payday(ev);
  }, [positions, account]);

  const fullSize = useCallback((): [number, number] => [window.innerWidth * dpr(), window.innerHeight * dpr()], []);

  // page frame follows its wrapper; fullscreen follows the window
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const e = engineRef.current;
      if (!e || full || !pageCanvas.current) return;
      const [w, h] = pageSize();
      e.setOutput(pageCanvas.current, w, h, 'page');
    });
    ro.observe(el);
    const onResize = () => {
      const e = engineRef.current;
      if (!e || !full || !fullCanvas.current) return;
      const [w, h] = fullSize();
      e.setOutput(fullCanvas.current, w, h, 'cinema');
    };
    window.addEventListener('resize', onResize);
    return () => { ro.disconnect(); window.removeEventListener('resize', onResize); };
  }, [full, pageSize, fullSize, ready]);

  // page ⇄ fullscreen: one engine, re-targeted, so the story carries on
  useEffect(() => {
    const e = engineRef.current;
    if (!e || !ready) return;
    if (full && fullCanvas.current) {
      const [w, h] = fullSize();
      e.setOutput(fullCanvas.current, w, h, 'cinema');
    } else if (!full && pageCanvas.current) {
      const [w, h] = pageSize();
      e.setOutput(pageCanvas.current, w, h, 'page');
    }
  }, [full, ready, pageSize, fullSize]);

  useEffect(() => { engineRef.current?.setTour(tour); }, [tour, ready]);
  useEffect(() => { engineRef.current?.setWalk?.(walk); }, [walk, ready]);
  // The controls line shows when a walk starts, then gets out of the shot.
  useEffect(() => {
    if (!walk) { setWalkHint(false); return; }
    setWalkHint(true);
    const t = window.setTimeout(() => setWalkHint(false), 5000);
    return () => window.clearTimeout(t);
  }, [walk]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
      if (e.key === 'Escape') {
        // Out of the walk first: the first Esc only frees the mouse (the
        // browser keeps that one), the next leaves first person, the next
        // leaves fullscreen.
        if (engineRef.current?.escape?.()) return;
        if (walk) { setWalk(false); return; }
        if (full) setFull(false);
        else { setTour(false); engineRef.current?.resetView(); }
        return;
      }
      if (e.key.toLowerCase() === 'm' && engineRef.current?.setSoundMuted) {
        const m = !engineRef.current.soundMuted?.();
        engineRef.current.setSoundMuted(m);
        setMuted(m);
        return;
      }
      if (!full) return;
      if (e.key.toLowerCase() === 't') setTour((v) => !v);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [full, walk]);

  const poke = useCallback(() => {
    setControls(true);
    window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => { if (!hovering.current) setControls(false); }, IDLE_HIDE_MS);
  }, []);
  useEffect(() => { if (full) poke(); return () => window.clearTimeout(hideTimer.current); }, [full, poke]);

  const hover = { onMouseEnter: () => { hovering.current = true; }, onMouseLeave: () => { hovering.current = false; } };

  return (
    <div className="flex flex-col gap-2">
      {/* Controls sit above the frame, not on it: the frame is the shot. */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[11px] text-krypt-dim">
          {walk ? 'walking · click to look · WASD move · ← → turn · E talk or play · Shift run · EXIT door or Esc' : hint}{ready ? ` · ${fps} fps` : ''}
        </span>
        {focused && (
          <GlassButton
            live={false}
            variant="active"
            onClick={() => engineRef.current?.resetView()}
            className="px-2.5 py-1 text-xs"
          >
            <Undo2 className="h-3.5 w-3.5" /> Back <span className="text-white/50">· following {focused} · Esc</span>
          </GlassButton>
        )}
        <div className="ml-auto flex items-center gap-2">
          {sceneControls}
          {canWalk && <WalkBtn walk={walk} setWalk={setWalk} />}
          {muted !== null && <SoundBtn muted={muted} onToggle={() => { const m = !muted; engineRef.current?.setSoundMuted?.(m); setMuted(m); }} />}
          <Controls tour={tour} setTour={setTour} tourLabel={tourLabel} tourTitle={tourTitle} />
          <ToolBtn onClick={() => setFull(true)} title="Fill the window with the scene (Esc to exit)">
            <Maximize2 className="h-3.5 w-3.5" /> Fullscreen
          </ToolBtn>
        </div>
      </div>
      <div
        ref={wrapRef}
        className="relative h-[calc(100vh-292px)] min-h-[440px] overflow-hidden rounded-2xl border border-white/10 shadow-[0_30px_60px_-30px_rgba(0,0,0,0.9),0_0_0_1px_rgba(59,130,246,0.08)]"
        style={{ background: frameBg }}
      >
        <canvas
          ref={pageCanvas}
          className={`absolute inset-0 h-full w-full touch-none ${walk ? 'cursor-crosshair' : 'cursor-grab active:cursor-grabbing'}`}
          style={{ visibility: full ? 'hidden' : 'visible' }}
        />
        {!full && <WalkHint show={walkHint} keys={walkKeys} />}
        {!ready && !error && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <div className="animate-pulse font-pixel text-xs text-krypt-purple">{loading}</div>
          </div>
        )}
        {error && (
          <div className="absolute inset-0 grid place-items-center text-sm text-krypt-loss">WebGL could not start: {error}</div>
        )}
      </div>

      {full && createPortal(
        <div
          className="fixed inset-0 z-[66] select-none"
          // Frameless window: a title-bar drag region underneath an overlay
          // still swallows clicks in Electron unless the overlay opts out.
          style={{ WebkitAppRegion: 'no-drag', cursor: controls ? 'default' : 'none', background: frameBg } as CSSProperties}
          onMouseMove={poke}
        >
          <canvas ref={fullCanvas} className="absolute inset-0 h-full w-full touch-none" />
          <WalkHint show={walkHint} keys={walkKeys} />
          {/* The money shot: one live-glass bar bending the running three.js
              scene beneath it (Chromium's backdrop-filter reads a WebGL
              canvas like any other pixels). Its buttons are bare, so the
              bar is a single lens rather than lenses nested in a lens. The
              fade runs on the glass element itself — opacity on an ancestor
              would make that ancestor the backdrop root and blank the bend. */}
          <div className="absolute bottom-5 right-5" {...hover}>
            <GlassPanel
              preset="modal"
              display="flex"
              tint="linear-gradient(180deg, rgba(14,18,36,0.34), rgba(8,8,16,0.44))"
              optics={FULL_BAR_OPTICS}
              bendPx={18}
              className={`items-center gap-1 rounded-[22px] p-2 transition-all duration-300 ${controls ? 'translate-y-0 opacity-100' : 'pointer-events-none translate-y-4 opacity-0'}`}
            >
              {canWalk && <WalkBtn bare walk={walk} setWalk={setWalk} />}
              {muted !== null && <SoundBtn bare muted={muted} onToggle={() => { const m = !muted; engineRef.current?.setSoundMuted?.(m); setMuted(m); }} />}
              <Controls bare tour={tour} setTour={setTour} tourLabel={tourLabel} tourTitle={`${tourTitle} (T)`} />
              <ToolBtn bare onClick={() => setFull(false)} title="Exit fullscreen (Esc)">
                <Minimize2 className="h-3.5 w-3.5" /> Exit
              </ToolBtn>
              <span className="px-2 font-mono text-[10px] text-white/60 [text-shadow:0_1px_6px_rgba(0,0,0,0.8)]">{fps} fps · Esc exit · T {tourLabel.toLowerCase()}</span>
            </GlassPanel>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

/** The fullscreen bar is one wide lens over a moving scene: a deeper rim and
 *  stronger colour split than the in-page chrome, a lighter frost so the
 *  scene's lights still read through it. */
const FULL_BAR_OPTICS = {
  depth: 0.42, curvature: 0.18, bend: 0.7, bendWidth: 0.22, dispersion: 0.75,
  frost: 6, saturate: 1.8, sheen: 0.6, glow: 0.18,
};

function Controls({ tour, setTour, tourLabel, tourTitle, bare }: {
  tour: boolean; setTour: (fn: (v: boolean) => boolean) => void; tourLabel: string; tourTitle: string;
  /** Inside the fullscreen glass bar: plain buttons, no glass of their own. */
  bare?: boolean;
}) {
  return (
    <ToolBtn bare={bare} active={tour} onClick={() => setTour((v) => !v)} title={tourTitle}>
      <Route className="h-3.5 w-3.5" /> {tourLabel}
    </ToolBtn>
  );
}

/** Sound on/off, for a scene that has it. Icon only: the toolbar is full. */
function SoundBtn({ muted, onToggle, bare }: { muted: boolean; onToggle: () => void; bare?: boolean }) {
  return (
    <ToolBtn
      bare={bare}
      active={!muted}
      onClick={onToggle}
      title={`${muted ? 'Sound off' : 'Sound on'} (M) · music by Kevin MacLeod (incompetech.com), CC BY 4.0`}
    >
      {muted ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
    </ToolBtn>
  );
}

function WalkBtn({ walk, setWalk, bare }: { walk: boolean; setWalk: (fn: (v: boolean) => boolean) => void; bare?: boolean }) {
  return (
    <ToolBtn
      bare={bare}
      active={walk}
      onClick={() => setWalk((v) => !v)}
      title="Walk it in first person: WASD to move, mouse or ← → to look, E to talk to the crew or play a table, Shift to run, an EXIT door or Esc to stop"
    >
      <Footprints className="h-3.5 w-3.5" /> Walk
    </ToolBtn>
  );
}

/** The walk controls, shown for a few seconds when a walk starts: on the
 *  frame, not in it, and gone before anyone records. */
function WalkHint({ show, keys }: { show: boolean; keys?: string }) {
  return (
    <div
      className={`pointer-events-none absolute left-1/2 top-4 -translate-x-1/2 rounded-full border border-white/15 bg-black/55 px-4 py-1.5 font-mono text-[11px] text-white/85 backdrop-blur transition-opacity duration-700 ${show ? 'opacity-100' : 'opacity-0'}`}
    >
      Click to look · WASD move · ← → turn · E talk or play · {keys ? `${keys} · ` : ''}Shift run · wheel zoom · EXIT door or Esc to leave
    </div>
  );
}

function ToolBtn({ active, onClick, title, children, bare }: {
  active?: boolean; onClick: () => void; title?: string; children: ReactNode; bare?: boolean;
}) {
  if (bare) {
    return (
      <button
        onClick={onClick}
        title={title}
        className={`flex items-center gap-1.5 rounded-[14px] px-3 py-1.5 text-xs font-medium transition-all active:scale-95 ${active ? 'bg-krypt-purple/35 text-white shadow-[inset_0_0_0_1px_rgba(59,130,246,0.6),0_0_18px_-4px_rgba(59,130,246,0.8)]' : 'text-white/80 hover:bg-white/10 hover:text-white'}`}
      >
        {children}
      </button>
    );
  }
  return (
    // Frost, not live glass: on this page the GPU already renders a three.js
    // scene every frame, and these three sat at ~0.6 ms/frame live — the
    // difference between ~100 fps and the 90 fps floor. Same material family;
    // the live glass is spent on the view switch and the fullscreen bar.
    <GlassButton live={false} variant={active ? 'active' : 'default'} onClick={onClick} title={title} className="px-2.5 py-1.5 text-xs">
      {children}
    </GlassButton>
  );
}
