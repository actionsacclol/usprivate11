// ────────────────────────────────────────────────────────────────────────
// THE TERMINAL CONTRACT
//
// Deliberately separate from `shared/types.ts`, which is the ENGINE contract:
// `BotPosition` / `SignalRow` only describe things one of our engines did or
// watched inside this session. The types below describe *any* Polymarket
// market, assembled on demand from several providers that disagree, and are
// the row shapes every terminal screen renders.
//
// If the two ever merge you get one struct where half the fields are only
// populated sometimes and no way to tell which half. Keep them apart.
//
// THE ONE RULE
//   A value nobody could produce is `null`, renders as an em dash, and is
//   never coerced to 0 or to a default. When something cannot be shown, the
//   row also carries WHY, and what would fix it (`unknowns`).
// ────────────────────────────────────────────────────────────────────────

/** Where a single number actually came from. Carried per field, not per row. */
export type DataSource =
  | 'gateway'    // public market metadata and quotes (list rows cached up to 30 s by the exchange)
  | 'gamma'      // the same, in records saved under the name the code used before the US port
  | 'clob'       // the exchange's REST book and price history
  | 'clob-ws'    // OUR OWN websocket feed, stamped when WE received it
  | 'data-api'   // the exchange's record of this account
  | 'venue'      // the exchange's REST API, whatever the endpoint
  | 'ws'         // the exchange's websocket, as above
  | 'ledger'     // our own reconciled fill ledger (the local DB)
  | 'local';     // computed on this machine from the above

export const DATA_SOURCE_LABEL: Record<DataSource, string> = {
  gateway: 'Public market data',
  gamma: 'Public market data',
  clob: 'Polymarket US',
  'clob-ws': 'Our live feed',
  'data-api': 'Your Polymarket US account',
  venue: 'Polymarket US',
  ws: 'Our live feed',
  ledger: 'Your fill ledger',
  local: 'Computed locally',
};

/** A per-field stamp: which provider answered, and when WE saw it. */
export interface SourceStamp {
  source: DataSource;
  /** ISO8601. Local arrival time — when we received it, not when they made it. */
  observedAt: string;
  /** Seconds stale at assembly. null when the provider dates nothing. */
  ageSec: number | null;
}

/** Fields that carry provenance. Adding one here forces the provider to stamp it. */
export type MarketField =
  | 'probability' | 'bid' | 'ask' | 'spread' | 'mid'
  | 'volume24h' | 'volumeTotal' | 'liquidity'
  | 'priceChange24h' | 'priceChange1h'
  | 'closeTime' | 'status' | 'lastTrade' | 'book' | 'history'
  // A position's average cost. Carries provenance because it can come from
  // OUR fill ledger or from Polymarket US's own record of this account, and the
  // two are not the same claim.
  | 'avgCost';

export type Provenance = Partial<Record<MarketField, SourceStamp>>;

/**
 * Why a field is missing — shown verbatim in the UI.
 * "No holder data" is useless; "the free endpoint refuses it — add a key in
 * Settings" is a product. Write these as the second kind.
 */
export interface UnknownReason {
  field: MarketField | string;
  reason: string;
}

export type MarketStatus = 'open' | 'closed' | 'settled' | 'inactive' | 'unknown';

/**
 * The venue. NEVER blur two venues into one UI: Kalshi is a CFTC-regulated US
 * exchange and Polymarket is not the same thing legally, so their fees,
 * settlement guarantees and resolution sources are each their own.
 */
export type Venue = 'polymarket' | 'kalshi';

export const VENUE_LABEL: Record<Venue, string> = {
  polymarket: 'Polymarket US',
  kalshi: 'Kalshi',
};

/**
 * The row shape every terminal screen renders. Prices are PROBABILITY IN
 * CENTS, 0..100 — bounded, which is a gift the crypto version never had.
 */
export interface MarketSummary {
  conditionId: string;
  venue: Venue;
  title: string;
  slug: string | null;
  eventSlug: string | null;
  category: string | null;
  status: MarketStatus;
  /** The real outcome labels, e.g. ["Yes","No"]. null when the exchange gave none. */
  outcomes: [string, string] | null;

  /** YES probability in cents 0..100. null when no provider had a price. */
  probability: number | null;
  /** Best bid / best ask for the YES token, cents. */
  bid: number | null;
  ask: number | null;
  /** ask - bid. null unless BOTH sides are known — never a half-spread. */
  spread: number | null;
  /** (bid+ask)/2. null unless both sides known. */
  mid: number | null;

  volume24hUsd: number | null;
  volumeTotalUsd: number | null;
  liquidityUsd: number | null;

  /**
   * SIGNED probability move over the window, in cents. +12.5 means the YES
   * price is 12.5c higher than a day ago.
   *
   * The exchange omits these on a market that has not moved in the window, and it
   * omits them on roughly a quarter of live rows — so null means "not
   * reported", NOT "did not move". The volatility column sorts an unknown to
   * the end rather than treating it as zero, because ranking a market as
   * least-volatile on the strength of a missing field is a judgement nobody
   * made.
   */
  priceChange24h: number | null;
  priceChange1h: number | null;

  closeTime: string | null;
  /** Hours until close; negative when past. null when closeTime unknown. */
  hoursToClose: number | null;

  tickSize: number | null;
  minOrderSize: number | null;
  yesTokenId: string | null;
  noTokenId: string | null;

  /** Set only once the market actually settled. */
  resolvedOutcome: 'yes' | 'no' | null;

  sources: Provenance;
  unknowns: UnknownReason[];
}

// ───────── order book ────────────────────────────────────────────────────

export interface BookLevel {
  priceCents: number;
  /** Contracts resting at this level. */
  size: number;
  /** Running total of `size` from the touch outward. */
  cumulative: number;
  /** size * priceCents / 100 — dollars it would cost to lift this level. */
  notionalUsd: number;
}

/**
 * Both sides of one token's book. `available: false` means the venue returned
 * nothing — which is NOT the same as an empty book, and the UI must say which.
 */
export interface BookSnapshot {
  conditionId: string;
  tokenSide: 'yes' | 'no';
  bids: BookLevel[];
  asks: BookLevel[];
  bestBid: number | null;
  bestAsk: number | null;
  spreadCents: number | null;
  midCents: number | null;
  /** Sum of notional on each side, dollars. */
  bidDepthUsd: number | null;
  askDepthUsd: number | null;
  available: boolean;
  fetchedAt: string;
  source: DataSource;
  note: string | null;
}

// ───────── price history ─────────────────────────────────────────────────

export type TerminalInterval = '1h' | '6h' | '1d' | '1w' | 'max';

export const INTERVALS: { id: TerminalInterval; label: string }[] = [
  { id: '1h', label: '1H' },
  { id: '6h', label: '6H' },
  { id: '1d', label: '1D' },
  { id: '1w', label: '1W' },
  { id: 'max', label: 'MAX' },
];

