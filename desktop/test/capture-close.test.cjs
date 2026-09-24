'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');

// Exercise the actual close handler without starting Core or an Electron window.
const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
const registration = source.match(/win\.on\('close', event => \{[\s\S]*?\n  \}\);/)[0];

function fixture() {
  const win = new EventEmitter(), calls = [];
  let resolve;
  const capture = { active: true, confirming: false, confirmStop: action => {
    calls.push(action); capture.confirming = true;
    return new Promise(done => { resolve = value => { capture.confirming = false; done(value); }; });
  } };
  const context = vm.createContext({ win, capture, closePending: false, quitting: false, tray: false, smoke: false });
  let closed = 0, hidden = 0;
  const close = () => {
    let prevented = false;
    win.emit('close', { preventDefault: () => { prevented = true; } });
    if (!prevented) closed++;
    return prevented;
  };
  win.close = close; win.hide = () => hidden++;
  vm.runInContext(registration, context);
  return { capture, context, calls, close, resolve: value => resolve(value),
    closed: () => closed, hidden: () => hidden, settle: () => new Promise(setImmediate) };
}

test('clearing the recording job cannot bypass a pending close failure notice', async () => {
  const f = fixture();
  assert.equal(f.close(), true);
  f.capture.active = false;
  assert.equal(f.close(), true);
  assert.deepEqual(f.calls, ['close']);
  assert.equal(f.closed(), 0);
  f.resolve(false); await f.settle();
  assert.equal(f.closed(), 0);
  assert.equal(f.context.closePending, false);
  assert.equal(f.close(), false, 'a later explicit close remains available');
});

test('closing during a quit error notice neither hides nor destroys the window', () => {
  const f = fixture();
  f.capture.active = false; f.capture.confirming = true;
  f.context.quitting = true; f.context.tray = true;
  assert.equal(f.close(), true);
  assert.equal(f.closed(), 0);
  assert.equal(f.hidden(), 0);
  assert.deepEqual(f.calls, []);
});

test('a confirmed save permits exactly one subsequent close', async () => {
  const f = fixture();
  assert.equal(f.close(), true);
  f.capture.active = false;
  f.resolve(true); await f.settle();
  assert.equal(f.closed(), 1);
  assert.equal(f.context.closePending, false);
});
