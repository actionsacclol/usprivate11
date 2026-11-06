// Every TraderConfig key must have a default in DEFAULT_CONFIG.
//
//   npm run test:unit
//
// This is not style policing. `mergeConfig()` builds the renderer's config as
// `{...DEFAULT_CONFIG, ...loaded}`, and applying a preset REPLACES the whole
// config with one built from DEFAULT_CONFIG — so a key missing here reaches the
// UI as `undefined` and is wiped on every Apply. That is how a user's lifetime
// loss cap silently returned to 0 (off).
//
// Parsed from source rather than imported: settings-store.ts imports `electron`,
// which cannot load outside an Electron process. A regex over the two files is
// the same check by hand, and it is the shape of the bug (a key present in one
// file and absent from the other) that matters, not the values.

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import test from 'node:test';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(root, p), 'utf-8').replace(/\r\n/g, '\n');

/** The body of a top-level `{ … }` block, from `start` to the first line that
 *  is exactly `}` or `};` — CRLF already normalised by read(). */
function blockBody(src, start) {
  const i = src.indexOf(start);
  assert.ok(i >= 0, `could not find ${start}`);
  const end = src.slice(i).search(/\n\}[;]?\n/);
  assert.ok(end > 0, `could not find the end of ${start}`);
  return src.slice(i, i + end);
}

/** Keys declared at one level of indentation, ignoring comments and nesting. */
const keysOf = (body, re) => [...body.matchAll(re)].map((m) => m[1]);

test('every TraderConfig key has a default in DEFAULT_CONFIG', () => {
  const iface = blockBody(read('shared/types.ts'), 'export interface TraderConfig {');
  const defaults = blockBody(
    read('electron/system/settings-store.ts'), 'export const DEFAULT_CONFIG');

  const declared = keysOf(iface, /^ {2}([A-Za-z][A-Za-z0-9]*)\??\s*:/gm);
  const defaulted = new Set(keysOf(defaults, /^ {2}([A-Za-z][A-Za-z0-9]*)\s*:/gm));

  // Sanity: a broken regex must fail loudly rather than pass vacuously.
  assert.ok(declared.length > 100, `only parsed ${declared.length} TraderConfig keys`);
  assert.ok(defaulted.size > 100, `only parsed ${defaulted.size} DEFAULT_CONFIG keys`);
  assert.ok(declared.includes('crypto15mEnabled'), 'parser missed a known key');

  const missing = declared.filter((k) => !defaulted.has(k));
  assert.deepEqual(missing, [],
    `TraderConfig keys with no DEFAULT_CONFIG entry — a preset apply wipes each `
    + `of these: ${missing.join(', ')}`);
});

test('DEFAULT_CONFIG declares nothing TraderConfig does not', () => {
  const iface = blockBody(read('shared/types.ts'), 'export interface TraderConfig {');
  const defaults = blockBody(
    read('electron/system/settings-store.ts'), 'export const DEFAULT_CONFIG');

  const declared = new Set(keysOf(iface, /^ {2}([A-Za-z][A-Za-z0-9]*)\??\s*:/gm));
  const extra = keysOf(defaults, /^ {2}([A-Za-z][A-Za-z0-9]*)\s*:/gm)
    .filter((k) => !declared.has(k));
  assert.deepEqual(extra, [], `DEFAULT_CONFIG keys not in TraderConfig: ${extra.join(', ')}`);
});
