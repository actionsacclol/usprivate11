import { describe, expect, it } from 'vitest';
import {
  C15_PRESET_CORE, intervalDataWarning, noDataHeadline, replayInterval,
} from '@shared/c15Presets';

// The recorder only records the interval selected on the Crypto tab. On a
// machine recording 5m, the Backtest page's 15m presets used to come back
// "0 trades" with the cause buried. These helpers make the page say why,
// before and after the run.

const FIVE_MIN_ONLY = {
  byInterval: { '5m': { windows: 4482, resolved: 4472 } },
  recordingInterval: '5m',
};

describe('replayInterval', () => {
  it('uses the preset’s own interval over the live one', () => {
    expect(replayInterval(C15_PRESET_CORE.sniper, '5m')).toBe('15m');
    expect(replayInterval(C15_PRESET_CORE['sniper-5m'], '15m')).toBe('5m');
  });

  it('follows the live setting when the patch has none ("current settings")', () => {
    expect(replayInterval({}, '5m')).toBe('5m');
    expect(replayInterval({}, 'hourly')).toBe('hourly');
  });

  it('falls back to 15m like the backend for missing or unknown values', () => {
    expect(replayInterval({}, undefined)).toBe('15m');
    expect(replayInterval({}, 'weekly')).toBe('15m');
    // No 4-hour series on this venue.
    expect(replayInterval({}, '4h')).toBe('15m');
  });
});

describe('C15_PRESET_CORE', () => {
  it('has no paired both-sides preset: it would be a wash trade on this venue', () => {
    expect(Object.keys(C15_PRESET_CORE)).not.toContain('paired');
    for (const patch of Object.values(C15_PRESET_CORE)) {
      expect(Object.keys(patch).join(' ')).not.toMatch(/Paired/);
    }
  });
});

describe('intervalDataWarning', () => {
  it('warns that a 15m preset has nothing to replay on a 5m-recording machine', () => {
    const w = intervalDataWarning('15m', FIVE_MIN_ONLY);
    expect(w).toContain('trades 15m windows and none are recorded');
    expect(w).toContain('currently 5m');
    expect(w).toContain('select “15 min” on the Crypto tab');
    // Switching the interval moves the live engine too — the hint must say so.
    expect(w).toContain('crypto engine trades');
  });

  it('is silent when the interval has resolved windows', () => {
    expect(intervalDataWarning('5m', FIVE_MIN_ONLY)).toBeNull();
  });

  it('is silent when the stats are unknown (older backend / not loaded)', () => {
    expect(intervalDataWarning('15m', null)).toBeNull();
    expect(intervalDataWarning('15m', { recordingInterval: '5m' })).toBeNull();
  });

  it('says "not yet" when the right interval is being recorded', () => {
    const w = intervalDataWarning('15m', { byInterval: {}, recordingInterval: '15m' });
    expect(w).toContain('collecting 15m now');
  });

  it('names collection being off first', () => {
    const w = intervalDataWarning('15m', FIVE_MIN_ONLY, false);
    expect(w).toMatch(/^No 15m windows recorded, and crypto data collection is off/);
    expect(w).toContain('select “15 min” on the Crypto tab');
  });
});

describe('noDataHeadline', () => {
  it('leads with the cause, never a number', () => {
    const base = { recordingInterval: '5m', recordingOn: true, recorded: { '5m': 10 }, message: '' };
    expect(noDataHeadline({ ...base, reason: 'interval', interval: '15m' })).toBe('No 15m windows recorded');
    expect(noDataHeadline({ ...base, reason: 'none-yet', interval: '5m' })).toBe('No 5m windows recorded yet');
    expect(noDataHeadline({ ...base, reason: 'collection-off', interval: '15m' })).toBe('Crypto data collection is off');
    expect(noDataHeadline({ ...base, reason: 'assets', interval: '5m' })).toContain("this strategy's coins");
    expect(noDataHeadline(undefined)).toBe('No collected data yet');
  });
});
