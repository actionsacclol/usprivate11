import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * The Crypto-tab presets carry no measured numbers. The original app's replay
 * figures came from a different venue (different fees, a different settlement
 * index), and nothing has been measured on this exchange yet. A number that
 * can't be attributed to this venue doesn't get shown.
 */
const src = readFileSync(join(__dirname, '..', 'src', 'pages', 'Crypto15m.tsx'), 'utf8');
const presets = src.slice(src.indexOf('const C15_PRESETS'), src.indexOf('function StrategySettings'));
const hints = [...presets.matchAll(/hint: '((?:[^'\\]|\\.)*)'/g)].map((m) => m[1]);
const MEASURED = /[−+-]?\d+(?:\.\d+)?¢\/(?:contract|ct)/;

describe('Crypto-tab preset hints', () => {
  it('found the preset hints', () => {
    expect(hints.length).toBeGreaterThanOrEqual(4);
  });

  it('quote no per-contract result and say nothing has been measured here', () => {
    for (const h of hints) {
      expect(h, h).not.toMatch(MEASURED);
      expect(h, h).toMatch(/Not measured on this exchange yet/);
    }
  });

  it('offer no paired both-sides preset', () => {
    expect(presets).not.toMatch(/paired/i);
  });

  it('drop the old venue and the old settlement source', () => {
    for (const gone of ['Chainlink', 'Gamma', 'CLOB', 'TWAP']) {
      expect(presets).not.toContain(gone);
    }
  });
});
