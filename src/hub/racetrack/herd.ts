import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';

// Every horse on the card — the racers on the track and the runners walking
// up to the gate — is drawn by ONE Herd: six instanced meshes (coat, legs,
// silks, cap + saddle cloth, blob shadow, lane glow), repacked every frame,
// so thirty galloping horses cost six draw calls. A low-poly thoroughbred
// with a crouched jockey: silks in the signal source's colour, cap and
// saddle cloth in the market category's.
//
// The engine owns the story (where each horse is, how fast); the herd only
// turns a Pose into matrices. No allocations per frame.

export interface Pose {
  x: number; y: number; z: number;
  /** Heading: local +x → (cos ψ, 0, −sin ψ). */
  yaw: number;
  scale: number;
  /** Gait phase (radians) and stride 0 (standing) … 1 (full gallop). */
  phase: number;
  stride: number;
  coat: THREE.Color;
  silks: THREE.Color;
  cap: THREE.Color;
  /** Lane glow under the horse, 0..1. */
  glow: number;
}

interface Part { geo: THREE.BufferGeometry; color: [number, number, number] }

function piece(g: THREE.BufferGeometry, m: THREE.Matrix4, shade: number): Part {
  const out = g.index ? g.toNonIndexed() : g.clone();
  g.dispose();
  for (const k of Object.keys(out.attributes)) if (k !== 'position' && k !== 'normal') out.deleteAttribute(k);
  out.applyMatrix4(m);
  return { geo: out, color: [shade, shade, shade] };
}

function bake(parts: Part[]): THREE.BufferGeometry {
  for (const p of parts) {
    const n = p.geo.attributes.position.count;
    const c = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) c.set(p.color, i * 3);
    p.geo.setAttribute('color', new THREE.BufferAttribute(c, 3));
  }
  const g = mergeGeometries(parts.map((p) => p.geo))!;
  parts.forEach((p) => p.geo.dispose());
  return g;
}

const M = new THREE.Matrix4();
const Q = new THREE.Quaternion();
const E = new THREE.Euler();
const V = new THREE.Vector3();
const S = new THREE.Vector3();
function at(x: number, y: number, z: number, rz = 0, sx = 1, sy = 1, sz = 1, rx = 0, ry = 0): THREE.Matrix4 {
  return M.compose(V.set(x, y, z), Q.setFromEuler(E.set(rx, ry, rz)), S.set(sx, sy, sz));
}

function horseBody(): THREE.BufferGeometry {
  const p: Part[] = [];
  p.push(piece(new THREE.CylinderGeometry(0.15, 0.15, 0.56, 7).rotateZ(Math.PI / 2), at(0, 0.6, 0, 0, 1, 1.12, 0.92), 1));
  p.push(piece(new THREE.IcosahedronGeometry(0.17, 0), at(0.27, 0.61, 0, 0, 1, 1.05, 0.92), 1));
  p.push(piece(new THREE.IcosahedronGeometry(0.18, 0), at(-0.27, 0.63, 0, 0, 1, 1, 0.95), 1));
  p.push(piece(new THREE.CylinderGeometry(0.075, 0.12, 0.42, 6), at(0.43, 0.82, 0, -0.75), 1));
  p.push(piece(new THREE.CylinderGeometry(0.042, 0.075, 0.3, 6), at(0.62, 0.92, 0, -2.1), 1));
  for (const z of [-0.035, 0.035]) p.push(piece(new THREE.ConeGeometry(0.025, 0.08, 4), at(0.55, 1.07, z, 0.2), 0.8));
  // mane and tail, darker than the coat
  p.push(piece(new THREE.BoxGeometry(0.04, 0.42, 0.035), at(0.36, 0.89, 0, -0.75), 0.22));
  p.push(piece(new THREE.ConeGeometry(0.06, 0.4, 5), at(-0.5, 0.5, 0, 2.5), 0.22));
  // the jockey's boots and breeches ride on the horse's mesh, so they stay dark
  for (const z of [-0.085, 0.085]) p.push(piece(new THREE.BoxGeometry(0.16, 0.05, 0.04), at(0.06, 0.8, z, -0.4), 0.12));
  return bake(p);
}

