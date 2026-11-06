import * as THREE from 'three';
import { canvasTex } from '../casinoTextures';
import type { Btn, View } from '../../casino/ui';

// The pieces every immersive table is built from (tables/*.ts). You don't
// play through a panel: you sit down, the camera frames the table, and the
// table is the interface — chips you click to bet with, acrylic plaques you
// click to act, the felt you click to place a chip, a machine's own buttons
// and lever, a terminal's screen. Text that has to be read in a hurry (a
// hand's total, a result) floats over the table, anchored to it.

/** A word floating over the table, anchored to a point in it. */
export interface WorldLabel {
  at: THREE.Vector3;
  text: string;
  sub?: string;
  color: string;
  /** 'tag': a small pill (a total, a stake). 'big': a result that rises and
   *  fades. 'tip': a quiet note. */
  kind: 'tag' | 'big' | 'tip';
  /** For 'big': seconds since it appeared, and how long it stays. */
  age?: number;
  life?: number;
}

/** What the camera does while you sit at a table. */
export interface TableView {
  eye: THREE.Vector3;
  look: THREE.Vector3;
  fov: number;
}

/** Something you can click. */
export interface Hit {
  id: string;
  tip: string;
}

/** A table, set up for you to play at it. */
export abstract class TableRig {
  readonly group = new THREE.Group();
  protected owned: { dispose(): void }[] = [];
  /** Meshes you can click, with what clicking one does and says. */
  protected spots: { mesh: THREE.Object3D; id: string; tip: () => string; on: () => boolean }[] = [];
  protected hovered: string | null = null;

  abstract view(): TableView;
  abstract update(dt: number, t: number): void;
  abstract labels(): WorldLabel[];

  /** What's under the cursor (the ray from it). */
  pick(ray: THREE.Raycaster): Hit | null {
    const meshes = this.spots.filter((s) => s.on()).map((s) => s.mesh);
    const hits = ray.intersectObjects(meshes, true);
    for (const h of hits) {
      for (let o: THREE.Object3D | null = h.object; o; o = o.parent) {
        const s = this.spots.find((x) => x.mesh === o);
        if (s) return { id: s.id, tip: s.tip() };
      }
    }
    return null;
  }

  hover(id: string | null): void {
    this.hovered = id;
  }

  protected keep<T extends { dispose(): void }>(x: T): T {
    this.owned.push(x);
    return x;
  }

  dispose(): void {
    this.group.removeFromParent();
    for (const o of this.owned) o.dispose();
    this.owned = [];
  }
}

// ── chips ──────────────────────────────────────────────────────────────────

export const CHIP_COLOR: Record<number, string> = { 1: '#e5e7eb', 5: '#dc2626', 25: '#16a34a', 100: '#1f2937', 500: '#7c3aed', 1000: '#eab308' };
const DENOMS = [1000, 500, 100, 25, 5, 1];
export const CHIP_R = 0.04;
export const CHIP_H = 0.009;

/** An amount as the chips a dealer would stack it in, biggest first. */
export function chipsFor(amount: number, max = 18): number[] {
  const out: number[] = [];
  let left = Math.round(amount);
  for (const d of DENOMS) {
    while (left >= d && out.length < max) {
      out.push(d);
      left -= d;
    }
  }
  return out;
}

function chipTexture(): THREE.CanvasTexture {
  // white with edge spots: the instance colour tints it, the spots stay pale
  const t = canvasTex(64, 64, (g, w, h) => {
    g.fillStyle = '#ffffff';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#5a5a5a';
    for (let i = 0; i < 8; i++) g.fillRect((i * w) / 8 + 2, 0, w / 16, h);
    g.fillStyle = '#ffffff';
    g.beginPath(); g.arc(w / 2, h / 2, w * 0.3, 0, Math.PI * 2); g.fill();
    g.strokeStyle = '#8a8a8a';
    g.lineWidth = 2;
    g.beginPath(); g.arc(w / 2, h / 2, w * 0.22, 0, Math.PI * 2); g.stroke();
  });
  t.magFilter = THREE.NearestFilter;
  return t;
}

export interface Stack {
  key: string;
  x: number;
  y: number;
  z: number;
  amount: number;
}

/** Chips on a table: one instanced draw. Stacks are set by key; a stack
 *  that moves (paid out, swept away) slides there. */
