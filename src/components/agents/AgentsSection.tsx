import { useMemo, useState } from 'react';
import {
  Copy, Download, Pencil, PlugZap, Plus, RotateCcw, Trash2, Upload, X,
} from 'lucide-react';
import type { McpStatus, TraderConfig } from '@shared/types';
import type {
  AgentPaper, AgentScore, ForecastScoreboard, McpActivity, McpAgentStatus,
} from '@shared/market';
import {
  agentExportJson, agentsOf, DEFAULT_AGENT_ID, effectiveAgentMode, effectiveCaps, MAX_AGENTS,
  parseAgentImport, rulesInWords, type McpAgent,
} from '@shared/agents';
import { ConfirmDialog, Modal, Section } from '../common';
import { GlassButton } from '../glass/GlassButton';
import { GlassSwitch } from '../glass/GlassSwitch';
import { ChoiceCard } from '../SetupSteps';
import { ConnectAgent } from '../ConnectAgent';
import { AgentEditor } from './AgentEditor';
import { AGENT_TEMPLATES, agentFromTemplate, duplicateAgent, type AgentTemplate } from '../../utils/agentTemplates';
import { upsertAgent } from '../../utils/agentForm';
import { useToast } from '../../state/ToastProvider';
import { cls, fmtUsd } from '../../utils/format';
import { userMessage } from '../../utils/errors';

/**
 * "Your agents": the user's own AI traders, one card each.
 *
 * Every number on a card is one somebody counted — forecasts and the skill
 * score from the scoreboard (scored on that agent's own calls only), paper
 * P&L from that agent's own paper book, and sessions from the server. A
 * number nobody produced (no resolved forecasts, an unmarked position) is a
 * dash or words, never a zero.
 *
 * Tokens never come here. "Connect" writes or copies a token-less setup
 * (`--mcp --agent <id>`); the HTTP examples, which do carry a token, go
 * backend -> main -> clipboard and this window only ever learns `{ ok }`.
 */

const VERDICT_WORDS: Record<AgentScore['verdict'], { text: string; tone: string }> = {
  'too-few': { text: 'too few settled', tone: 'text-krypt-muted' },
  indistinguishable: { text: 'no measurable edge', tone: 'text-krypt-warn' },
  'agent-better': { text: 'beats the market', tone: 'text-krypt-win' },
  'market-better': { text: 'market beats it', tone: 'text-krypt-loss' },
};

function sessionsLine(st: McpAgentStatus | null): string {
  if (!st) return 'status unknown';
  const n = st.sessions ?? 0;
  return n > 0 ? `${n} client${n === 1 ? '' : 's'} connected` : 'no client connected';
}

/** Open positions in an agent's own paper book; null = not read. */
export function openPaperPositions(p: AgentPaper | null | undefined): number | null {
  const pos = p?.portfolio?.positions;
  return Array.isArray(pos) ? pos.length : null;
}

/** Realised + unrealised paper P&L, or null when any half is unknown (an
 *  unmarked position is not $0). */
export function paperPnl(p: AgentPaper | null | undefined): number | null {
  const pf = p?.portfolio;
  if (!pf) return null;
  const r = pf.realizedPnlUsd;
  const u = pf.totalUnrealizedPnlUsd ?? (Array.isArray(pf.positions) && pf.positions.length === 0 ? 0 : null);
  return typeof r === 'number' && typeof u === 'number' ? r + u : null;
}

