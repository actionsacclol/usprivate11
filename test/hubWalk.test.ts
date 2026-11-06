import { describe, expect, it } from 'vitest';
import * as THREE from 'three';
import { BODY_R, Walker, atExit, collide, exitDistance, step, visibleFrom, type WalkSpace } from '../src/hub/walk';
import { FLOOR_WALK, ROOMS, STAGE } from '../src/hub/engine/layout';
import { POKER_WALK, TABLE_R, seatOf } from '../src/hub/poker/room';

// First-person walk (walk.ts): the hub is still read-only, so what needs
// pinning is the room geometry — you start somewhere open, walls hold,
// doorways don't, and nothing is placed where you can't reach it.

/** Walk a straight line in small steps, as the Walker does. */
function walk(space: WalkSpace, from: [number, number], to: [number, number]): [number, number] {
  let [x, z] = collide(from[0], from[1], space);
  const n = 200;
  for (let i = 0; i < n; i++) {
    [x, z] = step(x, z, (to[0] - from[0]) / n, (to[1] - from[1]) / n, space);
  }
  return [x, z];
}

function isFree(space: WalkSpace, x: number, z: number): boolean {
  const [cx, cz] = collide(x, z, space);
  return Math.hypot(cx - x, cz - z) < 1e-6;
}

describe('collide', () => {
  const box: WalkSpace = {
    wallT: 0.2, walls: [[-5, 0, 5, 0]], props: [[0, 3, 1]],
    start: { x: 0, z: 1, yaw: 0 }, eye: 1.6, speed: 2,
  };

  it('pushes a body off a wall to its own side', () => {
    const [, z] = collide(0, 0.1, box);
    expect(z).toBeCloseTo(BODY_R + 0.1, 5);
    const [, z2] = collide(0, -0.1, box);
    expect(z2).toBeCloseTo(-(BODY_R + 0.1), 5);
  });

  it('pushes a body out of a prop', () => {
    const [x, z] = collide(0, 3.2, box);
    expect(Math.hypot(x, z - 3)).toBeCloseTo(1 + BODY_R, 5);
  });

  it('a fast step cannot tunnel through a thin wall', () => {
    const [, z] = step(0, 1, 0, -10, box);
    expect(z).toBeGreaterThan(0);
  });
});

describe('the casino floor on foot', () => {
  it('starts in the open foyer', () => {
    expect(isFree(FLOOR_WALK, FLOOR_WALK.start.x, FLOOR_WALK.start.z)).toBe(true);
  });

  it('walks in from the doors across the foyer to the pit', () => {
    const [x] = walk(FLOOR_WALK, [-12.6, 0], [-7, 0]);
    expect(x).toBeGreaterThan(-7.1);
  });

  it('the salon is glass: in only through its door', () => {
    const [, z] = walk(FLOOR_WALK, [-2.5, -3.5], [-2.5, -7]);
    expect(z).toBeGreaterThan(-4.7);
    const [, zd] = walk(FLOOR_WALK, [0.5, -3.5], [0.5, -5.6]);
    expect(zd).toBeLessThan(-5.4);
  });

  it('the cage is behind its bars', () => {
    const [, z] = walk(FLOOR_WALK, [9, -5], [9, -9]);
    expect(z).toBeGreaterThan(-6.7);
  });

  it('cannot walk off the floor', () => {
    const [x] = walk(FLOOR_WALK, [-8.4, 3], [-20, 3]);
    expect(x).toBeGreaterThan(-14.1);
    // the front wall beside the club's door…
    const [, z] = walk(FLOOR_WALK, [-3, 5], [-3, 20]);
    expect(z).toBeLessThan(10.1);
    // …and through the door you get as far as the stage's edge
    const [, zc] = walk(FLOOR_WALK, [0.5, 5], [0.5, 20]);
    expect(zc).toBeGreaterThan(12);
    expect(zc).toBeLessThan(STAGE.z);
  });

  it('every area has its sign', () => {
    expect(ROOMS.map((r) => r.id).sort()).toEqual(['backtest', 'bridge', 'desk', 'forge', 'optimizer', 'research', 'vault']);
  });
});

describe('jumping', () => {
  it('Space takes you up about half a metre and lands you within a second', () => {
    const w = new Walker(FLOOR_WALK);
    let landed = 0;
    w.onLand = () => { landed++; };
    expect(w.jump()).toBe(true);
    let top = 0;
    for (let t = 0; t < 1; t += 1 / 60) {
      w.update(1 / 60);
      top = Math.max(top, w.hop);
    }
    expect(top).toBeGreaterThan(0.65);
    expect(top).toBeLessThan(0.85);
    expect(w.hop).toBe(0);
    expect(landed).toBe(1);
  });

  it('no jumping in mid-air, mid-conversation or seated', () => {
    const w = new Walker(FLOOR_WALK);
    w.jump();
    w.update(0.1);
    expect(w.jump()).toBe(false);
    const still = new Walker(FLOOR_WALK);
    still.frozen = true;
    expect(still.jump()).toBe(false);
  });

  it("you can jump up onto the club's stage, stand on it, and drop off it", () => {
    const w = new Walker(FLOOR_WALK);
    const keys = (w as unknown as { keys: Set<string> }).keys;
    // just in front of the main stage, facing it (yaw π looks down +z)
    w.x = STAGE.x;
    w.z = STAGE.z - STAGE.r - 0.35;
    w.yaw = Math.PI;
    keys.add('f');
    for (let t = 0; t < 1; t += 1 / 60) w.update(1 / 60);
    expect(w.hop).toBe(0); // a walk into it gets you nowhere
    const z0 = w.z;
    w.jump();
    for (let t = 0; t < 1.2; t += 1 / 60) w.update(1 / 60);
    expect(w.hop).toBeCloseTo(STAGE.h, 5);
    expect(w.z).toBeGreaterThan(z0 + 0.5);
    // about face and walk off the front: down you go
    w.yaw = 0;
    for (let t = 0; t < 2; t += 1 / 60) w.update(1 / 60);
    expect(w.hop).toBe(0);
    expect(w.z).toBeLessThan(STAGE.z - STAGE.r);
  });

  it('the camera rides the jump', () => {
    const w = new Walker(FLOOR_WALK);
    const cam = new THREE.PerspectiveCamera();
    w.apply(cam);
    const floorY = cam.position.y;
    w.jump();
    for (let i = 0; i < 12; i++) w.update(1 / 60);
    w.apply(cam);
    expect(cam.position.y).toBeGreaterThan(floorY + 0.2);
  });
});

