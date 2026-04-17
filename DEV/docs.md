# OMARI Bot — Technical Documentation

**Service:** Headless, automated BitTorrent-to-Dropbox downloader  
**Control surface:** Telegram bot + RSS feed  
**Runtime:** Python 3.11+ · asyncio · libtorrent 2.x · TinyDB · Dropbox SDK  
**Entry point:** `python omar_bot/main.py`

---

## Architecture Overview

```
Telegram User
     │
     ▼ /rent <magnet> [tv|movie]
  bot.py ──────────────────────────────────────────────────┐
     │                                                      │
     ▼ add_download()                                       │ notify_user()
  database.py  ◄──── rss_worker.py (RSS feed polls)        │
     │                                                      │
     ▼ get_downloads_by_status('queued')                   │
  queue_processor.py                                        │
     │                                                      │
     ├── asyncio.to_thread ──► torrent.py (download)       │
     │         progress_cb ───────────────────────────────►─┘
     │
     └── await ──────────────► dropbox_sync.py (upload)
                                      │
                                      ▼ os.remove() after FileMetadata confirmed
                               Local file deleted
```

All three long-running services (`bot`, `rss_worker`, `queue_processor`) run concurrently under a single `asyncio` event loop managed by `main.py`. Blocking operations (`download_magnet`, `_sync_blocking`) are offloaded to thread-pool workers via `asyncio.to_thread()` to keep the event loop responsive.

---

## Module Reference

### `config.py`

**Role:** Single source of truth for all runtime configuration. Loads a `.env` file at import time and exposes typed constants. Every other module imports `config` instead of reading `os.environ` directly.

**Key constants:**

| Constant | Type | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | `str` | Bot token from BotFather |
| `ALLOWED_USER_IDS` | `list[str]` | Authorised Telegram user IDs |
| `DROPBOX_REFRESH_TOKEN` | `str` | Long-lived OAuth2 refresh token |
| `DROPBOX_APP_KEY` | `str` | Dropbox app key |
| `DROPBOX_APP_SECRET` | `str` | Dropbox app secret |
| `RSS_URLS` | `list[str]` | Feed URLs to poll |
| `RSS_POLL_INTERVAL` | `int` | Seconds between feed polls (default 900) |
| `MAX_CONCURRENT_DOWNLOADS` | `int` | Semaphore cap (default 2) |
| `DOWNLOAD_PATH` | `str` | Local download directory (default `./downloads`) |
| `DOWNLOAD_TIMEOUT_MINUTES` | `int` | Stall detection threshold (default 30) |

**Side effects on import:** Creates `downloads/` and `data/` directories if absent. Raises `RuntimeError` immediately for missing required secrets (`TELEGRAM_TOKEN`, all three Dropbox credentials).

---

### `database.py`

**Role:** TinyDB state management. Tracks every download through its full lifecycle and provides a thread-safe API used by all other modules.

#### Record schema

| Field | Type | Description |
|---|---|---|
| `identifier` | `str` | Primary key — magnet URI or RSS entry link |
| `title` | `str` | Human-readable display name |
| `source_type` | `str` | `'magnet'` or `'rss'` |
| `media_type` | `str` | `'tv'`, `'movie'`, or `'unknown'` |
| `status` | `str` | Current lifecycle state (see below) |
| `chat_id` | `int` | Telegram chat to notify; `0` for RSS-sourced |
| `target_name` | `str\|None` | Local file/folder name after download |
| `retry_count` | `int` | Number of pipeline retries attempted |
| `added_at` | `float` | Unix timestamp — initial insert |
| `updated_at` | `float` | Unix timestamp — last status change |

#### Lifecycle states

```
queued ──► downloading ──► uploading ──► completed
  ▲              │               │
  │   (retry)    ▼               ▼
  └──────────  failed          failed
                            cancelled  (via /cancel command)
```

#### Public functions

