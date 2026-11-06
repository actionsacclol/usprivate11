import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { ErrorBoundary, PageCrash } from './ErrorBoundary';
import type { PageId } from '../App';
import { FRONT_DOOR, forgetLastPage, loadLastPage, saveLastPage } from '../state/lastPage';

/**
 * The current page, remembered across launches, unless it crashed.
 *
 * The ordering matters: a page that throws while rendering is caught by its
 * boundary in the commit phase (`onCrash`), and only AFTER that does this
 * commit's passive effect run to persist `page`. Without the `crashed` check
 * the effect would write the crashed page straight back, and the next launch
 * would open on it again.
 */
export function usePersistedPage(): [PageId, (p: PageId) => void, (p: PageId) => void] {
  const [page, setPage] = useState<PageId>(loadLastPage);
  const crashed = useRef<PageId | null>(null);
  useEffect(() => {
    if (crashed.current === page) return;
    crashed.current = null;
    saveLastPage(page);
  }, [page]);
  const onCrash = useCallback((p: PageId) => {
    crashed.current = p;
    forgetLastPage();
  }, []);
  return [page, setPage, onCrash];
}

/** One page inside its own error boundary. Keyed on the page, so navigating
 *  away is also how a crashed page is left behind. */
export function PageGuard({
  page, setPage, onCrash, children,
}: {
  page: PageId;
  setPage: (p: PageId) => void;
  onCrash: (p: PageId) => void;
  children: ReactNode;
}) {
  // Home is the front door (AI Agents), unless that is what crashed.
  const home: PageId = page === FRONT_DOOR ? 'dashboard' : FRONT_DOOR;
  return (
    <ErrorBoundary
      key={page}
      onError={() => onCrash(page)}
      fallback={(error, reset) => (
        <PageCrash
          error={error}
          homeLabel={home === 'dashboard' ? 'Dashboard' : 'AI Agents'}
          onHome={() => setPage(home)}
          onRetry={reset}
        />
      )}
    >
      {children}
    </ErrorBoundary>
  );
}
