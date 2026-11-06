import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { hudNumbers, type HubData } from '../data';
import type { HubSceneEngine } from '../HubStage';
import { paydayLine, type PaydayEvent } from '../payday';
import { activityBus, type ActivityRecord, type AgentCallActivity } from '../../state/activity';
import { agentCallBeat } from '../engine/jobs';
import { cacheMeasureText } from '../text';
import { F_SANS, TONE, drawHud, drawTags, type FeedLine, type TagView, type Tone } from '../engine/overlay';
import { Smoke } from '../engine/smoke';
import { tagLabel } from '../../utils/agents';
import { DeskBook, type Desk } from './model';
import { CleanKit } from './rat';
import { DeskStation, FurnitureKit, Monitor } from './desk';
import { Room, ROOM, WALL } from './room';
import { Walker, visibleFrom } from '../walk';
import { deskInReach, penthouseWalkSpace } from './walkspace';
import {
  paintAccount, paintAgents, paintClock, paintPositions, paintRails, paintSettled, paintSignals, paintStream,
  type Painter, type WallSource,
} from './screens';

// The Penthouse: the user's own agents (the ones they set up on the AI Agents
// page, nobody else) each at a battlestation of five monitors, under a video
// wall of eight, with the city at night through the glass. Every screen shows
// what is really happening (model.ts, screens.ts); the rats act out each real
// tool call as it lands — typing, reading, a flinch on a refusal, a fist on
// an order — and between calls they smoke, sip and look round their screens.
// Read-only like every hub scene: it listens to the activity bus and reads the
// app state the stage hands it, and nothing here can place an order.

/** How far back a desk's history is filled from the bus when the scene
 *  opens (the bus keeps the last 120 agent calls, renderer memory only). */
