'use strict';

const { randomUUID } = require('node:crypto');

function statusHtml(rows, events) {
  const escape = value => String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
  return `<!doctype html><html lang="en"><meta charset="utf-8">
    <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
    <title>StandTerm Desktop - Diagnostics</title>
    <style>body{font:15px system-ui;background:#18212b;color:#e6edf3;margin:28px}
    h1{font-size:24px}h2{font-size:18px;margin-top:28px}table{border-collapse:collapse;width:100%}
    th,td{text-align:left;vertical-align:top;padding:9px;border-bottom:1px solid #384454;overflow-wrap:anywhere}
    th{width:180px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#101820;padding:16px}
    p{color:#b8c7d8}</style>
    <h1>Desktop diagnostics</h1><p>Read-only snapshot. Use View &gt; Refresh (Ctrl/Cmd+R) for current data.</p>
    <h2>Runtime and storage</h2><table>${rows.map(([name, value]) => `<tr><th>${escape(name)}</th><td>${escape(value)}</td></tr>`).join('')}</table>
    <h2>Preview and browser handoff</h2><p>External embedded previews are blocked in Desktop.
    Use the preview's Open in browser button and confirm the destination. This opens your OS default browser;
    its cookies and login are separate. Desktop credentials are not added to the URL.</p>
    <h2>Recent startup events</h2><p>Up to 200 structured events from this run. No terminal content,
    private keys, tokens or external URLs. Previous runs are available in the diagnostics log folder.</p>
    <pre>${escape(events.map(event => JSON.stringify(event)).join('\n') || 'No events yet.')}</pre></html>`;
}

function createStatusWindow(owner, snapshot, { copyUrl }) {
  const { BrowserWindow, Menu, session } = require('electron');
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
      win = new BrowserWindow({ title: 'StandTerm Desktop - Diagnostics', width: 900, height: 720,
        show: false, parent: owner, webPreferences: { session: isolated, sandbox: true,
          contextIsolation: true, nodeIntegration: false, webviewTag: false, devTools: false } });
      win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      for (const name of ['will-navigate', 'will-frame-navigate', 'will-redirect', 'will-attach-webview']) {
        win.webContents.on(name, event => event.preventDefault());
      }
      win.setMenu(Menu.buildFromTemplate([{ label: 'View', submenu: [
        { label: 'Refresh', accelerator: 'CommandOrControl+R', click: () => refresh().catch(() => {}) },
        { label: 'Copy backend URL', click: copyUrl },
        { role: 'close' },
      ] }]));
      const owned = win;
      const close = () => { if (!owned.isDestroyed()) owned.destroy(); };
      owner.once('closed', close);
      owned.once('closed', () => owner.removeListener('closed', close));
    }
    async function refresh() {
      const { rows, events } = snapshot();
      await win.loadURL('data:text/html,' + encodeURIComponent(statusHtml(rows, events)));
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
