import { F_MONO, F_SANS } from '../engine/overlay';
import { drawPrompt } from '../engine/talkui';

// The game panels' building blocks, drawn into the scene's own canvas like
// the dialogue (talkui.ts): what is on screen is what Fullscreen shows. A
// panel draws its buttons and returns their rectangles; the engine hit-tests
// a click against them and hands the id back to the game.

export interface Btn { x: number; y: number; w: number; h: number; id: string; enabled: boolean }
/** `fill`: the panel is the whole canvas (a terminal's screen in the world). */
export interface View { W: number; H: number; u: number; t: number; fill?: boolean }
export interface Box { x: number; y: number; w: number; h: number }

export function rr(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number): void {
  g.beginPath();
  g.roundRect(x, y, w, h, r);
}

export function chipsText(n: number): string {
  return Math.round(n).toLocaleString('en-US');
}

/** The panel: bottom centre, the game's colour along the top, its title and
 *  your stack in the header, a key line in the footer. Returns the body. */
export function panel(g: CanvasRenderingContext2D, v: View, o: { title: string; sub: string; color: string; h: number; chips: number; hint: string }): Box {
  const { W, H, u } = v;
  const pw = v.fill ? W - 8 * u : Math.min(W - 40 * u, 1040 * u);
  const ph = v.fill ? H - 12 * u : Math.min(H - 70 * u, o.h * u);
  const px = W / 2 - pw / 2;
  const py = v.fill ? 8 * u : H - ph - 18 * u;
  const grd = g.createLinearGradient(0, py, 0, py + ph);
  grd.addColorStop(0, 'rgba(14,10,28,0.94)');
  grd.addColorStop(1, 'rgba(6,4,14,0.97)');
  rr(g, px, py, pw, ph, 16 * u);
  g.fillStyle = grd;
  g.fill();
  g.strokeStyle = o.color;
  g.lineWidth = 1.6 * u;
  g.globalAlpha = 0.85;
  g.stroke();
  g.globalAlpha = 1;
  rr(g, px + 22 * u, py - 2 * u, 64 * u, 4 * u, 2 * u);
  g.fillStyle = o.color;
  g.fill();
  const pad = 20 * u;
  g.textBaseline = 'top';
  g.textAlign = 'left';
  g.font = `700 ${22 * u}px ${F_SANS}`;
  g.fillStyle = o.color;
  g.fillText(o.title.toUpperCase(), px + pad, py + pad - 4 * u);
  g.font = `500 ${11.5 * u}px ${F_MONO}`;
  g.fillStyle = 'rgba(255,255,255,0.55)';
  g.fillText(o.sub, px + pad, py + pad + 24 * u);
  // your stack
  g.textAlign = 'right';
  g.font = `800 ${22 * u}px ${F_SANS}`;
  g.fillStyle = '#FDE68A';
  g.fillText(`${chipsText(o.chips)}`, px + pw - pad, py + pad - 4 * u);
  g.font = `500 ${11 * u}px ${F_MONO}`;
  g.fillStyle = 'rgba(253,230,138,0.65)';
  g.fillText('PLAY CHIPS · not money', px + pw - pad, py + pad + 24 * u);
  // footer
  g.textBaseline = 'middle';
  g.textAlign = 'right';
  g.font = `500 ${11.5 * u}px ${F_MONO}`;
  g.fillStyle = 'rgba(255,255,255,0.5)';
  g.fillText(`${o.hint}  ·  Esc leave`, px + pw - pad, py + ph - 16 * u);
  return { x: px + pad, y: py + pad + 48 * u, w: pw - pad * 2, h: ph - pad - 48 * u - 30 * u };
}

