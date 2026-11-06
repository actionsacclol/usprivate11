import * as THREE from 'three';
import type { NavGraph } from './nav';
import type { NavNode } from './layout';
import { bakePart } from './bake';

export type Role = 'researcher' | 'smith' | 'quant' | 'trader' | 'keeper' | 'risk' | 'engineer' | 'agent';
export type Anim = 'idle' | 'type' | 'hammer' | 'look' | 'wrench' | 'cheer' | 'grab' | 'panic' | 'hands';

export type Step =
  | { k: 'go'; to: string; run?: boolean }
  | { k: 'work'; anim: Anim; dur: number; onBeat?: () => void }
  | { k: 'do'; fn: (w: Worker) => void }
  | { k: 'waitFor'; cond: () => boolean; max: number; anim?: Anim };

// The house staff, and your agents: everyone on the floor is a mob rat, all
// one size — a fedora, a pinstripe suit, a tail — in the low-poly, faceted,
// flat-shaded look of a PS2-era game. The role ids are the app's concepts
// (director.ts hands work out by them); what they wear says the role:
//
//   researcher  sportsbook writers   purple suit, lilac tie, a slip pad
//   smith       blackjack dealers    black suit, amber bow tie, a deck
//   quant       practice dealers     teal suit, round glasses, a tablet
//   trader      high-limit dealers   crimson suit, pink bow tie
//   keeper      cashiers             green suit, the green eyeshade
//   risk        security             black suit, shades
//   engineer    croupiers            navy suit, cyan bow tie, the rake
//   agent       YOUR AI agents       a suit in their own colour, a cream fedora
//
// About half of them smoke: a cigarette with a lit ember (its own little
// emissive mesh, so it glows) and the smoke the engine draws off it.
export interface RoleStyle {
  /** The jacket: the body, and the colour that says the role. */
  suit: string;
  /** Sleeves: the jacket's own colour for a suit. */
  sleeve: string;
  /** Trousers. */
  trim: string;
  /** What the worker does while standing at its home station. A staff that
   *  stands frozen between jobs reads as a screenshot, not a casino. */
  homeAnim: Anim;
  speed: number;
  hat: 'fedora' | 'eyeshade' | 'none';
  /** The fedora's felt. */
  hatColor: string;
  /** The hat band (defaults to the neck colour). */
  band?: string;
  eyes: 'none' | 'shades' | 'glasses';
  neck: 'none' | 'bowtie' | 'tie';
  neckColor: string;
  tool: 'none' | 'slip' | 'cards' | 'tablet' | 'rake';
}

const PANTS = '#1d1a22';
const FELT_DARK = '#17141a';

export const ROLE_STYLE: Record<Role, RoleStyle> = {
  researcher: { suit: '#4c1d95', sleeve: '#4c1d95', trim: PANTS, homeAnim: 'type', speed: 1.7, hat: 'fedora', hatColor: FELT_DARK, eyes: 'none', neck: 'tie', neckColor: '#C084FC', tool: 'slip' },
  smith: { suit: '#1c1917', sleeve: '#1c1917', trim: PANTS, homeAnim: 'idle', speed: 1.6, hat: 'fedora', hatColor: '#3a2a1c', eyes: 'none', neck: 'bowtie', neckColor: '#F59E0B', tool: 'cards' },
  quant: { suit: '#115e59', sleeve: '#115e59', trim: PANTS, homeAnim: 'look', speed: 2.0, hat: 'fedora', hatColor: FELT_DARK, eyes: 'glasses', neck: 'bowtie', neckColor: '#2DD4BF', tool: 'tablet' },
  trader: { suit: '#831843', sleeve: '#831843', trim: PANTS, homeAnim: 'type', speed: 1.8, hat: 'fedora', hatColor: FELT_DARK, eyes: 'none', neck: 'bowtie', neckColor: '#F472B6', tool: 'none' },
  keeper: { suit: '#14532d', sleeve: '#14532d', trim: PANTS, homeAnim: 'idle', speed: 2.1, hat: 'eyeshade', hatColor: '#14532d', eyes: 'none', neck: 'tie', neckColor: '#4ADE80', tool: 'none' },
  risk: { suit: '#0f0f12', sleeve: '#0f0f12', trim: '#0b0b0e', homeAnim: 'type', speed: 1.9, hat: 'fedora', hatColor: '#0b0b0d', eyes: 'shades', neck: 'tie', neckColor: '#DC2626', tool: 'none' },
  engineer: { suit: '#1e3a5f', sleeve: '#1e3a5f', trim: PANTS, homeAnim: 'wrench', speed: 1.6, hat: 'fedora', hatColor: '#2a2f3a', eyes: 'none', neck: 'bowtie', neckColor: '#38BDF8', tool: 'rake' },
  // The user's own AI agents. The suit colour is per agent (utils/agents.ts),
  // passed in as a style override; the cream fedora is what says "a guest,
  // not the house" at a glance.
  agent: { suit: '#94A3B8', sleeve: '#94A3B8', trim: '#1E293B', homeAnim: 'look', speed: 2.6, hat: 'fedora', hatColor: '#E8DFC9', eyes: 'none', neck: 'tie', neckColor: '#94A3B8', tool: 'tablet' },
};

/** Rat fur, a different coat per worker. */
const FUR = ['#8a8178', '#6f665e', '#a39a8f', '#5c524a', '#9b8b7a', '#7a7068', '#b0a89c', '#64594f'];
export const PINK = '#d99aa3';

