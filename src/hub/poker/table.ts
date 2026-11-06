import type { BotPosition } from '@shared/types';
import type { HubSignal } from '../data';
import type { ActivityEvent } from '../../state/activity';
import { aiName } from '../engine/jobs';
import type { HubData } from '../data';
import { subject } from '../text';
import type { Member, Pose } from './members';
import { MEMBER_IDS, PERSONAS, line, type Features, type MemberId, type Speaker } from './roster';
import type { PokerRoom, ScoreRow } from './room';
import { cents, personaScoreView, seatLine, seatValue, seatVote, type Seat } from './seats';

// The table's loop. The bot trades dozens of times a second; a hand a viewer
// can follow takes ten. So engine orders queue, ONE hand is played
// at a time, and everything else is counted but not staged:
//
//   present (the dealer deals the market) → argue (table talk: 3–4 voices,
//     overlapping) → vote (each player pushes chips onto YES or NO, one by
//     one) → verdict (the showdown: the dealer reveals the bot's real call)
//     → out (bets come back, the card is mucked) → next
//
// The showdown is the bot's real decision (`signal.traded`), not the bets:
// when the table bet against what the bot actually did, the dealer calls it
// OVERRULED — the drama comes from the data, not from dice.
//
// The table is decoration over a decision already made. It has no order path
// and writes nothing: no `window.krypt`, no config, no backend call. The
// personas are an argument staged around the bot's call (in the spirit of
// invariant 8, "the AI is an argument, never an order"), and nothing they
// bet can change what the bot does. The chips are props: no stake, no money.
//
// AGENTS MODE (seats.ts). When the user's real forecasters hold the seats,
// the hands are the markets they actually forecast (live record_forecast /
// Analyse events, or their newest open calls when the scene opens), each
// seat says only its own number and the mid it argued against, a seat with
// no call on the market stays silent behind a "—", and its bet is that call
// against its mid. There is no showdown to fake: the stamp is the range of
// what was really said. None of the personas' scripted lines (gloating,
// sulking, PAYDAY cheers) are ever put in a real agent's mouth.

/** Share of hands left to the raw rule bets, overrule and all. */
export const OVERRULE_RATE = 0.12;

/** The players who can be talked round. Greed and Panic stay pure to type:
 *  that is the joke. */
export const PERSUADABLE: MemberId[] = ['quant', 'fomo', 'doubt', 'risk'];

/** Read the table: swing the persuadable players, in order, toward the bot's
 *  actual decision until the majority matches it. Pure; returns a copy.
 *
 *  Why: no persona's rule models the bot, and the rules lean NO (Panic and
 *  Doubt rarely say yes, Risk refuses once the book nears its cap), so left
 *  alone roughly every other hand ended in "OVERRULED!" — and a twist that
 *  happens every time stops being one. The leaderboard never sees these
 *  swung bets; it scores each persona's own rule (see `settle`). */
export function readTheRoom(votes: Record<MemberId, boolean>, target: boolean): Record<MemberId, boolean> {
  const out = { ...votes };
  const majority = () => {
    const y = MEMBER_IDS.filter((id) => out[id]).length;
    return target ? y > MEMBER_IDS.length - y : MEMBER_IDS.length - y > y;
  };
  for (const id of PERSUADABLE) {
    if (majority()) break;
    out[id] = target;
  }
  return out;
}

export type Phase = 'idle' | 'present' | 'argue' | 'vote' | 'verdict' | 'out';

interface Cue { at: number; done: boolean; run: () => void }

export interface HandView {
  n: number;
  phase: Phase;
  t: number;
  /** The bot signal being played; null for an agents-mode hand. */
  sig: HubSignal | null;
  f: Features;
  votes: Record<MemberId, boolean>;
  yes: number;
  no: number;
  shownVotes: number;
  approved: boolean | null;
  stampText: string;
  stampSub: string;
  stampT: number | null;
  outT: number;
  /** Agents mode: the market, and the card's own words. */
  agent?: { ticker: string; kicker: string; dirLine: string; facts: string; stampColor: string };
  /** When this hand leaves the table, if not the persona default. */
  doneAt?: number;
}

