import { useEffect, useState } from 'react';
import {
  ArrowRight, Bot, FlaskConical, KeyRound, MousePointerClick, ShieldAlert, Target, X,
} from 'lucide-react';
import type { PageId } from '../App';
import { ConnectAgent } from '../components/ConnectAgent';
import { AutopilotQuickstart } from '../components/AutopilotQuickstart';
import { Badge, ChoiceCard } from '../components/SetupSteps';
import { useToast } from '../state/ToastProvider';
import { useApp } from '../state/AppStateProvider';
import { KryptSprite } from '../components/KryptSprite';
import { AccountSetup } from '../components/AccountSetup';
import { GlassPanel } from '../components/glass/GlassPanel';
import { GlassButton } from '../components/glass/GlassButton';
import { GLASS_TINTS } from '../components/glass/glassPresets';
import { ONBOARDING_HISTORY, startHistoryDownload } from '../utils/historyDownload';
import { YouTubeGuide } from '../components/YouTubeGuide';

// Steps, in order. The disclaimer comes straight after the welcome: the
// agent step's buttons switch the local agent server on, and that must never
// be possible BEFORE the risks are accepted. The agent step is skippable,
// because a user who wants only the terminal or the engines owes this app
// nothing about AI. The account step comes before the agent step on purpose: an
// invite only counts at sign-up, so it has to be offered before anything else, and
// it can be left at any point. Then the goal, which decides where they land and whether
// the agent step shows at all. Then the start choice: "Start on paper" (the default, and
// the whole app with no API key at all) or setting up an account — which is only
// the step BEFORE going live; it never switches the app to real money. The
// Autopilot setup is only there for someone who picked "In-app Autopilot".
type Step = 'welcome' | 'disclaimer' | 'account' | 'goal' | 'agent' | 'start' | 'autopilot';
type Goal = 'ai' | 'strategy' | 'hand';

/** Where each goal lands. Manual trading starts on Discover, where markets are found. */
const GOAL_PAGE: Record<Goal, PageId> = { ai: 'agents', strategy: 'strategies', hand: 'discover' };

