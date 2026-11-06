import { ps2Box, ps2Text } from './ps2ui';
// Everything with words in it is drawn in 2D on top of the rendered frame:
// room signs, speech bubbles, the HUD. Canvas text is crisp at any output
// size, uses the real webfonts, and is composited into the same canvas as the
// 3D frame, so nothing can drift out of register with what it labels.

export const F_SANS = '"Chakra Petch", system-ui, sans-serif';
export const F_MONO = '"JetBrains Mono", Menlo, monospace';
export const F_PIXEL = '"Press Start 2P", monospace';
const EMOJI = '"Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"';

export type Tone = 'neutral' | 'win' | 'loss' | 'scan' | 'momo' | 'forge' | 'test' | 'opt' | 'gold';

export const TONE: Record<Tone, { fg: string; edge: string }> = {
  neutral: { fg: '#1b1530', edge: '#ffffff' },
  win: { fg: '#0f5132', edge: '#22C55E' },
  loss: { fg: '#7f1d1d', edge: '#EF4444' },
  scan: { fg: '#3b0764', edge: '#A855F7' },
  momo: { fg: '#831843', edge: '#EC4899' },
  forge: { fg: '#7c2d12', edge: '#F59E0B' },
  test: { fg: '#134e4a', edge: '#2DD4BF' },
  opt: { fg: '#0c4a6e', edge: '#38BDF8' },
  gold: { fg: '#713f12', edge: '#FBBF24' },
};

export interface SignView {
  x: number; y: number;
  icon: string; name: string; counter: string;
  accent: string; alpha: number; hot: number; focused: boolean;
}

export interface BubbleView {
  x: number; y: number; text: string; tone: Tone; age: number; life: number; big: boolean;
}

export interface FeedLine { text: string; tone: Tone; age: number }

export interface HudView {
  W: number; H: number; u: number; portrait: boolean;
  pnl: number | null; roi: number | null; winRate: number | null;
  settledToday: number | null; open: number | null; wins: number; losses: number;
  pnlPulse: number;
  feed: FeedLine[];
  banner: { text: string; sub: string; t: number } | null;
  poly: { x: number; y: number } | null;
  showBrand: boolean;
  /** Line under the KRYPT TRADER wordmark, and the wordmark's gradient —
   *  each scene keeps the HUD layout but speaks in its own palette. */
  brandSub?: string;
  brandColors?: [string, string, string];
}

function font(w: number | string, px: number, fam: string): string {
  return `${w} ${Math.round(px)}px ${fam}`;
}

function rr(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  g.beginPath();
  g.roundRect(x, y, w, h, r);
}

