import { describe, expect, it, vi } from 'vitest';
import { sanitizeChannelPatch } from '../electron/system/configPatch';

/**
 * The allowlist main applies to config patches from chat (/pause, /resume,
 * /agents off) and supervisor scripts before persisting them. /pause now
 * also stops the AI agents, so the agent keys pass — but only toward OFF.
 */

describe('config patches from chat and scripts', () => {
  it("lets /pause's whole patch through, agents included", () => {
    const drop = vi.fn();
    const out = sanitizeChannelPatch('remote', {
      crypto15mEnabled: false, scriptsLiveEnabled: false,
      mcpTradeMode: 'off', autopilotEnabled: false,
    }, drop);
    expect(drop).not.toHaveBeenCalled();
    expect(out).toEqual({
      crypto15mEnabled: false, scriptsLiveEnabled: false,
      mcpTradeMode: 'off', autopilotEnabled: false,
    });
  });

  it('never lets a channel turn the agents ON or LIVE', () => {
    const drop = vi.fn();
    for (const source of ['remote', 'script']) {
      const out = sanitizeChannelPatch(source, { mcpTradeMode: 'live', autopilotEnabled: true }, drop);
      expect(out).toEqual({});
    }
    expect(sanitizeChannelPatch('remote', { mcpTradeMode: 'paper' }, drop)).toEqual({ mcpTradeMode: 'paper' });
    expect(sanitizeChannelPatch('remote', { mcpTradeMode: 'bogus' }, drop)).toEqual({});
  });

  it('/resume may put the crypto engine back; a supervisor script may flip it too', () => {
    const drop = vi.fn();
    expect(sanitizeChannelPatch('remote', { crypto15mEnabled: true }, drop))
      .toEqual({ crypto15mEnabled: true });
    expect(sanitizeChannelPatch('script', { crypto15mEnabled: false }, drop)).toEqual({ crypto15mEnabled: false });
    expect(sanitizeChannelPatch('script', { crypto15mDirectionMode: 'model', crypto15mEntryMax: 80 }, drop))
      .toEqual({ crypto15mDirectionMode: 'model', crypto15mEntryMax: 80 });
  });

  it('engines this venue does not have are not on the list at all', () => {
    const drop = vi.fn();
    expect(sanitizeChannelPatch('remote', { enableTrading: true, perpsEnabled: true, copyEnabled: true }, drop)).toEqual({});
    expect(drop).toHaveBeenCalledTimes(3);
  });

  it('drops anything not on the list, and mistyped values', () => {
    const drop = vi.fn();
    expect(sanitizeChannelPatch('remote', {
      accountMode: 'live', mcpLiveApproval: false, crypto15mEnabled: 'yes', crypto15mEntryMax: Infinity,
    }, drop)).toEqual({});
    expect(drop).toHaveBeenCalledTimes(4);
  });
});
