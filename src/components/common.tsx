import { ReactNode, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Share2 } from 'lucide-react';
import { cls } from '../utils/format';
import { shareToX, X_PROFILE } from '../utils/share';
import type { RuleCondition, TraderConfig } from '@shared/types';
import { GlassPanel } from './glass/GlassPanel';
import { GlassButton } from './glass/GlassButton';
import { GlassSwitch } from './glass/GlassSwitch';
import { useLiveGlass } from './glass/glassQuality';
import { GLASS_TINTS } from './glass/glassPresets';

/**
 * Dialogs render into <body>. Every card is frosted with a backdrop-filter,
 * and a non-`none` backdrop-filter makes its element the containing block for
 * `fixed` descendants (just like `transform`), so a dialog rendered in place
 * inside a card would lay its "full-screen" overlay out inside that card. The
 * portal makes that impossible for every dialog at once.
 */
export function DialogShell({
  onClose, maxWidth, children,
}: { onClose: () => void; maxWidth: string; children: ReactNode }) {
  return createPortal(
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/45 p-4"
      onMouseDown={onClose}
    >
      <GlassPanel
        preset="modal"
        tint={GLASS_TINTS.modal}
        className={cls('glass-pop w-full rounded-2xl p-5', maxWidth)}
        onMouseDown={(e) => e.stopPropagation()}
      >
        {children}
      </GlassPanel>
    </div>,
    document.body,
  );
}

/**
 * A larger dialog for multi-step flows (setup wizards). Unlike the small
 * dialogs it does NOT close on a backdrop click: a stray click halfway through
 * pasting a key would throw the user's progress away. Escape and the flow's
 * own buttons close it.
 */
export function Modal({
  open, onClose, maxWidth = 'max-w-xl', children,
}: { open: boolean; onClose: () => void; maxWidth?: string; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center overflow-y-auto bg-black/45 p-4">
      <GlassPanel
        preset="modal"
        tint={GLASS_TINTS.modal}
        className={cls('glass-pop w-full rounded-2xl p-6', maxWidth)}
      >
        {children}
      </GlassPanel>
    </div>,
    document.body,
  );
}

// A small in-app name/rename prompt. Electron doesn't support window.prompt(),
// so anything that needs a quick text input uses this modal instead.
export function NameDialog({
  open, title, label, initialValue = '', placeholder, confirmLabel = 'Save',
  onSubmit, onClose,
}: {
  open: boolean;
  title: string;
  label?: string;
  initialValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  onSubmit: (value: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initialValue);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setValue(initialValue);
    const t = setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 30);
    return () => clearTimeout(t);
  }, [open, initialValue]);

  if (!open) return null;

  const submit = (): void => {
    const v = value.trim();
    if (!v) return;
    onSubmit(v);
  };

  return (
    <DialogShell onClose={onClose} maxWidth="max-w-sm">
        <h3 className="text-sm font-semibold text-white">{title}</h3>
        {label && <p className="mt-1 text-xs text-krypt-muted">{label}</p>}
        <input
          ref={inputRef}
          autoFocus
          value={value}
          placeholder={placeholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') { e.preventDefault(); submit(); }
            else if (e.key === 'Escape') { e.preventDefault(); onClose(); }
          }}
          className="krypt-input mt-3 w-full"
        />
        <div className="mt-4 flex justify-end gap-2">
          <GlassButton onClick={onClose}>Cancel</GlassButton>
          <GlassButton variant="primary" onClick={submit} disabled={!value.trim()}>
            {confirmLabel}
          </GlassButton>
        </div>
    </DialogShell>
  );
}

export function ConfirmDialog({
  open, title, body, confirmLabel = 'Confirm', cancelLabel = 'Cancel', danger,
  onConfirm, onClose,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <DialogShell onClose={onClose} maxWidth="max-w-md">
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      <div className="mt-2 text-xs leading-relaxed text-white/70">{body}</div>
      <div className="mt-4 flex justify-end gap-2">
        <GlassButton onClick={onClose}>{cancelLabel}</GlassButton>
        <GlassButton variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>
          {confirmLabel}
        </GlassButton>
      </div>
    </DialogShell>
  );
}

