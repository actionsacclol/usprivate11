import * as THREE from 'three';
import { CHIP_VALUES, type BlackjackSession, type BjView } from '../../casino/sessions';
import { chipsText } from '../../casino/ui';
import { BJ_HAND_X } from '../floor';
import { BJ_TABLES } from '../layout';
import { ChipStacks, Plaque, TableRig, chipMesh, ringMesh, type TableView, type WorldLabel } from './common';

// Blackjack, sat at the table (the middle of the arc). The camera looks down
// the felt at the dealer. Your chips fan out on the left: click one to add it
// to your bet in the circle (click the stack in the circle to take one back);
// the plaques fan out on the right — DEAL, HIT, STAND, DOUBLE, SPLIT — lit when
// they do something. The cards land where the dealer deals them; your chips
// sit in front of your hand; a win is paid beside it and slides to you, a
// loss is swept to the dealer. Totals float over the cards.

const FELT_Y = 0.796;
/** The betting circle, in front of your cards. */
const BET_Z = 0.8;
/** A point on the felt, by angle round the table's middle (90° = straight at
 *  you) and radius. */
function arc(tx: number, tz: number, deg: number, r: number): [number, number] {
  const a = (deg * Math.PI) / 180;
  return [tx + Math.cos(a) * r, tz + Math.sin(a) * r];
}

const ACTIONS = [
  { id: 'deal', label: 'DEAL', color: '#22c55e', deg: 76, tip: 'Deal the hand (Space)' },
  { id: 'hit', label: 'HIT', color: '#f59e0b', deg: 62, tip: 'Hit: another card (H)' },
  { id: 'stand', label: 'STAND', color: '#ef4444', deg: 47, tip: 'Stand: keep what you have (Space)' },
  { id: 'double', label: 'DOUBLE', color: '#38bdf8', deg: 32, tip: 'Double: twice the bet, one more card (X)' },
  { id: 'split', label: 'SPLIT', color: '#a855f7', deg: 17, tip: 'Split the pair into two hands (P)' },
] as const;

export class BlackjackRig extends TableRig {
  private tx: number;
  private tz: number;
  private stacks = new ChipStacks();
  private plaques = new Map<string, Plaque>();
  private chips: { v: number; mesh: THREE.Mesh; y: number }[] = [];
  private ring: THREE.Mesh;
  private last: BjView;
  private doneAt = -1;
  private paid = false;
  private swept = false;
  private clock = 0;

