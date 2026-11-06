import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TraderConfig } from '@shared/types';
import { autoOnboarding, ONBOARDING_REVISION } from '@shared/onboarding';
import { GuideHost } from '../src/components/tour/GuideHost';
import { TOUR_OFFERED_KEY, useGuide } from '../src/components/tour/useTour';

/**
 * Onboarding: first run, once after an update, and a replay.
 *
 * It used to open only while the disclaimer was unaccepted, so nobody
 * upgrading would ever see the agent setup. Now a saved onboarding revision
 * below the current one opens it once, as a replay: closable, the disclaimer
 * read-only, nothing written but the "seen" mark. And a replay is UI state:
 * stepping through it writes only what the user explicitly does in a step.
 */

let userData = '';
vi.mock('electron', () => ({ app: { getPath: () => userData } }));

const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn(), push: vi.fn() }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({ useToast: () => toast }));
// ConnectAgent and the Autopilot quickstart write only on their own explicit
// buttons (their own tests say so); here they are stand-ins that expose the
// onboarding's paths through them.
vi.mock('../src/components/ConnectAgent', () => ({
  ConnectAgent: ({ onAutopilot }: { onAutopilot: () => void }) => (
    <button type="button" onClick={onAutopilot}>pick-autopilot</button>
  ),
}));
vi.mock('../src/components/AutopilotQuickstart', () => ({
  AutopilotQuickstart: ({ onSkip }: { onSkip?: () => void }) => (
    <button type="button" onClick={onSkip}>quickstart-skip</button>
  ),
}));

describe('autoOnboarding', () => {
  it('first run, after an update, or nothing', () => {
    expect(autoOnboarding(null)).toBeNull();
    expect(autoOnboarding({ acceptedDisclaimer: false })).toBe('first');
    expect(autoOnboarding({ acceptedDisclaimer: false, onboardingSeen: ONBOARDING_REVISION })).toBe('first');
    // A pre-3.0 settings.json has no field at all.
    expect(autoOnboarding({ acceptedDisclaimer: true })).toBe('update');
    expect(autoOnboarding({ acceptedDisclaimer: true, onboardingSeen: ONBOARDING_REVISION - 1 })).toBe('update');
    expect(autoOnboarding({ acceptedDisclaimer: true, onboardingSeen: ONBOARDING_REVISION })).toBeNull();
  });
});

describe('the saved revision', () => {
  beforeEach(() => {
    userData = mkdtempSync(join(tmpdir(), 'krypt-onb-'));
    // The store mirrors settings to a vault outside userData; keep it here.
    vi.stubEnv('LOCALAPPDATA', userData);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    rmSync(userData, { recursive: true, force: true });
  });
  const freshStore = async () => { vi.resetModules(); return import('../electron/system/settings-store'); };

  it('an upgraded settings.json reads as unseen; a saved revision survives a reload', async () => {
    writeFileSync(join(userData, 'settings.json'), JSON.stringify({ acceptedDisclaimer: true, config: {} }));
    let store = await freshStore();
    expect(store.load().onboardingSeen).toBe(0);
    expect(autoOnboarding(store.load())).toBe('update');
    store.save({ ...store.get(), onboardingSeen: ONBOARDING_REVISION });
    store = await freshStore();
    expect(store.load().onboardingSeen).toBe(ONBOARDING_REVISION);
    expect(autoOnboarding(store.load())).toBeNull();
  });

  it('a hand-edited value is coerced, never trusted as a string or a negative', async () => {
    writeFileSync(join(userData, 'settings.json'),
      JSON.stringify({ acceptedDisclaimer: true, onboardingSeen: '99', config: {} }));
    expect((await freshStore()).load().onboardingSeen).toBe(0);
    writeFileSync(join(userData, 'settings.json'),
      JSON.stringify({ acceptedDisclaimer: true, onboardingSeen: -3, config: {} }));
    expect((await freshStore()).load().onboardingSeen).toBe(0);
  });
});

