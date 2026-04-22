"""
rss_worker.py — Automated RSS feed poller.

Polls all feeds in config.RSS_URLS on a configurable interval, selects the
highest-quality release per show episode, and queues new entries via
database.add_download().

Feed structure (showRSS with magnets=true&namespaces=true):
    entry.title         — "Hacks S05E02 720p WEB H264 JFF"
    entry.link          — full magnet URI (used as download identifier)
    entry.tv_show_name  — canonical show name e.g. "The Boys"
    entry.tv_show_id    — numeric show ID (string)
    entry.tv_episode_id — unique numeric episode ID (string) — the stable
                          deduplication key; same episode at different
                          qualities shares this ID.
    entry.tv_info_hash  — 40-char hex infohash

Grouping strategy:
    Primary  — (tv_show_id, tv_episode_id): exact match on the showRSS
               namespace fields. This correctly handles multiple quality
               variants of the same episode appearing in one feed poll.
    Fallback — normalised title key from torrent.get_best_quality_per_show(),
               used when the tv: namespace fields are absent (non-showRSS feeds).

All entries are treated as media_type='tv' because showRSS is a TV-only
service. Generic RSS feeds fall back to torrent.detect_media_type().
"""

import asyncio

import feedparser
from loguru import logger

from . import config
from .database import add_download
from .torrent import detect_media_type, get_best_quality_per_show, _RESOLUTION_RE, _QUALITY_WEIGHTS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tv_field(entry: feedparser.FeedParserDict, name: str) -> str:
    """
    Return a showRSS tv: namespace field from a feedparser entry.

    feedparser maps namespaced elements using the document-declared prefix,
    so <tv:show_name> becomes entry['tv_show_name']. Some feedparser builds
    may instead use the namespace URI to derive a prefix ('showrss_'); we
    try both to be safe.
    """
    return (
        entry.get(f"tv_{name}")
        or entry.get(f"showrss_{name}")
        or ""
    )


def _magnet_from_entry(entry: feedparser.FeedParserDict) -> str:
    """
    Extract the magnet URI from an entry.

    showRSS places the magnet in <link> and also in <enclosure url="...">.
    feedparser exposes <link> as entry.link and <enclosure> as
    entry.enclosures[0].href. Prefer <link> as it is always present.
    """
    link = entry.get("link", "")
    if link.startswith("magnet:"):
        return link

    # Fallback: enclosure
    for enc in entry.get("enclosures", []):
        href = enc.get("href", "")
        if href.startswith("magnet:"):
            return href

    return ""


def _quality_weight(title: str) -> int:
    """Return the numeric quality weight for a title string (0 if no tag found)."""
    match = _RESOLUTION_RE.search(title)
    if not match:
        return 0
    return _QUALITY_WEIGHTS.get(match.group(1).lower(), 0)


# ---------------------------------------------------------------------------
# Per-feed processing
# ---------------------------------------------------------------------------

def _process_feed(feed_url: str) -> int:
    """
    Parse a single RSS feed, select the best quality release per episode,
    and queue any new entries.

    Returns the number of new items queued.
    """
    feed = feedparser.parse(feed_url)

    if feed.bozo:
        # bozo=True means feedparser encountered a malformed feed.
        logger.warning(
            f"Feed parse warning for {feed_url!r}: {feed.bozo_exception}"
        )

    entries = feed.get("entries", [])
    if not entries:
        logger.debug(f"No entries in feed: {feed_url!r}")
        return 0

    # -----------------------------------------------------------------------
    # Group entries by episode using showRSS namespace fields if available,
    # then fall back to the regex-based normaliser from torrent.py.
    # -----------------------------------------------------------------------

    has_namespace = bool(_tv_field(entries[0], "episode_id"))

    if has_namespace:
        # Primary path: group by (show_id, episode_id) — exact and reliable.
        groups: dict[str, tuple[int, feedparser.FeedParserDict]] = {}
        for entry in entries:
            show_id    = _tv_field(entry, "show_id")
            episode_id = _tv_field(entry, "episode_id")
            if not show_id or not episode_id:
                continue
            key    = f"{show_id}:{episode_id}"
            weight = _quality_weight(entry.get("title", ""))
            current_weight, _ = groups.get(key, (0, None))
            if weight > current_weight:
                groups[key] = (weight, entry)

        best_per_episode = {k: v[1] for k, v in groups.items()}
    else:
        # Fallback path: regex-based normalised title grouping.
        best_per_episode = get_best_quality_per_show(entries)

    # -----------------------------------------------------------------------
    # Queue new entries.
    # -----------------------------------------------------------------------

    queued_count = 0
    for key, entry in best_per_episode.items():
        magnet = _magnet_from_entry(entry)
        if not magnet:
            logger.warning(f"No magnet URI found for entry: {entry.get('title')!r}")
            continue

        title = entry.get("title", "Unknown")

        # Determine media type — showRSS is TV-only; generic feeds use regex.
        if has_namespace:
            media_type = "tv"
        else:
            media_type = detect_media_type(title)

        # Use show name from namespace if available, else raw title.
        show_name = _tv_field(entry, "show_name") or title

        added = add_download(
            identifier=magnet,
            source_type="rss",
            title=title,
            chat_id=config.NOTIFICATION_CHAT_ID,  # 0 if unset → notify_user() no-ops cleanly.
            media_type=media_type,
        )
        if added:
            logger.info(f"RSS queued [{media_type.upper()}]: {title!r}")
            queued_count += 1

    return queued_count


# ---------------------------------------------------------------------------
# Async background worker
# ---------------------------------------------------------------------------

async def rss_worker() -> None:
    """
    Background coroutine — runs for the lifetime of the process.

    Polls all feeds in config.RSS_URLS in parallel every
    config.RSS_POLL_INTERVAL seconds. Each feed is isolated in its own
    try/except so a broken or unreachable feed does not stop others.

    Designed to be launched with asyncio.create_task() from main.py.
    """
    logger.info(
        f"RSS worker started. Polling {len(config.RSS_URLS)} feed(s) "
        f"every {config.RSS_POLL_INTERVAL}s."
    )

    while True:
        if not config.RSS_URLS:
            logger.debug("RSS worker: no feeds configured, sleeping.")
        else:
            # Run all feed polls concurrently; each is wrapped individually.
            tasks = [
                asyncio.to_thread(_poll_single_feed, url)
                for url in config.RSS_URLS
            ]
            await asyncio.gather(*tasks)

        await asyncio.sleep(config.RSS_POLL_INTERVAL)


def _poll_single_feed(feed_url: str) -> None:
    """
    Thread-pool worker: fetch and process one feed URL.
    Isolated try/except so a broken feed never propagates to the gather().
    feedparser.parse() is blocking (HTTP request), so this runs via
    asyncio.to_thread() inside rss_worker().
    """
    try:
        count = _process_feed(feed_url)
        logger.debug(
            f"RSS poll complete: {feed_url!r} — {count} new item(s) queued."
        )
    except Exception as exc:
        logger.error(f"RSS poll failed for {feed_url!r}: {exc}")
