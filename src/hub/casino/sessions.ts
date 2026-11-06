import {
  activeHand, basicStrategy, canDouble, canSplit, deal, double, handTotal, hit, split, stand,
  type BjRound, type Card, type Shoe,
} from './blackjack';
import { betLabel, colorOf, settle, spin, type PlacedBet, type RouletteBet } from './roulette';
import { SYM_LABEL, spinSlots, type SlotSpin } from './slots';
import {
  americanOdds, closesIn, ticketReturn, writeTicket, type BoardRow,
} from './sportsbook';
import { REFILL_BELOW, START_CHIPS, canRefill, record, type CasinoSave } from './wallet';
import { COCKTAILS, orderCocktail, type Cocktail, type Garnish } from './bar';
import { drawIcon } from './hotbarUi';
import { paintBets, paintMarkets } from './sportsbookScreens';
import type { Rng } from './rng';
import type { GameKind } from '../engine/layout';
import {
  STAKES, banner, button, chipsText, fit, label, panel, rr, stepStake, type Btn, type View,
} from './ui';

// One game at one table, played by you on foot. Every game is PLAY CHIPS:
// nothing here can reach an order, a wallet or the backend. Each session owns
// its pacing (cards land one by one, the wheel runs its seconds, reels stop
// left to right) and pokes the 3D floor through CasinoCtx only.

export interface CasinoCtx {
  save: CasinoSave;
  persist(): void;
  rng: Rng;
  shoe(table: number): Shoe;
  /** 3D: the cards on blackjack table t (null = face down). */
  bjCards(table: number, dealer: (Card | null)[], hands: Card[][]): void;
  /** The dealer NPC at table t deals. */
  dealerDeals(table: number): void;
  spinWheel(n: number, dur: number): void;
  wheelBusy(): boolean;
  /** Your agents at the wheel bet on the same spin. */
  wheelJoin(n: number, dur: number): void;
  spinSlot(machine: number, stops: SlotSpin['stops'], dur: number): void;
  slotWin(machine: number): void;
  board(): { rows: BoardRow[]; at: number | null; note: string | null };
  /** A line for the floor's feed (big wins only: the feed is for real events). */
  brag(text: string): void;
}

export interface GameSession {
  readonly kind: GameKind;
  key(code: string): boolean;
  /** Something in the world was clicked (a chip, a plaque, a cell on the
   *  felt, a button on a screen): its id. */
  click(id: string): void;
  /** The mouse wheel at the table (a chip size, a stake). */
  wheel?(dir: 1 | -1): void;
  update(dt: number): void;
  /** The keys, for the one thin line along the bottom while you play. */
  hint(): string;
  /** Leaving the table: clear what the session put on it. */
  close(): void;
}

/** A session whose screen is a screen in the world (a betting terminal,
 *  the cashier's ledger, the bar's menu): it paints into it, and a click on
 *  it comes back as one of the buttons it drew. */
export interface PaintedSession extends GameSession {
  paint(g: CanvasRenderingContext2D, v: View): Btn[];
}

export function isPainted(s: GameSession): s is PaintedSession {
  return typeof (s as PaintedSession).paint === 'function';
}

/** Chips, by value, as they sit on the felt. */
export const CHIP_VALUES = [5, 25, 100, 500] as const;

/** What a blackjack table shows right now, for the 3D table to lay out. */
export interface BjView {
  phase: 'bet' | 'deal' | 'play' | 'dealer' | 'done';
  dealer: { cards: (Card | null)[]; total: number | null };
  hands: { cards: Card[]; bet: number; total: number; soft: boolean; active: boolean; result: string | null; doubled: boolean }[];
  can: { deal: boolean; hit: boolean; stand: boolean; double: boolean; split: boolean; bet: boolean };
  /** basic strategy's call, while it's your move */
  tip: string | null;
  msg: { text: string; color: string; age: number } | null;
}

function take(ctx: CasinoCtx, n: number): boolean {
  if (ctx.save.chips < n) return false;
  ctx.save.chips -= n;
  return true;
}

function give(ctx: CasinoCtx, n: number): void {
  ctx.save.chips += n;
}

function signed(n: number): string {
  return `${n >= 0 ? '+' : '−'}${chipsText(Math.abs(n))}`;
}

// ── blackjack ──────────────────────────────────────────────────────────────

export class BlackjackSession implements GameSession {
  readonly kind = 'blackjack' as const;
  /** The chips in your betting circle, in the order you put them there
   *  (kept for the next hand: a rebet). */
  stack: number[] = [];
  private round: BjRound | null = null;
  /** What the table shows so far (cards land one at a time). */
  private shownPlayer: number[] = [];
  private shownDealer = 0;
  private holeOpen = false;
  private opened = false;
  private settled = false;
  private wait = 0;
  private msg: { text: string; color: string; age: number } | null = null;

  constructor(private ctx: CasinoCtx, readonly table: number) {
    if (ctx.save.chips >= 25) this.stack = [25];
    ctx.bjCards(table, [], []);
  }

  get chips(): number {
    return this.ctx.save.chips;
  }

  /** Your bet: the chips in the circle. */
  get bet(): number {
    return this.stack.reduce((a, c) => a + c, 0);
  }

  private addChip(v: number): void {
    if (this.busy || this.bet + v > this.ctx.save.chips) return;
    this.stack.push(v);
  }

  private takeChip(): void {
    if (!this.busy) this.stack.pop();
  }

  private get busy(): boolean {
    return this.round !== null && !this.settled;
  }

