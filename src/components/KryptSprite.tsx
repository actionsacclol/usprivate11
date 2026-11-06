import { useEffect, useRef } from 'react';
import type { CSSProperties } from 'react';
import spriteUrl from '../assets/spritebluey.png';

const FRAMES = 8;       // spritebluey.png is a 384×48 strip → 8 frames of 48×48
const DURATION = 900;   // ms per idle loop

interface KryptSpriteProps {
  /** Rendered frame size in px (sheet is 48×48 per frame). */
  size?: number;
  /** Adds the lounging-pet bob + blue ground glow. */
  pet?: boolean;
  className?: string;
  style?: CSSProperties;
  title?: string;
}

/**
 * Animated blue Krypt mascot. The frame stepping is driven by the Web
 * Animations API (not CSS keyframes) so it reliably cycles all 8 frames at
 * any size and can't be silently frozen by reduced-motion CSS.
 */
export function KryptSprite({ size = 48, pet = false, className, style, title }: KryptSpriteProps) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof el.animate !== 'function') return;
    const anim = el.animate(
      [
        { backgroundPositionX: '0px' },
        { backgroundPositionX: `${-FRAMES * size}px` },
      ],
      { duration: DURATION, iterations: Infinity, easing: `steps(${FRAMES})` },
    );
    return () => anim.cancel();
  }, [size]);

  return (
    <span
      ref={ref}
      role="img"
      aria-label={title ?? 'Krypt mascot'}
      title={title}
      className={`krypt-sprite${pet ? ' krypt-pet' : ''}${className ? ` ${className}` : ''}`}
      style={{
        width: size,
        height: size,
        backgroundImage: `url(${spriteUrl})`,
        backgroundRepeat: 'no-repeat',
        backgroundSize: `${FRAMES * size}px ${size}px`,
        ...style,
      }}
    />
  );
}
