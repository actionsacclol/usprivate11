import type { GlassOptics } from '@samasante/liquid-glass';

// Tuned optics for the app's glass surfaces, one bag per role.
//
// `strength` is deliberately absent: the library expresses refraction as a
// fraction of the box diagonal, so one number reads as a 4px rim on a button
// and a 50px smear on the 1380px title bar. Each preset instead names the
// displacement it wants in PIXELS (`bendPx`) and GlassPanel converts that to
// a strength from the measured box, so every surface bends by the same
// physical amount whatever its size.
//
// `frost` here is the backdrop blur in px (material mode). It must be 0 on
// any lens that sits ON TOP of text (the nav pill, the segmented lens) —
// a blurred backdrop there is the label under it going soft, which is the one
// thing a premium control can't do.

/**
 * Displacement-map raster size, for EVERY lens. Measured on this app
 * (Electron 30 / Chromium 124, RTX 3080): Blink re-decodes a backdrop
 * filter's <feImage> map every frame once it is large enough — one 256px map
 * costs ~4 ms/frame whatever the lens size (231 fps -> 117 with a single 36px
 * button), seven of them took the app to 21 fps, and the library default of
 * 512 costs ~25 ms each. Later, with a page's worth of lenses live at once,
 * even 192 sat in the 13–21 fps range while the maps re-decoded; 64 made the
 * cost vanish. The map is smooth and bilinearly stretched, so 64 reads the
 * same on a wide bar.
 */
export const GLASS_MAP_SIZE = 64;

// Dispersion (the RGB split) is the expensive part of a lens: it runs the
// displacement three times plus two composites. Measured on Dashboard with
// 12 lenses: 127 fps with dispersion everywhere, 212 with none. So the split
// is reserved for the hero lenses — the sliding selection pills and the Agent
// Hub's fullscreen bar — and every other preset bends without it.

export type GlassPreset = 'chrome' | 'button' | 'card' | 'pill' | 'modal';

export interface PresetSpec {
  /** Max displacement at the rim, in px. */
  bendPx: number;
  optics: Partial<GlassOptics>;
}

export const GLASS_PRESETS: Record<GlassPreset, PresetSpec> = {
  // Title bar, sidebar, top bar. Wide panels: no body dome (curvature 0),
  // which is what stops a stretched lens map "blooming an oval" across a
  // 1380px bar — only a thin rim meniscus bends.
  chrome: {
    bendPx: 9,
    optics: {
      mapSize: GLASS_MAP_SIZE,
      depth: 0.22,
      curvature: 0,
      bend: 0.6,
      bendWidth: 0.1,
      dispersion: 0,
      frost: 22,
      saturate: 1.6,
      sheen: 0.32,
      sheenWidth: 2,
      sheenFalloff: 1.6,
      sheenAngle: 60,
      glow: 0.06,
      glowSpread: 0.6,
      specular: 0.9,
      brightness: 0,
    },
  },
  // Hero buttons: a real little dome so the backdrop visibly magnifies.
  button: {
    bendPx: 6,
    optics: {
      mapSize: GLASS_MAP_SIZE,
      depth: 0.55,
      curvature: 0.3,
      bend: 0.55,
      bendWidth: 0.2,
      dispersion: 0,
      frost: 4,
      saturate: 1.5,
      sheen: 0.55,
      sheenWidth: 1.5,
      sheenFalloff: 1.4,
      sheenAngle: 50,
      glow: 0.16,
      glowSpread: 0.7,
      specular: 1.1,
      brightness: 0,
    },
  },
  // Stat tiles. Frosted enough that the numbers on top stay crisp against a
  // moving backdrop; the rim carries the "glass" read.
  card: {
    bendPx: 10,
    optics: {
      mapSize: GLASS_MAP_SIZE,
      depth: 0.28,
      curvature: 0.08,
      bend: 0.5,
      bendWidth: 0.12,
      dispersion: 0,
      frost: 18,
      saturate: 1.5,
      sheen: 0.38,
      sheenWidth: 2,
      sheenFalloff: 1.5,
      sheenAngle: 55,
      glow: 0.08,
      glowSpread: 0.8,
      specular: 1,
      brightness: 0,
    },
  },
  // The sliding selection lens (nav pill, segmented controls). Sits over the
  // label: zero frost, flat centre (low depth, no dome) so the text in the
  // middle reads crisp while the rim bends and splits colour.
  pill: {
    bendPx: 7,
    optics: {
      mapSize: GLASS_MAP_SIZE,
      depth: 0.3,
      curvature: 0,
      bend: 0.75,
      bendWidth: 0.2,
      dispersion: 0.7,
      frost: 0,
      saturate: 1.25,
      sheen: 0.7,
      sheenWidth: 1.5,
      sheenFalloff: 1.3,
      sheenAngle: 45,
      glow: 0.22,
      glowSpread: 0.6,
      glowFalloff: 0.8,
      specular: 1.15,
      brightness: 0,
    },
  },
  // Dialogs, toasts, the fullscreen bar. Frost is moderate, not heavy: the blur
  // runs BEFORE the displacement, so a thick frost leaves the rim nothing to
  // bend. Legibility comes from the darker modal tint instead, and the rim
  // visibly warps whatever the dialog is floating over.
  modal: {
    bendPx: 18,
    optics: {
      mapSize: GLASS_MAP_SIZE,
      depth: 0.2,
      curvature: 0.04,
      bend: 0.55,
      bendWidth: 0.08,
      dispersion: 0,
      frost: 12,
      saturate: 1.7,
      sheen: 0.42,
      sheenWidth: 2,
      sheenFalloff: 1.5,
      sheenAngle: 50,
      glow: 0.07,
      glowSpread: 0.6,
      specular: 1,
      brightness: 0,
    },
  },
};

/** Tints (the translucent colour IS the glass tint in material mode — an
 *  opaque one hides the refraction entirely). */
export const GLASS_TINTS = {
  chrome: 'linear-gradient(180deg, rgba(13,15,26,0.74), rgba(8,8,14,0.7))',
  panel: 'linear-gradient(180deg, rgba(16,18,32,0.8), rgba(10,10,17,0.84))',
  card: 'linear-gradient(160deg, rgba(20,23,40,0.8), rgba(12,12,20,0.86))',
  button: 'linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.03))',
  pill: 'linear-gradient(135deg, rgba(29,78,216,0.20), rgba(59,130,246,0.16) 50%, rgba(56,189,248,0.18))',
  modal: 'linear-gradient(170deg, rgba(22,25,42,0.8), rgba(12,12,20,0.86))',
} as const;

// Whether live (SVG-filter) glass is on now: Settings → Appearance (Auto /
// Live / Frost), the Auto watchdog, and the OS reduced-transparency setting.
// See glassQuality.ts. Re-exported here for existing callers.
export { liveGlassEnabled } from './glassQuality';