**`add_download(identifier, source_type, title, chat_id, media_type) → bool`**  
Insert a new record if the `identifier` does not already exist. Returns `True` if inserted (new), `False` if it was a duplicate. Protected by a `threading.Lock` to prevent race conditions from concurrent RSS worker ticks inserting the same entry simultaneously.

**`update_status(identifier, new_status, **extra_fields) → None`**  
Update lifecycle status and `updated_at` timestamp in a single atomic write. The `**extra_fields` mechanism allows additional fields (e.g. `target_name`, `title`) to be updated in the same call.

**`get_downloads_by_status(status) → list[dict]`**  
Return all records matching the given status. Used by `queue_processor` to find `'queued'` items each poll cycle.

**`increment_retry(identifier) → int`**  
Atomically increment `retry_count` and return the new value. Protected by `threading.Lock`. Used by `queue_processor` to decide whether to re-queue or permanently fail an item.

**`get_download(identifier) → dict | None`**  
Return a single record by identifier.

**`get_recent(status, limit=10) → list[dict]`**  
Return the most recent `limit` records with the given status, sorted by `updated_at` descending. Used by the `/list` bot command.

---

### `bot.py`

**Role:** Telegram command handlers and the `notify_user` notification helper. Only users whose IDs appear in `config.ALLOWED_USER_IDS` can issue commands.

#### Public functions

**`get_bot_application() → telegram.ext.Application`**  
Factory called once by `main.py`. Builds the `ApplicationBuilder` with `TELEGRAM_TOKEN` and registers all command handlers. Returns the configured app ready for `initialize()` and `start()`.

**`notify_user(bot, chat_id, message) → None`** *(async)*  
Shared notification helper. Sends a plain-text Telegram message to `chat_id`. Used by `queue_processor` for completion/failure alerts and by the progress callback for download updates. No-ops silently when `chat_id=0` (RSS-sourced items with no originating chat). Catches and logs `telegram` exceptions without propagating them.

#### Command handlers

| Command | Handler | Behaviour |
|---|---|---|
| `/help` | `help_command` | Lists all available commands |
| `/rent <magnet_uri> [tv\|movie]` | `rent_command` | Validates magnet URI via full regex (`btih:` + 40-char hex or 32-char base32 hash), accepts optional media type hint, stores `chat_id` in DB, queues the download |
| `/status` | `status_command` | Lists all items in `queued`, `downloading`, or `uploading` state |
| `/list` | `list_command` | Shows the 10 most recent `completed` or `failed` items |
| `/cancel <id_prefix>` | `cancel_command` | Prefix-matches against `queued` items only; disambiguates if prefix is too short; marks matched item as `cancelled` |

**Magnet validation regex:**
```
^magnet:\?xt=urn:btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})
```
Accepts both SHA-1 (40 hex chars) and BitTorrent v2 (32 base32 chars) info-hashes.

---

### `torrent.py`

**Role:** libtorrent 2.x download engine and RSS quality selector. Contains the blocking download logic (run via `asyncio.to_thread()`) and the regex tooling for normalising feed titles.

#### Exceptions

**`DownloadTimeoutError(RuntimeError)`**  
Raised when a torrent makes no byte-level progress for `DOWNLOAD_TIMEOUT_MINUTES`. Indicates a stalled or dead torrent. Triggers the retry logic in `queue_processor`.

**`InvalidMagnetError(ValueError)`**  
Raised when libtorrent cannot parse the magnet URI. These items are never retried — they go straight to `'failed'`.

#### `TorrentManager` class

Wraps a single `lt.session` instance. One instance is created per process in `main.py` and shared across all concurrent downloads.

**`__init__(save_path=None)`**  
Initialises the libtorrent 2.x session using the settings dict pattern (replaces the deprecated `session.listen_on()`). Sets `listen_interfaces` to `0.0.0.0:6881`.

