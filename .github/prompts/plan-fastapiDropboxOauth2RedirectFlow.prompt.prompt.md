## Plan: Add FastAPI Dropbox OAuth2 Redirect Flow

The current `/start` command uses `DropboxOAuth2FlowNoRedirect` — users must manually copy a code from Dropbox and paste it back into Telegram. Switching to a redirect URI flow requires a lightweight FastAPI web server alongside the bot, using `DropboxOAuth2Flow` (with redirect) from the Dropbox SDK.

---

### Phase 1 — Dependencies & Config

1. **`omar_bot/requirements.txt`** — add `fastapi` and `uvicorn[standard]`

2. **`omar_bot/config.py`** — add four new variables:
   - `DROPBOX_REDIRECT_URI` _(required)_ — the exact callback URL, e.g. `http://yourhost:8080/auth/dropbox/callback`. **Must also be registered in the Dropbox App Console** under _Redirect URIs_.
   - `OAUTH_BASE_URL` _(optional, default `http://localhost:8080`)_ — the base URL the bot sends to users in the Telegram link
   - `API_HOST` _(optional, default `0.0.0.0`)_
   - `API_PORT` _(optional int, default `8080`)_

---

### Phase 2 — New `omar_bot/api.py`

New FastAPI module with:

- **`_pending_flows: dict[str, dict]`** — maps CSRF state token → `{flow, session, user_id}`. In-memory; flows lost on restart (user re-runs `/start`).
- **`_bot_app`** — set by `main.py` at startup so the callback can send a Telegram notification.
- **`GET /auth/dropbox/start?telegram_user_id=<id>`**
  - Creates `DropboxOAuth2Flow` with `redirect_uri=DROPBOX_REDIRECT_URI`
  - Calls `flow.start()` → populates session dict with CSRF token; returns Dropbox auth URL
  - Stores `_pending_flows[session['csrf']] = {flow, session, user_id}`
  - Returns HTTP 302 redirect to Dropbox authorization URL
- **`GET /auth/dropbox/callback?code=&state=`**
  - Pops entry from `_pending_flows` by `state` → 400 if not found
  - Calls `flow.finish({'code': code, 'state': state}, session)` → 400 on CSRF mismatch
  - Calls `auth_store.save_refresh_token(result.refresh_token)`
  - If `_bot_app` is set, enqueues a Telegram notification to the user (`✅ Dropbox linked!`)
  - Returns an HTML success page instructing the user to return to Telegram
- Exposes a `run_server()` coroutine for `main.py` to run as a task

---

### Phase 3 — `omar_bot/bot.py` changes

- Remove `DropboxOAuth2FlowNoRedirect` import and `_pending_auth_flows` dict
- Remove `handle_auth_code` message handler and its registration in `get_bot_application()`
- Update `start_command` to send: `{config.OAUTH_BASE_URL}/auth/dropbox/start?telegram_user_id={user_id}`

---

### Phase 4 — `omar_bot/main.py` changes

- Import `api` module; after creating `bot_app`, set `api.bot_app = bot_app`
- Add 4th task: `api_task = asyncio.create_task(api.run_server(), name="api_server")`
- Include `api_task` in the shutdown cancellation logic

---

**Relevant files**

- `omar_bot/requirements.txt`
- `omar_bot/config.py`
- `omar_bot/api.py` _(new)_
- `omar_bot/bot.py` — `start_command`, `handle_auth_code`, `get_bot_application`
- `omar_bot/main.py` — `main()` coroutine

---

**Verification**

1. On startup, uvicorn logs `Uvicorn running on http://0.0.0.0:8080`
2. `/start` in Telegram → bot sends a clickable link to the `/auth/dropbox/start` endpoint
3. Clicking it in a browser → redirects to Dropbox consent page
4. Approving on Dropbox → browser lands on `/auth/dropbox/callback` → success HTML shown
5. `data/dropbox_token.json` is created with the refresh token
6. `/rent` commands upload to Dropbox successfully

---

**Decisions**

- The `DropboxOAuth2FlowNoRedirect` approach is removed entirely (not kept as fallback)
- Pending OAuth state is held in-memory only; lost on restart, but `/start` re-initiates
- No HTTPS is enforced — production deployment (reverse proxy / ngrok) is the user's responsibility
- **The Dropbox App Console redirect URI must be manually registered by the user** before this flow will work
