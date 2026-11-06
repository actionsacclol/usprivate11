import { ExternalLink } from 'lucide-react';
import { cls } from '../utils/format';
import { openPolymarketMarket } from '../utils/polymarket';

/**
 * A market ticker rendered as a clickable deep link to its Polymarket page.
 * Opens in the user's browser via the sandboxed `openExternal` bridge
 * (never an in-app navigation). `label` lets callers show something other
 * than the raw ticker (e.g. the asset name) while still linking by ticker.
 */
export function TickerLink({
  ticker,
  eventTicker,
  env,
  label,
  className,
}: {
  ticker: string;
  eventTicker?: string;
  env?: string;
  label?: string;
  className?: string;
}) {
  if (!ticker) return <span className="text-krypt-dim">{label ?? '—'}</span>;
  return (
    <button
      type="button"
      onClick={() => void openPolymarketMarket({ ticker, eventTicker, env })}
      title="Open this market on Polymarket US"
      className={cls(
        'group inline-flex items-center gap-1 font-mono text-xs text-krypt-purple',
        'transition-colors hover:text-krypt-pink hover:underline',
        className,
      )}
    >
      {label ?? ticker}
      <ExternalLink className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-70" />
    </button>
  );
}
