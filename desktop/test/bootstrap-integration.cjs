'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');

async function run() {
  const [python, bundle, existingRoot] = process.argv.slice(2);
  if (!python || !bundle) throw new Error('Pass a prepared platform venv Python and staged bundle directory.');
  const directory = await fs.mkdtemp(path.join(__dirname, '..', 'dist', 'core-runtime-smoke-'));
  const root = existingRoot || path.join(directory, 'runtime');
  async function call(prepare) {
    const args = [path.join(__dirname, '..', 'bootstrap.py'), '--bundle', bundle, '--test-root', root,
      ...(prepare ? ['--prepare'] : [])];
    return new Promise((resolve, reject) => {
      const child = spawn(python, args, { stdio: ['pipe', 'pipe', 'pipe'] });
      let output = '';
      child.stdout.on('data', bytes => { output += bytes; process.stdout.write(bytes); });
      child.stderr.resume();
      child.on('error', reject);
      child.on('close', code => {
        if (code !== 0) reject(new Error(`Bootstrap exited with ${code}; inspect ${root}/setup.log`));
        else resolve(JSON.parse(output.trim().split('\n').at(-1)));
      });
    });
  }
  assert.equal((await call(false)).type, existingRoot ? 'ready' : 'needs_setup');
  const ready = await call(true);
  assert.equal(ready.type, 'ready');
  assert.equal(ready.root, root);
  assert.deepEqual(await call(false), ready);
  console.log(`Managed runtime smoke passed: ${root}`);
}

run().catch(error => { console.error(error.message); process.exitCode = 1; });
