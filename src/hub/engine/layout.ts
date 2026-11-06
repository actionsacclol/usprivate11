import type { Platform, WalkSpace } from '../walk';

// The casino's floor plan, in world units (1 unit ~ 1 m; a staff member is
// ~1.2 tall). Everything that has to agree on WHERE something is — the
// geometry, the walk graph, the signs, click-to-focus, the games you can play
// on foot — reads it from here, so a moved table can never leave a dealer
// dealing to thin air.
//
// An open casino hall, not a corridor of rooms. Orientation, as the
// isometric camera sees it: +x runs to the lower-right, +z to the lower-left
// (the front, nearest the camera), -z to the upper-right (the tall back wall,
// and beyond it, across the night, the POLYMARKET teller window), -x to the
// upper-left (the tall entrance wall with the doors).
//
//   back wall:   SPORTSBOOK (video wall, betting windows, seats)
//                HIGH-LIMIT salon (glass, the real orders' table)
//                CASHIER CAGE (counter and bars, the vault)
//   middle:      the entrance foyer · a carpet loop round the TABLE-GAMES PIT
//                (two blackjack tables, roulette) · the round bar
//   front:       the SLOT HALL (banks of machines, the practice table)
//                the VIP LOUNGE (the pit boss, your agents' spots)
//                and between them, a padded door to THE VELVET TAIL, the
//                cigar club behind the front wall (on foot only, club.ts)
//
// The room ids are the app's own concepts and never changed with the theme
// (jobs.ts, the director and the tests speak them): research = the
// Sportsbook, forge = the blackjack pit (the card room), backtest = the slot
// hall's practice table, desk = the High-Limit salon, vault = the Cashier
// Cage, optimizer = Roulette, bridge = the VIP lounge where the pit boss sits.

export type RoomId = 'research' | 'forge' | 'backtest' | 'desk' | 'vault' | 'optimizer' | 'bridge';

export interface RoomDef {
  id: RoomId;
  name: string;
  icon: string;
  /** Accent for the sign, trims and screen glows. */
  accent: string;
  floor: string;
  x0: number; x1: number; z0: number; z1: number;
  cx: number; cz: number;
  /** Where the floating sign sits in the overview. */
  sign: [number, number, number];
}

/** The tall walls the overview draws (back and entrance). */
export const WALL_H = 2.8;
/** The ceiling first person walks under (walkshell.ts). */
export const CEIL_H = 4.2;
export const RIM_H = 0.32;
export const PART_H = 0.62;

export const FLOOR = { x0: -14, x1: 14, z0: -10, z1: 10 };

function room(id: RoomId, name: string, icon: string, accent: string, floor: string,
  x0: number, x1: number, z0: number, z1: number, sign?: [number, number, number]): RoomDef {
  const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
  return { id, name, icon, accent, floor, x0, x1, z0, z1, cx, cz, sign: sign ?? [cx, WALL_H + 0.6, cz] };
}

export const ROOMS: RoomDef[] = [
  room('research', 'SPORTSBOOK', '📺', '#A855F7', '#211a3e', -14, -5, -10, -4.4, [-9.5, WALL_H + 1.6, -10.2]),
  room('desk', 'HIGH-LIMIT', '♠️', '#EC4899', '#3a1022', -3.5, 4.5, -10, -4.6, [0.5, WALL_H + 1.6, -10.2]),
  room('vault', 'CASHIER CAGE', '💰', '#22C55E', '#12301f', 6, 14, -10, -4.4, [10, WALL_H + 1.6, -10.2]),
  room('forge', 'BLACKJACK', '🃏', '#F59E0B', '#38200f', -6.2, 0.4, -2.9, 1.6, [-2.9, 1.9, 0.4]),
  room('optimizer', 'ROULETTE', '🎡', '#38BDF8', '#2a1630', 0.4, 6.0, -2.9, 1.6, [3.2, 1.9, 0.4]),
  room('backtest', 'SLOTS', '🎰', '#2DD4BF', '#102c30', -14, -5, 4.2, 10, [-9.5, 0.2, 10.6]),
  room('bridge', 'VIP LOUNGE', '🍸', '#FBBF24', '#2a2030', 6, 14, 3.9, 10, [10, 0.2, 10.6]),
];

export const ROOM: Record<RoomId, RoomDef> = Object.fromEntries(ROOMS.map((r) => [r.id, r])) as Record<RoomId, RoomDef>;

export function roomAt(x: number, z: number): RoomId | null {
  for (const r of ROOMS) {
    if (x >= r.x0 && x <= r.x1 && z >= r.z0 && z <= r.z1) return r.id;
  }
  return null;
}

// ── props: where things stand ─────────────────────────────────────────────

/** Blackjack tables: the flat edge's midpoint (the dealer stands just behind
 *  it, at -z, facing the guests on the arc at +z). */
