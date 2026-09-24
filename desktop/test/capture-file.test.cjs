'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { CaptureFile, MAX_CHUNK_BYTES } = require('../capture-file.cjs');

test('capture publishes exact bytes without overwriting existing files', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'standterm-capture-file-'));
  const target = path.join(directory, 'test.webm');
  const output = await CaptureFile.create(target);
  const partial = output.partial;
  await output.write(Buffer.from('first'));
  await output.write(Buffer.from('second'));
  await assert.rejects(fs.stat(target), { code: 'ENOENT' });
  await output.finish();
  assert.equal(await fs.readFile(target, 'utf8'), 'firstsecond');
  await assert.rejects(fs.stat(partial), { code: 'ENOENT' });
  await assert.rejects(CaptureFile.create(target), /already exists/);
  assert.equal(await fs.readFile(target, 'utf8'), 'firstsecond');
});

test('failed partial cleanup retains both published and partial paths without another publication', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'standterm-capture-unlink-'));
  const target = path.join(directory, 'test.webm');
  let links = 0;
  const api = { exports: {} };
  vm.runInNewContext(await fs.readFile(path.join(__dirname, '..', 'capture-file.cjs'), 'utf8'), {
    Uint8Array, module: api,
    require: name => name === 'node:fs/promises' ? { ...fs,
      link: async (...args) => { links++; return fs.link(...args); },
      unlink: async () => { throw new Error('Fixture partial cleanup failure'); },
    } : require(name),
  });
  const output = await api.exports.CaptureFile.create(target);
  await output.write(Buffer.from('recording bytes'));
  await assert.rejects(output.finish(), /Fixture partial cleanup failure/);
  assert.equal(await fs.readFile(target, 'utf8'), 'recording bytes');
  assert.equal(await fs.readFile(output.partial, 'utf8'), 'recording bytes');
  assert.equal(links, 1);
  assert.equal(output.handle, null);
});

test('capture retains partial output on a publish race or empty recording', async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'standterm-capture-race-'));
  const target = path.join(directory, 'test.webm');
  const output = await CaptureFile.create(target);
  await output.write(Buffer.from('recording'));
  await fs.writeFile(target, 'another writer', { flag: 'wx' });
  await assert.rejects(output.finish(), { code: 'EEXIST' });
  assert.equal(await fs.readFile(target, 'utf8'), 'another writer');
  assert.equal(await fs.readFile(output.partial, 'utf8'), 'recording');
  const empty = await CaptureFile.create(path.join(directory, 'empty.webm'));
  await assert.rejects(empty.finish(), /No capture frames/);
  await empty.close();
  assert.equal((await fs.stat(empty.partial)).size, 0);
});

test('capture enforces chunk size and handles short writes', async () => {
  const written = [];
  const output = new CaptureFile('unused', 'unused.partial', {
    write: async (bytes, offset, length) => {
      const count = Math.min(2, length);
      written.push(...bytes.subarray(offset, offset + count));
      return { bytesWritten: count };
    },
    close: async () => {},
  });
  await output.write(Buffer.from('short writes'));
  assert.equal(Buffer.from(written).toString(), 'short writes');
  await assert.rejects(output.write(new Uint8Array(MAX_CHUNK_BYTES + 1)), /Invalid capture chunk/);
  await assert.rejects(output.write('not bytes'), /Invalid capture chunk/);
  await output.close();
  await assert.rejects(output.write(Buffer.from('closed')), /Invalid capture chunk/);
});
