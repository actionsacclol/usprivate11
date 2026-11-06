import * as THREE from 'three';
import { bakePart } from './bake';
import { CrewKit, PINK, buildRatHead, buildRatTail, newRatParts } from './crew';

// The backstage crew: three rats in nothing but red high-tops (a Chicago
// colourway: red, white, a black swoosh), dancing a synced routine across
// the back of the club's stage. All three run off one clock, so they hit
// every move together; the routine works through a set of moves, eight
// beats each at 120 to the minute, and every so often the three swap places.
// Pure poses from (move, beat), so it can be tested without a GPU.

export type Move = 'bounce' | 'wave' | 'runningman' | 'switch' | 'floss' | 'kickline' | 'jumpclap' | 'spin' | 'shuffle' | 'bodyroll' | 'dab';

/** The routine, in order, looping. */
export const ROUTINE: readonly Move[] = ['bounce', 'wave', 'runningman', 'switch', 'floss', 'kickline', 'jumpclap', 'spin', 'shuffle', 'switch', 'bodyroll', 'dab'];
export const BPM = 120;
export const BEATS_PER_MOVE = 8;

/** Where in the routine the clock is: the move, and the beat in it. */
export function routineAt(t: number): { move: Move; beat: number; index: number } {
  const beats = (t * BPM) / 60;
  const n = Math.floor(beats / BEATS_PER_MOVE);
  return { move: ROUTINE[n % ROUTINE.length], beat: beats - n * BEATS_PER_MOVE, index: n };
}

/** How many times the three have swapped places by beat count `index`
 *  (the number of 'switch' moves finished). */
export function switchesBefore(index: number): number {
  const per = ROUTINE.filter((m) => m === 'switch').length;
  let n = Math.floor(index / ROUTINE.length) * per;
  for (let i = 0; i < index % ROUTINE.length; i++) if (ROUTINE[i] === 'switch') n++;
  return n;
}

/** The three marks across the back of the stage. */
export const CREW_X = [-1.9, 0.5, 2.9] as const;
export const CREW_Z = 26.55;

/** Where dancer k stands at time t, and which way it faces: on its mark,
 *  facing the room (-z); on a 'switch' each moves one mark along, the one
 *  from the end looping round behind the other two. */
export function crewMark(k: number, t: number): { x: number; z: number; yaw: number } {
  const { move, beat, index } = routineAt(t);
  const from = switchesBefore(index);
  const a = (k + from) % 3, b = (k + from + 1) % 3;
  if (move !== 'switch') return { x: CREW_X[a] + travel(move, beat), z: CREW_Z, yaw: Math.PI };
  const e = Math.min(1, beat / BEATS_PER_MOVE);
  const s = e * e * (3 - 2 * e);
  const wrap = b < a;
  return {
    x: CREW_X[a] + (CREW_X[b] - CREW_X[a]) * s,
    z: CREW_Z + Math.sin(Math.PI * s) * (wrap ? 0.7 : -0.25),
    yaw: wrap ? Math.PI + Math.sin(Math.PI * s) * 1.2 : Math.PI,
  };
}

/** The whole line travelling together: the shuffle slides left, then
 *  right; the kick line drifts with the kicks. */
export function travel(move: Move, beat: number): number {
  if (move === 'shuffle') return Math.sin((Math.PI * beat) / 4) * 0.5;
  if (move === 'kickline') return Math.sin((Math.PI * beat) / 2) * 0.22;
  return 0;
}

export interface Pose {
  legL: number; legR: number; legLz: number; legRz: number;
  armL: number; armR: number; armLz: number; armRz: number;
  tiltX: number; tiltZ: number; dip: number; jump: number;
  headX: number; headY: number; headZ: number;
  /** extra turn about their own axis */
  spin: number;
}

const REST: Pose = { legL: 0, legR: 0, legLz: 0, legRz: 0, armL: 0, armR: 0, armLz: 0, armRz: 0, tiltX: 0, tiltZ: 0, dip: 0, jump: 0, headX: 0, headY: 0, headZ: 0, spin: 0 };

