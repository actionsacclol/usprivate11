import { useSyncExternalStore } from 'react';

/**
 * Glass quality: how the app's glass surfaces render.
 *
 *   live  — the liquid-glass material (SVG displacement + frost), always.
 *   frost — the CSS frost only: same layout and tint, no SVG filters.
 *   auto  — live, unless this machine can't hold the frame rate with it, in
 *           which case the watchdog below demotes it to frost and says so.
 *
 * A per-viewer preference, so it lives in localStorage (it was already the
 * `krypt.glass = 'frost'` escape hatch). Storage failing means Auto.
 *
 * Live glass is also off where it can't work or isn't wanted: no
 * ResizeObserver (jsdom under vitest — the library measures with one and would
 * throw), or the OS asks for reduced transparency.
 */
export type GlassQuality = 'auto' | 'live' | 'frost';

const KEY = 'krypt.glass';
const DEMOTED_KEY = 'krypt.glass.autoFrost';

function read(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function write(key: string, v: string | null): void {
  try {
    if (v === null) localStorage.removeItem(key);
    else localStorage.setItem(key, v);
  } catch { /* convenience only */ }
}

function storedQuality(): GlassQuality {
  const v = read(KEY);
  return v === 'live' || v === 'frost' ? v : 'auto';
}

// Tests flip this to exercise Auto under jsdom, which has no ResizeObserver.
let canRunLive = typeof ResizeObserver !== 'undefined';

function reducedTransparency(): boolean {
  try {
    return typeof matchMedia === 'function'
      && matchMedia('(prefers-reduced-transparency: reduce)').matches;
  } catch {
    return false;
  }
}

let quality: GlassQuality = storedQuality();
let demoted = read(DEMOTED_KEY) === '1';
let reduced = reducedTransparency();
const listeners = new Set<() => void>();

function effective(): boolean {
  if (!canRunLive || reduced) return false;
  if (quality === 'frost') return false;
  if (quality === 'live') return true;
  return !demoted;
}
let snapshot = effective();

function emit(): void {
  const next = effective();
  if (next === snapshot) {
    listeners.forEach((l) => l());
    return;
  }
  snapshot = next;
  listeners.forEach((l) => l());
}

try {
  matchMedia('(prefers-reduced-transparency: reduce)')
    .addEventListener('change', () => { reduced = reducedTransparency(); emit(); });
} catch { /* old engine / jsdom */ }

export function liveGlassEnabled(): boolean { return snapshot; }

export function useLiveGlass(): boolean {
  return useSyncExternalStore(subscribe, liveGlassEnabled, liveGlassEnabled);
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

export interface GlassQualityState {
  quality: GlassQuality;
  /** Auto has fallen back to frost on this machine. */
  autoDemoted: boolean;
  /** The OS asked for reduced transparency (always frost). */
  reducedTransparency: boolean;
  live: boolean;
}

export function glassQualityState(): GlassQualityState {
  return { quality, autoDemoted: demoted, reducedTransparency: reduced, live: snapshot };
}

/** The user's choice. Choosing Auto again clears an earlier demotion, so it
 *  gets a fresh measurement. */
export function setGlassQuality(q: GlassQuality): void {
  quality = q;
  write(KEY, q === 'auto' ? null : q);
  if (q === 'auto') {
    demoted = false;
    write(DEMOTED_KEY, null);
  }
  emit();
}

function demote(): void {
  if (quality !== 'auto' || demoted) return;
  demoted = true;
  write(DEMOTED_KEY, '1');
  emit();
  try { window.dispatchEvent(new CustomEvent('krypt:glass-demoted')); } catch { /* noop */ }
}

// ── The Auto watchdog ──────────────────────────────────────────────────────
//
// Samples frame timing in short windows (never continuously: an always-on
// rAF loop would itself keep the GPU busy) and demotes Auto to frost when this
// machine can't hold its own refresh rate with live glass on.
//
// The refresh interval is LEARNED as the best window median seen — a 60 Hz
// display settles at ~16.7 ms, a 144 Hz one at ~6.9 — so the test is relative
// ("frames are taking 1.5× as long as this display can show them"), not a
// fixed fps that would demote every 60 Hz laptop or never fire on a 240 Hz
// monitor. Two slow windows in a row are required: one page mount or GC
// pause is a hitch, not a machine that can't cope.

const WINDOW_MS = 1500;
const PERIOD_MS = 20_000;
const SLOW_RATIO = 1.5;
const SLOW_WINDOWS = 2;

let started = false;
let bestMedian = Infinity;
let slowStreak = 0;
let sampling = false;
let timer: ReturnType<typeof setTimeout> | null = null;

function median(xs: number[]): number {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)] ?? 0;
}

function sampleWindow(): Promise<number[]> {
  return new Promise((done) => {
    const d: number[] = [];
    let last = 0;
    let t0 = 0;
    const tick = (t: number): void => {
      if (t0 === 0) { t0 = t; last = t; requestAnimationFrame(tick); return; }
      d.push(t - last);
      last = t;
      if (t - t0 < WINDOW_MS) requestAnimationFrame(tick);
      else done(d);
    };
    requestAnimationFrame(tick);
  });
}

async function runWindow(): Promise<void> {
  if (sampling || document.hidden || quality !== 'auto' || demoted || !snapshot) return;
  if (document.documentElement.hasAttribute('data-window-hidden')) return;
  sampling = true;
  try {
    const d = await sampleWindow();
    if (d.length < 20 || document.hidden) return;
    judge(median(d));
  } finally {
    sampling = false;
  }
}

/** One window's verdict. The refresh interval is never shorter than ~4 ms
 *  (240 Hz): a best median under that is an uncapped dev run, where nothing
 *  is slow. The 12 ms floor keeps a 240 Hz display dipping to 120 from
 *  counting as a machine that can't cope. */
function judge(m: number): void {
  bestMedian = Math.min(bestMedian, m);
  const refresh = Math.max(bestMedian, 4);
  if (m > refresh * SLOW_RATIO && m > 12) {
    slowStreak += 1;
    if (slowStreak >= SLOW_WINDOWS) demote();
  } else {
    slowStreak = 0;
  }
}

function schedule(ms: number): void {
  if (timer) clearTimeout(timer);
  timer = setTimeout(async () => {
    await runWindow();
    schedule(PERIOD_MS);
  }, ms);
}

/** Start the Auto watchdog once, at app start. */
export function startGlassWatchdog(): void {
  if (started || typeof window === 'undefined' || typeof requestAnimationFrame !== 'function') return;
  started = true;
  schedule(4000);
}

/** Check soon (after a page change, when a heavier page has just mounted).
 *  The delay lets the mount's own work finish first — that is a hitch, not
 *  the steady state being judged. */
export function kickGlassWatchdog(): void {
  if (!started) return;
  schedule(1200);
}

/** Test seam. */
export const __glassQualityTest = {
  reset(): void {
    quality = storedQuality(); demoted = read(DEMOTED_KEY) === '1';
    reduced = reducedTransparency(); bestMedian = Infinity; slowStreak = 0;
    snapshot = effective();
  },
  judge(medians: number[]): void { medians.forEach(judge); },
  canRunLive(on: boolean): void { canRunLive = on; snapshot = effective(); },
};
