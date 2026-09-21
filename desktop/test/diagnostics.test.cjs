'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { create } = require('../i18n.js');
const { createDiagnostics, diagnosticsMenu, agentConnectionInfo, openDeveloperTools, MAX_LOG_BYTES } = require('../diagnostics.cjs');
const { statusHtml } = require('../diagnostics-window.cjs');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-diagnostics-test-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, logger: createDiagnostics(directory, { mode: 'wsl', version: '0.3.3' }) };
}

test('diagnostics persist only bounded structured metadata, never payloads or credentials', t => {
  const { logger } = fixture(t);
  logger.write('backend_verify_retry', { port: 51761, attempt: 2, elapsedMs: 2000,
    code: 'ECONNREFUSED', token: 'secret-value', password: 'secret-value',
    message: 'secret-value', stderr: 'secret-value', origin: 'http://127.0.0.1/?token=secret-value' });
  logger.write('unknown-event-secret-value', { port: 5000 });
  logger.write('backend_exit', { code: 'secret-value', exitCode: null, port: Infinity, expected: true });
  const text = fs.readFileSync(logger.file, 'utf8');
  assert.ok(!text.includes('secret-value'));
  const [retry, exited] = text.trim().split('\n').map(JSON.parse);
  assert.equal(retry.port, 51761);
  assert.equal(retry.code, 'ECONNREFUSED');
  assert.equal(retry.mode, 'wsl');
  assert.equal(retry.attempt, 2);
  assert.equal(exited.expected, true);
  assert.equal(Object.hasOwn(exited, 'exitCode'), false);
  assert.equal(Object.hasOwn(exited, 'port'), false);
});

test('both diagnostic languages preserve menu IDs, callbacks and literal backend URLs', t => {
  const { logger } = fixture(t);
  for (const locale of ['en', 'zh-TW']) {
    const { t: translate } = create(locale), calls = [], copied = [];
    const menu = diagnosticsMenu({ t: translate, origin: 'http://127.0.0.1:64487', mode: 'wsl',
      instanceId: 'test-instance', version: '0.5.2', logger, persistent: true,
      openLogs: () => calls.push('logs'), openTools: () => calls.push('tools'), openStatus: () => calls.push('status'),
      copyText: value => copied.push(value) });
    assert.equal(menu.id, 'diagnostics');
    assert.equal(menu.label, translate('desktop.toolbar.menu_diagnostics'));
    const item = id => menu.submenu.find(item => item.id === id);
    item('diagnostics-status').click(); item('diagnostics-logs').click(); item('diagnostics-devtools').click();
    item('diagnostics-copy-origin').click();
    assert.deepEqual(calls, ['status', 'logs', 'tools']);
    assert.deepEqual(copied, ['http://127.0.0.1:64487']);
    assert.equal(item('diagnostics-origin').label, translate('desktop.diagnostics.origin', { origin: copied[0] }));
    assert.equal(item('diagnostics-core-version').label, translate('desktop.about.core_version', { version: translate('desktop.about.unknown_version') }));
    assert.ok(menu.submenu.some(item => item.label === translate('desktop.diagnostics.web_settings_persistent')));
  }
});

test('diagnostic translations and raw events stay escaped in a scriptless snapshot', () => {
  const event = { event: 'backend_ready', mode: 'wsl', code: 'ECONNREFUSED', value: '</pre><script>bad()</script>&' };
  for (const locale of ['en', 'zh-TW']) {
    const i18n = create(locale);
    const html = statusHtml([['Literal label', '/tmp/<profile>&{value}']], [event], i18n);
    assert.ok(html.includes(`<html lang="${locale}">`));
    assert.ok(html.includes(i18n.t('desktop.diagnostics.title')));
    assert.ok(html.includes('/tmp/&lt;profile&gt;&amp;{value}'));
    assert.ok(html.includes('&quot;event&quot;:&quot;backend_ready&quot;'));
    assert.ok(html.includes('ECONNREFUSED'));
    assert.ok(!html.includes('<script'));
    assert.ok(html.includes("default-src 'none'"));
    assert.ok(statusHtml([], [], i18n).includes(i18n.t('desktop.diagnostics.no_events')));
  }
  const injected = statusHtml([], [], { locale: 'en" onload="bad()', t: () => '<img src=x onerror=bad()>&' });
  assert.ok(injected.includes('<html lang="en">'));
  assert.ok(!injected.includes('<img'));
  assert.ok(injected.includes('&lt;img'));
});

