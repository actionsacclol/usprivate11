import { describe, expect, it } from 'vitest';
import { collide } from '../src/hub/walk';
import {
  AGENT_DOCKS, BARTENDERS, CLUB, CLUB_BAR, CLUB_DOOR, DJ_BOOTH, FLOOR, FLOOR_WALK, GAME_SPOTS, HUMIDOR, NAV_EDGES, NAV_NODES,
  PLAY_SEATS, PODIUMS, POLES, ROOMS, RUNWAY, STAGE, clubChairs, inClub, roomAt, sofaSeats, stageChairs,
} from '../src/hub/engine/layout';
import { NavGraph } from '../src/hub/engine/nav';

// The casino hall's plan (engine/layout.ts). The staff and your agents walk
// the waypoint graph and nothing else, so a graph edge through a table is a
// dealer walking through the felt; and a game you can't reach on foot is a
// game you can't play. Both are pinned here, without a GPU.

const byId = new Map(NAV_NODES.map((n) => [n.id, n]));

function segPointDist(ax: number, az: number, bx: number, bz: number, px: number, pz: number): number {
  const dx = bx - ax, dz = bz - az;
  const len2 = dx * dx + dz * dz || 1e-9;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (pz - az) * dz) / len2));
  return Math.hypot(px - (ax + dx * t), pz - (az + dz * t));
}

function segsCross(a: number[], b: number[]): boolean {
  const [x1, y1, x2, y2] = a, [x3, y3, x4, y4] = b;
  const d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3);
  if (Math.abs(d) < 1e-12) return false;
  const t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d;
  const u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d;
  return t > 0.001 && t < 0.999 && u > 0.001 && u < 0.999;
}

/** Stations (a facing) stand AT their table; corridors keep clear of it. */
const isStation = (id: string) => byId.get(id)?.face !== undefined;

