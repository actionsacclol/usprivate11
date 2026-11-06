/**
 * Load-time migration for settings written before the AI-agent port.
 *
 * Kept out of settings-store.ts for the same reason sanitize.ts is kept out of
 * ipc.ts: settings-store imports `electron`, which cannot load in a test
 * process, and a migration that only ever runs against a real user's
 * settings.json is exactly the code that must not go untested.
 *
 * Mirrors python config._migrate_mcp, which applies the same rules to what the
 * backend receives — both sides must agree, or a legacy live user is silently
 * dropped to paper on one of them.
 */
import type { TraderConfig } from '../../shared/types';
import { cleanAgents, DEFAULT_AGENT_ID, isValidAgentId } from '../../shared/agents';

/** Keys of the old model. Read once below, then dropped: the store spreads
 *  whatever the file holds over the defaults, so without this a stale key
 *  survives every load and is pushed to the backend forever. */
export const RETIRED_CONFIG_KEYS = [
  'mcpLiveTradingEnabled', 'mcpDailyLimitUsd', 'aiAnthropicModel', 'aiOpenaiModel',
  // Engines this venue doesn't have. A profile or settings file carried over
  // from the original app can still name them: none may reach the backend.
  'enableTrading', 'tradeWhales', 'tradeMomentum', 'tradeConvergence', 'whaleActivityWs',
  'mainRecordSignals', 'takerRebatePct', 'crypto15mRtdsWs',
] as const;

/** Whole families of retired keys: Perps, copy trading, and paired both-sides
 *  mode, which would be a wash trade on this venue. */
const RETIRED_PREFIXES = ['perps', 'copy', 'crypto15mPaired'];

export function isRetiredConfigKey(k: string): boolean {
  return (RETIRED_CONFIG_KEYS as readonly string[]).includes(k)
    || RETIRED_PREFIXES.some((p) => k.startsWith(p));
}

const AI_PROVIDERS = new Set(['anthropic', 'openai', 'openrouter', 'gemini', 'ollama', 'lmstudio']);

export interface MigrateOpts {
  /** The running config (settings.json's own), not a profile snapshot or a
   *  patch. Only it may come out Live from an old file. */
  live?: boolean;
  /** The user had already accepted the disclaimer: an EXISTING install. */
  existingUser?: boolean;
}

/**
 * Paper | Live. A stored config with no `accountMode` is from before the
 * switch existed:
 *
 *   terminalPaperMode: true    -> paper  (they were already on paper)
 *   existing user, running cfg -> live   (engines keep doing exactly what
 *                                         they did; their own switches stand)
 *   fresh install / snapshot   -> paper
 *
 * Idempotent: a config that already names accountMode keeps it, and anything
 * but the exact word 'live' is paper. Afterwards terminalPaperMode is DERIVED
 * from it, so the two can never disagree.
 */
export function migrateAccountMode(src: Record<string, unknown>, opts: MigrateOpts = {}): void {
  if (src.accountMode !== 'live' && src.accountMode !== 'paper') {
    const absent = !('accountMode' in src);
    src.accountMode = absent && src.terminalPaperMode !== true && opts.live === true && opts.existingUser === true
      ? 'live' : 'paper';
  }
  src.terminalPaperMode = src.accountMode === 'paper';
}

/**
 * The agent model. Before named agents, one switch let agents trade live
 * (`mcpLiveTradingEnabled`) and the daily cap was `mcpDailyLimitUsd`:
 *
 *   mcpLiveTradingEnabled: true -> mcpTradeMode 'live' (unless one is set),
 *                                  mcpAllowLiveSwitches true (the old switch
 *                                  also covered engine switches),
 *                                  Default agent live (when there is no list)
 *   mcpDailyLimitUsd            -> mcpDailySpendUsd (unless one is set)
 */
export function migrateAgents(src: Record<string, unknown>, defaults: TraderConfig): void {
  const legacyLive = src.mcpLiveTradingEnabled === true;
  if (!('mcpTradeMode' in src)) src.mcpTradeMode = legacyLive ? 'live' : defaults.mcpTradeMode;
  if (!['off', 'paper', 'live'].includes(String(src.mcpTradeMode))) src.mcpTradeMode = defaults.mcpTradeMode;
  if (!('mcpAllowLiveSwitches' in src) && legacyLive) src.mcpAllowLiveSwitches = true;
  if (!('mcpDailySpendUsd' in src) && typeof src.mcpDailyLimitUsd === 'number'
      && Number.isFinite(src.mcpDailyLimitUsd)) {
    src.mcpDailySpendUsd = src.mcpDailyLimitUsd;
  }
  // A file with NO list is from before agents existed: its Default inherits
  // the (migrated) trade mode, so a user whose agents were live stays live. A
  // list that exists is cleaned on every write, never rewritten.
  src.mcpAgents = cleanAgents('mcpAgents' in src ? src.mcpAgents : undefined, String(src.mcpTradeMode));
  const apId = String(src.autopilotAgentId ?? '').trim().toLowerCase();
  src.autopilotAgentId = isValidAgentId(apId) ? apId : DEFAULT_AGENT_ID;
}

/** One model key for all six providers. The old per-provider keys fill it
 *  for the provider that was selected. */
export function migrateAi(src: Record<string, unknown>, defaults: TraderConfig): void {
  const p = String(src.aiProvider ?? '').trim().toLowerCase();
  src.aiProvider = AI_PROVIDERS.has(p) ? p : defaults.aiProvider;
  if (typeof src.aiModel !== 'string') {
    const old = src.aiProvider === 'openai' ? src.aiOpenaiModel
      : src.aiProvider === 'anthropic' ? src.aiAnthropicModel : undefined;
    if (typeof old === 'string' && old.trim()) src.aiModel = old.trim();
    else if (src.aiProvider === 'anthropic') src.aiModel = defaults.aiModel;
    else src.aiModel = '';
  }
}

/**
 * Merge a stored config (the running one, a saved profile's snapshot, or a
 * store write) over the defaults with every migration applied and every
 * retired key dropped. Idempotent: running it on its own output changes
 * nothing.
 */
export function migrateConfig(raw: unknown, defaults: TraderConfig, opts: MigrateOpts = {}): TraderConfig {
  const src: Record<string, unknown> =
    raw && typeof raw === 'object' && !Array.isArray(raw) ? { ...(raw as Record<string, unknown>) } : {};
  migrateAccountMode(src, opts);
  migrateAgents(src, defaults);
  migrateAi(src, defaults);
  for (const k of Object.keys(src)) if (isRetiredConfigKey(k)) delete src[k];
  // No 4-hour series on this venue.
  if (!['5m', '15m', 'hourly'].includes(String(src.crypto15mInterval ?? defaults.crypto15mInterval))) {
    src.crypto15mInterval = defaults.crypto15mInterval;
  }
  return { ...defaults, ...src } as TraderConfig;
}
