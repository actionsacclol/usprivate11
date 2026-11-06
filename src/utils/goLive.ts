import type { CredentialsStatusAll, TraderConfig } from '@shared/types';
import type { AgentPaper, ForecastScoreboard } from '@shared/market';
import { agentsOf, DEFAULT_AGENT_ID, type McpAgent } from '@shared/agents';

/**
 * The "Go live" checklists, as pure functions.
 *
 * Putting an agent on real money used to take several actions in several
 * places — the account mode, the agents' trade mode, each agent's own mode,
 * the approval queue — and each confirm described only its own switch. The
 * checklist puts every precondition on one screen with its REAL state, and
 * everything that decides what gets written lives here, where a test can pin
 * it: the panels only render it.
 *
 * What it deliberately does NOT do: require a track record. A weak or missing
 * record is stated in plain words and needs its own explicit acknowledgement —
 * never hidden, softened, or folded into the final button.
 */

type Cfg = Partial<TraderConfig> | null | undefined;

const usd = (v: number): string => `$${Math.abs(v).toFixed(2)}`;

// ── Caps ────────────────────────────────────────────────────────────────────

export interface AgentCaps {
  mcpMaxOrderUsd: number;
  mcpDailySpendUsd: number;
  mcpMaxPositions: number;
  mcpDailyLossUsd: number;
}

export const CAP_KEYS = [
  'mcpMaxOrderUsd', 'mcpDailySpendUsd', 'mcpMaxPositions', 'mcpDailyLossUsd',
] as const satisfies readonly (keyof AgentCaps)[];

/** Small enough that a first week of mistakes is a lesson, not a loss: a
 *  model that is confidently wrong can spend at most $20 a day, and stops
 *  buying after losing $10. The shipped defaults ($25 / $100 / 10 / $50) are
 *  sized for someone who already trusts their agent. */
export const STARTER_CAPS: Readonly<AgentCaps> = Object.freeze({
  mcpMaxOrderUsd: 5,
  mcpDailySpendUsd: 20,
  mcpMaxPositions: 3,
  mcpDailyLossUsd: 10,
});

/** The backend's own defaults (python/config.py) — what a config without the
 *  key means, so the checklist shows the cap that is actually enforced. */
const CAP_DEFAULTS: Readonly<AgentCaps> = {
  mcpMaxOrderUsd: 25, mcpDailySpendUsd: 100, mcpMaxPositions: 10, mcpDailyLossUsd: 50,
};

export function currentCaps(c: Cfg): AgentCaps {
  return {
    mcpMaxOrderUsd: c?.mcpMaxOrderUsd ?? CAP_DEFAULTS.mcpMaxOrderUsd,
    mcpDailySpendUsd: c?.mcpDailySpendUsd ?? CAP_DEFAULTS.mcpDailySpendUsd,
    mcpMaxPositions: c?.mcpMaxPositions ?? CAP_DEFAULTS.mcpMaxPositions,
    mcpDailyLossUsd: c?.mcpDailyLossUsd ?? CAP_DEFAULTS.mcpDailyLossUsd,
  };
}

/** "Use starter caps": the four caps and nothing else. */
export function starterCapsPatch(): Partial<TraderConfig> {
  return { ...STARTER_CAPS };
}

/** Why these caps can't be written, or `null` when they can. On the go-live
 *  path a cap of 0 ("no cap" in config) is refused: going live uncapped is
 *  not something this screen will write. */
export function capsProblem(caps: AgentCaps): string | null {
  const money = (v: number): boolean => Number.isFinite(v) && v >= 1;
  if (!money(caps.mcpMaxOrderUsd)) return 'Max per order must be at least $1.';
  if (!money(caps.mcpDailySpendUsd)) return 'Max spend per day must be at least $1.';
  if (!money(caps.mcpDailyLossUsd)) return 'Daily loss stop must be at least $1.';
  if (!Number.isInteger(caps.mcpMaxPositions) || caps.mcpMaxPositions < 1 || caps.mcpMaxPositions > 200) {
    return 'Max open positions must be a whole number from 1 to 200.';
  }
  return null;
}

