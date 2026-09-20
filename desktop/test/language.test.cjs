'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createLanguage } = require('../language.cjs');
const { create } = require('../i18n.js');

const owner = { isDestroyed: () => false };
function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-language-'));
  return { directory, file: path.join(directory, 'language.json') };
}
function dialog(response) {
  const calls = [];
  return { calls, showMessageBox: async (_win, options) => { calls.push(options); return { response }; } };
}

test('language persists per profile and takes effect only for a new instance', async () => {
  const f = fixture(), original = createLanguage(f.file), prompt = dialog(2);
  assert.equal(original.locale, 'en');
  assert.equal(await original.choose(prompt, owner), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(f.file, 'utf8')), { version: 1, locale: 'zh-TW' });
  assert.equal(original.locale, 'en');
  assert.equal(original.t('desktop.menu.settings'), 'Settings...');
  const reloaded = createLanguage(f.file);
  assert.equal(reloaded.locale, 'zh-TW');
  assert.equal(reloaded.t('desktop.menu.settings'), create('zh-TW').t('desktop.menu.settings'));
  assert.equal(createLanguage(path.join(f.directory, 'other-profile', 'language.json')).locale, 'en');
  const english = dialog(1);
  assert.equal(await reloaded.choose(english, owner), true);
  assert.equal(english.calls[0].buttons[0], reloaded.t('desktop.common.cancel'));
  assert.equal(createLanguage(f.file).locale, 'en');
  assert.deepEqual(fs.readdirSync(f.directory), ['language.json']);
});

test('missing, malformed and unsupported preferences fall back to English', () => {
  const f = fixture();
  assert.equal(createLanguage(f.file).locale, 'en');
  for (const value of ['invalid', 'null', '{"version":2,"locale":"zh-TW"}',
    '{"version":1,"locale":"zh-CN"}', '{"version":1,"locale":{}}',
    '{"version":1,"locale":"zh-TW","padding":"' + 'x'.repeat(16384) + '"}']) {
    fs.writeFileSync(f.file, value);
    assert.equal(createLanguage(f.file).locale, 'en');
  }
});

test('cancel, unknown responses and selecting the current language do not write', async () => {
  const f = fixture(), language = createLanguage(f.file);
  for (const response of [0, -1, 3, '2', undefined, 1]) {
    const prompt = dialog(response);
    assert.equal(await language.choose(prompt, owner), response === 1);
    assert.equal(prompt.calls[0].defaultId, 0);
    assert.equal(prompt.calls[0].cancelId, 0);
    assert.equal(fs.existsSync(f.file), false);
  }
});

test('duplicate and stale language dialogs cannot write a selection', async () => {
  const f = fixture(), language = createLanguage(f.file);
  let resolve, destroyed = false, calls = 0;
  const win = { isDestroyed: () => destroyed };
  const prompt = { showMessageBox: () => { calls++; return new Promise(done => { resolve = done; }); } };
  const pending = language.choose(prompt, win);
  assert.equal(await language.choose(prompt, win), false);
  destroyed = true;
  resolve({ response: 2 });
  assert.equal(await pending, false);
  assert.equal(calls, 1);
  assert.equal(fs.existsSync(f.file), false);
  assert.equal(await language.choose(prompt, win), false);
});

test('failed persistence leaves the saved choice and active language unchanged', async () => {
  const f = fixture(), language = createLanguage(f.file), prompt = dialog(2);
  fs.mkdirSync(f.file);
  assert.equal(await language.choose(prompt, owner), false);
  assert.equal(language.locale, 'en');
  assert.equal(prompt.calls.at(-1).type, 'error');
  assert.equal(prompt.calls.at(-1).message, language.t('desktop.language.failed'));
  assert.deepEqual(fs.readdirSync(f.directory), ['language.json']);
  const check = dialog(0);
  await language.choose(check, owner);
  assert.match(check.calls[0].detail, /Saved choice: English/);
});

test('a lost save notification does not report rollback or discard the persisted choice', async () => {
  const f = fixture(), language = createLanguage(f.file);
  let calls = 0;
  const prompt = { showMessageBox: async () => {
    if (++calls > 1) throw new Error('Window closed');
    return { response: 2 };
  } };
  assert.equal(await language.choose(prompt, owner), false);
  assert.equal(createLanguage(f.file).locale, 'zh-TW');
  const check = dialog(0);
  await language.choose(check, owner);
  assert.equal(check.calls[0].detail, language.t('desktop.language.detail', { language: language.t('desktop.language.name_zh_tw') }));
});
