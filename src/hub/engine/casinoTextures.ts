import * as THREE from 'three';
import { SUIT_GLYPH, rankLabel, type Card } from '../casino/blackjack';
import { WHEEL_ORDER, colorOf } from '../casino/roulette';
import { STRIP, drawSymbol } from '../casino/slots';
import { americanOdds, closesIn, type BoardRow } from '../casino/sportsbook';

// Every picture the casino hall paints on a canvas: carpets, neon, felts, the
// wheel's real pocket order, the slot strip, a deck of cards, and the live
// sportsbook board. Kept apart from floor.ts so the geometry reads as
// geometry.

export function canvasTex(w: number, h: number, draw: (g: CanvasRenderingContext2D, w: number, h: number) => void, repeat = false): THREE.CanvasTexture {
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  draw(c.getContext('2d')!, w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  if (repeat) t.wrapS = t.wrapT = THREE.RepeatWrapping;
  return t;
}

/** A canvas-backed texture for screens, redrawn at a capped rate: each upload
 *  is a texImage2D, and a wall of boards at 60 Hz is wasted bandwidth. */
export class Screen {
  readonly canvas = document.createElement('canvas');
  readonly ctx: CanvasRenderingContext2D;
  readonly tex: THREE.CanvasTexture;
  private acc = 0;
  constructor(w: number, h: number, private hz: number, private draw: (g: CanvasRenderingContext2D, w: number, h: number, t: number) => void) {
    this.canvas.width = w;
    this.canvas.height = h;
    this.ctx = this.canvas.getContext('2d')!;
    this.tex = new THREE.CanvasTexture(this.canvas);
    this.tex.colorSpace = THREE.SRGBColorSpace;
    this.tex.anisotropy = 4;
    this.draw(this.ctx, w, h, 0);
  }
  update(dt: number, t: number, force = false): void {
    this.acc += dt;
    if (!force && this.acc < 1 / this.hz) return;
    this.acc = 0;
    this.draw(this.ctx, this.canvas.width, this.canvas.height, t);
    this.tex.needsUpdate = true;
  }
}

/** Casino carpet: a soft weave under a fine gold lattice, a rosette in every
 *  diamond. Low contrast on purpose: across the floor it reads as cloth. */
export function carpetTexture(base: string, motif: string, petal: string): THREE.CanvasTexture {
  return canvasTex(256, 256, (g) => {
    g.fillStyle = base;
    g.fillRect(0, 0, 256, 256);
    for (let i = 0; i < 5000; i++) {
      g.fillStyle = Math.random() < 0.6 ? 'rgba(0,0,0,0.12)' : 'rgba(255,255,255,0.035)';
      g.fillRect(Math.random() * 256, Math.random() * 256, 2, 2);
    }
    g.strokeStyle = motif;
    g.globalAlpha = 0.3;
    g.lineWidth = 2;
    g.beginPath();
    for (const o of [-256, -128, 0, 128, 256]) {
      g.moveTo(o, 0); g.lineTo(o + 256, 256);
      g.moveTo(o + 256, 0); g.lineTo(o, 256);
    }
    g.stroke();
    for (let k = 0; k <= 4; k++) for (let m = 0; m <= 4; m++) {
      const x = k * 64, y = m * 64;
      if ((k + m) % 2 === 1) {
        g.globalAlpha = 0.42;
        g.fillStyle = petal;
        for (const [dx, dy] of [[7, 0], [-7, 0], [0, 7], [0, -7]]) {
          g.beginPath(); g.arc(x + dx, y + dy, 4.5, 0, Math.PI * 2); g.fill();
        }
        g.globalAlpha = 0.6;
        g.fillStyle = motif;
        g.beginPath(); g.arc(x, y, 3, 0, Math.PI * 2); g.fill();
      } else {
        g.globalAlpha = 0.45;
        g.fillStyle = motif;
        g.beginPath(); g.arc(x, y, 2.5, 0, Math.PI * 2); g.fill();
      }
    }
  }, true);
}

/** The red runner: gold borders, a diamond chain. Repeats along u. */
export function runnerTexture(): THREE.CanvasTexture {
  return canvasTex(128, 64, (g) => {
    g.fillStyle = '#5b0d18';
    g.fillRect(0, 0, 128, 64);
    for (let i = 0; i < 500; i++) {
      g.fillStyle = Math.random() < 0.6 ? 'rgba(0,0,0,0.18)' : 'rgba(255,200,200,0.05)';
      g.fillRect(Math.random() * 128, Math.random() * 64, 1.5, 1.5);
    }
    g.fillStyle = '#d9a646';
    g.fillRect(0, 3, 128, 3); g.fillRect(0, 58, 128, 3);
    g.fillStyle = '#8f2333';
    g.fillRect(0, 8, 128, 2); g.fillRect(0, 54, 128, 2);
    g.fillStyle = '#d9a646';
    for (const x of [0, 64, 128]) {
      g.beginPath(); g.moveTo(x, 20); g.lineTo(x + 12, 32); g.lineTo(x, 44); g.lineTo(x - 12, 32); g.closePath(); g.fill();
    }
    g.fillStyle = '#e8c070';
    for (const x of [32, 96]) { g.beginPath(); g.arc(x, 32, 3.5, 0, Math.PI * 2); g.fill(); }
  }, true);
}

/** Black-and-cream marble with gold grout. */
export function marbleTexture(): THREE.CanvasTexture {
  return canvasTex(128, 128, (g) => {
    for (let i = 0; i < 2; i++) for (let j = 0; j < 2; j++) {
      const dark = (i + j) % 2 === 0;
      g.fillStyle = dark ? '#17121a' : '#d8cdb8';
      g.fillRect(i * 64, j * 64, 64, 64);
      g.strokeStyle = dark ? 'rgba(255,255,255,0.10)' : 'rgba(80,60,40,0.22)';
      g.lineWidth = 1.2;
      for (let v = 0; v < 3; v++) {
        g.beginPath();
        let x = i * 64 + Math.random() * 64, y = j * 64;
        g.moveTo(x, y);
        for (let s = 0; s < 6; s++) { x += (Math.random() - 0.5) * 18; y += 64 / 6; g.lineTo(x, y); }
        g.stroke();
      }
    }
    g.strokeStyle = '#c9a04a';
    g.lineWidth = 2;
    g.strokeRect(0, 0, 128, 128);
    g.beginPath(); g.moveTo(64, 0); g.lineTo(64, 128); g.moveTo(0, 64); g.lineTo(128, 64); g.stroke();
  }, true);
}

/** Neon lettering on black, for signs that should bloom. */
export function neonText(text: string, color: string, w: number, h: number, sub?: string): THREE.CanvasTexture {
  return canvasTex(w, h, (g) => {
    g.fillStyle = '#07050b';
    g.fillRect(0, 0, w, h);
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    const big = sub ? h * 0.5 : h * 0.62;
    const y = sub ? h * 0.4 : h / 2 + h * 0.04;
    g.font = `700 ${Math.round(big)}px "Chakra Petch", sans-serif`;
    let s = big;
    while (g.measureText(text).width > w * 0.92 && s > 8) {
      s -= 2;
      g.font = `700 ${Math.round(s)}px "Chakra Petch", sans-serif`;
    }
    g.shadowColor = color;
    g.shadowBlur = h * 0.16;
    g.fillStyle = color;
    g.fillText(text, w / 2, y);
    g.shadowBlur = 0;
    g.fillStyle = '#ffffff';
    g.globalAlpha = 0.85;
    g.fillText(text, w / 2, y);
    if (sub) {
      g.globalAlpha = 0.9;
      g.font = `600 ${Math.round(h * 0.2)}px "JetBrains Mono", monospace`;
      g.fillStyle = color;
      g.fillText(sub, w / 2, h * 0.8);
    }
  });
}

/** Abstract tickers for the sportsbook's side screens and the kiosk cube:
 *  bars and blocks, no numbers — nothing on them pretends to be a price. */
export function tickerTexture(): THREE.CanvasTexture {
  return canvasTex(256, 144, (g, w, h) => {
    g.fillStyle = '#0c0718';
    g.fillRect(0, 0, w, h);
    const cols = ['#A855F7', '#EC4899', '#38BDF8', '#F59E0B', '#4ADE80'];
    g.fillStyle = '#C084FC';
    g.fillRect(10, 10, 90, 12);
    for (let i = 0; i < 5; i++) {
      const y = 34 + i * 21;
      g.fillStyle = cols[i % cols.length];
      g.fillRect(10, y, 26, 13);
      g.fillStyle = 'rgba(255,255,255,0.75)';
      g.fillRect(44, y + 2, 60 + Math.random() * 70, 9);
      g.fillStyle = cols[(i + 2) % cols.length];
      g.fillRect(w - 56, y, 44, 13);
    }
    g.strokeStyle = 'rgba(168,85,247,0.6)';
    g.lineWidth = 3;
    g.strokeRect(2, 2, w - 4, h - 4);
  });
}

/** One reel's strip, round the drum: canvas x runs round the circumference
 *  (stop k is centred at x = (k + ½)/22 of the width), each symbol drawn
 *  turned so it reads upright on the drum. */
export function reelStripTexture(): THREE.CanvasTexture {
  const n = STRIP.length;
  const cell = 64;
  return canvasTex(n * cell, 64, (g, w, h) => {
    g.fillStyle = '#fbf6ea';
    g.fillRect(0, 0, w, h);
    for (let i = 0; i < n; i++) {
      const cx = (i + 0.5) * cell;
      g.strokeStyle = 'rgba(0,0,0,0.12)';
      g.beginPath(); g.moveTo(i * cell, 0); g.lineTo(i * cell, h); g.stroke();
      g.save();
      g.translate(cx, h / 2);
      g.rotate(Math.PI / 2);
      drawSymbol(g, STRIP[i], 56);
      g.restore();
    }
  });
}

/** The wheel's face: 37 pockets in the real single-zero order, numbered.
 *  Pocket i of WHEEL_ORDER spans canvas angles [i, i+1]·2π/37, which is the
 *  same angle in the floor's x–z plane (floor.ts lays the face down). */
export function wheelTexture(): THREE.CanvasTexture {
  return canvasTex(512, 512, (g, w) => {
    const c = w / 2;
    g.fillStyle = '#2b1a10';
    g.fillRect(0, 0, w, w);
    const n = WHEEL_ORDER.length;
    for (let i = 0; i < n; i++) {
      const a0 = (i / n) * Math.PI * 2, a1 = ((i + 1) / n) * Math.PI * 2;
      const col = colorOf(WHEEL_ORDER[i]);
      g.fillStyle = col === 'green' ? '#15803D' : col === 'red' ? '#B91C1C' : '#111111';
      g.beginPath(); g.moveTo(c, c); g.arc(c, c, c - 3, a0, a1); g.closePath(); g.fill();
    }
    g.strokeStyle = '#E8C170';
    g.lineWidth = 3;
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2;
      g.beginPath();
      g.moveTo(c + Math.cos(a) * c * 0.6, c + Math.sin(a) * c * 0.6);
      g.lineTo(c + Math.cos(a) * (c - 3), c + Math.sin(a) * (c - 3));
      g.stroke();
    }
    g.fillStyle = '#ffffff';
    g.font = '700 22px "Chakra Petch", sans-serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    for (let i = 0; i < n; i++) {
      const a = ((i + 0.5) / n) * Math.PI * 2;
      g.save();
      g.translate(c + Math.cos(a) * c * 0.88, c + Math.sin(a) * c * 0.88);
      g.rotate(a + Math.PI / 2);
      g.fillText(String(WHEEL_ORDER[i]), 0, 0);
      g.restore();
    }
    const grd = g.createRadialGradient(c, c, 0, c, c, c * 0.6);
    grd.addColorStop(0, '#8a6a2c');
    grd.addColorStop(1, '#4a2a14');
    g.fillStyle = grd;
    g.beginPath(); g.arc(c, c, c * 0.6, 0, Math.PI * 2); g.fill();
    g.strokeStyle = '#E8C170';
    g.lineWidth = 6;
    g.beginPath(); g.arc(c, c, c * 0.6, 0, Math.PI * 2); g.stroke();
    // the pockets' inner ring
    g.lineWidth = 3;
    g.beginPath(); g.arc(c, c, c * 0.76, 0, Math.PI * 2); g.stroke();
  });
}

