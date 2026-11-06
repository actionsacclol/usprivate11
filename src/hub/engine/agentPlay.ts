import { autoPlay, handTotal, type Shoe } from '../casino/blackjack';
import { agentBet, betLabel, colorOf, payoutMultiplier, settle, spin } from '../casino/roulette';
import { SYM_LABEL, spinSlots } from '../casino/slots';
import { START_CHIPS, type CasinoSave } from '../casino/wallet';
import type { Rng } from '../casino/rng';
import type { AgentIdentity } from '../../utils/agents';
import type { CasinoFloor } from './floor';
import type { Worker } from './crew';
import type { Tone } from './overlay';
import { PLAY_SEATS, PROP, SLOTS, type PlaySeat } from './layout';

// Your agents, between real jobs, at the tables. What they do here is PLAY
// CHIPS: their own stacks (saved with yours, see casino/wallet.ts), the real
// rules, basic strategy at blackjack. A real tool call still wins: the
// director walks the agent to the table that call belongs to, and because the
// agent's home is the seat it was playing, the walk back ends at its game.

export interface PlayHost {
  time(): number;
  rng: Rng;
  floor: CasinoFloor;
  shoe(table: number): Shoe;
  save: CasinoSave;
  persist(): void;
  say(w: Worker, text: string, tone: Tone, opts?: { life?: number; force?: boolean }): boolean;
  /** The dealer at blackjack table t deals (if it's standing there). */
  dealerDeals(table: number): void;
  /** You are at the wheel: the agents bet on your spins, never start one. */
  userAtWheel(): boolean;
  /** You are playing blackjack table t: its felt shows your cards, not theirs. */
  userAtTable(table: number): boolean;
  /** A sound from their game, where it's played (the engine decides whether
   *  anyone is on the floor to hear it). */
  sfx?(kind: 'roulette' | 'slots' | 'bells', at: { x: number; z: number }, dur: number): void;
}

interface Guest {
  id: string;
  w: Worker;
  who: AgentIdentity;
  seat: PlaySeat | null;
  /** walking to the cage for a refill */
  cashier: boolean;
  next: number;
  rounds: number;
  /** a round on a timer (reels, the wheel) */
  pending: { at: number; fn: () => void } | null;
  /** when this guest's table cards come off the felt */
  clearAt: number;
}

const CAGE_SPOTS = ['vault.agentA', 'vault.agentB'];

function k(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(Math.round(n));
}

function signed(n: number): string {
  return `${n >= 0 ? '+' : '−'}${k(Math.abs(n))}`;
}

export class AgentPlay {
  private guests = new Map<string, Guest>();
  private taken = new Set<string>();
  private dirty = false;
  private saveCd = 0;

  constructor(private host: PlayHost) {}

  add(id: string, w: Worker, who: AgentIdentity): void {
    if (this.guests.has(id)) return;
    if (this.host.save.agents[id] === undefined) {
      this.host.save.agents[id] = START_CHIPS;
      this.dirty = true;
    }
    this.guests.set(id, {
      id, w, who, seat: null, cashier: false, rounds: 0, pending: null, clearAt: 0,
      next: this.host.time() + 2 + Math.random() * 3,
    });
  }

  /** The same figure, known by a new id (the Default agent, once its real
   *  client shows up): its seat and stack move with it. */
  rekey(from: string, to: string, who: AgentIdentity): void {
    const g = this.guests.get(from);
    if (!g || this.guests.has(to)) return;
    this.guests.delete(from);
    g.id = to;
    g.who = who;
    const a = this.host.save.agents;
    if (a[to] === undefined) a[to] = a[from] ?? START_CHIPS;
    this.guests.set(to, g);
    this.dirty = true;
  }

  remove(id: string): void {
    const g = this.guests.get(id);
    if (!g) return;
    if (g.seat) this.taken.delete(g.seat.node);
    this.guests.delete(id);
  }

  chips(id: string): number {
    return this.host.save.agents[id] ?? START_CHIPS;
  }

