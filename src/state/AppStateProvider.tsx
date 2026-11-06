import {
  createContext, ReactNode, useContext, useEffect, useMemo, useRef, useState,
} from 'react';
import type {
  AccountSnapshot, AppState, BackendInfo, BotPosition, CredentialsState,
  CredentialsStatusAll,
  LogEntry, TraderConfig,
} from '@shared/types';
import type { PaperStatus } from '@shared/market';
import { createPoll } from '@shared/poll';
import { useToast } from './ToastProvider';

interface AppStateApi {
  state: AppState | null;
  config: TraderConfig | null;
  backend: BackendInfo;
  account: AccountSnapshot | null;
  /** The account's paper book. Only read while the app is in Paper. */
  paper: PaperStatus | null;
  positions: BotPosition[];
  credentials: CredentialsState | null;
  credentialsAll: CredentialsStatusAll | null;
  appVersion: string;
  refresh: {
    state: () => Promise<void>;
    account: () => Promise<void>;
    /** Reads the exchange and the paper book now. */
    wallet: () => Promise<void>;
    paper: () => Promise<void>;
    positions: () => Promise<void>;
    credentials: () => Promise<void>;
    backend: () => Promise<void>;
  };
}

const AppCtx = createContext<AppStateApi | null>(null);

// The log stream lives in its OWN context. It flushes every ~150ms on a chatty
// backend, and while it sat in the main context value that flush re-rendered
// every single useApp() consumer — the whole app, four times a second, for a
// list two screens read. Nothing else changes when a log line arrives.
const LogsCtx = createContext<LogEntry[]>([]);

export function useApp(): AppStateApi {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error('useApp must be inside AppStateProvider');
  return ctx;
}

/** The in-memory log buffer. Separate from useApp() on purpose — see LogsCtx. */
export function useLogs(): LogEntry[] {
  return useContext(LogsCtx);
}

const DEFAULT_BACKEND: BackendInfo = {
  status: 'stopped',
  pid: null,
  startedAt: null,
  lastError: null,
  pythonOk: false,
  authOk: false,
};

/** OS-level toast for money moments (fills, settlements). Electron grants
 * Notification permission by default; failures are silently ignored. */
function notify(title: string, body: string): void {
  try {
    // eslint-disable-next-line no-new
    new Notification(title, { body, silent: true });
  } catch { /* headless / permission denied — never break state flow */ }
}

