import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import type { BotPosition } from '@shared/types';
import type { HubSignal } from '../data';
import { balanceOf, hudNumbers, type HubData } from '../data';
import type { HubSceneEngine } from '../HubStage';
import { paydayLine, type PaydayEvent } from '../payday';
import type { ActivityRecord } from '../../state/activity';
import { beatFor } from '../engine/jobs';
import { cacheMeasureText, subject } from '../text';
import { drawHud, type FeedLine, type Tone } from '../engine/overlay';
import { Particles, Shockwaves } from './fx';
import { Herd, type Pose } from './herd';
import { ToteBoard, type ToteRow } from './tote';
import {
  CAT_COLOR, LANES, LH, LOSS_POS, R0, TOTE_POS, TW, Track, WINS_POS, X_CHUTE, X_FINISH, X_GATE,
  catOf, finishS, gateS, lapLength, laneOffset, silksOf, trackPoint, type Cat,
} from './track';
import {
  drawFlash, drawFloats, drawLaneLabels, drawLegend, drawMarker, drawTags, drawWell, layoutFloats, type Box, type Float, type Tag,
} from './overlay';

// The Race Track: every signal the bot sees walks up to the starting gate
// (traded ones load and break, skipped ones are scratched), every trade it
// takes is a racer in the lane its price puts it in, and every settlement
// is decided at the wire — a win crosses under a photo flash and confetti
// with its real amount, a loss pulls up and its dust goes to the LOSSES
// paddock.
//
// What a racer's progress means, honestly: a position carries no time-to-
// resolution (BotPosition has createdAt and resolvedAt, never an end date),
// so a racer does NOT show "how close to settling" it is. Like the old
// orbit, it laps its price lane at a pace set by that price (favourites on
// the rail run quicker), and only the real settlement sends it to the wire.
//
// A busy bot can open and settle positions faster than anyone can watch, and
// a 15-minute crypto trade can settle seconds after it opens. So, as in the
// other scenes, the SHOW is sampled and the COUNTS are not: a capped field,
// at most one or two arrivals and finishes a second, and every racer runs
// long enough to be read before it goes to the wire. The tote board, the
// winner's circle and the paddock count everything.

const MAX_RACERS = 30;
const MIN_LIFE = 4.5;
const MAX_RUNNERS = 3;

const MOMO = '#EC4899';

const COATS = ['#4A2C1A', '#5E3620', '#7C4A2A', '#2B2A2E', '#8E8C92', '#3A2418', '#6B3F22'];

type RacerState = 'break' | 'race' | 'sprint' | 'won' | 'pullup' | 'fade';

interface Racer {
  key: string;
  posId: number | null;
  sigId: number | null;
  ticker: string;
  dir: 'yes' | 'no';
  label: string;
  cat: Cat;
  price: number | null;
  /** Lane: distance out from the inner rail. */
  off: number;
  /** Distance run along its lane. */
  s: number;
  /** Cruising speed on its lane (units/s), from its price. */
  pace: number;
  speed: number;
  /** Sprint: distance left to the wire, and the speed to cover it. */
  toWire: number;
  kick: number;
  pose: Pose;
  pos: THREE.Vector3;
  yaw: number;
  dust: number;
  born: number;
  state: RacerState;
  stateT: number;
  finaleAt: number | null;
  won: boolean | null;
  pnl: number | null;
}

interface Runner {
  sig: HubSignal;
  color: string;
  traded: boolean;
  cat: Cat;
  off: number;
  stall: number;
  t: number;
  dur: number;
  label: string;
  pose: Pose;
  pos: THREE.Vector3;
  prev: THREE.Vector3;
  yaw: number;
  scratched: boolean;
}

/** A camera shot the cinematic holds on (a finish, a pull-up, PAYDAY). */
interface Shot { pos: THREE.Vector3; t: number; hold: number; dist: number; pitch: number; yaw: number; id: number }

function damp(a: number, b: number, k: number, dt: number): number {
  return b + (a - b) * Math.exp(-k * dt);
}

function wrapPi(a: number): number {
  return a - Math.round(a / (Math.PI * 2)) * Math.PI * 2;
}

/** Polymarket prices live in 1..99; anything else is "no price", never 0¢. */
function priceOf(c: number | null | undefined): number | null {
  return typeof c === 'number' && c >= 1 && c <= 99 ? c : null;
}

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}

/** Stake → horse size, on the old planet-size curve. */
function sizeOf(stake: number): number {
  const s = THREE.MathUtils.clamp(0.13 + Math.sqrt(Math.max(0, stake)) * 0.045, 0.13, 0.42);
  return 1.12 + ((s - 0.13) / 0.29) * 0.42;
}

/** Lap time from price: the old orbit's "inner lanes circle faster". */
function paceOf(price: number | null, off: number): number {
  const p = price ?? 40;
  const lap = 15 + (1 - p / 100) * 13;
  return lapLength(off) / lap;
}

function bez(a: THREE.Vector3, b: THREE.Vector3, c: THREE.Vector3, d: THREE.Vector3, t: number, out: THREE.Vector3): THREE.Vector3 {
  const u = 1 - t;
  const w0 = u * u * u, w1 = 3 * u * u * t, w2 = 3 * u * t * t, w3 = t * t * t;
  return out.set(
    a.x * w0 + b.x * w1 + c.x * w2 + d.x * w3,
    0,
    a.z * w0 + b.z * w1 + c.z * w2 + d.z * w3,
  );
}

const ease = (x: number) => x * x * (3 - 2 * x);

export class RaceTrackEngine implements HubSceneEngine {
  private out: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private W = 1280;
  private H = 720;
  private glCanvas = document.createElement('canvas');
  private renderer: THREE.WebGLRenderer;
  private composer: EffectComposer;
  private bloom: UnrealBloomPass;
  private rt: THREE.WebGLRenderTarget;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(38, 16 / 9, 0.1, 400);

  private track = new Track();
  private herd: Herd;
  private tote = new ToteBoard();
  private particles = new Particles(3200);
  private waves = new Shockwaves(12);
  private racers: Racer[] = [];
  private runners: Runner[] = [];