/** The roulette layout printed on the felt: zero, the 3×12 grid, dozens and
 *  the even-money boxes. Read from the dealer's side (canvas top = -z). */
/** The roulette layout's cells, in its texture's pixels (1024 x 440): the
 *  zero, the 12 x 3 numbers, the 2:1 column bets at their end, the dozens
 *  and the six even-money boxes below. Immersive play maps a click on the
 *  felt back through these. */
export const RL = { w: 1024, h: 440, gx: 120, gy: 24, gw: 854, gh: 230, colW: 46, dy: 262, dh: 70, ey: 340, eh: 70 } as const;

export function rouletteLayoutTexture(): THREE.CanvasTexture {
  return canvasTex(RL.w, RL.h, (g, w) => {
    g.fillStyle = '#0f5b3d';
    g.fillRect(0, 0, w, RL.h);
    g.strokeStyle = '#f3ead2';
    g.lineWidth = 3;
    const { gx, gy, gw, gh } = RL;
    g.fillStyle = '#15803D';
    g.beginPath(); g.moveTo(gx, gy); g.lineTo(gx - 90, gy + gh / 2); g.lineTo(gx, gy + gh); g.closePath(); g.fill(); g.stroke();
    g.fillStyle = '#ffffff';
    g.font = '700 40px "Chakra Petch", sans-serif';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText('0', gx - 34, gy + gh / 2);
    for (let i = 0; i < 12; i++) for (let j = 0; j < 3; j++) {
      const num = i * 3 + (3 - j);
      const x = gx + (i * gw) / 12, y = gy + (j * gh) / 3;
      g.strokeRect(x, y, gw / 12, gh / 3);
      g.fillStyle = colorOf(num) === 'red' ? '#DC2626' : '#111111';
      g.beginPath(); g.ellipse(x + gw / 24, y + gh / 6, 26, 20, 0, 0, Math.PI * 2); g.fill();
      g.fillStyle = '#ffffff';
      g.font = '700 26px "Chakra Petch", sans-serif';
      g.fillText(String(num), x + gw / 24, y + gh / 6 + 1);
    }
    // the columns: 2 to 1, at the end of each row
    g.font = '700 18px "Chakra Petch", sans-serif';
    for (let j = 0; j < 3; j++) {
      const y = gy + (j * gh) / 3;
      g.strokeRect(gx + gw, y, RL.colW, gh / 3);
      g.save();
      g.translate(gx + gw + RL.colW / 2, y + gh / 6);
      g.rotate(-Math.PI / 2);
      g.fillStyle = '#f3ead2';
      g.fillText('2 TO 1', 0, 1);
      g.restore();
    }
    const dy = RL.dy;
    g.font = '700 30px "Chakra Petch", sans-serif';
    ['1st 12', '2nd 12', '3rd 12'].forEach((t, i) => {
      g.strokeRect(gx + (i * gw) / 3, dy, gw / 3, 70);
      g.fillStyle = '#f3ead2';
      g.fillText(t, gx + ((i + 0.5) * gw) / 3, dy + 36);
    });
    const ey = RL.ey;
    ['1–18', 'EVEN', '', '', 'ODD', '19–36'].forEach((t, i) => {
      const x = gx + (i * gw) / 6;
      g.strokeRect(x, ey, gw / 6, 70);
      if (t) { g.fillStyle = '#f3ead2'; g.fillText(t, x + gw / 12, ey + 36); }
      if (i === 2 || i === 3) {
        g.fillStyle = i === 2 ? '#DC2626' : '#111111';
        const cx = x + gw / 12, cy = ey + 35;
        g.beginPath(); g.moveTo(cx - 40, cy); g.lineTo(cx, cy - 24); g.lineTo(cx + 40, cy); g.lineTo(cx, cy + 24); g.closePath(); g.fill();
      }
    });
  });
}