export function AppStateProvider({ children }: { children: ReactNode }) {
  const toast = useToast();
  const [state, setState] = useState<AppState | null>(null);
  const [backend, setBackend] = useState<BackendInfo>(DEFAULT_BACKEND);
  const [account, setAccount] = useState<AccountSnapshot | null>(null);
  const [paper, setPaper] = useState<PaperStatus | null>(null);
  const [positions, setPositions] = useState<BotPosition[]>([]);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [credentials, setCredentials] = useState<CredentialsState | null>(null);
  const [credentialsAll, setCredentialsAll] = useState<CredentialsStatusAll | null>(null);
  const [appVersion, setAppVersion] = useState('1.0.0');

  // Maintain a recent-positions merge order. Use a ref for high-frequency
  // updates so we don't churn React state.
  const positionsByIdRef = useRef<Map<number, BotPosition>>(new Map());

  const flushPositions = (): void => {
    const arr = Array.from(positionsByIdRef.current.values());
    // NaN-safe sort with a fallback chain. If the primary field is
    // missing or unparseable we fall through to the next, otherwise
    // the comparator returns NaN and the sort silently shuffles rows.
    const ts = (p: BotPosition): number => {
      for (const c of [p.lastUpdated, p.resolvedAt, p.createdAt]) {
        if (!c) continue;
        const t = new Date(c).getTime();
        if (Number.isFinite(t)) return t;
      }
      return 0;
    };
    arr.sort((a, b) => ts(b) - ts(a));
    setPositions(arr.slice(0, 500));
  };

  // Coalesce high-frequency position events. A burst of fills used to
  // fire one full sort + re-render PER event; instead schedule a single
  // trailing flush ~150ms after the first event in a burst. The ref map is
  // updated synchronously, so no data is lost — only the render is batched.
  const flushPosTimer = useRef<number | null>(null);
  const scheduleFlushPositions = (): void => {
    if (flushPosTimer.current != null) return;
    flushPosTimer.current = window.setTimeout(() => {
      flushPosTimer.current = null;
      flushPositions();
    }, 150);
  };
  // Same batching for the log stream: a chatty backend used to trigger one
  // full context re-render PER line. Buffer lines and flush every ~150ms.
  const logBufRef = useRef<LogEntry[]>([]);
  const flushLogTimer = useRef<number | null>(null);
  const scheduleFlushLogs = (): void => {
    if (flushLogTimer.current != null) return;
    flushLogTimer.current = window.setTimeout(() => {
      flushLogTimer.current = null;
      const buf = logBufRef.current;
      if (!buf.length) return;
      logBufRef.current = [];
      setLogs((cur) => {
        const next = [...cur, ...buf];
        if (next.length > 1000) next.splice(0, next.length - 1000);
        return next;
      });
    }, 150);
  };

  const refreshState = async (): Promise<void> => {
    try {
      const s = await window.krypt.state.get();
      setState(s);
    } catch { /* noop */ }
  };

  const refreshAccount = async (): Promise<void> => {
    try {
      const a = await window.krypt.data.account();
      setAccount(a);
    } catch { /* noop */ }
  };

  const refreshPaper = async (): Promise<void> => {
    // The book is only read in Paper: `paper` is null in Live.
    if (!paperMode) return;
    try {
      setPaper(await window.krypt.paper.status());
    } catch {
      setPaper(null);
    }
  };

  const refreshWallet = async (): Promise<void> => {
    await Promise.allSettled([
      window.krypt.data.accountRefresh().then(setAccount),
      refreshPaper(),
    ]);
  };

  const refreshPositions = async (): Promise<void> => {
    try {
      const rows = await window.krypt.data.positions({ limit: 500 });
      const map = new Map<number, BotPosition>();
      for (const r of rows) map.set(r.id, r);
      positionsByIdRef.current = map;
      flushPositions();
    } catch { /* noop */ }
  };

  const refreshCredentials = async (): Promise<void> => {
    try {
      const [c, all] = await Promise.all([
        window.krypt.credentials.status(),
        window.krypt.credentials.statusAll().catch(() => null),
      ]);
      setCredentials(c);
      setCredentialsAll(all);
    } catch { /* noop */ }
  };

  const refreshBackend = async (): Promise<void> => {
    try {
      const b = await window.krypt.backend.info();
      setBackend(b);
    } catch { /* noop */ }
  };

  // Initial load + subscriptions.
  useEffect(() => {
    let mounted = true;
    const init = async () => {
      const [s, b, a, c, pos, ls, ver] = await Promise.all([
        window.krypt.state.get(),
        window.krypt.backend.info(),
        window.krypt.data.account(),
        window.krypt.credentials.status().catch(() => null),
        window.krypt.data.positions({ limit: 500 }),
        window.krypt.logs.tail(500),
        window.krypt.app.version(),
      ]);
      if (!mounted) return;
      setState(s);
      setBackend(b);
      setAccount(a);
      setCredentials(c);
      // Don't await — initial paint shouldn't block on this.
      void window.krypt.credentials.statusAll().then(setCredentialsAll).catch(() => null);
      const pmap = new Map<number, BotPosition>();
      for (const r of pos) pmap.set(r.id, r);
      positionsByIdRef.current = pmap;
      flushPositions();
      setLogs(ls);
      setAppVersion(ver);
    };
    void init();

    const offState = window.krypt.state.onChange((s) => setState(s));
    const offBackend = window.krypt.backend.onInfo((b) => setBackend(b));
    const offAccount = window.krypt.data.onAccount((a) => setAccount(a));
    const offPos = window.krypt.data.onPosition((p) => {
      // Money-moment notifications: diff status transitions so fills,
      // stop-outs and settlements surface without staring at a table.
      const prev = positionsByIdRef.current.get(p.id);
      if (prev && prev.status !== p.status) {
        const name = p.title || p.ticker;
        if (p.status === 'filled' && prev.status !== 'filled') {
          notify(`Filled: ${name}`, `${p.filledContracts} @ ${p.avgFillPriceCents ?? '?'}¢`);
        } else if (p.resolved && !prev.resolved && typeof p.pnlUsd === 'number') {
          const won = p.pnlUsd >= 0;
          notify(`${won ? 'Won' : 'Lost'} $${Math.abs(p.pnlUsd).toFixed(2)} — ${name}`, p.status ?? '');
        }
      }
      positionsByIdRef.current.set(p.id, p);
      scheduleFlushPositions();
    });
    const offLog = window.krypt.logs.onAppend((entry) => {
      logBufRef.current.push(entry);
      scheduleFlushLogs();
    });
    // Backend explicitly tells us when credentials change (after save,
    // clear, or startup migration). React on it instead of waiting
    // for the 6s poll — that wait was why the dashboard kept flashing
    // "API keys not configured" right after the user saved keys.
    const offCreds = window.krypt.credentials.onChanged(() => {
      void refreshCredentials();
    });
    // Factory-reset wipes everything in the backend DB. Drop our caches
    // immediately and refetch — without this, stale positions
    // would keep painting until the next poll cycle.
    // The crypto engine hit its overall take-profit target and turned itself
    // off. The toggle flips via the broadcast state update; surface a toast so
    // the user knows WHY it stopped (a silent stop reads as "broken").
    const offAutoOff = window.krypt.crypto15m.onAutoOff((d) => {
      const gained = typeof d?.gained === 'number' ? d.gained : 0;
      const target = typeof d?.target === 'number' ? d.target : 0;
      toast.success(
        `Crypto take-profit hit (+$${gained.toFixed(2)} ≥ $${target.toFixed(2)}) — crypto engine turned off.`,
      );
    });
    const offReset = window.krypt.app.onDataReset(() => {
      positionsByIdRef.current = new Map();
      setPositions([]);
      void Promise.all([
        refreshAccount(),
        refreshPositions(),
      ]);
    });

    // ── Polling ──────────────────────────────────────────────────────
    //
    // Credentials and positions are PUSHED by the backend and subscribed to above
    // (`credentials:changed`, `position:new` + `position:update`), so the timer is
    // a single 60s reconcile: it exists to heal a dropped event or a backend
    // restart, not to be the transport.
    //
    // Visibility-aware (see shared/poll.ts): the app hides to the tray rather
    // than closing, so a hidden window holds no timer at all and runs a single
    // catch-up refresh the moment it comes back.
    const timers = {
      setInterval: (fn: () => void, ms: number) => window.setInterval(fn, ms),
      clearInterval: (id: number) => window.clearInterval(id),
      isHidden: () => document.visibilityState === 'hidden',
    };
    const reconcile = createPoll(
      () => Promise.all([refreshCredentials(), refreshPositions()]),
      60000,
      timers,
    );
    reconcile.start();
    const onVisibility = (): void => {
      reconcile.onVisibilityChange();
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      mounted = false;
      offState();
      offBackend();
      offAccount();
      offPos();
      offLog();
      offCreds();
      offAutoOff();
      offReset();
      if (flushPosTimer.current != null) window.clearTimeout(flushPosTimer.current);
      if (flushLogTimer.current != null) window.clearTimeout(flushLogTimer.current);
      document.removeEventListener('visibilitychange', onVisibility);
      reconcile.stop();
    };
  }, []);

  const config = state?.config ?? null;
  const paperMode = !!config && config.accountMode !== 'live';
  const backendUp = backend.status === 'running';

  // The paper balance has no push event, so it is read on a timer while it's on
  // screen. Marking open positions can reach the network, so not faster than this.
  // Starting with the engine means the first read lands as soon as there is an answer.
  useEffect(() => {
    if (!paperMode || !backendUp) {
      setPaper(null);
      return undefined;
    }
    void refreshPaper();
    const poll = createPoll(
      refreshPaper,
      10000,
      {
        setInterval: (fn: () => void, ms: number) => window.setInterval(fn, ms),
        clearInterval: (id: number) => window.clearInterval(id),
        isHidden: () => document.visibilityState === 'hidden',
      },
    );
    poll.start();
    const onVisibility = (): void => poll.onVisibilityChange();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      poll.stop();
    };
  }, [paperMode, backendUp]);

  const api = useMemo<AppStateApi>(
    () => ({
      state,
      config,
      backend,
      account,
      paper,
      positions,
      credentials,
      credentialsAll,
      appVersion,
      refresh: {
        state: refreshState,
        account: refreshAccount,
        wallet: refreshWallet,
        paper: refreshPaper,
        positions: refreshPositions,
        credentials: refreshCredentials,
        backend: refreshBackend,
      },
    }),
    [
      state, config, backend, account, paper, positions,
      credentials, credentialsAll, appVersion,
    ],
  );

  return (
    <AppCtx.Provider value={api}>
      <LogsCtx.Provider value={logs}>{children}</LogsCtx.Provider>
    </AppCtx.Provider>
  );
}