/** A button: its label, and the key that does the same, in a cap. */
export function button(
  g: CanvasRenderingContext2D, v: View, out: Btn[], id: string, x: number, y: number, w: number, h: number,
  label: string, o: { key?: string; color?: string; enabled?: boolean; hot?: boolean; small?: boolean } = {},
): void {
  const { u } = v;
  const enabled = o.enabled !== false;
  const color = o.color ?? '#A78BFA';
  rr(g, x, y, w, h, 9 * u);
  g.fillStyle = !enabled ? 'rgba(255,255,255,0.04)' : o.hot ? color : 'rgba(255,255,255,0.08)';
  g.fill();
  g.strokeStyle = enabled ? color : 'rgba(255,255,255,0.12)';
  g.lineWidth = 1.2 * u;
  g.globalAlpha = enabled ? 0.9 : 1;
  g.stroke();
  g.globalAlpha = 1;
  g.textBaseline = 'middle';
  g.textAlign = 'center';
  g.font = `700 ${(o.small ? 13 : 15) * u}px ${F_SANS}`;
  g.fillStyle = !enabled ? 'rgba(255,255,255,0.3)' : o.hot ? '#0b0814' : '#ffffff';
  const ky = o.key && !o.small ? -5 * u : 0;
  g.fillText(label, x + w / 2, y + h / 2 + ky);
  if (o.key && !o.small) {
    g.font = `600 ${10 * u}px ${F_MONO}`;
    g.fillStyle = !enabled ? 'rgba(255,255,255,0.2)' : o.hot ? 'rgba(11,8,20,0.7)' : 'rgba(255,255,255,0.45)';
    g.fillText(o.key, x + w / 2, y + h / 2 + 10 * u);
  }
  out.push({ x, y, w, h, id, enabled });
}

export function label(g: CanvasRenderingContext2D, text: string, x: number, y: number, size: number, color: string, o: { align?: CanvasTextAlign; weight?: number; mono?: boolean; base?: CanvasTextBaseline } = {}): void {
  g.textAlign = o.align ?? 'left';
  g.textBaseline = o.base ?? 'middle';
  g.font = `${o.weight ?? 600} ${size}px ${o.mono ? F_MONO : F_SANS}`;
  g.fillStyle = color;
  g.fillText(text, x, y);
}

/** Trim `text` with an ellipsis to fit `w` in the current font. */
export function fit(g: CanvasRenderingContext2D, text: string, w: number): string {
  let s = text;
  while (s.length > 3 && g.measureText(s).width > w) s = `${s.slice(0, -2)}…`;
  return s;
}

/** A result banner over the body. */
export function banner(g: CanvasRenderingContext2D, v: View, b: Box, text: string, color: string, age: number): void {
  const { u } = v;
  const a = Math.min(1, age * 4);
  g.save();
  g.globalAlpha = a;
  g.font = `800 ${26 * u}px ${F_SANS}`;
  const w = g.measureText(text).width + 40 * u;
  const x = b.x + b.w / 2 - w / 2, y = b.y - 6 * u;
  rr(g, x, y, w, 40 * u, 12 * u);
  g.fillStyle = 'rgba(8,6,16,0.9)';
  g.fill();
  g.strokeStyle = color;
  g.lineWidth = 2 * u;
  g.stroke();
  g.fillStyle = color;
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(text, b.x + b.w / 2, y + 21 * u);
  g.restore();
}

export const STAKES = [5, 10, 25, 50, 100, 250, 500, 1000] as const;

export function stepStake(cur: number, dir: 1 | -1, max: number): number {
  const i = STAKES.findIndex((s) => s >= cur);
  const at = i < 0 ? STAKES.length - 1 : i;
  const next = STAKES[Math.max(0, Math.min(STAKES.length - 1, at + dir))];
  return Math.max(STAKES[0], Math.min(next, Math.max(STAKES[0], max)));
}

/** The prompt under the crosshair: "[E] Play Blackjack". */
export function drawPlayPrompt(g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; name: string; sub: string; color: string }): void {
  drawPrompt(g, { W: v.W, H: v.H, u: v.u, label: v.name, sub: v.sub, color: v.color });
}

