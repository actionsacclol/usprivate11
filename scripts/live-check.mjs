// Live provider check for the manual terminal.
//
//   npm run py:live
//
// Hits the REAL Polymarket APIs, so it is deliberately kept out of
// `npm run py:test`: it is slow and it depends on somebody else's uptime, and
// a red run here should never block CI. Run it by hand after touching
// anything in python/terminal.py or python/terminal_trade.py — this is the
// layer that catches a vanished route or a changed response shape, which no
// amount of green unit tests will.
//
// Read-only: it never authenticates and never places an order.
import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

ensureVenv();
run(VENV_PY, ['live_check_terminal.py', ...process.argv.slice(2)]);
