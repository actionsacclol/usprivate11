// The casino's games run on PLAY CHIPS only (wallet.ts): nothing here ever
// touches the account, an order or the backend. Randomness is injectable so
// the rules can be pinned by tests with a seed.

export type Rng = () => number;

/** A small, fast, seedable PRNG (mulberry32). Good enough for cards and
 *  wheels; not for anything secret. */
export function mulberry32(seed: number): Rng {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const defaultRng: Rng = () => Math.random();

/** 0 .. n-1 */
export function randInt(rng: Rng, n: number): number {
  return Math.min(n - 1, Math.floor(rng() * n));
}

export function pickOf<T>(rng: Rng, xs: readonly T[]): T {
  return xs[randInt(rng, xs.length)];
}
