'use strict';

const WINDOWS_MODES = Object.freeze({ windows: 'StandTerm Desktop', wsl: 'StandTerm Desktop (WSL)' });
const MODES = Object.freeze({ ...WINDOWS_MODES, macos: 'StandTerm Desktop' });
const APP_ID = 'com.squirrel.StandTermDesktopEvaluation.StandTermDesktopEvaluation';

function desktopMode(argv, platform = process.platform) {
  const values = argv.filter(value => value.startsWith('--backend='));
  if (values.length > 1 || (values.length && !Object.hasOwn(MODES, values[0].slice(10)))) {
    throw new Error('Use exactly one supported backend: --backend=windows, --backend=wsl or --backend=macos.');
  }
  const mode = values.length ? values[0].slice(10) : platform === 'darwin' ? 'macos' : 'windows';
  if ((platform === 'darwin' && mode !== 'macos') || (platform !== 'darwin' && mode === 'macos')) {
    throw new Error('The macOS backend requires native macOS; Windows and WSL modes require Windows.');
  }
  return mode;
}

module.exports = { MODES, WINDOWS_MODES, APP_ID, desktopMode };
