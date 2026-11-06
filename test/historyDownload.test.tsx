// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, act } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { HistoryDownloadStatus } from '@shared/types';
import { HistoryDownload } from '../src/components/HistoryDownload';
import { TooFewTrades, tooFewTrades } from '../src/components/TooFewTrades';
import { fmtDuration, historyEstimate, historyWindows } from '../src/utils/historyDownload';

const idle: HistoryDownloadStatus = {
  status: 'idle', total: 0, done: 0, saved: 0, unavailable: 0, days: null, intervals: [], message: null, etaSec: null,
};

let trading: Record<string, ReturnType<typeof vi.fn>>;
const flush = async (): Promise<void> => { await act(async () => { await Promise.resolve(); }); };

beforeEach(() => {
  trading = {
    historyStatus: vi.fn(async () => idle),
    historyStart: vi.fn(async () => ({ ...idle, status: 'running', total: 3360, done: 0, etaSec: 840 })),
    historyCancel: vi.fn(async () => ({ ...idle, status: 'cancelled' })),
  };
  (window as unknown as { krypt: unknown }).krypt = { trading };
});
afterEach(cleanup);

describe('the estimate', () => {
  it('counts 4 coins, and matches what was measured for a week of 15m and hourly', () => {
    expect(historyWindows(7, ['15m', 'hourly'])).toBe(3360);
    expect(historyEstimate(7, ['15m', 'hourly'])).toBe('14 min');
  });

  it('caps 5-minute markets at what the venue keeps', () => {
    expect(historyWindows(40, ['5m'])).toBe(13 * 288 * 4);
  });

  it('reads as minutes, then hours', () => {
    expect(fmtDuration(10)).toBe('1 min');
    expect(fmtDuration(4800)).toBe('1 h 20 min');
    expect(fmtDuration(3600)).toBe('1 h');
  });
});

describe('the Backtest card', () => {
  it('starts what was picked and shows progress', async () => {
    render(<HistoryDownload />);
    await flush();
    expect(screen.getByTestId('history-start').textContent).toContain('about 14 min');
    fireEvent.click(screen.getByTestId('history-days-14'));
    expect(screen.getByTestId('history-start').textContent).toContain('about 28 min');
    fireEvent.click(screen.getByTestId('history-5m'));
    trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 3360, done: 0, etaSec: 840 });
    fireEvent.click(screen.getByTestId('history-start'));
    await flush();
    expect(trading.historyStart).toHaveBeenCalledWith({ days: 14, intervals: ['5m', '15m', 'hourly'] });
    expect(screen.getByTestId('history-progress').textContent).toBe('0 of 3,360 markets · about 14 min left');
    expect(screen.queryByTestId('history-start')).toBeNull();
  });

  it('picks up a download already running, and can stop it', async () => {
    trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 100, done: 40, etaSec: 15 });
    render(<HistoryDownload />);
    await flush();
    expect(screen.getByTestId('history-progress').textContent).toContain('40 of 100');
    trading.historyStatus.mockResolvedValue({ ...idle, status: 'cancelled' });
    fireEvent.click(screen.getByTestId('history-stop'));
    await flush();
    expect(trading.historyCancel).toHaveBeenCalled();
    expect(screen.getByText(/What was already saved stays/)).toBeTruthy();
  });

  it('polls while running, and tells the page its counts changed', async () => {
    vi.useFakeTimers();
    try {
      trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 100, done: 40, etaSec: 15 });
      const onChange = vi.fn();
      render(<HistoryDownload onChange={onChange} />);
      await flush();
      trading.historyStatus.mockClear();
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(trading.historyStatus).toHaveBeenCalledTimes(1);
      expect(onChange).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('counts once more when it finishes, for the last rows', async () => {
    vi.useFakeTimers();
    try {
      trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 100, done: 99, etaSec: 1 });
      const onChange = vi.fn();
      render(<HistoryDownload onChange={onChange} />);
      await flush();
      onChange.mockClear();
      trading.historyStatus.mockResolvedValue({ ...idle, status: 'done', total: 100, done: 100, saved: 100 });
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(onChange).toHaveBeenCalledTimes(1);
      expect(screen.getByTestId('history-done')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('says what happened at the end, and shows a refusal as written', async () => {
    trading.historyStatus.mockResolvedValue({ ...idle, status: 'done', saved: 3300, unavailable: 60, message: 'Polymarket US has nothing older.' });
    const r = render(<HistoryDownload />);
    await flush();
    expect(screen.getByTestId('history-done').textContent).toBe("Done: 3,300 markets saved. 60 weren't available. Polymarket US has nothing older.");
    r.unmount();
    trading.historyStatus.mockResolvedValue({ ...idle, status: 'error', message: 'Offline mode is on in Settings, so nothing can be downloaded.' });
    render(<HistoryDownload />);
    await flush();
    expect(screen.getByText(/Offline mode is on/)).toBeTruthy();
  });

  it('opened before the engine is up, it keeps asking and finds the resumed download', async () => {
    vi.useFakeTimers();
    try {
      trading.historyStatus.mockResolvedValue(null);
      render(<HistoryDownload />);
      await flush();
      expect(screen.getByTestId('history-start')).toBeTruthy();
      trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 100, done: 40, etaSec: 15 });
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(screen.getByTestId('history-progress').textContent).toContain('40 of 100');
    } finally {
      vi.useRealTimers();
    }
  });

  it('an idle card still notices a download that starts later', async () => {
    vi.useFakeTimers();
    try {
      render(<HistoryDownload />);
      await flush();
      trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 100, done: 1, etaSec: 50 });
      await act(async () => { await vi.advanceTimersByTimeAsync(10100); });
      expect(screen.getByTestId('history-progress')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it('an engine that stops answering does not wipe what the card knows', async () => {
    vi.useFakeTimers();
    try {
      trading.historyStatus.mockResolvedValue({ ...idle, status: 'running', total: 100, done: 40, etaSec: 15 });
      render(<HistoryDownload />);
      await flush();
      trading.historyStatus.mockResolvedValue(null);
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
      expect(screen.getByTestId('history-progress').textContent).toContain('40 of 100');
    } finally {
      vi.useRealTimers();
    }
  });

  it('survives a backend that is down', async () => {
    trading.historyStatus.mockRejectedValue(new Error('down'));
    trading.historyStart.mockRejectedValue(new Error('down'));
    render(<HistoryDownload />);
    await flush();
    fireEvent.click(screen.getByTestId('history-start'));
    await flush();
    expect(screen.getByTestId('history-start')).toBeTruthy();
  });
});

describe('too few trades', () => {
  it('warns on a small sample, and not on none or plenty', () => {
    expect([tooFewTrades(0), tooFewTrades(1), tooFewTrades(29), tooFewTrades(30)]).toEqual([false, true, true, false]);
    const r = render(<TooFewTrades n={5} />);
    expect(screen.getByTestId('too-few-trades').textContent).toContain('Only 5 trades');
    r.rerender(<TooFewTrades n={1} />);
    expect(screen.getByTestId('too-few-trades').textContent).toContain('Only 1 trade:');
    r.rerender(<TooFewTrades n={30} />);
    expect(screen.queryByTestId('too-few-trades')).toBeNull();
    r.rerender(<TooFewTrades n={0} />);
    expect(screen.queryByTestId('too-few-trades')).toBeNull();
  });
});