// ── Paper P&L of an agent's own paper account ──────────────────────────────

/** Realised + open, net of fees; null when unknown (a position with no bid,
 *  the book not loaded) — an unmarked position is not $0. */
export function paperPnlOf(p: AgentPaper | null | undefined): number | null {
  const pf = p?.portfolio;
  if (!pf) return null;
  const real = pf.realizedPnlUsd;
  const unreal = pf.totalUnrealizedPnlUsd;
  if (typeof real !== 'number' || !Number.isFinite(real)) return null;
  if (unreal === null) return null;
  if (unreal === undefined) {
    // Older portfolio shape: sum the positions, any unmarked one makes it unknown.
    let sum = 0;
    for (const pos of pf.positions ?? []) {
      if (pos.unrealizedPnlUsd === null || !Number.isFinite(pos.unrealizedPnlUsd)) return null;
      sum += pos.unrealizedPnlUsd;
    }
    return real + sum;
  }
  return Number.isFinite(unreal) ? real + unreal : null;
}

/** Has this paper account traded at all? No trades is no record — a dash,
 *  not a $0.00 nobody earned. */
export function paperTraded(p: AgentPaper | null | undefined): boolean {
  const pf = p?.portfolio;
  if (!pf) return false;
  if ((pf.positions ?? []).length > 0) return true;
  if ((p?.orders?.orders ?? []).some((o) => (o.filledSize ?? 0) > 0)) return true;
  return typeof pf.realizedPnlUsd === 'number' && Math.abs(pf.realizedPnlUsd) > 1e-9;
}

// ── The agents' record ──────────────────────────────────────────────────────

export type RecordVerdict = 'unavailable' | 'none' | 'too-few' | 'no-edge' | 'worse' | 'beats';

export interface AgentRecord {
  verdict: RecordVerdict;
  /** Forecasts recorded (settled or not). null when not counted. */
  forecasts: number | null;
  /** Settled markets forecast that had a market price to beat. */
  settled: number;
  minScored: number;
  /** Brier skill vs the market on those, >0 = the agent's error was smaller. */
  skill: number | null;
  /** Paper realised + unrealised. null when unknown. */
  paperPnlUsd: number | null;
  tone: 'good' | 'warn' | 'bad';
  headline: string;
  notes: string[];
  /** The record is weak or missing: going live needs its own tick-box. */
  needsAck: boolean;
}

/**
 * Every agent's record together, from the scoreboard (which in this app holds
 * agent forecasts only — the Analyse button never produces a number) and the
 * agents' paper accounts. Thresholds are the scoreboard's own
 * (forecast_ledger.py): fewer than `minScored` settled, two-sided markets is
 * "too few"; a Brier difference inside two standard errors is "no measurable
 * edge". A confident verdict over too few markets is downgraded, never
 * trusted.
 */
