import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import { cacheMeasureText } from '../text';
import type { Desk } from './model';
import { CleanKit, SEAT_Y, SeatedRat, furFor } from './rat';
import { paintCalls, paintMarket, paintPulse, paintRecord, paintStatus, type Painter } from './screens';
import type { Smoke } from '../engine/smoke';

// One agent's battlestation: a black desk with an underglow in the agent's
// colour, five monitors on arms (tool calls, the last market, the forecast
// record, the activity strip, a portrait status screen), a keyboard, a
// gaming chair, an energy can on the go with the empties lined up behind it,
// and an ashtray. The empties and the butts are honest too: one empty per 20
// real tool calls this session, one butt per refusal.

export const DESK_W = 2.9;
export const DESK_D = 0.8;
export const TOP_Y = 0.64;
/** Where the chair stands, in front of the desk (the rat faces -z). */
export const CHAIR_Z = 0.7;

function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  g.beginPath();
  g.roundRect(x, y, w, h, r);
}

function tex(w: number, h: number, draw: (g: CanvasRenderingContext2D) => void): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d')!);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

/** Shared geometry and materials for every desk in the room. */
export class FurnitureKit {
  readonly geo = {
    top: new RoundedBoxGeometry(DESK_W, 0.045, DESK_D, 3, 0.02),
    leg: new RoundedBoxGeometry(0.05, TOP_Y, DESK_D - 0.12, 2, 0.015),
    box: new THREE.BoxGeometry(1, 1, 1),
    plane: new THREE.PlaneGeometry(1, 1),
    pole: new THREE.CylinderGeometry(0.018, 0.018, 1, 12),
    foot: new THREE.CylinderGeometry(0.11, 0.12, 0.018, 24),
    seat: new RoundedBoxGeometry(0.52, 0.09, 0.5, 3, 0.04),
    back: new RoundedBoxGeometry(0.5, 0.78, 0.09, 3, 0.04),
    head: new RoundedBoxGeometry(0.34, 0.16, 0.08, 3, 0.03),
    arm: new RoundedBoxGeometry(0.06, 0.04, 0.3, 2, 0.015),
    spoke: new RoundedBoxGeometry(0.34, 0.035, 0.05, 2, 0.012),
    wheel: new THREE.SphereGeometry(0.035, 10, 8),
    can: new THREE.CylinderGeometry(0.042, 0.042, 0.15, 20),
    canTop: new THREE.CylinderGeometry(0.036, 0.042, 0.012, 20),
    tray: new THREE.CylinderGeometry(0.075, 0.065, 0.026, 24),
    butt: new THREE.CylinderGeometry(0.009, 0.009, 0.05, 8),
    kbd: new RoundedBoxGeometry(0.52, 0.02, 0.16, 2, 0.008),
    mouse: new RoundedBoxGeometry(0.055, 0.024, 0.09, 3, 0.012),
  };
  readonly mat = {
    top: new THREE.MeshStandardMaterial({ color: '#121418', roughness: 0.42, metalness: 0.25 }),
    metal: new THREE.MeshStandardMaterial({ color: '#0e0f12', roughness: 0.32, metalness: 0.7 }),
    bezel: new THREE.MeshStandardMaterial({ color: '#08090b', roughness: 0.3, metalness: 0.35 }),
    pole: new THREE.MeshStandardMaterial({ color: '#2a2d33', roughness: 0.22, metalness: 0.85 }),
    mat: new THREE.MeshStandardMaterial({ color: '#0b0c0f', roughness: 0.95 }),
    kbd: new THREE.MeshStandardMaterial({ color: '#0c0d10', roughness: 0.5, metalness: 0.2 }),
    chair: new THREE.MeshStandardMaterial({ color: '#1b1e25', roughness: 0.88 }),
    chairBase: new THREE.MeshStandardMaterial({ color: '#15171b', roughness: 0.3, metalness: 0.75 }),
    can: new THREE.MeshStandardMaterial({ roughness: 0.32, metalness: 0.55 }),
    canTop: new THREE.MeshStandardMaterial({ color: '#c9ced6', roughness: 0.25, metalness: 0.95 }),
    tray: new THREE.MeshStandardMaterial({ color: '#1d2026', roughness: 0.12, metalness: 0.2 }),
    butt: new THREE.MeshStandardMaterial({ color: '#ECE6DA', roughness: 0.8 }),
    keys: new THREE.MeshBasicMaterial({ toneMapped: false, transparent: true }),
  };
  private byColor = new Map<string, THREE.Material>();
  private poolTex: THREE.CanvasTexture;

