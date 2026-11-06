import * as THREE from 'three';

// Cigarette smoke for the whole floor: ONE Points draw. Each puff rises,
// drifts, swells and fades; its size is in world units, so it reads the same
// in the overview's orthographic lens and up close on foot (the vertex shader
// divides by clip w, which is 1 under ortho and the depth under perspective).

const N = 640;

export class Smoke {
  readonly points: THREE.Points;
  private pos = new Float32Array(N * 3).fill(-999);
  private vel = new Float32Array(N * 3);
  private age = new Float32Array(N).fill(99);
  private life = new Float32Array(N).fill(1);
  private size = new Float32Array(N);
  private alpha = new Float32Array(N);
  private amax = new Float32Array(N).fill(0.42);
  private grow = new Float32Array(N).fill(0.12);
  private next = 0;
  private mat: THREE.ShaderMaterial;

  constructor() {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(this.pos, 3));
    g.setAttribute('size', new THREE.BufferAttribute(this.size, 1));
    g.setAttribute('alpha', new THREE.BufferAttribute(this.alpha, 1));
    this.mat = new THREE.ShaderMaterial({
      transparent: true, depthWrite: false,
      uniforms: { uHalfH: { value: 360 }, uColor: { value: new THREE.Color('#c9c2ba') } },
      vertexShader: /* glsl */`
        attribute float size; attribute float alpha;
        uniform float uHalfH;
        varying float vA;
        void main() {
          vA = alpha;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_Position = projectionMatrix * mv;
          gl_PointSize = size * projectionMatrix[1][1] * uHalfH / gl_Position.w;
        }`,
      fragmentShader: /* glsl */`
        uniform vec3 uColor;
        varying float vA;
        void main() {
          vec2 c = gl_PointCoord - 0.5;
          float d = length(c);
          float a = smoothstep(0.5, 0.1, d) * vA;
          if (a < 0.01) discard;
          gl_FragColor = vec4(uColor, a);
        }`,
    });
    this.points = new THREE.Points(g, this.mat);
    this.points.frustumCulled = false;
    this.points.renderOrder = 2;
  }

  /** The 3D frame's height in pixels: point sizes are in its pixels. */
  setViewHeight(h: number): void {
    this.mat.uniforms.uHalfH.value = h / 2;
  }

  /** One puff at (x, y, z). `big` is an exhale. The options shape your own
   *  smoke (walk mode): a thin wisp off the cigarette in your paw, a cloud
   *  blown out ahead of you, the revolver's muzzle smoke, plaster dust. */
  emit(
    x: number, y: number, z: number, big = false,
    o: { size?: number; grow?: number; life?: number; alpha?: number; v?: readonly [number, number, number] } = {},
  ): void {
    const i = this.next;
    this.next = (this.next + 1) % N;
    this.pos[i * 3] = x; this.pos[i * 3 + 1] = y; this.pos[i * 3 + 2] = z;
    const a = Math.random() * Math.PI * 2;
    const s = big ? 0.12 : 0.04;
    this.vel[i * 3] = Math.cos(a) * s;
    this.vel[i * 3 + 1] = big ? 0.35 : 0.26 + Math.random() * 0.12;
    this.vel[i * 3 + 2] = Math.sin(a) * s;
    this.age[i] = 0;
    this.life[i] = big ? 2.4 : 1.6 + Math.random() * 0.8;
    this.size[i] = big ? 0.12 : 0.05;
    this.amax[i] = 0.42;
    this.grow[i] = 0.12;
    if (o.v) { this.vel[i * 3] = o.v[0]; this.vel[i * 3 + 1] = o.v[1]; this.vel[i * 3 + 2] = o.v[2]; }
    if (o.size !== undefined) this.size[i] = o.size;
    if (o.grow !== undefined) this.grow[i] = o.grow;
    if (o.life !== undefined) this.life[i] = o.life;
    if (o.alpha !== undefined) this.amax[i] = o.alpha;
  }

  update(dt: number): void {
    let any = false;
    for (let i = 0; i < N; i++) {
      if (this.age[i] >= this.life[i]) {
        if (this.alpha[i] !== 0) { this.alpha[i] = 0; any = true; }
        continue;
      }
      any = true;
      this.age[i] += dt;
      const k = this.age[i] / this.life[i];
      // a slow curl, slowing as it climbs; a puff blown sideways loses its push
      this.vel[i * 3] += Math.sin(this.age[i] * 3 + i) * 0.02 * dt;
      const drag = Math.exp(-1.6 * dt);
      this.vel[i * 3] *= drag;
      this.vel[i * 3 + 2] *= drag;
      this.pos[i * 3] += this.vel[i * 3] * dt;
      this.pos[i * 3 + 1] += this.vel[i * 3 + 1] * dt * (1 - k * 0.5);
      this.pos[i * 3 + 2] += this.vel[i * 3 + 2] * dt;
      this.size[i] += dt * this.grow[i];
      this.alpha[i] = Math.min(1, k * 6) * (1 - k) * this.amax[i];
    }
    if (!any) return;
    const g = this.points.geometry;
    (g.attributes.position as THREE.BufferAttribute).needsUpdate = true;
    (g.attributes.size as THREE.BufferAttribute).needsUpdate = true;
    (g.attributes.alpha as THREE.BufferAttribute).needsUpdate = true;
  }

  dispose(): void {
    this.points.geometry.dispose();
    this.mat.dispose();
  }
}
