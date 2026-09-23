'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { EventEmitter } = require('node:events');
const { loadWindowState, trackWindowState } = require('../window-state.cjs');

const primary = { x: 0, y: 0, width: 1920, height: 1040 };
function fixture(area = primary) {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-window-state-')), 'window-wsl.json');
  const screen = { getPrimaryDisplay: () => ({ workArea: primary }), getDisplayMatching: () => ({ workArea: area }) };
  const write = (bounds, maximized = true) => fs.writeFileSync(file, JSON.stringify({ version: 1, bounds, maximized }));
  return { file, screen, write };
}

test('restores maximization and normal bounds on an attached secondary display', () => {
  const f = fixture({ x: -1600, y: 0, width: 1600, height: 900 });
  f.write({ x: -1400, y: 30, width: 1000, height: 700 });
  assert.deepEqual(loadWindowState(f.file, f.screen), {
    options: { x: -1400, y: 30, width: 1000, height: 700, minWidth: 640, minHeight: 480 }, maximized: true,
  });
});

test('removed displays and changed work areas keep the entire normal window reachable', () => {
  const f = fixture();
  f.write({ x: -2400, y: -100, width: 2200, height: 1200 });
  assert.deepEqual(loadWindowState(f.file, f.screen).options, { ...primary, minWidth: 640, minHeight: 480 });
  f.write({ x: 1850, y: 1000, width: 800, height: 600 });
  assert.deepEqual(loadWindowState(f.file, f.screen).options, {
    x: 1120, y: 440, width: 800, height: 600, minWidth: 640, minHeight: 480,
  });
});

test('missing, oversized, malformed and unsupported state falls back without blocking startup', () => {
  const f = fixture();
  const defaults = loadWindowState(f.file, f.screen);
  for (const value of ['null', 'broken', '{"version":2}', JSON.stringify({ version: 1,
    bounds: { x: Number.MAX_SAFE_INTEGER, y: 0, width: 1000, height: 700 }, maximized: true }),
    ' '.repeat(16385)]) {
    fs.writeFileSync(f.file, value);
    assert.deepEqual(loadWindowState(f.file, f.screen), defaults);
  }
});

function fakeWindow(bounds) {
  const win = new EventEmitter();
  Object.assign(win, { visible: true, minimized: false, maximized: false, fullscreen: false, bounds });
  win.isDestroyed = () => false;
  win.isVisible = () => win.visible;
  win.isMinimized = () => win.minimized;
  win.isMaximized = () => win.maximized;
  win.isFullScreen = () => win.fullscreen;
  win.getNormalBounds = () => win.bounds;
  return win;
}

test('exit flushes normal bounds and preserves maximization across minimize, tray hide and fullscreen', () => {
  for (const state of ['minimized', 'hidden', 'fullscreen']) {
    const f = fixture(), initial = loadWindowState(f.file, f.screen);
    const bounds = { x: 80, y: 60, width: 900, height: 650 };
    const win = fakeWindow(bounds);
    const tracker = trackWindowState(win, f.file, initial);
    win.emit('move');
    win.maximized = true;
    win.emit('maximize');
    win[state === 'hidden' ? 'visible' : state] = state !== 'hidden';
    win.maximized = false;
    win.bounds = { x: -32000, y: -32000, width: 1, height: 1 };
    win.emit('resize');
    tracker.save();
    assert.deepEqual(JSON.parse(fs.readFileSync(f.file)), { version: 1, bounds, maximized: true });
    win.emit('closed');
  }
});

test('close to tray saves restored bounds and an unwritable cache does not block closing', () => {
  const f = fixture(), initial = loadWindowState(f.file, f.screen);
  const win = fakeWindow({ x: 100, y: 70, width: 800, height: 600 });
  trackWindowState(win, f.file, initial);
  win.emit('close');
  assert.equal(loadWindowState(f.file, f.screen).maximized, false);
  assert.equal(loadWindowState(f.file, f.screen).options.width, 800);
  const other = fixture();
  fs.mkdirSync(other.file);
  let failures = 0;
  trackWindowState(win, other.file, initial, () => failures++);
  assert.doesNotThrow(() => win.emit('close'));
  assert.equal(failures, 1);
  assert.deepEqual(fs.readdirSync(path.dirname(other.file)), ['window-wsl.json']);
  win.emit('closed');
});

test('native rounding does not accumulate in the saved dimensions across launches', () => {
  const f = fixture();
  const bounds = { x: 100, y: 70, width: 800, height: 600 };
  f.write(bounds, false);
  for (let launch = 0; launch < 3; launch++) {
    const initial = loadWindowState(f.file, f.screen);
    const win = fakeWindow({ ...bounds, height: 601 });
    trackWindowState(win, f.file, initial);
    win.emit('resize'); win.emit('close'); win.emit('closed');
    assert.deepEqual(JSON.parse(fs.readFileSync(f.file)).bounds, bounds);
  }
});
