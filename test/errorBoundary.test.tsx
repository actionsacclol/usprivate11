import { Suspense, useState } from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ErrorBoundary, PageCrash } from '../src/components/ErrorBoundary';
import { guarded } from '../src/hub/views';

/**
 * A page that throws on mount must not unmount the whole window (the TopBar's
 * Pause and every engine switch with it). Pinned here: the boundary contains
 * a crash, shows the error with a way out, can be retried, reports through
 * onError; and a 3D scene that can't run says so without taking the page
 * with it. (The page-persistence half, PageGuard + lastPage, is the shell's
 * and is tested with it.)
 */

function Bomb({ message = 'scene exploded' }: { message?: string }): JSX.Element {
  throw new Error(message);
}

function Shell({ onError, onHome }: { onError?: (e: Error) => void; onHome?: () => void }) {
  const [broken, setBroken] = useState(true);
  return (
    <div>
      <div data-testid="chrome">Pause</div>
      <button onClick={() => setBroken(false)}>fix</button>
      <ErrorBoundary
        onError={onError}
        fallback={(error, reset) => (
          <PageCrash error={error} homeLabel="Dashboard" onHome={onHome ?? (() => {})} onRetry={reset} />
        )}
      >
        {broken ? <Bomb /> : <div data-testid="page">page</div>}
      </ErrorBoundary>
    </div>
  );
}

beforeEach(() => {
  // React logs every caught error; the boundary logs it again. Both are
  // expected here and would only bury real failures.
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('a page that crashes', () => {
  it('is contained, shows the error and leaves the chrome alive', () => {
    render(<Shell />);
    expect(screen.getByRole('alert').textContent).toContain('This page crashed');
    expect(screen.getByRole('alert').textContent).toContain('scene exploded');
    expect(screen.getByTestId('chrome')).toBeTruthy();
  });

  it('reports the error once through onError', () => {
    const onError = vi.fn();
    render(<Shell onError={onError} />);
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toBeInstanceOf(Error);
  });

  it('offers a way home', () => {
    const onHome = vi.fn();
    render(<Shell onHome={onHome} />);
    fireEvent.click(screen.getByText('Go to Dashboard'));
    expect(onHome).toHaveBeenCalledTimes(1);
  });

  it('Try again re-renders the page once the cause is gone', () => {
    render(<Shell />);
    fireEvent.click(screen.getByText('fix'));
    fireEvent.click(screen.getByText('Try again'));
    expect(screen.getByTestId('page')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a throw that is not an Error still shows something', () => {
    function Weird(): JSX.Element {
      // eslint-disable-next-line no-throw-literal
      throw 'plain string';
    }
    render(
      <ErrorBoundary fallback={(e) => <div role="alert">{e.message}</div>}>
        <Weird />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('alert').textContent).toBe('plain string');
  });
});

describe('a 3D scene that cannot run', () => {
  it('says "3D view unavailable" when the scene throws (WebGL refused)', async () => {
    const Scene = guarded(async () => ({ default: () => <Bomb message="WebGL context could not be created" /> }));
    render(
      <div>
        <Suspense fallback={<div>Loading…</div>}><Scene /></Suspense>
        <div data-testid="pots">pots</div>
      </div>,
    );
    await act(async () => { await Promise.resolve(); });
    expect(await screen.findByText('3D view unavailable')).toBeTruthy();
    expect(screen.getByText(/WebGL context could not be created/)).toBeTruthy();
    expect(screen.getByTestId('pots')).toBeTruthy();
  });

  it('says so when its chunk fails to load', async () => {
    const Scene = guarded(() => Promise.reject(new Error('Failed to fetch dynamically imported module')));
    render(<Suspense fallback={<div>Loading…</div>}><Scene /></Suspense>);
    expect(await screen.findByText('3D view unavailable')).toBeTruthy();
    expect(screen.getByText(/Failed to fetch/)).toBeTruthy();
  });
});
