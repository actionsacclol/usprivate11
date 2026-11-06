import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { makeGlowTexture } from '../engine/items';
import { F_MONO, F_SANS } from '../engine/overlay';

// The race track's fixed furniture: the oval, its rails and price lanes, the
// grandstand and its crowd, the floodlight towers, the starting gantry, the
// finish line with its photo-finish booth, the tote board, the winner's
// circle and the LOSSES paddock. A night meet under floodlights — warm light
// pools on dark loam, chalk lane lines, a glowing stand — so it can't be
// mistaken for the casino floor or the poker room.
//
// Draw-call budget (see engine/bake.ts for why it matters under ANGLE): every
// static structure is ONE vertex-coloured mesh plus ONE emissive mesh, the
// ground (dirt, turf, apron, lane lines, finish line, light pools) is ONE
// shader quad, the crowd is ONE Points, the light cones ONE mesh. No shadow
// maps: the racers carry their own blob shadows (herd.ts) and the floodlight
// pools are painted by the ground shader.

// ── geometry of the oval ───────────────────────────────────────────────────
// A stadium: two straights joined by two semicircles. Racers run counter-
// clockwise seen from above (as US meets do): right → left down the far
// "home" straight in front of the grandstand, left → right along the near
// backstretch where the starting gantry stands.

/** Half the length of each straight. */
export const LH = 7;
/** Inner rail radius (at the turns). */
export const R0 = 4.6;
/** Track width, inner rail to outer rail. */
export const TW = 4.4;
/** The wire: across the home (far) straight, in front of the stand. */
export const X_FINISH = -3;
/** The starting gantry, across the backstretch (near straight). */
export const X_GATE = 2.2;
/** Stalls in the gantry, inner rail to outer rail. */
export const STALLS = 8;
/** The gap in the outer rail runners come in through (the chute). */
export const X_CHUTE = X_GATE - 6;

/** Polymarket prices live in 1..99. Inner lanes are favourites, outer
 *  longshots: the lane IS the market's own probability, as the old orbit
 *  radius was. */
export function laneOffset(priceCents: number): number {
  const p = THREE.MathUtils.clamp(priceCents, 1, 99);
  return 0.35 + (1 - p / 100) * (TW - 0.7);
}
export const LANES = [90, 70, 50, 30, 10];

/** Length of one lap on a lane `off` out from the inner rail. */
export function lapLength(off: number): number {
  return 4 * LH + 2 * Math.PI * (R0 + off);
}

/** Where the wire is along a lane, measured from the lap's start (the far
 *  straight's right end). */
export function finishS(): number {
  return LH - X_FINISH;
}

/** Where the gantry is along a lane. */
export function gateS(off: number): number {
  return 2 * LH + Math.PI * (R0 + off) + (X_GATE + LH);
}

/** A point `s` along lane `off`, and its heading. Heading ψ is the yaw that
 *  turns local +x onto the direction of travel: (cos ψ, 0, −sin ψ). */
export function trackPoint(s: number, off: number, out: THREE.Vector3): number {
  const R = R0 + off;
  const P = lapLength(off);
  s = ((s % P) + P) % P;
  const a = 2 * LH;
  const b = a + Math.PI * R;
  const c = b + 2 * LH;
  if (s < a) {
    out.set(LH - s, 0, -R);
    return Math.PI;
  }
  if (s < b) {
    const f = (s - a) / R;
    out.set(-LH - R * Math.sin(f), 0, -R * Math.cos(f));
    return Math.PI + f;
  }
  if (s < c) {
    out.set(-LH + (s - b), 0, R);
    return 2 * Math.PI;
  }
  const f = (s - c) / R;
  out.set(LH + R * Math.sin(f), 0, R * Math.cos(f));
  return 2 * Math.PI + f;
}

/** Racers that win head here; the coin stream lands here. Infield, by the
 *  left turn, a short canter from the wire. */
export const WINS_POS = new THREE.Vector3(-7.6, 0, 0.2);
/** Racers that lose are led off to here: outside the right turn. */
export const LOSS_POS = new THREE.Vector3(18.4, 0, 0.4);
/** The infield pond and its two radii. */
const POND = new THREE.Vector3(8.6, 0, 0.4);
const POND_R = [2.0, 1.7];
/** The tote board, infield centre, facing the camera side. */
export const TOTE_POS = new THREE.Vector3(2.4, 0, -1.3);
export const TOTE_W = 7.2;
export const TOTE_H = 2.7;
export const TOTE_Y = 0.75;

/** Category → cap and saddle-cloth colour (the old orbit's sector colours). */
export const CATS = ['Crypto', 'Sports', 'Politics', 'Economics', 'Financials', 'Climate', 'Entertainment', 'Other'] as const;
export type Cat = (typeof CATS)[number];
export const CAT_COLOR: Record<Cat, string> = {
  Crypto: '#22D3EE', Sports: '#4ADE80', Politics: '#A78BFA', Economics: '#FBBF24',
  Financials: '#FB923C', Climate: '#60A5FA', Entertainment: '#F472B6', Other: '#CBD5E1',
};

/** Unknown categories (Polymarket has dozens) share "Other" rather than
 *  inventing a colour per string. */
export function catOf(category: string | null | undefined): Cat {
  const c = (category ?? '').trim().toLowerCase();
  for (const k of CATS) if (k.toLowerCase() === c) return k;
  if (/weather|climate/.test(c)) return 'Climate';
  if (/financ|compan|stock/.test(c)) return 'Financials';
  if (/econ/.test(c)) return 'Economics';
  if (/sport/.test(c)) return 'Sports';
  if (/polit|election/.test(c)) return 'Politics';
  if (/crypto/.test(c)) return 'Crypto';
  if (/entertain|culture|music|film|tv/.test(c)) return 'Entertainment';
  return 'Other';
}

