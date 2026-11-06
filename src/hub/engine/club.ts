import * as THREE from 'three';
import { bakePart, mergeStatic } from './bake';
import { canvasTex } from './casinoTextures';
import { CrewKit, PINK, ROLE_STYLE, buildRat, buildRatHead, buildRatTail, newRatParts, type RatParts, type RoleStyle } from './crew';
import {
  BACKSTAGE, BARTENDERS, BOUNCER, CEIL_H, CLUB, CLUB_BAR, CLUB_CEIL, CLUB_DOOR, CLUB_SOFAS, CLUB_STOOLS, CLUB_TABLES,
  DJ_BOOTH, DJ_RISER, DJ_YAW, HUMIDOR, PODIUMS, POLES, RUNWAY, STAGE, clubChairs, sofaSeats, sofaTable, stageChairs, type ClubSeat,
} from './layout';
import type { Persona } from './npcs';
import { BackupDancer, CREW_X, CREW_Z, crewMark, routineAt } from './backstage';

// The Velvet Tail: the gentlemen's club behind the casino's front wall,
// through a pair of padded doors across the pit from the High-Limit salon.
// On foot only (the overview stays the hall).
//
//   the stage   round, in the middle of the room, chairs ringing it, a
//               runway back to the curtain, a go-go podium either side, a
//               ring of stage lights over it. The Tailfeathers dance on its
//               poles, in sequinned two-pieces, pearls, feathered headbands
//               and heels. Every stage is a platform: you can jump up
//   the bar     down the right wall, backlit, two bartenders: walk up and
//               order a cocktail for play chips (bar.ts, BarSession)
//   the lounge  down the left: sofas, club chairs, cigars, the humidor; the
//               VIP booth back right, DJ Gramps on his riser back left,
//               Knuckles on the door
//
// Decoration with a pulse, like the rest of the walk: nobody in here reads
// or writes anything real.

const WALL_T = 0.24;
const DOOR_H = 2.45;
const W = CLUB.x1 - CLUB.x0;
const D = CLUB.z1 - CLUB.z0;
const TRUSS_Y = CLUB_CEIL - 0.75;
const PALETTE = ['#ff2d95', '#b0367f', '#8b5cf6', '#e6a336', '#e0368f', '#7d4dff'];

/** The club's people you can talk to (walk.ts, E). Same rules as the
 *  floor's (npcs.ts): characters, no numbers. */
export const CLUB_PERSONAS: Persona[] = [
  {
    id: 'club.bouncer',
    name: 'Knuckles Malone',
    title: 'The Door · The Velvet Tail',
    greet: [
      'Velvet Tail. Members and guests. You look like a guest.',
      'Evenin’. Lounge to the left, bar to the right, the stage straight ahead. Nobody climbs on the stage.',
    ],
    bye: ['Mind the step.', 'Enjoy the show. Politely.'],
    barks: ['Keep it classy, pal.', 'No shootin’ in the club.', 'Humidor’s on your left.'],
    topics: [
      {
        ask: 'What is this place?',
        say: [
          'The Velvet Tail. Cigars, cocktails, the Tailfeathers, and Gramps on the decks.',
          'The pit boss built it for the regulars. The regulars are rats. So is the pit boss. So, I’m told, are you.',
        ],
      },
      {
        ask: 'Who are the dancers?',
        say: [
          'The Tailfeathers. Best pole act on the river.',
          'They dance, you clap, everybody keeps their paws to themselves. That’s the whole rulebook, and I’m the binding.',
        ],
      },
      {
        ask: 'About the revolver…',
        say: ['In here it stays in your pocket.', 'You take it out, the music stops. You don’t want to be the guy who stopped the music.'],
      },
    ],
  },
  {
    id: 'club.bartender',
    name: 'Dot Whiskerton',
    title: 'Behind the Bar · The Velvet Tail',
    greet: ['What’ll it be, sugar?', 'Step up to the counter and order, hon. I shake, you sip.'],
    bye: ['Don’t be a stranger.', 'Pace yourself. The vodka in your pocket isn’t going anywhere.'],
    barks: ['Last call is never.', 'Ice? We’ve got ice.', 'Mind the cigar ash, hon.'],
    topics: [
      {
        ask: 'What’s good?',
        say: [
          'The Velvet Tail. House special. I won’t tell you what’s in it, and you won’t remember.',
          'For a lighter night: a Cosmo. For a heavier one: a Manhattan, then a chair.',
        ],
      },
      {
        ask: 'Is any of this real money?',
        say: [
          'Nothing in the club is. Not the drinks, not the cigars, not the chips out on the floor.',
          'The only real money in this building is the bets the bot places, and those go out through the salon, never in here.',
        ],
      },
    ],
  },
  {
    id: 'club.dj',
    name: 'DJ Gramps',
    title: 'On the Decks · The Velvet Tail',
    greet: ['Gramps on the decks, kid. Jazz only. Always jazz.', 'You want a request? I take requests. I ignore them, but I take them.'],
    bye: ['Keep dancin’.', 'Stay swingin’.'],
    barks: ['This one’s for the high rollers!', 'Make some noise for the Tailfeathers!', 'Everybody SWING!'],
    topics: [
      {
        ask: 'Can you play something else?',
        say: ['I can play it louder.'],
      },
      {
        ask: 'Who wrote this?',
        say: ['A fella called Kevin MacLeod. Good ear. The credit’s up top every time a record starts. Respect the artist, kid.'],
      },
    ],
  },
];

/** Lou, behind the round bar out in the hall. */
export const HALL_BAR_PERSONA: Persona = {
  id: 'hall.bartender',
  name: 'Lou Bottlesworth',
  title: 'The Round Bar · The Casino',
  greet: ['What’s your poison, pal?', 'Step up. The round bar never closes and neither do I.'],
  bye: ['Good luck out there.', 'Tip your dealer. Not me, I’m fine.'],
  barks: ['Cocktails! Get your cocktails!', 'Drinks on… well, on you.', 'Shaken, stirred, whatever, pal.'],
  topics: [
    {
      ask: 'What’ve you got?',
      say: ['Same menu as the Velvet Tail, better view of the tables.', 'Walk up to the counter and press E. I shake it, you drink it, it goes in your hotbar.'],
    },
    {
      ask: 'Is any of this real money?',
      say: [
        'Play chips, pal. Nothing in this building costs you a cent but your dignity.',
        'The only real money moving in here is the bets the bot places, and they go out through the High-Limit room.',
      ],
    },
  ],
};

const PATRON_BARKS = ['Bravo!', 'Encore!', 'Magnifique!', 'Now THAT’S a spin.', 'Another round for the table!', 'Finest cigars on the river.'];
const CROWD_BARKS = ['This one SWINGS!', 'Gramps is on fire tonight!', 'Look at those lights!', 'Who’s buyin’?', 'I can’t feel my tail!', 'Dance, ya big cheese!'];

function std(color: string, o: { rough?: number; metal?: number; e?: string; ei?: number; map?: THREE.Texture } = {}): THREE.MeshStandardMaterial {
  return new THREE.MeshStandardMaterial({
    color, roughness: o.rough ?? 0.7, metalness: o.metal ?? 0.05, flatShading: true, map: o.map ?? null,
    emissive: o.e ?? '#000000', emissiveIntensity: o.ei ?? (o.e ? 1 : 0),
  });
}

function nearest<T extends THREE.Texture>(t: T): T {
  t.magFilter = THREE.NearestFilter;
  t.minFilter = THREE.NearestFilter;
  t.generateMipmaps = false;
  return t;
}

function neonText(lines: { text: string; font: string; y: number; color: string; blur: number }[], w = 512, h = 160): THREE.CanvasTexture {
  return canvasTex(w, h, (g) => {
    g.clearRect(0, 0, w, h);
    g.textAlign = 'center';
    g.textBaseline = 'middle';
    for (const l of lines) {
      g.font = l.font;
      g.shadowColor = l.color;
      g.shadowBlur = l.blur;
      g.fillStyle = l.color;
      g.fillText(l.text, w / 2, l.y);
      g.shadowBlur = l.blur / 3;
      g.fillStyle = '#fff1f8';
      g.fillText(l.text, w / 2, l.y);
    }
  });
}

/** The club's name in pink neon. */
function clubSign(): THREE.CanvasTexture {
  return neonText([
    { text: 'The Velvet Tail', font: 'italic 700 62px Georgia, "Times New Roman", serif', y: 66, color: '#ff4fa8', blur: 24 },
    { text: 'GENTLEMEN’S CLUB · COCKTAILS · JAZZ', font: '700 24px "Chakra Petch", sans-serif', y: 128, color: '#F2C66D', blur: 14 },
  ]);
}

function carpetTex(): THREE.Texture {
  const t = nearest(canvasTex(64, 64, (g, w, h) => {
    g.fillStyle = '#2a0a12';
    g.fillRect(0, 0, w, h);
    g.fillStyle = '#360d17';
    g.fillRect(0, 0, w / 2, h / 2);
    g.fillRect(w / 2, h / 2, w / 2, h / 2);
    g.fillStyle = '#8a6424';
    for (const [x, y] of [[16, 16], [48, 48], [48, 16], [16, 48]]) {
      g.fillRect(x - 1, y - 5, 2, 10);
      g.fillRect(x - 5, y - 1, 10, 2);
    }
  }, true));
  t.repeat.set(W / 1.6, D / 1.6);
  return t;
}