  private deal(): void {
    if (this.busy || this.bet <= 0) return;
    if (!take(this.ctx, this.bet)) {
      this.msg = { text: 'Not enough chips — the cashier will top you up', color: '#FCA5A5', age: 0 };
      return;
    }
    this.round = deal(this.ctx.shoe(this.table), this.bet);
    this.shownPlayer = [0];
    this.shownDealer = 0;
    this.holeOpen = false;
    this.opened = false;
    this.settled = false;
    this.msg = null;
    this.wait = 0.05;
    this.ctx.dealerDeals(this.table);
  }

  private act(move: 'hit' | 'stand' | 'double' | 'split'): void {
    const r = this.round;
    if (!r || r.phase !== 'player' || !this.caughtUp) return;
    const h = activeHand(r)!;
    const shoe = this.ctx.shoe(this.table);
    if (move === 'double') {
      if (!canDouble(r) || !take(this.ctx, h.bet)) return;
      double(r, shoe);
    } else if (move === 'split') {
      if (!canSplit(r) || !take(this.ctx, h.bet)) return;
      split(r, shoe);
      this.shownPlayer = [1, 1];
    } else if (move === 'hit') hit(r, shoe);
    else stand(r, shoe);
    this.ctx.dealerDeals(this.table);
    this.wait = 0.05;
  }

  /** Every card you were dealt is on the table: you may act. */
  private get caughtUp(): boolean {
    const r = this.round;
    if (!r) return true;
    return this.opened && r.hands.every((h, i) => (this.shownPlayer[i] ?? 0) >= h.cards.length);
  }

  update(dt: number): void {
    if (this.msg) this.msg.age += dt;
    const r = this.round;
    if (!r || this.settled) return;
    this.wait -= dt;
    if (this.wait > 0) return;
    // One card lands per beat: the opening four alternate (you, dealer, you,
    // the hole card), then each hand's draws in order, then the hole card
    // turns, then the dealer draws, then the round settles.
    const pace = 0.32;
    while (this.shownPlayer.length < r.hands.length) this.shownPlayer.push(1);
    const nextHand = r.hands.findIndex((h, k) => (this.shownPlayer[k] ?? 0) < h.cards.length);
    if (!this.opened) {
      if (this.shownPlayer[0] <= this.shownDealer) this.shownPlayer[0]++;
      else this.shownDealer++;
      if (this.shownDealer >= 2 && this.shownPlayer[0] >= 2) this.opened = true;
      this.wait = pace;
    } else if (nextHand >= 0) {
      this.shownPlayer[nextHand]++;
      this.wait = pace;
    } else if (r.phase === 'done' && !this.holeOpen) {
      this.holeOpen = true;
      this.wait = 0.45;
    } else if (r.phase === 'done' && this.shownDealer < r.dealer.length) {
      this.shownDealer++;
      this.wait = 0.5;
    } else if (r.phase === 'done') {
      this.finish(r);
    }
    this.push3d();
  }

  private finish(r: BjRound): void {
    this.settled = true;
    give(this.ctx, r.returned);
    record(this.ctx.save, r.staked, r.returned);
    this.ctx.persist();
    const net = r.returned - r.staked;
    const res = r.hands.map((h) => h.result);
    let text: string;
    if (res.includes('blackjack')) text = `Blackjack! ${signed(net)}`;
    else if (res.length === 1 && res[0] === 'bust') text = `Bust ${signed(net)}`;
    else if (handTotal(r.dealer).total > 21 && net > 0) text = `Dealer busts ${signed(net)}`;
    else if (net > 0) text = `You win ${signed(net)}`;
    else if (net === 0) text = 'Push — your bet back';
    else text = `Dealer wins ${signed(net)}`;
    this.msg = { text, color: net > 0 ? '#4ADE80' : net === 0 ? '#E5E7EB' : '#F87171', age: 0 };
    if (net >= 500) this.ctx.brag(`🃏 you won ${chipsText(net)} chips at blackjack`);
    // the same bet again, as far as your stack goes
    while (this.stack.length && this.bet > this.ctx.save.chips) this.stack.pop();
  }

  view(): BjView {
    const r = this.round;
    const playing = r?.phase === 'player' && this.caughtUp;
    const phase: BjView['phase'] = !r || this.settled ? (r && this.settled ? 'done' : 'bet') : !this.opened ? 'deal' : r.phase === 'player' ? 'play' : 'dealer';
    const dealerCards: (Card | null)[] = r ? r.dealer.slice(0, this.shownDealer).map((c, i) => (i === 1 && !this.holeOpen ? null : c)) : [];
    const seen = dealerCards.filter((c): c is Card => c !== null);
    const can = {
      deal: !this.busy && this.bet > 0 && this.ctx.save.chips >= this.bet,
      hit: !!playing,
      stand: !!playing,
      double: !!playing && !!r && canDouble(r) && this.ctx.save.chips >= activeHand(r)!.bet,
      split: !!playing && !!r && canSplit(r) && this.ctx.save.chips >= activeHand(r)!.bet,
      bet: !this.busy,
    };
    let tip: string | null = null;
    if (playing && r) {
      const h = activeHand(r)!;
      tip = basicStrategy(h.cards, r.dealer[0], { canDouble: canDouble(r), canSplit: canSplit(r) });
    }
    return {
      phase,
      dealer: { cards: dealerCards, total: seen.length ? handTotal(seen).total : null },
      hands: r ? r.hands.map((h, k) => {
        const cards = h.cards.slice(0, this.shownPlayer[k] ?? 0);
        const t = handTotal(cards);
        return {
          cards, bet: h.bet, total: t.total, soft: t.soft, doubled: !!h.doubled,
          active: r.phase === 'player' && r.active === k && r.hands.length > 1,
          result: this.settled ? h.result ?? null : null,
        };
      }) : [],
      can, tip, msg: this.msg,
    };
  }

