import * as THREE from 'three';
import spriteUrl from '../../assets/spritebluey.png';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { mergeStatic } from '../engine/bake';
import { CHIP, CHIP_H, ChipField, ChipKit } from './chips';
import { MEMBER_IDS, type MemberId } from './roster';
import type { ScoreView } from './seats';
import type { WalkSpace } from '../walk';

// The private card room: walnut panelling and oxblood leather, a deep-teal
// felt oval under ONE warm lamp, and everything else falling away into the
// dark. The light is the composition: a pool on the felt, the players lit
// from above and a little from the front, the room only glimpsed (sconces,
// a backlit bar, the leaderboard). Static geometry is merged (engine/bake),
// every chip is one instanced mesh (chips.ts), one light casts a shadow.

export const TABLE_Y = 0.95;
/** The felt's half-axes: long along x, short along z. */
export const FELT_A = 2.72;
export const FELT_B = 1.4;
/** The padded rail's centre line and its tube radius. */
const RAIL_A = 3.12;
const RAIL_B = 1.79;
const RAIL_R = 0.17;
/** The table's short half-width to the outside of the rail (walk tests). */
export const TABLE_R = RAIL_B + RAIL_R;
/** The players stand on a wider ellipse round the rail. */
const SEAT_A = 3.95;
const SEAT_B = 2.62;
/** Six seats on the back arc, three each side of the dealer, so every face
 *  points at the camera and the front of the table stays open like a stage.
 *  Degrees on the seat ellipse, 0 = +x, 90 = +z (the camera's side). */
const SEAT_DEG = [195, 220, 245, 295, 320, 345];
/** A chair's centre, past its player on the seat ellipse: the player sits on
 *  the front of the cushion with the backrest behind them, away from the felt. */
const CHAIR_OUT = 0.32;
const CHAIR_BACK_Z = -0.38;
/** The dealer stands at the head of the table, facing the camera. */
const DEALER_POS = new THREE.Vector3(0, 0, -2.45);
const BLUEY_SCALE = 1.75;
const BLUEY_Y = 1.78;

/** The padded leather doors on the back wall: the way out on foot. */
const DOOR_X = -6.35;
const BACK_Z = -6.5;

const rad = (d: number) => (d * Math.PI) / 180;

export function seatOf(i: number): [number, number] {
  const a = rad(SEAT_DEG[i]);
  return [Math.cos(a) * SEAT_A, Math.sin(a) * SEAT_B];
}

/** A point on the felt toward seat `i`: `k` of the way from the centre to
 *  the felt's edge. */
function feltToward(i: number, k: number): [number, number] {
  const a = rad(SEAT_DEG[i]);
  return [Math.cos(a) * FELT_A * k, Math.sin(a) * FELT_B * k];
}

/** The room on foot (walk.ts): the walls as drawn, the front wall that
 *  walking shows, and the table (an oval as five circles), the six seats and
 *  their chairs, the dealer, the bar and the lounge as solid. The walk starts
 *  by the front wall, facing the table. */
export const POKER_WALK: WalkSpace = {
  wallT: 0.2,
  walls: [
    [-8.8, -6.35, 8.8, -6.35],
    [-8.7, -6.35, -8.7, 8.6],
    [8.7, -6.35, 8.7, 8.6],
    [-8.8, 8.6, 8.8, 8.6],
  ],
  props: [
    [0, 0, 1.97],
    [-1.3, 0, 1.87], [1.3, 0, 1.87],
    [-2.35, 0, 1.5], [2.35, 0, 1.5],
    [-2.95, 0, 1.0], [2.95, 0, 1.0],
    ...SEAT_DEG.map((_, i): [number, number, number] => [...seatOf(i), 0.55]),
    ...SEAT_DEG.map((d): [number, number, number] => [Math.cos(rad(d)) * (SEAT_A + CHAIR_OUT + 0.2), Math.sin(rad(d)) * (SEAT_B + CHAIR_OUT + 0.2), 0.5]),
    [DEALER_POS.x, DEALER_POS.z, 0.55],
    // the bar along the right wall, and its stools
    [8.0, -5.2, 0.6], [8.0, -4.2, 0.6], [8.0, -3.2, 0.6], [8.0, -2.2, 0.6],
    [7.1, -4.6, 0.25], [7.1, -3.5, 0.25], [7.1, -2.4, 0.25],
    // the lounge, front left: sofa and lamp table
    [-8.1, 4.6, 0.55], [-8.1, 5.6, 0.55], [-8.1, 6.6, 0.55], [-8.2, 3.5, 0.32],
    // the palm, front right
    [8.0, 6.6, 0.45],
  ],
  start: { x: 0, z: 7.4, yaw: 0, pitch: -0.12 },
  eye: 1.65,
  speed: 2.4,
  exits: [[DOOR_X - 1.6, -6.35, DOOR_X + 1.6, -6.35]],
};

function canvasTex(w: number, h: number, draw: (g: CanvasRenderingContext2D, w: number, h: number) => void, repeat?: [number, number]): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d')!, w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  if (repeat) {
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.repeat.set(repeat[0], repeat[1]);
  }
  return t;
}

/** A small seeded random, so the room is the same room every time. */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function grain(g: CanvasRenderingContext2D, w: number, h: number, n: number, a: number, r: () => number): void {
  for (let i = 0; i < n; i++) {
    g.fillStyle = r() < 0.5 ? `rgba(0,0,0,${a * r()})` : `rgba(255,236,210,${a * 0.6 * r()})`;
    g.fillRect(r() * w, r() * h, 1 + r() * 2, 1 + r() * 2);
  }
}

/** Long dark-oak boards, staggered joints, a satin finish. */
function planks(g: CanvasRenderingContext2D, w: number, h: number): void {
  const r = rng(7);
  g.fillStyle = '#1e1009';
  g.fillRect(0, 0, w, h);
  const bw = 32;
  for (let y = 0; y < h; y += bw) {
    let x = -Math.floor(r() * 300);
    while (x < w) {
      const len = 170 + Math.floor(r() * 220);
      const l = 18 + r() * 22;
      g.fillStyle = `rgb(${52 + l},${28 + l * 0.55},${14 + l * 0.25})`;
      g.fillRect(x + 1, y + 1, len - 2, bw - 2);
      // grain: long faint streaks along the board
      for (let k = 0; k < 5; k++) {
        g.strokeStyle = `rgba(0,0,0,${0.06 + r() * 0.08})`;
        g.lineWidth = 1;
        const yy = y + 3 + r() * (bw - 6);
        g.beginPath(); g.moveTo(x + 2, yy); g.bezierCurveTo(x + len * 0.3, yy + (r() - 0.5) * 4, x + len * 0.7, yy + (r() - 0.5) * 4, x + len - 2, yy); g.stroke();
      }
      g.fillStyle = 'rgba(0,0,0,0.5)';
      g.fillRect(x, y, 2, bw);
      x += len;
    }
    g.fillStyle = 'rgba(0,0,0,0.55)';
    g.fillRect(0, y, w, 1.5);
  }
  grain(g, w, h, 1200, 0.06, r);
}

/** Raised walnut panels for the wainscot. */
function wainscot(g: CanvasRenderingContext2D, w: number, h: number): void {
  const r = rng(11);
  const grd = g.createLinearGradient(0, 0, 0, h);
  grd.addColorStop(0, '#3a2112');
  grd.addColorStop(1, '#24140a');
  g.fillStyle = grd;
  g.fillRect(0, 0, w, h);
  for (let x = 0; x < w; x += 4) {
    g.fillStyle = `rgba(0,0,0,${0.05 + r() * 0.08})`;
    g.fillRect(x, 0, 2, h);
  }
  // one raised panel per tile
  const m = 26;
  g.fillStyle = 'rgba(255,200,140,0.05)';
  g.fillRect(m, m, w - m * 2, h - m * 2);
  g.strokeStyle = 'rgba(0,0,0,0.55)';
  g.lineWidth = 6;
  g.strokeRect(m, m, w - m * 2, h - m * 2);
  g.strokeStyle = 'rgba(255,210,150,0.16)';
  g.lineWidth = 2;
  g.strokeRect(m - 5, m - 5, w - m * 2 + 10, h - m * 2 + 10);
  grain(g, w, h, 600, 0.08, r);
}

/** Deep green damask above the wainscot. */
function damask(g: CanvasRenderingContext2D, w: number, h: number): void {
  const r = rng(3);
  g.fillStyle = '#0f2a22';
  g.fillRect(0, 0, w, h);
  g.fillStyle = 'rgba(160,140,80,0.10)';
  const motif = (cx: number, cy: number, s: number) => {
    g.beginPath();
    g.moveTo(cx, cy - s);
    g.bezierCurveTo(cx + s * 0.7, cy - s * 0.5, cx + s * 0.5, cy + s * 0.3, cx, cy + s);
    g.bezierCurveTo(cx - s * 0.5, cy + s * 0.3, cx - s * 0.7, cy - s * 0.5, cx, cy - s);
    g.fill();
    g.beginPath(); g.arc(cx, cy, s * 0.18, 0, Math.PI * 2); g.fill();
  };
  motif(w / 2, h / 2, 70);
  for (const [x, y] of [[0, 0], [w, 0], [0, h], [w, h]]) motif(x, y, 70);
  g.strokeStyle = 'rgba(160,140,80,0.07)';
  g.lineWidth = 2;
  for (let x = 0; x < w; x += w / 4) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, h); g.stroke(); }
  grain(g, w, h, 900, 0.06, r);
}

