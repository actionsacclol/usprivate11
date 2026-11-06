import type { BotPosition, TraderConfig } from '@shared/types';
import type { HubSignal } from '../data';
import { GLOBAL_RAIL_DEFAULTS } from '@shared/agents';
import { balanceOf, type HubData } from '../data';
import { sessionPnlOf } from '../payday';
import { shortReason } from '../engine/jobs';
import { F_MONO, F_SANS } from '../engine/overlay';
import {
  agoShort, pulseBuckets, verdictText, type Desk, type DeskBook, type DeskCall, type ToolGroup,
} from './model';

// What every monitor in the Penthouse shows. Plain 2D canvas painters: a desk
// owns five (calls, market, record, pulse, status) and the video wall eight.
// Each one draws from the desk books (model.ts) or the app state the stage
// hands in, and nothing else — a monitor with nothing real to show says so in
// words, never with a placeholder number.

const EMOJI = '"Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"';
const BG0 = '#060a12';
const BG1 = '#0b1220';
const INK = '#E2E8F0';
const DIM = '#64748B';
const FAINT = '#334155';
const OK = '#4ADE80';
const BAD = '#F87171';
const WARN = '#FBBF24';

export const GROUP_COLOR: Record<ToolGroup, string> = {
  research: '#38BDF8', forecast: '#FBBF24', order: '#F472B6', account: '#A78BFA', build: '#34D399', other: '#94A3B8',
};
const MODE_COLOR = { live: '#EF4444', paper: '#3B82F6', off: '#475569' } as const;

export type Painter = (g: CanvasRenderingContext2D, w: number, h: number, now: number) => void;

function font(weight: number | string, px: number, fam: string, emoji = false): string {
  return `${weight} ${Math.round(px)}px ${fam}${emoji ? `, ${EMOJI}` : ''}`;
}

function fit(g: CanvasRenderingContext2D, text: string, w: number): string {
  if (w <= 0) return '';
  if (g.measureText(text).width <= w) return text;
  const cps = Array.from(text);
  let lo = 0, hi = cps.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (g.measureText(`${cps.slice(0, mid).join('')}…`).width <= w) lo = mid; else hi = mid - 1;
  }
  return `${cps.slice(0, lo).join('').trimEnd()}…`;
}

function wrap(g: CanvasRenderingContext2D, text: string, w: number, lines: number): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  const out: string[] = [];
  let cur = '';
  for (let i = 0; i < words.length; i++) {
    const next = cur ? `${cur} ${words[i]}` : words[i];
    if (g.measureText(next).width <= w) { cur = next; continue; }
    if (cur) out.push(cur);
    cur = words[i];
    if (out.length === lines - 1) {
      out.push(fit(g, [cur, ...words.slice(i + 1)].join(' '), w));
      return out;
    }
  }
  if (cur) out.push(fit(g, cur, w));
  return out.slice(0, lines);
}

function rr(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  g.beginPath();
  g.roundRect(x, y, w, h, r);
}

function base(g: CanvasRenderingContext2D, w: number, h: number): void {
  const grd = g.createLinearGradient(0, 0, 0, h);
  grd.addColorStop(0, BG1);
  grd.addColorStop(1, BG0);
  g.fillStyle = grd;
  g.fillRect(0, 0, w, h);
}

/** The bar every screen starts with: a thin accent line, a title, and a
 *  right-hand note. → the header's height. */
function header(g: CanvasRenderingContext2D, w: number, h: number, title: string, right: string, accent: string): number {
  const hh = Math.round(Math.min(h * 0.14, 52));
  g.fillStyle = 'rgba(255,255,255,0.03)';
  g.fillRect(0, 0, w, hh);
  g.fillStyle = accent;
  g.fillRect(0, 0, w, Math.max(3, Math.round(hh * 0.07)));
  g.textBaseline = 'middle';
  g.textAlign = 'left';
  g.fillStyle = INK;
  g.font = font(700, hh * 0.42, F_SANS, true);
  const rw = right ? (g.font = font(600, hh * 0.32, F_MONO), g.measureText(right).width + 24) : 0;
  g.font = font(700, hh * 0.42, F_SANS, true);
  g.fillText(fit(g, title, w - rw - 28), 14, hh / 2 + 2);
  if (right) {
    g.textAlign = 'right';
    g.fillStyle = DIM;
    g.font = font(600, hh * 0.32, F_MONO);
    g.fillText(right, w - 14, hh / 2 + 2);
  }
  g.fillStyle = 'rgba(255,255,255,0.06)';
  g.fillRect(0, hh, w, 1);
  return hh;
}

function pill(g: CanvasRenderingContext2D, x: number, y: number, text: string, color: string, px: number, align: 'left' | 'right' | 'center' = 'left'): number {
  g.font = font(800, px, F_SANS);
  const tw = g.measureText(text).width;
  const pw = tw + px * 1.2;
  const ph = px * 1.6;
  const x0 = align === 'right' ? x - pw : align === 'center' ? x - pw / 2 : x;
  rr(g, x0, y - ph / 2, pw, ph, ph / 2);
  g.fillStyle = color;
  g.fill();
  g.fillStyle = '#ffffff';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(text, x0 + pw / 2, y + 1);
  return pw;
}

