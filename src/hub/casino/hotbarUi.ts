import { F_PIXEL } from '../engine/overlay';
import { buzzWord, CYLINDER, HOTBAR, type Hotbar, type ItemId } from './hotbar';
import { rr } from './ui';

// The hotbar along the bottom of the screen while you walk the casino: five
// slots, the one in your paw raised and lit, each with a 16×16 pixel-art
// icon (the PS2 had a HUD of sprites, not vectors). A buzz meter above it
// once you've been drinking, the revolver's rounds, and a crosshair while
// it's out.

/** 16×16 sprites, one character a pixel; '.' is clear. */
export const ICONS: Record<ItemId, { px: string[]; pal: Record<string, string> }> = {
  hand: {
    pal: { k: '#3a1520', p: '#e7a3b3', P: '#c46f86', s: '#f4f1ea', S: '#2a2d38' },
    px: [
      '................',
      '....k.k.k.k.....',
      '...kpkpkpkpk....',
      '...kpkpkpkpk....',
      '...kpkpkpkpk....',
      '...kpppppppk.k..',
      '...kpppppppkkpk.',
      '...kppppppppppk.',
      '...kpppPpppppk..',
      '...kppppppppk...',
      '....kpppppppk...',
      '.....kppPpppk...',
      '......kppppk....',
      '......kssssk....',
      '......kSSSSk....',
      '......kSSSSk....',
    ],
  },
  cig: {
    pal: { k: '#2a1a10', o: '#d98a3a', O: '#a35f1f', w: '#f6f2e8', a: '#9a948c', e: '#ff5a1f', y: '#ffd166', s: '#cfcac4' },
    px: [
      '................',
      '............s...',
      '.............s..',
      '.............s..',
      '............s...',
      '...........s....',
      '............s...',
      '................',
      'kkkkkkkkkkkkkk..',
      'koooowwwwwwwwaek',
      'kooOowwwwwwwwaey',
      'koooowwwwwwwwaek',
      'kkkkkkkkkkkkkk..',
      '................',
      '................',
      '................',
    ],
  },
  beer: {
    pal: { k: '#140c06', g: '#e7c26a', b: '#6b3a12', h: '#c98a4a', B: '#3f2008', l: '#f2e2b0', r: '#b91c1c' },
    px: [
      '......kggk......',
      '......kggk......',
      '......kbbk......',
      '......kbhk......',
      '......kbhk......',
      '.....kbbhbk.....',
      '....kbbbhbbk....',
      '....kbbbhbbk....',
      '....kllllllk....',
      '....klrrrrlk....',
      '....kllllllk....',
      '....kbbbhbbk....',
      '....kbbbhbbk....',
      '....kbbbhbbk....',
      '....kBBBBBBk....',
      '.....kkkkkk.....',
    ],
  },
  vodka: {
    pal: { k: '#1b2a33', r: '#c81e1e', c: '#d8eef8', w: '#ffffff', q: '#a9d6ee', l: '#f8f8f8' },
    px: [
      '......krrk......',
      '......krrk......',
      '......kcwk......',
      '......kcwk......',
      '......kcwk......',
      '.....kccwck.....',
      '....kcccwcck....',
      '....kqqqwqqk....',
      '....krrrrrrk....',
      '....krllllrk....',
      '....krrrrrrk....',
      '....kqqqwqqk....',
      '....kqqqwqqk....',
      '....kqqqwqqk....',
      '....kqqqqqqk....',
      '.....kkkkkk.....',
    ],
  },
  gun: {
    pal: { k: '#0b0f14', S: '#1f2937', s: '#4b5563', h: '#9ca3af', w: '#7c4a1e', W: '#4a2a0e' },
    px: [
      '................',
      '................',
      '................',
      '...kk........kk.',
      '..kSSkkkkkkkkSSk',
      '..kSshhhhhhhhhhk',
      '..kssssssssssssk',
      '..kssSsSskkkkkkk',
      '..ksssssk.......',
      '.kwwkSkSk.......',
      '.kwwk.kk........',
      'kwwwk...........',
      'kwWwk...........',
      'kwWWk...........',
      '.kkk............',
      '................',
    ],
  },
  cocktail: {
    // 'l' is the drink's own colour, filled in when there is one
    pal: { k: '#1b2a33', w: '#ffffff', l: '#8a8a8a', g: '#d9b05a', o: '#6a8f2a', s: '#cfe8f5' },
    px: [
      '................',
      '...........gg...',
      '.kkkkkkkkkgkkkk.',
      '..kwllllgllllk..',
      '...kwlllolllk...',
      '....kwlloolk....',
      '.....kwlllk.....',
      '......kllk......',
      '.......kk.......',
      '.......ss.......',
      '.......ss.......',
      '.......ss.......',
      '.......ss.......',
      '.....kssssk.....',
      '....kkkkkkkk....',
      '................',
    ],
  },
};

