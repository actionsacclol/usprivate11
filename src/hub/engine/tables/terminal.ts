import * as THREE from 'three';
import type { PaintedSession } from '../../casino/sessions';
import { CLUB_BAR, PROP, type GameSpot } from '../layout';
import { TableRig, Terminal, type Hit, type TableView, type WorldLabel } from './common';

// The places you play through a screen in the world: the sportsbook's
// betting terminal (on the window's counter, or on a kiosk under the big
// board), the cashier's ledger on the cage counter (with a brass bell to ring
// for a refill), the bar's menu card on the counter. The session paints the
// screen; a click on it is one of the buttons it painted.

export type TerminalPlace = 'sbA' | 'sbB' | 'sbW' | 'cgA' | 'cgB' | 'bar0' | 'bar1' | 'hb0' | 'hb1';

interface Placement {
  at: THREE.Vector3;
  /** which way the screen faces */
  yaw: number;
  /** tipped back by this much */
  tilt: number;
  width: number;
  eye: THREE.Vector3;
  look: THREE.Vector3;
  fov: number;
  /** stands on a pedestal (the kiosk) or a little stand on a counter */
  stand: 'kiosk' | 'desk' | 'tent';
}

function placement(spot: GameSpot): Placement {
  const id = spot.id as TerminalPlace;
  const z = spot.z;
  const V = (x: number, y: number, zz: number) => new THREE.Vector3(x, y, zz);
  switch (id) {
    case 'hb0': case 'hb1': {
      // on the round bar's counter, between you and the well
      const [bx, bz] = PROP.bar;
      const nx = spot.x - bx, nz = spot.z - bz, d = Math.hypot(nx, nz) || 1;
      const ux = nx / d, uz = nz / d;
      return {
        at: V(bx + ux * 1.12, 1.24, bz + uz * 1.12), yaw: Math.atan2(ux, uz), tilt: 0.3, width: 0.66,
        eye: V(bx + ux * 1.85, 1.46, bz + uz * 1.85), look: V(bx + ux * 1.12, 1.2, bz + uz * 1.12), fov: 44, stand: 'tent',
      };
    }
    case 'sbA': case 'sbB': {
      const wz = id === 'sbA' ? PROP.windowA[1] : PROP.windowB[1];
      return { at: V(-12.66, 1.27, wz), yaw: Math.PI / 2, tilt: 0.28, width: 0.7, eye: V(-11.98, 1.47, wz), look: V(-12.66, 1.27, wz), fov: 36, stand: 'desk' };
    }
    case 'sbW':
      return { at: V(-9.3, 1.28, -7.78), yaw: 0, tilt: 0.3, width: 0.7, eye: V(-9.3, 1.5, -7.1), look: V(-9.3, 1.29, -7.8), fov: 36, stand: 'kiosk' };
    case 'cgA': case 'cgB': {
      const x = id === 'cgA' ? 7.6 : 10.4;
      return { at: V(x, 1.24, -6.32), yaw: 0, tilt: 0.3, width: 0.66, eye: V(x + 0.06, 1.47, -5.55), look: V(x + 0.06, 1.17, -6.35), fov: 44, stand: 'desk' };
    }
    default:
      return { at: V(CLUB_BAR.x + 0.2, 1.3, z), yaw: -Math.PI / 2, tilt: 0.3, width: 0.66, eye: V(CLUB_BAR.x - 0.48, 1.48, z), look: V(CLUB_BAR.x + 0.2, 1.3, z), fov: 38, stand: 'tent' };
  }
}

export class TerminalRig extends TableRig {
  private term: Terminal;
  private p: Placement;
  private bell: THREE.Mesh | null = null;
  private ring = 0;