  constructor(private s: BlackjackSession) {
    super();
    const [tx, tz] = BJ_TABLES[s.table];
    this.tx = tx;
    this.tz = tz;
    this.last = s.view();
    this.group.add(this.stacks.mesh);
    this.keep(this.stacks);
    // your chips: one of each, fanned on the left
    CHIP_VALUES.forEach((v, i) => {
      const m = chipMesh(v);
      const [x, z] = arc(tx, tz, 150 - i * 15, 0.72);
      const y = FELT_Y + 0.016;
      m.position.set(x, y, z);
      this.group.add(m);
      this.keep(m.geometry);
      this.keep(m.material as THREE.Material);
      this.chips.push({ v, mesh: m, y });
      this.spots.push({
        mesh: m, id: `chip:${v}`, tip: () => `Add ${chipsText(v)} to your bet`,
        on: () => this.s.view().can.bet && this.s.bet + v <= this.s.chips,
      });
    });
    // the plaques, fanned on the right
    for (const a of ACTIONS) {
      const p = new Plaque(a.label, a.color);
      const [x, z] = arc(tx, tz, a.deg, 0.71);
      p.place(x, FELT_Y + 0.007, z, Math.PI / 2 - (a.deg * Math.PI) / 180);
      this.group.add(p.mesh);
      this.keep(p);
      this.plaques.set(a.id, p);
      this.spots.push({ mesh: p.mesh, id: a.id, tip: () => a.tip, on: () => this.can(a.id) });
    }
    // the betting circle: a gold ring; click your stack to take a chip back
    this.ring = ringMesh(0.078, '#F2C66D');
    this.ring.position.set(tx, FELT_Y + 0.002, tz + BET_Z);
    this.group.add(this.ring);
    this.keep(this.ring.geometry);
    this.keep(this.ring.material as THREE.Material);
    const pad = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.09, 0.08, 12), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
    pad.position.set(tx, FELT_Y + 0.04, tz + BET_Z);
    this.group.add(pad);
    this.keep(pad.geometry);
    this.keep(pad.material as THREE.Material);
    this.spots.push({ mesh: pad, id: 'bet', tip: () => (this.s.bet ? `Your bet: ${chipsText(this.s.bet)} — click to take a chip back` : 'Your betting circle'), on: () => this.s.view().can.bet && this.s.bet > 0 });
  }

  private can(id: string): boolean {
    const c = this.last.can;
    return id === 'deal' ? c.deal : id === 'hit' ? c.hit : id === 'stand' ? c.stand : id === 'double' ? c.double : id === 'split' ? c.split : false;
  }

  view(): TableView {
    const { tx, tz } = this;
    return { eye: new THREE.Vector3(tx, 1.5, tz + 1.5), look: new THREE.Vector3(tx, 0.86, tz + 0.12), fov: 50 };
  }

  update(dt: number, t: number): void {
    this.clock += dt;
    const v = this.s.view();
    this.last = v;
    const { tx, tz } = this;
    for (const [id, p] of this.plaques) p.set(this.can(id), this.hovered === id, dt, t);
    for (const c of this.chips) {
      const hot = this.hovered === `chip:${c.v}`;
      c.mesh.position.y += ((c.y + (hot ? 0.025 : 0)) - c.mesh.position.y) * Math.min(1, dt * 14);
      (c.mesh.material as THREE.MeshStandardMaterial).emissiveIntensity = hot ? 0.35 : 0;
    }
    (this.ring.material as THREE.MeshBasicMaterial).opacity = v.can.bet ? 0.6 + Math.sin(t * 4) * 0.25 : 0.35;

    // the chips on the felt
    const hz = tz + BET_Z;
    if (v.phase === 'bet' || (v.phase === 'done' && this.swept && this.clock - this.doneAt > 2.2)) {
      this.stacks.set(this.s.bet ? [{ key: 'bet', x: tx, y: FELT_Y, z: hz, amount: this.s.bet }] : []);
      if (v.phase === 'bet') { this.doneAt = -1; this.paid = this.swept = false; }
    } else if (v.phase !== 'done') {
      this.doneAt = -1;
      this.paid = this.swept = false;
      this.stacks.set(v.hands.map((h, k) => ({ key: `hand${k}`, x: tx + BJ_HAND_X(v.hands.length, k), y: FELT_Y, z: hz, amount: h.bet })));
    } else {
      // settled: pay the winners beside their stake, then everything goes
      if (this.doneAt < 0) this.doneAt = this.clock;
      const since = this.clock - this.doneAt;
      if (!this.paid) {
        this.paid = true;
        const keep = v.hands.map((h, k) => ({ key: `hand${k}`, x: tx + BJ_HAND_X(v.hands.length, k), y: FELT_Y, z: hz, amount: h.bet }));
        const pays = v.hands.flatMap((h, k) => {
          const won = h.result === 'blackjack' ? h.bet * 1.5 : h.result === 'win' ? h.bet : 0;
          return won > 0 ? [{ key: `pay${k}`, x: tx + BJ_HAND_X(v.hands.length, k) + 0.085, y: FELT_Y, z: hz, amount: won }] : [];
        });
        this.stacks.set([...keep, ...pays]);
      } else if (!this.swept && since > 1.1) {
        this.swept = true;
        v.hands.forEach((h, k) => {
          const toYou = h.result === 'win' || h.result === 'blackjack' || h.result === 'push';
          const x = tx + BJ_HAND_X(v.hands.length, k);
          this.stacks.slide(`hand${k}`, x, toYou ? tz + 1.15 : tz - 0.05);
          this.stacks.slide(`pay${k}`, x, tz + 1.15);
        });
        this.stacks.set([]);
      }
    }
    this.stacks.update(dt);
  }

  labels(): WorldLabel[] {
    const v = this.last;
    const { tx, tz } = this;
    const out: WorldLabel[] = [];
    const y = FELT_Y + 0.05;
    if (v.dealer.total !== null) {
      out.push({ at: new THREE.Vector3(tx + 0.3, y, tz + 0.37), text: v.dealer.total > 21 ? 'BUST' : String(v.dealer.total), sub: 'DEALER', color: v.dealer.total > 21 ? '#F87171' : '#E5E7EB', kind: 'tag' });
    }
    v.hands.forEach((h, k) => {
      if (!h.cards.length) return;
      const at = new THREE.Vector3(tx + BJ_HAND_X(v.hands.length, k) + 0.16, y, tz + 0.6);
      let text = h.soft && h.total < 21 ? `soft ${h.total}` : String(h.total);
      let color = h.active ? '#FBBF24' : '#ffffff';
      if (h.total > 21) { text = 'BUST'; color = '#F87171'; }
      if (h.result) {
        const r = h.result;
        const net = r === 'blackjack' ? h.bet * 1.5 : r === 'win' ? h.bet : r === 'push' ? 0 : -h.bet;
        text = r === 'blackjack' ? 'BLACKJACK' : r.toUpperCase();
        color = net > 0 ? '#4ADE80' : net === 0 ? '#E5E7EB' : '#F87171';
        out.push({ at: at.clone().setY(y + 0.02), text, sub: net ? `${net > 0 ? '+' : '−'}${chipsText(Math.abs(net))}` : 'your bet back', color, kind: 'tag' });
        return;
      }
      out.push({ at, text, sub: h.doubled ? 'DOUBLED' : (h.active || v.hands.length === 1) && v.tip ? `strategy: ${v.tip}` : undefined, color, kind: 'tag' });
    });
    if ((v.phase === 'bet' || v.phase === 'done') && this.s.bet > 0) {
      out.push({ at: new THREE.Vector3(tx, y, tz + BET_Z), text: chipsText(this.s.bet), sub: 'BET', color: '#FDE68A', kind: 'tag' });
    }
    if (v.msg) out.push({ at: new THREE.Vector3(tx, 1.0, tz + 0.45), text: v.msg.text, color: v.msg.color, kind: 'big', age: v.msg.age });
    return out;
  }
}
