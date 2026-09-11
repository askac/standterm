'use strict';

const { ipcMain, Menu, BrowserWindow } = require('electron');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const TOOLBAR_HEIGHT = 36;
const TOOLBAR_URL = pathToFileURL(path.join(__dirname, 'toolbar.html')).href;
const TOOLBAR_ASSETS = ['toolbar.html', 'toolbar.js', 'toolbar.css'].map(file => pathToFileURL(path.join(__dirname, file)).href);
const TOOLBAR_MENUS = ['standterm', 'edit', 'agent-menu', 'view', 'diagnostics'];

function installToolbar(win, coreView, capture, commands) {
  const contents = win.webContents;
  let state = { mac: process.platform === 'darwin', state: 'idle', label: '' };
  let noticeId = 0;
  const send = value => {
    state = { ...state, ...value };
    if (!contents.isDestroyed() && contents.mainFrame.url === TOOLBAR_URL) contents.send('standterm-toolbar-state', state);
  };
  const layout = () => {
    if (win.isDestroyed()) return;
    const [width, height] = win.getContentSize();
    coreView.setBounds({ x: 0, y: TOOLBAR_HEIGHT, width, height: Math.max(0, height - TOOLBAR_HEIGHT) });
  };
  win.contentView.addChildView(coreView);
  win.on('resize', layout);
  win.contentView.on('bounds-changed', layout);
  layout();
  contents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  contents.session.setPermissionCheckHandler(() => false);
  contents.session.setDevicePermissionHandler(() => false);
  contents.session.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !TOOLBAR_ASSETS.includes(details.url) }));
  contents.setWindowOpenHandler(() => ({ action: 'deny' }));
  for (const event of ['will-navigate', 'will-frame-navigate', 'will-redirect', 'will-attach-webview']) {
    contents.on(event, event => event.preventDefault());
  }
  ipcMain.handle('standterm-toolbar-action', async (event, action) => {
    if (event.sender !== contents || event.senderFrame !== contents.mainFrame
        || event.senderFrame.url !== TOOLBAR_URL || typeof action !== 'string' || win.isDestroyed()) return false;
    if (action === 'ready') { send(state); return true; }
    if (BrowserWindow.getFocusedWindow() !== win || !win.isVisible() || win.isMinimized()) return false;
    if (action.startsWith('menu:')) {
      const id = action.slice(5);
      if (!TOOLBAR_MENUS.includes(id)) return false;
      await commands.refresh();
      if (win.isDestroyed() || BrowserWindow.getFocusedWindow() !== win) return false;
      coreView.webContents.focus();
      Menu.getApplicationMenu()?.getMenuItemById(id)?.submenu?.popup({ window: win });
      return true;
    }
    switch (action) {
      case 'copy-text': return commands.edit('copy');
      case 'paste-text': return commands.edit('paste');
      case 'screenshot-file': await capture.screenshot('file'); break;
      case 'screenshot-clipboard': await capture.screenshot('clipboard'); break;
      case 'record': await capture.start(); break;
      case 'pause-recording': await capture.togglePause(); break;
      case 'stop-recording': await capture.stop(); break;
      default: return false;
    }
    return true;
  });
  win.once('closed', () => {
    ipcMain.removeHandler('standterm-toolbar-action');
    if (!coreView.webContents.isDestroyed()) coreView.webContents.close();
  });
  return { send, layout, url: TOOLBAR_URL, notify: async (notice, error = false) => send({ notice, error, noticeId: ++noticeId }) };
}

module.exports = { installToolbar, TOOLBAR_URL, TOOLBAR_HEIGHT };