export interface TableHost {
  members: Record<MemberId, Member>;
  room: PokerRoom;
  say(who: Speaker, text: string, opts?: { life?: number; force?: boolean }): boolean;
  log(text: string, tone: 'win' | 'loss' | 'gold' | 'neutral' | 'scan' | 'momo' | 'test'): void;
  speaking(who: Speaker | null): void;
  coins(n: number): void;
  /** A real settlement, staged as a showdown (persona table only). */
  showdown?(won: boolean, pnl: string, subj: string): void;
}

const T_ARGUE = 0.9;
const T_VOTE = 5.6;
const T_VERDICT = 7.6;
const T_OUT = 10.6;
const T_DONE = 11.4;

function money(v: number): string {
  return `$${Math.abs(v).toFixed(2)}`;
}

function pick<T>(xs: T[]): T {
  return xs[Math.floor(Math.random() * xs.length)];
}

export class PokerTable {
  hand: HandView | null = null;
  private cues: Cue[] = [];
  private queue: HubSignal[] = [];
  private seenSig = new Set<string>();
  private seenPos = new Map<number, string>();
  private primed = false;
  private runId: number | null = null;
  private n = 0;
  private gap = 1.2;
  private latest: HubData | null = null;
  private score: Record<MemberId, { right: number; n: number }> = Object.fromEntries(MEMBER_IDS.map((id) => [id, { right: 0, n: 0 }])) as Record<MemberId, { right: number; n: number }>;
  private scoreDirty = true;
  private scoreCd = 0;
  private settleQ: BotPosition[] = [];
  private settleCd = 6;
  /** Each played signal's RULE bets (before reading the table), by signal
   *  id: a settlement of a position opened from it is scored on what each
   *  persona's own rule said about the signal as it was presented. */
  private ruleVotes = new Map<number, Record<MemberId, boolean>>();
  mood: Speaker = 'quant';
  paydayT = 0;
  /** Who plays: the personas, or the user's real forecasters (seats.ts). */
  mode: 'personas' | 'agents' = 'personas';
  private seats: Seat[] = [];
  private agenda: { ticker: string; title: string | null }[] = [];
  /** Words each real seat may be shown with (its label), by member. */
  seatOf(id: MemberId): Seat | undefined {
    return this.seats.find((x) => x.member === id);
  }
  /** The market on the felt right now (agents mode). */
  get currentTicker(): string | null {
    return this.hand?.agent?.ticker ?? null;
  }
  /** The latest AI analysis per ticker, from the activity bus. The Quant
   *  quotes it when the table plays that exact market; with none, the
   *  Quant argues in character and cites nothing. */
  private ai = new Map<string, { ev: Extract<ActivityEvent, { kind: 'aiAnalysis' }>; at: number }>();

  constructor(private host: TableHost) {}

  // ── who sits ──────────────────────────────────────────────────────────────

  /** Seat the real forecasters (agents mode) or the personas (null). A
   *  change of players ends the hand on the felt: a hand dealt to one table
   *  is never finished by the other. */
  setSeats(seats: Seat[] | null): void {
    const mode = seats ? 'agents' : 'personas';
    const changed = mode !== this.mode
      || (seats && seats.map((x) => x.f.who.seatId).join() !== this.seats.map((x) => x.f.who.seatId).join());
    this.seats = seats ?? [];
    if (!changed) return;
    this.mode = mode;
    this.abort();
    if (mode === 'personas') {
      this.agenda = [];
      this.scoreDirty = true; // the personas' own leaderboard goes back up
    }
  }

  /** Put a market the real forecasters called on the table (deduped). */
  noteAgenda(ticker: string, title: string | null = null): void {
    if (this.agenda.some((a) => a.ticker === ticker)) return;
    if (this.hand?.agent?.ticker === ticker && this.hand.phase !== 'out') return;
    this.agenda.push({ ticker, title });
    if (this.agenda.length > 4) this.agenda.splice(0, this.agenda.length - 4);
  }

