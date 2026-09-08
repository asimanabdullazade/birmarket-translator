/**
 * Gapless, non-overlapping playback queue for the server's synthesized
 * translation audio (Step 6).
 *
 * Each incoming segment is a small, complete WAV file, base64-encoded (see
 * backend/websocket/handlers.py -- synthesized speech is wrapped in a WAV
 * header before being sent, specifically so the browser's own
 * decodeAudioData can handle it without us hand-rolling PCM decoding).
 * This class:
 *   - decodes each segment,
 *   - schedules it to start exactly when the previously-queued segment
 *     ends -- never overlapping, never leaving an audible gap if segments
 *     arrive in time (see "Prevent segments overlapping" in Step 6),
 *   - routes everything through one GainNode so volume/mute affects every
 *     segment uniformly, including ones already scheduled to play.
 *
 * Segments are enqueued strictly in arrival order via an internal promise
 * chain (`enqueue` awaits the previous call before decoding/scheduling the
 * next) -- decodeAudioData is async, so without this, two segments
 * arriving close together could finish decoding out of order and get
 * scheduled in the wrong sequence.
 *
 * Intentionally independent of MicCapture's AudioContext (audio/
 * audioCapture.js) -- one captures mic input, this one plays back
 * synthesized speech, and there's no reason to couple their lifecycles.
 * (Phase 10 follow-up: hooks/useWebSocket.js reads `getPlaybackEndsAt()`
 * to gate mic forwarding while this is playing -- see "Mic gating during
 * playback" there -- but that's a one-way read, not a real coupling: this
 * class has no idea the mic exists.)
 */
export class TranslationAudioPlayer {
  constructor() {
    this.audioContext = null;
    this.gainNode = null;
    this.nextStartTime = 0;
    this.volume = 1;
    this.muted = false;
    this._chain = Promise.resolve();
    // Phase 10 follow-up: wall-clock (Date.now()-based) estimate of when
    // everything currently enqueued will finish playing -- see
    // getPlaybackEndsAt(). Same wall-clock-via-AudioContext-delay trick
    // enqueue()'s own return value already uses for Step 7.
    this._playbackEndsAt = 0;
  }

  _ensureContext() {
    if (!this.audioContext) {
      this.audioContext = new AudioContext();
      this.gainNode = this.audioContext.createGain();
      this.gainNode.gain.value = this.muted ? 0 : this.volume;
      this.gainNode.connect(this.audioContext.destination);
      this.nextStartTime = 0;
    }
    return this.audioContext;
  }

  setVolume(volume) {
    this.volume = volume;
    if (this.gainNode && !this.muted) {
      this.gainNode.gain.value = volume;
    }
  }

  setMuted(muted) {
    this.muted = muted;
    if (this.gainNode) {
      this.gainNode.gain.value = muted ? 0 : this.volume;
    }
  }

  /**
   * Decode and schedule one base64-encoded WAV segment for gapless
   * playback. Resolves with a wall-clock (`Date.now()`-based) estimate of
   * the moment this segment actually becomes audible -- used for Step 7's
   * latency breakdown (see the caller in hooks/useWebSocket.js, which
   * reports the *first* chunk's value back to the server as an
   * `audio_played` message). Resolves `undefined` instead if this segment
   * failed to decode/play, so callers know not to report a bogus number.
   */
  enqueue(base64Wav) {
    this._chain = this._chain
      .then(() => this._enqueueOne(base64Wav))
      .catch((err) => {
        console.warn("[TranslationAudioPlayer] Failed to play a synthesized audio segment", err);
        return undefined;
      });
    return this._chain;
  }

  async _enqueueOne(base64Wav) {
    const audioContext = this._ensureContext();
    if (audioContext.state === "suspended") {
      // Browsers suspend a freshly-created AudioContext until a user
      // gesture -- clicking "Start Translation" is that gesture, so this
      // normally resolves immediately.
      await audioContext.resume();
    }

    const binary = atob(base64Wav);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }

    const audioBuffer = await audioContext.decodeAudioData(bytes.buffer);

    const source = audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(this.gainNode);

    // Start right after whatever's already queued ends -- never before
    // "now" (e.g. after an idle gap since the last segment) and never
    // overlapping the previous one.
    const startAt = Math.max(this.nextStartTime, audioContext.currentTime);
    source.start(startAt);
    const endAt = startAt + audioBuffer.duration;
    this.nextStartTime = endAt;

    // Step 7: AudioContext's clock has no fixed relationship to
    // Date.now() by itself, but the *delay* between "now" and `startAt`
    // is in the same units (seconds) regardless of anchor -- so
    // Date.now() plus that delay is a fair wall-clock estimate of when
    // this segment actually starts being audible, including any queueing
    // wait behind a still-playing earlier segment.
    const playedAtMs = Date.now() + Math.max(0, (startAt - audioContext.currentTime) * 1000);

    // Phase 10 follow-up: same trick, but for when this segment (and thus
    // everything queued so far) finishes -- see getPlaybackEndsAt(). Uses
    // Math.max, not a plain assignment, purely as defense-in-depth: startAt
    // is already derived from nextStartTime so segments should never
    // resolve out of order here, but a resolve-order surprise should never
    // be able to *shrink* the tracked end time.
    const endsAtMs = Date.now() + Math.max(0, (endAt - audioContext.currentTime) * 1000);
    this._playbackEndsAt = Math.max(this._playbackEndsAt, endsAtMs);

    return playedAtMs;
  }

  /**
   * Phase 10 follow-up: wall-clock ms estimate of when all currently
   * enqueued/playing audio will finish -- 0 (already "in the past") if
   * nothing has ever been enqueued or everything queued has finished.
   * Callers should re-check this shortly after `enqueue()`'s returned
   * promise resolves, not at call time -- see the caller in
   * hooks/useWebSocket.js for why.
   */
  getPlaybackEndsAt() {
    return this._playbackEndsAt;
  }

  /** Stop everything scheduled/playing and reset the queue (e.g. on Stop). */
  stop() {
    this.audioContext?.close();
    this.audioContext = null;
    this.gainNode = null;
    this.nextStartTime = 0;
    this._playbackEndsAt = 0;
    this._chain = Promise.resolve();
    // volume/muted are deliberately NOT reset here -- they're a user
    // preference that should carry over into the next session.
  }
}