/** The pose for a move at a beat. `k` is the dancer (0–2): the wave ripples
 *  down the line; everything else is in unison. Arms: x negative raises
 *  them forward and up; z out from the body is negative on the left,
 *  positive on the right. */
export function pose(move: Move, beat: number, k: number): Pose {
  const p: Pose = { ...REST };
  const b = beat;
  const on = (x: number) => Math.max(0, Math.sin(Math.PI * x)); // a bump on each beat
  switch (move) {
    case 'bounce':
      // raise the roof: paws pushing up overhead on every beat, knees in it
      p.dip = -on(b) * 0.15;
      p.armL = -2.45 + on(b) * 0.7; p.armR = -2.45 + on(b) * 0.7;
      p.armLz = -0.4; p.armRz = 0.4;
      p.headX = on(b) * 0.28;
      p.legL = -on(b) * 0.2; p.legR = -on(b) * 0.2;
      break;
    case 'wave': {
      // arms out to the sides, a wave rolling through them and down the line
      const ph = Math.PI * (b / 2) - k * 0.7;
      p.armLz = -1.45 + Math.sin(ph) * 0.55;
      p.armRz = 1.45 + Math.sin(ph + Math.PI) * 0.55;
      p.tiltZ = Math.sin(ph) * 0.2;
      p.dip = -on(b) * 0.08;
      p.headZ = Math.sin(ph) * 0.25;
      break;
    }
    case 'runningman': {
      const s = Math.sin(Math.PI * b);
      p.legL = s * 1.0; p.legR = -s * 1.0;
      p.armL = -s * 1.1 - 0.5; p.armR = s * 1.1 - 0.5;
      p.dip = -Math.abs(s) * 0.11;
      p.tiltX = 0.2;
      p.headX = Math.abs(s) * 0.15;
      break;
    }
    case 'switch': {
      // the strut to the next spot (the position itself is crewMark's)
      const s = Math.sin(Math.PI * b);
      p.legL = s * 0.6; p.legR = -s * 0.6;
      p.armL = -s * 0.8; p.armR = s * 0.8;
      p.dip = -Math.abs(s) * 0.07;
      p.headY = 0.4;
      p.tiltZ = s * 0.08;
      break;
    }
    case 'floss': {
      // arms swing side to side, the hips the other way, fast
      const s = Math.sin(Math.PI * b * 2);
      p.armL = 0.3; p.armR = 0.3;
      p.armLz = s * 1.0; p.armRz = s * 1.0;
      p.tiltZ = -s * 0.28;
      p.legLz = s * 0.14; p.legRz = s * 0.14;
      p.dip = -Math.abs(s) * 0.05;
      break;
    }
    case 'kickline': {
      // arms out, a high kick on every beat, left then right
      const ph = b % 2;
      const kick = Math.sin(Math.PI * Math.min(1, ph % 1 / 0.7));
      if (ph < 1) p.legL = -1.5 * kick; else p.legR = -1.5 * kick;
      p.armLz = -1.35; p.armRz = 1.35;
      p.armL = -0.3; p.armR = -0.3;
      p.tiltX = -0.15 * kick;
      p.jump = 0.05 * kick;
      p.headX = -0.1 * kick;
      break;
    }
    case 'jumpclap': {
      // up on the beat, knees tucked, paws clapping overhead
      const ph = b % 1;
      p.jump = Math.sin(Math.PI * ph) * 0.34;
      p.legL = -0.75 * Math.sin(Math.PI * ph); p.legR = p.legL;
      p.armL = -2.85; p.armR = -2.85;
      const clap = Math.abs(Math.cos(Math.PI * ph));
      p.armLz = -0.12 - clap * 0.5; p.armRz = 0.12 + clap * 0.5;
      p.headX = -0.2;
      break;
    }
    case 'spin': {
      // a full turn every two beats, arms tucked, then flung out on the hit
      const ph = (b % 2) / 2;
      const e = ph < 0.7 ? ph / 0.7 : 1;
      p.spin = e * e * (3 - 2 * e) * Math.PI * 2;
      p.armL = -1.3; p.armR = -1.3; p.armLz = 0.45; p.armRz = -0.45;
      if (ph >= 0.7) { p.armLz = -1.5; p.armRz = 1.5; p.armL = -0.5; p.armR = -0.5; p.dip = -0.08; }
      p.jump = ph < 0.7 ? 0.06 : 0;
      break;
    }
    case 'shuffle': {
      // feet kicking out to the side in turn (the line slides: travel())
      const s = Math.sin(Math.PI * b * 2);
      p.legLz = -Math.max(0, s) * 0.8; p.legRz = Math.max(0, -s) * 0.8;
      p.legL = -Math.max(0, s) * 0.3; p.legR = -Math.max(0, -s) * 0.3;
      p.armL = -1.1; p.armR = -1.1; p.armLz = -0.45 + s * 0.2; p.armRz = 0.45 + s * 0.2;
      p.dip = -Math.abs(s) * 0.08;
      p.headX = Math.abs(s) * 0.15;
      break;
    }
    case 'bodyroll': {
      // a wave down the body: chest out, hips back, a dip, again
      const ph = Math.PI * b;
      p.tiltX = Math.sin(ph) * 0.3;
      p.headX = -Math.sin(ph + 0.8) * 0.35;
      p.dip = -(1 - Math.cos(ph)) * 0.06;
      p.legL = -(1 - Math.cos(ph)) * 0.15; p.legR = p.legL;
      p.armL = -0.6 - Math.sin(ph) * 0.5; p.armR = -0.6 - Math.sin(ph) * 0.5;
      p.armLz = -0.3; p.armRz = 0.3;
      break;
    }
    case 'dab': {
      // two beats each way: one arm up and out, the other across the face
      const left = Math.floor(b / 2) % 2 === 0;
      const hit = Math.min(1, (b % 2) / 0.25);
      if (left) {
        p.armL = -2.3 * hit; p.armLz = -0.9 * hit;
        p.armR = -1.7 * hit; p.armRz = -0.9 * hit;
        p.headX = 0.45 * hit; p.headY = 0.5 * hit;
      } else {
        p.armR = -2.3 * hit; p.armRz = 0.9 * hit;
        p.armL = -1.7 * hit; p.armLz = 0.9 * hit;
        p.headX = 0.45 * hit; p.headY = -0.5 * hit;
      }
      p.dip = -0.07 * hit;
      p.tiltZ = (left ? 0.1 : -0.1) * hit;
      break;
    }
  }
  return p;
}

