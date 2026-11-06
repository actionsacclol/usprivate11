/**
 * Input sanitising for the agent, AI and profile IPC channels.
 *
 * Kept out of ipc.ts so it can be tested: ipc.ts imports `electron`, which
 * cannot load outside a real Electron process, so everything defined in it is
 * unreachable by a test. These functions stand between a renderer (or an AI
 * agent's settings patch) and a backend that spends money, which makes them
 * the last code in the app that should be untestable.
 *
 * Deliberately dumb: no URLs, no paths, no free-form strings a downstream
 * layer has to interpret. The renderer names an agent, a client, a provider;
 * everything else is clamped or dropped.
 */
import { cleanAgents, isValidAgentId } from '../../shared/agents';
import { isRetiredConfigKey } from './legacy-settings';
import type { McpClientId } from '../../shared/types';

export function clampInt(v: unknown, lo: number, hi: number, dflt: number): number {
  // Absent is absent, whichever way the renderer spells it: Number(null) is 0,
  // which would clamp to the MINIMUM rather than fall back to the default.
  if (v === null || v === undefined || v === '') return dflt;
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(hi, Math.max(lo, Math.round(n)));
}

// ── The user's named agents ──────────────────────────────────────────────────
//
// The agent list is user state that decides what an AI may trade, so it is
// cleaned at this boundary: shared/agents.ts clamps every rule, strips control
// characters from names, keeps Default first, caps the count, and only the
// exact word 'live' is live. The backend validates it again on every load
// (python/mcp_agents.py) — this is main's own check, not a substitute.

export const cleanMcpAgents = cleanAgents;

const TRADE_MODES = new Set(['off', 'paper', 'live']);

/**
 * A renderer `config:update` patch, with the agent keys made safe. A patch
 * whose `mcpAgents` is not a list is DROPPED rather than applied: `undefined`
 * survives structured-clone IPC, and spread over the stored config it would
 * have replaced every agent with a bare Default — deleting the user's agents
 * (and, with pruneAgentTokens, their tokens) because of a renderer bug.
 */
export function cleanConfigPatch<T extends Record<string, unknown>>(patch: T): T {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return {} as T;
  const out: Record<string, unknown> = { ...patch };
  if ('mcpAgents' in out) {
    if (Array.isArray(out.mcpAgents)) out.mcpAgents = cleanAgents(out.mcpAgents);
    else delete out.mcpAgents;
  }
  if ('autopilotAgentId' in out) {
    const id = String(out.autopilotAgentId ?? '').trim().toLowerCase();
    if (isValidAgentId(id)) out.autopilotAgentId = id;
    else delete out.autopilotAgentId;
  }
  if ('mcpTradeMode' in out && !TRADE_MODES.has(String(out.mcpTradeMode))) delete out.mcpTradeMode;
  if ('accountMode' in out && out.accountMode !== 'paper' && out.accountMode !== 'live') delete out.accountMode;
  if ('aiProvider' in out) {
    const p = cleanAiProvider(out.aiProvider);
    if (p) out.aiProvider = p;
    else delete out.aiProvider;
  }
  // Derived from accountMode by the store on every write; a direct write is
  // a stale renderer, and honouring it would let paper and the account mode
  // disagree.
  delete out.terminalPaperMode;
  // Retired keys (legacy-settings migrates them on load). A renderer that
  // still sends one must not resurrect it.
  for (const k of Object.keys(out)) if (isRetiredConfigKey(k)) delete out[k];
  return out as T;
}

/** Agent ids in `before` that `after` no longer has: the agents the user
 *  deleted in this save. Drives setConfig's `pruneAgentTokens`. */
export function removedAgentIds(before: unknown, after: unknown): string[] {
  const ids = (l: unknown): string[] => (Array.isArray(l)
    ? l.map((a) => String((a as { id?: unknown })?.id ?? '').trim().toLowerCase()).filter(Boolean) : []);
  const keep = new Set(ids(after));
  return ids(before).filter((id) => !keep.has(id));
}