export function agentRecord(
  board: ForecastScoreboard | null,
  papers: Record<string, AgentPaper | null> | null,
): AgentRecord {
  let paperPnlUsd: number | null = null;
  const notes: string[] = [];
  if (papers === null) {
    notes.push('Paper accounts not loaded, so paper P&L is unknown.');
  } else {
    const traded = Object.values(papers).filter((p) => paperTraded(p));
    if (!traded.length) {
      notes.push('No paper trades yet.');
    } else {
      let sum = 0;
      let known = true;
      for (const p of traded) {
        const v = paperPnlOf(p);
        if (v === null) { known = false; break; }
        sum += v;
      }
      paperPnlUsd = known ? sum : null;
      if (!known) notes.push('Paper P&L unknown: a paper position has no bid to mark it at.');
      else if (sum < 0) notes.push(`Paper is DOWN ${usd(sum)} (realised + open, net of fees).`);
    }
  }
  const minScored = board?.minScored ?? 30;
  const base = { minScored, paperPnlUsd, notes };
  if (!board) {
    return {
      ...base, verdict: 'unavailable', forecasts: null, settled: 0, skill: null, tone: 'bad',
      headline: 'The scoreboard is unavailable, so there is no evidence your agents beat the market.',
      needsAck: true,
    };
  }
  const forecasts = board.totalForecasts;
  const o = board.overall;
  const settled = o?.nPaired ?? 0;
  const skill = o?.skill ?? null;
  const rest = { ...base, forecasts, settled, skill };
  if (forecasts === 0) {
    return {
      ...rest, verdict: 'none', tone: 'bad', needsAck: true,
      headline: 'Your agents have not recorded a single forecast yet. There is no evidence they beat the market.',
    };
  }
  const v = o?.verdict ?? 'too-few';
  if (v === 'too-few' || settled < minScored) {
    return {
      ...rest, verdict: 'too-few', tone: 'warn', needsAck: true,
      headline: `Not enough resolved forecasts yet to tell if they beat the market (${settled} of ${minScored} needed).`,
    };
  }
  if (v === 'market-better') {
    return {
      ...rest, verdict: 'worse', tone: 'bad', needsAck: true,
      headline: `So far they do NOT beat the market price: the market was more accurate (n=${settled}).`,
    };
  }
  if (v === 'indistinguishable') {
    return {
      ...rest, verdict: 'no-edge', tone: 'warn', needsAck: true,
      headline: `So far they do NOT beat the market price: no measurable difference (n=${settled}).`,
    };
  }
  const losing = paperPnlUsd !== null && paperPnlUsd < 0;
  return {
    ...rest, verdict: 'beats', tone: losing ? 'warn' : 'good', needsAck: losing || paperPnlUsd === null,
    headline: `So far they beat the market (n=${settled}). Past markets, not a promise.`,
  };
}

/**
 * ONE named agent's record, from its own scoreboard row and its own paper
 * account. Each agent stands on its own forecasts — a careful researcher's
 * good record must not vouch for a sports gambler.
 */
export function agentRecordFor(
  board: ForecastScoreboard | null, paper: AgentPaper | null | undefined, agentId: string,
): AgentRecord {
  const traded = paperTraded(paper);
  const paperPnlUsd = traded ? paperPnlOf(paper) : null;
  const notes: string[] = [];
  if (!traded) notes.push('No paper trades yet.');
  else if (paperPnlUsd === null) notes.push('Paper P&L unknown: a position has no bid to mark it at.');
  else if (paperPnlUsd < 0) notes.push(`Paper is DOWN ${usd(paperPnlUsd)} (realised + open, net of fees).`);
  const minScored = board?.minScored ?? 30;
  const base = { minScored, paperPnlUsd, notes };
  if (!board || !board.byAgent) {
    return {
      ...base, verdict: 'unavailable', forecasts: null, settled: 0, skill: null, tone: 'bad',
      headline: 'No scoreboard for this agent.', needsAck: true,
    };
  }
  const row = board.byAgent.find((r) => r.agentId === agentId) ?? null;
  const forecasts = row?.total ?? 0;
  const settled = row?.nPaired ?? 0;
  const skill = row?.skill ?? null;
  const rest = { ...base, forecasts, settled, skill };
  if (!row || forecasts === 0) {
    return { ...rest, verdict: 'none', tone: 'bad', needsAck: true, headline: 'No forecasts yet.' };
  }
  if (row.verdict === 'too-few' || settled < minScored) {
    return { ...rest, verdict: 'too-few', tone: 'warn', needsAck: true, headline: `Too few resolved (${settled} of ${minScored}).` };
  }
  if (row.verdict === 'market-better') {
    return { ...rest, verdict: 'worse', tone: 'bad', needsAck: true, headline: `The market beat it (n=${settled}).` };
  }
  if (row.verdict === 'indistinguishable') {
    return { ...rest, verdict: 'no-edge', tone: 'warn', needsAck: true, headline: `No measurable edge (n=${settled}).` };
  }
  const losing = paperPnlUsd !== null && paperPnlUsd < 0;
  return {
    ...rest, verdict: 'beats', tone: losing ? 'warn' : 'good', needsAck: losing,
    headline: `Beat the market so far (n=${settled}). Not a promise.`,
  };
}

