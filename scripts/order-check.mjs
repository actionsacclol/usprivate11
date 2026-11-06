// Live check of the Polymarket US order path, on the owner's own API key.
//
//   npm run py:orders -- --dry-run     no key, nothing signed or sent
//   npm run py:orders                  one post-only order, cancelled. Cost: $0
//   npm run py:orders -- --with-fill   also real small fills (real money)
//
// Unlocks buying NO. Never run by `npm run py:test` or CI. See python/live_order_check.py.
import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

ensureVenv();
run(VENV_PY, ['live_order_check.py', ...process.argv.slice(2)]);