  hint(): string {
    return this.busy
      ? 'H hit · Space stand · X double · P split · or click the plaques · Esc or move to leave'
      : 'Click chips to bet · click your stack to take one back · Space deal · Esc or move to leave';
  }

  private push3d(): void {
    const r = this.round;
    if (!r) { this.ctx.bjCards(this.table, [], []); return; }
    const dealer: (Card | null)[] = r.dealer.slice(0, this.shownDealer).map((c, i) => (i === 1 && !this.holeOpen ? null : c));
    const hands = r.hands.map((h, i) => h.cards.slice(0, this.shownPlayer[i] ?? 0));
    this.ctx.bjCards(this.table, dealer, hands);
  }

  key(code: string): boolean {
    switch (code) {
      case 'Space': case 'Enter': case 'KeyN':
        if (!this.busy) this.deal();
        else if (this.round?.phase === 'player') this.act('stand');
        return true;
      case 'KeyH': this.act('hit'); return true;
      case 'KeyX': this.act('double'); return true;
      case 'KeyP': this.act('split'); return true;
      case 'Minus': case 'NumpadSubtract': case 'Backspace':
        this.takeChip(); return true;
      case 'Equal': case 'NumpadAdd':
        this.addChip(25); return true;
      case 'KeyC':
        if (!this.busy) this.stack = []; return true;
    }
    return false;
  }

  click(id: string): void {
    if (id === 'deal') this.deal();
    else if (id === 'hit' || id === 'stand' || id === 'double' || id === 'split') this.act(id);
    else if (id.startsWith('chip:')) this.addChip(Number(id.slice(5)));
    else if (id === 'bet') this.takeChip();
    else if (id === 'clear' && !this.busy) this.stack = [];
  }

  close(): void {
    // a hand in play is abandoned: its stake stays on the table, like a real one
    this.ctx.bjCards(this.table, [], []);
  }
}

// ── roulette ───────────────────────────────────────────────────────────────

const OUTSIDE: { id: string; bet: RouletteBet; key: string; label: string; color: string }[] = [
  { id: 'red', bet: { kind: 'red' }, key: 'R', label: 'RED', color: '#EF4444' },
  { id: 'black', bet: { kind: 'black' }, key: 'B', label: 'BLACK', color: '#9CA3AF' },
  { id: 'odd', bet: { kind: 'odd' }, key: 'O', label: 'ODD', color: '#38BDF8' },
  { id: 'even', bet: { kind: 'even' }, key: 'E', label: 'EVEN', color: '#38BDF8' },
  { id: 'low', bet: { kind: 'low' }, key: 'L', label: '1–18', color: '#38BDF8' },
  { id: 'high', bet: { kind: 'high' }, key: 'H', label: '19–36', color: '#38BDF8' },
  { id: 'd1', bet: { kind: 'dozen', n: 1 }, key: '1', label: '1st 12', color: '#A78BFA' },
  { id: 'd2', bet: { kind: 'dozen', n: 2 }, key: '2', label: '2nd 12', color: '#A78BFA' },
  { id: 'd3', bet: { kind: 'dozen', n: 3 }, key: '3', label: '3rd 12', color: '#A78BFA' },
];

/** A bet's spot on the felt, as an id: n0..n36, red, black, odd, even, low,
 *  high, d1..d3 (dozens), c1..c3 (columns). */
export function betKey(b: RouletteBet): string {
  if (b.kind === 'straight') return `n${b.n}`;
  if (b.kind === 'dozen') return `d${b.n}`;
  if (b.kind === 'column') return `c${b.n}`;
  return b.kind;
}

export function betFromKey(k: string): RouletteBet | null {
  if (/^n\d+$/.test(k)) {
    const n = Number(k.slice(1));
    return n >= 0 && n <= 36 ? { kind: 'straight', n } : null;
  }
  if (/^[dc][123]$/.test(k)) return { kind: k[0] === 'd' ? 'dozen' : 'column', n: Number(k[1]) as 1 | 2 | 3 };
  if (['red', 'black', 'odd', 'even', 'low', 'high'].includes(k)) return { kind: k as 'red' };
  return null;
}

export class RouletteSession implements GameSession {
  readonly kind = 'roulette' as const;
  /** The chip in your hand: what a click on the felt puts down. */
  stake: number = CHIP_VALUES[1];
  private num = 17;
  bets: PlacedBet[] = [];
  spinning: { n: number; t: number; dur: number; staked: number } | null = null;
  msg: { text: string; color: string; age: number } | null = null;
  last: number[] = [];
  /** The last spin's number and what each bet did, for the felt to show. */
  outcome: { n: number; age: number; bets: { key: string; stake: number; back: number }[] } | null = null;
  private placed: { key: string; stake: number }[] = [];

  constructor(private ctx: CasinoCtx) {}

  private place(bet: RouletteBet): void {
    if (this.spinning) return;
    if (!take(this.ctx, this.stake)) {
      this.msg = { text: 'Not enough chips', color: '#FCA5A5', age: 0 };
      return;
    }
    if (this.outcome) this.outcome = null;
    const same = this.bets.find((p) => betLabel(p.bet) === betLabel(bet));
    if (same) same.stake += this.stake;
    else this.bets.push({ bet, stake: this.stake });
    this.placed.push({ key: betKey(bet), stake: this.stake });
    this.msg = null;
  }