export const BJ_TABLES = [[-4.5, -0.9], [-1.2, -0.9]] as const;
export const BJ_R = 0.95;
/** The guests' arc: four seats per table at this radius. */
export const BJ_SEAT_R = 1.35;
export const BJ_SEAT_DEG = [25, 65, 115, 155] as const;

export function bjSeat(t: number, s: number): [number, number] {
  const [x, z] = BJ_TABLES[t];
  const a = (BJ_SEAT_DEG[s] * Math.PI) / 180;
  return [x + Math.cos(a) * BJ_SEAT_R, z + Math.sin(a) * BJ_SEAT_R];
}

/** Slot machines: position, and the way the screen faces (a unit vector). */
export interface SlotDef { x: number; z: number; fx: number; fz: number }
export const SLOTS: SlotDef[] = [
  // along the entrance-side wall, facing into the hall
  ...[5.2, 6.15, 7.1, 8.05, 9.0].map((z) => ({ x: -13.5, z, fx: 1, fz: 0 })),
  // the island: two rows back to back
  ...[5.6, 6.5, 7.4, 8.3].map((z) => ({ x: -10.9, z, fx: -1, fz: 0 })),
  ...[5.6, 6.5, 7.4, 8.3].map((z) => ({ x: -10.2, z, fx: 1, fz: 0 })),
];
/** Where a player stands (or an agent sits) at machine i. */
export function slotSpot(i: number): [number, number] {
  const s = SLOTS[i];
  return [s.x + s.fx * 0.78, s.z + s.fz * 0.78];
}

export const PROP = {
  // sportsbook
  videoWall: [-9.5, -10] as const, // the big board, on the back wall
  kiosk: [-5.75, -8.9] as const, // the turning odds cube on its pedestal
  windowA: [-13.0, -8.6] as const, // betting windows: writers behind, at the wall
  windowB: [-13.0, -6.8] as const,
  printer: [-13.1, -5.15] as const,
  // blackjack pit (the card room)
  shoe: [-2.85, -2.6] as const, // the pit stand: shuffler and dealing shoe
  inbox: [-0.1, -2.6] as const,
  shelf: [-5.7, -2.6] as const, // finished chips wait here
  // slot hall: the practice table and its replay machine
  practice: [-6.7, 7.0] as const,
  slot: [-6.7, 9.1] as const,
  discard: [-5.6, 5.0] as const,
  // high-limit salon
  betChute: [3.6, -10] as const, // bets leave for POLYMARKET through the wall
  payout: [-2.5, -10] as const, // and winnings come back in here
  table: [0.5, -7.2] as const,
  tableA: [-0.4, -7.2] as const,
  tableB: [1.4, -7.2] as const,
  rail: [3.85, -5.6] as const, // slips and strategy chips wait on the rail
  betsBoard: [0.5, -10] as const,
  // cashier cage
  vaultDoor: [12.4, -10] as const,
  pile: [9.4, -8.7] as const,
  beacon: [13.6, -6.95] as const,
  security: [13.3, -7.6] as const,
  // roulette
  wheel: [1.6, -0.5] as const,
  layout: [3.4, -0.5] as const,
  marquee: [5.3, -2.55] as const,
  // the bar
  bar: [10.6, -1.2] as const,
  // vip lounge
  podium: [11.7, 5.1] as const, // the pit boss's high chair
  lectern: [12.9, 5.1] as const,
};

export const CAGE_Z = -6.6;
export const SALON_FRONT_Z = -4.6;
export const SALON_X0 = -3.5;
export const SALON_X1 = 4.5;
/** The salon's door in its glass front. */
export const SALON_DOOR = [0.5, 1.5] as const; // centre x, width
export const BAR_R = 1.3;
export const BAR_STOOL_R = 1.85;

// ── walk graph ─────────────────────────────────────────────────────────────

export interface NavNode { id: string; x: number; z: number; face?: number }

/** Facing angles: a worker's local +z is "forward"; heading = atan2(dx, dz). */
const F_PZ = 0;
const F_PX = Math.PI / 2;
const F_NZ = Math.PI;
const F_NX = -Math.PI / 2;
const faceTo = (x: number, z: number, tx: number, tz: number) => Math.atan2(tx - x, tz - z);

const nodes: NavNode[] = [];
const edges: [string, string][] = [];
function n(id: string, x: number, z: number, face?: number) { nodes.push({ id, x, z, face }); }
function e(a: string, b: string) { edges.push([a, b]); }
function chain(...ids: string[]) { for (let i = 1; i < ids.length; i++) e(ids[i - 1], ids[i]); }
function st(id: string, x: number, z: number, face: number, via: string) { n(id, x, z, face); e(via, id); }

