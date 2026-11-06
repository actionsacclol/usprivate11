import { describe, expect, it } from 'vitest';
import { Shoe } from '../src/hub/casino/blackjack';
import { mulberry32 } from '../src/hub/casino/rng';
import { BlackjackSession, RouletteSession, SportsbookSession, betFromKey, betKey, type CasinoCtx } from '../src/hub/casino/sessions';
import { markToMarket } from '../src/hub/casino/sportsbookScreens';
import type { BoardRow } from '../src/hub/casino/sportsbook';
import { chipsFor } from '../src/hub/engine/tables/common';

// The tables you play by clicking the table itself (engine/tables/*.ts):
// the sessions behind them, without a GPU. What a click on a chip, a cell of
// the felt or a plaque does to your stack.

function ctx(chips = 1000): CasinoCtx {
  const rng = mulberry32(7);
  const shoe = new Shoe(rng);
  return {
    save: { chips, refills: 0, tickets: [], stats: { rounds: 0, won: 0, staked: 0, returned: 0 }, agents: {} } as unknown as CasinoCtx['save'],
    persist: () => {},
    rng,
    shoe: () => shoe,
    bjCards: () => {},
    dealerDeals: () => {},
    spinWheel: () => {},
    wheelBusy: () => false,
    wheelJoin: () => {},
    spinSlot: () => {},
    slotWin: () => {},
    board: () => ({ rows: [], at: null, note: null }),
    brag: () => {},
  };
}

describe('chips on the felt', () => {
  it('an amount is stacked in the biggest chips first', () => {
    expect(chipsFor(630)).toEqual([500, 100, 25, 5]);
    expect(chipsFor(0)).toEqual([]);
    expect(chipsFor(1000).length).toBe(1);
  });
});

describe('blackjack, by clicking chips and plaques', () => {
  it('your bet is the chips you put in the circle', () => {
    const s = new BlackjackSession(ctx(), 0);
    s.click('chip:100');
    s.click('chip:5');
    expect(s.bet).toBe(25 + 100 + 5);
    s.click('bet'); // click the stack: one back
    expect(s.bet).toBe(125);
  });

  it('you can’t bet more than you have', () => {
    const s = new BlackjackSession(ctx(60), 0);
    s.click('chip:500');
    expect(s.bet).toBe(25);
  });

  it('dealing takes the bet; the plaques light for what you can do', () => {
    const c = ctx();
    const s = new BlackjackSession(c, 0);
    expect(s.view().can.deal).toBe(true);
    expect(s.view().can.hit).toBe(false);
    s.click('deal');
    expect(c.save.chips).toBe(975);
    // the cards land one by one; then it's your move (or the hand's done)
    for (let i = 0; i < 40; i++) s.update(0.1);
    const v = s.view();
    expect(['play', 'done']).toContain(v.phase);
    if (v.phase === 'play') {
      expect(v.can.hit && v.can.stand).toBe(true);
      expect(v.can.bet).toBe(false);
      expect(v.tip).not.toBeNull();
    }
  });
});

describe('roulette, by clicking the felt', () => {
  it('every spot on the felt has a key and back', () => {
    for (let n = 0; n <= 36; n++) expect(betKey(betFromKey(`n${n}`)!)).toBe(`n${n}`);
    for (const k of ['red', 'black', 'odd', 'even', 'low', 'high', 'd1', 'd2', 'd3', 'c1', 'c2', 'c3']) expect(betKey(betFromKey(k)!)).toBe(k);
    expect(betFromKey('n37')).toBeNull();
    expect(betFromKey('purple')).toBeNull();
  });

  it('a click puts the chip in your hand on that spot; undo takes the last back', () => {
    const c = ctx();
    const s = new RouletteSession(c);
    s.click('chip:100');
    s.click('bet:n17');
    s.click('bet:n17');
    s.click('bet:red');
    expect(s.stakeOn('n17')).toBe(200);
    expect(s.stakeOn('red')).toBe(100);
    expect(c.save.chips).toBe(700);
    s.click('undo');
    expect(s.stakeOn('red')).toBe(0);
    expect(c.save.chips).toBe(800);
  });

  it('the wheel steps the chip in your hand', () => {
    const s = new RouletteSession(ctx());
    s.click('chip:5');
    s.wheel(1);
    expect(s.stake).toBe(25);
    s.wheel(-1);
    s.wheel(-1);
    expect(s.stake).toBe(5);
  });

  it('after a spin, the felt knows what each bet did', () => {
    const c = ctx();
    const s = new RouletteSession(c);
    s.click('bet:red');
    s.click('bet:black');
    s.key('Space');
    for (let i = 0; i < 80; i++) s.update(0.1);
    const o = s.outcome!;
    expect(o).not.toBeNull();
    expect(o.bets.map((b) => b.key).sort()).toEqual(['black', 'red']);
    const back = o.bets.reduce((a, b) => a + b.back, 0);
    expect(c.save.chips).toBe(1000 - 50 + back);
  });
});

describe('the sportsbook station', () => {
  const row = (id: string, yes: number | null, no: number | null): BoardRow => ({
    conditionId: id, title: `Market ${id}`, event: null, yesLabel: 'Home', noLabel: 'Away', yesCents: yes, noCents: no, volume24hUsd: 1e5, hoursToClose: 20,
  });

  it('click a market, a side, the stake, place: a ticket at the live price', () => {
    const c = ctx();
    const rows = [row('a', 40, 62), row('b', 70, 31)];
    c.board = () => ({ rows, at: Date.now(), note: null });
    const s = new SportsbookSession(c);
    s.click('row:1');
    s.click('sideB');
    s.click('more');
    s.update(0.1);
    s.click('place');
    const t = c.save.tickets[0];
    expect(t.conditionId).toBe('b');
    expect(t.side).toBe('no');
    expect(t.priceCents).toBe(31);
    expect(c.save.chips).toBe(1000 - t.stake);
  });

  it('your bets are marked to the price now', () => {
    const t = { id: 't', conditionId: 'a', title: 'x', side: 'yes' as const, label: 'Home', priceCents: 40, stake: 100, placedAt: 0, status: 'open' as const, paid: 0 };
    expect(markToMarket(t, [row('a', 50, 52)])).toEqual({ now: 50, value: 125 });
    expect(markToMarket(t, [row('z', 50, 52)])).toBeNull();
  });
});
