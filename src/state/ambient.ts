/**
 * The ambient clock: every DECORATIVE loop in the app — the aurora drifting
 * behind the glass, the mascot sprites, the top bar's boss widget — advances
 * on ONE timer at AMBIENT_FPS instead of on every display refresh.
 *
 * Why. Measured with Chrome tracing on an idle window (240 Hz display): the
 * compositor drew 240 frames a second forever, because any one infinite CSS
 * animation or rAF loop asks for a frame on every vsync. With live glass that
 * is the expensive part: each drawn frame re-runs the backdrop filter of every
 * glass surface over whatever moved. These loops are a 38–63 s drift, a 2 px
 * bob and a ~9 fps sprite — at 15 Hz they look the same, and the window draws
 * ~15 frames a second at rest instead of 240.
 *
 * What it drives: infinite CSS/WAAPI animations on `.glass-aurora`,
 * `.krypt-sprite` or anything marked `[data-ambient]`, taken off the
 * compositor's per-frame clock (paused, then stepped by setting currentTime),
 * plus callbacks registered with onAmbientTick (canvas widgets). It never
 * touches real interaction — scrolling, hovers, the gliding glass lenses,
 * transitions and the Agent Hub's 3D render loop all keep the full refresh
 * rate.
 *
 * Off while the window is hidden; reduced motion already removes the CSS
 * animations it would drive.
 */

export const AMBIENT_FPS = 15;
const AMBIENT_SELECTOR = '.glass-aurora, .krypt-sprite, [data-ambient]';

type Tick = (now: number) => void;
const ticks = new Set<Tick>();
const origins = new WeakMap<Animation, number>();
let timer: ReturnType<typeof setInterval> | null = null;

function hidden(): boolean {
  return document.hidden || document.documentElement.hasAttribute('data-window-hidden');
}

function isAmbient(a: Animation): boolean {
  const effect = a.effect as KeyframeEffect | null;
  const target = effect?.target as Element | null | undefined;
  if (!effect || !target || typeof target.closest !== 'function') return false;
  if (effect.getTiming().iterations !== Infinity) return false;
  return !!target.closest(AMBIENT_SELECTOR);
}

/** Take new ambient animations off the per-frame clock, keeping their phase. */
function adopt(now: number): Animation[] {
  const out: Animation[] = [];
  for (const a of document.getAnimations()) {
    if (!isAmbient(a)) continue;
    if (!origins.has(a)) {
      const ct = typeof a.currentTime === 'number' ? a.currentTime : 0;
      origins.set(a, now - ct);
      a.pause();
    }
    out.push(a);
  }
  return out;
}

function step(): void {
  if (hidden()) return;
  const now = performance.now();
  for (const a of adopt(now)) {
    const origin = origins.get(a) ?? now;
    a.currentTime = now - origin;
  }
  ticks.forEach((t) => {
    try { t(now); } catch { /* one widget must not stop the others */ }
  });
}

/** Start once, at app start. */
export function startAmbientClock(): void {
  if (timer !== null || typeof document === 'undefined') return;
  if (typeof document.getAnimations !== 'function') return;
  step();
  timer = setInterval(step, Math.round(1000 / AMBIENT_FPS));
}

export function stopAmbientClock(): void {
  if (timer !== null) clearInterval(timer);
  timer = null;
}

/** Run `cb` on every ambient tick (a canvas widget's draw). Returns an
 *  unsubscribe. Falls back to its own timer if the clock was never started
 *  (tests, a page rendered outside the app shell). */
export function onAmbientTick(cb: Tick): () => void {
  if (timer === null) {
    const own = setInterval(() => { if (!hidden()) cb(performance.now()); },
      Math.round(1000 / AMBIENT_FPS));
    return () => { clearInterval(own); };
  }
  ticks.add(cb);
  return () => { ticks.delete(cb); };
}
