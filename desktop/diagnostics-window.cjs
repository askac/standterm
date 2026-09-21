'use strict';

const { randomUUID } = require('node:crypto');
const { create, normalizeLocale } = require('./i18n.js');

function statusHtml(rows, events, { locale, t } = create('en')) {
  const escape = value => String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
  const text = key => escape(t(`desktop.diagnostics.${key}`));
  return `<!doctype html><html lang="${normalizeLocale(locale)}"><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
    <title>${text('window_title')}</title>
    <style>body{font:15px system-ui;background:#18212b;color:#e6edf3;margin:28px}
    h1{font-size:24px}h2{font-size:18px;margin-top:28px}table{border-collapse:collapse;width:100%}
    th,td{text-align:left;vertical-align:top;padding:9px;border-bottom:1px solid #384454;overflow-wrap:anywhere}
    th{width:180px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101820;padding:16px}
    p{color:#b8c7d8}</style>
    <h1>${text('title')}</h1><p>${text('read_only_hint')}</p>
    <h2>${text('runtime_heading')}</h2><table>${rows.map(([name, value]) => `<tr><th>${escape(name)}</th><td>${escape(value)}</td></tr>`).join('')}</table>
    <h2>${text('handoff_heading')}</h2><p>${text('handoff_hint')}</p>
    <h2>${text('events_heading')}</h2><p>${text('events_hint')}</p>
    <pre>${escape(events.map(event => JSON.stringify(event)).join('\n') || t('desktop.diagnostics.no_events'))}</pre></html>`;
}

function createStatusWindow(owner, snapshot, { copyUrl, i18n = create('en') }) {
  const { BrowserWindow, Menu, session } = require('electron');
  const { t } = i18n;
  let win;
  let opening;
  async function show() {
    if (owner.isDestroyed()) return;
    if (!win || win.isDestroyed()) {
      const isolated = session.fromPartition(`standterm-diagnostics-${randomUUID()}`, { cache: false });
      isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
      isolated.setPermissionCheckHandler(() => false);
      isolated.setDevicePermissionHandler(() => false);
      isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !details.url.startsWith('data:text/html,') }));
      win = new BrowserWindow({ title: t('desktop.diagnostics.window_title'), width: 900, height: 720,
        show: false, parent: owner, webPreferences: { session: isolated, sandbox: true,
          contextIsolation: true, nodeIntegration: false, webviewTag: false, devTools: false } });
      win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      for (const name of ['will-navigate', 'will-frame-navigate', 'will-redirect', 'will-attach-webview']) {
        win.webContents.on(name, event => event.preventDefault());
      }
      const owned = win;
      const close = () => { if (!owned.isDestroyed()) owned.destroy(); };
      owner.once('closed', close);
      owned.once('closed', () => owner.removeListener('closed', close));
    }
    async function refresh() {
      win.setTitle(t('desktop.diagnostics.window_title'));
      win.setMenu(Menu.buildFromTemplate([{ label: t('desktop.toolbar.menu_view'), submenu: [
        { label: t('desktop.diagnostics.refresh'), accelerator: 'CommandOrControl+R', click: () => refresh().catch(() => {}) },
        { label: t('desktop.diagnostics.copy_backend_url'), click: copyUrl },
        { role: 'close' },
      ] }]));

      const { rows, events } = snapshot();
      await win.loadURL('data:text/html,' + encodeURIComponent(statusHtml(rows, events, i18n)));
    }
    await refresh();
    win.show();
    win.focus();
    return win;
  }
  return () => {
    if (!opening) opening = show().finally(() => { opening = null; });
    return opening;
  };
}

module.exports = { statusHtml, createStatusWindow };
