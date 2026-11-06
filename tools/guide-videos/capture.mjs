// Screens for the guide videos: walks videos.json in the real app and saves one screenshot per scene, plus
// where each highlighted thing is. Everything runs on a throwaway profile on paper, with no key.
//
//   npm run build
//   node tools/guide-videos/capture.mjs [video-id ...]     quickstart always runs, because it passes onboarding
//
// Writes tools/guide-videos/shots/<video>/<scene>.png + .json. SHOT_SANDBOX picks the throwaway folder.
import { createRequire } from 'node:module';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const APP = path.resolve(HERE, '..', '..');
const require = createRequire(path.join(APP, 'package.json'));
const { _electron: electron } = require('playwright-core');
const only = new Set(process.argv.slice(2));
const videos = JSON.parse(fs.readFileSync(path.join(HERE, 'videos.json'), 'utf8'));
const SB = process.env.SHOT_SANDBOX || path.join(os.tmpdir(), 'krypt-guide-shots');
const VW = 1600, VH = 900, DSF = 1.2;

// Own port, home, Claude folders and vault: a real app running at the same time is never reached.
const AGENT_PORT = 47699;
fs.rmSync(SB, { recursive: true, force: true });
const home = path.join(SB, 'home');
const env = { ...process.env, APPDATA: path.join(SB, 'Roaming'), LOCALAPPDATA: path.join(SB, 'Local'), USERPROFILE: home, HOME: home,
  KRYPT_POLYBOT_US_VAULT: path.join(SB, 'vault'), CLAUDE_CONFIG_DIR: path.join(home, '.claude') };