/** A blackjack table's felt: the arc's lettering and the betting spots, laid
 *  so the dealer reads it upside down and the players read it straight. */
export function blackjackFeltTexture(): THREE.CanvasTexture {
  return canvasTex(512, 512, (g, w, h) => {
    g.fillStyle = '#0f5b3d';
    g.fillRect(0, 0, w, h);
    for (let i = 0; i < 1800; i++) {
      g.fillStyle = Math.random() < 0.5 ? 'rgba(0,0,0,0.07)' : 'rgba(255,255,255,0.03)';
      g.fillRect(Math.random() * w, Math.random() * h, 2, 2);
    }
    // the canvas centre is the flat edge's midpoint; the arc bulges downward
    const cx = w / 2, cy = 0;
    g.strokeStyle = '#E8C170';
    g.lineWidth = 4;
    g.beginPath(); g.arc(cx, cy, w * 0.33, 0, Math.PI); g.stroke();
    g.font = '700 26px "Chakra Petch", sans-serif';
    g.fillStyle = '#F2C66D';
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    const say = (text: string, r: number, size: number, color: string) => {
      g.font = `700 ${size}px "Chakra Petch", sans-serif`;
      g.fillStyle = color;
      const chars = [...text];
      const span = Math.min(Math.PI * 0.75, chars.length * 0.065);
      chars.forEach((ch, i) => {
        const a = Math.PI / 2 + span / 2 - (i / Math.max(1, chars.length - 1)) * span;
        g.save();
        g.translate(cx + Math.cos(a) * r, cy + Math.sin(a) * r);
        g.rotate(a - Math.PI / 2);
        g.fillText(ch, 0, 0);
        g.restore();
      });
    };
    say('BLACKJACK PAYS 3 TO 2', w * 0.23, 24, '#F2C66D');
    say('DEALER STANDS ON ALL 17s', w * 0.29, 16, '#f3ead2');
    // four betting spots, one per seat
    for (const deg of [25, 65, 115, 155]) {
      const a = (deg * Math.PI) / 180;
      const r = w * 0.4;
      g.strokeStyle = '#f3ead2';
      g.lineWidth = 3;
      g.beginPath(); g.arc(cx + Math.cos(a) * r, cy + Math.sin(a) * r, 22, 0, Math.PI * 2); g.stroke();
    }
  });
}

