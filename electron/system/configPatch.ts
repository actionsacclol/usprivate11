/**
 * Config patches arriving from chat (`remote`) or a supervisor script
 * (`script`): the allowlist main applies before PERSISTING them.
 *
 * The store is not that gate: `patchConfig` is a plain spread, so whatever the
 * backend emitted would land in settings.json verbatim — a backend bug or a
 * compromised chat transport could write any key at all. (The AI agents'
 * `mcp` channel has its own, wider rule: sanitize.cleanAgentConfigPatch.)
 *
 * Kept free of `electron` imports so it can be unit-tested.
 */

type Kind = 'boolean' | 'number' | 'directionMode' | 'offOnly' | 'agentModeNarrow';

/** What the two senders legitimately produce: `remote.py` `_pause`/`_resume`
 *  flip the engine switches and `/pause` / `/agents off` stop the AI agents;
 *  `script_engine.py` `_SUPERVISE_KEYS` adds the crypto strategy posture. */
export const CONFIG_PATCH_ALLOW: Record<string, Kind> = {
  crypto15mEnabled: 'boolean',
  scriptsLiveEnabled: 'boolean',
  // Stopping the agents only: these can be turned OFF from a channel, never
  // on. Going live for agents is a checklist in the app, not a chat line.
  autopilotEnabled: 'offOnly',
  mcpTradeMode: 'agentModeNarrow',
  crypto15mDirectionMode: 'directionMode',
  crypto15mEntryThreshold: 'number',
  crypto15mEntryMax: 'number',
  crypto15mMinDeltaPct: 'number',
  crypto15mTimeDelayMin: 'number',
};

export function sanitizeChannelPatch(
  _source: string,
  patch: Record<string, unknown>,
  drop: (k: string, why: string) => void,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(patch ?? {})) {
    const kind = CONFIG_PATCH_ALLOW[k];
    if (!kind) { drop(k, 'not patchable from this channel'); continue; }
    if (kind === 'boolean') {
      if (typeof v !== 'boolean') { drop(k, `expected boolean, got ${typeof v}`); continue; }
      out[k] = v;
    } else if (kind === 'offOnly') {
      if (v !== false) { drop(k, 'can only be turned OFF from this channel'); continue; }
      out[k] = false;
    } else if (kind === 'agentModeNarrow') {
      if (v !== 'off' && v !== 'paper') { drop(k, "only 'off' or 'paper' from this channel"); continue; }
      out[k] = v;
    } else if (kind === 'number') {
      if (typeof v !== 'number' || !Number.isFinite(v)) { drop(k, 'not a finite number'); continue; }
      out[k] = v;
    } else if (v === 'favorite' || v === 'contrarian' || v === 'model') {
      out[k] = v;
    } else {
      drop(k, 'not a valid direction mode');
    }
  }
  return out;
}