export function Page({
  title, subtitle, actions, children,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-end justify-between gap-4 px-6 py-5">
        <div>
          <h2 className="glass-title text-2xl font-semibold tracking-tight">{title}</h2>
          {subtitle && (
            <p className="mt-1 text-sm text-krypt-muted">{subtitle}</p>
          )}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      <div className="flex-1 overflow-y-auto px-6 pb-8">{children}</div>
    </div>
  );
}

export function Card({
  className, children, header, footer,
}: {
  className?: string;
  children: ReactNode;
  header?: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className={cls('krypt-card', className)}>
      {header && (
        <div className="-mx-5 -mt-5 mb-4 rounded-t-2xl border-b border-white/[0.06] bg-gradient-to-b from-white/[0.05] to-white/[0.01] px-5 py-3">
          {header}
        </div>
      )}
      {children}
      {footer && (
        <div className="-mx-5 -mb-5 mt-4 rounded-b-2xl border-t border-white/[0.06] bg-white/[0.02] px-5 py-3">
          {footer}
        </div>
      )}
    </div>
  );
}

export function StatCard({
  label, value, hint, accent, className,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  accent?: 'good' | 'bad' | 'warn' | 'neutral';
  className?: string;
}) {
  // Stat tiles are live glass: they're the numbers a clip is framed around,
  // and there are few enough per page to stay in the per-page live-glass
  // budget. The accent is a coloured inner hairline + bloom.
  return (
    <GlassPanel
      preset="card"
      tint={ACCENT_TINT[accent ?? 'neutral']}
      className={cls('rounded-2xl p-5', className)}
      style={{ boxShadow: ACCENT_GLOW[accent ?? 'neutral'] }}
    >
      <div className="text-[11px] uppercase tracking-wider text-krypt-muted">{label}</div>
      <div className="mt-1 font-mono text-2xl font-medium tabular-nums text-white [text-shadow:0_1px_12px_rgba(0,0,0,0.5)]">{value}</div>
      {hint && <div className="mt-1 text-xs text-white/50">{hint}</div>}
    </GlassPanel>
  );
}

const ACCENT_TINT: Record<'good' | 'bad' | 'warn' | 'neutral', string> = {
  neutral: GLASS_TINTS.card,
  good: 'linear-gradient(160deg, rgba(34,197,94,0.13), rgba(14,15,24,0.58) 55%)',
  bad: 'linear-gradient(160deg, rgba(239,68,68,0.13), rgba(14,15,24,0.58) 55%)',
  warn: 'linear-gradient(160deg, rgba(245,158,11,0.13), rgba(14,15,24,0.58) 55%)',
};

const ACCENT_GLOW: Record<'good' | 'bad' | 'warn' | 'neutral', string> = {
  neutral: 'var(--glass-shadow)',
  good: 'inset 0 0 0 1px rgba(34,197,94,0.32), 0 0 34px -14px rgba(34,197,94,0.55), var(--glass-shadow)',
  bad: 'inset 0 0 0 1px rgba(239,68,68,0.32), 0 0 34px -14px rgba(239,68,68,0.55), var(--glass-shadow)',
  warn: 'inset 0 0 0 1px rgba(245,158,11,0.32), 0 0 34px -14px rgba(245,158,11,0.5), var(--glass-shadow)',
};

/** A `StatCard` with a small "share to X" button in the top-right corner. */
export function ShareableStat({
  label, value, hint, accent, className, shareText,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  accent?: 'good' | 'bad' | 'warn' | 'neutral';
  className?: string;
  /** Nothing to share (paper money, say): no button. */
  shareText?: string;
}) {
  return (
    <div className={cls('relative', className)}>
      <StatCard label={label} value={value} hint={hint} accent={accent} />
      {shareText && <ShareButton text={shareText} className="absolute right-3 top-3" />}
    </div>
  );
}