export interface PricePoint {
  /** Unix seconds. */
  t: number;
  /** Probability in cents, 0..100. */
  p: number;
}

export interface PriceSeries {
  conditionId: string;
  side: 'yes' | 'no';
  interval: TerminalInterval;
  /**
   * Never gap-filled. A period with no trades produces NO point, not a flat
   * synthetic one — an invented candle in an illiquid market is how someone
   * buys a corpse.
   */
  points: PricePoint[];
  source: DataSource;
  /** Built from our own websocket tape rather than the venue index. */
  ownFeed: boolean;
  /** Real data, but very few points. Say so; do NOT discard it. */
  thin: boolean;
  note: string | null;
}

// ───────── resolution risk (the "security panel" of a prediction market) ──

export type RiskLevel = 'ok' | 'watch' | 'risk' | 'unknown';

export interface ResolutionCheck {
  id: string;
  label: string;
  level: RiskLevel;
  /** What we actually found. Shown verbatim — no rounding to a verdict. */
  detail: string;
  /**
   * Whether the check could be RUN at all. An unread check is `false` and is
   * excluded from the score — it is never silently counted as a pass.
   */
  resolved: boolean;
}

export interface ResolutionReport {
  conditionId: string;
  checks: ResolutionCheck[];
  resolvedCount: number;
  totalCount: number;
  /**
   * 0..100, averaged over the checks that ACTUALLY resolved. Below
   * MIN_RESOLVED_CHECKS this is null and the UI prints no score at all.
   */
  score: number | null;
  /** e.g. "averaged over the 6 of 11 checks that resolved". */
  scoreNote: string;
  /** Human resolver / oracle, when we could read it. */
  resolverName: string | null;
  resolutionSource: string | null;
  fetchedAt: string;
}

/** Fewer resolved checks than this and we refuse to print a score. */
export const MIN_RESOLVED_CHECKS = 4;

// ───────── discover ──────────────────────────────────────────────────────

export type DiscoverColumn =
  | 'new'         // recently listed
  | 'closing'     // closing soonest, still tradable
  | 'volume'      // highest 24h volume
  | 'volatility'  // biggest absolute 24h price move
  | 'watchlist';  // the user's own list

export const DISCOVER_COLUMNS: { id: DiscoverColumn; label: string; hint: string }[] = [
  { id: 'new', label: 'New', hint: 'Recently listed markets' },
  { id: 'closing', label: 'Closing soon', hint: 'Still tradable, closing soonest' },
  { id: 'volume', label: 'High volume', hint: 'Most traded so far' },
  { id: 'volatility', label: 'High volatility', hint: 'Biggest 24h price move' },
  { id: 'watchlist', label: 'Watchlist', hint: 'Markets you starred' },
];

/**
 * NUMERIC filters SKIP a row whose metric has not arrived — a threshold on a
 * quantity nobody produced is not a judgement about that row.
 * CATEGORICAL filters EXCLUDE it — the user named a set, and "I do not know"
 * is not a member of it. Both behaviours are pinned by tests.
 */
export interface DiscoverFilters {
  minVolume24hUsd?: number | null;
  maxVolume24hUsd?: number | null;
  minLiquidityUsd?: number | null;
  minProbability?: number | null;
  maxProbability?: number | null;
  maxHoursToClose?: number | null;
  minHoursToClose?: number | null;
  /** Categorical. Empty/absent = no filter. */
  categories?: string[];
  search?: string;
}

export interface DiscoverResult {
  column: DiscoverColumn;
  rows: MarketSummary[];
  fetchedAt: string;
  /** Rows the provider returned that a NUMERIC filter skipped for lack of data. */
  skippedForUnknown: number;
  /** Rows a CATEGORICAL filter excluded. */
  excluded: number;
  note: string | null;
}

// ───────── act (manual orders) ───────────────────────────────────────────

export type OrderType = 'GTC' | 'FAK' | 'FOK';

/**
 * The renderer names an ENTITY, a side, a price and a size — never a URL or a
 * host. Main decides which of its hardcoded hosts to contact. That is the
 * structural fix for SSRF; a URL validator is the fragile one.
 */
export interface ManualOrderRequest {
  conditionId: string;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  /** Probability in cents, 1..99. Snapped to the market tick by the backend. */
  priceCents: number;
  /** Contracts. Raised to the venue minimum by the backend, which says so. */
  size: number;
  orderType: OrderType;
  /**
   * GTC only. The order may rest and may never take: if it would trade right
   * away it is refused and nothing is sent. A fill is a maker fill, no fee.
   */
  postOnly?: boolean;
  /**
   * The dollar cost the UI showed the user. The MAIN process re-computes it
   * and refuses the order if it drifted — so a hostile renderer cannot spend
   * more than the number a human actually read.
   */
  confirmUsd: number;
  /**
   * Preview/place this against the PAPER simulator. Set from the user's
   * setting, threaded to the backend so one place decides what blocks a
   * ticket — paper needs no API key, but every other refusal still applies.
   */
  paper?: boolean;
}

export interface ManualOrderPreview {
  conditionId: string;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  /** After tick-snapping. Differs from what was typed; the UI shows both. */
  priceCents: number | null;
  requestedPriceCents: number;
  /** After the venue minimum was applied. */
  size: number | null;
  requestedSize: number;
  costUsd: number | null;
  /** Contracts available at or better than `priceCents`. null = book unknown. */
  fillableSize: number | null;
  /** Average price you would actually pay walking the book. null when unknown. */
  estimatedAvgCents: number | null;
  /** estimatedAvg - priceCents, in cents. null when either is unknown. */
  slippageCents: number | null;
  feeUsd: number | null;
  maxPayoutUsd: number | null;
  maxLossUsd: number | null;
  tickSize: number | null;
  minOrderSize: number | null;
  /** The touch when this ticket was priced. null = the book was unreadable. */
  bestBidCents: number | null;
  bestAskCents: number | null;
  /**
   * Would this price cross RIGHT NOW (buy ≥ best ask, sell ≤ best bid)? false
   * means the order would REST — an offer, not a trade. null when the touch
   * could not be read, which is not the same as "no".
   */
  crossesNow: boolean | null;
  /**
   * Cash this account can spend — the cached account balance live, the paper
   * cash in paper mode. null = we could not read it, which blocks nothing and
   * must never render as $0.00.
   */
  availableUsd: number | null;
  warnings: string[];
  /** Hard blockers — the ticket must not submit while this is non-empty. */
  blockers: string[];
}

export interface ManualOrderResult {
  ok: boolean;
  orderId: string | null;
  status: string | null;
  filledSize: number | null;
  avgPriceCents: number | null;
  message: string;
  /** Local row id for the order in our ledger, when we recorded one. */
  localId: number | null;
}

// ───────── flatten: sell every open position on the ACCOUNT ──────────────
//
// The engines each flatten what THEY opened. This flattens what you HOLD,
// which is a larger set: hand-placed positions, script positions, and
// anything bought before this app existed. Every sell still goes THROUGH
// `place()` in the backend, so the paper gate, the manual ceilings and the
// order log all apply exactly as they do to a single click.

