import { useEffect, useState, type ReactNode } from 'react';
import {
  ArrowLeft, ArrowRight, CheckCircle2, Copy, ExternalLink, KeyRound, Loader2, Smartphone,
  UserPlus, Users, Wallet,
} from 'lucide-react';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { apiKeyProblem, normalizePastedKey } from '../utils/apiKey';
import {
  loadProgress, openSquadOnce, saveProgress, stepsFor, type SetupPath, type SetupStep,
} from '../utils/accountSetup';
import {
  POLYMARKET_DEVELOPER_URL, POLYMARKET_INVITE_CODE, POLYMARKET_SIGNUP_URL,
  POLYMARKET_SQUAD_URL, POLYMARKET_SUPPORT_EMAIL,
} from '../utils/links';
import { Badge, ChoiceCard, StepDots } from './SetupSteps';

/**
 * The guided Polymarket US setup: sign up, verify, fund, create a key, paste it. One
 * action per screen, used by onboarding and the API Key page. When the key connects,
 * the squad page opens in the browser for a one-click join.
 *
 * Links open in the person's own browser, not a window inside this app. Google
 * commonly refuses sign-in in embedded windows, and identity details should never
 * be typed into a window this app controls.
 *
 * Nothing here writes to the Polymarket US account or leaves Paper. The only saved
 * thing is the API key, and only when the person presses Save on the last screen.
 */

function openLink(url: string): void {
  void window.krypt.app.openExternal(url);
}

/** A way out when the browser doesn't open, or opens on the wrong profile. */
function CopyLink({ url, label = 'Copy link' }: { url: string; label?: string }) {
  const toast = useToast();
  return (
    <button
      type="button"
      className="krypt-btn-ghost text-xs"
      onClick={() => {
        void navigator.clipboard.writeText(url)
          .then(() => toast.success('Copied'))
          .catch(() => toast.error(`Could not copy. The link is: ${url}`));
      }}
    >
      <Copy className="h-3 w-3" /> {label}
    </button>
  );
}

function OpenLink({ url, label, testId }: { url: string; label: string; testId: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <button type="button" className="krypt-btn-primary" onClick={() => openLink(url)} data-testid={testId}>
        {label} <ExternalLink className="h-4 w-4" />
      </button>
      <CopyLink url={url} />
    </div>
  );
}

function Do({ items }: { items: ReactNode[] }) {
  return (
    <ol className="space-y-2">
      {items.map((item, i) => (
        <li key={i} className="flex items-start gap-2.5 text-sm text-white/90">
          <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full bg-krypt-purple/25 text-[11px] font-semibold text-white">
            {i + 1}
          </span>
          <span className="min-w-0">{item}</span>
        </li>
      ))}
    </ol>
  );
}

function Note({ tone = 'muted', children }: { tone?: 'muted' | 'warn'; children: ReactNode }) {
  return (
    <div className={tone === 'warn'
      ? 'rounded-lg border border-krypt-warn/40 bg-krypt-warn/5 p-3 text-xs leading-relaxed text-krypt-warn'
      : 'rounded-lg border border-krypt-border bg-krypt-surface2 p-3 text-xs leading-relaxed text-krypt-muted'}
    >
      {children}
    </div>
  );
}

const SAME_LOGIN = 'Use the same sign-in as your account: Google, Apple or email.';

const STEP_TITLES: Record<SetupStep, { title: string; icon: typeof UserPlus }> = {
  signup: { title: 'Create your Polymarket US account', icon: UserPlus },
  verify: { title: 'Verify your identity on your phone', icon: Smartphone },
  fund: { title: 'Add money when you want real trades', icon: Wallet },
  key: { title: 'Create your API key', icon: KeyRound },
  paste: { title: 'Paste your API key here', icon: KeyRound },
};

export function SquadCard() {
  return (
    <div className="mb-4 rounded-xl border border-krypt-purple/40 bg-krypt-purple/5 p-4" data-testid="squad-card">
      <div className="flex items-center gap-2 text-sm font-medium text-white">
        <Users className="h-4 w-4 text-krypt-purple" /> Join the Krypt squad
      </div>
      <p className="mb-3 mt-1 text-xs text-krypt-muted">
        Opens in your browser. Sign in with your Polymarket US account, then join Krypt.
      </p>
      <OpenLink url={POLYMARKET_SQUAD_URL} label="Open the squad page" testId="squad-card-open" />
    </div>
  );
}

