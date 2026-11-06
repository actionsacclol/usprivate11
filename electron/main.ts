import { app, BrowserWindow, Notification, screen, session, shell } from 'electron';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { appendLog, broadcastState, registerIpc } from './ipc';
import { applyAccountFromArgv } from './system/accounts';
import { installAppMenu } from './system/app-menu';
import { setStartWithWindows } from './system/autostart';
import { startDiscordRpc, stopDiscordRpc } from './system/discord';
import { pythonBackend } from './system/python-backend';
import * as store from './system/settings-store';
import { destroyTray, installTray, rebuild as rebuildTray } from './system/tray';
import { cleanAgentConfigPatch } from './system/sanitize';
import { sanitizeChannelPatch } from './system/configPatch';
import { liveOrderNotice } from '../shared/agentOrderNotice';

process.env.DIST_ELECTRON = __dirname;
process.env.DIST = join(__dirname, '..', 'dist');
process.env.VITE_PUBLIC = app.isPackaged
  ? process.env.DIST
  : join(__dirname, '..', 'public');

// Give Windows a stable application identity. Must match the AppUserModelID
// electron-builder stamps onto the Start Menu / desktop shortcuts (the appId)
// so the running app, its taskbar button, and the installed shortcut are all
// treated as the same program (correct icon + grouping + notifications).
if (process.platform === 'win32') {
  app.setAppUserModelId('cc.krypt.polybot.us');
}

// Multi-account: if launched with --account=<name>, repoint userData at that
// account's folder. MUST run before requestSingleInstanceLock (the lock keys
// off userData) and before any data dir is read.
applyAccountFromArgv();

let mainWindow: BrowserWindow | null = null;

