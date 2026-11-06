// Shared wording helpers for every hub scene. Three-free, so a scene's
// chunk never drags in another scene's engine just to shorten a title.

/** Polymarket's crypto Up/Down titles name the coin in words
 *  ("Bitcoin Up or Down - October 8, 3:15PM-3:30PM ET"). */
const COIN_WORDS: [RegExp, string][] = [
  [/^(bitcoin|btc)\b/i, 'BTC'], [/^(ethereum|eth)\b/i, 'ETH'], [/^(solana|sol)\b/i, 'SOL'],
  [/^(xrp|ripple)\b/i, 'XRP'], [/^(dogecoin|doge)\b/i, 'DOGE'], [/^(hyperliquid|hype)\b/i, 'HYPE'],
  [/^(bnb|binance coin)\b/i, 'BNB'],
];

/** A market id fit for a label: a slug or id, clipped. */
export function marketTag(id: string | null | undefined): string {
  const s = (id ?? '').trim();
  return s.length > 16 ? `${s.slice(0, 15)}…` : s || 'market';
}

/** "Will the Lakers beat the Celtics tonight?" → "Lakers beat Celtics";
 *  "Bitcoin Up or Down - …" → "BTC". A bubble has room for a few words, not
 *  a question. With no title, the market id itself, shortened. */
export function subject(ticker: string, title: string): string {
  const raw = (title ?? '').trim();
  if (!raw || raw === ticker) return marketTag(ticker);
  if (/\bup or down\b/i.test(raw)) {
    for (const [re, coin] of COIN_WORDS) if (re.test(raw)) return coin;
  }
  let t = raw.replace(/^(Will|Is|Does|Do|Can|Are)\s+(there\s+be\s+)?(the\s+|an?\s+)?/i, '').replace(/\?$/, '');
  t = t.replace(/\s+(tonight|today|this week|this month|in 20\d\d|by .*)$/i, '');
  t = t.replace(/\bthe\s+/gi, '');
  return t.length > 24 ? `${t.slice(0, 23).trimEnd()}…` : t;
}

/** Memoise `measureText` widths on an overlay context. The overlays re-measure
 * the same nameplates, bubbles and HUD strings every frame; in the profile that
 * was ~5% of the main thread on its own. Every caller reads only `.width`, and
 * the key includes `font`, so a font change can't return a stale width. */
export function cacheMeasureText(ctx: CanvasRenderingContext2D): CanvasRenderingContext2D {
  const orig = ctx.measureText.bind(ctx);
  const cache = new Map<string, TextMetrics>();
  ctx.measureText = (text: string): TextMetrics => {
    const key = `${ctx.font}\u0000${text}`;
    let m = cache.get(key);
    if (!m) {
      if (cache.size > 4000) cache.clear(); // bubbles carry live numbers — bound it
      m = orig(text);
      cache.set(key, m);
    }
    return m;
  };
  return ctx;
}
