import { afterEach, describe, expect, it, vi } from 'vitest';
import { AMBIENT_FPS, onAmbientTick, startAmbientClock, stopAmbientClock } from '../src/state/ambient';

describe('the ambient clock', () => {
  afterEach(() => {
    stopAmbientClock();
    vi.useRealTimers();
    document.documentElement.removeAttribute('data-window-hidden');
  });

  it('ticks a canvas widget at ~AMBIENT_FPS, not per frame', () => {
    vi.useFakeTimers();
    const calls: number[] = [];
    const off = onAmbientTick((t) => calls.push(t));
    vi.advanceTimersByTime(1000);
    off();
    expect(calls.length).toBeGreaterThanOrEqual(AMBIENT_FPS - 1);
    expect(calls.length).toBeLessThanOrEqual(AMBIENT_FPS + 1);
  });

  it('skips ticks while the window is hidden', () => {
    vi.useFakeTimers();
    let n = 0;
    const off = onAmbientTick(() => { n += 1; });
    document.documentElement.setAttribute('data-window-hidden', '');
    vi.advanceTimersByTime(1000);
    off();
    expect(n).toBe(0);
  });

  it('unsubscribing stops the ticks', () => {
    vi.useFakeTimers();
    let n = 0;
    const off = onAmbientTick(() => { n += 1; });
    vi.advanceTimersByTime(200);
    off();
    const at = n;
    vi.advanceTimersByTime(1000);
    expect(n).toBe(at);
  });

  it('starting is idempotent and safe without the Web Animations API', () => {
    expect(() => { startAmbientClock(); startAmbientClock(); }).not.toThrow();
  });
});
