import { randInt, type Rng } from './rng';

// A three-reel slot. Every reel carries the same 22-stop strip; a spin picks
// one stop per reel and the centre line pays. The return to player is a fact
// of the strip and the paytable, not a dial: `rtp()` computes it exactly, the
// machine prints it, and test/casinoGames.test.ts pins it below 100%.

export type Sym = 'cherry' | 'lemon' | 'bell' | 'bar' | 'diamond' | 'seven';

export const SYMBOLS: readonly Sym[] = ['cherry', 'lemon', 'bell', 'bar', 'diamond', 'seven'];

/** The strip, spread so no symbol bunches up (5 cherries, 5 lemons, 4 bells,
 *  4 bars, 2 diamonds, 2 sevens). */
export const STRIP: readonly Sym[] = [
  'cherry', 'bell', 'lemon', 'bar', 'cherry', 'seven', 'lemon', 'bell', 'cherry', 'bar', 'lemon',
  'diamond', 'cherry', 'bell', 'lemon', 'bar', 'cherry', 'seven', 'lemon', 'bell', 'bar', 'diamond',
];

/** Three of a kind on the line, in multiples of the bet. */
export const THREE_PAYS: Record<Sym, number> = {
  seven: 200, diamond: 100, bar: 25, bell: 15, lemon: 10, cherry: 10,
};
/** Exactly two cherries anywhere on the line. */
export const TWO_CHERRIES = 2;

export interface SlotSpin {
  stops: [number, number, number];
  line: [Sym, Sym, Sym];
  /** Multiple of the bet paid back (0 = nothing). */
  pays: number;
}

export function lineOf(stops: readonly [number, number, number]): [Sym, Sym, Sym] {
  return [STRIP[stops[0]], STRIP[stops[1]], STRIP[stops[2]]];
}

export function paysFor(line: readonly Sym[]): number {
  if (line[0] === line[1] && line[1] === line[2]) return THREE_PAYS[line[0]];
  const cherries = line.filter((s) => s === 'cherry').length;
  return cherries === 2 ? TWO_CHERRIES : 0;
}

export function spinSlots(rng: Rng): SlotSpin {
  const stops: [number, number, number] = [randInt(rng, STRIP.length), randInt(rng, STRIP.length), randInt(rng, STRIP.length)];
  const line = lineOf(stops);
  return { stops, line, pays: paysFor(line) };
}

/** The exact return to player, over all 22³ lines. */
export function rtp(): number {
  const n = STRIP.length;
  let total = 0;
  for (let a = 0; a < n; a++) for (let b = 0; b < n; b++) for (let c = 0; c < n; c++) {
    total += paysFor([STRIP[a], STRIP[b], STRIP[c]]);
  }
  return total / n ** 3;
}

export const SYM_LABEL: Record<Sym, string> = {
  cherry: 'CHERRY', lemon: 'LEMON', bell: 'BELL', bar: 'BAR', diamond: 'DIAMOND', seven: '7',
};

/** One symbol, drawn centred at (0, 0) in a box `s` wide: the reels in 3D
 *  and the machine panel draw the same pictures. */
export function drawSymbol(g: CanvasRenderingContext2D, sym: Sym, s: number): void {
  g.save();
  g.lineJoin = 'round';
  switch (sym) {
    case 'cherry': {
      g.strokeStyle = '#15803D';
      g.lineWidth = s * 0.07;
      g.beginPath();
      g.moveTo(-s * 0.18, s * 0.08);
      g.quadraticCurveTo(-s * 0.05, -s * 0.32, s * 0.16, -s * 0.34);
      g.moveTo(s * 0.18, s * 0.1);
      g.quadraticCurveTo(s * 0.12, -s * 0.14, s * 0.16, -s * 0.34);
      g.stroke();
      g.fillStyle = '#DC2626';
      for (const [x, y] of [[-s * 0.18, s * 0.17], [s * 0.18, s * 0.19]]) {
        g.beginPath(); g.arc(x, y, s * 0.17, 0, Math.PI * 2); g.fill();
      }
      g.fillStyle = 'rgba(255,255,255,0.55)';
      g.beginPath(); g.arc(-s * 0.23, s * 0.11, s * 0.045, 0, Math.PI * 2); g.fill();
      break;
    }
    case 'lemon': {
      g.fillStyle = '#FACC15';
      g.beginPath(); g.ellipse(0, 0, s * 0.34, s * 0.25, -0.35, 0, Math.PI * 2); g.fill();
      g.strokeStyle = '#A16207';
      g.lineWidth = s * 0.03;
      g.stroke();
      break;
    }
    case 'bell': {
      g.fillStyle = '#F59E0B';
      g.beginPath();
      g.moveTo(-s * 0.3, s * 0.2);
      g.quadraticCurveTo(-s * 0.26, -s * 0.34, 0, -s * 0.34);
      g.quadraticCurveTo(s * 0.26, -s * 0.34, s * 0.3, s * 0.2);
      g.closePath();
      g.fill();
      g.fillStyle = '#B45309';
      g.fillRect(-s * 0.34, s * 0.18, s * 0.68, s * 0.07);
      g.beginPath(); g.arc(0, s * 0.3, s * 0.06, 0, Math.PI * 2); g.fill();
      break;
    }
    case 'bar': {
      g.fillStyle = '#111827';
      g.fillRect(-s * 0.4, -s * 0.16, s * 0.8, s * 0.32);
      g.fillStyle = '#F2C66D';
      g.font = `900 ${s * 0.26}px "Chakra Petch", sans-serif`;
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText('BAR', 0, s * 0.01);
      break;
    }
    case 'diamond': {
      g.fillStyle = '#38BDF8';
      g.beginPath();
      g.moveTo(0, -s * 0.34); g.lineTo(s * 0.3, -s * 0.06); g.lineTo(0, s * 0.36); g.lineTo(-s * 0.3, -s * 0.06);
      g.closePath();
      g.fill();
      g.fillStyle = 'rgba(255,255,255,0.45)';
      g.beginPath(); g.moveTo(0, -s * 0.34); g.lineTo(s * 0.12, -s * 0.06); g.lineTo(-s * 0.12, -s * 0.06); g.closePath(); g.fill();
      break;
    }
    case 'seven': {
      g.fillStyle = '#DC2626';
      g.font = `900 ${s * 0.72}px "Chakra Petch", sans-serif`;
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.fillText('7', 0, s * 0.04);
      g.strokeStyle = '#7F1D1D';
      g.lineWidth = s * 0.025;
      g.strokeText('7', 0, s * 0.04);
      break;
    }
  }
  g.restore();
}
