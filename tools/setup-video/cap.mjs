// node cap.mjs <steps.json> — one CDP session at a fixed 1600x900 viewport (2x pixels).
//  {nav:"AI Agents"} sidebar   {click:"Button text"} exact   {clickHas:"substr", sel?:"button"}
//  {eval:"js"}  {evalFile:"rects.js"}  {wait:ms}  {shot:"out.png", clip?:[x,y,w,h]}  {scroll:"css selector or text", y?:n}
//  {burst:"prefix", n:frames, every:ms}  numbered frames for motion clips
import { readFileSync, writeFileSync } from 'node:fs';
const PORT = process.env.CDP_PORT || '9344';
const W = Number(process.env.VW || 1600), H = Number(process.env.VH || 900), DSF = Number(process.env.DSF || 1.2);
const steps = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
const page = targets.find((t) => t.type === 'page' && !t.url.startsWith('devtools'));
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener('open', r, { once: true }));
let id = 0; const pending = new Map();
ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
const send = (method, params = {}) => new Promise((res) => { const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const evalJs = async (expr) => { const r = (await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true })).result; return r?.exceptionDetails ? 'EXC ' + (r.exceptionDetails.exception?.description || '').slice(0, 300) : r?.result?.value; };
await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
await sleep(400);
const shot = async (out, clip) => {
  const p = { format: 'png', captureBeyondViewport: false };
  if (clip) p.clip = { x: clip[0], y: clip[1], width: clip[2], height: clip[3], scale: 1 };
  const r = await send('Page.captureScreenshot', p);
  writeFileSync(out, Buffer.from(r.result.data, 'base64'));
};
const KEYS = { left: ['ArrowLeft', 'ArrowLeft', 37], right: ['ArrowRight', 'ArrowRight', 39], e: ['KeyE', 'e', 69], w: ['KeyW', 'w', 87], a: ['KeyA', 'a', 65], s: ['KeyS', 's', 83], d: ['KeyD', 'd', 68], one: ['Digit1', '1', 49], two: ['Digit2', '2', 50], three: ['Digit3', '3', 51], esc: ['Escape', 'Escape', 27] };
const hold = async (spec, ms) => {
  const ks = spec.split('+').map((k) => KEYS[k]);
  for (const [code, key, kc] of ks) await send('Input.dispatchKeyEvent', { type: 'rawKeyDown', code, key, windowsVirtualKeyCode: kc });
  await sleep(ms);
  for (const [code, key, kc] of ks.reverse()) await send('Input.dispatchKeyEvent', { type: 'keyUp', code, key, windowsVirtualKeyCode: kc });
};
// Screencast: frames at the renderer's own pace, each saved with its timestamp.
const frames = [];
ws.addEventListener('message', (ev) => {
  const m = JSON.parse(ev.data);
  if (m.method === 'Page.screencastFrame' && cast) {
    const n = cast.n++;
    const f = `${cast.prefix}${String(n).padStart(5, '0')}.jpg`;
    writeFileSync(f, Buffer.from(m.params.data, 'base64'));
    cast.times.push([f, m.params.metadata.timestamp]);
    send('Page.screencastFrameAck', { sessionId: m.params.sessionId });
  }
});
let cast = null;
const runSub = async (s) => {
  if (s.hold) await hold(s.hold, s.ms);
  if (s.click) await evalJs(`(() => { const b=[...document.querySelectorAll('button')].find(x=>x.innerText.trim()===${JSON.stringify(s.click)}); b && b.click(); })()`);
  if (s.eval) console.log('eval', JSON.stringify(await evalJs(s.eval)));
  if (s.wait) await sleep(s.wait);
};
for (const s of steps) {
  if (s.cast) {
    cast = { prefix: s.cast, n: 0, times: [] };
    await send('Page.startScreencast', { format: 'jpeg', quality: 92, maxWidth: 1920, maxHeight: 1080, everyNthFrame: 1 });
    for (const sub of s.steps || []) await runSub(sub);
    await send('Page.stopScreencast');
    await sleep(300);
    writeFileSync(`${s.cast}times.json`, JSON.stringify(cast.times));
    console.log('cast', s.cast, cast.n, 'frames over', ((cast.times.at(-1)?.[1] - cast.times[0]?.[1]) || 0).toFixed(2), 's');
    cast = null;
    continue;
  }
  if (s.hold) await hold(s.hold, s.ms);
  if (s.nav) console.log('nav', s.nav, await evalJs(`(() => { const b=[...document.querySelectorAll('aside button')].find(x=>x.textContent.trim()===${JSON.stringify(s.nav)}); if(!b) return 'missing'; b.click(); return 'ok'; })()`));
  if (s.click) console.log('click', s.click, await evalJs(`(() => { const b=[...document.querySelectorAll('button,[role=button],label')].find(x=>x.innerText.trim()===${JSON.stringify(s.click)}); if(!b) return 'missing'; b.click(); return 'ok'; })()`));
  if (s.clickHas) console.log('clickHas', s.clickHas, await evalJs(`(() => { const b=[...document.querySelectorAll(${JSON.stringify(s.sel || 'button')})].filter(x=>x.innerText.includes(${JSON.stringify(s.clickHas)})).pop(); if(!b) return 'missing'; b.click(); return 'ok'; })()`));
  if (s.scroll) console.log('scroll', await evalJs(`(() => { const t=${JSON.stringify(s.scroll)}; const el=[...document.querySelectorAll('h1,h2,h3,h4,div,span,p,button')].find(x=>x.childElementCount<4 && x.innerText && x.innerText.trim().startsWith(t)); if(!el) return 'missing'; el.scrollIntoView({block:'start'}); const m=document.querySelector('main'); ${s.y ? `(el.closest('main')||m||document.scrollingElement).scrollBy(0, ${s.y});` : ''} return 'ok'; })()`));
  if (s.evalFile) console.log('evalFile', JSON.stringify(await evalJs(readFileSync(s.evalFile, 'utf8'))));
  if (s.eval) console.log('eval', JSON.stringify(await evalJs(s.eval)));
  if (s.wait) await sleep(s.wait);
  if (s.shot) { await shot(s.shot, s.clip); console.log('shot', s.shot); }
  if (s.burst) { for (let i = 0; i < s.n; i++) { const t0 = Date.now(); await shot(`${s.burst}${String(i).padStart(4, '0')}.png`); await sleep(Math.max(0, s.every - (Date.now() - t0))); } console.log('burst', s.burst, s.n); }
}
ws.close();
