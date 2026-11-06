// Discord Rich Presence — best-effort, never blocks startup.
// See guidelines.md §8 — Krypt PolyBot US gets the same RPC treatment as
// the rest of the suite: "Using Krypt PolyBot US · krypt.cc" + 2 buttons.

import type { Client as RpcClient, Presence } from 'discord-rpc';

const CLIENT_ID = '1495323918234423406';

/** Discord is often opened after the app, or restarted while it runs. */
const RETRY_MS = 30_000;

let client: RpcClient | null = null;
let wanted = false;
let connected = false;
let retry: ReturnType<typeof setTimeout> | null = null;
let startedAt = 0;

function activity(): Presence {
  return {
    details: 'Trading on Polymarket US',
    state: 'krypt.cc/tools/polyus',
    startTimestamp: startedAt,
    largeImageKey: 'krypt',
    largeImageText: 'Krypt PolyBot US',
    instance: false,
    buttons: [
      { label: 'Free Tools', url: 'https://krypt.cc/tools' },
      { label: 'Join Discord', url: 'https://discord.gg/muzFKR657F' },
    ],
  };
}

function scheduleRetry(): void {
  if (!wanted || retry) return;
  retry = setTimeout(() => {
    retry = null;
    void connect();
  }, RETRY_MS);
  // Never the reason the app stays open.
  retry.unref?.();
}

/** Both reject rather than throw when there is no socket: no Discord is normal. */
function close(c: RpcClient, clear: boolean): void {
  if (clear) c.clearActivity().catch(() => undefined);
  c.destroy().catch(() => undefined);
}

async function connect(): Promise<void> {
  if (!wanted || client) return;
  let c: RpcClient | null = null;
  try {
    const { Client } = await import('discord-rpc');
    if (!wanted || client) return;
    const next = new Client({ transport: 'ipc' });
    c = next;
    client = next;
    next.on('ready', () => {
      if (client !== next) return;
      connected = true;
      next.setActivity(activity()).catch(() => undefined);
    });
    next.on('disconnected', () => {
      if (client !== next) return;
      client = null;
      connected = false;
      scheduleRetry();
    });
    await next.login({ clientId: CLIENT_ID });
  } catch {
    // No Discord running, or it dropped mid-login. Already handled if the client
    // was replaced or stopped in the meantime.
    if (c && client !== c) return;
    if (c) close(c, false);
    client = null;
    connected = false;
    scheduleRetry();
  }
}

export async function startDiscordRpc(): Promise<void> {
  if (wanted) return;
  wanted = true;
  startedAt = Date.now();
  await connect();
}

export function stopDiscordRpc(): void {
  wanted = false;
  if (retry) clearTimeout(retry);
  retry = null;
  const c = client;
  const wasConnected = connected;
  client = null;
  connected = false;
  if (c) close(c, wasConnected);
}

export function isDiscordConnected(): boolean {
  return connected;
}
