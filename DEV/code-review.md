# OMARI Bot — Code Review

**Date:** April 17, 2026
**Scope:** Full codebase (`omar_bot/`) as of Phase C implementation completion

---

## Summary

The codebase is well-structured with clear separation of concerns, consistent use of async patterns, and solid defensive coding throughout. Seven issues were identified: two critical (one correctness bug, one misleading log), and five suggestions (UX, safety, and maintainability).

| Severity         | Count |
| ---------------- | ----- |
| 🔴 Critical      | 2     |
| 🟡 Suggestion    | 5     |
| ✅ Good Practice | 10    |

---

## 🔴 Critical Issues

### 1. Misleading startup warning after Dropbox is successfully linked

**File:** `config.py`

`config.py` fires its `DROPBOX_REFRESH_TOKEN is not set` warning at **import time**, before `auth_store` is consulted. Once a user successfully links Dropbox via `/start` (token saved to `data/dropbox_token.json`), every subsequent restart still logs:

```
Config: DROPBOX_REFRESH_TOKEN is not set. Send /start to the Telegram bot to link your Dropbox account.
```

The account is correctly linked, but the log suggests otherwise — confusing for operators.

**Fix:** Remove the warning block from `config.py`. The accurate, authoritative check already exists in `main.py` via `auth_store.is_linked()`.

```python
# config.py — keep only the assignment, drop the warning block
DROPBOX_REFRESH_TOKEN: str = _optional("DROPBOX_REFRESH_TOKEN", default="")

# main.py already has the correct check (no change needed):
if not auth_store.is_linked():
    logger.warning("Dropbox is not linked. Send /start to link your account.")
```

---

### 2. Media type never auto-detected for manual magnet downloads

**File:** `queue_processor.py` + `bot.py`

When a user runs `/rent <magnet>` without specifying `tv` or `movie`, `media_type="unknown"` is stored. After the download resolves, the actual torrent name (`target_name`) is available — but `detect_media_type()` is never called on it. The result: all manually queued downloads with no type hint upload to the Dropbox root with a warning log, silently violating User Story 3 ("automatically parse the requested download to determine the media type").

`detect_media_type()` already exists in `torrent.py` and is correct. It just isn't called at the right moment.

**Fix:** In `_process_item`, after `target_name` is resolved, auto-detect when the type is still `"unknown"`:

```python
media_type = item.get("media_type", "unknown")
if media_type == "unknown":
    media_type = detect_media_type(target_name)
    update_status(
        identifier, "uploading",
        target_name=target_name,
        title=target_name,
        media_type=media_type,
    )
else:
    update_status(identifier, "uploading", target_name=target_name, title=target_name)
```

---

## 🟡 Suggestions

### 3. `/help` doesn't mention `/start`

**File:** `bot.py` — `help_command`

The help text lists five commands but omits `/start`. A new user following the bot has no in-app way to discover the linking command.

**Fix:** Add `/start` to the help response:

```python
"/start — Link your Dropbox account\n"
```

---

### 4. Task exception callback silently swallows errors

**File:** `queue_processor.py` — `run_queue_processor()`

```python
task.add_done_callback(
    lambda t: t.exception()  # Surface exceptions to the logger.
    if not t.cancelled() and t.exception()
    else None
)
```

`t.exception()` retrieves the exception object but nothing is done with it — it is never logged or handled. The comment says "Surface exceptions to the logger" but the code does not do that.

**Fix:**

```python
def _log_task_exception(t: asyncio.Task) -> None:
    if not t.cancelled() and (exc := t.exception()):
        logger.error(f"Unhandled task exception in {t.get_name()}: {exc}")

task.add_done_callback(_log_task_exception)
```

---

### 5. Running `/start` twice silently discards the first flow

**File:** `bot.py` — `start_command()`

If an authorized user runs `/start` twice before completing the auth flow, the first `DropboxOAuth2FlowNoRedirect` instance is silently replaced in `_pending_auth_flows`. Any code generated from the first Dropbox page then fails with a cryptic "Invalid authorization code" error.