/** An agent id named by the renderer (Connect, Rotate, Close paper), or
 *  null. Never defaulted: wiring a click on one agent up as another would
 *  connect the user's client as the wrong trader. */
export function cleanAgentIdArg(v: unknown): string | null {
  if (typeof v !== 'string') return null;
  const id = v.trim().toLowerCase();
  return isValidAgentId(id) ? id : null;
}

export const MCP_CLIENT_IDS: readonly McpClientId[] = [
  'claude-desktop', 'claude-code', 'cursor', 'codex', 'windsurf', 'vscode',
];

export function cleanClientId(v: unknown): McpClientId | null {
  const s = String(v ?? '').trim();
  return (MCP_CLIENT_IDS as readonly string[]).includes(s) ? (s as McpClientId) : null;
}

/** The connection health check's one input. Only a literal `true` runs the
 *  network checks: a truthy string from a buggy caller falls to the free,
 *  local-only run — never the other way. */
export function cleanHealthArgs(raw: unknown): { deep: boolean } {
  if (raw === true) return { deep: true };
  const r = (raw && typeof raw === 'object' && !Array.isArray(raw))
    ? raw as Record<string, unknown> : {};
  return { deep: r.deep === true };
}

// ── An AI agent's settings patch (mcp:configPatch) ───────────────────────────
//
// The backend classifies every key against the user's MCP permissions; this
// is main's own second check, because main owns config and an agent that
// could write the account mode, its own permissions or caps, the Autopilot
// budget, or anything credential-adjacent would have no rails left.

// Paper | Live and the paper bankrolls: an agent that could flip the first
// would arm every engine with real money in one write; one that could refill
// a bankroll could make any paper record look like anything. `network` is
// the venue environment.
const AGENT_NEVER_KEYS = new Set([
  'accountMode', 'terminalPaperMode', 'terminalPaperBankrollUsd', 'mcpPaperBankrollUsd', 'network',
]);

/** Key-level filter: never the keys above, never an mcp / remote / ai /
 *  autopilot / account / terminalMax key, never anything secret-looking. */
export function filterAgentConfigPatch(raw: unknown): Record<string, unknown> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  return Object.fromEntries(Object.entries(raw as Record<string, unknown>).filter(([k]) =>
    !AGENT_NEVER_KEYS.has(k)
    && !/^(mcp|remote|ai|autopilot|account|terminalMax)/.test(k)
    && !/webhook|url|token|discord|telegram|key|secret|private|passphrase|funder/i.test(k)));
}

/**
 * The whole agent patch as main applies it: the key filter, then each value
 * typed against the default config, then the live-switch rule. Without the
 * user's "whole-account powers" switch an agent may only turn things OFF (a
 * literal `false`): pausing is always allowed, arming never is. The backend
 * applies the same rule; this side does not trust the sender.
 */
export function cleanAgentConfigPatch(
  raw: unknown,
  defaults: Record<string, unknown>,
  allowLiveSwitches: boolean,
  log: (key: string, why: string) => void = () => {},
): Record<string, unknown> {
  const src = (raw && typeof raw === 'object' && !Array.isArray(raw)) ? raw as Record<string, unknown> : {};
  const kept = filterAgentConfigPatch(src);
  for (const k of Object.keys(src)) if (!(k in kept)) log(k, 'never patchable by an AI agent');
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(kept)) {
    const def = defaults[k];
    if (def === undefined) { log(k, 'not a known setting'); continue; }
    const typed = def === null ? (v === null || typeof v !== 'object' || Array.isArray(v))
      : Array.isArray(def) ? Array.isArray(v)
        : typeof def === 'object' ? (!!v && typeof v === 'object' && !Array.isArray(v))
          : typeof def === 'number' ? (typeof v === 'number' && Number.isFinite(v))
            : typeof v === typeof def;
    if (!typed) { log(k, `expected ${Array.isArray(def) ? 'array' : def === null ? 'a value' : typeof def}`); continue; }
    if (!allowLiveSwitches && v !== false) { log(k, 'whole-account powers are off for agents'); continue; }
    out[k] = v;
  }
  return out;
}

