import { describe, expect, it } from 'vitest';
import {
  agentOrderMessage, agentOrderToast, liveOrderNotice, liveOutcome,
} from '../shared/agentOrderNotice';

/**
 * Every LIVE agent order is told to the user (2026-10-12 release audit:
 * "with approvals off, the user is not told about every order"). The push
 * arrives in the original shape {mode, message, approvalId?} and in a
 * superset with the order's fields and outcome; both, and anything missing.
 */

describe('the desktop notification for a live agent order', () => {
  it('fires for every live outcome in the superset shape', () => {
    const base = { mode: 'live', agentName: 'Sam', title: 'Will it rain?', side: 'yes',
      action: 'buy', size: 10, price: 42 };
    for (const [status, word] of [['sent', 'sent'], ['filled', 'filled'],
      ['refused', 'refused'], ['unknown', 'UNKNOWN']] as const) {
      const n = liveOrderNotice({ ...base, status });
      expect(n, status).not.toBeNull();
      expect(n!.title).toContain('Sam');
      expect(n!.title).toContain(word);
      expect(n!.body).toContain('BUY 10 YES @ 42¢');
      expect(n!.body).toContain('Will it rain?');
    }
    expect(liveOrderNotice({ ...base, status: 'unknown' })!.body).toContain('Check Polymarket');
    expect(liveOrderNotice({ ...base, status: 'unknown' })!.critical).toBe(true);
    expect(liveOrderNotice({ ...base, status: 'filled' })!.critical).toBe(false);
  });

  it('fires for the original shape too, reading the message', () => {
    const n = liveOrderNotice({ mode: 'live', message: 'LIVE buy 5 YES @ 30¢ in "X" — order placed' });
    expect(n?.title).toBe('An AI agent: LIVE order sent');
    expect(n?.body).toContain('LIVE buy 5 YES');
    expect(liveOutcome({ mode: 'live', message: 'Polymarket refused: not enough balance' })).toBe('refused');
  });

  it('stays quiet for paper, actions and approval requests (those have their own)', () => {
    expect(liveOrderNotice({ mode: 'paper', message: 'paper buy' })).toBeNull();
    expect(liveOrderNotice({ mode: 'action', message: 'paused engines' })).toBeNull();
    expect(liveOrderNotice({ mode: 'live', message: 'wants to buy', approvalId: 7 })).toBeNull();
  });

  it('survives missing and mistyped fields', () => {
    expect(liveOrderNotice(null)).toBeNull();
    expect(liveOrderNotice(undefined)).toBeNull();
    expect(liveOrderNotice({ mode: 'live' })).toEqual({
      title: 'An AI agent: LIVE order sent', body: 'Open AI Agents to see it.', critical: false,
    });
    const n = liveOrderNotice({ mode: 'live', status: 42, size: 'lots', price: NaN, agentName: {} });
    expect(n?.title).toBe('An AI agent: LIVE order sent');
  });
});

describe('the in-app toast', () => {
  it('builds a message from the fields when there is none', () => {
    expect(agentOrderMessage({ agentName: 'Sam', action: 'sell', size: 3, side: 'no', price: 61, status: 'filled' }))
      .toBe('Sam — SELL 3 NO @ 61¢ (filled)');
    expect(agentOrderMessage({ mode: 'live' })).toBeNull();
    expect(agentOrderToast({ mode: 'live' })).toBeNull();
  });

  it('keeps the approval and paper wording', () => {
    expect(agentOrderToast({ mode: 'live', message: 'buy 5', approvalId: 3 })).toMatchObject({
      tone: 'warn', ms: 15_000,
    });
    expect(agentOrderToast({ mode: 'live', message: 'buy 5', approvalId: 3 })!.text).toContain('Approve or reject');
    expect(agentOrderToast({ mode: 'paper', message: 'paper buy' })).toEqual({
      text: 'AI agent — paper buy', tone: 'info', ms: 8_000,
    });
  });

  it('names a live outcome the user must look at', () => {
    const t = agentOrderToast({ mode: 'live', status: 'unknown', message: 'no response from Polymarket' });
    expect(t?.tone).toBe('warn');
    expect(t?.text).toContain('UNKNOWN');
    expect(agentOrderToast({ mode: 'live', status: 'refused', message: 'too big' })!.text).toContain('refused');
  });
});

describe('rail refusals (status "blocked")', () => {
  it('show in the app as refused but raise no desktop notification', () => {
    const ev = { mode: 'live', status: 'blocked', agentName: 'Ada', message: 'Ada: LIVE buy blocked by the daily cap' };
    expect(liveOutcome(ev)).toBe('refused');
    expect(liveOrderNotice(ev)).toBeNull();
  });
  it('a venue refusal still notifies', () => {
    expect(liveOrderNotice({ mode: 'live', status: 'refused', agentName: 'Ada', message: 'x' })).not.toBeNull();
  });
});
