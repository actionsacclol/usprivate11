import { randInt, type Rng } from './rng';

// European roulette: one zero, 37 pockets, a house edge of 1/37 on every bet.
// The wheel order is the real one, so the 3D wheel lands the ball in the
// pocket that won (floor.ts reads WHEEL_ORDER).

export const WHEEL_ORDER = [
  0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10,
  5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26,
] as const;

const REDS = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);

export type PocketColor = 'green' | 'red' | 'black';

export function colorOf(n: number): PocketColor {
  if (n === 0) return 'green';
  return REDS.has(n) ? 'red' : 'black';
}

export type RouletteBet =
  | { kind: 'straight'; n: number }
  | { kind: 'red' | 'black' | 'odd' | 'even' | 'low' | 'high' }
  | { kind: 'dozen' | 'column'; n: 1 | 2 | 3 };

/** Winnings per chip staked, on top of the stake coming back. */
export function payoutMultiplier(b: RouletteBet): number {
  if (b.kind === 'straight') return 35;
  if (b.kind === 'dozen' || b.kind === 'column') return 2;
  return 1;
}

export function wins(b: RouletteBet, n: number): boolean {
  if (b.kind === 'straight') return b.n === n;
  if (n === 0) return false;
  switch (b.kind) {
    case 'red': return colorOf(n) === 'red';
    case 'black': return colorOf(n) === 'black';
    case 'odd': return n % 2 === 1;
    case 'even': return n % 2 === 0;
    case 'low': return n <= 18;
    case 'high': return n >= 19;
    case 'dozen': return Math.ceil(n / 12) === b.n;
    case 'column': return ((n - 1) % 3) + 1 === b.n;
  }
  return false;
}

export function betLabel(b: RouletteBet): string {
  switch (b.kind) {
    case 'straight': return `${b.n}`;
    case 'red': return 'RED';
    case 'black': return 'BLACK';
    case 'odd': return 'ODD';
    case 'even': return 'EVEN';
    case 'low': return '1–18';
    case 'high': return '19–36';
    case 'dozen': return ['1st 12', '2nd 12', '3rd 12'][b.n - 1];
    case 'column': return `col ${b.n}`;
  }
  return '?';
}

export function spin(rng: Rng): number {
  return randInt(rng, 37);
}

export function pocketIndex(n: number): number {
  return WHEEL_ORDER.indexOf(n as (typeof WHEEL_ORDER)[number]);
}

export interface PlacedBet { bet: RouletteBet; stake: number }

/** Chips back from a spin: each winning bet returns its stake plus winnings. */
export function settle(bets: readonly PlacedBet[], n: number): number {
  let back = 0;
  for (const { bet, stake } of bets) if (wins(bet, n)) back += stake * (1 + payoutMultiplier(bet));
  return back;
}

/** The bets an agent likes: mostly even money, now and then a number. */
export function agentBet(rng: Rng): RouletteBet {
  const r = rng();
  if (r < 0.18) return { kind: 'straight', n: randInt(rng, 37) };
  if (r < 0.36) return { kind: 'dozen', n: (randInt(rng, 3) + 1) as 1 | 2 | 3 };
  const even = ['red', 'black', 'odd', 'even', 'low', 'high'] as const;
  return { kind: even[randInt(rng, even.length)] };
}