function mix(a: Pose, b: Pose, k: number): Pose {
  const out = { ...a };
  for (const key of Object.keys(a) as (keyof Pose)[]) out[key] = a[key] + (b[key] - a[key]) * k;
  return out;
}

/** A pair of red high-tops on a leg: the upper, a white side panel, a black
 *  swoosh, a white midsole, laces. */
function sneaker(kit: CrewKit, leg: THREE.Group, side: 1 | -1): void {
  const m = (geo: THREE.BufferGeometry, color: string, x: number, y: number, z: number) => {
    const o = new THREE.Mesh(geo, kit.mat(color, { rough: 0.55 }));
    o.position.set(x, y, z);
    o.castShadow = true;
    leg.add(o);
    return o;
  };
  m(new THREE.BoxGeometry(0.15, 0.04, 0.27), '#f4f1ea', 0, -0.335, 0.04); // the sole
  m(new THREE.BoxGeometry(0.14, 0.09, 0.25), '#c8102e', 0, -0.275, 0.04); // the upper
  m(new THREE.BoxGeometry(0.13, 0.11, 0.1), '#c8102e', 0, -0.2, -0.03); // the high collar
  m(new THREE.BoxGeometry(0.142, 0.05, 0.1), '#f4f1ea', 0, -0.27, 0.04); // the side panel
  const swoosh = m(new THREE.BoxGeometry(0.145, 0.016, 0.12), '#0b0b0d', 0, -0.262, 0.035);
  swoosh.rotation.x = -0.25 * side;
  m(new THREE.BoxGeometry(0.06, 0.012, 0.12), '#f4f1ea', 0, -0.226, 0.1); // the laces
  m(new THREE.BoxGeometry(0.135, 0.03, 0.04), '#0b0b0d', 0, -0.15, -0.06); // the collar's black cuff
}

