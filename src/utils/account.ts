import type { AccountMode } from '@shared/market';
import type { TraderConfig } from '@shared/types';

/**
 * The app-wide account mode, read from config.
 *
 * Paper is Polymarket's real order books with imaginary money; Live is the
 * user's own account. Paper is the master: in Paper the terminal, phone,
 * standing rules and agents trade paper, and the engines send nothing (the
 * backend gates every signer path), so the renderer only needs to SAY which
 * one is in force — it never decides it.
 */

type Cfg = Partial<Pick<TraderConfig, 'accountMode'>> | null | undefined;

/** Live only on the exact word; anything else (missing, loading) is Paper,
 *  the side that cannot spend money. */
export function isLive(c: Cfg): boolean {
  return c?.accountMode === 'live';
}

export function accountModeOf(c: Cfg): AccountMode {
  return isLive(c) ? 'live' : 'paper';
}

export const MODE_LABEL: Record<AccountMode, string> = {
  paper: 'Paper',
  live: 'Live',
};

/** The patch for "Back to paper" on the account. Nothing else changes: the
 *  engines keep their own switches, which simply send nothing in Paper. */
export function backToPaperAccountPatch(): Partial<TraderConfig> {
  return { accountMode: 'paper' };
}

/** The patch for Go live, once credentials passed and the user ticked. */
export function accountGoLivePatch(): Partial<TraderConfig> {
  return { accountMode: 'live' };
}
