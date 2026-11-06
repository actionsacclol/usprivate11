import type { HistoryDownloadStatus } from '@shared/types';

/** What onboarding offers: enough for a first backtest, in about a quarter of an hour. */
export const ONBOARDING_HISTORY = { days: 7, intervals: ['15m', 'hourly'] } as const;

/** How far back Polymarket US had each series when measured. The backend caps to the same. */
const MAX_DAYS: Record<string, number> = { '5m': 13, '15m': 40, hourly: 40 };
const WINDOWS_PER_DAY: Record<string, number> = { '5m': 288, '15m': 96, hourly: 24 };
const COINS = 4;
const REQUESTS_PER_WINDOW = 2;
const REQUESTS_PER_SEC = 8;

export function historyWindows(days: number, intervals: readonly string[]): number {
  return intervals.reduce(
    (sum, iv) => sum + Math.min(days, MAX_DAYS[iv] ?? days) * (WINDOWS_PER_DAY[iv] ?? 0) * COINS,
    0,
  );
}

/** "14 min" or "1 h 20 min". */
export function fmtDuration(sec: number): string {
  const min = Math.max(1, Math.round(sec / 60));
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  return min % 60 ? `${h} h ${min % 60} min` : `${h} h`;
}

export function historyEstimate(days: number, intervals: readonly string[]): string {
  return fmtDuration((historyWindows(days, intervals) * REQUESTS_PER_WINDOW) / REQUESTS_PER_SEC);
}

export function historyActive(s: HistoryDownloadStatus | null | undefined): boolean {
  return s?.status === 'running' || s?.status === 'waiting';
}

/** Never throws: nothing should stop on a download that couldn't be started. */
export async function startHistoryDownload(
  days: number,
  intervals: readonly string[],
): Promise<HistoryDownloadStatus | null> {
  try {
    return await window.krypt.trading.historyStart({ days, intervals: [...intervals] });
  } catch {
    return null;
  }
}
