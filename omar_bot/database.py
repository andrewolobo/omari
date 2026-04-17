"""
database.py — TinyDB state management.

Tracks the full lifecycle of every download:
    queued -> downloading -> uploading -> completed -> failed

Each record schema:
    identifier      str   — magnet URI or RSS entry link (primary key)
    title           str   — human-readable name
    source_type     str   — 'magnet' | 'rss'
    media_type      str   — 'tv' | 'movie' | 'unknown'
    status          str   — lifecycle state (see above)
    chat_id         int   — Telegram chat_id to notify on completion/failure
    target_name     str   — local file/folder name set after download completes
    retry_count     int   — number of pipeline retries attempted
    added_at        float — Unix timestamp of initial insert
    updated_at      float — Unix timestamp of last status change
"""

import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger
from tinydb import TinyDB, Query

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

Path("data").mkdir(exist_ok=True)

_db = TinyDB("data/db.json")
_Downloads = Query()

# Lock to make the search-then-insert in add_download() atomic across
# the RSS worker threads that may run concurrently.
_lock = threading.Lock()

# Valid media type values — enforced at insert time.
MEDIA_TYPES = {"tv", "movie", "unknown"}

# Valid lifecycle statuses.
STATUSES = {"queued", "downloading", "uploading", "completed", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_download(
    identifier: str,
    source_type: str,
    title: str = "Unknown",
    chat_id: int = 0,
    media_type: str = "unknown",
) -> bool:
    """
    Insert a new download record if it does not already exist.

    Returns True if inserted (new), False if the identifier already exists.
    Thread-safe: protected by a module-level lock to prevent duplicate inserts
    from concurrent RSS worker ticks.

    Args:
        identifier:  Unique key — magnet URI or RSS entry link.
        source_type: 'magnet' or 'rss'.
        title:       Human-readable display name.
        chat_id:     Telegram chat_id from the originating message (0 for RSS).
        media_type:  'tv', 'movie', or 'unknown'.
    """
    if media_type not in MEDIA_TYPES:
        logger.warning(
            f"add_download: unexpected media_type '{media_type}' for '{title}'. "
            f"Falling back to 'unknown'."
        )
        media_type = "unknown"

    with _lock:
        if _db.search(_Downloads.identifier == identifier):
            logger.debug(f"Duplicate skipped: {title!r} ({identifier[:60]}…)")
            return False

        now = time.time()
        _db.insert(
            {
                "identifier": identifier,
                "title": title,
                "source_type": source_type,
                "media_type": media_type,
                "status": "queued",
                "chat_id": chat_id,
                "target_name": None,
                "retry_count": 0,
                "added_at": now,
                "updated_at": now,
            }
        )
        logger.info(f"Queued [{media_type.upper()}] {title!r} via {source_type}")
        return True


def update_status(identifier: str, new_status: str, **extra_fields: Any) -> None:
    """
    Update the lifecycle status of a download record.

    Any additional keyword arguments are written to the record atomically in
    the same update call (e.g. target_name='My.Show.S01E01').

    Args:
        identifier: The record's unique key.
        new_status: Target status — must be one of STATUSES.
        **extra_fields: Optional additional fields to update (e.g. target_name).
    """
    if new_status not in STATUSES:
        raise ValueError(
            f"update_status: '{new_status}' is not a valid status. "
            f"Must be one of: {', '.join(sorted(STATUSES))}"
        )

    payload: dict[str, Any] = {"status": new_status, "updated_at": time.time()}
    payload.update(extra_fields)

    _db.update(payload, _Downloads.identifier == identifier)
    logger.debug(f"Status → {new_status!r} | {identifier[:60]}…")


def get_downloads_by_status(status: str) -> list[dict]:
    """Return all records matching the given lifecycle status."""
    if status not in STATUSES:
        raise ValueError(
            f"get_downloads_by_status: '{status}' is not a valid status."
        )
    return _db.search(_Downloads.status == status)


def increment_retry(identifier: str) -> int:
    """
    Atomically increment retry_count for a record.

    Returns the new retry count so the caller can decide whether to
    re-queue or permanently mark the item as failed.
    """
    with _lock:
        results = _db.search(_Downloads.identifier == identifier)
        if not results:
            logger.warning(f"increment_retry: identifier not found — {identifier[:60]}…")
            return 0

        current = results[0].get("retry_count", 0)
        new_count = current + 1
        _db.update(
            {"retry_count": new_count, "updated_at": time.time()},
            _Downloads.identifier == identifier,
        )
        logger.debug(f"Retry count → {new_count} | {identifier[:60]}…")
        return new_count


def get_download(identifier: str) -> dict | None:
    """Return a single record by identifier, or None if not found."""
    results = _db.search(_Downloads.identifier == identifier)
    return results[0] if results else None


def get_recent(status: str, limit: int = 10) -> list[dict]:
    """
    Return the most recent `limit` records with the given status,
    ordered newest-first by updated_at.
    """
    records = get_downloads_by_status(status)
    return sorted(records, key=lambda r: r.get("updated_at", 0), reverse=True)[:limit]
