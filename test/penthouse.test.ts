import { describe, expect, it } from 'vitest';
import type { AccountSnapshot, TraderConfig } from '@shared/types';
import type { ActivityRecord, AgentCallActivity } from '../src/state/activity';
import type { HubData, HubScoreboard } from '../src/hub/data';
import { DeskBook, agoShort, isOrderTool, pulseBuckets, toolGroup } from '../src/hub/penthouse/model';
import { paintAccount, paintCalls, paintMarket, paintRecord, paintStatus, type WallSource } from '../src/hub/penthouse/screens';
import { HUB_VIEWS } from '../src/hub/views';
import { atExit, collide, step } from '../src/hub/walk';
import { WALK_START, deskInReach, penthouseWalkSpace } from '../src/hub/penthouse/walkspace';
import { DESK_D } from '../src/hub/penthouse/desk';

/**
 * The Penthouse: only the agents the user set up get a desk, and every
 * number on its monitors came from a real call, the scoreboard or the
 * config. Pinned:
 *
 *   - every tool the MCP server serves has a group (none is guessed);
 *   - desks: one per configured agent; the app-made Default agent only once
 *     the user renames it, it does something, or it is the only agent; calls
 *     from a deleted agent are dropped, an id-less (older backend) call is
 *     the Default agent's;
 *   - a forecast's fair value stays on its market until another market is
 *     touched; replays never double count;
 *   - the monitors say "—" or words, never a number nobody produced.
 */

const NOW = Date.UTC(2026, 9, 11, 18, 0, 0);
const CID_A = `0x${'a1'.repeat(32)}`;
const CID_B = `0x${'b2'.repeat(32)}`;

function cfg(over: Partial<TraderConfig> & { mcpAgents?: unknown } = {}): TraderConfig {
  return {
    mcpEnabled: true, mcpTradeMode: 'live', accountMode: 'live',
    mcpAgents: [
      { id: 'default', name: 'Default', color: '#A855F7', emoji: '🤖', mode: 'paper', enabled: true, rules: {} },
      { id: 'scout01', name: 'Scout', color: '#38BDF8', emoji: '🦊', mode: 'live', enabled: true, rules: { maxOrderUsd: 5 } },
      { id: 'quant02', name: 'Quant', color: '#F472B6', emoji: '📈', mode: 'paper', enabled: false, rules: {} },
    ],
    ...over,
  } as unknown as TraderConfig;
}

let seq = 0;
function rec(over: Partial<AgentCallActivity> = {}, at = NOW): ActivityRecord {
  const ev: AgentCallActivity = {
    kind: 'agentCall', call: 'call', client: 'claude-code 2.1', transport: null, model: 'opus',
    tool: 'get_market', outcome: 'ok', reason: null, summary: 'get_market Fed cut',
    conditionId: CID_A, title: 'Will the Fed cut rates?', fairCents: null, edgeCents: null, midCents: null,
    side: null, mode: null, durationMs: 120, suppressed: 0, agentId: 'scout01', agentName: 'Scout', ...over,
  };
  return { seq: ++seq, at, ev };
}

/** A 2D context that records what was written, for painter tests. */
function fakeCtx(): CanvasRenderingContext2D & { texts: string[] } {
  const texts: string[] = [];
  const store: Record<string | symbol, unknown> = {};
  return new Proxy(store, {
    get(t, k) {
      if (k === 'texts') return texts;
      if (k === 'measureText') return (s: string) => ({ width: String(s).length * 8 });
      if (k === 'fillText') return (s: string) => { texts.push(String(s)); };
      if (k === 'createLinearGradient' || k === 'createRadialGradient') return () => ({ addColorStop() { /* recorded nowhere */ } });
      if (k in t) return t[k];
      return () => undefined;
    },
    set(t, k, v) { t[k] = v; return true; },
  }) as unknown as CanvasRenderingContext2D & { texts: string[] };
}

