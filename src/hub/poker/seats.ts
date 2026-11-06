import type { AgentCallActivity, ActivityEvent } from '../../state/activity';
import type { HubAgentScore, HubScoreboard, HubVerdict } from '../data';
import { isValidAgentId, type McpAgent } from '@shared/agents';
import { agentIdentity, hubAgentIdentity, type AgentIdentity } from '../../utils/agents';
import { MEMBER_IDS, PERSONAS, type MemberId } from './roster';

// The high-stakes table, played by REAL forecasters. When the user's agents
// have been forecasting (record_forecast), the six seats belong to them, not
// to the emotions: each named agent is a seat (forecast_ledger scores per
// agent: one agent driven from two clients is one record), its plate and its
// bet on the felt show its real fair value on the market being played, and
// the "WHO CALLED IT?" leaderboard shows its real Brier score and skill
// against the market price. PolyBot's Analyse
// button is number-free by design, so it never takes a seat. Three-free and
// pure, so every rule is unit-tested.
//
// "ticker" below is the market key: a Polymarket conditionId.
//
// The user's NAMED agents sit as themselves (utils/agents hubAgentIdentity):
// a forecast row or a live record_forecast that names its agent takes that
// agent's seat, in its own name and colour; one without (an older backend)
// keeps the client/model seat.
//
// The one rule that matters: NEVER INVENT A FORECAST. A seat with no forecast
// on the current market shows "—" and says nothing; a forecaster with no
// settled markets has no score, not 0. The persona table (Greed, Panic…) is
// the fallback under two real forecasters, and an explicit toggle.

export const MIN_REAL_FORECASTERS = 2;

export interface Call {
  /** Fair value for YES, 1..99. */
  fair: number;
  /** The market mid when it was made; null = no two-sided quote then. */
  mid: number | null;
  /** Epoch ms. */
  at: number;
}

export interface Forecaster {
  who: AgentIdentity;
  /** Its latest call per ticker. */
  calls: Map<string, Call>;
  /** From the scoreboard; null = nothing scored yet (not zero). */
  score: Pick<HubAgentScore, 'n' | 'nPaired' | 'brierAgent' | 'brierAgentPaired' | 'brierMarket' | 'skill' | 'verdict'> | null;
  total: number;
  lastAt: number;
}

export interface Seat {
  member: MemberId;
  f: Forecaster;
  /** Disambiguated when two seats would read the same. */
  label: string;
}

function ts(v: string | null | undefined): number {
  if (!v) return 0;
  // SQLite's datetime('now') is UTC without a zone: say so
  const t = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(v) ? v : `${v.replace(' ', 'T')}Z`);
  return Number.isFinite(t) ? t : 0;
}

/** An unsettled call older than this is no longer "on the table". */
export const OPEN_CALL_MS = 48 * 3600_000;

/** The roster key: one seat per agent id, whatever model or client drove
 *  it, matching forecast_ledger's per-agent scoring (`byAgent`). Only a call
 *  with no agent id (an older backend) falls back to its client identity. */
export function seatKey(who: AgentIdentity, agentId: string | null | undefined): string {
  return isValidAgentId(agentId) ? `agent|${agentId}` : who.id;
}

/** The scoreboard → forecasters keyed by agent. Calls come from `recent`
 *  (newest first): each agent's latest UNSETTLED call per market from the
 *  last 48 h. Scores come from `byAgent`. An older backend without either
 *  yields none (the personas sit). `agents` is the config's list (null = not
 *  loaded: client/model identities). */
