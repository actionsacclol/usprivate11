import { randInt, type Rng } from './rng';

// Blackjack, Las Vegas Strip rules: a six-deck shoe, the dealer stands on
// every 17, peeks for blackjack under an ace or a ten, blackjack pays 3:2,
// double on any first two cards, one split (split aces take one card each,
// and 21 on a split hand is not a blackjack). Play chips only.

export type Suit = 'S' | 'H' | 'D' | 'C';
export interface Card { rank: number; suit: Suit }

export const SUIT_GLYPH: Record<Suit, string> = { S: '♠', H: '♥', D: '♦', C: '♣' };

export function rankLabel(rank: number): string {
  return rank === 1 ? 'A' : rank === 11 ? 'J' : rank === 12 ? 'Q' : rank === 13 ? 'K' : String(rank);
}

export function isRed(c: Card): boolean {
  return c.suit === 'H' || c.suit === 'D';
}

/** Ace counts 1 here; handTotal decides when it is 11. */
export function cardValue(c: Card): number {
  return c.rank >= 10 ? 10 : c.rank;
}

export function handTotal(cards: readonly Card[]): { total: number; soft: boolean } {
  let total = 0;
  let aces = 0;
  for (const c of cards) {
    total += cardValue(c);
    if (c.rank === 1) aces++;
  }
  const soft = aces > 0 && total + 10 <= 21;
  return { total: soft ? total + 10 : total, soft };
}

export function isBlackjack(cards: readonly Card[]): boolean {
  return cards.length === 2 && handTotal(cards).total === 21;
}

/** A six-deck shoe with a cut card at three quarters: past it, the next
 *  round starts from a fresh shuffle. */
export class Shoe {
  private cards: Card[] = [];
  private cut = 0;
  constructor(private rng: Rng, private decks = 6) {
    this.shuffle();
  }

  shuffle(): void {
    const out: Card[] = [];
    for (let d = 0; d < this.decks; d++) {
      for (const suit of ['S', 'H', 'D', 'C'] as Suit[]) for (let r = 1; r <= 13; r++) out.push({ rank: r, suit });
    }
    for (let i = out.length - 1; i > 0; i--) {
      const j = randInt(this.rng, i + 1);
      [out[i], out[j]] = [out[j], out[i]];
    }
    this.cards = out;
    this.cut = Math.floor(out.length * 0.25);
  }

  /** Called between rounds, never mid-hand. */
  get needsShuffle(): boolean {
    return this.cards.length <= this.cut;
  }

  draw(): Card {
    if (!this.cards.length) this.shuffle();
    return this.cards.pop()!;
  }

  get remaining(): number {
    return this.cards.length;
  }
}

export type HandResult = 'blackjack' | 'win' | 'push' | 'lose' | 'bust';

export interface BjHand {
  cards: Card[];
  bet: number;
  doubled: boolean;
  done: boolean;
  /** Born of a split: 21 is not a blackjack, and aces stop at one card. */
  split: boolean;
  result: HandResult | null;
}

export type BjPhase = 'player' | 'done';

export interface BjRound {
  dealer: Card[];
  hands: BjHand[];
  /** The hand being played. */
  active: number;
  phase: BjPhase;
  /** The dealer's second card is face down until the players are done. */
  holeHidden: boolean;
  /** Chips staked in total (bets, doubles, splits). */
  staked: number;
  /** Chips the round returns, stakes included (0 = everything lost). */
  returned: number;
}

function newHand(bet: number, split = false): BjHand {
  return { cards: [], bet, doubled: false, done: false, split, result: null };
}

/** Deal a round: player, dealer, player, dealer (the hole card). A natural on
 *  either side settles at once (the dealer peeks under an ace or a ten). */
export function deal(shoe: Shoe, bet: number): BjRound {
  if (shoe.needsShuffle) shoe.shuffle();
  const hand = newHand(bet);
  const dealer: Card[] = [];
  hand.cards.push(shoe.draw());
  dealer.push(shoe.draw());
  hand.cards.push(shoe.draw());
  dealer.push(shoe.draw());
  const r: BjRound = { dealer, hands: [hand], active: 0, phase: 'player', holeHidden: true, staked: bet, returned: 0 };
  const peek = cardValue(dealer[0]) === 10 || dealer[0].rank === 1;
  if (isBlackjack(hand.cards) || (peek && isBlackjack(dealer))) {
    hand.done = true;
    finish(r, shoe, false);
  }
  return r;
}

export function activeHand(r: BjRound): BjHand | null {
  return r.phase === 'player' ? r.hands[r.active] ?? null : null;
}

export function canDouble(r: BjRound): boolean {
  const h = activeHand(r);
  return !!h && h.cards.length === 2 && !h.doubled && !(h.split && h.cards[0].rank === 1);
}

export function canSplit(r: BjRound): boolean {
  const h = activeHand(r);
  return !!h && r.hands.length === 1 && h.cards.length === 2 && cardValue(h.cards[0]) === cardValue(h.cards[1]);
}

function advance(r: BjRound, shoe: Shoe): void {
  while (r.active < r.hands.length && r.hands[r.active].done) r.active++;
  if (r.active >= r.hands.length) finish(r, shoe, true);
  else {
    // a split hand gets its second card when its turn comes
    const h = r.hands[r.active];
    if (h.cards.length === 1) {
      h.cards.push(shoe.draw());
      if (h.split && h.cards[0].rank === 1) h.done = true; // split aces: one card
      if (handTotal(h.cards).total === 21) h.done = true;
      if (h.done) advance(r, shoe);
    }
  }
}

