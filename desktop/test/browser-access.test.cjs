'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createBrowserAccess, validateAccessUrl } = require('../browser-access.cjs');

const origin = 'http://127.0.0.1:64487';
function fixture({ consent = true, badUrl = false } = {}) {
  const requests = [], copied = [], opened = [], notices = [];
  const access = createBrowserAccess({ origin, launcherToken: 'launcher-test', available: () => true,
    confirm: async () => consent, copy: value => copied.push(value), open: async value => opened.push(value),
    notify: async value => notices.push(value), session: { fetch: async (url, options) => {
      requests.push({ url, options });
      return new Response(JSON.stringify(url.endsWith('/access-url')
        ? { status: 'ok', access_url: badUrl ? 'https://example.com/?token=access-test' : `${origin}/?token=access-test` }
        : { status: 'ok', authorization_url: `${origin}/?token=access-test&authorize=grant-test` }));
    } },
  });
  return { access, requests, copied, opened, notices };
}

test('access token is copied on demand, never the Desktop session cookie', async () => {
  const f = fixture();
  assert.equal(f.requests.length, 0);
  assert.equal(await f.access.run('copy-token'), true);
  assert.deepEqual(f.copied, ['access-test']);
  assert.equal(f.requests.length, 1);
  assert.equal(f.requests[0].options.redirect, 'error');
  assert.equal(f.requests[0].options.credentials, 'include');
  assert.ok(!f.notices.join().includes('access-test'));
});

test('new browser authorization requires consent and stays bound to the backend origin', async () => {
  const f = fixture();
  assert.equal(await f.access.run('open'), true);
  assert.deepEqual(f.opened, [`${origin}/?token=access-test&authorize=grant-test`]);
  assert.equal(f.requests[1].options.headers['X-StandTerm-Launcher-Token'], 'launcher-test');
  const canceled = fixture({ consent: false });
  assert.equal(await canceled.access.run('copy-auth'), false);
  assert.equal(canceled.requests.length, 0);
  const bad = fixture({ badUrl: true });
  assert.equal(await bad.access.run('open'), false);
  assert.equal(bad.opened.length, 0);
  assert.equal(bad.copied.length, 0);
  assert.ok(!bad.notices.join().includes('access-test'));
});

test('access URLs reject other origins, paths, credentials and unexpected query fields', () => {
  for (const value of [`${origin}/other?token=a`, `${origin}/?token=a&unexpected=b`,
    'http://user:password@127.0.0.1:64487/?token=a', `${origin}/?token=a#fragment`, `${origin}/`]) {
    assert.throws(() => validateAccessUrl(value, origin));
  }
  assert.throws(() => validateAccessUrl(`${origin}/?token=a`, origin, true));
});
