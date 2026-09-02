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
        this.onPCMChunk(event.data);
      } else if (event.data && event.data.type === "info") {
        console.warn("[MicCapture]", event.data.message);
      }
    };

    this.sourceNode.connect(this.workletNode);
    // Not connected to `audioContext.destination` -- we don't want to play
    // the mic back out of the speakers, just process it.
  }

  stop() {
    this.workletNode?.port?.close?.();
    this.sourceNode?.disconnect();
    this.workletNode?.disconnect();
    this.mediaStream?.getTracks().forEach((track) => track.stop());
    this.audioContext?.close();

    this.audioContext = null;
    this.mediaStream = null;
    this.sourceNode = null;
    this.workletNode = null;
  }
}
