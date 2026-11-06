// ────────────────────────────────────────────────────────────────────────
// Pure series geometry for the terminal's charts.
//
// Deliberately in `shared/` and free of any React import, so it can be unit
// tested directly. This is the layer where "the chart looked empty because
// the token is new" turns out to be a plausible-looking lie — the logic is
// small, invisible when wrong, and exactly what belongs in a test.
// ────────────────────────────────────────────────────────────────────────

export interface SeriesPoint {
  /** Unix seconds. */
  t: number;
  /** Probability in cents, 0..100. */
  p: number;
}

/** A point, or an explicit hole. `p: null` BREAKS the line. */
export interface GapPoint {
  t: number;
  p: number | null;
}

/** A gap wider than this multiple of the typical spacing is a real hole. */
export const GAP_FACTOR = 3;

/**
 * Insert explicit nulls where the series skips a period.
 *
 * Chart libraries connect consecutive points with a straight line, which
 * across a multi-hour hole in the tape draws a trend that never happened.
 * That is the same class of lie as inventing a candle: in an illiquid market
 * it is how somebody buys a corpse. So we find the typical spacing and, where
 * a gap is much larger, drop a null in the middle to break the line.
 *
 * Never removes or moves a real point, and never invents a value — the
 * inserted entries carry `p: null`, which renders as nothing at all.
 */
export function breakGaps(points: SeriesPoint[]): GapPoint[] {
  // Two points cannot establish a "typical" spacing, so there is nothing to
  // judge a gap against. Pass them through untouched rather than guess.
  if (points.length < 3) return points.map((p) => ({ t: p.t, p: p.p }));

  const deltas: number[] = [];
  for (let i = 1; i < points.length; i++) deltas.push(points[i].t - points[i - 1].t);

  const median = [...deltas].sort((a, b) => a - b)[Math.floor(deltas.length / 2)] ?? 0;
  if (median <= 0) return points.map((p) => ({ t: p.t, p: p.p }));

  const out: GapPoint[] = [{ t: points[0].t, p: points[0].p }];
  for (let i = 1; i < points.length; i++) {
    const gap = points[i].t - points[i - 1].t;
    if (gap > median * GAP_FACTOR) {
      out.push({ t: points[i - 1].t + Math.floor(gap / 2), p: null });
    }
    out.push({ t: points[i].t, p: points[i].p });
  }
  return out;
}

/** How many holes `breakGaps` found — surfaced under the chart. */
export function countGaps(points: GapPoint[]): number {
  let n = 0;
  for (const p of points) if (p.p === null) n++;
  return n;
}
