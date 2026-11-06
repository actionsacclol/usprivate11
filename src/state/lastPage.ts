// Which page the app opens on, remembered across launches. Its own module
// (not App.tsx) so the rules can be tested without mounting the whole app.
import type { PageId } from '../App';

export const PAGE_IDS: readonly PageId[] = [
  'dashboard', 'positions', 'history', 'profiles', 'settings', 'api', 'logs',
  'guide', 'about', 'visualizer', 'crypto15m', 'accounts', 'backtest',
  'terminal', 'scripts', 'strategies', 'discover', 'market', 'track', 'remote', 'agents',
];

export const LAST_PAGE_KEY = 'krypt.polybot.lastPage';

/** AI agents are the front door: the bundled heuristic strategies measured
 *  net negative in the offline backtest, while an agent that has to commit a
 *  forecast before every buy, scored against the market, is evidence. */
export const FRONT_DOOR: PageId = 'agents';

export function isPageId(v: unknown): v is PageId {
  return typeof v === 'string' && (PAGE_IDS as readonly string[]).includes(v);
}

/** Where the app opens: where the user left off, else the front door. A
 *  stored id this build doesn't know is forgotten rather than trusted.
 *  Storage is a convenience: blocked or cleared, it is the front door. */
export function loadLastPage(): PageId {
  try {
    const v = window.localStorage.getItem(LAST_PAGE_KEY);
    if (isPageId(v)) return v;
    if (v !== null) window.localStorage.removeItem(LAST_PAGE_KEY);
  } catch { /* storage blocked */ }
  return FRONT_DOOR;
}

export function saveLastPage(p: PageId): void {
  try { window.localStorage.setItem(LAST_PAGE_KEY, p); } catch { /* convenience only */ }
}

/** A page that crashed must not be the page the next launch opens on, or one
 *  bad render becomes a crash on every start. */
export function forgetLastPage(): void {
  try { window.localStorage.removeItem(LAST_PAGE_KEY); } catch { /* convenience only */ }
}