/** A deck of cards in one atlas: 13 columns (A..K) × 4 rows (♠ ♥ ♦ ♣), plus
 *  the back in column 13 of row 0. `cardUV` gives a card's rectangle. */
export const CARD_W = 96;
export const CARD_H = 134;
const SUIT_ROW: Record<Card['suit'], number> = { S: 0, H: 1, D: 2, C: 3 };
export function cardAtlasTexture(): THREE.CanvasTexture {
  return canvasTex(14 * CARD_W, 4 * CARD_H, (g) => {
    for (const suit of ['S', 'H', 'D', 'C'] as Card['suit'][]) {
      for (let rank = 1; rank <= 13; rank++) {
        drawCardFace(g, (rank - 1) * CARD_W, SUIT_ROW[suit] * CARD_H, CARD_W, CARD_H, { rank, suit });
      }
    }
    drawCardBack(g, 13 * CARD_W, 0, CARD_W, CARD_H);
  });
}

/** [u0, v0, u1, v1] for a card (null = the back), with flipY's v. */
export function cardUV(c: Card | null): [number, number, number, number] {
  const col = c ? c.rank - 1 : 13;
  const row = c ? SUIT_ROW[c.suit] : 0;
  const u0 = col / 14, u1 = (col + 1) / 14;
  const v1 = 1 - row / 4, v0 = 1 - (row + 1) / 4;
  return [u0, v0, u1, v1];
}

