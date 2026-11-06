import type { BotPosition } from '@shared/types';

// The Agent Hub page's Wins / Losses pots: every settlement this session,
// counted once. Pure and three-free so the page (eager) can use it without
// pulling a scene in, and so it can be tested.

export interface Pot {
  wins: number;
  losses: number;
  winPnl: number;
  lossPnl: number;
}

export const EMPTY_POT: Pot = { wins: 0, losses: 0, winPnl: 0, lossPnl: 0 };

/** Above this many remembered position ids, forget the ones that have rolled
 *  off the positions feed — they can't change again. */
export const SEEN_CAP = 4000;

/**
 * Count the settlements in `positions` that `seen` hasn't counted yet, and
 * remember them. A position's id maps to whether it has been counted, so a
 * settled position that the feed re-sends (or whose P&L is corrected) is
 * still ONE win or ONE loss.
 *
 * `sessionStart` (ms) keeps the pots to this session: the feed also carries
 * positions that settled before the app started, and opening the page used to
 * pour all of them in as if they had just happened. A position with no
 * `resolvedAt` can't be placed in time and is left out rather than guessed.
 * A dry run never reached the exchange, so it is no one's win.
 */
export function countPot(seen: Map<number, boolean>, positions: BotPosition[], sessionStart: number | null): Pot {
  const d: Pot = { ...EMPTY_POT };
  for (const p of positions) {
    if (seen.get(p.id)) continue;
    if (!p.resolved || (p.outcomeCorrect !== 0 && p.outcomeCorrect !== 1)) continue;
    if (p.status === 'dry_run') { seen.set(p.id, true); continue; }
    if (sessionStart !== null) {
      const at = p.resolvedAt ? Date.parse(p.resolvedAt) : NaN;
      if (!Number.isFinite(at) || at < sessionStart) { seen.set(p.id, true); continue; }
    }
    seen.set(p.id, true);
    const pnl = typeof p.pnlUsd === 'number' && Number.isFinite(p.pnlUsd) ? p.pnlUsd : 0;
    if (p.outcomeCorrect === 1) { d.wins++; d.winPnl += pnl; } else { d.losses++; d.lossPnl += pnl; }
  }
  if (seen.size > SEEN_CAP) {
    const live = new Set(positions.map((p) => p.id));
    for (const id of seen.keys()) if (!live.has(id)) seen.delete(id);
  }
  return d;
}

export function addPot(a: Pot, b: Pot): Pot {
  return { wins: a.wins + b.wins, losses: a.losses + b.losses, winPnl: a.winPnl + b.winPnl, lossPnl: a.lossPnl + b.lossPnl };
}
