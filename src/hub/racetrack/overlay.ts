import { F_MONO, F_SANS } from '../engine/overlay';

// Race-card type: thin, letter-spaced, monospaced labels with hairline
// leaders — a programme and a tote board, not stickers. The other two scenes
// shout in bubbles; this one annotates.

const EMOJI = '"Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji"';

function font(w: number | string, px: number, fam: string, emoji = false): string {
  return `${w} ${Math.round(px)}px ${fam}${emoji ? `, ${EMOJI}` : ''}`;
}

/** Letter-spaced text (canvas `letterSpacing` isn't in every Chromium we ship). */
function spaced(g: CanvasRenderingContext2D, text: string, x: number, y: number, sp: number, align: 'left' | 'center' | 'right' = 'left'): number {
  let w = 0;
  for (const ch of text) w += g.measureText(ch).width + sp;
  w -= sp;
  let cx = align === 'center' ? x - w / 2 : align === 'right' ? x - w : x;
  const saved = g.textAlign;
  g.textAlign = 'left';
  for (const ch of text) {
    g.fillText(ch, cx, y);
    cx += g.measureText(ch).width + sp;
  }
  g.textAlign = saved;
  return w;
}

export interface Tag { x: number; y: number; text: string; color: string; alpha: number; size: number; dot: boolean }

/** A screen rectangle something already occupies this frame. Every label
 *  the scene draws claims one, and the ones placed later steer round them:
 *  the gate is where runners, their tags, the lane ticks and the
 *  SCRATCHED / OFF calls all bunch up. */
export interface Box { x0: number; y0: number; x1: number; y1: number }

function hits(b: Box, placed: Box[]): boolean {
  for (const p of placed) if (b.x0 < p.x1 && b.x1 > p.x0 && b.y0 < p.y1 && b.y1 > p.y0) return true;
  return false;
}

/** Racer / runner tags, biggest first. Each tries a few spots round its
 *  horse (up-right, higher, up-left, below) and takes the first one clear of
 *  everything already placed; a tag with no clear spot is skipped this frame
 *  rather than drawn over another label. */
export function drawTags(g: CanvasRenderingContext2D, tags: Tag[], u: number, max: number, placed: Box[] = []): void {
  let n = 0;
  for (const t of tags) {
    // a tag on its way out doesn't hold a spot (or ghost over a live one)
    if (n >= max || t.alpha <= 0.25) continue;
    g.font = font(500, 10.5 * u * t.size, F_MONO, true);
    const w = g.measureText(t.text).width;
    const h = 13 * u * t.size;
    const spots: [number, number][] = [[9 * u, -9 * u], [9 * u, -9 * u - h - 6 * u], [-w - 9 * u, -9 * u], [9 * u, h + 10 * u]];
    let box: Box | null = null;
    let x = 0, y = 0;
    for (const [dx, dy] of spots) {
      x = t.x + dx; y = t.y + dy;
      const b = { x0: x - 3 * u, y0: y - h, x1: x + w + 3 * u, y1: y + 3 * u };
      if (!hits(b, placed)) { box = b; break; }
    }
    if (!box) continue;
    placed.push(box);
    n++;
    g.save();
    // hairline leader from the horse to the tag's nearest corner
    g.strokeStyle = t.color;
    g.globalAlpha = t.alpha * 0.55;
    g.lineWidth = 1 * u;
    g.beginPath();
    g.moveTo(t.x, t.y - 2 * u);
    g.lineTo(Math.min(Math.max(t.x, box.x0), box.x1), y < t.y ? box.y1 : box.y0);
    g.stroke();
    g.globalAlpha = t.alpha;
    g.fillStyle = 'rgba(2,6,18,0.62)';
    g.fillRect(box.x0, box.y0, box.x1 - box.x0, box.y1 - box.y0);
    g.fillStyle = t.color;
    g.textBaseline = 'alphabetic';
    g.fillText(t.text, x, y);
    g.restore();
  }
}

