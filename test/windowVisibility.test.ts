import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetWindowVisibilityForTests, isWindowVisible, onWindowVisibility,
} from '../src/state/visibility';

/**
 * Electron runs the renderer with backgroundThrottling off, so the page never
 * learns it was minimized or closed to the tray, and the Agent Hub's WebGL
 * loop and the aurora kept rendering at full rate for nobody. Main now sends
 * window:visibility; this pins the renderer side of that: the signal reaches
 * subscribers (the hub stops/starts its engine off it) and flips the root
 * attribute the CSS pauses every animation on.
 */

let fire: ((visible: boolean) => void) | null = null;
const isVisible = vi.fn();

beforeEach(() => {
  __resetWindowVisibilityForTests();
  fire = null;
  isVisible.mockReset();
  isVisible.mockResolvedValue(true);
  // PolyBot's preload contract puts these on `krypt.app` (KRYPTRADER_PORT §5c)
  (window as unknown as { krypt: unknown }).krypt = {
    app: {
      version: () => Promise.resolve('3.0.0'),
      isVisible,
      onVisibility: (cb: (v: boolean) => void) => { fire = cb; return () => { fire = null; }; },
    },
  };
});

afterEach(() => {
  __resetWindowVisibilityForTests();
});

const root = () => document.documentElement;

describe('window visibility', () => {
  it('reaches subscribers and pauses CSS animation while minimized or in the tray', async () => {
    const seen: boolean[] = [];
    const off = onWindowVisibility((v) => seen.push(v));
    await Promise.resolve();
    expect(isWindowVisible()).toBe(true);
    expect(root().hasAttribute('data-window-hidden')).toBe(false);

    fire!(false); // minimize / hide
    expect(isWindowVisible()).toBe(false);
    expect(root().hasAttribute('data-window-hidden')).toBe(true);

    fire!(true); // restore / show
    expect(root().hasAttribute('data-window-hidden')).toBe(false);
    expect(seen).toEqual([false, true]);

    off();
    fire!(false);
    expect(seen).toEqual([false, true]);
  });

  it('starts hidden when the app launched straight into the tray', async () => {
    isVisible.mockResolvedValue(false);
    const seen: boolean[] = [];
    onWindowVisibility((v) => seen.push(v));
    await new Promise((r) => setTimeout(r, 0));
    expect(isWindowVisible()).toBe(false);
    expect(seen).toEqual([false]);
  });

  it('repeats nothing: an unchanged state is not re-announced', () => {
    const seen: boolean[] = [];
    onWindowVisibility((v) => seen.push(v));
    fire!(true);
    fire!(true);
    expect(seen).toEqual([]);
  });

  it('survives an older preload with no visibility channel', () => {
    (window as unknown as { krypt: unknown }).krypt = { app: {}, window: {} };
    expect(() => onWindowVisibility(() => {})).not.toThrow();
    expect(isWindowVisible()).toBe(true);
  });

  it('also accepts the channel on `krypt.window` (the kryptrader spot)', async () => {
    let fireW: ((v: boolean) => void) | null = null;
    (window as unknown as { krypt: unknown }).krypt = {
      app: { version: () => Promise.resolve('x') },
      window: {
        isVisible: () => Promise.resolve(true),
        onVisibility: (cb: (v: boolean) => void) => { fireW = cb; return () => {}; },
      },
    };
    const seen: boolean[] = [];
    onWindowVisibility((v) => seen.push(v));
    await Promise.resolve();
    fireW!(false);
    expect(seen).toEqual([false]);
    expect(root().hasAttribute('data-window-hidden')).toBe(true);
  });

  it('survives no preload at all', () => {
    delete (window as unknown as { krypt?: unknown }).krypt;
    expect(() => onWindowVisibility(() => {})).not.toThrow();
    expect(isWindowVisible()).toBe(true);
  });

  it('the stylesheet really pauses animations on that attribute', () => {
    const css = readFileSync(join(__dirname, '..', 'src', 'index.css'), 'utf8');
    expect(css).toMatch(/:root\[data-window-hidden\] \*[^{]*\{\s*animation-play-state: paused !important;/);
  });
});
