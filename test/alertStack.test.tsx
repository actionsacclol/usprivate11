import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AppAlert, BackendInfo } from '@shared/types';
import { AlertStack } from '../src/components/AlertStack';
import { alertKey, engineAlert, updateDismissed } from '../src/utils/alerts';

/**
 * The corner alerts: they say what to do, they go away when the problem does, and
 * closing one sticks until the problem comes back or gets worse.
 */

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn() }));
const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({ useToast: () => toast }));

const ALERT = (over: Partial<AppAlert> = {}): AppAlert => ({
  id: 'api:gateway', severity: 'error', title: 'Can\'t reach Polymarket US',
  detail: 'No response for 40 seconds.', fix: 'Check your internet connection.', action: null, ...over,
});
const BACKEND = (over: Partial<BackendInfo> = {}): BackendInfo => ({
  status: 'running', pid: 1, startedAt: null, lastError: null, pythonOk: true, authOk: true, ...over,
});

let list: ReturnType<typeof vi.fn>;
let krypt: Record<string, unknown>;
const restart = vi.fn(async () => ({ ok: true }));
const resume = vi.fn(async () => ({ ok: true, message: 'Trading resumed' }));

function setApp(over: Record<string, unknown> = {}): void {
  app.value = {
    backend: BACKEND(), state: { acceptedDisclaimer: true },
    refresh: { backend: vi.fn(async () => {}) }, ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  list = vi.fn(async () => []);
  krypt = { alerts: { list }, backend: { restart }, trading: { resume } };
  (window as unknown as { krypt: unknown }).krypt = krypt;
  setApp();
});

const flush = async (): Promise<void> => { await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };

async function mount(onNav = vi.fn()) {
  const r = render(<AlertStack onNav={onNav} />);
  await flush();
  return { ...r, onNav };
}

const tick = async (): Promise<void> => {
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); await Promise.resolve(); });
  await flush();
};

describe('engineAlert', () => {
  it('says nothing while the backend is fine or just getting going', () => {
    expect(engineAlert(BACKEND(), 999_999)).toBeNull();
    expect(engineAlert(BACKEND({ status: 'stopped' }), 1000)).toBeNull();
    expect(engineAlert(BACKEND({ status: 'starting' }), 30_000)).toBeNull();
    expect(engineAlert(BACKEND({ status: 'restarting' }), 30_000)).toBeNull();
  });

  it('holds back while an automatic respawn may still fix a crash', () => {
    expect(engineAlert(BACKEND({ status: 'crashed' }), 2000)).toBeNull();
  });

  it('reports a crash that stays, with a way to fix it and the reason', () => {
    const a = engineAlert(BACKEND({ status: 'crashed', lastError: 'boom' }), 9000)!;
    expect(a.title).toContain('crashed');
    expect(a.detail).toContain('boom');
    expect(a.fix).toBeTruthy();
    expect(a.action?.kind).toBe('restart');
  });

  it('reports a stop or a stuck start after its grace', () => {
    expect(engineAlert(BACKEND({ status: 'stopped' }), 20_000)?.title).toContain('stopped');
    expect(engineAlert(BACKEND({ status: 'starting' }), 61_000)?.title).toContain('isn\'t starting');
  });
});

describe('updateDismissed', () => {
  const a = ALERT();
  const key = alertKey(a);

  it('remembers a closed alert while its problem lasts', () => {
    const d = updateDismissed(new Map([[key, 1000]]), [a], 500_000);
    expect(d.get(key)).toBe(500_000);
  });

  it('forgets it only once the problem has been gone a while', () => {
    const closed = new Map([[key, 1000]]);
    expect(updateDismissed(closed, [], 30_000).has(key)).toBe(true);
    expect(updateDismissed(closed, [], 70_000).has(key)).toBe(false);
  });

  it('returns the same map when nothing changed, so a poll does not re-render', () => {
    const empty = new Map<string, number>();
    expect(updateDismissed(empty, [a], 5000)).toBe(empty);
  });

  it('treats getting worse as a new alert', () => {
    expect(alertKey(ALERT({ severity: 'warn' }))).not.toBe(alertKey(ALERT({ severity: 'error' })));
  });
});

