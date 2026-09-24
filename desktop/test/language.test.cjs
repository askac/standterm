'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createLanguage } = require('../language.cjs');
const { create } = require('../i18n.js');

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-language-'));
  return { directory, file: path.join(directory, 'language.json') };
}

test('Core language updates existing translators and the next-launch cache per profile', () => {
  const f = fixture(), language = createLanguage(f.file), { t } = language;
  assert.equal(language.locale, 'en');
  for (const locale of ['zh-TW', 'en']) {
    assert.deepEqual(language.sync(locale), { changed: true, persisted: true });
    assert.equal(language.locale, locale);
    assert.equal(t('desktop.menu.settings'), create(locale).t('desktop.menu.settings'));
    assert.equal(createLanguage(f.file).locale, locale);
  }
  assert.equal(createLanguage(path.join(f.directory, 'other-profile/language.json')).locale, 'en');
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

test('invalid and older-Core values leave the active language and cache untouched', () => {
  const f = fixture(), language = createLanguage(f.file);
  language.sync('zh-TW');
  const before = fs.readFileSync(f.file, 'utf8');
  for (const value of [undefined, null, '', 'fr', 'zh-CN', {}, 1]) {
    assert.deepEqual(language.sync(value), { changed: false });
    assert.equal(language.locale, 'zh-TW');
    assert.equal(fs.readFileSync(f.file, 'utf8'), before);
  }
});

test('first English snapshot creates a cache and repeated snapshots do not rewrite it', () => {
  const f = fixture(), language = createLanguage(f.file);
  assert.deepEqual(language.sync('en'), { changed: false, persisted: true });
  fs.utimesSync(f.file, 1, 1);
  assert.deepEqual(language.sync('en'), { changed: false });
  assert.equal(fs.statSync(f.file).mtimeMs, 1000);
});

test('failed cache writes still follow Core without repeated I/O or temporary files', () => {
  const f = fixture(), language = createLanguage(f.file);
  fs.mkdirSync(f.file);
  assert.deepEqual(language.sync('zh-TW'), { changed: true, persisted: false });
  assert.equal(language.locale, 'zh-TW');
  assert.deepEqual(language.sync('zh-TW'), { changed: false });
  assert.deepEqual(fs.readdirSync(f.directory), ['language.json']);
  assert.equal(createLanguage(f.file).locale, 'en');
});