  constructor() {
    // A generic energy drink: matte black, a neon slash, a bolt. No brand.
    this.mat.can.map = tex(256, 128, (g) => {
      g.fillStyle = '#0a0b0d';
      g.fillRect(0, 0, 256, 128);
      g.fillStyle = '#39FF6A';
      g.save();
      g.translate(128, 64);
      g.rotate(-0.35);
      g.fillRect(-90, -9, 180, 18);
      g.restore();
      g.beginPath();
      g.moveTo(70, 14); g.lineTo(52, 66); g.lineTo(66, 66); g.lineTo(56, 114); g.lineTo(86, 52); g.lineTo(71, 52); g.lineTo(84, 14);
      g.closePath();
      g.fill();
      g.font = '800 30px "Chakra Petch", sans-serif';
      g.fillStyle = '#E5FFE9';
      g.fillText('VOLT', 150, 100);
    });
    this.mat.keys.map = tex(512, 160, (g) => {
      g.clearRect(0, 0, 512, 160);
      const grd = g.createLinearGradient(0, 0, 512, 0);
      for (const [o, c] of [[0, '#FF3D7F'], [0.25, '#A855F7'], [0.5, '#38BDF8'], [0.75, '#34D399'], [1, '#FBBF24']] as const) grd.addColorStop(o, c);
      for (let r = 0; r < 5; r++) {
        for (let c = 0; c < 15; c++) {
          const w = r === 4 && c > 4 && c < 10 ? 0 : 28;
          if (!w) continue;
          g.fillStyle = grd;
          g.globalAlpha = 0.5;
          roundRect(g, 10 + c * 33, 8 + r * 30, w, 24, 4);
          g.fill();
        }
      }
      g.globalAlpha = 0.5;
      roundRect(g, 10 + 5 * 33, 8 + 4 * 30, 33 * 5 - 5, 24, 4);
      g.fill();
    });
    this.poolTex = tex(256, 256, (g) => {
      const r = g.createRadialGradient(128, 128, 0, 128, 128, 128);
      r.addColorStop(0, 'rgba(255,255,255,0.9)');
      r.addColorStop(0.4, 'rgba(255,255,255,0.35)');
      r.addColorStop(1, 'rgba(255,255,255,0)');
      g.fillStyle = r;
      g.fillRect(0, 0, 256, 256);
    });
  }

  /** The agent-coloured pieces: the LED strip (glows), a chair stripe. */
  led(color: string): THREE.MeshStandardMaterial {
    const m = new THREE.MeshStandardMaterial({ color: '#000000', emissive: color, emissiveIntensity: 1.2 });
    return m;
  }

  accent(color: string): THREE.Material {
    let m = this.byColor.get(color);
    if (!m) {
      m = new THREE.MeshStandardMaterial({ color, roughness: 0.7 });
      this.byColor.set(color, m);
    }
    return m;
  }

  pool(color: string): THREE.MeshBasicMaterial {
    return new THREE.MeshBasicMaterial({
      map: this.poolTex, color, transparent: true, opacity: 0.32, depthWrite: false,
      blending: THREE.AdditiveBlending, toneMapped: false,
    });
  }

  dispose(): void {
    Object.values(this.geo).forEach((g) => g.dispose());
    Object.values(this.mat).forEach((m) => { (m as THREE.MeshStandardMaterial).map?.dispose(); m.dispose(); });
    this.byColor.forEach((m) => m.dispose());
    this.poolTex.dispose();
  }
}

/** A screen: a canvas texture behind a thin bezel, repainted only when what
 *  it shows changed (and on a slow tick for its "12s ago" text). */
export class Monitor {
  readonly canvas = document.createElement('canvas');
  readonly ctx: CanvasRenderingContext2D;
  readonly tex: THREE.CanvasTexture;
  readonly group = new THREE.Group();
  readonly screen: THREE.Mesh;
  readonly mat: THREE.MeshBasicMaterial;
  private last = -1e9;
  private dirty = true;
  /** Seconds between repaints with nothing new (the "ago" text, a clock). */
  tick: number;