export function AgentsSection({
  config, status, board, activity, patch, papers, onPapersChanged,
}: {
  config: Partial<TraderConfig> | null;
  status: McpStatus | null;
  board: ForecastScoreboard | null;
  activity: McpActivity | null;
  patch: (p: Record<string, unknown>) => Promise<void>;
  /** Each agent's own paper book (mcp.paper(agentId)), keyed by agent id. */
  papers?: Record<string, AgentPaper | null> | null;
  onPapersChanged?: () => void;
}) {
  const toast = useToast();
  const agents = useMemo(() => agentsOf(config), [config]);
  const [editing, setEditing] = useState<{ agent: McpAgent; isNew: boolean } | null>(null);
  const [picking, setPicking] = useState(false);
  const [connecting, setConnecting] = useState<McpAgent | null>(null);
  const [exporting, setExporting] = useState<McpAgent | null>(null);
  const [importing, setImporting] = useState(false);
  const [deleting, setDeleting] = useState<McpAgent | null>(null);
  // Switching an agent off that still holds paper positions: offer to close
  // them (the confirm only appears when there is something to close).
  const [disabling, setDisabling] = useState<McpAgent | null>(null);
  const [resetting, setResetting] = useState<McpAgent | null>(null);
  const [closeToo, setCloseToo] = useState(false);
  const full = agents.length >= MAX_AGENTS;

  const save = async (list: McpAgent[], extra: Record<string, unknown> = {}): Promise<void> => {
    await patch({ mcpAgents: list, ...extra });
  };

  const onSave = async (a: McpAgent): Promise<void> => {
    try {
      await save(upsertAgent(agentsOf(config), a));
      toast.success(editing?.isNew ? `${a.name} created. Connect it to a client to start.` : `${a.name} saved.`);
      setEditing(null);
    } catch (e) {
      toast.error(userMessage(e));
    }
  };

  const openPaper = (id: string): number => openPaperPositions(papers?.[id]) ?? 0;

  /** Close one agent's paper positions at the bid. Paper only; never live. */
  const closePaper = async (a: McpAgent): Promise<void> => {
    try {
      const r = await window.krypt.mcp.closeAgentPaper(a.id);
      const n = Array.isArray(r?.closed) ? r.closed.length : null;
      const left = Array.isArray(r?.open) ? r.open.length : 0;
      const text = r?.message || (n === null
        ? `${a.name}'s paper positions were closed.`
        : `Closed ${n} paper position${n === 1 ? '' : 's'} of ${a.name}'s at the bid.`);
      // Some could not be sold (no bid): say so rather than "closed".
      if (r?.ok === false || left > 0) toast.push(text, 'warn', 10_000);
      else toast.success(text);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      onPapersChanged?.();
    }
  };

  /** Wipe one agent's paper account back to the starting bankroll. The
   *  user's call alone: agents can't (their paper record is what they are
   *  judged by, and what their paper loss stop is rebuilt from). */
  const resetPaper = async (a: McpAgent): Promise<void> => {
    try {
      await window.krypt.mcp.paper(a.id, true);
      toast.success(`${a.name}'s paper account is back to its starting bankroll.`);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      onPapersChanged?.();
    }
  };

  const toggle = async (a: McpAgent, enabled: boolean, close = false): Promise<void> => {
    if (!enabled && close) await closePaper(a);
    await save(upsertAgent(agentsOf(config), { ...a, enabled, updatedAt: new Date().toISOString() }));
    toast.push(enabled ? `${a.name} is on.` : `${a.name} is off: its token is refused until you switch it back on.`, 'info', 5000);
  };

  const askToggle = (a: McpAgent, enabled: boolean): void => {
    if (!enabled && openPaper(a.id) > 0) {
      setCloseToo(false);
      setDisabling(a);
      return;
    }
    void toggle(a, enabled);
  };

  // Deleting the agent Autopilot runs as turns Autopilot OFF. It used to
  // switch Autopilot to Default without a word — Default has its own guide,
  // rules and mode, so the schedule kept spending the user's AI budget as a
  // different trader than the one they set up.
  const remove = async (a: McpAgent, close = false): Promise<void> => {
    if (close) await closePaper(a);
    const runsAutopilot = config?.autopilotAgentId === a.id;
    const extra = runsAutopilot
      ? { autopilotAgentId: DEFAULT_AGENT_ID, autopilotEnabled: false }
      : {};
    await save(agentsOf(config).filter((x) => x.id !== a.id), extra);
    toast.success(runsAutopilot
      ? `${a.name} deleted, and Autopilot is off (it ran as ${a.name}). Pick an agent and switch it back on when you want.`
      : `${a.name} deleted. Its token no longer works, so clients set up as it are refused.`);
  };

  const byId = <T extends { agentId: string }>(rows: T[] | undefined, id: string): T | null =>
    rows?.find((r) => r.agentId === id) ?? null;

  return (
    <Section
      title="Your agents"
      description="Your own AI traders. Each has its own personality and decision rules, its own token, its own record — and the rails on this page still bind all of them."
    >
      <div data-tour="agents-section">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <GlassButton variant="primary" onClick={() => setPicking(true)} disabled={full} data-qa="agent-new">
          <Plus className="h-4 w-4" /> New agent
        </GlassButton>
        <button type="button" onClick={() => setImporting(true)} disabled={full} className="krypt-btn-default" data-qa="agent-import">
          <Upload className="h-4 w-4" /> Import
        </button>
        <span className="text-[11px] text-krypt-dim">
          {full ? `That's the most (${MAX_AGENTS}). Delete one to add another.` : `${agents.length} of ${MAX_AGENTS}.`}
          {' '}A guide is advice to the model; rules and caps are enforced by the app on every order the agent places.
        </span>
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-qa="agent-cards" data-tour="agent-cards">
        {agents.map((a) => (
          <AgentCard
            key={a.id}
            agent={a}
            config={config}
            st={status?.agents?.find((x) => x.id === a.id) ?? null}
            score={byId(board?.byAgent, a.id)}
            paper={papers?.[a.id] ?? null}
            orders={activity?.orders.filter((o) => (o.agentId ?? DEFAULT_AGENT_ID) === a.id) ?? null}
            onToggle={(v) => askToggle(a, v)}
            onConnect={() => setConnecting(a)}
            onEdit={() => setEditing({ agent: a, isNew: false })}
            onDuplicate={() => setEditing({ agent: duplicateAgent(a, agents.map((x) => x.id)), isNew: true })}
            onExport={() => setExporting(a)}
            onDelete={() => { setCloseToo(openPaper(a.id) > 0); setDeleting(a); }}
            onResetPaper={() => setResetting(a)}
            disableDuplicate={full}
          />
        ))}
      </div>

      {picking && (
        <TemplatePicker
          onClose={() => setPicking(false)}
          onPick={(t) => {
            setPicking(false);
            setEditing({ agent: agentFromTemplate(t, agents.map((x) => x.id)), isNew: true });
          }}
        />
      )}
      {editing && (
        <AgentEditor
          agent={editing.agent}
          isNew={editing.isNew}
          config={config}
          otherNames={agents.filter((x) => x.id !== editing.agent.id).map((x) => x.name)}
          onSave={onSave}
          onClose={() => setEditing(null)}
        />
      )}
      {connecting && (
        <ConnectModal
          agent={connecting}
          st={status?.agents?.find((x) => x.id === connecting.id) ?? null}
          httpEnabled={!!config?.mcpHttpEnabled}
          onClose={() => setConnecting(null)}
        />
      )}
      {exporting && <ExportModal agent={exporting} onClose={() => setExporting(null)} />}
      {importing && (
        <ImportModal
          taken={agents.map((a) => a.id)}
          onClose={() => setImporting(false)}
          onImport={(a) => { setImporting(false); setEditing({ agent: a, isNew: true }); }}
        />
      )}
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name ?? 'this agent'}?`}
        danger
        confirmLabel="Delete"
        onClose={() => setDeleting(null)}
        onConfirm={() => { const a = deleting; setDeleting(null); if (a) void remove(a, closeToo); }}
        body={
          <div className="space-y-2" data-qa="agent-delete-confirm">
            <p>
              Its token stops working immediately: every client you set up as {deleting?.name} is
              refused from now on (remove the entry from those apps when you like). Its forecasts,
              orders and paper fills stay in the records.
            </p>
            {deleting && config?.autopilotAgentId === deleting.id && (
              <p className="text-krypt-warn" data-qa="agent-delete-autopilot">
                Autopilot runs as this agent, so Autopilot will be switched off. Pick another agent
                and switch it back on whenever you like.
              </p>
            )}
            {deleting && openPaper(deleting.id) > 0 ? (
              <ClosePaperChoice n={openPaper(deleting.id)} checked={closeToo} onChange={setCloseToo} />
            ) : (
              <p>Real (Live) positions it opened are never closed by this — close those yourself if you want them gone.</p>
            )}
          </div>
        }
      />
      <ConfirmDialog
        open={resetting !== null}
        title={`Reset ${resetting?.name ?? 'this agent'}'s paper account?`}
        danger
        confirmLabel="Reset"
        onClose={() => setResetting(null)}
        onConfirm={() => { const a = resetting; setResetting(null); if (a) void resetPaper(a); }}
        body={
          <div className="space-y-2" data-qa="agent-reset-paper-confirm">
            <p>
              Its paper cash goes back to the starting bankroll and its paper positions, orders and
              P&amp;L are wiped. That record is how you judge it before letting it trade live, and
              its paper daily loss stop counts from it, so the stop starts over too.
            </p>
            <p>Its forecasts, live orders and real positions are not touched.</p>
          </div>
        }
      />
      <ConfirmDialog
        open={disabling !== null}
        title={`Switch ${disabling?.name ?? 'this agent'} off?`}
        confirmLabel="Switch off"
        onClose={() => setDisabling(null)}
        onConfirm={() => { const a = disabling; setDisabling(null); if (a) void toggle(a, false, closeToo); }}
        body={
          <div className="space-y-2">
            <p>Its token is refused until you switch it back on.</p>
            {disabling && (
              <ClosePaperChoice n={openPaper(disabling.id)} checked={closeToo} onChange={setCloseToo} />
            )}
          </div>
        }
      />
      </div>
    </Section>
  );
}