**`download_magnet(magnet_uri, progress_cb=None) → str`**  
Synchronous blocking download. Must be called via `asyncio.to_thread()`. Internally:
1. Parses the URI with `lt.parse_magnet_uri()` (libtorrent 2.x API — replaces deprecated `lt.add_magnet_uri()`)
2. Sets `storage_mode_sparse` (replaces deprecated `lt.storage_mode_t(2)`)
3. Calls `_wait_for_metadata()` — blocks until peers resolve the info-hash, or raises `DownloadTimeoutError`
4. Calls `_run_download_loop()` — polls progress every 5 seconds, fires `progress_cb` on each 1% milestone, raises `DownloadTimeoutError` if bytes stall for `DOWNLOAD_TIMEOUT_MINUTES`
5. Removes the torrent handle from the session in a `finally` block to stop seeding
6. Returns the resolved torrent name (file or root folder)

**`shutdown()`**  
Pauses the libtorrent session. Called by `main.py` during graceful shutdown.

#### Quality selector functions

**`detect_media_type(title) → str`**  
Returns `'tv'` if the title contains an `S01E01`-style episode marker, otherwise `'movie'`. Used by `rss_worker` when calling `add_download()`.

**`get_best_quality_per_show(entries) → dict[str, entry]`**  
Regex-based fallback grouping for generic RSS feeds without structured namespace fields. Normalises each entry title by stripping resolution tags, codec names, audio formats, years, release groups, and separators. Groups entries by normalised key, retaining the highest-quality entry per group.

Quality weights: `2160p=4`, `1080p=3`, `720p=2`, `480p=1`.

Returns `dict[normalised_show_name → feedparser entry]`.

---

### `dropbox_sync.py`

**Role:** Chunked Dropbox upload service with OAuth2 refresh-token authentication, rate-limit retry, and safe-delete guarantees.

#### Public function

**`sync_to_dropbox(local_target_name) → None`** *(async)*  
Async entry point. Offloads all blocking I/O to a thread-pool worker via `asyncio.to_thread(_sync_blocking, local_target_name)`. Raises `FileNotFoundError` if the local path does not exist.

#### Internal functions

**`_make_client() → dropbox.Dropbox`**  
Returns a Dropbox client using the OAuth2 refresh-token flow (`DROPBOX_REFRESH_TOKEN` + `DROPBOX_APP_KEY` + `DROPBOX_APP_SECRET`). The SDK handles access token renewal automatically. A new client is created per sync call — no module-level singleton.

**`_call_with_retry(fn, *args, **kwargs)`**  
Retry wrapper for any single Dropbox SDK call. On `RateLimitError`, reads `exc.error.retry_after` from the SDK response and sleeps before retrying. After `MAX_RETRIES` (5) attempts, re-raises the error.

**`_upload_file(dbx, local_path, dropbox_path) → None`**  
Uploads a single file. Selects the upload strategy based on file size:
- **≤ 100 MB:** `files_upload()` — single request
- **> 100 MB:** three-step chunked session:
  1. `files_upload_session_start()` — first 100 MB chunk → session ID
  2. `files_upload_session_append_v2()` — middle chunks
  3. `files_upload_session_finish()` — final chunk + commit

All calls use `WriteMode.overwrite` to prevent `WriteConflictError` on re-upload.

**Safe-delete guarantee:** `os.remove()` is called only after `files_upload_session_finish()` (or `files_upload()`) returns a `dropbox.files.FileMetadata` object. Any exception exits before reaching the delete call, preserving the local file for retry.

**`_sync_blocking(local_target_name) → None`**  
Synchronous inner function run in a thread-pool worker. Handles two cases:
- **Single file:** uploads directly, then deletes
- **Folder tree:** walks with `os.walk()`, uploads each file preserving Dropbox directory structure, removes empty directories bottom-up after all files are uploaded

---

### `rss_worker.py`

**Role:** Background RSS feed poller. Polls all configured feeds in parallel, selects the highest-quality release per show episode, and queues new entries.