describe('tools', () => {
  it('groups every tool the MCP server serves', () => {
    const served = `get_status get_my_agent record_forecast get_portfolio get_orders get_trade_history get_engine_activity
      discover_markets search_markets get_market get_order_book get_price_history get_resolution_risk
      get_related_markets get_cross_venue manage_watchlist preview_order place_order cancel_order
      close_position cancel_all_orders flatten_wallet manage_standing_instructions pause_all_engines set_engines
      update_config backtest_script parlay_schedule research_data get_script_docs
      list_scripts validate_script save_script set_script_state`.split(/\s+/);
    for (const t of served) expect([t, toolGroup(t)]).not.toEqual([t, 'other']);
    expect(toolGroup('a_tool_from_a_later_server')).toBe('other');
    expect(isOrderTool('place_order') && isOrderTool('close_position') && !isOrderTool('get_market')).toBe(true);
  });
});

describe('desks', () => {
  it('gives the user’s agents a desk, and the unrenamed Default one only once it works', () => {
    const b = new DeskBook();
    b.sync(cfg());
    expect(b.list().map((d) => d.id)).toEqual(['scout01', 'quant02']);
    b.call(rec({ agentId: 'default', agentName: 'Default' }));
    expect(b.list().map((d) => d.id)).toEqual(['default', 'scout01', 'quant02']);
    // it goes by the client that drives it, as everywhere else in the hub
    expect(b.get('default')!.who.label).toBe('Claude Code · opus');
  });

  it('shows the Default agent when it is renamed, or the only agent', () => {
    const b = new DeskBook();
    b.sync(cfg({ mcpAgents: [{ id: 'default', name: 'Default', color: '#A855F7', emoji: '🤖', mode: 'paper', enabled: true, rules: {} }] }));
    expect(b.list().map((d) => d.id)).toEqual(['default']);
    const c = new DeskBook();
    c.sync(cfg({ mcpAgents: [{ id: 'default', name: 'Boss', color: '#A855F7', emoji: '🤖', mode: 'paper', enabled: true, rules: {} }, { id: 'scout01', name: 'Scout', color: '#38BDF8', emoji: '🦊', mode: 'live', enabled: true, rules: {} }] }));
    expect(c.list().map((d) => d.who.name)).toEqual(['Boss', 'Scout']);
  });

  it('drops a deleted agent’s calls and files an id-less call under Default', () => {
    const b = new DeskBook();
    b.sync(cfg());
    expect(b.call(rec({ agentId: 'gone0001', agentName: 'Gone' }))).toBeNull();
    expect(b.call(rec({ agentId: undefined, agentName: undefined }))?.id).toBe('default');
  });

  it('works out each desk’s real mode', () => {
    const b = new DeskBook();
    b.sync(cfg());
    expect(b.get('scout01')!.mode).toBe('live');
    expect(b.get('quant02')!.mode).toBe('off');          // switched off
    b.sync(cfg({ accountMode: 'paper' } as Partial<TraderConfig>));
    expect(b.get('scout01')!.mode).toBe('paper');        // account in Paper: nobody trades live
    b.sync(cfg({ mcpEnabled: false } as Partial<TraderConfig>));
    expect(b.get('scout01')!.mode).toBe('off');
    b.sync(cfg());
    expect(b.get('scout01')!.caps.maxOrderUsd).toBe(5);  // its own, stricter cap
  });
});

