import { americanOdds, closesIn, ticketReturn, type BoardRow, type Ticket } from './sportsbook';
import { chipsText, type Btn } from './ui';

// What the sportsbook's monitors show: the wall of market screens (each a
// fixed slice of the board, so nothing rotates past before you read it),
// the "your bets" screen with every ticket marked to the live price, and the
// market list at a betting station. Plain canvas: the same painters fill the
// wall's monitors (floor.ts) and the station's (tables/sportsbook.ts).

const SANS = '"Chakra Petch", sans-serif';
const MONO = '"JetBrains Mono", monospace';

const cents = (c: number | null) => (c === null ? '—' : `${c.toFixed(c % 1 ? 1 : 0)}¢`);

function fitText(g: CanvasRenderingContext2D, text: string, w: number): string {
  if (g.measureText(text).width <= w) return text;
  let t = text;
  while (t.length > 4 && g.measureText(`${t}…`).width > w) t = t.slice(0, -1);
  return `${t}…`;
}

function header(g: CanvasRenderingContext2D, w: number, title: string, right: string, color: string, hh = 54): void {
  const grd = g.createLinearGradient(0, 0, w, 0);
  grd.addColorStop(0, color);
  grd.addColorStop(1, '#1e1b4b');
  g.fillStyle = grd;
  g.fillRect(0, 0, w, hh);
  g.textBaseline = 'middle';
  g.textAlign = 'left';
  g.fillStyle = '#ffffff';
  g.font = `800 ${Math.round(hh * 0.52)}px ${SANS}`;
  g.fillText(title, 18, hh / 2 + 1);
  g.textAlign = 'right';
  g.fillStyle = 'rgba(255,255,255,0.75)';
  g.font = `600 ${Math.round(hh * 0.32)}px ${MONO}`;
  g.fillText(right, w - 18, hh / 2 + 1);
}

export function ago(at: number | null): string {
  if (!at) return '';
  const s = Math.max(0, Math.round((Date.now() - at) / 1000));
  return s < 90 ? `updated ${s}s ago` : `updated ${Math.round(s / 60)}m ago`;
}

/** One market screen: `count` rows of the board from `from`, each with both
 *  sides' prices. Rows are clickable (`row:i`) when `click` is set; `sel`
 *  is highlighted. */
export function paintMarkets(
  g: CanvasRenderingContext2D, w: number, h: number,
  o: { rows: readonly BoardRow[]; from: number; count: number; title: string; at: number | null; note?: string | null; sel?: number; click?: boolean; lit?: Map<number, number> },
): Btn[] {
  const out: Btn[] = [];
  g.fillStyle = '#080512';
  g.fillRect(0, 0, w, h);
  const hh = Math.round(h * 0.12);
  header(g, w, o.title, ago(o.at), '#3b0764', hh);
  const shown = o.rows.slice(o.from, o.from + o.count);
  if (!shown.length) {
    g.textAlign = 'center';
    g.fillStyle = 'rgba(196,181,253,0.7)';
    g.font = `700 ${Math.round(h * 0.06)}px ${SANS}`;
    g.fillText(o.rows.length ? 'no more markets' : (o.note ?? 'Loading Polymarket US’s sports markets…'), w / 2, h / 2);
    return out;
  }
  const top = hh + 6;
  const rh = (h - top - 6) / o.count;
  const colA = w * 0.62, colB = w * 0.81, colW = w * 0.18;
  shown.forEach((r, k) => {
    const i = o.from + k;
    const y = top + k * rh;
    const on = i === o.sel;
    const lit = o.lit?.get(i) ?? 0;
    g.fillStyle = on ? 'rgba(168,85,247,0.35)' : lit > 0 ? `rgba(250,204,21,${0.1 + lit * 0.3})` : k % 2 ? 'rgba(168,85,247,0.08)' : 'rgba(236,72,153,0.05)';
    g.fillRect(6, y + 2, w - 12, rh - 4);
    if (on) {
      g.fillStyle = '#C084FC';
      g.fillRect(6, y + 2, 6, rh - 4);
    }
    if (o.click) out.push({ x: 6, y, w: w - 12, h: rh, id: `row:${i}`, enabled: true });
    g.textAlign = 'left';
    g.textBaseline = 'middle';
    g.fillStyle = '#ffffff';
    g.font = `700 ${Math.round(rh * 0.3)}px ${SANS}`;
    g.fillText(fitText(g, r.title, colA - 34), 22, y + rh * 0.36);
    g.fillStyle = 'rgba(255,255,255,0.5)';
    g.font = `500 ${Math.round(rh * 0.2)}px ${MONO}`;
    const vol = r.volume24hUsd === null ? '—' : r.volume24hUsd >= 1e6 ? `$${(r.volume24hUsd / 1e6).toFixed(1)}M` : `$${Math.round(r.volume24hUsd / 1e3)}K`;
    g.fillText(`vol 24h ${vol} · closes ${closesIn(r.hoursToClose)}`, 22, y + rh * 0.72);
    const side = (x: number, label: string, c: number | null, col: string) => {
      g.fillStyle = 'rgba(255,255,255,0.06)';
      g.fillRect(x, y + 6, colW, rh - 12);
      g.textAlign = 'center';
      g.fillStyle = 'rgba(255,255,255,0.7)';
      g.font = `600 ${Math.round(rh * 0.18)}px ${MONO}`;
      g.fillText(fitText(g, label, colW - 10), x + colW / 2, y + rh * 0.32);
      g.fillStyle = c === null ? 'rgba(255,255,255,0.3)' : col;
      g.font = `800 ${Math.round(rh * 0.3)}px ${SANS}`;
      g.fillText(c === null ? '—' : `${cents(c)} ${americanOdds(c)}`, x + colW / 2, y + rh * 0.68);
    };
    side(colA, r.yesLabel, r.yesCents, '#4ADE80');
    side(colB, r.noLabel, r.noCents, '#F9A8D4');
  });
  return out;
}

