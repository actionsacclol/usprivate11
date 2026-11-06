import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft, ChevronDown, ChevronUp, Maximize2, Search, SlidersHorizontal,
  Star, X,
} from 'lucide-react';
import type {
  DiscoverColumn, DiscoverFilters, DiscoverResult, MarketSummary, WatchEntry,
} from '@shared/market';
import { DISCOVER_COLUMNS } from '@shared/market';
import { Card, Empty, Page } from '../components/common';
import {
  Note, ProbBar, Src, Unknowns, Val, cents, hours, usd,
} from '../components/terminal/TerminalBits';
import { selectMarket } from '../state/terminalNav';
import { cls } from '../utils/format';

// Module-level cache so re-mounting the tab shows last-known data instantly
// instead of four empty columns (same pattern as the other live pages).
const cached: Partial<Record<DiscoverColumn, DiscoverResult>> = {};

const REFRESH_MS = 20_000;

/**
 * Rows the expanded view asks for. The backend hard-caps `discover` at 100 and
 * The exchange's page is 100, so this IS the whole page — asking for more would be a
 * number the data cannot honour, and a table that silently shows fewer rows
 * than it claims is the same lie as a coerced zero.
 */
const SCREENER_LIMIT = 100;

export function DiscoverPage({ onNav }: { onNav: (p: 'market') => void }) {
  const [results, setResults] = useState(cached);
  const [filters, setFilters] = useState<DiscoverFilters>({});
  const [showFilters, setShowFilters] = useState(false);
  const [query, setQuery] = useState('');
  const [searchResult, setSearchResult] = useState<DiscoverResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [watch, setWatch] = useState<WatchEntry[]>([]);
  // Expanded ("screener") view: one column, every row we can get, as a dense
  // sortable table. null = the normal four-column board.
  const [screener, setScreener] = useState<DiscoverColumn | null>(null);
  const [screenerRows, setScreenerRows] = useState<DiscoverResult | null>(null);
  const [screenerBusy, setScreenerBusy] = useState(false);

  // Keep the filter object out of the poll effect's dependency list by
  // reading it from a ref. Depending on it directly would tear down and
  // rebuild the interval on every keystroke.
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  // The settled copy of the filters, 350ms after the last edit — the same
  // debounce the board's filter effect uses. `filters` itself is a fresh object
  // per keystroke, so an effect that depends on it rebuilds its interval and
  // fires an immediate request for every character typed.
  const [filtersSettled, setFiltersSettled] = useState(filters);
  useEffect(() => {
    if (filters === filtersSettled) return;
    const h = window.setTimeout(() => setFiltersSettled(filters), 350);
    return () => window.clearTimeout(h);
  }, [filters, filtersSettled]);

  const loadWatch = useCallback(async () => {
    const r = await window.krypt.terminal.watchlist({ action: 'list' });
    setWatch(r.entries ?? []);
  }, []);

  useEffect(() => {
    let alive = true;
    let busy = false;
    async function tick() {
      if (busy) return;   // a stalled backend must not stack requests
      busy = true;
      try {
        const cols = DISCOVER_COLUMNS.map((c) => c.id);
        const out = await Promise.all(cols.map((column) =>
          window.krypt.terminal
            .discover({ column, limit: 25, filters: filtersRef.current })
            .catch(() => null)));
        if (!alive) return;
        const next = { ...cached };
        cols.forEach((c, i) => { if (out[i]) next[c] = out[i]!; });
        Object.assign(cached, next);
        setResults(next);
      } finally { busy = false; }
    }
    void tick();
    void loadWatch();
    const t = window.setInterval(tick, REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
  }, [loadWatch]);

  // Re-fetch when the filters change, without touching the timer.
  //
  // Skips the first run. Both effects used to fire on mount — the poll ran
  // tick() at t=0 and this timer fired at t=350ms, before the first responses
  // had populated the TTL cache — so opening Discover cost TEN requests
  // instead of five, every time.
  const firstFilterRun = useRef(true);
  useEffect(() => {
    if (firstFilterRun.current) { firstFilterRun.current = false; return; }
    let alive = true;
    const h = window.setTimeout(async () => {
      const cols = DISCOVER_COLUMNS.map((c) => c.id);
      const out = await Promise.all(cols.map((column) =>
        window.krypt.terminal.discover({ column, limit: 25, filters })
          .catch(() => null)));
      if (!alive) return;
      const next = { ...cached };
      cols.forEach((c, i) => { if (out[i]) next[c] = out[i]!; });
      Object.assign(cached, next);
      setResults(next);
    }, 350);
    return () => { alive = false; clearTimeout(h); };
  }, [filters]);

  // Debounced, sequenced search. Typing "trump" used to fire five requests and
  // paint whichever answered last, so a slow reply for "tr" could land on top
  // of the right rows. The `alive` flag drops every response but the newest.
  useEffect(() => {
    if (!query.trim()) { setSearchResult(null); setSearching(false); return; }
    let alive = true;
    setSearching(true);
    const h = window.setTimeout(async () => {
      try {
        const r = await window.krypt.terminal.search({ query, limit: 40 });
        if (alive) setSearchResult(r);
      } catch {
        /* keep the last good rows rather than blanking the list */
      } finally {
        if (alive) setSearching(false);
      }
    }, 220);
    return () => { alive = false; window.clearTimeout(h); };
  }, [query]);

  const open = useCallback((cid: string) => {
    selectMarket(cid);
    onNav('market');
  }, [onNav]);

  // The screener asks for the backend's full page rather than the 25 a column
  // shows, and keeps its own poll so the board's timer does not also pay for
  // it. Filters still apply — the expanded view is the same query, not a
  // different one, or the counts would stop agreeing with the column.
  useEffect(() => {
    if (!screener) { setScreenerRows(null); return; }
    let alive = true;
    let busy = false;
    async function tick() {
      if (busy) return;
      busy = true;
      setScreenerBusy(true);
      try {
        const r = await window.krypt.terminal.discover({
          column: screener!, limit: SCREENER_LIMIT, filters: filtersRef.current,
        });
        if (alive) setScreenerRows(r);
      } catch { /* keep the last good page rather than blanking the table */ }
      finally { busy = false; if (alive) setScreenerBusy(false); }
    }
    void tick();
    const t = window.setInterval(tick, REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
    // Deliberately NOT `filters`: the timer reads filtersRef, and the settled
    // copy is what re-runs this — once, 350ms after the last keystroke.
  }, [screener, filtersSettled]);

  const toggleWatch = useCallback(async (m: MarketSummary) => {
    const on = watch.some((w) => w.conditionId === m.conditionId);
    await window.krypt.terminal.watchlist({
      action: on ? 'remove' : 'add',
      conditionId: m.conditionId,
      title: m.title,
    });
    await loadWatch();
  }, [watch, loadWatch]);

  const watched = useMemo(
    () => new Set(watch.map((w) => w.conditionId)), [watch]);

  const activeFilterCount = Object.values(filters)
    .filter((v) => v !== null && v !== undefined && v !== '' &&
      !(Array.isArray(v) && v.length === 0)).length;

  return (
    <Page
      title="Discover"
      subtitle="What is happening right now, across every Polymarket US market — not just the ones an engine watched."
      actions={
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-krypt-dim" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search markets, or paste a condition id"
              className="w-72 rounded-lg border border-krypt-border bg-krypt-surface py-1.5 pl-8 pr-7 text-sm text-white placeholder:text-krypt-dim focus:border-krypt-purple/50 focus:outline-none"
            />
            {query && (
              <button
                onClick={() => { setQuery(''); setSearchResult(null); }}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-krypt-dim hover:text-white"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <button
            onClick={() => setShowFilters((v) => !v)}
            className={cls(
              'flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition-colors',
              showFilters || activeFilterCount
                ? 'border-krypt-purple/50 bg-krypt-purple/10 text-white'
                : 'border-krypt-border text-krypt-muted hover:text-white')}
          >
            <SlidersHorizontal className="h-3.5 w-3.5" />
            Filters{activeFilterCount ? ` (${activeFilterCount})` : ''}
          </button>
        </>
      }
    >
      {showFilters && (
        <FilterBar filters={filters} onChange={setFilters} />
      )}

      {searchResult ? (
        <Card header={
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-white">
              Search: “{query}”
            </span>
            <span className="text-xs text-krypt-dim">
              {searching ? 'searching…' : `${searchResult.rows.length} result(s)`}
            </span>
          </div>
        }>
          {searchResult.note && <div className="mb-3"><Note>{searchResult.note}</Note></div>}
          <div className="space-y-1">
            {searchResult.rows.map((m) => (
              <Row key={m.conditionId} m={m} onOpen={open}
                watched={watched.has(m.conditionId)} onWatch={toggleWatch} />
            ))}
          </div>
        </Card>
      ) : screener ? (
        <Screener
          column={screener}
          result={screenerRows}
          busy={screenerBusy}
          onBack={() => setScreener(null)}
          onOpen={open}
          watched={watched}
          onWatch={toggleWatch}
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2 2xl:grid-cols-3">
          {DISCOVER_COLUMNS.map((col) => (
            <Column
              key={col.id}
              label={col.label}
              hint={col.hint}
              result={results[col.id]}
              onOpen={open}
              watched={watched}
              onWatch={toggleWatch}
              onExpand={() => setScreener(col.id)}
            />
          ))}
        </div>
      )}
    </Page>
  );
}

function Column({
  label, hint, result, onOpen, watched, onWatch, onExpand,
}: {
  label: string; hint: string;
  result?: DiscoverResult;
  onOpen: (cid: string) => void;
  watched: Set<string>;
  onWatch: (m: MarketSummary) => void;
  onExpand: () => void;
}) {
  return (
    <Card
      className="flex max-h-[calc(100vh-16rem)] flex-col"
      header={
        <div className="flex items-baseline justify-between gap-2">
          {/* The whole heading is the affordance, not a stray icon beside it:
              the column title is the thing a user reaches for when they want
              "show me all of these". */}
          <button
            onClick={onExpand}
            title={`Open every ${label.toLowerCase()} market in a full table`}
            className="group/hd min-w-0 text-left"
          >
            <div className="flex items-center gap-1.5 text-sm font-medium text-white group-hover/hd:text-krypt-pink">
              {label}
              <Maximize2 className="h-3 w-3 shrink-0 text-krypt-dim opacity-0 transition-opacity group-hover/hd:opacity-100" />
            </div>
            <div className="text-[11px] text-krypt-dim">{hint}</div>
          </button>
          {result && (
            <div className="text-right text-[10px] text-krypt-dim">
              <div>{result.rows.length} shown</div>
              {/* A filter that silently drops rows it could not evaluate reads
                  as "there is nothing here" — which is a lie about the market
                  rather than about our data. So say it out loud. */}
              {result.skippedForUnknown > 0 && (
                <div title="These rows are missing the metric your filter tests, so the filter could not judge them. They are skipped, not excluded.">
                  {result.skippedForUnknown} skipped (no data)
                </div>
              )}
              {result.excluded > 0 && (
                <div title="Excluded by a categorical filter — you named a set, and these are not in it.">
                  {result.excluded} excluded
                </div>
              )}
            </div>
          )}
        </div>
      }
    >
      {!result ? (
        <div className="py-8 text-center text-sm text-krypt-dim">Loading…</div>
      ) : result.note && !result.rows.length ? (
        <Note>{result.note}</Note>
      ) : !result.rows.length ? (
        <Empty title="Nothing here" description="No markets matched." />
      ) : (
        <div className="-mx-1 min-h-0 flex-1 space-y-1 overflow-y-auto px-1">
          {result.rows.map((m) => (
            <Row key={m.conditionId} m={m} onOpen={onOpen}
              watched={watched.has(m.conditionId)} onWatch={onWatch} />
          ))}
        </div>
      )}
    </Card>
  );
}

function Row({
  m, onOpen, watched, onWatch,
}: {
  m: MarketSummary;
  onOpen: (cid: string) => void;
  watched: boolean;
  onWatch: (m: MarketSummary) => void;
}) {
  return (
    <div className="group rounded-lg px-2 py-2 transition-colors hover:bg-white/[0.03]">
      <div className="flex items-start gap-2">
        <button
          onClick={() => onWatch(m)}
          title={watched ? 'Remove from watchlist' : 'Add to watchlist'}
          className={cls('mt-0.5 shrink-0 transition-colors',
            watched ? 'text-krypt-warn' : 'text-krypt-dim opacity-0 hover:text-white group-hover:opacity-100')}
        >
          <Star className={cls('h-3.5 w-3.5', watched && 'fill-current')} />
        </button>
        <button
          onClick={() => onOpen(m.conditionId)}
          className="min-w-0 flex-1 text-left"
        >
          <div className="truncate text-sm text-white group-hover:text-krypt-pink">
            {m.title}
          </div>
          <div className="mt-1 flex items-center gap-3 text-[11px] text-krypt-dim">
            <span className="flex items-center gap-1">
              <Src stamp={m.sources.volume24h} />
              24h <Val v={m.volume24hUsd} fmt={usd} />
            </span>
            <span className="flex items-center gap-1">
              <Src stamp={m.sources.spread} />
              spread <Val v={m.spread} fmt={cents} />
            </span>
            <span className="flex items-center gap-1">
              <Src stamp={m.sources.closeTime} />
              <Val v={m.hoursToClose} fmt={hours} />
            </span>
            <Unknowns items={m.unknowns} compact />
          </div>
        </button>
        <div className="shrink-0 text-right">
          <div className="flex items-center justify-end gap-1 font-mono text-sm text-white">
            <Src stamp={m.sources.probability} />
            <Val v={m.probability} fmt={cents} />
          </div>
          <div className="mt-1 w-20"><ProbBar v={m.probability} /></div>
        </div>
      </div>
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Screener — one column, every row, as a dense sortable table
//
// The board answers "what should I look at?" in four short lists. This
// answers "show me all of them, ranked how I want" — the same query, more of
// it, with every metric side by side instead of three per row.
//
// The em-dash rule survives sorting, which is the only genuinely tricky part:
// an unknown NEVER climbs the table. Sorting descending by 24h volume must not
// put the markets whose volume nobody reported at the top just because `null`
// happens to compare high, and flipping to ascending must not put them at the
// top either. They sink to the bottom in BOTH directions, because "I do not
// know" is not a large value or a small one.
// ────────────────────────────────────────────────────────────────────────

type SortKey =
  | 'rank' | 'title' | 'probability' | 'priceChange1h' | 'priceChange24h'
  | 'volume24hUsd' | 'volumeTotalUsd' | 'liquidityUsd' | 'spread'
  | 'hoursToClose';

interface SortCol {
  key: SortKey;
  label: string;
  hint?: string;
  /** Right-aligned numerics, left-aligned text. */
  numeric: boolean;
  /** Ascending first for "smaller is more interesting" columns. */
  ascFirst?: boolean;
}

const SCREENER_COLS: SortCol[] = [
  { key: 'title', label: 'Market', numeric: false, ascFirst: true },
  { key: 'probability', label: 'Price', numeric: true, hint: 'YES probability, cents' },
  { key: 'priceChange1h', label: '1H', numeric: true, hint: 'Signed price move over the last hour' },
  { key: 'priceChange24h', label: '24H', numeric: true, hint: 'Signed price move over the last day' },
  { key: 'volume24hUsd', label: 'Vol 24h', numeric: true },
  { key: 'volumeTotalUsd', label: 'Vol total', numeric: true },
  { key: 'liquidityUsd', label: 'Liquidity', numeric: true },
  { key: 'spread', label: 'Spread', numeric: true, hint: 'Ask minus bid. Never a half-spread', ascFirst: true },
  { key: 'hoursToClose', label: 'Closes', numeric: true, ascFirst: true },
];

/** Signed cents, with an explicit + so a rise is unmistakable at a glance. */
const signedCents = (n: number) => `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(1)}¢`;

function changeClass(v: number | null): string {
  if (v === null || v === undefined) return 'text-krypt-dim';
  if (v > 0) return 'text-krypt-win';
  if (v < 0) return 'text-krypt-loss';
  return 'text-krypt-muted';
}

function Screener({
  column, result, busy, onBack, onOpen, watched, onWatch,
}: {
  column: DiscoverColumn;
  result: DiscoverResult | null;
  busy: boolean;
  onBack: () => void;
  onOpen: (cid: string) => void;
  watched: Set<string>;
  onWatch: (m: MarketSummary) => void;
}) {
  // 'rank' means "the order the backend ranked them in", which is what the
  // column actually MEANS — newest first, closing soonest, biggest mover. It
  // is the default because re-sorting by anything else discards that meaning,
  // and the user should opt into that rather than land in it.
  const [sort, setSort] = useState<{ key: SortKey; dir: 'asc' | 'desc' }>(
    { key: 'rank', dir: 'asc' });

  const meta = DISCOVER_COLUMNS.find((c) => c.id === column);
  const rows = result?.rows ?? [];

  const sorted = useMemo(() => {
    // Bound to a const so TypeScript narrows it inside the comparator; reading
    // `sort.key` there keeps the 'rank' case in the union and it is not a
    // MarketSummary field.
    const key = sort.key;
    if (key === 'rank') return rows;
    const dir = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      if (key === 'title') return a.title.localeCompare(b.title) * dir;
      // The volatility column ranks on the ABSOLUTE move; sorting the table by
      // that same column should keep meaning "biggest mover", not "highest
      // signed number", or clicking the header a user just arrived from would
      // silently change the question.
      const abs = column === 'volatility' && key === 'priceChange24h';
      const av = a[key] as number | null;
      const bv = b[key] as number | null;
      // Unknowns sink in BOTH directions. This is the em-dash rule applied to
      // ordering: null is not a big number and it is not a small one.
      if (av === null || av === undefined) return bv === null || bv === undefined ? 0 : 1;
      if (bv === null || bv === undefined) return -1;
      return ((abs ? Math.abs(av) : av) - (abs ? Math.abs(bv) : bv)) * dir;
    });
  }, [rows, sort, column]);

  const click = (c: SortCol) => {
    setSort((s) => s.key === c.key
      ? { key: c.key, dir: s.dir === 'asc' ? 'desc' : 'asc' }
      : { key: c.key, dir: c.ascFirst ? 'asc' : 'desc' });
  };

  return (
    <Card className="flex max-h-[calc(100vh-13rem)] flex-col">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <button
            onClick={onBack}
            className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-2.5 py-1 text-xs text-krypt-muted hover:text-white"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> All columns
          </button>
          <div>
            <div className="text-sm font-medium text-white">{meta?.label ?? column}</div>
            <div className="text-[11px] text-krypt-dim">{meta?.hint}</div>
          </div>
        </div>
        <div className="text-right text-[11px] text-krypt-dim">
          <div>
            {busy && !rows.length ? 'loading…' : `${sorted.length} market(s)`}
            {sort.key !== 'rank' && ' · re-sorted'}
          </div>
          {/* The counts mean different things and the column view already says
              so; repeat them here rather than dropping them, because this is
              the view people will screenshot. */}
          {!!result?.skippedForUnknown && (
            <div title="Missing the metric your filter tests, so the filter could not judge them. Skipped, not excluded.">
              {result.skippedForUnknown} skipped (no data)
            </div>
          )}
          {!!result?.excluded && (
            <div title="Excluded by a categorical filter — you named a set, and these are not in it.">
              {result.excluded} excluded
            </div>
          )}
        </div>
      </div>

      {result?.note && <div className="mb-3"><Note>{result.note}</Note></div>}

      {!result && !rows.length ? (
        <div className="py-10 text-center text-sm text-krypt-dim">Loading…</div>
      ) : !rows.length ? (
        <Empty title="Nothing here" description="No markets matched." />
      ) : (
        // Wide content scrolls inside its own box; the page never scrolls
        // sideways.
        <div className="-mx-2 min-h-0 flex-1 overflow-auto px-2">
          <table className="w-full border-collapse text-sm">
            <thead className="sticky top-0 z-10 bg-krypt-surface">
              <tr className="border-b border-krypt-border">
                <th className="w-8 px-1 py-2 text-left text-[10px] font-normal uppercase tracking-wider text-krypt-dim">
                  #
                </th>
                <th className="w-6 px-1 py-2" />
                {SCREENER_COLS.map((c) => {
                  const active = sort.key === c.key;
                  return (
                    <th
                      key={c.key}
                      className={cls('px-2 py-2 text-[10px] font-normal uppercase tracking-wider',
                        c.numeric ? 'text-right' : 'text-left')}
                    >
                      <button
                        onClick={() => click(c)}
                        title={c.hint}
                        className={cls('inline-flex items-center gap-1 hover:text-white',
                          active ? 'text-white' : 'text-krypt-dim')}
                      >
                        {c.label}
                        {active && (sort.dir === 'asc'
                          ? <ChevronUp className="h-3 w-3" />
                          : <ChevronDown className="h-3 w-3" />)}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {sorted.map((m, i) => (
                <tr
                  key={m.conditionId}
                  className="group border-b border-krypt-border/40 transition-colors hover:bg-white/[0.03]"
                >
                  <td className="px-1 py-1.5 text-right font-mono text-[11px] text-krypt-dim">
                    {i + 1}
                  </td>
                  <td className="px-1 py-1.5">
                    <button
                      onClick={() => onWatch(m)}
                      title={watched.has(m.conditionId) ? 'Remove from watchlist' : 'Add to watchlist'}
                      className={cls('transition-colors',
                        watched.has(m.conditionId)
                          ? 'text-krypt-warn'
                          : 'text-krypt-dim opacity-0 hover:text-white group-hover:opacity-100')}
                    >
                      <Star className={cls('h-3.5 w-3.5', watched.has(m.conditionId) && 'fill-current')} />
                    </button>
                  </td>
                  <td className="max-w-0 px-2 py-1.5">
                    <button
                      onClick={() => onOpen(m.conditionId)}
                      className="block w-full truncate text-left text-white hover:text-krypt-pink"
                      title={m.title}
                    >
                      {m.title}
                    </button>
                    <div className="flex items-center gap-2 text-[10px] text-krypt-dim">
                      {m.category && <span>{m.category}</span>}
                      <Unknowns items={m.unknowns} compact />
                    </div>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono">
                    <span className="inline-flex items-center gap-1">
                      <Src stamp={m.sources.probability} />
                      <Val v={m.probability} fmt={cents} />
                    </span>
                  </td>
                  <td className={cls('whitespace-nowrap px-2 py-1.5 text-right font-mono',
                    changeClass(m.priceChange1h))}
                  >
                    <Val v={m.priceChange1h} fmt={signedCents} className={changeClass(m.priceChange1h)} />
                  </td>
                  <td className={cls('whitespace-nowrap px-2 py-1.5 text-right font-mono',
                    changeClass(m.priceChange24h))}
                  >
                    <Val v={m.priceChange24h} fmt={signedCents} className={changeClass(m.priceChange24h)} />
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-krypt-muted">
                    <Val v={m.volume24hUsd} fmt={usd} />
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-krypt-muted">
                    <Val v={m.volumeTotalUsd} fmt={usd} />
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-krypt-muted">
                    <Val v={m.liquidityUsd} fmt={usd} />
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-krypt-muted">
                    <Val v={m.spread} fmt={cents} />
                  </td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-right font-mono text-krypt-muted">
                    <Val v={m.hoursToClose} fmt={hours} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Filters
//
// The two kinds behave differently ON PURPOSE, and the UI says so, because
// a user who does not know which rule applies cannot read the counts.
// ────────────────────────────────────────────────────────────────────────

const CATEGORIES = [
  'crypto', 'sports', 'politics', 'finance', 'tech',
  'economics', 'culture', 'geopolitics', 'weather', 'other',
];

function FilterBar({
  filters, onChange,
}: { filters: DiscoverFilters; onChange: (f: DiscoverFilters) => void }) {
  const setNum = (k: keyof DiscoverFilters) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const raw = e.target.value.trim();
    onChange({ ...filters, [k]: raw === '' ? null : Number(raw) });
  };
  const toggleCat = (c: string) => {
    const cur = filters.categories ?? [];
    onChange({
      ...filters,
      categories: cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c],
    });
  };

  return (
    <Card className="mb-4">
      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">
            Numeric — a row whose metric has not arrived is <em>skipped</em>
          </div>
          <p className="mb-3 text-[11px] text-krypt-dim">
            A threshold on a quantity nobody produced is not a judgement about
            that market, so it is counted as skipped rather than quietly hidden.
          </p>
          <div className="grid grid-cols-2 gap-2">
            <NumField label="Min 24h volume $" value={filters.minVolume24hUsd} onChange={setNum('minVolume24hUsd')} />
            <NumField label="Min liquidity $" value={filters.minLiquidityUsd} onChange={setNum('minLiquidityUsd')} />
            <NumField label="Min probability ¢" value={filters.minProbability} onChange={setNum('minProbability')} />
            <NumField label="Max probability ¢" value={filters.maxProbability} onChange={setNum('maxProbability')} />
            <NumField label="Min hours to close" value={filters.minHoursToClose} onChange={setNum('minHoursToClose')} />
            <NumField label="Max hours to close" value={filters.maxHoursToClose} onChange={setNum('maxHoursToClose')} />
          </div>
        </div>
        <div>
          <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">
            Category — a row we could not categorise is <em>excluded</em>
          </div>
          <p className="mb-3 text-[11px] text-krypt-dim">
            Here you are naming a set, and “we don’t know” is not a member of it.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {CATEGORIES.map((c) => {
              const on = (filters.categories ?? []).includes(c);
              return (
                <button
                  key={c}
                  onClick={() => toggleCat(c)}
                  className={cls(
                    'rounded-full border px-2.5 py-1 text-xs capitalize transition-colors',
                    on
                      ? 'border-krypt-purple/60 bg-krypt-purple/15 text-white'
                      : 'border-krypt-border text-krypt-muted hover:text-white')}
                >
                  {c}
                </button>
              );
            })}
          </div>
          <button
            onClick={() => onChange({})}
            className="mt-4 text-xs text-krypt-dim underline-offset-2 hover:text-white hover:underline"
          >
            Clear all filters
          </button>
        </div>
      </div>
    </Card>
  );
}

function NumField({
  label, value, onChange,
}: {
  label: string;
  value: number | null | undefined;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
}) {
  return (
    <label className="block">
      <span className="text-[11px] text-krypt-dim">{label}</span>
      <input
        type="number"
        value={value ?? ''}
        onChange={onChange}
        placeholder="any"
        className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1 font-mono text-sm text-white placeholder:text-krypt-dim focus:border-krypt-purple/50 focus:outline-none"
      />
    </label>
  );
}
