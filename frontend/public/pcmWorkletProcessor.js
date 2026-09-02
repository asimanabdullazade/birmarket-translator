/**
 * AudioWorkletProcessor that converts the browser's native audio into
 * PCM16LE frames at a fixed target sample rate, batches them into small
 * (~100-300ms) chunks, and posts each chunk back to the main thread.
 *
 * Two responsibilities beyond a naive passthrough:
 *
 * 1. Resampling: `new AudioContext({ sampleRate })` only *requests* that
 *    rate -- some browsers/OS audio stacks ignore it and keep running at
 *    the hardware's native rate instead (commonly 44100 or 48000 Hz). If
 *    that happens and we don't correct for it, every chunk carries the
 *    wrong sample rate baked into its samples -- audible as pitch-shifted
 *    ("chipmunk" or slow-motion) distortion on the backend. So this
 *    worklet always compares the *actual* native rate (the `sampleRate`
 *    global, set by the browser) against the `targetSampleRate` it's told
 *    to produce, and linearly resamples if they differ. When they match
 *    (the common case), this is a no-op passthrough.
 *
 * 2. Chunk batching: `process()` is called once per render quantum (128
 *    frames -- a few ms), far smaller than the 100-300ms chunk size we
 *    actually want to send over the WebSocket. Sending one WS message per
 *    render quantum would be needlessly chatty. This worklet accumulates
 *    resampled samples internally and only posts a message once it has a
 *    full `chunkMs` worth of audio.
 *
 * Loaded via `audioContext.audioWorklet.addModule("/pcmWorkletProcessor.js")`
 * -- it must be a plain script served as a static file, not bundled, which
 * is why it lives in public/ rather than src/.
 */
class PCMWorkletProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};

    // `sampleRate` is a global provided by AudioWorkletGlobalScope -- the
    // *actual* rate this AudioContext ended up running at.
    this.nativeSampleRate = sampleRate;
    this.targetSampleRate = opts.targetSampleRate || this.nativeSampleRate;
    this.chunkMs = opts.chunkMs || 200;
    this.chunkSizeSamples = Math.round((this.targetSampleRate * this.chunkMs) / 1000);

    this.needsResample = this.nativeSampleRate !== this.targetSampleRate;
    this.resampleRatio = this.nativeSampleRate / this.targetSampleRate;

    // Resampler state, carried across process() calls for continuity.
    this._acc = 0; // fractional position (in native-sample units) of the next output sample

    // Output samples not yet posted (accumulated until chunkSizeSamples reached).
    this._pending = [];
    this._pendingLength = 0;

    if (this.needsResample) {
      this.port.postMessage({
        type: "info",
        message:
          `Resampling ${this.nativeSampleRate}Hz -> ${this.targetSampleRate}Hz in the worklet ` +
          "(the browser did not honor the requested AudioContext sample rate).",
      });
    }
  }

  _resample(nativeBlock) {
    if (!this.needsResample) {
      // Still copy: the array backing `nativeBlock` may be reused by the
      // audio graph on the next render quantum, but we hold onto this data
      // across calls in `_pending` until a full chunk is ready.
      return Float32Array.from(nativeBlock);
    }

    const n = nativeBlock.length;
    const out = [];
    while (this._acc < n) {
      const idx = Math.floor(this._acc);
      const frac = this._acc - idx;
      const s0 = nativeBlock[idx];
      // Linear interpolation would ideally use nativeBlock[idx + 1], but at
      // the very end of a block that sample lives in the *next* block,
      // which we don't have yet -- fall back to repeating the last sample.
      // That's a negligible approximation for speech audio.
      const s1 = idx + 1 < n ? nativeBlock[idx + 1] : nativeBlock[n - 1];
      out.push(s0 + (s1 - s0) * frac);
      this._acc += this.resampleRatio;
    }
    this._acc -= n;
    return out;
  }

  _enqueue(samples) {
    this._pending.push(samples);
    this._pendingLength += samples.length;

    while (this._pendingLength >= this.chunkSizeSamples) {
      const flat = new Float32Array(this._pendingLength);
      let offset = 0;
      for (const piece of this._pending) {
        flat.set(piece, offset);
        offset += piece.length;
      }

      const chunk = flat.subarray(0, this.chunkSizeSamples);
      const remainder = flat.subarray(this.chunkSizeSamples);

      const pcm16 = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const sample = Math.max(-1, Math.min(1, chunk[i]));
        pcm16[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      }
      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);

      this._pending = remainder.length > 0 ? [remainder] : [];
      this._pendingLength = remainder.length;
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      const resampled = this._resample(input[0]);
      this._enqueue(resampled);
    }
    return true;
  }
}

registerProcessor("pcm-worklet-processor", PCMWorkletProcessor);
