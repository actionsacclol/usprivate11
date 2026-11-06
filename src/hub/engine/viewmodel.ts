import * as THREE from 'three';
import type { Garnish, Glass } from '../casino/bar';
import type { Hotbar, ItemId } from '../casino/hotbar';
import { canvasTex } from './casinoTextures';
import { PINK } from './crew';

// Your own paw in first person, and what's in it: a cigarette, a beer, a
// bottle of vodka, a revolver. It lives in its own little scene, drawn over
// the hall by a second render pass that clears depth first, so a bottle never
// sinks into the wall you're leaning on and the wheel's zoom never stretches
// your arm. Same flat-shaded, low-poly kit as the rats on the floor; the
// frame's PS2 pixels and dither land on it like on everything else.
//
// Poses are authored in this camera's space (it sits at the origin looking
// down -z): where the grip is, which way the item points, and which way the
// forearm reaches. The item is placed at the grip with its own orientation,
// and the arm is posed to meet it, so a bottle can tip up to your mouth
// without the arm having to bend round it.

const V = (x: number, y: number, z: number) => new THREE.Vector3(x, y, z);
const UP = new THREE.Vector3(0, 1, 0);

interface Pose { at: THREE.Vector3; dir: THREE.Vector3; reach: THREE.Vector3 }
interface ItemRig { obj: THREE.Object3D; axis: THREE.Vector3; rest: Pose; up: Pose }

function flat(color: string, o: { rough?: number; metal?: number; emissive?: string; ei?: number; map?: THREE.Texture } = {}): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({
    color, roughness: o.rough ?? 0.75, metalness: o.metal ?? 0, flatShading: true,
    emissive: o.emissive ?? '#000000', emissiveIntensity: o.ei ?? 1, map: o.map ?? null,
  });
}

function mesh(geo: THREE.BufferGeometry, mat: THREE.Material, x = 0, y = 0, z = 0): THREE.Mesh {
  const m = new THREE.Mesh(geo, mat);
  m.position.set(x, y, z);
  return m;
}

/** A cylinder lying along +z. */
function rod(r0: number, r1: number, len: number, seg = 6): THREE.CylinderGeometry {
  const g = new THREE.CylinderGeometry(r1, r0, len, seg);
  g.rotateX(Math.PI / 2);
  return g;
}

function nearest(t: THREE.Texture): THREE.Texture {
  t.magFilter = THREE.NearestFilter;
  t.minFilter = THREE.NearestFilter;
  t.generateMipmaps = false;
  t.anisotropy = 1;
  return t;
}

function labelTex(bg: string, ink: string, top: string, big: string): THREE.Texture {
  return nearest(canvasTex(64, 32, (g, w, h) => {
    g.fillStyle = bg;
    g.fillRect(0, 0, w, h);
    g.fillStyle = ink;
    g.fillRect(0, 3, w, 2);
    g.fillRect(0, h - 5, w, 2);
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.font = 'bold 7px monospace';
    g.fillText(top, w / 4, 10);
    g.font = 'bold 11px sans-serif';
    g.fillText(big, w / 4, 19);
    // the label wraps round: print it twice so it's seen from either side
    g.font = 'bold 7px monospace';
    g.fillText(top, (3 * w) / 4, 10);
    g.font = 'bold 11px sans-serif';
    g.fillText(big, (3 * w) / 4, 19);
  }));
}

function pinstripeTex(): THREE.Texture {
  const t = nearest(canvasTex(16, 8, (g, w, h) => {
    g.fillStyle = '#20232c';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#4a4f5e';
    g.fillRect(3, 0, 1, h);
    g.fillRect(11, 0, 1, h);
  }, true));
  t.repeat.set(3, 1);
  return t;
}