// The carpet loop round the pit, and the way in from the doors. The "c"
// ids are the original spine's; c0 is just inside the doors.
n('c0', -12.4, 0);
n('c1', -9.6, 0);
n('c2', -7.0, 0);
n('loop.bl', -7.0, -3.75);
n('loop.b1', -4.5, -3.75);
n('loop.b2', -2.85, -3.75);
n('loop.b3', 0.5, -3.75);
n('loop.b4', 3.4, -3.75);
n('loop.br', 6.7, -3.75);
n('c6', 6.7, 0);
n('loop.fr', 6.7, 3.3);
n('loop.f4', 3.4, 3.3);
n('loop.f3', 0.5, 3.3);
n('loop.f2', -2.85, 3.3);
n('loop.f1', -4.5, 3.3);
n('loop.fl', -7.0, 3.3);
chain('c0', 'c1', 'c2');
chain('c2', 'loop.bl', 'loop.b1', 'loop.b2', 'loop.b3', 'loop.b4', 'loop.br', 'c6', 'loop.fr', 'loop.f4', 'loop.f3', 'loop.f2', 'loop.f1', 'loop.fl', 'c2');

// ── research: the sportsbook
n('research.door', -8.2, -4.9);
n('research.mid', -11.9, -5.4);
n('research.aisle', -12.05, -7.7);
n('research.back', -13.55, -7.7);
n('research.front', -11.75, -9.1);
chain('loop.bl', 'research.door', 'research.mid', 'research.aisle', 'research.back');
e('research.aisle', 'research.front');
e('c1', 'research.mid');
st('research.scanA', -13.55, -8.6, F_PX, 'research.back');
st('research.scanB', -13.55, -6.8, F_PX, 'research.back');
st('research.printer', -12.45, -5.15, F_NX, 'research.mid');
st('research.wall', -9.5, -9.1, F_NZ, 'research.front');
st('research.agentA', -12.3, -8.6, F_NX, 'research.aisle');
st('research.agentB', -12.3, -6.8, F_NX, 'research.aisle');

// ── desk: the high-limit salon (through its glass door)
n('desk.door', 0.5, -4.6);
n('desk.mid', 0.5, -5.35);
n('desk.sideL', -2.25, -6.0);
n('desk.backL', -2.25, -8.35);
n('desk.sideR', 3.1, -6.6);
n('desk.backR', 3.1, -8.35);
chain('loop.b3', 'desk.door', 'desk.mid');
chain('desk.mid', 'desk.sideL', 'desk.backL');
chain('desk.mid', 'desk.sideR', 'desk.backR');
st('desk.termA', -0.4, -8.3, F_PZ, 'desk.backL');
st('desk.termB', 1.4, -8.3, F_PZ, 'desk.backR');
e('desk.termA', 'desk.termB');
st('desk.tube', 3.6, -8.85, F_NZ, 'desk.backR');
st('desk.airlock', -2.55, -8.85, F_NZ, 'desk.backL');
st('desk.rack', 3.2, -5.6, F_PX, 'desk.mid');
st('desk.agentA', -0.1, -6.05, F_NZ, 'desk.mid');
st('desk.agentB', 1.15, -6.05, F_NZ, 'desk.mid');

// ── vault: the cashier cage. Staff come and go by the service door behind
// the salon (desk.backR → the passage → the cage); guests stay at the bars.
n('vault.door', 4.5, -9.3);
n('vault.passage', 5.25, -9.3);
n('vault.gate', 6.0, -9.3);
n('vault.cage', 6.95, -8.4);
n('vault.counter', 9.0, -7.05);
n('vault.front', 9.0, -5.35);
chain('desk.tube', 'vault.door', 'vault.passage', 'vault.gate', 'vault.cage');
e('loop.br', 'vault.front');
st('vault.pile', 7.75, -8.7, F_PX, 'vault.cage');
st('vault.win', 7.6, -7.2, F_PZ, 'vault.cage');
e('vault.win', 'vault.counter');
st('vault.idle', 10.4, -7.2, F_PZ, 'vault.counter');
st('vault.pileB', 11.25, -8.55, F_NX, 'vault.idle');
st('vault.risk', 12.55, -7.6, F_PX, 'vault.idle');
st('vault.agentA', 7.6, -5.9, F_NZ, 'vault.front');
st('vault.agentB', 10.4, -5.9, F_NZ, 'vault.front');

// ── forge: the blackjack pit. The dealers work from the pit's inside, a
// walkway behind the tables that the back of the loop opens onto.
n('pit.l', -6.2, -1.95);
n('pit.1', -4.5, -1.95);
n('pit.2', -2.85, -1.95);
n('pit.3', -1.2, -1.95);
n('pit.4', 0.4, -1.95);
n('pit.5', 1.6, -1.95);
n('pit.6', 3.4, -1.95);
n('pit.r', 5.9, -1.5);
chain('pit.l', 'pit.1', 'pit.2', 'pit.3', 'pit.4', 'pit.5', 'pit.6', 'pit.r');
// in at either end: the pit stand blocks the middle of the back
e('loop.bl', 'pit.l');
e('c6', 'pit.r');
st('forge.anvil', -4.5, -1.35, F_PZ, 'pit.1');
st('forge.idle', -1.2, -1.35, F_PZ, 'pit.3');
n('forge.furnace', -2.85, -2.0, F_NZ);
e('pit.2', 'forge.furnace');
st('forge.outbox', -5.7, -2.0, F_NZ, 'pit.l');
st('forge.inbox', -0.1, -2.0, F_NZ, 'pit.4');
n('forge.door', -2.85, 1.35);
chain('loop.f2', 'forge.door');
st('forge.agentA', -6.15, -0.55, F_PX, 'c2');
st('forge.agentB', -2.85, 0.75, F_NZ, 'forge.door');

