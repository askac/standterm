'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'recorder.js'), 'utf8');

function fixture({ supported = true, audio = false } = {}) {
  let encoder;
  let stoppedTracks = 0;
  let requested;
  const track = new EventTarget();
  track.stop = () => { stoppedTracks++; };
  const stream = {
    getTracks: () => [track], getVideoTracks: () => [track],
    getAudioTracks: () => audio ? [track] : [],
  };
  class Encoder extends EventTarget {
    static isTypeSupported() { return supported; }
    constructor(input, options) {
      super(); encoder = this;
      assert.equal(input, stream);
      assert.equal(options.videoBitsPerSecond, 4000000);
      this.state = 'inactive';
    }
    start(slice) { assert.equal(slice, 1000); this.state = 'recording'; }
    pause() { this.state = 'paused'; }
    resume() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.dispatchEvent(new Event('stop')); }
    chunk(data) {
      const event = new Event('dataavailable'); event.data = data;
      this.dispatchEvent(event);
    }
  }
  const context = vm.createContext({ window: {}, MediaRecorder: Encoder, navigator: {
    mediaDevices: { getDisplayMedia: async options => { requested = options; return stream; } },
  } });
  vm.runInContext(source, context);
  return { api: context.window.recorder, track,
    encoder: () => encoder, requested: () => requested, stoppedTracks: () => stoppedTracks };
}

test('private recorder requests video only and drains exact chunks', async () => {
  const f = fixture();
  const format = await f.api.start();
  assert.equal(format.audio, false);
  assert.match(format.mimeType, /^video\/webm/);
  assert.equal(f.requested().audio, false);
  assert.equal(f.requested().video.frameRate, 30);
  f.encoder().chunk(new Blob(['first']));
  f.encoder().chunk(new Blob(['second']));
  const batch = await f.api.drain();
  assert.equal(batch.chunks.map(bytes => Buffer.from(bytes).toString()).join(''), 'firstsecond');
  assert.equal((await f.api.drain()).chunks.length, 0);
  await f.api.stop();
  assert.equal((await f.api.drain()).stopped, true);
  assert.ok(f.stoppedTracks() > 0);
  await f.api.stop();
  await assert.rejects(f.api.start(), /already started/);
});

test('private recorder stops on chunk/queue limits and unexpected source loss', async () => {
  for (const kind of ['chunk', 'queue', 'source']) {
    const f = fixture();
    await f.api.start();
    if (kind === 'chunk') f.encoder().chunk({ size: 8 * 1024 * 1024 + 1 });
    if (kind === 'queue') {
      const chunk = { size: 8 * 1024 * 1024, arrayBuffer: async () => new ArrayBuffer(0) };
      for (let count = 0; count < 5; count++) f.encoder().chunk(chunk);
    }
    if (kind === 'source') f.track.dispatchEvent(new Event('ended'));
    const batch = await f.api.drain();
    assert.equal(batch.error, kind === 'source' ? 'recording_source_ended' : 'recording_buffer_full');
    assert.equal(batch.stopped, true);
    assert.ok(f.stoppedTracks() > 0);
  }
});

test('private recorder rejects missing codecs or unexpected audio tracks', async () => {
  const unsupported = fixture({ supported: false });
  await assert.rejects(unsupported.api.start(), /not available/);
  assert.equal(unsupported.requested(), undefined);
  const audio = fixture({ audio: true });
  await assert.rejects(audio.api.start(), /video-only/);
  assert.ok(audio.stoppedTracks() > 0);
});

test('private recorder can pause, resume, and stop while paused', async () => {
  const f = fixture();
  await f.api.start();
  f.api.pause();
  assert.equal(f.encoder().state, 'paused');
  f.api.pause();
  f.api.resume();
  assert.equal(f.encoder().state, 'recording');
  f.api.pause();
  await f.api.stop();
  assert.equal(f.encoder().state, 'inactive');
  f.api.resume();
  assert.equal(f.encoder().state, 'inactive');
});
