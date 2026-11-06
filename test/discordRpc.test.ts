import { EventEmitter } from 'node:events';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Discord Rich Presence. Discord is often opened after the app or restarted while it
 * runs, so the app keeps trying. No Discord is normal and must never surface an error.
 */

class FakeClient extends EventEmitter {
  static made: FakeClient[] = [];
  static loginOk = true;
  /** Leaves the login waiting, as a slow Discord does. */
  static hold = false;
  setActivity = vi.fn(async (_a: Record<string, unknown>) => ({}));
  clearActivity = vi.fn(async () => ({}));
  destroy = vi.fn(async () => undefined);
  login = vi.fn(async () => {
    if (FakeClient.hold) return new Promise<this>(() => undefined);
    if (!FakeClient.loginOk) throw new Error('Could not connect');
    this.emit('ready');
    return this;
  });
  constructor() {
    super();
    FakeClient.made.push(this);
  }
}

vi.mock('discord-rpc', () => ({ Client: FakeClient }));

import { isDiscordConnected, startDiscordRpc, stopDiscordRpc } from '../electron/system/discord';

const last = (): FakeClient => FakeClient.made[FakeClient.made.length - 1];
const unhandled = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  FakeClient.made = [];
  FakeClient.loginOk = true;
  FakeClient.hold = false;
  unhandled.mockClear();
  process.on('unhandledRejection', unhandled);
});
afterEach(() => {
  stopDiscordRpc();
  vi.useRealTimers();
  process.off('unhandledRejection', unhandled);
});

describe('Discord presence', () => {
  it('shows this product, with its own page and working buttons', async () => {
    await startDiscordRpc();
    expect(isDiscordConnected()).toBe(true);
    const a = last().setActivity.mock.calls[0][0];
    expect(a.state).toBe('krypt.cc/tools/polyus');
    expect(a.details).toBe('Trading on Polymarket US');
    expect(a.largeImageText).toBe('Krypt PolyBot US');
    expect(a.buttons).toEqual([
      { label: 'Free Tools', url: 'https://krypt.cc/tools' },
      { label: 'Join Discord', url: 'https://discord.gg/muzFKR657F' },
    ]);
  });

  it('finds Discord when it opens after the app', async () => {
    FakeClient.loginOk = false;
    await startDiscordRpc();
    expect(isDiscordConnected()).toBe(false);
    FakeClient.loginOk = true;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(isDiscordConnected()).toBe(true);
    expect(last().setActivity).toHaveBeenCalledTimes(1);
  });

  it('keeps trying while Discord stays closed', async () => {
    FakeClient.loginOk = false;
    await startDiscordRpc();
    await vi.advanceTimersByTimeAsync(90_000);
    expect(FakeClient.made).toHaveLength(4);
  });

  it('reconnects after Discord restarts, keeping the start time', async () => {
    await startDiscordRpc();
    const first = last();
    const started = first.setActivity.mock.calls[0][0].startTimestamp;
    first.emit('disconnected');
    expect(isDiscordConnected()).toBe(false);
    await vi.advanceTimersByTimeAsync(1_000);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(last()).not.toBe(first);
    expect(isDiscordConnected()).toBe(true);
    expect(last().setActivity.mock.calls[0][0].startTimestamp).toBe(started);
  });

  it('stops trying once switched off, and shows nothing', async () => {
    FakeClient.loginOk = false;
    await startDiscordRpc();
    stopDiscordRpc();
    await vi.advanceTimersByTimeAsync(120_000);
    expect(FakeClient.made).toHaveLength(1);
  });

  it('clears the status when switched off, and its own close causes no reconnect', async () => {
    await startDiscordRpc();
    const c = last();
    stopDiscordRpc();
    expect(c.clearActivity).toHaveBeenCalled();
    expect(c.destroy).toHaveBeenCalled();
    c.emit('disconnected');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeClient.made).toHaveLength(1);
  });

  it('switching off during a login does not bring the status back', async () => {
    FakeClient.hold = true;
    void startDiscordRpc();
    await vi.advanceTimersByTimeAsync(0);
    const c = last();
    stopDiscordRpc();
    c.emit('ready');
    expect(c.setActivity).not.toHaveBeenCalled();
    expect(isDiscordConnected()).toBe(false);
  });

  it('turns back on after being switched off', async () => {
    await startDiscordRpc();
    stopDiscordRpc();
    await startDiscordRpc();
    expect(FakeClient.made).toHaveLength(2);
    expect(isDiscordConnected()).toBe(true);
  });

  it('never leaves a rejected promise behind when there is no Discord', async () => {
    await startDiscordRpc();
    const c = last();
    c.setActivity.mockRejectedValue(new Error('nope'));
    c.clearActivity.mockRejectedValue(new Error('no socket'));
    c.destroy.mockRejectedValue(new Error('no socket'));
    c.emit('ready');
    stopDiscordRpc();
    vi.useRealTimers();
    await new Promise((r) => setTimeout(r, 10));
    expect(unhandled).not.toHaveBeenCalled();
  });
});