let krypt: {
  app: { markOnboardingSeen: ReturnType<typeof vi.fn>; openExternal: ReturnType<typeof vi.fn> };
  state: { acceptDisclaimer: ReturnType<typeof vi.fn> };
  config: { update: ReturnType<typeof vi.fn> };
  credentials: { statusAll: ReturnType<typeof vi.fn> };
};

function setApp(cfg: Partial<TraderConfig>, accepted: boolean): void {
  app.value = {
    state: { acceptedDisclaimer: accepted },
    config: { accountMode: 'paper', terminalPaperBankrollUsd: 1000, mcpTradeMode: 'paper', ...cfg },
    backend: { status: 'running', authOk: false },
    refresh: { state: vi.fn(async () => {}) },
  };
}

beforeEach(() => {
  localStorage.clear();
  toast.success.mockClear();
  toast.warn.mockClear();
  krypt = {
    app: { markOnboardingSeen: vi.fn(async () => ({ ok: true })), openExternal: vi.fn(async () => {}) },
    state: { acceptDisclaimer: vi.fn(async () => ({ ok: true })) },
    config: { update: vi.fn(async () => ({ ok: true })) },
    credentials: { statusAll: vi.fn(async () => ({ mainnet: { hasWalletKey: false } })) },
    trading: { historyStart: vi.fn(async () => null) },
  };
  (window as unknown as { krypt: unknown }).krypt = krypt;
});

const cont = (): void => { fireEvent.click(screen.getByTestId('onboarding-continue')); };
// Past the account step, on paper (test/accountSetup.test.tsx covers the guide itself).
const leaveAccount = (): void => { fireEvent.click(screen.getByTestId('setup-leave')); };
// Past the account step and the goal, on its default: an AI, so the agent step comes next.
const toAgent = (): void => { leaveAccount(); cont(); };
const flush = async (): Promise<void> => { await act(async () => { await Promise.resolve(); }); };

