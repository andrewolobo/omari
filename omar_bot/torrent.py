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

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional

try:
    import libtorrent as lt
except ModuleNotFoundError:  # pragma: no cover - environment-dependent
    lt = None
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

# Strips "www.site.org    -   " style website prefixes embedded in some torrent names.
_WEBSITE_PREFIX_RE = re.compile(
    r"^[\w.-]+\.(com|org|net|to|io|ru|cc)\s*[-\u2013\u2014]+\s*", re.IGNORECASE
)

# Strips a trailing 4-digit year sitting between the show name and SxxExx,
# e.g. "Invincible 2021 S04E07" → "Invincible".
_TRAILING_YEAR_RE = re.compile(r"\s+(19|20)\d{2}\s*$")

# Common media file extensions to strip from the resolved torrent filename.
_FILE_EXT_RE = re.compile(r"\.(mkv|mp4|avi|mov|wmv|m4v|ts|flv)$", re.IGNORECASE)


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


def parse_show_name(title: str) -> str:
    """
    Extract the show name from a TV episode torrent title.

    Handles the following variations observed in practice:
        - Website prefixes:    "www.UIndex.org    -    DTF St Louis S01E02 ..."
        - File extensions:     "Hacks S05E02 720p WEB H264-JFF[EZTVx.to].mkv"
        - Year before SxxExx:  "Invincible 2021 S04E07 ..." → "Invincible"
        - All-lowercase titles: "hacks s05e01 720p web h264 sylix" → "Hacks"
        - Multi-word titles:   "Monarch Legacy of Monsters S02E08 ..."

    Returns the raw title stripped of its prefix/extension if no SxxExx marker
    is found (safe fallback — the name is still useful as a folder label).
    """
    # Strip website prefix (e.g. "www.UIndex.org    -    ").
    name = _WEBSITE_PREFIX_RE.sub("", title).strip()
    # Strip common media file extensions.
    name = _FILE_EXT_RE.sub("", name).strip()
    # Find the SxxExx marker and take everything before it.
    match = _EPISODE_RE.search(name)
    if not match:
        return name.strip()
    name = name[: match.start()]
    # Strip a trailing year that appeared between the show name and SxxExx.
    name = _TRAILING_YEAR_RE.sub("", name)
    # Strip any trailing punctuation/separator characters left behind.
    name = name.strip(" -\u2013\u2014")
    # Normalise case for fully lowercase or fully uppercase titles.
    # e.g. "hacks" → "Hacks", "INVINCIBLE" → "Invincible"
    if name == name.lower() or name == name.upper():
        name = name.title()
    return name


def parse_episode_key(title: str) -> str | None:
    """
    Derive a normalised (show, episode) deduplication key from a title.

    Returns a string of the form ``"show name:sXXeYY"`` (all lowercase),
    or ``None`` if no SxxExx marker is found (i.e. the title appears to be
    a movie or does not contain a parseable episode code).

    Examples:
        "DTF St Louis S01E07 720p WEB H264 JFF"        → "dtf st louis:s01e07"
        "Invincible 2021 S04E07 DONT DO ANYTHING RASH" → "invincible:s04e07"
        "The Boys S05E03 Every One of You … 720p …"    → "the boys:s05e03"
        "Oppenheimer 2023 2160p BluRay REMUX"           → None
    """
    # Strip website prefix and file extensions before looking for the marker.
    cleaned = _WEBSITE_PREFIX_RE.sub("", title).strip()
    cleaned = _FILE_EXT_RE.sub("", cleaned).strip()

    match = _EPISODE_RE.search(cleaned)
    if not match:
        return None

    # Everything before the SxxExx marker is the show name portion.
    show_raw = cleaned[: match.start()]
    # Strip a trailing year that sits between the show name and the episode marker.
    show_raw = _TRAILING_YEAR_RE.sub("", show_raw)
    # Strip trailing punctuation / separator characters.
    show = show_raw.strip(" -\u2013\u2014").lower()

    # Use the matched SxxExx text lowercased as the episode code.
    episode_code = match.group(0).lower()

    return f"{show}:{episode_code}"


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
        if lt is None:
            raise RuntimeError(
                "libtorrent Python bindings are not installed. "
                "On Fedora install: sudo dnf install rb_libtorrent-python3"
            )

        self.save_path = save_path or config.DOWNLOAD_PATH

        # libtorrent 2.x session initialisation — listen_on() is removed.
        settings = {
            "listen_interfaces": "0.0.0.0:6881",
            "alert_mask": lt.alert.category_t.all_categories,
        }
        self.session = lt.session(settings)
        # Set by shutdown() to signal all blocking thread-pool workers to exit.
        self._stop = threading.Event()
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
        _wait_for_metadata(handle, self._stop)

        name = handle.name()
        logger.info(f"Metadata resolved: {name!r} — starting download")

        # --- Download loop ---
        try:
            _run_download_loop(handle, name, progress_cb, self._stop)
        finally:
            # Always remove the handle to stop seeding after we are done,
            # even if an exception is raised.
            self.session.remove_torrent(handle)
            logger.info(f"Torrent handle removed for: {name!r}")

        return name

    def shutdown(self) -> None:
        """Signal all download threads to stop, then pause the libtorrent session."""
        # Set the stop flag first so any thread blocking in Event.wait() wakes
        # up immediately and exits its loop before we pause the session.
        self._stop.set()
        self.session.pause()
        logger.info("TorrentManager: session paused.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_METADATA_POLL_INTERVAL = 1   # seconds between metadata-wait polls
_DOWNLOAD_POLL_INTERVAL = 5   # seconds between progress polls


def _wait_for_metadata(handle: lt.torrent_handle, stop: threading.Event) -> None:
    """
    Block until torrent metadata is resolved.

    Respects DOWNLOAD_TIMEOUT_MINUTES: if metadata is not received in that
    window (which can happen if a magnet has zero peers), DownloadTimeoutError
    is raised so the item can be moved to 'failed' and retried later.

    Returns immediately if stop is set (shutdown signal).
    """
    timeout_secs = config.DOWNLOAD_TIMEOUT_MINUTES * 60
    deadline = time.monotonic() + timeout_secs

    while not handle.status().has_metadata:
        if time.monotonic() > deadline:
            raise DownloadTimeoutError(
                f"Metadata not received within {config.DOWNLOAD_TIMEOUT_MINUTES} minutes. "
                "The torrent may have no active peers."
            )
        # Event.wait() blocks for up to the poll interval but wakes instantly
        # when stop.set() is called from TorrentManager.shutdown().
        if stop.wait(timeout=_METADATA_POLL_INTERVAL):
            logger.debug("Metadata wait interrupted by shutdown signal.")
            return

    logger.debug("Metadata received.")


def _run_download_loop(
    handle: lt.torrent_handle,
    name: str,
    progress_cb: Optional[Callable[[float], None]],
    stop: threading.Event,
) -> None:
    """
    Poll download progress until the torrent is fully seeded.

    Stall detection: if the downloaded byte count does not increase for
    DOWNLOAD_TIMEOUT_MINUTES, DownloadTimeoutError is raised.

    Exits immediately when stop is set (shutdown signal).

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

        # Block for the poll interval but return immediately if shutdown is
        # signalled. This replaces time.sleep() so the thread never sits idle
        # for up to 5 seconds after Ctrl-C.
        if stop.wait(timeout=_DOWNLOAD_POLL_INTERVAL):
            logger.debug(f"Download loop interrupted by shutdown signal: {name!r}")
            return