  stacks(): { name: string; color: string; chips: number }[] {
    return [...this.guests.values()].map((g) => ({ name: g.who.name, color: g.who.color, chips: this.chips(g.id) }));
  }

  /** Who sits where (for the tags): "Sports Sam · 1.2K". */
  tagFor(id: string, base: string): string {
    return this.guests.has(id) ? `${base} · ${k(this.chips(id))}` : base;
  }

  private credit(g: Guest, n: number): void {
    this.host.save.agents[g.id] = Math.max(0, this.chips(g.id) + n);
    this.dirty = true;
  }

  private seatFor(g: Guest): PlaySeat | null {
    const free = PLAY_SEATS.filter((s) => !this.taken.has(s.node));
    if (!free.length) return null;
    // a change of game now and then: prefer one it isn't already at
    const prefer = g.seat ? free.filter((s) => s.kind !== g.seat!.kind) : free;
    const pool = prefer.length ? prefer : free;
    // weight the kinds evenly, whatever the seat counts
    const kinds = [...new Set(pool.map((s) => s.kind))];
    const kind = kinds[Math.floor(this.host.rng() * kinds.length)];
    const of = pool.filter((s) => s.kind === kind);
    return of[Math.floor(this.host.rng() * of.length)];
  }

  private sit(g: Guest, seat: PlaySeat | null): void {
    if (g.seat) this.taken.delete(g.seat.node);
    g.seat = seat;
    g.rounds = 0;
    if (seat) {
      this.taken.add(seat.node);
      g.w.home = seat.node;
    }
  }

  update(dt: number): void {
    const now = this.host.time();
    for (const g of this.guests.values()) {
      if (g.pending && now >= g.pending.at) {
        const fn = g.pending.fn;
        g.pending = null;
        fn();
      }
      if (g.clearAt && now >= g.clearAt && g.seat?.kind === 'blackjack' && !this.host.userAtTable(g.seat.table)) {
        this.host.floor.setBjCards(g.seat.table, [], []);
        g.clearAt = 0;
      }
      // walking, working a real job, or a round in flight: not now
      if (g.w.busy || g.pending) continue;
      if (g.cashier) {
        if (g.w.atHome) {
          this.host.save.agents[g.id] = START_CHIPS;
          this.dirty = true;
          g.cashier = false;
          this.host.say(g.w, `chips from the cage: ${k(START_CHIPS)}`, 'gold', { life: 2.8 });
          this.sit(g, this.seatFor(g));
        }
        continue;
      }
      if (this.chips(g.id) < 10) {
        // broke: off to the cage
        this.sit(g, null);
        g.cashier = true;
        g.w.home = CAGE_SPOTS[g.w.id % CAGE_SPOTS.length];
        this.host.say(g.w, 'out of chips — to the cage', 'loss', { life: 2.6 });
        continue;
      }
      if (!g.seat || g.rounds >= 6 + (g.w.id % 5)) {
        const s = this.seatFor(g);
        if (s) this.sit(g, s);
        g.next = now + 1;
        continue;
      }
      if (!g.w.atHome || now < g.next) continue;
      this.round(g, now);
    }
    this.saveCd -= dt;
    if (this.dirty && this.saveCd <= 0) {
      this.dirty = false;
      this.saveCd = 5;
      this.host.persist();
    }
  }

  private betOf(g: Guest): number {
    const c = this.chips(g.id);
    return Math.max(5, Math.min(100, Math.round((c * 0.04) / 5) * 5));
  }

