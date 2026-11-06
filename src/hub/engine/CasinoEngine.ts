import { cacheMeasureText } from '../text';
import * as THREE from 'three';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';
import { CrewKit, ROLE_STYLE, RatFigure, Worker, type Role } from './crew';
import { Smoke } from './smoke';
import { ClubRat, ClubRoom, HALL_BAR_PERSONA } from './club';
import { BlackjackRig } from './tables/blackjack';
import { RouletteRig } from './tables/roulette';
import { SlotRig } from './tables/slots';
import { TerminalRig } from './tables/terminal';
import { SportsbookRig, type StationPlace } from './tables/sportsbook';
import type { Hit, TableRig } from './tables/common';
import { HubAudio, credit, musicMix } from './audio';
import { ViewModel } from './viewmodel';
import { AIM_LINES, BOSS_LINES, BulletHoles, DRUNK_LINES, GRAZE_LINES, SHOT_LINES, pick, rayPastRat, wallBetween } from './shots';
import { HOTBAR, Hotbar, type HotbarEvent } from '../casino/hotbar';
import { drawCrosshair, drawHotbar } from '../casino/hotbarUi';
import { Director, type HubData, type Stage } from './director';
import { CoinPile, Flights, ItemKit, Sparks } from './items';
import { NavGraph } from './nav';
import {
  AGENT_DOCKS, BJ_TABLES, CEIL_H, CLUB, CLUB_DOOR, FLOOR, HALL_BAR_WELL, STAGE, FLOOR_WALK, GAME_SPOTS, POLY_POS, PROP, ROOM, ROOMS, WALL_H, inClub, roomAt,
  type GameSpot, type RoomId,
} from './layout';
import { Walker, visibleFrom } from '../walk';
import { buildWalkShell, type WalkShell } from './walkshell';
import { PERSONA_BY_ID, agentPersona, type Persona } from './npcs';
import { Talk } from './dialogue';
import { drawDialogue, drawTalkPrompt, type Rect } from './talkui';
import {
  drawBubbles, drawHud, drawSigns, drawTags, type BubbleView, type FeedLine, type SignView, type TagView, type Tone,
} from './overlay';
import { configuredAgents, namedAgentIdentity, tagLabel, type AgentIdentity } from '../../utils/agents';
import { hudNumbers } from '../data';
import { paydayLine, type PaydayEvent } from '../payday';
import type { ActivityRecord } from '../../state/activity';
import { CasinoFloor } from './floor';
import { AgentPlay } from './agentPlay';
import { Shoe } from '../casino/blackjack';
import { defaultRng } from '../casino/rng';
import { buildBoard, settleTicket, type BoardRow, type MarketRow } from '../casino/sportsbook';
import { loadSave, storeSave, type CasinoSave } from '../casino/wallet';
import {
  BlackjackSession, CashierSession, RouletteSession, SlotsSession, SportsbookSession, type CasinoCtx, type GameSession,
  BarSession, MIX_S, isPainted,
} from '../casino/sessions';
import { drawChipHud, drawHintBar, drawNowPlaying, drawPlayPrompt, drawTooltip, drawWorldLabels } from '../casino/ui';

export type OutputMode = 'page' | 'cinema';

/** Anyone who can say something: the floor's staff and your agents, Bluey,
 *  and the club's people. */
type Speaker = Worker | 'captain' | ClubRat;

interface Bubble {
  who: Speaker;
  text: string;
  tone: Tone;
  age: number;
  life: number;
  big: boolean;
}

const ISO_EL = Math.atan(1 / Math.SQRT2); // 35.264°: true isometric
/** The 3D frame is drawn at about this many lines and blown up with hard
 *  pixels: the PS2's 480. The HUD, signs and panels stay at full resolution. */
const PS2_LINES = 480;

/** The PS2 frame buffer: 15-bit colour with an ordered (Bayer 4×4) dither,
 *  after tone mapping, in display space. */
const PS2_SHADER = {
  uniforms: {
    tDiffuse: { value: null as THREE.Texture | null }, uRes: { value: new THREE.Vector2(640, 480) },
    uDrunk: { value: 0 }, uTime: { value: 0 },
  },
  vertexShader: /* glsl */`
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: /* glsl */`
    uniform sampler2D tDiffuse; uniform vec2 uRes; uniform float uDrunk; uniform float uTime; varying vec2 vUv;
    float b2(vec2 a) { a = floor(a); return fract(a.x * 0.5 + a.y * a.y * 0.75); }
    float b4(vec2 a) { return b2(0.5 * a) * 0.25 + b2(a); }
    void main() {
      vec4 c = texture2D(tDiffuse, vUv);
      if (uDrunk > 0.001) {
        // double vision that drifts, and the edges going dark
        vec2 o = uDrunk * vec2(0.011 * sin(uTime * 1.3), 0.006 * cos(uTime * 0.9));
        c.rgb = mix(c.rgb, texture2D(tDiffuse, vUv + o).rgb, 0.45 * min(1.0, uDrunk * 1.6));
        c.rgb *= 1.0 - smoothstep(0.3, 0.75, length(vUv - 0.5)) * 0.5 * uDrunk;
      }
      float d = b4(vUv * uRes) - 0.5;
      vec3 q = floor(c.rgb * 31.0 + 0.5 + d) / 31.0;
      gl_FragColor = vec4(clamp(q, 0.0, 1.0), 1.0);
    }`,
};
const ISO_AZ = Math.PI / 4;
const MAX_BUBBLES = 7;
/** At most this many of your agents on the floor at once. */
const MAX_GUESTS = 8;

/** The house staff. Homes are stations from layout.ts; `wander` is where
 *  each one drifts when there's no work, so an idle floor still moves. The
 *  names are the personas you can talk to on foot (npcs.ts). */
const CREW: { role: Role; name: string; home: string; wander: string[] }[] = [
  { role: 'researcher', name: 'Ada', home: 'research.scanA', wander: ['research.wall', 'research.printer'] },
  { role: 'researcher', name: 'Rui', home: 'research.scanB', wander: ['research.wall'] },
  { role: 'smith', name: 'Bo', home: 'forge.anvil', wander: ['forge.furnace', 'forge.outbox'] },
  { role: 'smith', name: 'Kit', home: 'forge.idle', wander: ['forge.furnace', 'forge.inbox'] },
  { role: 'quant', name: 'Quinn', home: 'backtest.tableB', wander: ['backtest.tape'] },
  { role: 'quant', name: 'Nova', home: 'backtest.tape', wander: ['backtest.bin'] },
  { role: 'trader', name: 'Tess', home: 'desk.termA', wander: ['desk.tube'] },
  { role: 'trader', name: 'Max', home: 'desk.termB', wander: ['desk.rack'] },
  { role: 'keeper', name: 'Goldie', home: 'vault.idle', wander: ['vault.pileB'] },
  { role: 'keeper', name: 'Penny', home: 'vault.win', wander: ['vault.pile'] },
  { role: 'risk', name: 'Rex', home: 'vault.risk', wander: ['vault.idle'] },
  { role: 'engineer', name: 'Gus', home: 'optimizer.panel', wander: ['optimizer.reactor'] },
  { role: 'engineer', name: 'Ivy', home: 'optimizer.turbine', wander: ['optimizer.reactor'] },
];

const GAME_COLOR: Record<GameSpot['kind'], string> = {
  blackjack: '#F59E0B', roulette: '#38BDF8', slots: '#2DD4BF', sportsbook: '#A855F7', cashier: '#4ADE80', bar: '#FF5FAE',
};
/** How far you look down, seated: the felt, the wheel and the reels sit
 *  above the game's panel. */
const SEATED_PITCH: Partial<Record<GameSpot['kind'], number>> = {
  blackjack: -0.62, roulette: -0.6, slots: -0.72, cashier: -0.05, bar: -0.36,
};
const GAME_SUB: Record<GameSpot['kind'], string> = {
  blackjack: 'play chips · pays 3:2', roulette: 'play chips · single zero', slots: 'play chips · 94% RTP',
  sportsbook: 'real Polymarket US odds · play-chip tickets', cashier: 'your stack · refills',
  bar: 'cocktails for play chips · they go in your hotbar',
};

/** The keys that walk: pressing one ends a conversation. */
const MOVE_CODES = new Set(['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight']);

function damp(a: number, b: number, k: number, dt: number): number {
  return b + (a - b) * Math.exp(-k * dt);
}

/** A darker shade of a suit colour, for its trim. */
function shade(hex: string, k = 0.45): string {
  const c = new THREE.Color(hex);
  c.multiplyScalar(k);
  return `#${c.getHexString()}`;
}

/**
 * The Agent Hub casino. Renders three.js offscreen, then composites that frame
 * plus every word on screen (signs, bubbles, HUD, the game panels) onto ONE
 * visible 2D canvas, so the page frame and Fullscreen show the same picture.
 */
export class CasinoEngine {
  private out: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private mode: OutputMode = 'page';
  private W = 1280;
  private H = 720;
  private portrait = false;