export function hit(r: BjRound, shoe: Shoe): void {
  const h = activeHand(r);
  if (!h) return;
  h.cards.push(shoe.draw());
  const t = handTotal(h.cards).total;
  if (t >= 21) h.done = true;
  if (h.done) advance(r, shoe);
}

export function stand(r: BjRound, shoe: Shoe): void {
  const h = activeHand(r);
  if (!h) return;
  h.done = true;
  advance(r, shoe);
}

/** Double the bet, take exactly one card. The caller has already taken the
 *  extra chips (it knows the balance; this module does not). */
export function double(r: BjRound, shoe: Shoe): void {
  const h = activeHand(r);
  if (!h || !canDouble(r)) return;
  r.staked += h.bet;
  h.bet *= 2;
  h.doubled = true;
  h.cards.push(shoe.draw());
  h.done = true;
  advance(r, shoe);
}

/** One split into two hands of the same bet. Caller takes the extra chips. */
export function split(r: BjRound, shoe: Shoe): void {
  const h = activeHand(r);
  if (!h || !canSplit(r)) return;
  const second = newHand(h.bet, true);
  second.cards.push(h.cards.pop()!);
  h.split = true;
  r.staked += h.bet;
  r.hands.push(second);
  h.cards.push(shoe.draw());
  if (h.cards[0].rank === 1 || handTotal(h.cards).total === 21) h.done = true;
  if (h.done) advance(r, shoe);
}

/** The dealer draws to 17 (stands on soft 17 too), then every hand settles. */
function finish(r: BjRound, shoe: Shoe, dealerPlays: boolean): void {
  r.holeHidden = false;
  const live = r.hands.some((h) => handTotal(h.cards).total <= 21 && !(isBlackjack(h.cards) && !h.split));
  if (dealerPlays && live) {
    while (handTotal(r.dealer).total < 17) r.dealer.push(shoe.draw());
  }
  const d = handTotal(r.dealer).total;
  const dBj = isBlackjack(r.dealer);
  let returned = 0;
  for (const h of r.hands) {
    const p = handTotal(h.cards).total;
    const pBj = isBlackjack(h.cards) && !h.split;
    let res: HandResult;
    if (p > 21) res = 'bust';
    else if (pBj && dBj) res = 'push';
    else if (pBj) res = 'blackjack';
    else if (dBj) res = 'lose';
    else if (d > 21 || p > d) res = 'win';
    else if (p === d) res = 'push';
    else res = 'lose';
    h.result = res;
    h.done = true;
    if (res === 'blackjack') returned += h.bet + Math.floor(h.bet * 1.5);
    else if (res === 'win') returned += h.bet * 2;
    else if (res === 'push') returned += h.bet;
  }
  r.returned = returned;
  r.phase = 'done';
}

export type BjMove = 'hit' | 'stand' | 'double' | 'split';

/** Basic strategy for six decks, dealer stands on soft 17: what the agents
 *  play, and what the table suggests to you. */
export function basicStrategy(cards: readonly Card[], up: Card, opts: { canDouble: boolean; canSplit: boolean }): BjMove {
  const u = up.rank === 1 ? 11 : cardValue(up);
  const { total, soft } = handTotal(cards);
  if (opts.canSplit && cards.length === 2 && cardValue(cards[0]) === cardValue(cards[1])) {
    const v = cards[0].rank === 1 ? 11 : cardValue(cards[0]);
    if (v === 11 || v === 8) return 'split';
    if ((v === 9 && ![7, 10, 11].includes(u)) || ((v === 2 || v === 3 || v === 7) && u <= 7)
      || (v === 6 && u <= 6) || (v === 4 && (u === 5 || u === 6))) return 'split';
  }
  if (soft) {
    if (total >= 20) return 'stand';
    if (total === 19) return u === 6 && opts.canDouble ? 'double' : 'stand';
    if (total === 18) {
      if (u >= 2 && u <= 6) return opts.canDouble ? 'double' : 'stand';
      return u <= 8 ? 'stand' : 'hit';
    }
    if (total === 17) return u >= 3 && u <= 6 && opts.canDouble ? 'double' : 'hit';
    if (total >= 15) return u >= 4 && u <= 6 && opts.canDouble ? 'double' : 'hit';
    return u >= 5 && u <= 6 && opts.canDouble ? 'double' : 'hit';
  }
  if (total >= 17) return 'stand';
  if (total >= 13) return u <= 6 ? 'stand' : 'hit';
  if (total === 12) return u >= 4 && u <= 6 ? 'stand' : 'hit';
  if (total === 11) return opts.canDouble ? 'double' : 'hit';
  if (total === 10) return u <= 9 && opts.canDouble ? 'double' : 'hit';
  if (total === 9) return u >= 3 && u <= 6 && opts.canDouble ? 'double' : 'hit';
  return 'hit';
}

/** Play a whole round on basic strategy (the agents' tables). `afford(n)`
 *  says whether n more chips can be staked for a double or split. */
export function autoPlay(shoe: Shoe, bet: number, afford: (n: number) => boolean): BjRound {
  const r = deal(shoe, bet);
  let guard = 0;
  while (r.phase === 'player' && guard++ < 40) {
    const h = activeHand(r)!;
    const move = basicStrategy(h.cards, r.dealer[0], {
      canDouble: canDouble(r) && afford(h.bet),
      canSplit: canSplit(r) && afford(h.bet),
    });
    if (move === 'split') split(r, shoe);
    else if (move === 'double') double(r, shoe);
    else if (move === 'hit') hit(r, shoe);
    else stand(r, shoe);
  }
  return r;
}
