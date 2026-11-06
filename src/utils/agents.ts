// Who an AI agent is, from what the backend knows about it: the MCP
// handshake's client name ("claude-code 2.1"), Autopilot's own tag
// ("autopilot (claude-opus-5)"), the model it named in record_forecast, and —
// for the Analyse button — the model that answered. One pure mapping, used by
// onboarding (did THIS client connect?), the AI Agents page, the Agent Hub's
// crew and the poker table's seats, so the same agent has the same name and
// colour everywhere.
//
// Every input string already passed the backend's shape and secret checks
// (mcp_server.safe_label); this only names and colours. It never invents a
// model: an agent that never said which model it runs has none on its tag.

import {
  AGENT_COLORS, DEFAULT_AGENT_COLOR, DEFAULT_AGENT_ID, agentsOf, cleanAgentName, isValidAgentId,
  type McpAgent,
} from '@shared/agents';

export type AgentKind =
  | 'claude-code' | 'cursor' | 'claude-desktop' | 'codex' | 'autopilot' | 'http' | 'analyse' | 'other';

export interface AgentIdentity {
  /** One per client: a crew member. */
  id: string;
  /** One per client AND model: a poker seat ("each distinct client/model"). */
  seatId: string;
  kind: AgentKind;
  /** "Claude Code", "Cursor", "Autopilot", "Analyse". */
  name: string;
  /** Short model ("opus", "gpt-5"); null when nobody said. */
  model: string | null;
  /** "Claude Code · opus". */
  label: string;
  color: string;
  /** A named agent's own emoji; absent for a client identity. */
  emoji?: string;
  /** A short phrase from a named agent's guide — the USER's words, never a
   *  number (guideMotto) — or null when none fits. */
  motto?: string | null;
}

const NAME: Record<AgentKind, string> = {
  'claude-code': 'Claude Code', cursor: 'Cursor', 'claude-desktop': 'Claude Desktop', codex: 'Codex',
  autopilot: 'Autopilot', http: 'HTTP agent', analyse: 'Analyse', other: 'Agent',
};

/** Suit colours. Distinct at a 30px figure size, and none of them the bot
 *  crew's role colours, so an agent never reads as a bot worker. */
export const AGENT_COLOR: Record<AgentKind, string> = {
  'claude-code': '#E07A50',
  cursor: '#E2E8F0',
  'claude-desktop': '#F2C46D',
  codex: '#34D399',
  autopilot: '#C084FC',
  http: '#38BDF8',
  analyse: '#FDE047',
  other: '#94A3B8',
};

const OTHER_PALETTE = ['#94A3B8', '#F472B6', '#A3E635', '#FB923C', '#67E8F9', '#FCA5A5'];

export function kindOf(client: string | null | undefined, transport?: string | null): AgentKind {
  const c = (client ?? '').toLowerCase();
  if (c.startsWith('autopilot')) return 'autopilot';
  if (c.includes('claude-code') || c.includes('claude code')) return 'claude-code';
  if (c.includes('cursor')) return 'cursor';
  if (c.includes('claude-ai') || c.includes('claude desktop') || c.includes('claude-desktop')) return 'claude-desktop';
  if (c.includes('codex')) return 'codex';
  if (transport && transport !== 'mcp') return 'http';
  return 'other';
}

/** "claude-opus-4-1" → "opus"; "openai/gpt-5-mini" → "gpt-5-mini". */
export function shortModel(m: string | null | undefined): string | null {
  const raw = (m ?? '').trim();
  if (!raw) return null;
  const s = raw.toLowerCase().split('/').pop() ?? raw.toLowerCase();
  for (const fam of ['opus', 'sonnet', 'haiku']) if (s.includes(fam)) return fam;
  return s.length > 16 ? `${s.slice(0, 15)}…` : s;
}

/** The family the Analyse button's label names: "Claude", "GPT", "Gemini". */
export function modelFamily(m: string | null | undefined): string | null {
  const s = (m ?? '').toLowerCase();
  if (!s) return null;
  if (s.includes('claude') || /opus|sonnet|haiku/.test(s)) return 'Claude';
  if (s.includes('gpt') || /(^|\/)o\d/.test(s)) return 'GPT';
  if (s.includes('gemini')) return 'Gemini';
  return shortModel(m);
}

function hashIdx(s: string, n: number): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h) % n;
}

