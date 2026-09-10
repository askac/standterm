'use strict';

const { app, BrowserWindow, Menu, clipboard, ClipboardItem, dialog, session, shell } = require('electron');
const fs = require('node:fs/promises');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { randomUUID } = require('node:crypto');
const { CaptureFile, MAX_CHUNK_BYTES } = require('./capture-file.cjs');
const { CaptureSettings, captureName } = require('./capture-settings.cjs');

const RECORDER_URL = pathToFileURL(path.join(__dirname, 'recorder.html')).href;
const RECORDER_SCRIPT_URL = pathToFileURL(path.join(__dirname, 'recorder.js')).href;
const CAPTURE_TIMEOUT_MS = 15000;

async function bounded(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new Error('Capture timed out.')), CAPTURE_TIMEOUT_MS);
    })]);
  } finally { clearTimeout(timer); }
}

class DesktopCapture {
  constructor(win, { contents = win.webContents, onChange = () => {}, onDiagnostic = () => {}, notify = null } = {}) {
    this.win = win;
    this.contents = contents;
    this.onChange = onChange;
    this.onDiagnostic = onDiagnostic;
    this.notify = notify || ((message, error = false) => dialog.showMessageBox(win, {
      type: error ? 'error' : 'info', title: 'StandTerm Capture', message,
    }));
    this.state = 'idle';
    this.job = null;
    this.directory = app.getPath('downloads');
    this.settings = new CaptureSettings(path.join(app.getPath('userData'), 'capture-settings.json'));
    this.folderSelection = null;
    this.screenshotBusy = false;
    // Reuse one private partition; Electron retains sessions for the app lifetime.
    this.recorderPartition = `standterm-recorder-${randomUUID()}`;
  }

  get active() { return this.state !== 'idle'; }

  update() {
    const menu = Menu.getApplicationMenu();
    const elapsed = this.job?.startedAt ? Math.floor(((this.job.pausedAt || Date.now()) - this.job.startedAt - (this.job.pausedMs || 0)) / 1000) : 0;
    const clock = `${Math.floor(elapsed / 60).toString().padStart(2, '0')}:${(elapsed % 60).toString().padStart(2, '0')}`;
    const label = this.state === 'paused' ? `Recording paused ${clock}` : this.state === 'recording' ? `Recording ${clock}`
      : this.state === 'idle' ? 'Not recording' : `${this.state === 'starting' ? 'Starting' : 'Saving'} recording...`;
    const status = menu?.getMenuItemById('capture-status');
    if (status) status.label = label;
    const start = menu?.getMenuItemById('capture-start');
    if (start) start.enabled = !this.active;
    const stop = menu?.getMenuItemById('capture-stop');
    if (stop) stop.enabled = ['recording', 'paused'].includes(this.state);
    this.onChange(this.active ? `${this.state === 'paused' ? 'PAUSED' : 'REC'} ${clock}` : '', {
      state: this.state, label, screenshotBusy: this.screenshotBusy,
    });
  }

  menu() {
    return { label: 'Capture', submenu: [
      { label: 'Copy screenshot', accelerator: 'CommandOrControl+Alt+S', click: () => this.screenshot('clipboard') },
      { label: 'Save screenshot (PNG)', click: () => this.screenshot('file') },
      { type: 'separator' },
      { id: 'capture-start', label: 'Start recording (WebM)', click: () => this.start() },
      { id: 'capture-stop', label: 'Stop and save recording', accelerator: 'CommandOrControl+Alt+R',
        enabled: false, click: () => this.stop() },
      { id: 'capture-status', label: 'Not recording', enabled: false },
    ] };
  }