function iconPath(): string {
  const candidates = [
    app.isPackaged
      ? join(process.resourcesPath, 'app.asar.unpacked', 'resources', 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    app.isPackaged
      ? join(process.resourcesPath, 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    // In-asar fallback (resources/** is packed into app.asar via build.files).
    join(__dirname, '..', 'resources', 'krypt.ico'),
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  return candidates[0];
}

function createMainWindow(): BrowserWindow {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return mainWindow;
  }
  const state = store.get();
  const bounds = state.windowBounds;
  const primary = screen.getPrimaryDisplay();
  const width = bounds?.width ?? Math.min(1380, primary.workArea.width - 40);
  const height = bounds?.height ?? Math.min(900, primary.workArea.height - 40);
  const x = bounds?.x;
  const y = bounds?.y;

  mainWindow = new BrowserWindow({
    width,
    height,
    minWidth: 1100,
    minHeight: 720,
    x,
    y,
    backgroundColor: '#0A0A0F',
    show: false,
    frame: false,
    titleBarStyle: 'hidden',
    icon: iconPath(),
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      // Disable Chrome DevTools in the packaged build so users can't crack open
      // the running renderer (Ctrl+Shift+I / F12 become no-ops). Kept on in dev.
      devTools: !app.isPackaged,
      // backgroundThrottling is left at Electron's default (ON). It was
      // disabled so the Visualizer's rAF loop would keep animating while the
      // window was hidden to tray — i.e. so a canvas nobody could see kept
      // burning CPU/GPU. Nothing in the renderer is trading-critical: the
      // Python backend places, manages and exits every order, standing
      // instructions fire in `service.py`, and the desktop notification for
      // one is raised HERE in main (`notifyStanding`). The renderer's own
      // fill/settlement notifications are raised from IPC event handlers,
      // which Chromium does not throttle. The canvas loops (Visualizer,
      // Terminal, BossFight) simply park their frame callback while hidden
      // and resume on show.
    },
  });

  // Harden navigation: never let the renderer open child windows or
  // navigate away from the app. External links go to the OS browser.
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (e, target) => {
    const dev = process.env['VITE_DEV_SERVER_URL'];
    if ((dev && target.startsWith(dev)) || target.startsWith('file://')) return;
    e.preventDefault();
    if (/^https?:\/\//i.test(target)) void shell.openExternal(target);
  });

  const url = process.env['VITE_DEV_SERVER_URL'];
  if (url) {
    void mainWindow.loadURL(url);
  } else {
    void mainWindow.loadFile(join(process.env.DIST!, 'index.html'));
  }

  mainWindow.once('ready-to-show', () => {
    if (state.startMinimized && process.argv.includes('--autostart')) {
      // skip showing
    } else {
      mainWindow?.show();
    }
  });

  // Debounced: move/resize fire many times per drag; persist once it settles.
  let boundsTimer: NodeJS.Timeout | null = null;
  const persistBounds = (): void => {
    if (boundsTimer) clearTimeout(boundsTimer);
    boundsTimer = setTimeout(() => {
      boundsTimer = null;
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (mainWindow.isMaximized() || mainWindow.isMinimized()) return;
      const b = mainWindow.getBounds();
      const cur = store.get();
      store.save({ ...cur, windowBounds: b });
    }, 500);
  };
  mainWindow.on('move', persistBounds);
  mainWindow.on('resize', persistBounds);
  // Visibility for the renderer: with backgroundThrottling at its default
  // the page's own visibilitychange is unreliable for minimize/tray, so main
  // says it outright (src/state/visibility.ts parks animations and the Hub).
  const sendVisibility = (): void => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send('window:visibility', mainWindow.isVisible() && !mainWindow.isMinimized());
  };
  for (const ev of ['minimize', 'restore', 'hide', 'show'] as const) {
    mainWindow.on(ev as 'minimize', sendVisibility);
  }
  mainWindow.on('maximize', () => mainWindow?.webContents.send('window:maximizeChange', true));
  mainWindow.on('unmaximize', () => mainWindow?.webContents.send('window:maximizeChange', false));

  mainWindow.on('close', (e) => {
    // Hide-to-tray rather than fully closing on user X click. The user
    // can fully quit from tray or with a keystroke.
    if (!quitting) {
      e.preventDefault();
      mainWindow?.hide();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  return mainWindow;
}

/**
 * Tell the user a standing instruction fired — even when the window is not
 * focused, which is exactly when a stop matters most.
 *
 * A DESKTOP notification, deliberately, not a webhook: a webhook would post
 * the user's positions to somebody else's server on every fire. This never
 * leaves the machine, so the local-first claim survives.
 */
function notifyStanding(data: unknown): void {
  try {
    if (!store.get().config.terminalNotifications) return;
    if (!Notification.isSupported()) return;
    const e = (data ?? {}) as {
      kind?: string; title?: string; conditionId?: string;
      priceCents?: number; triggerCents?: number;
      blocked?: boolean; result?: string;
    };
    const market = e.title || (e.conditionId ?? '').slice(0, 12) || 'a market';
    const kind = ({
      stopLoss: 'Stop loss', takeProfit: 'Take profit',
      alertAbove: 'Alert', alertBelow: 'Alert',
    } as Record<string, string>)[e.kind ?? ''] ?? 'Instruction';
    const price = typeof e.priceCents === 'number' ? `${e.priceCents}¢` : '—';

    const n = new Notification({
      // A blocked stop is the one the user MUST see, so it is not softened.
      title: e.blocked
        ? `${kind} did NOT fire — ${market}`
        : `${kind} fired at ${price} — ${market}`,
      body: e.blocked
        ? (e.result || 'The trigger was hit but nothing was sold. Open Krypt PolyBot US to see why.')
        : (e.result || `Trigger ${e.triggerCents ?? '—'}¢, market at ${price}.`),
      icon: iconPath(),
      urgency: e.blocked ? 'critical' : 'normal',
    });
    // Clicking it should land you where you can act on it.
    n.on('click', () => {
      const w = createMainWindow();
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    });
    n.show();
  } catch {
    /* notifications are a convenience — never let one break the event pipe */
  }
}

/**
 * A live agent order is waiting for the user's approval. It expires in ten
 * minutes, and the window is usually not focused when an agent is working,
 * so this is a desktop notification (local, like notifyStanding) — only for
 * the approval case, never for every agent action.
 */
function notifyApproval(data: unknown): void {
  try {
    const e = (data ?? {}) as { approvalId?: unknown; message?: unknown; mode?: unknown };
    if (typeof e.approvalId !== 'number' || !Number.isFinite(e.approvalId)) return;
    if (!Notification.isSupported()) return;
    const n = new Notification({
      title: 'An AI agent is asking to place a LIVE order',
      body: `${typeof e.message === 'string' ? e.message.slice(0, 180) : 'Open AI Agents to approve or reject it.'}`
        + ' It expires in 10 minutes.',
      icon: iconPath(),
      urgency: 'critical',
    });
    n.on('click', () => {
      const w = createMainWindow();
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    });
    n.show();
  } catch {
    /* a convenience — never let it break the event pipe */
  }
}

/**
 * A LIVE agent order went out, filled, was refused, or its outcome is unknown.
 * With approvals off nobody clicked anything, so this is how the user is told
 * about every real-money order an agent makes — a desktop notification, local
 * like notifyStanding, raised here in main so it shows while the window is
 * hidden to the tray. Not gated by a setting: the engines' own fill
 * notifications have none either, and these spend real money unasked.
 */
function notifyAgentOrder(data: unknown): void {
  try {
    const notice = liveOrderNotice(data as Record<string, unknown> | null);
    if (!notice) return;
    if (!Notification.isSupported()) return;
    const n = new Notification({
      title: notice.title,
      body: notice.body,
      icon: iconPath(),
      urgency: notice.critical ? 'critical' : 'normal',
    });
    n.on('click', () => {
      const w = createMainWindow();
      if (w.isMinimized()) w.restore();
      w.show();
      w.focus();
    });
    n.show();
  } catch {
    /* a convenience — never let it break the event pipe */
  }
}

let quitting = false;

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const w = createMainWindow();
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
  });

  app.whenReady().then(bootstrap);
}

