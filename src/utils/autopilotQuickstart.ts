import type { TraderConfig } from '@shared/types';
import type { AiProvider } from '@shared/market';
import { DEFAULT_AGENT_ID, isValidAgentId } from '@shared/agents';

/**
 * The Autopilot quickstart's content and its one config write, kept pure so
 * the part that decides what reaches settings is testable without a window.
 *
 * The quickstart writes NOTHING until its last screen. It then sends exactly
 * one patch through `config.update` (the same path, and the same backend
 * validation, as the Autopilot panel's own inputs), and that patch can only
 * ever say paper: `buildQuickstartPatch` has no way to express live, and a
 * test holds it to that.
 */

export interface ProviderInfo {
  id: AiProvider;
  name: string;
  /** 'key' = cloud account with an API key; 'local' = a program on this PC. */
  needs: 'key' | 'local';
  /** Where to get the key, or the app. Hardcoded official pages only. */
  getUrl: string;
  getLabel: string;
  blurb: string;
  keyHint?: string;
}

export const PROVIDERS: ProviderInfo[] = [
  {
    id: 'anthropic', name: 'Claude', needs: 'key',
    getUrl: 'https://platform.claude.com/settings/keys', getLabel: 'Get a Claude key',
    blurb: 'Anthropic. Strong at reading resolution rules and reasoning. Pay per use.',
    keyHint: 'sk-ant-…',
  },
  {
    id: 'openai', name: 'OpenAI', needs: 'key',
    getUrl: 'https://platform.openai.com/api-keys', getLabel: 'Get an OpenAI key',
    blurb: 'GPT models. Pay per use.',
    keyHint: 'sk-…',
  },
  {
    id: 'openrouter', name: 'OpenRouter', needs: 'key',
    getUrl: 'https://openrouter.ai/settings/keys', getLabel: 'Get an OpenRouter key',
    blurb: 'One key for hundreds of models. Reports what each run really cost.',
    keyHint: 'sk-or-…',
  },
  {
    id: 'gemini', name: 'Gemini', needs: 'key',
    getUrl: 'https://aistudio.google.com/app/apikey', getLabel: 'Get a Gemini key',
    blurb: 'Google. Has a free tier with tight limits.',
    keyHint: 'AIza…',
  },
  {
    id: 'ollama', name: 'Ollama', needs: 'local',
    getUrl: 'https://ollama.com/download', getLabel: 'Download Ollama',
    blurb: 'Runs a model on this computer. Free, private, needs a decent PC.',
  },
  {
    id: 'lmstudio', name: 'LM Studio', needs: 'local',
    getUrl: 'https://lmstudio.ai/download', getLabel: 'Download LM Studio',
    blurb: 'Runs a model on this computer with a friendly app. Free and private.',
  },
];

export const LOCAL_HOWTO: Partial<Record<AiProvider, string[]>> = {
  ollama: [
    'Install Ollama and open it.',
    'Download a model that can use tools. In a terminal: ollama pull llama3.1:8b',
    'Come back here and press "Check again".',
  ],
  lmstudio: [
    'Install LM Studio and download a model.',
    'Open the Developer tab and start the local server.',
    'Come back here and press "Check again".',
  ],
};

export interface Mission {
  id: string;
  title: string;
  blurb: string;
  text: string;
}

/**
 * Missions are instructions to the model, not rails. Every one is written to
 * fit the rails it runs under anyway (a forecast before any buy, an edge that
 * must clear Polymarket's fee, paper fills against the real book) and none
 * promises a profit. The tool names are the real ones in python/mcp_tools.py.
 */
export const MISSIONS: Mission[] = [
  {
    id: 'closing',
    title: 'Closing soon: forecast, then paper-trade',
    blurb: 'Looks at markets closing soon. Trades on paper only when its edge is real.',
    text:
      'Look at markets that close soon (discover_markets, column "closing"). Pick three to five '
      + 'you can actually reason about and read each one\'s resolution rules with get_market and '
      + 'get_resolution_risk. Record a calibrated forecast (record_forecast) for every market you '
      + 'form a view on, including the ones you decide not to trade. Paper-trade only where '
      + 'record_forecast shows your edge after Polymarket\'s fee clearing the minimum, a few shares '
      + 'at a time. Most markets should end with a forecast and no trade. In your report, list '
      + 'each forecast next to the market price.',
  },
  {
    id: 'positions',
    title: 'Watch my agent\'s positions',
    blurb: 'Re-checks what the agent already holds and flags risks. Opens nothing new.',
    text:
      'Review the open positions in your own account (get_portfolio). For each one, re-read the '
      + 'market and its rules with get_market, note what has changed since it was opened, and '
      + 'record an updated forecast. Flag anything risky in your report: a price that has moved '
      + 'hard against the position, a resolution coming up soon, or rules that could resolve it in '
      + 'a surprising way. Do not open new positions. Only sell (close_position) if your updated '
      + 'forecast says a position is clearly worth less than the current bid.',
  },
  {
    id: 'crypto',
    title: 'Crypto up/down: forecast only',
    blurb: 'Forecasts the fast Bitcoin and Ethereum up/down markets. Never places an order.',
    text:
      'Look at the recurring crypto Up-or-Down markets (discover_markets, column "crypto_updown"). '
      + 'For a few Bitcoin and Ethereum windows, read the rules with get_market: which price source '
      + 'resolves it, and at what time. Then record a forecast with your reasoning. Do not place '
      + 'any orders: this mission is only to learn whether your forecasts beat the market price on '
      + 'fast markets. Keep the report short: market, your fair value, the market price.',
  },
  {
    id: 'research',
    title: 'Research only, no trades',
    blurb: 'Forecasts busy markets and reports where it disagrees with the price.',
    text:
      'Browse high-volume and volatile markets (discover_markets, columns "volume" and '
      + '"volatility"). Pick a handful you can reason about, read their rules, and record honest '
      + 'forecasts. Do not place any orders. In your report, list the three markets where your '
      + 'forecast differs most from the price, and say plainly why you might be wrong.',
  },
];

