import { describe, expect, it } from 'vitest';
import type { TraderConfig } from '@shared/types';
import { defaultAgent } from '../shared/agents';
import { migrateConfig, RETIRED_CONFIG_KEYS } from '../electron/system/legacy-settings';

/**
 * The settings.json migration for the AI-agent port. It runs against a real
 * user's file exactly once and must not drop anyone from live to paper (or the
 * other way) by accident. python config._migrate_mcp applies the same rules
 * to what the backend receives; these pin the TypeScript half.
 */

// The bits of DEFAULT_CONFIG the migration reads (settings-store imports
// electron, so it can't load here).
const DEFAULTS = {
  accountMode: 'paper', terminalPaperMode: true, terminalPaperBankrollUsd: 1000,
  mcpTradeMode: 'paper', mcpDailySpendUsd: 100, mcpAllowLiveSwitches: false,
  mcpAgents: [defaultAgent('paper')], autopilotAgentId: 'default',
  aiProvider: 'anthropic', aiModel: 'claude-opus-5',
} as unknown as TraderConfig;

const running = (raw: Record<string, unknown>, existingUser = true) =>
  migrateConfig(raw, DEFAULTS, { live: true, existingUser });

describe('agents: the old single live switch', () => {
  it('mcpLiveTradingEnabled: true -> trade mode live, whole-account powers, Default agent live', () => {
    const c = running({ mcpEnabled: true, mcpLiveTradingEnabled: true });
    expect(c.mcpTradeMode).toBe('live');
    expect(c.mcpAllowLiveSwitches).toBe(true);
    expect(c.mcpAgents!.map((a) => [a.id, a.mode])).toEqual([['default', 'live']]);
  });

  it('mcpLiveTradingEnabled: false -> paper, powers untouched', () => {
    const c = running({ mcpLiveTradingEnabled: false });
    expect(c.mcpTradeMode).toBe('paper');
    expect(c.mcpAllowLiveSwitches).toBe(false);
    expect(c.mcpAgents![0].mode).toBe('paper');
  });

  it('an explicit new setting wins over the legacy flag', () => {
    const c = running({ mcpLiveTradingEnabled: true, mcpTradeMode: 'off', mcpAllowLiveSwitches: false });
    expect(c.mcpTradeMode).toBe('off');
    expect(c.mcpAllowLiveSwitches).toBe(false);
  });

  it('an agent list that exists is kept, never rewritten from the legacy flag', () => {
    const c = running({ mcpLiveTradingEnabled: true, mcpAgents: [{ id: 'default', mode: 'paper' }, { id: 'sam1', mode: 'live' }] });
    expect(c.mcpAgents!.map((a) => [a.id, a.mode])).toEqual([['default', 'paper'], ['sam1', 'live']]);
  });

  it('mcpDailyLimitUsd -> mcpDailySpendUsd, unless the new key is set', () => {
    expect(running({ mcpDailyLimitUsd: 40 }).mcpDailySpendUsd).toBe(40);
    expect(running({ mcpDailyLimitUsd: 40, mcpDailySpendUsd: 75 }).mcpDailySpendUsd).toBe(75);
    expect(running({}).mcpDailySpendUsd).toBe(100);
  });

  it('an unknown trade mode falls to the default, and a bad autopilot agent id to Default', () => {
    const c = running({ mcpTradeMode: 'YOLO', autopilotAgentId: '../x' });
    expect(c.mcpTradeMode).toBe('paper');
    expect(c.autopilotAgentId).toBe('default');
  });
});

describe('AI: one model key for six providers', () => {
  it('the selected provider\'s old model fills aiModel', () => {
    expect(running({ aiProvider: 'openai', aiOpenaiModel: 'gpt-5', aiAnthropicModel: 'claude-x' }).aiModel).toBe('gpt-5');
    expect(running({ aiProvider: 'anthropic', aiAnthropicModel: 'claude-x' }).aiModel).toBe('claude-x');
  });
  it('an empty old OpenAI model stays empty (no default for a fast-moving id list)', () => {
    expect(running({ aiProvider: 'openai', aiOpenaiModel: '' }).aiModel).toBe('');
  });
  it('an existing aiModel is never overwritten', () => {
    expect(running({ aiProvider: 'openai', aiModel: 'o5', aiOpenaiModel: 'gpt-5' }).aiModel).toBe('o5');
  });
  it('an unknown provider is the default, never guessed', () => {
    expect(running({ aiProvider: 'mistral' }).aiProvider).toBe('anthropic');
    expect(running({ aiProvider: 'gemini', aiModel: 'g' }).aiProvider).toBe('gemini');
  });
});

describe('Paper | Live', () => {
  it('no accountMode + terminalPaperMode true -> paper', () => {
    expect(running({ terminalPaperMode: true }).accountMode).toBe('paper');
  });
  it('no accountMode, an EXISTING user\'s running config -> live (engines keep doing what they did)', () => {
    const c = running({ terminalPaperMode: false, enableTrading: true });
    expect(c.accountMode).toBe('live');
    expect(c.terminalPaperMode).toBe(false);
  });
  it('no accountMode, a fresh install (disclaimer never accepted) -> paper', () => {
    expect(running({ terminalPaperMode: false }, false).accountMode).toBe('paper');
  });
  it('a profile snapshot or a patch can never come out live from an old file', () => {
    expect(migrateConfig({ terminalPaperMode: false }, DEFAULTS).accountMode).toBe('paper');
    expect(migrateConfig({ terminalPaperMode: false }, DEFAULTS, { existingUser: true }).accountMode).toBe('paper');
  });
  it('a stored accountMode is kept; anything but the exact word live is paper', () => {
    expect(running({ accountMode: 'live', terminalPaperMode: true }).accountMode).toBe('live');
    expect(running({ accountMode: 'LIVE' }).accountMode).toBe('paper');
  });
  it('terminalPaperMode is derived and can never disagree', () => {
    expect(running({ accountMode: 'live', terminalPaperMode: true }).terminalPaperMode).toBe(false);
    expect(running({ accountMode: 'paper', terminalPaperMode: false }).terminalPaperMode).toBe(true);
  });
});

describe('the migration as a whole', () => {
  it('drops every retired key', () => {
    const c = running({ mcpLiveTradingEnabled: true, mcpDailyLimitUsd: 5, aiAnthropicModel: 'a', aiOpenaiModel: 'b' });
    for (const k of RETIRED_CONFIG_KEYS) expect(c).not.toHaveProperty(k);
  });

  it('is idempotent', () => {
    const once = running({ mcpLiveTradingEnabled: true, mcpDailyLimitUsd: 40, aiProvider: 'openai',
      aiOpenaiModel: 'gpt-5', terminalPaperMode: false });
    const twice = running(once as unknown as Record<string, unknown>);
    expect(twice).toEqual(once);
  });

  it('nothing at all -> the defaults (paper everywhere)', () => {
    const c = running({}, false);
    expect(c.accountMode).toBe('paper');
    expect(c.mcpTradeMode).toBe('paper');
    expect(c.mcpAgents!.map((a) => a.id)).toEqual(['default']);
  });
});