export function drawLaneLabels(g: CanvasRenderingContext2D, lanes: { x: number; y: number; text: string }[], caption: { x: number; y: number } | null, u: number, placed: Box[] = []): void {
  g.save();
  g.font = font(500, 9.5 * u, F_MONO);
  g.fillStyle = 'rgba(254,243,199,0.7)';
  g.textBaseline = 'middle';
  for (const l of lanes) {
    g.fillStyle = 'rgba(2,6,18,0.55)';
    const w = g.measureText(l.text).width;
    g.fillRect(l.x - w / 2 - 3 * u, l.y - 7 * u, w + 6 * u, 14 * u);
    placed.push({ x0: l.x - w / 2 - 5 * u, y0: l.y - 8 * u, x1: l.x + w / 2 + 5 * u, y1: l.y + 8 * u });
    g.fillStyle = 'rgba(254,243,199,0.92)';
    g.textAlign = 'center';
    g.fillText(l.text, l.x, l.y + 0.5);
  }
  if (caption) {
    g.font = font(500, 9 * u, F_MONO);
    g.fillStyle = 'rgba(254,243,199,0.5)';
    g.textAlign = 'left';
    spaced(g, 'PRICE LANES · INNER RAIL = FAVOURITE', caption.x, caption.y, 1.2 * u);
  }
  g.restore();
}

