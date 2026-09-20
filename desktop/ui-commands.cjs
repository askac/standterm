'use strict';

const { BrowserWindow, Menu } = require('electron');
const { allowedNavigation } = require('./policy.cjs');
const { create } = require('./i18n.js');

const UI_ACTIONS = Object.freeze({
  settings: 'desktop.menu.settings', newTab: 'desktop.menu.new_tab', closeTab: 'desktop.menu.close_tab',
  closeAll: 'desktop.menu.close_all', files: 'desktop.menu.files', pip: 'desktop.menu.pip',
  agentPanel: 'desktop.menu.agent_panel', pauseAgent: 'desktop.menu.pause_agent',
});

function createUiCommands(win, contents, origin, t = create('en').t) {
  let refreshing = false;
  const current = () => !win.isDestroyed() && !contents.isDestroyed() && allowedNavigation(contents.getURL(), origin);
  const focused = () => current() && BrowserWindow.getFocusedWindow() === win;
  function edit(action) {
    if (!['copy', 'paste'].includes(action) || !focused() || !win.isVisible() || win.isMinimized()) return false;
    // Restore the Core editing target, including text fields. Never send Ctrl+C/V.
    contents.focus();
    contents[action]();
    return true;
  }
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
    run, refresh, edit,
    item: action => ({ id: `ui-${action}`, label: t(UI_ACTIONS[action]), enabled: false, click: () => run(action) }),
  };
}

module.exports = { createUiCommands, UI_ACTIONS };
