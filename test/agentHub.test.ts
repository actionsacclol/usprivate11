import { describe, expect, it, vi } from 'vitest';
import type { AccountSnapshot, BotPosition } from '@shared/types';
import type { HubSignal } from '../src/hub/data';
import type { StrategyWithBacktest } from '../src/hub/library';
import { PokerTable, OVERRULE_RATE, PERSUADABLE, readTheRoom, type TableHost } from '../src/hub/poker/table';
import { MEMBER_IDS, PERSONAS, line, type Features, type MemberId } from '../src/hub/poker/roster';
import { balanceOf, hudNumbers } from '../src/hub/data';
import { hasEdge, savedBacktests, shortStrategy } from '../src/hub/library';
import {
  PAYDAY_COOLDOWN_MS, PaydayTracker, paydayLine, sessionPnlOf, settlementThreshold,
} from '../src/hub/payday';
import { EMPTY_POT, addPot, countPot } from '../src/hub/pot';
import { cacheMeasureText, subject } from '../src/hub/text';

/**
 * The Agent Hub's data rules. The scenes themselves are three.js and are
 * checked by eye; what is pinned here is everything a viewer could mistake
 * for a fact about their account:
 *
 *   - PAYDAY fires on REAL events only (a big settlement or a new +$100
 *     session step), shows the real amount, never for what was already
 *     settled when the page opened, never for a dry run, at most once a
 *     minute;
 *   - the poker table's "read the room" swing is theatre, and the WHO CALLED
 *     IT? leaderboard scores each persona's own rule, never the swung bet;
 *   - the HUD and the page render a number nobody produced as absent, not 0;
 *   - the session pots count each settlement once, this session only.
 */

let nextId = 1;
function pos(over: Partial<BotPosition> = {}): BotPosition {
  const id = over.id ?? nextId++;
  return {
    id, signalSource: 'crypto', signalId: id, ticker: `0x${'1a'.repeat(32)}`, eventTicker: 'nba-lal', title: 'Will the Lakers win?',
    category: 'Sports', direction: 'yes', action: 'buy', targetContracts: 10, limitPriceCents: 50, filledContracts: 10,
    avgFillPriceCents: 50, costUsd: 5, feesUsd: 0.1, clientOrderId: '', orderId: null, status: 'filled',
    confidence: 60, edgePts: 10, signalPriceCents: 50, resolved: false, outcomeCorrect: null, settlementUsd: null,
    pnlUsd: null, markPriceCents: null, livePnlUsd: null, balanceBeforeUsd: null, network: 'mainnet',
    createdAt: '2026-10-06T10:00:00Z', lastUpdated: '2026-10-06T10:00:00Z', resolvedAt: null, error: null,
    ...over,
  };
}

function settle(p: BotPosition, pnlUsd: number, at = '2026-10-06T12:00:00Z'): BotPosition {
  return { ...p, resolved: true, outcomeCorrect: pnlUsd > 0 ? 1 : 0, pnlUsd, resolvedAt: at };
}

function account(over: Partial<AccountSnapshot> = {}): AccountSnapshot {
  return {
    cashUsd: 1000, portfolioUsd: 0, totalUsd: 1000, startBankrollUsd: 1000, roiPct: 0, realizedPnlUsd: 0,
    unrealizedPnlUsd: 0, openCostUsd: 0, feesUsd: 0, wins: 0, losses: 0, winRate: 0, pendingCount: 0, openCount: 0,
    resolvedCount: 0, totalOpened: 0,
    byEnv: { paper: { wins: 0, losses: 0, realizedPnl: 0 }, production: { wins: 0, losses: 0, realizedPnl: 0 } },
    sessionPnlUsd: 0, sessionRoiPct: 0, sessionBaselineUsd: 1000, sessionRunId: 1,
    sessionStartedAt: '2026-10-06T11:00:00Z',
    ...over,
  } as AccountSnapshot;
}

const label = (p: BotPosition) => subject(p.ticker, p.title);