function installCsp(): void {
  // Strict CSP for packaged builds only — the Vite dev server needs
  // inline/eval + ws for HMR, so we don't apply it in development.
  if (!app.isPackaged) return;
  session.defaultSession.webRequest.onHeadersReceived((details, cb) => {
    cb({
      responseHeaders: {
        ...details.responseHeaders,
        'Content-Security-Policy': [
          "default-src 'self'; script-src 'self'; " +
          "style-src 'self' 'unsafe-inline'; " +
          "font-src 'self'; img-src 'self' data:; " +
          "connect-src 'self'",
        ],
      },
    });
  });
}

// ── Config patches arriving from chat / a supervisor script ──────────────
//
// These channels are the only place config is written by something other
// than the user, so main is the last gate before it is PERSISTED. The
// allowlist for chat and scripts lives in system/configPatch.ts (testable);
// the AI agents' channel uses sanitize.cleanAgentConfigPatch.
function sanitizeConfigPatch(
  source: string, patch: Record<string, unknown>,
): Record<string, unknown> {
  const drop = (k: string, why: string) =>
    console.error(`[${source}] dropped config key '${k}': ${why}`);
  if (source === 'mcp') {
    // An AI agent's update_config. sanitize.cleanAgentConfigPatch is the
    // whole rule: never the account mode, the paper bankrolls, mcp*/remote*/
    // ai*/autopilot*, the network or anything secret-looking; every value
    // typed against DEFAULT_CONFIG; and without the user's "whole-account
    // powers" switch only a literal `false` (turning something off). The
    // backend checks the same — this side does not trust the sender.
    return cleanAgentConfigPatch(
      patch, store.DEFAULT_CONFIG as unknown as Record<string, unknown>,
      store.get().config.mcpAllowLiveSwitches === true, drop);
  }
  return sanitizeChannelPatch(source, patch, drop);
}