test('translated diagnostic windows refresh through the same isolated scriptless route', async () => {
  for (const locale of ['en', 'zh-TW']) {
    const owner = new EventEmitter(); owner.isDestroyed = () => false;
    let requestFilter, permission, device, snapshotCalls = 0, copied = 0;
    const windows = [];
    class Window extends EventEmitter {
      constructor(options) {
        super(); this.options = options; this.urls = []; this.destroyed = false;
        this.webContents = new EventEmitter();
        this.webContents.setWindowOpenHandler = handler => { this.openHandler = handler; };
        windows.push(this);
      }
      isDestroyed() { return this.destroyed; }
      destroy() { this.destroyed = true; this.emit('closed'); }
      setMenu(menu) { this.menu = menu; }
      setTitle(title) { this.title = title; }
      async loadURL(url) { this.urls.push(url); }
      show() {}
      focus() {}
    }
    const isolated = {
      setPermissionRequestHandler: handler => { permission = handler; },
      setPermissionCheckHandler: handler => { isolated.permissionCheck = handler; },
      setDevicePermissionHandler: handler => { device = handler; },
      webRequest: { onBeforeRequest: handler => { requestFilter = handler; } },
    };
    const electron = { BrowserWindow: Window, Menu: { buildFromTemplate: template => template },
      session: { fromPartition: (_partition, options) => { assert.equal(options.cache, false); return isolated; } } };
    const api = { exports: {} };
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'diagnostics-window.cjs'), 'utf8'), {
      require: name => name === 'electron' ? electron : require(name.startsWith('.') ? path.join(__dirname, '..', name) : name),
      module: api,
    });
    const i18n = create(locale);
    const show = api.exports.createStatusWindow(owner, () => ({ rows: [['Snapshot', ++snapshotCalls]], events: [] }),
      { copyUrl: () => copied++, i18n });
    const win = await show();
    assert.equal(win.options.title, i18n.t('desktop.diagnostics.window_title'));
    assert.equal(win.options.webPreferences.sandbox, true);
    assert.equal(win.options.webPreferences.nodeIntegration, false);
    assert.equal(win.options.webPreferences.preload, undefined);
    assert.equal(win.menu[0].label, i18n.t('desktop.toolbar.menu_view'));
    const [refresh, copy] = win.menu[0].submenu;
    assert.equal(refresh.label, i18n.t('desktop.diagnostics.refresh'));
    assert.equal(refresh.accelerator, 'CommandOrControl+R');
    await refresh.click(); copy.click();
    assert.equal(snapshotCalls, 2);
    assert.equal(copied, 1);
    assert.equal(await show(), win);
    assert.equal(windows.length, 1);
    assert.ok(decodeURIComponent(win.urls.at(-1)).includes(`<html lang="${locale}">`));
    assert.equal(win.openHandler().action, 'deny');
    for (const [url, blocked] of [[win.urls[0], false], ['https://example.com/', true], ['file:///private.txt', true]]) {
      requestFilter({ url }, result => assert.equal(result.cancel, blocked));
    }
    permission(null, 'media', allowed => assert.equal(allowed, false));
    assert.equal(isolated.permissionCheck(), false);
    assert.equal(device(), false);
    owner.emit('closed');
    assert.equal(win.isDestroyed(), true);
  }
});

test('diagnostic rotation is bounded and write failure does not block startup', t => {
  const { directory, logger } = fixture(t);
  fs.writeFileSync(logger.file, 'x'.repeat(MAX_LOG_BYTES));
  logger.write('startup');
  const previous = path.join(directory, 'startup.previous.jsonl');
  assert.equal(fs.statSync(previous).size, MAX_LOG_BYTES);
  fs.writeFileSync(logger.file, 'y'.repeat(MAX_LOG_BYTES));
  logger.write('startup');
  assert.equal(fs.readFileSync(previous, 'utf8')[0], 'y');
  assert.ok(fs.statSync(logger.file).size < 1024);
  const blocked = createDiagnostics(logger.file, { mode: 'windows', version: '0.3.3' });
  assert.doesNotThrow(() => blocked.write('startup'));
  assert.equal(blocked.available, false);
});