  private clear(): void {
    if (this.spinning) return;
    for (const p of this.bets) give(this.ctx, p.stake);
    this.bets = [];
    this.placed = [];
  }

  /** Take back the last chip you put down. */
  private undo(): void {
    if (this.spinning) return;
    const last = this.placed.pop();
    if (!last) return;
    const p = this.bets.find((b) => betKey(b.bet) === last.key);
    if (!p) return;
    p.stake -= last.stake;
    give(this.ctx, last.stake);
    if (p.stake <= 0) this.bets = this.bets.filter((b) => b !== p);
  }

  /** What's on a spot of the felt. */
  stakeOn(key: string): number {
    return this.bets.find((b) => betKey(b.bet) === key)?.stake ?? 0;
  }

  wheel(dir: 1 | -1): void {
    const i = CHIP_VALUES.indexOf(this.stake as (typeof CHIP_VALUES)[number]);
    this.stake = CHIP_VALUES[Math.max(0, Math.min(CHIP_VALUES.length - 1, (i < 0 ? 1 : i) + dir))];
  }

  hint(): string {
    return this.spinning
      ? 'No more bets: the ball is rolling'
      : 'Pick a chip on the rail (or − +) · click the felt to bet · Space spin · U undo · C clear · Esc or move to leave';
  }

  private spin(): void {
    if (this.spinning || !this.bets.length || this.ctx.wheelBusy()) return;
    const n = spin(this.ctx.rng);
    const dur = 5.5;
    this.ctx.spinWheel(n, dur);
    this.ctx.wheelJoin(n, dur);
    this.spinning = { n, t: 0, dur, staked: this.bets.reduce((s, p) => s + p.stake, 0) };
    this.msg = null;
  }

  update(dt: number): void {
    if (this.msg) this.msg.age += dt;
    if (this.outcome) this.outcome.age += dt;
    const s = this.spinning;
    if (!s) return;
    s.t += dt;
    if (s.t < s.dur + 0.2) return;
    const back = settle(this.bets, s.n);
    this.outcome = { n: s.n, age: 0, bets: this.bets.map((p) => ({ key: betKey(p.bet), stake: p.stake, back: settle([p], s.n) })) };
    give(this.ctx, back);
    record(this.ctx.save, s.staked, back);
    this.ctx.persist();
    this.last.push(s.n);
    if (this.last.length > 12) this.last.shift();
    const col = colorOf(s.n);
    const net = back - s.staked;
    this.msg = { text: `${s.n} ${col.toUpperCase()} · ${net > 0 ? `you win ${signed(net)}` : net === 0 ? 'even' : signed(net)}`, color: net > 0 ? '#4ADE80' : '#F87171', age: 0 };
    if (net >= 500) this.ctx.brag(`🎡 ${s.n} came in: you won ${chipsText(net)} chips`);
    this.bets = [];
    this.placed = [];
    this.spinning = null;
  }

  key(code: string): boolean {
    const o = OUTSIDE.find((x) => code === `Key${x.key}` || code === `Digit${x.key}`);
    if (o) { this.place(o.bet); return true; }
    switch (code) {
      case 'KeyN': this.place({ kind: 'straight', n: this.num }); return true;
      case 'Comma': this.num = (this.num + 36) % 37; return true;
      case 'Period': this.num = (this.num + 1) % 37; return true;
      case 'Equal': case 'NumpadAdd': this.wheel(1); return true;
      case 'Minus': case 'NumpadSubtract': this.wheel(-1); return true;
      case 'KeyC': this.clear(); return true;
      case 'KeyU': case 'Backspace': this.undo(); return true;
      case 'Space': case 'Enter': this.spin(); return true;
    }
    return false;
  }

  click(id: string): void {
    if (id.startsWith('bet:')) {
      const b = betFromKey(id.slice(4));
      if (b) this.place(b);
      return;
    }
    if (id.startsWith('chip:')) { this.stake = Number(id.slice(5)); return; }
    if (id === 'undo') { this.undo(); return; }
    const o = OUTSIDE.find((x) => x.id === id);
    if (o) this.place(o.bet);
    else if (id === 'num') this.place({ kind: 'straight', n: this.num });
    else if (id === 'nless') this.num = (this.num + 36) % 37;
    else if (id === 'nmore') this.num = (this.num + 1) % 37;
    else if (id === 'sless') this.stake = stepStake(this.stake, -1, this.ctx.save.chips);
    else if (id === 'smore') this.stake = stepStake(this.stake, 1, this.ctx.save.chips);
    else if (id === 'clear') this.clear();
    else if (id === 'spin') this.spin();
  }

  close(): void {
    // bets not yet spun go back to your stack
    if (!this.spinning) this.clear();
  }
}

// ── slots ──────────────────────────────────────────────────────────────────

export class SlotsSession implements GameSession {
  readonly kind = 'slots' as const;
  bet = 10;
  spinning: { r: SlotSpin; t: number; dur: number } | null = null;
  last: SlotSpin | null = null;
  msg: { text: string; color: string; age: number } | null = null;
  /** The last spin's win, and how long ago (the machine's meter counts it up). */
  win: { amount: number; age: number } | null = null;
  private blur = 0;

  constructor(private ctx: CasinoCtx, readonly machine: number) {}

  get chips(): number {
    return this.ctx.save.chips;
  }

  hint(): string {
    return 'Pull the lever, press SPIN or Space · BET − + (or the wheel) · Esc or move to leave';
  }

  private spin(): void {
    if (this.spinning) return;
    if (!take(this.ctx, this.bet)) {
      this.msg = { text: 'Not enough chips', color: '#FCA5A5', age: 0 };
      return;
    }
    const r = spinSlots(this.ctx.rng);
    const dur = 2.2;
    this.ctx.spinSlot(this.machine, r.stops, dur);
    this.spinning = { r, t: 0, dur };
    this.msg = null;
    this.win = null;
  }