/** Your stack, top right, while you walk the floor. */
export function drawChipHud(g: CanvasRenderingContext2D, v: { W: number; u: number; chips: number; open: number }): void {
  const { W, u } = v;
  g.save();
  const text = `${chipsText(v.chips)} chips`;
  g.font = `800 ${16 * u}px ${F_SANS}`;
  const w = g.measureText(text).width + 40 * u;
  const x = W - w - 16 * u, y = 74 * u;
  rr(g, x, y, w, 30 * u, 15 * u);
  g.fillStyle = 'rgba(8,6,16,0.66)';
  g.fill();
  g.strokeStyle = 'rgba(253,230,138,0.6)';
  g.lineWidth = 1.2 * u;
  g.stroke();
  g.fillStyle = '#FBBF24';
  g.beginPath(); g.arc(x + 16 * u, y + 15 * u, 7 * u, 0, Math.PI * 2); g.fill();
  g.strokeStyle = '#B45309';
  g.lineWidth = 2 * u;
  g.beginPath(); g.arc(x + 16 * u, y + 15 * u, 4 * u, 0, Math.PI * 2); g.stroke();
  g.fillStyle = '#FDE68A';
  g.textAlign = 'left';
  g.textBaseline = 'middle';
  g.fillText(text, x + 28 * u, y + 16 * u);
  if (v.open > 0) {
    g.font = `500 ${10.5 * u}px ${F_MONO}`;
    g.fillStyle = 'rgba(255,255,255,0.6)';
    g.textAlign = 'right';
    g.fillText(`${v.open} open ticket${v.open === 1 ? '' : 's'}`, x + w - 2 * u, y + 42 * u);
  }
  g.restore();
}

/** The track that just started, and its credit (the music is CC BY), top
 *  centre for a few seconds, like a car radio in a PS2 game. */
export function drawNowPlaying(g: CanvasRenderingContext2D, v: { W: number; u: number; text: string; t: number }): void {
  const { W, u, t } = v;
  const a = Math.min(1, t / 0.4, Math.max(0, (8 - t) / 0.8));
  if (a <= 0) return;
  g.save();
  g.globalAlpha = a;
  g.font = `500 ${11 * u}px ${F_MONO}`;
  const tw = g.measureText(v.text).width;
  const w = tw + 44 * u, h = 26 * u;
  const x = W / 2 - w / 2, y = 14 * u - (1 - Math.min(1, t / 0.4)) * 8 * u;
  rr(g, x, y, w, h, 13 * u);
  g.fillStyle = 'rgba(8,6,16,0.72)';
  g.fill();
  g.strokeStyle = 'rgba(242,198,109,0.55)';
  g.lineWidth = 1.2 * u;
  g.stroke();
  g.fillStyle = '#F2C66D';
  g.textAlign = 'left';
  g.textBaseline = 'middle';
  g.font = `700 ${13 * u}px ${F_SANS}`;
  g.fillText('♪', x + 13 * u, y + h / 2 + 1 * u);
  g.font = `500 ${11 * u}px ${F_MONO}`;
  g.fillStyle = 'rgba(255,255,255,0.88)';
  g.fillText(v.text, x + 30 * u, y + h / 2 + 1 * u);
  g.restore();
}

/** Words floating over a table, anchored to it (tables/*.ts): a 'tag' is a
 *  small pill (a hand's total, a stake), a 'big' one a result that rises
 *  and fades, a 'tip' a quiet note. */
export interface LabelView { x: number; y: number; text: string; sub?: string; color: string; kind: 'tag' | 'big' | 'tip'; age?: number; life?: number }