// ── optimizer: roulette
st('optimizer.panel', 1.6, -1.45, F_PZ, 'pit.5');
st('optimizer.turbine', 3.4, -1.4, F_PZ, 'pit.6');
st('optimizer.reactor', 5.3, -1.95, F_NZ, 'pit.r');
n('optimizer.door', 3.4, 1.35);
chain('loop.f4', 'optimizer.door');
st('optimizer.agentA', 5.3, 0.5, F_NX, 'c6');
st('optimizer.agentB', 5.25, -0.35, F_NX, 'c6');

// ── backtest: the slot hall and its practice table
n('backtest.door', -6.9, 4.4);
n('slots.n', -12.25, 4.45);
n('slots.s', -12.25, 9.55);
n('slots.east', -8.55, 4.45);
n('slots.easts', -8.55, 9.55);
n('backtest.mid', -6.7, 5.4);
chain('loop.fl', 'backtest.door', 'slots.east', 'slots.n', 'slots.s', 'slots.easts', 'slots.east');
e('backtest.door', 'backtest.mid');
st('backtest.table', -7.85, 7.0, F_PX, 'slots.east');
st('backtest.tableB', -6.7, 5.85, F_PZ, 'backtest.mid');
n('backtest.tape', -6.7, 8.2, F_PZ);
e('slots.easts', 'backtest.tape');
st('backtest.bin', -6.25, 5.0, F_PX, 'backtest.mid');
st('backtest.agentA', -5.35, 6.6, F_NX, 'backtest.mid');
st('backtest.agentB', -5.35, 7.6, F_NX, 'backtest.agentA');

// ── bridge: the vip lounge
n('bridge.mid', 9.3, 3.6);
chain('loop.fr', 'bridge.mid');
e('c6', 'bridge.mid');
st('bridge.report', 10.75, 4.85, F_PX, 'bridge.mid');
st('bridge.helm', 12.55, 3.9, F_NX, 'bridge.mid');
export const AGENT_DOCKS = ['bridge.dock0', 'bridge.dock1', 'bridge.dock2', 'bridge.dock3', 'bridge.dock4'] as const;
st('bridge.dock0', 8.6, 4.6, F_PX, 'bridge.mid');
st('bridge.dock1', 9.2, 5.75, F_PX, 'bridge.mid');
st('bridge.dock2', 8.3, 6.8, F_PX, 'bridge.mid');
st('bridge.dock3', 9.9, 7.1, F_PX, 'bridge.mid');
st('bridge.dock4', 11.6, 6.85, faceTo(11.6, 6.85, PROP.podium[0], PROP.podium[1]), 'bridge.mid');
st('bridge.agentA', 10.1, 6.15, F_PX, 'bridge.mid');
st('bridge.agentB', 10.6, 3.75, F_PZ, 'bridge.mid');

// ── the games' seats: where your agents sit to play (and stand to watch)
export type GameKind = 'blackjack' | 'roulette' | 'slots' | 'sportsbook' | 'cashier' | 'bar';

