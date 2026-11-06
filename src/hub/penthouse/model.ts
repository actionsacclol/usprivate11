import {
  DEFAULT_AGENT_ID, effectiveAgentMode, effectiveCaps, isValidAgentId, rulesInWords,
  type GlobalTradeMode, type McpAgent,
} from '@shared/agents';
import type { TraderConfig } from '@shared/types';
import type { HubAgentScore, HubForecastRow, HubScoreboard } from '../data';
import type { ActivityRecord, AgentCallActivity } from '../../state/activity';
import { configuredAgents, hubAgentIdentity, namedAgentIdentity, type AgentIdentity } from '../../utils/agents';

// The Penthouse's books: one desk per agent the USER set up (config
// mcpAgents), and what each desk's monitors show. Pure, three-free and
// window-free, so the screens' numbers are pinned by tests rather than eyeballed.
//
// Every number here came from somewhere real:
//   - tool calls: the backend's `mcp:toolCall` push, re-read by type on the
//     activity bus (state/activity.ts). Renderer memory only, so "this session"
//     means since the app started;
//   - the forecast record: forecast_ledger.scoreboard (local SQLite), the
//     same read High Stakes makes;
//   - mode, rules and caps: the config, through the same helpers the AI Agents
//     page uses (shared/agents.ts).
// A value nobody produced stays null and is drawn as a dash.

export type ToolGroup = 'research' | 'forecast' | 'order' | 'account' | 'build' | 'other';

/** The MCP server's tools (python/mcp_tools.py), by what the work is. A tool
 *  this build doesn't know is 'other', never guessed into a group. */
const GROUPS: Record<Exclude<ToolGroup, 'other'>, readonly string[]> = {
  research: [
    'discover_markets', 'search_markets', 'get_market', 'get_order_book', 'get_price_history',
    'get_resolution_risk', 'get_related_markets', 'get_cross_venue',
    'manage_watchlist',
  ],
  forecast: ['record_forecast'],
  order: [
    'preview_order', 'place_order', 'cancel_order', 'close_position', 'cancel_all_orders',
    'flatten_account', 'flatten_wallet', 'manage_standing_instructions',
  ],
  account: ['get_status', 'get_my_agent', 'get_portfolio', 'get_orders', 'get_trade_history', 'get_engine_activity'],
  build: [
    'pause_all_engines', 'set_engines', 'update_config',
    'backtest_script', 'parlay_schedule', 'research_data', 'get_script_docs', 'list_scripts',
    'validate_script', 'save_script', 'set_script_state',
  ],
};
const GROUP_OF = new Map<string, ToolGroup>(
  Object.entries(GROUPS).flatMap(([g, tools]) => tools.map((t) => [t, g as ToolGroup] as const)),
);

export function toolGroup(tool: string | null | undefined): ToolGroup {
  return (tool && GROUP_OF.get(tool)) || 'other';
}

/** Calls that send, cancel or arm something — the ORDERS list. A preview is
 *  listed too (marked as one): it is the agent sizing an order. */
export function isOrderTool(tool: string | null | undefined): boolean {
  return toolGroup(tool) === 'order';
}

export interface DeskCall {
  seq: number;
  /** Epoch ms the call finished (the bus record's time). */
  at: number;
  connect: boolean;
  tool: string | null;
  group: ToolGroup;
  summary: string;
  outcome: 'ok' | 'refused' | 'error';
  reason: string | null;
  mode: 'paper' | 'live' | null;
  conditionId: string | null;
  title: string | null;
  fairCents: number | null;
  midCents: number | null;
  edgeCents: number | null;
  side: 'yes' | 'no' | null;
  suppressed: number;
  durationMs: number | null;
}

/** The market the agent touched last, with its own fair value when the call
 *  that touched it (or an earlier one on the same market) recorded one. */
export interface DeskMarket {
  conditionId: string;
  title: string | null;
  fairCents: number | null;
  midCents: number | null;
  edgeCents: number | null;
  side: 'yes' | 'no' | null;
  /** When it was last touched, and by which tool. */
  at: number;
  tool: string | null;
  /** When the fair value above was recorded (null: none on this market). */
  forecastAt: number | null;
}

