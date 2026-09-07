'use strict';

const { allowedFloatingWindow, allowedFilesDownload, allowedNavigation } = require('./policy.cjs');

function installFloatingWindows(opener, origin, openExternal = () => {}) {
  const children = new Set();
  const closing = new WeakSet();
  const download = (contents, details) => {
    if (!details.postBody && allowedNavigation(opener.webContents.getURL(), origin)) {
      if (allowedFilesDownload(details.url, origin)) contents.downloadURL(details.url);
      else void openExternal(details.url, contents);
    }
    return { action: 'deny' };
  };
  opener.webContents.setWindowOpenHandler(details => {
    if (allowedFilesDownload(details.url, origin)) return download(opener.webContents, details);
    if (details.url !== 'about:blank') return download(opener.webContents, details);
    if ([...children].some(child => !closing.has(child))
        || !allowedFloatingWindow(details, opener.webContents.getURL(), origin)) return { action: 'deny' };
    // about:blank inherits the opener's sandbox, no-Node/no-preload preferences
    // and private session. Do not create a second privileged renderer bridge.
    return { action: 'allow', outlivesOpener: false,
      overrideBrowserWindowOptions: { autoHideMenuBar: true, alwaysOnTop: true,
        minimizable: false, maximizable: false, fullscreenable: false } };
  });
  opener.webContents.on('did-create-window', child => {
    children.add(child);
    child.removeMenu();
    child.webContents.setWindowOpenHandler(details => download(child.webContents, details));
    // These windows host adopted DOM, not documents loaded through navigation.
    // Session network/permission restrictions are inherited independently.
    child.webContents.on('will-navigate', event => event.preventDefault());
    child.webContents.on('will-frame-navigate', event => event.preventDefault());
    child.webContents.on('will-redirect', event => event.preventDefault());
    child.webContents.on('will-attach-webview', event => event.preventDefault());
    // Release admission when close starts, retaining lifecycle ownership until
    // closed. PiP -> Files requests the replacement in the same renderer task.
    child.on('close', () => closing.add(child));
    child.webContents.on('will-prevent-unload', event => event.preventDefault());
    child.once('closed', () => children.delete(child));
  });
  const closeChildren = () => {
    for (const child of children) if (!child.isDestroyed()) child.close();
  };
  opener.webContents.on('did-start-navigation', (_event, _url, inPlace, isMainFrame) => {
    if (isMainFrame && !inPlace) closeChildren();
  });
  opener.once('closed', closeChildren);
  opener.webContents.once('render-process-gone', closeChildren);
}

module.exports = { installFloatingWindows };
