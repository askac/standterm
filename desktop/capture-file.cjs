'use strict';

const fs = require('node:fs/promises');
const { randomUUID } = require('node:crypto');

const MAX_CHUNK_BYTES = 8 * 1024 * 1024;

class CaptureFile {
  static async create(destination) {
    try {
      await fs.lstat(destination);
      throw new Error('The destination already exists. Choose a new filename.');
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    const partial = `${destination}.${randomUUID()}.partial`;
    const handle = await fs.open(partial, 'wx', 0o600);
    return new CaptureFile(destination, partial, handle);
  }

  constructor(destination, partial, handle) {
    this.destination = destination;
    this.partial = partial;
    this.handle = handle;
    this.bytes = 0;
  }

  async write(data) {
    if (!(data instanceof Uint8Array) || data.byteLength > MAX_CHUNK_BYTES || !this.handle) {
      throw new Error('Invalid capture chunk or closed destination.');
    }
    let offset = 0;
    while (offset < data.byteLength) {
      const { bytesWritten } = await this.handle.write(data, offset, data.byteLength - offset);
      if (!bytesWritten) throw new Error('The capture destination stopped accepting data.');
      offset += bytesWritten;
      this.bytes += bytesWritten;
    }
  }

  async close() {
    if (!this.handle) return;
    const handle = this.handle;
    this.handle = null;
    await handle.close();
  }

  async finish() {
    if (!this.bytes) throw new Error('No capture frames were recorded.');
    if (this.handle) await this.handle.sync();
    await this.close();
    // Publish without overwriting a file created after the Save dialog. A hard
    // link in the same directory does not copy the recording's data again.
    await fs.link(this.partial, this.destination);
    await fs.unlink(this.partial);
    this.partial = null;
    return this.destination;
  }
}

module.exports = { CaptureFile, MAX_CHUNK_BYTES };