/** Order source → the jockey's silks. */
export const SILKS: Record<string, string> = {
  crypto: '#EC4899', script: '#A855F7', external: '#F59E0B',
};
export function silksOf(source: string | null | undefined): string {
  const s = source ?? '';
  return SILKS[s.startsWith('script:') ? 'script' : s] ?? SILKS.crypto;
}

/** Traditional saddle-cloth colours, stall 1..8: the gantry's number plates. */
const CLOTH = ['#E11D48', '#F8FAFC', '#2563EB', '#FACC15', '#16A34A', '#111827', '#F97316', '#EC4899'];

// ── building blocks ────────────────────────────────────────────────────────

/** Collects static pieces into one vertex-coloured geometry per material. */
class Kit {
  private parts: THREE.BufferGeometry[] = [];
  private m = new THREE.Matrix4();
  private q = new THREE.Quaternion();
  private e = new THREE.Euler();
  private c = new THREE.Color();

  add(geo: THREE.BufferGeometry, color: string, x: number, y: number, z: number, rx = 0, ry = 0, rz = 0, sx = 1, sy = 1, sz = 1): void {
    this.q.setFromEuler(this.e.set(rx, ry, rz));
    this.m.compose(new THREE.Vector3(x, y, z), this.q, new THREE.Vector3(sx, sy, sz));
    this.addM(geo, color, this.m);
  }

  addM(geo: THREE.BufferGeometry, color: string, m: THREE.Matrix4): void {
    const g = geo.index ? geo.toNonIndexed() : geo.clone();
    for (const k of Object.keys(g.attributes)) if (k !== 'position' && k !== 'normal') g.deleteAttribute(k);
    g.applyMatrix4(m);
    this.c.set(color);
    const n = g.attributes.position.count;
    const col = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) { col[i * 3] = this.c.r; col[i * 3 + 1] = this.c.g; col[i * 3 + 2] = this.c.b; }
    g.setAttribute('color', new THREE.BufferAttribute(col, 3));
    this.parts.push(g);
  }

  build(): THREE.BufferGeometry | null {
    if (!this.parts.length) return null;
    const g = mergeGeometries(this.parts);
    this.parts.forEach((p) => p.dispose());
    this.parts = [];
    return g;
  }
}

const BOX = new THREE.BoxGeometry(1, 1, 1);
const CYL = new THREE.CylinderGeometry(1, 1, 1, 8);
const CYL4 = new THREE.CylinderGeometry(1, 1, 1, 4);

// ── the ground: one shader quad ───────────────────────────────────────────

const GROUND_VS = /* glsl */`
  varying vec2 vW;
  void main() {
    vec4 w = modelMatrix * vec4(position, 1.0);
    vW = w.xz;
    gl_Position = projectionMatrix * viewMatrix * w;
  }`;

