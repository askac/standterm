'use strict';

const { randomUUID } = require('node:crypto');

function browserSessionOptions(mode, temporary = false) {
  if (!['windows', 'wsl'].includes(mode)) throw new Error('Invalid browser profile mode.');
  return {
    partition: temporary ? `standterm-test-${randomUUID()}` : `persist:standterm-ui-${mode}-v1`,
    options: { cache: false },
  };
}

async function resetBrowserAuthentication(session) {
  // Preserve Core's localStorage/IndexedDB, including browser-owned CryptoKeys.
  // A previous process's cookie or service worker must not see fresh credentials.
  await session.clearStorageData({ storages: ['cookies', 'serviceworkers', 'cachestorage'] });
  await session.clearAuthCache();
}

module.exports = { browserSessionOptions, resetBrowserAuthentication };
