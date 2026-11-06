import { describe, expect, it } from 'vitest';
import { CYLINDER, HOTBAR, Hotbar, buzzWord, type HotbarEvent } from '../src/hub/casino/hotbar';
import { ICONS } from '../src/hub/casino/hotbarUi';
import { COCKTAILS } from '../src/hub/casino/bar';
import { rayPastRat, wallBetween } from '../src/hub/engine/shots';

/** Run the hotbar for `secs` in small steps; collect what it said. */
function run(hb: Hotbar, secs: number, dt = 1 / 60): HotbarEvent[] {
  const ev: HotbarEvent[] = [];
  for (let t = 0; t < secs; t += dt) ev.push(...hb.update(dt));
  return ev;
}

function holding(slot: number): Hotbar {
  const hb = new Hotbar();
  hb.select(slot);
  run(hb, 0.5);
  return hb;
}

const kinds = (ev: HotbarEvent[]) => ev.map((e) => e.k);

describe('hotbar: picking', () => {
  it('starts with empty paws and swaps through a lower and a raise', () => {
    const hb = new Hotbar();
    expect(hb.item).toBe('hand');
    hb.select(HOTBAR.findIndex((h) => h.id === 'gun'));
    expect(hb.item).toBe('gun');
    expect(hb.shown).toBe('hand');
    run(hb, 0.5);
    expect(hb.shown).toBe('gun');
    expect(hb.ready).toBe(true);
  });

  it('cycles round, both ways', () => {
    const hb = new Hotbar();
    hb.cycle(-1);
    expect(hb.slot).toBe(HOTBAR.length - 1);
    hb.cycle(1);
    expect(hb.slot).toBe(0);
  });

  it('ignores a slot that is not there', () => {
    const hb = new Hotbar();
    hb.select(9);
    expect(hb.slot).toBe(0);
  });
});

describe('hotbar: the revolver', () => {
  const GUN = HOTBAR.findIndex((h) => h.id === 'gun');

  it('fires once per press, six to a cylinder, then reloads itself', () => {
    const hb = holding(GUN);
    let shots = 0;
    for (let i = 0; i < CYLINDER; i++) {
      shots += kinds(hb.press()).filter((k) => k === 'shot').length;
      hb.release();
      run(hb, 0.4);
    }
    expect(shots).toBe(CYLINDER);
    expect(hb.ammo).toBe(0);
    // the empty gun reloads on its own once the last kick is done
    const ev = run(hb, 2);
    expect(kinds(ev)).toContain('reloaded');
    expect(hb.ammo).toBe(CYLINDER);
  });

  it('will not fire faster than its hammer', () => {
    const hb = holding(GUN);
    expect(kinds(hb.press())).toEqual(['shot']);
    hb.release();
    expect(hb.press()).toEqual([]);
  });

  it('reloads on R only when a round is gone, and not mid-swap', () => {
    const hb = holding(GUN);
    expect(hb.reloadGun()).toBe(false);
    hb.press();
    hb.release();
    expect(hb.reloadGun()).toBe(true);
    expect(hb.press()).toEqual([]); // no firing mid-reload
  });

  it('a reload does not finish in your pocket', () => {
    const hb = holding(GUN);
    hb.press(); hb.release();
    hb.reloadGun();
    hb.select(0);
    run(hb, 2);
    hb.select(GUN);
    run(hb, 0.5);
    expect(hb.ammo).toBe(CYLINDER - 1);
  });
});

describe('hotbar: the cigarette', () => {
  const CIG = HOTBAR.findIndex((h) => h.id === 'cig');

  it('a held drag burns it and exhales on release', () => {
    const hb = holding(CIG);
    const before = hb.cig;
    hb.press();
    run(hb, 1.5);
    expect(hb.raise).toBeGreaterThan(0.9);
    expect(hb.cig).toBeLessThan(before - 0.05);
    const ev = hb.release();
    expect(ev).toEqual([expect.objectContaining({ k: 'exhale' })]);
    expect(hb.exhale).toBeGreaterThan(0);
  });

  it('a tap is not a drag', () => {
    const hb = holding(CIG);
    hb.press();
    run(hb, 0.1);
    expect(hb.release()).toEqual([]);
  });

  it('smoked down to the filter, it is flicked and a new one lit', () => {
    const hb = holding(CIG);
    hb.press();
    const ev = kinds(run(hb, 12));
    expect(ev).toContain('flick');
    expect(ev).toContain('lit');
    expect(hb.cig).toBeGreaterThan(0.5);
  });

  it('does not get you drunk', () => {
    const hb = holding(CIG);
    hb.press();
    run(hb, 3);
    expect(hb.buzz).toBe(0);
  });
});

