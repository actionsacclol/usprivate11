import { F_MONO, F_SANS } from '../engine/overlay';
import type { BubbleStyle } from './roster';

// The table's words. Every player has a voice you can tell apart with the
// sound off — shape, font and motion — because that IS the character on a
// muted Instagram autoplay.

const EMOJI = '"Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"';

export interface CBubble {
  x: number; y: number;
  text: string;
  style: BubbleStyle;
  age: number; life: number;
  seed: number;
}

function fontOf(s: BubbleStyle, u: number, scale = 1): string {
  return `${s.font.replace('{px}', String(Math.round(s.px * u * scale)))}, ${EMOJI}`;
}

function wrap(g: CanvasRenderingContext2D, text: string, maxW: number): string[] {
  const words = text.split(' ');
  const out: string[] = [];
  let cur = '';
  for (const w of words) {
    const t = cur ? `${cur} ${w}` : w;
    if (g.measureText(t).width > maxW && cur) { out.push(cur); cur = w; } else cur = t;
  }
  if (cur) out.push(cur);
  return out.slice(0, 3);
}

function rr(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  g.beginPath();
  g.roundRect(x, y, w, h, r);
}

/** Bubble outlines per voice. Each returns after building the path. */
function outline(g: CanvasRenderingContext2D, s: BubbleStyle, x: number, y: number, w: number, h: number, u: number, t: number, seed: number): void {
  switch (s.shape) {
    case 'burst': {
      // a shout: a spiky starburst around the box
      const cx = x + w / 2, cy = y + h / 2;
      const rx = w / 2 + 14 * u, ry = h / 2 + 12 * u;
      const n = 22;
      g.beginPath();
      for (let i = 0; i <= n * 2; i++) {
        const a = (i / (n * 2)) * Math.PI * 2;
        const k = i % 2 ? 1 : 0.82 + 0.05 * Math.sin(seed + i);
        const px = cx + Math.cos(a) * rx * k, py = cy + Math.sin(a) * ry * k;
        if (i === 0) g.moveTo(px, py); else g.lineTo(px, py);
      }
      g.closePath();
      return;
    }
    case 'shaky': {
      // a nervous, wobbling outline
      const r = Math.min(h / 2, 18 * u);
      const steps = 48;
      g.beginPath();
      for (let i = 0; i <= steps; i++) {
        const a = (i / steps) * Math.PI * 2;
        const px = x + w / 2 + Math.cos(a) * (w / 2 + 6 * u);
        const py = y + h / 2 + Math.sin(a) * (h / 2 + 4 * u);
        const j = Math.sin(t * 30 + i * 2.3 + seed) * 1.6 * u;
        if (i === 0) g.moveTo(px + j, py + j); else g.lineTo(px + j, py - j);
      }
      void r;
      g.closePath();
      return;
    }
    case 'terminal':
    case 'sign':
      g.beginPath();
      g.rect(x, y, w, h);
      return;
    case 'pixel': {
      // stepped corners, like the 8-bit dealer himself
      const s2 = 5 * u;
      g.beginPath();
      g.moveTo(x + s2, y); g.lineTo(x + w - s2, y); g.lineTo(x + w - s2, y + s2); g.lineTo(x + w, y + s2);
      g.lineTo(x + w, y + h - s2); g.lineTo(x + w - s2, y + h - s2); g.lineTo(x + w - s2, y + h); g.lineTo(x + s2, y + h);
      g.lineTo(x + s2, y + h - s2); g.lineTo(x, y + h - s2); g.lineTo(x, y + s2); g.lineTo(x + s2, y + s2);
      g.closePath();
      return;
    }
    case 'flat':
      rr(g, x, y, w, h, 6 * u);
      return;
    case 'bouncy':
    default:
      rr(g, x, y, w, h, h / 2);
  }
}

