// Renderer-side unit tests for the terminal's pure chart geometry.
//
//   npm run test:unit
//
// Node 24 strips TypeScript types natively, so this imports `shared/series.ts`
// directly — no bundler, no test framework, no new dependency.
//
// This logic was the last untested thing in the terminal, and it is exactly
// the kind that fails invisibly: a chart that bridges a six-hour hole in the
// tape looks completely normal, and draws a trend that never happened.

import { strict as assert } from 'node:assert';
import test from 'node:test';

import { GAP_FACTOR, breakGaps, countGaps } from '../shared/series.ts';

const pts = (...pairs) => pairs.map(([t, p]) => ({ t, p }));

test('an evenly spaced series is passed through untouched', () => {
  const input = pts([0, 10], [60, 11], [120, 12], [180, 13]);
  const out = breakGaps(input);
  assert.equal(countGaps(out), 0);
  assert.deepEqual(out, input);
});

test('a hole in the tape becomes an explicit break', () => {
  // 60s spacing, then a 10-minute hole.
  const out = breakGaps(pts([0, 10], [60, 11], [120, 12], [720, 13]));
  assert.equal(countGaps(out), 1);
  const nullIdx = out.findIndex((p) => p.p === null);
  assert.ok(nullIdx > 0 && nullIdx < out.length - 1, 'the break must sit inside the hole');
  assert.ok(out[nullIdx].t > 120 && out[nullIdx].t < 720);
});

test('every real point survives, in order, with its value intact', () => {
  const input = pts([0, 10], [60, 11], [120, 12], [900, 13], [960, 14]);
  const out = breakGaps(input);
  const real = out.filter((p) => p.p !== null);
  assert.deepEqual(real, input, 'a real observation was dropped or altered');
  const ts = out.map((p) => p.t);
  assert.deepEqual(ts, [...ts].sort((a, b) => a - b), 'series is not ascending');
});

test('a break never invents a value', () => {
  const out = breakGaps(pts([0, 10], [60, 11], [120, 12], [3600, 13]));
  for (const p of out) {
    assert.ok(p.p === null || typeof p.p === 'number');
  }
  // The inserted entry is a hole, not a midpoint of the two neighbours.
  const hole = out.find((p) => p.p === null);
  assert.equal(hole.p, null);
});

test('jitter under the threshold is not treated as a gap', () => {
  // Spacing 60,60,60 then 150 — under 3x, so it is jitter, not a hole.
  const out = breakGaps(pts([0, 10], [60, 11], [120, 12], [180, 13], [330, 14]));
  assert.equal(countGaps(out), 0);
});

test('a gap just over the threshold IS a gap', () => {
  const step = 60;
  const over = step * GAP_FACTOR + 1;
  const out = breakGaps(pts([0, 10], [step, 11], [step * 2, 12], [step * 2 + over, 13]));
  assert.equal(countGaps(out), 1);
});

test('fewer than three points are passed through', () => {
  // Two points cannot establish a typical spacing, so there is nothing to
  // judge a gap against — guessing one would be the invention we are avoiding.
  assert.deepEqual(breakGaps([]), []);
  assert.deepEqual(breakGaps(pts([0, 10])), pts([0, 10]));
  assert.deepEqual(breakGaps(pts([0, 10], [86400, 11])), pts([0, 10], [86400, 11]));
});

test('duplicate timestamps do not produce a zero median', () => {
  // A median spacing of 0 would make every gap "infinitely" large and break
  // the line between every single point.
  const out = breakGaps(pts([0, 10], [0, 11], [0, 12], [60, 13]));
  assert.equal(countGaps(out), 0);
});

test('multiple holes each get their own break', () => {
  const out = breakGaps(pts([0, 1], [60, 2], [120, 3], [900, 4], [960, 5], [3000, 6]));
  assert.equal(countGaps(out), 2);
});

test('a real zero is charted, not treated as missing', () => {
  // 0c is a legitimate probability. Only `null` means "no data".
  const out = breakGaps(pts([0, 0], [60, 0], [120, 0]));
  assert.equal(countGaps(out), 0);
  assert.deepEqual(out.map((p) => p.p), [0, 0, 0]);
});

test('breakGaps does not mutate its input', () => {
  const input = pts([0, 10], [60, 11], [120, 12], [3600, 13]);
  const copy = JSON.parse(JSON.stringify(input));
  breakGaps(input);
  assert.deepEqual(input, copy);
});
