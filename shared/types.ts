// ────────────────────────────────────────────────────────────────────────
// Shared types between Electron main / preload / renderer.
// All IPC payloads should be declared here and re-exported via the
// `KryptApi` interface.
// ────────────────────────────────────────────────────────────────────────

// The TERMINAL contract lives in `shared/market.ts` and is deliberately kept
// separate from the types below, which are the ENGINE contract. Only the
// KryptApi surface at the bottom of this file joins them.
import type {
  AccountMode, AgentPaper, AiAnalysis, AiProvider, AiProviderCheck, AiStatus,
  AutopilotStatus, ForecastScoreboard, HealthReport,
  McpActivity, McpAgentStatus, McpLossToday, McpOrderEvent, McpOrderRow, McpRails, McpSeen,
  McpToolCallEvent, McpTradeMode, PaperStatus,
  BookSnapshot, DiscoverColumn, DiscoverFilters, DiscoverResult,
  FlattenPreview, FlattenResult,
  ManualOrderPreview, ManualOrderRequest, ManualOrderResult, MarketSummary,
  CrossVenueComparison,
  PaperOrder, PaperOrderResult, PaperPortfolio, RelatedMarkets,
  PriceSeries, ResolutionReport, RestingOrder,
  SellPositionRequest, SellPreview, SellResult,
  SettledRecord, StandingInstruction,
  TerminalFill, TerminalHealth, TerminalInterval, TerminalPortfolio,
  WatchEntry,
} from './market';
import type { McpAgent } from './agents';

// One environment: the Polymarket US production exchange.
export type Network = 'mainnet';

/** Our own locally-timestamped quote samples for one market. */
export interface MicroSeries {
  conditionId: string;
  side: 'yes' | 'no';
  points: { t: number; bid: number | null; ask: number | null; spread: number | null }[];
  medianSpreadCents: number | null;
  sampleCount: number;
  source: string;
  note: string | null;
}

/** A manual order attempt as recorded locally — failures included. */
export interface TerminalOrderLogRow {
  localId: number;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  orderType: string;
  requestedSize: number;
  requestedPriceCents: number;
  sentSize: number | null;
  sentPriceCents: number | null;
  orderId: string | null;
  status: string;
  error: string | null;
  filledSize: number | null;
  avgFillPriceCents: number | null;
  feesUsd: number | null;
  reconciled: boolean;
  origin: string;
  standingId: string | null;
  createdAt: string;
}

/** One paired chat account allowed to command this app. */
export interface RemotePairedAccount {
  id: string;
  label: string | null;
  pairedAt: string;
}

/** One row of the remote-command audit trail — refusals included. */
export interface RemoteAuditRow {
  at: string;
  platform: string;
  sender: string;
  command: string;
  /** 'accepted' | 'refused' | 'unauthorised' | 'rate-limited' | 'error' */
  outcome: string;
  detail: string;
  /** Unpaired senders only: one row per sender, with how many messages it
   *  sent and when the first one came. */
  count?: number;
  firstAt?: string;
}

export interface RemotePlatformStatus {
  connected: boolean;
  lastError: string | null;
  paired: RemotePairedAccount[];
  hasToken: boolean;
  /** Masked shape of the stored token, never the token itself. */
  tokenMasked?: string | null;
  botUsername?: string | null;
}

export interface RemoteStatus {
  discord: RemotePlatformStatus;
  telegram: RemotePlatformStatus;
  audit: RemoteAuditRow[];
  fetchedAt: string;
}

// ───────── AI agents over MCP (python/mcp_server.py) ───────────────────

export interface McpSession {
  id: string;
  /** The client's own name from its MCP `initialize` (Claude, Cursor, …). */
  client: string;
  version: string | null;
  protocol: string | null;
  connectedAt: string;
  lastSeen: string;
  reads: number;
  writes: number;
  refused: number;
}

export interface McpAuditRow {
  at: string;
  client: string;
  tool: string;
  outcome: 'accepted' | 'refused' | 'failed' | 'error' | string;
  detail: string;
}

/** python mcp_server.status(). Never a token — masked shapes only. */
export interface McpStatus {
  enabled: boolean;
  running: boolean;
  port: number;
  url: string;
  lastError: string | null;
  /** The Default agent's token exists (never the token). */
  hasToken: boolean;
  tokenMasked: string | null;
  /** The global agent trade mode as the backend applies it (account mode
   *  included: Paper ⇒ never 'live'). Absent on a backend from before. */
  tradeMode?: McpTradeMode;
  rails?: McpRails;
  /** "Ask me first" is on. */
  approvals?: boolean;
  agents?: McpAgentStatus[];
  /** Live agent orders waiting for the user. */
  pendingApprovals?: McpOrderRow[];
  sessions: McpSession[];
  toolCount: number;
  /** BUY spend by agents since 00:00 UTC. null = unreadable. */
  spentTodayUsd: number | null;
  /** Today's loss for the stop. */
  lossTodayUsd?: number | null;
  lossToday?: McpLossToday | null;
  /** The HTTP API (/api/v1) is switched on. */
  httpEnabled?: boolean;
  /** Its base URL on the loopback listener. */
  httpUrl?: string;
  /** The account mode the backend applies (Paper wins over everything). */
  accountMode?: AccountMode;
  audit: McpAuditRow[];
  fetchedAt: string;
}

export type McpClientId =
  | 'claude-desktop' | 'claude-code' | 'cursor' | 'codex' | 'windsurf' | 'vscode';

export interface McpClientSetup {
  id: McpClientId;
  label: string;
  /** The agent this setup is for, and the server name it is filed under. */
  agentId: string;
  serverName: string;
  /** Where the app would write the entry; null = copy/paste only. */
  configPath: string | null;
  /** Every file one click would write (Claude Desktop: normal + Store build). */
  configPaths?: string[];
  /** The client looks installed (its config folder exists). */
  detected: boolean;
  /** This agent's entry is already in its config file. */
  installed: boolean;
  /** One click can write it (else copy/paste). */
  canInstall: boolean;
  /** Text to paste (a config block or a shell command). No token, ever. */
  snippet: string;
  snippetKind: 'json' | 'toml' | 'shell';
  note?: string;
}

export interface McpInstallResult {
  ok: boolean;
  message: string;
  files?: string[];
  backups?: string[];
}

/** Pushed when a standing instruction fires (or is blocked from firing). */
export interface StandingEvent {
  id: string;
  conditionId: string;
  title: string | null;
  kind: string;
  priceCents: number;
  triggerCents: number;
  blocked?: boolean;
  result?: string;
  ok?: boolean;
}

export type SignalSource = 'crypto' | 'script' | 'external' | (string & {});

// ───────── Trader configuration (mirrors python/config.py) ─────────────

/** One composed entry condition in the rule builder: `<field> <op> <value>`.
 *  field = a crypto snapshot key (favoritePrice, bookImbalance, minsLeft, …). */
export interface RuleCondition {
  field: string;
  op: '>=' | '<=' | '>' | '<';
  value: number;
}

export interface TraderConfig {
  // Environment
  network: Network;

  // Read by the engines that remain (crypto, scripts, the backend's loops).
  minCashReserveFraction: number;
  maxTotalExposureFraction: number;
  positionPollInterval: number;
  balancePollInterval: number;
  resolutionCheckInterval: number;
  marketRefreshInterval: number;
  startBankrollUsd: number;
  stopLossOnDay: number; // negative number, e.g. -50.0 USD

  /** Hours of history the dashboard P&L chart covers. */
  statsChartWindowHours: number;

