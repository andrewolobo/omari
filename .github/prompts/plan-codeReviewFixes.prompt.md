# Plan: Implement Code Review Fixes (OMARI Bot)

## Overview

Implement all 7 issues from the code review (DEV/code-review.md) across `omar_bot/`. Two critical bugs and five suggestions. All changes are surgical — no new files needed.

---

## Phase 1: Critical Fixes

### Step 1 — Remove misleading DROPBOX_REFRESH_TOKEN warning (`config.py`)

- File: `omar_bot/config.py` lines ~96-101
- Delete the `if not DROPBOX_REFRESH_TOKEN:` block that logs a warning at import time.
- Keep the `DROPBOX_REFRESH_TOKEN: str = _optional(...)` assignment.
- The authoritative check already lives in `main.py` via `auth_store.is_linked()`.

### Step 2 — Auto-detect media_type after download resolves (`queue_processor.py`)

- File: `omar_bot/queue_processor.py`, inside `_process_item()`
- Add `from torrent import ... detect_media_type` to the import (torrent already imported via TorrentManager).
- After `target_name` is resolved (after the `asyncio.to_thread(tm.download_magnet, ...)` call), read `media_type = item.get("media_type", "unknown")`.
- If `media_type == "unknown"`, call `detect_media_type(target_name)` and include `media_type=media_type` in the `update_status(..., "uploading", ...)` call.
- Otherwise keep existing `update_status(identifier, "uploading", target_name=target_name, title=target_name)` as-is.

---

## Phase 2: Suggestions

### Step 3 — Add `/start` to `/help` output (`bot.py`)

- File: `omar_bot/bot.py`, `help_command()` function (~line 174)
- Prepend `/start — Link your Dropbox account\n` to the reply text.

### Step 4 — Fix silent task exception swallowing (`queue_processor.py`)

- File: `omar_bot/queue_processor.py`, `run_queue_processor()` (~line 108)
- Replace the inline lambda `task.add_done_callback(lambda t: t.exception() ...)` with a named function `_log_task_exception(t)` that logs via `logger.error()`.
- Define `_log_task_exception` as a module-level function before `run_queue_processor`.

### Step 5 — Guard against double `/start` flow overwrite (`bot.py`)

- File: `omar_bot/bot.py`, `start_command()` (~line 124, after the `is_linked()` check)
- After the `is_linked()` early return, add: if `user_id in _pending_auth_flows`, reply with message telling user to paste the code or run `/start` again, then return.

### Step 6 — Anchor auth_store paths to `__file__` (`auth_store.py`)

- File: `omar_bot/auth_store.py`, lines 22-23
- Replace relative `Path("data/dropbox_token.json")` with `Path(__file__).parent / "data" / "dropbox_token.json"`.
- Same for `_TMP_PATH`.

### Step 7 — Re-fetch item from DB in `_process_item` (`queue_processor.py`)

- File: `omar_bot/queue_processor.py`, start of `_process_item()`
- Import `get_download` (already in database.py, already imported via `from database import ...`).
- At the very start of `_process_item()` (before `async with sem:`), re-fetch the record: `fresh = get_download(identifier)`.
- If `fresh is None` or `fresh.get("status") != "queued"`, log and return early.
- Use `fresh` as the authoritative source for `media_type` and other fields (not the captured `item`).

---

## Relevant Files

- `omar_bot/config.py` — remove warning block (Step 1)
- `omar_bot/queue_processor.py` — Steps 2, 4, 7 (imports + \_process_item + callback)
- `omar_bot/bot.py` — Steps 3, 5 (help text + double-start guard)
- `omar_bot/auth_store.py` — Step 6 (path anchoring)

## Verification

1. Run `python -m py_compile omar_bot/config.py omar_bot/queue_processor.py omar_bot/bot.py omar_bot/auth_store.py` — should pass with no errors.
2. Manually review that `config.py` no longer logs the DROPBOX_REFRESH_TOKEN warning.
3. Confirm `_log_task_exception` is defined and wired to `task.add_done_callback`.
4. Confirm `auth_store.TOKEN_PATH` prints correctly from different working directories.

## Execution Order

Steps 1 and 2 are independent and can be done in parallel with Steps 3-7. Steps 4 and 7 both touch `queue_processor.py` — do them together in one pass. Steps 3 and 5 both touch `bot.py` — do them together in one pass.
