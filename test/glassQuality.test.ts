import { beforeEach, describe, expect, it } from 'vitest';
import {
  __glassQualityTest as T, glassQualityState, liveGlassEnabled, setGlassQuality, subscribe,
} from '../src/components/glass/glassQuality';

describe('glass quality', () => {
  beforeEach(() => {
    localStorage.clear();
    T.reset();
    T.canRunLive(true);
  });

  it('defaults to Auto, live', () => {
    expect(glassQualityState().quality).toBe('auto');
    expect(liveGlassEnabled()).toBe(true);
  });

  it('Frost and Live are honoured and persisted; Auto stores nothing', () => {
    setGlassQuality('frost');
    expect(liveGlassEnabled()).toBe(false);
    expect(localStorage.getItem('krypt.glass')).toBe('frost');
    setGlassQuality('live');
    expect(liveGlassEnabled()).toBe(true);
    setGlassQuality('auto');
    expect(localStorage.getItem('krypt.glass')).toBeNull();
  });

  it('never runs live where the library cannot (no ResizeObserver)', () => {
    T.canRunLive(false);
    setGlassQuality('live');
    expect(liveGlassEnabled()).toBe(false);
  });

  it('notifies subscribers when the effective mode flips', () => {
    let n = 0;
    const off = subscribe(() => { n += 1; });
    setGlassQuality('frost');
    off();
    setGlassQuality('live');
    expect(n).toBe(1);
  });
});

describe('the Auto watchdog', () => {
  beforeEach(() => {
    localStorage.clear();
    T.reset();
    T.canRunLive(true);
  });

  it('a 60 Hz display holding 60 is never demoted', () => {
    T.judge([16.7, 16.6, 16.8, 16.7, 16.7, 16.9]);
    expect(liveGlassEnabled()).toBe(true);
  });

  it('a 144 Hz display dropping to ~70 for two windows steps down to Frost', () => {
    T.judge([6.9, 6.9, 14.2, 14.1]);
    expect(liveGlassEnabled()).toBe(false);
    expect(glassQualityState().autoDemoted).toBe(true);
    expect(localStorage.getItem('krypt.glass.autoFrost')).toBe('1');
  });

  it('one slow window is a hitch, not a verdict', () => {
    T.judge([16.7, 33.4, 16.7, 33.4, 16.7]);
    expect(liveGlassEnabled()).toBe(true);
  });

  it('a 240 Hz display dipping to 120 is not "slow" (12 ms floor)', () => {
    T.judge([4.2, 8.4, 8.4, 8.4]);
    expect(liveGlassEnabled()).toBe(true);
  });

  it('never overrides an explicit Live choice', () => {
    setGlassQuality('live');
    T.judge([16.7, 40, 40, 40]);
    expect(liveGlassEnabled()).toBe(true);
  });

  it('choosing Auto again clears the demotion so it re-measures', () => {
    T.judge([6.9, 14.2, 14.2]);
    expect(liveGlassEnabled()).toBe(false);
    setGlassQuality('auto');
    expect(liveGlassEnabled()).toBe(true);
    expect(localStorage.getItem('krypt.glass.autoFrost')).toBeNull();
  });
});

describe('the Frost switch', () => {
  it('is a real role=switch that toggles', async () => {
    const { render, fireEvent } = await import('@testing-library/react');
    const React = await import('react');
    const { Switch } = await import('../src/components/common');
    localStorage.clear();
    T.reset();
    T.canRunLive(false);
    let v = false;
    const { getByRole } = render(React.createElement(Switch, {
      checked: false, label: 'Real orders', onChange: (n: boolean) => { v = n; },
    }));
    const sw = getByRole('switch') as HTMLInputElement;
    expect(sw.getAttribute('aria-label')).toBe('Real orders');
    fireEvent.click(sw);
    expect(v).toBe(true);
  });
});
