'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { MODES, APP_ID } = require('./desktop-mode.cjs');

const EVENTS = new Set(['--squirrel-install', '--squirrel-updated', '--squirrel-uninstall', '--squirrel-obsolete']);
function isSquirrelEvent(argv) { return EVENTS.has(argv[1]); }

function shortcutSpecs(executable, desktop, programs) {
  const target = path.resolve(path.dirname(executable), '..', 'Update.exe');
  return Object.entries(MODES).flatMap(([mode, title]) => [desktop, programs].map(folder => ({
    path: path.join(folder, `${title}.lnk`),
    options: {
      target, args: `--processStart "${path.basename(executable)}" --process-start-args "--backend=${mode}"`,
      cwd: path.dirname(target), description: title, icon: executable, iconIndex: 0, appUserModelId: `${APP_ID}.${mode}`,
    },
  })));
}

async function handleSquirrelEvent(app, shell, argv, executable = process.execPath) {
  if (!isSquirrelEvent(argv)) return false;
  if (argv[1] === '--squirrel-obsolete') return true;
  await app.whenReady();
  const specs = shortcutSpecs(executable, app.getPath('desktop'), path.join(app.getPath('appData'),
    'Microsoft', 'Windows', 'Start Menu', 'Programs'));
  for (const spec of specs) {
    const exists = fs.existsSync(spec.path);
    if (exists) {
      // Do not overwrite/remove another launcher merely because names match.
      const previous = shell.readShortcutLink(spec.path);
      if (path.resolve(previous.target).toLowerCase() !== path.resolve(spec.options.target).toLowerCase()
          || previous.args !== spec.options.args) {
        if (argv[1] === '--squirrel-uninstall') continue;
        throw new Error(`Another shortcut already uses ${path.basename(spec.path)}.`);
      }
    }
    if (argv[1] === '--squirrel-uninstall') {
      if (exists && !await shell.trashItem(spec.path).then(() => true, () => false)) {
        throw new Error('Could not remove an owned StandTerm shortcut.');
      }
    } else {
      fs.mkdirSync(path.dirname(spec.path), { recursive: true });
      if (!shell.writeShortcutLink(spec.path, exists ? 'update' : 'create', spec.options)) {
        throw new Error(`Could not create ${path.basename(spec.path)}.`);
      }
    }
  }
  return true;
}

module.exports = { isSquirrelEvent, handleSquirrelEvent, shortcutSpecs };
