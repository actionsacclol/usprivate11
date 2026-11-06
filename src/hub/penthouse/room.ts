import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import { neonText } from '../engine/casinoTextures';

// The penthouse: a top-floor corner unit with two walls of glass (behind the
// desks and down the left), a slatted walnut wall on the right that carries
// the video wall, and a night city below — every building's windows lit by
// a world-space pattern in its shader, so a tower and a low block share one
// draw. Inside: a polished floor, linear ceiling lights, a lounge by the
// window and a glass-door fridge of energy cans by the bar.

export const ROOM = { x0: -13, x1: 13, z0: -8, z1: 6.5, h: 4.4 } as const;
/** The video wall's screens: 4 across, 2 high, on the right wall, facing -x. */
export const WALL = { x: ROOM.x1 - 0.06, cols: 4, rows: 2, w: 2.3, h: 1.29, gap: 0.08, z0: -6.9, y0: 1.2 } as const;
/** The elevator in the front wall: the way out on foot. */
export const ELEVATOR = { x0: 1.3, x1: 2.9, h: 2.5 } as const;

function canvasTex(w: number, h: number, draw: (g: CanvasRenderingContext2D) => void, repeat?: [number, number]): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d')!);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  if (repeat) {
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.repeat.set(repeat[0], repeat[1]);
  }
  return t;
}

/** Seeded, so the city is the same city every time it opens. */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

export class Room {
  readonly group = new THREE.Group();
  private disposables: { dispose(): void }[] = [];
  private beacons: THREE.MeshBasicMaterial;
  private winUniform = { value: 1.0 };
  private fridgeGlow: THREE.MeshBasicMaterial;
  private haze: THREE.CanvasTexture[] = [];
  private t = 0;

  constructor() {
    this.sky();
    this.city();
    this.shell();
    this.lounge();
    this.bar();
    this.elevator();
    this.beacons = this.beaconMat;
    this.fridgeGlow = this.fridgeMat;
  }

  private keep<T extends { dispose(): void }>(x: T): T {
    this.disposables.push(x);
    return x;
  }

  private mesh(geo: THREE.BufferGeometry, mat: THREE.Material, x = 0, y = 0, z = 0, parent: THREE.Object3D = this.group): THREE.Mesh {
    const m = new THREE.Mesh(geo, mat);
    m.position.set(x, y, z);
    parent.add(m);
    return m;
  }

  // ── outside ──────────────────────────────────────────────────────────────

