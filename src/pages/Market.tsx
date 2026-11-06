import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft, ExternalLink, Network, RefreshCw, Scale, ShieldQuestion, Sparkles,
  Star,
} from 'lucide-react';
import type {
  AiAnalysis, AiStatus,
  BookSnapshot, CrossVenueComparison, ManualOrderPreview, ManualOrderResult,
  MarketSummary,
  OrderType, PriceSeries, RelatedMarkets, ResolutionReport, RestingOrder,
  SellPreview, SellResult, StandingKind,
  TerminalInterval, TerminalPosition,
} from '@shared/market';
import {
  AI_LEAN_LABEL, INTERVALS, MIN_RESOLVED_CHECKS, ORIGIN_LABEL, STANDING_LABEL,
} from '@shared/market';
import type { MicroSeries } from '@shared/types';
import { Card, Empty, Page } from '../components/common';
import {
  Note, OwnFeedBadge, ProbBar, ProbChart, RiskPill, SourceLegend, Src,
  Unknowns, Val, cents, centsTight, hours, signedUsd, usd, usdExact,
} from '../components/terminal/TerminalBits';
import { selectMarket, useSelectedMarket } from '../state/terminalNav';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { cls } from '../utils/format';

const POLL_MS = 4000;

export function MarketPage({ onNav }: { onNav: (p: 'discover' | 'track') => void }) {
  const conditionId = useSelectedMarket();
  const [market, setMarket] = useState<MarketSummary | null>(null);
  const [side, setSide] = useState<'yes' | 'no'>('yes');
  const [interval, setInterval_] = useState<TerminalInterval>('1d');
  const [history, setHistory] = useState<PriceSeries | null>(null);
  const [book, setBook] = useState<BookSnapshot | null>(null);
  const [micro, setMicro] = useState<MicroSeries | null>(null);
  const [risk, setRisk] = useState<ResolutionReport | null>(null);
  const [related, setRelated] = useState<RelatedMarkets | null>(null);
  // Cross-venue is opt-in: the first run indexes ~11,800 Kalshi series.
  const [crossOn, setCrossOn] = useState(false);
  const [cross, setCross] = useState<CrossVenueComparison | null>(null);
  const [crossLoading, setCrossLoading] = useState(false);
  const [riskLoading, setRiskLoading] = useState(false);
  // AI analysis is opt-in per market and never polled: each run bills the
  // user's own provider account, so a click is the only thing that starts one.
  const [ai, setAi] = useState<AiAnalysis | null>(null);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [watched, setWatched] = useState(false);
  const [ticketPrice, setTicketPrice] = useState<number | null>(null);
  // Bumped whenever an order comes back, so the position panel re-reads what
  // you hold immediately instead of waiting out its 12 s cadence.
  const [posNonce, setPosNonce] = useState(0);

  // Poll the fast things. Deliberately does NOT include the resolution report
  // (11 checks, several of them extra fetches) — that is opt-in below, with
  // its cost stated on the button.
  useEffect(() => {
    if (!conditionId) return;
    let alive = true;
    let busy = false;
    async function tick() {
      if (busy || !conditionId) return;
      busy = true;
      try {
        const [m, b, mi] = await Promise.all([
          window.krypt.terminal.market(conditionId).catch(() => null),
          window.krypt.terminal.book(conditionId, side).catch(() => null),
          window.krypt.terminal.micro(conditionId, side).catch(() => null),
        ]);
        if (!alive) return;
        if (m) setMarket(m.market);
        if (b) setBook(b);
        if (mi) setMicro(mi);
      } finally { busy = false; }
    }
    void tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => { alive = false; window.clearInterval(id); };
  }, [conditionId, side]);

  // History moves slower than the book; poll it on its own, longer cycle.
  useEffect(() => {
    if (!conditionId) return;
    let alive = true;
    async function load() {
      const h = await window.krypt.terminal
        .history(conditionId!, side, interval).catch(() => null);
      if (alive && h) setHistory(h);
    }
    void load();
    const id = window.setInterval(load, 30_000);
    return () => { alive = false; window.clearInterval(id); };
  }, [conditionId, side, interval]);

  // Siblings move slowly and cost an extra event fetch, so they get their own
  // long cycle rather than riding the 4s book poll.
  useEffect(() => {
    if (!conditionId) return;
    let alive = true;
    async function load() {
      const r = await window.krypt.terminal.related(conditionId!).catch(() => null);
      if (alive && r) setRelated(r);
    }
    void load();
    const id = window.setInterval(load, 60_000);
    return () => { alive = false; window.clearInterval(id); };
  }, [conditionId]);

  useEffect(() => {
    if (!conditionId) return;
    let alive = true;
    window.krypt.terminal.watchlist({ action: 'list' }).then((r) => {
      if (alive) setWatched((r.entries ?? []).some((e) => e.conditionId === conditionId));
    }).catch(() => {});
    // Opening a different market resets the panels that belong to the old one,
    // so a stale book can never be read as this market's.
    setRisk(null);
    setHistory(null);
    setBook(null);
    setRelated(null);
    // A new market invalidates the pairing entirely — showing the previous
    // market's Kalshi counterpart would be the worst possible stale state.
    setCross(null);
    setCrossOn(false);
    return () => { alive = false; };
  }, [conditionId]);

  const loadCross = useCallback(async () => {
    if (!conditionId) return;
    setCrossOn(true);
    setCrossLoading(true);
    try {
      setCross(await window.krypt.terminal.crossVenue(conditionId, interval));
    } finally { setCrossLoading(false); }
  }, [conditionId, interval]);

  // Keep the overlay honest when the interval changes: the buckets differ per
  // interval, so a series fetched for 1D must not be drawn on a 1W axis.
  useEffect(() => {
    if (!crossOn || !conditionId) return;
    let alive = true;
    setCrossLoading(true);
    window.krypt.terminal.crossVenue(conditionId, interval)
      .then((c) => { if (alive) setCross(c); })
      .catch(() => {})
      .finally(() => { if (alive) setCrossLoading(false); });
    return () => { alive = false; };
  }, [interval, crossOn, conditionId]);

  const loadRisk = useCallback(async () => {
    if (!conditionId) return;
    setRiskLoading(true);
    try {
      setRisk(await window.krypt.terminal.resolution(conditionId));
    } finally { setRiskLoading(false); }
  }, [conditionId]);

  // The panel needs to know whether the feature is on and keyed BEFORE the
  // user clicks, so the button can explain itself instead of failing.
  useEffect(() => {
    let alive = true;
    window.krypt.ai.status()
      .then((v) => { if (alive) setAiStatus(v); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  // A new market invalidates the previous analysis. Showing one market's
  // reading under another market's title is the worst thing this panel could
  // do, so it is cleared rather than left stale.
  useEffect(() => { setAi(null); }, [conditionId]);

  const loadAi = useCallback(async () => {
    if (!conditionId) return;
    setAiLoading(true);
    try {
      setAi(await window.krypt.ai.analyze(conditionId));
    } finally { setAiLoading(false); }
  }, [conditionId]);

  const toggleWatch = useCallback(async () => {
    if (!conditionId) return;
    await window.krypt.terminal.watchlist({
      action: watched ? 'remove' : 'add',
      conditionId, title: market?.title,
    });
    setWatched((v) => !v);
  }, [conditionId, watched, market]);

  const openOnPolymarket = useCallback(async () => {
    if (!market) return;
    const r = await window.krypt.polymarket.marketUrl({
      eventTicker: market.eventSlug ?? '', ticker: market.conditionId,
    });
    if (r.url) void window.krypt.app.openExternal(r.url);
  }, [market]);

  if (!conditionId) {
    return (
      <Page title="Market" subtitle="Open a market from Discover.">
        <Empty
          title="No market selected"
          description="Pick one on the Discover screen, or paste a market slug into its search box."
          action={
            <button
              onClick={() => onNav('discover')}
              className="rounded-lg border border-krypt-purple/50 bg-krypt-purple/10 px-4 py-2 text-sm text-white"
            >
              Go to Discover
            </button>
          }
        />
      </Page>
    );
  }

  const displayProb = market
    ? (side === 'yes'
      ? market.probability
      : (market.probability === null ? null : Math.round((100 - market.probability) * 10) / 10))
    : null;

  const sideLabel = side === 'yes'
    ? (market?.outcomes?.[0] ?? 'YES')
    : (market?.outcomes?.[1] ?? 'NO');

  // MarketSummary.bid/ask are the YES token's quotes (shared/market.ts:96-98).
  // The NO token is that book mirrored: buying NO at p is selling YES at 100−p,
  // so NO bid = 100 − YES ask, NO ask = 100 − YES bid, and the spread is the
  // same width either way. The stamp travels with the quote it was derived
  // from. Printing YES numbers under a NO headline is a lie the user cannot
  // see, so the strip is labelled with the side as well.
  const mirror = (v: number | null) => (v === null ? null : Math.round((100 - v) * 10) / 10);
  const sideBid = market ? (side === 'yes' ? market.bid : mirror(market.ask)) : null;
  const sideAsk = market ? (side === 'yes' ? market.ask : mirror(market.bid)) : null;
  const bidStamp = market ? (side === 'yes' ? market.sources.bid : market.sources.ask) : undefined;
  const askStamp = market ? (side === 'yes' ? market.sources.ask : market.sources.bid) : undefined;

  return (
    <Page
      title={market?.title ?? 'Loading…'}
      subtitle={market
        ? `${market.category ?? 'uncategorised'} · closes ${market.closeTime ?? '—'} · ${market.conditionId.slice(0, 10)}…`
        : conditionId}
      actions={
        <>
          <button
            onClick={() => { selectMarket(null); onNav('discover'); }}
            className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-sm text-krypt-muted hover:text-white"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> Discover
          </button>
          <button
            onClick={toggleWatch}
            className={cls('flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm',
              watched
                ? 'border-krypt-warn/50 bg-krypt-warn/10 text-krypt-warn'
                : 'border-krypt-border text-krypt-muted hover:text-white')}
          >
            <Star className={cls('h-3.5 w-3.5', watched && 'fill-current')} />
            {watched ? 'Watching' : 'Watch'}
          </button>
          <button
            onClick={openOnPolymarket}
            className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-sm text-krypt-muted hover:text-white"
          >
            <ExternalLink className="h-3.5 w-3.5" /> Polymarket US
          </button>
        </>
      }
    >
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-4">
          {/* ── price + chart ─────────────────────────────────────── */}
          <Card>
            <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
              <div>
                <div className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-krypt-muted">
                  <button
                    onClick={() => setSide('yes')}
                    className={cls('rounded px-2 py-0.5',
                      side === 'yes' ? 'bg-krypt-win/15 text-krypt-win' : 'text-krypt-dim hover:text-white')}
                  >
                    {market?.outcomes?.[0] ?? 'YES'}
                  </button>
                  <button
                    onClick={() => setSide('no')}
                    className={cls('rounded px-2 py-0.5',
                      side === 'no' ? 'bg-krypt-loss/15 text-krypt-loss' : 'text-krypt-dim hover:text-white')}
                  >
                    {market?.outcomes?.[1] ?? 'NO'}
                  </button>
                </div>
                <div className="mt-1 flex items-baseline gap-2">
                  <Src stamp={market?.sources.probability} />
                  <span className="font-mono text-4xl text-white">
                    <Val v={displayProb} fmt={cents} />
                  </span>
                  {history?.ownFeed && <OwnFeedBadge note={history.note} />}
                </div>
                <div className="mt-2 w-56"><ProbBar v={displayProb} /></div>
              </div>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => (crossOn ? setCrossOn(false) : void loadCross())}
                  title="Overlay the same question priced on Kalshi"
                  className={cls('mr-2 flex items-center gap-1 rounded px-2 py-1 text-xs',
                    crossOn && cross?.kalshi
                      ? 'bg-krypt-warn/15 text-krypt-warn'
                      : 'text-krypt-dim hover:text-white')}
                >
                  <Scale className="h-3 w-3" />
                  Kalshi
                </button>
                {INTERVALS.map((iv) => (
                  <button
                    key={iv.id}
                    onClick={() => setInterval_(iv.id)}
                    className={cls('rounded px-2 py-1 text-xs',
                      interval === iv.id
                        ? 'bg-krypt-purple/20 text-white'
                        : 'text-krypt-dim hover:text-white')}
                  >
                    {iv.label}
                  </button>
                ))}
              </div>
            </div>

            {history ? (
              <>
                <ProbChart
                  points={history.points}
                  overlay={cross?.kalshi && cross.series.length
                    ? { label: `Kalshi · ${cross.kalshi.ticker}`, data: cross.series }
                    : null}
                  markers={ticketPrice !== null
                    // Drawn in the series' OWN unit (cents on a cents axis).
                    // A limit line converted from the wrong unit sits nowhere
                    // near where it belongs and looks completely convincing.
                    ? [{ cents: ticketPrice, label: 'your limit', tone: 'neutral' }]
                    : []}
                />
                {(history.note || history.thin) && (
                  <div className="mt-2">
                    <Note tone={history.thin ? 'warn' : 'info'}>
                      {history.note ?? `Only ${history.points.length} points — thin, but real.`}
                    </Note>
                  </div>
                )}
              </>
            ) : (
              <div className="grid h-[220px] place-items-center text-sm text-krypt-dim">Loading chart…</div>
            )}
          </Card>

          {/* ── stats strip ───────────────────────────────────────── */}
          {market && (
            <Card>
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
                <Stat label={`Bid · ${sideLabel}`} stamp={bidStamp} v={sideBid} fmt={cents} />
                <Stat label={`Ask · ${sideLabel}`} stamp={askStamp} v={sideAsk} fmt={cents} />
                <Stat label="Spread" stamp={market.sources.spread} v={market.spread} fmt={cents} />
                <Stat label="24h volume" stamp={market.sources.volume24h} v={market.volume24hUsd} fmt={usd} />
                <Stat label="Liquidity" stamp={market.sources.liquidity} v={market.liquidityUsd} fmt={usd} />
                <Stat label="Closes in" stamp={market.sources.closeTime} v={market.hoursToClose} fmt={hours} />
              </div>
              <div className="mt-4 space-y-3">
                <SourceLegend sources={market.sources} />
                <Unknowns items={market.unknowns} />
              </div>
            </Card>
          )}

          {/* ── our own microstructure ────────────────────────────── */}
          <MicroPanel micro={micro} />

          {/* ── cross-venue ───────────────────────────────────────── */}
          <CrossVenuePanel
            cross={cross}
            loading={crossLoading}
            enabled={crossOn}
            onRun={loadCross}
          />

          {/* ── related markets ───────────────────────────────────── */}
          <RelatedPanel
            related={related}
            onOpen={(cid) => { selectMarket(cid); }}
          />

          {/* ── resolution risk ───────────────────────────────────── */}
          <ResolutionPanel report={risk} loading={riskLoading} onRun={loadRisk} />

          {/* ── AI analysis ───────────────────────────────────────── */}
          <AiPanel
            analysis={ai}
            status={aiStatus}
            loading={aiLoading}
            onRun={loadAi}
          />

        </div>

        {/* ── right rail: book + ticket ─────────────────────────────── */}
        <div className="space-y-4">
          <BookPanel book={book} side={side} onPick={setTicketPrice} />
          <PublicQuoteNote />
          <Ticket
            market={market}
            side={side}
            onPriceChange={setTicketPrice}
            onNavTrack={() => onNav('track')}
            onOrderResult={() => setPosNonce((n) => n + 1)}
          />
          {/* Directly under the ticket: what you hold here, and the one
              click that actually gets you out of it. */}
          <PositionPanel
            market={market}
            refreshKey={posNonce}
            onNavTrack={() => onNav('track')}
          />
        </div>
      </div>
    </Page>
  );
}

function Stat({
  label, v, fmt, stamp,
}: {
  label: string; v: number | null;
  fmt: (n: number) => string;
  stamp?: MarketSummary['sources'][keyof MarketSummary['sources']];
}) {
  return (
    <div>
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-krypt-muted">
        <Src stamp={stamp} />{label}
      </div>
      <div className="mt-0.5 font-mono text-lg text-white"><Val v={v} fmt={fmt} /></div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Order book
// ────────────────────────────────────────────────────────────────────────

/** Without an API key the book comes from the exchange's public quotes, which
 *  it caches for up to 30 seconds. Paper fills are walked out of the same book. */
function PublicQuoteNote() {
  const { credentials } = useApp();
  if (credentials === null || credentials?.hasWalletKey) return null;
  return (
    <p className="text-[11px] text-krypt-warn" data-testid="public-quote-note">
      No API key saved: this book comes from public quotes, which the exchange caches for up to
      30 seconds. Add a key for the live feed.
    </p>
  );
}

// ────────────────────────────────────────────────────────────────────────

function BookPanel({
  book, side, onPick,
}: { book: BookSnapshot | null; side: 'yes' | 'no'; onPick: (c: number) => void }) {
  const maxCum = useMemo(() => {
    if (!book) return 0;
    return Math.max(
      book.bids.at(-1)?.cumulative ?? 0,
      book.asks.at(-1)?.cumulative ?? 0, 1);
  }, [book]);

  return (
    <Card header={
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-medium text-white">
          Order book · {side.toUpperCase()}
        </span>
        <span className="font-mono text-xs text-krypt-dim">
          <Val v={book?.spreadCents ?? null} fmt={(n) => `${n.toFixed(1)}¢ wide`} />
        </span>
      </div>
    }>
      {!book ? (
        <div className="py-6 text-center text-sm text-krypt-dim">Loading…</div>
      ) : !book.available ? (
        // "Could not read" and "genuinely empty" are different facts about the
        // market, and a terminal that renders them identically is lying about
        // one of them.
        <Note tone="warn">{book.note ?? 'The book could not be read.'}</Note>
      ) : (
        <div className="space-y-2">
          <Ladder
            levels={[...book.asks].reverse().slice(-8)}
            tone="loss" maxCum={maxCum} onPick={onPick} label="Asks (what you would pay)"
          />
          <div className="flex items-center justify-between border-y border-krypt-border py-1.5 font-mono text-xs">
            <span className="text-krypt-dim">mid</span>
            <span className="text-white"><Val v={book.midCents} fmt={cents} /></span>
          </div>
          <Ladder
            levels={book.bids.slice(0, 8)}
            tone="win" maxCum={maxCum} onPick={onPick} label="Bids (what you would receive)"
          />
          <div className="flex justify-between pt-1 text-[11px] text-krypt-dim">
            <span>bid depth <Val v={book.bidDepthUsd} fmt={usd} /></span>
            <span>ask depth <Val v={book.askDepthUsd} fmt={usd} /></span>
          </div>
          {book.note && <Note>{book.note}</Note>}
        </div>
      )}
    </Card>
  );
}

function Ladder({
  levels, tone, maxCum, onPick, label,
}: {
  levels: BookSnapshot['bids'];
  tone: 'win' | 'loss';
  maxCum: number;
  onPick: (c: number) => void;
  label: string;
}) {
  if (!levels.length) {
    return <div className="py-2 text-center text-[11px] text-krypt-dim">no {tone === 'win' ? 'bids' : 'asks'}</div>;
  }
  return (
    <div>
      <div className="mb-1 text-[10px] uppercase tracking-wider text-krypt-dim">{label}</div>
      {levels.map((l) => (
        <button
          key={l.priceCents}
          onClick={() => onPick(l.priceCents)}
          title="Click to load this price into the ticket"
          className="relative flex w-full items-center justify-between px-1 py-0.5 font-mono text-xs hover:bg-white/[0.04]"
        >
          <span
            className={cls('absolute inset-y-0 right-0 opacity-[0.14]',
              tone === 'win' ? 'bg-krypt-win' : 'bg-krypt-loss')}
            style={{ width: `${Math.min(100, (l.cumulative / maxCum) * 100)}%` }}
          />
          <span className={cls('relative', tone === 'win' ? 'text-krypt-win' : 'text-krypt-loss')}>
            {l.priceCents.toFixed(1)}¢
          </span>
          <span className="relative text-krypt-muted">{l.size.toLocaleString()}</span>
          <span className="relative w-16 text-right text-krypt-dim">{usd(l.notionalUsd)}</span>
        </button>
      ))}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Our own feed
// ────────────────────────────────────────────────────────────────────────

function MicroPanel({ micro }: { micro: MicroSeries | null }) {
  return (
    <Card header={
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-sm font-medium text-white">
          Microstructure <OwnFeedBadge />
        </span>
        <span className="text-[11px] text-krypt-dim">
          {micro?.sampleCount ?? 0} samples
        </span>
      </div>
    }>
      <p className="mb-3 text-xs text-krypt-muted">
        Real spread over time, measured from quotes this machine received
        directly and timestamped on arrival — no indexer lag, and no hosted
        competitor using the same public REST can show it. Recording starts
        when you open this market and stops when you leave.
      </p>
      <div className="flex items-baseline gap-6">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-krypt-muted">Median spread</div>
          <div className="font-mono text-2xl text-white">
            <Val v={micro?.medianSpreadCents ?? null} fmt={cents} />
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-krypt-muted">Latest</div>
          <div className="font-mono text-2xl text-white">
            <Val v={micro?.points.at(-1)?.spread ?? null} fmt={cents} />
          </div>
        </div>
      </div>
      {micro?.note && <div className="mt-3"><Note>{micro.note}</Note></div>}
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Resolution risk — the "security panel" of a prediction market
// ────────────────────────────────────────────────────────────────────────

function ResolutionPanel({
  report, loading, onRun,
}: { report: ResolutionReport | null; loading: boolean; onRun: () => void }) {
  return (
    <Card header={
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-sm font-medium text-white">
          <ShieldQuestion className="h-4 w-4 text-krypt-purple" /> Resolution risk
        </span>
        <button
          onClick={onRun}
          disabled={loading}
          className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-2.5 py-1 text-xs text-krypt-muted hover:text-white disabled:opacity-50"
        >
          <RefreshCw className={cls('h-3 w-3', loading && 'animate-spin')} />
          {report ? 'Re-run' : 'Run 11 checks'}
        </button>
      </div>
    }>
      {!report ? (
        <div className="space-y-2">
          <p className="text-xs text-krypt-muted">
            In crypto the danger is a rug. Here it is <strong>resolution</strong>:
            who decides, on what source, how ambiguous the wording is, and
            whether a sibling market contradicts this one.
          </p>
          {/* Expensive analysis is opt-in with its price stated, not hidden in
              a background poll. */}
          <p className="text-[11px] text-krypt-dim">
            Costs about 3 extra provider calls (the event and its siblings), so
            it runs on demand rather than on every refresh.
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="flex items-baseline gap-3">
            <div className="font-mono text-3xl text-white">
              <Val v={report.score} fmt={(n) => n.toFixed(0)}
                title={`Fewer than ${MIN_RESOLVED_CHECKS} checks resolved, so no score is shown.`} />
            </div>
            <div className="text-xs text-krypt-muted">{report.scoreNote}</div>
          </div>
          {report.resolutionSource && (
            <div className="text-xs text-krypt-muted">
              Resolves on: <span className="text-white">{report.resolutionSource}</span>
            </div>
          )}
          <div className="space-y-1.5">
            {report.checks.map((c) => (
              <div key={c.id} className={cls('flex items-start gap-2 rounded-lg px-2 py-1.5',
                !c.resolved && 'opacity-60')}
              >
                <div className="w-16 shrink-0 pt-0.5">
                  <RiskPill level={c.level}>{c.resolved ? c.level : 'n/a'}</RiskPill>
                </div>
                <div className="min-w-0">
                  <div className="text-xs text-white">{c.label}</div>
                  <div className="text-[11px] text-krypt-muted">{c.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// AI analysis — a language model reading the market, out loud
//
// The panel's job is to be USEFUL without becoming authoritative. Everything
// below is arranged around one rule: it never shows a number the model made
// up. There is no probability, no fair value and no edge, because the model
// is never asked for one and the response schema gives it nowhere to put one
// (see ai_analysis.py). What it shows instead is a direction, the argument
// behind it, and — given equal weight on screen — what the model does not
// know.
// ────────────────────────────────────────────────────────────────────────

const LEAN_STYLE: Record<string, string> = {
  cheap: 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win',
  rich: 'border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss',
  fair: 'border-krypt-border bg-krypt-surface2/60 text-krypt-muted',
  no_view: 'border-krypt-border bg-krypt-surface2/60 text-krypt-dim',
};

function AiBullets({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <div>
      <div className="mb-1 text-[11px] uppercase tracking-wide text-krypt-dim">{title}</div>
      <ul className="space-y-1">
        {items.map((t, i) => (
          <li key={i} className="flex gap-2 text-xs text-krypt-muted">
            <span className="text-krypt-dim">·</span>
            <span className="min-w-0">{t}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function AiPanel({
  analysis, status, loading, onRun,
}: {
  analysis: AiAnalysis | null;
  status: AiStatus | null;
  loading: boolean;
  onRun: () => void;
}) {
  const provider = status?.provider ?? 'anthropic';
  const caps = status?.capabilities?.[provider];
  const label = caps?.label ?? provider;
  // Ollama and LM Studio run on this computer: no key, and offline mode
  // (which stops anything leaving the machine) does not block them.
  const local = caps?.local ?? (provider === 'ollama' || provider === 'lmstudio');
  const keyed = status?.keys?.[provider]?.present ?? false;
  const needsKey = status?.keys?.[provider]?.needsKey ?? !local;
  // Each blocker gets its own sentence naming the switch that fixes it. A
  // disabled button with no explanation is the thing this codebase treats as
  // a bug everywhere else.
  const blocked = !status ? 'Checking…'
    : status.offline && !local ? 'Offline mode is on, so nothing is sent to a cloud provider.'
      : !status.enabled ? 'AI analysis is off. Turn it on in Settings → AI analysis.'
        : needsKey && !keyed ? `No ${label} API key is stored. Add one in Settings → AI analysis.`
          : null;

  return (
    <Card header={
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-sm font-medium text-white">
          <Sparkles className="h-4 w-4 text-krypt-purple" /> AI analysis
        </span>
        <button
          onClick={onRun}
          disabled={loading || !!blocked}
          title={blocked ?? undefined}
          className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-2.5 py-1 text-xs text-krypt-muted hover:text-white disabled:opacity-50"
        >
          <RefreshCw className={cls('h-3 w-3', loading && 'animate-spin')} />
          {analysis ? 'Re-run' : 'Read this market'}
        </button>
      </div>
    }>
      {!analysis ? (
        <div className="space-y-2">
          <p className="text-xs text-krypt-muted">
            Sends this market&rsquo;s <strong>question, rules text and quote</strong> to
            {' '}{provider} and asks it to read the resolution criteria
            adversarially: what the market turns on, the traps in the wording,
            what would move it, and what it does not know.
          </p>
          {/* The two things a user should know BEFORE they click, not after. */}
          <p className="text-[11px] text-krypt-dim">
            Never sends your API key, your positions or your orders. Billed to
            your own {provider} account, on demand only — nothing here polls.
          </p>
          <p className="text-[11px] text-krypt-dim">
            It will not give you a probability or a fair value. It gives a
            direction and its reasoning; a number from a language model would
            read as authority it has not earned.
          </p>
          {blocked && <Note tone="warn">{blocked}</Note>}
        </div>
      ) : !analysis.ok ? (
        <Note tone="warn">
          {analysis.reason}
          {analysis.fix && <div className="mt-1 text-krypt-muted">{analysis.fix}</div>}
        </Note>
      ) : (
        <div className="space-y-3">
          {/* The lean. A word, not a number — and the caveat sits with it
              rather than in a footnote nobody reads. */}
          <div className="flex flex-wrap items-center gap-2">
            <span className={cls(
              'rounded-lg border px-2.5 py-1 text-sm font-medium',
              LEAN_STYLE[analysis.lean ?? 'no_view'])}
            >
              {AI_LEAN_LABEL[analysis.lean ?? 'no_view']}
            </span>
            {analysis.confidence && (
              <span className="text-[11px] text-krypt-dim">
                {analysis.confidence} confidence
              </span>
            )}
            <span className="text-[11px] text-krypt-dim">· a reading, not a forecast</span>
          </div>

          {analysis.summary && (
            <p className="text-xs text-krypt-muted">{analysis.summary}</p>
          )}

          <AiBullets title="Resolution wording" items={analysis.resolutionNotes} />
          <AiBullets title="What would move it" items={analysis.drivers} />
          <AiBullets title="Why that lean" items={analysis.reasoning} />

          {/* Given the same weight as everything else on purpose. On a panel
              like this the honest list of gaps is the most useful thing here. */}
          <AiBullets title="What it does not know" items={analysis.unknowns} />

          {!analysis.webSearch && (
            <p className="text-[11px] text-krypt-dim">
              Answered from model knowledge only — no web access, so anything
              recent is outside what it can see. Turn on web search in Settings
              to ground it in current sources.
            </p>
          )}

          {analysis.sources.length > 0 && (
            <div>
              <div className="mb-1 text-[11px] uppercase tracking-wide text-krypt-dim">
                Sources it read
              </div>
              <ul className="space-y-1">
                {analysis.sources.map((src, i) => (
                  <li key={i}>
                    <button
                      onClick={() => window.krypt.app.openExternal(src.url)}
                      className="flex items-start gap-1.5 text-left text-xs text-krypt-purple hover:underline"
                    >
                      <ExternalLink className="mt-0.5 h-3 w-3 shrink-0" />
                      <span className="min-w-0 break-all">{src.title ?? src.url}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Shown, not hidden. A silent redaction is its own small
              dishonesty, and a model that keeps reaching for a number is
              worth knowing about. */}
          {analysis.redactions > 0 && (
            <Note tone="warn">
              {analysis.redactions === 1
                ? '1 sentence was removed because it stated a numeric estimate for this market.'
                : `${analysis.redactions} sentences were removed because they stated numeric estimates for this market.`}
              {' '}This panel does not show a model&rsquo;s price.
            </Note>
          )}

          <div className="border-t border-krypt-border pt-2 text-[11px] text-krypt-dim">
            {analysis.model} · {new Date(analysis.askedAt).toLocaleTimeString()}
            {analysis.latencyMs != null && ` · ${(analysis.latencyMs / 1000).toFixed(1)}s`}
            {analysis.webSearch && ' · web search on'}
          </div>
        </div>
      )}
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// The ticket — the only place in the terminal that spends money
// ────────────────────────────────────────────────────────────────────────

// Marketable FIRST, and the default. A ticket whose default RESTS is a ticket
// that quietly does nothing: the first live test placed a limit buy 1¢ under
// the ask, read "Order sent" as "bought", and tried to sell a position that
// never existed. Crossing now is what a person means by "buy this"; resting is
// a deliberate choice and stays exactly one click away.
const ORDER_TYPES: { id: OrderType; label: string; hint: string }[] = [
  { id: 'FAK', label: 'Marketable', hint: 'Crosses now at your price or better; the remainder is killed, never rested.' },
  { id: 'GTC', label: 'Limit', hint: 'Rests on the book until filled or cancelled. Fills as maker pay no fee — but nothing fills until someone takes your price.' },
  { id: 'FOK', label: 'All or none', hint: 'Fills the whole size immediately or is killed entirely.' },
];
const DEFAULT_ORDER_TYPE: OrderType = 'FAK';

function Ticket({
  market, side, onPriceChange, onNavTrack, onOrderResult,
}: {
  market: MarketSummary | null;
  side: 'yes' | 'no';
  onPriceChange: (c: number | null) => void;
  onNavTrack: () => void;
  /** Any order came back — the position panel below is now stale. */
  onOrderResult: () => void;
}) {
  const { config, account } = useApp();
  const toast = useToast();
  const [action, setAction] = useState<'buy' | 'sell'>('buy');
  const [price, setPrice] = useState('');
  const [size, setSize] = useState('');
  const [orderType, setOrderType] = useState<OrderType>(DEFAULT_ORDER_TYPE);
  const [preview, setPreview] = useState<ManualOrderPreview | null>(null);
  // The preview the user actually READ when they pressed Review. The live
  // preview keeps re-quoting behind the panel; the confirmation is a contract
  // about one specific number, so it gets frozen here.
  const [confirmedPreview, setConfirmedPreview] = useState<ManualOrderPreview | null>(null);
  const [priceMoved, setPriceMoved] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<ManualOrderResult | null>(null);

  const paper = !!config?.terminalPaperMode;
  const priceNum = price === '' ? null : Number(price);
  const sizeNum = size === '' ? null : Number(size);

  // What this account can actually spend, shown BEFORE the order is reviewed.
  // The preview is authoritative (it is the same number the backend blocks
  // on); the account snapshot fills the gap before a size has been typed and
  // there is no preview yet. Both read the same cached account balance, so
  // they cannot disagree. `null` renders as "unknown", never as $0.00 — the
  // header's own $0.00-when-down bug is the one being avoided here.
  const availableUsd =
    preview?.availableUsd
    ?? (paper || !account || account.backendDown ? null : account.cashUsd);

  useEffect(() => { onPriceChange(priceNum); }, [priceNum, onPriceChange]);

  // Seed the price from the live touch the first time we have one, so the
  // ticket opens at a tradable number rather than at zero. Falls back to the
  // mid when the side we want has no quote, and CLAMPS to 1..99 — the venue
  // rejects anything outside that, so seeding a 100 would open the ticket
  // permanently blocked.
  //
  // WHICH touch depends on the action, and this is the whole difference
  // between an exit and an offer. A BUY crosses the ASK; a SELL crosses the
  // BID. Seeding a sell from the ask (or from the mid) opens the ticket at a
  // price that RESTS — which is exactly what "I tried to sell at the same
  // price I bought at and it technically works but it's wack" is: the order
  // was never marketable.
  //
  // Re-seeded when the action flips, never on the 4 s poll: `seededFor`
  // remembers which action the number on screen belongs to, so a typed price
  // survives every re-render but a buy price never lingers on a sell ticket.
  const seededFor = useRef<'buy' | 'sell' | null>(null);
  useEffect(() => {
    if (!market) return;
    if (price !== '' && seededFor.current === action) return;
    const flip = (v: number | null) => (v === null ? null : 100 - v);
    const touch = action === 'buy'
      ? (side === 'yes' ? market.ask : flip(market.bid))
      : (side === 'yes' ? market.bid : flip(market.ask));
    const fallback = side === 'yes' ? market.probability : flip(market.probability);
    const seed = touch ?? fallback;
    if (seed === null) return;
    seededFor.current = action;
    setPrice(String(Math.min(99, Math.max(1, Math.round(seed)))));
  }, [market, side, price, action]);

  // Any edit invalidates a standing confirmation. Otherwise a user could
  // confirm one number and send a different one.
  useEffect(() => {
    setConfirming(false); setResult(null);
    setConfirmedPreview(null); setPriceMoved(false);
  }, [price, size, action, orderType, side]);

  // `paper` rides along so the BACKEND decides what blocks the ticket. Without
  // it the preview kept reporting the no-key blocker, which disabled the
  // button in the one mode that exists precisely so you can trade without a
  // key.
  // Keyed on the PRIMITIVE fields, never on the `market` object: the 4s poll
  // replaces that object every tick, which re-fired this memo and refetched
  // the preview while the confirmation panel was open.
  const conditionId = market?.conditionId ?? null;
  const req = useMemo(() => (conditionId && priceNum !== null && sizeNum !== null ? {
    conditionId, side, action,
    priceCents: priceNum, size: sizeNum, orderType,
    confirmUsd: 0, paper,
  } : null), [conditionId, side, action, priceNum, sizeNum, orderType, paper]);

  const reqRef = useRef(req);
  reqRef.current = req;

  useEffect(() => {
    if (!req) { setPreview(null); return; }
    let alive = true;
    const h = window.setTimeout(async () => {
      const p = await window.krypt.terminal.preview(req).catch(() => null);
      if (alive && p) setPreview(p);
    }, 250);
    return () => { alive = false; window.clearTimeout(h); };
  }, [req]);

  // A newer preview must never quietly replace the number under a standing
  // confirmation. If the cost moved by more than a couple of cents, drop back
  // to un-confirmed and say so — the user re-reads the new figure and decides.
  useEffect(() => {
    if (!confirming || !confirmedPreview || !preview) return;
    const a = confirmedPreview.costUsd;
    const b = preview.costUsd;
    const moved = a === null || b === null ? a !== b : Math.abs(a - b) > 0.02;
    if (!moved) return;
    setConfirming(false);
    setConfirmedPreview(null);
    setPriceMoved(true);
  }, [preview, confirming, confirmedPreview]);

  const submit = useCallback(async () => {
    const r = reqRef.current;
    // The SNAPSHOT decides, not the live preview: what the user read is what
    // gets sent.
    const snap = confirmedPreview;
    if (!r || !snap || snap.blockers.length) return;
    setSending(true);
    try {
      // Two distinct calls, never one with a flag. `paperPlace` cannot reach
      // the exchange and `place` refuses outright while paper mode is on, so
      // there is no single `if` whose inversion spends real money.
      const res = paper
        ? await window.krypt.terminal.paperPlace({ ...r, confirmUsd: snap.costUsd ?? 0 })
        // Send back exactly the cost the user just read. The BACKEND re-derives
        // it and refuses if the book moved — the confirmation is a contract
        // about a number, not a checkbox.
        : await window.krypt.terminal.place({ ...r, confirmUsd: snap.costUsd ?? 0 });
      setResult({
        ok: res.ok, message: res.message,
        orderId: null, status: res.status ?? null,
        filledSize: res.filledSize ?? null,
        avgPriceCents: res.avgPriceCents ?? null,
        localId: res.localId ?? null,
      });
      setConfirming(false);
      setConfirmedPreview(null);
      toast[res.ok ? 'success' : 'error'](res.message);
      // What you hold just changed (or may have). Tell the position panel
      // rather than making the user wait out its slow cadence.
      onOrderResult();
      if (res.ok) { setSize(''); }
    } finally { setSending(false); }
  }, [confirmedPreview, toast, paper, onOrderResult]);

  // The terminal is manual: a ticket is gated on paper mode, the API key and
  // the manual ceilings (all of which arrive as preview blockers), never on
  // the automation master switch that runs the engines.
  const blocked = !preview || preview.blockers.length > 0;

  return (
    <Card
      className={cls(paper && 'ring-1 ring-krypt-pink/40')}
      header={
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-white">Order ticket</span>
          {/* The mode badge is the most important pixel on this card: it is
              the difference between a simulation and your money. */}
          <span className={cls(
            'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
            paper
              ? 'border-krypt-pink/60 bg-krypt-pink/15 text-krypt-pink'
              : 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win')}
          >
            {paper ? 'paper' : 'live · real money'}
          </span>
        </div>
      }
    >
      {!market ? (
        <div className="py-4 text-center text-sm text-krypt-dim">Loading…</div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2">
            {(['buy', 'sell'] as const).map((a) => (
              <button
                key={a}
                onClick={() => setAction(a)}
                className={cls('rounded-lg border py-2 text-sm capitalize',
                  action === a
                    ? (a === 'buy'
                      ? 'border-krypt-win/60 bg-krypt-win/15 text-krypt-win'
                      : 'border-krypt-loss/60 bg-krypt-loss/15 text-krypt-loss')
                    : 'border-krypt-border text-krypt-muted hover:text-white')}
              >
                {a} {side.toUpperCase()}
              </button>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-[11px] text-krypt-dim">Price (probability ¢)</span>
              <input
                type="number" min={1} max={99} value={price}
                onChange={(e) => setPrice(e.target.value)}
                className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-white focus:border-krypt-purple/50 focus:outline-none"
              />
            </label>
            <label className="block">
              <span className="text-[11px] text-krypt-dim">Size (contracts)</span>
              <input
                type="number" min={1} value={size}
                onChange={(e) => setSize(e.target.value)}
                className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-white focus:border-krypt-purple/50 focus:outline-none"
              />
            </label>
          </div>

          {/* Under the price, because it is about THIS price: a limit that
              does not cross is an offer nobody has to take. The first live
              test lost an afternoon to exactly this. */}
          {orderType === 'GTC' && preview?.crossesNow === false && (
            <Note tone="warn">
              At {preview.priceCents ?? priceNum}¢ this {action} <strong>will
              rest on the book, not fill</strong>
              {action === 'buy' && preview.bestAskCents !== null
                && <> — the best ask is {centsTight(preview.bestAskCents)}</>}
              {action === 'sell' && preview.bestBidCents !== null
                && <> — the best bid is {centsTight(preview.bestBidCents)}</>}
              . It is not a position until someone takes it. Switch to
              Marketable to cross now.
            </Note>
          )}

          <div className="grid grid-cols-3 gap-1">
            {ORDER_TYPES.map((t) => (
              <button
                key={t.id}
                onClick={() => setOrderType(t.id)}
                title={t.hint}
                className={cls('rounded-lg border py-1.5 text-[11px]',
                  orderType === t.id
                    ? 'border-krypt-purple/60 bg-krypt-purple/15 text-white'
                    : 'border-krypt-border text-krypt-muted hover:text-white')}
              >
                {t.label}
              </button>
            ))}
          </div>

          {action === 'sell' && (
            <Note>
              A sell only fills if it crosses a <strong>bid</strong>. Anything
              above the best bid rests on the book — that is an offer, not an
              exit — so the price is seeded at the bid. To close a position in
              one click with a slippage cap instead, use the position panel
              below.
            </Note>
          )}

          {/* The balance, on the ticket, before Review. Three orders in the
              first live test were reviewed and sent against a $2.94 balance
              and rejected by the exchange in a sentence nobody could read. */}
          <div className="flex justify-between text-[11px]">
            <span className="text-krypt-dim">
              {/* Paper: net of what resting paper buys still hold, so it
                  can differ from the account's cash on Track. */}
              {paper ? 'Paper cash, free' : 'Available'}
            </span>
            <span className={cls('font-mono',
              availableUsd === null ? 'text-krypt-dim' : 'text-krypt-muted')}
            >
              {availableUsd === null ? 'balance unknown' : usdExact(availableUsd)}
            </span>
          </div>

          {preview && (
            <div className="space-y-1.5 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-xs">
              {/* Exact dollars, not usd()'s K-rounding: every figure in this
                  block is money this order actually commits. */}
              <Line label="Cost" v={preview.costUsd} fmt={usdExact} strong />
              <Line label="Fee (taker)" v={preview.feeUsd} fmt={usdExact} />
              <Line label="Fillable now" v={preview.fillableSize} fmt={(n) => n.toLocaleString()} />
              <Line label="Est. avg fill" v={preview.estimatedAvgCents} fmt={cents} />
              <Line label="Slippage" v={preview.slippageCents} fmt={(n) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}¢`} />
              <div className="my-1 border-t border-krypt-border" />
              <Line label="Max payout" v={preview.maxPayoutUsd} fmt={usdExact} />
              <Line label="Max loss" v={preview.maxLossUsd} fmt={usdExact} />
            </div>
          )}

          {preview?.warnings.map((w) => (
            <Note key={w} tone="warn">{w}</Note>
          ))}
          {preview?.blockers.map((b) => (
            <Note key={b} tone="warn"><strong>Blocked:</strong> {b}</Note>
          ))}

          {priceMoved && !confirming && (
            <Note tone="warn">
              The price moved while you were confirming — read the new cost and
              confirm again.
            </Note>
          )}

          {!confirming || !confirmedPreview ? (
            <button
              disabled={blocked}
              onClick={() => {
                // Freeze what is on screen right now; the poll may re-quote a
                // moment later and the panel must keep showing THIS number.
                setConfirmedPreview(preview);
                setPriceMoved(false);
                setConfirming(true);
              }}
              className={cls(
                'w-full rounded-lg py-2.5 text-sm font-medium transition-colors',
                blocked
                  ? 'cursor-not-allowed bg-krypt-surface2 text-krypt-dim'
                  : 'bg-krypt-purple/20 text-white ring-1 ring-krypt-purple/50 hover:bg-krypt-purple/30')}
            >
              {`Review ${paper ? 'paper ' : ''}${action} ${sizeNum ?? 0} ${side.toUpperCase()}`}
            </button>
          ) : (
            <div className={cls('space-y-2 rounded-lg border p-3',
              paper
                ? 'border-krypt-pink/40 bg-krypt-pink/5'
                : 'border-krypt-purple/40 bg-krypt-purple/5')}
            >
              {paper && (
                <div className="text-[11px] uppercase tracking-wider text-krypt-pink">
                  Simulated — no real money moves
                </div>
              )}
              {/* The SNAPSHOT, not the live preview — and exact dollars, so a
                  $1,249 order can never confirm as "$1.2K". */}
              <div className="text-xs text-white">
                {action === 'buy' ? 'Buying' : 'Selling'}{' '}
                <span className="font-mono">{confirmedPreview.size ?? sizeNum}</span>{' '}
                {side.toUpperCase()} at{' '}
                <span className="font-mono">{confirmedPreview.priceCents ?? priceNum}¢</span> for{' '}
                <span className="font-mono">{usdExact(confirmedPreview.costUsd ?? 0)}</span>.
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => { setConfirming(false); setConfirmedPreview(null); }}
                  className="flex-1 rounded-lg border border-krypt-border py-2 text-sm text-krypt-muted hover:text-white"
                >
                  Cancel
                </button>
                <button
                  onClick={submit}
                  disabled={sending}
                  className="flex-1 rounded-lg bg-krypt-purple/30 py-2 text-sm font-medium text-white ring-1 ring-krypt-purple/60 hover:bg-krypt-purple/40 disabled:opacity-50"
                >
                  {sending ? 'Sending…' : 'Confirm'}
                </button>
              </div>
            </div>
          )}

          {paper && (
            <Note>
              Paper fills come out of the <strong>real</strong> book at the
              price you would actually have walked — never your limit, never
              more size than was resting. A resting order fills only once the
              market trades <em>through</em> it, because we cannot know your
              place in the queue.
            </Note>
          )}

          {result && (
            <Note tone={result.ok ? 'info' : 'warn'}>
              {result.message}
              {result.ok && (
                <button onClick={onNavTrack} className="ml-1 underline underline-offset-2">
                  See it on {paper ? 'Track → Paper' : 'Track'}
                </button>
              )}
            </Note>
          )}

          <StandingForm market={market} side={side} />
        </div>
      )}
    </Card>
  );
}

function Line({
  label, v, fmt, strong,
}: { label: string; v: number | null; fmt: (n: number) => string; strong?: boolean }) {
  return (
    <div className="flex justify-between">
      <span className="text-krypt-dim">{label}</span>
      <span className={cls('font-mono', strong ? 'text-base text-white' : 'text-krypt-muted')}>
        <Val v={v} fmt={fmt} />
      </span>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Position panel — what you hold HERE, and the one click that closes it
// ────────────────────────────────────────────────────────────────────────
//
// The ticket can sell, but only if you type a price that crosses a bid. The
// first live test of this app closed with "selling is a little wack… I tried
// to sell at the same price I bought at" — a limit at your entry rests on the
// book, so the exit sat there doing nothing. This panel is the other half:
// you pick a slippage tolerance, the backend walks the bids, and it sends one
// marketable fill-and-kill sized to exactly what fits inside that tolerance.

/** How slack the exit may be, in cents below the best bid. */
const SLIPPAGE_CHOICES = [1, 2, 5, 10];
const DEFAULT_SLIPPAGE = 5;
// Deliberately NOT the 4 s market/book poll. This reads the whole account (or
// the whole paper ledger) and prices nothing, so it runs slowly and on
// demand: on mount, after any order, and every 12 s.
const POSITION_POLL_MS = 12_000;

function PositionPanel({
  market, refreshKey, onNavTrack,
}: {
  market: MarketSummary | null;
  refreshKey: number;
  onNavTrack: () => void;
}) {
  const { config } = useApp();
  const paper = !!config?.terminalPaperMode;
  const cid = market?.conditionId ?? null;
  const [rows, setRows] = useState<TerminalPosition[]>([]);
  // Orders of ours still sitting on the book in THIS market. They are the
  // other half of "where did my money go": a resting buy is neither a
  // position nor a failure, and until this panel showed it there was nowhere
  // in the app that said it existed while you were looking at the market.
  const [working, setWorking] = useState<RestingOrder[]>([]);
  // "You hold nothing here" and "we could not read what you hold" are
  // different sentences, and an empty panel says the first one. So a failed
  // read carries its reason instead of rendering as no position.
  const [failure, setFailure] = useState<string | null>(null);

  const loadWorking = useCallback(async () => {
    // Paper orders never reach the venue, so this read would be about the
    // wrong ledger entirely. The paper simulator keeps its own resting list
    // on Track → Paper.
    if (!cid || paper) { setWorking([]); return; }
    const r = await window.krypt.terminal.orders(cid).catch(() => null);
    // A failed read leaves the previous rows alone rather than emptying the
    // section — "we could not ask" must not render as "nothing is working".
    if (!r) return;
    setWorking((r.orders ?? []).filter(
      (o) => o.remainingSize === null || o.remainingSize === undefined
        || o.remainingSize > 0));
  }, [cid, paper]);

  const load = useCallback(async () => {
    if (!cid) return;
    void loadWorking();
    try {
      const pf = paper
        ? await window.krypt.terminal.paperPortfolio()
        : await window.krypt.terminal.portfolio();
      // `walletReadable: false` is the backend saying it never got an answer.
      // Its empty `positions` is "we could not ask", not "you hold nothing",
      // and rendering the two the same way is how a panel tells you a
      // position vanished.
      if (pf.walletReadable === false) {
        setRows([]);
        setFailure(pf.unreconciledNote
          ?? `Could not read your ${paper ? 'paper account' : 'account'}, so `
            + 'what you hold here is unknown — not nothing.');
        return;
      }
      const mine = (pf.positions ?? [])
        .filter((p) => p.conditionId === cid && p.size > 0);
      setRows(mine);
      setFailure(null);
    } catch {
      setRows([]);
      setFailure(
        `Could not read your ${paper ? 'paper account' : 'account'}, so what you `
        + 'hold here is unknown — not nothing. Nothing was sold.');
    }
  }, [cid, paper, loadWorking]);

  useEffect(() => {
    setRows([]);
    setWorking([]);
    setFailure(null);
  }, [cid, paper]);

  useEffect(() => {
    if (!cid) return;
    let alive = true;
    const run = () => { if (alive) void load(); };
    run();
    const id = window.setInterval(run, POSITION_POLL_MS);
    return () => { alive = false; window.clearInterval(id); };
  }, [cid, load, refreshKey]);

  // The venue's order list lags its own acknowledgement by a moment, so the
  // read fired the instant an order came back routinely missed the order that
  // had just been placed — and an order that does not appear reads as an
  // order that did not happen. One extra read, three seconds later, on order
  // results only (refreshKey starts at 0 and only a result moves it).
  useEffect(() => {
    if (!cid || !refreshKey) return;
    const h = window.setTimeout(() => { void load(); }, 3000);
    return () => window.clearTimeout(h);
  }, [cid, refreshKey, load]);

  if (!cid) return null;
  if (!rows.length && !working.length && !failure) return null;

  return (
    <Card
      className={cls(paper && 'ring-1 ring-krypt-pink/40')}
      header={
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-white">
            {rows.length ? 'Your position here' : 'Working here'}
          </span>
          <span className={cls(
            'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
            paper
              ? 'border-krypt-pink/60 bg-krypt-pink/15 text-krypt-pink'
              : 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win')}
          >
            {paper ? 'paper' : 'live · real money'}
          </span>
        </div>
      }
    >
      <div className="space-y-3">
        {failure ? (
          <Note tone="warn">{failure}</Note>
        ) : (
          rows.map((p) => (
            <PositionRow
              key={`${p.conditionId}:${p.side}`}
              position={p}
              paper={paper}
              onSold={() => { void load(); }}
              onNavTrack={onNavTrack}
            />
          ))
        )}

        {working.length > 0 && (
          <div className="space-y-2">
            <div className="text-[11px] uppercase tracking-wider text-krypt-dim">
              Working orders
            </div>
            {working.map((o) => (
              <WorkingRow
                key={o.orderId}
                order={o}
                onChanged={() => { void load(); }}
              />
            ))}
          </div>
        )}
      </div>
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// One resting order of ours, in this market
// ────────────────────────────────────────────────────────────────────────
//
// It is NOT a position and this row says so in as many words. The two buttons
// are the only two things that can be done about it: take it off the book, or
// stop waiting and cross the spread.

function WorkingRow({
  order, onChanged,
}: { order: RestingOrder; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [pv, setPv] = useState<ManualOrderPreview | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const filled = order.filledSize ?? 0;
  const remaining = order.remainingSize
    ?? (order.size === null ? null : order.size - filled);
  const touch = order.bestTouchCents ?? null;
  const touchLabel = order.action === 'buy' ? 'ask' : 'bid';

  const cancel = useCallback(async () => {
    setBusy(true);
    try {
      const r = await window.krypt.terminal.cancel(order.orderId);
      toast[r.ok ? 'success' : 'error'](r.message);
      onChanged();
    } finally { setBusy(false); }
  }, [order.orderId, toast, onChanged]);

  // Reprice = cancel, then send the same size as a marketable order at the
  // touch. Priced FIRST, so the confirmation is about a number the user read;
  // the frozen preview is the one that gets sent, exactly as on the ticket.
  const repriceReq = useMemo(() => (
    touch === null || remaining === null || remaining <= 0 ? null : {
      conditionId: order.conditionId,
      side: order.side,
      action: order.action,
      priceCents: Math.round(touch),
      size: remaining,
      orderType: 'FAK' as OrderType,
      confirmUsd: 0,
      paper: false,
    }), [order.conditionId, order.side, order.action, touch, remaining]);

  const startReprice = useCallback(async () => {
    if (!repriceReq) return;
    setBusy(true);
    setNote(null);
    try {
      const p = await window.krypt.terminal.preview(repriceReq).catch(() => null);
      if (!p) { setNote('Could not price the replacement, so nothing was cancelled.'); return; }
      setPv(p);
      setConfirming(true);
    } finally { setBusy(false); }
  }, [repriceReq]);

  const doReprice = useCallback(async () => {
    if (!repriceReq || !pv) return;
    setBusy(true);
    try {
      // Cancel first: two live orders for the same intent is the one outcome
      // worse than a resting order that never fills.
      const c = await window.krypt.terminal.cancel(order.orderId);
      if (!c.ok) {
        setNote(`Nothing was repriced — the cancel failed: ${c.message}`);
        toast.error(c.message);
        return;
      }
      const r = await window.krypt.terminal.place({
        ...repriceReq, confirmUsd: pv.costUsd ?? 0,
      });
      // The gap between the two calls is real, and if the replacement is
      // refused the user now holds NEITHER order. Saying so is the whole
      // point of this panel.
      setNote(r.ok ? r.message
        : `Your resting order was cancelled but the replacement was refused: ${r.message} `
          + 'Nothing is on the book now.');
      toast[r.ok ? 'success' : 'error'](r.message);
    } finally {
      setBusy(false);
      setConfirming(false);
      setPv(null);
      onChanged();
    }
  }, [repriceReq, pv, order.orderId, toast, onChanged]);

  return (
    <div className="space-y-2 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-xs">
      <div className="text-white">
        <span className="text-krypt-dim">Working · </span>
        <span className="capitalize">{order.action}</span>{' '}
        <span className="font-mono">{order.size ?? '—'}</span>{' '}
        {order.side.toUpperCase()} @{' '}
        <span className="font-mono">
          {order.priceCents === null ? '—' : centsTight(order.priceCents)}
        </span>
        <span className="text-krypt-dim"> · </span>
        <span className="font-mono">{filled}</span>
        <span className="text-krypt-dim"> of </span>
        <span className="font-mono">{order.size ?? '—'}</span>
        <span className="text-krypt-dim"> filled</span>
        {touch !== null && (
          <>
            <span className="text-krypt-dim"> · best {touchLabel} </span>
            <span className="font-mono">{centsTight(touch)}</span>
          </>
        )}
        <span className="text-krypt-dim"> — not a position until it fills</span>
      </div>

      {order.origin !== 'manual' && (
        <div className="text-[11px] text-krypt-dim">
          Placed by: {ORIGIN_LABEL[order.origin] ?? order.origin}
        </div>
      )}

      {note && <Note tone="info">{note}</Note>}

      {confirming && pv ? (
        <div className="space-y-2 rounded-lg border border-krypt-purple/40 bg-krypt-purple/5 p-2">
          {pv.blockers.length ? (
            <Note tone="warn"><strong>Blocked:</strong> {pv.blockers[0]}</Note>
          ) : (
            <div className="text-white">
              Cancel and re-send {repriceReq?.size} {order.side.toUpperCase()} as a
              marketable {order.action} at{' '}
              <span className="font-mono">{centsTight(repriceReq?.priceCents ?? 0)}</span> for{' '}
              <span className="font-mono">{usdExact(pv.costUsd ?? 0)}</span>.
            </div>
          )}
          <div className="flex gap-2">
            <button
              onClick={() => { setConfirming(false); setPv(null); }}
              className="flex-1 rounded-lg border border-krypt-border py-1.5 text-krypt-muted hover:text-white"
            >
              Keep it resting
            </button>
            <button
              onClick={doReprice}
              disabled={busy || pv.blockers.length > 0}
              className="flex-1 rounded-lg bg-krypt-purple/30 py-1.5 font-medium text-white ring-1 ring-krypt-purple/60 hover:bg-krypt-purple/40 disabled:opacity-50"
            >
              {busy ? 'Working…' : 'Confirm reprice'}
            </button>
          </div>
        </div>
      ) : (
        <div className="flex gap-2">
          <button
            onClick={cancel}
            disabled={busy}
            className="flex-1 rounded-lg border border-krypt-border py-1.5 text-krypt-muted hover:text-white disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={startReprice}
            disabled={busy || !repriceReq}
            title={repriceReq ? undefined
              : 'We could not read the touch, so there is no price to cross to.'}
            className="flex-1 rounded-lg border border-krypt-purple/50 bg-krypt-purple/10 py-1.5 text-white hover:bg-krypt-purple/20 disabled:opacity-50"
          >
            Reprice to fill
          </button>
        </div>
      )}
    </div>
  );
}

function PositionRow({
  position, paper, onSold, onNavTrack,
}: {
  position: TerminalPosition;
  paper: boolean;
  onSold: () => void;
  onNavTrack: () => void;
}) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [sizeText, setSizeText] = useState('');
  const [slip, setSlip] = useState(DEFAULT_SLIPPAGE);
  const [pv, setPv] = useState<SellPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<SellResult | null>(null);

  const label = position.sideLabel ?? position.side.toUpperCase();
  const sizeNum = sizeText === '' ? null : Number(sizeText);

  // The market resolved: there is no book, and a decided position settles
  // (or worthless), never sold. Said here rather than after a pointless round
  // trip that would come back "could not read the order book".
  const settledReason = position.settled
    ? (position.resolvedOutcome === position.side
      ? 'This market has resolved in your favour — a won position settles, it is not sold.'
      : 'This market has resolved and this side lost, so there is nothing to sell.')
    : null;

  // THE FREEZE. There is no poll behind this panel: the preview is taken once
  // when the confirm opens, and re-taken only when the size or the slippage
  // changes. So the number the user reads is the number `confirmUsd` carries,
  // and the backend refuses the order if the book moved underneath it.
  useEffect(() => {
    if (!open) { setPv(null); return; }
    let alive = true;
    setLoading(true);
    void (async () => {
      try {
        const p = await window.krypt.terminal.sellPreview({
          conditionId: position.conditionId,
          side: position.side,
          ...(sizeNum !== null && sizeNum > 0 ? { size: sizeNum } : {}),
          maxSlippageCents: slip,
        });
        if (alive) setPv(p);
      } catch {
        if (alive) setPv(null);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
    // `position` deliberately absent: the 12 s refresh must not re-quote a
    // confirmation the user is already reading.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, sizeNum, slip, position.conditionId, position.side]);

  const submit = useCallback(async () => {
    if (!pv || pv.blockers.length || pv.fillableSize === null) return;
    setSending(true);
    try {
      const res = await window.krypt.terminal.sellPosition({
        conditionId: position.conditionId,
        side: position.side,
        size: pv.fillableSize,
        maxSlippageCents: pv.maxSlippageCents,
        // Exactly the figure on screen. Main re-derives it and refuses the
        // sell if the book moved — a contract about a number, not a checkbox.
        confirmUsd: pv.proceedsUsd ?? 0,
      });
      setResult(res);
      setOpen(false);
      setPv(null);
      toast[res.ok ? 'success' : 'error'](res.message);
      onSold();
    } finally { setSending(false); }
  }, [pv, position.conditionId, position.side, toast, onSold]);

  return (
    <div className="rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-medium text-white">{label}</span>
        <span className="font-mono text-sm text-krypt-muted">
          <Val v={position.size} fmt={(n) => n.toLocaleString()} /> contracts
        </span>
      </div>

      <div className="mt-2 space-y-1.5 text-xs">
        <div className="flex justify-between">
          <span className="text-krypt-dim">Avg cost</span>
          {/* The provenance dot is the point: a basis we computed from your
              fills and one Polymarket US computed for your account are different
              claims, and the screen has to say which. */}
          <span
            className="inline-flex items-center gap-1 font-mono text-krypt-muted"
            title={position.basisSource === 'data-api'
              ? 'Polymarket US’s own average for this account — your fill ledger '
                + 'does not reach back this far.'
              : position.basisSource === 'ledger'
                ? 'Average cost computed here from your reconciled fills.'
                : undefined}
          >
            <Src stamp={position.sources?.avgCost} />
            <Val v={position.avgCostCents} fmt={cents} />
          </span>
        </div>
        <Line label="Mark" v={position.markCents} fmt={cents} />
        <Line label="Value" v={position.valueUsd} fmt={usdExact} />
        <div className="flex justify-between">
          <span className="text-krypt-dim">Unrealised P&amp;L</span>
          <span className={cls('font-mono',
            position.unrealizedPnlUsd === null ? 'text-krypt-dim'
              : position.unrealizedPnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}
          >
            <Val
              v={position.unrealizedPnlUsd}
              fmt={signedUsd}
              title="No cost basis for these contracts, so the P&L is unknown — not zero."
            />
          </span>
        </div>
      </div>

      {settledReason ? (
        <Note>
          {settledReason}{' '}
          <button onClick={onNavTrack} className="underline underline-offset-2">
            See the settled record
          </button>
        </Note>
      ) : !open ? (
        <button
          onClick={() => { setResult(null); setOpen(true); }}
          className="mt-3 w-full rounded-lg border border-krypt-loss/50 bg-krypt-loss/10 py-2 text-sm font-medium text-krypt-loss hover:bg-krypt-loss/20"
        >
          {paper ? 'Sell paper' : 'Sell · real money'}
        </button>
      ) : (
        <div className={cls('mt-3 space-y-2 rounded-lg border p-3',
          paper
            ? 'border-krypt-pink/40 bg-krypt-pink/5'
            : 'border-krypt-purple/40 bg-krypt-purple/5')}
        >
          {paper && (
            <div className="text-[11px] uppercase tracking-wider text-krypt-pink">
              Simulated — no real money moves
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-[11px] text-krypt-dim">Size (contracts)</span>
              <input
                type="number" min={1} max={position.size}
                value={sizeText}
                placeholder={String(Math.floor(position.size))}
                onChange={(e) => setSizeText(e.target.value)}
                className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-white focus:border-krypt-purple/50 focus:outline-none"
              />
            </label>
            <div>
              <span className="text-[11px] text-krypt-dim">Max slippage</span>
              <div className="mt-0.5 grid grid-cols-4 gap-1">
                {SLIPPAGE_CHOICES.map((c) => (
                  <button
                    key={c}
                    onClick={() => setSlip(c)}
                    title={`The order can never fill more than ${c}¢ below the best bid.`}
                    className={cls('rounded-md border py-1 text-[11px]',
                      slip === c
                        ? 'border-krypt-purple/60 bg-krypt-purple/15 text-white'
                        : 'border-krypt-border text-krypt-muted hover:text-white')}
                  >
                    {c}¢
                  </button>
                ))}
              </div>
            </div>
          </div>

          {loading && !pv ? (
            <div className="py-2 text-center text-xs text-krypt-dim">
              Walking the book…
            </div>
          ) : pv ? (
            <>
              <div className="space-y-1.5 border-t border-krypt-border pt-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-krypt-dim">Fillable</span>
                  <span className="font-mono text-krypt-muted">
                    <Val v={pv.fillableSize} fmt={(n) => n.toLocaleString()} />
                    {' of '}
                    <Val v={pv.size} fmt={(n) => n.toLocaleString()} />
                  </span>
                </div>
                <Line label="Est. avg fill" v={pv.estimatedAvgCents} fmt={cents} />
                <Line label="Worst fill (your limit)" v={pv.worstFillCents} fmt={cents} />
                <Line label="Est. proceeds" v={pv.proceedsUsd} fmt={usdExact} strong />
                <Line label="Fee (taker)" v={pv.feeUsd} fmt={usdExact} />
                <div className="flex justify-between">
                  <span className="text-krypt-dim">Est. P&amp;L</span>
                  <span className={cls('font-mono',
                    pv.estimatedPnlUsd === null ? 'text-krypt-dim'
                      : pv.estimatedPnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}
                  >
                    <Val
                      v={pv.estimatedPnlUsd}
                      fmt={signedUsd}
                      title="No cost basis for these contracts, so the P&L is unknown — not zero."
                    />
                  </span>
                </div>
              </div>

              <div className="text-[11px] leading-snug text-krypt-dim">{pv.note}</div>

              {pv.warnings.map((w) => <Note key={w} tone="warn">{w}</Note>)}
              {pv.blockers.map((b) => (
                <Note key={b} tone="warn"><strong>Blocked:</strong> {b}</Note>
              ))}
            </>
          ) : (
            <Note tone="warn">
              This sell could not be priced, so nothing is being offered to
              send. Nothing was sold.
            </Note>
          )}

          <div className="flex gap-2">
            <button
              onClick={() => { setOpen(false); setPv(null); }}
              className="flex-1 rounded-lg border border-krypt-border py-2 text-sm text-krypt-muted hover:text-white"
            >
              Cancel
            </button>
            <button
              onClick={submit}
              disabled={sending || loading || !pv || pv.blockers.length > 0}
              title={pv?.blockers[0]}
              className="flex-1 rounded-lg bg-krypt-loss/25 py-2 text-sm font-medium text-white ring-1 ring-krypt-loss/60 hover:bg-krypt-loss/35 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {sending ? 'Selling…'
                : `Sell ${pv?.fillableSize ?? ''} ${paper ? 'paper' : ''}`.trim()}
            </button>
          </div>
        </div>
      )}

      {result && (
        <Note tone={result.ok ? 'info' : 'warn'}>
          {result.message}
          {result.ok && (
            <>
              {result.filledSize !== null && (
                <>
                  {' '}Filled{' '}
                  <span className="font-mono">{result.filledSize}</span>
                  {result.avgPriceCents !== null && (
                    <> at <span className="font-mono">{cents(result.avgPriceCents)}</span></>
                  )}
                  {result.proceedsUsd !== null && (
                    <> for <span className="font-mono">{usdExact(result.proceedsUsd)}</span></>
                  )}
                  {!!result.killedSize && (
                    <>; <span className="font-mono">{result.killedSize}</span> killed
                      (still yours)</>
                  )}
                  .
                </>
              )}
              <button onClick={onNavTrack} className="ml-1 underline underline-offset-2">
                See it on {paper ? 'Track → Paper' : 'Track'}
              </button>
            </>
          )}
        </Note>
      )}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Standing instructions
// ────────────────────────────────────────────────────────────────────────

const KINDS: StandingKind[] = ['stopLoss', 'takeProfit', 'alertAbove', 'alertBelow'];

function StandingForm({ market, side }: { market: MarketSummary; side: 'yes' | 'no' }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<StandingKind>('stopLoss');
  const [trigger, setTrigger] = useState('');
  const [size, setSize] = useState('');

  const needsSize = kind === 'stopLoss' || kind === 'takeProfit';

  const add = useCallback(async () => {
    const r = await window.krypt.terminal.standing({
      action: 'add',
      item: {
        conditionId: market.conditionId, title: market.title, side, kind,
        triggerCents: Number(trigger),
        size: needsSize ? Number(size) : null,
      },
    });
    if (r.error) { toast.error(r.error); return; }
    toast.success(`${STANDING_LABEL[kind]} armed at ${trigger}¢.`);
    setTrigger(''); setSize(''); setOpen(false);
  }, [market, side, kind, trigger, size, needsSize, toast]);

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="w-full rounded-lg border border-dashed border-krypt-border py-2 text-xs text-krypt-dim hover:text-white"
      >
        + Standing instruction (stop / take-profit / alert)
      </button>
    );
  }

  return (
    <div className="space-y-2 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3">
      <p className="text-[11px] text-krypt-muted">
        This fires later without a click — because you wrote it down. It passes
        the same checks and the same live-trading switch a manual order does,
        and you can see and cancel it on the Track screen.
      </p>
      <div className="grid grid-cols-2 gap-1">
        {KINDS.map((k) => (
          <button
            key={k}
            onClick={() => setKind(k)}
            className={cls('rounded-lg border py-1.5 text-[11px]',
              kind === k
                ? 'border-krypt-purple/60 bg-krypt-purple/15 text-white'
                : 'border-krypt-border text-krypt-muted hover:text-white')}
          >
            {STANDING_LABEL[k]}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="block">
          <span className="text-[11px] text-krypt-dim">
            Trigger (probability points)
          </span>
          <input
            type="number" min={1} max={99} value={trigger}
            onChange={(e) => setTrigger(e.target.value)}
            className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1 font-mono text-sm text-white focus:outline-none"
          />
        </label>
        {needsSize && (
          <label className="block">
            <span className="text-[11px] text-krypt-dim">Contracts to close</span>
            <input
              type="number" min={1} value={size}
              onChange={(e) => setSize(e.target.value)}
              className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1 font-mono text-sm text-white focus:outline-none"
            />
          </label>
        )}
      </div>
      <div className="flex gap-2">
        <button
          onClick={() => setOpen(false)}
          className="flex-1 rounded-lg border border-krypt-border py-1.5 text-xs text-krypt-muted hover:text-white"
        >
          Cancel
        </button>
        <button
          onClick={add}
          disabled={!trigger || (needsSize && !size)}
          className="flex-1 rounded-lg bg-krypt-purple/25 py-1.5 text-xs text-white ring-1 ring-krypt-purple/50 disabled:opacity-40"
        >
          Arm
        </button>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Related markets
//
// The resolution report already works these out to look for a contradiction.
// Showing them is nearly free, and it is the closest thing to cross-venue
// arbitrage available inside a single venue: a sibling asking a
// near-identical question at a very different price means one of the two is
// wrong, and it might be the one you are about to trade.
// ────────────────────────────────────────────────────────────────────────

function RelatedPanel({
  related, onOpen,
}: { related: RelatedMarkets | null; onOpen: (cid: string) => void }) {
  if (!related || (!related.rows.length && !related.note)) return null;

  return (
    <Card header={
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-sm font-medium text-white">
          <Network className="h-4 w-4 text-krypt-purple" /> Related markets
        </span>
      </div>
    }>
      {related.note && !related.rows.length ? (
        <Note>{related.note}</Note>
      ) : (
        <>
          <div className="space-y-1">
            {related.rows.map((r) => (
              <button
                key={r.conditionId}
                onClick={() => onOpen(r.conditionId)}
                className="flex w-full items-center gap-3 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-white/[0.03]"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-white">{r.title}</div>
                  <div className="mt-0.5 flex items-center gap-2 text-[11px] text-krypt-dim">
                    <span>24h <Val v={r.volume24hUsd} fmt={usd} /></span>
                    {r.gapCents !== null && (
                      <span
                        className={cls(r.contradicts && 'text-krypt-warn')}
                        title="This sibling asks a near-identical question, so a large gap means one of the two is wrong."
                      >
                        {r.gapCents.toFixed(1)}¢ away on a near-identical question
                      </span>
                    )}
                  </div>
                </div>
                <div className="w-24 shrink-0">
                  <ProbBar v={r.probability} />
                </div>
                <div className="w-14 shrink-0 text-right font-mono text-xs text-white">
                  <Val v={r.probability} fmt={cents} />
                </div>
              </button>
            ))}
          </div>
        </>
      )}
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Cross-venue: the same question on Kalshi
//
// The one panel in the terminal that asserts something no provider told us —
// that these two markets ask the same question. So it shows the matched title
// verbatim, the reasons for AND against, and it withholds the spread entirely
// unless the pairing is strong. A fictional edge is exactly the kind of
// number somebody trades on.
// ────────────────────────────────────────────────────────────────────────

function CrossVenuePanel({
  cross, loading, onRun, enabled,
}: {
  cross: CrossVenueComparison | null;
  loading: boolean;
  onRun: () => void;
  enabled: boolean;
}) {
  const [showWhy, setShowWhy] = useState(false);

  return (
    <Card header={
      <div className="flex items-center justify-between">
        <span className="flex items-center gap-2 text-sm font-medium text-white">
          <Scale className="h-4 w-4 text-krypt-warn" /> Same question on Kalshi
        </span>
        <button
          onClick={onRun}
          disabled={loading}
          className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-2.5 py-1 text-xs text-krypt-muted hover:text-white disabled:opacity-50"
        >
          <RefreshCw className={cls('h-3 w-3', loading && 'animate-spin')} />
          {enabled ? 'Re-check' : 'Look for a match'}
        </button>
      </div>
    }>
      {!enabled ? (
        <div className="space-y-2">
          <p className="text-xs text-krypt-muted">
            Polymarket US and Kalshi list overlapping questions. Seeing the same
            one priced on both is useful context — a second opinion from a
            different pool of traders — and you can check it yourself in two
            clicks. Reference data only: this app reads Kalshi, it never trades
            there.
          </p>
          {/* Expensive analysis is opt-in with its price stated. */}
          <p className="text-[11px] text-krypt-dim">
            The first check builds an index of Kalshi&rsquo;s ~11,800 market
            series (a few seconds). After that it is nearly free, and it never
            runs as part of a background poll.
          </p>
        </div>
      ) : loading && !cross ? (
        <div className="py-6 text-center text-sm text-krypt-dim">
          Indexing Kalshi&hellip;
        </div>
      ) : !cross ? null : !cross.kalshi || !cross.match ? (
        <Note>{cross.note ?? 'No counterpart found.'}</Note>
      ) : (
        <div className="space-y-3">
          {/* ── the two prices, side by side, never blended ───────────── */}
          <div className="grid grid-cols-2 gap-3">
            <VenueBox
              venue="Polymarket US"
              title={cross.polymarket?.title ?? null}
              subtitle={null}
              prob={cross.polymarket?.probability ?? null}
              bid={cross.polymarket?.bid ?? null}
              ask={cross.polymarket?.ask ?? null}
              vol={cross.polymarket?.volume24hUsd ?? null}
              close={cross.polymarket?.closeTime ?? null}
            />
            <VenueBox
              venue={cross.kalshi.side === 'no' ? 'Kalshi · NO side, inverted' : 'Kalshi'}
              accent
              title={cross.kalshi.title}
              subtitle={cross.kalshi.subtitle}
              prob={cross.kalshi.probability}
              bid={cross.kalshi.bid}
              ask={cross.kalshi.ask}
              vol={cross.kalshi.volume24hUsd}
              close={cross.kalshi.closeTime}
              onOpen={() => void window.krypt.app.openExternal(cross.kalshi!.url)}
            />
          </div>

          {/* ── the gap, or an explicit refusal to print one ──────────── */}
          <div className="flex items-center gap-3 rounded-lg border border-krypt-border bg-krypt-surface2/40 px-3 py-2">
            <div className="font-mono text-2xl text-white">
              <Val
                v={cross.spreadCents}
                fmt={(n) => `${n > 0 ? '+' : ''}${n.toFixed(1)}¢`}
                title="No spread is shown for a pairing this uncertain."
              />
            </div>
            <div className="min-w-0 flex-1">
              <div className="text-[10px] uppercase tracking-wider text-krypt-dim">
                Difference
              </div>
              <div className="text-xs text-krypt-muted">{cross.spreadNote}</div>
            </div>
            <span className={cls(
              'shrink-0 rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
              cross.match.confidence === 'strong'
                ? 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win'
                : 'border-krypt-warn/40 bg-krypt-warn/10 text-krypt-warn')}
            >
              {cross.match.confidence} match
            </span>
          </div>

          <button
            onClick={() => setShowWhy((v) => !v)}
            className="text-[11px] text-krypt-dim underline-offset-2 hover:text-white hover:underline"
          >
            {showWhy ? 'Hide' : 'Why do we think these are the same question?'}
          </button>

          {showWhy && (
            <div className="space-y-1.5 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-[11px]">
              <div className="text-krypt-muted">
                Match score {cross.match.score.toFixed(2)} — this is our
                inference, not something either venue states.
              </div>
              {cross.kalshi.side === 'no' && (
                <div className="text-krypt-warn">
                  Compared Polymarket US YES with Kalshi NO: the Kalshi price, bid, ask
                  and history above are 100 minus its YES quote, so the two numbers
                  answer the same question.
                </div>
              )}
              {cross.match.for.map((r) => (
                <div key={r} className="text-krypt-win">+ {r}</div>
              ))}
              {/* Shown even on a strong match: the reasons against do not stop
                  mattering just because the score cleared a threshold. */}
              {cross.match.against.map((r) => (
                <div key={r} className="text-krypt-warn">− {r}</div>
              ))}
            </div>
          )}

          {cross.historyNote && <Note>{cross.historyNote}</Note>}

          {/* ── the venue difference, always ──────────────────────────── */}
          <Note tone="warn">{cross.venueNote}</Note>
          <ul className="space-y-1 text-[11px] text-krypt-dim">
            {cross.caveats.map((c) => <li key={c}>• {c}</li>)}
          </ul>
        </div>
      )}
    </Card>
  );
}

function VenueBox({
  venue, title, subtitle, prob, bid, ask, vol, close, accent, onOpen,
}: {
  venue: string;
  title: string | null;
  subtitle: string | null;
  prob: number | null;
  bid: number | null;
  ask: number | null;
  vol: number | null;
  close: string | null;
  accent?: boolean;
  onOpen?: () => void;
}) {
  return (
    <div className={cls(
      'rounded-lg border p-3',
      accent ? 'border-krypt-warn/30 bg-krypt-warn/[0.04]' : 'border-krypt-border')}
    >
      <div className="flex items-center justify-between">
        <span className={cls('text-[10px] uppercase tracking-wider',
          accent ? 'text-krypt-warn' : 'text-krypt-purple')}
        >
          {venue}
        </span>
        {onOpen && (
          <button onClick={onOpen} title="Open on Kalshi to check this yourself"
            className="text-krypt-dim hover:text-white">
            <ExternalLink className="h-3 w-3" />
          </button>
        )}
      </div>
      {/* The matched title VERBATIM — the single most useful thing on the
          panel, because it is what lets the user reject a bad pairing. */}
      <div className="mt-1 line-clamp-2 text-[11px] text-white" title={title ?? undefined}>
        {title ?? '—'}
      </div>
      {subtitle && (
        <div className="text-[11px] text-krypt-muted">{subtitle}</div>
      )}
      <div className="mt-2 font-mono text-2xl text-white">
        <Val v={prob} fmt={cents} />
      </div>
      <div className="mt-1 text-[11px] text-krypt-dim">
        bid <Val v={bid} fmt={cents} /> · ask <Val v={ask} fmt={cents} />
      </div>
      <div className="text-[11px] text-krypt-dim">
        24h <Val v={vol} fmt={usd} /> · closes {close ? new Date(close).toLocaleDateString() : '—'}
      </div>
    </div>
  );
}
