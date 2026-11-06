// Plays every guide video in the built app on a throwaway profile and reports what the player saw.
import { createRequire } from 'node:module';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, '..', '..');
const require = createRequire(path.join(APP, 'package.json'));
const { _electron: electron } = require('playwright-core');
const SB = process.env.SHOT_SANDBOX || path.join(os.tmpdir(), 'krypt-guide-play');
const EXE = process.env.PLAY_EXE;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
fs.rmSync(SB, { recursive: true, force: true });
const home = path.join(SB, 'home');
const env = { ...process.env, APPDATA: path.join(SB, 'Roaming'), LOCALAPPDATA: path.join(SB, 'Local'), USERPROFILE: home, HOME: home, KRYPT_POLYBOT_US_VAULT: path.join(SB, 'vault') };
delete env.VITE_DEV_SERVER_URL;
const ud = path.join(SB, 'ud');
for (const d of [ud, home, env.APPDATA, env.LOCALAPPDATA, env.KRYPT_POLYBOT_US_VAULT]) fs.mkdirSync(d, { recursive: true });
const app = await electron.launch(EXE ? { executablePath: EXE, args: [`--user-data-dir=${ud}`], env } : { executablePath: path.join(APP, 'node_modules/electron/dist/electron.exe'), args: [APP, `--user-data-dir=${ud}`], env, cwd: APP });
const page = await app.firstWindow();
const errors = [];
page.on('pageerror', (e) => errors.push(e.stack || String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
const results = [];
try {
  await page.locator('[data-testid="onboarding-continue"]').waitFor({ timeout: 60000 });
  await sleep(1000);
  await page.evaluate(() => window.krypt.config.update({ mcpPort: 47698 }));
  const check = async (label) => {
    await page.locator('[data-testid="guide-video-player"] video').waitFor({ timeout: 8000 });
    await sleep(2500);
    const r = await page.evaluate(() => {
      const v = document.querySelector('[data-testid="guide-video-player"] video');
      return { t: Math.round(v.currentTime * 10) / 10, dur: Math.round(v.duration), w: v.videoWidth, err: v.error?.message ?? null, paused: v.paused, src: v.currentSrc.slice(0, 40), audio: (v.webkitAudioDecodedByteCount ?? 0) > 0 };
    });
    results.push({ label, ...r });
  };
  // Autoplay may be held by the browser until a click, and a user's click always counts here.
  await page.locator('[data-testid="guide-video-open"]').click({ force: true });
  await check('onboarding card');
  await page.keyboard.press('Escape');
  await page.evaluate(() => window.krypt.config.update({ acceptedDisclaimer: true, onboardingComplete: true }));
  await page.evaluate(() => location.reload());
  await sleep(4000);
  await page.evaluate(() => [...document.querySelectorAll('aside button')].find((b) => b.innerText.trim().startsWith('Guide'))?.click());
  await sleep(1200);
  for (let guard = 0; guard < 3; guard++) {
    if (await page.locator('[data-testid="guide-videos"]').count()) break;
    await page.keyboard.press('Escape');
    await page.evaluate(() => { for (const t of ['Skip', 'Not now']) [...document.querySelectorAll('button')].find((b) => b.innerText.trim() === t)?.click(); });
    await sleep(800);
    await page.evaluate(() => [...document.querySelectorAll('aside button')].find((b) => b.innerText.trim().startsWith('Guide'))?.click());
    await sleep(1200);
  }
  const ids = await page.evaluate(() => [...document.querySelectorAll('[data-testid^="guide-video-"]')].map((b) => b.getAttribute('data-testid')).filter((x) => x !== 'guide-videos'));
  for (const id of ids) {
    await page.evaluate((i) => document.querySelector(`[data-testid="${i}"]`).click(), id);
    await check(id);
    await page.keyboard.press('Escape');
    await sleep(300);
  }
} finally {
  for (const r of results) console.log(JSON.stringify(r));
  console.log('errors:', JSON.stringify([...new Set(errors)].slice(0, 6), null, 0).slice(0, 1400));
  await app.close().catch(() => {});
}