export interface FlattenPreviewRow {
  conditionId: string;
  side: 'yes' | 'no';
  size: number;
  /** The bid this exit would be sent at. null when the book had none. */
  bidCents: number | null;
  /** Contracts that would actually cross. Less than `size` = partial exit. */
  fillableSize: number | null;
  /** Estimated proceeds. null when unknown or when the row is blocked. */
  proceedsUsd: number | null;
  /** Why this one will NOT be sold. null means it will be. */
  blocked: string | null;
  /** The killed-remainder warning, when only part of the size can cross. */
  partial: string | null;
  /** Whether an engine still tracks this market. null = we could not tell. */
  engineTracked: boolean | null;
}

/**
 * What a flatten WOULD do. Read-only — it prices, it never sends. Every
 * figure is an estimate off a book that is already a moment old, and an
 * unknown one is null so the UI prints an em dash.
 */
export interface FlattenPreview {
  ok: boolean;
  positions: FlattenPreviewRow[];
  /** How many of `positions` are actually sellable — `positions.length`. */
  count: number;
  /** Rows that held nothing. */
  skipped: number;
  /**
   * Resolved positions dropped BEFORE any book read. A settled market is
   * closed on the exchange, so there is no book to sell into. Reported as
   * one count with one sentence, never as one refusal row each.
   */
  settledSkipped?: number;
  proceedsUsd: number | null;
  engineTracked: number | null;
  paper: boolean;
  message: string;
}

export interface FlattenResultRow {
  conditionId: string;
  side: 'yes' | 'no';
  size: number;
  ok: boolean;
  message: string;
}

export interface FlattenResult {
  ok: boolean;
  /** Exits sent or refused by us. `sold` + `refused` always equals this. */
  attempted: number;
  sold: number;
  /** Refused by us OR rejected by the venue — either way, still open. */
  refused: number;
  /** Rows that held nothing to begin with. */
  skipped: number;
  /** Resolved positions left alone — see `FlattenPreview.settledSkipped`. */
  settledSkipped?: number;
  /** Estimated from the previews. null when any accepted sell was unpriceable. */
  proceedsUsd: number | null;
  /** How many sold positions an engine was tracking. null = unreadable. */
  engineTrackedSold: number | null;
  reason: string;
  results: FlattenResultRow[];
  message: string;
}

// ───────── sell ONE position: the marketable exit ───────────────────────
//
// The order ticket can already sell — but a hand-typed sell at the price you
// BOUGHT at rests on the book and never fills, which is not an exit, it is an
// offer. This is the other shape of the same operation: name a position and a
// slippage tolerance, and the backend prices a marketable fill-and-kill
// against the bids. It never sends a size that would leave a resting
// remainder, and the limit it uses is a promise about the worst price you can
// get, not a hint.

export interface SellPositionRequest {
  conditionId: string;
  side: 'yes' | 'no';
  /** Contracts. Omit for the whole position. Floored to whole contracts. */
  size?: number;
  /**
   * How far below the best bid the limit may go, in cents. The order can
   * never fill below `bestBid − this`. Default 5.
   */
  maxSlippageCents?: number;
  /**
   * The proceeds figure the user READ. Main re-derives it and refuses if the
   * book moved — required by `sellPosition`, ignored by the preview.
   */
  confirmUsd?: number;
}

/**
 * Where the average cost came from. `paper` is the paper ledger's own
 * average — never mixed with a real one.
 */
export type SellBasisSource = 'ledger' | 'data-api' | 'paper' | null;

export interface SellPreview {
  conditionId: string;
  side: 'yes' | 'no';
  /** The venue's own word for the side held — "Up", "Over", a team name. */
  sideLabel: string | null;
  title: string | null;
  /** Whether this priced the PAPER book. Decided in main, not the renderer. */
  paper: boolean;
  /** Contracts held right now. null when the position could not be read. */
  positionSize: number | null;
  /** What was asked for, before the whole-contract floor. */
  requestedSize: number | null;
  /** What we are trying to sell, in whole contracts. */
  size: number | null;
  /**
   * What actually fits inside the slippage cap, and therefore the size the
   * order is sent at. Less than `size` means the rest stays in the position —
   * we never send a size that would leave a resting remainder.
   */
  fillableSize: number | null;
  bestBidCents: number | null;
  /** The limit the order carries: the deepest level the walk touched. */
  limitCents: number | null;
  estimatedAvgCents: number | null;
  /** Your limit IS your worst case on a marketable order. */
  worstFillCents: number | null;
  maxSlippageCents: number;
  /** `fillableSize` at `estimatedAvgCents`. An estimate, never a quote. */
  proceedsUsd: number | null;
  /** Taker fee on the fee-v2 curve. null = unknown, which is NOT zero. */
  feeUsd: number | null;
  netProceedsUsd: number | null;
  avgCostCents: number | null;
  basisSource: SellBasisSource;
  costBasisUsd: number | null;
  /**
   * Net proceeds minus what these contracts cost. null when the basis is
   * unknown — never zero, never break-even. Gross of fees (and warned about)
   * when the market's fee rate could not be read.
   */
  estimatedPnlUsd: number | null;
  markCents: number | null;
  settled: boolean;
  resolvedOutcome: 'yes' | 'no' | null;
  minOrderSize: number | null;
  tickSize: number | null;
  warnings: string[];
  /** Hard blockers — the Sell button must not send while this is non-empty. */
  blockers: string[];
  /** The "off the book as it is now, not a quote" sentence. */
  note: string;
}

export interface SellResult {
  ok: boolean;
  orderId: string | null;
  status: string | null;
  localId: number | null;
  /** Contracts actually sent — `SellPreview.fillableSize`. */
  sentSize: number | null;
  limitCents: number | null;
  /**
   * What filled. null on a live order until it reconciles against
   * Polymarket's own ledger a few seconds later — never the estimate wearing
   * a disguise.
   */
  filledSize: number | null;
  avgPriceCents: number | null;
  proceedsUsd: number | null;
  /** Sent minus filled. FAK kills the remainder, which costs nothing. */
  killedSize: number | null;
  /** What the preview said it would fetch, carried for the record. */
  estimatedProceedsUsd: number | null;
  message: string;
  /** The preview this was sent against, including any refusal's reason. */
  preview: SellPreview | null;
}

/** Who created this order. A terminal must never blur manual and automated. */
export type OrderOrigin =
  | 'manual' | 'crypto15m' | 'script' | 'ai' | 'external' | 'unknown';

export const ORIGIN_LABEL: Record<OrderOrigin, string> = {
  manual: 'You',
  crypto15m: 'Crypto engine',
  script: 'Your script',
  ai: 'AI agent',
  external: 'Placed elsewhere',
  unknown: 'Unknown',
};