  // 15-minute crypto strategy (optional — backend fills any missing key
  // from its crypto15m_* defaults). All are editable from the 15m tab.
  crypto15mEnabled?: boolean;
  crypto15mInterval?: '5m' | '15m' | 'hourly';   // which recurring up/down window to monitor
  crypto15mAssets?: string[] | null;             // which crypto series to trade (null/undefined = all)
  crypto15mArbDetect?: boolean;                  // detect Up+Down ≠ $1 arbitrage
  crypto15mArbMinEdgeCents?: number;             // flag arb when the gross edge >= this (cents)
  crypto15mImbalanceDetect?: boolean;            // compute the order-flow / book-imbalance signal
  crypto15mImbalanceLevels?: number;             // order-book depth levels to sum
  crypto15mImbalanceGate?: boolean;              // require imbalance to confirm the entry side
  crypto15mImbalanceGateMin?: number;            // confirmation strength 0..1
  crypto15mIndicatorDetect?: boolean;            // compute underlying MACD/RSI (rule fields, detection-only)
  crypto15mWsBook?: boolean;                     // real-time book over the exchange's WebSocket (needs an API key; display-only)
  crypto15mUseRules?: boolean;                   // use the composed rule-set as the entry gate
  crypto15mRules?: RuleCondition[];              // composed entry conditions (all AND-ed)
  crypto15mSizingMode?: 'fixed' | 'balance_pct'; // fixed contracts vs % of balance
  crypto15mOrderSize?: number;                   // contracts per entry (fixed mode)
  crypto15mBalancePct?: number;                  // fraction of balance per bet (balance_pct mode)
  crypto15mMaxLossPct?: number;                  // cap risk/bet to this fraction of balance (0 = off)
  crypto15mStreakSizing?: boolean;               // scale next bet by the current settled win/loss streak
  crypto15mStreakLossPct?: number;               // % size change per consecutive loss (compounds; e.g. 20 = +20%/loss)
  crypto15mStreakWinPct?: number;                // % size change per consecutive win (0 = a win resets to base)
  crypto15mStreakMaxMult?: number;               // hard cap on the streak multiplier (x base size)
  crypto15mMaxConcurrent?: number;
  crypto15mDailyLossLimit?: number;              // halt NEW crypto15m entries once today's realized P&L <= this (<=0; 0 = off)
  crypto15mLifetimeLossLimitPct?: number;        // pause the engine once cumulative realized loss >= this fraction of start bankroll (0–1; default 0.5; 0 = off)
  crypto15mLifetimeLossLimitUsd?: number;        // absolute-$ override for the lifetime breaker (>0 takes precedence over the %; 0 = use %)
  crypto15mTakeProfitTotal?: number;             // turn the crypto engine OFF once realized P&L gained since enabling >= this ($; 0 = off)
  crypto15mDirectionMode?: 'favorite' | 'contrarian' | 'model';
  // ── Settlement sniper (model direction mode) ─────────────────────────
  crypto15mModelMinProb?: number;        // enter only when the model calls the side >= this probability (0.5–1)
  crypto15mModelMinEdgeCents?: number;   // require this much fee-net edge vs the book (cents)
  crypto15mModelFinalMinute?: boolean;   // allow final-minute strikes (3-sigma + live-WS-spot + runway guards)
  crypto15mModelAutopause?: boolean;     // calibration guard: auto-pause model entries when the rolling hit rate degrades
  crypto15mModelMaxBookGapCents?: number; // skip when the model claims more edge than this vs a live book outside the final minute (0 = off)
  crypto15mSpotWs?: boolean;             // live spot price over WebSocket (model input; REST stays the fallback)
  crypto15mSellIntoStrength?: boolean;       // rest a GTC ask at the strength target on fill (maker; sells spikes between ticks)
  crypto15mSellStrengthCents?: number;       // the resting ask price (50–99¢); only positions entered below it qualify
  crypto15mTimeDelayMin?: number;   // act only in the last N minutes (0–15)
  crypto15mEntryThreshold?: number; // favorite probability, fraction 0–1
  crypto15mEntryMax?: number;       // skip above this price, fraction 0–1
  crypto15mExitThreshold?: number;  // stop-loss price, fraction 0–1 (0 = hold)
  crypto15mTakeProfit?: number;     // per-bet cash-out: sell when our side's bid reaches this, fraction 0–1 (0 = off)
  crypto15mStopLossPct?: number;    // per-bet stop: sell when down >= this fraction of entry cost (0–1); 0 = off
  crypto15mTakeProfitPct?: number;  // per-bet cash-out: sell when up >= this fraction of entry cost (0–1); 0 = off
  crypto15mMinRsi?: number;         // direction-aware RSI confirmation (0–100); 0 = off
  crypto15mMinMacdHist?: number;    // direction-aware MACD-histogram confirmation (magnitude); 0 = off
  crypto15mMinDeltaPct?: number;    // required underlying move, fraction (0 = off)
  crypto15mEntryDiff?: number;      // limit markup through the spread, fraction (taker style)
  crypto15mEntryStyle?: 'maker' | 'taker'; // rest at the bid (no fee) vs cross the ask (always fills)
  crypto15mTakerFak?: boolean; // taker entries + escalation use a marketable FAK (fill-now-or-kill) order
  crypto15mMakerCancelMin?: number; // cancel unfilled entries with < N min to close (0 = at close)
  crypto15mMakerEscalate?: boolean; // at the cancel lead, cross to taker instead of abandoning
  crypto15mMakerFillSec?: number;   // fast escalate: cross to taker after a maker rests this many seconds unfilled (0 = off)
  crypto15mHoursStartUtc?: number;  // only enter between these UTC hours…
  crypto15mHoursEndUtc?: number;    // …start == end (or 0/24) = always
  crypto15mRecordSignals?: boolean; // passively log signals+outcomes for backtesting

