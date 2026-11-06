import { app, BrowserWindow, Notification } from 'electron';
import { spawn, spawnSync, ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { BackendInfo, BackendStatus, LogEntry } from '../../shared/types';

// ──────────────────────────────────────────────────────────────────────
// Backend lifecycle manager. Spawns the Python child, talks JSON-RPC
// over stdin/stdout, restarts on crash, broadcasts state to all
// renderer windows.
// ──────────────────────────────────────────────────────────────────────

type Pending = {
  resolve: (val: any) => void;
  reject: (err: Error) => void;
  method: string;
  startedAt: number;
};

type EventCallback = (name: string, data: any) => void;
type LogCallback = (entry: LogEntry) => void;

// A backend that survives this long is considered to have started cleanly;
// a later crash gets a fresh restart budget instead of counting toward the
// rapid-crash cap.
const STABLE_UPTIME_MS = 30_000;
// Hard ceiling on consecutive *fast* crashes before we stop respawning. This
// is what prevents a backend that dies on startup from spinning up dozens of
// short-lived processes ("20 instances") in a tight loop.
const MAX_RAPID_RESTARTS = 5;
// Hang watchdog: ping the backend on this cadence; after this many consecutive
// ping timeouts, force-kill + restart. Catches a WEDGED-but-alive backend
// (event loop blocked, deadlock) that the exit handler can never see — the
// status stayed 'running' forever while all trading was dead.
const PING_INTERVAL_MS = 60_000;
const PING_TIMEOUT_MS = 15_000;
const PING_FAILS_TO_RESTART = 3;
// Slow self-heal tier. After the rapid-crash cap gives up (gaveUp), we don't
// stop forever — a transient startup-locking window (AV scan, Windows Update,
// a briefly-locked SQLite at db.init_db) can produce 5 quick crashes and then
// clear on its own. We re-attempt on this slow cadence, resetting the fast
// budget each time. Kept intentionally UNBOUNDED: a tray-minimized 24/7 bot
// should keep trying to come back for as long as the app is open; the 5-minute
// gap is gentle enough to never become a tight crash loop, and the notification
// is de-duped so a genuinely-broken bundle can't spam the user.
const SLOW_RETRY_INTERVAL_MS = 5 * 60_000;

class PythonBackend {
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, Pending>();
  private buffer = '';
  private status: BackendStatus = 'stopped';
  private startedAt: string | null = null;
  private lastError: string | null = null;
  private authOk = false;
  private pythonOk = false;
  private restartTimer: NodeJS.Timeout | null = null;
  private slowRetryTimer: NodeJS.Timeout | null = null;
  private slowRetryCount = 0;
  private crashNotified = false;
  private pingTimer: NodeJS.Timeout | null = null;
  private pingFails = 0;
  private pingInFlight = false;
  private restartAttempts = 0;
  private childStartedAtMs = 0;
  private gaveUp = false;
  private nextId = 1;
  private eventHandlers: EventCallback[] = [];
  private logHandlers: LogCallback[] = [];
  private statusHandlers: ((info: BackendInfo) => void)[] = [];
  private requestedStop = false;

  // ─── Public API ────────────────────────────────────────────────────

  async start(): Promise<void> {
    if (this.status === 'running' || this.status === 'starting') return;
    this.requestedStop = false;
    // A manual (re)start clears any prior give-up state and the rapid-crash
    // budget so the user always gets a fresh set of attempts.
    this.gaveUp = false;
    this.restartAttempts = 0;
    this.slowRetryCount = 0;
    this.crashNotified = false;
    // Cancel any pending auto-restart (fast or slow) so it can't fire AFTER we
    // spawn here and bring up a second backend (two live backends both place
    // real orders).
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (this.slowRetryTimer) {
      clearTimeout(this.slowRetryTimer);
      this.slowRetryTimer = null;
    }
    this.startChild();
  }

  async stop(): Promise<void> {
    this.requestedStop = true;
    this.stopPingWatchdog();
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    // A user Stop must stay stopped — cancel the slow self-heal tier too.
    if (this.slowRetryTimer) {
      clearTimeout(this.slowRetryTimer);
      this.slowRetryTimer = null;
    }
    const c = this.child;
    if (!c) {
      this.setStatus('stopped');
      return;
    }
    try {
      // Polite shutdown via stdin so the backend can flush logs.
      this.send({ type: 'rpc', id: this.id(), method: 'shutdown', params: {} });
    } catch {
      /* ignore */
    }
    setTimeout(() => {
      if (this.child && !this.child.killed) {
        try {
          this.child.kill('SIGTERM');
        } catch {
          /* ignore */
        }
      }
    }, 1500);
  }

  async restart(): Promise<void> {
    this.setStatus('restarting');
    await this.stop();
    setTimeout(() => this.start(), 1500);
  }

  info(): BackendInfo {
    return {
      status: this.status,
      pid: this.child?.pid ?? null,
      startedAt: this.startedAt,
      lastError: this.lastError,
      pythonOk: this.pythonOk,
      authOk: this.authOk,
    };
  }

  isRunning(): boolean {
    return this.status === 'running';
  }

  /**
   * Recovery state for out-of-renderer surfaces (the system tray). Distinguishes
   * "gave up after repeated crashes" from a plain user Stop:
   *   - gaveUp:   the rapid-restart cap was hit; the fast tier is exhausted.
   *   - retrying: a slow self-heal re-attempt is scheduled (backend will try
   *               again on its own — the user need not intervene).
   */
  recoveryState(): { gaveUp: boolean; retrying: boolean } {
    return { gaveUp: this.gaveUp, retrying: this.slowRetryTimer !== null };
  }

  onEvent(cb: EventCallback): () => void {
    this.eventHandlers.push(cb);
    return () => {
      this.eventHandlers = this.eventHandlers.filter((c) => c !== cb);
    };
  }

  onLog(cb: LogCallback): () => void {
    this.logHandlers.push(cb);
    return () => {
      this.logHandlers = this.logHandlers.filter((c) => c !== cb);
    };
  }

  onStatusChange(cb: (info: BackendInfo) => void): () => void {
    this.statusHandlers.push(cb);
    return () => {
      this.statusHandlers = this.statusHandlers.filter((c) => c !== cb);
    };
  }

  /** Send a JSON-RPC request to the backend. Resolves with `result` (or rejects on backend error). */
  async request<T = any>(method: string, params: any = {}, timeoutMs = 30_000): Promise<T> {
    if (!this.child || this.status !== 'running') {
      throw new Error('Backend not running');
    }
    const id = this.id();
    const msg = { type: 'rpc', id, method, params };
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`RPC ${method} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, {
        method,
        startedAt: Date.now(),
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      try {
        this.send(msg);
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e as Error);
      }
    });
  }

  // ─── Internals ─────────────────────────────────────────────────────

  private id(): string {
    return `r${this.nextId++}`;
  }

  /**
   * How an AI client should launch the stdio MCP relay: this same backend
   * binary with `--mcp` (python/mcp_stdio.py). It needs only the userData path
   * — it finds the running app through mcp/bridge.json and reads the token
   * from the encrypted store itself, so no secret goes into a client config.
   */
  relaySpec(): { command: string; args: string[]; env: Record<string, string> } | null {
    const prevError = this.lastError;
    const r = this.resolveBackendBin();
    this.lastError = prevError;
    if (!r) return null;
    return {
      command: r.cmd,
      args: [...r.args, '--mcp'],
      env: { KRYPT_POLYBOT_US_USERDATA: app.getPath('userData') },
    };
  }

  private resolveBackendBin(): { cmd: string; args: string[]; cwd: string } | null {
    // Production: ship a PyInstaller-compiled `krypt-polybot-us-backend.exe`
    // under resources/python/. Dev: use the project venv if present,
    // otherwise fall back through `py -3` / `python` / `python3`.
    if (app.isPackaged) {
      const base = join(process.resourcesPath, 'python');
      const exe =
        process.platform === 'win32'
          ? join(base, 'krypt-polybot-us-backend.exe')
          : join(base, 'krypt-polybot-us-backend');
      if (existsSync(exe)) {
        return { cmd: exe, args: [], cwd: base };
      }
      this.lastError = `bundled backend binary missing at ${exe}`;
      return null;
    }
    const pyDir = join(process.cwd(), 'python');
    const servicePath = join(pyDir, 'service.py');
    if (!existsSync(servicePath)) {
      this.lastError = `service.py not found at ${servicePath}`;
      return null;
    }

    // 1. Project-local venv beats anything on PATH.
    const venvPy = process.platform === 'win32'
      ? join(pyDir, '.venv', 'Scripts', 'python.exe')
      : join(pyDir, '.venv', 'bin', 'python');
    if (existsSync(venvPy)) {
      return { cmd: venvPy, args: [servicePath], cwd: pyDir };
    }

    // 2. System Python — try in order. Probe with --version (cheap).
    const candidates: { cmd: string; pre: string[] }[] = process.platform === 'win32'
      ? [
          { cmd: 'py', pre: ['-3'] },
          { cmd: 'python', pre: [] },
          { cmd: 'python3', pre: [] },
        ]
      : [
          { cmd: 'python3', pre: [] },
          { cmd: 'python', pre: [] },
        ];
    for (const c of candidates) {
      try {
        const probe = spawnSync(c.cmd, [...c.pre, '--version'], {
          stdio: 'ignore', shell: false, windowsHide: true,
        });
        if (!probe.error && probe.status === 0) {
          return { cmd: c.cmd, args: [...c.pre, servicePath], cwd: pyDir };
        }
      } catch { /* keep trying */ }
    }
    this.lastError = 'No Python 3 interpreter found. Run `npm run py:setup` or install Python 3.10+ from https://python.org.';
    return null;
  }

  private startChild(): void {
    this.setStatus('starting');
    const userData = app.getPath('userData');
    // Make sure logs/data/credentials dirs exist before backend starts.
    for (const sub of ['logs', 'data', 'credentials']) {
      const d = join(userData, sub);
      if (!existsSync(d)) mkdirSync(d, { recursive: true });
    }

    const resolved = this.resolveBackendBin();
    if (!resolved) {
      this.pythonOk = false;
      this.setStatus('crashed');
      // No Python at all — don't loop-retry forever.
      this.requestedStop = true;
      return;
    }
    const { cmd, args, cwd } = resolved;
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(cmd, args, {
        cwd,
        env: {
          ...process.env,
          KRYPT_POLYBOT_US_USERDATA: userData,
          KRYPT_APP_VERSION: app.getVersion(),
          PYTHONUNBUFFERED: '1',
          PYTHONIOENCODING: 'utf-8',
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      });
    } catch (e: any) {
      this.lastError = `spawn failed: ${e?.message || e}`;
      this.pythonOk = false;
      this.setStatus('crashed');
      this.scheduleRestart(true);
      return;
    }

    this.child = child;
    this.pythonOk = true;
    this.startedAt = new Date().toISOString();
    this.childStartedAtMs = Date.now();
    this.lastError = null;
    this.buffer = '';

    child.stdout.setEncoding('utf-8');
    child.stderr.setEncoding('utf-8');
    child.stdout.on('data', (chunk: string) => this.onStdout(chunk));
    child.stderr.on('data', (chunk: string) => this.onStderr(chunk));
    child.on('error', (err) => {
      if (this.child !== child) return;
      this.lastError = `child error: ${err.message}`;
      this.pythonOk = false;
    });
    child.on('exit', (code, signal) => {
      // Identity guard: if a new child was already assigned (restart race), the
      // OLD child's late exit must NOT null the new child or schedule another
      // spawn — that would leave two live Python backends both placing real
      // orders.
      if (this.child !== child) return;
      this.child = null;
      this.stopPingWatchdog();
      // Fail in-flight RPCs immediately — the UI shouldn't wait out their
      // full timeouts for a reply that can never come.
      for (const p of this.pending.values()) p.reject(new Error('backend exited'));
      this.pending.clear();
      if (this.requestedStop) {
        this.setStatus('stopped');
        return;
      }
      const uptime = Date.now() - this.childStartedAtMs;
      this.lastError = `backend exited (code=${code} signal=${signal})`;
      this.setStatus('crashed');
      // Only crashes that happen shortly after launch count toward the
      // rapid-crash cap. A backend that ran fine for a while and then died
      // gets a clean restart (handled inside scheduleRestart).
      this.scheduleRestart(uptime < STABLE_UPTIME_MS);
    });

    // Backend emits its own `backend:ready` event on first stdin tick.
    // We optimistically call status `running` once we see *any* event.

    this.startPingWatchdog(child);
  }

  private stopPingWatchdog(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    this.pingFails = 0;
    this.pingInFlight = false;
  }

  private startPingWatchdog(child: ChildProcessWithoutNullStreams): void {
    this.stopPingWatchdog();
    this.pingTimer = setInterval(() => {
      // Only watch the child we were started for; a restart re-arms its own.
      if (this.child !== child || this.status !== 'running' || this.requestedStop) return;
      if (this.pingInFlight) return; // previous ping still deciding
      this.pingInFlight = true;
      this.request('ping', {}, PING_TIMEOUT_MS)
        .then(() => {
          this.pingFails = 0;
        })
        .catch(() => {
          if (this.child !== child || this.requestedStop) return;
          this.pingFails++;
          if (this.pingFails >= PING_FAILS_TO_RESTART) {
            // Wedged-but-alive: the process exists but its event loop is not
            // answering. Force-kill; the exit handler owns the restart path
            // (identity-guarded, so this can never double-spawn).
            this.lastError = `backend unresponsive (${this.pingFails} pings timed out) — force-restarting`;
            const entry: LogEntry = {
              ts: new Date().toISOString(),
              level: 'ERROR',
              source: 'backend',
              msg: this.lastError,
            };
            for (const h of this.logHandlers) h(entry);
            this.stopPingWatchdog();
            try {
              child.kill('SIGKILL');
            } catch {
              /* ignore */
            }
          }
        })
        .finally(() => {
          this.pingInFlight = false;
        });
    }, PING_INTERVAL_MS);
  }

  private scheduleRestart(quickCrash: boolean): void {
    if (this.requestedStop || this.gaveUp) return;
    if (this.restartTimer) return;

    if (quickCrash) {
      this.restartAttempts++;
    } else {
      // Crashed only after a long, stable run — start the budget over. This is
      // a genuine recovery boundary, so also re-arm the crash notification: a
      // real crash hours later should alert again, not be swallowed by the
      // give-up we may have shown earlier.
      this.restartAttempts = 0;
      this.crashNotified = false;
      this.slowRetryCount = 0;
    }

    if (this.restartAttempts >= MAX_RAPID_RESTARTS) {
      // Stop the FAST loop. Something is wrong with the backend (bad bundle, AV
      // quarantine, a transient startup-locking window) — respawning
      // immediately would just keep launching short-lived processes.
      //
      // NOTE: we set gaveUp but deliberately do NOT set requestedStop. gaveUp
      // means "gave up after crashes" (recoverable); requestedStop means "the
      // user asked to stop" (must stay stopped). Conflating them used to brick
      // the backend permanently after a transient hiccup. gaveUp still blocks
      // the fast tier; the slow self-heal tier below owns recovery.
      this.gaveUp = true;
      this.lastError =
        `backend crashed ${this.restartAttempts}× on startup; pausing fast ` +
        `restarts, will retry every ${Math.round(SLOW_RETRY_INTERVAL_MS / 60_000)}m. ` +
        `${this.lastError ?? ''}`.trim();
      this.setStatus('crashed');
      const entry: LogEntry = {
        ts: new Date().toISOString(),
        level: 'ERROR',
        source: 'backend',
        msg: this.lastError,
      };
      for (const h of this.logHandlers) h(entry);
      // A tray-minimized 24/7 user never sees the in-app card — alert the OS.
      // De-duped: only the first give-up of a crash cycle notifies (reset once
      // the backend runs cleanly again, or on a manual start), so a genuinely
      // broken bundle can't fire a toast every 5 minutes forever.
      if (!this.crashNotified) {
        this.crashNotified = true;
        this.notifyCrashed();
      }
      this.scheduleSlowRetry();
      return;
    }

    const delay = Math.min(2000 * this.restartAttempts, 20_000);
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      // A stop()/start() may have raced in during the backoff window — don't
      // resurrect a backend that was asked to stop or already respawned.
      if (this.requestedStop || this.gaveUp || this.child) return;
      this.startChild();
    }, delay);
  }

  /**
   * Slow self-heal tier. Scheduled after the fast tier gives up. On fire it
   * wipes the rapid-crash budget and clears gaveUp so a cleared transient
   * condition gets a fresh set of fast attempts. If it crashes right back out,
   * scheduleRestart hits the cap again and re-arms us — so the steady state is
   * a short fast burst every SLOW_RETRY_INTERVAL_MS, never a tight loop.
   */
  private scheduleSlowRetry(): void {
    if (this.requestedStop) return; // a real user Stop stays stopped
    if (this.slowRetryTimer) return; // already armed
    this.slowRetryTimer = setTimeout(() => {
      this.slowRetryTimer = null;
      // stop()/start() may have raced in during the wait.
      if (this.requestedStop || this.child) return;
      this.slowRetryCount++;
      this.restartAttempts = 0;
      this.gaveUp = false;
      const entry: LogEntry = {
        ts: new Date().toISOString(),
        level: 'INFO',
        source: 'backend',
        msg: `backend self-heal retry #${this.slowRetryCount} — attempting restart`,
      };
      for (const h of this.logHandlers) h(entry);
      // Reflect the retry in the tray immediately (retrying flag flips off).
      this.emitStatus();
      this.startChild();
    }, SLOW_RETRY_INTERVAL_MS);
    // Tray reads recoveryState() on the next status broadcast; make sure the
    // "retrying" flag is visible now that a slow attempt is pending.
    this.emitStatus();
  }

  private notifyCrashed(): void {
    try {
      if (!Notification.isSupported()) return;
      const n = new Notification({
        title: 'Krypt PolyBot US — trading stopped',
        body:
          'The trading backend crashed repeatedly and has stopped. ' +
          'It will keep retrying automatically; open the app to check its status.',
      });
      n.show();
    } catch {
      /* notifications are best-effort — never let one break recovery */
    }
  }

  private send(obj: any): void {
    if (!this.child || !this.child.stdin.writable) {
      throw new Error('Backend stdin not writable');
    }
    this.child.stdin.write(JSON.stringify(obj) + '\n');
  }

  private onStdout(chunk: string): void {
    this.buffer += chunk;
    let nl: number;
    while ((nl = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, nl).trim();
      this.buffer = this.buffer.slice(nl + 1);
      if (!line) continue;
      this.handleLine(line);
    }
  }

  private onStderr(chunk: string): void {
    // Stderr means an unexpected stack — log it.
    const text = chunk.toString().trim();
    if (text) {
      const entry: LogEntry = {
        ts: new Date().toISOString(),
        level: 'ERROR',
        source: 'backend',
        msg: text,
      };
      for (const h of this.logHandlers) h(entry);
    }
  }

  private handleLine(line: string): void {
    let obj: any;
    try {
      obj = JSON.parse(line);
    } catch {
      // Not JSON — treat as info log.
      const entry: LogEntry = {
        ts: new Date().toISOString(),
        level: 'INFO',
        source: 'backend',
        msg: line,
      };
      for (const h of this.logHandlers) h(entry);
      return;
    }

    if (this.status === 'starting') {
      // Mark as running on the first received line (any kind). Note: we do
      // NOT reset restartAttempts here — a backend can emit a line and then
      // crash immediately, which would otherwise reset the counter forever
      // and let it respawn endlessly. The counter is reset only after the
      // process has stayed up for STABLE_UPTIME_MS (see scheduleRestart).
      this.setStatus('running');
    }

    if (obj.type === 'rpc') {
      const p = this.pending.get(obj.id);
      if (!p) return;
      this.pending.delete(obj.id);
      if (obj.ok) {
        p.resolve(obj.result);
      } else {
        p.reject(new Error(obj.error || 'rpc failed'));
      }
      return;
    }
    if (obj.type === 'event') {
      const name = String(obj.name || '');
      const data = obj.data;
      if (name === 'backend:authChanged') {
        this.authOk = !!data?.authOk;
        this.emitStatus();
      }
      for (const h of this.eventHandlers) h(name, data);
      return;
    }
    if (obj.type === 'log') {
      const entry: LogEntry = {
        ts: obj.ts || new Date().toISOString(),
        level: obj.level || 'INFO',
        source: obj.source || 'backend',
        msg: obj.msg || '',
      };
      for (const h of this.logHandlers) h(entry);
      return;
    }
  }

  private setStatus(s: BackendStatus): void {
    this.status = s;
    this.emitStatus();
  }

  private emitStatus(): void {
    const info = this.info();
    for (const h of this.statusHandlers) h(info);
    // Fan out to every renderer.
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send('backend:info', info);
      }
    }
  }
}

export const pythonBackend = new PythonBackend();
