import * as THREE from 'three';
import { CrewKit, PINK, buildRatHead, buildRatTail, newRatParts, type RatParts } from '../engine/crew';
import { bakePart } from '../engine/bake';

// Your agents, off the clock and at their own desks: the same rat as on the
// casino floor (one rig, one size, the head and tail from crew.ts) but drawn
// clean — smooth shading and rounder parts instead of the PS2 facets — and
// dressed for the job: a hoodie in the agent's own colour, joggers, white
// sneakers, a headset whose mic lights up while it is on a call, and a lit
// cigarette at the corner of the mouth.

/** Seat height of the chairs these rats sit in. */
export const SEAT_Y = 0.46;
/** The rig's hip pivot above its feet (crew.ts builds legs from there). */
const HIP = 0.36;

const up = new THREE.Vector3(0, 1, 0);

/** The crew kit with smooth materials and more segments. */
export class CleanKit extends CrewKit {
  override readonly g = {
    skull: new THREE.SphereGeometry(0.27, 28, 20),
    snout: new THREE.ConeGeometry(0.15, 0.32, 24),
    nose: new THREE.SphereGeometry(0.045, 14, 10),
    ear: new THREE.SphereGeometry(0.13, 22, 14),
    eye: new THREE.SphereGeometry(0.038, 14, 10),
    tooth: new THREE.BoxGeometry(0.035, 0.05, 0.02),
    whisker: new THREE.BoxGeometry(0.2, 0.006, 0.006),
    body: new THREE.CapsuleGeometry(0.21, 0.2, 8, 20),
    collar: new THREE.CylinderGeometry(0.12, 0.17, 0.12, 20),
    stripe: new THREE.BoxGeometry(0.012, 0.34, 0.012),
    lapel: new THREE.BoxGeometry(0.07, 0.24, 0.025),
    shirt: new THREE.BoxGeometry(0.1, 0.18, 0.02),
    limb: new THREE.CapsuleGeometry(0.068, 0.17, 6, 14),
    hand: new THREE.SphereGeometry(0.07, 16, 12),
    shoe: new THREE.BoxGeometry(0.13, 0.08, 0.22),
    spat: new THREE.BoxGeometry(0.135, 0.05, 0.12),
    bow: new THREE.ConeGeometry(0.05, 0.08, 4),
    tie: new THREE.BoxGeometry(0.06, 0.22, 0.02),
    crown: new THREE.CylinderGeometry(0.19, 0.23, 0.17, 8),
    brim: new THREE.CylinderGeometry(0.37, 0.37, 0.022, 10),
    hatband: new THREE.CylinderGeometry(0.234, 0.236, 0.045, 8),
    visorBand: new THREE.TorusGeometry(0.27, 0.035, 5, 10),
    visor: new THREE.CylinderGeometry(0.3, 0.3, 0.02, 10, 1, false, -Math.PI / 2, Math.PI),
    lens: new THREE.BoxGeometry(0.11, 0.06, 0.025),
    goggle: new THREE.TorusGeometry(0.06, 0.018, 5, 8),
    tail: new THREE.CylinderGeometry(0.018, 0.035, 0.32, 10),
    belt: new THREE.TorusGeometry(0.205, 0.03, 5, 8),
    cig: new THREE.CylinderGeometry(0.012, 0.012, 0.12, 10),
    cigar: new THREE.CylinderGeometry(0.022, 0.026, 0.17, 6),
    ember: new THREE.SphereGeometry(0.017, 10, 8),
    slip: new THREE.BoxGeometry(0.18, 0.24, 0.02),
    tablet: new THREE.BoxGeometry(0.26, 0.18, 0.025),
    deck: new THREE.BoxGeometry(0.1, 0.05, 0.14),
    handle: new THREE.CylinderGeometry(0.018, 0.018, 0.7, 5),
    paddle: new THREE.BoxGeometry(0.16, 0.02, 0.06),
    shadow: new THREE.CircleGeometry(0.34, 24),
  };

