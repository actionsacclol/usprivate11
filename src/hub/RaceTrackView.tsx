import { HubStage } from './HubStage';
import { RaceTrackEngine } from './racetrack/RaceTrackEngine';

// The Race Track scene: a night meet under floodlights. Every signal walks up
// to the starting gate (traded ones load and break, skipped ones are
// scratched), every trade races in the lane its price puts it in, and every
// settlement is decided at the wire — a photo-finish flash and confetti for
// a win, a pull-up and the LOSSES paddock for a loss.
function create(canvas: HTMLCanvasElement, w: number, h: number) {
  return new RaceTrackEngine(canvas, w, h);
}

export default function RaceTrackView() {
  return (
    <HubStage
      create={create}
      hint="drag to orbit · scroll to zoom · click a racer to follow it"
      loading="Raising the floodlights…"
      tourLabel="Cinematic"
      tourTitle="Camera rides each runner to the gate, then cuts to the finish"
      frameBg="#04060d"
    />
  );
}
