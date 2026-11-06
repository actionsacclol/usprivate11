// Renderer-side unit tests for the visibility-aware poll scheduler.
//
//   npm run test:unit
//
// Node 24 strips TypeScript types natively, so this imports `shared/poll.ts`
// directly — no bundler, no test framework, no new dependency.
//
// The bug this guards against is invisible in the UI: the app hides to the
// tray instead of closing, so a poll that keeps its interval while hidden runs
// for hours against a backend nobody is watching. The other half is the one
// the user DOES see — coming back from the tray to numbers that are up to a
// full period old because the scheduler waited for the next tick.

import { strict as assert } from 'node:assert';
import test from 'node:test';

import { createPoll } from '../shared/poll.ts';

/** A fake clock: no real timers, and the interval bodies fire on demand. */
function harness({ hidden = false } = {}) {
  const state = { hidden, runs: 0, nextId: 1 };
  const timers = new Map();
  const deps = {
    setInterval: (fn, ms) => {
      const id = state.nextId++;
      timers.set(id, { fn, ms });
      return id;
    },
    clearInterval: (id) => { timers.delete(id); },
    isHidden: () => state.hidden,
  };
  const task = async () => { state.runs++; };
  return {
    state,
    timers,
    deps,
    task,
    /** Fire every armed interval once. */
    tick: () => { for (const t of [...timers.values()]) t.fn(); },
  };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

test('a visible poll arms exactly one interval at the requested period', () => {
  const h = harness();
  const p = createPoll(h.task, 60_000, h.deps);
  p.start();
  assert.equal(h.timers.size, 1);
  assert.equal([...h.timers.values()][0].ms, 60_000);
  assert.equal(p.isArmed(), true);
});

test('start() does not fire the task — the caller already did its initial load', () => {
  const h = harness();
  createPoll(h.task, 60_000, h.deps).start();
  assert.equal(h.state.runs, 0);
});

test('a poll started while hidden holds no timer at all', () => {
  const h = harness({ hidden: true });
  const p = createPoll(h.task, 8_000, h.deps);
  p.start();
  assert.equal(h.timers.size, 0, 'a hidden window must not keep polling');
  assert.equal(p.isArmed(), false);
});

test('going to the tray clears the interval', () => {
  const h = harness();
  const p = createPoll(h.task, 8_000, h.deps);
  p.start();
  h.state.hidden = true;
  p.onVisibilityChange();
  assert.equal(h.timers.size, 0);
});

test('coming back from the tray catches up immediately, then re-arms', async () => {
  const h = harness();
  const p = createPoll(h.task, 60_000, h.deps);
  p.start();
  h.state.hidden = true;
  p.onVisibilityChange();
  assert.equal(h.state.runs, 0);

  h.state.hidden = false;
  p.onVisibilityChange();
  await settle();
  assert.equal(h.state.runs, 1, 'the user must not stare at a stale screen');
  assert.equal(h.timers.size, 1, 'the interval must come back');
});

test('a repeated visible event does not stack intervals or re-fire', async () => {
  const h = harness();
  const p = createPoll(h.task, 60_000, h.deps);
  p.start();
  h.state.hidden = true;
  p.onVisibilityChange();
  h.state.hidden = false;
  p.onVisibilityChange();
  p.onVisibilityChange();
  p.onVisibilityChange();
  await settle();
  assert.equal(h.timers.size, 1, 'each visibilitychange armed another timer');
  assert.equal(h.state.runs, 1, 'each visibilitychange re-ran the task');
});

test('a stalled task never stacks: ticks during a run are dropped', async () => {
  const h = harness();
  let release;
  let calls = 0;
  const blocked = new Promise((r) => { release = r; });
  const p = createPoll(() => { calls++; return blocked; }, 1_000, h.deps);
  p.start();

  p.runNow();
  await settle();
  p.runNow();
  h.tick();
  h.tick();
  await settle();
  assert.equal(calls, 1, 'polls stacked up behind a stalled backend');

  // Once it finally answers, the next tick is served normally.
  release();
  await settle();
  h.tick();
  await settle();
  assert.equal(calls, 2);
});

test('a rejected task does not wedge the in-flight guard', async () => {
  const h = harness();
  let calls = 0;
  const p = createPoll(async () => { calls++; throw new Error('backend down'); },
    1_000, h.deps);
  p.start();
  p.runNow();
  await settle();
  p.runNow();
  await settle();
  assert.equal(calls, 2, 'a failed poll must not block every later poll');
});

test('stop() is final and idempotent', async () => {
  const h = harness();
  const p = createPoll(h.task, 1_000, h.deps);
  p.start();
  p.stop();
  p.stop();
  assert.equal(h.timers.size, 0);
  p.runNow();
  p.onVisibilityChange();
  await settle();
  assert.equal(h.state.runs, 0);
  assert.equal(h.timers.size, 0, 'a stopped poll re-armed itself');
});
