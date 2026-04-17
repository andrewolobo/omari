"""
queue_processor.py — Async download and upload pipeline.

Public API:
    run_queue_processor(tm, bot)  — long-running coroutine; launch with
                                    asyncio.create_task() from main.py.

Pipeline per item:
    queued → downloading → [asyncio.to_thread(download_magnet)] →
    uploading → [sync_to_dropbox] → completed → notify_user

Retry schedule (reset to 'queued' after delay):
    Attempt 1 → wait  30 s
    Attempt 2 → wait 120 s
    Attempt 3 → wait 300 s
    Attempt 4+→ mark 'failed', notify user

InvalidMagnetError is never retried — a bad magnet is permanently bad.
CancelledError resets the item to 'queued' so it restarts on next run.

Concurrency:
    asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS) caps parallel downloads.
    An in-memory set tracks identifiers that already have an active task so
    poll cycles cannot spawn duplicate tasks for the same item.

Progress notifications:
    The download callback runs inside a thread-pool worker (asyncio.to_thread).
    asyncio.run_coroutine_threadsafe() bridges it back to the event loop to
    send Telegram messages every 10 percentage-point milestone.
"""

import asyncio
from typing import Callable

from loguru import logger
from telegram import Bot

import auth_store
import config
from bot import notify_user
from database import get_download, get_downloads_by_status, increment_retry, update_status
from dropbox_sync import sync_to_dropbox
from torrent import DownloadTimeoutError, InvalidMagnetError, TorrentManager, detect_media_type

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Seconds to wait before re-queuing on each retry attempt.
_RETRY_DELAYS: dict[int, int] = {1: 30, 2: 120, 3: 300}

# Seconds between queue-poll cycles.
_POLL_INTERVAL = 10

# Maximum retries before permanent failure.
_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_task_exception(t: asyncio.Task) -> None:
    """Done-callback that logs any unhandled exception from a task."""
    if not t.cancelled() and (exc := t.exception()):
        logger.error(f"Unhandled task exception in {t.get_name()}: {exc}")


# ---------------------------------------------------------------------------
# Main queue loop
# ---------------------------------------------------------------------------

