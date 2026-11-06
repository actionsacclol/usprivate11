import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, BookOpen, Bot, CheckCircle2, ClipboardCopy, Code2, Eye,
  FileDown, FileUp, FlaskConical, Play, Plus, ShieldAlert, ShieldCheck,
  Trash2, X, Zap,
} from 'lucide-react';
import type {
  ScriptApiDocs, ScriptAudit, ScriptBacktest, ScriptShadowOrder, UserScript,
} from '@shared/types';
import { Page, Card } from '../components/common';
import { TooFewTrades, tooFewTrades } from '../components/TooFewTrades';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { cls, fmtUsd } from '../utils/format';

const ScriptEditor = lazy(() =>
  import('../components/ScriptEditor').then((m) => ({ default: m.ScriptEditor })));

const NEW_SCRIPT_TEMPLATE = `# krypt-script v1
# name: My Strategy
# description: Describe what this strategy does.

def decide(ctx):
    ml = ctx["minsLeft"]
    if ml is None or ml > 3.0:
        return None
    fav = ctx["favorite"]
    if fav not in ("up", "down"):
        return None
    ask = ctx["upAsk"] if fav == "up" else ctx["downAsk"]
    if ask is None:
        return None  # no real order book this tick - never trade a phantom quote
    if 0.80 <= ask <= 0.95:
        return {"side": fav, "price": "ask", "reason": "late favorite"}
    return None
`;

const BT_WINDOWS = [7, 14, 30, 60];
/** Coins the crypto snapshot can offer decide(). Mirrors C15_ALL_ASSETS, but
 *  a script's scope is its OWN property — picking coins here never touches the
 *  15m Crypto engine's settings, and that engine's settings never touch this. */
const SCRIPT_ASSETS = ['BTC', 'ETH', 'SOL', 'XRP'];

type PanelTab = 'shadow' | 'backtest' | 'risk' | 'log' | 'docs' | 'ai';

/** The sidebar used to show ENABLED/off, which is not the question a user is
 *  asking — a script can read ENABLED and be running zero times because a
 *  gate somewhere else is holding it. These render the backend's real state. */
const STATE_LABEL: Record<string, string> = {
  off: 'off',
  blocked: 'BLOCKED',
  error: 'ERROR',
  starting: 'starting…',
  running: 'RUNNING',
};
const STATE_TONE: Record<string, string> = {
  off: 'text-krypt-dim',
  blocked: 'text-krypt-warn',
  error: 'text-krypt-loss',
  starting: 'text-krypt-muted',
  running: 'text-krypt-win',
};

const SEVERITY_TONE: Record<string, string> = {
  critical: 'border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss',
  warning: 'border-krypt-warn/40 bg-krypt-warn/10 text-krypt-warn',
  info: 'border-krypt-border bg-krypt-surface2/40 text-krypt-muted',
};

/** The backend files credential access under 'wallet'; there is none here. */
const auditCategoryLabel = (c: string): string => (c === 'wallet' ? 'credentials' : c);

