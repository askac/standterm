'use strict';

// Reuse the existing code-native terminal glyph; no external artwork is needed.
const fs = require('node:fs');
function writeIcon(destination) {
  const size = 256;
  const maskBytes = size * size / 8;
  const imageBytes = 40 + size * size * 4 + maskBytes;
  const icon = Buffer.alloc(22 + imageBytes);
  icon.writeUInt16LE(1, 2); icon.writeUInt16LE(1, 4);
  icon.writeUInt16LE(1, 10); icon.writeUInt16LE(32, 12);
  icon.writeUInt32LE(imageBytes, 14); icon.writeUInt32LE(22, 18);
  icon.writeUInt32LE(40, 22); icon.writeInt32LE(size, 26); icon.writeInt32LE(size * 2, 30);
  icon.writeUInt16LE(1, 34); icon.writeUInt16LE(32, 36);
  icon.writeUInt32LE(size * size * 4, 42);
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
    const a = Math.floor(x / 8), b = Math.floor(y / 8);
    const glyph = (a >= 7 && a <= 14 && Math.abs(b - 15) === a - 7)
      || (b >= 22 && b <= 24 && a >= 18 && a <= 25);
    const offset = 62 + ((size - 1 - y) * size + x) * 4;
    icon[offset] = glyph ? 255 : 35; icon[offset + 1] = glyph ? 255 : 35;
    icon[offset + 2] = glyph ? 255 : 35; icon[offset + 3] = 255;
  }
  fs.writeFileSync(destination, icon, { flag: 'wx' });
}
module.exports = { writeIcon };