export interface PlaySeat { node: string; kind: Exclude<GameKind, 'cashier' | 'bar'>; table: number }
export const PLAY_SEATS: PlaySeat[] = [];
for (let t = 0; t < BJ_TABLES.length; t++) {
  n(`bj${t}.front`, BJ_TABLES[t][0], 1.35);
  e(`bj${t}.front`, t === 0 ? 'loop.f1' : 'loop.f2');
  e(`bj${t}.front`, 'forge.door');
  for (let s = 0; s < BJ_SEAT_DEG.length; s++) {
    const [x, z] = bjSeat(t, s);
    st(`bj${t}.s${s}`, x, z, faceTo(x, z, BJ_TABLES[t][0], BJ_TABLES[t][1] + 0.15), `bj${t}.front`);
    // the two middle seats are yours: you play from behind them, and an
    // agent sitting there would sit in your view of the felt
    if (s === 0 || s === BJ_SEAT_DEG.length - 1) PLAY_SEATS.push({ node: `bj${t}.s${s}`, kind: 'blackjack', table: t });
  }
}
e('bj0.front', 'bj1.front');
[2.75, 3.6, 4.45].forEach((x, i) => {
  st(`rl.s${i}`, x, 0.5, F_NZ, 'optimizer.door');
  PLAY_SEATS.push({ node: `rl.s${i}`, kind: 'roulette', table: 0 });
});
SLOTS.forEach((_, i) => {
  const [x, z] = slotSpot(i);
  const s = SLOTS[i];
  // the left wall's machines and the island's west row share the west aisle
  const via = s.fx > 0 && s.x < -12 ? 'slots.w' : s.fx < 0 ? 'slots.w' : 'slots.e';
  n(`sl.${i}.a`, via === 'slots.w' ? -12.25 : -8.55, z);
  st(`sl.${i}`, x, z, faceTo(x, z, s.x, s.z), `sl.${i}.a`);
  PLAY_SEATS.push({ node: `sl.${i}`, kind: 'slots', table: i });
});
// the aisles' own nodes, threaded north to south
{
  const west = SLOTS.map((_, i) => i).filter((i) => nodes.find((nd) => nd.id === `sl.${i}.a`)!.x < -10);
  const east = SLOTS.map((_, i) => i).filter((i) => !west.includes(i));
  const byZ = (ids: number[]) => ids.map((i) => `sl.${i}.a`).sort((a, b) => nodes.find((nd) => nd.id === a)!.z - nodes.find((nd) => nd.id === b)!.z);
  // two machines at one z share an aisle point: merge them onto the first
  const thread = (ids: string[], top: string, bottom: string) => {
    const uniq: string[] = [];
    for (const id of ids) {
      const nd = nodes.find((x) => x.id === id)!;
      const twin = uniq.find((u) => Math.abs(nodes.find((x) => x.id === u)!.z - nd.z) < 1e-6);
      if (twin) e(twin, id);
      else uniq.push(id);
    }
    chain(top, ...uniq, bottom);
  };
  thread(byZ(west), 'slots.n', 'slots.s');
  thread(byZ(east), 'slots.east', 'slots.easts');
}
// The bar: stools round the counter, for agents between games. Not a game.
export const BAR_STOOLS: [number, number][] = [];
for (let i = 0; i < 8; i++) {
  const a = (i / 8) * Math.PI * 2 + Math.PI / 8;
  BAR_STOOLS.push([PROP.bar[0] + Math.cos(a) * BAR_STOOL_R, PROP.bar[1] + Math.sin(a) * BAR_STOOL_R]);
}

export const NAV_NODES = nodes;
export const NAV_EDGES = edges;
export const FACE = { PZ: F_PZ, PX: F_PX, NZ: F_NZ, NX: F_NX };

/** The POLYMARKET teller window, out across the night past the back wall,
 *  behind the High-Limit salon: every bet visibly LEAVES the floor. */
export const POLY_POS = [3.0, 1.6, -21.5] as const;

// ── what you can play on foot ─────────────────────────────────────────────

export interface GameSpot {
  id: string;
  kind: GameKind;
  /** Where you stand to play. */
  x: number;
  z: number;
  /** What you look at (the prompt picks the spot you face). */
  lx: number;
  lz: number;
  table: number;
  name: string;
}

export const GAME_SPOTS: GameSpot[] = [
  ...BJ_TABLES.map(([x, z], t): GameSpot => ({ id: `bj${t}`, kind: 'blackjack', x, z: z + 1.9, lx: x, lz: z + 0.3, table: t, name: `Blackjack · table ${t + 1}` })),
  { id: 'rl', kind: 'roulette', x: 1.75, z: 1.05, lx: 1.9, lz: -0.5, table: 0, name: 'Roulette' },
  ...SLOTS.map((s, i): GameSpot => {
    const [x, z] = slotSpot(i);
    return { id: `sl${i}`, kind: 'slots', x, z, lx: s.x, lz: s.z, table: i, name: `Slot machine ${i + 1}` };
  }),
  { id: 'sbA', kind: 'sportsbook', x: -12.25, z: -8.6, lx: PROP.windowA[0], lz: PROP.windowA[1], table: 0, name: 'Sportsbook window' },
  { id: 'sbB', kind: 'sportsbook', x: -12.25, z: -6.8, lx: PROP.windowB[0], lz: PROP.windowB[1], table: 1, name: 'Sportsbook window' },
  { id: 'sbW', kind: 'sportsbook', x: -9.3, z: -7.15, lx: PROP.videoWall[0], lz: PROP.videoWall[1], table: 2, name: 'Sportsbook board' },
  { id: 'cgA', kind: 'cashier', x: 7.6, z: -5.85, lx: 7.6, lz: CAGE_Z, table: 0, name: 'Cashier' },
  { id: 'cgB', kind: 'cashier', x: 10.4, z: -5.85, lx: 10.4, lz: CAGE_Z, table: 1, name: 'Cashier' },
];

// ── the club: The Velvet Tail ───────────────────────────────────────────
// A gentlemen's club behind the front wall, across the pit from the
// High-Limit salon's door (both at x 0.5). On foot only: the overview stays
// the hall. In through the padded doors: the round main stage in the middle
// of the room, chairs ringing it, a runway from it back to the curtain; a
// go-go podium either side; the long bar down the right wall, the cigar
// lounge down the left, a VIP booth and the DJ on his riser in the back
// corners. Every stage is a platform (walk.ts): you can jump up onto it.

