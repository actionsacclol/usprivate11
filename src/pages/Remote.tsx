import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Check, Copy, KeyRound, Loader2, RefreshCw, Send, ShieldAlert, Smartphone,
  Trash2, X,
} from 'lucide-react';
import type { RemoteStatus, TraderConfig } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Card, Empty, Page, Section, Switch } from '../components/common';
import { CommitInput, Note } from '../components/terminal/TerminalBits';
import { cls } from '../utils/format';

type Platform = 'discord' | 'telegram';

export function RemotePage() {
  const { config, refresh } = useApp();
  const toast = useToast();
  const [status, setStatus] = useState<RemoteStatus | null>(null);

  const load = useCallback(async () => {
    try { setStatus(await window.krypt.remote.status()); } catch { /* keep last */ }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(load, 6000);
    return () => window.clearInterval(id);
  }, [load]);

  const update = useCallback(async <K extends keyof TraderConfig>(
    key: K, value: TraderConfig[K],
  ) => {
    await window.krypt.config.update({ [key]: value } as Partial<TraderConfig>);
    await refresh.state();
    // Starting/stopping a transport is the backend's job; ask it to reconcile
    // immediately rather than waiting for its 20s supervisor tick.
    try { setStatus(await window.krypt.remote.sync()); } catch { /* ignore */ }
  }, [refresh]);

  if (!config) {
    return <Page title="Remote control"><div className="text-krypt-muted">Loading…</div></Page>;
  }

  const on = !!config.remoteEnabled;

  return (
    <Page
      title="Remote control"
      subtitle="Leave the app running on this PC and check on it — or react to it — from your phone."
      actions={
        <button
          onClick={async () => {
            const r = await window.krypt.remote.test();
            toast[r.sent ? 'success' : 'error'](r.message);
          }}
          className="flex items-center gap-1.5 rounded-lg border border-krypt-border px-3 py-1.5 text-sm text-krypt-muted hover:text-white"
        >
          <Send className="h-3.5 w-3.5" /> Send test message
        </button>
      }
    >
      {/* ── what this actually does, before any switch ─────────────────── */}
      <Section title="Before you turn this on">
        <Card>
          <div className="space-y-3 text-xs text-krypt-muted">
            <p>
              A chat bot means your <strong className="text-white">positions
              and P&amp;L travel to Discord&rsquo;s or Telegram&rsquo;s
              servers</strong>. That is unavoidable — it is what a chat bot is
              — and it is why both ship switched off. Everything else in this
              app stays on your machine.
            </p>
            <p>
              <strong className="text-white">Only accounts you pair can use
              it.</strong> A message from anyone else is dropped before it is
              read, and logged so you can see it happened. The bot token is not
              the protection: tokens end up in screenshots and pasted logs. The
              paired-id list is.
            </p>
            <p>
              Your token is stored the same way your API key is — encrypted
              at rest, never in <span className="font-mono">settings.json</span>,
              never sent back to this screen.
            </p>
          </div>
          <div className="mt-4 border-t border-krypt-border pt-4">
            <Switch
              label="Enable remote control"
              description="Master switch. Nothing connects until you also enable a platform below and pair an account."
              checked={on}
              onChange={(v) => void update('remoteEnabled', v)}
            />
          </div>
        </Card>
      </Section>

      {on && (
        <>
          <PlatformCard
            platform="telegram"
            title="Telegram"
            status={status}
            enabled={!!config.remoteTelegramEnabled}
            onToggle={(v) => void update('remoteTelegramEnabled', v)}
            onChanged={load}
            setupSteps={[
              'Open Telegram and message @BotFather.',
              'Send /newbot and follow the prompts to name it.',
              'BotFather replies with a token — paste it below.',
              'Then generate a pairing code and send it to YOUR bot as: /pair 123456',
            ]}
            pairHint="Telegram ids are hard to find by hand, so pairing is code-based: generate a code here, send it to your bot, and it learns your chat id."
          />

          <PlatformCard
            platform="discord"
            title="Discord"
            status={status}
            enabled={!!config.remoteDiscordEnabled}
            onToggle={(v) => void update('remoteDiscordEnabled', v)}
            onChanged={load}
            setupSteps={[
              'Go to discord.com/developers/applications and create an application.',
              'Open the Bot tab, add a bot, and copy its token.',
              'On that same tab, turn ON the MESSAGE CONTENT intent — without it the bot receives empty messages and every command silently does nothing.',
              'Invite the bot to a private server, or just DM it.',
              'Pair with a code, or paste your own Discord user id below.',
            ]}
            pairHint="Your Discord user id: enable Settings → Advanced → Developer Mode, then right-click your name and Copy User ID."
            allowManualId
          />

          {/* ── the money switch, kept visually separate ──────────────── */}
          <Section title="Placing orders from chat">
            <Card className={cls(config.remoteTradingEnabled && 'ring-1 ring-krypt-loss/40')}>
              <div className="mb-3 flex items-start gap-2 text-xs text-krypt-warn">
                <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                <p>
                  This is a bigger deal than reading. With it on, anyone holding
                  your unlocked phone can spend your balance. It is a separate
                  switch on purpose and it never turns on with the one above.
                </p>
              </div>
              <Switch
                label="Allow /buy and /sell from chat"
                description="Every order is previewed first and needs a fresh /confirm within 90 seconds. It still obeys your live-trading switch and paper mode."
                checked={!!config.remoteTradingEnabled}
                onChange={(v) => void update('remoteTradingEnabled', v)}
              />
              {config.remoteTradingEnabled && (
                <label className="mt-3 block max-w-xs">
                  <span className="text-[11px] text-krypt-dim">
                    Maximum size for one chat-placed order (USD)
                  </span>
                  {/* Committed on blur/Enter. Per keystroke, clearing the box
                      to retype it persisted "" as 0 — which here means NO cap. */}
                  <CommitInput
                    type="number" min={0}
                    value={config.remoteMaxOrderUsd ?? 100}
                    onCommit={(raw) => {
                      const n = Number(raw);
                      if (Number.isFinite(n) && n >= 0) void update('remoteMaxOrderUsd', n);
                    }}
                    className="mt-0.5 w-full rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-sm text-white focus:outline-none"
                  />
                  <span className="mt-1 block text-[11px] text-krypt-dim">
                    Enforced in the backend, not in the chat. 0 means no cap.
                  </span>
                </label>
              )}
            </Card>
          </Section>

          <AuditLog rows={status?.audit ?? []} />
        </>
      )}
    </Page>
  );
}

