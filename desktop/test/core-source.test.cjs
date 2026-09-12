'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { EventEmitter } = require('node:events');
const { spawn } = require('node:child_process');
const { sourceStore, coreController } = require('../core-source.cjs');
const { stopOwnedBackend } = require('../backend-stop.cjs');

async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'standterm-source-test-'));
  const identity = `0.4.5:${'a'.repeat(64)}`;
  return { root, identity, store: sourceStore(root, identity) };
}

test('Core source defaults to bundled and consumes an intent once before execution', async () => {
  const { store } = await fixture();
  assert.equal((await store.read()).source, 'bundled');
  await store.queue('enable');
  assert.equal((await store.consume()).pending, 'enable');
  assert.equal((await store.consume()).pending, null);
  await assert.rejects(store.queue('git reset --hard'), /Invalid Core action/);
});

test('installer and identity resets clear Git selection and pending operations together', async () => {
  const { root, store } = await fixture();
  for (const action of ['enable', 'update']) {
    await store.select('git');
    await store.queue(action);
    const changed = sourceStore(root, `0.4.6:${'b'.repeat(64)}`);
    assert.equal((await changed.read()).source, 'bundled');
    assert.equal((await changed.consume()).pending, null);
    await store.select('git');
    await store.queue(action);
    await store.reset();
    assert.equal((await store.read()).source, 'bundled');
    assert.equal((await store.read()).pending, null);
  }
});

test('invalid or oversized preferences cannot introduce arbitrary paths or actions', async () => {
  const { root, identity, store } = await fixture();
  for (const value of [null, { version: 1, identity, source: 'git', pending: 'shell' }, 'x'.repeat(5000)]) {
    await fs.writeFile(path.join(root, 'core-source.json'), JSON.stringify(value));
    assert.equal((await store.read()).source, 'bundled');
    assert.equal((await store.read()).pending, null);
  }
});

test('Git setup failure preserves selection and offers explicit retry without a healthy Core', async () => {
  const { store } = await fixture();
  const actions = [];
  const restarts = [];
  await store.queue('enable');
  const controller = coreController({ store, prepareBundled: () => { throw new Error('unexpected bundled fallback'); },
    manage: async action => { actions.push(action); throw new Error('Git failed'); },
    dialog: { showMessageBox: async () => ({ response: 1 }) }, restart: action => restarts.push(action) });
  await assert.rejects(controller.prepare(), /Git failed/);
  assert.deepEqual(actions, ['enable']);
  assert.equal((await store.read()).pending, null);
  assert.equal(await controller.failure(new Error('Git failed')), 'restart');
  assert.deepEqual(restarts, ['enable']);
});

test('routing retains bundled checks and uses Git check without preparing dependencies on ordinary startup', async () => {
  const { store } = await fixture();
  const actions = [];
  const controller = coreController({ store, prepareBundled: async () => 'bundled',
    manage: async action => { actions.push(action); return action; } });
  assert.equal(await controller.prepare(), 'bundled');
  await store.queue('enable');
  assert.equal(await controller.prepare(), 'enable');
  assert.equal((await store.read()).source, 'git');
  assert.equal(await controller.prepare(), 'check');
  await store.queue('recover');
  assert.equal(await controller.prepare(), 'recover');
  assert.equal((await store.read()).source, 'bundled');
  assert.deepEqual(actions, ['enable', 'check', 'recover']);
});

test('missing Git leaves native recovery available and canceled confirmation queues nothing', async () => {
  const { store } = await fixture();
  let calls = 0;
  const controller = coreController({ store, manage: async () => ({ git_available: false, workspace: 'absent' }),
    restart: () => assert.fail('Canceled operation must not restart'),
    dialog: { showMessageBox: async options => {
      if (calls++ === 0) {
        assert.deepEqual(options.buttons, ['Close', 'Restore bundled Core', 'Open Desktop logs']);
        return { response: 1 };
      }
      assert.equal(options.defaultId, 0);
      return { response: 0 };
    } } });
  assert.equal(await controller.showManager(), false);
  assert.equal((await store.read()).pending, null);
});

function childFixture() {
  const child = new EventEmitter();
  child.exitCode = child.signalCode = null;
  child.stdin = { end: () => { child.ended = true; } };
  child.kill = () => { child.killed = true; };
  return child;
}

test('backend stop waits for actual exit after graceful EOF and forced termination', async () => {
  const child = childFixture();
  let stopped = false;
  const result = stopOwnedBackend(child, { gracefulMs: 5, terminateMs: 200 }).then(() => { stopped = true; });
  assert.equal(child.ended, true);
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(child.killed, true);
  assert.equal(stopped, false);
  child.emit('exit', 0);
  await result;
  assert.equal(stopped, true);
});

test('a backend that ignores termination prevents update and relaunch', async () => {
  const child = childFixture();
  await assert.rejects(stopOwnedBackend(child, { gracefulMs: 1, terminateMs: 5 }), /did not stop/);
  assert.equal(child.listenerCount('exit'), 0);
});

test('a failed spawn does not delay shutdown waiting for a nonexistent process', async () => {
  const { root } = await fixture();
  const child = spawn(path.join(root, 'missing-backend'), [], { stdio: 'pipe' });
  await new Promise(resolve => child.once('error', resolve));
  await stopOwnedBackend(child);
});
