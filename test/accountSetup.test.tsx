import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AccountSetup } from '../src/components/AccountSetup';
import { ApiKeysPage } from '../src/pages/ApiKeys';
import { loadProgress, saveProgress, stepsFor } from '../src/utils/accountSetup';
import { normalizePastedKey } from '../src/utils/apiKey';
import {
  POLYMARKET_DEVELOPER_URL, POLYMARKET_SIGNUP_URL, POLYMARKET_SQUAD_URL,
} from '../src/utils/links';

/**
 * The guided Polymarket US setup. What matters most: each button opens the right
 * link, a bad or rejected key never reads as connected, nothing leaves Paper, and
 * someone waiting on identity approval can close the app and come back.
 */

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warn: vi.fn(), info: vi.fn() }));
const app = vi.hoisted(() => ({ value: {} as Record<string, unknown> }));
vi.mock('../src/state/AppStateProvider', () => ({ useApp: () => app.value }));
vi.mock('../src/state/ToastProvider', () => ({ useToast: () => toast }));

const KEY_ID = '123e4567-e89b-12d3-a456-426614174000';
const SECRET = 'A'.repeat(43) + '=';

let krypt: {
  app: { openExternal: ReturnType<typeof vi.fn> };
  config: { update: ReturnType<typeof vi.fn> };
  credentials: {
    statusAll: ReturnType<typeof vi.fn>;
    save: ReturnType<typeof vi.fn>;
    test: ReturnType<typeof vi.fn>;
    clear: ReturnType<typeof vi.fn>;
  };
};

beforeEach(() => {
  localStorage.clear();
  app.value = {
    backend: { status: 'running', authOk: false },
    refresh: {
      credentials: vi.fn(async () => {}),
      account: vi.fn(async () => {}),
      backend: vi.fn(async () => {}),
    },
  };
  krypt = {
    app: { openExternal: vi.fn(async () => {}) },
    config: { update: vi.fn(async () => ({ ok: true })) },
    credentials: {
      statusAll: vi.fn(async () => ({ mainnet: { hasWalletKey: false } })),
      save: vi.fn(async () => ({ ok: true })),
      test: vi.fn(async () => ({ ok: true, data: { balanceUsd: 12.5, signerOk: true, issues: [] } })),
      clear: vi.fn(async () => ({ ok: true })),
    },
  };
  (window as unknown as { krypt: unknown }).krypt = krypt;
});

const flush = async (): Promise<void> => {
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
};
const click = (id: string): void => { fireEvent.click(screen.getByTestId(id)); };
const paste = (id: string, value: string): void => {
  fireEvent.change(screen.getByTestId(id), { target: { value } });
};

async function mountGuide(onDone = vi.fn()): Promise<ReturnType<typeof vi.fn>> {
  render(<AccountSetup onDone={onDone} />);
  await flush();
  return onDone;
}

describe('progress', () => {
  it('survives a restart and refuses anything that no longer makes sense', () => {
    expect(loadProgress()).toBeNull();
    saveProgress({ path: 'new', step: 'verify' });
    expect(loadProgress()).toEqual({ path: 'new', step: 'verify' });
    // `signup` isn't on the path for someone who already has an account.
    localStorage.setItem('krypt-us-account-setup-v2', JSON.stringify({ path: 'existing', step: 'signup' }));
    expect(loadProgress()).toBeNull();
    localStorage.setItem('krypt-us-account-setup-v2', '{not json');
    expect(loadProgress()).toBeNull();
    saveProgress(null);
    expect(localStorage.getItem('krypt-us-account-setup-v2')).toBeNull();
  });

  it('puts the invite first, since it generally only counts at sign-up', () => {
    expect(stepsFor('new')[0]).toBe('signup');
    expect(stepsFor('existing')).toEqual(['key', 'paste']);
    expect(stepsFor('new').at(-1)).toBe('paste');
    expect(stepsFor('existing').at(-1)).toBe('paste');
  });
});

describe('normalizePastedKey', () => {
  it('cleans up spaces and quotes and swaps values in the wrong boxes', () => {
    expect(normalizePastedKey(`  "${KEY_ID}", `, ` '${SECRET}' `)).toEqual({ keyId: KEY_ID, secretKey: SECRET });
    expect(normalizePastedKey(SECRET, KEY_ID)).toEqual({ keyId: KEY_ID, secretKey: SECRET });
    // Two wrong values are left for the shape check to refuse.
    expect(normalizePastedKey('nope', 'nah')).toEqual({ keyId: 'nope', secretKey: 'nah' });
  });
});