/** Geometry + material cache shared by every rat on the floor. Low segment
 *  counts and flat shading on purpose: the faceted look is the art style. */
export class CrewKit {
  readonly g = {
    skull: new THREE.SphereGeometry(0.27, 8, 6),
    snout: new THREE.ConeGeometry(0.15, 0.32, 7),
    nose: new THREE.SphereGeometry(0.045, 6, 4),
    ear: new THREE.SphereGeometry(0.13, 7, 5),
    eye: new THREE.SphereGeometry(0.038, 6, 4),
    tooth: new THREE.BoxGeometry(0.035, 0.05, 0.02),
    whisker: new THREE.BoxGeometry(0.2, 0.008, 0.008),
    body: new THREE.CapsuleGeometry(0.2, 0.2, 3, 7),
    collar: new THREE.CylinderGeometry(0.12, 0.17, 0.12, 7),
    stripe: new THREE.BoxGeometry(0.012, 0.34, 0.012),
    lapel: new THREE.BoxGeometry(0.07, 0.24, 0.025),
    shirt: new THREE.BoxGeometry(0.1, 0.18, 0.02),
    limb: new THREE.CapsuleGeometry(0.068, 0.17, 2, 6),
    hand: new THREE.SphereGeometry(0.068, 6, 4),
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
    tail: new THREE.CylinderGeometry(0.018, 0.035, 0.32, 5),
    belt: new THREE.TorusGeometry(0.205, 0.03, 5, 8),
    cig: new THREE.CylinderGeometry(0.013, 0.013, 0.12, 5),
    cigar: new THREE.CylinderGeometry(0.022, 0.026, 0.17, 6),
    ember: new THREE.SphereGeometry(0.019, 5, 4),
    slip: new THREE.BoxGeometry(0.18, 0.24, 0.02),
    tablet: new THREE.BoxGeometry(0.26, 0.18, 0.025),
    deck: new THREE.BoxGeometry(0.1, 0.05, 0.14),
    handle: new THREE.CylinderGeometry(0.018, 0.018, 0.7, 5),
    paddle: new THREE.BoxGeometry(0.16, 0.02, 0.06),
    shadow: new THREE.CircleGeometry(0.34, 12),
  };
  private mats = new Map<string, THREE.Material>();

  mat(color: string, opts: { emissive?: string; ei?: number; rough?: number } = {}): THREE.Material {
    const key = `${color}|${opts.emissive ?? ''}|${opts.ei ?? 0}|${opts.rough ?? 0.7}`;
    let m = this.mats.get(key);
    if (!m) {
      m = new THREE.MeshStandardMaterial({
        color, roughness: opts.rough ?? 0.7, metalness: 0.05, flatShading: true,
        emissive: opts.emissive ?? '#000000', emissiveIntensity: opts.ei ?? 0,
      });
      this.mats.set(key, m);
    }
    return m;
  }

  /** Shared by every baked rig part: colour lives in the vertices. */
  readonly skinMat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.66, metalness: 0.05, flatShading: true });
  readonly shadowMat = new THREE.MeshBasicMaterial({ color: '#000000', transparent: true, opacity: 0.28, depthWrite: false });
  /** Three ember materials, so the smokers don't all draw on the same beat
   *  (the engine pulses them). */
  readonly embers = [0, 1, 2].map(() => new THREE.MeshStandardMaterial({ color: '#ffb36b', emissive: '#ff6a1a', emissiveIntensity: 1.6 }));
}

function mesh(g: THREE.BufferGeometry, m: THREE.Material, cast = true): THREE.Mesh {
  const o = new THREE.Mesh(g, m);
  o.castShadow = cast;
  return o;
}

/** The groups a rat is built from: the same rig for the staff, your agents
 *  and the pit boss, so they are all one size and animate alike. */
export interface RatParts {
  group: THREE.Group;
  rig: THREE.Group;
  head: THREE.Group;
  legL: THREE.Group;
  legR: THREE.Group;
  armL: THREE.Group;
  armR: THREE.Group;
}

export function newRatParts(): RatParts {
  return {
    group: new THREE.Group(), rig: new THREE.Group(), head: new THREE.Group(),
    legL: new THREE.Group(), legR: new THREE.Group(), armL: new THREE.Group(), armR: new THREE.Group(),
  };
}

/** The tail: four segments, out from under the clothes and curling. */
export function buildRatTail(kit: CrewKit, rig: THREE.Group): void {
  const g = kit.g;
  let ty = 0.42, tz = -0.18, ta = 1.05;
  for (let i = 0; i < 4; i++) {
    const seg = mesh(g.tail, kit.mat(PINK), false);
    seg.scale.setScalar(1 - i * 0.18);
    const len = 0.32 * (1 - i * 0.18);
    const cy = ty - Math.cos(ta) * len / 2, cz = tz - Math.sin(ta) * len / 2;
    seg.position.set(Math.sin(i * 0.9) * 0.05, cy, cz);
    // the segment's +y (its thin end) points down the tail
    seg.rotation.x = Math.atan2(-Math.sin(ta), -Math.cos(ta));
    rig.add(seg);
    ty -= Math.cos(ta) * len;
    tz -= Math.sin(ta) * len;
    ta += 0.5;
  }
}

