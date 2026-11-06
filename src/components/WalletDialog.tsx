import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ArrowDownToLine, ArrowUpFromLine, KeyRound, RefreshCw, RotateCcw, Settings, X } from 'lucide-react';
import type { PageId } from '../App';
import { DialogShell, NumberInput } from './common';
import { usePaperReset } from './AccountModePanel';
import { useApp } from '../state/AppStateProvider';
import { balanceView, paperView, realBalanceOf } from '../utils/balance';
import { cls, fmtUsd } from '../utils/format';
import { setHideBalance, useHideBalance } from '../utils/hideBalance';
import { isLive } from '../utils/account';

/** The account snapshot is pushed every 15s, so a shorter interval would do nothing. */
const CHECK_EVERY: { seconds: number; label: string }[] = [
  { seconds: 15, label: '15 seconds' },
  { seconds: 30, label: '30 seconds' },
  { seconds: 60, label: '1 minute' },
  { seconds: 300, label: '5 minutes' },
];

const usd = (v: number | null): string => (v === null ? '—' : fmtUsd(v));

function Row({ label, value, testId, tint }: { label: string; value: string; testId?: string; tint?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1 text-sm">
      <span className="text-krypt-muted">{label}</span>
      <span data-testid={testId} className={cls('font-mono tabular-nums', tint ?? 'text-white')}>{value}</span>
    </div>
  );
}

function Steps({ title, steps, children }: { title: string; steps: ReactNode[]; children?: ReactNode }) {
  return (
    <div className="mt-3 rounded-xl border border-sky-400/30 bg-sky-500/10 p-3 text-sm text-white/90">
      <div className="font-semibold text-white">{title}</div>
      <ol className="mt-2 list-decimal space-y-1 pl-5">
        {steps.map((s, i) => <li key={i}>{s}</li>)}
      </ol>
      {children}
    </div>
  );
}

function ago(readAt: number, now: number): string {
  const s = Math.max(0, Math.round(now / 1000 - readAt));
  if (s < 5) return 'Updated just now';
  if (s < 60) return `Updated ${s} seconds ago`;
  const m = Math.floor(s / 60);
  return `Updated ${m} minute${m === 1 ? '' : 's'} ago`;
}

/**
 * The wallet, opened from the top bar's balance and the sidebar card. Polymarket US
 * has no withdrawal or deposit API for a retail key (only its app moves money), so
 * those two buttons show the steps instead of doing it.
 */
