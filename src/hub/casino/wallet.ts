import type { Ticket } from './sportsbook';

// Play chips, kept in this browser profile's localStorage: a per-viewer
// convenience, never money, never sent anywhere. Every read and write is
// wrapped: a private window or blocked storage still plays, it just forgets.

export const START_CHIPS = 1000;
/** Under this the cashier will top you up to START_CHIPS. */
export const REFILL_BELOW = 50;
const KEY = 'krypt.hub.casino.v1';
const MAX_TICKETS = 60;

export interface CasinoStats { rounds: number; won: number; staked: number; returned: number }

export interface CasinoSave {
  chips: number;
  /** The user's agents' own stacks, by agent identity id. */
  agents: Record<string, number>;
  tickets: Ticket[];
  stats: CasinoStats;
  refills: number;
}

function fresh(): CasinoSave {
  return { chips: START_CHIPS, agents: {}, tickets: [], stats: { rounds: 0, won: 0, staked: 0, returned: 0 }, refills: 0 };
}

function whole(v: unknown, d: number): number {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.floor(v) : d;
}

/** Parse a stored save, keeping what is well-formed and defaulting the rest. */
export function parseSave(raw: string | null): CasinoSave {
  const out = fresh();
  if (!raw) return out;
  let v: unknown;
  try { v = JSON.parse(raw); } catch { return out; }
  if (!v || typeof v !== 'object') return out;
  const o = v as Record<string, unknown>;
  out.chips = whole(o.chips, START_CHIPS);
  out.refills = whole(o.refills, 0);
  if (o.agents && typeof o.agents === 'object') {
    for (const [k, n] of Object.entries(o.agents as Record<string, unknown>)) {
      if (typeof k === 'string' && k.length < 120) out.agents[k] = whole(n, START_CHIPS);
    }
  }
  if (o.stats && typeof o.stats === 'object') {
    const s = o.stats as Record<string, unknown>;
    out.stats = { rounds: whole(s.rounds, 0), won: whole(s.won, 0), staked: whole(s.staked, 0), returned: whole(s.returned, 0) };
  }
  if (Array.isArray(o.tickets)) {
    out.tickets = o.tickets.filter((t): t is Ticket => !!t && typeof t === 'object'
      && typeof (t as Ticket).id === 'string' && typeof (t as Ticket).conditionId === 'string'
      && ((t as Ticket).side === 'yes' || (t as Ticket).side === 'no')
      && typeof (t as Ticket).priceCents === 'number' && typeof (t as Ticket).stake === 'number'
      && ['open', 'won', 'lost', 'void'].includes((t as Ticket).status)).slice(-MAX_TICKETS);
  }
  return out;
}

export function loadSave(): CasinoSave {
  try {
    return parseSave(window.localStorage.getItem(KEY));
  } catch {
    return fresh();
  }
}

export function storeSave(s: CasinoSave): void {
  try {
    const keep = { ...s, tickets: s.tickets.slice(-MAX_TICKETS) };
    window.localStorage.setItem(KEY, JSON.stringify(keep));
  } catch {
    /* storage blocked: the chips live for this visit only */
  }
}

/** The house's one rule: you can always get back to the table. */
export function canRefill(chips: number): boolean {
  return chips < REFILL_BELOW;
}

/** Book a finished round on the user's stats. */
export function record(s: CasinoSave, staked: number, returned: number): void {
  s.stats.rounds++;
  s.stats.staked += staked;
  s.stats.returned += returned;
  if (returned > staked) s.stats.won++;
}
