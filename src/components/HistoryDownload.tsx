import { useEffect, useRef, useState } from 'react';
import { Download, Square } from 'lucide-react';
import type { HistoryDownloadStatus } from '@shared/types';
import { cls } from '../utils/format';
import {
  fmtDuration, historyActive, historyEstimate, startHistoryDownload,
} from '../utils/historyDownload';

const DAY_CHOICES = [7, 14, 30, 40];

/** Past crypto markets from Polymarket US's public history, so a new install can test a
 *  strategy today. `refreshKey` changes when something else started a download. */
export function HistoryDownload({ refreshKey = 0, onChange }: {
  refreshKey?: number;
  /** Stored data changed, so the counts around this card are stale. */
  onChange?: () => void;
}) {
  const [days, setDays] = useState(7);
  const [fiveMin, setFiveMin] = useState(false);
  const [job, setJob] = useState<HistoryDownloadStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const notify = useRef(onChange);
  notify.current = onChange;

  const active = historyActive(job);
  const intervals = fiveMin ? ['5m', '15m', 'hourly'] : ['15m', 'hourly'];

  // Keeps asking the whole time the card is open. The app reopens on the last page, so
  // this can mount before the engine is up, with a saved download about to resume, and
  // "no answer" must not read as "nothing is happening".
  const unknown = !job;
  useEffect(() => {
    let live = true;
    const read = (): void => {
      void window.krypt.trading.historyStatus()
        .then((s) => {
          if (!live || !s) return;
          setJob(s);
          // `active` too: the tick that finds it finished has the last rows to count.
          if (active || historyActive(s)) notify.current?.();
        })
        .catch(() => { /* backend down: the next tick asks again */ });
    };
    read();
    const id = window.setInterval(read, active || unknown ? 2000 : 10000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [refreshKey, active, unknown]);

  const start = async (): Promise<void> => {
    setBusy(true);
    const started = await startHistoryDownload(days, intervals);
    if (started) setJob(started);
    setBusy(false);
  };

  const stop = async (): Promise<void> => {
    setBusy(true);
    try {
      const stopped = await window.krypt.trading.historyCancel();
      if (stopped) setJob(stopped);
    } catch { /* backend down */ }
    setBusy(false);
    notify.current?.();
  };

  const total = job?.total ?? 0;
  const done = job?.done ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  return (
    <div className="rounded-lg bg-krypt-surface2/50 p-3" data-testid="history-download">
      <div className="text-sm font-semibold text-white">Download past prices</div>
      <p className="mt-1 text-xs leading-relaxed text-krypt-muted">
        Get past crypto up/down markets from Polymarket US so you can test a strategy right now.
        Free public data, no account needed, and nothing is traded. It runs in the background,
        and if you close the app it carries on next time.
      </p>

      {active ? (
        <div className="mt-3">
          <div className="h-1.5 overflow-hidden rounded-full bg-krypt-border">
            <div className="h-full rounded-full bg-krypt-purple transition-[width]" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2 text-[11px] text-krypt-dim">
            <span data-testid="history-progress">
              {total > 0
                ? `${done.toLocaleString()} of ${total.toLocaleString()} markets`
                : 'Starting…'}
              {job?.etaSec != null && total > 0 ? ` · about ${fmtDuration(job.etaSec)} left` : ''}
            </span>
            <button
              type="button"
              onClick={() => void stop()}
              disabled={busy}
              data-testid="history-stop"
              className="inline-flex items-center gap-1.5 rounded-md border border-krypt-border bg-krypt-surface2 px-2.5 py-1 text-xs text-krypt-dim transition-colors hover:text-white disabled:opacity-50"
            >
              <Square className="h-3 w-3" /> Stop
            </button>
          </div>
          {job?.message && <p className="mt-1.5 text-[11px] text-krypt-warn">{job.message}</p>}
        </div>
      ) : (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <div className="flex flex-wrap gap-1.5">
            {DAY_CHOICES.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDays(d)}
                data-testid={`history-days-${d}`}
                className={cls(
                  'rounded-md border px-2.5 py-1 text-xs transition-colors',
                  d === days
                    ? 'border-krypt-purple/60 bg-krypt-purple/15 text-krypt-purple'
                    : 'border-krypt-border bg-krypt-surface2 text-krypt-dim hover:text-white',
                )}
              >
                {d} days
              </button>
            ))}
          </div>
          <label className="flex items-center gap-1.5 text-[11px] text-krypt-dim">
            <input
              type="checkbox"
              checked={fiveMin}
              onChange={(e) => setFiveMin(e.target.checked)}
              data-testid="history-5m"
              className="h-3.5 w-3.5 accent-krypt-purple"
            />
            Include 5-minute markets (last 13 days only)
          </label>
          <button
            type="button"
            onClick={() => void start()}
            disabled={busy}
            data-testid="history-start"
            className="inline-flex items-center gap-2 rounded-md border border-krypt-purple/40 bg-krypt-purple/10 px-3 py-1.5 text-xs font-semibold text-krypt-purple transition-colors hover:bg-krypt-purple/20 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" />
            Download · about {historyEstimate(days, intervals)}
          </button>
        </div>
      )}

      {!active && job?.status === 'done' && (
        <p className="mt-2 text-[11px] text-krypt-win" data-testid="history-done">
          Done: {job.saved.toLocaleString()} markets saved.
          {job.unavailable > 0 ? ` ${job.unavailable.toLocaleString()} weren't available.` : ''}
          {job.message ? ` ${job.message}` : ''}
        </p>
      )}
      {!active && job?.status === 'cancelled' && (
        <p className="mt-2 text-[11px] text-krypt-dim">Stopped. What was already saved stays.</p>
      )}
      {!active && job?.status === 'error' && (
        <p className="mt-2 text-[11px] text-krypt-warn">{job.message}</p>
      )}

      <p className="mt-2 text-[11px] leading-relaxed text-krypt-dim">
        Only BTC goes back past about 3 weeks: ETH, SOL and XRP stop there. Downloaded markets have
        one price a minute and no order sizes, so they are coarser than what the app records while
        it&apos;s open. Backtest results tell you how many came from here.
      </p>
    </div>
  );
}
