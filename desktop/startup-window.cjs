'use strict';

const { create, normalizeLocale } = require('./i18n.js');

function startupHtml(title, { locale, t } = create('en')) {
  const escape = value => String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
  return `<!doctype html><html lang="${normalizeLocale(locale)}"><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
    <title>${escape(title)}</title>
    <style>body{font:15px system-ui;background:#1e1e1e;color:#eee;margin:28px}
    h1{font-size:21px;margin:0 0 20px}p{color:#ccc}progress{width:100%;accent-color:#58a6ff}</style>
    <h1>${escape(title)}</h1><p role="status">${escape(t('desktop.startup.starting'))}</p>
    <progress aria-label="${escape(t('desktop.startup.starting'))}"></progress></html>`;
}

function createStartupWindow(title, i18n) {
  const { BrowserWindow, session } = require('electron');
  const url = 'data:text/html,' + encodeURIComponent(startupHtml(title, i18n));
  const isolated = session.fromPartition('standterm-startup', { cache: false });
  isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  isolated.setDevicePermissionHandler(() => false);
  isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: details.url !== url }));
  const win = new BrowserWindow({ title, width: 440, height: 210, show: true,
    backgroundColor: '#1e1e1e', resizable: false, maximizable: false, fullscreenable: false,
    closable: false, autoHideMenuBar: true,
    webPreferences: { session: isolated, sandbox: true, contextIsolation: true,
      nodeIntegration: false, webviewTag: false, devTools: false } });
  win.setMenu(null);
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  for (const event of ['will-navigate', 'will-frame-navigate', 'will-redirect', 'will-attach-webview']) {
    win.webContents.on(event, event => event.preventDefault());
  }
  return {
    ready: win.loadURL(url),
    focus() {
      if (win.isDestroyed()) return;
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    },
    close() { if (!win.isDestroyed()) win.destroy(); },
  };
}

module.exports = { startupHtml, createStartupWindow };
