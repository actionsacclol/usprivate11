import type { Talk } from './dialogue';
import { cursor, keyGlyph, nameTab, ps2Box, ps2Text } from './ps2ui';

// The talk prompt and the dialogue box, drawn into the scene's own canvas
// like every other word on it — so they are in Fullscreen and in a recording,
// exactly where they are on the page. PS2-era: a beveled box along the
// bottom, the speaker's name on a tab over it, the line typed out in bold
// with a hard shadow, a blinking ▶ on the choice you're on and a ▼ when the
// line waits for you (ps2ui.ts).

export interface Rect { x: number; y: number; w: number; h: number }

function wrap(g: CanvasRenderingContext2D, text: string, maxW: number): string[] {
  const out: string[] = [];
  for (const para of text.split('\n')) {
    let line = '';
    for (const word of para.split(' ')) {
      const t = line ? `${line} ${word}` : word;
      if (line && g.measureText(t).width > maxW) { out.push(line); line = word; } else line = t;
    }
    out.push(line);
  }
  return out;
}

/** "[E] Talk to Ada Vantablack", under the crosshair: a small box, the key
 *  as a button glyph. */
export function drawTalkPrompt(g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; name: string; title: string; color: string }): void {
  drawPrompt(g, { ...v, label: `Talk to ${v.name}`, sub: v.title });
}

/** The same box for anything you can use (a table, the cashier, the bar). */
export function drawPrompt(g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; label: string; sub: string; color: string }): void {
  const { W, H, u } = v;
  g.save();
  g.font = `700 ${17 * u}px "Chakra Petch", sans-serif`;
  const lw = g.measureText(v.label).width;
  g.font = `600 ${11 * u}px "Chakra Petch", sans-serif`;
  const tw = g.measureText(v.sub).width;
  const w = Math.max(lw + 56 * u, tw + 28 * u) + 16 * u;
  const h = 60 * u;
  const x = W / 2 - w / 2, y = H * 0.6;
  ps2Box(g, x, y, w, h, u, v.color);
  const kw = keyGlyph(g, x + 14 * u, y + 22 * u, 'E', u);
  ps2Text(g, v.label, x + 14 * u + kw + 10 * u, y + 22 * u, 17 * u, '#ffffff', { u });
  ps2Text(g, v.sub, x + 14 * u, y + 44 * u, 11 * u, 'rgba(239,230,207,0.75)', { weight: 600, u });
  g.restore();
}

/** The conversation. Returns the option boxes, for clicks. */
export function drawDialogue(g: CanvasRenderingContext2D, t: Talk, v: { W: number; H: number; u: number; color: string; t: number }): Rect[] {
  const { W, H, u } = v;
  const pw = Math.min(W - 48 * u, 900 * u);
  const pad = 24 * u;
  const textW = pw - pad * 2;
  g.save();

  const size = 19 * u;
  g.font = `700 ${size}px "Chakra Petch", sans-serif`;
  const full = wrap(g, t.line, textW);
  const opts = t.mode === 'choose' ? t.options : [];
  const optH = 30 * u;
  const lineH = 27 * u;
  const ph = pad + 22 * u + full.length * lineH + (opts.length ? 12 * u + opts.length * optH : 0) + 28 * u;
  const px = W / 2 - pw / 2;
  const py = H - ph - 30 * u;

  ps2Box(g, px, py, pw, ph, u, v.color);
  // who's talking, on a tab over the corner; what they do, beside it
  const tw = nameTab(g, px + 14 * u, py - 14 * u, t.persona.name.toUpperCase(), v.color, u);
  ps2Text(g, t.persona.title, px + 14 * u + tw + 10 * u, py - 2 * u, 11 * u, 'rgba(239,230,207,0.8)', { weight: 600, u });

  // the line, typed out
  let y = py + pad + 12 * u;
  const dim = t.mode === 'choose';
  let left = Math.floor(dim ? t.line.length : t.shown);
  for (const ln of full) {
    if (left <= 0) break;
    ps2Text(g, ln.slice(0, left), px + pad, y, size, dim ? 'rgba(255,255,255,0.6)' : '#ffffff', { u });
    left -= ln.length + 1;
    y += lineH;
  }
  y = py + pad + 12 * u + full.length * lineH;

  const rects: Rect[] = [];
  if (opts.length) {
    y += 4 * u;
    opts.forEach((o, i) => {
      const r = { x: px + pad - 8 * u, y: y - optH / 2 + 4 * u, w: textW + 16 * u, h: optH - 2 * u };
      rects.push(r);
      const on = i === t.sel;
      if (on) {
        g.fillStyle = 'rgba(253,230,138,0.12)';
        g.fillRect(Math.round(r.x), Math.round(r.y), Math.round(r.w), Math.round(r.h));
        cursor(g, px + pad - 2 * u, r.y + r.h / 2, 'right', u, v.t);
      }
      ps2Text(g, `${i + 1}.`, px + pad + 18 * u, r.y + r.h / 2, 13 * u, on ? '#FDE68A' : 'rgba(239,230,207,0.6)', { pixel: true, u });
      ps2Text(g, o.text, px + pad + 50 * u, r.y + r.h / 2, 17 * u, on ? '#FDE68A' : o.asked ? 'rgba(255,255,255,0.5)' : '#ffffff', { u });
      y += optH;
    });
  }

  // waiting on you: the ▼; and the keys, small, along the bottom edge
  if (t.mode === 'say' && !t.typing) cursor(g, px + pw - pad - 6 * u, py + ph - 26 * u, 'down', u, v.t);
  const hint = t.mode === 'choose' ? '1-9 OR CLICK · ESC OR MOVE TO LEAVE' : t.typing ? 'E SKIP' : 'E CONTINUE · ESC OR MOVE TO LEAVE';
  ps2Text(g, hint, px + pad, py + ph - 14 * u, 8 * u, 'rgba(239,230,207,0.55)', { pixel: true, u });
  g.restore();
  return rects;
}