/** Centered words for a screen with nothing real to show. */
function quiet(g: CanvasRenderingContext2D, w: number, top: number, h: number, line: string, sub?: string): void {
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillStyle = '#94A3B8';
  g.font = font(600, Math.min(26, w / 20), F_SANS);
  const cy = top + (h - top) / 2;
  g.fillText(fit(g, line, w - 40), w / 2, sub ? cy - 14 : cy);
  if (sub) {
    g.fillStyle = DIM;
    g.font = font(500, Math.min(17, w / 30), F_MONO);
    g.fillText(fit(g, sub, w - 40), w / 2, cy + 18);
  }
}

/** The watermark over a desk whose agent can't act. */
function offMark(g: CanvasRenderingContext2D, w: number, h: number, d: Desk): void {
  if (d.mode !== 'off') return;
  g.save();
  g.fillStyle = 'rgba(2,6,12,0.62)';
  g.fillRect(0, 0, w, h);
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillStyle = '#94A3B8';
  g.font = font(800, Math.min(w / 11, 54), F_SANS);
  g.fillText(d.enabled ? 'AGENTS OFF' : 'SWITCHED OFF', w / 2, h / 2 - 8);
  g.fillStyle = DIM;
  g.font = font(500, Math.min(w / 34, 16), F_MONO);
  g.fillText(d.enabled ? 'agent trading is off in AI Agents' : 'switched off on the AI Agents page', w / 2, h / 2 + 30);
  g.restore();
}

