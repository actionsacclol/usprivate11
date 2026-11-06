import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Compass } from 'lucide-react';
import type { PageId } from '../../App';
import { OnboardingModal } from '../../pages/Onboarding';
import { GlassPanel } from '../glass/GlassPanel';
import { GlassButton } from '../glass/GlassButton';
import { GLASS_TINTS } from '../glass/glassPresets';
import { Tour } from './Tour';
import { ErrorBoundary } from '../ErrorBoundary';
import { GuideContext, markTourOffered, wasTourOffered, type GuideApi } from './useTour';

/**
 * Owns the three "show me around" surfaces: onboarding (first run, after an
 * update, or a replay), the spotlight tour, and the one-time "take a tour?"
 * prompt.
 *
 * Onboarding is opened once when the disclaimer is unaccepted and closed only
 * by its own onDone. It records the disclaimer as soon as it is accepted,
 * mid-flow, with the agent, API key and Autopilot steps still to come; keying
 * the modal straight off acceptedDisclaimer would close it on them.
 *
 * A replay is UI state and nothing else: no setting, key, profile or the
 * disclaimer is touched to show it, and closing it does not move the user off
 * the page they were on (first run lands on AI Agents because a new user has
 * no page yet; a returning one does).
 *
 * After an update that changed onboarding (shared/onboarding.ts), it opens
 * once by itself in that same replay mode — an existing user has accepted the
 * disclaimer, so first run alone would never show them the agent setup. Any
 * close (done, X, Escape, a crash skip) records it seen, so it is once.
 */
export function GuideHost({
  needsOnboarding, needsUpdateOnboarding = false, setPage, children,
}: {
  needsOnboarding: boolean;
  /** Disclaimer accepted, but this onboarding revision not seen yet. */
  needsUpdateOnboarding?: boolean;
  setPage: (p: PageId) => void;
  children: ReactNode;
}) {
  const [onboarding, setOnboarding] = useState<'first' | 'update' | 'replay' | null>(null);
  const [tourOpen, setTourOpen] = useState(false);
  const [offer, setOffer] = useState(false);
  // Opened by itself at most once per launch: the "seen" write and its state
  // broadcast land after the close, and must not reopen it in between.
  const autoOpened = useRef(false);

  useEffect(() => {
    if (needsOnboarding) { autoOpened.current = true; setOnboarding('first'); return; }
    // Accepting the disclaimer mid first-run makes this true too; `m ?? ...`
    // leaves the open first run alone.
    if (needsUpdateOnboarding && !autoOpened.current) {
      autoOpened.current = true;
      setOnboarding((m) => m ?? 'update');
    }
  }, [needsOnboarding, needsUpdateOnboarding]);

  const markSeen = (): void => {
    try {
      void window.krypt.app.markOnboardingSeen?.().catch(() => { /* shows again next launch */ });
    } catch { /* an older preload: shows again next launch */ }
  };
  const closeOnboarding = (): void => { markSeen(); setOnboarding(null); };

  const startTour = useCallback((): void => {
    setOffer(false);
    setTourOpen(true);
  }, []);
  const replayOnboarding = useCallback((): void => {
    setTourOpen(false);
    setOffer(false);
    setOnboarding((m) => m ?? 'replay');
  }, []);
  const api = useMemo<GuideApi>(() => ({ startTour, replayOnboarding }), [startTour, replayOnboarding]);

  const onboardingDone = (page: PageId): void => {
    const mode = onboarding;
    closeOnboarding();
    // "Set up account" is the user's own pick of where to go next, so it
    // moves them on any mode; otherwise only first run moves them.
    if (page === 'api' || mode === 'first') setPage(page);
    if (mode === 'replay') return;
    // Offered once, after a first-run or post-update onboarding finishes —
    // never on a launch, never after a manual replay, and never again once
    // seen (Start or Not now alike).
    if (!wasTourOffered()) {
      markTourOffered();
      setOffer(true);
    }
  };

  return (
    <GuideContext.Provider value={api}>
      {children}
      {/* The onboarding modal and the tour render OUTSIDE the page guard (they
          sit over the whole window), so each gets its own boundary: on first
          run the modal reopens on every launch, and a throw in it must not
          take the window down each time. */}
      {onboarding && (
        <ErrorBoundary
          fallback={() => (
            <SetupCrash
              firstRun={onboarding === 'first'}
              onSkip={closeOnboarding}
            />
          )}
        >
          <OnboardingModal
            replay={onboarding !== 'first'}
            updated={onboarding === 'update'}
            onDone={onboardingDone}
            onClose={closeOnboarding}
          />
        </ErrorBoundary>
      )}
      {offer && !tourOpen && !onboarding && (
        <TourOffer onStart={startTour} onDismiss={() => setOffer(false)} />
      )}
      <ErrorBoundary
        key={tourOpen ? 'tour-open' : 'tour-closed'}
        fallback={() => null}
        onError={() => setTourOpen(false)}
      >
        <Tour open={tourOpen} onClose={() => setTourOpen(false)} onNavigate={setPage} />
      </ErrorBoundary>
    </GuideContext.Provider>
  );
}