/** One of the three: a bare rat (fur, tail, pink paws) in red high-tops. */
export class BackupDancer {
  readonly parts = newRatParts();
  private cur: Pose = { ...REST };
  private frozen = 0;

  constructor(kit: CrewKit, readonly k: number, fur: string) {
    const p = this.parts;
    const g = kit.g;
    const shadow = new THREE.Mesh(g.shadow, kit.shadowMat);
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.012;
    shadow.scale.setScalar(0.85);
    p.group.add(shadow, p.rig);
    const furMat = kit.mat(fur, { rough: 0.85 });
    const belly = kit.mat(new THREE.Color(fur).lerp(new THREE.Color('#f4ece0'), 0.35).getStyle(), { rough: 0.85 });
    const m = (geo: THREE.BufferGeometry, mat: THREE.Material, x = 0, y = 0, z = 0) => {
      const o = new THREE.Mesh(geo, mat);
      o.position.set(x, y, z);
      o.castShadow = true;
      return o;
    };
    for (const [leg, sx, side] of [[p.legL, -0.1, -1], [p.legR, 0.1, 1]] as const) {
      leg.position.set(sx, 0.36, 0);
      leg.add(m(g.limb, furMat, 0, -0.15, 0));
      sneaker(kit, leg, side);
      p.rig.add(leg);
    }
    const body = m(g.body, furMat, 0, 0.58, 0);
    const tummy = m(new THREE.SphereGeometry(0.16, 8, 6), belly, 0, 0.55, 0.07);
    tummy.scale.set(0.95, 1.25, 0.6);
    p.rig.add(body, tummy);
    buildRatTail(kit, p.rig);
    for (const [arm, sx] of [[p.armL, -0.25], [p.armR, 0.25]] as const) {
      arm.position.set(sx, 0.76, 0);
      arm.add(m(g.limb, furMat, 0, -0.13, 0), m(g.hand, kit.mat(PINK), 0, -0.27, 0));
      p.rig.add(arm);
    }
    buildRatHead(kit, p.head, fur, 'none');
    p.rig.add(p.head);
    for (const part of [p.legL, p.legR, p.armL, p.armR]) bakePart(part, kit.skinMat, false);
    bakePart(p.rig, kit.skinMat, true);
    bakePart(p.head, kit.skinMat, true);
  }

  get group(): THREE.Group { return this.parts.group; }

  spook(secs: number): void {
    this.frozen = Math.max(this.frozen, secs);
  }

  /** Pose for the routine at time t; `at` is where it stands now and `yaw`
   *  which way it faces. */
  update(dt: number, t: number, at: { x: number; y: number; z: number }, yaw: number): void {
    const { move, beat, index } = routineAt(t);
    let want = pose(move, beat, this.k);
    // the last beat of a move blends into the next one's first
    if (beat > BEATS_PER_MOVE - 0.5) {
      const next = routineAt(t + 0.5);
      if (next.index !== index) want = mix(want, pose(next.move, 0, this.k), (beat - (BEATS_PER_MOVE - 0.5)) / 0.5);
    }
    if (this.frozen > 0) {
      this.frozen -= dt;
      want = { ...REST, armL: -2.75 + Math.sin(t * 30) * 0.06, armR: -2.75 - Math.sin(t * 30) * 0.06, armLz: -0.5, armRz: 0.5 };
    }
    // sharp, but not snapping: the hits land
    const k = 1 - Math.exp(-22 * dt);
    this.cur = mix(this.cur, want, k);
    const c = this.cur;
    const p = this.parts;
    p.group.position.set(at.x, at.y, at.z);
    p.group.rotation.y = yaw + (this.frozen > 0 ? 0 : want.spin);
    p.legL.rotation.set(c.legL, 0, c.legLz);
    p.legR.rotation.set(c.legR, 0, c.legRz);
    p.armL.rotation.set(c.armL, 0, c.armLz);
    p.armR.rotation.set(c.armR, 0, c.armRz);
    p.rig.rotation.set(c.tiltX, 0, c.tiltZ);
    p.rig.position.y = c.dip + c.jump;
    p.head.rotation.set(c.headX, c.headY, c.headZ);
  }
}
