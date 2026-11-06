import type { McpAgent } from '@shared/agents';

/**
 * "Run as agent: [Default ▾]" — which of the user's named agents something
 * (Autopilot) acts as. A plain select on purpose: it is one choice in a form
 * that already has a dozen, and its whole job is to be unambiguous. A
 * switched-off agent is listed but not choosable, because the backend refuses
 * to run as it.
 */
export function AgentPicker({
  agents, value, onChange, label = 'Run as agent', hint, testId,
}: {
  agents: McpAgent[];
  value: string;
  onChange: (id: string) => void;
  label?: string;
  hint?: string;
  testId?: string;
}) {
  const known = agents.some((a) => a.id === value);
  return (
    <div>
      <span className="krypt-label">{label}</span>
      <select
        value={known ? value : ''}
        onChange={(e) => { if (e.target.value) onChange(e.target.value); }}
        className="krypt-input"
        data-testid={testId}
      >
        {!known && <option value="">(deleted agent: pick another)</option>}
        {agents.map((a) => (
          <option key={a.id} value={a.id} disabled={!a.enabled}>
            {a.emoji} {a.name}{a.enabled ? '' : ' (switched off)'}
          </option>
        ))}
      </select>
      {hint && <span className="krypt-help block">{hint}</span>}
    </div>
  );
}
