'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');

function fixture() {
  const filename = path.join(__dirname, '..', 'capture.cjs');
  const localRequire = createRequire(filename);
  let now = 13000, status, diagnostic, message;
  class Clock extends Date { static now() { return now; } }
  const context = { module: { exports: {} }, __dirname: path.dirname(filename), Date: Clock,
    setTimeout, clearTimeout,
    require: name => name === 'electron' ? { Menu: { getApplicationMenu: () => null } } : localRequire(name) };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context);
  const capture = Object.create(context.module.exports.DesktopCapture.prototype);
  Object.assign(capture, {
    state: 'recording', job: { startedAt: 1000 },
    onChange: (_title, state) => { status = state; },
    onDiagnostic: state => { diagnostic = state; },
    notify: async value => { message = value; },
    win: { isDestroyed: () => false, isVisible: () => true, isMinimized: () => false,
      isFocused: () => true, getContentSize: () => [624, 561] },
    contents: { capturePage: async () => { throw new Error('Native capture failed: sensitive fixture'); } },
  });
  return { capture, advance: ms => { now += ms; }, now: () => now,
    status: () => status, diagnostic: () => diagnostic, message: () => message };
}

test('recording time advances, freezes while paused, and excludes paused intervals', () => {
  const f = fixture();
  const c = f.capture;
  c.update(); assert.equal(f.status().label, 'Recording 00:12');
  c.state = 'paused'; c.job.pausedAt = f.now();
  f.advance(10000); c.update(); assert.equal(f.status().label, 'Recording paused 00:12');
  c.state = 'recording'; c.job.pausedMs = 10000; c.job.pausedAt = null;
  f.advance(3000); c.update(); assert.equal(f.status().label, 'Recording 00:15');
  f.advance(60000); c.update(); assert.equal(f.status().label, 'Recording 01:15');
  c.state = 'idle'; c.job = null; c.update(); assert.equal(f.status().label, 'Not recording');
});

test('failed capture reports only window metadata and releases the screenshot action', async () => {
  const f = fixture();
  assert.equal((await f.capture.screenshot('file')).error, true);
  assert.deepEqual(JSON.parse(JSON.stringify(f.diagnostic())), {
    width: 624, height: 561, visible: true, minimized: false, focused: true,
  });
  assert.match(f.message(), /foreground and retry/);
  assert.ok(!f.message().includes('sensitive fixture'));
  assert.equal(f.capture.screenshotBusy, false);
});