/**
 * What setup shows if it crashes. First run still owes the user the
 * disclaimer (nothing else in onboarding is required), so the way out accepts
 * it in the same words; a replay just closes.
 */
export function SetupCrash({ firstRun, onSkip }: { firstRun: boolean; onSkip: () => void }) {
  const [busy, setBusy] = useState(false);
  const skip = async (): Promise<void> => {
    setBusy(true);
    try {
      if (firstRun) await window.krypt.state.acceptDisclaimer();
    } catch { /* the app still opens; onboarding returns next launch */ }
    onSkip();
  };
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" data-testid="setup-crash">
      <div role="alertdialog" aria-labelledby="setup-crash-title"
        className="w-[460px] max-w-full rounded-2xl border border-krypt-border bg-krypt-surface p-5 text-sm">
        <h2 id="setup-crash-title" className="text-base font-semibold text-white">Setup hit a problem</h2>
        <p className="mt-2 text-xs leading-relaxed text-krypt-muted">
          The setup screens couldn&apos;t open. You can skip them: the app starts on Paper (real
          order books, imaginary money) with every engine off, and you can take the tour later
          from the compass button in the sidebar.
        </p>
        {firstRun && (
          <p className="mt-2 text-xs leading-relaxed text-krypt-warn">
            By continuing you accept the disclaimer: Krypt PolyBot US is provided as-is; trading involves
            risk, all profit and loss is your own, and you are responsible for following
            Polymarket US&apos;s terms and rulebook and your local law.
          </p>
        )}
        <div className="mt-4 flex justify-end">
          <button type="button" className="krypt-btn-primary" disabled={busy} onClick={() => void skip()}
            data-testid="setup-crash-skip">
            {firstRun ? 'Accept and skip setup' : 'Skip setup'}
          </button>
        </div>
      </div>
    </div>
  );
}

/** The one-time prompt. Bottom-right and small: it is an offer, not another
 *  modal between a new user and the app they just set up. */
function TourOffer({ onStart, onDismiss }: { onStart: () => void; onDismiss: () => void }) {
  return (
    <div
      role="dialog"
      aria-labelledby="tour-offer-title"
      aria-describedby="tour-offer-body"
      data-testid="tour-offer"
      className="fixed bottom-5 right-5 z-[60] w-[300px] max-w-[calc(100vw-24px)]"
    >
      <GlassPanel
        preset="card"
        tint={GLASS_TINTS.modal}
        className="glass-pop rounded-2xl border border-white/[0.08] p-4 motion-reduce:[animation:none]"
        style={{ boxShadow: '0 0 60px -18px rgba(59,130,246,0.6), var(--glass-shadow)' }}
      >
        <div className="flex items-start gap-3">
          <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-krypt-glow shadow-krypt-soft">
            <Compass className="h-4 w-4 text-white" />
          </div>
          <div>
            <h2 id="tour-offer-title" className="text-sm font-semibold text-white">
              Take a 1-minute tour of the app?
            </h2>
            <p id="tour-offer-body" className="mt-0.5 text-xs leading-relaxed text-krypt-muted">
              It points at each tab and says what it is for. You can start it later from the compass
              button at the top of the sidebar.
            </p>
          </div>
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" onClick={onDismiss} className="krypt-btn-ghost px-2.5 text-xs">
            Not now
          </button>
          <GlassButton variant="primary" onClick={onStart} className="px-3 py-1.5 text-xs" autoFocus>
            Start
          </GlassButton>
        </div>
      </GlassPanel>
    </div>
  );
}