describe('calls', () => {
  it('counts each call once, newest first, and lists orders', () => {
    const b = new DeskBook();
    b.sync(cfg());
    const r1 = rec({ tool: 'get_market' }, NOW - 5000);
    const r2 = rec({ tool: 'place_order', mode: 'paper', summary: 'buy 5 YES @ 17¢' }, NOW - 2000);
    const r3 = rec({ tool: 'place_order', outcome: 'refused', reason: '$4.93 (fees included) is over the agent per-order cap of $1.60.' }, NOW - 1000);
    for (const r of [r2, r1, r3, r3, r2]) b.call(r);
    const d = b.get('scout01')!;
    expect([d.ok, d.refused, d.error]).toEqual([2, 1, 0]);
    expect(d.calls.map((c) => c.seq)).toEqual([r3.seq, r2.seq, r1.seq]);
    expect(d.orders.map((c) => c.seq)).toEqual([r3.seq, r2.seq]);
    expect(d.lastAt).toBe(NOW - 1000);
  });

  it('keeps a forecast on its market until another market is touched', () => {
    const b = new DeskBook();
    b.sync(cfg());
    b.call(rec({ tool: 'record_forecast', fairCents: 61, midCents: 57, edgeCents: 3.2, side: 'yes' }, NOW - 9000));
    b.call(rec({ tool: 'get_order_book', fairCents: null, midCents: 58 }, NOW - 4000));
    let m = b.get('scout01')!.market!;
    expect([m.fairCents, m.midCents, m.edgeCents, m.side, m.forecastAt]).toEqual([61, 58, 3.2, 'yes', NOW - 9000]);
    b.call(rec({ tool: 'get_market', conditionId: CID_B, title: 'Other' }, NOW - 1000));
    m = b.get('scout01')!.market!;
    expect([m.conditionId, m.fairCents, m.forecastAt]).toEqual([CID_B, null, null]);
    // a refusal doesn't move the market screen
    b.call(rec({ tool: 'place_order', conditionId: CID_A, outcome: 'refused' }, NOW));
    expect(b.get('scout01')!.market!.conditionId).toBe(CID_B);
  });

  it('buckets calls by minute for the activity strip', () => {
    const p = [
      { at: NOW - 10_000, outcome: 'ok' as const }, { at: NOW - 20_000, outcome: 'refused' as const },
      { at: NOW - 5 * 60_000 - 1, outcome: 'ok' as const }, { at: NOW - 31 * 60_000, outcome: 'ok' as const },
    ];
    const b = pulseBuckets(p, NOW, 30);
    expect(b[29]).toEqual({ ok: 1, refused: 1, error: 0 });
    expect(b[24]).toEqual({ ok: 1, refused: 0, error: 0 });
    expect(b.reduce((a, x) => a + x.ok + x.refused + x.error, 0)).toBe(3);
    expect([agoShort(null, NOW), agoShort(NOW - 12_000, NOW), agoShort(NOW - 4 * 60_000, NOW)]).toEqual(['—', '12s', '4m']);
  });
});

describe('scoreboard', () => {
  it('files each forecast under its own agent (id-less ones are Default’s)', () => {
    const b = new DeskBook();
    b.sync(cfg());
    const board: HubScoreboard = {
      byAgent: [{ agentId: 'scout01', n: 12, nPaired: 12, brierAgent: 0.18, brierMarket: 0.2, skill: 0.1, verdict: 'agent-better', pending: 3 }],
      recent: [
        { createdAt: null, conditionId: CID_A, title: 'A', model: null, client: null, agentId: 'scout01', fairValueCents: 61, marketMidCents: 57, outcome: null },
        { createdAt: null, conditionId: CID_B, title: 'B', model: null, client: null, agentId: null, fairValueCents: 40, marketMidCents: 44, outcome: 0 },
      ],
    };
    b.scoreboard(board);
    expect(b.get('scout01')!.score!.n).toBe(12);
    expect(b.get('scout01')!.forecasts.map((r) => r.title)).toEqual(['A']);
    expect(b.get('default')!.forecasts.map((r) => r.title)).toEqual(['B']);
    // Default has a record now, so it gets its desk
    expect(b.list().map((d) => d.id)).toContain('default');
  });
});

describe('a forecast just recorded', () => {
  it('shows at once as pending, once, and lends its title to the market', () => {
    const b = new DeskBook();
    b.sync(cfg());
    b.call(rec({ tool: 'record_forecast', title: null, fairCents: 36, midCents: 40, edgeCents: 2.3, side: 'no' }, NOW - 4000));
    let d = b.get('scout01')!;
    expect(d.forecasts.map((r) => [r.fairValueCents, r.outcome])).toEqual([[36, null]]);
    expect(d.market!.title).toBeNull();
    // the scoreboard's next read has it (with its title): one row, not two
    b.scoreboard({
      byAgent: [],
      recent: [{ createdAt: new Date(NOW - 3500).toISOString(), conditionId: CID_A, title: 'Will the Fed cut rates?', model: null, client: null, agentId: 'scout01', fairValueCents: 36, marketMidCents: 40, outcome: null }],
    });
    d = b.get('scout01')!;
    expect(d.forecasts).toHaveLength(1);
    expect(d.market!.title).toBe('Will the Fed cut rates?');
  });
});

