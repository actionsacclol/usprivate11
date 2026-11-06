import { describe, expect, it } from 'vitest';
import {
  AGENT_CATEGORIES, agentExportJson, cleanAgent, parseAgentImport, rulesInWords, type McpAgent,
} from '@shared/agents';
import { AGENT_TEMPLATES, agentFromTemplate, duplicateAgent } from '../src/utils/agentTemplates';

function sam(over: Record<string, unknown> = {}): McpAgent {
  return cleanAgent({
    id: 'sam1', name: 'Sports Sam', emoji: '🏀', color: '#F59E0B', guide: 'NBA only.',
    rules: { categoriesAllow: ['sports'], minPriceCents: 20, maxPriceCents: 80 },
    mode: 'paper', enabled: true, ...over,
  })!;
}

describe('templates', () => {
  it('five honest starters, each valid, on paper, and inside the rails', () => {
    expect(AGENT_TEMPLATES).toHaveLength(5);
    const cats = AGENT_CATEGORIES.map((c) => c.id) as string[];
    for (const t of AGENT_TEMPLATES) {
      const a = agentFromTemplate(t, ['default']);
      expect(cleanAgent(a)).toEqual(a);                     // what the backend would store
      expect(a.mode).toBe('paper');
      expect(a.enabled).toBe(true);
      expect(a.id).not.toBe('default');
      // Forecast before buying, and an edge only after Polymarket's fee.
      expect(t.guide).toMatch(/forecast/i);
      expect(t.guide.toLowerCase()).toMatch(/fee|no orders|do not trade|place no orders/);
      // Polymarket, not Kalshi; shares, not contracts.
      expect(t.guide).not.toMatch(/kalshi|contract/i);
      expect(JSON.stringify(t.rules)).not.toMatch(/Contracts/);
      for (const c of [...(t.rules.categoriesAllow ?? []), ...(t.rules.categoriesDeny ?? [])]) {
        expect(cats).toContain(c);
      }
      // No hype: nothing promises a result.
      expect(t.guide.toLowerCase()).not.toMatch(/guarantee|easy money|can't lose|always win|profit/);
    }
    const crypto = AGENT_TEMPLATES.find((t) => t.key === 'crypto-forecaster')!;
    expect(crypto.rules.maxOpenPositions).toBe(0);
    expect(rulesInWords(agentFromTemplate(crypto, []).rules)).toContain('Forecasts only — no orders');
    const sports = agentFromTemplate(AGENT_TEMPLATES.find((t) => t.key === 'sports-value')!, []);
    expect(sports.rules).toMatchObject({
      categoriesAllow: ['sports'], minPriceCents: 20, maxPriceCents: 80, maxHoursToClose: 48, maxSharesPerMarket: 25,
    });
  });

  it('a blank agent has no rules of its own', () => {
    const a = agentFromTemplate(null, []);
    expect(a.name).toBe('New agent');
    expect(rulesInWords(a.rules)).toEqual([]);
  });

  it('a duplicate is a new agent on paper', () => {
    const d = duplicateAgent(sam({ mode: 'live' }), ['default', 'sam1']);
    expect(d.id).not.toBe('sam1');
    expect(d.mode).toBe('paper');
    expect(d.name).toBe('Sports Sam (copy)');
  });
});

describe('export / import', () => {
  it('a shared file carries the personality and rules, never an id, a token or a mode', () => {
    const text = agentExportJson(sam({ mode: 'live' }));
    const parsed = JSON.parse(text);
    expect(parsed.kryptPolybotAgent).toBe(1);
    expect(Object.keys(parsed.agent).sort()).toEqual(['color', 'emoji', 'guide', 'name', 'rules']);
    expect(text).not.toMatch(/sam1|"live"|token|enabled|createdAt/);
  });

  it('an import is a NEW agent on paper, whatever the file says', () => {
    const hostile = JSON.stringify({
      kryptPolybotAgent: 1,
      agent: { id: 'default', name: 'Evil\nline', mode: 'live', enabled: true, token: 'kt_x',
               rules: { maxOrderUsd: -5, sides: 'yes' } },
    });
    const r = parseAgentImport(hostile, ['default']);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.agent.id).not.toBe('default');
    expect(r.agent.mode).toBe('paper');
    expect(r.agent.name).toBe('Evil line');
    expect(r.agent.rules.maxOrderUsd).toBe(0);
    expect(JSON.stringify(r.agent)).not.toContain('kt_x');
    const back = parseAgentImport(agentExportJson(sam()), []);
    expect(back.ok && back.agent.rules).toEqual(sam().rules);
  });

  it('refuses what is not a polybot agent file', () => {
    expect(parseAgentImport('nope', []).ok).toBe(false);
    expect(parseAgentImport('{"kryptTraderAgent":1,"agent":{"name":"x"}}', []).ok).toBe(false);
    expect(parseAgentImport(JSON.stringify({ kryptPolybotAgent: 1, agent: { name: 'x', guide: 'y'.repeat(70000) } }), []).ok).toBe(false);
  });
});
