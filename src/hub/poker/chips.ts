import * as THREE from 'three';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';

// Casino chips, the table's whole vocabulary. Every chip in the room — the
// dealer's rack, each player's stacks, the bets on the felt, the pot — is ONE
// instanced mesh with one material: the chip's colour is per instance, and
// the white edge inlays and face ring come from a shared greyscale mask that
// the shader mixes toward ivory. So a room of ~600 chips is one draw call
// (plus its shadow), and nothing here allocates per frame.
//
// The chips are props. They carry no stake and no money: a stack's height is
// the leaderboard's real record (room.ts), a bet is a real call's side, the
// pot grows only on a real settlement — never a number invented for them.

export const CHIP_R = 0.1;
export const CHIP_H = 0.026;

export const CHIP = {
  white: '#E6E0D2', red: '#B3122E', green: '#12804A', black: '#1C1C22',
  purple: '#5B2A86', blue: '#1F4FA8', gold: '#C99A2E', teal: '#0F766E',
} as const;

/** The mask: left half the edge (inlays), right half the face (rim dashes and
 *  an inner ring). Red channel = "ivory here". */
function maskTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = 256; c.height = 128;
  const g = c.getContext('2d')!;
  g.fillStyle = '#000';
  g.fillRect(0, 0, 256, 128);
  g.fillStyle = '#fff';
  // edge: six inlays round the rim, full height bar a thin lip
  for (let i = 0; i < 6; i++) g.fillRect(i * (128 / 6) + 5, 26, 10, 76);
  // face: eight rim dashes, a thin inner ring, a dot of a centre
  const cx = 192, cy = 64;
  for (let i = 0; i < 8; i++) {
    const a = (i / 8) * Math.PI * 2;
    g.save();
    g.translate(cx + Math.cos(a) * 54, cy + Math.sin(a) * 54);
    g.rotate(a);
    g.fillRect(-5, -9, 10, 18);
    g.restore();
  }
  g.strokeStyle = '#fff';
  g.lineWidth = 3;
  g.beginPath(); g.arc(cx, cy, 38, 0, Math.PI * 2); g.stroke();
  const t = new THREE.CanvasTexture(c);
  t.anisotropy = 4;
  return t;
}

/** One chip: an open edge and a top face (nothing ever sees a chip's
 *  underside), UVs pointed at the mask's two halves. 48 triangles: ~600
 *  chips, drawn twice (shadow + main), stay a rounding error. */
function chipGeometry(): THREE.BufferGeometry {
  const seg = 16;
  const side = new THREE.CylinderGeometry(CHIP_R, CHIP_R, CHIP_H, seg, 1, true);
  const top = new THREE.CircleGeometry(CHIP_R, seg);
  top.rotateX(-Math.PI / 2);
  top.translate(0, CHIP_H / 2, 0);
  const remap = (g: THREE.BufferGeometry, face: boolean) => {
    const uv = g.attributes.uv as THREE.BufferAttribute;
    for (let i = 0; i < uv.count; i++) uv.setX(i, face ? 0.5 + uv.getX(i) * 0.5 : uv.getX(i) * 0.5);
  };
  remap(side, false);
  remap(top, true);
  const g = mergeGeometries([side.toNonIndexed(), top.toNonIndexed()])!;
  side.dispose();
  top.dispose();
  return g;
}

export class ChipKit {
  readonly geo = chipGeometry();
  readonly mask = maskTexture();
  readonly mat: THREE.MeshStandardMaterial;

  constructor() {
    this.mat = new THREE.MeshStandardMaterial({ color: '#ffffff', map: this.mask, roughness: 0.42, metalness: 0.04 });
    this.mat.onBeforeCompile = (s) => {
      s.fragmentShader = s.fragmentShader
        .replace('#include <map_fragment>', 'float chipMask = texture2D( map, vMapUv ).r;')
        .replace('#include <color_fragment>', '#include <color_fragment>\n\tdiffuseColor.rgb = mix( diffuseColor.rgb, vec3( 0.92, 0.89, 0.82 ), chipMask );');
    };
    this.mat.customProgramCacheKey = () => 'krypt-poker-chip';
  }

  dispose(): void {
    this.geo.dispose();
    this.mask.dispose();
    this.mat.dispose();
  }
}

/** A fixed pool of chip instances; ranges are handed out at build time.
 *  Writes are batched: the GPU upload happens once in `flush()`. */
export class ChipField {
  readonly mesh: THREE.InstancedMesh;
  private used = 0;
  private dirtyM = false;
  private dirtyC = false;
  private _m = new THREE.Matrix4();
  private _q = new THREE.Quaternion();
  private _e = new THREE.Euler();
  private _p = new THREE.Vector3();
  private _s = new THREE.Vector3(1, 1, 1);
  private _c = new THREE.Color();
  private static ZERO = new THREE.Matrix4().makeScale(0, 0, 0);

  constructor(kit: ChipKit, readonly capacity: number) {
    this.mesh = new THREE.InstancedMesh(kit.geo, kit.mat, capacity);
    this.mesh.castShadow = true;
    this.mesh.receiveShadow = true;
    // instances move (bets, the pot): a stale bounding sphere would cull them
    this.mesh.frustumCulled = false;
    for (let i = 0; i < capacity; i++) {
      this.mesh.setMatrixAt(i, ChipField.ZERO);
      this.mesh.setColorAt(i, this._c.set(CHIP.white));
    }
  }

  alloc(n: number): number {
    const start = this.used;
    this.used = Math.min(this.capacity, this.used + n);
    return start;
  }

  /** Place chip `i` lying flat at (x, y, z), turned `yaw`, or on its edge
   *  (`edge`: the face looking down +z, as in a dealer's rack). */
  put(i: number, x: number, y: number, z: number, yaw = 0, edge = false, tilt = 0): void {
    this._e.set(edge ? Math.PI / 2 : tilt, yaw, 0);
    this._m.compose(this._p.set(x, y, z), this._q.setFromEuler(this._e), this._s);
    this.mesh.setMatrixAt(i, this._m);
    this.dirtyM = true;
  }

  hide(i: number): void {
    this.mesh.setMatrixAt(i, ChipField.ZERO);
    this.dirtyM = true;
  }

  color(i: number, c: string): void {
    this.mesh.setColorAt(i, this._c.set(c));
    this.dirtyC = true;
  }

  flush(): void {
    if (this.dirtyM) this.mesh.instanceMatrix.needsUpdate = true;
    if (this.dirtyC && this.mesh.instanceColor) this.mesh.instanceColor.needsUpdate = true;
    this.dirtyM = this.dirtyC = false;
  }
}
