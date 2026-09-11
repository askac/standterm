'use strict';

const { BrowserWindow, clipboard, dialog } = require('electron');
const { allowedNavigation } = require('./policy.cjs');

function installContextPaste(win, contents, origin, notify) {
  let pending = false;
  let navigation = 0;
  contents.on('did-start-navigation', (_event, _url, _inPlace, mainFrame) => {
    if (mainFrame) navigation++;
  });
  const available = () => !win.isDestroyed() && !contents.isDestroyed()
    && !contents.isLoadingMainFrame() && allowedNavigation(contents.getURL(), origin)
    && win.isVisible() && !win.isMinimized() && BrowserWindow.getFocusedWindow() === win;
  contents.session.setPermissionRequestHandler((requester, permission, callback, details) => {
    if (permission !== 'clipboard-read' || requester !== contents || details?.isMainFrame !== true
        || !allowedNavigation(details.requestingUrl, origin) || pending || !available()) {
      callback(false);
      return;
    }
    pending = true;
    const epoch = navigation;
    const frame = contents.mainFrame;
    const current = () => epoch === navigation && !frame.isDestroyed() && contents.mainFrame === frame && available();
    // Never grant web clipboard access. Only the native confirmation authorizes
    // a single text read, delivered to the captured Core paste-review target.
    void (async () => {
      const id = await frame.executeJavaScript('window.standtermUi?.contextPasteRequest()');
      if (typeof id !== 'string' || id.length > 80 || !current()) return;
      const result = await dialog.showMessageBox(win, {
        type: 'question', title: 'Paste into StandTerm',
        message: 'Paste clipboard text into this terminal?',
        detail: 'This reads clipboard text once. Multi-line or large text still requires review. '
          + 'For direct paste, use the Paste button beside the application menu.',
        buttons: ['Cancel', 'Paste'], defaultId: 0, cancelId: 0, noLink: true,
      });
      if (result.response !== 1) return;
      if (!current()) return;
      const valid = await frame.executeJavaScript(`window.standtermUi?.contextPasteRequest() === ${JSON.stringify(id)}`);
      if (!valid || !current()) { await notify('Paste canceled because the target changed.', true); return; }
      const text = await clipboard.readText();
      if (!current()) return;
      const delivered = await frame.executeJavaScript(
        `window.standtermUi?.completeContextPaste(${JSON.stringify(id)}, ${JSON.stringify(text)})`);
      if (!delivered) await notify('Paste canceled because the target changed.', true);
      else if (!text) await notify('The clipboard contains no text.');
    })().catch(async () => {
      if (!win.isDestroyed()) await notify('Paste unavailable. Use the Paste toolbar button or your terminal paste shortcut.', true);
    }).finally(() => {
      pending = false;
      callback(false);
    });
  });
}

module.exports = { installContextPaste };
