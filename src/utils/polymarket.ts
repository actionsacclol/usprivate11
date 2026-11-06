// Open the public Polymarket US page for a position or trade. Markets are
// addressed by slug, and the ticker the engines carry is that slug (a side
// suffix like `#yes` belongs to the plumbing, not the URL).
import { polymarketMarketUrl } from './links';

type LinkArgs = { ticker?: string; eventTicker?: string; env?: string };

export function polymarketUrl(args: LinkArgs): string {
  return polymarketMarketUrl((args.ticker || '').split('#')[0]) ?? '';
}

export async function openPolymarketMarket(args: LinkArgs): Promise<void> {
  const url = polymarketUrl(args);
  if (url) await window.krypt.app.openExternal(url);
}