  update(dt: number): void {
    if (this.msg) this.msg.age += dt;
    if (this.win) this.win.age += dt;
    this.blur += dt;
    const s = this.spinning;
    if (!s) return;
    s.t += dt;
    if (s.t < s.dur + 0.15) return;
    const back = s.r.pays * this.bet;
    give(this.ctx, back);
    record(this.ctx.save, this.bet, back);
    this.ctx.persist();
    this.last = s.r;
    this.spinning = null;
    if (back > 0) {
      this.win = { amount: back, age: 0 };
      this.ctx.slotWin(this.machine);
      const what = s.r.pays >= 10 ? `3 × ${SYM_LABEL[s.r.line[0]]}` : 'two cherries';
      this.msg = { text: `${what} — pays ${s.r.pays}× · ${signed(back - this.bet)}`, color: '#4ADE80', age: 0 };
      if (back - this.bet >= 500) this.ctx.brag(`🎰 ${what}: you won ${chipsText(back - this.bet)} chips`);
    } else {
      this.msg = { text: `no win · ${signed(-this.bet)}`, color: 'rgba(248,113,113,0.9)', age: 0 };
    }
    if (this.ctx.save.chips < this.bet) this.bet = Math.max(STAKES[0], Math.min(this.bet, this.ctx.save.chips));
  }

  key(code: string): boolean {
    switch (code) {
      case 'Space': case 'Enter': this.spin(); return true;
      case 'Minus': case 'NumpadSubtract': if (!this.spinning) this.bet = stepStake(this.bet, -1, this.ctx.save.chips); return true;
      case 'Equal': case 'NumpadAdd': if (!this.spinning) this.bet = stepStake(this.bet, 1, this.ctx.save.chips); return true;
    }
    return false;
  }

  click(id: string): void {
    if (id === 'spin' || id === 'lever') this.spin();
    else if (id === 'less' && !this.spinning) this.bet = stepStake(this.bet, -1, this.ctx.save.chips);
    else if (id === 'more' && !this.spinning) this.bet = stepStake(this.bet, 1, this.ctx.save.chips);
  }

  wheel(dir: 1 | -1): void {
    if (!this.spinning) this.bet = stepStake(this.bet, dir, this.ctx.save.chips);
  }

  close(): void { /* a spin in flight finishes on the machine; its chips are already settled on its timer */ }
}

// ── the sportsbook ─────────────────────────────────────────────────────────

/** How many markets the station's list monitor shows at once. */
export const SB_LIST_ROWS = 9;

/** A betting station: three monitors — the markets (pick one), the slip
 *  (side, stake, place), and your bets, marked to the live price. */
export class SportsbookSession implements GameSession {
  readonly kind = 'sportsbook' as const;
  private sel = 0;
  private side: 'yes' | 'no' = 'yes';
  private stake = 25;
  private msg: { text: string; color: string; age: number } | null = null;

  constructor(private ctx: CasinoCtx) {}

  private rows(): BoardRow[] {
    return this.ctx.board().rows;
  }

  private place(): void {
    const row = this.rows()[this.sel];
    if (!row) return;
    const price = this.side === 'yes' ? row.yesCents : row.noCents;
    if (price === null) { this.msg = { text: 'Nobody offers that side right now', color: '#FCA5A5', age: 0 }; return; }
    const t = writeTicket(row, this.side, this.stake, Date.now());
    if (!t || !take(this.ctx, this.stake)) { this.msg = { text: 'Not enough chips', color: '#FCA5A5', age: 0 }; return; }
    this.ctx.save.tickets.push(t);
    this.ctx.persist();
    this.msg = { text: `Ticket: ${t.label} @ ${t.priceCents}¢ · returns ${chipsText(ticketReturn(t.stake, t.priceCents))} if it wins`, color: '#C4B5FD', age: 0 };
  }

  update(dt: number): void {
    if (this.msg) this.msg.age += dt;
    const n = this.rows().length;
    if (this.sel >= n) this.sel = Math.max(0, n - 1);
    // a side nobody offers can't be picked: stand on the one that's offered
    const r = this.rows()[this.sel];
    if (r && (this.side === 'yes' ? r.yesCents : r.noCents) === null) this.side = this.side === 'yes' ? 'no' : 'yes';
  }

  key(code: string): boolean {
    const n = this.rows().length;
    switch (code) {
      case 'Comma': this.sel = Math.max(0, this.sel - 1); return true;
      case 'Period': this.sel = Math.min(Math.max(0, n - 1), this.sel + 1); return true;
      case 'PageUp': this.sel = Math.max(0, this.sel - SB_LIST_ROWS); return true;
      case 'PageDown': this.sel = Math.min(Math.max(0, n - 1), this.sel + SB_LIST_ROWS); return true;
      case 'Digit1': this.side = 'yes'; return true;
      case 'Digit2': this.side = 'no'; return true;
      case 'Minus': case 'NumpadSubtract': this.stake = stepStake(this.stake, -1, this.ctx.save.chips); return true;
      case 'Equal': case 'NumpadAdd': this.stake = stepStake(this.stake, 1, this.ctx.save.chips); return true;
      case 'Enter': case 'Space': this.place(); return true;
    }
    return false;
  }

