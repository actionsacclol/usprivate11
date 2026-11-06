import { describe, expect, it } from 'vitest';
import {
  Shoe, autoPlay, basicStrategy, canDouble, canSplit, deal, double, handTotal, hit, isBlackjack, split, stand,
  type BjRound, type Card,
} from '../src/hub/casino/blackjack';
import { WHEEL_ORDER, colorOf, payoutMultiplier, settle, wins, type RouletteBet } from '../src/hub/casino/roulette';
import { STRIP, THREE_PAYS, paysFor, rtp, spinSlots } from '../src/hub/casino/slots';
import {
  americanOdds, boardRow, buildBoard, decimalOdds, settleTicket, ticketReturn, writeTicket,
} from '../src/hub/casino/sportsbook';
import { START_CHIPS, canRefill, parseSave, record } from '../src/hub/casino/wallet';
import { mulberry32 } from '../src/hub/casino/rng';

// The casino's games are play chips only. What is pinned here is that the
// rules are the real ones and the house edge is what the table says it is.

const c = (rank: number, suit: Card['suit'] = 'S'): Card => ({ rank, suit });

/** A shoe that deals exactly these cards, in order. */
function rigged(cards: Card[]): Shoe {
  const s = new Shoe(mulberry32(1), 1);
  const queue = [...cards];
  (s as unknown as { draw: () => Card }).draw = () => queue.shift() ?? c(2);
  Object.defineProperty(s, 'needsShuffle', { get: () => false });
  return s;
}

describe('blackjack hands', () => {
  it('counts an ace as 11 only while that stays under 22', () => {
    expect(handTotal([c(1), c(6)])).toEqual({ total: 17, soft: true });
    expect(handTotal([c(1), c(6), c(10)])).toEqual({ total: 17, soft: false });
    expect(handTotal([c(1), c(1), c(9)])).toEqual({ total: 21, soft: true });
    expect(isBlackjack([c(1), c(13)])).toBe(true);
    expect(isBlackjack([c(7), c(7), c(7)])).toBe(false);
  });

  it('a six-deck shoe holds 312 cards and reshuffles past the cut', () => {
    const s = new Shoe(mulberry32(7));
    expect(s.remaining).toBe(312);
    for (let i = 0; i < 240; i++) s.draw();
    expect(s.needsShuffle).toBe(true);
  });
});

describe('blackjack rounds', () => {
  it('a natural pays 3:2 at once', () => {
    // player A, dealer 9, player K, dealer 7
    const r = deal(rigged([c(1), c(9), c(13), c(7)]), 10);
    expect(r.phase).toBe('done');
    expect(r.hands[0].result).toBe('blackjack');
    expect(r.returned).toBe(25);
  });

  it('the dealer peeks: a dealer natural under an ace beats 20 before anyone acts', () => {
    const r = deal(rigged([c(10), c(1), c(10), c(13)]), 10);
    expect(r.phase).toBe('done');
    expect(r.hands[0].result).toBe('lose');
    expect(r.returned).toBe(0);
  });

  it('naturals both sides push', () => {
    const r = deal(rigged([c(1), c(1), c(12), c(10)]), 10);
    expect(r.hands[0].result).toBe('push');
    expect(r.returned).toBe(10);
  });

  it('the dealer stands on soft 17', () => {
    // player 10+8, dealer 6 + A (soft 17)
    const shoe = rigged([c(10), c(6), c(8), c(1)]);
    const r = deal(shoe, 10);
    stand(r, shoe);
    expect(r.dealer).toHaveLength(2);
    expect(r.hands[0].result).toBe('win');
    expect(r.returned).toBe(20);
  });

  it('a bust loses without the dealer drawing', () => {
    const shoe = rigged([c(10), c(5), c(6), c(10), c(9)]);
    const r = deal(shoe, 10);
    hit(r, shoe);
    expect(r.hands[0].result).toBe('bust');
    expect(r.dealer).toHaveLength(2);
    expect(r.returned).toBe(0);
  });

  it('a double stakes twice and takes exactly one card', () => {
    // player 6+5 = 11, dealer 6 + 10, double draws 10 → 21; dealer 16 draws 9 → bust
    const shoe = rigged([c(6), c(6), c(5), c(10), c(10), c(9)]);
    const r = deal(shoe, 10);
    expect(canDouble(r)).toBe(true);
    double(r, shoe);
    expect(r.hands[0].cards).toHaveLength(3);
    expect(r.staked).toBe(20);
    expect(r.returned).toBe(40);
  });

  it('a split makes two hands of the same bet, each settled on its own', () => {
    // player 8,8 vs dealer 10,7 (17). Hand 1: 8+3+10 = 21 wins; hand 2: 8+10 = 18 wins.
    const shoe = rigged([c(8), c(10), c(8, 'H'), c(7), c(3), c(10), c(10)]);
    const r = deal(shoe, 10);
    expect(canSplit(r)).toBe(true);
    split(r, shoe);
    expect(r.hands).toHaveLength(2);
    expect(r.staked).toBe(20);
    hit(r, shoe); // 8+3 → 11, +10 → 21 done
    hit(r, shoe);
    stand(r, shoe); // second hand 8+10 = 18
    expect(r.phase).toBe('done');
    expect(r.hands.map((h) => h.result)).toEqual(['win', 'win']);
    expect(r.returned).toBe(40);
  });

  it('21 on a split hand is not a blackjack', () => {
    const shoe = rigged([c(1), c(9), c(1, 'H'), c(8), c(13), c(13)]);
    const r: BjRound = deal(shoe, 10);
    split(r, shoe);
    expect(r.phase).toBe('done');
    expect(r.hands.every((h) => h.result === 'win')).toBe(true);
    expect(r.returned).toBe(40); // 1:1 each, not 3:2
  });

  it('basic strategy doubles 11, stands 13 against a 6, hits 16 against a 10, splits aces', () => {
    const can = { canDouble: true, canSplit: true };
    expect(basicStrategy([c(6), c(5)], c(10), can)).toBe('double');
    expect(basicStrategy([c(10), c(3)], c(6), can)).toBe('stand');
    expect(basicStrategy([c(10), c(6)], c(10), can)).toBe('hit');
    expect(basicStrategy([c(1), c(1, 'H')], c(9), can)).toBe('split');
    expect(basicStrategy([c(6), c(5)], c(10), { canDouble: false, canSplit: false })).toBe('hit');
  });

  it('over many rounds the house keeps a small edge on basic strategy', () => {
    const shoe = new Shoe(mulberry32(42));
    let staked = 0, returned = 0;
    for (let i = 0; i < 40000; i++) {
      const r = autoPlay(shoe, 10, () => true);
      staked += r.staked;
      returned += r.returned;
    }
    const edge = 1 - returned / staked;
    expect(edge).toBeGreaterThan(-0.02);
    expect(edge).toBeLessThan(0.03);
  });
});

