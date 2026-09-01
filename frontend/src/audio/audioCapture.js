/**
 * Microphone capture using the Web Audio API.
 *
 * Opens the given input device with getUserMedia, runs it through the
 * AudioWorklet defined in public/pcmWorkletProcessor.js to convert it to
 * PCM16LE, and invokes `onPCMChunk(arrayBuffer)` for every frame -- the
 * caller (useWebSocket.js) is responsible for sending those bytes over
 * the WebSocket.
 */

import { AUDIO_SAMPLE_RATE } from "../config.js";

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
    this.workletNode = new AudioWorkletNode(this.audioContext, "pcm-worklet-processor");

    this.workletNode.port.onmessage = (event) => {
      this.onPCMChunk(event.data);
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
