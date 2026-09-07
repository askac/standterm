'use strict';

const MODES = Object.freeze({ windows: 'StandTerm Desktop', wsl: 'StandTerm Desktop (WSL)' });
const APP_ID = 'com.squirrel.StandTermDesktopEvaluation.StandTermDesktopEvaluation';

function desktopMode(argv) {
  const values = argv.filter(value => value.startsWith('--backend='));
  if (values.length > 1 || (values.length && !Object.hasOwn(MODES, values[0].slice(10)))) {
    throw new Error('Use exactly one supported backend: --backend=windows or --backend=wsl.');
  }
  return values.length ? values[0].slice(10) : 'windows';
}

module.exports = { MODES, APP_ID, desktopMode };