const HISTORY_MS = 6 * 60 * 60_000;
/** A call newer than this is acted out; older ones just fill the screens. */
const LIVE_MS = 20_000;
const DESK_SPACING = 3.4;
const MAX_DESK_LIGHTS = 6;
const MOVE_CODES = new Set(['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);
const ease = (x: number) => x * x * (3 - 2 * x);

interface Bubble { desk: string; text: string; tone: Tone; age: number; life: number }

function damp(a: number, b: number, k: number, dt: number): number {
  return b + (a - b) * Math.exp(-k * dt);
}

function wrapPi(a: number): number {
  return Math.atan2(Math.sin(a), Math.cos(a));
}

type Shot = { kind: 'overview' } | { kind: 'desk'; id: string } | { kind: 'wall' } | { kind: 'skyline' };

export class PenthouseEngine implements HubSceneEngine {
  private out: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private W = 1280;
  private H = 720;
  private glCanvas = document.createElement('canvas');
  private renderer: THREE.WebGLRenderer;
  private composer: EffectComposer;
  private bloom: UnrealBloomPass;
  private rt: THREE.WebGLRenderTarget;
  private envRT: THREE.WebGLRenderTarget;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(42, 16 / 9, 0.05, 1500);
  private renderPass: RenderPass;
  // on foot (walk.ts): a camera only, like the Casino's
  private walker = new Walker(penthouseWalkSpace([]));
  private walking = false;
  private walkCam = new THREE.PerspectiveCamera(68, 16 / 9, 0.05, 1500);
  /** Leaning in at a desk (E): its id, and how far in (0 standing … 1). */
  private leanIn: string | null = null;
  private leanT = 0;
  private leanFrom: string | null = null;
  private _lean = new THREE.PerspectiveCamera();

  private room = new Room();
  private furniture = new FurnitureKit();
  private rats = new CleanKit();
  private smoke = new Smoke();
  private emberMats = [0, 1, 2].map(() => new THREE.MeshStandardMaterial({ color: '#ffb36b', emissive: '#ff6a1a', emissiveIntensity: 2.4 }));
  private book = new DeskBook();
  private stations = new Map<string, DeskStation>();
  private stationColor = new Map<string, string>();
  private roster = -1;
  private wall: Monitor[] = [];
  private deskLights: THREE.PointLight[] = [];
  private latest: HubData | null = null;
  private scoreLoaded = false;
  private seeded = false;
  private pending: ActivityRecord[] = [];
  private approvals: { at: number; text: string; mode: string; decision: 'approved' | 'rejected' | null }[] = [];

  // presentation
  private feed: FeedLine[] = [];
  private bubbles: Bubble[] = [];
  private banner: { text: string; sub: string; t: number } | null = null;
  private pnlPulse = 0;
  private lastPnl: number | null = null;

  // camera
  private yaw = 0.3;
  private pitch = 0.42;
  private dist = 16;
  private fov = 42;
  private target = new THREE.Vector3(0, 1, -3);
  private userYaw = 0;
  private userPitch = 0;
  private userZoom = 1;
  private follow: string | null = null;
  private tour = false;
  private tourShot: Shot = { kind: 'overview' };
  private tourT = 0;
  private tourIdx = 0;
  private hot: { id: string; at: number } | null = null;
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
  onWalkChange: ((on: boolean) => void) | null = null;

  private _v = new THREE.Vector3();
  private _v2 = new THREE.Vector3();

  constructor(canvas: HTMLCanvasElement, w: number, h: number) {
    this.out = canvas;
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) throw new Error('2D canvas unavailable');
    this.ctx = cacheMeasureText(ctx);

    this.renderer = new THREE.WebGLRenderer({ canvas: this.glCanvas, antialias: false, alpha: false, stencil: false, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.0;

    // soft studio reflections on the floor, the desks and the cans
    const pm = new THREE.PMREMGenerator(this.renderer);
    const env = new RoomEnvironment();
    this.envRT = pm.fromScene(env, 0.04);
    env.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      (m.material as THREE.Material | undefined)?.dispose?.();
    });
    pm.dispose();
    this.scene.environment = this.envRT.texture;
    this.scene.environmentIntensity = 0.1;
    this.scene.background = new THREE.Color('#05060d');
    // a purple haze the city sinks into (indoors, under 30 units, it barely registers)
    this.scene.fog = new THREE.FogExp2('#1d0f34', 0.0042);

    // night in a lit room: a cool fill off the city, moonlight through the
    // glass, warm downlights over the desks and the lounge
    this.scene.add(new THREE.HemisphereLight('#7565bd', '#16111c', 0.85));
    // a violet wash off the cove along the glass
    for (const [x, z] of [[-4, ROOM.z0 + 0.6], [ROOM.x0 + 0.6, 0]] as const) {
      const v = new THREE.PointLight('#8b5cf6', 5, 13, 2);
      v.position.set(x, ROOM.h - 0.4, z);
      this.scene.add(v);
    }
    const moon = new THREE.DirectionalLight('#a9b8ff', 0.55);
    moon.position.set(-30, 40, -40);
    this.scene.add(moon);
    for (const [x, z, i] of [[-6, -3.4, 12], [0, -3.4, 12], [6, -3.4, 12], [-9.4, 3.8, 8], [6.8, 4.8, 7]] as const) {
      const l = new THREE.PointLight('#ffd2a1', i, 11, 2);
      l.position.set(x, ROOM.h - 0.3, z);
      this.scene.add(l);
    }
    this.scene.add(this.room.group, this.smoke.points);
    this.buildWall();

    this.rt = new THREE.WebGLRenderTarget(w, h, { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(this.renderer, this.rt);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.composer.addPass(this.renderPass);
    // the city's windows, the LED strips, the ceiling slots, embers and mic
    // lights bloom; the monitors sit just under the threshold and stay sharp
    this.bloom = new UnrealBloomPass(new THREE.Vector2(w, h), 0.3, 0.45, 0.96);
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

  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  ingest(d: HubData): void {
    this.latest = d;
    if (d.scoreboard !== undefined) this.scoreLoaded = true;
    this.book.sync(d.config ?? null);
    this.book.scoreboard(d.scoreboard);
    if (d.config && !this.seeded) {
      // the desks exist now: fill their screens from what the bus still holds
      this.seeded = true;
      for (const r of activityBus.recent(HISTORY_MS)) this.book.call(r);
      for (const r of this.pending.splice(0)) this.take(r, false);
    }
    this.syncStations();
    for (const w of this.wall) w.invalidate();
    const pnl = hudNumbers(d.account).pnl;
    if (pnl !== null && this.lastPnl !== null) {
      const bank = d.account?.startBankrollUsd || 1000;
      this.pnlPulse = Math.max(this.pnlPulse, Math.min(1, Math.abs(pnl - this.lastPnl) / (bank * 0.01)));
    }
    this.lastPnl = pnl;
  }

  activity(rec: ActivityRecord, replay: boolean): void {
    const ev = rec.ev;
    if (ev.kind === 'agent') {
      if (ev.approvalId === null && !ev.decision) return;
      this.approvals.unshift({ at: rec.at, text: ev.message, mode: ev.mode, decision: ev.decision });
      if (this.approvals.length > 12) this.approvals.length = 12;
      this.wall.forEach((w) => w.invalidate());
      return;
    }
    if (ev.kind !== 'agentCall') return;
    if (!this.seeded) { this.pending.push(rec); return; }
    this.take(rec, !replay || Date.now() - rec.at < LIVE_MS);
  }

  payday(ev: PaydayEvent): void {
    this.banner = { text: 'PAYDAY!', sub: paydayLine(ev), t: 0 };
    this.log(`🏆 PAYDAY ${paydayLine(ev)}`, 'gold');
    for (const s of this.stations.values()) { s.rat.act('cheer', 2.4); s.pulse(); }
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
    this.smoke.setViewHeight(h);
    this.attachInput();
  }

  setTour(on: boolean): void {
    if (on && this.walking) this.setWalk(false);
    if (this.tour === on) return;
    this.tour = on;
    if (on) { this.setFollow(null); this.tourT = 0; this.tourShot = { kind: 'overview' }; }
    this.onTourChange?.(on);
  }

  setWalk(on: boolean): void {
    if (on === this.walking) return;
    this.walking = on;
    this.leanIn = null;
    this.leanT = 0;
    this.walker.frozen = false;
    if (on) {
      if (this.tour) this.setTour(false);
      this.setFollow(null);
      this.walker.reset();
      this.walker.onKey = (code, repeat) => this.walkKey(code, repeat);
    }
    this.renderPass.camera = on ? this.walkCam : this.camera;
    this.attachInput();
    this.onWalkChange?.(on);
  }

  /** Esc: standing up from a desk first; the walk ends on the next one. */
  escape(): boolean {
    if (!this.walking || !this.leanIn) return false;
    this.standUp();
    return true;
  }

  /** E at a desk: lean in over the agent's shoulder (E again, or a step,
   *  stands you back up). */
  private walkKey(code: string, repeat: boolean): boolean {
    if (this.leanIn) {
      if (code === 'KeyE') { if (!repeat) this.standUp(); return true; }
      if (MOVE_CODES.has(code)) {
        if (repeat) return true;
        this.standUp();
        return false;
      }
      return false;
    }
    if (code !== 'KeyE' || repeat) return false;
    const id = this.deskNear();
    if (!id) return false;
    this.leanIn = id;
    this.leanFrom = id;
    this.walker.frozen = true;
    return true;
  }

  private standUp(): void {
    this.leanIn = null;
    this.walker.frozen = false;
    this.walker.relock();
  }

  private deskNear(): string | null {
    const desks = [...this.stations].map(([id, s]) => ({ id, x: s.group.position.x, z: s.group.position.z }));
    return deskInReach(desks, this.walker.x, this.walker.z)?.id ?? null;
  }

  /** The walker's eye, eased toward the leaned-in view of a desk. */
  private poseWalkCam(dt: number): void {
    this.walker.apply(this.walkCam);
    this.walkCam.fov = this.walker.fov;
    this.leanT = Math.max(0, Math.min(1, this.leanT + (this.leanIn ? dt : -dt) / 0.55));
    const st = this.leanFrom ? this.stations.get(this.leanFrom) : undefined;
    if (this.leanT > 0 && st) {
      const f = st.focus;
      const L = this._lean;
      L.position.set(f.x + Math.sin(0.35) * Math.cos(0.42) * 2.3, f.y + Math.sin(0.42) * 2.3, f.z + Math.cos(0.35) * Math.cos(0.42) * 2.3);
      L.lookAt(f);
      const k = ease(this.leanT);
      this.walkCam.position.lerp(L.position, k);
      this.walkCam.quaternion.slerp(L.quaternion, k);
      this.walkCam.fov += (46 - this.walkCam.fov) * k;
    }
    this.walkCam.aspect = this.W / this.H;
    this.walkCam.updateProjectionMatrix();
    this.walkCam.updateMatrixWorld();
  }

  private activeCam(): THREE.PerspectiveCamera {
    return this.walking ? this.walkCam : this.camera;
  }

  resetView(): void {
    this.userYaw = 0;
    this.userPitch = 0;
    this.userZoom = 1;
    this.setFollow(null);
  }

  dispose(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.detach?.();
    for (const s of this.stations.values()) s.dispose();
    this.wall.forEach((m) => m.dispose());
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
    this.room.dispose();
    this.furniture.dispose();
    this.rats.dispose();
    this.smoke.dispose();
    this.emberMats.forEach((m) => m.dispose());
    this.envRT.dispose();
    this.bloom.dispose();
    this.rt.dispose();
    this.composer.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.scene.clear();
    this.stations.clear();
  }

  // ── the books → the room ─────────────────────────────────────────────────

  /** One agent call on the books; acted out at its desk when it is live. */
  private take(rec: ActivityRecord, live: boolean): void {
    const desk = this.book.call(rec);
    this.syncStations();
    if (!desk || !live) return;
    const st = this.stations.get(desk.id);
    if (!st) return;
    const ev = rec.ev as AgentCallActivity;
    st.pulse();
    const mood = ev.call === 'connect' ? 'cheer'
      : ev.outcome === 'refused' ? 'refused'
        : ev.outcome === 'error' ? 'read'
          : (ev.tool === 'place_order' || ev.tool === 'close_position') ? 'cheer'
            : (ev.tool === 'record_forecast' || desk.calls[0]?.group === 'build' || desk.calls[0]?.group === 'order') ? 'type'
              : 'read';
    st.rat.look = desk.calls[0]?.group === 'research' ? -1 : desk.calls[0]?.group === 'forecast' ? 1 : 0;
    st.rat.act(mood, mood === 'refused' ? 2.2 : mood === 'cheer' ? 2.6 : 3.2);
    const beat = agentCallBeat(ev, tagLabel(desk.who));
    if (beat) {
      this.bubbles = this.bubbles.filter((b) => b.desk !== desk.id);
      this.bubbles.push({ desk: desk.id, text: beat.text, tone: beat.tone, age: 0, life: 3.6 });
      this.log(beat.log, beat.tone);
    }
    this.hot = { id: desk.id, at: this.time };
    this.wall.forEach((w) => w.invalidate());
  }

  private syncStations(): void {
    const desks = this.book.list();
    let rebuild = this.book.roster !== this.roster;
    for (const d of desks) if (this.stationColor.get(d.id) !== d.who.color) rebuild = true;
    if (!rebuild) return;
    this.roster = this.book.roster;
    const keep = new Set(desks.map((d) => d.id));
    for (const [id, s] of this.stations) {
      if (keep.has(id) && this.stationColor.get(id) === s.desk.who.color) continue;
      this.removeStation(id, s);
    }
    desks.forEach((d, i) => {
      if (!this.stations.has(d.id)) {
        const s = new DeskStation(this.furniture, this.rats, d, i + 1, this.emberMats[i % 3], () => this.scoreLoaded);
        this.stations.set(d.id, s);
        this.stationColor.set(d.id, d.who.color);
        this.scene.add(s.group);
      }
    });
    this.layout(desks);
    if (this.follow && !this.stations.has(this.follow)) this.setFollow(null);
  }

  private removeStation(id: string, s: DeskStation): void {
    this.scene.remove(s.group);
    const geos = new Set<THREE.BufferGeometry>();
    s.group.traverse((o) => {
      const m = o as THREE.Mesh;
      // shared kit geometry stays; a rat's baked parts are its own
      if (m.isMesh && m.geometry && !Object.values(this.furniture.geo).includes(m.geometry as never)
        && !Object.values(this.rats.g).includes(m.geometry as never) && !Object.values(this.rats.x).includes(m.geometry as never)) geos.add(m.geometry);
    });
    geos.forEach((g) => g.dispose());
    s.dispose();
    this.stations.delete(id);
    this.stationColor.delete(id);
  }

  /** One row of up to six along the glass; two rows past that. */
  private layout(desks: Desk[]): void {
    const n = desks.length;
    const rows = n <= 6 ? [desks] : [desks.slice(0, Math.ceil(n / 2)), desks.slice(Math.ceil(n / 2))];
    const zs = rows.length === 1 ? [-3.9] : [-5.2, -1.4];
    rows.forEach((row, r) => {
      row.forEach((d, i) => {
        const s = this.stations.get(d.id);
        if (!s) return;
        s.group.position.set((i - (row.length - 1) / 2) * DESK_SPACING - 0.9, 0, zs[r]);
      });
    });
    this.walker.space = penthouseWalkSpace([...this.stations.values()].map((s) => ({ x: s.group.position.x, z: s.group.position.z })));
    // a screen glow at each of the first few desks
    this.deskLights.forEach((l) => this.scene.remove(l));
    this.deskLights.forEach((l) => l.dispose());
    this.deskLights = [];
    desks.slice(0, MAX_DESK_LIGHTS).forEach((d) => {
      const s = this.stations.get(d.id);
      if (!s) return;
      const c = new THREE.Color('#8fb4ff').lerp(new THREE.Color(d.who.color), 0.3);
      const l = new THREE.PointLight(c, 1.3, 3.2, 2);
      l.position.set(s.group.position.x, 1.2, s.group.position.z - 0.05);
      this.scene.add(l);
      this.deskLights.push(l);
    });
  }

  private buildWall(): void {
    const src: WallSource = {
      data: () => this.latest,
      book: this.book,
      approvals: () => this.approvals,
    };
    const painters: Painter[] = [
      paintAccount(src), paintAgents(src), paintStream(src), paintClock(src),
      paintPositions(src), paintSignals(src), paintSettled(src), paintRails(src),
    ];
    painters.forEach((p, i) => {
      const col = i % WALL.cols;
      const row = Math.floor(i / WALL.cols);
      // the clock ticks every second; the rest repaint on news (and every 5s)
      const m = new Monitor(this.furniture, [1024, 576], [WALL.w, WALL.h], p, i === 3 ? 1 : 5, false);
      m.group.position.set(WALL.x, WALL.y0 + (1 - row) * (WALL.h + WALL.gap) + WALL.h / 2, WALL.z0 + col * (WALL.w + WALL.gap) + WALL.w / 2);
      m.group.rotation.y = -Math.PI / 2;
      this.scene.add(m.group);
      this.wall.push(m);
    });
  }

  private log(text: string, tone: Tone): void {
    this.feed.push({ text, tone, age: 0 });
    if (this.feed.length > 6) this.feed.shift();
  }

  private setFollow(id: string | null): void {
    this.follow = id;
    const d = id ? this.book.get(id) : null;
    this.onFocusChange?.(d ? `${d.who.name.toLowerCase()}'s desk` : null);
  }

  // ── camera ───────────────────────────────────────────────────────────────

  private deskBounds(): { cx: number; cz: number; half: number; depth: number } {
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    for (const s of this.stations.values()) {
      x0 = Math.min(x0, s.group.position.x - 1.6);
      x1 = Math.max(x1, s.group.position.x + 1.6);
      z0 = Math.min(z0, s.group.position.z - 0.5);
      z1 = Math.max(z1, s.group.position.z + 1.1);
    }
    if (!Number.isFinite(x0)) return { cx: -0.9, cz: -3.6, half: 5, depth: 2 };
    return { cx: (x0 + x1) / 2, cz: (z0 + z1) / 2, half: Math.max(3.4, (x1 - x0) / 2), depth: z1 - z0 };
  }

  private tourNext(): Shot {
    const ids = [...this.stations.keys()];
    const seq: Shot[] = [{ kind: 'overview' }, ...ids.map((id) => ({ kind: 'desk', id }) as Shot), { kind: 'wall' }, { kind: 'skyline' }];
    this.tourIdx = (this.tourIdx + 1) % seq.length;
    return seq[this.tourIdx];
  }

  private updateCamera(dt: number): void {
    const aspect = this.W / this.H;
    const b = this.deskBounds();
    let shot: Shot = this.follow ? { kind: 'desk', id: this.follow } : { kind: 'overview' };
    if (this.tour && !this.follow) {
      this.tourT += dt;
      // a call that just landed takes the camera to its desk
      if (this.hot && this.time - this.hot.at < 0.5 && this.stations.has(this.hot.id)
        && !(this.tourShot.kind === 'desk' && this.tourShot.id === this.hot.id)) {
        this.tourShot = { kind: 'desk', id: this.hot.id };
        this.tourT = 0;
      } else if (this.tourT > (this.tourShot.kind === 'overview' ? 9 : 6.5)) {
        this.tourShot = this.tourNext();
        this.tourT = 0;
      }
      shot = this.tourShot;
    }
    const target = this._v2;
    let dist: number, pitch: number, yaw: number, fov = 42, k = 2.2;
    if (shot.kind === 'desk' && this.stations.get(shot.id)) {
      const s = this.stations.get(shot.id)!;
      // over the agent's right shoulder, high enough to clear its head
      target.copy(s.focus).add(this._v.set(0, 0.06, 0));
      dist = 2.9; pitch = 0.5; yaw = 0.4; fov = 46; k = 3;
    } else if (shot.kind === 'wall') {
      target.set(WALL.x, WALL.y0 + WALL.h + WALL.gap / 2, WALL.z0 + (WALL.cols * (WALL.w + WALL.gap)) / 2);
      dist = 8.2; pitch = 0.06; yaw = -Math.PI / 2; fov = 50;
    } else if (shot.kind === 'skyline') {
      target.set(-6, 1.7, ROOM.z0 - 2);
      dist = 9.5; pitch = 0.1; yaw = 0.42; fov = 50;
    } else {
      target.set(b.cx + 2.2, 0.95, b.cz + 0.4);
      pitch = 0.36; yaw = -0.62;
      // fit the desks' width (and the rows' depth) in the frame
      const hf = THREE.MathUtils.degToRad(fov) / 2;
      const byW = (b.half * 1.06) / (Math.tan(hf) * aspect);
      dist = Math.max(8, byW + b.depth * 0.6 + 3.2);
    }
    yaw += this.userYaw;
    const goalYaw = this.yaw + wrapPi(yaw - this.yaw);
    const goalPitch = THREE.MathUtils.clamp(pitch + this.userPitch, -0.15, 1.25);
    const goalDist = dist * this.userZoom;
    this.target.x = damp(this.target.x, target.x, k, dt);
    this.target.y = damp(this.target.y, target.y, k, dt);
    this.target.z = damp(this.target.z, target.z, k, dt);
    this.yaw = damp(this.yaw, goalYaw, k, dt);
    this.pitch = damp(this.pitch, goalPitch, k, dt);
    this.dist = damp(this.dist, goalDist, k, dt);
    this.fov = damp(this.fov, fov, k, dt);
    const cp = Math.cos(this.pitch);
    const p = this.camera.position.set(
      this.target.x + Math.sin(this.yaw) * cp * this.dist,
      this.target.y + Math.sin(this.pitch) * this.dist,
      this.target.z + Math.cos(this.yaw) * cp * this.dist,
    );
    // stay inside the unit (the glass is a wall too)
    p.x = THREE.MathUtils.clamp(p.x, ROOM.x0 + 0.3, ROOM.x1 - 0.3);
    p.z = THREE.MathUtils.clamp(p.z, ROOM.z0 + 0.3, ROOM.z1 - 0.3);
    p.y = THREE.MathUtils.clamp(p.y, 0.4, ROOM.h - 0.25);
    this.camera.lookAt(this.target);
    this.camera.fov = this.fov;
    this.camera.aspect = aspect;
    this.camera.updateProjectionMatrix();
  }

  // ── input ────────────────────────────────────────────────────────────────

  private attachInput(): void {
    this.detach?.();
    const el = this.out;
    if (this.walking) {
      this.detach = this.walker.attach(el);
      return;
    }
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
      this.userPitch = THREE.MathUtils.clamp(d.pitch + dy * 0.003, -0.5, 0.7);
    };
    const up = (e: PointerEvent) => {
      const d = this.drag;
      this.drag = null;
      try { el.releasePointerCapture(e.pointerId); } catch { /* already released */ }
      if (!d || d.moved) return;
      const id = this.pick(e.clientX, e.clientY);
      if (this.tour && id) this.setTour(false);
      if (id) { this.userYaw = 0; this.userPitch = 0; this.userZoom = 1; }
      this.setFollow(id && id !== this.follow ? id : null);
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      this.userZoom = THREE.MathUtils.clamp(this.userZoom * Math.exp(e.deltaY * 0.0012), 0.35, 1.6);
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

  /** The desk under the cursor: nearest rat head or monitor block on screen. */
  private pick(clientX: number, clientY: number): string | null {
    const rect = this.out.getBoundingClientRect();
    const px = ((clientX - rect.left) / rect.width) * this.W;
    const py = ((clientY - rect.top) / rect.height) * this.H;
    let best: string | null = null;
    let bd = (120 * this.ui()) ** 2;
    for (const [id, s] of this.stations) {
      const o = s.group.position;
      const pts = [s.rat.tagWorld(this._v).clone(), s.focus.clone(),
        new THREE.Vector3(o.x, 0.8, o.z + 0.75), new THREE.Vector3(o.x, 0.7, o.z)];
      for (const p of pts) {
        const q = this.project(p);
        if (!q.ok) continue;
        const d = (q.x - px) ** 2 + (q.y - py) ** 2;
        if (d < bd) { bd = d; best = id; }
      }
    }
    return best;
  }

  private ui(): number {
    return Math.min(this.W / 1280, this.H / 720);
  }

  private project(p: THREE.Vector3): { x: number; y: number; ok: boolean } {
    const v = this._v.copy(p).project(this.activeCam());
    return { x: (v.x * 0.5 + 0.5) * this.W, y: (-v.y * 0.5 + 0.5) * this.H, ok: v.z < 1 && v.z > -1 };
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
        console.error('[penthouse] frame failed', err);
      }
    }
  }

  private update(dt: number): void {
    this.time += dt;
    const nowMs = Date.now();
    this.room.update(dt);
    this.updateCamera(dt);
    if (this.walking) {
      this.walker.update(dt);
      if (this.walker.exited) this.setWalk(false);
      else this.poseWalkCam(dt);
    }
    const cam = this.activeCam().position;
    for (const s of this.stations.values()) {
      const near = cam.distanceToSquared(s.focus) < 16;
      s.update(dt, this.time, nowMs, this.smoke, near, this._v);
    }
    for (const w of this.wall) w.update(this.time, nowMs, 0.5);
    this.emberMats.forEach((m, i) => { m.emissiveIntensity = 2.0 + Math.sin(this.time * (1.3 + i * 0.4) + i) * 0.7; });
    this.smoke.update(dt);
    for (const f of this.feed) f.age += dt;
    for (const b of this.bubbles) b.age += dt;
    this.bubbles = this.bubbles.filter((b) => b.age < b.life);
    this.pnlPulse = Math.max(0, this.pnlPulse - dt * 0.8);
    if (this.banner) {
      this.banner.t += dt;
      if (this.banner.t > 3.4) this.banner = null;
    }
  }

  private render(): void {
    this.composer.render();
    const g = this.ctx;
    g.drawImage(this.glCanvas, 0, 0, this.W, this.H);
    const u = this.ui();

    // name tags over each rat (asleep: switched off)
    const tags: TagView[] = [];
    // the desk in close-up says who it is on its own screens
    const framed = this.walking ? (this.leanT > 0.5 ? this.leanFrom : null)
      : this.follow ?? (this.tour && this.tourShot.kind === 'desk' ? this.tourShot.id : null);
    for (const [id, s] of this.stations) {
      const d = this.book.get(id);
      if (!d || id === framed) continue;
      const at = s.rat.tagWorld(this._v2);
      if (this.walking && !visibleFrom(this.walkCam, at, 14)) continue;
      const q = this.project(at);
      if (!q.ok) continue;
      const mode = d.mode === 'off' ? '💤' : d.mode === 'live' ? '● LIVE' : 'PAPER';
      tags.push({ x: q.x, y: q.y, label: `${tagLabel(d.who)} · ${mode}`, color: d.who.color, dim: d.mode === 'off' });
    }
    drawTags(g, tags, u);

    // what it just did, in a clean bubble over its head
    for (const b of this.bubbles) {
      const s = this.stations.get(b.desk);
      if (!s) continue;
      const at = s.rat.tagWorld(this._v2);
      if (this.walking && !visibleFrom(this.walkCam, at, 14)) continue;
      const q = this.project(at);
      if (!q.ok) continue;
      bubble(g, q.x, q.y - 26 * u, b, u, this.W);
    }

    if (this.walking) {
      const id = this.leanIn ?? this.deskNear();
      const d = id ? this.book.get(id) : null;
      if (d) prompt(g, this.W, this.H, u, this.leanIn ? 'E or step away · stand up' : `E · lean in at ${d.who.name}'s desk`);
      else if (this.walker.exitDist < 2) prompt(g, this.W, this.H, u, 'walk into the elevator to leave');
    }

    if (!this.stations.size && this.latest?.config) {
      card(g, this.W, this.H, u, 'Your agents work here', 'Set one up on the AI Agents page and it gets a desk, five monitors and a view.');
    }

    const n = hudNumbers(this.latest?.account ?? null);
    drawHud(g, {
      W: this.W, H: this.H, u, portrait: false,
      pnl: n.pnl, roi: n.roi, winRate: n.winRate, settledToday: n.settledToday, open: n.open,
      wins: 0, losses: 0, pnlPulse: this.pnlPulse,
      feed: this.feed, banner: this.banner, poly: null, showBrand: true,
      brandSub: 'PENTHOUSE · krypt.cc',
      brandColors: ['#67E8F9', '#22D3EE', '#818CF8'],
    });
  }
}

function rr(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  g.beginPath();
  g.roundRect(x, y, w, h, r);
}

/** A modern speech bubble: dark glass, the tone's edge, a soft fade. */
function bubble(g: CanvasRenderingContext2D, x: number, y: number, b: Bubble, u: number, W: number): void {
  const inT = Math.min(1, b.age / 0.18);
  const out = Math.min(1, (b.life - b.age) / 0.4);
  const a = Math.min(inT, out);
  if (a <= 0) return;
  const px = 13 * u;
  g.save();
  g.globalAlpha = a;
  g.font = `600 ${Math.round(px)}px ${F_SANS}, "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"`;
  const tw = g.measureText(b.text).width;
  const w = tw + 22 * u, h = px + 16 * u;
  let bx = x - w / 2;
  bx = Math.max(8 * u, Math.min(W - w - 8 * u, bx));
  const by = y - h - (1 - inT) * 8 * u;
  rr(g, bx, by, w, h, 9 * u);
  g.fillStyle = 'rgba(8,11,20,0.86)';
  g.fill();
  g.strokeStyle = TONE[b.tone].edge;
  g.lineWidth = 1.5 * u;
  g.stroke();
  g.beginPath();
  const tx = Math.max(bx + 14 * u, Math.min(bx + w - 14 * u, x));
  g.moveTo(tx - 6 * u, by + h);
  g.lineTo(tx, by + h + 7 * u);
  g.lineTo(tx + 6 * u, by + h);
  g.fillStyle = 'rgba(8,11,20,0.86)';
  g.fill();
  g.fillStyle = '#F1F5F9';
  g.textBaseline = 'middle';
  g.fillText(b.text, bx + 11 * u, by + h / 2 + 0.5 * u);
  g.restore();
}

function prompt(g: CanvasRenderingContext2D, W: number, H: number, u: number, text: string): void {
  g.save();
  g.font = `600 ${Math.round(14 * u)}px ${F_SANS}`;
  const w = g.measureText(text).width + 28 * u, h = 30 * u;
  const x = (W - w) / 2, y = H - 70 * u;
  rr(g, x, y, w, h, h / 2);
  g.fillStyle = 'rgba(10,6,22,0.78)';
  g.fill();
  g.strokeStyle = 'rgba(167,139,250,0.6)';
  g.lineWidth = 1.2 * u;
  g.stroke();
  g.fillStyle = '#EDE9FE';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(text, W / 2, y + h / 2 + 0.5 * u);
  g.restore();
}

function card(g: CanvasRenderingContext2D, W: number, H: number, u: number, title: string, sub: string): void {
  g.save();
  g.font = `700 ${Math.round(24 * u)}px ${F_SANS}`;
  const w = Math.min(W - 40 * u, Math.max(g.measureText(title).width, 520 * u) + 60 * u);
  const h = 104 * u;
  const x = (W - w) / 2, y = H * 0.55 - h / 2;
  rr(g, x, y, w, h, 16 * u);
  g.fillStyle = 'rgba(6,9,18,0.78)';
  g.fill();
  g.strokeStyle = 'rgba(103,232,249,0.5)';
  g.lineWidth = 1.5 * u;
  g.stroke();
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillStyle = '#F1F5F9';
  g.fillText(title, W / 2, y + 36 * u);
  g.font = `500 ${Math.round(14 * u)}px ${F_SANS}`;
  g.fillStyle = '#94A3B8';
  g.fillText(sub, W / 2, y + 70 * u);
  g.restore();
}
