import type { AccountSnapshot, BotPosition, TraderConfig } from '@shared/types';
import type { SavedBacktest } from './library';
import type { MarketRow } from './casino/sportsbook';
import { sessionPnlOf } from './payday';

// ── what the hub reads from the backend, structurally ─────────────────────
//
// The hub reads three things beyond useApp(): Autopilot's status, the
// forecast scoreboard (python/forecast_ledger.py `scoreboard`) and, for the
// poker table, nothing else. Each is typed HERE as the subset the hub actually
// reads, so the full response types in shared/market.ts can grow without the
// hub caring, and an older backend that lacks a field is just "absent".

export interface HubAutopilot {
  enabled?: boolean;
  running?: boolean;
  step?: number;
  /** Newest first. */
  runs?: { finishedAt: string | null; trigger?: string | null }[];
}

/** Agent minus market on the paired subset, as forecast_ledger names it. */
export type HubVerdict = 'too-few' | 'indistinguishable' | 'agent-better' | 'market-better';

/** One named agent's record (`byAgent`): its own latest call per settled
 *  market, scored against the mid it saw. */
export interface HubAgentScore {
  agentId: string;
  n: number;
  nPaired: number;
  brierAgent: number | null;
  brierAgentPaired?: number | null;
  brierMarket: number | null;
  skill: number | null;
  verdict: HubVerdict;
  total?: number;
  pending?: number;
  lastAt?: string | null;
  models?: string[];
  clients?: string[];
}

/** One forecast (`recent`, newest first). `outcome` null = not settled. */
export interface HubForecastRow {
  id?: number;
  createdAt: string | null;
  conditionId: string;
  title: string | null;
  model: string | null;
  client: string | null;
  agentId?: string | null;
  fairValueCents: number | null;
  marketMidCents: number | null;
  outcome?: number | null;
}

export interface HubScoreboard {
  byAgent?: HubAgentScore[];
  recent?: HubForecastRow[];
}

/** The read-only backend calls the hub makes, through the preload surface
 *  (docs/KRYPTRADER_PORT.md §5c). Optional-chained end to end: an older
 *  preload without the group answers undefined, never throws. */
interface HubReads {
  autopilot?: { status?: () => Promise<HubAutopilot | null> };
  ai?: { scoreboard?: (recent?: number) => Promise<HubScoreboard | null> };
  terminal?: {
    discover?: (args: { column: string; limit?: number; filters?: { categories?: string[] } }) => Promise<{ rows?: unknown[]; note?: string | null } | null>;
    market?: (conditionId: string) => Promise<{ market?: unknown } | null>;
  };
}

/** The Casino sportsbook's board: Polymarket's own sports markets, as the
 *  Discover page reads them. */
export interface HubSportsbook { rows: MarketRow[]; at: number; note: string | null }

function isRow(r: unknown): r is MarketRow {
  return !!r && typeof r === 'object' && typeof (r as MarketRow).conditionId === 'string' && typeof (r as MarketRow).title === 'string';
}

/** Two Discover reads (the busiest sports markets, and those closing
 *  soonest: tonight's games), through the backend's Polymarket client.
 *  undefined = this build has no Discover call. */
export async function readSportsMarkets(): Promise<HubSportsbook | null | undefined> {
  const fn = reads()?.terminal?.discover;
  if (typeof fn !== 'function') return undefined;
  const filters = { categories: ['sports'] };
  const [vol, soon] = await Promise.all([
    fn({ column: 'volume', limit: 100, filters }).catch(() => null),
    fn({ column: 'closing', limit: 100, filters }).catch(() => null),
  ]);
  if (!vol && !soon) return null;
  const rows = [...(vol?.rows ?? []), ...(soon?.rows ?? [])].filter(isRow);
  return { rows, at: Date.now(), note: rows.length ? null : (vol?.note ?? soon?.note ?? null) };
}

/** The markets open sportsbook tickets wait on, one read each. */
export async function readMarkets(ids: readonly string[]): Promise<MarketRow[] | undefined> {
  const fn = reads()?.terminal?.market;
  if (typeof fn !== 'function') return undefined;
  const out: MarketRow[] = [];
  for (const id of ids) {
    try {
      const r = await fn(id);
      if (isRow(r?.market)) out.push(r!.market as MarketRow);
    } catch { /* one market that won't load leaves its ticket open */ }
  }
  return out;
}

function reads(): HubReads | undefined {
  return (window as unknown as { krypt?: HubReads }).krypt;
}

/** Autopilot's status (in-memory + local SQLite; no network). undefined =
 *  this build has no such call. */
export async function readAutopilot(): Promise<HubAutopilot | null | undefined> {
  const fn = reads()?.autopilot?.status;
  if (typeof fn !== 'function') return undefined;
  return (await fn()) ?? null;
}