/** The head, on its pivot: skull, a long snout with a pink nose, big round
 *  ears, beady eyes, buck teeth, whiskers, and shades or glasses. */
export function buildRatHead(kit: CrewKit, head: THREE.Group, furColor: string, eyes: RoleStyle['eyes']): void {
  const g = kit.g;
  head.position.y = 1.08;
  const fur = kit.mat(furColor, { rough: 0.8 });
  const skull = mesh(g.skull, fur);
  skull.scale.set(1, 0.92, 1.05);
  const snout = mesh(g.snout, fur);
  snout.rotation.x = Math.PI / 2;
  snout.position.set(0, -0.05, 0.3);
  const nose = mesh(g.nose, kit.mat('#e5788a'));
  nose.position.set(0, -0.04, 0.46);
  head.add(skull, snout, nose);
  for (const sx of [-1, 1]) {
    const ear = mesh(g.ear, fur);
    ear.scale.set(1, 1.05, 0.32);
    ear.position.set(sx * 0.25, 0.1, -0.04);
    ear.rotation.z = -sx * 0.35;
    const inner = mesh(g.ear, kit.mat(PINK), false);
    inner.scale.set(0.66, 0.7, 0.2);
    inner.position.set(sx * 0.245, 0.1, -0.005);
    inner.rotation.z = -sx * 0.35;
    const eye = mesh(g.eye, kit.mat('#0d0a0c', { rough: 0.25 }), false);
    eye.position.set(sx * 0.11, 0.09, 0.235);
    eye.scale.set(1, 1.15, 0.7);
    const tooth = mesh(g.tooth, kit.mat('#F5F0E0'), false);
    tooth.position.set(sx * 0.02, -0.11, 0.37);
    head.add(ear, inner, eye, tooth);
    for (const k of [-1, 0, 1]) {
      const wh = mesh(g.whisker, kit.mat('#e8e2d6'), false);
      wh.position.set(sx * 0.13, -0.05 + k * 0.025, 0.38);
      wh.rotation.set(0, -sx * 0.35, k * 0.15 * sx);
      head.add(wh);
    }
  }
  if (eyes === 'shades') {
    for (const sx of [-0.1, 0.1]) {
      const l = mesh(g.lens, kit.mat('#050507', { rough: 0.15 }), false);
      l.position.set(sx, 0.09, 0.255);
      head.add(l);
    }
  } else if (eyes === 'glasses') {
    for (const sx of [-0.1, 0.1]) {
      const gl = mesh(g.goggle, kit.mat('#C79A45', { rough: 0.35 }), false);
      gl.position.set(sx, 0.09, 0.26);
      head.add(gl);
    }
  }
}

/** Build a mob rat into `p`. Returns the lit end of its smoke (null for a
 *  non-smoker) and the tool in its paw. */
