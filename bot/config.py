"""
Bot settings, loaded from environment variables (see .env.example).

Same pydantic-settings pattern as config/settings.py, deliberately -- env
vars rather than argv, since this bot is launched via `docker run -e ...`
(or `docker compose`/`--env-file`), where env vars are the idiomatic
interface, unlike a locally-invoked script such as
_dev_stream_meeting_audio.py where argv makes more sense.

Nothing here is secret by itself. TEAMS_JOIN_URL is the closest thing to
sensitive (it's a meeting join link) -- keep it in a local, git-ignored
.env file, not committed.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "bot/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Meeting join ---
    # The Teams "join on the web" guest link for the meeting to join. No
    # default -- the bot refuses to start without one (see bot.py).
    teams_join_url: Optional[str] = None

    # Must match whatever meeting_id value listeners use in
    # listener.html?meeting_id=... and whatever value fed
    # _dev_stream_meeting_audio.py/MeetingBroadcast.jsx during testing --
    # this is the join key between the bot and the frontend, not anything
    # Teams-specific.
    meeting_id: str = "dev-meeting"

    # Base URL for the backend's meeting ingest WebSocket -- the bot
    # connects to f"{backend_ws_base}/ws/meeting/{meeting_id}/ingest".
    # IMPORTANT (see bot/README.md): if the backend runs outside Docker on
    # your Mac while this bot runs in a container, "localhost" inside
    # the container is the container itself, not your Mac -- use
    # ws://host.docker.internal:8000 instead. Nothing else in this repo is
    # containerized yet, so this is an easy first-run trap.
    backend_ws_base: str = "ws://localhost:8000"

    # Display name the bot shows in the Teams participant list.
    bot_display_name: str = "Translation Bot"

    # --- Browser / media ---
    # Playwright browser channel. Empty string = Playwright's own bundled
    # browser. Set to "chrome" or "msedge" to use an official build
    # instead, which is what you need if Teams turns out to require H.264:
    # Playwright's bundled browser was the codec-stripped open-source
    # Chromium up to and including v1.56 (this repo's pin), and only
    # switched to Chrome for Testing -- which does ship H.264/AAC -- in
    # v1.57. A named channel must actually be installed in the image; the
    # base Playwright image does not include Google Chrome by default.
    # NOTE on Apple Silicon: Playwright still uses plain Chromium on
    # arm64 Linux even after 1.57, and Google Chrome has no linux/arm64
    # build at all, so on an M-series Mac this setting can only work if
    # the container is run with --platform linux/amd64.
    browser_channel: str = ""

    # Whether to join with the camera on. Default false, and enforced by
    # withholding the browser's camera permission entirely rather than by
    # clicking a toggle -- see launch_browser() in browser_join.py. A bot
    # that only listens has no reason to publish video, and publishing
    # video is what drags the H.264 requirement above into the picture.
    join_with_camera: bool = False

    # --- Timeouts ---
    # Seconds allowed for the interstitial-bypass + pre-join form steps.
    join_timeout_s: float = 60.0
    # Seconds allowed waiting in the lobby to be admitted by an organizer.
    # Teams commonly holds guests here for a while -- keep this generous.
    lobby_timeout_s: float = 240.0
    # How often (seconds) to poll for meeting-end while in the meeting.
    meeting_end_poll_interval_s: float = 5.0
    # How often (seconds) to take a screenshot while waiting for
    # admission. The lobby wait used to be a blind window: one screenshot
    # at the very end, minutes after whatever actually went wrong.
    admission_screenshot_interval_s: float = 20.0

    # --- Debugging ---
    # Every join-flow step writes a screenshot here (see logging_utils.py)
    # -- mount this as a volume so screenshots survive/are inspectable
    # outside the container. This is the primary debugging aid for the
    # Teams-DOM automation, which is expected to need real-world
    # iteration -- see bot/browser_join.py and bot/README.md.
    screenshot_dir: str = "/var/log/bot/screenshots"
    log_level: str = "INFO"

    # --- Audio ---
    # Name of the PulseAudio null-sink module loaded by entrypoint.sh --
    # must match what's passed to `pactl load-module module-null-sink
    # sink_name=...` there. audio_pipeline.py reads from
    # f"{pulse_sink_name}.monitor".
    pulse_sink_name: str = "meetingsink"
    # Must match config/settings.py's audio_sample_rate on the backend.
    audio_sample_rate: int = 16000
    # Matches _dev_stream_meeting_audio.py's FRAME_MS -- real-time-paced
    # frame size sent to the ingest WebSocket.
    frame_ms: int = 200


@lru_cache
def get_bot_settings() -> BotSettings:
    return BotSettings()