  /** The hoodie, the sneakers and the headset. */
  readonly x = {
    hood: new THREE.SphereGeometry(0.17, 20, 14),
    rib: new THREE.CylinderGeometry(0.13, 0.15, 0.08, 20),
    pocket: new THREE.BoxGeometry(0.24, 0.1, 0.03),
    string: new THREE.CylinderGeometry(0.008, 0.008, 0.14, 6),
    cuff: new THREE.CylinderGeometry(0.074, 0.074, 0.045, 16),
    sole: new THREE.BoxGeometry(0.14, 0.03, 0.235),
    band: new THREE.TorusGeometry(0.29, 0.02, 10, 32, Math.PI),
    cup: new THREE.CylinderGeometry(0.075, 0.075, 0.05, 22),
    cupRing: new THREE.TorusGeometry(0.07, 0.012, 8, 22),
    boom: new THREE.CylinderGeometry(0.008, 0.008, 1, 8),
    micTip: new THREE.SphereGeometry(0.024, 12, 10),
  };

  private smooth = new Map<string, THREE.MeshStandardMaterial>();

  override mat(color: string, opts: { emissive?: string; ei?: number; rough?: number } = {}): THREE.Material {
    const key = `${color}|${opts.emissive ?? ''}|${opts.ei ?? 0}|${opts.rough ?? 0.6}`;
    let m = this.smooth.get(key);
    if (!m) {
      m = new THREE.MeshStandardMaterial({
        color, roughness: opts.rough ?? 0.6, metalness: 0.03,
        emissive: opts.emissive ?? '#000000', emissiveIntensity: opts.ei ?? 0,
      });
      this.smooth.set(key, m);
    }
    return m;
  }

  override readonly skinMat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.58, metalness: 0.02 });

  dispose(): void {
    for (const g of [...Object.values(this.g), ...Object.values(this.x)]) g.dispose();
    this.smooth.forEach((m) => m.dispose());
    this.smooth.clear();
    this.skinMat.dispose();
    this.shadowMat.dispose();
    this.embers.forEach((m) => m.dispose());
  }
}

function mesh(g: THREE.BufferGeometry, m: THREE.Material, cast = true): THREE.Mesh {
  const o = new THREE.Mesh(g, m);
  o.castShadow = cast;
  return o;
}

/** A thin cylinder from `a` to `b` (both in the parent's space). */
function rod(geo: THREE.BufferGeometry, m: THREE.Material, a: THREE.Vector3, b: THREE.Vector3): THREE.Mesh {
  const o = mesh(geo, m, false);
  const d = new THREE.Vector3().subVectors(b, a);
  o.scale.set(1, d.length(), 1);
  o.position.copy(a).addScaledVector(d, 0.5);
  o.quaternion.setFromUnitVectors(up, d.normalize());
  return o;
}

export interface AgentRat {
  parts: RatParts;
  ember: THREE.Mesh;
  /** The headset mic's light: its own material, lit while on a call. */
  mic: THREE.Mesh;
  micMat: THREE.MeshStandardMaterial;
  /** Where a can sits in the left paw. */
  paw: THREE.Object3D;
}

const FUR = ['#8a8178', '#6f665e', '#a39a8f', '#5c524a', '#9b8b7a', '#7a7068', '#b0a89c'];

export function furFor(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return FUR[Math.abs(h) % FUR.length];
}