/** What a ticket is worth now, if its market is on the board: the side's
 *  price now against the price it was written at. */
export function markToMarket(t: Ticket, rows: readonly BoardRow[]): { now: number; value: number } | null {
  const r = rows.find((x) => x.conditionId === t.conditionId);
  if (!r) return null;
  const now = t.side === 'yes' ? r.yesCents : r.noCents;
  if (now === null) return null;
  return { now, value: Math.floor((t.stake * now) / t.priceCents) };
}

/** Your bets: open tickets first (with the price now, and what the ticket
 *  is worth at it), then the settled ones. */
export function paintBets(g: CanvasRenderingContext2D, w: number, h: number, tickets: readonly Ticket[], rows: readonly BoardRow[], max = 7): void {
  g.fillStyle = '#080512';
  g.fillRect(0, 0, w, h);
  const open = tickets.filter((t) => t.status === 'open');
  const hh = Math.round(h * 0.12);
  const staked = open.reduce((a, t) => a + t.stake, 0);
  header(g, w, 'YOUR BETS', open.length ? `${open.length} open · ${chipsText(staked)} chips riding` : 'play chips', '#831843', hh);
  const list = [...open.slice().reverse(), ...tickets.filter((t) => t.status !== 'open').reverse()].slice(0, max);
  if (!list.length) {
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillStyle = 'rgba(255,255,255,0.55)';
    g.font = `700 ${Math.round(h * 0.06)}px ${SANS}`;
    g.fillText('No tickets yet', w / 2, h / 2 - h * 0.04);
    g.font = `500 ${Math.round(h * 0.035)}px ${MONO}`;
    g.fillStyle = 'rgba(255,255,255,0.4)';
    g.fillText('sit at a betting window or the kiosk and place one', w / 2, h / 2 + h * 0.05);
    return;
  }
  const top = hh + 6;
  const rh = (h - top - 6) / max;
  list.forEach((t, k) => {
    const y = top + k * rh;
    const col = t.status === 'won' ? '#4ADE80' : t.status === 'lost' ? '#F87171' : t.status === 'void' ? '#E5E7EB' : '#C4B5FD';
    g.fillStyle = k % 2 ? 'rgba(236,72,153,0.06)' : 'rgba(168,85,247,0.08)';
    g.fillRect(6, y + 2, w - 12, rh - 4);
    g.fillStyle = col;
    g.fillRect(6, y + 2, 6, rh - 4);
    g.textBaseline = 'middle';
    g.textAlign = 'left';
    g.font = `800 ${Math.round(rh * 0.26)}px ${SANS}`;
    g.fillStyle = '#ffffff';
    const head = `${t.label} @ ${cents(t.priceCents)}`;
    g.fillText(fitText(g, head, w * 0.42), 22, y + rh * 0.34);
    g.font = `500 ${Math.round(rh * 0.2)}px ${MONO}`;
    g.fillStyle = 'rgba(255,255,255,0.55)';
    g.fillText(fitText(g, t.title, w * 0.56), 22, y + rh * 0.72);
    g.textAlign = 'right';
    if (t.status === 'open') {
      const m = markToMarket(t, rows);
      g.font = `800 ${Math.round(rh * 0.26)}px ${SANS}`;
      g.fillStyle = '#FDE68A';
      g.fillText(`${chipsText(t.stake)} → ${chipsText(ticketReturn(t.stake, t.priceCents))}`, w - 18, y + rh * 0.34);
      g.font = `600 ${Math.round(rh * 0.2)}px ${MONO}`;
      if (m) {
        const d = m.now - t.priceCents;
        g.fillStyle = d > 0 ? '#4ADE80' : d < 0 ? '#F87171' : 'rgba(255,255,255,0.6)';
        g.fillText(`now ${cents(m.now)} ${d > 0 ? '▲' : d < 0 ? '▼' : '='} · worth ~${chipsText(m.value)}`, w - 18, y + rh * 0.72);
      } else {
        g.fillStyle = 'rgba(255,255,255,0.45)';
        g.fillText('waiting on the result', w - 18, y + rh * 0.72);
      }
    } else {
      g.font = `800 ${Math.round(rh * 0.26)}px ${SANS}`;
      g.fillStyle = col;
      g.fillText(t.status.toUpperCase(), w - 18, y + rh * 0.34);
      g.font = `600 ${Math.round(rh * 0.2)}px ${MONO}`;
      g.fillText(t.status === 'won' ? `paid ${chipsText(t.paid)}` : t.status === 'void' ? `refunded ${chipsText(t.paid)}` : `lost ${chipsText(t.stake)}`, w - 18, y + rh * 0.72);
    }
  });
}