export function buildRat(
  kit: CrewKit, p: RatParts, s: RoleStyle,
  o: { fur: string; smoke: 'none' | 'cig' | 'cigar'; emberMat: THREE.Material; castShadow: boolean },
): { ember: THREE.Mesh | null; tool: THREE.Object3D | null } {
  const g = kit.g;
  const shadow = new THREE.Mesh(g.shadow, kit.shadowMat);
  shadow.rotation.x = -Math.PI / 2;
  shadow.position.y = 0.012;
  shadow.renderOrder = 1;
  p.group.add(shadow, p.rig);

  // legs (pivot at the hip): trousers, two-tone shoes with white spats
  for (const [leg, sx] of [[p.legL, -0.1], [p.legR, 0.1]] as const) {
    leg.position.set(sx, 0.36, 0);
    const l = mesh(g.limb, kit.mat(s.trim));
    l.position.y = -0.15;
    const shoe = mesh(g.shoe, kit.mat('#1a1614'));
    shoe.position.set(0, -0.3, 0.04);
    const spat = mesh(g.spat, kit.mat('#EDE7DA'));
    spat.position.set(0, -0.27, -0.01);
    leg.add(l, shoe, spat);
    p.rig.add(leg);
  }
  // the jacket, pinstriped, with lapels, the shirt and what's at the neck
  const body = mesh(g.body, kit.mat(s.suit, { rough: 0.55 }));
  body.position.y = 0.58;
  p.rig.add(body);
  const pin = new THREE.Color(s.suit).lerp(new THREE.Color('#ffffff'), 0.35).getStyle();
  for (const sx of [-0.15, -0.075, 0.075, 0.15]) {
    const st = mesh(g.stripe, kit.mat(pin), false);
    st.position.set(sx, 0.58, Math.sqrt(Math.max(0, 0.2 * 0.2 - sx * sx)) + 0.004);
    st.rotation.y = Math.asin(sx / 0.2);
    p.rig.add(st);
  }
  const shirt = mesh(g.shirt, kit.mat('#EFEAE0'), false);
  shirt.position.set(0, 0.76, 0.19);
  p.rig.add(shirt);
  for (const sx of [-1, 1]) {
    const lap = mesh(g.lapel, kit.mat(new THREE.Color(s.suit).multiplyScalar(0.55).getStyle()), false);
    lap.position.set(sx * 0.065, 0.74, 0.195);
    lap.rotation.z = sx * 0.35;
    p.rig.add(lap);
  }
  const collar = mesh(g.collar, kit.mat('#EFEAE0'));
  collar.position.y = 0.86;
  p.rig.add(collar);
  if (s.neck === 'bowtie') {
    for (const sx of [-1, 1]) {
      const w = mesh(g.bow, kit.mat(s.neckColor), false);
      w.rotation.z = (sx * Math.PI) / 2;
      w.position.set(sx * 0.045, 0.84, 0.18);
      p.rig.add(w);
    }
  } else if (s.neck === 'tie') {
    const t = mesh(g.tie, kit.mat(s.neckColor), false);
    t.position.set(0, 0.73, 0.207);
    t.rotation.x = -0.1;
    p.rig.add(t);
  }
  const belt = mesh(g.belt, kit.mat('#1f1812'));
  belt.rotation.x = Math.PI / 2;
  belt.position.y = 0.47;
  p.rig.add(belt);

  buildRatTail(kit, p.rig);

  // arms (pivot at the shoulder): sleeves, pink paws
  for (const [arm, sx] of [[p.armL, -0.25], [p.armR, 0.25]] as const) {
    arm.position.set(sx, 0.76, 0);
    const a = mesh(g.limb, kit.mat(s.sleeve));
    a.position.y = -0.13;
    const hand = mesh(g.hand, kit.mat(PINK));
    hand.position.y = -0.27;
    arm.add(a, hand);
    p.rig.add(arm);
  }

  buildRatHead(kit, p.head, o.fur, s.eyes);
  // the hat: a fedora, its band in the role's colour; the cashiers' visor
  if (s.hat === 'fedora') {
    const felt = kit.mat(s.hatColor, { rough: 0.85 });
    const hat = new THREE.Group();
    hat.position.set(0, 0.2, -0.01);
    hat.rotation.set(-0.08, 0, 0.06);
    const brim = mesh(g.brim, felt);
    const crown = mesh(g.crown, felt);
    crown.position.y = 0.095;
    crown.scale.set(1, 1, 1.12);
    const band = mesh(g.hatband, kit.mat(s.band ?? s.neckColor));
    band.position.y = 0.04;
    band.scale.set(1, 1, 1.12);
    hat.add(brim, crown, band);
    // flattened into the head part so the bake still makes it one mesh
    hat.updateMatrix();
    for (const c of [...hat.children]) {
      c.applyMatrix4(hat.matrix);
      p.head.add(c);
    }
  } else if (s.hat === 'eyeshade') {
    const band = mesh(g.visorBand, kit.mat('#14532d'));
    band.rotation.x = Math.PI / 2 - 0.15;
    band.position.y = 0.12;
    const brim = mesh(g.visor, kit.mat('#22C55E', { emissive: '#15803D', ei: 0.35, rough: 0.3 }), false);
    brim.position.set(0, 0.14, 0.04);
    brim.rotation.x = 0.25;
    p.head.add(band, brim);
  }
  // the smoke: a cigarette (or the boss's cigar) at the corner of the mouth
  let ember: THREE.Mesh | null = null;
  if (o.smoke !== 'none') {
    const big = o.smoke === 'cigar';
    const stick = mesh(big ? g.cigar : g.cig, kit.mat(big ? '#5a3a22' : '#F2EEE6'), false);
    // pointing out of the mouth's corner: forward, a little down, to the side
    stick.rotation.set(Math.PI / 2 + 0.22, 0.5, 0, 'YXZ');
    const len = big ? 0.17 : 0.12;
    const dir = new THREE.Vector3(0, 1, 0).applyEuler(stick.rotation);
    const corner = new THREE.Vector3(0.07, -0.11, 0.33);
    stick.position.copy(corner).addScaledVector(dir, len / 2);
    p.head.add(stick);
    // the lit end: its own glowing mesh (a bake can't make vertices glow)
    ember = new THREE.Mesh(g.ember, o.emberMat);
    ember.position.copy(corner).addScaledVector(dir, len + 0.004);
    if (big) ember.scale.setScalar(1.3);
  }
  p.rig.add(p.head);
  return { ember, tool: null };
}

let seq = 0;

/** A rat that isn't a worker: the pit boss on the podium. Same rig, same
 *  size; it breathes, hops on a big moment, and smokes a cigar. */
export class RatFigure {
  readonly parts = newRatParts();
  readonly ember: THREE.Mesh | null;
  private phase = Math.random() * 10;
  /** Seconds left with its paws up (a gun went off on the floor). */
  private scared = 0;

  constructor(kit: CrewKit, style: RoleStyle, fur: string, smoke: 'none' | 'cig' | 'cigar') {
    const p = this.parts;
    const r = buildRat(kit, p, style, { fur, smoke, emberMat: kit.embers[0], castShadow: true });
    for (const part of [p.legL, p.legR, p.armL, p.armR]) bakePart(part, kit.skinMat, false);
    bakePart(p.rig, kit.skinMat, true);
    bakePart(p.head, kit.skinMat, true);
    this.ember = r.ember;
    if (r.ember) p.head.add(r.ember);
  }

  get group(): THREE.Group { return this.parts.group; }

  spook(secs: number): void {
    this.scared = Math.max(this.scared, secs);
  }

