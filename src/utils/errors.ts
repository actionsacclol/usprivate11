import { parseErrorText } from '@shared/errors';

/**
 * Turn any thrown thing into a sentence a person can act on.
 *
 * Every toast used to print the raw transport: "Error invoking remote method
 * 'mcp:decide': Error: ValueError: …". That is three layers of plumbing in
 * front of the one part that means anything. So:
 *
 *  1. the transport is peeled off (shared/errors.ts, the same parser main's
 *     encoder is tested against);
 *  2. a known `[code]` from the backend becomes plain words;
 *  3. a few well-known transport failures (timeouts, a backend that died
 *     mid-call, an older backend) become plain words too;
 *  4. otherwise the backend's own sentence is shown — those are written for
 *     people already.
 */

/** Codes the backend (or main) attaches, in words. A code missing here is
 *  fine: the message that came with it is shown instead. */
const CODE_WORDS: Record<string, string | ((msg: string) => string)> = {
  backend_down: (m) => m || "The trading engine isn't running. Start it from the status bar.",
  mode_mismatch: (m) => m || 'The app switched between Paper and Live while this was open. Check it again.',
  account_mode_mismatch: (m) => m || 'The app switched between Paper and Live while this was open. Check it again.',
  no_credentials: 'No API key is saved. Add one on the API Key page — Paper needs none.',
  auth_failed: 'Polymarket US did not accept your saved API key. Re-test it on the API Key page.',
  unauthorized: 'Polymarket US did not accept your saved API key. Re-test it on the API Key page.',
  rate_limited: 'Polymarket US is rate-limiting requests right now. Wait a few seconds and try again.',
  timeout: 'That took too long to answer. The engine may still be busy — try again in a moment.',
  market_closed: 'That market is closed for trading.',
  insufficient_funds: 'Not enough balance in the account for that order.',
  cap_exceeded: (m) => m || 'That is over one of your caps (the agent caps, or Settings → Manual trading).',
  not_found: (m) => m || "That isn't there any more. Refresh and try again.",
  unsupported: (m) => m || "This version of the trading engine can't do that yet.",
};

function rawText(e: unknown): string {
  if (e instanceof Error) return e.message;
  if (typeof e === 'string') return e;
  if (e && typeof e === 'object') {
    const o = e as { message?: unknown; error?: unknown };
    if (typeof o.message === 'string') return o.message;
    if (typeof o.error === 'string') return o.error;
  }
  return e === undefined || e === null ? '' : String(e);
}

export function errorCode(e: unknown): string | null {
  return parseErrorText(rawText(e)).code;
}

export function userMessage(e: unknown, fallback = 'Something went wrong. Try again.'): string {
  const { code, message } = parseErrorText(rawText(e));
  if (code && code in CODE_WORDS) {
    const w = CODE_WORDS[code];
    return typeof w === 'function' ? w(message) : w;
  }
  if (/^RPC \w+ timed out after \d+ms$/i.test(message)) {
    return 'That took too long to answer. The engine may still be busy — try again in a moment.';
  }
  if (/^backend exited$/i.test(message) || /^Backend not running$/i.test(message)) {
    return 'The trading engine stopped while doing that. Start it again from the status bar, then try again.';
  }
  if (/^unknown method:/i.test(message)) {
    return "This version of the trading engine can't do that yet.";
  }
  return message || fallback;
}