export function drawWell(g: CanvasRenderingContext2D, x: number, y: number, title: string, count: number, usd: number, color: string, u: number, pulse: number, placed: Box[] = []): void {
  g.save();
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  // A dark plate under both lines, like the core readout: drawn straight over
  // the well's own glow, the glowing title smeared into an unreadable blob.
  const sign0 = usd > 0 ? '+' : usd < 0 ? '−' : '';
  const sub0 = `${count.toLocaleString('en-US')} · ${sign0}$${Math.abs(usd).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  g.font = font(600, 11.5 * u, F_MONO);
  const pw = Math.max(g.measureText(sub0).width, title.length * 13 * u) + 28 * u;
  const ph = 46 * u;
  placed.push({ x0: x - pw / 2, y0: y - 14 * u, x1: x + pw / 2, y1: y - 14 * u + ph });
  g.fillStyle = 'rgba(4,8,20,0.78)';
  g.strokeStyle = color;
  g.globalAlpha = 0.9;
  g.beginPath();
  g.roundRect(x - pw / 2, y - 14 * u, pw, ph, 8 * u);
  g.fill();
  g.lineWidth = 1 * u;
  g.globalAlpha = 0.45;
  g.stroke();
  g.globalAlpha = 1;
  g.font = font(700, 14 * u, F_MONO);
  g.fillStyle = color;
  g.shadowColor = color;
  g.shadowBlur = (2 + pulse * 10) * u;
  spaced(g, title, x, y, 4 * u, 'center');
  g.shadowBlur = 0;
  g.font = font(600, 11.5 * u, F_MONO);
  g.fillStyle = 'rgba(226,232,240,0.9)';
  const sign = usd > 0 ? '+' : usd < 0 ? '−' : '';
  g.fillText(`${count.toLocaleString('en-US')} · ${sign}$${Math.abs(usd).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`, x, y + 17 * u);
  g.restore();
}

export interface Float { x: number; y: number; text: string; color: string; age: number; life: number; big: boolean; oy?: number }

function floatFont(f: Float, u: number, pop = 1): string {
  return font(700, (f.big ? 22 : 16) * u * pop, F_SANS, true);
}

/** Give each float (a win's amount, SCRATCHED, AND THEY'RE OFF) a clear
 *  spot: newest last, nudged up a line at a time until it overlaps nothing
 *  already placed, then claimed so the tags steer round it too. */
export function layoutFloats(g: CanvasRenderingContext2D, fl: Float[], u: number, placed: Box[]): void {
  g.save();
  for (const f of fl) {
    const k = f.age / f.life;
    if (k >= 1) continue;
    g.font = floatFont(f, u);
    const w = g.measureText(f.text).width;
    const h = (f.big ? 26 : 19) * u;
    const cy = f.y - k * 34 * u;
    let oy = 0;
    let b: Box = { x0: f.x - w / 2 - 4 * u, y0: cy - h / 2, x1: f.x + w / 2 + 4 * u, y1: cy + h / 2 };
    for (let i = 0; i < 5 && hits(b, placed); i++) {
      oy -= h + 2 * u;
      b = { x0: b.x0, y0: cy + oy - h / 2, x1: b.x1, y1: cy + oy + h / 2 };
    }
    // ease into the new spot instead of jumping a line
    f.oy = f.oy === undefined ? oy : f.oy + (oy - f.oy) * 0.35;
    placed.push({ x0: b.x0, y0: cy + f.oy - h / 2, x1: b.x1, y1: cy + f.oy + h / 2 });
  }
  g.restore();
}

export function drawFloats(g: CanvasRenderingContext2D, fl: Float[], u: number): void {
  for (const f of fl) {
    const k = f.age / f.life;
    if (k >= 1) continue;
    g.save();
    g.globalAlpha = Math.min(1, f.age / 0.12) * Math.min(1, (1 - k) / 0.35);
    const pop = 1 + Math.max(0, 0.18 - f.age) * 2.2;
    g.font = floatFont(f, u, pop);
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    g.shadowColor = f.color;
    g.shadowBlur = 12 * u;
    g.fillStyle = f.color;
    g.fillText(f.text, f.x, f.y - k * 34 * u + (f.oy ?? 0));
    g.restore();
  }
}

export function drawLegend(g: CanvasRenderingContext2D, W: number, H: number, u: number): void {
  const items: [string, string][] = [['#EC4899', 'crypto silks'], ['#A855F7', 'script silks']];
  g.save();
  g.font = font(500, 10 * u, F_MONO);
  g.textBaseline = 'middle';
  g.textAlign = 'right';
  // Top-right under the brand: the bottom corners belong to the feed and
  // the LOSSES paddock.
  void H;
  const y1 = 66 * u;
  const y0 = y1 + 16 * u;
  g.fillStyle = 'rgba(253,230,138,0.62)';
  g.fillText('racers = open trades · cap = category · size = stake', W - 18 * u, y0);
  g.fillText('inner lane = favourite · outer lane = longshot', W - 18 * u, y0 + 15 * u);
  let x = W - 18 * u;
  for (const [c, t] of items.reverse()) {
    const w = g.measureText(t).width;
    g.fillStyle = 'rgba(226,232,240,0.75)';
    g.fillText(t, x, y1);
    x -= w + 8 * u;
    g.fillStyle = c;
    g.beginPath();
    g.arc(x, y1, 3.5 * u, 0, Math.PI * 2);
    g.fill();
    x -= 16 * u;
  }
  g.restore();
}

/** A landmark's name on a hairline post: FINISH, STARTING GATE. */
export function drawMarker(g: CanvasRenderingContext2D, x: number, y: number, text: string, color: string, u: number, hot = 0, placed: Box[] = []): void {
  g.save();
  g.font = font(600, 10 * u, F_MONO);
  g.textBaseline = 'middle';
  let w = 0;
  for (const ch of text) w += g.measureText(ch).width + 2 * u;
  const bw = w + 12 * u, bh = 17 * u;
  placed.push({ x0: x - bw / 2 - 2 * u, y0: y - bh / 2 - 2 * u, x1: x + bw / 2 + 2 * u, y1: y + bh / 2 + 2 * u });
  g.fillStyle = 'rgba(6,8,14,0.7)';
  g.strokeStyle = color;
  g.globalAlpha = 0.85;
  g.beginPath();
  g.roundRect(x - bw / 2, y - bh / 2, bw, bh, 4 * u);
  g.fill();
  g.globalAlpha = 0.35 + hot * 0.65;
  g.lineWidth = 1 * u;
  g.stroke();
  g.globalAlpha = 1;
  g.fillStyle = color;
  g.shadowColor = color;
  g.shadowBlur = hot * 12 * u;
  spaced(g, text, x, y + 0.5, 2 * u, 'center');
  g.restore();
}

/** The photo flash: the whole frame blinks white for an instant. */
export function drawFlash(g: CanvasRenderingContext2D, W: number, H: number, k: number): void {
  if (k <= 0.01) return;
  g.save();
  g.globalAlpha = Math.min(0.55, k * k * 0.55);
  g.fillStyle = '#FFFDF5';
  g.fillRect(0, 0, W, H);
  g.restore();
}
