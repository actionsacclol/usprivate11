/**
 * Visibility-aware polling.
 *
 * The app hides to the tray rather than closing, so a plain `setInterval` in
 * the renderer keeps hammering the backend for hours while nobody is looking
 * at the screen. Every poll in `AppStateProvider` used to do exactly that.
 *
 * A controller here:
 *   - never has more than one run in flight (a stalled backend must not let
 *     poll requests stack up behind each other),
 *   - holds no timer at all while the document is hidden,
 *   - and, when the window comes back, runs ONCE immediately before re-arming
 *     the interval — otherwise the first thing the user sees is up to a full
 *     period of stale numbers.
 *
 * Timers and the hidden test are injected so this is a pure state machine that
 * can be driven by a fake clock in `e2e/poll.test.mjs`. Nothing in here touches
 * `window` or `document`.
 */

export interface PollTimers {
  setInterval: (fn: () => void, ms: number) => number;
  clearInterval: (id: number) => void;
  /** `document.visibilityState === 'hidden'` in the renderer. */
  isHidden: () => boolean;
}

export interface PollController {
  /** Arm the interval (no-op while hidden). Does NOT run the task — the
   *  caller has just done its own initial load. */
  start: () => void;
  /** Hook for `visibilitychange`: pause when hidden, catch up when visible. */
  onVisibilityChange: () => void;
  /** Run now unless a run is already in flight. */
  runNow: () => void;
  /** Tear down. Safe to call twice. */
  stop: () => void;
  /** Test/diagnostic: is an interval currently armed? */
  isArmed: () => boolean;
}

export function createPoll(
  task: () => Promise<unknown>,
  intervalMs: number,
  timers: PollTimers,
): PollController {
  let id: number | null = null;
  let busy = false;
  let stopped = false;

  const runNow = (): void => {
    if (stopped || busy) return;
    busy = true;
    void Promise.resolve()
      .then(task)
      .catch(() => { /* every task already swallows its own errors */ })
      .finally(() => { busy = false; });
  };

  const arm = (): void => {
    if (stopped || id !== null || timers.isHidden()) return;
    id = timers.setInterval(runNow, intervalMs);
  };

  const disarm = (): void => {
    if (id === null) return;
    timers.clearInterval(id);
    id = null;
  };

  return {
    start: () => {
      if (stopped) return;
      arm();
    },
    onVisibilityChange: () => {
      if (stopped) return;
      if (timers.isHidden()) {
        disarm();
        return;
      }
      // Already armed means we never actually paused — don't double-fire.
      if (id !== null) return;
      arm();
      runNow();
    },
    runNow,
    stop: () => {
      stopped = true;
      disarm();
    },
    isArmed: () => id !== null,
  };
}
