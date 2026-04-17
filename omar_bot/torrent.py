"""
torrent.py — libtorrent 2.x download engine and RSS quality selector.

Public API:
    TorrentManager                    — session wrapper, one instance per process
        .download_magnet(uri, cb)     — synchronous blocking download (run via asyncio.to_thread)

    get_best_quality_per_show(entries) -> dict[str, entry]
        — groups RSS feed entries by normalised show/movie name and returns
          the highest-quality entry per title. Used by rss_worker.

Design notes:
    • download_magnet() is intentionally synchronous — libtorrent's C++ core
      is not async-native. queue_processor wraps it in asyncio.to_thread() so
      it runs in a thread-pool worker without blocking the event loop.
    • Progress is reported via an optional callback rather than print(), so
      queue_processor can forward updates to Telegram without coupling this
      module to the bot.
    • A stall-detection timeout raises DownloadTimeoutError when progress has
      not changed for config.DOWNLOAD_TIMEOUT_MINUTES minutes.
    • The torrent handle is removed from the session upon completion to prevent
      unintended seeding after the file has been uploaded to Dropbox.
"""

import re
import time
from typing import Callable, Optional

import libtorrent as lt
from loguru import logger

import config

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DownloadTimeoutError(RuntimeError):
    """Raised when a torrent makes no progress for DOWNLOAD_TIMEOUT_MINUTES."""


class InvalidMagnetError(ValueError):
    """Raised when the magnet URI cannot be parsed by libtorrent."""


# ---------------------------------------------------------------------------
# Quality selection — RSS feed
# ---------------------------------------------------------------------------

# Resolution tags in descending priority order.
_QUALITY_WEIGHTS: dict[str, int] = {
    "2160p": 4,
    "1080p": 3,
    "720p":  2,
    "480p":  1,
}

# Regex to detect a resolution tag anywhere in a title string.
_RESOLUTION_RE = re.compile(r"(2160p|1080p|720p|480p)", re.IGNORECASE)

# Patterns stripped when normalising a title to a show/movie key.
# Order matters — strip resolution and codec tags before year and release groups.
_STRIP_PATTERNS = [
    re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE),       # resolution
    re.compile(r"\b(BluRay|BDRip|WEB-?DL|WEBRip|HDTV|DVDRip)\b", re.IGNORECASE),  # source
    re.compile(r"\b(x264|x265|HEVC|AVC|H\.?264|H\.?265|XviD)\b", re.IGNORECASE),  # codec
    re.compile(r"\b(AAC|AC3|DTS|DD5\.?1|Atmos|TrueHD)\b", re.IGNORECASE),         # audio
    re.compile(r"\b(EXTENDED|REMASTERED|PROPER|REPACK|INTERNAL)\b", re.IGNORECASE),# flags
    re.compile(r"\b(19|20)\d{2}\b"),                                               # year
    re.compile(r"-[A-Z0-9]+$"),                                                    # release group
    re.compile(r"[\[\(][^\]\)]*[\]\)]"),                                           # bracketed tags
    re.compile(r"[._]"),                                                            # separators
    re.compile(r"\s{2,}"),                                                         # extra spaces
]

# Regex that matches a TV episode marker (S01E01 / s01e01) used to detect media type.
_EPISODE_RE = re.compile(r"\bS\d{1,2}E\d{1,2}\b", re.IGNORECASE)


def _normalise_title(raw: str) -> str:
    """
    Strip resolution, codec, year, and release-group tokens from a title,
    returning a lowercase key suitable for grouping related feed entries
    under the same show or movie name.
    """
    title = raw
    for pattern in _STRIP_PATTERNS:
        title = pattern.sub(" ", title)
    return title.strip().lower()


def detect_media_type(title: str) -> str:
    """
    Return 'tv' if a season/episode marker is present, otherwise 'movie'.
    Used by rss_worker when calling add_download().
    """
    return "tv" if _EPISODE_RE.search(title) else "movie"


def get_best_quality_per_show(entries: list) -> dict[str, object]:
    """
    Group RSS feed entries by normalised show/movie name and return a dict
    mapping each name to its highest-quality entry.

    Args:
        entries: feedparser feed.entries list.

    Returns:
        dict[normalised_name → feedparser entry]

    Example:
        {"breaking bad s05e01": <entry 1080p>, "oppenheimer": <entry 2160p>}
    """
    best: dict[str, tuple[int, object]] = {}  # key → (weight, entry)

    for entry in entries:
        title = getattr(entry, "title", "") or ""
        if not title:
            continue

        key = _normalise_title(title)
        match = _RESOLUTION_RE.search(title)
        weight = _QUALITY_WEIGHTS.get(match.group(1).lower(), 0) if match else 0

        current_weight, _ = best.get(key, (0, None))
        if weight > current_weight:
            best[key] = (weight, entry)

    # Return only the entry objects, keyed by normalised name.
    return {k: v[1] for k, v in best.items()}


# ---------------------------------------------------------------------------
# Torrent engine
# ---------------------------------------------------------------------------

