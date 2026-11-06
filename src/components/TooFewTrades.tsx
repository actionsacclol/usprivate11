/** Below this many trades a backtest result is mostly luck. Same line replay.py draws. */
export const FEW_TRADES = 30;

export function tooFewTrades(n: number): boolean {
  return n > 0 && n < FEW_TRADES;
}

/** A small sample can read as 5 of 5 wins. Says so above the numbers, not under them. */
export function TooFewTrades({ n }: { n: number }) {
  if (!tooFewTrades(n)) return null;
  return (
    <div
      data-testid="too-few-trades"
      className="mt-3 rounded-lg border border-krypt-warn/40 bg-krypt-warn/10 p-2.5 text-xs leading-relaxed text-krypt-warn"
    >
      <strong>Only {n} {n === 1 ? 'trade' : 'trades'}: too few to tell luck from skill.</strong>{' '}
      Don&apos;t read anything into these numbers yet. Test a longer window, or download more
      history on the Backtest page.
    </div>
  );
}