  private abort(): void {
    this.hand = null;
    this.cues = [];
    this.gap = 0.8;
    for (let i = 0; i < MEMBER_IDS.length; i++) this.host.room?.setBet(i, null);
    for (const id of MEMBER_IDS) this.host.members?.[id]?.set('idle');
  }

  // ── data in ───────────────────────────────────────────────────────────────

  noteAnalysis(ev: Extract<ActivityEvent, { kind: 'aiAnalysis' }>, at: number): void {
    this.ai.delete(ev.conditionId);
    this.ai.set(ev.conditionId, { ev, at });
    if (this.ai.size > 50) this.ai.delete(this.ai.keys().next().value as string);
  }

  /** "Claude, 12m ago: fair 58¢ · cheap" for this ticker, or null. A read
   *  older than a day is stale enough to leave out. */
  aiCite(ticker: string, now = Date.now()): string | null {
    const a = this.ai.get(ticker);
    if (!a || now - a.at > 24 * 3600_000) return null;
    const mins = Math.max(0, Math.round((now - a.at) / 60_000));
    const when = mins < 1 ? 'just now' : mins < 60 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`;
    // Six providers now: anything not named is not "Claude".
    const who = aiName(a.ev.provider);
    const fair = a.ev.fairCents !== null ? `fair ${Math.round(a.ev.fairCents)}¢` : 'no fair value';
    return `${who}, ${when}: ${fair} · ${a.ev.verdict}`;
  }

  ingest(d: HubData): void {
    this.latest = d;
    const runId = d.account?.sessionRunId ?? 0;
    if (this.primed && this.runId !== null && runId !== this.runId) {
      // a restarted session is a fresh record
      this.seenSig.clear();
      this.seenPos.clear();
      this.primed = false;
      for (const id of MEMBER_IDS) this.score[id] = { right: 0, n: 0 };
      this.scoreDirty = true;
    }
    this.runId = runId;
    for (const s of d.signals.slice(0, 60)) {
      const k = `${s.source}:${s.id}`;
      if (this.seenSig.has(k)) continue;
      this.seenSig.add(k);
      // Before the first hand, take the freshest signal on screen rather
      // than making the opening shot wait for a new one.
      if (!this.primed && this.queue.length) continue;
      this.queue.push(s);
    }
    if (this.queue.length > 4) this.queue.length = 4;
    if (this.seenSig.size > 4000) this.seenSig = new Set(d.signals.map((x) => `${x.source}:${x.id}`));

    for (const p of d.positions) {
      const sig = p.resolved ? `r:${p.outcomeCorrect}` : 'o';
      const prev = this.seenPos.get(p.id);
      this.seenPos.set(p.id, sig);
      if (!this.primed || prev === sig || !p.resolved || p.outcomeCorrect === null) continue;
      // The personas' record keeps counting while agents sit; the showdown
      // SCENE (gloats and sulks) is persona theatre and plays only for them.
      this.settle(p, this.mode === 'personas');
    }
    if (this.seenPos.size > 6000) {
      const keep = new Set(d.positions.map((p) => p.id));
      for (const id of this.seenPos.keys()) if (!keep.has(id)) this.seenPos.delete(id);
    }
    this.primed = true;
  }

  private features(x: { ticker: string; title: string; category: string; direction: 'yes' | 'no'; price: number | null; edge: number | null; conf: number | null }): Features {
    const a = this.latest?.account;
    const cap = this.latest?.config?.crypto15mMaxConcurrent;
    return {
      subj: subject(x.ticker, x.title),
      title: x.title,
      dir: x.direction === 'yes' ? 'YES' : 'NO',
      // Prices are shown in cents 1..99; anything else is "absent", never 0¢.
      price: x.price !== null && x.price >= 1 && x.price <= 99 ? Math.round(x.price) : null,
      edge: x.edge,
      conf: x.conf,
      cat: x.category,
      open: typeof a?.openCount === 'number' ? a.openCount : null,
      cap: typeof cap === 'number' && cap > 0 ? cap : null,
    };
  }

  private sigFeatures(s: HubSignal): Features {
    return this.features({
      ticker: s.ticker, title: s.title, category: s.category, direction: s.direction,
      price: s.priceCents, edge: s.edgePts ?? null, conf: s.confidence ?? null,
    });
  }

  private posFeatures(p: BotPosition): Features {
    return this.features({
      ticker: p.ticker, title: p.title, category: p.category, direction: p.direction,
      price: p.avgFillPriceCents ?? p.limitPriceCents, edge: p.edgePts ?? null, conf: p.confidence ?? null,
    });
  }

  /** Score every player on every settled trade: did their rule call it? The
   *  record covers all settlements, not just the staged ones, so the
   *  leaderboard is the real hit rate of each bias on this session's trades.
   *  Rule bets only: scoring the table-read bets would credit the
   *  persuadable four with the bot's own decision. */
  private settle(p: BotPosition, stage = true): void {
    const won = p.outcomeCorrect === 1;
    const votes = this.ruleVotes.get(p.signalId) ?? this.rules(this.posFeatures(p));
    for (const id of MEMBER_IDS) {
      const s = this.score[id];
      s.n++;
      if (votes[id] === won) s.right++;
    }
    this.scoreDirty = true;
    if (!stage) return;
    this.settleQ.push(p);
    if (this.settleQ.length > 6) {
      this.settleQ.sort((a, b) => Math.abs(b.pnlUsd ?? 0) - Math.abs(a.pnlUsd ?? 0));
      this.settleQ.length = 6;
    }
  }

  private rules(f: Features): Record<MemberId, boolean> {
    return Object.fromEntries(MEMBER_IDS.map((id) => [id, PERSONAS[id].votes(f)])) as Record<MemberId, boolean>;
  }

  scoreRows(): ScoreRow[] {
    return MEMBER_IDS.map((id) => ({ id, right: this.score[id].right, n: this.score[id].n }));
  }

  // ── per frame ─────────────────────────────────────────────────────────────

  update(dt: number): void {
    this.paydayT = Math.max(0, this.paydayT - dt);
    this.scoreCd -= dt;
    if (this.scoreDirty && this.scoreCd <= 0 && this.mode === 'personas') {
      this.scoreCd = 0.5;
      this.scoreDirty = false;
      this.host.room.setScore(personaScoreView(this.scoreRows()));
    }

    const d = this.hand;
    if (!d) {
      this.gap -= dt;
      if (this.gap <= 0) {
        if (this.mode === 'agents') {
          if (this.agenda.length) this.beginAgents(this.agenda.shift()!);
        } else if (this.queue.length) {
          this.begin(this.queue.shift()!);
        }
      }
    } else {
      d.t += dt;
      for (const c of this.cues) {
        if (!c.done && d.t >= c.at) { c.done = true; c.run(); }
      }
      if (d.stampT !== null) d.stampT += dt;
      if (d.phase === 'out') d.outT = Math.min(1, d.outT + dt / 0.7);
      if (d.t >= (d.doneAt ?? T_DONE)) {
        this.hand = null;
        this.gap = 0.6;
        for (let i = 0; i < MEMBER_IDS.length; i++) this.host.room.setBet(i, null);
      }
    }

    // showdowns play in the gaps, never over the bets or the reveal
    this.settleCd -= dt;
    const quiet = !d || d.phase === 'present' || d.phase === 'out' || d.phase === 'idle';
    if (this.settleCd <= 0 && quiet && this.settleQ.length && this.mode === 'personas') {
      this.settleCd = 5.5;
      this.react(this.settleQ.shift()!);
    }
  }

  private pose(id: MemberId, p: Pose, dur: number): void {
    const m = this.host.members[id];
    m.set(p);
    const until = (this.hand?.t ?? 0) + dur;
    const d = this.hand;
    if (d) this.cues.push({ at: until, done: false, run: () => { if (m.pose === p) m.set('idle'); } });
    else window.setTimeout(() => { if (m.pose === p) m.set('idle'); }, dur * 1000);
  }

  private speak(who: Speaker, text: string, dur = 1.8, force = false): void {
    if (this.host.say(who, text, { force, life: 2.9 })) {
      this.host.speaking(who);
      if (who !== 'dealer') {
        this.host.members[who].talking = dur;
        this.mood = who;
        this.host.room.setMood(PERSONAS[who].color);
      }
    }
  }

  // ── a hand ────────────────────────────────────────────────────────────────

  private begin(sig: HubSignal): void {
    const f = this.sigFeatures(sig);
    const rule = this.rules(f);
    this.ruleVotes.set(sig.id, rule);
    if (this.ruleVotes.size > 300) this.ruleVotes.delete(this.ruleVotes.keys().next().value as number);
    // Overrules stay as a rare beat; the rest of the time the table is read.
    const votes = Math.random() < OVERRULE_RATE ? { ...rule } : readTheRoom(rule, !!sig.traded);
    const yes = MEMBER_IDS.filter((id) => votes[id]).length;
    this.n++;
    const d: HandView = {
      n: this.n, phase: 'present', t: 0, sig, f, votes, yes, no: MEMBER_IDS.length - yes,
      shownVotes: 0, approved: null, stampText: '', stampSub: '', stampT: null, outT: 0,
    };
    this.hand = d;
    this.cues = [];
    const B = this.host.room;
    B.feltPulse = 1;
    B.feltColor.set('#FFB547');
    B.feed(`#${d.n} ${f.subj}`);
    if (Math.random() < 0.45) this.at(0.3, () => this.speak('dealer', line(PERSONAS.dealer.pro(f)), 1.4));

    // ── table talk: who speaks, in what order ─────────────────────────────
    const pro = MEMBER_IDS.filter((id) => votes[id]);
    const con = MEMBER_IDS.filter((id) => !votes[id]);
    let opener: MemberId;
    if (votes.greed && Math.random() < 0.5) opener = 'greed';
    else if ((f.price ?? 0) >= 70 && !votes.panic) opener = 'panic';
    else opener = pick(MEMBER_IDS.filter((id) => id !== 'quant'));
    const order: MemberId[] = [opener];
    const other = (side: MemberId[]) => side.filter((id) => !order.includes(id) && id !== 'quant');
    const oppSide = votes[opener] ? con : pro;
    const reply = other(oppSide);
    if (reply.length) order.push(pick(reply));
    order.push('quant');
    const nearCap = f.open !== null && f.cap !== null && f.open / f.cap >= 0.85;
    if (!order.includes('risk') && (nearCap || (order.includes('greed') && Math.random() < 0.6))) order.push('risk');
    else {
      const rest = other(MEMBER_IDS);
      if (rest.length && Math.random() < 0.7) order.push(pick(rest));
    }
    let at = T_ARGUE;
    order.slice(0, 4).forEach((id) => {
      const mine = votes[id];
      const p = PERSONAS[id];
      const cite = id === 'quant' ? this.aiCite(sig.ticker) : null;
      const text = cite ?? line(mine ? p.pro(f) : p.con(f), mine ? 'yes.' : 'no.');
      this.at(at, () => {
        d.phase = 'argue';
        this.speak(id, text, 1.8);
        // body language that matches the line
        if (id === 'greed' && mine) { this.pose('greed', 'bang', 1.2); this.at(d.t + 0.35, () => { B.tableShake.v = 1; }); }
        else if (id === 'panic' && !mine) this.pose('panic', 'cower', 1.9);
        else if (id === 'risk' && !mine) this.pose('risk', 'whistle', 1.4);
        else if (id === 'doubt') this.pose('doubt', 'nod', 1.3);
        else this.pose(id, 'talk', 1.6);
        // others react to a shout
        if (id === 'greed' && mine && !votes.panic) this.at(d.t + 0.5, () => this.pose('panic', 'cower', 1.2));
      });
      // overlap like a real argument: the next voice cuts in before this
      // bubble is done
      at += 0.95 + Math.random() * 0.35;
    });
    if (order.length >= 4 && Math.random() < 0.4) {
      this.at(at - 0.1, () => { this.speak('dealer', 'ORDER AT THE TABLE!', 1, true); B.dealerTap(2); });
    }

    // ── bets: chips pushed onto YES or NO, one by one ─────────────────────
    this.at(T_VOTE, () => {
      d.phase = 'vote';
      this.host.speaking(null);
    });
    MEMBER_IDS.forEach((id, i) => {
      this.at(T_VOTE + 0.15 + i * 0.16, () => {
        this.host.members[id].set('vote');
        B.setBet(i, votes[id]);
        d.shownVotes++;
      });
    });

    // ── showdown ──────────────────────────────────────────────────────────
    this.at(T_VERDICT, () => this.verdict(d));
    this.at(T_OUT, () => {
      d.phase = 'out';
      for (const id of MEMBER_IDS) if (this.host.members[id].pose === 'vote') this.host.members[id].set('idle');
      B.feltColor.set('#FFB547');
    });
  }

