/**
 * Where someone is in the guided Polymarket US account setup.
 *
 * Identity approval happens on their phone and may not be instant, so the place
 * has to survive closing the app. This is a per-machine convenience: losing it
 * only means picking the step again, so localStorage is enough. It holds no
 * credential, only which screen comes next.
 */

import { POLYMARKET_SQUAD_URL } from './links';

export type SetupPath = 'new' | 'existing';
export type SetupStep = 'signup' | 'verify' | 'fund' | 'key' | 'paste';

// The invite comes first: Polymarket US's FAQ says a referral code must be applied
// during sign-up. The squad isn't a step: its page opens once the account connects
// (openSquadOnce).
const NEW_ACCOUNT: SetupStep[] = ['signup', 'verify', 'fund', 'key', 'paste'];
const EXISTING_ACCOUNT: SetupStep[] = ['key', 'paste'];

export function stepsFor(path: SetupPath): SetupStep[] {
  return path === 'new' ? NEW_ACCOUNT : EXISTING_ACCOUNT;
}

export interface SetupProgress {
  path: SetupPath;
  step: SetupStep;
}

const STORAGE_KEY = 'krypt-us-account-setup-v2';
const SQUAD_OPENED_KEY = 'krypt-us-squad-opened-v1';

/**
 * Opens the squad page the first time an account connects on this machine, whichever
 * way the key was added. Polymarket US publishes no way to join a squad through its
 * API, so the person still clicks Join on that page; nothing changes on their account
 * until they do. Once only, so replacing a key doesn't open it again.
 * Returns whether it opened.
 */
export function openSquadOnce(): boolean {
  try {
    if (localStorage.getItem(SQUAD_OPENED_KEY)) return false;
    localStorage.setItem(SQUAD_OPENED_KEY, '1');
  } catch { /* storage unavailable: opening again later does no harm */ }
  void window.krypt.app.openExternal(POLYMARKET_SQUAD_URL);
  return true;
}

/** The saved place, or null if there is none or it no longer makes sense. */
export function loadProgress(): SetupProgress | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as Partial<SetupProgress>;
    if (p.path !== 'new' && p.path !== 'existing') return null;
    if (!p.step || !stepsFor(p.path).includes(p.step)) return null;
    return { path: p.path, step: p.step };
  } catch {
    return null;
  }
}

export function saveProgress(progress: SetupProgress | null): void {
  try {
    if (progress) localStorage.setItem(STORAGE_KEY, JSON.stringify(progress));
    else localStorage.removeItem(STORAGE_KEY);
  } catch { /* storage can be unavailable; the guide still works for this session */ }
}