describe('roulette', () => {
  it('is the real single-zero wheel', () => {
    expect(WHEEL_ORDER).toHaveLength(37);
    expect(new Set(WHEEL_ORDER).size).toBe(37);
    expect(colorOf(0)).toBe('green');
    expect(colorOf(32)).toBe('red');
    expect(colorOf(15)).toBe('black');
    // red and black alternate round the wheel after the zero
    for (let i = 1; i < 36; i++) expect(colorOf(WHEEL_ORDER[i])).not.toBe(colorOf(WHEEL_ORDER[i + 1]));
  });

  it('pays 35:1 on a number, 2:1 on a dozen, evens on red; zero beats the outside', () => {
    expect(settle([{ bet: { kind: 'straight', n: 17 }, stake: 10 }], 17)).toBe(360);
    expect(settle([{ bet: { kind: 'dozen', n: 2 }, stake: 10 }], 17)).toBe(30);
    expect(settle([{ bet: { kind: 'red' }, stake: 10 }], 1)).toBe(20);
    expect(settle([{ bet: { kind: 'red' }, stake: 10 }, { bet: { kind: 'even' }, stake: 10 }], 0)).toBe(0);
    expect(wins({ kind: 'column', n: 1 }, 34)).toBe(true);
    expect(wins({ kind: 'high' }, 19)).toBe(true);
  });

  it('every bet carries the same 1/37 house edge', () => {
    const bets: RouletteBet[] = [
      { kind: 'straight', n: 5 }, { kind: 'red' }, { kind: 'odd' }, { kind: 'low' }, { kind: 'dozen', n: 3 }, { kind: 'column', n: 2 },
    ];
    for (const b of bets) {
      let back = 0;
      for (let n = 0; n <= 36; n++) if (wins(b, n)) back += 1 + payoutMultiplier(b);
      expect(back / 37).toBeCloseTo(36 / 37, 10);
    }
  });
});

describe('slots', () => {
  it('pays the table: three of a kind, or two cherries', () => {
    expect(paysFor(['seven', 'seven', 'seven'])).toBe(THREE_PAYS.seven);
    expect(paysFor(['cherry', 'bar', 'cherry'])).toBe(2);
    expect(paysFor(['cherry', 'bar', 'bell'])).toBe(0);
  });

  it('returns 94% to the player: under 100, and what the machine prints', () => {
    const r = rtp();
    expect(r).toBeGreaterThan(0.9);
    expect(r).toBeLessThan(0.97);
    expect(Math.round(r * 100)).toBe(94);
  });

  it('a spin lands on stops of the strip', () => {
    const rng = mulberry32(3);
    for (let i = 0; i < 50; i++) {
      const s = spinSlots(rng);
      s.stops.forEach((st, k) => expect(STRIP[st]).toBe(s.line[k]));
    }
  });
});