const cents = (c: number | null): string => (c === null ? '—' : `${Math.abs(c) >= 10 ? Math.round(c) : c.toFixed(1)}¢`);
const signed = (c: number): string => `${c >= 0 ? '+' : '−'}${cents(Math.abs(c))}`;
function usd(v: number | null | undefined, sign = false): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—';
  const s = sign ? (v > 0 ? '+' : v < 0 ? '−' : '') : v < 0 ? '−' : '';
  return `${s}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function mark(c: Pick<DeskCall, 'outcome'>): { text: string; color: string } {
  return c.outcome === 'ok' ? { text: '✓', color: OK } : c.outcome === 'refused' ? { text: '⛔', color: BAD } : { text: '⚠', color: WARN };
}

/** One call as a monitor line: the summary, a refusal's rail, a forecast's
 *  number. Paper or live is always said on an order. */
function callLine(c: DeskCall): string {
  if (c.outcome === 'refused') return `refused: ${shortReason(c.reason)} · ${c.summary}`;
  if (c.outcome === 'error') return `failed: ${c.reason ?? c.summary}`;
  if (c.tool === 'record_forecast' && c.fairCents !== null) {
    const edge = c.edgeCents === null ? '' : ` · edge ${signed(c.edgeCents)}${c.side ? ` ${c.side.toUpperCase()}` : ''}`;
    return `fair ${cents(c.fairCents)} vs ${cents(c.midCents)}${edge} · ${c.title ?? c.summary}`;
  }
  const mode = c.group === 'order' && c.mode ? `${c.mode.toUpperCase()} ` : '';
  return `${mode}${c.summary}`;
}

// ── a desk's five monitors ──────────────────────────────────────────────────

/** The big centre screen: every tool call, newest on top. */
export function paintCalls(d: Desk): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const total = d.ok + d.refused + d.error;
    const hh = header(g, w, h, `${d.who.emoji ? `${d.who.emoji} ` : ''}${d.who.label}`, `TOOL CALLS · ${total}${d.suppressed ? ` (+${d.suppressed})` : ''}`, d.who.color);
    if (!d.calls.length) {
      quiet(g, w, hh, h, d.connectAt ? 'Connected — no tool calls yet' : 'No tool calls yet this session', 'every call lands here the moment it is made');
      offMark(g, w, h, d);
      return;
    }
    const rows = Math.max(4, Math.floor((h - hh - 8) / 34));
    const rh = (h - hh - 8) / rows;
    const px = rh * 0.5;
    d.calls.slice(0, rows).forEach((c, i) => {
      const y = hh + 4 + i * rh;
      const fresh = Math.max(0, 1 - (now - c.at) / 2500);
      if (fresh > 0) {
        g.fillStyle = `rgba(56,189,248,${0.22 * fresh})`;
        g.fillRect(0, y, w, rh);
      } else if (i % 2 === 1) {
        g.fillStyle = 'rgba(255,255,255,0.025)';
        g.fillRect(0, y, w, rh);
      }
      const cy = y + rh / 2 + 1;
      g.textBaseline = 'middle';
      g.textAlign = 'right';
      g.fillStyle = DIM;
      g.font = font(500, px * 0.9, F_MONO);
      g.fillText(agoShort(c.at, now), 58, cy);
      g.textAlign = 'left';
      g.fillStyle = GROUP_COLOR[c.group];
      g.font = font(700, px, F_MONO);
      const tool = fit(g, c.tool ?? 'call', w * 0.27);
      g.fillText(tool, 70, cy);
      const tx = 70 + w * 0.28;
      g.fillStyle = c.outcome === 'ok' ? '#CBD5E1' : c.outcome === 'refused' ? '#FCA5A5' : '#FDE68A';
      g.font = font(500, px * 0.92, F_SANS);
      g.fillText(fit(g, callLine(c), w - tx - 44), tx, cy);
      const m = mark(c);
      g.textAlign = 'center';
      g.fillStyle = m.color;
      g.font = font(700, px, F_SANS, true);
      g.fillText(m.text, w - 22, cy);
    });
    offMark(g, w, h, d);
  };
}

/** Left: the market it touched last — its fair value against the price. */
export function paintMarket(d: Desk): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const m = d.market;
    const hh = header(g, w, h, 'LAST MARKET', m ? `${agoShort(m.at, now)} ago` : '', d.who.color);
    if (!m) {
      quiet(g, w, hh, h, 'No market touched yet', 'a market read or forecast shows here');
      offMark(g, w, h, d);
      return;
    }
    const pad = 16;
    g.textAlign = 'left';
    g.textBaseline = 'alphabetic';
    g.fillStyle = INK;
    g.font = font(700, h * 0.075, F_SANS);
    const lines = wrap(g, m.title ?? `${m.conditionId.slice(0, 10)}…${m.conditionId.slice(-6)}`, w - pad * 2, 2);
    lines.forEach((l, i) => g.fillText(l, pad, hh + h * 0.11 + i * h * 0.09));
    const top = hh + h * 0.11 + lines.length * h * 0.09 + 4;
    if (m.fairCents === null && m.midCents === null) {
      g.fillStyle = DIM;
      g.font = font(500, h * 0.055, F_MONO);
      g.fillText(`read with ${m.tool ?? 'a tool'} · no forecast on it`, pad, top + h * 0.1);
      offMark(g, w, h, d);
      return;
    }
    // the two numbers
    const colW = (w - pad * 2) / 2;
    const big = h * 0.16;
    const label = (x: number, text: string, color: string) => {
      g.fillStyle = color;
      g.font = font(600, h * 0.045, F_MONO);
      g.fillText(text, x, top + h * 0.06);
    };
    label(pad, 'ITS FAIR VALUE', d.who.color);
    label(pad + colW, 'MARKET MID', '#94A3B8');
    g.font = font(800, big, F_SANS);
    g.fillStyle = d.who.color;
    g.fillText(cents(m.fairCents), pad, top + h * 0.06 + big);
    g.fillStyle = INK;
    g.fillText(cents(m.midCents), pad + colW, top + h * 0.06 + big);
    // 0..100 bar with both ticks
    const by = top + h * 0.06 + big + h * 0.1;
    const bx = pad, bw = w - pad * 2;
    rr(g, bx, by, bw, h * 0.03, h * 0.015);
    g.fillStyle = FAINT;
    g.fill();
    const tick = (c: number | null, color: string, up: boolean) => {
      if (c === null) return;
      const x = bx + (Math.min(99, Math.max(1, c)) / 100) * bw;
      g.fillStyle = color;
      g.fillRect(x - 2, by - h * 0.03, 4, h * 0.09);
      g.beginPath();
      const ty = up ? by - h * 0.035 : by + h * 0.065;
      g.moveTo(x - 6, ty); g.lineTo(x + 6, ty); g.lineTo(x, up ? ty + 7 : ty - 7);
      g.fill();
    };
    tick(m.midCents, '#E2E8F0', false);
    tick(m.fairCents, d.who.color, true);
    // the edge, and when its number was set
    g.font = font(700, h * 0.06, F_SANS);
    const ey = by + h * 0.17;
    if (m.edgeCents !== null) {
      g.fillStyle = m.edgeCents >= 0 ? OK : BAD;
      g.fillText(`edge ${signed(m.edgeCents)}${m.side ? ` on ${m.side.toUpperCase()}` : ''} after fees`, pad, ey);
    } else if (m.fairCents !== null) {
      g.fillStyle = DIM;
      g.fillText('no side offered at that price', pad, ey);
    }
    g.textAlign = 'right';
    g.fillStyle = DIM;
    g.font = font(500, h * 0.045, F_MONO);
    g.fillText(m.forecastAt ? `forecast ${agoShort(m.forecastAt, now)} ago` : 'no forecast yet', w - pad, ey);
    offMark(g, w, h, d);
  };
}

/** Right: its forecast record, scored against the market when markets settle. */
export function paintRecord(d: Desk, loaded: () => boolean): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const s = d.score;
    const hh = header(g, w, h, 'FORECAST RECORD', s ? `${s.n} settled` : '', d.who.color);
    if (!s && !d.forecasts.length) {
      quiet(g, w, hh, h, loaded() ? 'No forecasts recorded yet' : 'Reading the scoreboard…', loaded() ? 'record_forecast calls are scored when markets settle' : undefined);
      offMark(g, w, h, d);
      return;
    }
    const pad = 16;
    let y = hh + 12;
    if (s) {
      const cells: [string, string, string][] = [
        ['SETTLED', String(s.n), INK],
        ['PENDING', s.pending === undefined ? '—' : String(s.pending), INK],
        ['SKILL', s.skill === null ? '—' : `${s.skill >= 0 ? '+' : '−'}${Math.abs(s.skill * 100).toFixed(0)}%`, s.skill === null ? DIM : s.skill >= 0 ? OK : BAD],
      ];
      const cw = (w - pad * 2) / cells.length;
      cells.forEach(([k, v, c], i) => {
        const x = pad + i * cw;
        g.textAlign = 'left';
        g.textBaseline = 'alphabetic';
        g.fillStyle = DIM;
        g.font = font(600, h * 0.045, F_MONO);
        g.fillText(k, x, y + h * 0.05);
        g.fillStyle = c;
        g.font = font(800, h * 0.11, F_SANS);
        g.fillText(v, x, y + h * 0.16);
      });
      y += h * 0.2;
      const v = verdictText(s.verdict);
      pill(g, pad, y + h * 0.03, v.text, v.tone === 'win' ? '#15803D' : v.tone === 'loss' ? '#B91C1C' : '#334155', h * 0.042);
      g.textAlign = 'right';
      g.textBaseline = 'middle';
      g.fillStyle = DIM;
      g.font = font(500, h * 0.042, F_MONO);
      const brier = s.brierAgent === null ? 'brier —' : `brier ${s.brierAgent.toFixed(3)} vs mkt ${s.brierMarket === null ? '—' : s.brierMarket.toFixed(3)}`;
      g.fillText(brier, w - pad, y + h * 0.03);
      y += h * 0.09;
    }
    const rows = d.forecasts.slice(0, Math.max(1, Math.floor((h - y - 6) / (h * 0.095))));
    const rh = h * 0.095;
    rows.forEach((r, i) => {
      const cy = y + i * rh + rh / 2;
      const o = r.outcome;
      g.textBaseline = 'middle';
      g.textAlign = 'left';
      // 0.5 is a UMA 50-50 split: neither side won.
      g.fillStyle = o === null || o === undefined || o === 0.5 ? DIM : o === 1 ? OK : BAD;
      g.font = font(700, rh * 0.5, F_SANS, true);
      g.fillText(o === null || o === undefined ? '•' : o === 1 ? 'YES' : o === 0.5 ? '50/50' : 'NO', pad, cy);
      g.fillStyle = '#CBD5E1';
      g.font = font(500, rh * 0.46, F_SANS);
      const nums = `${cents(r.fairValueCents)} / ${cents(r.marketMidCents)}`;
      g.font = font(600, rh * 0.44, F_MONO);
      const nw = g.measureText(nums).width;
      g.font = font(500, rh * 0.46, F_SANS);
      g.fillText(fit(g, r.title ?? r.conditionId, w - pad * 2 - nw - 70), pad + 56, cy);
      g.textAlign = 'right';
      g.fillStyle = d.who.color;
      g.font = font(600, rh * 0.44, F_MONO);
      g.fillText(nums, w - pad, cy);
    });
    void now;
    offMark(g, w, h, d);
  };
}

/** The top strip: calls a minute over the last half hour. */
export function paintPulse(d: Desk): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const buckets = pulseBuckets(d.pulse, now, 30);
    const last5 = buckets.slice(-5).reduce((a, b) => a + b.ok + b.refused + b.error, 0) / 5;
    const hh = header(g, w, h, 'ACTIVITY · last 30 min', `${last5 >= 10 ? Math.round(last5) : last5.toFixed(1)} calls/min`, d.who.color);
    const pad = 14;
    const bw = (w - pad * 2) / buckets.length;
    const top = hh + 10;
    const bottom = h - 26;
    const peak = Math.max(3, ...buckets.map((b) => b.ok + b.refused + b.error));
    g.fillStyle = 'rgba(255,255,255,0.05)';
    for (let k = 1; k <= 2; k++) g.fillRect(pad, bottom - ((bottom - top) * k) / 3, w - pad * 2, 1);
    buckets.forEach((b, i) => {
      let y = bottom;
      const x = pad + i * bw + 1;
      for (const [n, c] of [[b.ok, OK], [b.refused, BAD], [b.error, WARN]] as const) {
        if (!n) continue;
        const hgt = ((bottom - top) * n) / peak;
        g.fillStyle = c;
        g.fillRect(x, y - hgt, Math.max(1, bw - 2), hgt);
        y -= hgt;
      }
    });
    g.textBaseline = 'middle';
    g.textAlign = 'left';
    g.fillStyle = DIM;
    g.font = font(500, 13, F_MONO);
    g.fillText('−30m', pad, h - 12);
    g.textAlign = 'right';
    const who = [d.client?.split(/\s+/)[0], d.model].filter(Boolean).join(' · ');
    g.fillText(`last call ${d.lastAt ? `${agoShort(d.lastAt, now)} ago` : '—'}${who ? ` · ${who}` : ''}`, w - pad, h - 12);
    offMark(g, w, h, d);
  };
}

/** The portrait screen: who it is, its mode, its rules and its orders. */
export function paintStatus(d: Desk): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const grd = g.createLinearGradient(0, 0, 0, h * 0.32);
    grd.addColorStop(0, `${d.who.color}55`);
    grd.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = grd;
    g.fillRect(0, 0, w, h * 0.32);
    g.fillStyle = d.who.color;
    g.fillRect(0, 0, w, 5);
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.font = font(400, h * 0.1, F_SANS, true);
    g.fillStyle = '#ffffff';
    g.fillText(d.who.emoji ?? '🤖', w / 2, h * 0.09);
    g.font = font(800, h * 0.05, F_SANS, true);
    g.fillText(fit(g, d.who.name, w - 24), w / 2, h * 0.175);
    const mode = d.mode;
    pill(g, w / 2, h * 0.235, mode.toUpperCase(), MODE_COLOR[mode], h * 0.026, 'center');
    g.textAlign = 'center';
    g.fillStyle = '#94A3B8';
    g.font = font(500, h * 0.024, F_MONO);
    const who = [d.client?.split(/\s+/)[0], d.model].filter(Boolean).join(' · ');
    g.fillText(fit(g, who || 'no client has connected yet', w - 20), w / 2, h * 0.285);

    // counts
    const cy = h * 0.35;
    const cells: [string, number, string][] = [['OK', d.ok, OK], ['REFUSED', d.refused, BAD], ['ERRORS', d.error, WARN]];
    const cw = (w - 20) / 3;
    cells.forEach(([k, n, c], i) => {
      const x = 10 + i * cw + cw / 2;
      g.fillStyle = c;
      g.font = font(800, h * 0.045, F_SANS);
      g.fillText(String(n), x, cy);
      g.fillStyle = DIM;
      g.font = font(600, h * 0.018, F_MONO);
      g.fillText(k, x, cy + h * 0.035);
    });
    g.fillStyle = DIM;
    g.font = font(500, h * 0.021, F_MONO);
    g.fillText(`last call ${d.lastAt ? `${agoShort(d.lastAt, now)} ago` : '—'}`, w / 2, cy + h * 0.075);

    // orders
    let y = h * 0.48;
    const section = (title: string) => {
      g.textAlign = 'left';
      g.fillStyle = d.who.color;
      g.font = font(700, h * 0.022, F_MONO);
      g.fillText(title, 12, y);
      g.fillStyle = 'rgba(255,255,255,0.08)';
      g.fillRect(12, y + h * 0.017, w - 24, 1);
      y += h * 0.045;
    };
    section('ORDERS');
    if (!d.orders.length) {
      g.fillStyle = DIM;
      g.font = font(500, h * 0.022, F_SANS);
      g.fillText('none this session', 12, y);
      y += h * 0.045;
    }
    for (const o of d.orders.slice(0, 4)) {
      const m = mark(o);
      g.textAlign = 'left';
      g.fillStyle = m.color;
      g.font = font(700, h * 0.022, F_SANS, true);
      g.fillText(m.text, 12, y);
      g.fillStyle = o.mode === 'live' ? '#FCA5A5' : '#93C5FD';
      g.font = font(700, h * 0.019, F_MONO);
      const tag = o.tool === 'preview_order' ? 'PREVIEW' : (o.mode ?? '').toUpperCase() || '—';
      g.fillText(tag, 36, y);
      g.fillStyle = '#CBD5E1';
      g.font = font(500, h * 0.021, F_SANS);
      g.fillText(fit(g, o.outcome === 'refused' ? shortReason(o.reason) : o.summary, w - 112), 100, y);
      g.textAlign = 'right';
      g.fillStyle = DIM;
      g.font = font(500, h * 0.018, F_MONO);
      g.fillText(agoShort(o.at, now), w - 12, y);
      y += h * 0.042;
    }
    y += h * 0.012;
    section('RULES');
    const caps = [
      d.caps.maxOrderUsd > 0 ? `≤ ${usd(d.caps.maxOrderUsd).replace('.00', '')}/order` : 'no order cap',
      d.caps.dailySpendUsd > 0 ? `${usd(d.caps.dailySpendUsd).replace('.00', '')}/day` : 'no daily cap',
      `edge ≥ ${d.caps.minEdgeCents}¢`,
    ];
    g.textAlign = 'left';
    g.fillStyle = INK;
    g.font = font(600, h * 0.021, F_MONO);
    g.fillText(fit(g, caps.join(' · '), w - 24), 12, y);
    y += h * 0.04;
    g.fillStyle = '#94A3B8';
    g.font = font(500, h * 0.021, F_SANS);
    for (const r of (d.rules.length ? d.rules : ['no rules of its own']).slice(0, Math.max(1, Math.floor((h - y) / (h * 0.038))))) {
      g.fillText(fit(g, `· ${r}`, w - 24), 12, y);
      y += h * 0.038;
    }
    offMark(g, w, h, d);
  };
}

// ── the video wall ───────────────────────────────────────────────────────────

export interface WallSource {
  data(): HubData | null;
  book: DeskBook;
  approvals(): { at: number; text: string; mode: string; decision: 'approved' | 'rejected' | null }[];
}

const ACCENT = '#22D3EE';

function rowsArea(h: number, hh: number, rowH: number): number {
  return Math.max(1, Math.floor((h - hh - 10) / rowH));
}

export function paintAccount(src: WallSource): Painter {
  return (g, w, h) => {
    base(g, w, h);
    const d = src.data();
    const a = d?.account ?? null;
    const mode = d?.config?.accountMode === 'live' ? 'live' : 'paper';
    const hh = header(g, w, h, 'ACCOUNT', '', ACCENT);
    pill(g, w - 16, hh / 2 + 2, mode === 'live' ? 'LIVE ACCOUNT' : 'PAPER ACCOUNT', mode === 'live' ? MODE_COLOR.live : MODE_COLOR.paper, hh * 0.3, 'right');
    const down = !a || a.backendDown;
    const bal = down ? null : balanceOf(a);
    const pad = 28;
    g.textAlign = 'left';
    g.textBaseline = 'alphabetic';
    g.fillStyle = DIM;
    g.font = font(600, 20, F_MONO);
    g.fillText('BALANCE', pad, hh + 50);
    g.fillStyle = INK;
    g.font = font(800, 96, F_SANS);
    g.fillText(usd(bal), pad, hh + 146);
    const pnl = down ? null : sessionPnlOf(a);
    const cells: [string, string, string][] = [
      ['SESSION P&L', usd(pnl, true), pnl === null ? DIM : pnl >= 0 ? OK : BAD],
      ['TODAY', usd(down ? null : a?.todayPnlUsd, true), (a?.todayPnlUsd ?? 0) >= 0 ? OK : BAD],
      ['CASH', usd(down || bal === null ? null : a?.cashUsd), INK],
      ['OPEN', down || bal === null ? '—' : String(a?.openCount ?? '—'), INK],
    ];
    const cw = (w - pad * 2) / cells.length;
    cells.forEach(([k, v, c], i) => {
      const x = pad + i * cw;
      g.fillStyle = DIM;
      g.font = font(600, 18, F_MONO);
      g.fillText(k, x, hh + 220);
      g.fillStyle = c;
      g.font = font(800, 40, F_SANS);
      g.fillText(fit(g, v, cw - 12), x, hh + 266);
    });
    let note: [string, string] | null = null;
    if (down) note = ['BACKEND DOWN — nothing here could be read', BAD];
    else if (a?.tradingRestriction) note = [a.tradingRestriction.message, WARN];
    else if (a?.balanceSyncing) note = ['ledgers catching up after a fill — totals may dip briefly', WARN];
    if (note) {
      rr(g, pad, h - 92, w - pad * 2, 60, 12);
      g.fillStyle = `${note[1]}22`;
      g.fill();
      g.strokeStyle = note[1];
      g.lineWidth = 2;
      g.stroke();
      g.fillStyle = note[1];
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      g.font = font(700, 22, F_SANS);
      g.fillText(fit(g, note[0], w - pad * 2 - 30), w / 2, h - 62);
    }
  };
}

function isOpen(p: BotPosition): boolean {
  return !p.resolved && (p.status === 'filled' || p.status === 'partial');
}

export function paintPositions(src: WallSource): Painter {
  return (g, w, h) => {
    base(g, w, h);
    const d = src.data();
    const open = (d?.positions ?? []).filter(isOpen).sort((a, b) => (b.costUsd ?? 0) - (a.costUsd ?? 0));
    const hh = header(g, w, h, 'OPEN POSITIONS · BOT', `${open.length} open`, '#A78BFA');
    if (!open.length) { quiet(g, w, hh, h, 'No open bot positions'); return; }
    const rowH = 52;
    const n = rowsArea(h, hh, rowH);
    open.slice(0, n).forEach((p, i) => {
      const y = hh + 8 + i * rowH;
      const cy = y + rowH / 2;
      if (i % 2) { g.fillStyle = 'rgba(255,255,255,0.025)'; g.fillRect(0, y, w, rowH); }
      pill(g, 18, cy, p.direction.toUpperCase(), p.direction === 'yes' ? '#15803D' : '#B91C1C', 16);
      g.textAlign = 'left';
      g.textBaseline = 'middle';
      g.fillStyle = INK;
      g.font = font(600, 22, F_SANS);
      g.fillText(fit(g, p.title || p.ticker, w - 420), 86, cy);
      g.textAlign = 'right';
      g.fillStyle = '#94A3B8';
      g.font = font(600, 19, F_MONO);
      g.fillText(`${cents(p.avgFillPriceCents)} → ${cents(p.markPriceCents)}`, w - 150, cy);
      const pl = p.livePnlUsd;
      g.fillStyle = pl === null ? DIM : pl >= 0 ? OK : BAD;
      g.font = font(700, 22, F_SANS);
      g.fillText(usd(pl, true), w - 18, cy);
    });
  };
}

function sigTime(s: HubSignal): number {
  const t = Date.parse(s.createdAt);
  return Number.isFinite(t) ? t : 0;
}

export function paintSignals(src: WallSource): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const d = src.data();
    const sig = [...(d?.signals ?? [])].sort((a, b) => sigTime(b) - sigTime(a));
    const hh = header(g, w, h, 'ENGINE ORDERS', '', '#EC4899');
    if (!sig.length) { quiet(g, w, hh, h, 'No engine orders yet'); return; }
    const rowH = 48;
    sig.slice(0, rowsArea(h, hh, rowH)).forEach((s, i) => {
      const y = hh + 8 + i * rowH;
      const cy = y + rowH / 2;
      if (i % 2) { g.fillStyle = 'rgba(255,255,255,0.025)'; g.fillRect(0, y, w, rowH); }
      g.textBaseline = 'middle';
      g.textAlign = 'left';
      g.fillStyle = '#F472B6';
      g.font = font(700, 17, F_MONO);
      g.fillText(s.source.toUpperCase().slice(0, 6), 16, cy);
      g.fillStyle = INK;
      g.font = font(500, 21, F_SANS);
      g.fillText(fit(g, s.title || s.ticker, w - 470), 110, cy);
      g.textAlign = 'right';
      g.fillStyle = '#CBD5E1';
      g.font = font(600, 18, F_MONO);
      g.fillText(`${s.direction.toUpperCase()} ${cents(s.priceCents)}`, w - 150, cy);
      g.fillStyle = s.traded ? OK : DIM;
      g.font = font(700, 17, F_SANS);
      g.fillText(s.traded ? 'TRADED' : `${agoShort(sigTime(s) || null, now)} ago`, w - 16, cy);
    });
  };
}

export function paintAgents(src: WallSource): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const desks = [...src.book.list()].sort((a, b) => (b.lastAt ?? 0) - (a.lastAt ?? 0));
    const hh = header(g, w, h, 'YOUR AGENTS', `${desks.length} desk${desks.length === 1 ? '' : 's'}`, '#FBBF24');
    if (!desks.length) { quiet(g, w, hh, h, 'No agents set up yet', 'add one on the AI Agents page'); return; }
    const rowH = Math.min(62, (h - hh - 10) / Math.max(4, desks.length));
    desks.slice(0, rowsArea(h, hh, rowH)).forEach((d, i) => {
      const y = hh + 6 + i * rowH;
      const cy = y + rowH / 2;
      if (i % 2) { g.fillStyle = 'rgba(255,255,255,0.025)'; g.fillRect(0, y, w, rowH); }
      g.fillStyle = d.who.color;
      g.fillRect(0, y + 6, 5, rowH - 12);
      g.textBaseline = 'middle';
      g.textAlign = 'left';
      g.fillStyle = INK;
      g.font = font(700, rowH * 0.38, F_SANS, true);
      g.fillText(fit(g, `${d.who.emoji ?? ''} ${d.who.label}`.trim(), w * 0.36), 20, cy);
      pill(g, w * 0.4, cy, d.mode.toUpperCase(), MODE_COLOR[d.mode], rowH * 0.24);
      g.textAlign = 'right';
      g.font = font(600, rowH * 0.3, F_MONO);
      g.fillStyle = OK;
      g.fillText(`${d.ok} ok`, w * 0.66, cy);
      g.fillStyle = d.refused ? BAD : DIM;
      g.fillText(`${d.refused} refused`, w * 0.82, cy);
      g.fillStyle = DIM;
      g.fillText(d.lastAt ? `${agoShort(d.lastAt, now)} ago` : '—', w - 16, cy);
    });
  };
}

export function paintStream(src: WallSource): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const calls = src.book.stream(24).map((c) => ({ at: c.at, kind: 'call' as const, c }));
    const appr = src.approvals().map((a) => ({ at: a.at, kind: 'approval' as const, a }));
    const rows = [...calls, ...appr].sort((x, y) => y.at - x.at);
    const hh = header(g, w, h, 'EVERY AGENT CALL', `${calls.length ? 'live' : 'waiting'}`, '#38BDF8');
    if (!rows.length) { quiet(g, w, hh, h, 'Quiet floor — no agent has called yet', 'calls appear here from every desk at once'); return; }
    const rowH = 40;
    rows.slice(0, rowsArea(h, hh, rowH)).forEach((r, i) => {
      const y = hh + 6 + i * rowH;
      const cy = y + rowH / 2;
      const fresh = Math.max(0, 1 - (now - r.at) / 2500);
      if (fresh > 0) { g.fillStyle = `rgba(56,189,248,${0.2 * fresh})`; g.fillRect(0, y, w, rowH); }
      g.textBaseline = 'middle';
      g.textAlign = 'right';
      g.fillStyle = DIM;
      g.font = font(500, 16, F_MONO);
      g.fillText(agoShort(r.at, now), 58, cy);
      g.textAlign = 'left';
      if (r.kind === 'approval') {
        g.fillStyle = r.a.mode === 'live' ? '#FCA5A5' : '#FDE68A';
        g.font = font(700, 18, F_SANS, true);
        const verb = r.a.decision === 'approved' ? '✅ approved' : r.a.decision === 'rejected' ? '✖ rejected' : '🛎 needs you';
        g.fillText(fit(g, `${verb} · ${r.a.text}`, w - 90), 70, cy);
        return;
      }
      const c = r.c;
      g.fillStyle = c.desk.who.color;
      g.font = font(700, 18, F_SANS, true);
      const name = fit(g, `${c.desk.who.emoji ?? ''} ${c.desk.who.name}`.trim(), 190);
      g.fillText(name, 70, cy);
      g.fillStyle = GROUP_COLOR[c.group];
      g.font = font(700, 17, F_MONO);
      g.fillText(fit(g, c.tool ?? 'call', 200), 270, cy);
      g.fillStyle = c.outcome === 'ok' ? '#CBD5E1' : c.outcome === 'refused' ? '#FCA5A5' : '#FDE68A';
      g.font = font(500, 18, F_SANS);
      g.fillText(fit(g, callLine(c), w - 520), 480, cy);
      const m = mark(c);
      g.textAlign = 'center';
      g.fillStyle = m.color;
      g.font = font(700, 19, F_SANS, true);
      g.fillText(m.text, w - 20, cy);
    });
  };
}

export function paintClock(src: WallSource): Painter {
  return (g, w, h, now) => {
    base(g, w, h);
    const d = new Date(now);
    const hh = header(g, w, h, 'TIME', d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }), '#818CF8');
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillStyle = INK;
    g.font = font(800, 150, F_MONO);
    g.fillText(d.toLocaleTimeString('en-GB', { hour12: false }), w / 2, hh + 140);
    g.fillStyle = '#94A3B8';
    g.font = font(600, 30, F_MONO);
    g.fillText(`UTC ${d.toISOString().slice(11, 19)}`, w / 2, hh + 250);
  };
}

export function paintSettled(src: WallSource): Painter {
  return (g, w, h) => {
    base(g, w, h);
    const d = src.data();
    const a = d?.account ?? null;
    const done = (d?.positions ?? []).filter((p) => p.resolved && p.outcomeCorrect !== null)
      .sort((x, y) => (Date.parse(y.resolvedAt ?? '') || 0) - (Date.parse(x.resolvedAt ?? '') || 0));
    const tw = a?.todayWins, tl = a?.todayLosses;
    const hh = header(g, w, h, 'SETTLED · BOT', tw !== undefined && tl !== undefined && !a?.backendDown ? `today ${tw}W / ${tl}L` : '', '#34D399');
    if (!done.length) { quiet(g, w, hh, h, 'Nothing settled yet'); return; }
    const rowH = 48;
    done.slice(0, rowsArea(h, hh, rowH)).forEach((p, i) => {
      const y = hh + 8 + i * rowH;
      const cy = y + rowH / 2;
      if (i % 2) { g.fillStyle = 'rgba(255,255,255,0.025)'; g.fillRect(0, y, w, rowH); }
      const win = p.outcomeCorrect === 1;
      g.textBaseline = 'middle';
      g.textAlign = 'left';
      g.fillStyle = win ? OK : BAD;
      g.font = font(800, 20, F_SANS);
      g.fillText(win ? 'WIN' : 'LOSS', 16, cy);
      g.fillStyle = INK;
      g.font = font(500, 21, F_SANS);
      g.fillText(fit(g, p.title || p.ticker, w - 260), 90, cy);
      g.textAlign = 'right';
      g.fillStyle = (p.pnlUsd ?? 0) >= 0 ? OK : BAD;
      g.font = font(700, 22, F_SANS);
      g.fillText(usd(p.pnlUsd, true), w - 16, cy);
    });
  };
}

function cfgNum(c: TraderConfig | null | undefined, k: keyof TraderConfig, dflt: number): number {
  const v = c?.[k];
  return typeof v === 'number' && Number.isFinite(v) ? v : dflt;
}

export function paintRails(src: WallSource): Painter {
  return (g, w, h) => {
    base(g, w, h);
    const c = src.data()?.config ?? null;
    const hh = header(g, w, h, 'AGENT RAILS · GLOBAL', '', '#F472B6');
    if (!c) { quiet(g, w, hh, h, 'Settings not loaded'); return; }
    const mode = c.mcpEnabled === false ? 'off' : (c.mcpTradeMode ?? 'paper');
    const rows: [string, string, string][] = [
      ['AGENT SERVER', c.mcpEnabled === false ? 'OFF' : 'ON', c.mcpEnabled === false ? DIM : OK],
      ['AGENT TRADING', String(mode).toUpperCase(), MODE_COLOR[mode as keyof typeof MODE_COLOR] ?? INK],
      ['ACCOUNT', c.accountMode === 'live' ? 'LIVE' : 'PAPER', c.accountMode === 'live' ? MODE_COLOR.live : MODE_COLOR.paper],
      ['LIVE ORDERS NEED YOU', c.mcpLiveApproval === false ? 'NO' : 'YES', c.mcpLiveApproval === false ? WARN : OK],
      ['PER ORDER', `$${cfgNum(c, 'mcpMaxOrderUsd', GLOBAL_RAIL_DEFAULTS.maxOrderUsd)}`, INK],
      ['SPEND / DAY', `$${cfgNum(c, 'mcpDailySpendUsd', GLOBAL_RAIL_DEFAULTS.dailySpendUsd)}`, INK],
      ['LOSS STOP / DAY', cfgNum(c, 'mcpDailyLossUsd', 0) > 0 ? `$${cfgNum(c, 'mcpDailyLossUsd', 0)}` : 'none', INK],
      ['MIN EDGE', `${cfgNum(c, 'mcpMinEdgeCents', GLOBAL_RAIL_DEFAULTS.minEdgeCents)}¢`, INK],
      ['MAX POSITIONS', String(cfgNum(c, 'mcpMaxPositions', GLOBAL_RAIL_DEFAULTS.maxOpenPositions)), INK],
    ];
    const colW = (w - 40) / 2;
    const rowH = (h - hh - 20) / Math.ceil(rows.length / 2);
    rows.forEach(([k, v, col], i) => {
      const x = 20 + (i % 2) * colW;
      const y = hh + 10 + Math.floor(i / 2) * rowH;
      g.textAlign = 'left';
      g.textBaseline = 'alphabetic';
      g.fillStyle = DIM;
      g.font = font(600, 17, F_MONO);
      g.fillText(k, x, y + rowH * 0.38);
      g.fillStyle = col;
      g.font = font(800, 36, F_SANS);
      g.fillText(v, x, y + rowH * 0.85);
    });
  };
}
