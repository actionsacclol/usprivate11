import * as THREE from 'three';
import { colorOf, payoutMultiplier } from '../../casino/roulette';
import { CHIP_VALUES, betFromKey, betKey, type RouletteSession } from '../../casino/sessions';
import { chipsText } from '../../casino/ui';
import { RL } from '../casinoTextures';
import type { CasinoFloor } from '../floor';
import { PROP } from '../layout';
import { ChipStacks, Plaque, TableRig, chipMesh, ringMesh, type Hit, type TableView, type WorldLabel } from './common';

// Roulette, leaning over the layout. You bet on the felt itself: pick a chip
// off the rail, click a number, a colour, a dozen, a column, and the chip
// lands there (stacks grow as you add). SPIN on the rail sends the ball, and
// the camera turns to watch the wheel; when it drops, a marker sits on the
// winning number, winners are paid beside their stake and slide to you,
// losers are swept to the wheel.

const LAYOUT_W = 2.4;
const LAYOUT_D = 1.02;
const TOP_Y = 0.796;

/** A cell of the layout, in its texture's pixels, and the bet it is. */
interface Cell { key: string; x: number; y: number; w: number; h: number }

function cells(): Cell[] {
  const { gx, gy, gw, gh, colW, dy, dh, ey, eh } = RL;
  const out: Cell[] = [{ key: 'n0', x: gx - 90, y: gy, w: 90, h: gh }];
  for (let i = 0; i < 12; i++) for (let j = 0; j < 3; j++) {
    out.push({ key: `n${i * 3 + (3 - j)}`, x: gx + (i * gw) / 12, y: gy + (j * gh) / 3, w: gw / 12, h: gh / 3 });
  }
  for (let j = 0; j < 3; j++) out.push({ key: `c${3 - j}`, x: gx + gw, y: gy + (j * gh) / 3, w: colW, h: gh / 3 });
  for (let i = 0; i < 3; i++) out.push({ key: `d${i + 1}`, x: gx + (i * gw) / 3, y: dy, w: gw / 3, h: dh });
  ['low', 'even', 'red', 'black', 'odd', 'high'].forEach((k, i) => out.push({ key: k, x: gx + (i * gw) / 6, y: ey, w: gw / 6, h: eh }));
  return out;
}

const NAMES: Record<string, string> = {
  red: 'RED', black: 'BLACK', odd: 'ODD', even: 'EVEN', low: '1–18', high: '19–36',
  d1: '1st 12', d2: '2nd 12', d3: '3rd 12', c1: 'Column 1', c2: 'Column 2', c3: 'Column 3',
};
const nameOf = (k: string) => NAMES[k] ?? k.slice(1);

export class RouletteRig extends TableRig {
  private cells = cells();
  private stacks = new ChipStacks(420);
  private chips: { v: number; mesh: THREE.Mesh; y: number }[] = [];
  private plaques = new Map<string, Plaque>();
  private glow: THREE.Mesh;
  private dolly: THREE.Mesh;
  private sel: THREE.Mesh;
  private hoverCell: Cell | null = null;
  private seen: object | null = null;
  private outcomeAt = 0;
  private clock = 0;
  private layout: THREE.Mesh;
  private lx: number;
  private lz: number;