export function drawBubbles(g: CanvasRenderingContext2D, list: CBubble[], u: number, W: number, H: number, t: number): void {
  for (const b of list) {
    const s = b.style;
    const inT = Math.min(1, b.age / (s.shape === 'flat' ? 0.4 : 0.16));
    const out = Math.min(1, (b.life - b.age) / 0.3);
    if (out <= 0) continue;
    g.save();
    g.globalAlpha = out * (s.shape === 'flat' ? Math.min(1, inT * 1.2) : 1);
    const text = s.upper ? b.text.toUpperCase() : b.text;
    g.font = fontOf(s, u);
    const maxW = 250 * u;
    const lines = wrap(g, s.shape === 'terminal' ? `> ${text}` : text, maxW);
    const lh = s.px * u * 1.3;
    const tw = Math.max(...lines.map((l) => g.measureText(l).width));
    const padX = (s.shape === 'burst' ? 14 : 12) * u, padY = (s.shape === 'sign' ? 10 : 8) * u;
    const w = tw + padX * 2;
    const h = lines.length * lh + padY * 2 + (s.shape === 'sign' ? 6 * u : 0);
    const tail = 12 * u;
    let bob = 0;
    if (s.shape === 'bouncy') bob = Math.abs(Math.sin(t * 8 + b.seed)) * -6 * u;
    if (s.shape === 'flat') bob = (1 - inT) * 10 * u;
    const ax = b.x;
    const ay = b.y + bob;
    let x = ax - w / 2;
    x = Math.max(8 * u, Math.min(W - w - 8 * u, x));
    let y = ay - h - tail;
    y = Math.max(8 * u, Math.min(H - h - 8 * u, y));
    // pop: overshoot for the loud ones, none for Doubt
    const pop = s.shape === 'flat' ? 1 : inT < 1 ? 0.5 + inT * 0.62 : 1 + Math.max(0, 0.12 - (b.age - 0.16)) * 0.8;
    const shake = s.shape === 'burst' && b.age < 0.5 ? Math.sin(t * 70) * 2.5 * u * (1 - b.age / 0.5) : 0;
    g.translate(ax + shake, ay);
    g.scale(pop, pop);
    g.translate(-ax, -ay);

    // tail first, so the body covers its root
    g.fillStyle = s.bg;
    g.strokeStyle = s.edge;
    g.lineWidth = (s.shape === 'sign' ? 3.5 : 2.5) * u;
    g.beginPath();
    const tx = Math.max(x + 10 * u, Math.min(x + w - 10 * u, ax));
    g.moveTo(tx - 8 * u, y + h - 2);
    g.lineTo(ax, ay - 2 * u);
    g.lineTo(tx + 8 * u, y + h - 2);
    g.closePath();
    g.fill();
    g.stroke();

    // The Quant's terminal is dark on a dark room: it glows in its own
    // phosphor instead of casting a drop shadow, or it vanishes.
    const glow = s.shape === 'terminal';
    g.shadowColor = glow ? s.edge : 'rgba(0,0,0,0.5)';
    g.shadowBlur = (glow ? 16 : 10) * u;
    g.shadowOffsetY = glow ? 0 : 3 * u;
    outline(g, s, x, y, w, h, u, t, b.seed);
    g.fill();
    g.shadowColor = 'transparent';
    g.stroke();
    if (s.shape === 'sign') {
      // hazard band along the top
      g.save();
      g.beginPath();
      g.rect(x, y, w, 6 * u);
      g.clip();
      g.fillStyle = '#E5383B';
      g.fillRect(x, y, w, 6 * u);
      g.fillStyle = '#FFFFFF';
      for (let sx = x - 12 * u; sx < x + w; sx += 12 * u) {
        g.beginPath();
        g.moveTo(sx, y + 6 * u); g.lineTo(sx + 6 * u, y); g.lineTo(sx + 10 * u, y); g.lineTo(sx + 4 * u, y + 6 * u);
        g.fill();
      }
      g.restore();
    }
    // text
    g.fillStyle = s.fg;
    g.textBaseline = 'middle';
    g.textAlign = 'left';
    const top = y + padY + (s.shape === 'sign' ? 6 * u : 0);
    lines.forEach((l, i) => {
      let jx = 0, jy = 0;
      if (s.shape === 'shaky') { jx = Math.sin(t * 40 + i + b.seed) * 0.9 * u; jy = Math.cos(t * 37 + i) * 0.9 * u; }
      g.fillText(l, x + padX + jx, top + lh * (i + 0.5) + jy + 1);
    });
    if (s.shape === 'terminal' && Math.floor(t * 2.5) % 2 === 0) {
      const last = lines[lines.length - 1];
      g.fillRect(x + padX + g.measureText(last).width + 3 * u, top + lh * (lines.length - 0.5) - lh * 0.35, 8 * u, lh * 0.7);
    }
    g.restore();
  }
}

