'use strict';

function agentMenu({ showHelp, uiItems = [] }) {
  return { id: 'agent-menu', label: 'Agent', submenu: [
    ...uiItems,
    { id: 'agent-help', label: 'Getting started...', click: showHelp },
  ] };
}

module.exports = { agentMenu };
