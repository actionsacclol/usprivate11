/**
 * The living backdrop behind the whole app: slow aurora light, a faint grid
 * and film grain. Glass only reads as glass when there is something behind it
 * to bend — over flat near-black a refracting rim is invisible — so this layer
 * is what every glass surface in the app is actually showing off.
 *
 * Cost rules (the app has already had one 7-fps regression): the blobs are
 * pre-softened radial gradients (no CSS `filter: blur`, which would re-run per
 * frame) animated on `transform` only, so each is one GPU texture the
 * compositor slides around. Grid, grain and vignette are static. Nothing here
 * ever triggers layout or paint after the first frame.
 */
export function GlassBackdrop() {
  return (
    <div aria-hidden className="glass-backdrop">
      <div className="glass-aurora glass-aurora-a" />
      <div className="glass-aurora glass-aurora-b" />
      <div className="glass-aurora glass-aurora-c" />
      <div className="glass-aurora glass-aurora-d" />
      <div className="glass-grid" />
      <div className="glass-grain" />
      <div className="glass-vignette" />
    </div>
  );
}
