import { describe, expect, it } from 'vitest';
import type { CredentialsStatusAll, TraderConfig } from '@shared/types';
import type { AgentPaper, ForecastScore, ForecastScoreboard } from '@shared/market';
import {
  agentRecord, agentRecordFor, agentsLiveOnLive, backToPaperPatch, CAP_KEYS, currentCaps,
  defaultLiveSelection, goLiveAgentNames, goLiveChecklist, goLivePatch, goLiveSummary, liveAgents,
  liveCandidates, noAgentReachable, paperPnlOf, STARTER_CAPS, starterCapsPatch, keyFingerprint,
  accountGoLiveChecklist,
  type AgentCaps, type GoLiveInput, type KeyVerify,
} from '../src/utils/goLive';

/**
 * The Go live screen is the one place a click turns agents from paper into
 * real money. Everything it decides is pinned here: what each checklist item
 * says from the real state, how honest the record verdict is, and — most of
 * all — that the final write carries only what the user confirmed.
 */

const score = (p: Partial<ForecastScore>): ForecastScore => ({
  n: 0, nPaired: 0, brierAgent: null, brierAgentPaired: null, brierMarket: null, skill: null,
  diffMean: null, diffSe: null, verdict: 'too-few', ...p,
});

const board = (overall: Partial<ForecastScore>, total = 40): ForecastScoreboard => ({
  totalForecasts: total, pending: 5, resolved: 35, scoredMarkets: 35, minScored: 30,
  overall: score(overall), buckets: [], byAgent: [], recent: [],
});

const paper = (realized: number | null, unreal: number | null = 0, positions = 1): AgentPaper => ({
  portfolio: {
    realizedPnlUsd: realized, totalUnrealizedPnlUsd: unreal, cashUsd: 1000, equityUsd: 1000,
    positions: Array.from({ length: positions }, (_, i) => ({
      conditionId: `market-${i}`, title: null, side: 'yes', size: 5, avgCostCents: 50,
      markCents: 50, unrealizedPnlUsd: unreal,
    })),
  },
  orders: { orders: [] },
});

const KEY_ID = '123e4567-e89b-12d3-a456-426614174000';
const creds = (has: boolean, address = KEY_ID): CredentialsStatusAll => ({
  current: 'mainnet',
  mainnet: {
    env: 'mainnet', hasWalletKey: has, hasApiCreds: has, address: has ? address : '', addressPreview: '',
    keyId: has ? address : '', keyStoredUnencrypted: false,
  } as CredentialsStatusAll['mainnet'],
});
const fp = keyFingerprint(creds(true));

const paperCfg: Partial<TraderConfig> = {
  accountMode: 'paper', mcpTradeMode: 'paper', mcpEnabled: true,
  mcpMaxOrderUsd: 25, mcpDailySpendUsd: 100, mcpMaxPositions: 10, mcpDailyLossUsd: 50, mcpLiveApproval: true,
};

describe('agentRecord: the verdict thresholds', () => {
  it('says plainly when there is no scoreboard or no forecast', () => {
    const none = agentRecord(null, { default: paper(0) });
    expect(none.verdict).toBe('unavailable');
    expect(none.tone).toBe('bad');
    expect(none.needsAck).toBe(true);

    const zero = agentRecord(board({}, 0), {});
    expect(zero.verdict).toBe('none');
    expect(zero.forecasts).toBe(0);
    expect(zero.headline).toMatch(/not recorded a single forecast/);
    expect(zero.notes).toContain('No paper trades yet.');
  });

  it('is "too few" below 30 resolved markets, even if the backend claimed a winner', () => {
    const r = agentRecord(board({ n: 29, nPaired: 29, verdict: 'agent-better', skill: 0.5 }), { default: paper(10) });
    expect(r.verdict).toBe('too-few');
    expect(r.headline).toMatch(/29 of 30 needed/);
    expect(r.needsAck).toBe(true);
  });

  it('calls a two-standard-error tie and a loss what they are: NOT beating the market', () => {
    const tie = agentRecord(board({ n: 40, nPaired: 40, verdict: 'indistinguishable' }), { default: paper(5) });
    expect(tie.verdict).toBe('no-edge');
    expect(tie.headline).toMatch(/no measurable difference \(n=40\)/);
    const worse = agentRecord(board({ n: 40, nPaired: 40, verdict: 'market-better' }), { default: paper(5) });
    expect(worse.verdict).toBe('worse');
    expect(worse.tone).toBe('bad');
  });

  it('only a real win with a non-losing paper record needs no acknowledgement', () => {
    const win = agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better', skill: 0.08 }),
      { default: paper(12, 3) });
    expect(win.verdict).toBe('beats');
    expect(win.paperPnlUsd).toBe(15);
    expect(win.needsAck).toBe(false);

    const losing = agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better' }),
      { default: paper(-8, 2), sam1: paper(0, 0) });
    expect(losing.notes.join(' ')).toMatch(/DOWN \$6\.00/);
    expect(losing.needsAck).toBe(true);
  });

  it('never turns an unmarked paper position into $0', () => {
    const r = agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better' }), { default: paper(5, null) });
    expect(r.paperPnlUsd).toBeNull();
    expect(r.needsAck).toBe(true);
    expect(paperPnlOf(paper(5, null))).toBeNull();
    expect(paperPnlOf(null)).toBeNull();
  });

  it('an unloaded paper account is said, not zeroed', () => {
    const r = agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better' }), null);
    expect(r.paperPnlUsd).toBeNull();
    expect(r.notes[0]).toMatch(/not loaded/);
    expect(r.needsAck).toBe(true);
  });
});