  // ── an agents-mode hand: real numbers only ───────────────────────────────

  private beginAgents(item: { ticker: string; title: string | null }): void {
    const { ticker } = item;
    const seated = this.seats.filter((x) => seatValue(x, ticker) !== null);
    if (!seated.length) return; // nobody called it any more: nothing to stage
    const title = item.title ?? ticker;
    const f = this.features({
      ticker, title, category: '', direction: 'yes', price: null, edge: null, conf: null,
    });
    const votesTri = Object.fromEntries(MEMBER_IDS.map((id) => [id, seatVote(this.seatOf(id), ticker)])) as Record<MemberId, boolean | null>;
    const votes = Object.fromEntries(MEMBER_IDS.map((id) => [id, votesTri[id] === true])) as Record<MemberId, boolean>;
    const yes = MEMBER_IDS.filter((id) => votesTri[id] === true).length;
    const no = MEMBER_IDS.filter((id) => votesTri[id] === false).length;
    const vals = seated.map((x) => seatValue(x, ticker) as number);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const mids = seated.map((x) => x.f.calls.get(ticker)?.mid ?? null).filter((m): m is number => m !== null);
    this.n++;
    const d: HandView = {
      n: this.n, phase: 'present', t: 0, sig: null, f, votes, yes, no,
      shownVotes: 0, approved: null, stampText: '', stampSub: '', stampT: null, outT: 0,
      agent: {
        ticker,
        kicker: `${subject(ticker, title)}  ·  ${seated.length} FORECAST${seated.length === 1 ? '' : 'S'}`,
        dirLine: lo === hi ? `fair ${cents(lo)}` : `fair ${cents(lo)}–${cents(hi)}`,
        // the mids the calls were argued against, never an invented "now"
        facts: mids.length ? `mid was ${Math.min(...mids) === Math.max(...mids) ? cents(mids[0]) : `${cents(Math.min(...mids))}–${cents(Math.max(...mids))}`}` : 'no two-sided quote',
        stampColor: '#FFB547',
      },
    };
    this.hand = d;
    this.cues = [];
    const B = this.host.room;
    B.feltPulse = 1;
    B.feltColor.set('#FFB547');
    B.feed(`#${d.n} ${subject(ticker, title)}`);

    // each seat with a call speaks it, newest call first
    const order = [...seated].sort((a, b) => (b.f.calls.get(ticker)?.at ?? 0) - (a.f.calls.get(ticker)?.at ?? 0));
    let at = T_ARGUE;
    for (const x of order.slice(0, 5)) {
      const text = seatLine(x, ticker);
      if (!text) continue;
      this.at(at, () => {
        d.phase = 'argue';
        this.speak(x.member, text, 1.8);
        this.pose(x.member, 'talk', 1.6);
      });
      at += 1.05;
    }

    const voteAt = Math.max(T_VOTE, at + 0.4);
    this.at(voteAt, () => {
      d.phase = 'vote';
      this.host.speaking(null);
    });
    MEMBER_IDS.forEach((id, i) => {
      if (votesTri[id] === null) return; // no call, or no mid: no bet
      this.at(voteAt + 0.15 + i * 0.16, () => {
        this.host.members[id].set('vote');
        B.setBet(i, votesTri[id]);
        d.shownVotes++;
      });
    });

    const verdictAt = voteAt + 2.0;
    this.at(verdictAt, () => {
      d.phase = 'verdict';
      d.stampT = 0;
      // What was said, summarised — not a decision. YES/NO "cheap" is the
      // calls against their own mids; with no mids there is no side.
      d.stampText = yes && !no ? 'YES CHEAP' : no && !yes ? 'NO CHEAP' : yes && no ? 'SPLIT' : 'NO VOTE';
      d.stampSub = `${seated.length} call${seated.length === 1 ? '' : 's'} · ${d.agent!.dirLine}`;
      B.dealerTap(1);
      B.feltPulse = 1;
      this.speak('dealer', 'CARDS ON THE TABLE.', 1.2, true);
      this.host.log(`♠ #${d.n} ${subject(ticker, title)} · ${d.agent!.dirLine}${yes + no ? ` · ${yes}–${no}` : ''}`, 'gold');
    });
    this.at(verdictAt + 3.0, () => {
      d.phase = 'out';
      for (const id of MEMBER_IDS) if (this.host.members[id].pose === 'vote') this.host.members[id].set('idle');
    });
    // many seats speaking run longer than a persona hand
    d.doneAt = verdictAt + 3.8;
  }