/** "Also close its N paper positions" — offered, never assumed. */
function ClosePaperChoice({ n, checked, onChange }: { n: number; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-start gap-2 rounded-lg border border-krypt-border bg-krypt-surface2 p-2" data-qa="agent-close-paper">
      <input type="checkbox" className="mt-0.5 h-4 w-4 accent-krypt-purple" checked={checked}
        onChange={(e) => onChange(e.target.checked)} />
      <span>
        Also close its {n} open paper position{n === 1 ? '' : 's'} (sold at the current bid, imaginary
        money). Otherwise they stay open and settle on their own. Real (Live) positions are never
        touched by this.
      </span>
    </label>
  );
}

function AgentCard({
  agent: a, config, st, score, paper, orders, onToggle, onConnect, onEdit, onDuplicate,
  onExport, onDelete, onResetPaper, disableDuplicate,
}: {
  agent: McpAgent;
  config: Partial<TraderConfig> | null;
  st: McpAgentStatus | null;
  score: AgentScore | null;
  paper: AgentPaper | null;
  orders: McpActivity['orders'] | null;
  onToggle: (v: boolean) => void;
  onConnect: () => void;
  onEdit: () => void;
  onDuplicate: () => void;
  onExport: () => void;
  onDelete: () => void;
  onResetPaper: () => void;
  disableDuplicate: boolean;
}) {
  // The backend's answer when it has one (it knows the account mode, the
  // global mode and the agent's own); else the same rule here, account mode
  // included — a red LIVE badge on an app in Paper was wrong.
  const eff = st?.mode ?? effectiveAgentMode(
    config?.mcpTradeMode, a, config?.accountMode === 'live' ? 'live' : 'paper');
  const caps = effectiveCaps(config, a.rules);
  const rules = rulesInWords(a.rules);
  // No paper orders is no record, not a $0.00 one.
  const traded = !!paper && ((paper.orders?.orders?.length ?? 0) > 0 || (openPaperPositions(paper) ?? 0) > 0);
  const pnl = traded ? paperPnl(paper) : null;
  // Orders that WENT THROUGH. A refused attempt (no forecast, no edge, over
  // a cap) is not a live order, and counting them read as real money spent.
  const liveOrders = orders === null ? null : orders.filter((o) => o.mode === 'live' && o.ok).length;
  const paperOrders = orders === null ? null : orders.filter((o) => o.mode === 'paper' && o.ok).length;
  const isDefault = a.id === DEFAULT_AGENT_ID;
  const verdict = score ? VERDICT_WORDS[score.verdict] : null;

  return (
    <div
      className={cls('flex flex-col rounded-2xl border p-4 transition',
        a.enabled ? 'border-white/[0.08] bg-white/[0.03]' : 'border-white/[0.05] bg-white/[0.015] opacity-70')}
      style={{ boxShadow: `inset 3px 0 0 ${a.color}` }}
      data-qa={`agent-card-${a.id}`}
    >
      <div className="flex items-start gap-3">
        <span
          className="grid h-10 w-10 shrink-0 place-items-center rounded-xl text-xl"
          style={{ background: `${a.color}26`, boxShadow: `inset 0 0 0 1px ${a.color}66` }}
        >
          {a.emoji}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-semibold text-white" title={a.name}>{a.name}</span>
            <ModeBadge eff={eff} configured={a.mode} />
          </div>
          <div className="mt-0.5 truncate text-[11px] text-krypt-dim" title={st?.serverName}>
            {sessionsLine(st)}
            {st && !st.hasToken && ' · no token yet'}
          </div>
        </div>
        <div title={a.enabled ? 'On: its token works' : 'Off: its token is refused'}>
          <GlassSwitch checked={a.enabled} onCheckedChange={onToggle} ariaLabel={`${a.name} on/off`} width={38} height={21} />
        </div>
      </div>

      <div className="mt-3 grid grid-cols-4 gap-1.5 text-center">
        {/* No scoreboard row = not loaded (or no forecasts yet): a dash,
            never a counted 0. */}
        <Stat label="Forecasts" value={score ? String(score.total) : '—'} />
        <Stat label="Resolved" value={score ? String(score.nPaired) : '—'} />
        <Stat
          label="Skill"
          value={score?.skill === null || score?.skill === undefined ? '—' : `${(score.skill * 100).toFixed(1)}%`}
          tone={score?.skill ? (score.skill > 0 ? 'text-krypt-win' : 'text-krypt-loss') : undefined}
        />
        <Stat
          label="Paper P&L"
          value={traded ? fmtUsd(pnl, { sign: true }) : '—'}
          tone={pnl === null ? undefined : pnl < 0 ? 'text-krypt-loss' : pnl > 0 ? 'text-krypt-win' : undefined}
        />
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-3 text-[10px] text-krypt-dim">
        {verdict && <span className={verdict.tone}>{verdict.text}</span>}
        {paperOrders === null || liveOrders === null
          ? <span>orders —</span>
          : <span>{paperOrders} paper order{paperOrders === 1 ? '' : 's'} · {liveOrders} live order{liveOrders === 1 ? '' : 's'}</span>}
        {st?.spentTodayUsd !== null && st?.spentTodayUsd !== undefined && <span>{fmtUsd(st.spentTodayUsd)} today</span>}
      </div>

      <div className="mt-3 flex min-h-[22px] flex-wrap gap-1">
        {rules.length === 0 && <span className="text-[11px] text-krypt-dim">No rules of its own: the global rails only.</span>}
        {rules.map((r) => (
          <span key={r} className="rounded-full border border-white/[0.08] bg-white/[0.04] px-2 py-0.5 text-[10px] text-white/80">{r}</span>
        ))}
        {(caps.agentBinds.maxOrderUsd || caps.agentBinds.dailySpendUsd) && (
          <span className="rounded-full border border-krypt-warn/30 bg-krypt-warn/10 px-2 py-0.5 text-[10px] text-krypt-warn">
            {fmtUsd(caps.maxOrderUsd)}/order · {fmtUsd(caps.dailySpendUsd)}/day
          </span>
        )}
      </div>
      {a.guide && <p className="mt-2 line-clamp-2 text-[11px] italic text-krypt-muted">&ldquo;{a.guide.split('\n')[0]}&rdquo;</p>}

      <div className="mt-auto flex flex-wrap gap-1.5 pt-3">
        <button type="button" onClick={onConnect} className="krypt-btn-primary text-xs" disabled={!a.enabled} data-qa={`agent-connect-${a.id}`}>
          <PlugZap className="h-3.5 w-3.5" /> Connect
        </button>
        <button type="button" onClick={onEdit} className="krypt-btn-default text-xs" data-qa={`agent-edit-${a.id}`}>
          <Pencil className="h-3.5 w-3.5" /> Edit
        </button>
        <button type="button" onClick={onDuplicate} className="krypt-btn-ghost text-xs" disabled={disableDuplicate} title="Duplicate (as paper)">
          <Copy className="h-3.5 w-3.5" />
        </button>
        <button type="button" onClick={onExport} className="krypt-btn-ghost text-xs" title="Export (no token, no id)" data-qa={`agent-export-${a.id}`}>
          <Download className="h-3.5 w-3.5" />
        </button>
        {traded && (
          <button type="button" onClick={onResetPaper} className="krypt-btn-ghost text-xs" title="Reset its paper account" data-qa={`agent-reset-paper-${a.id}`}>
            <RotateCcw className="h-3.5 w-3.5" />
          </button>
        )}
        {!isDefault && (
          <button type="button" onClick={onDelete} className="krypt-btn-ghost ml-auto text-xs text-krypt-loss" title="Delete" data-qa={`agent-delete-${a.id}`}>
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </div>
  );
}

function ModeBadge({ eff, configured }: { eff: 'off' | 'paper' | 'live'; configured: 'paper' | 'live' }) {
  if (eff === 'live') {
    return <span className="rounded border border-krypt-loss/50 bg-krypt-loss/15 px-1.5 py-px text-[9px] font-bold uppercase tracking-wider text-krypt-loss">Live</span>;
  }
  const waiting = configured === 'live';
  return (
    <span
      className="rounded border border-white/10 bg-white/[0.05] px-1.5 py-px text-[9px] font-bold uppercase tracking-wider text-krypt-muted"
      title={waiting ? 'Set to live; it trades paper until the app is Live and agent trading is Live.' : undefined}
    >
      {eff === 'off' ? 'Read only' : 'Paper'}{waiting ? ' · set live' : ''}
    </span>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-white/[0.05] bg-white/[0.02] px-1 py-1.5">
      <div className="text-[9px] uppercase tracking-wider text-krypt-dim">{label}</div>
      <div className={cls('font-mono text-xs', tone ?? 'text-white')}>{value}</div>
    </div>
  );
}

function TemplatePicker({ onPick, onClose }: { onPick: (t: AgentTemplate | null) => void; onClose: () => void }) {
  const [sel, setSel] = useState<string>(AGENT_TEMPLATES[0].key);
  return (
    <Modal open onClose={onClose} maxWidth="max-w-2xl">
      <div className="mb-3 flex items-start gap-3" data-qa="agent-templates">
        <div className="flex-1">
          <h3 className="text-base font-semibold text-white">New agent</h3>
          <p className="text-[11px] text-krypt-muted">
            Start from a template and make it yours, or start blank. Every template starts on paper,
            and none of them is a promise of an edge — the scoreboard is how you find out.
          </p>
        </div>
        <button onClick={onClose} className="krypt-btn-ghost p-1" aria-label="Close"><X className="h-4 w-4" /></button>
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {AGENT_TEMPLATES.map((t) => (
          <ChoiceCard key={t.key} selected={sel === t.key} onClick={() => setSel(t.key)} testId={`template-${t.key}`}
            title={<span>{t.emoji} {t.title}</span>}>
            {t.blurb}
          </ChoiceCard>
        ))}
        <ChoiceCard selected={sel === 'blank'} onClick={() => setSel('blank')} testId="template-blank" title="✏️ Blank">
          No guide, no rules: the global rails only. Write it yourself.
        </ChoiceCard>
      </div>
      <div className="mt-4 flex justify-end gap-2">
        <GlassButton onClick={onClose}>Cancel</GlassButton>
        <GlassButton
          variant="primary"
          onClick={() => onPick(AGENT_TEMPLATES.find((t) => t.key === sel) ?? null)}
          data-qa="template-continue"
        >
          Continue
        </GlassButton>
      </div>
    </Modal>
  );
}

function ConnectModal({ agent, st, httpEnabled, onClose }: {
  agent: McpAgent; st: McpAgentStatus | null; httpEnabled: boolean; onClose: () => void;
}) {
  const toast = useToast();
  const [rotate, setRotate] = useState(false);
  const copyHttp = async (): Promise<void> => {
    try {
      const r = await window.krypt.mcp.copyHttpSnippet(agent.id);
      if (r.ok) toast.success(r.message ?? `HTTP examples for ${agent.name} copied. They contain its token — treat it like a password.`);
      else toast.error(r.message ?? 'Could not copy the examples.');
    } catch (e) {
      toast.error(userMessage(e));
    }
  };
  return (
    <Modal open onClose={onClose} maxWidth="max-w-2xl">
      <div className="mb-3 flex items-start gap-3" data-qa="agent-connect-modal">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl text-lg"
          style={{ background: `${agent.color}26`, boxShadow: `inset 0 0 0 1px ${agent.color}66` }}>
          {agent.emoji}
        </span>
        <div className="flex-1">
          <h3 className="text-base font-semibold text-white">Connect {agent.name}</h3>
          <p className="text-[11px] text-krypt-muted">
            The setup names {agent.name} (no token in it: the app hands the agent its own token), so
            whatever client you add it to IS {agent.name}: its guide, its rules, its record.
            {st?.serverName && <> It shows up in the client as <span className="font-mono text-white">{st.serverName}</span>.</>}
          </p>
        </div>
        <button onClick={onClose} className="krypt-btn-ghost p-1" aria-label="Close"><X className="h-4 w-4" /></button>
      </div>
      <ConnectAgent agentId={agent.id} hideAutopilot onAutopilot={onClose} serverName={st?.serverName} />
      <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-white/[0.06] pt-3 text-[11px] text-krypt-muted">
        {httpEnabled && (
          <button type="button" className="krypt-btn-default text-xs" onClick={() => void copyHttp()}>
            <Copy className="h-3.5 w-3.5" /> Copy HTTP API examples
          </button>
        )}
        <button type="button" className="krypt-btn-ghost text-xs" onClick={() => setRotate(true)}>
          <RotateCcw className="h-3.5 w-3.5" /> New token for {agent.name}
        </button>
        <span className="ml-auto">To run it in the app instead, pick it under Autopilot → Run as agent.</span>
      </div>
      <ConfirmDialog
        open={rotate}
        title={`New token for ${agent.name}?`}
        confirmLabel="Rotate"
        onClose={() => setRotate(false)}
        onConfirm={() => {
          setRotate(false);
          void window.krypt.mcp.rotateToken(agent.id)
            .then(() => toast.success(`New token for ${agent.name}. Clients set up from this app pick it up by themselves; re-copy the HTTP examples if you use them.`))
            .catch((e) => toast.error(userMessage(e)));
        }}
        body={<p>Clients you set up from this app keep working: their setup holds no token, and the relay reads the new one. Anything calling the HTTP API as {agent.name} — or anything that ever saw its token — is refused until it gets the new one. Your other agents are not affected.</p>}
      />
    </Modal>
  );
}

function ExportModal({ agent, onClose }: { agent: McpAgent; onClose: () => void }) {
  const toast = useToast();
  const text = agentExportJson(agent);
  return (
    <Modal open onClose={onClose} maxWidth="max-w-xl">
      <h3 className="text-base font-semibold text-white">Share {agent.name}</h3>
      <p className="mb-2 text-[11px] text-krypt-muted">
        Its name, look, guide and rules. No token, no id, no records, and no mode: whoever imports it
        starts on paper.
      </p>
      <textarea readOnly value={text} rows={12} className="krypt-input font-mono text-[11px]" data-qa="agent-export-json" />
      <div className="mt-3 flex justify-end gap-2">
        <GlassButton onClick={onClose}>Close</GlassButton>
        <GlassButton
          variant="primary"
          onClick={() => {
            void navigator.clipboard.writeText(text)
              .then(() => toast.success('Copied. Paste it anywhere, or into Import on another machine.'))
              .catch(() => toast.error('Could not reach the clipboard.'));
          }}
        >
          <Copy className="h-4 w-4" /> Copy
        </GlassButton>
      </div>
    </Modal>
  );
}

function ImportModal({ taken, onImport, onClose }: {
  taken: string[]; onImport: (a: McpAgent) => void; onClose: () => void;
}) {
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  return (
    <Modal open onClose={onClose} maxWidth="max-w-xl">
      <h3 className="text-base font-semibold text-white">Import an agent</h3>
      <p className="mb-2 text-[11px] text-krypt-muted">
        Paste a shared agent. It arrives as a new agent on paper, and you review it in the editor
        before anything is saved. Nothing in the file can switch it to live.
      </p>
      <textarea
        value={text} rows={10} onChange={(e) => { setText(e.target.value); setError(null); }}
        className="krypt-input font-mono text-[11px]" placeholder='{"kryptPolybotAgent": 1, "agent": { … }}'
        data-qa="agent-import-json"
      />
      {error && <p className="mt-1 text-[11px] text-krypt-loss">{error}</p>}
      <div className="mt-3 flex justify-end gap-2">
        <GlassButton onClick={onClose}>Cancel</GlassButton>
        <GlassButton
          variant="primary"
          disabled={!text.trim()}
          onClick={() => {
            const r = parseAgentImport(text, taken);
            if (r.ok) onImport(r.agent); else setError(r.error);
          }}
        >
          <Upload className="h-4 w-4" /> Review
        </GlassButton>
      </div>
    </Modal>
  );
}
