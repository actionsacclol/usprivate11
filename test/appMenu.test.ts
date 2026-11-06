import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Packaged builds shouldn't keep Electron's default shortcuts (Ctrl+R, F11, Ctrl+W), but a
 * Mac must keep its Edit menu or copy and paste stop working, API key field included.
 */

const electron = vi.hoisted(() => ({
  app: { isPackaged: true },
  Menu: {
    setApplicationMenu: vi.fn(),
    buildFromTemplate: vi.fn((template: unknown) => ({ template })),
  },
}));

vi.mock('electron', () => electron);

import { installAppMenu } from '../electron/system/app-menu';

const realPlatform = process.platform;
const setPlatform = (value: string): void => {
  Object.defineProperty(process, 'platform', { value, configurable: true });
};

beforeEach(() => {
  electron.app.isPackaged = true;
  electron.Menu.setApplicationMenu.mockClear();
  electron.Menu.buildFromTemplate.mockClear();
});
afterEach(() => setPlatform(realPlatform));

describe('app menu', () => {
  it.each(['win32', 'linux'])('removes the menu on %s', (platform) => {
    setPlatform(platform);
    installAppMenu();
    expect(electron.Menu.setApplicationMenu).toHaveBeenCalledWith(null);
  });

  it('keeps copy and paste on a Mac, without reload or developer tools', () => {
    setPlatform('darwin');
    installAppMenu();
    const roles = electron.Menu.buildFromTemplate.mock.calls[0][0] as Array<{ role: string }>;
    expect(roles.map((r) => r.role)).toEqual(['appMenu', 'editMenu', 'windowMenu']);
    expect(electron.Menu.setApplicationMenu).toHaveBeenCalledTimes(1);
  });

  it('leaves the default menu alone in development', () => {
    electron.app.isPackaged = false;
    installAppMenu();
    expect(electron.Menu.setApplicationMenu).not.toHaveBeenCalled();
  });
});