  private at(t: number, run: () => void): void {
    this.cues.push({ at: t, done: false, run });
  }

  /** The showdown: the dealer turns over what the bot really did. */
  private verdict(d: HandView): void {
    const B = this.host.room;
    const sig = d.sig;
    if (!sig) return;
    // the bot's actual decision, re-read now (the flag can flip after the
    // signal first appears)
    const now = this.latest?.signals.find((s) => s.id === sig.id && s.source === sig.source) ?? sig;
    const approved = now.traded;
    d.approved = approved;
    d.phase = 'verdict';
    d.stampT = 0;
    const majority = d.yes > d.no ? true : d.yes < d.no ? false : null;
    const f = d.f;
    const px = f.price !== null ? ` @ ${f.price}¢` : '';
    if (approved) {
      // a matching live position supplies the real size; without one there
      // is no size to show, so none is invented
      const pos = this.latest?.positions.find((p) => !p.resolved && p.ticker === sig.ticker && p.direction === sig.direction && p.filledContracts > 0);
      d.stampText = 'BOT CALLS';
      d.stampSub = pos ? `BUY ${pos.filledContracts}× ${f.dir}${px}` : `BUY ${f.dir}${px}`;
    } else {
      d.stampText = 'BOT FOLDS';
      d.stampSub = `pass on ${f.subj}`;
    }
    let dealerLine = approved ? 'THE BOT CALLS.' : 'THE BOT FOLDS.';
    if (majority === null) dealerLine = 'split table. the bot decides.';
    else if (majority !== approved) {
      dealerLine = 'OVERRULED!';
      d.stampSub = `${d.stampSub} · table bet ${d.yes}–${d.no}`;
    }
    B.dealerTap(majority !== null && majority !== approved ? 3 : 1);
    B.tableShake.v = 1;
    B.feltPulse = 1;
    B.feltColor.set(approved ? '#4ADE80' : '#F87171');
    this.speak('dealer', dealerLine, 1.2, true);
    this.host.log(`♠ #${d.n} ${approved ? 'BOT CALLS' : 'BOT FOLDS'} ${d.yes}–${d.no} · ${f.subj}`, approved ? 'win' : 'loss');

    // the losing side sulks; the winning side's loudest gloats
    const losers = MEMBER_IDS.filter((id) => d.votes[id] !== approved);
    const winners = MEMBER_IDS.filter((id) => d.votes[id] === approved);
    this.at(T_VERDICT + 0.9, () => {
      const sore = losers.includes('greed') ? 'greed' : losers.includes('panic') ? 'panic' : losers[0];
      if (sore) {
        this.speak(sore, pick(PERSONAS[sore].sore), 1.4);
        this.pose(sore, sore === 'panic' ? 'cower' : 'talk', 1.4);
      }
      const happy = winners.includes('greed') ? 'greed' : winners.includes('fomo') ? 'fomo' : null;
      if (happy && approved) this.pose(happy, 'cheer', 1.4);
    });
  }

