import * as THREE from 'three';
import { THREE_PAYS, TWO_CHERRIES, drawSymbol } from '../../casino/slots';
import type { SlotsSession } from '../../casino/sessions';
import { chipsText } from '../../casino/ui';
import { canvasTex } from '../casinoTextures';
import type { CasinoFloor } from '../floor';
import { TableRig, type TableView, type WorldLabel } from './common';

// A slot machine, up close: the reels fill the view. Over them, the
// machine's own meter — CREDITS, BET, WIN (a win counts up) — and its pay
// table; on the deck, its buttons (BET −, BET +, SPIN); at its side, the
// lever you pull. All in the machine's own frame: local +z faces you.

function buttonTex(label: string, color: string): THREE.CanvasTexture {
  return canvasTex(128, 128, (g, w, h) => {
    g.fillStyle = color;
    g.beginPath(); g.arc(w / 2, h / 2, w / 2, 0, Math.PI * 2); g.fill();
    g.strokeStyle = 'rgba(255,255,255,0.75)';
    g.lineWidth = 6;
    g.beginPath(); g.arc(w / 2, h / 2, w / 2 - 6, 0, Math.PI * 2); g.stroke();
    g.fillStyle = '#ffffff';
    g.font = `800 ${label.length > 3 ? 30 : 36}px "Chakra Petch", sans-serif`;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(label, w / 2, h / 2 + 2);
  });
}

export class SlotRig extends TableRig {
  private frame: THREE.Matrix4;
  private meter: { canvas: HTMLCanvasElement; g: CanvasRenderingContext2D; tex: THREE.CanvasTexture; acc: number };
  private buttons: { id: string; mesh: THREE.Mesh; mat: THREE.MeshStandardMaterial; y: number }[] = [];
  private shownWin = 0;