describe('AlertStack', () => {
  it('shows nothing when nothing is wrong', async () => {
    await mount();
    expect(screen.queryByTestId('alert-stack')).toBeNull();
  });

  it('sits in the bottom-right corner and says what happened and what to do', async () => {
    list.mockResolvedValue([ALERT()]);
    await mount();
    expect(screen.getByTestId('alert-stack').className).toMatch(/bottom-5.*right-5/);
    const el = screen.getByTestId('alert-api:gateway');
    expect(el.textContent).toContain('Can\'t reach Polymarket US');
    expect(el.textContent).toContain('No response for 40 seconds.');
    expect(el.textContent).toContain('What to do: Check your internet connection.');
    expect(el.getAttribute('role')).toBe('alert');
  });

  it('never appears over first-run setup', async () => {
    setApp({ state: { acceptedDisclaimer: false } });
    list.mockResolvedValue([ALERT()]);
    await mount();
    expect(screen.queryByTestId('alert-stack')).toBeNull();
  });

  it('goes away by itself when the problem is fixed', async () => {
    list.mockResolvedValue([ALERT()]);
    await mount();
    expect(screen.getByTestId('alert-api:gateway')).toBeTruthy();
    list.mockResolvedValue([]);
    await tick();
    expect(screen.queryByTestId('alert-api:gateway')).toBeNull();
  });

  it('X closes it, and the next poll does not bring it back', async () => {
    list.mockResolvedValue([ALERT(), ALERT({ id: 'kill-switch', title: 'Kill' })]);
    await mount();
    fireEvent.click(screen.getByTestId('alert-close-api:gateway'));
    expect(screen.queryByTestId('alert-api:gateway')).toBeNull();
    await tick();
    expect(screen.queryByTestId('alert-api:gateway')).toBeNull();
    expect(screen.getByTestId('alert-kill-switch')).toBeTruthy();
  });

  it('a closed warning comes back when it becomes an error', async () => {
    list.mockResolvedValue([ALERT({ severity: 'warn' })]);
    await mount();
    fireEvent.click(screen.getByTestId('alert-close-api:gateway'));
    list.mockResolvedValue([ALERT({ severity: 'error' })]);
    await tick();
    expect(screen.getByTestId('alert-api:gateway')).toBeTruthy();
  });

  it('keeps showing the last answer when a poll fails', async () => {
    list.mockResolvedValue([ALERT()]);
    await mount();
    list.mockResolvedValue(null);
    await tick();
    expect(screen.getByTestId('alert-api:gateway')).toBeTruthy();
    list.mockRejectedValue(new Error('rpc'));
    await tick();
    expect(screen.getByTestId('alert-api:gateway')).toBeTruthy();
  });

  it('the action button goes to the right page', async () => {
    list.mockResolvedValue([ALERT({ id: 'key', action: { kind: 'nav', page: 'api', label: 'Open API Key page' } })]);
    const { onNav } = await mount();
    fireEvent.click(screen.getByTestId('alert-action-key'));
    expect(onNav).toHaveBeenCalledWith('api');
  });

  it('Resume lifts the pause and re-reads', async () => {
    list.mockResolvedValue([ALERT({ id: 'paused', severity: 'warn', action: { kind: 'resume', label: 'Resume' } })]);
    await mount();
    list.mockResolvedValue([]);
    fireEvent.click(screen.getByTestId('alert-action-paused'));
    await flush();
    expect(resume).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('alert-paused')).toBeNull();
  });

  it('a dead backend shows its own alert with a restart button, and drops the stale ones', async () => {
    list.mockResolvedValue([ALERT()]);
    const r = await mount();
    setApp({ backend: BACKEND({ status: 'crashed', lastError: 'boom' }) });
    r.rerender(<AlertStack onNav={r.onNav} />);
    await flush();
    expect(screen.queryByTestId('alert-api:gateway')).toBeNull();
    // Still down after the respawn window.
    const later = Date.now() + 20_000;
    const clock = vi.spyOn(Date, 'now').mockReturnValue(later);
    setApp({ backend: BACKEND({ status: 'crashed', lastError: 'boom' }) });
    r.rerender(<AlertStack onNav={r.onNav} />);
    await flush();
    expect(screen.getByTestId('alert-engine').textContent).toContain('boom');
    fireEvent.click(screen.getByTestId('alert-action-engine'));
    await flush();
    expect(restart).toHaveBeenCalledTimes(1);
    clock.mockRestore();
  });

  it('holds a long list behind a button rather than filling the screen', async () => {
    list.mockResolvedValue(['a', 'b', 'c', 'd', 'e', 'f'].map((id) => ALERT({ id })));
    await mount();
    expect(screen.queryByTestId('alert-e')).toBeNull();
    fireEvent.click(screen.getByTestId('alert-more'));
    expect(screen.getByTestId('alert-f')).toBeTruthy();
  });
});