  private glCanvas = document.createElement('canvas');
  private renderer: THREE.WebGLRenderer;
  private composer: EffectComposer;
  private bloom: UnrealBloomPass;
  private bloomScale = 0.5;
  private rt: THREE.WebGLRenderTarget;
  private scene = new THREE.Scene();
  private camera = new THREE.OrthographicCamera(-10, 10, 10, -10, 0.1, 300);
  private walkCam = new THREE.PerspectiveCamera(68, 16 / 9, 0.05, 260);
  private walker = new Walker(FLOOR_WALK);
  private walking = false;
  private renderPass: RenderPass;
  private shell: WalkShell;
  private doorOpen = 0;
  // talking to the crew on foot (npcs.ts, dialogue.ts)
  private talk: Talk | null = null;
  private talkWith: Speaker | null = null;
  private talkTarget: { who: Speaker; persona: Persona; color: string; d: number } | null = null;
  private talkColor = '#A855F7';
  private talkRects: Rect[] = [];
  private barkAt = new Map<string, number>();
  private barkNext = 0;
  // playing on foot (casino/sessions.ts)
  private game: GameSession | null = null;
  /** The table you're sat at, as a 3D interface (tables/*.ts). */
  private rig: TableRig | null = null;
  /** Drawn sharp, on foot: in the sportsbook (its screens are for reading)
   *  and at any screen you play through (the cashier, the bar). */
  private crisp = false;
  /** The seated camera, eased toward the rig's view. */
  private seatEye = new THREE.Vector3();
  private seatLook = new THREE.Vector3();
  private seatFov = 50;
  /** The cursor over the frame while seated (NDC), and what's under it. */
  private cursor = { x: 0, y: 0, sx: 0, sy: 0, in: false };
  private under: Hit | null = null;
  /** Getting up: the camera eases from the seat back to your eyes. */
  private unseat: { pos: THREE.Vector3; quat: THREE.Quaternion; fov: number; t: number } | null = null;
  private playTarget: { spot: GameSpot; d: number } | null = null;
  private save: CasinoSave = loadSave();
  private shoes = BJ_TABLES.map(() => new Shoe(defaultRng));
  private board: { rows: BoardRow[]; at: number | null; note: string | null } = { rows: [], at: null, note: 'Loading Polymarket US’s sports markets…' };
  private play: AgentPlay;
  private sun: THREE.DirectionalLight;
  private envRT: THREE.WebGLRenderTarget;
  private smoke = new Smoke();
  /** What's in your paw on foot (hotbar.ts), drawn by its own pass. */
  private hb = new Hotbar();
  private vm: ViewModel;
  private vmPass: RenderPass;
  private holes = new BulletHoles();
  /** The muzzle flash lighting the room for a frame or two. */
  private flashLight = new THREE.PointLight('#ffc77a', 0, 14, 2);
  private wispAt = 0;
  private aimCheckAt = 0;
  private aimLineAt = new Map<Worker, number>();
  private bossLineAt = 0;
  private blackedOut = false;
  /** The Velvet Tail, behind the front wall (on foot only). */
  private club: ClubRoom;
  /** Where you're playing (the spot you sat down at). */
  private playSpot: GameSpot | null = null;
  /** Lou, working the round bar's well; where round it he is. */
  private hallBartender: ClubRat;
  private louAngle = 0;
  private clubOpen = 0;
  private clubDoorAt = 0;
  private offStageAt = 0;
  private audio = new HubAudio();
  /** The credit for the track that just started (CC BY), on screen a while. */
  private nowPlaying: { text: string; t: number } | null = null;
  private wasReloading = false;
  private gulpAt = 0;
  private cardsShown = new Map<number, number>();
  private smokeAt = new Map<THREE.Object3D, number>();
  /** The pit boss: a rat in a black fedora, with a cigar. */
  private boss: RatFigure;
  private ps2: ShaderPass;
  /** The 3D frame's size vs the output's (PS2_LINES / H). */
  private rs = 1;

  private nav = new NavGraph();
  private crewKit = new CrewKit();
  private kit = new ItemKit();
  private floor: CasinoFloor;
  private flights: Flights;
  private sparks: Sparks;
  private pile: CoinPile;
  private crew: Worker[] = [];
  /** The user's own agents on the floor, by identity id. */
  private agents = new Map<string, { w: Worker; lastAt: number; who: AgentIdentity; placeholder: boolean }>();
  private director: Director;

  private bubbles: Bubble[] = [];
  private feed: FeedLine[] = [];
  private hot = new Map<RoomId, number>();
  private lastHot: RoomId = 'desk';
  private banner: { text: string; sub: string; t: number } | null = null;
  private pnlPulse = 0;
  private lastPnl: number | null = null;

  // camera
  private target = new THREE.Vector3();
  private targetGoal = new THREE.Vector3();
  private home = new THREE.Vector3();
  private halfH = 10;
  private zoom = 1;
  private zoomGoal = 1;
  private focused: RoomId | null = null;
  private tour = false;
  private tourT = 0;
  private tourFocus = false;
  private paydayFocusUntil = 0;

  // input
  private drag: { x: number; y: number; moved: boolean; tx: number; tz: number } | null = null;
  private detach: (() => void) | null = null;

  // loop
  private raf = 0;
  private running = false;
  private last = 0;
  private time = 0;
  private frameErr = false;
  private fpsFrames = 0;
  private fpsAcc = 0;
  fps = 0;
  onFocusChange: ((label: string | null) => void) | null = null;
  onTourChange: ((on: boolean) => void) | null = null;
  onWalkChange: ((on: boolean) => void) | null = null;

  private _v = new THREE.Vector3();
  private _v2 = new THREE.Vector3();
  private _ray = new THREE.Raycaster();
  private _plane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

  constructor(canvas: HTMLCanvasElement, w: number, h: number) {
    this.out = canvas;
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) throw new Error('2D canvas unavailable');
    this.ctx = cacheMeasureText(ctx);

    this.renderer = new THREE.WebGLRenderer({
      canvas: this.glCanvas, antialias: false, alpha: false, stencil: false,
      powerPreference: 'high-performance',
    });
    this.renderer.setPixelRatio(1);
    this.renderer.toneMapping = THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;

    this.scene.background = new THREE.Color('#07040a');
    const pmrem = new THREE.PMREMGenerator(this.renderer);
    const room = new RoomEnvironment();
    this.envRT = pmrem.fromScene(room, 0.04);
    room.dispose();
    pmrem.dispose();
    this.scene.environment = this.envRT.texture;
    this.scene.environmentIntensity = 0.3;
    this.scene.add(new THREE.HemisphereLight('#ffe1b8', '#3a1420', 0.9));
    this.sun = new THREE.DirectionalLight('#ffd6a8', 1.75);
    this.sun.position.set(-9, 24, 13);
    this.sun.castShadow = true;
    this.sun.shadow.mapSize.set(2048, 2048);
    const sc = this.sun.shadow.camera;
    sc.left = -24; sc.right = 24; sc.top = 19; sc.bottom = -19; sc.near = 1; sc.far = 70;
    this.sun.shadow.bias = -0.0004;
    this.sun.shadow.normalBias = 0.03;
    this.sun.shadow.radius = 3;
    this.scene.add(this.sun, this.sun.target);
    const fill = new THREE.DirectionalLight('#a99cff', 0.45);
    fill.position.set(14, 9, 16);
    this.scene.add(fill);

    this.floor = new CasinoFloor(this.kit);
    this.scene.add(this.floor.night, this.floor.group);
    this.flights = new Flights(this.scene);
    this.sparks = new Sparks(this.kit.glowTex);
    this.scene.add(this.sparks.points);
    this.pile = new CoinPile(PROP.pile[0], PROP.pile[1]);
    this.pile.set(110);
    this.floor.group.add(this.pile.mesh);
    this.shell = buildWalkShell(this.kit.glowTex);
    this.floor.group.add(this.shell.group);
    for (const c of CREW) {
      const w = new Worker(this.crewKit, c.role, c.name, c.home, c.wander, this.nav);
      this.crew.push(w);
      this.floor.group.add(w.group);
    }
    // Bluey, the pit boss: a blue-grey rat at his chair, facing the floor
    this.boss = new RatFigure(this.crewKit, {
      ...ROLE_STYLE.trader, suit: '#1b1e2a', sleeve: '#1b1e2a', neck: 'tie', neckColor: '#F2C66D',
      hatColor: '#0b0b0d', band: '#F2C66D', eyes: 'none',
    }, '#6b7f93', 'cigar');
    this.boss.group.position.set(PROP.podium[0] - 0.5, 0.1, PROP.podium[1]);
    this.boss.group.rotation.y = -Math.PI / 2;
    this.floor.group.add(this.boss.group);
    this.scene.add(this.smoke.points);
    // a light that's always there (at zero) so a shot never adds one and
    // recompiles every material in the hall
    this.scene.add(this.holes.mesh, this.flashLight);
    this.vm = new ViewModel(this.envRT.texture);
    this.club = new ClubRoom(this.crewKit);
    this.floor.group.add(this.club.group);
    this.hallBartender = new ClubRat(this.crewKit, 'bartender', {
      ...ROLE_STYLE.smith, suit: '#f4f1ea', sleeve: '#f4f1ea', neck: 'bowtie', neckColor: '#FBBF24', hatColor: '#1c1917', band: '#FBBF24',
    }, '#9b8b7a', PROP.bar[0] + HALL_BAR_WELL, PROP.bar[1], Math.PI / 2, 'Lou', { emberMat: this.crewKit.embers[0], persona: HALL_BAR_PERSONA });
    this.floor.group.add(this.hallBartender.group);
    this.audio.onTrack = (t) => { this.nowPlaying = { text: credit(t), t: 0 }; };

    const stage: Stage = {
      floor: this.floor, kit: this.kit, flights: this.flights, sparks: this.sparks, pile: this.pile,
      crew: this.crew, root: this.floor.group,
      say: (who, text, tone, opts) => this.say(who, text, tone, opts),
      log: (text, tone) => this.log(text, tone),
      activity: (room, amount) => this.heat(room, amount),
      agent: (who) => this.agentAboard(who),
    };
    this.director = new Director(stage);

    this.play = new AgentPlay({
      time: () => this.time,
      rng: defaultRng,
      floor: this.floor,
      shoe: (t) => this.shoes[t],
      save: this.save,
      persist: () => storeSave(this.save),
      say: (w, text, tone, opts) => this.say(w, text, tone, opts),
      dealerDeals: (t) => this.dealerDeals(t),
      userAtWheel: () => this.game?.kind === 'roulette',
      userAtTable: (t) => this.game instanceof BlackjackSession && this.playTable === t,
      sfx: (kind, at, dur) => {
        if (!this.walking) return;
        if (kind === 'roulette') this.audio.roulette(dur, at);
        else if (kind === 'slots') this.audio.slots(dur, at);
        else this.audio.bells(false, at);
      },
    });