**Fix:** Add a guard before creating a new flow:

```python
if user_id in _pending_auth_flows:
    await update.message.reply_text(
        "Authorization already in progress. Paste the code from the Dropbox page, "
        "or run /start again to generate a fresh link."
    )
    return
```

---

### 6. Relative paths in `auth_store.py` are cwd-dependent

**File:** `auth_store.py`

```python
TOKEN_PATH = Path("data/dropbox_token.json")
_TMP_PATH  = Path("data/dropbox_token.json.tmp")
```

These are relative to the process working directory, not the script file. `config.py` correctly anchors its `.env` path to `Path(__file__).parent`. If `main.py` is started from a different directory (e.g. a systemd service with `WorkingDirectory` set elsewhere), the token is written in the wrong location and the app appears unlinked on every startup.

**Fix:**

```python
_BASE      = Path(__file__).parent / "data"
TOKEN_PATH = _BASE / "dropbox_token.json"
_TMP_PATH  = _BASE / "dropbox_token.json.tmp"
```

---

### 7. Queue loop works from a stale item snapshot

**File:** `queue_processor.py` — `run_queue_processor()`

`item` is captured at poll time and passed into the task closure. If the DB record is modified between the poll and the task executing (e.g. the user cancels the item, or a parallel task touches the record at higher concurrency), the task operates on stale data.

With the current default of `MAX_CONCURRENT_DOWNLOADS=1` this window is negligible. However, if the default is raised in future it becomes a real race condition.

**Fix:** Re-fetch the record from the DB inside `_process_item` using the identifier before starting stage 1 work, and bail out early if the status is no longer `"queued"`.

---

## ✅ Good Practices

1. **Atomic token write** — `save_refresh_token()` writes to a `.tmp` file then calls `os.replace()`. A crash mid-write cannot corrupt the stored token.
2. **`threading.Lock` on `add_download` and `increment_retry`** — the only two operations that do a read-then-write on the DB. Correctly prevents race conditions between the RSS worker threads and the queue poller.
3. **`_reset_in_progress()` on both startup and shutdown** — ensures no items are permanently stranded in `downloading` or `uploading` after a crash or ungraceful kill.
4. **`asyncio.to_thread()` used consistently** for all blocking I/O — libtorrent, Dropbox API calls, and feedparser HTTP requests all run in the thread pool, keeping the event loop free for Telegram.
5. **Safe-delete in `dropbox_sync`** — `os.remove()` is only called after the Dropbox SDK returns a `FileMetadata` object. A failed upload never destroys the local file.
6. **Per-feed isolation in `rss_worker`** — each feed is polled inside its own `try/except` inside `asyncio.to_thread`. One unreachable feed does not block or fail others.
7. **`InvalidMagnetError` is never retried** — correct. A structurally invalid magnet will always fail and retrying wastes resources.
8. **`CancelledError` is re-raised** after resetting the item to `queued` — satisfies the asyncio contract and ensures graceful shutdown propagates correctly.
9. **libtorrent handle removed in `finally`** — prevents unintended seeding after the file has been uploaded to Dropbox.
10. **Token resolution order in `_make_client()`** — auth_store takes precedence over the `.env` static fallback, with a clear `RuntimeError` if neither is set. The precedence and reasoning are well-documented.

---

## Files Reviewed

| File                 | Role                                     |
| -------------------- | ---------------------------------------- |
| `auth_store.py`      | Dropbox refresh token persistence        |
| `bot.py`             | Telegram command handlers + OAuth flow   |
| `config.py`          | Environment variable loader              |
| `database.py`        | TinyDB CRUD layer                        |
| `dropbox_sync.py`    | Chunked Dropbox upload                   |
| `main.py`            | Entrypoint + graceful shutdown           |
| `queue_processor.py` | Async download/upload pipeline           |
| `rss_worker.py`      | RSS feed poller                          |
| `torrent.py`         | libtorrent 2.x engine + quality selector |