export interface Desk {
  /** The config agent id. */
  id: string;
  agent: McpAgent;
  who: AgentIdentity;
  /** What it actually trades in: the global agent mode, the account mode and
   *  its own switch (effectiveAgentMode), and 'off' when it or the MCP server
   *  is switched off. */
  mode: GlobalTradeMode;
  enabled: boolean;
  rules: string[];
  caps: { maxOrderUsd: number; dailySpendUsd: number; minEdgeCents: number };
  /** Newest first. */
  calls: DeskCall[];
  /** Newest first. */
  orders: DeskCall[];
  /** Call times (epoch ms) and outcomes for the activity chart, oldest first. */
  pulse: { at: number; outcome: DeskCall['outcome'] }[];
  ok: number;
  refused: number;
  error: number;
  /** Calls the backend's throttle folded into the ones above. */
  suppressed: number;
  lastAt: number | null;
  connectAt: number | null;
  client: string | null;
  model: string | null;
  market: DeskMarket | null;
  score: HubAgentScore | null;
  /** This agent's own forecasts, newest first. */
  forecasts: HubForecastRow[];
  /** Bumps on every change a monitor shows. */
  rev: number;
}

export const CALLS_KEPT = 40;
export const ORDERS_KEPT = 10;
export const FORECASTS_KEPT = 10;
export const PULSE_WINDOW_MS = 30 * 60_000;

function deskFor(agent: McpAgent): Desk {
  return {
    id: agent.id, agent, who: namedAgentIdentity({ ...agent, name: agent.name || 'Default' }),
    mode: 'off', enabled: agent.enabled !== false, rules: [],
    caps: { maxOrderUsd: 0, dailySpendUsd: 0, minEdgeCents: 0 },
    calls: [], orders: [], pulse: [], ok: 0, refused: 0, error: 0, suppressed: 0,
    lastAt: null, connectAt: null, client: null, model: null, market: null,
    score: null, forecasts: [], rev: 0,
  };
}

/** The Default agent's name until the user renames it. */
function unrenamed(a: McpAgent): boolean {
  return a.id === DEFAULT_AGENT_ID && (!a.name || a.name === 'Default');
}

export class DeskBook {
  private desks = new Map<string, Desk>();
  /** The desks shown, in the config's order. */
  private shown: string[] = [];
  private seen = new Set<number>();
  private agents: McpAgent[] | null = null;
  private config: TraderConfig | null = null;
  private board: HubScoreboard | null = null;
  /** Forecasts recorded since the scoreboard was last read, per desk. */
  private live = new Map<string, HubForecastRow[]>();
  /** Bumps when the set of desks (or their order) changes. */
  roster = 0;

  /** The config's agents, mode and rules. → whether the desks changed. */
  sync(config: TraderConfig | null | undefined): boolean {
    this.config = config ?? null;
    const agents = configuredAgents(config ?? null);
    this.agents = agents;
    if (!agents) return false;
    const live = new Set(agents.map((a) => a.id));
    for (const id of [...this.desks.keys()]) if (!live.has(id)) this.desks.delete(id);
    for (const a of agents) {
      let d = this.desks.get(a.id);
      if (!d) { d = deskFor(a); this.desks.set(a.id, d); }
      d.agent = a;
      this.refresh(d);
    }
    return this.reorder();
  }