delete env.VITE_DEV_SERVER_URL;
const ud = path.join(SB, 'ud');
for (const d of [ud, home, env.APPDATA, env.LOCALAPPDATA, env.KRYPT_POLYBOT_US_VAULT, env.CLAUDE_CONFIG_DIR, path.join(env.APPDATA, 'Claude'), path.join(home, '.cursor')]) {
  fs.mkdirSync(d, { recursive: true });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const app = await electron.launch({ executablePath: path.join(APP, 'node_modules/electron/dist/electron.exe'),
  args: [APP, `--user-data-dir=${ud}`], env, cwd: APP });
// Links are recorded, not opened: no browser pops up on the desktop while this runs.
await app.evaluate(({ shell }) => { shell.openExternal = async () => undefined; });
const page = await app.firstWindow();
const cdp = await page.context().newCDPSession(page);
await cdp.send('Emulation.setDeviceMetricsOverride', { width: VW, height: VH, deviceScaleFactor: DSF, mobile: false });
const problems = [];
const errors = [];
page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
page.on('dialog', (d) => d.accept().catch(() => {}));

// Find the on-screen element a step means. Last match wins, because a dialog sits after the page behind it.
const HELPERS = `(() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = (e) => (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ');
  window.__find = (q) => {
    if (q.id) return [...document.querySelectorAll('[data-testid="' + q.id + '"]')].filter(vis).at(-1) || null;
    if (q.qa) return [...document.querySelectorAll('[data-qa="' + q.qa + '"]')].filter(vis).at(-1) || null;
    if (q.sel) return [...document.querySelectorAll(q.sel)].filter(vis).at(q.first ? 0 : -1) || null;
    if (q.nav) return [...document.querySelectorAll('aside button')].filter(vis).find((b) => clean(b).startsWith(q.nav)) || null;
    if (q.size) return [...document.querySelectorAll('label,div')].filter(vis).filter((d) => /^Size/.test(clean(d)) && d.querySelector('input')).map((d) => d.querySelector('input')).at(0) || null;
    if (q.t) return [...document.querySelectorAll('button,a,[role=button],label,[role=tab]')].filter(vis).filter((b) => clean(b).startsWith(q.t)).at(q.first ? 0 : -1) || null;
    if (q.has) {
      const all = [...document.querySelectorAll('main *, [role=dialog] *, header *, body > div *')].filter(vis).filter((e) => clean(e).startsWith(q.has));
      // Shortest text wins; on a tie the later one, because a dialog comes after the page behind it.
      const least = Math.min(...all.map((e) => clean(e).length));
      let hit = all.filter((e) => clean(e).length === least).at(-1) || null;
      for (let i = 0; hit && i < (q.up || 0); i++) hit = hit.parentElement;
      return hit;
    }
    return null;
  };
  window.__setInput = (e, v) => {
    const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, v);
    e.dispatchEvent(new Event('input', { bubbles: true }));
  };
  /** A component's useState hook holding this value: a screen that needs a real key can't be reached otherwise. */
  window.__setHook = (e, from, to) => {
    let f = e[Object.keys(e).find((k) => k.startsWith('__reactFiber$'))];
    while (f) {
      let h = f.memoizedState;
      if (f.tag === 0 || f.tag === 11 || f.tag === 15) {
        while (h) {
          if (h.queue && h.queue.dispatch && h.memoizedState === from) { h.queue.dispatch(to); return true; }
          h = h.next;
        }
      }
      f = f.return;
    }
    return false;
  };
  return 1;
})()`;
const helpers = () => page.evaluate(HELPERS);

async function act(step, where) {
  await helpers();
  if (step.wait) return sleep(step.wait);
  if (step.js) { const v = await page.evaluate(step.js); if (step.print) console.log('   js', JSON.stringify(v)?.slice(0, 400)); return v; }
  if (step.config) { await page.evaluate((c) => window.krypt.config.update(c), step.config); return sleep(400); }
  if (step.esc) { await page.keyboard.press('Escape'); return sleep(400); }
  if (step.fill) {
    const ok = await page.evaluate(([q, v]) => { const e = window.__find(q); if (!e) return false; e.focus?.(); window.__setInput(e, v); return true; }, [step.fill, step.value]);
    if (!ok) problems.push(`${where}: no field ${JSON.stringify(step.fill)}`);
    return sleep(250);
  }
  if (step.hook) {
    const ok = await page.evaluate(([q, a, b]) => { const e = window.__find(q); return !!e && window.__setHook(e, a, b); }, [step.hook, step.from, step.to]);
    if (!ok) problems.push(`${where}: no hook ${JSON.stringify(step.hook)} ${step.from}`);
    return sleep(step.after ?? 800);
  }
  if (step.scrollTo) {
    await page.evaluate((q) => window.__find(q)?.scrollIntoView({ block: q.block || 'center' }), step.scrollTo);
    return sleep(500);
  }
  // Anything else is a click.
  const ok = await page.evaluate((q) => { const e = window.__find(q); if (!e) return false; e.click(); return true; }, step);
  if (!ok && !step.optional) problems.push(`${where}: nothing to click for ${JSON.stringify(step)}`);
  return sleep(step.after ?? 800);
}

async function measure(hl, where) {
  await helpers();
  return page.evaluate(({ hl, dsf }) => hl.map((q) => {
    const e = window.__find(q);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    return [r.left * dsf, r.top * dsf, r.right * dsf, r.bottom * dsf].map((v) => Math.round(v));
  }), { hl, dsf: DSF }).then((rects) => {
    rects.forEach((r, i) => { if (!r) problems.push(`${where}: no highlight for ${JSON.stringify(hl[i])}`); });
    return rects;
  });
}

// Fills the Default agent's scoreboard through the agent server, as a client would. Paper only, on this throwaway profile.
async function seedAgent() {
  await page.evaluate((c) => window.krypt.config.update(c), { mcpEnabled: true, mcpTradeMode: 'paper' });
  await sleep(3000);
  const spec = await page.evaluate(() => window.krypt.mcp.clients('default'));
  const entry = JSON.parse(spec.find((c) => c.id === 'cursor').snippet).mcpServers['krypt-polybot-us'];
  // The entry names the profile it relays to. Anything but this throwaway one stops the run.
  if (path.resolve(entry.env?.KRYPT_POLYBOT_US_USERDATA ?? '') !== path.resolve(ud)) throw new Error('relay is not on the throwaway profile');
  const proc = spawn(entry.command, entry.args, { env: { ...env, ...entry.env }, stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
  let buf = '';
  proc.stdout.on('data', (d) => { buf += d.toString(); });
  let nid = 0;
  const call = async (method, params, ms = 40000) => {
    const id = ++nid;
    proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    const end = Date.now() + ms;
    while (Date.now() < end) {
      const hit = buf.split('\n').filter(Boolean).map((l) => { try { return JSON.parse(l); } catch { return null; } }).find((m) => m?.id === id);
      if (hit) return hit;
      await sleep(100);
    }
    return { error: 'timeout' };
  };
  const tool = async (name, args = {}) => {
    const r = await call('tools/call', { name, arguments: args });
    const t = r.result?.structuredContent ?? r.result?.content?.[0]?.text ?? r.error;
    if (typeof t !== 'string') return t;
    try { return JSON.parse(t); } catch { return t; }
  };
  try {
    await call('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'claude-code', version: 'guide' } });
    proc.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);
    const status = await tool('get_status');
    if (status?.permissions?.trade !== 'paper') throw new Error(`agent mode is ${status?.permissions?.trade}, so nothing was traded`);
    const found = await tool('search_markets', { query: 'Pro Football Champion', limit: 30 });
    const rows = (found?.rows ?? []).map((m) => ({ ...m, ask: m.yesAsk ?? m.ask ?? m.bestAsk })).filter((m) => m.ask >= 4 && m.ask <= 70).slice(0, 3);
    for (const m of rows) {
      const id = m.conditionId ?? m.slug;
      const f = await tool('record_forecast', { conditionId: id, fairValueCents: Math.min(99, Math.round(m.ask + 12)), model: 'demo',
        rationale: 'A throwaway demo forecast for the guide video screens, on a paper account.' });
      const o = await tool('place_order', { conditionId: id, side: 'yes', action: 'buy', size: 5, priceCents: Math.ceil(m.ask), orderType: 'FAK', forecastId: f?.forecastId ?? f?.id });
      console.log('   seeded', String(id).slice(0, 30), 'ask', m.ask, 'forecast', f?.forecastId ?? f?.id, 'order', JSON.stringify(o)?.slice(0, 120));
    }
    if (!rows.length) console.log('   no market to seed:', JSON.stringify(found?.rows?.[0])?.slice(0, 700));
  } finally {
    proc.kill();
  }
  await sleep(1200);
}

// Sharper than page.screenshot, which comes back at 1600 wide. Falls back to it if the window is asleep.
async function shoot(file) {
  const grab = cdp.send('Page.captureScreenshot', { format: 'png', clip: { x: 0, y: 0, width: VW, height: VH, scale: 1 } });
  const late = sleep(10000).then(() => null);
  const shot = await Promise.race([grab, late]);
  if (shot?.data) fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
  else { problems.push(`${file}: fell back to a smaller screenshot`); await page.screenshot({ path: file }); }
}

let n = 0;
try {
  await page.locator('[data-testid="onboarding-continue"]').waitFor({ timeout: 60000 });
  await sleep(1500);
  await page.evaluate((port) => window.krypt.config.update({ mcpPort: port }), AGENT_PORT);
  // Polymarket US's status and passing toasts aren't part of the lesson, except where a scene asks for them.
  await page.addStyleTag({ content: `.guide-hide-alerts [data-testid="alert-stack"] { display: none !important; }
    .fixed.right-5.top-14 { display: none !important; }` });
  await app.evaluate(({ BrowserWindow }) => { BrowserWindow.getAllWindows()[0]?.setPosition(-2600, 40); });
  for (const video of videos) {
    const keep = !only.size || only.has(video.id) || video.id === 'quickstart';
    if (!keep) continue;
    const folder = path.join(HERE, 'shots', video.id);
    fs.rmSync(folder, { recursive: true, force: true });
    fs.mkdirSync(folder, { recursive: true });
    console.log(`== ${video.id}`);
    for (const step of video.start ?? []) await act(step, `${video.id}/start`);
    await page.evaluate(() => { for (const e of document.querySelectorAll('main, main *')) if (e.scrollTop) e.scrollTop = 0; });
    for (const scene of video.scenes) {
      const where = `${video.id}/${scene.id}`;
      if (scene.seed) await seedAgent().catch((e) => problems.push(`seed: ${e}`));
      for (const step of scene.do ?? []) await act(step, where);
      await page.evaluate((show) => {
        document.documentElement.classList.toggle('guide-hide-alerts', !show);
      }, !!scene.showAlerts);
      await sleep(scene.settle ?? 500);
      const hl = (scene.hl ?? []).map((h) => h.find);
      const rects = await measure(hl, where);
      await shoot(path.join(folder, `${scene.id}.png`));
      fs.writeFileSync(path.join(folder, `${scene.id}.json`), JSON.stringify({ rects }));
      n += 1;
    }
    for (const step of video.end ?? []) await act(step, `${video.id}/end`);
  }
} catch (e) {
  problems.push(`stopped: ${e.stack || e}`);
  process.exitCode = 1;
} finally {
  console.log(`${n} shots`);
  for (const p of problems) console.log('PROBLEM', p);
  if (errors.length) console.log('RENDERER', errors.slice(0, 5));
  await app.close().catch(() => {});
}
