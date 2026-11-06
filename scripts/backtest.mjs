// Run the fee-aware signal backtest against your Krypt PolyBot US DB.
//
//   npm run py:backtest                       # auto-detect your DB
//   npm run py:backtest -- --demo             # synthetic demo (see the format)
//   npm run py:backtest -- --db "C:/path/krypt-polybot-us.db" --fee 0.07
//
// Reuses the project venv (same helper as setup/test). The Python script
// opens the DB read-only and never mutates your trade history.
import { ensureVenv, run, VENV_PY } from './python-utils.mjs';

ensureVenv();
run(VENV_PY, ['backtest.py', ...process.argv.slice(2)]);