function groundFs(): string {
  const lanes = LANES.map((p) => laneOffset(p).toFixed(4));
  return /* glsl */`
  uniform float uTime; uniform float uFlash; uniform float uWin; uniform float uLoss;
  varying vec2 vW;
  const float LH = ${LH.toFixed(3)};
  const float R0 = ${R0.toFixed(3)};
  const float TW = ${TW.toFixed(3)};
  const float XF = ${X_FINISH.toFixed(3)};
  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x), mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
  }
  float line(float d, float w) {
    float fw = max(fwidth(d), 1e-4);
    return 1.0 - smoothstep(w, w + fw * 1.5, abs(d));
  }
  float pool(vec2 p, vec2 c, float r) { vec2 d = p - c; return exp(-dot(d, d) / (r * r)); }
  void main() {
    vec2 p = vW;
    // offset from the inner rail: < 0 infield, 0..TW track, > TW outside
    vec2 q = vec2(max(abs(p.x) - LH, 0.0), p.y);
    float off = length(q) - R0;
    float n = vnoise(p * 3.1) * 0.6 + vnoise(p * 9.7) * 0.4;

    // floodlight pools: four corner towers and the stand's roof lights
    float light = 0.22
      + 0.62 * pool(p, vec2(-9.5, -3.5), 8.0) + 0.62 * pool(p, vec2(9.5, -3.5), 8.0)
      + 0.55 * pool(p, vec2(-9.5, 4.5), 8.0) + 0.55 * pool(p, vec2(9.5, 4.5), 8.0)
      + 0.55 * pool(p, vec2(-2.0, -8.0), 7.0) + 0.25 * pool(p, vec2(2.0, 1.0), 9.0)
      + 0.35 * pool(p, vec2(-15.8, -11.6), 4.0) + 0.35 * pool(p, vec2(16.2, -11.6), 4.0)
      + 0.35 * pool(p, vec2(-16.2, 10.6), 4.0) + 0.35 * pool(p, vec2(15.6, 10.8), 4.0)
      + 0.3 * pool(p, vec2(18.4, 0.4), 4.5);
    light += uFlash * 2.2 * pool(p, vec2(XF, -R0 - TW * 0.5), 4.0);

    // apron: dark asphalt with a faint grain
    vec3 col = vec3(0.035, 0.04, 0.055) * (0.85 + n * 0.3);
    // infield turf, mown in bands
    if (off < 0.0) {
      float band = 0.86 + 0.14 * step(0.5, fract(p.x * 0.28));
      col = vec3(0.05, 0.16, 0.08) * band * (0.85 + n * 0.3);
      // a lighter turf collar along the inner rail
      col = mix(col, vec3(0.07, 0.2, 0.1), smoothstep(-0.9, -0.1, off) * 0.6);
    }
    // the infield pond, right of the tote board: dark water that catches
    // the floodlights in slow ripples
    vec2 pq = (p - vec2(${POND.x.toFixed(2)}, ${POND.z.toFixed(2)})) / vec2(${POND_R[0].toFixed(2)}, ${POND_R[1].toFixed(2)});
    float pr = length(pq);
    if (pr < 1.0) {
      // long horizontal glints, the floodlights smeared by slow ripples
      float rip = sin(p.y * 11.0 + sin(p.x * 1.7 + uTime * 0.5) * 1.8 + uTime * 0.4);
      col = vec3(0.02, 0.05, 0.09) + vec3(0.35, 0.42, 0.5) * smoothstep(0.82, 1.0, rip) * 0.3 * (1.0 - pr * 0.6);
      col = mix(col, vec3(0.1, 0.16, 0.1), smoothstep(0.86, 1.0, pr));
    }
    // the racing surface: dark loam, harrowed along the lap
    if (off >= 0.0 && off <= TW) {
      float harrow = 0.92 + 0.08 * sin(off * 70.0 + n * 3.0);
      col = vec3(0.22, 0.145, 0.095) * harrow * (0.8 + n * 0.35);
      // the cushion is lighter where the field runs
      col *= 1.0 + 0.12 * sin(off / TW * 3.14159);
    }
    // LOSSES paddock: straw underfoot
    vec2 pd = abs(p - vec2(${LOSS_POS.x.toFixed(2)}, ${LOSS_POS.z.toFixed(2)})) - vec2(1.6, 2.9);
    if (max(pd.x, pd.y) < 0.0) col = vec3(0.17, 0.12, 0.06) * (0.75 + n * 0.5) * (1.0 + uLoss * 0.6);
    col *= light;

    // chalk price lanes (the 50¢ lane a touch brighter)
    float onTrack = step(0.0, off) * step(off, TW);
    float lanes = 0.0;
    ${lanes.map((o, i) => `lanes += line(off - ${o}, 0.012) * ${i === 2 ? '0.5' : '0.26'};`).join('\n    ')}
    // dashed, like a chalk line that's been run over
    vec2 qd = vec2(max(abs(p.x) - LH, 0.0), p.y);
    float along = abs(p.x) <= LH ? p.x : atan(qd.y, qd.x * sign(p.x) + 1e-4) * (R0 + 2.0);
    lanes *= 0.55 + 0.45 * step(0.35, fract(along * 0.9));
    col += vec3(0.62, 0.86, 1.0) * lanes * onTrack * 0.55;

    // the rails' white kick-boards painted on the ground edge
    col += vec3(0.9) * (line(off, 0.03) + line(off - TW, 0.03)) * 0.25;

    // the wire: a white line with a chequered edge across the home straight
    if (p.y < 0.0 && onTrack > 0.0) {
      float d = p.x - XF;
      float wire = line(d, 0.05);
      float chk = step(abs(d), 0.22) * mod(floor(off * 5.0) + floor((d + 0.22) * 4.6), 2.0) * step(0.07, abs(d));
      col = mix(col, vec3(0.95), max(wire, chk * 0.85));
      col += vec3(1.0, 0.98, 0.9) * wire * (1.4 + uFlash * 4.0);
    }

    // the night closes in past the stand and the towers
    float far = length((p - vec2(1.0, -1.0)) * vec2(0.8, 1.0));
    col *= 1.0 - smoothstep(17.0, 34.0, far);
    gl_FragColor = vec4(col, 1.0);
  }`;
}

// ── the gantry's per-stall lamps and light curtains ─────────────────────────

export interface Stall {
  /** 0 idle, >0 a runner is loading (lamp red → amber), then green on the break. */
  load: number;
  /** The break: 1 at the jump, decaying. */
  open: number;
}

export class Track {
  readonly group = new THREE.Group();
  readonly glowTex = makeGlowTexture();
  readonly stalls: Stall[] = Array.from({ length: STALLS }, () => ({ load: 0, open: 0 }));