  constructor(private s: RouletteSession, floor: CasinoFloor) {
    super();
    this.layout = floor.rouletteLayout;
    [this.lx, this.lz] = PROP.layout;
    this.group.add(this.stacks.mesh);
    this.keep(this.stacks);
    // the chip you bet with: four on the rail, the one in hand ringed
    const railZ = this.lz + 0.59, railY = 0.86;
    CHIP_VALUES.forEach((v, i) => {
      const m = chipMesh(v);
      m.position.set(this.lx - 1.05 + i * 0.13, railY, railZ);
      this.group.add(m);
      this.keep(m.geometry);
      this.keep(m.material as THREE.Material);
      this.chips.push({ v, mesh: m, y: railY });
      this.spots.push({ mesh: m, id: `chip:${v}`, tip: () => `Bet with ${chipsText(v)} chips`, on: () => !this.s.spinning });
    });
    this.sel = ringMesh(0.055, '#FDE68A');
    this.group.add(this.sel);
    this.keep(this.sel.geometry);
    this.keep(this.sel.material as THREE.Material);
    // SPIN, UNDO, CLEAR on the rail to the right
    for (const [id, label, color, dx, tip] of [
      ['spin', 'SPIN', '#38bdf8', 0.82, 'Spin the wheel (Space)'],
      ['undo', 'UNDO', '#f59e0b', 0.52, 'Take back your last chip (U)'],
      ['clear', 'CLEAR', '#ef4444', 0.25, 'Take all your bets back (C)'],
    ] as const) {
      const p = new Plaque(label, color, id === 'spin' ? 0.22 : 0.17, 0.07);
      p.place(this.lx + dx, railY - 0.005, railZ);
      this.group.add(p.mesh);
      this.keep(p);
      this.plaques.set(id, p);
      this.spots.push({ mesh: p.mesh, id, tip: () => tip, on: () => this.can(id) });
    }
    // a glow over the cell under the cursor, and the winning number's marker
    this.glow = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
      color: '#ffffff', transparent: true, opacity: 0.22, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false,
    }));
    this.glow.rotation.x = -Math.PI / 2;
    this.glow.visible = false;
    this.group.add(this.glow);
    this.keep(this.glow.geometry);
    this.keep(this.glow.material as THREE.Material);
    this.dolly = new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.035, 0.09, 8), new THREE.MeshStandardMaterial({
      color: '#e8f4ff', emissive: '#bfe6ff', emissiveIntensity: 1.2, roughness: 0.1, metalness: 0.3,
    }));
    this.dolly.visible = false;
    this.group.add(this.dolly);
    this.keep(this.dolly.geometry);
    this.keep(this.dolly.material as THREE.Material);
  }

  private can(id: string): boolean {
    const busy = !!this.s.spinning;
    if (id === 'spin') return !busy && this.s.bets.length > 0;
    return !busy && this.s.bets.length > 0;
  }

  /** A point of the texture, on the felt. */
  private at(px: number, py: number): THREE.Vector3 {
    return new THREE.Vector3(this.lx - LAYOUT_W / 2 + (px / RL.w) * LAYOUT_W, TOP_Y, this.lz - LAYOUT_D / 2 + (py / RL.h) * LAYOUT_D);
  }

  private centre(key: string): THREE.Vector3 {
    const c = this.cells.find((x) => x.key === key);
    return c ? this.at(c.x + c.w / 2, c.y + c.h / 2) : this.at(RL.w / 2, RL.h / 2);
  }

  view(): TableView {
    const watching = !!this.s.spinning || (this.s.outcome !== null && this.s.outcome.age < 1.8);
    if (watching) {
      const [wx, wz] = PROP.wheel;
      return { eye: new THREE.Vector3(wx + 0.95, 1.6, wz + 1.15), look: new THREE.Vector3(wx, 0.78, wz), fov: 48 };
    }
    return { eye: new THREE.Vector3(this.lx + 0.05, 1.72, this.lz + 1.5), look: new THREE.Vector3(this.lx + 0.05, TOP_Y, this.lz + 0.08), fov: 52 };
  }

  pick(ray: THREE.Raycaster): Hit | null {
    const spot = super.pick(ray);
    if (spot) { this.hoverCell = null; return spot; }
    this.hoverCell = null;
    if (this.s.spinning) return null;
    const hit = ray.intersectObject(this.layout, false)[0];
    if (!hit?.uv) return null;
    const px = hit.uv.x * RL.w, py = (1 - hit.uv.y) * RL.h;
    const c = this.cells.find((x) => px >= x.x && px <= x.x + x.w && py >= x.y && py <= x.y + x.h);
    if (!c) return null;
    this.hoverCell = c;
    const b = betFromKey(c.key);
    const on = this.s.stakeOn(c.key);
    const pays = b ? `pays ${payoutMultiplier(b)}:1` : '';
    return { id: `bet:${c.key}`, tip: `${nameOf(c.key)} · ${pays} · put ${chipsText(this.s.stake)} on it${on ? ` (${chipsText(on)} there)` : ''}` };
  }

  update(dt: number, t: number): void {
    this.clock += dt;
    for (const [id, p] of this.plaques) p.set(this.can(id), this.hovered === id, dt, t);
    for (const c of this.chips) {
      const hot = this.hovered === `chip:${c.v}`;
      const chosen = this.s.stake === c.v;
      c.mesh.position.y += ((c.y + (chosen ? 0.03 : 0) + (hot ? 0.015 : 0)) - c.mesh.position.y) * Math.min(1, dt * 14);
      (c.mesh.material as THREE.MeshStandardMaterial).emissiveIntensity = hot ? 0.35 : chosen ? 0.15 : 0;
      if (chosen) this.sel.position.set(c.mesh.position.x, 0.835, c.mesh.position.z);
    }
    (this.sel.material as THREE.MeshBasicMaterial).opacity = 0.6 + Math.sin(t * 5) * 0.3;
    // the cell under the cursor
    const hc = this.hoverCell;
    this.glow.visible = !!hc && !this.s.spinning;
    if (hc) {
      const p = this.at(hc.x + hc.w / 2, hc.y + hc.h / 2);
      this.glow.position.set(p.x, TOP_Y + 0.003, p.z);
      this.glow.scale.set((hc.w / RL.w) * LAYOUT_W, (hc.h / RL.h) * LAYOUT_D, 1);
      (this.glow.material as THREE.MeshBasicMaterial).opacity = 0.18 + Math.sin(t * 8) * 0.06;
    }
    // the chips on the felt: your bets, or the last spin's being paid and swept
    const o = this.s.outcome;
    if (o && o !== this.seen) {
      this.seen = o;
      this.outcomeAt = this.clock;
      const stacks = o.bets.map((b) => ({ key: `b:${b.key}`, ...this.centre(b.key), amount: b.stake }));
      const pays = o.bets.filter((b) => b.back > 0).map((b) => {
        const c = this.centre(b.key);
        return { key: `p:${b.key}`, x: c.x + 0.07, y: c.y, z: c.z, amount: b.back - b.stake };
      });
      this.stacks.set([...stacks, ...pays]);
      const n = this.centre(`n${o.n}`);
      this.dolly.position.set(n.x, TOP_Y + 0.045, n.z);
    }
    this.dolly.visible = !!o && o.age < 8;
    if (o && this.clock - this.outcomeAt > 2.2 && this.s.bets.length === 0 && !this.s.spinning) {
      // swept: the winners slide to you, the rest go to the wheel
      for (const b of o.bets) {
        const c = this.centre(b.key);
        if (b.back > 0) {
          this.stacks.slide(`b:${b.key}`, c.x, this.lz + 1.1);
          this.stacks.slide(`p:${b.key}`, c.x, this.lz + 1.1);
        } else {
          this.stacks.slide(`b:${b.key}`, PROP.wheel[0] + 0.6, PROP.wheel[1]);
        }
      }
      this.stacks.set([]);
      this.outcomeAt = Infinity;
    }
    if (!o || this.s.bets.length > 0 || this.s.spinning) {
      this.stacks.set(this.s.bets.map((b) => ({ key: `b:${betKey(b.bet)}`, ...this.centre(betKey(b.bet)), amount: b.stake })));
    }
    this.stacks.update(dt);
  }

  labels(): WorldLabel[] {
    const out: WorldLabel[] = [];
    const total = this.s.bets.reduce((a, b) => a + b.stake, 0);
    if (total > 0) out.push({ at: new THREE.Vector3(this.lx - 0.3, 0.86, this.lz + 0.66), text: chipsText(total), sub: 'ON THE TABLE', color: '#FDE68A', kind: 'tag' });
    const o = this.s.outcome;
    if (o && o.age < 6) {
      const col = colorOf(o.n);
      const net = o.bets.reduce((a, b) => a + b.back - b.stake, 0);
      // over the wheel while you watch it, then over the winning number
      const at = o.age < 1.8 ? new THREE.Vector3(PROP.wheel[0], 1.12, PROP.wheel[1]) : this.centre(`n${o.n}`).setY(TOP_Y + 0.3);
      out.push({
        at, text: `${o.n} ${col.toUpperCase()}`, sub: net > 0 ? `you win ${chipsText(net)}` : net < 0 ? `−${chipsText(-net)}` : o.bets.length ? 'even' : undefined,
        color: col === 'red' ? '#F87171' : col === 'green' ? '#4ADE80' : '#E5E7EB', kind: 'big', age: o.age < 1.8 ? o.age : o.age - 1.8, life: o.age < 1.8 ? 9 : 4.2,
      });
    }
    if (this.s.msg && (!o || this.s.msg.age < 2)) {
      if (!this.s.msg.text.match(/^\d/)) out.push({ at: new THREE.Vector3(this.lx, 1.0, this.lz), text: this.s.msg.text, color: this.s.msg.color, kind: 'big', age: this.s.msg.age });
    }
    return out;
  }
}