  /** `hop` (seconds left) bounces it; otherwise an idle sway, a hand that
   *  takes the cigar now and then. */
  update(dt: number, hop: number): void {
    this.phase += dt;
    const p = this.parts;
    const t = this.phase;
    p.rig.position.y = hop > 0 ? Math.abs(Math.sin(hop * 12)) * 0.25 : Math.sin(t * 2.2) * 0.012;
    p.head.rotation.y = Math.sin(t * 0.4) * 0.35;
    p.head.rotation.x = Math.sin(t * 0.7) * 0.05;
    const puff = Math.max(0, Math.sin(t * 0.55)) ** 6;
    p.armR.rotation.x = -0.3 - puff * 1.9;
    p.armR.rotation.z = -puff * 0.35;
    p.armL.rotation.x = hop > 0 ? -2.6 : -0.15;
    if (this.scared > 0) {
      this.scared = Math.max(0, this.scared - dt);
      const shake = Math.sin(t * 31) * 0.06;
      p.armL.rotation.x = -2.75 + shake;
      p.armR.rotation.x = -2.75 - shake;
      p.armL.rotation.z = -0.5;
      p.armR.rotation.z = 0.5;
      p.head.rotation.y *= 0.3;
    } else {
      p.armL.rotation.z = 0;
    }
  }
}

export class Worker {
  readonly id = seq++;
  readonly role: Role;
  readonly name: string;
  /** The station every job ends at. Fixed for the staff; your agents' moves
   *  with the game they sit at, so a real call takes them away from the
   *  table and the walk back ends at it. */
  home: string;
  readonly group = new THREE.Group();
  /** Everything above the feet: bobs and leans without moving the shadow. */
  private rig = new THREE.Group();
  private head = new THREE.Group();
  private legL = new THREE.Group();
  private legR = new THREE.Group();
  private armL = new THREE.Group();
  private armR = new THREE.Group();
  private toolObj: THREE.Object3D | null = null;
  readonly holdFront = new THREE.Group();
  readonly holdOver = new THREE.Group();
  /** The lit end of its cigarette (null: doesn't smoke). The engine draws
   *  the smoke off it. */
  readonly ember: THREE.Mesh | null = null;

  // position on the walk graph (the "base"), plus a visual lane offset
  x = 0;
  z = 0;
  heading = 0;
  private headingTarget = 0;
  private at: string | null;
  private path: NavNode[] = [];
  private pathI = 0;
  private running = false;
  private lane: number;

  private steps: Step[] = [];
  private cur: Step | null = null;
  private stepT = 0;
  private anim: Anim = 'idle';
  private phase = Math.random() * 10;
  private beatPhase = 0;
  private wanderT = 4 + Math.random() * 8;
  /** Set when this worker has a job queued — the director never double-books. */
  carrying: THREE.Object3D | null = null;
  carryOver = false;
  /** Recent speech, so one worker never talks over itself. */
  quietUntil = 0;
  /** Stations within its own room it may idly drift between. */
  wander: string[];
  /** A floating name tag (the user's agents only): "Claude Code · opus". */
  tag: { label: string; color: string } | null = null;
  /** Someone walking the floor is talking to this worker (walk.ts): it stops
   *  what it is doing, mid-step, and turns to face them; the job resumes
   *  where it left off when the conversation ends. */
  private heldBy: { x: number; z: number } | null = null;
  /** A gun on the floor (walk mode): it stops, faces it, paws up, until
   *  `t` runs out; `jump` is a startled hop at the start. The job resumes
   *  where it left off, like after a conversation. */
  private scare: { x: number; z: number; t: number; jump: number } | null = null;
  private style: RoleStyle;

  constructor(
    kit: CrewKit, role: Role, name: string, home: string, wander: string[], nav: NavGraph,
    style?: Partial<RoleStyle>,
  ) {
    this.role = role;
    this.name = name;
    this.home = home;
    this.wander = wander;
    this.lane = (this.id % 2 === 0 ? 1 : -1) * (0.16 + (this.id % 3) * 0.06);
    // an agent's colour is its suit, its sleeves and its hat band; the tie
    // is a paler shade of it, so it shows against the jacket
    const agentTint = style?.suit && role === 'agent'
      ? { sleeve: style.suit, band: style.suit, neckColor: new THREE.Color(style.suit).lerp(new THREE.Color('#ffffff'), 0.55).getStyle() }
      : {};
    const s: RoleStyle = { ...ROLE_STYLE[role], ...agentTint, ...style };
    this.style = s;
    const fur = FUR[(this.id * 5 + 1) % FUR.length];
    // about half the floor smokes
    const smoke = (this.id * 7 + 3) % 2 === 0 ? 'cig' : 'none';
    // The user's agents are extra figures on top of the full house staff:
    // they skip the shadow pass, grounded by the blob shadow alone.
    const cast = role !== 'agent';
    const parts: RatParts = {
      group: this.group, rig: this.rig, head: this.head,
      legL: this.legL, legR: this.legR, armL: this.armL, armR: this.armR,
    };
    const built = buildRat(kit, parts, s, { fur, smoke, emberMat: kit.embers[this.id % kit.embers.length], castShadow: cast });

    // hold points
    this.holdFront.position.set(0, 0.7, 0.36);
    this.holdOver.position.set(0, 1.62, 0.05);
    this.rig.add(this.holdFront, this.holdOver);
    this.addTool(kit, s);

    // Bake each rigid part to one mesh. Limbs skip the shadow pass: the blob
    // shadow already grounds the worker, and four tiny limb shadows per
    // worker were a quarter of all shadow-pass draw calls.
    for (const p of [this.legL, this.legR, this.armL, this.armR]) bakePart(p, kit.skinMat, false);
    bakePart(this.rig, kit.skinMat, cast);
    bakePart(this.head, kit.skinMat, cast);
    if (!cast) this.rig.traverse((o) => { o.castShadow = false; });
    if (built.ember) {
      this.head.add(built.ember);
      this.ember = built.ember;
    }

    const start = nav.get(home);
    this.x = start.x;
    this.z = start.z;
    this.at = home;
    this.heading = this.headingTarget = start.face ?? 0;
    this.group.position.set(this.x, 0, this.z);
  }

