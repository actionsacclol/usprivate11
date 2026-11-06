import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, ArrowRight, Copy, RefreshCw, X, XCircle } from 'lucide-react';
import type { AppAlert } from '@shared/types';
import type { PageId } from '../App';
import { useApp } from '../state/AppStateProvider';
import { useToast } from '../state/ToastProvider';
import { alertKey, engineAlert, updateDismissed } from '../utils/alerts';
import { GlassPanel } from './glass/GlassPanel';

const POLL_MS = 10_000;
const MAX_SHOWN = 4;

/**
 * What's wrong right now, in the bottom-right corner: each alert says what happened
 * and what to do. An alert closes itself when its problem is fixed. Closing one with
 * the X keeps it closed until the problem goes away and comes back, or gets worse.
 *
 * Toasts (top right) stay for "this just happened". These are for "this is still true".
 *
 * Frost, not live glass: these can stay up for hours, and live glass costs an SVG
 * filter for as long as it's on screen.
 */
export function AlertStack({ onNav }: { onNav: (p: PageId) => void }) {
  const { backend, state, refresh } = useApp();
  const toast = useToast();
  const [fromBackend, setFromBackend] = useState<AppAlert[]>([]);
  const [dismissed, setDismissed] = useState<ReadonlyMap<string, number>>(new Map());
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const running = backend.status === 'running';

  const pull = useCallback(async (): Promise<void> => {
    const list = await window.krypt.alerts.list().catch(() => null);
    if (list) setFromBackend(list);
  }, []);

  useEffect(() => {
    if (!running) {
      // A dead backend's last report isn't true any more; only `engine` is.
      setFromBackend([]);
      return undefined;
    }
    void pull();
    const t = window.setInterval(() => { if (!document.hidden) void pull(); }, POLL_MS);
    const onVisible = (): void => { if (!document.hidden) void pull(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(t);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [running, pull]);

  // How long the backend has been in this status, ticking only while it's unhealthy.
  const statusSince = useRef({ status: backend.status, at: Date.now() });
  if (statusSince.current.status !== backend.status) {
    statusSince.current = { status: backend.status, at: Date.now() };
  }
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (running) return undefined;
    const t = window.setInterval(() => setNow(Date.now()), 5000);
    return () => window.clearInterval(t);
  }, [running]);

  const alerts = useMemo<AppAlert[]>(() => {
    const engine = engineAlert(backend, Math.max(now, Date.now()) - statusSince.current.at);
    return engine ? [engine, ...fromBackend] : fromBackend;
  }, [backend, fromBackend, now]);

  useEffect(() => {
    setDismissed((d) => updateDismissed(d, alerts, Date.now()));
  }, [alerts]);

  // Nothing on top of first-run setup: the app isn't in use yet.
  if (!state?.acceptedDisclaimer) return null;

  const open = alerts.filter((a) => !dismissed.has(alertKey(a)));
  if (open.length === 0) return null;
  const shown = expanded ? open : open.slice(0, MAX_SHOWN);
  const hidden = open.length - shown.length;

  const act = async (a: AppAlert): Promise<void> => {
    const action = a.action;
    if (!action) return;
    if (action.kind === 'nav') {
      onNav(action.page as PageId);
      return;
    }
    if (action.kind === 'copy') {
      await navigator.clipboard.writeText(action.text)
        .then(() => toast.success('Copied'))
        .catch(() => toast.error(`Could not copy: ${action.text}`));
      return;
    }
    setBusy(true);
    try {
      if (action.kind === 'restart') {
        await window.krypt.backend.restart();
        window.setTimeout(() => { void refresh.backend(); }, 1200);
      } else {
        const r = await window.krypt.trading.resume();
        if (r.ok) toast.success(r.message || 'Trading resumed');
        else toast.error(r.message || 'Could not resume');
        await pull();
      }
    } catch (e) {
      toast.error(`That didn't work: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="pointer-events-none fixed bottom-5 right-5 z-40 flex max-h-[calc(100vh-5rem)] w-[380px] max-w-[calc(100vw-2.5rem)] flex-col gap-2 overflow-y-auto"
      data-testid="alert-stack"
    >
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="pointer-events-auto self-end rounded-full border border-krypt-border bg-krypt-surface2 px-3 py-1 text-xs text-white/90 hover:border-krypt-borderHi"
          data-testid="alert-more"
        >
          {hidden} more {hidden === 1 ? 'alert' : 'alerts'}
        </button>
      )}
      {shown.map((a) => {
        const isError = a.severity === 'error';
        const Icon = isError ? XCircle : AlertTriangle;
        const ActionIcon = a.action?.kind === 'copy' ? Copy
          : a.action?.kind === 'nav' ? ArrowRight : RefreshCw;
        return (
          <GlassPanel
            key={alertKey(a)}
            live={false}
            preset="modal"
            display="flex"
            tint="linear-gradient(165deg, rgba(24,30,52,0.94), rgba(10,12,22,0.97))"
            style={{
              boxShadow: isError
                ? 'inset 0 0 0 1px rgba(239,68,68,0.55), 0 12px 32px -14px rgba(239,68,68,0.6)'
                : 'inset 0 0 0 1px rgba(245,158,11,0.5), 0 12px 32px -14px rgba(245,158,11,0.5)',
            }}
            className="glass-pop pointer-events-auto shrink-0 items-start gap-3 rounded-2xl p-3"
            role={isError ? 'alert' : 'status'}
            data-testid={`alert-${a.id}`}
          >
            <Icon className={`mt-0.5 h-5 w-5 shrink-0 ${isError ? 'text-krypt-loss' : 'text-krypt-warn'}`} />
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold text-white">{a.title}</div>
              {a.detail && <div className="mt-0.5 break-words text-xs text-krypt-muted">{a.detail}</div>}
              <div className="mt-1.5 break-words text-xs text-white/90">
                <span className="font-semibold text-white">What to do: </span>{a.fix}
              </div>
              {a.action && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void act(a)}
                  className="krypt-btn-default mt-2 text-xs"
                  data-testid={`alert-action-${a.id}`}
                >
                  {a.action.label} <ActionIcon className="h-3 w-3" />
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={() => setDismissed((d) => new Map(d).set(alertKey(a), Date.now()))}
              className="shrink-0 text-krypt-muted transition-colors hover:text-white"
              aria-label={`Close: ${a.title}`}
              data-testid={`alert-close-${a.id}`}
            >
              <X className="h-4 w-4" />
            </button>
          </GlassPanel>
        );
      })}
    </div>
  );
}
