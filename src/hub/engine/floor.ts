import * as THREE from 'three';
import {
  AGENT_DOCKS, BAR_R, BAR_STOOLS, BJ_R, BJ_TABLES, CAGE_Z, FLOOR, LOUNGE_SOFAS, NAV_NODES, PALMS, PART_H, POLY_POS, PROP,
  CLUB_DOOR, RIM_H, ROOMS, SALON_DOOR, SALON_FRONT_Z, SALON_GLASS, SALON_X1, SB_SEATS, SLOTS, WALL_H, bjSeat, slotSpot, type RoomId,
} from './layout';
import type { ItemKit } from './items';
import { mergeStatic } from './bake';
import {
  CARD_H, CARD_W, Screen, blackjackFeltTexture, cardAtlasTexture, cardUV, carpetTexture, marbleTexture,
  neonText, reelStripTexture, rouletteLayoutTexture, runnerTexture, tickerTexture, wheelTexture,
} from './casinoTextures';
import type { Card } from '../casino/blackjack';
import { WHEEL_ORDER, colorOf, pocketIndex } from '../casino/roulette';
import { STRIP } from '../casino/slots';
import type { BoardRow, Ticket } from '../casino/sportsbook';
import { paintBets, paintMarkets } from '../casino/sportsbookScreens';

// The casino hall, as geometry plus the moving parts the director and the
// games poke: the sportsbook's live board and odds cube, the blackjack
// tables' cards, the roulette wheel and ball, every slot machine's reels,
// the practice table's replay, the bet chute and payout window, the vault
// and the security beacon, the POLYMARKET teller window across the night.
// Built from primitives on purpose — chunky toy shapes read at 30px and cost
// nothing to load — then merged by material (bake.ts), so the hall is a few
// dozen draw calls. No point lights: pools of light are additive floor decals
// in ONE instanced mesh, and everything that glows is emissive for the bloom.

type V2 = readonly [number, number];

const BRASS = '#C79A45';
const WOOD = '#3a2216';
const WOOD_D = '#24140d';
const LACQ = '#16111a';
const FELT = '#0f5b3d';
const LEATHER = '#1c1013';
const IVORY = '#efe6d2';
const VELVET = '#5b1030';

class Mats {
  private m = new Map<string, THREE.MeshStandardMaterial>();
  get(color: string, o: { e?: string; ei?: number; metal?: number; rough?: number } = {}): THREE.MeshStandardMaterial {
    const k = `${color}|${o.e ?? ''}|${o.ei ?? 0}|${o.metal ?? 0.1}|${o.rough ?? 0.75}`;
    let mat = this.m.get(k);
    if (!mat) {
      mat = new THREE.MeshStandardMaterial({
        color, emissive: o.e ?? '#000000', emissiveIntensity: o.ei ?? 0,
        metalness: o.metal ?? 0.1, roughness: o.rough ?? 0.75,
        // faceted, PS2-era: no smoothed normals anywhere in the hall
        flatShading: true,
      });
      this.m.set(k, mat);
    }
    return mat;
  }
  brass(): THREE.MeshStandardMaterial { return this.get(BRASS, { metal: 0.85, rough: 0.3 }); }
  /** A material the floor animates itself (flashes, pulses) — never shared. */
  readonly owned = new Set<THREE.Material>();
  own(color: string, e: string, ei: number, metal = 0.1, rough = 0.6): THREE.MeshStandardMaterial {
    const m = new THREE.MeshStandardMaterial({ color, emissive: e, emissiveIntensity: ei, metalness: metal, roughness: rough, flatShading: true });
    this.owned.add(m);
    return m;
  }
}

function at<T extends THREE.Object3D>(o: T, x: number, y: number, z: number): T {
  o.position.set(x, y, z);
  return o;
}

export interface Blip { row: number; color: string; age: number }

/** One slot machine's reels, in the instanced drum mesh. */
interface Reels {
  base: THREE.Matrix4;
  /** current angle per reel, and the spin in progress */
  angle: [number, number, number];
  spin: { from: [number, number, number]; to: [number, number, number]; t: number; ends: [number, number, number] } | null;
  /** the replay machine just turns while a backtest replays */
  free: boolean;
  flash: number;
}

/** Where hand k of n lies across a blackjack table (x, from its middle). */
export const BJ_HAND_X = (n: number, k: number): number => (n === 1 ? 0 : k === 0 ? -0.24 : 0.24);
/** Markets per monitor on the sportsbook's wall. */
const SB_PER_MONITOR = 5;
const REEL_X = [-0.17, 0, 0.17];
const REEL_Y = 1.07;
/** A reel is the front of a drum, seen through the window: an arc of a
 *  cylinder this big, showing this many symbols at once, its strip scrolling
 *  past (a whole drum carrying all 22 symbols at a size that fits the
 *  cabinet squashed each one five to one). */
const REEL_R = 0.19;
const REEL_ARC = 1.8;
const REEL_SHOWS = 3.2;
const REEL_Z = 0.3 - REEL_R;
const STOP_ANGLE = (k: number) => ((k + 0.5) / STRIP.length) * Math.PI * 2;
const TOPPER_COLORS = ['#F472B6', '#38BDF8', '#FBBF24', '#4ADE80', '#A78BFA', '#FB923C'];

export class CasinoFloor {
  readonly group = new THREE.Group();
  /** Everything beyond the walls: the night, the teller window. */
  readonly night = new THREE.Group();
  /** The entrance doors as the overview shows them; first person has its
   *  own sliding pair (walkshell.ts), so these hide while someone walks. */
  readonly overviewOnly = new THREE.Group();
  readonly mats = new Mats();
  private textures: THREE.Texture[] = [];
  readonly roomFloors: THREE.Mesh[] = [];

  // sportsbook
  private kioskHead = new THREE.Group();
  private kioskTip!: THREE.MeshStandardMaterial;
  private oddsPingT = 0;
  /** The sportsbook's wall of monitors: your bets, then the board in fixed
   *  slices (nothing pages past before you can read it). */
  private monitors: Screen[] = [];
  private tickets: readonly Ticket[] = [];
  private boardRows: BoardRow[] = [];
  private boardAt: number | null = null;
  private boardNote: string | null = 'Loading Polymarket US’s sports markets…';
  readonly blips: Blip[] = [];
  private printerLight!: THREE.MeshStandardMaterial;
  private printerFlash = 0;

  // blackjack pit
  private shuffleLight!: THREE.MeshStandardMaterial;
  private dealCard!: THREE.MeshStandardMaterial;
  private dealFlash = 0;
  /** Where a dealt card lands on the first table's felt: the sparks fly here. */
  readonly dealSpot = new THREE.Vector3(BJ_TABLES[0][0], 0.86, BJ_TABLES[0][1] + 0.35);
  private cardMat!: THREE.MeshStandardMaterial;
  private cardPool: THREE.Mesh[] = [];
  private cardsOut: THREE.Mesh[][] = BJ_TABLES.map(() => []);

  // practice table
  private display!: Screen;
  private displayMesh!: THREE.Mesh;
  private series: number[] = [];
  private progress = 1;
  private verdict: boolean | null = null;
  private replaying = false;
  private displayLabel = 'BACKTEST';
  private displaySub = 'idle';
  private candle!: THREE.MeshStandardMaterial;
  private discardFlash = 0;
  private discardRing!: THREE.MeshStandardMaterial;

  // slot machines (the last one is the practice table's replay machine)
  private reelMesh!: THREE.InstancedMesh;
  private reels: Reels[] = [];
  private toppers!: THREE.InstancedMesh;
  /** The slot machines' levers: one instanced draw for the arms, one for the
   *  knobs, so the one you pull can move. */
  private leverArms!: THREE.InstancedMesh;
  private leverKnobs!: THREE.InstancedMesh;
  private leverPull: number[] = [];
  /** The roulette layout's felt (immersive play reads where you click on it). */
  rouletteLayout!: THREE.Mesh;
  private _c = new THREE.Color();

  // high-limit salon
  private chuteRing!: THREE.MeshStandardMaterial;
  private chuteFlash = 0;
  private payoutLight!: THREE.MeshStandardMaterial;
  private payoutCoin = new THREE.Group();
  private payoutFlash = 0;
  private limitSigns: THREE.MeshStandardMaterial[] = [];
  private bets!: Screen;
  readonly betLines: { text: string; color: string }[] = [];

  // cashier cage
  private vaultWheel = new THREE.Group();
  private vaultSpin = 0;
  private beaconHead = new THREE.Group();
  private beaconMat!: THREE.MeshStandardMaterial;
  private beaconBeams: THREE.Mesh[] = [];
  private alarmPool!: THREE.MeshBasicMaterial;
  private cagePool!: THREE.MeshBasicMaterial;
  alarm = 0;
  private cageFlash = 0;

  // roulette
  private wheelHead = new THREE.Group();
  private ballGroup = new THREE.Group();
  private ball!: THREE.Mesh;
  private wheelSpeed = 0.35;
  private wheelTarget = 0.35;
  private wheelAngle = 0;
  private ballAngle = 0;
  /** A spin you (or an agent) bet on: the ball lands in `pocket`. */
  private play: { t: number; dur: number; n: number; from: number; to: number; wheelV0: number } | null = null;
  /** The ball rides its pocket until the next spin. */
  private ballIn: number | null = null;
  private lastNumbers: number[] = [];
  private marquee!: Screen;
  private marqueeFlash = 0;

  // the bar
  private barGlow!: THREE.MeshStandardMaterial;
  private barSign = new THREE.Group();

  // vip lounge
  /** The pit boss's hop on a big moment (seconds left); the engine's rat
   *  boss on the podium reads it. */
  blueyHop = 0;

  // POLYMARKET
  readonly teller = new THREE.Group();
  private tellerFlash = 0;
  private tellerGlow!: THREE.MeshStandardMaterial;
  private bulbsA!: THREE.MeshStandardMaterial;
  private bulbsB!: THREE.MeshStandardMaterial;
  private bokeh!: THREE.Points;
  private bokehMat!: THREE.ShaderMaterial;

  // drop-off spots the director fills
  readonly inboxSlots: THREE.Vector3[] = [];
  readonly shelfSlots: THREE.Vector3[] = [];
  readonly railSlots: THREE.Vector3[] = [];
  readonly payoutSlots: THREE.Vector3[] = [];
  /** A bet enters the chute here, and leaves the building for the teller. */
  readonly betChute = new THREE.Vector3(PROP.betChute[0], 0.95, PROP.betChute[1] + 0.35);
  readonly payoutPos = new THREE.Vector3(PROP.payout[0], 0.95, PROP.payout[1] + 0.5);
  readonly tellerPos = new THREE.Vector3(...POLY_POS);
  private _m4 = new THREE.Matrix4();
  private _m4b = new THREE.Matrix4();

  constructor(private kit: ItemKit) {
    this.buildPlinth();
    this.buildFloors();
    this.buildWalls();
    this.buildPools();
    this.buildSportsbook();
    this.buildPit();
    this.buildRoulette();
    this.buildSlotHall();
    this.buildSalon();
    this.buildCage();
    this.buildBar();
    this.buildLounge();
    this.buildEntrance();
    this.buildNight();
    mergeStatic(this.overviewOnly, [], () => true);
    mergeStatic(
      this.group,
      [this.kioskHead, this.payoutCoin, this.vaultWheel, this.beaconHead, this.displayMesh,
        this.wheelHead, this.ballGroup, this.overviewOnly, this.reelMesh, this.toppers, this.leverArms, this.leverKnobs, this.barSign, ...this.cardPool],
      () => true,
    );
  }

  // ── small builders ─────────────────────────────────────────────────────────

  private tex<T extends THREE.Texture>(t: T): T {
    this.textures.push(t);
    return t;
  }

  private mesh(geo: THREE.BufferGeometry, mat: THREE.Material, x: number, y: number, z: number, cast = true, parent: THREE.Object3D = this.group): THREE.Mesh {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    m.castShadow = cast;
    m.receiveShadow = true;
    parent.add(m);
    return m;
  }

  private boxAt(x: number, z: number, w: number, h: number, d: number, color: string, o: { metal?: number; rough?: number; e?: string; ei?: number } = {}, y0 = 0, parent: THREE.Object3D = this.group): THREE.Mesh {
    return this.mesh(new THREE.BoxGeometry(w, h, d), this.mats.get(color, o), x, y0 + h / 2, z, true, parent);
  }

  private chipStack(x: number, y: number, z: number, color: string, n: number, parent: THREE.Object3D = this.group): void {
    this.mesh(new THREE.CylinderGeometry(0.07, 0.07, 0.022 * n, 10), this.mats.get(color, { rough: 0.45 }), x, y + 0.011 * n, z, true, parent);
    this.mesh(new THREE.CylinderGeometry(0.072, 0.072, 0.012, 10), this.mats.get('#F5F5F4', { rough: 0.45 }), x, y + 0.022 * n - 0.01, z, false, parent);
  }

  /** A chip tray: a brass box with five rows of colour. */
  private chipTray(x: number, y: number, z: number, rotY: number): void {
    const g = new THREE.Group();
    g.position.set(x, y, z);
    g.rotation.y = rotY;
    this.mesh(new THREE.BoxGeometry(0.6, 0.05, 0.22), this.mats.brass(), 0, 0.025, 0, false, g);
    ['#DC2626', '#16A34A', '#1F2937', '#2563EB', '#F5F5F4'].forEach((c, i) => {
      this.mesh(new THREE.BoxGeometry(0.1, 0.05, 0.18), this.mats.get(c, { rough: 0.45 }), -0.24 + i * 0.12, 0.06, 0, false, g);
    });
    this.group.add(g);
  }

