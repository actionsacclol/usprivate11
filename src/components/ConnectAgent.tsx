import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowRight, Bot, CheckCircle2, Copy, Eye, EyeOff, FolderOpen, Loader2, Trash2, Wand2,
} from 'lucide-react';
import type { McpToolCallEvent } from '@shared/market';
import type { McpClientId, McpClientSetup } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { agentIdentity, clientMatchesPick } from '../utils/agents';
import { cls } from '../utils/format';
import { DEFAULT_AGENT_ID } from '@shared/agents';
import { Modal } from './common';
import { AutopilotQuickstart } from './AutopilotQuickstart';
import { userMessage } from '../utils/errors';

/**
 * Pick your agent's client, add or copy its setup, and watch for it to show up.
 *
 * Used by onboarding and by each agent's Connect dialog. The rules:
 *
 *  - No token is involved. Polybot's client setups are stdio launches of this
 *    app's own backend with `--mcp --agent <id>`: the relay reads that agent's
 *    token from the encrypted store itself, so a config file — or the
 *    clipboard — never holds a secret. (The HTTP API is the one door that
 *    needs a token; that lives on its own card.)
 *  - "Add" edits the client's own config file in MAIN: a timestamped backup
 *    first, only our own entry touched, an unreadable file refused. Claude
 *    Code is the exception: "Add" runs its own `claude mcp add`.
 *  - Detecting the agent is cheap. It listens to the backend's tool-call push
 *    and, as a slow fallback, reads `mcp.seen` — backend memory only. It never
 *    polls `mcp.status`.
 *  - It never turns anything up past paper. `paperDefault` (onboarding) sets
 *    the server on and trade mode to paper from OFF; elsewhere it only switches
 *    the server on. From live it says so and leaves the mode alone, and if the
 *    server is off it asks before switching it back on.
 */

export const AGENT_CLIENTS: { id: McpClientId; name: string; where: string; oneClick?: boolean }[] = [
  {
    id: 'claude-desktop', name: 'Claude Desktop', oneClick: true,
    where: 'Press Add to Claude Desktop (the app edits its config for you, keeping a dated backup), then fully quit Claude Desktop — right-click its tray icon → Quit — and open it again.',
  },
  {
    id: 'claude-code', name: 'Claude Code', oneClick: true,
    where: 'Press Add to Claude Code. It sets this up for every project on this computer. Then start a new Claude Code session.',
  },
  {
    id: 'cursor', name: 'Cursor', oneClick: true,
    where: 'Press Add to Cursor (it writes ~/.cursor/mcp.json, keeping a backup), then check Cursor Settings → MCP shows it switched on.',
  },
  {
    id: 'codex', name: 'Codex', oneClick: true,
    where: 'Press Add to Codex (it replaces an older entry for this agent instead of adding a second, and keeps a backup), then restart Codex.',
  },
  {
    id: 'windsurf', name: 'Windsurf', oneClick: true,
    where: 'Press Add to Windsurf (it writes its mcp_config.json, keeping a backup), then restart Windsurf.',
  },
  {
    id: 'vscode', name: 'VS Code',
    where: 'Command Palette → "MCP: Open User Configuration", then merge the copied block in.',
  },
];

/** After this long without a call, the waiting line says what usually fixes it. */
export const WAIT_HINT_MS = 60_000;

export type Pick = McpClientId | 'autopilot';

/** How often the fallback reads backend memory while waiting. */
const SEEN_FALLBACK_MS = 5_000;

