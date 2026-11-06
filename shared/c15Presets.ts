import type { CollectionStats, ReplayNoData, TraderConfig } from './types';

export type C15Interval = '5m' | '15m' | 'hourly';
const C15_INTERVAL_IDS: readonly string[] = ['5m', '15m', 'hourly'];
/** The Crypto tab's chip label per interval (src/pages/Crypto15m.tsx C15_INTERVALS). */
const C15_INTERVAL_CHIP: Record<C15Interval, string> = {
  '5m': '5 min', '15m': '15 min', hourly: '1 hour',
};

/**
 * The window series a crypto backtest of `patch` replays. Mirrors the
 * backend: the patch is merged over the live config, and a missing or
 * unknown interval falls back to '15m' (python crypto15m._interval).
 */
export function replayInterval(
  patch: Partial<TraderConfig>, current?: string | null,
): C15Interval {
  const iv = patch.crypto15mInterval ?? current ?? '15m';
  return C15_INTERVAL_IDS.includes(iv) ? (iv as C15Interval) : '15m';
}

/**
 * Pre-run warning for a strategy whose interval has NO resolved windows
 * recorded at all, so the replay can only come back empty. The recorder only
 * records the interval selected on the Crypto tab: on a machine recording 5m,
 * every 15m preset (and vice versa) has nothing to replay. null when there is
 * data, or when the stats don't say (older backend / not loaded yet).
 */
export function intervalDataWarning(
  interval: C15Interval,
  c15: Pick<CollectionStats['c15'], 'byInterval' | 'recordingInterval'> | null | undefined,
  recordingOn = true,
): string | null {
  if (!c15?.byInterval) return null;
  if ((c15.byInterval[interval]?.resolved ?? 0) > 0) return null;
  const rec = c15.recordingInterval;
  const select =
    `select “${C15_INTERVAL_CHIP[interval]}” on the Crypto tab and leave the app open for a few days to collect them ` +
    '(this also changes which windows the crypto engine trades, if it is on)';
  if (!recordingOn) {
    return `No ${interval} windows recorded, and crypto data collection is off — turn it on below` +
      (rec && rec !== interval ? `, then ${select}.` : '.') +
      ' Until then this backtest has nothing to replay.';
  }
  if (rec === interval) {
    return `No ${interval} windows recorded yet. The recorder is collecting ${interval} now — ` +
      'leave the app open; this backtest has nothing to replay until windows settle.';
  }
  return `This strategy trades ${interval} windows and none are recorded: the recorder only records ` +
    `the interval selected on the Crypto tab${rec ? ` (currently ${rec})` : ''}. To test it, ${select}.`;
}

/** Headline for a replay that scanned nothing. Leads with the cause, never a number. */
export function noDataHeadline(noData?: ReplayNoData | null): string {
  switch (noData?.reason) {
    case 'interval':
      return `No ${noData.interval} windows recorded`;
    case 'none-yet':
      return `No ${noData.interval} windows recorded yet`;
    case 'collection-off':
      return 'Crypto data collection is off';
    case 'assets':
      return `No recorded ${noData.interval} windows for this strategy's coins`;
    default:
      return 'No collected data yet';
  }
}

/**
 * Shared crypto up/down preset cores — the replay-relevant settings that the
 * Crypto tab quick-presets (src/pages/Crypto15m.tsx C15_PRESETS) and the
 * Backtest strategy list (src/pages/Backtest.tsx C15_STRATS) agree on.
 *
 * Retune a strategy's numbers HERE so both pages move together. Each page
 * spreads its core and layers page-specific extras on top: the Crypto tab adds
 * execution-only knobs (entry style, FAK, WS toggles, exit/stop zeros); the
 * Backtest page keeps a few intentionally divergent numbers as explicit
 * overrides (see the comments at each site — do NOT fold those in here).
 */
export const C15_PRESET_CORE: Record<
  'sniper' | 'sniper-5m' | 'favorite' | 'contrarian',
  Partial<TraderConfig>
> = {
  // 'sniper' had already drifted between the two pages before this file
  // existed (Crypto tab: modelMinProb 0.9985 / delay 2 / entryMax 0.99 vs
  // Backtest: 0.97 / 5 / 0.97), so those three stay as page-level overrides —
  // only the part both pages agree on lives here.
  sniper: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'model', crypto15mUseRules: false,
    crypto15mModelMinEdgeCents: 2, crypto15mModelFinalMinute: true,
  },
  'sniper-5m': {
    crypto15mInterval: '5m', crypto15mAssets: ['BTC'],
    crypto15mDirectionMode: 'favorite', crypto15mUseRules: false,
    crypto15mTimeDelayMin: 2,
    crypto15mEntryThreshold: 0.90, crypto15mEntryMax: 0.99,
    crypto15mMinDeltaPct: 0.0005,
  },
  favorite: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'favorite', crypto15mUseRules: false,
    crypto15mEntryThreshold: 0.95, crypto15mEntryMax: 0.98,
  },
  contrarian: {
    crypto15mInterval: '15m',
    crypto15mDirectionMode: 'contrarian', crypto15mUseRules: false,
    crypto15mEntryThreshold: 0.90,
  },
};
