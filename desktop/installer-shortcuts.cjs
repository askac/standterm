'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { WINDOWS_MODES: MODES, APP_ID } = require('./desktop-mode.cjs');

function shortcutPlan(executable, desktop, programs, selected) {
  if (selected.some(mode => !Object.hasOwn(MODES, mode))) throw new Error('Invalid shortcut mode.');
  return Object.entries(MODES).flatMap(([mode, title]) => [desktop, programs].map(folder => ({
    path: path.join(folder, `${title}.lnk`), selected: selected.includes(mode),
    options: { target: executable, args: `--backend=${mode}`, cwd: path.dirname(executable),
      description: title, icon: executable, iconIndex: 0, appUserModelId: `${APP_ID}.${mode}` },
  })));
}

async function applyShortcuts(shell, plan) {
  // Validate the whole plan before writing. A matching display name is not ownership.
  const checked = plan.map(item => {
    const exists = fs.existsSync(item.path);
    let owned = false;
    if (exists) {
      const previous = shell.readShortcutLink(item.path);
      owned = path.resolve(previous.target).toLowerCase() === path.resolve(item.options.target).toLowerCase()
        && previous.args === item.options.args;
      if (!owned && item.selected) throw new Error(`Another shortcut already uses ${path.basename(item.path)}.`);
    }
    return { ...item, exists, owned };
  });
  for (const item of checked) {
    if (item.selected) {
      fs.mkdirSync(path.dirname(item.path), { recursive: true });
      if (!shell.writeShortcutLink(item.path, item.exists ? 'update' : 'create', item.options)) {
        throw new Error(`Could not create ${path.basename(item.path)}.`);
      }
    } else if (item.owned) await shell.trashItem(item.path);
  }
}

module.exports = { shortcutPlan, applyShortcuts };
