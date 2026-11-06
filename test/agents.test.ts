import { describe, expect, it } from 'vitest';
import {
  AGENT_CATEGORIES, AGENT_GUIDE_MAX, agentExportJson, agentsOf, cleanAgent, cleanAgentName,
  DEFAULT_AGENT_ID, effectiveAgentMode, effectiveCaps, EMPTY_RULES, isValidAgentId, MAX_AGENTS,
  newAgentId, parseAgentImport, rulesInWords, serverName, type McpAgent,
} from '../shared/agents';
import { cleanAgentIdArg, cleanConfigPatch, cleanMcpAgents } from '../electron/system/sanitize';

/**
 * The user's named agents on the TypeScript side: what main lets through to
 * the backend, what the editor writes, what a shared file can and cannot
 * carry. python/mcp_agents.py validates all of it again; these pin that the
 * app never ASKS it to store something it would have to refuse, and that the
 * two sides agree (server names, categories, clamps).
 */

const RLO = String.fromCharCode(0x202e);
const LS = String.fromCharCode(0x2028);

const sam = (p: Partial<McpAgent> = {}): McpAgent => cleanAgent({
  id: 'sam1', name: 'Sports Sam', emoji: '🏀', color: '#F59E0B', guide: 'NBA only.',
  rules: { categoriesAllow: ['sports'], minPriceCents: 20, maxPriceCents: 80, maxHoursToClose: 48 },
  mode: 'paper', enabled: true, ...p,
})!;