  async chooseFile(extension) {
    let directory = this.settings.get(extension);
    if (directory) {
      try {
        if (!(await fs.stat(directory)).isDirectory()) throw new Error('Not a folder.');
        await fs.access(directory, fs.constants.W_OK);
      } catch {
        await this.notify('The saved capture folder is unavailable. Choose a writable folder again.', true);
        directory = null;
      }
    }
    if (!directory) directory = await this.chooseDirectory(extension);
    return directory ? path.join(directory, captureName(extension)) : null;
  }

  async chooseDirectory(extension) {
    if (this.folderSelection) { await this.folderSelection; return this.settings.get(extension); }
    this.folderSelection = (async () => {
      const result = await dialog.showOpenDialog(this.win, {
        title: extension === 'png' ? 'Choose screenshot folder' : 'Choose recording folder',
        message: 'Future captures save here automatically. Change this in StandTerm > Capture Settings.',
        defaultPath: this.settings.get(extension) || this.directory,
        properties: ['openDirectory', 'createDirectory'], buttonLabel: 'Use this folder',
      });
      if (result.canceled || !result.filePaths?.[0]) return null;
      const directory = result.filePaths[0];
      if (!(await fs.stat(directory)).isDirectory()) throw new Error('Choose a folder.');
      await fs.access(directory, fs.constants.W_OK);
      this.settings.set(extension, directory);
      return directory;
    })();
    try { return await this.folderSelection; } finally { this.folderSelection = null; }
  }

  async configure() {
    try {
      const { response } = await dialog.showMessageBox(this.win, {
        title: 'Capture Settings', message: 'Desktop capture folders',
        detail: `Screenshots: ${this.settings.get('png') || 'Choose on first save'}\nRecordings: ${this.settings.get('webm') || 'Choose on first recording'}\n\nPNG screenshots and silent WebM recordings are saved automatically.`,
        buttons: ['Done', 'Screenshot folder...', 'Recording folder...', 'Open screenshot folder', 'Open recording folder'],
        defaultId: 0, cancelId: 0, noLink: true,
      });
      if (response === 1 || response === 2) await this.chooseDirectory(response === 1 ? 'png' : 'webm');
      if (response === 3 || response === 4) {
        const directory = this.settings.get(response === 3 ? 'png' : 'webm');
        if (!directory) return;
        const error = await shell.openPath(directory);
        if (error) throw new Error('Could not open the capture folder.');
      }
    } catch (error) { await this.notify(error.message, true); }
  }

  async screenshot(kind, destination = null) {
    if (this.screenshotBusy) return;
    this.screenshotBusy = true;
    this.update();
    let output;
    try {
      if (this.win.isDestroyed() || !this.win.isVisible() || this.win.isMinimized()) {
        throw new Error('Show the StandTerm window before capturing it.');
      }
      // Capture before opening a dialog so the saved image is the requested view.
      let image;
      try {
        image = await bounded(this.contents.capturePage());
        if (image.isEmpty()) throw new Error('Empty capture.');
      } catch (error) {
        if (!this.win.isDestroyed()) {
          const [width, height] = this.win.getContentSize();
          this.onDiagnostic({ width, height, visible: this.win.isVisible(),
            minimized: this.win.isMinimized(), focused: this.win.isFocused() });
        }
        throw new Error('The terminal view could not be captured. Bring StandTerm to the foreground and retry. Window state is available in Diagnostics.', { cause: error });
      }
      const png = image.toPNG();
      if (kind === 'clipboard') {
        await clipboard.write([new ClipboardItem({ 'image/png': new Blob([png], { type: 'image/png' }) })]);
        await this.notify('Screenshot copied to the clipboard.');
        return { copied: true };
      }
      destination = destination || await this.chooseFile('png');
      if (!destination) return null;
      output = await CaptureFile.create(destination);
      for (let offset = 0; offset < png.length; offset += MAX_CHUNK_BYTES) {
        await output.write(png.subarray(offset, offset + MAX_CHUNK_BYTES));
      }
      await output.finish();
      await this.notify(`Screenshot saved to:\n${destination}`);
      return { destination };
    } catch (error) {
      await output?.close().catch(() => {});
      await this.notify(`${error.message}${output?.partial ? `\nUnfinished file retained at:\n${output.partial}` : ''}`, true);
      return { error: true };
    } finally { this.screenshotBusy = false; this.update(); }
  }

