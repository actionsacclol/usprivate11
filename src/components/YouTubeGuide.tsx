import { ExternalLink, Play } from 'lucide-react';
import { cls } from '../utils/format';
import thumb from '../assets/youtube-guide.jpg';

// The owner's own walk-through on YouTube (2026-11-13). One press opens it in
// the user's browser. Same shape as Krypto Copy's.
//
// It is drawn LARGE, as the video itself: its own thumbnail, a play button in
// the middle, a glow round it. As a one-line row it reads as one more tip and
// gets skipped. The thumbnail is a file shipped with the app, so showing it
// asks nothing of YouTube, and the packaged CSP (default-src 'self') covers it.
//
// It is not a player inside the app, on purpose. A player in the app is always
// signed out of YouTube, and YouTube answers a signed-out play from a
// connection it distrusts with "Sign in to confirm you're not a bot" (seen in
// Krypto Copy; no player setting changed it). In the browser the user is
// usually signed in, the video plays, and it is an ordinary view. Nothing here
// contacts YouTube: the app only hands the address to the system browser.

/** The guide shown on the first onboarding screen. */
export const ONBOARDING_GUIDE_VIDEO_ID = 'r6k6yJFJ-Iw';

export const youTubeWatchUrl = (id: string): string => `https://www.youtube.com/watch?v=${id}`;

export function YouTubeGuide({ id = ONBOARDING_GUIDE_VIDEO_ID, className }: { id?: string; className?: string }) {
  return (
    <button
      type="button"
      onClick={() => void window.krypt.app.openExternal(youTubeWatchUrl(id))}
      className={cls(
        'group relative block aspect-video w-full overflow-hidden rounded-2xl border-2 border-sky-400/70 bg-black text-left',
        'shadow-[0_0_36px_-4px_rgba(59,130,246,0.65)] transition hover:border-sky-300 hover:shadow-[0_0_48px_-2px_rgba(59,130,246,0.85)]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/70',
        className,
      )}
      aria-label="Watch the guide. It opens on YouTube, in your browser."
      data-testid="youtube-guide-open"
    >
      <img src={thumb} alt="" draggable={false} className="absolute inset-0 h-full w-full object-cover transition duration-500 group-hover:scale-[1.03]" />
      {/* A little shade so the button and the words read on any frame. */}
      <span className="absolute inset-0 bg-gradient-to-t from-black/85 via-black/10 to-black/35" />

      <span className="absolute right-3 top-3 rounded-full bg-sky-500 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-white shadow-lg">Watch this first</span>

      {/* The play button: a still white disc, and a ring that breathes behind it. */}
      <span className="absolute left-1/2 top-1/2 flex h-20 w-20 -translate-x-1/2 -translate-y-1/2 items-center justify-center">
        <span className="absolute inset-0 animate-ping rounded-full bg-white/35 [animation-duration:2.2s] motion-reduce:hidden" />
        <span className="relative flex h-20 w-20 items-center justify-center rounded-full bg-white shadow-[0_8px_30px_rgba(0,0,0,0.55)] transition group-hover:scale-110">
          <Play className="ml-1 h-9 w-9 fill-blue-500 text-blue-500" />
        </span>
      </span>

      <span className="absolute inset-x-0 bottom-0 flex items-end justify-between gap-3 px-4 pb-3">
        <span className="min-w-0">
          <span className="block text-[17px] font-semibold leading-tight text-white">Watch the guide</span>
          <span className="mt-0.5 block text-xs leading-snug text-white/80">How to use the app, start to finish.</span>
        </span>
        <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full bg-black/60 px-2.5 py-1 text-[10px] font-semibold text-white/90">
          Opens on YouTube <ExternalLink className="h-3 w-3" />
        </span>
      </span>
    </button>
  );
}
