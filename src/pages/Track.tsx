import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, RefreshCw, Radar, Trash2, X } from 'lucide-react';
import type {
  FlattenPreview, FlattenResult, PaperOrder, PaperPortfolio, RestingOrder,
  SettledOutcome, SettledRecord,
  StandingInstruction, TerminalFill, TerminalPortfolio,
} from '@shared/market';
import { ORIGIN_LABEL, STANDING_LABEL } from '@shared/market';
import type { TerminalOrderLogRow } from '@shared/types';
import { Card, Empty, Page, StatCard } from '../components/common';
import {
  Note, Src, Unknowns, Val, cents, signedUsd, usdExact,
} from '../components/terminal/TerminalBits';
import { useApp } from '../state/AppStateProvider';
// usdExact, not usd(): every dollar figure on this page is a ledger figure the
// user reconciles against Polymarket. usd()'s K-rounding turned $1,249.30 of
// cash into "$1.2K", which does not reconcile with anything.
import { selectMarket } from '../state/terminalNav';
import { useToast } from '../state/ToastProvider';
import { cls } from '../utils/format';

type Tab = 'positions' | 'orders' | 'standing' | 'fills' | 'log' | 'paper';

/** Green/red only for a P&L we actually have. Unknown is neutral. */
const pnlAccent = (v: number | null | undefined): 'good' | 'bad' | 'neutral' =>
  (v === null || v === undefined) ? 'neutral' : (v >= 0 ? 'good' : 'bad');

const TABS: { id: Tab; label: string }[] = [
  { id: 'positions', label: 'Positions' },
  { id: 'orders', label: 'Resting orders' },
  { id: 'standing', label: 'Standing instructions' },
  { id: 'fills', label: 'Fill ledger' },
  { id: 'log', label: 'Order log' },
  { id: 'paper', label: 'Paper' },
];

