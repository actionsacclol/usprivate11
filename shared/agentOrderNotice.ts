/**
 * What an AI agent's order push says, for the toast (renderer) and the
 * desktop notification (main).
 *
 * The backend's `mcp:order` push started as {mode, message, approvalId?}.
 * It grows a superset — {status, agentName, title, side, action, size,
 * price} — so a live order's outcome can be told without parsing prose. Both
 * shapes arrive, and any field may be missing or mistyped: nothing here
 * trusts one.
 */

export interface AgentOrderEventLike {
  mode?: unknown;
  status?: unknown;
  message?: unknown;
  approvalId?: unknown;
  agentName?: unknown;
  title?: unknown;
  side?: unknown;
  action?: unknown;
  size?: unknown;
  price?: unknown;
}

const str = (v: unknown, max = 120): string =>
  (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, max) : '');
const fin = (v: unknown): number | null =>
  (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** "BUY 10 YES @ 42¢ in "Market"", or '' when there is nothing to say. */
export function agentOrderSummary(d: AgentOrderEventLike | null | undefined): string {
  if (!d || typeof d !== 'object') return '';
  const action = str(d.action, 8).toUpperCase();
  const side = str(d.side, 8).toUpperCase();
  const size = fin(d.size);
  const price = fin(d.price);
  const title = str(d.title, 60);
  const parts = [
    action,
    size !== null ? `${size}` : '',
    side,
    price !== null ? `@ ${price}¢` : '',
  ].filter(Boolean);
  const head = parts.join(' ');
  if (!head && !title) return '';
  return title ? `${head}${head ? ' in ' : ''}"${title}"` : head;
}

/** The text a push carries: its own message, or one built from its fields.
 *  null when neither exists (nothing to show). */
export function agentOrderMessage(d: AgentOrderEventLike | null | undefined): string | null {
  if (!d || typeof d !== 'object') return null;
  const msg = str(d.message, 400);
  if (msg) return msg;
  const who = str(d.agentName, 40);
  const what = agentOrderSummary(d);
  const status = str(d.status, 20);
  if (!what && !status) return null;
  const line = [who, what].filter(Boolean).join(' — ');
  return status ? `${line}${line ? ' ' : ''}(${status})` : line;
}

/** A LIVE order's outcome (not an approval request — that has its own). */
export function isLiveOutcome(d: AgentOrderEventLike | null | undefined): boolean {
  if (!d || typeof d !== 'object') return false;
  if (typeof d.approvalId === 'number') return false;
  return str(d.mode, 10).toLowerCase() === 'live';
}

export type LiveOutcome = 'filled' | 'sent' | 'refused' | 'unknown';

export function liveOutcome(d: AgentOrderEventLike | null | undefined): LiveOutcome {
  const s = str(d?.status, 20).toLowerCase();
  if (['filled', 'matched', 'partial', 'partially_filled'].includes(s)) return 'filled';
  if (['refused', 'failed', 'rejected', 'error', 'unmatched', 'blocked'].includes(s)) return 'refused';
  if (s === 'unknown') return 'unknown';
  if (s) return 'sent';
  // The original shape has no status: read what the message admits to.
  const m = str(d?.message, 400);
  if (/\brefused\b|\brejected\b|\bfailed\b|\bdeclined\b/i.test(m)) return 'refused';
  if (/\bunknown\b|no response|unconfirmed/i.test(m)) return 'unknown';
  if (/\bfilled\b|\bmatched\b/i.test(m)) return 'filled';
  return 'sent';
}

/** The desktop notification for a live agent order outcome, or null when the
 *  push is not one (paper, an approval request, an action). */
export function liveOrderNotice(d: AgentOrderEventLike | null | undefined):
  { title: string; body: string; critical: boolean } | null {
  if (!isLiveOutcome(d)) return null;
  // 'blocked' is the app's own rails saying no: nothing reached Polymarket.
  // An agent retrying into a cap would raise one of these a minute, so it is
  // the in-app feed's to show, not the desktop's.
  if (str(d?.status, 20).toLowerCase() === 'blocked') return null;
  const outcome = liveOutcome(d);
  const who = str(d?.agentName, 40) || 'An AI agent';
  const title = {
    filled: `${who}: LIVE order filled`,
    sent: `${who}: LIVE order sent`,
    refused: `${who}: LIVE order refused`,
    unknown: `${who}: LIVE order outcome UNKNOWN`,
  }[outcome];
  const msg = agentOrderMessage(d) ?? '';
  const tail = outcome === 'unknown' ? ' Check Polymarket US: it may have gone through.' : '';
  const body = (msg || agentOrderSummary(d) || 'Open AI Agents to see it.').slice(0, 220) + tail;
  return { title, body, critical: outcome === 'unknown' || outcome === 'refused' };
}

/** The in-app toast for any agent push (paper, live, approval, action), or
 *  null when it carries nothing to say. */
export function agentOrderToast(d: AgentOrderEventLike | null | undefined):
  { text: string; tone: 'warn' | 'info'; ms: number } | null {
  const msg = agentOrderMessage(d);
  if (!msg) return null;
  const approval = typeof d?.approvalId === 'number';
  if (approval) {
    return { text: `AI agent wants a LIVE order — ${msg} Approve or reject it on AI Agents.`, tone: 'warn', ms: 15_000 };
  }
  const live = str(d?.mode, 10).toLowerCase() === 'live' || /\bLIVE\b|ENABLED/.test(msg);
  if (live && isLiveOutcome(d)) {
    const outcome = liveOutcome(d);
    const lead = outcome === 'refused' ? 'AI agent LIVE order refused'
      : outcome === 'unknown' ? 'AI agent LIVE order — outcome UNKNOWN, check Polymarket US'
      : outcome === 'filled' ? 'AI agent LIVE order filled'
      : 'AI agent';
    return { text: `${lead} — ${msg}`, tone: 'warn', ms: outcome === 'unknown' ? 15_000 : 8_000 };
  }
  return { text: `AI agent — ${msg}`, tone: live ? 'warn' : 'info', ms: 8_000 };
}