  private sky(): void {
    const geo = this.keep(new THREE.SphereGeometry(600, 48, 24));
    const mat = this.keep(new THREE.ShaderMaterial({
      side: THREE.BackSide, depthWrite: false, fog: false,
      uniforms: {},
      vertexShader: /* glsl */`
        varying vec3 vDir;
        void main() {
          vDir = normalize(position);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }`,
      fragmentShader: /* glsl */`
        varying vec3 vDir;
        void main() {
          float y = vDir.y;
          vec3 zen = vec3(0.007, 0.006, 0.024);
          vec3 mid = vec3(0.03, 0.018, 0.075);
          vec3 hor = vec3(0.11, 0.05, 0.17);
          vec3 c = mix(mid, zen, smoothstep(0.05, 0.6, y));
          c = mix(hor, c, smoothstep(-0.12, 0.16, y));
          // the city's own glow, low on the horizon
          c += vec3(0.20, 0.07, 0.26) * exp(-abs(y + 0.06) * 26.0) * 0.5;
          gl_FragColor = vec4(c, 1.0);
        }`,
    }));
    const dome = new THREE.Mesh(geo, mat);
    dome.renderOrder = -10;
    this.group.add(dome);

    // stars, and a moon over the back-left
    const r = rng(7);
    const n = 700;
    const pos = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const a = r() * Math.PI * 2, y = 0.08 + r() * 0.92;
      const s = Math.sqrt(1 - y * y);
      pos[i * 3] = Math.cos(a) * s * 560;
      pos[i * 3 + 1] = y * 560;
      pos[i * 3 + 2] = Math.sin(a) * s * 560;
    }
    const sg = this.keep(new THREE.BufferGeometry());
    sg.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const sm = this.keep(new THREE.PointsMaterial({ color: '#cfd8ff', size: 1.3, sizeAttenuation: false, fog: false, transparent: true, opacity: 0.8 }));
    this.group.add(new THREE.Points(sg, sm));
    const moonTex = this.keep(canvasTex(256, 256, (g) => {
      const grd = g.createRadialGradient(128, 128, 20, 128, 128, 128);
      grd.addColorStop(0, 'rgba(255,248,230,1)');
      grd.addColorStop(0.32, 'rgba(255,240,215,1)');
      grd.addColorStop(0.36, 'rgba(200,190,255,0.35)');
      grd.addColorStop(1, 'rgba(120,110,200,0)');
      g.fillStyle = grd;
      g.fillRect(0, 0, 256, 256);
    }));
    const moon = this.mesh(this.keep(new THREE.PlaneGeometry(60, 60)), this.keep(new THREE.MeshBasicMaterial({ map: moonTex, transparent: true, depthWrite: false, fog: false, toneMapped: false, color: new THREE.Color(1.6, 1.55, 1.45) })), -230, 190, -470);
    moon.lookAt(0, 0, 0);
  }

  private beaconMat!: THREE.MeshBasicMaterial;

  private city(): void {
    const r = rng(2026);
    const N = 340;
    const geo = this.keep(new THREE.BoxGeometry(1, 1, 1));
    geo.translate(0, 0.5, 0);
    const mat = this.keep(new THREE.MeshStandardMaterial({ color: '#0a0d15', roughness: 0.85, metalness: 0.15, emissive: '#ffffff' }));
    const winU = this.winUniform;
    mat.onBeforeCompile = (sh) => {
      sh.uniforms.uWin = winU;
      sh.vertexShader = sh.vertexShader
        .replace('#include <common>', '#include <common>\nvarying vec3 vWP;\nvarying vec3 vWN;')
        .replace('#include <project_vertex>', `#include <project_vertex>
          vec4 wp = vec4(transformed, 1.0);
          vec3 wn = objectNormal;
          #ifdef USE_INSTANCING
            wp = instanceMatrix * wp;
            wn = mat3(instanceMatrix) * wn;
          #endif
          vWP = (modelMatrix * wp).xyz;
          vWN = normalize(mat3(modelMatrix) * wn);`);
      sh.fragmentShader = sh.fragmentShader
        .replace('#include <common>', `#include <common>
          uniform float uWin;
          varying vec3 vWP;
          varying vec3 vWN;
          float h21(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }`)
        .replace('#include <emissivemap_fragment>', `
          {
            vec3 n = normalize(vWN);
            float side = 1.0 - step(0.6, abs(n.y));
            bool xFace = abs(n.x) > 0.5;
            vec2 cell = xFace ? vec2(vWP.z, vWP.y) : vec2(vWP.x, vWP.y);
            float faceSeed = floor((xFace ? vWP.x : vWP.z) * 0.37);
            vec2 g = cell / vec2(1.1, 1.5);
            vec2 id = floor(g);
            vec2 f = fract(g);
            float win = step(0.16, f.x) * step(f.x, 0.84) * step(0.22, f.y) * step(f.y, 0.78);
            float h = h21(id + faceSeed * 17.0);
            float lit = step(0.76, h);
            vec3 warm = mix(vec3(1.0, 0.72, 0.42), vec3(0.62, 0.78, 1.0), step(0.62, fract(h * 9.0)));
            float bright = 0.1 + fract(h * 31.0) * 0.26;
            totalEmissiveRadiance = warm * win * lit * side * bright * uWin;
          }`);
    };
    const inst = new THREE.InstancedMesh(geo, mat, N);
    const m4 = new THREE.Matrix4();
    const q = new THREE.Quaternion();
    const tall: THREE.Vector3[] = [];
    let k = 0;
    for (let i = 0; i < N * 3 && k < N; i++) {
      const a = r() * Math.PI * 2;
      const d = 80 + Math.pow(r(), 0.65) * 380;
      const x = Math.cos(a) * d, z = Math.sin(a) * d;
      const w = 8 + r() * 18, dd = 8 + r() * 18;
      const towers = d > 170 && r() < 0.12;
      const top = towers ? 4 + r() * 40 : -120 + r() * 108;
      const base = -140;
      m4.compose(new THREE.Vector3(x, base, z), q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), (r() - 0.5) * 0.3), new THREE.Vector3(w, top - base, dd));
      inst.setMatrixAt(k, m4);
      inst.setColorAt(k, new THREE.Color().setHSL(0.62 + r() * 0.08, 0.2, 0.05 + r() * 0.05));
      if (top > 0) tall.push(new THREE.Vector3(x, top + 1.2, z));
      k++;
    }
    inst.count = k;
    inst.instanceMatrix.needsUpdate = true;
    if (inst.instanceColor) inst.instanceColor.needsUpdate = true;
    this.group.add(inst);

    // aircraft warning lights on everything taller than us
    this.beaconMat = this.keep(new THREE.MeshBasicMaterial({ color: new THREE.Color(2, 0.18, 0.15), toneMapped: false }));
    const bg = this.keep(new THREE.SphereGeometry(0.7, 8, 6));
    for (const p of tall) this.mesh(bg, this.beaconMat, p.x, p.y, p.z);

    // the streets, far below: a grid of sodium light
    const street = this.keep(canvasTex(512, 512, (g) => {
      g.fillStyle = '#04050a';
      g.fillRect(0, 0, 512, 512);
      for (let i = 0; i < 512; i += 64) {
        g.fillStyle = 'rgba(255,150,70,0.55)';
        g.fillRect(i, 0, 3, 512);
        g.fillRect(0, i, 512, 3);
        for (let j = 0; j < 512; j += 16) {
          g.fillStyle = 'rgba(255,210,150,0.9)';
          g.fillRect(i - 1, j, 5, 3);
          g.fillRect(j, i - 1, 3, 5);
        }
      }
    }, [26, 26]));
    const ground = this.mesh(this.keep(new THREE.PlaneGeometry(1400, 1400)), this.keep(new THREE.MeshBasicMaterial({ map: street, color: new THREE.Color(0.7, 0.55, 0.85), toneMapped: false })), 0, -139, 0);
    ground.rotation.x = -Math.PI / 2;

    // purple haze lying over the city below us: two slow, soft layers
    const r2 = rng(99);
    const hazeGeo = this.keep(new THREE.PlaneGeometry(1300, 1300));
    for (const [y, color, opacity, rep] of [[-34, '#3a1b66', 0.42, 3], [-78, '#5b2a8f', 0.5, 2]] as const) {
      const tex = this.keep(canvasTex(512, 512, (g) => {
        g.clearRect(0, 0, 512, 512);
        for (let i = 0; i < 70; i++) {
          const x = r2() * 512, yy = r2() * 512, rad = 40 + r2() * 120;
          const grd = g.createRadialGradient(x, yy, 0, x, yy, rad);
          grd.addColorStop(0, `rgba(255,255,255,${0.10 + r2() * 0.12})`);
          grd.addColorStop(1, 'rgba(255,255,255,0)');
          g.fillStyle = grd;
          for (const ox of [-512, 0, 512]) for (const oy of [-512, 0, 512]) {
            g.save(); g.translate(ox, oy); g.fillRect(x - rad, yy - rad, rad * 2, rad * 2); g.restore();
          }
        }
      }, [rep, rep]));
      this.haze.push(tex);
      const m = this.mesh(hazeGeo, this.keep(new THREE.MeshBasicMaterial({
        map: tex, color, transparent: true, opacity, depthWrite: false, side: THREE.DoubleSide,
      })), 0, y, 0);
      m.rotation.x = -Math.PI / 2;
      m.renderOrder = -1;
    }
  }

  // ── the unit ─────────────────────────────────────────────────────────────

  private shell(): void {
    const { x0, x1, z0, z1, h } = ROOM;
    const W = x1 - x0, D = z1 - z0;
    const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
    // floor: polished dark stone in big slabs
    const floorTex = this.keep(canvasTex(1024, 1024, (g) => {
      g.fillStyle = '#1a1c21';
      g.fillRect(0, 0, 1024, 1024);
      const r = rng(11);
      for (let i = 0; i < 5000; i++) {
        g.fillStyle = `rgba(255,255,255,${r() * 0.025})`;
        g.fillRect(r() * 1024, r() * 1024, 2 + r() * 6, 1 + r() * 3);
      }
      g.fillStyle = 'rgba(0,0,0,0.55)';
      g.fillRect(0, 0, 1024, 3);
      g.fillRect(0, 0, 3, 1024);
    }, [W / 3, D / 3]));
    const floor = this.mesh(this.keep(new THREE.PlaneGeometry(W, D)), this.keep(new THREE.MeshStandardMaterial({ map: floorTex, roughness: 0.62, metalness: 0.0 })), cx, 0, cz);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    // ceiling, with its linear light slots
    const ceil = this.mesh(this.keep(new THREE.PlaneGeometry(W, D)), this.keep(new THREE.MeshStandardMaterial({ color: '#0d0f13', roughness: 0.95 })), cx, h, cz);
    ceil.rotation.x = Math.PI / 2;
    const slot = this.keep(new THREE.MeshBasicMaterial({ color: new THREE.Color(0.82, 0.78, 0.74), toneMapped: false }));
    const box = this.keep(new THREE.BoxGeometry(1, 1, 1));
    for (const z of [-5.6, -3, -0.4, 2.2]) {
      const s = this.mesh(box, slot, -1, h - 0.01, z);
      s.scale.set(18, 0.02, 0.06);
    }
    // cove light along the glass
    const cove = this.keep(new THREE.MeshBasicMaterial({ color: new THREE.Color(0.62, 0.32, 1.15), toneMapped: false }));
    const cb = this.mesh(box, cove, cx, h - 0.04, z0 + 0.12);
    cb.scale.set(W, 0.02, 0.02);
    const cl = this.mesh(box, cove, x0 + 0.12, h - 0.04, cz);
    cl.scale.set(0.02, 0.02, D);

    // glass: back and left, with slim black mullions
    const glass = this.keep(new THREE.MeshBasicMaterial({ color: '#a8c0e8', transparent: true, opacity: 0.05, depthWrite: false, side: THREE.DoubleSide }));
    const pg = this.keep(new THREE.PlaneGeometry(1, 1));
    const back = this.mesh(pg, glass, cx, h / 2, z0);
    back.scale.set(W, h, 1);
    const left = this.mesh(pg, glass, x0, h / 2, cz);
    left.rotation.y = Math.PI / 2;
    left.scale.set(D, h, 1);
    const frame = this.keep(new THREE.MeshStandardMaterial({ color: '#0a0b0e', roughness: 0.35, metalness: 0.7 }));
    for (let x = x0; x <= x1 + 1e-6; x += W / 10) {
      const m = this.mesh(box, frame, x, h / 2, z0);
      m.scale.set(0.07, h, 0.1);
    }
    for (let z = z0; z <= z1 + 1e-6; z += D / 6) {
      const m = this.mesh(box, frame, x0, h / 2, z);
      m.scale.set(0.1, h, 0.07);
    }
    for (const y of [0.04, h - 0.02]) {
      const b1 = this.mesh(box, frame, cx, y, z0);
      b1.scale.set(W, 0.08, 0.14);
      const b2 = this.mesh(box, frame, x0, y, cz);
      b2.scale.set(0.14, 0.08, D);
    }
    // right wall: walnut slats; front wall: dark plaster, a neon sign
    const slats = this.keep(canvasTex(512, 256, (g) => {
      g.fillStyle = '#120c08';
      g.fillRect(0, 0, 512, 256);
      for (let x = 0; x < 512; x += 32) {
        const grd = g.createLinearGradient(x, 0, x + 26, 0);
        grd.addColorStop(0, '#3b2416');
        grd.addColorStop(0.5, '#4a2e1c');
        grd.addColorStop(1, '#2e1c11');
        g.fillStyle = grd;
        g.fillRect(x + 3, 0, 26, 256);
      }
    }, [D / 1.6, 1]));
    const right = this.mesh(pg, this.keep(new THREE.MeshStandardMaterial({ map: slats, roughness: 0.7 })), x1, h / 2, cz);
    right.rotation.y = -Math.PI / 2;
    right.scale.set(D, h, 1);
    const front = this.mesh(pg, this.keep(new THREE.MeshStandardMaterial({ color: '#15161a', roughness: 0.92 })), cx, h / 2, z1);
    front.rotation.y = Math.PI;
    front.scale.set(W, h, 1);
    const sign = this.keep(neonText('KRYPT', '#22D3EE', 1024, 256, 'PENTHOUSE'));
    const sm = this.mesh(pg, this.keep(new THREE.MeshBasicMaterial({ map: sign, transparent: true, toneMapped: false, color: new THREE.Color(1.05, 1.05, 1.05) })), -2, 2.7, z1 - 0.02);
    sm.rotation.y = Math.PI;
    sm.scale.set(4.2, 1.05, 1);
    // the video wall's backing panel
    const panel = this.mesh(box, this.keep(new THREE.MeshStandardMaterial({ color: '#050608', roughness: 0.4, metalness: 0.3 })), WALL.x + 0.03, WALL.y0 + (WALL.rows * (WALL.h + WALL.gap)) / 2 - WALL.gap / 2, WALL.z0 + (WALL.cols * (WALL.w + WALL.gap)) / 2 - WALL.gap / 2);
    panel.scale.set(0.04, WALL.rows * (WALL.h + WALL.gap) + 0.14, WALL.cols * (WALL.w + WALL.gap) + 0.14);
  }

  private lounge(): void {
    // a rug, an L sofa looking out over the city, a low table
    const rug = this.mesh(this.keep(new THREE.PlaneGeometry(5.2, 3.8)), this.keep(new THREE.MeshStandardMaterial({ color: '#23252b', roughness: 1 })), -9.6, 0.006, 3.6);
    rug.rotation.x = -Math.PI / 2;
    const fabric = this.keep(new THREE.MeshStandardMaterial({ color: '#2c3038', roughness: 0.95 }));
    const seat = this.keep(new RoundedBoxGeometry(1, 1, 1, 3, 0.06));
    const part = (x: number, y: number, z: number, sx: number, sy: number, sz: number) => {
      const m = this.mesh(seat, fabric, x, y, z);
      m.scale.set(sx, sy, sz);
    };
    part(-9.3, 0.2, 5.2, 3.6, 0.4, 1.0);      // long run, back to the front wall
    part(-9.3, 0.62, 5.62, 3.6, 0.5, 0.24);   // its back
    part(-7.2, 0.2, 4.1, 1.0, 0.4, 3.2);      // the return
    part(-6.8, 0.62, 4.1, 0.24, 0.5, 3.2);
    for (const [x, z] of [[-10.4, 5.05], [-9.1, 5.05], [-7.3, 3.4]] as const) part(x, 0.47, z, 0.55, 0.14, 0.5); // cushions
    const glassTop = this.keep(new THREE.MeshStandardMaterial({ color: '#0f1216', roughness: 0.05, metalness: 0.4 }));
    const t = this.mesh(this.keep(new RoundedBoxGeometry(1.6, 0.05, 0.9, 2, 0.02)), glassTop, -9.4, 0.36, 3.7);
    t.castShadow = true;
    const leg = this.keep(new THREE.MeshStandardMaterial({ color: '#0a0b0d', roughness: 0.3, metalness: 0.8 }));
    const box = this.keep(new THREE.BoxGeometry(1, 1, 1));
    for (const [x, z] of [[-10.1, 3.35], [-8.7, 3.35], [-10.1, 4.05], [-8.7, 4.05]] as const) {
      const l = this.mesh(box, leg, x, 0.17, z);
      l.scale.set(0.04, 0.34, 0.04);
    }
    // a floor lamp, and planters by the glass
    const lampShade = this.keep(new THREE.MeshBasicMaterial({ color: new THREE.Color(1.5, 1.25, 0.9), toneMapped: false }));
    const pole = this.mesh(this.keep(new THREE.CylinderGeometry(0.02, 0.02, 1.7, 10)), leg, -11.9, 0.85, 5.5);
    void pole;
    this.mesh(this.keep(new THREE.CylinderGeometry(0.16, 0.22, 0.3, 24, 1, true)), lampShade, -11.9, 1.75, 5.5);
    const pot = this.keep(new THREE.MeshStandardMaterial({ color: '#d9d4cb', roughness: 0.6 }));
    const leaf = this.keep(new THREE.MeshStandardMaterial({ color: '#2f5d3a', roughness: 0.75 }));
    const potG = this.keep(new THREE.CylinderGeometry(0.28, 0.22, 0.6, 24));
    const leafG = this.keep(new THREE.BoxGeometry(0.06, 1, 0.012));
    const r = rng(5);
    for (const [px, pz] of [[-12.3, -7.2], [12.2, -7.4], [-12.3, 1.6]] as const) {
      this.mesh(potG, pot, px, 0.3, pz);
      for (let i = 0; i < 9; i++) {
        const l = this.mesh(leafG, leaf, px + (r() - 0.5) * 0.25, 0.6 + 0.45, pz + (r() - 0.5) * 0.25);
        l.scale.y = 0.7 + r() * 0.7;
        l.position.y = 0.6 + l.scale.y / 2;
        l.rotation.set((r() - 0.5) * 0.35, r() * Math.PI, (r() - 0.5) * 0.35);
      }
    }
  }

  private fridgeMat!: THREE.MeshBasicMaterial;

  private bar(): void {
    // a bar counter on the front wall and, beside it, a glass-door fridge
    // stacked with the same cans the desks drink
    const stone = this.keep(new THREE.MeshStandardMaterial({ color: '#e4e1db', roughness: 0.25, metalness: 0.05 }));
    const dark = this.keep(new THREE.MeshStandardMaterial({ color: '#111216', roughness: 0.5, metalness: 0.3 }));
    const rb = this.keep(new RoundedBoxGeometry(1, 1, 1, 2, 0.02));
    const counter = this.mesh(rb, dark, 6.8, 0.48, 5.9);
    counter.scale.set(4, 0.96, 0.7);
    const top = this.mesh(rb, stone, 6.8, 0.98, 5.85);
    top.scale.set(4.2, 0.05, 0.85);
    const body = this.mesh(rb, dark, 10.6, 0.95, 6.05);
    body.scale.set(1.25, 1.9, 0.75);
    this.fridgeMat = this.keep(new THREE.MeshBasicMaterial({ color: new THREE.Color(0.45, 0.62, 0.95), toneMapped: false }));
    const inside = this.mesh(this.keep(new THREE.PlaneGeometry(1.05, 1.7)), this.fridgeMat, 10.6, 0.98, 5.665);
    inside.rotation.y = Math.PI;
    // cans on three shelves (instanced: 30 cans, one draw)
    const canTex = this.keep(canvasTex(128, 64, (g) => {
      g.fillStyle = '#0a0b0d';
      g.fillRect(0, 0, 128, 64);
      g.fillStyle = '#39FF6A';
      g.fillRect(0, 26, 128, 10);
    }));
    const can = new THREE.InstancedMesh(this.keep(new THREE.CylinderGeometry(0.05, 0.05, 0.17, 14)), this.keep(new THREE.MeshStandardMaterial({ map: canTex, roughness: 0.3, metalness: 0.5, emissive: '#39FF6A', emissiveIntensity: 0.15 })), 30);
    const m4 = new THREE.Matrix4();
    let k = 0;
    for (let s = 0; s < 3; s++) {
      for (let i = 0; i < 10; i++) {
        m4.makeTranslation(10.15 + (i % 5) * 0.22, 0.35 + s * 0.52 + (i >= 5 ? 0.0 : 0.0), 5.78 + (i >= 5 ? 0.14 : 0));
        can.setMatrixAt(k++, m4);
      }
    }
    can.instanceMatrix.needsUpdate = true;
    this.group.add(can);
    const glassDoor = this.mesh(this.keep(new THREE.PlaneGeometry(1.1, 1.78)), this.keep(new THREE.MeshBasicMaterial({ color: '#cfe8ff', transparent: true, opacity: 0.08, depthWrite: false })), 10.6, 0.98, 5.66);
    glassDoor.rotation.y = Math.PI;
    // stools
    const stoolG = this.keep(new THREE.CylinderGeometry(0.2, 0.2, 0.06, 20));
    const stemG = this.keep(new THREE.CylinderGeometry(0.025, 0.025, 0.66, 10));
    for (const x of [5.6, 6.8, 8]) {
      this.mesh(stoolG, dark, x, 0.69, 5.2);
      this.mesh(stemG, dark, x, 0.33, 5.2);
    }
  }

  private elevator(): void {
    const { x0, x1, h } = ELEVATOR;
    const z = ROOM.z1 - 0.03;
    const steel = this.keep(new THREE.MeshStandardMaterial({ color: '#8d929b', roughness: 0.32, metalness: 0.9 }));
    const frame = this.keep(new THREE.MeshStandardMaterial({ color: '#0b0c0f', roughness: 0.3, metalness: 0.7 }));
    const box = this.keep(new THREE.BoxGeometry(1, 1, 1));
    const w = (x1 - x0) / 2;
    for (const cx of [x0 + w / 2, x1 - w / 2]) {
      const p = this.mesh(box, steel, cx, h / 2, z);
      p.scale.set(w - 0.012, h, 0.04);
    }
    for (const [cx, sx] of [[x0 - 0.06, 0.12], [x1 + 0.06, 0.12]] as const) {
      const f = this.mesh(box, frame, cx, (h + 0.12) / 2, z + 0.01);
      f.scale.set(sx, h + 0.12, 0.08);
    }
    const head = this.mesh(box, frame, (x0 + x1) / 2, h + 0.06, z + 0.01);
    head.scale.set(x1 - x0 + 0.24, 0.12, 0.08);
    const sign = this.keep(canvasTex(512, 128, (g) => {
      g.fillStyle = '#07040d';
      g.fillRect(0, 0, 512, 128);
      g.fillStyle = '#C4B5FD';
      g.font = '700 58px "Chakra Petch", sans-serif';
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText('▼ ELEVATOR · EXIT', 256, 66);
    }));
    const sm = this.mesh(this.keep(new THREE.PlaneGeometry(1.2, 0.3)), this.keep(new THREE.MeshBasicMaterial({ map: sign, toneMapped: false, color: new THREE.Color(0.9, 0.9, 0.9) })), (x0 + x1) / 2, h + 0.36, z - 0.02);
    sm.rotation.y = Math.PI;
  }

  /** Every frame: the beacons blink, the windows breathe a little. */
  update(dt: number): void {
    this.t += dt;
    const on = (this.t % 1.6) < 0.18 ? 1 : 0.08;
    this.beacons.color.setRGB(2 * on, 0.18 * on, 0.15 * on);
    this.winUniform.value = 1.0 + Math.sin(this.t * 0.07) * 0.06;
    this.fridgeGlow.color.setRGB(0.45, 0.62, 0.95 + Math.sin(this.t * 0.8) * 0.03);
    // the haze drifts
    this.haze.forEach((h, i) => { h.offset.x = this.t * (0.0016 + i * 0.0011); h.offset.y = this.t * 0.0007 * (i ? -1 : 1); });
  }

  dispose(): void {
    this.disposables.forEach((d) => d.dispose());
    this.disposables = [];
  }
}
