// Shared helpers for the Python build/setup scripts. Pure Node.js,
// works on any system without PowerShell.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const __dirname = dirname(fileURLToPath(import.meta.url));
export const ROOT = dirname(__dirname);
export const PY_DIR = join(ROOT, 'python');
export const VENV = join(PY_DIR, '.venv');
export const VENV_PY = process.platform === 'win32'
  ? join(VENV, 'Scripts', 'python.exe')
  : join(VENV, 'bin', 'python');

const isWin = process.platform === 'win32';

/** Pretty-print a command line and run it synchronously, fail-fast on non-zero exit. */
export function run(cmd, args, opts = {}) {
  const display = `${cmd} ${args.map((a) => (a.includes(' ') ? `"${a}"` : a)).join(' ')}`;
  console.log(`> ${display}`);
  const r = spawnSync(cmd, args, {
    stdio: 'inherit',
    cwd: PY_DIR,
    shell: false,
    ...opts,
  });
  if (r.error) {
    throw new Error(`${cmd} could not be spawned: ${r.error.message}`);
  }
  if (r.status !== 0) {
    throw new Error(`${display} failed with exit code ${r.status}`);
  }
}

/** Try a command silently and return whether it succeeded. */
export function tryCmd(cmd, args) {
  try {
    const r = spawnSync(cmd, args, {
      stdio: 'ignore',
      shell: false,
      windowsHide: true,
    });
    return !r.error && r.status === 0;
  } catch {
    return false;
  }
}

/** Minimum interpreter the backend runs on. `service.py` uses
 *  `asyncio.timeout` (3.11+), and CI/the frozen build pin 3.11 — a dev venv
 *  built on an older interpreter would pass `--version` and then fail at the
 *  first master-loop tick. */
export const MIN_PY = [3, 11];

/** `[major, minor]` reported by `<cmd> <args> --version`, or null. */
function pyVersion(cmd, args) {
  try {
    const r = spawnSync(cmd, [...args, '--version'], {
      encoding: 'utf8', shell: false, windowsHide: true,
    });
    if (r.error || r.status !== 0) return null;
    const m = /Python (\d+)\.(\d+)/.exec(`${r.stdout || ''}${r.stderr || ''}`);
    return m ? [Number(m[1]), Number(m[2])] : null;
  } catch {
    return null;
  }
}

function atLeast(v, min) {
  return v[0] > min[0] || (v[0] === min[0] && v[1] >= min[1]);
}

/** Resolve a working system Python interpreter of at least MIN_PY. Throws
 *  with a helpful message (naming what WAS found) if none qualifies. Caches
 *  the result. */
let _systemPy = null;
export function findSystemPython() {
  if (_systemPy) return _systemPy;
  // Order: py launcher (Windows), python3, python.
  const py = { cmd: 'py', args: ['-3', '--version'], spawnAs: 'py' };
  const python = { cmd: 'python', args: ['--version'], spawnAs: 'python' };
  const python3 = { cmd: 'python3', args: ['--version'], spawnAs: 'python3' };
  // actions/setup-python puts the pinned version first on PATH and sets
  // pythonLocation, but `py -3` ignores both and picks the newest Python on the
  // runner. That froze the Windows backend on 3.15, whose installer keeps
  // python3t.dll where PyInstaller can't find it, so cryptography's _rust.pyd
  // failed to load in the bundle. On CI, use the Python the workflow asked for.
  const candidates = isWin
    ? (process.env.pythonLocation ? [python, py, python3] : [py, python, python3])
    : [
        { cmd: 'python3', args: ['--version'], spawnAs: 'python3' },
        { cmd: 'python', args: ['--version'], spawnAs: 'python' },
      ];
  const seen = [];
  for (const c of candidates) {
    if (!tryCmd(c.cmd, c.args)) continue;
    const v = pyVersion(c.cmd, c.args.filter((a) => a !== '--version'));
    if (v && atLeast(v, MIN_PY)) {
      _systemPy = c.spawnAs;
      return _systemPy;
    }
    seen.push(`${c.spawnAs} = ${v ? v.join('.') : 'unknown'}`);
  }
  const found = seen.length ? ` Found: ${seen.join(', ')}.` : '';
  throw new Error(
    `No Python ${MIN_PY.join('.')}+ interpreter found on PATH.${found} ` +
    'Install Python 3.11+ from https://python.org and try again.',
  );
}

/** Args used to invoke the system python (e.g. `py -3` -> `['-3']`). */
export function systemPyPrefix() {
  return findSystemPython() === 'py' ? ['-3'] : [];
}

/** Ensure the Python venv exists and has all backend deps installed.
 *  No-op if the venv interpreter is already present and httpx imports OK. */
export function ensureVenv({ force = false } = {}) {
  if (!existsSync(VENV) || force) {
    const sys = findSystemPython();
    console.log('>> Creating Python venv at python/.venv');
    run(sys, [...systemPyPrefix(), '-m', 'venv', '.venv']);
  }
  if (!existsSync(VENV_PY)) {
    throw new Error(`venv created but no interpreter at ${VENV_PY}`);
  }
  // Verify deps; skip pip install only if ALL core imports resolve.
  if (!force && tryCmd(VENV_PY, ['-c', 'import httpx, cryptography, websockets, keyring'])) {
    return;
  }
  console.log('>> Installing Python deps into venv (one-time, ~30s)');
  run(VENV_PY, ['-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', '--disable-pip-version-check']);
  run(VENV_PY, ['-m', 'pip', 'install', '-r', 'requirements.txt', '--disable-pip-version-check']);
}