  start(destination = null) {
    if (this.active) return this.starting;
    this.state = 'starting';
    this.update();
    this.starting = this.begin(destination);
    return this.starting;
  }

  async begin(destination) {
    const job = { grantPending: false, recorder: null, output: null, error: null };
    this.job = job;
    try {
      destination = destination || await this.chooseFile('webm');
      if (!destination) { this.job = null; this.state = 'idle'; this.update(); return null; }
      if (this.win.isDestroyed() || !this.win.isVisible() || this.win.isMinimized()) {
        throw new Error('Show the StandTerm window before recording it.');
      }
      if (this.win.isFullScreen()) throw new Error('Leave fullscreen before recording so the recording indicator remains visible.');
      job.output = await CaptureFile.create(destination);
      const isolated = session.fromPartition(this.recorderPartition);
      const trustedRecorder = contents => contents === job.recorder?.webContents
        && contents?.mainFrame.url === RECORDER_URL && this.job === job && job.grantPending;
      isolated.setPermissionCheckHandler((contents, permission) => trustedRecorder(contents)
        && permission === 'display-capture');
      isolated.setPermissionRequestHandler((contents, permission, callback, details) => {
        // Chromium reports display capture as media with no camera/mic types.
        // Real device requests carry types and remain denied. The source handler
        // below independently permits only the fixed StandTerm frame, no audio.
        callback(
          trustedRecorder(contents) && details.isMainFrame === true
          && (permission === 'display-capture' || (permission === 'media'
            && Array.isArray(details.mediaTypes) && details.mediaTypes.length === 0)),
        );
      });
      isolated.setDevicePermissionHandler(() => false);
      isolated.setDisplayMediaRequestHandler((request, callback) => {
        const allowed = this.job === job && job.grantPending && request.userGesture
          && request.videoRequested && !request.audioRequested
          && request.frame === job.recorder?.webContents.mainFrame
          && request.frame?.url === RECORDER_URL && !this.win.isDestroyed();
        if (!allowed) { callback({}); return; }
        job.grantPending = false;
        callback({ video: this.contents.mainFrame });
      });
      isolated.webRequest.onBeforeRequest((details, callback) => callback({
        cancel: ![RECORDER_URL, RECORDER_SCRIPT_URL].includes(details.url),
      }));
      job.recorder = new BrowserWindow({ show: false, webPreferences: {
        session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false,
        webSecurity: true, webviewTag: false, backgroundThrottling: false, devTools: false,
      } });
      job.recorder.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
      job.recorder.webContents.on('will-navigate', event => event.preventDefault());
      job.recorder.webContents.on('will-attach-webview', event => event.preventDefault());
      job.recorder.webContents.on('render-process-gone', () => {
        job.error = new Error('The recording process stopped unexpectedly.');
        if (['recording', 'paused'].includes(this.state)) void this.stop();
      });
      await bounded(job.recorder.loadURL(RECORDER_URL));
      job.grantPending = true;
      const result = await bounded(job.recorder.webContents.executeJavaScript(`window.recorder.start()
        .catch(error => ({error: true, message: String(error?.message || error)}))`, true));
      job.grantPending = false;
      if (result.error) throw new Error(result.message);
      if (result.audio !== false || !result.mimeType.startsWith('video/webm')) throw new Error('Invalid recorder format.');
      job.mimeType = result.mimeType;
      job.startedAt = Date.now();
      this.state = 'recording';
      this.update();
      job.timer = setInterval(() => { this.update(); void this.pump(job); }, 500);
      return { destination, mimeType: job.mimeType };
    } catch (error) {
      job.error = error;
      await this.finish(job, false);
      return { error: true };
    }
  }