  constructor(kit: FurnitureKit, px: [number, number], size: [number, number], private paint: Painter, tick = 2, stand = true) {
    this.canvas.width = px[0];
    this.canvas.height = px[1];
    this.ctx = cacheMeasureText(this.canvas.getContext('2d')!);
    this.tex = new THREE.CanvasTexture(this.canvas);
    this.tex.colorSpace = THREE.SRGBColorSpace;
    this.tex.anisotropy = 8;
    this.tick = tick;
    // just under 1.0: the bloom pass (threshold above it) leaves text sharp
    this.mat = new THREE.MeshBasicMaterial({ map: this.tex, toneMapped: false, color: new THREE.Color(0.9, 0.9, 0.9) });
    const [w, h] = size;
    this.screen = new THREE.Mesh(kit.geo.plane, this.mat);
    this.screen.scale.set(w, h, 1);
    this.screen.position.z = 0.002;
    const bezel = new THREE.Mesh(kit.geo.box, kit.mat.bezel);
    bezel.scale.set(w + 0.022, h + 0.022, 0.022);
    bezel.position.z = -0.011;
    const back = new THREE.Mesh(kit.geo.box, kit.mat.metal);
    back.scale.set(w * 0.5, h * 0.5, 0.04);
    back.position.z = -0.04;
    this.group.add(this.screen, bezel, back);
    if (stand) {
      const pole = new THREE.Mesh(kit.geo.pole, kit.mat.pole);
      pole.position.set(0, 0, -0.065);
      pole.rotation.x = Math.PI / 2;
      pole.scale.set(1, 0.05, 1);
      this.group.add(pole);
    }
  }

  invalidate(): void { this.dirty = true; }

  update(nowSec: number, nowMs: number, minGap = 0.25): void {
    const since = nowSec - this.last;
    if ((this.dirty && since >= minGap) || since >= this.tick) {
      this.dirty = false;
      this.last = nowSec;
      try {
        this.paint(this.ctx, this.canvas.width, this.canvas.height, nowMs);
      } catch {
        // a painter that throws leaves the last frame up rather than a hole
      }
      this.tex.needsUpdate = true;
    }
  }

  dispose(): void {
    this.tex.dispose();
    this.mat.dispose();
  }
}

export type MonitorKey = 'calls' | 'market' | 'record' | 'pulse' | 'status';

export class DeskStation {
  readonly group = new THREE.Group();
  readonly swivel = new THREE.Group();
  readonly rat: SeatedRat;
  readonly monitors: Record<MonitorKey, Monitor>;
  readonly ledMat: THREE.MeshStandardMaterial;
  readonly poolMat: THREE.MeshBasicMaterial;
  private empties: THREE.Mesh[] = [];
  private butts: THREE.Mesh[] = [];
  private rev = -1;
  private flash = 0;
  private smokeAcc = 0;
  private phase: number;
  /** Where its monitors are, in world space (the follow camera's target). */
  readonly focus = new THREE.Vector3();

