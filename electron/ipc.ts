import { app, BrowserWindow, clipboard, dialog, ipcMain, shell } from 'electron';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type {
  ActionResult,
  AppState,
  BotPosition,
  CredentialsInput,
  CredentialsState,
  McpClientId,
  McpStatus,
  PositionFilter,
  Profile,
  ProfileScope,
  TraderConfig,
} from '../shared/types';
import { setStartWithWindows } from './system/autostart';
import { startDiscordRpc, stopDiscordRpc } from './system/discord';
import { pythonBackend } from './system/python-backend';
import * as accounts from './system/accounts';
import * as mcpClients from './system/mcp-clients';
import * as store from './system/settings-store';
import {
  cleanAgentIdArg, cleanAiKeyProvider, cleanAiProvider, cleanApiKey, cleanClientId,
  cleanConfigPatch, cleanHealthArgs, isPersonalKey, omitForShare, omitPersonal, removedAgentIds,
} from './system/sanitize';
import { agentsOf } from '../shared/agents';
import { ONBOARDING_REVISION } from '../shared/onboarding';
import { parseErrorText } from '../shared/errors';

// ──────────────────────────────────────────────────────────────────────
// Single source of every ipcMain.handle/.on registration. Mirror in
// preload.ts so TypeScript catches channel-name drift at compile time.
// ──────────────────────────────────────────────────────────────────────

const ok = <T>(data?: T, message?: string): ActionResult<T> => ({
  ok: true,
  data,
  message,
});
const err = (message: string): ActionResult => ({ ok: false, message });

function broadcastState(state: AppState): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send('state:changed', state);
    }
  }
}

function genId(): string {
  return `p_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e6).toString(36)}`;
}

// ─── Profile scoping ─────────────────────────────────────────────────────
// A profile saves and applies the crypto engine's tuning and nothing else, so
// it can never touch the user's own setup (agents, AI, remote, account mode).
const PROFILE_SCOPE: ProfileScope = 'crypto';

// The subset of `cfg` a profile should APPLY — only crypto15m* keys, with the
// live on/off switch stripped so applying a profile can't silently start or
// stop the engine.
function scopedApplyPatch(cfg: TraderConfig): Partial<TraderConfig> {
  const patch: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(cfg)) {
    if (k.startsWith('crypto15m') && !isPersonalKey(k)) patch[k] = v;
  }
  delete patch.crypto15mEnabled;
  return patch as Partial<TraderConfig>;
}

/**
 * Push live config from store to backend whenever it changes.
 *
 * `pruneAgentTokens` tells the backend the USER removed named agents in this
 * save, so their token files go too. Set by config:update alone, and only
 * when the patch's agent list dropped an id: a reset or any other push must
 * never prune (a backend that started on defaults would delete real tokens).
 */
async function pushConfigToBackend(opts: { pruneAgentTokens?: boolean } = {}): Promise<void> {
  if (!pythonBackend.isRunning()) return;
  const state = store.get();
  try {
    await pythonBackend.request('setConfig', {
      config: state.config,
      ...(opts.pruneAgentTokens === true ? { pruneAgentTokens: true } : {}),
    });
  } catch (e) {
    /* swallow — backend will be re-pushed when it comes back up */
  }
}

/** An RPC failure as words: "unknown method" (an older backend) becomes the
 *  given fallback; anything else is the backend's own message. */
function rpcMessage(e: unknown, unsupported: string): string {
  const m = String((e as Error)?.message ?? e ?? '');
  return /unknown method|no handler|not found: rpc/i.test(m) ? unsupported : (m || unsupported);
}

/** A backend refusal as the renderer should read it (no stack, no class). */
function backendDown(what: string): never {
  throw new Error(`The trading backend is not running, so ${what}. Start it from the status bar.`);
}