export interface RestingOrder {
  orderId: string;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  priceCents: number | null;
  size: number | null;
  filledSize: number | null;
  /** Still on the book: `size − filledSize`. null when either is unknown. */
  remainingSize?: number | null;
  /**
   * The price this order must reach to fill — the best ask for a buy, the best
   * bid for a sell. Only populated when the list was fetched for ONE market
   * (`orders(conditionId)`); null elsewhere, and null is "we did not read it",
   * never "there is no touch".
   */
  bestTouchCents?: number | null;
  createdAt: string | null;
  origin: OrderOrigin;
  source: DataSource;
}

/**
 * A standing instruction the user WROTE DOWN that fires later — a stop loss, a
 * take profit, an alert. It is still user-directed: no strategy, score or
 * signal creates one. Every pending instruction is visible and cancellable on
 * one page.
 */
export type StandingKind = 'stopLoss' | 'takeProfit' | 'alertAbove' | 'alertBelow';

export const STANDING_LABEL: Record<StandingKind, string> = {
  stopLoss: 'Stop loss',
  takeProfit: 'Take profit',
  alertAbove: 'Alert above',
  alertBelow: 'Alert below',
};

export interface StandingInstruction {
  id: string;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  kind: StandingKind;
  /**
   * The trigger, in PROBABILITY POINTS (cents) — not a percentage. A stop on a
   * bounded 0..100 quantity is a point, and saying "%" would be a lie.
   */
  triggerCents: number;
  /** Contracts to close. null on an alert, which trades nothing. */
  size: number | null;
  createdAt: string;
  armed: boolean;
  lastCheckedAt: string | null;
  firedAt: string | null;
  note: string | null;
}

// ───────── track ─────────────────────────────────────────────────────────

/**
 * Cost basis convention: AVERAGE COST, not FIFO — that is what a trader means
 * by "I am up 2x on this". The UI says so, so nobody has to guess.
 */
export interface TerminalPosition {
  conditionId: string;
  venue: Venue;
  title: string | null;
  slug: string | null;
  /**
   * Which of the market's two outcomes is held, by INDEX: 'yes' is outcome 0,
   * 'no' is outcome 1. Never inferred from the label text — outcome 0 is
   * spelled "Yes" on a binary market, "Up" on a crypto one and a team name on
   * a sports one, and reading the word instead of the index is what used to
   * file half this account's fills under the wrong side.
   */
  side: 'yes' | 'no';
  /** The venue's own word for that side — "Up", "Over", a team name. */
  sideLabel?: string | null;
  size: number;
  /**
   * Average cost in cents. null only when NEITHER our fill ledger nor
   * Polymarket US's own record of this account can produce one. NEVER
   * break-even, never 0.
   */
  avgCostCents: number | null;
  costBasisUsd: number | null;
  /**
   * Where `avgCostCents` came from: computed here from your reconciled fills,
   * or taken from Polymarket US's own average for this account when our ledger
   * does not reach back far enough. null when there is no basis at all.
   */
  basisSource?: 'ledger' | 'data-api' | null;
  markCents: number | null;
  valueUsd: number | null;
  /**
   * OPEN positions only. A settled market has no unrealised anything — the
   * outcome is decided, so its P&L is realised. null whenever avgCostCents is null: an unknown basis has
   * unknown P&L.
   */
  unrealizedPnlUsd: number | null;
  /** Banked: earlier partial sells, plus settlement once the market resolves. */
  realizedPnlUsd: number | null;
  /** The settlement half of `realizedPnlUsd`. null while the market is open. */
  settledPnlUsd: number | null;
  /**
   * Polymarket's OWN P&L for this position, carried verbatim so ours can be
   * reconciled against it instead of merely trusted.
   */
  venueCashPnlUsd?: number | null;
  venueRealizedPnlUsd?: number | null;
  status: MarketStatus;
  /** By INDEX, same vocabulary as `side` — so the comparison is meaningful. */
  resolvedOutcome: 'yes' | 'no' | null;
  /** The venue's word for the winning outcome, when we know it. */
  resolvedOutcomeLabel?: string | null;
  /** Both outcome labels in index order, [outcome0, outcome1]. */
  outcomeLabels?: [string, string] | null;
  closeTime: string | null;
  /**
   * No cost basis could be established from EITHER source. Such a position is
   * excluded from every aggregate figure and counted in a warning strip —
   * never guessed at.
   */
  unreconciled: boolean;
  unreconciledSize: number | null;
  /**
   * The outcome is posted, so `markCents` is EXACT (100 or 0) rather than a
   * mark. Prediction markets settling to 0 or 1 is the one place this domain
   * beats crypto: realised P&L is not an estimate.
   */
  settled: boolean;
  sources: Provenance;
  unknowns: UnknownReason[];
}

export interface TerminalPortfolio {
  positions: TerminalPosition[];
  /**
   * Did the account actually answer? `false` means the empty `positions` above
   * is "we could not ask", NOT "you hold nothing" — and those are opposite
   * instructions to anything about to sell. Absent on older payloads, which
   * are treated as readable.
   */
  walletReadable?: boolean;
  /** Aggregates over RECONCILED rows only. null when nothing reconciled. */
  totalValueUsd: number | null;
  totalCostUsd: number | null;
  /** Open positions only — see TerminalPosition.unrealizedPnlUsd. */
  totalUnrealizedPnlUsd: number | null;
  /**
   * Sells banked in the fill ledger PLUS the settlement of every market the
   * ledger has fills for — NOT just the ones the account still holds.
   *
   * Gross of fees, so it stays comparable with the venue's own figures — see
   * `realizedPnlNetUsd` for what actually reached the balance.
   */
  realizedPnlUsd: number | null;
  /** Trading fees across the whole ledger. null when any fill's fee is unknown. */
  realizedFeesUsd?: number | null;
  /** `realizedPnlUsd` after `realizedFeesUsd`. The number that moved. */
  realizedPnlNetUsd?: number | null;
  /** How much of `realizedPnlUsd` came from settlement rather than selling. */
  settledPnlUsd: number | null;
  /**
   * Polymarket's OWN total P&L across the same positions. Shown beside ours so
   * the two can be reconciled rather than trusted — where they disagree the
   * user can see it instead of guessing which screen is lying.
   */
  venueCashPnlUsd?: number | null;
  /**
   * OUR P&L over the same held-positions scope `venueCashPnlUsd` covers. That
   * comparison must use this, not `settledPnlUsd`, which now spans the whole
   * ledger and would look like a wild disagreement.
   */
  walletPnlUsd?: number | null;
  /**
   * The record, computed from the FILL LEDGER rather than current holdings:
   * markets where we held the winning side into settlement. `wonMarkets + lostMarkets` deliberately does not equal
   * `ledgerMarkets` — `bothSidesMarkets` are counted in both columns.
   */
  wonMarkets?: number | null;
  lostMarkets?: number | null;
  /** Markets held on both sides into settlement, counted in both columns. */
  bothSidesMarkets?: number | null;
  /** Outstanding contracts whose outcome we could not read. Never a loss. */
  unknownResolutionMarkets?: number | null;
  /** Sold back before resolution, so no outcome decided them. */
  closedOutMarkets?: number | null;
  /** Distinct markets the fill ledger covers. */
  ledgerMarkets?: number | null;
  /** One sentence saying what the counts above are and are not. */
  recordNote?: string | null;
  /** Rows with NO cost basis from either source, left out of the aggregates. */
  unreconciledCount: number;
  unreconciledNote: string | null;
  /** Rows priced off the venue's own average rather than our fill ledger. */
  venueBasisCount?: number;
  cashUsd: number | null;
  fetchedAt: string;
  costBasisConvention: 'average';
}