  private round(g: Guest, now: number): void {
    const seat = g.seat!;
    g.rounds++;
    g.next = now + 6 + this.host.rng() * 5;
    const bet = Math.min(this.betOf(g), this.chips(g.id));
    if (seat.kind === 'blackjack') {
      const shoe = this.host.shoe(seat.table);
      const r = autoPlay(shoe, bet, (n) => this.chips(g.id) - bet >= n);
      const net = r.returned - r.staked;
      this.credit(g, net);
      this.host.dealerDeals(seat.table);
      if (!this.host.userAtTable(seat.table)) {
        this.host.floor.setBjCards(seat.table, r.dealer, r.hands.map((h) => h.cards));
        g.clearAt = now + 3.5;
      }
      const p = handTotal(r.hands[0].cards).total;
      const d = handTotal(r.dealer).total;
      const res = r.hands[0].result;
      const text = res === 'blackjack' ? `🃏 blackjack! ${signed(net)}`
        : res === 'bust' ? `🃏 busted on ${p} · ${signed(net)}`
          : `🃏 ${p} vs ${d} · ${net === 0 ? 'push' : signed(net)}`;
      this.host.say(g.w, text, net > 0 ? 'win' : net < 0 ? 'loss' : 'neutral', { life: 2.8 });
      g.w.assign([{ k: 'work', anim: net > 0 ? 'cheer' : 'look', dur: net > 0 ? 1.0 : 0.8 }]);
      return;
    }
    if (seat.kind === 'roulette') {
      // the wheel is shared: one spin at a time, and never under your bets
      if (this.host.floor.rouletteBusy || this.host.userAtWheel()) {
        g.rounds--;
        g.next = now + 2;
        return;
      }
      const n = spin(this.host.rng);
      const dur = 4.6;
      this.host.floor.spinRoulette(n, dur);
      this.host.sfx?.('roulette', { x: PROP.wheel[0], z: PROP.wheel[1] }, dur);
      // everyone seated at the wheel rides this spin
      for (const o of this.guests.values()) {
        if (o.seat?.kind === 'roulette' && o.w.atHome && !o.pending) this.betOnSpin(o, n, dur, now);
      }
      return;
    }
    // slots
    const res = spinSlots(this.host.rng);
    const dur = 2.0;
    this.host.floor.spinSlot(seat.table, res.stops, dur);
    const m = SLOTS[seat.table];
    this.host.sfx?.('slots', { x: m.x, z: m.z }, dur);
    this.credit(g, -bet);
    g.pending = {
      at: now + dur + 0.1,
      fn: () => {
        const back = res.pays * bet;
        this.credit(g, back);
        if (back > 0) {
          this.host.floor.slotWin(seat.table);
          this.host.sfx?.('bells', { x: m.x, z: m.z }, 0);
          const what = res.pays >= 10 ? `3 × ${SYM_LABEL[res.line[0]]}` : 'two cherries';
          this.host.say(g.w, `🎰 ${what} · ${signed(back - bet)}`, 'win', { life: 2.8 });
          g.w.assign([{ k: 'work', anim: 'cheer', dur: back >= bet * 10 ? 1.6 : 0.8 }]);
        } else if (this.host.rng() < 0.35) {
          this.host.say(g.w, `🎰 nothing · ${signed(-bet)}`, 'neutral', { life: 2.2 });
        }
      },
    };
  }

  private betOnSpin(g: Guest, n: number, dur: number, now: number): void {
    const bet = Math.min(this.betOf(g), this.chips(g.id));
    if (bet <= 0) return;
    const b = agentBet(this.host.rng);
    this.credit(g, -bet);
    g.next = Math.max(g.next, now + dur + 3 + this.host.rng() * 4);
    g.pending = {
      at: now + dur + 0.15,
      fn: () => {
        const back = settle([{ bet: b, stake: bet }], n);
        this.credit(g, back);
        const net = back - bet;
        const col = colorOf(n);
        this.host.say(g.w, `🎡 ${n} ${col} · ${betLabel(b)}${net > 0 ? ` pays ${payoutMultiplier(b)}:1` : ''} · ${signed(net)}`, net > 0 ? 'win' : 'loss', { life: 2.8 });
        g.w.assign([{ k: 'work', anim: net > 0 ? 'cheer' : 'look', dur: net > 0 ? 1.1 : 0.7 }]);
      },
    };
  }

  /** You spun: your agents at the wheel bet on it too. */
  joinSpin(n: number, dur: number): void {
    const now = this.host.time();
    for (const g of this.guests.values()) {
      if (g.seat?.kind === 'roulette' && g.w.atHome && !g.pending) this.betOnSpin(g, n, dur, now);
    }
  }

}