describe('starter caps', () => {
  it('writes exactly the four caps and nothing else', () => {
    expect(starterCapsPatch()).toEqual({
      mcpMaxOrderUsd: 5, mcpDailySpendUsd: 20, mcpMaxPositions: 3, mcpDailyLossUsd: 10,
    });
    expect(Object.keys(starterCapsPatch()).sort()).toEqual([...CAP_KEYS].sort());
  });

  it('cannot be mutated by a caller', () => {
    expect(Object.isFrozen(STARTER_CAPS)).toBe(true);
    const p = starterCapsPatch();
    p.mcpMaxOrderUsd = 999;
    expect(STARTER_CAPS.mcpMaxOrderUsd).toBe(5);
  });

  it('shows the enforced default when a cap is missing from config', () => {
    expect(currentCaps({})).toEqual({
      mcpMaxOrderUsd: 25, mcpDailySpendUsd: 100, mcpMaxPositions: 10, mcpDailyLossUsd: 50,
    });
  });
});

describe('goLiveChecklist', () => {
  const caps = currentCaps(paperCfg);
  const winning = agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better' }), { default: paper(5) });
  const input = (p: Partial<GoLiveInput> = {}): GoLiveInput => ({
    config: paperCfg, creds: creds(true), verify: { ok: true, message: 'ok', fingerprint: fp, balanceUsd: 12 },
    record: winning, recordAck: false, caps, approval: true, approvalOffAck: false,
    liveAgentIds: ['default'], ...p,
  });
  const step = (i: GoLiveInput, id: string) => goLiveChecklist(i).steps.find((s) => s.id === id)!;

  it('blocks at step 1 with no API key (any Paper user without one)', () => {
    const l = goLiveChecklist(input({ creds: creds(false), verify: null }));
    expect(l.ready).toBe(false);
    expect(l.steps[0].id).toBe('keys');
    expect(l.steps[0].state).toBe('block');
    expect(l.steps[0].detail).toMatch(/No API key saved/);
  });

  it('wants a Test of the CURRENT key, and a passing one', () => {
    expect(step(input({ verify: null }), 'keys').state).toBe('block');
    // A test of a key that has since been replaced vouches for nothing.
    expect(step(input({ creds: creds(true, '0xother') }), 'keys').state).toBe('block');
    const bad = step(input({ verify: { ok: false, message: '401', fingerprint: fp } }), 'keys');
    expect(bad.state).toBe('block');
    expect(bad.detail).toMatch(/did not accept.*401/);
    expect(step(input(), 'keys').state).toBe('ok');
    expect(step(input({ creds: null }), 'keys').state).toBe('block');
  });

  it('a key Polymarket US says is not ready warns, with the reasons', () => {
    const s = step(input({ verify: { ok: true, message: 'ok', fingerprint: fp, ready: false, issues: ['no balance'] } }), 'keys');
    expect(s.state).toBe('warn');
    expect(s.detail).toMatch(/no balance/);
  });

  it('a weak record blocks until acknowledged, then reads as a warning — never ok', () => {
    const weak = agentRecord(board({ n: 2, nPaired: 2 }), { default: paper(0) });
    expect(step(input({ record: weak }), 'record').state).toBe('block');
    expect(step(input({ record: weak, recordAck: true }), 'record').state).toBe('warn');
    expect(step(input(), 'record').state).toBe('ok');
  });

  it('approval off counts only through its own confirm', () => {
    expect(step(input(), 'approval').state).toBe('ok');
    expect(step(input({ approval: false }), 'approval').state).toBe('block');
    expect(step(input({ approval: false, approvalOffAck: true }), 'approval').state).toBe('warn');
  });

  it('refuses caps it would not write, including "no cap"', () => {
    const bad = (c: Partial<AgentCaps>) => step(input({ caps: { ...caps, ...c } }), 'caps').state;
    expect(bad({ mcpMaxOrderUsd: 0 })).toBe('block');
    expect(bad({ mcpDailySpendUsd: Number.NaN })).toBe('block');
    expect(bad({ mcpMaxPositions: 2.5 })).toBe('block');
    expect(bad({ mcpMaxPositions: 201 })).toBe('block');
    expect(bad({ mcpDailyLossUsd: 0.5 })).toBe('block');
    expect(bad({})).toBe('ok');
  });

  it('marks the account mode as applied by Go live in Paper, done when Live', () => {
    expect(step(input(), 'env').state).toBe('apply');
    expect(step(input({ config: { ...paperCfg, accountMode: 'live' } }), 'env').state).toBe('ok');
  });

  it('is ready only when nothing blocks', () => {
    expect(goLiveChecklist(input()).ready).toBe(true);
    expect(goLiveChecklist(input({ verify: null })).ready).toBe(false);
  });
});