/** Floating "share this stat to X" button. */
export function ShareButton({
  text, className, size = 'sm',
}: {
  text: string;
  className?: string;
  size?: 'sm' | 'xs';
}) {
  const sz = size === 'xs' ? 'h-6 w-6' : 'h-7 w-7';
  const ic = size === 'xs' ? 'h-3 w-3' : 'h-3.5 w-3.5';
  return (
    <button
      onClick={() => void shareToX(text)}
      title={`Share to ${X_PROFILE}`}
      className={cls(
        'grid place-items-center rounded-lg border border-white/10 bg-gradient-to-b from-white/[0.08] to-white/[0.02] text-krypt-muted shadow-[inset_0_1px_0_rgba(255,255,255,0.08)] transition-all duration-200 hover:border-krypt-purple/50 hover:bg-krypt-purple/15 hover:text-white hover:shadow-[0_0_16px_-4px_rgba(59,130,246,0.7)] active:scale-90',
        sz,
        className,
      )}
    >
      <Share2 className={ic} />
    </button>
  );
}

export function Empty({
  title, description, action,
}: { title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="grid place-items-center rounded-2xl border border-dashed border-white/10 bg-white/[0.015] p-10 text-center">
      <div>
        <div className="text-base font-medium text-white">{title}</div>
        {description && <p className="mt-1 max-w-md text-sm text-krypt-muted">{description}</p>}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}

export function Switch({
  checked, onChange, label, description, disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label?: string;
  description?: string;
  disabled?: boolean;
}) {
  // The row stays one big click target (as the old <button> row was); the
  // thumb is the library's glass switch, where the lens IS the thumb. The
  // switch owns its own clicks/drags, so the row ignores events from inside
  // it, otherwise one tap would toggle twice. Keyboard users get the real
  // <input role="switch"> inside GlassSwitch.
  const liveGlass = useLiveGlass();
  return (
    <div
      onClick={() => { if (!disabled) onChange(!checked); }}
      className={cls(
        'flex w-full cursor-pointer select-none items-center justify-between gap-4 rounded-xl border border-white/[0.07] bg-white/[0.025] p-3 text-left shadow-[inset_0_1px_0_rgba(255,255,255,0.04)] transition-colors hover:border-white/[0.14] hover:bg-white/[0.04]',
        disabled && 'cursor-not-allowed',
      )}
    >
      {/* Dim the text only: GlassSwitch dims itself when disabled, and a
          row-level opacity on top would take the thumb down to ~20%. */}
      <div className={cls('flex-1', disabled && 'opacity-50')}>
        {label && <div className="text-sm text-white">{label}</div>}
        {description && (
          <div className="mt-0.5 text-xs text-krypt-muted">{description}</div>
        )}
      </div>
      <div className="shrink-0" onClick={(e) => e.stopPropagation()}>
        {liveGlass ? (
          <GlassSwitch
            checked={checked}
            onCheckedChange={onChange}
            disabled={disabled}
            ariaLabel={label}
            width={44}
            height={24}
          />
        ) : (
          <FrostSwitch checked={checked} onChange={onChange} disabled={disabled} label={label} />
        )}
      </div>
    </div>
  );
}

/** The switch without an SVG lens, for Frost glass quality: same 44×24 box,
 *  same track colours, a CSS thumb. Still a real <input role="switch">, so
 *  keyboard users and anything that finds switches by role see no change. */
function FrostSwitch({
  checked, onChange, disabled, label,
}: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <label className={cls('relative inline-flex h-6 w-11 items-center', disabled ? 'cursor-not-allowed opacity-40' : 'cursor-pointer')}>
      <input
        type="checkbox"
        role="switch"
        className="peer sr-only"
        checked={checked}
        disabled={disabled}
        aria-label={label}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className={cls(
        'absolute inset-0 rounded-full border transition-colors duration-200 peer-focus-visible:ring-2 peer-focus-visible:ring-krypt-purple/60',
        checked
          ? 'border-krypt-purple/60 bg-krypt-purple/70 shadow-[0_0_12px_-2px_rgba(59,130,246,0.7)]'
          : 'border-white/10 bg-white/[0.08]',
      )} />
      <span className={cls(
        'absolute left-[3px] h-[18px] w-[18px] rounded-full bg-white shadow-[0_1px_3px_rgba(0,0,0,0.35)] transition-transform duration-200 ease-out',
        checked && 'translate-x-5',
      )} />
    </label>
  );
}

