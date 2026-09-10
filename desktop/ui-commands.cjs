'use strict';

const { BrowserWindow, Menu } = require('electron');
const { allowedNavigation } = require('./policy.cjs');

const UI_ACTIONS = Object.freeze({
  settings: 'Settings...', newTab: 'New terminal tab', closeTab: 'Close terminal tab',
  closeAll: 'Close all terminal tabs...', files: 'Files...', pip: 'Terminal to PiP',
  agentPanel: 'Show / hide Agent Panel', pauseAgent: 'Pause Agent for current terminal',
});

function createUiCommands(win, contents, origin) {
  let refreshing = false;
  const current = () => !win.isDestroyed() && !contents.isDestroyed() && allowedNavigation(contents.getURL(), origin);
  const focused = () => current() && BrowserWindow.getFocusedWindow() === win;
  async function snapshot() {
    if (!current()) return null;
    try {
      const state = await contents.executeJavaScript('window.standtermUi?.version === 1 ? window.standtermUi.snapshot() : null');
      return state?.version === 1 && state.ready === true && typeof state.actions === 'object' ? state : null;
    } catch { return null; }
  }
  async function run(action) {
    if (!Object.hasOwn(UI_ACTIONS, action) || !current()) return false;
    if (action === 'settings') { win.show(); win.focus(); contents.focus(); }
    if (!focused()) return false;
    const state = await snapshot();
    if (!state?.actions?.[action] || !focused()) return false;
    try {
      return await contents.executeJavaScript(`window.standtermUi?.run(${JSON.stringify(action)}, ${JSON.stringify(state.terminalId)})`, true);
    } catch { return false; }
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const state = await snapshot();
      for (const action of Object.keys(UI_ACTIONS)) {
        const item = Menu.getApplicationMenu()?.getMenuItemById(`ui-${action}`);
        if (item) item.enabled = !!state?.actions?.[action] && (action === 'settings' || focused());
      }
    } finally { refreshing = false; }
  }
  const timer = setInterval(() => { void refresh(); }, 750);
  win.once('closed', () => clearInterval(timer));
  win.on('focus', () => { void refresh(); });
  win.on('blur', () => { void refresh(); });
  return {
    run, refresh,
    item: action => ({ id: `ui-${action}`, label: UI_ACTIONS[action], enabled: false, click: () => run(action) }),
  };
}

module.exports = { createUiCommands, UI_ACTIONS };
