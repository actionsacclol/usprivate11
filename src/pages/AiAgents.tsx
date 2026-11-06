import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, Bot, CheckCircle2, Lock, RefreshCw, ShieldAlert, ShieldCheck, XCircle,
} from 'lucide-react';
import type {
  AgentPaper, ForecastScore, ForecastScoreboard, McpActivity, McpOrderRow, McpTradeMode,
} from '@shared/market';
import type { McpStatus, TraderConfig } from '@shared/types';
import type { PageId } from '../App';
import { agentsOf } from '@shared/agents';
import {
  Card, ConfirmDialog, Empty, NumberInput, Page, Section, StatCard, Switch,
} from '../components/common';
import { Note } from '../components/terminal/TerminalBits';
import { AutopilotPanel } from '../components/AutopilotPanel';
import { ConnectionsPanel } from '../components/ConnectionsPanel';
import { HttpApiCard } from '../components/HttpApiCard';
import { AgentFlow } from '../components/AgentFlow';
import { GoLivePanel } from '../components/GoLivePanel';
import { AgentsSection } from '../components/agents/AgentsSection';
import { agentsLiveOnLive, backToPaperPatch, liveAgents } from '../utils/goLive';
import { configuredAgents, hubAgentIdentity, tagLabel } from '../utils/agents';
import { useApp } from '../state/AppStateProvider';
import { usePoll } from '../state/usePoll';
import { useToast } from '../state/ToastProvider';
import { cls, fmtCents, fmtNum, fmtTimeShort, fmtUsd } from '../utils/format';
import { publishActivity } from '../state/activity';
import { userMessage } from '../utils/errors';

/**
 * AI agents over MCP, and the scoreboard that says whether to trust them.
 *
 * The two live on one page on purpose. An agent that can trade and a record of
 * whether its forecasts beat the market are the same decision: the second is
 * the only evidence for turning the first up from paper to live.
 *
 * This is also the app's front door: the bundled strategies measured net
 * negative in the offline backtest, while an agent that must commit a
 * forecast before every buy, scored against the market, is evidence.
 */

const VERDICT: Record<ForecastScore['verdict'], { text: string; tone: string }> = {
  'too-few': {
    text: 'Not enough resolved markets to say anything yet. Keep forecasting — and do not size a position off this.',
    tone: 'text-krypt-muted',
  },
  indistinguishable: {
    text: 'No measurable difference from the market price. The agents’ "edges" are not distinguishable from noise.',
    tone: 'text-krypt-warn',
  },
  'agent-better': {
    text: 'The agents beat the market price on resolved markets, by more than two standard errors. Past markets, not a promise.',
    tone: 'text-krypt-win',
  },
  'market-better': {
    text: 'The market price beat the agents. When they disagreed, the market was usually right — their edges were not edges.',
    tone: 'text-krypt-loss',
  },
};

const shortCid = (cid: string | null | undefined): string =>
  (cid ? `${cid.slice(0, 10)}…` : '—');

/** "BUY 12.5 YES · Will X happen? @ 41¢" — shares are floats on Polymarket. */
function orderLine(o: McpOrderRow): string {
  const size = Number.isFinite(o.size) ? (Number.isInteger(o.size) ? String(o.size) : o.size.toFixed(2)) : '—';
  return `${o.action.toUpperCase()} ${size} ${o.side.toUpperCase()} @ ${fmtCents(o.priceCents)}`;
}