describe('PAYDAY: real events only', () => {
  it('does not celebrate settlements that were already there when the page opened', () => {
    const t = new PaydayTracker();
    const old = settle(pos(), 500);
    expect(t.check([old], account(), label, 0)).toBeNull();
    expect(t.check([old], account(), label, 1_000)).toBeNull();
  });

  it('celebrates one big new settlement with its real amount', () => {
    const t = new PaydayTracker();
    const p = pos();
    t.check([p], account(), label, 0);
    const ev = t.check([settle(p, 42.5)], account(), label, 1_000);
    expect(ev).toEqual({ kind: 'settlement', amountUsd: 42.5, label: 'Lakers win' });
    expect(paydayLine(ev!)).toBe('+$42.50 · Lakers win');
  });

  it('needs max($25, 2% of the balance) from one settlement', () => {
    expect(settlementThreshold(1000)).toBe(25);
    expect(settlementThreshold(5000)).toBe(100);
    // unknown balance: the floor, not a guess
    expect(settlementThreshold(null)).toBe(25);
    const t = new PaydayTracker();
    const p = pos();
    t.check([p], account({ totalUsd: 5000 }), label, 0);
    expect(t.check([settle(p, 60)], account({ totalUsd: 5000 }), label, 1_000)).toBeNull();
  });

  it('ignores dry runs and losses', () => {
    const t = new PaydayTracker();
    const a = pos({ status: 'dry_run' });
    const b = pos();
    t.check([a, b], account(), label, 0);
    expect(t.check([settle(a, 900), settle(b, -80)], account(), label, 1_000)).toBeNull();
  });

  it('celebrates each NEW +$100 session step once, with the session P&L', () => {
    const t = new PaydayTracker();
    expect(t.check([], account({ sessionPnlUsd: 40 }), label, 0)).toBeNull();
    const ev = t.check([], account({ sessionPnlUsd: 130 }), label, 1_000);
    expect(ev).toEqual({ kind: 'milestone', amountUsd: 130, label: 'session P&L' });
    expect(paydayLine(ev!)).toBe('+$130.00 session P&L');
    // dipping back under and up again is not a new step
    t.check([], account({ sessionPnlUsd: 80 }), label, 2 * PAYDAY_COOLDOWN_MS);
    expect(t.check([], account({ sessionPnlUsd: 150 }), label, 3 * PAYDAY_COOLDOWN_MS)).toBeNull();
    expect(t.check([], account({ sessionPnlUsd: 205 }), label, 4 * PAYDAY_COOLDOWN_MS)?.kind).toBe('milestone');
  });

  it('a session P&L with no baseline behind it is unknown, not a step', () => {
    expect(sessionPnlOf(account({ sessionBaselineUsd: 0, sessionPnlUsd: 0 }))).toBeNull();
    const t = new PaydayTracker();
    t.check([], account({ sessionBaselineUsd: 0, sessionPnlUsd: 0 }), label, 0);
    // the first real reading primes the ladder; it does not fire
    expect(t.check([], account({ sessionPnlUsd: 250 }), label, 1_000)).toBeNull();
  });

  it('fires at most once a minute, and drops (not queues) the extra', () => {
    const t = new PaydayTracker();
    const a = pos();
    const b = pos();
    t.check([a, b], account(), label, 0);
    expect(t.check([settle(a, 50), b], account(), label, 1_000)).not.toBeNull();
    expect(t.check([settle(a, 50), settle(b, 70)], account(), label, 2_000)).toBeNull();
    expect(t.check([settle(a, 50), settle(b, 70)], account(), label, 2_000 + PAYDAY_COOLDOWN_MS)).toBeNull();
  });
});