export function ConnectAgent({
  paperDefault, onAutopilot, autopilotSetup = 'modal', className, agentId, hideAutopilot, serverName,
}: {
  /** The name the agent's config files the server under. */
  serverName?: string;
  /** Onboarding: server on AND trade mode paper (from off). Never live. */
  paperDefault?: boolean;
  /** Which named agent the setup connects AS. Omitted = the Default agent. */
  agentId?: string;
  /** The per-agent Connect dialog: Autopilot is picked on its own panel. */
  hideAutopilot?: boolean;
  /** "In-app Autopilot" has no config: it uses the AI key. With
   *  autopilotSetup 'callback' this IS the setup (onboarding shows the
   *  quickstart as its own step); with 'modal' it is the "AI settings" link
   *  and "Set up Autopilot" opens the quickstart here. */
  onAutopilot: () => void;
  autopilotSetup?: 'modal' | 'callback';
  className?: string;
}) {
  const { config, refresh, backend } = useApp();
  const toast = useToast();
  const aid = agentId ?? DEFAULT_AGENT_ID;
  const [quick, setQuick] = useState(false);
  // In-app Autopilot first: it is the only choice that needs nothing
  // installed. The per-agent dialog (hideAutopilot) starts on Claude Desktop.
  const [pick, setPick] = useState<Pick>(hideAutopilot ? 'claude-desktop' : 'autopilot');
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState<McpClientId | null>(null);
  const [copiedAt, setCopiedAt] = useState<number | null>(null);
  const [setups, setSetups] = useState<McpClientSetup[]>([]);
  const [showSnippet, setShowSnippet] = useState(false);
  const engineDown = backend?.status !== 'running';
  const [seen, setSeen] = useState<{ label: string; what: string } | null>(null);
  const [other, setOther] = useState<string | null>(null);

  const want = pick === 'autopilot' ? null : pick;
  const liveNow = config?.mcpTradeMode === 'live';
  const wantRef = useRef(want);
  wantRef.current = want;

  const loadSetups = useCallback(async (): Promise<void> => {
    try {
      setSetups(await window.krypt.mcp.clients(aid));
    } catch { /* keep the last list */ }
  }, [aid]);
  useEffect(() => { void loadSetups(); }, [loadSetups]);

  // The push stream: green the moment the picked client connects or calls.
  useEffect(() => {
    setSeen(null);
    setOther(null);
    setShowSnippet(false);
    if (!want) return;
    const off = window.krypt.mcp.onToolCall?.((d: McpToolCallEvent) => {
      // Connecting a named agent: only a call made with ITS token counts.
      if (d?.agentId && d.agentId !== aid) return;
      const id = agentIdentity({ client: d?.client, model: d?.model, transport: d?.transport });
      if (clientMatchesPick(d?.client, want)) {
        setSeen({ label: id.label, what: d.kind === 'connect' ? 'connected' : `called ${d.tool ?? 'a tool'}` });
      } else if (!String(d?.client ?? '').toLowerCase().startsWith('autopilot')) {
        setOther(id.label);
      }
    });
    return () => { off?.(); };
  }, [want, aid]);

  // The fallback: backend memory, while waiting only.
  useEffect(() => {
    if (!want || seen) return;
    let alive = true;
    const read = async () => {
      try {
        const s = await window.krypt.mcp.seen();
        if (!alive || !s) return;
        const hit = (s.clients ?? []).find((c) => clientMatchesPick(c.client, want)
          && (!c.agentId || c.agentId === aid));
        if (hit) {
          const id = agentIdentity({ client: hit.client });
          setSeen({ label: id.label, what: hit.called ? 'made a call' : 'connected' });
        }
      } catch { /* backend down: keep waiting */ }
    };
    void read();
    const i = window.setInterval(read, SEEN_FALLBACK_MS);
    return () => { alive = false; window.clearInterval(i); };
  }, [want, seen, aid]);

  /** Server on (and paper, from onboarding) — shared by Copy and Add. */
  const ensureServer = async (): Promise<boolean> => {
    const p: Record<string, unknown> = {};
    if (!config?.mcpEnabled) {
      if (liveNow && config?.accountMode === 'live' && !window.confirm(
        'Agent trading is set to LIVE and the agent server is off.\n\n'
        + 'Switching the server on lets a connected agent place REAL-money orders '
        + '(your approvals and caps still apply). Switch it on?\n\n'
        + 'Cancel changes nothing. To use paper instead, switch to it on the AI Agents page.',
      )) return false;
      p.mcpEnabled = true;
    }
    if (paperDefault && config?.mcpTradeMode === 'off') p.mcpTradeMode = 'paper';
    if (Object.keys(p).length) {
      await window.krypt.config.update(p as never);
      await refresh.state();
    }
    return true;
  };

  const install = async (client: McpClientId, name: string): Promise<void> => {
    setBusy(true);
    try {
      if (!(await ensureServer())) return;
      const r = await window.krypt.mcp.install(client, aid);
      if (r.ok) {
        setCopied(client);
        setCopiedAt(Date.now());
        toast.push(r.message, 'success', 12_000);
      } else {
        toast.push(r.message || `Couldn't add it to ${name}.`, 'error', 12_000);
      }
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
      void loadSetups();
    }
  };

  const uninstall = async (client: McpClientId, name: string): Promise<void> => {
    setBusy(true);
    try {
      const r = await window.krypt.mcp.uninstall(client, aid);
      if (r.ok) toast.success(r.message);
      else toast.push(r.message || `Couldn't remove it from ${name}.`, 'error', 10_000);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
      void loadSetups();
    }
  };

  const openFolder = async (client: McpClientId): Promise<void> => {
    try {
      const r = await window.krypt.mcp.openConfigFolder(client);
      if (r && !r.ok && r.message) toast.warn(r.message);
    } catch (e) {
      toast.error(userMessage(e));
    }
  };

  const copy = async (client: McpClientId, name: string): Promise<void> => {
    setBusy(true);
    try {
      if (!(await ensureServer())) return;
      const r = await window.krypt.mcp.copyConfig(client, aid);
      if (!r.ok) {
        toast.error(r.message || 'Could not copy the setup.');
        return;
      }
      setCopied(client);
      setCopiedAt(Date.now());
      toast.success(`${name} setup copied. It holds no token — the app hands the agent its token itself.`);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const chosen = AGENT_CLIENTS.find((c) => c.id === pick);
  const setup = chosen ? setups.find((s) => s.id === chosen.id) ?? null : null;
  const name = serverName ?? setup?.serverName ?? 'krypt-polybot-us';
  const canAdd = !!chosen?.oneClick && (setup ? setup.canInstall : true);
  return (
    <div className={cls('space-y-3', className)} data-tour="connect-agent">
      {paperDefault && liveNow && (
        <div
          role="note"
          data-testid="connect-live-note"
          className="rounded-lg border border-krypt-loss/40 bg-krypt-loss/10 px-3 py-2 text-[11px] leading-relaxed text-krypt-loss"
        >
          <span className="font-semibold">Agent trading is set to LIVE.</span> Connecting a client
          won&apos;t change that: it does not switch them to paper. Do that on the AI Agents page if you want to.
        </div>
      )}
      {engineDown && (
        <div role="note" data-testid="connect-engine-down"
          className="rounded-lg border border-krypt-warn/40 bg-krypt-warn/10 px-3 py-2 text-[11px] leading-relaxed text-krypt-warn">
          The trading engine isn&apos;t running, so there is no agent server to connect to yet. Start
          it from the status bar; these buttons wake up when it&apos;s running.
        </div>
      )}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {[...(hideAutopilot ? [] : [{ id: 'autopilot' as Pick, name: 'In-app Autopilot (easiest)' }]),
          ...AGENT_CLIENTS.map((c) => ({ id: c.id as Pick, name: c.name }))].map((c) => {
          const st = c.id === 'autopilot' ? null : setups.find((s) => s.id === c.id);
          return (
            <button
              key={c.id}
              type="button"
              onClick={() => setPick(c.id)}
              data-testid={`connect-pick-${c.id}`}
              className={cls(
                'rounded-lg border px-2 py-2 text-xs font-medium transition',
                pick === c.id
                  ? 'border-krypt-purple/70 bg-krypt-purple/15 text-white shadow-[0_0_16px_-6px_rgba(59,130,246,0.8)]'
                  : 'border-krypt-border bg-krypt-surface2 text-krypt-muted hover:border-krypt-borderHi hover:text-white',
              )}
            >
              {c.name}
              {st?.installed && <span className="ml-1 text-[9px] uppercase tracking-wider text-krypt-win">added</span>}
            </button>
          );
        })}
      </div>

      {chosen ? (
        <div className="rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
          <div className="flex items-start gap-3">
            <div className="flex-1 text-[11px] leading-relaxed text-krypt-muted">
              {canAdd ? (
                <>
                  <span className="text-white">1.</span> {chosen.where}{' '}
                  <span className="text-white">2.</span> Ask it to use <span className="font-mono text-white">{name}</span>.
                </>
              ) : (
                <>
                  <span className="text-white">1.</span> Copy the setup.{' '}
                  <span className="text-white">2.</span> {chosen.where}{' '}
                  <span className="text-white">3.</span> Ask it to use <span className="font-mono text-white">{name}</span>.
                </>
              )}
              {setup?.note && <span className="mt-1 block text-krypt-dim">{setup.note}</span>}
              {setup && !setup.detected && chosen.oneClick && (
                <span className="mt-1 block text-krypt-warn">
                  {chosen.name} doesn&apos;t look installed on this computer (its settings folder isn&apos;t there yet).
                </span>
              )}
            </div>
            <div className="flex shrink-0 flex-col items-end gap-1.5">
              {canAdd && (
                <button
                  type="button"
                  onClick={() => void install(chosen.id, chosen.name)}
                  disabled={busy || engineDown}
                  className="krypt-btn-primary"
                  data-testid="connect-install"
                  title={chosen.id === 'claude-code'
                    ? "Runs Claude Code's own setup command for you."
                    : "Writes the entry into that app's own config file for you. A dated backup of the old file is kept beside it."}
                >
                  <Wand2 className="h-4 w-4" /> {setup?.installed ? 'Re-add to' : 'Add to'} {chosen.name}
                </button>
              )}
              <button
                type="button"
                onClick={() => void copy(chosen.id, chosen.name)}
                disabled={busy || engineDown}
                className={canAdd ? 'krypt-btn-default' : 'krypt-btn-primary'}
                data-testid="connect-copy"
                title="Puts the setup on your clipboard. It holds no token."
              >
                <Copy className="h-4 w-4" /> Copy setup
              </button>
              <div className="flex items-center gap-3">
                {setup?.snippet && (
                  <button type="button" onClick={() => setShowSnippet((v) => !v)}
                    className="text-[11px] text-krypt-muted hover:text-white">
                    {showSnippet ? <EyeOff className="mr-1 inline h-3 w-3" /> : <Eye className="mr-1 inline h-3 w-3" />}
                    {showSnippet ? 'Hide' : 'Show'}
                  </button>
                )}
                {chosen.oneClick && chosen.id !== 'claude-code' && (
                  <button
                    type="button"
                    onClick={() => void openFolder(chosen.id)}
                    className="text-[11px] text-krypt-muted hover:text-white"
                  >
                    <FolderOpen className="mr-1 inline h-3 w-3" />Open folder
                  </button>
                )}
                {setup?.installed && chosen.oneClick && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void uninstall(chosen.id, chosen.name)}
                    className="text-[11px] text-krypt-muted hover:text-krypt-loss"
                    title={chosen.id === 'claude-code'
                      ? 'Remove this agent from Claude Code'
                      : "Remove this agent's entry from that app's config (a backup is kept)"}
                  >
                    <Trash2 className="mr-1 inline h-3 w-3" />Remove
                  </button>
                )}
              </div>
            </div>
          </div>
          {showSnippet && setup?.snippet && (
            <div className="mt-2">
              {setup.configPath && (
                <div className="mb-1 truncate font-mono text-[10px] text-krypt-dim" title={setup.configPath}>
                  {setup.configPath}
                </div>
              )}
              <pre className="max-h-48 overflow-auto rounded-lg border border-krypt-border bg-black/30 p-2 font-mono text-[10px] text-krypt-muted">
                {setup.snippet}
              </pre>
            </div>
          )}
          <WaitingLine
            copied={copied === chosen.id} copiedAt={copied === chosen.id ? copiedAt : null}
            seen={seen} other={other} name={chosen.name} server={name}
          />
        </div>
      ) : (
        <div className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
          <Bot className="mt-0.5 h-4 w-4 shrink-0 text-krypt-purple" />
          <div className="flex-1 text-[11px] leading-relaxed text-krypt-muted">
            Nothing to install: the app runs the agent itself, on a schedule, with an AI you choose.
            A cloud key (Claude, OpenAI, OpenRouter, Gemini; billed to <span className="text-white">you</span>)
            or a free model on this computer (Ollama, LM Studio). It starts on paper, with daily run,
            token and tool-call budgets, and stays off until you switch it on.
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <button
              type="button"
              data-testid="connect-autopilot-setup"
              onClick={() => (autopilotSetup === 'callback' ? onAutopilot() : setQuick(true))}
              disabled={engineDown}
              className="krypt-btn-primary"
            >
              Set up Autopilot <ArrowRight className="h-4 w-4" />
            </button>
            {autopilotSetup === 'modal' && (
              <button type="button" onClick={onAutopilot} className="text-[11px] text-krypt-muted hover:text-white">
                AI settings
              </button>
            )}
          </div>
        </div>
      )}
      {autopilotSetup === 'modal' && (
        <Modal open={quick} onClose={() => setQuick(false)} maxWidth="max-w-2xl">
          <AutopilotQuickstart onDone={() => setQuick(false)} onSkip={() => setQuick(false)} />
        </Modal>
      )}
    </div>
  );
}