  /** One activity record. → the desk it landed on, or null (not an agent
   *  call, a repeat, or an agent the user has since deleted). */
  call(rec: ActivityRecord): Desk | null {
    if (rec.ev.kind !== 'agentCall' || this.seen.has(rec.seq)) return null;
    this.seen.add(rec.seq);
    if (this.seen.size > 4000) this.seen = new Set([...this.seen].slice(-2000));
    const ev: AgentCallActivity = rec.ev;
    // An older backend's event carries no agent id: it was the Default agent.
    const id = isValidAgentId(ev.agentId) ? ev.agentId : DEFAULT_AGENT_ID;
    const d = this.desks.get(id);
    if (!d) return null;
    if (ev.client) d.client = ev.client;
    if (ev.model) d.model = ev.model;
    if (ev.call === 'connect') {
      d.connectAt = Math.max(d.connectAt ?? 0, rec.at);
      this.refresh(d);
      this.reorder();
      return d;
    }
    const c: DeskCall = {
      seq: rec.seq, at: rec.at, connect: false, tool: ev.tool, group: toolGroup(ev.tool),
      summary: ev.summary, outcome: ev.outcome, reason: ev.reason, mode: ev.mode,
      conditionId: ev.conditionId, title: ev.title, fairCents: ev.fairCents, midCents: ev.midCents,
      edgeCents: ev.edgeCents, side: ev.side, suppressed: ev.suppressed, durationMs: ev.durationMs,
    };
    insertNewestFirst(d.calls, c, CALLS_KEPT);
    if (isOrderTool(c.tool)) insertNewestFirst(d.orders, c, ORDERS_KEPT);
    d.pulse.push({ at: c.at, outcome: c.outcome });
    d.pulse.sort((a, b) => a.at - b.at);
    const cut = Math.max(...d.pulse.map((p) => p.at)) - PULSE_WINDOW_MS;
    while (d.pulse.length && d.pulse[0].at < cut) d.pulse.shift();
    if (d.pulse.length > 1200) d.pulse.splice(0, d.pulse.length - 1200);
    d[c.outcome]++;
    d.suppressed += Math.max(0, c.suppressed);
    d.lastAt = Math.max(d.lastAt ?? 0, c.at);
    if (c.conditionId && c.outcome === 'ok' && (!d.market || c.at >= d.market.at)) {
      d.market = nextMarket(d.market, c);
    }
    if (c.tool === 'record_forecast' && c.outcome === 'ok' && c.conditionId && c.fairCents !== null) {
      const rows = this.live.get(d.id) ?? [];
      rows.unshift({
        createdAt: new Date(c.at).toISOString(), conditionId: c.conditionId, title: c.title,
        model: d.model, client: d.client, agentId: d.id, fairValueCents: c.fairCents,
        marketMidCents: c.midCents, outcome: null,
      });
      this.live.set(d.id, rows.slice(0, FORECASTS_KEPT));
    }
    this.refresh(d);
    this.reorder();
    return d;
  }

  /** The forecast scoreboard. undefined (the scene didn't read one) keeps
   *  what the desks have. */
  scoreboard(b: HubScoreboard | null | undefined): void {
    if (b === undefined) return;
    this.board = b;
    for (const d of this.desks.values()) this.score(d);
    this.reorder();
  }

  /** The desks, in the config's order. */
  list(): Desk[] {
    return this.shown.map((id) => this.desks.get(id)).filter((d): d is Desk => !!d);
  }

  get(id: string): Desk | undefined {
    return this.desks.get(id);
  }

  /** Every call on every desk, newest first. */
  stream(limit: number): (DeskCall & { desk: Desk })[] {
    const out: (DeskCall & { desk: Desk })[] = [];
    for (const d of this.list()) for (const c of d.calls) out.push({ ...c, desk: d });
    return out.sort((a, b) => b.at - a.at || b.seq - a.seq).slice(0, limit);
  }

  private refresh(d: Desk): void {
    const cfg = this.config;
    const a = d.agent;
    d.enabled = a.enabled !== false;
    const global = cfg?.mcpTradeMode;
    d.mode = !d.enabled || cfg?.mcpEnabled === false
      ? 'off' : effectiveAgentMode(global, a, cfg?.accountMode);
    d.rules = rulesInWords(a.rules);
    const caps = effectiveCaps(cfg, a.rules);
    d.caps = { maxOrderUsd: caps.maxOrderUsd, dailySpendUsd: caps.dailySpendUsd, minEdgeCents: caps.minEdgeCents };
    // The unrenamed Default agent goes by the client that drives it ("Claude
    // Code · opus"), as everywhere else in the hub; a named one is itself.
    d.who = unrenamed(a) && d.client
      ? hubAgentIdentity({ agentId: a.id, client: d.client, model: d.model }, this.agents)
      : namedAgentIdentity({ ...a, name: a.name || 'Default' }, d.model);
    this.score(d);
    d.rev++;
  }

