import type { Circle, Seg, WalkSpace } from '../walk';
import { CHAIR_Z, DESK_D, DESK_W } from './desk';
import { ELEVATOR, ROOM } from './room';

// The Penthouse on foot (walk.ts): the unit's walls and glass, every desk
// (its outline is a wall, so you walk round it, not through the monitors), every
// chair, the lounge, the bar and the planters. The way out is the elevator in
// the front wall. Pure, so the floor plan is testable without a GPU.

function rect(x0: number, z0: number, x1: number, z1: number): Seg[] {
  return [[x0, z0, x1, z0], [x1, z0, x1, z1], [x1, z1, x0, z1], [x0, z1, x0, z0]];
}

/** Where a walk starts: by the elevator, looking down the room at the desks. */
export const WALK_START = { x: (ELEVATOR.x0 + ELEVATOR.x1) / 2, z: ROOM.z1 - 1.3, yaw: 0, pitch: -0.05 };

export function penthouseWalkSpace(desks: readonly { x: number; z: number }[]): WalkSpace {
  const { x0, x1, z0, z1 } = ROOM;
  const walls: Seg[] = [
    [x0, z0, x1, z0], [x1, z0, x1, z1], [x0, z0, x0, z1],
    // the front wall, with the elevator in it
    [x0, z1, ELEVATOR.x0, z1], [ELEVATOR.x1, z1, x1, z1],
    // the lounge: the long run, its return, the table
    ...rect(-11.1, 4.7, -7.5, 5.75), ...rect(-7.7, 2.5, -6.7, 5.7), ...rect(-10.2, 3.25, -8.6, 4.15),
    // the bar and the fridge
    ...rect(4.8, 5.5, 8.8, 6.3), ...rect(9.97, 5.67, 11.23, 6.43),
  ];
  const props: Circle[] = [
    [5.6, 5.2, 0.22], [6.8, 5.2, 0.22], [8, 5.2, 0.22],          // bar stools
    [-12.3, -7.2, 0.32], [12.2, -7.4, 0.32], [-12.3, 1.6, 0.32], // planters
    [-11.9, 5.5, 0.16],                                           // the floor lamp
  ];
  for (const d of desks) {
    walls.push(...rect(d.x - DESK_W / 2, d.z - DESK_D / 2, d.x + DESK_W / 2, d.z + DESK_D / 2));
    props.push([d.x, d.z + CHAIR_Z, 0.36]);
  }
  return {
    walls, props, wallT: 0.12,
    start: WALK_START,
    eye: 1.55,
    speed: 2.6,
    exits: [[ELEVATOR.x0, z1, ELEVATOR.x1, z1]],
  };
}

/** The desk a walker at (x, z) is standing at, within `reach` of its chair. */
export function deskInReach<T extends { x: number; z: number }>(desks: readonly (T & { id: string })[], x: number, z: number, reach = 2.2): (T & { id: string }) | null {
  let best: (T & { id: string }) | null = null;
  let bd = reach;
  for (const d of desks) {
    const dist = Math.hypot(x - d.x, z - (d.z + CHAIR_Z));
    if (dist < bd) { bd = dist; best = d; }
  }
  return best;
}
