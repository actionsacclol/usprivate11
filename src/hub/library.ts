/** A strategy with its last saved backtest, structurally. */
export interface StrategyWithBacktest {
  name: string;
  backtest?: { netCentsPerContract?: number | null; t?: number | null; n?: number | null } | null;
}

/** One strategy's LAST SAVED backtest, as the user's own data produced it. */
export interface SavedBacktest {
  name: string;
  /** Net cents per share after fees. */
  net: number;
  t: number | null;
  n: number | null;
}

// The Casino's practice table, between real runs, shows a STATIC
// readout of the last saved results: the only verdicts the hub may read are
// the ones already on disk. It is never a timer source: an earlier build
// re-"ran" these rows every 12-18 s so the chamber always looked busy. A run
// now animates only when one really finishes (state/activity.ts).
//
// PolyBot has no strategy library with saved verdicts yet (kryptrader's
// `turbine.library()`), so the chamber idles between real runs: a backtest
// run elsewhere in the app reaches it through the activity bus. The cache
// and the pure helpers stay so a future saved-results read is one function.
// The hub must never START a backtest or an optimizer to fill the chamber:
// that is minutes of CPU the user never asked for.

const TTL_MS = 10 * 60_000;
let cache: { at: number; rows: Promise<SavedBacktest[]> } | null = null;

export function shortStrategy(name: string): string {
  let s = name.replace('★', '').split(/ — | \(/)[0].trim();
  s = s.replace(/^(BTC|ETH|SOL|XRP|DOGE|HYPE|BNB)\s+15m\s+/i, '');
  if (s.length > 20) s = s.split(' ').slice(-2).join(' ');
  return s;
}

/** Strategies that have a saved result; one without is not "0¢ edge". */
export function savedBacktests(strategies: StrategyWithBacktest[] | null | undefined): SavedBacktest[] {
  const out: SavedBacktest[] = [];
  for (const st of strategies ?? []) {
    const net = st.backtest?.netCentsPerContract;
    if (typeof net !== 'number' || !Number.isFinite(net)) continue;
    out.push({ name: shortStrategy(st.name), net, t: st.backtest?.t ?? null, n: st.backtest?.n ?? null });
  }
  return out;
}

/** A positive mean is not an edge on its own: under t = 2 it is the backtest
 *  that loses money live. Unknown t is not a pass. */
export function hasEdge(b: SavedBacktest): boolean {
  return b.net > 0 && typeof b.t === 'number' && b.t >= 2;
}

export function loadSavedBacktests(now = Date.now()): Promise<SavedBacktest[]> {
  if (cache && now - cache.at < TTL_MS) return cache.rows;
  // No saved-results source in this build: an honest empty list.
  const rows = Promise.resolve<SavedBacktest[]>([]);
  cache = { at: now, rows };
  return rows;
}