  private score(d: Desk): void {
    const b = this.board;
    const board = !b ? [] : (b.recent ?? [])
      // forecasts recorded before agents had ids were the Default agent's
      .filter((r) => (r.agentId ? r.agentId === d.id : d.id === DEFAULT_AGENT_ID));
    if (b) d.score = b.byAgent?.find((s) => s.agentId === d.id) ?? null;
    // one just recorded and not on the scoreboard yet shows as pending
    const at = (r: HubForecastRow) => Date.parse(r.createdAt ?? '') || 0;
    const fresh = (this.live.get(d.id) ?? []).filter((l) => !board.some((r) => r.conditionId === l.conditionId
      && r.fairValueCents === l.fairValueCents && Math.abs(at(r) - at(l)) < 180_000));
    this.live.set(d.id, fresh);
    d.forecasts = [...fresh, ...board].sort((x, y) => at(y) - at(x)).slice(0, FORECASTS_KEPT);
    if (d.market && !d.market.title) {
      d.market.title = d.forecasts.find((r) => r.conditionId === d.market!.conditionId && r.title)?.title ?? null;
    }
    d.rev++;
  }

  /** Which desks show: every agent the user set up. The Default agent is the
   *  one the app makes on its own, so its desk shows once the user renames
   *  it, once it has done anything, or when it is the only agent there is. */
  private reorder(): boolean {
    const agents = this.agents ?? [];
    const ids = agents.filter((a) => {
      if (!unrenamed(a)) return true;
      const d = this.desks.get(a.id);
      const active = !!d && (d.calls.length > 0 || d.connectAt !== null || d.forecasts.length > 0 || (d.score?.total ?? d.score?.n ?? 0) > 0);
      return active || agents.length === 1;
    }).map((a) => a.id);
    const changed = ids.length !== this.shown.length || ids.some((id, i) => this.shown[i] !== id);
    if (changed) { this.shown = ids; this.roster++; }
    return changed;
  }
}

function insertNewestFirst(list: DeskCall[], c: DeskCall, cap: number): void {
  let i = 0;
  while (i < list.length && (list[i].at > c.at || (list[i].at === c.at && list[i].seq > c.seq))) i++;
  list.splice(i, 0, c);
  if (list.length > cap) list.length = cap;
}

function nextMarket(prev: DeskMarket | null, c: DeskCall): DeskMarket {
  const same = prev?.conditionId === c.conditionId;
  const forecast = c.tool === 'record_forecast' && c.fairCents !== null;
  return {
    conditionId: c.conditionId as string,
    title: c.title ?? (same ? prev!.title : null),
    // a later read of the same market keeps the agent's number on it
    fairCents: forecast ? c.fairCents : same ? prev!.fairCents : null,
    midCents: c.midCents ?? (same ? prev!.midCents : null),
    edgeCents: forecast ? c.edgeCents : same ? prev!.edgeCents : null,
    side: forecast ? c.side : same ? prev!.side : null,
    at: c.at,
    tool: c.tool,
    forecastAt: forecast ? c.at : same ? prev!.forecastAt : null,
  };
}

/** Calls per minute over the last `minutes`, oldest bucket first, split by
 *  outcome — the activity strip's bars. */
export function pulseBuckets(
  pulse: Desk['pulse'], now: number, minutes = 30,
): { ok: number; refused: number; error: number }[] {
  const out = Array.from({ length: minutes }, () => ({ ok: 0, refused: 0, error: 0 }));
  for (const p of pulse) {
    const k = Math.floor((now - p.at) / 60_000);
    if (k < 0 || k >= minutes) continue;
    out[minutes - 1 - k][p.outcome]++;
  }
  return out;
}

/** "12s", "4m", "3h", "2d": how long ago, short enough for a monitor row. */
export function agoShort(at: number | null, now: number): string {
  if (at === null) return '—';
  const s = Math.max(0, Math.round((now - at) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86_400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86_400)}d`;
}

/** The forecast scorer's verdict, in the words a monitor shows. */
export function verdictText(v: HubAgentScore['verdict'] | undefined): { text: string; tone: 'win' | 'loss' | 'flat' } {
  switch (v) {
    case 'agent-better': return { text: 'BEATING THE MARKET', tone: 'win' };
    case 'market-better': return { text: 'MARKET IS AHEAD', tone: 'loss' };
    case 'indistinguishable': return { text: 'TOO CLOSE TO CALL', tone: 'flat' };
    default: return { text: 'NEEDS MORE SETTLED', tone: 'flat' };
  }
}
