'use strict';

// Run with checkout Electron. Exercise main's packaged failure/relaunch path
// without opening Core, touching an installed profile or actually relaunching.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { spawn } = require('node:child_process');
const { stopOwnedBackend } = require('../backend-stop.cjs');
const electron = require('electron');
const { app, dialog } = electron;
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'standterm-core-failure-'));
fs.mkdirSync(path.join(root, 'bundle'));
fs.writeFileSync(path.join(root, 'bundle', 'manifest.json'), JSON.stringify({ id: 'a'.repeat(64) }));
app.setPath('appData', root);
Object.defineProperty(app, 'isPackaged', { value: true });
Object.defineProperty(process, 'resourcesPath', { value: root });
const setup = require('../setup.cjs');
const duringLoad = process.argv.includes('--exit-during-load');
const captureCancel = process.argv.includes('--capture-cancel');
let backend;
let viewLoaded = false;
if (duringLoad) {
  const index = process.argv.indexOf('--exit-during-load');
  const [python, stage] = process.argv.slice(index + 1);
  assert.ok(path.isAbsolute(python) && path.isAbsolute(stage));
  process.env.STANDTERM_AGENT_RUNTIME_DIR = path.join(root, 'agent');
  process.env.STANDTERM_SESSION_RECOVERY_STORE = path.join(root, 'session-recovery.json');
  setup.preparePackagedBackend = async () => ({ executable: python,
    args: ['-u', path.join(stage, 'bundle', 'core', 'desktop', 'backend.py')], cwd: path.join(stage, 'bundle', 'core') });
} else setup.preparePackagedBackend = async () => { throw new Error('Synthetic broken Core'); };
let dialogs = 0;
let relaunched = false;
dialog.showMessageBox = async options => {
  assert.equal(options.defaultId, 0);
  if (dialogs++ === 0) {
    assert.equal(options.title, 'StandTerm Core is unavailable');
    assert.equal(options.message, duringLoad ? 'The owned Core exited during startup.' : 'Synthetic broken Core');
    return { response: 2 };
  }
  assert.equal(options.title, 'Change StandTerm Core');
  return { response: 1 };
};
dialog.showErrorBox = (_title, message) => { console.error(message); originalExit(1); };
app.relaunch = () => { relaunched = true; };
const originalExit = app.exit.bind(app);
app.exit = code => {
  try {
    assert.equal(code, 0);
    assert.equal(dialogs, 2);
    assert.equal(relaunched, !captureCancel);
    if (duringLoad) assert.equal(viewLoaded, true);
    const saved = JSON.parse(fs.readFileSync(path.join(root, 'StandTermDesktopEvaluation',
      process.platform === 'darwin' ? 'macos' : 'windows', 'core-source.json')));
    assert.equal(saved.source, 'bundled');
    assert.equal(saved.pending, captureCancel ? null : 'recover');
    console.log(`Desktop Core failure smoke passed: ${duringLoad ? 'backend exit during page load' : captureCancel
      ? 'capture cancellation preserves source and clears restart intent' : 'native recovery choices without Core'}.`);
    originalExit(0);
  } catch (error) { console.error(error.stack); originalExit(1); }
};
setTimeout(() => { console.error('Core failure smoke timed out.'); originalExit(1); }, 90000).unref();
class View extends electron.WebContentsView {
  constructor(options) {
    super(options);
    const load = this.webContents.loadURL.bind(this.webContents);
    this.webContents.loadURL = async url => {
      const result = await load(url);
      viewLoaded = true;
      await stopOwnedBackend(backend);
      return result;
    };
  }
}
const directory = path.resolve(__dirname, '..');
const context = vm.createContext({ process, console, Buffer, URL, setTimeout, clearTimeout,
  __dirname: directory, require: name => name === 'electron' ? { ...electron, ...(duringLoad ? { WebContentsView: View } : {}) }
    : name === 'node:child_process' ? { spawn: (...args) => (backend = spawn(...args)) }
      : name.startsWith('./') ? require(path.join(directory, name)) : require(name) });
vm.runInContext('(function () {\n' + fs.readFileSync(path.join(directory, 'main.cjs'), 'utf8')
  + '\nglobalThis.injectCapture = value => { capture = value; };\n})()', context);
if (captureCancel) context.injectCapture({ active: true, confirmStop: async () => {
  setTimeout(() => app.exit(0), 0);
  return false;
} });