/**
 * Local-optimistic mirror of a config-backed value, for multi-step editors
 * (multi-selects, pickers, list builders). `config.update` is an async IPC
 * round-trip, so two quick edits would otherwise both build on the
 * pre-first-edit snapshot and the first would silently revert. Re-seeds from
 * the prop only when the external value genuinely changes (compared by JSON);
 * when the LATEST commit rejects, falls back to whatever the prop says now.
 */
export function useOptimisticValue<T>(
  value: T,
  onCommit: (next: T) => void | Promise<void>,
): [T, (next: T) => void] {
  const [local, setLocal] = useState<T>(value);
  const key = JSON.stringify(value ?? null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { setLocal(value); }, [key]);
  const valueRef = useRef(value);
  valueRef.current = value;
  const seq = useRef(0);
  const apply = (next: T): void => {
    setLocal(next);
    const mine = ++seq.current;
    const rollback = (): void => {
      if (mine === seq.current) setLocal(valueRef.current);
    };
    try {
      void Promise.resolve(onCommit(next)).catch(rollback);
    } catch {
      rollback();
    }
  };
  return [local, apply];
}

// A composable AND-of-conditions entry-rule builder, reused by every trading
// module (main bot + 15m crypto). `fields` is the per-module vocabulary; the
// `useRulesKey`/`rulesKey` say which config keys to read/write. The rule-set,
// when enabled, REPLACES that module's built-in threshold gate.
export type RuleFieldSpec = { key: string; label: string; dflt: number };
const RULE_OPS: Array<RuleCondition['op']> = ['>=', '<=', '>', '<'];

// Numeric value box for a rule condition. Local text buffer committed on
// blur/Enter — binding the number straight to config and doing
// `parseFloat(e.target.value) || 0` per keystroke collapsed the decimal point
// ("0.55" → "0") and reset a cleared box to 0, making it un-typeable.
function RuleValueInput({ value, onCommit }: { value: number; onCommit: (n: number) => void }) {
  const [text, setText] = useState(() => String(value));
  const focused = useRef(false);
  useEffect(() => { if (!focused.current) setText(String(value)); }, [value]);
  const commit = (): void => {
    const n = parseFloat(text);
    if (!Number.isFinite(n)) { setText(String(value)); return; }
    setText(String(n));
    if (n !== value) onCommit(n);
  };
  return (
    <input
      type="number"
      step="any"
      inputMode="decimal"
      value={text}
      onFocus={() => { focused.current = true; }}
      onChange={(e) => setText(e.target.value)}
      onBlur={() => { focused.current = false; commit(); }}
      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
      className="krypt-input w-24 font-mono"
    />
  );
}