test('capture diagnostics record window state without error payloads', t => {
  const { logger } = fixture(t);
  logger.write('capture_failed', { width: 624, height: 561, visible: true, minimized: false,
    focused: true, message: 'private path or terminal data', screenshot: 'private bytes' });
  const entry = logger.snapshot()[0];
  assert.equal(entry.width, 624);
  assert.equal(entry.height, 561);
  assert.equal(entry.visible, true);
  assert.equal(entry.minimized, false);
  assert.equal(entry.focused, true);
  assert.ok(!JSON.stringify(entry).includes('private'));
});

test('diagnostics menu shows the actual owned endpoint without an access URL', t => {
  const { logger } = fixture(t);
  const options = { origin: 'http://127.0.0.1:64487', mode: 'wsl', version: '0.3.3', logger,
    instanceId: 'test-instance', openLogs: () => {}, openTools: () => {} };
  const menu = diagnosticsMenu(options);
  const endpoint = menu.submenu.find(item => item.id === 'diagnostics-origin');
  assert.equal(endpoint.label, 'URL: http://127.0.0.1:64487');
  assert.equal(endpoint.enabled, false);
  assert.equal(menu.submenu.find(item => item.id === 'diagnostics-core-version').label, 'Core version: Unknown (older Core)');
  assert.equal(diagnosticsMenu({ ...options, coreVersion: '2.11.0-dev' }).submenu
    .find(item => item.id === 'diagnostics-core-version').label, 'Core version: 2.11.0-dev');
  for (const origin of ['http://127.0.0.1:64487/?token=secret', 'https://example.com', 'http://secret@127.0.0.1:64487']) {
    assert.throws(() => diagnosticsMenu({ ...options, origin }));
  }
});

test('backend URL copy preserves origin validation without another AgentInfo entry', t => {
  const { logger } = fixture(t);
  const options = { origin: 'http://127.0.0.1:64487', mode: 'wsl', instanceId: 'desktop-test-1',
    token: 'secret', session_token: 'secret', launcher_token: 'secret', terminal: 'private content' };
  const info = agentConnectionInfo(options);
  assert.deepEqual(info, { schema: 'standterm_agent_connection', schema_version: 1,
    base_url: options.origin, agentinfo_url: options.origin + '/agentinfo',
    instance_id: 'desktop-test-1', backend_mode: 'wsl' });
  const copied = [];
  const menu = diagnosticsMenu({ ...options, version: '0.4.0', logger, copyText: value => copied.push(value) });
  assert.deepEqual(copied, []);
  menu.submenu.find(item => item.id === 'diagnostics-copy-origin').click();
  assert.deepEqual(copied, [options.origin]);
  assert.equal(menu.submenu.find(item => item.id === 'diagnostics-copy-agent'), undefined);
  for (const invalid of [{ origin: options.origin + '/?token=secret' }, { instanceId: '' },
    { instanceId: 'id\nsecret' }, { mode: 'unknown' }]) {
    assert.throws(() => agentConnectionInfo({ ...options, ...invalid }));
  }
});

test('Developer Tools opens only after explicit consent and never for destroyed contents', async () => {
  const calls = [];
  const contents = { isDestroyed: () => false, openDevTools: options => calls.push(options) };
  assert.equal(await openDeveloperTools(contents, async () => false), false);
  assert.equal(await openDeveloperTools(contents, async () => 'true'), false);
  assert.equal(calls.length, 0);
  assert.equal(await openDeveloperTools(contents, async () => true), true);
  assert.deepEqual(calls, [{ mode: 'detach' }]);
  contents.isDestroyed = () => true;
  assert.equal(await openDeveloperTools(contents, async () => true), false);
  assert.equal(calls.length, 1);
});

test('diagnostics page escapes display data and snapshots remain bounded and detached', t => {
  const { logger } = fixture(t);
  for (let n = 0; n < 220; n++) logger.write('startup', { port: n, token: 'secret' });
  const snapshot = logger.snapshot();
  assert.equal(snapshot.length, 200);
  snapshot[0].port = 'modified';
  assert.equal(logger.snapshot()[0].port, 20);
  const html = statusHtml([['Injected', '<img src="https://example.com" onerror="alert(1)">']], logger.snapshot());
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('secret'));
  assert.ok(html.includes("default-src 'none'"));
  assert.ok(html.includes('&lt;img'));
});
