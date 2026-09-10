'use strict';

function validateAccessUrl(value, origin, authorization = false) {
  if (typeof value !== 'string' || value.length > 8192) throw new Error('Invalid access response.');
  const url = new URL(value);
  if (url.origin !== origin || url.pathname !== '/' || url.username || url.password || url.hash
      || !url.searchParams.get('token') || url.searchParams.getAll('token').length !== 1
      || (authorization && (!url.searchParams.get('authorize') || url.searchParams.getAll('authorize').length !== 1))
      || [...url.searchParams.keys()].some(key => !['token', 'authorize'].includes(key))) {
    throw new Error('Invalid access response.');
  }
  return url;
}

function createBrowserAccess({ origin, session, launcherToken, available, confirm, copy, open, notify }) {
  const base = new URL(origin);
  if (base.origin !== origin || base.protocol !== 'http:' || base.hostname !== '127.0.0.1'
      || !base.port || typeof launcherToken !== 'string' || !launcherToken) throw new Error('Invalid browser access authority.');
  let pending = false;
  async function request(route, options = {}) {
    if (!available()) throw new Error('Desktop is unavailable.');
    const response = await session.fetch(`${origin}${route}`, {
      ...options, credentials: 'include', redirect: 'error', signal: AbortSignal.timeout(5000),
    });
    if (!response.ok) throw new Error('Browser access request failed.');
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 16384) throw new Error('Invalid access response.');
        chunks.push(Buffer.from(value));
      }
    } finally { await reader.cancel().catch(() => {}); }
    const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (data?.status !== 'ok' || !available()) throw new Error('Browser access request failed.');
    return data;
  }
  async function run(action) {
    if (!['open', 'copy-auth', 'copy-url', 'copy-token'].includes(action) || pending || !available()) return false;
    pending = true;
    try {
      if (['open', 'copy-auth'].includes(action) && !await confirm(action)) return false;
      const data = await request('/access-url');
      let url = validateAccessUrl(data.access_url, origin);
      if (['open', 'copy-auth'].includes(action)) {
        const grant = await request('/launcher/browser_authorization_url', {
          method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-StandTerm-Launcher-Token': launcherToken },
          body: new URLSearchParams({ access_url: url.href }).toString(),
        });
        url = validateAccessUrl(grant.authorization_url, origin, true);
      }
      if (!available()) return false;
      if (action === 'open') await open(url.href);
      else copy(action === 'copy-token' ? url.searchParams.get('token') : url.href);
      await notify(action === 'open' ? 'Browser authorization opened in the default browser.' : 'Access information copied. Treat it as a password.');
      return true;
    } catch {
      // Never forward URL-bearing network errors, access tokens or grants to logs/UI.
      await notify('Could not prepare browser access. Check that this Desktop backend is still running.', true);
      return false;
    } finally { pending = false; }
  }
  return {
    run,
    dispose: () => { launcherToken = ''; },
    menu: { label: 'Browser Access', submenu: [
      { label: 'Open in browser...', click: () => run('open') },
      { label: 'Copy browser authorization URL...', click: () => run('copy-auth') },
      { type: 'separator' },
      { label: 'Copy access URL (sensitive)', click: () => run('copy-url') },
      { label: 'Copy access token (sensitive)', click: () => run('copy-token') },
    ] },
  };
}

module.exports = { createBrowserAccess, validateAccessUrl };