describe('the API key step: an untradeable account blocks, not warns', () => {
  const caps = currentCaps(paperCfg);
  const winning = agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better' }), { default: paper(5) });
  const v = (p: Partial<KeyVerify> = {}): KeyVerify => ({
    ok: true, message: 'ok', fingerprint: fp, balanceUsd: 12, ready: true, issues: [], fatalIssues: [], ...p,
  });
  const agents = (verify: KeyVerify) => goLiveChecklist({
    config: paperCfg, creds: creds(true), verify, record: winning, recordAck: false, caps,
    approval: true, approvalOffAck: false, liveAgentIds: ['default'],
  });
  const keys = (l: { steps: { id: string; state: string; detail: string }[] }) => l.steps.find((s) => s.id === 'keys')!;

  it.each([
    'the account has no balance',
    'trading is restricted on this account',
  ])('a fatal readiness issue blocks instead of warning: %s', (issue) => {
    const l = agents(v({ ready: false, issues: [issue], fatalIssues: [issue] }));
    expect(keys(l).state).toBe('block');
    expect(keys(l).detail).toContain(issue);
    expect(l.ready).toBe(false);
  });

  it('a caution alone still only warns', () => {
    const l = agents(v({ ready: false, issues: ['balance is $3.00 — enough to trade up to ~60c'] }));
    expect(keys(l).state).toBe('warn');
    expect(l.ready).toBe(true);
  });

  it('an older backend without fatalIssues keeps the old warn', () => {
    const s = keys(agents({ ok: true, message: 'ok', fingerprint: fp, ready: false, issues: ['no balance'] }));
    expect(s.state).toBe('warn');
  });

  it('a passing Test is green, and says nothing about a region or a wallet', () => {
    const s = keys(agents(v()));
    expect(s.state).toBe('ok');
    expect(s.detail).toMatch(/Polymarket US/);
    expect(s.detail.toLowerCase()).not.toMatch(/region|wallet/);
  });

  it('a key the exchange refuses blocks, with its reason', () => {
    const s = keys(agents(v({ ok: false, message: 'invalid signature' })));
    expect(s.state).toBe('block');
    expect(s.detail).toContain('invalid signature');
  });

  it('a fatal issue blocks the account checklist too', () => {
    const empty = accountGoLiveChecklist({ config: paperCfg, creds: creds(true),
      verify: v({ ready: false, issues: ['the account has no balance'], fatalIssues: ['the account has no balance'] }), armed: [], ack: true });
    expect(empty.steps[0].state).toBe('block');
  });
});

