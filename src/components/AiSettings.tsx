import { useCallback, useEffect, useState } from 'react';
import { Check, Cpu, PlugZap, Sparkles, Trash2 } from 'lucide-react';
import type { TraderConfig } from '@shared/types';
import type { AiProvider, AiProviderCaps, AiProviderCheck, AiStatus } from '@shared/market';
import { Card, Section, Switch } from './common';
import { useToast } from '../state/ToastProvider';
import { cls } from '../utils/format';
import { userMessage } from '../utils/errors';

/**
 * AI provider settings: the Market page's "Read this market" analysis and
 * Autopilot both run on what is chosen here.
 *
 * The key is the reason this is its own component: it never enters `config`.
 * Everything else here is written to settings.json; the key goes straight to
 * the backend's encrypted credential store (the same one holding the API
 * key), is verified against the provider before it is stored, and comes back
 * only as present + a masked shape. There is no reveal: once saved, the only
 * operations are replace and remove.
 *
 * The renderer names a provider id and a model id — never a host. Where each
 * provider lives is decided in python/ai_providers.py.
 */

/** Fields the backend sends that the shared type leaves loose. */
type Caps = AiProviderCaps & { catalogue?: 'curated' | 'listed'; modelsEndpoint?: string };
type Status = AiStatus & {
  prices?: Record<string, { inPerMTok: number; outPerMTok: number }>;
  capabilities?: Partial<Record<AiProvider, Caps>>;
};
type Check = AiProviderCheck & {
  modelInfo?: { id: string; tools: boolean | null; context?: number | null; loaded?: boolean | null }[];
};

/** Display order, and the copy that is ours rather than the backend's. */
export const AI_PROVIDER_CHOICES: { id: AiProvider; name: string; keyHint?: string }[] = [
  { id: 'anthropic', name: 'Claude', keyHint: 'sk-ant-…' },
  { id: 'openai', name: 'OpenAI', keyHint: 'sk-…' },
  { id: 'openrouter', name: 'OpenRouter', keyHint: 'sk-or-…' },
  { id: 'gemini', name: 'Gemini', keyHint: 'AIza…' },
  { id: 'ollama', name: 'Ollama' },
  { id: 'lmstudio', name: 'LM Studio' },
];

const LOCAL_SETUP: Partial<Record<AiProvider, string>> = {
  ollama: 'Install Ollama, start it (`ollama serve`, or open the app) and pull a model '
    + 'that can call tools, e.g. `ollama pull llama3.1:8b`.',
  lmstudio: 'In LM Studio, download a model, then open the Developer tab and start the '
    + 'local server (or run `lms server start`). Leave the model blank to use whichever '
    + 'one is loaded.',
};

export const isLocalProvider = (p: AiProvider): boolean => p === 'ollama' || p === 'lmstudio';

/** A provider's model list, whatever shape the backend sent (an older one
 *  kept one chosen model per provider as a string). */
export function modelList(status: AiStatus | null | undefined, p: AiProvider): string[] {
  const v = status?.models?.[p];
  if (Array.isArray(v)) return v.filter((m): m is string => typeof m === 'string' && !!m);
  return typeof v === 'string' && v ? [v] : [];
}

export function keyPresent(status: AiStatus | null | undefined, p: AiProvider): boolean {
  const k = status?.keys?.[p] as unknown;
  if (typeof k === 'boolean') return k;
  return !!(k && typeof k === 'object' && (k as { present?: unknown }).present === true);
}