export function drawCardFace(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, c: Card): void {
  g.save();
  g.fillStyle = '#fbf8f1';
  g.beginPath(); g.roundRect(x + 2, y + 2, w - 4, h - 4, w * 0.09); g.fill();
  g.strokeStyle = 'rgba(0,0,0,0.25)';
  g.lineWidth = 1.5;
  g.stroke();
  const red = c.suit === 'H' || c.suit === 'D';
  g.fillStyle = red ? '#C81E1E' : '#111827';
  g.textAlign = 'left';
  g.textBaseline = 'top';
  g.font = `800 ${Math.round(h * 0.2)}px "Chakra Petch", sans-serif`;
  g.fillText(rankLabel(c.rank), x + w * 0.1, y + h * 0.05);
  g.font = `${Math.round(h * 0.17)}px sans-serif`;
  g.fillText(SUIT_GLYPH[c.suit], x + w * 0.1, y + h * 0.25);
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.font = `${Math.round(h * 0.42)}px sans-serif`;
  g.fillText(SUIT_GLYPH[c.suit], x + w * 0.58, y + h * 0.6);
  g.restore();
}

export function drawCardBack(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number): void {
  g.save();
  g.fillStyle = '#7f1d1d';
  g.beginPath(); g.roundRect(x + 2, y + 2, w - 4, h - 4, w * 0.09); g.fill();
  g.strokeStyle = '#F2C66D';
  g.lineWidth = 2;
  g.beginPath(); g.roundRect(x + 8, y + 8, w - 16, h - 16, w * 0.06); g.stroke();
  g.strokeStyle = 'rgba(242,198,109,0.45)';
  g.lineWidth = 1;
  for (let k = -h; k < w + h; k += 10) {
    g.beginPath(); g.moveTo(x + 8 + k, y + 8); g.lineTo(x + 8 + k - (h - 16), y + h - 8); g.stroke();
  }
  // a gold diamond medallion: an ornament, not a letter (a "K" read as a king)
  const cx = x + w / 2, cy = y + h / 2, r = w * 0.2;
  g.fillStyle = '#7f1d1d';
  g.beginPath(); g.moveTo(cx, cy - r * 1.3); g.lineTo(cx + r, cy); g.lineTo(cx, cy + r * 1.3); g.lineTo(cx - r, cy); g.closePath(); g.fill();
  g.strokeStyle = '#F2C66D';
  g.lineWidth = 2;
  g.stroke();
  g.fillStyle = '#F2C66D';
  g.beginPath(); g.arc(cx, cy, r * 0.32, 0, Math.PI * 2); g.fill();
  g.restore();
}

