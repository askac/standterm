'use strict';

function stopOwnedBackend(child, { gracefulMs = 5000, terminateMs = 15000 } = {}) {
  if (!child || (child.spawnfile && !child.pid) || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let forced;
    function exited() { clearTimeout(graceful); clearTimeout(forced); resolve(); }
    const graceful = setTimeout(() => {
      forced = setTimeout(() => {
        child.removeListener('exit', exited);
        reject(new Error('The owned backend did not stop. No Core update or restart was started.'));
      }, terminateMs);
      child.kill();
    }, gracefulMs);
    child.once('exit', exited);
    child.stdin.end();
  });
}

module.exports = { stopOwnedBackend };
