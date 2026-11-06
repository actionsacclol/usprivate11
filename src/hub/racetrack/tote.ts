import * as THREE from 'three';
import { F_MONO, F_SANS } from '../engine/overlay';
import { TOTE_H, TOTE_POS, TOTE_W, TOTE_Y } from './track';

// The tote board in the infield: the old orrery's core readout, now a lamp-
// lit odds board. Everything on it is real — session P&L, bankroll, every
// settlement counted (staged or not), and the field on the track with the
// odds its own price implies (34¢ ≈ 2-1). Drawn into a canvas texture, and
// only redrawn when what it says changes (at most twice a second), so it
// costs nothing on a quiet night.

export interface ToteRow { name: string; price: number | null; color: string }
export interface ToteView {
  pnl: number | null;
  bank: number | null;
  wins: number;
  winUsd: number;
  losses: number;
  lossUsd: number;
  onTrack: number;
  rows: ToteRow[];
}

const CW = 1024;
const CH = Math.round((CW * TOTE_H) / TOTE_W);

const ODDS: [number, number][] = [
  [1, 9], [1, 6], [1, 5], [1, 4], [2, 7], [1, 3], [2, 5], [1, 2], [4, 7], [4, 6], [4, 5], [1, 1], [6, 5], [6, 4], [7, 4],
  [2, 1], [5, 2], [3, 1], [7, 2], [4, 1], [5, 1], [6, 1], [8, 1], [10, 1], [12, 1], [16, 1], [20, 1], [25, 1], [33, 1], [50, 1], [99, 1],
];

/** A price in cents as traditional fractional odds: the board's language. */
export function oddsOf(priceCents: number): string {
  const p = Math.min(99, Math.max(1, priceCents));
  const r = Math.log((100 - p) / p);
  let best = ODDS[0];
  let bd = Infinity;
  for (const o of ODDS) {
    const d = Math.abs(Math.log(o[0] / o[1]) - r);
    if (d < bd) { bd = d; best = o; }
  }
  return best[0] === best[1] ? 'EVS' : `${best[0]}-${best[1]}`;
}

