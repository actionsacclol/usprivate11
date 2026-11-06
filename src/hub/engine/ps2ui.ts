// (the fonts are overlay.ts's; repeated here so overlay.ts can import this)
const F_SANS = '"Chakra Petch", system-ui, sans-serif';
const F_PIXEL = '"Press Start 2P", monospace';

// The casino's talking UI, in the PS2's own idiom: hard-edged beveled boxes
// with a gradient fill and a double border, a name tab in the speaker's
// colour, bold text with a hard black drop shadow (no soft glow, no rounded
// pills), a blinking ▶ cursor on the choice you're on and a ▼ when a line
// waits for you. Shared by the speech bubbles (overlay.ts), the dialogue
// and the prompts (talkui.ts).

/** Snap to whole pixels: hard edges stay hard. */
const px = (v: number) => Math.round(v);

/** The box: a hard drop shadow, a navy-to-black fill, an ivory outer line,
 *  an inner line in `accent`, and a square of it in each corner. */
export function ps2Box(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, u: number, accent: string, alpha = 0.9): void {
  x = px(x); y = px(y); w = px(w); h = px(h);
  const b = Math.max(1, px(2 * u));
  g.save();
  g.fillStyle = 'rgba(0,0,0,0.55)';
  g.fillRect(x + b * 2, y + b * 2, w, h);
  const grd = g.createLinearGradient(0, y, 0, y + h);
  grd.addColorStop(0, `rgba(30,22,64,${alpha})`);
  grd.addColorStop(0.55, `rgba(14,10,34,${alpha})`);
  grd.addColorStop(1, `rgba(5,4,12,${alpha})`);
  g.fillStyle = grd;
  g.fillRect(x, y, w, h);
  // a lighter band along the top: the bevel
  g.fillStyle = 'rgba(255,255,255,0.07)';
  g.fillRect(x, y, w, Math.max(b * 3, px(h * 0.18)));
  g.strokeStyle = '#efe6cf';
  g.lineWidth = b;
  g.strokeRect(x + b / 2, y + b / 2, w - b, h - b);
  g.strokeStyle = accent;
  g.lineWidth = Math.max(1, px(u));
  const i = b * 2;
  g.strokeRect(x + i + 0.5, y + i + 0.5, w - i * 2 - 1, h - i * 2 - 1);
  const c = b * 2;
  g.fillStyle = accent;
  for (const [cx, cy] of [[x, y], [x + w - c, y], [x, y + h - c], [x + w - c, y + h - c]]) g.fillRect(cx, cy, c, c);
  g.restore();
}

/** Text with a hard black shadow and outline, the way PS2 subtitles read
 *  over anything. */
export function ps2Text(
  g: CanvasRenderingContext2D, text: string, x: number, y: number, size: number, color: string,
  o: { weight?: number; pixel?: boolean; align?: CanvasTextAlign; base?: CanvasTextBaseline; u?: number } = {},
): void {
  const u = o.u ?? 1;
  g.font = o.pixel ? `${px(size)}px ${F_PIXEL}` : `${o.weight ?? 700} ${px(size)}px ${F_SANS}`;
  g.textAlign = o.align ?? 'left';
  g.textBaseline = o.base ?? 'middle';
  const s = Math.max(1, px(2 * u));
  g.fillStyle = 'rgba(0,0,0,0.85)';
  g.fillText(text, px(x) + s, px(y) + s);
  g.lineWidth = Math.max(1, px(2 * u));
  g.lineJoin = 'miter';
  g.strokeStyle = 'rgba(0,0,0,0.9)';
  g.strokeText(text, px(x), px(y));
  g.fillStyle = color;
  g.fillText(text, px(x), px(y));
}

/** The speaker's name on a tab over the box's top-left corner. */
export function nameTab(g: CanvasRenderingContext2D, x: number, y: number, name: string, color: string, u: number): number {
  g.font = `${px(11 * u)}px ${F_PIXEL}`;
  const w = px(g.measureText(name).width + 22 * u), h = px(24 * u);
  const b = Math.max(1, px(2 * u));
  g.fillStyle = 'rgba(0,0,0,0.55)';
  g.fillRect(px(x) + b * 2, px(y) + b * 2, w, h);
  g.fillStyle = color;
  g.fillRect(px(x), px(y), w, h);
  g.fillStyle = 'rgba(0,0,0,0.25)';
  g.fillRect(px(x), px(y) + h - b * 2, w, b * 2);
  g.strokeStyle = '#efe6cf';
  g.lineWidth = b;
  g.strokeRect(px(x) + b / 2, px(y) + b / 2, w - b, h - b);
  g.fillStyle = '#0b0614';
  g.textAlign = 'left';
  g.textBaseline = 'middle';
  g.fillText(name, px(x + 11 * u), px(y + h / 2 + 1 * u));
  return w;
}

/** A key, as a button glyph: a square with its letter. */
export function keyGlyph(g: CanvasRenderingContext2D, x: number, y: number, key: string, u: number): number {
  g.font = `${px(10 * u)}px ${F_PIXEL}`;
  const w = px(Math.max(22 * u, g.measureText(key).width + 12 * u)), h = px(22 * u);
  const b = Math.max(1, px(2 * u));
  g.fillStyle = '#efe6cf';
  g.fillRect(px(x), px(y - h / 2), w, h);
  g.fillStyle = 'rgba(0,0,0,0.3)';
  g.fillRect(px(x), px(y + h / 2) - b * 2, w, b * 2);
  g.fillStyle = '#0b0614';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(key, px(x + w / 2), px(y + 1 * u));
  return w;
}

/** The blinking ▶ (a choice) or ▼ (waiting on you), drawn as pixels. */
export function cursor(g: CanvasRenderingContext2D, x: number, y: number, kind: 'right' | 'down', u: number, t: number, color = '#FDE68A'): void {
  if (Math.sin(t * 9) < -0.2) return;
  const s = Math.max(2, px(3 * u));
  g.fillStyle = 'rgba(0,0,0,0.8)';
  const draw = (ox: number, oy: number, c: string) => {
    g.fillStyle = c;
    for (let i = 0; i < 4; i++) {
      if (kind === 'right') g.fillRect(px(x) + ox + i * s, px(y) + oy - (3 - i) * s, s, (3 - i) * 2 * s + s);
      else g.fillRect(px(x) + ox - (3 - i) * s, px(y) + oy + i * s, (3 - i) * 2 * s + s, s);
    }
  };
  draw(s, s, 'rgba(0,0,0,0.8)');
  draw(0, 0, color);
}