  private addTool(kit: CrewKit, s: RoleStyle): void {
    const g = kit.g;
    let t: THREE.Object3D | null = null;
    switch (s.tool) {
      case 'slip': {
        const cb = new THREE.Group();
        const b = mesh(g.slip, kit.mat('#3b2a5a'));
        const p = mesh(g.slip, kit.mat('#FFFFFF'));
        p.scale.set(0.82, 0.8, 0.5);
        p.position.z = 0.012;
        cb.add(b, p);
        cb.rotation.x = -0.5;
        t = cb;
        break;
      }
      case 'tablet': {
        const tb = mesh(g.tablet, kit.mat('#0F172A', { emissive: '#2DD4BF', ei: 0.35 }));
        tb.rotation.x = -0.9;
        t = tb;
        break;
      }
      case 'cards': {
        const d = new THREE.Group();
        const back = mesh(g.deck, kit.mat('#b91c1c'));
        const face = mesh(g.deck, kit.mat('#FFFFFF'));
        face.scale.set(0.95, 0.4, 0.95);
        face.position.y = 0.03;
        d.add(back, face);
        d.rotation.x = Math.PI / 2;
        t = d;
        break;
      }
      case 'rake': {
        // the croupier's stick: a long brass handle, a small paddle
        const r = new THREE.Group();
        const hd = mesh(g.handle, kit.mat('#C79A45', { rough: 0.35 }));
        hd.position.y = 0.2;
        const pad = mesh(g.paddle, kit.mat('#3a2216'));
        pad.position.y = 0.55;
        r.add(hd, pad);
        r.rotation.x = Math.PI / 2;
        t = r;
        break;
      }
      default: break;
    }
    if (!t) return;
    t.position.set(0, -0.3, 0.08);
    this.armR.add(t);
    this.toolObj = t;
  }

  // ── job API ─────────────────────────────────────────────────────────────

  get busy(): boolean {
    return this.cur !== null || this.steps.length > 0;
  }

  /** Standing at its home with nothing to do. */
  get atHome(): boolean {
    return !this.busy && this.at === this.home;
  }

  /** True while the worker is just walking home or drifting — the director
   *  may take it for real work. */
  get free(): boolean {
    return !this.busy || this.idleJob;
  }
  private idleJob = false;

  assign(steps: Step[]): void {
    if (this.idleJob) {
      this.steps = [];
      this.cur = null;
      this.idleJob = false;
      this.snapToGraph();
    }
    this.steps.push(...steps);
  }

  /** A walk interrupted mid-segment re-plans from the node it was heading
   *  to, which keeps everyone on the corridors instead of cutting through
   *  walls. */
  private snapToGraph(): void {
    if (this.path.length && this.pathI < this.path.length) this.at = null;
  }

  /** Free what this worker alone owns (its baked rig geometry). The kit's
   *  shared geometry and materials stay: other workers use them. Only the
   *  user's agents ever leave the floor. */
  dispose(kit: CrewKit): void {
    const shared = new Set<THREE.BufferGeometry>(Object.values(kit.g));
    this.drop()?.removeFromParent();
    this.group.traverse((o) => {
      const m = o as THREE.Mesh;
      if (m.isMesh && m.geometry && !shared.has(m.geometry)) m.geometry.dispose();
    });
    this.group.removeFromParent();
  }

  headWorld(out: THREE.Vector3): THREE.Vector3 {
    return out.set(this.group.position.x, this.group.position.y + 1.55, this.group.position.z);
  }

  pickUp(obj: THREE.Object3D, over = false): void {
    this.drop();
    this.carrying = obj;
    this.carryOver = over;
    (over ? this.holdOver : this.holdFront).add(obj);
    obj.position.set(0, 0, 0);
    obj.rotation.set(0, 0, 0);
    if (this.toolObj) this.toolObj.visible = false;
  }

  /** Detach the carried item, leaving it at its current world transform. */
  drop(): THREE.Object3D | null {
    const o = this.carrying;
    if (!o) return null;
    this.carrying = null;
    const wp = new THREE.Vector3();
    o.getWorldPosition(wp);
    o.removeFromParent();
    o.position.copy(wp);
    if (this.toolObj) this.toolObj.visible = true;
    return o;
  }

  // ── per frame ─────────────────────────────────────────────────────────────

  hold(at: { x: number; z: number } | null): void {
    this.heldBy = at;
  }

  /** Paws up, facing (x, z), for `secs` (at least). `jump` startles it. */
  spook(at: { x: number; z: number }, secs: number, jump = false): void {
    const prev = this.scare;
    this.scare = {
      x: at.x, z: at.z,
      t: Math.max(prev?.t ?? 0, secs),
      jump: jump ? 0.4 : (prev?.jump ?? 0),
    };
  }

  get spooked(): boolean {
    return !!this.scare;
  }