  // data diffing
  private seenSig = new Set<string>();
  private seenPos = new Map<number, string>();
  private primed = false;
  private runId: number | null = null;
  private latest: HubData | null = null;
  private posById = new Map<number, BotPosition>();
  private sigQ: HubSignal[] = [];
  private openQ: BotPosition[] = [];
  private runnerCd = 0.5;
  private spawnCd = 0.3;
  private finaleCd = 0;
  // every settlement, staged or not
  private wins = 0;
  private losses = 0;
  private winUsd = 0;
  private lossUsd = 0;

  // presentation
  private feed: FeedLine[] = [];
  private floats: (Float & { wx: number; wy: number; wz: number })[] = [];
  private banner: { text: string; sub: string; t: number } | null = null;
  private pnlPulse = 0;
  private lastPnl: number | null = null;
  private shot: Shot | null = null;
  private shotId = 0;
  private lastBreak: Racer | null = null;
  private stormT = 0;
  private flash2d = 0;
  private toteT = -1;
  private timers: { at: number; fn: () => void }[] = [];

  // camera
  private yaw = 0;
  private pitch = 0.86;
  private dist = 33;
  private fov = 38;
  private target = new THREE.Vector3(2.6, 0, -1.5);
  private userYaw = 0;
  private userPitch = 0;
  private userZoom = 1;
  private tourYaw = 0;
  private cutId = -1;
  private follow: Racer | null = null;
  private tour = false;
  private drag: { x: number; y: number; moved: boolean; yaw: number; pitch: number } | null = null;
  private detach: (() => void) | null = null;

  private raf = 0;
  private running = false;
  private last = 0;
  private time = 0;
  private frameErr = false;
  private fpsFrames = 0;
  private fpsAcc = 0;
  fps = 0;
  onTourChange: ((on: boolean) => void) | null = null;
  onFocusChange: ((label: string | null) => void) | null = null;

  private _v = new THREE.Vector3();
  private _v2 = new THREE.Vector3();
  private _v3 = new THREE.Vector3();
  private _pts = [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()];

  constructor(canvas: HTMLCanvasElement, w: number, h: number) {
    this.out = canvas;
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) throw new Error('2D canvas unavailable');
    this.ctx = cacheMeasureText(ctx);