function flashTex(): THREE.Texture {
  return canvasTex(64, 64, (g, w, h) => {
    const cx = w / 2, cy = h / 2;
    const grd = g.createRadialGradient(cx, cy, 0, cx, cy, w / 2);
    grd.addColorStop(0, 'rgba(255,255,240,1)');
    grd.addColorStop(0.25, 'rgba(255,214,120,0.9)');
    grd.addColorStop(1, 'rgba(255,120,30,0)');
    g.fillStyle = grd;
    g.beginPath();
    // a six-point star
    for (let i = 0; i < 12; i++) {
      const a = (i / 12) * Math.PI * 2;
      const r = i % 2 === 0 ? w / 2 : w / 7;
      g.lineTo(cx + Math.cos(a) * r, cy + Math.sin(a) * r);
    }
    g.closePath();
    g.fill();
  });
}

export class ViewModel {
  readonly scene = new THREE.Scene();
  readonly camera = new THREE.PerspectiveCamera(62, 16 / 9, 0.01, 5);
  private arm = new THREE.Group();
  private rigs: Partial<Record<ItemId, ItemRig>> = {};
  private paper!: THREE.Mesh;
  private ash!: THREE.Mesh;
  private ember!: THREE.Mesh;
  private emberMat!: THREE.MeshStandardMaterial;
  private liquid!: THREE.Mesh;
  /** The cocktail: a glass of each shape, its drink, its garnish. */
  private glasses = new Map<Glass, { obj: THREE.Group; drink: THREE.Mesh; base: number }>();
  private garnishes = new Map<Garnish, THREE.Object3D>();
  private drinkMat!: THREE.MeshStandardMaterial;
  private servedId = '';
  private cylinder = new THREE.Group();
  private flash = new THREE.Group();
  private muzzle = new THREE.Object3D();
  private glow = new THREE.PointLight('#ff8a3c', 0, 0, 2);
  private spinShown = 0;
  private flashT = 0;
  private sway = new THREE.Vector2();
  private lastLook: { yaw: number; pitch: number } | null = null;
  private owned: { dispose(): void }[] = [];
  private _p = new THREE.Vector3();
  private _q = new THREE.Quaternion();
  private _q2 = new THREE.Quaternion();
  private _e = new THREE.Euler();
  private _bx = new THREE.Vector3();
  private _by = new THREE.Vector3();
  private _bm = new THREE.Matrix4();

