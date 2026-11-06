import * as THREE from 'three';
import type { Seg } from '../walk';
import { canvasTex } from './casinoTextures';

// The revolver on the casino floor: what a shot hits, who it nearly hits, and
// what the room says about it. Nobody gets hurt: it's a cartoon mob casino,
// so a bullet that would have found a rat goes past its whiskers, and the
// wall behind takes the hole. Draw on a rat and it puts its paws up; fire
// and the whole room does.

/** Said by the rat you're pointing it at. */
export const AIM_LINES = [
  'Easy… easy, pal.',
  'Whoa. Put that thing down.',
  'I got a family, see?',
  'Take the chips! Take ’em!',
  'We’re all friends here, right?',
  'Nobody needs to get hurt.',
  'Not the face! Not the face!',
  'You’re makin’ me nervous.',
];

/** Shouted round the room when it goes off. */
export const SHOT_LINES = [
  'WHO’S SHOOTIN’?!',
  'HIT THE DECK!',
  'Not in the house!',
  'Somebody get Bluey!',
  'Are you nuts?!',
  'Watch the felt!',
  'You tryin’ to bring the heat down on us?!',
  'Cops’ll be here in five!',
];

/** The rat it went past. */
export const GRAZE_LINES = [
  'That went through my HAT!',
  'My whiskers! You singed my whiskers!',
  'Missed me, ya bum!',
  'You coulda killed me!',
  'I felt the wind off that one!',
];

/** Bluey, from the podium. */
export const BOSS_LINES = [
  'NOT IN MY CASINO!',
  'Put the piece away, kid.',
  'Somebody’s payin’ for that wall.',
  'One more and you’re swimmin’ with the fishes.',
];

/** Said to you once you've had a few. */
export const DRUNK_LINES = [
  'You’ve had a few, huh pal?',
  'Go easy on the sauce.',
  'Bar’s that way. Actually, no — it ain’t.',
  'You’re swayin’, friend.',
];

export function pick<T>(xs: readonly T[], r = Math.random()): T {
  return xs[Math.min(xs.length - 1, Math.floor(r * xs.length))];
}

/**
 * How close a ray passes to a standing rat: a vertical post at (x, z) from
 * the floor to `top`. Returns the distance along the ray at the closest point
 * and how far off the post it is there, or null if that's behind the eye,
 * past `maxT`, or above/below the rat.
 */
export function rayPastRat(
  o: { x: number; y: number; z: number }, d: { x: number; y: number; z: number },
  x: number, z: number, maxT: number, top = 1.75,
): { t: number; off: number } | null {
  const h = Math.hypot(d.x, d.z);
  if (h < 1e-6) return null;
  // closest approach in plan, then check the height there
  const t = ((x - o.x) * d.x + (z - o.z) * d.z) / (h * h);
  if (t <= 0 || t > maxT) return null;
  const px = o.x + d.x * t, pz = o.z + d.z * t, py = o.y + d.y * t;
  if (py < 0 || py > top) return null;
  return { t, off: Math.hypot(px - x, pz - z) };
}

/** Does the straight line from a to b cross a wall? (Plan view, the walls
 *  the walker collides with.) */
export function wallBetween(ax: number, az: number, bx: number, bz: number, walls: readonly Seg[]): boolean {
  const cross = (x1: number, z1: number, x2: number, z2: number) => x1 * z2 - z1 * x2;
  const rx = bx - ax, rz = bz - az;
  for (const [x0, z0, x1, z1] of walls) {
    const sx = x1 - x0, sz = z1 - z0;
    const den = cross(rx, rz, sx, sz);
    if (Math.abs(den) < 1e-9) continue;
    const qx = x0 - ax, qz = z0 - az;
    const t = cross(qx, qz, sx, sz) / den;
    const u = cross(qx, qz, rx, rz) / den;
    if (t > 1e-4 && t < 1 - 1e-4 && u >= 0 && u <= 1) return true;
  }
  return false;
}

/** The holes a session's shots leave in the walls, tables and floor: one
 *  instanced draw, the oldest reused once it's full. */
export class BulletHoles {
  readonly mesh: THREE.InstancedMesh;
  private next = 0;
  private count = 0;
  private _m = new THREE.Matrix4();
  private _q = new THREE.Quaternion();
  private _s = new THREE.Vector3();
  private _z = new THREE.Vector3(0, 0, 1);

  constructor(private max = 48) {
    const tex = canvasTex(16, 16, (g) => {
      // a pixel-art hole: black core, a splintered pale rim
      g.fillStyle = 'rgba(200,184,160,0.55)';
      for (const [x, y] of [[5, 3], [10, 3], [3, 6], [12, 7], [4, 11], [11, 12], [7, 13], [8, 2]]) g.fillRect(x, y, 2, 2);
      g.fillStyle = 'rgba(40,30,24,0.9)';
      g.fillRect(5, 5, 6, 6);
      g.fillRect(6, 4, 4, 8);
      g.fillRect(4, 6, 8, 4);
      g.fillStyle = '#050304';
      g.fillRect(6, 6, 4, 4);
    });
    tex.magFilter = THREE.NearestFilter;
    tex.minFilter = THREE.NearestFilter;
    tex.generateMipmaps = false;
    const mat = new THREE.MeshBasicMaterial({
      map: tex, transparent: true, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2,
    });
    this.mesh = new THREE.InstancedMesh(new THREE.PlaneGeometry(0.08, 0.08), mat, max);
    this.mesh.count = 0;
    this.mesh.frustumCulled = false;
    this.mesh.renderOrder = 1;
  }

  /** A hole at `p` on a surface facing `n` (world space, unit). */
  add(p: THREE.Vector3, n: THREE.Vector3): void {
    this._q.setFromUnitVectors(this._z, n);
    this._q.multiply(new THREE.Quaternion().setFromAxisAngle(this._z, Math.random() * Math.PI * 2));
    const s = 0.8 + Math.random() * 0.4;
    this._m.compose(p.clone().addScaledVector(n, 0.004), this._q, this._s.set(s, s, s));
    this.mesh.setMatrixAt(this.next, this._m);
    this.next = (this.next + 1) % this.max;
    this.count = Math.min(this.max, this.count + 1);
    this.mesh.count = this.count;
    this.mesh.instanceMatrix.needsUpdate = true;
  }

  dispose(): void {
    this.mesh.geometry.dispose();
    const m = this.mesh.material as THREE.MeshBasicMaterial;
    m.map?.dispose();
    m.dispose();
  }
}