function StepBody({ step }: { step: Exclude<SetupStep, 'paste'> }) {
  if (step === 'signup') {
    return (
      <div className="space-y-3">
        <Do items={[
          <>Click <b className="text-white">Open the sign-up page</b>. It opens in your web browser.</>,
          <>Click <b className="text-white">Continue with Google</b> or <b className="text-white">Continue with Apple</b>.</>,
          <>Remember which one you picked. You&apos;ll use the same one on your phone and for your API key.</>,
          <>If the page sends you to the phone app, that&apos;s fine. Come back here and click <b className="text-white">Done, next</b>.</>,
        ]} />
        <OpenLink url={POLYMARKET_SIGNUP_URL} label="Open the sign-up page" testId="setup-open-signup" />
        <Note tone="warn">
          Do this before anything else. Polymarket US only accepts an invite code during sign-up, so it
          can&apos;t be added later. If you&apos;re asked for a code, it&apos;s{' '}
          <b data-testid="setup-invite-code">{POLYMARKET_INVITE_CODE}</b>.
        </Note>
        <Note>
          The page says &ldquo;Join yuhgo&rdquo;. That&apos;s Krypt&apos;s account, and this is Krypt&apos;s
          invite link, so Krypt benefits when you use it. The page offers $25 to you and $25 to Krypt.
          Polymarket US&apos;s FAQ says the bonus credit comes after you sign up with the code, finish
          onboarding and deposit at least $10, and it sets the terms. Any Polymarket US account works
          with this app.
        </Note>
      </div>
    );
  }
  if (step === 'verify') {
    return (
      <div className="space-y-3">
        <Do items={[
          <>On your phone, open your app store and install <b className="text-white">Polymarket US</b>.</>,
          <>Open it and sign in. {SAME_LOGIN}</>,
          <>Follow its identity check. It asks for your date of birth, address and Social Security number.</>,
          <>Wait until the app says you&apos;re approved.</>,
        ]} />
        <Note tone="warn">
          Those details go to Polymarket US, in its own app. This app never asks for them.
        </Note>
        <Note>
          Not approved yet? Choose <b className="text-white">Finish later</b>; this guide remembers your
          place. If you&apos;re turned down, Polymarket US decides who qualifies and{' '}
          <span className="text-white">{POLYMARKET_SUPPORT_EMAIL}</span> can help. Paper trading works
          either way.
        </Note>
      </div>
    );
  }
  if (step === 'fund') {
    return (
      <div className="space-y-3">
        <Do items={[
          <>In the Polymarket US phone app, tap <b className="text-white">Portfolio</b> at the bottom, then <b className="text-white">Deposit</b>.</>,
          <>Pick how to pay and follow its steps. The account you pay from must be in your own name.</>,
          <>Start small. Only add money you can afford to lose.</>,
        ]} />
        <Note>
          Paper trading needs no money, so you can skip this and add funds later. Real orders can&apos;t
          go through until there&apos;s a balance.
        </Note>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <Do items={[
        <>Click <b className="text-white">Open the developer page</b>. It opens in your web browser.</>,
        <>Sign in. {SAME_LOGIN}</>,
        <>Create a new key. You&apos;ll see a <b className="text-white">Key ID</b> and a <b className="text-white">Secret Key</b>.</>,
        <>Leave that page open and come back here.</>,
      ]} />
      <OpenLink url={POLYMARKET_DEVELOPER_URL} label="Open the developer page" testId="setup-open-developer" />
      <Note tone="warn">
        Polymarket US shows the Secret Key only once. Anyone who has it can trade your account, so
        never send it to anyone. If you lose it, delete that key on the developer page and make a new one.
      </Note>
      <Note>Can&apos;t create a key? Your identity check probably isn&apos;t finished. Finish it in the Polymarket US phone app.</Note>
    </div>
  );
}

interface Connected {
  balanceUsd: number;
  issue: string | null;
  squadOpened: boolean;
}

function PasteStep({ onConnected }: { onConnected: (c: Connected) => void }) {
  const { refresh } = useApp();
  const [keyId, setKeyId] = useState('');
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const save = async (): Promise<void> => {
    const { keyId: id, secretKey } = normalizePastedKey(keyId, secret);
    const bad = apiKeyProblem(id, secretKey);
    if (bad) { setProblem(bad); return; }
    setProblem(null);
    setBusy(true);
    try {
      const saved = await window.krypt.credentials.save({ keyId: id, secretKey, env: 'mainnet' });
      if (!saved.ok) { setProblem(saved.message || 'The key could not be saved.'); return; }
      const t = await window.krypt.credentials.test('mainnet');
      if (!t.ok) {
        setProblem(t.message || 'Polymarket US did not accept this key. Check that you copied both parts.');
        return;
      }
      const data = t.data as { balanceUsd?: number; issues?: string[] } | undefined;
      setKeyId('');
      setSecret('');
      await Promise.all([refresh.credentials(), refresh.account(), refresh.backend()]);
      onConnected({
        balanceUsd: data?.balanceUsd ?? 0,
        issue: data?.issues?.[0] ?? null,
        squadOpened: openSquadOnce(),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <Do items={[
        <>Copy the <b className="text-white">Key ID</b> from the Polymarket US page and paste it in the first box.</>,
        <>Copy the <b className="text-white">Secret Key</b> and paste it in the second box.</>,
        <>Click <b className="text-white">Save and test</b>.</>,
      ]} />
      <div>
        <label className="krypt-label">Key ID</label>
        <input
          type="text" className="krypt-input font-mono" value={keyId} autoComplete="off" spellCheck={false}
          placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
          onChange={(e) => setKeyId(e.target.value)} data-testid="api-key-id"
        />
      </div>
      <div>
        <label className="krypt-label">Secret Key</label>
        <input
          type="password" className="krypt-input font-mono" value={secret} autoComplete="off" spellCheck={false}
          placeholder="The long one, shown only once"
          onChange={(e) => setSecret(e.target.value)} data-testid="api-key-secret"
        />
      </div>
      {problem && <Note tone="warn"><span data-testid="setup-key-problem">{problem}</span></Note>}
      <button type="button" className="krypt-btn-primary" disabled={busy} onClick={() => void save()} data-testid="setup-save-key">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />} Save and test
      </button>
      <Note>
        Your key stays on this computer, encrypted, and is only used to sign requests to Polymarket US.
        Saving it does not turn on real trading. Once it connects, the Krypt squad page opens in your
        browser so you can join the squad and its chat. That link credits Krypt as your referrer.
      </Note>
    </div>
  );
}

/**
 * @param onDone  The person is finished or chose to leave for now.
 * @param onBackOut  Back from the first screen. Without it, no Back button shows there.
 * @param leaveLabel  What the leave button says on the first screen.
 */
export function AccountSetup({ onDone, onBackOut, leaveLabel = 'Continue on paper' }: {
  onDone: () => void;
  onBackOut?: () => void;
  leaveLabel?: string;
}) {
  const toast = useToast();
  const [saved] = useState(loadProgress);
  const [path, setPath] = useState<SetupPath | null>(saved?.path ?? null);
  const [step, setStep] = useState<SetupStep | null>(saved?.step ?? null);
  const [connected, setConnected] = useState<Connected | null>(null);
  const [hadKey, setHadKey] = useState(false);

  // A key already saved: nothing to set up but the squad, and any saved place is stale.
  useEffect(() => {
    let live = true;
    void window.krypt.credentials.statusAll()
      .then((all) => {
        if (!live || !all?.mainnet?.hasWalletKey) return;
        setHadKey(true);
        setPath(null);
        setStep(null);
        saveProgress(null);
      })
      .catch(() => { /* unknown: offer the guide */ });
    return () => { live = false; };
  }, []);

  const go = (p: SetupPath, s: SetupStep): void => {
    setPath(p);
    setStep(s);
    saveProgress({ path: p, step: s });
  };
  const toChooser = (): void => {
    setPath(null);
    setStep(null);
    saveProgress(null);
  };

  if (connected) {
    return (
      <div className="space-y-3" data-testid="setup-done">
        <div className="flex items-center gap-2 text-base font-semibold text-white">
          <CheckCircle2 className="h-5 w-5 text-krypt-win" /> Your account is connected
        </div>
        <p className="text-sm text-white/90">
          Polymarket US accepted your key. Balance: <b data-testid="setup-balance">${connected.balanceUsd.toFixed(2)}</b>
        </p>
        {connected.issue && <Note tone="warn">{connected.issue}</Note>}
        <div className="rounded-xl border border-krypt-purple/40 bg-krypt-purple/5 p-3" data-testid="setup-squad">
          <div className="flex items-center gap-2 text-sm font-medium text-white">
            <Users className="h-4 w-4 text-krypt-purple" /> One more click: join the Krypt squad
          </div>
          <p className="mb-2 mt-1 text-xs text-krypt-muted">
            {connected.squadOpened
              ? 'We opened the squad page in your browser. '
              : 'Open the squad page in your browser. '}
            Click join there to get into the squad and its chat. If it asks you to sign in: {SAME_LOGIN}
          </p>
          <OpenLink
            url={POLYMARKET_SQUAD_URL}
            label={connected.squadOpened ? 'Open it again' : 'Open the squad page'}
            testId="setup-open-squad"
          />
        </div>
        <Note>
          You&apos;re still on <b className="text-white">Paper</b>, so nothing can send a real order. Going
          Live is its own step: Settings, then Paper | Live.
        </Note>
        <button type="button" className="krypt-btn-primary" onClick={onDone} data-testid="setup-finish">
          Finish <ArrowRight className="h-4 w-4" />
        </button>
      </div>
    );
  }

  if (!path || !step) {
    return (
      <div className="space-y-3" data-testid="setup-chooser">
        <h3 className="text-base font-semibold text-white">
          {hadKey ? 'Your API key is saved' : 'Do you have a Polymarket US account?'}
        </h3>
        {hadKey ? <SquadCard /> : (
          <>
            <p className="text-xs text-krypt-muted">
              You only need one for real trades. Paper trading works without an account. New here?
              Start with the first option: Krypt&apos;s invite only applies when you sign up.
            </p>
            <ChoiceCard
              testId="setup-new" selected={false}
              onClick={() => go('new', 'signup')}
              title={<><UserPlus className="mr-1 inline h-4 w-4" />No, help me make one</>}
              badge={<Badge asWritten>Free 25 dollars</Badge>}
            >
              We&apos;ll take you through each step, one screen at a time.
            </ChoiceCard>
            <ChoiceCard
              testId="setup-existing" selected={false}
              onClick={() => go('existing', 'key')}
              title={<><Users className="mr-1 inline h-4 w-4" />Yes, I have an account</>}
            >
              Sign in on the developer page and create an API key. It takes a minute. The invite
              doesn&apos;t apply to an account you already have.
            </ChoiceCard>
            <button type="button" className="krypt-btn-ghost text-xs" onClick={() => go('existing', 'paste')} data-testid="setup-have-key">
              I already have my API key
            </button>
          </>
        )}
        <div className="flex items-center justify-between gap-2 pt-1">
          {onBackOut
            ? <button type="button" className="krypt-btn-ghost" onClick={onBackOut}>Back</button>
            : <span />}
          <button
            type="button" className="krypt-btn-default" data-testid="setup-leave"
            onClick={onDone}
          >
            {hadKey ? 'Continue' : leaveLabel} <ArrowRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    );
  }

  const steps = stepsFor(path);
  const at = steps.indexOf(step);
  const { title, icon: Icon } = STEP_TITLES[step];
  const prev = at > 0 ? steps[at - 1] : null;
  const following = at < steps.length - 1 ? steps[at + 1] : null;

  return (
    <div className="space-y-4" data-testid={`setup-step-${step}`}>
      <div>
        <div className="mb-2 flex items-center gap-3">
          <StepDots count={steps.length} at={at} />
          <span className="text-[11px] text-krypt-muted">Step {at + 1} of {steps.length}</span>
        </div>
        <h3 className="flex items-center gap-2 text-base font-semibold text-white">
          <Icon className="h-4 w-4 text-krypt-purple" /> {title}
        </h3>
      </div>

      {step === 'paste'
        ? <PasteStep onConnected={(c) => { saveProgress(null); setConnected(c); }} />
        : <StepBody step={step} />}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-krypt-border pt-3">
        <button type="button" className="krypt-btn-ghost" onClick={() => (prev ? go(path, prev) : toChooser())} data-testid="setup-back">
          <ArrowLeft className="h-4 w-4" /> Back
        </button>
        <div className="flex items-center gap-2">
          <button
            type="button" className="krypt-btn-ghost" data-testid="setup-later"
            onClick={() => { toast.info('Saved. Pick up where you left off on the API Key page.'); onDone(); }}
          >
            Finish later
          </button>
          {following && (
            <button type="button" className="krypt-btn-primary" onClick={() => go(path, following)} data-testid="setup-next">
              {step === 'fund' ? 'Done or skip, next' : 'Done, next'} <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