describe('goLivePatch: the one write', () => {
  const caps = currentCaps(paperCfg);
  const ALLOWED = new Set<string>(['accountMode', 'mcpTradeMode', 'mcpLiveApproval', 'mcpAgents', ...CAP_KEYS]);
  const D = { liveAgentIds: ['default'] };

  it('switches the account mode and trade mode with approvals ON, and writes no unchanged cap', () => {
    const p = goLivePatch(paperCfg, { caps, approval: true, approvalOffAck: false, ...D });
    const { mcpAgents, ...rest } = p!;
    expect(rest).toEqual({ accountMode: 'live', mcpTradeMode: 'live', mcpLiveApproval: true });
    expect(mcpAgents?.map((a) => [a.id, a.mode])).toEqual([['default', 'live']]);
  });

  it('never sets anything outside what the summary showed', () => {
    const cfg: Partial<TraderConfig> = {
      ...paperCfg, mcpEnabled: false, autopilotEnabled: false, crypto15mEnabled: false, mcpAllowLiveSwitches: false,
    };
    const p = goLivePatch(cfg, { caps: { ...STARTER_CAPS }, approval: true, approvalOffAck: false, ...D })!;
    for (const k of Object.keys(p)) expect(ALLOWED.has(k)).toBe(true);
    expect(p).not.toHaveProperty('mcpEnabled');
    expect(p).not.toHaveProperty('crypto15mEnabled');
    expect(p).not.toHaveProperty('terminalPaperMode');
    expect(p).toMatchObject({ ...STARTER_CAPS });
  });

  it('keeps approvals on unless they were turned off through the confirm', () => {
    const off = { ...paperCfg, mcpLiveApproval: false };
    expect(goLivePatch(off, { caps, approval: true, approvalOffAck: false, ...D })!.mcpLiveApproval).toBe(true);
    expect(goLivePatch(paperCfg, { caps, approval: false, approvalOffAck: false, ...D })!.mcpLiveApproval).toBe(true);
    expect(goLivePatch(paperCfg, { caps, approval: false, approvalOffAck: true, ...D })!.mcpLiveApproval).toBe(false);
  });

  it('does not rewrite the account mode when already Live', () => {
    const p = goLivePatch({ ...paperCfg, accountMode: 'live' }, { caps, approval: true, approvalOffAck: false, ...D })!;
    expect(p).not.toHaveProperty('accountMode');
  });

  it('writes nothing at all with invalid caps', () => {
    expect(goLivePatch(paperCfg, { caps: { ...caps, mcpMaxOrderUsd: 0 }, approval: true, approvalOffAck: false, ...D })).toBeNull();
  });

  it('summarises the spend and the approval honestly', () => {
    expect(goLiveSummary({ caps: { ...STARTER_CAPS }, approval: true, approvalOffAck: false, ...D })).toBe(
      'Your agents can place real orders on Polymarket US, up to $20.00/day ($5.00 per order, '
      + 'at most 3 open positions, buys stop after $10.00 lost in a day), each needing your approval.');
    expect(goLiveSummary({ caps, approval: false, approvalOffAck: true })).toMatch(/WITHOUT asking you per order/);
  });
});

const sam = { id: 'sam1', name: 'Sports Sam', mode: 'paper', enabled: true, rules: {} };
const res = { id: 'res1', name: 'Careful Researcher', mode: 'live', enabled: true, rules: {} };
const off = { id: 'off1', name: 'Switched Off', mode: 'live', enabled: false, rules: {} };
const multiCfg = {
  ...paperCfg,
  mcpAgents: [{ id: 'default', name: 'Default', mode: 'paper' }, sam, res, off],
} as unknown as Partial<TraderConfig>;

