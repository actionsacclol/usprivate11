import * as THREE from 'three';
import { CEIL_H, CLUB_DOOR, FLOOR, FLOOR_EXIT, PROP, SALON_DOOR, SALON_FRONT_Z, SALON_GLASS, WALL_H } from './layout';
import { mergeStatic } from './bake';

// The casino as someone standing in it sees it (walk.ts). The isometric view
// is a dollhouse — the near walls cut away, no ceiling, the salon's glass cut
// down to a rail — because a camera above has to see in. At eye height that
// reads as a set, not a hall. So first person, and ONLY first person, gets
// the rest of the building: the near walls, the walls up to a high coffered
// ceiling hung with chandeliers, the salon's glass to its own lintel, and
// sliding doors out. The group is hidden in the overview, so that view is
// exactly what it was.

const WALL_T = 0.24;
const SALON_H = 3.0;

export interface WalkShell {
  group: THREE.Group;
  /** 0 = shut, 1 = open: the doors out slide as you come up to them. */
  setDoor(open: number): void;
  dispose(): void;
}

function exitSignTexture(): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = 512; c.height = 160;
  const g = c.getContext('2d')!;
  g.fillStyle = '#04140a';
  g.fillRect(0, 0, 512, 160);
  g.strokeStyle = '#4ADE80';
  g.lineWidth = 10;
  g.strokeRect(8, 8, 496, 144);
  g.fillStyle = '#86EFAC';
  g.font = '800 92px "Chakra Petch", sans-serif';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText('EXIT ▸', 256, 86);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export function buildWalkShell(glowTex: THREE.Texture): WalkShell {
  const group = new THREE.Group();
  group.visible = false;
  const textures: THREE.Texture[] = [];
  const { x0, x1, z0, z1 } = FLOOR;

  const panel = new THREE.MeshStandardMaterial({ color: '#2a170f', roughness: 0.6, metalness: 0.05 });
  const wains = new THREE.MeshStandardMaterial({ color: '#3a2014', roughness: 0.5 });
  const damask = new THREE.MeshStandardMaterial({ color: '#3b1020', roughness: 0.85 });
  const brass = new THREE.MeshStandardMaterial({ color: '#C79A45', roughness: 0.3, metalness: 0.85 });
  const glass = new THREE.MeshStandardMaterial({
    color: '#e8e4f0', transparent: true, opacity: 0.08, roughness: 0.05, metalness: 0.3,
    depthWrite: false, side: THREE.DoubleSide,
  });
  const neon = new THREE.MeshStandardMaterial({ color: '#F2C66D', emissive: '#F59E0B', emissiveIntensity: 1.6 });
  const sconce = new THREE.MeshStandardMaterial({ color: '#fff1c2', emissive: '#FFB45E', emissiveIntensity: 2.6 });

  const box = (w: number, h: number, d: number, m: THREE.Material, x: number, y: number, z: number) => {
    const o = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m);
    o.position.set(x, y, z);
    o.receiveShadow = true;
    group.add(o);
    return o;
  };

  // the walls above the overview's: damask between a chair rail and the cornice
  const upper = CEIL_H - WALL_H;
  box(x1 - x0 + 0.44, upper, WALL_T, damask, 0, WALL_H + upper / 2, z0 - 0.1);
  box(WALL_T, upper, z1 - z0 + 0.44, damask, x0 - 0.1, WALL_H + upper / 2, 0);
  // the near walls, whole: wainscot, damask, a brass rail between. The front
  // one has the club's door in it (club.ts hangs the doors and the sign).
  const dl = CLUB_DOOR[0] - CLUB_DOOR[1] / 2, dr = CLUB_DOOR[0] + CLUB_DOOR[1] / 2;
  const DOOR_H = 2.5;
  for (const [w, d, x, z] of [
    [dl - (x0 - 0.22), WALL_T, (x0 - 0.22 + dl) / 2, z1 + 0.1],
    [x1 + 0.22 - dr, WALL_T, (dr + x1 + 0.22) / 2, z1 + 0.1],
    [WALL_T, z1 - z0 + 0.44, x1 + 0.1, 0],
  ] as const) {
    box(w, 0.95, d, wains, x, 0.475, z);
    box(w, CEIL_H - 0.95, d, damask, x, 0.95 + (CEIL_H - 0.95) / 2, z);
    box(w === WALL_T ? WALL_T + 0.06 : w, 0.05, w === WALL_T ? d : WALL_T + 0.06, brass, x, 0.97, z);
  }
  box(dr - dl, CEIL_H - DOOR_H, WALL_T, damask, CLUB_DOOR[0], DOOR_H + (CEIL_H - DOOR_H) / 2, z1 + 0.1);
  // sconces on the near walls, facing in (none where the door is)
  for (let x = x0 + 2; x < x1; x += 4) {
    if (x > dl - 0.4 && x < dr + 0.4) continue;
    box(0.16, 0.24, 0.08, sconce, x, 2.1, z1 - 0.04);
    box(0.22, 0.04, 0.12, brass, x, 1.96, z1 - 0.04);
  }
  for (let z = z0 + 2; z < z1; z += 4) {
    box(0.08, 0.24, 0.16, sconce, x1 - 0.04, 2.1, z);
    box(0.12, 0.04, 0.22, brass, x1 - 0.04, 1.96, z);
  }
  // a gold cove light where the walls meet the ceiling
  for (const [w, d, x, z] of [
    [x1 - x0, 0.06, 0, z0 + 0.05], [x1 - x0, 0.06, 0, z1 - 0.05], [0.06, z1 - z0, x0 + 0.05, 0], [0.06, z1 - z0, x1 - 0.05, 0],
  ] as const) box(w, 0.05, d, neon, x, CEIL_H - 0.12, z);

  // the ceiling, coffered with brass
  {
    const ceiling = new THREE.Mesh(
      new THREE.PlaneGeometry(x1 - x0 + 0.4, z1 - z0 + 0.4),
      new THREE.MeshStandardMaterial({ color: '#140b0d', emissive: '#0c0506', emissiveIntensity: 1, roughness: 0.9, side: THREE.DoubleSide }),
    );
    ceiling.rotation.x = Math.PI / 2;
    ceiling.position.y = CEIL_H + 0.01;
    group.add(ceiling);
    // the coffers' ribs: dark bronze, not mirror brass (bright brass read as scaffolding)
    const rib = new THREE.MeshStandardMaterial({ color: '#3d2b14', roughness: 0.6, metalness: 0.4 });
    for (let x = x0 + 2; x < x1; x += 2) box(0.06, 0.1, z1 - z0, rib, x, CEIL_H - 0.05, 0);
    for (let z = z0 + 2; z < z1; z += 2) box(x1 - x0, 0.1, 0.06, rib, 0, CEIL_H - 0.05, z);
    // a recessed downlight in every coffer: one instanced draw
    const lamp = new THREE.InstancedMesh(
      new THREE.CylinderGeometry(0.07, 0.07, 0.02, 12),
      new THREE.MeshStandardMaterial({ color: '#fff4d6', emissive: '#FFD58A', emissiveIntensity: 2.2 }),
      Math.ceil((x1 - x0) / 2) * Math.ceil((z1 - z0) / 2),
    );
    const m4 = new THREE.Matrix4();
    let n = 0;
    for (let x = x0 + 1; x < x1; x += 2) for (let z = z0 + 1; z < z1; z += 2) lamp.setMatrixAt(n++, m4.makeTranslation(x, CEIL_H - 0.01, z));
    lamp.count = n;
    lamp.frustumCulled = false;
    group.add(lamp);
  }

  // the salon's glass, to its lintel, brass mullions; and its door's arch
  for (const [ax, az, bx, bz] of SALON_GLASS) {
    const len = Math.hypot(bx - ax, bz - az);
    if (len < 0.05) continue;
    const rot = -Math.atan2(bz - az, bx - ax);
    const ux = (bx - ax) / len, uz = (bz - az) / len;
    const put = (m: THREE.Mesh, s: number, y: number) => {
      m.position.set(ax + ux * s, y, az + uz * s);
      m.rotation.y = rot;
      group.add(m);
    };
    const paneH = SALON_H - 1.05;
    put(new THREE.Mesh(new THREE.PlaneGeometry(len, paneH), glass), len / 2, 1.05 + paneH / 2);
    put(new THREE.Mesh(new THREE.BoxGeometry(len, 0.12, 0.16), panel), len / 2, SALON_H);
    const n = Math.max(1, Math.ceil(len / 1.6));
    for (let i = 0; i <= n; i++) put(new THREE.Mesh(new THREE.BoxGeometry(0.05, paneH, 0.08), brass), (len * i) / n, 1.05 + paneH / 2);
  }
  box(SALON_DOOR[1] + 0.2, 0.12, 0.16, panel, SALON_DOOR[0], SALON_H, SALON_FRONT_Z);
  box(SALON_DOOR[1] - 0.2, 0.05, 0.18, neon, SALON_DOOR[0], SALON_H - 0.1, SALON_FRONT_Z);

  // chandeliers: over the pit, each area, the foyer and the bar. Crystals are
  // ONE instanced mesh; their light is bloom on the bulbs.
  {
    const spots: [number, number, number][] = [
      [-2.85, -0.2, 0.95], [3.2, -0.2, 0.95], [-9.5, -7.0, 0.85], [0.5, -7.2, 0.7], [9.4, -7.6, 0.6],
      [-10.5, 6.8, 0.85], [10.3, 6.6, 0.85], [-11.3, 0, 0.6], [...PROP.bar, 0.6],
    ];
    const bulb = new THREE.MeshStandardMaterial({ color: '#fff4d6', emissive: '#FFC56E', emissiveIntensity: 1.6 });
    const crystalGeo = new THREE.OctahedronGeometry(0.05, 0);
    crystalGeo.scale(1, 1.8, 1);
    const crystalMat = new THREE.MeshStandardMaterial({ color: '#fff7e6', emissive: '#FFD58A', emissiveIntensity: 0.7, roughness: 0.1, metalness: 0.2 });
    const PER = 18;
    const crystals = new THREE.InstancedMesh(crystalGeo, crystalMat, spots.length * PER * 3);
    const m4 = new THREE.Matrix4();
    let k = 0;
    for (const [x, z, r] of spots) {
      const y = CEIL_H - 0.9;
      const chain = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, 0.9, 4), brass);
      chain.position.set(x, CEIL_H - 0.45, z);
      group.add(chain);
      for (const [rr, dy] of [[r, 0], [r * 0.62, 0.18], [r * 0.3, 0.34]] as const) {
        const ring = new THREE.Mesh(new THREE.TorusGeometry(rr, 0.025, 6, 32), brass);
        ring.rotation.x = Math.PI / 2;
        ring.position.set(x, y + dy, z);
        group.add(ring);
      }
      const core = new THREE.Mesh(new THREE.SphereGeometry(0.13, 12, 8), bulb);
      core.position.set(x, y - 0.06, z);
      group.add(core);
      for (let i = 0; i < PER; i++) {
        const a = (i / PER) * Math.PI * 2;
        crystals.setMatrixAt(k++, m4.makeTranslation(x + Math.cos(a) * r, y - 0.12, z + Math.sin(a) * r));
        crystals.setMatrixAt(k++, m4.makeTranslation(x + Math.cos(a + 0.2) * r * 0.62, y + 0.05, z + Math.sin(a + 0.2) * r * 0.62));
        crystals.setMatrixAt(k++, m4.makeTranslation(x + Math.cos(a + 0.1) * r * 0.3, y + 0.22, z + Math.sin(a + 0.1) * r * 0.3));
      }
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTex, color: '#FFC56E', transparent: true, opacity: 0.18, blending: THREE.AdditiveBlending, depthWrite: false,
      }));
      halo.scale.setScalar(r * 2.6);
      halo.position.set(x, y - 0.05, z);
      group.add(halo);
    }
    crystals.count = k;
    crystals.frustumCulled = false;
    group.add(crystals);
  }

  // the way out: sliding doors in the entrance wall
  const ex = FLOOR_EXIT[0];
  const ez = (FLOOR_EXIT[1] + FLOOR_EXIT[3]) / 2;
  const face = ex + 0.13;
  const doorH = 2.2;
  const halfW = (FLOOR_EXIT[3] - FLOOR_EXIT[1]) / 2;
  const panels: THREE.Mesh[] = [];
  {
    const dark = new THREE.Mesh(new THREE.PlaneGeometry(halfW * 2, doorH), new THREE.MeshBasicMaterial({ color: '#07040a' }));
    dark.position.set(face + 0.005, doorH / 2, ez);
    dark.rotation.y = Math.PI / 2;
    group.add(dark);
    const green = new THREE.MeshStandardMaterial({ color: '#14532d', emissive: '#4ADE80', emissiveIntensity: 1.8 });
    const panelM = new THREE.MeshStandardMaterial({ color: '#4a2a18', roughness: 0.45, metalness: 0.1 });
    for (const side of [-1, 1]) {
      const p = new THREE.Mesh(new THREE.BoxGeometry(0.06, doorH, halfW), panelM);
      p.position.set(face + 0.05, doorH / 2, ez + side * halfW / 2);
      p.userData.side = side;
      const handle = new THREE.Mesh(new THREE.BoxGeometry(0.03, 0.4, 0.03), brass);
      handle.position.set(0.04, 0.05, -side * (halfW / 2 - 0.08));
      const stripe = new THREE.Mesh(new THREE.BoxGeometry(0.02, doorH - 0.3, 0.03), green);
      stripe.position.set(0.035, 0, -side * (halfW / 2 - 0.03));
      p.add(handle, stripe);
      panels.push(p);
      group.add(p);
      const post = new THREE.Mesh(new THREE.BoxGeometry(0.16, doorH + 0.15, 0.14), brass);
      post.position.set(face + 0.06, (doorH + 0.15) / 2, ez + side * (halfW + 0.07));
      group.add(post);
    }
    const head = new THREE.Mesh(new THREE.BoxGeometry(0.16, 0.15, halfW * 2 + 0.28), brass);
    head.position.set(face + 0.06, doorH + 0.075, ez);
    const tex = exitSignTexture();
    textures.push(tex);
    const sign = new THREE.Mesh(new THREE.PlaneGeometry(0.75, 0.235), new THREE.MeshBasicMaterial({ map: tex, toneMapped: false }));
    // beside the doors: the hall's name hangs over them
    sign.position.set(face + 0.15, 1.9, ez + halfW + 0.65);
    sign.rotation.y = Math.PI / 2;
    group.add(head, sign);
  }

  mergeStatic(group, panels, () => true);

  return {
    group,
    setDoor(open: number) {
      const o = Math.max(0, Math.min(1, open));
      for (const p of panels) p.position.z = ez + (p.userData.side as number) * (halfW / 2 + o * halfW * 0.95);
    },
    dispose() {
      textures.forEach((t) => t.dispose());
    },
  };
}
