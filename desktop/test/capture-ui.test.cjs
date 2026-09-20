'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createRequire } = require('node:module');
const { create } = require('../i18n.js');

function fixture(locale, response = 0) {
  const filename = path.join(__dirname, '..', 'capture.cjs'), localRequire = createRequire(filename);
  const dialogs = [], calls = [], folders = { png: '/tmp/PNG <fixture> {recording_folder}', webm: '/tmp/WebM <fixture>' };
  const api = { exports: {} };
  vm.runInNewContext(fs.readFileSync(filename, 'utf8'), {
    module: api, __dirname: path.dirname(filename),
    require: name => name === 'electron' ? {
      dialog: { showMessageBox: async (_win, options) => { dialogs.push(options); return { response }; } },
      shell: { openPath: async directory => { calls.push(directory); return ''; } },
    } : localRequire(name),
  });
  const capture = Object.create(api.exports.DesktopCapture.prototype);
  Object.assign(capture, { t: create(locale).t, win: {}, settings: { get: extension => folders[extension] },
    chooseDirectory: async extension => calls.push(extension),
    screenshot: kind => calls.push(kind), start: () => calls.push('start'), stop: () => calls.push('stop'),
    notify: async () => assert.fail('Unexpected capture error'),
  });
  return { capture, dialogs, calls, folders };
}

test('both languages preserve Capture Settings response indices and raw folder paths', async () => {
  for (const locale of ['en', 'zh-TW']) for (const response of [0, 1, 2, 3, 4]) {
    const f = fixture(locale, response);
    await f.capture.configure();
    const options = f.dialogs[0];
    assert.equal(options.title, f.capture.t('desktop.capture.settings_title'));
    assert.equal(options.defaultId, 0); assert.equal(options.cancelId, 0);
    assert.equal(options.buttons.length, 5);
    assert.equal(options.buttons[1], f.capture.t('desktop.capture.change_screenshot_folder'));
    assert.ok(options.detail.includes(f.folders.png));
    assert.ok(options.detail.includes(f.folders.webm));
    assert.deepEqual(f.calls, [[], ['png'], ['webm'], [f.folders.png], [f.folders.webm]][response]);
  }
});

test('translated capture menu labels keep fixed callbacks, IDs and accelerators', () => {
  for (const locale of ['en', 'zh-TW']) {
    const f = fixture(locale), menu = f.capture.menu();
    assert.equal(menu.label, f.capture.t('desktop.capture.menu'));
    assert.equal(menu.submenu[0].accelerator, 'CommandOrControl+Alt+S');
    assert.equal(menu.submenu[3].id, 'capture-start');
    assert.equal(menu.submenu[4].id, 'capture-stop');
    assert.equal(menu.submenu[4].accelerator, 'CommandOrControl+Alt+R');
    assert.equal(menu.submenu[5].id, 'capture-status');
    for (const item of menu.submenu) if (item.click) item.click();
    assert.deepEqual(f.calls, ['clipboard', 'file', 'start', 'stop']);
  }
});