export function buildAgentRat(kit: CleanKit, color: string, fur: string, emberMat: THREE.Material): AgentRat {
  const g = kit.g;
  const x = kit.x;
  const p = newRatParts();
  p.group.add(p.rig);
  const cloth = new THREE.Color(color).lerp(new THREE.Color('#1b1e26'), 0.18);
  const hoodie = kit.mat(cloth.getStyle(), { rough: 0.85 });
  const trim = kit.mat(cloth.clone().multiplyScalar(0.62).getStyle(), { rough: 0.85 });
  const jogger = kit.mat('#2a2e37', { rough: 0.9 });

  // legs (pivot at the hip): joggers and white sneakers
  for (const [leg, sx] of [[p.legL, -0.1], [p.legR, 0.1]] as const) {
    leg.position.set(sx, HIP, 0);
    const l = mesh(g.limb, jogger);
    l.position.y = -0.15;
    const shoe = mesh(g.shoe, kit.mat('#F4F5F7', { rough: 0.45 }));
    shoe.position.set(0, -0.29, 0.04);
    const sole = mesh(x.sole, kit.mat('#C7CCD4', { rough: 0.6 }));
    sole.position.set(0, -0.335, 0.045);
    leg.add(l, shoe, sole);
    p.rig.add(leg);
  }
  // the hoodie: body, the hood bunched behind the neck, a ribbed collar,
  // the pouch pocket and its drawstrings
  const body = mesh(g.body, hoodie);
  body.position.y = 0.58;
  const hood = mesh(x.hood, hoodie);
  hood.scale.set(1.05, 0.62, 0.78);
  hood.position.set(0, 0.86, -0.12);
  const rib = mesh(x.rib, trim);
  rib.position.y = 0.86;
  const pocket = mesh(x.pocket, trim, false);
  pocket.position.set(0, 0.5, 0.2);
  p.rig.add(body, hood, rib, pocket);
  for (const sx of [-0.045, 0.045]) {
    const s = mesh(x.string, kit.mat('#EDEFF2'), false);
    s.position.set(sx, 0.77, 0.205);
    s.rotation.z = sx > 0 ? -0.08 : 0.08;
    p.rig.add(s);
  }
  buildRatTail(kit, p.rig);

  // arms (pivot at the shoulder): sleeves, cuffs, pink paws
  for (const [arm, sx] of [[p.armL, -0.26], [p.armR, 0.26]] as const) {
    arm.position.set(sx, 0.76, 0);
    const a = mesh(g.limb, hoodie);
    a.position.y = -0.13;
    const cuff = mesh(x.cuff, trim);
    cuff.position.y = -0.235;
    const hand = mesh(g.hand, kit.mat(PINK));
    hand.position.y = -0.285;
    arm.add(a, cuff, hand);
    p.rig.add(arm);
  }

  buildRatHead(kit, p.head, fur, 'none');
  // the headset, worn in front of the ears
  const plastic = kit.mat('#111318', { rough: 0.35 });
  const band = mesh(x.band, plastic, false);
  band.position.set(0, 0.0, 0.07);
  p.head.add(band);
  for (const sx of [-1, 1]) {
    const cup = mesh(x.cup, plastic);
    cup.rotation.z = Math.PI / 2;
    cup.position.set(sx * 0.28, -0.02, 0.07);
    const ring = mesh(x.cupRing, kit.mat(color, { rough: 0.4 }), false);
    ring.rotation.y = Math.PI / 2;
    ring.position.set(sx * 0.306, -0.02, 0.07);
    p.head.add(cup, ring);
  }
  const micA = new THREE.Vector3(-0.29, -0.05, 0.1);
  const micB = new THREE.Vector3(-0.11, -0.13, 0.31);
  p.head.add(rod(x.boom, plastic, micA, micB));
  const micMat = new THREE.MeshStandardMaterial({ color: '#0f1115', emissive: color, emissiveIntensity: 0.25, roughness: 0.3 });
  const mic = new THREE.Mesh(x.micTip, micMat);
  mic.position.copy(micB);

  // the cigarette, out of the right corner of the mouth, and its lit end
  const stick = mesh(g.cig, kit.mat('#F2EEE6'), false);
  stick.rotation.set(Math.PI / 2 + 0.22, 0.5, 0, 'YXZ');
  const dir = new THREE.Vector3(0, 1, 0).applyEuler(stick.rotation);
  const corner = new THREE.Vector3(0.07, -0.11, 0.33);
  stick.position.copy(corner).addScaledVector(dir, 0.06);
  p.head.add(stick);
  const ember = new THREE.Mesh(g.ember, emberMat);
  ember.position.copy(corner).addScaledVector(dir, 0.124);
  p.rig.add(p.head);

  const paw = new THREE.Object3D();
  paw.position.set(0.02, -0.3, 0.06);
  p.armL.add(paw);

  for (const part of [p.legL, p.legR, p.armL, p.armR]) bakePart(part, kit.skinMat, true);
  bakePart(p.rig, kit.skinMat, true);
  bakePart(p.head, kit.skinMat, true);
  p.head.add(ember, mic);
  return { parts: p, ember, mic, micMat, paw };
}

export type RatMood = 'idle' | 'type' | 'read' | 'refused' | 'cheer' | 'sleep';

function damp(a: number, b: number, k: number, dt: number): number {
  return b + (a - b) * Math.exp(-k * dt);
}

/** A rat in its desk chair. The engine says what it is doing (a real call
 *  sets `type`/`read`, a refusal `refused`, a filled order `cheer`; an agent
 *  switched off sleeps), and between calls it smokes, sips and looks round
 *  its monitors. */