/** The sportsbook's video wall: Polymarket's real sports markets at their
 *  live prices, or, with nothing loaded, a board that says so. */
export function paintSportsBoard(
  g: CanvasRenderingContext2D, w: number, h: number, t: number,
  board: { rows: BoardRow[]; at: number | null; note: string | null; blips: { row: number; color: string; age: number }[] },
): void {
  g.fillStyle = '#080512';
  g.fillRect(0, 0, w, h);
  // header
  const grd = g.createLinearGradient(0, 0, w, 0);
  grd.addColorStop(0, '#3b0764');
  grd.addColorStop(1, '#1e1b4b');
  g.fillStyle = grd;
  g.fillRect(0, 0, w, 64);
  g.fillStyle = '#E9D5FF';
  g.font = '800 40px "Chakra Petch", sans-serif';
  g.textBaseline = 'middle';
  g.textAlign = 'left';
  g.fillText('SPORTSBOOK', 22, 34);
  g.font = '600 20px "JetBrains Mono", monospace';
  g.fillStyle = '#93C5FD';
  g.fillText('LIVE · POLYMARKET', 290, 36);
  g.textAlign = 'right';
  g.fillStyle = 'rgba(255,255,255,0.65)';
  const ago = board.at ? Math.max(0, Math.round((Date.now() - board.at) / 1000)) : null;
  g.fillText(ago === null ? '' : ago < 90 ? `updated ${ago}s ago` : `updated ${Math.round(ago / 60)}m ago`, w - 22, 36);

  const rows = board.rows;
  if (!rows.length) {
    g.textAlign = 'center';
    g.fillStyle = '#C4B5FD';
    g.font = '700 34px "Chakra Petch", sans-serif';
    g.fillText(board.note ?? 'Loading the board…', w / 2, h / 2);
    g.font = '500 20px "JetBrains Mono", monospace';
    g.fillStyle = 'rgba(255,255,255,0.5)';
    g.fillText('real markets only — nothing on this board is made up', w / 2, h / 2 + 44);
    return;
  }
  // columns
  const colX = { title: 22, a: w - 410, b: w - 210 };
  g.font = '600 18px "JetBrains Mono", monospace';
  g.fillStyle = 'rgba(255,255,255,0.45)';
  g.textAlign = 'left';
  g.fillText('MARKET', colX.title, 86);
  g.textAlign = 'center';
  g.fillText('SIDE A', colX.a + 90, 86);
  g.fillText('SIDE B', colX.b + 90, 86);
  const per = 8;
  // the board scrolls a page every eight seconds
  const pages = Math.max(1, Math.ceil(rows.length / per));
  const page = Math.floor(t / 8) % pages;
  const shown = rows.slice(page * per, page * per + per);
  const top = 104, rh = (h - top - 40) / per;
  shown.forEach((r, i) => {
    const y = top + i * rh;
    const blip = board.blips.find((b) => b.row === i);
    const lit = blip ? Math.max(0, 1 - blip.age / 3) : 0;
    g.fillStyle = lit > 0 ? blip!.color : i % 2 ? 'rgba(168,85,247,0.10)' : 'rgba(236,72,153,0.06)';
    g.globalAlpha = lit > 0 ? 0.2 + lit * 0.4 : 1;
    g.fillRect(10, y, w - 20, rh - 6);
    g.globalAlpha = 1;
    g.textAlign = 'left';
    g.fillStyle = '#ffffff';
    g.font = '600 24px "Chakra Petch", sans-serif';
    let title = r.title;
    while (g.measureText(title).width > colX.a - colX.title - 24 && title.length > 6) title = `${title.slice(0, -2)}…`;
    g.fillText(title, colX.title, y + rh * 0.36);
    g.font = '500 16px "JetBrains Mono", monospace';
    g.fillStyle = 'rgba(255,255,255,0.5)';
    const vol = r.volume24hUsd === null ? '—' : r.volume24hUsd >= 1e6 ? `$${(r.volume24hUsd / 1e6).toFixed(1)}M` : `$${Math.round(r.volume24hUsd / 1e3)}K`;
    g.fillText(`vol 24h ${vol} · closes ${closesIn(r.hoursToClose)}`, colX.title, y + rh * 0.74);
    const side = (x: number, label: string, c: number | null, col: string) => {
      g.fillStyle = 'rgba(255,255,255,0.06)';
      g.fillRect(x, y + 6, 180, rh - 18);
      g.textAlign = 'center';
      g.fillStyle = 'rgba(255,255,255,0.7)';
      g.font = '600 15px "JetBrains Mono", monospace';
      let lab = label;
      while (g.measureText(lab).width > 168 && lab.length > 4) lab = `${lab.slice(0, -2)}…`;
      g.fillText(lab, x + 90, y + rh * 0.3);
      g.fillStyle = c === null ? 'rgba(255,255,255,0.3)' : col;
      g.font = '800 26px "Chakra Petch", sans-serif';
      g.fillText(c === null ? '—' : `${c.toFixed(c % 1 ? 1 : 0)}¢  ${americanOdds(c)}`, x + 90, y + rh * 0.68);
    };
    side(colX.a, r.yesLabel, r.yesCents, '#4ADE80');
    side(colX.b, r.noLabel, r.noCents, '#F9A8D4');
  });
  g.textAlign = 'center';
  g.font = '500 16px "JetBrains Mono", monospace';
  g.fillStyle = 'rgba(255,255,255,0.45)';
  g.fillText(`page ${page + 1}/${pages} · ${rows.length} markets · prices are Polymarket US's own · tickets here are play chips`, w / 2, h - 18);
}