export function RuleBuilder({
  config, fields, useRulesKey, rulesKey, label, description, tip,
}: {
  config?: TraderConfig | null;
  fields: RuleFieldSpec[];
  useRulesKey: keyof TraderConfig;
  rulesKey: keyof TraderConfig;
  label: string;
  description: string;
  tip?: ReactNode;
}) {
  const useRules = !!(config as Record<string, unknown> | null | undefined)?.[useRulesKey as string];
  const rules = (((config as Record<string, unknown> | null | undefined)?.[rulesKey as string]) ?? []) as RuleCondition[];
  const save = (patch: Partial<TraderConfig>) => void window.krypt.config.update(patch);
  const setRules = (next: RuleCondition[]) => save({ [rulesKey]: next } as Partial<TraderConfig>);

  return (
    <div className="rounded-xl border border-krypt-border bg-krypt-surface p-3">
      <Switch
        checked={useRules}
        onChange={(v) => save({ [useRulesKey]: v } as Partial<TraderConfig>)}
        label={label}
        description={description}
      />
      {useRules && (
        <div className="mt-3 space-y-2">
          {rules.length === 0 && (
            <div className="text-[11px] text-krypt-warn">
              No conditions yet — add at least one (an empty rule-set never trades).
            </div>
          )}
          {rules.map((r, i) => (
            <div key={i} className="flex items-center gap-2">
              <select
                value={r.field}
                onChange={(e) => setRules(rules.map((x, j) => (j === i ? { ...x, field: e.target.value } : x)))}
                className="krypt-input flex-1"
              >
                {fields.map((f) => <option key={f.key} value={f.key}>{f.label}</option>)}
              </select>
              <select
                value={r.op}
                onChange={(e) => setRules(rules.map((x, j) => (j === i ? { ...x, op: e.target.value as RuleCondition['op'] } : x)))}
                className="krypt-input w-16 text-center font-mono"
              >
                {RULE_OPS.map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
              <RuleValueInput
                value={r.value}
                onCommit={(n) => setRules(rules.map((x, j) => (j === i ? { ...x, value: n } : x)))}
              />
              <button
                onClick={() => setRules(rules.filter((_, j) => j !== i))}
                className="px-1 text-krypt-loss/80 hover:text-krypt-loss"
                title="Remove condition"
              >✕</button>
            </div>
          ))}
          <button
            onClick={() => setRules([...rules, { field: fields[0].key, op: '>=', value: fields[0].dflt }])}
            className="krypt-btn-default text-xs"
          >
            + Add condition
          </button>
          {tip && <div className="text-[10px] text-krypt-dim">{tip}</div>}
        </div>
      )}
    </div>
  );
}

export function NumberInput({
  value, onChange, min, max, step, suffix, prefix, disabled,
}: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  prefix?: string;
  disabled?: boolean;
}) {
  // Edit a LOCAL text buffer, commit on blur/Enter. Binding the input straight
  // to the numeric prop (and only propagating finite parses) made the box
  // un-typeable: clearing it, or typing an intermediate value like "", "-" or
  // "0." parses to NaN, gets dropped, and React snaps the old number back — the
  // "can't type in the box" bug. We re-sync from the prop only when the field
  // isn't focused, so external updates (presets, polling) never clobber typing.
  const [text, setText] = useState(() => (Number.isFinite(value) ? String(value) : ''));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setText(Number.isFinite(value) ? String(value) : '');
  }, [value]);

  const commit = (): void => {
    const n = parseFloat(text);
    if (!Number.isFinite(n)) {
      setText(Number.isFinite(value) ? String(value) : '');
      return;
    }
    let clamped = n;
    if (typeof min === 'number') clamped = Math.max(min, clamped);
    if (typeof max === 'number') clamped = Math.min(max, clamped);
    setText(String(clamped));
    if (clamped !== value) onChange(clamped);
  };

  return (
    <div className={cls('relative', disabled && 'opacity-50')}>
      {prefix && (
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-xs text-krypt-dim">
          {prefix}
        </span>
      )}
      <input
        type="number"
        value={text}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        onFocus={() => { focused.current = true; }}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => { focused.current = false; commit(); }}
        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
        className={cls(
          'krypt-input font-mono',
          prefix && 'pl-7',
          suffix && 'pr-12',
          disabled && 'cursor-not-allowed',
        )}
      />
      {suffix && (
        <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-krypt-dim">
          {suffix}
        </span>
      )}
    </div>
  );
}

export function Section({
  title, description, children,
}: { title: string; description?: string; children: ReactNode }) {
  return (
    <div className="mb-6">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-[0.16em] text-krypt-muted">
          {title}
        </h3>
        {description && (
          <p className="ml-4 max-w-md text-right text-xs text-krypt-dim">{description}</p>
        )}
      </div>
      {children}
    </div>
  );
}

/** Edits a stored 0-1 fraction but shows/accepts it as a human percent (e.g. 0.02 <-> "2%"). */
export function PercentInput({
  value, onChange, step = 1, min = 0, max = 100, disabled,
}: {
  value: number;
  onChange: (fraction: number) => void;
  step?: number;
  min?: number;
  max?: number;
  disabled?: boolean;
}) {
  return (
    <NumberInput
      value={Math.round((value || 0) * 10000) / 100}
      step={step}
      min={min}
      max={max}
      suffix="%"
      disabled={disabled}
      onChange={(v) => onChange(v / 100)}
    />
  );
}