export class SeatedRat {
  readonly r: AgentRat;
  private mood: RatMood = 'idle';
  private until = 0;
  private moodT = 0;
  private phase: number;
  private nextSip: number;
  private sipT = -1;
  private nextPuff: number;
  private puffT = -1;
  /** -1 left monitor, 0 centre, 1 right: where the work is. */
  look = 0;
  /** True while the agent is switched off. */
  asleep = false;
  /** The can, while it is in the paw (else on the desk). */
  private can: THREE.Object3D | null = null;
  private canHome: { parent: THREE.Object3D; pos: THREE.Vector3; rot: THREE.Euler } | null = null;
  private ang = { lean: 0, hx: 0, hy: 0, hz: 0, alx: 0, alz: 0, arx: 0, arz: 0 };

  constructor(kit: CleanKit, color: string, fur: string, emberMat: THREE.Material, seed: number) {
    this.r = buildAgentRat(kit, color, fur, emberMat);
    this.phase = seed * 7.3;
    this.nextSip = 6 + (seed % 5) * 3;
    this.nextPuff = 3 + (seed % 3) * 2;
    const p = this.r.parts;
    p.rig.position.y = SEAT_Y - HIP;
    p.legL.rotation.set(-1.45, 0, 0.07);
    p.legR.rotation.set(-1.45, 0, -0.07);
  }

  get group(): THREE.Group { return this.r.parts.group; }

  /** Give it its can: it sips from it now and then. */
  holdCan(can: THREE.Object3D): void {
    this.can = can;
    this.canHome = { parent: can.parent!, pos: can.position.clone(), rot: can.rotation.clone() };
  }

  act(mood: RatMood, secs: number): void {
    // a refusal or an order outranks plain typing for its moment
    const rank = (m: RatMood) => (m === 'refused' ? 3 : m === 'cheer' ? 2 : m === 'type' || m === 'read' ? 1 : 0);
    if (this.time() < this.until && rank(mood) < rank(this.mood)) {
      this.until = Math.max(this.until, this.time() + secs * 0.5);
      return;
    }
    if (this.mood !== mood) this.moodT = 0;
    this.mood = mood;
    this.until = this.time() + secs;
  }

  private t = 0;
  private time(): number { return this.t; }

  /** Busy (on a call) right now: the mic is lit. */
  busy(): boolean {
    return !this.asleep && this.t < this.until && this.mood !== 'idle';
  }