  // ── User strategy scripts (Scripts tab) ─────────────────────────────
  // Scripts themselves live in the backend DB; these are the engine master
  // switch + the money rails applied to EVERY script (sandboxed or trusted).
  scriptsLiveEnabled?: boolean;     // master: scripts may place REAL orders
  scriptPollSec?: number;           // engine cadence, seconds (2–3600)
  scriptMaxEntryCents?: number;     // hard cap on any script submit price (1–99)
  scriptMaxContracts?: number;      // per-order size cap
  scriptMaxOpen?: number;           // open positions per script
  scriptDailyLossUsd?: number;      // per-script realized daily loss → auto-disable (0 = off)
  scriptMaxEnabled?: number;        // concurrently enabled scripts
  /** Manual terminal: stop contacting every provider. Degrades, never lies. */
  terminalOfflineMode?: boolean;
  /** Desktop notification when a standing instruction fires. Stays local. */
  terminalNotifications?: boolean;
  /**
   * AI market analysis. Off and keyless by default, on-demand only. The keys
   * themselves are NOT here — they live behind the same encrypted writer as
   * the exchange API key, never in settings.json.
   */
  aiEnabled?: boolean;
  aiProvider?: AiProvider;
  /** The model on the selected provider (one key for all six; it replaced
   *  aiAnthropicModel / aiOpenaiModel, migrated in legacy-settings.ts). */
  aiModel?: string;
  /** Server-side web search. Costs extra per search; grounds event markets. */
  aiWebSearch?: boolean;
  /** Remote control via chat bots. Master switch; off by default. */
  remoteEnabled?: boolean;
  remoteDiscordEnabled?: boolean;
  remoteTelegramEnabled?: boolean;
  /** Place orders from chat. Its OWN switch — bigger blast radius. */
  remoteTradingEnabled?: boolean;
  /** Hard ceiling on one chat-placed order, USD. Enforced in the backend. */
  remoteMaxOrderUsd?: number;
  /**
   * DERIVED from `accountMode` (=== 'paper') on every store write. Kept as a
   * key because the terminal, phone and agents read it; nothing should write
   * it directly any more — switch the account mode instead.
   */
  terminalPaperMode?: boolean;
  /** Starting cash of the ACCOUNT's paper book (terminal, phone, scripts), USD. */
  terminalPaperBankrollUsd?: number;
  /**
   * Paper | Live for the whole app. Paper (the default for a fresh install):
   * the terminal, phone, standing rules and agents all trade paper, and the
   * engines (crypto, live scripts) send nothing — their own switches are kept,
   * the effective switch is `switch AND live`. Never agent-writable, never in a
   * profile. Going live needs an API key that passes credentials.test and an
   * explicit tick.
   */
  accountMode?: AccountMode;
  /** AI agents over MCP: local server for Claude / Cursor / Codex. Off by default. */
  mcpEnabled?: boolean;
  mcpPort?: number;
  /** Master switch for what agents may trade: off | paper | live. Under live,
   *  only agents whose own mode is live trade real money. */
  mcpTradeMode?: McpTradeMode;
  /** Per-order and per-UTC-day caps on agent BUYS, all agents together, USD
   *  (0 = no cap). Each agent's own caps can only narrow these. */
  mcpMaxOrderUsd?: number;
  mcpDailySpendUsd?: number;
  /** Open positions across every agent. */
  mcpMaxPositions?: number;
  /** Edge an agent's own forecast must clear after Polymarket's fee, ¢/share. */
  mcpMinEdgeCents?: number;
  /** The same check for a post-only buy, which rests and pays no fee, ¢/share. */
  mcpMinEdgeRestingCents?: number;
  /** Realised today + open losses at the bid; at the limit agent BUYS stop. */
  mcpDailyLossUsd?: number;
  /** "Ask me first": each live agent order waits for approval. On by default. */
  mcpLiveApproval?: boolean;
  /** The user's named agents (shared/agents.ts). Default always first. */
  mcpAgents?: McpAgent[];
  /** Whole-account powers: engines on, arming scripts / schedule live,
   *  engine settings that spend, flattening the account. */
  mcpAllowLiveSwitches?: boolean;
  /** Agents may write/run strategy scripts (full Python in-process). */
  mcpScriptsEnabled?: boolean;
  /** Starting cash of each agent's own paper account, USD. */
  mcpPaperBankrollUsd?: number;
  /** The plain HTTP API (/api/v1) on the same loopback listener. Off by default. */
  mcpHttpEnabled?: boolean;
  /** Autopilot: the app runs one of your agents on a timer with your AI key. */
  autopilotEnabled?: boolean;
  autopilotIntervalMin?: number;
  autopilotMaxRunsPerDay?: number;
  autopilotDailyTokenBudget?: number;
  autopilotMaxSteps?: number;
  /** The user's standing brief for every run. */
  autopilotMission?: string;
  /** The named agent Autopilot runs as (its rules and caps bind it). */
  autopilotAgentId?: string;
  // Engine knobs with no dedicated control. Declared (and defaulted in the
  // settings store) so a value set by an AI agent's update_config persists
  // instead of being reverted by the next config push.
  dbCleanupInterval?: number;
  crypto15mRulesNo?: RuleCondition[];
  crypto15mPollSec?: number;
  crypto15mModelFmMaxBookGapCents?: number;
  crypto15mAutosizeToMinNotional?: boolean;
  scriptHookTimeoutSec?: number;
  scriptMarketLimit?: number;       // markets handed to decide_market() per tick (0 = off)
  scriptMarketMinVolume?: number;   // skip markets below this volume entirely
  scriptMarketMaxSpreadCents?: number; // max real bid/ask spread a decide_market entry may cross (0 = off)
}

export interface CredentialsState {
  env?: Network;
  /** True once an API key (Key ID + Secret Key) is saved. The name is kept from
   *  the shared backend shape; there is no wallet on this venue. */
  hasWalletKey: boolean;
  /** Same fact as hasWalletKey on this venue. */
  hasApiCreds: boolean;
  /** The Key ID (public, safe to show). */
  address: string;
  /** Shortened Key ID for display. */
  addressPreview: string;
  /** The Key ID. */
  keyId?: string;
  /** True when the secret had to be written to disk in plaintext because no OS
   *  keystore (DPAPI/keychain) was available. Surfaced so the UI can warn. */
  keyStoredUnencrypted?: boolean;
}

export interface CredentialsStatusAll {
  current: Network;
  mainnet: CredentialsState;
}

export interface CredentialsInput {
  /** The API key's Key ID (a UUID). */
  keyId: string;
  /** The API key's Secret Key (base64 Ed25519). Polymarket shows it once. */
  secretKey: string;
  env?: Network;
}

// ───────── Profiles (saved strategy configurations) ────────────────────

// What a profile configures. The crypto engine is the only engine with tuning
// to save, so it is the only scope.
export type ProfileScope = 'crypto';

export interface Profile {
  id: string;
  name: string;
  description?: string;
  /** Engine this profile configures. */
  scope?: ProfileScope;
  createdAt: string;
  updatedAt: string;
  config: TraderConfig;
  builtin?: boolean;
}

// ───────── Persistent app state (kept in userData/settings.json) ────────

export interface AppState {
  config: TraderConfig;
  activeCryptoProfileId?: string | null;
  customProfiles: Profile[];
  startMinimized: boolean;
  startWithWindows: boolean;
  enableDiscordRpc: boolean;
  acceptedDisclaimer: boolean;
  /** Sidebar surface: 'simple' shows Home/Strategies/Positions/Settings only.
   *  A view preference — never pushed to the Python backend. */
  uiMode: 'simple' | 'advanced';
  windowBounds: { x: number; y: number; width: number; height: number } | null;
  /** Which onboarding revision the user has been through (shared/onboarding.ts).
   *  0 = none / a settings file from before the field existed. */
  onboardingSeen?: number;
}

// ───────── Backend status / live events ────────────────────────────────

/** One problem in the corner stack (python/alerts.py). `fix` is never empty. */
export interface AppAlert {
  id: string;
  severity: 'error' | 'warn';
  title: string;
  detail: string;
  fix: string;
  action: AppAlertAction | null;
}

export type AppAlertAction =
  | { kind: 'nav'; page: string; label: string }
  | { kind: 'copy'; text: string; label: string }
  | { kind: 'resume'; label: string }
  | { kind: 'restart'; label: string };

export type BackendStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'restarting'
  | 'crashed';

export interface BackendInfo {
  status: BackendStatus;
  pid: number | null;
  startedAt: string | null;
  lastError: string | null;
  pythonOk: boolean;
  authOk: boolean;
}

export interface AccountSnapshot {
  cashUsd: number;
  portfolioUsd: number;
  totalUsd: number;
  /** True on the placeholder snapshot returned while the backend process is
   *  down. NOTHING in that object was measured — the numeric fields are zeros
   *  only because this interface demands numbers. Anything rendering a figure
   *  from this snapshot must show an em dash instead: a balance nobody could
   *  read is not a balance of $0.00, and green $0.00 in the header was the
   *  app's most confident lie. */
  backendDown?: boolean;
  /** Behind the wallet panel. */
  wallet?: {
    /** The latest read of the exchange worked. */
    readOk: boolean;
    /** Epoch seconds of the last read that worked, null before the first. */
    readAt: number | null;
    openOrdersUsd?: number | null;
    pendingWithdrawalCount?: number;
    pendingWithdrawalsUsd?: number | null;
  };
  balanceSyncing?: boolean;  // an order just filled or settled — ledgers catching up; totals may transiently dip
  /** Exchange matching-engine restart (orders paused) or its post-restart
   *  post-only window. Null when trading normally. */
  tradingRestriction?: { kind: 'restarting' | 'post_only'; untilEpoch: number; message: string } | null;
  /** Authenticated order/fill push feed (an accelerator; REST stays authoritative). */
  userFeed?: { running: boolean; connected: boolean; events: number; lastEventAgoSec: number | null; lastError: string };
  startBankrollUsd: number;
  /** Where the bankroll baseline came from. "user" = pinned in
   *  Settings, "auto" = first snapshot we recorded for this env,
   *  "live" = current total (no history yet, ROI starts at 0). */
  bankrollSource?: 'user' | 'auto' | 'live';
  roiPct: number;
  /** Position-derived realized P&L (sum of pnl_usd across resolved trades).
   *  Useful for transparency/audit, but dashboard headlines use the
   *  balance-snapshot deltas below. */
  realizedPnlUsd: number;
  /** Today P&L = total balance now − total balance at first snapshot of today.
   *  This is the SIMPLE TRUTH — it doesn't depend on per-position math. */
  todayPnlUsd?: number;
  /** All-time P&L = total balance now − total balance at first ever snapshot
   *  for this env (or pinned bankroll baseline). */
  alltimePnlUsd?: number;
  /** Snapshots used to compute the deltas above. */
  todayBaselineUsd?: number | null;
  alltimeBaselineUsd?: number | null;
  todayWins?: number;
  todayLosses?: number;
  unrealizedPnlUsd: number;
  openCostUsd: number;
  feesUsd: number;
  wins: number;
  losses: number;
  winRate: number;
  pendingCount: number;
  openCount: number;
  resolvedCount: number;
  totalOpened: number;
  byNetwork: { mainnet: AccountByEnv };
  // ── Session = current bot run ────────────────────────────────────
  /** Session P&L = current total - the active run's start_total_usd.
   *  This is the headline P&L number on the dashboard. */
  sessionPnlUsd?: number;
  sessionRoiPct?: number;
  sessionBaselineUsd?: number;
  sessionStartedAt?: string;
  sessionRunId?: number;
}

