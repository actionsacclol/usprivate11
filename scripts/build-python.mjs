// Builds the Python backend into a single-folder PyInstaller bundle that
// electron-builder picks up via the `extraResources` glob in package.json.
//
// Output: python/dist/krypt-polybot-us-backend/krypt-polybot-us-backend(.exe)
import { spawnSync } from 'node:child_process';
import { existsSync, mkdtempSync, readdirSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ensureVenv, run, VENV_PY, PY_DIR } from './python-utils.mjs';

ensureVenv();

console.log('>> Installing PyInstaller');
// Range, not an exact pin: an exact pin (==6.6.0) rotted out of PyPI for the CI
// runners' Python, failing the build. Any 6.x works for our usage.
run(VENV_PY, ['-m', 'pip', 'install', 'pyinstaller>=6.6,<7', '--disable-pip-version-check']);

console.log('>> Cleaning previous build');
for (const d of ['build', 'dist']) {
  const p = join(PY_DIR, d);
  if (existsSync(p)) {
    rmSync(p, { recursive: true, force: true });
  }
}

// Build from the .spec file: it is the single source of truth for what gets
// bundled, including the lazy-imported modules a plain CLI invocation would miss.
console.log('>> Running PyInstaller from spec (this takes ~60s)');
run(VENV_PY, ['-m', 'PyInstaller', '--noconfirm', 'krypt-polybot-us-backend.spec']);

const out = join(PY_DIR, 'dist', 'krypt-polybot-us-backend');
if (!existsSync(out)) {
  console.error('!! PyInstaller did not produce', out);
  process.exit(1);
}

// Smoke-test the FROZEN bundle before electron-builder packages it. This is the
// gate that catches a bundle which built clean but is missing a runtime dep \u2014
// a lazy import can be dropped by PyInstaller without any build error, and the
// backend then dies with ModuleNotFoundError on the user's machine (the app ships
// with NO system-Python fallback). The frozen exe's `--selftest` imports the core
// deps AND signs a request offline; a non-zero exit here aborts `npm run dist` so a broken backend can
// never reach a release installer.
const exe = join(out, process.platform === 'win32'
  ? 'krypt-polybot-us-backend.exe'
  : 'krypt-polybot-us-backend');
console.log('>> Selftest: verifying frozen bundle can import deps + sign an order');
// In a throwaway profile: a frozen backend with no userData falls back to
// <bundle>/data and restores the user's newest DB snapshot from the survival
// vault into it, which electron-builder then packaged (the first 3.1.0 build
// shipped an 825 MB research DB). The selftest isolates itself too; this is
// the second lock.
const profile = mkdtempSync(join(tmpdir(), 'kp-build-selftest-'));
const st = spawnSync(exe, ['--selftest'], {
  stdio: 'inherit',
  windowsHide: true,
  env: {
    ...process.env,
    KRYPT_POLYBOT_US_USERDATA: join(profile, 'userdata'),
    KRYPT_POLYBOT_US_VAULT: join(profile, 'vault'),
  },
});
rmSync(profile, { recursive: true, force: true });
if (st.error) {
  console.error(`!! Could not run frozen backend selftest: ${st.error.message}`);
  process.exit(1);
}
if (st.status !== 0) {
  console.error(
    '!! Frozen backend FAILED selftest (see report above). The bundle is missing\n' +
    '!! a runtime dependency or signer \u2014 refusing to ship it. Check that the build\n' +
    '!! venv has all of requirements.txt and that the .spec collects the failing\n' +
    '!! package, then rebuild. Aborting before electron-builder.',
  );
  process.exit(1);
}

// The bundle ships to users: no database and no user data, ever. Anything a
// run of the bundle left behind goes, and a database file anywhere in it
// fails the build.
for (const d of [join(out, 'data'), join(out, '_internal', 'data')]) {
  if (existsSync(d)) {
    console.log(`>> Removing ${d} (runtime data must never ship)`);
    rmSync(d, { recursive: true, force: true });
  }
}
const stray = [];
const walk = (dir) => {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) walk(p);
    else if (/\.(db|db-wal|db-shm|sqlite3?)$/i.test(name)) stray.push(p);
  }
};
walk(out);
if (stray.length) {
  console.error('!! The bundle contains database files; refusing to ship user data:');
  for (const p of stray) console.error(`!!   ${p}`);
  process.exit(1);
}

console.log('>> OK \u2014 backend bundled + selftest PASSED at python/dist/krypt-polybot-us-backend');
