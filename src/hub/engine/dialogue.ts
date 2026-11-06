import type { Persona, Topic } from './npcs';

// A conversation with one staff member, Skyrim style: they say something, you
// pick what to say back, they answer one beat at a time. Pure state — the
// engine draws it (talkui.ts) and feeds it keys — so it is tested on its own.

export const GOODBYE = 'Goodbye.';
const CHARS_PER_SEC = 52;

export interface TalkOption {
  text: string;
  /** Already asked: shown dimmer, still pickable. */
  asked: boolean;
}

export class Talk {
  readonly persona: Persona;
  /** 'say' = they're talking (E to go on); 'choose' = your turn. */
  mode: 'say' | 'choose' = 'say';
  /** The beat they're saying now. */
  line = '';
  /** How much of `line` is on screen (typewriter). */
  shown = 0;
  sel = 0;
  closed = false;
  private queue: string[] = [];
  private menu: Topic[];
  private asked = new Set<string>();
  private leaving = false;
  private byeLine: string;

  constructor(persona: Persona, pick: (n: number) => number = (n) => Math.floor(Math.random() * n)) {
    this.persona = persona;
    this.menu = persona.topics;
    this.queue = [persona.greet[pick(persona.greet.length)] ?? '...'];
    this.byeLine = persona.bye[pick(persona.bye.length)] ?? '...';
    this.nextBeat();
  }

  get options(): TalkOption[] {
    return [
      ...this.menu.map((t) => ({ text: t.ask, asked: this.asked.has(t.ask) })),
      { text: GOODBYE, asked: false },
    ];
  }

  get typing(): boolean {
    return this.mode === 'say' && this.shown < this.line.length;
  }

  update(dt: number): void {
    if (this.mode === 'say') this.shown = Math.min(this.line.length, this.shown + dt * CHARS_PER_SEC);
  }

  /** E / Enter / a click on the box: finish the line, then the next beat,
   *  then (out of beats) your turn — or, after goodbye, the end. */
  advance(): void {
    if (this.closed) return;
    if (this.mode === 'choose') { this.choose(this.sel); return; }
    if (this.typing) { this.shown = this.line.length; return; }
    if (this.queue.length) { this.nextBeat(); return; }
    if (this.leaving) { this.closed = true; return; }
    this.mode = 'choose';
    this.sel = Math.min(this.sel, this.options.length - 1);
  }

  move(d: number): void {
    if (this.mode !== 'choose') return;
    const n = this.options.length;
    this.sel = (this.sel + d + n) % n;
  }

  choose(i: number): void {
    if (this.closed || this.mode !== 'choose') return;
    const opts = this.options;
    if (i < 0 || i >= opts.length) return;
    this.sel = 0;
    if (i === opts.length - 1) {
      this.leaving = true;
      this.queue = [this.byeLine];
      this.nextBeat();
      return;
    }
    const t = this.menu[i];
    this.asked.add(t.ask);
    this.queue = [...t.say];
    // A follow-up opens its own menu; answering one with none goes home.
    this.menu = t.next?.length ? t.next : this.persona.topics;
    this.nextBeat();
  }

  private nextBeat(): void {
    this.mode = 'say';
    this.line = this.queue.shift() ?? '...';
    this.shown = 0;
  }
}