// Every write this flow can make, and what a replay (or the once-after-update
// run) does with it. On a replay nothing is written on arrival or on Continue;
// each of these needs the same explicit act it needs on first run.
//  - disclaimer: `state.acceptDisclaimer`. First run only, and only with both
//    boxes ticked. A replay shows it as already accepted and read-only.
//  - account step (AccountSetup): saves an API key only on its own Save button.
//    Never leaves Paper. Its links open in the person's browser.
//  - goal step: writes nothing. It only picks the next step and the landing page.
//  - agent step (ConnectAgent with `paperDefault`): on the user's own click it
//    may switch the agent server on (`mcpEnabled`) and set agent trading to
//    paper from off. Never to live, and never from live.
//  - Start choice: "Start on paper" writes nothing on first run (a fresh
//    install already is on Paper); on a replay while Live it switches the
//    account back to Paper, which can only stop real orders. "Set up
//    account" writes nothing: it closes onboarding on the API Key page, and the
//    account still stays on Paper until Settings → Paper | Live.
//    The history option next to it starts a download of public prices when either
//    button is pressed. Ticked on first run, and unticked on a replay, where it needs
//    the same explicit act as everything else.
//  - Autopilot quickstart: writes only on its own explicit buttons.
export function OnboardingModal({ onDone, replay = false, updated = false, onClose }: {
  /** Where to land: AI Agents (the front door) or the API Key page. */
  onDone: (page: PageId) => void;
  /** Not first run: informational, closable. */
  replay?: boolean;
  /** A replay that opened by itself after an update (GuideHost): same rules,
   *  it just says why it is here. */
  updated?: boolean;
  /** Replay only: the close button and Escape. */
  onClose?: () => void;
}) {
  const [step, setStep] = useState(0);
  const [accepted, setAccepted] = useState(false);
  const [eligible, setEligible] = useState(false);
  const [wantAutopilot, setWantAutopilot] = useState(false);
  // "Set up account" picked: finish on the API Key page instead.
  const [keyNext, setKeyNext] = useState(false);
  const [goal, setGoal] = useState<Goal>('ai');
  const [wantHistory, setWantHistory] = useState(!replay);
  const toast = useToast();
  const { state, config } = useApp();
  // Replayed after acceptance, the disclaimer is something to re-read.
  const alreadyAccepted = replay && !!state?.acceptedDisclaimer;
  const steps: Step[] = [
    'welcome', 'disclaimer', 'account', 'goal',
    ...(goal === 'ai' ? ['agent' as const] : []),
    'start',
    ...(goal === 'ai' && wantAutopilot ? ['autopilot' as const] : []),
  ];
  const at = steps[step];
  const last = steps.length - 1;
  const next = (): void => setStep((s) => Math.min(s + 1, last));
  const back = (): void => setStep((s) => Math.max(0, s - 1));

  // The disclaimer is recorded the moment it is accepted, not at the end:
  // the steps after it are optional, and someone who closes the app halfway
  // through them has still accepted it. GuideHost keeps this modal open until
  // onDone, so recording it early does not close it.
  const acceptAndContinue = async (): Promise<void> => {
    if (alreadyAccepted) { next(); return; }
    if (!accepted || !eligible) {
      toast.warn('Please tick both boxes to continue');
      return;
    }
    await window.krypt.state.acceptDisclaimer();
    next();
  };

  const finish = (toKey = keyNext): void => {
    if (!replay) toast.success('Welcome to Krypt PolyBot US');
    onDone(toKey ? 'api' : GOAL_PAGE[goal]);
  };

  // Last step done (or skipped): finish; otherwise on to the next one.
  const advanceOrFinish = (toKey = keyNext): void => {
    if (step >= last) finish(toKey); else next();
  };
  const embedded = at === 'account' || at === 'start' || at === 'autopilot';
  const offline = !!config?.terminalOfflineMode;

  const downloadIfWanted = (): void => {
    if (wantHistory && !offline) {
      void startHistoryDownload(ONBOARDING_HISTORY.days, ONBOARDING_HISTORY.intervals);
    }
  };

  // "Start on paper". The app already is in Paper unless a replay found it
  // Live; then this is the user's own act and it can only narrow.
  const startOnPaper = async (): Promise<void> => {
    setKeyNext(false);
    downloadIfWanted();
    if (config?.accountMode === 'live') {
      await window.krypt.config.update({ accountMode: 'paper' });
      toast.success('Back on Paper. Nothing can send a real order now.');
    }
    advanceOrFinish(false);
  };

  // "Set up account": nothing is written here; the API Key page does it, and
  // the account stays on Paper until the user goes Live in Settings.
  const addKey = (): void => {
    setKeyNext(true);
    downloadIfWanted();
    advanceOrFinish(true);
  };

  // A replay can be left at any point; first run cannot (the disclaimer has
  // to be answered once).
  useEffect(() => {
    if (!replay || !onClose) return undefined;
    const onKey = (e: KeyboardEvent): void => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [replay, onClose]);

  const bankroll = config?.terminalPaperBankrollUsd ?? 1000;

  return (
    // No backdrop-blur on the overlay: a backdrop-filter there would become
    // the glass panel's backdrop root, and the panel would refract a flat
    // dim instead of the app behind it.
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/55">
      <GlassPanel
        preset="modal"
        tint={GLASS_TINTS.modal}
        // display through the prop, not a `flex` class: GlassPanel sets
        // display inline, which beats the class, and then the middle never
        // scrolls and a short window clips the footer with Continue in it.
        display="flex"
        className="glass-pop relative max-h-[94vh] w-[680px] max-w-[94vw] flex-col overflow-hidden rounded-3xl"
        style={{ boxShadow: '0 0 80px -20px rgba(59,130,246,0.55), var(--glass-shadow)' }}
        role="dialog"
        aria-modal="true"
        aria-label={replay ? 'Onboarding (replay)' : 'Onboarding'}
      >
        {replay && onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close onboarding"
            title="Close (nothing changes)"
            className="absolute right-3 top-3 z-10 grid h-7 w-7 place-items-center rounded-lg text-krypt-muted transition-colors hover:bg-white/[0.08] hover:text-white"
          >
            <X className="h-4 w-4" />
          </button>
        )}
        <div className="shrink-0 bg-gradient-to-r from-krypt-indigo/70 via-krypt-purple/70 to-krypt-pink/70 pb-px">
          {at === 'welcome' ? (
            <div className="bg-[linear-gradient(180deg,rgba(24,30,64,0.85),rgba(12,14,28,0.9))] px-8 py-6 text-center">
              {/* Bluey on a lit glass disc: the app's own mascot, not a
                  letter tile. KryptSprite is on the ambient clock (15 Hz). */}
              <div className="relative mx-auto mb-3 grid h-20 w-20 place-items-center">
                <div aria-hidden className="absolute inset-0 rounded-full bg-[radial-gradient(circle_at_50%_40%,rgba(96,165,250,0.45),rgba(59,130,246,0.12)_55%,transparent_72%)]" />
                <div aria-hidden className="absolute inset-2 rounded-full border border-white/15 bg-white/[0.04] shadow-[inset_0_1px_0_rgba(255,255,255,0.25),0_10px_30px_-8px_rgba(59,130,246,0.7)]" />
                <KryptSprite size={56} className="relative" title="Bluey" />
              </div>
              <h2 className="font-pixel text-base tracking-wider">KRYPT POLYBOT</h2>
              <p className="mt-1 text-xs text-krypt-muted">
                Your AI agent on Polymarket US: inside rails, scored against the market.
              </p>
              {replay && (
                <p className="mx-auto mt-2 w-fit rounded-full border border-krypt-purple/40 bg-krypt-purple/10 px-2.5 py-0.5 text-[10px] uppercase tracking-wider text-krypt-purple">
                  {updated ? 'New in this version' : 'Replay'} · nothing changes unless you act in a step
                </p>
              )}
              {/* The installer stops the agent relays that Claude Desktop,
                  Cursor and Codex keep running, so their connection is gone
                  until they are restarted. */}
              {updated && (
                <p className="mx-auto mt-2 max-w-md text-[11px] leading-relaxed text-krypt-warn" data-testid="onboarding-reconnect-note">
                  Using Krypt PolyBot US from Claude Desktop, Cursor or Codex? The update closed their
                  connection. Fully quit and reopen them (Claude Desktop: right-click its tray icon → Quit)
                  so they reconnect.
                </p>
              )}
            </div>
          ) : (
            // Later steps get the room: a slim bar instead of the hero.
            <div className="flex items-center gap-3 bg-[linear-gradient(180deg,rgba(24,30,64,0.85),rgba(12,14,28,0.9))] px-8 py-3">
              <div className="h-[18px] w-[18px] rounded-md bg-gradient-to-br from-sky-400 via-blue-500 to-blue-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.45),0_0_14px_-2px_rgba(59,130,246,0.85)]" />
              <h2 className="font-pixel text-xs tracking-wider">KRYPT POLYBOT</h2>
              {replay && (
                <span className="rounded-full border border-krypt-purple/40 bg-krypt-purple/10 px-2 py-0.5 text-[9px] uppercase tracking-wider text-krypt-purple">
                  {updated ? 'New' : 'Replay'}
                </span>
              )}
              <span className={replay ? 'ml-auto mr-8 text-[11px] text-krypt-muted' : 'ml-auto text-[11px] text-krypt-muted'}>
                {at === 'agent' ? 'Your AI agent' : at === 'disclaimer' ? 'Before you trade'
                  : at === 'account' ? 'Polymarket US account'
                  : at === 'goal' ? 'What you want to do'
                  : at === 'start' ? 'Paper or API key' : 'Autopilot'}
              </span>
            </div>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-8 py-6">
          {at === 'welcome' && (
            <div className="space-y-4 text-sm text-white/90">
              {/* The owner's walk-through, first thing on the first screen. It opens
                  in the browser (YouTubeGuide.tsx has why not in place). The short
                  bundled videos are on the Guide page. */}
              <YouTubeGuide />
              <p>
                Connect the AI agent you already use —{' '}
                <span className="text-white">Claude Desktop, Claude Code, Cursor, Codex, Windsurf or
                VS Code</span> — and it can read every Polymarket US market, commit to a forecast, and
                trade <span className="text-white">on paper</span> against the real order book. Every
                forecast is scored against the market price once the market resolves, so you find out
                whether it beats the market before a dollar is at risk.
              </p>
              <p className="text-krypt-muted">
                The <span className="text-white">terminal</span> (Discover, Market, Track) is the manual
                side, and the crypto and script <span className="text-white">engines</span> are here too. Everything
                runs locally on your machine — your API key never leaves it.
              </p>
              <ul className="grid grid-cols-2 gap-3 text-xs">
                <Feature title="AI agents (MCP)" body="Your agent, on a local server only it can reach." />
                <Feature title="Forecast first" body="No buy without a forecast that beats the price after fees." />
                <Feature title="Paper by default" body="Real books, imaginary money. Live is its own switch." />
                <Feature title="Forecast scoreboard" body="Brier vs the market, per agent." />
                <Feature title="Crypto engine" body="5m, 15m and 1h Up/Down markets, with its own switch." />
                <Feature title="Strategy scripts" body="Write your own, with money rails." />
              </ul>

            </div>
          )}

          {at === 'disclaimer' && (
            <div className="space-y-4 text-sm">
              <div className="flex items-start gap-3 rounded-lg border border-krypt-loss/30 bg-krypt-loss/5 p-3 text-krypt-loss">
                <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" />
                <div className="text-xs leading-relaxed">
                  <strong>Disclaimer.</strong> Krypt PolyBot US is provided as-is, free.
                  Auto-trading involves risk; all P&amp;L is your own. We make no
                  guarantee of profitability. You are solely responsible for
                  compliance with Polymarket US&apos;s terms and rules and with applicable
                  law. An AI agent can be wrong with total
                  confidence. Once the app is Live, the engines and any agent you set
                  live trade real money — start with a small balance you can afford to lose.
                </div>
              </div>
              <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                <input
                  type="checkbox"
                  checked={alreadyAccepted || accepted}
                  disabled={alreadyAccepted}
                  onChange={(e) => setAccepted(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-krypt-purple"
                  data-testid="disclaimer-accept"
                />
                <span className="text-xs text-white/90">
                  I&apos;ve read the disclaimer and accept the risks of auto-trading.
                  {alreadyAccepted && (
                    <span className="mt-0.5 block text-krypt-muted" data-testid="disclaimer-accepted">
                      You accepted this when you set up the app. It stays accepted; it is here to re-read.
                    </span>
                  )}
                </span>
              </label>
              <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                <input
                  type="checkbox"
                  checked={alreadyAccepted || eligible}
                  disabled={alreadyAccepted}
                  onChange={(e) => setEligible(e.target.checked)}
                  className="mt-0.5 h-4 w-4 accent-krypt-purple"
                  data-testid="disclaimer-eligible"
                />
                <span className="text-xs text-white/90">
                  I confirm I am eligible for a Polymarket US account and accept full
                  responsibility for my trading.
                </span>
              </label>
              <div className="rounded-lg border border-krypt-warn/40 bg-krypt-warn/5 p-3 text-xs text-krypt-warn">
                You start in <strong>PAPER</strong> — Polymarket US&apos;s real order books, imaginary
                money, no API key needed. The terminal, your phone and your AI agents trade paper, and
                the engines (Crypto, live Scripts) send nothing. Real money needs a saved API key AND
                switching the app to Live in Settings; even then each engine has its own switch, off by
                default, and turning one on never turns on another.
              </div>
            </div>
          )}

          {at === 'account' && <AccountSetup onDone={next} onBackOut={back} />}

          {at === 'goal' && (
            <div className="space-y-3 text-sm text-white/90" data-testid="onboarding-goal">
              <h3 className="text-base font-semibold">What do you want to do?</h3>
              <ChoiceCard
                testId="goal-ai" selected={goal === 'ai'} onClick={() => setGoal('ai')}
                title={<><Bot className="mr-1 inline h-4 w-4" />Let an AI trade for me</>}
              >
                Connect Claude, Cursor or Codex, or let the app run an AI itself. It starts on
                imaginary money.
              </ChoiceCard>
              <ChoiceCard
                testId="goal-strategy" selected={goal === 'strategy'} onClick={() => { setGoal('strategy'); setWantAutopilot(false); }}
                title={<><Target className="mr-1 inline h-4 w-4" />Try a ready-made strategy</>}
              >
                Pick one and see how it does. It runs on imaginary money until you decide otherwise.
              </ChoiceCard>
              <ChoiceCard
                testId="goal-hand" selected={goal === 'hand'} onClick={() => { setGoal('hand'); setWantAutopilot(false); }}
                title={<><MousePointerClick className="mr-1 inline h-4 w-4" />Trade myself</>}
              >
                Find a market, pick a side and place your own orders, at real prices with imaginary money.
              </ChoiceCard>
              <p className="text-[11px] text-krypt-dim">You can do any of these later. This only picks where you start.</p>
            </div>
          )}

          {at === 'agent' && (
            <div className="space-y-3 text-sm text-white/90">
              <h3 className="text-base font-semibold">Connect your AI agent</h3>
              <p className="text-xs leading-relaxed text-white/80">
                Don&apos;t use Claude, Cursor or Codex? Pick{' '}
                <span className="text-white">In-app Autopilot</span>: nothing to install — the app runs
                the agent itself with an AI you choose (including free ones on your own computer).
              </p>
              <p className="text-xs leading-relaxed text-krypt-muted">
                Pick the one you use. Connecting it switches on the local agent server, and if agent
                trading is off it sets it to <span className="text-white">paper</span>: real order books,
                imaginary money. Nothing here can turn on live trading; that is its own deliberate
                switch on the AI Agents page. The client&apos;s config holds no token.
              </p>
              <ConnectAgent
                paperDefault
                autopilotSetup="callback"
                onAutopilot={() => { setWantAutopilot(true); next(); }}
              />
              <p className="text-[11px] text-krypt-dim">
                Then ask it something like: &ldquo;Use krypt-polybot-us. Check get_status, look through
                markets closing soon, read the rules, and record honest forecasts.&rdquo; Not using an AI
                agent? Skip this: nothing is switched on unless you connect one.
              </p>
            </div>
          )}

          {at === 'start' && (
            <div className="space-y-3 text-sm text-white/90" data-testid="onboarding-start">
              <h3 className="text-base font-semibold">How do you want to start?</h3>
              <ChoiceCard
                testId="start-paper-card" selected={!keyNext} onClick={() => setKeyNext(false)}
                title={<><FlaskConical className="mr-1 inline h-4 w-4" />Start on paper (no API key needed)</>}
                badge={<Badge>Recommended</Badge>}
              >
                Polymarket US&apos;s real markets and order books with imaginary money — $
                {bankroll.toLocaleString()} to start. The terminal, your phone and your AI agents all
                work, and none of them can spend real money.
              </ChoiceCard>
              <ChoiceCard
                testId="add-key-card" selected={keyNext} onClick={() => setKeyNext(true)}
                title={<><KeyRound className="mr-1 inline h-4 w-4" />Set up my Polymarket US account</>}
              >
                Opens the API Key page, which takes you through it one step at a time and remembers
                your place. The app still starts on Paper; going Live is its own step in
                Settings → Paper | Live whenever you are ready.
              </ChoiceCard>
              {!offline && (
                <label className="flex items-start gap-3 rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
                  <input
                    type="checkbox"
                    checked={wantHistory}
                    onChange={(e) => setWantHistory(e.target.checked)}
                    className="mt-0.5 h-4 w-4 accent-krypt-purple"
                    data-testid="onboarding-history"
                  />
                  <span className="text-xs text-white/90">
                    Download the last {ONBOARDING_HISTORY.days} days of crypto prices, so Backtest works
                    straight away.
                    <span className="mt-0.5 block text-krypt-muted">
                      Free public data, no account needed, and nothing is traded. It runs in the background
                      and takes about 15 minutes.
                    </span>
                  </span>
                </label>
              )}
              <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                <button onClick={back} className="krypt-btn-ghost">Back</button>
                <div className="flex gap-2">
                  <button onClick={addKey} className="krypt-btn-default" data-testid="onboarding-add-key">
                    Set up account
                  </button>
                  <GlassButton variant="primary" onClick={() => void startOnPaper()} data-testid="onboarding-start-paper">
                    Start on paper <ArrowRight className="h-4 w-4" />
                  </GlassButton>
                </div>
              </div>
            </div>
          )}

          {at === 'autopilot' && (
            <AutopilotQuickstart
              onDone={() => finish()}
              onSkip={() => finish()}
              onBackOut={back}
            />
          )}
        </div>

        {/* The account guide has its own progress and buttons; a second row of
            dots under them only reads as a second thing to finish. */}
        {at !== 'account' && (
        <div className="flex shrink-0 items-center justify-between border-t border-krypt-border bg-krypt-surface2/50 px-8 py-4">
          <div className="flex gap-1" aria-label={`Onboarding step ${step + 1} of ${steps.length}`}>
            {steps.map((_, i) => (
              <span
                key={i}
                className={
                  i === step
                    ? 'h-1.5 w-6 rounded-full bg-krypt-purple'
                    : 'h-1.5 w-1.5 rounded-full bg-krypt-border'
                }
              />
            ))}
          </div>
          {/* The start and Autopilot steps carry their own back / skip /
              continue: two sets of buttons on one screen is one too many. */}
          {embedded ? (
            <span className="text-[11px] text-krypt-dim">
              {at === 'start' ? 'Paper or API key' : 'Autopilot setup'}
            </span>
          ) : (
            <div className="flex items-center gap-2">
              {step > 0 && (
                <button onClick={back} className="krypt-btn-ghost">
                  Back
                </button>
              )}
              {at === 'agent' && (
                <button onClick={next} className="krypt-btn-ghost">
                  Skip
                </button>
              )}
              <GlassButton
                variant="primary"
                data-testid="onboarding-continue"
                onClick={() => { if (at === 'disclaimer') void acceptAndContinue(); else next(); }}
              >
                Continue <ArrowRight className="h-4 w-4" />
              </GlassButton>
            </div>
          )}
        </div>
        )}
      </GlassPanel>
    </div>
  );
}

function Feature({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-lg border border-krypt-border bg-krypt-surface2 p-3">
      <div className="text-xs font-semibold text-white">{title}</div>
      <div className="mt-0.5 text-[11px] text-krypt-muted">{body}</div>
    </div>
  );
}