  click(id: string): void {
    if (id.startsWith('row:')) { this.sel = Number(id.slice(4)); return; }
    if (id === 'sideA') this.side = 'yes';
    else if (id === 'sideB') this.side = 'no';
    else if (id === 'less') this.stake = stepStake(this.stake, -1, this.ctx.save.chips);
    else if (id === 'more') this.stake = stepStake(this.stake, 1, this.ctx.save.chips);
    else if (id === 'place') this.place();
    else if (id === 'up') this.sel = Math.max(0, this.sel - SB_LIST_ROWS);
    else if (id === 'down') this.sel = Math.min(Math.max(0, this.rows().length - 1), this.sel + SB_LIST_ROWS);
  }

  hint(): string {
    return 'Click a market, a side, the stake and Place · wheel scrolls · 1/2 side · − + stake · Enter · Esc or move to leave';
  }

  wheel(dir: 1 | -1): void {
    const n = this.rows().length;
    this.sel = Math.max(0, Math.min(Math.max(0, n - 1), this.sel - dir));
  }

  /** The left monitor: the markets, a page round the one you've picked. */
  paintList(g: CanvasRenderingContext2D, v: View): Btn[] {
    const board = this.ctx.board();
    const n = board.rows.length;
    const from = Math.max(0, Math.min(this.sel - Math.floor(SB_LIST_ROWS / 2), n - SB_LIST_ROWS));
    const out = paintMarkets(g, v.W, v.H, {
      rows: board.rows, from, count: SB_LIST_ROWS, sel: this.sel, click: true, at: board.at, note: board.note,
      title: n ? `MARKETS ${from + 1}–${Math.min(n, from + SB_LIST_ROWS)} of ${n}` : 'MARKETS',
    });
    // page buttons in the header
    if (n > SB_LIST_ROWS) {
      const hh = Math.round(v.H * 0.12);
      const bw = 70, x = v.W * 0.44;
      button(g, v, out, 'up', x, 6, bw, hh - 12, '▲', { small: true, enabled: from > 0, color: '#C084FC' });
      button(g, v, out, 'down', x + bw + 8, 6, bw, hh - 12, '▼', { small: true, enabled: from + SB_LIST_ROWS < n, color: '#C084FC' });
    }
    return out;
  }

  /** The middle monitor: the slip. */
  paintSlip(g: CanvasRenderingContext2D, v: View): Btn[] {
    const { W, H } = v;
    const out: Btn[] = [];
    g.fillStyle = '#0a0716';
    g.fillRect(0, 0, W, H);
    const hh = Math.round(H * 0.12);
    const grd = g.createLinearGradient(0, 0, W, 0);
    grd.addColorStop(0, '#6d28d9');
    grd.addColorStop(1, '#1e1b4b');
    g.fillStyle = grd;
    g.fillRect(0, 0, W, hh);
    label(g, 'BET SLIP', 22, hh / 2 + 1, hh * 0.5, '#ffffff', { weight: 800, base: 'middle' });
    label(g, `${chipsText(this.ctx.save.chips)} chips`, W - 22, hh / 2 + 1, hh * 0.42, '#FDE68A', { weight: 800, align: 'right', base: 'middle' });
    const r = this.rows()[this.sel];
    const pad = 28;
    if (!r) {
      label(g, this.ctx.board().note ?? 'Loading Polymarket US’s sports markets…', W / 2, H / 2, 30, '#C4B5FD', { align: 'center', weight: 700, base: 'middle' });
      return out;
    }
    // the market, up to two lines
    g.font = `700 34px "Chakra Petch", sans-serif`;
    const words = r.title.split(' ');
    const lines: string[] = [''];
    for (const w of words) {
      const t = lines[lines.length - 1] ? `${lines[lines.length - 1]} ${w}` : w;
      if (g.measureText(t).width > W - pad * 2 && lines[lines.length - 1]) lines.push(w);
      else lines[lines.length - 1] = t;
    }
    lines.slice(0, 2).forEach((l, i) => label(g, i === 1 && lines.length > 2 ? fit(g, `${l}…`, W - pad * 2) : l, pad, hh + 44 + i * 40, 34, '#ffffff', { weight: 700, base: 'middle' }));
    label(g, `closes ${closesIn(r.hoursToClose)} · Polymarket US's live price`, pad, hh + 126, 20, 'rgba(255,255,255,0.55)', { mono: true, weight: 500, base: 'middle' });
    // the two sides
    const half = (W - pad * 2 - 20) / 2, sy = hh + 156, sh = 120;
    const sideBtn = (id: 'sideA' | 'sideB', x: number, name: string, c: number | null, s: 'yes' | 'no') =>
      button(g, { ...v, u: 2 }, out, id, x, sy, half, sh, fit(g, name, half - 20), {
        key: c === null ? 'no price' : `${c.toFixed(c % 1 ? 1 : 0)}¢ · ${americanOdds(c)}`, hot: this.side === s, enabled: c !== null,
        color: s === 'yes' ? '#4ADE80' : '#F472B6',
      });
    sideBtn('sideA', pad, r.yesLabel, r.yesCents, 'yes');
    sideBtn('sideB', pad + half + 20, r.noLabel, r.noCents, 'no');
    // the stake
    const ky = sy + sh + 36;
    label(g, 'STAKE', pad, ky, 20, 'rgba(255,255,255,0.5)', { mono: true, base: 'middle' });
    button(g, { ...v, u: 2 }, out, 'less', pad + 100, ky - 30, 70, 60, '−', { small: true, color: '#C084FC' });
    label(g, chipsText(this.stake), pad + 250, ky, 44, '#FDE68A', { align: 'center', weight: 800, base: 'middle' });
    button(g, { ...v, u: 2 }, out, 'more', pad + 330, ky - 30, 70, 60, '+', { small: true, color: '#C084FC' });
    const price = this.side === 'yes' ? r.yesCents : r.noCents;
    if (price !== null) {
      label(g, `returns ${chipsText(ticketReturn(this.stake, price))}`, W - pad, ky - 12, 30, '#C4B5FD', { align: 'right', weight: 800, base: 'middle' });
      label(g, `if ${this.side === 'yes' ? r.yesLabel : r.noLabel} wins`, W - pad, ky + 20, 18, 'rgba(196,181,253,0.7)', { align: 'right', mono: true, base: 'middle' });
    }
    button(g, { ...v, u: 2 }, out, 'place', pad, H - 100, W - pad * 2, 80, 'PLACE TICKET', {
      key: 'Enter', hot: true, enabled: price !== null && this.ctx.save.chips >= this.stake, color: '#A855F7',
    });
    if (this.msg) banner(g, { ...v, u: 1.25 }, { x: pad, y: H - 160, w: W - pad * 2, h: 60 }, this.msg.text, this.msg.color, this.msg.age);
    return out;
  }

