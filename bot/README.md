# Teams meeting bot (Phase 12)

Joins a Microsoft Teams meeting as an anonymous/guest participant via a
headed, browser-automated Chromium session, captures the meeting's audio,
and streams it into the existing `/ws/meeting/{meeting_id}/ingest`
endpoint (see `../backend/websocket/meeting_handlers.py`) -- the exact
same wire contract `../_dev_stream_meeting_audio.py` (a WAV file) and
`../frontend/src/MeetingBroadcast.jsx` (your own mic) already use. No
backend or frontend changes were needed for this bot to work -- that's
the payoff of how Phase 11's ingest contract was designed.

The bot is **one-way only**: it listens to the meeting and never injects
or plays audio back into it. Translated audio stays listener-only (via
`listener.html?meeting_id=...`), same as every other mode in this app.

## What's verified vs. what needs your machine

This was built in a sandboxed environment with no Docker daemon and no
route to `teams.microsoft.com` (a plain request to `microsoft.com`
returned `403 Forbidden` there). So:

**Already verified, for real, before this was ever handed to you:**
- The entire audio pipeline -- PulseAudio null sink -> ffmpeg capture ->
  the real ingest WebSocket -> the real backend's VAD segmenter -> the
  mock provider -> broadcast -> a real listener socket actually
  receiving a transcript. See `_verify_audio_pipeline.py`.
- Headed Chromium launching under Xvfb with the exact fake-media launch
  flags this bot uses, navigating, and the screenshot debugging helper
  working. See `_verify_playwright_launch.py`.
- `entrypoint.sh` end-to-end (Xvfb up, PulseAudio up with the null sink,
  handoff to `bot.py`), run directly as a shell script outside Docker.

**NOT verified -- expect to iterate here, on your machine, against a
real meeting:**
- `docker build` / `docker run` themselves (no Docker daemon in the build
  sandbox).
- Everything in `browser_join.py`'s Teams-specific selectors: the
  "Continue on this browser" click, the pre-join form, lobby/admission
  detection, meeting-end detection. These were written from public
  documentation about Teams' web-join flow, never against a live page.
  **Expect the first real run to fail partway through and need
  adjustment -- that's normal for this kind of browser automation, not a
  defect.** Every step writes a screenshot to `SCREENSHOT_DIR` (see
  `logging_utils.py`) specifically so you can see exactly where it broke
  and fix the selector, rather than debugging blind.
- Whether Chromium's actual meeting audio output lands in the PulseAudio
  null sink inside a real container -- see "Troubleshooting" below, this
  is flagged as the single biggest open technical risk.

## Setup

1. **Docker Desktop** must be installed and running on your machine (this
   assumes you have it -- if not, install it first).
2. Copy the env file and fill in your real meeting link:
```bash
   cp .env.example .env
   # edit .env -- at minimum set TEAMS_JOIN_URL
```
3. **If your backend runs outside Docker** (the normal case today --
   nothing else in this repo is containerized) **on the same machine**,
   set in `.env`:
BACKEND_WS_BASE=ws://host.docker.internal:8000
   `localhost` inside the container means the container itself, not your
   Mac -- this is the single most common first-run trap.
4. Set `MEETING_ID` in `.env` to whatever value your `listener.html`
   tabs use, e.g. `listener.html?meeting_id=my-meeting`.
5. Build the image:
```bash
   docker build -t teams-bot .
```

## First run: prove Docker works before touching Teams

Before pointing this at a real meeting, isolate "did Docker/Xvfb/
PulseaAudio/Chromium set up correctly" from "did the Teams automation
work" by confirming the container can launch and screenshot the local
test page:

```bash
docker run --rm -v "$(pwd)/screenshots:/var/log/bot/screenshots" \
    --env-file .env -e TEAMS_JOIN_URL="" teams-bot \
    python3 _verify_playwright_launch.py
```

If this fails, the problem is in the container setup (Xvfb, PulseAudio,
Chromium launch flags), not Teams' DOM -- fix that first.

## Running against a real meeting

```bash
docker run --rm -v "$(pwd)/screenshots:/var/log/bot/screenshots" \
    --env-file .env teams-bot
```

Watch the logs. On success, open `listener.html?meeting_id=<your
meeting_id>` (in en/az/ru tabs) and talk in the meeting -- you should see
transcripts and translations appear. On failure, check
`./screenshots/` for the last step that ran and adjust the matching
selector/text pattern in `browser_join.py` (see the named constants near
the top of that file).

Use `TRANSLATION_PROVIDER=mock` on the backend first to prove plumbing
without spending real API calls; switch to `gemini` for real translation
quality.

## Known v1 limitations

- **No automatic reconnect** if the ingest WebSocket drops mid-meeting --
  fatal for that run, restart the container to resume.
- **No auto-scheduling/calendar integration** -- you start the bot
  manually for a specific meeting, same as every other dev entrypoint in
  this project.
- **One bot per meeting, no multi-meeting orchestration.**
- **Only anonymous/guest-joinable meetings** are supported -- meetings
  that require signing into your org's Teams account aren't handled.
- **No multi-hour stability hardening** -- ffmpeg/Chromium memory growth
  and audio clock drift over a very long meeting are unaddressed.
- **The bot never speaks into the meeting** -- by design, not a
  limitation to fix later. See the module docstring in `browser_join.py`.

## Troubleshooting

**Container fails to build:** check that the pinned Docker tag in
`Dockerfile` (`mcr.microsoft.com/playwright/python:v1.56.0-jammy`) still
resolves -- Playwright's available tags change over time. If bumping it,
bump `requirements.txt`'s `playwright==` pin to the exact same version at
the same time; a mismatch between the Python package and the image's
bundled Chromium is a common source of confusing failures.

**Joins the meeting but the transcript never appears / audio looks
silent (RMS logged near 0 in `audio_pipeline.py`'s early-frame logging):**
this is the single biggest open risk in this build -- Chromium's output
audio may not be reaching the PulseAudio null sink. Things to check, in
order:
1. Confirm the null sink is actually the default sink inside the running
   container: `docker exec -it <container> pactl info` should show
   `Default Sink: meetingsink` (or whatever `PULSE_SINK_NAME` you set).
2. Confirm Chromium itself is using PulseAudio for audio output, not
   silently falling back to a null/dummy backend of its own. If needed,
   try explicitly setting `PULSE_SERVER=unix:${XDG_RUNTIME_DIR}/pulse/native`
   in the container environment before Chromium launches.
3. As a sanity check, `docker exec -it <container> parec -d
   meetingsink.monitor | pv > /dev/null` while the bot is in a meeting --
   you should see nonzero throughput if the meeting audio is really
   reaching the sink.

**Never gets admitted from the lobby:** check `screenshots/*-in-lobby-*`
and `*-admission-timeout-*` -- either nobody let the bot in, or
`wait_for_admission`'s "leave call" detection didn't recognize the
in-meeting UI (Teams' UI may have changed since this was written). Adjust
`_LEAVE_CALL_TEXTS` in `browser_join.py`.

**Consent/policy:** this bot captures and transcribes meeting audio.
Whether that requires participant notice depends on your organization's
policy and your jurisdiction -- confirm that independently before using
this in a real meeting; it's outside this bot's technical scope.