export function drawWorldLabels(g: CanvasRenderingContext2D, labels: LabelView[], u: number): void {
  g.save();
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  for (const l of labels) {
    if (l.kind === 'big') {
      const age = l.age ?? 0;
      const a = Math.min(1, age / 0.2) * Math.max(0, Math.min(1, ((l.life ?? 3.6) - age) / 0.8));
      if (a <= 0) continue;
      const y = l.y - Math.min(1, age / 0.6) * 22 * u;
      g.globalAlpha = a;
      g.font = `800 ${30 * u}px ${F_SANS}`;
      g.lineWidth = 6 * u;
      g.strokeStyle = 'rgba(0,0,0,0.75)';
      g.strokeText(l.text, l.x, y);
      g.fillStyle = l.color;
      g.fillText(l.text, l.x, y);
      if (l.sub) {
        g.font = `700 ${15 * u}px ${F_SANS}`;
        g.lineWidth = 4 * u;
        g.strokeText(l.sub, l.x, y + 26 * u);
        g.fillStyle = '#ffffff';
        g.fillText(l.sub, l.x, y + 26 * u);
      }
      g.globalAlpha = 1;
    } else if (l.kind === 'tag') {
      g.font = `800 ${17 * u}px ${F_SANS}`;
      const tw = g.measureText(l.text).width;
      g.font = `600 ${9 * u}px ${F_MONO}`;
      const sw = l.sub ? g.measureText(l.sub).width : 0;
      const w = Math.max(tw, sw) + 18 * u, h = (l.sub ? 38 : 26) * u;
      rr(g, l.x - w / 2, l.y - h, w, h, 8 * u);
      g.fillStyle = 'rgba(8,6,16,0.8)';
      g.fill();
      g.strokeStyle = l.color;
      g.globalAlpha = 0.8;
      g.lineWidth = 1.4 * u;
      g.stroke();
      g.globalAlpha = 1;
      if (l.sub) {
        g.fillStyle = 'rgba(255,255,255,0.55)';
        g.fillText(l.sub, l.x, l.y - h + 10 * u);
      }
      g.font = `800 ${17 * u}px ${F_SANS}`;
      g.fillStyle = l.color;
      g.fillText(l.text, l.x, l.y - (l.sub ? 13 : 13) * u);
    } else {
      g.font = `600 ${12 * u}px ${F_MONO}`;
      g.lineWidth = 3 * u;
      g.strokeStyle = 'rgba(0,0,0,0.7)';
      g.strokeText(l.text, l.x, l.y);
      g.fillStyle = l.color;
      g.fillText(l.text, l.x, l.y);
    }
  }
  g.restore();
}

/** What's under the cursor at a table, beside it. */
export function drawTooltip(g: CanvasRenderingContext2D, v: { x: number; y: number; text: string; u: number; W: number }): void {
  const { u } = v;
  g.save();
  g.font = `600 ${12.5 * u}px ${F_SANS}`;
  const w = g.measureText(v.text).width + 20 * u, h = 26 * u;
  let x = v.x + 16 * u;
  if (x + w > v.W - 8 * u) x = v.x - w - 12 * u;
  const y = v.y + 14 * u;
  rr(g, x, y, w, h, 7 * u);
  g.fillStyle = 'rgba(8,6,16,0.86)';
  g.fill();
  g.strokeStyle = 'rgba(253,230,138,0.55)';
  g.lineWidth = 1.2 * u;
  g.stroke();
  g.fillStyle = '#ffffff';
  g.textAlign = 'left';
  g.textBaseline = 'middle';
  g.fillText(v.text, x + 10 * u, y + h / 2 + 1 * u);
  g.restore();
}

/** The keys, in one thin line along the bottom while you play. */
export function drawHintBar(g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; text: string }): void {
  const { u } = v;
  g.save();
  g.font = `600 ${11.5 * u}px ${F_MONO}`;
  const w = g.measureText(v.text).width + 28 * u, h = 24 * u;
  const x = v.W / 2 - w / 2, y = v.H - h - 12 * u;
  rr(g, x, y, w, h, 12 * u);
  g.fillStyle = 'rgba(8,6,16,0.6)';
  g.fill();
  g.fillStyle = 'rgba(255,255,255,0.78)';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(v.text, v.W / 2, y + h / 2 + 1 * u);
  g.restore();
}