  constructor(kit: FurnitureKit, rats: CleanKit, readonly desk: Desk, seed: number, emberMat: THREE.Material, scoreLoaded: () => boolean) {
    this.phase = seed * 1.7;
    const color = desk.who.color;
    const add = (geo: THREE.BufferGeometry, mat: THREE.Material, x: number, y: number, z: number, parent: THREE.Object3D = this.group) => {
      const m = new THREE.Mesh(geo, mat);
      m.position.set(x, y, z);
      parent.add(m);
      return m;
    };
    // desk
    add(kit.geo.top, kit.mat.top, 0, TOP_Y - 0.0225, 0);
    for (const sx of [-1, 1]) add(kit.geo.leg, kit.mat.metal, sx * (DESK_W / 2 - 0.1), TOP_Y / 2, 0);
    this.ledMat = kit.led(color);
    const led = add(kit.geo.box, this.ledMat, 0, TOP_Y - 0.05, DESK_D / 2 - 0.03);
    led.scale.set(DESK_W - 0.16, 0.012, 0.012);
    const ledBack = add(kit.geo.box, this.ledMat, 0, TOP_Y - 0.05, -DESK_D / 2 + 0.03);
    ledBack.scale.set(DESK_W - 0.16, 0.012, 0.012);
    this.poolMat = kit.pool(color);
    const pool = add(kit.geo.plane, this.poolMat, 0, 0.004, 0.1);
    pool.rotation.x = -Math.PI / 2;
    pool.scale.set(DESK_W + 1.4, DESK_D + 1.8, 1);
    pool.renderOrder = 1;
    const deskMat = add(kit.geo.box, kit.mat.mat, 0.05, TOP_Y + 0.002, 0.2);
    deskMat.scale.set(1.4, 0.004, 0.42);
    add(kit.geo.kbd, kit.mat.kbd, -0.06, TOP_Y + 0.014, 0.25);
    const keys = add(kit.geo.plane, kit.mat.keys, -0.06, TOP_Y + 0.0255, 0.25);
    keys.rotation.x = -Math.PI / 2;
    keys.scale.set(0.5, 0.15, 1);
    add(kit.geo.mouse, kit.mat.kbd, 0.42, TOP_Y + 0.014, 0.26);

    // monitors, all facing the chair
    const loaded = scoreLoaded;
    this.monitors = {
      calls: new Monitor(kit, [768, 432], [0.98, 0.55], paintCalls(desk), 1),
      market: new Monitor(kit, [576, 360], [0.74, 0.46], paintMarket(desk), 2),
      record: new Monitor(kit, [576, 360], [0.74, 0.46], paintRecord(desk, loaded), 3),
      pulse: new Monitor(kit, [768, 216], [0.98, 0.275], paintPulse(desk), 3),
      status: new Monitor(kit, [324, 576], [0.36, 0.64], paintStatus(desk), 2),
    };
    const place = (m: Monitor, x: number, y: number, z: number, ry: number, rx = 0) => {
      m.group.position.set(x, y, z);
      m.group.rotation.set(rx, ry, 0, 'YXZ');
      this.group.add(m.group);
    };
    place(this.monitors.calls, 0, 1.14, -0.17, 0, -0.03);
    place(this.monitors.pulse, 0, 1.6, -0.19, 0, 0.1);
    place(this.monitors.market, -0.9, 1.1, -0.07, 0.5);
    place(this.monitors.record, 0.9, 1.1, -0.07, -0.5);
    place(this.monitors.status, 1.42, 1.06, 0.06, -0.78);
    // the arm the centre pair hangs on, and the side monitors' posts
    const post = add(kit.geo.pole, kit.mat.pole, 0, (TOP_Y + 1.6) / 2, -0.3);
    post.scale.set(1, 1.6 - TOP_Y, 1);
    add(kit.geo.foot, kit.mat.pole, 0, TOP_Y + 0.009, -0.3);
    for (const [x, z, top] of [[-0.98, -0.16, 1.1], [0.98, -0.16, 1.1], [1.5, -0.03, 1.06]] as const) {
      const p = add(kit.geo.pole, kit.mat.pole, x, (TOP_Y + top) / 2, z);
      p.scale.set(1, top - TOP_Y, 1);
      add(kit.geo.foot, kit.mat.pole, x, TOP_Y + 0.009, z);
    }

    // the can on the go, the empties behind it, the ashtray
    const can = this.can(kit, -0.72, 0.22);
    for (let i = 0; i < 5; i++) {
      const e = this.can(kit, -1.28 + (i % 3) * 0.1, -0.3 + Math.floor(i / 3) * 0.1);
      if (i === 4) { e.rotation.z = Math.PI / 2; e.position.y = TOP_Y + 0.042; e.position.x = -1.1; e.position.z = 0.0; }
      e.visible = false;
      this.empties.push(e);
    }
    add(kit.geo.tray, kit.mat.tray, 0.86, TOP_Y + 0.013, 0.24);
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2 + seed;
      const b = add(kit.geo.butt, kit.mat.butt, 0.86 + Math.cos(a) * 0.04, TOP_Y + 0.03, 0.24 + Math.sin(a) * 0.04);
      b.rotation.set(Math.PI / 2 - 0.3, 0, a);
      b.visible = false;
      this.butts.push(b);
    }