// ───────── the settled RECORD, from the fill ledger ───────────────────────
//
// `TerminalPortfolio.positions` is what the account holds now. A settled
// position leaves that list, so it can't serve as a record. This is the other
// thing: every market the FILL LEDGER settled.

export type SettledOutcome = 'won' | 'lost' | 'closedOut' | 'unknown';

export interface SettledRecordRow {
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  /** The venue's own word for the side held — "Up", "Over", a team name. */
  sideLabel: string | null;
  /** Contracts still outstanding at settlement. 0 = closed by selling. */
  contracts: number;
  avgCostCents: number | null;
  costUsd: number;
  /** $1 a contract on the winner, $0 on the loser. null when nothing settled. */
  payoutUsd: number | null;
  /** Net of the fees the ledger knows. null when they are not all known. */
  pnlUsd: number | null;
  feesUsd: number | null;
  feesKnown: boolean;
  /** What the sells alone banked, so a closed-out row can show its working. */
  soldPnlUsd: number;
  outcome: SettledOutcome;
  resolvedOutcome: 'yes' | 'no' | null;
  resolvedOutcomeLabel: string | null;
  /** The market's close/resolution time. null when the venue never said. */
  settledAt: string | null;
  /** Still listed on the account. null = it could not be read, NOT "no". */
  inWallet: boolean | null;
  /** Both sides were held to settlement, so this market won AND lost. */
  bothSides: boolean;
}

export interface SettledRecordTotals {
  /** Markets, not rows — and the same sets `TerminalPortfolio` counts. */
  won: number;
  lost: number;
  /** Counted in BOTH columns above, so the two do not add up to `markets`. */
  bothSides: number;
  closedOut: number;
  unknown: number;
  /** Markets that are done. A market still open is not counted here. */
  markets: number;
  /** Held on a market that has not closed yet: not in the record. */
  openMarkets?: number;
  /** Markets that are done — won, lost or closed out, deduplicated. */
  settledMarkets: number;
  rows: number;
  /** Net across the rows that have a number. null when none does. */
  pnlUsd: number | null;
  /** Rows that total could not include. */
  pnlMissingRows: number;
  feesUsd: number | null;
  feesKnown: boolean;
  unknownRows: number;
}

export interface SettledRecord {
  rows: SettledRecordRow[];
  totals: SettledRecordTotals;
  /** false → every `inWallet` is null, because the account read failed. */
  walletReadable: boolean;
  fetchedAt: string;
  note: string | null;
}

export interface TerminalFill {
  id: string;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  size: number;
  /** What it ACTUALLY filled at, from the venue ledger — not what we asked. */
  priceCents: number | null;
  feeUsd: number | null;
  /**
   * The exchange's own word: maker (the order was resting, no fee), taker (it
   * crossed), mixed. null = not read yet, or a fill from before this was kept.
   */
  liquidity?: 'maker' | 'taker' | 'mixed' | null;
  costUsd: number | null;
  ts: string;
  /** Matched against the venue own fills endpoint. */
  reconciled: boolean;
  origin: OrderOrigin;
  source: DataSource;
}

// ───────── cross-venue (Polymarket vs Kalshi) ────────────────────────────

/**
 * How confident we are that these two markets ask the SAME question.
 *
 * Nothing in either API says they do — it is inferred from words, numbers and
 * dates. A wrong pairing produces a large, confident, entirely fictional
 * "edge", so anything below `possible` is reported as NO match rather than a
 * bad one, and only a `strong` match ever gets a spread printed next to it.
 */
export type MatchConfidence = 'strong' | 'possible';

/**
 * WHICH two things were compared.
 *
 * Not always YES against YES. Kalshi lists plenty of levels from the opposite
 * direction — "$75,999.99 or below" where Polymarket asks "above $76,000" —
 * and there the answer to the Polymarket question is Kalshi's NO price, so the
 * quote and the history line are inverted. That inversion changes the SIGN of
 * the spread, so it is stated rather than left to be inferred.
 */
export interface ComparedSides {
  polymarket: 'yes';
  kalshi: 'yes' | 'no';
}

export interface CrossVenueMatch {
  confidence: MatchConfidence;
  /** 0..1. Shown so the pairing can be judged, not just trusted. */
  score: number;
  /** Why we think it matches — rendered verbatim. */
  for: string[];
  /** What argues against it. Also rendered, and never hidden on a good score. */
  against: string[];
  /** Which side of each venue's book the numbers above came from. */
  comparedSides: ComparedSides;
}

export interface CrossVenueSide {
  title: string | null;
  probability: number | null;
  bid: number | null;
  ask: number | null;
  spread: number | null;
  closeTime: string | null;
  volume24hUsd: number | null;
  liquidityUsd: number | null;
}

export interface KalshiSide extends CrossVenueSide {
  ticker: string;
  /**
   * The leg within the event, e.g. "Cut 25bps" — or, when the NO side was the
   * comparable one, `NO on "..."`, because the number beside it is 100 minus
   * the exchange's YES quote.
   */
  subtitle: string | null;
  /**
   * Which side of the Kalshi book `probability` / `bid` / `ask` and the `k`
   * history line report. 'no' means every one of them is inverted.
   */
  side: 'yes' | 'no';
  /** Public Kalshi page, so the user can check both numbers themselves. */
  url: string;
}

/** One bucket of the shared time grid. `null` = that venue did not trade. */
export interface CrossVenuePoint {
  t: number;
  /** Polymarket, cents. */
  p: number | null;
  /** Kalshi, cents. */
  k: number | null;
}

export interface CrossVenueComparison {
  conditionId: string;
  interval: TerminalInterval;
  polymarket: CrossVenueSide | null;
  kalshi: KalshiSide | null;
  match: CrossVenueMatch | null;
  /** Kalshi minus Polymarket, cents. null unless the match is `strong`. */
  spreadCents: number | null;
  spreadNote: string | null;
  series: CrossVenuePoint[];
  /** Buckets where BOTH venues traded — the only ones truly comparable. */
  overlapPeriods: number;
  historyNote: string | null;
  /** The legal/settlement difference between the venues. Always shown. */
  venueNote: string;
  /** What a price gap is NOT, by itself. */
  caveats: string[];
  fetchedAt: string;
  note: string | null;
}

// ───────── related markets ───────────────────────────────────────────────

