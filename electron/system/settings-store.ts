import { app } from 'electron';
import { copyFileSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { basename, dirname, join } from 'node:path';
import type { AppState, Profile, TraderConfig } from '../../shared/types';
import { defaultAgent, DEFAULT_AGENT_ID } from '../../shared/agents';
import { migrateConfig } from './legacy-settings';
import { isPersonalKey } from './sanitize';

// Krypt user data lives under app.getPath('userData') on Windows that
// resolves to %APPDATA%/Krypt PolyBot US. Anything else (Documents, etc.)
// would be subject to Controlled Folder Access — see READMEGUIDE §16.

const userDataDir = (): string => app.getPath('userData');

const settingsFile = (): string => join(userDataDir(), 'settings.json');

// Survival vault OUTSIDE the roaming userData dir: settings (and, on the
// python side, the newest DB snapshot) are mirrored here so they outlive
// anything that removes %APPDATA%/Krypt PolyBot US — an uninstall with the
// "delete app data" box ticked, a reinstall gone wrong, a userData reset.
// One subdir per account (multi-account repoints userData at accounts/<x>).
// Keep the layout in sync with python db._vault_dir().
function vaultSettingsFile(): string | null {
  try {
    const base = process.env.LOCALAPPDATA
      ? join(process.env.LOCALAPPDATA, 'Krypt PolyBot US Vault')
      : join(app.getPath('home'), '.krypt-polybot-us-vault');
    const ud = userDataDir();
    const tag = basename(dirname(ud)).toLowerCase() === 'accounts'
      ? basename(ud)
      : 'default';
    const dir = join(base, tag);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    return join(dir, 'settings.json');
  } catch {
    return null;
  }
}

export const DEFAULT_CONFIG: TraderConfig = {
  network: 'mainnet',

  // Read by the engines that remain. KEEP IN SYNC with python/config.py.
  minCashReserveFraction: 0.05,
  maxTotalExposureFraction: 0.75,
  positionPollInterval: 30,
  balancePollInterval: 60,
  resolutionCheckInterval: 300,
  marketRefreshInterval: 300,
  startBankrollUsd: 0.0,
  stopLossOnDay: -50.0,
  statsChartWindowHours: 168,

  crypto15mEnabled: false,
  crypto15mInterval: '15m',
  crypto15mAssets: null,
  crypto15mSizingMode: 'fixed',
  crypto15mOrderSize: 5,
  crypto15mBalancePct: 0.02,
  crypto15mMaxLossPct: 0,
  crypto15mStreakSizing: false,
  crypto15mStreakLossPct: 20,
  crypto15mStreakWinPct: 0,
  crypto15mStreakMaxMult: 4,
  crypto15mMaxConcurrent: 7,
  crypto15mDailyLossLimit: -50.0,
  crypto15mLifetimeLossLimitPct: 0.5,
  crypto15mLifetimeLossLimitUsd: 0.0,
  crypto15mTakeProfitTotal: 0,
  crypto15mDirectionMode: 'favorite',
  crypto15mModelMinProb: 0.97,
  crypto15mModelMinEdgeCents: 2.0,
  crypto15mModelFinalMinute: true,
  crypto15mModelAutopause: true,
  crypto15mModelMaxBookGapCents: 25,
  crypto15mSpotWs: true,
  crypto15mSellIntoStrength: false,
  crypto15mSellStrengthCents: 80,
  crypto15mTimeDelayMin: 8,
  crypto15mEntryThreshold: 0.95,
  crypto15mEntryMax: 0.98,
  crypto15mExitThreshold: 0.4,
  crypto15mTakeProfit: 0,
  crypto15mStopLossPct: 0,
  crypto15mTakeProfitPct: 0,
  crypto15mMinRsi: 0,
  crypto15mMinMacdHist: 0,
  crypto15mMinDeltaPct: 0,
  crypto15mEntryDiff: 0.02,
  crypto15mEntryStyle: 'taker',
  crypto15mTakerFak: true,
  crypto15mMakerCancelMin: 1,
  crypto15mMakerEscalate: true,
  crypto15mMakerFillSec: 4,
  crypto15mHoursStartUtc: 0,
  crypto15mHoursEndUtc: 24,
  crypto15mRecordSignals: true,
  crypto15mArbDetect: true,
  crypto15mArbMinEdgeCents: 1,
  crypto15mImbalanceDetect: true,
  crypto15mImbalanceLevels: 3,
  crypto15mImbalanceGate: false,
  crypto15mImbalanceGateMin: 0.2,
  crypto15mIndicatorDetect: true,
  crypto15mWsBook: true,
  crypto15mUseRules: false,
  crypto15mRules: [],

  // User strategy scripts (Scripts tab): master switch + money rails.
  scriptsLiveEnabled: false,
  scriptPollSec: 5,
  scriptMaxEntryCents: 97,
  scriptMaxContracts: 20,
  scriptMaxOpen: 2,
  scriptDailyLossUsd: 25,
  scriptMaxEnabled: 10,
  // decide_market() universe. Must be listed here, not just in the Python
  // defaults: mergeConfig() builds the renderer's config from THIS object, so
  // a key missing here never reaches the UI (the Scripts page would silently
  // render a hardcoded fallback instead of the real setting).
  // KEEP IN SYNC with python/config.py `script_market_limit`. This object wins:
  // it is pushed whole to the backend on every boot, so a stale value here
  // silently overrode the Python default on every install (20 vs 150 — scripts
  // saw ~2% of the board while the release notes claimed the fix had shipped).
  // KEEP IN SYNC with python/config.py `terminal_offline_mode`. The manual
  // terminal's kill switch: with it on, no terminal screen contacts any
  // provider. Needs to exist HERE or the Settings toggle reads `undefined`
  // and silently falls back to the JSX default.
  terminalOfflineMode: false,
  // KEEP IN SYNC with python/config.py `terminal_paper_mode`. Routes manual
  // orders to the paper simulator. Persisted so the ticket cannot silently
  // come back LIVE after a restart — that is the one direction this switch
  // must never flip on its own.
  // KEEP IN SYNC with python/config.py `terminal_notifications`.
  terminalNotifications: true,
  // KEEP IN SYNC with python/config.py `remote_*`. Remote control via chat
  // bots — all off by default; `remoteTradingEnabled` is deliberately its own
  // switch and must never be turned on as a side effect of enabling remote.
  // KEEP IN SYNC with python/config.py `ai_*`. Off and keyless by default; the
  // keys themselves never live in settings.json. Missing here, the Settings
  // toggle read `undefined` and a preset apply wiped the chosen model.
  aiEnabled: false,
  aiProvider: 'anthropic',
  // One model key for all six providers (replaced aiAnthropicModel /
  // aiOpenaiModel; legacy-settings.ts migrates them).
  aiModel: 'claude-opus-5-5',
  aiWebSearch: false,
  remoteEnabled: false,
  remoteDiscordEnabled: false,
  remoteTelegramEnabled: false,
  remoteTradingEnabled: false,
  remoteMaxOrderUsd: 100,
  // Paper | Live for the whole app. A fresh install starts on Paper; an
  // existing one is migrated in legacy-settings.ts. terminalPaperMode is
  // DERIVED from it on every write (=== 'paper').
  accountMode: 'paper',
  terminalPaperMode: true,
  terminalPaperBankrollUsd: 1000,
  // KEEP IN SYNC with python/config.py `mcp_*`. AI agents over MCP — off by
  // default; live trading and scripts are each their own switch.
  mcpEnabled: false,
  mcpPort: 47635,
  mcpTradeMode: 'paper',
  mcpMaxOrderUsd: 25,
  mcpDailySpendUsd: 100,
  mcpMaxPositions: 10,
  mcpMinEdgeCents: 3,
  mcpDailyLossUsd: 50,
  mcpLiveApproval: true,
  mcpAgents: [defaultAgent('paper')],
  mcpAllowLiveSwitches: false,
  mcpScriptsEnabled: false,
  mcpPaperBankrollUsd: 1000,
  mcpHttpEnabled: false,
  // KEEP IN SYNC with python/config.py `autopilot_*` (workstream B).
  autopilotEnabled: false,
  autopilotIntervalMin: 60,
  autopilotMaxRunsPerDay: 12,
  autopilotDailyTokenBudget: 1_500_000,
  autopilotMaxSteps: 15,
  autopilotMission: '',
  autopilotAgentId: DEFAULT_AGENT_ID,
  // KEEP IN SYNC with python/config.py — knobs with no dedicated control,
  // present so an agent's update_config persists (see sanitizeConfigPatch).
  dbCleanupInterval: 3600,
  crypto15mRulesNo: [],
  crypto15mPollSec: 4,
  crypto15mModelFmMaxBookGapCents: 75,
  crypto15mAutosizeToMinNotional: false,
  scriptHookTimeoutSec: 1,
  scriptMarketLimit: 150,
  scriptMarketMinVolume: 5000,
  // Max real bid/ask spread a decide_market() entry may cross (0 = off). The
  // docs and the shipped example both tell the user to tune this, so it needs a
  // control — hand-editing settings.json is clobbered by the in-memory cache.
  scriptMarketMaxSpreadCents: 2,
};

export const DEFAULT_STATE: AppState = {
  config: { ...DEFAULT_CONFIG },
  activeCryptoProfileId: null,
  customProfiles: [],
  startMinimized: false,
  startWithWindows: false,
  enableDiscordRpc: true,
  acceptedDisclaimer: false,
  uiMode: 'simple',
  windowBounds: null,
  onboardingSeen: 0,
};

let cached: AppState | null = null;

// Provenance of the in-memory state: true when it came from a successfully
// parsed settings file (primary, .bak, or vault) OR the user has since made an
// explicit settings change. When BOTH the primary and the .bak are corrupt we
// fall back to bare defaults — mirroring those to the vault would destroy the
// last good copy there, so save() skips the vault mirror until this is true.
// (Incidental saves like window-bounds updates intentionally do NOT flip it.)
let vaultMirrorAllowed = false;

function ensureDir(): void {
  const d = userDataDir();
  if (!existsSync(d)) mkdirSync(d, { recursive: true });
}

// Every path that builds a config from stored or caller-supplied data goes
// through the migration (legacy-settings.ts), so a retired key can't come
// back in through a profile, a replace or a patch any more than through the
// file, the agent list is always clean, and terminalPaperMode always follows
// accountMode. `live` + `existingUser` only for the running config on load.
function mergeConfig(
  loaded: Partial<TraderConfig> | undefined,
  opts: { live?: boolean; existingUser?: boolean } = {},
): TraderConfig {
  return migrateConfig(loaded, DEFAULT_CONFIG, opts);
}

/** A config built from a partial one with every migration applied (paper
 *  when it names no account mode). For profile snapshots. */
export function mergeConfigPublic(partial: Partial<TraderConfig>): TraderConfig {
  return mergeConfig(partial);
}

export function mergeProfile(loaded: any): Profile | null {
  if (!loaded || typeof loaded !== 'object') return null;
  if (!loaded.id || !loaded.name || !loaded.config) return null;
  return {
    id: String(loaded.id),
    name: String(loaded.name),
    description: loaded.description ? String(loaded.description) : undefined,
    scope: 'crypto',
    createdAt: loaded.createdAt || new Date().toISOString(),
    updatedAt: loaded.updatedAt || new Date().toISOString(),
    builtin: !!loaded.builtin,
    config: mergeConfig(loaded.config),
  };
}

function mergeState(loaded: any): AppState {
  if (!loaded || typeof loaded !== 'object') return { ...DEFAULT_STATE };
  const profiles: Profile[] = Array.isArray(loaded.customProfiles)
    ? loaded.customProfiles.map(mergeProfile).filter((p: Profile | null): p is Profile => p !== null)
    : [];
  return {
    config: mergeConfig(loaded.config, { live: true, existingUser: !!loaded.acceptedDisclaimer }),
    activeCryptoProfileId: loaded.activeCryptoProfileId || null,
    customProfiles: profiles,
    startMinimized: !!loaded.startMinimized,
    startWithWindows: !!loaded.startWithWindows,
    enableDiscordRpc:
      typeof loaded.enableDiscordRpc === 'boolean' ? loaded.enableDiscordRpc : true,
    acceptedDisclaimer: !!loaded.acceptedDisclaimer,
    // New installs default to simple; a settings file from before this key
    // existed belongs to a user who already learned the full cockpit.
    uiMode:
      loaded.uiMode === 'simple' || loaded.uiMode === 'advanced'
        ? loaded.uiMode
        : loaded.acceptedDisclaimer
          ? 'advanced'
          : 'simple',
    windowBounds: loaded.windowBounds || null,
    // Missing in every pre-3.0 settings.json: 0, so an upgrade sees onboarding once.
    onboardingSeen: Number.isInteger(loaded.onboardingSeen) && loaded.onboardingSeen > 0
      ? Math.min(loaded.onboardingSeen, 1000) : 0,
  };
}

// Parse-validate a settings file. Throws on unreadable/unparseable content and
// on JSON that parses but isn't a state object (e.g. a truncated write that
// left "null") — callers treat a throw as "try the next fallback".
function readStateFile(path: string): AppState {
  const parsed = JSON.parse(readFileSync(path, 'utf-8'));
  if (!parsed || typeof parsed !== 'object') throw new Error('not a settings object');
  return mergeState(parsed);
}

export function load(): AppState {
  if (cached) return cached;
  ensureDir();
  const f = settingsFile();
  if (!existsSync(f)) {
    // Not a first run necessarily — a wiped userData looks identical.
    // Recover from the rolling .bak (survives a lone file deletion) or the
    // vault mirror (survives a full userData wipe) before falling back to
    // bare defaults.
    for (const candidate of [`${f}.bak`, vaultSettingsFile()]) {
      if (!candidate || !existsSync(candidate)) continue;
      try {
        cached = readStateFile(candidate);
        vaultMirrorAllowed = true;
        console.error(`settings.json missing — restored from ${candidate}`);
        save(cached);
        return cached;
      } catch (e) {
        console.error(`settings restore from ${candidate} failed:`, e);
      }
    }
    cached = { ...DEFAULT_STATE };
    save(cached);
    return cached;
  }
  try {
    cached = readStateFile(f);
    vaultMirrorAllowed = true;
  } catch (e) {
    console.error('settings parse failed:', e);
    // Preserve the corrupt file for recovery/debugging BEFORE anything overwrites
    // it — otherwise the next save() clobbers it permanently and every custom
    // profile is lost with no trace.
    try {
      const bad = `${f}.corrupt-${Date.now()}`;
      copyFileSync(f, bad);
      console.error(`backed up corrupt settings to ${bad}`);
    } catch (be) {
      console.error('could not back up corrupt settings:', be);
    }
    // Recover from the rolling backup written on the last good save(), then
    // from the vault mirror (outside userData), before giving up to bare
    // defaults. Each candidate is parse-validated before being accepted.
    for (const candidate of [`${f}.bak`, vaultSettingsFile()]) {
      if (!candidate || !existsSync(candidate)) continue;
      try {
        cached = readStateFile(candidate);
        vaultMirrorAllowed = true;
        console.error(`recovered settings from ${candidate}`);
        return cached;
      } catch (re) {
        console.error(`settings recovery from ${candidate} failed:`, re);
      }
    }
    // Nothing recoverable — run on defaults, but keep vaultMirrorAllowed false
    // so save() never mirrors these defaults over a possibly-good vault copy.
    cached = { ...DEFAULT_STATE };
  }
  return cached;
}

export function save(state: AppState): AppState {
  ensureDir();
  cached = state;
  const f = settingsFile();
  const tmp = `${f}.tmp`;
  writeFileSync(tmp, JSON.stringify(state, null, 2), 'utf-8');
  // Roll the previous file to .bak before replacing it, so a later corruption has
  // a known-good fallback (see load()). Only refresh .bak from a file that CURRENTLY
  // parses — never clobber a good backup with an already-corrupt live file.
  try {
    if (existsSync(f)) {
      JSON.parse(readFileSync(f, 'utf-8'));
      copyFileSync(f, `${f}.bak`);
    }
  } catch {
    /* live file missing or corrupt — leave the existing .bak intact */
  }
  renameSync(tmp, f);
  // Mirror to the vault (outside userData) so settings survive a full
  // userData wipe. Best-effort — never let a vault failure break a save.
  // Gated on provenance: state that fell back to bare defaults (both the
  // primary and every backup corrupt) must not overwrite the vault's last
  // good copy. Written tmp+rename (like the python DB vault mirror) so a
  // crash mid-write can't corrupt the vault too.
  try {
    const vf = vaultSettingsFile();
    if (vf && vaultMirrorAllowed) {
      const vtmp = `${vf}.tmp`;
      copyFileSync(f, vtmp);
      renameSync(vtmp, vf);
    }
  } catch {
    /* vault unavailable — primary save already succeeded */
  }
  return state;
}

export function get(): AppState {
  return cached ?? load();
}

export function patchConfig(patch: Partial<TraderConfig>): AppState {
  const cur = get();
  // An explicit config change makes the in-memory state authoritative again,
  // even if this session started from the defaults fallback.
  vaultMirrorAllowed = true;
  const next: AppState = { ...cur, config: mergeConfig({ ...cur.config, ...patch }) };
  return save(next);
}

export function replaceConfig(config: TraderConfig): AppState {
  const cur = get();
  vaultMirrorAllowed = true;
  const next: AppState = { ...cur, config: mergeConfig(config) };
  return save(next);
}

/**
 * Settings → Reset. The trading settings go back to their defaults; the
 * user's own setup does not: named agents (their tokens would be orphaned in
 * the credential store), Autopilot, the AI provider, remote pairing, the
 * account mode and its paper bankrolls (isPersonalKey). A whole-config reset
 * used to switch a Live account back to Paper and delete every agent behind a
 * button on a page about strategy knobs.
 */
export function resetConfig(): AppState {
  const cur = get();
  vaultMirrorAllowed = true;
  const keep = Object.fromEntries(
    Object.entries(cur.config).filter(([k]) => isPersonalKey(k)),
  ) as Partial<TraderConfig>;
  const next: AppState = {
    ...cur, config: mergeConfig({ ...DEFAULT_CONFIG, ...keep }),
    activeCryptoProfileId: null,
  };
  return save(next);
}