function WaitingLine({ copied, copiedAt, seen, other, name, server }: {
  copied: boolean; copiedAt: number | null; seen: { label: string; what: string } | null;
  other: string | null; name: string; server: string;
}) {
  // A minute after the setup went out with no call back, the usual cause is
  // a client that only reads its config at startup.
  const [late, setLate] = useState(false);
  useEffect(() => {
    setLate(false);
    if (!copied || !copiedAt || seen) return undefined;
    const t = window.setTimeout(() => setLate(true), Math.max(0, copiedAt + WAIT_HINT_MS - Date.now()));
    return () => window.clearTimeout(t);
  }, [copied, copiedAt, seen]);
  if (seen) {
    return (
      <div className="mt-2 flex items-center gap-2 text-xs text-krypt-win" role="status" data-testid="connect-seen">
        <CheckCircle2 className="h-4 w-4" />
        <span><span className="font-medium">{seen.label}</span> {seen.what}. It&apos;s connected.</span>
      </div>
    );
  }
  return (
    <div className="mt-2 flex items-center gap-2 text-xs text-krypt-muted" role="status">
      {copied ? <Loader2 className="h-3.5 w-3.5 animate-spin text-krypt-purple" /> : <span className="h-2 w-2 rounded-full bg-krypt-dim" />}
      <span>
        {copied ? `Waiting for ${name}'s first call…` : `Waiting for ${name}…`}
        {other && <span className="text-krypt-dim"> (saw {other} — a different client; it works too)</span>}
        {late && (
          <span className="mt-0.5 block text-krypt-warn" data-testid="connect-wait-hint">
            Still nothing? {waitHint(name, server)}
          </span>
        )}
      </span>
    </div>
  );
}

/** What usually fixes "I added it and nothing happened", per client. */
export function waitHint(name: string, server = 'krypt-polybot-us'): string {
  if (name === 'Claude Desktop') {
    return `Restart Claude Desktop after adding — fully quit it (right-click its tray icon → Quit), open it again, then ask it to use ${server}.`;
  }
  if (name === 'Claude Code') return `Start a new Claude Code session after adding, then ask it to use ${server}.`;
  if (name === 'Codex') return `Restart Codex after adding, then ask it to use ${server}.`;
  if (name === 'Cursor') return `Check Cursor Settings → MCP shows it switched on and green, then ask it to use ${server}.`;
  return `Restart ${name} after adding, then ask it to use ${server}. The app has to stay open while an agent uses it.`;
}