describe('the card room on foot', () => {
  it('starts in open floor, facing the table', () => {
    const s = POKER_WALK.start;
    expect(isFree(POKER_WALK, s.x, s.z)).toBe(true);
    expect(s.z).toBeGreaterThan(0); // in front of the table, looking down -z
  });

  it('the table and the seats are solid', () => {
    const [x, z] = walk(POKER_WALK, [0, 7], [0, 0]);
    expect(Math.hypot(x, z)).toBeGreaterThanOrEqual(TABLE_R);
    // the oval is long: its end holds too, out past the rail, and a walk
    // aimed off-centre slides round it without ever cutting into it
    const [xe] = walk(POKER_WALK, [7, 0], [0, 0]);
    expect(xe).toBeGreaterThan(3.3);
    const [xs, zs] = walk(POKER_WALK, [7, 0.3], [0, 0.3]);
    expect((xs / 3.3) ** 2 + (zs / TABLE_R) ** 2).toBeGreaterThan(1);
    for (let i = 0; i < 6; i++) {
      const [sx, sz] = seatOf(i);
      expect(isFree(POKER_WALK, sx, sz)).toBe(false);
    }
  });

  it('stays inside the room', () => {
    const [, z] = walk(POKER_WALK, [6, 7], [6, 30]);
    expect(z).toBeLessThan(8.6);
  });
});

describe('the doors out', () => {
  it('the casino: back down the aisle and out the entrance door', () => {
    const s = FLOOR_WALK.start;
    expect(atExit(s.x, s.z, FLOOR_WALK)).toBe(false);
    const [x, z] = walk(FLOOR_WALK, [s.x, s.z], [-14, 0]);
    expect(atExit(x, z, FLOOR_WALK)).toBe(true);
  });

  it('the casino: the entrance wall away from the door is just a wall', () => {
    const [x, z] = walk(FLOOR_WALK, [-11, -3], [-14, -3]);
    expect(atExit(x, z, FLOOR_WALK)).toBe(false);
    expect(exitDistance(x, z, FLOOR_WALK)).toBeGreaterThan(1);
  });

  it('the card room: the leather doors are the way out, the rest of the wall is not', () => {
    const s = POKER_WALK.start;
    expect(atExit(s.x, s.z, POKER_WALK)).toBe(false);
    const [x, z] = walk(POKER_WALK, [-6.35, 3], [-6.35, -9]);
    expect(atExit(x, z, POKER_WALK)).toBe(true);
    // off-centre still counts: the door is 3.5 m across
    const [xo, zo] = walk(POKER_WALK, [-7.6, 3], [-7.6, -9]);
    expect(atExit(xo, zo, POKER_WALK)).toBe(true);
    // a metre short of it does not
    expect(atExit(-6.35, -5.0, POKER_WALK)).toBe(false);
    const [x2, z2] = walk(POKER_WALK, [5, 3], [5, -9]);
    expect(atExit(x2, z2, POKER_WALK)).toBe(false);
  });

  it('standing still by a door never throws you out', () => {
    const w = new Walker(FLOOR_WALK);
    w.x = -13.6; w.z = 0;
    w.update(0.2);
    expect(w.exited).toBe(false);
  });
});

describe('Walker', () => {
  it('walks forward the way it faces', () => {
    const w = new Walker(FLOOR_WALK);
    // facing +x along the corridor; no keys yet → stays put
    const x0 = w.x;
    w.update(0.5);
    expect(w.x).toBeCloseTo(x0, 6);
    const cam = new THREE.PerspectiveCamera();
    w.apply(cam);
    const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(cam.quaternion);
    expect(fwd.x).toBeGreaterThan(0.99);
    expect(cam.position.y).toBeCloseTo(FLOOR_WALK.eye, 2);
  });
});

describe('labels in first person', () => {
  it('a label behind the lens is not drawn (it would land mirrored)', () => {
    const cam = new THREE.PerspectiveCamera();
    cam.position.set(0, 1.6, 0);
    cam.rotation.set(0, 0, 0);
    cam.updateMatrixWorld();
    expect(visibleFrom(cam, new THREE.Vector3(0, 1.6, -3), 10)).toBe(true);
    expect(visibleFrom(cam, new THREE.Vector3(0, 1.6, 3), 10)).toBe(false);
    expect(visibleFrom(cam, new THREE.Vector3(0, 1.6, -30), 10)).toBe(false);
  });
});