  update(dt: number, nav: NavGraph): void {
    if (this.heldBy) {
      this.anim = 'idle';
      this.headingTarget = Math.atan2(this.heldBy.x - this.x, this.heldBy.z - this.z);
      let dh = this.headingTarget - this.heading;
      dh = Math.atan2(Math.sin(dh), Math.cos(dh));
      this.heading += dh * Math.min(1, dt * 8);
      this.pose(dt, false);
      return;
    }
    const sc = this.scare;
    if (sc) {
      sc.t -= dt;
      sc.jump = Math.max(0, sc.jump - dt);
      if (sc.t <= 0) {
        this.scare = null;
      } else {
        this.anim = 'hands';
        this.headingTarget = Math.atan2(sc.x - this.x, sc.z - this.z);
        let dh = this.headingTarget - this.heading;
        dh = Math.atan2(Math.sin(dh), Math.cos(dh));
        this.heading += dh * Math.min(1, dt * 10);
        this.pose(dt, false);
        return;
      }
    }
    this.stepT += dt;
    if (!this.cur) this.nextStep(nav);
    const c = this.cur;
    let moving = false;
    if (c) {
      switch (c.k) {
        case 'go':
          moving = this.walk(dt);
          if (!moving) this.finishStep();
          break;
        case 'work':
          this.anim = c.anim;
          if (c.onBeat) {
            const period = c.anim === 'hammer' ? 0.42 : 0.5;
            const before = Math.floor(this.beatPhase / period);
            this.beatPhase += dt;
            if (Math.floor(this.beatPhase / period) > before) c.onBeat();
          }
          if (this.stepT >= c.dur) this.finishStep();
          break;
        case 'waitFor':
          this.anim = c.anim ?? 'idle';
          if (c.cond() || this.stepT >= c.max) this.finishStep();
          break;
        case 'do':
          this.finishStep();
          break;
      }
    } else {
      this.anim = this.at === this.home ? this.style.homeAnim : 'idle';
      this.wanderT -= dt;
      if (this.wanderT <= 0) {
        this.wanderT = 7 + Math.random() * 10;
        this.idleWander();
      }
    }

    // turn toward the travel direction (or the station's facing)
    let dh = this.headingTarget - this.heading;
    dh = Math.atan2(Math.sin(dh), Math.cos(dh));
    this.heading += dh * Math.min(1, dt * 10);
    this.pose(dt, moving);
  }

  private idleWander(): void {
    if (!this.wander.length) return;
    const atHome = this.at === this.home;
    const dest = atHome ? this.wander[Math.floor(Math.random() * this.wander.length)] : this.home;
    if (dest === this.at) return;
    this.steps.push({ k: 'go', to: dest }, { k: 'work', anim: 'look', dur: 1.5 + Math.random() * 2 });
    this.idleJob = true;
  }

  private nextStep(nav: NavGraph): void {
    while (!this.cur) {
      const s = this.steps.shift();
      if (!s) {
        this.idleJob = false;
        // Every job ends at home: the staff always has a station to return to.
        if (this.at !== this.home) {
          this.steps.push({ k: 'go', to: this.home });
          this.idleJob = true;
          continue;
        }
        return;
      }
      this.stepT = 0;
      this.beatPhase = 0;
      if (s.k === 'do') {
        s.fn(this);
        continue;
      }
      this.cur = s;
      if (s.k === 'go') this.plan(s.to, !!s.run, nav);
    }
  }

  private finishStep(): void {
    this.cur = null;
    this.stepT = 0;
  }

  private plan(to: string, run: boolean, nav: NavGraph): void {
    this.running = run;
    const from = this.at ?? nav.nearest(this.x, this.z).id;
    this.path = nav.path(from, to);
    this.pathI = 0;
    // Skip a first node we're already standing on.
    while (this.pathI < this.path.length - 1) {
      const p = this.path[this.pathI];
      if (Math.hypot(p.x - this.x, p.z - this.z) > 0.05) break;
      this.pathI++;
    }
    this.at = null;
  }

  /** Returns false once arrived. */
  private walk(dt: number): boolean {
    if (this.pathI >= this.path.length) return false;
    const speed = this.style.speed * (this.running ? 1.9 : 1);
    let budget = speed * dt;
    while (budget > 0 && this.pathI < this.path.length) {
      const t = this.path[this.pathI];
      const dx = t.x - this.x;
      const dz = t.z - this.z;
      const d = Math.hypot(dx, dz);
      if (d > 1e-4) this.headingTarget = Math.atan2(dx, dz);
      if (d <= budget) {
        this.x = t.x;
        this.z = t.z;
        budget -= d;
        this.pathI++;
        if (this.pathI >= this.path.length) {
          this.at = t.id;
          if (t.face !== undefined) this.headingTarget = t.face;
          return false;
        }
      } else {
        this.x += (dx / d) * budget;
        this.z += (dz / d) * budget;
        budget = 0;
      }
    }
    return true;
  }

