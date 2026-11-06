import { useEffect, useMemo, useState } from 'react';
import { HubStage } from './HubStage';
import { readScoreboard, type HubScoreboard } from './data';
import { PokerEngine } from './poker/PokerEngine';
import { MIN_REAL_FORECASTERS, assignSeats, forecastersFrom, tableMode, type TableChoice } from './poker/seats';
import { GlassButton } from '../components/glass/GlassButton';
import { useApp } from '../state/AppStateProvider';
import { configuredAgents } from '../utils/agents';

// High Stakes: a private card room, one oval table under one warm lamp. With
// two or more real forecasters — the user's named agents, each recording its
// own fair values with record_forecast — the seats are theirs: each one's bet
// on the felt is its real fair value against the mid it saw, and the
// "WHO CALLED IT?" leaderboard is the forecast scoreboard. Under two, or on
// request, six trading emotions play every engine order and the dealer's
// showdown reveals the bot's real call.
function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new PokerEngine(canvas, w, h);
}

/** The scoreboard is one local SQLite read with pure scoring — no network
 *  (forecast_ledger.scoreboard; resolving outcomes is a separate job that
 *  this never triggers) — so a modest re-read while the scene is open keeps
 *  a newly settled market's score current. */
const SCOREBOARD_POLL_MS = 60_000;
/** Kept from the Council days, so a remembered choice survives the redesign. */
const CHOICE_KEY = 'krypt.council.seats';

function loadChoice(): TableChoice | null {
  try {
    const v = window.localStorage.getItem(CHOICE_KEY);
    return v === 'personas' || v === 'agents' ? v : null;
  } catch {
    return null;
  }
}

export default function PokerView() {
  const [board, setBoard] = useState<HubScoreboard | null | undefined>(undefined);
  const [choice, setChoice] = useState<TableChoice | null>(loadChoice);
  const { config } = useApp();

  useEffect(() => {
    let alive = true;
    const pull = async () => {
      try {
        const b = await readScoreboard();
        if (alive) setBoard(b ?? null);
        if (b === undefined) window.clearInterval(i); // no such call in this build
      } catch {
        if (alive) setBoard(null); // backend down: the personas sit
      }
    };
    void pull();
    const i = window.setInterval(pull, SCOREBOARD_POLL_MS);
    return () => { alive = false; window.clearInterval(i); };
  }, []);

  // counted the way the engine seats them: a named agent is its own seat
  const agents = useMemo(() => configuredAgents(config), [config]);
  const real = useMemo(() => assignSeats(forecastersFrom(board, agents)).length, [board, agents]);
  const mode = tableMode(choice, real);
  const pick = (c: TableChoice) => {
    setChoice(c);
    try { window.localStorage.setItem(CHOICE_KEY, c); } catch { /* a remembered toggle is a convenience */ }
  };
  const extra = useMemo(() => ({ scoreboard: board, councilChoice: choice }), [board, choice]);
  const agentsOff = real < MIN_REAL_FORECASTERS;

  const controls = (
    <div className="flex items-center gap-1" role="group" aria-label="Who plays at the table">
      <GlassButton
        live={false}
        variant={mode === 'personas' ? 'active' : 'default'}
        onClick={() => pick('personas')}
        title="Six trading emotions play the engines' orders"
        className="px-2.5 py-1.5 text-xs"
      >
        Personas
      </GlassButton>
      <GlassButton
        live={false}
        variant={mode === 'agents' ? 'active' : 'default'}
        onClick={() => pick('agents')}
        disabled={agentsOff}
        title={agentsOff
          ? `Needs ${MIN_REAL_FORECASTERS} real forecasters: named agents calling record_forecast (${real} so far)`
          : 'Your real forecasters: their fair values, bet against the market'}
        className="px-2.5 py-1.5 text-xs"
      >
        Your agents
      </GlassButton>
    </div>
  );

  return (
    <HubStage
      create={create}
      hint={mode === 'agents'
        ? 'your forecasters · drag to look around · click a seat to follow it'
        : 'drag to look around · scroll to zoom · click a player to follow them'}
      loading="Shuffling up and dealing…"
      tourLabel="Cinematic"
      tourTitle="Camera cuts to whoever is talking, then to the showdown"
      frameBg="#080605"
      extra={extra}
      controls={controls}
    />
  );
}
