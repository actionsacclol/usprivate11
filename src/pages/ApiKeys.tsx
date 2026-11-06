import React, { useEffect, useRef, useState } from 'react';
import {
  Eye, EyeOff, KeyRound, RefreshCcw, UserPlus,
  Save, ShieldAlert, ShieldCheck, Trash2, Wifi, WifiOff,
} from 'lucide-react';
import type { CredentialsState, CredentialsStatusAll } from '@shared/types';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { Card, Page, Section } from '../components/common';
import { cls } from '../utils/format';
import { AccountSetup, SquadCard } from '../components/AccountSetup';
import { apiKeyProblem } from '../utils/apiKey';
import { openSquadOnce } from '../utils/accountSetup';

export function ApiKeysPage() {
  const { backend, refresh } = useApp();
  const [statusAll, setStatusAll] = useState<CredentialsStatusAll | null>(null);

  const reload = async (): Promise<void> => {
    try {
      const all = await window.krypt.credentials.statusAll();
      setStatusAll(all);
    } catch { /* noop */ }
  };

  useEffect(() => {
    void reload();
  }, [backend.authOk]);

  const cred: CredentialsState | undefined = statusAll?.mainnet;
  const hasKey = !!cred?.hasWalletKey;

  // Opens by itself once when there is no key, and stays closed if the person hides
  // it. It stays up after its own save, so its "connected" screen can show, and comes
  // back by itself if the key is later deleted.
  const [guideOpen, setGuideOpen] = useState(false);
  const autoOpened = useRef(false);
  useEffect(() => {
    if (!statusAll) return;
    if (hasKey) { autoOpened.current = false; return; }
    if (!autoOpened.current) {
      autoOpened.current = true;
      setGuideOpen(true);
    }
  }, [statusAll, hasKey]);

  return (
    <Page
      title="API Key"
      subtitle="Connect your Polymarket US account with an API key. The key is stored locally (encrypted with your Windows account) under %APPDATA%/Krypt PolyBot US/credentials and is only used to sign requests to Polymarket US."
      actions={
        <>
          {!guideOpen && (
            <button onClick={() => setGuideOpen(true)} className="krypt-btn-default" data-testid="api-key-show-guide">
              <UserPlus className="h-4 w-4" /> Setup guide
            </button>
          )}
          <button onClick={() => void reload()} className="krypt-btn-default" title="Re-read credential status from disk">
            <RefreshCcw className="h-4 w-4" /> Refresh
          </button>
        </>
      }
    >
      {guideOpen && (
        <div data-testid="api-key-guide">
          <Card className="mb-4">
            <AccountSetup
              leaveLabel="Hide this guide"
              onDone={() => { setGuideOpen(false); void reload(); }}
            />
          </Card>
        </div>
      )}
      {!guideOpen && hasKey && <SquadCard />}

      <Section title="Connection">
        <Card>
          <div className="flex flex-col items-start gap-4 md:flex-row md:items-center">
            <div className={cls(
              'grid h-10 w-10 shrink-0 place-items-center rounded-lg',
              backend.authOk ? 'bg-krypt-win/10 text-krypt-win' : 'bg-krypt-loss/10 text-krypt-loss',
            )}>
              {backend.authOk ? <Wifi className="h-5 w-5" /> : <WifiOff className="h-5 w-5" />}
            </div>
            <div className="flex-1">
              <div className="text-sm text-white">
                {backend.authOk ? 'Connected · Polymarket US' : 'Not connected'}
              </div>
              <div className="mt-0.5 text-xs text-krypt-muted">
                {cred?.keyId || cred?.address
                  ? <>Key ID <span className="font-mono text-white">{cred.keyId || cred.address}</span></>
                  : 'Add an API key to read your balance and place orders. Paper trading does not need one.'}
              </div>
            </div>
          </div>
        </Card>
      </Section>

      {!guideOpen && (
        <KeySlot
          status={cred}
          onSaved={async () => { await reload(); await refresh.credentials(); await refresh.account(); await refresh.backend(); }}
        />
      )}

      <Section title="Security notes">
        <Card>
          <ul className="list-disc space-y-1.5 pl-5 text-xs text-krypt-muted">
            <li>Your Secret Key is written under <span className="font-mono text-white">%APPDATA%/Krypt PolyBot US/credentials</span>, encrypted at rest with the Windows user keystore (DPAPI).</li>
            <li>The Python backend signs each request to Polymarket US locally. The Secret Key itself is never sent anywhere.</li>
            <li>Anyone with this key can trade your account. If you think it leaked, delete it on the Polymarket US developer page and create a new one.</li>
            <li>Click &quot;Delete&quot; before uninstalling if you want the key gone from this machine.</li>
          </ul>
        </Card>
      </Section>
    </Page>
  );
}

interface SlotProps {
  status?: CredentialsState;
  onSaved: () => Promise<void>;
}