  // ── settlements: the showdown ─────────────────────────────────────────────

  private react(p: BotPosition): void {
    const won = p.outcomeCorrect === 1;
    const votes = this.ruleVotes.get(p.signalId) ?? this.rules(this.posFeatures(p));
    const pnl = money(p.pnlUsd ?? 0);
    const subj = subject(p.ticker, p.title);
    if (won) {
      const gloat: MemberId = votes.greed ? 'greed' : votes.fomo ? 'fomo' : MEMBER_IDS.find((id) => votes[id]) ?? 'quant';
      this.speak(gloat, pick(PERSONAS[gloat].win(pnl)), 1.6, true);
      this.pose(gloat, 'gloat', 1.6);
      const sulk = MEMBER_IDS.find((id) => !votes[id] && id !== gloat);
      if (sulk && Math.random() < 0.5) window.setTimeout(() => this.speak(sulk, pick(PERSONAS[sulk].sore), 1.2), 700);
      this.host.coins(Math.max(3, Math.min(18, Math.round((p.pnlUsd ?? 0) / 2))));
      this.host.showdown?.(true, `+${pnl}`, subj);
      this.host.log(`💰 settled +${pnl} · ${subj}`, 'win');
    } else {
      if (!votes.panic) {
        this.speak('panic', pick(PERSONAS.panic.loss(pnl)), 1.6, true);
        this.pose('panic', 'cower', 1.8);
      }
      if (!votes.doubt) {
        window.setTimeout(() => {
          this.speak('doubt', pick(PERSONAS.doubt.loss(pnl)), 1.2);
          this.pose('doubt', 'nod', 1.4);
        }, 650);
      }
      if (votes.panic && votes.doubt) this.speak('risk', pick(PERSONAS.risk.loss(pnl)), 1.4, true);
      this.host.showdown?.(false, `−${pnl}`, subj);
      this.host.log(`🚨 settled −${pnl} · ${subj}`, 'loss');
    }
  }