/** The forecast scoreboard (one local SQLite read + pure scoring). */
export async function readScoreboard(recent = 200): Promise<HubScoreboard | null | undefined> {
  const fn = reads()?.ai?.scoreboard;
  if (typeof fn !== 'function') return undefined;
  return (await fn(recent)) ?? null;
}

/** One thing an engine did, as a scene stages it. This venue has no public
 *  trade tape to scan, so the scenes' queue is the engines' own orders. */
export interface HubSignal {
  id: number;
  source: string;
  ticker: string;
  eventTicker: string;
  title: string;
  category: string;
  direction: 'yes' | 'no';
  priceCents: number;
  confidence: number;
  edgePts: number;
  createdAt: string;
  /** False when the order never produced a fill (canceled, expired, error). */
  traded: boolean;
}

/** The engines' orders as hub signals, same order as the position list. */
export function hubSignals(positions: readonly BotPosition[]): HubSignal[] {
  return positions.map((p) => ({
    id: p.id,
    source: p.signalSource,
    ticker: p.ticker,
    eventTicker: p.eventTicker,
    title: p.title,
    category: p.category,
    direction: p.direction,
    priceCents: p.signalPriceCents || p.limitPriceCents,
    confidence: p.confidence,
    edgePts: p.edgePts,
    createdAt: p.createdAt,
    traded: p.status === 'submitted' || p.status === 'partial' || p.status === 'filled',
  }));
}

/** Everything a hub scene reads, straight from `useApp()`. Kept in its own
 *  three-free module so the stage (in the page bundle's lazy chunk) and every
 *  scene agree on one shape.
 *
 *  The Agent Hub is READ-ONLY. A scene gets this snapshot (plus the activity
 *  bus's past-tense events, see state/activity.ts) and nothing else: no
 *  `window.krypt`, no order path, no settings writes. What it draws is what
 *  already happened. The stage adds two cheap reads: the saved backtest
 *  results (library.ts, cached) and, for the Casino's VIP lounge, the
 *  Autopilot status (in-memory + local SQLite, see HubStage). */
export interface HubData {
  signals: HubSignal[];
  positions: BotPosition[];
  account: AccountSnapshot | null;
  config?: TraderConfig | null;
  /** Last SAVED backtest per strategy; empty until one has been run. */
  library?: SavedBacktest[];
  /** Autopilot's status; undefined when this scene didn't ask for it, null
   *  when the read failed (backend down). */
  autopilot?: HubAutopilot | null;
  /** The forecast scoreboard (local SQLite, no network), for High Stakes'
   *  real forecasters; undefined when the scene didn't ask for it, null when
   *  the read failed. */
  scoreboard?: HubScoreboard | null;
  /** High Stakes' "Personas | Your agents" toggle; null = not chosen. */
  councilChoice?: 'personas' | 'agents' | null;
  /** The Casino's sportsbook board (Polymarket sports markets); undefined
   *  when the scene didn't ask, null when the read failed. */
  sportsbook?: HubSportsbook | null;
}

/** The HUD's numbers, with every one nobody could produce left `null` (drawn
 *  as an em dash). The backend fills an account it has never read with zeros:
 *  a fresh install with no keys reports `winRate: 0`, `sessionPnlUsd: 0` and
 *  `totalUsd: 0`, and a HUD that prints "+$0.00 · 0.0% win rate" over that is
 *  telling the user something about a bot that has never traded. */
export interface HudNumbers {
  pnl: number | null;
  roi: number | null;
  winRate: number | null;
  /** Bot trades settled today (wins + losses). */
  settledToday: number | null;
  open: number | null;
}

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export function hudNumbers(a: AccountSnapshot | null): HudNumbers {
  const pnl = sessionPnlOf(a);
  const wins = num(a?.wins);
  const losses = num(a?.losses);
  const tw = num(a?.todayWins);
  const tl = num(a?.todayLosses);
  return {
    pnl,
    roi: pnl === null ? null : num(a?.sessionRoiPct),
    // A win rate over zero settled trades is not 0%, it's undefined.
    winRate: wins !== null && losses !== null && wins + losses > 0 ? num(a?.winRate) : null,
    settledToday: tw !== null && tl !== null ? tw + tl : null,
    open: num(a?.openCount),
  };
}

/** The account balance, only when there is one: a zero total with no session
 *  baseline is "never read" (no keys, backend still starting), not "broke". */
export function balanceOf(a: AccountSnapshot | null): number | null {
  if (!a) return null;
  const t = num(a.totalUsd);
  if (t === null) return null;
  if (t > 0) return t;
  return num(a.sessionBaselineUsd) !== null && (a.sessionBaselineUsd as number) > 0 ? t : null;
}
