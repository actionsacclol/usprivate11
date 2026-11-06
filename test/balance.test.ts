import { describe, expect, it } from 'vitest';
import type { AccountSnapshot } from '@shared/types';
import { balanceView, paperView, realBalanceOf, todayPnlOf } from '../src/utils/balance';

const snap = (p: Partial<AccountSnapshot> = {}): AccountSnapshot => ({
  cashUsd: 900, portfolioUsd: 100, totalUsd: 1000, startBankrollUsd: 1000, roiPct: 0,
  realizedPnlUsd: 0, unrealizedPnlUsd: 0, openCostUsd: 0, feesUsd: 0, wins: 0, losses: 0, winRate: 0,
  pendingCount: 0, openCount: 0, resolvedCount: 0, totalOpened: 0,
  byNetwork: { mainnet: { wins: 0, losses: 0, realizedPnl: 0 } },
  alltimePnlUsd: 0, alltimeBaselineUsd: 1000, sessionPnlUsd: 0, sessionBaselineUsd: 1000, sessionRoiPct: 0,
  ...p,
});

describe('balanceView: an unknown balance is a dash, never $0.00', () => {
  const paper = { config: { accountMode: 'paper' as const }, authOk: false, hasKeys: true };
  const live = { config: { accountMode: 'live' as const }, authOk: false, hasKeys: true };

  it('no snapshot, or main\'s backend-down placeholder, is unknown with every delta', () => {
    for (const a of [null, snap({ backendDown: true, totalUsd: 0 })]) {
      const v = balanceView(a, paper);
      expect(v.known).toBe(false);
      expect([v.totalUsd, v.sessionPnlUsd, v.alltimePnlUsd, v.roiPct]).toEqual([null, null, null, null]);
      expect(v.why).toMatch(/starting or stopped/);
    }
  });

  it('no API key saved is unknown in either mode (the snapshot is the real account)', () => {
    expect(balanceView(snap({ totalUsd: 0 }), { ...paper, hasKeys: false }).why).toMatch(/No API key saved/);
    expect(balanceView(snap({ totalUsd: 0 }), { ...live, hasKeys: false }).known).toBe(false);
  });

  it('Live with a key that did not authenticate is unknown; Paper does not need auth', () => {
    expect(balanceView(snap(), live).why).toMatch(/not accepted/);
    expect(balanceView(snap(), { ...live, authOk: true }).known).toBe(true);
    expect(balanceView(snap(), paper).known).toBe(true);
  });

  it('a snapshot kept from before the engine stopped is not the balance now', () => {
    expect(balanceView(snap(), { ...paper, engineRunning: false }).known).toBe(false);
    expect(balanceView(snap(), { ...paper, engineRunning: true }).known).toBe(true);
  });

  it('a non-finite total is unknown', () => {
    expect(balanceView(snap({ totalUsd: Number.NaN }), paper).known).toBe(false);
  });

  it('a delta with no baseline yet is unknown, not zero', () => {
    const v = balanceView(snap({ alltimeBaselineUsd: null, sessionBaselineUsd: 0, todayBaselineUsd: null, todayPnlUsd: 5 }), paper);
    expect(v.known).toBe(true);
    expect(v.alltimePnlUsd).toBeNull();
    expect(v.sessionPnlUsd).toBeNull();
    expect(v.sessionRoiPct).toBeNull();
    expect(v.todayPnlUsd).toBeNull();
  });

  it("today's P&L before today's first snapshot is a dash, wherever it is drawn", () => {
    // The backend sends todayPnlUsd 0 with no baseline; the Terminal's TODAY
    // chip used to print that as +$0.00.
    expect(todayPnlOf(snap({ todayBaselineUsd: null, todayPnlUsd: 0 }))).toBeNull();
    expect(todayPnlOf(snap({ todayBaselineUsd: 1000, todayPnlUsd: -12.5 }))).toBe(-12.5);
    expect(todayPnlOf(null)).toBeNull();
  });

  it('ROI needs a starting bankroll', () => {
    expect(balanceView(snap({ startBankrollUsd: 0, roiPct: 12 }), paper).roiPct).toBeNull();
    expect(balanceView(snap({ startBankrollUsd: 500, roiPct: 12 }), paper).roiPct).toBe(12);
  });
});

describe('paperView', () => {
  const book = { bankrollUsd: 1000, cashUsd: 900, equityUsd: 1050, realizedPnlUsd: 0, openPositions: 2 };

  it('shows the paper book: total, cash, positions, and P&L against its bankroll', () => {
    expect(paperView(book)).toEqual({
      totalUsd: 1050, cashUsd: 900, portfolioUsd: 150, pnlUsd: 50, roiPct: 5, bankrollUsd: 1000,
    });
  });

  it('a fresh book reads as its bankroll with no P&L', () => {
    const v = paperView({ ...book, cashUsd: 1000, equityUsd: 1000, openPositions: 0 });
    expect([v.totalUsd, v.portfolioUsd, v.pnlUsd, v.roiPct]).toEqual([1000, 0, 0, 0]);
  });

  it('unknown stays unknown, never $0.00', () => {
    for (const p of [null, undefined]) {
      expect(paperView(p)).toEqual({
        totalUsd: null, cashUsd: null, portfolioUsd: null, pnlUsd: null, roiPct: null, bankrollUsd: null,
      });
    }
    const v = paperView({ ...book, equityUsd: null });
    expect([v.totalUsd, v.portfolioUsd, v.pnlUsd, v.roiPct]).toEqual([null, null, null, null]);
  });

  it('has no return on an empty bankroll', () => {
    expect(paperView({ ...book, bankrollUsd: 0 }).roiPct).toBeNull();
  });
});

describe('realBalanceOf', () => {
  const a = { totalUsd: 16 } as AccountSnapshot;
  const book = { bankrollUsd: 1000, cashUsd: 1000, equityUsd: 1000, realizedPnlUsd: 0, openPositions: 0 };

  it('is there only with a key saved and a balance actually measured', () => {
    expect(realBalanceOf(a, { ...book, hasWalletKey: true })).toBe(16);
    expect(realBalanceOf(a, { ...book, hasWalletKey: false })).toBeNull();
    expect(realBalanceOf({ ...a, backendDown: true }, { ...book, hasWalletKey: true })).toBeNull();
    expect(realBalanceOf(null, { ...book, hasWalletKey: true })).toBeNull();
    expect(realBalanceOf(a, null)).toBeNull();
  });
});