// ── the market card ────────────────────────────────────────────────────────

const INK = '#1d1712';
const IVORY = '#F7F0E0';
const GOLD = '#C9A55A';

export interface CardView {
  x: number; y: number;
  /** Where the 3D card lies on the felt: the overlay is dealt from there. */
  fromX: number; fromY: number;
  t: number;            // seconds since the hand was dealt
  out: number;          // 0..1 swept away
  item: number;
  kicker: string;
  title: string;
  dirLine: string;
  dir: 'YES' | 'NO';
  facts: string;
  tally: { yes: number; no: number; shown: number } | null;
  /** `color` overrides calls-green / folds-red: the real table's stamp
   *  summarises forecasts, it decides nothing. */
  stamp: { approved: boolean; text: string; sub: string; t: number; color?: string } | null;
  color: string;
}

/** A casino chip in 2D: a disc in `fill`, ivory edge dashes, a ring. */
function chip2d(g: CanvasRenderingContext2D, x: number, y: number, r: number, fill: string, lit: boolean): void {
  g.save();
  g.globalAlpha *= lit ? 1 : 0.22;
  g.fillStyle = fill;
  g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2); g.fill();
  g.strokeStyle = 'rgba(247,240,224,0.95)';
  g.lineWidth = r * 0.32;
  g.setLineDash([r * 0.45, r * 0.45]);
  g.beginPath(); g.arc(x, y, r * 0.8, 0, Math.PI * 2); g.stroke();
  g.setLineDash([]);
  g.lineWidth = Math.max(1, r * 0.12);
  g.beginPath(); g.arc(x, y, r * 0.5, 0, Math.PI * 2); g.stroke();
  g.restore();
}

/** `text` cut with an ellipsis to fit `max` px in the current font. */
function fitText(g: CanvasRenderingContext2D, text: string, max: number): string {
  if (g.measureText(text).width <= max) return text;
  let t = text;
  while (t.length > 1 && g.measureText(`${t}…`).width > max) t = t.slice(0, -1);
  return `${t.trimEnd()}…`;
}

