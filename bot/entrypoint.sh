#!/usr/bin/env bash
# Container startup: bring up a virtual display (Xvfb), a PulseAudio
# server with a null-sink Chromium's meeting audio will be routed into,
# then hand off to the actual bot.
#
# Every step here was individually verified in a real (non-Docker) sandbox
# during development -- Xvfb readiness via the X11 unix socket file,
# PulseAudio needing XDG_RUNTIME_DIR set explicitly when running as root
# (this container runs as root by default, matching the base Playwright
# image), and the null-sink module/monitor-source naming
# bot/audio_pipeline.py expects. What was NOT verified here is Chromium's
# *own* audio actually reaching this sink inside a real Docker container
# during a real meeting -- see bot/README.md's troubleshooting section
# and the RMS-logging in audio_pipeline.py, which exists specifically to
# make that failure mode ("connected but silent") diagnosable quickly.
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
export DISPLAY=":${DISPLAY_NUM}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-bot}"
mkdir -p "${XDG_RUNTIME_DIR}"
chmod 700 "${XDG_RUNTIME_DIR}"

echo "[entrypoint] starting Xvfb on display ${DISPLAY}..."
Xvfb "${DISPLAY}" -screen 0 1920x1080x24 &
XVFB_PID=$!

# Wait for Xvfb's X11 unix socket to appear rather than a fixed sleep --
# a fixed sleep is exactly the kind of flakiness this should avoid when a
# cheap, direct check is available.
for _ in $(seq 1 50); do
    if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
        break
    fi
    sleep 0.2
done
if [ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
    echo "[entrypoint] Xvfb did not become ready in time" >&2
    exit 1
fi
echo "[entrypoint] Xvfb ready (pid ${XVFB_PID})"

echo "[entrypoint] starting PulseAudio..."
pulseaudio -D --exit-idle-time=-1 --disallow-exit --log-target=stderr

# Wait for the PulseAudio socket similarly, instead of a fixed sleep.
for _ in $(seq 1 50); do
    if pactl info > /dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
if ! pactl info > /dev/null 2>&1; then
    echo "[entrypoint] PulseAudio did not become ready in time" >&2
    exit 1
fi
echo "[entrypoint] PulseAudio ready"

PULSE_SINK_NAME="${PULSE_SINK_NAME:-meetingsink}"
echo "[entrypoint] loading null sink '${PULSE_SINK_NAME}'..."
pactl load-module module-null-sink \
    "sink_name=${PULSE_SINK_NAME}" \
    "sink_properties=device.description=MeetingSink"
pactl set-default-sink "${PULSE_SINK_NAME}"
echo "[entrypoint] null sink ready and set as default -- Chromium's output should route here"

if [ "$#" -gt 0 ]; then
    # An explicit command was passed to `docker run ... teams-bot <cmd>` --
    # run that instead of the real bot. This is what lets
    # `docker run ... teams-bot python3 _verify_playwright_launch.py`
    # (the "prove Docker works before touching Teams" sanity check in
    # bot/README.md) actually run the verify script rather than silently
    # falling through to bot.py, which was a real bug in an earlier
    # version of this file.
    echo "[entrypoint] running: $*"
    exec "$@"
else
    echo "[entrypoint] starting bot.py"
    exec python3 /app/bot.py
fi