function bareName(client: string): string {
  // "my-agent 0.3.1" → "my-agent": the version is not part of who it is
  const first = client.trim().split(/\s+/)[0] ?? '';
  if (!first || first === 'unknown') return 'Agent';
  return first.length > 18 ? `${first.slice(0, 17)}…` : first;
}

export function agentIdentity(src: {
  client?: string | null; model?: string | null; source?: 'panel' | 'mcp' | null; transport?: string | null;
}): AgentIdentity {
  if (src.source === 'panel') {
    const fam = modelFamily(src.model);
    const label = `Analyse · ${fam ?? 'AI'}`;
    return {
      id: 'analyse', seatId: `analyse|${fam ?? ''}`, kind: 'analyse', name: 'Analyse',
      model: fam, label, color: AGENT_COLOR.analyse,
    };
  }
  let client = (src.client ?? '').trim();
  let model = src.model ?? null;
  // Agent forecasts recorded before the client had its own column stored the
  // client in `model`.
  if (!client && model && kindOf(model) !== 'other') {
    client = model;
    model = null;
  }
  const kind = kindOf(client, src.transport);
  if (kind === 'autopilot' && !model) {
    const m = /\(([^)]+)\)/.exec(client);
    model = m ? m[1] : null;
  }
  const short = shortModel(model);
  const name = kind === 'other' || kind === 'http' ? bareName(client) : NAME[kind];
  const id = `${kind}|${kind === 'other' || kind === 'http' ? name : ''}`;
  const color = kind === 'other' ? OTHER_PALETTE[hashIdx(name, OTHER_PALETTE.length)] : AGENT_COLOR[kind];
  return {
    id, seatId: `${id}|${short ?? ''}`, kind, name, model: short,
    label: short ? `${name} · ${short}` : name, color,
  };
}

// ── the user's NAMED agents (shared/agents.ts) ──────────────────────────────

/** A name tag over a 30px figure and a poker seat plate have room for this many
 *  code points; the stored cap (AGENT_NAME_MAX, 40) is for the agent card. */
