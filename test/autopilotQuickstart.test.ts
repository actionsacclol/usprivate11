import { describe, expect, it } from 'vitest';
import {
  BUDGETS, MISSIONS, MISSION_MAX, PROVIDERS, buildQuickstartPatch, costLine, maxDailyCostUsd,
} from '../src/utils/autopilotQuickstart';

/**
 * The quickstart's single settings write. It turns Autopilot on, which is
 * the one place the app calls a model on a timer, so what it writes is
 * pinned here number by number, and "never live" is checked for every
 * combination of inputs it can be given.
 */

const byId = (id: string) => BUDGETS.find((b) => b.id === id)!;

describe('budget presets', () => {
  it('map to the documented numbers', () => {
    expect(BUDGETS.map((b) => [b.id, b.intervalMin, b.maxRunsPerDay, b.dailyTokenBudget, b.maxSteps]))
      .toEqual([
        ['light', 240, 3, 300_000, 8],
        ['standard', 120, 6, 1_000_000, 12],
        ['heavy', 60, 12, 3_000_000, 20],
      ]);
  });

  it('reach the patch unchanged, inside the panel\'s own bounds', () => {
    for (const b of BUDGETS) {
      const p = buildQuickstartPatch({ provider: 'anthropic', model: '', mission: 'x', budget: b });
      expect(p.autopilotIntervalMin).toBe(b.intervalMin);
      expect(p.autopilotMaxRunsPerDay).toBe(b.maxRunsPerDay);
      expect(p.autopilotDailyTokenBudget).toBe(b.dailyTokenBudget);
      expect(p.autopilotMaxSteps).toBe(b.maxSteps);
      expect(b.maxRunsPerDay * b.intervalMin).toBeLessThanOrEqual(24 * 60);
    }
  });

  it('clamps a hand-built preset into bounds rather than passing junk through', () => {
    const p = buildQuickstartPatch({
      provider: 'ollama', model: '', mission: '',
      budget: { ...byId('light'), intervalMin: 1, maxRunsPerDay: 10_000, dailyTokenBudget: 1, maxSteps: 99 },
    });
    expect(p.autopilotIntervalMin).toBe(15);
    expect(p.autopilotMaxRunsPerDay).toBe(96);
    expect(p.autopilotDailyTokenBudget).toBe(50_000);
    expect(p.autopilotMaxSteps).toBe(40);
  });
});

describe('the patch never writes live', () => {
  it('is paper for every provider, budget and mission', () => {
    for (const pr of PROVIDERS) {
      for (const b of BUDGETS) {
        for (const m of [...MISSIONS.map((x) => x.text), '', 'trade live with real money']) {
          const p = buildQuickstartPatch({ provider: pr.id, model: 'm', mission: m, budget: b });
          expect(p.mcpTradeMode).toBe('paper');
          expect(JSON.stringify(p)).not.toMatch(/"live"/);
          expect(p.autopilotEnabled).toBe(true);
        }
      }
    }
  });

  it('touches only the keys it owns: no account mode, no caps, no approval, no permissions', () => {
    const p = buildQuickstartPatch({ provider: 'anthropic', model: '', mission: 'x', budget: byId('standard') });
    expect(Object.keys(p).sort()).toEqual([
      'aiModel', 'aiProvider', 'autopilotAgentId', 'autopilotDailyTokenBudget', 'autopilotEnabled',
      'autopilotIntervalMin', 'autopilotMaxRunsPerDay', 'autopilotMaxSteps', 'autopilotMission',
      'mcpTradeMode',
    ]);
    expect(p).not.toHaveProperty('accountMode');
    expect(p).not.toHaveProperty('mcpAgents');
  });

  it('runs as the agent picked, Default otherwise, and never a malformed or reserved id', () => {
    const b = byId('standard');
    const id = (agentId?: string) => buildQuickstartPatch({ provider: 'anthropic', model: '', mission: 'x', budget: b, agentId }).autopilotAgentId;
    expect(id()).toBe('default');
    expect(id('sam1')).toBe('sam1');
    expect(id('../x')).toBe('default');
    expect(id('manual')).toBe('default');
  });

  it('trims the mission and caps it at the panel\'s limit', () => {
    const p = buildQuickstartPatch({
      provider: 'anthropic', model: ' claude-sonnet-5 ', mission: `  ${'a'.repeat(5000)}  `, budget: byId('light'),
    });
    expect(p.autopilotMission).toHaveLength(MISSION_MAX);
    expect(p.aiModel).toBe('claude-sonnet-5');
  });
});

describe('missions', () => {
  it('fit the rails: forecast before any trade, no promise of profit, no live', () => {
    for (const m of MISSIONS) {
      expect(m.text).toMatch(/forecast/i);
      expect(m.text.length).toBeLessThanOrEqual(MISSION_MAX);
      expect(m.text).not.toMatch(/\blive\b|guarantee|profit|kalshi/i);
    }
    expect(MISSIONS.find((m) => m.id === 'closing')!.text).toMatch(/after Polymarket's fee/);
  });

  it('name real tools and columns', () => {
    const all = MISSIONS.map((m) => m.text).join(' ');
    for (const t of ['discover_markets', 'get_market', 'record_forecast', 'get_portfolio']) {
      expect(all).toContain(t);
    }
    for (const c of all.matchAll(/column "([a-z_]+)"/g)) {
      expect(['new', 'closing', 'volume', 'volatility', 'watchlist', 'crypto_updown']).toContain(c[1]);
    }
  });
});

describe('cost estimate', () => {
  const prices = { 'claude-sonnet-5': { inPerMTok: 2, outPerMTok: 10 } };

  it('prices a full budget at 90% input / 10% output', () => {
    expect(maxDailyCostUsd(1_000_000, prices['claude-sonnet-5'])).toBe(2.8);
    expect(maxDailyCostUsd(1_000_000, undefined)).toBeNull();
    expect(maxDailyCostUsd(0, prices['claude-sonnet-5'])).toBeNull();
  });

  it('says free for local, a number only where a price is published, and never guesses', () => {
    expect(costLine({ provider: 'ollama', model: 'x', budget: byId('heavy') })).toMatch(/^Free/);
    expect(costLine({ provider: 'anthropic', model: 'claude-sonnet-5', budget: byId('standard'), prices }))
      .toMatch(/\$2\.80/);
    const unpriced = costLine({ provider: 'openai', model: 'gpt-5.5', budget: byId('standard'), prices });
    expect(unpriced).not.toMatch(/\$/);
    expect(costLine({ provider: 'openrouter', model: 'x/y', budget: byId('light'), prices }))
      .toMatch(/OpenRouter reports/);
  });
});

describe('provider links', () => {
  it('are hardcoded https pages on the providers\' own domains, one per provider', () => {
    expect(PROVIDERS.map((p) => p.id)).toEqual(['anthropic', 'openai', 'openrouter', 'gemini', 'ollama', 'lmstudio']);
    expect(PROVIDERS.every((p) => p.getUrl.startsWith('https://'))).toBe(true);
    expect(PROVIDERS.map((p) => new URL(p.getUrl).host)).toEqual([
      'platform.claude.com', 'platform.openai.com', 'openrouter.ai',
      'aistudio.google.com', 'ollama.com', 'lmstudio.ai',
    ]);
  });
});