// ── The API key ─────────────────────────────────────────────────────────────

/** The last on-click "Test" of the API key. Tied to the key's identity so a
 *  test of the old key never vouches for a replaced one. */
export interface KeyVerify {
  ok: boolean;
  message: string;
  fingerprint: string;
  balanceUsd?: number | null;
  /** The backend's trading-readiness verdict (balance, account state). */
  ready?: boolean;
  issues?: string[];
  /** The issues under which no order can work at all. Absent from an older
   *  backend, which then keeps the old 'warn'. */
  fatalIssues?: string[];
}

export function keyFingerprint(creds: CredentialsStatusAll | null | undefined): string {
  const m = creds?.mainnet;
  if (!m?.hasWalletKey) return '';
  return (m.keyId || m.address || '').toLowerCase();
}

/** The API key, saved and tested on a click. Shared by the agents' Go live and
 *  the account's: both end in real orders signed with this key. */
function keyState(creds: CredentialsStatusAll | null, verify: KeyVerify | null): GoLiveStep {
  const base = { id: 'keys' as const, title: 'Polymarket US API key' };
  if (!creds) {
    return { ...base, state: 'block', detail: 'API key status is unavailable (is the backend running?).' };
  }
  const m = creds.mainnet;
  if (!m?.hasWalletKey) {
    return { ...base, state: 'block', detail: 'No API key saved. Add one on the API Key page — Paper never needed one, Live does.' };
  }
  if (!verify || verify.fingerprint !== keyFingerprint(creds)) {
    return { ...base, state: 'block', detail: 'Saved, not tested yet. Test reads your balance with this key.' };
  }
  if (!verify.ok) {
    return { ...base, state: 'block', detail: `Polymarket US did not accept this key: ${verify.message}` };
  }
  const bal = typeof verify.balanceUsd === 'number' ? ` · balance ${usd(verify.balanceUsd)}` : '';
  const fatal = verify.fatalIssues ?? [];
  if (fatal.length) {
    return { ...base, state: 'block', detail: `Connected${bal}, but no order can go through yet: ${fatal.join('; ')}.` };
  }
  if (verify.ready === false) {
    const rest = verify.issues ?? [];
    const why = rest.length ? `: ${rest.join('; ')}` : '';
    return { ...base, state: 'warn', detail: `Connected${bal}, but the account is not ready to trade${why}.` };
  }
  return { ...base, state: 'ok', detail: `Tested with Polymarket US${bal}.` };
}

// ── The agents' checklist ───────────────────────────────────────────────────

/** ok: done. warn: done, with something the user should know. apply: will be
 *  done by the Go live button. block: Go live stays disabled. */
export type StepState = 'ok' | 'warn' | 'apply' | 'block';
export type StepId = 'keys' | 'record' | 'agents' | 'caps' | 'approval' | 'env';
export type AccountStepId = 'keys' | 'engines' | 'ack';

export interface GoLiveStep {
  id: StepId;
  state: StepState;
  title: string;
  detail: string;
}

export interface GoLiveInput {
  config: Cfg;
  creds: CredentialsStatusAll | null;
  verify: KeyVerify | null;
  record: AgentRecord;
  recordAck: boolean;
  caps: AgentCaps;
  approval: boolean;
  /** Turning approval off went through its own red confirm. */
  approvalOffAck: boolean;
  /** The named agents ticked to go live. The rest stay on paper. */
  liveAgentIds: string[];
}

export interface GoLiveChecklist {
  steps: GoLiveStep[];
  ready: boolean;
}

export function goLiveChecklist(i: GoLiveInput): GoLiveChecklist {
  const steps: GoLiveStep[] = [
    keyState(i.creds, i.verify), recordStep(i), agentsStep(i), capsStep(i), approvalStep(i), envStep(i),
  ];
  return { steps, ready: steps.every((s) => s.state !== 'block') };
}