  constructor(private s: PaintedSession, spot: GameSpot) {
    super();
    const place = spot.id as TerminalPlace;
    this.p = placement(spot);
    const p = this.p;
    const holder = new THREE.Group();
    holder.position.copy(p.at);
    holder.rotation.set(0, p.yaw, 0, 'YXZ');
    this.group.add(holder);
    this.term = this.keep(new Terminal(1024, 470, p.width, (g, v) => this.s.paint(g, v)));
    const tilt = new THREE.Group();
    tilt.rotation.x = -p.tilt;
    holder.add(tilt);
    tilt.add(this.term.mesh);
    const h = (p.width * 470) / 1024;
    const dark = this.keep(new THREE.MeshStandardMaterial({ color: '#141218', roughness: 0.5, metalness: 0.4 }));
    if (p.stand === 'tent') {
      // a menu card: a gold-edged frame on a little easel
      const frame = new THREE.Mesh(this.keep(new THREE.BoxGeometry(p.width + 0.04, h + 0.04, 0.012)), this.keep(new THREE.MeshStandardMaterial({ color: '#C79A45', metalness: 0.8, roughness: 0.3 })));
      frame.position.z = -0.008;
      tilt.add(frame);
    } else {
      const body = new THREE.Mesh(this.keep(new THREE.BoxGeometry(p.width + 0.05, h + 0.05, 0.05)), dark);
      body.position.z = -0.03;
      tilt.add(body);
      const neck = new THREE.Mesh(this.keep(new THREE.BoxGeometry(0.06, p.stand === 'kiosk' ? p.at.y : 0.25, 0.06)), dark);
      neck.position.set(0, p.stand === 'kiosk' ? -p.at.y / 2 : -0.18, -0.06);
      holder.add(neck);
      if (p.stand === 'kiosk') {
        const foot = new THREE.Mesh(this.keep(new THREE.BoxGeometry(0.45, 0.05, 0.4)), dark);
        foot.position.set(0, -p.at.y + 0.025, -0.06);
        holder.add(foot);
      }
    }
    // the cashier's bell, on the counter beside the ledger
    if (place === 'cgA' || place === 'cgB') {
      const brass = this.keep(new THREE.MeshStandardMaterial({ color: '#d8b46a', metalness: 0.9, roughness: 0.25, emissive: '#ffd27a', emissiveIntensity: 0 }));
      const bell = new THREE.Mesh(this.keep(new THREE.SphereGeometry(0.05, 14, 8, 0, Math.PI * 2, 0, Math.PI / 2)), brass);
      const base = new THREE.Mesh(this.keep(new THREE.CylinderGeometry(0.06, 0.065, 0.015, 14)), dark);
      bell.position.set(p.at.x + 0.42, p.at.y - 0.2, p.at.z + 0.05);
      base.position.set(p.at.x + 0.42, p.at.y - 0.208, p.at.z + 0.05);
      this.group.add(bell, base);
      this.bell = bell;
      this.spots.push({ mesh: bell, id: 'refill', tip: () => 'Ring for a refill (under 50 chips)', on: () => true });
    }
  }

  view(): TableView {
    return { eye: this.p.eye.clone(), look: this.p.look.clone(), fov: this.p.fov };
  }

  pick(ray: THREE.Raycaster): Hit | null {
    const spot = super.pick(ray);
    if (spot) return spot;
    const hit = ray.intersectObject(this.term.mesh, false)[0];
    if (!hit?.uv) return null;
    const b = this.term.hit(hit.uv);
    return b ? { id: b.id, tip: '' } : null;
  }

  /** The bell rings (it's clicked): a flash. */
  rang(): void {
    this.ring = 1;
  }

  update(dt: number, t: number): void {
    this.term.update(dt, t);
    if (this.bell) {
      this.ring = Math.max(0, this.ring - dt * 2);
      const m = this.bell.material as THREE.MeshStandardMaterial;
      m.emissiveIntensity = this.ring * 1.5 + (this.hovered === 'refill' ? 0.5 : 0);
      this.bell.rotation.z = Math.sin(t * 40) * 0.15 * this.ring;
    }
  }

  labels(): WorldLabel[] {
    return [];
  }
}
