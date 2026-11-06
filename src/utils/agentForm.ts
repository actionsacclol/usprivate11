import {
  AGENT_GUIDE_MAX, AGENT_NAME_MAX, cleanAgent, type AgentCategory, type AgentMode,
  type AgentSides, type McpAgent,
} from '@shared/agents';

/**
 * The agent editor's form, and the one mapping from it to what is stored.
 *
 * Numbers are kept as the strings the user typed, because "" means NO RULE
 * and must never become 0 — a max-positions of 0 is a forecast-only agent, a
 * blank one is "the global cap applies". Validation says what is wrong in
 * words next to the field; the mapping then runs the same cleaner main and
 * the backend use, so what the editor shows is what gets enforced.
 */

export interface AgentForm {
  name: string;
  emoji: string;
  color: string;
  guide: string;
  mode: AgentMode;
  enabled: boolean;
  categoriesAllow: AgentCategory[];
  categoriesDeny: AgentCategory[];
  minPriceCents: string;
  maxPriceCents: string;
  minHoursToClose: string;
  maxHoursToClose: string;
  sides: AgentSides;
  maxSharesPerMarket: string;
  maxOpenPositions: string;
  minEdgeCents: string;
  dailySpendUsd: string;
  maxOrderUsd: string;
}

export type AgentFormErrors = Partial<Record<keyof AgentForm, string>>;

const s = (v: number | null): string => (v === null ? '' : String(v));

export function agentToForm(a: McpAgent): AgentForm {
  const r = a.rules;
  return {
    name: a.name, emoji: a.emoji, color: a.color, guide: a.guide, mode: a.mode, enabled: a.enabled,
    categoriesAllow: [...(r.categoriesAllow ?? [])], categoriesDeny: [...(r.categoriesDeny ?? [])],
    minPriceCents: s(r.minPriceCents), maxPriceCents: s(r.maxPriceCents),
    minHoursToClose: s(r.minHoursToClose), maxHoursToClose: s(r.maxHoursToClose),
    sides: r.sides, maxSharesPerMarket: s(r.maxSharesPerMarket),
    maxOpenPositions: s(r.maxOpenPositions), minEdgeCents: s(r.minEdgeCents),
    dailySpendUsd: s(r.dailySpendUsd), maxOrderUsd: s(r.maxOrderUsd),
  };
}

/** "" -> null (no rule); a number -> that number; anything else -> NaN. */
export function parseField(v: string): number | null {
  const t = v.trim();
  if (t === '') return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : Number.NaN;
}

type Spec = { lo: number; hi: number; int?: boolean; label: string };
const NUMERIC: Partial<Record<keyof AgentForm, Spec>> = {
  minPriceCents: { lo: 1, hi: 99, int: true, label: 'A price from 1 to 99¢' },
  maxPriceCents: { lo: 1, hi: 99, int: true, label: 'A price from 1 to 99¢' },
  minHoursToClose: { lo: 0, hi: 24 * 365, label: 'Hours, 0 to 8760' },
  maxHoursToClose: { lo: 0, hi: 24 * 365, label: 'Hours, 0 to 8760' },
  maxSharesPerMarket: { lo: 0, hi: 1_000_000, int: true, label: 'A whole number of shares, 0 or more' },
  maxOpenPositions: { lo: 0, hi: 200, int: true, label: 'A whole number from 0 to 200' },
  minEdgeCents: { lo: 0, hi: 50, label: 'Cents, 0 to 50' },
  dailySpendUsd: { lo: 0, hi: 1e7, label: 'Dollars, 0 or more' },
  maxOrderUsd: { lo: 0, hi: 1e6, label: 'Dollars, 0 or more' },
};

export function validateAgentForm(f: AgentForm, otherNames: string[] = []): AgentFormErrors {
  const e: AgentFormErrors = {};
  const name = f.name.trim();
  if (!name) e.name = 'Give it a name.';
  else if (Array.from(name).length > AGENT_NAME_MAX) e.name = `At most ${AGENT_NAME_MAX} characters.`;
  else if (otherNames.some((n) => n.trim().toLowerCase() === name.toLowerCase())) {
    e.name = 'Another agent already has this name.';
  }
  if (Array.from(f.guide).length > AGENT_GUIDE_MAX) e.guide = `At most ${AGENT_GUIDE_MAX} characters.`;
  for (const [key, spec] of Object.entries(NUMERIC) as [keyof AgentForm, Spec][]) {
    const n = parseField(f[key] as string);
    if (n === null) continue;
    if (Number.isNaN(n) || n < spec.lo || n > spec.hi || (spec.int && !Number.isInteger(n))) {
      e[key] = spec.label + '.';
    }
  }
  const lo = parseField(f.minPriceCents);
  const hi = parseField(f.maxPriceCents);
  if (!e.minPriceCents && !e.maxPriceCents && lo !== null && hi !== null && lo > hi) {
    e.maxPriceCents = 'The max must be at least the min.';
  }
  const hlo = parseField(f.minHoursToClose);
  const hhi = parseField(f.maxHoursToClose);
  if (!e.minHoursToClose && !e.maxHoursToClose && hlo !== null && hhi !== null && hlo > hhi) {
    e.maxHoursToClose = 'The max must be at least the min.';
  }
  const both = f.categoriesAllow.filter((c) => f.categoriesDeny.includes(c));
  if (both.length) e.categoriesDeny = 'A category cannot be both allowed and blocked.';
  return e;
}

export function formIsValid(e: AgentFormErrors): boolean {
  return Object.keys(e).length === 0;
}

/** The stored agent: the base's id and createdAt, everything else from the
 *  form, through the shared cleaner. Throws on an invalid form — the editor
 *  never calls it with one. */
export function formToAgent(f: AgentForm, base: McpAgent, now = new Date()): McpAgent {
  const n = (v: string): number | null => parseField(v);
  const a = cleanAgent({
    id: base.id, createdAt: base.createdAt ?? now.toISOString(), updatedAt: now.toISOString(),
    name: f.name, emoji: f.emoji, color: f.color, guide: f.guide, mode: f.mode, enabled: f.enabled,
    rules: {
      categoriesAllow: f.categoriesAllow.length ? f.categoriesAllow : null,
      categoriesDeny: f.categoriesDeny.length ? f.categoriesDeny : null,
      minPriceCents: n(f.minPriceCents), maxPriceCents: n(f.maxPriceCents),
      minHoursToClose: n(f.minHoursToClose), maxHoursToClose: n(f.maxHoursToClose),
      sides: f.sides, maxSharesPerMarket: n(f.maxSharesPerMarket),
      maxOpenPositions: n(f.maxOpenPositions), minEdgeCents: n(f.minEdgeCents),
      dailySpendUsd: n(f.dailySpendUsd), maxOrderUsd: n(f.maxOrderUsd),
    },
  });
  if (!a) throw new Error('invalid agent');
  return a;
}

/** Replace one agent in the list (or append it), keeping Default first. */
export function upsertAgent(list: McpAgent[], a: McpAgent): McpAgent[] {
  const i = list.findIndex((x) => x.id === a.id);
  if (i === -1) return [...list, a];
  const next = [...list];
  next[i] = a;
  return next;
}
