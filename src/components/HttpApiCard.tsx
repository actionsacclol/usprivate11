import { useMemo, useState } from 'react';
import { Copy, Globe } from 'lucide-react';
import type { McpStatus, TraderConfig } from '@shared/types';
import { agentsOf, DEFAULT_AGENT_ID } from '@shared/agents';
import { Card, Switch } from './common';
import { AgentPicker } from './agents/AgentPicker';
import { useToast } from '../state/ToastProvider';
import { userMessage } from '../utils/errors';

/**
 * The plain HTTP API, for scripts, n8n, LangChain — anything that does not
 * speak MCP. Same listener, and every call goes through the same dispatcher
 * as an MCP tool call, so the rails are the ones on this page.
 *
 * This is the ONE door that needs a token in the caller's hands (MCP clients
 * are token-less stdio setups). So the snippet is built by the backend and put
 * on the clipboard by main, and this component only ever learns `{ ok }`.
 * Each agent has its own token: the caller IS the agent whose snippet it used.
 */
export function HttpApiCard({
  config, status, patch,
}: {
  config: TraderConfig | null;
  status: McpStatus | null;
  patch: (p: Record<string, unknown>) => Promise<void>;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const agents = useMemo(() => agentsOf(config), [config]);
  const [agentId, setAgentId] = useState<string>(DEFAULT_AGENT_ID);
  const port = status?.port || config?.mcpPort || 47635;
  const serverOn = !!config?.mcpEnabled;
  const apiOn = serverOn && !!config?.mcpHttpEnabled;
  const base = `http://127.0.0.1:${port}/api/v1`;
  const name = agents.find((a) => a.id === agentId)?.name ?? 'Default';

  const copy = async (): Promise<void> => {
    setBusy(true);
    try {
      const r = await window.krypt.mcp.copyHttpSnippet(agentId);
      if (r.ok) toast.success(r.message ?? `curl and Python examples for ${name} copied. They contain its token — treat it like a password.`);
      else toast.error(r.message ?? 'Could not copy the examples.');
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="mt-3">
      <div data-tour="http-api">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-sm font-medium text-white">
              <Globe className="h-4 w-4 text-krypt-muted" /> HTTP API
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-krypt-muted">
              For scripts, n8n, LangChain or your own bot. Same port as MCP, and every call runs
              through the same rails: trade mode, a forecast before every buy, caps, approvals and
              the daily loss stop. The token in the examples decides which of your agents the
              caller is — its rules, its caps, its paper book.
            </p>
          </div>
        </div>
        <div className="mt-3">
          <Switch
            checked={apiOn}
            onChange={(v) => void patch({ mcpHttpEnabled: v })}
            label="Enable the HTTP API"
            description={serverOn
              ? 'Off by default, separately from MCP. Loopback only; the token is required on every request.'
              : 'Switch on "Allow AI agents" above first.'}
            disabled={!serverOn}
          />
        </div>
        <div className="mt-3 grid items-end gap-3 sm:grid-cols-[1fr,auto]">
          <AgentPicker
            agents={agents}
            value={agentId}
            onChange={setAgentId}
            label="Examples for agent"
            hint="Each agent has its own token. Rotate it on its card if a snippet leaks."
          />
          <button
            onClick={() => void copy()}
            disabled={!apiOn || busy}
            className="krypt-btn-default shrink-0"
            data-qa="http-copy"
            title={apiOn ? 'Copy curl + Python examples (token included) to the clipboard' : 'Turn on the HTTP API first'}
          >
            <Copy className="h-4 w-4" /> Copy curl + Python
          </button>
        </div>
        <div className="mt-3 space-y-1 rounded-lg border border-krypt-border bg-krypt-surface2 p-3 font-mono text-[11px] text-krypt-muted">
          <div><span className="text-krypt-win">GET </span> {base}/tools</div>
          <div><span className="text-krypt-warn">POST</span> {base}/tools/<span className="text-white/80">{'{name}'}</span> <span className="font-sans text-krypt-dim">— JSON arguments in the body</span></div>
          <div><span className="text-krypt-win">GET </span> {base}/openapi.json <span className="font-sans text-krypt-dim">— import into LangChain / n8n</span></div>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">
          A refusal comes back as JSON with the same reason an MCP client reads (422; 404 for an
          unknown tool; 429 when rate-limited; 403 while the API is off or the agent is switched
          off). The token never appears on this screen: copying puts it straight on your clipboard.
        </p>
      </div>
    </Card>
  );
}