export interface BotRun {
  id: number;
  network: Network;
  startedAt: string;
  endedAt: string | null;
  startCashUsd: number;
  startPortfolioUsd: number;
  startTotalUsd: number;
  endCashUsd: number | null;
  endPortfolioUsd: number | null;
  endTotalUsd: number | null;
  pnlUsd: number;
  tradesOpened: number;
  tradesWon: number;
  tradesLost: number;
  isActive: boolean;
}

export interface BotRunsResponse {
  runs: BotRun[];
  activeRunId: number;
  activeRun: BotRun | null;
}

export interface AccountByEnv {
  wins: number;
  losses: number;
  realizedPnl: number;
}

export interface PnlPoint {
  at: string;
  cashUsd: number;
  portfolioUsd: number;
  totalUsd: number;
  realizedPnlUsd: number;
  openPositions: number;
}

export interface BotPosition {
  id: number;
  signalSource: SignalSource;
  signalId: number;
  ticker: string;
  eventTicker: string;
  title: string;
  category: string;
  direction: 'yes' | 'no';
  action: 'buy' | 'sell';
  targetContracts: number;
  limitPriceCents: number;
  filledContracts: number;
  avgFillPriceCents: number | null;
  costUsd: number;
  feesUsd: number;
  clientOrderId: string;
  orderId: string | null;
  status:
    | 'submitted'
    | 'partial'
    | 'filled'
    | 'canceled'
    | 'expired'
    | 'gone'
    | 'error'
    | 'dry_run';
  confidence: number;
  edgePts: number;
  signalPriceCents: number;
  resolved: boolean;
  outcomeCorrect: number | null;
  settlementUsd: number | null;
  pnlUsd: number | null;
  /** Live price of the held outcome (cents), from Polymarket's last
   *  reconcile. Null until the position has a mark. */
  markPriceCents: number | null;
  /** Unrealized mark-to-market P&L for an open filled position (held
   *  contracts valued at `markPriceCents` minus cost). Null for resolved /
   *  unfilled / not-yet-marked rows — use `pnlUsd` once resolved. */
  livePnlUsd: number | null;
  balanceBeforeUsd: number | null;
  network: Network;
  createdAt: string;
  lastUpdated: string;
  resolvedAt: string | null;
  error: string | null;
}

export interface LogEntry {
  ts: string;
  level: 'DEBUG' | 'INFO' | 'WARN' | 'ERROR' | 'CRITICAL';
  source: 'main' | 'backend' | 'trader' | 'discord' | 'app' | (string & {});
  msg: string;
}

export interface ActionResult<T = void> {
  ok: boolean;
  message?: string;
  data?: T;
}

// ───────── 15-minute crypto monitor (read-only) ────────────────────────

export interface Crypto15mConstants {
  timeDelayMin: number;
  entryThreshold: number;
  exitThreshold: number;
  entryMax: number;
  minDeltaPct?: number;
  entryDiff: number;
  directionMode?: 'favorite' | 'contrarian';
  entryStyle?: 'maker' | 'taker';
  hoursStartUtc?: number;
  hoursEndUtc?: number;
}

export interface Crypto15mAsset {
  asset: string;        // e.g. "BTC"
  series: string;       // e.g. "KXBTC15M"
  enabled?: boolean;    // false = toggled off (won't open new positions)
  upAsk?: number | null;        // best ask to BUY the Up token (0..1)
  downAsk?: number | null;      // best ask to BUY the Down token (0..1)
  arbEdgeCents?: number | null; // 100*(1 − (upAsk+downAsk)); > 0 = buyable arb (gross of fees)
  arbSignal?: boolean;          // arbEdgeCents >= the configured minimum
  bookImbalance?: number | null;// top-of-book (bid−ask)/(bid+ask) on the Up token, [-1,1]
  spotUsd: number | null;
  open15mUsd: number | null;
  deltaUsd: number | null;       // abs(open - live)
  deltaPct?: number | null;      // abs(open - live) / open
  hasMarket: boolean;
  ticker: string | null;
  closeTime: string | null;
  minsLeft: number | null;
  upProb: number | null;         // 0..1
  downProb: number | null;       // 0..1
  favorite: 'up' | 'down' | null;
  favoritePrice: number | null;  // 0..1
  entryCost: number | null;      // 0..1 (cost to buy the favorite side)
  yesBid?: number | null;        // 0..1 (best yes bid)
  yesAsk?: number | null;        // 0..1 (best yes ask)
  inWindow: boolean;             // within the last `timeDelayMin` minutes
  signal: boolean;               // all entry conditions met
  openMarketCount: number;
  error: string | null;
  hourUtc?: number | null;       // current UTC hour 0-23 (timing rule field)
  peersAgree?: number | null;    // 0..1 — fraction of other coins favoring the same side
  marketBias?: number | null;    // -1..1 — market-wide up/down lean (breadth)
  // underlying technical indicators (MACD/RSI on the 1-min underlying; rule fields)
  macd?: number | null;          // MACD line
  macdSignal?: number | null;    // MACD signal line
  macdHist?: number | null;      // MACD histogram = macd − signal
  macdCross?: number | null;     // +1 bullish / −1 bearish / 0 no cross on this bar
  rsi?: number | null;           // Wilder RSI(14), 0..100
  wsBid?: number | null;         // REAL-TIME best bid (favorite side), cents
  wsAsk?: number | null;         // REAL-TIME best ask (favorite side), cents
  priceSource?: 'ws' | 'rest' | 'gamma';  // which priced the entry decision: live book vs public (cached) quote
  // settlement model (spot-lag sniper) fields
  strikeUsd?: number | null;     // window-open reference the model measures against
  deltaSignedPct?: number | null; // (spot − open) / open, SIGNED
  sigma1m?: number | null;       // realized 1-min vol (fraction/√min)
  modelProb?: number | null;     // terminal-spot model P(up), 0..1
  edgeNetCents?: number | null;  // model's best fee-net edge on either side (cents)
  spotLive?: boolean;            // spot came from the live WS feed this instant
}

export interface Crypto15mSnapshot {
  fetchedAt: string;
  spotOk: boolean;
  spotSource: string;
  hoursOk?: boolean;             // inside the configured UTC trading hours
  constants: Crypto15mConstants;
  assets: Crypto15mAsset[];
}

export type Crypto15mStatusName =
  | 'dry_run' | 'submitted' | 'filled' | 'exiting'
  | 'exited' | 'settled' | 'canceled' | 'error';

export interface Crypto15mPosition {
  id: number;
  asset: string;
  series: string;
  ticker: string;
  side: 'up' | 'down' | '';
  direction: 'yes' | 'no' | '';
  targetContracts: number;
  filledContracts: number;
  entryLimitCents: number;
  avgEntryCents: number | null;
  costUsd: number;
  status: Crypto15mStatusName | string;
  exitReason: string | null;
  exitLimitCents: number | null;
  proceedsUsd: number | null;
  confidence: number;          // favorite prob at entry (×100)
  entryDeltaUsd: number | null;
  outcomeCorrect: number | null;
  settlementUsd: number | null;
  pnlUsd: number | null;
  resolved: boolean;
  settling: boolean;           // window closed, awaiting the exchange's settlement
  closeTime: string;
  network: Network;
  createdAt: string;
  resolvedAt: string | null;
  error: string | null;
  strategy?: string;   // which strategy opened it (favorite|contrarian|rules)
  feesUsd?: number;    // venue-reported entry+exit fees (0 when none reported)
}

