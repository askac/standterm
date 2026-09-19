(function (root, factory) {
    if (typeof module === 'object' && module.exports) module.exports = factory();
    else root.StandTermI18n = factory();
})(globalThis, function () {
    'use strict';

    function normalizeLocale(value) {
        return value === 'zh-TW' ? 'zh-TW' : 'en';
    }

    function create(messages, requestedLocale) {
        const locale = normalizeLocale(requestedLocale);
        const english = messages.en || {};
        const translated = messages[locale] || {};
        const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
        function t(key, params = {}) {
            const template = (own(translated, key) && translated[key])
                || (own(english, key) && english[key]) || key;
            return template.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
                (placeholder, name) => own(params, name) ? String(params[name]) : placeholder);
        }

        function apply(root) {
            root.querySelectorAll('[data-i18n]').forEach(element => {
                element.textContent = t(element.dataset.i18n);
            });
            for (const attribute of ['title', 'aria-label', 'placeholder']) {
                root.querySelectorAll(`[data-i18n-${attribute}]`).forEach(element => {
                    element.setAttribute(attribute, t(element.getAttribute(`data-i18n-${attribute}`)));
                });
            }
        }
        return { locale, t, apply };
    }

    return { normalizeLocale, create };
});
