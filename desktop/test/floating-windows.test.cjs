'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { allowedFloatingWindow, allowedFilesDownload } = require('../policy.cjs');
const { installFloatingWindows } = require('../floating-windows.cjs');
const origin = 'http://127.0.0.1:45678';
const valid = { url: 'about:blank', disposition: 'new-window', frameName: 'standterm-floating',
  features: 'popup,width=720,height=620', referrer: { url: `${origin}/` } };

test('floating admission requires owned-origin blank-child request shape', () => {
  assert.equal(allowedFloatingWindow(valid, `${origin}/?debug=1`, origin), true);
  assert.equal(allowedFloatingWindow({ ...valid, referrer: { url: '', policy: 'no-referrer' } }, origin, origin), true);
  for (const change of [{ url: 'https://example.com' }, { url: `${origin}/` }, { url: 'about:blank#x' },
    { url: 'data:text/html,x' }, { disposition: 'other' }, { frameName: 'Files' },
    { features: 'nodeIntegration=yes' }, { postBody: {} }, { referrer: {} },
    { referrer: { url: 'http://127.0.0.1:45679/' } }]) {
    assert.equal(allowedFloatingWindow({ ...valid, ...change }, origin, origin), false);
  }
  assert.equal(allowedFloatingWindow(valid, 'https://example.com', origin), false);
});

test('Files downloads are exact-origin tickets, not arbitrary external links', () => {
  assert.equal(allowedFilesDownload(`${origin}/sftp/download/${'a'.repeat(32)}`, origin), true);
  for (const url of ['https://example.com/sftp/download/' + 'a'.repeat(32),
    `${origin}/sftp/download/x`, `${origin}/sftp/download/${'a'.repeat(32)}?redirect=1`,
    `${origin}/sftp/download/${'a'.repeat(32)}#x`, `${origin}/other/${'a'.repeat(32)}`]) {
    assert.equal(allowedFilesDownload(url, origin), false);
  }
});

test('floating children deny navigation/nesting and close with opener lifecycle', () => {
  const opener = new EventEmitter();
  opener.webContents = new EventEmitter();
  opener.webContents.getURL = () => origin;
  opener.webContents.setWindowOpenHandler = fn => { opener.handler = fn; };
  const downloads = [];
  opener.webContents.downloadURL = url => downloads.push(['opener', url]);
  installFloatingWindows(opener, origin);
  assert.equal(opener.handler(valid).action, 'allow');
  const child = new EventEmitter();
  child.webContents = new EventEmitter();
  child.webContents.setWindowOpenHandler = fn => { child.handler = fn; };
  child.webContents.downloadURL = url => downloads.push(['child', url]);
  child.removeMenu = () => {};
  child.isDestroyed = () => !!child.closed;
  child.close = () => { child.closed = true; child.emit('closed'); };
  opener.webContents.emit('did-create-window', child);
  assert.equal(opener.handler(valid).action, 'deny');
  assert.equal(child.handler(valid).action, 'deny');
  const ticket = `${origin}/sftp/download/${'a'.repeat(32)}`;
  assert.equal(opener.handler({ url: ticket }).action, 'deny');
  assert.equal(child.handler({ url: ticket }).action, 'deny');
  assert.deepEqual(downloads, [['opener', ticket], ['child', ticket]]);
  for (const details of [{ url: ticket, postBody: {} }, { url: 'https://example.com/' }]) {
    assert.equal(child.handler(details).action, 'deny');
  }
  opener.webContents.getURL = () => 'https://example.com/';
  assert.equal(child.handler({ url: ticket }).action, 'deny');
  assert.equal(opener.handler({ url: ticket }).action, 'deny');
  assert.equal(downloads.length, 2);
  opener.webContents.getURL = () => origin;
  child.emit('close');
  assert.equal(opener.handler(valid).action, 'allow', 'a closing child must not block its replacement');
  for (const name of ['will-navigate', 'will-frame-navigate', 'will-redirect', 'will-attach-webview', 'will-prevent-unload']) {
    let prevented = false;
    child.webContents.emit(name, { preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true);
  }
  opener.webContents.emit('did-start-navigation', {}, origin, true, true);
  assert.equal(child.closed, undefined);
  opener.webContents.emit('did-start-navigation', {}, origin, false, true);
  assert.equal(child.closed, true);
  assert.equal(opener.handler(valid).action, 'allow');
});
