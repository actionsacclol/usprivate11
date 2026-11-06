import { describe, expect, it } from 'vitest';
import { musicMix } from '../src/hub/engine/audio';
import { Dancer, ROUTINE_S, SET } from '../src/hub/engine/club';
import { BEATS_PER_MOVE, BPM, ROUTINE, crewMark, pose, routineAt, switchesBefore } from '../src/hub/engine/backstage';
import { CrewKit } from '../src/hub/engine/crew';
import { BACKSTAGE, POLES, STAGE, inClub } from '../src/hub/engine/layout';
import { COCKTAILS, orderCocktail } from '../src/hub/casino/bar';

// The Velvet Tail (engine/club.ts) and the casino's sound mix (audio.ts),
// without a GPU or a sound card: where the dancers can go, and what you hear
// from where you stand.

describe('the music mix', () => {
  const at = (o: Partial<Parameters<typeof musicMix>[0]>) => musicMix({ walking: true, inClub: false, doorDist: 20, doorOpen: 0, ...o });

  it('the overview hears the hall quietly, and never the club', () => {
    const m = musicMix({ walking: false, inClub: false, doorDist: 0, doorOpen: 1 });
    expect(m.club).toBe(0);
    expect(m.hall).toBeGreaterThan(0);
  });

  it('across the hall the club is silent; at its door, a muffled thump', () => {
    expect(at({ doorDist: 12 }).club).toBe(0);
    const shut = at({ doorDist: 1, doorOpen: 0 });
    const open = at({ doorDist: 1, doorOpen: 1 });
    expect(shut.club).toBeGreaterThan(0);
    expect(shut.clubCut).toBeLessThan(1000);
    expect(open.club).toBeGreaterThan(shut.club);
    expect(open.clubCut).toBeGreaterThan(shut.clubCut);
  });

  it('inside the club, its music plays and the hall goes behind the wall', () => {
    const m = at({ inClub: true, doorDist: 6 });
    expect(m.club).toBeGreaterThan(m.hall * 3);
    expect(m.hallCut).toBeLessThan(1000);
  });

  it('a blackout takes the music down with you', () => {
    expect(at({ black: 1 }).hall).toBeLessThan(at({}).hall);
  });
});

describe('the Tailfeathers', () => {
  const kit = new CrewKit();

  it('each works her own pole, on the stage, through the whole set', () => {
    const total = SET.reduce((a, r) => a + ROUTINE_S[r], 0);
    POLES.forEach((pole, i) => {
      const d = new Dancer(kit, i);
      const done: string[] = [];
      for (let t = 0; t < total + 5; t += 1 / 30) {
        const r = d.update(1 / 30, t, i);
        if (r) done.push(r);
        const p = d.group.position;
        expect(Math.hypot(p.x - pole[0], p.z - pole[1])).toBeLessThan(0.5);
        expect(p.y).toBeGreaterThanOrEqual(STAGE.h - 1e-6);
        expect(p.y).toBeLessThan(STAGE.h + 0.7);
        expect(inClub(p.x, p.z)).toBe(true);
      }
      // she gets through every kind of routine
      for (const r of new Set(SET)) expect(done, `dancer ${i}`).toContain(r);
    });
  });

  it('a gunshot stops the routine until she gets her nerve back', () => {
    const d = new Dancer(kit, 0);
    for (let t = 0; t < 2; t += 1 / 30) d.update(1 / 30, t, 0);
    d.spook(3);
    const finished: (string | null)[] = [];
    for (let t = 0; t < 2.5; t += 1 / 30) finished.push(d.update(1 / 30, t, 0));
    expect(finished.every((r) => r === null)).toBe(true);
  });
});