  /** The right monitor: your bets. */
  paintBetsScreen(g: CanvasRenderingContext2D, v: View): Btn[] {
    paintBets(g, v.W, v.H, this.ctx.save.tickets, this.rows(), 7);
    return [];
  }

  close(): void { /* tickets live on in the wallet */ }
}

// ── the cashier ────────────────────────────────────────────────────────────

export class CashierSession implements PaintedSession {
  readonly kind = 'cashier' as const;
  private msg: { text: string; color: string; age: number } | null = null;

  constructor(private ctx: CasinoCtx, private agents: () => { name: string; color: string; chips: number }[]) {}

  private refill(): void {
    if (!canRefill(this.ctx.save.chips)) {
      this.msg = { text: `The cage tops up stacks under ${REFILL_BELOW}`, color: '#FCA5A5', age: 0 };
      return;
    }
    this.ctx.save.chips = START_CHIPS;
    this.ctx.save.refills++;
    this.ctx.persist();
    this.msg = { text: `Here you go: ${chipsText(START_CHIPS)} chips`, color: '#4ADE80', age: 0 };
  }

  update(dt: number): void { if (this.msg) this.msg.age += dt; }

  hint(): string {
    return 'Ring the bell (or R) for a refill under 50 · Esc or move to leave';
  }

  key(code: string): boolean {
    if (code === 'KeyR' || code === 'Enter' || code === 'Space') { this.refill(); return true; }
    return false;
  }

  click(id: string): void { if (id === 'refill') this.refill(); }

  paint(g: CanvasRenderingContext2D, v: View): Btn[] {
    const { u } = v;
    const out: Btn[] = [];
    const s = this.ctx.save;
    const b = panel(g, v, {
      title: 'Cashier', sub: 'play chips only — they buy nothing and are worth nothing, in this browser profile alone',
      color: '#4ADE80', h: 300, chips: s.chips, hint: 'R refill (under 50)',
    });
    const net = s.stats.returned - s.stats.staked;
    const rows: [string, string][] = [
      ['rounds played', chipsText(s.stats.rounds)],
      ['rounds won', chipsText(s.stats.won)],
      ['chips staked', chipsText(s.stats.staked)],
      ['chips back', chipsText(s.stats.returned)],
      ['net', signed(net)],
      ['refills', chipsText(s.refills)],
      ['open tickets', chipsText(s.tickets.filter((t) => t.status === 'open').length)],
    ];
    rows.forEach(([k, val], i) => {
      label(g, k, b.x, b.y + 8 * u + i * 24 * u, 13 * u, 'rgba(255,255,255,0.6)', { mono: true, weight: 500 });
      label(g, val, b.x + 230 * u, b.y + 8 * u + i * 24 * u, 15 * u, k === 'net' ? (net >= 0 ? '#4ADE80' : '#F87171') : '#ffffff', { align: 'right', weight: 700 });
    });
    const ax = b.x + 290 * u;
    label(g, "YOUR AGENTS' STACKS", ax, b.y + 8 * u, 11 * u, 'rgba(255,255,255,0.5)', { mono: true });
    const ag = this.agents();
    if (!ag.length) label(g, 'none on the floor yet', ax, b.y + 32 * u, 13 * u, 'rgba(255,255,255,0.45)', { weight: 500 });
    ag.slice(0, 7).forEach((a, i) => {
      g.fillStyle = a.color;
      g.beginPath(); g.arc(ax + 6 * u, b.y + 32 * u + i * 24 * u, 5 * u, 0, Math.PI * 2); g.fill();
      label(g, a.name, ax + 18 * u, b.y + 32 * u + i * 24 * u, 14 * u, '#ffffff', { weight: 600 });
      label(g, chipsText(a.chips), ax + 250 * u, b.y + 32 * u + i * 24 * u, 14 * u, '#FDE68A', { align: 'right', weight: 700 });
    });
    button(g, v, out, 'refill', b.x + b.w - 240 * u, b.y + 8 * u, 240 * u, 56 * u, canRefill(s.chips) ? `Get ${chipsText(START_CHIPS)} chips` : `Refills under ${REFILL_BELOW}`, {
      key: 'R', hot: canRefill(s.chips), enabled: canRefill(s.chips), color: '#4ADE80',
    });
    if (this.msg) banner(g, v, { ...b, w: b.w - 260 * u }, this.msg.text, this.msg.color, this.msg.age);
    return out;
  }

  close(): void { /* nothing on the counter */ }
}

/** How long a cocktail takes to shake. */
export const MIX_S = 2.4;

