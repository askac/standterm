'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { create, normalizeLocale } = require('../i18n.js');
const messages = require('../messages.js');

test('the Desktop catalog contains reviewed bilingual messages and normalizes locales', () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages['zh-TW']).sort());
  for (const value of [undefined, null, {}, 'zh-CN', 'constructor', '../zh-TW']) assert.equal(normalizeLocale(value), 'en');
  assert.equal(normalizeLocale('zh-TW'), 'zh-TW');
  assert.equal(create('en').t('desktop.menu.new_tab'), 'New terminal tab');
  assert.notEqual(create('zh-TW').t('desktop.menu.new_tab'), create('en').t('desktop.menu.new_tab'));
  assert.equal(create('en').t('constructor'), 'constructor');
  assert.equal(create('zh-TW').t('desktop.missing'), 'desktop.missing');
});

test('missing translations fall back to English and substitution remains literal display data', () => {
  const context = { StandTermDesktopMessages: { en: { 'test.literal': '{value} / {missing}' }, 'zh-TW': {} } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'i18n.js'), 'utf8'), context);
  const t = context.StandTermDesktopI18n.create('zh-TW').t;
  const value = '<b>$& {missing}</b>';
  assert.equal(t('test.literal', { value }), value + ' / {missing}');
  assert.equal(t('test.literal', Object.create({ value: 'inherited' })), '{value} / {missing}');
});
