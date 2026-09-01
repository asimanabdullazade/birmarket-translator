/**
 * AudioWorkletProcessor that converts the browser's native float32 audio
 * into PCM16LE frames and posts them back to the main thread, where
 * audioCapture.js forwards them to the backend over the WebSocket.
 *
 * Loaded via `audioContext.audioWorklet.addModule("/pcmWorkletProcessor.js")`
 * -- it must be a plain script served as a static file, not bundled, which
 * is why it lives in public/ rather than src/.
 */
class PCMWorkletProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0] && input[0].length > 0) {
      const channelData = input[0];
      const pcm16 = new Int16Array(channelData.length);
      for (let i = 0; i < channelData.length; i++) {
        const sample = Math.max(-1, Math.min(1, channelData[i]));
        pcm16[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
      }
      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    }
    return true;
  }
}

registerProcessor("pcm-worklet-processor", PCMWorkletProcessor);