export function drawCard(g: CanvasRenderingContext2D, c: CardView, u: number, W: number, H: number): void {
  // dealt: it slides up off the felt and turns face up, like a real card
  const deal = Math.min(1, c.t / 0.55);
  const e = 1 - Math.pow(1 - deal, 3);
  const a = Math.min(1, deal * 2.5) * (1 - c.out);
  if (a <= 0) return;
  g.save();
  g.globalAlpha = a;
  const w = 410 * u;
  // Every row is measured to the card's inner width before the card is
  // sized: a long title is cut with an ellipsis, a price line that would
  // collide with its facts sends the facts to their own row.
  const inner = w - 48 * u;
  g.font = `700 ${Math.round(25 * u)}px ${F_SANS}, ${EMOJI}`;
  const wrapped = wrap(g, c.title, inner);
  const titleLines = wrapped.slice(0, 2).map((l, i, xs) => fitText(g, i === xs.length - 1 && wrapped.length > 2 ? `${l}…` : l, inner));
  let dirPx = 23;
  g.font = `800 ${Math.round(dirPx * u)}px ${F_SANS}`;
  while (dirPx > 15 && g.measureText(c.dirLine).width > inner) {
    dirPx -= 1;
    g.font = `800 ${Math.round(dirPx * u)}px ${F_SANS}`;
  }
  const dirText = fitText(g, c.dirLine, inner);
  const dw = g.measureText(dirText).width;
  g.font = `600 ${Math.round(14.5 * u)}px ${F_SANS}, ${EMOJI}`;
  const factsW = c.facts ? g.measureText(c.facts).width : 0;
  const factsOwnRow = !!c.facts && dw + 12 * u + factsW > inner;
  const facts = c.facts ? fitText(g, c.facts, factsOwnRow ? inner : inner - dw - 12 * u) : '';
  const h = (122 + titleLines.length * 30 + (factsOwnRow ? 24 : 0) + (c.tally ? 42 : 0)) * u;
  let x = c.x - w / 2;
  let y = c.y - h;
  x = Math.max(10 * u, Math.min(W - w - 10 * u, x));
  y = Math.max(10 * u, Math.min(H - h - 10 * u, y));
  const cx = x + w / 2, cy = y + h / 2;
  // from the felt to its place, flipping (scale x through zero) on the way
  const px = c.fromX + (cx - c.fromX) * e;
  const py = c.fromY + (cy - c.fromY) * e - Math.sin(Math.PI * e) * 40 * u;
  const flip = Math.abs(Math.cos(Math.PI * Math.min(1, deal * 1.1)));
  const faceUp = deal > 0.45;
  g.translate(px, py);
  g.scale(Math.max(0.02, flip) * (0.35 + 0.65 * e), 0.35 + 0.65 * e);
  g.rotate((1 - e) * -0.25);
  g.translate(-cx, -cy);
  g.shadowColor = 'rgba(0,0,0,0.55)';
  g.shadowBlur = 24 * u;
  g.shadowOffsetY = 8 * u;
  rr(g, x, y, w, h, 16 * u);
  g.fillStyle = faceUp ? IVORY : '#6b1020';
  g.fill();
  g.shadowColor = 'transparent';
  if (!faceUp) {
    g.strokeStyle = GOLD;
    g.lineWidth = 3 * u;
    rr(g, x + 10 * u, y + 10 * u, w - 20 * u, h - 20 * u, 10 * u);
    g.stroke();
    g.restore();
    return;
  }
  // a gold double rule, the suit in the corners
  g.strokeStyle = c.color;
  g.lineWidth = 3 * u;
  rr(g, x + 8 * u, y + 8 * u, w - 16 * u, h - 16 * u, 11 * u);
  g.stroke();
  g.strokeStyle = 'rgba(201,165,90,0.55)';
  g.lineWidth = 1 * u;
  rr(g, x + 13 * u, y + 13 * u, w - 26 * u, h - 26 * u, 8 * u);
  g.stroke();
  g.fillStyle = INK;
  g.textBaseline = 'middle';
  g.textAlign = 'center';
  g.font = `400 ${Math.round(22 * u)}px Georgia, "Times New Roman", serif`;
  g.fillText('♠', x + w - 28 * u, y + 30 * u);
  g.fillText('♠', x + 28 * u, y + h - 28 * u);

  let ty = y + 32 * u;
  g.textAlign = 'left';
  g.fillStyle = '#8a6a2a';
  g.font = `600 ${Math.round(12 * u)}px ${F_MONO}, ${EMOJI}`;
  // kept clear of the corner suit: a long market name is cut, not overprinted
  g.fillText(fitText(g, `HAND #${c.item}  ·  ${c.kicker}`, w - 70 * u), x + 24 * u, ty);
  ty += 32 * u;
  g.fillStyle = INK;
  g.font = `700 ${Math.round(25 * u)}px ${F_SANS}, ${EMOJI}`;
  for (const l of titleLines) { g.fillText(l, x + 24 * u, ty); ty += 30 * u; }
  ty += 6 * u;
  g.font = `800 ${Math.round(dirPx * u)}px ${F_SANS}`;
  g.fillStyle = c.dir === 'YES' ? '#15803D' : '#B91C1C';
  g.fillText(dirText, x + 24 * u, ty);
  g.font = `600 ${Math.round(14.5 * u)}px ${F_SANS}, ${EMOJI}`;
  g.fillStyle = '#5b4a36';
  if (factsOwnRow) {
    ty += 26 * u;
    g.fillText(facts, x + 24 * u, ty);
    ty += 36 * u;
  } else {
    if (facts) g.fillText(facts, x + 36 * u + dw, ty + 1);
    ty += 38 * u;
  }

  if (c.tally) {
    // the bets: a chip per player, lit as it goes in
    const { yes, no, shown } = c.tally;
    const r = 8 * u;
    let dx = x + 24 * u;
    g.font = `800 ${Math.round(14 * u)}px ${F_MONO}`;
    g.fillStyle = '#15803D';
    g.fillText('YES', dx, ty);
    dx += 40 * u;
    let k = 0;
    for (let i = 0; i < yes; i++, k++) {
      chip2d(g, dx + r, ty, r, '#12804A', k < shown);
      dx += r * 2 + 5 * u;
    }
    dx += 10 * u;
    g.fillStyle = INK;
    g.font = `800 ${Math.round(19 * u)}px ${F_MONO}`;
    const score = `${Math.min(yes, shown)} – ${Math.max(0, Math.min(no, shown - yes))}`;
    g.fillText(score, dx, ty);
    dx += g.measureText(score).width + 14 * u;
    for (let i = 0; i < no; i++, k++) {
      chip2d(g, dx + r, ty, r, '#B3122E', k < shown);
      dx += r * 2 + 5 * u;
    }
    g.font = `800 ${Math.round(14 * u)}px ${F_MONO}`;
    g.fillStyle = '#B91C1C';
    g.fillText('NO', dx + 4 * u, ty);
  }
  g.restore();

  // the showdown: the dealer's call lands beside the card like a plaque
  if (c.stamp) {
    const st = c.stamp;
    const k = Math.min(1, st.t / 0.18);
    const scale = 2.2 - 1.2 * (1 - Math.pow(1 - k, 3));
    const col = st.color ?? (st.approved ? '#22C55E' : '#EF4444');
    g.save();
    g.globalAlpha = Math.min(1, st.t / 0.08) * (1 - c.out);
    g.font = `800 ${Math.round(40 * u)}px ${F_SANS}`;
    const tw = g.measureText(st.text).width;
    g.font = `700 ${Math.round(16 * u)}px ${F_SANS}`;
    // the sub line (a market name) is cut to the card's width
    const sub = st.sub ? fitText(g, st.sub, 340 * u) : '';
    const sw = sub ? g.measureText(sub).width : 0;
    const bw = Math.max(tw, sw) + 44 * u;
    const bh = (st.sub ? 90 : 64) * u;
    // Beside the card, never on it (the call must not hide the price or the
    // facts it is about) and, where the frame allows, not over the felt's
    // nameplates either: right of the card, else left, else above it.
    const gap = 16 * u;
    let sx = x + w + gap + bw / 2;
    let sy = y + h / 2;
    if (sx + bw / 2 > W - 10 * u) sx = x - gap - bw / 2;
    if (sx - bw / 2 < 10 * u) {
      sx = Math.max(bw / 2 + 10 * u, Math.min(W - bw / 2 - 10 * u, x + w / 2));
      sy = y - bh / 2 - 8 * u;
    }
    sy = Math.max(bh / 2 + 10 * u, Math.min(H - bh / 2 - 10 * u, sy));
    g.translate(sx, sy);
    g.rotate(-0.06);
    g.scale(scale, scale);
    // Near-opaque: the card's price line must not show through and garble
    // the call — it has to read at a glance.
    g.shadowColor = 'rgba(0,0,0,0.6)';
    g.shadowBlur = 20 * u;
    rr(g, -bw / 2, -bh / 2, bw, bh, 12 * u);
    g.fillStyle = 'rgba(16,10,7,0.94)';
    g.fill();
    g.shadowColor = 'transparent';
    g.strokeStyle = GOLD;
    g.lineWidth = 2 * u;
    g.stroke();
    rr(g, -bw / 2 + 6 * u, -bh / 2 + 6 * u, bw - 12 * u, bh - 12 * u, 8 * u);
    g.strokeStyle = col;
    g.lineWidth = 3 * u;
    g.stroke();
    g.fillStyle = col;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.shadowColor = col;
    g.shadowBlur = 14 * u;
    g.font = `800 ${Math.round(40 * u)}px ${F_SANS}`;
    g.fillText(st.text, 0, st.sub ? -13 * u : 2 * u);
    g.shadowBlur = 0;
    if (st.sub) {
      g.fillStyle = '#F1E6CC';
      g.font = `700 ${Math.round(16 * u)}px ${F_SANS}`;
      g.fillText(sub, 0, 24 * u);
    }
    g.restore();
  }
}