export const CLUB = { x0: -11, x1: 12, z0: FLOOR.z1 + 0.1, z1: 28 };
/** The door in the hall's front wall: centre x, width. */
export const CLUB_DOOR = [0.5, 1.7] as const;
export const CLUB_CEIL = 5.2;
/** The main stage: round, in the middle of the room. `h` is every stage's. */
export const STAGE = { x: 0.5, z: 19.2, r: 2.6, h: 0.6 };
/** The runway from the main stage back to the curtain, and the backstage. */
export const RUNWAY = { x0: -0.6, x1: 1.6, z0: 21.5, z1: 25.6 };
export const BACKSTAGE = { x0: -3.2, x1: 4.2, z0: 25.6, z1: 28 };
/** Go-go podiums: round, raised, a pole in each. */
export const PODIUMS: readonly { x: number; z: number; r: number }[] = [{ x: -6.4, z: 22.6, r: 1.1 }, { x: 7.0, z: 23.6, r: 1.1 }];
/** The poles: two on the main stage, then the podiums'. */
export const POLES: readonly (readonly [number, number])[] = [[-0.6, 19.2], [1.6, 19.2], [-6.4, 22.6], [7.0, 23.6]];
/** The DJ, on a riser in the back-left corner, facing the stage. */
export const DJ_BOOTH = [-8.8, 26.6] as const;
export const DJ_RISER = { x0: -10.85, x1: -6.9, z0: 24.9, z1: 27.85, h: 0.3 };
export const DJ_YAW = Math.atan2(STAGE.x - DJ_BOOTH[0], STAGE.z - DJ_BOOTH[1]);
/** The bar down the right wall: its counter's front face (a line along z).
 *  Between its near end and the front wall, the stock room. The stools are
 *  scenery, not walls: you slide along the counter past them. */
export const CLUB_BAR = { x: 9.3, z0: 12.4, z1: 21.2 };
/** Where you stand to order, and the bartender across the counter. */
export const BAR_SPOTS: readonly number[] = [15.6, 19.2];
export const BARTENDERS: readonly (readonly [number, number])[] = BAR_SPOTS.map((z) => [CLUB_BAR.x + 1.05, z] as const);
export const CLUB_STOOLS: readonly (readonly [number, number])[] = [13.0, 14.1, 17.0, 18.1, 20.4].map((z) => [CLUB_BAR.x - 0.55, z] as const);
export const HUMIDOR = [-10.5, 11.3] as const;
export const BOUNCER = [-0.9, 10.75] as const;

export interface ClubSeat { x: number; z: number; yaw: number }
/** Sofas: centre, which way they face, length. The lounge's along the left
 *  wall, and the VIP booth in the back-right corner. */
export const CLUB_SOFAS: readonly { x: number; z: number; yaw: number; len: number }[] = [
  { x: -10.35, z: 13.6, yaw: Math.PI / 2, len: 2.2 },
  { x: -10.35, z: 17.2, yaw: Math.PI / 2, len: 2.2 },
  { x: -10.35, z: 20.8, yaw: Math.PI / 2, len: 2.2 },
  { x: 10.0, z: 27.45, yaw: Math.PI, len: 2.6 },
];
/** The low table in front of a sofa. */
export function sofaTable(f: { x: number; z: number; yaw: number }): [number, number] {
  return [f.x + Math.sin(f.yaw) * 0.95, f.z + Math.cos(f.yaw) * 0.95];
}
/** Two places on each sofa. */
export function sofaSeats(): ClubSeat[] {
  return CLUB_SOFAS.flatMap((f) => [-0.5, 0.5].map((k) => ({
    x: f.x + Math.cos(f.yaw) * k * f.len * 0.6, z: f.z - Math.sin(f.yaw) * k * f.len * 0.6, yaw: f.yaw,
  })));
}
/** Low tables with two club chairs each, facing the stage. */
export const CLUB_TABLES: readonly (readonly [number, number])[] = [[-7.6, 12.6], [-7.4, 15.6], [6.4, 12.4], [6.6, 15.6]];
export function clubChairs(): ClubSeat[] {
  const fx = STAGE.x, fz = STAGE.z;
  return CLUB_TABLES.flatMap(([x, z]) => {
    const dx = fx - x, dz = fz - z, d = Math.hypot(dx, dz);
    const ux = dx / d, uz = dz / d;
    const yaw = Math.atan2(ux, uz);
    return [-1, 1].map((k) => ({ x: x - ux * 0.78 + uz * k * 0.48, z: z - uz * 0.78 - ux * k * 0.48, yaw }));
  });
}

/** The rail: chairs ringing the main stage, facing it, with gaps to get by
 *  (and up). Angles from +x; -90 faces the door. */
export const RAIL_DEG: readonly number[] = [-165, -135, -105, -75, -45, -15, 15, 165];
export function stageChairs(): ClubSeat[] {
  return RAIL_DEG.map((deg) => {
    const a = (deg * Math.PI) / 180, r = STAGE.r + 0.75;
    const x = STAGE.x + Math.cos(a) * r, z = STAGE.z + Math.sin(a) * r;
    return { x, z, yaw: Math.atan2(STAGE.x - x, STAGE.z - z) };
  });
}