export class ChipStacks {
  readonly mesh: THREE.InstancedMesh;
  private cur = new Map<string, { x: number; y: number; z: number; amount: number; tx: number; tz: number; fade: number }>();
  private _m = new THREE.Matrix4();
  private _c = new THREE.Color();

  constructor(max = 320) {
    const geo = new THREE.CylinderGeometry(CHIP_R, CHIP_R, CHIP_H, 14);
    this.mesh = new THREE.InstancedMesh(geo, new THREE.MeshStandardMaterial({ map: chipTexture(), roughness: 0.45 }), max);
    this.mesh.count = 0;
    this.mesh.frustumCulled = false;
  }

  /** Lay out exactly these stacks (others vanish, unless sliding away). */
  set(stacks: Stack[]): void {
    const keys = new Set(stacks.map((s) => s.key));
    for (const [k, v] of this.cur) if (!keys.has(k) && v.fade <= 0) this.cur.delete(k);
    for (const s of stacks) {
      const c = this.cur.get(s.key);
      if (c) {
        c.amount = s.amount;
        c.tx = s.x; c.tz = s.z; c.y = s.y;
      } else {
        this.cur.set(s.key, { x: s.x, y: s.y, z: s.z, amount: s.amount, tx: s.x, tz: s.z, fade: 0 });
      }
    }
  }

  /** Send a stack somewhere and let it go (paid to you, swept by the house). */
  slide(key: string, x: number, z: number): void {
    const c = this.cur.get(key);
    if (!c) return;
    c.tx = x; c.tz = z; c.fade = 0.9;
  }

  update(dt: number): void {
    let n = 0;
    const k = 1 - Math.exp(-8 * dt);
    for (const [key, s] of this.cur) {
      s.x += (s.tx - s.x) * k;
      s.z += (s.tz - s.z) * k;
      if (s.fade > 0) {
        s.fade -= dt;
        if (s.fade <= 0) { this.cur.delete(key); continue; }
      }
      const chips = chipsFor(s.amount);
      for (let i = 0; i < chips.length && n < this.mesh.instanceMatrix.count; i++) {
        // a little jitter, so a stack reads as a stack of real chips
        const jx = Math.sin(i * 7.3 + s.x * 13) * 0.0025, jz = Math.cos(i * 5.1 + s.z * 11) * 0.0025;
        this._m.makeTranslation(s.x + jx, s.y + CHIP_H / 2 + i * CHIP_H, s.z + jz);
        this.mesh.setMatrixAt(n, this._m);
        this.mesh.setColorAt(n, this._c.set(CHIP_COLOR[chips[i]] ?? '#e5e7eb'));
        n++;
      }
    }
    this.mesh.count = n;
    this.mesh.instanceMatrix.needsUpdate = true;
    if (this.mesh.instanceColor) this.mesh.instanceColor.needsUpdate = true;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    const m = this.mesh.material as THREE.MeshStandardMaterial;
    m.map?.dispose();
    m.dispose();
  }
}

/** A single chip of a value, for a rack of them to pick from. */
export function chipMesh(value: number): THREE.Mesh {
  const geo = new THREE.CylinderGeometry(CHIP_R * 1.15, CHIP_R * 1.15, CHIP_H * 4, 16);
  const tex = canvasTex(128, 128, (g, w, h) => {
    g.fillStyle = CHIP_COLOR[value] ?? '#e5e7eb';
    g.fillRect(0, 0, w, h);
    g.fillStyle = 'rgba(255,255,255,0.85)';
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2;
      g.fillRect(w / 2 + Math.cos(a) * w * 0.42 - 5, h / 2 + Math.sin(a) * h * 0.42 - 5, 10, 10);
    }
    g.fillStyle = '#ffffff';
    g.font = '800 40px "Chakra Petch", sans-serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(String(value), w / 2, h / 2 + 2);
  });
  const mat = new THREE.MeshStandardMaterial({ map: tex, roughness: 0.4, emissive: '#ffffff', emissiveIntensity: 0 });
  const m = new THREE.Mesh(geo, mat);
  return m;
}

// ── plaques: the acrylic markers you click to act ───────────────────────────

/** A lit acrylic plaque lying on the felt with a word on it. */
export class Plaque {
  readonly mesh: THREE.Mesh;
  private mat: THREE.MeshStandardMaterial;
  private tex: THREE.CanvasTexture;
  private lit = 0;

