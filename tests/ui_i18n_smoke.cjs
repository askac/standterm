'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const i18n = require('../static/js/standterm-i18n.js');

test('reviewed translations fall back to English and unknown keys remain visible', () => {
    const messages = { en: { 'test.ready': 'Ready', 'test.pending': 'Pending', 'test.empty': 'Empty' },
        'zh-TW': { 'test.ready': 'Ready translation', 'test.empty': '' } };
    const translated = i18n.create(messages, 'zh-TW');
    assert.equal(translated.locale, 'zh-TW');
    assert.equal(translated.t('test.ready'), 'Ready translation');
    assert.equal(translated.t('test.pending'), 'Pending');
    assert.equal(translated.t('test.empty'), 'Empty');
    assert.equal(translated.t('test.missing'), 'test.missing');
    assert.equal(i18n.create({ en: messages.en }, 'zh-TW').t('test.ready'), 'Ready');
    assert.equal(i18n.create({}, 'en').t('test.missing'), 'test.missing');
});

test('unsupported locale input consistently selects English', () => {
    for (const locale of [undefined, null, '', 'en', 'fr', 'zh-CN', 'zh-TW<script>', {}, 1]) {
        const translator = i18n.create({ en: { 'test.ready': 'Ready' },
            'zh-TW': { 'test.ready': 'Ready translation' } }, locale);
        assert.equal(translator.locale, 'en');
        assert.equal(translator.t('test.ready'), 'Ready');
    }
});

test('interpolation is literal, nonrecursive and preserves missing placeholders', () => {
    const translator = i18n.create({ en: {
        'test.message': '{host} / {host} / {count} / {enabled} / {missing}',
    } }, 'en');
    assert.equal(translator.t('test.message', { host: '{count} <$&>', count: 0, enabled: false }),
        '{count} <$&> / {count} <$&> / 0 / false / {missing}');
    assert.equal(translator.t('test.message'), '{host} / {host} / {count} / {enabled} / {missing}');
});

test('prototype properties cannot become messages or interpolation values', () => {
    const english = Object.assign(Object.create({ 'test.inherited': 'Not a message' }),
        { 'test.message': '{host}' });
    const translated = Object.create({ 'test.message': 'Not a translation' });
    const translator = i18n.create({ en: english, 'zh-TW': translated }, 'zh-TW');
    assert.equal(translator.t('test.inherited'), 'test.inherited');
    assert.equal(translator.t('toString'), 'toString');
    assert.equal(translator.t('test.message', Object.create({ host: 'Not a parameter' })), '{host}');
});

function element(attributes) {
    return {
        attributes: { ...attributes },
        dataset: { i18n: attributes['data-i18n'] },
        textContent: 'Original text',
        set innerHTML(_value) { assert.fail('Translations must not be interpreted as HTML'); },
        getAttribute(name) { return this.attributes[name]; },
        setAttribute(name, value) { this.attributes[name] = value; },
    };
}

test('DOM application uses text and only the allowed display attributes', () => {
    const markup = '<img src=x onerror="alert(1)"> & {host}';
    const translated = i18n.create({ en: { 'test.text': markup, 'test.attribute': 'Display label' } }, 'en');
    const button = element({ 'data-i18n': 'test.text', 'data-i18n-title': 'test.attribute',
        'data-i18n-aria-label': 'test.attribute', 'data-agent-mode': 'observe' });
    const input = element({ 'data-i18n-placeholder': 'test.attribute', value: 'operator input' });
    const link = element({ 'data-i18n-href': 'test.text', href: '/unchanged',
        'data-i18n-onclick': 'test.text', onclick: 'unchanged',
        'data-i18n-value': 'test.text', value: 'typed-control-value' });
    const elements = [button, input, link];
    const root = { querySelectorAll(selector) {
        const match = /^\[([a-z0-9-]+)\]$/.exec(selector);
        assert.ok(match, `Unexpected selector: ${selector}`);
        return elements.filter(item => Object.hasOwn(item.attributes, match[1]));
    } };
    translated.apply(root);
    assert.equal(button.textContent, markup);
    assert.equal(button.attributes.title, 'Display label');
    assert.equal(button.attributes['aria-label'], 'Display label');
    assert.equal(button.attributes['data-agent-mode'], 'observe');
    assert.equal(input.attributes.placeholder, 'Display label');
    assert.equal(input.attributes.value, 'operator input');
    assert.equal(input.textContent, 'Original text');
    assert.equal(link.attributes.href, '/unchanged');
    assert.equal(link.attributes.onclick, 'unchanged');
    assert.equal(link.attributes.value, 'typed-control-value');
    assert.equal(link.textContent, 'Original text');
});

test('the browser entry point exposes the same translation API without CommonJS', () => {
    const context = vm.createContext({});
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/standterm-i18n.js'), 'utf8'), context);
    const translator = context.StandTermI18n.create({ en: { 'test.ready': 'Ready' } }, 'en');
    assert.equal(translator.t('test.ready'), 'Ready');
    assert.equal(typeof translator.apply, 'function');
});