/** Enabled agents only: a switched-off agent cannot trade, so offering it a
 *  live tick would be consent to nothing. */
export function liveCandidates(c: Cfg): McpAgent[] {
  return agentsOf(c).filter((a) => a.enabled);
}

/** The ticks the step opens with: the agents already set to live, or — when
 *  there is just one agent — that one. Never every agent by default. */
export function defaultLiveSelection(c: Cfg): string[] {
  const cands = liveCandidates(c);
  const already = cands.filter((a) => a.mode === 'live').map((a) => a.id);
  if (already.length) return already;
  return cands.length === 1 ? [cands[0].id] : [];
}

function selectedAgents(c: Cfg, ids: string[]): McpAgent[] {
  return liveCandidates(c).filter((a) => ids.includes(a.id));
}

function agentsStep({ config, liveAgentIds }: GoLiveInput): GoLiveStep {
  const base = { id: 'agents' as const, title: 'Which agents go live' };
  const sel = selectedAgents(config, liveAgentIds);
  if (!sel.length) {
    return { ...base, state: 'block', detail: 'Tick at least one agent. Unticked agents keep trading on paper.' };
  }
  const rest = liveCandidates(config).length - sel.length;
  return {
    ...base, state: 'ok',
    detail: `${sel.map((a) => a.name).join(', ')} ${sel.length === 1 ? 'goes' : 'go'} live`
      + (rest ? `; ${rest} other${rest === 1 ? '' : 's'} stay${rest === 1 ? 's' : ''} on paper.` : '.'),
  };
}

function recordStep({ record, recordAck }: GoLiveInput): GoLiveStep {
  const base = { id: 'record' as const, title: "Your agents' record", detail: record.headline };
  if (!record.needsAck) return { ...base, state: 'ok' };
  return { ...base, state: recordAck ? 'warn' : 'block' };
}

function capsStep({ caps }: GoLiveInput): GoLiveStep {
  const problem = capsProblem(caps);
  const base = { id: 'caps' as const, title: 'Caps' };
  if (problem) return { ...base, state: 'block', detail: problem };
  return {
    ...base, state: 'ok',
    detail: `${usd(caps.mcpMaxOrderUsd)}/order · ${usd(caps.mcpDailySpendUsd)}/day · ${caps.mcpMaxPositions} positions · stop after ${usd(caps.mcpDailyLossUsd)} lost in a day`,
  };
}

function approvalStep({ approval, approvalOffAck }: GoLiveInput): GoLiveStep {
  const base = { id: 'approval' as const, title: 'Approvals' };
  if (approval) return { ...base, state: 'ok', detail: 'Every live order waits for your approval.' };
  if (approvalOffAck) {
    return { ...base, state: 'warn', detail: 'OFF: orders that pass the rails are sent without asking you.' };
  }
  return { ...base, state: 'block', detail: 'Turning approvals off needs its own confirmation.' };
}

function envStep({ config }: GoLiveInput): GoLiveStep {
  const base = { id: 'env' as const, title: 'Account mode' };
  if (config?.accountMode === 'live') {
    return { ...base, state: 'ok', detail: 'The app is already Live.' };
  }
  return { ...base, state: 'apply', detail: 'In Paper. Go live switches the whole app to Live — every armed engine listed below starts spending real money.' };
}

// ── What becomes real money when the app goes Live ──────────────────────────

/** Remote trading is not an engine — nothing trades until the user sends an
 *  order — but it is an arm that becomes real money with the account mode. */
export function remoteTradingArmed(c: Cfg): boolean {
  return !!c?.remoteEnabled && !!c?.remoteTradingEnabled
    && !!(c.remoteDiscordEnabled || c.remoteTelegramEnabled);
}

/** Live-set agents that can actually reach the app: through the MCP server,
 *  or Autopilot's own agent. Empty unless the global trade mode is live. */
export function reachableLiveAgents(c: Cfg): McpAgent[] {
  const live = liveAgents(c);
  if (c?.mcpEnabled) return live;
  if (c?.autopilotEnabled) return live.filter((a) => a.id === (c.autopilotAgentId || DEFAULT_AGENT_ID));
  return [];
}