  /** PAYDAY (a real big win, see payday.ts): the whole table loses it. */
  payday(): void {
    this.paydayT = 5.5;
    const M = this.host.members;
    if (this.mode === 'agents') {
      // The bot's win, celebrated by the table — but no persona cheer is put
      // in a real agent's mouth: only the dealer speaks.
      for (const id of MEMBER_IDS) if (this.seatOf(id)) M[id].set('cheer');
      window.setTimeout(() => { for (const id of MEMBER_IDS) if (M[id].pose === 'cheer') M[id].set('idle'); }, 4000);
      this.speak('dealer', pick(PERSONAS.dealer.payday), 2, true);
      this.host.room.dealerTap(6);
      this.host.coins(160);
      return;
    }
    M.greed.climbTable(5.5);
    for (const id of MEMBER_IDS) M[id].set(id === 'panic' ? 'cower' : 'wild');
    window.setTimeout(() => { for (const id of MEMBER_IDS) if (M[id].pose === 'wild' || M[id].pose === 'cower') M[id].set('idle'); }, 5500);
    this.speak('greed', pick(PERSONAS.greed.payday), 2.4, true);
    const others: MemberId[] = ['fomo', 'panic', 'doubt', 'quant', 'risk'];
    others.forEach((id, i) => window.setTimeout(() => this.speak(id, pick(PERSONAS[id].payday), 2), 450 + i * 520));
    window.setTimeout(() => this.speak('dealer', pick(PERSONAS.dealer.payday), 2, true), 3100);
    this.host.room.dealerTap(6);
    this.host.room.tableShake.v = 1.5;
    this.host.coins(160);
  }
}
