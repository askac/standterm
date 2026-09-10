'use strict';

const { contextBridge, ipcRenderer } = require('electron');

// Loaded only in the bundled Desktop toolbar, never Core or a floating window.
contextBridge.exposeInMainWorld('desktopToolbar', Object.freeze({
  invoke: action => ipcRenderer.invoke('standterm-toolbar-action', action),
  onState: callback => ipcRenderer.on('standterm-toolbar-state', (_event, state) => callback(state)),
}));
