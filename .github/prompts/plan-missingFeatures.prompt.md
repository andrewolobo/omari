# Plan: Implement Missing Features (Stories 1, 4, 6)

## TL;DR

Three gaps remain vs the user stories: Dropbox OAuth flow (Story 1 — largest), sequential queue default (Story 4 — trivial), and RSS completion notifications (Story 6 — small). Strategy: Story 1 uses `DropboxOAuth2FlowNoRedirect` from the Dropbox SDK (no redirect server needed — user pastes code back to bot). Story 4 is a one-line default change. Story 6 adds an optional `NOTIFICATION_CHAT_ID` env var.

---

## Phase A — Sequential Queue Default (Story 4) · trivial

**Step 1.** `config.py` — change `_int("MAX_CONCURRENT_DOWNLOADS", default=2)` → `default=1`.

---

## Phase B — RSS Completion Notifications (Story 6) · small

**Step 2.** `config.py` — add `NOTIFICATION_CHAT_ID: int = _int("NOTIFICATION_CHAT_ID", default=0)` in the Telegram section. When 0 (unset), `notify_user()`'s existing no-op preserves backward compatibility.

**Step 3.** `rss_worker.py` — in `_process_feed()`, replace `chat_id=0` with `chat_id=config.NOTIFICATION_CHAT_ID`.

**Step 4.** `.env.example` — document `NOTIFICATION_CHAT_ID` (your Telegram user ID; optional; used for RSS notifications).

---

## Phase C — Dropbox OAuth Flow (Story 1) · main effort

### Design

Use `DropboxOAuth2FlowNoRedirect` from `dropbox.oauth`. This generates an authorization URL → user visits it, Dropbox shows them a code → user pastes the code back to the bot → SDK's `finish(code)` exchanges it for a refresh token. **No redirect server needed.** Already in the installed Dropbox SDK — zero new dependencies.

### Step C1 · New file `auth_store.py`

- `TOKEN_PATH = Path("data/dropbox_token.json")`
- `save_refresh_token(token: str) -> None` — atomic write (tmp → rename) to avoid corrupt reads
- `load_refresh_token() -> str | None` — returns `None` if file absent or malformed
- `is_linked() -> bool` — `bool(load_refresh_token() or config.DROPBOX_REFRESH_TOKEN)`

### Step C2 · `config.py` _(depends on C1)_

- `DROPBOX_REFRESH_TOKEN` → `_optional("DROPBOX_REFRESH_TOKEN", default="")` — no longer a startup crash if empty; `DROPBOX_APP_KEY` / `DROPBOX_APP_SECRET` remain `_require()`
- Add a log warning if `DROPBOX_REFRESH_TOKEN` is empty at startup (prompt user to `/start`)

### Step C3 · `dropbox_sync.py` _(depends on C1)_

- `_make_client()`: try `auth_store.load_refresh_token()` first; fall back to `config.DROPBOX_REFRESH_TOKEN`; raise `RuntimeError("Dropbox not linked — send /start to the bot.")` if neither is set

### Step C4 · `bot.py` _(depends on C1, parallel with C3)_

- Module-level `_pending_auth_flows: dict[int, DropboxOAuth2FlowNoRedirect] = {}`
- New `start_command`:
  - Auth check
  - If `auth_store.is_linked()` → reply "Dropbox already linked. Use /help to see commands."
  - Else → create `DropboxOAuth2FlowNoRedirect(config.DROPBOX_APP_KEY, config.DROPBOX_APP_SECRET, token_access_type='offline')`, call `.start()` to get URL, store in `_pending_auth_flows[user_id]`, reply with URL + paste instructions
- New `handle_auth_code` `MessageHandler` (`filters.TEXT & ~filters.COMMAND`):
  - Auth check
  - If no pending flow for `user_id` → silently return (ignore normal text messages)
  - Call `flow.finish(text.strip())`
  - On success → `auth_store.save_refresh_token(result.refresh_token)`, del from dict, reply "✅ Dropbox linked successfully!"
  - On exception → reply "❌ Invalid code. Try /start again.", del from dict
- Register both in `get_bot_application()`:
  - `CommandHandler("start", start_command)`
  - `MessageHandler(filters.TEXT & ~filters.COMMAND, handle_auth_code)`

### Step C5 · `main.py` _(depends on C1)_

- After `_reset_in_progress()`, import and check `auth_store.is_linked()`; if `False`, log a warning telling the user to send `/start` to the bot before using it

---

## Relevant Files

| File                         | Steps      |
| ---------------------------- | ---------- |
| `omar_bot/config.py`         | A1, B2, C2 |
| `omar_bot/rss_worker.py`     | B3         |
| `omar_bot/.env.example`      | B4         |
| NEW `omar_bot/auth_store.py` | C1         |
| `omar_bot/dropbox_sync.py`   | C3         |
| `omar_bot/bot.py`            | C4         |
| `omar_bot/main.py`           | C5         |

## Execution Order

Phases A & B (Steps 1–4) are fully independent of Phase C — implement together.

Within Phase C: C1 → C2, then C3 + C4 + C5 in parallel.

---

## Verification

1. Empty `DROPBOX_REFRESH_TOKEN` in `.env` → app starts without crash, logs "Dropbox not linked" warning
2. `/start` in Telegram → bot returns a Dropbox authorization URL
3. Authorize at URL, paste code back → bot replies "✅ Dropbox linked", `data/dropbox_token.json` exists
4. Bot restart → `is_linked()` returns `True`; no warning; pipeline runs normally
5. `/rent <magnet>` → file appears in correct Dropbox directory
6. Set `NOTIFICATION_CHAT_ID=<your id>` → RSS-completed download sends Telegram notification
7. Two `/rent` commands back-to-back → second starts only after first completes (default `MAX_CONCURRENT_DOWNLOADS=1`)

---

## Decisions

- Auth code state (`_pending_auth_flows`) is **in-memory only**. If the bot restarts mid-auth, the user runs `/start` again — acceptable for a personal bot.
- `.env`-supplied `DROPBOX_REFRESH_TOKEN` remains fully supported as a bypass (backward compatible for existing deployments).
- `DROPBOX_APP_KEY` / `DROPBOX_APP_SECRET` stay as `_require()` — needed for OAuth initiation and token refresh.
- Story 4: changing the default to 1 satisfies "strictly one at a time"; `MAX_CONCURRENT_DOWNLOADS` env override remains available for power users.
