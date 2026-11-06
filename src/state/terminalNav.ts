import { useEffect, useState } from 'react';

// A module-level store rather than a React context, on purpose.
//
// A context object is a NEW identity on every provider render, so any
// `useEffect` that depends on it re-runs on every poll — tearing down and
// rebuilding whatever it set up. On the crypto build that pattern deleted the
// recorded tape on every tick, so a live chart could never accumulate, and it
// LOOKED fine because React batched the state writes into no net change.
//
// Subscribing to a stable module store makes that class of bug impossible:
// the setter identity never changes, so an effect that depends on it never
// re-runs.

let selected: string | null = null;
const subs = new Set<(v: string | null) => void>();

/** Open a market on the Market screen. Pass null to clear. */
export function selectMarket(conditionId: string | null): void {
  if (selected === conditionId) return;
  selected = conditionId;
  for (const fn of subs) fn(selected);
}

export function getSelectedMarket(): string | null {
  return selected;
}

export function useSelectedMarket(): string | null {
  const [v, setV] = useState<string | null>(selected);
  useEffect(() => {
    // `setV` is stable across renders, so this effect runs exactly once.
    subs.add(setV);
    setV(selected);
    return () => { subs.delete(setV); };
  }, []);
  return v;
}