export function registerIpc(): void {
  // ─── App ──────────────────────────────────────────────────────────
  ipcMain.handle('app:version', () => app.getVersion());
  ipcMain.handle('app:openExternal', async (_e, url: string) => {
    // Only ever hand http(s)/mailto to the OS — never file:// or custom
    // schemes (a known Electron remote-code-execution vector).
    if (typeof url === 'string' && /^(https?|mailto):/i.test(url)) {
      await shell.openExternal(url);
    }
  });
  ipcMain.handle('app:showItemInFolder', async (_e, p: string) => {
    shell.showItemInFolder(p);
  });
  ipcMain.handle('app:getUserDataPath', () => app.getPath('userData'));

  // ─── Accounts (multi-instance) ─────────────────────────────────────
  ipcMain.handle('accounts:current', () => accounts.currentAccount());
  ipcMain.handle('accounts:list', () => accounts.listAccounts());
  ipcMain.handle('accounts:create', (_e, name: string) => accounts.createAccount(name));
  ipcMain.handle('accounts:launch', (_e, name: string) => accounts.launchAccount(name));

  // ─── State / config ───────────────────────────────────────────────
  ipcMain.handle('state:get', () => store.get());
  ipcMain.handle('state:setStartMinimized', (_e, v: boolean) => {
    const next = store.save({ ...store.get(), startMinimized: !!v });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setStartWithWindows', (_e, v: boolean) => {
    setStartWithWindows(!!v);
    const next = store.save({ ...store.get(), startWithWindows: !!v });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:setEnableDiscordRpc', (_e, v: boolean) => {
    // This used to persist nothing and do nothing — it returned ok() while
    // Rich Presence stayed on regardless, so a user who turned it off was
    // told it worked and it hadn't. It now actually connects/disconnects,
    // and the persisted value is what `bootstrap()` honours at startup.
    // Not awaited: `client.login()` waits on the Discord socket and already
    // swallows its own failure (no Discord running is the normal case), so
    // the toggle must not block on it.
    const on = !!v;
    const next = store.save({ ...store.get(), enableDiscordRpc: on });
    if (on) void startDiscordRpc();
    else stopDiscordRpc();
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('state:acceptDisclaimer', () => {
    const next = store.save({ ...store.get(), acceptedDisclaimer: true });
    broadcastState(next);
    return ok();
  });
  // View preference only — never pushed to the Python backend (pushConfigToBackend
  // sends state.config, and uiMode deliberately lives outside it).
  ipcMain.handle('state:setUiMode', (_e, v: 'simple' | 'advanced') => {
    const mode = v === 'advanced' ? 'advanced' : 'simple';
    const next = store.save({ ...store.get(), uiMode: mode });
    broadcastState(next);
    return ok();
  });
  // Re-show the first-run onboarding (clears the accepted flag, broadcasts so
  // the renderer pops the modal immediately — no relaunch needed).
  ipcMain.handle('state:resetOnboarding', () => {
    const next = store.save({ ...store.get(), acceptedDisclaimer: false });
    broadcastState(next);
    return ok();
  });
  // No argument: main writes the revision it was built with, so the renderer
  // can't record a future one and skip the next onboarding.
  ipcMain.handle('state:markOnboardingSeen', () => {
    const cur = store.get();
    if ((cur.onboardingSeen ?? 0) >= ONBOARDING_REVISION) return ok();
    const next = store.save({ ...cur, onboardingSeen: ONBOARDING_REVISION });
    broadcastState(next);
    return ok();
  });

  ipcMain.handle('config:get', () => store.get().config);
  ipcMain.handle('config:update', async (_e, raw: Partial<TraderConfig>) => {
    // cleanConfigPatch: a bad mcpAgents is dropped (never "replace every agent
    // with Default"), ids and modes are allowlisted, terminalPaperMode follows
    // accountMode in the store rather than being written directly.
    const patch = cleanConfigPatch(raw as Record<string, unknown>) as Partial<TraderConfig>;
    const before = store.get().config.mcpAgents;
    const next = store.patchConfig(patch);
    broadcastState(next);
    await pushConfigToBackend({
      pruneAgentTokens: 'mcpAgents' in patch && removedAgentIds(before, next.config.mcpAgents).length > 0,
    });
    return next.config;
  });
  // A whole-config replace from the renderer may not rewrite the user's own
  // setup (agents, account mode, AI, remote): only the strategy keys land.
  ipcMain.handle('config:replace', async (_e, cfg: TraderConfig) => {
    const patch = cleanConfigPatch(omitPersonal(cfg ?? {}) as Record<string, unknown>) as Partial<TraderConfig>;
    const next = store.patchConfig(patch);
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  ipcMain.handle('config:reset', async () => {
    const next = store.resetConfig();
    broadcastState(next);
    await pushConfigToBackend();
    return next.config;
  });
  // ─── Profiles ─────────────────────────────────────────────────────
  ipcMain.handle('profiles:list', () => store.get().customProfiles);
  ipcMain.handle('profiles:save', (_e, name: string, description?: string) => {
    if (!name?.trim()) return err('Profile name required');
    const cur = store.get();
    const now = new Date().toISOString();
    const profile: Profile = {
      id: genId(),
      name: name.trim(),
      description: description?.trim() || undefined,
      scope: PROFILE_SCOPE,
      createdAt: now,
      updatedAt: now,
      // Snapshot the strategy; apply is scoped. The user's own setup (agents,
      // AI, remote, the account mode and its paper bankrolls) is never stored
      // in a profile: those keys land as defaults, which apply never reads.
      config: store.mergeConfigPublic(omitPersonal(structuredClone(cur.config)) as Partial<TraderConfig>),
    };
    const next = store.save({
      ...cur,
      customProfiles: [...cur.customProfiles, profile],
      activeCryptoProfileId: profile.id,
    });
    broadcastState(next);
    return ok(profile, `Saved crypto profile "${profile.name}"`);
  });
  ipcMain.handle('profiles:apply', async (_e, id: string) => {
    const cur = store.get();
    const p = cur.customProfiles.find((x) => x.id === id);
    if (!p) return err('Profile not found');
    // Scoped apply: patch ONLY the crypto engine's keys, leaving the live switch
    // and everything else untouched.
    const next = store.patchConfig(scopedApplyPatch(p.config));
    const stateNext = store.save({ ...store.get(), activeCryptoProfileId: id });
    broadcastState(stateNext);
    await pushConfigToBackend();
    return ok(next.config, `Applied crypto profile "${p.name}"`);
  });
  ipcMain.handle('profiles:rename', (_e, id: string, name: string) => {
    if (!name?.trim()) return err('Name required');
    const cur = store.get();
    const idx = cur.customProfiles.findIndex((p) => p.id === id);
    if (idx < 0) return err('Profile not found');
    const updated = [...cur.customProfiles];
    updated[idx] = { ...updated[idx], name: name.trim(), updatedAt: new Date().toISOString() };
    const next = store.save({ ...cur, customProfiles: updated });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('profiles:delete', (_e, id: string) => {
    const cur = store.get();
    const next = store.save({
      ...cur,
      customProfiles: cur.customProfiles.filter((p) => p.id !== id),
      activeCryptoProfileId: cur.activeCryptoProfileId === id ? null : cur.activeCryptoProfileId,
    });
    broadcastState(next);
    return ok();
  });
  ipcMain.handle('profiles:duplicate', (_e, id: string) => {
    const cur = store.get();
    const orig = cur.customProfiles.find((p) => p.id === id);
    if (!orig) return err('Profile not found');
    const dup: Profile = {
      ...orig,
      id: genId(),
      name: `${orig.name} (copy)`,
      config: structuredClone(orig.config),
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    const next = store.save({
      ...cur,
      customProfiles: [...cur.customProfiles, dup],
    });
    broadcastState(next);
    return ok(dup);
  });
  ipcMain.handle('profiles:export', (_e, id: string) => {
    const cur = store.get();
    const p = cur.customProfiles.find((x) => x.id === id);
    if (!p) return err('Profile not found');
    // The file gets shared: the strategy and nothing of the user's own (agents,
    // remote pairing, AI provider, account mode, arm switches).
    const json = JSON.stringify(
      { kryptTraderProfile: 1, profile: { ...p, config: omitForShare(p.config) } },
      null,
      2,
    );
    return ok(json);
  });
  ipcMain.handle('profiles:import', (_e, json: string) => {
    try {
      const parsed = JSON.parse(json);
      // Normalize the imported profile exactly like load() does — fills in any
      // missing config keys and validates id/name/config — instead of trusting
      // the file shape. A foreign/partial profile is rejected, not stored raw.
      // The file came from someone else: keep only what a shared profile may
      // carry, then normalise (defaults fill the rest; apply is scoped anyway).
      const src = parsed?.profile;
      const norm = store.mergeProfile(src && typeof src === 'object' && src.config && typeof src.config === 'object'
        ? { ...src, config: omitForShare(src.config) } : src);
      if (!norm) return err('Not a valid Krypt PolyBot US profile');
      const cur = store.get();
      const dup: Profile = {
        ...norm,
        id: genId(),
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      const next = store.save({ ...cur, customProfiles: [...cur.customProfiles, dup] });
      broadcastState(next);
      return ok(dup, `Imported "${dup.name}"`);
    } catch (e: any) {
      return err(`Invalid profile JSON: ${e?.message || e}`);
    }
  });

  // ─── Credentials ──────────────────────────────────────────────────
  // Backend now returns a per-env "status all" payload. We expose two
  // shapes: `status` (current-env shape, for back-compat with the app
  // state) and `statusAll` (both envs, used by the API Keys page).
  const emptyCred = () => ({
    env: 'mainnet' as const, hasWalletKey: false, hasApiCreds: false,
    address: '', addressPreview: '',
  });
  const emptyAllCreds = () => ({
    current: 'mainnet' as const,
    mainnet: emptyCred(),
  });
  ipcMain.handle('credentials:status', async () => {
    if (!pythonBackend.isRunning()) {
      return emptyCred() satisfies CredentialsState;
    }
    const all = await pythonBackend.request('credentialStatus', {}) as any;
    if (all && all.current && all[all.current]) {
      return all[all.current] as CredentialsState;
    }
    return all as CredentialsState;
  });
  ipcMain.handle('credentials:statusAll', async () => {
    if (!pythonBackend.isRunning()) return emptyAllCreds();
    return await pythonBackend.request('credentialStatus', {});
  });
  ipcMain.handle('credentials:save', async (_e, input: CredentialsInput) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      // Only the two fields the exchange issues (and the env) cross this boundary.
      await pythonBackend.request('setCredentials', {
        keyId: String(input?.keyId ?? '').trim(),
        secretKey: String(input?.secretKey ?? '').trim(),
        ...(input?.env ? { env: input.env } : {}),
      });
      return ok();
    } catch (e: any) {
      return err(parseErrorText(`${e?.message || e}`).message);
    }
  });
  ipcMain.handle('credentials:test', async (_e, env?: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('testCredentials', env ? { env } : {});
      return ok(data, 'Connected to Polymarket US');
    } catch (e: any) {
      // Shown as written on the API Key page, so without the exception's class name.
      return err(parseErrorText(`${e?.message || e}`).message);
    }
  });
  ipcMain.handle('credentials:clear', async (_e, env?: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      await pythonBackend.request('clearCredentials', env ? { env } : {});
      return ok();
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  // ─── Backend ──────────────────────────────────────────────────────
  ipcMain.handle('backend:info', () => pythonBackend.info());
  ipcMain.handle('backend:start', async () => {
    await pythonBackend.start();
    return ok();
  });
  ipcMain.handle('backend:stop', async () => {
    await pythonBackend.stop();
    return ok();
  });
  ipcMain.handle('backend:restart', async () => {
    await pythonBackend.restart();
    return ok();
  });
  ipcMain.handle('backend:runOnce', async (_e, action: string) => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('runOnce', { action });
      return ok(data, (data as any)?.summary || 'Done');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  // ─── Trading control ──────────────────────────────────────────────
  ipcMain.handle('trading:cancelAllOpen', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('cancelAllOpen', {});
      return ok(data, `Canceled ${data.canceled} order(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  // The kill switch the exchange's rulebook requires of automated systems.
  ipcMain.handle('trading:killSwitch', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('killSwitch', {});
      if (data.cancelError) {
        return err(`Kill switch is ON, but cancelling orders failed: ${data.cancelError}. Check polymarket.us.`);
      }
      return ok(data, `Kill switch ON. Engines paused, ${data.canceled} order(s) cancelled.`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('trading:killSwitchClear', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      return ok(await pythonBackend.request('killSwitchClear', {}), 'Kill switch off. Engines stay paused until you resume them.');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  // After the kill switch is cleared the engines stay paused, and this is the way
  // out. It only lifts the pause: each engine still has its own switch.
  ipcMain.handle('trading:resume', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      await pythonBackend.request('pause', { paused: false });
      return ok(undefined, 'Trading resumed');
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('alerts:list', async () => {
    if (!pythonBackend.isRunning()) return null;
    try {
      const r = await pythonBackend.request('alerts', {}, 15_000);
      return Array.isArray(r?.alerts) ? r.alerts : null;
    } catch {
      return null;   // keep showing the last answer
    }
  });
  ipcMain.handle('trading:killSwitchStatus', async () => {
    if (!pythonBackend.isRunning()) return { engaged: false, reason: null };
    try {
      return await pythonBackend.request('killSwitchStatus', {});
    } catch {
      return { engaged: false, reason: null };
    }
  });
  ipcMain.handle('trading:flatten', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('flatten', {});
      return ok(data, `Flattened ${data.closed} order(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });
  ipcMain.handle('trading:status', async () => {
    // The "why isn't it trading" checklist. Backend-down stub keeps the
    // panel rendering (with the real answer: backend not running).
    if (!pythonBackend.isRunning()) {
      return {
        main: [{ id: 'backend', label: 'Backend running', state: 'blocked', reason: 'backend not running' }],
        c15: { enabled: false, live: false, authed: false, env: 'mainnet', blockReasons: {} },
      };
    }
    return await pythonBackend.request('tradingStatus', {});
  });

  // ─── Factory reset ─────────────────────────────────────────────────
  // Wipes ALL trade history, bot runs and P&L snapshots from the local DB. The
  // public market cache is preserved — it's just public data and re-syncing is
  // expensive. Backend reconciles fresh `external` rows for any live positions
  // immediately afterward.
  ipcMain.handle('app:factoryReset', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('factoryReset', {}) as any;
      const total = Object.values(data?.deleted || {}).reduce(
        (a: number, b: any) => a + (Number(b) || 0), 0,
      );
      return ok(data, `Cleared ${total} row(s)`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  // Wipe trade history / P&L / sessions only — keeps settings and the API key.
  // Backend pauses the loop, clears the history tables, then re-baselines the
  // session run on the current balance.
  ipcMain.handle('app:clearHistory', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const data = await pythonBackend.request('clearHistory', {}) as any;
      const total = Object.values(data?.deleted || {}).reduce(
        (a: number, b: any) => a + (Number(b) || 0), 0,
      );
      return ok(data, `Cleared ${total} row(s) of history`);
    } catch (e: any) {
      return err(`${e?.message || e}`);
    }
  });

  // ─── Data ─────────────────────────────────────────────────────────
  // A placeholder, not a reading. `backendDown` is the only honest field
  // here — the zeros exist because AccountSnapshot's numbers are not
  // nullable, and every renderer must show an em dash rather than them.
  const accountPlaceholder = () => ({
    backendDown: true,
    cashUsd: 0, portfolioUsd: 0, totalUsd: 0,
    startBankrollUsd: store.get().config.startBankrollUsd,
    roiPct: 0, realizedPnlUsd: 0, unrealizedPnlUsd: 0,
    openCostUsd: 0, feesUsd: 0, wins: 0, losses: 0, winRate: 0,
    pendingCount: 0, openCount: 0, resolvedCount: 0, totalOpened: 0,
    byNetwork: {
      mainnet: { wins: 0, losses: 0, realizedPnl: 0 },
    },
  });
  ipcMain.handle('data:account', async () => {
    if (!pythonBackend.isRunning()) return accountPlaceholder();
    return await pythonBackend.request('account', {});
  });
  ipcMain.handle('data:accountRefresh', async () => {
    if (!pythonBackend.isRunning()) return accountPlaceholder();
    return await pythonBackend.request('accountRefresh', {}, 20_000);
  });
  ipcMain.handle('data:pnlSeries', async (_e, sinceHours?: number) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('pnlSeries', { sinceHours });
  });
  ipcMain.handle('data:positions', async (_e, filter?: PositionFilter) => {
    if (!pythonBackend.isRunning()) return [];
    return await pythonBackend.request('positions', filter || {});
  });
  ipcMain.handle('data:botRuns', async (_e, env?: string | null, limit?: number) => {
    if (!pythonBackend.isRunning()) {
      return { runs: [], activeRunId: 0, activeRun: null };
    }
    const params: Record<string, unknown> = {};
    if (env) params.env = env;
    if (limit) params.limit = limit;
    return await pythonBackend.request('botRuns', params);
  });

  // ─── 15-minute crypto monitor (read-only) ─────────────────────────
  ipcMain.handle('crypto15m:snapshot', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        fetchedAt: new Date().toISOString(),
        spotOk: false,
        spotSource: 'unavailable',
        constants: {
          timeDelayMin: 8, entryThreshold: 0.95, entryMax: 0.98,
          exitThreshold: 0.4, minDeltaPct: 0, entryDiff: 0.02, directionMode: 'favorite',
          entryStyle: 'maker', hoursStartUtc: 0, hoursEndUtc: 24,
        },
        assets: [],
      };
    }
    return await pythonBackend.request('crypto15m', {});
  });
  ipcMain.handle('crypto15m:status', async () => {
    if (!pythonBackend.isRunning()) {
      return {
        enabled: false, authed: false, trading: false,
        orderSize: 1, maxConcurrent: 7, env: 'mainnet',
        sizing: {
          mode: 'fixed', balancePct: 0.02, maxLossPct: 0, balanceUsd: 0,
          estPriceCents: 0, estContracts: 1, estCostUsd: 0, note: '',
        },
        stats: { openCount: 0, wins: 0, losses: 0, realizedPnlUsd: 0, total: 0 },
        open: [], recent: [],
      };
    }
    return await pythonBackend.request('crypto15mStatus', {});
  });
  ipcMain.handle('crypto15m:history', async (_e, opts?: { limit?: number }) => {
    if (!pythonBackend.isRunning()) return { rows: [] };
    return await pythonBackend.request('c15History', { limit: opts?.limit ?? 300 });
  });
  ipcMain.handle('crypto15m:backtest', async (_e, args?: { sinceDays?: number; config?: Record<string, unknown> }) => {
    if (!pythonBackend.isRunning()) return null;
    // Replays can chew through weeks of ticks — give them a longer leash.
    return await pythonBackend.request('c15Backtest', args || {}, 120_000);
  });
  ipcMain.handle('backtest:collection', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('collectionStats', {});
  });
  // Public reads into the backtest tables; the backend caps what it's asked for.
  ipcMain.handle('backtest:historyStart', async (_e, args?: { days?: number; intervals?: string[] }) => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('historyStart', { days: args?.days, intervals: args?.intervals }, 15_000);
  });
  ipcMain.handle('backtest:historyStatus', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('historyStatus', {}, 15_000);
  });
  ipcMain.handle('backtest:historyCancel', async () => {
    if (!pythonBackend.isRunning()) return null;
    return await pythonBackend.request('historyCancel', {}, 15_000);
  });
  ipcMain.handle('backtest:export', async () => {
    if (!pythonBackend.isRunning()) return null;
    const r = await pythonBackend.request('exportResearch', {}, 120_000) as { dir?: string } | null;
    if (r?.dir) shell.showItemInFolder(r.dir);
    return r;
  });
  // ─── User strategy scripts (Scripts tab) ──────────────────────────
  ipcMain.handle('scripts:list', async () => {
    if (!pythonBackend.isRunning()) return { scripts: [] };
    return await pythonBackend.request('scriptsList', {});
  });
  ipcMain.handle('scripts:save', async (_e, s: Record<string, unknown>) => {
    return await pythonBackend.request('scriptSave', s || {});
  });
  ipcMain.handle('scripts:delete', async (_e, id: string) => {
    return await pythonBackend.request('scriptDelete', { id });
  });
  ipcMain.handle('scripts:setEnabled', async (_e, id: string, enabled: boolean) => {
    return await pythonBackend.request('scriptSetEnabled', { id, enabled });
  });
  ipcMain.handle('scripts:setAssets', async (_e, id: string, assets: string[] | null) => {
    return await pythonBackend.request('scriptSetAssets', { id, assets });
  });
  ipcMain.handle('scripts:exportFile', async (e, name: string, code: string) => {
    const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
    const safe = (name || 'strategy').replace(/[^\w.-]+/g, '-').slice(0, 60);
    const res = await dialog.showSaveDialog(win!, {
      title: 'Export strategy script',
      defaultPath: join(app.getPath('documents'), `${safe}.py`),
      filters: [{ name: 'Python script', extensions: ['py'] }],
    });
    if (res.canceled || !res.filePath) return err('canceled');
    writeFileSync(res.filePath, code, 'utf-8');
    shell.showItemInFolder(res.filePath);
    return ok(res.filePath);
  });
  ipcMain.handle('scripts:importFile', async (e) => {
    const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
    const res = await dialog.showOpenDialog(win!, {
      title: 'Import strategy script',
      properties: ['openFile'],
      filters: [{ name: 'Python script', extensions: ['py', 'txt'] }],
    });
    if (res.canceled || !res.filePaths?.[0]) return err('canceled');
    try {
      const code = readFileSync(res.filePaths[0], 'utf-8');
      // Imported code is never executed here. It lands as a new, disabled,
      // shadow-mode script and is risk-audited before the user can enable it
      // — which matters more now that scripts are full Python.
      if (code.length > 128 * 1024) return err('Script exceeds 128KB');
      return ok(code);
    } catch (e2: any) {
      return err(e2?.message || 'Could not read that file');
    }
  });
  ipcMain.handle('scripts:setDryRun', async (_e, id: string, dryRun: boolean) => {
    return await pythonBackend.request('scriptSetDryRun', { id, dryRun });
  });
  ipcMain.handle('scripts:shadowOrders', async (_e, id: string, limit?: number) => {
    return await pythonBackend.request('scriptShadowOrders', { id, limit });
  });
  ipcMain.handle('scripts:validate', async (_e, code: string) => {
    return await pythonBackend.request('scriptValidate', { code });
  });
  ipcMain.handle('scripts:backtest', async (_e, args?: Record<string, unknown>) => {
    if (!pythonBackend.isRunning()) return null;
    // Script replays chew through the same tick corpus as config replays.
    return await pythonBackend.request('scriptBacktest', args || {}, 120_000);
  });
  ipcMain.handle('scripts:contextPack', async () => {
    // 60s leash: generation is sub-second, but right after a cold boot the
    // backend may still be chewing through startup work.
    return await pythonBackend.request('scriptContextPack', {}, 60_000);
  });
  ipcMain.handle('scripts:docs', async () => {
    return await pythonBackend.request('scriptApiDocs', {}, 60_000);
  });
  // Save the AI context pack as a .txt (some AI apps take file uploads more
  // gracefully than a 10k-character paste).
  ipcMain.handle('scripts:exportPack', async (e) => {
    if (!pythonBackend.isRunning()) return err('Engine not running');
    const r = (await pythonBackend.request('scriptContextPack', {})) as { text?: string } | null;
    if (!r?.text) return err('Context pack generation failed');
    const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
    const res = await dialog.showSaveDialog(win!, {
      title: 'Save AI context pack',
      defaultPath: join(app.getPath('documents'), 'krypt-ai-context-pack.txt'),
      filters: [{ name: 'Text file', extensions: ['txt'] }],
    });
    if (res.canceled || !res.filePath) return err('canceled');
    writeFileSync(res.filePath, r.text, 'utf-8');
    shell.showItemInFolder(res.filePath);
    return ok(res.filePath);
  });

  // Resolve the public Polymarket browser URL for a market (used to make the
  // ticker in the positions / trade-history tables a clickable deep link).
  ipcMain.handle(
    'polymarket:marketUrl',
    async (_e, args?: { eventTicker?: string; ticker?: string; env?: string }) => {
      if (!pythonBackend.isRunning()) return { url: '' };
      return await pythonBackend.request('polymarketUrl', args || {});
    },
  );
  // ─── Terminal (manual) ────────────────────────────────────────────
  // Every channel below names an ENTITY (a market slug), a column, a side or
  // an interval. None of them accepts a URL or a hostname: the host table
  // lives in the backend's terminal.py and nothing from the renderer reaches
  // it. That is the structural fix for SSRF — a URL validator is the fragile
  // one, because it has to be right every single time.
  //
  // `offline` is what each channel returns when the backend is not running.
  // It is shaped like a real answer with HONEST NULLS, so the UI renders em
  // dashes and a reason instead of a screen full of confident zeroes.
  const termDown = (what: string) =>
    `The trading backend is not running, so ${what} could not be read. ` +
    `Start it from the status bar.`;

  const term = async <T>(method: string, params: unknown, offline: T): Promise<T> => {
    if (!pythonBackend.isRunning()) return offline;
    return (await pythonBackend.request(method, params ?? {})) as T;
  };

  ipcMain.handle(
    'terminal:discover',
    async (_e, args?: { column?: string; limit?: number; filters?: unknown }) =>
      term('terminalDiscover', args, {
        column: args?.column ?? 'volume', rows: [],
        fetchedAt: new Date().toISOString(),
        skippedForUnknown: 0, excluded: 0,
        note: termDown('the market list'),
      }),
  );
  ipcMain.handle(
    'terminal:search',
    async (_e, args?: { query?: string; limit?: number }) =>
      term('terminalSearch', args, {
        column: 'volume', rows: [], fetchedAt: new Date().toISOString(),
        skippedForUnknown: 0, excluded: 0, note: termDown('search'),
      }),
  );
  ipcMain.handle(
    'terminal:market',
    async (_e, args?: { conditionId?: string }) =>
      term('terminalMarket', args, { market: null }),
  );
  ipcMain.handle(
    'terminal:book',
    async (_e, args?: { conditionId?: string; side?: string }) =>
      term('terminalBook', args, {
        conditionId: args?.conditionId ?? '', tokenSide: args?.side ?? 'yes',
        bids: [], asks: [], bestBid: null, bestAsk: null,
        spreadCents: null, midCents: null, bidDepthUsd: null, askDepthUsd: null,
        available: false, fetchedAt: new Date().toISOString(), source: 'venue',
        note: termDown('the order book'),
      }),
  );
  ipcMain.handle(
    'terminal:history',
    async (_e, args?: { conditionId?: string; side?: string; interval?: string }) =>
      term('terminalHistory', args, {
        conditionId: args?.conditionId ?? '', side: args?.side ?? 'yes',
        interval: args?.interval ?? '1d', points: [], source: 'venue',
        ownFeed: false, thin: false, note: termDown('price history'),
      }),
  );
  ipcMain.handle(
    'terminal:resolution',
    async (_e, args?: { conditionId?: string }) =>
      term('terminalResolution', args, {
        conditionId: args?.conditionId ?? '', checks: [], resolvedCount: 0,
        totalCount: 0, score: null,
        scoreNote: termDown('the resolution checks'),
        resolverName: null, resolutionSource: null,
        fetchedAt: new Date().toISOString(),
      }),
  );
  ipcMain.handle(
    'terminal:micro',
    async (_e, args?: { conditionId?: string; side?: string }) =>
      term('terminalMicro', args, {
        conditionId: args?.conditionId ?? '', side: args?.side ?? 'yes',
        points: [], medianSpreadCents: null, sampleCount: 0,
        source: 'ws', note: termDown('our own feed'),
      }),
  );
  ipcMain.handle(
    'terminal:related',
    async (_e, args?: { conditionId?: string; limit?: number }) =>
      term('terminalRelated', args, {
        conditionId: args?.conditionId ?? '', eventSlug: null, rows: [],
        sumProbability: null,
        fetchedAt: new Date().toISOString(),
        note: termDown('related markets'),
      }),
  );
  ipcMain.handle(
    'terminal:crossVenue',
    async (_e, args?: { conditionId?: string; interval?: string }) =>
      term('terminalCrossVenue', args, {
        conditionId: args?.conditionId ?? '', interval: args?.interval ?? '1d',
        polymarket: null, kalshi: null, match: null,
        spreadCents: null, spreadNote: null, series: [], overlapPeriods: 0,
        historyNote: null,
        venueNote: '', caveats: [],
        fetchedAt: new Date().toISOString(),
        note: termDown('the Kalshi comparison'),
      }),
  );
  // ── AI analysis ──────────────────────────────────────────────────────
  // On-demand only. No polling channel exists here on purpose: every call
  // spends the user's own provider credit, so the only thing that can start
  // one is a click.
  ipcMain.handle('ai:status', async () =>
    term('aiStatus', {}, {
      enabled: false, provider: 'anthropic' as const, model: '', webSearch: false,
      offline: false, models: {}, keys: {},
    }),
  );
  ipcMain.handle(
    'ai:analyze',
    async (_e, args?: { conditionId?: string }) =>
      term('aiAnalyze', args, {
        ok: false, provider: 'anthropic', model: '',
        askedAt: new Date().toISOString(), latencyMs: null,
        webSearch: false, sources: [],
        summary: null, resolutionNotes: [], drivers: [],
        unknowns: [], reasoning: [], lean: null, confidence: null,
        redactions: 0,
        reason: termDown('the analysis'),
        fix: null,
      }),
  );
  // Keys are verified against the provider BEFORE they are stored, by the
  // backend. The provider is allowlisted here (an unknown id must never file
  // a key under the wrong company's name), and an invalid paste is refused
  // rather than collapsing to '' — which would mean "delete my saved key".
  ipcMain.handle(
    'ai:setKey',
    async (_e, args?: { provider?: string; key?: string }) => {
      if (!pythonBackend.isRunning()) {
        return { ok: false, message: 'The trading backend is not running, so the key could not be verified.' };
      }
      const provider = cleanAiKeyProvider(args?.provider);
      if (!provider) return { ok: false, message: 'That AI provider does not take a key.' };
      const key = cleanApiKey(args?.key);
      if (key === null) return { ok: false, message: 'That does not look like an API key — nothing was saved.' };
      const res = await pythonBackend.request('aiSetKey', { provider, key }, 30_000);
      // Never the key itself, only that one was set or cleared.
      appendLog({ ts: new Date().toISOString(), level: 'INFO', source: 'app',
        msg: `${provider} API key ${key ? 'saved' : 'cleared'}` });
      return res;
    },
  );
  ipcMain.handle(
    'ai:listModels',
    async (_e, args?: { provider?: string }) => {
      const provider = cleanAiProvider(args?.provider);
      if (!provider) return { ok: false, models: [], message: 'Unknown AI provider.' };
      return term('aiListModels', { provider }, {
        ok: false, models: [],
        message: termDown('the model list'),
      });
    },
  );
  // "Test connection". Free endpoints only (model listings), so it never
  // bills the user. The renderer names a provider id and nothing else.
  ipcMain.handle('ai:checkProvider', async (_e, args?: { provider?: string }) => {
    const provider = cleanAiProvider(args?.provider);
    if (!provider) return { ok: false, provider: 'anthropic', message: 'Unknown AI provider.' };
    if (!pythonBackend.isRunning()) return { ok: false, provider, message: termDown('the provider check') };
    try {
      return await pythonBackend.request('aiCheckProvider', { provider }, 45_000);
    } catch (e) {
      return { ok: false, provider, message: rpcMessage(e, 'This version of the engine cannot test a provider yet.') };
    }
  });
  // Every agent forecast, scored against the market once it resolves.
  ipcMain.handle('ai:scoreboard', async (_e, args?: { recent?: number }) => {
    if (!pythonBackend.isRunning()) backendDown('the forecast scoreboard could not be read');
    const recent = Math.min(200, Math.max(1, Math.floor(Number(args?.recent) || 40)));
    return await pythonBackend.request('aiScoreboard', { recent }, 20_000);
  });

  ipcMain.handle('terminal:health', async () =>
    term('terminalHealth', {}, {
      providers: [], offline: false, fetchedAt: new Date().toISOString(),
    }),
  );
  ipcMain.handle(
    'terminal:watchlist',
    async (_e, args?: { action?: string; conditionId?: string; title?: string }) =>
      term('terminalWatchlist', args, { entries: [] }),
  );

  // ── the money-spending half ───────────────────────────────────────
  ipcMain.handle('terminal:preview', async (_e, order?: unknown) =>
    term('terminalPreview', { order }, {
      conditionId: '', side: 'yes', action: 'buy',
      priceCents: null, requestedPriceCents: 0, size: null, requestedSize: 0,
      costUsd: null, fillableSize: null, estimatedAvgCents: null,
      slippageCents: null, feeUsd: null, maxPayoutUsd: null, maxLossUsd: null,
      tickSize: null, minOrderSize: null,
      bestBidCents: null, bestAskCents: null, crossesNow: null,
      availableUsd: null,
      warnings: [],
      blockers: [termDown('this order')],
    }),
  );
  ipcMain.handle('terminal:place', async (_e, order?: unknown) =>
    term('terminalPlace', { order }, {
      ok: false, orderId: null, status: null, filledSize: null,
      avgPriceCents: null, localId: null,
      message: termDown('this order'),
    }),
  );
  ipcMain.handle('terminal:cancel', async (_e, orderId?: string) =>
    term('terminalCancel', { orderId }, {
      ok: false, message: termDown('the cancel'),
    }),
  );
  // `conditionId` is optional: with it the backend narrows the list to one
  // market and stamps each row with the touch it needs to reach to fill, which
  // is what the Market page's working-order rows read. Without it this is the
  // wallet-wide list Track shows, and no book is read.
  ipcMain.handle('terminal:orders', async (_e, args?: { conditionId?: string }) =>
    term('terminalOrders', args ?? {}, {
      orders: [], fetchedAt: new Date().toISOString(),
      note: termDown('your resting orders'),
    }),
  );
  // Flatten. Two channels over one backend method: the preview spends
  // nothing and is ungated, the flatten signs an order for every position you
  // own and passes the same gates as `terminal:place`. Offline, both come
  // back saying nothing happened — never an empty account, which would read as
  // "you hold nothing" to both the panel and the user.
  ipcMain.handle('terminal:flattenPreview', async () =>
    term('terminalFlattenWallet', { preview: true }, {
      ok: false, positions: [], count: 0, skipped: 0, settledSkipped: 0,
      proceedsUsd: null, engineTracked: null, paper: false,
      message: termDown('what a flatten would sell'),
    }),
  );
  ipcMain.handle('terminal:flattenWallet', async () =>
    term('terminalFlattenWallet', {}, {
      ok: false, attempted: 0, sold: 0, refused: 0, skipped: 0,
      settledSkipped: 0,
      proceedsUsd: null, engineTrackedSold: null, reason: 'flatten',
      results: [], message: termDown('the flatten'),
    }),
  );
  // One-click exit out of ONE position. Two channels over two backend
  // methods: the preview spends nothing, the sell signs an order and passes
  // the same gates as `terminal:place`. Offline, the preview comes back with
  // a BLOCKER rather than an empty position — "we could not ask" must never
  // render as "you hold nothing".
  ipcMain.handle('terminal:sellPreview', async (_e, order?: unknown) =>
    term('terminalSellPreview', { order }, {
      conditionId: '', side: 'yes', sideLabel: null, title: null,
      paper: false, positionSize: null, requestedSize: null,
      size: null, fillableSize: null, bestBidCents: null, limitCents: null,
      estimatedAvgCents: null, worstFillCents: null, maxSlippageCents: 5,
      proceedsUsd: null, feeUsd: null, netProceedsUsd: null,
      avgCostCents: null, basisSource: null, costBasisUsd: null,
      estimatedPnlUsd: null, markCents: null, settled: false,
      resolvedOutcome: null, minOrderSize: null, tickSize: null,
      warnings: [],
      blockers: [termDown('this position')],
      note: 'Nothing was priced.',
    }),
  );
  ipcMain.handle('terminal:sellPosition', async (_e, order?: unknown) =>
    term('terminalSellPosition', { order }, {
      ok: false, orderId: null, status: null, localId: null,
      sentSize: null, limitCents: null, filledSize: null, avgPriceCents: null,
      proceedsUsd: null, killedSize: null, estimatedProceedsUsd: null,
      preview: null,
      message: termDown('this sell'),
    }),
  );
  ipcMain.handle('terminal:portfolio', async () =>
    term('terminalPortfolio', {}, {
      // Offline the account was never asked, so the empty list below must not
      // be read as "you hold nothing".
      positions: [], walletReadable: false,
      totalValueUsd: null, totalCostUsd: null,
      totalUnrealizedPnlUsd: null, realizedPnlUsd: null, settledPnlUsd: null,
      venueCashPnlUsd: null,
      unreconciledCount: 0, unreconciledNote: termDown('your positions'),
      venueBasisCount: 0, settledWorthlessCount: 0,
      cashUsd: null, fetchedAt: new Date().toISOString(),
      costBasisConvention: 'average',
    }),
  );
  // The settled RECORD — the ledger, not the live account. Its own channel so the
  // positions poll never pays for the ledger walk. Offline it comes back with
  // no rows AND a note saying why, never as an empty record, which would read
  // as "you have never settled anything".
  ipcMain.handle('terminal:settled', async () =>
    term('terminalSettled', {}, {
      rows: [], totals: {
        won: 0, lost: 0, bothSides: 0, closedOut: 0, unknown: 0,
        markets: 0, settledMarkets: 0, rows: 0, pnlUsd: null,
        pnlMissingRows: 0, feesUsd: null, feesKnown: true, unknownRows: 0,
      },
      walletReadable: false, fetchedAt: new Date().toISOString(),
      note: termDown('your settled record'),
    }),
  );
  ipcMain.handle('terminal:fills', async (_e, limit?: number) =>
    term('terminalFills', { limit }, {
      fills: [], fetchedAt: new Date().toISOString(),
      note: termDown('your fill ledger'),
    }),
  );
  ipcMain.handle('terminal:orderLog', async (_e, limit?: number) =>
    term('terminalOrderLog', { limit }, {
      orders: [], fetchedAt: new Date().toISOString(),
    }),
  );
  ipcMain.handle('terminal:reconcile', async () =>
    term('terminalReconcile', {}, {
      inserted: 0, matched: 0, corrected: 0, unmapped: 0,
      scanned: 0, complete: false,
      note: termDown('the reconcile'),
    }),
  );
  ipcMain.handle(
    'terminal:standing',
    async (_e, args?: { action?: string; id?: string; armed?: boolean; item?: unknown }) =>
      term('terminalStanding', args, {
        instructions: [], error: termDown('your standing instructions'),
      }),
  );

  // ── paper trading ─────────────────────────────────────────────────
  // Separate channels from the live ones. A single entry point with a
  // `paper: true` flag would be one bad `if` away from spending real money in
  // what the user believes is a simulator, so the two never share a path.
  ipcMain.handle('terminal:paperPlace', async (_e, order?: unknown) =>
    term('terminalPaperPlace', { order }, {
      ok: false, localId: null, status: null, filledSize: null,
      avgPriceCents: null, message: termDown('this paper order'),
    }),
  );
  ipcMain.handle('terminal:paperPortfolio', async () =>
    term('terminalPaperPortfolio', {}, {
      positions: [], walletReadable: false,
      totalValueUsd: null, totalCostUsd: null,
      totalUnrealizedPnlUsd: null, realizedPnlUsd: null, settledPnlUsd: null,
      unreconciledCount: 0, unreconciledNote: termDown('your paper account'),
      cashUsd: null, bankrollUsd: 0, equityUsd: null, returnPct: null,
      fetchedAt: new Date().toISOString(), costBasisConvention: 'average',
    }),
  );
  ipcMain.handle('terminal:paperOrders', async (_e, limit?: number) =>
    term('terminalPaperOrders', { limit }, {
      orders: [], fetchedAt: new Date().toISOString(),
    }),
  );
  ipcMain.handle('terminal:paperCancel', async (_e, localId?: number) =>
    term('terminalPaperCancel', { localId }, {
      ok: false, message: termDown('the cancel'),
    }),
  );
  ipcMain.handle('terminal:paperReset', async () =>
    term('terminalPaperReset', {}, {
      cleared: 0, message: termDown('the reset'),
    }),
  );

  // ─── Remote control (Discord / Telegram) ──────────────────────────
  // Setup only. The bot TOKEN goes straight to the backend, which stores it
  // through the same encrypted writer as the exchange API key — it is never written
  // to settings.json and never comes back out to the renderer.
  const remoteDown = {
    discord: { connected: false, lastError: null, paired: [], hasToken: false },
    telegram: { connected: false, lastError: null, paired: [], hasToken: false },
    audit: [], fetchedAt: new Date().toISOString(),
  };
  ipcMain.handle('remote:status', async () =>
    term('remoteStatus', {}, remoteDown));
  ipcMain.handle(
    'remote:setToken',
    async (_e, args?: { platform?: string; token?: string }) =>
      term('remoteSetToken', args, {
        ok: false, message: termDown('the bot token'),
      }),
  );
  ipcMain.handle('remote:pairCode', async (_e, args?: { platform?: string }) =>
    term('remotePairCode', args, { code: '', expiresAt: '', ttlSec: 0 }));
  ipcMain.handle(
    'remote:addPaired',
    async (_e, args?: { platform?: string; id?: string; label?: string }) =>
      term('remoteAddPaired', args, {
        paired: [], error: termDown('the pairing'),
      }),
  );
  ipcMain.handle(
    'remote:unpair',
    async (_e, args?: { platform?: string; id?: string }) =>
      term('remoteUnpair', args, { paired: [] }),
  );
  ipcMain.handle('remote:sync', async () => term('remoteSync', {}, remoteDown));
  ipcMain.handle('remote:test', async () =>
    term('remoteTest', {}, { sent: 0, message: termDown('the test message') }));

  // ─── AI agents (MCP) ──────────────────────────────────────────────
  // Setup and oversight only. Agents reach the backend over loopback with a
  // bearer token; nothing they send passes through the renderer.
  //
  // The token never reaches the renderer either. Client configs are
  // token-less stdio (`--mcp --agent <id>`: the relay reads that agent's
  // token from the encrypted store). The ONE thing that carries a token — the
  // HTTP API snippet — goes backend -> main -> clipboard, and the renderer
  // gets `{ ok }` back. There is no reveal.
  const mcpDown = (): McpStatus => ({
    enabled: false, running: false, port: 0, url: '', lastError: termDown('the AI agent server'),
    hasToken: false, tokenMasked: null, sessions: [], toolCount: 0,
    spentTodayUsd: null, audit: [], agents: [], pendingApprovals: [],
    fetchedAt: new Date().toISOString(),
  });
  /** The agent a click names, from THIS config — or null. Never defaulted. */
  const agentFor = (raw: unknown): { id: string; name: string } | null => {
    const id = cleanAgentIdArg(raw);
    if (!id) return null;
    const a = agentsOf(store.get().config).find((x) => x.id === id);
    return a ? { id: a.id, name: a.name } : null;
  };
  const noAgent = { ok: false, message: 'Unknown agent. Refresh the page and try again.' };

  ipcMain.handle('mcp:status', async () => term('mcpStatus', {}, mcpDown()));
  ipcMain.handle('mcp:sync', async () => term('mcpSync', {}, mcpDown()));
  // Every token op names the agent it is for; each agent has its own token.
  ipcMain.handle('mcp:rotateToken', async (_e, args?: { agentId?: string }) => {
    const a = agentFor(args?.agentId);
    if (!a) throw new Error(noAgent.message);
    return term('mcpRotateToken', { agentId: a.id }, mcpDown());
  });
  ipcMain.handle('mcp:paper', async (_e, args?: { agentId?: string; reset?: boolean; limit?: number }) => {
    const id = cleanAgentIdArg(args?.agentId ?? 'default');
    if (!id) return { portfolio: null, orders: null };
    const limit = Math.min(200, Math.max(1, Math.floor(Number(args?.limit) || 50)));
    return term('mcpPaper', { agentId: id, reset: args?.reset === true, limit }, { portfolio: null, orders: null });
  });
  ipcMain.handle('mcp:decide', async (_e, args?: { id?: number; approve?: boolean }) => {
    if (!pythonBackend.isRunning()) return { ok: false, message: termDown('the approval') };
    const id = Math.floor(Number(args?.id));
    if (!Number.isFinite(id) || id <= 0) return { ok: false, message: 'No such agent order.' };
    // 60s: approving runs the full send path (fresh market, book, order,
    // fills) after the re-vet. Only a literal true approves.
    return await pythonBackend.request('mcpDecide', { id, approve: args?.approve === true }, 60_000);
  });
  ipcMain.handle('mcp:settleUnknown', async (_e, args?: { id?: number }) => {
    if (!pythonBackend.isRunning()) return { ok: false, message: termDown('the check') };
    const id = Math.floor(Number(args?.id));
    if (!Number.isFinite(id) || id <= 0) return { ok: false, message: 'No such agent order.' };
    // 60s: it reads the fill ledger and asks the exchange about the order.
    return await pythonBackend.request('mcpSettleUnknown', { id }, 60_000);
  });
  ipcMain.handle('mcp:activity', async (_e, args?: { limit?: number }) => {
    const limit = Math.min(500, Math.max(1, Math.floor(Number(args?.limit) || 100)));
    return term('mcpActivity', { limit }, { orders: [], pending: [] });
  });
  // Which agent clients connected this session: backend memory only, so the
  // onboarding and Connect panels can poll it without network reads.
  ipcMain.handle('mcp:seen', async () => {
    if (!pythonBackend.isRunning()) return { clients: [] };
    try {
      return await pythonBackend.request('mcpSeen', {}, 10_000);
    } catch {
      return { clients: [] };   // an older backend has no mcpSeen
    }
  });
  ipcMain.handle('mcp:copyHttpSnippet', async (_e, args?: { agentId?: string }) => {
    const a = agentFor(args?.agentId);
    if (!a) return noAgent;
    if (!pythonBackend.isRunning()) return { ok: false, message: termDown('the HTTP snippet') };
    try {
      const res = await pythonBackend.request<{ text?: string }>('mcpHttpSnippet', { agentId: a.id }, 20_000);
      if (!res?.text) return { ok: false, message: 'The backend returned no snippet.' };
      clipboard.writeText(res.text);
      return { ok: true, message: `Copied the HTTP examples for ${a.name} — they include its token, so paste them only where you mean to.` };
    } catch (e) {
      return { ok: false, message: rpcMessage(e, 'This version of the engine cannot build the HTTP snippet yet.') };
    }
  });
  ipcMain.handle('mcp:clients', async (_e, args?: { agentId?: string }) => {
    const a = agentFor(args?.agentId ?? 'default');
    if (!a) return [];
    return mcpClients.listClients(pythonBackend.relaySpec(), a);
  });
  ipcMain.handle('mcp:install', async (_e, args?: { clientId?: string; agentId?: string }) => {
    const client = cleanClientId(args?.clientId);
    if (!client) return { ok: false, message: 'No client selected.' };
    const a = agentFor(args?.agentId);
    if (!a) return noAgent;
    const out = client === 'claude-code'
      ? await mcpClients.installClaudeCode(pythonBackend.relaySpec(), a)
      : mcpClients.install(client, pythonBackend.relaySpec(), a);
    // Paths only — and the entry holds no token anyway.
    appendLog({ ts: new Date().toISOString(), level: out.ok ? 'INFO' : 'WARN', source: 'app',
      msg: `${client} config for agent ${a.id} ${out.ok ? `written: ${(out.files ?? []).join(', ')}` : 'not written'}`
        + (out.backups?.length ? ` (backup: ${out.backups.join(', ')})` : '') });
    return out;
  });
  ipcMain.handle('mcp:uninstall', async (_e, args?: { clientId?: string; agentId?: string }) => {
    const client = cleanClientId(args?.clientId);
    if (!client) return { ok: false, message: 'No client selected.' };
    // A deleted agent's entry may still need removing: the id is enough.
    const id = cleanAgentIdArg(args?.agentId);
    if (!id) return noAgent;
    const a = agentFor(id) ?? { id, name: '' };
    return client === 'claude-code' ? mcpClients.uninstallClaudeCode(a) : mcpClients.uninstall(client, a);
  });
  ipcMain.handle('mcp:copyConfig', async (_e, args?: { clientId?: string; agentId?: string }) => {
    const client = cleanClientId(args?.clientId);
    if (!client) return { ok: false, message: 'No client selected.' };
    const a = agentFor(args?.agentId);
    if (!a) return noAgent;
    const spec = pythonBackend.relaySpec();
    if (!spec) return { ok: false, message: 'The backend binary could not be located.' };
    clipboard.writeText(mcpClients.snippetFor(client, spec, a));
    return { ok: true, message: 'Copied. It holds no token: the app hands the agent its token itself.' };
  });
  // Opens the folder the client reads its config from. Picks the folder;
  // takes no path.
  ipcMain.handle('mcp:openConfigFolder', async (_e, args?: { clientId?: string }) => {
    const client = cleanClientId(args?.clientId);
    if (!client) return { ok: false, message: 'No client selected.' };
    const dir = mcpClients.configFolder(client, mcpClients.systemPathEnv());
    if (!dir || !existsSync(dir)) {
      return { ok: false, message: 'That app has not created its settings folder yet. Open it once first, or use Add.' };
    }
    const fail = await shell.openPath(dir);
    return fail ? { ok: false, message: fail } : { ok: true };
  });
  // Close one agent's PAPER positions at the bid (offered before deleting or
  // switching off an agent that still holds some). User-only, never an MCP
  // tool; the backend handler touches nothing live.
  ipcMain.handle('agents:closePaper', async (_e, args?: { agentId?: string }) => {
    const id = cleanAgentIdArg(args?.agentId);
    if (!id) return noAgent;
    if (!pythonBackend.isRunning()) return { ok: false, message: termDown('its paper positions') };
    try {
      return await pythonBackend.request('mcpAgentClosePaper', { agentId: id }, 60_000);
    } catch (e) {
      return { ok: false, message: rpcMessage(e, 'This version of the engine cannot close an agent\'s paper positions yet.') };
    }
  });

  // ─── Connection health ────────────────────────────────────────────
  // `deep` (network checks) only on the user's click; the panel's open-time
  // run is local state.
  ipcMain.handle('health:check', async (_e, args?: unknown) => {
    if (!pythonBackend.isRunning()) return null;
    const { deep } = cleanHealthArgs(args);
    try {
      return await pythonBackend.request('healthCheck', { deep }, deep ? 90_000 : 20_000);
    } catch {
      return null;   // an older backend has no healthCheck
    }
  });

  // ─── Autopilot ────────────────────────────────────────────────────
  // Status is read-only; the one action is "run now", which bills a run, so
  // it is a deliberate click and the backend re-checks every budget.
  ipcMain.handle('autopilot:status', async () => {
    if (!pythonBackend.isRunning()) return null;
    try {
      return await pythonBackend.request('autopilotStatus', {}, 20_000);
    } catch {
      return null;
    }
  });
  ipcMain.handle('autopilot:runNow', async () => {
    if (!pythonBackend.isRunning()) return { ok: false, message: termDown('Autopilot') };
    try {
      return await pythonBackend.request('autopilotRunNow', {}, 20_000);
    } catch (e) {
      return { ok: false, message: rpcMessage(e, 'This version of the engine has no Autopilot yet.') };
    }
  });

  // ─── The account's paper book ─────────────────────────────────────
  // Status is local (no network); reset empties the paper book back to the
  // bankroll and touches nothing live. The renderer confirms first.
  ipcMain.handle('paper:status', async () => {
    if (!pythonBackend.isRunning()) return null;
    try {
      return await pythonBackend.request('paperStatus', {}, 20_000);
    } catch {
      return null;
    }
  });
  ipcMain.handle('paper:reset', async () => {
    if (!pythonBackend.isRunning()) return err('Backend not running');
    try {
      const r = await pythonBackend.request('paperReset', {}, 30_000);
      return ok(r, 'Paper account reset to its starting balance.');
    } catch (e: any) {
      return err(rpcMessage(e, 'The paper account could not be reset.'));
    }
  });

  // ─── Logs ─────────────────────────────────────────────────────────
  ipcMain.handle('logs:tail', async (_e, _limit?: number) => {
    // In-memory ring buffer maintained via the on-log handler in main.ts.
    return logsBuffer.slice(-1 * (_limit || 500));
  });
  ipcMain.handle('logs:clear', () => {
    logsBuffer.length = 0;
    return ok();
  });
  ipcMain.handle('logs:openFolder', async () => {
    const p = join(app.getPath('userData'), 'logs');
    if (existsSync(p)) shell.openPath(p);
  });

  // ─── Window ───────────────────────────────────────────────────────
  ipcMain.on('window:minimize', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.minimize();
  });
  ipcMain.on('window:maximize', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!w) return;
    if (w.isMaximized()) w.unmaximize();
    else w.maximize();
  });
  ipcMain.on('window:close', (e) => {
    BrowserWindow.fromWebContents(e.sender)?.close();
  });
  ipcMain.handle('window:isMaximized', (e) => {
    return BrowserWindow.fromWebContents(e.sender)?.isMaximized() ?? false;
  });
  // Can anyone see the window? main also pushes window:visibility on
  // minimize / hide / restore / show (main.ts), so the renderer can park its
  // animations and the Agent Hub its render loop.
  ipcMain.handle('window:isVisible', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    return !!w && w.isVisible() && !w.isMinimized();
  });
}

// In-memory ring buffer for logs (last 5000 lines). Filled by main.ts
// via pythonBackend.onLog().
const MAX_LOGS = 5000;
export const logsBuffer: any[] = [];

export function appendLog(entry: any): void {
  logsBuffer.push(entry);
  if (logsBuffer.length > MAX_LOGS) logsBuffer.shift();
}

// Expose to main.ts so it can pipe events out.
export { broadcastState };
