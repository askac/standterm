'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { app, clipboard, dialog, Menu } = require('electron');

async function until(check) {
  const deadline = Date.now() + 15000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error('Capture state timed out.');
    await new Promise(resolve => setTimeout(resolve, 50));
  }
}

async function run(win, capture) {
  const outputRoot = app.isPackaged ? app.getPath('temp') : path.join(__dirname, '..', 'dist');
  await fs.mkdir(outputRoot, { recursive: true });
  const directory = await fs.mkdtemp(path.join(outputRoot, 'capture-smoke-'));
  const screenshot = path.join(directory, 'standterm.png');
  const video = path.join(directory, 'standterm.webm');
  await new Promise(resolve => setTimeout(resolve, 300));
  const shot = await capture.screenshot('file', screenshot);
  assert.equal(shot.destination, screenshot);
  assert.equal((await fs.readFile(screenshot)).subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  // Exercise PNG clipboard packaging without reading or replacing user data.
  const originalWrite = clipboard.write;
  let clipboardBytes;
  clipboard.write = async items => {
    assert.deepEqual(items[0].types, ['image/png']);
    clipboardBytes = Buffer.from(await (await items[0].getType('image/png')).arrayBuffer());
  };
  try { assert.equal((await capture.screenshot('clipboard')).copied, true); }
  finally { clipboard.write = originalWrite; }
  assert.equal(clipboardBytes.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  const originalSave = dialog.showSaveDialog;
  dialog.showSaveDialog = async () => ({ canceled: true });
  try {
    assert.equal(await capture.start(), null);
    assert.equal(await capture.screenshot('file'), null);
    assert.equal(capture.active, false);
    let cancelDialog;
    dialog.showSaveDialog = () => new Promise(resolve => { cancelDialog = resolve; });
    const starting = capture.start();
    const stopping = capture.stop();
    cancelDialog({ canceled: true });
    assert.deepEqual(await Promise.all([starting, stopping]), [null, null]);
    assert.equal(capture.active, false);
  } finally { dialog.showSaveDialog = originalSave; }
  await win.webContents.executeJavaScript(`{
    const marker = document.createElement('div'); marker.id = 'capture-smoke-marker';
    marker.style.cssText = 'position:fixed;inset:0 auto auto 0;width:100px;height:100px;background:rgb(0,240,0);z-index:2147483647';
    document.body.append(marker);
  }`);
  await new Promise(resolve => setTimeout(resolve, 300));
  await capture.screenshot('file', path.join(directory, 'source.png'));
  const started = await capture.start(video);
  console.log('Capture smoke: recording started.');
  assert.equal(started.destination, video);
  assert.equal(capture.state, 'recording');
  assert.equal(Menu.getApplicationMenu().getMenuItemById('capture-start').enabled, false);
  assert.equal(Menu.getApplicationMenu().getMenuItemById('capture-stop').enabled, true);
  assert.equal((await capture.start(path.join(directory, 'duplicate.webm'))).destination, video);
  assert.match(win.getTitle(), /REC/);
  const privatePrefs = capture.job.recorder.webContents.getLastWebPreferences();
  assert.equal(privatePrefs.sandbox, true);
  assert.equal(privatePrefs.nodeIntegration, false);
  assert.equal(privatePrefs.preload, undefined);
  assert.equal(await win.webContents.executeJavaScript('typeof window.recorder'), 'undefined');
  assert.equal(await capture.job.recorder.webContents.executeJavaScript('document.cookie'), '');
  const denied = await capture.job.recorder.webContents.executeJavaScript(`
    navigator.mediaDevices.getDisplayMedia({video: true}).then(stream => {
      stream.getTracks().forEach(track => track.stop()); return false;
    }, () => true)`, true);
  assert.equal(denied, true, 'a second media request must not reuse the native recording grant');
  console.log('Capture smoke: repeated display capture denied.');
  for (const contents of [win.webContents, capture.job.recorder.webContents]) {
    const cameraDenied = await contents.executeJavaScript(`
      navigator.mediaDevices.getUserMedia({video: true, audio: true}).then(stream => {
        stream.getTracks().forEach(track => track.stop()); return false;
      }, () => true)`, true);
    assert.equal(cameraDenied, true, 'camera/microphone access must remain denied');
    console.log('Capture smoke: camera/microphone request denied.');
  }
  const pageDenied = await win.webContents.executeJavaScript(`
    navigator.mediaDevices.getDisplayMedia({video: true}).then(stream => {
      stream.getTracks().forEach(track => track.stop()); return false;
    }, () => true)`, true);
  assert.equal(pageDenied, true, 'the terminal page must not start a capture');
  console.log('Capture smoke: terminal display capture denied.');
  const originalMessage = dialog.showMessageBox;
  dialog.showMessageBox = async () => ({ response: 0 });
  try {
    win.close();
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(win.isDestroyed(), false);
    assert.equal(capture.state, 'recording', 'canceling close keeps recording');
  } finally { dialog.showMessageBox = originalMessage; }
  for (let index = 0; index < 4; index++) {
    await win.webContents.executeJavaScript(`window.terminalTest.writeTerminalOutput(${JSON.stringify('\r\nCapture smoke frame ') } + ${index})`);
    await new Promise(resolve => setTimeout(resolve, 750));
  }
  const [stopped, concurrent] = await Promise.all([capture.stop(), capture.stop()]);
  assert.deepEqual(stopped, concurrent);
  assert.equal(stopped.destination, video);
  assert.equal(capture.active, false);
  assert.ok(stopped.bytes > 1000);
  const bytes = await fs.readFile(video);
  assert.equal(bytes.subarray(0, 4).toString('hex'), '1a45dfa3');
  // Decode the result inside Chromium, without enabling file access in Core.
  const decoded = await win.webContents.executeJavaScript(`new Promise((resolve, reject) => {
    const bytes = Uint8Array.from(atob(${JSON.stringify(bytes.toString('base64'))}), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], {type: 'video/webm'}));
    const video = document.createElement('video');
    const timer = setTimeout(() => { URL.revokeObjectURL(url); reject(new Error('Video decode timed out')); }, 10000);
    // MediaRecorder can emit an initial black frame while capture warms up.
    video.onloadeddata = () => { video.currentTime = 1; };
    video.onseeked = () => {
      clearTimeout(timer); URL.revokeObjectURL(url);
      const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      const context = canvas.getContext('2d'); context.drawImage(video, 0, 0);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let markerPixels = 0;
      for (let offset = 0; offset < pixels.length; offset += 4) {
        if (pixels[offset] < 60 && pixels[offset + 1] > 180 && pixels[offset + 2] < 60) markerPixels++;
      }
      resolve({width: video.videoWidth, height: video.videoHeight, markerPixels, png: canvas.toDataURL('image/png')});
    };
    video.onerror = () => { clearTimeout(timer); URL.revokeObjectURL(url); reject(new Error('Video decode failed')); };
    video.muted = true; video.src = url; video.load();
  })`);
  assert.ok(decoded.width > 0 && decoded.height > 0);
  await fs.writeFile(path.join(directory, 'decoded-frame.png'), Buffer.from(decoded.png.split(',')[1], 'base64'), { flag: 'wx' });
  assert.ok(decoded.markerPixels > 300,
    'video must contain the marker from the StandTerm page, not the recorder or desktop');
  await win.webContents.executeJavaScript("document.getElementById('capture-smoke-marker').remove()");
  assert.equal((await fs.readdir(directory)).some(name => name.endsWith('.partial')), false);
  const originalNotify = capture.notify;
  capture.notify = async () => {};
  try {
    assert.equal((await capture.start(video)).error, true);
    assert.deepEqual(await fs.readFile(video), bytes, 'existing recordings must not be replaced');
    const failure = path.join(directory, 'failed.webm');
    await capture.start(failure);
    await new Promise(resolve => setTimeout(resolve, 1200));
    capture.job.error = new Error('Injected recording failure');
    const failed = await capture.stop();
    assert.equal(failed.error, true);
    assert.ok((await fs.stat(failed.partial)).size >= 0);
    await assert.rejects(fs.stat(failure), { code: 'ENOENT' });
  } finally { capture.notify = originalNotify; }
  const hidden = path.join(directory, 'hidden.webm');
  await capture.start(hidden);
  await new Promise(resolve => setTimeout(resolve, 1200));
  win.hide();
  await until(() => !capture.active);
  assert.ok((await fs.stat(hidden)).size > 0, 'hiding the window must finalize the recording');
  win.show();
  const confirmed = path.join(directory, 'confirmed.webm');
  await capture.start(confirmed);
  await new Promise(resolve => setTimeout(resolve, 1200));
  dialog.showMessageBox = async () => ({ response: 1 });
  try {
    assert.equal(await capture.confirmStop('closing the test window'), true);
    assert.equal(capture.active, false);
    assert.ok((await fs.stat(confirmed)).size > 0);
  } finally { dialog.showMessageBox = originalMessage; }
  console.log(`Capture smoke passed: PNG and decodable silent WebM saved in ${directory}`);
}

module.exports = { run };