export function forecastersFrom(
  board: HubScoreboard | null | undefined, agents: readonly McpAgent[] | null = null, now = Date.now(),
): Map<string, Forecaster> {
  const out = new Map<string, Forecaster>();
  const scores = new Map<string, HubAgentScore>();
  for (const r of board?.byAgent ?? []) if (r && typeof r.agentId === 'string') scores.set(r.agentId, r);
  const scoreFor = (aid: string | null | undefined): Forecaster['score'] => {
    const r = aid ? scores.get(aid) : undefined;
    return r && r.n > 0 ? {
      n: r.n, nPaired: r.nPaired, brierAgent: r.brierAgent, brierAgentPaired: r.brierAgentPaired ?? null,
      brierMarket: r.brierMarket, skill: r.skill, verdict: r.verdict,
    } : null;
  };
  // agents whose total the scoreboard already counted (byAgent.total)
  const counted = new Set<string>();
  for (const r of board?.recent ?? []) {
    if (!r || typeof r.conditionId !== 'string') continue;
    const who = hubAgentIdentity({ agentId: r.agentId, client: r.client, model: r.model, source: 'mcp' }, agents);
    const key = seatKey(who, r.agentId);
    let f = out.get(key);
    if (!f) {
      const sc = r.agentId ? scores.get(r.agentId) : undefined;
      f = { who, calls: new Map(), total: sc?.total ?? 0, lastAt: ts(sc?.lastAt ?? r.createdAt), score: scoreFor(r.agentId) };
      out.set(key, f);
      if (typeof sc?.total === 'number') counted.add(key);
    }
    if (!counted.has(key)) f.total++;
    const at = ts(r.createdAt);
    f.lastAt = Math.max(f.lastAt, at);
    if (r.outcome !== null && r.outcome !== undefined) continue; // settled: scored, not on the table
    if (at && now - at > OPEN_CALL_MS) continue;
    if (f.calls.has(r.conditionId)) continue; // newest first: keep that one
    const fair = r.fairValueCents;
    if (typeof fair !== 'number' || !(fair >= 1 && fair <= 99)) continue;
    const mid = typeof r.marketMidCents === 'number' && r.marketMidCents >= 1 && r.marketMidCents <= 99 ? r.marketMidCents : null;
    f.calls.set(r.conditionId, { fair, mid, at });
  }
  // an agent with a record but nothing in the recent window still sits
  for (const r of scores.values()) {
    const who = hubAgentIdentity({ agentId: r.agentId, client: r.clients?.[0] ?? null, model: r.models?.[0] ?? null, source: 'mcp' }, agents);
    const key = seatKey(who, r.agentId);
    if (out.has(key)) continue;
    out.set(key, { who, calls: new Map(), total: r.total ?? r.n, lastAt: ts(r.lastAt), score: scoreFor(r.agentId) });
  }
  return out;
}

/** A forecast that just happened (an agent's record_forecast, or the Analyse
 *  button), folded into the roster without waiting for the next scoreboard
 *  read. Returns the ticker it was about, or null if it carried no number. */
export function noteLiveForecast(
  roster: Map<string, Forecaster>, ev: ActivityEvent, at: number, agents: readonly McpAgent[] | null = null,
): { ticker: string; seatId: string } | null {
  let who: AgentIdentity;
  let agentId: string | null = null;
  let ticker: string | null;
  let fair: number | null;
  // The mid it was argued against; the Analyse button's event carries none,
  // so its call casts no vote until the scoreboard read supplies one.
  let mid: number | null = null;
  if (ev.kind === 'agentCall') {
    const c = ev as AgentCallActivity;
    if (c.call !== 'call' || c.tool !== 'record_forecast' || c.outcome !== 'ok') return null;
    who = hubAgentIdentity(c, agents);
    agentId = c.agentId ?? null;
    ticker = c.conditionId;
    fair = c.fairCents;
    mid = c.midCents;
  } else if (ev.kind === 'aiAnalysis') {
    who = agentIdentity({ source: 'panel', model: ev.provider === 'openai' ? 'gpt' : ev.provider === 'anthropic' ? 'claude' : ev.provider });
    ticker = ev.conditionId;
    fair = ev.fairCents;
  } else {
    return null;
  }
  // No number, no forecast: the Analyse button's "unclear" is not a seat value.
  if (!ticker || fair === null || !(fair >= 1 && fair <= 99)) return null;
  const key = seatKey(who, agentId);
  let f = roster.get(key);
  if (!f) {
    f = { who, calls: new Map(), score: null, total: 0, lastAt: at };
    roster.set(key, f);
  } else if (!f.who.model && who.model) {
    f.who = who; // the model named on this call goes on the plate
  }
  f.total++;
  f.lastAt = Math.max(f.lastAt, at);
  const prev = f.calls.get(ticker);
  // A replayed older call never overwrites a newer one.
  if (!prev || prev.at <= at) f.calls.set(ticker, { fair, mid, at });
  return { ticker, seatId: key };
}