  constructor(private s: SlotsSession, floor: CasinoFloor) {
    super();
    this.frame = floor.slotFrame(s.machine);
    this.group.matrixAutoUpdate = false;
    this.group.matrix.copy(this.frame);
    this.group.matrixWorldNeedsUpdate = true;
    // the meter and pay table, over the reels
    const canvas = document.createElement('canvas');
    canvas.width = 512; canvas.height = 176;
    const tex = new THREE.CanvasTexture(canvas);
    tex.colorSpace = THREE.SRGBColorSpace;
    this.meter = { canvas, g: canvas.getContext('2d')!, tex, acc: 1 };
    this.keep(tex);
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.54, 0.186), new THREE.MeshBasicMaterial({ map: tex, toneMapped: false }));
    screen.position.set(0, 1.37, 0.153);
    this.group.add(screen);
    this.keep(screen.geometry);
    this.keep(screen.material as THREE.Material);
    // the buttons, on the tilted deck
    for (const [id, label, color, x, tip] of [
      ['less', 'BET−', '#b45309', -0.15, 'Bet less (←)'],
      ['more', 'BET+', '#b45309', 0, 'Bet more (→)'],
      ['spin', 'SPIN', '#15803d', 0.15, 'Spin (Space)'],
    ] as const) {
      const t = this.keep(buttonTex(label, color));
      const mat = new THREE.MeshStandardMaterial({ map: t, emissive: '#ffffff', emissiveMap: t, emissiveIntensity: 0.5, roughness: 0.35 });
      const side = new THREE.MeshStandardMaterial({ color, roughness: 0.4 });
      this.keep(mat); this.keep(side);
      const m = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.045, 0.024, 18), [side, mat, side]);
      this.keep(m.geometry);
      const y = 0.896;
      m.position.set(x, y, 0.252);
      m.rotation.x = -0.25;
      this.group.add(m);
      this.buttons.push({ id, mesh: m, mat, y });
      this.spots.push({ mesh: m, id, tip: () => tip, on: () => !this.s.spinning });
    }
    // the lever, at the machine's side
    const pull = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.62, 0.2), new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }));
    pull.position.set(0.36, 1.22, 0.05);
    this.group.add(pull);
    this.keep(pull.geometry);
    this.keep(pull.material as THREE.Material);
    this.spots.push({ mesh: pull, id: 'lever', tip: () => 'Pull the lever', on: () => !this.s.spinning && this.s.chips >= this.s.bet });
  }

  private local(x: number, y: number, z: number): THREE.Vector3 {
    return new THREE.Vector3(x, y, z).applyMatrix4(this.frame);
  }

  view(): TableView {
    return { eye: this.local(0, 1.27, 0.95), look: this.local(0, 1.12, 0.12), fov: 50 };
  }

  update(dt: number, t: number): void {
    for (const b of this.buttons) {
      const hot = this.hovered === b.id && !this.s.spinning;
      b.mat.emissiveIntensity = this.s.spinning ? 0.15 : hot ? 1.4 : b.id === 'spin' ? 0.8 + Math.sin(t * 4) * 0.2 : 0.5;
      b.mesh.position.y = b.y - (hot ? 0.004 : 0);
    }
    // the win meter counts up
    const w = this.s.win;
    this.shownWin = w ? Math.min(w.amount, this.shownWin + Math.max(1, w.amount * dt * 1.5)) : 0;
    const m = this.meter;
    m.acc += dt;
    if (m.acc < 1 / 15) return;
    m.acc = 0;
    this.paintMeter(m.g, t);
    m.tex.needsUpdate = true;
  }

  private paintMeter(g: CanvasRenderingContext2D, t: number): void {
    const W = 512, H = 176;
    g.fillStyle = '#05040a';
    g.fillRect(0, 0, W, H);
    g.strokeStyle = '#b45309';
    g.lineWidth = 4;
    g.strokeRect(2, 2, W - 4, H - 4);
    const cell = (label: string, value: string, x: number, color: string) => {
      g.fillStyle = 'rgba(255,255,255,0.55)';
      g.font = '700 16px "Chakra Petch", sans-serif';
      g.textAlign = 'center';
      g.fillText(label, x, 30);
      g.fillStyle = color;
      g.font = '800 34px "JetBrains Mono", monospace';
      g.fillText(value, x, 68);
    };
    cell('CREDITS', chipsText(this.s.chips), 70, '#FDE68A');
    cell('BET', chipsText(this.s.bet), 170, '#FBBF24');
    const winning = this.s.win !== null;
    cell('WIN', winning ? chipsText(Math.floor(this.shownWin)) : '0', 270, winning && Math.sin(t * 12) > -0.3 ? '#4ADE80' : '#14532d');
    // the banner line
    g.textAlign = 'center';
    g.font = '800 22px "Chakra Petch", sans-serif';
    if (this.s.spinning) {
      g.fillStyle = '#FBBF24';
      g.fillText('GOOD LUCK', 170, 118);
    } else if (winning) {
      g.fillStyle = Math.sin(t * 10) > 0 ? '#4ADE80' : '#FDE68A';
      g.fillText(`WINNER · ${chipsText(this.s.win!.amount)}`, 170, 118);
    } else {
      g.fillStyle = 'rgba(255,255,255,0.5)';
      g.fillText(this.s.chips < this.s.bet ? 'SEE THE CASHIER' : 'PULL OR PRESS SPIN', 170, 118);
    }
    g.fillStyle = 'rgba(255,255,255,0.35)';
    g.font = '600 13px "JetBrains Mono", monospace';
    g.fillText('94% RETURN · PLAY CHIPS', 170, 152);
    // the pay table
    g.fillStyle = '#1a1206';
    g.fillRect(330, 8, 174, 160);
    const pays = (Object.entries(THREE_PAYS) as [keyof typeof THREE_PAYS, number][]).sort((a, c) => c[1] - a[1]);
    pays.forEach(([sym, p], i) => {
      const y = 22 + i * 22;
      for (let k = 0; k < 3; k++) {
        g.save();
        g.translate(350 + k * 22, y);
        drawSymbol(g, sym, 20);
        g.restore();
      }
      g.fillStyle = '#FDE68A';
      g.font = '800 16px "JetBrains Mono", monospace';
      g.textAlign = 'right';
      g.fillText(`${p}×`, 494, y + 6);
    });
    const y = 22 + pays.length * 22;
    for (let k = 0; k < 2; k++) {
      g.save();
      g.translate(350 + k * 22, y);
      drawSymbol(g, 'cherry', 20);
      g.restore();
    }
    g.fillStyle = '#FDE68A';
    g.textAlign = 'right';
    g.fillText(`${TWO_CHERRIES}×`, 494, y + 6);
    g.textAlign = 'left';
  }

  labels(): WorldLabel[] {
    const out: WorldLabel[] = [];
    const w = this.s.win;
    if (w && w.age < 3) out.push({ at: this.local(0, 1.66, 0.2), text: `WIN ${chipsText(w.amount)}`, color: '#4ADE80', kind: 'big', age: w.age });
    const m = this.s.msg;
    if (m && !w && m.age < 2.5 && /chips/i.test(m.text)) out.push({ at: this.local(0, 1.6, 0.2), text: m.text, color: m.color, kind: 'big', age: m.age });
    return out;
  }
}
