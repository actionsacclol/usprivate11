import {
  forwardRef, useCallback, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties, type HTMLAttributes, type ReactNode,
} from 'react';
import { Glass, type GlassOptics } from '@samasante/liquid-glass';
import { cls } from '../../utils/format';
import { GLASS_PRESETS, type GlassPreset } from './glassPresets';
import { useLiveGlass } from './glassQuality';

export interface GlassPanelProps extends Omit<HTMLAttributes<HTMLDivElement>, 'children'> {
  preset?: GlassPreset;
  /** false → the CSS frost (same material family, no SVG filter). Used to
   *  demote a surface that would blow the per-page live-glass budget. */
  live?: boolean;
  /** The translucent tint (a CSS background). */
  tint?: string;
  /** Overrides on top of the preset's optics. */
  optics?: Partial<GlassOptics>;
  /** Override the preset's rim displacement in px. */
  bendPx?: number;
  /** The library's material wrapper is `display: inline-block` INLINE, so a
   *  Tailwind `flex`/`block` in className silently loses to it. Layout display
   *  therefore has to come through here (it lands in the inline style). */
  display?: CSSProperties['display'];
  children?: ReactNode;
}

/**
 * The app's one glass primitive. Live mode is the library's material:
 * `backdrop-filter: blur() saturate() url(#displacement)`, i.e. it frosts AND
 * bends the real page behind it (Blink only, which Electron is). Frost mode is
 * a plain div with the `.glass-frost` CSS, for surfaces that repeat too often
 * to each carry an SVG filter.
 *
 * The ref resolves to the outer wrapper in both modes, so callers can drive a
 * transform on it (the sliding lenses, the press wobble) without a re-render.
 */
export const GlassPanel = forwardRef<HTMLDivElement, GlassPanelProps>(function GlassPanel({
  preset = 'card', live = true, tint, optics, bendPx, display = 'block',
  className, style, children, ...rest
}, ref) {
  const spec = GLASS_PRESETS[preset];
  // A hook, not a read-once flag: switching Settings → Appearance (or the
  // Auto watchdog demoting this machine) re-renders every surface at once.
  const liveOn = useLiveGlass();
  const useLive = live && liveOn;
  const [strength, setStrength] = useState(0.02);
  const probe = useRef<HTMLSpanElement>(null);
  const setRef = useCallback((el: HTMLDivElement | null) => {
    if (typeof ref === 'function') ref(el);
    else if (ref) ref.current = el;
  }, [ref]);

  // The material wrapper doesn't forward a ref, so a display:none probe
  // child finds it (probe.parentElement) — for the caller's ref and for the
  // size → strength conversion below.
  useLayoutEffect(() => {
    if (!useLive) return;
    const host = probe.current?.parentElement as HTMLDivElement | null;
    if (!host) return;
    setRef(host);
    const px = bendPx ?? spec.bendPx;
    const measure = (): void => {
      const w = host.offsetWidth;
      const h = host.offsetHeight;
      if (!w || !h) return;
      // Library: displacement px = strength × √((w² + h²) / 2). Quantised so
      // sub-pixel layout jitter doesn't re-version the filter every frame.
      const norm = Math.sqrt((w * w + h * h) / 2);
      const s = Math.round((px / norm) * 2000) / 2000;
      setStrength((prev) => (prev === s ? prev : s));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(host);
    return () => { ro.disconnect(); setRef(null); };
  }, [useLive, bendPx, spec.bendPx, setRef]);

  const merged = useMemo(
    () => ({ ...spec.optics, ...optics, strength }),
    [spec, optics, strength],
  );

  if (!useLive) {
    return (
      <div
        ref={setRef}
        className={cls('glass-frost', className)}
        style={{ display, ...(tint ? { background: tint } : null), ...style }}
        {...rest}
      >
        {children}
      </div>
    );
  }

  return (
    <Glass
      optics={merged}
      className={cls('glass-live', className)}
      style={{ display, ...(tint ? { background: tint } : null), ...style }}
      {...rest}
    >
      {children}
      <span ref={probe} aria-hidden data-lg-layer="" style={{ display: 'none' }} />
    </Glass>
  );
});
