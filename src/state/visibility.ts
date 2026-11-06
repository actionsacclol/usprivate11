import { useEffect, useState } from 'react';

/**
 * Can anyone see this window?
 *
 * Electron runs the renderer with backgroundThrottling off (see main.ts for
 * why), which also means `document.visibilityState` stays "visible" while the
 * window is minimized or closed to the tray. So main tells us: it sends
 * window:visibility on minimize/hide/restore/show. The page's own
 * visibilitychange still counts too (a plain browser, or a future build that
 * turns throttling back on).
 *
 * Two consumers: the root `data-window-hidden` attribute, which pauses every
 * CSS animation (the aurora drifts forever otherwise), and the Agent Hub,
 * which stops its WebGL loop.
 */

type Listener = (visible: boolean) => void;

interface VisibilityApi {
  onVisibility?: (cb: (visible: boolean) => void) => (() => void) | void;
  isVisible?: () => Promise<boolean>;
}

let mainVisible = true;
let current = true;
let wired = false;
const listeners = new Set<Listener>();

function docHidden(): boolean {
  return typeof document !== 'undefined' && document.visibilityState === 'hidden';
}

function recompute(): void {
  const next = mainVisible && !docHidden();
  if (next === current) return;
  current = next;
  try { document.documentElement.toggleAttribute('data-window-hidden', !next); } catch { /* no DOM */ }
  listeners.forEach((l) => { try { l(next); } catch { /* one consumer never breaks another */ } });
}

function wire(): void {
  if (wired || typeof window === 'undefined') return;
  wired = true;
  document.addEventListener('visibilitychange', recompute);
  // Optional-chained: an older preload has neither. Polybot's contract puts
  // these on `krypt.app`; `krypt.window` is accepted too (kryptrader's spot),
  // whichever the preload actually exposes.
  const k = (window as unknown as { krypt?: Record<string, VisibilityApi | undefined> }).krypt;
  const w = [k?.app, k?.window].find((g) => typeof g?.onVisibility === 'function' || typeof g?.isVisible === 'function');
  w?.onVisibility?.((v) => { mainVisible = !!v; recompute(); });
  const first = w?.isVisible?.();
  if (first && typeof first.then === 'function') {
    first.then((v) => { mainVisible = !!v; recompute(); }, () => { /* assume visible */ });
  }
  recompute();
}

export function isWindowVisible(): boolean {
  return current;
}

/** Subscribe; the callback hears every change from now on. */
export function onWindowVisibility(cb: Listener): () => void {
  wire();
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

/** Wires the root attribute (call once, from the app shell) and returns the
 *  current visibility for components that render differently when hidden. */
export function useWindowVisibility(): boolean {
  const [visible, setVisible] = useState(current);
  useEffect(() => {
    const off = onWindowVisibility(setVisible);
    setVisible(current);
    return off;
  }, []);
  return visible;
}

/** Test hook: forget the wiring and state between tests. */
export function __resetWindowVisibilityForTests(): void {
  mainVisible = true;
  current = true;
  wired = false;
  listeners.clear();
  try { document.documentElement.removeAttribute('data-window-hidden'); } catch { /* no DOM */ }
}