/** Seat order: the busiest forecasters take the centre seats, where the
 *  camera looks hardest. */
const SEAT_ORDER = [2, 3, 1, 4, 0, 5];

/** Up to six real forecasters, most recently active first. */
export function assignSeats(roster: Map<string, Forecaster>, max = MEMBER_IDS.length): Seat[] {
  const fs = [...roster.values()]
    .filter((f) => f.total > 0 || f.calls.size > 0)
    .sort((a, b) => b.lastAt - a.lastAt || b.total - a.total)
    .slice(0, Math.min(max, MEMBER_IDS.length));
  const seats = fs.map((f, i) => ({ member: MEMBER_IDS[SEAT_ORDER[i]], f, label: f.who.label }));
  // Two seats that would read the same ("Analyse · Claude" for two Claude
  // models) are told apart by their model rather than left ambiguous.
  const dup = new Map<string, number>();
  for (const s of seats) dup.set(s.label, (dup.get(s.label) ?? 0) + 1);
  for (const s of seats) {
    if ((dup.get(s.label) ?? 0) > 1) s.label = `${s.label} (${s.f.who.seatId.split('|').pop() || '?'})`;
  }
  return seats;
}

export type TableChoice = 'personas' | 'agents';

/** Who plays. Real forecasters by default once there are two; the personas
 *  under two (one player is not a game), or when asked for. */
export function tableMode(choice: TableChoice | null, realCount: number): TableChoice {
  if (realCount < MIN_REAL_FORECASTERS) return 'personas';
  return choice ?? 'agents';
}

/** A seat's real fair value on `ticker`, or null — drawn as "—". */
export function seatValue(seat: Seat | undefined, ticker: string | null): number | null {
  if (!seat || !ticker) return null;
  return seat.f.calls.get(ticker)?.fair ?? null;
}

export function cents(v: number): string {
  return `${Math.round(v)}¢`;
}

/** "Claude Code · opus · 61¢", or "… · —" with no call on this market; a
 *  named agent's plate leads with its emoji. */
export function plateText(seat: Seat, ticker: string | null): string {
  const v = seatValue(seat, ticker);
  const name = seat.f.who.emoji ? `${seat.f.who.emoji} ${seat.label}` : seat.label;
  return `${name} · ${v === null ? '—' : cents(v)}`;
}

/** A seat's bet on a market: YES is cheap (fair above the mid it saw), NO
 *  is cheap (below), or no bet — no call, no mid then, or exactly the mid. */
export function seatVote(seat: Seat | undefined, ticker: string | null): boolean | null {
  if (!seat || !ticker) return null;
  const c = seat.f.calls.get(ticker);
  if (!c || c.mid === null || c.fair === c.mid) return null;
  return c.fair > c.mid;
}

/** What a seat says on a market: its own number, and the mid it argued
 *  against when there was one. A model is never given words it did not say;
 *  a NAMED agent may add a few words of its guide, in quotes — the user's
 *  own description of it, digit-free (guideMotto), so the only number on the
 *  line is still its forecast. No call on the market, nothing said. */
export function seatLine(seat: Seat, ticker: string): string | null {
  const c = seat.f.calls.get(ticker);
  if (!c) return null;
  const line = c.mid === null ? `fair ${cents(c.fair)}` : `fair ${cents(c.fair)} · mid was ${cents(c.mid)}`;
  return seat.f.who.motto ? `${line} · “${seat.f.who.motto}”` : line;
}