  private groundMat: THREE.ShaderMaterial;
  private crowdMat: THREE.ShaderMaterial;
  private starMat: THREE.ShaderMaterial;
  private coneMat: THREE.ShaderMaterial;
  private solidMat = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.72, metalness: 0.08, flatShading: true });
  private glowMat = new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false });
  private railMat = new THREE.MeshStandardMaterial({ color: '#F1F5F9', roughness: 0.4, metalness: 0.1, emissive: '#CBD5E1', emissiveIntensity: 0.16 });
  private lampMesh: THREE.InstancedMesh;
  private curtainMesh: THREE.InstancedMesh;
  private winRingMat: THREE.MeshBasicMaterial;
  private winGlow: THREE.Sprite;
  private lossLamp: THREE.Sprite;
  private finishFlash: THREE.Sprite;
  private signTex: THREE.CanvasTexture;
  private winsPulse = 0;
  private lossPulse = 0;
  /** 0..1 — a photo-finish flash at the wire. */
  flash = 0;
  /** 0..1 — PAYDAY: the crowd's cameras all go off. */
  crowdFlash = 0;
  private _m = new THREE.Matrix4();
  private _c = new THREE.Color();
  private _q = new THREE.Quaternion();
  private _v = new THREE.Vector3();
  private _s = new THREE.Vector3();

  constructor() {
    const G = this.group;

    // ── sky: a few stars above the haze ─────────────────────────────────
    const N = 700;
    const pos = new Float32Array(N * 3);
    const tw = new Float32Array(N);
    for (let i = 0; i < N; i++) {
      const u = 0.12 + Math.random() * 0.88, a = Math.random() * Math.PI * 2, r = 120;
      const h = Math.sqrt(1 - u * u);
      pos[i * 3] = Math.cos(a) * h * r; pos[i * 3 + 1] = u * r * 0.6; pos[i * 3 + 2] = Math.sin(a) * h * r;
      tw[i] = Math.random();
    }
    const sg = new THREE.BufferGeometry();
    sg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    sg.setAttribute('tw', new THREE.BufferAttribute(tw, 1));
    this.starMat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
      uniforms: { uTime: { value: 0 }, uPx: { value: 1 } },
      vertexShader: /* glsl */`
        attribute float tw; uniform float uTime; uniform float uPx; varying float vA;
        void main() {
          vA = (0.35 + 0.35 * sin(uTime * (0.4 + tw * 1.6) + tw * 40.0)) * smoothstep(5.0, 40.0, position.y);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = (tw > 0.95 ? 2.6 : 1.4) * uPx;
        }`,
      fragmentShader: /* glsl */`
        varying float vA;
        void main() {
          float a = 1.0 - smoothstep(0.0, 0.5, length(gl_PointCoord - 0.5));
          gl_FragColor = vec4(vec3(0.75, 0.82, 1.0) * a * vA, a * vA);
        }`,
    });
    const stars = new THREE.Points(sg, this.starMat);
    stars.frustumCulled = false;
    G.add(stars);
    // the glow the lights throw into the night air over the stadium
    for (const [x, y, z, col, s, o] of [[-2, 8, -16, '#FDE7B0', 46, 0.17], [0, 4, 0, '#93C5FD', 60, 0.05]] as const) {
      const haze = this.sprite(col, s, o);
      haze.position.set(x, y, z);
      G.add(haze);
    }

    // ── the ground ──────────────────────────────────────────────────────
    this.groundMat = new THREE.ShaderMaterial({
      uniforms: { uTime: { value: 0 }, uFlash: { value: 0 }, uWin: { value: 0 }, uLoss: { value: 0 } },
      vertexShader: GROUND_VS,
      fragmentShader: groundFs(),
    });
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(96, 76), this.groundMat);
    ground.rotation.x = -Math.PI / 2;
    ground.position.set(1, 0, -2);
    G.add(ground);

    const solid = new Kit();
    const glow = new Kit();

    // ── rails: inner (closed) and outer (open at the chute) ─────────────
    const railGeos: THREE.BufferGeometry[] = [];
    const rail = (off: number, gap: boolean) => {
      const P = lapLength(off);
      const pts: THREE.Vector3[] = [];
      // start just past the chute so the gap is the curve's two ends
      const s0 = gap ? gateS(off) - (X_GATE - X_CHUTE) + 0.9 : 0;
      const len = gap ? P - 1.8 : P;
      const steps = 220;
      for (let i = 0; i <= steps; i++) {
        const v = new THREE.Vector3();
        trackPoint(s0 + (len * i) / steps, off, v);
        v.y = 0.46;
        pts.push(v);
      }
      if (!gap) pts.pop();
      const curve = new THREE.CatmullRomCurve3(pts, !gap);
      railGeos.push(new THREE.TubeGeometry(curve, 300, 0.045, 5, !gap));
      // posts every ~1.1 units
      const posts = Math.floor(len / 1.1);
      for (let i = 0; i <= posts; i++) {
        const v = new THREE.Vector3();
        trackPoint(s0 + (len * i) / posts, off, v);
        const g = new THREE.CylinderGeometry(0.03, 0.03, 0.46, 4);
        g.translate(v.x, 0.23, v.z);
        railGeos.push(g);
      }
    };
    rail(-0.08, false);
    rail(TW + 0.08, true);
    const railGeo = mergeGeometries(railGeos.map((g) => { const n = g.index ? g.toNonIndexed() : g; for (const k of Object.keys(n.attributes)) if (k !== 'position' && k !== 'normal') n.deleteAttribute(k); return n; }));
    railGeos.forEach((g) => g.dispose());
    if (railGeo) G.add(new THREE.Mesh(railGeo, this.railMat));

    // ── the grandstand, behind the home straight ────────────────────────
    const zS = -(R0 + TW) - 1.15;
    const x0 = -13, x1 = 8.5, gw = x1 - x0, gx = (x0 + x1) / 2;
    const ROWS = 8;
    for (let i = 0; i < ROWS; i++) {
      const h = (i + 1) * 0.3;
      solid.add(BOX, i % 2 ? '#262C44' : '#2F3654', gx, h / 2, zS - 0.25 - i * 0.5, 0, 0, 0, gw, h, 0.5);
    }
    // a warm strip along every tier's nose: the stand reads as rows of seats
    for (let i = 0; i < ROWS; i++) glow.add(BOX, i % 2 ? '#7C5A2A' : '#8A6630', gx, (i + 1) * 0.3 + 0.005, zS - i * 0.5 - 0.02, 0, 0, 0, gw, 0.012, 0.03);
    const back = zS - ROWS * 0.5;
    solid.add(BOX, '#3A1420', gx, 2.3, back - 0.15, 0, 0, 0, gw + 0.4, 4.6, 0.3);
    // the roof: a cantilevered slab with a lit underside
    solid.add(BOX, '#141824', gx, 4.55, zS - 1.8, 0.08, 0, 0, gw + 1.2, 0.18, 4.8);
    solid.add(BOX, '#0E1220', gx, 4.85, zS + 0.5, 0, 0, 0, gw + 1.2, 0.55, 0.12);
    for (let x = x0; x <= x1 + 0.01; x += gw / 6) solid.add(CYL, '#3B4256', x, 2.3, back + 0.2, 0, 0, 0, 0.09, 4.6, 0.09);
    glow.add(BOX, '#FFE7B8', gx, 4.38, zS + 0.38, 0, 0, 0, gw + 0.8, 0.05, 0.1);
    // gold trim along the fascia's top edge: the stand's outline from above
    glow.add(BOX, '#F5C451', gx, 5.14, zS + 0.5, 0, 0, 0, gw + 1.2, 0.04, 0.14);
    glow.add(BOX, '#B8892F', gx, 4.66, zS - 4.2, 0, 0, 0, gw + 1.2, 0.03, 0.08);
    for (let i = 0; i < 9; i++) glow.add(BOX, '#FFF4DC', x0 + 1 + (i * (gw - 2)) / 8, 4.4, zS - 0.6, 0, 0, 0, 0.7, 0.04, 0.5);
    // aisles of light between the sections
    for (let i = 1; i < 6; i++) glow.add(BOX, '#FBBF24', x0 + (i * gw) / 6, 0.06 + ROWS * 0.15, zS - ROWS * 0.25, -Math.atan2(0.3, 0.5), 0, 0, 0.05, 0.03, ROWS * 0.58);
    // the stand's sign, centred over the wire
    this.signTex = signTexture();
    const sign = new THREE.Mesh(
      new THREE.PlaneGeometry(9.6, 1.2),
      new THREE.MeshBasicMaterial({ map: this.signTex, toneMapped: false, transparent: true }),
    );
    sign.position.set(X_FINISH + 0.4, 5.55, zS + 0.44);
    G.add(sign);
    solid.add(BOX, '#0B0F1A', X_FINISH + 0.4, 5.55, zS + 0.38, 0, 0, 0, 9.9, 1.35, 0.08);

    // the crowd: one Points on the tiers, with cameras that go off
    const seats: number[] = [];
    const seatCol: number[] = [];
    const seed: number[] = [];
    const pal = ['#E2E8F0', '#94A3B8', '#F87171', '#60A5FA', '#FBBF24', '#34D399', '#C084FC', '#F472B6', '#FDE68A'];
    const c = new THREE.Color();
    for (let i = 0; i < ROWS; i++) {
      for (let x = x0 + 0.2; x < x1 - 0.2; x += 0.13) {
        if (Math.random() < 0.18) continue;
        seats.push(x + (Math.random() - 0.5) * 0.08, (i + 1) * 0.3 + 0.1 + Math.random() * 0.04, zS - 0.3 - i * 0.5 + (Math.random() - 0.5) * 0.1);
        c.set(pal[Math.floor(Math.random() * pal.length)]).multiplyScalar(0.35 + Math.random() * 0.25);
        seatCol.push(c.r, c.g, c.b);
        seed.push(Math.random());
      }
    }
    const cg = new THREE.BufferGeometry();
    cg.setAttribute('position', new THREE.Float32BufferAttribute(seats, 3));
    cg.setAttribute('color', new THREE.Float32BufferAttribute(seatCol, 3));
    cg.setAttribute('seed', new THREE.Float32BufferAttribute(seed, 1));
    this.crowdMat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: { uTime: { value: 0 }, uPx: { value: 1 }, uFlash: { value: 0 } },
      vertexShader: /* glsl */`
        attribute vec3 color; attribute float seed; uniform float uTime; uniform float uPx; uniform float uFlash;
        varying vec3 vC; varying float vF;
        void main() {
          // a camera flash: rare at rest, a storm on PAYDAY
          float rate = 0.05 + uFlash * 1.6;
          float ph = fract(uTime * rate * (0.6 + seed) + seed * 17.0);
          vF = (1.0 - smoothstep(0.0, 0.035, ph)) * step(0.82 - uFlash * 0.5, fract(seed * 91.3 + floor(uTime * rate * (0.6 + seed) + seed * 17.0) * 0.37));
          vC = color;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_Position = projectionMatrix * mv;
          gl_PointSize = (2.2 + vF * 3.0) * uPx * (26.0 / max(1.0, -mv.z));
        }`,
      fragmentShader: /* glsl */`
        varying vec3 vC; varying float vF;
        void main() {
          float d = length(gl_PointCoord - 0.5);
          if (d > 0.5) discard;
          vec3 c = mix(vC, vec3(3.0), vF);
          gl_FragColor = vec4(c, 1.0);
        }`,
    });
    const crowd = new THREE.Points(cg, this.crowdMat);
    G.add(crowd);

    // ── floodlight towers at the four corners ───────────────────────────
    const coneGeos: THREE.BufferGeometry[] = [];
    const towers: [number, number, number, number][] = [[-15.8, -11.6, -9.5, -3.5], [16.2, -11.6, 9.5, -3.5], [-16.2, 10.6, -9.5, 4.5], [15.6, 10.8, 9.5, 4.5]];
    for (const [tx, tz, ax, az] of towers) {
      const H = 11;
      solid.add(CYL4, '#2B3140', tx, H / 2, tz, 0, Math.PI / 4, 0, 0.22, H, 0.22);
      solid.add(BOX, '#2B3140', tx, 0.15, tz, 0, 0, 0, 1.0, 0.3, 1.0);
      // the bank faces its pool, tilted down
      const yaw = Math.atan2(ax - tx, az - tz);
      const top = new THREE.Vector3(tx, H + 0.6, tz);
      const aim = new THREE.Vector3(ax, 0, az);
      solid.add(BOX, '#1A1F2B', tx, H + 0.6, tz, 0, yaw, 0, 2.6, 1.5, 0.35);
      for (let r = 0; r < 3; r++) for (let k = 0; k < 4; k++) {
        const lx = (k - 1.5) * 0.6, ly = (r - 1) * 0.42;
        const v = new THREE.Vector3(lx, ly, 0.2).applyAxisAngle(new THREE.Vector3(0, 1, 0), yaw).add(top);
        glow.add(BOX, '#FFF8E7', v.x, v.y, v.z, 0, yaw, 0, 0.46, 0.3, 0.06);
      }
      // the beam: an open cone from the bank to its pool
      const len = top.distanceTo(aim);
      const cone = new THREE.ConeGeometry(5.2, len, 20, 1, true);
      cone.translate(0, -len / 2, 0); // apex at the origin, opening down −y
      const n = cone.attributes.position.count;
      const tAttr = new Float32Array(n);
      for (let i = 0; i < n; i++) tAttr[i] = -cone.attributes.position.getY(i) / len;
      cone.setAttribute('aT', new THREE.BufferAttribute(tAttr, 1));
      const dir = aim.clone().sub(top).normalize();
      this._q.setFromUnitVectors(new THREE.Vector3(0, -1, 0), dir);
      cone.applyMatrix4(new THREE.Matrix4().compose(top, this._q, new THREE.Vector3(1, 1, 1)));
      cone.deleteAttribute('uv');
      coneGeos.push(cone);
    }
    this.coneMat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
      vertexShader: /* glsl */`
        attribute float aT; varying float vT; varying vec3 vN; varying vec3 vV;
        void main() {
          vT = aT;
          vec4 w = modelMatrix * vec4(position, 1.0);
          vN = normalize(mat3(modelMatrix) * normal);
          vV = normalize(cameraPosition - w.xyz);
          gl_Position = projectionMatrix * viewMatrix * w;
        }`,
      fragmentShader: /* glsl */`
        varying float vT; varying vec3 vN; varying vec3 vV;
        void main() {
          float edge = pow(abs(dot(normalize(vN), normalize(vV))), 1.6);
          // clamp before pow: pow(negative) is NaN, and one NaN pixel blacks the
          // whole frame once the bloom blurs it
          float a = 0.1 * pow(clamp(1.0 - vT, 0.0, 1.0), 1.3) * edge * smoothstep(0.0, 0.06, vT);
          gl_FragColor = vec4(vec3(1.0, 0.95, 0.82) * a, a);
        }`,
    });
    const coneGeo = mergeGeometries(coneGeos);
    coneGeos.forEach((g) => g.dispose());
    if (coneGeo) {
      const cones = new THREE.Mesh(coneGeo, this.coneMat);
      cones.renderOrder = 5;
      G.add(cones);
    }

    // ── the starting gantry across the backstretch ──────────────────────
    const gz0 = R0 - 0.45, gz1 = R0 + TW + 0.45, gzc = (gz0 + gz1) / 2, gl = gz1 - gz0;
    const GH = 2.25;
    // a slim gantry, not a wall: two white posts and a light truss the field
    // gallops under on every lap
    for (const z of [gz0 - 0.1, gz1 + 0.1]) {
      solid.add(BOX, '#E5E7EB', X_GATE, GH / 2, z, 0, 0, 0, 0.28, GH, 0.28);
      solid.add(BOX, '#15803D', X_GATE, 0.9, z, 0, 0, 0, 0.3, 0.12, 0.3);
      solid.add(BOX, '#334155', X_GATE, 0.04, z, 0, 0, 0, 0.6, 0.08, 0.6);
    }
    solid.add(BOX, '#F1F5F9', X_GATE, GH + 0.08, gzc, 0, 0, 0, 0.42, 0.16, gl + 0.5);
    solid.add(BOX, '#15803D', X_GATE, GH + 0.165, gzc, 0, 0, 0, 0.3, 0.01, gl + 0.5);
    solid.add(BOX, '#166534', X_GATE, GH - 0.06, gzc, 0, 0, 0, 0.44, 0.08, gl + 0.5);
    // stall partitions hang from the truss, clear of a passing jockey's cap
    for (let i = 0; i <= STALLS; i++) {
      const z = R0 + (i * TW) / STALLS;
      solid.add(BOX, '#CBD5E1', X_GATE, GH - 0.3, z, 0, 0, 0, 0.36, 0.42, 0.025);
    }
    // number plates in saddle-cloth colours, facing the camera side
    for (let i = 0; i < STALLS; i++) {
      const z = R0 + ((i + 0.5) * TW) / STALLS;
      glow.add(BOX, CLOTH[i], X_GATE + 0.22, GH + 0.08, z, 0, 0, 0, 0.02, 0.12, 0.24);
    }
    // stall lamps: one instanced mesh, coloured per stall each frame
    this.lampMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(0.16, 0.06, 0.26), new THREE.MeshBasicMaterial({ toneMapped: false }), STALLS);
    // light curtains: the stall "doors", up while a runner is loading
    this.curtainMesh = new THREE.InstancedMesh(
      new THREE.PlaneGeometry(1, 1),
      new THREE.MeshBasicMaterial({ color: '#86EFAC', transparent: true, opacity: 0.32, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }),
      STALLS,
    );
    for (let i = 0; i < STALLS; i++) {
      const z = R0 + ((i + 0.5) * TW) / STALLS;
      this._m.makeTranslation(X_GATE, GH + 0.2, z);
      this.lampMesh.setMatrixAt(i, this._m);
      this.lampMesh.setColorAt(i, this._c.set('#334155'));
    }
    this.curtainMesh.count = 0;
    G.add(this.lampMesh, this.curtainMesh);

    // ── the wire: finish pole, mirror and the photo-finish booth ────────
    const zIn = -(R0 - 0.3), zOut = -(R0 + TW + 0.7);
    solid.add(CYL, '#F8FAFC', X_FINISH, 1.3, zIn, 0, 0, 0, 0.06, 2.6, 0.06);
    solid.add(CYL, '#E11D48', X_FINISH, 1.0, zIn, 0, 0, 0, 0.065, 0.3, 0.065);
    glow.add(CYL, '#9CA3AF', X_FINISH, 2.66, zIn, Math.PI / 2, 0, 0, 0.2, 0.03, 0.2);
    solid.add(CYL, '#E5E7EB', X_FINISH, 2.66, zIn - 0.02, Math.PI / 2, 0, 0, 0.23, 0.02, 0.23);
    solid.add(CYL, '#CBD5E1', X_FINISH, 1.5, zOut, 0, 0, 0, 0.07, 3.0, 0.07);
    solid.add(BOX, '#0F172A', X_FINISH, 3.25, zOut, 0, 0, 0, 0.8, 0.6, 0.7);
    glow.add(CYL, '#93C5FD', X_FINISH, 3.25, zOut + 0.37, Math.PI / 2, 0, 0, 0.12, 0.04, 0.12);
    glow.add(BOX, '#FDE68A', X_FINISH, 3.58, zOut, 0, 0, 0, 0.82, 0.05, 0.72);
    this.finishFlash = this.sprite('#FFFFFF', 7, 0);
    this.finishFlash.position.set(X_FINISH, 2.2, -(R0 + TW / 2));
    G.add(this.finishFlash);

    // ── the tote board, infield ─────────────────────────────────────────
    const tx = TOTE_POS.x, tz = TOTE_POS.z;
    solid.add(BOX, '#0B1020', tx, TOTE_Y + TOTE_H / 2, tz - 0.12, 0, 0, 0, TOTE_W + 0.36, TOTE_H + 0.36, 0.2);
    for (const lx of [-TOTE_W * 0.32, TOTE_W * 0.32]) solid.add(BOX, '#1E293B', tx + lx, TOTE_Y / 2, tz - 0.12, 0, 0, 0, 0.22, TOTE_Y, 0.22);
    // the bulb rows along its top and bottom
    for (let i = 0; i < 26; i++) {
      const x = tx - TOTE_W / 2 + (i + 0.5) * (TOTE_W / 26);
      glow.add(BOX, '#FCD34D', x, TOTE_Y + TOTE_H + 0.1, tz, 0, 0, 0, 0.07, 0.07, 0.04);
      glow.add(BOX, '#FCD34D', x, TOTE_Y - 0.1, tz, 0, 0, 0, 0.07, 0.07, 0.04);
    }
    // a low hedge around its foot
    solid.add(BOX, '#14351F', tx, 0.18, tz + 0.35, 0, 0, 0, TOTE_W + 0.8, 0.36, 0.5);

    // topiary round the pond and the infield's far corners
    const cone = new THREE.ConeGeometry(1, 1, 7);
    const trees: [number, number, number][] = [
      [POND.x - 1.6, POND.z - 2.0, 1.0], [POND.x + 1.0, POND.z - 2.2, 0.8], [POND.x + 2.6, POND.z + 0.4, 0.9],
      [POND.x - 0.2, POND.z + 2.2, 0.7], [-3.6, 2.6, 0.75], [-10.4, 2.0, 0.85], [-10.9, -0.9, 0.8],
    ];
    for (const [x, z, h] of trees) {
      solid.add(CYL, '#3B2A1E', x, 0.18 * h, z, 0, 0, 0, 0.07, 0.36 * h, 0.07);
      solid.add(cone, '#16452A', x, 0.36 * h + 0.55 * h, z, 0, 0, 0, 0.42 * h, 1.1 * h, 0.42 * h);
      solid.add(cone, '#1D5A36', x, 0.36 * h + 0.95 * h, z, 0, 0, 0, 0.3 * h, 0.8 * h, 0.3 * h);
    }
    cone.dispose();

    // ── the winner's circle ─────────────────────────────────────────────
    const wx = WINS_POS.x, wz = WINS_POS.z;
    solid.add(CYL, '#E7DCC6', wx, 0.06, wz, 0, 0, 0, 1.7, 0.12, 1.7);
    const hedge = new THREE.TorusGeometry(1.85, 0.16, 6, 28);
    hedge.rotateX(Math.PI / 2);
    solid.add(hedge, '#1C4A2A', wx, 0.18, wz);
    hedge.dispose();
    this.winRingMat = new THREE.MeshBasicMaterial({ color: '#FCD34D', transparent: true, opacity: 0.7, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false });
    const winRing = new THREE.Mesh(new THREE.TorusGeometry(1.62, 0.035, 6, 64), this.winRingMat);
    winRing.rotation.x = Math.PI / 2;
    winRing.position.set(wx, 0.14, wz);
    this.winGlow = this.sprite('#FCD34D', 5, 0.18);
    this.winGlow.position.set(wx, 0.5, wz);
    G.add(winRing, this.winGlow);
    // garland posts
    for (let i = 0; i < 8; i++) {
      const a = (i / 8) * Math.PI * 2;
      solid.add(CYL, '#F8FAFC', wx + Math.cos(a) * 1.85, 0.45, wz + Math.sin(a) * 1.85, 0, 0, 0, 0.04, 0.9, 0.04);
      glow.add(CYL, '#FDE68A', wx + Math.cos(a) * 1.85, 0.93, wz + Math.sin(a) * 1.85, 0, 0, 0, 0.07, 0.07, 0.07);
    }

    // ── the LOSSES paddock and its barn ─────────────────────────────────
    const px = LOSS_POS.x, pz = LOSS_POS.z;
    const fence = (x: number, z: number, len: number, along: 'x' | 'z') => {
      for (const h of [0.28, 0.55]) {
        if (along === 'x') solid.add(BOX, '#6B4F3A', x, h, z, 0, 0, 0, len, 0.06, 0.06);
        else solid.add(BOX, '#6B4F3A', x, h, z, 0, 0, 0, 0.06, 0.06, len);
      }
      const n2 = Math.round(len / 0.9);
      for (let i = 0; i <= n2; i++) {
        const f = i / n2 - 0.5;
        solid.add(BOX, '#5A4130', along === 'x' ? x + f * len : x, 0.32, along === 'z' ? z + f * len : z, 0, 0, 0, 0.08, 0.64, 0.08);
      }
    };
    fence(px, pz - 2.9, 3.2, 'x');
    fence(px, pz + 2.9, 3.2, 'x');
    fence(px - 1.6, pz + 1.3, 3.2, 'z');
    fence(px + 1.6, pz, 5.8, 'z');
    // the barn, at the back of the paddock
    solid.add(BOX, '#5B1D1D', px + 0.3, 0.75, pz - 2.0, 0, 0, 0, 2.2, 1.5, 1.4);
    const roof = new THREE.CylinderGeometry(1, 1, 1, 3);
    solid.add(roof, '#2B1010', px + 0.3, 1.82, pz - 2.0, 0, 0, Math.PI / 2, 0.85, 2.4, 0.9);
    roof.dispose();
    solid.add(BOX, '#1A0A0A', px + 0.3, 0.55, pz - 1.29, 0, 0, 0, 0.8, 1.1, 0.02);
    this.lossLamp = this.sprite('#EF4444', 3.2, 0.25);
    this.lossLamp.position.set(px + 0.3, 1.75, pz - 1.2);
    G.add(this.lossLamp);
    glow.add(BOX, '#F87171', px + 0.3, 1.6, pz - 1.28, 0, 0, 0, 0.25, 0.12, 0.04);

    const sg2 = solid.build();
    if (sg2) G.add(new THREE.Mesh(sg2, this.solidMat));
    const gg = glow.build();
    if (gg) G.add(new THREE.Mesh(gg, this.glowMat));
  }

  sprite(color: string, size: number, opacity: number): THREE.Sprite {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.glowTex, color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false }));
    s.scale.setScalar(size);
    return s;
  }

  /** Which stall a lane loads into. */
  stallOf(off: number): number {
    return THREE.MathUtils.clamp(Math.floor((off / TW) * STALLS), 0, STALLS - 1);
  }

  pulseWins(): void { this.winsPulse = 1; }
  pulseLoss(): void { this.lossPulse = 1; }

  setPixelScale(s: number): void {
    this.starMat.uniforms.uPx.value = s;
    this.crowdMat.uniforms.uPx.value = s;
  }

  update(dt: number, t: number): void {
    this.starMat.uniforms.uTime.value = t;
    this.crowdMat.uniforms.uTime.value = t;
    this.flash = Math.max(0, this.flash - dt * 2.4);
    this.crowdFlash = Math.max(0, this.crowdFlash - dt * 0.4);
    this.winsPulse = Math.max(0, this.winsPulse - dt * 1.2);
    this.lossPulse = Math.max(0, this.lossPulse - dt * 1.2);
    // a win also sets the stand's cameras off for a moment
    this.crowdMat.uniforms.uFlash.value = Math.max(this.crowdFlash, this.flash * 0.6);
    this.groundMat.uniforms.uTime.value = t;
    this.groundMat.uniforms.uFlash.value = this.flash;
    this.groundMat.uniforms.uLoss.value = this.lossPulse;
    (this.finishFlash.material as THREE.SpriteMaterial).opacity = this.flash * 0.9;
    this.finishFlash.scale.setScalar(6 + this.flash * 5);
    this.winRingMat.opacity = 0.55 + this.winsPulse * 0.45 + Math.sin(t * 2) * 0.05;
    (this.winGlow.material as THREE.SpriteMaterial).opacity = 0.16 + this.winsPulse * 0.5;
    this.winGlow.scale.setScalar(5 + this.winsPulse * 3);
    (this.lossLamp.material as THREE.SpriteMaterial).opacity = 0.22 + this.lossPulse * 0.6 + Math.sin(t * 1.3) * 0.04;

    // stall lamps and curtains
    let nc = 0;
    for (let i = 0; i < STALLS; i++) {
      const st = this.stalls[i];
      st.open = Math.max(0, st.open - dt * 0.8);
      if (st.open > 0) this._c.set('#22C55E').multiplyScalar(1 + st.open * 2.5);
      else if (st.load > 0) this._c.set(st.load > 0.6 ? '#F59E0B' : '#EF4444').multiplyScalar(1.6);
      else this._c.set('#334155');
      this.lampMesh.setColorAt(i, this._c);
      if (st.load > 0) {
        const z = R0 + ((i + 0.5) * TW) / STALLS;
        this._q.setFromAxisAngle(this._v.set(0, 1, 0), Math.PI / 2);
        this._m.compose(this._v.set(X_GATE + 0.2, 0.6, z), this._q, this._s.set(TW / STALLS - 0.06, 1.15, 1));
        this.curtainMesh.setMatrixAt(nc++, this._m);
      }
    }
    this.curtainMesh.count = nc;
    this.curtainMesh.instanceMatrix.needsUpdate = true;
    if (this.lampMesh.instanceColor) this.lampMesh.instanceColor.needsUpdate = true;
  }

  dispose(): void {
    this.glowTex.dispose();
    this.signTex.dispose();
    this.solidMat.dispose();
    this.glowMat.dispose();
    this.railMat.dispose();
  }
}

/** The stand's sign: drawn once. */
function signTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = 1024;
  c.height = 128;
  const g = c.getContext('2d')!;
  g.clearRect(0, 0, c.width, c.height);
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.font = `700 64px ${F_SANS}`;
  const grad = g.createLinearGradient(200, 0, 824, 0);
  grad.addColorStop(0, '#FDE68A');
  grad.addColorStop(0.5, '#FFFFFF');
  grad.addColorStop(1, '#FDE68A');
  g.fillStyle = grad;
  g.shadowColor = 'rgba(253,230,138,0.8)';
  g.shadowBlur = 18;
  g.fillText('KRYPT POLYBOT  DOWNS', 512, 58);
  g.shadowBlur = 0;
  g.font = `600 20px ${F_MONO}`;
  g.fillStyle = 'rgba(226,232,240,0.75)';
  g.fillText('NIGHT MEET · EVERY TRADE RACES ITS PRICE', 512, 108);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}