// ── AI providers ────────────────────────────────────────────────────────────
//
// The renderer names a provider by id. Hosts live in the python backend and
// never cross this boundary. These are allowlists, not defaults: mapping an
// unknown id to Anthropic would file a Gemini key under the Anthropic name
// and send it to the wrong company.

export const AI_PROVIDERS = [
  'anthropic', 'openai', 'openrouter', 'gemini', 'ollama', 'lmstudio',
] as const;
export type AiProviderId = typeof AI_PROVIDERS[number];

/** Providers that take an API key. Ollama and LM Studio run on this machine. */
export const AI_KEYED_PROVIDERS: readonly AiProviderId[] = ['anthropic', 'openai', 'openrouter', 'gemini'];

export function cleanAiProvider(v: unknown): AiProviderId | null {
  const p = String(v ?? '').trim().toLowerCase();
  return (AI_PROVIDERS as readonly string[]).includes(p) ? (p as AiProviderId) : null;
}

export function cleanAiKeyProvider(v: unknown): AiProviderId | null {
  const p = cleanAiProvider(v);
  return p && AI_KEYED_PROVIDERS.includes(p) ? p : null;
}

/**
 * An API key as pasted, or null if it cannot be one.
 *
 * `''` is a real answer — "remove the saved key" — so an INVALID key must not
 * collapse to it: a paste with a stray control character would otherwise
 * silently delete the key the user already had. Keys go into an HTTP header,
 * so anything outside printable ASCII is refused rather than stripped.
 */
export function cleanApiKey(v: unknown): string | null {
  if (v === null || v === undefined) return '';
  const k = String(v).trim();
  if (k === '') return '';
  if (k.length > 400) return null;
  return /^[\x21-\x7e]+$/.test(k) ? k : null;
}

// ── Profiles never carry personal or arm-switch settings ─────────────────────
// A profile is a strategy snapshot, and profiles are exported and shared.
// Agents, Autopilot, remote control, the AI provider, the account mode and its
// paper bankrolls are the user's own setup: never applied from, imported
// with, or exported in a profile (a local save still snapshots them; applying
// filters them out).
const PERSONAL_PREFIXES = ['mcp', 'autopilot', 'remote', 'ai', 'account'];
const PERSONAL_KEYS = new Set([
  'accountMode', 'terminalPaperMode', 'terminalPaperBankrollUsd', 'mcpPaperBankrollUsd',
  'network', 'scriptsLiveEnabled', 'terminalOfflineMode', 'terminalNotifications',
]);

export function isPersonalKey(k: string): boolean {
  return PERSONAL_KEYS.has(k) || PERSONAL_PREFIXES.some((p) => k.startsWith(p)) || /webhook/i.test(k);
}

/** A copy of `config` without any personal / arm-switch key. */
export function omitPersonal<T extends object>(config: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config as Record<string, unknown>)) {
    if (!isPersonalKey(k)) out[k] = v;
  }
  return out as Partial<T>;
}

// The engines' live arm switches: applying a profile never touches these, and
// a shared file shouldn't carry them either, so nobody reads "on" in a file.
const SHARE_EXCLUDE = new Set(['crypto15mEnabled']);

/** What a profile file may carry across machines: the strategy, nothing of
 *  the user's own. Used on EXPORT and on IMPORT. */
export function omitForShare<T extends object>(config: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(config as Record<string, unknown>)) {
    if (!isPersonalKey(k) && !SHARE_EXCLUDE.has(k)) out[k] = v;
  }
  return out as Partial<T>;
}
