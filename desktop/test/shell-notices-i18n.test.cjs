'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { create } = require('../i18n.js');

// Run the actual main-process callbacks without launching Core or a native window.
const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
const portOptions = source.match(/  const handoff = await startWithPort\(([\s\S]*?\n  })\);/)[1];
const downloadRegistration = source.match(/  installFloatingWindows\(win,[\s\S]*?\n  }\);/)[0];

test('localized port prompts preserve numeric choices and structured reasons', async () => {
  for (const locale of ['en', 'zh-TW']) for (const response of [0, 1, 2]) {
    const { t } = create(locale);
    const dialogs = [];
    const options = vm.runInNewContext('(' + portOptions + ')', {
      settingsPath: 'fixture', t, mode: 'windows', MODES: { windows: 'Windows' },
      process: { platform: 'linux' }, smoke: false,
      launchBackend: () => assert.fail('Unexpected launch'), prepared: null,
      verifyBackend: () => assert.fail('Unexpected verify'), stopBackend: () => assert.fail('Unexpected stop'),
      diagnostics: { write() {} },
      dialog: { showMessageBox: async options => { dialogs.push(options); return { response }; } },
    });
    assert.equal(options.t, t);
    for (const [reason, key] of [[undefined, 'confirm_in_use'], ['host_permission_denied', 'confirm_permission'],
      ['host_address_in_use', 'confirm_host_in_use'], ['is reserved or denied by Windows', 'confirm_in_use']]) {
      assert.equal(await options.confirm(51000, 51001, reason), ['cancel', 'once', 'remember'][response]);
      const notice = dialogs.at(-1);
      assert.equal(notice.message, t(`desktop.port.${key}`, { port: 51000, candidate: 51001 }));
      assert.deepEqual(Array.from(notice.buttons), [t('desktop.common.cancel'), t('desktop.port.use_once'), t('desktop.port.use_and_remember')]);
      assert.equal(notice.defaultId, 0);
      assert.equal(notice.cancelId, 0);
    }
  }
});

function downloadFixture(locale, response, reject = false) {
  const { t } = create(locale);
  const dialogs = [], reveals = [], events = [];
  const owner = {};
  let callback;
  vm.runInNewContext(downloadRegistration, {
    t, win: {}, handoff: { origin: 'http://127.0.0.1:51000' }, openExternal() {}, contents: {},
    installFloatingWindows: (_win, _origin, _open, _contents, done) => { callback = done; },
    dialog: { showMessageBox: async (target, options) => {
      assert.equal(target, owner);
      dialogs.push(options);
      if (reject) throw new Error('Private dialog failure');
      return { response };
    } },
    shell: { showItemInFolder: value => reveals.push(value) },
    diagnostics: { write: value => events.push(value) },
  });
  return { t, dialogs, reveals, events,
    send: async result => { callback(result, owner); await new Promise(setImmediate); } };
}

test('download notices reveal only a confirmed completed native path in both languages', async () => {
  const rawPath = 'C:\\Downloads\\<file> {path}.txt';
  for (const locale of ['en', 'zh-TW']) for (const response of [0, 1]) {
    for (const result of [{ state: 'completed', path: rawPath }, { state: 'completed', path: '' },
      { state: 'interrupted', path: rawPath }]) {
      const f = downloadFixture(locale, response);
      await f.send(result);
      const completed = result.state === 'completed' && !!result.path;
      const notice = f.dialogs[0];
      assert.equal(notice.type, completed ? 'info' : 'warning');
      assert.equal(notice.message, f.t(completed ? 'desktop.download.complete' : 'desktop.download.incomplete'));
      assert.equal(notice.detail, f.t(completed ? 'desktop.download.saved_to' : 'desktop.download.retry', { path: rawPath }));
      assert.equal(notice.buttons.length, completed ? 2 : 1);
      assert.equal(notice.cancelId, 0);
      assert.equal(notice.defaultId, 0);
      assert.deepEqual(f.reveals, completed && response === 1 ? [rawPath] : []);
      assert.deepEqual(f.events, []);
    }
  }
});

test('download notification failure neither reveals a path nor retries the operation', async () => {
  const f = downloadFixture('zh-TW', 1, true);
  await f.send({ state: 'completed', path: 'C:\\Downloads\\secret.txt' });
  assert.equal(f.dialogs.length, 1);
  assert.deepEqual(f.reveals, []);
  assert.deepEqual(f.events, ['download_notice_failed']);
});