    // MSAA for the overview; resize3d() takes it away on foot (the PS2 had
    // none, and the jaggies are half the look)
    this.rt = new THREE.WebGLRenderTarget(w, h, { type: THREE.HalfFloatType, samples: 4 });
    this.composer = new EffectComposer(this.renderer, this.rt);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.composer.addPass(this.renderPass);
    // your paw and what's in it, over the hall (depth cleared first)
    this.vmPass = new RenderPass(this.vm.scene, this.vm.camera);
    this.vmPass.clear = false;
    this.vmPass.clearDepth = true;
    this.vmPass.enabled = false;
    this.composer.addPass(this.vmPass);
    // A soft glow on the neon, the bulbs and the screens — kept low: the
    // hall should read lit by its lamps, not hazed by its signs.
    this.bloom = new UnrealBloomPass(new THREE.Vector2(w, h), 0.4, 0.3, 1.05);
    this.bloomScale = 0.5;
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());
    this.ps2 = new ShaderPass(PS2_SHADER);
    this.composer.addPass(this.ps2);

    this.setOutput(canvas, w, h, 'page');
  }

  /** The blackjack table your session is at (for the agents' cards). */
  private playTable = -1;

  // ── public API ───────────────────────────────────────────────────────────

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
    this.audio.start();
  }

  stop(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.audio.stop();
  }

  /** Sound on or off (remembered in this browser profile). */
  soundMuted(): boolean {
    return this.audio.muted;
  }

  setSoundMuted(m: boolean): void {
    this.audio.setMuted(m);
    if (m) this.nowPlaying = null;
  }

  ingest(d: HubData): void {
    this.director.ingest(d);
    const pnl = hudNumbers(d.account).pnl;
    if (pnl !== null && this.lastPnl !== null) {
      const bank = d.account?.startBankrollUsd || 1000;
      this.pnlPulse = Math.max(this.pnlPulse, Math.min(1, Math.abs(pnl - this.lastPnl) / (bank * 0.01)));
    }
    this.lastPnl = pnl;
    if (d.sportsbook !== undefined) {
      const s = d.sportsbook;
      this.board = s
        ? { rows: buildBoard(s.rows), at: s.at, note: s.note ?? (s.rows.length ? null : 'No sports markets with a price right now') }
        : { rows: this.board.rows, at: this.board.at, note: this.board.rows.length ? this.board.note : 'Polymarket US could not be read just now' };
      this.floor.setBoard(this.board.rows, this.board.at, this.board.note);
    }
    this.roster(d);
  }

  activity(rec: ActivityRecord, replay: boolean): void {
    this.director.activity(rec, replay);
  }

  scriptLog(name: string | null, lines: string[]): void {
    this.director.scriptLog(name, lines);
  }

  payday(ev: PaydayEvent): void {
    this.banner = { text: 'PAYDAY!', sub: paydayLine(ev), t: 0 };
    this.director.payday();
    this.say('captain', 'PAYDAY! 💰💰💰', 'gold', { force: true, big: true, life: 3.5 });
    this.log(`💰 PAYDAY ${paydayLine(ev)}`, 'gold');
    this.heat('vault', 3);
    if (!this.focused || this.focused === 'vault' || this.tour) this.paydayFocusUntil = this.time + 4.5;
  }

  /** The markets whose result your open sportsbook tickets wait on. */
  marketsWanted(): string[] {
    return [...new Set(this.save.tickets.filter((t) => t.status === 'open').map((t) => t.conditionId))].slice(0, 10);
  }

  /** Those markets, as Polymarket reports them now: settle what resolved. */
  marketsRead(rows: MarketRow[]): void {
    let changed = false;
    this.save.tickets = this.save.tickets.map((t) => {
      if (t.status !== 'open') return t;
      const m = rows.find((r) => r.conditionId === t.conditionId);
      const s = settleTicket(t, m ?? null);
      if (s.status !== 'open') {
        changed = true;
        this.save.chips += s.paid;
        const line = s.status === 'won' ? `🎫 ticket won: ${s.label} · +${s.paid.toLocaleString('en-US')} chips`
          : s.status === 'void' ? `🎫 ticket void: ${s.label} · stake back` : `🎫 ticket lost: ${s.label}`;
        this.log(line, s.status === 'won' ? 'win' : 'neutral');
      }
      return s;
    });
    if (changed) storeSave(this.save);
  }

  setOutput(canvas: HTMLCanvasElement, w: number, h: number, mode: OutputMode): void {
    w = Math.max(64, Math.round(w));
    h = Math.max(64, Math.round(h));
    if (canvas !== this.out) {
      const ctx = canvas.getContext('2d', { alpha: false });
      if (!ctx) throw new Error('2D canvas unavailable');
      this.out = canvas;
      this.ctx = cacheMeasureText(ctx);
    }
    this.mode = mode;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;
    this.W = w;
    this.H = h;
    this.portrait = h > w;
    this.resize3d();
    this.fit();
    this.attachInput();
  }

  /** On foot the 3D frame is PS2-sized — ~480 lines, no anti-aliasing,
   *  15-bit colour and a dither — and render() blows it up with hard
   *  pixels. The isometric overview is a dashboard: full resolution,
   *  anti-aliased, clean. */
  private resize3d(): void {
    const w = this.W, h = this.H;
    // on foot the PS2's 480 lines; sat at a table, twice that, so the felt,
    // the reels and the screens read; the overview, full resolution
    this.rs = !this.walking || this.crisp ? 1 : Math.min(1, (this.game ? PS2_LINES * 1.9 : PS2_LINES) / h);
    const rw = Math.max(64, Math.round(w * this.rs)), rh = Math.max(64, Math.round(h * this.rs));
    const samples = this.walking && !this.crisp ? 0 : 4;
    for (const t of [this.composer.renderTarget1, this.composer.renderTarget2]) {
      if (t.samples === samples) continue;
      t.samples = samples;
      t.dispose(); // reallocated, with the new sample count, on next use
    }
    this.renderer.setSize(rw, rh, false);
    this.composer.setSize(rw, rh);
    this.bloom.setSize(Math.round(rw * this.bloomScale), Math.round(rh * this.bloomScale));
    this.ps2.enabled = this.walking && !this.crisp;
    this.ps2.uniforms.uRes.value.set(rw, rh);
    this.vm.setAspect(w / h);
    this.smoke.setViewHeight(rh);
    const ps = this.uiScale() * this.rs;
    this.sparks.setPixelScale(ps);
    this.floor.setPixelScale(ps);
  }

  setTour(on: boolean): void {
    if (on && this.walking) this.setWalk(false);
    this.tour = on;
    this.tourT = 3.5;
    this.tourFocus = false;
    if (!on) this.focus(null);
    this.onTourChange?.(on);
  }

  resetView(): void {
    this.focus(null);
  }

  /** Esc from the stage: leaves a table or ends a conversation first (true =
   *  taken), so the walk only ends on the next one. */
  escape(): boolean {
    if (this.game) {
      this.endGame();
      return true;
    }
    if (!this.talk) return false;
    this.endTalk();
    return true;
  }

  // ── on foot: talk and play ───────────────────────────────────────────────

  private walkKey(code: string, repeat = false): boolean {
    // Walking off ends any conversation or interaction there and then: the
    // key isn't taken, so you're already moving, and the mouse looks again (a
    // key press may ask for the pointer back; Esc can't, so after Esc it
    // waits for a click). A key still held from walking up (its auto-repeat)
    // doesn't count: you only leave on a fresh press.
    if ((this.game || this.talk) && MOVE_CODES.has(code)) {
      if (repeat) return true;
      if (this.game) this.endGame();
      if (this.talk) this.endTalk();
      this.walker.relock();
      return false;
    }
    const gm = this.game;
    if (gm) {
      gm.key(code);
      return true; // seated at a table, every other key is the game's
    }
    const t = this.talk;
    if (t) {
      if (code === 'KeyE' || code === 'Enter' || code === 'Space') t.advance();
      else if (/^Digit[1-9]$/.test(code)) t.choose(Number(code.slice(5)) - 1);
      if (t.closed) this.endTalk();
      return true;
    }
    // out cold: nothing to do but wait to come to
    if (this.hb.black > 0) return true;
    // the hotbar: 1–5 pick, Q the next, F (or a click) uses it, R reloads
    if (/^Digit[1-6]$/.test(code)) { this.hbEvents(this.hb.select(Number(code.slice(5)) - 1)); return true; }
    if (code === 'KeyQ') { this.hbEvents(this.hb.cycle(1)); return true; }
    if (code === 'KeyR') { this.hb.reloadGun(); return true; }
    if (code === 'KeyF') { if (!repeat) this.useItem(true); return true; }
    if (code === 'KeyE') {
      // whichever you face more squarely and stand nearer: a table, or a person
      const p = this.playTarget, tk = this.talkTarget;
      if (p && (!tk || p.d <= tk.d + 0.4)) { this.startGame(p.spot); return true; }
      if (tk) { this.startTalk(); return true; }
    }
    return false;
  }

  private useItem(down: boolean): void {
    if (!down || this.game || this.talk) {
      this.hbEvents(this.hb.release());
      return;
    }
    this.hbEvents(this.hb.press());
  }

  private walkClick(clientX: number, clientY: number): void {
    const r = this.out.getBoundingClientRect();
    const x = ((clientX - r.left) / r.width) * this.W;
    const y = ((clientY - r.top) / r.height) * this.H;
    if (this.game) {
      this.seatPointer(clientX, clientY);
      const h = this.pickAt(this.cursor.x, this.cursor.y);
      if (h) {
        this.audio.click();
        if (h.id === 'refill' && this.rig instanceof TerminalRig) this.rig.rang();
        if (h.id.startsWith('chip:') || h.id.startsWith('bet:') || h.id === 'bet') this.audio.chips(2);
        this.game.click(h.id);
      }
      return;
    }
    const t = this.talk;
    if (!t) return;
    const i = this.talkRects.findIndex((b) => x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h);
    if (t.mode === 'choose' && i >= 0) t.choose(i);
    else if (t.mode === 'say') t.advance();
    if (t.closed) this.endTalk();
  }

  private casinoCtx(): CasinoCtx {
    return {
      save: this.save,
      persist: () => storeSave(this.save),
      rng: defaultRng,
      shoe: (t) => this.shoes[t],
      bjCards: (t, dealer, hands) => {
        this.floor.setBjCards(t, dealer, hands);
        // the session pushes its cards every frame: a card sound only when one lands
        const n = dealer.length + hands.reduce((a, h) => a + h.length, 0);
        if (n > (this.cardsShown.get(t) ?? 0)) this.audio.card();
        this.cardsShown.set(t, n);
      },
      dealerDeals: (t) => this.dealerDeals(t),
      spinWheel: (n, dur) => { this.audio.roulette(dur); this.floor.spinRoulette(n, dur); },
      wheelBusy: () => this.floor.rouletteBusy,
      wheelJoin: (n, dur) => this.play.joinSpin(n, dur),
      spinSlot: (i, stops, dur) => { this.audio.slots(dur); this.floor.pullLever(i); this.floor.spinSlot(i, stops, dur); },
      slotWin: (i) => { this.audio.bells(true); this.floor.slotWin(i); },
      board: () => this.board,
      brag: (text) => { this.audio.chips(8); this.log(text, 'gold'); },
    };
  }

  private startGame(spot: GameSpot): void {
    const c = this.casinoCtx();
    this.playTable = -1;
    this.playSpot = spot;
    switch (spot.kind) {
      case 'blackjack': this.game = new BlackjackSession(c, spot.table); this.playTable = spot.table; break;
      case 'roulette': this.game = new RouletteSession(c); break;
      case 'slots': this.game = new SlotsSession(c, spot.table); break;
      case 'sportsbook': this.game = new SportsbookSession(c); break;
      case 'cashier': this.game = new CashierSession(c, () => this.play.stacks()); break;
      case 'bar': {
        // the bartender across the counter shakes it; it lands in slot six
        const bt = spot.id.startsWith('hb') ? this.hallBartender : this.club.bartenderAt(spot.z);
        this.game = new BarSession(c, bt?.name ?? 'Dot', {
          mix: () => {
            bt?.mix(MIX_S);
            this.audio.shaker(MIX_S, bt ? { x: bt.x, z: bt.z } : undefined);
          },
          serve: (d) => {
            this.hb.giveCocktail(d);
            this.hb.select(HOTBAR.findIndex((h) => h.id === 'cocktail'));
            this.audio.clink();
            if (bt) this.say(bt, pick([`One ${d.name}. Enjoy, sugar.`, 'There you go, hon.', 'Sip it slow.']), 'neutral', { force: true, life: 2.6 });
          },
        });
        break;
      }
    }
    this.hbEvents(this.hb.release());
    this.walker.frozen = true;
    if (document.pointerLockElement) document.exitPointerLock();
    // Take your place at it, looking down at the felt (or up at the board):
    // what the game does on the table shows above its panel.
    this.walker.x = spot.x;
    this.walker.z = spot.z;
    this.walker.yaw = Math.atan2(-(spot.lx - spot.x), -(spot.lz - spot.z));
    this.walker.pitch = SEATED_PITCH[spot.kind] ?? 0;
    if (spot.id === 'sbW') this.walker.pitch = 0.1;
    // the table itself is the interface: its rig, and a camera framed on it
    // (eased in from where your eyes were)
    this.rig = this.makeRig(spot);
    if (this.rig) this.scene.add(this.rig.group);
    this.seatEye.copy(this.walkCam.position);
    this.seatLook.copy(this.walkCam.position).add(new THREE.Vector3(0, 0, -1.5).applyQuaternion(this.walkCam.quaternion));
    this.seatFov = this.walkCam.fov;
    this.unseat = null;
    this.cursor.in = false;
    this.under = null;
    this.resize3d();
  }

  private makeRig(spot: GameSpot): TableRig | null {
    const g = this.game;
    if (!g) return null;
    if (g instanceof BlackjackSession) return new BlackjackRig(g);
    if (g instanceof RouletteSession) return new RouletteRig(g, this.floor);
    if (g instanceof SlotsSession) return new SlotRig(g, this.floor);
    if (g instanceof SportsbookSession) return new SportsbookRig(g, spot.id as StationPlace);
    if (isPainted(g)) return new TerminalRig(g, spot);
    return null;
  }

  private endGame(): void {
    this.game?.close();
    this.game = null;
    this.playSpot = null;
    this.playTable = -1;
    this.rig?.dispose();
    this.rig = null;
    this.under = null;
    this.out.style.cursor = '';
    // ease back up out of the seat
    this.unseat = { pos: this.walkCam.position.clone(), quat: this.walkCam.quaternion.clone(), fov: this.walkCam.fov, t: 0 };
    this.walker.frozen = false;
    this.walker.pitch = 0;
    storeSave(this.save);
    this.resize3d();
  }

  /** What's under the cursor at the table (and the hand cursor over it). */
  private pickAt(nx: number, ny: number): Hit | null {
    if (!this.rig) return null;
    this._ray.setFromCamera(new THREE.Vector2(nx, ny), this.walkCam);
    const h = this.rig.pick(this._ray);
    this.rig.hover(h?.id ?? null);
    this.out.style.cursor = h ? 'pointer' : '';
    return h;
  }

  private seatPointer(clientX: number, clientY: number): void {
    const r = this.out.getBoundingClientRect();
    this.cursor.x = ((clientX - r.left) / r.width) * 2 - 1;
    this.cursor.y = -(((clientY - r.top) / r.height) * 2 - 1);
    this.cursor.in = true;
  }

  /** The dealer at blackjack table t deals, if it's standing at its table. */
  private dealerDeals(t: number): void {
    const home = t === 0 ? 'forge.anvil' : 'forge.idle';
    const w = this.crew.find((c) => c.home === home);
    if (w && w.atHome) w.assign([{ k: 'work', anim: 'hammer', dur: 1.1 }]);
  }

  private startTalk(): void {
    const tg = this.talkTarget;
    if (!tg) return;
    this.talk = new Talk(tg.persona);
    this.talkWith = tg.who;
    this.talkColor = tg.color;
    this.hbEvents(this.hb.release());
    this.walker.frozen = true;
    if (document.pointerLockElement) document.exitPointerLock();
    if (tg.who !== 'captain') tg.who.hold({ x: this.walker.x, z: this.walker.z });
  }

  private endTalk(): void {
    if (this.talkWith && this.talkWith !== 'captain') this.talkWith.hold(null);
    this.talk = null;
    this.talkWith = null;
    this.talkRects = [];
    this.walker.frozen = false;
  }

  /** Who you'd talk to and what you'd play: the nearest within reach and
   *  roughly in front of you. */
  private findTargets(): void {
    const fx = -Math.sin(this.walker.yaw), fz = -Math.cos(this.walker.yaw);
    let best: { who: Speaker; persona: Persona; color: string; d: number } | null = null;
    let bestScore = Infinity;
    const consider = (who: Speaker, x: number, z: number, persona: Persona | undefined, color: string) => {
      if (!persona) return;
      // nobody across a wall: not into the club from the hall, nor back
      if (wallBetween(this.walker.x, this.walker.z, x, z, FLOOR_WALK.walls)) return;
      const dx = x - this.walker.x, dz = z - this.walker.z;
      const d = Math.hypot(dx, dz);
      if (d > 2.4 || d < 1e-3) return;
      const cos = (dx * fx + dz * fz) / d;
      if (cos < 0.86) return;
      const score = d * (2 - cos);
      if (score < bestScore) { bestScore = score; best = { who, persona, color, d: score }; }
    };
    for (const w of this.crew) {
      const h = w.headWorld(this._v);
      if (w.role === 'agent') {
        if (w.tag) consider(w, h.x, h.z, agentPersona(w.tag.label), w.tag.color);
      } else {
        consider(w, h.x, h.z, PERSONA_BY_ID.get(w.name), ROLE_COLOR[w.role] ?? '#A855F7');
      }
    }
    consider('captain', PROP.podium[0], PROP.podium[1], PERSONA_BY_ID.get('captain'), '#FBBF24');
    consider(this.hallBartender, this.hallBartender.x, this.hallBartender.z, HALL_BAR_PERSONA, '#FBBF24');
    if (this.club.inside.visible) {
      for (const r of this.club.rats) if (r.persona) consider(r, r.x, r.z, r.persona, '#FF5FAE');
    }
    this.talkTarget = best;

    let play: { spot: GameSpot; d: number } | null = null;
    let playScore = Infinity;
    for (const s of GAME_SPOTS) {
      const d = Math.hypot(s.x - this.walker.x, s.z - this.walker.z);
      if (d > 1.9) continue;
      const lx = s.lx - this.walker.x, lz = s.lz - this.walker.z;
      const ld = Math.hypot(lx, lz) || 1;
      const cos = (lx * fx + lz * fz) / ld;
      if (cos < 0.62) continue;
      const score = d * (2 - cos);
      if (score < playScore) { playScore = score; play = { spot: s, d: score }; }
    }
    this.playTarget = play;
  }

  private barks(): void {
    if (this.time < this.barkNext) return;
    // in the club: the patrons, the door and the bar
    if (this.club.inside.visible && inClub(this.walker.x, this.walker.z)) {
      for (const r of this.club.rats) {
        if (Math.hypot(r.x - this.walker.x, r.z - this.walker.z) > 3.6) continue;
        if ((this.barkAt.get(r.name) ?? 0) > this.time) continue;
        const lines = this.club.barkLines(r);
        if (this.say(r, pick(lines), 'neutral', { life: 3 })) {
          this.barkAt.set(r.name, this.time + 25 + Math.random() * 20);
          this.barkNext = this.time + 6 + Math.random() * 5;
          return;
        }
      }
    }
    if (this.inTheClub) return;
    for (const w of this.crew) {
      if (w.role === 'agent') continue;
      const p = PERSONA_BY_ID.get(w.name);
      if (!p?.barks.length) continue;
      const h = w.headWorld(this._v);
      if (Math.hypot(h.x - this.walker.x, h.z - this.walker.z) > 4.2) continue;
      if ((this.barkAt.get(w.name) ?? 0) > this.time) continue;
      const line = this.hb.buzz > 0.5 && Math.random() < 0.6 ? pick(DRUNK_LINES) : p.barks[Math.floor(Math.random() * p.barks.length)];
      if (this.say(w, line, 'neutral', { life: 3.4 })) {
        this.barkAt.set(w.name, this.time + 30 + Math.random() * 25);
        this.barkNext = this.time + 7 + Math.random() * 6;
        return;
      }
    }
  }

  setWalk(on: boolean): void {
    if (on === this.walking) return;
    this.walking = on;
    if (on) {
      if (this.tour) this.setTour(false);
      this.focus(null);
      this.paydayFocusUntil = 0;
      this.walker.reset();
      this.walker.onKey = (code, repeat) => this.walkKey(code, repeat);
      this.walker.onClick = (x, y) => this.walkClick(x, y);
      this.walker.onLand = () => this.audio.land();
      this.walker.onPointer = (x, y) => { if (this.game) this.seatPointer(x, y); };
      this.walker.onWheel = (dir) => { this.game?.wheel?.(dir); };
      this.walker.onUse = (down) => this.useItem(down);
      this.walker.onKeyUp = (code) => { if (code === 'KeyF') this.useItem(false); };
      this.hb.reset();
      this.blackedOut = false;
      this.doorOpen = 0;
      this.shell.setDoor(0);
    } else {
      if (this.game) this.endGame();
      this.endTalk();
      this.talkTarget = null;
      this.playTarget = null;
      this.hb.release();
      this.audio.stopDrag();
      this.walker.drunk = 0;
      this.walker.frozen = false;
      this.ps2.uniforms.uDrunk.value = 0;
      this.flashLight.intensity = 0;
    }
    this.shell.group.visible = on;
    this.club.group.visible = on;
    this.floor.overviewOnly.visible = !on;
    // on foot the far end of the hall goes into a smoky haze
    this.scene.fog = on ? new THREE.Fog('#0d0709', 7, 30) : null;
    this.renderPass.camera = on ? this.walkCam : this.camera;
    this.vmPass.enabled = on;
    this.crisp = this.wantCrisp();
    this.resize3d();
    this.attachInput();
    this.onWalkChange?.(on);
  }

  focus(room: RoomId | null): void {
    this.focused = room;
    if (room) {
      const r = ROOM[room];
      this.targetGoal.set(r.cx, 0.6, r.cz);
      this.zoomGoal = this.portrait ? 1.9 : 2.2;
    } else {
      this.targetGoal.copy(this.home);
      this.zoomGoal = 1;
    }
    this.onFocusChange?.(room ? ROOM[room].name.toLowerCase() : null);
  }

  get focusedRoom(): RoomId | null {
    return this.focused;
  }

  dispose(): void {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.detach?.();
    storeSave(this.save);
    this.flights.clear();
    const geos = new Set<THREE.BufferGeometry>();
    const mats = new Set<THREE.Material>();
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.geometry) geos.add(m.geometry);
      const mat = m.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(mat)) mat.forEach((x) => mats.add(x));
      else if (mat) mats.add(mat);
    });
    for (const g of Object.values(this.crewKit.g)) geos.add(g);
    for (const g of Object.values(this.kit.g)) geos.add(g);
    geos.forEach((g) => g.dispose());
    mats.forEach((m) => m.dispose());
    this.floor.dispose();
    this.shell.dispose();
    this.smoke.dispose();
    this.vm.dispose();
    this.club.dispose();
    this.audio.dispose();
    this.holes.dispose();
    this.kit.glowTex.dispose();
    this.bloom.dispose();
    this.rt.dispose();
    this.envRT.dispose();
    this.composer.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.scene.clear();
  }

  // ── the user's agents ────────────────────────────────────────────────────

  /** Your configured agents come in as guests and play between jobs. The
   *  unrenamed Default agent has no name of its own until a client drives
   *  it, so it arrives as "Default" and takes that client's name on its
   *  first real call (agentAboard). */
  private roster(d: HubData): void {
    const agents = configuredAgents(d.config ?? null);
    if (!agents) return;
    for (const a of agents.filter((x) => x.enabled !== false).slice(0, 6)) {
      const who = namedAgentIdentity({ id: a.id, name: a.name || 'Default', color: a.color ?? '', emoji: a.emoji, guide: a.guide });
      if (this.agents.has(who.id)) continue;
      const placeholder = a.id === 'default' && (!a.name || a.name === 'Default');
      // the Default agent already on the floor under its client's name
      if (placeholder && [...this.agents.values()].some((x) => !x.who.id.startsWith('agent|'))) continue;
      const w = this.agentAboard(who);
      const e = this.agents.get(who.id);
      if (w && e) e.placeholder = placeholder;
    }
  }

  /** One guest per agent, at a free spot in the VIP lounge; the play loop
   *  then seats it at a game. A real call by an agent not yet here brings
   *  it in (the director only asks when a real call arrives). */
  private agentAboard(who: AgentIdentity): Worker | null {
    const have = this.agents.get(who.id);
    if (have) {
      have.lastAt = this.time;
      return have.w;
    }
    // the unrenamed Default agent's first real call: it was the placeholder
    if (!who.id.startsWith('agent|')) {
      const ph = this.agents.get('agent|default');
      if (ph?.placeholder) {
        this.agents.delete('agent|default');
        ph.placeholder = false;
        ph.who = who;
        ph.lastAt = this.time;
        ph.w.tag = { label: tagLabel(who), color: who.color };
        this.agents.set(who.id, ph);
        this.play.rekey('agent|default', who.id, who);
        return ph.w;
      }
    }
    if (this.agents.size >= MAX_GUESTS) {
      const idle = [...this.agents.entries()].filter(([, a]) => a.w.free).sort((a, b) => a[1].lastAt - b[1].lastAt)[0];
      if (!idle) return null;
      this.dropAgent(idle[0]);
    }
    const used = new Set([...this.agents.values()].map((a) => a.w.home));
    const dock = AGENT_DOCKS.find((d) => !used.has(d)) ?? AGENT_DOCKS[this.agents.size % AGENT_DOCKS.length];
    const w = new Worker(this.crewKit, 'agent', who.name, dock, [], this.nav, { suit: who.color, trim: shade(who.color) });
    w.tag = { label: tagLabel(who), color: who.color };
    this.agents.set(who.id, { w, lastAt: this.time, who, placeholder: false });
    this.crew.push(w);
    this.floor.group.add(w.group);
    this.play.add(who.id, w, who);
    const p = w.group.position;
    this.sparks.burst(p.x, 1.0, p.z, who.color, 14, 1.6, 1.8);
    return w;
  }

  private dropAgent(id: string): void {
    const a = this.agents.get(id);
    if (!a) return;
    this.agents.delete(id);
    this.play.remove(id);
    const i = this.crew.indexOf(a.w);
    if (i >= 0) this.crew.splice(i, 1);
    this.bubbles = this.bubbles.filter((b) => b.who !== a.w);
    a.w.dispose(this.crewKit);
  }

  // ── speech + feed ────────────────────────────────────────────────────────

  private say(who: Speaker, text: string, tone: Tone, opts: { big?: boolean; force?: boolean; life?: number } = {}): boolean {
    const mine = this.bubbles.findIndex((b) => b.who === who);
    if (mine >= 0) {
      if (!opts.force) return false;
      this.bubbles.splice(mine, 1);
    }
    if (this.bubbles.length >= MAX_BUBBLES) {
      if (!opts.force) return false;
      this.bubbles.sort((a, b) => b.age - a.age);
      this.bubbles.shift();
    }
    this.bubbles.push({ who, text, tone, age: 0, life: opts.life ?? (opts.big ? 3.2 : 2.7), big: !!opts.big });
    // on foot, you hear them say it (a squeak of "voice", from where they are)
    if (this.walking && this.sameRoom(who)) {
      const at = who === 'captain' ? { x: PROP.podium[0], z: PROP.podium[1] } : { x: who.headWorld(this._v2).x, z: this._v2.z };
      this.audio.voice(who === 'captain' ? 'Bluey' : who.name, text, at);
    }
    return true;
  }

  private log(text: string, tone: Tone): void {
    this.feed.push({ text, tone, age: 0 });
    if (this.feed.length > 6) this.feed.shift();
  }

  private heat(room: RoomId, amount = 1): void {
    this.hot.set(room, Math.min(1.5, (this.hot.get(room) ?? 0) + amount));
    if (amount >= 0.8) this.lastHot = room;
  }

  // ── camera ───────────────────────────────────────────────────────────────

  private uiScale(): number {
    return this.portrait ? this.W / 720 : Math.min(this.W / 1280, this.H / 720);
  }

  private viewDir(az: number, out: THREE.Vector3): THREE.Vector3 {
    return out.set(Math.cos(ISO_EL) * Math.sin(az), Math.sin(ISO_EL), Math.cos(ISO_EL) * Math.cos(az));
  }

  /** Frame the whole hall (and POLYMARKET, in landscape) at zoom 1, from
   *  its real projected bounds. */
  private fit(): void {
    const pts: THREE.Vector3[] = [];
    const { x0, x1, z0, z1 } = FLOOR;
    for (const [x, z] of [[x0 - 0.4, z0 - 0.4], [x0 - 0.4, z1], [x1, z0 - 0.4], [x1, z1]] as const) {
      pts.push(new THREE.Vector3(x, -0.8, z), new THREE.Vector3(x, x < x0 + 1 || z < z0 + 1 ? WALL_H + 0.6 : 0.4, z));
    }
    if (!this.portrait) {
      for (const s of [-2, 2]) pts.push(new THREE.Vector3(POLY_POS[0] + s * 0.707, POLY_POS[1] + 2.4, POLY_POS[2] - s * 0.707));
    }
    const dir = this.viewDir(ISO_AZ, this._v);
    const right = new THREE.Vector3(dir.z, 0, -dir.x).normalize();
    const up = new THREE.Vector3().crossVectors(dir, right).normalize();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const p of pts) {
      const sx = p.dot(right), sy = p.dot(up);
      minX = Math.min(minX, sx); maxX = Math.max(maxX, sx);
      minY = Math.min(minY, sy); maxY = Math.max(maxY, sy);
    }
    const aspect = this.W / this.H;
    const padTop = this.portrait ? 0.3 : 0.04;
    const padBot = this.portrait ? 0.26 : 0.02;
    const needH = (maxY - minY) / (1 - padTop - padBot);
    const needW = maxX - minX;
    this.halfH = Math.max(needH, needW / aspect) / 2;
    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2 + (padTop - padBot) * this.halfH;
    const g = new THREE.Vector3().addScaledVector(right, cx).addScaledVector(up, cy);
    const t = -g.y / dir.y;
    g.addScaledVector(dir, t);
    this.home.copy(g);
    if (!this.focused) {
      this.targetGoal.copy(this.home);
      if (this.time === 0) this.target.copy(this.home);
    }
  }

  private updateCamera(dt: number): void {
    if (this.walking) {
      this.walker.update(dt);
      if (this.walker.exited) {
        this.setWalk(false);
        this.updateCamera(dt);
        return;
      }
      const want = Math.max(0, Math.min(1, (1.7 - this.walker.exitDist) / 0.9));
      this.doorOpen = damp(this.doorOpen, want, 6, dt);
      this.shell.setDoor(this.doorOpen);
      // the club's padded doors swing in as you come, and the room behind
      // them is only drawn while you could see into it
      const cd = this.clubDoorDist();
      const wantClub = Math.max(0, Math.min(1, (1.9 - cd) / 0.9));
      if (wantClub > 0.3 && this.clubOpen <= 0.3 && this.time >= this.clubDoorAt) {
        this.audio.door({ x: CLUB_DOOR[0], z: CLUB.z0 });
        this.clubDoorAt = this.time + 1.5;
      }
      this.clubOpen = damp(this.clubOpen, wantClub, 5, dt);
      this.club.setDoor(this.clubOpen);
      this.club.inside.visible = inClub(this.walker.x, this.walker.z) || this.clubOpen > 0.02;
      if (this.game) {
        this.game.update(dt);
      } else if (this.talk) {
        this.talk.update(dt);
        if (this.talkWith && this.talkWith !== 'captain') this.talkWith.hold({ x: this.walker.x, z: this.walker.z });
      } else {
        this.findTargets();
        this.barks();
      }
      this.walker.apply(this.walkCam);
      this.walkCam.fov = this.walker.fov;
      const crisp = this.wantCrisp();
      if (crisp !== this.crisp) {
        this.crisp = crisp;
        this.resize3d();
      }
      if (this.rig && this.game) this.seatCamera(dt);
      else if (this.unseat) {
        const u = this.unseat;
        u.t += dt / 0.45;
        const k = u.t >= 1 ? 1 : u.t * u.t * (3 - 2 * u.t);
        this.walkCam.position.lerpVectors(u.pos, this.walkCam.position, k);
        this.walkCam.quaternion.slerpQuaternions(u.quat, this.walkCam.quaternion, k);
        this.walkCam.fov = u.fov + (this.walkCam.fov - u.fov) * k;
        this.walkCam.updateMatrixWorld();
        if (u.t >= 1) this.unseat = null;
      }
      this.walkCam.aspect = this.W / this.H;
      this.walkCam.updateProjectionMatrix();
      this.hands(dt);
      this.sun.target.position.set(0, 0, 0);
      return;
    }
    if (this.tour && !this.drag) {
      this.tourT -= dt;
      if (this.tourT <= 0) {
        this.tourFocus = !this.tourFocus;
        this.tourT = this.tourFocus ? 4.5 : 6;
        this.focus(this.tourFocus ? this.lastHot : null);
      }
    }
    if (this.paydayFocusUntil > this.time) {
      if (this.focused !== 'vault') this.focus('vault');
    } else if (this.paydayFocusUntil > 0) {
      this.paydayFocusUntil = 0;
      if (!this.tour) this.focus(null);
    }

    this.target.x = damp(this.target.x, this.targetGoal.x, 3.2, dt);
    this.target.y = damp(this.target.y, this.targetGoal.y, 3.2, dt);
    this.target.z = damp(this.target.z, this.targetGoal.z, 3.2, dt);
    this.zoom = damp(this.zoom, this.zoomGoal, 3.2, dt);

    const t = this.time;
    const az = ISO_AZ + Math.sin(t * 0.11) * 0.035;
    const dir = this.viewDir(az, this._v);
    const drift = this._v2.set(Math.sin(t * 0.13) * 0.35, 0, Math.cos(t * 0.1) * 0.25);
    const look = drift.add(this.target);
    this.camera.position.copy(look).addScaledVector(dir, 90);
    this.camera.up.set(0, 1, 0);
    this.camera.lookAt(look);
    const aspect = this.W / this.H;
    const hh = this.halfH;
    this.camera.left = -hh * aspect;
    this.camera.right = hh * aspect;
    this.camera.top = hh;
    this.camera.bottom = -hh;
    this.camera.near = 1;
    this.camera.far = 240;
    this.camera.zoom = this.zoom;
    this.camera.updateProjectionMatrix();
    this.camera.updateMatrixWorld();

    this.sun.target.position.set(this.target.x * 0.3, 0, this.target.z * 0.3);
  }

  // ── input ────────────────────────────────────────────────────────────────

  private attachInput(): void {
    this.detach?.();
    const el = this.out;
    if (this.walking) {
      this.detach = this.walker.attach(el);
      return;
    }
    const pxPerUnit = () => (el.clientHeight || 1) / ((2 * this.halfH) / this.zoom);
    const down = (e: PointerEvent) => {
      if (e.button !== 0) return;
      this.drag = { x: e.clientX, y: e.clientY, moved: false, tx: this.targetGoal.x, tz: this.targetGoal.z };
      el.setPointerCapture(e.pointerId);
    };
    const move = (e: PointerEvent) => {
      const d = this.drag;
      if (!d) return;
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      if (!d.moved && Math.hypot(dx, dy) < 5) return;
      if (!d.moved) {
        d.moved = true;
        if (this.tour) this.setTour(false);
      }
      const k = 1 / pxPerUnit();
      const dir = this.viewDir(ISO_AZ, this._v);
      const right = new THREE.Vector3(dir.z, 0, -dir.x).normalize();
      const fwd = new THREE.Vector3(-dir.x, 0, -dir.z).normalize();
      const fy = 1 / Math.sin(ISO_EL);
      this.targetGoal.x = d.tx - right.x * dx * k + fwd.x * dy * k * fy;
      this.targetGoal.z = d.tz - right.z * dx * k + fwd.z * dy * k * fy;
      this.targetGoal.x = Math.max(-20, Math.min(20, this.targetGoal.x));
      this.targetGoal.z = Math.max(-18, Math.min(15, this.targetGoal.z));
    };
    const up = (e: PointerEvent) => {
      const d = this.drag;
      this.drag = null;
      try { el.releasePointerCapture(e.pointerId); } catch { /* already released */ }
      if (!d || d.moved) return;
      const room = this.pickRoom(e.clientX, e.clientY);
      if (this.tour) this.setTour(false);
      this.focus(room && room !== this.focused ? room : null);
    };
    const wheel = (e: WheelEvent) => {
      e.preventDefault();
      this.zoomGoal = Math.max(0.75, Math.min(3.8, this.zoomGoal * Math.exp(-e.deltaY * 0.0015)));
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

  private pickRoom(clientX: number, clientY: number): RoomId | null {
    const r = this.out.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1);
    this._ray.setFromCamera(ndc, this.camera);
    const hit = this._ray.ray.intersectPlane(this._plane, this._v);
    return hit ? roomAt(hit.x, hit.z) : null;
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
        console.error('[agent-hub] frame failed', err);
      }
    }
  }

  private update(dt: number): void {
    this.time += dt;
    this.director.update(dt);
    this.play.update(dt);
    for (const w of this.crew) w.update(dt, this.nav);
    this.floor.update(dt, this.time);
    this.boss.update(dt, this.floor.blueyHop);
    this.updateLou(dt);
    this.smokers();
    this.smoke.update(dt);
    this.flashLight.intensity *= Math.exp(-dt * 38);
    this.floor.setTickets(this.save.tickets);
    // up on a stage: the door has words about it
    if (this.inTheClub && this.walker.hop >= STAGE.h - 0.05 && this.time >= this.offStageAt) {
      this.offStageAt = this.time + 6 + Math.random() * 3;
      const door = this.club.rats.find((r) => r.role === 'bouncer');
      if (door) this.say(door, pick(['HEY! Off the stage, pal!', 'The stage is for the talent!', 'Down. Now. I’m askin’ nice.', 'You’re not on the bill, buddy.']), 'loss', { force: true, big: true });
    }
    if (this.walking && this.club.inside.visible && this.club.update(dt, this.time)) {
      // a spin finished: the room applauds, and somebody says so
      const mid = { x: STAGE.x, z: STAGE.z };
      this.audio.applause(2, mid);
      const fans = this.club.rats.filter((r) => r.seated);
      const fan = fans[Math.floor(Math.random() * fans.length)];
      if (fan && Math.random() < 0.6) this.say(fan, pick(this.club.barkLines(fan)), 'neutral', { life: 2.4 });
    }
    this.ps2.uniforms.uTime.value = this.time;
    this.flights.update(dt);
    this.sparks.update(dt);
    this.pile.update(dt);
    for (const b of this.bubbles) b.age += dt;
    this.bubbles = this.bubbles.filter((b) => b.age < b.life);
    for (const f of this.feed) f.age += dt;
    for (const [k, v] of this.hot) this.hot.set(k, Math.max(0, v - dt * 0.6));
    this.pnlPulse = Math.max(0, this.pnlPulse - dt * 2.5);
    if (this.banner) {
      this.banner.t += dt;
      if (this.banner.t > 3.4) this.banner = null;
    }
    this.updateCamera(dt);
    if (this.walking) this.audio.setListener(this.walker.x, this.walker.z, this.walker.yaw);
    this.audio.update(dt, musicMix({
      walking: this.walking,
      inClub: this.walking && inClub(this.walker.x, this.walker.z),
      doorDist: this.walking ? this.clubDoorDist() : 99,
      doorOpen: this.clubOpen,
      black: this.walking ? this.hb.blackAlpha : 0,
    }));
    if (this.nowPlaying) {
      this.nowPlaying.t += dt;
      if (this.nowPlaying.t > 8) this.nowPlaying = null;
    }
  }

  /** You're on foot in the club (and not in the hall). */
  private get inTheClub(): boolean {
    return this.walking && inClub(this.walker.x, this.walker.z);
  }

  /** Whether you'd see or hear this speaker from where you stand: the
   *  hall's people and the club's are on opposite sides of a wall, unless
   *  the club's doors are open. */
  private sameRoom(who: Speaker): boolean {
    const theirs = who instanceof ClubRat && inClub(who.x, who.z);
    if (!this.walking) return !theirs;
    return theirs === this.inTheClub || this.clubOpen > 0.3;
  }

  /** Sat at a table: the camera eases to the rig's framing and leans a
   *  little toward the cursor; the rig animates; whatever's under the cursor
   *  is found again (the table moves under it). */
  private seatCamera(dt: number): void {
    const rig = this.rig!;
    const v = rig.view();
    const k = 1 - Math.exp(-5 * dt);
    this.seatEye.lerp(v.eye, k);
    this.seatLook.lerp(v.look, k);
    this.seatFov += (v.fov - this.seatFov) * k;
    const c = this.cursor;
    const kc = 1 - Math.exp(-3 * dt);
    c.sx += ((c.in ? c.x : 0) - c.sx) * kc;
    c.sy += ((c.in ? c.y : 0) - c.sy) * kc;
    const cam = this.walkCam;
    cam.position.copy(this.seatEye);
    const fwd = this._v.copy(this.seatLook).sub(this.seatEye);
    const dist = fwd.length();
    fwd.normalize();
    const right = this._v2.crossVectors(fwd, cam.up).normalize();
    const up = new THREE.Vector3().crossVectors(right, fwd);
    const look = this.seatLook.clone().addScaledVector(right, c.sx * dist * 0.1).addScaledVector(up, c.sy * dist * 0.06);
    cam.lookAt(look);
    cam.fov = this.seatFov;
    cam.updateMatrixWorld();
    rig.update(dt, this.time);
    if (c.in) this.under = this.pickAt(c.x, c.y);
  }

  private wantCrisp(): boolean {
    if (!this.walking) return false;
    const g = this.game;
    if (g) return g instanceof SportsbookSession || isPainted(g);
    return roomAt(this.walker.x, this.walker.z) === 'research';
  }

  /** Lou works the well: drifting round it facing the room, and to whoever
   *  is ordering (or talking to him), facing them. */
  private updateLou(dt: number): void {
    const lou = this.hallBartender;
    const [bx, bz] = PROP.bar;
    const ordering = this.game instanceof BarSession && this.playSpot?.id.startsWith('hb') ? this.playSpot : null;
    const talking = this.talkWith === lou;
    let want = this.louAngle + dt * 0.12;
    if (ordering) want = Math.atan2(ordering.z - bz, ordering.x - bx);
    else if (talking) want = Math.atan2(this.walker.z - bz, this.walker.x - bx);
    let d = want - this.louAngle;
    d = Math.atan2(Math.sin(d), Math.cos(d));
    this.louAngle += d * Math.min(1, dt * (ordering || talking ? 4 : 1));
    lou.group.position.set(bx + Math.cos(this.louAngle) * HALL_BAR_WELL, 0, bz + Math.sin(this.louAngle) * HALL_BAR_WELL);
    // facing out, over the counter
    if (!talking) lou.hold({ x: bx + Math.cos(this.louAngle) * 3, z: bz + Math.sin(this.louAngle) * 3 });
    lou.update(dt, this.time, null);
  }

  /** How far you stand from the club's doors. */
  private clubDoorDist(): number {
    return Math.hypot(this.walker.x - CLUB_DOOR[0], this.walker.z - CLUB.z0);
  }

  /** Every lit cigarette smokes: a thin stream off the ember, and now and
   *  then an exhale off the snout. The embers glow brighter as they draw. */
  private smokers(): void {
    const t = this.time;
    this.crewKit.embers.forEach((m, i) => {
      const draw = Math.max(0, Math.sin(t * 0.7 + i * 2.1)) ** 8;
      m.emissiveIntensity = 1.1 + draw * 3.2 + Math.sin(t * 9 + i) * 0.15;
    });
    const emit = (ember: THREE.Object3D, x: number, y: number, z: number, yaw: number) => {
      const next = this.smokeAt.get(ember) ?? Math.random();
      if (next > t) return;
      ember.getWorldPosition(this._v);
      this.smoke.emit(this._v.x, this._v.y + 0.02, this._v.z);
      // an exhale: two big puffs off the snout, every few seconds
      if (Math.random() < 0.06) {
        for (let k = 0; k < 2; k++) this.smoke.emit(x + Math.sin(yaw) * 0.45, y - 0.1, z + Math.cos(yaw) * 0.45, true);
      }
      this.smokeAt.set(ember, t + 0.22 + Math.random() * 0.12);
    };
    for (const w of this.crew) {
      if (w.ember) emit(w.ember, w.group.position.x, 1.08, w.group.position.z, w.group.rotation.y);
    }
    const b = this.boss.group;
    if (this.boss.ember) emit(this.boss.ember, b.position.x, b.position.y + 1.08, b.position.z, b.rotation.y);
    if (this.walking && this.club.inside.visible) for (const c of this.club.embers()) emit(c.ember, c.x, c.y, c.z, c.yaw);
  }

  // ── in your paw ──────────────────────────────────────────────────────────

  /** The hotbar's frame: its events, the paw, the smoke off your cigarette,
   *  the buzz in your eyes and legs, and whoever you're pointing it at. */
  private hands(dt: number): void {
    const hb = this.hb;
    const busy = !!this.game || !!this.talk;
    if (busy && hb.using) this.hbEvents(hb.release());
    this.hbEvents(hb.update(dt));
    const w = this.walker;
    w.drunk = hb.buzz;
    this.ps2.uniforms.uDrunk.value = hb.buzz;
    const out = hb.black > 0;
    if (out !== this.blackedOut) {
      this.blackedOut = out;
      if (!busy) w.frozen = out;
    }
    // the sounds of what's in your paw
    const drinking = (hb.shown === 'beer' || hb.shown === 'vodka' || hb.shown === 'cocktail') && hb.using && hb.raise > 0.85 && hb.level[hb.shown] > 0;
    if (drinking && this.time >= this.gulpAt) {
      this.audio.gulp();
      this.gulpAt = this.time + 0.42;
    }
    if (!busy && hb.shown === 'cig' && hb.lit && hb.using && hb.raise > 0.3) this.audio.startDrag();
    else this.audio.stopDrag();
    if (hb.reload > 0 && !this.wasReloading) this.audio.reload();
    this.wasReloading = hb.reload > 0;
    this.vm.update(dt, this.time, hb, {
      move: w.moveFrac, bob: w.bobPhase, yaw: w.yaw, pitch: w.pitch, visible: !busy,
    });
    if (busy) return;
    const cam = this.walkCam;

    // a wisp off the lit end; a drag pulls it in instead
    if (hb.shown === 'cig' && hb.lit && hb.lower < 0.5 && this.time >= this.wispAt) {
      this.wispAt = this.time + (hb.using ? 0.5 : 0.16);
      const p = this.vm.emberInHall(cam, this._v);
      this.smoke.emit(p.x, p.y + 0.004, p.z, false, { size: 0.006, grow: 0.035, life: 1.5, alpha: 0.35, v: [0, 0.16, 0] });
    }
    // an exhale: a cloud blown out ahead of you
    if (hb.exhale > 0) {
      const n = Math.random() < dt * (14 + 26 * hb.exhaleK) ? 1 : 0;
      for (let i = 0; i < n + (Math.random() < dt * 20 ? 1 : 0); i++) {
        const m = this.vm.mouthInHall(cam, this._v);
        const f = this._v2.set(0, 0, -1).applyQuaternion(cam.quaternion);
        const sp = 0.5 + Math.random() * 0.5;
        this.smoke.emit(m.x, m.y, m.z, true, {
          size: 0.03 + Math.random() * 0.025, grow: 0.22, life: 1.9 + Math.random() * 0.8, alpha: 0.22,
          v: [f.x * sp + (Math.random() - 0.5) * 0.15, 0.06 + Math.random() * 0.08, f.z * sp + (Math.random() - 0.5) * 0.15],
        });
      }
    }

    // pointing it at someone: they put their paws up
    if (hb.shown === 'gun' && hb.ready && hb.reload <= 0 && this.time >= this.aimCheckAt) {
      this.aimCheckAt = this.time + 0.12;
      const d = this._v2.set(0, 0, -1).applyQuaternion(cam.quaternion);
      let best: Worker | null = null, bt = Infinity;
      for (const r of this.crew) {
        const hit = rayPastRat(cam.position, d, r.x, r.z, 7);
        if (!hit || hit.off > 0.45 || hit.t >= bt) continue;
        if (wallBetween(w.x, w.z, r.x, r.z, FLOOR_WALK.walls)) continue;
        best = r;
        bt = hit.t;
      }
      if (best) {
        best.spook({ x: w.x, z: w.z }, 0.9);
        if ((this.aimLineAt.get(best) ?? 0) <= this.time) {
          this.aimLineAt.set(best, this.time + 7 + Math.random() * 4);
          this.say(best, pick(AIM_LINES), 'neutral', { force: true, life: 2.6 });
        }
      }
    }
  }

  private hbEvents(ev: HotbarEvent[]): void {
    for (const e of ev) {
      switch (e.k) {
        case 'shot': this.shoot(); break;
        case 'dry': this.audio.dryFire(); break;
        case 'exhale': this.audio.exhale(e.strength); break;
        case 'empty': this.audio.clink(); break;
        case 'burn':
          this.walker.shake = 1;
          this.audio.cough();
          break;
        case 'flick': {
          // the butt goes, a few sparks off the end
          const p = this.vm.emberInHall(this.walkCam, this._v);
          this.sparks.burst(p.x, p.y, p.z, '#ff7a2a', 4, 0.6, 0.6);
          break;
        }
        case 'lit': {
          this.audio.lighter();
          const p = this.vm.emberInHall(this.walkCam, this._v);
          this.sparks.burst(p.x, p.y, p.z, '#ffd27a', 3, 0.25, 0.4);
          break;
        }
        case 'wake':
          this.walker.reset();
          this.banner = { text: 'You blacked out', sub: 'Came to by the door. The chips are all there.', t: 0 };
          break;
        default: break;
      }
    }
  }

  /** Bang. A hole where it lands, a flash on the walls, nobody hurt: the
   *  rat it nearly found jumps, the room puts its paws up, Bluey shouts. */
  private shoot(): void {
    const cam = this.walkCam;
    const w = this.walker;
    this.vm.fired();
    this.audio.gunshot();
    w.kick += 0.045;
    const d = new THREE.Vector3(0, 0, -1).applyQuaternion(cam.quaternion);
    // a drink in you and it goes where it likes
    const spread = 0.004 + this.hb.buzz * 0.06;
    d.x += (Math.random() - 0.5) * spread * 2;
    d.y += (Math.random() - 0.5) * spread * 2;
    d.z += (Math.random() - 0.5) * spread * 2;
    d.normalize();
    const m = this.vm.muzzleInHall(cam, this._v);
    this.flashLight.position.copy(m);
    this.flashLight.intensity = 16;
    for (let i = 0; i < 3; i++) {
      this.smoke.emit(m.x, m.y, m.z, false, {
        size: 0.025, grow: 0.16, life: 0.9 + Math.random() * 0.4, alpha: 0.45,
        v: [d.x * 0.5 + (Math.random() - 0.5) * 0.1, 0.12, d.z * 0.5 + (Math.random() - 0.5) * 0.1],
      });
    }

    const hit = this.shotHit(cam, d);
    const maxT = hit ? hit.distance : 40;
    let graze: Worker | null = null, gt = Infinity;
    for (const r of this.crew) {
      const p = rayPastRat(cam.position, d, r.x, r.z, maxT);
      if (p && p.off < 0.38 && p.t < gt) { graze = r; gt = p.t; }
    }
    const bp = this.boss.group.position;
    const bossGraze = rayPastRat(cam.position, d, bp.x, bp.z, Math.min(maxT, gt), bp.y + 1.75);
    const bossHit = !!bossGraze && bossGraze.off < 0.38;
    if (bossHit) graze = null;
    if (hit) {
      this.holes.add(hit.point, hit.normal);
      const p = hit.point;
      this.sparks.burst(p.x, p.y, p.z, '#FFD27A', 9, 1.2, 1.1);
      for (let i = 0; i < 2; i++) {
        this.smoke.emit(p.x + hit.normal.x * 0.05, p.y + hit.normal.y * 0.05, p.z + hit.normal.z * 0.05, false, {
          size: 0.04, grow: 0.12, life: 0.8, alpha: 0.35,
          v: [hit.normal.x * 0.3, 0.05, hit.normal.z * 0.3],
        });
      }
    }

    // the club: the record stops, the dancers freeze, Knuckles has words
    const me = { x: w.x, z: w.z };
    if (inClub(w.x, w.z) || (this.club.inside.visible && this.clubDoorDist() < 3)) {
      this.club.spook(3.2);
      this.audio.recordScratch();
      const door = this.club.rats.find((r) => r.role === 'bouncer');
      if (door) this.say(door, pick(['HEY! Not in the club!', 'Put. It. Away.', 'You just stopped the music, pal.']), 'loss', { force: true, big: true });
    }
    // the room
    let yeller: Worker | null = null;
    for (const r of this.crew) {
      const dist = Math.hypot(r.x - me.x, r.z - me.z) + (inClub(me.x, me.z) ? 6 : 0);
      if (dist > 11 && r !== graze) continue;
      r.spook(me, 2.2 + Math.random() * 1.4 + (r === graze ? 1.6 : 0), r === graze || dist < 4.5);
      if (r !== graze && (!yeller || Math.random() < 0.35)) yeller = r;
    }
    if (graze) this.say(graze, pick(GRAZE_LINES), 'loss', { force: true, big: true });
    if (yeller && Math.random() < 0.8) this.say(yeller, pick(SHOT_LINES), 'neutral', { force: true });
    if (bossHit || (!this.inTheClub && Math.hypot(bp.x - me.x, bp.z - me.z) < 15)) {
      this.boss.spook(bossHit ? 4 : 2.5);
      this.floor.blueyHop = Math.max(this.floor.blueyHop, bossHit ? 1 : 0.5);
      if (bossHit || this.time >= this.bossLineAt) {
        this.bossLineAt = this.time + 6;
        this.say('captain', bossHit ? 'YOU SHOT AT ME?! IN MY OWN JOINT?!' : pick(BOSS_LINES), 'gold', { force: true, big: true });
      }
    }
  }

  /** The first solid thing along the shot: no glow, no glass you can't see,
   *  nothing hidden, and never a rat (they get grazed, never hit). */
  private shotHit(cam: THREE.Camera, d: THREE.Vector3): { point: THREE.Vector3; normal: THREE.Vector3; distance: number } | null {
    const rats = new Set<THREE.Object3D>(this.crew.map((r) => r.group));
    rats.add(this.boss.group);
    const ray = this._ray;
    ray.set(cam.position, d);
    ray.camera = cam;
    ray.far = 40;
    const hits = ray.intersectObject(this.floor.group, true);
    ray.far = Infinity;
    const im = new THREE.Matrix4();
    for (const h of hits) {
      const o = h.object as THREE.Mesh;
      if (!o.isMesh) continue;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      const mt = mats[0];
      if (!mt || mt.depthWrite === false || mt.blending === THREE.AdditiveBlending) continue;
      if (mt.transparent && mt.opacity < 0.25) continue;
      let ok = true;
      for (let p: THREE.Object3D | null = o; p; p = p.parent) {
        if (!p.visible || rats.has(p)) { ok = false; break; }
      }
      if (!ok) continue;
      const n = h.face ? h.face.normal.clone() : d.clone().negate();
      const inst = o as unknown as THREE.InstancedMesh;
      if (inst.isInstancedMesh && h.instanceId !== undefined) {
        inst.getMatrixAt(h.instanceId, im);
        n.transformDirection(im);
      }
      n.transformDirection(o.matrixWorld);
      if (n.dot(d) > 0) n.negate();
      return { point: h.point.clone(), normal: n, distance: h.distance };
    }
    return null;
  }

  private project(p: THREE.Vector3): { x: number; y: number } {
    this._v2.copy(p).project(this.walking ? this.walkCam : this.camera);
    return { x: (this._v2.x * 0.5 + 0.5) * this.W, y: (-this._v2.y * 0.5 + 0.5) * this.H };
  }

  private seen(p: THREE.Vector3, maxDist: number): boolean {
    return !this.walking || visibleFrom(this.walkCam, p, maxDist);
  }

  private render(): void {
    this.composer.render();
    const g = this.ctx;
    // on foot, hard pixels, no filtering: the low-res frame stays low-res
    g.imageSmoothingEnabled = !this.walking || this.crisp;
    g.drawImage(this.glCanvas, 0, 0, this.W, this.H);
    g.imageSmoothingEnabled = true;
    // a blackout takes the hall away, not the HUD: the banner reads over it
    const black = this.walking ? this.hb.blackAlpha : 0;
    if (black > 0) {
      g.fillStyle = `rgba(0,0,0,${black})`;
      g.fillRect(0, 0, this.W, this.H);
    }
    const u = this.uiScale();
    const seated = !!this.game;

    const d = this.director;
    const a = d.account;
    const n = hudNumbers(a);
    const counters: Record<RoomId, string> = {
      research: this.board.rows.length ? `${this.board.rows.length} live markets` : `${d.signalsSeen.toLocaleString('en-US')} orders`,
      forge: d.forgeSign,
      backtest: `practice table · ${d.backtestSign}`,
      desk: n.open !== null ? `${n.open.toLocaleString('en-US')} open` : `${d.ordersSeen.toLocaleString('en-US')} orders`,
      vault: typeof a?.todayWins === 'number' ? `${a.todayWins.toLocaleString('en-US')} wins today` : `${d.winsSeen.toLocaleString('en-US')} wins`,
      optimizer: d.optimizerSign,
      bridge: d.bridgeSign,
    };
    const club = this.inTheClub;
    if (!seated && !club) {
      const signs: SignView[] = [];
      for (const r of ROOMS) {
        const at = this.walking ? this._v.set(r.cx, CEIL_H - 1.1, r.cz) : this._v.set(...r.sign);
        if (!this.seen(at, 14)) continue;
        const p = this.project(at);
        if (this.walking && p.y < this.H * 0.16) continue;
        const focusedOther = this.focused && this.focused !== r.id;
        signs.push({
          x: p.x, y: p.y, icon: r.icon, name: r.name, counter: counters[r.id], accent: r.accent,
          alpha: focusedOther ? 0.35 : 1, hot: Math.min(1, this.hot.get(r.id) ?? 0), focused: this.focused === r.id,
        });
      }
      drawSigns(g, signs, u * (this.portrait ? 0.85 : this.zoom > 1.6 ? 1.1 : 1), this.W);
    }

    if (this.agents.size && !club) {
      const tags: TagView[] = [];
      for (const [id, { w }] of this.agents) {
        if (!w.tag) continue;
        const head = w.headWorld(this._v).add(this._v2.set(0, 0.32, 0));
        if (!this.seen(head, 11)) continue;
        const p = this.project(head);
        const talking = this.bubbles.some((b) => b.who === w);
        tags.push({ x: p.x, y: p.y, label: this.play.tagFor(id, w.tag.label), color: w.tag.color, dim: talking });
      }
      tags.sort((a2, b) => b.y - a2.y);
      for (let i = 1; i < tags.length; i++) {
        for (let j = 0; j < i; j++) {
          if (Math.abs(tags[i].x - tags[j].x) < 130 * u && Math.abs(tags[i].y - tags[j].y) < 20 * u) {
            tags[i].y = tags[j].y - 21 * u;
          }
        }
      }
      drawTags(g, tags, u * (this.zoom > 1.6 ? 1.1 : 1));
    }

    const views: BubbleView[] = [];
    for (const b of this.bubbles) {
      if (!this.sameRoom(b.who)) continue;
      const anchor = b.who === 'captain'
        ? this._v.set(PROP.podium[0], 2.05 + this.floor.blueyHop * 0.1, PROP.podium[1])
        : b.who.headWorld(this._v);
      if (!this.seen(anchor, 14)) continue;
      const p = this.project(anchor);
      if (seated && p.y > this.H * 0.45) continue;
      views.push({ x: p.x, y: p.y, text: b.text, tone: b.tone, age: b.age, life: b.life, big: b.big });
    }
    views.sort((p, q) => p.y - q.y);
    for (let i = 1; i < views.length; i++) {
      for (let j = 0; j < i; j++) {
        if (Math.abs(views[i].x - views[j].x) < 150 * u && Math.abs(views[i].y - views[j].y) < 34 * u) {
          views[i].y = views[j].y + 36 * u;
        }
      }
    }
    drawBubbles(g, views, u * (this.zoom > 1.6 ? 1.12 : 1), this.W);

    drawHud(g, {
      W: this.W, H: this.H, u, portrait: this.portrait,
      pnl: n.pnl,
      roi: n.roi,
      winRate: n.winRate,
      settledToday: n.settledToday,
      open: n.open,
      wins: d.winsSeen, losses: d.lossesSeen,
      pnlPulse: this.pnlPulse,
      // the feed sits where the dialogue and the tables' panels do
      feed: this.talk || this.game ? [] : this.feed,
      banner: this.banner,
      poly: null,
      showBrand: true,
      brandSub: 'AGENT HUB · CASINO',
      brandColors: ['#FDE68A', '#F2C66D', '#F472B6'],
    });

    if (this.nowPlaying && !this.game) drawNowPlaying(g, { W: this.W, u, text: this.nowPlaying.text, t: this.nowPlaying.t });

    if (this.walking) {
      drawChipHud(g, { W: this.W, u, chips: this.save.chips, open: this.save.tickets.filter((t) => t.status === 'open').length });
      if (this.game) {
        if (this.rig) {
          drawWorldLabels(g, this.rig.labels().flatMap((l) => {
            if (!visibleFrom(this.walkCam, l.at, 12)) return [];
            const p = this.project(l.at);
            return [{ ...l, x: p.x, y: p.y }];
          }), u);
        }
        if (this.under?.tip && this.cursor.in) {
          drawTooltip(g, { x: ((this.cursor.x + 1) / 2) * this.W, y: ((1 - this.cursor.y) / 2) * this.H, text: this.under.tip, u, W: this.W });
        }
        drawHintBar(g, { W: this.W, H: this.H, u, text: this.game.hint() });
      } else if (this.talk) {
        this.talkRects = drawDialogue(g, this.talk, { W: this.W, H: this.H, u, color: this.talkColor, t: this.time });
      } else {
        const p = this.playTarget, tk = this.talkTarget;
        if (p && (!tk || p.d <= tk.d + 0.4)) {
          drawPlayPrompt(g, {
            W: this.W, H: this.H, u, color: GAME_COLOR[p.spot.kind],
            name: p.spot.kind === 'cashier' ? 'Visit the cashier' : p.spot.kind === 'sportsbook' ? 'Bet at the sportsbook' : p.spot.kind === 'bar' ? 'Order a cocktail' : `Play ${p.spot.name}`,
            sub: GAME_SUB[p.spot.kind],
          });
        } else if (tk) {
          drawTalkPrompt(g, { W: this.W, H: this.H, u, name: tk.persona.name, title: tk.persona.title, color: tk.color });
        }
        if (this.hb.shown === 'gun' && this.hb.lower < 0.5) {
          drawCrosshair(g, { W: this.W, H: this.H, u, spread: this.hb.recoil + this.hb.buzz * 0.8, reloading: this.hb.reload > 0 });
        }
        drawHotbar(g, { W: this.W, H: this.H, u, hb: this.hb, locked: this.walker.locked });
      }
    }
  }
}

/** A conversation is framed in the colour of the speaker's area. */
const ROLE_COLOR: Record<string, string> = {
  researcher: '#C084FC', smith: '#F59E0B', quant: '#2DD4BF', trader: '#F472B6',
  keeper: '#4ADE80', risk: '#EF4444', engineer: '#38BDF8', agent: '#94A3B8',
};