/**
 * Every arm that becomes real money the moment the app goes Live, one line
 * each. The engines keep their own switches in Paper (they simply send
 * nothing), so a confirm that named only one of them would let the rest
 * start spending unasked.
 */
export function liveSwitchLines(c: Cfg): string[] {
  if (!c) return [];
  const out: string[] = [];
  if (c.crypto15mEnabled) out.push('Crypto (up/down markets): trading on');
  if (c.scriptsLiveEnabled) out.push('Scripts: live orders on (armed scripts send real orders)');
  const reach = reachableLiveAgents(c);
  if (reach.length) {
    const approval = c.mcpLiveApproval === false ? 'approval OFF' : 'each order waits for your approval';
    const names = reach.map((a) => a.name);
    const who = names.length <= 3 ? names.join(', ') : `${names.slice(0, 3).join(', ')} +${names.length - 3}`;
    out.push(`AI agents LIVE: ${who} (${approval}${c.autopilotEnabled ? ', Autopilot on' : ''})`);
  }
  if (remoteTradingArmed(c)) out.push('Remote trading from Discord / Telegram');
  return out;
}

/** The same, as one block of text. */
export function liveSwitchMessage(c: Cfg): string {
  const lines = liveSwitchLines(c).map((l) => `  • ${l}`);
  const head = 'Switch to LIVE (real money)?';
  if (!lines.length) {
    return `${head}\n\nNothing is armed to trade on its own. Orders you place in Live use real funds.`;
  }
  return `${head}\n\nThese are armed and will use REAL money as soon as you switch:\n`
    + `${lines.join('\n')}\n\nCancel and turn any of them off first if that is not what you want.`;
}

// ── The account's Go live (Paper -> Live) ───────────────────────────────────

export interface AccountGoLiveInput {
  config: Cfg;
  creds: CredentialsStatusAll | null;
  verify: KeyVerify | null;
  /** The arms that become real money (liveSwitchLines). */
  armed: string[];
  /** "I understand this uses real money", ticked. */
  ack: boolean;
}

export interface AccountGoLiveStep {
  id: AccountStepId;
  state: StepState;
  title: string;
  detail: string;
}

/**
 * Paper -> Live for the whole app. Three things, each with its real state: an
 * API key saved and tested on a click, the list of every arm that starts
 * spending real money, and an explicit acknowledgement. Going back to Paper
 * needs none of this — narrowing is always safe.
 */
export function accountGoLiveChecklist(i: AccountGoLiveInput): { steps: AccountGoLiveStep[]; ready: boolean } {
  const keys = keyState(i.creds, i.verify);
  const engines: AccountGoLiveStep = i.armed.length
    ? { id: 'engines', state: 'warn', title: 'What starts using real money',
      detail: `${i.armed.length} armed: ${i.armed.join(' · ')}` }
    : { id: 'engines', state: 'ok', title: 'What starts using real money',
      detail: 'Nothing is armed to trade on its own. Orders you place yourself will use real funds.' };
  const ack: AccountGoLiveStep = i.ack
    ? { id: 'ack', state: 'ok', title: 'Real money', detail: 'You confirmed that Live orders spend the real dollars in your account.' }
    : { id: 'ack', state: 'block', title: 'Real money', detail: 'Tick the box to confirm Live orders spend the real dollars in your account.' };
  const steps: AccountGoLiveStep[] = [{ ...keys, id: 'keys' }, engines, ack];
  return { steps, ready: steps.every((s) => s.state !== 'block') };
}

/** The one write the account's Go live makes. Nothing else: every engine's
 *  own switch stays exactly where the user left it. */
export function accountGoLivePatch(): Partial<TraderConfig> {
  return { accountMode: 'live' };
}

/** Back to Paper: the account mode only. The UI confirms it, because Paper
 *  also pauses the engines' exits managing REAL positions, while those
 *  positions stay open on the exchange. */