/** Every raised floor in the club, for walking (and jumping) on. */
export const CLUB_PLATFORMS: Platform[] = [
  { kind: 'disc', x: STAGE.x, z: STAGE.z, r: STAGE.r, h: STAGE.h },
  { kind: 'rect', ...RUNWAY, h: STAGE.h },
  { kind: 'rect', ...BACKSTAGE, h: STAGE.h },
  ...PODIUMS.map((p): Platform => ({ kind: 'disc', x: p.x, z: p.z, r: p.r, h: STAGE.h })),
  { kind: 'rect', ...DJ_RISER },
];

export function inClub(x: number, z: number): boolean {
  return x > CLUB.x0 && x < CLUB.x1 && z > CLUB.z0 && z < CLUB.z1;
}

// the club's bar is a place to "play" too: up to the counter, order across it
GAME_SPOTS.push(...BAR_SPOTS.map((z, i): GameSpot => ({
  id: `bar${i}`, kind: 'bar', x: CLUB_BAR.x - 0.62, z, lx: BARTENDERS[i][0], lz: z, table: i, name: 'The bar',
})));

// The round bar in the hall: two places to stand and order (between the
// stools), the bartender working the well inside the ring.
export const HALL_BAR_SPOTS: readonly number[] = [Math.PI, Math.PI / 2];
export const HALL_BAR_WELL = 0.68;
GAME_SPOTS.push(...HALL_BAR_SPOTS.map((a, i): GameSpot => ({
  id: `hb${i}`, kind: 'bar', x: PROP.bar[0] + Math.cos(a) * 1.95, z: PROP.bar[1] + Math.sin(a) * 1.95,
  lx: PROP.bar[0], lz: PROP.bar[1], table: i, name: 'The bar',
})));

// ── walking it (walk.ts) ──────────────────────────────────────────────────

/** The way out on foot: the doors in the entrance wall. */
export const FLOOR_EXIT = [FLOOR.x0 - 0.1, -0.75, FLOOR.x0 - 0.1, 0.75] as const;

/** The salon's glass, on foot: its front with the door, and its sides. */
export const SALON_GLASS: [number, number, number, number][] = [
  [SALON_X0, SALON_FRONT_Z, SALON_DOOR[0] - SALON_DOOR[1] / 2, SALON_FRONT_Z],
  [SALON_DOOR[0] + SALON_DOOR[1] / 2, SALON_FRONT_Z, SALON_X1, SALON_FRONT_Z],
  [SALON_X0, FLOOR.z0, SALON_X0, SALON_FRONT_Z],
  [SALON_X1, FLOOR.z0, SALON_X1, SALON_FRONT_Z],
];

/** The cage: its barred counter, the panel that closes the service nook, and
 *  its side wall. Staff pass the service door (the walk graph); you don't. */
export const CAGE_WALLS: [number, number, number, number][] = [
  [SALON_X1, CAGE_Z, FLOOR.x1, CAGE_Z],
  [6.0, FLOOR.z0, 6.0, CAGE_Z],
];