export function AiSettings({
  config, update,
}: {
  config: TraderConfig;
  update: <K extends keyof TraderConfig>(k: K, v: TraderConfig[K]) => Promise<void>;
}) {
  const toast = useToast();
  const [status, setStatus] = useState<Status | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<Check | null>(null);
  const [listed, setListed] = useState<string[]>([]);

  const provider: AiProvider = (config.aiProvider ?? 'anthropic') as AiProvider;
  const caps = status?.capabilities?.[provider];
  const label = caps?.label ?? AI_PROVIDER_CHOICES.find((p) => p.id === provider)?.name ?? provider;
  const local = caps?.local ?? isLocalProvider(provider);
  const needsKey = caps?.needsKey ?? !local;

  const load = useCallback((): void => {
    window.krypt.ai.status().then((s) => setStatus(s as Status)).catch(() => setStatus(null));
  }, []);
  useEffect(load, [load]);

  const runCheck = useCallback(async (p: AiProvider): Promise<void> => {
    setChecking(true);
    try {
      const res = await window.krypt.ai.checkProvider(p) as Check;
      setCheck(res);
      if (res.models?.length) { setListed(res.models); load(); }
    } catch (e) {
      setCheck({ ok: false, provider: p, message: userMessage(e) });
    } finally {
      setChecking(false);
    }
  }, [load]);

  // Switching provider changes which key matters, so a half-typed one in the
  // box belongs to the provider the user just left. A local provider is
  // checked at once: it is a call to this machine and costs nothing. A cloud
  // one waits for the button, so opening Settings never sends a key anywhere.
  useEffect(() => {
    setDraft('');
    setCheck(null);
    setListed([]);
    if (isLocalProvider(provider)) void runCheck(provider);
  }, [provider, runCheck]);

  const pickProvider = async (p: AiProvider): Promise<void> => {
    if (p === provider) return;
    // ONE patch: the model id belongs to the provider just left, and a
    // provider-only write would push it to the new provider in between.
    // Empty means "that provider's default" (for LM Studio: the loaded
    // model). main broadcasts the new state, so the page re-renders from it.
    await window.krypt.config.update({ aiProvider: p, aiModel: '' });
    load();
  };

  const saveKey = async (key: string): Promise<void> => {
    setBusy(true);
    try {
      const r = await window.krypt.ai.setKey(provider, key);
      if (!r.ok) { toast.error(r.message || 'The key was not saved.'); return; }
      setDraft('');
      setCheck(null);
      if (r.models?.length) setListed(r.models);
      load();
      toast.success(key ? (r.message || `${label} key saved.`) : `${label} key removed.`);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const loadModels = async (): Promise<void> => {
    setBusy(true);
    try {
      const r = await window.krypt.ai.listModels(provider);
      if (!r.ok) { toast.error(r.message ?? 'Could not load models.'); return; }
      setListed(r.models);
      toast.success(`${r.models.length} models available.`);
    } catch (e) {
      toast.error(userMessage(e));
    } finally {
      setBusy(false);
    }
  };

  // Typed model ids are committed on blur / Enter, not per keystroke: every
  // commit is a settings write and a config push to the backend.
  const [modelDraft, setModelDraft] = useState(config.aiModel ?? '');
  useEffect(() => { setModelDraft(config.aiModel ?? ''); }, [config.aiModel, provider]);
  const commitModel = async (): Promise<void> => {
    const next = modelDraft.trim();
    if (next === (config.aiModel ?? '')) return;
    await update('aiModel', next);
    load();
  };

  const hasKey = keyPresent(status, provider);
  const masked = (status?.keys?.[provider] as { masked?: string | null } | undefined)?.masked ?? null;
  const models = Array.from(new Set([...listed, ...modelList(status, provider)]));
  const curated = caps?.catalogue === 'curated' && models.length > 0;
  const modelValue = config.aiModel || (status?.provider === provider ? status?.model ?? '' : '');
  const toolInfo = check?.modelInfo ? new Map(check.modelInfo.map((m) => [m.id, m.tools])) : null;
  const webOk = caps ? caps.webSearch !== false : true;

  return (
    <Section
      title="AI provider"
      description={
        'The language model behind "Read this market" and Autopilot. Bring a key from a cloud '
        + 'provider — billed to you directly — or run a model on this machine with Ollama or '
        + 'LM Studio. The app has no AI and no account of its own.'
      }
    >
      <Card>
        <div data-tour="ai-settings">
          <Switch
            label="Enable AI market analysis"
            description="Adds a 'Read this market' button to every market page. Nothing runs on a timer — each click is one request on your own provider."
            checked={!!config.aiEnabled}
            onChange={(v) => void update('aiEnabled', v)}
          />
        </div>

        {/* What leaves the machine, stated before the key box rather than
            after it. The privacy panel below lists the provider hosts with
            live call counts, so this claim is checkable. */}
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <div className="rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-xs text-krypt-muted">
            <div className="mb-1 font-medium text-white">What the analysis sends</div>
            The market&rsquo;s question, its published rules text, the current quote, volume and
            close time — public data anyone can read on Polymarket US.{' '}
            <strong className="text-white">Never your API key, your positions, your cost basis or
            your orders.</strong> A local model sends nothing off this computer at all.
          </div>
          <div className="rounded-lg border border-krypt-border bg-krypt-surface2/40 p-3 text-xs text-krypt-muted">
            <div className="mb-1 font-medium text-white">What it will not tell you</div>
            A probability, a fair value, or an edge in cents. The analysis is never asked for one
            and its response format gives it nowhere to put one: it returns a direction — looks
            cheap / rich / about right / no view — with its reasoning. Numbers come only from your
            agents&apos; own forecasts, which are scored against the market on the AI Agents page.
          </div>
        </div>

        <div className="mt-4">
          <Row label="Provider" hint="Which account, or which program on this machine, the model runs on.">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3" data-testid="ai-provider-picker">
              {AI_PROVIDER_CHOICES.map((p) => {
                const isLocal = status?.capabilities?.[p.id]?.local ?? isLocalProvider(p.id);
                return (
                  <button
                    key={p.id}
                    type="button"
                    data-testid={`ai-provider-${p.id}`}
                    onClick={() => void pickProvider(p.id)}
                    className={cls(
                      'flex items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-sm font-medium transition',
                      provider === p.id
                        ? 'border-krypt-purple/50 bg-krypt-purple/15 text-white'
                        : 'border-krypt-border text-krypt-dim hover:text-white',
                    )}
                  >
                    {isLocal && <Cpu className="h-3.5 w-3.5" />}
                    {p.name}
                    {keyPresent(status, p.id) && <Check className="h-3.5 w-3.5 text-krypt-win" />}
                  </button>
                );
              })}
            </div>
          </Row>
        </div>

        <div className="mt-4 grid gap-4 md:grid-cols-2">
          <Row
            label="Model"
            hint={curated
              ? 'Most capable first. Cost per analysis follows the same order.'
              : local
                ? 'Installed models appear here once the connection test sees them.'
                : 'Type any model id, or load the list your key can reach.'}
          >
            {curated ? (
              <select
                value={modelValue}
                onChange={(e) => void update('aiModel', e.target.value).then(load)}
                className="krypt-input"
              >
                {!models.includes(modelValue) && <option value={modelValue}>{modelValue || '(provider default)'}</option>}
                {models.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            ) : (
              <div className="flex gap-2">
                <input
                  list={`ai-models-${provider}`}
                  value={modelDraft}
                  spellCheck={false}
                  autoComplete="off"
                  data-testid="ai-model-input"
                  placeholder={provider === 'lmstudio'
                    ? 'blank = the model loaded in LM Studio'
                    : `default: ${models[0] ?? '—'}`}
                  onChange={(e) => setModelDraft(e.target.value)}
                  onBlur={() => void commitModel()}
                  onKeyDown={(e) => { if (e.key === 'Enter') void commitModel(); }}
                  className="krypt-input min-w-0 flex-1 font-mono"
                />
                <datalist id={`ai-models-${provider}`}>
                  {models.map((m) => {
                    const t = toolInfo?.get(m);
                    return (
                      <option key={m} value={m}>
                        {t === false ? 'no tool calling' : t ? 'tools' : ''}
                      </option>
                    );
                  })}
                </datalist>
                <button
                  type="button"
                  onClick={() => void loadModels()}
                  disabled={busy || (needsKey && !hasKey)}
                  title={needsKey && !hasKey ? 'Add an API key first.' : 'Ask the provider which models this key can reach'}
                  className="krypt-btn-default shrink-0 text-xs"
                >
                  Load
                </button>
              </div>
            )}
          </Row>

          <Row
            label="Connection"
            hint="Uses free endpoints only (the model list) — never a billed request."
          >
            <button
              type="button"
              data-testid="ai-test-connection"
              disabled={checking || (needsKey && !hasKey)}
              onClick={() => void runCheck(provider)}
              className={cls(
                'inline-flex w-full items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition',
                checking || (needsKey && !hasKey)
                  ? 'cursor-not-allowed border-krypt-border text-krypt-dim'
                  : 'border-krypt-purple/40 bg-krypt-purple/15 text-krypt-purple hover:bg-krypt-purple/25',
              )}
            >
              <PlugZap className="h-4 w-4" />
              {checking ? 'Testing…' : 'Test connection'}
            </button>
          </Row>
        </div>

        {check && (
          <p
            data-testid="ai-check-result"
            className={cls(
              'mt-3 rounded-lg border px-3 py-2 text-xs leading-relaxed',
              check.ok
                ? 'border-krypt-win/30 bg-krypt-win/10 text-krypt-win'
                : 'border-krypt-warn/30 bg-krypt-warn/10 text-krypt-warn',
            )}
          >
            {check.message}
            {check.ok && check.tools === false && ' This model cannot call tools, so Autopilot will not run on it.'}
          </p>
        )}

        {needsKey ? (
          <div className="mt-4">
            <Row
              label={`${label} API key`}
              hint="Checked against the provider before it is saved, then stored encrypted the same way your API key is — never in settings.json, never in a log, never read back to this screen."
            >
              <div className="flex gap-2">
                <input
                  type="password"
                  value={draft}
                  autoComplete="off"
                  spellCheck={false}
                  data-testid="ai-key-input"
                  placeholder={hasKey
                    ? `Saved${masked ? `: ${masked}` : ''}. Type a new one to replace it.`
                    : `Paste your ${label} API key (${AI_PROVIDER_CHOICES.find((p) => p.id === provider)?.keyHint ?? ''})`}
                  onChange={(e) => setDraft(e.target.value)}
                  className="krypt-input min-w-0 flex-1 font-mono"
                />
                <button
                  type="button"
                  disabled={busy || !draft.trim()}
                  onClick={() => void saveKey(draft.trim())}
                  className="krypt-btn-primary shrink-0 text-xs"
                >
                  Verify &amp; save
                </button>
                {hasKey && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void saveKey('')}
                    title={`Remove the saved ${label} key`}
                    className="krypt-btn-default shrink-0 text-xs hover:text-krypt-loss"
                  >
                    <Trash2 className="h-4 w-4" />
                  </button>
                )}
              </div>
            </Row>
            <p className="mt-2 text-xs text-krypt-dim">
              {hasKey
                ? `A ${label} key is saved. It cannot be read back here — replace it or remove it.`
                : `No ${label} key yet. Markets will say so instead of offering an analysis.`}
            </p>
          </div>
        ) : (
          <p className="mt-4 flex items-start gap-2 text-xs leading-relaxed text-krypt-dim">
            <Cpu className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              {label} runs on this machine: no key, no bill, and the market data never
              leaves your computer. {LOCAL_SETUP[provider]}
            </span>
          </p>
        )}

        <div className="mt-4 border-t border-krypt-border pt-4">
          <Switch
            label="Let the model search the web"
            description={!webOk
              ? `${label} has no web search — a local model can only reason about the data `
                + 'it is shown. The analysis will say it did not search.'
              : 'Grounds the reading in current sources instead of a training cutoff, which '
                + 'matters most on event markets. It costs extra per search, adds a few seconds, '
                + `and sends the market question to your provider's search${caps?.webSearchHow ? ` (${caps.webSearchHow})` : ''}. `
                + 'With this off, the panel says plainly that anything recent is outside what the model can see.'}
            checked={(config.aiWebSearch ?? false) && webOk}
            disabled={!webOk}
            onChange={(v) => void update('aiWebSearch', v)}
          />
        </div>

        {caps?.toolsHow && (
          <p className="mt-3 text-xs leading-relaxed text-krypt-dim">
            Autopilot needs tool calling: {caps.toolsHow}.{caps.tokens ? ` Token counts: ${caps.tokens}.` : ''}
          </p>
        )}

        <p className="mt-4 flex items-start gap-2 text-xs leading-relaxed text-krypt-dim">
          <Sparkles className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            An analysis runs only when you press the button on a market, never on a timer and
            never in the background{local ? '' : ', because each one is a billed request on your key'}.
            The model is told not to recommend a trade — it has no way to place one regardless.
            Autopilot is the one thing that calls the model on a schedule, and it has its own
            switch and budgets on the AI Agents page.
          </span>
        </p>
      </Card>
    </Section>
  );
}

function Row({
  label, hint, children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="mb-1 block text-sm font-medium text-white">{label}</label>
      {children}
      {hint && <p className="mt-1 text-xs text-krypt-dim">{hint}</p>}
    </div>
  );
}