// ────────────────────────────────────────────────────────────────────────

function PlatformCard({
  platform, title, status, enabled, onToggle, onChanged,
  setupSteps, pairHint, allowManualId,
}: {
  platform: Platform;
  title: string;
  status: RemoteStatus | null;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  onChanged: () => void;
  setupSteps: string[];
  pairHint: string;
  allowManualId?: boolean;
}) {
  const toast = useToast();
  const st = status?.[platform];
  const [token, setToken] = useState('');
  const [saving, setSaving] = useState(false);
  const [code, setCode] = useState<{ code: string; ttlSec: number } | null>(null);
  const [left, setLeft] = useState(0);
  const [manualId, setManualId] = useState('');
  const timer = useRef<number | null>(null);

  // Count the pairing code down so an expired one cannot sit on screen looking
  // usable — it is refused by the backend either way, but a stale code on the
  // screen just reads as "pairing is broken".
  useEffect(() => {
    if (!code) return;
    // The countdown is kept in the tick's own closure, not read out of a
    // setState updater. An updater must be PURE — React is free to call it
    // twice — and the old one dropped the pairing code from inside it, so a
    // re-invoked updater could clear a code that had just been issued.
    let remaining = code.ttlSec;
    setLeft(remaining);
    timer.current = window.setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) { setLeft(0); setCode(null); return; }
      setLeft(remaining);
    }, 1000);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [code]);

  const saveToken = async () => {
    setSaving(true);
    try {
      const r = await window.krypt.remote.setToken(platform, token.trim());
      toast[r.ok ? 'success' : 'error'](r.message);
      if (r.ok) setToken('');
      onChanged();
    } finally { setSaving(false); }
  };

  return (
    <Section title={title}>
      <Card>
        <div className="flex items-start justify-between gap-4">
          <ol className="list-decimal space-y-1 pl-4 text-xs text-krypt-muted">
            {setupSteps.map((s) => <li key={s}>{s}</li>)}
          </ol>
          <div className="shrink-0 text-right">
            <span className={cls(
              'rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider',
              st?.connected
                ? 'border-krypt-win/40 bg-krypt-win/10 text-krypt-win'
                : 'border-krypt-border text-krypt-dim')}
            >
              {st?.connected ? 'connected' : 'not connected'}
            </span>
            {st?.botUsername && (
              <div className="mt-1 text-[11px] text-krypt-dim">@{st.botUsername}</div>
            )}
          </div>
        </div>

        {st?.lastError && (
          <div className="mt-3"><Note tone="warn">{st.lastError}</Note></div>
        )}

        {/* ── token ─────────────────────────────────────────────────── */}
        <div className="mt-4 border-t border-krypt-border pt-4">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-krypt-muted">
            <KeyRound className="h-3.5 w-3.5" /> Bot token
            {st?.hasToken && (
              <span className="ml-2 font-mono text-krypt-dim">
                stored {st.tokenMasked ?? ''}
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <input
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={st?.hasToken ? 'Paste a new token to replace it' : 'Paste the bot token'}
              className="min-w-0 flex-1 rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-xs text-white placeholder:text-krypt-dim focus:border-krypt-purple/50 focus:outline-none"
            />
            <button
              onClick={saveToken}
              disabled={saving || !token.trim()}
              className="flex items-center gap-1.5 rounded-lg border border-krypt-purple/50 bg-krypt-purple/10 px-3 py-1.5 text-xs text-white disabled:opacity-40"
            >
              {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Check className="h-3 w-3" />}
              Verify &amp; save
            </button>
            {st?.hasToken && (
              <button
                onClick={async () => {
                  await window.krypt.remote.setToken(platform, '');
                  toast.info('Token cleared.');
                  onChanged();
                }}
                title="Remove the stored token"
                className="rounded-lg border border-krypt-border px-2 py-1.5 text-krypt-dim hover:text-krypt-loss"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
          <p className="mt-1 text-[11px] text-krypt-dim">
            Checked against {title} before it is stored, so a typo fails here
            rather than as a bot that never connects.
          </p>
        </div>

        {/* ── pairing ───────────────────────────────────────────────── */}
        <div className="mt-4 border-t border-krypt-border pt-4">
          <div className="mb-2 text-[11px] uppercase tracking-wider text-krypt-muted">
            Paired accounts — the only ones this bot answers
          </div>
          <p className="mb-3 text-[11px] text-krypt-dim">{pairHint}</p>

          {!st?.paired.length ? (
            <div className="mb-3 rounded-lg border border-dashed border-krypt-border p-3 text-center text-[11px] text-krypt-dim">
              Nobody paired. The bot will refuse every command until you pair.
            </div>
          ) : (
            <div className="mb-3 space-y-1">
              {st.paired.map((p) => (
                <div key={p.id} className="flex items-center gap-2 rounded-lg border border-krypt-border px-2.5 py-1.5">
                  <Smartphone className="h-3.5 w-3.5 text-krypt-win" />
                  <span className="font-mono text-xs text-white">{p.id}</span>
                  {p.label && <span className="text-[11px] text-krypt-muted">{p.label}</span>}
                  <span className="ml-auto text-[10px] text-krypt-dim">
                    {new Date(p.pairedAt).toLocaleDateString()}
                  </span>
                  <button
                    onClick={async () => {
                      await window.krypt.remote.unpair(platform, p.id);
                      onChanged();
                    }}
                    title="Remove — this account can no longer command the app"
                    className="text-krypt-dim hover:text-krypt-loss"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={async () => setCode(await window.krypt.remote.pairCode(platform))}
              className="rounded-lg border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-white"
            >
              Generate pairing code
            </button>
            {code && (
              <div className="flex items-center gap-2 rounded-lg border border-krypt-purple/50 bg-krypt-purple/10 px-3 py-1.5">
                <span className="font-mono text-lg tracking-widest text-white">{code.code}</span>
                <button
                  onClick={() => { void navigator.clipboard?.writeText(code.code); }}
                  className="text-krypt-dim hover:text-white"
                >
                  <Copy className="h-3.5 w-3.5" />
                </button>
                <span className="text-[11px] text-krypt-muted">
                  send <span className="font-mono">/pair {code.code}</span> to your bot · {left}s
                </span>
              </div>
            )}
          </div>

          {allowManualId && (
            <div className="mt-3 flex gap-2">
              <input
                value={manualId}
                onChange={(e) => setManualId(e.target.value)}
                placeholder="…or paste your Discord user id"
                className="min-w-0 flex-1 rounded-lg border border-krypt-border bg-krypt-surface px-2 py-1.5 font-mono text-xs text-white placeholder:text-krypt-dim focus:outline-none"
              />
              <button
                onClick={async () => {
                  const r = await window.krypt.remote.addPaired(platform, manualId.trim());
                  if (r.error) toast.error(r.error);
                  else { toast.success('Paired.'); setManualId(''); onChanged(); }
                }}
                disabled={!manualId.trim()}
                className="rounded-lg border border-krypt-border px-3 py-1.5 text-xs text-krypt-muted hover:text-white disabled:opacity-40"
              >
                Add
              </button>
            </div>
          )}
        </div>

        <div className="mt-4 border-t border-krypt-border pt-4">
          <Switch
            label={`Connect the ${title} bot`}
            description={`Starts the connection. Needs a token and at least one paired account to be useful.`}
            checked={enabled}
            onChange={onToggle}
          />
        </div>
      </Card>
    </Section>
  );
}

function AuditLog({ rows }: { rows: RemoteStatus['audit'] }) {
  return (
    <Section
      title="Command log"
      description="Every command, including the ones that were refused."
    >
      <Card>
        {/* A refusal from an unpaired id is the most interesting row here: it
            means somebody found your bot. Hiding it would hide exactly the
            thing worth knowing. */}
        {!rows.length ? (
          <Empty title="Nothing yet"
            description="Commands sent to your bots appear here — accepted, refused, or from a stranger." />
        ) : (
          <div className="max-h-72 space-y-1 overflow-y-auto">
            {rows.map((r, i) => (
              <div key={`${r.at}-${i}`} className="flex items-center gap-2 text-[11px]">
                <span className="w-28 shrink-0 text-krypt-dim">
                  {new Date(r.at).toLocaleTimeString()}
                </span>
                <span className="w-16 shrink-0 text-krypt-muted">{r.platform}</span>
                <span className="w-24 shrink-0 truncate font-mono text-krypt-dim">{r.sender}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-white">{r.command}</span>
                {(r.count ?? 0) > 1 && (
                  <span className="shrink-0 font-mono text-krypt-dim"
                    title={r.firstAt ? `first seen ${new Date(r.firstAt).toLocaleString()}` : undefined}>
                    ×{r.count}
                  </span>
                )}
                <span className={cls('shrink-0 rounded px-1.5 py-0.5 uppercase tracking-wider',
                  r.outcome === 'accepted' ? 'text-krypt-win'
                    : r.outcome === 'unauthorised' ? 'bg-krypt-loss/15 text-krypt-loss'
                      : 'text-krypt-warn')}
                >
                  {r.outcome}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </Section>
  );
}