/** Diamond-tufted leather: oxblood, buttons, creases. */
function tufted(base: string): (g: CanvasRenderingContext2D, w: number, h: number) => void {
  return (g, w, h) => {
    const r = rng(19);
    g.fillStyle = base;
    g.fillRect(0, 0, w, h);
    const s = w / 4;
    for (let y = 0; y <= h; y += s / 2) {
      for (let x = ((y / (s / 2)) % 2) * (s / 2); x <= w; x += s) {
        const grd = g.createRadialGradient(x, y, 1, x, y, s * 0.55);
        grd.addColorStop(0, 'rgba(0,0,0,0.55)');
        grd.addColorStop(0.25, 'rgba(0,0,0,0.12)');
        grd.addColorStop(0.7, 'rgba(255,220,200,0.07)');
        grd.addColorStop(1, 'rgba(0,0,0,0)');
        g.fillStyle = grd;
        g.fillRect(x - s, y - s, s * 2, s * 2);
        g.fillStyle = 'rgba(20,6,4,0.9)';
        g.beginPath(); g.arc(x, y, 3, 0, Math.PI * 2); g.fill();
      }
    }
    grain(g, w, h, 500, 0.08, r);
  };
}

/** The card back: oxblood with a gold lattice and a K. */
function cardBack(g: CanvasRenderingContext2D, w: number, h: number): void {
  g.fillStyle = '#f3ecdc';
  g.fillRect(0, 0, w, h);
  g.fillStyle = '#6b1020';
  g.beginPath(); g.roundRect(12, 12, w - 24, h - 24, 14); g.fill();
  g.strokeStyle = 'rgba(232,190,110,0.55)';
  g.lineWidth = 2;
  for (let k = -h; k < w + h; k += 22) {
    g.beginPath(); g.moveTo(k, 12); g.lineTo(k + h, h - 12); g.stroke();
    g.beginPath(); g.moveTo(k + h, 12); g.lineTo(k, h - 12); g.stroke();
  }
  g.strokeStyle = '#E8BE6E';
  g.lineWidth = 4;
  g.beginPath(); g.roundRect(22, 22, w - 44, h - 44, 10); g.stroke();
  g.fillStyle = '#6b1020';
  g.beginPath(); g.arc(w / 2, h / 2, 40, 0, Math.PI * 2); g.fill();
  g.strokeStyle = '#E8BE6E';
  g.stroke();
  g.fillStyle = '#E8BE6E';
  g.font = '700 54px Georgia, "Times New Roman", serif';
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText('K', w / 2, h / 2 + 3);
}

/** The market card's face: the house ace — the brand, never a hand value. */
function cardFace(g: CanvasRenderingContext2D, w: number, h: number): void {
  g.fillStyle = '#f6efdf';
  g.fillRect(0, 0, w, h);
  g.strokeStyle = '#C9A55A';
  g.lineWidth = 4;
  g.beginPath(); g.roundRect(14, 14, w - 28, h - 28, 12); g.stroke();
  g.fillStyle = '#16120e';
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.font = '700 46px Georgia, "Times New Roman", serif';
  g.fillText('A', 40, 48);
  g.font = '700 34px Georgia, serif';
  g.fillText('♠', 40, 88);
  g.save();
  g.translate(w - 40, h - 48);
  g.rotate(Math.PI);
  g.font = '700 46px Georgia, "Times New Roman", serif';
  g.fillText('A', 0, 0);
  g.font = '700 34px Georgia, serif';
  g.fillText('♠', 0, -40);
  g.restore();
  g.font = '400 190px Georgia, serif';
  g.fillText('♠', w / 2, h / 2 - 6);
  g.fillStyle = '#9a7a34';
  g.font = '700 17px "Chakra Petch", sans-serif';
  g.fillText('KRYPT  POLYBOT', w / 2, h - 70);
}

class Screen {
  readonly tex: THREE.CanvasTexture;
  readonly g: CanvasRenderingContext2D;
  constructor(w: number, h: number, private paint: (g: CanvasRenderingContext2D, w: number, h: number) => void) {
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    this.g = c.getContext('2d')!;
    this.tex = new THREE.CanvasTexture(c);
    this.tex.colorSpace = THREE.SRGBColorSpace;
    this.tex.anisotropy = 8;
    this.redraw();
  }
  redraw(): void {
    this.paint(this.g, this.g.canvas.width, this.g.canvas.height);
    this.tex.needsUpdate = true;
  }
}

export interface ScoreRow { id: MemberId; right: number; n: number }

/** Per seat: where its stacks, cards and bet live on the felt. */
interface SeatSpot {
  stack: [number, number];
  bet: [number, number];
  /** The felt's tangent at this seat (unit), to lay things side by side. */
  ux: number;
  uz: number;
  /** Pool ranges in the chip field. */
  stackAt: number;
  betAt: number;
  /** Bet: -1 none, else 0..1 pushed in; its side and chip count. */
  betK: number;
  betGoal: number;
  betSide: boolean | null;
  betChips: number;
  stackH: number[];
  stackShown: number[];
  lammer: THREE.Mesh;
  lamp: THREE.MeshStandardMaterial;
  decor: THREE.Group;
}

const STACKS = 3;
const STACK_MAX = 16;
const BET_MAX = 10;
const POT_STACKS = 12;
const POT_PER = 14;
const STACK_COLORS = [CHIP.black, CHIP.purple, CHIP.red, CHIP.blue, CHIP.green, CHIP.gold];

export class PokerRoom {
  readonly group = new THREE.Group();
  private textures: THREE.Texture[] = [];
  private own = new Set<THREE.Material>();
  readonly chips = new ChipKit();
  private field: ChipField;
  private rain: THREE.InstancedMesh;
  private rainState: { p: THREE.Vector3; v: THREE.Vector3; r: THREE.Euler; w: THREE.Vector3 }[] = [];
  private rainT = 0;

  private keyLight: THREE.SpotLight;
  private lampGlow: THREE.MeshStandardMaterial;
  private haze: THREE.MeshBasicMaterial;
  private inlay: THREE.MeshStandardMaterial;
  feltPulse = 0;
  /** The 3D YES/NO markers on the bets; off when the overlay labels each
   *  bet with its real cents instead. */
  lammers = true;
  feltColor = new THREE.Color('#FFB547');
  private spots: SeatSpot[] = [];
  private lammerYes: THREE.MeshBasicMaterial;
  private lammerNo: THREE.MeshBasicMaterial;
  private potAt = 0;
  private potShown = 0;
  private potGoal = 0;
  private card = new THREE.Group();
  /** 0 in the shoe, 1 on the felt; the flip 0 down .. 1 up. */
  private cardK = 0;
  private cardGoal = 0;
  private cardFlip = 0;
  private cardFlipGoal = 0;
  private history: Screen;
  private historyLines: string[] = [];
  private historyDirty = false;
  private historyCd = 0;
  private score: Screen;
  private scoreView: ScoreView = { title: 'WHO CALLED IT?', caption: 'call accuracy · settled trades', lines: [] };
  private neon: THREE.MeshBasicMaterial;
  private moodColor = new THREE.Color('#FFB547');
  private bluey: THREE.Sprite;
  private blueyTex: THREE.Texture;
  private blueyFrame = 0;
  private blueyAcc = 0;
  blueyHop = 0;
  private tapT = 99;
  private tapHits = 0;
  private dust: THREE.Points;
  readonly tableShake = { v: 0 };
  private tableTop = new THREE.Group();
  readonly placards: THREE.Vector3[] = [];
  /** Where each seat's bet sits on the felt (for its label). */
  readonly betAnchors: THREE.Vector3[] = [];
  readonly moodAnchor = new THREE.Vector3(8.2, 3.55, -3.6);
  /** The point over the felt where the market card is played. */
  readonly cardAnchor = new THREE.Vector3(0, TABLE_Y + 0.02, 0.35);
  /** The fourth wall. The room is a stage: the normal camera sits where
   *  this wall would be. Shown only to someone walking the room (walk.ts),
   *  who would otherwise turn round into fog. */
  readonly frontWall = new THREE.Group();
  /** 0..1: someone on foot is at the doors, so the EXIT sign brightens. */
  private exitNear = 0;
  private exitSign: THREE.MeshBasicMaterial;
  private _v = new THREE.Vector3();

