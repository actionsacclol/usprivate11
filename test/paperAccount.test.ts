import { describe, expect, it } from 'vitest';
import type { CredentialsStatusAll, TraderConfig } from '@shared/types';
import {
  accountGoLiveChecklist, accountGoLivePatch, backToPaperAccountPatch, liveSwitchLines,
  liveSwitchMessage, reachableLiveAgents, remoteTradingArmed, keyFingerprint,
} from '../src/utils/goLive';
import { accountModeOf, isLive, MODE_LABEL } from '../src/utils/account';

/**
 * One app-wide account mode. Paper (the default) is Polymarket US's real order
 * books with imaginary money; Live is the deliberate switch. These pin the
 * renderer half: the account Go live checklist, what it says will start
 * spending, and that its writes are the account mode and nothing else. (The
 * migration of real settings files is pinned in legacySettings.test.ts.)
 */

const creds = (has: boolean): CredentialsStatusAll => ({
  current: 'mainnet',
  mainnet: {
    env: 'mainnet', hasWalletKey: has, hasApiCreds: has, address: has ? 'AbC-123' : '', addressPreview: '',
    keyId: has ? 'AbC-123' : '', keyStoredUnencrypted: false,
  } as CredentialsStatusAll['mainnet'],
});
const verified = { ok: true, message: 'ok', fingerprint: keyFingerprint(creds(true)), balanceUsd: 40 };
const base = { config: { accountMode: 'paper' as const }, armed: [] as string[] };

describe('the account Go live checklist', () => {
  it('a Paper user with no API key is blocked at the key, told where to add one', () => {
    const l = accountGoLiveChecklist({ ...base, creds: creds(false), verify: null, ack: true });
    expect(l.ready).toBe(false);
    expect(l.steps[0].state).toBe('block');
    expect(l.steps[0].detail).toMatch(/API Key page/);
  });

  it('needs a test of the current key and an explicit acknowledgement', () => {
    expect(accountGoLiveChecklist({ ...base, creds: creds(true), verify: null, ack: true }).ready).toBe(false);
    expect(accountGoLiveChecklist({ ...base, creds: creds(true), verify: verified, ack: false }).ready).toBe(false);
    expect(accountGoLiveChecklist({ ...base, creds: creds(true), verify: verified, ack: true }).ready).toBe(true);
  });

  it('the key fingerprint follows the key id, case-insensitively, and is empty without one', () => {
    expect(keyFingerprint(creds(true))).toBe('abc-123');
    expect(keyFingerprint(creds(false))).toBe('');
    expect(keyFingerprint(null)).toBe('');
  });

  it('names every arm that starts spending, and still lets you go live knowingly', () => {
    const cfg: Partial<TraderConfig> = {
      accountMode: 'paper', crypto15mEnabled: true, scriptsLiveEnabled: true,
    };
    const armed = liveSwitchLines(cfg);
    expect(armed).toHaveLength(2);
    expect(armed.join(' | ')).toMatch(/Crypto.*Scripts/);
    const l = accountGoLiveChecklist({ config: cfg, creds: creds(true), verify: verified, armed, ack: true });
    expect(l.steps.find((s) => s.id === 'engines')!.state).toBe('warn');
    expect(l.ready).toBe(true);
  });

  it('says plainly when nothing is armed', () => {
    expect(liveSwitchLines({ accountMode: 'paper' })).toEqual([]);
    expect(liveSwitchMessage({ accountMode: 'paper' })).toMatch(/Nothing is armed/);
    const l = accountGoLiveChecklist({ ...base, creds: creds(true), verify: verified, ack: true });
    expect(l.steps.find((s) => s.id === 'engines')!.state).toBe('ok');
  });

  it('counts agents only when a live agent can actually reach the app', () => {
    const agents = [{ id: 'default', name: 'Default', mode: 'live', enabled: true }];
    const cfg = { mcpTradeMode: 'live', mcpAgents: agents } as unknown as Partial<TraderConfig>;
    expect(reachableLiveAgents(cfg)).toEqual([]);   // server and Autopilot off
    expect(liveSwitchLines({ ...cfg, mcpEnabled: true }).join()).toMatch(/AI agents LIVE: Default \(each order waits/);
    expect(liveSwitchLines({ ...cfg, autopilotEnabled: true, mcpLiveApproval: false }).join())
      .toMatch(/approval OFF, Autopilot on/);
    expect(liveSwitchLines({ ...cfg, mcpEnabled: true, mcpTradeMode: 'paper' })).toEqual([]);
  });

  it('remote trading is an arm only with remote on, trading on and a transport on', () => {
    expect(remoteTradingArmed({ remoteEnabled: true, remoteTradingEnabled: true, remoteTelegramEnabled: true })).toBe(true);
    expect(remoteTradingArmed({ remoteEnabled: true, remoteTradingEnabled: true })).toBe(false);
    expect(remoteTradingArmed({ remoteTradingEnabled: true, remoteDiscordEnabled: true })).toBe(false);
  });

  it('writes the account mode and nothing else; Paper is always one write away', () => {
    expect(accountGoLivePatch()).toEqual({ accountMode: 'live' });
    expect(backToPaperAccountPatch()).toEqual({ accountMode: 'paper' });
    // terminalPaperMode is derived by the store, never written from here.
    expect(accountGoLivePatch()).not.toHaveProperty('terminalPaperMode');
  });
});

describe('reading the mode', () => {
  it('is Live only on the exact word', () => {
    expect(isLive({ accountMode: 'live' })).toBe(true);
    expect(isLive({ accountMode: 'paper' })).toBe(false);
    expect(isLive({ accountMode: 'LIVE' as never })).toBe(false);
    expect(isLive(null)).toBe(false);
    expect(accountModeOf(undefined)).toBe('paper');
    expect(MODE_LABEL[accountModeOf({ accountMode: 'live' })]).toBe('Live');
  });
});