export const HUB_NAME_MAX = 20;
/** A guide phrase on a poker seat: a few words, not the guide. */
export const MOTTO_MAX = 32;
const COLOR_RE = /^#[0-9A-Fa-f]{6}$/;
// What shared/agents strips from a name, for the emoji too (ZWJ stays: it
// holds a family or a flag together).
// eslint-disable-next-line no-control-regex
const CTRL = /[\u0000-\u001f\u007f-\u009f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

/** A name fit for the hub: control characters and bidi overrides out (a name
 *  arrives in config AND on the tool-call stream, and a forged line or a
 *  reversed run of text must not reach a canvas), one line, and short. */
export function hubName(raw: unknown, fallback = 'Agent'): string {
  const s = cleanAgentName(raw, fallback);
  const cps = Array.from(s);
  return cps.length > HUB_NAME_MAX ? `${cps.slice(0, HUB_NAME_MAX - 1).join('').trimEnd()}…` : s;
}

function hubEmoji(raw: unknown): string | undefined {
  const s = Array.from(String(raw ?? '').replace(CTRL, '').trim()).slice(0, 8).join('');
  return s || undefined;
}

/**
 * A few words of the agent's guide for its poker seat: the user's own
 * description of it ("You are a patient researcher." → "I'm a patient
 * researcher"). Never a number — a seat's only number is its forecast, and a
 * guide line like "buy under 30¢" read out beside it would look like one —
 * so a phrase with a digit is skipped. null when nothing fits.
 */
export function guideMotto(guide: unknown): string | null {
  const text = String(guide ?? '').replace(CTRL, '\n');
  for (const part of text.split(/[\n.!?;:]+/).slice(0, 4)) {
    let p = part.replace(/^[\s\-*•>#]+/, '').replace(/["“”‘’`*_]/g, '').replace(/\s+/g, ' ').trim();
    if (Array.from(p).length < 4 || /\d/.test(p)) continue;
    // the guide talks TO the agent; its seat speaks as itself
    p = p.replace(/^you are\b/i, "I'm").replace(/^you're\b/i, "I'm").replace(/^you\b/i, 'I');
    let out = '';
    for (const w of p.split(' ')) {
      const next = out ? `${out} ${w}` : w;
      if (Array.from(next).length > MOTTO_MAX) break;
      out = next;
    }
    if (!out) continue; // one enormous word: nothing readable to quote
    return out.length < p.length ? `${out.replace(/[,\s]+$/, '')}…` : out;
  }
  return null;
}

/**
 * A named agent's identity, keyed by agent id — the identity its TOKEN
 * resolved to, which the tool-call stream carries as `agentId`. One crew
 * member per agent (`id`, whatever model drives it); `seatId` keeps the model
 * in it, like agentIdentity's: one named agent driven by two models is two
 * forecasters on the scoreboard. Name, emoji and colour are re-cleaned here,
 * whichever door they came through.
 */
export function namedAgentIdentity(
  agent: { id: string; name: string; color: string; emoji?: string; guide?: string },
  model?: string | null,
): AgentIdentity {
  const short = shortModel(model);
  const name = hubName(agent.name);
  return {
    id: `agent|${agent.id}`,
    seatId: `agent|${agent.id}|${short ?? ''}`,
    kind: 'other',
    name,
    model: short,
    label: short ? `${name} · ${short}` : name,
    color: COLOR_RE.test(agent.color) ? agent.color : AGENT_COLORS[hashIdx(agent.id, AGENT_COLORS.length)],
    emoji: hubEmoji(agent.emoji),
    motto: guideMotto(agent.guide),
  };
}

/** A forecaster whose agent no longer exists in the config: its calls are
 *  real and stay on the scoreboard, under no name it no longer has. */
const DELETED_AGENT = { name: 'Deleted agent', color: '#64748B' };

/** The configured agents, or null while there is no config yet — then no id
 *  can be told apart and everything keeps its client/model identity. */
export function configuredAgents(config: Parameters<typeof agentsOf>[0]): McpAgent[] | null {
  return config ? agentsOf(config) : null;
}

/**
 * Who did it, for the Agent Hub: the user's named agent when the event (or
 * forecast row) says which, else the client/model identity. Used for the
 * Casino's crew, the poker table's seats and the AI Agents page's lists, so a
 * named agent reads the same everywhere.
 *
 *  - No agent id (an older backend, the Analyse button) → agentIdentity.
 *  - The Default agent keeps the identity it always had ("Claude Code ·
 *    opus") unless the user RENAMED it; then it is that name.
 *  - Any other id: the CONFIG's name, emoji and colour for that id; failing
 *    that, the name the event carried (backend-scrubbed, re-cleaned here);
 *    failing that, "Deleted agent" — never another agent's identity.
 */
export function hubAgentIdentity(
  src: {
    agentId?: string | null; agentName?: string | null;
    client?: string | null; model?: string | null; source?: 'panel' | 'mcp' | null; transport?: string | null;
  },
  agents: readonly McpAgent[] | null | undefined,
): AgentIdentity {
  const aid = src.source !== 'panel' && isValidAgentId(src.agentId) ? src.agentId : null;
  if (!aid) return agentIdentity(src);
  const a = agents?.find((x) => x.id === aid) ?? null;
  if (aid === DEFAULT_AGENT_ID) {
    const name = a ? a.name : (src.agentName ?? null);
    if (!name || name === 'Default') return agentIdentity(src);
    return namedAgentIdentity({
      id: aid, name, color: a?.color ?? DEFAULT_AGENT_COLOR, emoji: a?.emoji, guide: a?.guide,
    }, src.model);
  }
  if (a) return namedAgentIdentity(a, src.model);
  if (src.agentName) return namedAgentIdentity({ id: aid, name: src.agentName, color: '' }, src.model);
  if (agents) return namedAgentIdentity({ id: aid, ...DELETED_AGENT }, src.model);
  return agentIdentity(src);
}

/** The name tag / plate text: the agent's emoji, then its label. */
export function tagLabel(who: AgentIdentity): string {
  return who.emoji ? `${who.emoji} ${who.label}` : who.label;
}

/** The onboarding picker's ids → the kind its handshake reports. */
export const PICKER_KIND = {
  'claude-code': 'claude-code', cursor: 'cursor', 'claude-desktop': 'claude-desktop', codex: 'codex',
  windsurf: 'other', vscode: 'other',
} as const satisfies Record<string, AgentKind>;

/** Does a client name from the MCP handshake look like the client the user
 *  picked? Windsurf and VS Code have no kind of their own, so their names are
 *  matched as words. */
export function clientMatchesPick(client: string | null | undefined, pick: keyof typeof PICKER_KIND): boolean {
  const c = (client ?? '').toLowerCase();
  if (pick === 'windsurf') return c.includes('windsurf') || c.includes('codeium');
  if (pick === 'vscode') return c.includes('visual studio code') || c.includes('vscode') || c.includes('copilot');
  return kindOf(client) === PICKER_KIND[pick];
}