export function ScriptsPage() {
  const { config } = useApp();
  const toast = useToast();
  const [scripts, setScripts] = useState<UserScript[]>([]);
  const [selId, setSelId] = useState<string | null>(null);
  const [code, setCode] = useState('');
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [tab, setTab] = useState<PanelTab>('backtest');
  const [btDays, setBtDays] = useState(30);
  const [btRes, setBtRes] = useState<ScriptBacktest | null>(null);
  const [shadow, setShadow] = useState<ScriptShadowOrder[] | null>(null);
  const [logs, setLogs] = useState<Record<string, string[]>>({});
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [armModal, setArmModal] = useState(false);
  /** Risk findings for the code currently in the EDITOR (the saved script's
   *  own audit rides on `sel.audit`). Refreshed on every validate/save. */
  const [audit, setAudit] = useState<ScriptAudit | null>(null);
  const [packText, setPackText] = useState<string | null>(null);
  const [showPack, setShowPack] = useState(false);
  const [docs, setDocs] = useState<ScriptApiDocs | null>(null);
  const [pasteText, setPasteText] = useState('');
  const codeRef = useRef(code);
  codeRef.current = code;

  const sel = useMemo(
    () => scripts.find((s) => s.id === selId) ?? null,
    [scripts, selId],
  );

  const refresh = async (keepSel = true) => {
    try {
      const r = await window.krypt.scripts.list();
      setScripts(r.scripts);
      if (!keepSel || !r.scripts.some((s) => s.id === selId)) {
        setSelId(r.scripts[0]?.id ?? null);
      }
    } catch {
      /* backend not up yet */
    }
  };

  useEffect(() => {
    void refresh(false);
    // Loaded up front (not just when the API tab opens) because the editor's
    // autocomplete is driven by this exact field list.
    window.krypt.scripts.docs().then(setDocs).catch(() => {});
  }, []);

  /** The shadow ledger: what the script WOULD have traded. Re-fetched when
   *  the selection changes, and polled while the tab is open so a shadow run
   *  fills in live rather than needing a page switch to refresh. */
  const loadShadow = async (id: string) => {
    try {
      const r = await window.krypt.scripts.shadowOrders(id, 200);
      setShadow(r.orders);
    } catch {
      setShadow([]);
    }
  };

  useEffect(() => {
    setShadow(null);
    if (selId) void loadShadow(selId);
  }, [selId]);

  useEffect(() => {
    if (tab !== 'shadow' || !selId) return;
    const t = setInterval(() => void loadShadow(selId), 15_000);
    return () => clearInterval(t);
  }, [tab, selId]);

  /** Inline validation for the editor: the SAME backend validator the save
   *  and enable paths use, so what you see while typing is what will gate
   *  the script — not a second, drifting copy of the rules in the UI. */
  const lintCode = async (src: string) => {
    if (!src.trim()) return [];
    const r = await window.krypt.scripts.validate(src);
    setAudit(r.audit);
    const parse = (msg: string, severity: 'error' | 'warning') => {
      const m = /^line (\d+):\s*(.*)$/.exec(msg);
      return m
        ? { line: Number(m[1]), message: m[2], severity }
        : { line: 0, message: msg, severity };
    };
    return [
      ...r.errors.map((e) => parse(e, 'error')),
      ...r.warnings.map((w) => parse(w, 'warning')),
    ];
  };

  // Backend push: auto-disables (script error / daily loss) + live log lines.
  useEffect(() => {
    const offStatus = window.krypt.scripts.onStatus((d) => {
      setScripts((cur) => cur.map((s) => (
        s.id === d.id ? { ...s, enabled: d.enabled, lastError: d.lastError ?? s.lastError } : s
      )));
      if (!d.enabled && d.lastError) toast.warn(`Script disabled: ${d.lastError.slice(0, 140)}`);
    });
    const offLog = window.krypt.scripts.onLog((d) => {
      setLogs((cur) => {
        const next = [...(cur[d.id] ?? []), ...d.lines].slice(-200);
        return { ...cur, [d.id]: next };
      });
    });
    return () => { offStatus(); offLog(); };
  }, [toast]);

  // Selecting a script loads its code into the editor.
  useEffect(() => {
    if (sel) {
      setCode(sel.code);
      setDirty(false);
      setErrors([]);
      setWarnings([]);
      setBtRes(null);
      setConfirmDelete(false);
      setAudit(sel.audit ?? null);
    }
  }, [selId]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async (): Promise<UserScript | null> => {
    if (!sel) return null;
    setBusy('save');
    try {
      const r = await window.krypt.scripts.save({ id: sel.id, code: codeRef.current });
      setErrors(r.errors);
      setWarnings(r.warnings);
      setAudit(r.audit ?? null);
      setDirty(false);
      await refresh();
      if (r.errors.length) toast.warn('Saved, but the script does not validate — it was disabled.');
      else if (r.disarmed) { setTab('risk'); toast.warn('Saved and DISABLED — the new code trips the risk audit. Check the Risk tab.'); }
      else if (r.audit && !r.audit.ok) { setTab('risk'); toast.warn(`Saved. Risk audit: ${r.audit.critical} critical finding(s).`); }
      else toast.success('Script saved.');
      return r.script;
    } catch (e: any) {
      toast.error(e?.message || String(e));
      return null;
    } finally {
      setBusy(null);
    }
  };

  const createScript = async (initialCode: string, name?: string) => {
    setBusy('create');
    try {
      const r = await window.krypt.scripts.save({ code: initialCode, name });
      await refresh(false);
      setSelId(r.script.id);
      setErrors(r.errors);
      setWarnings(r.warnings);
      if (r.errors.length) toast.warn('Imported with validation errors — fix before enabling.');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    } finally {
      setBusy(null);
    }
  };

  const validate = async () => {
    setBusy('validate');
    try {
      const r = await window.krypt.scripts.validate(codeRef.current);
      setErrors(r.errors);
      setWarnings(r.warnings);
      setAudit(r.audit);
      if (!r.ok) return;
      if (r.audit && !r.audit.ok) {
        setTab('risk');
        toast.warn(`Runs, but the risk audit found ${r.audit.critical} critical issue(s).`);
      } else {
        toast.success('Script is valid.');
      }
    } catch (e: any) {
      toast.error(e?.message || String(e));
    } finally {
      setBusy(null);
    }
  };

  const runBacktest = async () => {
    if (!sel) return;
    setBusy('backtest');
    setTab('backtest');
    try {
      // Always test what's in the editor (saving first if dirty).
      if (dirty) await save();
      const r = await window.krypt.scripts.backtest({ id: sel.id, sinceDays: btDays });
      setBtRes(r);
      if (!r) toast.error('Engine not running — start the backend first.');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    } finally {
      setBusy(null);
    }
  };

  const setEnabled = async (s: UserScript, enabled: boolean) => {
    try {
      const r = await window.krypt.scripts.setEnabled(s.id, enabled);
      setScripts((cur) => cur.map((x) => (x.id === s.id ? r.script : x)));
      // Shadow scripts start ticking immediately; only an ARMED one needs
      // the master switch, so only an armed one should be told about it.
      if (enabled && !s.dryRun && !config?.scriptsLiveEnabled) {
        toast.info('Script enabled, but it is ARMED and the master "Scripts live" switch is off — it will not run until you turn that on.');
      } else if (enabled) {
        toast.success('Script enabled — it starts recording shadow orders on the next tick.');
      }
    } catch (e: any) {
      toast.error(e?.message || String(e));
    }
  };

  /** Arming is the moment a script can spend real money, so the confirmation
   *  must describe the code that will ACTUALLY run — re-read the row (and its
   *  risk audit) from the backend first rather than trusting whatever this
   *  page last cached, which may predate an import, an AI paste, or an edit
   *  made from another window. */
  const openArmModal = async () => {
    await refresh();
    setArmModal(true);
  };

  const applyAssets = async (assets: string[] | null) => {
    if (!sel) return;
    try {
      const r = await window.krypt.scripts.setAssets(sel.id, assets);
      setScripts((cur) => cur.map((x) => (x.id === sel.id ? r.script : x)));
    } catch (e: any) {
      toast.error(e?.message || String(e));
    }
  };

  const importScript = async () => {
    try {
      const r = await window.krypt.scripts.importFile();
      if (!r.ok) {
        if (r.message !== 'canceled') toast.error(r.message || 'Import failed');
        return;
      }
      // Lands as a NEW script in shadow mode, disabled, and still has to pass
      // the validator before it can be enabled — importing is never arming.
      await createScript(r.data ?? '');
      toast.success('Imported. Review it, then validate and backtest before arming.');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    }
  };

  const exportScript = async () => {
    if (!sel) return;
    try {
      const r = await window.krypt.scripts.exportFile(sel.name, codeRef.current);
      if (!r.ok && r.message !== 'canceled') toast.error(r.message || 'Export failed');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    }
  };

  const applyDryRun = async (dryRun: boolean) => {
    if (!sel) return;
    try {
      const r = await window.krypt.scripts.setDryRun(sel.id, dryRun);
      setScripts((cur) => cur.map((x) => (x.id === sel.id ? r.script : x)));
      setArmModal(false);
      toast.info(dryRun
        ? 'Back to shadow — this script records intents instead of ordering.'
        : 'ARMED. This script now places real orders when it fires.');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    }
  };

  const doDelete = async () => {
    if (!sel) return;
    try {
      await window.krypt.scripts.delete(sel.id);
      setConfirmDelete(false);
      await refresh(false);
      toast.success('Script deleted.');
    } catch (e: any) {
      toast.error(e?.message || String(e));
    }
  };

  const openPack = async () => {
    setShowPack(true);
    if (!packText) {
      try {
        const r = await window.krypt.scripts.contextPack();
        setPackText(r.text);
      } catch (e: any) {
        toast.error(e?.message || String(e));
      }
    }
  };

  const copyPack = async () => {
    if (!packText) return;
    await navigator.clipboard.writeText(packText);
    toast.success('Context pack copied — paste it into any AI chat.');
  };

  const importPaste = async () => {
    const m = pasteText.match(/```(?:python)?\s*([\s\S]*?)```/);
    const extracted = (m ? m[1] : pasteText).trim();
    if (!extracted) return;
    await createScript(extracted + '\n');
    setPasteText('');
    setShowPack(false);
    setTab('backtest');
  };

  const selLogs = sel ? (logs[sel.id] ?? []) : [];

  return (
    <Page
      title="Scripts"
      subtitle="Write (or AI-generate) your own strategies, backtest them on your recorded data, then let them trade under hard safety rails. Scripts run independently — no other engine has to be switched on."
      actions={(
        <>
          <button onClick={() => void openPack()} className="krypt-btn-default inline-flex items-center gap-2">
            <Bot className="h-4 w-4" /> AI Context Pack
          </button>
          <button onClick={() => void importScript()} className="krypt-btn-default inline-flex items-center gap-2">
            <FileUp className="h-4 w-4" /> Import
          </button>
          <button
            onClick={() => void createScript(NEW_SCRIPT_TEMPLATE)}
            className="krypt-btn-primary inline-flex items-center gap-2"
          >
            <Plus className="h-4 w-4" /> New script
          </button>
        </>
      )}
    >
      {/* master switch + rails */}
      <Card className="mb-4">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <div className="flex items-center gap-3">
            <Toggle
              checked={!!config?.scriptsLiveEnabled}
              onChange={(v) => void window.krypt.config.update({ scriptsLiveEnabled: v })}
            />
            <div>
              <div className="text-sm font-semibold text-white">Scripts live</div>
              <div className="text-[11px] text-krypt-dim">
                Master switch for REAL orders. Shadow scripts run either way.
              </div>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-4 text-[11px] text-krypt-muted">
            <Rail label="Max entry" suffix="¢" value={config?.scriptMaxEntryCents ?? 97} onCommit={(v) => void window.krypt.config.update({ scriptMaxEntryCents: v })} />
            <Rail label="Max size" suffix=" lots" value={config?.scriptMaxContracts ?? 20} onCommit={(v) => void window.krypt.config.update({ scriptMaxContracts: v })} />
            <Rail label="Max open/script" value={config?.scriptMaxOpen ?? 2} onCommit={(v) => void window.krypt.config.update({ scriptMaxOpen: v })} />
            <Rail label="Daily loss stop $" value={config?.scriptDailyLossUsd ?? 25} onCommit={(v) => void window.krypt.config.update({ scriptDailyLossUsd: v })} />
            <Rail label="Markets/tick" value={config?.scriptMarketLimit ?? 150} title="How many general markets decide_market() is offered each tick, highest 24h volume first. 0 turns the hook off." onCommit={(v) => void window.krypt.config.update({ scriptMarketLimit: v })} />
            <Rail
              label="Max spread" suffix="¢"
              value={config?.scriptMarketMaxSpreadCents ?? 2}
              title="Widest real bid/ask a decide_market() entry may cross, checked against the live book at submit. A wide book can eat a thin-margin strategy's whole edge before it starts. 0 = off."
              onCommit={(v) => void window.krypt.config.update({ scriptMarketMaxSpreadCents: v })}
            />
            <span className="text-krypt-dim">(these apply to every script and cannot be raised from inside one)</span>
          </div>
        </div>
      </Card>

      <div className="flex min-h-[560px] gap-4">
        {/* ── script list ── */}
        <div className="w-64 shrink-0 space-y-2">
          {scripts.length === 0 && (
            <div className="rounded-xl border border-dashed border-krypt-border p-4 text-xs leading-relaxed text-krypt-dim">
              No scripts yet. Create one, or open the <b className="text-white">AI Context Pack</b>,
              paste it into ChatGPT/Claude/any AI, describe a strategy, and paste the result back.
            </div>
          )}
          {scripts.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelId(s.id)}
              className={cls(
                'w-full rounded-xl border p-3 text-left transition-colors',
                s.id === selId
                  ? 'border-krypt-purple/60 bg-krypt-purple/10'
                  : 'border-krypt-border bg-krypt-surface hover:border-krypt-purple/30',
              )}
            >
              <div className="flex items-center gap-2">
                <Code2 className="h-3.5 w-3.5 shrink-0 text-krypt-purple" />
                <span className="truncate text-sm text-white">{s.name}</span>
                {s.audit && !s.audit.ok && (
                  <ShieldAlert
                    className="h-3.5 w-3.5 shrink-0 text-krypt-loss"
                    aria-label={`${s.audit.critical} critical risk finding(s)`}
                  />
                )}
                {s.lastError && <span className="ml-auto h-2 w-2 shrink-0 rounded-full bg-krypt-loss shadow-[0_0_6px_currentColor]" />}
              </div>
              <div className="mt-1 flex items-center justify-between text-[11px]">
                <span className="flex items-center gap-1.5">
                  {/* The REAL state, not just the enabled flag: a script can
                      read ENABLED and be running zero times. */}
                  <span className={STATE_TONE[s.status?.state ?? 'off']}>
                    {STATE_LABEL[s.status?.state ?? 'off']}
                  </span>
                  {/* Shadow vs live is the difference between a simulation and
                      spending money — never let it be ambiguous at a glance. */}
                  <span className={s.dryRun ? 'text-krypt-purple' : 'text-krypt-loss'}>
                    {s.dryRun ? 'shadow' : 'LIVE'}
                  </span>
                </span>
                {(s.dryRun ? s.shadowStats : s.stats) && (
                  <span className="font-mono text-krypt-dim">
                    {(s.dryRun ? s.shadowStats! : s.stats!).wins}W/
                    {(s.dryRun ? s.shadowStats! : s.stats!).losses}L{' '}
                    <span className={(s.dryRun ? s.shadowStats! : s.stats!).pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
                      {fmtUsd((s.dryRun ? s.shadowStats! : s.stats!).pnlUsd, { sign: true })}
                    </span>
                  </span>
                )}
              </div>
            </button>
          ))}
        </div>

        {/* ── editor + panels ── */}
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          {sel ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <div className="mr-auto min-w-0">
                  <div className="truncate text-sm font-semibold text-white">
                    {sel.name}{dirty && <span className="text-krypt-warn"> •</span>}
                  </div>
                  {sel.description && (
                    <div className="truncate text-[11px] text-krypt-dim">{sel.description}</div>
                  )}
                </div>
                <label className="flex items-center gap-1.5 text-[11px] text-krypt-muted">
                  <Toggle checked={sel.enabled} onChange={(v) => void setEnabled(sel, v)} />
                  Enabled
                </label>
                <button
                  onClick={() => (sel.dryRun ? void openArmModal() : void applyDryRun(true))}
                  className={cls(
                    'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition-colors',
                    sel.dryRun
                      ? 'border-krypt-border bg-krypt-purple/10 text-krypt-purple'
                      : 'border-krypt-loss/50 bg-krypt-loss/10 text-krypt-loss',
                  )}
                  title={sel.dryRun
                    ? 'Shadow — records what it would trade. Click to arm for real orders.'
                    : 'ARMED — places real orders. Click to return to shadow.'}
                >
                  {sel.dryRun ? <Eye className="h-3.5 w-3.5" /> : <Zap className="h-3.5 w-3.5" />}
                  {sel.dryRun ? 'Shadow' : 'Live'}
                </button>
                <button
                  onClick={() => setTab('risk')}
                  className={cls(
                    'inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs transition-colors',
                    (audit ?? sel.audit)?.ok === false
                      ? 'border-krypt-loss/50 bg-krypt-loss/10 text-krypt-loss'
                      : 'border-krypt-border text-krypt-muted hover:text-white',
                  )}
                  title="What this script's code does — network, filesystem, credential and dynamic-execution access. Scripts are full Python, so read this before enabling one you didn't write."
                >
                  {(audit ?? sel.audit)?.ok === false
                    ? <ShieldAlert className="h-3.5 w-3.5" />
                    : <ShieldCheck className="h-3.5 w-3.5" />}
                  {(audit ?? sel.audit)?.ok === false
                    ? `Risk (${(audit ?? sel.audit)!.critical})`
                    : 'Risk'}
                </button>
                <button onClick={() => void exportScript()} className="rounded-md border border-krypt-border p-1.5 text-krypt-dim hover:text-white" title="Export this script to a .py file">
                  <FileUp className="h-3.5 w-3.5 rotate-180" />
                </button>
                <button onClick={() => void validate()} disabled={busy !== null} className="krypt-btn-default text-xs">
                  Validate
                </button>
                <button onClick={() => void save()} disabled={busy !== null || !dirty} className="krypt-btn-primary text-xs">
                  {busy === 'save' ? 'Saving…' : 'Save'}
                </button>
                {confirmDelete ? (
                  <button onClick={() => void doDelete()} className="inline-flex items-center gap-1 rounded-md border border-krypt-loss/60 bg-krypt-loss/10 px-2.5 py-1.5 text-xs text-krypt-loss">
                    <Trash2 className="h-3.5 w-3.5" /> Confirm delete
                  </button>
                ) : (
                  <button onClick={() => setConfirmDelete(true)} className="rounded-md border border-krypt-border p-1.5 text-krypt-dim hover:text-krypt-loss" title="Delete script">
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>

              <StatusLine script={sel} onScope={(a) => void applyAssets(a)} />

              {sel.lastError && (
                <div className="flex items-start gap-2 rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-[11px] text-krypt-loss">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span className="min-w-0 break-words">Last error: {sel.lastError}</span>
                </div>
              )}
              {errors.map((e, i) => (
                <div key={i} className="rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-1.5 text-[11px] text-krypt-loss">{e}</div>
              ))}
              {warnings.map((w, i) => (
                <div key={i} className="rounded-lg border border-krypt-warn/40 bg-krypt-warn/10 px-3 py-1.5 text-[11px] text-krypt-warn">{w}</div>
              ))}

              <div className="h-[340px]">
                <Suspense fallback={<div className="grid h-full place-items-center text-xs text-krypt-dim">Loading editor…</div>}>
                  <ScriptEditor
                    value={code}
                    onChange={(c) => { setCode(c); setDirty(true); }}
                    fields={docs?.fields}
                    lintSource={lintCode}
                  />
                </Suspense>
              </div>

              {/* panel tabs */}
              <div className="flex items-center gap-1.5">
                {(['shadow', 'backtest', 'risk', 'log', 'docs', 'ai'] as PanelTab[]).map((t) => (
                  <button
                    key={t}
                    onClick={() => {
                      if (t === 'ai') { void openPack(); return; }
                      setTab(t);
                      if (t === 'docs' && !docs) {
                        window.krypt.scripts.docs().then(setDocs).catch(() => {});
                      }
                    }}
                    className={cls(
                      'rounded-md px-3 py-1.5 text-xs transition-colors',
                      tab === t ? 'bg-white/[0.08] text-white' : 'text-krypt-muted hover:text-white',
                    )}
                  >
                    {t === 'shadow' ? `Shadow${shadow?.length ? ` (${shadow.length})` : ''}`
                      : t === 'backtest' ? 'Backtest'
                        : t === 'risk' ? `Risk${(audit ?? sel.audit)?.critical ? ` (${(audit ?? sel.audit)!.critical})` : ''}`
                          : t === 'log' ? `Script log${selLogs.length ? ` (${selLogs.length})` : ''}`
                            : t === 'docs' ? 'API Reference' : 'AI Context Pack'}
                  </button>
                ))}
                {tab === 'backtest' && (
                  <div className="ml-auto flex items-center gap-1.5">
                    {BT_WINDOWS.map((d) => (
                      <button
                        key={d}
                        onClick={() => setBtDays(d)}
                        className={cls(
                          'rounded px-2 py-1 text-[11px]',
                          btDays === d ? 'bg-krypt-purple/20 text-krypt-purple' : 'text-krypt-dim hover:text-white',
                        )}
                      >
                        {d}d
                      </button>
                    ))}
                    <button
                      onClick={() => void runBacktest()}
                      disabled={busy !== null}
                      className="inline-flex items-center gap-1.5 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-3 py-1.5 text-xs text-krypt-purple hover:bg-krypt-purple/20 disabled:opacity-50"
                    >
                      <FlaskConical className="h-3.5 w-3.5" />
                      {busy === 'backtest' ? 'Replaying…' : 'Run backtest'}
                    </button>
                  </div>
                )}
              </div>

              {tab === 'shadow' && (
                <ShadowLedger
                  orders={shadow}
                  dryRun={sel.dryRun}
                  onRefresh={() => void loadShadow(sel.id)}
                />
              )}
              {tab === 'backtest' && <BacktestResult res={btRes} busy={busy === 'backtest'} />}
              {tab === 'risk' && <AuditPanel audit={audit ?? sel.audit} stale={code !== sel.code} />}
              {tab === 'docs' && <DocsPanel docs={docs} />}
              {tab === 'log' && (
                <div className="max-h-56 overflow-y-auto rounded-lg border border-krypt-border bg-krypt-void/50 p-3 font-mono text-[11px] leading-relaxed text-krypt-muted">
                  {selLogs.length === 0
                    ? <span className="text-krypt-dim">No log lines yet — call log("…") in your script; lines appear here while it runs live.</span>
                    : selLogs.map((l, i) => <div key={i}>{l}</div>)}
                </div>
              )}
            </>
          ) : (
            <div className="grid flex-1 place-items-center rounded-xl border border-dashed border-krypt-border text-sm text-krypt-dim">
              Select or create a script
            </div>
          )}
        </div>
      </div>

      {/* ── arm-for-real-orders modal ── */}
      {armModal && sel && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onMouseDown={() => setArmModal(false)}>
          <div className="w-full max-w-md rounded-xl border border-krypt-loss/50 bg-krypt-surface p-5" onMouseDown={(e) => e.stopPropagation()}>
            <div className="flex items-center gap-2 text-krypt-loss">
              <Zap className="h-5 w-5" />
              <h3 className="text-sm font-semibold">Arm “{sel.name}” for real orders?</h3>
            </div>
            <div className="mt-3 space-y-2 text-xs leading-relaxed text-krypt-muted">
              <p>
                In shadow mode this script runs against live ticks and records every order it
                <i> would</i> have placed. Arming it means the next time it fires, it spends
                <b className="text-white"> real money</b> on your Polymarket US account.
              </p>
              {sel.shadowStats && sel.shadowStats.n > 0 ? (
                <p>
                  Its shadow record so far: <b className="text-white">{sel.shadowStats.n}</b> order(s),{' '}
                  {sel.shadowStats.wins}W / {sel.shadowStats.losses}L, simulated{' '}
                  <b className={sel.shadowStats.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
                    {fmtUsd(sel.shadowStats.pnlUsd)}
                  </b>.
                </p>
              ) : (
                <p className="text-krypt-warn">
                  This script has <b>no shadow record yet</b> — nothing has been observed about how it
                  behaves on live data. Consider letting it run in shadow first.
                </p>
              )}
              <p>
                Shadow simulates <b className="text-white">entries only</b>: exits (manage() sells,
                take-profit and stop-loss) need a real position, so shadow orders are always held to
                settlement. A script that depends on its exits will behave differently live.
              </p>
              <p>
                <b className="text-white">supervise() does not run in shadow</b> — it changes the real
                engine&apos;s settings, so a shadow script never touches them. Arming turns it on, and
                from then on this script can retune the crypto engine and switch engines off.
              </p>
              {sel.audit && !sel.audit.ok && (
                <div className="rounded-lg border border-krypt-loss/50 bg-krypt-loss/15 p-2.5">
                  <div className="flex items-center gap-1.5 font-semibold text-krypt-loss">
                    <ShieldAlert className="h-3.5 w-3.5" />
                    The risk audit flagged this script
                  </div>
                  <p className="mt-1 text-krypt-muted">
                    {sel.audit.critical} critical finding(s):{' '}
                    {sel.audit.categories.map(auditCategoryLabel).join(', ')}. Scripts run as full Python inside the
                    process holding your <b className="text-krypt-warn">decrypted API key</b>.
                    Do not arm code you have not read — check the Risk tab first.
                  </p>
                </div>
              )}
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button onClick={() => setArmModal(false)} className="krypt-btn-default">Cancel</button>
              <button
                onClick={() => void applyDryRun(false)}
                className="rounded-md border border-krypt-loss/60 bg-krypt-loss/15 px-3 py-1.5 text-xs font-semibold text-krypt-loss"
              >
                Arm for real orders
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── AI context pack modal ── */}
      {showPack && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onMouseDown={() => setShowPack(false)}>
          <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-xl border border-krypt-border bg-krypt-surface p-5" onMouseDown={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Bot className="h-4 w-4 text-krypt-purple" />
                <h3 className="text-sm font-semibold text-white">AI Context Pack</h3>
              </div>
              <button onClick={() => setShowPack(false)} className="text-krypt-dim hover:text-white"><X className="h-4 w-4" /></button>
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">
              1. Copy the pack. 2. Paste it into <b className="text-white">any</b> AI chat (ChatGPT, Claude, Gemini…)
              and describe the strategy you want. 3. Paste the AI's reply below — the script is imported,
              validated, and ready to backtest. It includes your live script API, field docs, safety rails,
              and your actual recorded-data inventory.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <button
                onClick={() => void copyPack()}
                disabled={!packText}
                className="krypt-btn-primary inline-flex items-center gap-2 text-xs disabled:opacity-50"
              >
                <ClipboardCopy className="h-3.5 w-3.5" />
                {packText ? 'Copy context pack' : 'Generating…'}
              </button>
              <button
                onClick={() => {
                  void window.krypt.scripts.exportPack().then((r) => {
                    if (r.ok) toast.success('Saved — the file window should pop up.');
                    else if (r.message !== 'canceled') toast.error(r.message || 'Export failed');
                  });
                }}
                disabled={!packText}
                className="krypt-btn-default inline-flex items-center gap-2 text-xs disabled:opacity-50"
                title="Save the full pack as a .txt (handy for AI apps that take file uploads)"
              >
                <FileDown className="h-3.5 w-3.5" /> Export .txt
              </button>
              {packText && (
                <span className="inline-flex items-center gap-1 text-[11px] text-krypt-win">
                  <CheckCircle2 className="h-3.5 w-3.5" /> {Math.round(packText.length / 1000)}k chars, built from your live config + data
                </span>
              )}
            </div>
            <div className="mt-3 min-h-0 flex-1 overflow-y-auto rounded-lg border border-krypt-border bg-krypt-void/50 p-3 font-mono text-[10px] leading-relaxed text-krypt-dim whitespace-pre-wrap">
              {packText ? `${packText.slice(0, 2500)}\n…` : 'Building the pack from your config, sandbox and recorded data…'}
            </div>
            <div className="mt-3">
              <div className="text-[11px] font-semibold uppercase tracking-wide text-krypt-muted">Paste the AI's reply</div>
              <textarea
                value={pasteText}
                onChange={(e) => setPasteText(e.target.value)}
                placeholder={'Paste the AI response here (the ```python block is extracted automatically)…'}
                className="krypt-input mt-1.5 h-24 w-full resize-none font-mono text-[11px]"
              />
              <div className="mt-2 flex justify-end">
                <button
                  onClick={() => void importPaste()}
                  disabled={!pasteText.trim() || busy !== null}
                  className="krypt-btn-primary inline-flex items-center gap-2 text-xs disabled:opacity-50"
                >
                  <Play className="h-3.5 w-3.5" /> Import as script
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </Page>
  );
}

/** The one line the page never had: is this script ACTUALLY running, and if
 *  not, what is stopping it? Every gate is evaluated backend-side (that's
 *  where they're applied), so this can't drift from the truth the way a
 *  re-derivation from `enabled` + `dryRun` in the renderer always did. */
function StatusLine({ script, onScope }: {
  script: UserScript;
  onScope: (assets: string[] | null) => void;
}) {
  const [editScope, setEditScope] = useState(false);
  const st = script.status;
  const scope = script.assets;
  if (!st) return null;

  const toggleAsset = (a: string) => {
    const cur = scope ?? [...SCRIPT_ASSETS];
    const next = cur.includes(a) ? cur.filter((x) => x !== a) : [...cur, a];
    // Every coin selected is the same thing as "no scope" — store it that way
    // so a script doesn't silently miss a coin added in a later release.
    onScope(next.length === 0 || next.length === SCRIPT_ASSETS.length ? null : next);
  };

  return (
    <div className={cls(
      'rounded-lg border px-3 py-2 text-[11px]',
      st.state === 'running' ? 'border-krypt-win/30 bg-krypt-win/[0.06]'
        : st.state === 'blocked' ? 'border-krypt-warn/40 bg-krypt-warn/10'
          : st.state === 'error' ? 'border-krypt-loss/40 bg-krypt-loss/10'
            : 'border-krypt-border bg-krypt-surface2/30',
    )}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={cls('font-semibold uppercase tracking-wide', STATE_TONE[st.state])}>
          {STATE_LABEL[st.state]}
        </span>
        <span className="min-w-0 flex-1 text-krypt-muted">{st.detail}</span>
        <button
          onClick={() => setEditScope((v) => !v)}
          className="shrink-0 text-krypt-purple hover:underline"
          title="Which coins this script's decide() hook is offered. This is the script's own setting — the 15m Crypto tab's asset checkboxes do not affect it."
        >
          Coins: {scope ? scope.join(', ') : 'all'}
        </button>
      </div>
      {editScope && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-krypt-border/60 pt-2">
          {SCRIPT_ASSETS.map((a) => {
            const on = !scope || scope.includes(a);
            return (
              <button
                key={a}
                onClick={() => toggleAsset(a)}
                className={cls(
                  'rounded px-2 py-0.5 font-mono text-[10px] transition-colors',
                  on ? 'bg-krypt-purple/20 text-krypt-purple' : 'bg-krypt-surface2 text-krypt-dim',
                )}
              >
                {a}
              </button>
            );
          })}
          <span className="ml-2 text-[10px] text-krypt-dim">
            This script only. Independent of the 15m Crypto engine.
          </span>
        </div>
      )}
    </div>
  );
}

/** What the script's code actually does. This replaced the language sandbox:
 *  scripts are full Python now, so the app reviews rather than restricts —
 *  and a review nobody reads is worth nothing, which is why a critical
 *  finding pushes the user here from save, validate and arm. */
function AuditPanel({ audit, stale }: { audit: ScriptAudit | null; stale: boolean }) {
  if (!audit) {
    return (
      <div className="rounded-lg border border-krypt-border p-6 text-center text-xs text-krypt-dim">
        Save or validate the script to scan it.
      </div>
    );
  }
  return (
    <div className="space-y-2 rounded-lg border border-krypt-border bg-krypt-void/40 p-3">
      <div className="flex items-start gap-2">
        {audit.ok
          ? <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-krypt-win" />
          : <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-krypt-loss" />}
        <div className="min-w-0">
          <div className={cls('text-xs font-semibold',
            audit.ok ? 'text-krypt-win' : 'text-krypt-loss')}>
            {audit.summary}
          </div>
          {stale && (
            <div className="mt-0.5 text-[10px] text-krypt-warn">
              The editor differs from the saved script — this scan covers what is
              saved. Save or Validate to rescan.
            </div>
          )}
        </div>
      </div>

      {audit.findings.length > 0 && (
        <div className="max-h-64 space-y-1 overflow-y-auto">
          {audit.findings.map((f, i) => (
            <div
              key={i}
              className={cls('rounded border px-2 py-1.5 text-[11px] leading-relaxed',
                SEVERITY_TONE[f.severity] ?? SEVERITY_TONE.info)}
            >
              <span className="font-mono text-[10px] uppercase opacity-70">
                {f.severity}
                {f.line ? ` · line ${f.line}` : ''} · {auditCategoryLabel(f.category)}
              </span>
              <div className="mt-0.5">{f.message}</div>
            </div>
          ))}
        </div>
      )}

      <p className="border-t border-krypt-border/60 pt-2 text-[10px] leading-relaxed text-krypt-dim">
        Scripts run as <b className="text-krypt-muted">full Python</b>, in the same process
        that holds your <b className="text-krypt-warn">decrypted API key</b>. This scan reads
        the source and reports what it recognizes — it is a smoke detector, not a lock, and it
        cannot see through deliberately obfuscated code (which is why obfuscation is itself
        reported as critical). Read anything you did not write, especially code an AI generated
        or someone sent you. The money rails at the top of this page still apply to every
        script and cannot be raised from inside one.
      </p>
    </div>
  );
}

function BacktestResult({ res, busy }: { res: ScriptBacktest | null; busy: boolean }) {
  if (busy) {
    return <div className="rounded-lg border border-krypt-border p-6 text-center text-xs text-krypt-dim">Replaying your script over every recorded window…</div>;
  }
  if (!res) {
    return (
      <div className="rounded-lg border border-dashed border-krypt-border p-6 text-center text-xs leading-relaxed text-krypt-dim">
        Run a backtest to replay this script over the app's recorded real-book ticks —
        taker fills at the recorded ask, Polymarket US fees included, same safety rails as live.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {res.scriptError && (
        <div className="rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-[11px] text-krypt-loss">
          Script died mid-run: {res.scriptError}
        </div>
      )}
      <TooFewTrades n={res.n} />
      <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-6">
        <Stat label="Trades" value={`${res.n}`} sub={`${res.windowsScanned} windows`} />
        <Stat label="Win rate" value={res.n ? `${(res.winRate * 100).toFixed(1)}%` : '—'} />
        {/* No trades = no edge to show: a green "0.00¢" reads as a verdict. */}
        <Stat label="Edge / contract" value={res.n ? `${res.netEvCentsPerContract.toFixed(2)}¢` : '—'} tone={!res.n ? undefined : res.netEvCentsPerContract >= 0 ? 'good' : 'bad'} />
        <Stat label="Total P&L" value={res.n ? fmtUsd(res.totalPnlUsd, { sign: true }) : '—'} tone={!res.n ? undefined : res.totalPnlUsd >= 0 ? 'good' : 'bad'} />
        {/* Clustered by close time: the coins closing together move together. */}
        {/* On a tiny sample this reads 20 from 5 trades, which looks like proof. */}
        {tooFewTrades(res.n)
          ? <Stat label="t-stat" value="—" sub="too few trades" />
          : <Stat label="t-stat" value={res.tStat != null ? res.tStat.toFixed(2) : '—'} sub={`${res.tStat != null && Math.abs(res.tStat) >= 2 ? 'significant-ish' : 'noise-level'}${res.nClusters ? ` · ${res.nClusters} closes` : ''}`} />}
        <Stat label="Max drawdown" value={fmtUsd(res.maxDrawdownUsd)} tone="bad" />
      </div>
      {Object.keys(res.byAsset).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {Object.entries(res.byAsset).map(([a, st]) => (
            <span key={a} className="rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">
              {a} {st.wins}/{st.n}{' '}
              <span className={st.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>{fmtUsd(st.pnlUsd, { sign: true })}</span>
            </span>
          ))}
        </div>
      )}
      {(res.scriptLogs?.length ?? 0) > 0 && (
        <div className="max-h-32 overflow-y-auto rounded-lg border border-krypt-border bg-krypt-void/50 p-2 font-mono text-[10px] text-krypt-muted">
          {res.scriptLogs!.map((l, i) => <div key={i}>{l}</div>)}
        </div>
      )}
      <ul className="space-y-0.5">
        {res.caveats.map((c, i) => (
          <li key={i} className="text-[10px] leading-relaxed text-krypt-warn/80">⚠ {c}</li>
        ))}
      </ul>
    </div>
  );
}

function DocsPanel({ docs }: { docs: ScriptApiDocs | null }) {
  const [showExamples, setShowExamples] = useState(false);
  if (!docs) {
    return <div className="rounded-lg border border-krypt-border p-6 text-center text-xs text-krypt-dim">Loading the API reference…</div>;
  }
  return (
    <div className="max-h-[420px] space-y-3 overflow-y-auto rounded-lg border border-krypt-border bg-krypt-void/40 p-4">
      <div className="flex items-center gap-2 text-sm font-semibold text-white">
        <BookOpen className="h-4 w-4 text-krypt-purple" /> Script API
        <span className="text-[10px] font-normal text-krypt-dim">
          — generated live from the engine; always current
        </span>
      </div>
      <pre className="whitespace-pre-wrap rounded-lg bg-krypt-surface2/50 p-3 font-mono text-[10.5px] leading-relaxed text-krypt-muted">
        {docs.contract}
      </pre>

      <div className="text-xs font-semibold uppercase tracking-wide text-krypt-muted">
        ctx fields ({docs.fields.length})
      </div>
      <table className="w-full text-left text-[11px]">
        <tbody>
          {docs.fields.map((f) => (
            <tr key={f.name} className="border-t border-krypt-border/50 align-top">
              <td className="whitespace-nowrap py-1 pr-3 font-mono text-krypt-purple">ctx["{f.name}"]</td>
              <td className="py-1 pr-3 text-krypt-muted">{f.doc}</td>
              <td className="py-1">
                {f.backtestable
                  ? <span className="rounded bg-krypt-win/10 px-1.5 py-0.5 text-[9px] uppercase text-krypt-win">backtestable</span>
                  : <span className="rounded bg-krypt-warn/10 px-1.5 py-0.5 text-[9px] uppercase text-krypt-warn">live-only</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="text-xs font-semibold uppercase tracking-wide text-krypt-muted">
        Language &amp; runtime
      </div>
      <div className="space-y-1 text-[11px] leading-relaxed text-krypt-muted">
        <div>
          Scripts are <b className="text-white">full Python</b> — imports, classes and the
          standard library all work. What the code does is <i>reviewed</i>, not restricted:
          see the <b className="text-white">Risk</b> tab.
        </div>
        <div>
          Injected without an import:{' '}
          {(docs.injected ?? []).map((b) => (
            <span key={b} className="mr-1 rounded bg-krypt-surface2 px-1.5 py-0.5 font-mono text-[10px] text-krypt-dim">{b}</span>
          ))}
        </div>
        <div>
          Hooks are called <b className="text-white">synchronously on the engine loop</b> and must
          return within <b className="text-white">{docs.hookTimeoutSec ?? 1}s</b>. A hook that
          blocks past it is abandoned and the script is disabled — so no sleeping, no network
          calls, no heavy per-tick loops.
        </div>
      </div>

      <div className="text-xs font-semibold uppercase tracking-wide text-krypt-muted">Safety rails (current values)</div>
      <div className="text-[11px] text-krypt-muted">
        Max entry <b className="text-white">{docs.rails.maxEntryCents}¢</b> · max order{' '}
        <b className="text-white">{docs.rails.maxContracts}</b> contracts · max open/script{' '}
        <b className="text-white">{docs.rails.maxOpen}</b> · daily loss stop{' '}
        <b className="text-white">${docs.rails.dailyLossUsd}</b> · default size{' '}
        <b className="text-white">{docs.rails.defaultOrderSize}</b> contracts.
        These apply to every script and cannot be raised from inside one.
      </div>

      <button onClick={() => setShowExamples(!showExamples)} className="text-[11px] text-krypt-purple underline-offset-2 hover:underline">
        {showExamples ? 'Hide' : 'Show'} example scripts ({docs.examples.length})
      </button>
      {showExamples && docs.examples.map((ex) => (
        <div key={ex.name}>
          <div className="mb-1 text-[11px] font-semibold text-white">{ex.name}</div>
          <pre className="overflow-x-auto rounded-lg bg-krypt-surface2/50 p-3 font-mono text-[10.5px] leading-relaxed text-krypt-muted">{ex.code}</pre>
        </div>
      ))}
    </div>
  );
}

function Rail({ label, value, suffix, title, onCommit }: {
  label: string; value: number; suffix?: string; title?: string;
  onCommit: (v: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => { setDraft(String(value)); }, [value]);
  const commit = () => {
    // All money rails are non-negative; the backend clamps negatives to 0
    // (= OFF), so clamp here too or the UI would keep showing a negative while
    // the rail is silently disabled.
    const v = Math.max(0, Number(draft));
    if (Number.isFinite(v) && v !== value) { onCommit(v); setDraft(String(v)); }
    else setDraft(String(value));
  };
  return (
    <label className="flex items-center gap-1" title={title}>
      <span className={title ? 'decoration-krypt-dim/60 underline-offset-2 hover:underline' : undefined}>{label}</span>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); }}
        className="w-14 rounded border border-krypt-border bg-krypt-void/60 px-1.5 py-0.5 text-center font-mono text-[11px] text-white outline-none focus:border-krypt-purple/60"
      />
      {suffix && <span>{suffix}</span>}
    </label>
  );
}

/** The shadow ledger — every order the script WOULD have placed, settled from
 *  the real market outcome. This is where a shadow run actually shows its
 *  work; the W/L figure in the sidebar is only the summary of it. */
function ShadowLedger({
  orders, dryRun, onRefresh,
}: {
  orders: ScriptShadowOrder[] | null;
  dryRun: boolean;
  onRefresh: () => void;
}) {
  if (orders === null) {
    return <div className="p-4 text-xs text-krypt-dim">Loading…</div>;
  }

  const real = orders.filter((o) => !o.refused);
  const settled = real.filter((o) => o.resolved);
  const pnl = settled.reduce((a, o) => a + (o.pnlUsd ?? 0), 0);
  const wins = settled.filter((o) => o.won).length;

  return (
    <div className="rounded-lg border border-krypt-border bg-krypt-void/50">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-krypt-border px-3 py-2 text-[11px]">
        <span className="text-krypt-muted">
          <b className="text-white">{real.length}</b> would-be order(s)
        </span>
        <span className="text-krypt-muted">
          <b className="text-white">{settled.length}</b> settled ·{' '}
          {wins}W/{settled.length - wins}L
        </span>
        <span className="text-krypt-muted">
          simulated{' '}
          <b className={pnl >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
            {fmtUsd(pnl, { sign: true })}
          </b>
        </span>
        {orders.length > real.length && (
          <span className="text-krypt-dim">
            {orders.length - real.length} refused by rails
          </span>
        )}
        <button onClick={onRefresh} className="ml-auto text-krypt-purple hover:underline">
          Refresh
        </button>
      </div>

      {orders.length === 0 ? (
        <div className="p-4 text-xs leading-relaxed text-krypt-dim">
          {dryRun
            ? 'Nothing yet. The script is in shadow, so this fills in as it finds entries — a selective strategy can legitimately go hours or days without firing. Entries settle automatically once the market resolves.'
            : 'This script is armed for real orders, so its trades appear in Positions and History rather than here. The shadow ledger only records what a script in Shadow mode would have done.'}
        </div>
      ) : (
        <div className="max-h-56 overflow-y-auto">
          <table className="w-full text-left text-[11px]">
            <thead className="sticky top-0 bg-krypt-surface text-krypt-dim">
              <tr>
                <th className="px-3 py-1.5 font-normal">When</th>
                <th className="px-3 py-1.5 font-normal">Market</th>
                <th className="px-3 py-1.5 font-normal">Side</th>
                <th className="px-3 py-1.5 text-right font-normal">Size</th>
                <th className="px-3 py-1.5 text-right font-normal">Entry</th>
                <th className="px-3 py-1.5 font-normal">Result</th>
                <th className="px-3 py-1.5 text-right font-normal">P&amp;L</th>
                <th className="px-3 py-1.5 font-normal">Why</th>
              </tr>
            </thead>
            <tbody className="font-mono text-krypt-muted">
              {orders.map((o) => (
                <tr key={o.id} className="border-t border-krypt-border/50">
                  <td className="whitespace-nowrap px-3 py-1.5">
                    {(o.at || '').replace('T', ' ').slice(5, 16)}
                  </td>
                  <td className="max-w-[13rem] truncate px-3 py-1.5 text-white">
                    {o.asset || o.ticker}
                  </td>
                  <td className="px-3 py-1.5 uppercase">{o.side}</td>
                  <td className="px-3 py-1.5 text-right">{o.refused ? '—' : o.contracts}</td>
                  <td className="px-3 py-1.5 text-right">
                    {o.refused ? '—' : `${o.entryCents}c`}
                  </td>
                  <td className="px-3 py-1.5">
                    {o.refused
                      ? <span className="text-krypt-dim">refused</span>
                      : !o.resolved
                        ? <span className="text-krypt-purple">open</span>
                        : o.won
                          ? <span className="text-krypt-win">won</span>
                          : <span className="text-krypt-loss">lost</span>}
                  </td>
                  <td className="px-3 py-1.5 text-right">
                    {o.pnlUsd === null || o.pnlUsd === undefined ? '—' : (
                      <span className={o.pnlUsd >= 0 ? 'text-krypt-win' : 'text-krypt-loss'}>
                        {fmtUsd(o.pnlUsd, { sign: true })}
                      </span>
                    )}
                  </td>
                  <td className="max-w-[15rem] truncate px-3 py-1.5 text-krypt-dim">
                    {o.refused ? o.note : o.reason}
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

function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={cls(
        'relative h-5 w-9 shrink-0 rounded-full border transition-colors',
        checked ? 'border-krypt-purple/60 bg-krypt-purple/40' : 'border-krypt-border bg-krypt-surface2',
      )}
    >
      <span
        className={cls(
          'absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white transition-all',
          checked ? 'left-[18px]' : 'left-0.5',
        )}
      />
    </button>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: 'good' | 'bad' }) {
  return (
    <div className="rounded-lg bg-krypt-surface2/60 p-2">
      <div className="text-[10px] uppercase tracking-wide text-krypt-dim">{label}</div>
      <div className={cls('font-mono text-sm', tone === 'good' ? 'text-krypt-win' : tone === 'bad' ? 'text-krypt-loss' : 'text-white')}>{value}</div>
      {sub && <div className="text-[10px] text-krypt-dim">{sub}</div>}
    </div>
  );
}
