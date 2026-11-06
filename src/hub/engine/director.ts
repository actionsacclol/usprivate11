import * as THREE from 'three';
import type { AccountSnapshot, BotPosition } from '@shared/types';
import type { HubSignal } from '../data';
import type { HubAutopilot } from '../data';
import type { CasinoFloor } from './floor';
import type { CoinPile, Flights, ItemKit, Sparks } from './items';
import type { Worker, Step } from './crew';
import type { Tone } from './overlay';
import type { RoomId } from './layout';
import type { ActivityRecord, AgentCallActivity } from '../../state/activity';
import { configuredAgents, hubAgentIdentity, tagLabel, type AgentIdentity } from '../../utils/agents';

// The director turns REAL events into jobs for the house staff. Every area
// of the casino floor is fed by something that actually happened (the room
// ids are the app's concepts; floor.ts dresses them):
//
//   research   the Sportsbook: engine orders (data), AI analyses (activity bus)
//   forge      the Card Room: script saves / validations / enables, applied
//              presets and profiles, agent script and settings edits (bus)
//   backtest   the Practice Table: a backtest that finished anywhere in the
//              app, with its real equity curve and numbers (bus); between
//              runs a static readout of the last SAVED results (library.ts),
//              never a re-"run"
//   optimizer  the Roulette wheel: a Coin Optimizer run that finished (bus);
//              a scheduled runner flipping strategy at the top of the UTC
//              hour (config + clock)
//   desk       the High-Limit Table: bot and script orders (positions),
//              manual terminal orders and AI agent orders (bus), each
//              labelled with its source; real ones leave for POLYMARKET
//   vault      the Cashier Cage: settlements (positions), standing rules
//              firing and daily stops (bus)
//   bridge     the VIP lounge and its pit boss: agent approvals, Autopilot
//              runs (bus + the cheap autopilot status read), real P&L
//              milestones
//
// and, apart from the house staff, the USER'S OWN AGENTS: every connected
// client (Claude Code, Cursor, Codex, Autopilot, an HTTP client) is a named
// guest that walks to the table of each real tool call it makes and quotes
// it (the `mcp:toolCall` push via the bus; rooms in jobs.ts). An agent that
// makes no call waits on its spot in the lounge.
//
// and with nothing happening an area idles: the staff wander and work their
// stations (crew.ts) and say nothing. An earlier build ran the backtest room
// and the optimizer on 12–24 s timers, re-"running" saved results and
// "reviewing" saved schedules, and forged every signal into a fake strategy
// chip; the scene looked busy whether or not anything had happened. Those
// timers are gone, and `test/hubActivity.test.ts` pins that with zero events
// the director produces no work at all.
//
// A busy bot can open and settle positions faster than a dealer can walk
// them across the floor, so each stream goes through a small capped queue:
// the surplus is dropped from the SHOW, never from the counters.

import { hudNumbers, type HubData } from '../data';
import { hasEdge, type SavedBacktest } from '../library';
import { subject } from '../text';
import {
  activeSlots, agentCallBeat, beatFor, flipLine, positionSource, scheduleFlips, scheduledRunners, signalHandoff,
  type AgentBeat, type Beat, type SlotNow,
} from './jobs';
export type { HubData };

export interface Stage {
  floor: CasinoFloor;
  kit: ItemKit;
  flights: Flights;
  sparks: Sparks;
  pile: CoinPile;
  crew: Worker[];
  root: THREE.Object3D;
  say(who: Worker | 'captain', text: string, tone: Tone, opts?: { big?: boolean; force?: boolean; life?: number }): boolean;
  log(text: string, tone: Tone): void;
  activity(room: RoomId, amount?: number): void;
  /** The user's own agent as a guest, brought onto the floor on first
   *  sight; null when the lounge has no spot free (CasinoEngine). Optional: a
   *  stage without one stages no agents. */
  agent?(who: AgentIdentity): Worker | null;
}

export interface DirectorOptions {
  /** The wall clock, for the scheduled-runner hour flip. Injected for tests. */
  now?: () => number;
}

interface Chip { obj: THREE.Object3D }
interface Bag { obj: THREE.Object3D; pnl: number; title: string }

function money(v: number): string {
  return `$${Math.abs(v).toFixed(2)}`;
}

function rand(a: number, b: number): number {
  return a + Math.random() * (b - a);
}

function pick<T>(xs: T[]): T {
  return xs[Math.floor(Math.random() * xs.length)];
}

