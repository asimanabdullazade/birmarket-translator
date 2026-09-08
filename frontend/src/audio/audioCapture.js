/**
 * Microphone capture using the Web Audio API.
 *
 * Opens the given input device with getUserMedia, runs it through the
 * AudioWorklet defined in public/pcmWorkletProcessor.js to convert it to
 * PCM16LE at a fixed target sample rate, batched into ~AUDIO_SEND_CHUNK_MS
 * chunks, and invokes `onPCMChunk(arrayBuffer)` for each chunk -- the
 * caller (useWebSocket.js) is responsible for sending those bytes over the
 * WebSocket.
 *
 * Note on sample rate: `new AudioContext({ sampleRate })` only *requests*
 * that rate -- some browsers/OS audio stacks ignore it and keep running at
 * the hardware's native rate instead. The worklet checks the actual native
 * rate against AUDIO_SAMPLE_RATE and resamples on the fly if they differ
 * (logging a console warning when it does), so the bytes we send are
 * always genuinely at AUDIO_SAMPLE_RATE regardless of what the browser did
 * with the request.
 *
 * Pause/resume (Phase 9)
 * -----------------------
 * `pause()`/`resume()` suspend/resume the AudioContext *and* gate the
 * worklet callback client-side (belt-and-suspenders -- a suspended
 * AudioContext should already stop delivering audio process callbacks, but
 * gating the callback too means a frame can never reach `onPCMChunk` while
 * paused even if that assumption is wrong on some browser). Deliberately
 * NOT a stop()+start() cycle -- that would re-request getUserMedia (a
 * fresh permission-adjacent prompt/flicker on some browsers) and
 * re-register the audio worklet module for no reason.
 *
 * Original-audio monitor ("sidetone")
 * -------------------------------------
 * `setMonitorVolume(volume)` fans the same `sourceNode` that already feeds
 * the worklet out to a second destination, `monitorGainNode ->
 * audioContext.destination` (Web Audio natively supports one output
 * feeding multiple inputs -- no splitter node needed), so the user can
 * optionally hear their own mic locally. Defaults to 0 (silent/off): with
 * volume above 0 and no headphones, this mic input feeding back out the
 * speakers can cause audible feedback -- `echoCancellation: true` on the
 * getUserMedia constraints above doesn't reliably cover a second, unrelated
 * AudioContext's output (TranslationAudioPlayer in audioPlayback.js runs
 * its own separate context). See the "Original audio" slider's caption in
 * AudioControls.jsx for the user-facing version of this caveat.
 */

import { AUDIO_SAMPLE_RATE, AUDIO_SEND_CHUNK_MS } from "../config.js";

export class MicCapture {
  constructor({ deviceId, onPCMChunk }) {
    this.deviceId = deviceId;
    this.onPCMChunk = onPCMChunk;
    this.audioContext = null;
    this.mediaStream = null;
    this.sourceNode = null;
    this.workletNode = null;
    this.monitorGainNode = null;
    this._paused = false;
  }

  async start() {
    this.audioContext = new AudioContext({ sampleRate: AUDIO_SAMPLE_RATE });

    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: this.deviceId ? { exact: this.deviceId } : undefined,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });

    await this.audioContext.audioWorklet.addModule("/pcmWorkletProcessor.js");

    this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.workletNode = new AudioWorkletNode(this.audioContext, "pcm-worklet-processor", {
      processorOptions: {
        targetSampleRate: AUDIO_SAMPLE_RATE,
        chunkMs: AUDIO_SEND_CHUNK_MS,
      },
    });

    this.workletNode.port.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        if (!this._paused) {
          this.onPCMChunk(event.data);
        }
      } else if (event.data && event.data.type === "info") {
        console.warn("[MicCapture]", event.data.message);
      }
    };

    this.sourceNode.connect(this.workletNode);
    // Not connected to `audioContext.destination` directly -- we don't
    // want the raw mic feeding straight into the speakers. The optional
    // local monitor ("sidetone") path below is a separate, explicit,
    // volume-controlled fan-out for that -- see the module docstring.
    this.monitorGainNode = this.audioContext.createGain();
    this.monitorGainNode.gain.value = 0; // off by default -- see the module docstring's feedback-risk note
    this.sourceNode.connect(this.monitorGainNode);
    this.monitorGainNode.connect(this.audioContext.destination);
  }

  /** See "Pause/resume" in the module docstring. */
  async pause() {
    this._paused = true;
    try {
      await this.audioContext?.suspend();
    } catch {
      // Some browsers can reject suspend() in edge-case states; the
      // callback-level `_paused` gate above still keeps frames from being
      // sent either way, so this is not fatal.
    }
  }

  /** See "Pause/resume" in the module docstring. */
  async resume() {
    this._paused = false;
    try {
      await this.audioContext?.resume();
    } catch {
      // See pause() -- the callback gate is the load-bearing guarantee;
      // a resume() rejection just means we rely on that alone.
    }
  }

  /** See "Original-audio monitor" in the module docstring. `volume` is
   * clamped to [0, 1], same convention as TranslationAudioPlayer.setVolume
   * in audioPlayback.js. */
  setMonitorVolume(volume) {
    if (this.monitorGainNode) {
      this.monitorGainNode.gain.value = Math.max(0, Math.min(1, volume));
    }
  }

  stop() {
    this.workletNode?.port?.close?.();
    this.sourceNode?.disconnect();
    this.workletNode?.disconnect();
    this.monitorGainNode?.disconnect();
    this.mediaStream?.getTracks().forEach((track) => track.stop());
    this.audioContext?.close();

    this.audioContext = null;
    this.mediaStream = null;
    this.sourceNode = null;
    this.workletNode = null;
    this.monitorGainNode = null;
  }
}
