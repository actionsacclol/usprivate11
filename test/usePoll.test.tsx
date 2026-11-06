import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { usePoll } from '../src/state/usePoll';

/**
 * `usePoll` drives every live panel in the terminal, and an adversarial review
 * found two real bugs in it that a test would have caught on the first run:
 *
 *   1. A single `alive` ref was shared by every run of the effect. On a deps
 *      change React runs the cleanup (`alive = false`) then the new effect body
 *      (`alive = true`) in the same commit, so a request still in flight for
 *      the PREVIOUS ticker sailed through the guard and was committed as the
 *      new one's data — you opened market B and saw market A's book.
 *
 *   2. An `inFlight` boolean was never cleared by the cleanup, so the new
 *      entity's immediate fetch returned early and you waited a whole poll
 *      interval for the right data, while the wrong data sat on screen.
 *
 * Both are pinned below. They are the reason this file exists.
 */

function Probe({ ticker, fetcher, interval = 0, enabled = true }: {
  ticker: string;
  fetcher: (t: string) => Promise<string>;
  interval?: number;
  enabled?: boolean;
}) {
  const { data, error, loading } = usePoll<string>(
    () => fetcher(ticker), interval, [ticker], enabled,
  );
  return (
    <div>
      <span data-testid="data">{data ?? 'null'}</span>
      <span data-testid="error">{error ?? 'none'}</span>
      <span data-testid="loading">{String(loading)}</span>
    </div>
  );
}

const deferred = () => {
  let resolve!: (v: string) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<string>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
};

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

describe('usePoll', () => {
  it('loads and shows data', async () => {
    const { rerender } = render(
      <Probe ticker="A" fetcher={async (t) => `data:${t}`} />,
    );
    await waitFor(() => expect(screen.getByTestId('data').textContent).toBe('data:A'));
    expect(screen.getByTestId('loading').textContent).toBe('false');
    rerender(<Probe ticker="A" fetcher={async (t) => `data:${t}`} />);
  });

  it('does NOT commit a slow response for the previous entity', async () => {
    // The bug: switch from A to B while A is still in flight, then let A
    // resolve. Its data must be discarded, not painted as B's.
    const a = deferred();
    const b = deferred();
    const fetcher = vi.fn((t: string) => (t === 'A' ? a.promise : b.promise));

    const { rerender } = render(<Probe ticker="A" fetcher={fetcher} />);
    rerender(<Probe ticker="B" fetcher={fetcher} />);

    await act(async () => { a.resolve('data:A'); });
    expect(screen.getByTestId('data').textContent).not.toBe('data:A');

    await act(async () => { b.resolve('data:B'); });
    await waitFor(() => expect(screen.getByTestId('data').textContent).toBe('data:B'));
  });

  it('fetches the new entity immediately, not after a full interval', async () => {
    // The second bug: an uncleared in-flight latch made the new entity's first
    // fetch return early, so the wrong data stayed on screen for a whole poll.
    const a = deferred();
    const fetcher = vi.fn((t: string) => (t === 'A' ? a.promise : Promise.resolve(`data:${t}`)));

    const { rerender } = render(<Probe ticker="A" fetcher={fetcher} interval={60_000} />);
    expect(fetcher).toHaveBeenCalledTimes(1);

    rerender(<Probe ticker="B" fetcher={fetcher} interval={60_000} />);
    // Without advancing any timer, B must already have been requested.
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher).toHaveBeenLastCalledWith('B');
  });

  it('clears the previous entity\'s data while the new one loads', async () => {
    // Otherwise a stale row is briefly readable as belonging to the new
    // market, which is the same lie as committing it.
    const b = deferred();
    const fetcher = (t: string) => (t === 'A' ? Promise.resolve('data:A') : b.promise);

    const { rerender } = render(<Probe ticker="A" fetcher={fetcher} />);
    await waitFor(() => expect(screen.getByTestId('data').textContent).toBe('data:A'));

    rerender(<Probe ticker="B" fetcher={fetcher} />);
    expect(screen.getByTestId('data').textContent).toBe('null');
    await act(async () => { b.resolve('data:B'); });
  });

  it('keeps the last good data when a poll fails, and says so', async () => {
    // Stale-and-labelled beats empty-and-unexplained.
    let call = 0;
    const fetcher = async () => {
      call += 1;
      if (call === 1) return 'good';
      throw new Error('provider parked');
    };
    render(<Probe ticker="A" fetcher={fetcher} interval={1000} />);
    await waitFor(() => expect(screen.getByTestId('data').textContent).toBe('good'));

    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    await waitFor(() => expect(screen.getByTestId('error').textContent).toBe('provider parked'));
    expect(screen.getByTestId('data').textContent).toBe('good');
  });

  it('clears a previous error once a poll succeeds again', async () => {
    let call = 0;
    const fetcher = async () => {
      call += 1;
      if (call === 1) throw new Error('boom');
      return 'recovered';
    };
    render(<Probe ticker="A" fetcher={fetcher} interval={1000} />);
    await waitFor(() => expect(screen.getByTestId('error').textContent).toBe('boom'));
    await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
    await waitFor(() => expect(screen.getByTestId('error').textContent).toBe('none'));
  });

  it('polls on the interval', async () => {
    const fetcher = vi.fn(async (t: string) => `data:${t}`);
    render(<Probe ticker="A" fetcher={fetcher} interval={1000} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(4);
  });

  it('stops polling after unmount', async () => {
    const fetcher = vi.fn(async (t: string) => `data:${t}`);
    const { unmount } = render(<Probe ticker="A" fetcher={fetcher} interval={500} />);
    await waitFor(() => expect(fetcher).toHaveBeenCalled());
    unmount();
    const seen = fetcher.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(fetcher.mock.calls.length).toBe(seen);
  });

  it('does not fetch at all when disabled', async () => {
    const fetcher = vi.fn(async () => 'x');
    render(<Probe ticker="A" fetcher={fetcher} interval={500} enabled={false} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(fetcher).not.toHaveBeenCalled();
    expect(screen.getByTestId('loading').textContent).toBe('false');
  });

  it('does not stack requests when the interval is shorter than the fetch', async () => {
    // A slow backend must not accumulate a queue of in-flight polls.
    const pending: Array<(v: string) => void> = [];
    const fetcher = vi.fn(() => new Promise<string>((res) => pending.push(res)));
    render(<Probe ticker="A" fetcher={fetcher} interval={100} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    // One outstanding request at a time, not ten.
    expect(fetcher.mock.calls.length).toBeLessThanOrEqual(2);
  });
});
