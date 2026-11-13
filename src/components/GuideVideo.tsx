import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowRight, PlayCircle, X } from 'lucide-react';
import manifest from '../assets/guide/videos.json';

/**
 * The guide videos: real app screens, a voice and captions burned in, so they work with the sound off.
 * Bundled with the app, so they play offline and nothing is fetched: the packaged CSP's
 * default-src 'self' covers them. tools/guide-videos makes them and writes the manifest.
 */
export interface GuideVideo {
  id: string;
  title: string;
  blurb: string;
  seconds: number;
}

const files = import.meta.glob('../assets/guide/*.{mp4,jpg}', { eager: true, query: '?url', import: 'default' }) as Record<string, string>;
const url = (id: string, ext: 'mp4' | 'jpg'): string => files[`../assets/guide/${id}.${ext}`];

export const GUIDE_VIDEOS: GuideVideo[] = manifest;

export function videoLength(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

export function GuideVideoModal({ video, onClose, onPick }: {
  video: GuideVideo;
  onClose: () => void;
  /** Offers the next video under the player. */
  onPick?: (next: GuideVideo) => void;
}) {
  // Capture phase, and stopped there: the replayed onboarding modal closes on
  // Escape from a window listener, and Escape here should only close the video.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      onClose();
    };
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [onClose]);

  const at = GUIDE_VIDEOS.findIndex((v) => v.id === video.id);
  const next = onPick && at >= 0 ? GUIDE_VIDEOS[at + 1] : undefined;

  // Portalled to <body>: the onboarding GlassPanel is a containing block for
  // `fixed` (its glass filter), so rendered inside it the player would be
  // clipped to the panel instead of covering the window.
  return createPortal(
    <div
      className="fixed inset-0 z-[70] grid place-items-center bg-black/80 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={video.title}
      data-testid="guide-video-player"
    >
      <div className="relative w-[1100px] max-w-[94vw]" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the video"
          className="absolute -top-10 right-0 grid h-8 w-8 place-items-center rounded-lg text-white/80 transition-colors hover:bg-white/[0.1] hover:text-white"
        >
          <X className="h-5 w-5" />
        </button>
        <video
          key={video.id}
          src={url(video.id, 'mp4')}
          poster={url(video.id, 'jpg')}
          controls
          autoPlay
          playsInline
          className="aspect-video w-full rounded-2xl bg-black shadow-krypt-strong"
        />
        <div className="mt-3 flex items-center justify-between gap-3">
          <div className="min-w-0 text-sm font-semibold text-white">{video.title}</div>
          {next && (
            <button type="button" onClick={() => onPick?.(next)} className="krypt-btn-default shrink-0" data-testid="guide-video-next">
              Next: {next.title} <ArrowRight className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function Poster({ video, big }: { video: GuideVideo; big?: boolean }) {
  return (
    <span className="relative block overflow-hidden rounded-xl">
      <img src={url(video.id, 'jpg')} alt="" className="aspect-video w-full object-cover" />
      <span className="absolute bottom-1.5 left-1.5 transition-transform group-hover:scale-110">
        <PlayCircle className={big ? 'h-12 w-12 text-white drop-shadow' : 'h-8 w-8 text-white drop-shadow'} />
      </span>
      <span className="absolute bottom-1.5 right-1.5 rounded bg-black/75 px-1.5 py-0.5 font-mono text-[11px] text-white">
        {videoLength(video.seconds)}
      </span>
    </span>
  );
}

/** Every video, with the one to watch first on its own row. Top of the Guide page. */
export function GuideVideoLibrary() {
  const [playing, setPlaying] = useState<GuideVideo | null>(null);
  const [first, ...rest] = GUIDE_VIDEOS;
  const open = (v: GuideVideo) => () => setPlaying(v);
  return (
    <div data-testid="guide-videos">
      <button
        type="button"
        onClick={open(first)}
        data-testid={`guide-video-${first.id}`}
        className="group mb-4 grid w-full items-center gap-5 rounded-2xl border border-sky-400/40 bg-sky-500/10 p-3 text-left transition-colors hover:border-sky-400/70 hover:bg-sky-500/15 md:grid-cols-[360px_1fr]"
      >
        <Poster video={first} big />
        <span>
          <span className="block text-xs font-semibold uppercase tracking-wider text-sky-300">Watch this first</span>
          <span className="mt-1 block text-xl font-semibold text-white">{first.title}</span>
          <span className="mt-1 block text-sm text-krypt-muted">{first.blurb}</span>
        </span>
      </button>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        {rest.map((v) => (
          <button
            key={v.id}
            type="button"
            onClick={open(v)}
            data-testid={`guide-video-${v.id}`}
            className="group rounded-2xl border border-krypt-border bg-white/[0.02] p-2.5 text-left transition-colors hover:border-sky-400/50 hover:bg-white/[0.04]"
          >
            <Poster video={v} />
            <span className="mt-2 block px-1 text-sm font-semibold text-white">{v.title}</span>
            <span className="mt-0.5 block px-1 pb-1 text-xs text-krypt-muted">{v.blurb}</span>
          </button>
        ))}
      </div>
      {playing && <GuideVideoModal video={playing} onClose={() => setPlaying(null)} onPick={setPlaying} />}
    </div>
  );
}