  private bankerLamp(x: number, z: number, y: number): void {
    const M = this.mats;
    this.mesh(new THREE.CylinderGeometry(0.08, 0.1, 0.04, 12), M.brass(), x, y + 0.02, z, false);
    this.mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.3, 6), M.brass(), x, y + 0.17, z, false);
    const shade = this.mesh(new THREE.CylinderGeometry(0.06, 0.16, 0.12, 14, 1, true), M.get('#14532d', { e: '#22C55E', ei: 1.6 }), x, y + 0.34, z, false);
    (shade.material as THREE.MeshStandardMaterial).side = THREE.DoubleSide;
    this.mesh(new THREE.SphereGeometry(0.05, 10, 8), M.get('#fff1c2', { e: '#FFD58A', ei: 3 }), x, y + 0.29, z, false);
  }

  private palm(x: number, z: number): void {
    const M = this.mats;
    this.mesh(new THREE.CylinderGeometry(0.24, 0.18, 0.42, 14), M.brass(), x, 0.21, z);
    this.mesh(new THREE.CylinderGeometry(0.05, 0.07, 1.2, 8), M.get('#5b4026', { rough: 0.9 }), x, 1.0, z);
    const leaf = M.get('#1f6b3a', { rough: 0.8 });
    for (let i = 0; i < 7; i++) {
      const a = (i / 7) * Math.PI * 2;
      const l = new THREE.Mesh(new THREE.ConeGeometry(0.09, 0.85, 4), leaf);
      l.scale.set(1, 1, 0.3);
      l.position.set(x + Math.cos(a) * 0.32, 1.56, z + Math.sin(a) * 0.32);
      l.rotation.set(0, -a, 0);
      l.rotateZ(-Math.PI / 2 + 0.55);
      l.castShadow = true;
      this.group.add(l);
    }
  }

  /** A bar stool / slot stool: a brass post, a round red seat. */
  private stool(x: number, z: number, seat = '#7f1d1d'): void {
    const M = this.mats;
    this.mesh(new THREE.CylinderGeometry(0.17, 0.2, 0.04, 14), M.brass(), x, 0.02, z, false);
    this.mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.6, 8), M.brass(), x, 0.32, z, false);
    this.mesh(new THREE.CylinderGeometry(0.2, 0.18, 0.1, 16), M.get(seat, { rough: 0.6 }), x, 0.66, z);
  }

  /** An armchair facing `face` (heading, radians): seat, back, arms. */
  private armchair(x: number, z: number, face: number, color = VELVET): void {
    const g = new THREE.Group();
    g.position.set(x, 0, z);
    g.rotation.y = face;
    const m = this.mats.get(color, { rough: 0.85 });
    this.mesh(new THREE.BoxGeometry(0.62, 0.4, 0.58), m, 0, 0.2, 0, true, g);
    // the back sits behind the sitter: away from where the chair faces
    this.mesh(new THREE.BoxGeometry(0.66, 0.62, 0.14), m, 0, 0.62, -0.26, true, g);
    for (const s of [-1, 1]) this.mesh(new THREE.BoxGeometry(0.1, 0.24, 0.56), m, s * 0.33, 0.48, 0, true, g);
    this.mesh(new THREE.BoxGeometry(0.68, 0.04, 0.16), this.mats.brass(), 0, 0.95, -0.26, false, g);
    this.group.add(g);
  }

  private wall(x0: number, z0: number, x1: number, z1: number, h: number, color: string, t = 0.18, y0 = 0): THREE.Mesh | null {
    const len = Math.hypot(x1 - x0, z1 - z0);
    if (len < 0.01) return null;
    const m = new THREE.Mesh(new THREE.BoxGeometry(len, h, t), this.mats.get(color, { rough: 0.6 }));
    m.position.set((x0 + x1) / 2, y0 + h / 2, (z0 + z1) / 2);
    m.rotation.y = -Math.atan2(z1 - z0, x1 - x0);
    m.castShadow = true;
    m.receiveShadow = true;
    this.group.add(m);
    return m;
  }

  /** A low divider: a wood half-wall with balusters and a brass top rail. */
  private rail(x0: number, z0: number, x1: number, z1: number): void {
    const len = Math.hypot(x1 - x0, z1 - z0);
    if (len < 0.05) return;
    const rot = -Math.atan2(z1 - z0, x1 - x0);
    const ux = (x1 - x0) / len, uz = (z1 - z0) / len;
    const put = (m: THREE.Mesh, s: number, y: number) => {
      m.position.set(x0 + ux * s, y, z0 + uz * s);
      m.rotation.y = rot;
      m.castShadow = true;
      m.receiveShadow = true;
      this.group.add(m);
    };
    put(new THREE.Mesh(new THREE.BoxGeometry(len, 0.38, 0.16), this.mats.get(WOOD, { rough: 0.55 })), len / 2, 0.19);
    put(new THREE.Mesh(new THREE.BoxGeometry(len, 0.04, 0.2), this.mats.brass()), len / 2, 0.4);
    put(new THREE.Mesh(new THREE.BoxGeometry(len, 0.05, 0.1), this.mats.brass()), len / 2, PART_H);
    const n = Math.max(1, Math.round(len / 0.32));
    for (let i = 0; i <= n; i++) {
      put(new THREE.Mesh(new THREE.BoxGeometry(0.03, PART_H - 0.42, 0.03), this.mats.brass()), (len * i) / n, (PART_H + 0.42) / 2);
    }
  }

  private lampPost(x: number, z: number): void {
    this.mesh(new THREE.BoxGeometry(0.14, 1.05, 0.2), this.mats.get(WOOD_D, { rough: 0.5 }), x, 0.525, z);
    this.mesh(new THREE.BoxGeometry(0.18, 0.05, 0.24), this.mats.brass(), x, 1.06, z, false);
    this.mesh(new THREE.SphereGeometry(0.08, 10, 8), this.mats.get('#fff1c2', { e: '#FFC56E', ei: 1.5 }), x, 1.16, z, false);
  }

  /** A screen on the back wall (z = const), facing +z. */
  private wallScreen(p: V2, w: number, h: number, y: number, tex: THREE.Texture, accent: string): void {
    const [x, z] = p;
    this.mesh(new THREE.BoxGeometry(w + 0.18, h + 0.18, 0.1), this.mats.brass(), x, y, z + 0.16, true);
    this.mesh(new THREE.BoxGeometry(w + 0.04, h + 0.04, 0.02), this.mats.get('#05040a'), x, y, z + 0.215, false);
    this.mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: tex, toneMapped: false }), x, y, z + 0.23, false);
    this.mesh(new THREE.BoxGeometry(w + 0.18, 0.05, 0.12), this.mats.get(accent, { e: accent, ei: 1.5 }), x, y - h / 2 - 0.12, z + 0.17, false);
  }

  /** A neon sign: a lacquer box with the lettering on its face, turned to
   *  face `face` (heading). */
  private sign(text: string, color: string, x: number, y: number, z: number, w: number, h: number, face = 0, sub?: string): void {
    const g = new THREE.Group();
    g.position.set(x, y, z);
    g.rotation.y = face;
    this.mesh(new THREE.BoxGeometry(w + 0.1, h + 0.1, 0.08), this.mats.get(LACQ), 0, 0, 0, false, g);
    const t = this.tex(neonText(text, color, 1024, Math.round((1024 * h) / w), sub));
    this.mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: t, toneMapped: false }), 0, 0, 0.045, false, g);
    this.group.add(g);
  }

  // ── the room ──────────────────────────────────────────────────────────────

  private buildPlinth(): void {
    const { x0, x1, z0, z1 } = FLOOR;
    const slab = (pad: number, depth: number, color: string, top: number, o: { e?: string; ei?: number; metal?: number; rough?: number }) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0 + pad * 2, depth, z1 - z0 + pad * 2), this.mats.get(color, o));
      m.position.set((x0 + x1) / 2, top - depth / 2, (z0 + z1) / 2);
      m.receiveShadow = true;
      this.group.add(m);
    };
    slab(0.12, 0.5, '#24160f', -0.035, { rough: 0.5 });
    // the gold line round the whole floor: the building's neon signature
    slab(0.2, 0.07, '#F2C66D', -0.56, { e: '#F59E0B', ei: 1.15, metal: 0.6, rough: 0.35 });
    slab(0.04, 0.5, '#0e0809', -0.63, { rough: 0.8 });
  }

  private buildFloors(): void {
    const tiled = (tex: THREE.CanvasTexture, rx: number, rz: number) => {
      const t = tex.clone();
      t.repeat.set(rx, rz);
      t.needsUpdate = true;
      this.tex(t);
      return new THREE.MeshStandardMaterial({ map: t, roughness: 0.95, metalness: 0 });
    };
    const floor = (x0: number, x1: number, z0: number, z1: number, mat: THREE.Material, y: number, room: RoomId | null) => {
      const m = new THREE.Mesh(new THREE.BoxGeometry(x1 - x0, 0.06, z1 - z0), mat);
      m.position.set((x0 + x1) / 2, y, (z0 + z1) / 2);
      m.receiveShadow = true;
      m.userData.room = room;
      this.group.add(m);
      if (room) this.roomFloors.push(m);
    };
    // the hall's own carpet, wall to wall
    const hall = this.tex(carpetTexture('#2a0f1c', '#c9a04a', '#e0607e'));
    floor(FLOOR.x0, FLOOR.x1, FLOOR.z0, FLOOR.z1, tiled(hall, (FLOOR.x1 - FLOOR.x0) / 2.4, (FLOOR.z1 - FLOOR.z0) / 2.4), 0, null);
    // each area's carpet over it, in its own colours under the same lattice
    const carpets: Partial<Record<RoomId, [string, string, string]>> = {
      research: ['#211a3e', '#c9a04a', '#a78bfa'],
      desk: ['#3a1022', '#d4a650', '#f472b6'],
      vault: ['#12301f', '#d4a650', '#4ade80'],
      forge: ['#0f2a1e', '#d9a646', '#fbbf24'],
      optimizer: ['#2a1630', '#c9a04a', '#7dd3fc'],
      backtest: ['#102c30', '#c9a04a', '#5eead4'],
    };
    for (const r of ROOMS) {
      const c = carpets[r.id];
      if (!c) continue;
      const t = this.tex(carpetTexture(...c));
      floor(r.x0 + 0.05, r.x1 - 0.05, r.z0 + 0.05, r.z1 - 0.05, tiled(t, (r.x1 - r.x0) / 2.2, (r.z1 - r.z0) / 2.2), 0.01, r.id);
    }
    // the lounge: marble
    const lounge = ROOMS.find((r) => r.id === 'bridge')!;
    const marble = this.tex(marbleTexture());
    floor(lounge.x0 + 0.05, lounge.x1 - 0.05, lounge.z0 + 0.05, lounge.z1 - 0.05, tiled(marble, (lounge.x1 - lounge.x0) / 1.4, (lounge.z1 - lounge.z0) / 1.4), 0.012, 'bridge');
    // the red runner: in from the doors, and round the pit
    const runner = this.tex(runnerTexture());
    const strip = (x0: number, z0: number, x1: number, z1: number) => {
      const len = Math.hypot(x1 - x0, z1 - z0);
      const t = runner.clone();
      t.repeat.set(len / 2, 1);
      t.needsUpdate = true;
      this.tex(t);
      const m = new THREE.Mesh(new THREE.BoxGeometry(len, 0.02, 1.15), new THREE.MeshStandardMaterial({ map: t, roughness: 0.95 }));
      m.position.set((x0 + x1) / 2, 0.035, (z0 + z1) / 2);
      m.rotation.y = -Math.atan2(z1 - z0, x1 - x0);
      m.receiveShadow = true;
      this.group.add(m);
    };
    strip(FLOOR.x0, 0, -7.0, 0);
    strip(-7.6, -3.75, 7.3, -3.75);
    strip(-7.6, 3.3, 7.3, 3.3);
    strip(-7.0, -3.2, -7.0, 2.75);
    strip(6.7, -3.2, 6.7, 2.75);
  }

  private buildWalls(): void {
    const M = this.mats;
    const panel = '#2a170f';
    const { x0, x1, z0, z1 } = FLOOR;
    // tall: the back wall and the entrance wall — the far sides from the camera
    this.wall(x0 - 0.1, z0 - 0.1, x1 + 0.1, z0 - 0.1, WALL_H, panel, 0.22);
    this.wall(x0 - 0.1, z0 - 0.1, x0 - 0.1, z1 + 0.1, WALL_H, panel, 0.22);
    const backZ = z0 - 0.1 + 0.11;
    const sideX = x0 - 0.1 + 0.11;
    const backLen = x1 - x0 + 0.2;
    const sideLen = z1 - z0 + 0.2;
    // wainscot, chair rail, crown and pilasters, along both inner faces
    this.mesh(new THREE.BoxGeometry(backLen, 0.9, 0.04), M.get('#3a2014', { rough: 0.5 }), 0, 0.45, backZ + 0.02, false);
    this.mesh(new THREE.BoxGeometry(0.04, 0.9, sideLen), M.get('#3a2014', { rough: 0.5 }), sideX + 0.02, 0.45, 0, false);
    for (const y of [0.92, WALL_H - 0.04]) {
      this.mesh(new THREE.BoxGeometry(backLen, 0.05, 0.08), M.brass(), 0, y, backZ + 0.04, false);
      this.mesh(new THREE.BoxGeometry(0.08, 0.05, sideLen), M.brass(), sideX + 0.04, y, 0, false);
    }
    // no pilaster across a screen, the payout window, the chute or the vault
    const busy: [number, number][] = [[-13.4, -5.6], [-3.4, 1.9], [3.0, 4.2], [11.1, 13.7]];
    for (let x = x0 + 0.6; x <= x1; x += 2.0) {
      if (busy.some(([a, b]) => x > a && x < b)) continue;
      this.mesh(new THREE.BoxGeometry(0.07, WALL_H - 0.95, 0.05), M.brass(), x, (WALL_H + 0.95) / 2, backZ + 0.03, false);
    }
    for (let z = z0 + 0.6; z <= z1; z += 2.0) {
      if (Math.abs(z) < 1.4) continue; // the doors
      this.mesh(new THREE.BoxGeometry(0.05, WALL_H - 0.95, 0.07), M.brass(), sideX + 0.03, (WALL_H + 0.95) / 2, z, false);
    }
    const sconce = M.get('#fff1c2', { e: '#FFB45E', ei: 2.8 });
    for (const x of [-4.4, 5.2, 13.2]) {
      this.mesh(new THREE.BoxGeometry(0.16, 0.24, 0.08), sconce, x, 1.9, backZ + 0.06, false);
      this.mesh(new THREE.BoxGeometry(0.22, 0.04, 0.12), M.brass(), x, 1.76, backZ + 0.07, false);
    }
    for (const z of [-3.2, 3.2, 5.6, 7.6]) {
      this.mesh(new THREE.BoxGeometry(0.08, 0.24, 0.16), sconce, sideX + 0.06, 1.9, z, false);
      this.mesh(new THREE.BoxGeometry(0.12, 0.04, 0.22), M.brass(), sideX + 0.07, 1.76, z, false);
    }
    // a neon strip per back-wall area, in its colour, under the crown
    for (const r of ROOMS) {
      if (r.z0 > z0 + 0.01) continue;
      this.mesh(new THREE.BoxGeometry(r.x1 - r.x0 - 0.4, 0.06, 0.05), M.get(r.accent, { e: r.accent, ei: 1.8 }), (r.x0 + r.x1) / 2, WALL_H - 0.16, backZ + 0.05, false);
    }
    this.mesh(new THREE.BoxGeometry(0.05, 0.06, 5.4), M.get('#2DD4BF', { e: '#2DD4BF', ei: 1.8 }), sideX + 0.05, WALL_H - 0.16, 7.1, false);
    this.mesh(new THREE.BoxGeometry(0.05, 0.06, 5.4), M.get('#A855F7', { e: '#A855F7', ei: 1.8 }), sideX + 0.05, WALL_H - 0.16, -7.1, false);
    // camera-side rims: cut away so the floor reads like a dollhouse
    const rim = '#2a170f';
    // (the front one stops either side of the club's door)
    const dl = CLUB_DOOR[0] - CLUB_DOOR[1] / 2, dr = CLUB_DOOR[0] + CLUB_DOOR[1] / 2;
    for (const [ax, az, bx, bz] of [
      [x0 - 0.1, z1 + 0.1, dl, z1 + 0.1], [dr, z1 + 0.1, x1 + 0.1, z1 + 0.1], [x1 + 0.1, z0 - 0.1, x1 + 0.1, z1 + 0.1],
    ] as const) {
      this.wall(ax, az, bx, bz, RIM_H, rim);
      const len = Math.hypot(bx - ax, bz - az);
      const cap = new THREE.Mesh(new THREE.BoxGeometry(len, 0.04, 0.24), M.brass());
      cap.position.set((ax + bx) / 2, RIM_H + 0.02, (az + bz) / 2);
      cap.rotation.y = -Math.atan2(bz - az, bx - ax);
      this.group.add(cap);
    }
  }

  /** Light pools on the carpet: one instanced quad, additive. They are what
   *  makes the hall read as lit by lamps rather than by a sun. */
  private buildPools(): void {
    const pools: [number, number, number, string][] = [
      [-9.5, -7.2, 6.0, '#8a6ac0'], [0.5, -7.2, 4.6, '#c0607e'], [9.4, -8.0, 3.6, '#6aa86a'],
      [-4.5, -0.4, 3.2, '#c08a46'], [-1.2, -0.4, 3.2, '#c08a46'], [2.4, -0.5, 3.6, '#6a9cc0'],
      [-11.6, 6.8, 4.6, '#4aa898'], [-6.7, 7.0, 3.2, '#4aa898'], [10.6, -1.2, 4.0, '#c09a46'],
      [10.4, 6.4, 5.0, '#c09a46'], [-11.6, 0, 3.0, '#a07a52'],
    ];
    for (let x = -6.4; x <= 6.4; x += 3.2) { pools.push([x, -3.75, 2.2, '#a07a52']); pools.push([x, 3.3, 2.2, '#a07a52']); }
    const geo = new THREE.PlaneGeometry(1, 1);
    geo.rotateX(-Math.PI / 2);
    const mat = new THREE.MeshBasicMaterial({ map: this.kit.glowTex, transparent: true, opacity: 0.6, blending: THREE.AdditiveBlending, depthWrite: false });
    const im = new THREE.InstancedMesh(geo, mat, pools.length);
    const m4 = new THREE.Matrix4();
    const c = new THREE.Color();
    pools.forEach(([x, z, s, col], i) => {
      m4.makeScale(s, 1, s).setPosition(x, 0.05, z);
      im.setMatrixAt(i, m4);
      im.setColorAt(i, c.set(col));
    });
    im.frustumCulled = false;
    im.renderOrder = 1;
    this.group.add(im);
    const pool = (x: number, z: number, s: number, color: string, opacity: number) => {
      const mm = new THREE.MeshBasicMaterial({ map: this.kit.glowTex, color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false });
      const p = new THREE.Mesh(geo, mm);
      p.scale.set(s, 1, s);
      p.position.set(x, 0.055, z);
      p.renderOrder = 1;
      this.group.add(p);
      return mm;
    };
    this.cagePool = pool(PROP.pile[0], PROP.pile[1], 3.6, '#E0A23A', 0.5);
    this.alarmPool = pool(9.5, -5.6, 6.5, '#ff2a2a', 0);
  }

  // ── the sportsbook ────────────────────────────────────────────────────────

  private buildSportsbook(): void {
    const M = this.mats;
    // the monitor wall: four across, two high. Top left is YOUR BETS, the
    // rest the board, five markets a screen, in a fixed order
    {
      const [cx, wz] = PROP.videoWall;
      const mw = 1.8, mh = 1.0125, gap = 0.12;
      const lit = new Map<number, number>();
      const per = SB_PER_MONITOR;
      for (let k = 0; k < 8; k++) {
        const col = k % 4, row = Math.floor(k / 4);
        const x = cx + (col - 1.5) * (mw + gap), y = row === 0 ? 2.2 : 1.12;
        const from = (k - 1) * per;
        const scr = new Screen(768, 432, 1, (g, w, h) => {
          if (k === 0) { paintBets(g, w, h, this.tickets, this.boardRows, 6); return; }
          lit.clear();
          for (const b of this.blips) lit.set(b.row, Math.max(0, 1 - b.age / 3));
          paintMarkets(g, w, h, {
            rows: this.boardRows, from, count: per, at: this.boardAt, note: this.boardNote, lit,
            title: this.boardRows.length > from ? `MARKETS ${from + 1}–${Math.min(this.boardRows.length, from + per)}` : 'SPORTSBOOK',
          });
        });
        this.tex(scr.tex);
        this.monitors.push(scr);
        this.mesh(new THREE.BoxGeometry(mw + 0.08, mh + 0.08, 0.08), M.get('#0b0910', { rough: 0.4, metal: 0.4 }), x, y, wz + 0.16, false);
        this.mesh(new THREE.PlaneGeometry(mw, mh), new THREE.MeshBasicMaterial({ map: scr.tex, toneMapped: false }), x, y, wz + 0.205, false);
      }
      this.mesh(new THREE.BoxGeometry(4 * mw + 3 * gap + 0.2, 0.05, 0.12), M.get('#A855F7', { e: '#A855F7', ei: 1.5 }), cx, 0.52, wz + 0.17, false);
    }
    const side = this.tex(tickerTexture());
    // the odds cube on its column
    {
      const [kx, kz] = PROP.kiosk;
      this.mesh(new THREE.CylinderGeometry(0.5, 0.58, 0.16, 20), M.brass(), kx, 0.08, kz);
      this.mesh(new THREE.CylinderGeometry(0.11, 0.16, 1.35, 12), M.get(LACQ, { rough: 0.3 }), kx, 0.82, kz);
      this.mesh(new THREE.CylinderGeometry(0.2, 0.2, 0.08, 16), M.brass(), kx, 1.5, kz, false);
      const face = new THREE.MeshBasicMaterial({ map: side, toneMapped: false });
      const cap = M.get(LACQ, { rough: 0.3 });
      const cube = new THREE.Mesh(new THREE.BoxGeometry(0.82, 0.62, 0.82), [face, face, cap, cap, face, face]);
      cube.castShadow = true;
      this.kioskTip = M.own('#A855F7', '#A855F7', 2);
      this.kioskHead.position.set(kx, 1.92, kz);
      this.kioskHead.add(cube,
        at(new THREE.Mesh(new THREE.BoxGeometry(0.86, 0.04, 0.86), M.brass()), 0, 0.33, 0),
        at(new THREE.Mesh(new THREE.BoxGeometry(0.86, 0.04, 0.86), M.brass()), 0, -0.33, 0),
        at(new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 10), this.kioskTip), 0, 0.45, 0));
      this.group.add(this.kioskHead);
    }
    // betting windows on the wall side: writers behind, guests in front (+x)
    for (const [x, z] of [PROP.windowA, PROP.windowB]) {
      const g = new THREE.Group();
      g.position.set(x, 0, z);
      g.rotation.y = Math.PI / 2; // the guest side faces +x
      this.mesh(new THREE.BoxGeometry(1.2, 0.98, 0.56), M.get(WOOD, { rough: 0.5 }), 0, 0.49, 0, true, g);
      this.mesh(new THREE.BoxGeometry(1.28, 0.04, 0.64), M.brass(), 0, 1.0, 0, false, g);
      for (let i = 0; i < 6; i++) this.mesh(new THREE.BoxGeometry(0.025, 0.55, 0.025), M.brass(), -0.5 + i * 0.2, 1.3, 0.25, false, g);
      this.mesh(new THREE.BoxGeometry(1.1, 0.06, 0.05), M.brass(), 0, 1.58, 0.25, false, g);
      this.mesh(new THREE.BoxGeometry(0.6, 0.36, 0.05), M.get(LACQ), 0, 1.22, -0.05, false, g);
      const scr = this.mesh(new THREE.PlaneGeometry(0.54, 0.3), M.get('#120d22', { e: '#A855F7', ei: 1.3 }), 0, 1.22, -0.08, false, g);
      scr.rotation.y = Math.PI;
      this.mesh(new THREE.BoxGeometry(0.9, 0.04, 0.02), M.get('#120d22', { e: '#A855F7', ei: 1.8 }), 0, 0.9, 0.29, false, g);
      this.group.add(g);
    }
    this.sign('BETS', '#C084FC', -13.82, 2.15, -7.7, 1.4, 0.42, Math.PI / 2);
    // the ticket printer
    const [px, pz] = PROP.printer;
    this.boxAt(px, pz, 0.52, 0.98, 0.62, LACQ, { rough: 0.35 });
    this.mesh(new THREE.BoxGeometry(0.56, 0.04, 0.66), M.brass(), px, 1.0, pz, false);
    this.printerLight = M.own('#2a1f45', '#A855F7', 0.6);
    this.mesh(new THREE.BoxGeometry(0.06, 0.05, 0.42), this.printerLight, px + 0.27, 0.86, pz, false);
    // the seats, in rows facing the board, a cocktail table every second one
    for (const [x, z] of SB_SEATS) this.armchair(x, z, Math.PI, '#3b1d5e');
    for (const [x, z] of [[-10.3, -7.95], [-8.3, -7.95], [-10.3, -6.35], [-8.3, -6.35]] as const) {
      this.mesh(new THREE.CylinderGeometry(0.16, 0.16, 0.03, 14), M.brass(), x, 0.62, z + 0.38, false);
      this.mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.6, 6), M.brass(), x, 0.31, z + 0.38, false);
    }
  }

  // ── the blackjack pit ─────────────────────────────────────────────────────

  private buildPit(): void {
    const M = this.mats;
    const feltTex = this.tex(blackjackFeltTexture());
    feltTex.repeat.set(1 / (2 * (BJ_R - 0.1)), 1 / (BJ_R - 0.1));
    feltTex.offset.set(0.5, 1);
    const feltMat = new THREE.MeshStandardMaterial({ map: feltTex, roughness: 0.95 });
    BJ_TABLES.forEach(([tx, tz], t) => {
      // the half-moon: the arc toward +z (the guests), the flat edge at tz
      const half = (r: number) => ({ thetaStart: -Math.PI / 2, thetaLength: Math.PI, r });
      const h1 = half(BJ_R - 0.15);
      this.mesh(new THREE.CylinderGeometry(h1.r, h1.r - 0.05, 0.7, 24, 1, false, h1.thetaStart, h1.thetaLength), M.get(WOOD, { rough: 0.55 }), tx, 0.35, tz);
      this.mesh(new THREE.CylinderGeometry(BJ_R, BJ_R, 0.08, 28, 1, false, -Math.PI / 2, Math.PI), M.get(WOOD_D, { rough: 0.5 }), tx, 0.74, tz);
      // the dealer's side is flat: close it
      this.mesh(new THREE.BoxGeometry(2 * (BJ_R - 0.15), 0.7, 0.04), M.get(WOOD, { rough: 0.55 }), tx, 0.35, tz + 0.02);
      this.mesh(new THREE.BoxGeometry(2 * BJ_R, 0.08, 0.06), M.get(WOOD_D, { rough: 0.5 }), tx, 0.74, tz + 0.03, false);
      // the felt: a half-disc with the table's lettering
      const s = new THREE.Shape();
      const R = BJ_R - 0.1;
      s.moveTo(-R, 0);
      s.absarc(0, 0, R, Math.PI, 2 * Math.PI, false);
      s.lineTo(-R, 0);
      const felt = new THREE.Mesh(new THREE.ShapeGeometry(s, 24), feltMat);
      felt.rotation.x = -Math.PI / 2;
      felt.position.set(tx, 0.785, tz);
      felt.receiveShadow = true;
      this.group.add(felt);
      // the padded rail round the arc
      const torus = new THREE.Mesh(new THREE.TorusGeometry(BJ_R - 0.04, 0.06, 6, 18, Math.PI), M.get(LEATHER, { rough: 0.55 }));
      torus.rotation.x = Math.PI / 2;
      torus.position.set(tx, 0.8, tz);
      this.group.add(torus);
      // the dealer's edge: chip rack, the shoe, a limit sign
      this.chipTray(tx, 0.79, tz + 0.14, 0);
      this.boxAt(tx + 0.55, tz + 0.12, 0.16, 0.12, 0.26, '#1d4ed8', { rough: 0.4 }, 0.78);
      const lim = M.own('#1a0f05', '#F59E0B', 1.2);
      this.limitSigns.push(lim);
      this.mesh(new THREE.BoxGeometry(0.3, 0.16, 0.04), M.brass(), tx - 0.62, 0.92, tz + 0.05, false);
      this.mesh(new THREE.BoxGeometry(0.26, 0.12, 0.02), lim, tx - 0.62, 0.92, tz + 0.08, false);
      // the guests' stools
      for (let k = 0; k < 4; k++) {
        const [sx, sz] = bjSeat(t, k);
        this.stool(sx, sz, '#14532d');
      }
    });
    // the first table's "deal" card: it flashes when strategy work is dealt
    this.dealCard = M.own(IVORY, '#FDE68A', 0, 0.1, 0.5);
    const card = this.mesh(new THREE.BoxGeometry(0.16, 0.012, 0.23), this.dealCard, this.dealSpot.x - 0.3, 0.8, this.dealSpot.z - 0.05, false);
    card.rotation.y = 0.3;
    // the pit stand: the shuffler and the shoe between the tables
    {
      const [sx, sz] = PROP.shoe;
      this.boxAt(sx, sz, 1.1, 0.9, 0.7, WOOD, { rough: 0.55 });
      this.mesh(new THREE.BoxGeometry(1.16, 0.04, 0.76), M.brass(), sx, 0.92, sz, false);
      const shuf = this.boxAt(sx - 0.2, sz, 0.42, 0.28, 0.36, LACQ, { rough: 0.3 }, 0.94);
      shuf.castShadow = true;
      this.shuffleLight = M.own('#3b2408', '#F59E0B', 1.4);
      this.mesh(new THREE.BoxGeometry(0.3, 0.03, 0.04), this.shuffleLight, sx - 0.2, 1.23, sz + 0.19, false);
      for (let i = 0; i < 3; i++) this.boxAt(sx + 0.2 + i * 0.13, sz - 0.1, 0.1, 0.08 + i * 0.03, 0.15, i === 1 ? '#1d4ed8' : '#b91c1c', { rough: 0.5 }, 0.94);
      this.bankerLamp(sx + 0.42, sz + 0.2, 0.94);
    }
    // the side tables: fresh strategy work comes in, finished chips wait
    for (const [[x, z], slots] of [[PROP.inbox, this.inboxSlots], [PROP.shelf, this.shelfSlots]] as const) {
      this.boxAt(x, z, 0.75, 0.78, 0.55, WOOD, { rough: 0.5 });
      this.mesh(new THREE.BoxGeometry(0.7, 0.02, 0.5), M.get(FELT, { rough: 0.95 }), x, 0.79, z, false);
      for (let i = 0; i < 3; i++) slots.push(new THREE.Vector3(x - 0.22 + i * 0.22, 0.84, z));
    }
    // the cards you (and only you) are dealt: a small pool of quads on the atlas
    const atlas = this.tex(cardAtlasTexture());
    this.cardMat = new THREE.MeshStandardMaterial({ map: atlas, roughness: 0.5 });
    for (let i = 0; i < 24; i++) {
      const geo = new THREE.PlaneGeometry(0.16, 0.16 * (CARD_H / CARD_W));
      geo.rotateX(-Math.PI / 2);
      const m = new THREE.Mesh(geo, this.cardMat);
      m.visible = false;
      m.castShadow = false;
      this.cardPool.push(m);
      this.group.add(m);
    }
  }

  // ── roulette ──────────────────────────────────────────────────────────────

  private buildRoulette(): void {
    const M = this.mats;
    const [wx, wz] = PROP.wheel;
    const R = 0.8;
    this.mesh(new THREE.CylinderGeometry(R, R - 0.1, 0.76, 28), M.get(WOOD, { rough: 0.5 }), wx, 0.38, wz);
    const rim = this.mesh(new THREE.TorusGeometry(R - 0.04, 0.05, 6, 28), M.get(WOOD_D, { rough: 0.4 }), wx, 0.78, wz, false);
    rim.rotation.x = Math.PI / 2;
    this.mesh(new THREE.CylinderGeometry(R - 0.09, R - 0.09, 0.02, 28), M.get('#2b1a10', { rough: 0.4 }), wx, 0.77, wz, false);
    this.wheelHead.position.set(wx, 0.79, wz);
    const faceTex = this.tex(wheelTexture());
    const face = new THREE.Mesh(new THREE.CircleGeometry(R - 0.17, 37), new THREE.MeshStandardMaterial({ map: faceTex, roughness: 0.4, metalness: 0.1 }));
    face.rotation.x = -Math.PI / 2;
    const gold = M.get('#d8b46a', { metal: 0.85, rough: 0.28 });
    const cone = new THREE.Mesh(new THREE.ConeGeometry(0.16, 0.12, 16), gold);
    cone.position.y = 0.06;
    const spindle = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.18, 8), gold);
    spindle.position.y = 0.18;
    this.wheelHead.add(face, cone, spindle);
    for (let i = 0; i < 4; i++) {
      const arm = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.02, 0.02), gold);
      arm.position.y = 0.24;
      arm.rotation.y = (i * Math.PI) / 4;
      this.wheelHead.add(arm);
    }
    this.group.add(this.wheelHead);
    this.ballGroup.position.set(wx, 0.83, wz);
    this.ball = at(new THREE.Mesh(new THREE.SphereGeometry(0.035, 12, 10), M.get('#ffffff', { e: '#ffffff', ei: 0.5, rough: 0.2 })), R - 0.14, 0, 0);
    this.ballGroup.add(this.ball);
    this.group.add(this.ballGroup);
    // the layout table, butted against the wheel
    const [lx, lz] = PROP.layout;
    this.boxAt(lx, lz, 2.5, 0.72, 1.12, WOOD, { rough: 0.55 });
    this.mesh(new THREE.BoxGeometry(2.6, 0.06, 1.22), M.get(WOOD_D, { rough: 0.45 }), lx, 0.75, lz, false);
    const layoutTex = this.tex(rouletteLayoutTexture());
    this.rouletteLayout = this.mesh(new THREE.BoxGeometry(2.4, 0.02, 1.02), new THREE.MeshStandardMaterial({ map: layoutTex, roughness: 0.95 }), lx, 0.785, lz, false);
    this.mesh(new THREE.BoxGeometry(2.6, 0.07, 0.07), M.get(LEATHER, { rough: 0.6 }), lx, 0.82, lz + 0.59, false);
    this.chipTray(lx, 0.79, lz - 0.45, 0);
    // the last-numbers tower: the real results of the real spins
    const [mx, mz] = PROP.marquee;
    const mg = new THREE.Group();
    mg.position.set(mx, 0, mz);
    // turned to the players at the layout
    mg.rotation.y = Math.atan2(3.4 - mx, 0.5 - mz);
    this.mesh(new THREE.CylinderGeometry(0.06, 0.09, 0.6, 8), M.brass(), 0, 0.3, 0, true, mg);
    this.boxAt(0, 0, 0.5, 1.6, 0.3, LACQ, { rough: 0.3 }, 0.6, mg);
    this.marquee = new Screen(128, 400, 4, (g, w, h) => this.drawMarquee(g, w, h));
    this.tex(this.marquee.tex);
    this.mesh(new THREE.PlaneGeometry(0.42, 1.36), new THREE.MeshBasicMaterial({ map: this.marquee.tex, toneMapped: false }), 0, 1.4, 0.16, false, mg);
    this.mesh(new THREE.BoxGeometry(0.56, 0.06, 0.36), M.brass(), 0, 2.23, 0, false, mg);
    for (const [dx, c] of [[-0.12, '#DC2626'], [0, '#16A34A'], [0.12, '#F5F5F4']] as const) {
      this.mesh(new THREE.SphereGeometry(0.05, 10, 8), M.get(c, { e: c, ei: 1.6 }), dx, 2.32, 0, false, mg);
    }
    this.group.add(mg);
    // the roulette players' stools along the layout
    for (const x of [2.75, 3.6, 4.45]) this.stool(x, 0.5, '#1e3a8a');
  }

  private drawMarquee(g: CanvasRenderingContext2D, w: number, h: number): void {
    g.fillStyle = '#05060a';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#38BDF8';
    g.font = '700 18px "Chakra Petch", sans-serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText('LAST', w / 2, 18);
    const nums = this.lastNumbers.slice(-9).reverse();
    if (!nums.length) {
      g.fillStyle = 'rgba(255,255,255,0.4)';
      g.font = '500 14px "JetBrains Mono", monospace';
      g.fillText('no spins', w / 2, h / 2);
    }
    nums.forEach((n, i) => {
      const y = 50 + i * 38;
      const col = colorOf(n);
      g.fillStyle = col === 'red' ? '#EF4444' : col === 'green' ? '#22C55E' : '#E5E7EB';
      g.font = `800 ${i === 0 ? 34 : 26}px "Chakra Petch", sans-serif`;
      g.globalAlpha = i === 0 ? 1 : 0.85 - i * 0.06;
      g.fillText(String(n), col === 'red' ? w * 0.35 : col === 'black' ? w * 0.65 : w / 2, y);
    });
    g.globalAlpha = 1;
    if (this.marqueeFlash > 0) {
      g.fillStyle = `rgba(56,189,248,${this.marqueeFlash * 0.25})`;
      g.fillRect(0, 0, w, h);
    }
  }

  // ── the slot hall ──────────────────────────────────────────────────────────

  private buildSlotHall(): void {
    const M = this.mats;
    const machines = [...SLOTS.map((s) => ({ ...s, replay: false })), { x: PROP.slot[0], z: PROP.slot[1], fx: 0, fz: -1, replay: true }];
    const stripTex = this.tex(reelStripTexture());
    stripTex.wrapS = THREE.RepeatWrapping;
    const reelGeo = new THREE.CylinderGeometry(REEL_R, REEL_R, 0.14, 24, 1, true, -REEL_ARC / 2, REEL_ARC);
    // the arc's u runs over REEL_SHOWS symbols, centred on the front; each
    // reel's offset (aOff, in strip widths) scrolls it
    const uv = reelGeo.attributes.uv as THREE.BufferAttribute;
    for (let i = 0; i < uv.count; i++) uv.setX(i, (uv.getX(i) - 0.5) * (REEL_SHOWS / STRIP.length));
    reelGeo.rotateZ(Math.PI / 2);
    reelGeo.setAttribute('aOff', new THREE.InstancedBufferAttribute(new Float32Array(machines.length * 3), 1));
    const reelMat = new THREE.MeshBasicMaterial({ map: stripTex, toneMapped: false });
    reelMat.onBeforeCompile = (sh) => {
      sh.vertexShader = sh.vertexShader
        .replace('#include <common>', '#include <common>\nattribute float aOff;')
        .replace('#include <uv_vertex>', '#include <uv_vertex>\n#ifdef USE_MAP\n  vMapUv.x += aOff;\n#endif');
    };
    this.reelMesh = new THREE.InstancedMesh(reelGeo, reelMat, machines.length * 3);
    this.reelMesh.frustumCulled = false;
    this.toppers = new THREE.InstancedMesh(new THREE.BoxGeometry(0.62, 0.2, 0.34), new THREE.MeshBasicMaterial({ color: '#ffffff', toneMapped: false }), machines.length);
    this.toppers.frustumCulled = false;
    {
      // the lever's arm and knob, built about its pivot at the arm's foot
      const arm = new THREE.CylinderGeometry(0.02, 0.02, 0.45, 6);
      arm.translate(0, 0.225, 0);
      const knob = new THREE.SphereGeometry(0.06, 10, 8);
      knob.translate(0, 0.475, 0);
      this.leverArms = new THREE.InstancedMesh(arm, M.brass(), machines.length);
      this.leverKnobs = new THREE.InstancedMesh(knob, M.get('#DC2626', { e: '#DC2626', ei: 0.6, rough: 0.3 }), machines.length);
      this.leverArms.frustumCulled = this.leverKnobs.frustumCulled = false;
      this.leverPull = machines.map(() => 0);
    }
    machines.forEach((s, i) => {
      const yaw = Math.atan2(s.fx, s.fz);
      const g = new THREE.Group();
      g.position.set(s.x, 0, s.z);
      g.rotation.y = yaw;
      const body = s.replay ? '#0b2a30' : ['#2a0f2e', '#0f1d3a', '#2e1a0b', '#0f2e1d'][i % 4];
      this.boxAt(0, 0, 0.66, 0.78, 0.6, LACQ, { rough: 0.3 }, 0, g);
      this.boxAt(0, -0.08, 0.62, 0.78, 0.44, body, { rough: 0.35 }, 0.78, g);
      this.mesh(new THREE.BoxGeometry(0.66, 0.05, 0.62), M.brass(), 0, 0.8, 0, false, g);
      // the reel window and its brass frame; the screen glass sits proud
      this.mesh(new THREE.BoxGeometry(0.56, 0.3, 0.02), M.get('#05040a'), 0, REEL_Y, 0.15, false, g);
      for (const dy of [-0.17, 0.17]) this.mesh(new THREE.BoxGeometry(0.6, 0.03, 0.05), M.brass(), 0, REEL_Y + dy, 0.17, false, g);
      // the payline
      // (two arrows at the window's edges: a bar across it hid the very
      // symbol it marks)
      for (const sx of [-1, 1]) {
        const arrow = this.mesh(new THREE.ConeGeometry(0.022, 0.04, 3), M.get('#DC2626', { e: '#EF4444', ei: 2 }), sx * 0.27, REEL_Y, 0.3, false, g);
        arrow.rotation.z = (sx * Math.PI) / 2;
      }
      // the button deck and the lever
      const deck = this.mesh(new THREE.BoxGeometry(0.6, 0.05, 0.22), M.get('#1f1724', { rough: 0.4 }), 0, 0.84, 0.24, false, g);
      deck.rotation.x = -0.25;
      for (const bx of [-0.15, 0, 0.15]) this.mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.03, 10), M.get('#F59E0B', { e: '#F59E0B', ei: 1.2 }), bx, 0.88, 0.25, false, g);
      this.group.add(g);
      g.updateMatrixWorld(true);
      // the topper: one instanced draw for the whole hall
      const top = new THREE.Matrix4().makeTranslation(0, 1.72, -0.05).premultiply(g.matrixWorld);
      this.toppers.setMatrixAt(i, top);
      this.toppers.setColorAt(i, this._c.set(s.replay ? '#2DD4BF' : TOPPER_COLORS[i % TOPPER_COLORS.length]).multiplyScalar(0.8));
      const reels: Reels = { base: g.matrixWorld.clone(), angle: [0, 0, 0], spin: null, free: s.replay, flash: 0 };
      // a random resting line, so the hall isn't all one picture
      for (let k = 0; k < 3; k++) reels.angle[k] = STOP_ANGLE(Math.floor(Math.random() * STRIP.length));
      this.reels.push(reels);
      if (!s.replay) {
        const [sx, sz] = slotSpot(i);
        this.stool(sx, sz);
      }
    });
    this.group.add(this.reelMesh, this.toppers, this.leverArms, this.leverKnobs);
    this.writeReels(true);
    this.writeLevers();
    this.sign('SLOTS', '#2DD4BF', -13.82, 2.15, 7.1, 1.6, 0.48, Math.PI / 2, '94% RTP · PLAY CHIPS');

    // the practice table: the round table, the replay display over it
    const [hx, hz] = PROP.practice;
    this.mesh(new THREE.CylinderGeometry(0.42, 0.5, 0.08, 20), M.brass(), hx, 0.04, hz);
    this.mesh(new THREE.CylinderGeometry(0.22, 0.3, 0.66, 16), M.get(WOOD, { rough: 0.5 }), hx, 0.38, hz);
    this.mesh(new THREE.CylinderGeometry(0.95, 0.9, 0.1, 28), M.get(WOOD_D, { rough: 0.5 }), hx, 0.75, hz);
    this.mesh(new THREE.CylinderGeometry(0.81, 0.81, 0.02, 28), M.get('#0d5048', { rough: 0.95 }), hx, 0.8, hz, false);
    const prail = this.mesh(new THREE.TorusGeometry(0.88, 0.07, 6, 28), M.get(LEATHER, { rough: 0.55 }), hx, 0.82, hz, false);
    prail.rotation.x = Math.PI / 2;
    for (const [dx, dz, c] of [[0.35, 0.25, '#DC2626'], [-0.3, 0.35, '#1F2937'], [0.1, -0.4, '#2563EB']] as const) this.chipStack(hx + dx, 0.82, hz + dz, c, 4);
    this.display = new Screen(320, 200, 30, (g, w, h) => this.drawDisplay(g, w, h));
    this.tex(this.display.tex);
    this.displayMesh = at(new THREE.Mesh(
      new THREE.PlaneGeometry(2.2, 1.375),
      new THREE.MeshBasicMaterial({ map: this.display.tex, transparent: true, opacity: 0.95, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }),
    ), hx, 1.95, hz);
    this.displayMesh.rotation.y = Math.PI / 4;
    this.group.add(this.displayMesh);
    this.candle = M.own('#334155', '#64748B', 0.5);
    this.mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.75, 6), M.brass(), hx + 0.62, 1.15, hz - 0.62, false);
    this.mesh(new THREE.CylinderGeometry(0.09, 0.09, 0.26, 12), this.candle, hx + 0.62, 1.62, hz - 0.62, false);
    this.mesh(new THREE.CylinderGeometry(0.1, 0.1, 0.04, 12), M.brass(), hx + 0.62, 1.77, hz - 0.62, false);
    const [bx, bz] = PROP.discard;
    this.mesh(new THREE.CylinderGeometry(0.28, 0.24, 0.55, 16), M.get(LACQ, { rough: 0.35 }), bx, 0.275, bz);
    this.discardRing = M.own('#7f1d1d', '#EF4444', 0.6);
    const ring = this.mesh(new THREE.TorusGeometry(0.28, 0.035, 8, 20), this.discardRing, bx, 0.56, bz, false);
    ring.rotation.x = Math.PI / 2;
  }

  /** Pose the levers: pulled down toward you, springing back. */
  private writeLevers(): void {
    const m = this._m4, r = this._m4b;
    this.reels.forEach((rl, i) => {
      const k = this.leverPull[i] ?? 0;
      r.makeRotationX(Math.sin(Math.min(1, k) * Math.PI) * 1.05);
      m.makeTranslation(0.36, 0.93, 0.05).multiply(r).premultiply(rl.base);
      this.leverArms.setMatrixAt(i, m);
      this.leverKnobs.setMatrixAt(i, m);
    });
    this.leverArms.instanceMatrix.needsUpdate = true;
    this.leverKnobs.instanceMatrix.needsUpdate = true;
  }

  /** Pull machine i's lever (you, at the machine). */
  pullLever(i: number): void {
    if (this.leverPull[i] !== undefined) this.leverPull[i] = 0.0001;
  }

  /** Machine i's frame: its local +z faces the player, y up, at the floor. */
  slotFrame(i: number): THREE.Matrix4 {
    return this.reels[i].base.clone();
  }

  /** Pose every reel from its angle (all of them once, then only the moving). */
  private writeReels(all: boolean): void {
    const m = this._m4;
    const off = this.reelMesh.geometry.attributes.aOff as THREE.InstancedBufferAttribute;
    this.reels.forEach((rl, i) => {
      if (!all && !rl.spin && !(rl.free && this.replaying)) return;
      for (let k = 0; k < 3; k++) {
        if (all) {
          m.makeTranslation(REEL_X[k], REEL_Y, REEL_Z).premultiply(rl.base);
          this.reelMesh.setMatrixAt(i * 3 + k, m);
        }
        // the drum's angle, as how far round its strip it has turned
        const a = rl.angle[k] / (Math.PI * 2);
        off.setX(i * 3 + k, a - Math.floor(a));
      }
    });
    if (all) this.reelMesh.instanceMatrix.needsUpdate = true;
    off.needsUpdate = true;
  }

  // ── the high-limit salon ───────────────────────────────────────────────────

  private buildSalon(): void {
    const M = this.mats;
    // its glass: a low brass-capped wall with a short pane in the overview
    // (first person raises it to the ceiling, walkshell.ts)
    const glass = new THREE.MeshStandardMaterial({ color: '#f6e7c8', transparent: true, opacity: 0.16, roughness: 0.05, metalness: 0.2, depthWrite: false });
    for (const [ax, az, bx, bz] of SALON_GLASS) {
      this.rail(ax, az, bx, bz);
      const len = Math.hypot(bx - ax, bz - az);
      const pane = new THREE.Mesh(new THREE.BoxGeometry(len, 0.4, 0.03), glass);
      pane.position.set((ax + bx) / 2, PART_H + 0.22, (az + bz) / 2);
      pane.rotation.y = -Math.atan2(bz - az, bx - ax);
      this.group.add(pane);
    }
    for (const s of [-1, 1]) this.lampPost(SALON_DOOR[0] + s * (SALON_DOOR[1] / 2 + 0.05), SALON_FRONT_Z);
    this.sign('HIGH LIMIT', '#F472B6', SALON_DOOR[0], 1.75, SALON_FRONT_Z, 1.5, 0.36, 0, 'REAL ORDERS');
    // the long table: a felt stadium, padded all round
    const stadium = (L: number, D: number) => {
      const r = D / 2, h = L / 2 - r;
      const s = new THREE.Shape();
      s.moveTo(-h, -r);
      s.lineTo(h, -r);
      s.absarc(h, 0, r, -Math.PI / 2, Math.PI / 2, false);
      s.lineTo(-h, r);
      s.absarc(-h, 0, r, Math.PI / 2, (3 * Math.PI) / 2, false);
      return s;
    };
    const [tx, tz] = PROP.table;
    const slab = (shape: THREE.Shape, depth: number, y: number, mat: THREE.Material, cast: boolean) => {
      const m = new THREE.Mesh(new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: false, curveSegments: 14 }), mat);
      m.rotation.x = -Math.PI / 2;
      m.position.set(tx, y, tz);
      m.castShadow = cast;
      m.receiveShadow = true;
      this.group.add(m);
    };
    const L = 3.6, D = 1.3;
    slab(stadium(L - 0.5, D - 0.45), 0.7, 0, M.get(WOOD, { rough: 0.55 }), true);
    slab(stadium(L, D), 0.08, 0.7, M.get(WOOD_D, { rough: 0.5 }), true);
    slab(stadium(L - 0.22, D - 0.22), 0.02, 0.78, M.get('#6b1028', { rough: 0.95 }), false);
    const ring = stadium(L + 0.02, D + 0.02);
    ring.holes.push(stadium(L - 0.2, D - 0.2) as unknown as THREE.Path);
    slab(ring, 0.1, 0.76, M.get(LEATHER, { rough: 0.55 }), false);
    const line = stadium(L - 0.6, D - 0.6);
    line.holes.push(stadium(L - 0.66, D - 0.66) as unknown as THREE.Path);
    slab(line, 0.005, 0.8, M.get('#E8C170', { e: '#B8862B', ei: 0.4 }), false);
    const sign = M.own('#1a0610', '#EC4899', 1.2);
    this.limitSigns.push(sign);
    for (const [x, z] of [PROP.tableA, PROP.tableB]) {
      this.chipTray(x, 0.81, z - 0.42, 0);
      this.mesh(new THREE.BoxGeometry(0.34, 0.16, 0.05), M.brass(), x + 0.55, 0.9, z - 0.5, false);
      this.mesh(new THREE.BoxGeometry(0.3, 0.12, 0.03), sign, x + 0.55, 0.9, z - 0.47, false);
    }
    // the board of real orders, over the dealers
    this.bets = new Screen(384, 160, 8, (g, w, h) => this.drawBets(g, w, h));
    this.tex(this.bets.tex);
    this.wallScreen(PROP.betsBoard, 2.4, 1.0, 1.75, this.bets.tex, '#EC4899');
    // bets leave through a brass chute in the back wall
    {
      const [cx, cz] = PROP.betChute;
      const tube = this.mesh(new THREE.CylinderGeometry(0.42, 0.42, 0.75, 20, 1, true), new THREE.MeshStandardMaterial({ color: BRASS, metalness: 0.85, roughness: 0.3, side: THREE.DoubleSide }), cx, 0.95, cz + 0.45);
      tube.rotation.x = Math.PI / 2;
      this.mesh(new THREE.CircleGeometry(0.4, 20), M.get('#0b0610'), cx, 0.95, cz + 0.12, false);
      this.chuteRing = M.own('#EC4899', '#EC4899', 1.2);
      this.mesh(new THREE.TorusGeometry(0.45, 0.06, 10, 24), this.chuteRing, cx, 0.95, cz + 0.82, false);
      this.mesh(new THREE.TorusGeometry(0.45, 0.06, 8, 24), this.chuteRing, cx, 0.95, cz + 0.42, false);
      this.boxAt(cx, cz + 0.5, 1.0, 0.5, 0.6, WOOD, { rough: 0.55 });
    }
    // winnings come back through the payout window: a brass grille and a coin
    {
      const [hx, hz] = PROP.payout;
      this.mesh(new THREE.BoxGeometry(1.5, 1.7, 0.16), M.get(WOOD_D, { rough: 0.5 }), hx, 0.95, hz + 0.19, true);
      this.payoutLight = M.own('#3b2a08', '#FBBF24', 0.5);
      this.mesh(new THREE.PlaneGeometry(1.1, 1.2), this.payoutLight, hx, 1.0, hz + 0.28, false);
      for (let i = 0; i < 5; i++) this.mesh(new THREE.BoxGeometry(0.03, 1.2, 0.03), M.brass(), hx - 0.44 + i * 0.22, 1.0, hz + 0.31, false);
      this.mesh(new THREE.BoxGeometry(1.6, 0.08, 0.3), M.brass(), hx, 0.36, hz + 0.36, false);
      this.payoutCoin.position.set(hx, 1.95, hz + 0.31);
      const coin = new THREE.Mesh(new THREE.CylinderGeometry(0.24, 0.24, 0.05, 24), M.get('#F2C66D', { e: '#B45309', ei: 0.5, metal: 0.8, rough: 0.25 }));
      coin.rotation.x = Math.PI / 2;
      this.payoutCoin.add(coin);
      this.group.add(this.payoutCoin);
      for (let i = 0; i < 4; i++) this.payoutSlots.push(new THREE.Vector3(hx - 0.28 + (i % 2) * 0.56, 0.42, hz + 0.5 + Math.floor(i / 2) * 0.32));
    }
    // the rail: slips and strategy chips wait here
    const [rx, rz] = PROP.rail;
    const t = new THREE.Group();
    t.position.set(rx, 0, rz);
    this.mesh(new THREE.BoxGeometry(0.6, 0.08, 1.1), M.get(WOOD, { rough: 0.5 }), 0, 0.76, 0, true, t);
    this.mesh(new THREE.BoxGeometry(0.5, 0.02, 1.0), M.get('#4a1f36', { rough: 0.95 }), 0, 0.805, 0, false, t);
    for (const sx of [-1, 1]) for (const sz of [-1, 1]) this.mesh(new THREE.BoxGeometry(0.05, 0.74, 0.05), M.brass(), sx * 0.22, 0.37, sz * 0.47, false, t);
    this.group.add(t);
    for (let i = 0; i < 4; i++) this.railSlots.push(new THREE.Vector3(rx, 0.87, rz - 0.4 + i * 0.27));
  }

  // ── the cashier cage ──────────────────────────────────────────────────────

  private buildCage(): void {
    const M = this.mats;
    // the counter: wood below, marble on top, bars above where the windows are
    const z = CAGE_Z;
    const x0 = SALON_X1, x1 = FLOOR.x1;
    this.boxAt((x0 + x1) / 2, z, x1 - x0, 1.0, 0.3, WOOD, { rough: 0.55 });
    this.mesh(new THREE.BoxGeometry(x1 - x0, 0.05, 0.42), M.get('#d8cdb8', { rough: 0.3 }), (x0 + x1) / 2, 1.02, z + 0.04, false);
    // solid panels at either end, bars in between
    this.boxAt((x0 + 6.6) / 2, z, 6.6 - x0, 1.2, 0.12, '#2b1a12', { rough: 0.55 }, 1.05);
    this.boxAt((11.4 + x1) / 2, z, x1 - 11.4, 1.2, 0.12, '#2b1a12', { rough: 0.55 }, 1.05);
    for (let x = 6.6; x <= 11.4 + 1e-6; x += 0.12) {
      if (Math.abs(x - 7.6) < 0.32 || Math.abs(x - 10.4) < 0.32) continue; // the two windows
      this.mesh(new THREE.BoxGeometry(0.025, 1.2, 0.025), M.brass(), x, 1.65, z, false);
    }
    this.mesh(new THREE.BoxGeometry(x1 - x0, 0.06, 0.16), M.brass(), (x0 + x1) / 2, 2.27, z, false);
    for (const wx of [7.6, 10.4]) {
      this.mesh(new THREE.BoxGeometry(0.7, 0.05, 0.08), M.brass(), wx, 1.62, z, false);
      this.mesh(new THREE.BoxGeometry(0.5, 0.28, 0.02), M.get('#0d0a08', { e: '#F2C66D', ei: 0.35 }), wx, 1.9, z - 0.04, false);
    }
    this.sign('CASHIER', '#4ADE80', 9.0, 2.5, z + 0.02, 1.8, 0.42, 0, 'CHIPS · REFILLS');
    // the side wall of the cage, by the salon (staff go round behind it)
    this.wall(6.0, FLOOR.z0, 6.0, z, 2.3, '#2b1a12', 0.12);
    // the vault door on the back wall
    const [vx, vz] = PROP.vaultDoor;
    this.boxAt(vx, vz + 0.2, 2.3, 2.4, 0.3, '#2b2420', { metal: 0.2, rough: 0.4 });
    this.mesh(new THREE.BoxGeometry(2.4, 0.08, 0.36), M.brass(), vx, 2.42, vz + 0.2, false);
    this.vaultWheel.position.set(vx, 1.2, vz + 0.44);
    const door = new THREE.Mesh(new THREE.CylinderGeometry(1.0, 1.0, 0.22, 28), M.get('#d8b46a', { metal: 0.85, rough: 0.28 }));
    door.rotation.x = Math.PI / 2;
    door.castShadow = true;
    const hub = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.22, 0.2, 16), M.get('#3b2a14', { metal: 0.6, rough: 0.35 }));
    hub.rotation.x = Math.PI / 2;
    hub.position.z = 0.14;
    const bolts = new THREE.Mesh(new THREE.TorusGeometry(0.78, 0.05, 6, 28), M.get('#8a6a2c', { metal: 0.85, rough: 0.3 }));
    bolts.position.z = 0.12;
    this.vaultWheel.add(door, hub, bolts);
    for (let i = 0; i < 3; i++) {
      const spoke = new THREE.Mesh(new THREE.BoxGeometry(1.3, 0.07, 0.07), M.get('#3b2a14', { metal: 0.6, rough: 0.35 }));
      spoke.rotation.z = (i * Math.PI) / 3;
      spoke.position.z = 0.16;
      this.vaultWheel.add(spoke);
    }
    this.group.add(this.vaultWheel);
    // chips stacked round the coin pile
    for (const [dx, dz, c, n] of [[-1.5, 0.6, '#DC2626', 6], [-1.6, 0.0, '#16A34A', 4], [1.5, 0.7, '#1F2937', 5], [1.55, 0.1, '#2563EB', 7], [1.2, 1.1, '#F5F5F4', 3]] as const) {
      this.chipStack(PROP.pile[0] + dx, 0.03, PROP.pile[1] + dz, c, n);
    }
    // security: a console with monitors on the house, and the beacon
    {
      const [cx, cz] = PROP.security;
      const g = new THREE.Group();
      g.position.set(cx, 0, cz);
      g.rotation.y = -Math.PI / 2; // the screens face the guard (-x)
      this.mesh(new THREE.BoxGeometry(1.0, 0.74, 0.5), M.get(WOOD, { rough: 0.5 }), 0, 0.37, 0, true, g);
      this.mesh(new THREE.BoxGeometry(1.05, 0.04, 0.55), M.brass(), 0, 0.76, 0, false, g);
      for (const dx of [-0.24, 0.24]) {
        this.mesh(new THREE.BoxGeometry(0.44, 0.3, 0.05), M.get(LACQ), dx, 1.02, 0.12, false, g);
        const scr = this.mesh(new THREE.PlaneGeometry(0.4, 0.26), M.get('#120d22', { e: dx < 0 ? '#EF4444' : '#38BDF8', ei: 1.2 }), dx, 1.02, 0.09, false, g);
        scr.rotation.y = Math.PI;
      }
      this.group.add(g);
    }
    const [bx, bz] = PROP.beacon;
    this.mesh(new THREE.CylinderGeometry(0.05, 0.07, 2.0, 8), M.brass(), bx, 1.0, bz, false);
    this.beaconHead.position.set(bx, 2.08, bz);
    this.beaconMat = M.own('#7f1d1d', '#EF4444', 0.3);
    this.beaconHead.add(new THREE.Mesh(new THREE.SphereGeometry(0.2, 16, 10, 0, Math.PI * 2, 0, Math.PI / 2), this.beaconMat));
    for (let i = 0; i < 2; i++) {
      const b = new THREE.Mesh(
        new THREE.ConeGeometry(0.5, 2.6, 16, 1, true),
        new THREE.MeshBasicMaterial({ color: '#EF4444', transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }),
      );
      b.position.x = (i ? -1 : 1) * 1.3;
      b.rotation.z = i ? -Math.PI / 2 : Math.PI / 2;
      b.position.y = 0.1;
      this.beaconBeams.push(b);
      this.beaconHead.add(b);
    }
    this.group.add(this.beaconHead);
  }

  // ── the bar ───────────────────────────────────────────────────────────────

  private buildBar(): void {
    const M = this.mats;
    const [bx, bz] = PROP.bar;
    // the ring counter, with the bartenders' well inside
    const outer = this.mesh(new THREE.CylinderGeometry(BAR_R, BAR_R, 1.0, 40, 1, true), M.get(WOOD, { rough: 0.5 }), bx, 0.5, bz);
    (outer.material as THREE.MeshStandardMaterial).side = THREE.DoubleSide;
    const top = this.mesh(new THREE.RingGeometry(BAR_R - 0.42, BAR_R + 0.08, 40), M.get('#d8cdb8', { rough: 0.3 }), bx, 1.02, bz, false);
    top.rotation.x = -Math.PI / 2;
    const lip = this.mesh(new THREE.TorusGeometry(BAR_R + 0.06, 0.04, 6, 40), M.brass(), bx, 1.03, bz, false);
    lip.rotation.x = Math.PI / 2;
    this.barGlow = M.own('#1a1205', '#FBBF24', 1.4);
    const led = this.mesh(new THREE.TorusGeometry(BAR_R + 0.01, 0.025, 6, 40), this.barGlow, bx, 0.12, bz, false);
    led.rotation.x = Math.PI / 2;
    // the bottle tower: shelves of lit glass
    this.mesh(new THREE.CylinderGeometry(0.42, 0.48, 1.6, 20), M.get(LACQ, { rough: 0.3 }), bx, 0.8, bz);
    const bottle = new THREE.CylinderGeometry(0.035, 0.04, 0.2, 8);
    const cols = ['#F59E0B', '#4ADE80', '#F472B6', '#38BDF8', '#FDE68A'];
    for (const [y, r] of [[1.0, 0.46], [1.3, 0.44], [1.58, 0.42]] as const) {
      const shelf = this.mesh(new THREE.TorusGeometry(r, 0.025, 6, 24), M.brass(), bx, y - 0.1, bz, false);
      shelf.rotation.x = Math.PI / 2;
      for (let i = 0; i < 12; i++) {
        const a = (i / 12) * Math.PI * 2 + y;
        const c = cols[i % cols.length];
        this.mesh(bottle, M.get(c, { e: c, ei: 0.9, rough: 0.2 }), bx + Math.cos(a) * r, y, bz + Math.sin(a) * r, false);
      }
    }
    this.mesh(new THREE.CylinderGeometry(0.5, 0.42, 0.08, 20), M.brass(), bx, 1.64, bz, false);
    // the sign over it, two-faced, turning all the way round
    {
      const g = this.barSign;
      g.position.set(bx, 2.15, bz);
      this.mesh(new THREE.BoxGeometry(1.1, 0.42, 0.08), M.get(LACQ), 0, 0, 0, false, g);
      const t = this.tex(neonText('BAR', '#FBBF24', 1024, 390, 'COCKTAILS · ORDER HERE'));
      const face = new THREE.MeshBasicMaterial({ map: t, toneMapped: false });
      this.mesh(new THREE.PlaneGeometry(1.0, 0.38), face, 0, 0, 0.045, false, g);
      const back = this.mesh(new THREE.PlaneGeometry(1.0, 0.38), face, 0, 0, -0.045, false, g);
      back.rotation.y = Math.PI;
      this.mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.5, 6), M.brass(), 0, -0.45, 0, false, g);
      this.group.add(g);
    }
    for (const [x, z] of BAR_STOOLS) this.stool(x, z, '#4a1f36');
  }

  // ── the vip lounge ────────────────────────────────────────────────────────

  private buildLounge(): void {
    const M = this.mats;
    const [cx, cz] = PROP.podium;
    this.mesh(new THREE.CylinderGeometry(0.62, 0.66, 0.08, 28), M.brass(), cx, 0.07, cz);
    this.mesh(new THREE.CylinderGeometry(0.56, 0.56, 0.04, 28), M.get('#3b0d1c', { rough: 0.9 }), cx, 0.12, cz, false);
    this.boxAt(cx, cz, 0.2, 0.3, 0.2, LACQ);
    this.boxAt(cx, cz, 0.72, 0.5, 0.72, VELVET, { rough: 0.85 });
    this.boxAt(cx + 0.4, cz, 0.16, 1.25, 0.82, VELVET, { rough: 0.85 });
    this.mesh(new THREE.BoxGeometry(0.2, 0.08, 0.86), M.brass(), cx + 0.4, 1.28, cz, false);
    const [hx, hz] = PROP.lectern;
    this.boxAt(hx, hz, 0.55, 0.95, 1.4, WOOD, { rough: 0.5 });
    const lid = this.mesh(new THREE.BoxGeometry(0.62, 0.05, 1.5), M.brass(), hx, 0.97, hz, false);
    lid.rotation.z = -0.2;
    this.bankerLamp(hx - 0.05, hz + 0.45, 1.0);
    // your agents' spots: a gold ring on the marble for each
    for (const id of AGENT_DOCKS) {
      const nd = NAV_NODES.find((n) => n.id === id)!;
      const r = this.mesh(new THREE.TorusGeometry(0.32, 0.025, 6, 28), M.get('#F2C66D', { e: '#F59E0B', ei: 1.3, metal: 0.6, rough: 0.3 }), nd.x, 0.045, nd.z, false);
      r.rotation.x = Math.PI / 2;
    }
    // sofas round low tables
    for (const [x, z, face] of LOUNGE_SOFAS) {
      const g = new THREE.Group();
      g.position.set(x, 0, z);
      g.rotation.y = face === 0 ? Math.PI : face;
      const m = M.get('#4c1d95', { rough: 0.85 });
      this.mesh(new THREE.BoxGeometry(1.6, 0.4, 0.75), m, 0, 0.2, 0, true, g);
      this.mesh(new THREE.BoxGeometry(1.6, 0.6, 0.18), m, 0, 0.6, -0.3, true, g);
      for (const s of [-1, 1]) this.mesh(new THREE.BoxGeometry(0.14, 0.3, 0.72), m, s * 0.8, 0.5, 0, true, g);
      this.mesh(new THREE.BoxGeometry(1.64, 0.04, 0.2), M.brass(), 0, 0.91, -0.3, false, g);
      this.mesh(new THREE.BoxGeometry(0.9, 0.04, 0.45), M.get('#d8cdb8', { rough: 0.3 }), 0, 0.45, 0.85, false, g);
      this.mesh(new THREE.CylinderGeometry(0.05, 0.08, 0.42, 8), M.brass(), 0, 0.21, 0.85, false, g);
      this.group.add(g);
    }
    // the velvet rope at the lounge's mouth
    for (const x of [7.0, 8.4]) {
      this.mesh(new THREE.CylinderGeometry(0.12, 0.14, 0.05, 12), M.brass(), x, 0.05, 3.95, false);
      this.mesh(new THREE.CylinderGeometry(0.03, 0.03, 0.85, 8), M.brass(), x, 0.48, 3.95, false);
      this.mesh(new THREE.SphereGeometry(0.06, 10, 8), M.brass(), x, 0.93, 3.95, false);
    }
    const rope = this.mesh(new THREE.CylinderGeometry(0.025, 0.025, 1.4, 8), M.get('#991B1B', { rough: 0.7 }), 7.7, 0.82, 3.95, false);
    rope.rotation.z = Math.PI / 2;
    for (const [x, z] of PALMS) this.palm(x, z);
    // the pit boss stands at his chair: a rat (CasinoEngine, crew.ts RatFigure)
  }

  // ── the way in ────────────────────────────────────────────────────────────

  private buildEntrance(): void {
    const M = this.mats;
    const x = FLOOR.x0 - 0.1 + 0.13;
    const g = this.overviewOnly;
    g.add(at(new THREE.Mesh(new THREE.BoxGeometry(0.06, 2.2, 1.5), M.get('#120b08', { rough: 0.4 })), x + 0.02, 1.1, 0));
    for (const s of [-1, 1]) {
      g.add(at(new THREE.Mesh(new THREE.BoxGeometry(0.04, 2.0, 0.66), M.get('#4a2a18', { rough: 0.45 })), x + 0.05, 1.0, s * 0.36));
      g.add(at(new THREE.Mesh(new THREE.BoxGeometry(0.03, 0.4, 0.03), M.brass()), x + 0.08, 1.05, s * 0.1));
    }
    g.add(at(new THREE.Mesh(new THREE.BoxGeometry(0.1, 2.35, 1.7), M.brass()), x - 0.02, 1.175, 0));
    g.add(at(new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.16, 0.5), M.get('#06200f', { e: '#4ADE80', ei: 2.2 })), x + 0.06, 2.42, 0));
    this.group.add(g);
    // the hall's name over the doors, facing in
    this.sign('KRYPT CASINO', '#FDE68A', x + 0.1, 2.58, 0, 3.0, 0.4, Math.PI / 2);
  }

  private buildNight(): void {
    const N = 2000;
    const pos = new Float32Array(N * 3);
    const size = new Float32Array(N);
    const tint = new Float32Array(N);
    for (let i = 0; i < N; i++) {
      pos[i * 3] = (Math.random() - 0.5) * 190;
      pos[i * 3 + 1] = -9 - Math.random() * 44;
      pos[i * 3 + 2] = (Math.random() - 0.5) * 190;
      size[i] = Math.random() < 0.12 ? 6 + Math.random() * 9 : 1.5 + Math.random() * 2.6;
      tint[i] = Math.random();
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    g.setAttribute('size', new THREE.BufferAttribute(size, 1));
    g.setAttribute('tint', new THREE.BufferAttribute(tint, 1));
    this.bokehMat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      uniforms: { uTime: { value: 0 }, uPx: { value: 1 } },
      vertexShader: /* glsl */`
        attribute float size; attribute float tint;
        uniform float uTime; uniform float uPx;
        varying float vT; varying float vA;
        void main() {
          vT = tint;
          vA = 0.35 + 0.25 * sin(uTime * (0.3 + tint * 0.8) + tint * 40.0);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = size * uPx;
        }`,
      fragmentShader: /* glsl */`
        varying float vT; varying float vA;
        void main() {
          vec2 c = gl_PointCoord - 0.5;
          float d = length(c);
          float a = smoothstep(0.5, 0.2, d) * 0.8 + smoothstep(0.5, 0.0, d) * 0.2;
          vec3 gold = vec3(1.0, 0.72, 0.35);
          vec3 rose = vec3(1.0, 0.42, 0.62);
          vec3 cyan = vec3(0.45, 0.8, 1.0);
          vec3 col = vT < 0.6 ? gold : (vT < 0.85 ? rose : cyan);
          gl_FragColor = vec4(col * a * vA, a * vA);
        }`,
    });
    this.bokeh = new THREE.Points(g, this.bokehMat);
    this.bokeh.frustumCulled = false;
    this.night.add(this.bokeh);
    for (const [x, y, z, c, s, o] of [
      [-30, -40, -10, '#7a2a52', 60, 0.2], [25, -45, 20, '#8a5a1a', 55, 0.16], [5, -50, -35, '#3b1f5e', 70, 0.22],
    ] as const) {
      const haze = this.kit.glow(c, s, o);
      haze.position.set(x, y, z);
      this.night.add(haze);
    }
    this.buildTeller();
  }

  /** POLYMARKET: a neon teller window across the night that every real bet
   *  flies to, and every real win flies back from. It faces the camera. */
  private buildTeller(): void {
    const M = this.mats;
    const s = this.teller;
    s.position.copy(this.tellerPos);
    s.rotation.y = Math.PI / 4;
    s.add(new THREE.Mesh(new THREE.BoxGeometry(3.4, 2.4, 0.5), M.get('#1a1020', { rough: 0.6 })));
    s.add(at(new THREE.Mesh(new THREE.BoxGeometry(3.8, 0.3, 0.9), M.get('#24160f', { rough: 0.5 })), 0, -1.35, 0.1));
    s.add(at(new THREE.Mesh(new THREE.BoxGeometry(4.0, 0.06, 1.0), M.get('#F2C66D', { e: '#F59E0B', ei: 1.4, metal: 0.6 })), 0, -1.52, 0.1));
    const arch = new THREE.Shape();
    arch.moveTo(-0.85, -0.95);
    arch.lineTo(0.85, -0.95);
    arch.lineTo(0.85, 0.25);
    arch.absarc(0, 0.25, 0.85, 0, Math.PI, false);
    arch.lineTo(-0.85, -0.95);
    this.tellerGlow = M.own('#0b1e5b', '#3B82F6', 1.6);
    s.add(at(new THREE.Mesh(new THREE.ShapeGeometry(arch, 16), this.tellerGlow), 0, -0.05, 0.26));
    const frame = new THREE.Shape();
    frame.moveTo(-1.0, -1.05);
    frame.lineTo(1.0, -1.05);
    frame.lineTo(1.0, 0.25);
    frame.absarc(0, 0.25, 1.0, 0, Math.PI, false);
    frame.lineTo(-1.0, -1.05);
    frame.holes.push(arch as unknown as THREE.Path);
    s.add(at(new THREE.Mesh(new THREE.ShapeGeometry(frame, 16), M.brass()), 0, -0.05, 0.255));
    for (let i = 0; i < 5; i++) s.add(at(new THREE.Mesh(new THREE.BoxGeometry(0.03, 1.9, 0.03), M.brass()), -0.56 + i * 0.28, -0.05, 0.28));
    s.add(at(new THREE.Mesh(new THREE.BoxGeometry(2.1, 0.1, 0.4), M.brass()), 0, -0.98, 0.42));
    const name = this.tex(neonText('POLYMARKET', '#93C5FD', 1024, 192));
    s.add(at(new THREE.Mesh(new THREE.BoxGeometry(3.8, 0.86, 0.3), M.get('#0d0a14', { rough: 0.5 })), 0, 1.65, 0.05));
    s.add(at(new THREE.Mesh(new THREE.PlaneGeometry(3.5, 0.66), new THREE.MeshBasicMaterial({ map: name, toneMapped: false })), 0, 1.65, 0.21));
    this.bulbsA = M.own('#fff1c2', '#FFC56E', 2.4);
    this.bulbsB = M.own('#fff1c2', '#FFC56E', 0.4);
    const bulb = new THREE.SphereGeometry(0.045, 8, 6);
    const spots: [number, number][] = [];
    for (let i = 0; i <= 16; i++) for (const y of [1.2, 2.1]) spots.push([-1.85 + (3.7 * i) / 16, y]);
    const m4 = new THREE.Matrix4();
    for (const [mat, odd] of [[this.bulbsA, 1], [this.bulbsB, 0]] as const) {
      const mine = spots.filter((_, i) => i % 2 === odd);
      const im = new THREE.InstancedMesh(bulb, mat, mine.length);
      mine.forEach(([x, y], i) => im.setMatrixAt(i, m4.makeTranslation(x, y, 0.22)));
      s.add(im);
    }
    const halo = this.kit.glow('#3B82F6', 5, 0.16);
    halo.position.set(0, 0.4, -0.6);
    s.add(halo);
    this.night.add(s);
    mergeStatic(s, [], () => true);
  }

  // ── screen painters ──────────────────────────────────────────────────────

  private drawDisplay(g: CanvasRenderingContext2D, w: number, h: number): void {
    g.clearRect(0, 0, w, h);
    g.fillStyle = 'rgba(45,212,191,0.10)';
    g.fillRect(4, 4, w - 8, h - 8);
    g.strokeStyle = 'rgba(242,198,109,0.85)';
    g.lineWidth = 5;
    g.strokeRect(4, 4, w - 8, h - 8);
    if (this.series.length < 2) {
      g.fillStyle = '#5EEAD4';
      g.font = '700 30px "Chakra Petch", sans-serif';
      g.fillText(this.displayLabel, 16, 38);
      g.font = '600 20px "JetBrains Mono", monospace';
      g.fillText(this.displaySub, 16, h / 2 + 20);
      return;
    }
    g.strokeStyle = 'rgba(45,212,191,0.18)';
    for (let i = 1; i < 4; i++) {
      g.beginPath();
      g.moveTo(4, (h * i) / 4);
      g.lineTo(w - 4, (h * i) / 4);
      g.stroke();
    }
    const s = this.series;
    let lo = Infinity, hi = -Infinity;
    for (const v of s) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const span = Math.max(1e-6, hi - lo);
    const n = Math.max(2, Math.floor(s.length * this.progress));
    g.strokeStyle = this.verdict === null ? '#5EEAD4' : this.verdict ? '#4ADE80' : '#F87171';
    g.lineWidth = 9;
    g.lineJoin = 'round';
    g.beginPath();
    for (let i = 0; i < n; i++) {
      const x = 14 + (i / (s.length - 1)) * (w - 28);
      const y = h - 18 - ((s[i] - lo) / span) * (h - 50);
      if (i === 0) g.moveTo(x, y); else g.lineTo(x, y);
    }
    g.stroke();
    if (this.replaying) {
      const x = 14 + ((n - 1) / (s.length - 1)) * (w - 28);
      g.fillStyle = 'rgba(94,234,212,0.25)';
      g.fillRect(x - 3, 8, 6, h - 16);
    }
    g.fillStyle = '#99F6E4';
    g.font = '700 30px "Chakra Petch", sans-serif';
    g.fillText(this.displayLabel, 16, 38);
  }

  private drawBets(g: CanvasRenderingContext2D, w: number, h: number): void {
    g.fillStyle = '#12091c';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#F472B6';
    g.font = '700 22px "Chakra Petch", sans-serif';
    g.fillText('BETS →  POLYMARKET', 12, 28);
    g.font = '500 19px "JetBrains Mono", monospace';
    if (!this.betLines.length) {
      g.fillStyle = 'rgba(255,255,255,0.4)';
      g.fillText('no real orders yet', 12, 70);
    }
    this.betLines.slice(-5).forEach((l, i, arr) => {
      g.fillStyle = l.color;
      g.globalAlpha = 0.45 + 0.55 * ((i + 1) / arr.length);
      g.fillText(l.text, 12, 56 + i * 24);
    });
    g.globalAlpha = 1;
  }

  // ── pokes from the director ───────────────────────────────────────────────

  /** A real signal came in: a row on the board lights in its colour. */
  oddsPing(color: string): void {
    this.oddsPingT = 1;
    this.kioskTip.emissive.set(color);
    const row = Math.floor(Math.random() * Math.max(1, Math.min(this.boardRows.length, 7 * SB_PER_MONITOR)));
    for (let i = this.blips.length - 1; i >= 0; i--) if (this.blips[i].row === row) this.blips.splice(i, 1);
    this.blips.push({ row, color, age: 0 });
    if (this.blips.length > 8) this.blips.shift();
  }

  ticketPrint(): void { this.printerFlash = 1; }
  dealSnap(): void { this.dealFlash = 1; }
  discardPuff(): void { this.discardFlash = 1; }
  slideBet(): void { this.chuteFlash = 1; }
  payoutBlink(): void { this.payoutFlash = 1; }
  tellerHit(): void { this.tellerFlash = 1; }
  cageDeposit(big = false): void { this.vaultSpin = big ? 6 : 1.6; this.cageFlash = big ? 2 : 1; }
  /** The wheel spins up while the optimiser's result is being staged (a
   *  game spin in progress keeps the wheel; it only speeds the marquee). */
  setSpinning(on: boolean): void { this.wheelTarget = on ? 9 : 0.35; }

  postBet(text: string, color: string): void {
    this.betLines.push({ text, color });
    if (this.betLines.length > 8) this.betLines.shift();
  }

  setPracticeIdle(label: string, sub: string): void {
    if (this.replaying) return;
    this.series = [];
    this.verdict = null;
    this.displayLabel = label;
    this.displaySub = sub;
    this.display.update(0, 0, true);
  }

  startPractice(series: number[], label: string, sub = ''): void {
    this.series = series;
    this.displaySub = sub;
    this.progress = 0;
    this.verdict = null;
    this.replaying = true;
    this.displayLabel = label;
    this.candle.emissive.set('#5EEAD4');
    this.candle.emissiveIntensity = 1;
  }

  finishPractice(pass: boolean | null, label: string): void {
    this.progress = 1;
    this.replaying = false;
    this.verdict = pass;
    this.displayLabel = label;
    this.candle.emissive.set(pass === null ? '#5EEAD4' : pass ? '#22C55E' : '#EF4444');
    this.candle.emissiveIntensity = 3;
    this.display.update(0, 0, true);
  }

  get practiceRunning(): boolean { return this.replaying; }

  // ── the games ─────────────────────────────────────────────────────────────

  /** The sportsbook board: real rows, when they were read, or why none. */
  setBoard(rows: BoardRow[], at: number | null, note: string | null): void {
    this.boardRows = rows;
    this.boardAt = at;
    this.boardNote = note;
    for (const m of this.monitors) m.update(0, 0, true);
  }

  get rouletteBusy(): boolean { return this.play !== null; }

  /** Your sportsbook tickets, for the YOUR BETS monitor. */
  setTickets(t: readonly Ticket[]): void {
    this.tickets = t;
  }

  /** Spin for real money's opposite — play chips: the ball runs `dur`
   *  seconds and drops into `n`'s pocket. */
  spinRoulette(n: number, dur = 5.5): void {
    const idx = pocketIndex(n);
    const pocketA = ((idx + 0.5) / WHEEL_ORDER.length) * Math.PI * 2;
    // the wheel coasts from its speed now to a slow drift; where it will be
    // at the end is known, so the ball's landing angle is too
    const v0 = Math.max(this.wheelSpeed, 2.2);
    const vEnd = 0.35;
    const wheelEnd = this.wheelAngle + ((v0 + vEnd) / 2) * dur;
    const land = pocketA - wheelEnd;
    // the ball runs the other way: several laps more, ending on `land`
    const from = this.ballAngle;
    let to = land;
    while (to > from - Math.PI * 10) to -= Math.PI * 2;
    this.play = { t: 0, dur, n, from, to, wheelV0: v0 };
    this.ballIn = null;
    this.wheelSpeed = v0;
  }

  /** A slot machine's reels: each stops on its result, left to right. */
  spinSlot(i: number, stops: readonly [number, number, number], dur = 2.2): void {
    const r = this.reels[i];
    if (!r) return;
    const to: [number, number, number] = [0, 0, 0];
    const ends: [number, number, number] = [0, 0, 0];
    for (let k = 0; k < 3; k++) {
      let target = STOP_ANGLE(stops[k]);
      // forward several turns
      while (target < r.angle[k] + Math.PI * 2 * (3 + k)) target += Math.PI * 2;
      to[k] = target;
      ends[k] = dur * (0.55 + 0.225 * k);
    }
    r.spin = { from: [...r.angle] as [number, number, number], to, t: 0, ends };
  }

  slotWin(i: number): void {
    const r = this.reels[i];
    if (r) r.flash = 3;
  }

  get slotCount(): number { return SLOTS.length; }

  /** Lay a blackjack hand on table `t`'s felt: the dealer's cards at the
   *  flat edge, the player's in front of the middle seats. A null card is
   *  face down. Empty arrays clear the table. */
  setBjCards(t: number, dealer: (Card | null)[], hands: Card[][]): void {
    for (const m of this.cardsOut[t] ?? []) {
      m.visible = false;
    }
    this.cardsOut[t] = [];
    const [tx, tz] = BJ_TABLES[t];
    const put = (c: Card | null, x: number, z: number, rot: number) => {
      const m = this.cardPool.find((p) => !p.visible);
      if (!m) return;
      const [u0, v0, u1, v1] = cardUV(c);
      const uv = m.geometry.attributes.uv as THREE.BufferAttribute;
      // PlaneGeometry's corners, in its own order: (0,1) (1,1) (0,0) (1,0)
      uv.setXY(0, u0, v1); uv.setXY(1, u1, v1); uv.setXY(2, u0, v0); uv.setXY(3, u1, v0);
      uv.needsUpdate = true;
      m.position.set(x, 0.8 + this.cardsOut[t].length * 0.0012, z);
      m.rotation.set(0, rot, 0);
      m.visible = true;
      this.cardsOut[t].push(m);
    };
    dealer.forEach((c, i) => put(c, tx - 0.12 * (dealer.length - 1) / 2 + i * 0.12, tz + 0.37, 0));
    hands.forEach((h, k) => {
      const cx = tx + BJ_HAND_X(hands.length, k);
      h.forEach((c, i) => put(c, cx - 0.04 + i * 0.05, tz + 0.6 - i * 0.03, 0.08 * (i - 1)));
    });
  }

  setPixelScale(s: number): void {
    this.bokehMat.uniforms.uPx.value = s;
  }

  update(dt: number, t: number): void {
    this.bokehMat.uniforms.uTime.value = t;

    // sportsbook
    this.kioskHead.rotation.y += dt * 0.35;
    this.oddsPingT = Math.max(0, this.oddsPingT - dt * 1.6);
    this.kioskTip.emissiveIntensity = 1.2 + this.oddsPingT * 6 + Math.max(0, Math.sin(t * 4)) * 0.6;
    for (const b of this.blips) b.age += dt;
    for (const m of this.monitors) m.update(dt, t);
    this.printerFlash = Math.max(0, this.printerFlash - dt * 2);
    this.printerLight.emissiveIntensity = 0.6 + this.printerFlash * 4;

    // blackjack
    this.dealFlash = Math.max(0, this.dealFlash - dt * 4);
    this.dealCard.emissiveIntensity = this.dealFlash * 2.5;
    this.shuffleLight.emissiveIntensity = 1.1 + Math.sin(t * 2.2) * 0.25 + this.dealFlash * 1.5;

    // practice table
    if (this.replaying) this.progress = Math.min(1, this.progress + dt / 2.4);
    this.display.update(dt, t, false);
    this.candle.emissiveIntensity = Math.max(this.replaying ? 1 + Math.sin(t * 14) * 0.6 : 0.5, this.candle.emissiveIntensity - dt * 0.6);
    this.discardFlash = Math.max(0, this.discardFlash - dt * 1.5);
    this.discardRing.emissiveIntensity = 0.6 + this.discardFlash * 4;
    (this.displayMesh.material as THREE.MeshBasicMaterial).opacity = 0.82 + Math.sin(t * 9) * 0.05;

    // slot machines
    let topperDirty = false;
    this.reels.forEach((r, i) => {
      if (r.spin) {
        r.spin.t += dt;
        let done = true;
        for (let k = 0; k < 3; k++) {
          const p = Math.min(1, r.spin.t / r.spin.ends[k]);
          // fast, then a firm stop with the faintest overshoot
          const e = p < 1 ? 1 - Math.pow(1 - p, 3) : 1;
          r.angle[k] = r.spin.from[k] + (r.spin.to[k] - r.spin.from[k]) * e;
          if (p < 1) done = false;
        }
        if (done) {
          for (let k = 0; k < 3; k++) r.angle[k] = r.spin.to[k] % (Math.PI * 2);
          r.spin = null;
          this.writeReels(true);
        }
      } else if (r.free && this.replaying) {
        for (let k = 0; k < 3; k++) r.angle[k] += dt * (12 + k * 2);
      }
      if (r.flash > 0) {
        r.flash = Math.max(0, r.flash - dt);
        const on = Math.floor(r.flash * 8) % 2 === 0;
        this.toppers.setColorAt(i, this._c.set(on ? '#ffffff' : TOPPER_COLORS[i % TOPPER_COLORS.length]).multiplyScalar(on ? 1.6 : 0.8));
        topperDirty = true;
      }
    });
    if (this.reels.some((r) => r.spin || (r.free && this.replaying))) this.writeReels(false);
    if (this.leverPull.some((k) => k > 0)) {
      this.leverPull = this.leverPull.map((k) => (k > 0 ? (k + dt * 2.2 >= 1 ? 0 : k + dt * 2.2) : 0));
      this.writeLevers();
    }
    if (topperDirty && this.toppers.instanceColor) this.toppers.instanceColor.needsUpdate = true;

    // high-limit salon
    this.chuteFlash = Math.max(0, this.chuteFlash - dt * 3);
    this.chuteRing.emissiveIntensity = 1.2 + this.chuteFlash * 5;
    this.payoutFlash = Math.max(0, this.payoutFlash - dt * 1.4);
    this.payoutLight.emissiveIntensity = 0.5 + this.payoutFlash * 4 * (0.6 + 0.4 * Math.sin(t * 20));
    this.payoutCoin.rotation.y += dt * (0.5 + this.payoutFlash * 9);
    this.limitSigns.forEach((m, i) => { m.emissiveIntensity = 1.0 + Math.sin(t * (2 + i)) * 0.2 + this.chuteFlash * 1.2; });
    this.bets.update(dt, t);

    // cashier cage
    this.vaultSpin = Math.max(0, this.vaultSpin - dt * 1.2);
    this.vaultWheel.rotation.z -= dt * this.vaultSpin * 2.2;
    this.cageFlash = Math.max(0, this.cageFlash - dt * 1.2);
    this.cagePool.opacity = 0.45 + this.cageFlash * 0.4;
    this.alarm = Math.max(0, this.alarm - dt);
    const al = Math.min(1, this.alarm);
    this.beaconHead.rotation.y += dt * (al > 0 ? 9 : 0.4);
    this.beaconMat.emissiveIntensity = 0.3 + al * (3 + Math.sin(t * 18) * 1.5);
    for (const b of this.beaconBeams) (b.material as THREE.MeshBasicMaterial).opacity = al * 0.22;
    this.alarmPool.opacity = al * (0.5 + Math.sin(t * 18) * 0.3);

    // roulette
    const p = this.play;
    if (p) {
      p.t += dt;
      const k = Math.min(1, p.t / p.dur);
      // the wheel coasts linearly down to a drift
      this.wheelSpeed = p.wheelV0 + (0.35 - p.wheelV0) * k;
      this.wheelAngle += dt * this.wheelSpeed;
      const e = 1 - Math.pow(1 - k, 2.4);
      this.ballAngle = p.from + (p.to - p.from) * e;
      // the ball drops from the track into the pockets over the last third
      const drop = Math.min(1, Math.max(0, (k - 0.62) / 0.3));
      const bounce = drop > 0 && drop < 1 ? Math.abs(Math.sin(drop * Math.PI * 3)) * 0.03 * (1 - drop) : 0;
      this.ball.position.set(0.66 - drop * 0.17, bounce, 0);
      if (k >= 1) {
        this.ballIn = p.n;
        this.lastNumbers.push(p.n);
        if (this.lastNumbers.length > 20) this.lastNumbers.shift();
        this.marqueeFlash = 1;
        this.marquee.update(0, t, true);
        this.play = null;
        this.wheelTarget = 0.35;
      }
    } else {
      this.wheelSpeed += (this.wheelTarget - this.wheelSpeed) * Math.min(1, dt * 1.4);
      this.wheelAngle += dt * this.wheelSpeed;
      if (this.ballIn !== null && this.wheelTarget <= 0.35) {
        // the ball sits in its pocket and rides round with the wheel
        const pocketA = ((pocketIndex(this.ballIn) + 0.5) / WHEEL_ORDER.length) * Math.PI * 2;
        this.ballAngle = pocketA - this.wheelAngle;
        this.ball.position.set(0.49, 0, 0);
      } else {
        this.ballIn = null;
        this.ballAngle -= dt * this.wheelSpeed * 1.6;
        this.ball.position.set(0.66, 0, 0);
      }
    }
    this.wheelHead.rotation.y = this.wheelAngle;
    this.ballGroup.rotation.y = -this.ballAngle;
    this.marqueeFlash = Math.max(0, this.marqueeFlash - dt * 1.5);
    this.marquee.update(dt, t);

    // the bar
    this.barGlow.emissiveIntensity = 1.2 + Math.sin(t * 1.3) * 0.3;
    this.barSign.rotation.y += dt * 0.7;

    // vip lounge
    this.blueyHop = Math.max(0, this.blueyHop - dt);

    // POLYMARKET
    this.teller.position.y = this.tellerPos.y + Math.sin(t * 0.8) * 0.12;
    this.tellerFlash = Math.max(0, this.tellerFlash - dt * 2.5);
    this.tellerGlow.emissiveIntensity = 1.5 + this.tellerFlash * 4;
    const chase = Math.floor(t * 4) % 2 === 0;
    this.bulbsA.emissiveIntensity = chase ? 2.6 : 0.4 + this.tellerFlash * 2;
    this.bulbsB.emissiveIntensity = chase ? 0.4 + this.tellerFlash * 2 : 2.6;
  }

  dispose(): void {
    for (const t of this.textures) t.dispose();
  }
}
