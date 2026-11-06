import type { Crypto15mBacktest } from '@shared/types';
import { cleanAgentName, isValidAgentId } from '@shared/agents';

// The in-renderer activity bus: "something the user (or the engine, or an
// agent) just did, and how it came out". Pages publish here AFTER a real
// action completes, with the real result; the Agent Hub listens and stages it.
//
// Why a bus: the hub's rooms used to invent their own work on timers (a
// backtest chamber that re-"ran" saved results every 15 s, an optimizer that
// cycled saved schedules every 20 s), because nothing told them when real
// work happened elsewhere in the app. Now every room is fed by the action
// itself, and a room with nothing to show idles.
//
// What this is NOT:
//   - not telemetry (invariant 4): it sends nothing anywhere, persists
//     nothing, and lives only in this renderer's memory. Closing the app
//     forgets it. There is no network, IPC, file or storage call in here;
//   - not an action path: an event is a past-tense description. Nothing that
//     listens can turn one into an order, a config write or a backend call
//     (invariant 10);
//   - not allowed to break a page: `publishActivity` swallows every error,
//     including one thrown while BUILDING the payload, and a listener that
//     throws is isolated from the other listeners and from the publisher.
//
// A value nobody produced stays `null` (invariant 1): a backtest over zero
// trades has no edge and no win rate, not 0¢ and 0%.

export type BacktestEngine = '15m' | 'script';

export type ActivityEvent =
  | {
    kind: 'backtest';
    engine: BacktestEngine;
    /** What was replayed: a preset, a profile, a script, "current settings". */
    strategy: string;
    days: number | null;
    trades: number | null;
    /** Net cents per share after fees; null over zero trades. */
    netCents: number | null;
    /** 0..100; null over zero trades. */
    winRatePct: number | null;
    totalPnlUsd: number | null;
    /** t-statistic when the engine reports one (script backtests). */
    t: number | null;
    /** The run's real equity curve, downsampled; empty when it has none. */
    equity: number[];
  }
  | {
    kind: 'optimizer';
    coin: string;
    granularityH: number | null;
    days: number | null;
    swept: number | null;
    /** Best whole-day strategy for the coin, if any cleared the bar. */
    winner: string | null;
    winnerNetCents: number | null;
    /** The assembled per-hour schedule: only buckets that elected a winner. */
    slots: { start: number; end: number; name: string }[];
  }
  | {
    kind: 'script';
    op: 'saved' | 'created' | 'validated' | 'enabled' | 'disabled' | 'autoDisabled';
    name: string | null;
    id: string | null;
    ok: boolean;
    errors: number | null;
    /** The engine's reason, for an auto-disable. */
    detail: string | null;
  }
  | { kind: 'preset'; what: 'strategy' | 'profile'; name: string; ok: boolean }
  | {
    kind: 'manualOrder';
    op: 'submit' | 'close' | 'cancel';
    ok: boolean;
    /** Which market (slug) and its title, for the label. The
     *  numbers below are Polymarket's answer, never the request. */
    conditionId: string | null;
    title?: string | null;
    side: 'yes' | 'no' | null;
    action: 'buy' | 'sell' | null;
    status: string | null;
    filled: number | null;
    avgCents: number | null;
    message: string;
  }
  | {
    kind: 'aiAnalysis';
    conditionId: string;
    title: string | null;
    provider: string;
    verdict: 'cheap' | 'rich' | 'fair' | 'unclear';
    /** 1..99 or null: the model declined to commit, or the backend refused
     *  a number outside the range (invariant 8). */
    fairCents: number | null;
    confidence: 'low' | 'medium' | 'high';
  }
  | { kind: 'rule'; ruleKind: 'stop' | 'take' | 'alert' | null; conditionId: string | null; title?: string | null; message: string }
  | {
    kind: 'agent';
    mode: 'paper' | 'live' | 'action' | 'off';
    message: string;
    /** The approval this is about: a live agent order waiting for the user
     *  (decision null), or the user's answer to one. */
    approvalId: number | null;
    decision: 'approved' | 'rejected' | null;
  }
  | { kind: 'halt'; scope: 'daily' | 'c15TakeProfit'; reason: string }
  | AgentCallActivity;

