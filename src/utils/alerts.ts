/**
 * The renderer's half of the alerts. The backend reports everything it can
 * (python/alerts.py), but a dead backend can't report itself, so that one is here.
 * Dismissal is here too, and only lives until the app closes: a problem still
 * there at the next start is worth saying again.
 */
import type { AppAlert, BackendInfo } from '@shared/types';

// 'stopped' is also the state before the first report arrives, and a start can
// take a while on a slow machine. Neither is a fault until it has gone on a bit.
const STOPPED_GRACE_MS = 15_000;
const STARTING_GRACE_MS = 60_000;
// 'crashed' also shows for a moment before an automatic respawn, when there's
// nothing to do. Still crashed after this means the app has stopped trying.
const CRASHED_GRACE_MS = 8_000;
// A problem must stay gone this long before closing it stops counting, or a flaky
// connection would bring back an alert the user has already closed, over and over.
const FORGET_DISMISSED_AFTER_MS = 60_000;

const restart = { kind: 'restart', label: 'Restart engine' } as const;
const FIX = 'Click Restart engine. If it keeps happening, restart the app and check the Logs page.';

/** An alert for the backend process itself, or null while it's fine. */
export function engineAlert(backend: BackendInfo, msInStatus: number): AppAlert | null {
  const why = backend.lastError ? ` Last error: ${backend.lastError.slice(0, 160)}` : '';
  const doing = 'Nothing is trading and balances aren\'t updating.';
  if (backend.status === 'crashed' && msInStatus >= CRASHED_GRACE_MS) {
    return {
      id: 'engine', severity: 'error', title: 'The trading engine crashed',
      detail: `${doing}${why}`, fix: FIX, action: restart,
    };
  }
  if (backend.status === 'stopped' && msInStatus >= STOPPED_GRACE_MS) {
    return {
      id: 'engine', severity: 'error', title: 'The trading engine is stopped',
      detail: `${doing}${why}`, fix: FIX, action: restart,
    };
  }
  if ((backend.status === 'starting' || backend.status === 'restarting') && msInStatus >= STARTING_GRACE_MS) {
    return {
      id: 'engine', severity: 'error', title: 'The trading engine isn\'t starting',
      detail: `It has been trying for over a minute.${why}`, fix: FIX, action: restart,
    };
  }
  return null;
}

/** Getting worse counts as new: a warning you closed can come back as an error. */
export function alertKey(a: AppAlert): string {
  return `${a.id}:${a.severity}`;
}

/**
 * The dismissed set after this poll. Keeps `key -> last time the problem was seen`,
 * and forgets keys whose problem has been gone long enough. Returns the same map
 * when nothing changed, so a poll with nothing new doesn't re-render.
 */
export function updateDismissed(
  dismissed: ReadonlyMap<string, number>, active: readonly AppAlert[], now: number,
): ReadonlyMap<string, number> {
  const activeKeys = new Set(active.map(alertKey));
  let changed = false;
  const next = new Map<string, number>();
  for (const [key, seen] of dismissed) {
    if (activeKeys.has(key)) {
      next.set(key, now);
      if (seen !== now) changed = true;
    } else if (now - seen < FORGET_DISMISSED_AFTER_MS) {
      next.set(key, seen);
    } else {
      changed = true;
    }
  }
  return changed ? next : dismissed;
}
