import type { ReactNode } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The header counts the AI agents (2026-10-12 release audit): it said "All
 * engines paused" while agents traded real money with approvals off. Live
 * agents get a chip and an always-visible "Stop agents" (confirm first), and
 * the account's Go live says the agents stay on paper.
 */

const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn(), push: vi.fn() }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({ useToast: () => toast }));
vi.mock('../src/components/glass/GlassPanel', () => ({
  GlassPanel: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock('../src/components/glass/GlassSegmented', () => ({ GlassSegmented: () => null }));

import { TopBar } from '../src/components/TopBar';
import { GoLiveAccountDialog } from '../src/components/AccountModePanel';

const update = vi.fn(async () => ({}));

const LIVE_AGENT = { id: 'default', name: 'Default', enabled: true, mode: 'live' };

function setConfig(config: Record<string, unknown>): void {
  app.value = {
    config,
    account: null,
    backend: { status: 'running' },
    state: { uiMode: 'advanced' },
    positions: [],
    credentialsAll: null,
    refresh: {
      state: vi.fn(async () => undefined), credentials: vi.fn(async () => undefined),
      account: vi.fn(async () => undefined), backend: vi.fn(async () => undefined),
      positions: vi.fn(async () => undefined),
    },
  };
}

beforeEach(() => {
  update.mockClear();
  (window as unknown as { krypt: unknown }).krypt = {
    config: { update },
    state: { setUiMode: vi.fn() },
    terminal: { standing: vi.fn(async () => ({ instructions: [] })) },
    credentials: { test: vi.fn() },
  };
});

describe('the squad banner', () => {
  it('opens the squad link and says so, in place of the old monsters', () => {
    const openExternal = vi.fn(async () => undefined);
    (window as unknown as { krypt: Record<string, unknown> }).krypt.app = { openExternal };
    setConfig({ accountMode: 'paper', mcpEnabled: false, mcpAgents: [] });
    render(<TopBar />);
    const banner = screen.getByTestId('topbar-squad');
    expect(banner.textContent).toContain('Join the Krypt squad');
    expect(banner.title).toMatch(/credits Krypt/);
    fireEvent.click(banner);
    expect(openExternal).toHaveBeenCalledWith('https://polymarket.us/squad/join/0xOHjNIfwLBVzmovbjml?referrer=yuhgo');
  });
});

describe('the header and the AI agents', () => {
  it('does not say "All engines paused" while agents trade real money', () => {
    setConfig({ accountMode: 'live', mcpTradeMode: 'live', mcpEnabled: true,
      mcpLiveApproval: false, mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    expect(screen.getByRole('heading').textContent).toBe('AI agents live');
    expect(screen.getByTestId('topbar-stop-agents')).toBeTruthy();
    expect(screen.getByTestId('topbar-agents-chip').getAttribute('title')).toContain('approvals OFF');
  });

  describe('the balance', () => {
    const book = { bankrollUsd: 1000, cashUsd: 900, equityUsd: 1050, realizedPnlUsd: 0, openPositions: 1 };
    const real = { totalUsd: 16, sessionPnlUsd: 2, sessionRoiPct: 14 };

    it('in Paper is the paper book, with the real one in the tooltip, and no share button', () => {
      setConfig({ accountMode: 'paper' });
      app.value = { ...app.value, account: real, paper: { ...book, hasWalletKey: true } };
      render(<TopBar />);
      expect(screen.getByText('Paper balance')).toBeTruthy();
      const balance = screen.getByTitle(/Your real account: \$16\.00/);
      expect(balance.textContent).toBe('$1,050');
      expect(screen.getByText('Paper P&L')).toBeTruthy();
      expect(screen.getByText('+$50.00')).toBeTruthy();
      expect(screen.queryByTitle(/Share to/)).toBeNull();
    });

    it('in Paper with no answer yet is a dash, never $0.00', () => {
      setConfig({ accountMode: 'paper' });
      app.value = { ...app.value, account: { totalUsd: 0, sessionPnlUsd: 0 }, paper: null };
      const { container } = render(<TopBar />);
      expect(container.textContent).not.toContain('$0.00');
    });

    it('in Live is the real account, as before', () => {
      setConfig({ accountMode: 'live' });
      app.value = { ...app.value, account: real, paper: null };
      render(<TopBar />);
      expect(screen.getByText('Balance')).toBeTruthy();
      expect(screen.getByText('$16.00')).toBeTruthy();
      expect(screen.getByText('Session P&L')).toBeTruthy();
      expect(screen.getByTitle(/Share to/)).toBeTruthy();
    });
  });

  it('counts agents alongside live engines', () => {
    setConfig({ accountMode: 'live', crypto15mEnabled: true, mcpTradeMode: 'live',
      mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    expect(screen.getByRole('heading').textContent).toBe('1 engine + AI agents live');
  });

  it('Stop agents asks first, then puts the agents back on paper', async () => {
    setConfig({ accountMode: 'live', mcpTradeMode: 'live', autopilotEnabled: true,
      mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    fireEvent.click(screen.getByTestId('topbar-stop-agents'));
    expect(update).not.toHaveBeenCalled();
    expect(screen.getByText('Stop your AI agents trading real money?')).toBeTruthy();
    await act(async () => { const btns = screen.getAllByRole('button', { name: 'Stop agents' }); fireEvent.click(btns[btns.length - 1]); });
    expect(update).toHaveBeenCalledWith({ mcpTradeMode: 'paper' });
    expect(toast.success).toHaveBeenCalled();
  });

  it('agents on paper or off: no stop button, the engines headline as before', () => {
    setConfig({ accountMode: 'live', mcpTradeMode: 'paper', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    const { unmount } = render(<TopBar />);
    expect(screen.getByRole('heading').textContent).toBe('All engines paused');
    expect(screen.queryByTestId('topbar-stop-agents')).toBeNull();
    expect(screen.getByTestId('topbar-agents-chip').getAttribute('title')).toContain('paper');
    unmount();
    // In Paper an agent set live sends nothing real.
    setConfig({ accountMode: 'paper', mcpTradeMode: 'live', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    render(<TopBar />);
    expect(screen.queryByTestId('topbar-stop-agents')).toBeNull();
  });
});

describe('the Paper | Live switch', () => {
  const pressed = (id: string): string | null => screen.getByTestId(id).getAttribute('aria-pressed');

  it('shows two buttons with the current side lit, on either side', () => {
    setConfig({ accountMode: 'paper' });
    const r = render(<TopBar />);
    expect(screen.getByTestId('mode-paper').textContent).toBe('Paper');
    expect(screen.getByTestId('mode-live').textContent).toBe('Live');
    expect(pressed('mode-paper')).toBe('true');
    expect(pressed('mode-live')).toBe('false');
    r.unmount();
    setConfig({ accountMode: 'live' });
    render(<TopBar />);
    expect(pressed('mode-paper')).toBe('false');
    expect(pressed('mode-live')).toBe('true');
  });

  it('claims neither side until the config has loaded', () => {
    app.value = { ...app.value, config: null };
    render(<TopBar />);
    expect(pressed('mode-paper')).toBe('false');
    expect(pressed('mode-live')).toBe('false');
    expect((screen.getByTestId('mode-live') as HTMLButtonElement).disabled).toBe(true);
  });

  it('Live opens the checklist and changes nothing by itself', () => {
    setConfig({ accountMode: 'paper' });
    render(<TopBar />);
    expect(screen.queryByTestId('golive-account')).toBeNull();
    fireEvent.click(screen.getByTestId('mode-live'));
    expect(screen.getByTestId('golive-account')).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
    expect(pressed('mode-paper')).toBe('true');
  });

  it('Paper asks first when live, and changes nothing by itself', async () => {
    setConfig({ accountMode: 'live' });
    render(<TopBar />);
    await act(async () => { fireEvent.click(screen.getByTestId('mode-paper')); await Promise.resolve(); });
    expect(screen.getByTestId('back-to-paper-confirm')).toBeTruthy();
    expect(update).not.toHaveBeenCalled();
  });

  it('clicking the side you are already on does nothing', async () => {
    setConfig({ accountMode: 'paper' });
    const r = render(<TopBar />);
    fireEvent.click(screen.getByTestId('mode-paper'));
    expect(update).not.toHaveBeenCalled();
    r.unmount();
    setConfig({ accountMode: 'live' });
    render(<TopBar />);
    await act(async () => { fireEvent.click(screen.getByTestId('mode-live')); await Promise.resolve(); });
    expect(update).not.toHaveBeenCalled();
    expect(screen.queryByTestId('back-to-paper-confirm')).toBeNull();
  });
});

describe("the account's Go live", () => {
  it('says the agents stay on paper and links to their page', () => {
    setConfig({ accountMode: 'paper', mcpTradeMode: 'paper', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    const onNav = vi.fn();
    const onClose = vi.fn();
    render(<GoLiveAccountDialog onClose={onClose} onNav={onNav} />);
    expect(screen.getByTestId('golive-account-agents-paper').textContent)
      .toContain('Your AI agents stay on paper — go live for them on the AI Agents page');
    fireEvent.click(screen.getByTestId('golive-account-agents-link'));
    expect(onNav).toHaveBeenCalledWith('agents');
    expect(onClose).toHaveBeenCalled();
  });

  it('says nothing of the kind when an agent is already set live', () => {
    setConfig({ accountMode: 'paper', mcpTradeMode: 'live', mcpEnabled: true, mcpAgents: [LIVE_AGENT] });
    render(<GoLiveAccountDialog onClose={() => undefined} />);
    expect(screen.queryByTestId('golive-account-agents-paper')).toBeNull();
  });
});