/** A hotbar icon, `px` screen pixels to the sprite's pixel. */
export function drawIcon(g: CanvasRenderingContext2D, id: ItemId, x: number, y: number, px: number, over?: Record<string, string>): void {
  sprite(g, id, x, y, px, over);
}

function sprite(g: CanvasRenderingContext2D, id: ItemId, x: number, y: number, px: number, over?: Record<string, string>): void {
  const { px: rows } = ICONS[id];
  const pal = over ? { ...ICONS[id].pal, ...over } : ICONS[id].pal;
  for (let j = 0; j < rows.length; j++) {
    const row = rows[j];
    for (let i = 0; i < row.length; i++) {
      const c = pal[row[i]];
      if (!c) continue;
      g.fillStyle = c;
      g.fillRect(Math.round(x + i * px), Math.round(y + j * px), Math.ceil(px), Math.ceil(px));
    }
  }
}

export function drawHotbar(g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; hb: Hotbar; locked: boolean }): void {
  const { W, H, u, hb } = v;
  const n = HOTBAR.length;
  const s = 50 * u, gap = 6 * u;
  const w = n * s + (n - 1) * gap;
  const x0 = W / 2 - w / 2;
  const y0 = H - s - 16 * u;
  g.save();
  g.textBaseline = 'middle';
  for (let i = 0; i < n; i++) {
    const it = HOTBAR[i];
    const on = i === hb.slot;
    const x = x0 + i * (s + gap);
    const y = y0 - (on ? 6 * u : 0);
    rr(g, x, y, s, s, 6 * u);
    g.fillStyle = on ? 'rgba(40,26,8,0.88)' : 'rgba(8,6,16,0.72)';
    g.fill();
    g.lineWidth = (on ? 2.2 : 1.2) * u;
    g.strokeStyle = on ? '#F2C66D' : 'rgba(255,255,255,0.22)';
    g.stroke();
    // a bottle you've finished shows empty until the next one comes
    const empty = (it.id === 'beer' || it.id === 'vodka') ? hb.level[it.id] <= 0 : it.id === 'cocktail' && !hb.cocktail;
    g.globalAlpha = empty ? 0.35 : 1;
    const over = it.id === 'cocktail' ? { l: hb.cocktail?.color ?? 'rgba(0,0,0,0)', o: hb.cocktail?.garnish === 'cherry' ? '#c8102e' : hb.cocktail?.garnish === 'lime' ? '#7ac943' : hb.cocktail?.garnish === 'peel' ? '#f39c12' : '#6a8f2a' } : undefined;
    sprite(g, it.id, x + (s - 16 * 2.4 * u) / 2, y + 4 * u, 2.4 * u, over);
    g.globalAlpha = 1;
    // the key
    g.font = `${7 * u}px ${F_PIXEL}`;
    g.textAlign = 'left';
    g.fillStyle = on ? '#FDE68A' : 'rgba(255,255,255,0.5)';
    g.fillText(String(i + 1), x + 4 * u, y + 7 * u);
    // what's left: the bottles' level, the cigarette, the rounds
    const bar = (k: number, color: string) => {
      g.fillStyle = 'rgba(0,0,0,0.55)';
      g.fillRect(x + 6 * u, y + s - 6 * u, s - 12 * u, 3 * u);
      g.fillStyle = color;
      g.fillRect(x + 6 * u, y + s - 6 * u, (s - 12 * u) * Math.max(0, Math.min(1, k)), 3 * u);
    };
    if (it.id === 'beer') bar(hb.level.beer, '#F2C66D');
    if (it.id === 'vodka') bar(hb.level.vodka, '#BFE6FF');
    if (it.id === 'cocktail' && hb.cocktail) bar(hb.level.cocktail, hb.cocktail.color);
    if (it.id === 'cig') bar(hb.lit ? (hb.cig - 0.2) / 0.8 : 0, '#F4F1EA');
    if (it.id === 'gun') {
      for (let r = 0; r < CYLINDER; r++) {
        g.fillStyle = r < hb.ammo ? '#F2C66D' : 'rgba(255,255,255,0.18)';
        g.fillRect(x + 7 * u + r * 6.4 * u, y + s - 7 * u, 4 * u, 4 * u);
      }
    }
  }

  // the name of what you just picked up, a moment, then it fades
  const fade = Math.max(0, Math.min(1, 2.2 - hb.picked));
  if (fade > 0) {
    const it = HOTBAR[hb.slot];
    g.globalAlpha = fade;
    g.textAlign = 'center';
    g.font = `${10 * u}px ${F_PIXEL}`;
    g.fillStyle = '#ffffff';
    g.fillText(hb.nameOf(hb.slot).toUpperCase(), W / 2, y0 - 36 * u);
    g.font = `${7.5 * u}px ${F_PIXEL}`;
    const use = v.locked ? 'click or F' : 'F';
    const hint = it.id === 'hand' ? '1-6 to pick · Q next' : it.id === 'cocktail' && !hb.cocktail ? 'order one at the club bar' : `${use} · ${it.hint}`;
    const hw = g.measureText(hint).width + 16 * u;
    g.fillStyle = 'rgba(0,0,0,0.55)';
    g.fillRect(W / 2 - hw / 2, y0 - 25 * u, hw, 16 * u);
    g.fillStyle = 'rgba(255,255,255,0.85)';
    g.fillText(hint, W / 2, y0 - 16.5 * u);
    g.globalAlpha = 1;
  }

  // the buzz, once there is one
  if (hb.buzz > 0.01) {
    const bw = w * 0.6, bx = W / 2 - bw / 2, by = y0 - (fade > 0 ? 56 : 18) * u;
    g.fillStyle = 'rgba(0,0,0,0.55)';
    g.fillRect(bx, by, bw, 5 * u);
    g.fillStyle = hb.buzz > 0.85 ? '#F87171' : hb.buzz > 0.6 ? '#FB923C' : '#F2C66D';
    g.fillRect(bx, by, bw * hb.buzz, 5 * u);
    g.font = `${7.5 * u}px ${F_PIXEL}`;
    g.textAlign = 'right';
    g.fillStyle = 'rgba(255,255,255,0.7)';
    g.fillText('BUZZ', bx - 6 * u, by + 3 * u);
    g.textAlign = 'left';
    g.fillText(buzzWord(hb.buzz).toUpperCase(), bx + bw + 6 * u, by + 3 * u);
  }
  g.restore();
}

/** A small cross at the centre while the revolver's out; its gap opens with
 *  the kick and with every drink in you. */
export function drawCrosshair(g: CanvasRenderingContext2D, v: { W: number; H: number; u: number; spread: number; reloading: boolean }): void {
  const { W, H, u } = v;
  const cx = Math.round(W / 2), cy = Math.round(H / 2);
  const gap = (5 + v.spread * 12) * u, len = 8 * u;
  g.save();
  g.globalAlpha = v.reloading ? 0.35 : 0.9;
  for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
    const x1 = cx + dx * gap, y1 = cy + dy * gap;
    const x2 = cx + dx * (gap + len), y2 = cy + dy * (gap + len);
    g.strokeStyle = 'rgba(0,0,0,0.8)';
    g.lineWidth = 4 * u;
    g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
    g.strokeStyle = '#ffffff';
    g.lineWidth = 2 * u;
    g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
  }
  g.restore();
}
