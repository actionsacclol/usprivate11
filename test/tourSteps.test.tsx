import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { TraderConfig } from '@shared/types';
import { TOUR_STEPS, navTourId } from '../src/components/tour/tourSteps';
import { TOP_INSET, placeCard } from '../src/components/tour/useTour';

/**
 * The tour's stops are pinned to what the sidebar actually renders. A step
 * whose target was renamed would be skipped silently at runtime (that is the
 * graceful path for a tab hidden in Simple mode), so the only place a broken
 * target can show up is here. And the order is derived from the Sidebar's
 * own nav definition: a tab added there without a tour stop fails this file.
 */

const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/components/FlexStatsCard', () => ({ FlexStatsCard: () => null }));

const config: Partial<TraderConfig> = {
  accountMode: 'paper', enableTrading: false, mcpEnabled: false, mcpTradeMode: 'paper',
};

beforeEach(() => {
  localStorage.clear();
  app.value = {
    config,
    state: { uiMode: 'advanced' },
    account: { totalUsd: 100, cashUsd: 100, portfolioUsd: 0 },
    backend: { status: 'running', authOk: true },
  };
  (window as unknown as { krypt: unknown }).krypt = {
    accounts: { current: () => Promise.resolve('Default') },
  };
});

describe('tour steps', () => {
  it('every step points at a data-tour target the Sidebar renders', async () => {
    const { Sidebar } = await import('../src/components/Sidebar');
    const { container } = render(<Sidebar page="agents" setPage={() => {}} />);
    const missing = TOUR_STEPS.filter((s) => !container.querySelector(`[data-tour="${s.id}"]`)).map((s) => s.id);
    expect(missing).toEqual([]);
  });

  it('AI Agents and the Agent Hub come first: the AGENTS group leads the sidebar', async () => {
    const { NAV_GROUPS } = await import('../src/components/Sidebar');
    expect(NAV_GROUPS[0].label).toBe('Agents');
    expect(NAV_GROUPS[0].items.map((i) => i.id)).toEqual(['agents', 'visualizer']);
    // Not also listed further down.
    expect(NAV_GROUPS.slice(1).flatMap((g) => g.items.map((i) => i.id))).not.toContain('agents');
  });

  it('Simple mode has Scripts and Backtest, and App agrees with the sidebar on what Simple is', async () => {
    const { NAV_GROUPS } = await import('../src/components/Sidebar');
    const { SIMPLE_PAGES } = await import('../src/App');
    const simple = NAV_GROUPS.flatMap((g) => g.items).filter((i) => i.tier === 'simple').map((i) => i.id);
    expect(simple).toEqual(expect.arrayContaining(['scripts', 'backtest', 'discover', 'strategies', 'agents']));
    expect([...SIMPLE_PAGES].sort()).toEqual([...simple].sort());
  });

  it('visits the sidebar tabs first, in exactly the sidebar order, then the status steps', async () => {
    const { NAV_GROUPS } = await import('../src/components/Sidebar');
    const navIds = NAV_GROUPS.flatMap((g) => g.items.map((it) => navTourId(it.id)));
    expect(TOUR_STEPS.slice(0, navIds.length).map((s) => s.id)).toEqual(navIds);
    expect(TOUR_STEPS.slice(navIds.length).every((s) => !s.page)).toBe(true);
  });

  it('names each tab as the sidebar does, under its group, and "Go there" opens that tab', async () => {
    const { NAV_GROUPS } = await import('../src/components/Sidebar');
    for (const g of NAV_GROUPS) {
      for (const it of g.items) {
        const s = TOUR_STEPS.find((x) => x.id === navTourId(it.id))!;
        expect(s.title).toBe(it.label);
        expect(s.group).toBe(g.label);
        expect(s.page).toBe(it.id);
      }
    }
  });

  it('has unique ids and short, plain blurbs: one or two sentences', () => {
    expect(new Set(TOUR_STEPS.map((s) => s.id)).size).toBe(TOUR_STEPS.length);
    for (const s of TOUR_STEPS) {
      expect(s.title.trim(), s.id).not.toBe('');
      expect(s.body.trim().length, s.id).toBeGreaterThan(20);
      expect(s.body.length, s.id).toBeLessThanOrEqual(260);
      const sentences = s.body.split(/[.!?](?:\s|$)/).filter((x) => x.trim());
      expect(sentences.length, s.id).toBeLessThanOrEqual(2);
    }
  });

  it('never sells the bot: no claim of profit, edge or guaranteed returns', () => {
    const all = TOUR_STEPS.map((s) => s.body.toLowerCase()).join(' ');
    for (const word of ['guarantee', 'profitable', 'risk-free', 'make money', 'beats the market', 'kalshi']) {
      expect(all).not.toContain(word);
    }
  });

  it('the balance card says which book it is: PAPER unless the account is Live', async () => {
    const { Sidebar } = await import('../src/components/Sidebar');
    const first = render(<Sidebar page="agents" setPage={() => {}} />);
    expect(first.getByTestId('account-mode-pill').textContent).toBe('paper');
    first.unmount();
    app.value = { ...app.value, config: { ...config, accountMode: 'live' } };
    const second = render(<Sidebar page="agents" setPage={() => {}} />);
    expect(second.getByTestId('account-mode-pill').textContent).toBe('live');
  });
});

describe('placeCard', () => {
  const vp = { width: 1380, height: 864 };
  const card = { width: 320, height: 220 };

  it('sits to the right of a sidebar item, centred on it', () => {
    const p = placeCard({ top: 300, left: 8, width: 224, height: 32 }, card, vp);
    expect(p.side).toBe('right');
    expect(p.left).toBeGreaterThan(232);
    expect(p.top).toBe(300 + 16 - 110);
  });

  it('flips left when the right edge has no room, and slides to stay on screen', () => {
    const p = placeCard({ top: 820, left: 1200, width: 140, height: 36 }, card, vp);
    expect(p.side).toBe('left');
    expect(p.left + card.width).toBeLessThanOrEqual(1200);
    expect(p.top + card.height).toBeLessThanOrEqual(vp.height);
  });

  it('puts header steps below, clear of the title-bar drag region', () => {
    const p = placeCard({ top: 6, left: 300, width: 24, height: 24 }, card, vp, ['bottom', 'left', 'right', 'top']);
    expect(p.side).toBe('bottom');
    expect(p.top).toBeGreaterThanOrEqual(TOP_INSET);
    expect(p.left).toBeGreaterThanOrEqual(12);
  });

  it('centres on a window too small for any side', () => {
    const p = placeCard({ top: 100, left: 100, width: 200, height: 200 }, card, { width: 420, height: 400 });
    expect(p.side).toBe('center');
  });
});