describe('the bar', () => {
  const save = () => ({ chips: 100 }) as unknown as Parameters<typeof orderCocktail>[0];

  it('a cocktail costs its price in play chips', () => {
    const s = save();
    const r = orderCocktail(s, 'martini');
    expect(r.ok).toBe(true);
    expect(s.chips).toBe(100 - COCKTAILS.find((c) => c.id === 'martini')!.price);
  });

  it('no chips, no cocktail; and nothing off-menu', () => {
    const s = save();
    s.chips = 5;
    expect(orderCocktail(s, 'velvet').ok).toBe(false);
    expect(s.chips).toBe(5);
    expect(orderCocktail(save(), 'mojito').ok).toBe(false);
  });

  it('the menu is sane: a price, a kick, a glass for each', () => {
    expect(COCKTAILS.length).toBe(6);
    for (const c of COCKTAILS) {
      expect(c.price).toBeGreaterThan(0);
      expect(c.strength).toBeGreaterThan(0.1);
      expect(c.strength).toBeLessThan(0.6);
    }
    expect(new Set(COCKTAILS.map((c) => c.id)).size).toBe(COCKTAILS.length);
  });
});

describe('the backstage crew', () => {
  it('works through the routine, eight beats a move at 120 bpm, and loops', () => {
    expect(routineAt(0).move).toBe(ROUTINE[0]);
    expect(routineAt(4.01).move).toBe(ROUTINE[1]); // 8 beats = 4 s
    const loop = (ROUTINE.length * BEATS_PER_MOVE * 60) / BPM;
    expect(routineAt(loop + 0.1).move).toBe(ROUTINE[0]);
    expect(new Set(ROUTINE).size).toBeGreaterThanOrEqual(8);
  });

  it('counts the swaps', () => {
    const per = ROUTINE.filter((m) => m === 'switch').length;
    expect(switchesBefore(0)).toBe(0);
    expect(switchesBefore(ROUTINE.length)).toBe(per);
    const first = ROUTINE.indexOf('switch');
    expect(switchesBefore(first)).toBe(0);
    expect(switchesBefore(first + 1)).toBe(1);
  });

  it('dances in unison (the wave ripples down the line), every pose sane', () => {
    for (const m of new Set(ROUTINE)) {
      for (let b = 0; b < BEATS_PER_MOVE; b += 0.25) {
        const [a, c] = [pose(m, b, 0), pose(m, b, 2)];
        for (const v of Object.values(a)) expect(Number.isFinite(v)).toBe(true);
        if (m !== 'wave') expect(c).toEqual(a);
        expect(Math.abs(a.armL)).toBeLessThan(3.2);
        expect(a.jump).toBeGreaterThanOrEqual(0);
        expect(a.jump).toBeLessThan(0.4);
      }
    }
  });

  it('stays on the backstage, swaps and all', () => {
    const loop = (ROUTINE.length * BEATS_PER_MOVE * 60) / BPM;
    for (let t = 0; t < loop * 2; t += 0.05) {
      const marks = [0, 1, 2].map((k) => crewMark(k, t));
      for (const p of marks) {
        expect(p.x).toBeGreaterThan(BACKSTAGE.x0);
        expect(p.x).toBeLessThan(BACKSTAGE.x1);
        expect(p.z).toBeGreaterThan(BACKSTAGE.z0);
        expect(p.z).toBeLessThan(BACKSTAGE.z1 - 0.3);
      }
      // nobody walks through anybody
      for (let i = 0; i < 3; i++) for (let j = i + 1; j < 3; j++) {
        expect(Math.hypot(marks[i].x - marks[j].x, marks[i].z - marks[j].z)).toBeGreaterThan(0.45);
      }
    }
    // after a swap, everyone's on a different mark than before
    const sw = ROUTINE.indexOf('switch');
    const before = crewMark(0, ((sw * BEATS_PER_MOVE - 1) * 60) / BPM).x;
    const after = crewMark(0, (((sw + 1) * BEATS_PER_MOVE + 1) * 60) / BPM).x;
    expect(after).not.toBe(before);
  });
});
