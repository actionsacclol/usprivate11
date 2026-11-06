import { HubStage } from './HubStage';
import { CasinoEngine } from './engine/CasinoEngine';

// The Casino scene: an open casino hall at night, seen as a cutaway from
// above or walked in first person. The house staff stage what really happened
// in the app, table by table (engine/director.ts), and idle when nothing did.
// Your AI agents are the guests: a real tool call walks them to its table;
// between calls they play the games for play chips. On foot you can play
// too — blackjack, roulette, slots — and bet play chips at a sportsbook whose
// board is Polymarket's real sports markets.
function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new CasinoEngine(canvas, w, h);
}

export default function CasinoView() {
  return (
    <HubStage
      create={create}
      hint="drag to pan · scroll to zoom · click an area to follow it · Walk to play the tables"
      loading="Opening the casino…"
      tourLabel="Tour"
      tourTitle="Camera tours the busiest tables"
      frameBg="#07040a"
      wantsAutopilot
      wantsSportsbook
      walkKeys="Space jump · 1–6 hotbar · F or click to use"
    />
  );
}