  constructor() {
    const G = this.group;
    const std = (color: string, o: { e?: string; ei?: number; rough?: number; metal?: number; map?: THREE.Texture | null } = {}) =>
      new THREE.MeshStandardMaterial({
        color, map: o.map ?? null, emissive: o.e ?? '#000000', emissiveIntensity: o.ei ?? 0,
        roughness: o.rough ?? 0.8, metalness: o.metal ?? 0.04,
      });
    const own = <M extends THREE.Material>(m: M) => { this.own.add(m); return m; };
    const add = (geo: THREE.BufferGeometry, m: THREE.Material, x: number, y: number, z: number, cast = true, recv = true, parent: THREE.Object3D = G) => {
      const o = new THREE.Mesh(geo, m);
      o.position.set(x, y, z);
      o.castShadow = cast;
      o.receiveShadow = recv;
      parent.add(o);
      return o;
    };
    const tex = (t: THREE.CanvasTexture) => { this.textures.push(t); return t; };

    // ── shared materials ────────────────────────────────────────────────
    const brass = std('#C9A55A', { metal: 0.85, rough: 0.32 });
    const walnut = std('#3b2112', { rough: 0.38, metal: 0.05 });
    const darkWood = std('#1f1109', { rough: 0.55 });
    const leatherTex = tex(canvasTex(256, 256, tufted('#4a1414'), [2, 2]));
    const leather = std('#ffffff', { map: leatherTex, rough: 0.55 });
    const blackLeather = std('#1a1210', { rough: 0.48, metal: 0.02 });

    // ── shell ───────────────────────────────────────────────────────────
    const floorTex = tex(canvasTex(512, 512, planks, [5, 7]));
    const floor = add(new THREE.PlaneGeometry(17.6, 15.2), std('#ffffff', { map: floorTex, rough: 0.45 }), 0, 0, 1.1, false);
    floor.rotation.x = -Math.PI / 2;
    // the oval rug under the table: ink teal, a gold key border
    const rugTex = tex(canvasTex(1024, 640, (g, w, h) => {
      const r = rng(5);
      g.clearRect(0, 0, w, h);
      g.save();
      g.beginPath(); g.ellipse(w / 2, h / 2, w / 2 - 2, h / 2 - 2, 0, 0, Math.PI * 2); g.clip();
      g.fillStyle = '#3a0f14'; g.fillRect(0, 0, w, h);
      g.strokeStyle = '#B48A3E'; g.lineWidth = 10;
      g.beginPath(); g.ellipse(w / 2, h / 2, w / 2 - 34, h / 2 - 34, 0, 0, Math.PI * 2); g.stroke();
      g.lineWidth = 3;
      g.beginPath(); g.ellipse(w / 2, h / 2, w / 2 - 54, h / 2 - 54, 0, 0, Math.PI * 2); g.stroke();
      g.fillStyle = '#1d0709';
      g.beginPath(); g.ellipse(w / 2, h / 2, w / 2 - 60, h / 2 - 60, 0, 0, Math.PI * 2); g.fill();
      g.strokeStyle = 'rgba(180,138,62,0.18)'; g.lineWidth = 2;
      for (let k = 1; k < 6; k++) { g.beginPath(); g.ellipse(w / 2, h / 2, (w / 2 - 60) * k / 6, (h / 2 - 60) * k / 6, 0, 0, Math.PI * 2); g.stroke(); }
      grain(g, w, h, 4000, 0.12, r);
      g.restore();
    }));
    const rug = add(new THREE.CircleGeometry(1, 72), new THREE.MeshStandardMaterial({ map: rugTex, transparent: true, roughness: 1, depthWrite: false }), 0, 0.006, -0.25, false);
    rug.rotation.x = -Math.PI / 2;
    rug.scale.set(6.1, 4.4, 1);

    // walls: damask above, walnut wainscot below, a chair rail and a crown
    const paperTex = tex(canvasTex(256, 256, damask, [10, 3]));
    const paper = std('#ffffff', { map: paperTex, rough: 0.9 });
    const wainTex = tex(canvasTex(256, 192, wainscot, [11, 1]));
    const wainM = std('#ffffff', { map: wainTex, rough: 0.5 });
    const wainSideTex = tex(canvasTex(256, 192, wainscot, [10, 1]));
    const wainSideM = std('#ffffff', { map: wainSideTex, rough: 0.5 });
    add(new THREE.PlaneGeometry(17.6, 6.2), paper, 0, 3.1, BACK_Z, false);
    add(new THREE.BoxGeometry(17.6, 1.3, 0.08), wainM, 0, 0.65, BACK_Z + 0.04, false);
    add(new THREE.BoxGeometry(17.6, 0.07, 0.12), walnut, 0, 1.33, BACK_Z + 0.06, false);
    add(new THREE.BoxGeometry(17.6, 0.26, 0.2), walnut, 0, 6.05, BACK_Z + 0.1, false);
    add(new THREE.BoxGeometry(17.6, 0.12, 0.1), darkWood, 0, 0.06, BACK_Z + 0.09, false);
    for (const s of [-1, 1]) {
      const side = add(new THREE.PlaneGeometry(15.2, 6.2), paper, s * 8.8, 3.1, 1.1, false);
      side.rotation.y = -s * Math.PI / 2;
      const wv = add(new THREE.BoxGeometry(0.08, 1.3, 15.2), wainSideM, s * 8.76, 0.65, 1.1, false);
      void wv;
      add(new THREE.BoxGeometry(0.12, 0.07, 15.2), walnut, s * 8.74, 1.33, 1.1, false);
      add(new THREE.BoxGeometry(0.2, 0.26, 15.2), walnut, s * 8.7, 6.05, 1.1, false);
    }
    // the ceiling: seen only on foot (the stage camera is above it, and a
    // plane's back face is culled), so it costs the overview nothing
    const ceil = add(new THREE.PlaneGeometry(17.6, 15.2), std('#120a06', { rough: 0.9 }), 0, 6.18, 1.1, false, false);
    ceil.rotation.x = Math.PI / 2;
    // walnut pilasters with brass capitals
    for (const x of [-8.45, -4.55, 6.15, 8.45]) {
      add(new THREE.BoxGeometry(0.42, 6.0, 0.16), walnut, x, 3.0, BACK_Z + 0.08);
      add(new THREE.BoxGeometry(0.5, 0.12, 0.22), brass, x, 5.86, BACK_Z + 0.1, false);
      add(new THREE.BoxGeometry(0.5, 0.14, 0.22), darkWood, x, 0.07, BACK_Z + 0.1, false);
    }

    // ── the doors (back wall, far left): padded leather, brass studs ────
    {
      const x = DOOR_X;
      add(new THREE.BoxGeometry(2.7, 3.45, 0.14), walnut, x, 1.72, BACK_Z + 0.07, false);
      const doorTex = tex(canvasTex(256, 384, tufted('#3a1010')));
      const doorM = std('#ffffff', { map: doorTex, rough: 0.5 });
      for (const s of [-1, 1]) {
        add(new THREE.BoxGeometry(1.08, 3.05, 0.1), doorM, x + s * 0.56, 1.58, BACK_Z + 0.16, false);
        add(new THREE.BoxGeometry(0.05, 0.5, 0.05), brass, x + s * 0.14, 1.55, BACK_Z + 0.24, false);
        // a brass kick plate and a brass edge to catch the light
        add(new THREE.BoxGeometry(1.0, 0.28, 0.02), brass, x + s * 0.56, 0.2, BACK_Z + 0.22, false);
      }
      add(new THREE.BoxGeometry(2.7, 0.1, 0.2), brass, x, 3.45, BACK_Z + 0.12, false);
    }

    // ── back wall dressing ──────────────────────────────────────────────
    // HIGH STAKES: brass letters on a black lacquer board
    const signTex = tex(canvasTex(1024, 220, (g, w, h) => {
      g.fillStyle = '#0b0807';
      g.fillRect(0, 0, w, h);
      g.strokeStyle = '#C9A55A'; g.lineWidth = 6; g.strokeRect(10, 10, w - 20, h - 20);
      g.lineWidth = 2; g.strokeRect(22, 22, w - 44, h - 44);
      const grd = g.createLinearGradient(0, 40, 0, 170);
      grd.addColorStop(0, '#F6DC9A'); grd.addColorStop(0.5, '#C9A55A'); grd.addColorStop(1, '#8A6A2A');
      g.fillStyle = grd;
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.font = 'italic 700 104px Georgia, "Times New Roman", serif';
      // the gap between the words sits dead centre, where the lamp's cord
      // crosses the sign from the stage camera
      g.textAlign = 'right';
      g.fillText('High', w / 2 - 22, 104);
      g.textAlign = 'left';
      g.fillText('Stakes', w / 2 + 22, 104);
      g.textAlign = 'center';
      g.font = '600 26px "Chakra Petch", sans-serif';
      g.fillText('♠   KRYPT  POLYBOT  ·  PRIVATE  ROOM   ♠', w / 2, 178);
    }));
    // Sized and hung for the stage camera: between the frame's top edge and
    // the lamp's crown, at the overview and at the push-in alike.
    // A touch of glow of its own, so the brass reads in the dark room.
    add(new THREE.PlaneGeometry(2.8, 0.6), new THREE.MeshStandardMaterial({
      map: signTex, emissiveMap: signTex, emissive: '#ffffff', emissiveIntensity: 0.32, roughness: 0.35, metalness: 0.3,
    }), 0, 4.12, BACK_Z + 0.06, false, false);

    // the hand history: a framed slate of the table's last words
    this.history = new Screen(704, 448, (g, w, h) => this.paintHistory(g, w, h));
    this.textures.push(this.history.tex);
    add(new THREE.BoxGeometry(2.8, 1.85, 0.08), brass, -2.85, 3.12, BACK_Z + 0.05, false);
    add(new THREE.PlaneGeometry(2.66, 1.71), new THREE.MeshBasicMaterial({ map: this.history.tex, toneMapped: false, color: '#ddd5c6' }), -2.85, 3.12, BACK_Z + 0.1, false, false);

    // the leaderboard: WHO CALLED IT?, framed in gilt
    this.score = new Screen(1024, 640, (g, w, h) => this.paintScore(g, w, h));
    this.textures.push(this.score.tex);
    add(new THREE.BoxGeometry(3.7, 2.38, 0.1), brass, 3.95, 3.15, BACK_Z + 0.06, false);
    add(new THREE.PlaneGeometry(3.52, 2.2), new THREE.MeshBasicMaterial({ map: this.score.tex, toneMapped: false, color: '#e2dacc' }), 3.95, 3.15, BACK_Z + 0.12, false, false);

    // sconces: brass arm, a little silk shade glowing, and the fan of light
    // it throws on the wall. No light of their own: the bloom and the baked
    // splash sell them, and lights are the scene's real per-pixel cost.
    const shadeM = own(std('#2a1606', { e: '#FF9A48', ei: 2.6, rough: 0.6 }));
    const splashes: THREE.BufferGeometry[] = [];
    const sconce = (x: number, y: number, z: number, ry: number) => {
      const s = new THREE.Group();
      s.position.set(x, y, z);
      s.rotation.y = ry;
      add(new THREE.BoxGeometry(0.1, 0.26, 0.04), brass, 0, 0, 0.02, false, true, s);
      add(new THREE.CylinderGeometry(0.018, 0.018, 0.2, 6), brass, 0, 0.02, 0.12, false, true, s).rotation.x = Math.PI / 2;
      add(new THREE.CylinderGeometry(0.07, 0.12, 0.18, 14, 1, true), shadeM, 0, 0.14, 0.22, false, false, s);
      G.add(s);
      const sp = new THREE.PlaneGeometry(1.7, 2.6);
      sp.translate(0, 0.15, 0.03);
      sp.applyMatrix4(new THREE.Matrix4().makeRotationY(ry).setPosition(x, y, z));
      splashes.push(sp);
    };
    for (const x of [-0.8, 0.8, 7.3]) sconce(x, 3.0, BACK_Z, 0);
    for (const z of [-3.9, 0.6, 4.6]) sconce(-8.8, 3.0, z, Math.PI / 2);
    for (const z of [0.4, 4.4]) sconce(8.8, 3.0, z, -Math.PI / 2);
    {
      const splashTex = tex(canvasTex(128, 192, (g, w, h) => {
        // a lamp's fan: bright at the shade, up and down the wall
        const grd = g.createRadialGradient(w / 2, h * 0.45, 2, w / 2, h * 0.45, w * 0.5);
        grd.addColorStop(0, 'rgba(255,255,255,1)');
        grd.addColorStop(0.35, 'rgba(255,255,255,0.35)');
        grd.addColorStop(1, 'rgba(255,255,255,0)');
        g.fillStyle = '#000';
        g.fillRect(0, 0, w, h);
        g.save();
        g.scale(1, 1.6);
        g.translate(0, -h * 0.17);
        g.fillStyle = grd;
        g.fillRect(0, 0, w, h);
        g.restore();
      }));
      const merged = mergeGeometries(splashes);
      splashes.forEach((x) => x.dispose());
      const splash = new THREE.Mesh(merged, new THREE.MeshBasicMaterial({ map: splashTex, color: '#B8682A', transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
      G.add(splash);
    }

    // a painting on the left wall: an oversized ace of spades in gilt
    {
      const art = tex(canvasTex(384, 512, (g, w, h) => {
        const grd = g.createRadialGradient(w / 2, h / 2, 20, w / 2, h / 2, h * 0.7);
        grd.addColorStop(0, '#2a3b33'); grd.addColorStop(1, '#0c1210');
        g.fillStyle = grd; g.fillRect(0, 0, w, h);
        g.fillStyle = '#efe6d2';
        g.beginPath(); g.roundRect(70, 70, w - 140, h - 140, 18); g.fill();
        g.fillStyle = '#111';
        g.font = '400 170px Georgia, serif';
        g.textAlign = 'center'; g.textBaseline = 'middle';
        g.fillText('♠', w / 2, h / 2);
        g.font = '700 40px Georgia, serif';
        g.fillText('A', 104, 112);
      }));
      add(new THREE.BoxGeometry(0.08, 2.2, 1.7), brass, -8.74, 3.35, -1.6, false);
      const p = add(new THREE.PlaneGeometry(1.5, 2.0), std('#ffffff', { map: art, rough: 0.6 }), -8.69, 3.35, -1.6, false, false);
      p.rotation.y = Math.PI / 2;
    }

    // ── the bar (right wall): a backlit shelf of bottles, a walnut counter
    {
      const glowTex = tex(canvasTex(64, 256, (g, w, h) => {
        const grd = g.createLinearGradient(0, 0, 0, h);
        grd.addColorStop(0, '#2a1404'); grd.addColorStop(0.5, '#FFB458'); grd.addColorStop(1, '#2a1404');
        g.fillStyle = grd; g.fillRect(0, 0, w, h);
      }));
      const back = add(new THREE.PlaneGeometry(4.2, 2.2), new THREE.MeshBasicMaterial({ map: glowTex, color: '#a07040', toneMapped: false }), 8.74, 2.55, -3.6, false, false);
      back.rotation.y = -Math.PI / 2;
      for (const y of [1.55, 2.3, 3.05]) add(new THREE.BoxGeometry(0.3, 0.04, 4.2), walnut, 8.6, y, -3.6, false);
      add(new THREE.BoxGeometry(0.3, 2.5, 0.12), walnut, 8.6, 2.55, -5.75, false);
      add(new THREE.BoxGeometry(0.3, 2.5, 0.12), walnut, 8.6, 2.55, -1.45, false);
      // bottles: one instanced draw, dark glass silhouetted on the glow
      const bottleG = new THREE.CylinderGeometry(0.06, 0.07, 0.34, 10);
      bottleG.translate(0, 0.17, 0);
      const r = rng(23);
      const N = 48;
      const bottles = new THREE.InstancedMesh(bottleG, new THREE.MeshStandardMaterial({ color: '#ffffff', roughness: 0.25, metalness: 0.1 }), N);
      const m4 = new THREE.Matrix4();
      const col = new THREE.Color();
      const glass = ['#3d1f0a', '#16301a', '#2b0d0d', '#5a3a12', '#0f1f2e', '#4a4030'];
      for (let i = 0; i < N; i++) {
        const shelf = i % 3;
        const z = -5.55 + (Math.floor(i / 3) / (N / 3)) * 3.9 + r() * 0.05;
        const s = 0.8 + r() * 0.45;
        m4.makeScale(1, s, 1).setPosition(8.62, [1.57, 2.32, 3.07][shelf], z);
        bottles.setMatrixAt(i, m4);
        bottles.setColorAt(i, col.set(glass[Math.floor(r() * glass.length)]));
      }
      G.add(bottles);
      // the counter
      add(new THREE.BoxGeometry(0.8, 1.05, 4.2), std('#ffffff', { map: wainTex, rough: 0.5 }), 8.0, 0.53, -3.7);
      add(new THREE.BoxGeometry(0.98, 0.08, 4.36), walnut, 7.95, 1.09, -3.7);
      const rail = add(new THREE.CylinderGeometry(0.03, 0.03, 4.2, 8), brass, 7.45, 0.22, -3.7, false);
      rail.rotation.x = Math.PI / 2;
      for (const z of [-4.6, -3.5, -2.4]) {
        add(new THREE.CylinderGeometry(0.24, 0.22, 0.12, 18), leather, 7.1, 0.8, z);
        add(new THREE.CylinderGeometry(0.03, 0.03, 0.75, 6), brass, 7.1, 0.38, z, false);
        add(new THREE.CylinderGeometry(0.2, 0.2, 0.03, 16), brass, 7.1, 0.015, z, false);
      }
      // the neon over the bar: it takes the table's mood colour
      const neonTex = tex(canvasTex(512, 128, (g, w, h) => {
        g.clearRect(0, 0, w, h);
        g.strokeStyle = '#ffffff';
        g.lineWidth = 7;
        g.lineCap = 'round';
        g.font = 'italic 400 84px Georgia, serif';
        g.textAlign = 'center'; g.textBaseline = 'middle';
        g.strokeText('Table Talk', w / 2, h / 2 + 4);
        g.fillStyle = 'rgba(255,255,255,0.35)';
        g.fillText('Table Talk', w / 2, h / 2 + 4);
      }));
      this.neon = own(new THREE.MeshBasicMaterial({ map: neonTex, color: '#FFB547', transparent: true, depthWrite: false, toneMapped: false, blending: THREE.AdditiveBlending }));
      const neon = add(new THREE.PlaneGeometry(2.2, 0.55), this.neon, 8.7, 3.95, -3.6, false, false);
      neon.rotation.y = -Math.PI / 2;
    }

    // ── the lounge, front left, and a palm front right ──────────────────
    {
      const sofa = new THREE.Group();
      sofa.position.set(-8.15, 0, 5.6);
      sofa.rotation.y = Math.PI / 2;
      add(new THREE.BoxGeometry(2.6, 0.45, 0.95), leather, 0, 0.38, 0, true, true, sofa);
      add(new THREE.BoxGeometry(2.6, 0.75, 0.25), leather, 0, 0.9, -0.38, true, true, sofa);
      for (const s of [-1, 1]) add(new THREE.CylinderGeometry(0.2, 0.2, 0.95, 14), leather, s * 1.3, 0.62, 0, true, true, sofa).rotation.x = Math.PI / 2;
      G.add(sofa);
      add(new THREE.CylinderGeometry(0.3, 0.3, 0.05, 20), walnut, -8.2, 0.62, 3.5);
      add(new THREE.CylinderGeometry(0.05, 0.12, 0.6, 10), darkWood, -8.2, 0.3, 3.5);
      add(new THREE.CylinderGeometry(0.03, 0.03, 0.55, 6), brass, -8.2, 0.92, 3.5, false);
      add(new THREE.CylinderGeometry(0.14, 0.24, 0.3, 16, 1, true), shadeM, -8.2, 1.3, 3.5, false, false);
      // palm: a pot and a few blades
      add(new THREE.CylinderGeometry(0.38, 0.3, 0.6, 16), brass, 8.0, 0.3, 6.6);
      const leaf = std('#1f4a2a', { rough: 0.7 });
      const rr = rng(31);
      for (let i = 0; i < 9; i++) {
        const a = (i / 9) * Math.PI * 2;
        const b = add(new THREE.ConeGeometry(0.16, 1.6 + rr() * 0.6, 4), leaf, 8.0 + Math.cos(a) * 0.35, 1.3, 6.6 + Math.sin(a) * 0.35);
        b.rotation.set(Math.sin(a) * 0.6, 0, -Math.cos(a) * 0.6);
      }
    }

    // ── the table ───────────────────────────────────────────────────────
    G.add(this.tableTop);
    const T = this.tableTop;
    const ell = (a: number, b: number, h: number, y: number, m: THREE.Material, seg = 96) => {
      const geo = new THREE.CylinderGeometry(1, 1, h, seg);
      geo.scale(a, 1, b);
      return add(geo, m, 0, y, 0, true, true, T);
    };
    // two pedestals under a walnut apron
    for (const x of [-1.5, 1.5]) {
      add(new THREE.CylinderGeometry(0.28, 0.42, TABLE_Y - 0.25, 20), darkWood, x, (TABLE_Y - 0.25) / 2, 0);
      add(new THREE.CylinderGeometry(0.62, 0.7, 0.08, 24), brass, x, 0.04, 0, false);
    }
    ell(3.05, 1.72, 0.26, TABLE_Y - 0.17, walnut);
    // the racetrack: a glossy walnut band between felt and rail
    ell(RAIL_A, RAIL_B, 0.05, TABLE_Y - 0.02, std('#4a2814', { rough: 0.22, metal: 0.08 }));
    // the felt
    const feltTex = tex(canvasTex(1024, 528, (g, w, h) => this.paintFelt(g, w, h)));
    const felt = add(new THREE.CircleGeometry(1, 128), std('#ffffff', { map: feltTex, rough: 0.95 }), 0, TABLE_Y + 0.006, 0, false, true, T);
    felt.rotation.x = -Math.PI / 2;
    felt.scale.set(FELT_A, FELT_B, 1);
    // the padded rail: black leather round the whole oval
    const railPts: THREE.Vector3[] = [];
    for (let i = 0; i < 96; i++) {
      const a = (i / 96) * Math.PI * 2;
      railPts.push(new THREE.Vector3(Math.cos(a) * RAIL_A, 0, Math.sin(a) * RAIL_B));
    }
    const railGeo = new THREE.TubeGeometry(new THREE.CatmullRomCurve3(railPts, true), 192, RAIL_R, 14, true);
    railGeo.scale(1, 0.72, 1);
    add(railGeo, blackLeather, 0, TABLE_Y + 0.07, 0, true, true, T);
    // the inlay where felt meets wood: brass that glows the showdown's colour
    const inlayPts: THREE.Vector3[] = [];
    for (let i = 0; i < 96; i++) {
      const a = (i / 96) * Math.PI * 2;
      inlayPts.push(new THREE.Vector3(Math.cos(a) * (FELT_A + 0.02), 0, Math.sin(a) * (FELT_B + 0.02)));
    }
    this.inlay = own(std('#5a4018', { e: '#FFB547', ei: 0.35, metal: 0.6, rough: 0.35 }));
    add(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(inlayPts, true), 192, 0.016, 6, true), this.inlay, 0, TABLE_Y + 0.012, 0, false, false, T);

    // the dealer's rack, the shoe and the discard tray at the head of the table
    add(new THREE.BoxGeometry(0.92, 0.07, 0.34), darkWood, 0, TABLE_Y + 0.03, -1.12, true, true, T);
    add(new THREE.BoxGeometry(0.26, 0.15, 0.36), std('#101010', { rough: 0.4 }), 0.8, TABLE_Y + 0.08, -1.05, true, true, T);
    add(new THREE.BoxGeometry(0.27, 0.02, 0.37), brass, 0.8, TABLE_Y + 0.16, -1.05, false, true, T);
    add(new THREE.BoxGeometry(0.26, 0.06, 0.36), std('#101010', { rough: 0.4 }), -0.8, TABLE_Y + 0.04, -1.05, true, true, T);

    // ── chips: rack, stacks, bets, the pot — one instanced mesh ─────────
    const rackN = 8 * 10;
    const cap = rackN + MEMBER_IDS.length * (STACKS * STACK_MAX + BET_MAX) + POT_STACKS * POT_PER;
    this.field = new ChipField(this.chips, cap);
    T.add(this.field.mesh);
    {
      const at = this.field.alloc(rackN);
      const cols = [CHIP.white, CHIP.red, CHIP.green, CHIP.black, CHIP.purple, CHIP.blue, CHIP.gold, CHIP.teal];
      for (let c = 0; c < 8; c++) {
        for (let k = 0; k < 10; k++) {
          const i = at + c * 10 + k;
          this.field.put(i, -0.37 + c * 0.105, TABLE_Y + 0.13, -1.24 + k * (CHIP_H + 0.002), 0, true);
          this.field.color(i, cols[c]);
        }
      }
    }

    // ── each seat: club chair, cards and stacks, bet spot and lamp ──────
    const backTex = tex(canvasTex(256, 360, cardBack));
    const backM = std('#ffffff', { map: backTex, rough: 0.5 });
    const cardG = new THREE.BoxGeometry(0.25, 0.006, 0.35);
    this.lammerYes = this.lammerMat('YES', '#15803D');
    this.lammerNo = this.lammerMat('NO', '#B91C1C');
    const lammerG = new THREE.CircleGeometry(0.11, 24);
    const lampG = new THREE.RingGeometry(0.15, 0.19, 32);
    for (let i = 0; i < MEMBER_IDS.length; i++) {
      const [sx, sz] = seatOf(i);
      const a = rad(SEAT_DEG[i]);
      // the seat's tangent on the felt, for laying out cards beside chips
      const tx = -Math.sin(a) * FELT_A, tz = Math.cos(a) * FELT_B;
      const tl = Math.hypot(tx, tz);
      // +1 when the tangent points away from the dealer (right half)
      const side = i < SEAT_DEG.length / 2 ? -1 : 1;
      const ux = (tx / tl) * side, uz = (tz / tl) * side;
      const stack = feltToward(i, 0.8);
      const bet = feltToward(i, 0.52);
      const yaw = Math.atan2(-sx, -sz);

      // placard: on the rail in front of the seat
      this.placards.push(new THREE.Vector3(Math.cos(a) * (RAIL_A + 0.05), TABLE_Y + 0.2, Math.sin(a) * (RAIL_B + 0.05)));
      this.betAnchors.push(new THREE.Vector3(bet[0], TABLE_Y + 0.02, bet[1]));

      // the chair: tufted oxblood leather, a brass cap (the same at every
      // seat: an empty chair belongs to nobody). `yaw` turns the chair's +z
      // toward the table, so the cushion faces the felt and the backrest goes
      // on -z, behind whoever sits in it.
      const chair = new THREE.Group();
      chair.position.set(Math.cos(a) * (SEAT_A + CHAIR_OUT), 0, Math.sin(a) * (SEAT_B + CHAIR_OUT));
      chair.rotation.y = yaw;
      add(new THREE.BoxGeometry(1.0, 0.42, 0.9), leather, 0, 0.42, 0, true, true, chair);
      add(new THREE.BoxGeometry(1.06, 1.15, 0.24), leather, 0, 1.05, CHAIR_BACK_Z, true, true, chair);
      add(new THREE.BoxGeometry(1.08, 0.05, 0.26), brass, 0, 1.64, CHAIR_BACK_Z, false, true, chair);
      for (const s of [-1, 1]) {
        add(new THREE.BoxGeometry(0.18, 0.62, 0.88), leather, s * 0.56, 0.52, 0.02, true, true, chair);
        for (const fz of [-0.36, 0.36]) add(new THREE.CylinderGeometry(0.04, 0.03, 0.2, 8), brass, s * 0.45, 0.1, fz, false, true, chair);
      }
      G.add(chair);

      // the seat's own things on the felt: two hole cards, face down
      const decor = new THREE.Group();
      for (const k of [0, 1]) {
        const c = add(cardG, backM, stack[0] + ux * (0.42 + k * 0.07), TABLE_Y + 0.013 + k * 0.007, stack[1] + uz * (0.42 + k * 0.07), true, true, decor);
        c.rotation.y = yaw + (k ? 0.12 : -0.06);
      }
      T.add(decor);

      // the bet spot's ring lamp: lights the side the chips went to
      const lamp = own(std('#2a1d12', { e: '#000000', ei: 0, rough: 0.4 }));
      const ring = add(lampG, lamp, bet[0], TABLE_Y + 0.009, bet[1], false, false, T);
      ring.rotation.x = -Math.PI / 2;
      ring.scale.set(1, 1, 1);

      // the lammer: a marker on the bet saying which side it backs
      const lam = new THREE.Mesh(lammerG, this.lammerYes);
      lam.visible = false;
      G.add(lam);

      const stackAt = this.field.alloc(STACKS * STACK_MAX);
      const betAt = this.field.alloc(BET_MAX);
      for (let s = 0; s < STACKS; s++) {
        for (let k = 0; k < STACK_MAX; k++) this.field.color(stackAt + s * STACK_MAX + k, STACK_COLORS[(i + s * 2) % STACK_COLORS.length]);
      }
      this.spots.push({
        stack, bet, ux, uz, stackAt, betAt, betK: 0, betGoal: 0, betSide: null, betChips: 5,
        stackH: [8, 6, 4], stackShown: [-1, -1, -1], lammer: lam, lamp, decor,
      });
    }

    // the pot, in the middle of the felt
    this.potAt = this.field.alloc(POT_STACKS * POT_PER);
    for (let i = 0; i < POT_STACKS * POT_PER; i++) this.field.color(this.potAt + i, STACK_COLORS[(i % POT_STACKS) % STACK_COLORS.length]);
    this.potGoal = 30;

    // the market card, dealt from the shoe to the middle of the felt
    {
      const faceTex = tex(canvasTex(256, 360, cardFace));
      const w = 0.44, h = 0.62;
      const face = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshStandardMaterial({ map: faceTex, roughness: 0.45 }));
      face.rotation.x = -Math.PI / 2;
      face.position.y = 0.0045;
      const back = new THREE.Mesh(new THREE.PlaneGeometry(w, h), backM);
      back.rotation.x = Math.PI / 2;
      back.position.y = -0.0045;
      const edge = new THREE.Mesh(new THREE.BoxGeometry(w, 0.008, h), std('#efe8d8', { rough: 0.5 }));
      face.castShadow = back.castShadow = edge.castShadow = true;
      this.card.add(face, back, edge);
      this.card.visible = false;
      G.add(this.card);
    }

    // ── the dealer ──────────────────────────────────────────────────────
    this.blueyTex = new THREE.TextureLoader().load(spriteUrl);
    this.blueyTex.magFilter = this.blueyTex.minFilter = THREE.NearestFilter;
    this.blueyTex.colorSpace = THREE.SRGBColorSpace;
    this.blueyTex.repeat.set(1 / 8, 1);
    this.textures.push(this.blueyTex);
    // He stands behind the table at its head, so the rail in front of him
    // hides his feet the way a table would (depth-tested, unlike a sprite
    // that had to sit in front of a throne).
    this.bluey = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.blueyTex, transparent: true, alphaTest: 0.3 }));
    this.bluey.scale.setScalar(BLUEY_SCALE);
    this.bluey.position.set(DEALER_POS.x, BLUEY_Y, DEALER_POS.z);
    G.add(this.bluey);
    // a dealer's bow tie and a little stool behind him would be lost in the
    // dark; the brass lectern edge under the rack reads instead
    add(new THREE.BoxGeometry(1.4, 0.9, 0.5), walnut, DEALER_POS.x, 0.45, DEALER_POS.z - 0.35);

    // ── the one lamp ────────────────────────────────────────────────────
    {
      // low and compact, so from the stage camera it hangs below the sign
      // rather than across it
      const LY = 4.3;
      const prof: THREE.Vector2[] = [];
      for (let i = 0; i <= 12; i++) {
        const k = i / 12;
        prof.push(new THREE.Vector2(1.18 - Math.pow(k, 1.6) * 1.02, k * 0.56));
      }
      const outer = add(new THREE.LatheGeometry(prof, 48), std('#0f4a37', { e: '#0b5a3c', ei: 0.55, rough: 0.22, metal: 0.25 }), 0, LY, -0.15, false, false);
      void outer;
      this.lampGlow = own(std('#fff1d6', { e: '#FFC680', ei: 2.4, rough: 0.6 }));
      const inner = new THREE.Mesh(new THREE.LatheGeometry(prof.map((p) => new THREE.Vector2(p.x * 0.97, p.y * 0.97)), 48), this.lampGlow);
      inner.material.side = THREE.BackSide;
      inner.position.set(0, LY + 0.01, -0.15);
      G.add(inner);
      add(new THREE.TorusGeometry(1.18, 0.03, 8, 64), brass, 0, LY, -0.15, false, false).rotation.x = Math.PI / 2;
      add(new THREE.CylinderGeometry(0.16, 0.16, 0.1, 16), brass, 0, LY + 0.6, -0.15, false, false);
      add(new THREE.CylinderGeometry(0.015, 0.015, 6.2 - LY - 0.64, 6), brass, 0, (6.2 + LY + 0.64) / 2, -0.15, false, false);
      this.keyLight = new THREE.SpotLight('#FFD9A8', 110, 14, 0.82, 0.55, 2);
      this.keyLight.position.set(0, LY + 0.2, -0.15);
      this.keyLight.target.position.set(0, TABLE_Y, -0.2);
      this.keyLight.castShadow = true;
      this.keyLight.shadow.mapSize.set(1024, 1024);
      this.keyLight.shadow.bias = -0.0005;
      this.keyLight.shadow.normalBias = 0.02;
      this.keyLight.shadow.radius = 3;
      this.keyLight.shadow.camera.near = 1.5;
      this.keyLight.shadow.camera.far = 8;
      G.add(this.keyLight, this.keyLight.target);
      // the haze in the light: a faint cone, brightest at the shade
      const hazeTex = tex(canvasTex(8, 128, (g, w, h) => {
        const grd = g.createLinearGradient(0, 0, 0, h);
        grd.addColorStop(0, 'rgba(255,255,255,1)');
        grd.addColorStop(1, 'rgba(0,0,0,1)');
        g.fillStyle = grd; g.fillRect(0, 0, w, h);
      }));
      this.haze = new THREE.MeshBasicMaterial({ map: hazeTex, color: '#FFB866', transparent: true, opacity: 0.08, blending: THREE.AdditiveBlending, depthWrite: false });
      const hazeGeo = new THREE.CylinderGeometry(1.08, 3.1, LY - TABLE_Y - 0.05, 48, 1, true);
      hazeGeo.scale(1, 1, 0.66);
      add(hazeGeo, this.haze, 0, (LY + TABLE_Y) / 2, -0.15, false, false);
    }
    // a fill from the front so faces aren't lit only from above, a warm wash
    // on the back wall that rims the players, and the bar's glow
    const fill = new THREE.PointLight('#FFD2A0', 85, 22, 2);
    fill.position.set(0, 4.0, 7.5);
    G.add(fill);
    const wash = new THREE.PointLight('#FF9F55', 30, 11, 2);
    wash.position.set(0, 3.6, -5.0);
    G.add(wash);
    const barGlow = new THREE.PointLight('#FFA552', 9, 6, 2);
    barGlow.position.set(7.9, 2.6, -3.6);
    G.add(barGlow);

    // dust turning in the lamplight
    {
      const N = 220;
      const pos = new Float32Array(N * 3);
      const r = rng(41);
      for (let i = 0; i < N; i++) {
        const a = r() * Math.PI * 2, d = Math.sqrt(r()) * 2.4;
        pos[i * 3] = Math.cos(a) * d; pos[i * 3 + 1] = 1.2 + r() * 3.2; pos[i * 3 + 2] = Math.sin(a) * d * 0.7;
      }
      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
      this.dust = new THREE.Points(g, new THREE.PointsMaterial({ color: '#FFDDB0', size: 0.03, transparent: true, opacity: 0.5, blending: THREE.AdditiveBlending, depthWrite: false }));
      G.add(this.dust);
    }

    // PAYDAY: chips rain from the ceiling
    {
      const N = 260;
      this.rain = new THREE.InstancedMesh(this.chips.geo, this.chips.mat, N);
      const col = new THREE.Color();
      const pal = [CHIP.red, CHIP.green, CHIP.black, CHIP.purple, CHIP.blue, CHIP.gold, CHIP.white];
      for (let i = 0; i < N; i++) {
        this.rain.setColorAt(i, col.set(pal[i % pal.length]));
        this.rainState.push({ p: new THREE.Vector3(0, -10, 0), v: new THREE.Vector3(), r: new THREE.Euler(), w: new THREE.Vector3() });
      }
      this.rain.count = 0;
      this.rain.frustumCulled = false;
      this.rain.castShadow = false;
      G.add(this.rain);
    }

    mergeStatic(
      G,
      [this.tableTop, this.bluey, this.card, this.rain, ...this.spots.map((s) => s.lammer)],
      (m) => !this.own.has(m),
    );
    // The felt furniture shakes with the table, so it merges on its own:
    // everything on the table top that never moves relative to it.
    mergeStatic(T, [this.field.mesh, ...this.spots.map((s) => s.decor)], (m) => !this.own.has(m));
    for (const s of this.spots) mergeStatic(s.decor, [], () => true);

    // Built after the merge so it stays one object to show and hide.
    {
      const F = this.frontWall;
      const wall = new THREE.Mesh(new THREE.PlaneGeometry(17.6, 6.2), paper);
      wall.position.set(0, 3.1, 8.7);
      wall.rotation.y = Math.PI;
      wall.receiveShadow = true;
      const wains = new THREE.Mesh(new THREE.BoxGeometry(17.6, 1.3, 0.08), wainM);
      wains.position.set(0, 0.65, 8.66);
      const crown = new THREE.Mesh(new THREE.BoxGeometry(17.6, 0.26, 0.2), walnut);
      crown.position.set(0, 6.05, 8.6);
      F.add(wall, wains, crown);
      for (const x of [-4.5, 4.5]) {
        const p = new THREE.Mesh(new THREE.BoxGeometry(0.42, 6.0, 0.16), walnut);
        p.position.set(x, 3.0, 8.62);
        F.add(p);
      }
      // the doors are the way out on foot: say so, over them
      const c = document.createElement('canvas');
      c.width = 512; c.height = 160;
      const g = c.getContext('2d')!;
      g.fillStyle = '#071a0e';
      g.fillRect(0, 0, 512, 160);
      g.strokeStyle = '#C9A55A';
      g.lineWidth = 10;
      g.strokeRect(8, 8, 496, 144);
      g.fillStyle = '#86EFAC';
      g.font = '800 92px "Chakra Petch", sans-serif';
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText('◂ EXIT', 256, 86);
      const t = new THREE.CanvasTexture(c);
      t.colorSpace = THREE.SRGBColorSpace;
      this.textures.push(t);
      this.exitSign = new THREE.MeshBasicMaterial({ map: t, toneMapped: false });
      const sign = new THREE.Mesh(new THREE.PlaneGeometry(1.3, 0.41), this.exitSign);
      sign.position.set(DOOR_X, 3.75, BACK_Z + 0.12);
      F.add(sign);
      F.visible = false;
      G.add(F);
    }
    this.field.flush();
  }

  private lammerMat(txt: string, bg: string): THREE.MeshBasicMaterial {
    const t = canvasTex(128, 128, (g) => {
      g.clearRect(0, 0, 128, 128);
      g.fillStyle = '#f4efe2';
      g.beginPath(); g.arc(64, 64, 63, 0, Math.PI * 2); g.fill();
      g.fillStyle = bg;
      g.beginPath(); g.arc(64, 64, 55, 0, Math.PI * 2); g.fill();
      g.fillStyle = '#ffffff';
      g.font = `800 ${txt.length > 2 ? 44 : 54}px "Chakra Petch", sans-serif`;
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(txt, 64, 68);
    });
    this.textures.push(t);
    const m = new THREE.MeshBasicMaterial({ map: t, transparent: true, alphaTest: 0.5, side: THREE.DoubleSide, toneMapped: false, color: '#d8d8d8' });
    this.own.add(m);
    return m;
  }

  /** The felt: deep teal, a gold betting line, a ring at every seat's bet
   *  spot and the house mark where the market card lands. */
  private paintFelt(g: CanvasRenderingContext2D, w: number, h: number): void {
    const r = rng(17);
    const grd = g.createRadialGradient(w / 2, h * 0.5, 30, w / 2, h / 2, w * 0.55);
    grd.addColorStop(0, '#13705C');
    grd.addColorStop(0.65, '#0D5747');
    grd.addColorStop(1, '#083A30');
    g.fillStyle = grd;
    g.fillRect(0, 0, w, h);
    for (let i = 0; i < 9000; i++) {
      g.fillStyle = r() < 0.5 ? `rgba(0,0,0,${0.05 * r()})` : `rgba(160,255,220,${0.035 * r()})`;
      g.fillRect(r() * w, r() * h, 1.5, 1.5);
    }
    const X = (x: number) => (x / FELT_A * 0.5 + 0.5) * w;
    const Z = (z: number) => (z / FELT_B * 0.5 + 0.5) * h;
    const px = w / (2 * FELT_A);
    const gold = 'rgba(214,180,104,0.75)';
    g.strokeStyle = gold;
    g.lineWidth = 3;
    g.beginPath(); g.ellipse(w / 2, h / 2, w * 0.5 * 0.68, h * 0.5 * 0.68, 0, 0, Math.PI * 2); g.stroke();
    g.lineWidth = 1.5;
    g.beginPath(); g.ellipse(w / 2, h / 2, w * 0.5 * 0.66, h * 0.5 * 0.66, 0, 0, Math.PI * 2); g.stroke();
    // bet rings
    for (let i = 0; i < SEAT_DEG.length; i++) {
      const [bx, bz] = feltToward(i, 0.52);
      g.lineWidth = 2.5;
      g.beginPath(); g.arc(X(bx), Z(bz), 0.2 * px, 0, Math.PI * 2); g.stroke();
    }
    // the card's spot and the house mark in front of it
    const [cx, cz] = [X(0), Z(0.35)];
    g.setLineDash([8, 6]);
    g.lineWidth = 2;
    g.beginPath(); g.roundRect(cx - 0.27 * px, cz - 0.36 * px, 0.54 * px, 0.72 * px, 10); g.stroke();
    g.setLineDash([]);
    g.fillStyle = 'rgba(214,180,104,0.55)';
    g.textAlign = 'center'; g.textBaseline = 'middle';
    g.font = 'italic 700 44px Georgia, "Times New Roman", serif';
    g.fillText('Krypt PolyBot US', X(0), Z(0.98));
    g.font = '600 15px "Chakra Petch", sans-serif';
    g.fillText('YES  ·  NO  ·  NO LIMIT ON BEING RIGHT', X(0), Z(1.2));
    g.font = '400 40px Georgia, serif';
    g.fillText('♠', X(-1.55), Z(0.55));
    g.fillText('♠', X(1.55), Z(0.55));
  }

  /** Someone is walking the room (walk.ts): close the fourth wall, and
   *  shrink the dust — sized for a lens 13 m away, a mote at arm's length
   *  read as a white square. */
  setOnFoot(on: boolean): void {
    this.frontWall.visible = on;
    (this.dust.material as THREE.PointsMaterial).size = on ? 0.01 : 0.03;
    if (!on) this.exitNear = 0;
  }

  /** How close a walker is to the doors (0 far, 1 at them). */
  setExitNear(f: number): void {
    this.exitNear = Math.max(0, Math.min(1, f));
  }

  // ── painters ──────────────────────────────────────────────────────────

  /** A line for the hand history (newest at the bottom). */
  feed(text: string): void {
    this.historyLines.push(text);
    if (this.historyLines.length > 30) this.historyLines.shift();
    this.historyDirty = true;
  }

  private paintHistory(g: CanvasRenderingContext2D, w: number, h: number): void {
    g.fillStyle = '#0d1512';
    g.fillRect(0, 0, w, h);
    g.strokeStyle = 'rgba(201,165,90,0.4)';
    g.lineWidth = 2;
    g.strokeRect(14, 14, w - 28, h - 28);
    g.fillStyle = '#C9A55A';
    g.font = 'italic 700 40px Georgia, "Times New Roman", serif';
    g.textBaseline = 'middle';
    g.textAlign = 'left';
    g.fillText('Hand history', 34, 50);
    g.font = '600 18px "JetBrains Mono", monospace';
    g.fillStyle = 'rgba(201,165,90,0.6)';
    g.textAlign = 'right';
    g.fillText('table talk · live', w - 34, 52);
    g.textAlign = 'left';
    const lines = this.historyLines.slice(-8);
    g.font = '500 22px "JetBrains Mono", monospace';
    lines.forEach((l, k) => {
      const hand = l.startsWith('#');
      g.fillStyle = hand ? '#F2D58E' : `rgba(230,220,200,${0.45 + 0.55 * ((k + 1) / lines.length)})`;
      const s = l.length > 44 ? `${l.slice(0, 43)}…` : l;
      g.fillText(s, 34, 104 + k * 41);
    });
    if (!lines.length) {
      g.fillStyle = 'rgba(230,220,200,0.4)';
      g.fillText('waiting for the first hand…', 34, 110);
    }
  }

  /** The leaderboard: the personas' call accuracy, or the real
   *  forecasters' skill vs the market (seats.ts builds both views). */
  setScore(view: ScoreView): void {
    this.scoreView = view;
    this.score.redraw();
  }

  private paintScore(g: CanvasRenderingContext2D, w: number, h: number): void {
    const v = this.scoreView;
    const bg = g.createLinearGradient(0, 0, 0, h);
    bg.addColorStop(0, '#0f2620');
    bg.addColorStop(1, '#08130f');
    g.fillStyle = bg;
    g.fillRect(0, 0, w, h);
    g.strokeStyle = 'rgba(201,165,90,0.45)';
    g.lineWidth = 2;
    g.strokeRect(14, 14, w - 28, h - 28);
    g.fillStyle = '#E6C77E';
    g.font = 'italic 700 58px Georgia, "Times New Roman", serif';
    g.textBaseline = 'middle';
    g.textAlign = 'left';
    g.fillText(v.title, 36, 62);
    g.font = '600 22px "JetBrains Mono", monospace';
    g.fillStyle = 'rgba(201,165,90,0.7)';
    g.textAlign = 'right';
    g.fillText(v.caption, w - 36, 64);
    g.textAlign = 'left';
    const rh = (h - 128) / 6;
    v.lines.slice(0, 6).forEach((r, i) => {
      const y = 124 + i * rh + rh / 2;
      if (i % 2 === 0) {
        g.fillStyle = 'rgba(201,165,90,0.06)';
        g.fillRect(24, y - rh / 2 + 4, w - 48, rh - 8);
      }
      // a chip in the player's colour
      g.fillStyle = r.color;
      g.beginPath(); g.arc(58, y, 20, 0, Math.PI * 2); g.fill();
      g.strokeStyle = 'rgba(240,232,214,0.85)';
      g.lineWidth = 4;
      g.setLineDash([6, 6]);
      g.beginPath(); g.arc(58, y, 15, 0, Math.PI * 2); g.stroke();
      g.setLineDash([]);
      g.fillStyle = '#F1E6CC';
      // A real forecaster's name is longer than a persona's: it shrinks to
      // fit its column, and its sub line sits under it.
      const nameY = r.sub ? y - 12 : y + 2;
      let px = r.sub ? 32 : 42;
      g.font = `700 ${px}px "Chakra Petch", sans-serif`;
      while (px > 20 && g.measureText(r.name).width > 330) {
        px -= 2;
        g.font = `700 ${px}px "Chakra Petch", sans-serif`;
      }
      g.fillText(r.name, 96, nameY);
      if (r.sub) {
        g.font = '500 19px "JetBrains Mono", monospace';
        g.fillStyle = 'rgba(201,165,90,0.75)';
        g.fillText(r.sub.length > 40 ? `${r.sub.slice(0, 39)}…` : r.sub, 96, y + 22);
      }
      // bar: a fill from the left, or (signed) from the middle
      const bx = 450, bw = 300;
      g.fillStyle = 'rgba(201,165,90,0.12)';
      g.fillRect(bx, y - 11, bw, 22);
      if (r.bar !== null) {
        g.fillStyle = r.signed ? (r.bar >= 0 ? '#4ADE80' : '#F87171') : r.color;
        if (r.signed) {
          const mid = bx + bw / 2;
          const len = (bw / 2) * Math.min(1, Math.abs(r.bar));
          g.fillRect(r.bar >= 0 ? mid : mid - len, y - 11, len, 22);
          g.fillStyle = 'rgba(241,230,204,0.6)';
          g.fillRect(mid - 1, y - 15, 2, 30);
        } else {
          g.fillRect(bx, y - 11, bw * Math.max(0, Math.min(1, r.bar)), 22);
        }
      }
      g.textAlign = 'right';
      g.font = '700 48px "JetBrains Mono", monospace';
      g.fillStyle = r.tone === 'none' ? 'rgba(201,165,90,0.45)' : r.tone === 'good' ? '#86EFAC' : r.tone === 'bad' ? '#FCA5A5' : '#F1E6CC';
      g.fillText(r.value, w - 36, y + 2);
      g.textAlign = 'left';
    });
  }

  // ── pokes ──────────────────────────────────────────────────────────────

  /** How many chips seat `i` pushes on its next bet (2..10). */
  setBetSize(i: number, chips: number): void {
    const s = this.spots[i];
    if (s && s.betGoal === 0) s.betChips = Math.max(2, Math.min(BET_MAX, Math.round(chips)));
  }

  /** A seat's bet: chips pushed onto the felt for YES (green) or NO (red),
   *  or null to bring them back. */
  setBet(i: number, v: boolean | null): void {
    const s = this.spots[i];
    if (!s) return;
    if (v === null) {
      s.betGoal = 0;
      s.lamp.emissive.set('#000000');
      s.lamp.emissiveIntensity = 0;
      return;
    }
    s.betSide = v;
    s.betGoal = 1;
    const col = v ? CHIP.green : CHIP.red;
    for (let k = 0; k < BET_MAX; k++) this.field.color(s.betAt + k, col);
    s.lammer.material = v ? this.lammerYes : this.lammerNo;
    s.lamp.emissive.set(v ? '#22FF77' : '#FF3030');
    s.lamp.emissiveIntensity = 2.2;
  }

  /** How tall each seat's three stacks stand (its leaderboard record; an
   *  equal default when nothing is scored), and whether the seat is in play. */
  setStacks(i: number, total: number | null, inPlay: boolean): void {
    const s = this.spots[i];
    if (!s) return;
    s.decor.visible = inPlay;
    const n = inPlay ? Math.max(3, Math.min(STACKS * STACK_MAX, Math.round(total ?? 18))) : 0;
    s.stackH = [Math.ceil(n * 0.45), Math.ceil(n * 0.33), 0];
    s.stackH[2] = Math.max(0, n - s.stackH[0] - s.stackH[1]);
    s.stackH = s.stackH.map((x) => Math.min(STACK_MAX, x));
    if (!inPlay) this.setBet(i, null);
  }

  /** The dealer raps the table (the reveal; ORDER). */
  dealerTap(times = 1): void {
    this.tapT = 0;
    this.tapHits = times;
    this.blueyHop = 0.6;
  }

  setMood(color: string): void {
    this.moodColor.set(color);
  }

  /** The market card: dealt (and turned face up) or mucked. */
  setCard(dealt: boolean, faceUp: boolean): void {
    this.cardGoal = dealt ? 1 : 0;
    this.cardFlipGoal = dealt && faceUp ? 1 : 0;
  }

  /** Chips into the pot (a real settlement); returns where they land. */
  addPot(n: number): void {
    this.potGoal = Math.min(POT_STACKS * POT_PER, this.potGoal + n);
  }

  potTop(out: THREE.Vector3): THREE.Vector3 {
    return out.set(0, TABLE_Y + 0.25, -0.3);
  }

  /** PAYDAY: a shower of chips over the table. */
  rainChips(): void {
    this.rainT = 6;
    this.rain.count = this.rainState.length;
    for (const c of this.rainState) {
      c.p.set((Math.random() - 0.5) * 8, 5 + Math.random() * 3, (Math.random() - 0.5) * 5 - 0.4);
      c.v.set((Math.random() - 0.5) * 0.6, -1.2 - Math.random() * 1.6, (Math.random() - 0.5) * 0.6);
      c.r.set(Math.random() * 6, Math.random() * 6, Math.random() * 6);
      c.w.set((Math.random() - 0.5) * 10, (Math.random() - 0.5) * 10, (Math.random() - 0.5) * 10);
    }
  }

  private _m = new THREE.Matrix4();
  private _q = new THREE.Quaternion();
  private _one = new THREE.Vector3(1, 1, 1);

  update(dt: number, t: number, cam: THREE.Camera): void {
    // the lamp breathes, barely: a steady warm light is the premium read
    const f = 1 + Math.sin(t * 0.9) * 0.012;
    this.keyLight.intensity = 110 * f;
    this.lampGlow.emissiveIntensity = 2.4 * f;
    this.haze.opacity = 0.075 + Math.sin(t * 0.4) * 0.008;

    // the inlay: the showdown's colour, fading back to warm brass
    this.feltPulse = Math.max(0, this.feltPulse - dt * 0.9);
    this.inlay.emissive.copy(this.feltColor);
    this.inlay.emissiveIntensity = 0.3 + this.feltPulse * 2.4;

    // the neon takes the mood
    this.neon.color.lerp(this.moodColor, Math.min(1, dt * 3));
    this.exitSign.color.setScalar(0.6 + this.exitNear * 0.6);

    // history: repainted when something was said, at most a few times a second
    this.historyCd -= dt;
    if (this.historyDirty && this.historyCd <= 0) {
      this.historyDirty = false;
      this.historyCd = 0.3;
      this.history.redraw();
    }

    // stacks, bets and the pot (only what changed is rewritten)
    const ty = TABLE_Y + 0.008 + CHIP_H / 2;
    this.spots.forEach((s, i) => {
      for (let k = 0; k < STACKS; k++) {
        const hgt = s.stackH[k];
        if (hgt === s.stackShown[k]) continue;
        s.stackShown[k] = hgt;
        // the cards lie on the far side (+u, away from the dealer's rack)
        const off = (k - 1) * 0.215 - 0.08;
        for (let c = 0; c < STACK_MAX; c++) {
          const idx = s.stackAt + k * STACK_MAX + c;
          if (c < hgt) this.field.put(idx, s.stack[0] + s.ux * off, ty + c * CHIP_H, s.stack[1] + s.uz * off, c * 0.7);
          else this.field.hide(idx);
        }
      }
      const was = s.betK;
      s.betK += (s.betGoal - s.betK) * Math.min(1, dt * 7);
      if (Math.abs(s.betK - s.betGoal) < 0.002) s.betK = s.betGoal;
      const moving = s.betK !== was;
      const shown = s.betK > 0.01;
      s.lammer.visible = this.lammers && shown && s.betGoal === 1 && s.betK > 0.6;
      if (moving || (!shown && was !== 0)) {
        const e = s.betK * s.betK * (3 - 2 * s.betK);
        const x = s.stack[0] + (s.bet[0] - s.stack[0]) * e;
        const z = s.stack[1] + (s.bet[1] - s.stack[1]) * e;
        for (let c = 0; c < BET_MAX; c++) {
          if (shown && c < s.betChips) this.field.put(s.betAt + c, x + (c % 2) * 0.004, ty + c * CHIP_H, z, c * 0.9);
          else this.field.hide(s.betAt + c);
        }
      }
      if (s.lammer.visible) {
        s.lammer.position.set(s.bet[0], ty + s.betChips * CHIP_H + 0.12, s.bet[1]);
        s.lammer.rotation.set(0, Math.atan2(cam.position.x - s.bet[0], cam.position.z - s.bet[1]), 0);
      }
    });
    if (this.potShown !== this.potGoal) {
      // pour a few per frame: the growth is the payoff
      const step = Math.max(1, Math.ceil(Math.abs(this.potGoal - this.potShown) / 10));
      const to = this.potShown < this.potGoal ? Math.min(this.potGoal, this.potShown + step) : this.potGoal;
      for (let i = Math.min(this.potShown, to); i < Math.max(this.potShown, to); i++) {
        const st = i % POT_STACKS, lvl = Math.floor(i / POT_STACKS);
        const ring = st < 4 ? 0 : 1;
        const a = st < 4 ? st * (Math.PI / 2) + 0.4 : (st - 4) * (Math.PI / 4);
        const rr = ring ? 0.36 : 0.16;
        const idx = this.potAt + i;
        if (i < to) this.field.put(idx, Math.cos(a) * rr * 1.25, ty + lvl * CHIP_H, -0.34 + Math.sin(a) * rr * (ring ? 0.72 : 1), i * 0.5);
        else this.field.hide(idx);
      }
      this.potShown = to;
    }
    this.field.flush();

    // the market card: slide from the shoe, turn over; back to the dealer
    const wasK = this.cardK;
    this.cardK += (this.cardGoal - this.cardK) * Math.min(1, dt * 4.5);
    this.cardFlip += (this.cardFlipGoal - this.cardFlip) * Math.min(1, dt * 5);
    if (Math.abs(this.cardK - this.cardGoal) < 0.002) this.cardK = this.cardGoal;
    this.card.visible = this.cardK > 0.02 || wasK > 0.02;
    if (this.card.visible) {
      const k = this.cardK;
      this.card.position.set(0.8 * (1 - k), TABLE_Y + 0.02 + Math.sin(Math.PI * this.cardFlip) * 0.18 + (1 - k) * 0.12, -1.05 + (this.cardAnchor.z + 1.05) * k);
      this.card.rotation.set(0, (1 - k) * 0.6, Math.PI * (1 - this.cardFlip));
    }

    // the dealer raps: Bluey hops for each tap
    this.tapT += dt;
    if (this.tapHits > 0 && this.tapT < 0.3 * this.tapHits) {
      const c = (this.tapT % 0.3) / 0.3;
      this.blueyHop = Math.max(this.blueyHop, Math.sin(c * Math.PI) * 0.35);
    } else this.tapHits = 0;
    this.tableShake.v = Math.max(0, this.tableShake.v - dt * 4);
    this.tableTop.position.y = Math.sin(t * 60) * 0.008 * this.tableShake.v;

    this.blueyAcc += dt;
    if (this.blueyAcc > 0.9 / 8) {
      this.blueyAcc = 0;
      this.blueyFrame = (this.blueyFrame + 1) % 8;
      this.blueyTex.offset.x = this.blueyFrame / 8;
    }
    this.blueyHop = Math.max(0, this.blueyHop - dt);
    this.bluey.position.y = BLUEY_Y + Math.sin(t * 2) * 0.025 + (this.blueyHop > 0 ? Math.abs(Math.sin(this.blueyHop * 10)) * 0.22 : 0);

    // PAYDAY rain
    if (this.rainT > 0) {
      this.rainT -= dt;
      this.rainState.forEach((c, i) => {
        c.p.addScaledVector(c.v, dt);
        c.v.y -= 3.5 * dt;
        const floor = Math.abs(c.p.x) < RAIL_A && Math.abs(c.p.z) < RAIL_B ? TABLE_Y + 0.02 : 0.02;
        if (c.p.y < floor) { c.p.y = floor; c.v.set(0, 0, 0); c.r.x = 0; c.r.z = 0; }
        else { c.r.x += c.w.x * dt; c.r.y += c.w.y * dt; c.r.z += c.w.z * dt; }
        this._m.compose(c.p, this._q.setFromEuler(c.r), this._one);
        this.rain.setMatrixAt(i, this._m);
      });
      this.rain.instanceMatrix.needsUpdate = true;
      if (this.rainT <= 0) this.rain.count = 0;
    }

    this.dust.rotation.y += dt * 0.025;
    this.dust.position.y = Math.sin(t * 0.3) * 0.08;
  }

  get dealerHead(): THREE.Vector3 {
    return this._v.set(DEALER_POS.x, this.bluey.position.y + BLUEY_SCALE * 0.5, DEALER_POS.z);
  }

  dispose(): void {
    this.textures.forEach((t) => t.dispose());
    this.chips.dispose();
  }
}
