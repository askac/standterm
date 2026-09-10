'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

class CaptureSettings {
  constructor(file) {
    this.file = file;
    this.directories = {};
    try {
      if (fs.statSync(file).size > 16384) return;
      const data = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (data.version !== 1) return;
      for (const kind of ['png', 'webm']) {
        if (typeof data[kind] === 'string' && path.isAbsolute(data[kind]) && !data[kind].includes('\0')) {
          this.directories[kind] = data[kind];
        }
      }
    } catch { /* Missing or invalid preferences require a new folder selection. */ }
  }

  get(kind) {
    if (!['png', 'webm'].includes(kind)) throw new Error('Invalid capture format.');
    return this.directories[kind] || null;
  }

  set(kind, directory) {
    this.get(kind);
    if (typeof directory !== 'string' || !path.isAbsolute(directory) || directory.includes('\0')) {
      throw new Error('Choose an absolute capture folder.');
    }
    const next = { ...this.directories, [kind]: directory };
    // The first chosen folder is shared until either format is customized.
    for (const format of ['png', 'webm']) if (!next[format]) next[format] = directory;
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const temporary = `${this.file}.${randomUUID()}.tmp`;
    try {
      fs.writeFileSync(temporary, JSON.stringify({ version: 1, ...next }, null, 2), { flag: 'wx', mode: 0o600 });
      fs.renameSync(temporary, this.file);
      this.directories = next;
    } finally {
      try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }
}

function captureName(extension, now = new Date()) {
  if (!['png', 'webm'].includes(extension)) throw new Error('Invalid capture format.');
  return `StandTerm-${now.toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}.${extension}`;
}

module.exports = { CaptureSettings, captureName };