    // the chair, and its rat, on one swivel
    this.swivel.position.set(0, 0, CHAIR_Z);
    this.group.add(this.swivel);
    for (let i = 0; i < 5; i++) {
      const s = add(kit.geo.spoke, kit.mat.chairBase, 0, 0.06, 0, this.swivel);
      s.rotation.y = (i / 5) * Math.PI * 2;
      s.position.set(Math.cos(s.rotation.y) * 0.16, 0.06, -Math.sin(s.rotation.y) * 0.16);
      add(kit.geo.wheel, kit.mat.chairBase, Math.cos(s.rotation.y) * 0.32, 0.035, -Math.sin(s.rotation.y) * 0.32, this.swivel);
    }
    const lift = add(kit.geo.pole, kit.mat.chairBase, 0, 0.24, 0, this.swivel);
    lift.scale.set(1.6, 0.36, 1.6);
    add(kit.geo.seat, kit.mat.chair, 0, SEAT_Y - 0.045, 0.02, this.swivel);
    const back = add(kit.geo.back, kit.mat.chair, 0, SEAT_Y + 0.42, 0.29, this.swivel);
    back.rotation.x = -0.12;
    const head = add(kit.geo.head, kit.mat.chair, 0, SEAT_Y + 0.88, 0.35, this.swivel);
    head.rotation.x = -0.12;
    for (const sx of [-0.17, 0.17]) {
      const st = add(kit.geo.box, kit.accent(color), sx, SEAT_Y + 0.44, 0.335, this.swivel);
      st.scale.set(0.05, 0.7, 0.012);
      st.rotation.x = -0.12;
      add(kit.geo.arm, kit.mat.chair, sx * 1.65, SEAT_Y + 0.16, 0.05, this.swivel);
    }
    this.rat = new SeatedRat(rats, color, furFor(desk.id), emberMat, seed);
    this.rat.group.rotation.y = Math.PI;
    this.swivel.add(this.rat.group);
    this.rat.holdCan(can);
  }

  private can(kit: FurnitureKit, x: number, z: number): THREE.Mesh {
    const c = new THREE.Mesh(kit.geo.can, kit.mat.can);
    c.position.set(x, TOP_Y + 0.075, z);
    c.rotation.y = x * 3;
    const top = new THREE.Mesh(kit.geo.canTop, kit.mat.canTop);
    top.position.y = 0.081;
    c.add(top);
    this.group.add(c);
    return c;
  }

  /** A call just landed on this desk: the strip flares. */
  pulse(): void { this.flash = 1; }

  update(dt: number, nowSec: number, nowMs: number, smoke: Smoke, camNear: boolean, _v: THREE.Vector3): void {
    const d = this.desk;
    this.rat.asleep = d.mode === 'off';
    const exhale = this.rat.update(dt);
    this.swivel.rotation.y = Math.sin(nowSec * 0.21 + this.phase) * (this.rat.busy() ? 0.03 : 0.12);
    this.flash = Math.max(0, this.flash - dt * 1.4);
    const off = d.mode === 'off';
    this.ledMat.emissiveIntensity = (off ? 0.35 : 1.15) + this.flash * 1.8;
    this.poolMat.opacity = (off ? 0.08 : 0.2) + this.flash * 0.18;

    if (d.rev !== this.rev) {
      this.rev = d.rev;
      for (const m of Object.values(this.monitors)) m.invalidate();
      const calls = d.ok + d.refused + d.error;
      this.empties.forEach((e, i) => { e.visible = i < Math.min(5, Math.floor(calls / 20)); });
      this.butts.forEach((b, i) => { b.visible = i < Math.min(8, d.refused); });
    }
    // the screens you are close to repaint sooner
    for (const m of Object.values(this.monitors)) m.update(nowSec, nowMs, camNear ? 0.15 : 0.6);

    // smoke: a thin wisp off the lit end, and a cloud on each exhale
    if (!off) {
      this.smokeAcc += dt;
      if (this.smokeAcc > 0.22) {
        this.smokeAcc = 0;
        const p = this.rat.emberWorld(_v);
        smoke.emit(p.x, p.y + 0.01, p.z, false, { size: 0.03, grow: 0.08, alpha: 0.28 });
      }
      if (exhale) {
        const m = this.rat.mouthWorld(_v);
        for (let i = 0; i < 4; i++) smoke.emit(m.x, m.y, m.z, true, { size: 0.07, grow: 0.16, alpha: 0.32, v: [(Math.random() - 0.5) * 0.12, 0.12, (Math.random() - 0.2) * 0.25] });
      }
    }
    this.group.updateMatrixWorld();
    this.focus.set(0, 1.18, -0.15).applyMatrix4(this.group.matrixWorld);
  }

  dispose(): void {
    Object.values(this.monitors).forEach((m) => m.dispose());
    this.ledMat.dispose();
    this.poolMat.dispose();
    this.rat.dispose();
  }
}