    this.renderer = new THREE.WebGLRenderer({ canvas: this.glCanvas, antialias: false, alpha: false, stencil: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;

    this.scene.background = new THREE.Color('#04060d');
    // floodlit night: a cool sky fill, a warm key from the stand's side,
    // a cool rim from across the track. No shadow maps (see track.ts).
    this.scene.add(new THREE.HemisphereLight('#8EA2D8', '#20160E', 0.75));
    const key = new THREE.DirectionalLight('#FFF0D6', 2.1);
    key.position.set(-8, 16, 12);
    const rim = new THREE.DirectionalLight('#A9C4FF', 0.9);
    rim.position.set(12, 10, -14);
    this.scene.add(key, rim);
    this.herd = new Herd(this.track.glowTex, 48);
    this.scene.add(this.track.group, this.tote.mesh, this.herd.group, this.particles.points, this.waves.group);

    this.rt = new THREE.WebGLRenderTarget(w, h, { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(this.renderer, this.rt);
    this.composer.addPass(new RenderPass(this.scene, this.camera));
    // floodlights, the stand's strip lights, the tote bulbs and the silks
    // bloom; the dirt and the turf stay below the threshold
    this.bloom = new UnrealBloomPass(new THREE.Vector2(w, h), 0.62, 0.42, 0.88);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());

    this.setOutput(canvas, w, h, 'page');
  }

  // ── HubSceneEngine ───────────────────────────────────────────────────────

  start(): void {
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    const loop = () => {
      if (!this.running) return;
      this.raf = requestAnimationFrame(loop);
      this.frame();
    };
    this.raf = requestAnimationFrame(loop);
  }

  /** Pause the render loop while nobody can see the window (HubStage,
   *  off state/visibility.ts); start() resumes it. Everything else stays
   *  live: ingest/activity keep filling their (capped) queues. */
  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  ingest(d: HubData): void {
    this.latest = d;
    const runId = d.account?.sessionRunId ?? 0;
    if (this.primed && this.runId !== null && runId !== this.runId) {
      // a new session is a fresh card's worth of counts
      this.seenSig.clear();
      this.seenPos.clear();
      this.primed = false;
      this.wins = this.losses = 0;
      this.winUsd = this.lossUsd = 0;
    }
    this.runId = runId;

    this.posById.clear();
    for (const p of d.positions) this.posById.set(p.id, p);

    for (const s of d.signals.slice(0, 60)) {
      const key = `${s.source}:${s.id}`;
      if (this.seenSig.has(key)) continue;
      this.seenSig.add(key);
      if (!this.primed) continue;
      this.sigQ.push(s);
    }
    if (this.sigQ.length > 3) this.sigQ.length = 3;
    if (this.seenSig.size > 4000) this.seenSig = new Set(d.signals.map((x) => `${x.source}:${x.id}`));

    for (const p of d.positions) {
      const sig = p.resolved ? `r:${p.outcomeCorrect}` : 'o';
      const prev = this.seenPos.get(p.id);
      this.seenPos.set(p.id, sig);
      if (!this.primed || prev === sig) continue;
      const live = p.status === 'filled' || p.status === 'partial';
      if (prev === undefined && live && !p.resolved) {
        this.openQ.push(p);
        if (this.openQ.length > 6) this.openQ.shift();
      }
      if (p.resolved && prev !== sig) {
        if (p.outcomeCorrect === 1) { this.wins++; this.winUsd += p.pnlUsd ?? 0; }
        else if (p.outcomeCorrect === 0) { this.losses++; this.lossUsd += p.pnlUsd ?? 0; }
      }
    }
    if (this.seenPos.size > 6000) {
      for (const id of this.seenPos.keys()) if (!this.posById.has(id)) this.seenPos.delete(id);
    }
    this.primed = true;

    // bind and resolve racers against the new state
    const now = this.time;
    const bound = new Set(this.racers.map((r) => r.posId).filter((x): x is number => x !== null));
    for (const r of this.racers) {
      if (r.state !== 'race' && r.state !== 'break') continue;
      if (r.posId === null && r.sigId !== null) {
        // a runner that broke from the gate becomes a real trade when its
        // position shows up: by signal id, or failing that the same market
        // and side
        const match = d.positions.find((p) => !bound.has(p.id) && !p.resolved
          && (p.signalId === r.sigId || (p.ticker === r.ticker && p.direction === r.dir)));
        if (match) {
          r.posId = match.id;
          bound.add(match.id);
          // now it has a real stake, so it takes its real size
          r.pose.scale = sizeOf(match.costUsd ?? 0);
        }
        else if (now - r.born > 12) { r.state = 'fade'; r.stateT = 0; }
      }
      if (r.posId === null || r.finaleAt !== null) continue;
      const p = this.posById.get(r.posId);
      if (!p) { r.state = 'fade'; r.stateT = 0; continue; }
      if (p.resolved && p.outcomeCorrect !== null) {
        r.won = p.outcomeCorrect === 1;
        r.pnl = p.pnlUsd ?? null;
        // Every racer runs long enough to be read before its finish.
        r.finaleAt = Math.max(now, r.born + MIN_LIFE);
      }
    }

    const pnl = hudNumbers(d.account).pnl;
    if (pnl !== null && this.lastPnl !== null) {
      const bank = d.account?.startBankrollUsd || 1000;
      this.pnlPulse = Math.max(this.pnlPulse, Math.min(1, Math.abs(pnl - this.lastPnl) / (bank * 0.01)));
    }
    this.lastPnl = pnl;
  }

  /** Orders placed outside the bot (by hand, or by an AI agent) and AI
   *  reads, as feed lines. They get no racer: a racer is a bot position
   *  with a price lane and a settlement, and the renderer holds no portfolio
   *  for these (polling one would be a Polymarket call). */
  activity(rec: ActivityRecord, _replay: boolean): void {
    const ev = rec.ev;
    if (ev.kind !== 'manualOrder' && ev.kind !== 'agent' && ev.kind !== 'aiAnalysis') return;
    const b = beatFor(ev);
    if (b && (b.room === 'desk' || b.room === 'research')) this.log(b.log, b.tone);
  }

  payday(ev: PaydayEvent): void {
    this.banner = { text: 'PAYDAY!', sub: paydayLine(ev), t: 0 };
    // fireworks over the stand, every camera in the crowd going off, and a
    // coin storm from the tote board into the winner's circle
    this.track.crowdFlash = 1;
    this.track.flash = Math.max(this.track.flash, 0.6);
    this.waves.fire(TOTE_POS.x, 0.05, TOTE_POS.z + 0.8, '#FFD27A', 9, 1.4);
    this.particles.burst(TOTE_POS.x, 3.4, TOTE_POS.z, '#FFE08A', 120, 7, 2.2, 1.4, 3, 1.2);
    this.stormT = 2.8;
    this.cue({ pos: new THREE.Vector3((TOTE_POS.x + WINS_POS.x) / 2, 1.2, 0), hold: 3.0, dist: 20, pitch: 0.62, yaw: 0 });
    this.log(`🏆 PAYDAY ${paydayLine(ev)}`, 'gold');
  }

  setOutput(canvas: HTMLCanvasElement, w: number, h: number, _mode: 'page' | 'cinema'): void {
    w = Math.max(64, Math.round(w));
    h = Math.max(64, Math.round(h));
    if (canvas !== this.out) {
      const ctx = canvas.getContext('2d', { alpha: false });
      if (!ctx) throw new Error('2D canvas unavailable');
      this.out = canvas;
      this.ctx = cacheMeasureText(ctx);
    }
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    this.W = w;
    this.H = h;
    this.renderer.setSize(w, h, false);
    this.composer.setSize(w, h);
    this.bloom.setSize(Math.round(w / 2), Math.round(h / 2));
    // point sprites are in pixels: scale with the output height so a
    // fullscreen frame doesn't get pinprick sparks
    this.particles.setPixelScale(Math.max(0.6, h / 720));
    this.track.setPixelScale(Math.max(0.75, h / 720));
    this.attachInput();
  }

  setTour(on: boolean): void {
    if (this.tour === on) return;
    this.tour = on;
    if (on) this.setFollow(null);
    this.onTourChange?.(on);
  }

  resetView(): void {
    this.userYaw = 0;
    this.userPitch = 0;
    this.userZoom = 1;
    this.setFollow(null);
  }

  private setFollow(r: Racer | null): void {
    this.follow = r;
    this.onFocusChange?.(r ? r.label.toLowerCase() : null);
  }

  dispose(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.detach?.();
    this.herd.dispose();
    this.tote.dispose();
    const geos = new Set<THREE.BufferGeometry>();
    const mats = new Set<THREE.Material>();
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.geometry) geos.add(m.geometry);
      const mat = m.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(mat)) mat.forEach((x) => mats.add(x));
      else if (mat) mats.add(mat);
    });
    geos.forEach((g) => g.dispose());
    mats.forEach((m) => m.dispose());
    this.particles.dispose();
    this.waves.dispose();
    this.track.dispose();
    this.bloom.dispose();
    this.rt.dispose();
    this.composer.dispose();
    this.renderer.dispose();
    // Chromium caps live WebGL contexts per page: hand this one back now.
    this.renderer.forceContextLoss();
    this.scene.clear();
    this.racers = [];
    this.runners = [];
    this.timers = [];
  }

  private log(text: string, tone: Tone): void {
    this.feed.push({ text, tone, age: 0 });
    if (this.feed.length > 6) this.feed.shift();
  }

  private float(pos: THREE.Vector3, text: string, color: string, big = false, life = big ? 2.6 : 1.9): void {
    this.floats.push({ x: 0, y: 0, wx: pos.x, wy: pos.y + 1.3, wz: pos.z, text, color, age: 0, life, big });
    if (this.floats.length > 8) this.floats.shift();
  }

  /** Run `fn` `delay` seconds of scene time from now (paused with the loop,
   *  dropped on dispose — unlike a window timer). */
  private later(delay: number, fn: () => void): void {
    this.timers.push({ at: this.time + delay, fn });
  }

  private cue(s: Omit<Shot, 't' | 'id'>): void {
    this.shot = { ...s, t: this.time, id: ++this.shotId };
  }

  private newPose(silks: string, cap: string, key: string, scale: number): Pose {
    return {
      x: 0, y: 0, z: 0, yaw: 0, scale, phase: (hash(key) % 628) / 100, stride: 0,
      coat: new THREE.Color(COATS[hash(key) % COATS.length]),
      silks: new THREE.Color(silks), cap: new THREE.Color(cap), glow: 0,
    };
  }

  // ── the card ─────────────────────────────────────────────────────────────

  private spawnRunner(s: HubSignal): void {
    const cat = catOf(s.category);
    const price = priceOf(s.priceCents);
    const off = laneOffset(price ?? 40);
    const subj = subject(s.ticker, s.title);
    const dir = s.direction.toUpperCase();
    const key = `s:${s.source}:${s.id}`;
    const color = silksOf(s.source);
    const r: Runner = {
      sig: s, color, traded: s.traded, cat, off, stall: this.track.stallOf(off),
      t: 0, dur: s.traded ? 2.9 : 3.4,
      label: `↗ ${subj} ${dir}${price !== null ? ` ${price}¢` : ''}`,
      pose: this.newPose(color, CAT_COLOR[cat], key, 1.2),
      pos: new THREE.Vector3(X_CHUTE, 0, R0 + TW + 3.6),
      prev: new THREE.Vector3(X_CHUTE, 0, R0 + TW + 4),
      yaw: Math.PI / 2,
      scratched: false,
    };
    this.runners.push(r);
    this.log(`🐎 ${subj} ${dir} — to the gate`, 'momo');
  }

  private makeRacer(opts: { key: string; posId: number | null; sigId: number | null; ticker: string; title: string; dir: 'yes' | 'no'; cat: Cat; source: string; price: number | null; stake: number }): Racer {
    const off = laneOffset(opts.price ?? 40);
    const pace = paceOf(opts.price, off);
    const r: Racer = {
      key: opts.key, posId: opts.posId, sigId: opts.sigId, ticker: opts.ticker, dir: opts.dir,
      label: subject(opts.ticker, opts.title).slice(0, 14), cat: opts.cat, price: opts.price,
      off, s: gateS(off), pace, speed: 0, toWire: 0, kick: 0,
      pose: this.newPose(silksOf(opts.source), CAT_COLOR[opts.cat], opts.key, sizeOf(opts.stake)),
      pos: new THREE.Vector3(), yaw: 0, dust: 0,
      born: this.time, state: 'break', stateT: 0, finaleAt: null, won: null, pnl: null,
    };
    r.yaw = trackPoint(r.s, off, r.pos);
    this.racers.push(r);
    // the break: that stall's lamp goes green and the doors fly
    const st = this.track.stalls[this.track.stallOf(off)];
    st.open = 1;
    st.load = 0;
    this.waves.fire(X_GATE + 0.6, 0.04, r.pos.z, '#86EFAC', 1.4, 0.6);
    this.particles.burst(X_GATE + 0.5, 0.15, r.pos.z, '#8B6B4A', 12, 2.4, 1.1, 0.6, 5, 0.9);
    this.lastBreak = r;
    return r;
  }

  private spawnFromPosition(pos: BotPosition): void {
    this.makeRacer({
      key: `p:${pos.id}`, posId: pos.id, sigId: pos.signalId, ticker: pos.ticker, title: pos.title, dir: pos.direction,
      cat: catOf(pos.category), source: pos.signalSource,
      price: priceOf(pos.avgFillPriceCents ?? pos.limitPriceCents), stake: pos.costUsd ?? 0,
    });
  }

  private finale(r: Racer): void {
    r.stateT = 0;
    if (r.won) {
      // the stretch drive: kick on to the wire from wherever it is
      r.state = 'sprint';
      const P = lapLength(r.off);
      r.toWire = (((finishS() - r.s) % P) + P) % P;
      if (r.toWire < 0.6) r.toWire += P;
      const dur = THREE.MathUtils.clamp(r.toWire / (r.pace * 2.2), 0.9, 3.0);
      r.kick = r.toWire / dur;
      // the cinematic cuts to the wire to see it arrive
      // from the infield's left, looking up the stretch at the wire: the
      // tote board stays out of the shot, the stand fills the back
      this.cue({ pos: new THREE.Vector3(X_FINISH + 0.4, 0.6, -(R0 + r.off)), hold: dur + 2.6, dist: 8.5, pitch: 0.3, yaw: -0.8 });
    } else {
      r.state = 'pullup';
      const pos = r.pos;
      this.waves.fire(pos.x, 0.04, pos.z, '#F87171', 2.0, 0.7, true);
      // its dust goes to the paddock
      for (let i = 0; i < 10; i++) {
        this.particles.emit({ x: pos.x, y: 0.5, z: pos.z, color: i % 2 ? '#7F1D1D' : '#B45309', size: 1.4, life: 1.3 + i * 0.06, home: LOSS_POS.clone().setY(0.4) });
      }
      this.later(1.3, () => this.track.pulseLoss());
      const txt = r.pnl !== null ? `−$${Math.abs(r.pnl).toFixed(2)}` : 'LOSS';
      this.float(pos, txt, '#FCA5A5');
      this.log(`🐴 pulled up ${txt} · ${r.label}`, 'loss');
      this.cue({ pos: pos.clone().setY(0.5), hold: 2.6, dist: 8, pitch: 0.45, yaw: Math.atan2(pos.x - 1, pos.z) * 0.4 });
    }
  }

  /** It hit the wire: photo flash, confetti, and the real amount. */
  private photoFinish(r: Racer): void {
    const pos = r.pos;
    this.track.flash = 1;
    this.flash2d = 1;
    this.waves.fire(pos.x, 0.04, pos.z, '#FDE68A', 1.6, 0.7);
    const conf = ['#F43F5E', '#FDE047', '#22D3EE', '#A78BFA', '#4ADE80', '#FFFFFF', '#FB923C'];
    for (const c of conf) this.particles.burst(pos.x, 1.6, pos.z, c, 15, 4.2, 1.5, 2.0, 3.2, 1.4);
    // the coin stream: gold motes that visibly fly into the winner's circle
    const n = THREE.MathUtils.clamp(Math.round((r.pnl ?? 10) / 1.2), 12, 30);
    for (let i = 0; i < n; i++) {
      this.particles.emit({
        x: pos.x + (Math.random() - 0.5) * 0.4, y: 0.8, z: pos.z + (Math.random() - 0.5) * 0.4,
        color: i % 3 ? '#FCD34D' : '#FEF3C7', size: 2.4, life: 1.3 + i * 0.035,
        home: WINS_POS.clone().add(this._v2.set((Math.random() - 0.5) * 1.6, 0.3, (Math.random() - 0.5) * 1.6)),
      });
    }
    this.later(1.4, () => this.track.pulseWins());
    const txt = r.pnl !== null ? `+$${r.pnl.toFixed(2)}` : 'WIN';
    this.float(pos, txt, '#6EE7B7', true);
    this.log(`🏁 first past the wire ${txt} · ${r.label}`, 'win');
  }

  private remove(i: number): void {
    const r = this.racers[i];
    if (this.follow === r) this.setFollow(null);
    if (this.lastBreak === r) this.lastBreak = null;
    this.racers.splice(i, 1);
  }

  // ── camera ───────────────────────────────────────────────────────────────

  private ui(): number {
    return Math.min(this.W / 1280, this.H / 720);
  }

  /** Behind a moving horse, three-quarters on from the infield side, so the
   *  stand and the floodlights are the backdrop (from the outside the camera
   *  would sit in the grandstand on the home straight). */
  private chaseYaw(heading: number): number {
    return Math.atan2(-Math.cos(heading), Math.sin(heading)) - 0.62;
  }

  private updateCamera(dt: number): void {
    let dist = 33;
    let pitch = 0.8;
    let yawGoal = Math.sin(this.time * 0.05) * 0.1;
    let fit = true;
    let kk = 1.4;
    const target = this._v.set(2.6, 0, -1.5);
    let cut = false;
    if (this.tour) {
      kk = 2.0;
      const shot = this.shot && this.time - this.shot.t < this.shot.hold ? this.shot : null;
      const runner = this.runners[this.runners.length - 1];
      const fresh = this.lastBreak && this.time - this.lastBreak.born < 4.5 && this.lastBreak.state !== 'fade' ? this.lastBreak : null;
      if (shot) {
        // a hard cut on the finish (or the pull-up), then hold it
        target.copy(shot.pos);
        dist = shot.dist; pitch = shot.pitch; yawGoal = shot.yaw; fit = false;
        if (this.cutId !== shot.id) { this.cutId = shot.id; cut = true; }
      } else if (runner && runner.t < runner.dur * 0.95) {
        // ride the runner in to the gate
        target.copy(runner.pos).setY(0.6);
        dist = 6.5; pitch = 0.4; yawGoal = this.chaseYaw(runner.yaw); fit = false;
      } else if (fresh) {
        target.copy(fresh.pos).setY(0.6);
        dist = 7; pitch = 0.38; yawGoal = this.chaseYaw(fresh.yaw); fit = false;
      } else {
        // between races: a slow crane round the oval
        this.tourYaw += dt * 0.06;
        yawGoal = this.tourYaw; dist = 30; pitch = 0.7;
      }
    }
    if (this.stormT > 0 && !this.follow && !this.tour) { target.set(-2, 0.6, 0); dist = 26; }
    if (this.follow) {
      target.copy(this.follow.pos).setY(0.6);
      dist = 6.8;
      pitch = 0.4;
      yawGoal = this.chaseYaw(this.follow.yaw);
      fit = false;
      kk = 2.4;
    }
    yawGoal += this.userYaw;
    // take the short way round to the new heading
    yawGoal = this.yaw + wrapPi(yawGoal - this.yaw);
    const pitchGoal = THREE.MathUtils.clamp(pitch + this.userPitch, 0.12, 1.4);
    const distGoal = dist * this.userZoom;
    const aspect = this.W / this.H;
    let fovGoal = 40;
    if (fit) {
      // fit the whole meet: the winner's circle to the paddock across, the
      // stand to the apron in depth
      const byW = 2 * Math.atan(0.64 / aspect);
      const byH = 2 * Math.atan(11.2 / 33);
      fovGoal = THREE.MathUtils.clamp(THREE.MathUtils.radToDeg(Math.max(byW, byH)), 26, 56);
    }
    if (cut) {
      this.target.copy(target);
      this.dist = distGoal;
      this.yaw = yawGoal;
      this.pitch = pitchGoal;
      this.fov = fovGoal;
    } else {
      this.target.x = damp(this.target.x, target.x, kk, dt);
      this.target.y = damp(this.target.y, target.y, kk, dt);
      this.target.z = damp(this.target.z, target.z, kk, dt);
      this.dist = damp(this.dist, distGoal, kk, dt);
      this.yaw = damp(this.yaw, yawGoal, kk, dt);
      this.pitch = damp(this.pitch, pitchGoal, kk, dt);
      this.fov = damp(this.fov, fovGoal, kk, dt);
    }
    const cp = Math.cos(this.pitch);
    this.camera.position.set(
      this.target.x + Math.sin(this.yaw) * cp * this.dist,
      this.target.y + Math.sin(this.pitch) * this.dist,
      this.target.z + Math.cos(this.yaw) * cp * this.dist,
    );
    this.camera.lookAt(this.target);
    this.camera.fov = this.fov;
    this.camera.aspect = aspect;
    this.camera.updateProjectionMatrix();
  }

  // ── input ────────────────────────────────────────────────────────────────

  private attachInput(): void {
    this.detach?.();
    const el = this.out;
    const down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      this.drag = { x: e.clientX, y: e.clientY, moved: false, yaw: this.userYaw, pitch: this.userPitch };
      el.setPointerCapture(e.pointerId);
    };
    const move = (e: PointerEvent) => {
      const d = this.drag;
      if (!d) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (!d.moved && Math.hypot(dx, dy) < 5) return;
      d.moved = true;
      this.userYaw = d.yaw - dx * 0.004;
      this.userPitch = THREE.MathUtils.clamp(d.pitch + dy * 0.003, -0.6, 0.45);
    };
    const up = (e: PointerEvent) => {
      const d = this.drag;
      this.drag = null;
      try { el.releasePointerCapture(e.pointerId); } catch { /* already released */ }
      if (!d || d.moved) return;
      const r = this.pick(e.clientX, e.clientY);
      if (this.tour && r) this.setTour(false);
      this.setFollow(r && r !== this.follow ? r : null);
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      this.userZoom = THREE.MathUtils.clamp(this.userZoom * Math.exp(e.deltaY * 0.0012), 0.4, 1.5);
    };
    el.addEventListener('pointerdown', down);
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
    el.addEventListener('wheel', wheel, { passive: false });
    this.detach = () => {
      el.removeEventListener('pointerdown', down);
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      el.removeEventListener('wheel', wheel);
    };
  }

  private pickable(r: Racer): boolean {
    return r.state === 'race' || r.state === 'break' || r.state === 'sprint';
  }

  /** Nearest racer to the cursor on screen: forgiving on small horses. */
  private pick(clientX: number, clientY: number): Racer | null {
    const rect = this.out.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * this.W;
    const py = ((clientY - rect.top) / rect.height) * this.H;
    let best: Racer | null = null;
    let bd = (44 * this.ui()) ** 2;
    for (const r of this.racers) {
      if (!this.pickable(r)) continue;
      const s = this.project(this._v3.copy(r.pos).setY(0.6));
      const d = (s.x - px) ** 2 + (s.y - py) ** 2;
      if (d < bd) { bd = d; best = r; }
    }
    return best;
  }

  // ── frame ────────────────────────────────────────────────────────────────

  private frame(): void {
    const now = performance.now();
    const dt = Math.min(0.05, Math.max(0, (now - this.last) / 1000));
    this.last = now;
    this.fpsFrames++;
    this.fpsAcc += dt;
    if (this.fpsAcc >= 0.5) {
      this.fps = Math.round(this.fpsFrames / this.fpsAcc);
      this.fpsFrames = 0;
      this.fpsAcc = 0;
    }
    try {
      this.update(dt);
      this.render();
    } catch (err) {
      if (!this.frameErr) {
        this.frameErr = true;
        console.error('[racetrack] frame failed', err);
      }
    }
  }

  private updateRunner(r: Runner, dt: number): boolean {
    r.t += dt;
    const u = Math.min(1, r.t / r.dur);
    const zl = R0 + r.off;
    const [A, B, C, D] = this._pts;
    A.set(X_CHUTE, 0, R0 + TW + 3.6);
    B.set(X_CHUTE, 0, R0 + TW + 0.3);
    r.prev.copy(r.pos);
    let stride = 0.5;
    let scale = 1.2;
    const st = this.track.stalls[r.stall];
    if (r.traded) {
      // walk up through the chute, round behind its stall, load, break
      C.set(X_GATE - 3.4, 0, zl);
      D.set(X_GATE - 1.3, 0, zl);
      if (u < 0.7) bez(A, B, C, D, ease(u / 0.7), r.pos);
      else if (u < 0.84) r.pos.set(X_GATE - 1.3 + 1.3 * ease((u - 0.7) / 0.14), 0, zl);
      else { r.pos.set(X_GATE, 0, zl); stride = 0; }
      if (u >= 0.7) st.load = Math.max(st.load, (u - 0.7) / 0.3);
    } else {
      // scratched: up to the gate, a beat, and led away again
      C.set(X_GATE - 4.2, 0, zl);
      D.set(X_GATE - 2.6, 0, zl);
      if (u < 0.55) bez(A, B, C, D, ease(u / 0.55), r.pos);
      else if (u < 0.72) { r.pos.copy(D); stride = 0; }
      else {
        const k2 = ease((u - 0.72) / 0.28);
        r.pos.lerpVectors(D, B, k2);
        scale *= 1 - Math.max(0, (u - 0.85) / 0.15);
      }
      if (u >= 0.56 && !r.scratched) {
        r.scratched = true;
        // short: the next runner is usually already on its way to the gate
        this.float(r.pos, 'SCRATCHED', '#FCA5A5', false, 1.2);
      }
    }
    const dx = r.pos.x - r.prev.x, dz = r.pos.z - r.prev.z;
    if (dx * dx + dz * dz > 1e-6) {
      const goal = Math.atan2(-dz, dx);
      r.yaw = r.yaw + wrapPi(goal - r.yaw) * Math.min(1, dt * 8);
    }
    const p = r.pose;
    p.x = r.pos.x; p.y = 0; p.z = r.pos.z; p.yaw = r.yaw;
    p.stride = damp(p.stride, stride, 6, dt);
    p.phase += dt * (5 + p.stride * 6);
    p.scale = scale;
    p.glow = 0.6 + Math.sin(this.time * 6) * 0.2;
    this.herd.add(p);
    if (u < 1) return true;

    if (r.traded) {
      // loaded: the gate flies open and it becomes a racer on its lane
      const s = r.sig;
      const racer = this.makeRacer({
        key: `s:${s.source}:${s.id}`, posId: null, sigId: s.id, ticker: s.ticker, title: s.title, dir: s.direction,
        cat: r.cat, source: s.source, price: priceOf(s.priceCents),
        stake: 20,
      });
      racer.pose.phase = p.phase;
      this.float(racer.pos, 'AND THEY’RE OFF · BUY', r.color);
    } else {
      this.log(`↺ scratched: skipped ${subject(r.sig.ticker, r.sig.title)}`, 'neutral');
    }
    return false;
  }

  private update(dt: number): void {
    this.time += dt;
    const t = this.time;

    for (let i = this.timers.length - 1; i >= 0; i--) {
      if (t >= this.timers[i].at) { const f = this.timers[i].fn; this.timers.splice(i, 1); f(); }
    }

    // ── schedule: runners to the gate, racers on, finishes out ───────────
    this.runnerCd -= dt;
    if (this.runnerCd <= 0 && this.sigQ.length && this.runners.length < MAX_RUNNERS) {
      this.runnerCd = 0.9 + Math.random() * 0.6;
      this.spawnRunner(this.sigQ.shift()!);
    }
    this.spawnCd -= dt;
    let alive = 0;
    for (const r of this.racers) if (r.state === 'race' || r.state === 'break') alive++;
    if (this.spawnCd <= 0 && this.openQ.length && alive < MAX_RACERS) {
      this.spawnCd = 0.55 + Math.random() * 0.35;
      // newest first: the open pool churns, and an old entry may be settled
      const p = this.openQ.pop()!;
      const cur = this.posById.get(p.id);
      if (cur && !cur.resolved) this.spawnFromPosition(cur);
    }
    this.finaleCd -= dt;

    this.herd.begin();
    for (const st of this.track.stalls) st.load = Math.max(0, st.load - dt * 2);
    for (let i = this.runners.length - 1; i >= 0; i--) {
      if (!this.updateRunner(this.runners[i], dt)) this.runners.splice(i, 1);
    }

    // ── racers ───────────────────────────────────────────────────────────
    for (let i = this.racers.length - 1; i >= 0; i--) {
      const r = this.racers[i];
      r.stateT += dt;
      let scale = r.pose.scale;
      let drawScale = scale;
      switch (r.state) {
        case 'break': {
          r.speed = r.pace * ease(Math.min(1, r.stateT / 1.4));
          if (r.stateT >= 1.4) { r.state = 'race'; r.stateT = 0; }
          break;
        }
        case 'race':
          r.speed = r.pace;
          if (r.finaleAt !== null && t >= r.finaleAt) {
            if (this.finaleCd <= 0) { this.finaleCd = 0.32; this.finale(r); }
            else if (t - r.finaleAt > 3) { r.state = 'fade'; r.stateT = 0; }
          }
          break;
        case 'sprint': {
          r.speed = damp(r.speed, r.kick, 6, dt);
          r.toWire -= r.speed * dt;
          if (r.toWire <= 0) {
            r.s += r.toWire; // land exactly on the wire this frame
            r.yaw = trackPoint(r.s, r.off, r.pos);
            this.photoFinish(r);
            r.state = 'won';
            r.stateT = 0;
          }
          break;
        }
        case 'won': {
          // eases up past the wire, then leaves the frame to the confetti
          r.speed = Math.max(0, r.speed * Math.exp(-dt * 1.4));
          if (r.stateT > 1.8) drawScale = scale * Math.max(0, 1 - (r.stateT - 1.8) / 0.6);
          if (r.stateT > 2.4) { this.remove(i); continue; }
          break;
        }
        case 'pullup': {
          r.speed = r.pace * Math.max(0, 1 - r.stateT / 1.1);
          if (r.stateT > 1.9) drawScale = scale * Math.max(0, 1 - (r.stateT - 1.9) / 0.7);
          if (r.stateT > 2.6) { this.remove(i); continue; }
          break;
        }
        case 'fade': {
          r.speed = r.pace * 0.6;
          drawScale = scale * Math.max(0, 1 - r.stateT / 0.8);
          if (r.stateT >= 0.8) { this.remove(i); continue; }
          break;
        }
      }
      if (r.state !== 'sprint' || r.toWire > 0) {
        r.s += r.speed * dt;
        r.yaw = trackPoint(r.s, r.off, r.pos);
      }
      const p = r.pose;
      p.x = r.pos.x; p.y = 0; p.z = r.pos.z; p.yaw = r.yaw;
      p.stride = Math.min(1, r.speed / 2.2);
      p.phase += dt * r.speed * 3.3;
      p.glow = r === this.follow ? 1 : r.state === 'sprint' ? 0.9 : 0.55;
      const keep = p.scale;
      p.scale = drawScale;
      this.herd.add(p);
      p.scale = keep;
      scale = keep;
      // kicked-up dirt, more of it at a gallop
      r.dust += dt * r.speed * (r.state === 'sprint' ? 3 : 0.9);
      while (r.dust > 1) {
        r.dust -= 1;
        const c = Math.cos(r.yaw), s = -Math.sin(r.yaw);
        this.particles.emit({
          x: r.pos.x - c * 0.45 * scale, y: 0.08, z: r.pos.z - s * 0.45 * scale,
          vx: -c * 0.8 + (Math.random() - 0.5) * 0.6, vy: 1.2 + Math.random(), vz: -s * 0.8 + (Math.random() - 0.5) * 0.6,
          color: r.state === 'sprint' ? '#C9A27A' : '#7A5A3E', size: 0.9, life: 0.45, drag: 1, gravity: 6,
        });
      }
    }
    this.herd.end();

    // PAYDAY: fireworks over the stand, coins from the tote to the circle
    if (this.stormT > 0) {
      this.stormT -= dt;
      if (Math.random() < dt * 3.2) {
        const fx = -11 + Math.random() * 18, fy = 6.5 + Math.random() * 2, fz = -10.5 - Math.random() * 2.5;
        const col = ['#FDE047', '#F472B6', '#60A5FA', '#4ADE80', '#FFFFFF'][Math.floor(Math.random() * 5)];
        this.particles.burst(fx, fy, fz, col, 46, 6, 2.2, 1.4, 1.6, 1);
      }
      for (let i = 0; i < 3; i++) {
        this.particles.emit({
          x: TOTE_POS.x + (Math.random() - 0.5) * 6, y: 3.6, z: TOTE_POS.z, color: Math.random() < 0.7 ? '#FCD34D' : '#FFFFFF', size: 1.7,
          life: 1.2 + Math.random() * 0.5, home: WINS_POS.clone().add(this._v2.set((Math.random() - 0.5) * 1.6, 0.3, (Math.random() - 0.5) * 1.6)),
        });
      }
      if (Math.random() < dt * 4) this.track.pulseWins();
    }

    this.track.update(dt, t);
    this.particles.update(dt);
    this.waves.update(dt);
    for (const f of this.feed) f.age += dt;
    for (const f of this.floats) f.age += dt;
    this.floats = this.floats.filter((f) => f.age < f.life);
    this.pnlPulse = Math.max(0, this.pnlPulse - dt * 2.5);
    this.flash2d = Math.max(0, this.flash2d - dt * 4);
    if (this.banner) {
      this.banner.t += dt;
      if (this.banner.t > 3.4) this.banner = null;
    }
    if (t - this.toteT >= 0.5) {
      this.toteT = t;
      this.updateTote();
    }
    this.updateCamera(dt);
  }

  private updateTote(): void {
    const a = this.latest?.account ?? null;
    const rows: ToteRow[] = [];
    let onTrack = 0;
    for (const r of this.racers) {
      if (r.state !== 'race' && r.state !== 'break' && r.state !== 'sprint') continue;
      onTrack++;
      rows.push({ name: `${r.label} ${r.dir.toUpperCase()}`, price: r.price, color: CAT_COLOR[r.cat] });
    }
    // favourites first, as a tote board lists them
    rows.sort((x, y) => (y.price ?? 0) - (x.price ?? 0));
    rows.length = Math.min(rows.length, 6);
    this.tote.update({
      pnl: hudNumbers(a).pnl,
      bank: balanceOf(a),
      wins: this.wins, winUsd: this.winUsd, losses: this.losses, lossUsd: this.lossUsd,
      onTrack, rows,
    }, this.time);
  }

  private project(p: THREE.Vector3): { x: number; y: number; ok: boolean } {
    this._v2.copy(p).project(this.camera);
    return { x: (this._v2.x * 0.5 + 0.5) * this.W, y: (-this._v2.y * 0.5 + 0.5) * this.H, ok: this._v2.z < 1 };
  }

  private render(): void {
    this.composer.render();
    const g = this.ctx;
    g.drawImage(this.glCanvas, 0, 0, this.W, this.H);
    const u = this.ui();
    const P = (x: number, y: number, z: number) => this.project(this._v.set(x, y, z));

    // Every label claims its screen box, and what's placed later steers
    // round it: fixed furniture first (lane ticks, landmarks, the two
    // plates), then the floats, then the tags, which are the ones that move.
    const placed: Box[] = [];

    // lane prices down the backstretch's left end, nearest the viewer
    const lanes = [];
    for (const p of LANES) {
      const s = P(-LH + 0.9, 0, R0 + laneOffset(p));
      if (s.ok) lanes.push({ x: s.x, y: s.y, text: `${p}¢` });
    }
    drawLaneLabels(g, lanes, null, u, placed);

    // landmarks
    const fm = P(X_FINISH, 3.2, -(R0 - 0.3));
    if (fm.ok) drawMarker(g, fm.x, fm.y, 'FINISH', '#FDE68A', u, this.track.flash, placed);
    let loading = 0;
    for (const st of this.track.stalls) loading = Math.max(loading, st.load, st.open);
    const gm = P(X_GATE, 3.05, R0 + TW + 0.65);
    if (gm.ok) drawMarker(g, gm.x, gm.y, 'STARTING GATE', '#86EFAC', u, loading, placed);

    // the winner's circle and the paddock
    const ws = P(WINS_POS.x, 0, WINS_POS.z + 2.5);
    if (ws.ok) drawWell(g, ws.x, ws.y, "WINNER'S CIRCLE", this.wins, this.winUsd, '#FCD34D', u, 0, placed);
    const ls = P(LOSS_POS.x, 0, LOSS_POS.z + 3.5);
    if (ls.ok) drawWell(g, ls.x, ls.y, 'LOSSES', this.losses, this.lossUsd, '#F87171', u, 0, placed);

    // the floats claim their spots before the tags (drawn last, on top)
    for (const f of this.floats) {
      const s = P(f.wx, f.wy, f.wz);
      f.x = s.x; f.y = s.y;
    }
    layoutFloats(g, this.floats, u, placed);

    // racer + runner tags: biggest first so the culling keeps the important ones
    const tags: Tag[] = [];
    for (const r of this.runners) {
      const s = this.project(this._v3.copy(r.pos).setY(1.1));
      // a scratched runner's tag gives way to its SCRATCHED call at once
      const k2 = r.t / r.dur;
      const fade = r.traded ? 1 : k2 < 0.55 ? 1 : Math.max(0, 1 - (k2 - 0.55) / 0.08);
      if (s.ok) tags.push({ x: s.x, y: s.y, text: r.label, color: '#F9A8D4', alpha: Math.min(1, r.t * 3) * fade, size: 1.05, dot: true });
    }
    const ordered = [...this.racers].sort((a, b) => (b === this.follow ? 1 : 0) - (a === this.follow ? 1 : 0) || b.pose.scale - a.pose.scale);
    for (const r of ordered) {
      if (!this.pickable(r)) continue;
      const s = this.project(this._v3.copy(r.pos).setY(1.15 * r.pose.scale));
      if (!s.ok) continue;
      tags.push({ x: s.x, y: s.y, text: `${r.label} ${r.dir.toUpperCase()}`, color: CAT_COLOR[r.cat], alpha: r.state === 'break' ? Math.min(1, r.stateT * 2) : 0.92, size: r === this.follow ? 1.3 : 0.95, dot: true });
    }
    drawTags(g, tags, u, 16, placed);
    drawFloats(g, this.floats, u);
    drawFlash(g, this.W, this.H, this.flash2d);
    drawLegend(g, this.W, this.H, u);

    const n = hudNumbers(this.latest?.account ?? null);
    drawHud(g, {
      W: this.W, H: this.H, u, portrait: false,
      pnl: n.pnl,
      roi: n.roi,
      winRate: n.winRate,
      settledToday: n.settledToday,
      open: n.open,
      wins: this.wins, losses: this.losses,
      pnlPulse: this.pnlPulse,
      feed: this.feed,
      banner: this.banner,
      poly: null,
      showBrand: true,
      brandSub: 'RACE TRACK · krypt.cc',
      brandColors: ['#FDE68A', '#FBBF24', '#F59E0B'],
    });
  }
}