class TorrentManager:
    """
    Thin wrapper around a libtorrent 2.x session.

    One instance should be created per process and shared across all downloads.
    The underlying session manages the peer-wire protocol, DHT, and disk I/O.
    """

    def __init__(self, save_path: Optional[str] = None) -> None:
        self.save_path = save_path or config.DOWNLOAD_PATH

        # libtorrent 2.x session initialisation — listen_on() is removed.
        settings = {
            "listen_interfaces": "0.0.0.0:6881",
            "alert_mask": lt.alert.category_t.all_categories,
        }
        self.session = lt.session(settings)
        logger.info(f"TorrentManager: session started, save_path={self.save_path!r}")

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_magnet(
        self,
        magnet_uri: str,
        progress_cb: Optional[Callable[[float], None]] = None,
    ) -> str:
        """
        Download a torrent from a magnet URI.

        This method BLOCKS the calling thread — it must be called via
        asyncio.to_thread() from the async queue_processor to avoid
        freezing the event loop.

        Args:
            magnet_uri:  A valid magnet URI string.
            progress_cb: Optional callable(pct: float). Called each poll
                         cycle when progress changes by ≥1 percentage point.
                         queue_processor uses this to send Telegram updates.

        Returns:
            The name of the downloaded file or root folder (str).

        Raises:
            InvalidMagnetError:    If libtorrent cannot parse the URI.
            DownloadTimeoutError:  If no progress is made within
                                   config.DOWNLOAD_TIMEOUT_MINUTES.
        """
        # --- Parse magnet URI (libtorrent 2.x API) ---
        try:
            atp = lt.parse_magnet_uri(magnet_uri)
        except Exception as exc:
            raise InvalidMagnetError(f"Cannot parse magnet URI: {exc}") from exc

        atp.save_path = self.save_path
        atp.storage_mode = lt.storage_mode_t.storage_mode_sparse

        handle = self.session.add_torrent(atp)
        logger.info(f"Torrent added, awaiting metadata…")

        # --- Wait for metadata ---
        _wait_for_metadata(handle)

        name = handle.name()
        logger.info(f"Metadata resolved: {name!r} — starting download")

        # --- Download loop ---
        try:
            _run_download_loop(handle, name, progress_cb)
        finally:
            # Always remove the handle to stop seeding after we are done,
            # even if an exception is raised.
            self.session.remove_torrent(handle)
            logger.info(f"Torrent handle removed for: {name!r}")

        return name

    def shutdown(self) -> None:
        """Pause the session cleanly (called from main.py on SIGINT/SIGTERM)."""
        self.session.pause()
        logger.info("TorrentManager: session paused.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_METADATA_POLL_INTERVAL = 1   # seconds between metadata-wait polls
_DOWNLOAD_POLL_INTERVAL = 5   # seconds between progress polls


def _wait_for_metadata(handle: lt.torrent_handle) -> None:
    """
    Block until torrent metadata is resolved.

    Respects DOWNLOAD_TIMEOUT_MINUTES: if metadata is not received in that
    window (which can happen if a magnet has zero peers), DownloadTimeoutError
    is raised so the item can be moved to 'failed' and retried later.
    """
    timeout_secs = config.DOWNLOAD_TIMEOUT_MINUTES * 60
    deadline = time.monotonic() + timeout_secs

    while not handle.status().has_metadata:
        if time.monotonic() > deadline:
            raise DownloadTimeoutError(
                f"Metadata not received within {config.DOWNLOAD_TIMEOUT_MINUTES} minutes. "
                "The torrent may have no active peers."
            )
        time.sleep(_METADATA_POLL_INTERVAL)

    logger.debug("Metadata received.")


def _run_download_loop(
    handle: lt.torrent_handle,
    name: str,
    progress_cb: Optional[Callable[[float], None]],
) -> None:
    """
    Poll download progress until the torrent is fully seeded.

    Stall detection: if the downloaded byte count does not increase for
    DOWNLOAD_TIMEOUT_MINUTES, DownloadTimeoutError is raised.

    Progress callback is fired at most once per percentage point to avoid
    flooding the Telegram API.
    """
    timeout_secs = config.DOWNLOAD_TIMEOUT_MINUTES * 60
    last_progress_pct = -1.0
    last_bytes_downloaded = -1
    stall_deadline = time.monotonic() + timeout_secs

    while True:
        status = handle.status()

        if status.is_seeding:
            # Fire the callback one final time at 100% if not already done.
            if progress_cb and last_progress_pct < 100.0:
                try:
                    progress_cb(100.0)
                except Exception:
                    pass
            logger.info(f"Download complete: {name!r}")
            return

        pct = status.progress * 100.0
        downloaded = status.total_done

        # Stall detection — reset deadline whenever bytes are received.
        if downloaded > last_bytes_downloaded:
            last_bytes_downloaded = downloaded
            stall_deadline = time.monotonic() + timeout_secs
        elif time.monotonic() > stall_deadline:
            raise DownloadTimeoutError(
                f"Download stalled for {config.DOWNLOAD_TIMEOUT_MINUTES} minutes: {name!r}"
            )

        # Emit a log line every poll cycle.
        logger.debug(
            f"{name!r}: {pct:.1f}% | "
            f"↓ {status.download_rate / 1024:.0f} KB/s | "
            f"peers: {status.num_peers}"
        )

        # Fire the progress callback at most once per whole percentage point.
        if progress_cb and pct - last_progress_pct >= 1.0:
            try:
                progress_cb(pct)
            except Exception as exc:
                # A failing callback (e.g. Telegram rate limit) must not
                # crash the download loop.
                logger.warning(f"progress_cb raised: {exc}")
            last_progress_pct = pct

        time.sleep(_DOWNLOAD_POLL_INTERVAL)