describe('The High Stakes table', () => {
  const allNo = Object.fromEntries(MEMBER_IDS.map((id) => [id, false])) as Record<MemberId, boolean>;

  it('reads the room toward the real decision, leaving Greed and Panic alone', () => {
    const v = readTheRoom(allNo, true);
    const yes = MEMBER_IDS.filter((id) => v[id]).length;
    expect(yes).toBeGreaterThan(MEMBER_IDS.length - yes);
    expect(v.greed).toBe(false);
    expect(v.panic).toBe(false);
    for (const id of MEMBER_IDS) if (v[id]) expect(PERSUADABLE).toContain(id);
    // a copy, never the rule votes themselves
    expect(allNo.quant).toBe(false);
  });

  it('leaves a room that already agrees untouched', () => {
    expect(readTheRoom(allNo, false)).toEqual(allNo);
  });

  it('keeps the overrule a rare beat', () => {
    expect(OVERRULE_RATE).toBeGreaterThan(0);
    expect(OVERRULE_RATE).toBeLessThan(0.25);
  });

  it('never quotes a number the signal did not carry', () => {
    const f: Features = {
      subj: 'Lakers', title: 'Will the Lakers win?', dir: 'YES', price: null, edge: null, conf: null,
      cat: '', open: null, cap: null,
    };
    for (const id of [...MEMBER_IDS, 'dealer'] as const) {
      const p = PERSONAS[id];
      for (const l of [...p.pro(f), ...p.con(f)]) {
        if (l) expect(l).not.toMatch(/null|undefined|NaN/);
      }
    }
    // edge is gross points: no line may call it "after fees" or derive an EV
    const g = { ...f, edge: 12, conf: 70, price: 40 };
    const said = [...PERSONAS.quant.pro(g), ...PERSONAS.quant.con(g)].join(' | ');
    expect(said).not.toMatch(/after fees|EV/);
    expect(line([null, false, ''], 'fallback')).toBe('fallback');
  });

  it('scores WHO CALLED IT? on the rule votes, not the room-read ones', () => {
    vi.spyOn(Math, 'random').mockReturnValue(0.5); // > OVERRULE_RATE: the room is read
    const member = () => ({ pose: 'idle', talking: 0, set(p: string) { this.pose = p; }, climbTable() {} });
    let view: { lines: { name: string; value: string }[] } | null = null;
    const host = {
      members: Object.fromEntries(MEMBER_IDS.map((id) => [id, member()])),
      room: {
        feltPulse: 0, feltColor: { set() {} }, tableShake: { v: 0 },
        feed() {}, setBet() {}, setMood() {}, dealerTap() {},
        setScore(v: typeof view) { view = v; },
      },
      say: () => true, log() {}, speaking() {}, coins() {},
    } as unknown as TableHost;
    const c = new PokerTable(host);
    // price 50, edge 0, conf 50: Greed and Risk vote yes, the
    // rest no. The bot traded it, so the room swings Quant and FOMO to yes.
    const sig: HubSignal = {
      id: 77, source: 'crypto', ticker: 'KXNBA-LAL', eventTicker: 'KXNBA', title: 'Will the Lakers win?',
      category: 'Sports', direction: 'yes', priceCents: 50, confidence: 50, edgePts: 0,
      createdAt: '2026-10-06T10:00:00Z', traded: true,
    };
    const p = pos({ signalId: 77, confidence: 50, edgePts: 0 });
    c.ingest({ signals: [sig], positions: [p], account: null });
    c.update(1.5); // the hand is dealt
    expect(c.hand?.votes.quant).toBe(true);
    c.ingest({ signals: [sig], positions: [settle(p, 4)], account: null });
    c.update(0.6);
    const row = (id: MemberId) => c.scoreRows().find((r) => r.id === id)!;
    expect(row('greed')).toMatchObject({ right: 1, n: 1 });
    expect(row('risk')).toMatchObject({ right: 1, n: 1 });
    // Quant's own rule said no (edge 0 < 10), so a win is a miss for Quant
    expect(row('quant')).toMatchObject({ right: 0, n: 1 });
    // and the screen shows exactly those records
    expect(view!.lines.find((l) => l.name === 'GREED')!.value).toBe('100%');
    expect(view!.lines.find((l) => l.name === 'THE QUANT')!.value).toBe('0%');
  });
});

