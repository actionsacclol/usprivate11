import { useSyncExternalStore } from 'react';

/**
 * The wallet's "hide my balance" setting, for streaming or screenshots: masks the
 * amounts in the top bar and the sidebar card. A view preference on this machine, so
 * it lives in browser storage and never touches trading config.
 */
const KEY = 'krypt.hideBalance';

export const HIDDEN_AMOUNT = '•••••';

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === '1';
  } catch {
    return false;
  }
}

let hidden = read();
const listeners = new Set<() => void>();

export function setHideBalance(next: boolean): void {
  hidden = next;
  try {
    localStorage.setItem(KEY, next ? '1' : '0');
  } catch { /* still applies until the app closes */ }
  listeners.forEach((l) => l());
}

export function useHideBalance(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => hidden,
  );
}
