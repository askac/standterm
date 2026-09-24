'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { create } = require('../i18n.js');

function fixture({ locale = 'en', failure = true, notifyFailure = false, decision = null } = {}) {
  const filename = path.join(__dirname, '..', 'capture.cjs');
  const localRequire = createRequire(filename);
  const dialogs = [], notices = [], calls = [];
  const dialog = { showMessageBox: async (_win, options) => {
    dialogs.push(options);
    if (options.type === 'question') return decision ? decision() : { response: 1 };
    return { response: 0 };
  } };
  const context = { module: { exports: {} }, __dirname: path.dirname(filename),
    setTimeout, clearTimeout, setInterval, clearInterval,
    require: name => name === 'electron' ? { Menu: { getApplicationMenu: () => null }, dialog } : localRequire(name) };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context);
  const capture = Object.create(context.module.exports.DesktopCapture.prototype);
  const output = { partial: '/tmp/capture <fixture>.partial', destination: '/tmp/capture <fixture>.webm', bytes: 5,
    finish: async () => {
      calls.push('publish');
      if (failure) throw new Error('Fixture disk error');
      output.partial = null; return output.destination;
    }, close: async () => { calls.push('close-output'); } };
  const job = { startedAt: Date.now(), output, mimeType: 'video/webm',
    recorder: { isDestroyed: () => false, destroy: () => calls.push('destroy-recorder'),
      webContents: { executeJavaScript: async script => {
        calls.push(script);
        return script === 'window.recorder.drain()' ? { chunks: [] } : undefined;
      } } } };
  Object.assign(capture, { t: create(locale).t, state: 'recording', job,
    onChange: () => {}, notify: async (message, error) => {
      notices.push({ message, error });
      if (notifyFailure) throw new Error('Fixture notice unavailable');
    },
    win: { isDestroyed: () => false, isMinimized: () => true,
      restore: () => calls.push('restore'), show: () => calls.push('show'), focus: () => calls.push('focus') },
  });
  return { capture, job, output, dialogs, notices, calls, dialog };
}

test('failed save cancels this close or quit and leaves a readable error with retained paths', async () => {
  for (const locale of ['en', 'zh-TW']) for (const action of ['close', 'quit']) {
    const f = fixture({ locale });
    assert.equal(await f.capture.confirmStop(action), false);
    assert.equal(f.capture.state, 'idle');
    assert.equal(f.calls.filter(call => call === 'publish').length, 1);
    assert.equal(f.dialogs[0].defaultId, 0);
    assert.equal(f.dialogs[0].cancelId, 0);
    assert.equal(f.dialogs[0].message, f.capture.t(`desktop.capture.confirm_${action}`));
    const error = f.dialogs.at(-1);
    assert.equal(error.type, 'error');
    assert.ok(error.detail.includes('Fixture disk error'));
    assert.ok(error.detail.includes(f.output.partial));
    assert.ok(error.detail.includes(f.output.destination));
    assert.ok(f.calls.includes('show'));
    assert.equal(await f.capture.confirmStop(action), true, 'a later explicit exit is not permanently blocked');
  }
});

test('cancel keeps the recording untouched and a successful save permits closing', async () => {
  const canceled = fixture({ decision: async () => ({ response: 0 }) });
  assert.equal(await canceled.capture.confirmStop('close'), false);
  assert.equal(canceled.capture.state, 'recording');
  assert.deepEqual(canceled.calls, []);
  const saved = fixture({ failure: false });
  assert.equal(await saved.capture.confirmStop('quit'), true);
  assert.equal(saved.capture.state, 'idle');
  assert.equal(saved.dialogs.length, 1);
  assert.equal(saved.calls.filter(call => call === 'publish').length, 1);
});

test('failure while a confirmation is waiting cannot be lost when the job is cleared', async () => {
  let answer;
  const f = fixture({ decision: () => new Promise(resolve => { answer = resolve; }) });
  const confirming = f.capture.confirmStop('quit');
  assert.equal((await f.capture.stop()).error, true);
  assert.equal(f.capture.job, null);
  answer({ response: 1 });
  assert.equal(await confirming, false);
  assert.equal(f.calls.filter(call => call === 'publish').length, 1);
  assert.ok(f.dialogs.at(-1).detail.includes(f.output.partial));
});

test('stop retains startup failure and confirmation distinguishes canceled startup', async () => {
  const f = fixture();
  f.capture.state = 'idle'; f.capture.job = null;
  f.capture.chooseFile = async () => { throw new Error('Fixture startup error'); };
  const starting = f.capture.start();
  const [result, allowed] = await Promise.all([f.capture.stop(), f.capture.confirmStop('quit')]);
  assert.equal(result.error, true);
  assert.equal(result, await starting);
  assert.equal(result.partial, undefined);
  assert.equal(allowed, false);
  assert.ok(f.dialogs.at(-1).detail.includes('Fixture startup error'));
  const canceled = fixture();
  canceled.capture.state = 'idle'; canceled.capture.job = null;
  canceled.capture.chooseFile = async () => null;
  const canceling = canceled.capture.start();
  assert.equal(await canceled.capture.confirmStop('quit'), true);
  assert.equal(await canceling, null);
});

test('notification failure never changes the saved result or permits a failed-save exit', async () => {
  for (const failure of [false, true]) {
    const f = fixture({ failure, notifyFailure: true });
    assert.equal(await f.capture.confirmStop('quit'), !failure);
    assert.equal(f.calls.filter(call => call === 'publish').length, 1);
    if (failure) assert.ok(f.dialogs.at(-1).detail.includes('Fixture disk error'));
  }
});

test('an unavailable error dialog still cancels closing after a failed save', async () => {
  const f = fixture();
  f.dialog.showMessageBox = async (_win, options) => {
    if (options.type === 'question') return { response: 1 };
    throw new Error('Fixture dialog unavailable');
  };
  assert.equal(await f.capture.confirmStop('quit'), false);
  assert.equal(f.capture.state, 'idle');
});

test('concurrent stops share one publication and a stale confirmation never stops a new job', { timeout: 3000 }, async () => {
  const f = fixture({ failure: false });
  const [first, second] = await Promise.all([f.capture.stop(), f.capture.stop()]);
  assert.equal(first, second);
  assert.equal(f.calls.filter(call => call === 'publish').length, 1);
  let answer;
  const stale = fixture({ failure: false, decision: () => new Promise(resolve => { answer = resolve; }) });
  const confirming = stale.capture.confirmStop('close');
  assert.equal(await stale.capture.confirmStop('quit'), false);
  await stale.capture.stop();
  const replacement = {};
  stale.capture.job = replacement; stale.capture.state = 'recording';
  answer({ response: 1 });
  assert.equal(await confirming, false);
  assert.equal(stale.capture.job, replacement);
  assert.equal(stale.calls.filter(call => call === 'publish').length, 1);
});
