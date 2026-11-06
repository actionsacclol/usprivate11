import { describe, expect, it, vi } from 'vitest';
import {
  cleanAgentConfigPatch, cleanAiKeyProvider, cleanAiProvider, cleanApiKey, cleanClientId,
  cleanConfigPatch, cleanHealthArgs, clampInt, filterAgentConfigPatch, isPersonalKey, omitForShare,
  omitPersonal, removedAgentIds,
} from '../electron/system/sanitize';

/**
 * main's own checks between the renderer (or an AI agent's settings patch)
 * and a backend that spends money. The backend checks again; these pin that
 * main never forwards what it should not.
 */

const DEFAULTS: Record<string, unknown> = {
  crypto15mEnabled: false, crypto15mEntryMax: 5, crypto15mDirectionMode: 'favorite',
  allowedCategories: null, accountMode: 'paper', terminalPaperMode: true,
  terminalPaperBankrollUsd: 1000, mcpTradeMode: 'paper', remoteTradingEnabled: false,
  aiProvider: 'anthropic', autopilotEnabled: false, network: 'mainnet', mcpPaperBankrollUsd: 1000,
};

describe('an AI agent\'s settings patch (mcp:configPatch)', () => {
  it('never the account mode, the paper bankrolls, the network, or any mcp/remote/ai/autopilot key', () => {
    const out = filterAgentConfigPatch({
      accountMode: 'live', terminalPaperMode: false, terminalPaperBankrollUsd: 1e9, mcpPaperBankrollUsd: 1e9,
      network: 'x', mcpTradeMode: 'live', mcpAllowLiveSwitches: true, remoteTradingEnabled: true,
      aiProvider: 'openai', autopilotEnabled: true, crypto15mEntryMax: 7,
    });
    expect(out).toEqual({ crypto15mEntryMax: 7 });
  });

  it('never anything secret-looking', () => {
    expect(filterAgentConfigPatch({ discordWebhookUrl: 'x', apiKey: 'x', secretKey: 'x', crypto15mEntryMax: 1 }))
      .toEqual({ crypto15mEntryMax: 1 });
  });

  it('without whole-account powers only a literal false (turning something off) gets through', () => {
    const log = vi.fn();
    const out = cleanAgentConfigPatch(
      { autopilotEnabled: false, crypto15mEnabled: true, crypto15mEntryMax: 2 }, DEFAULTS, false, log);
    expect(out).toEqual({});
    expect(log).toHaveBeenCalledWith('crypto15mEnabled', expect.stringMatching(/whole-account/));
    expect(cleanAgentConfigPatch({ crypto15mEnabled: false }, DEFAULTS, false, log)).toEqual({ crypto15mEnabled: false });
  });

  it('with the powers, values are typed against the defaults', () => {
    const out = cleanAgentConfigPatch({
      crypto15mEnabled: true, crypto15mEntryMax: 'lots', crypto15mDirectionMode: 'model',
      allowedCategories: ['sports'], unknownKey: 1, accountMode: 'live',
    }, DEFAULTS, true);
    expect(out).toEqual({
      crypto15mEnabled: true, crypto15mDirectionMode: 'model', allowedCategories: ['sports'],
    });
  });

  it('a non-object patch is nothing', () => {
    expect(cleanAgentConfigPatch(null, DEFAULTS, true)).toEqual({});
    expect(cleanAgentConfigPatch([1, 2], DEFAULTS, true)).toEqual({});
  });
});

describe('a renderer config:update patch', () => {
  it('modes are allowlisted, terminalPaperMode is derived (never written), retired keys never return', () => {
    expect(cleanConfigPatch({ mcpTradeMode: 'yolo', accountMode: 'LIVE' })).toEqual({});
    expect(cleanConfigPatch({ mcpTradeMode: 'live', accountMode: 'live' })).toEqual({ mcpTradeMode: 'live', accountMode: 'live' });
    expect(cleanConfigPatch({ terminalPaperMode: false, mcpPort: 5 })).toEqual({ mcpPort: 5 });
    expect(cleanConfigPatch({ mcpLiveTradingEnabled: true, mcpDailyLimitUsd: 1, aiAnthropicModel: 'x' })).toEqual({});
    expect(cleanConfigPatch({ aiProvider: 'Gemini' })).toEqual({ aiProvider: 'gemini' });
    expect(cleanConfigPatch({ aiProvider: 'mistral' })).toEqual({});
  });

  it('removedAgentIds names what the save dropped, which is what drives pruneAgentTokens', () => {
    expect(removedAgentIds([{ id: 'default' }, { id: 'sam1' }], [{ id: 'default' }])).toEqual(['sam1']);
    expect(removedAgentIds([{ id: 'default' }], [{ id: 'default' }, { id: 'new1' }])).toEqual([]);
    expect(removedAgentIds(undefined, [{ id: 'default' }])).toEqual([]);
  });
});

describe('profiles never carry the user\'s own setup', () => {
  it('agents, Autopilot, AI, remote and the account mode are personal', () => {
    for (const k of ['mcpAgents', 'mcpTradeMode', 'autopilotAgentId', 'aiModel', 'remoteTradingEnabled',
      'accountMode', 'terminalPaperMode', 'terminalPaperBankrollUsd', 'mcpPaperBankrollUsd', 'scriptsLiveEnabled']) {
      expect(isPersonalKey(k)).toBe(true);
    }
    for (const k of ['crypto15mEntryMax', 'crypto15mEntryThreshold']) {
      expect(isPersonalKey(k)).toBe(false);
    }
  });

  it('a shared file also drops the engines\' arm switches', () => {
    const cfg = { crypto15mEntryMax: 5, crypto15mEnabled: true, accountMode: 'live', mcpAgents: [] };
    expect(omitPersonal(cfg)).toEqual({ crypto15mEntryMax: 5, crypto15mEnabled: true });
    expect(omitForShare(cfg)).toEqual({ crypto15mEntryMax: 5 });
  });
});

describe('small allowlists', () => {
  it('AI providers: six, and only four take a key', () => {
    expect(cleanAiProvider('LMStudio')).toBe('lmstudio');
    expect(cleanAiProvider('https://evil')).toBeNull();
    expect(cleanAiKeyProvider('ollama')).toBeNull();
    expect(cleanAiKeyProvider('openrouter')).toBe('openrouter');
  });

  it('an API key: empty means clear, an invalid paste is refused (never "clear")', () => {
    expect(cleanApiKey(undefined)).toBe('');
    expect(cleanApiKey('  ')).toBe('');
    expect(cleanApiKey(' sk-abc ')).toBe('sk-abc');
    expect(cleanApiKey('sk-abc\nX-Header: 1')).toBeNull();
    expect(cleanApiKey('x'.repeat(401))).toBeNull();
  });

  it('client ids and the health check\'s one input', () => {
    expect(cleanClientId('windsurf')).toBe('windsurf');
    expect(cleanClientId('../../x')).toBeNull();
    expect(cleanHealthArgs({ deep: true })).toEqual({ deep: true });
    expect(cleanHealthArgs({ deep: 'true' })).toEqual({ deep: false });
    expect(cleanHealthArgs(true)).toEqual({ deep: true });
    expect(cleanHealthArgs(undefined)).toEqual({ deep: false });
  });

  it('clampInt: absent is the default, not the minimum', () => {
    expect(clampInt(null, 1, 10, 5)).toBe(5);
    expect(clampInt('', 1, 10, 5)).toBe(5);
    expect(clampInt(99, 1, 10, 5)).toBe(10);
    expect(clampInt('3.6', 1, 10, 5)).toBe(4);
  });
});
