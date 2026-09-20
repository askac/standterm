(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./messages.js'));
  else root.StandTermDesktopI18n = factory(root.StandTermDesktopMessages);
})(globalThis, function (messages) {
  'use strict';

  const normalizeLocale = value => value === 'zh-TW' ? 'zh-TW' : 'en';
  const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  function create(requestedLocale) {
    const locale = normalizeLocale(requestedLocale);
    const english = messages.en || {};
    const translated = messages[locale] || {};
    function t(key, params = {}) {
      const template = (own(translated, key) && translated[key]) || (own(english, key) && english[key]) || key;
      return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
        (placeholder, name) => own(params, name) ? String(params[name]) : placeholder);
    }
    function apply(root) {
      for (const element of root.querySelectorAll('[data-i18n]')) element.textContent = t(element.dataset.i18n);
      for (const attribute of ['title', 'aria-label']) {
        for (const element of root.querySelectorAll(`[data-i18n-${attribute}]`)) {
          element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`)));
        }
      }
    }
    return { locale, t, apply };
  }
  return { normalizeLocale, create };
});