  /** → 'exhale' on the frame a drag is blown out, for the engine's smoke. */
  update(dt: number): 'exhale' | null {
    this.t += dt;
    this.moodT += dt;
    const t = this.t + this.phase;
    let mood: RatMood = this.t < this.until ? this.mood : 'idle';
    if (this.asleep) mood = 'sleep';
    const p = this.r.parts;
    const goal = { lean: -0.1, hx: 0, hy: Math.sin(t * 0.35) * 0.35, hz: 0, alx: -0.35, alz: 0.06, arx: -0.35, arz: -0.06 };
    let out: 'exhale' | null = null;

    if (mood === 'type' || mood === 'cheer') {
      const k = Math.sin(t * 17), k2 = Math.sin(t * 15 + 1.7);
      Object.assign(goal, { lean: 0.16, hx: 0.06 + Math.sin(t * 3) * 0.02, hy: this.look * 0.45 + Math.sin(t * 1.3) * 0.06, alx: -1.32 + k * 0.07, alz: 0.22, arx: -1.32 + k2 * 0.07, arz: -0.22 });
      if (mood === 'cheer' && this.moodT < 1.6) {
        goal.arx = -2.9 + Math.sin(this.moodT * 16) * 0.18;
        goal.arz = -0.15;
        goal.hx = -0.2;
        goal.lean = -0.05;
      }
    } else if (mood === 'read') {
      Object.assign(goal, { lean: 0.08, hx: 0.04, hy: this.look * 0.4 + Math.sin(t * 0.9) * 0.4, alx: -1.2, alz: 0.18, arx: -1.28 + Math.sin(t * 6) * 0.03, arz: -0.3 });
    } else if (mood === 'refused') {
      const s = Math.max(0, 1 - this.moodT / 1.4);
      Object.assign(goal, { lean: -0.06, hx: -0.05, hy: Math.sin(this.moodT * 22) * 0.4 * s, alx: this.moodT < 0.9 ? -2.5 : -1.3, alz: 0.4, arx: this.moodT < 0.9 ? -2.5 : -1.3, arz: -0.4 });
    } else if (mood === 'sleep') {
      Object.assign(goal, { lean: 0.22, hx: 0.62, hy: 0.15, hz: 0.18, alx: -1.38, alz: 0.3, arx: -1.38, arz: -0.3 });
    }

    // between calls: a drag on the cigarette, a sip of the can
    if (mood === 'idle') {
      if (this.sipT < 0 && this.t > this.nextSip && this.can) {
        this.sipT = 0;
        this.toPaw(true);
      }
      if (this.sipT >= 0) {
        this.sipT += dt;
        const lift = Math.min(1, this.sipT / 0.6) * Math.min(1, Math.max(0, (2.6 - this.sipT) / 0.6));
        goal.alx = -0.35 + (-2.05 + 0.35) * lift;
        goal.alz = 0.06 + 0.5 * lift;
        goal.hx = -0.22 * lift;
        if (this.sipT > 2.7) {
          this.sipT = -1;
          this.toPaw(false);
          this.nextSip = this.t + 14 + Math.random() * 16;
        }
      } else if (this.puffT < 0 && this.t > this.nextPuff) {
        this.puffT = 0;
      }
      if (this.puffT >= 0) {
        this.puffT += dt;
        const b = Math.sin(Math.min(1, this.puffT / 1.4) * Math.PI);
        goal.hx = -0.24 * b;
        goal.lean = -0.16;
        if (this.puffT > 0.9 && this.puffT - dt <= 0.9) out = 'exhale';
        if (this.puffT > 1.6) {
          this.puffT = -1;
          this.nextPuff = this.t + 6 + Math.random() * 7;
        }
      }
    } else if (this.sipT >= 0) {
      // called back to work mid-sip: the can goes down at once
      this.sipT = -1;
      this.toPaw(false);
    }

    const k = mood === 'refused' ? 18 : 7;
    const a = this.ang;
    a.lean = damp(a.lean, goal.lean, 4, dt);
    a.hx = damp(a.hx, goal.hx, 5, dt);
    a.hy = damp(a.hy, goal.hy, mood === 'refused' ? 30 : 4, dt);
    a.hz = damp(a.hz, goal.hz, 3, dt);
    a.alx = damp(a.alx, goal.alx, k, dt);
    a.alz = damp(a.alz, goal.alz, k, dt);
    a.arx = damp(a.arx, goal.arx, k, dt);
    a.arz = damp(a.arz, goal.arz, k, dt);
    p.rig.rotation.x = a.lean;
    p.rig.scale.y = 1 + Math.sin(t * (mood === 'sleep' ? 1.1 : 1.8)) * (mood === 'sleep' ? 0.014 : 0.007);
    p.head.rotation.set(a.hx, a.hy, a.hz);
    p.armL.rotation.set(a.alx, 0, a.alz);
    p.armR.rotation.set(a.arx, 0, a.arz);
    this.r.micMat.emissiveIntensity = this.busy() ? 1.5 + Math.sin(this.t * 12) * 0.35 : 0.2;
    return out;
  }

  private toPaw(inPaw: boolean): void {
    const c = this.can, home = this.canHome;
    if (!c || !home) return;
    if (inPaw) {
      this.r.paw.add(c);
      c.position.set(0, 0, 0);
      c.rotation.set(0, 0, 0);
    } else {
      home.parent.add(c);
      c.position.copy(home.pos);
      c.rotation.copy(home.rot);
    }
  }

  /** World position of the lit end (smoke rises from it). */
  emberWorld(out: THREE.Vector3): THREE.Vector3 {
    return this.r.ember.getWorldPosition(out);
  }

  /** World position just in front of the mouth (an exhale). */
  mouthWorld(out: THREE.Vector3): THREE.Vector3 {
    out.set(0.02, -0.1, 0.42);
    return this.r.parts.head.localToWorld(out);
  }

  /** World position over the head, for its name tag. */
  tagWorld(out: THREE.Vector3): THREE.Vector3 {
    out.set(0, 0.55, 0);
    return this.r.parts.head.localToWorld(out);
  }

  /** The engine frees geometry and materials by walking its scene once
   *  (shared ones included); only the mic's own material is this rat's. */
  dispose(): void {
    this.r.micMat.dispose();
  }
}
