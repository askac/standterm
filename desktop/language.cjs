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
  const { locale, t } = create(selected);
  let pending = false;
  async function choose(dialog, win) {
    if (pending || win.isDestroyed()) return false;
    pending = true;
    try {
      const answer = await dialog.showMessageBox(win, {
        type: 'question', title: t('desktop.language.title'), message: t('desktop.language.choose'),
        detail: t('desktop.language.detail', { language: t(selected === 'zh-TW' ? 'desktop.language.name_zh_tw' : 'desktop.language.name_en') }),
        buttons: [t('desktop.common.cancel'), t('desktop.language.name_en'), t('desktop.language.name_zh_tw')],
        defaultId: 0, cancelId: 0, noLink: true,
      });
      if (win.isDestroyed() || ![1, 2].includes(answer.response)) return false;
      const next = answer.response === 2 ? 'zh-TW' : 'en';
      if (next === selected) return true;
      const temporary = `${file}.${randomUUID()}.tmp`;
      try {
        fs.mkdirSync(path.dirname(file), { recursive: true });
        fs.writeFileSync(temporary, JSON.stringify({ version: 1, locale: next }, null, 2), { flag: 'wx', mode: 0o600 });
        fs.renameSync(temporary, file);
      } finally {
        try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
      }
      selected = next;
      if (!win.isDestroyed()) await dialog.showMessageBox(win, { type: 'info', title: t('desktop.language.title'),
        message: t('desktop.language.saved'), buttons: [t('desktop.common.ok')], noLink: true });
      return true;
    } catch {
      if (!win.isDestroyed()) await dialog.showMessageBox(win, { type: 'error', title: t('desktop.language.title'),
        message: t('desktop.language.failed'), buttons: [t('desktop.common.ok')], noLink: true }).catch(() => {});
      return false;
    } finally { pending = false; }
  }
  return { locale, t, choose };
}

module.exports = { createLanguage };