describe('the sportsbook board', () => {
  const live = {
    conditionId: '0xabc', title: 'Lakers vs. Celtics', outcomes: ['Lakers', 'Celtics'] as [string, string],
    bid: 38, ask: 40, volume24hUsd: 50_000, hoursToClose: 5, status: 'open',
  };

  it('prices a side from the real book: YES at the ask, NO at 100 − bid', () => {
    const r = boardRow(live)!;
    expect(r.yesCents).toBe(40);
    expect(r.noCents).toBe(62);
    expect(r.yesLabel).toBe('Lakers');
    expect(r.noLabel).toBe('Celtics');
  });

  it('a market with no price, settled or past its close is not on the board', () => {
    expect(boardRow({ ...live, bid: null, ask: null })).toBeNull();
    expect(boardRow({ ...live, resolvedOutcome: 'yes' })).toBeNull();
    expect(boardRow({ ...live, hoursToClose: -1 })).toBeNull();
    expect(boardRow({ ...live, status: 'closed' })).toBeNull();
  });

  it('the board is deduplicated: this week\'s games soonest first, then the rest busiest first', () => {
    const b = buildBoard([
      { ...live, conditionId: '0xfut', hoursToClose: 2000, volume24hUsd: 5 },
      { ...live, conditionId: '0xbig', hoursToClose: 900, volume24hUsd: 99_000 },
      { ...live, hoursToClose: 30 },
      { ...live, hoursToClose: 30 },
      { ...live, conditionId: '0xnow', hoursToClose: 2 },
    ]);
    expect(b.map((r) => r.conditionId)).toEqual(['0xnow', '0xabc', '0xbig', '0xfut']);
  });

  it('long shots on both sides are left off: a board is for contests', () => {
    expect(buildBoard([{ ...live, bid: 99.7, ask: 99.9 }])).toEqual([]);
    expect(buildBoard([{ ...live, bid: 0.1, ask: 0.3 }])).toEqual([]);
    expect(buildBoard([{ ...live, bid: 2, ask: 4 }])).toHaveLength(1);
  });

  it('odds are the price, read two ways', () => {
    expect(decimalOdds(40)).toBe(2.5);
    expect(americanOdds(40)).toBe('+150');
    expect(americanOdds(60)).toBe('−150');
  });

  it('a ticket pays at its own price, and only on the real result', () => {
    const t = writeTicket(boardRow(live)!, 'yes', 100, 1)!;
    expect(t.priceCents).toBe(40);
    expect(settleTicket(t, { resolvedOutcome: null, status: 'open' }).status).toBe('open');
    const won = settleTicket(t, { resolvedOutcome: 'yes' });
    expect(won.status).toBe('won');
    expect(won.paid).toBe(ticketReturn(100, 40));
    expect(won.paid).toBe(250);
    expect(settleTicket(t, { resolvedOutcome: 'no' })).toMatchObject({ status: 'lost', paid: 0 });
  });

  it('a side nobody offers cannot be written', () => {
    expect(writeTicket({ ...boardRow(live)!, noCents: null }, 'no', 10, 1)).toBeNull();
  });
});

describe('the chip wallet', () => {
  it('a broken or missing save starts at the table stake', () => {
    expect(parseSave(null).chips).toBe(START_CHIPS);
    expect(parseSave('{nope').chips).toBe(START_CHIPS);
    expect(parseSave(JSON.stringify({ chips: -5 })).chips).toBe(START_CHIPS);
  });

  it('keeps well-formed tickets and agent stacks, drops the rest', () => {
    const s = parseSave(JSON.stringify({
      chips: 420.7, agents: { 'agent|sam': 900, bad: 'x' },
      tickets: [{ id: 'a', conditionId: '0x1', side: 'yes', priceCents: 40, stake: 10, status: 'open' }, { id: 1 }],
    }));
    expect(s.chips).toBe(420);
    expect(s.agents['agent|sam']).toBe(900);
    expect(s.agents.bad).toBe(START_CHIPS);
    expect(s.tickets).toHaveLength(1);
  });

  it('the cashier tops up only a short stack; stats add up', () => {
    expect(canRefill(49)).toBe(true);
    expect(canRefill(50)).toBe(false);
    const s = parseSave(null);
    record(s, 20, 40);
    record(s, 10, 0);
    expect(s.stats).toEqual({ rounds: 2, won: 1, staked: 30, returned: 40 });
  });
});

describe('the sportsbook board, per game', () => {
  it('shows at most two markets from one match', () => {
    const row = (id: string, ev: string | null) => ({
      conditionId: id, title: id, eventSlug: ev, bid: 40, ask: 42, volume24hUsd: 1, hoursToClose: 3, status: 'open',
    });
    const b = buildBoard([row('a', 'm1'), row('b', 'm1'), row('c', 'm1'), row('d', 'm2'), row('e', null)]);
    expect(b.map((r) => r.conditionId)).toEqual(['a', 'b', 'd', 'e']);
  });
});