/** One of the user's own agents calling a tool (or connecting), as pushed by
 *  the backend's dispatcher (`mcp:toolCall`). The backend already built every
 *  field from a whitelist and dropped anything credential-shaped; this keeps
 *  it that way by re-reading each field by type (agentCallEvent). */
export interface AgentCallActivity {
  kind: 'agentCall';
  call: 'call' | 'connect';
  client: string;
  transport: string | null;
  model: string | null;
  tool: string | null;
  outcome: 'ok' | 'refused' | 'error';
  reason: string | null;
  summary: string;
  /** The market it was about (slug, shape-checked), if any. */
  conditionId: string | null;
  /** That market's title when the backend sent one. */
  title: string | null;
  /** 1..99, or null when it named none. */
  fairCents: number | null;
  edgeCents: number | null;
  /** The mid a forecast was argued against; null = no two-sided quote. */
  midCents: number | null;
  side: 'yes' | 'no' | null;
  mode: 'paper' | 'live' | null;
  durationMs: number | null;
  /** Calls the backend's throttle dropped just before this one. */
  suppressed: number;
  /** The named agent the caller's TOKEN resolved to (shared/agents.ts), so
   *  the hub can show the user's own agent as itself. Absent on an older
   *  backend's events: those keep the client/model identity. */
  agentId?: string | null;
  /** That agent's name when the event left the backend — user text,
   *  re-cleaned here. The hub prefers the CONFIG's name for the same id. */
  agentName?: string | null;
}

export type ActivityKind = ActivityEvent['kind'];

export interface ActivityRecord {
  /** Monotonic per bus, so a listener can tell a replay from a repeat. */
  seq: number;
  /** Epoch ms when the action COMPLETED (when it was published). */
  at: number;
  ev: ActivityEvent;
}

export type ActivityListener = (rec: ActivityRecord) => void;

export const ACTIVITY_CAP = 200;
/** Agent tool calls get their own ring: an agent working a market list makes
 *  a call every second or two, and in a shared ring it would push the
 *  backtest the user just ran out of the hub's catch-up. */
export const AGENT_CALL_CAP = 120;

export interface ActivityBus {
  publish(ev: ActivityEvent, now?: number): ActivityRecord;
  subscribe(fn: ActivityListener): () => void;
  /** Records from the last `windowMs`, OLDEST first. */
  recent(windowMs: number, now?: number): ActivityRecord[];
  size(): number;
  clear(): void;
}

/** A fixed ring: the oldest record is overwritten, so a chatty source can
 *  never grow the renderer's memory. */
function ring(cap: number) {
  const slots: (ActivityRecord | undefined)[] = new Array(cap);
  let head = 0;
  let count = 0;
  return {
    push(rec: ActivityRecord) {
      slots[head] = rec;
      head = (head + 1) % cap;
      count = Math.min(cap, count + 1);
    },
    each(fn: (r: ActivityRecord) => void) {
      for (let i = 0; i < count; i++) {
        const r = slots[(head - count + i + cap) % cap];
        if (r) fn(r);
      }
    },
    get count() { return count; },
    clear() { slots.fill(undefined); head = 0; count = 0; },
  };
}

export function createActivityBus(cap = ACTIVITY_CAP, agentCap = AGENT_CALL_CAP): ActivityBus {
  const main = ring(cap);
  const calls = ring(agentCap);
  let seq = 0;
  const listeners = new Set<ActivityListener>();
  return {
    publish(ev, now = Date.now()) {
      const rec: ActivityRecord = { seq: ++seq, at: now, ev };
      (ev.kind === 'agentCall' ? calls : main).push(rec);
      for (const fn of [...listeners]) {
        // one broken listener must not starve the others or the publisher
        try { fn(rec); } catch { /* isolated */ }
      }
      return rec;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => { listeners.delete(fn); };
    },
    recent(windowMs, now = Date.now()) {
      const out: ActivityRecord[] = [];
      const take = (r: ActivityRecord) => { if (now - r.at <= windowMs && r.at <= now) out.push(r); };
      main.each(take);
      calls.each(take);
      return out.sort((a, b) => a.seq - b.seq);
    },
    size: () => main.count + calls.count,
    clear() {
      main.clear();
      calls.clear();
    },
  };
}