export interface RelatedMarket extends MarketSummary {
  /** 0..1 token similarity. Forced to 0 for OPPOSITE questions. */
  titleOverlap: number;
  /**
   * Distance from this market's price, in cents — only set for a sibling
   * asking a near-identical question. null for a different leg of the event,
   * which is supposed to be priced differently.
   */
  gapCents: number | null;
  contradicts: boolean;
}

export interface RelatedMarkets {
  conditionId: string;
  eventSlug: string | null;
  rows: RelatedMarket[];
  /**
   * All legs' YES prices summed. null when fewer than two legs are priced.
   */
  sumProbability: number | null;
  fetchedAt: string;
  note: string | null;
}

// ───────── AI analysis ───────────────────────────────────────────────────

/**
 * The model's directional read, RELATIVE TO THE QUOTED PRICE.
 *
 * Four words, and deliberately not a number. A language model will produce
 * "68%" for any question you put to it, with no calibration behind it and a
 * fluency that reads like authority — it would be the least trustworthy
 * figure in this app and would look like the most trustworthy. So the model
 * is not asked for one and the response schema gives it nowhere to put one.
 *
 * `no_view` is a real answer and is rendered as one, not as a failure.
 */
export type AiLean = 'cheap' | 'rich' | 'fair' | 'no_view';

export type AiConfidence = 'low' | 'medium' | 'high';

/** The renderer names one of these and a model id — never a host or URL.
 *  `ollama` and `lmstudio` run on this machine and take no key. */
export type AiProvider =
  | 'anthropic' | 'openai' | 'openrouter' | 'gemini' | 'ollama' | 'lmstudio';

export const AI_PROVIDERS: readonly AiProvider[] = [
  'anthropic', 'openai', 'openrouter', 'gemini', 'ollama', 'lmstudio',
];
/** Providers that take an API key. Ollama and LM Studio run on this machine. */
export const AI_KEYED_PROVIDERS: readonly AiProvider[] = ['anthropic', 'openai', 'openrouter', 'gemini'];
export const AI_PROVIDER_LABEL: Record<AiProvider, string> = {
  anthropic: 'Anthropic', openai: 'OpenAI', openrouter: 'OpenRouter', gemini: 'Google Gemini',
  ollama: 'Ollama (local)', lmstudio: 'LM Studio (local)',
};

/** A page the server-side web search actually read, so a claim can be checked. */
export interface AiSource {
  url: string;
  title: string | null;
}

/**
 * One analysis. Success and failure share this shape exactly, so the panel
 * renders one contract rather than making every field an optional chain.
 */
export interface AiAnalysis {
  ok: boolean;
  provider: AiProvider | string;
  /** The model that actually answered, as reported by the provider. */
  model: string;
  askedAt: string;
  latencyMs: number | null;
  webSearch: boolean;
  sources: AiSource[];

  /** What the market turns on. null when nothing came back. */
  summary: string | null;
  /** Traps in the resolution wording. Empty is a real answer: clean rules. */
  resolutionNotes: string[];
  /** Dated events between now and close that would move it. */
  drivers: string[];
  /**
   * What the model does NOT know. Rendered verbatim and prominently — on a
   * panel like this it is the most useful list on screen.
   */
  unknowns: string[];
  /** The argument behind the lean, one step per item. */
  reasoning: string[];
  lean: AiLean | null;
  confidence: AiConfidence | null;

  /**
   * Sentences removed because the model priced the market itself despite
   * being told not to. Shown to the user rather than hidden: a silent
   * redaction is its own small dishonesty, and a model that keeps reaching
   * for a number is worth knowing about.
   */
  redactions: number;

  /** Why there is no analysis, and what would fix it. Both null when ok. */
  reason: string | null;
  fix: string | null;
}

export interface AiKeyState {
  present: boolean;
  /** A shape the user can recognise. The key itself never reaches the UI. */
  masked: string | null;
  /** false for local providers (Ollama, LM Studio). */
  needsKey?: boolean;
}

/** What a provider can do, from the backend's capability table. Rendered so
 *  Settings can say "no web search on a local model" instead of offering a
 *  switch that would quietly do nothing. Every field optional: an older
 *  backend sends none of this. */
export interface AiProviderCaps {
  label?: string;
  local?: boolean;
  needsKey?: boolean;
  webSearch?: boolean;
  webSearchHow?: string;
  /** 'all' = every model can call tools; 'per-model' = Test connection says. */
  tools?: 'all' | 'per-model';
  toolsHow?: string;
  tokens?: string;
  pricing?: 'published' | 'provider' | 'none' | 'free';
}

/** "Test connection". Free endpoints only — a model listing, never a billed
 *  completion. `tools` is whether the selected model can drive Autopilot,
 *  when the provider could say. */
export interface AiProviderCheck {
  ok: boolean;
  provider: AiProvider;
  message: string;
  model?: string;
  models?: string[];
  tools?: boolean | null;
  webSearch?: boolean;
}

export interface AiStatus {
  enabled: boolean;
  provider: AiProvider;
  /** The selected model (config `aiModel`). Older backends kept one per
   *  provider in `models` instead. */
  model?: string;
  webSearch: boolean;
  /** Offline mode reaches the providers too, and the panel says so. */
  offline: boolean;
  /** Per provider: the chosen model (older backend) or suggestions. */
  models: Partial<Record<AiProvider, string | string[]>>;
  keys: Partial<Record<AiProvider, AiKeyState>>;
  providers?: AiProvider[];
  capabilities?: Partial<Record<AiProvider, AiProviderCaps>>;
  /** Whether an analysis could be attempted on the selected provider. */
  hasKey?: boolean;
  /** The web-search setting as stored (webSearch = setting AND capable). */
  webSearchSetting?: boolean;
  /** USD per million tokens, only where a price is published. */
  prices?: Record<string, { inPerMTok: number; outPerMTok: number }>;
}

/** How the lean reads on screen. Never a number, by design. */
export const AI_LEAN_LABEL: Record<AiLean, string> = {
  cheap: 'Looks cheap',
  rich: 'Looks rich',
  fair: 'Looks about right',
  no_view: 'No view',
};

// ───────── paper trading ─────────────────────────────────────────────────

/**
 * A simulated order. Its own type, its own table, its own totals — a paper
 * number is never added to a real one.
 *
 * The simulation is deliberately PESSIMISTIC where it cannot be exact: fills
 * come out of the real book at the walked average (never your limit), and a
 * resting order fills only when the market trades STRICTLY through its price,
 * because we cannot know queue position and inventing fills is the classic
 * paper-trading lie.
 */
export interface PaperOrder {
  localId: number;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  /** GTC/FAK/FOK, or SETTLE for the synthetic payout row. */
  orderType: string;
  requestedSize: number;
  requestedPriceCents: number;
  filledSize: number;
  /** The average actually walked out of the book. null when nothing filled. */
  avgFillPriceCents: number | null;
  restingSize: number;
  feesUsd: number | null;
  costUsd: number | null;
  status: 'filled' | 'partial' | 'resting' | 'unfilled' | 'canceled' | 'settled' | string;
  /** Why it did what it did, in words. Shown verbatim. */
  note: string | null;
  createdAt: string;
  filledAt: string | null;
}