function horseLeg(): THREE.BufferGeometry {
  const p: Part[] = [];
  // hip at the origin, hoof at y = −0.5
  p.push(piece(new THREE.CylinderGeometry(0.038, 0.026, 0.46, 5), at(0, -0.23, 0), 1));
  p.push(piece(new THREE.CylinderGeometry(0.03, 0.038, 0.05, 5), at(0, -0.475, 0), 0.15));
  return bake(p);
}

function jockey(): THREE.BufferGeometry {
  const p: Part[] = [];
  // a crouched torso (rounded, not a crate), shoulders forward over the neck
  p.push(piece(new THREE.CylinderGeometry(0.065, 0.075, 0.24, 6).rotateZ(Math.PI / 2), at(0.07, 0.93, 0, -0.38, 1, 1, 1.05), 1));
  for (const z of [-0.07, 0.07]) p.push(piece(new THREE.CylinderGeometry(0.022, 0.02, 0.2, 5).rotateZ(Math.PI / 2), at(0.21, 0.9, z, -0.6), 0.9));
  return bake(p);
}

function capAndCloth(): THREE.BufferGeometry {
  const p: Part[] = [];
  p.push(piece(new THREE.SphereGeometry(0.055, 7, 5), at(0.2, 1.02, 0, 0, 1.1, 0.95, 1), 1));
  p.push(piece(new THREE.BoxGeometry(0.06, 0.01, 0.06), at(0.265, 1.005, 0), 0.6));
  // a slim saddle cloth on each flank, under the jockey's knee
  for (const z of [-0.142, 0.142]) p.push(piece(new THREE.BoxGeometry(0.2, 0.1, 0.01), at(-0.04, 0.66, z), 1));
  return bake(p);
}

function shadowTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const g = c.getContext('2d')!;
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, 'rgba(0,0,0,0.75)');
  grd.addColorStop(0.55, 'rgba(0,0,0,0.35)');
  grd.addColorStop(1, 'rgba(0,0,0,0)');
  g.fillStyle = grd;
  g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
}

/** Satin silks: a little self-light in their own colour, so they read
 *  under the floodlights and catch the bloom. */
function satin(self: number): THREE.MeshStandardMaterial {
  const m = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.32, metalness: 0.05, flatShading: true });
  m.onBeforeCompile = (sh) => {
    sh.fragmentShader = sh.fragmentShader.replace(
      '#include <emissivemap_fragment>',
      `#include <emissivemap_fragment>\n  totalEmissiveRadiance += vColor.rgb * ${self.toFixed(3)};`,
    );
  };
  return m;
}

// gait: hips (body-local) and the rotary gallop's leg order
const HIPS: [number, number, number, number][] = [
  [-0.27, 0.52, -0.08, 0.0], [-0.27, 0.52, 0.08, 0.7], [0.26, 0.5, 0.08, 2.5], [0.26, 0.5, -0.08, 3.2],
];

export class Herd {
  readonly group = new THREE.Group();
  private body: THREE.InstancedMesh;
  private legs: THREE.InstancedMesh;
  private silks: THREE.InstancedMesh;
  private caps: THREE.InstancedMesh;
  private shadows: THREE.InstancedMesh;
  private glows: THREE.InstancedMesh;
  private shadowTex = shadowTexture();
  private n = 0;
  private readonly cap: number;

  private base = new THREE.Matrix4();
  private bodyM = new THREE.Matrix4();
  private tmp = new THREE.Matrix4();
  private rot = new THREE.Matrix4();
  private q = new THREE.Quaternion();
  private v = new THREE.Vector3();
  private s = new THREE.Vector3();
  private up = new THREE.Vector3(0, 1, 0);
  private c = new THREE.Color();

