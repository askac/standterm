'use strict';

const { create } = require('./i18n.js');

function agentMenu({ showHelp, uiItems = [], t = create('en').t }) {
  return { id: 'agent-menu', label: t('desktop.agent.menu_title'), submenu: [
    ...uiItems,
    { id: 'agent-help', label: t('desktop.agent.getting_started'), click: showHelp },
  ] };
}

module.exports = { agentMenu };
