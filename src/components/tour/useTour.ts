import { createContext, useContext } from 'react';
import type { TourSide } from './tourSteps';

/**
 * The app-level "show me around" controls: start the tour, replay
 * onboarding. Provided by GuideHost (mounted once, in App's Shell).
 *
 * The default is a pair of no-ops rather than a thrown error, so a component
 * that offers these (the sidebar's tour button, a Settings row) still renders
 * in a test without the host around it.
 */
export interface GuideApi {
  startTour: () => void;
  /** Opens onboarding from step 1 as UI state only: it resets no setting,
   *  key, profile, the disclaimer or the last page. */
  replayOnboarding: () => void;
}

const noop = (): void => {};
export const GuideContext = createContext<GuideApi>({ startTour: noop, replayOnboarding: noop });
export const useGuide = (): GuideApi => useContext(GuideContext);

// The first-run "take a tour?" prompt is offered once, ever. A per-viewer
// convenience: storage blocked or cleared means it may be offered again,
// which is harmless — what must never happen is the tour itself starting on
// every launch, and nothing here can do that.
export const TOUR_OFFERED_KEY = 'krypt.polybot.tour.offered';

export function wasTourOffered(): boolean {
  try { return localStorage.getItem(TOUR_OFFERED_KEY) === '1'; } catch { return false; }
}

export function markTourOffered(): void {
  try { localStorage.setItem(TOUR_OFFERED_KEY, '1'); } catch { /* convenience only */ }
}

export interface Box { top: number; left: number; width: number; height: number }
export interface Placement { top: number; left: number; side: TourSide | 'center' }

/** Space kept clear at the top: the 36px title bar is a window drag region,
 *  and Electron lets a drag region swallow clicks even under a higher
 *  z-index, so a card button sitting over it would move the window instead. */
export const TOP_INSET = 44;
const GAP = 14;
const MARGIN = 12;

const clamp = (v: number, lo: number, hi: number): number => Math.max(lo, Math.min(hi, v));

/**
 * Where the tooltip card goes: the first preferred side it fits on, slid
 * along that edge so it stays on screen; centred as a last resort (a tiny
 * window). Pure, so the flipping is tested without a layout engine.
 */
export function placeCard(
  t: Box,
  card: { width: number; height: number },
  vp: { width: number; height: number },
  prefer: TourSide[] = ['right', 'left', 'bottom', 'top'],
): Placement {
  const maxTop = Math.max(TOP_INSET, vp.height - card.height - MARGIN);
  const maxLeft = Math.max(MARGIN, vp.width - card.width - MARGIN);
  const midY = clamp(t.top + t.height / 2 - card.height / 2, TOP_INSET, maxTop);
  const midX = clamp(t.left + t.width / 2 - card.width / 2, MARGIN, maxLeft);
  for (const side of prefer) {
    if (side === 'right') {
      const left = t.left + t.width + GAP;
      if (left + card.width <= vp.width - MARGIN) return { side, left, top: midY };
    } else if (side === 'left') {
      const left = t.left - GAP - card.width;
      if (left >= MARGIN) return { side, left, top: midY };
    } else if (side === 'bottom') {
      const top = t.top + t.height + GAP;
      if (top + card.height <= vp.height - MARGIN) return { side, top: Math.max(top, TOP_INSET), left: midX };
    } else {
      const top = t.top - GAP - card.height;
      if (top >= TOP_INSET) return { side, top, left: midX };
    }
  }
  return {
    side: 'center',
    top: clamp((vp.height - card.height) / 2, TOP_INSET, maxTop),
    left: clamp((vp.width - card.width) / 2, MARGIN, maxLeft),
  };
}

/** The element a step points at, if it is on screen at all. A tab hidden in
 *  Simple mode isn't rendered, and a `hidden` block is in the DOM but displays
 *  nothing: both mean "skip this step". */
export function findTarget(id: string): HTMLElement | null {
  // Step ids are fixed literals (tourSteps.ts), never caller input.
  const el = document.querySelector<HTMLElement>(`[data-tour="${id}"]`);
  if (!el) return null;
  // checkVisibility (Chromium 105+) sees display:none on any ancestor. jsdom
  // has no layout, so there everything in the DOM counts as shown.
  const check = (el as HTMLElement & { checkVisibility?: () => boolean }).checkVisibility;
  if (typeof check === 'function' && !check.call(el)) return null;
  return el;
}

/**
 * Bring a target into its own scroll region. The sidebar nav is the one
 * region that scrolls, and most tabs sit below its fold on a laptop-height
 * window. Only the nearest scrollable ancestor moves: `scrollIntoView` would
 * also scroll `overflow: hidden` ancestors and shift the whole app shell.
 */
export function revealTarget(el: HTMLElement): void {
  let p = el.parentElement;
  while (p && p !== document.body) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && p.scrollHeight > p.clientHeight) {
      const pr = p.getBoundingClientRect();
      const er = el.getBoundingClientRect();
      if (er.top < pr.top + 8 || er.bottom > pr.bottom - 8) {
        p.scrollTop += (er.top + er.height / 2) - (pr.top + pr.height / 2);
      }
      return;
    }
    p = p.parentElement;
  }
}
