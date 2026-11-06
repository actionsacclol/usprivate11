import { app, Menu } from 'electron';

/**
 * Electron's default menu never shows in this frameless window, but its shortcuts still
 * work: Ctrl+R reloads, F11 goes full screen, Ctrl+W hides the app to the tray.
 *
 * Packaged builds drop it. Windows and Linux need no menu for copy and paste, because the
 * page handles those keys itself. On a Mac they only work through the Edit menu, so it stays,
 * along with the menus Mac users expect. Dev builds keep the default for reload and DevTools.
 */
export function installAppMenu(): void {
  if (!app.isPackaged) return;
  Menu.setApplicationMenu(
    process.platform === 'darwin'
      ? Menu.buildFromTemplate([{ role: 'appMenu' }, { role: 'editMenu' }, { role: 'windowMenu' }])
      : null,
  );
}