export function TrackPage({ onNav }: { onNav: (p: 'market') => void }) {
  const toast = useToast();
  const [tab, setTab] = useState<Tab>('positions');
  const [pf, setPf] = useState<TerminalPortfolio | null>(null);
  const [orders, setOrders] = useState<RestingOrder[]>([]);
  const [standing, setStanding] = useState<StandingInstruction[]>([]);
  const [fills, setFills] = useState<TerminalFill[]>([]);
  const [log, setLog] = useState<TerminalOrderLogRow[]>([]);
  const [paper, setPaper] = useState<PaperPortfolio | null>(null);
  const [paperOrders, setPaperOrders] = useState<PaperOrder[]>([]);
  const [reconciling, setReconciling] = useState(false);

  // Only what the screen is actually showing. The portfolio feeds the header
  // cards on every tab and the standing list feeds the tab-bar badge, so those
  // two are always fetched; the other five belong to exactly one tab each, and
  // polling all seven every 12s was five requests a second nobody was reading.
  const refresh = useCallback(async () => {
    const on = (t: Tab) => tab === t;
    const [p, s, o, f, l, pp, po] = await Promise.all([
      window.krypt.terminal.portfolio().catch(() => null),
      window.krypt.terminal.standing({ action: 'list' }).catch(() => null),
      on('orders') ? window.krypt.terminal.orders().catch(() => null) : null,
      on('fills') ? window.krypt.terminal.fills(200).catch(() => null) : null,
      on('log') ? window.krypt.terminal.orderLog(200).catch(() => null) : null,
      on('paper') ? window.krypt.terminal.paperPortfolio().catch(() => null) : null,
      on('paper') ? window.krypt.terminal.paperOrders(200).catch(() => null) : null,
    ]);
    if (p) setPf(p);
    if (s) setStanding(s.instructions ?? []);
    if (o) setOrders(o.orders ?? []);
    if (f) setFills(f.fills ?? []);
    if (l) setLog(l.orders ?? []);
    if (pp) setPaper(pp);
    if (po) setPaperOrders(po.orders ?? []);
  }, [tab]);

  // `refresh` changes identity with the tab, so switching tabs restarts this
  // timer — which fetches that tab's data immediately, then keeps the 12s
  // cadence.
  useEffect(() => {
    let alive = true;
    let busy = false;
    async function tick() {
      if (busy) return;
      busy = true;
      try { if (alive) await refresh(); } finally { busy = false; }
    }
    void tick();
    const id = window.setInterval(tick, 12_000);
    return () => { alive = false; window.clearInterval(id); };
  }, [refresh]);

  // A standing instruction that fired — or was blocked from firing — must
  // reach the user. A silent counter is not a safety mechanism.
  useEffect(() => {
    const off = window.krypt.terminal.onStanding((e) => {
      if (e.blocked) {
        // The backend says WHY it held fire (paper mode, unreadable account,
        // a refused order) — repeat that rather than guess a reason.
        toast.error(
          `${STANDING_LABEL[e.kind as keyof typeof STANDING_LABEL] ?? e.kind} on ` +
          `"${e.title ?? e.conditionId.slice(0, 10)}" hit ${e.priceCents}¢ — ` +
          `${e.result ?? 'nothing was sold.'}`);
      } else {
        toast.info(
          `${STANDING_LABEL[e.kind as keyof typeof STANDING_LABEL] ?? e.kind} fired at ` +
          `${e.priceCents}¢: ${e.result ?? 'alert'}`);
      }
      void refresh();
    });
    return off;
  }, [toast, refresh]);

  const reconcile = useCallback(async () => {
    setReconciling(true);
    try {
      const r = await window.krypt.terminal.reconcile();
      // An incomplete walk is NOT a success. Saying "reconciled" over a
      // truncated pass is how old positions stay basis-less while the screen
      // looks finished.
      if (r.note) toast.info(r.note);
      else toast.success(
        `Reconciled ${r.scanned ?? 0} fill(s) from Polymarket US's ledger: ` +
        `${r.inserted} new, ${r.matched} order(s) matched` +
        (r.corrected ? `, ${r.corrected} re-filed under the right outcome` : '') +
        '.');
      await refresh();
    } finally { setReconciling(false); }
  }, [toast, refresh]);

  const open = (cid: string) => { selectMarket(cid); onNav('market'); };

  return (
    <Page
      title="Track"
      subtitle="What you actually did, priced from Polymarket US's own ledger — not from what we asked for."
      actions={
        <button
          onClick={reconcile}
          disabled={reconciling}
          className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-sm text-krypt-muted hover:text-white disabled:opacity-50"
        >
          <RefreshCw className={cls('h-3.5 w-3.5', reconciling && 'animate-spin')} />
          Reconcile fills
        </button>
      }
    >
      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
        <StatCard label="Cash" value={<Val v={pf?.cashUsd ?? null} fmt={usdExact} />} />
        <StatCard label="Position value" value={<Val v={pf?.totalValueUsd ?? null} fmt={usdExact} />}
          hint="reconciled rows only" />
        <StatCard label="Cost basis" value={<Val v={pf?.totalCostUsd ?? null} fmt={usdExact} />}
          hint="average cost, not FIFO" />
        {/* `?? 0` in the accent test would tint an UNKNOWN P&L green — a
            confident visual claim about a number we do not have, which is the
            same lie as printing $0.00. Unknown stays neutral. */}
        <StatCard
          label="Unrealised"
          value={<Val v={pf?.totalUnrealizedPnlUsd ?? null} fmt={signedUsd} />}
          accent={pnlAccent(pf?.totalUnrealizedPnlUsd)}
          hint="open positions only"
        />
        {/* NET of fees. `realizedPnlUsd` is gross — the same convention
            Polymarket US's own P&L uses — but fees are charged on the way in AND
            the way out, so the gross figure is not what reached the balance.
            The hint shows both halves rather than hiding either. One fill
            with an unknown fee makes the net unknown; the gross then shown is
            LABELLED gross — under the plain label it once turned the owner's
            $64 loss into an $11 gain. */}
        <StatCard
          label={pf?.realizedPnlNetUsd == null && pf?.realizedPnlUsd != null
            ? 'Realised, before fees' : 'Realised'}
          value={<Val
            v={pf?.realizedPnlNetUsd ?? pf?.realizedPnlUsd ?? null}
            fmt={signedUsd}
          />}
          accent={pnlAccent(pf?.realizedPnlNetUsd ?? pf?.realizedPnlUsd)}
          hint={
            pf?.realizedPnlNetUsd != null && pf?.realizedFeesUsd != null
              && pf?.realizedPnlUsd != null
              ? `${signedUsd(pf.realizedPnlUsd)} before ${usdExact(pf.realizedFeesUsd)} in fees`
              : pf?.realizedPnlNetUsd == null && pf?.realizedPnlUsd != null
                ? 'fees not deducted: some fills have an unknown fee'
                : pf?.settledPnlUsd != null
                  ? `${signedUsd(pf.settledPnlUsd)} of it from settlement`
                  : 'sells and settlements'
          }
        />
      </div>

      {/* The record — from the FILL LEDGER, not current holdings. */}
      {pf?.wonMarkets != null && pf?.lostMarkets != null
        && (pf.wonMarkets > 0 || pf.lostMarkets > 0) && (
        <div className="mb-4 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs text-krypt-muted">
          <span className="font-mono text-krypt-win">{pf.wonMarkets} won</span>
          <span className="text-krypt-dim">/</span>
          <span className="font-mono text-krypt-loss">{pf.lostMarkets} lost</span>
          {pf.recordNote && (
            <span className="text-[11px] text-krypt-dim">{pf.recordNote}</span>
          )}
        </div>
      )}


      {/* Polymarket's own number, next to ours. Two independently computed
          figures that agree are worth more than one you are asked to trust —
          and when they disagree the user gets to see it rather than find out
          from their bank balance. */}
      {/* Compared against `walletPnlUsd`, NOT the realised total: the venue's
          `cashPnl` covers only the positions still held, and the
          realised figure now spans the whole ledger. Comparing those two
          would report a wild disagreement between two correct numbers. */}
      {pf?.venueCashPnlUsd != null && pf.walletPnlUsd != null && (
        <p className="mb-4 text-[11px] text-krypt-dim">
          Polymarket US’s own P&amp;L across the positions still held is{' '}
          <span className="font-mono text-krypt-muted">
            {signedUsd(pf.venueCashPnlUsd)}
          </span>
          {Math.abs(pf.venueCashPnlUsd - pf.walletPnlUsd) <= 1
            ? ' — ours agrees. The small gap is entry fees, which their average price excludes.'
            : `, against ${signedUsd(pf.walletPnlUsd)} from our ledger. `
              + 'They disagree, so treat neither as settled until a reconcile closes the gap.'}
          {' '}That is what you hold now, not your record — the Realised figure above covers
          every market you have fills for.
        </p>
      )}

      {/* The warning strip. Rows without a venue-confirmed basis are shown but
          left OUT of every figure above — counted here rather than folded in
          at a guessed price. */}
      {pf?.unreconciledNote && (
        <div className="mb-4">
          <Note tone="warn">{pf.unreconciledNote}</Note>
        </div>
      )}

      <div className="mb-4 flex flex-wrap gap-1 border-b border-krypt-border">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cls('-mb-px border-b-2 px-3 py-2 text-sm transition-colors',
              tab === t.id
                ? 'border-krypt-purple text-white'
                : 'border-transparent text-krypt-muted hover:text-white')}
          >
            {t.label}
            {t.id === 'standing' && standing.filter((s) => s.armed && !s.firedAt).length > 0 && (
              <span className="ml-1.5 rounded-full bg-krypt-purple/25 px-1.5 py-0.5 text-[10px] text-white">
                {standing.filter((s) => s.armed && !s.firedAt).length}
              </span>
            )}
          </button>
        ))}
      </div>

      {tab === 'positions' && <Positions pf={pf} onOpen={open} onRefresh={refresh} />}
      {tab === 'orders' && <Orders orders={orders} onOpen={open} onRefresh={refresh} />}
      {tab === 'standing' && <Standing items={standing} onOpen={open} onRefresh={refresh} />}
      {tab === 'fills' && <Fills fills={fills} onOpen={open} />}
      {tab === 'log' && <OrderLog rows={log} onOpen={open} />}
      {tab === 'paper' && (
        <Paper pf={paper} orders={paperOrders} onOpen={open} onRefresh={refresh} />
      )}
    </Page>
  );
}

/**
 * Sell everything you hold, from one control.
 *
 * Three things make it safe enough to sit on this page. It PREVIEWS first —
 * the panel lists every position and what its exit is estimated to fetch, off
 * the same walk the backend will use. It is DISABLED with the reason written
 * out whenever it could not work (paper mode, live trading off, nothing to
 * sell), rather than failing after the click. And it reports PER POSITION,
 * because "flattened" over an account where three exits bounced is the exact
 * lie this screen exists to prevent.
 */