  async drain(job) {
    const data = await bounded(job.recorder.webContents.executeJavaScript('window.recorder.drain()'));
    if (!Array.isArray(data.chunks) || data.chunks.length > 64) throw new Error('Invalid recording data.');
    for (const chunk of data.chunks) {
      if (!(chunk instanceof ArrayBuffer)) throw new Error('Invalid recording chunk.');
      await job.output.write(new Uint8Array(chunk));
    }
    if (data.error) throw new Error(`Recording stopped: ${data.error}`);
    if (data.stopped && ['recording', 'paused'].includes(this.state)) throw new Error('The recording stream ended.');
  }

  async pump(job) {
    if (this.job !== job || !['recording', 'paused'].includes(this.state) || job.pumping) return;
    job.pumping = this.drain(job);
    try { await job.pumping; } catch (error) {
      job.error = error;
      // Schedule after this pump is released; stop waits for an in-flight write.
      setTimeout(() => { if (this.job === job) void this.stop(); }, 0);
    } finally { job.pumping = null; }
  }

  async stop() {
    if (this.state === 'starting') await this.starting;
    if (!this.job) return null;
    if (this.stopping) return this.stopping;
    const job = this.job;
    this.state = 'stopping';
    clearInterval(job.timer);
    this.update();
    this.stopping = (async () => {
      try {
        if (job.pumping) await job.pumping;
        if (job.error) throw job.error;
        await bounded(job.recorder.webContents.executeJavaScript('window.recorder.stop()'));
        await this.drain(job);
        return await this.finish(job, true);
      } catch (error) {
        job.error = error;
        return this.finish(job, false);
      }
    })();
    try { return await this.stopping; } finally { this.stopping = null; }
  }

  async togglePause() {
    const job = this.job;
    if (!job || this.pausePending || !['recording', 'paused'].includes(this.state)) return;
    const pause = this.state === 'recording';
    this.pausePending = true;
    try {
      await bounded(job.recorder.webContents.executeJavaScript(`window.recorder.${pause ? 'pause' : 'resume'}()`));
      if (this.job !== job || !['recording', 'paused'].includes(this.state)) return;
      if (pause) job.pausedAt = Date.now();
      else { job.pausedMs = (job.pausedMs || 0) + Date.now() - job.pausedAt; job.pausedAt = null; }
      this.state = pause ? 'paused' : 'recording';
      this.update();
    } catch (error) { job.error = error; if (this.job === job) await this.stop(); }
    finally { this.pausePending = false; }
  }

  async finish(job, publish) {
    clearInterval(job.timer);
    job.grantPending = false;
    if (job.recorder && !job.recorder.isDestroyed()) job.recorder.destroy();
    let destination;
    try {
      if (publish) destination = await job.output.finish();
    } catch (error) { job.error = error; }
    await job.output?.close().catch(error => { job.error = job.error || error; });
    try {
      if (job.error) {
        await this.notify(`${job.error.message || String(job.error)}${job.output?.partial ? `\nUnfinished recording retained at:\n${job.output.partial}` : ''}`, true);
        return { error: true, partial: job.output?.partial };
      }
      await this.notify(`Recording saved to:\n${destination}`);
      return { destination, bytes: job.output.bytes, mimeType: job.mimeType };
    } finally {
      if (this.job === job) { this.job = null; this.state = 'idle'; this.update(); }
    }
  }

  async confirmStop(action) {
    if (!this.active) return true;
    const { response } = await dialog.showMessageBox(this.win, {
      type: 'question', title: 'StandTerm recording is active',
      message: `Stop and save the recording before ${action}?`,
      buttons: ['Keep recording', 'Stop and save'], defaultId: 0, cancelId: 0,
    });
    if (response !== 1) return false;
    await this.stop();
    return true;
  }
}

module.exports = { DesktopCapture };