#### Feed source — showRSS

The configured feed (`showrss.info`) provides a `xmlns:tv` namespace with structured episode metadata:

| feedparser attribute | XML element | Example |
|---|---|---|
| `entry.title` | `<title>` | `Hacks S05E02 720p WEB H264 JFF` |
| `entry.link` | `<link>` | Full magnet URI |
| `entry.tv_show_name` | `<tv:show_name>` | `The Boys` |
| `entry.tv_show_id` | `<tv:show_id>` | `1183` |
| `entry.tv_episode_id` | `<tv:episode_id>` | `235930` |
| `entry.tv_info_hash` | `<tv:info_hash>` | `3644F718…` |

#### Grouping strategy

**Primary (showRSS):** Group by `(tv_show_id, tv_episode_id)`. This correctly handles the same episode appearing multiple times at different qualities (e.g. Daredevil S02E05 at 720p and 1080p). The best quality within each group is selected.

**Fallback (generic feeds):** Delegates to `torrent.get_best_quality_per_show()` — regex title normalisation. Activated automatically when the `tv:` namespace fields are absent.

#### Public function

**`rss_worker() → None`** *(async coroutine)*  
Long-running background coroutine. Each iteration:
1. Launches one `asyncio.to_thread(_poll_single_feed, url)` per URL in `config.RSS_URLS`
2. Awaits all feeds in parallel via `asyncio.gather()`
3. Sleeps for `config.RSS_POLL_INTERVAL` seconds

Each feed is processed in its own isolated try/except block inside `_poll_single_feed` — a dead or malformed feed does not prevent other feeds from being polled.

All showRSS entries get `media_type='tv'` and `chat_id=0` (no originating Telegram chat).

---

### `queue_processor.py`

**Role:** Async download-and-upload pipeline. Polls the database for queued items, dispatches each as a concurrent `asyncio.Task`, and orchestrates the full pipeline from download through to Dropbox upload and user notification.

#### Public function

**`run_queue_processor(tm, bot) → None`** *(async coroutine)*  
Long-running background coroutine. Each 10-second poll cycle:
1. Calls `get_downloads_by_status('queued')`
2. For each item not already in `active_ids`, creates an `asyncio.Task` via `asyncio.create_task()`
3. Tasks are bounded by `asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)`

An `active_ids: set[str]` prevents duplicate tasks from being spawned for the same identifier between the `create_task()` call and the task's first `update_status('downloading')`.

#### Pipeline (per item)

```
update_status('downloading')
        │
        ▼
asyncio.to_thread(tm.download_magnet, identifier, progress_cb)
        │  ← fires progress_cb every 1% → send Telegram message every 10%
        ▼
update_status('uploading', target_name=name, title=name)
        │
        ▼
await sync_to_dropbox(name)
        │
        ▼
update_status('completed')
        │
        ▼
notify_user(bot, chat_id, "✅ Done: {name}")
```

#### Retry logic

On any exception (other than `InvalidMagnetError` or `CancelledError`):

| `retry_count` after increment | Action |
|---|---|
| 1 | Reset to `'queued'`, sleep 30 s |
| 2 | Reset to `'queued'`, sleep 120 s |
| 3 | Reset to `'queued'`, sleep 300 s |
| 4+ | Mark `'failed'`, `notify_user(…, "❌ Failed…")` |

`InvalidMagnetError` → immediately `'failed'`, no retry  
`CancelledError` → reset to `'queued'` (graceful shutdown), re-raise

#### Progress callback

**`_make_progress_callback(bot, chat_id, title, loop) → Callable[[float], None]`**  
Returns a thread-safe callback suitable for passing to `TorrentManager.download_magnet()`. The callback runs in a thread-pool worker, so it uses `asyncio.run_coroutine_threadsafe(notify_user(…), loop)` to schedule Telegram messages on the event loop without blocking the download thread. Fires at most once per 10-percentage-point milestone. Errors in the callback are logged and never propagate to the download loop.