export function backToPaperAccountPatch(): Partial<TraderConfig> {
  return { accountMode: 'paper' };
}

// ── What the agents' Go live writes ─────────────────────────────────────────

export interface GoLiveDraft {
  caps: AgentCaps;
  approval: boolean;
  approvalOffAck: boolean;
  /** The agents ticked in "Which agents go live". */
  liveAgentIds: string[];
}

/**
 * The ONE config patch the Go live button writes, or `null` when the caps are
 * invalid or no agent is ticked. Only what the final summary showed: the
 * account mode (only if it has to change), trade mode, the caps that differ,
 * the approval switch, and each agent's mode — ticked ones live, every other
 * one paper. Never the MCP server, Autopilot, permissions, the minimum edge,
 * any agent's rules or any other engine.
 *
 * Approval stays ON unless it was turned off through its own confirm.
 */
export function goLivePatch(config: Cfg, draft: GoLiveDraft): Partial<TraderConfig> | null {
  if (capsProblem(draft.caps)) return null;
  const live = new Set(selectedAgents(config, draft.liveAgentIds).map((a) => a.id));
  if (!live.size) return null;
  const patch: Partial<TraderConfig> = { mcpTradeMode: 'live' };
  if (config?.accountMode !== 'live') patch.accountMode = 'live';
  const cur = currentCaps(config);
  for (const k of CAP_KEYS) {
    if (draft.caps[k] !== cur[k]) patch[k] = draft.caps[k];
  }
  patch.mcpLiveApproval = !(draft.approval === false && draft.approvalOffAck);
  patch.mcpAgents = agentsOf(config).map((a) => ({ ...a, mode: live.has(a.id) ? 'live' : 'paper' }));
  return patch;
}

/** Names of the agents that go live with this draft, for the summary. */
export function goLiveAgentNames(config: Cfg, draft: Pick<GoLiveDraft, 'liveAgentIds'>): string[] {
  return selectedAgents(config, draft.liveAgentIds).map((a) => a.name);
}

/** "Back to paper" for the agents: trade mode only. The account mode is left
 *  alone — the user may be Live for the terminal or an engine, and flipping
 *  it here would be a side effect nobody asked for. */
export function backToPaperPatch(): Partial<TraderConfig> {
  return { mcpTradeMode: 'paper' };
}

/** The final confirm's summary of what Go live does. */
export function goLiveSummary(draft: Omit<GoLiveDraft, 'liveAgentIds'> & { liveAgentIds?: string[] },
  agentNames: string[] = []): string {
  const ask = draft.approval || !draft.approvalOffAck
    ? 'each needing your approval'
    : 'WITHOUT asking you per order';
  const head = `Your agents can place real orders on Polymarket US, up to ${usd(draft.caps.mcpDailySpendUsd)}/day `
    + `(${usd(draft.caps.mcpMaxOrderUsd)} per order, at most ${draft.caps.mcpMaxPositions} open positions, `
    + `buys stop after ${usd(draft.caps.mcpDailyLossUsd)} lost in a day), ${ask}.`;
  if (!agentNames.length) return head;
  return `${head} Live: ${agentNames.join(', ')}. Every other agent trades on paper, and each live `
    + 'agent\'s own rules and caps still apply on top.';
}

/** Enabled agents set to live — the ones that spend while the global mode is
 *  live. Empty when the global mode is not live. */
export function liveAgents(c: Cfg): McpAgent[] {
  if (c?.mcpTradeMode !== 'live') return [];
  return agentsOf(c).filter((a) => a.enabled && a.mode === 'live');
}

/** Agents on real money: what the red bar on the AI Agents page keys on. The
 *  app Live, the global mode live, AND at least one agent set live — a global
 *  "live" with every agent on paper spends nothing. */
export function agentsLiveOnLive(c: Cfg): boolean {
  return c?.accountMode === 'live' && liveAgents(c).length > 0;
}

/** Live mode does nothing until an agent can reach the app. */
export function noAgentReachable(c: Cfg): boolean {
  return !c?.mcpEnabled && !c?.autopilotEnabled;
}