  constructor(env: THREE.Texture | null) {
    this.scene.environment = env;
    this.scene.environmentIntensity = 0.3;
    this.scene.add(new THREE.HemisphereLight('#ffe1b8', '#3a1420', 1.0));
    const key = new THREE.DirectionalLight('#ffd6a8', 1.5);
    key.position.set(-0.6, 1, 0.5);
    this.scene.add(key, this.glow);

    this.buildArm();
    this.rigs.cig = this.buildCig();
    this.rigs.beer = this.buildBottle('beer');
    this.rigs.vodka = this.buildBottle('vodka');
    this.rigs.gun = this.buildGun();
    this.rigs.cocktail = this.buildCocktail();
    for (const r of Object.values(this.rigs)) if (r) this.scene.add(r.obj);
    this.scene.add(this.arm);
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      if (!m.isMesh) return;
      this.owned.push(m.geometry);
      const mats = Array.isArray(m.material) ? m.material : [m.material];
      for (const mt of mats) {
        this.owned.push(mt);
        const map = (mt as THREE.MeshStandardMaterial).map;
        if (map) this.owned.push(map);
      }
    });
  }

  setAspect(a: number): void {
    this.camera.aspect = a;
    this.camera.updateProjectionMatrix();
  }

  // ── the parts ────────────────────────────────────────────────────────────

  private buildArm(): void {
    const pink = flat(PINK, { rough: 0.85 });
    const palm = mesh(new THREE.BoxGeometry(0.042, 0.026, 0.05), pink);
    const fingers = new THREE.BoxGeometry(0.0078, 0.0078, 0.038);
    for (let i = 0; i < 4; i++) {
      const f = mesh(fingers, pink, -0.0165 + i * 0.011, -0.006, 0.036);
      f.rotation.x = 0.9;
      this.arm.add(f);
    }
    const thumb = mesh(new THREE.BoxGeometry(0.011, 0.011, 0.03), pink, -0.029, 0.006, 0.012);
    thumb.rotation.y = 0.55;
    const wrist = mesh(rod(0.02, 0.02, 0.03, 6), pink, 0, 0, -0.03);
    const cuff = mesh(rod(0.031, 0.031, 0.024, 7), flat('#f4f1ea', { rough: 0.6 }), 0, 0, -0.05);
    const link = mesh(new THREE.BoxGeometry(0.008, 0.008, 0.008), flat('#E7C26A', { rough: 0.3, metal: 0.8 }), 0.031, 0, -0.05);
    const sleeve = mesh(rod(0.048, 0.038, 0.5, 7), flat('#ffffff', { rough: 0.8, map: pinstripeTex() }), 0, 0, -0.31);
    this.arm.add(palm, thumb, wrist, cuff, link, sleeve);
  }

  private buildCig(): ItemRig {
    const g = new THREE.Group();
    const filter = mesh(rod(0.0055, 0.0055, 0.022), flat('#d98a3a'), 0, 0, -0.004);
    const paperGeo = rod(0.0055, 0.0055, 1);
    paperGeo.translate(0, 0, 0.5);
    this.paper = mesh(paperGeo, flat('#f3efe6', { rough: 0.9 }), 0, 0, 0.007);
    this.ash = mesh(rod(0.0056, 0.0056, 0.004), flat('#8f8a84'));
    this.emberMat = flat('#7a1d08', { emissive: '#ff5a1f', ei: 1.4 });
    this.ember = mesh(rod(0.0057, 0.0052, 0.005), this.emberMat);
    g.add(filter, this.paper, this.ash, this.ember);
    return {
      obj: g, axis: V(0, 0, 1),
      // between the fingers, low on the right, the lit end up and away
      rest: { at: V(0.2, -0.2, -0.44), dir: V(-0.3, 0.55, -0.78), reach: V(-0.35, 0.35, -0.87) },
      // a drag: the paw comes up under your snout, the lit end out ahead
      up: { at: V(0.035, -0.135, -0.25), dir: V(0.15, 0.45, -0.88), reach: V(-0.3, 0.8, -0.5) },
    };
  }

  private buildBottle(kind: 'beer' | 'vodka'): ItemRig {
    const g = new THREE.Group();
    const lathe = (pts: [number, number][]) => new THREE.LatheGeometry(pts.map(([x, y]) => new THREE.Vector2(x, y)), 8);
    if (kind === 'beer') {
      const glass = flat('#4a2408', { rough: 0.22 });
      g.add(mesh(lathe([
        [0, -0.075], [0.029, -0.075], [0.032, -0.07], [0.032, 0.035], [0.027, 0.055],
        [0.013, 0.078], [0.011, 0.105], [0.0125, 0.122], [0.0125, 0.128], [0, 0.128],
      ]), glass));
      const label = new THREE.MeshStandardMaterial({ map: labelTex('#f0dfae', '#9b1c1c', 'KRYPT', 'LAGER'), roughness: 0.8, flatShading: true });
      g.add(mesh(new THREE.CylinderGeometry(0.0326, 0.0326, 0.05, 8, 1, true), label, 0, -0.018, 0));
      const neck = new THREE.MeshStandardMaterial({ color: '#E7C26A', roughness: 0.4, metalness: 0.6, flatShading: true });
      g.add(mesh(new THREE.CylinderGeometry(0.0128, 0.0128, 0.016, 8, 1, true), neck, 0, 0.098, 0));
    } else {
      const liquidMat = new THREE.MeshStandardMaterial({
        color: '#cdeeff', roughness: 0.05, transparent: true, opacity: 0.5, depthWrite: false, flatShading: true,
      });
      const liq = new THREE.CylinderGeometry(0.026, 0.026, 0.115, 8);
      liq.translate(0, 0.0575, 0);
      this.liquid = mesh(liq, liquidMat, 0, -0.077, 0);
      this.liquid.renderOrder = 1;
      const glass = new THREE.Mesh(new THREE.LatheGeometry([
        [0, -0.08], [0.028, -0.08], [0.03, -0.075], [0.03, 0.04], [0.024, 0.058],
        [0.012, 0.075], [0.011, 0.112], [0, 0.112],
      ].map(([x, y]) => new THREE.Vector2(x, y)), 8), new THREE.MeshStandardMaterial({
        color: '#e6f6ff', roughness: 0.06, metalness: 0.1, transparent: true, opacity: 0.32, depthWrite: false, flatShading: true,
      }));
      glass.renderOrder = 2;
      const label = new THREE.MeshStandardMaterial({ map: labelTex('#f7f5ef', '#c81e1e', 'KRYPT', 'ВОДКА'), roughness: 0.8, flatShading: true });
      g.add(
        this.liquid, glass,
        mesh(new THREE.CylinderGeometry(0.0305, 0.0305, 0.045, 8, 1, true), label, 0, -0.02, 0),
        mesh(new THREE.CylinderGeometry(0.0128, 0.0128, 0.02, 8), flat('#b91c1c', { rough: 0.5 }), 0, 0.12, 0),
      );
    }
    return {
      obj: g, axis: V(0, 1, 0),
      rest: { at: V(0.21, -0.23, -0.46), dir: V(-0.08, 1, 0.06), reach: V(-0.3, 0.3, -0.9) },
      // tipped up: the neck at your lips, the bottom up in front of you
      up: { at: V(0.02, -0.07, -0.24), dir: V(-0.03, -0.42, 0.9), reach: V(-0.35, 0.85, -0.4) },
    };
  }

  private buildCocktail(): ItemRig {
    const g = new THREE.Group();
    const glass = new THREE.MeshStandardMaterial({
      color: '#eef8ff', roughness: 0.05, metalness: 0.1, transparent: true, opacity: 0.35, depthWrite: false, flatShading: true,
    });
    const solid = flat('#e6f2fa', { rough: 0.1 });
    // nearly opaque, matte (no reflections: a shiny drink mirrored the room
    // and read as milk), glowing a little in its own colour
    this.drinkMat = new THREE.MeshStandardMaterial({ color: '#c4772a', roughness: 0.75, metalness: 0, envMapIntensity: 0, transparent: true, opacity: 0.95, flatShading: true, emissive: '#c4772a', emissiveIntensity: 0.18 });
    const stemmed = (bowl: THREE.BufferGeometry, bowlY: number, drink: THREE.BufferGeometry, drinkY: number) => {
      const o = new THREE.Group();
      o.add(mesh(new THREE.CylinderGeometry(0.035, 0.035, 0.005, 10), solid, 0, -0.06, 0));
      o.add(mesh(new THREE.CylinderGeometry(0.0045, 0.0045, 0.075, 6), solid, 0, -0.022, 0));
      const b = mesh(bowl, glass, 0, bowlY, 0);
      b.renderOrder = 2;
      const d = mesh(drink, this.drinkMat, 0, drinkY, 0);
      d.renderOrder = 1;
      o.add(d, b);
      return { obj: o, drink: d, base: drinkY };
    };
    // a martini's cone, a coupe's shallow bowl: the drink a cone/bowl too,
    // its apex at the bottom so it shrinks toward it as you drink
    const cone = (r: number, h: number) => { const c = new THREE.CylinderGeometry(r, 0.003, h, 10); c.translate(0, h / 2, 0); return c; };
    this.glasses.set('martini', stemmed(new THREE.CylinderGeometry(0.058, 0.004, 0.065, 10, 1, true), 0.048, cone(0.05, 0.055), 0.017));
    this.glasses.set('coupe', stemmed(new THREE.CylinderGeometry(0.056, 0.022, 0.035, 10, 1, true), 0.033, cone(0.05, 0.03), 0.017));
    {
      // a rocks glass: a heavy tumbler with real walls and a thick base, the
      // drink over one big rock of ice. Held from the side (the paw at the
      // group's origin grips its wall), not through the middle.
      const o = new THREE.Group();
      o.position.x = -0.05;
      const wall = new THREE.LatheGeometry([
        [0, -0.04], [0.038, -0.04], [0.041, -0.037], [0.044, 0.036], [0.044, 0.04],
        [0.0405, 0.04], [0.0385, -0.018], [0, -0.018],
      ].map(([x, y]) => new THREE.Vector2(x, y)), 12);
      const tumbler = mesh(wall, new THREE.MeshStandardMaterial({
        color: '#cfe6ee', roughness: 0.3, metalness: 0, envMapIntensity: 0.12, transparent: true, opacity: 0.14, depthWrite: false, flatShading: true, side: THREE.FrontSide,
      }), 0, 0, 0);
      tumbler.renderOrder = 3;
      const heavy = mesh(new THREE.CylinderGeometry(0.0385, 0.04, 0.021, 12), new THREE.MeshStandardMaterial({
        color: '#dff2fb', roughness: 0.3, envMapIntensity: 0.2, transparent: true, opacity: 0.45, depthWrite: false, flatShading: true,
      }), 0, -0.029, 0);
      heavy.renderOrder = 2;
      const dg = new THREE.CylinderGeometry(0.0375, 0.0372, 0.042, 12);
      dg.translate(0, 0.021, 0);
      const d = mesh(dg, this.drinkMat, 0, -0.018, 0);
      d.renderOrder = 1;
      const rock = mesh(new THREE.BoxGeometry(0.03, 0.028, 0.03), new THREE.MeshStandardMaterial({
        color: '#e8f6ff', roughness: 0.05, transparent: true, opacity: 0.35, depthWrite: false, flatShading: true,
      }), 0, 0.01, 0);
      rock.rotation.set(0.25, 0.6, 0.12);
      rock.renderOrder = 2;
      o.add(heavy, d, rock, tumbler);
      this.glasses.set('rocks', { obj: o, drink: d, base: -0.018 });
    }
    // garnishes: an olive on a pick, a cherry, a twist of peel, a wedge of lime
    const pick = (fruit: THREE.Mesh) => {
      const o = new THREE.Group();
      const stick = mesh(new THREE.CylinderGeometry(0.0015, 0.0015, 0.08, 4), flat('#d9b05a'), 0, 0, 0);
      fruit.position.y = -0.018;
      o.add(stick, fruit);
      o.position.set(0.02, 0.06, 0);
      o.rotation.z = 0.35;
      return o;
    };
    this.garnishes.set('olive', pick(mesh(new THREE.SphereGeometry(0.009, 6, 4), flat('#6a8f2a'))));
    this.garnishes.set('cherry', pick(mesh(new THREE.SphereGeometry(0.01, 6, 4), flat('#c8102e', { rough: 0.2 }))));
    // a twist of orange peel, hung over the rim
    const peel = new THREE.Group();
    const twist = mesh(new THREE.TorusGeometry(0.011, 0.0035, 4, 8, Math.PI * 1.3), flat('#f39c12'), 0, 0, 0);
    twist.rotation.set(0, Math.PI / 2, 0.4);
    peel.add(twist, mesh(new THREE.BoxGeometry(0.004, 0.024, 0.009), flat('#f6a23a'), 0.004, -0.014, 0));
    this.garnishes.set('peel', peel);
    const lime = mesh(new THREE.CylinderGeometry(0.018, 0.018, 0.008, 6, 1, false, 0, Math.PI), flat('#7ac943'), 0.045, 0.072, 0);
    lime.rotation.x = Math.PI / 2;
    this.garnishes.set('lime', lime);
    for (const v of this.glasses.values()) g.add(v.obj);
    for (const v of this.garnishes.values()) g.add(v);
    return {
      obj: g, axis: V(0, 1, 0),
      rest: { at: V(0.2, -0.19, -0.44), dir: V(-0.06, 1, 0.05), reach: V(-0.3, 0.3, -0.9) },
      // up to your lips, tipped back
      up: { at: V(0.02, -0.075, -0.22), dir: V(-0.02, 0.35, 0.94), reach: V(-0.35, 0.85, -0.4) },
    };
  }

  private buildGun(): ItemRig {
    const g = new THREE.Group();
    const steel = flat('#2a2d33', { rough: 0.4, metal: 0.65 });
    const blued = flat('#3a3f48', { rough: 0.32, metal: 0.75 });
    const wood = flat('#5b3418', { rough: 0.7 });
    const grip = mesh(new THREE.BoxGeometry(0.022, 0.066, 0.03), wood, 0, -0.012, -0.012);
    grip.rotation.x = 0.3;
    const cyl = mesh(rod(0.019, 0.019, 0.038, 6), blued);
    this.cylinder.position.set(0, 0.034, 0.03);
    this.cylinder.add(cyl);
    // the six chambers, so the turn reads
    const hole = new THREE.CylinderGeometry(0.0042, 0.0042, 0.002, 5);
    hole.rotateX(Math.PI / 2);
    const black = flat('#0b0c0f');
    for (let i = 0; i < 6; i++) {
      const a = (i / 6) * Math.PI * 2;
      this.cylinder.add(mesh(hole, black, Math.cos(a) * 0.011, Math.sin(a) * 0.011, 0.0195));
    }
    const hammer = mesh(new THREE.BoxGeometry(0.006, 0.016, 0.01), steel, 0, 0.052, -0.016);
    hammer.rotation.x = -0.4;
    const guard = mesh(new THREE.TorusGeometry(0.013, 0.0025, 4, 8), steel, 0, 0.008, 0.018);
    guard.rotation.y = Math.PI / 2;
    g.add(
      grip, this.cylinder, hammer, guard,
      mesh(new THREE.BoxGeometry(0.02, 0.034, 0.05), steel, 0, 0.03, 0.012),
      mesh(rod(0.0075, 0.0075, 0.1, 6), blued, 0, 0.044, 0.095),
      mesh(new THREE.BoxGeometry(0.009, 0.01, 0.06), steel, 0, 0.033, 0.085),
      mesh(new THREE.BoxGeometry(0.01, 0.006, 0.1), steel, 0, 0.053, 0.08),
      mesh(new THREE.BoxGeometry(0.004, 0.01, 0.008), steel, 0, 0.058, 0.138),
      mesh(new THREE.BoxGeometry(0.004, 0.014, 0.004), steel, 0, 0.011, 0.016),
    );
    // the flash: a star facing you and two crossed plumes along the barrel
    const fm = new THREE.MeshBasicMaterial({
      map: flashTex(), color: new THREE.Color('#ffd27a').multiplyScalar(4), transparent: true,
      blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide,
    });
    const plane = new THREE.PlaneGeometry(0.11, 0.11);
    const face = mesh(plane, fm);
    const p1 = mesh(plane, fm, 0, 0, 0.04);
    p1.rotation.y = Math.PI / 2;
    p1.scale.set(2.2, 0.8, 1);
    const p2 = mesh(plane, fm, 0, 0, 0.04);
    p2.rotation.set(Math.PI / 2, 0, Math.PI / 2);
    p2.scale.set(0.8, 2.2, 1);
    this.flash.add(face, p1, p2);
    this.flash.position.set(0, 0.044, 0.152);
    this.flash.visible = false;
    this.muzzle.position.set(0, 0.044, 0.15);
    g.add(this.flash, this.muzzle);
    // aimed so the barrel meets the crosshair a few metres out
    const at = V(0.13, -0.15, -0.4);
    const dir = V(0, 0, -6).sub(V(at.x, at.y + 0.044, at.z)).normalize();
    return {
      obj: g, axis: V(0, 0, 1),
      rest: { at, dir, reach: V(-0.12, 0.22, -0.97) },
      up: { at, dir, reach: V(-0.12, 0.22, -0.97) },
    };
  }

  // ── per frame ────────────────────────────────────────────────────────────

  /** Pose the paw for this frame. `move` 0–1 is how fast you're walking,
   *  `bob` the walker's step phase; yaw/pitch give the arm a lag behind a
   *  quick look. */
  update(dt: number, t: number, hb: Hotbar, v: { move: number; bob: number; yaw: number; pitch: number; visible: boolean }): void {
    const item = hb.shown;
    const rig = item === 'hand' || (item === 'cocktail' && !hb.cocktail) ? undefined : this.rigs[item];
    for (const r of Object.values(this.rigs)) if (r) r.obj.visible = r === rig && v.visible;
    this.arm.visible = !!rig && v.visible;
    this.flashT = Math.max(0, this.flashT - dt);
    this.flash.visible = this.flashT > 0 && item === 'gun';
    if (!rig || !v.visible) {
      this.glow.intensity = 0;
      this.lastLook = null;
      return;
    }

    // a lag behind the look: turn quickly and the paw trails, then catches up
    if (this.lastLook) {
      let dy = v.yaw - this.lastLook.yaw;
      dy = Math.atan2(Math.sin(dy), Math.cos(dy));
      const dp = v.pitch - this.lastLook.pitch;
      this.sway.x = THREE.MathUtils.clamp(this.sway.x + dy * 0.35, -0.05, 0.05);
      this.sway.y = THREE.MathUtils.clamp(this.sway.y - dp * 0.35, -0.04, 0.04);
    }
    this.lastLook = { yaw: v.yaw, pitch: v.pitch };
    const k = Math.exp(-9 * dt);
    this.sway.multiplyScalar(k);

    const r = hb.raise * hb.raise * (3 - 2 * hb.raise);
    const at = this._p.copy(rig.rest.at).lerp(rig.up.at, r);
    const dir = new THREE.Vector3().copy(rig.rest.dir).normalize().lerp(rig.up.dir.clone().normalize(), r).normalize();
    const reach = new THREE.Vector3().copy(rig.rest.reach).lerp(rig.up.reach, r).normalize();

    // walking bob, the sway, the buzz
    const lo = hb.lower * hb.lower;
    at.x += Math.cos(v.bob) * 0.008 * v.move * (1 - r) + this.sway.x * 0.4;
    at.y += -Math.abs(Math.sin(v.bob)) * 0.01 * v.move + this.sway.y * 0.4 - lo * 0.3;
    at.z += lo * 0.04;
    if (hb.buzz > 0) {
      at.x += Math.sin(t * 1.1) * 0.012 * hb.buzz;
      at.y += Math.sin(t * 1.7 + 1) * 0.008 * hb.buzz;
    }

    // An item that points along its +z (the revolver, the cigarette) is
    // turned with its top kept up. A shortest-arc turn from +z to "into the
    // screen" is a half turn about whatever axis is handy, and it rolled
    // the revolver onto its side.
    const q = rig.axis.z === 1 ? this.lookQuat(dir, this._q) : this._q.setFromUnitVectors(rig.axis, dir);
    if (item === 'gun') {
      // the kick: muzzle up, the gun back into the paw; a reload rolls it
      // over and spins the cylinder
      const rk = hb.recoil * hb.recoil;
      const rl = Math.sin(Math.min(1, hb.reloadK) * Math.PI);
      at.z += rk * 0.035;
      at.y += rk * 0.012 - rl * 0.035;
      at.x -= rl * 0.02;
      this._q2.setFromEuler(this._e.set(-rk * 0.5 + rl * 0.25, 0, rl * 1.1));
      q.multiply(this._q2);
      const spinGoal = hb.reload > 0 ? hb.spin + hb.reloadK * 12 : hb.spin;
      this.spinShown += (spinGoal - this.spinShown) * Math.min(1, dt * 18);
      this.cylinder.rotation.z = (this.spinShown * Math.PI) / 3;
    }
    rig.obj.position.copy(at);
    rig.obj.quaternion.copy(q);
    this.arm.position.copy(at);
    this.arm.lookAt(at.x + reach.x, at.y + reach.y, at.z + reach.z);

    if (item === 'cig') {
      const len = 0.075 * Math.max(0.05, (hb.cig - 0.2) / 0.8);
      this.paper.scale.z = len;
      this.ash.position.z = 0.007 + len + 0.002;
      this.ember.position.z = 0.007 + len + 0.0065;
      const drag = hb.using && hb.raise > 0.6 ? 1 : 0;
      this.emberMat.emissiveIntensity = 1.3 + drag * 3.4 + Math.sin(t * 11) * 0.2;
      this.ember.getWorldPosition(this.glow.position);
      this.glow.color.set('#ff8a3c');
      this.glow.intensity = 0.0025 + drag * 0.006;
    } else if (item === 'cocktail' && hb.cocktail) {
      const c = hb.cocktail;
      if (c.id !== this.servedId) {
        this.servedId = c.id;
        this.drinkMat.color.set(c.color);
        this.drinkMat.emissive.set(c.color);
        for (const [k, v] of this.glasses) v.obj.visible = k === c.glass;
        for (const [k, v] of this.garnishes) {
          v.visible = k === c.garnish;
          if (k !== c.garnish) continue;
          // it goes on this glass's rim (and moves with the glass)
          this.glasses.get(c.glass)!.obj.add(v);
          const rim = c.glass === 'rocks' ? { y: 0.04, r: 0.044 } : c.glass === 'coupe' ? { y: 0.05, r: 0.056 } : { y: 0.08, r: 0.058 };
          if (k === 'peel') v.position.set(rim.r, rim.y, 0);
          else if (k === 'lime') v.position.set(rim.r - 0.008, rim.y, 0);
          else v.position.set(0.02, rim.y - 0.018, 0);
        }
      }
      const g = this.glasses.get(c.glass)!;
      const lv = Math.max(0.001, hb.level.cocktail);
      if (c.glass === 'rocks') g.drink.scale.set(1, lv, 1);
      else g.drink.scale.setScalar(Math.cbrt(lv));
      g.drink.visible = hb.level.cocktail > 0.005;
      this.glow.intensity = 0;
    } else if (item === 'vodka') {
      this.liquid.scale.y = Math.max(0.001, hb.level.vodka);
      this.liquid.visible = hb.level.vodka > 0.005;
      this.glow.intensity = 0;
    } else if (item === 'gun') {
      this.muzzle.getWorldPosition(this.glow.position);
      this.glow.color.set('#ffc06a');
      this.glow.intensity = this.flashT > 0 ? 0.08 : 0;
    } else {
      this.glow.intensity = 0;
    }
  }

  /** +z along `dir`, +y as near world up as it gets. */
  private lookQuat(dir: THREE.Vector3, out: THREE.Quaternion): THREE.Quaternion {
    const x = this._bx.crossVectors(UP, dir).normalize();
    const y = this._by.crossVectors(dir, x);
    return out.setFromRotationMatrix(this._bm.makeBasis(x, y, dir));
  }

  /** The gun went off: show the flash for a couple of frames. */
  fired(): void {
    this.flashT = 0.06;
    this.flash.rotation.z = Math.random() * Math.PI;
    const s = 0.85 + Math.random() * 0.4;
    this.flash.scale.setScalar(s);
  }

  /** Where a point of the paw's scene sits in the hall: the same spot on
   *  screen, as far from the eye. (The two lenses differ, so a straight
   *  transform would put the smoke off the end of the cigarette.) */
  private toHall(local: THREE.Vector3, cam: THREE.PerspectiveCamera, out: THREE.Vector3): THREE.Vector3 {
    const dist = local.length();
    const ndc = this._p.copy(local).project(this.camera);
    out.set(ndc.x, ndc.y, 0.5).unproject(cam).sub(cam.position).normalize();
    return out.multiplyScalar(dist).add(cam.position);
  }

  emberInHall(cam: THREE.PerspectiveCamera, out: THREE.Vector3): THREE.Vector3 {
    const p = this.ember.getWorldPosition(new THREE.Vector3());
    return this.toHall(p, cam, out);
  }

  muzzleInHall(cam: THREE.PerspectiveCamera, out: THREE.Vector3): THREE.Vector3 {
    const p = this.muzzle.getWorldPosition(new THREE.Vector3());
    return this.toHall(p, cam, out);
  }

  /** Just below the eye, where an exhale comes out. */
  mouthInHall(cam: THREE.PerspectiveCamera, out: THREE.Vector3): THREE.Vector3 {
    return this.toHall(V(0, -0.1, -0.3), cam, out);
  }

  dispose(): void {
    for (const o of this.owned) o.dispose();
    this.owned = [];
  }
}