/** The paper account. Same shape as the real portfolio, plus the bankroll. */
export interface PaperPortfolio extends TerminalPortfolio {
  bankrollUsd: number;
  /** cash + position value. The number that answers "did this work?". */
  equityUsd: number | null;
  returnPct: number | null;
}

export interface PaperOrderResult {
  ok: boolean;
  localId: number | null;
  status: string | null;
  filledSize: number | null;
  avgPriceCents: number | null;
  message: string;
  warnings?: string[];
}

// ───────── watchlist ─────────────────────────────────────────────────────

export interface WatchEntry {
  conditionId: string;
  title: string | null;
  addedAt: string;
  note: string | null;
}

// ───────── provider health (cost discipline, made visible) ───────────────

export interface ProviderHealth {
  host: string;
  purpose: string;
  calls: number;
  errors: number;
  /** Median round trip, ms. null before the first response. */
  latencyMs: number | null;
  /** Set while the host is parked after a 429. ISO; null when live. */
  parkedUntil: string | null;
  cacheHits: number;
  cacheMisses: number;
}

export interface TerminalHealth {
  providers: ProviderHealth[];
  /** The user's kill switch. When true nothing was requested from anyone. */
  offline: boolean;
  fetchedAt: string;
}

// ───────── formatting helpers (the em-dash rule, in one place) ───────────

/** The one true renderer for a possibly-unknown number. */
export function dash(v: number | null | undefined, fmt?: (n: number) => string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return fmt ? fmt(v) : String(v);
}

export function fmtCents(v: number | null | undefined): string {
  return dash(v, (n) => `${n.toFixed(1)}¢`);
}

export function fmtProb(v: number | null | undefined): string {
  return dash(v, (n) => `${n.toFixed(1)}%`);
}

export function fmtUsdOrDash(v: number | null | undefined): string {
  return dash(v, (n) => {
    const a = Math.abs(n);
    const s = a >= 1_000_000 ? `${(a / 1_000_000).toFixed(2)}M`
      : a >= 1_000 ? `${(a / 1_000).toFixed(1)}K`
        : a.toFixed(2);
    return `${n < 0 ? '-' : ''}$${s}`;
  });
}

/** Money the user COMMITS to — always two decimals, never K-rounded. A ticket
 *  cost or a ledger figure abbreviated to "$1.2K" is not the number they
 *  agreed to; `fmtUsdOrDash` stays for large summary figures. */
export function fmtUsdExact(v: number | null | undefined): string {
  return dash(v, (n) => `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`);
}

export function fmtSignedUsd(v: number | null | undefined): string {
  return dash(v, (n) => `${n < 0 ? '-' : '+'}$${Math.abs(n).toFixed(2)}`);
}

export function fmtHours(v: number | null | undefined): string {
  return dash(v, (n) => {
    if (n < 0) return `${(-n).toFixed(0)}h ago`;
    if (n < 1) return `${Math.round(n * 60)}m`;
    if (n < 48) return `${n.toFixed(1)}h`;
    return `${(n / 24).toFixed(0)}d`;
  });
}

// ───────── AI agents (MCP), forecasts, paper, health, Autopilot ──────────
//
// For RPCs polybot already had (mcpStatus, mcpActivity, aiScoreboard) the
// python is the truth for these shapes: mcp_server.status / agents_status,
// mcp_rails.activity / pending, forecast_ledger.scoreboard. Markets are a
// `conditionId` (the market's slug) plus a side; sizes are float shares.

export type McpTradeMode = 'off' | 'paper' | 'live';
export type AccountMode = 'paper' | 'live';

export interface McpRails {
  /** 0 = no cap. */
  maxOrderUsd: number;
  dailySpendUsd: number;
  maxOpenPositions: number;
  minEdgeCents: number;
  dailyLossUsd: number;
  forecastTtlMin: number;
}

/** One named agent as mcp_server.agents_status reports it. Never a token. */
export interface McpAgentStatus {
  id: string;
  name: string;
  emoji: string;
  color: string;
  enabled: boolean;
  /** What the user set on the card. */
  configuredMode: 'paper' | 'live';
  /** What it actually trades in now (global mode and account mode applied). */
  mode: McpTradeMode;
  rails: McpRails;
  rulesInWords: string[];
  /** The server name its client configs file it under. */
  serverName: string;
  /** null = could not be read. */
  spentTodayUsd: number | null;
  hasToken: boolean;
  tokenMasked: string | null;
  /** Live MCP sessions authenticated as this agent. */
  sessions: number;
}

/** One agent order attempt (python mcp_rails.activity). */
export interface McpOrderRow {
  id: number;
  at: string;
  mode: 'paper' | 'live';
  client: string | null;
  agentId: string | null;
  /** null when that agent has since been deleted. */
  agentName: string | null;
  conditionId: string;
  title: string | null;
  side: 'yes' | 'no';
  action: 'buy' | 'sell';
  orderType?: string | null;
  size: number;
  priceCents: number;
  forecastId: number | null;
  committedUsd: number | null;
  ok: boolean;
  orderId: string | null;
  filled: number | null;
  avgFillCents: number | null;
  message: string;
  status: 'pending' | 'deciding' | 'approved' | 'rejected' | 'expired' | 'failed'
    | 'unknown' | 'cancelled' | string | null;
}

export interface McpActivity {
  orders: McpOrderRow[];
  pending: McpOrderRow[];
}

/** Today's agent loss for the stop; absent on an older backend. */
export interface McpLossToday {
  realizedUsd?: number;
  unrealizedUsd?: number;
  lossUsd: number;
  unmarkedCostUsd?: number;
  positionsUnreadable?: boolean;
}

/** One agent tool call (or a client connecting), as the backend built it.
 *  Every string passed a shape and secret check there; free text an AI wrote
 *  is never included. */
export interface McpToolCallEvent {
  v: 1;
  kind: 'call' | 'connect';
  /** Epoch ms. */
  at: number;
  client: string;
  transport: string | null;
  model: string | null;
  tool: string | null;
  outcome: 'ok' | 'refused' | 'error';
  reason: string | null;
  durationMs: number;
  summary: string;
  /** The market, shape-checked. */
  conditionId?: string | null;
  /** Short label for the market (kryptrader's slot). */
  ticker?: string | null;
  fairCents: number | null;
  edgeCents: number | null;
  midCents?: number | null;
  side: 'yes' | 'no' | null;
  mode: 'paper' | 'live' | null;
  suppressed?: number;
  /** The named agent the caller's TOKEN resolved to (never what it claims). */
  agentId?: string | null;
  agentName?: string | null;
}