function ago(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s ago` : `${Math.round(s / 60)}m ago`;
}

/** A capped queue: when it overflows, the OLDEST beat leaves the show. */
function push<T>(q: T[], x: T, cap: number): void {
  q.push(x);
  if (q.length > cap) q.splice(0, q.length - cap);
}

const ROOM_CAP = 4;
/** Beats queued per agent: a looping agent's surplus leaves the show (the
 *  feed still counts it), never the queue's memory. */
const AGENT_CAP = 3;
/** While an agent has called within this window, its own crew member is
 *  quoting its orders, so the desk's copy of the same order stays quiet. */
const AGENT_STREAM_MS = 30_000;

export class Director {
  // ── counters (every event, not just the animated ones) ──────────────────
  signalsSeen = 0;
  ordersSeen = 0;
  winsSeen = 0;
  lossesSeen = 0;
  /** Strategy events staged since the hub opened (catch-up included). */
  forgeEvents = 0;

  // ── latest account numbers for the HUD ──────────────────────────────────
  account: AccountSnapshot | null = null;
  autopilot: HubAutopilot | null = null;

  private seenSig = new Set<string>();
  private seenPos = new Map<number, string>();
  private primed = false;
  private runId: number | null = null;

  private sigQ: HubSignal[] = [];
  private orderQ: BotPosition[] = [];
  private winQ: BotPosition[] = [];
  private lossQ: BotPosition[] = [];
  private pings = 0;

  private researchQ: Beat[] = [];
  private forgeQ: Beat[] = [];
  private backtestQ: Beat[] = [];
  private optQ: Beat[] = [];
  private deskQ: Beat[] = [];
  private riskQ: Beat[] = [];
  private bridgeQ: Beat[] = [];
  private agentQ = new Map<string, { who: AgentIdentity; beats: AgentBeat[] }>();
  private agentStreamAt = -Infinity;
  /** Tool calls the user's agents made since the hub opened (catch-up too). */
  agentCalls = 0;

  private outbox: Chip[] = [];
  private rack: Chip[] = [];
  private airlock: Bag[] = [];
  private cardsMoving = 0;
  private bagsFlying = 0;

  private pingCd = 0;
  private fireCd = 1;
  private inCd = 1.5;
  private alarmCd = 2;
  private bridgeCd = 0;
  private traderTalkCd = 0;
  private lossTalkCd = 0;
  private clockCd = 0;
  private scriptLogCd = 0;
  private pnlMark: number | null = null;

  private library: SavedBacktest[] = [];
  private lastRun: { stamp: string } | null = null;
  private signals: HubSignal[] = [];
  private config: HubData['config'] = null;
  private slotsNow: Map<string, SlotNow> | null = null;
  private slotHour = -1;
  private savedSlotCount = 0;
  private optimizing = false;
  private tableBooked = false;
  private autopilotRunning: boolean | null = null;
  private idleShown = '';
  private now: () => number;

  constructor(private s: Stage, opts: DirectorOptions = {}) {
    this.now = opts.now ?? Date.now;
  }

  /** The backtest room's sign: the last run this session, else the saved
   *  library, else nothing to report. */
  get backtestSign(): string {
    if (this.lastRun) return `last ${this.lastRun.stamp}`;
    if (!this.library.length) return 'no saved runs';
    return `${this.library.filter(hasEdge).length}/${this.library.length} edge`;
  }

  get optimizerSign(): string {
    return this.savedSlotCount ? `${this.savedSlotCount} slots` : 'idle';
  }

  get forgeSign(): string {
    return this.forgeEvents ? `${this.forgeEvents} update${this.forgeEvents === 1 ? '' : 's'}` : 'idle';
  }

  /** Only a real Autopilot state: "running" while a run is, "on" while it is
   *  enabled, the agents on the floor, and the pit boss's name otherwise. */
  get bridgeSign(): string {
    const a = this.autopilot;
    if (a?.running) return `autopilot · step ${a.step}`;
    const n = this.agentQ.size;
    if (n) return `${n} agent${n === 1 ? '' : 's'} on the floor`;
    if (a?.enabled) return 'autopilot on';
    return 'PIT BOSS BLUEY';
  }

  private worker(role: Worker['role'], prefer?: (w: Worker) => boolean): Worker | null {
    const free = this.s.crew.filter((w) => w.role === role && w.free);
    if (!free.length) return null;
    return free.find((w) => prefer?.(w)) ?? free[Math.floor(Math.random() * free.length)];
  }

  // ── data in ───────────────────────────────────────────────────────────────

  ingest(d: HubData): void {
    this.account = d.account;
    this.signals = d.signals;
    this.config = d.config ?? null;
    this.savedSlotCount = scheduledRunners(d.config).reduce((n, r) => n + (r.schedule?.length ?? 0), 0);
    if (d.library) {
      this.library = d.library;
      this.showIdleReadout();
    }
    if (d.autopilot !== undefined) this.ingestAutopilot(d.autopilot ?? null);
    const runId = d.account?.sessionRunId ?? 0;
    if (this.primed && this.runId !== null && runId !== this.runId) {
      // A restarted session is a fresh story: the counters restart, but the
      // staff finishes what's in its hands.
      this.seenSig.clear();
      this.seenPos.clear();
      this.primed = false;
      this.signalsSeen = 0;
      this.ordersSeen = this.winsSeen = this.lossesSeen = 0;
      this.pnlMark = null;
    }
    this.runId = runId;

    for (const sg of d.signals.slice(0, 80)) {
      const key = `${sg.source}:${sg.id}`;
      if (this.seenSig.has(key)) continue;
      this.seenSig.add(key);
      if (!this.primed) continue;
      this.signalsSeen++;
      this.pings++;
      push(this.sigQ, sg, 3);
    }
    if (this.seenSig.size > 4000) this.seenSig = new Set(d.signals.map((x) => `${x.source}:${x.id}`));

    for (const p of d.positions) {
      const sig = p.resolved ? `r:${p.outcomeCorrect}` : 'o';
      const prev = this.seenPos.get(p.id);
      this.seenPos.set(p.id, sig);
      if (!this.primed || prev === sig) continue;
      const live = p.status === 'filled' || p.status === 'partial';
      if (prev === undefined && (live || p.resolved)) {
        this.ordersSeen++;
        push(this.orderQ, p, 5);
      }
      if (p.resolved && prev !== sig) {
        if (p.outcomeCorrect === 1) {
          this.winsSeen++;
          this.winQ.push(p);
          if (this.winQ.length > 6) {
            // keep the juiciest: the biggest win is the one worth carrying
            this.winQ.sort((a, b) => (b.pnlUsd ?? 0) - (a.pnlUsd ?? 0));
            this.winQ.length = 6;
          }
        } else if (p.outcomeCorrect === 0) {
          this.lossesSeen++;
          this.lossQ.push(p);
          if (this.lossQ.length > 2) {
            this.lossQ.sort((a, b) => (a.pnlUsd ?? 0) - (b.pnlUsd ?? 0));
            this.lossQ.length = 2;
          }
        }
      }
    }
    if (this.seenPos.size > 6000) {
      const keep = new Set(d.positions.map((p) => p.id));
      for (const id of this.seenPos.keys()) if (!keep.has(id)) this.seenPos.delete(id);
    }
    this.primed = true;

    const pnl = hudNumbers(d.account).pnl;
    if (pnl !== null) {
      const step = this.milestoneStep(d.account);
      if (this.pnlMark === null) this.pnlMark = Math.floor(pnl / step) * step;
      else if (pnl >= this.pnlMark + step) {
        this.pnlMark = Math.floor(pnl / step) * step;
        this.s.say('captain', `🎰 P&L ${pnl >= 0 ? '+' : '−'}$${Math.abs(this.pnlMark).toFixed(0)}! nice work, everyone`, 'win', { force: true });
        this.s.floor.blueyHop = 1.2;
      } else if (pnl < this.pnlMark - step) {
        this.pnlMark = Math.floor(pnl / step) * step;
      }
    }
  }

  /** The practice table's display between runs: the best saved result, as
   *  a still. (The library has no timestamps, so "best" is the honest pick,
   *  not "newest".) */
  private showIdleReadout(): void {
    if (this.lastRun || this.tableBooked) return;
    let label = 'BACKTEST';
    let sub = 'idle · no saved runs';
    if (this.library.length) {
      const best = [...this.library].sort((a, b) => b.net - a.net)[0];
      label = 'SAVED · BEST';
      sub = `${best.name.slice(0, 14)} ${best.net >= 0 ? '+' : '−'}${Math.abs(best.net).toFixed(1)}¢${hasEdge(best) ? ' ✓' : ''}`;
    }
    const key = `${label}|${sub}`;
    if (key === this.idleShown) return;
    this.idleShown = key;
    this.s.floor.setPracticeIdle(label, sub);
  }

  private ingestAutopilot(a: HubAutopilot | null): void {
    this.autopilot = a;
    const running = a ? !!a.running : null;
    // A run STARTING is news; one already running when the hub opened is
    // the sign's job, not a bubble's.
    if (running && this.autopilotRunning === false) {
      const head = a?.runs?.[0];
      const trig = head && head.finishedAt === null ? head.trigger : null;
      const t = `🤖 Autopilot run started${trig ? ` (${trig})` : ''}`;
      push(this.bridgeQ, { room: 'bridge', text: t, log: t, tone: 'gold' }, ROOM_CAP);
    }
    this.autopilotRunning = running;
  }

  /** One real event from the activity bus. `replay` = it happened before
   *  the hub opened (catch-up), so the feed says how long ago. */
  activity(rec: ActivityRecord, replay = false): void {
    if (rec.ev.kind === 'agentCall') {
      this.agentCall(rec.ev, rec.at, replay);
      return;
    }
    const beat = beatFor(rec.ev);
    if (!beat) return;
    if (replay) beat.log = `${beat.log} · ${ago(this.now() - rec.at)}`;
    switch (beat.room) {
      case 'research': push(this.researchQ, beat, ROOM_CAP); break;
      case 'forge': this.forgeEvents++; push(this.forgeQ, beat, ROOM_CAP); break;
      case 'backtest': push(this.backtestQ, beat, ROOM_CAP); break;
      case 'optimizer': push(this.optQ, beat, ROOM_CAP); break;
      case 'desk': push(this.deskQ, beat, ROOM_CAP); break;
      case 'vault': push(this.riskQ, beat, ROOM_CAP); break;
      case 'bridge': push(this.bridgeQ, beat, ROOM_CAP); break;
    }
  }

  /** One real tool call by one of the user's agents: its guest walks to
   *  that table and quotes the call. */
  private agentCall(ev: AgentCallActivity, at: number, replay: boolean): void {
    // The user's named agent as itself (its config name, emoji and colour,
    // keyed by the agent id its token resolved to); an older event without
    // an id keeps the client/model identity.
    const agents = configuredAgents(this.config);
    let who = hubAgentIdentity(ev, agents);
    const prev = this.agentQ.get(who.id);
    // the model may be named only on an earlier call: keep it on the tag
    if (!who.model && prev?.who.model) who = hubAgentIdentity({ ...ev, model: prev.who.model }, agents);
    const beat = agentCallBeat(ev, tagLabel(who));
    if (!beat) return;
    if (ev.call === 'call') {
      this.agentCalls++;
      this.agentStreamAt = this.now();
    }
    if (replay) beat.log = `${beat.log} · ${ago(this.now() - at)}`;
    let q = prev;
    if (!q) {
      q = { who, beats: [] };
      this.agentQ.set(who.id, q);
    }
    // the newest identity: a rename or a new colour shows on the next call
    q.who = who;
    q.beats.push(beat);
    if (q.beats.length > AGENT_CAP) {
      // An agent calls faster than a crew member can walk. What leaves the
      // show first is a routine read; a refusal, an order or a forecast is
      // the story, and is dropped only when nothing else is left to drop.
      const routine = q.beats.findIndex((b) => !b.run && b.room !== 'desk' && !b.text.includes('forecast'));
      q.beats.splice(routine >= 0 ? routine : 0, 1);
    }
  }

  /** A running script's own log() lines. They are the script narrating its
   *  decisions, not orders (its orders arrive as positions), so a card-room
   *  dealer reads the newest one out at most every few seconds and the rest
   *  are dropped from the show. */
  scriptLog(name: string | null, lines: string[]): void {
    const last = [...lines].reverse().find((l) => typeof l === 'string' && l.trim());
    if (!last || this.scriptLogCd > 0) return;
    this.scriptLogCd = 6;
    const raw = `📜 ${name ? `${name.slice(0, 16)}: ` : ''}${last.replace(/\s+/g, ' ').trim()}`;
    const text = raw.length > 56 ? `${raw.slice(0, 55)}…` : raw;
    this.s.log(text, 'forge');
    const w = this.worker('smith');
    if (w) this.s.say(w, text, 'forge');
    this.s.activity('forge', 0.5);
  }

  private milestoneStep(a: AccountSnapshot | null): number {
    const bank = a?.startBankrollUsd ?? 1000;
    return Math.max(25, Math.round((bank * 0.05) / 25) * 25);
  }

  // ── per frame ─────────────────────────────────────────────────────────────

  update(dt: number): void {
    this.pingCd -= dt;
    this.fireCd -= dt;
    this.inCd -= dt;
    this.alarmCd -= dt;
    this.bridgeCd -= dt;
    this.traderTalkCd -= dt;
    this.lossTalkCd -= dt;
    this.clockCd -= dt;
    this.scriptLogCd -= dt;

    if (this.pings > 0 && this.pingCd <= 0) {
      this.pingCd = 0.35;
      this.pings = 0;
      this.s.floor.oddsPing(Math.random() < 0.5 ? '#3B82F6' : '#38BDF8');
      this.s.activity('research', 0.4);
    }
    // The clock is read once a second; it only ever REPORTS a flip the
    // backend makes at the same moment, it never schedules work.
    if (this.clockCd <= 0) {
      this.clockCd = 1;
      this.checkSchedule();
    }
    this.research();
    this.forge();
    this.backtest();
    this.orders();
    this.incoming();
    this.vault();
    this.losses();
    this.risk();
    this.optimizer();
    this.bridge();
    this.agents();
  }

  // ── 1. sportsbook: engine orders, and AI reads ─────────────────────────────────

  private research(): void {
    if (this.researchQ.length) {
      const w = this.worker('researcher');
      if (!w) return;
      const b = this.researchQ.shift()!;
      w.assign([
        { k: 'go', to: w.home },
        { k: 'work', anim: 'type', dur: 0.7 },
        { k: 'do', fn: (wk) => {
          this.s.floor.oddsPing('#3B82F6');
          this.s.say(wk, b.text, b.tone, { force: true, life: 3.4 });
          this.s.log(b.log, b.tone);
          this.s.activity('research', 1);
        } },
        { k: 'work', anim: 'look', dur: 1.4 },
      ]);
      return;
    }
    if (!this.sigQ.length || this.cardsMoving >= 2) return;
    const w = this.worker('researcher');
    if (!w) return;
    const sg = this.sigQ.shift()!;
    this.sigQ.length = Math.min(this.sigQ.length, 1);
    const color = '#38BDF8';
    const subj = subject(sg.ticker, sg.title);
    const dir = sg.direction.toUpperCase();
    const text = `📈 ${subj} ${dir} ${sg.priceCents}¢`;
    this.cardsMoving++;
    w.assign([
      { k: 'go', to: w.home },
      { k: 'work', anim: 'type', dur: 0.6 },
      { k: 'do', fn: (wk) => {
        this.s.floor.oddsPing(color);
        this.s.say(wk, text, 'momo');
        this.s.log(text, 'momo');
        this.s.activity('research', 1);
      } },
      { k: 'work', anim: 'look', dur: 0.7 },
      { k: 'go', to: 'research.printer' },
      { k: 'do', fn: () => this.s.floor.ticketPrint() },
      { k: 'work', anim: 'grab', dur: 0.45 },
      { k: 'do', fn: (wk) => wk.pickUp(this.s.kit.card(color)) },
      // The bot's call on THIS signal, re-read now: the flag can flip a
      // moment after the signal first appears.
      { k: 'work', anim: 'look', dur: 0.8 },
      { k: 'do', fn: (wk) => {
        const now = this.signals.find((x) => x.id === sg.id && x.source === sg.source) ?? sg;
        const h = signalHandoff(now);
        if (!h.traded) {
          wk.drop()?.removeFromParent();
          this.cardsMoving--;
          this.s.floor.ticketPrint();
          this.s.say(wk, h.text, 'neutral');
          return;
        }
        this.s.say(wk, h.text, 'momo');
        wk.assign([
          { k: 'go', to: 'desk.rack' },
          { k: 'work', anim: 'grab', dur: 0.3 },
          { k: 'do', fn: (w2) => {
            const o = w2.drop();
            this.cardsMoving--;
            if (o) this.toRack(o);
            this.s.activity('desk', 0.6);
          } },
        ]);
      } },
    ]);
  }

  private place(o: THREE.Object3D, at: THREE.Vector3): void {
    this.s.root.add(o);
    o.position.copy(at);
    o.rotation.set(0, Math.random() * 0.6 - 0.3, 0);
    o.scale.setScalar(1);
  }

  private toRack(o: THREE.Object3D): void {
    if (this.rack.length >= this.s.floor.railSlots.length) this.rack.shift()?.obj.removeFromParent();
    this.rack.push({ obj: o });
    this.rack.forEach((c, i) => this.place(c.obj, this.s.floor.railSlots[i]));
  }

  // ── 2. card room: real strategy work, dealt out ──────────────────────────

  private forge(): void {
    if (!this.forgeQ.length) return;
    const w = this.worker('smith');
    if (!w) return;
    const b = this.forgeQ.shift()!;
    const ok = b.tone !== 'loss';
    w.assign([
      { k: 'go', to: 'forge.anvil' },
      { k: 'work', anim: 'hammer', dur: ok ? 1.5 : 0.9, onBeat: () => {
        this.s.floor.dealSnap();
        const d = this.s.floor.dealSpot;
        this.s.sparks.burst(d.x, d.y, d.z, ok ? '#FDBA74' : '#9CA3AF', 8, 1.8, 2.0);
        this.s.activity('forge', 0.6);
      } },
      { k: 'do', fn: (wk) => {
        this.s.say(wk, b.text, b.tone, { force: true, life: 3.2 });
        this.s.log(b.log, b.tone);
        this.s.activity('forge', 1);
        if (!ok) return;
        const chip = this.s.kit.chip(b.deploy ? '#2DD4BF' : '#F59E0B');
        const d = this.s.floor.dealSpot;
        this.s.sparks.burst(d.x, d.y + 0.1, d.z, '#FDE68A', 20, 2.6, 2.4);
        wk.pickUp(chip);
        wk.assign(b.deploy ? [
          // enabled / applied: it goes to work at the high-limit table
          { k: 'go', to: 'desk.rack' },
          { k: 'work', anim: 'grab', dur: 0.3 },
          { k: 'do', fn: (w2) => {
            const o = w2.drop();
            if (o) this.toRack(o);
            this.s.activity('desk', 0.6);
          } },
        ] : [
          // saved / validated: it sits finished on the shelf
          { k: 'go', to: 'forge.outbox' },
          { k: 'work', anim: 'grab', dur: 0.3 },
          { k: 'do', fn: (w2) => {
            const o = w2.drop();
            if (!o) return;
            if (this.outbox.length >= this.s.floor.shelfSlots.length) this.outbox.shift()?.obj.removeFromParent();
            this.outbox.push({ obj: o });
            this.outbox.forEach((c, i) => this.place(c.obj, this.s.floor.shelfSlots[i]));
          } },
        ]);
      } },
    ]);
  }

  // ── 3. practice table: a finished run, played back from its real curve ──

  private backtest(): void {
    if (!this.backtestQ.length || this.tableBooked || this.s.floor.practiceRunning) return;
    const w = this.worker('quant');
    if (!w) return;
    const b = this.backtestQ.shift()!;
    this.tableBooked = true;
    const series = b.series && b.series.length > 1 ? b.series : [];
    w.assign([
      { k: 'go', to: 'backtest.table' },
      { k: 'do', fn: () => {
        this.s.floor.startPractice(series, b.holo ?? 'BACKTEST', series.length ? '' : 'no equity curve');
        this.s.activity('backtest', 1);
      } },
      { k: 'work', anim: 'type', dur: 2.5 },
      { k: 'do', fn: (wk) => {
        this.tableBooked = false;
        this.lastRun = { stamp: b.stamp ?? '—' };
        this.s.floor.finishPractice(b.pass ?? null, b.stamp ?? '');
        this.s.say(wk, b.text, b.tone, { force: true, life: 3.6 });
        this.s.log(b.log, b.tone);
        this.s.activity('backtest', 1);
      } },
      { k: 'work', anim: b.pass ? 'cheer' : 'look', dur: 1.2 },
    ]);
  }

  // ── 4. high-limit table: bot, script, manual and agent orders ───────────

  /** A real order leaves the building: a chip stack through the chute,
   *  across the night to the POLYMARKET teller window. */
  private capsule(): void {
    const floor = this.s.floor;
    floor.slideBet();
    const cap = this.s.kit.capsule();
    this.s.flights.launch(cap, floor.betChute, floor.tellerPos, 1.5, 2.2, () => {
      floor.tellerHit();
      const sp = floor.teller.position;
      this.s.sparks.burst(sp.x, sp.y, sp.z, '#93C5FD', 8, 1.6, 0.6);
    });
  }

  private orders(): void {
    if (this.fireCd > 0) return;
    if (this.deskQ.length) {
      this.fireCd = rand(0.9, 1.3);
      const b = this.deskQ.shift()!;
      if (b.capsule) this.capsule();
      if (b.board) this.s.floor.postBet(b.board.text, b.board.color);
      this.s.activity('desk', 0.9);
      // An agent order whose agent is on the floor: the agent quotes it (its
      // own beat); the table posts the board line and sends the bet.
      if (b.agentOrder && this.now() - this.agentStreamAt < AGENT_STREAM_MS) return;
      this.s.log(b.log, b.tone);
      const w = this.worker('trader') ?? pick(this.s.crew.filter((c) => c.role === 'trader'));
      if (w) this.s.say(w, b.text, b.tone, { force: true, life: 3.2 });
      return;
    }
    if (!this.orderQ.length) return;
    this.fireCd = rand(0.55, 0.85);
    const p = this.orderQ.pop()!;
    this.orderQ.length = Math.min(this.orderQ.length, 3);
    this.capsule();
    this.s.activity('desk', 0.7);
    const filled = p.filledContracts > 0;
    const qty = filled ? p.filledContracts : p.targetContracts;
    const price = p.avgFillPriceCents ?? p.limitPriceCents;
    const px = typeof price === 'number' && Number.isFinite(price) ? ` @${Math.round(price)}¢` : '';
    const side = p.action === 'sell' ? 'SELL' : 'BUY';
    const dir = p.direction.toUpperCase();
    const subj = subject(p.ticker, p.title);
    const src = positionSource(p.signalSource);
    this.s.floor.postBet(`${src} ${side} ${qty}× ${dir}${px} ${subj.slice(0, 10)}`, dir === 'YES' ? '#4ADE80' : '#F9A8D4');
    if (this.traderTalkCd <= 0) {
      const w = pick(this.s.crew.filter((c) => c.role === 'trader'));
      if (w) {
        const lines = [
          `${src} ${side} ${qty}× ${dir}${px}`,
          filled ? `${src} filled ${qty}×${px}` : `${src} working${px}`,
          `🎲 ${subj} ${dir} → POLYMARKET`,
        ];
        if (this.s.say(w, pick(lines), 'momo')) this.traderTalkCd = rand(2.8, 4.2);
      }
    }
  }

  // ── 5. wins come home: POLYMARKET → payout window → cashier cage ────────

  private incoming(): void {
    if (this.inCd > 0 || !this.winQ.length) return;
    if (this.airlock.length + this.bagsFlying >= 4) return;
    this.inCd = rand(0.55, 0.9);
    this.winQ.sort((a, b) => (b.pnlUsd ?? 0) - (a.pnlUsd ?? 0));
    const p = this.winQ.shift()!;
    const floor = this.s.floor;
    const bag = this.s.kit.bag();
    this.bagsFlying++;
    this.s.flights.launch(bag, floor.teller.position, floor.payoutPos, 1.4, 2.6, () => {
      this.bagsFlying--;
      floor.payoutBlink();
      this.s.activity('desk', 0.4);
      const slot = floor.payoutSlots[this.airlock.length % floor.payoutSlots.length];
      const b = this.s.kit.bag();
      b.scale.setScalar(0.85);
      this.s.root.add(b);
      b.position.copy(slot);
      this.airlock.push({ obj: b, pnl: p.pnlUsd ?? 0, title: subject(p.ticker, p.title) });
      // Log on arrival, not on deposit: a cashier's walk to the cage takes
      // seconds, and gating the feed on it made a 63%-win session read as a
      // stream of losses (the alarm logs instantly).
      this.s.log(`💰 +${money(p.pnlUsd ?? 0)} · ${subject(p.ticker, p.title)}`, 'win');
    }, true);
  }

  private vault(): void {
    if (!this.airlock.length) return;
    const w = this.worker('keeper');
    if (!w) return;
    const bag = this.airlock.shift()!;
    this.airlock.forEach((b, i) => b.obj.position.copy(this.s.floor.payoutSlots[i]));
    const spot = this.s.crew.filter((c) => c.role === 'keeper').indexOf(w) % 2 ? 'vault.pileB' : 'vault.pile';
    w.assign([
      { k: 'go', to: 'desk.airlock' },
      { k: 'work', anim: 'grab', dur: 0.35 },
      { k: 'do', fn: (wk) => wk.pickUp(bag.obj, true) },
      { k: 'go', to: spot },
      { k: 'work', anim: 'grab', dur: 0.3 },
      { k: 'do', fn: (wk) => {
        wk.drop()?.removeFromParent();
        const pile = this.s.pile;
        pile.add(Math.max(2, Math.min(16, Math.round(bag.pnl / 2))));
        this.s.floor.cageDeposit(bag.pnl >= 40);
        const top = pile.top(new THREE.Vector3());
        this.s.sparks.burst(top.x, top.y + 0.3, top.z, '#FDE047', 9, 1.4, 2.0);
        const big = bag.pnl >= 25;
        const txt = big ? `+${money(bag.pnl)} BIG WIN 💰` : `+${money(bag.pnl)} 💰`;
        this.s.say(wk, txt, 'win', { big });
        this.s.activity('vault', 1);
      } },
      { k: 'work', anim: bag.pnl >= 25 ? 'cheer' : 'idle', dur: bag.pnl >= 25 ? 1.4 : 0.2 },
    ]);
  }

  // ── 6. losses: the alarm, and security hurries to the table ─────────────

  private losses(): void {
    if (this.alarmCd > 0 || !this.lossQ.length) return;
    // Slower than wins arrive and not worst-first: the alarm is a beat in the
    // story, and always picking the biggest loss over-reports them.
    this.alarmCd = rand(6, 9);
    const p = this.lossQ.splice(Math.floor(Math.random() * this.lossQ.length), 1)[0]!;
    const amt = money(p.pnlUsd ?? 0);
    const subj = subject(p.ticker, p.title);
    this.s.floor.alarm = 2.6;
    this.s.activity('vault', 0.8);
    this.s.log(`🚨 −${amt} · ${subj}`, 'loss');
    const w = this.worker('risk');
    if (w) {
      w.assign([
        { k: 'work', anim: 'panic', dur: 0.5 },
        { k: 'go', to: 'desk.termB', run: true },
        { k: 'work', anim: 'type', dur: 1.1 },
        { k: 'do', fn: (wk) => {
          // A settlement that went against us, not a stop: say what happened.
          this.s.say(wk, pick([`settled against us · −${amt}`, `logged it: −${amt}`, `lost the stake · −${amt}`]), 'loss', { force: true });
          this.s.activity('desk', 0.6);
        } },
        { k: 'work', anim: 'type', dur: 0.8 },
      ]);
    } else if (this.lossTalkCd <= 0) {
      const t = pick(this.s.crew.filter((c) => c.role === 'trader'));
      if (t && this.s.say(t, `ouch · −${amt} on ${subj}`, 'loss')) this.lossTalkCd = 4;
    }
  }

  // ── 7. risk: standing rules firing, daily stops ───────────────────────────

  private risk(): void {
    if (!this.riskQ.length) return;
    const w = this.worker('risk');
    if (!w) return;
    const b = this.riskQ.shift()!;
    if (b.alarm) this.s.floor.alarm = 2.6;
    this.s.log(b.log, b.tone);
    this.s.activity('vault', 1);
    w.assign([
      ...(b.alarm ? [{ k: 'work', anim: 'panic', dur: 0.5 } as Step] : []),
      { k: 'go', to: 'vault.risk', run: !!b.alarm },
      { k: 'work', anim: 'type', dur: 0.7 },
      { k: 'do', fn: (wk) => { this.s.say(wk, b.text, b.tone, { force: true, life: 3.4 }); } },
      { k: 'work', anim: 'type', dur: 1.0 },
    ]);
  }

  // ── 8. roulette: a real optimiser run's result, or a schedule flip ──────

  private checkSchedule(): void {
    const t = new Date(this.now());
    const hour = t.getUTCHours();
    const next = activeSlots(scheduledRunners(this.config), hour);
    const prev = this.slotsNow;
    this.slotsNow = next;
    // The first read primes: opening the hub mid-hour is not a flip.
    if (prev === null || hour === this.slotHour) {
      this.slotHour = hour;
      return;
    }
    this.slotHour = hour;
    for (const f of scheduleFlips(prev, next)) {
      const line = flipLine(f, hour);
      push(this.optQ, { room: 'optimizer', text: line, log: line, tone: 'opt' }, ROOM_CAP);
    }
  }

  private optimizer(): void {
    if (!this.optQ.length || this.optimizing) return;
    const w = this.worker('engineer', (x) => x.home === 'optimizer.panel');
    if (!w) return;
    const b = this.optQ.shift()!;
    this.optimizing = true;
    w.assign([
      { k: 'go', to: 'optimizer.panel' },
      { k: 'do', fn: () => {
        this.s.floor.setSpinning(true);
        this.s.activity('optimizer', 1);
      } },
      // long enough to see the wheel spin up
      { k: 'work', anim: 'type', dur: 2.0 },
      { k: 'do', fn: (wk) => {
        this.s.floor.setSpinning(false);
        this.s.say(wk, b.text, b.tone, { force: true, life: 3.4 });
        this.s.log(b.log, b.tone);
        this.s.activity('optimizer', 1);
        wk.pickUp(this.s.kit.tablet());
      } },
      { k: 'go', to: 'bridge.report' },
      { k: 'work', anim: 'grab', dur: 0.3 },
      { k: 'do', fn: (wk) => {
        wk.drop()?.removeFromParent();
        this.optimizing = false;
        this.s.floor.blueyHop = 0.8;
      } },
    ]);
  }

  // ── 9. the pit boss: AI agent supervision ──────────────────────────────

  private bridge(): void {
    if (this.bridgeCd > 0 || !this.bridgeQ.length) return;
    this.bridgeCd = 3.2;
    const b = this.bridgeQ.shift()!;
    this.s.say('captain', b.text, b.tone, { force: true, life: 3.4 });
    this.s.log(b.log, b.tone);
    this.s.floor.blueyHop = 1;
    this.s.activity('bridge', 1);
  }

  // ── 10. the user's agents ───────────────────────────────────────────────

  private agents(): void {
    if (!this.agentQ.size || !this.s.agent) return;
    for (const q of this.agentQ.values()) {
      if (!q.beats.length) continue;
      const w = this.s.agent(q.who);
      if (!w) {
        // no lounge spot free: this agent is not staged, and its queue never grows
        q.beats.length = 0;
        continue;
      }
      w.tag = { label: tagLabel(q.who), color: q.who.color };
      if (!w.free) continue;
      const b = q.beats.shift()!;
      const say = (wk: Worker) => {
        this.s.say(wk, b.text, b.tone, { force: true, life: 3.4 });
        this.s.log(b.log, b.tone);
        this.s.activity(b.room, 1);
        if (b.run) this.s.floor.alarm = Math.max(this.s.floor.alarm, 1.4);
        if (b.room === 'research') this.s.floor.oddsPing(q.who.color);
      };
      if (b.connect) {
        w.assign([
          { k: 'go', to: w.home },
          { k: 'do', fn: say },
          { k: 'work', anim: 'cheer', dur: 1.1 },
        ]);
        continue;
      }
      // Two guest spots per area; an agent keeps to "its" side, so two
      // agents at the same table stand apart.
      const side = ((w as { id?: number }).id ?? 0) % 2 ? 'B' : 'A';
      w.assign([
        ...(b.run ? [{ k: 'work', anim: 'panic', dur: 0.4 } as Step] : []),
        { k: 'go', to: `${b.room}.agent${side}`, run: b.run },
        { k: 'work', anim: b.anim, dur: 0.6 },
        { k: 'do', fn: say },
        { k: 'work', anim: b.run ? 'type' : 'look', dur: 0.9 },
      ]);
    }
  }

  /** PAYDAY (a real big win, see payday.ts): a storm of gold chips from
   *  the POLYMARKET teller window straight into the cage. */
  payday(): void {
    const floor = this.s.floor;
    const pile = this.s.pile;
    for (let i = 0; i < 28; i++) {
      window.setTimeout(() => {
        const bag = this.s.kit.bag();
        bag.scale.setScalar(0.7);
        const top = pile.top(new THREE.Vector3());
        top.x += rand(-0.5, 0.5);
        top.z += rand(-0.5, 0.5);
        this.s.flights.launch(bag, floor.teller.position, top, 1.3 + Math.random() * 0.4, 5 + Math.random() * 2, () => {
          pile.add(6);
          this.s.sparks.burst(top.x, top.y, top.z, '#FDE047', 10, 2, 2.5);
          floor.cageDeposit(true);
        }, true);
      }, i * 70);
    }
    for (const w of this.s.crew) {
      if (w.role === 'keeper' || w.role === 'trader' || (w.free && Math.random() < 0.6)) {
        w.assign([{ k: 'work', anim: 'cheer', dur: 2.6 + Math.random() }]);
      }
    }
    floor.blueyHop = 2.5;
  }
}