describe('GuideHost after an update', () => {
  const host = (setPage = vi.fn(), update = true) => (
    <GuideHost needsOnboarding={false} needsUpdateOnboarding={update} setPage={setPage}><div>app</div></GuideHost>
  );
  beforeEach(() => setApp({}, true));

  it('opens by itself as a replay that says why, and closing it marks it seen and writes nothing else', async () => {
    const { rerender } = render(host());
    expect(screen.getByRole('dialog', { name: 'Onboarding (replay)' })).toBeTruthy();
    expect(screen.getByText(/New in this version/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Close onboarding'));
    await flush();
    expect(screen.queryByRole('dialog', { name: 'Onboarding (replay)' })).toBeNull();
    expect(krypt.app.markOnboardingSeen).toHaveBeenCalledTimes(1);
    expect(krypt.state.acceptDisclaimer).not.toHaveBeenCalled();
    expect(krypt.config.update).not.toHaveBeenCalled();
    // The seen mark's broadcast hasn't landed yet: it must not reopen.
    rerender(host());
    expect(screen.queryByRole('dialog', { name: 'Onboarding (replay)' })).toBeNull();
  });

  it('finishing it keeps the user on their page and offers the (new) tour once', async () => {
    const setPage = vi.fn();
    render(host(setPage));
    cont(); // welcome
    // The disclaimer is shown accepted and read-only.
    expect(screen.getByTestId('disclaimer-accepted')).toBeTruthy();
    expect((screen.getByTestId('disclaimer-accept') as HTMLInputElement).disabled).toBe(true);
    cont(); // disclaimer, already accepted
    await flush();
    toAgent();
    fireEvent.click(screen.getByRole('button', { name: 'Skip' })); // agent step
    fireEvent.click(screen.getByTestId('onboarding-start-paper')); // Paper, already on it
    await flush();
    expect(screen.queryByRole('dialog', { name: 'Onboarding (replay)' })).toBeNull();
    expect(setPage).not.toHaveBeenCalled();
    expect(krypt.app.markOnboardingSeen).toHaveBeenCalled();
    expect(krypt.state.acceptDisclaimer).not.toHaveBeenCalled();
    expect(krypt.config.update).not.toHaveBeenCalled();
    expect(screen.getByTestId('tour-offer')).toBeTruthy();
  });

  it('nothing opens once the revision is seen', () => {
    render(host(vi.fn(), false));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('first run', () => {
  beforeEach(() => setApp({ mcpEnabled: false, mcpTradeMode: 'off' }, false));
  const first = (setPage = vi.fn()) => render(
    <GuideHost needsOnboarding setPage={setPage}><button data-tour="nav-agents">AI Agents</button></GuideHost>,
  );

  it('the risks are accepted before the agent step, and only with both boxes ticked', async () => {
    first();
    expect(screen.getByRole('dialog', { name: 'Onboarding' })).toBeTruthy();
    expect(screen.queryByLabelText('Close onboarding')).toBeNull(); // first run can't be closed
    cont(); // -> disclaimer, not the agent step
    expect(screen.queryByText('pick-autopilot')).toBeNull();
    cont(); // nothing ticked: refused
    await flush();
    fireEvent.click(screen.getByTestId('disclaimer-accept'));
    cont(); // only one ticked: refused
    await flush();
    expect(krypt.state.acceptDisclaimer).not.toHaveBeenCalled();
    expect(toast.warn).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByTestId('disclaimer-eligible'));
    cont();
    await flush();
    expect(krypt.state.acceptDisclaimer).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('setup-chooser')).toBeTruthy();
    toAgent();
    expect(screen.getByText('pick-autopilot')).toBeTruthy();
  });

  async function throughDisclaimer(): Promise<void> {
    cont();
    fireEvent.click(screen.getByTestId('disclaimer-accept'));
    fireEvent.click(screen.getByTestId('disclaimer-eligible'));
    cont();
    await flush();
    toAgent();
  }

  it('Start on paper lands on AI Agents, writes no setting, and offers the tour once', async () => {
    const setPage = vi.fn();
    const r = first(setPage);
    await throughDisclaimer();
    fireEvent.click(screen.getByText('Skip'));
    fireEvent.click(screen.getByTestId('onboarding-start-paper'));
    await flush();
    expect(setPage).toHaveBeenCalledWith('agents');
    expect(krypt.config.update).not.toHaveBeenCalled();
    expect(krypt.app.markOnboardingSeen).toHaveBeenCalled();
    expect(screen.getByTestId('tour-offer').textContent).toContain('Take a 1-minute tour of the app?');
    expect(localStorage.getItem(TOUR_OFFERED_KEY)).toBe('1');
    fireEvent.click(screen.getByText('Start'));
    expect(screen.getByTestId('tour')).toBeTruthy();
    expect(screen.getByRole('dialog').querySelector('h2')!.textContent).toBe('AI Agents');
    r.unmount();
  });

  it('Set up account lands on the API Key page and still writes nothing', async () => {
    const setPage = vi.fn();
    first(setPage);
    await throughDisclaimer();
    fireEvent.click(screen.getByText('Skip'));
    fireEvent.click(screen.getByTestId('onboarding-add-key'));
    await flush();
    expect(setPage).toHaveBeenCalledWith('api');
    expect(krypt.config.update).not.toHaveBeenCalled();
  });

  describe('what they want to do', () => {
    const toGoal = async (setPage = vi.fn()) => {
      first(setPage);
      cont();
      fireEvent.click(screen.getByTestId('disclaimer-accept'));
      fireEvent.click(screen.getByTestId('disclaimer-eligible'));
      cont();
      await flush();
      leaveAccount();
      expect(screen.getByTestId('onboarding-goal')).toBeTruthy();
      return setPage;
    };

    it('offers the three choices, and writes nothing', async () => {
      await toGoal();
      expect(screen.getByTestId('goal-ai').textContent).toContain('Let an AI trade for me');
      expect(screen.getByTestId('goal-strategy').textContent).toContain('Try a ready-made strategy');
      expect(screen.getByTestId('goal-hand').textContent).toContain('Trade myself');
      expect(krypt.config.update).not.toHaveBeenCalled();
    });

    it('an AI goes through the agent step and lands on AI Agents', async () => {
      const setPage = await toGoal();
      cont();
      expect(screen.getByText('Connect your AI agent')).toBeTruthy();
      fireEvent.click(screen.getByText('Skip'));
      fireEvent.click(screen.getByTestId('onboarding-start-paper'));
      await flush();
      expect(setPage).toHaveBeenCalledWith('agents');
    });

    it.each([
      ['goal-strategy', 'strategies'],
      ['goal-hand', 'discover'],
    ])('%s skips the agent step and lands on %s', async (card, page) => {
      const setPage = await toGoal();
      fireEvent.click(screen.getByTestId(card));
      cont();
      expect(screen.queryByText('Connect your AI agent')).toBeNull();
      fireEvent.click(screen.getByTestId('onboarding-start-paper'));
      await flush();
      expect(setPage).toHaveBeenCalledWith(page);
      expect(krypt.config.update).not.toHaveBeenCalled();
    });

    it('setting up an account still lands on the API Key page', async () => {
      const setPage = await toGoal();
      fireEvent.click(screen.getByTestId('goal-hand'));
      cont();
      fireEvent.click(screen.getByTestId('onboarding-add-key'));
      await flush();
      expect(setPage).toHaveBeenCalledWith('api');
    });

    it('changing their mind after picking Autopilot drops its step', async () => {
      await toGoal();
      cont();
      fireEvent.click(screen.getByText('pick-autopilot'));
      expect(screen.getByLabelText('Onboarding step 6 of 7')).toBeTruthy();
      fireEvent.click(screen.getByText('Back')); // agent
      fireEvent.click(screen.getByText('Back')); // goal
      fireEvent.click(screen.getByTestId('goal-hand'));
      expect(screen.getByLabelText('Onboarding step 4 of 5')).toBeTruthy();
    });
  });

  describe('the history option', () => {
    const toStart = async () => {
      await throughDisclaimer();
      fireEvent.click(screen.getByText('Skip'));
    };

    it('is ticked on first run and starts a week of 15m and hourly', async () => {
      first();
      await toStart();
      expect((screen.getByTestId('onboarding-history') as HTMLInputElement).checked).toBe(true);
      fireEvent.click(screen.getByTestId('onboarding-start-paper'));
      await flush();
      expect(krypt.trading.historyStart).toHaveBeenCalledTimes(1);
      expect(krypt.trading.historyStart).toHaveBeenCalledWith({ days: 7, intervals: ['15m', 'hourly'] });
    });

    it('also starts from Set up account', async () => {
      first();
      await toStart();
      fireEvent.click(screen.getByTestId('onboarding-add-key'));
      await flush();
      expect(krypt.trading.historyStart).toHaveBeenCalledTimes(1);
    });

    it('does nothing when unticked', async () => {
      first();
      await toStart();
      fireEvent.click(screen.getByTestId('onboarding-history'));
      fireEvent.click(screen.getByTestId('onboarding-start-paper'));
      await flush();
      expect(krypt.trading.historyStart).not.toHaveBeenCalled();
    });

    it('is not offered in offline mode', async () => {
      setApp({ terminalOfflineMode: true }, false);
      first();
      await toStart();
      expect(screen.queryByTestId('onboarding-history')).toBeNull();
      fireEvent.click(screen.getByTestId('onboarding-start-paper'));
      await flush();
      expect(krypt.trading.historyStart).not.toHaveBeenCalled();
    });

    it('a failed start never holds up onboarding', async () => {
      krypt.trading.historyStart.mockRejectedValue(new Error('down'));
      const setPage = vi.fn();
      first(setPage);
      await toStart();
      fireEvent.click(screen.getByTestId('onboarding-start-paper'));
      await flush();
      expect(setPage).toHaveBeenCalledWith('agents');
    });
  });

  it('In-app Autopilot adds the quickstart as the last step', async () => {
    const setPage = vi.fn();
    first(setPage);
    await throughDisclaimer();
    fireEvent.click(screen.getByText('pick-autopilot'));
    expect(screen.getByLabelText('Onboarding step 6 of 7')).toBeTruthy();
    fireEvent.click(screen.getByTestId('onboarding-start-paper'));
    await flush();
    fireEvent.click(screen.getByText('quickstart-skip'));
    await flush();
    expect(setPage).toHaveBeenCalledWith('agents');
  });
});

function ReplayButton() {
  const { replayOnboarding } = useGuide();
  return <button type="button" onClick={replayOnboarding}>replay</button>;
}

describe('Replay onboarding', () => {
  const openReplay = () => {
    const setPage = vi.fn();
    const utils = render(<GuideHost needsOnboarding={false} setPage={setPage}><ReplayButton /></GuideHost>);
    fireEvent.click(screen.getByText('replay'));
    return { ...utils, setPage };
  };

  it('writes nothing: not the disclaimer, not a setting, not the page, and offers no tour', async () => {
    setApp({ mcpEnabled: true }, true);
    const { setPage } = openReplay();
    expect(screen.getByLabelText('Onboarding step 1 of 6')).toBeTruthy();
    cont(); cont();
    await flush();
    toAgent();
    cont();
    await flush();
    fireEvent.click(screen.getByTestId('onboarding-start-paper'));
    await flush();
    expect(screen.queryByRole('dialog', { name: 'Onboarding (replay)' })).toBeNull();
    expect(krypt.state.acceptDisclaimer).not.toHaveBeenCalled();
    expect(krypt.config.update).not.toHaveBeenCalled();
    expect(setPage).not.toHaveBeenCalled();
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.queryByTestId('tour-offer')).toBeNull();
  });

  it('a replay leaves the history option unticked, so nothing downloads without an explicit act', async () => {
    setApp({}, true);
    openReplay();
    cont(); cont();
    await flush();
    toAgent();
    cont();
    await flush();
    expect((screen.getByTestId('onboarding-history') as HTMLInputElement).checked).toBe(false);
    fireEvent.click(screen.getByTestId('onboarding-start-paper'));
    await flush();
    expect(krypt.trading.historyStart).not.toHaveBeenCalled();
  });

  it('"Start on paper" on a replay while Live switches back to Paper, and only that', async () => {
    setApp({ accountMode: 'live' }, true);
    openReplay();
    cont(); cont();
    await flush();
    toAgent();
    cont();
    await flush();
    fireEvent.click(screen.getByTestId('onboarding-start-paper'));
    await flush();
    expect(krypt.config.update).toHaveBeenCalledTimes(1);
    expect(krypt.config.update).toHaveBeenCalledWith({ accountMode: 'paper' });
  });

  it('closes from the close button or Escape, untouched', () => {
    setApp({}, true);
    openReplay();
    fireEvent.click(screen.getByLabelText('Close onboarding'));
    expect(screen.queryByRole('dialog', { name: 'Onboarding (replay)' })).toBeNull();
    fireEvent.click(screen.getByText('replay'));
    expect(screen.getByRole('dialog', { name: 'Onboarding (replay)' })).toBeTruthy();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Onboarding (replay)' })).toBeNull();
    expect(krypt.config.update).not.toHaveBeenCalled();
    expect(krypt.state.acceptDisclaimer).not.toHaveBeenCalled();
  });
});