export function AiAgentsPage({ onNav }: { onNav?: (p: PageId) => void } = {}) {
  const { config, refresh, backend } = useApp();
  const toast = useToast();
  const [armLive, setArmLive] = useState(false);
  const [armSwitches, setArmSwitches] = useState(false);
  const [armScripts, setArmScripts] = useState(false);
  const [confirmNoApproval, setConfirmNoApproval] = useState(false);
  const [deciding, setDeciding] = useState<number | null>(null);
  const [goLiveOpen, setGoLiveOpen] = useState(false);

  // Keyed on the account mode: a Paper <-> Live switch changes what every
  // agent trades in, so the previous reads are dropped and read again at once.
  const acctMode = config?.accountMode ?? 'paper';
  const status = usePoll<McpStatus>(() => window.krypt.mcp.status(), 5_000, [acctMode]);
  const activity = usePoll<McpActivity>(() => window.krypt.mcp.activity(100), 10_000, [acctMode]);
  const board = usePoll<ForecastScoreboard>(() => window.krypt.ai.scoreboard(), 30_000, [acctMode]);

  // Each agent's own paper book, read together (one small local read each).
  const agentIds = useMemo(() => agentsOf(config).map((a) => a.id), [config]);
  const idsKey = agentIds.join(',');
  const papers = usePoll<Record<string, AgentPaper | null>>(async () => {
    const rows = await Promise.all(agentIds.map(async (id) => {
      try {
        return [id, await window.krypt.mcp.paper(id)] as const;
      } catch {
        return [id, null] as const;
      }
    }));
    return Object.fromEntries(rows);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, 20_000, [idsKey, acctMode]);

  const reloadAll = useCallback((): void => {
    status.reload(); activity.reload(); board.reload(); papers.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The page often mounts before the backend handshake (it is the front
  // door): read everything again the moment the engine comes up, rather than
  // showing "not running" until the next slow poll.
  const backendUp = backend.status === 'running';
  useEffect(() => { if (backendUp) reloadAll(); }, [backendUp, reloadAll]);

  // App.tsx toasts every agent order app-wide; this page only reloads.
  useEffect(() => window.krypt.mcp.onOrder(() => {
    activity.reload();
    status.reload();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), []);

  const decide = async (id: number, approve: boolean): Promise<void> => {
    setDeciding(id);
    try {
      const res = await window.krypt.mcp.decide(id, approve);
      // Only an answer that took is a decision; a refused one (expired,
      // re-vet failed) is just the backend's message.
      publishActivity(() => (res.ok
        ? { kind: 'agent', mode: 'live', message: res.message, approvalId: id, decision: approve ? 'approved' : 'rejected' }
        : { kind: 'agent', mode: 'action', message: res.message, approvalId: null, decision: null }));
      if (res.ok) toast.success(res.message); else toast.warn(res.message);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setDeciding(null);
      status.reload();
      activity.reload();
    }
  };

  const patch = async (p: Record<string, unknown>): Promise<void> => {
    await window.krypt.config.update(p as Partial<TraderConfig>);
    await refresh.state();
    // Start/stop the listener to match, then re-read.
    try { await window.krypt.mcp.sync(); } catch { /* the next poll says */ }
    status.reload();
  };

  const s = status.data;
  const mode: McpTradeMode = (config?.mcpTradeMode ?? 'paper') as McpTradeMode;
  const appLive = config?.accountMode === 'live';
  const liveOn = agentsLiveOnLive(config);
  const pending = s?.pendingApprovals ?? activity.data?.pending ?? [];
  const realized = useMemo(() => {
    const books = papers.data ? Object.values(papers.data) : [];
    if (!books.length) return null;
    let sum = 0;
    for (const b of books) {
      const r = b?.portfolio?.realizedPnlUsd;
      if (typeof r !== 'number') return null;
      sum += r;
    }
    return sum;
  }, [papers.data]);

  return (
    <Page
      title="AI Agents"
      subtitle="Connect Claude, Cursor, Codex or any MCP client — or let Autopilot run one for you. Watch it research, let it trade on paper, and let the scoreboard say whether it beats the market."
      actions={
        <div className="flex gap-2">
          <button onClick={reloadAll} className="krypt-btn-default">
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
          <button onClick={() => setGoLiveOpen(true)} className="krypt-btn-danger" data-qa="golive-open">
            <ShieldCheck className="h-4 w-4" /> {liveOn ? 'Live setup' : 'Go live'}
          </button>
        </div>
      }
    >
      {/* Unmissable on purpose: a page that looked the same on paper and on
          real money is how someone forgets which one they left it on. */}
      {liveOn && (
        <div
          className="mb-4 flex flex-wrap items-center gap-3 rounded-xl border border-krypt-loss/60 bg-krypt-loss/15 px-4 py-3"
          data-qa="live-bar"
        >
          <AlertTriangle className="h-5 w-5 shrink-0 text-krypt-loss" />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-white">LIVE: agents can spend real money</div>
            <div className="text-[11px] text-white/70">
              Set to live: {liveAgents(config).map((a) => a.name).join(', ')} · up to{' '}
              {fmtUsd(config?.mcpDailySpendUsd ?? 100)}/day of your money
              {config?.mcpLiveApproval !== false ? ', each order waiting for your approval.' : ' — approvals are OFF.'}
              {!config?.mcpEnabled && !config?.autopilotEnabled && ' None of them can reach the app right now (the agent server and Autopilot are off).'}
            </div>
          </div>
          <button
            onClick={() => void patch(backToPaperPatch() as Record<string, unknown>)}
            className="krypt-btn-primary text-xs"
            data-qa="live-bar-back-to-paper"
          >
            Back to paper
          </button>
        </div>
      )}

      <AgentFlow
        board={board.data}
        activity={activity.data}
        mode={mode}
        clientsSeen={(s?.sessions ?? []).map((x) => x.client)}
        paperRealizedUsd={realized}
        onAutopilot={() => onNav?.('settings')}
        onHub={() => onNav?.('visualizer')}
      />

      <AgentsSection
        config={config}
        status={s}
        board={board.data}
        activity={activity.data}
        papers={papers.data}
        onPapersChanged={() => papers.reload()}
        patch={patch}
      />

      <Card className="mb-4">
        <div className="flex gap-3" data-tour="agent-disclosure">
          <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-krypt-warn" />
          <div className="space-y-2 text-[11px] leading-relaxed text-krypt-muted">
            <p className="text-sm font-medium text-white">Read this before letting an AI trade</p>
            <p>
              The app runs a local <span className="text-white">MCP server</span>. An AI client on{' '}
              <span className="text-white">this computer</span> connects to it and gets tools: market
              data, your account, paper trading, backtests, strategy scripts. It listens on 127.0.0.1
              only, every agent has its own token, and requests from web pages are refused.
            </p>
            <p>
              An agent must <span className="text-white">record a forecast before every buy</span>, and
              the order is refused unless that forecast beats the price by your minimum edge{' '}
              <span className="text-white">after Polymarket US&apos;s fee</span>. It has its own per-order,
              daily-spend, loss-stop and position caps on top of the global ones, live orders{' '}
              <span className="text-white">wait for your approval</span> by default, and it can only
              sell or cancel what <span className="text-white">its own orders filled</span> — never your
              positions. Exits are never capped: a cap on getting out is a cap on safety.
            </p>
            <p>
              <span className="text-white">Paper</span> fills against the real order book with imaginary
              money, in each agent&apos;s own paper account. <span className="text-white">Live</span> spends
              your money. On a liquid market the price is already a strong forecast; a language model can
              be wrong with total confidence, and market text can carry instructions meant for it. The
              scoreboard below is the evidence — run paper until it says otherwise.
            </p>
            <p className="text-krypt-warn">
              What the agent reads is sent to its AI provider (Anthropic, OpenAI, Cursor…): markets, and
              your positions, balances and orders. That is how these clients work. Everything else in this
              app stays on your machine.
            </p>
          </div>
        </div>
      </Card>

      <Section title="Waiting for you" description="Live agent orders that need your approval.">
        <div data-tour="agent-approvals">
          {pending.length === 0 ? (
            <Card>
              <p className="text-[11px] text-krypt-muted" data-qa="approvals-empty">
                Nothing waiting. {config?.mcpLiveApproval !== false
                  ? 'When a live agent wants to place an order it lands here (and on your phone, if remote control is paired) until you approve or reject it. Unanswered orders expire after 10 minutes.'
                  : 'Approvals are off: live agent orders are sent without asking you.'}
              </p>
            </Card>
          ) : (
            <Card className="ring-1 ring-krypt-warn/40">
              <div className="mb-2 text-sm font-medium text-white">
                Live orders waiting for you ({pending.length})
              </div>
              <p className="mb-3 text-[11px] text-krypt-muted">
                Approving re-checks the order against the market as it is now — price, caps and
                today&apos;s loss — before anything is sent. Unanswered orders expire after 10 minutes.
              </p>
              <div className="space-y-2">
                {pending.map((o) => (
                  <div key={o.id} className="flex flex-wrap items-center gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 px-3 py-2" data-qa={`approval-${o.id}`}>
                    <span className="font-mono text-[11px] text-krypt-dim">#{o.id}</span>
                    <span className="rounded bg-krypt-loss/15 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-krypt-loss">
                      {o.mode === 'live' ? 'LIVE' : 'PAPER'}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-xs text-white" title={o.title ?? o.conditionId}>
                      <span className="font-mono">{orderLine(o)}</span> · {o.title ?? shortCid(o.conditionId)}
                    </span>
                    <span className="text-[11px] text-krypt-muted">
                      {fmtUsd(o.committedUsd)} · <span className="text-white">{o.agentName ?? 'deleted agent'}</span>
                      {o.client ? ` via ${o.client}` : ''} · {fmtTimeShort(o.at)}
                    </span>
                    <div className="ml-auto flex gap-2">
                      <button
                        onClick={() => void decide(o.id, false)}
                        disabled={deciding === o.id}
                        className="krypt-btn-default text-xs"
                      >
                        Reject
                      </button>
                      <button
                        onClick={() => void decide(o.id, true)}
                        disabled={deciding !== null || !appLive}
                        title={appLive ? undefined : 'The app is in Paper now, so this can only be rejected.'}
                        className="krypt-btn-danger text-xs"
                      >
                        Approve &amp; send
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>
      </Section>

      <Section title="Connection" description="A loopback MCP server inside the app. Nothing outside this computer can reach it.">
        <Card>
          <div data-tour="agent-server">
            <ServerRow status={s} error={status.error} />
            <div className="mt-4 grid gap-3 md:grid-cols-2">
              <Switch
                checked={!!config?.mcpEnabled}
                onChange={(v) => void patch({ mcpEnabled: v })}
                label="Allow AI agents"
                description="Starts the server. Off by default; switching it off closes the port immediately. What agents may trade is set under Trading below."
              />
              <div>
                <span className="krypt-label">Port</span>
                <NumberInput
                  value={config?.mcpPort ?? 47635}
                  min={1024}
                  max={65535}
                  onChange={(v) => void patch({ mcpPort: Math.round(v) })}
                />
                <span className="krypt-hint">
                  Change it if another program owns 47635. Clients set up from this app follow it.
                </span>
              </div>
            </div>
            <div className="mt-4 border-t border-krypt-border pt-3">
              <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">Connected clients</div>
              {!s?.sessions?.length ? (
                <p className="text-[11px] text-krypt-dim">
                  None yet. A client appears here once it connects. The app has to stay open while an
                  agent uses it.
                </p>
              ) : (
                <div className="space-y-1">
                  {s.sessions.map((x) => (
                    <div key={x.id || x.connectedAt} className="flex items-center gap-2 rounded-lg border border-krypt-border px-2.5 py-1.5 text-[11px]">
                      <Bot className="h-3.5 w-3.5 text-krypt-win" />
                      <span className="text-white">{x.client}</span>
                      {x.version && <span className="text-krypt-dim">{x.version}</span>}
                      <span className="ml-auto text-krypt-dim">
                        {x.reads} reads · {x.writes} actions · {x.refused} refused · seen {fmtTimeShort(x.lastSeen)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </Card>
      </Section>

      <ConnectionsPanel onNav={onNav} />

      <div className="mb-6">
        <HttpApiCard config={config} status={s} patch={patch} />
      </div>
      <p className="-mt-3 mb-6 text-[11px] text-krypt-dim">
        Once connected, ask the agent something like: &ldquo;Use krypt-polybot-us. Check get_status and
        get_my_agent, look through markets closing soon, read the rules, and record honest forecasts.
        Only trade where your edge after the fee clears the minimum.&rdquo;
      </p>

      <Section title="Trading" description="What a connected agent may do with orders.">
        <Card>
          <div data-tour="agent-rails">
            <div className="flex gap-2">
              {(['off', 'paper', 'live'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  data-qa={`trade-mode-${m}`}
                  onClick={() => {
                    if (m === 'live' && mode !== 'live') setArmLive(true);
                    else void patch({ mcpTradeMode: m });
                  }}
                  className={cls(
                    'flex-1 rounded-lg border px-3 py-2 text-sm font-medium transition',
                    mode === m
                      ? m === 'live'
                        ? 'border-krypt-loss/60 bg-krypt-loss/10 text-white'
                        : 'border-krypt-purple/60 bg-krypt-purple/10 text-white'
                      : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi',
                  )}
                >
                  {m === 'off' ? 'Read only' : m === 'paper' ? 'Paper' : 'Live'}
                </button>
              ))}
            </div>
            <p className="mt-2 text-[11px] text-krypt-dim">
              For real money, use <button type="button" onClick={() => setGoLiveOpen(true)} className="text-krypt-purple underline-offset-2 hover:underline">Go live</button>:
              your API key, your agent&apos;s record, caps, approvals and the account mode, checked on one screen.
            </p>
            <p className="mt-2 text-[11px] text-krypt-muted">
              {mode === 'off' && 'Agents can read markets and record forecasts. Every order is refused.'}
              {mode === 'paper' && 'Orders fill against the real Polymarket US book with imaginary money, in each agent’s own paper account, and settle on the real outcome.'}
              {mode === 'live' && (
                <span className="text-krypt-loss">
                  Agents set to live ({liveAgents(config).map((a) => a.name).join(', ') || 'none yet — set one on its card'})
                  {appLive
                    ? ' spend your real dollars. Every other agent trades paper.'
                    : ' would spend your real dollars, but the app is in Paper, so every agent trades paper until you Go live.'}
                </span>
              )}
            </p>
            <p className="mt-1 text-[11px] text-krypt-dim">
              This is the master switch for all of your agents. The caps below bind them all together;
              each agent&apos;s own rules can only make them stricter for that agent.
            </p>
            <div className="mt-3">
              <Switch
                checked={config?.mcpLiveApproval !== false}
                onChange={(v) => {
                  if (v) void patch({ mcpLiveApproval: true });
                  else setConfirmNoApproval(true);
                }}
                label="Ask me before each live order"
                description="On by default. Each live order waits here (and on your phone, if remote control is paired) until you approve it."
              />
            </div>

            <div className="mt-4 grid gap-3 md:grid-cols-3">
              <Rail label="Max per order" hint="All agents. 0 = no cap.">
                <NumberInput prefix="$" value={config?.mcpMaxOrderUsd ?? 25} min={0}
                  onChange={(v) => void patch({ mcpMaxOrderUsd: v })} />
              </Rail>
              <Rail label="Max spend per day (UTC)"
                hint={s?.spentTodayUsd !== null && s?.spentTodayUsd !== undefined && Number.isFinite(s.spentTodayUsd)
                  ? `${fmtUsd(s.spentTodayUsd)} committed to live buys today. 0 = no cap.`
                  : 'All agents together, counting orders still resting. 0 = no cap.'}>
                <NumberInput prefix="$" value={config?.mcpDailySpendUsd ?? 100} min={0}
                  onChange={(v) => void patch({ mcpDailySpendUsd: v })} />
              </Rail>
              <Rail label="Max open positions" hint="Across every agent.">
                <NumberInput value={config?.mcpMaxPositions ?? 10} min={0} max={500}
                  onChange={(v) => void patch({ mcpMaxPositions: Math.round(v) })} />
              </Rail>
              <Rail label="Minimum edge" hint="Per share, after Polymarket US's fee, vs the agent's own forecast.">
                <NumberInput suffix="¢" value={config?.mcpMinEdgeCents ?? 3} min={0} max={50}
                  onChange={(v) => void patch({ mcpMinEdgeCents: v })} />
              </Rail>
              <Rail label="Daily loss stop"
                hint={(() => {
                  const loss = s?.lossToday?.lossUsd ?? s?.lossTodayUsd;
                  const base = 'Realised losses per UTC day; open positions count once their market closes. Buys stop at the limit; exits never do.';
                  if (typeof loss !== 'number') return base;
                  return `Down ${fmtUsd(loss)} today. ${s?.lossToday?.positionsUnreadable
                    ? 'Live positions could not be read just now, so agent buys are paused until they can. ' : ''}${base}`;
                })()}>
                <NumberInput prefix="$" value={config?.mcpDailyLossUsd ?? 50} min={0}
                  onChange={(v) => void patch({ mcpDailyLossUsd: v })} />
              </Rail>
              <Rail label="Agent paper bankroll" hint="Starting cash of EACH agent's own paper account — separate from yours.">
                <NumberInput prefix="$" value={config?.mcpPaperBankrollUsd ?? 1000} min={1}
                  onChange={(v) => void patch({ mcpPaperBankrollUsd: v })} />
              </Rail>
            </div>
          </div>
        </Card>
      </Section>

      <Section
        title="Permissions"
        description="Everything beyond an agent's own orders is a separate switch. Both off by default. Turning things OFF and pausing engines is always allowed."
      >
        <Card>
          <div className="grid gap-3 md:grid-cols-2" data-tour="agent-permissions">
            <Switch
              checked={!!config?.mcpAllowLiveSwitches}
              onChange={(v) => {
                if (v) setArmSwitches(true);
                else void patch({ mcpAllowLiveSwitches: false });
              }}
              label="Whole-account powers"
              description="Switching the crypto engine on, arming the crypto schedule for real orders, changing engine settings that spend (sizing, risk limits), and flattening the account."
              disabled={!config?.mcpEnabled}
            />
            <div>
              <Switch
                checked={!!config?.mcpScriptsEnabled}
                onChange={(v) => {
                  if (v) setArmScripts(true);
                  else void patch({ mcpScriptsEnabled: false });
                }}
                label="Write and run strategy scripts"
                description="Agents can write scripts and backtest them. A script an agent writes or changes is saved switched off, in shadow, until you read it and enable it on the Scripts page. Only you can arm a script for real orders."
                disabled={!config?.mcpEnabled}
              />
              <p className="mt-2 text-[11px] text-krypt-dim">
                This runs code an agent wrote on this computer. Agent scripts are limited to plain
                calculation (a short list of maths, date and text modules; no files, no network, no
                other programs), and an agent&rsquo;s backtest runs in a separate process on a copy of
                the recorded data. These are checks, not a sandbox: the code runs as your Windows user,
                and a script you approve runs inside the engine, which holds your API key. Turn this
                on only for an agent you trust. Autopilot never gets it.
              </p>
            </div>
          </div>
          {s && (
            <p className="mt-3 text-[11px] text-krypt-dim">
              A connected client sees {fmtNum(s.toolCount)} tools. Reconnect your client after changing
              these so it sees the new behaviour.
            </p>
          )}
          <div className="mt-4 flex gap-2 rounded-lg border border-krypt-border bg-krypt-surface2 p-3 text-[11px] leading-relaxed text-krypt-muted">
            <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-krypt-dim" />
            <div>
              <span className="text-white">Never available to an agent, whatever is switched on:</span>{' '}
              switching the app between Paper and Live, or refilling a paper bankroll; your Polymarket US
              API key; AI keys and bot tokens; its own permissions, caps and
              rails; remote control; the network. An agent reads text other people wrote, and a prompt
              injection must not be able to reach any of these. The one exception is the scripts
              permission above: it runs an agent&rsquo;s code on this computer, which is why it is off
              until you turn it on.
            </div>
          </div>
        </Card>
      </Section>

      <AutopilotPanel onNav={onNav} />

      <Scoreboard board={board.data} error={board.error} />

      <AgentActivity
        data={activity.data}
        error={activity.error}
        audit={s?.audit ?? []}
        onSettle={async (id) => {
          try {
            const r = await window.krypt.mcp.settleUnknown(id);
            if (r.ok) toast.success(r.message); else toast.warn(r.message);
          } catch (e) {
            toast.error(userMessage(e));
          } finally {
            activity.reload();
            status.reload();
          }
        }}
      />

      {goLiveOpen && (
        <GoLivePanel
          board={board.data}
          activity={activity.data}
          onClose={() => setGoLiveOpen(false)}
          onNav={onNav}
        />
      )}
      <ConfirmDialog
        open={armLive}
        title="Let AI agents spend real money?"
        danger
        confirmLabel="Allow live trading"
        onClose={() => setArmLive(false)}
        onConfirm={() => { setArmLive(false); void patch({ mcpTradeMode: 'live' }); }}
        body={
          <div className="space-y-2">
            <p>
              Agents you set to live on their card
              {appLive ? '' : ' (once the app is Live — it is in Paper now)'} will be able to ask for
              real orders. Every other agent keeps trading paper.
            </p>
            {config?.mcpLiveApproval !== false ? (
              <p>
                Each live order waits for your approval here (and on your phone, if remote control is
                paired) and is re-checked against the market at the moment you approve. Unanswered
                orders expire after 10 minutes.
              </p>
            ) : (
              <p>
                <span className="text-krypt-loss">Approvals are off</span>, so live orders are sent
                without asking you per order.
              </p>
            )}
            <p>
              Every buy still needs a forecast that clears your minimum edge after Polymarket US&apos;s fee,
              and stays inside {fmtUsd(config?.mcpMaxOrderUsd ?? 25)} per order and{' '}
              {fmtUsd(config?.mcpDailySpendUsd ?? 100)} per day.
            </p>
            <p className="text-krypt-warn">
              Scoreboard right now: {board.data ? VERDICT[board.data.overall.verdict].text : 'unavailable.'}
            </p>
          </div>
        }
      />
      <ConfirmDialog
        open={armSwitches}
        title="Give agents whole-account powers?"
        danger
        confirmLabel="Allow"
        onClose={() => setArmSwitches(false)}
        onConfirm={() => { setArmSwitches(false); void patch({ mcpAllowLiveSwitches: true }); }}
        body={
          <div className="space-y-2">
            <p>
              A connected agent will be able to switch trading engines on, arm the crypto schedule for
              real orders, loosen engine sizing and risk limits and flatten your account — without
              asking you per change. Those engines trade with your engine
              settings, not the agent caps on this page. Scripts are the exception: only you can arm a
              script live.
            </p>
            <p>
              Every change is logged below. You can switch this off at any time; an agent cannot switch
              it back on.
            </p>
          </div>
        }
      />
      <ConfirmDialog
        open={armScripts}
        title="Let agents write and run code on this computer?"
        danger
        confirmLabel="Allow scripts"
        onClose={() => setArmScripts(false)}
        onConfirm={() => { setArmScripts(false); void patch({ mcpScriptsEnabled: true }); }}
        body={
          <div className="space-y-2">
            <p>
              A connected agent will be able to write strategy scripts and run them in backtests on
              this computer, as your Windows user.
            </p>
            <p>
              Agent scripts are checked before they run: plain calculation only, no files, network or
              other programs. Backtests run in a separate process on a copy of the recorded data. These
              checks lower the risk; they do not remove it. An agent that reads a prompt injection could
              still try to misuse this.
            </p>
            <p>
              A script an agent writes or changes does not run in the engine, not even in shadow, until
              you read its code and enable it on the Scripts page. Only you can arm a script for real orders.
            </p>
          </div>
        }
      />
      <ConfirmDialog
        open={confirmNoApproval}
        title="Let agents place live orders without asking?"
        danger
        confirmLabel="Stop asking"
        onClose={() => setConfirmNoApproval(false)}
        onConfirm={() => { setConfirmNoApproval(false); void patch({ mcpLiveApproval: false }); }}
        body={
          <div className="space-y-2">
            <p>
              Agents set to live (and Autopilot running as one) will send live orders the moment they
              pass the rails — no approval step.
            </p>
            <p>
              The forecast gate, the per-order, daily-spend and daily-loss caps, and your agents&apos; own
              rules still apply. You will still be told about every order.
            </p>
          </div>
        }
      />
    </Page>
  );
}

function ServerRow({ status, error }: { status: McpStatus | null; error: string | null }) {
  const running = !!status?.running;
  const enabled = !!status?.enabled;
  const Icon = running ? CheckCircle2 : status?.lastError ? AlertTriangle : XCircle;
  const tone = running ? 'text-krypt-win' : status?.lastError ? 'text-krypt-warn' : 'text-krypt-dim';
  return (
    <div>
      <div className="flex items-center gap-2">
        <Icon className={cls('h-4 w-4', tone)} />
        <Bot className="h-4 w-4 text-krypt-muted" />
        <span className="text-sm text-white">MCP server</span>
        <span className={cls('text-[11px]', tone)}>
          {running ? `listening on 127.0.0.1:${status?.port}` : enabled ? 'not listening' : 'off'}
        </span>
        {status && running && (
          <span className="ml-auto font-mono text-[11px] text-krypt-dim">
            {fmtNum(status.toolCount)} tools · {status.agents?.length ?? 1} agent{(status.agents?.length ?? 1) === 1 ? '' : 's'}
          </span>
        )}
      </div>
      {(status?.lastError || error) && (
        <div className="mt-2"><Note tone="warn">{status?.lastError ?? error}</Note></div>
      )}
    </div>
  );
}

function Rail({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <span className="krypt-label">{label}</span>
      {children}
      {hint && <span className="krypt-hint">{hint}</span>}
    </div>
  );
}

const brier = (v: number | null | undefined): string => (v === null || v === undefined ? '—' : v.toFixed(4));

function Scoreboard({ board, error }: { board: ForecastScoreboard | null; error: string | null }) {
  const { config } = useApp();
  const agents = useMemo(() => configuredAgents(config), [config]);
  const o = board?.overall;
  return (
    <Section
      title="Do your agents beat the market?"
      description="Every forecast an agent records, scored against the market price at that moment once the market resolves. Brier score: lower is better."
    >
      <div data-tour="agent-scoreboard">
        {error && <div className="mb-3"><Note tone="warn">{error}</Note></div>}
        {!board || !o ? (
          <Card><div className="py-6 text-center text-sm text-krypt-muted">{error ? 'Unavailable.' : 'Loading…'}</div></Card>
        ) : board.totalForecasts === 0 ? (
          <Empty
            title="No forecasts yet"
            description="An agent records one with record_forecast before every buy (and should for every market it studies). Each is scored when its market resolves."
          />
        ) : (
          <>
            <Card className="mb-3">
              <p className={cls('text-sm', VERDICT[o.verdict].tone)}>{VERDICT[o.verdict].text}</p>
              <p className="mt-1 text-[11px] text-krypt-dim">
                {o.nPaired} of {board.minScored} resolved markets needed for a verdict
                {o.diffSe !== null && o.diffMean !== null && (
                  <> · agent − market Brier {o.diffMean >= 0 ? '+' : ''}{o.diffMean.toFixed(4)} ± {(2 * o.diffSe).toFixed(4)} (2 s.e.)</>
                )}
              </p>
            </Card>
            <div className="mb-3 grid gap-3 md:grid-cols-4">
              <StatCard label="Agent Brier" value={brier(o.brierAgentPaired ?? o.brierAgent)}
                hint="On markets that had a two-sided quote" />
              <StatCard label="Market Brier" value={brier(o.brierMarket)}
                hint="The mid at the moment of each forecast" />
              <StatCard
                label="Skill vs market"
                value={o.skill === null ? '—' : `${(o.skill * 100).toFixed(1)}%`}
                accent={o.verdict === 'agent-better' ? 'good' : o.verdict === 'market-better' ? 'bad' : 'neutral'}
                hint=">0 means the agents' error was smaller"
              />
              <StatCard label="Resolved / pending"
                value={`${board.scoredMarkets} / ${board.pending}`}
                hint={`${board.totalForecasts} forecasts recorded`} />
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <Card header={<span className="text-xs font-medium text-white">By agent</span>}>
                {!board.byAgent?.length ? (
                  <p className="py-4 text-center text-xs text-krypt-muted">Nothing resolved yet.</p>
                ) : (
                  <table className="krypt-table">
                    <thead>
                      <tr>
                        <th className="krypt-th">Agent</th>
                        <th className="krypt-th text-right">Markets</th>
                        <th className="krypt-th text-right">Agent</th>
                        <th className="krypt-th text-right">Market</th>
                        <th className="krypt-th">Verdict</th>
                      </tr>
                    </thead>
                    <tbody>
                      {board.byAgent.map((r) => (
                        <tr key={r.agentId}>
                          <td className="krypt-td text-[11px]">{tagLabel(hubAgentIdentity({ agentId: r.agentId }, agents))}</td>
                          <td className="krypt-td text-right font-mono">{r.nPaired}/{r.n}</td>
                          <td className="krypt-td text-right font-mono">{brier(r.brierAgentPaired ?? r.brierAgent)}</td>
                          <td className="krypt-td text-right font-mono">{brier(r.brierMarket)}</td>
                          <td className={cls('krypt-td text-[11px]', VERDICT[r.verdict].tone)}>{r.verdict}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
              <Card header={<span className="text-xs font-medium text-white">Calibration — when it said X%, how often was it YES?</span>}>
                {board.buckets.length === 0 ? (
                  <p className="py-4 text-center text-xs text-krypt-muted">Nothing resolved yet.</p>
                ) : (
                  <table className="krypt-table">
                    <thead>
                      <tr>
                        <th className="krypt-th">Said</th>
                        <th className="krypt-th text-right">n</th>
                        <th className="krypt-th text-right">Avg forecast</th>
                        <th className="krypt-th text-right">Happened</th>
                      </tr>
                    </thead>
                    <tbody>
                      {board.buckets.map((b) => (
                        <tr key={b.lo}>
                          <td className="krypt-td font-mono">{Math.round(b.lo * 100)}–{Math.round(b.hi * 100)}%</td>
                          <td className="krypt-td text-right font-mono">{b.n}</td>
                          <td className="krypt-td text-right font-mono">{(b.meanForecast * 100).toFixed(0)}%</td>
                          <td className="krypt-td text-right font-mono">{(b.hitRate * 100).toFixed(0)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Card>
            </div>

            <Card className="mt-3" header={<span className="text-xs font-medium text-white">Recent forecasts</span>}>
              <table className="krypt-table">
                <thead>
                  <tr>
                    <th className="krypt-th w-20">When</th>
                    <th className="krypt-th">Market</th>
                    <th className="krypt-th w-28">By</th>
                    <th className="krypt-th w-16 text-right">Agent</th>
                    <th className="krypt-th w-16 text-right">Mid</th>
                    <th className="krypt-th w-20 text-right">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {board.recent.map((r) => (
                    <tr key={r.id}>
                      <td className="krypt-td text-krypt-muted">{fmtTimeShort(r.createdAt)}</td>
                      <td className="krypt-td">
                        <div className="truncate text-[11px] text-white/90" title={r.conditionId}>{r.title ?? shortCid(r.conditionId)}</div>
                      </td>
                      <td className="krypt-td text-[11px] text-krypt-muted">
                        {tagLabel(hubAgentIdentity({ agentId: r.agentId, client: r.client, model: r.model }, agents))}
                      </td>
                      <td className="krypt-td text-right font-mono">{fmtCents(r.fairValueCents)}</td>
                      <td className="krypt-td text-right font-mono">{fmtCents(r.marketMidCents)}</td>
                      <td className="krypt-td text-right font-mono">
                        {r.outcome === null ? <span className="text-krypt-dim">pending</span>
                          : r.outcome === 1 ? 'YES' : r.outcome === 0 ? 'NO' : `${(r.outcome * 100).toFixed(0)}¢`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </>
        )}
      </div>
    </Section>
  );
}

function AgentActivity({
  data, error, audit, onSettle,
}: {
  data: McpActivity | null; error: string | null; audit: McpStatus['audit'];
  /** Live orders whose outcome is unknown hold the agent's caps until checked. */
  onSettle: (id: number) => Promise<void>;
}) {
  const [settling, setSettling] = useState<number | null>(null);
  return (
    <Section title="Agent activity" description="Every order an agent attempted — refused ones too, with the reason — and every other action it took.">
      <div data-tour="agent-activity">
        {error && <div className="mb-3"><Note tone="warn">{error}</Note></div>}
        {!data || data.orders.length === 0 ? (
          <Empty title="No agent orders yet" description="Orders an agent places or attempts show up here, with the reason for every refusal." />
        ) : (
          <Card>
            <table className="krypt-table">
              <thead>
                <tr>
                  <th className="krypt-th w-20">When</th>
                  <th className="krypt-th w-14">Mode</th>
                  <th className="krypt-th">Order</th>
                  <th className="krypt-th">Result</th>
                </tr>
              </thead>
              <tbody>
                {data.orders.map((o) => (
                  <tr key={o.id}>
                    <td className="krypt-td text-krypt-muted">{fmtTimeShort(o.at)}</td>
                    <td className={cls('krypt-td text-[10px] uppercase tracking-wider',
                      o.mode === 'live' ? 'text-krypt-loss' : 'text-krypt-muted')}>{o.mode}</td>
                    <td className="krypt-td">
                      <span className="font-mono text-[11px] text-white/90">{orderLine(o)}</span>
                      <div className="truncate text-[11px] text-krypt-dim" title={o.conditionId}>{o.title ?? shortCid(o.conditionId)}</div>
                      <div className="text-[10px] text-krypt-dim">
                        <span className="text-krypt-purple">{o.agentName ?? (o.agentId ? 'deleted agent' : 'agent')}</span>
                        {o.client ? ` via ${o.client}` : ''}
                      </div>
                    </td>
                    <td className={cls('krypt-td text-[11px]', o.ok ? 'text-krypt-win' : o.status === 'pending' ? 'text-krypt-purple' : 'text-krypt-warn')}>
                      {o.status && o.status !== 'approved' && (
                        <span className="mr-1 uppercase tracking-wider">[{o.status}]</span>
                      )}
                      {o.message}
                      {o.mode === 'live' && o.status === 'unknown' && (
                        <button
                          type="button"
                          className="krypt-btn-default ml-2 px-2 py-0.5 text-[10px]"
                          disabled={settling !== null}
                          data-qa={`settle-unknown-${o.id}`}
                          title="Ask Polymarket US what happened to this order and book it. Until then it counts against the agent's daily spend and position slots."
                          onClick={() => {
                            setSettling(o.id);
                            void onSettle(o.id).finally(() => setSettling(null));
                          }}
                        >
                          {settling === o.id ? 'Checking…' : 'Check with Polymarket US'}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        )}
        {audit.length > 0 && (
          <Card className="mt-3" header={<span className="text-xs font-medium text-white">Other agent actions</span>}>
            <div className="max-h-80 space-y-1 overflow-y-auto">
              {audit.map((r, i) => (
                <div key={`${r.at}-${i}`} className="flex items-center gap-2 text-[11px]">
                  <span className="w-20 shrink-0 text-krypt-dim">{fmtTimeShort(r.at)}</span>
                  <span className="w-28 shrink-0 truncate text-krypt-muted">{r.client}</span>
                  <span className="w-36 shrink-0 truncate font-mono text-white">{r.tool}</span>
                  <span className="min-w-0 flex-1 truncate font-mono text-krypt-dim" title={r.detail}>{r.detail}</span>
                  <span className={cls('shrink-0 rounded px-1.5 py-0.5 uppercase tracking-wider',
                    r.outcome === 'accepted' ? 'text-krypt-win'
                      : r.outcome === 'refused' ? 'bg-krypt-loss/15 text-krypt-loss'
                        : 'text-krypt-warn')}>
                    {r.outcome}
                  </span>
                </div>
              ))}
            </div>
          </Card>
        )}
      </div>
    </Section>
  );
}
