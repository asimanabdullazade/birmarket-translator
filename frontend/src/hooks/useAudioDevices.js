import { useCallback, useEffect, useState } from "react";

/**
 * Enumerates microphone (audioinput) and output (audiooutput) devices.
 *
 * Device labels are only populated by the browser once microphone
 * permission has been granted, so this requests a throwaway getUserMedia
 * stream first (and immediately stops it) purely to unlock labels.
 */
export function useAudioDevices() {
  const [microphones, setMicrophones] = useState([]);
  const [outputs, setOutputs] = useState([]);
  const [permissionError, setPermissionError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      tempStream.getTracks().forEach((track) => track.stop());

      const devices = await navigator.mediaDevices.enumerateDevices();
      setMicrophones(devices.filter((d) => d.kind === "audioinput"));
      setOutputs(devices.filter((d) => d.kind === "audiooutput"));
      setPermissionError(null);
    } catch (err) {
      setPermissionError(err.message || "Microphone permission denied");
    }
  }, []);

  useEffect(() => {
    refresh();
    navigator.mediaDevices?.addEventListener?.("devicechange", refresh);
    return () => navigator.mediaDevices?.removeEventListener?.("devicechange", refresh);
  }, [refresh]);

  return { microphones, outputs, permissionError, refresh };
}