  constructor(label: string, readonly color: string, w = 0.15, d = 0.058) {
    this.tex = canvasTex(256, 100, (g, cw, ch) => {
      g.fillStyle = '#0b0910';
      g.fillRect(0, 0, cw, ch);
      g.strokeStyle = color;
      g.lineWidth = 8;
      g.strokeRect(6, 6, cw - 12, ch - 12);
      g.fillStyle = '#ffffff';
      g.font = `800 ${label.length > 6 ? 42 : 50}px "Chakra Petch", sans-serif`;
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText(label, cw / 2, ch / 2 + 3);
    });
    this.mat = new THREE.MeshStandardMaterial({
      map: this.tex, emissive: '#ffffff', emissiveMap: this.tex, emissiveIntensity: 0.3, roughness: 0.3,
    });
    const geo = new THREE.BoxGeometry(w, 0.012, d);
    this.mesh = new THREE.Mesh(geo, [
      new THREE.MeshStandardMaterial({ color, roughness: 0.4 }), new THREE.MeshStandardMaterial({ color, roughness: 0.4 }),
      this.mat, this.mat,
      new THREE.MeshStandardMaterial({ color, roughness: 0.4 }), new THREE.MeshStandardMaterial({ color, roughness: 0.4 }),
    ]);
  }

  /** Lit when it does something now, brighter under the cursor. */
  set(on: boolean, hot: boolean, dt: number, t: number): void {
    const want = !on ? 0.08 : hot ? 1.6 + Math.sin(t * 10) * 0.2 : 0.75;
    this.lit += (want - this.lit) * Math.min(1, dt * 12);
    this.mat.emissiveIntensity = this.lit;
    this.mesh.position.y = this.baseY + (hot && on ? 0.006 : 0);
  }

  private baseY = 0;
  place(x: number, y: number, z: number, rotY = 0): this {
    this.baseY = y;
    this.mesh.position.set(x, y, z);
    this.mesh.rotation.y = rotY;
    return this;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    for (const m of this.mesh.material as THREE.Material[]) m.dispose();
    this.tex.dispose();
  }
}

// ── terminals: a screen in the world that a session paints ──────────────────

/** A screen in the world, painted by a session's paint() (the betting
 *  terminal, the cashier's ledger, the bar's menu). A click on it comes back
 *  as one of the buttons the session drew. */
export class Terminal {
  readonly canvas = document.createElement('canvas');
  readonly tex: THREE.CanvasTexture;
  readonly mesh: THREE.Mesh;
  private g: CanvasRenderingContext2D;
  private btns: Btn[] = [];
  private acc = 1;

  constructor(readonly w: number, readonly h: number, width: number, private paint: (g: CanvasRenderingContext2D, v: View) => Btn[], private bg = '#06050c') {
    this.canvas.width = w;
    this.canvas.height = h;
    this.g = this.canvas.getContext('2d')!;
    this.tex = new THREE.CanvasTexture(this.canvas);
    this.tex.colorSpace = THREE.SRGBColorSpace;
    this.tex.anisotropy = 4;
    this.mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(width, (width * h) / w),
      new THREE.MeshBasicMaterial({ map: this.tex, toneMapped: false }),
    );
  }

  update(dt: number, t: number): void {
    this.acc += dt;
    if (this.acc < 1 / 20) return;
    this.acc = 0;
    const g = this.g;
    g.fillStyle = this.bg;
    g.fillRect(0, 0, this.w, this.h);
    this.btns = this.paint(g, { W: this.w, H: this.h, u: 1, t, fill: true });
    this.tex.needsUpdate = true;
  }

  /** The button under a point of the screen (its uv), if any. */
  hit(uv: THREE.Vector2): Btn | null {
    const x = uv.x * this.w, y = (1 - uv.y) * this.h;
    return this.btns.find((b) => b.enabled && x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) ?? null;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    (this.mesh.material as THREE.Material).dispose();
    this.tex.dispose();
  }
}

/** A glow ring under the cursor or round something selected. */
export function ringMesh(r: number, color: string): THREE.Mesh {
  const m = new THREE.Mesh(
    new THREE.RingGeometry(r * 0.78, r, 24),
    new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.85, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }),
  );
  m.rotation.x = -Math.PI / 2;
  return m;
}
