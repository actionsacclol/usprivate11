import type { ReactNode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The wallet opens from the top bar and the sidebar. Its Refresh forces a read, a
 * failed read says so instead of passing old numbers off as current, and Withdraw
 * can only give the steps: Polymarket US has no withdrawal API for a retail key.
 */

const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({
  useToast: () => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn(), push: vi.fn() }),
}));
vi.mock('../src/components/glass/GlassPanel', () => ({
  GlassPanel: ({ children, onMouseDown }: { children: ReactNode; onMouseDown?: () => void }) => <div onMouseDown={onMouseDown}>{children}</div>,
}));
vi.mock('../src/components/glass/GlassSegmented', () => ({ GlassSegmented: () => null }));

import { WalletDialog } from '../src/components/WalletDialog';
import { TopBar } from '../src/components/TopBar';
import { setHideBalance } from '../src/utils/hideBalance';

const wallet = vi.fn(async () => undefined);
const update = vi.fn(async () => ({}));
const nav = vi.fn();

const LIVE_ACCOUNT = {
  cashUsd: 40, portfolioUsd: 10, totalUsd: 50, sessionBaselineUsd: 50, sessionPnlUsd: 0,
  wallet: { readOk: true, readAt: Date.now() / 1000 - 12, openOrdersUsd: 7.5, pendingWithdrawalCount: 1, pendingWithdrawalsUsd: 20 },
};

function setApp(over: Record<string, unknown>): void {
  app.value = {
    config: { accountMode: 'paper', terminalPaperBankrollUsd: 1000, balancePollInterval: 60 },
    account: null,
    paper: { equityUsd: 1100, cashUsd: 900, bankrollUsd: 1000, hasWalletKey: false },
    backend: { status: 'running', authOk: true },
    state: { uiMode: 'simple' },
    credentials: { hasWalletKey: false },
    positions: [],
    refresh: {
      wallet, state: vi.fn(async () => undefined), paper: vi.fn(async () => undefined),
      account: vi.fn(async () => undefined), positions: vi.fn(async () => undefined), backend: vi.fn(async () => undefined),
    },
    ...over,
  };
}
const LIVE = {
  config: { accountMode: 'live', balancePollInterval: 60 },
  account: LIVE_ACCOUNT,
  credentials: { hasWalletKey: true },
};

async function open(): Promise<void> {
  render(<WalletDialog onClose={vi.fn()} onNav={nav} />);
  await waitFor(() => expect(screen.getByTestId('wallet-refresh').textContent).toContain('Refresh'));
}

beforeEach(() => {
  wallet.mockClear();
  update.mockClear();
  nav.mockClear();
  setHideBalance(false);
  (window as unknown as { krypt: unknown }).krypt = {
    config: { update },
    app: { openExternal: vi.fn() },
    paper: { reset: vi.fn(async () => ({ ok: true })) },
    terminal: { standing: vi.fn(async () => ({ instructions: [] })) },
  };
});
afterEach(cleanup);

describe('the wallet panel', () => {
  it('reads the exchange when it opens and again on Refresh', async () => {
    setApp(LIVE);
    await open();
    expect(wallet).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('wallet-refresh'));
    await waitFor(() => expect(wallet).toHaveBeenCalledTimes(2));
  });

  it('shows the real figures in Live, with how fresh they are', async () => {
    setApp(LIVE);
    await open();
    expect(screen.getByTestId('wallet-panel').getAttribute('data-mode')).toBe('live');
    expect(screen.getByTestId('wallet-total').textContent).toContain('50.00');
    expect(screen.getByTestId('wallet-open-orders').textContent).toContain('7.50');
    expect(screen.getByTestId('wallet-pending-withdrawals').textContent).toContain('20.00');
    expect(screen.getByTestId('wallet-updated').textContent).toMatch(/Updated \d+ seconds ago/);
  });

  it('says so when the read failed, rather than calling old numbers current', async () => {
    setApp({ ...LIVE, account: { ...LIVE_ACCOUNT, wallet: { readOk: false, readAt: 1000 } } });
    await open();
    expect(screen.getByTestId('wallet-read-failed').textContent).toContain('last numbers');
    expect(screen.queryByTestId('wallet-updated')).toBeNull();
  });

  it('shows a dash, not $0.00, while nobody has read the balance', async () => {
    setApp({ ...LIVE, account: { ...LIVE_ACCOUNT, backendDown: true } });
    await open();
    expect(screen.getByTestId('wallet-total').textContent).toBe('—');
    expect(screen.getByTestId('wallet-unknown')).toBeTruthy();
  });

  it('shows the paper book in Paper and labels it pretend', async () => {
    setApp({});
    await open();
    expect(screen.getByTestId('wallet-panel').textContent).toContain('Pretend money');
    expect(screen.getByTestId('wallet-total').textContent).toBe('$1,100');
    expect(screen.getByTestId('wallet-paper-reset')).toBeTruthy();
    // No key: there is no real balance to check.
    expect(screen.queryByTestId('wallet-check-every')).toBeNull();
  });

  it('gives the steps for a withdrawal and moves no money itself', async () => {
    setApp(LIVE);
    await open();
    expect(screen.queryByTestId('wallet-withdraw-rules')).toBeNull();
    fireEvent.click(screen.getByTestId('wallet-withdraw'));
    expect(screen.getByTestId('wallet-withdraw-rules').textContent).toContain("can't withdraw for you");
    expect(update).not.toHaveBeenCalled();
    expect(wallet).toHaveBeenCalledTimes(1);
  });

  it('changes how often the balance is read', async () => {
    setApp(LIVE);
    await open();
    await act(async () => {
      fireEvent.change(screen.getByTestId('wallet-check-every'), { target: { value: '30' } });
    });
    expect(update).toHaveBeenCalledWith({ balancePollInterval: 30 });
  });

  it('goes to the API key page and closes', async () => {
    setApp(LIVE);
    const onClose = vi.fn();
    render(<WalletDialog onClose={onClose} onNav={nav} />);
    fireEvent.click(screen.getByTestId('wallet-api-key'));
    expect(onClose).toHaveBeenCalled();
    expect(nav).toHaveBeenCalledWith('api');
    await waitFor(() => expect(screen.getByTestId('wallet-refresh').textContent).toContain('Refresh'));
  });

  it('opens from the top bar, and the hide setting masks the header amounts', async () => {
    setApp(LIVE);
    render(<TopBar />);
    expect(screen.getByTestId('wallet-open-topbar').textContent).toContain('$50.00');
    act(() => setHideBalance(true));
    expect(screen.getByTestId('wallet-open-topbar').textContent).not.toContain('50.00');
    fireEvent.click(screen.getByTestId('wallet-open-topbar'));
    expect(screen.getByTestId('wallet-panel')).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId('wallet-refresh').textContent).toContain('Refresh'));
    expect((screen.getByTestId('wallet-hide') as HTMLInputElement).checked).toBe(true);
  });
});
