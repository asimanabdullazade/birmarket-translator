"""
Orchestrator / main entrypoint: launch -> join -> wait for admission ->
run audio capture + ingest concurrently with meeting-end watching ->
teardown.

Talks to backend/websocket/meeting_handlers.py's existing
/ws/meeting/{meeting_id}/ingest endpoint completely unmodified -- see
ingest_client.py. Everything Teams-specific lives in browser_join.py;
everything audio-specific lives in audio_pipeline.py. This file just
wires them together and owns exit codes / top-level error handling.
"""

from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

import audio_pipeline
import browser_join
import ingest_client
import speaker_tracker as speaker_tracker_mod
from config import BotSettings, get_bot_settings
from logging_utils import configure_logging, save_screenshot


def _task_exception(task: "asyncio.Task") -> "BaseException | None":
    """task.exception() but safe to call on a not-yet-done or cancelled task."""
    if not task.done() or task.cancelled():
        return None
    return task.exception()


async def run(settings: BotSettings) -> int:
    logger = configure_logging(settings.log_level)

    if not settings.teams_join_url:
        logger.error("TEAMS_JOIN_URL is not set -- refusing to start. See bot/.env.example.")
        return 1

    async with async_playwright() as pw:
        browser, context, page = await browser_join.launch_browser(pw, settings)

        try:
            try:
                await browser_join.bypass_interstitial(page, settings.teams_join_url, settings, logger)
                await browser_join.complete_prejoin(page, settings, logger)
                await browser_join.wait_for_admission(page, settings, logger)
            except browser_join.JoinFailure as exc:
                logger.error("failed to join the meeting: %s", exc)
                await save_screenshot(page, "join-failed-final", settings.screenshot_dir, logger)
                return 1

            logger.info("in the meeting -- starting audio capture and ingest")
            stop_event = asyncio.Event()

            ffmpeg_proc = await audio_pipeline.start_ffmpeg_capture(settings, logger)
            frame_source = audio_pipeline.capture_frames(ffmpeg_proc, settings, logger)

            # Phase 14: best-effort speaker attribution. Optional by
            # design -- if this task dies or matches nothing, captions
            # simply arrive unattributed and everything else is unaffected.
            tracker = speaker_tracker_mod.SpeakerTracker(page, settings, logger)
            speaker_task = asyncio.create_task(
                tracker.run(stop_event),
                name="speaker_tracker",
            )

            ingest_task = asyncio.create_task(
                ingest_client.stream_to_ingest(settings, frame_source, stop_event, logger, tracker),
                name="ingest",
            )
            end_watch_task = asyncio.create_task(
                browser_join.watch_for_meeting_end(page, settings, logger, stop_event),
                name="meeting-end-watch",
            )

            exit_code = 0
            try:
                done, pending = await asyncio.wait(
                    {ingest_task, end_watch_task}, return_when=asyncio.FIRST_COMPLETED
                )

                first_exc = _task_exception(ingest_task)
                if first_exc is not None:
                    logger.error("ingest task failed: %s", first_exc)
                    exit_code = 1

                stop_event.set()

                # speaker_task is deliberately NOT in the FIRST_COMPLETED
                # set above -- attribution ending must never bring the
                # session down -- but it still has to be drained here, or
                # it is left pending when the loop closes.
                for task in list(pending) + [speaker_task]:
                    try:
                        await asyncio.wait_for(task, timeout=10.0)
                    except asyncio.TimeoutError:
                        logger.warning("task %s did not finish within 10s of stop_event, cancelling", task.get_name())
                        task.cancel()
                    except Exception:
                        pass  # surfaced below via _task_exception, if it's the ingest task

                # Surface any exception from the ingest task even if it
                # was the one still pending above (e.g. meeting-end fired
                # first and ingest_task is only now finishing up).
                later_exc = _task_exception(ingest_task)
                if later_exc is not None:
                    logger.error("ingest task failed: %s", later_exc)
                    exit_code = 1

            finally:
                await audio_pipeline.stop_capture(ffmpeg_proc, logger)

            logger.info("session complete, exit code %d", exit_code)
            return exit_code

        finally:
            await browser.close()


def main() -> None:
    settings = get_bot_settings()
    exit_code = asyncio.run(run(settings))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