async def run_queue_processor(tm: TorrentManager, bot: Bot) -> None:
    """
    Long-running coroutine — poll for queued items and dispatch each as
    an asyncio task, capped by MAX_CONCURRENT_DOWNLOADS.

    Args:
        tm:  Shared TorrentManager (one libtorrent session per process).
        bot: Telegram Bot instance for sending notifications.
    """
    sem = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)

    # Identifiers that already have an active task. Prevents duplicate tasks
    # being spawned in the window between create_task() and the task updating
    # the DB status to 'downloading'.
    active_ids: set[str] = set()

    logger.info(
        f"Queue processor started — "
        f"max concurrent downloads: {config.MAX_CONCURRENT_DOWNLOADS}"
    )

    _warned_unlinked = False

    while True:
        if not auth_store.is_linked():
            if not _warned_unlinked:
                logger.warning(
                    "Queue processor: Dropbox is not linked — "
                    "downloads are paused until authorisation is complete."
                )
                _warned_unlinked = True
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        _warned_unlinked = False  # Reset so we log again if token is ever revoked.
        queued = get_downloads_by_status("queued")

        for item in queued:
            identifier = item["identifier"]
            if identifier in active_ids:
                continue  # Task already running for this item.

            active_ids.add(identifier)

            async def _run(item: dict = item) -> None:
                try:
                    await _process_item(sem, tm, bot, item)
                finally:
                    active_ids.discard(item["identifier"])

            task = asyncio.create_task(
                _run(),
                name=f"dl:{identifier[:16]}",
            )
            task.add_done_callback(_log_task_exception)

        await asyncio.sleep(_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Per-item pipeline
# ---------------------------------------------------------------------------

async def _process_item(
    sem: asyncio.Semaphore,
    tm: TorrentManager,
    bot: Bot,
    item: dict,
) -> None:
    """
    Execute the full download → upload pipeline for one queued item,
    holding the semaphore for the duration.
    """
    identifier = item["identifier"]

    # Re-fetch from DB to guard against stale snapshot data (e.g. a cancel
    # that arrived between the poll and this task executing).
    fresh = get_download(identifier)
    if fresh is None or fresh.get("status") != "queued":
        logger.info(
            f"Skipping {identifier[:16]!r}: status is "
            f"{fresh.get('status') if fresh else 'missing'!r} — expected 'queued'."
        )
        return

    title   = fresh.get("title", "Unknown")
    chat_id = fresh.get("chat_id", 0)
    item    = fresh  # use the authoritative record throughout the pipeline

    async with sem:
        # Mark downloading immediately — prevents the next poll cycle from
        # picking this item up again via get_downloads_by_status('queued').
        update_status(identifier, "downloading")
        logger.info(f"Started: {title!r}")

        try:
            # ----------------------------------------------------------
            # Stage 1: Download (blocking — runs in thread-pool worker)
            # ----------------------------------------------------------
            loop        = asyncio.get_running_loop()
            progress_cb = _make_progress_callback(bot, chat_id, title, loop)

            target_name: str = await asyncio.to_thread(
                tm.download_magnet, identifier, progress_cb
            )

            # Update the record with the resolved torrent name now that we
            # have it (replaces the "Manual magnet" placeholder). Auto-detect
            # the media type here if the user did not provide a hint.
            media_type = item.get("media_type", "unknown")
            if media_type == "unknown":
                media_type = detect_media_type(target_name)
                update_status(
                    identifier,
                    "uploading",
                    target_name=target_name,
                    title=target_name,
                    media_type=media_type,
                )
            else:
                update_status(
                    identifier,
                    "uploading",
                    target_name=target_name,
                    title=target_name,
                )
            logger.info(f"Uploading to Dropbox: {target_name!r}")

            # ----------------------------------------------------------
            # Stage 2: Upload to Dropbox (async, non-blocking)
            # ----------------------------------------------------------
            await sync_to_dropbox(target_name, media_type)

            # ----------------------------------------------------------
            # Stage 3: Complete
            # ----------------------------------------------------------
            update_status(identifier, "completed")
            await notify_user(bot, chat_id, f"✅ Done: {target_name}")
            logger.info(f"Pipeline complete: {title!r}")

        except InvalidMagnetError as exc:
            # A bad magnet will never succeed — skip retries entirely.
            logger.error(f"Invalid magnet for {title!r}: {exc}")
            update_status(identifier, "failed")
            await notify_user(bot, chat_id, f"❌ Failed (bad magnet): {title}")

        except asyncio.CancelledError:
            # Graceful shutdown — reset so the item retries on next startup.
            logger.warning(f"Task cancelled: {title!r} — resetting to queued.")
            update_status(identifier, "queued")
            raise  # Must re-raise so asyncio marks the task cancelled.

        except Exception as exc:
            await _handle_retry(bot, identifier, title, chat_id, exc)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

async def _handle_retry(
    bot: Bot,
    identifier: str,
    title: str,
    chat_id: int,
    exc: Exception,
) -> None:
    """
    Increment the retry counter and either re-queue with backoff or
    permanently mark the item as failed.
    """
    retry_count = increment_retry(identifier)
    exc_type    = type(exc).__name__

    logger.warning(
        f"Pipeline error (attempt {retry_count}/{_MAX_RETRIES}) "
        f"for {title!r}: {exc_type}: {exc}"
    )

    if retry_count <= _MAX_RETRIES:
        delay = _RETRY_DELAYS.get(retry_count, 300)
        logger.info(f"Retrying {title!r} in {delay}s…")
        await asyncio.sleep(delay)
        update_status(identifier, "queued")
    else:
        logger.error(f"Permanently failed after {_MAX_RETRIES} retries: {title!r}")
        update_status(identifier, "failed")
        await notify_user(bot, chat_id, f"❌ Failed after {_MAX_RETRIES} retries: {title}")


# ---------------------------------------------------------------------------
# Progress callback factory
# ---------------------------------------------------------------------------

def _make_progress_callback(
    bot: Bot,
    chat_id: int,
    title: str,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[float], None]:
    """
    Return a thread-safe progress callback suitable for TorrentManager.

    The callback is invoked from a thread-pool worker (asyncio.to_thread),
    so it cannot directly await coroutines. It uses
    asyncio.run_coroutine_threadsafe() to schedule Telegram messages on the
    event loop without blocking the download thread.

    Messages are sent once per 10-percentage-point milestone (0%, 10%, …, 100%)
    to avoid flooding the Telegram API.

    Args:
        bot:     Telegram Bot used to send messages.
        chat_id: Destination chat. If 0 (RSS-sourced), the callback no-ops.
        title:   Display name shown in the progress message.
        loop:    The running event loop (captured before entering the thread).

    Returns:
        A callable(pct: float) → None suitable for passing to download_magnet().
    """
    last_milestone = [-1]  # Mutable list so the closure can mutate it.

    def callback(pct: float) -> None:
        if not chat_id:
            return

        milestone = int(pct // 10) * 10
        if milestone <= last_milestone[0]:
            return

        last_milestone[0] = milestone
        message = f"⬇️ {title}: {milestone}%"

        future = asyncio.run_coroutine_threadsafe(
            notify_user(bot, chat_id, message),
            loop,
        )

        def _log_error(f: asyncio.Future) -> None:
            exc = f.exception()
            if exc:
                logger.warning(f"Progress notification error: {exc}")

        future.add_done_callback(_log_error)

    return callback