function FlattenAll({
  pf, onRefresh,
}: { pf: TerminalPortfolio; onRefresh: () => Promise<void> }) {
  const { config } = useApp();
  const [preview, setPreview] = useState<FlattenPreview | null>(null);
  const [result, setResult] = useState<FlattenResult | null>(null);
  const [busy, setBusy] = useState(false);

  const paper = !!config?.terminalPaperMode;
  // A SETTLED position is not sellable: a resolved market is closed on the
  // exchange, so there is no book to sell into.
  const openCount = pf.positions.filter((p) => !p.settled).length;
  const disabledReason = paper
    ? 'Paper mode is on — nothing here would reach the exchange. Turn it off '
      + 'in Settings to flatten for real.'
    : openCount
      ? null
      : pf.positions.length
        ? 'Nothing to sell — every position is settled. A resolved market is '
          + 'closed on the exchange, so there is no book to sell into.'
        : 'Nothing to sell — the account holds no open positions.';

  // Titles come from the portfolio the user is already looking at, so the
  // panel names the same markets the table above does.
  const titleFor = (cid: string, side: string) =>
    pf.positions.find((p) => p.conditionId === cid && p.side === side)?.title
    ?? `${cid.slice(0, 16)}…`;

  const startPreview = useCallback(async () => {
    setBusy(true);
    setResult(null);
    try {
      setPreview(await window.krypt.terminal.flattenPreview());
    } finally { setBusy(false); }
  }, []);

  const run = useCallback(async () => {
    setBusy(true);
    try {
      const r = await window.krypt.terminal.flattenWallet();
      setResult(r);
      setPreview(null);
      await onRefresh();
    } finally { setBusy(false); }
  }, [onRefresh]);

  return (
    <div className="mb-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11px] text-krypt-dim">
          Cost basis is <strong>average cost</strong>, not FIFO — that is what a
          trader means by “I am up 2x on this bag”. Every fill on this account
          counts towards it, whatever placed it. Where our ledger cannot reach
          back far enough the average is Polymarket US’s own for this account, and
          the dot beside it says so; only when neither can answer is the P&amp;L
          unknown — and then it stays unknown, not zero.
        </p>
        <button
          onClick={startPreview}
          disabled={busy || !!disabledReason || !!preview}
          title={disabledReason ?? 'Preview selling every position you hold.'}
          className={cls(
            'shrink-0 rounded-lg border px-3 py-1.5 text-sm transition-colors',
            disabledReason
              ? 'cursor-not-allowed border-krypt-border text-krypt-dim'
              : 'border-krypt-loss/50 text-krypt-loss hover:bg-krypt-loss/10')}
        >
          Flatten all
        </button>
      </div>

      {/* The reason is written out, not just hovered. A greyed button with no
          explanation is a dead end. */}
      {disabledReason && pf.positions.length > 0 && (
        <div className="mt-2"><Note>{disabledReason}</Note></div>
      )}

      {preview && (
        <div className="mt-3 space-y-2 rounded-lg border border-krypt-loss/40 bg-krypt-loss/[0.06] p-3">
          <div className="text-sm font-medium text-white">
            Sell {preview.count} position{preview.count === 1 ? '' : 's'} at the
            current best bid?
          </div>
          {!preview.ok ? (
            <Note tone="warn">{preview.message}</Note>
          ) : (
            <>
              {/* One sentence for the whole settled pile, never one refusal
                  row each. They are not in the table above and they never
                  should have been. */}
              {!!preview.settledSkipped && (
                <Note>
                  <strong>{preview.settledSkipped}</strong> settled position
                  {preview.settledSkipped === 1 ? ' is' : 's are'} not listed:
                  a resolved market is closed on the exchange, so there is no
                  book to sell into. They are in the settled record below the
                  positions table.
                </Note>
              )}
              <div className="text-xs text-krypt-muted">
                Estimated proceeds{' '}
                <span className="font-mono text-white">
                  <Val v={preview.proceedsUsd} fmt={usdExact} />
                </span>{' '}
                — an estimate off the book as it is now, not a quote. Each sell
                is a fill-and-kill: whatever does not cross is killed, not
                rested.
              </div>
              <div className="max-h-64 overflow-y-auto rounded border border-krypt-border/60">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                      <th className="p-1.5">Market</th>
                      <th className="p-1.5">Side</th>
                      <th className="p-1.5 text-right">Size</th>
                      <th className="p-1.5 text-right">Bid</th>
                      <th className="p-1.5 text-right">Est. proceeds</th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.positions.map((p) => (
                      <tr key={`${p.conditionId}-${p.side}`}
                        className={cls('border-b border-krypt-border/40 last:border-0',
                          p.blocked && 'bg-krypt-warn/[0.06]')}
                      >
                        <td className="max-w-[18rem] p-1.5">
                          <div className="truncate text-white">
                            {titleFor(p.conditionId, p.side)}
                          </div>
                          {p.blocked && (
                            <div className="text-[10px] text-krypt-warn">
                              Will NOT be sold: {p.blocked}
                            </div>
                          )}
                          {p.partial && (
                            <div className="text-[10px] text-krypt-warn">{p.partial}</div>
                          )}
                          {p.engineTracked && (
                            <div className="text-[10px] text-krypt-dim">
                              An engine is tracking this one; its reconcile will
                              book the close.
                            </div>
                          )}
                        </td>
                        <td className={cls('p-1.5 font-medium',
                          p.side === 'yes' ? 'text-krypt-win' : 'text-krypt-loss')}
                        >
                          {p.side.toUpperCase()}
                        </td>
                        <td className="p-1.5 text-right font-mono text-krypt-muted">
                          {p.size.toLocaleString()}
                        </td>
                        <td className="p-1.5 text-right font-mono text-krypt-muted">
                          <Val v={p.bidCents} fmt={cents} />
                        </td>
                        <td className="p-1.5 text-right font-mono text-white">
                          <Val v={p.proceedsUsd} fmt={usdExact} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {preview.engineTracked === null && (
                <Note>
                  We could not read the engines’ own position tables, so how
                  many of these an engine is tracking is unknown — not none.
                </Note>
              )}
            </>
          )}
          <div className="flex gap-2">
            <button
              onClick={() => setPreview(null)}
              className="flex-1 rounded-lg border border-krypt-border py-2 text-sm text-krypt-muted hover:text-white"
            >
              Cancel
            </button>
            <button
              onClick={run}
              disabled={busy || !preview.ok || preview.count === 0}
              className="flex-1 rounded-lg bg-krypt-loss/25 py-2 text-sm font-medium text-white ring-1 ring-krypt-loss/60 hover:bg-krypt-loss/35 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy ? 'Selling…' : `Sell ${preview.count} position${preview.count === 1 ? '' : 's'}`}
            </button>
          </div>
        </div>
      )}

      {result && (
        <div className="mt-3 space-y-2 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3">
          <Note tone={result.ok ? 'info' : 'warn'}>{result.message}</Note>
          {result.results.map((r) => (
            <div key={`${r.conditionId}-${r.side}`}
              className="flex gap-2 text-[11px] leading-snug"
            >
              <span className={cls('shrink-0 font-medium',
                r.ok ? 'text-krypt-win' : 'text-krypt-warn')}
              >
                {r.ok ? 'sold' : 'not sold'}
              </span>
              <span className="text-krypt-muted">
                <span className="text-white">{titleFor(r.conditionId, r.side)}</span>{' '}
                {r.side.toUpperCase()} {r.size.toLocaleString()} — {r.message}
              </span>
            </div>
          ))}
          <button
            onClick={() => setResult(null)}
            className="text-[11px] text-krypt-dim underline underline-offset-2 hover:text-white"
          >
            Dismiss
          </button>
        </div>
      )}
    </div>
  );
}

const OUTCOME_LABEL: Record<SettledOutcome, string> = {
  won: 'Won',
  lost: 'Lost',
  closedOut: 'Closed out',
  unknown: 'Unknown',
};

const OUTCOME_TONE: Record<SettledOutcome, string> = {
  won: 'text-krypt-win',
  lost: 'text-krypt-loss',
  closedOut: 'text-krypt-muted',
  unknown: 'text-krypt-warn',
};

/** A close time, or an em dash. Never a date we made up. */
const settledDay = (iso: string | null) => {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toLocaleDateString(undefined, { year: '2-digit', month: 'short', day: 'numeric' });
};

/**
 * The RECORD — every market the fill ledger ever settled.
 *
 * The Positions table is what the account holds now, and a settled position
 * leaves it. So the record comes from the fill ledger instead.
 */
function SettledRecordTable({ onOpen }: { onOpen: (cid: string) => void }) {
  const [data, setData] = useState<SettledRecord | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<SettledOutcome | 'all'>('all');

  useEffect(() => {
    let live = true;
    setErr(null);
    window.krypt.terminal.settled()
      .then((r) => { if (live) setData(r); })
      .catch((e: unknown) => {
        if (live) setErr(`Could not read your settled record: ${String(e)}`);
      });
    return () => { live = false; };
  }, []);

  if (err) return <div className="mt-3"><Note tone="warn">{err}</Note></div>;
  if (!data) {
    return <div className="py-6 text-center text-sm text-krypt-dim">Reading your fill ledger…</div>;
  }

  const t = data.totals;
  const counts: Record<SettledOutcome | 'all', number> = {
    all: data.rows.length,
    won: data.rows.filter((r) => r.outcome === 'won').length,
    lost: data.rows.filter((r) => r.outcome === 'lost').length,
    closedOut: data.rows.filter((r) => r.outcome === 'closedOut').length,
    unknown: data.rows.filter((r) => r.outcome === 'unknown').length,
  };
  const rows = filter === 'all' ? data.rows : data.rows.filter((r) => r.outcome === filter);
  const chips: (SettledOutcome | 'all')[] = ['all', 'won', 'lost', 'closedOut', 'unknown'];

  return (
    <div className="mt-3 space-y-2">
      <Note>
        This is your <strong>record</strong>, counted from the fill ledger.
        “Positions” above is what the account holds now, so that table can never
        be a win rate. {data.note}
      </Note>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <span className="text-krypt-muted">
          <strong className="text-krypt-win">{t.won}</strong> won ·{' '}
          <strong className="text-krypt-loss">{t.lost}</strong> lost ·{' '}
          <strong className="text-white">{t.closedOut}</strong> closed out
          {t.unknown > 0 && (
            <> · <strong className="text-krypt-warn">{t.unknown}</strong> unknown</>
          )}
          {t.bothSides > 0 && (
            <> · <strong className="text-white">{t.bothSides}</strong> held both sides</>
          )}
        </span>
        <span className="text-krypt-muted">
          Net{' '}
          <span className={cls('font-mono',
            t.pnlUsd === null ? 'text-krypt-dim'
              : t.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}
          >
            <Val v={t.pnlUsd} fmt={signedUsd} />
          </span>
          {t.feesUsd !== null && (
            <span className="text-krypt-dim"> after <Val v={t.feesUsd} fmt={usdExact} /> of fees</span>
          )}
          {t.pnlMissingRows > 0 && (
            <span className="text-krypt-dim">
              {' '}— {t.pnlMissingRows} row(s) could not be valued and are left out
            </span>
          )}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {chips.map((c) => (
          <button
            key={c}
            onClick={() => setFilter(c)}
            className={cls(
              'rounded-full border px-2.5 py-1 text-[11px] transition-colors',
              filter === c
                ? 'border-krypt-accent/60 bg-krypt-accent/10 text-white'
                : 'border-krypt-border text-krypt-muted hover:text-white')}
          >
            {c === 'all' ? 'All' : OUTCOME_LABEL[c]} ({counts[c]})
          </button>
        ))}
      </div>


      {rows.length === 0 ? (
        <div className="py-6 text-center text-sm text-krypt-dim">
          Nothing in this category.
        </div>
      ) : (
        <div className="max-h-[32rem] overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-krypt-bg">
              <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                <th className="pb-2">Market</th>
                <th className="pb-2">Side</th>
                <th className="pb-2">Outcome</th>
                <th className="pb-2 text-right" title="Contracts still outstanding when it settled.">Contracts</th>
                <th className="pb-2 text-right">Avg cost</th>
                <th className="pb-2 text-right">Cost</th>
                <th className="pb-2 text-right" title="$1 a contract on the winning outcome, $0 on the loser.">Payout</th>
                <th className="pb-2 text-right" title="Net of the fees the ledger knows about.">P&amp;L</th>
                <th className="pb-2 text-right">Settled</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.conditionId}-${r.side}`}
                  className="border-b border-krypt-border/40 last:border-0"
                >
                  <td className="max-w-[20rem] py-2 pr-2">
                    <button
                      onClick={() => onOpen(r.conditionId)}
                      className="truncate text-left text-white hover:text-krypt-accent"
                      title={r.title ?? r.conditionId}
                    >
                      {r.title ?? `${r.conditionId.slice(0, 16)}…`}
                    </button>
                    <div className="flex flex-wrap gap-1.5 text-[10px] text-krypt-dim">
                      {r.bothSides && (
                        <span className="text-krypt-warn"
                          title="Both sides were held to settlement, so this market appears once per side — won on one, lost on the other. It is counted in both columns above."
                        >
                          both sides
                        </span>
                      )}
                      {r.resolvedOutcomeLabel && (
                        <span>resolved {r.resolvedOutcomeLabel}</span>
                      )}
                    </div>
                  </td>
                  <td className={cls('py-2 font-medium',
                    r.side === 'yes' ? 'text-krypt-win' : 'text-krypt-loss')}
                  >
                    {r.sideLabel ?? r.side.toUpperCase()}
                  </td>
                  <td className={cls('py-2 text-xs', OUTCOME_TONE[r.outcome])}>
                    {OUTCOME_LABEL[r.outcome]}
                  </td>
                  <td className="py-2 text-right font-mono text-krypt-muted">
                    {r.contracts.toLocaleString()}
                  </td>
                  <td className="py-2 text-right font-mono text-krypt-muted">
                    <Val v={r.avgCostCents} fmt={cents} />
                  </td>
                  <td className="py-2 text-right font-mono text-krypt-muted">
                    {usdExact(r.costUsd)}
                  </td>
                  <td className="py-2 text-right font-mono text-krypt-muted">
                    <Val v={r.payoutUsd} fmt={usdExact} />
                  </td>
                  <td className={cls('py-2 text-right font-mono',
                    r.pnlUsd === null ? 'text-krypt-dim'
                      : r.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}
                    title={r.feesKnown
                      ? 'Net of this market’s fees.'
                      : 'Some fills here have no recorded fee, so a net figure would be a guess.'}
                  >
                    <Val v={r.pnlUsd} fmt={signedUsd} />
                  </td>
                  <td className="py-2 text-right text-xs text-krypt-dim">
                    {settledDay(r.settledAt)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Positions({
  pf, onOpen, onRefresh,
}: {
  pf: TerminalPortfolio | null;
  onOpen: (cid: string) => void;
  onRefresh: () => Promise<void>;
}) {
  // Hooks first — the early returns below must not skip them.
  const [showRecord, setShowRecord] = useState(false);
  if (!pf) return <div className="py-8 text-center text-sm text-krypt-dim">Loading…</div>;
  // No holdings is not an empty record, so the settled table stays reachable.
  if (!pf.positions.length && !pf.ledgerMarkets) {
    return <Empty title="No open positions"
      description="Positions you hold on Polymarket US appear here, whether or not this app opened them." />;
  }
  // This table is what is OPEN now; the record lives in its own table below,
  // read from the fill ledger.
  const visible = pf.positions.filter((p) => !p.settled);
  const settledDust = pf.positions.length - visible.length;
  // Markets the ledger has actually settled = every market it covers, minus
  // the ones whose outcome we could not read. Those are never counted as
  // losses, and they are not counted as settled either.
  const recordN = pf.ledgerMarkets == null
    ? null
    : pf.ledgerMarkets - (pf.unknownResolutionMarkets ?? 0);

  return (
    <Card>
      <FlattenAll pf={pf} onRefresh={onRefresh} />
      {(recordN !== null || settledDust > 0) && (
        <div className="mb-3 flex items-start justify-between gap-3">
          <Note>
            This table is what you hold now, so it is not a win rate
            {settledDust > 0 && <> ({settledDust} settled row{settledDust === 1 ? ' is' : 's are'} hidden here)</>}.
            What you actually won is the <strong>record</strong>, counted from
            your fill ledger.
          </Note>
          <button
            onClick={() => setShowRecord((v) => !v)}
            className="shrink-0 rounded-md border border-krypt-border px-2 py-1 text-xs text-krypt-muted hover:text-white"
          >
            {showRecord
              ? 'Hide settled'
              : `Show settled${recordN === null ? '' : ` (${recordN})`}`}
          </button>
        </div>
      )}
      {visible.length === 0 && (
        <div className="py-6 text-center text-sm text-krypt-dim">
          {pf.positions.length
            ? 'No open positions. Everything here is settled.'
            : 'No open positions.'}
        </div>
      )}
      <table className={cls('w-full text-sm', visible.length === 0 && 'hidden')}>
        <thead>
          <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
            <th className="pb-2">Market</th>
            <th className="pb-2">Side</th>
            <th className="pb-2 text-right">Size</th>
            <th className="pb-2 text-right">Avg cost</th>
            <th className="pb-2 text-right">Mark</th>
            <th className="pb-2 text-right">Value</th>
            <th className="pb-2 text-right" title="Unrealised for an open position; realised once the market settles.">P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((p) => (
            <tr
              key={`${p.conditionId}-${p.side}`}
              className={cls('border-b border-krypt-border/50 last:border-0',
                p.unreconciled && 'bg-krypt-warn/[0.04]')}
            >
              <td className="max-w-[24rem] py-2">
                <button
                  onClick={() => onOpen(p.conditionId)}
                  className="truncate text-left text-white hover:text-krypt-pink"
                >
                  {p.title ?? p.conditionId.slice(0, 18)}
                </button>
                <div className="mt-0.5 flex flex-wrap items-center gap-2">
                  {p.unreconciled && (
                    <span className="flex items-center gap-1 text-[10px] uppercase tracking-wider text-krypt-warn">
                      <AlertTriangle className="h-3 w-3" /> no basis
                    </span>
                  )}
                  {/* `resolvedOutcome === p.side` is false for BOTH a loss and
                      an unread outcome, so a null used to render as "settled
                      lost" — a confident claim we cannot make. Unknown gets its
                      own neutral badge. */}
                  {p.settled && (
                    <span
                      className={cls(
                        'flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wider',
                        p.resolvedOutcome === null || p.resolvedOutcome === undefined
                          ? 'border-krypt-border text-krypt-dim'
                          : p.resolvedOutcome === p.side
                            ? 'border-krypt-win/40 text-krypt-win'
                            : 'border-krypt-loss/40 text-krypt-loss')}
                      title={
                        p.resolvedOutcome
                          ? `This market settled ${p.resolvedOutcomeLabel ?? p.resolvedOutcome.toUpperCase()}. `
                            + 'The value shown is exact, not a mark — a binary contract '
                            + 'is worth exactly $1 or exactly $0 once the outcome is posted. '
                            + 'Won or lost is decided by the outcome INDEX, not by the word, '
                            + 'so it reads the same on a Yes/No, an Up/Down and a named market.'
                          : 'This market has settled, but the winning outcome could not be '
                            + 'read — so whether this side won or lost is unknown, not lost.'}
                    >
                      <CheckCircle2 className="h-3 w-3" />
                      {p.resolvedOutcome
                        ? `settled ${p.resolvedOutcome === p.side ? 'won' : 'lost'}`
                        : 'settled · outcome unknown'}
                    </span>
                  )}
                  <Unknowns items={p.unknowns} compact />
                </div>
              </td>
              {/* The venue's own word for the side held. "YES" on a Bitcoin
                  Up-or-Down market is a translation nobody asked for, and it
                  is the same mistranslation that used to lose the cost basis. */}
              <td className={cls('py-2 font-medium',
                p.side === 'yes' ? 'text-krypt-win' : 'text-krypt-loss')}
                title={p.sideLabel
                  ? `Outcome ${p.side === 'yes' ? 1 : 2} of 2 on this market`
                  : undefined}
              >
                {p.sideLabel ?? p.side.toUpperCase()}
              </td>
              <td className="py-2 text-right font-mono text-krypt-muted">
                <Val v={p.size} fmt={(n) => n.toLocaleString()} />
              </td>
              {/* The provenance dot is the whole point: a basis we computed
                  from your fills and one Polymarket US computed for your account
                  are different claims, and the screen has to say which. */}
              <td className="py-2 text-right font-mono text-krypt-muted">
                <span className="inline-flex items-center gap-1"
                  title={p.basisSource === 'data-api'
                    ? 'Polymarket US’s own average for this account. Your fill '
                      + 'ledger does not reach back this far — press Reconcile '
                      + 'fills to replace it with ours.'
                    : p.basisSource === 'ledger'
                      ? 'Average cost computed here from your reconciled fills.'
                      : undefined}
                >
                  <Src stamp={p.sources.avgCost} />
                  <Val v={p.avgCostCents} fmt={cents} />
                </span>
              </td>
              <td className="py-2 text-right font-mono text-white">
                <span className="inline-flex items-center gap-1"
                  title={p.settled
                    ? 'Exact: the outcome is posted, so this side is worth 100¢ or 0¢.'
                    : undefined}
                >
                  <Src stamp={p.sources.probability} />
                  <Val v={p.markCents} fmt={cents} />
                  {p.settled && <span className="text-[9px] text-krypt-dim">exact</span>}
                </span>
              </td>
              <td className="py-2 text-right font-mono text-white">
                <Val v={p.valueUsd} fmt={usdExact} />
              </td>
              {/* A settled row has no unrealised figure — rendering the
                  column as an em dash made a decided win or loss look like
                  missing data. Show whichever number this row actually has;
                  the settled badge in the first column says which kind. */}
              <td className={cls('py-2 text-right font-mono',
                (p.settled ? p.realizedPnlUsd : p.unrealizedPnlUsd) === null
                  ? 'text-krypt-dim'
                  : (p.settled ? p.realizedPnlUsd! : p.unrealizedPnlUsd!) >= 0
                    ? 'text-krypt-win' : 'text-krypt-loss')}
                title={p.settled ? 'Realised at settlement' : 'Unrealised — mark vs cost'}
              >
                <Val v={p.settled ? p.realizedPnlUsd : p.unrealizedPnlUsd}
                  fmt={signedUsd} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {showRecord && <SettledRecordTable onOpen={onOpen} />}
    </Card>
  );
}

function Orders({
  orders, onOpen, onRefresh,
}: { orders: RestingOrder[]; onOpen: (cid: string) => void; onRefresh: () => void }) {
  const toast = useToast();
  const cancel = async (id: string) => {
    const r = await window.krypt.terminal.cancel(id);
    toast[r.ok ? 'success' : 'error'](r.message);
    onRefresh();
  };
  if (!orders.length) {
    return <Empty title="No resting orders"
      description="Limit orders you have working on the book show up here." />;
  }
  return (
    <Card>
      <p className="mb-3 text-[11px] text-krypt-dim">
        Every working order on your account, whichever engine placed it. An order
        this app has no record of is shown as “Placed elsewhere” rather than
        quietly attributed to you.
      </p>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
            <th className="pb-2">Market</th>
            <th className="pb-2">Side</th>
            <th className="pb-2 text-right">Price</th>
            <th className="pb-2 text-right">Size</th>
            <th className="pb-2 text-right">Filled</th>
            <th className="pb-2 text-right">Placed by</th>
            <th className="pb-2" />
          </tr>
        </thead>
        <tbody>
          {orders.map((o) => (
            <tr key={o.orderId} className="border-b border-krypt-border/50 last:border-0">
              <td className="max-w-[22rem] py-2">
                <button onClick={() => onOpen(o.conditionId)}
                  className="truncate text-left text-white hover:text-krypt-pink">
                  {o.title ?? o.conditionId.slice(0, 18)}
                </button>
              </td>
              <td className={cls('py-2', o.action === 'buy' ? 'text-krypt-win' : 'text-krypt-loss')}>
                {o.action} {o.side.toUpperCase()}
              </td>
              <td className="py-2 text-right font-mono text-white"><Val v={o.priceCents} fmt={cents} /></td>
              <td className="py-2 text-right font-mono text-krypt-muted">
                <Val v={o.size} fmt={(n) => n.toLocaleString()} />
              </td>
              <td className="py-2 text-right font-mono text-krypt-muted">
                <Val v={o.filledSize} fmt={(n) => n.toLocaleString()} />
              </td>
              <td className="py-2 text-right text-[11px] text-krypt-dim">
                {ORIGIN_LABEL[o.origin] ?? o.origin}
              </td>
              <td className="py-2 text-right">
                <button
                  onClick={() => cancel(o.orderId)}
                  title="Cancel this order"
                  className="text-krypt-dim hover:text-krypt-loss"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Standing({
  items, onOpen, onRefresh,
}: { items: StandingInstruction[]; onOpen: (cid: string) => void; onRefresh: () => void }) {
  const cancel = async (id: string) => {
    await window.krypt.terminal.standing({ action: 'cancel', id });
    onRefresh();
  };
  const arm = async (id: string, armed: boolean) => {
    await window.krypt.terminal.standing({ action: 'arm', id, armed });
    onRefresh();
  };
  return (
    <Card>
      <p className="mb-3 text-[11px] text-krypt-dim">
        The one thing here that acts without a click — because you wrote it
        down. Every pending instruction is on this page and cancellable from
        it, and each one passes exactly the checks a manual order does.
        Triggers are <strong>probability points</strong>, not percentages.
      </p>
      {!items.length ? (
        <Empty title="Nothing standing"
          description="Add a stop, take-profit or alert from a market's order ticket." />
      ) : (
        <div className="space-y-2">
          {items.map((s) => (
            <div key={s.id} className={cls(
              'flex items-center gap-3 rounded-lg border p-3',
              s.firedAt ? 'border-krypt-border bg-krypt-surface2/30 opacity-70'
                : s.armed ? 'border-krypt-purple/30 bg-krypt-purple/5'
                  : 'border-krypt-warn/30 bg-krypt-warn/5')}
            >
              <Radar className={cls('h-4 w-4 shrink-0',
                s.firedAt ? 'text-krypt-dim' : s.armed ? 'text-krypt-purple' : 'text-krypt-warn')} />
              <div className="min-w-0 flex-1">
                <button onClick={() => onOpen(s.conditionId)}
                  className="truncate text-left text-sm text-white hover:text-krypt-pink">
                  {s.title ?? s.conditionId.slice(0, 18)}
                </button>
                <div className="text-[11px] text-krypt-muted">
                  {STANDING_LABEL[s.kind]} · {s.side.toUpperCase()} at{' '}
                  <span className="font-mono">{s.triggerCents}¢</span>
                  {s.size !== null && <> · {s.size} contracts</>}
                  {s.firedAt && <> · fired {new Date(s.firedAt).toLocaleString()}</>}
                </div>
                {s.note && <div className="mt-1 text-[11px] text-krypt-warn">{s.note}</div>}
              </div>
              {!s.firedAt && (
                <button
                  onClick={() => arm(s.id, !s.armed)}
                  className="rounded border border-krypt-border px-2 py-1 text-[11px] text-krypt-muted hover:text-white"
                >
                  {s.armed ? 'Disarm' : 'Re-arm'}
                </button>
              )}
              <button onClick={() => cancel(s.id)} className="text-krypt-dim hover:text-krypt-loss">
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function Fills({ fills, onOpen }: { fills: TerminalFill[]; onOpen: (cid: string) => void }) {
  if (!fills.length) {
    return <Empty title="No fills recorded"
      description="This ledger is written from Polymarket US's own trade history, not from what we requested. Press Reconcile fills to pull it." />;
  }
  return (
    <Card>
      <p className="mb-3 text-[11px] text-krypt-dim">
        Every row here came from the exchange's records — every fill on this
        account, whether you placed it by hand or an engine did. Where a fee
        could not be determined it reads “—”, not $0: a fee we cannot see is
        not a fee of zero, and pretending otherwise flatters every number on
        the page.
      </p>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
            <th className="pb-2">When</th>
            <th className="pb-2">Market</th>
            <th className="pb-2">Action</th>
            <th className="pb-2 text-right">Size</th>
            <th className="pb-2 text-right">Price</th>
            <th className="pb-2 text-right">Cost</th>
            <th className="pb-2 text-right">Fee</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((f) => (
            <tr key={f.id} className="border-b border-krypt-border/50 last:border-0">
              <td className="py-2 text-[11px] text-krypt-dim">
                {new Date(f.ts).toLocaleString()}
              </td>
              <td className="max-w-[18rem] py-2">
                <button onClick={() => onOpen(f.conditionId)}
                  className="truncate text-left text-white hover:text-krypt-pink">
                  {f.title || f.conditionId.slice(0, 16)}
                </button>
              </td>
              <td className={cls('py-2', f.action === 'buy' ? 'text-krypt-win' : 'text-krypt-loss')}>
                {f.action} {f.side.toUpperCase()}
              </td>
              <td className="py-2 text-right font-mono text-krypt-muted">{f.size.toLocaleString()}</td>
              <td className="py-2 text-right font-mono text-white"><Val v={f.priceCents} fmt={cents} /></td>
              <td className="py-2 text-right font-mono text-krypt-muted"><Val v={f.costUsd} fmt={usdExact} /></td>
              <td className="py-2 text-right font-mono text-krypt-muted"><Val v={f.feeUsd} fmt={usdExact} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function OrderLog({
  rows, onOpen,
}: { rows: TerminalOrderLogRow[]; onOpen: (cid: string) => void }) {
  if (!rows.length) {
    return <Empty title="No manual orders yet"
      description="Orders you place from a market ticket are logged here — including the ones that failed." />;
  }
  return (
    <Card>
      <p className="mb-3 text-[11px] text-krypt-dim">
        Failed attempts are kept on purpose. Polymarket US has no client-side
        idempotency key, so an order that errored <em>after</em> the venue
        accepted it is exactly the row you need when a position turns up that
        you do not remember opening.
      </p>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
            <th className="pb-2">When</th>
            <th className="pb-2">Market</th>
            <th className="pb-2">Order</th>
            <th className="pb-2 text-right">Requested</th>
            <th className="pb-2 text-right">Filled</th>
            <th className="pb-2">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.localId} className="border-b border-krypt-border/50 last:border-0">
              <td className="py-2 text-[11px] text-krypt-dim">
                {new Date(r.createdAt).toLocaleString()}
              </td>
              <td className="max-w-[18rem] py-2">
                <button onClick={() => onOpen(r.conditionId)}
                  className="truncate text-left text-white hover:text-krypt-pink">
                  {r.title || r.conditionId.slice(0, 16)}
                </button>
              </td>
              <td className={cls('py-2 text-xs', r.action === 'buy' ? 'text-krypt-win' : 'text-krypt-loss')}>
                {r.action} {r.side.toUpperCase()} · {r.orderType}
                {r.standingId && <span className="ml-1 text-krypt-dim">(standing)</span>}
              </td>
              <td className="py-2 text-right font-mono text-xs text-krypt-muted">
                {r.requestedSize} @ {r.requestedPriceCents}¢
              </td>
              <td className="py-2 text-right font-mono text-xs text-white">
                <Val v={r.filledSize} fmt={(n) => n.toLocaleString()} />
                {r.avgFillPriceCents !== null && (
                  <span className="text-krypt-dim"> @ {r.avgFillPriceCents.toFixed(1)}¢</span>
                )}
              </td>
              <td className="py-2">
                <span className={cls('rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
                  r.status === 'filled' ? 'border-krypt-win/40 text-krypt-win'
                    : r.status === 'error' || r.status === 'rejected' ? 'border-krypt-loss/40 text-krypt-loss'
                      : r.status === 'unmatched' ? 'border-krypt-warn/40 text-krypt-warn'
                        : 'border-krypt-border text-krypt-muted')}
                  title={r.status === 'rejected'
                    ? r.error ?? 'This order was refused — nothing was placed and nothing filled.'
                    : r.error ?? undefined}
                >
                  {r.status}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Paper account
//
// A completely separate set of numbers, on purpose. The value of paper
// trading comes entirely from believing it, and you can only believe it if
// its figures could never have been quietly mixed with your real ones.
// ────────────────────────────────────────────────────────────────────────

function Paper({
  pf, orders, onOpen, onRefresh,
}: {
  pf: PaperPortfolio | null;
  orders: PaperOrder[];
  onOpen: (cid: string) => void;
  onRefresh: () => void;
}) {
  const toast = useToast();
  const [confirmReset, setConfirmReset] = useState(false);

  const reset = async () => {
    const r = await window.krypt.terminal.paperReset();
    toast.success(r.message);
    setConfirmReset(false);
    onRefresh();
  };

  if (!pf) return <div className="py-8 text-center text-sm text-krypt-dim">Loading…</div>;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <StatCard label="Paper cash" value={<Val v={pf.cashUsd} fmt={usdExact} />}
          hint={`from ${usdExact(pf.bankrollUsd)} bankroll`} />
        <StatCard label="Position value" value={<Val v={pf.totalValueUsd} fmt={usdExact} />} />
        <StatCard label="Equity" value={<Val v={pf.equityUsd} fmt={usdExact} />}
          hint="cash + positions" />
        <StatCard
          label="Unrealised"
          value={<Val v={pf.totalUnrealizedPnlUsd} fmt={signedUsd} />}
          accent={pnlAccent(pf.totalUnrealizedPnlUsd)}
        />
        {/* The hint's `?? 0` printed "+$0.00" for a realised P&L we do not
            have — the same lie as a green zero. Unknown reads as an em dash. */}
        <StatCard
          label="Return"
          value={<Val v={pf.returnPct} fmt={(n) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`} />}
          accent={pnlAccent(pf.returnPct)}
          hint={`realised ${pf.realizedPnlUsd == null ? '—' : signedUsd(pf.realizedPnlUsd)}`}
        />
      </div>

      {pf.unreconciledNote && <Note tone="warn">{pf.unreconciledNote}</Note>}

      <Card
        header={
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-white">How this is simulated</span>
            {confirmReset ? (
              <div className="flex gap-2">
                <button onClick={() => setConfirmReset(false)}
                  className="rounded border border-krypt-border px-2 py-1 text-[11px] text-krypt-muted hover:text-white">
                  Keep
                </button>
                <button onClick={reset}
                  className="rounded border border-krypt-loss/50 bg-krypt-loss/10 px-2 py-1 text-[11px] text-krypt-loss">
                  Wipe paper account
                </button>
              </div>
            ) : (
              <button onClick={() => setConfirmReset(true)}
                className="rounded border border-krypt-border px-2 py-1 text-[11px] text-krypt-muted hover:text-white">
                Reset
              </button>
            )}
          </div>
        }
      >
        {/* Stating the simplifications IS the feature. A simulator whose
            assumptions you cannot see teaches you to trust a number that was
            never earned. */}
        <ul className="space-y-1.5 text-xs text-krypt-muted">
          <li>
            <strong className="text-white">Fills come out of the real book.</strong>{' '}
            You get what was actually resting at your price, at the average you
            would have walked — never your limit, never more size than existed.
          </li>
          <li>
            <strong className="text-white">Real fees.</strong> The same fee-v2
            curve the live ticket quotes, from each market&rsquo;s own schedule.
            An unknown fee stays unknown rather than becoming zero.
          </li>
          <li>
            <strong className="text-white">Resting orders are pessimistic.</strong>{' '}
            We cannot know your place in the queue, so a resting order fills
            only when the market trades <em>strictly through</em> your price.
            Real life would sometimes fill you at the touch; erring the other
            way would invent fills that never happened.
          </li>
          <li>
            <strong className="text-white">No market impact.</strong> Your paper
            order does not move the real book, so a large order is modelled a
            little better than reality. The ticket warns when an order eats a
            meaningful share of the visible depth.
          </li>
          <li>
            <strong className="text-white">Settlement is exact.</strong> A
            resolved market pays 100¢ or 0¢, automatically.
          </li>
        </ul>
      </Card>

      {pf.positions.length > 0 && (
        <Card>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-krypt-border text-left text-[10px] uppercase tracking-wider text-krypt-muted">
                <th className="pb-2">Market</th>
                <th className="pb-2">Side</th>
                <th className="pb-2 text-right">Size</th>
                <th className="pb-2 text-right">Avg cost</th>
                <th className="pb-2 text-right">Mark</th>
                <th className="pb-2 text-right">Value</th>
                <th className="pb-2 text-right">Unrealised</th>
              </tr>
            </thead>
            <tbody>
              {pf.positions.map((p) => (
                <tr key={`${p.conditionId}-${p.side}`} className="border-b border-krypt-border/50 last:border-0">
                  <td className="max-w-[24rem] py-2">
                    <button onClick={() => onOpen(p.conditionId)}
                      className="truncate text-left text-white hover:text-krypt-pink">
                      {p.title ?? p.conditionId.slice(0, 18)}
                    </button>
                    {/* Same rule as the real table: an em dash always comes
                        with a reason. A short-dated market that has closed
                        cannot be valued, and saying so beats a blank cell. */}
                    <div className="mt-0.5 flex flex-wrap items-center gap-2">
                      {/* Same null rule as the real table: an unread outcome is
                          unknown, never "lost". */}
                      {p.settled && (
                        <span className={cls(
                          'rounded-full border px-1.5 py-0.5 text-[10px] uppercase tracking-wider',
                          !p.resolvedOutcome
                            ? 'border-krypt-border text-krypt-dim'
                            : p.resolvedOutcome === p.side
                              ? 'border-krypt-win/40 text-krypt-win'
                              : 'border-krypt-loss/40 text-krypt-loss')}>
                          {p.resolvedOutcome
                            ? `settled ${p.resolvedOutcome === p.side ? 'won' : 'lost'}`
                            : 'settled · outcome unknown'}
                        </span>
                      )}
                      <Unknowns items={p.unknowns} compact />
                    </div>
                  </td>
                  <td className={cls('py-2 font-medium',
                    p.side === 'yes' ? 'text-krypt-win' : 'text-krypt-loss')}>
                    {p.side.toUpperCase()}
                  </td>
                  <td className="py-2 text-right font-mono text-krypt-muted">
                    {p.size.toLocaleString()}
                  </td>
                  <td className="py-2 text-right font-mono text-krypt-muted">
                    <Val v={p.avgCostCents} fmt={cents} />
                  </td>
                  <td className="py-2 text-right font-mono text-white">
                    <Val v={p.markCents} fmt={cents} />
                  </td>
                  <td className="py-2 text-right font-mono text-white">
                    <Val v={p.valueUsd} fmt={usdExact} />
                  </td>
                  <td className={cls('py-2 text-right font-mono',
                    p.unrealizedPnlUsd === null ? 'text-krypt-dim'
                      : p.unrealizedPnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss')}>
                    <Val v={p.unrealizedPnlUsd} fmt={signedUsd} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Card header={<span className="text-sm font-medium text-white">Simulated orders</span>}>
        {!orders.length ? (
          <Empty
            title="No paper orders yet"
            description="Turn on paper mode in Settings, then place an order from any market ticket."
          />
        ) : (
          <div className="space-y-1.5">
            {orders.map((o) => (
              <div key={o.localId} className="rounded-lg border border-krypt-border/60 p-2.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <button onClick={() => onOpen(o.conditionId)}
                      className="truncate text-left text-sm text-white hover:text-krypt-pink">
                      {o.title || o.conditionId.slice(0, 20)}
                    </button>
                    <div className="mt-0.5 text-[11px] text-krypt-muted">
                      <span className={o.action === 'buy' ? 'text-krypt-win' : 'text-krypt-loss'}>
                        {o.orderType === 'SETTLE' ? 'settled' : `${o.action} ${o.side.toUpperCase()}`}
                      </span>
                      {' · '}asked {o.requestedSize.toLocaleString()} @ {o.requestedPriceCents}¢
                      {o.filledSize > 0 && (
                        <> · filled <span className="text-white">{o.filledSize.toLocaleString()}</span>
                          {o.avgFillPriceCents !== null && <> @ {o.avgFillPriceCents.toFixed(1)}¢</>}
                        </>
                      )}
                      {o.restingSize > 0 && <> · {o.restingSize.toLocaleString()} resting</>}
                    </div>
                    {/* The note is where the honesty lives — why it filled the
                        way it did, or why it did not. */}
                    {o.note && (
                      <div className="mt-1 text-[11px] text-krypt-dim">{o.note}</div>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <span className={cls(
                      'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
                      o.status === 'filled' ? 'border-krypt-win/40 text-krypt-win'
                        : o.status === 'settled' ? 'border-krypt-purple/40 text-krypt-purple'
                          : o.status === 'unfilled' || o.status === 'canceled'
                            ? 'border-krypt-border text-krypt-dim'
                            : 'border-krypt-warn/40 text-krypt-warn')}>
                      {o.status}
                    </span>
                    {(o.status === 'resting' || o.status === 'partial') && o.restingSize > 0 && (
                      <button
                        onClick={async () => {
                          const r = await window.krypt.terminal.paperCancel(o.localId);
                          toast[r.ok ? 'success' : 'error'](r.message);
                          onRefresh();
                        }}
                        title="Cancel the resting part"
                        className="text-krypt-dim hover:text-krypt-loss"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