/** mcp:order — a live order queued / sent / refused, or an agent action. */
export interface McpOrderEvent {
  mode: McpTradeMode | 'action';
  message: string;
  approvalId?: number | null;
  /** Live outcomes (mcp_rails): pending | sent | filled | partial | unmatched |
   *  refused (the venue placed nothing) | unknown | blocked (a rail said no,
   *  nothing sent). Absent on older pushes and on paper ones. */
  status?: string;
  kind?: string;
  ok?: boolean;
  agentId?: string | null;
  agentName?: string | null;
  title?: string | null;
  conditionId?: string | null;
  side?: string | null;
  action?: string | null;
  orderType?: string | null;
  size?: number | null;
  /** Cents. */
  price?: number | null;
  orderId?: string | null;
  filled?: number | null;
  reason?: string | null;
}

/** Agent clients seen this session (backend memory only — no network). */
export interface McpSeen {
  /** `at` = last seen (epoch ms); `called` = it called a tool rather than
   *  only connecting. firstAt/lastAt/calls/model ride along (kryptrader's). */
  clients: {
    client: string; agentId?: string | null; at?: number | null; called?: boolean;
    firstAt?: number; lastAt?: number; calls?: number; model?: string | null;
  }[];
  agents?: { agentId: string; firstAt: number; lastAt: number; calls: number; clients: string[] }[];
  now?: number;
}

// ── Forecast scoreboard (python forecast_ledger.scoreboard) ─────────────────

/** `too-few` under the minimum sample; `indistinguishable` inside two
 *  standard errors. The scoreboard does not crown a winner it cannot back. */
export type ForecastVerdict = 'too-few' | 'indistinguishable' | 'agent-better' | 'market-better';

export interface ForecastScore {
  n: number;
  nPaired: number;
  /** Brier score, lower is better. null when there is nothing to score. */
  brierAgent: number | null;
  /** The agent's Brier on the paired subset only — the like-for-like number. */
  brierAgentPaired: number | null;
  brierMarket: number | null;
  /** 1 − agent/market on the paired subset. >0 = the agent's error was smaller. */
  skill: number | null;
  diffMean: number | null;
  diffSe: number | null;
  verdict: ForecastVerdict;
}

export interface ForecastRow {
  id: number;
  createdAt: string | null;
  conditionId: string;
  title: string | null;
  model: string | null;
  client: string | null;
  agentId: string | null;
  fairValueCents: number | null;
  marketMidCents: number | null;
  /** YES payout 0..1; null until the market resolves. */
  outcome: number | null;
  resolvedAt: string | null;
  brierAgent: number | null;
  brierMarket: number | null;
}

export interface AgentScore extends ForecastScore {
  agentId: string;
  total: number;
  pending: number;
  lastAt: string | null;
  models?: string[];
  clients?: string[];
}

/** Kept for the Agent Hub's personas; polybot's ledger groups by agent. */
export interface ForecasterScore extends ForecastScore {
  client: string | null;
  model: string | null;
  agentId?: string | null;
  total: number;
  pending: number;
  lastAt: string | null;
  open: { conditionId: string; fairValueCents: number; marketMidCents: number | null; createdAt: string | null }[];
}

export interface ForecastScoreboard {
  totalForecasts: number;
  pending: number;
  resolved: number;
  scoredMarkets: number;
  minScored: number;
  overall: ForecastScore;
  buckets: { lo: number; hi: number; n: number; meanForecast: number; hitRate: number }[];
  byAgent?: AgentScore[];
  byForecaster?: ForecasterScore[];
  recent: ForecastRow[];
}

// ── Connection health ───────────────────────────────────────────────────────

export type HealthStatus = 'ok' | 'warn' | 'fail' | 'off';

/** What a row's button does. `nav` names an in-app page; `copy` carries a
 *  command the backend built from its own values — never a secret. */
export type HealthAction =
  | { kind: 'nav'; page: string; label: string }
  | { kind: 'copy'; text: string; label: string };

export interface HealthRow {
  id: string;
  label: string;
  status: HealthStatus;
  detail: string;
  fix: string | null;
  action: HealthAction | null;
  /** This check CAN touch the network, so it only does on "Run checks". */
  network: boolean;
  /** This result came from an end-to-end probe, not local state. */
  tested: boolean;
}

export interface HealthReport {
  checkedAt: string;
  deep: boolean;
  rows: HealthRow[];
}

// ── The account's paper book (terminal_paper `manual` book) ────────────────

export interface PaperStatus {
  accountMode?: AccountMode;
  bankrollUsd: number;
  cashUsd: number | null;
  equityUsd: number | null;
  realizedPnlUsd: number | null;
  unrealizedPnlUsd?: number | null;
  returnPct?: number | null;
  openPositions: number;
  restingOrders?: number;
  /** The bankroll a Reset would start from (config), when it differs from
   *  the one the current book started with. */
  nextBankrollUsd?: number | null;
  /** An API key is saved (Go live needs one). */
  hasWalletKey?: boolean;
  /** Positions with no bid to mark at. */
  unmarked?: number;
  fetchedAt?: string;
}

/** One agent's own paper account (mcpPaper {agentId}). */
export interface AgentPaper {
  portfolio: {
    cashUsd?: number | null; equityUsd?: number | null; returnPct?: number | null;
    realizedPnlUsd?: number | null; totalUnrealizedPnlUsd?: number | null; bankrollUsd?: number;
    positions?: {
      conditionId: string; title: string | null; side: string; size: number;
      avgCostCents: number | null; markCents: number | null; unrealizedPnlUsd: number | null;
    }[];
  } | null;
  orders: {
    orders?: {
      localId: number; title: string | null; side: string; action: string;
      filledSize: number; avgFillPriceCents: number | null; status: string; createdAt: string;
    }[];
  } | null;
}

// ── Autopilot ───────────────────────────────────────────────────────────────

export interface AutopilotRun {
  id: number;
  startedAt: string;
  finishedAt: string | null;
  trigger: 'schedule' | 'manual' | string;
  provider: string;
  model: string;
  status: 'running' | 'ok' | 'steps' | 'budget' | 'context' | 'stopped' | 'error' | string;
  steps: number;
  inputTokens: number | null;
  outputTokens: number | null;
  tokensEstimated?: boolean;
  costUsd: number | null;
  summary: string;
  error: string | null;
  tools: { tool: string; ok: boolean; brief: string }[];
}

export interface AutopilotStatus {
  enabled: boolean;
  running: boolean;
  startedAt?: string | null;
  step?: number;
  lastError: string | null;
  nextRunAt: string | null;
  /** Why the next run would not start, e.g. no key or budget spent. */
  blockedReason: string | null;
  today: { runs: number; tokens: number; costUsd: number | null };
  limits: { intervalMin: number; maxRunsPerDay: number; dailyTokenBudget: number; maxSteps: number };
  provider: string;
  providerLabel?: string;
  model: string;
  tokenAccounting?: string;
  toolCount?: number;
  runs: AutopilotRun[];
  agentId?: string;
  agentName?: string | null;
}
