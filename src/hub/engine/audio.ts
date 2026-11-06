import lobbyTime from '../../assets/audio/lobby-time.ogg';
import bossaAntigua from '../../assets/audio/bossa-antigua.ogg';
import backbayLounge from '../../assets/audio/backbay-lounge.ogg';
import smoothLovin from '../../assets/audio/smooth-lovin.ogg';
import bassWalker from '../../assets/audio/bass-walker.ogg';

// The Casino's sound: jazz in the hall, slower jazz in the club, and the
// sound effects, all through one Web Audio graph.
//
//   music:  two players, one per room (the hall, The Velvet Tail), each
//           with its own gain and a low-pass. Where you stand mixes them: in
//           the hall the club is a muffled thump behind its doors, which opens
//           up as they swing; in the club the hall goes muffled instead.
//           Tracks by Kevin MacLeod, CC BY 4.0, credited on screen as each
//           one starts (src/assets/audio/CREDITS.md).
//   effects: synthesised, nothing recorded. A revolver, a lighter, a drag
//           and an exhale, a swig, a cough, a bottle, cards, chips, the
//           ball in the wheel, the reels, the doors, applause, and a squeak
//           of "voice" under a speech bubble. Sounds out on the floor are
//           placed in stereo and fall off with distance; everything goes
//           through a short room reverb and a compressor.
//
// Muting is remembered in this browser profile. Nothing here touches the
// network: the tracks are bundled with the app.

export interface Track { url: string; title: string }
export const HALL_TRACKS: Track[] = [
  { url: lobbyTime, title: 'Lobby Time' },
  { url: bossaAntigua, title: 'Bossa Antigua' },
  { url: backbayLounge, title: 'Backbay Lounge' },
];
export const CLUB_TRACKS: Track[] = [
  { url: smoothLovin, title: 'Smooth Lovin' },
  { url: bassWalker, title: 'Bass Walker' },
];

/** The credit CC BY asks for, as shown on screen. */
export function credit(t: Track): string {
  return `“${t.title}” Kevin MacLeod (incompetech.com) · CC BY 4.0`;
}

export interface Mix {
  /** Each room's music level, 0–1, and its low-pass cut-off in Hz. */
  hall: number;
  club: number;
  hallCut: number;
  clubCut: number;
}

const OPEN = 18000;

/** What you hear from where you are. Pure, so it can be tested. */
export function musicMix(v: { walking: boolean; inClub: boolean; doorDist: number; doorOpen: number; black?: number }): Mix {
  const out = 1 - 0.8 * (v.black ?? 0);
  if (!v.walking) return { hall: 0.2, club: 0, hallCut: OPEN, clubCut: OPEN };
  const o = Math.max(0, Math.min(1, v.doorOpen));
  if (v.inClub) {
    return {
      hall: (0.05 + 0.1 * o) * out, hallCut: 450 + 2200 * o,
      club: 0.5 * out, clubCut: v.black ? 600 : OPEN,
    };
  }
  const near = Math.max(0, 1 - v.doorDist / 9);
  return {
    hall: 0.32 * out, hallCut: v.black ? 600 : OPEN,
    club: 0.24 * near * (0.3 + 0.7 * o) * out, clubCut: 420 + 2600 * o,
  };
}

const KEY = 'krypt.hub.sound';

function readMuted(): boolean {
  try { return localStorage.getItem(KEY) === 'off'; } catch { return false; }
}

interface Room {
  el: HTMLAudioElement;
  gain: GainNode;
  lp: BiquadFilterNode;
  tracks: Track[];
  i: number;
  want: number;
  /** When it last wanted silence (seconds): paused after a fade. */
  quietSince: number;
  announced: boolean;
  /** Seconds left with the music cut (a gunshot stops the record). */
  duck: number;
}

type At = { x: number; z: number } | undefined;

export class HubAudio {
  muted = readMuted();
  /** A track started where you can hear it: show its credit. */
  onTrack: ((t: Track) => void) | null = null;
  private ctx: AudioContext | null = null;
  private master!: GainNode;
  private sfx!: GainNode;
  private verbIn!: GainNode;
  private noise!: AudioBuffer;
  private rooms: { hall: Room; club: Room } | null = null;
  private running = false;
  private listener = { x: 0, z: 0, yaw: 0 };
  private dragging: { stop(): void } | null = null;
  private clock = 0;

