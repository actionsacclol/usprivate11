import * as THREE from 'three';
import type { SportsbookSession } from '../../casino/sessions';
import { PROP } from '../layout';
import { TableRig, Terminal, type Hit, type TableView, type WorldLabel } from './common';

// A betting station: three monitors on an arm, angled round you — the
// markets on the left (click one), the slip in the middle (pick a side, the
// stake, PLACE TICKET), your bets on the right with each open ticket marked
// to the live price. At either betting window, or at the kiosk under the
// monitor wall.

export type StationPlace = 'sbA' | 'sbB' | 'sbW';

const MW = 0.62;
const MH = MW * (640 / 1024);
const GAP = 0.04;
const TURN = 0.42;

export class SportsbookRig extends TableRig {
  private screens: Terminal[];
  private holder = new THREE.Group();
  private eye: THREE.Vector3;
  private look: THREE.Vector3;

  constructor(private s: SportsbookSession, place: StationPlace) {
    super();
    // where the station stands, and which way its screens face (toward you)
    let at: THREE.Vector3, yaw: number;
    if (place === 'sbW') {
      at = new THREE.Vector3(-9.3, 1.3, -7.82);
      yaw = 0;
    } else {
      const wz = place === 'sbA' ? PROP.windowA[1] : PROP.windowB[1];
      at = new THREE.Vector3(-12.68, 1.3, wz);
      yaw = Math.PI / 2;
    }
    this.holder.position.copy(at);
    this.holder.rotation.y = yaw;
    this.group.add(this.holder);
    const make = (paint: (g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; t: number }) => ReturnType<SportsbookSession['paintSlip']>) =>
      this.keep(new Terminal(1024, 640, MW, paint, '#080512'));
    const list = make((g, v) => this.s.paintList(g, v));
    const slip = make((g, v) => this.s.paintSlip(g, v));
    const bets = make((g, v) => this.s.paintBetsScreen(g, v));
    this.screens = [list, slip, bets];
    // the arm: a bar behind the screens on a post
    const dark = this.keep(new THREE.MeshStandardMaterial({ color: '#141218', roughness: 0.45, metalness: 0.5 }));
    const post = new THREE.Mesh(this.keep(new THREE.CylinderGeometry(0.025, 0.035, at.y - (place === 'sbW' ? 0 : 1.0), 8)), dark);
    post.position.set(0, -(at.y - (place === 'sbW' ? 0 : 1.0)) / 2, -0.09);
    this.holder.add(post);
    if (place === 'sbW') {
      const foot = new THREE.Mesh(this.keep(new THREE.BoxGeometry(0.5, 0.05, 0.42)), dark);
      foot.position.set(0, -at.y + 0.025, -0.09);
      this.holder.add(foot);
    }
    const bezel = this.keep(new THREE.BoxGeometry(MW + 0.03, MH + 0.03, 0.025));
    [-1, 0, 1].forEach((k, i) => {
      const g = new THREE.Group();
      // the side screens swing in toward you, like a trading desk
      const off = k * (MW + GAP) * Math.cos(TURN * Math.abs(k));
      g.position.set(off, 0, Math.abs(k) * (MW / 2) * Math.sin(TURN));
      g.rotation.y = -k * TURN;
      g.rotation.x = -0.08;
      const b = new THREE.Mesh(bezel, dark);
      b.position.z = -0.016;
      g.add(b, this.screens[i].mesh);
      this.holder.add(g);
    });
    this.holder.updateMatrixWorld(true);
    this.eye = this.holder.localToWorld(new THREE.Vector3(0, 0.18, 1.5));
    this.look = this.holder.localToWorld(new THREE.Vector3(0, -0.03, 0.05));
  }

  view(): TableView {
    return { eye: this.eye.clone(), look: this.look.clone(), fov: 52 };
  }

  pick(ray: THREE.Raycaster): Hit | null {
    for (const sc of this.screens) {
      const hit = ray.intersectObject(sc.mesh, false)[0];
      if (!hit?.uv) continue;
      const b = sc.hit(hit.uv);
      return b ? { id: b.id, tip: '' } : null;
    }
    return null;
  }

  update(dt: number, t: number): void {
    for (const sc of this.screens) sc.update(dt, t);
  }

  labels(): WorldLabel[] {
    return [];
  }
}