export interface Crypto15mStats {
  openCount: number;
  wins: number;
  losses: number;
  realizedPnlUsd: number;
  total: number;
}

// A worked example of the current sizing settings (computed server-side
// so the UI can show "≈ $X per bet" without duplicating the math).
export interface Crypto15mSizing {
  mode: 'fixed' | 'balance_pct';
  balancePct: number;     // fraction of balance per bet (balance_pct mode)
  maxLossPct: number;     // fraction of balance capped per bet (0 = off)
  balanceUsd: number;     // balance sized against (0 = unknown)
  estPriceCents: number;  // representative entry price used for the estimate
  estContracts: number;   // contracts a typical entry would buy
  estCostUsd: number;     // ≈ cost of a typical bet
  streakMult?: number;    // current streak-sizing multiplier (1 = flat/off)
  note: string;           // caveat to surface (empty = none)
}

export interface Crypto15mStatus {
  enabled: boolean;
  authed: boolean;    // API key saved and accepted
  trading: boolean;   // enabled AND connected = placing real orders (no paper mode)
  haltReason?: string; // engine-wide circuit-breaker (daily cap / lifetime loss) with how-to-resume text; '' when trading normally
  blockReasons?: Record<string, string>; // per-asset "why no entry" from the last tick
  byStrategy?: { strategy: string; n: number; wins: number; losses: number; pnl_usd: number; fees_usd: number }[];
  modelCalibration?: { ok: boolean; n: number; rate: number | null; lb: number | null };
  orderSize: number;
  maxConcurrent: number;
  sizing: Crypto15mSizing;
  env: Network;
  stats: Crypto15mStats;
  open: Crypto15mPosition[];
  recent: Crypto15mPosition[];
}

// One row of the "why isn't it trading" checklist. The first blocked row,
// reading top to bottom, is the answer.
export interface TradingGate {
  id: string;
  label: string;
  state: 'ok' | 'blocked' | 'off';
  reason: string;
}

export interface TradingStatus {
  /** Checks that apply to every engine (backend, API key, kill switch, ...). */
  main?: TradingGate[];
  c15: {
    enabled: boolean;
    live: boolean;
    authed: boolean;
    env: string;
    blockReasons: Record<string, string>;
  };
}

// Result payload of a replay backtest.
export interface Crypto15mBacktest {
  n: number;
  wins: number;
  winRate: number;
  netEvCentsPerContract: number;
  totalPnlUsd: number;
  maxDrawdownUsd: number;
  contracts: number;
  windowsScanned: number;
  byAsset: Record<string, { n: number; wins: number; pnlUsd: number }>;
  equity: { at: string | null; value: number }[];
  byHourUtc: { hour: number; n: number; wins: number; pnlUsd: number }[];
  byDay: { day: string; n: number; wins: number; pnlUsd: number }[];
  trades: { ticker: string; asset: string; side: string; costCents: number; minsLeft: number | null; won: boolean; pnlUsd: number; at: string }[];
  caveats: string[];
  /** t of the per-trade mean, CLUSTERED by settlement close time: coins closing
   *  together count as one draw. */
  tStat?: number | null;
  /** The same t treating every trade as independent (runs high). */
  tStatNaive?: number | null;
  /** How many clusters (close times / events) tStat rests on. */
  nClusters?: number;
  interval?: '5m' | '15m' | 'hourly';  // which recorded window series was replayed (absent on older backends)
  /** Present only when ZERO windows were replayable: why, in a form the page
   *  can headline. `message` is also caveats[0]. Absent on older backends. */
  noData?: ReplayNoData;
}

/** Why a crypto replay scanned nothing (python/replay.py empty_replay_report).
 *  - interval: the strategy's interval isn't the one being recorded
 *  - assets: windows exist, none for the strategy's coins
 *  - collection-off: crypto data collection is switched off
 *  - none-yet: recording the right interval, nothing settled yet */
export interface ReplayNoData {
  reason: 'interval' | 'assets' | 'collection-off' | 'none-yet';
  interval: string;
  recordingInterval: string | null;   // the interval the recorder is on (null = unknown)
  recordingOn: boolean | null;
  recorded: Record<string, number>;   // resolved windows per interval in the period
  message: string;
}

// ───────── user strategy scripts (Scripts tab) ─────────────────────────

export interface UserScriptStats {
  n: number;        // total position rows (entries + refusals excluded)
  open: number;
  wins: number;
  losses: number;
  pnlUsd: number;   // realized
}

/** One thing the risk audit noticed in a script's source. */
export interface ScriptAuditFinding {
  severity: 'critical' | 'warning' | 'info';
  /** 'network' | 'wallet' (the backend's word for credential access; the UI says
   *  "credentials") | 'exfiltration' | 'dynamic' | 'process' |
   *  'filesystem' | 'env' | 'introspection' | 'performance' | 'audit' */
  category: string;
  line: number;
  message: string;
}

/** The static risk scan that replaced the language sandbox. `ok` means
 *  "nothing critical was recognized" — never "safe". */
export interface ScriptAudit {
  ok: boolean;
  parsed: boolean;
  critical: number;
  warning: number;
  info: number;
  categories: string[];
  findings: ScriptAuditFinding[];
  summary: string;
}

export interface UserScriptStatus {
  /** off — not enabled. blocked — enabled but a gate is stopping it (detail
   *  says which). error — its code won't load. starting — enabled, no tick
   *  yet. running — actually executing. */
  state: 'off' | 'blocked' | 'error' | 'starting' | 'running';
  /** One sentence a user can act on, e.g. 'Armed for real orders, but the
   *  "Scripts live" master switch is off — it is not running.' */
  detail: string;
  ticks: number;
  intents: number;
  orders: number;
  lastTickAt: string | null;
  lastIntentAt: string | null;
  lastOrderAt: string | null;
}

export interface UserScript {
  id: string;
  name: string;
  description: string;
  code: string;
  enabled: boolean;
  /** Shadow mode: runs every rail but records intents instead of ordering.
   *  ON for every new script — arming is always a deliberate act. */
  dryRun: boolean;
  /** The script's OWN coin scope for decide(); null = every coin. Never the
   *  15m Crypto tab's crypto15mAssets, which used to silently narrow every
   *  script in the app. */
  assets: string[] | null;
  /** Static risk scan of the code. Scripts are full Python — this is what
   *  replaced the language sandbox, so it is shown, not enforced. */
  audit: ScriptAudit;
  /** Whether this script is ACTUALLY running right now, and if not, which
   *  gate is holding it. Computed backend-side, where the gates live. */
  status: UserScriptStatus;
  notes: string;
  lastError: string | null;
  lastErrorAt: string | null;
  createdAt: string;
  updatedAt: string;
  stats: UserScriptStats | null;
  /** Simulated track record while in shadow mode (same shape as `stats`). */
  shadowStats: UserScriptStats | null;
}

/** One row of a script's shadow ledger — an order it would have placed. */
export interface ScriptShadowOrder {
  id: number;
  source: 'crypto' | 'market' | (string & {});
  asset: string;
  ticker: string;
  side: string;
  contracts: number;
  entryCents: number;
  orderType: string;
  reason: string;
  refused: boolean;
  note: string;
  resolved: boolean;
  won: boolean | null;
  pnlUsd: number | null;
  at: string;
}

export interface ScriptValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  /** Risk findings for this exact source, refreshed on every validate. */
  audit: ScriptAudit;
  name: string;
  description: string;
  hasHeader: boolean;
  ctxFields: string[];
}

