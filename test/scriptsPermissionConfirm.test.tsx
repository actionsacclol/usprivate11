import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The AI Agents page's "Write and run strategy scripts" permission
 * (2026-10-12 release audit, C2 / H10): turning it ON asks first, in red,
 * and the copy no longer promises what the app does not do.
 */

const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn(), push: vi.fn() }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({ useToast: () => toast }));
vi.mock('../src/state/usePoll', () => ({
  usePoll: () => ({ data: null, error: null, loading: false, reload: () => undefined }),
}));
vi.mock('../src/components/AutopilotPanel', () => ({ AutopilotPanel: () => null }));
vi.mock('../src/components/ConnectionsPanel', () => ({ ConnectionsPanel: () => null }));
vi.mock('../src/components/HttpApiCard', () => ({ HttpApiCard: () => null }));
vi.mock('../src/components/AgentFlow', () => ({ AgentFlow: () => null }));
vi.mock('../src/components/GoLivePanel', () => ({ GoLivePanel: () => null }));
vi.mock('../src/components/agents/AgentsSection', () => ({ AgentsSection: () => null }));

import { AiAgentsPage } from '../src/pages/AiAgents';

const update = vi.fn(async () => ({}));

beforeEach(() => {
  update.mockClear();
  app.value = {
    config: { mcpEnabled: true, mcpScriptsEnabled: false, accountMode: 'paper' },
    refresh: { state: vi.fn(async () => undefined) },
    backend: { status: 'stopped' },
  };
  (window as unknown as { krypt: unknown }).krypt = {
    config: { update },
    mcp: { onOrder: () => () => undefined, sync: vi.fn(async () => ({})) },
  };
});

function scriptsSwitch(): HTMLElement {
  const label = screen.getByText('Write and run strategy scripts');
  const row = label.closest('label') ?? label.parentElement?.parentElement ?? label;
  const input = row.querySelector('input, button[role="switch"], [role="switch"]');
  return (input as HTMLElement) ?? label;
}

describe('the scripts permission', () => {
  it('turning it on asks first, in red, and only the confirm writes it', async () => {
    render(<AiAgentsPage />);
    fireEvent.click(scriptsSwitch());
    expect(update).not.toHaveBeenCalled();
    expect(screen.getByText('Let agents write and run code on this computer?')).toBeTruthy();
    fireEvent.click(screen.getByText('Allow scripts'));
    await Promise.resolve();
    expect(update).toHaveBeenCalledWith({ mcpScriptsEnabled: true });
  });

  it('turning it off needs no confirmation', async () => {
    app.value = { ...app.value, config: { mcpEnabled: true, mcpScriptsEnabled: true, accountMode: 'paper' } };
    render(<AiAgentsPage />);
    fireEvent.click(scriptsSwitch());
    await Promise.resolve();
    expect(update).toHaveBeenCalledWith({ mcpScriptsEnabled: false });
    expect(screen.queryByText('Let agents write and run code on this computer?')).toBeNull();
  });

  it('no longer promises what the app does not do', () => {
    const { container } = render(<AiAgentsPage />);
    const text = container.textContent ?? '';
    expect(text).not.toMatch(/no\s+credentials\s+loaded/i);
    expect(text).not.toMatch(/refused outright/i);
    expect(text).toMatch(/not a sandbox/i);
    expect(text).toMatch(/Only you can arm a script/i);
  });
});
