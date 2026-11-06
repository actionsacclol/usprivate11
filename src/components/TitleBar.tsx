import { useEffect, useState } from 'react';
import { Minus, Square, Copy as Restore, X } from 'lucide-react';
import { useApp } from '../state/AppStateProvider';
import { cls } from '../utils/format';
import { GlassPanel } from './glass/GlassPanel';
import { GLASS_TINTS } from './glass/glassPresets';

export function TitleBar() {
  const { backend, config, appVersion } = useApp();
  const [maxed, setMaxed] = useState(false);

  useEffect(() => {
    let mounted = true;
    void window.krypt.window.isMaximized().then((m) => {
      if (mounted) setMaxed(m);
    });
    const off = window.krypt.window.onMaximizeChange((m) => setMaxed(m));
    return () => {
      mounted = false;
      off();
    };
  }, []);

  const dot =
    backend.status === 'running' ? 'bg-krypt-win' :
    backend.status === 'starting' ? 'bg-krypt-warn' :
    backend.status === 'crashed' || backend.status === 'restarting' ? 'bg-krypt-loss' :
    'bg-krypt-dim';
  const live = config?.accountMode === 'live';

  return (
    // Live glass: the aurora drifting under the window's own frame is the
    // first thing anyone sees. Rim-only optics (chrome preset) because a
    // 1380px-wide lens with a body dome would bloom into an oval.
    <GlassPanel
      preset="chrome"
      display="flex"
      tint={GLASS_TINTS.chrome}
      className="titlebar-drag relative z-30 h-9 shrink-0 select-none items-center justify-between border-b border-white/[0.06] px-3"
    >
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <div className="h-[18px] w-[18px] rounded-md bg-gradient-to-br from-sky-400 via-blue-500 to-blue-700 shadow-[inset_0_1px_0_rgba(255,255,255,0.45),0_0_14px_-2px_rgba(59,130,246,0.85)]" />
          <span className="font-pixel text-[10px] uppercase tracking-[0.2em] text-white/90">
            Krypt PolyBot US
          </span>
          {appVersion && (
            <span className="rounded-full border border-krypt-purple/30 bg-krypt-purple/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-krypt-purple shadow-[inset_0_1px_0_rgba(255,255,255,0.08)]">
              v{appVersion}
            </span>
          )}
        </div>
        <div className="hidden items-center gap-2 text-[11px] text-krypt-muted lg:flex">
          <span className={cls('h-2 w-2 rounded-full', dot, backend.status === 'running' && 'shadow-[0_0_8px_currentColor]')} />
          <span className="capitalize">{backend.status}</span>
          {/* In Paper nothing is signed, so "auth needed" would raise an alarm
              about an API key nobody needs yet. The Paper | Live switch sits just
              below this bar, and a second "paper" label here looked clickable. */}
          {!live ? null : backend.authOk ? (
            <span className="krypt-pill border-krypt-win/40 bg-krypt-win/10 text-krypt-win">
              API key ok
            </span>
          ) : (
            <span className="krypt-pill border-krypt-loss/40 bg-krypt-loss/10 text-krypt-loss">
              API key needed
            </span>
          )}
        </div>
      </div>

      <div className="titlebar-no-drag flex items-center">
        <button
          onClick={() => window.krypt.window.minimize()}
          aria-label="Minimize"
          className="grid h-9 w-11 place-items-center text-krypt-muted transition-colors hover:bg-white/[0.06] hover:text-white"
        >
          <Minus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => window.krypt.window.maximize()}
          aria-label={maxed ? 'Restore' : 'Maximize'}
          className="grid h-9 w-11 place-items-center text-krypt-muted transition-colors hover:bg-white/[0.06] hover:text-white"
        >
          {maxed ? <Restore className="h-3 w-3" /> : <Square className="h-3 w-3" />}
        </button>
        <button
          onClick={() => window.krypt.window.close()}
          aria-label="Close"
          className="grid h-9 w-11 place-items-center text-krypt-muted transition-colors hover:bg-krypt-loss hover:text-white"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
    </GlassPanel>
  );
}