// Structured script-API reference (rendered by the Scripts page docs panel;
// generated backend-side from the same sources as the AI context pack).
export interface ScriptApiDocs {
  contract: string;
  fields: { name: string; doc: string; backtestable: boolean }[];
  /** Globals injected without an import (math, statistics, state, log, ctx).
   *  Scripts may import anything else themselves. */
  injected: string[];
  /** Wall-clock ceiling for one hook call before the engine abandons it. */
  hookTimeoutSec: number;
  rails: {
    maxEntryCents: number; maxContracts: number; maxOpen: number;
    dailyLossUsd: number; defaultOrderSize: number;
  };
  examples: { name: string; code: string }[];
  /** One-click Strategy presets (Simple mode). Ordinary krypt-scripts served
   *  from the backend so the code can never drift from the shipped engine;
   *  `expected`/`risks` are honest edge-audit labels, shown verbatim. */
  presets: {
    id: string; name: string; tagline: string;
    expected: string; risks: string; bankrollMin: number; code: string;
  }[];
}

// Same shape as a replay backtest plus the script run extras.
export interface ScriptBacktest extends Crypto15mBacktest {
  tStat?: number | null;
  scriptError?: string | null;
  scriptLogs?: string[];
}

export interface HistoryDownloadStatus {
  status: 'idle' | 'running' | 'waiting' | 'done' | 'error' | 'cancelled';
  total: number;
  done: number;
  saved: number;
  unavailable: number;
  days: number | null;
  intervals: string[];
  /** What to tell the person, when there is something. */
  message: string | null;
  /** Seconds left at the steady rate; null when nothing is running. */
  etaSec: number | null;
}

export interface CollectionStats {
  c15: {
    windows: number; resolved: number; ticks: number;
    /** Windows that came from the history download, not the recorder. */
    downloaded?: number;
    firstAt: string | null; lastAt: string | null;
    recent: { ticker: string; asset: string; favorite: string | null; favorite_price: number | null; up_won: number | null; resolved: number; close_time: string }[];
    /** Windows per interval ('5m' | '15m' | 'hourly'); absent on older backends. */
    byInterval?: Record<string, { windows: number; resolved: number }>;
    /** The interval the recorder is collecting now (the Crypto tab's selection). */
    recordingInterval?: string;
  };
  collecting: { c15: boolean };
}

// ───────── window.krypt API ────────────────────────────────────────────

export interface AccountInfo {
  name: string;       // "Default" or the account folder name
  current: boolean;   // is this the account this window is running?
  isDefault: boolean; // the original/default account
}

