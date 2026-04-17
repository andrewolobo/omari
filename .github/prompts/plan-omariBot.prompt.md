# Plan: OMARI Bot — Revised Implementation

**TL;DR:** Same five-module service, but restructured into 8 tighter phases. Critical bugs fixed before any code is written: libtorrent 2.x API, asyncio event loop blocking, expiring Dropbox tokens, missing config system, unsafe file deletion, and missing user feedback loop.

---

## Phase 0 — Project Setup

**New dependencies vs phases.md:**

- `python-dotenv` — secrets from `.env`, not hardcoded
- `aiofiles` — non-blocking file reads during upload
- `loguru` — replaces all `print()` calls

**`requirements.txt`**

```text
python-telegram-bot==20.7
tinydb==4.8.0
feedparser==6.0.11
dropbox==11.36.2
libtorrent==2.0.9
python-dotenv
aiofiles
loguru
```

> **libtorrent install note:** pip install is unreliable. On Linux use `apt-get install python3-libtorrent`. On Windows use pre-built wheels.

**New files in structure:**

```text
omar_bot/
├── .env                   # secrets (gitignored)
├── .env.example           # committed template
├── config.py              # loads .env, exposes typed constants
├── main.py                # thin entrypoint + graceful shutdown
├── database.py            # TinyDB schema + thread-safe access
├── bot.py                 # Telegram handlers + notification helpers
├── torrent.py             # libtorrent 2.x engine + quality selector
├── dropbox_sync.py        # chunked upload with retry + refresh token
├── rss_worker.py          # background poller (extracted from main.py)
├── queue_processor.py     # async queue with semaphore concurrency cap
├── data/
│   └── db.json
└── downloads/             # temp download dir (auto-created)
```

---

## Phase 1 — `config.py` (new)

Loads `.env` via `python-dotenv`. Exposes typed constants:

- `TELEGRAM_TOKEN` — bot token from BotFather
- `ALLOWED_USER_IDS` — list of authorized Telegram user ID strings
- `DROPBOX_REFRESH_TOKEN` — long-lived OAuth2 refresh token
- `DROPBOX_APP_KEY` — Dropbox app key
- `DROPBOX_APP_SECRET` — Dropbox app secret
- `RSS_URLS` — list of feed URLs (not a single string)
- `RSS_POLL_INTERVAL` — seconds between feed polls (e.g. 900)
- `MAX_CONCURRENT_DOWNLOADS` — semaphore cap (e.g. 2)
- `DOWNLOAD_PATH` — local download directory (default `./downloads`)
- `DOWNLOAD_TIMEOUT_MINUTES` — stale download timeout (e.g. 30)

---

## Phase 2 — `database.py`

**Fixes vs phases.md:**

- Add `updated_at` field — updated on every status change
- Add `retry_count` field — default 0; used by queue retry logic
- Add `chat_id` field — stored on insert; used to send completion notifications
- Add `target_name` field — populated after download; referenced by uploader
- Wrap `search + insert` in a `threading.Lock` — eliminates race condition between concurrent RSS ticks
- Remove `is_downloaded()` — redundant; callers use `add_download()` return value instead
- `update_status()` accepts `**kwargs` to update extra fields (e.g. `target_name`) atomically in one call
- New: `increment_retry(identifier)` → returns new retry count

**Functions:**

```python
add_download(identifier, source_type, title, chat_id) -> bool
update_status(identifier, new_status, **extra_fields)
get_downloads_by_status(status) -> list
increment_retry(identifier) -> int
```

---

## Phase 3 — `bot.py`

**Fixes vs phases.md:**

- Magnet validation: full regex instead of `startswith`:
  ```python
  re.match(r'magnet:\?xt=urn:btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})', uri)
  ```
- Store `chat_id` in DB record when `/rent` is called — enables completion notifications
- New commands: `/status`, `/list`, `/cancel <id>`
- New: `notify_user(bot, chat_id, message)` async helper used by queue and RSS worker

**Commands:**
| Command | Behaviour |
|---|---|
| `/rent <magnet_uri>` | Validate, add to DB with `chat_id`, confirm queue |
| `/status` | List all active (queued/downloading/uploading) items |
| `/list` | Show last 10 completed/failed items |
| `/cancel <identifier>` | Mark item cancelled, remove from active queue |

---

## Phase 4 — `torrent.py` (Most Changes)

**API fixes vs phases.md (libtorrent 2.x):**

| Old (broken)                    | New (correct)                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------- |
| `lt.add_magnet_uri()`           | `lt.parse_magnet_uri()` + `session.add_torrent()`                               |
| `session.listen_on()`           | settings dict `{'listen_interfaces': '0.0.0.0:6881'}`                           |
| `lt.storage_mode_t(2)`          | `lt.storage_mode_t.storage_mode_sparse`                                         |
| `time.sleep()` in async context | called via `asyncio.to_thread()` from queue_processor                           |
| No timeout                      | raises `DownloadTimeoutError` after `DOWNLOAD_TIMEOUT_MINUTES` with no progress |
| Keeps seeding after complete    | `session.remove_torrent(handle)` called before return                           |