describe('the walk for someone with no account', () => {
  it('opens each exact link, then connects without leaving Paper', async () => {
    const onDone = await mountGuide();
    click('setup-new');

    screen.getByTestId('setup-step-signup');
    click('setup-open-signup');
    expect(krypt.app.openExternal).toHaveBeenLastCalledWith('https://polymarket.us/join/yuhgo');
    expect(POLYMARKET_SIGNUP_URL).toBe('https://polymarket.us/join/yuhgo');
    click('setup-next');

    screen.getByTestId('setup-step-verify');
    click('setup-next');
    screen.getByTestId('setup-step-fund');
    click('setup-next');

    screen.getByTestId('setup-step-key');
    click('setup-open-developer');
    expect(krypt.app.openExternal).toHaveBeenLastCalledWith(POLYMARKET_DEVELOPER_URL);
    click('setup-next');

    screen.getByTestId('setup-step-paste');
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    click('setup-save-key');
    await flush();

    expect(krypt.credentials.save).toHaveBeenCalledWith({ keyId: KEY_ID, secretKey: SECRET, env: 'mainnet' });
    screen.getByTestId('setup-done');
    expect(screen.getByTestId('setup-balance').textContent).toBe('$12.50');
    expect(screen.getByTestId('setup-done').textContent).toContain('still on Paper');
    expect(krypt.app.openExternal).toHaveBeenLastCalledWith(
      'https://polymarket.us/squad/join/0xOHjNIfwLBVzmovbjml?referrer=yuhgo',
    );
    expect(screen.getByTestId('setup-squad').textContent).toContain('We opened the squad page');
    expect(krypt.config.update).not.toHaveBeenCalled();
    expect(loadProgress()).toBeNull();

    click('setup-finish');
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('says whose invite this is and that any account works', async () => {
    await mountGuide();
    click('setup-new');
    const text = screen.getByTestId('setup-step-signup').textContent ?? '';
    expect(text).toContain('Krypt benefits');
    expect(text).toContain('Any Polymarket US account works');
    expect(screen.getByTestId('setup-invite-code').textContent).toBe('YUHGO');
  });
});

describe('the key screen', () => {
  async function atPaste(): Promise<void> {
    await mountGuide();
    click('setup-have-key');
  }

  it('refuses a bad paste on the screen without sending anything', async () => {
    await atPaste();
    paste('api-key-id', 'hello');
    paste('api-key-secret', 'world');
    click('setup-save-key');
    await flush();
    expect(krypt.credentials.save).not.toHaveBeenCalled();
    expect(screen.getByTestId('setup-key-problem').textContent).toContain('Key ID');
    expect(screen.queryByTestId('setup-done')).toBeNull();
  });

  it('accepts the two values in the wrong boxes', async () => {
    await atPaste();
    paste('api-key-id', SECRET);
    paste('api-key-secret', KEY_ID);
    click('setup-save-key');
    await flush();
    expect(krypt.credentials.save).toHaveBeenCalledWith({ keyId: KEY_ID, secretKey: SECRET, env: 'mainnet' });
    screen.getByTestId('setup-done');
  });

  it('never reads as connected when the exchange rejects the key', async () => {
    krypt.credentials.test.mockResolvedValue({ ok: false, message: 'Polymarket US rejected this API key' });
    await atPaste();
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    click('setup-save-key');
    await flush();
    expect(screen.queryByTestId('setup-done')).toBeNull();
    expect(screen.getByTestId('setup-key-problem').textContent).toContain('rejected');
    // What they pasted stays, so a Secret Key shown only once isn't lost.
    expect((screen.getByTestId('api-key-secret') as HTMLInputElement).value).toBe(SECRET);
    expect(loadProgress()).not.toBeNull();
  });

  it('shows why an accepted key is not ready to trade', async () => {
    krypt.credentials.test.mockResolvedValue({
      ok: true, data: { balanceUsd: 0, signerOk: true, issues: ['The account has no balance'] },
    });
    await atPaste();
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    click('setup-save-key');
    await flush();
    expect(screen.getByTestId('setup-done').textContent).toContain('The account has no balance');
  });
});

describe('the squad page', () => {
  const SQUAD = 'https://polymarket.us/squad/join/0xOHjNIfwLBVzmovbjml?referrer=yuhgo';

  const squadOpens = (): number => krypt.app.openExternal.mock.calls.filter(([u]) => u === SQUAD).length;

  async function connect(): Promise<void> {
    click('setup-have-key');
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    click('setup-save-key');
    await flush();
  }

  it('someone signing in goes straight to the key, and the squad opens when it connects', async () => {
    await mountGuide();
    click('setup-existing');
    screen.getByTestId('setup-step-key');
    click('setup-next');
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    click('setup-save-key');
    await flush();
    expect(squadOpens()).toBe(1);
  });

  it('says so before it happens, and that it credits Krypt', async () => {
    await mountGuide();
    click('setup-have-key');
    const text = screen.getByTestId('setup-step-paste').textContent ?? '';
    expect(text).toContain('Krypt squad page opens');
    expect(text).toContain('credits Krypt');
  });

  it('does not open for a rejected or unanswered key', async () => {
    krypt.credentials.test.mockResolvedValue({ ok: false, message: 'rejected' });
    await mountGuide();
    await connect();
    expect(squadOpens()).toBe(0);
    expect(localStorage.getItem('krypt-us-squad-opened-v1')).toBeNull();
  });

  it('opens once per machine, and offers a button after that', async () => {
    const first = render(<AccountSetup onDone={vi.fn()} />);
    await flush();
    await connect();
    expect(squadOpens()).toBe(1);
    first.unmount();
    await mountGuide();
    await connect();
    expect(squadOpens()).toBe(1);
    expect(screen.getByTestId('setup-squad').textContent).toContain('Open the squad page in your browser');
    click('setup-open-squad');
    expect(squadOpens()).toBe(2);
  });

  it('also opens when the key is added through the plain form on the API Key page', async () => {
    krypt.credentials.statusAll.mockResolvedValue({ mainnet: { hasWalletKey: true } });
    render(<ApiKeysPage />);
    await flush();
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    fireEvent.click(screen.getByText(/Save & connect/));
    await flush();
    expect(squadOpens()).toBe(1);
  });

  it('never talks to the exchange to join: it only opens the page', async () => {
    await mountGuide();
    await connect();
    expect(krypt.credentials.save).toHaveBeenCalledTimes(1);
    expect(krypt.credentials.test).toHaveBeenCalledTimes(1);
    expect(krypt.config.update).not.toHaveBeenCalled();
  });
});

describe('the first choice', () => {
  it('steers newcomers to the invite without pushing account holders to make a second account', async () => {
    await mountGuide();
    expect(screen.getByTestId('setup-new').textContent).toContain('Free 25 dollars');
    const existing = screen.getByTestId('setup-existing').textContent ?? '';
    expect(existing).toContain('doesn\'t apply to an account you already have');
    expect(existing).not.toMatch(/new account|second account/i);
  });
});

describe('leaving and coming back', () => {
  it('picks up at the saved step after a restart', async () => {
    saveProgress({ path: 'new', step: 'verify' });
    await mountGuide();
    screen.getByTestId('setup-step-verify');
    expect(screen.getByText('Step 2 of 5')).toBeTruthy();
  });

  it('Finish later keeps the place and writes nothing else', async () => {
    const onDone = await mountGuide();
    click('setup-new');
    click('setup-next');
    click('setup-later');
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(loadProgress()).toEqual({ path: 'new', step: 'verify' });
    expect(krypt.credentials.save).not.toHaveBeenCalled();
    expect(krypt.config.update).not.toHaveBeenCalled();
  });

  it('can be left straight away for paper', async () => {
    const onDone = await mountGuide();
    click('setup-leave');
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(krypt.app.openExternal).not.toHaveBeenCalled();
  });

  it('with a key already saved it offers only the squad, and drops a stale place', async () => {
    saveProgress({ path: 'new', step: 'verify' });
    krypt.credentials.statusAll.mockResolvedValue({ mainnet: { hasWalletKey: true } });
    await mountGuide();
    screen.getByTestId('squad-card');
    expect(screen.queryByTestId('setup-new')).toBeNull();
    expect(loadProgress()).toBeNull();
  });
});

describe('the API Key page', () => {
  it('opens the guide when there is no key, and hiding it sticks through a refresh', async () => {
    render(<ApiKeysPage />);
    await flush();
    screen.getByTestId('api-key-guide');

    click('setup-leave');
    await flush();
    expect(screen.queryByTestId('api-key-guide')).toBeNull();

    fireEvent.click(screen.getByText('Refresh'));
    await flush();
    expect(screen.queryByTestId('api-key-guide')).toBeNull();

    click('api-key-show-guide');
    screen.getByTestId('api-key-guide');
  });

  it('keeps the connected screen up after the key saves, until Finish', async () => {
    render(<ApiKeysPage />);
    await flush();
    click('setup-have-key');
    paste('api-key-id', KEY_ID);
    paste('api-key-secret', SECRET);
    krypt.credentials.statusAll.mockResolvedValue({ mainnet: { hasWalletKey: true } });
    click('setup-save-key');
    await flush();
    fireEvent.click(screen.getByText('Refresh'));
    await flush();
    screen.getByTestId('setup-done');
    click('setup-finish');
    await flush();
    expect(screen.queryByTestId('api-key-guide')).toBeNull();
    screen.getByTestId('squad-card');
  });

  it('with a key saved it shows the squad link and no guide', async () => {
    krypt.credentials.statusAll.mockResolvedValue({ mainnet: { hasWalletKey: true } });
    render(<ApiKeysPage />);
    await flush();
    expect(screen.queryByTestId('api-key-guide')).toBeNull();
    click('squad-card-open');
    expect(krypt.app.openExternal).toHaveBeenCalledWith(POLYMARKET_SQUAD_URL);
  });
});