describe('monitors', () => {
  it('say so in words when there is nothing to show', () => {
    const b = new DeskBook();
    b.sync(cfg());
    const d = b.get('scout01')!;
    for (const [paint, line] of [
      [paintCalls(d), 'No tool calls yet this session'],
      [paintMarket(d), 'No market touched yet'],
      [paintRecord(d, () => true), 'No forecasts recorded yet'],
    ] as const) {
      const g = fakeCtx();
      paint(g, 768, 432, NOW);
      expect(g.texts).toContain(line);
      expect(g.texts.some((t) => /\$0\.00|\b0¢/.test(t))).toBe(false);
    }
  });

  it('show a refusal with the rail that refused it', () => {
    const b = new DeskBook();
    b.sync(cfg());
    b.call(rec({ tool: 'place_order', outcome: 'refused', reason: '$4.93 (fees included) is over the agent per-order cap of $1.60.', summary: 'buy 29 YES @ 17¢' }, NOW - 3000));
    const g = fakeCtx();
    paintCalls(b.get('scout01')!)(g, 768, 432, NOW);
    expect(g.texts.some((t) => t.startsWith('refused: over the per-order cap'))).toBe(true);
    const s = fakeCtx();
    paintStatus(b.get('scout01')!)(s, 324, 576, NOW);
    expect(s.texts).toContain('LIVE');
    expect(s.texts.some((t) => t.includes('≤ $5/order'))).toBe(true);
  });

  it('a switched-off agent’s screens say it is off', () => {
    const b = new DeskBook();
    b.sync(cfg());
    const g = fakeCtx();
    paintCalls(b.get('quant02')!)(g, 768, 432, NOW);
    expect(g.texts).toContain('SWITCHED OFF');
  });

  it('the account screen shows no balance it could not read', () => {
    const b = new DeskBook();
    const down = { backendDown: true, totalUsd: 0, cashUsd: 0, sessionPnlUsd: 0 } as unknown as AccountSnapshot;
    const src: WallSource = { data: () => ({ signals: [], positions: [], account: down, config: cfg() } as HubData), book: b, approvals: () => [] };
    const g = fakeCtx();
    paintAccount(src)(g, 1024, 576, NOW);
    expect(g.texts).toContain('—');
    expect(g.texts).toContain('BACKEND DOWN — nothing here could be read');
    expect(g.texts.some((t) => t.includes('$0.00'))).toBe(false);
  });
});

describe('on foot', () => {
  const desks = [{ id: 'a', x: -4.3, z: -3.9 }, { id: 'b', x: -0.9, z: -3.9 }, { id: 'c', x: 2.5, z: -3.9 }];
  const space = penthouseWalkSpace(desks);

  it('starts you somewhere free, by the elevator', () => {
    const [x, z] = collide(WALK_START.x, WALK_START.z, space);
    expect([x, z]).toEqual([WALK_START.x, WALK_START.z]);
    expect(atExit(WALK_START.x, WALK_START.z, space)).toBe(false);
  });

  it('walks you round a desk, never through its monitors', () => {
    // step straight at the middle desk from in front of its chair
    let x = -0.9, z = -1.6;
    for (let i = 0; i < 80; i++) [x, z] = step(x, z, 0, -0.05, space);
    expect(z).toBeGreaterThan(-3.9 + DESK_D / 2);
  });

  it('lets you out through the elevator', () => {
    let x = WALK_START.x, z = WALK_START.z, out = false;
    for (let i = 0; i < 80 && !out; i++) { [x, z] = step(x, z, 0, 0.05, space); out = atExit(x, z, space); }
    expect(out).toBe(true);
  });

  it('finds the desk you are standing at', () => {
    expect(deskInReach(desks, -0.9, -2.6)?.id).toBe('b');
    expect(deskInReach(desks, 9, 4)).toBeNull();
  });
});

describe('the hub', () => {
  it('lists the Penthouse as a scene', () => {
    expect(HUB_VIEWS.map((v) => v.id)).toContain('penthouse');
  });
});
