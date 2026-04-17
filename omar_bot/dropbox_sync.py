"""
dropbox_sync.py — Chunked Dropbox upload service.

Public API:
    sync_to_dropbox(local_target_name)  — async; uploads a file or folder tree
                                          then deletes local files only after
                                          confirmed upload success.

Design notes:
    • Auth uses the OAuth2 refresh-token flow (DROPBOX_REFRESH_TOKEN +
      DROPBOX_APP_KEY + DROPBOX_APP_SECRET). The SDK handles token renewal
      automatically — no manual expiry management required.
    • All upload calls pass WriteMode.overwrite so re-uploads of the same
      path do not raise WriteConflictError.
    • Files larger than CHUNK_SIZE are uploaded via the three-step session API:
        1. files_upload_session_start()   — send first chunk, receive session_id
        2. files_upload_session_append_v2() — send middle chunks
        3. files_upload_session_finish()  — send last chunk + commit metadata
    • Local files are deleted ONLY after files_upload_session_finish() (or
      files_upload() for small files) returns a FileMetadata object. If any
      exception is raised the local file is preserved for retry.
    • Dropbox HTTP 429 (RateLimitError) is retried up to MAX_RETRIES times,
      honouring the Retry-After header value supplied by the SDK.
    • sync_to_dropbox() is an async function that offloads the blocking I/O
      to a thread-pool worker via asyncio.to_thread(), keeping the event loop
      free for the Telegram bot and RSS worker.
"""

import asyncio
import os
import time
from pathlib import Path
from typing import Optional

import dropbox
import dropbox.files
import dropbox.exceptions
from loguru import logger

import auth_store
import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB per chunk
MAX_RETRIES = 5                  # Maximum RateLimitError retries per chunk call


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def _make_client() -> dropbox.Dropbox:
    """
    Return a Dropbox client authenticated via the refresh-token flow.

    Token resolution order:
        1. auth_store (written by /start command — takes precedence)
        2. config.DROPBOX_REFRESH_TOKEN (.env static fallback)

    Raises RuntimeError if no token is available from either source.
    """
    refresh_token = auth_store.load_refresh_token() or config.DROPBOX_REFRESH_TOKEN
    if not refresh_token:
        raise RuntimeError(
            "Dropbox is not linked. Send /start to the Telegram bot to "
            "authorize your Dropbox account."
        )
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=config.DROPBOX_APP_KEY,
        app_secret=config.DROPBOX_APP_SECRET,
    )


# ---------------------------------------------------------------------------
# Rate-limit aware call wrapper
# ---------------------------------------------------------------------------

