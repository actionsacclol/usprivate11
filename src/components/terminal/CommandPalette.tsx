import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ArrowRight, Compass, Loader2, Search, Star } from 'lucide-react';
import type { MarketSummary } from '@shared/market';
import type { PageId } from '../../App';
import { selectMarket } from '../../state/terminalNav';
import { cls } from '../../utils/format';
import { ProbBar, Val, cents } from './TerminalBits';

// ────────────────────────────────────────────────────────────────────────
// Ctrl/Cmd+K — jump to any market, or any screen, without reaching for the
// mouse. A terminal is a thing you use every day; the two-second cost of
// Discover -> scroll -> click is the difference between looking something up
// and not bothering.
// ────────────────────────────────────────────────────────────────────────

type Row =
  | { kind: 'page'; id: PageId; label: string; hint: string }
  | { kind: 'market'; market: MarketSummary };

const PAGES: { id: PageId; label: string; hint: string; keys: string }[] = [
  { id: 'discover', label: 'Discover', hint: 'What is happening', keys: 'discover markets new closing volume' },
  { id: 'market', label: 'Market', hint: 'The market you have open', keys: 'market chart book ticket' },
  { id: 'track', label: 'Track', hint: 'Positions, orders, fills, paper', keys: 'track positions pnl paper fills orders' },
  { id: 'strategies', label: 'Strategies', hint: 'Automation presets', keys: 'strategies presets' },
  { id: 'remote', label: 'Remote', hint: 'Discord / Telegram control from your phone', keys: 'remote phone discord telegram bot pair mobile' },
  { id: 'settings', label: 'Settings', hint: 'Preferences, privacy, paper mode', keys: 'settings privacy paper offline preferences' },
  { id: 'api', label: 'API Key', hint: 'Key and connection', keys: 'api keys credentials account' },
];

