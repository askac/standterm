'use strict';
const assert = require('node:assert/strict');
const { dialog, shell } = require('electron');

async function run(contents, coreOverlay = false) {
  const originalDialog = dialog.showMessageBox;
  const originalOpen = shell.openExternal;
  const opened = [];
  let prompts = 0;
  let answer = 0;
  const url = 'https://example.com/standterm-preview-test';
  dialog.showMessageBox = async (_owner, options) => {
    assert.equal(options.defaultId, 0);
    assert.equal(options.cancelId, 0);
    assert.ok(options.detail.startsWith(url));
    prompts++;
    return { response: answer };
  };
  shell.openExternal = async value => { opened.push(value); };
  const click = async () => {
    if (coreOverlay) {
      await contents.executeJavaScript(`document.getElementById('open-overlay-option').dataset.url = ${JSON.stringify(url)};
        document.getElementById('open-overlay-option').click();
        document.getElementById('overlay-fallback-open').click();`, true);
      assert.equal(await contents.executeJavaScript("document.getElementById('overlay-fallback').style.display"), 'flex');
      assert.equal(await contents.executeJavaScript("document.getElementById('overlay-iframe').getAttribute('src')"), 'about:blank');
    } else await contents.executeJavaScript(`window.open(${JSON.stringify(url)}, '_blank', 'noopener,noreferrer'); void 0`, true);
    await new Promise(resolve => setTimeout(resolve, 150));
  };
  try {
    await click();
    assert.equal(prompts, 1);
    assert.deepEqual(opened, []);
    answer = 1;
    await click();
    assert.equal(prompts, 2);
    assert.deepEqual(opened, [url]);
    await contents.executeJavaScript("window.open('file:///C:/Windows/notepad.exe'); void 0", true);
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(prompts, 2);
    if (coreOverlay) await contents.executeJavaScript("document.getElementById('close-overlay').click()");
  } finally { dialog.showMessageBox = originalDialog; shell.openExternal = originalOpen; }
}

module.exports = { run };
