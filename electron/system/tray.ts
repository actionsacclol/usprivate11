import { app, Menu, Tray, nativeImage } from 'electron';
import { join } from 'node:path';
import { existsSync } from 'node:fs';
import { pythonBackend } from './python-backend';

let tray: Tray | null = null;

/**
 * Human-readable backend status for the tray (tooltip + menu), so a
 * tray-minimized user who never opens the window still sees when the backend
 * has crashed and whether it is self-healing. Reads the backend manager
 * directly — the tray is rebuilt on every status change (see main.ts).
 */
function backendStatusLine(): string {
  const status = pythonBackend.info().status;
  if (status === 'running') return 'backend running';
  if (status === 'starting' || status === 'restarting') return 'backend starting…';
  if (status === 'crashed') {
    const { gaveUp, retrying } = pythonBackend.recoveryState();
    if (retrying || gaveUp) return 'backend crashed — retrying';
    return 'backend crashed';
  }
  return 'backend stopped';
}

interface TrayHandlers {
  openWindow: () => void;
  /** Agents and Autopilot off. */
  stopAgents?: () => void;
  quit: () => void;
}

function iconPath(): string {
  // Try resources/krypt.ico (works in dev and prod via extraResources/files).
  const candidates = [
    app.isPackaged
      ? join(process.resourcesPath, 'app.asar.unpacked', 'resources', 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    app.isPackaged
      ? join(process.resourcesPath, 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    // In-asar fallbacks (resources/** is packed into app.asar via build.files).
    join(__dirname, '..', 'resources', 'krypt.ico'),
    join(__dirname, '..', 'resources', 'krypt.png'),
    join(process.cwd(), 'resources', 'krypt.png'),
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  return candidates[candidates.length - 1];
}

export function installTray(handlers: TrayHandlers): Tray {
  if (tray && !tray.isDestroyed()) return tray;
  const img = nativeImage.createFromPath(iconPath());
  tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img);
  tray.setToolTip('Krypt PolyBot US');
  tray.on('click', () => handlers.openWindow());
  tray.on('double-click', () => handlers.openWindow());
  rebuild(handlers);
  return tray;
}

export function rebuild(handlers: TrayHandlers): void {
  if (!tray) return;
  const statusLine = backendStatusLine();
  // Tooltip carries the status so hovering the tray icon tells the story
  // without opening the window.
  const status = pythonBackend.info().status;
  tray.setToolTip(
    status === 'running'
      ? 'Krypt PolyBot US'
      : `Krypt PolyBot US — ${statusLine.toUpperCase()}`,
  );
  const menu = Menu.buildFromTemplate([
    { label: 'Open Krypt PolyBot US', click: () => handlers.openWindow() },
    { type: 'separator' },
    // Non-clickable status line so a tray-only user can see backend health.
    { label: `Status: ${statusLine}`, enabled: false },
    { type: 'separator' },
    ...(handlers.stopAgents
      ? [{ label: 'Stop AI agents', click: () => handlers.stopAgents?.() }]
      : []),
    { type: 'separator' },
    { label: 'Quit', click: () => handlers.quit() },
  ]);
  tray.setContextMenu(menu);
}

export function destroyTray(): void {
  if (tray && !tray.isDestroyed()) tray.destroy();
  tray = null;
}