describe('hotbar: the bottles', () => {
  const BEER = HOTBAR.findIndex((h) => h.id === 'beer');
  const VODKA = HOTBAR.findIndex((h) => h.id === 'vodka');

  it('a beer is a light buzz, a vodka a heavy one', () => {
    const beer = holding(BEER);
    beer.press();
    run(beer, 12);
    const vodka = holding(VODKA);
    vodka.press();
    run(vodka, 6);
    expect(beer.buzz).toBeGreaterThan(0.2);
    expect(beer.buzz).toBeLessThan(0.4);
    expect(vodka.buzz).toBeGreaterThan(beer.buzz);
  });

  it('a finished bottle is replaced', () => {
    const hb = holding(BEER);
    hb.press();
    const ev = run(hb, 11);
    expect(ev).toContainEqual({ k: 'empty', item: 'beer' });
    hb.release();
    run(hb, 2.5);
    expect(hb.level.beer).toBe(1);
  });

  it('a swig of vodka bites', () => {
    const hb = holding(VODKA);
    hb.press();
    run(hb, 1.5);
    expect(kinds(hb.release())).toContain('burn');
  });

  it('you sober up', () => {
    const hb = holding(BEER);
    hb.press();
    run(hb, 5);
    hb.release();
    const b = hb.buzz;
    run(hb, 10);
    expect(hb.buzz).toBeLessThan(b);
  });

  it('too much and you black out, and come to sobered a bit with empty paws', () => {
    const hb = holding(VODKA);
    hb.press();
    let out = false;
    for (let t = 0; t < 60 && !out; t += 1 / 60) {
      out = kinds(hb.update(1 / 60)).includes('blackout');
      // keep drinking: a finished bottle comes back and you go again
      if (!hb.using) hb.press();
      if (hb.level.vodka <= 0) { hb.release(); hb.press(); }
    }
    expect(out).toBe(true);
    expect(hb.blackAlpha).toBeGreaterThan(0);
    expect(hb.press()).toEqual([]); // nothing works while you're out
    const after = kinds(run(hb, 4));
    expect(after).toContain('wake');
    expect(hb.black).toBe(0);
    expect(hb.buzz).toBeLessThan(0.5);
    expect(hb.item).toBe('hand');
  });

  it('names the buzz', () => {
    expect(buzzWord(0)).toBe('sober');
    expect(buzzWord(0.5)).toBe('buzzed');
    expect(buzzWord(0.95)).toBe('seeing double');
  });
});

describe('hotbar: a cocktail', () => {
  const SLOT = HOTBAR.findIndex((h) => h.id === 'cocktail');

  it('slot six is empty until you order one', () => {
    const hb = holding(SLOT);
    expect(hb.cocktail).toBeNull();
    hb.press();
    run(hb, 2);
    expect(hb.buzz).toBe(0);
    expect(hb.nameOf(SLOT)).toBe('Cocktail');
  });

  it('a whole glass puts its strength in you, then it is gone', () => {
    const hb = holding(SLOT);
    const c = COCKTAILS.find((x) => x.id === 'manhattan')!;
    hb.giveCocktail(c);
    expect(hb.nameOf(SLOT)).toBe(c.name);
    run(hb, 0.5);
    hb.press();
    const ev = run(hb, 12);
    expect(ev).toContainEqual({ k: 'empty', item: 'cocktail' });
    expect(hb.buzz).toBeGreaterThan(c.strength - 0.06);
    expect(hb.cocktail).toBeNull();
    hb.release();
    run(hb, 3);
    expect(hb.level.cocktail).toBe(0); // no refill: order another
  });
});

describe('hotbar icons', () => {
  it('are 16×16 and use only their own palette', () => {
    for (const [id, icon] of Object.entries(ICONS)) {
      expect(icon.px.length, id).toBe(16);
      for (const row of icon.px) {
        expect(row.length, `${id}: ${row}`).toBe(16);
        for (const c of row) if (c !== '.') expect(icon.pal[c], `${id} '${c}'`).toBeDefined();
      }
    }
  });

  it('cover every slot', () => {
    for (const h of HOTBAR) expect(ICONS[h.id]).toBeDefined();
  });
});

describe('shots', () => {
  const eye = { x: 0, y: 1.6, z: 0 };

  it('a shot straight at a rat passes it at zero', () => {
    const r = rayPastRat(eye, { x: 0, y: 0, z: -1 }, 0, -3, 10);
    expect(r?.t).toBeCloseTo(3);
    expect(r?.off).toBeCloseTo(0);
  });

  it('never behind you, past the wall, or over its head', () => {
    expect(rayPastRat(eye, { x: 0, y: 0, z: -1 }, 0, 3, 10)).toBeNull();
    expect(rayPastRat(eye, { x: 0, y: 0, z: -1 }, 0, -3, 2)).toBeNull();
    const up = { x: 0, y: 0.8, z: -0.6 };
    expect(rayPastRat(eye, up, 0, -3, 10)).toBeNull();
  });

  it('measures a near miss', () => {
    const r = rayPastRat(eye, { x: 0, y: 0, z: -1 }, 0.3, -4, 10);
    expect(r?.off).toBeCloseTo(0.3);
  });

  it('sees walls between you and a rat', () => {
    const walls = [[-1, -2, 1, -2]] as const;
    expect(wallBetween(0, 0, 0, -4, walls)).toBe(true);
    expect(wallBetween(0, 0, 0, -1, walls)).toBe(false);
    expect(wallBetween(0, 0, 3, -4, walls)).toBe(false);
  });
});
