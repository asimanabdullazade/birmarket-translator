# Pinned to match bot/requirements.txt's playwright==1.56.0 exactly --
# Playwright's Python package version and the browser binaries baked
# into its Docker image must match; bump both together, never one alone.
# This exact tag was chosen because 1.56.0 is what was actually used to
# verify Chromium launch mechanics during development (see
# bot/_verify_playwright_launch.py) -- not necessarily the latest
# available tag. Check https://playwright.dev/python/docs/docker for
# current tags if this ever needs bumping.
FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# xvfb: virtual display so headed Chromium (not true headless -- see
#   browser_join.py's module docstring on WebRTC/audio reliability) has
#   somewhere to render.
# pulseaudio: virtual audio server; Chromium's meeting audio output is
#   routed into a null sink here, which ffmpeg then reads from (see
#   entrypoint.sh, audio_pipeline.py).
# ffmpeg: captures the null sink's monitor source as raw PCM16LE.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY entrypoint.sh ./
COPY testpage ./testpage
RUN chmod +x entrypoint.sh

# Mount a volume here to inspect join-flow screenshots from outside the
# container -- see config.py's screenshot_dir default and bot/README.md.
RUN mkdir -p /var/log/bot/screenshots

ENTRYPOINT ["./entrypoint.sh"]
