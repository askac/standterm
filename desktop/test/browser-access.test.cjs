'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createBrowserAccess, validateAccessUrl } = require('../browser-access.cjs');
const { create } = require('../i18n.js');

const origin = 'http://127.0.0.1:64487';
function fixture({ consent = true, badUrl = false, locale = 'en', failure = false } = {}) {
  const requests = [], copied = [], opened = [], notices = [], confirmations = [], errors = [];
  const access = createBrowserAccess({ origin, launcherToken: 'launcher-test', available: () => true,
    t: create(locale).t,
    confirm: async action => { confirmations.push(action); return consent; }, copy: value => copied.push(value), open: async value => opened.push(value),
    notify: async (value, error = false) => { notices.push(value); errors.push(error); }, session: { fetch: async (url, options) => {
      requests.push({ url, options });
      if (failure) throw new Error(`${origin}/?token=access-test&authorize=grant-test`);
      return new Response(JSON.stringify(url.endsWith('/access-url')
        ? { status: 'ok', access_url: badUrl ? 'https://example.com/?token=access-test' : `${origin}/?token=access-test` }
        : { status: 'ok', authorization_url: `${origin}/?token=access-test&authorize=grant-test` }));
    } },
  });
  return { access, requests, copied, opened, notices, confirmations, errors };
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

test('translated browser access menus preserve all four fixed actions and sensitive payloads', async () => {
  for (const locale of ['en', 'zh-TW']) {
    const { t } = create(locale);
    for (const [index, action, key] of [[0, 'open', 'open'], [1, 'copy-auth', 'copy_authorization'],
      [3, 'copy-url', 'copy_url'], [4, 'copy-token', 'copy_token']]) {
      const f = fixture({ locale });
      assert.equal(f.access.menu.label, t('desktop.browser_access.menu'));
      assert.equal(f.access.menu.submenu[index].label, t(`desktop.browser_access.${key}`));
      assert.equal(await f.access.menu.submenu[index].click(), true);
      const authorization = ['open', 'copy-auth'].includes(action);
      assert.deepEqual(f.confirmations, authorization ? [action] : []);
      assert.deepEqual(f.requests.map(request => request.url), authorization
        ? [`${origin}/access-url`, `${origin}/launcher/browser_authorization_url`] : [`${origin}/access-url`]);
      if (authorization) {
        assert.equal(f.requests[1].options.method, 'POST');
        assert.equal(new URLSearchParams(f.requests[1].options.body).get('access_url'), `${origin}/?token=access-test`);
      }
      const payload = action === 'copy-token' ? 'access-test'
        : `${origin}/?token=access-test${authorization ? '&authorize=grant-test' : ''}`;
      assert.deepEqual(f.opened, action === 'open' ? [payload] : []);
      assert.deepEqual(f.copied, action === 'open' ? [] : [payload]);
      assert.deepEqual(f.notices, [t(action === 'open' ? 'desktop.browser_access.opened' : 'desktop.browser_access.copied')]);
      assert.deepEqual(f.errors, [false]);
      assert.ok(!f.notices.join().includes('access-test'));
      assert.ok(!f.notices.join().includes('grant-test'));
      assert.equal(await f.access.run(f.access.menu.submenu[index].label), false);
      assert.equal(f.requests.length, authorization ? 2 : 1);
    }
  }
});

test('both languages retain zero-request cancellation and sanitized errors without retry', async () => {
  for (const locale of ['en', 'zh-TW']) {
    for (const action of ['open', 'copy-auth']) {
      const canceled = fixture({ consent: false, locale });
      assert.equal(await canceled.access.run(action), false);
      assert.deepEqual(canceled.requests, []);
      assert.deepEqual(canceled.notices, []);
    }
    const failed = fixture({ locale, failure: true });
    assert.equal(await failed.access.run('open'), false);
    assert.equal(failed.requests.length, 1);
    assert.deepEqual(failed.copied, []);
    assert.deepEqual(failed.opened, []);
    assert.deepEqual(failed.notices, [create(locale).t('desktop.browser_access.failed')]);
    assert.deepEqual(failed.errors, [true]);
    for (const privateValue of [origin, 'access-test', 'grant-test']) assert.ok(!failed.notices.join().includes(privateValue));
  }
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
