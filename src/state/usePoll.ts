import { useCallback, useEffect, useRef, useState } from 'react';
import { userMessage } from '../utils/errors';

/**
 * Poll one backend read, with the two bugs a naive version always has fixed.
 *
 * A generation token, not a boolean:
 *   * a single `alive` ref shared by every run of the effect lets a request
 *     still in flight for the PREVIOUS entity (an agent id, the account mode)
 *     sail through and be committed as the new one's data;
 *   * an `inFlight` boolean never cleared by the cleanup made the new
 *     entity's first fetch return early, so the wrong data sat on screen for a
 *     whole interval.
 * Every run stamps its generation, a later run supersedes it, and a
 * superseded response is dropped instead of committed. The in-flight guard
 * is per generation, so a backend slower than the interval never accumulates
 * requests.
 *
 * Errors keep the last good data on screen and say so (`error`), rather than
 * blanking the panel — stale-and-said-so beats empty-and-unexplained.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
  enabled = true,
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetchRef = useRef(fetcher);
  fetchRef.current = fetcher;
  const gen = useRef(0);
  const inFlight = useRef(-1);

  const run = useCallback(async (mine: number): Promise<void> => {
    if (inFlight.current === mine) return;
    inFlight.current = mine;
    try {
      const v = await fetchRef.current();
      if (mine !== gen.current) return;
      setData(v);
      setError(null);
    } catch (e) {
      if (mine !== gen.current) return;
      setError(userMessage(e));
    } finally {
      if (inFlight.current === mine) inFlight.current = -1;
      if (mine === gen.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const mine = ++gen.current;
    if (!enabled) {
      setLoading(false);
      return;
    }
    setLoading(true);
    // Entity changed: drop the previous one's data so a stale row can never
    // be read as belonging to the new one while the first fetch lands.
    setData(null);
    void run(mine);
    if (intervalMs <= 0) return;
    const id = setInterval(() => {
      // Nobody is looking (minimized / hidden to tray): skip the tick.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      if (mine === gen.current) void run(mine);
    }, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, intervalMs, enabled, ...deps]);

  const reload = useCallback((): void => { void run(gen.current); }, [run]);
  return { data, error, loading, reload };
}