export interface Plate { x: number; y: number; name: string; color: string; vote: boolean | null; dim: boolean }

/** Brass-edged leather nameplates on the rail; a YES/NO chip once the
 *  player's bet is in. */
export function drawPlates(g: CanvasRenderingContext2D, plates: Plate[], u: number): void {
  for (const p of plates) {
    g.save();
    g.globalAlpha = p.dim ? 0.6 : 1;
    g.font = `700 ${Math.round(12 * u)}px ${F_SANS}, ${EMOJI}`;
    const label = p.name;
    const tw = g.measureText(label).width;
    const vw = p.vote === null ? 0 : 38 * u;
    const w = tw + 24 * u + vw;
    const h = 23 * u;
    const x = p.x - w / 2, y = p.y - h / 2;
    g.shadowColor = 'rgba(0,0,0,0.6)';
    g.shadowBlur = 8 * u;
    g.shadowOffsetY = 2 * u;
    rr(g, x, y, w, h, h / 2);
    g.fillStyle = 'rgba(22,14,9,0.9)';
    g.fill();
    g.shadowColor = 'transparent';
    g.strokeStyle = 'rgba(201,165,90,0.85)';
    g.lineWidth = 1.2 * u;
    g.stroke();
    // the player's colour as a jewel
    g.fillStyle = p.color;
    g.beginPath(); g.arc(x + 11 * u, p.y, 4 * u, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#F1E6CC';
    g.textBaseline = 'middle';
    g.fillText(label, x + 19 * u, p.y + 1);
    if (p.vote !== null) {
      const bx = x + tw + 22 * u;
      rr(g, bx, y + 3 * u, vw - 6 * u, h - 6 * u, (h - 6 * u) / 2);
      g.fillStyle = p.vote ? '#15803D' : '#B91C1C';
      g.fill();
      g.fillStyle = '#FFFFFF';
      g.font = `800 ${Math.round(10.5 * u)}px ${F_SANS}`;
      g.textAlign = 'center';
      g.fillText(p.vote ? 'YES' : 'NO', bx + (vw - 6 * u) / 2, p.y + 1);
    }
    g.restore();
  }
}

export interface BetBadge { x: number; y: number; text: string; yes: boolean; color: string }

/** A real forecaster's bet on the felt: a chip with its fair value on it,
 *  green for a YES-cheap call, red for a NO-cheap one. */
export function drawBetBadges(g: CanvasRenderingContext2D, list: BetBadge[], u: number): void {
  for (const b of list) {
    const r = 15 * u;
    // a little below the spot: the rail plate above it says the same name
    const x = b.x, y = b.y + 10 * u;
    g.save();
    g.shadowColor = 'rgba(0,0,0,0.6)';
    g.shadowBlur = 8 * u;
    chip2d(g, x, y, r, b.yes ? '#12804A' : '#B3122E', true);
    g.shadowColor = 'transparent';
    g.strokeStyle = b.color;
    g.lineWidth = 2 * u;
    g.beginPath(); g.arc(x, y, r + 1.5 * u, 0, Math.PI * 2); g.stroke();
    g.fillStyle = '#FFFFFF';
    g.font = `800 ${Math.round(11.5 * u)}px ${F_SANS}`;
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.fillText(b.text, x, y + 1);
    g.font = `800 ${Math.round(8.5 * u)}px ${F_MONO}`;
    g.fillStyle = b.yes ? '#86EFAC' : '#FCA5A5';
    g.fillText(b.yes ? 'YES' : 'NO', x, y + r + 9 * u);
    g.restore();
  }
}

export function drawMood(g: CanvasRenderingContext2D, x: number, y: number, name: string, color: string, u: number, W = Infinity): void {
  g.save();
  g.font = `600 ${Math.round(10 * u)}px ${F_MONO}`;
  const k = 'MOOD';
  const kw = g.measureText(k).width;
  g.font = `800 ${Math.round(14 * u)}px ${F_SANS}, ${EMOJI}`;
  const nw = g.measureText(name).width;
  const w = kw + nw + 30 * u, h = 26 * u;
  // the bar's neon sits at the room's edge: keep its label in the frame
  x = Math.max(w / 2 + 10 * u, Math.min(W - w / 2 - 10 * u, x));
  rr(g, x - w / 2, y - h / 2, w, h, h / 2);
  g.fillStyle = 'rgba(22,14,9,0.86)';
  g.fill();
  g.strokeStyle = color;
  g.lineWidth = 2 * u;
  g.shadowColor = color;
  g.shadowBlur = 12 * u;
  g.stroke();
  g.shadowBlur = 0;
  g.textBaseline = 'middle';
  g.font = `600 ${Math.round(10 * u)}px ${F_MONO}`;
  g.fillStyle = GOLD;
  g.fillText(k, x - w / 2 + 11 * u, y + 1);
  g.font = `800 ${Math.round(14 * u)}px ${F_SANS}, ${EMOJI}`;
  g.fillStyle = color;
  g.fillText(name, x - w / 2 + 19 * u + kw, y + 1);
  g.restore();
}

/** A settlement, staged: SHOWDOWN and the real result, top centre. */
export function drawShowdown(g: CanvasRenderingContext2D, line: string, color: string, t: number, u: number, W: number, H: number): void {
  const a = Math.min(1, t / 0.25) * Math.min(1, (3.6 - t) / 0.5);
  if (a <= 0) return;
  g.save();
  g.globalAlpha = a;
  const y = H * 0.17 - (1 - Math.min(1, t / 0.25)) * 14 * u;
  g.font = `700 ${Math.round(15 * u)}px ${F_SANS}, ${EMOJI}`;
  const lw = g.measureText(line).width;
  g.font = `italic 700 ${Math.round(30 * u)}px Georgia, "Times New Roman", serif`;
  const tw = g.measureText('Showdown').width;
  const w = Math.max(lw, tw) + 70 * u, h = 74 * u;
  const x = W / 2 - w / 2;
  g.shadowColor = 'rgba(0,0,0,0.6)';
  g.shadowBlur = 20 * u;
  rr(g, x, y - h / 2, w, h, 14 * u);
  g.fillStyle = 'rgba(16,10,7,0.92)';
  g.fill();
  g.shadowColor = 'transparent';
  g.strokeStyle = GOLD;
  g.lineWidth = 1.5 * u;
  g.stroke();
  g.fillStyle = '#E6C77E';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText('Showdown', W / 2, y - 12 * u);
  g.font = `700 ${Math.round(15 * u)}px ${F_SANS}, ${EMOJI}`;
  g.fillStyle = color;
  g.fillText(line, W / 2, y + 20 * u);
  chip2d(g, x + 22 * u, y, 10 * u, color, true);
  chip2d(g, x + w - 22 * u, y, 10 * u, color, true);
  g.restore();
}