  constructor(glowTex: THREE.Texture, cap = 48) {
    this.cap = cap;
    const coatMat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.5, metalness: 0.12, flatShading: true });
    this.body = new THREE.InstancedMesh(horseBody(), coatMat, cap);
    this.legs = new THREE.InstancedMesh(horseLeg(), coatMat, cap * 4);
    this.silks = new THREE.InstancedMesh(jockey(), satin(0.28), cap);
    this.caps = new THREE.InstancedMesh(capAndCloth(), satin(0.35), cap);
    const plane = new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2);
    this.shadows = new THREE.InstancedMesh(plane, new THREE.MeshBasicMaterial({ map: this.shadowTex, transparent: true, depthWrite: false, opacity: 0.85 }), cap);
    this.glows = new THREE.InstancedMesh(plane.clone(), new THREE.MeshBasicMaterial({ map: glowTex, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false }), cap);
    this.shadows.renderOrder = 1;
    this.glows.renderOrder = 2;
    for (const m of [this.body, this.legs, this.silks, this.caps, this.shadows, this.glows]) {
      m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      // the herd spans the whole oval and is repacked every frame: its
      // bounding sphere would always be stale
      m.frustumCulled = false;
      m.count = 0;
    }
    // allocate the colour buffers up front
    this.c.set('#ffffff');
    for (let i = 0; i < cap; i++) {
      this.body.setColorAt(i, this.c);
      this.silks.setColorAt(i, this.c);
      this.caps.setColorAt(i, this.c);
      this.glows.setColorAt(i, this.c);
    }
    for (let i = 0; i < cap * 4; i++) this.legs.setColorAt(i, this.c);
    for (const m of [this.body, this.legs, this.silks, this.caps, this.glows]) m.instanceColor!.setUsage(THREE.DynamicDrawUsage);
    this.group.add(this.shadows, this.glows, this.body, this.legs, this.silks, this.caps);
  }

  begin(): void {
    this.n = 0;
  }

  add(p: Pose): void {
    if (this.n >= this.cap || p.scale <= 0.001) return;
    const i = this.n++;
    const st = p.stride;
    this.q.setFromAxisAngle(this.up, p.yaw);
    this.base.compose(this.v.set(p.x, p.y, p.z), this.q, this.s.set(p.scale, p.scale, p.scale));
    // gallop: the body bounces and rocks fore-and-aft with the stride
    const bob = st * 0.05 * Math.abs(Math.sin(p.phase));
    const rock = st * 0.07 * Math.sin(p.phase + 1.1);
    this.rot.makeRotationZ(rock);
    this.tmp.makeTranslation(0, bob, 0);
    this.bodyM.multiplyMatrices(this.base, this.tmp).multiply(this.rot);
    this.body.setMatrixAt(i, this.bodyM);
    this.silks.setMatrixAt(i, this.bodyM);
    this.caps.setMatrixAt(i, this.bodyM);
    for (let k = 0; k < 4; k++) {
      const h = HIPS[k];
      const swing = st * 0.75 * Math.sin(p.phase + h[3]) + (1 - st) * 0.02;
      this.tmp.makeTranslation(h[0], h[1], h[2]);
      this.rot.makeRotationZ(swing);
      this.tmp.multiply(this.rot);
      this.rot.multiplyMatrices(this.bodyM, this.tmp);
      this.legs.setMatrixAt(i * 4 + k, this.rot);
      this.legs.setColorAt(i * 4 + k, p.coat);
    }
    // ground decals ride the base, not the bounce
    this.tmp.makeScale(1.25, 1, 0.55);
    this.tmp.setPosition(0.02, 0.015, 0);
    this.rot.multiplyMatrices(this.base, this.tmp);
    this.shadows.setMatrixAt(i, this.rot);
    this.tmp.makeScale(1.7 * (0.6 + p.glow * 0.4), 1, 0.95 * (0.6 + p.glow * 0.4));
    this.tmp.setPosition(0, 0.02, 0);
    this.rot.multiplyMatrices(this.base, this.tmp);
    this.glows.setMatrixAt(i, this.rot);
    this.body.setColorAt(i, p.coat);
    this.silks.setColorAt(i, p.silks);
    this.caps.setColorAt(i, p.cap);
    this.c.copy(p.cap).multiplyScalar(0.55 * p.glow);
    this.glows.setColorAt(i, this.c);
  }

  end(): void {
    const n = this.n;
    for (const m of [this.body, this.silks, this.caps, this.shadows, this.glows]) {
      m.count = n;
      m.instanceMatrix.needsUpdate = true;
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
    }
    this.legs.count = n * 4;
    this.legs.instanceMatrix.needsUpdate = true;
    this.legs.instanceColor!.needsUpdate = true;
  }

  dispose(): void {
    const mats = new Set<THREE.Material>();
    for (const m of [this.body, this.legs, this.silks, this.caps, this.shadows, this.glows]) {
      m.geometry.dispose();
      mats.add(m.material as THREE.Material);
      m.dispose();
    }
    mats.forEach((m) => m.dispose());
    this.shadowTex.dispose();
  }
}
