import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import {
  LH, R0, TW, X_FINISH, X_GATE, catOf, finishS, gateS, lapLength, laneOffset, silksOf, trackPoint,
} from '../src/hub/racetrack/track';
import { oddsOf } from '../src/hub/racetrack/tote';

// The Race Track scene's geometry is what keeps it honest: the lane IS the
// price (favourites on the rail), the wire and the gate sit where the
// scene draws them, and a lap is one closed loop with no jumps.

describe('race track lanes', () => {
  it('puts favourites on the inner rail and longshots outside, all on the track', () => {
    const fav = laneOffset(90), even = laneOffset(50), long = laneOffset(10);
    expect(fav).toBeLessThan(even);
    expect(even).toBeLessThan(long);
    for (const p of [1, 25, 50, 75, 99, -5, 140]) {
      const o = laneOffset(p);
      expect(o).toBeGreaterThan(0);
      expect(o).toBeLessThan(TW);
    }
  });

  it('runs a lap as one continuous loop, counter-clockwise from above', () => {
    for (const off of [laneOffset(90), laneOffset(10)]) {
      const P = lapLength(off);
      const a = new THREE.Vector3(), b = new THREE.Vector3();
      let prevYaw = trackPoint(0, off, a);
      const steps = 2000;
      for (let i = 1; i <= steps; i++) {
        const yaw = trackPoint((P * i) / steps, off, b);
        // no jumps: consecutive samples are one step apart
        expect(a.distanceTo(b)).toBeLessThan((P / steps) * 1.05);
        // the heading only ever turns left (CCW) — except the wrap at a lap
        if (i < steps) expect(yaw).toBeGreaterThanOrEqual(prevYaw - 1e-9);
        prevYaw = yaw;
        a.copy(b);
      }
      // and every sample sits on its lane
      trackPoint(P * 0.37, off, a);
      const q = new THREE.Vector2(Math.max(Math.abs(a.x) - LH, 0), a.z);
      expect(q.length() - R0).toBeCloseTo(off, 6);
    }
  });

  it('places the wire on the home straight and the gate on the backstretch', () => {
    const v = new THREE.Vector3();
    const off = laneOffset(50);
    trackPoint(finishS(), off, v);
    expect(v.x).toBeCloseTo(X_FINISH, 6);
    expect(v.z).toBeLessThan(0);
    trackPoint(gateS(off), off, v);
    expect(v.x).toBeCloseTo(X_GATE, 6);
    expect(v.z).toBeCloseTo(R0 + off, 6);
  });
});

describe('race track labels', () => {
  it('quotes a price as the fractional odds it implies', () => {
    expect(oddsOf(50)).toBe('EVS');
    expect(oddsOf(33)).toBe('2-1');
    expect(oddsOf(20)).toBe('4-1');
    expect(oddsOf(80)).toBe('1-4');
    expect(oddsOf(1)).toBe('99-1');
  });

  it('folds unknown categories into Other and unknown sources into the crypto silks', () => {
    expect(catOf('NBA playoffs sports')).toBe('Sports');
    expect(catOf('weather')).toBe('Climate');
    expect(catOf('something new')).toBe('Other');
    expect(catOf(null)).toBe('Other');
    expect(silksOf('script:ab12cd34:crypto')).toBe('#A855F7');
    expect(silksOf('nonsense')).toBe(silksOf('crypto'));
  });
});