function KeySlot({ status, onSaved }: SlotProps) {
  const toast = useToast();
  const [keyId, setKeyId] = useState('');
  const [secretKey, setSecretKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [busy, setBusy] = useState(false);

  const has = !!status?.hasWalletKey;

  // A key can authenticate fine yet still be unable to trade (no balance, say).
  // Surface that verdict here so users find out now, not when orders never fill.
  const reportTest = (data?: { balanceUsd?: number; ready?: boolean; issues?: string[] }): void => {
    const balStr = `$${(data?.balanceUsd ?? 0).toFixed(2)}`;
    const issues = data?.issues ?? [];
    if (data?.ready === false || issues.length) {
      const detail = issues[0]
        || ((data?.balanceUsd ?? 0) <= 0 ? 'the account has no balance' : 'not ready to trade');
      toast.warn(`Connected · balance ${balStr} — but not ready to trade yet: ${detail}`);
    } else {
      toast.success(`Connected · balance ${balStr} · ready to trade`);
    }
  };

  const save = async (): Promise<void> => {
    const id = keyId.trim();
    const secret = secretKey.trim();
    if (!id && !secret && has) {
      toast.error('Nothing to update. Paste a new Key ID and Secret Key to replace this key, or click Delete to remove it.');
      return;
    }
    const problem = apiKeyProblem(id, secret);
    if (problem) {
      toast.error(problem);
      return;
    }
    setBusy(true);
    try {
      const r = await window.krypt.credentials.save({ keyId: id, secretKey: secret, env: 'mainnet' });
      if (!r.ok) { toast.error(r.message || 'Save failed'); return; }
      toast.success('API key saved. Verifying…');
      const t = await window.krypt.credentials.test('mainnet');
      if (t.ok) {
        reportTest(t.data);
        if (openSquadOnce()) toast.info('Opened the Krypt squad page in your browser. Click join there.');
      } else {
        toast.error(t.message || 'Could not connect to Polymarket US');
      }
      setKeyId('');
      setSecretKey('');
      await onSaved();
    } finally {
      setBusy(false);
    }
  };

  const test = async (): Promise<void> => {
    setBusy(true);
    try {
      const r = await window.krypt.credentials.test('mainnet');
      if (r.ok) reportTest(r.data);
      else toast.error(r.message || 'Test failed');
    } finally {
      setBusy(false);
    }
  };

  const clear = async (): Promise<void> => {
    if (!window.confirm('Delete the saved API key from this machine? It stays active on Polymarket US until you delete it there too.')) return;
    setBusy(true);
    try {
      const r = await window.krypt.credentials.clear('mainnet');
      if (r.ok) { toast.success('API key removed'); await onSaved(); }
      else toast.error(r.message || 'Failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="API key">
      <Card>
        <div className="mb-3 flex items-center gap-2">
          <div className="text-sm font-semibold text-white">Polymarket US API key</div>
          <span className={cls(
            'ml-auto rounded-md px-2 py-0.5 text-[10px] uppercase tracking-wider',
            has
              ? 'border border-krypt-win/30 bg-krypt-win/10 text-krypt-win'
              : 'border border-krypt-border bg-krypt-surface2 text-krypt-muted',
          )}>
            {has ? 'connected' : 'empty'}
          </span>
        </div>

        {has && (
          <div className="mb-3 flex items-center gap-2 rounded-lg border border-krypt-border bg-krypt-surface2 px-3 py-2 text-xs text-krypt-muted">
            <KeyRound className="h-3.5 w-3.5 text-krypt-win" />
            <div className="flex-1">
              <div>
                Key ID <span className="font-mono text-white">{status?.addressPreview || status?.keyId || '—'}</span>
              </div>
              <div className="text-[10px] text-krypt-dim">
                Paste a new key below to replace it, or click Delete to remove it.
              </div>
            </div>
          </div>
        )}

        {has && status?.keyStoredUnencrypted && (
          <div className="mb-3 flex items-start gap-2 rounded-lg border border-krypt-loss/50 bg-krypt-loss/10 px-3 py-2 text-xs text-krypt-loss">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <div>
              <div className="font-semibold">Secret Key stored unencrypted</div>
              <div className="mt-0.5 text-krypt-muted">
                No OS keystore (Windows DPAPI / macOS Keychain / Secret Service) was
                available, so your Secret Key is saved in plaintext on this machine.
                Anyone with access to this device, or to a backup or cloud sync of it,
                could trade your account. Enable a system keychain and re-save the key.
              </div>
            </div>
          </div>
        )}

        <label className="krypt-label mt-1">Key ID</label>
        <input
          type="text"
          className="krypt-input font-mono"
          placeholder={has ? 'paste a new Key ID to replace the saved key' : 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'}
          value={keyId}
          onChange={(e) => setKeyId(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          data-testid="api-key-id"
        />

        <label className="krypt-label mt-3 flex items-center justify-between">
          Secret Key
          <button
            type="button"
            onClick={() => setShowKey((v) => !v)}
            className="text-xs text-krypt-muted hover:text-white"
          >
            {showKey ? <><EyeOff className="mr-1 inline h-3 w-3" />hide</> : <><Eye className="mr-1 inline h-3 w-3" />show</>}
          </button>
        </label>
        <input
          type={showKey ? 'text' : 'password'}
          className="krypt-input font-mono"
          placeholder={has ? 'paste the new Secret Key' : 'base64, shown once by Polymarket US'}
          value={secretKey}
          onChange={(e) => setSecretKey(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          data-testid="api-key-secret"
        />
        <p className="krypt-help" data-testid="api-key-eligibility">
          Polymarket US accounts are for people who pass its identity check. Trading rules still apply to orders placed by software.
        </p>

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <button onClick={save} disabled={busy} className="krypt-btn-primary">
            <Save className="h-4 w-4" /> Save &amp; connect
          </button>
          <button onClick={test} disabled={busy || !has} className="krypt-btn-default">
            <ShieldCheck className="h-4 w-4" /> Test
          </button>
          {has && (
            <button onClick={clear} disabled={busy} className="krypt-btn-danger ml-auto">
              <Trash2 className="h-4 w-4" /> Delete
            </button>
          )}
        </div>
      </Card>
    </Section>
  );
}
