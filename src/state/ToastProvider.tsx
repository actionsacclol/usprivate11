import {
  createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState,
} from 'react';
import { CheckCircle, XCircle, Info, AlertTriangle, X } from 'lucide-react';
import { cls } from '../utils/format';
import { GlassPanel } from '../components/glass/GlassPanel';

export type ToastKind = 'success' | 'error' | 'info' | 'warn';

interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  ttl: number;
}

interface ToastApi {
  push: (message: string, kind?: ToastKind, ttl?: number) => void;
  success: (m: string) => void;
  error: (m: string) => void;
  info: (m: string) => void;
  warn: (m: string) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be inside ToastProvider');
  return ctx;
}

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback(
    (message: string, kind: ToastKind = 'info', ttl = 4500) => {
      const id = nextId++;
      setToasts((cur) => [...cur, { id, kind, message, ttl }]);
      setTimeout(() => {
        setToasts((cur) => cur.filter((t) => t.id !== id));
      }, ttl);
    },
    [],
  );

  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (m) => push(m, 'success'),
      error: (m) => push(m, 'error'),
      info: (m) => push(m, 'info'),
      warn: (m) => push(m, 'warn'),
    }),
    [push],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="pointer-events-none fixed right-5 top-14 z-[60] flex w-[360px] max-w-full flex-col gap-2">
        {toasts.map((t) => (
          <ToastItem
            key={t.id}
            toast={t}
            onClose={() => setToasts((cur) => cur.filter((x) => x.id !== t.id))}
          />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastItem({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const [exit, setExit] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setExit(true), toast.ttl - 250);
    return () => clearTimeout(t);
  }, [toast.ttl]);

  const Icon =
    toast.kind === 'success' ? CheckCircle :
    toast.kind === 'error' ? XCircle :
    toast.kind === 'warn' ? AlertTriangle :
    Info;

  // Live glass, and transient: a toast only costs its SVG filter while it is
  // on screen. The entrance/exit animate the glass element itself — an
  // ancestor with opacity < 1 would become its backdrop root and blank the
  // refraction for the whole fade.
  return (
    <GlassPanel
      preset="modal"
      display="flex"
      tint="linear-gradient(165deg, rgba(24,30,52,0.66), rgba(10,12,22,0.76))"
      style={{ boxShadow: TOAST_GLOW[toast.kind] }}
      className={cls(
        'glass-pop pointer-events-auto items-start gap-3 rounded-2xl p-3',
        exit && 'opacity-0 transition-opacity duration-200',
      )}
    >
      <Icon
        className={cls(
          'mt-0.5 h-5 w-5 shrink-0',
          toast.kind === 'success' && 'text-krypt-win',
          toast.kind === 'error' && 'text-krypt-loss',
          toast.kind === 'warn' && 'text-krypt-warn',
          toast.kind === 'info' && 'text-krypt-purple',
        )}
      />
      <div className="flex-1 text-sm text-white/95">{toast.message}</div>
      <button
        onClick={onClose}
        className="text-krypt-muted transition-colors hover:text-white"
        aria-label="Dismiss"
      >
        <X className="h-4 w-4" />
      </button>
    </GlassPanel>
  );
}

// A coloured inner hairline plus a soft bloom: the kind reads at a glance
// without an opaque border fighting the glass rim.
const TOAST_GLOW: Record<ToastKind, string> = {
  success: 'inset 0 0 0 1px rgba(34,197,94,0.35), 0 12px 32px -14px rgba(34,197,94,0.55)',
  error: 'inset 0 0 0 1px rgba(239,68,68,0.45), 0 12px 32px -14px rgba(239,68,68,0.6)',
  warn: 'inset 0 0 0 1px rgba(245,158,11,0.4), 0 12px 32px -14px rgba(245,158,11,0.5)',
  info: 'inset 0 0 0 1px rgba(255,255,255,0.08), 0 12px 32px -14px rgba(59,130,246,0.55)',
};