export function WalletDialog({ onClose, onNav }: { onClose: () => void; onNav?: (p: PageId) => void }) {
  const { config, account, paper, backend, credentials, refresh } = useApp();
  const live = isLive(config);
  const hasKey = !!credentials?.hasWalletKey;
  const hidden = useHideBalance();
  const [reading, setReading] = useState(false);
  const [steps, setSteps] = useState<'deposit' | 'withdraw' | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const reset = usePaperReset();

  const read = useCallback(async (): Promise<void> => {
    setReading(true);
    try {
      await refresh.wallet();
    } finally {
      setReading(false);
      setNow(Date.now());
    }
  }, [refresh]);

  // Fresh numbers on open: one signed read, and the reason someone opened it. Once:
  // `refresh` changes identity whenever app state does.
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current) return;
    opened.current = true;
    void read();
  }, [read]);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const view = balanceView(account, {
    config, authOk: !!backend.authOk, hasKeys: credentials ? hasKey : null, engineRunning: backend.status === 'running',
  });
  const book = paperView(paper);
  const realBalance = realBalanceOf(account, paper);
  const wallet = account?.wallet;
  const readFailed = !reading && hasKey && !account?.backendDown && wallet?.readOk === false;
  const go = (p: PageId) => (): void => { onClose(); onNav?.(p); };
  const pnlTint = (v: number | null): string | undefined =>
    (v === null ? 'text-krypt-dim' : v >= 0 ? 'text-krypt-win' : 'text-krypt-loss');
  const pending = wallet?.pendingWithdrawalCount ?? 0;

  return (
    <DialogShell onClose={onClose} maxWidth="max-w-lg">
      <div className="max-h-[80vh] overflow-y-auto pr-1" data-testid="wallet-panel" data-mode={live ? 'live' : 'paper'}>
        <div className="flex items-center gap-2">
          <h3 className="text-base font-semibold text-white">Your wallet</h3>
          <span className={cls(
            'rounded-full border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
            live ? 'border-krypt-loss/50 bg-krypt-loss/15 text-krypt-loss' : 'border-krypt-warn/50 bg-krypt-warn/15 text-krypt-warn',
          )}>
            {live ? 'live' : 'paper'}
          </span>
          <button type="button" onClick={onClose} aria-label="Close your wallet" className="ml-auto grid h-7 w-7 place-items-center rounded-lg text-white/70 hover:bg-white/[0.1] hover:text-white">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="mt-3 flex items-end justify-between gap-3">
          <div>
            <div className="text-xs text-krypt-muted">{live ? 'Real money on Polymarket US' : 'Pretend money. Not real.'}</div>
            <div data-testid="wallet-total" className="font-mono text-3xl tabular-nums text-white">
              {usd(live ? view.totalUsd : book.totalUsd)}
            </div>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <button type="button" onClick={() => void read()} disabled={reading} data-testid="wallet-refresh" className="krypt-btn-default">
              <RefreshCw className={cls('h-4 w-4', reading && 'animate-spin')} /> {reading ? 'Checking…' : 'Refresh'}
            </button>
            {live && wallet?.readAt && !readFailed && (
              <span className="text-[11px] text-krypt-dim" data-testid="wallet-updated">{ago(wallet.readAt, now)}</span>
            )}
          </div>
        </div>

        {live && view.why && <p className="mt-2 text-xs text-krypt-warn" data-testid="wallet-unknown">{view.why}</p>}
        {readFailed && (
          <p className="mt-2 text-xs text-krypt-warn" data-testid="wallet-read-failed">
            Couldn&apos;t reach Polymarket US{wallet?.readAt ? '. These are the last numbers it gave us.' : ', so the balance is not known yet.'}
          </p>
        )}

        <div className="mt-3 divide-y divide-white/[0.06] rounded-xl border border-white/[0.08] px-3 py-1">
          {live ? (
            <>
              <Row label="Ready to trade" value={usd(view.cashUsd)} testId="wallet-cash" />
              <Row label="In open positions" value={usd(view.portfolioUsd)} />
              {(wallet?.openOrdersUsd ?? 0) > 0 && view.known && <Row label="Held by open orders" value={usd(wallet?.openOrdersUsd ?? null)} testId="wallet-open-orders" />}
              {pending > 0 && (
                <Row
                  label={`Withdrawal${pending === 1 ? '' : 's'} on the way`}
                  value={wallet?.pendingWithdrawalsUsd == null ? String(pending) : usd(wallet.pendingWithdrawalsUsd)}
                  testId="wallet-pending-withdrawals"
                />
              )}
              <Row label="Up or down today" value={view.todayPnlUsd === null ? '—' : fmtUsd(view.todayPnlUsd, { sign: true })} tint={pnlTint(view.todayPnlUsd)} />
              <Row label="Up or down all time" value={view.alltimePnlUsd === null ? '—' : fmtUsd(view.alltimePnlUsd, { sign: true })} tint={pnlTint(view.alltimePnlUsd)} />
            </>
          ) : (
            <>
              <Row label="Ready to trade" value={usd(book.cashUsd)} testId="wallet-cash" />
              <Row label="In open positions" value={usd(book.portfolioUsd)} />
              <Row label="Up or down" value={book.pnlUsd === null ? '—' : fmtUsd(book.pnlUsd, { sign: true })} tint={pnlTint(book.pnlUsd)} />
              {realBalance !== null && <Row label="Your real account" value={usd(realBalance)} testId="wallet-real-balance" />}
            </>
          )}
        </div>

        <div className="mt-4 text-xs font-semibold uppercase tracking-wider text-krypt-muted">Real money in or out</div>
        <div className="mt-2 grid grid-cols-2 gap-2">
          <button type="button" className="krypt-btn-default justify-center" data-testid="wallet-deposit" aria-expanded={steps === 'deposit'} onClick={() => setSteps(steps === 'deposit' ? null : 'deposit')}>
            <ArrowDownToLine className="h-4 w-4" /> Deposit
          </button>
          <button type="button" className="krypt-btn-default justify-center" data-testid="wallet-withdraw" aria-expanded={steps === 'withdraw'} onClick={() => setSteps(steps === 'withdraw' ? null : 'withdraw')}>
            <ArrowUpFromLine className="h-4 w-4" /> Withdraw
          </button>
        </div>
        {steps === 'deposit' && (
          <Steps title="Deposit in the Polymarket US phone app" steps={['Open the Polymarket US phone app.', <>Tap <b>Portfolio</b> at the bottom.</>, <>Tap <b>Deposit</b>.</>]}>
            <p className="mt-2 text-xs text-white/70">Then come back here and press Refresh.</p>
          </Steps>
        )}
        {steps === 'withdraw' && (
          <Steps title="Withdraw in the Polymarket US phone app" steps={['Open the Polymarket US phone app.', <>Tap <b>Portfolio</b> at the bottom.</>, <>Tap <b>Withdraw</b>, then choose where to send it.</>]}>
            <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-white/70" data-testid="wallet-withdraw-rules">
              <li>This app can&apos;t withdraw for you. Polymarket US only allows it in its own app, so your API key can&apos;t move money out of your account.</li>
              <li>Only cash can come out. Money in a position has to be sold or settle first.</li>
              <li>It goes back the same way you paid in. A recent deposit has to clear first (3 to 4 business days for a card or bank).</li>
              <li>Bonus credit can only be used to trade. It can come out once that trade settles or is sold.</li>
            </ul>
          </Steps>
        )}
        {!live && <p className="mt-2 text-xs text-krypt-dim">The pretend money above can&apos;t be withdrawn. These are for your real Polymarket US account.</p>}

        <div className="mt-4 text-xs font-semibold uppercase tracking-wider text-krypt-muted">Settings</div>
        <div className="mt-2 space-y-3 text-sm">
          <label className="flex items-center justify-between gap-3">
            <span>
              <span className="block text-white">Hide my balance</span>
              <span className="block text-xs text-krypt-dim">In the top bar and the sidebar, for streaming or screenshots.</span>
            </span>
            <input type="checkbox" checked={hidden} onChange={(e) => setHideBalance(e.target.checked)} data-testid="wallet-hide" className="h-4 w-4 shrink-0 accent-sky-500" />
          </label>

          {hasKey && config && (
            <label className="flex items-center justify-between gap-3">
              <span className="text-white">Check my real balance every</span>
              <select
                value={config.balancePollInterval ?? 60}
                data-testid="wallet-check-every"
                onChange={(e) => void window.krypt.config.update({ balancePollInterval: Number(e.target.value) }).then(() => refresh.state())}
                className="krypt-input w-32 shrink-0"
              >
                {CHECK_EVERY.map((o) => <option key={o.seconds} value={o.seconds}>{o.label}</option>)}
              </select>
            </label>
          )}

          {!live && config && (
            <div className="flex items-end gap-2">
              <div className="flex-1">
                <label className="krypt-label">Pretend money to start with</label>
                <NumberInput
                  prefix="$"
                  min={1}
                  max={10_000_000}
                  value={config.terminalPaperBankrollUsd ?? 1000}
                  onChange={(v) => void window.krypt.config.update({ terminalPaperBankrollUsd: v }).then(() => Promise.all([refresh.state(), refresh.paper()]))}
                />
              </div>
              <button type="button" className="krypt-btn-default" disabled={reset.busy} onClick={reset.ask} data-testid="wallet-paper-reset">
                <RotateCcw className="h-4 w-4" /> Reset paper
              </button>
            </div>
          )}

          {onNav && (
            <div className="flex flex-wrap gap-2">
              <button type="button" className="krypt-btn-default" onClick={go('api')} data-testid="wallet-api-key">
                <KeyRound className="h-4 w-4" /> {hasKey ? 'Change my API key' : 'Connect my account'}
              </button>
              <button type="button" className="krypt-btn-default" onClick={go('settings')} data-testid="wallet-settings">
                <Settings className="h-4 w-4" /> All settings
              </button>
            </div>
          )}
        </div>
      </div>
      {reset.dialog}
    </DialogShell>
  );
}
