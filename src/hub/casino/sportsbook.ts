// The sportsbook's board is REAL: Polymarket's own sports markets at their
// live prices (the app's read-only Discover call, see hub/data.ts). A ticket
// is play chips at that price, and it settles only on Polymarket's real
// resolution. No price on the board is made up: a side nobody offers is not
// on the board.

/** The subset of a Discover row the board reads (shared/market.ts
 *  MarketSummary), so an older backend missing a field is just "absent". */
export interface MarketRow {
  conditionId: string;
  title: string;
  eventSlug?: string | null;
  outcomes?: [string, string] | null;
  bid?: number | null;
  ask?: number | null;
  volume24hUsd?: number | null;
  hoursToClose?: number | null;
  closeTime?: string | null;
  status?: string | null;
  resolvedOutcome?: 'yes' | 'no' | null;
  category?: string | null;
}

export interface BoardRow {
  conditionId: string;
  title: string;
  /** The game it belongs to (a match has many markets). */
  event: string | null;
  yesLabel: string;
  noLabel: string;
  /** What a YES costs now (the ask), cents; null when nobody offers it. */
  yesCents: number | null;
  /** What a NO costs now (100 − the YES bid), cents. */
  noCents: number | null;
  volume24hUsd: number | null;
  hoursToClose: number | null;
}

function cents(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 && v < 100 ? v : null;
}

/** A board row, or null when the market can't be bet on: not open, no price
 *  on either side, or already past its close. */
export function boardRow(m: MarketRow): BoardRow | null {
  if (!m || typeof m.conditionId !== 'string' || !m.conditionId) return null;
  if (m.status && m.status !== 'open' && m.status !== 'active') return null;
  if (m.resolvedOutcome) return null;
  if (typeof m.hoursToClose === 'number' && m.hoursToClose < 0) return null;
  const yes = cents(m.ask);
  const bid = cents(m.bid);
  const no = bid === null ? null : Math.round((100 - bid) * 10) / 10;
  if (yes === null && no === null) return null;
  const [y, n] = Array.isArray(m.outcomes) && m.outcomes.length >= 2 ? m.outcomes : ['Yes', 'No'];
  return {
    conditionId: m.conditionId,
    title: String(m.title || m.conditionId),
    event: typeof m.eventSlug === 'string' && m.eventSlug ? m.eventSlug : null,
    yesLabel: String(y || 'Yes'),
    noLabel: String(n || 'No'),
    yesCents: yes,
    noCents: no,
    volume24hUsd: typeof m.volume24hUsd === 'number' ? m.volume24hUsd : null,
    hoursToClose: typeof m.hoursToClose === 'number' ? m.hoursToClose : null,
  };
}

/** A market with a real contest in it: some side priced between 3¢ and 97¢.
 *  A season-long future at 0.2¢ / 99.8¢ is a real market too, but a board of
 *  them reads as a list of long shots, not a sportsbook. */
export function inPlay(b: BoardRow): boolean {
  const live = (c: number | null) => c !== null && c >= 3 && c <= 97;
  return live(b.yesCents) || live(b.noCents);
}

/** The board: deduplicated; games in the next week first, soonest first,
 *  then everything else busiest first; at most `perEvent` markets from one
 *  game (a match lists a dozen side markets) and `max` rows in all. */
export function buildBoard(rows: readonly MarketRow[], max = 40, perEvent = 2): BoardRow[] {
  const seen = new Set<string>();
  const out: BoardRow[] = [];
  for (const m of rows) {
    const b = boardRow(m);
    if (!b || seen.has(b.conditionId) || !inPlay(b)) continue;
    seen.add(b.conditionId);
    out.push(b);
  }
  const soon = (b: BoardRow) => b.hoursToClose !== null && b.hoursToClose <= 24 * 7;
  out.sort((a, b) => {
    if (soon(a) !== soon(b)) return soon(a) ? -1 : 1;
    if (soon(a)) return (a.hoursToClose ?? 0) - (b.hoursToClose ?? 0);
    return (b.volume24hUsd ?? -1) - (a.volume24hUsd ?? -1);
  });
  const per = new Map<string, number>();
  const kept = out.filter((b) => {
    if (!b.event) return true;
    const n = per.get(b.event) ?? 0;
    per.set(b.event, n + 1);
    return n < perEvent;
  });
  return kept.slice(0, max);
}

/** Decimal odds for a price: a 40¢ side pays 2.50 per chip staked. */
export function decimalOdds(c: number): number {
  return 100 / c;
}

/** American odds: favourites negative ("−150"), underdogs positive ("+150"). */
export function americanOdds(c: number): string {
  const p = c / 100;
  if (p >= 0.5) return `−${Math.round((p / (1 - p)) * 100)}`;
  return `+${Math.round(((1 - p) / p) * 100)}`;
}

export function closesIn(h: number | null): string {
  if (h === null) return '—';
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m`;
  if (h < 48) return `${Math.round(h)}h`;
  return `${Math.round(h / 24)}d`;
}

export type TicketStatus = 'open' | 'won' | 'lost' | 'void';

export interface Ticket {
  id: string;
  conditionId: string;
  title: string;
  side: 'yes' | 'no';
  label: string;
  /** The price the ticket was written at, cents. */
  priceCents: number;
  stake: number;
  placedAt: number;
  status: TicketStatus;
  /** Chips paid back when it settled (stake included). */
  paid: number;
}

/** What a winning ticket pays back, stake included, in whole chips. */
export function ticketReturn(stake: number, priceCents: number): number {
  return Math.floor((stake * 100) / priceCents);
}

export function writeTicket(row: BoardRow, side: 'yes' | 'no', stake: number, now: number): Ticket | null {
  const price = side === 'yes' ? row.yesCents : row.noCents;
  if (price === null || !(stake > 0)) return null;
  return {
    id: `${now.toString(36)}${Math.random().toString(36).slice(2, 6)}`,
    conditionId: row.conditionId,
    title: row.title,
    side,
    label: side === 'yes' ? row.yesLabel : row.noLabel,
    priceCents: price,
    stake: Math.floor(stake),
    placedAt: now,
    status: 'open',
    paid: 0,
  };
}

/** Settle an open ticket against Polymarket's real result. A market that
 *  closed without a yes/no outcome (cancelled, 50-50) voids it: the stake
 *  comes back. Anything else leaves it open. */
export function settleTicket(t: Ticket, m: { resolvedOutcome?: 'yes' | 'no' | null; status?: string | null } | null): Ticket {
  if (t.status !== 'open' || !m) return t;
  if (m.resolvedOutcome === 'yes' || m.resolvedOutcome === 'no') {
    const won = m.resolvedOutcome === t.side;
    return { ...t, status: won ? 'won' : 'lost', paid: won ? ticketReturn(t.stake, t.priceCents) : 0 };
  }
  if (m.status === 'void' || m.status === 'cancelled') return { ...t, status: 'void', paid: t.stake };
  return t;
}