  private pose(dt: number, moving: boolean): void {
    const run = this.running && moving;
    const carryingFront = !!this.carrying && !this.carryOver;
    const carryingOver = !!this.carrying && this.carryOver;
    this.phase += dt * (moving ? (run ? 15 : 10) : 1);
    const ph = this.phase;

    // Lane offset: two workers on the same corridor walk side by side
    // instead of merging into one four-armed blob. Faded out near the end of
    // a walk so they still arrive exactly at their station.
    let ox = 0, oz = 0;
    if (moving && this.pathI < this.path.length) {
      const end = this.path[this.path.length - 1];
      const dEnd = Math.hypot(end.x - this.x, end.z - this.z);
      const k = Math.min(1, dEnd / 0.9) * this.lane;
      ox = Math.cos(this.heading) * k;
      oz = -Math.sin(this.heading) * k;
    }
    this.group.position.set(this.x + ox, 0, this.z + oz);
    this.group.rotation.y = this.heading;

    let legSwing = 0, armL = 0, armR = 0, bob = 0, lean = 0, headX = 0, headY = 0, jump = 0;
    let armLz = 0, armRz = 0;
    if (moving) {
      const amp = run ? 0.95 : 0.65;
      legSwing = Math.sin(ph) * amp;
      armL = -Math.sin(ph) * amp * 0.9;
      armR = Math.sin(ph) * amp * 0.9;
      bob = Math.abs(Math.sin(ph)) * (run ? 0.09 : 0.055);
      lean = run ? 0.18 : 0.06;
    } else {
      bob = Math.sin(ph * 2.2) * 0.012;
      switch (this.anim) {
        case 'type':
          armL = -1.15 + Math.sin(ph * 22) * 0.12;
          armR = -1.15 + Math.sin(ph * 22 + 1.7) * 0.12;
          headX = 0.12 + Math.sin(ph * 3) * 0.04;
          this.phase += dt * 1.5;
          break;
        case 'hammer': {
          // dealing: the right hand flicks a card out across the felt on
          // every beat, then comes back to the deck in the left
          const p = (this.beatPhase % 0.42) / 0.42;
          armR = p < 0.35 ? -0.9 - (p / 0.35) * 0.75 : -1.65 + ((p - 0.35) / 0.65) * 0.75;
          armRz = p < 0.35 ? -(p / 0.35) * 0.45 : -0.45 + ((p - 0.35) / 0.65) * 0.45;
          armL = -1.05;
          armLz = 0.2;
          lean = 0.1;
          headX = 0.22;
          break;
        }
        case 'look':
          headX = -0.22 + Math.sin(ph * 0.9) * 0.05;
          headY = Math.sin(ph * 0.6) * 0.35;
          armR = -0.9;
          break;
        case 'wrench':
          // the croupier's hand on the wheel: a slow circle, then a flick
          armR = -1.25 + Math.sin(ph * 3) * 0.22;
          armRz = Math.cos(ph * 3) * 0.32;
          armL = -0.35;
          headX = 0.2;
          this.phase += dt * 1.5;
          break;
        case 'grab':
          armL = -1.0;
          armR = -1.0;
          lean = 0.25;
          headX = 0.3;
          break;
        case 'cheer':
          jump = Math.abs(Math.sin(ph * 7)) * 0.32;
          armL = -2.8 + Math.sin(ph * 14) * 0.25;
          armR = -2.8 - Math.sin(ph * 14) * 0.25;
          armLz = -0.3;
          armRz = 0.3;
          this.phase += dt * 1.5;
          break;
        case 'hands': {
          // stick 'em up: both paws high, shaking; a hop if it was startled
          const j = this.scare?.jump ?? 0;
          jump = j > 0 ? Math.sin((1 - j / 0.4) * Math.PI) * 0.22 : 0;
          armL = -2.75 + Math.sin(ph * 30) * 0.07;
          armR = -2.75 - Math.sin(ph * 30) * 0.07;
          armLz = -0.5;
          armRz = 0.5;
          headX = -0.08;
          lean = -0.06;
          this.phase += dt * 2;
          break;
        }
        case 'panic':
          jump = Math.abs(Math.sin(ph * 9)) * 0.12;
          armL = -2.6 + Math.sin(ph * 18) * 0.5;
          armR = -2.6 - Math.sin(ph * 18) * 0.5;
          this.phase += dt * 2;
          break;
        default:
          headY = Math.sin(ph * 0.35) * 0.4;
          armL = Math.sin(ph * 1.1) * 0.05;
          armR = -Math.sin(ph * 1.1) * 0.05;
      }
    }
    if (this.anim === 'panic' && moving) {
      armL = -2.6 + Math.sin(ph * 2) * 0.5;
      armR = -2.6 - Math.sin(ph * 2) * 0.5;
    }
    if (carryingFront) { armL = -1.25; armR = -1.25; armLz = 0.25; armRz = -0.25; }
    if (carryingOver) { armL = -2.95; armR = -2.95; armLz = 0.18; armRz = -0.18; }
    // paws up in a V, clear of the hat, whatever it was carrying
    if (this.anim === 'hands' && !moving) { armL = -2.75 + Math.sin(ph * 30) * 0.07; armR = -2.75 - Math.sin(ph * 30) * 0.07; armLz = -0.5; armRz = 0.5; }

    this.rig.position.y = bob + jump;
    this.rig.rotation.x = lean;
    this.legL.rotation.x = legSwing;
    this.legR.rotation.x = -legSwing;
    this.armL.rotation.x = armL;
    this.armR.rotation.x = armR;
    this.armL.rotation.z = armLz;
    this.armR.rotation.z = armRz;
    this.head.rotation.x = headX;
    this.head.rotation.y = headY;
    if (this.carrying) {
      this.carrying.rotation.y += dt * 1.2;
      this.carrying.position.y = Math.sin(ph * 0.5) * 0.02;
    }
  }
}