describe('the walk graph', () => {
  it('every edge names real nodes, and every node is reachable from the doors', () => {
    const nav = new NavGraph();
    for (const n of NAV_NODES) {
      const p = nav.path('c0', n.id);
      expect(p[0]?.id, n.id).toBe('c0');
      expect(p[p.length - 1].id).toBe(n.id);
    }
  });

  it('no corridor runs through a table, a machine or the bar', () => {
    const bad: string[] = [];
    for (const [a, b] of NAV_EDGES) {
      const na = byId.get(a)!, nb = byId.get(b)!;
      const margin = isStation(a) || isStation(b) ? 0.02 : 0.2;
      for (const [px, pz, r] of FLOOR_WALK.props) {
        const d = segPointDist(na.x, na.z, nb.x, nb.z, px, pz);
        if (d < r + margin) bad.push(`${a}→${b} clips the prop at (${px}, ${pz}) r${r}: ${d.toFixed(2)}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it('only the cage\'s service doors cross a wall', () => {
    const service = new Set(['vault.door', 'vault.gate']);
    const bad: string[] = [];
    for (const [a, b] of NAV_EDGES) {
      if (service.has(a) || service.has(b)) continue;
      const na = byId.get(a)!, nb = byId.get(b)!;
      for (const w of FLOOR_WALK.walls) {
        if (segsCross([na.x, na.z, nb.x, nb.z], [...w])) bad.push(`${a}→${b} crosses a wall`);
      }
    }
    expect(bad).toEqual([]);
  });

  it('the director\'s stations, the agents\' spots and every play seat exist', () => {
    for (const room of ['research', 'bridge', 'backtest', 'forge', 'desk', 'vault', 'optimizer']) {
      expect(byId.has(`${room}.agentA`), room).toBe(true);
      expect(byId.has(`${room}.agentB`), room).toBe(true);
    }
    for (const id of [
      'research.scanA', 'research.scanB', 'research.printer', 'research.wall', 'forge.anvil', 'forge.outbox',
      'forge.inbox', 'forge.furnace', 'forge.idle', 'backtest.table', 'backtest.tableB', 'backtest.tape', 'backtest.bin',
      'desk.termA', 'desk.termB', 'desk.tube', 'desk.airlock', 'desk.rack', 'vault.pile', 'vault.pileB', 'vault.idle',
      'vault.risk', 'optimizer.panel', 'optimizer.turbine', 'optimizer.reactor', 'bridge.report', ...AGENT_DOCKS,
    ]) expect(byId.has(id), id).toBe(true);
    for (const s of PLAY_SEATS) expect(byId.has(s.node), s.node).toBe(true);
    expect(PLAY_SEATS.filter((s) => s.kind === 'blackjack')).toHaveLength(4);
  });

  it('no two seats or stations stand on top of each other', () => {
    const pts = NAV_NODES.filter((n) => n.face !== undefined);
    for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) {
      const d = Math.hypot(pts[i].x - pts[j].x, pts[i].z - pts[j].z);
      expect(d, `${pts[i].id} / ${pts[j].id}`).toBeGreaterThan(0.45);
    }
  });
});

describe('the hall on foot', () => {
  /** Flood fill the walkable floor from the doors, on a 0.2 m grid. */
  function reachable(): Set<string> {
    const step = 0.2;
    const key = (i: number, j: number) => `${i},${j}`;
    const free = (x: number, z: number) => {
      const [cx, cz] = collide(x, z, FLOOR_WALK);
      return Math.hypot(cx - x, cz - z) < 1e-6;
    };
    const s = FLOOR_WALK.start;
    const seen = new Set<string>();
    const q: [number, number][] = [[Math.round(s.x / step), Math.round(s.z / step)]];
    seen.add(key(...q[0]));
    while (q.length) {
      const [i, j] = q.pop()!;
      for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const ni = i + di, nj = j + dj;
        const k = key(ni, nj);
        if (seen.has(k)) continue;
        const x = ni * step, z = nj * step;
        if (x < FLOOR.x0 - 1 || x > FLOOR.x1 + 1 || z < FLOOR.z0 - 1 || z > CLUB.z1 + 1) continue;
        if (!free(x, z)) continue;
        seen.add(k);
        q.push([ni, nj]);
      }
    }
    return seen;
  }
  const open = reachable();
  const near = (x: number, z: number, r = 0.45) => {
    for (let i = Math.round((x - r) / 0.2); i <= Math.round((x + r) / 0.2); i++) {
      for (let j = Math.round((z - r) / 0.2); j <= Math.round((z + r) / 0.2); j++) if (open.has(`${i},${j}`)) return true;
    }
    return false;
  };

  it('every game can be reached on foot', () => {
    for (const g of GAME_SPOTS) expect(near(g.x, g.z), g.id).toBe(true);
  });

  it('every area can be walked into', () => {
    for (const r of ROOMS) {
      if (r.id === 'vault') continue;
      expect(near(r.cx, r.cz, 1.6), r.id).toBe(true);
    }
  });

  it('the cage interior and the outside are not on the floor', () => {
    expect(near(9.4, -9.6, 0.2)).toBe(false);
    expect(near(-15, 0, 0.2)).toBe(false);
    // beside the club, and past its back wall
    expect(near(CLUB.x1 + 1, 15, 0.2)).toBe(false);
    expect(near(CLUB.x0 - 1, 15, 0.2)).toBe(false);
    expect(near(0.5, CLUB.z1 + 1, 0.2)).toBe(false);
  });

  it('the club is through its door, and only through its door', () => {
    expect(near(CLUB_DOOR[0], FLOOR.z1 + 0.1, 0.25)).toBe(true);
    expect(near(0.5, 13, 0.4)).toBe(true);
    expect(inClub(0.5, 13)).toBe(true);
    // the front wall either side of the door is still a wall
    for (const x of [-3, 4]) expect(near(x, FLOOR.z1 + 0.1, 0.15), `wall at ${x}`).toBe(false);
  });

  it('in the club: from the floor, the stages, behind the bar and the furniture are off limits', () => {
    expect(near(STAGE.x, STAGE.z, STAGE.r - 1), "the main stage").toBe(false);
    expect(near((RUNWAY.x0 + RUNWAY.x1) / 2, (RUNWAY.z0 + RUNWAY.z1) / 2, 0.3), 'the runway').toBe(false);
    for (const [x, z] of POLES) expect(near(x, z, 0.3), `pole ${x},${z}`).toBe(false);
    expect(near(DJ_BOOTH[0], DJ_BOOTH[1], 0.3)).toBe(false);
    for (const p of PODIUMS) expect(near(p.x, p.z, p.r - 0.45)).toBe(false);
    for (const [x, z] of BARTENDERS) expect(near(x, z, 0.3), 'behind the bar').toBe(false);
    expect(near(HUMIDOR[0], HUMIDOR[1], 0.15)).toBe(false);
    for (const c of [...clubChairs(), ...sofaSeats(), ...stageChairs()]) expect(near(c.x, c.z, 0.1)).toBe(false);
  });

  it('in the club: up to the stage between the rail chairs, the lounge and the bar are all reachable', () => {
    expect(near(STAGE.x, STAGE.z - STAGE.r - 0.5, 0.3), 'the stage, from the door').toBe(true);
    expect(near(STAGE.x + STAGE.r + 0.4, STAGE.z - 1.2, 0.3), 'the stage, from the side').toBe(true);
    expect(near(-8.4, 17.2, 0.5), 'the lounge').toBe(true);
    expect(near(CLUB_BAR.x - 0.75, 15.6, 0.3), 'the bar').toBe(true);
  });

  it('the club is big: at least twenty metres by fifteen', () => {
    expect(CLUB.x1 - CLUB.x0).toBeGreaterThanOrEqual(20);
    expect(CLUB.z1 - CLUB.z0).toBeGreaterThanOrEqual(15);
  });

  it('each game spot is in its own area, or its doorway', () => {
    for (const g of GAME_SPOTS) {
      // (the club's, and the round bar's out on the floor between the areas)
      const r = roomAt(g.x, g.z) ?? roomAt(g.lx, g.lz) ?? (inClub(g.x, g.z) ? 'club' : g.id.startsWith('hb') ? 'hall bar' : null);
      expect(r, g.id).not.toBeNull();
    }
  });
});
