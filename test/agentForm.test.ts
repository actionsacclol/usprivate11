import { describe, expect, it } from 'vitest';
import { AGENT_GUIDE_MAX, cleanAgent, cleanAgents, type McpAgent } from '@shared/agents';
import {
  agentToForm, formIsValid, formToAgent, parseField, upsertAgent, validateAgentForm,
} from '../src/utils/agentForm';

function sam(over: Record<string, unknown> = {}): McpAgent {
  return cleanAgent({
    id: 'sam1', name: 'Sports Sam', emoji: '🏀', color: '#F59E0B', guide: 'NBA only.',
    rules: { categoriesAllow: ['sports'], minPriceCents: 20, maxPriceCents: 80, maxSharesPerMarket: 25 },
    mode: 'paper', enabled: true, createdAt: '2026-10-01T00:00:00Z', updatedAt: null,
    ...over,
  })!;
}

describe('the editor: form -> stored agent', () => {
  it('round-trips, with a blank box meaning NO rule and 0 meaning zero', () => {
    const a = sam();
    const f = agentToForm(a);
    expect(f.minPriceCents).toBe('20');
    expect(f.maxSharesPerMarket).toBe('25');
    expect(f.maxOpenPositions).toBe('');
    const out = formToAgent({ ...f, maxOpenPositions: '0', minPriceCents: '' }, a, new Date('2026-10-06T00:00:00Z'));
    expect(out.rules.maxOpenPositions).toBe(0);
    expect(out.rules.minPriceCents).toBeNull();
    expect(out.rules.maxPriceCents).toBe(80);
    expect(out.rules.maxSharesPerMarket).toBe(25);
    expect(out.id).toBe('sam1');
    expect(out.createdAt).toBe('2026-10-01T00:00:00Z');
    expect(out.updatedAt).toBe('2026-10-06T00:00:00.000Z');
  });

  it('parseField: blank is no rule, junk is NaN', () => {
    expect(parseField('')).toBeNull();
    expect(parseField('  ')).toBeNull();
    expect(parseField('12.5')).toBe(12.5);
    expect(Number.isNaN(parseField('abc') as number)).toBe(true);
  });

  it('says what is wrong, next to the field', () => {
    const f = agentToForm(sam());
    expect(formIsValid(validateAgentForm(f))).toBe(true);
    const e = validateAgentForm({
      ...f, name: ' ', minPriceCents: '90', maxPriceCents: '10', maxOpenPositions: '2.5',
      minEdgeCents: 'abc', categoriesDeny: ['sports'], guide: 'g'.repeat(AGENT_GUIDE_MAX + 1),
      maxSharesPerMarket: '1.5',
    });
    expect(Object.keys(e).sort()).toEqual(
      ['categoriesDeny', 'guide', 'maxOpenPositions', 'maxPriceCents', 'maxSharesPerMarket', 'minEdgeCents', 'name']);
    expect(validateAgentForm(f, ['sports sam']).name).toMatch(/already has this name/);
  });

  it('a live agent stays live only by the exact word', () => {
    const out = formToAgent({ ...agentToForm(sam()), mode: 'live' }, sam());
    expect(out.mode).toBe('live');
  });

  it('upsert replaces in place and appends new ones', () => {
    const list = cleanAgents([{ id: 'default' }, sam()]);
    expect(upsertAgent(list, sam({ name: 'Sam 2' })).map((a) => a.name)).toEqual(['Default', 'Sam 2']);
    expect(upsertAgent(list, sam({ id: 'new1' }))).toHaveLength(3);
  });
});