**New: progress callback**

- `download_magnet(magnet_uri, progress_cb=None)` accepts an optional `progress_cb(pct: float)` callable
- queue_processor passes a function that sends Telegram updates every 10% increment

**Quality selector — fixed grouping:**

- Old: `get_best_quality_torrent(entries)` — returned one entry for the entire feed
- New: `get_best_quality_per_show(entries)` → `dict[normalized_show_name → best_entry]`
- Normalization strips resolution tags, year, release group suffixes with regex
- RSS worker iterates the full dict to process all shows in one feed poll

---

## Phase 5 — `dropbox_sync.py`

**Fixes vs phases.md:**

- **Refresh token auth:** replace static access token with:
  ```python
  dropbox.Dropbox(
      oauth2_refresh_token=config.DROPBOX_REFRESH_TOKEN,
      app_key=config.DROPBOX_APP_KEY,
      app_secret=config.DROPBOX_APP_SECRET
  )
  ```
- **Safe delete:** `os.remove()` called only after `files_upload_session_finish()` returns `FileMetadata` — never on exception; local file preserved on failure
- **Overwrite mode:** `mode=dropbox.files.WriteMode.overwrite` on all upload calls — prevents `WriteConflictError` on re-upload
- **RateLimitError retry:** catch `dropbox.exceptions.RateLimitError`, sleep `error.error.retry_after` seconds, retry up to 5 times before raising
- **Async wrapper:** `async def sync_to_dropbox(local_target_name)` wraps blocking I/O in `asyncio.to_thread()`
- **No global singleton:** `Dropbox` client instantiated per sync call or injected — not a module-level global

---

## Phase 6 — `rss_worker.py` (extracted from `main.py`)

- Polls `config.RSS_URLS` (list) in parallel via `asyncio.gather`
- Per feed: calls `get_best_quality_per_show(feed.entries)`, iterates all shows, calls `add_download()` per new entry
- Each feed wrapped in isolated try/except — one broken feed does not stop others
- Uses `loguru` for per-feed error logging

---

## Phase 7 — `queue_processor.py` (extracted from `main.py`)

- `asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)` caps parallelism
- Per-item pipeline under semaphore:
  1. `update_status(identifier, 'downloading')`
  2. `await asyncio.to_thread(tm.download_magnet, identifier, progress_cb)` — progress_cb sends Telegram updates every 10%
  3. `update_status(identifier, 'uploading', target_name=name)`
  4. `await sync_to_dropbox(name)`
  5. `update_status(identifier, 'completed')`
  6. `await notify_user(bot, chat_id, "✓ Complete: {title}")`

- **Retry logic:** on exception, `increment_retry(identifier)`
  - count < 3 → reset to `queued` with exponential backoff (30s / 120s / 300s)
  - count ≥ 3 → mark `failed`, call `notify_user(bot, chat_id, "✗ Failed: {title}")`

---

## Phase 8 — `main.py` (simplified)

- Registers `SIGINT`/`SIGTERM` handlers for graceful shutdown
- On signal: cancel background tasks, stop bot updater, flush in-progress items to `failed` so they retry on next run
- Thin entrypoint — wires and starts modules only:
  ```python
  asyncio.run(main())
  ```
- No business logic lives here

---

## Verification Checklist

1. Unit test `get_best_quality_per_show()` — mock feed with multiple shows and resolutions; assert correct grouping and selection
2. Unit test `add_download()` concurrency — two threads simultaneously; assert only one DB insert
3. Bot auth test — `/rent` from unauthorized ID; expect rejection message
4. Dropbox chunked upload test — local file >100MB; assert `FileMetadata` returned, local file deleted
5. Manual end-to-end: `/rent <valid_magnet>` → Telegram progress updates → Dropbox upload → local file deleted
6. Restart recovery: kill process mid-download, restart; assert item retries from `queued` state

---

## Priority Summary

| Priority | Issue                                                                                 |
| -------- | ------------------------------------------------------------------------------------- |
| Critical | `download_magnet` blocks event loop — wrap in `asyncio.to_thread`                     |
| Critical | Deprecated libtorrent 2.x API calls — full rewrite                                    |
| Critical | Dropbox static token expires — use refresh token flow                                 |
| High     | `config.py` is completely missing — define Phase 1                                    |
| High     | File deletion before confirmed upload — delete only on `FileMetadata`                 |
| High     | No Telegram completion/failure notifications — `notify_user` helper + `chat_id` in DB |
| High     | Quality selector doesn't group by title — `get_best_quality_per_show()`               |
| Medium   | No `/status`, `/list`, `/cancel` bot commands                                         |
| Medium   | No concurrency limit on simultaneous downloads — `asyncio.Semaphore`                  |
| Medium   | No retry logic for Dropbox rate limits — `RateLimitError` handler                     |
| Low      | Replace `print()` with `loguru` logging throughout                                    |
| Low      | Strengthen magnet URI regex validation                                                |