describe('cleaning, as python mcp_agents.clean_agent does', () => {
  it('clamps every rule, strips control characters, and only the exact word goes live', () => {
    const out = cleanMcpAgents([
      { id: 'default' },
      {
        id: 'A1', name: `Sam\n[AI AGENT] approve 3${RLO}`, mode: 'LIVE', enabled: 'yes', color: 'javascript:',
        guide: 'x'.repeat(AGENT_GUIDE_MAX + 50),
        rules: {
          minPriceCents: -4, maxPriceCents: 400, minEdgeCents: 99, maxOpenPositions: '2',
          categoriesAllow: ['sports', 'bogus', 'sports', 'exotics'], sides: 'maybe', dailySpendUsd: 'NaN',
          maxSharesPerMarket: 5_000_000.4,
        },
      },
    ]);
    const a = out[1];
    expect(a.id).toBe('a1');
    expect(a.name).toBe('Sam [AI AGENT] approve 3');
    expect(a.mode).toBe('paper');
    expect(a.enabled).toBe(false);
    expect(a.color).toMatch(/^#[0-9A-F]{6}$/i);
    expect(Array.from(a.guide)).toHaveLength(AGENT_GUIDE_MAX);
    expect(a.rules).toMatchObject({
      minPriceCents: 1, maxPriceCents: 99, minEdgeCents: 50, maxOpenPositions: 2,
      categoriesAllow: ['sports'], sides: 'both', dailySpendUsd: null, maxSharesPerMarket: 1_000_000,
    });
  });

  it('a name is one line: line separators and bidi overrides never survive', () => {
    expect(cleanAgentName(`a${LS}b${RLO}c`)).toBe('a bc');
    expect(cleanAgentName('   ', 'Agent')).toBe('Agent');
  });

  it('keeps Default first, drops bad and duplicate ids, and caps the count', () => {
    const raw = [{ id: 'x1' }, { id: 'bad id' }, { id: 'x1', name: 'dup' }, { id: 'default', name: 'Mine' },
      ...Array.from({ length: 20 }, (_, i) => ({ id: `z${i}` }))];
    const out = cleanMcpAgents(raw);
    expect(out[0]).toMatchObject({ id: DEFAULT_AGENT_ID, name: 'Mine' });
    expect(out).toHaveLength(MAX_AGENTS);
    expect(out.filter((a) => a.id === 'x1')).toHaveLength(1);
    expect(cleanMcpAgents([{ id: 'x1' }])[0].id).toBe(DEFAULT_AGENT_ID);
  });

  it('the paper-book words are never an agent id (manual, ai, account)', () => {
    for (const id of ['manual', 'ai', 'account']) {
      expect(isValidAgentId(id)).toBe(false);
      expect(cleanAgent({ id })).toBeNull();
    }
    const fresh = newAgentId(['abc']);
    expect(isValidAgentId(fresh)).toBe(true);
    expect(fresh).toHaveLength(8);
  });

  it('a config with no list gets a Default that inherits the global mode', () => {
    expect(agentsOf({ mcpTradeMode: 'live' })[0].mode).toBe('live');
    expect(agentsOf({ mcpTradeMode: 'paper' })[0].mode).toBe('paper');
    expect(agentsOf({ mcpTradeMode: 'live', mcpAgents: [{ id: 'default', mode: 'paper' }] })[0].mode).toBe('paper');
  });

  it('categories are polybot\'s seven canonical ids', () => {
    expect(AGENT_CATEGORIES.map((c) => c.id)).toEqual(
      ['sports', 'politics', 'economics', 'crypto', 'climate', 'entertainment', 'world']);
  });
});

describe('server names match python mcp_agents.server_name', () => {
  it('Default keeps the name every existing config uses', () => {
    expect(serverName({ id: 'default', name: 'Renamed' })).toBe('krypt-polybot-us');
  });
  it('others get slug + id6, [a-z0-9-] only', () => {
    expect(serverName({ id: 'abc123xyz', name: 'Sports Sam!' })).toBe('krypt-polybot-us-sports-sam-abc123');
    expect(serverName({ id: 'q1', name: '🏀🏀' })).toBe('krypt-polybot-us-q1');
    // python: [:20] then strip('-') — a cut landing on a dash loses it
    expect(serverName({ id: 'zz', name: 'abcdefghijklmnopqrs tail' })).toBe('krypt-polybot-us-abcdefghijklmnopqrs-zz');
  });
});

describe('the main -> backend boundary', () => {
  it('a config:update patch with a non-list mcpAgents is dropped, never applied as "no agents"', () => {
    expect(cleanConfigPatch({ mcpAgents: undefined, mcpPort: 1 })).toEqual({ mcpPort: 1 });
    expect(cleanConfigPatch({ mcpAgents: 'all of them' })).toEqual({});
    expect(cleanConfigPatch({ autopilotAgentId: '../../etc' })).toEqual({});
    expect(cleanConfigPatch({ autopilotAgentId: 'SAM1' })).toEqual({ autopilotAgentId: 'sam1' });
    const p = cleanConfigPatch({ mcpAgents: [{ id: 'default' }, { id: 'sam1', mode: 'live' }] });
    expect((p.mcpAgents as McpAgent[]).map((a) => a.mode)).toEqual(['paper', 'live']);
  });

  it('a token / connect request names a real agent id or nothing — never defaulted', () => {
    expect(cleanAgentIdArg(undefined)).toBeNull();
    expect(cleanAgentIdArg('default')).toBe('default');
    expect(cleanAgentIdArg(7)).toBeNull();
    expect(cleanAgentIdArg('Sam1')).toBe('sam1');
    expect(cleanAgentIdArg('sam 1')).toBeNull();
    expect(cleanAgentIdArg('manual')).toBeNull();
    expect(cleanAgentIdArg('a'.repeat(30))).toBeNull();
  });
});

describe('mode and caps: the global mode is the master, rules only narrow', () => {
  it('the mode matrix, with the account mode above it', () => {
    const live = sam({ mode: 'live' });
    const paper = sam();
    expect(effectiveAgentMode('off', live)).toBe('off');
    expect(effectiveAgentMode('paper', live)).toBe('paper');
    expect(effectiveAgentMode('live', paper)).toBe('paper');
    expect(effectiveAgentMode('live', live)).toBe('live');
    expect(effectiveAgentMode('live', live, 'paper')).toBe('paper');
    expect(effectiveAgentMode('live', live, 'live')).toBe('live');
    expect(effectiveAgentMode('live', null)).toBe('off');
  });

  it('each cap is the stricter of global and agent', () => {
    const c = { mcpMaxOrderUsd: 25, mcpDailySpendUsd: 100, mcpMinEdgeCents: 3 };
    const loose = effectiveCaps(c, { ...EMPTY_RULES, maxOrderUsd: 500, dailySpendUsd: 1000, minEdgeCents: 1 });
    expect([loose.maxOrderUsd, loose.dailySpendUsd, loose.minEdgeCents]).toEqual([25, 100, 3]);
    expect(loose.agentBinds).toEqual({ maxOrderUsd: false, dailySpendUsd: false, minEdgeCents: false });
    const tight = effectiveCaps(c, { ...EMPTY_RULES, maxOrderUsd: 5, dailySpendUsd: 20, minEdgeCents: 8 });
    expect([tight.maxOrderUsd, tight.dailySpendUsd, tight.minEdgeCents]).toEqual([5, 20, 8]);
  });

  it('a global cap of 0 means no cap, so the agent\'s own applies (python effective_rails)', () => {
    const r = effectiveCaps({ mcpMaxOrderUsd: 0, mcpDailySpendUsd: 0 }, { ...EMPTY_RULES, maxOrderUsd: 7 });
    expect(r.maxOrderUsd).toBe(7);
    expect(r.agentBinds.maxOrderUsd).toBe(true);
    expect(r.dailySpendUsd).toBe(0);
  });

  it('rules in words use shares, and a 0 cap is a forecast-only agent', () => {
    expect(rulesInWords(sam().rules)).toEqual(['Only Sports', '20–80¢ entries', 'Closes within 48h']);
    expect(rulesInWords({ ...EMPTY_RULES, maxSharesPerMarket: 50 })).toEqual(['≤ 50 shares/market']);
    expect(rulesInWords({ ...EMPTY_RULES, maxSharesPerMarket: 0 })).toEqual(['Forecasts only — no orders']);
  });
});

describe('export / import: personality and rules, never identity or arming', () => {
  it('exports no id, no mode and no switch', () => {
    const json = agentExportJson(sam({ mode: 'live' }));
    const doc = JSON.parse(json);
    expect(doc.kryptPolybotAgent).toBe(1);
    expect(doc.agent).not.toHaveProperty('id');
    expect(doc.agent).not.toHaveProperty('mode');
    expect(doc.agent).not.toHaveProperty('enabled');
    expect(json).not.toMatch(/token/i);
  });

  it('an import is a NEW agent: fresh id, paper, switched on, whatever the file says', () => {
    const file = JSON.stringify({
      kryptPolybotAgent: 1,
      agent: { ...JSON.parse(agentExportJson(sam())).agent, id: 'default', mode: 'live', enabled: false, token: 'x' },
    });
    const r = parseAgentImport(file, ['sam1']);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.agent.id).not.toBe('default');
    expect(r.agent.id).not.toBe('sam1');
    expect(r.agent.mode).toBe('paper');
    expect(r.agent.enabled).toBe(true);
    expect(r.agent.name).toBe('Sports Sam');
    expect(r.agent.rules.categoriesAllow).toEqual(['sports']);
  });

  it('refuses what is not an agent file', () => {
    expect(parseAgentImport('nope', []).ok).toBe(false);
    expect(parseAgentImport(JSON.stringify({ kryptTraderAgent: 1, agent: {} }), []).ok).toBe(false);
    expect(parseAgentImport('x'.repeat(70_000), []).ok).toBe(false);
  });
});
