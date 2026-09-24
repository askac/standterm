'use strict';

const { BrowserWindow, clipboard, dialog } = require('electron');
const { allowedNavigation } = require('./policy.cjs');
const { create } = require('./i18n.js');

function installContextPaste(win, contents, origin, notify, t = create('en').t) {
  let pending = false;
  let navigation = 0;
  contents.on('did-start-navigation', (_event, _url, _inPlace, mainFrame) => {
    if (mainFrame) navigation++;
  });
  const available = () => !win.isDestroyed() && !contents.isDestroyed()
    && !contents.isLoadingMainFrame() && allowedNavigation(contents.getURL(), origin)
    && win.isVisible() && !win.isMinimized() && BrowserWindow.getFocusedWindow() === win;
  const canWrite = (requester, permission, details) => permission === 'clipboard-sanitized-write'
    && requester === contents && details?.isMainFrame === true
    && allowedNavigation(details.requestingUrl, origin) && available();
  contents.session.setPermissionCheckHandler((requester, permission, requestingOrigin, details) =>
    allowedNavigation(requestingOrigin, origin) && canWrite(requester, permission, details));
  contents.session.setPermissionRequestHandler((requester, permission, callback, details) => {
    if (canWrite(requester, permission, details)) {
      callback(true);
      return;
    }
    if (permission !== 'clipboard-read' || requester !== contents || details?.isMainFrame !== true
        || !allowedNavigation(details.requestingUrl, origin) || pending || !available()) {
      callback(false);
      return;
    }
    pending = true;
    const epoch = navigation;
    const frame = contents.mainFrame;
    const current = () => epoch === navigation && !frame.isDestroyed() && contents.mainFrame === frame && available();
    // Never grant web clipboard reads. Only the native confirmation authorizes
    // a single text read, delivered to the captured Core paste-review target.
    void (async () => {
      const id = await frame.executeJavaScript('window.standtermUi?.contextPasteRequest()');
      if (typeof id !== 'string' || id.length > 80 || !current()) return;
      const result = await dialog.showMessageBox(win, {
        type: 'question', title: t('desktop.paste.title'),
        message: t('desktop.paste.message'), detail: t('desktop.paste.detail'),
        buttons: [t('desktop.common.cancel'), t('desktop.paste.confirm')], defaultId: 0, cancelId: 0, noLink: true,
      });
      if (result.response !== 1) return;
      if (!current()) return;
      const valid = await frame.executeJavaScript(`window.standtermUi?.contextPasteRequest() === ${JSON.stringify(id)}`);
      if (!valid || !current()) { await notify(t('desktop.paste.target_changed'), true); return; }
      const text = await clipboard.readText();
      if (!current()) return;
      const delivered = await frame.executeJavaScript(
        `window.standtermUi?.completeContextPaste(${JSON.stringify(id)}, ${JSON.stringify(text)})`);
      if (!delivered) await notify(t('desktop.paste.target_changed'), true);
      else if (!text) await notify(t('desktop.paste.empty'));
    })().catch(async () => {
      if (!win.isDestroyed()) await notify(t('desktop.paste.unconfirmed'), true);
    }).finally(() => {
      pending = false;
      callback(false);
    });
  });
}

module.exports = { installContextPaste };