const GARNISH_COLOR: Record<Garnish, string> = { olive: '#6a8f2a', cherry: '#c8102e', peel: '#f39c12', lime: '#7ac943' };

/** The club's bar: pick a cocktail, pay in play chips, the bartender shakes
 *  it, and it lands in your hotbar's sixth slot. */
export class BarSession implements PaintedSession {
  readonly kind = 'bar' as const;
  private sel = 0;
  private mixing: { drink: Cocktail; t: number } | null = null;
  private msg: { text: string; color: string; age: number } | null = null;

  constructor(private ctx: CasinoCtx, private bartender: string, private hooks: { mix(): void; serve(c: Cocktail): void }) {}

  hint(): string {
    return 'Click a drink on the menu, then Order · or 1-6 and Enter · Esc or move to leave';
  }

  wheel(dir: 1 | -1): void {
    if (!this.mixing) this.sel = (this.sel + COCKTAILS.length - dir) % COCKTAILS.length;
  }

  private order(): void {
    if (this.mixing) return;
    const r = orderCocktail(this.ctx.save, COCKTAILS[this.sel].id);
    if (!r.ok) {
      this.msg = { text: r.why, color: '#FCA5A5', age: 0 };
      return;
    }
    this.ctx.persist();
    this.mixing = { drink: r.drink, t: 0 };
    this.msg = null;
    this.hooks.mix();
  }

  update(dt: number): void {
    if (this.msg) this.msg.age += dt;
    const m = this.mixing;
    if (!m) return;
    m.t += dt;
    if (m.t < MIX_S) return;
    this.mixing = null;
    this.hooks.serve(m.drink);
    this.msg = { text: `Your ${m.drink.name}. It's in your hotbar: 6`, color: '#4ADE80', age: 0 };
  }

  key(code: string): boolean {
    if (/^Digit[1-6]$/.test(code)) { this.sel = Math.min(COCKTAILS.length - 1, Number(code.slice(5)) - 1); return true; }
    if (code === 'Enter' || code === 'Space') { this.order(); return true; }
    return false;
  }

  click(id: string): void {
    if (id === 'order') this.order();
    else if (id.startsWith('c:')) this.sel = Number(id.slice(2));
  }

  paint(g: CanvasRenderingContext2D, v: View): Btn[] {
    const { u } = v;
    const out: Btn[] = [];
    const b = panel(g, v, {
      title: 'The Bar', sub: `${this.bartender} is pouring · cocktails for play chips · yours land in hotbar slot 6`,
      color: '#FF5FAE', h: 372, chips: this.ctx.save.chips, hint: '1-6 or the wheel pick · Enter order',
    });
    // the menu
    const rowH = 38 * u, colW = Math.min(360 * u, b.w * 0.48);
    COCKTAILS.forEach((c, i) => {
      const y = b.y + i * (rowH + 6 * u);
      button(g, v, out, `c:${i}`, b.x, y, colW, rowH, `${c.name} · ${c.price}`, {
        small: true, hot: i === this.sel, color: c.color, enabled: !this.mixing,
      });
    });
    // the drink: its glass, big, and what's in it
    const c = COCKTAILS[this.sel];
    const rx = b.x + colW + 30 * u;
    const px = 7 * u;
    drawIcon(g, 'cocktail', rx, b.y - 4 * u, px, { l: c.color, o: GARNISH_COLOR[c.garnish] });
    const tx = rx + 16 * px + 20 * u;
    label(g, c.name, tx, b.y + 18 * u, 22 * u, '#ffffff', { weight: 800 });
    label(g, c.recipe, tx, b.y + 46 * u, 13 * u, 'rgba(255,255,255,0.65)', { mono: true, weight: 500 });
    label(g, `${c.price} chips`, tx, b.y + 76 * u, 16 * u, '#FDE68A', { weight: 700 });
    // the kick, in pips
    label(g, 'KICK', tx, b.y + 104 * u, 11 * u, 'rgba(255,255,255,0.5)', { mono: true });
    for (let k = 0; k < 5; k++) {
      rr(g, tx + 46 * u + k * 18 * u, b.y + 97 * u, 14 * u, 14 * u, 3 * u);
      g.fillStyle = k < Math.round(c.strength * 10) - 1 ? c.color : 'rgba(255,255,255,0.12)';
      g.fill();
    }
    const by = b.y + 16 * px + 8 * u;
    if (this.mixing) {
      const k = Math.min(1, this.mixing.t / MIX_S);
      label(g, `${this.bartender} is shaking your ${this.mixing.drink.name}…`, rx, by + 14 * u, 15 * u, '#ffffff', { weight: 600 });
      rr(g, rx, by + 32 * u, b.x + b.w - rx, 10 * u, 5 * u);
      g.fillStyle = 'rgba(255,255,255,0.1)';
      g.fill();
      rr(g, rx, by + 32 * u, (b.x + b.w - rx) * k, 10 * u, 5 * u);
      g.fillStyle = this.mixing.drink.color;
      g.fill();
    } else {
      const can = this.ctx.save.chips >= c.price;
      button(g, v, out, 'order', rx, by, Math.min(300 * u, b.x + b.w - rx), 56 * u, can ? `Order · ${c.price} chips` : 'Not enough chips', {
        key: 'Enter', hot: can, enabled: can, color: '#FF5FAE',
      });
    }
    if (this.msg) banner(g, v, { x: rx, y: b.y + b.h - 50 * u, w: b.x + b.w - rx, h: 44 * u }, this.msg.text, this.msg.color, this.msg.age);
    return out;
  }

  close(): void { /* the glass goes with you */ }
}
