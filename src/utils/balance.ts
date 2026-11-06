import type { AccountSnapshot, TraderConfig } from '@shared/types';
import type { PaperStatus } from '@shared/market';
import { isLive } from './account';

/**
 * The account balance, and every number computed from it, or null where
 * nobody knows.
 *
 * The account snapshot in this app is always the REAL account — the account
 * the engines trade. Paper has its own book (paper.status), so in Paper the
 * header, sidebar and Dashboard show that book through `paperView` instead.
 * A snapshot is never relabelled as paper cash, or the other way round.
 *
 * A balance is UNKNOWN when:
 *  - there is no snapshot, or it is main's placeholder while the backend is
 *    down (`backendDown`): its zeros were never measured;
 *  - the engine is known to be stopped (a snapshot kept from before is the
 *    last thing it said, not the balance now);
 *  - no API key is saved (no signed read can have produced it);
 *  - in Live, the API key did not authenticate;
 *  - the total itself is not a finite number.
 *
 * Unknown renders as a dash, and every P&L that is a balance delta — session,
 * today, all-time, ROI — is unknown with it. Never "$0.00".
 */
export interface BalanceContext {
  config: Partial<TraderConfig> | null | undefined;
  authOk: boolean;
  /** An API key is saved (null = not known yet). */
  hasKeys: boolean | null;
  /** The engine is up. Omitted = not known (assume up). */
  engineRunning?: boolean;
}

export interface BalanceView {
  known: boolean;
  totalUsd: number | null;
  cashUsd: number | null;
  portfolioUsd: number | null;
  sessionPnlUsd: number | null;
  sessionRoiPct: number | null;
  alltimePnlUsd: number | null;
  todayPnlUsd: number | null;
  roiPct: number | null;
  /** Why it is unknown, in words, or null when known. */
  why: string | null;
}

const fin = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

export function balanceUnknownReason(a: AccountSnapshot | null | undefined, ctx: BalanceContext): string | null {
  if (!a || a.backendDown) return 'No balance yet: the trading engine is starting or stopped.';
  if (ctx.engineRunning === false) return 'The trading engine is not running, so there is no current balance.';
  if (ctx.hasKeys === false) return 'No API key saved, so there is no account balance to show.';
  if (isLive(ctx.config) && !ctx.authOk) {
    return 'Polymarket US has not accepted the saved API key yet, so the balance is unknown.';
  }
  if (fin(a.totalUsd) === null) return 'No balance read yet.';
  return null;
}

/** Today's P&L, or null before today's first snapshot. The backend sends 0
 *  when it has no baseline yet, and a delta against no baseline is unknown,
 *  not +$0.00. */
export function todayPnlOf(a: AccountSnapshot | null | undefined): number | null {
  return a && fin(a.todayBaselineUsd) !== null ? fin(a.todayPnlUsd) : null;
}

export function balanceView(a: AccountSnapshot | null | undefined, ctx: BalanceContext): BalanceView {
  const why = balanceUnknownReason(a, ctx);
  if (why || !a) {
    return {
      known: false, totalUsd: null, cashUsd: null, portfolioUsd: null, sessionPnlUsd: null,
      sessionRoiPct: null, alltimePnlUsd: null, todayPnlUsd: null, roiPct: null, why,
    };
  }
  // Deltas need a baseline too: a P&L against "no baseline yet" is not 0.
  const session = (fin(a.sessionBaselineUsd) ?? 0) > 0 ? fin(a.sessionPnlUsd) : null;
  const alltime = fin(a.alltimeBaselineUsd) !== null ? fin(a.alltimePnlUsd) : null;
  const today = todayPnlOf(a);
  return {
    known: true,
    totalUsd: fin(a.totalUsd),
    cashUsd: fin(a.cashUsd),
    portfolioUsd: fin(a.portfolioUsd),
    sessionPnlUsd: session,
    sessionRoiPct: session === null ? null : fin(a.sessionRoiPct),
    alltimePnlUsd: alltime,
    todayPnlUsd: today,
    roiPct: (fin(a.startBankrollUsd) ?? 0) > 0 ? fin(a.roiPct) : null,
    why: null,
  };
}

/** The account's paper book, as display numbers. Null where it isn't known: a dash,
 *  never $0.00. */
export interface PaperView {
  totalUsd: number | null;
  cashUsd: number | null;
  /** What open positions are worth now. */
  portfolioUsd: number | null;
  /** Against the bankroll the book started with. */
  pnlUsd: number | null;
  roiPct: number | null;
  bankrollUsd: number | null;
}

export function paperView(p: PaperStatus | null | undefined): PaperView {
  const total = fin(p?.equityUsd);
  const cash = fin(p?.cashUsd);
  const bankroll = fin(p?.bankrollUsd);
  const pnl = total !== null && bankroll !== null ? total - bankroll : null;
  return {
    totalUsd: total,
    cashUsd: cash,
    portfolioUsd: total !== null && cash !== null ? total - cash : null,
    pnlUsd: pnl,
    roiPct: pnl !== null && bankroll !== null && bankroll > 0 ? (pnl / bankroll) * 100 : null,
    bankrollUsd: bankroll,
  };
}

/** The real balance while the app shows paper, for someone with a key saved. */
export function realBalanceOf(a: AccountSnapshot | null | undefined, p: PaperStatus | null | undefined): number | null {
  return p?.hasWalletKey && a && !a.backendDown ? fin(a.totalUsd) : null;
}

/** The badge next to a balance while the app is in Paper. */
export const PAPER_BADGE_TITLE =
  'The app is in Paper: orders go to the paper book (real Polymarket US order books, imaginary money) '
  + 'and the engines send nothing. This is your paper balance. AI agents each keep their own paper book.';