export function CommandPalette({
  open, onClose, onNav,
}: { open: boolean; onClose: () => void; onNav: (p: PageId) => void }) {
  const [q, setQ] = useState('');
  const [markets, setMarkets] = useState<MarketSummary[]>([]);
  const [searching, setSearching] = useState(false);
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setQ('');
    setMarkets([]);
    setCursor(0);
    // Focus after paint, or the input is not in the DOM yet.
    const t = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(t);
  }, [open]);

  // Debounced market search. Pages filter locally and instantly; markets cost
  // a request, so they wait for a pause in typing.
  useEffect(() => {
    if (!open || q.trim().length < 2) { setMarkets([]); return; }
    let alive = true;
    setSearching(true);
    const t = window.setTimeout(async () => {
      try {
        const r = await window.krypt.terminal.search({ query: q, limit: 12 });
        if (alive) setMarkets(r.rows ?? []);
      } catch {
        if (alive) setMarkets([]);
      } finally {
        if (alive) setSearching(false);
      }
    }, 220);
    return () => { alive = false; window.clearTimeout(t); };
  }, [q, open]);

  const rows = useMemo<Row[]>(() => {
    const needle = q.trim().toLowerCase();
    const pages = PAGES
      .filter((p) => !needle || `${p.label} ${p.keys}`.toLowerCase().includes(needle))
      .map<Row>((p) => ({ kind: 'page', id: p.id, label: p.label, hint: p.hint }));
    return [...pages, ...markets.map<Row>((m) => ({ kind: 'market', market: m }))];
  }, [q, markets]);

  useEffect(() => { setCursor(0); }, [rows.length]);

  const run = useCallback((row: Row) => {
    if (row.kind === 'page') {
      onNav(row.id);
    } else {
      selectMarket(row.market.conditionId);
      onNav('market');
    }
    onClose();
  }, [onNav, onClose]);

  const onKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setCursor((c) => Math.min(rows.length - 1, c + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const row = rows[cursor];
      if (row) run(row);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  }, [rows, cursor, run, onClose]);

  // Keep the highlighted row on screen when arrowing past the fold.
  useEffect(() => {
    const el = listRef.current?.querySelector(`[data-idx="${cursor}"]`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [cursor]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 pt-[12vh] backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="w-[min(640px,92vw)] overflow-hidden rounded-xl border border-krypt-borderHi bg-krypt-surface shadow-krypt-strong"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-krypt-border px-3 py-2.5">
          <Search className="h-4 w-4 shrink-0 text-krypt-dim" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Search markets, or jump to a screen…"
            className="w-full bg-transparent text-sm text-white placeholder:text-krypt-dim focus:outline-none"
          />
          {searching && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-krypt-dim" />}
          <kbd className="shrink-0 rounded border border-krypt-border px-1.5 py-0.5 text-[10px] text-krypt-dim">
            esc
          </kbd>
        </div>

        <div ref={listRef} className="max-h-[52vh] overflow-y-auto p-1.5">
          {!rows.length ? (
            <div className="px-3 py-6 text-center text-xs text-krypt-dim">
              {q.trim().length < 2
                ? 'Type to search. Markets need at least two characters.'
                : searching
                  ? 'Searching…'
                  : 'No match. Search covers the actively-traded book — paste a condition id for an exact market.'}
            </div>
          ) : rows.map((row, i) => (
            <button
              key={row.kind === 'page' ? `p-${row.id}` : `m-${row.market.conditionId}`}
              data-idx={i}
              onMouseEnter={() => setCursor(i)}
              onClick={() => run(row)}
              className={cls(
                'flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left',
                i === cursor ? 'bg-white/[0.07]' : 'hover:bg-white/[0.03]')}
            >
              {row.kind === 'page' ? (
                <>
                  <Compass className="h-3.5 w-3.5 shrink-0 text-krypt-purple" />
                  <span className="text-sm text-white">{row.label}</span>
                  <span className="truncate text-[11px] text-krypt-dim">{row.hint}</span>
                  <ArrowRight className="ml-auto h-3 w-3 shrink-0 text-krypt-dim" />
                </>
              ) : (
                <>
                  <Star className="h-3.5 w-3.5 shrink-0 text-krypt-dim" />
                  <span className="min-w-0 flex-1 truncate text-sm text-white">
                    {row.market.title}
                  </span>
                  <div className="w-16 shrink-0"><ProbBar v={row.market.probability} /></div>
                  <span className="w-12 shrink-0 text-right font-mono text-xs text-white">
                    <Val v={row.market.probability} fmt={cents} />
                  </span>
                </>
              )}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-3 border-t border-krypt-border px-3 py-1.5 text-[10px] text-krypt-dim">
          <span><kbd className="text-krypt-muted">↑↓</kbd> move</span>
          <span><kbd className="text-krypt-muted">↵</kbd> open</span>
          <span className="ml-auto">
            <kbd className="text-krypt-muted">g</kbd> then{' '}
            <kbd className="text-krypt-muted">d/m/t</kbd> jumps without opening this
          </span>
        </div>
      </div>
    </div>
  );
}

/**
 * App-wide keyboard shortcuts.
 *
 * Deliberately inert while the user is typing into a field — a terminal that
 * hijacks "d" mid-search is worse than one with no shortcuts at all.
 */
export function useTerminalHotkeys({
  onPalette, onNav,
}: { onPalette: () => void; onNav: (p: PageId) => void }): void {
  const pending = useRef<{ key: string; at: number } | null>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent): void => {
      const el = e.target as HTMLElement | null;
      // `e.target` is not always an Element — it can be `window` or
      // `document`, neither of which has `.closest`, and calling it there
      // throws INSIDE the listener and silently kills every shortcut. Guard
      // the call rather than assume the shape.
      const typing = !!el && (
        el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' ||
        el.isContentEditable === true ||
        (typeof el.closest === 'function' && el.closest('.cm-editor') !== null));

      // Ctrl/Cmd+K works even while typing — it is the escape hatch.
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        onPalette();
        return;
      }
      if (typing || e.ctrlKey || e.metaKey || e.altKey) return;

      // `g` then a letter — the vim-ish chord every terminal has.
      const now = Date.now();
      if (pending.current && now - pending.current.at < 900) {
        const target = ({
          d: 'discover', m: 'market', t: 'track',
          s: 'settings', w: 'api',
        } as Record<string, PageId>)[e.key.toLowerCase()];
        pending.current = null;
        if (target) { e.preventDefault(); onNav(target); }
        return;
      }
      if (e.key.toLowerCase() === 'g') {
        pending.current = { key: 'g', at: now };
        return;
      }
      if (e.key === '/') {
        e.preventDefault();
        onPalette();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onPalette, onNav]);
}