export interface KryptApi {
  app: {
    version: () => Promise<string>;
    openExternal: (url: string) => Promise<void>;
    showItemInFolder: (filePath: string) => Promise<void>;
    getUserDataPath: () => Promise<string>;
    factoryReset: () => Promise<ActionResult<{ deleted: Record<string, number> }>>;
    clearHistory: () => Promise<ActionResult<{ deleted: Record<string, number> }>>;
    onDataReset: (cb: (payload: unknown) => void) => () => void;
    /** Record that the user has been through this build's onboarding. Takes
     *  no revision: main writes the one it was built with. */
    markOnboardingSeen: () => Promise<ActionResult>;
    /** Can anyone see the window (false while minimized / hidden to tray). */
    isVisible: () => Promise<boolean>;
    onVisibility: (cb: (visible: boolean) => void) => () => void;
  };
  accounts: {
    current: () => Promise<string>;
    list: () => Promise<AccountInfo[]>;
    create: (name: string) => Promise<{ ok: boolean; name?: string; message?: string }>;
    launch: (name: string) => Promise<{ ok: boolean; message?: string }>;
  };
  state: {
    get: () => Promise<AppState>;
    onChange: (cb: (state: AppState) => void) => () => void;
    setStartMinimized: (v: boolean) => Promise<ActionResult>;
    setStartWithWindows: (v: boolean) => Promise<ActionResult>;
    setEnableDiscordRpc: (v: boolean) => Promise<ActionResult>;
    acceptDisclaimer: () => Promise<ActionResult>;
    /** Persist the sidebar surface (simple 4-page shell vs full cockpit). */
    setUiMode: (v: 'simple' | 'advanced') => Promise<ActionResult>;
    /** Re-show the first-run onboarding (clears the accepted flag). */
    resetOnboarding: () => Promise<ActionResult>;
    /** Same as app.markOnboardingSeen (kept where kryptrader had it). */
    markOnboardingSeen: () => Promise<ActionResult>;
  };
  config: {
    get: () => Promise<TraderConfig>;
    update: (patch: Partial<TraderConfig>) => Promise<TraderConfig>;
    replace: (config: TraderConfig) => Promise<TraderConfig>;
    reset: () => Promise<TraderConfig>;
  };
  profiles: {
    list: () => Promise<Profile[]>;
    save: (name: string, description?: string) => Promise<ActionResult<Profile>>;
    apply: (id: string) => Promise<ActionResult<TraderConfig>>;
    rename: (id: string, name: string) => Promise<ActionResult>;
    delete: (id: string) => Promise<ActionResult>;
    duplicate: (id: string) => Promise<ActionResult<Profile>>;
    export: (id: string) => Promise<ActionResult<string>>;
    import: (json: string) => Promise<ActionResult<Profile>>;
  };
  credentials: {
    /** Current-network credential status. */
    status: () => Promise<CredentialsState>;
    /** Single-network (mainnet) status. */
    statusAll: () => Promise<CredentialsStatusAll>;
    save: (input: CredentialsInput) => Promise<ActionResult>;
    /** Verify the API key by reading the account balance. `ready` is the
     *  trading-readiness verdict; when false, `issues` lists what's blocking
     *  live orders (e.g. zero balance). */
    test: (env?: Network) => Promise<ActionResult<{
      env: Network;
      balanceUsd: number;
      ready?: boolean;
      issues?: string[];
      /** The subset of `issues` under which no order can work at all. */
      fatalIssues?: string[];
      address?: string;
    }>>;
    clear: (env?: Network) => Promise<ActionResult>;
    /** Fires whenever the backend's view of credentials changes
     *  (after save / clear / startup migration). The renderer should
     *  re-pull `statusAll()` in response. */
    onChanged: (cb: (payload: unknown) => void) => () => void;
  };
  backend: {
    info: () => Promise<BackendInfo>;
    start: () => Promise<ActionResult>;
    stop: () => Promise<ActionResult>;
    restart: () => Promise<ActionResult>;
    onInfo: (cb: (info: BackendInfo) => void) => () => void;
    runOnce: (
      action:
        | 'syncMarkets'
        | 'pollOrders'
        | 'resolveAll'
        | 'reconcilePositions'
        | 'syncPositions'
        | 'recomputePnl'
        | 'reconcileFills'
        | 'auditPnl'
    ) => Promise<ActionResult<{ summary: string }>>;
  };
  trading: {
    cancelAllOpen: () => Promise<ActionResult<{ canceled: number }>>;
    killSwitch: () => Promise<ActionResult<{ engaged: boolean; canceled: number; cancelError?: string }>>;
    killSwitchClear: () => Promise<ActionResult<{ engaged: boolean }>>;
    /** Lifts the pause only. Each engine still has its own switch. */
    resume: () => Promise<ActionResult>;
    killSwitchStatus: () => Promise<{ engaged: boolean; reason: string | null }>;
    flatten: () => Promise<ActionResult<{ closed: number }>>;
    status: () => Promise<TradingStatus>;
    collection: () => Promise<CollectionStats | null>;
    /** Download past crypto markets from the public API, in the background. */
    historyStart: (args?: { days?: number; intervals?: string[] }) => Promise<HistoryDownloadStatus | null>;
    historyStatus: () => Promise<HistoryDownloadStatus | null>;
    /** Stop and forget the job. What was already saved stays. */
    historyCancel: () => Promise<HistoryDownloadStatus | null>;
    exportData: () => Promise<{ dir: string; files: string[] } | null>;
  };
  data: {
    account: () => Promise<AccountSnapshot>;
    /** Reads the exchange now rather than waiting for the balance interval. */
    accountRefresh: () => Promise<AccountSnapshot>;
    pnlSeries: (sinceHours?: number) => Promise<PnlPoint[]>;
    positions: (filter?: PositionFilter) => Promise<BotPosition[]>;
    botRuns: (env?: Network | null, limit?: number) => Promise<BotRunsResponse>;
    onAccount: (cb: (snap: AccountSnapshot) => void) => () => void;
    onPosition: (cb: (pos: BotPosition) => void) => () => void;
  };
  crypto15m: {
    snapshot: () => Promise<Crypto15mSnapshot>;
    status: () => Promise<Crypto15mStatus>;
    history: (opts?: { limit?: number }) => Promise<{ rows: Crypto15mPosition[] }>;
    backtest: (args?: { sinceDays?: number; config?: Record<string, unknown> }) => Promise<Crypto15mBacktest | null>;
    // Fired when the overall take-profit target turns the crypto engine off.
    onAutoOff: (cb: (d: { reason: string; gained: number; target: number }) => void) => () => void;
  };
  scripts: {
    list: () => Promise<{ scripts: UserScript[] }>;
    save: (s: { id?: string; name?: string; description?: string; code: string; notes?: string }) =>
      Promise<{ script: UserScript; errors: string[]; warnings: string[];
                audit: ScriptAudit; disarmed?: boolean }>;
    delete: (id: string) => Promise<{ ok: boolean }>;
    setEnabled: (id: string, enabled: boolean) => Promise<{ script: UserScript }>;
    /** The script's own coin scope for decide(); null/[] = every coin. */
    setAssets: (id: string, assets: string[] | null) => Promise<{ script: UserScript }>;
    /** Arm for real orders (dryRun=false) or return to shadow. Arming
     *  re-validates the code and refuses if it no longer passes. */
    setDryRun: (id: string, dryRun: boolean) => Promise<{ script: UserScript }>;
    /** The shadow ledger for one script (newest first). */
    shadowOrders: (id: string, limit?: number) => Promise<{ orders: ScriptShadowOrder[] }>;
    /** Save a script to a .py file via the OS save dialog. */
    exportFile: (name: string, code: string) => Promise<ActionResult<string>>;
    /** Read a .py file via the OS open dialog. Returns the code, unvalidated. */
    importFile: () => Promise<ActionResult<string>>;
    validate: (code: string) => Promise<ScriptValidation>;
    backtest: (args: { id?: string; code?: string; sinceDays?: number; assets?: string[] | null; config?: Record<string, unknown> }) =>
      Promise<ScriptBacktest | null>;
    contextPack: () => Promise<{ text: string }>;
    /** Structured API reference for the in-app docs panel. */
    docs: () => Promise<ScriptApiDocs>;
    /** Save the context pack as a .txt via the OS save dialog. */
    exportPack: () => Promise<ActionResult<string>>;
    /** Backend pushed a script status change (auto-disable + why). */
    onStatus: (cb: (d: { id: string; enabled: boolean; lastError?: string }) => void) => () => void;
    /** Batched log() lines from a running script. */
    onLog: (cb: (d: { id: string; lines: string[] }) => void) => () => void;
  };
  polymarket: {
    /** Resolve the public polymarket.us URL for a market/event. */
    marketUrl: (args: { eventTicker?: string; ticker?: string; env?: string }) =>
      Promise<{ url: string }>;
  };
  /**
   * The manual terminal. Note what these signatures take: a condition id, a
   * column, a side, an interval. Never a URL and never a host — main decides
   * which of its hardcoded hosts to contact.
   */
  terminal: {
    discover: (args: { column: DiscoverColumn; limit?: number; filters?: DiscoverFilters })
      => Promise<DiscoverResult>;
    search: (args: { query: string; limit?: number }) => Promise<DiscoverResult>;
    market: (conditionId: string) => Promise<{ market: MarketSummary | null }>;
    book: (conditionId: string, side: 'yes' | 'no') => Promise<BookSnapshot>;
    history: (conditionId: string, side: 'yes' | 'no', interval: TerminalInterval)
      => Promise<PriceSeries>;
    resolution: (conditionId: string) => Promise<ResolutionReport>;
    /** Our own tape for this market — starts recording when the page opens. */
    micro: (conditionId: string, side: 'yes' | 'no') => Promise<MicroSeries>;
    /** The other markets in this market's event, priced. */
    related: (conditionId: string, limit?: number) => Promise<RelatedMarkets>;
    /**
     * The same question on Kalshi, if one exists. Opt-in: the first call warms
     * a ~11,800-row series index, so it is never part of a poll.
     */
    crossVenue: (conditionId: string, interval: TerminalInterval)
      => Promise<CrossVenueComparison>;
    health: () => Promise<TerminalHealth>;
    watchlist: (args: { action: 'list' | 'add' | 'remove'; conditionId?: string; title?: string })
      => Promise<{ entries: WatchEntry[] }>;
    /** Dry-run the ticket. Always call this before `place`. */
    preview: (order: ManualOrderRequest) => Promise<ManualOrderPreview>;
    place: (order: ManualOrderRequest) => Promise<ManualOrderResult>;
    cancel: (orderId: string) => Promise<{ ok: boolean; message: string }>;
    /**
     * Resting orders. Pass a `conditionId` to narrow the list to one market
     * and have each row stamped with `bestTouchCents` — the price it would
     * have to reach to fill. Omit it for the account-wide list (Track), which
     * reads no books.
     */
    orders: (conditionId?: string)
      => Promise<{ orders: RestingOrder[]; fetchedAt: string; note: string | null }>;
    standing: (args: {
      action: 'list' | 'add' | 'cancel' | 'arm';
      id?: string; armed?: boolean; item?: Partial<StandingInstruction>;
    }) => Promise<{ instructions: StandingInstruction[]; error: string | null }>;
    /**
     * Price an account-wide flatten. Read-only: it walks the book for every
     * position you hold and says what each exit would fetch. Call it before
     * `flattenWallet`, and show the user what came back — the estimate is off
     * a book that is already a moment old, never a quote.
     */
    flattenPreview: () => Promise<FlattenPreview>;
    /**
     * Sell every open position in the account, one at a time. Gated in main on
     * paper mode, the live-trading switch and a saved API key, and every
     * individual sell still goes through the same `place()` as a click.
     */
    flattenWallet: () => Promise<FlattenResult>;
    /**
     * Price a one-click exit out of ONE position: the marketable sell behind
     * the Market page's position panel. Read-only. It walks the bids for the
     * size you name, caps the limit at `bestBid − maxSlippageCents`, and if
     * the whole size does not fit inside that cap it says how much does — the
     * order is then sized to exactly that, so nothing is ever left resting.
     *
     * `paper` is decided in MAIN from the user's setting, not sent from here.
     */
    sellPreview: (order: SellPositionRequest) => Promise<SellPreview>;
    /**
     * Send that exit — one marketable FAK, through the same `place()` as a
     * click, with the same paper gate, manual ceilings and drift check.
     * `confirmUsd` must be the proceeds figure the user actually read.
     */
    sellPosition: (order: SellPositionRequest) => Promise<SellResult>;
    portfolio: () => Promise<TerminalPortfolio>;
    /**
     * The settled RECORD from the fill ledger — every market it ever settled,
     * whether or not the position is still in the account.
     *
     * Deliberately its own call rather than part of `portfolio`, which the
     * Track page polls: this one walks the whole ledger and resolves every
     * market in it, so it runs when the user opens the settled table and not
     * every five seconds.
     */
    settled: () => Promise<SettledRecord>;
    fills: (limit?: number) => Promise<{ fills: TerminalFill[]; fetchedAt: string; note: string | null }>;
    orderLog: (limit?: number) => Promise<{ orders: TerminalOrderLogRow[]; fetchedAt: string }>;
    /**
     * Walk Polymarket's own trade history into the local fill ledger.
     * `complete: false` means there is more history beyond the page cap —
     * run it again. A truncated walk is never reported as success.
     */
    reconcile: () => Promise<{
      inserted: number; matched: number;
      /**
       * Fills that had been filed under the WRONG outcome and were re-filed
       * against Polymarket's own records. The walk re-reads the whole
       * history every pass, which is what makes the repair possible at all.
       */
      corrected?: number;
      /** Fills we could not tie to either outcome, so left out of the ledger. */
      unmapped?: number;
      scanned?: number; complete?: boolean; note: string | null;
    }>;
    onStanding: (cb: (e: StandingEvent) => void) => () => void;
    /**
     * Paper trading. Deliberately separate calls, not a flag on `place` — a
     * shared entry point is one `if` away from spending real money in what the
     * user believes is a simulator.
     */
    paperPlace: (order: ManualOrderRequest) => Promise<PaperOrderResult>;
    paperPortfolio: () => Promise<PaperPortfolio>;
    paperOrders: (limit?: number) => Promise<{ orders: PaperOrder[]; fetchedAt: string }>;
    paperCancel: (localId: number) => Promise<{ ok: boolean; message: string }>;
    paperReset: () => Promise<{ cleared: number; message: string }>;
  };
  /**
   * AI market analysis. On-demand only: there is no polling path and no
   * engine path, because every call spends the user's own provider credit.
   *
   * The analysis never contains a probability, a fair value or an edge —
   * see `AiLean` in shared/market.ts for why that is a design rule and not
   * an oversight.
   */
  ai: {
    status: () => Promise<AiStatus>;
    /** Analyse one market. Failure returns the same shape with ok:false. */
    analyze: (conditionId: string) => Promise<AiAnalysis>;
    /**
     * Verified against the provider BEFORE it is stored, so a typo fails on
     * the Settings screen rather than as a button that never works. Passing
     * an empty key clears it.
     */
    setKey: (provider: AiProvider, key: string)
      => Promise<{ ok: boolean; message: string; models?: string[]; masked?: string }>;
    /** What this key can actually reach — never a hardcoded list. */
    listModels: (provider: AiProvider)
      => Promise<{ ok: boolean; models: string[]; message?: string }>;
    /** "Test connection": free endpoints only (a model listing), never a
     *  billed completion. The renderer names a provider and nothing else. */
    checkProvider: (provider: AiProvider) => Promise<AiProviderCheck>;
    /** Every agent forecast, scored against the market once it resolves. */
    scoreboard: (recent?: number) => Promise<ForecastScoreboard>;
  };
  /** Autopilot: the app runs one of your agents on a timer with your AI key. */
  autopilot: {
    status: () => Promise<AutopilotStatus | null>;
    /** Starts one run now (billed to the user's key). Budgets are re-checked. */
    runNow: () => Promise<{ ok: boolean; message: string }>;
  };
  /** The ACCOUNT's paper book (the terminal's `manual` book). */
  paper: {
    status: () => Promise<PaperStatus | null>;
    /** Empties the account's paper book back to its bankroll. Live untouched. */
    reset: () => Promise<ActionResult<unknown>>;
  };
  /** Every connection, one row each, with a fix for the broken ones. `deep`
   *  runs the network checks — pass it only on the user's click. */
  alerts: {
    /** null: the backend can't answer right now. */
    list: () => Promise<AppAlert[] | null>;
  };
  health: {
    check: (deep?: boolean) => Promise<HealthReport | null>;
  };
  /**
   * Remote control via chat bots. Setup only — the commands themselves never
   * pass through the renderer; they arrive at the backend from Discord or
   * Telegram and are authorised there against the paired-id allowlist.
   */
  remote: {
    status: () => Promise<RemoteStatus>;
    /** Verified against the platform BEFORE it is stored. */
    setToken: (platform: 'discord' | 'telegram', token: string)
      => Promise<{ ok: boolean; message: string; username?: string }>;
    /** Mint a short-lived code the user types into the chat to pair. */
    pairCode: (platform: 'discord' | 'telegram')
      => Promise<{ code: string; expiresAt: string; ttlSec: number }>;
    /** Add an id by hand (the Discord path — the id is easy to copy). */
    addPaired: (platform: 'discord' | 'telegram', id: string, label?: string)
      => Promise<{ paired: RemotePairedAccount[]; error: string | null }>;
    unpair: (platform: 'discord' | 'telegram', id: string)
      => Promise<{ paired: RemotePairedAccount[] }>;
    /** Start/stop each transport to match config. Idempotent. */
    sync: () => Promise<RemoteStatus>;
    test: () => Promise<{ sent: number; message: string }>;
  };
  /** AI agents over MCP. Setup and oversight only — agents talk to the
   *  backend directly over loopback, never through the renderer. */
  mcp: {
    status: () => Promise<McpStatus>;
    sync: () => Promise<McpStatus>;
    /** A new token for ONE named agent. Its token-less client configs keep
     *  working (the relay reads the store); HTTP callers need the new one. */
    rotateToken: (agentId: string) => Promise<McpStatus>;
    /** One agent's own paper account; `reset` empties it first. */
    paper: (agentId: string, reset?: boolean) => Promise<AgentPaper>;
    /** Approve or reject a live agent order waiting for the user. Approval is
     *  re-vetted against the market as it is now before anything is sent. */
    decide: (id: number, approve: boolean) => Promise<{ ok: boolean; message: string }>;
    /** Check a live agent order whose outcome is unknown against Polymarket
     *  and book it (sent, filled, cancelled, or void if it never existed), so
     *  it stops holding the agent's spend and position slots. */
    settleUnknown: (id: number) => Promise<{ ok: boolean; status?: string; message: string }>;
    activity: (limit?: number) => Promise<McpActivity>;
    /** Agent clients seen this session (backend memory only). */
    seen: () => Promise<McpSeen>;
    /** curl + Python for the HTTP API, that agent's token included — put on
     *  the clipboard by MAIN. The renderer gets `{ ok }`, never the text. */
    copyHttpSnippet: (agentId: string) => Promise<{ ok: boolean; message?: string }>;
    /** Every client's setup for one agent (Default when omitted). */
    clients: (agentId?: string) => Promise<McpClientSetup[]>;
    install: (clientId: McpClientId, agentId: string) => Promise<McpInstallResult>;
    uninstall: (clientId: McpClientId, agentId: string) => Promise<McpInstallResult>;
    /** Put that client's (token-less) setup on the clipboard. */
    copyConfig: (clientId: McpClientId, agentId: string) => Promise<{ ok: boolean; message?: string }>;
    /** Open the folder that client reads its config from. Takes no path. */
    openConfigFolder: (clientId: McpClientId) => Promise<{ ok: boolean; message?: string }>;
    /** Close one agent's PAPER positions at the bid. Nothing live is touched. */
    closeAgentPaper: (agentId: string) => Promise<{
      ok: boolean; message: string; closed?: unknown[]; open?: unknown[];
    }>;
    onOrder: (cb: (d: McpOrderEvent) => void) => () => void;
    /** Every agent tool call from the backend's one dispatcher. Throttled and
     *  whitelisted there (no args, no token). */
    onToolCall: (cb: (d: McpToolCallEvent) => void) => () => void;
  };
  logs: {
    tail: (limit?: number) => Promise<LogEntry[]>;
    onAppend: (cb: (entry: LogEntry) => void) => () => void;
    clear: () => Promise<ActionResult>;
    openFolder: () => Promise<void>;
  };
  window: {
    minimize: () => void;
    maximize: () => void;
    close: () => void;
    isMaximized: () => Promise<boolean>;
    onMaximizeChange: (cb: (max: boolean) => void) => () => void;
    isVisible: () => Promise<boolean>;
    onVisibility: (cb: (visible: boolean) => void) => () => void;
  };
}

export interface PositionFilter {
  status?: BotPosition['status'][];
  resolved?: boolean | null;
  signalSource?: SignalSource | null;
  limit?: number;
}

declare global {
  interface Window {
    krypt: KryptApi;
  }
}