function usd(v: number, sign = false): string {
  const s = sign ? (v > 0 ? '+' : v < 0 ? '−' : '') : v < 0 ? '−' : '';
  return `${s}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export class ToteBoard {
  readonly mesh: THREE.Mesh;
  private canvas = document.createElement('canvas');
  private bg = document.createElement('canvas');
  private g: CanvasRenderingContext2D;
  private tex: THREE.CanvasTexture;
  private key = '';
  private lastDraw = -1;

  constructor() {
    this.canvas.width = this.bg.width = CW;
    this.canvas.height = this.bg.height = CH;
    this.g = this.canvas.getContext('2d')!;
    this.paintBackground();
    this.tex = new THREE.CanvasTexture(this.canvas);
    this.tex.colorSpace = THREE.SRGBColorSpace;
    this.tex.anisotropy = 4;
    this.mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(TOTE_W, TOTE_H),
      new THREE.MeshBasicMaterial({ map: this.tex, toneMapped: false }),
    );
    this.mesh.position.set(TOTE_POS.x, TOTE_Y + TOTE_H / 2, TOTE_POS.z);
    this.draw({ pnl: null, bank: null, wins: 0, winUsd: 0, losses: 0, lossUsd: 0, onTrack: 0, rows: [] });
  }

  /** The unlit bulb matrix behind the numbers: painted once. */
  private paintBackground(): void {
    const g = this.bg.getContext('2d')!;
    const grd = g.createLinearGradient(0, 0, 0, CH);
    grd.addColorStop(0, '#0A0D16');
    grd.addColorStop(1, '#05070C');
    g.fillStyle = grd;
    g.fillRect(0, 0, CW, CH);
    g.fillStyle = 'rgba(253,230,138,0.05)';
    for (let y = 6; y < CH; y += 9) for (let x = 6; x < CW; x += 9) { g.beginPath(); g.arc(x, y, 1.6, 0, Math.PI * 2); g.fill(); }
    g.strokeStyle = 'rgba(253,230,138,0.35)';
    g.lineWidth = 3;
    g.strokeRect(6, 6, CW - 12, CH - 12);
    g.strokeStyle = 'rgba(253,230,138,0.14)';
    g.beginPath();
    g.moveTo(CW * 0.42, 22);
    g.lineTo(CW * 0.42, CH - 22);
    g.stroke();
  }

  update(v: ToteView, t: number): void {
    if (t - this.lastDraw < 0.5) return;
    const key = JSON.stringify(v);
    if (key === this.key) return;
    this.draw(v);
    this.key = key;
    this.lastDraw = t;
  }

  private draw(v: ToteView): void {
    const g = this.g;
    g.drawImage(this.bg, 0, 0);
    g.textBaseline = 'alphabetic';
    const L = 36;
    // ── left: the money ───────────────────────────────────────────────
    g.textAlign = 'left';
    g.font = `600 22px ${F_MONO}`;
    g.fillStyle = 'rgba(253,230,138,0.8)';
    g.fillText('SESSION  P&L', L, 58);
    const pc = v.pnl === null ? '#94A3B8' : v.pnl >= 0 ? '#4ADE80' : '#F87171';
    g.font = `700 74px ${F_SANS}`;
    g.fillStyle = pc;
    g.shadowColor = pc;
    g.shadowBlur = 18;
    g.fillText(v.pnl === null ? '—' : usd(v.pnl, true), L, 136);
    g.shadowBlur = 0;
    g.font = `500 22px ${F_MONO}`;
    g.fillStyle = 'rgba(226,232,240,0.75)';
    g.fillText(v.bank === null ? 'BANKROLL  —' : `BANKROLL  $${v.bank.toLocaleString('en-US', { maximumFractionDigits: 0 })}`, L, 178);
    g.font = `700 30px ${F_MONO}`;
    g.fillStyle = '#86EFAC';
    g.fillText(`WON   ${v.wins}`, L, 250);
    g.fillStyle = '#FCA5A5';
    g.fillText(`LOST  ${v.losses}`, L, 300);
    g.font = `600 24px ${F_MONO}`;
    g.textAlign = 'right';
    g.fillStyle = 'rgba(134,239,172,0.85)';
    g.fillText(usd(v.winUsd, true), CW * 0.42 - 28, 250);
    g.fillStyle = 'rgba(252,165,165,0.85)';
    g.fillText(usd(v.lossUsd, true), CW * 0.42 - 28, 300);

    // ── right: the field and its odds ───────────────────────────────
    const R0 = CW * 0.42 + 34, R1 = CW - 36;
    g.textAlign = 'left';
    g.font = `600 22px ${F_MONO}`;
    g.fillStyle = 'rgba(253,230,138,0.8)';
    g.fillText(`ON TRACK  ${v.onTrack}`, R0, 58);
    g.textAlign = 'right';
    g.fillText('PRICE   ODDS', R1, 58);
    if (!v.rows.length) {
      g.textAlign = 'left';
      g.font = `500 22px ${F_MONO}`;
      g.fillStyle = 'rgba(226,232,240,0.45)';
      g.fillText('No runners yet: the field', R0, 132);
      g.fillText('fills as the bot trades.', R0, 164);
    }
    let y = 104;
    for (const r of v.rows.slice(0, 6)) {
      g.fillStyle = r.color;
      g.fillRect(R0, y - 20, 10, 24);
      g.textAlign = 'left';
      g.font = `600 26px ${F_MONO}`;
      g.fillStyle = '#F8FAFC';
      g.fillText(r.name.toUpperCase().slice(0, 16), R0 + 24, y);
      g.textAlign = 'right';
      g.fillStyle = 'rgba(226,232,240,0.7)';
      g.fillText(r.price === null ? '—' : `${r.price}¢`, R1 - 112, y);
      g.font = `700 28px ${F_MONO}`;
      g.fillStyle = '#FDE68A';
      g.shadowColor = '#FBBF24';
      g.shadowBlur = 10;
      g.fillText(r.price === null ? '—' : oddsOf(r.price), R1, y);
      g.shadowBlur = 0;
      y += 42;
    }
    this.tex.needsUpdate = true;
  }

  dispose(): void {
    this.tex.dispose();
    this.mesh.geometry.dispose();
    (this.mesh.material as THREE.Material).dispose();
  }
}
