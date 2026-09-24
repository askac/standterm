'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { create, normalizeLocale } = require('./i18n.js');

function createLanguage(file) {
  let selected = 'en';
  try {
    if (fs.statSync(file).size <= 16384) {
      const data = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (data?.version === 1) selected = normalizeLocale(data.locale);
    }
  } catch { /* Missing or invalid preferences use English. */ }
  let active = create(selected);
  let lastSynchronized;
  function sync(locale) {
    if (!['en', 'zh-TW'].includes(locale) || locale === lastSynchronized) return { changed: false };
    lastSynchronized = locale;
    const changed = active.locale !== locale;
    active = create(locale);
    const temporary = `${file}.${randomUUID()}.tmp`;
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(temporary, JSON.stringify({ version: 1, locale }, null, 2), { flag: 'wx', mode: 0o600 });
      fs.renameSync(temporary, file);
      return { changed, persisted: true };
    } catch {
      // Keep the current Core preference active even when its startup cache cannot be saved.
      return { changed, persisted: false };
    } finally {
      try { fs.unlinkSync(temporary); } catch { /* A failed cache write must not block the UI. */ }
    }
  }
  return { get locale() { return active.locale; }, t: (...args) => active.t(...args), sync };
}

module.exports = { createLanguage };