describe('absent is not zero', () => {
  it('a fresh install with no trades has no win rate and no session P&L', () => {
    const fresh = account({ totalUsd: 0, sessionBaselineUsd: 0, sessionPnlUsd: 0, winRate: 0 });
    const n = hudNumbers(fresh);
    expect(n.winRate).toBeNull();
    expect(n.pnl).toBeNull();
    expect(n.roi).toBeNull();
    expect(balanceOf(fresh)).toBeNull();
    expect(hudNumbers(null).settledToday).toBeNull();
  });

  it('real numbers pass straight through', () => {
    const a = account({ wins: 3, losses: 1, winRate: 75, sessionPnlUsd: -12.5, todayWins: 2, todayLosses: 1, openCount: 4 });
    const n = hudNumbers(a);
    expect(n).toMatchObject({ winRate: 75, pnl: -12.5, settledToday: 3, open: 4 });
    expect(balanceOf(a)).toBe(1000);
  });

  it('a strategy with no saved backtest is left out, not scored 0¢', () => {
    const lib = [
      { name: '★ BTC 15m Settlement Sniper — v2', backtest: { netCentsPerContract: 1.84, t: 3.1, n: 420 } },
      { name: 'Momentum (fast)', backtest: { netCentsPerContract: 0.9, t: 1.2, n: 80 } },
      { name: 'Never run', backtest: null },
    ] as unknown as StrategyWithBacktest[];
    const rows = savedBacktests(lib);
    expect(rows.map((r) => r.name)).toEqual(['Settlement Sniper', 'Momentum']);
    expect(rows.map(hasEdge)).toEqual([true, false]);
    expect(hasEdge({ name: 'x', net: 2, t: null, n: null })).toBe(false);
    expect(shortStrategy('ETH 15m VWAP Momentum (tight)')).toBe('VWAP Momentum');
  });
});

describe('session pots', () => {
  it('counts each settlement once, this session only, never a dry run', () => {
    const seen = new Map<number, boolean>();
    const start = Date.parse('2026-10-06T11:00:00Z');
    const before = settle(pos(), 10, '2026-10-06T09:00:00Z');
    const win = settle(pos(), 12.5);
    const loss = settle(pos(), -5);
    const dry = { ...settle(pos(), 99), status: 'dry_run' as const };
    const open = pos();
    let pot = addPot(EMPTY_POT, countPot(seen, [before, win, loss, dry, open], start));
    expect(pot).toEqual({ wins: 1, losses: 1, winPnl: 12.5, lossPnl: -5 });
    // the feed re-sends them (even with a corrected P&L): no double count
    pot = addPot(pot, countPot(seen, [before, { ...win, pnlUsd: 13 }, loss, dry, settle(open, 3)], start));
    expect(pot).toEqual({ wins: 2, losses: 1, winPnl: 15.5, lossPnl: -5 });
  });

  it('a settlement with no time cannot be placed in the session and is left out', () => {
    const seen = new Map<number, boolean>();
    const p = { ...settle(pos(), 5), resolvedAt: null };
    expect(countPot(seen, [p], Date.parse('2026-10-06T11:00:00Z'))).toEqual(EMPTY_POT);
  });
});

describe('text helpers', () => {
  it('subject() shortens a market to what fits in a bubble', () => {
    const cid = 'btc-updown-15m-1730000000-extra-long';
    expect(subject(cid, 'Bitcoin Up or Down - October 8, 3:15PM-3:30PM ET')).toBe('BTC');
    expect(subject(cid, 'Ethereum Up or Down - October 8, 3PM ET')).toBe('ETH');
    expect(subject(cid, 'Will the Lakers beat the Celtics tonight?')).toBe('Lakers beat Celtics');
    // no title: the slug itself, clipped
    expect(subject(cid, '')).toBe('btc-updown-15m-…');
    expect(subject(cid, cid)).toBe('btc-updown-15m-…');
    expect(subject('X', 'Will there be a government shutdown by November?')).toBe('government shutdown');
    const long = subject('X', 'Will the Federal Reserve cut interest rates by a quarter point?');
    expect(long.length).toBeLessThanOrEqual(24);
    expect(long.endsWith('…')).toBe(true);
  });

  it('cacheMeasureText measures each (font, text) once', () => {
    const measure = vi.fn((t: string) => ({ width: t.length } as TextMetrics));
    const ctx = { font: '10px a', measureText: measure } as unknown as CanvasRenderingContext2D;
    cacheMeasureText(ctx);
    expect(ctx.measureText('hello').width).toBe(5);
    ctx.measureText('hello');
    expect(measure).toHaveBeenCalledTimes(1);
    ctx.font = '20px a'; // a font change can't return a stale width
    ctx.measureText('hello');
    expect(measure).toHaveBeenCalledTimes(2);
  });
});