// ── "WHO CALLED IT?" ───────────────────────────────────────────────────────

export interface ScoreLine {
  name: string;
  color: string;
  /** The big number on the right; "—" when there is none. */
  value: string;
  /** A small line under the name. */
  sub?: string;
  /** 0..1 fill, or -1..1 when `signed`; null = no bar (nothing to score). */
  bar: number | null;
  signed?: boolean;
  tone: 'good' | 'bad' | 'neutral' | 'none';
}

export interface ScoreView { title: string; caption: string; lines: ScoreLine[] }

const TONE_OF: Record<HubVerdict, ScoreLine['tone']> = {
  'agent-better': 'good', 'market-better': 'bad', indistinguishable: 'neutral', 'too-few': 'neutral',
};

/** The real forecasters' scoreboard: skill vs the market price (1 − Brier
 *  agent / Brier market, on markets that had a quote), coloured by the
 *  scoreboard's own VERDICT — a positive skill over six markets is "too few",
 *  not a win. */
export function agentScoreView(seats: Seat[]): ScoreView {
  const lines = seats.map((s): ScoreLine & { k: number } => {
    const sc = s.f.score;
    const skill = sc?.skill ?? null;
    const brier = sc ? (sc.brierAgentPaired ?? sc.brierAgent) : null;
    const open = s.f.calls.size;
    const sub = sc && brier !== null
      ? `Brier ${brier.toFixed(3)}${sc.brierMarket !== null ? ` vs mkt ${sc.brierMarket.toFixed(3)}` : ''} · ${sc.nPaired} settled`
      : `nothing settled yet · ${open} open`;
    return {
      name: s.label, color: s.f.who.color, sub,
      value: skill === null ? '—' : `${skill > 0 ? '+' : skill < 0 ? '−' : ''}${Math.abs(skill * 100).toFixed(0)}%`,
      bar: skill === null ? null : Math.max(-1, Math.min(1, skill * 2)),
      signed: true,
      tone: skill === null || !sc ? 'none' : TONE_OF[sc.verdict],
      k: skill === null ? -Infinity : skill,
    };
  });
  lines.sort((a, b) => b.k - a.k);
  return { title: 'WHO CALLED IT?', caption: 'skill vs market · settled', lines: lines.map(({ k: _k, ...l }) => l) };
}

/** The personas' record: how often each bias's own rule called a settled
 *  trade right this session (table.ts `settle`). Unchanged wording. */
export function personaScoreView(rows: { id: MemberId; right: number; n: number }[]): ScoreView {
  const sorted = [...rows].sort((a, b) => (b.n ? b.right / b.n : -1) - (a.n ? a.right / a.n : -1));
  return {
    title: 'WHO CALLED IT?',
    caption: 'call accuracy · settled trades',
    lines: sorted.map((r) => {
      const pct = r.n ? (r.right / r.n) * 100 : null;
      return {
        name: PERSONAS[r.id].name, color: PERSONAS[r.id].color,
        value: pct === null ? '—' : `${pct.toFixed(0)}%`,
        bar: pct === null ? null : pct / 100,
        tone: pct === null ? 'none' : pct >= 55 ? 'good' : pct < 45 ? 'bad' : 'neutral',
      };
    }),
  };
}

/** The newest real calls, oldest first, as the table's opening hands:
 *  what the forecasters actually did just before the scene opened. */
export function recentTickers(seats: Seat[], max = 3): string[] {
  const latest = new Map<string, number>();
  for (const s of seats) {
    for (const [t, c] of s.f.calls) latest.set(t, Math.max(latest.get(t) ?? 0, c.at));
  }
  return [...latest.entries()].sort((a, b) => b[1] - a[1]).slice(0, max).map(([t]) => t).reverse();
}
