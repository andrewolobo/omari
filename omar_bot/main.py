"""
main.py — Entrypoint and graceful shutdown coordinator.

Starts four concurrent tasks:
    1. Telegram bot    — handles user commands via long-polling
    2. RSS worker      — polls configured feeds every RSS_POLL_INTERVAL seconds
    3. Queue processor — downloads torrents and uploads to Dropbox
    4. API server      — FastAPI/uvicorn serving the Dropbox OAuth2 callback endpoint

Shutdown (SIGINT / SIGTERM / Ctrl-C):
    • All background tasks are cancelled.
    • In-progress downloads (status 'downloading' or 'uploading') are reset to
      'queued' so they resume on the next process start rather than being
      silently abandoned.
    • The libtorrent session is paused cleanly.
    • The Telegram bot updater is stopped before the application shuts down.

Usage:
    python main.py
"""

import asyncio
import signal
import sys

from loguru import logger

import api
import auth_store
import config  # noqa: F401 — imported for side-effects (dir creation, validation)
from bot import get_bot_application
from database import get_downloads_by_status, update_status, backfill_episode_keys, prune_queued_episode_duplicates
from queue_processor import run_queue_processor
from rss_worker import rss_worker
from torrent import TorrentManager

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

logger.remove()  # Remove the default stderr sink.
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}",
    level="INFO",
    colorize=True,
)
logger.add(
    "data/omari.log",
    rotation="10 MB",
    retention="14 days",
    level="DEBUG",
    encoding="utf-8",
)


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------

async def _notify_unlinked(bot) -> None:
    """
    Send each authorised Telegram user their personal Dropbox auth link.

    Called once at startup when no refresh token is present. Each user gets
    their own link (embedding their user_id) so the OAuth callback can send
    the confirmation message to the correct chat.
    """
    async def _send(user_id: str) -> None:
        link = (
            f"{config.OAUTH_BASE_URL}/auth/dropbox/start"
            f"?telegram_user_id={user_id}"
        )
        try:
            await bot.send_message(
                chat_id=int(user_id),
                text=(
                    "⚠️ Dropbox is not linked — downloads are paused.\n\n"
                    f"Tap to authorise: {link}"
                ),
            )
            logger.info(f"Sent Dropbox auth prompt to user {user_id}.")
        except Exception as exc:
            logger.warning(f"Could not notify user {user_id} of unlinked state: {exc}")

    await asyncio.gather(*[_send(uid) for uid in config.ALLOWED_USER_IDS])


# ---------------------------------------------------------------------------
# Shutdown helpers
# ---------------------------------------------------------------------------

def _reset_in_progress() -> None:
    """
    Reset any items stuck in transient states back to 'queued'.

    Called on shutdown and on startup (in case the previous process was
    killed mid-pipeline). This ensures items are retried rather than
    left permanently in 'downloading' or 'uploading'.
    """
    for stuck_status in ("downloading", "uploading"):
        items = get_downloads_by_status(stuck_status)
        for item in items:
            update_status(item["identifier"], "queued")
            logger.info(
                f"Reset stuck item to queued: {item.get('title', '?')!r} "
                f"(was {stuck_status!r})"
            )


# ---------------------------------------------------------------------------
# Main coroutine
# ---------------------------------------------------------------------------

async def main() -> None:
    # -----------------------------------------------------------------------
    # Startup: reset any items left in transient states from a previous crash.
    # -----------------------------------------------------------------------
    _reset_in_progress()

    # -----------------------------------------------------------------------
    # Startup: backfill episode keys and prune any duplicate queued records.
    # -----------------------------------------------------------------------
    backfill_episode_keys()
    prune_queued_episode_duplicates()

    # -----------------------------------------------------------------------
    # Initialise shared resources.
    # -----------------------------------------------------------------------
    try:
        tm = TorrentManager()
    except RuntimeError as exc:
        logger.error(str(exc))
        logger.error(
            "Install the system package, then rerun. "
            "Fedora: sudo dnf install rb_libtorrent-python3"
        )
        return

    bot_app = get_bot_application()

    # Inject bot_app into the API module so the OAuth callback can send
    # Telegram notifications after a successful token exchange.
    api.bot_app = bot_app

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()
    logger.info("Telegram bot started.")

    bot = bot_app.bot

    # -----------------------------------------------------------------------
    # Startup: notify all authorised users if Dropbox is not yet linked.
    # -----------------------------------------------------------------------
    if not auth_store.is_linked():
        await _notify_unlinked(bot)

    # -----------------------------------------------------------------------
    # Launch background tasks.
    # -----------------------------------------------------------------------
    rss_task   = asyncio.create_task(rss_worker(),                    name="rss_worker")
    queue_task = asyncio.create_task(run_queue_processor(tm, bot),    name="queue_processor")
    api_task   = asyncio.create_task(api.run_server(),                name="api_server")

    logger.info("All services running. Press Ctrl-C to stop.")

    # -----------------------------------------------------------------------
    # Set up graceful shutdown on SIGINT and SIGTERM.
    # -----------------------------------------------------------------------
    loop        = asyncio.get_running_loop()
    stop_event  = asyncio.Event()

    def _signal_handler(sig: signal.Signals) -> None:
        logger.info(f"Signal {sig.name} received — shutting down…")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            # Windows does not support add_signal_handler for all signals.
            # KeyboardInterrupt from Ctrl-C is still caught by the try/except below.
            pass

    # -----------------------------------------------------------------------
    # Wait until a stop signal is received.
    # -----------------------------------------------------------------------
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    # -----------------------------------------------------------------------
    # Graceful shutdown sequence.
    # -----------------------------------------------------------------------
    logger.info("Cancelling background tasks…")

    # Signal uvicorn to drain connections and run the ASGI lifespan shutdown
    # hook *before* we cancel the task. Cancelling the task directly causes
    # a CancelledError to propagate through Starlette's lifespan receive()
    # which uvicorn logs as an ERROR traceback even though it is harmless.
    await api.stop_server()

    rss_task.cancel()
    queue_task.cancel()
    api_task.cancel()

    await asyncio.gather(rss_task, queue_task, api_task, return_exceptions=True)

    # Reset any items that were mid-flight when tasks were cancelled.
    _reset_in_progress()

    # Pause the libtorrent session cleanly.
    tm.shutdown()

    # Stop the Telegram bot.
    logger.info("Stopping Telegram bot…")
    await bot_app.updater.stop()
    await bot_app.stop()
    await bot_app.shutdown()

    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Already handled inside main(); suppress the traceback.