function tuftTex(): THREE.Texture {
  // oxblood leather, buttoned, with a border of brass studs
  return nearest(canvasTex(64, 128, (g, w, h) => {
    g.fillStyle = '#4a1410';
    g.fillRect(0, 0, w, h);
    g.strokeStyle = '#2c0a08';
    g.lineWidth = 2;
    for (let i = -h; i < h * 2; i += 16) {
      g.beginPath(); g.moveTo(0, i); g.lineTo(w, i + w); g.stroke();
      g.beginPath(); g.moveTo(0, i + w); g.lineTo(w, i); g.stroke();
    }
    g.fillStyle = '#d9b05a';
    for (let y = 4; y < h; y += 8) { g.fillRect(2, y, 2, 2); g.fillRect(w - 4, y, 2, 2); }
    for (let x = 4; x < w; x += 8) { g.fillRect(x, 2, 2, 2); g.fillRect(x, h - 4, 2, 2); }
  }));
}

function humidorTex(): THREE.Texture {
  return nearest(canvasTex(64, 128, (g, w) => {
    g.fillStyle = '#2a1206';
    g.fillRect(0, 0, w, 128);
    for (let s = 0; s < 4; s++) {
      const y = 10 + s * 29;
      g.fillStyle = '#5a3416';
      g.fillRect(4, y + 22, w - 8, 3);
      for (let b = 0; b < 3; b++) {
        g.fillStyle = ['#8b5a2b', '#a4502a', '#6d3b1b'][(s + b) % 3];
        g.fillRect(6 + b * 19, y + 8, 16, 14);
        g.fillStyle = '#e8c56a';
        g.fillRect(8 + b * 19, y + 12, 12, 3);
      }
    }
  }));
}

function speakerTex(): THREE.Texture {
  return nearest(canvasTex(32, 64, (g) => {
    g.fillStyle = '#0d0d10';
    g.fillRect(0, 0, 32, 64);
    for (const [y, r] of [[16, 11], [44, 14]] as const) {
      g.fillStyle = '#26262c';
      g.beginPath(); g.arc(16, y, r, 0, Math.PI * 2); g.fill();
      g.fillStyle = '#3a3a44';
      g.beginPath(); g.arc(16, y, r * 0.45, 0, Math.PI * 2); g.fill();
    }
  }));
}

/** A light beam: additive, brightest where it leaves the lamp, soft at its
 *  edges. The smoke in the room is what makes it read. */
