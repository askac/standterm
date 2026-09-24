'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const SAVE_DELAY_MS = 250;
const validBounds = value => value && ['x', 'y', 'width', 'height'].every(key => Number.isInteger(value[key])
  && value[key] >= -2147483648 && value[key] <= 2147483647)
  && value.width > 0 && value.height > 0;

function loadWindowState(file, screen) {
  let saved;
  try {
    if (fs.statSync(file).size <= 16384) {
      const data = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (data?.version === 1 && validBounds(data.bounds) && typeof data.maximized === 'boolean') saved = data;
    }
  } catch { /* Missing or invalid state uses the default window. */ }
  const primary = screen.getPrimaryDisplay().workArea;
  let area = primary;
  if (saved) {
    const match = screen.getDisplayMatching(saved.bounds).workArea;
    const b = saved.bounds;
    if (b.x < match.x + match.width && b.x + b.width > match.x
        && b.y < match.y + match.height && b.y + b.height > match.y) area = match;
  }
  const minWidth = Math.min(640, area.width), minHeight = Math.min(480, area.height);
  const width = Math.min(area.width, Math.max(minWidth, saved?.bounds.width || 1280));
  const height = Math.min(area.height, Math.max(minHeight, saved?.bounds.height || 850));
  const x = Math.max(area.x, Math.min(saved?.bounds.x ?? area.x + Math.floor((area.width - width) / 2), area.x + area.width - width));
  const y = Math.max(area.y, Math.min(saved?.bounds.y ?? area.y + Math.floor((area.height - height) / 2), area.y + area.height - height));
  return { options: { x, y, width, height, minWidth, minHeight }, maximized: saved?.maximized || false };
}

function trackWindowState(win, file, initial, onError = () => {}) {
  let bounds = { x: initial.options.x, y: initial.options.y, width: initial.options.width, height: initial.options.height };
  let maximized = initial.maximized;
  let timer;
  let lastSaved;
  function remember() {
    // Minimizing can change the native maximized flag. Keep the last visible state.
    if (!win.isDestroyed() && win.isVisible() && !win.isMinimized() && !win.isFullScreen()) {
      maximized = win.isMaximized();
      const normal = win.getNormalBounds();
      if (validBounds(normal)) {
        // Windows display scaling can round a restored coordinate by one DIP.
        // Keep the requested value in that case so each launch does not grow it.
        bounds = Object.fromEntries(Object.keys(bounds).map(key => [key,
          Math.abs(normal[key] - bounds[key]) <= 1 ? bounds[key] : normal[key]]));
      }
    }
  }
  function save() {
    clearTimeout(timer);
    remember();
    const value = JSON.stringify({ version: 1, bounds, maximized }, null, 2);
    if (value === lastSaved) return;
    const temporary = `${file}.${randomUUID()}.tmp`;
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(temporary, value, { flag: 'wx', mode: 0o600 });
      fs.renameSync(temporary, file);
      lastSaved = value;
    } catch { onError(); }
    finally { try { fs.unlinkSync(temporary); } catch { /* No temporary file remains after a successful rename. */ } }
  }
  function changed() {
    // Remember before a following minimize or hide event obscures native state.
    remember();
    clearTimeout(timer);
    timer = setTimeout(save, SAVE_DELAY_MS);
    timer.unref();
  }
  for (const event of ['move', 'resize', 'maximize', 'unmaximize']) win.on(event, changed);
  win.on('close', save);
  win.on('closed', () => clearTimeout(timer));
  return { save };
}

module.exports = { loadWindowState, trackWindowState };