export function fmtUsd(v: number, sign = false): string {
  const s = sign ? (v > 0 ? '+' : v < 0 ? '−' : '') : v < 0 ? '−' : '';
  return `${s}$${Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtInt(v: number | null): string {
  return v === null ? '—' : Math.round(v).toLocaleString('en-US');
}

/** Text that may hold an emoji: the emoji face goes last in the stack so the
 *  brand font still sets the letters. */
function mixed(w: number | string, px: number, fam: string): string {
  return `${w} ${Math.round(px)}px ${fam}, ${EMOJI}`;
}

export function drawSigns(g: CanvasRenderingContext2D, signs: SignView[], u: number, W: number): void {
  for (const s of signs) {
    if (s.alpha <= 0.01) continue;
    const scale = (s.focused ? 1.2 : 1) * (1 + s.hot * 0.08);
    const px = 15 * u * scale;
    g.save();
    g.globalAlpha = s.alpha;
    g.font = mixed(700, px, F_SANS);
    const title = `${s.icon} ${s.name}`;
    const tw = g.measureText(title).width;
    g.font = font(600, px * 0.82, F_MONO);
    const cw = g.measureText(s.counter).width;
    const padX = 10 * u * scale;
    const gap = 9 * u * scale;
    const h = px * 1.85;
    const w = tw + gap + cw + padX * 2 + 8 * u;
    // Kept inside the frame: a sign hanging off the edge is a sign nobody
    // reads, and the stem still points at the room.
    const x = Math.max(6 * u, Math.min(W - w - 6 * u, s.x - w / 2));
    const y = s.y - h / 2;
    // stem down to the room
    g.strokeStyle = s.accent;
    g.globalAlpha = s.alpha * 0.55;
    g.lineWidth = 2 * u;
    g.beginPath();
    g.moveTo(s.x, y + h);
    g.lineTo(s.x, y + h + 10 * u);
    g.stroke();
    g.globalAlpha = s.alpha;
    g.shadowColor = s.accent;
    g.shadowBlur = (10 + s.hot * 22) * u;
    rr(g, x, y, w, h, h / 2);
    g.fillStyle = 'rgba(14,10,26,0.86)';
    g.fill();
    g.shadowBlur = 0;
    g.lineWidth = (s.focused ? 2.5 : 1.6) * u;
    g.strokeStyle = s.accent;
    g.stroke();
    g.textBaseline = 'middle';
    g.font = mixed(700, px, F_SANS);
    g.fillStyle = '#ffffff';
    g.fillText(title, x + padX, s.y + 1);
    // counter in an accent pill
    g.font = font(600, px * 0.82, F_MONO);
    const pillX = x + padX + tw + gap;
    rr(g, pillX - 4 * u, s.y - px * 0.62, cw + 8 * u, px * 1.24, px * 0.4);
    g.fillStyle = s.accent + '33';
    g.fill();
    g.fillStyle = s.accent;
    g.fillText(s.counter, pillX, s.y + 1);
    g.restore();
  }
}

/** Who said it, how it went: the box's inner line and the text's colour. */
const BUBBLE_TONE: Record<Tone, { edge: string; text: string }> = {
  neutral: { edge: '#c4b5fd', text: '#ffffff' },
  win: { edge: '#22C55E', text: '#bbf7d0' },
  loss: { edge: '#EF4444', text: '#fecaca' },
  scan: { edge: '#38BDF8', text: '#e0f2fe' },
  momo: { edge: '#F472B6', text: '#fce7f3' },
  forge: { edge: '#F59E0B', text: '#fef3c7' },
  test: { edge: '#2DD4BF', text: '#ccfbf1' },
  opt: { edge: '#38BDF8', text: '#e0f2fe' },
  gold: { edge: '#FBBF24', text: '#FDE68A' },
};

/** Speech, PS2-style: a hard-edged beveled box with a stepped tail down to
 *  the speaker, bold text with a hard shadow (ps2ui.ts). It snaps open,
 *  then fades and rises when it's done. */
export function drawBubbles(g: CanvasRenderingContext2D, bubbles: BubbleView[], u: number, W: number): void {
  for (const b of bubbles) {
    const inT = Math.min(1, b.age / 0.12);
    const out = Math.min(1, (b.life - b.age) / 0.35);
    if (out <= 0) continue;
    const size = (b.big ? 17 : 14) * u;
    g.save();
    g.globalAlpha = out;
    g.font = mixed(700, size, F_SANS);
    const tw = g.measureText(b.text).width;
    const padX = 12 * u, padY = 8 * u;
    const w = tw + padX * 2;
    const h = size + padY * 2;
    const step = Math.max(2, Math.round(3 * u));
    const lift = (1 - out) * 10 * u;
    let x = b.x - w / 2;
    x = Math.max(6 * u, Math.min(W - w - 6 * u, x));
    const y = b.y - h - step * 4 - lift;
    // it opens like a PS2 window: a quick vertical wipe
    const open = inT < 1 ? inT : 1;
    const hh = Math.max(step * 2, h * open);
    const yy = y + (h - hh) / 2;
    const tone = BUBBLE_TONE[b.tone];
    ps2Box(g, x, yy, w, hh, u, tone.edge, 0.86);
    if (open >= 1) {
      // the tail: three steps down to them
      const tx = Math.round(Math.max(x + step * 3, Math.min(x + w - step * 4, b.x)));
      for (let k = 0; k < 3; k++) {
        const sw = (3 - k) * step;
        g.fillStyle = '#efe6cf';
        g.fillRect(tx - sw / 2 - step / 2, Math.round(y + h + k * step - 1), sw + step, step);
        g.fillStyle = 'rgba(14,10,34,0.9)';
        g.fillRect(tx - sw / 2 + step / 2 - step / 2, Math.round(y + h + k * step - 1), Math.max(0, sw - step), step - 1);
      }
      ps2Text(g, b.text, x + padX, y + h / 2 + 1 * u, size, tone.text, { u });
    }
    g.restore();
  }
}

export interface TagView { x: number; y: number; label: string; color: string; dim: boolean }

/** The floating name tag over each of the user's agents: who it is, in its
 *  suit colour. Small and always on, under any bubble (drawn first). */
export function drawTags(g: CanvasRenderingContext2D, tags: TagView[], u: number): void {
  for (const t of tags) {
    g.save();
    g.globalAlpha = t.dim ? 0.45 : 0.95;
    const px = 11 * u;
    g.font = font(700, px, F_SANS);
    const tw = g.measureText(t.label).width;
    const dot = 7 * u;
    const w = tw + dot + 18 * u;
    const h = px + 8 * u;
    const x = t.x - w / 2;
    const y = t.y - h;
    rr(g, x, y, w, h, h / 2);
    g.fillStyle = 'rgba(10,8,20,0.82)';
    g.fill();
    g.lineWidth = 1.5 * u;
    g.strokeStyle = t.color;
    g.stroke();
    g.beginPath();
    g.arc(x + 8 * u + dot / 2, y + h / 2, dot / 2, 0, Math.PI * 2);
    g.fillStyle = t.color;
    g.fill();
    g.fillStyle = '#F8FAFC';
    g.textBaseline = 'middle';
    g.fillText(t.label, x + 12 * u + dot, y + h / 2 + 0.5 * u);
    g.restore();
  }
}

function statChip(g: CanvasRenderingContext2D, x: number, y: number, label: string, value: string, color: string, u: number): number {
  g.font = font(600, 10.5 * u, F_MONO);
  const lw = g.measureText(label).width;
  g.font = font(700, 17 * u, F_SANS);
  const vw = g.measureText(value).width;
  const w = Math.max(lw, vw) + 22 * u;
  const h = 44 * u;
  rr(g, x, y, w, h, 9 * u);
  g.fillStyle = 'rgba(14,10,26,0.78)';
  g.fill();
  g.strokeStyle = 'rgba(255,255,255,0.10)';
  g.lineWidth = 1 * u;
  g.stroke();
  g.textBaseline = 'alphabetic';
  g.fillStyle = '#A1A1AA';
  g.font = font(600, 10.5 * u, F_MONO);
  g.fillText(label, x + 11 * u, y + 16 * u);
  g.fillStyle = color;
  g.font = font(700, 17 * u, F_SANS);
  g.fillText(value, x + 11 * u, y + 36 * u);
  return w;
}

export function drawHud(g: CanvasRenderingContext2D, v: HudView): void {
  const { W, H, u, portrait } = v;
  g.save();
  // vignette: keeps the HUD legible over a bright ship without a panel
  const vg = g.createLinearGradient(0, 0, 0, H * 0.22);
  vg.addColorStop(0, 'rgba(5,3,12,0.55)');
  vg.addColorStop(1, 'rgba(5,3,12,0)');
  g.fillStyle = vg;
  g.fillRect(0, 0, W, H * 0.22);

  const m = 18 * u;
  // ── P&L block ─────────────────────────────────────────────────────────
  const pnl = v.pnl;
  const pnlColor = pnl === null ? '#A1A1AA' : pnl >= 0 ? '#4ADE80' : '#F87171';
  let x = m, y = m;
  if (portrait) { x = W / 2; }
  g.textAlign = portrait ? 'center' : 'left';
  g.textBaseline = 'alphabetic';
  g.fillStyle = '#C4B5FD';
  g.font = font(600, 11.5 * u, F_MONO);
  g.fillText('SESSION P&L', x, y + 12 * u);
  const big = (portrait ? 54 : 40) * u * (1 + v.pnlPulse * 0.06);
  g.font = font(700, big, F_SANS);
  g.shadowColor = pnlColor;
  g.shadowBlur = (14 + v.pnlPulse * 26) * u;
  g.fillStyle = pnlColor;
  const pnlText = pnl === null ? '—' : fmtUsd(pnl, true);
  g.fillText(pnlText, x, y + 12 * u + big * 0.95);
  g.shadowBlur = 0;
  if (v.roi !== null) {
    const pw = g.measureText(pnlText).width;
    g.font = font(600, 15 * u, F_MONO);
    g.fillStyle = pnlColor;
    const roi = `${v.roi >= 0 ? '+' : ''}${v.roi.toFixed(1)}%`;
    if (portrait) g.fillText(roi, x, y + 12 * u + big * 0.95 + 22 * u);
    else g.fillText(roi, x + pw + 10 * u, y + 12 * u + big * 0.95);
  }
  g.textAlign = 'left';
  let sy = y + 12 * u + big + (portrait ? 34 : 12) * u;
  const chips: [string, string, string][] = [
    ['WIN RATE', v.winRate === null ? '—' : `${v.winRate.toFixed(1)}%`, '#ffffff'],
    // Settled, not "orders": todayWins + todayLosses is what the account
    // actually counts. Adding the open book made it read as today's orders
    // when it was every position still open, whenever it was placed.
    ['SETTLED TODAY', fmtInt(v.settledToday), '#F9A8D4'],
    ['OPEN POSITIONS', fmtInt(v.open), '#D8B4FE'],
  ];
  if (portrait) {
    g.font = font(700, 17 * u, F_SANS);
    let total = 0;
    const widths = chips.map(([l, val]) => {
      g.font = font(600, 10.5 * u, F_MONO);
      const lw = g.measureText(l).width;
      g.font = font(700, 17 * u, F_SANS);
      return Math.max(lw, g.measureText(val).width) + 22 * u;
    });
    total = widths.reduce((a, b) => a + b, 0) + 8 * u * (widths.length - 1);
    let cx = W / 2 - total / 2;
    chips.forEach(([l, val, c], i) => { statChip(g, cx, sy, l, val, c, u); cx += widths[i] + 8 * u; });
    sy += 52 * u;
  } else {
    let cx = m;
    for (const [l, val, c] of chips) cx += statChip(g, cx, sy, l, val, c, u) + 8 * u;
  }

  // ── brand (top-right) ─────────────────────────────────────────────────
  if (v.showBrand || portrait) {
    g.textAlign = portrait ? 'center' : 'right';
    const bx = portrait ? W / 2 : W - m;
    const by = portrait ? H - 70 * u : m + 14 * u;
    g.font = font(400, 13 * u, F_PIXEL);
    const grad = g.createLinearGradient(bx - 220 * u, 0, bx, 0);
    const [c0, c1, c2] = v.brandColors ?? ['#818CF8', '#C084FC', '#F472B6'];
    grad.addColorStop(0, c0);
    grad.addColorStop(0.5, c1);
    grad.addColorStop(1, c2);
    g.fillStyle = grad;
    g.fillText('KRYPT POLYBOT', bx, by);
    g.font = font(600, 11 * u, F_MONO);
    g.fillStyle = '#A1A1AA';
    g.fillText(v.brandSub ?? 'AGENT HUB · krypt.cc', bx, by + 20 * u);
  }
  // ── POLYMARKET tag in space ───────────────────────────────────────────────
  if (v.poly) {
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.font = font(700, 13 * u, F_SANS);
    const t = 'POLYMARKET';
    const w = g.measureText(t).width + 18 * u;
    rr(g, v.poly.x - w / 2, v.poly.y - 11 * u, w, 22 * u, 11 * u);
    g.fillStyle = 'rgba(8,20,52,0.8)';
    g.fill();
    g.strokeStyle = '#3B82F6';
    g.lineWidth = 1.4 * u;
    g.stroke();
    g.fillStyle = '#93C5FD';
    g.fillText(t, v.poly.x, v.poly.y + 1);
  }

  // ── feed (bottom-left) ────────────────────────────────────────────────
  g.textAlign = 'left';
  g.textBaseline = 'middle';
  const lines = v.feed.slice(-4);
  const lh = 24 * u;
  const fx = portrait ? m * 1.5 : m;
  let fy = portrait ? H - 120 * u - lines.length * lh : H - m - lines.length * lh;
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    const a = Math.min(1, l.age / 0.25) * Math.max(0, Math.min(1, (9 - l.age) / 1.5)) * (0.55 + 0.45 * ((i + 1) / lines.length));
    if (a <= 0) { fy += lh; continue; }
    g.globalAlpha = a;
    g.font = mixed(600, 13 * u, F_SANS);
    const w = Math.min(W * (portrait ? 0.85 : 0.42), g.measureText(l.text).width + 22 * u);
    rr(g, fx, fy, w, lh - 5 * u, 6 * u);
    g.fillStyle = 'rgba(14,10,26,0.72)';
    g.fill();
    g.fillStyle = TONE[l.tone].edge;
    g.fillRect(fx, fy + 3 * u, 3 * u, lh - 11 * u);
    g.fillStyle = '#F4F4F5';
    g.fillText(l.text, fx + 11 * u, fy + (lh - 5 * u) / 2 + 1, w - 16 * u);
    fy += lh;
  }
  g.globalAlpha = 1;

  // ── banner (payday) ───────────────────────────────────────────────────
  if (v.banner) {
    const t = v.banner.t;
    const a = Math.min(1, t / 0.2) * Math.min(1, (3.2 - t) / 0.5);
    if (a > 0) {
      const s = 1 + Math.max(0, 0.35 - t) * 1.2;
      g.globalAlpha = a;
      g.textAlign = 'center';
      g.textBaseline = 'middle';
      const cy = portrait ? H * 0.3 : H * 0.42;
      g.save();
      g.translate(W / 2, cy);
      g.scale(s, s);
      g.font = font(400, (portrait ? 46 : 52) * u, F_PIXEL);
      g.shadowColor = '#F59E0B';
      g.shadowBlur = 30 * u;
      g.fillStyle = '#FDE047';
      g.fillText(v.banner.text, 0, 0);
      g.shadowBlur = 16 * u;
      g.font = font(700, 34 * u, F_SANS);
      g.fillStyle = '#4ADE80';
      g.fillText(v.banner.sub, 0, 52 * u);
      g.restore();
    }
  }
  g.restore();
}
