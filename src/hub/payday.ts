import type { AccountSnapshot, BotPosition } from '@shared/types';

// PAYDAY, from real events only. The simulation build had a Payday button that
// pumped fake money into a fake balance and bannered the jump; the shipping app
// has no fake money, so a celebration here is something that actually happened
// to the user's account:
//
//   * one settlement that paid at least max($25, 2% of the balance), or
//   * the session P&L reaching a new +$100 high-water step.
//
// The banner shows the real amount. At most one celebration a minute: a burst
// of settlements (a 15m window closing on several coins at once) is one
// moment, and a scene that throws a party every few seconds stops meaning it.
// Pure and three-free so it can be unit-tested and shared by every scene.

export const PAYDAY_MIN_USD = 25;
export const PAYDAY_BALANCE_FRAC = 0.02;
export const PAYDAY_PNL_STEP_USD = 100;
export const PAYDAY_COOLDOWN_MS = 60_000;

export interface PaydayEvent {
  kind: 'settlement' | 'milestone';
  /** The real dollar amount: the settlement's P&L, or the session P&L. */
  amountUsd: number;
  /** What it was, for the banner's second line ("Lakers beat Celtics"). */
  label: string;
}

/** The single-settlement bar: $25, or 2% of a balance big enough that $25 is
 *  noise. An unknown balance falls back to the $25 floor rather than a guess. */
export function settlementThreshold(balanceUsd: number | null | undefined): number {
  const b = typeof balanceUsd === 'number' && Number.isFinite(balanceUsd) && balanceUsd > 0 ? balanceUsd : 0;
  return Math.max(PAYDAY_MIN_USD, b * PAYDAY_BALANCE_FRAC);
}

/** A session P&L only counts once there is a real baseline behind it: the
 *  backend reports `sessionPnlUsd: 0` when it has no balance at all, and that
 *  zero is "unknown", not "flat". */
export function sessionPnlOf(a: AccountSnapshot | null | undefined): number | null {
  if (!a || typeof a.sessionPnlUsd !== 'number' || !Number.isFinite(a.sessionPnlUsd)) return null;
  if (!(typeof a.sessionBaselineUsd === 'number' && a.sessionBaselineUsd > 0)) return null;
  return a.sessionPnlUsd;
}

export class PaydayTracker {
  private seen = new Map<number, boolean>();
  private primed = false;
  private runId: number | null = null;
  /** Highest +$100 step the session P&L has reached (0 = not above +$100). */
  private step: number | null = null;
  private lastAt = -Infinity;

  /** Feed every app-state change; returns a celebration or null. `now` is a
   *  millisecond clock (performance.now() in the app, a number in tests). */
  check(positions: BotPosition[], account: AccountSnapshot | null, label: (p: BotPosition) => string, now: number): PaydayEvent | null {
    const runId = account?.sessionRunId ?? null;
    if (this.primed && runId !== null && this.runId !== null && runId !== this.runId) {
      // A restarted session starts its own +$100 ladder.
      this.step = null;
    }
    if (runId !== null) this.runId = runId;

    // Opening the page must not celebrate settlements that happened before
    // it was open: the first pass only records what is already settled.
    let best: BotPosition | null = null;
    for (const p of positions) {
      const settled = p.resolved && p.outcomeCorrect !== null;
      const was = this.seen.get(p.id);
      this.seen.set(p.id, settled);
      if (!this.primed || !settled || was === true) continue;
      // A dry run never touched the exchange; its "P&L" is not money.
      if (p.status === 'dry_run') continue;
      if (typeof p.pnlUsd !== 'number' || !Number.isFinite(p.pnlUsd)) continue;
      if (!best || p.pnlUsd > (best.pnlUsd ?? -Infinity)) best = p;
    }
    if (this.seen.size > 6000) {
      const live = new Set(positions.map((p) => p.id));
      for (const id of this.seen.keys()) if (!live.has(id)) this.seen.delete(id);
    }

    const pnl = sessionPnlOf(account);
    let crossed = false;
    if (pnl !== null) {
      const s = Math.max(0, Math.floor(pnl / PAYDAY_PNL_STEP_USD));
      if (this.step === null || !this.primed) this.step = s;
      else if (s > this.step) {
        this.step = s;
        crossed = true;
      }
    }
    this.primed = true;

    let ev: PaydayEvent | null = null;
    if (best && (best.pnlUsd as number) >= settlementThreshold(account?.totalUsd)) {
      ev = { kind: 'settlement', amountUsd: best.pnlUsd as number, label: label(best) };
    } else if (crossed && pnl !== null) {
      ev = { kind: 'milestone', amountUsd: pnl, label: 'session P&L' };
    }
    if (!ev) return null;
    // Throttled events are dropped, not queued: a celebration a minute late
    // is about a moment the viewer has already moved past. The step is still
    // marked reached, so it can't fire again later out of context.
    if (now - this.lastAt < PAYDAY_COOLDOWN_MS) return null;
    this.lastAt = now;
    return ev;
  }
}

/** The banner's second line: the real amount and what earned it. */
export function paydayLine(ev: PaydayEvent): string {
  const v = ev.amountUsd;
  const amt = `${v < 0 ? '−' : '+'}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  return ev.kind === 'milestone' ? `${amt} session P&L` : `${amt} · ${ev.label}`;
}
