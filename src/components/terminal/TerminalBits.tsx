import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { AlertTriangle, CircleHelp, Info, Radio } from 'lucide-react';
import {
  Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import type {
  DataSource, PricePoint, Provenance, RiskLevel, SourceStamp, UnknownReason,
} from '@shared/market';
import { DATA_SOURCE_LABEL } from '@shared/market';
// Pure geometry, extracted to shared/ so it can be unit tested without React.
import type { GapPoint } from '@shared/series';
import { breakGaps, countGaps } from '@shared/series';
import { cls } from '../../utils/format';

// ────────────────────────────────────────────────────────────────────────
// The em-dash rule, made unavoidable.
//
// Everything that can render an unknown goes through here, so no screen can
// quietly decide that a missing number is a zero.
// ────────────────────────────────────────────────────────────────────────

export function Val({
  v, fmt, className, title,
}: {
  v: number | null | undefined;
  fmt?: (n: number) => string;
  className?: string;
  title?: string;
}) {
  const unknown = v === null || v === undefined || Number.isNaN(v);
  return (
    <span
      className={cls(className, unknown && 'text-krypt-dim')}
      title={unknown ? (title ?? 'Nobody could produce this value') : undefined}
    >
      {unknown ? '—' : (fmt ? fmt(v as number) : String(v))}
    </span>
  );
}

export const cents = (n: number) => `${n.toFixed(1)}¢`;
export const centsInt = (n: number) => `${Math.round(n)}¢`;
// A price as the user typed it: "15¢", not "15.0¢" and not a 15.5¢ tick
// rounded away to 16¢. For order prices, where the exact number is the point.
export const centsTight = (n: number) => `${Number(n.toFixed(2))}¢`;
export const usd = (n: number) => {
  const a = Math.abs(n);
  const s = a >= 1_000_000 ? `${(a / 1_000_000).toFixed(2)}M`
    : a >= 1_000 ? `${(a / 1_000).toFixed(1)}K`
      : a.toFixed(2);
  return `${n < 0 ? '-' : ''}$${s}`;
};
// Money the user COMMITS to — a ticket cost, a cash balance, a cost basis —
// never gets K-rounded: "$1.2K" on a confirmation line is not the number the
// user agreed to. usd() keeps its rounding for genuinely large summary
// figures (24h volume, book depth) where the magnitude is the point.
export const usdExact = (n: number) =>
  `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString(undefined, {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
export const signedUsd = (n: number) => `${n < 0 ? '-' : '+'}$${Math.abs(n).toFixed(2)}`;
export const hours = (n: number) => {
  // Past close. Rounding to whole hours printed "0h ago" for a market that had
  // just closed, which reads like a broken field rather than a fresh event —
  // so drop to minutes below the hour.
  if (n < 0) {
    const a = -n;
    if (a < 1) return `${Math.max(1, Math.round(a * 60))}m ago`;
    if (a < 48) return `${a.toFixed(0)}h ago`;
    return `${(a / 24).toFixed(0)}d ago`;
  }
  if (n < 1) return `${Math.max(1, Math.round(n * 60))}m`;
  if (n < 48) return `${n.toFixed(1)}h`;
  return `${(n / 24).toFixed(0)}d`;
};

// ────────────────────────────────────────────────────────────────────────
// Provenance
// ────────────────────────────────────────────────────────────────────────

const SOURCE_TONE: Record<DataSource, string> = {
  'clob-ws': 'bg-krypt-win',
  clob: 'bg-krypt-purple',
  gateway: 'bg-krypt-pink',
  gamma: 'bg-krypt-pink',
  'data-api': 'bg-krypt-warn',
  venue: 'bg-krypt-purple',
  ws: 'bg-krypt-win',
  ledger: 'bg-krypt-win',
  local: 'bg-krypt-dim',
};

/**
 * A dot naming which provider produced the number next to it, and how stale
 * it is. A terminal that shows a price without saying whether it came from
 * our own feed or a cached third-party index is asking for trust, and trust
 * is exactly what we are trying not to require.
 */
export function Src({ stamp, className }: { stamp?: SourceStamp; className?: string }) {
  if (!stamp) return null;
  const age = stamp.ageSec;
  const ageTxt = age === null ? 'age unknown'
    : age < 2 ? 'live'
      : age < 90 ? `${Math.round(age)}s old`
        : `${Math.round(age / 60)}m old`;
  return (
    <span
      className={cls('inline-block h-1.5 w-1.5 shrink-0 rounded-full align-middle',
        SOURCE_TONE[stamp.source] ?? 'bg-krypt-dim', className)}
      title={`${DATA_SOURCE_LABEL[stamp.source] ?? stamp.source} · ${ageTxt} · received ${stamp.observedAt}`}
    />
  );
}

export function SourceLegend({ sources }: { sources: Provenance }) {
  const used = useMemo(() => {
    const seen = new Map<DataSource, number>();
    for (const s of Object.values(sources)) {
      if (!s) continue;
      seen.set(s.source, (seen.get(s.source) ?? 0) + 1);
    }
    return [...seen.entries()];
  }, [sources]);
  if (!used.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-krypt-dim">
      <span className="uppercase tracking-wider">Sources</span>
      {used.map(([src, n]) => (
        <span key={src} className="flex items-center gap-1.5">
          <span className={cls('h-1.5 w-1.5 rounded-full', SOURCE_TONE[src])} />
          {DATA_SOURCE_LABEL[src]} <span className="text-krypt-dim/60">×{n}</span>
        </span>
      ))}
    </div>
  );
}

/**
 * Why something is missing, and what would fix it. "No data" is useless;
 * these strings are written to be the other kind, so they are rendered
 * verbatim rather than summarised.
 */
export function Unknowns({ items, compact }: { items: UnknownReason[]; compact?: boolean }) {
  if (!items?.length) return null;
  if (compact) {
    return (
      <span
        className="inline-flex items-center gap-1 text-[11px] text-krypt-dim"
        title={items.map((u) => `${u.field}: ${u.reason}`).join('\n\n')}
      >
        <CircleHelp className="h-3 w-3" />
        {items.length} unknown
      </span>
    );
  }
  return (
    <div className="space-y-2 rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3">
      <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-krypt-muted">
        <CircleHelp className="h-3.5 w-3.5" /> What we could not show
      </div>
      {items.map((u) => (
        <div key={u.field} className="text-xs text-krypt-muted">
          <span className="font-mono text-krypt-dim">{u.field}</span> — {u.reason}
        </div>
      ))}
    </div>
  );
}

// ────────────────────────────────────────────────────────────────────────
// Small display pieces
// ────────────────────────────────────────────────────────────────────────

/** A 0..100 probability bar. Renders an explicit "no price" state, not 0%. */
export function ProbBar({ v, className }: { v: number | null; className?: string }) {
  if (v === null || v === undefined || Number.isNaN(v)) {
    return (
      <div className={cls('h-1.5 w-full rounded-full bg-krypt-surface2', className)}
        title="No price — this bar is empty because nobody quoted it, not because it is at zero." />
    );
  }
  const pct = Math.max(0, Math.min(100, v));
  return (
    <div className={cls('h-1.5 w-full overflow-hidden rounded-full bg-krypt-surface2', className)}>
      <div
        className="h-full rounded-full bg-krypt-purple transition-[width] duration-300"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/**
 * A field whose value is COMMITTED on blur or Enter, never per keystroke.
 *
 * Wiring a cap straight to onChange persists every intermediate string: typing
 * "50" over "100" writes a $5 cap on the way, and clearing the box first
 * persists "" → 0 — which for every cap in this app means NO CAP. So a blank
 * number field commits nothing and restores the previous value.
 *
 * Lives here rather than in a page because two different Settings pages need
 * it; it is the one shared component module both already import.
 */
export function CommitInput({
  value, onCommit, type = 'text', className, min, placeholder, title,
}: {
  value: string | number;
  /** Called with the raw field text, only once the user is finished. */
  onCommit: (raw: string) => void;
  type?: 'text' | 'number';
  className?: string;
  min?: number;
  placeholder?: string;
  title?: string;
}) {
  const [draft, setDraft] = useState(String(value));
  const [editing, setEditing] = useState(false);
  // While the user is typing the field is theirs; an outside change (a poll, a
  // preset apply) only reaches it once they leave.
  useEffect(() => { if (!editing) setDraft(String(value)); }, [value, editing]);

  const commit = () => {
    setEditing(false);
    const raw = draft.trim();
    if (type === 'number' && raw === '') { setDraft(String(value)); return; }
    if (raw !== String(value)) onCommit(raw);
  };

  return (
    <input
      type={type} min={min} placeholder={placeholder} title={title}
      value={draft}
      onChange={(e) => { setEditing(true); setDraft(e.target.value); }}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.currentTarget.blur(); }}
      className={className}
    />
  );
}

const RISK_TONE: Record<RiskLevel, string> = {
  ok: 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win',
  watch: 'border-krypt-warn/40 bg-krypt-warn/10 text-krypt-warn',
  risk: 'border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss',
  unknown: 'border-krypt-border bg-krypt-surface2 text-krypt-dim',
};

export function RiskPill({ level, children }: { level: RiskLevel; children?: ReactNode }) {
  return (
    <span className={cls(
      'inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
      RISK_TONE[level])}
    >
      {children ?? level}
    </span>
  );
}

export function Note({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' }) {
  if (!children) return null;
  const Icon = tone === 'warn' ? AlertTriangle : Info;
  return (
    <div className={cls(
      'flex items-start gap-2 rounded-lg border px-3 py-2 text-xs',
      tone === 'warn'
        ? 'border-krypt-warn/30 bg-krypt-warn/5 text-krypt-warn'
        : 'border-krypt-border bg-krypt-surface2/40 text-krypt-muted')}
    >
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export function OwnFeedBadge({ note }: { note?: string | null }) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-krypt-win/40 bg-krypt-win/10 px-2 py-0.5 text-[10px] uppercase tracking-wider text-krypt-win"
      title={note ?? 'Built from quotes this machine received directly, timestamped on arrival.'}
    >
      <Radio className="h-3 w-3" /> our feed
    </span>
  );
}

// ────────────────────────────────────────────────────────────────────────
// The chart
//
// Y axis is BOUNDED 0..100 — a prediction market prices a probability, and
// that bound is a gift the crypto version never had: every chart is directly
// comparable and 50¢ always sits in the middle.
//
// Gaps are NEVER filled. Where the series skips a period we insert an
// explicit null so the line BREAKS, rather than letting the renderer draw a
// straight line across hours nobody traded — an invented trend in an
// illiquid market is how somebody buys a corpse.
// ────────────────────────────────────────────────────────────────────────

export function ProbChart({
  points, height = 220, markers = [], overlay,
}: {
  points: PricePoint[];
  height?: number;
  /** Horizontal lines drawn in the series' OWN unit (cents). */
  markers?: { cents: number; label: string; tone?: 'good' | 'bad' | 'neutral' }[];
  /**
   * A SECOND venue's series on the same axis. Pre-bucketed by the backend
   * onto a shared grid, so a null here means that venue genuinely did not
   * trade in that period — the line breaks, exactly as it does for the
   * primary series. Both are probabilities in cents, so the bounded 0..100
   * axis makes them directly comparable with no rescaling.
   */
  overlay?: { label: string; data: { t: number; p: number | null; k: number | null }[] } | null;
}) {
  const data = useMemo(() => {
    if (overlay?.data?.length) return overlay.data;
    return breakGaps(points);
  }, [points, overlay]);
  const gaps = useMemo(
    () => (overlay ? 0 : countGaps(data as GapPoint[])), [data, overlay]);

  if (!points.length && !overlay?.data?.length) {
    return (
      <div className="grid h-[220px] place-items-center rounded-lg border border-dashed border-krypt-border text-sm text-krypt-dim">
        No price history
      </div>
    );
  }

  return (
    <div>
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: -18 }}>
            <XAxis
              dataKey="t" type="number" domain={['dataMin', 'dataMax']}
              scale="time" tick={{ fontSize: 10, fill: '#71717A' }}
              tickFormatter={(t: number) =>
                new Date(t * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}
              stroke="rgba(255,255,255,0.08)"
            />
            {/* Bounded 0..100 on purpose — see the note above. */}
            <YAxis
              domain={[0, 100]} ticks={[0, 25, 50, 75, 100]}
              tick={{ fontSize: 10, fill: '#71717A' }}
              tickFormatter={(v: number) => `${v}`}
              stroke="rgba(255,255,255,0.08)" width={38}
            />
            <ReferenceLine y={50} stroke="rgba(255,255,255,0.12)" strokeDasharray="3 3" />
            {markers.map((m) => (
              <ReferenceLine
                key={`${m.label}-${m.cents}`}
                y={m.cents}
                stroke={m.tone === 'bad' ? '#EF4444' : m.tone === 'good' ? '#22C55E' : '#A1A1AA'}
                strokeDasharray="4 3"
                label={{ value: m.label, position: 'insideTopLeft', fontSize: 10, fill: '#A1A1AA' }}
              />
            ))}
            <Tooltip
              contentStyle={{
                background: '#11111A', border: '1px solid rgba(255,255,255,0.08)',
                borderRadius: 8, fontSize: 12,
              }}
              labelFormatter={(t) => new Date(Number(t) * 1000).toLocaleString()}
              formatter={(v, name) => [
                typeof v === 'number' ? `${v.toFixed(1)}¢` : 'no trades',
                String(name ?? 'YES')]}
            />
            <Line
              type="monotone" dataKey="p" stroke="#3B82F6" strokeWidth={1.6}
              dot={false} isAnimationActive={false}
              name="Polymarket US"
              // The whole point of breakGaps(): do NOT bridge a hole.
              connectNulls={false}
            />
            {overlay && (
              <Line
                type="monotone" dataKey="k" stroke="#F59E0B" strokeWidth={1.4}
                dot={false} isAnimationActive={false}
                name={overlay.label}
                strokeDasharray="4 2"
                // Same rule for the second venue: a period it did not trade is
                // a hole, never a line drawn straight through it.
                connectNulls={false}
              />
            )}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {overlay && (
        <div className="mt-1 flex items-center gap-4 text-[11px] text-krypt-dim">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4 bg-[#3B82F6]" /> Polymarket US
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-0.5 w-4 border-t border-dashed border-[#F59E0B]" />
            {overlay.label}
          </span>
          <span>Breaks in either line are periods that venue did not trade.</span>
        </div>
      )}
      {gaps > 0 && (
        <div className="mt-1 text-[11px] text-krypt-dim">
          {gaps} break{gaps === 1 ? '' : 's'} in the line — periods with no trades.
          Nothing is drawn across them.
        </div>
      )}
    </div>
  );
}
