'use strict';

const { allowedFloatingWindow, allowedFilesDownload, allowedNavigation } = require('./policy.cjs');

function installFloatingWindows(opener, origin, openExternal = () => {}, contents = opener.webContents,
  downloadDone = () => {}) {
  const children = new Set();
  const closing = new WeakSet();
  const downloads = new Map();
  const downloadSession = contents.session;
  const onDownload = (_event, item, source) => {
    const child = [...children].find(candidate => candidate.webContents === source);
    if (source !== contents && !child) return;
    if (!allowedFilesDownload(item.getURL(), origin)) return;
    const done = (_doneEvent, state) => {
      downloads.delete(item);
      if (state === 'completed' || state === 'interrupted') {
        downloadDone({ state, path: item.getSavePath() }, child && !child.isDestroyed() ? child : opener);
      }
    };
    downloads.set(item, done);
    item.once('done', done);
  };
  downloadSession.on('will-download', onDownload);
  opener.once('closed', () => {
    downloadSession.removeListener('will-download', onDownload);
    for (const [item, done] of downloads) item.removeListener('done', done);
    downloads.clear();
  });
  const download = (target, details) => {
    if (!details.postBody && allowedNavigation(contents.getURL(), origin)) {
      if (allowedFilesDownload(details.url, origin)) target.downloadURL(details.url);
      else void openExternal(details.url, target);
    }
    return { action: 'deny' };
  };
  contents.setWindowOpenHandler(details => {
    if (allowedFilesDownload(details.url, origin)) return download(contents, details);
    if (details.url !== 'about:blank') return download(contents, details);
    if ([...children].some(child => !closing.has(child))
        || !allowedFloatingWindow(details, contents.getURL(), origin)) return { action: 'deny' };
    // about:blank inherits the opener's sandbox, no-Node/no-preload preferences
    // and private session. Do not create a second privileged renderer bridge.
    return { action: 'allow', outlivesOpener: false,
      overrideBrowserWindowOptions: { autoHideMenuBar: true, alwaysOnTop: true,
        minimizable: false, maximizable: false, fullscreenable: false } };
  });
  contents.on('did-create-window', child => {
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
  contents.on('did-start-navigation', (_event, _url, inPlace, isMainFrame) => {
    if (isMainFrame && !inPlace) closeChildren();
  });
  opener.once('closed', closeChildren);
  contents.once('render-process-gone', closeChildren);
}

module.exports = { installFloatingWindows };