function beamMaterial(color: string, length: number, opacity: number): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
    uniforms: { uColor: { value: new THREE.Color(color) }, uH: { value: length }, uOpacity: { value: opacity } },
    vertexShader: /* glsl */`
      varying float vY; varying float vF;
      void main() {
        vY = position.y;
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vec3 n = normalize(normalMatrix * normal);
        vF = abs(dot(n, normalize(-mv.xyz)));
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: /* glsl */`
      uniform vec3 uColor; uniform float uH; uniform float uOpacity;
      varying float vY; varying float vF;
      void main() {
        float a = 0.25 + 0.75 * smoothstep(-uH * 0.5, uH * 0.5, vY);
        a *= pow(vF, 1.6) * uOpacity;
        gl_FragColor = vec4(uColor * a, a);
      }`,
  });
}

// ── people ─────────────────────────────────────────────────────────────────

interface Joints {
  legL: number; legR: number; legLz: number; legRz: number;
  armL: number; armR: number; armLz: number; armRz: number;
  tiltX: number; tiltZ: number; dip: number; headX: number; headY: number; headZ: number;
}
const ZERO: Joints = { legL: 0, legR: 0, legLz: 0, legRz: 0, armL: 0, armR: 0, armLz: 0, armRz: 0, tiltX: 0, tiltZ: 0, dip: 0, headX: 0, headY: 0, headZ: 0 };

function apply(p: RatParts, j: Joints, seatY = 0): void {
  p.legL.rotation.set(j.legL, 0, j.legLz);
  p.legR.rotation.set(j.legR, 0, j.legRz);
  p.armL.rotation.set(j.armL, 0, j.armLz);
  p.armR.rotation.set(j.armR, 0, j.armRz);
  p.rig.rotation.set(j.tiltX, 0, j.tiltZ);
  p.rig.position.y = seatY + j.dip;
  p.head.rotation.set(j.headX, j.headY, j.headZ);
}

function damp(cur: Joints, to: Joints, k: number): void {
  for (const key of Object.keys(cur) as (keyof Joints)[]) cur[key] += (to[key] - cur[key]) * k;
}

export type Routine = 'circle' | 'spin' | 'sway' | 'hook';
export const ROUTINE_S: Record<Routine, number> = { circle: 6, spin: 4.2, sway: 6.5, hook: 5 };
/** Each dancer works through this set, starting at a different place. */
export const SET: Routine[] = ['circle', 'spin', 'sway', 'circle', 'hook', 'spin', 'sway'];

const LOOKS = [
  { suit: '#d4145a', trim: '#F2C66D', fur: '#efe6da', feather: '#ffffff' },
  { suit: '#0fa37a', trim: '#F2C66D', fur: '#8a8178', feather: '#F2C66D' },
  { suit: '#7c3aed', trim: '#dcdcec', fur: '#c9b8a6', feather: '#e9d5ff' },
  { suit: '#f2b316', trim: '#fff3c4', fur: '#5c524a', feather: '#ff7ab8' },
];

/** A Tailfeather: a rat in a sequinned two-piece with a beaded fringe belt,
 *  pearls, bangles, heels and a feathered headband, working her pole. */
export class Dancer {
  readonly parts = newRatParts();
  readonly pole: readonly [number, number];
  private sequin: THREE.MeshStandardMaterial;
  private j: Joints = { ...ZERO };
  private theta: number;
  private lift = 0;
  private r = 0.34;
  private step = 0;
  private setI: number;
  private rt: number;
  private frozen = 0;

  constructor(kit: CrewKit, i: number) {
    const look = LOOKS[i % LOOKS.length];
    this.pole = POLES[i % POLES.length];
    this.setI = (i * 2) % SET.length;
    this.rt = i * 1.3;
    this.theta = -Math.PI / 2 + i;
    const p = this.parts;
    const g = kit.g;
    const shadow = new THREE.Mesh(g.shadow, kit.shadowMat);
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.012;
    shadow.scale.setScalar(0.8);
    p.group.add(shadow, p.rig);
    const m = (geo: THREE.BufferGeometry, mat: THREE.Material, x = 0, y = 0, z = 0) => {
      const o = new THREE.Mesh(geo, mat);
      o.position.set(x, y, z);
      return o;
    };
    const fur = kit.mat(look.fur, { rough: 0.8 });
    const gold = kit.mat(look.trim, { rough: 0.3 });
    // the sequins are their own glowing material, so they shimmer as she turns
    this.sequin = new THREE.MeshStandardMaterial({
      color: look.suit, roughness: 0.3, metalness: 0.4, flatShading: true, emissive: look.suit, emissiveIntensity: 0.25,
    });
    // legs: fur, and heels
    for (const [leg, sx] of [[p.legL, -0.085], [p.legR, 0.085]] as const) {
      leg.position.set(sx, 0.36, 0);
      const l = m(g.limb, fur, 0, -0.15, 0);
      l.scale.set(0.8, 1, 0.8);
      leg.add(l, m(new THREE.BoxGeometry(0.085, 0.06, 0.16), kit.mat(look.suit), 0, -0.3, 0.03), m(new THREE.BoxGeometry(0.03, 0.07, 0.03), gold, 0, -0.33, -0.04));
      p.rig.add(leg);
    }
    // the body, and the two-piece: a sequinned band top with straps, and
    // sequinned bottoms under a fringe belt of beads
    const body = m(g.body, fur, 0, 0.6, 0);
    body.scale.set(0.82, 0.95, 0.78);
    const top = m(new THREE.CylinderGeometry(0.172, 0.168, 0.11, 10, 1, true), this.sequin, 0, 0.71, 0);
    top.scale.z = 0.82;
    const bottoms = m(new THREE.CylinderGeometry(0.17, 0.158, 0.11, 10, 1, true), this.sequin, 0, 0.42, 0);
    bottoms.scale.z = 0.84;
    p.rig.add(body, top, bottoms);
    for (const sx of [-1, 1]) {
      const strap = m(new THREE.BoxGeometry(0.022, 0.15, 0.02), gold, sx * 0.085, 0.8, 0.05);
      strap.rotation.x = -0.45;
      p.rig.add(strap);
    }
    p.rig.add(m(new THREE.TorusGeometry(0.02, 0.006, 4, 8), gold, 0, 0.71, 0.142));
    const bead = new THREE.BoxGeometry(0.011, 0.075, 0.011);
    for (let k = 0; k < 20; k++) {
      const a = (k / 20) * Math.PI * 2;
      p.rig.add(m(bead, k % 2 ? gold : kit.mat(look.suit), Math.sin(a) * 0.168, 0.335, Math.cos(a) * 0.142));
    }
    const pearls = m(new THREE.TorusGeometry(0.11, 0.015, 4, 10), kit.mat('#f8f4ea', { rough: 0.2 }), 0, 0.85, 0.02);
    pearls.rotation.x = Math.PI / 2 - 0.25;
    p.rig.add(pearls);
    buildRatTail(kit, p.rig);
    // arms: fur, pink paws, gold bangles
    for (const [arm, sx] of [[p.armL, -0.2], [p.armR, 0.2]] as const) {
      arm.position.set(sx, 0.76, 0);
      const a = m(g.limb, fur, 0, -0.13, 0);
      a.scale.set(0.78, 1, 0.78);
      const bangle = m(new THREE.TorusGeometry(0.058, 0.012, 4, 8), gold, 0, -0.2, 0);
      bangle.rotation.x = Math.PI / 2;
      arm.add(a, m(g.hand, kit.mat(PINK), 0, -0.27, 0), bangle);
      p.rig.add(arm);
    }
    // the head: a rat's, with lashes, rouge, and a feathered headband
    buildRatHead(kit, p.head, look.fur, 'none');
    for (const sx of [-1, 1]) {
      for (const k of [-1, 0, 1]) {
        const lash = m(new THREE.BoxGeometry(0.008, 0.045, 0.008), kit.mat('#0d0a0c'), sx * (0.11 + k * 0.022), 0.15, 0.225);
        lash.rotation.z = -sx * 0.4 - k * 0.3;
        p.head.add(lash);
      }
      const blush = m(new THREE.SphereGeometry(0.04, 6, 4), kit.mat('#f08aa0'), sx * 0.16, -0.03, 0.2);
      blush.scale.set(1, 0.6, 0.3);
      p.head.add(blush);
    }
    const band = m(new THREE.TorusGeometry(0.262, 0.02, 4, 12), gold, 0, 0.1, 0);
    band.rotation.x = Math.PI / 2 - 0.2;
    const gem = m(new THREE.OctahedronGeometry(0.04, 0), kit.mat(look.suit, { rough: 0.2 }), 0.12, 0.17, 0.2);
    p.head.add(band, gem);
    const feather = new THREE.SphereGeometry(0.06, 6, 4);
    for (const [fx, fy, fz, rx, rz, sc] of [[0.15, 0.33, 0.06, -0.55, -0.5, 1], [0.2, 0.27, 0.0, -0.8, -0.95, 0.75]] as const) {
      const f = m(feather, kit.mat(look.feather, { rough: 0.9 }), fx, fy, fz);
      f.scale.set(0.55 * sc, 2.9 * sc, 0.22);
      f.rotation.set(rx, 0, rz);
      p.head.add(f);
    }
    p.rig.add(p.head);

    for (const part of [p.legL, p.legR, p.armL, p.armR]) bakePart(part, kit.skinMat, false);
    bakePart(p.rig, kit.skinMat, false);
    bakePart(p.head, kit.skinMat, false);
  }

  get group(): THREE.Group { return this.parts.group; }

  /** Stop dead and put both paws up (a gun went off). */
  spook(secs: number): void {
    this.frozen = Math.max(this.frozen, secs);
  }

  /** Advance; returns the routine she just finished, if she did. */
  update(dt: number, t: number, i: number): Routine | null {
    let done: Routine | null = null;
    const to: Joints = { ...ZERO };
    let omega = 0, liftTo = 0, rTo = 0.34;
    let facing: 'travel' | 'out' | 'in' = 'travel';
    if (this.frozen > 0) {
      this.frozen -= dt;
      facing = 'out';
      to.armL = -2.75 + Math.sin(t * 30) * 0.06; to.armLz = -0.5;
      to.armR = -2.75 - Math.sin(t * 30) * 0.06; to.armRz = 0.5;
      to.headX = -0.1;
    } else {
      this.rt += dt;
      const routine = SET[this.setI];
      if (this.rt >= ROUTINE_S[routine]) {
        done = routine;
        this.rt = 0;
        this.setI = (this.setI + 1) % SET.length;
      }
      const r = SET[this.setI];
      const k = this.rt / ROUTINE_S[r];
      const w = t * 2.4 + i;
      switch (r) {
        case 'circle': {
          // walking round it, the inside paw high on the brass
          omega = 0.75;
          this.step += dt * 6.5;
          to.legL = Math.sin(this.step) * 0.35;
          to.legR = -Math.sin(this.step) * 0.35;
          to.armL = -2.75; to.armLz = -0.35;
          to.armR = -1.0 + Math.sin(t * 2 + i) * 0.3; to.armRz = 1.0;
          to.tiltZ = -0.12;
          to.dip = -Math.abs(Math.sin(this.step)) * 0.03;
          to.headY = 0.25;
          break;
        }
        case 'spin': {
          // up the pole, then a long spin down it, knees tucked
          const climb = Math.min(1, k / 0.25);
          if (k < 0.25) {
            omega = 1.2;
            liftTo = 0.6 * climb * climb * (3 - 2 * climb);
          } else {
            const d = (k - 0.25) / 0.75;
            omega = 3.4 * (1 - 0.6 * d);
            liftTo = 0.6 * (1 - d);
          }
          rTo = 0.3;
          to.armL = -2.9; to.armLz = -0.45;
          to.armR = -2.75; to.armRz = -0.6;
          to.legL = -1.25; to.legR = -0.7 + Math.sin(t * 3) * 0.1;
          to.tiltZ = -0.25; to.tiltX = -0.1;
          to.headX = -0.15; to.headZ = -0.2;
          break;
        }
        case 'sway': {
          // back to the brass, facing the room, paws up behind, hips going
          facing = 'out';
          rTo = 0.28;
          const want = -Math.PI / 2;
          let dth = want - this.theta;
          dth = Math.atan2(Math.sin(dth), Math.cos(dth));
          this.theta += dth * Math.min(1, dt * 3);
          to.armL = -2.85; to.armLz = 0.3;
          to.armR = -2.85; to.armRz = -0.3;
          to.tiltZ = Math.sin(w) * 0.13;
          to.dip = -(0.5 - 0.5 * Math.cos(w * 2)) * 0.07;
          to.legL = -0.12 - Math.max(0, Math.sin(w)) * 0.25;
          to.legR = -0.12 - Math.max(0, -Math.sin(w)) * 0.25;
          to.headZ = Math.sin(t * 1.2 + i) * 0.12;
          to.headY = Math.sin(t * 0.6 + i) * 0.3;
          break;
        }
        case 'hook': {
          // facing the pole, a leg hooked round it, leaning way back
          facing = 'in';
          omega = 0.55;
          rTo = 0.38;
          to.legR = -1.45; to.legRz = -0.2;
          to.legL = 0.05;
          to.armL = -2.2; to.armLz = 0.2;
          to.armR = -2.2; to.armRz = -0.2;
          to.tiltX = -0.4;
          to.headX = -0.3;
          break;
        }
      }
    }
    this.theta += omega * dt;
    this.lift += (liftTo - this.lift) * Math.min(1, dt * 5);
    this.r += (rTo - this.r) * Math.min(1, dt * 3);
    damp(this.j, to, 1 - Math.exp(-7 * dt));

    const c = Math.cos(this.theta), s = Math.sin(this.theta);
    const g = this.group;
    g.position.set(this.pole[0] + c * this.r, STAGE.h + this.lift, this.pole[1] + s * this.r);
    g.rotation.y = facing === 'travel' ? -this.theta : facing === 'out' ? Math.PI / 2 - this.theta : Math.atan2(-c, -s);
    apply(this.parts, this.j);
    this.sequin.emissiveIntensity = 0.22 + 0.22 * Math.max(0, Math.sin(t * 5 + this.theta * 3 + i));
    return done;
  }

  dispose(): void {
    this.sequin.dispose();
  }
}

export type ClubRole = 'patron' | 'bartender' | 'bouncer' | 'clubber' | 'dj';

/** Everyone else in the club: patrons in the lounge with their cigars, the
 *  crowd on the dance floor, the bartenders, Knuckles at the door, Gramps
 *  on the decks. */
export class ClubRat {
  readonly parts = newRatParts();
  readonly ember: THREE.Mesh | null;
  readonly persona: Persona | null;
  readonly name: string;
  private j: Joints = { ...ZERO };
  private phase = Math.random() * 10;
  private heldBy: { x: number; z: number } | null = null;
  private scared = 0;
  private clap = 0;
  private mixing = 0;
  private baseYaw: number;
  private baseY: number;
  private danceStyle: number;

  constructor(
    kit: CrewKit, readonly role: ClubRole, style: RoleStyle, fur: string, x: number, z: number, yaw: number, name: string,
    o: { emberMat: THREE.Material; smoke?: 'none' | 'cig' | 'cigar'; y?: number; persona?: Persona | null; extra?: (p: RatParts) => void; style?: number },
  ) {
    this.name = name;
    this.persona = o.persona ?? null;
    this.danceStyle = o.style ?? 0;
    const p = this.parts;
    const smoke = o.smoke ?? (role === 'patron' ? 'cigar' : 'none');
    const r = buildRat(kit, p, style, { fur, smoke, emberMat: o.emberMat, castShadow: false });
    o.extra?.(p);
    for (const part of [p.legL, p.legR, p.armL, p.armR]) bakePart(part, kit.skinMat, false);
    bakePart(p.rig, kit.skinMat, false);
    bakePart(p.head, kit.skinMat, false);
    this.ember = r.ember;
    if (r.ember) p.head.add(r.ember);
    this.baseY = o.y ?? 0;
    p.group.position.set(x, this.baseY, z);
    p.group.rotation.y = yaw;
    this.baseYaw = yaw;
  }

  get group(): THREE.Group { return this.parts.group; }
  get x(): number { return this.parts.group.position.x; }
  get z(): number { return this.parts.group.position.z; }
  get seated(): boolean { return this.role === 'patron'; }

  headWorld(out: THREE.Vector3): THREE.Vector3 {
    return out.set(this.x, this.baseY + (this.seated ? 1.25 : 1.55), this.z);
  }

  /** Someone's talking to it: it turns to them until let go. */
  hold(at: { x: number; z: number } | null): void {
    this.heldBy = at;
  }

  spook(secs: number): void {
    this.scared = Math.max(this.scared, secs);
  }

  applaud(secs: number): void {
    this.clap = Math.max(this.clap, secs);
  }

  /** A bartender shakes a cocktail. */
  mix(secs: number): void {
    this.mixing = Math.max(this.mixing, secs);
  }

  update(dt: number, t: number, watch: { x: number; z: number } | null): void {
    this.phase += dt;
    const ph = this.phase;
    const to: Joints = { ...ZERO };
    let yaw = this.baseYaw;
    // the club's beat: about a hundred and twenty to the minute
    const beat = t * Math.PI * 4;
    switch (this.role) {
      case 'patron': {
        to.legL = -1.35; to.legR = -1.3;
        to.armL = -0.6; to.armLz = 0.15;
        to.tiltX = -0.12;
        // the cigar now and then
        const puff = Math.max(0, Math.sin(ph * 0.5)) ** 6;
        to.armR = -0.6 - puff * 1.5; to.armRz = -0.15 - puff * 0.3;
        if (watch) {
          let a = Math.atan2(watch.x - this.x, watch.z - this.z) - this.baseYaw;
          a = Math.atan2(Math.sin(a), Math.cos(a));
          to.headY = Math.max(-1, Math.min(1, a));
        }
        if (this.clap > 0) {
          this.clap -= dt;
          const c = Math.sin(ph * 22);
          to.armL = -1.3; to.armR = -1.3;
          to.armLz = 0.35 + c * 0.18; to.armRz = -0.35 - c * 0.18;
          to.headX = -0.15;
        }
        break;
      }
      case 'clubber': {
        to.dip = -Math.abs(Math.sin(beat / 2)) * 0.08;
        const s = this.danceStyle % 3;
        if (s === 0) {
          // a fist pumping on the beat
          to.armR = -2.6 + Math.sin(beat) * 0.4; to.armRz = 0.2;
          to.armL = -0.5 + Math.sin(beat) * 0.2;
          to.headX = Math.sin(beat) * 0.1;
        } else if (s === 1) {
          // both paws up, swaying
          to.armL = -2.8; to.armR = -2.8;
          to.armLz = -0.45 + Math.sin(beat / 4) * 0.3; to.armRz = 0.45 + Math.sin(beat / 4) * 0.3;
          to.tiltZ = Math.sin(beat / 4) * 0.12;
        } else {
          // the shuffle
          to.legL = Math.sin(beat / 2) * 0.4; to.legR = -Math.sin(beat / 2) * 0.4;
          to.armL = -1.2 + Math.sin(beat / 2) * 0.4; to.armR = -1.2 - Math.sin(beat / 2) * 0.4;
          to.headY = Math.sin(beat / 8) * 0.4;
        }
        yaw = this.baseYaw + Math.sin(ph * 0.3) * 0.8;
        break;
      }
      case 'bartender': {
        if (this.mixing > 0) {
          // shaking: the shaker over the shoulder, both paws on it
          this.mixing -= dt;
          to.armR = -2.3 + Math.sin(ph * 28) * 0.28; to.armRz = -0.3;
          to.armL = -2.1 + Math.sin(ph * 28 + 0.6) * 0.28; to.armLz = 0.3;
          to.headX = 0.1;
          to.dip = Math.sin(ph * 28) * 0.01;
        } else {
          // polishing a glass that is never clean enough
          to.armR = -1.3 + Math.sin(ph * 5) * 0.15; to.armRz = Math.sin(ph * 5 + 1) * 0.3;
          to.armL = -1.15;
          to.headY = Math.sin(ph * 0.4) * 0.5;
          to.dip = Math.sin(ph * 2.2) * 0.012;
        }
        break;
      }
      case 'dj': {
        // on the decks: a paw on each, nodding; now and then one goes up
        to.armL = -1.25 + Math.sin(beat) * 0.06; to.armLz = 0.25;
        const hype = Math.sin(ph * 0.4) > 0.85;
        to.armR = hype ? -2.8 : -1.25 + Math.sin(beat * 2) * 0.1; to.armRz = hype ? 0.3 : -0.25;
        to.headX = 0.12 + Math.abs(Math.sin(beat / 2)) * 0.15;
        to.dip = -Math.abs(Math.sin(beat / 2)) * 0.04;
        break;
      }
      default: {
        // the bouncer: arms folded, watching the door
        to.armL = -1.25; to.armLz = 0.55;
        to.armR = -1.35; to.armRz = -0.55;
        to.headY = Math.sin(ph * 0.3) * 0.6;
        to.dip = Math.sin(ph * 2) * 0.01;
      }
    }
    if (this.heldBy) yaw = Math.atan2(this.heldBy.x - this.x, this.heldBy.z - this.z);
    if (this.scared > 0) {
      this.scared -= dt;
      to.armL = -2.75 + Math.sin(ph * 30) * 0.07; to.armLz = -0.5;
      to.armR = -2.75 - Math.sin(ph * 30) * 0.07; to.armRz = 0.5;
      to.headY = 0;
      to.dip = 0;
    }
    damp(this.j, to, 1 - Math.exp(-8 * dt));
    let dy = yaw - this.group.rotation.y;
    dy = Math.atan2(Math.sin(dy), Math.cos(dy));
    this.group.rotation.y += dy * Math.min(1, dt * 6);
    // seated: the hips down on the cushion
    apply(this.parts, this.j, this.seated ? 0.07 : 0);
  }
}

interface Head { at: THREE.Vector3; housing: THREE.Object3D; beam: THREE.Mesh; mat: THREE.ShaderMaterial; color: THREE.Color }

const BEAM_L = 6.5;

// ── the room ───────────────────────────────────────────────────────────────

export class ClubRoom {
  /** On foot only. Holds the doors and the sign over them (seen from the
   *  hall), and `inside`, the club itself. */
  readonly group = new THREE.Group();
  /** The club: drawn (and animated) only while you could see into it. */
  readonly inside = new THREE.Group();
  readonly dancers: Dancer[] = [];
  /** The backstage crew, in red high-tops (backstage.ts). */
  readonly crew: BackupDancer[] = [];
  private crewBeams: { beam: THREE.Mesh; mat: THREE.ShaderMaterial }[] = [];
  private lastMove = -1;
  readonly rats: ClubRat[] = [];
  private leaves: THREE.Group[] = [];
  private dynamic: THREE.Object3D[] = [];
  private chase: THREE.MeshStandardMaterial[] = [];
  private pulse: THREE.MeshStandardMaterial[] = [];
  private heads: Head[] = [];
  private platters: THREE.Mesh[] = [];
  private owned: { dispose(): void }[] = [];
  private _c = new THREE.Color();
  private _v = new THREE.Vector3();
  private _down = new THREE.Vector3(0, -1, 0);

  constructor(kit: CrewKit) {
    this.group.visible = false;
    this.group.add(this.inside);
    this.buildRoom();
    this.buildStage();
    this.buildLights();
    this.buildBar();
    this.buildLounge();
    // merge everything that never moves, then add the doors and the people
    mergeStatic(this.inside, this.dynamic, (m) => !this.chase.includes(m as THREE.MeshStandardMaterial) && !this.pulse.includes(m as THREE.MeshStandardMaterial));
    this.buildDoors();
    this.buildPeople(kit);
  }

  private add<T extends THREE.Object3D>(o: T, x: number, y: number, z: number, to: THREE.Group = this.inside): T {
    o.position.set(x, y, z);
    to.add(o);
    return o;
  }

  private box(w: number, h: number, d: number, mat: THREE.Material, x: number, y: number, z: number, to: THREE.Group = this.inside): THREE.Mesh {
    const m = new THREE.Mesh(this.keep(new THREE.BoxGeometry(w, h, d)), mat);
    m.receiveShadow = true;
    return this.add(m, x, y, z, to);
  }

  private keep<T extends { dispose(): void }>(x: T): T {
    this.owned.push(x);
    return x;
  }

  private sign(tex: THREE.Texture, w: number, h: number, x: number, y: number, z: number, rotY: number, to: THREE.Group = this.inside): THREE.Mesh {
    const mat = this.keep(new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false }));
    mat.color.setScalar(1.6);
    const s = new THREE.Mesh(this.keep(new THREE.PlaneGeometry(w, h)), mat);
    s.rotation.y = rotY;
    return this.add(s, x, y, z, to);
  }

  // ── the shell ────────────────────────────────────────────────────────────

  private buildRoom(): void {
    const { x0, x1, z0, z1 } = CLUB;
    const cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
    const floor = new THREE.Mesh(this.keep(new THREE.PlaneGeometry(W, D)), this.keep(std('#ffffff', { rough: 0.95, map: this.keep(carpetTex()) })));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    this.add(floor, cx, 0.004, cz);
    const ceiling = new THREE.Mesh(this.keep(new THREE.PlaneGeometry(W, D)), this.keep(std('#08050a', { rough: 0.95, e: '#050306', ei: 1 })));
    ceiling.rotation.x = Math.PI / 2;
    this.add(ceiling, cx, CLUB_CEIL, cz);
    // walls: dark wainscot, black velvet above, a brass rail between
    const wains = this.keep(std('#1a0b08', { rough: 0.5 }));
    const velvet = this.keep(std('#1c0812', { rough: 0.95 }));
    const brass = this.keep(std('#C79A45', { rough: 0.3, metal: 0.85 }));
    for (const [w, d, x, z] of [[WALL_T, D + 0.24, x0, cz], [WALL_T, D + 0.24, x1, cz], [W + 0.24, WALL_T, cx, z1]] as const) {
      this.box(w, 1.1, d, wains, x, 0.55, z);
      this.box(w, CLUB_CEIL - 1.1, d, velvet, x, 1.1 + (CLUB_CEIL - 1.1) / 2, z);
      this.box(w === WALL_T ? WALL_T + 0.05 : w, 0.05, w === WALL_T ? d : WALL_T + 0.05, brass, x, 1.12, z);
    }
    // the club is taller than the hall: the front wall above the hall's ceiling
    this.box(W + 0.24, CLUB_CEIL - CEIL_H + 0.05, WALL_T, velvet, cx, CEIL_H + (CLUB_CEIL - CEIL_H) / 2, z0);
    // uplights: colour strips up the walls, pulsing (two sets, out of step)
    for (let k = 0; k < 2; k++) {
      const col = k ? '#ff3fa4' : '#8b5cf6';
      const mat = this.keep(std(col, { e: col, ei: 1.6 }));
      this.pulse.push(mat);
      // the back corners either side of the stage, and the front wall either side of the door
      const strips: [number, number, boolean][] = [
        [x0 + 0.13, 23.4, true], [x0 + 0.13, 25.6, true], [x1 - 0.13, 23.4, true], [x1 - 0.13, 25.6, true],
        [-3.0, z0 + 0.13, false], [-6.0, z0 + 0.13, false], [4.0, z0 + 0.13, false], [7.0, z0 + 0.13, false],
      ];
      strips.forEach(([x, z, side], i) => {
        if (i % 2 !== k) return;
        this.box(side ? 0.04 : 0.1, 3.4, side ? 0.1 : 0.04, mat, x, 1.9, z);
      });
    }
    // inside, over the door: the club's name again
    this.sign(this.keep(clubSign()), 2.4, 0.75, CLUB_DOOR[0], 3.15, z0 + 0.14, 0);
  }

  // ── the stage, the poles, the podiums, the DJ ────────────────────────────

  private buildStage(): void {
    const { z1 } = CLUB;
    // matte: a glossy stage under the spots threw a blinding highlight
    const deck = this.keep(std('#140a0e', { rough: 0.85 }));
    const skirt = this.keep(std('#0c0608', { rough: 0.9 }));
    const gold = this.keep(std('#D8B060', { rough: 0.35, metal: 0.8 }));
    const chrome = this.keep(std('#e6e1d6', { rough: 0.15, metal: 0.95 }));
    const S = STAGE;
    // the main stage: round, a gold lip, a pink underglow ring at its foot
    this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(S.r, S.r, S.h, 48)), deck), S.x, S.h / 2, S.z);
    this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(S.r + 0.02, S.r + 0.02, 0.24, 48, 1, true)), skirt), S.x, 0.12, S.z);
    const lip = new THREE.Mesh(this.keep(new THREE.TorusGeometry(S.r, 0.035, 4, 48)), gold);
    lip.rotation.x = Math.PI / 2;
    this.add(lip, S.x, S.h, S.z);
    const glowMat = this.keep(std('#ff2d95', { e: '#ff2d95', ei: 1.4 }));
    this.pulse.push(glowMat);
    const glow = new THREE.Mesh(this.keep(new THREE.TorusGeometry(S.r + 0.03, 0.025, 4, 48)), glowMat);
    glow.rotation.x = Math.PI / 2;
    this.add(glow, S.x, 0.05, S.z);
    // the runway back to the curtain, and the backstage under it
    const R = RUNWAY, B = BACKSTAGE;
    this.box(R.x1 - R.x0, S.h, R.z1 - R.z0, deck, (R.x0 + R.x1) / 2, S.h / 2, (R.z0 + R.z1) / 2);
    this.box(B.x1 - B.x0, S.h, B.z1 - B.z0, deck, (B.x0 + B.x1) / 2, S.h / 2, (B.z0 + B.z1) / 2);
    for (const x of [R.x0, R.x1]) this.box(0.04, 0.04, R.z1 - R.z0, gold, x, S.h, (R.z0 + R.z1) / 2);
    this.box(B.x1 - B.x0, 0.04, 0.04, gold, (B.x0 + B.x1) / 2, S.h, B.z0);
    // marquee bulbs round the main stage, down the runway, round the podiums
    const bulbs: [number, number, number][] = [];
    const ring = (x: number, z: number, r: number, from = 0, to = Math.PI * 2) => {
      const n = Math.round((r * (to - from)) / 0.3);
      for (let i = 0; i <= n; i++) {
        const a = from + ((to - from) * i) / n;
        bulbs.push([x + Math.cos(a) * (r + 0.02), S.h - 0.16, z + Math.sin(a) * (r + 0.02)]);
      }
    };
    // (not where the runway joins)
    const gap = Math.asin(((R.x1 - R.x0) / 2 + 0.1) / S.r);
    ring(S.x, S.z, S.r, Math.PI / 2 + gap, Math.PI * 2.5 - gap);
    for (let z = Math.max(R.z0, S.z + S.r - 0.1); z < R.z1; z += 0.3) for (const x of [R.x0 - 0.02, R.x1 + 0.02]) bulbs.push([x, S.h - 0.16, z]);
    for (const p of PODIUMS) ring(p.x, p.z, p.r);
    const bulbGeo = this.keep(new THREE.SphereGeometry(0.032, 6, 4));
    for (let set = 0; set < 2; set++) {
      const mat = this.keep(std('#fff1c8', { e: '#FFC56E', ei: 2 }));
      this.chase.push(mat);
      const mine = bulbs.filter((_, i) => i % 2 === set);
      const im = new THREE.InstancedMesh(bulbGeo, mat, mine.length);
      const m4 = new THREE.Matrix4();
      mine.forEach(([x, y, z], i) => im.setMatrixAt(i, m4.makeTranslation(x, y, z)));
      this.inside.add(im);
    }
    // the curtain behind the backstage: velvet folds, floor to ceiling
    const cw = B.x1 - B.x0 + 1.2, cx = (B.x0 + B.x1) / 2;
    const cg = this.keep(new THREE.PlaneGeometry(cw, CLUB_CEIL - S.h, 60, 1));
    const pos = cg.attributes.position as THREE.BufferAttribute;
    for (let i = 0; i < pos.count; i++) pos.setZ(i, Math.sin(pos.getX(i) * 7) * 0.07);
    cg.computeVertexNormals();
    const curtain = new THREE.Mesh(cg, this.keep(std('#5e0c19', { rough: 0.9 })));
    curtain.rotation.y = Math.PI;
    this.add(curtain, cx, S.h + (CLUB_CEIL - S.h) / 2, z1 - 0.2);
    // the name, big, over the curtain
    this.sign(this.keep(clubSign()), 5.6, 1.75, cx, CLUB_CEIL - 1.05, z1 - 0.32, Math.PI);
    // podiums: round, gold-rimmed, underglow too
    for (const p of PODIUMS) {
      this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(p.r, p.r * 1.04, S.h, 24)), deck), p.x, S.h / 2, p.z);
      const rim = new THREE.Mesh(this.keep(new THREE.TorusGeometry(p.r, 0.03, 4, 24)), gold);
      rim.rotation.x = Math.PI / 2;
      this.add(rim, p.x, S.h, p.z);
      const pg = new THREE.Mesh(this.keep(new THREE.TorusGeometry(p.r + 0.04, 0.022, 4, 24)), glowMat);
      pg.rotation.x = Math.PI / 2;
      this.add(pg, p.x, 0.05, p.z);
    }
    // the poles, floor to ceiling, a soft spot down each and a pool
    const shaftL = CLUB_CEIL - S.h;
    const poleGeo = this.keep(new THREE.CylinderGeometry(0.03, 0.03, shaftL, 8));
    const flange = this.keep(new THREE.CylinderGeometry(0.09, 0.11, 0.04, 10));
    const shaftGeo = this.keep(new THREE.CylinderGeometry(0.1, 0.8, shaftL, 18, 1, true));
    const colors = ['#e0368f', '#c0367f', '#7d4dff', '#e6a336'];
    POLES.forEach(([x, z], i) => {
      this.add(new THREE.Mesh(poleGeo, chrome), x, S.h + shaftL / 2, z);
      this.add(new THREE.Mesh(flange, chrome), x, S.h + 0.02, z);
      this.add(new THREE.Mesh(flange, chrome), x, CLUB_CEIL - 0.02, z);
      const shaft = new THREE.Mesh(shaftGeo, this.keep(beamMaterial(colors[i], shaftL, 0.16)));
      shaft.renderOrder = 3;
      this.dynamic.push(shaft);
      this.add(shaft, x, S.h + shaftL / 2, z);
      const pool = new THREE.Mesh(this.keep(new THREE.CircleGeometry(0.7, 16)), this.keep(new THREE.MeshBasicMaterial({
        color: colors[i], transparent: true, opacity: 0.08, blending: THREE.AdditiveBlending, depthWrite: false,
      })));
      pool.rotation.x = -Math.PI / 2;
      this.add(pool, x, S.h + 0.006, z);
    });
    // the DJ: a riser in the corner, the booth turned to the stage
    const Dr = DJ_RISER;
    this.box(Dr.x1 - Dr.x0, Dr.h, Dr.z1 - Dr.z0, skirt, (Dr.x0 + Dr.x1) / 2, Dr.h / 2, (Dr.z0 + Dr.z1) / 2);
    this.box(Dr.x1 - Dr.x0, 0.03, 0.03, gold, (Dr.x0 + Dr.x1) / 2, Dr.h, Dr.z0);
    this.box(0.03, 0.03, Dr.z1 - Dr.z0, gold, Dr.x1, Dr.h, (Dr.z0 + Dr.z1) / 2);
    const booth = new THREE.Group();
    booth.position.set(DJ_BOOTH[0], Dr.h, DJ_BOOTH[1]);
    booth.rotation.y = DJ_YAW;
    const part = (geo: THREE.BufferGeometry, mat: THREE.Material, x: number, y: number, z: number) => {
      const m = new THREE.Mesh(geo, mat);
      m.position.set(x, y, z);
      booth.add(m);
      return m;
    };
    part(this.keep(new THREE.BoxGeometry(2.0, 1.05, 0.6)), this.keep(std('#0e0c12', { rough: 0.5, metal: 0.3 })), 0, 0.525, 0.55);
    const front = this.keep(std('#1a0b20', { e: '#ff2d95', ei: 1.2 }));
    this.pulse.push(front);
    part(this.keep(new THREE.BoxGeometry(1.9, 0.85, 0.02)), front, 0, 0.5, 0.86);
    const vinyl = this.keep(std('#111114', { rough: 0.3 }));
    const deckM = this.keep(std('#2a2a30', { metal: 0.5 }));
    for (const ox of [-0.55, 0.55]) {
      part(this.keep(new THREE.BoxGeometry(0.46, 0.06, 0.46)), deckM, ox, 1.08, 0.55);
      const platter = part(this.keep(new THREE.CylinderGeometry(0.19, 0.19, 0.02, 14)), vinyl, ox, 1.12, 0.55);
      platter.add(new THREE.Mesh(this.keep(new THREE.BoxGeometry(0.1, 0.022, 0.03)), this.keep(std('#e0368f'))));
      this.platters.push(platter);
      this.dynamic.push(platter);
    }
    const spk = this.keep(std('#ffffff', { rough: 0.8, map: this.keep(speakerTex()) }));
    for (const ox of [-1.4, 1.4]) part(this.keep(new THREE.BoxGeometry(0.7, 1.6, 0.6)), spk, ox, 0.8, 0.3);
    this.inside.add(booth);
  }

  // ── the lights ───────────────────────────────────────────────────────────

  private buildLights(): void {
    const S = STAGE;
    const truss = this.keep(std('#26262c', { metal: 0.7, rough: 0.4 }));
    // a ring of truss over the main stage, hung from the ceiling
    const tr = S.r + 0.6;
    const ringM = new THREE.Mesh(this.keep(new THREE.TorusGeometry(tr, 0.11, 4, 40)), truss);
    ringM.rotation.x = Math.PI / 2;
    this.add(ringM, S.x, TRUSS_Y, S.z);
    for (let k = 0; k < 4; k++) {
      const a = (k / 4) * Math.PI * 2 + Math.PI / 4;
      this.box(0.05, CLUB_CEIL - TRUSS_Y, 0.05, truss, S.x + Math.cos(a) * tr, (CLUB_CEIL + TRUSS_Y) / 2, S.z + Math.sin(a) * tr);
    }
    // moving heads round the ring, each working the stage's dancers
    const beamGeo = this.keep(new THREE.CylinderGeometry(0.05, 0.6, BEAM_L, 16, 1, true));
    const yoke = this.keep(std('#141418', { metal: 0.6, rough: 0.4 }));
    const lens = this.keep(std('#ffffff', { e: '#ffffff', ei: 2.2 }));
    const bodyGeo = this.keep(new THREE.BoxGeometry(0.22, 0.22, 0.3));
    const glassGeo = this.keep(new THREE.CircleGeometry(0.08, 10));
    for (let i = 0; i < 6; i++) {
      const a = (i / 6) * Math.PI * 2 + Math.PI / 6;
      const housing = new THREE.Group();
      const glass = new THREE.Mesh(glassGeo, lens);
      glass.position.z = 0.151;
      housing.add(new THREE.Mesh(bodyGeo, yoke), glass);
      const pos = new THREE.Vector3(S.x + Math.cos(a) * tr, TRUSS_Y - 0.28, S.z + Math.sin(a) * tr);
      this.add(housing, pos.x, pos.y, pos.z);
      const color = new THREE.Color(PALETTE[i % PALETTE.length]);
      const mat = this.keep(beamMaterial('#ffffff', BEAM_L, 0.13));
      mat.uniforms.uColor.value.copy(color);
      const beam = new THREE.Mesh(beamGeo, mat);
      beam.renderOrder = 3;
      this.inside.add(beam);
      this.dynamic.push(housing, beam);
      this.heads.push({ at: pos, housing, beam, mat, color });
    }
  }

  // ── the bar ──────────────────────────────────────────────────────────────

  private buildBar(): void {
    const b = CLUB_BAR, x1 = CLUB.x1;
    const len = b.z1 - b.z0, bz = (b.z0 + b.z1) / 2;
    const wood = this.keep(std('#1e0e08', { rough: 0.45 }));
    const brass = this.keep(std('#C79A45', { rough: 0.3, metal: 0.85 }));
    this.box(0.55, 1.05, len, wood, b.x + 0.275, 0.525, bz);
    this.box(0.72, 0.05, len + 0.1, this.keep(std('#2a1410', { rough: 0.18, metal: 0.2 })), b.x + 0.29, 1.07, bz);
    // a pink LED line under the bar top, a brass foot rail
    this.box(0.02, 0.04, len, this.keep(std('#ff3fa4', { e: '#ff3fa4', ei: 2 })), b.x - 0.01, 0.97, bz);
    this.box(0.04, 0.04, len, brass, b.x - 0.12, 0.2, bz);
    // the back bar: a warm backlit wall of bottles on three shelves
    this.box(0.03, 2.2, len, this.keep(std('#3a2008', { e: '#ffab4a', ei: 0.9 })), x1 - 0.14, 2.0, bz);
    for (const y of [1.2, 1.65, 2.1, 2.55]) this.box(0.26, 0.04, len, wood, x1 - 0.26, y, bz);
    {
      const bottle = this.keep(new THREE.CylinderGeometry(0.035, 0.042, 0.26, 6));
      const glass = this.keep(new THREE.MeshStandardMaterial({ roughness: 0.15, metalness: 0.1, flatShading: true, emissive: '#2a1206', emissiveIntensity: 0.7 }));
      const per = Math.floor(len / 0.16);
      const im = new THREE.InstancedMesh(bottle, glass, per * 3);
      const m4 = new THREE.Matrix4();
      const tints = ['#5a2e0c', '#2f5a2a', '#c97a2a', '#e6f0ff', '#7a1020', '#3a2a50', '#d8b060'];
      let k = 0;
      for (const y of [1.2, 1.65, 2.1]) for (let i = 0; i < per; i++) {
        im.setMatrixAt(k, m4.makeTranslation(x1 - 0.26, y + 0.15, b.z0 + 0.1 + i * 0.16));
        im.setColorAt(k, this._c.set(tints[(k * 5 + Math.floor(y * 3)) % tints.length]));
        k++;
      }
      this.inside.add(im);
    }
    this.sign(this.keep(neonText([{ text: 'COCKTAILS', font: '700 92px "Chakra Petch", sans-serif', y: 80, color: '#22d3ee', blur: 22 }])), 3.2, 1.0, x1 - 0.15, 3.35, bz, -Math.PI / 2);
    // the stock room, between the bar and the front wall: panelled, a door
    {
      const sx = (b.x + x1) / 2, sz = (CLUB.z0 + b.z0) / 2, sd = b.z0 - CLUB.z0 - 0.12;
      this.box(x1 - b.x - 0.12, 3.0, sd, wood, sx, 1.5, sz + 0.06);
      this.box(0.04, 2.1, 0.9, this.keep(std('#3b1d10', { rough: 0.4 })), b.x - 0.03, 1.05, sz);
      this.sign(this.keep(neonText([{ text: 'STAFF ONLY', font: '700 70px "Chakra Petch", sans-serif', y: 80, color: '#ff3fa4', blur: 16 }])), 1.3, 0.4, b.x - 0.06, 2.4, sz, -Math.PI / 2);
    }
    // stools along the front
    const seat = this.keep(std('#7a1020', { rough: 0.5 }));
    const chrome = this.keep(std('#c9c4bb', { metal: 0.9, rough: 0.2 }));
    const seatGeo = this.keep(new THREE.CylinderGeometry(0.2, 0.18, 0.08, 10));
    const postGeo = this.keep(new THREE.CylinderGeometry(0.03, 0.03, 0.76, 6));
    const baseGeo = this.keep(new THREE.CylinderGeometry(0.18, 0.2, 0.03, 10));
    for (const [x, z] of CLUB_STOOLS) {
      this.add(new THREE.Mesh(seatGeo, seat), x, 0.8, z);
      this.add(new THREE.Mesh(postGeo, chrome), x, 0.38, z);
      this.add(new THREE.Mesh(baseGeo, chrome), x, 0.015, z);
    }
    // a couple of glasses waiting on the bar
    const cg = this.keep(std('#e6f6ff', { rough: 0.1, metal: 0.2 }));
    const bowl = this.keep(new THREE.CylinderGeometry(0.05, 0.004, 0.07, 8, 1, true));
    const stem = this.keep(new THREE.CylinderGeometry(0.004, 0.004, 0.09, 4));
    for (const z of [13.6, 17.6, 20.0]) {
      this.add(new THREE.Mesh(bowl, cg), b.x + 0.2, 1.24, z);
      this.add(new THREE.Mesh(stem, cg), b.x + 0.2, 1.14, z);
    }
  }

  // ── the lounge ───────────────────────────────────────────────────────────

  private buildLounge(): void {
    const wood = this.keep(std('#2a140b', { rough: 0.45 }));
    const leather = this.keep(std('#5a1c12', { rough: 0.42 }));
    const brass = this.keep(std('#C79A45', { rough: 0.3, metal: 0.85 }));
    const shade = this.keep(std('#ffd9a0', { e: '#ffa94d', ei: 1.4 }));
    const table = (x: number, z: number, r: number) => {
      this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(r, r, 0.04, 12)), wood), x, 0.55, z);
      this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(0.05, 0.07, 0.53, 6)), wood), x, 0.27, z);
      this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(0.075, 0.07, 0.025, 8)), brass), x - 0.12, 0.585, z + 0.08);
      this.add(new THREE.Mesh(this.keep(new THREE.CylinderGeometry(0.02, 0.04, 0.16, 6)), brass), x + 0.05, 0.65, z - 0.12);
      this.add(new THREE.Mesh(this.keep(new THREE.ConeGeometry(0.1, 0.1, 8, 1, true)), shade), x + 0.05, 0.76, z - 0.12);
    };
    for (const [x, z] of CLUB_TABLES) table(x, z, 0.4);
    // club chairs, each turned to the dance floor
    const chair = (s: ClubSeat) => {
      const g = new THREE.Group();
      const part = (w: number, h: number, d: number, mat: THREE.Material, x: number, y: number, z: number) => {
        const m = new THREE.Mesh(this.keep(new THREE.BoxGeometry(w, h, d)), mat);
        m.position.set(x, y, z);
        g.add(m);
      };
      part(0.62, 0.2, 0.58, leather, 0, 0.32, 0);
      part(0.62, 0.58, 0.16, leather, 0, 0.6, -0.26);
      for (const sx of [-1, 1]) part(0.13, 0.32, 0.58, leather, sx * 0.3, 0.46, 0);
      part(0.5, 0.2, 0.46, wood, 0, 0.1, 0);
      g.rotation.y = s.yaw;
      this.add(g, s.x, 0, s.z);
    };
    clubChairs().forEach(chair);
    stageChairs().forEach(chair);
    // sofas along the left wall and in the VIP corners, a low table at each
    for (const f of CLUB_SOFAS) {
      const g = new THREE.Group();
      const part = (w: number, h: number, d: number, x: number, y: number, z: number) => {
        const m = new THREE.Mesh(this.keep(new THREE.BoxGeometry(w, h, d)), leather);
        m.position.set(x, y, z);
        g.add(m);
      };
      part(f.len, 0.22, 0.72, 0, 0.33, 0);
      part(f.len, 0.6, 0.18, 0, 0.63, -0.3);
      for (const sx of [-1, 1]) part(0.16, 0.36, 0.72, sx * (f.len / 2 - 0.08), 0.46, 0);
      g.rotation.y = f.yaw;
      this.add(g, f.x, 0, f.z);
      const [tx, tz] = sofaTable(f);
      table(tx, tz, 0.34);
    }
    // pendants over the lounge's tables
    const cord = this.keep(new THREE.CylinderGeometry(0.008, 0.008, 2.3, 4));
    const pend = this.keep(new THREE.ConeGeometry(0.22, 0.2, 8, 1, true));
    const bulbGeo = this.keep(new THREE.SphereGeometry(0.06, 6, 4));
    const bulb = this.keep(std('#fff4d6', { e: '#FFC56E', ei: 2.4 }));
    for (const [x, z] of [...CLUB_TABLES, ...CLUB_SOFAS.map(sofaTable)]) {
      this.add(new THREE.Mesh(cord, brass), x, CLUB_CEIL - 1.15, z);
      this.add(new THREE.Mesh(pend, shade), x, CLUB_CEIL - 2.35, z);
      this.add(new THREE.Mesh(bulbGeo, bulb), x, CLUB_CEIL - 2.42, z);
    }
    // warm sconces over the lounge's sofas
    for (const f of CLUB_SOFAS.slice(0, 3)) {
      this.box(0.08, 0.22, 0.18, shade, CLUB.x0 + 0.16, 2.1, f.z);
      this.box(0.1, 0.03, 0.22, brass, CLUB.x0 + 0.14, 1.97, f.z);
    }
    // the humidor: a tall cabinet, its glass front glowing over the boxes
    {
      const [hx, hz] = HUMIDOR;
      this.box(0.55, 1.95, 0.6, wood, hx - 0.1, 0.975, hz);
      const tex = this.keep(humidorTex());
      const front = new THREE.Mesh(this.keep(new THREE.PlaneGeometry(0.46, 1.7)), this.keep(new THREE.MeshStandardMaterial({
        map: tex, emissive: '#ffffff', emissiveMap: tex, emissiveIntensity: 0.55, roughness: 0.3, flatShading: true,
      })));
      front.rotation.y = Math.PI / 2;
      this.add(front, hx + 0.181, 1.0, hz);
      this.box(0.04, 0.06, 0.62, brass, hx + 0.18, 1.98, hz);
    }
  }

  // ── the doors ────────────────────────────────────────────────────────────

  private buildDoors(): void {
    const dl = CLUB_DOOR[0] - CLUB_DOOR[1] / 2, dr = CLUB_DOOR[0] + CLUB_DOOR[1] / 2;
    const half = CLUB_DOOR[1] / 2;
    const z = CLUB.z0;
    const brass = this.keep(std('#C79A45', { rough: 0.3, metal: 0.85 }));
    const leather = this.keep(std('#ffffff', { rough: 0.5, map: this.keep(tuftTex()) }));
    for (const [hx, sx] of [[dl, 1], [dr, -1]] as const) {
      const pivot = new THREE.Group();
      pivot.position.set(hx, 0, z);
      const leaf = new THREE.Mesh(this.keep(new THREE.BoxGeometry(half - 0.02, DOOR_H - 0.06, 0.07)), leather);
      leaf.position.set((sx * (half - 0.02)) / 2, (DOOR_H - 0.06) / 2, 0);
      leaf.castShadow = true;
      // a little diamond window, and a brass push plate, each side
      const win = new THREE.Mesh(this.keep(new THREE.PlaneGeometry(0.18, 0.18)), this.keep(new THREE.MeshBasicMaterial({ color: '#ff7ab8' })));
      win.rotation.z = Math.PI / 4;
      for (const side of [-1, 1]) {
        const w2 = win.clone();
        w2.position.set((sx * (half - 0.02)) / 2, 1.6, side * 0.037);
        if (side < 0) w2.rotation.y = Math.PI;
        pivot.add(w2);
        const plate = new THREE.Mesh(this.keep(new THREE.BoxGeometry(0.08, 0.3, 0.01)), brass);
        plate.position.set(sx * (half - 0.16), 1.1, side * 0.04);
        pivot.add(plate);
      }
      pivot.add(leaf);
      pivot.userData.side = sx === 1 ? -1 : 1;
      this.leaves.push(pivot);
      this.group.add(pivot);
      this.box(0.08, DOOR_H, WALL_T + 0.04, brass, hx - sx * 0.04, DOOR_H / 2, z, this.group);
    }
    this.box(dr - dl + 0.16, 0.08, WALL_T + 0.04, brass, CLUB_DOOR[0], DOOR_H + 0.04, z, this.group);
    // the name in neon over the door, out in the hall
    this.sign(this.keep(clubSign()), 2.6, 0.81, CLUB_DOOR[0], 3.05, z - 0.14, Math.PI, this.group);
  }

  // ── the people ───────────────────────────────────────────────────────────

  private buildPeople(kit: CrewKit): void {
    POLES.forEach((_, i) => {
      const d = new Dancer(kit, i);
      this.dancers.push(d);
      this.inside.add(d.group);
    });
    // the backstage crew, and a spot on each of their marks
    ['#8a8178', '#efe6da', '#5c524a'].forEach((fur, k) => {
      const d = new BackupDancer(kit, k, fur);
      this.crew.push(d);
      this.inside.add(d.group);
    });
    const shaftL = CLUB_CEIL - STAGE.h;
    const geo = this.keep(new THREE.CylinderGeometry(0.1, 0.7, shaftL, 18, 1, true));
    for (const x of CREW_X) {
      const mat = this.keep(beamMaterial('#ff2d95', shaftL, 0.16));
      const beam = new THREE.Mesh(geo, mat);
      beam.renderOrder = 3;
      beam.position.set(x, STAGE.h + shaftL / 2, CREW_Z);
      this.inside.add(beam);
      this.crewBeams.push({ beam, mat });
    }
    const suits = ['#2b2140', '#1d2b22', '#3a1f14', '#1f2432', '#3a1030', '#262626', '#14303a', '#3a2a10'];
    const furs = ['#8a8178', '#6f665e', '#a39a8f', '#5c524a', '#9b8b7a', '#7a7068', '#b0a89c', '#64594f'];
    const ember = (i: number) => kit.embers[i % kit.embers.length];
    const style = (i: number, extra: Partial<RoleStyle> = {}): RoleStyle => ({
      ...ROLE_STYLE.trader, suit: suits[i % suits.length], sleeve: suits[i % suits.length], hatColor: i % 2 ? '#0b0b0d' : '#3a2a1c',
      neck: i % 3 === 0 ? 'tie' : 'bowtie', neckColor: ['#F2C66D', '#ff6fb5', '#9fd8ff'][i % 3], band: '#F2C66D', eyes: i % 4 === 3 ? 'glasses' : 'none',
      ...extra,
    });
    // patrons on the rail round the stage, and in the lounge, cigars going
    const seats = [...stageChairs().filter((_, i) => i !== 3), ...clubChairs(), ...sofaSeats()].filter((_, i) => i % 5 !== 4);
    seats.forEach((s, i) => {
      const r = new ClubRat(kit, 'patron', style(i), furs[i % furs.length], s.x, s.z, s.yaw, `patron${i}`, { emberMat: ember(i) });
      this.rats.push(r);
      this.inside.add(r.group);
    });
    // two bartenders, the door, the decks
    const barStyle = (neck: string): RoleStyle => ({ ...ROLE_STYLE.smith, suit: '#f4f1ea', sleeve: '#f4f1ea', neck: 'bowtie', neckColor: neck, hatColor: '#1c1917', band: neck });
    const [b0, b1] = BARTENDERS;
    const dot = new ClubRat(kit, 'bartender', barStyle('#b3123a'), '#c9b8a6', b0[0], b0[1], -Math.PI / 2, 'Dot', { emberMat: ember(0), persona: CLUB_PERSONAS[1] });
    const moe = new ClubRat(kit, 'bartender', barStyle('#22d3ee'), '#6f665e', b1[0], b1[1], -Math.PI / 2, 'Moe', { emberMat: ember(1), persona: CLUB_PERSONAS[1] });
    const knuckles = new ClubRat(kit, 'bouncer', { ...ROLE_STYLE.risk }, '#5c524a', BOUNCER[0], BOUNCER[1], Math.PI / 2, 'Knuckles', { emberMat: ember(0), persona: CLUB_PERSONAS[0] });
    const gramps = new ClubRat(
      kit, 'dj', { ...ROLE_STYLE.trader, suit: '#1a1a1a', sleeve: '#1a1a1a', hat: 'none', eyes: 'shades', neck: 'bowtie', neckColor: '#ff2d95' }, '#b0a89c',
      DJ_BOOTH[0], DJ_BOOTH[1], DJ_YAW, 'Gramps', {
        emberMat: ember(2), y: DJ_RISER.h, persona: CLUB_PERSONAS[2],
        extra: (p) => {
          // headphones
          const band = new THREE.Mesh(new THREE.TorusGeometry(0.28, 0.025, 4, 12, Math.PI), kit.mat('#141418'));
          band.position.y = 0.05;
          const cups = [-1, 1].map((sx) => {
            const c = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.08, 0.06, 8), kit.mat('#ff2d95'));
            c.rotation.z = Math.PI / 2;
            c.position.set(sx * 0.29, 0.03, 0);
            return c;
          });
          p.head.add(band, ...cups);
        },
      },
    );
    this.rats.push(dot, moe, knuckles, gramps);
    this.inside.add(dot.group, moe.group, knuckles.group, gramps.group);
  }

  // ── each frame ───────────────────────────────────────────────────────────

  /** 0 = shut, 1 = open: the padded doors swing in as you come. */
  setDoor(open: number): void {
    const o = Math.max(0, Math.min(1, open));
    for (const l of this.leaves) l.rotation.y = (l.userData.side as number) * o * 1.55;
  }

  /** The lit ends of the cigars and cigarettes, for the engine's smoke. */
  embers(): { ember: THREE.Mesh; x: number; z: number; yaw: number; y: number }[] {
    return this.rats.filter((r) => r.ember).map((r) => ({ ember: r.ember!, x: r.x, z: r.z, yaw: r.group.rotation.y, y: r.seated ? 1.15 : 1.08 }));
  }

  /** The bartender across the counter from where you stand. */
  bartenderAt(z: number): ClubRat | null {
    let best: ClubRat | null = null, bd = Infinity;
    for (const r of this.rats) {
      if (r.role !== 'bartender') continue;
      const d = Math.abs(r.z - z);
      if (d < bd) { bd = d; best = r; }
    }
    return best;
  }

  /** A gun went off in here: the dancers stop, everybody's paws go up. */
  spook(secs: number): void {
    for (const d of this.dancers) d.spook(secs);
    for (const d of this.crew) d.spook(secs);
    for (const r of this.rats) r.spook(secs + Math.random() * 0.8);
  }

  /** The backstage crew, on their marks (crewMark, backstage.ts). True
   *  when they've just finished a run of the routine (the room claps). */
  private crewDance(dt: number, t: number): boolean {
    const { index } = routineAt(t);
    const done = this.lastMove >= 0 && index !== this.lastMove && routineAt(t - 0.6).move === 'dab';
    this.lastMove = index;
    this.crew.forEach((d, k) => {
      const m = crewMark(k, t);
      d.update(dt, t, { x: m.x, y: STAGE.h, z: m.z }, m.yaw);
    });
    // the spots change colour on the beat
    const beatN = Math.floor((t * 120) / 60);
    this.crewBeams.forEach((b, i) => {
      b.mat.uniforms.uColor.value.set(PALETTE[(beatN + i) % PALETTE.length]);
      b.mat.uniforms.uOpacity.value = 0.12 + 0.08 * Math.max(0, Math.sin(Math.PI * ((t * 2) % 1)));
    });
    return done;
  }

  barkLines(r: ClubRat): readonly string[] {
    return r.persona?.barks ?? (r.role === 'clubber' ? CROWD_BARKS : PATRON_BARKS);
  }

  /** Animate. Returns true when a dancer has just finished a spin or a hook
   *  (the room applauds: the engine plays it and has someone shout). */
  update(dt: number, t: number): boolean {
    let applause = false;
    this.dancers.forEach((d, i) => {
      const done = d.update(dt, t, i);
      if (done === 'spin' || done === 'hook') applause = true;
    });
    // the crew finishing a run of the routine gets a hand too
    if (this.crewDance(dt, t)) applause = true;
    if (applause) for (const r of this.rats) if (r.seated && Math.random() < 0.75) r.applaud(1.4 + Math.random() * 0.8);
    for (const r of this.rats) {
      // the patrons watch whichever dancer is nearest
      let watch: { x: number; z: number } | null = null;
      if (r.seated) {
        let best = Infinity;
        for (const d of this.dancers) {
          const p = d.group.position;
          const dd = Math.hypot(p.x - r.x, p.z - r.z);
          if (dd < best) { best = dd; watch = { x: p.x, z: p.z }; }
        }
      }
      r.update(dt, t, watch);
    }
    this.lights(dt, t);
    return applause;
  }

  private lights(dt: number, t: number): void {
    // the stage lights: each works one of the main stage's dancers, drifting
    // round her; now and then they all swing to the runway together
    const together = Math.max(0, Math.sin(t * 0.17)) ** 4;
    this.heads.forEach((h, i) => {
      const d = this.dancers[i % 2]?.group.position;
      const own = this._v.set((d?.x ?? STAGE.x) + Math.sin(t * 0.7 + i) * 0.5, STAGE.h, (d?.z ?? STAGE.z) + Math.cos(t * 0.6 + i * 1.7) * 0.5);
      const rx = (RUNWAY.x0 + RUNWAY.x1) / 2, rz = RUNWAY.z0 + ((Math.sin(t * 0.5) + 1) / 2) * (RUNWAY.z1 - RUNWAY.z0);
      own.x += (rx - own.x) * together;
      own.z += (rz - own.z) * together;
      const dir = own.sub(h.at).normalize();
      h.beam.quaternion.setFromUnitVectors(this._down, dir);
      h.beam.position.copy(h.at).addScaledVector(dir, BEAM_L / 2);
      h.housing.lookAt(h.at.x + dir.x, h.at.y + dir.y, h.at.z + dir.z);
      // warm colours, stepping slowly
      const k = Math.floor(t / 6 + i * 0.5);
      h.color.lerp(this._c.set(PALETTE[k % PALETTE.length]), Math.min(1, dt * 2));
      h.mat.uniforms.uColor.value.copy(h.color);
    });
    for (const p of this.platters) p.rotation.y += dt * 3.5;
    const on = Math.floor(t * 3) % 2;
    this.chase.forEach((m, i) => { m.emissiveIntensity = i === on ? 2.4 : 0.5; });
    this.pulse.forEach((m, i) => { m.emissiveIntensity = 0.9 + 0.6 * Math.max(0, Math.sin(t * 1.6 + i * Math.PI)); });
  }

  dispose(): void {
    for (const d of this.dancers) d.dispose();
    for (const o of this.owned) o.dispose();
    this.owned = [];
  }
}