---

### `main.py`

**Role:** Thin entrypoint and graceful shutdown coordinator. Contains no business logic.

#### Startup sequence

1. Import `config` — validates secrets, creates `data/` and `downloads/` dirs
2. `_reset_in_progress()` — resets any items stuck in `'downloading'` or `'uploading'` to `'queued'` (handles previous crash recovery)
3. Create `TorrentManager` instance
4. Build and start Telegram bot (`initialize → start → updater.start_polling()`)
5. `asyncio.create_task(rss_worker())` — feed polling
6. `asyncio.create_task(run_queue_processor(tm, bot))` — download pipeline
7. Register `SIGINT`/`SIGTERM` handlers via `loop.add_signal_handler()`
8. Await `stop_event`

#### Shutdown sequence (on signal)

1. Cancel `rss_task` and `queue_task`
2. `asyncio.gather(..., return_exceptions=True)` — waits for cancellation to complete
3. `_reset_in_progress()` — rescues any mid-flight items
4. `tm.shutdown()` — pauses libtorrent session
5. `bot_app.updater.stop() → stop() → shutdown()` — stops Telegram in the correct order
6. Process exits cleanly

#### Logging

Two sinks configured at startup:
- **stderr** — `INFO` level, coloured, human-readable format
- **`data/omari.log`** — `DEBUG` level, rotating at 10 MB, retained for 14 days

---

## Data Flow Summary

```
User types:  /rent magnet:?xt=urn:btih:ABC123...
                │
                ▼
           bot.py: validate → add_download(identifier=magnet, chat_id=...) → DB

RSS poll:   rss_worker.py: fetch feed → group by (show_id, episode_id) →
            add_download(identifier=magnet, chat_id=0) → DB

Every 10s:  queue_processor.py: get_downloads_by_status('queued')
                │
                ├─ [under Semaphore]
                │
                ▼
            asyncio.to_thread → torrent.py: download_magnet()
                │
                │  progress_cb fires every 1%
                │  → run_coroutine_threadsafe → notify_user → Telegram: "⬇ 50%"
                │
                ▼ returns target_name
            dropbox_sync.py: _sync_blocking()
                │
                ├─ small file → files_upload()
                └─ large file → session_start → append_v2 × N → session_finish
                                                            │
                                              FileMetadata confirmed → os.remove()
                │
                ▼
            update_status('completed') → notify_user → Telegram: "✅ Done"
```

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `python-telegram-bot` | 20.7 | Telegram bot framework (async) |
| `tinydb` | 4.8.0 | Embedded JSON document database |
| `feedparser` | 6.0.11 | RSS/Atom feed parsing |
| `dropbox` | 11.36.2 | Dropbox API SDK |
| `libtorrent` | 2.0.9 | BitTorrent P2P engine |
| `python-dotenv` | latest | `.env` file loading |
| `aiofiles` | latest | Async file I/O |
| `loguru` | latest | Structured logging with rotation |

> **libtorrent install note:** pip install is unreliable on all platforms. On Linux: `sudo apt-get install python3-libtorrent`. On Windows: use pre-built wheels from the libtorrent GitHub releases page.

---

## Configuration Reference (`.env`)

```env
TELEGRAM_TOKEN=...
ALLOWED_USER_IDS=123456789,987654321

DROPBOX_REFRESH_TOKEN=...
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...

RSS_URLS=https://showrss.info/user/YOUR_ID.rss?magnets=true&namespaces=true&name=null&quality=anyhd&re=null
RSS_POLL_INTERVAL=900

MAX_CONCURRENT_DOWNLOADS=2
DOWNLOAD_PATH=./downloads
DOWNLOAD_TIMEOUT_MINUTES=30
```

Copy `.env.example` → `.env` and fill in your values before first run.
