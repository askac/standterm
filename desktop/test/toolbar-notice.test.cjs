'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture() {
  const elements = new Map();
  for (const [, id] of fs.readFileSync(path.join(__dirname, '..', 'toolbar.html'), 'utf8').matchAll(/id="([^"]+)"/g)) {
    const classes = new Set();
    elements.set(id, { textContent: '', title: '', hidden: false, dataset: {},
      classList: { add: name => classes.add(name), remove: name => classes.delete(name),
        contains: name => classes.has(name), toggle: (name, on) => on ? classes.add(name) : classes.delete(name) },
      setAttribute: () => {}, addEventListener: (_type, handler) => { elements.get(id).click = handler; } });
  }
  const timers = new Map();
  let time = 0, next = 0, receive, fail = false;
  const source = fs.readFileSync(path.join(__dirname, '..', 'toolbar.js'), 'utf8');
  vm.runInNewContext(source, {
    document: { getElementById: id => elements.get(id), querySelectorAll: () => [elements.get('save')] },
    window: { desktopToolbar: { onState: callback => { receive = callback; },
      invoke: () => fail ? Promise.reject(new Error('Fixture failure')) : Promise.resolve(true) } },
    setTimeout: (callback, delay) => { timers.set(++next, { callback, at: time + delay }); return next; },
    clearTimeout: id => timers.delete(id),
  });
  return { element: id => elements.get(id), receive, fail: () => { fail = true; },
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

test('failed toolbar actions use the same timed error presentation', async () => {
  const f = fixture(); f.fail(); f.element('save').click(); await Promise.resolve();
  assert.match(f.element('notice').textContent, /Action unavailable/);
  assert.ok(f.element('notice-area').classList.contains('error'));
  f.tick(10350); assert.equal(f.element('notice').textContent, '');
});

test('macOS keeps the system menu layout while notices use the shared renderer', () => {
  const f = fixture(); f.receive({ mac: true, notice: 'Copied.', noticeId: 1 });
  assert.equal(f.element('menus').hidden, true);
  assert.equal(f.element('mac-title').hidden, false);
  assert.equal(f.element('notice').textContent, 'Copied.');
  f.tick(5350); assert.equal(f.element('notice').textContent, '');
  f.receive({ mac: false });
  assert.equal(f.element('menus').hidden, false);
});