/** The app's one bus. */
export const activityBus: ActivityBus = createActivityBus();

/** The call-site helper. Pass a THUNK so that building the payload (which
 *  reads a backend result of whatever shape came back) is inside the same
 *  try: publishing is a side effect and must never change what the page
 *  does, even when it throws. A thunk returning null publishes nothing. */
export function publishActivity(build: ActivityEvent | (() => ActivityEvent | null), bus: ActivityBus = activityBus): void {
  try {
    const ev = typeof build === 'function' ? build() : build;
    if (ev) bus.publish(ev);
  } catch { /* never the publisher's problem */ }
}

// ── catch-up ────────────────────────────────────────────────────────────────

export const REPLAY_WINDOW_MS = 3 * 60_000;
export const REPLAY_BEAT_MS = 1_400;
export const REPLAY_MAX = 12;

/** What happened just before the hub opened, as quick beats: oldest first,
 *  one every `beatMs`, so the ship visibly does it rather than everything
 *  landing in one frame. Only the newest `max` are kept: a busy three
 *  minutes compresses to its latest beats, never to a minute of replay. */
export function replayPlan(
  records: ActivityRecord[], opts: { beatMs?: number; max?: number; firstMs?: number } = {},
): { rec: ActivityRecord; delayMs: number }[] {
  const beat = opts.beatMs ?? REPLAY_BEAT_MS;
  const max = opts.max ?? REPLAY_MAX;
  const first = opts.firstMs ?? 600;
  const keep = [...records].sort((a, b) => a.at - b.at || a.seq - b.seq).slice(-max);
  return keep.map((rec, i) => ({ rec, delayMs: first + i * beat }));
}

// ── payload builders (pure; shared by every call site) ──────────────────────

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** At most `n` points of a real curve, evenly sampled, last point kept. */
export function downsample(xs: number[], n = 48): number[] {
  const clean = xs.filter((x) => Number.isFinite(x));
  if (clean.length <= n) return clean;
  const out: number[] = [];
  for (let i = 0; i < n; i++) out.push(clean[Math.round((i * (clean.length - 1)) / (n - 1))]);
  return out;
}

export function backtestEvent(
  engine: BacktestEngine, strategy: string, days: number | null,
  r: (Crypto15mBacktest & { tStat?: number | null }) | null | undefined,
): ActivityEvent | null {
  if (!r) return null; // no result (backend down) is not a run
  const n = num(r.n);
  const traded = n !== null && n > 0;
  return {
    kind: 'backtest', engine, strategy, days,
    trades: n,
    netCents: traded ? num(r.netEvCentsPerContract) : null,
    winRatePct: traded && num(r.winRate) !== null ? (r.winRate as number) * 100 : null,
    totalPnlUsd: traded ? num(r.totalPnlUsd) : null,
    t: num(r.tStat),
    equity: Array.isArray(r.equity) ? downsample(r.equity.map((p) => p?.value as number)) : [],
  };
}

/** The shape an optimizer run reports (kryptrader's per-coin hour optimizer).
 *  Polybot has no optimizer page yet; the event and the hub room are kept so
 *  a future one only has to publish. Structural, so no backend type is
 *  assumed. */
export interface OptimizerResultLike {
  coin: string;
  granularityH?: number | null;
  sinceDays?: number | null;
  strategiesSwept?: number | null;
  coinWinner?: { name: string; n: number; netCents?: number | null } | null;
  schedule?: { start: number; end: number; winner?: string | null }[] | null;
}