export type BudgetId = 'light' | 'standard' | 'heavy';

export interface BudgetPreset {
  id: BudgetId;
  label: string;
  blurb: string;
  intervalMin: number;
  maxRunsPerDay: number;
  dailyTokenBudget: number;
  maxSteps: number;
}

/** All inside the Autopilot panel's own input bounds (15-1440 min, 1-96
 *  runs, 50k-50M tokens, 3-40 steps), so nothing is clamped on the way in. */
export const BUDGETS: BudgetPreset[] = [
  {
    id: 'light', label: 'Light', blurb: 'A few careful looks a day. Cheapest.',
    intervalMin: 240, maxRunsPerDay: 3, dailyTokenBudget: 300_000, maxSteps: 8,
  },
  {
    id: 'standard', label: 'Standard', blurb: 'Every two hours, a solid look each time.',
    intervalMin: 120, maxRunsPerDay: 6, dailyTokenBudget: 1_000_000, maxSteps: 12,
  },
  {
    id: 'heavy', label: 'Heavy', blurb: 'Hourly and thorough. Costs the most.',
    intervalMin: 60, maxRunsPerDay: 12, dailyTokenBudget: 3_000_000, maxSteps: 20,
  },
];

/** Agent runs are mostly input: every tool result is read back in. */
export const INPUT_SHARE = 0.9;

/**
 * Dollars if a whole day's token budget is spent, at a published price, or
 * null when there is no published price. The budget is a hard cap enforced
 * before each call, so this is the most a day can cost at that price (give
 * or take one call's overshoot), not a typical day.
 */
export function maxDailyCostUsd(
  tokensPerDay: number,
  price: { inPerMTok: number; outPerMTok: number } | undefined | null,
): number | null {
  if (!price || !Number.isFinite(tokensPerDay) || tokensPerDay <= 0) return null;
  const perM = INPUT_SHARE * price.inPerMTok + (1 - INPUT_SHARE) * price.outPerMTok;
  return Math.round((tokensPerDay / 1e6) * perM * 100) / 100;
}

export function costLine(args: {
  provider: AiProvider;
  model: string;
  budget: BudgetPreset;
  prices?: Record<string, { inPerMTok: number; outPerMTok: number }>;
}): string {
  const info = PROVIDERS.find((p) => p.id === args.provider);
  if (info?.needs === 'local') return 'Free: it runs on your computer.';
  const usd = maxDailyCostUsd(args.budget.dailyTokenBudget, args.prices?.[args.model]);
  if (usd !== null) {
    return `At most about $${usd.toFixed(2)} a day on ${args.model}, if it uses the whole budget. `
      + 'Most days use less.';
  }
  if (args.provider === 'openrouter') {
    return 'Cost depends on the model you picked. OpenRouter reports what each run really cost.';
  }
  return 'Cost depends on your provider\'s pricing. The token budget is the hard cap.';
}

export const MISSION_MAX = 2000;

/**
 * The one config write the quickstart makes. Paper, always: `mcpTradeMode`
 * is written as 'paper' whatever it was, including 'live' (Autopilot uses the
 * same mode as every connected agent, so leaving live in place would make a
 * "quickstart" a live trader). The caller says so on screen before the user
 * confirms. It never writes the account mode.
 */
export function buildQuickstartPatch(args: {
  provider: AiProvider;
  model: string;
  mission: string;
  budget: BudgetPreset;
  /** The named agent Autopilot runs as (its guide and rules apply). Default
   *  when not given. Never changes that agent's own mode: under the paper
   *  trade mode this patch writes, every agent trades paper. */
  agentId?: string;
}): Partial<TraderConfig> {
  const clamp = (v: number, lo: number, hi: number): number =>
    Math.min(hi, Math.max(lo, Math.round(v)));
  return {
    aiProvider: args.provider,
    aiModel: args.model.trim(),
    autopilotMission: args.mission.trim().slice(0, MISSION_MAX),
    autopilotIntervalMin: clamp(args.budget.intervalMin, 15, 1440),
    autopilotMaxRunsPerDay: clamp(args.budget.maxRunsPerDay, 1, 96),
    autopilotDailyTokenBudget: clamp(args.budget.dailyTokenBudget, 50_000, 50_000_000),
    autopilotMaxSteps: clamp(args.budget.maxSteps, 3, 40),
    mcpTradeMode: 'paper',
    autopilotEnabled: true,
    autopilotAgentId: isValidAgentId(args.agentId) ? args.agentId : DEFAULT_AGENT_ID,
  };
}