def _call_with_retry(fn, *args, **kwargs):
    """
    Call a Dropbox SDK function, retrying on RateLimitError up to MAX_RETRIES
    times. Sleeps for the duration specified in the error's retry_after field.

    Raises the original RateLimitError if all retries are exhausted.
    Propagates any other exception immediately.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except dropbox.exceptions.RateLimitError as exc:
            retry_after = getattr(exc.error, "retry_after", 5) or 5
            if attempt == MAX_RETRIES:
                logger.error(
                    f"Dropbox rate limit hit {MAX_RETRIES} times — giving up."
                )
                raise
            logger.warning(
                f"Dropbox rate limit (attempt {attempt}/{MAX_RETRIES}). "
                f"Sleeping {retry_after}s…"
            )
            time.sleep(retry_after)


# ---------------------------------------------------------------------------
# Single-file upload
# ---------------------------------------------------------------------------

def _upload_file(dbx: dropbox.Dropbox, local_path: str, dropbox_path: str) -> None:
    """
    Upload a single local file to Dropbox, deleting it locally only after a
    successful upload is confirmed.

    Small files (≤ CHUNK_SIZE) use the single-request endpoint.
    Large files use the three-step chunked session API.

    Args:
        dbx:          Authenticated Dropbox client.
        local_path:   Absolute path to the local file.
        dropbox_path: Destination path in Dropbox (must start with '/').

    Raises:
        Any Dropbox SDK exception on unrecoverable upload failure.
        Local file is NOT deleted on failure.
    """
    file_size = os.path.getsize(local_path)
    overwrite = dropbox.files.WriteMode.overwrite

    with open(local_path, "rb") as f:
        if file_size <= CHUNK_SIZE:
            # --- Small file: single request ---
            logger.debug(f"Uploading (single): {local_path} → {dropbox_path}")
            result = _call_with_retry(
                dbx.files_upload,
                f.read(),
                dropbox_path,
                mode=overwrite,
            )
        else:
            # --- Large file: chunked session ---
            logger.debug(
                f"Uploading (chunked, {file_size / 1024 ** 3:.2f} GB): "
                f"{local_path} → {dropbox_path}"
            )

            # Step 1: start session with the first chunk.
            start_result = _call_with_retry(
                dbx.files_upload_session_start,
                f.read(CHUNK_SIZE),
            )
            cursor = dropbox.files.UploadSessionCursor(
                session_id=start_result.session_id,
                offset=f.tell(),
            )
            commit = dropbox.files.CommitInfo(
                path=dropbox_path,
                mode=overwrite,
            )

            # Step 2: middle chunks.
            while f.tell() < file_size:
                remaining = file_size - f.tell()

                if remaining <= CHUNK_SIZE:
                    # Step 3: final chunk — commit the session.
                    result = _call_with_retry(
                        dbx.files_upload_session_finish,
                        f.read(CHUNK_SIZE),
                        cursor,
                        commit,
                    )
                    break
                else:
                    _call_with_retry(
                        dbx.files_upload_session_append_v2,
                        f.read(CHUNK_SIZE),
                        cursor,
                    )
                    cursor.offset = f.tell()

    # Confirm result is FileMetadata before deleting local file.
    if not isinstance(result, dropbox.files.FileMetadata):
        raise RuntimeError(
            f"Upload did not return FileMetadata for {dropbox_path!r}. "
            f"Got: {result!r}. Local file preserved."
        )

    # Safe delete — only reached if upload succeeded.
    os.remove(local_path)
    logger.info(f"Uploaded and cleaned up: {local_path}")


# ---------------------------------------------------------------------------
# Top-level sync entry point (synchronous inner function)
# ---------------------------------------------------------------------------

def _dropbox_root(media_type: str) -> str:
    """
    Return the Dropbox destination directory for a given media type.

    Strips surrounding slashes so paths can be composed consistently
    with a leading '/' prefix.
    """
    if media_type == "tv":
        return config.SHOWS_DIRECTORY.strip("/")
    elif media_type == "movie":
        return config.MOVIES_DIRECTORY.strip("/")
    else:
        # Unknown media type — upload to Dropbox root to avoid data loss.
        logger.warning(
            f"Unknown media_type {media_type!r}; uploading to Dropbox root."
        )
        return ""


def _sync_blocking(local_target_name: str, media_type: str = "unknown") -> None:
    """
    Synchronous worker: upload a downloaded file or folder tree to Dropbox,
    then delete all local content.

    Files are routed to SHOWS_DIRECTORY or MOVIES_DIRECTORY in Dropbox based
    on media_type ('tv' | 'movie' | 'unknown').

    Called via asyncio.to_thread() from sync_to_dropbox() so it runs in a
    thread-pool worker without blocking the event loop.

    Args:
        local_target_name: The file or folder name returned by TorrentManager,
                           relative to config.DOWNLOAD_PATH.
        media_type:        'tv', 'movie', or 'unknown'.
    """
    dbx = _make_client()
    base_path = os.path.join(config.DOWNLOAD_PATH, local_target_name)
    dest_root = _dropbox_root(media_type)

    if not os.path.exists(base_path):
        raise FileNotFoundError(
            f"sync_to_dropbox: path does not exist: {base_path!r}"
        )

    if os.path.isfile(base_path):
        # --- Single file ---
        dropbox_dest = f"/{dest_root}/{local_target_name}" if dest_root else f"/{local_target_name}"
        _upload_file(dbx, base_path, dropbox_dest)

    else:
        # --- Folder: walk the tree, preserving structure in Dropbox ---
        logger.info(f"Syncing folder: {base_path!r} → Dropbox:/{dest_root}/")
        for root, dirs, files in os.walk(base_path):
            # Sort for deterministic order and easier debugging.
            dirs.sort()
            for filename in sorted(files):
                local_file = os.path.join(root, filename)
                # Build Dropbox path preserving directory structure under the
                # media-type destination root.
                relative = os.path.relpath(local_file, config.DOWNLOAD_PATH)
                relative_fwd = relative.replace(os.sep, "/")
                dropbox_dest = f"/{dest_root}/{relative_fwd}" if dest_root else f"/{relative_fwd}"
                _upload_file(dbx, local_file, dropbox_dest)

        # Remove now-empty directories bottom-up.
        for root, dirs, files in os.walk(base_path, topdown=False):
            for d in dirs:
                dir_path = os.path.join(root, d)
                try:
                    os.rmdir(dir_path)
                except OSError:
                    logger.warning(f"Could not remove directory (not empty?): {dir_path}")
        try:
            os.rmdir(base_path)
        except OSError:
            logger.warning(f"Could not remove root directory: {base_path}")

    logger.info(f"Dropbox sync complete for: {local_target_name!r}")


# ---------------------------------------------------------------------------
# Async public entry point
# ---------------------------------------------------------------------------

async def sync_to_dropbox(local_target_name: str, media_type: str = "unknown") -> None:
    """
    Async wrapper: upload a downloaded file or folder to Dropbox.

    Offloads all blocking file I/O and Dropbox API calls to a thread-pool
    worker via asyncio.to_thread(), so the Telegram bot and RSS worker
    remain responsive during long uploads.

    Args:
        local_target_name: File/folder name as returned by TorrentManager.download_magnet().
        media_type:        'tv', 'movie', or 'unknown'. Controls the Dropbox
                           destination directory (SHOWS_DIRECTORY / MOVIES_DIRECTORY).

    Raises:
        FileNotFoundError: If the local path does not exist.
        RuntimeError:      If the Dropbox API does not confirm the upload.
        dropbox.exceptions.RateLimitError: If MAX_RETRIES rate-limit retries are exhausted.
    """
    await asyncio.to_thread(_sync_blocking, local_target_name, media_type)