  /** Build the graph on first use (an AudioContext costs a thread). */
  private ensure(): AudioContext | null {
    if (this.ctx) return this.ctx;
    try {
      const ctx = new AudioContext();
      this.ctx = ctx;
      const comp = ctx.createDynamicsCompressor();
      comp.threshold.value = -14;
      comp.ratio.value = 4;
      this.master = ctx.createGain();
      this.master.gain.value = 0.85;
      this.master.connect(comp);
      comp.connect(ctx.destination);
      this.sfx = ctx.createGain();
      this.sfx.gain.value = 0.9;
      this.sfx.connect(this.master);
      // a short, warm room
      const verb = ctx.createConvolver();
      const len = Math.floor(ctx.sampleRate * 1.5);
      const ir = ctx.createBuffer(2, len, ctx.sampleRate);
      for (let c = 0; c < 2; c++) {
        const d = ir.getChannelData(c);
        for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / len) ** 3.2;
      }
      verb.buffer = ir;
      this.verbIn = ctx.createGain();
      this.verbIn.gain.value = 0.5;
      const verbLp = ctx.createBiquadFilter();
      verbLp.type = 'lowpass';
      verbLp.frequency.value = 3200;
      this.verbIn.connect(verb);
      verb.connect(verbLp);
      verbLp.connect(this.master);
      this.noise = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate);
      const nd = this.noise.getChannelData(0);
      for (let i = 0; i < nd.length; i++) nd[i] = Math.random() * 2 - 1;
      this.rooms = { hall: this.room(HALL_TRACKS, 0), club: this.room(CLUB_TRACKS, 6) };
      return ctx;
    } catch {
      this.ctx = null;
      return null;
    }
  }

  /** A room's player: its music through a low-pass (a wall) and, for the
   *  club, a bass shelf (the sound system). */
  private room(tracks: Track[], bassDb: number): Room {
    const ctx = this.ctx!;
    const el = new Audio();
    el.preload = 'auto';
    const r: Room = {
      el, gain: ctx.createGain(), lp: ctx.createBiquadFilter(), tracks,
      i: Math.floor(Math.random() * tracks.length), want: 0, quietSince: 0, announced: false, duck: 0,
    };
    el.src = tracks[r.i].url;
    r.gain.gain.value = 0;
    r.lp.type = 'lowpass';
    r.lp.frequency.value = OPEN;
    r.lp.Q.value = 0.5;
    const src = ctx.createMediaElementSource(el);
    if (bassDb) {
      const bass = ctx.createBiquadFilter();
      bass.type = 'lowshelf';
      bass.frequency.value = 140;
      bass.gain.value = bassDb;
      src.connect(bass).connect(r.lp);
    } else {
      src.connect(r.lp);
    }
    r.lp.connect(r.gain);
    r.gain.connect(this.master);
    el.addEventListener('ended', () => {
      r.i = (r.i + 1) % r.tracks.length;
      el.src = r.tracks[r.i].url;
      r.announced = false;
      if (this.running && !this.muted && r.want > 0) void el.play().catch(() => {});
    });
    return r;
  }

  start(): void {
    this.running = true;
    if (this.muted) return;
    const ctx = this.ensure();
    if (!ctx) return;
    void ctx.resume().catch(() => {});
  }

  /** The scene is hidden: everything stops where it is. */
  stop(): void {
    this.running = false;
    this.stopDrag();
    if (this.rooms) for (const r of Object.values(this.rooms)) r.el.pause();
    void this.ctx?.suspend().catch(() => {});
  }

  setMuted(m: boolean): void {
    this.muted = m;
    try { localStorage.setItem(KEY, m ? 'off' : 'on'); } catch { /* not remembered */ }
    if (m) {
      const running = this.running;
      this.stop();
      this.running = running;
    } else if (this.running) {
      this.start();
    }
  }

  /** Where you stand and which way you face (walk mode), for the effects'
   *  stereo and distance. */
  setListener(x: number, z: number, yaw: number): void {
    this.listener = { x, z, yaw };
  }

  /** Called every frame with the room mix. */
  update(dt: number, mix: Mix): void {
    this.clock += dt;
    if (!this.running || this.muted || !this.rooms || !this.ctx) return;
    const now = this.ctx.currentTime;
    for (const [name, r] of [['hall', this.rooms.hall], ['club', this.rooms.club]] as const) {
      r.duck = Math.max(0, r.duck - dt);
      const level = (name === 'hall' ? mix.hall : mix.club) * (r.duck > 0 ? 0 : 1);
      const cut = name === 'hall' ? mix.hallCut : mix.clubCut;
      if (Math.abs(level - r.want) > 1e-3) r.gain.gain.setTargetAtTime(level, now, r.duck > 0 ? 0.03 : 0.35);
      r.lp.frequency.setTargetAtTime(cut, now, 0.25);
      r.want = level;
      if (level > 0.001) {
        r.quietSince = 0;
        if (r.el.paused) void r.el.play().catch(() => {});
        if (!r.announced && level > 0.12 && !r.el.paused) {
          r.announced = true;
          this.onTrack?.(r.tracks[r.i]);
        }
      } else if (!r.el.paused && r.duck <= 0) {
        if (!r.quietSince) r.quietSince = this.clock;
        else if (this.clock - r.quietSince > 2) r.el.pause();
      }
    }
  }

  dispose(): void {
    this.stop();
    if (this.rooms) for (const r of Object.values(this.rooms)) { r.el.removeAttribute('src'); r.el.load(); }
    void this.ctx?.close().catch(() => {});
    this.ctx = null;
    this.rooms = null;
  }

  // ── building blocks ──────────────────────────────────────────────────────

  /** A live context to play into, or null (muted, hidden, unavailable). */
  private live(): AudioContext | null {
    if (!this.running || this.muted) return null;
    const ctx = this.ensure();
    return ctx && ctx.state !== 'closed' ? ctx : null;
  }

  /** A gain stage into the effects bus: placed (stereo, distance) when it
   *  comes from somewhere on the floor, with a send to the room. */
  private out(gain: number, at: At, send = 0.15): GainNode | null {
    const ctx = this.ctx!;
    const g = ctx.createGain();
    let level = gain;
    let node: AudioNode = g;
    if (at) {
      const dx = at.x - this.listener.x, dz = at.z - this.listener.z;
      const d = Math.hypot(dx, dz);
      if (d > 16) return null;
      const y = this.listener.yaw;
      // yaw 0 looks down -z; "right" is +x
      const right = d > 1e-3 ? (dx * Math.cos(y) - dz * Math.sin(y)) / d : 0;
      level *= Math.min(1, 1.8 / (1 + d * 0.8));
      const p = ctx.createStereoPanner();
      p.pan.value = Math.max(-0.85, Math.min(0.85, right));
      g.connect(p);
      node = p;
    }
    g.gain.value = level;
    node.connect(this.sfx);
    if (send > 0) {
      const s = ctx.createGain();
      s.gain.value = send;
      node.connect(s);
      s.connect(this.verbIn);
    }
    return g;
  }

  private noiseSrc(t: number, dur: number): AudioBufferSourceNode {
    const s = this.ctx!.createBufferSource();
    s.buffer = this.noise;
    s.loop = true;
    s.start(t, Math.random() * 1.5);
    s.stop(t + dur);
    return s;
  }

  private filter(type: BiquadFilterType, f: number, q = 0.7): BiquadFilterNode {
    const b = this.ctx!.createBiquadFilter();
    b.type = type;
    b.frequency.value = f;
    b.Q.value = q;
    return b;
  }

  /** An attack-decay envelope on a fresh gain node. */
  private env(t: number, attack: number, peak: number, decay: number): GainNode {
    const g = this.ctx!.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(peak, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
    return g;
  }

  /** A burst of filtered noise. */
  private burst(t: number, o: { type: BiquadFilterType; f: number; q?: number; peak: number; attack?: number; decay: number; to: AudioNode; sweep?: number }): void {
    const n = this.noiseSrc(t, (o.attack ?? 0.002) + o.decay + 0.05);
    const f = this.filter(o.type, o.f, o.q);
    if (o.sweep) f.frequency.exponentialRampToValueAtTime(o.sweep, t + (o.attack ?? 0.002) + o.decay);
    const e = this.env(t, o.attack ?? 0.002, o.peak, o.decay);
    n.connect(f).connect(e).connect(o.to);
  }

  /** A pitched blip (sine, triangle, square…), optionally gliding. */
  private tone(t: number, o: { type: OscillatorType; f: number; to?: number; peak: number; attack?: number; decay: number; dest: AudioNode; lp?: number }): void {
    const ctx = this.ctx!;
    const osc = ctx.createOscillator();
    osc.type = o.type;
    osc.frequency.setValueAtTime(o.f, t);
    if (o.to) osc.frequency.exponentialRampToValueAtTime(o.to, t + (o.attack ?? 0.002) + o.decay);
    const e = this.env(t, o.attack ?? 0.002, o.peak, o.decay);
    if (o.lp) osc.connect(this.filter('lowpass', o.lp)).connect(e);
    else osc.connect(e);
    e.connect(o.dest);
    osc.start(t);
    osc.stop(t + (o.attack ?? 0.002) + o.decay + 0.05);
  }

  // ── the revolver ─────────────────────────────────────────────────────────

  gunshot(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(1, undefined, 0.75);
    if (!o) return;
    this.burst(t, { type: 'highpass', f: 1400, peak: 0.9, decay: 0.08, to: o });
    this.burst(t, { type: 'lowpass', f: 3200, sweep: 200, peak: 1, attack: 0.003, decay: 0.42, to: o });
    this.tone(t, { type: 'sine', f: 150, to: 38, peak: 1, decay: 0.22, dest: o });
  }

  dryFire(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.5, undefined, 0.05);
    if (!o) return;
    for (const dt of [0, 0.028]) this.burst(t + dt, { type: 'bandpass', f: 3200, q: 2, peak: 0.6, decay: 0.012, to: o });
  }

  /** The cylinder swings out, six rounds go in with a click each, it snaps
   *  shut: timed to the reload in hotbar.ts. */
  reload(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.45, undefined, 0.05);
    if (!o) return;
    this.burst(t + 0.05, { type: 'bandpass', f: 1800, q: 1.5, peak: 0.6, decay: 0.05, to: o });
    for (let i = 0; i < 6; i++) this.burst(t + 0.3 + i * 0.12, { type: 'bandpass', f: 4200, q: 3, peak: 0.35, decay: 0.02, to: o });
    this.burst(t + 1.4, { type: 'bandpass', f: 2200, q: 1.2, peak: 0.8, decay: 0.06, to: o });
  }

  // ── smoking and drinking ─────────────────────────────────────────────────

  lighter(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.5, undefined, 0.05);
    if (!o) return;
    this.burst(t, { type: 'bandpass', f: 4500, q: 2.5, peak: 0.5, decay: 0.05, to: o });
    this.burst(t + 0.06, { type: 'lowpass', f: 900, peak: 0.35, attack: 0.12, decay: 0.45, to: o });
  }

  /** A drag: the breath in and the tobacco crackling, until stopDrag(). */
  startDrag(): void {
    const ctx = this.live();
    if (!ctx || this.dragging) return;
    const t = ctx.currentTime;
    const o = this.out(1, undefined, 0.02);
    if (!o) return;
    const n = ctx.createBufferSource();
    n.buffer = this.noise;
    n.loop = true;
    const bp = this.filter('bandpass', 1500, 0.8);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(0.09, t + 0.5);
    n.connect(bp).connect(g).connect(o);
    n.start(t);
    for (let k = 0; k < 26; k++) {
      this.burst(t + 0.15 + k * (0.07 + Math.random() * 0.06), { type: 'highpass', f: 5000, peak: 0.08 + Math.random() * 0.1, decay: 0.008, to: o });
    }
    this.dragging = {
      stop: () => {
        const now = ctx.currentTime;
        g.gain.cancelScheduledValues(now);
        g.gain.setTargetAtTime(0.0001, now, 0.05);
        n.stop(now + 0.3);
        o.gain.setTargetAtTime(0, now, 0.05);
      },
    };
  }

  stopDrag(): void {
    this.dragging?.stop();
    this.dragging = null;
  }

  exhale(k: number): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.6, undefined, 0.1);
    if (!o) return;
    this.burst(t + 0.05, { type: 'lowpass', f: 1500, sweep: 320, peak: 0.18 + 0.2 * k, attack: 0.15, decay: 0.7 + k * 0.9, to: o });
  }

  gulp(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.5, undefined, 0.03);
    if (!o) return;
    this.tone(t, { type: 'triangle', f: 270 + Math.random() * 40, to: 140, peak: 0.5, decay: 0.12, dest: o, lp: 900 });
    this.burst(t + 0.02, { type: 'bandpass', f: 700, q: 1.5, peak: 0.12, decay: 0.08, to: o });
  }

  cough(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.6, undefined, 0.15);
    if (!o) return;
    for (const [dt, p] of [[0, 0.5], [0.26, 0.42], [0.5, 0.3]] as const) {
      this.burst(t + dt, { type: 'bandpass', f: 520, q: 1.4, peak: p, attack: 0.01, decay: 0.16, to: o });
      this.tone(t + dt, { type: 'sawtooth', f: 160, to: 110, peak: p * 0.15, decay: 0.12, dest: o, lp: 600 });
    }
  }

  /** A fresh bottle set down: glass on wood. */
  clink(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.35, undefined, 0.2);
    if (!o) return;
    this.tone(t, { type: 'sine', f: 2600, peak: 0.4, decay: 0.35, dest: o });
    this.tone(t + 0.004, { type: 'sine', f: 3920, peak: 0.25, decay: 0.25, dest: o });
    this.burst(t, { type: 'lowpass', f: 400, peak: 0.3, decay: 0.05, to: o });
  }

  /** The bartender's shaker: ice rattling in a tin, in time, then a pour. */
  shaker(secs: number, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.5, at, 0.12);
    if (!o) return;
    for (let tt = 0; tt < secs - 0.5; tt += 0.11) {
      this.burst(t + tt, { type: 'bandpass', f: 3800 + Math.random() * 1500, q: 2, peak: 0.25 + Math.random() * 0.15, decay: 0.04, to: o });
      this.burst(t + tt + 0.03, { type: 'highpass', f: 5000, peak: 0.12, decay: 0.02, to: o });
    }
    this.burst(t + secs - 0.4, { type: 'bandpass', f: 900, q: 0.8, peak: 0.25, attack: 0.05, decay: 0.45, to: o });
  }

  /** Feet back on the floor after a jump. */
  land(): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.5, undefined, 0.1);
    if (!o) return;
    this.tone(t, { type: 'sine', f: 110, to: 55, peak: 0.6, decay: 0.12, dest: o });
    this.burst(t, { type: 'lowpass', f: 500, peak: 0.3, decay: 0.08, to: o });
  }

  // ── the tables ───────────────────────────────────────────────────────────

  card(at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const o = this.out(0.5, at, 0.05);
    if (!o) return;
    this.burst(ctx.currentTime, { type: 'bandpass', f: 3600, q: 0.8, peak: 0.5, attack: 0.004, decay: 0.03, to: o });
  }

  chips(n = 5, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.4, at, 0.1);
    if (!o) return;
    for (let i = 0; i < n; i++) {
      const tt = t + i * 0.05 + Math.random() * 0.02;
      this.tone(tt, { type: 'sine', f: 3000 + Math.random() * 1600, peak: 0.25, decay: 0.05, dest: o });
      this.burst(tt, { type: 'highpass', f: 2500, peak: 0.15, decay: 0.015, to: o });
    }
  }

  /** The ball: a rolling rumble, then rattling down the frets to a stop. */
  roulette(dur: number, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.6, at, 0.15);
    if (!o) return;
    const n = this.noiseSrc(t, dur);
    const lp = this.filter('bandpass', 900, 1.2);
    lp.frequency.exponentialRampToValueAtTime(260, t + dur * 0.7);
    const g = ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(0.18, t + 0.2);
    g.gain.setValueAtTime(0.18, t + dur * 0.55);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur * 0.8);
    n.connect(lp).connect(g).connect(o);
    let tt = t + dur * 0.55, gap = 0.06;
    while (tt < t + dur - 0.05) {
      this.burst(tt, { type: 'bandpass', f: 2600 + Math.random() * 1200, q: 3, peak: 0.35, decay: 0.02, to: o });
      tt += gap;
      gap *= 1.18;
    }
    this.burst(t + dur, { type: 'bandpass', f: 1800, q: 2, peak: 0.7, decay: 0.05, to: o });
  }

  /** The reels: a ratchet while they spin, a thunk as each one stops. */
  slots(dur: number, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.45, at, 0.08);
    if (!o) return;
    for (let tt = t; tt < t + dur; tt += 1 / 20) this.burst(tt, { type: 'bandpass', f: 2200, q: 4, peak: 0.12, decay: 0.012, to: o });
    for (const k of [0.6, 0.8, 1]) this.tone(t + dur * k, { type: 'sine', f: 140, to: 70, peak: 0.6, decay: 0.12, dest: o });
  }

  /** A win: the bells. */
  bells(big = false, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.35, at, 0.3);
    if (!o) return;
    const notes = [1047, 1319, 1568, 2093];
    const rounds = big ? 3 : 1;
    for (let r = 0; r < rounds; r++) {
      notes.forEach((f, i) => {
        const tt = t + r * 0.45 + i * 0.09;
        this.tone(tt, { type: 'sine', f, peak: 0.4, decay: 0.4, dest: o });
        this.tone(tt, { type: 'sine', f: f * 2.01, peak: 0.12, decay: 0.25, dest: o });
      });
    }
  }

  /** A button on a table's panel. */
  click(): void {
    const ctx = this.live();
    if (!ctx) return;
    const o = this.out(0.25, undefined, 0);
    if (!o) return;
    this.tone(ctx.currentTime, { type: 'square', f: 1800, peak: 0.25, decay: 0.02, dest: o, lp: 3500 });
  }

  // ── the building and the people ──────────────────────────────────────────

  /** The club's padded doors swinging (or the hall's sliding ones). */
  door(at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.6, at, 0.2);
    if (!o) return;
    this.burst(t, { type: 'lowpass', f: 700, peak: 0.35, attack: 0.1, decay: 0.35, to: o });
    this.tone(t + 0.05, { type: 'sawtooth', f: 85, to: 62, peak: 0.08, attack: 0.05, decay: 0.3, dest: o, lp: 380 });
  }

  /** Someone said something: a few squeaks of "voice", the speaker's own
   *  pitch, as many as the line is long (a rat's Animalese). */
  voice(seed: string, text: string, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.2, at, 0.08);
    if (!o) return;
    let h = 0;
    for (const c of seed) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    const base = 520 + (h % 420);
    const n = Math.max(3, Math.min(10, Math.round(text.length / 5)));
    for (let i = 0; i < n; i++) {
      const f = base * (0.88 + ((h >> (i % 16)) & 7) * 0.04 + Math.random() * 0.06);
      this.tone(t + i * 0.075, { type: 'square', f, to: f * 1.08, peak: 0.22, attack: 0.006, decay: 0.05, dest: o, lp: 2400 });
    }
  }

  /** The room applauds. */
  applause(secs: number, at?: At): void {
    const ctx = this.live();
    if (!ctx) return;
    const t = ctx.currentTime;
    const o = this.out(0.5, at, 0.25);
    if (!o) return;
    const n = Math.round(secs * 26);
    for (let i = 0; i < n; i++) {
      const tt = t + Math.random() * secs * (0.6 + 0.4 * (i / n));
      this.burst(tt, { type: 'bandpass', f: 900 + Math.random() * 1800, q: 1.2, peak: 0.08 + Math.random() * 0.12, decay: 0.03, to: o });
    }
  }

  /** The record's needle dragged across it: the club's music stops dead,
   *  and comes back a couple of seconds later. */
  recordScratch(): void {
    const ctx = this.live();
    if (!ctx || !this.rooms) return;
    const t = ctx.currentTime;
    this.rooms.club.duck = 2.6;
    const o = this.out(0.5, undefined, 0.1);
    if (!o) return;
    this.tone(t, { type: 'sawtooth', f: 420, to: 1500, peak: 0.25, decay: 0.12, dest: o, lp: 2600 });
    this.tone(t + 0.12, { type: 'sawtooth', f: 1500, to: 160, peak: 0.25, decay: 0.25, dest: o, lp: 2600 });
    this.burst(t, { type: 'bandpass', f: 1800, q: 1, peak: 0.2, decay: 0.35, to: o });
  }
}
