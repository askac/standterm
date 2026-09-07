'use strict';

// This local page has no backend cookies, Node integration, preload or IPC.
// Only the desktop main process calls this interface, never the terminal page.
window.recorder = (() => {
  const MAX_QUEUE_BYTES = 32 * 1024 * 1024;
  let stream;
  let recorder;
  let chunks = [];
  let queuedBytes = 0;
  let error = null;
  let stopped = false;
  let stoppedPromise;

  function stopTracks() {
    if (stream) for (const track of stream.getTracks()) track.stop();
  }

  function fail(code) {
    error = error || code;
    if (recorder && recorder.state !== 'inactive') recorder.stop();
    else stopTracks();
  }

  return Object.freeze({
    async start() {
      if (recorder || stream) throw new Error('Recorder already started.');
      const mimeType = ['video/webm;codecs=vp8', 'video/webm;codecs=vp9', 'video/webm']
        .find(type => MediaRecorder.isTypeSupported(type));
      if (!mimeType) throw new Error('WebM recording is not available in this Electron runtime.');
      try {
        stream = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: false });
        if (stream.getAudioTracks().length || stream.getVideoTracks().length !== 1) {
          throw new Error('Expected one video-only capture track.');
        }
        recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 4000000 });
        stoppedPromise = new Promise(resolve => {
          recorder.addEventListener('stop', () => { stopped = true; stopTracks(); resolve(); }, { once: true });
        });
        recorder.addEventListener('dataavailable', event => {
          if (!event.data.size) return;
          if (event.data.size > 8 * 1024 * 1024 || queuedBytes + event.data.size > MAX_QUEUE_BYTES) {
            fail('recording_buffer_full');
            return;
          }
          chunks.push(event.data);
          queuedBytes += event.data.size;
        });
        recorder.addEventListener('error', () => fail('recording_encoder_failed'));
        stream.getVideoTracks()[0].addEventListener('ended', () => {
          if (!stopped && recorder.state !== 'inactive') fail('recording_source_ended');
        });
        recorder.start(1000);
        return { mimeType, audio: false };
      } catch (failure) {
        stopTracks();
        throw failure;
      }
    },

    async drain() {
      const batch = chunks;
      chunks = [];
      // Keep in-flight blobs in the budget until conversion finishes.
      const data = [];
      for (const chunk of batch) {
        data.push(await chunk.arrayBuffer());
        queuedBytes -= chunk.size;
      }
      return { chunks: data, error, stopped };
    },

    async stop() {
      if (recorder && recorder.state !== 'inactive') recorder.stop();
      if (stoppedPromise) await stoppedPromise;
      stopTracks();
    },
  });
})();