const circles: [number, number, number][] = [];
const solid = (x: number, z: number, r: number) => circles.push([x, z, r]);
// sportsbook: counters (as circles along them), the printer, the kiosk, seats
for (const [x, z] of [PROP.windowA, PROP.windowB]) for (const dz of [-0.35, 0, 0.35]) solid(x, z + dz, 0.32);
solid(...PROP.printer, 0.35);
solid(...PROP.kiosk, 0.55);
export const SB_SEATS: [number, number][] = [];
for (const z of [-7.95, -6.35]) for (const x of [-10.8, -9.8, -8.8, -7.8, -6.8]) SB_SEATS.push([x, z]);
for (const [x, z] of SB_SEATS) solid(x, z, 0.38);
// the blackjack tables (half-moons as two circles), their dealers' spot, the pit stand
for (const [x, z] of BJ_TABLES) {
  // the half-moon: the arc's bulge, and its two shoulders on the flat edge
  solid(x, z + 0.45, 0.55);
  solid(x - 0.55, z + 0.2, 0.38);
  solid(x + 0.55, z + 0.2, 0.38);
}
for (const p of [PROP.shoe, PROP.inbox, PROP.shelf]) solid(p[0], p[1], 0.4);
// roulette: the wheel, the layout, the marquee
solid(...PROP.wheel, 0.85);
for (const dx of [-0.75, 0, 0.75]) solid(PROP.layout[0] + dx, PROP.layout[1], 0.55);
solid(...PROP.marquee, 0.3);
// the slot machines and the practice table
for (const s of SLOTS) solid(s.x, s.z, 0.42);
solid(...PROP.practice, 0.95);
solid(...PROP.slot, 0.45);
solid(...PROP.discard, 0.3);
// high-limit: the long table (a stadium as three circles), the rail table
for (const dx of [-1.15, 0, 1.15]) solid(PROP.table[0] + dx, PROP.table[1], 0.68);
solid(...PROP.rail, 0.35);
// the bet chute's stand and the payout window's shelf, at the back wall
solid(PROP.betChute[0], PROP.betChute[1] + 0.5, 0.42);
solid(PROP.payout[0] - 0.4, PROP.payout[1] + 0.35, 0.3);
solid(PROP.payout[0] + 0.4, PROP.payout[1] + 0.35, 0.3);
// the bar
solid(...PROP.bar, BAR_R + 0.05);
for (const [x, z] of BAR_STOOLS) solid(x, z, 0.2);
// the lounge: the pit boss, the lectern, sofas
solid(...PROP.podium, 0.5);
solid(...PROP.lectern, 0.4);
export const LOUNGE_SOFAS: [number, number, number][] = [[8.4, 9.0, 0], [12.0, 9.0, 0], [13.3, 6.9, -Math.PI / 2]];
for (const [x, z, face] of LOUNGE_SOFAS) {
  solid(x, z, 0.75);
  // and its low table, in front of it
  const h = face === 0 ? Math.PI : face;
  solid(x + Math.sin(h) * 0.85, z + Math.cos(h) * 0.85, 0.42);
}
export const PALMS: [number, number][] = [[-13.25, -1.75], [-13.25, 1.75], [13.3, 9.3], [6.5, 9.3], [-4.4, -9.5], [13.35, -3.9]];
for (const [x, z] of PALMS) solid(x, z, 0.32);
// the club: podiums, tables, chairs, sofas, stools, the humidor, the
// bouncer by the door and the crowd on the floor
for (const [x, z] of CLUB_TABLES) solid(x, z, 0.4);
for (const c of [...clubChairs(), ...stageChairs()]) solid(c.x, c.z, 0.32);
for (const [x, z] of POLES) solid(x, z, 0.1);
for (const k of [-0.6, 0, 0.6]) {
  // the DJ's desk, across his front
  solid(DJ_BOOTH[0] + Math.sin(DJ_YAW) * 0.55 + Math.cos(DJ_YAW) * k, DJ_BOOTH[1] + Math.cos(DJ_YAW) * 0.55 - Math.sin(DJ_YAW) * k, 0.35);
}
for (const f of CLUB_SOFAS) {
  for (const k of [-0.35, 0, 0.35]) solid(f.x + Math.cos(f.yaw) * k * f.len, f.z - Math.sin(f.yaw) * k * f.len, 0.42);
  solid(...sofaTable(f), 0.35);
}
solid(...HUMIDOR, 0.42);
solid(...BOUNCER, 0.3);

const DOOR_L = CLUB_DOOR[0] - CLUB_DOOR[1] / 2, DOOR_R = CLUB_DOOR[0] + CLUB_DOOR[1] / 2;

export const FLOOR_WALK: WalkSpace = {
  wallT: 0.2,
  walls: [
    [FLOOR.x0 - 0.1, FLOOR.z0 - 0.1, FLOOR.x1 + 0.1, FLOOR.z0 - 0.1],
    [FLOOR.x0 - 0.1, FLOOR.z0 - 0.1, FLOOR.x0 - 0.1, FLOOR.z1 + 0.1],
    [FLOOR.x1 + 0.1, FLOOR.z0 - 0.1, FLOOR.x1 + 0.1, FLOOR.z1 + 0.1],
    // the front wall, with the club's door in it
    [FLOOR.x0 - 0.1, FLOOR.z1 + 0.1, DOOR_L, FLOOR.z1 + 0.1],
    [DOOR_R, FLOOR.z1 + 0.1, FLOOR.x1 + 0.1, FLOOR.z1 + 0.1],
    ...SALON_GLASS,
    ...CAGE_WALLS,
    // the club: its walls, the stage, the bar (closed at both ends: behind
    // it is the bartenders')
    [CLUB.x0, CLUB.z0, CLUB.x0, CLUB.z1],
    [CLUB.x1, CLUB.z0, CLUB.x1, CLUB.z1],
    [CLUB.x0, CLUB.z1, CLUB.x1, CLUB.z1],
    [CLUB_BAR.x, CLUB_BAR.z0, CLUB_BAR.x, CLUB_BAR.z1],
    [CLUB_BAR.x, CLUB_BAR.z0, CLUB.x1, CLUB_BAR.z0],
    // the stock room, between the bar and the front wall
    [CLUB_BAR.x, CLUB.z0, CLUB_BAR.x, CLUB_BAR.z0],
    [CLUB_BAR.x, CLUB_BAR.z1, CLUB.x1, CLUB_BAR.z1],
  ],
  props: circles,
  platforms: CLUB_PLATFORMS,
  // just inside the doors, looking across the foyer at the pit
  start: { x: -12.6, z: 0, yaw: -Math.PI / 2 },
  exits: [FLOOR_EXIT],
  eye: 1.55,
  speed: 2.6,
};
