'use strict';

const { allowedNavigation } = require('./policy.cjs');

function externalUrl(raw, origin) {
  if (typeof raw !== 'string' || raw.length > 4096 || /[\s\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069\\]/.test(raw)) return null;
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase().replace(/\.$/, '');
    if (!['http:', 'https:'].includes(url.protocol) || !host || url.username || url.password
        || url.origin === origin || host === 'localhost' || host.endsWith('.localhost')
        || /^127\./.test(host) || host === '[::1]' || host.startsWith('[::ffff:')
        || host === '0.0.0.0' || host === '[::]') return null;
    return url.href;
  } catch { return null; }
}

function createExternalOpener({ origin, owner, confirm, open, notify }) {
  let pending = false;
  return async (raw, source) => {
    const url = externalUrl(raw, origin);
    const valid = () => !owner.isDestroyed() && !source.isDestroyed()
      && allowedNavigation(owner.getURL(), origin);
    if (!url || pending || !valid()) return false;
    pending = true;
    try {
      if (await confirm(url) !== true || !valid()) return false;
      await open(url);
      return true;
    } catch {
      try { await notify(); } catch { /* The owner may close while the OS dialog is pending. */ }
      return false;
    } finally { pending = false; }
  };
}

module.exports = { externalUrl, createExternalOpener };