export function optimizerEvent(r: OptimizerResultLike | null | undefined): ActivityEvent | null {
  if (!r) return null;
  const w = r.coinWinner;
  return {
    kind: 'optimizer',
    coin: r.coin,
    granularityH: num(r.granularityH),
    days: num(r.sinceDays),
    swept: num(r.strategiesSwept),
    winner: w?.name ?? null,
    winnerNetCents: w && w.n > 0 ? num(w.netCents) : null,
    slots: (r.schedule ?? [])
      .filter((s) => !!s.winner)
      .map((s) => ({ start: s.start, end: s.end, name: s.winner as string })),
  };
}

/** A standing rule's push carries the rule row; only the label fields are
 *  read, and only when they have the expected shape. */
export function ruleEvent(d: { rule?: unknown; message?: unknown } | null | undefined): ActivityEvent | null {
  if (!d || typeof d.message !== 'string') return null;
  const rule = (d.rule && typeof d.rule === 'object') ? d.rule as Record<string, unknown> : {};
  const k = rule.kind;
  return {
    kind: 'rule',
    ruleKind: k === 'stop' || k === 'take' || k === 'alert' ? k : null,
    conditionId: conditionIdOf(rule.conditionId),
    title: str(rule.title, 120),
    message: d.message,
  };
}

/** A market id on this venue is a slug, e.g. btc-updown-15m-1730000000.
 *  Anything not slug-shaped is no market. */
export function conditionIdOf(v: unknown): string | null {
  return typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(v.trim()) ? v.trim() : null;
}

function str(v: unknown, max: number): string | null {
  if (typeof v !== 'string') return null;
  const t = v.replace(/\s+/g, ' ').trim();
  if (!t) return null;
  return t.length > max ? `${t.slice(0, max - 1)}…` : t;
}

/** The `mcp:toolCall` push, re-read field by field. Anything of the wrong
 *  type is absent, never coerced: a fair value outside 1..99 is null (not a
 *  price), an unknown outcome is not "ok". */
export function agentCallEvent(d: unknown): AgentCallActivity | null {
  if (!d || typeof d !== 'object') return null;
  const o = d as Record<string, unknown>;
  const client = str(o.client, 60);
  const summary = str(o.summary, 120);
  if (!client || !summary) return null;
  const outcome = o.outcome === 'ok' || o.outcome === 'refused' || o.outcome === 'error' ? o.outcome : null;
  if (!outcome) return null;
  const fair = num(o.fairCents);
  const mid = num(o.midCents);
  const dur = num(o.durationMs);
  const sup = num(o.suppressed);
  return {
    kind: 'agentCall',
    call: o.kind === 'connect' ? 'connect' : 'call',
    client,
    transport: str(o.transport, 24),
    model: str(o.model, 40),
    tool: str(o.tool, 48),
    outcome,
    reason: str(o.reason, 160),
    summary,
    conditionId: conditionIdOf(o.conditionId),
    title: str(o.title, 120),
    fairCents: fair !== null && fair >= 1 && fair <= 99 ? fair : null,
    edgeCents: num(o.edgeCents),
    midCents: mid !== null && mid >= 1 && mid <= 99 ? mid : null,
    side: o.side === 'yes' || o.side === 'no' ? o.side : null,
    mode: o.mode === 'paper' || o.mode === 'live' ? o.mode : null,
    durationMs: dur !== null && dur >= 0 ? Math.round(dur) : null,
    suppressed: sup !== null && sup > 0 ? Math.trunc(sup) : 0,
    // an id that is not an id is no agent (the client/model identity stands
    // in); a name is user text that rode the same event as a model's fields
    agentId: isValidAgentId(o.agentId) ? o.agentId : null,
    agentName: typeof o.agentName === 'string' ? cleanAgentName(o.agentName, '') || null : null,
  };
}

export function agentEvent(d: { mode?: unknown; message?: unknown; approvalId?: unknown } | null | undefined): ActivityEvent | null {
  if (!d || typeof d.message !== 'string') return null;
  const m = d.mode;
  const id = num(d.approvalId);
  return {
    kind: 'agent',
    mode: m === 'paper' || m === 'live' || m === 'action' || m === 'off' ? m : 'action',
    message: d.message,
    approvalId: id !== null ? Math.trunc(id) : null,
    decision: null,
  };
}
