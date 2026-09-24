'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(action = 'screenshot-file') {
  const elements = new Map();
  for (const [, id] of fs.readFileSync(path.join(__dirname, '..', 'toolbar.html'), 'utf8').matchAll(/id="([^"]+)"/g)) {
    const classes = new Set();
    elements.set(id, { textContent: '', title: '', hidden: false, dataset: {},
      classList: { add: name => classes.add(name), remove: name => classes.delete(name),
        contains: name => classes.has(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name) },
      setAttribute: () => {}, addEventListener: (_type, handler) => { elements.get(id).click = handler; } });
  }
  elements.get('save').dataset = action.startsWith('menu:') ? { menu: action.slice(5) } : { action };
  const timers = new Map();
  const calls = [];
  let time = 0, next = 0, receive, fail = false, result = true;
  const source = fs.readFileSync(path.join(__dirname, '..', 'toolbar.js'), 'utf8');
  vm.runInNewContext(source, {
    document: { documentElement: {}, getElementById: id => elements.get(id),
      querySelectorAll: selector => selector === '[data-action], [data-menu]' ? [elements.get('save')] : [] },
    window: { StandTermDesktopI18n: require('../i18n.js'), desktopToolbar: { onState: callback => { receive = callback; },
      invoke: action => {
        calls.push(action);
        return fail ? Promise.reject(new Error('Fixture failure')) : Promise.resolve(result);
      } } },
    setTimeout: (callback, delay) => { timers.set(++next, { callback, at: time + delay }); return next; },
    clearTimeout: id => timers.delete(id),
  });
  return { element: id => elements.get(id), receive, calls, fail: () => { fail = true; },
    result: value => { result = value; },
    tick: ms => {
      const end = time + ms;
      for (;;) {
        const entry = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!entry || entry[1].at > end) break;
        time = entry[1].at; timers.delete(entry[0]); entry[1].callback();
      }
      time = end;
    } };
}

test('notice fades after five seconds and recording updates never replay it', () => {
  const f = fixture();
  const state = { notice: 'Screenshot saved.', noticeId: 1 };
  f.receive(state);
  assert.equal(f.element('notice').textContent, state.notice);
  assert.ok(f.element('notice-area').classList.contains('visible'));
  f.tick(4900); f.receive({ ...state, state: 'recording', label: 'Recording 00:04' });
  f.tick(100);
  assert.equal(f.element('notice-area').classList.contains('visible'), false);
  f.tick(350);
  assert.equal(f.element('notice').textContent, '');
  assert.equal(f.element('notice-area').title, '');
  f.receive(state);
  assert.equal(f.element('notice').textContent, '');
});

test('a new error cancels pending fade cleanup and remains visible for ten seconds', () => {
  const f = fixture();
  f.receive({ notice: 'Saved.', noticeId: 1 }); f.tick(5100);
  f.receive({ notice: 'Could not save.', noticeId: 2, error: true }); f.tick(300);
  assert.equal(f.element('notice').textContent, 'Could not save.');
  assert.ok(f.element('notice-area').classList.contains('error'));
  f.tick(9600); assert.ok(f.element('notice-area').classList.contains('visible'));
  f.tick(100); assert.equal(f.element('notice-area').classList.contains('visible'), false);
});

test('explicitly rejected toolbar actions show an error without replaying the action', async () => {
  for (const action of ['screenshot-file', 'menu:standterm']) {
    const f = fixture(action); f.result(false); f.element('save').click();
    await new Promise(setImmediate);
    assert.equal(f.element('notice').textContent, 'Action unavailable in the current window state.');
    assert.ok(f.element('notice-area').classList.contains('error'));
    f.tick(10350); assert.equal(f.element('notice').textContent, '');
    assert.deepEqual(f.calls, ['ready', action]);
  }
});

test('failed toolbar invocations report an unknown result without replaying the action', async () => {
  for (const action of ['screenshot-file', 'menu:standterm']) {
    const f = fixture(action); f.fail(); f.element('save').click();
    await new Promise(setImmediate);
    assert.equal(f.element('notice').textContent, 'Could not confirm the action result. Check the current state.');
    assert.ok(f.element('notice-area').classList.contains('error'));
    f.tick(10350); assert.equal(f.element('notice').textContent, '');
    assert.deepEqual(f.calls, ['ready', action]);
  }
});

test('other toolbar results preserve operation feedback without inventing success', async () => {
  for (const result of [true, undefined, null, 0, '']) {
    const f = fixture(); f.result(result); f.element('save').click();
    f.receive({ notice: 'Could not save.', noticeId: 1, error: true });
    await new Promise(setImmediate);
    assert.equal(f.element('notice').textContent, 'Could not save.');
    assert.ok(f.element('notice-area').classList.contains('error'));
    assert.deepEqual(f.calls, ['ready', 'screenshot-file']);
  }
});

test('accepted requests leave completion notices to the operation', async () => {
  const f = fixture(); f.element('save').click();
  await new Promise(setImmediate);
  assert.equal(f.element('notice').textContent, '');
  f.receive({ notice: 'Screenshot saved.', noticeId: 1 });
  f.element('save').click();
  await new Promise(setImmediate);
  assert.equal(f.element('notice').textContent, 'Screenshot saved.');
  assert.deepEqual(f.calls, ['ready', 'screenshot-file', 'screenshot-file']);
});

test('macOS keeps the system menu layout while notices use the shared renderer', () => {
  const f = fixture(); f.receive({ mac: true, notice: 'Copied.', noticeId: 1 });
  assert.equal(f.element('menus').hidden, true);
  assert.equal(f.element('mac-title'), undefined);
  assert.equal(f.element('notice').textContent, 'Copied.');
  f.tick(5350); assert.equal(f.element('notice').textContent, '');
  f.receive({ mac: false });
  assert.equal(f.element('menus').hidden, false);
});