async function bootstrap(): Promise<void> {
  registerIpc();
  installCsp();
  installAppMenu();

  // Initialise persisted state.
  const state = store.load();

  // Start the Python backend and pipe its events into the renderer.
  pythonBackend.onStatusChange((info) => {
    for (const w of BrowserWindow.getAllWindows()) {
      if (!w.isDestroyed()) {
        w.webContents.send('backend:info', info);
      }
    }
    rebuildTray({
      openWindow: () => createMainWindow(),
      stopAgents: stopAgentsFromTray,
      quit: () => {
        quitting = true;
        app.quit();
      },
    });
  });

  pythonBackend.onEvent((name, data) => {
    // Every time the backend signals it's ready (cold start, manual
    // restart, crash recovery) re-push the persisted config. Without
    // this, the backend boots with DEFAULT_CONFIG and the user's engine
    // switches silently do nothing until they touch a setting.
    if (name === 'backend:ready') {
      // Retry a few times — a single failed push would strand the backend on
      // DEFAULT_CONFIG until the user next touches a setting.
      void (async () => {
        for (let attempt = 1; attempt <= 3; attempt++) {
          try {
            await pythonBackend.request('setConfig', { config: store.get().config });
            return;
          } catch (e) {
            if (attempt === 3) {
              console.error('config re-push failed after 3 attempts:', e);
            } else {
              await new Promise((r) => setTimeout(r, 2000));
            }
          }
        }
      })();
    }
    if (name === 'remote:configPatch') {
      // /pause and /resume from a phone. Persisted through the normal store so
      // the UI, settings.json and the backend all agree — and so a pause
      // survives a restart rather than quietly re-arming the engines.
      // Allowlisted HERE (see sanitizeConfigPatch) because the store does not
      // sanitise: only the engine switches and the crypto posture keys get
      // through, typed.
      try {
        const patch = sanitizeConfigPatch(
          'remote', (data as { patch?: Record<string, unknown> })?.patch ?? {});
        if (Object.keys(patch).length > 0) {
          const next = store.patchConfig(patch as never);
          broadcastState(next);
          void pythonBackend
            .request('setConfig', { config: next.config })
            .catch(() => { /* retried on the next config update */ });
        }
      } catch {
        /* best effort — the backend already applied it in memory */
      }
    }
    if (name === 'mcp:configPatch') {
      // An AI agent paused or switched engines / retuned the crypto posture.
      // Same path as the chat bots: allowlisted and typed here, persisted
      // through the store so the UI, settings.json and the backend agree.
      try {
        const patch = sanitizeConfigPatch(
          'mcp', (data as { patch?: Record<string, unknown> })?.patch ?? {});
        if (Object.keys(patch).length > 0) {
          const next = store.patchConfig(patch as never);
          broadcastState(next);
          void pythonBackend
            .request('setConfig', { config: next.config })
            .catch(() => { /* retried on the next config update */ });
        }
      } catch {
        /* best effort — the backend already applied it in memory */
      }
    }
    if (name === 'script:configPatch') {
      // A supervisor script changed whitelisted config (engine switch /
      // strategy posture). Persist via the normal settings store so the UI,
      // settings.json, and the backend all agree — same pattern as autoOff.
      // The backend already whitelisted + rate-limited the patch, but the
      // store does NOT re-sanitise (patchConfig is a plain spread), so this
      // side keeps its own allowlist rather than trusting the sender.
      try {
        const patch = sanitizeConfigPatch(
          'script', (data as { patch?: Record<string, unknown> })?.patch ?? {});
        if (Object.keys(patch).length > 0) {
          const next = store.patchConfig(patch as never);
          broadcastState(next);
          void pythonBackend
            .request('setConfig', { config: next.config })
            .catch(() => { /* retried on next config update */ });
        }
      } catch {
        /* best effort */
      }
    }
    if (name === 'mcp:order') {
      notifyApproval(data);
      notifyAgentOrder(data);
    }
    if (name === 'crypto15m:autoOff') {
      // The crypto engine hit its overall take-profit target and turned itself
      // off. Persist crypto15mEnabled=false (so it stays off across restarts and
      // the UI toggle reflects it), broadcast the new state to all windows, and
      // re-push config to the backend so both sides agree.
      try {
        const next = store.patchConfig({ crypto15mEnabled: false });
        broadcastState(next);
        void pythonBackend
          .request('setConfig', { config: next.config })
          .catch(() => {
            /* will retry next config update */
          });
      } catch {
        /* best effort — the backend already stopped trading */
      }
    }
    for (const w of BrowserWindow.getAllWindows()) {
      if (w.isDestroyed()) continue;
      if (name === 'account:update') {
        w.webContents.send('data:account', data);
      } else if (name === 'position:new' || name === 'position:update') {
        w.webContents.send('data:position', data);
      } else if (name === 'credentials:changed') {
        w.webContents.send('credentials:changed', data);
      } else if (name === 'data:reset') {
        w.webContents.send('data:reset', data);
      } else if (name === 'crypto15m:autoOff') {
        w.webContents.send('crypto15m:autoOff', data);
      } else if (name === 'script:status') {
        w.webContents.send('scripts:status', data);
      } else if (name === 'script:log') {
        w.webContents.send('scripts:log', data);
      } else if (name === 'mcp:order') {
        // A live agent order queued for approval / sent / refused, or an
        // agent action. The renderer toasts it and the AI Agents page reloads.
        w.webContents.send('mcp:order', data);
      } else if (name === 'mcp:toolCall') {
        // Every agent tool call, whitelisted and throttled in the backend.
        w.webContents.send('mcp:toolCall', data);
      } else if (name === 'terminal:standing') {
        notifyStanding(data);
        // A stop/take-profit/alert the USER wrote down either fired or was
        // blocked from firing. Both are surfaced — a stop that did nothing
        // because live trading was off is exactly the thing you must be told.
        w.webContents.send('terminal:standing', data);
      } else if (name === 'backend:authChanged' || name === 'backend:reconciled' || name === 'backend:ready' || name === 'backend:shutdown') {
        // status pipeline already covers these
      }
    }
  });

  pythonBackend.onLog((entry) => {
    appendLog(entry);
    for (const w of BrowserWindow.getAllWindows()) {
      if (!w.isDestroyed()) {
        w.webContents.send('logs:append', entry);
      }
    }
  });

  // Start the backend and push initial config in.
  void pythonBackend.start().then(async () => {
    // Wait a tick for backend to be running, then push config.
    const start = Date.now();
    while (!pythonBackend.isRunning() && Date.now() - start < 8000) {
      await new Promise((r) => setTimeout(r, 200));
    }
    try {
      await pythonBackend.request('setConfig', { config: state.config });
    } catch {
      /* will retry next config update */
    }
  });

  // Discord Rich Presence — best-effort, never blocks startup, and OFF if the
  // user turned it off (the persisted flag used to be written and never read).
  if (state.enableDiscordRpc) void startDiscordRpc();

  // System tray.
  installTray({
    openWindow: () => createMainWindow(),
    stopAgents: stopAgentsFromTray,
    quit: () => {
      quitting = true;
      app.quit();
    },
  });

  // Apply Windows autostart preference.
  if (app.isPackaged) {
    setStartWithWindows(state.startWithWindows);
  }

  // Open the main window unless the user wants to start to tray.
  const launchedAtLogin =
    (() => {
      try {
        return app.getLoginItemSettings().wasOpenedAtLogin === true;
      } catch {
        return false;
      }
    })() ||
    process.argv.some((a) => a === '--autostart' || a === '--hidden');

  if (!(state.startMinimized && launchedAtLogin)) {
    createMainWindow();
  }
}

/** Tray "Stop AI agents": the same as the phone's /agents off — agent trading
 *  off and Autopilot off. Never turns anything on. */
async function stopAgentsFromTray(): Promise<void> {
  const next = store.patchConfig({ mcpTradeMode: 'off', autopilotEnabled: false });
  broadcastState(next);
  if (pythonBackend.isRunning()) {
    try {
      await pythonBackend.request('setConfig', { config: next.config });
    } catch {
      /* ignore */
    }
  }
}

app.on('window-all-closed', (e: Electron.Event) => {
  // Prevent quit so we stay alive in the tray.
  e.preventDefault();
});

app.on('before-quit', async () => {
  quitting = true;
  stopDiscordRpc();
  destroyTray();
  await pythonBackend.stop();
});