describe('Go live: which agents go live', () => {
  const caps = currentCaps(paperCfg);
  const input = (ids: string[]): GoLiveInput => ({
    config: multiCfg, creds: creds(true), verify: { ok: true, message: 'ok', fingerprint: fp },
    record: agentRecord(board({ n: 35, nPaired: 35, verdict: 'agent-better' }), { default: paper(5) }),
    recordAck: false, caps, approval: true, approvalOffAck: false, liveAgentIds: ids,
  });
  const step = (ids: string[]) => goLiveChecklist(input(ids)).steps.find((s) => s.id === 'agents')!;

  it('blocks until at least one agent is ticked, and names the ticked ones', () => {
    expect(step([]).state).toBe('block');
    expect(step(['sam1']).detail).toBe('Sports Sam goes live; 2 others stay on paper.');
  });

  it('a switched-off agent cannot be ticked into live', () => {
    expect(step(['off1']).state).toBe('block');
    expect(liveCandidates(multiCfg).map((a) => a.id)).toEqual(['default', 'sam1', 'res1']);
    expect(goLivePatch(multiCfg, { caps, approval: true, approvalOffAck: false, liveAgentIds: ['off1'] })).toBeNull();
  });

  it('writes exactly the ticked agents live and every other one paper', () => {
    const p = goLivePatch(multiCfg, { caps, approval: true, approvalOffAck: false, liveAgentIds: ['sam1'] })!;
    expect(Object.fromEntries(p.mcpAgents!.map((a) => [a.id, a.mode]))).toEqual({
      default: 'paper', sam1: 'live', res1: 'paper', off1: 'paper',
    });
    expect(p.mcpAgents!.find((a) => a.id === 'off1')!.enabled).toBe(false);
  });

  it('opens with the agents already live, or the only agent, never all', () => {
    expect(defaultLiveSelection(multiCfg)).toEqual(['res1']);
    expect(defaultLiveSelection(paperCfg)).toEqual(['default']);
    const two = { ...paperCfg, mcpAgents: [{ id: 'default', mode: 'paper' }, sam] } as unknown as Partial<TraderConfig>;
    expect(defaultLiveSelection(two)).toEqual([]);
  });

  it('the summary names who can spend', () => {
    const d = { caps: { ...STARTER_CAPS }, approval: true, approvalOffAck: false, liveAgentIds: ['sam1', 'res1'] };
    const names = goLiveAgentNames(multiCfg, d);
    expect(names).toEqual(['Sports Sam', 'Careful Researcher']);
    expect(goLiveSummary(d, names)).toMatch(/Live: Sports Sam, Careful Researcher\. Every other agent trades on paper/);
  });

  it('the live bar needs a live agent and the app Live, not just a live global mode', () => {
    const liveApp = { ...multiCfg, accountMode: 'live' as const, mcpTradeMode: 'live' as const };
    expect(agentsLiveOnLive(liveApp)).toBe(true);
    expect(liveAgents(liveApp).map((a) => a.name)).toEqual(['Careful Researcher']);
    expect(agentsLiveOnLive({ ...liveApp, accountMode: 'paper' })).toBe(false);
    const allPaper = { ...liveApp, mcpAgents: [{ id: 'default', mode: 'paper' }, sam] } as unknown as Partial<TraderConfig>;
    expect(agentsLiveOnLive(allPaper)).toBe(false);
    expect(agentsLiveOnLive(null)).toBe(false);
  });

  it('each agent stands on its own record', () => {
    const b: ForecastScoreboard = {
      ...board({ n: 35, nPaired: 35, verdict: 'agent-better' }),
      byAgent: [
        { agentId: 'sam1', total: 50, pending: 5, lastAt: null, ...score({ n: 45, nPaired: 40, verdict: 'market-better', skill: -0.1 }) },
        { agentId: 'res1', total: 60, pending: 0, lastAt: null, ...score({ n: 60, nPaired: 60, verdict: 'agent-better', skill: 0.05 }) },
      ],
    };
    const s = agentRecordFor(b, paper(-3, 1), 'sam1');
    expect(s.verdict).toBe('worse');
    expect(s.paperPnlUsd).toBe(-2);
    expect(agentRecordFor(b, null, 'res1').verdict).toBe('beats');
    expect(agentRecordFor(b, null, 'default').verdict).toBe('none');
    // No paper trades is a dash, not $0.00.
    expect(agentRecordFor(b, paper(0, 0, 0), 'res1').paperPnlUsd).toBeNull();
    // Unmarked is unknown, never $0.
    expect(agentRecordFor(b, paper(2, null), 'res1').paperPnlUsd).toBeNull();
  });
});

describe('Back to paper (agents)', () => {
  it('changes trade mode only and leaves the account mode alone', () => {
    expect(backToPaperPatch()).toEqual({ mcpTradeMode: 'paper' });
    const after = { ...paperCfg, accountMode: 'live' as const, mcpTradeMode: 'live' as const, ...backToPaperPatch() };
    expect(after.accountMode).toBe('live');
    expect(agentsLiveOnLive(after)).toBe(false);
  });

  it('says when no agent can reach the app', () => {
    expect(noAgentReachable({ mcpEnabled: false, autopilotEnabled: false })).toBe(true);
    expect(noAgentReachable({ mcpEnabled: true })).toBe(false);
    expect(noAgentReachable({ autopilotEnabled: true })).toBe(false);
  });
});
