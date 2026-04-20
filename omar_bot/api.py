"""
api.py — FastAPI web server providing the Dropbox OAuth2 redirect-URI callback.

Endpoints:
    GET /auth/dropbox/start?telegram_user_id=<id>
        Initiates the OAuth2 redirect flow. Stores CSRF state and redirects
        the user's browser to the Dropbox consent page.

    GET /auth/dropbox/callback?code=<code>&state=<state>
        Dropbox redirects here after the user grants access. Validates the CSRF
        state token, exchanges the code for a refresh token, persists it via
        auth_store, and dispatches a Telegram notification to the originating user.

Design notes:
    • Pending flows are stored in _pending_flows, keyed by the CSRF state token
      written into the session dict by DropboxOAuth2Flow.start(). These are
      held in-memory; flows are lost on restart — users simply run /start again.
    • bot_app is a module-level reference set by main.py before the server
      starts so the callback can dispatch a Telegram message without a
      circular import.
    • run_server() wraps a uvicorn.Server in a coroutine so main.py can launch
      it as an asyncio.create_task() alongside the Telegram bot and RSS worker.
"""

import asyncio
import html
from typing import Optional

import uvicorn
from dropbox.oauth import (
    DropboxOAuth2Flow,
    BadRequestException,
    BadStateException,
    CsrfException,
    NotApprovedException,
)
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

import auth_store
import config

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Maps CSRF state token → {"flow": DropboxOAuth2Flow, "session": dict, "user_id": int}
_pending_flows: dict[str, dict] = {}

# Injected by main.py after bot_app is created, before tasks are launched.
# Used to dispatch Telegram notifications from the callback without a circular import.
bot_app = None

# Session key under which DropboxOAuth2Flow stores the CSRF token.
_CSRF_KEY = "dropbox_csrf_token"

# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/auth/dropbox/start")
async def auth_start(telegram_user_id: int) -> RedirectResponse:
    """
    Begin the Dropbox OAuth2 flow for a given Telegram user.

    Creates a DropboxOAuth2Flow, generates the Dropbox authorization URL
    (which also populates the session dict with the CSRF token), stores the
    flow under that CSRF token, then issues a 302 redirect to Dropbox.
    """
    session: dict = {}
    flow = DropboxOAuth2Flow(
        consumer_key=config.DROPBOX_APP_KEY,
        redirect_uri=config.DROPBOX_REDIRECT_URI,
        session=session,
        csrf_token_session_key=_CSRF_KEY,
        consumer_secret=config.DROPBOX_APP_SECRET,
        token_access_type="offline",
    )
    auth_url = flow.start()

    # After start(), session[_CSRF_KEY] holds the CSRF token, which Dropbox
    # will echo back as the `state` query parameter in the callback.
    csrf_token = session[_CSRF_KEY]
    _pending_flows[csrf_token] = {
        "flow": flow,
        "session": session,
        "user_id": telegram_user_id,
    }

    logger.info(
        f"OAuth2 flow started for Telegram user {telegram_user_id} "
        f"(CSRF: {csrf_token[:8]}…)"
    )
    return RedirectResponse(url=auth_url, status_code=302)


@app.get("/auth/dropbox/callback")
async def auth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
) -> HTMLResponse:
    """
    Handle the Dropbox OAuth2 callback.

    Validates the CSRF state token, exchanges the authorization code for a
    refresh token, persists it via auth_store, sends a Telegram notification,
    and returns an HTML result page.
    """
    # Surface Dropbox-level errors (e.g. user clicked "Cancel").
    if error is not None:
        description = error_description or error
        logger.warning(f"OAuth callback received Dropbox error: {error!r} — {description!r}")
        return HTMLResponse(
            content=_error_page(f"Dropbox returned an error: {description}"),
            status_code=400,
        )

    if not code or not state:
        logger.warning("OAuth callback received with missing code or state.")
        return HTMLResponse(
            content=_error_page("Missing authorization code or state parameter."),
            status_code=400,
        )

    # The state param is {csrf_token} or {csrf_token}|{url_state}; the CSRF
    # portion is always everything before the first "|".
    csrf_token = state.split("|", 1)[0]
    entry = _pending_flows.pop(csrf_token, None)
    if entry is None:
        logger.warning(
            f"OAuth callback received unknown or already-used CSRF token: {csrf_token[:8]}…"
        )
        return HTMLResponse(
            content=_error_page(
                "Authorization session not found or already used. "
                "Please run /start in Telegram again."
            ),
            status_code=400,
        )

    flow: DropboxOAuth2Flow = entry["flow"]
    user_id: int = entry["user_id"]

    try:
        result = flow.finish({"code": code, "state": state})
    except (BadRequestException, BadStateException, CsrfException) as exc:
        logger.warning(f"OAuth CSRF/state validation failed for user {user_id}: {exc}")
        return HTMLResponse(
            content=_error_page(
                "Authorization validation failed. Please run /start in Telegram and try again."
            ),
            status_code=400,
        )
    except NotApprovedException:
        logger.info(f"User {user_id} declined Dropbox authorization.")
        return HTMLResponse(
            content=_error_page(
                "You declined the authorization request. "
                "Run /start in Telegram if you change your mind."
            ),
            status_code=400,
        )
    except Exception as exc:
        logger.error(f"Unexpected error during OAuth code exchange for user {user_id}: {exc}")
        return HTMLResponse(
            content=_error_page(
                "An unexpected error occurred. Please run /start in Telegram and try again."
            ),
            status_code=500,
        )

    auth_store.save_refresh_token(result.refresh_token)
    logger.info(f"Dropbox account linked for Telegram user {user_id}.")

    # Schedule a Telegram notification on the running event loop.
    if bot_app is not None:
        try:
            task = asyncio.create_task(
                bot_app.bot.send_message(
                    chat_id=user_id,
                    text="Dropbox connected successfully! You can now use /rent to queue downloads.",
                )
            )
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
        except Exception as exc:
            logger.warning(
                f"Could not schedule Telegram notification for user {user_id}: {exc}"
            )

    return HTMLResponse(content=_success_page(), status_code=200)


# ---------------------------------------------------------------------------
# HTML response helpers
# ---------------------------------------------------------------------------

def _success_page() -> str:
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        "<title>Dropbox Linked</title>"
        "<style>"
        "body{font-family:sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:100vh;margin:0;background:#f0f4f8}"
        ".card{background:#fff;border-radius:12px;padding:2rem 3rem;"
        "box-shadow:0 2px 16px rgba(0,0,0,.1);text-align:center}"
        "h1{color:#00b386}p{color:#555}"
        "</style>"
        "</head>"
        "<body><div class='card'>"
        "<h1>&#x2705; Dropbox Linked!</h1>"
        "<p>Your Dropbox account has been connected successfully.</p>"
        "<p>You can close this window and return to Telegram.</p>"
        "</div></body></html>"
    )


def _error_page(message: str) -> str:
    safe_message = html.escape(message)
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        "<title>Authorization Error</title>"
        "<style>"
        "body{font-family:sans-serif;display:flex;align-items:center;"
        "justify-content:center;height:100vh;margin:0;background:#f0f4f8}"
        ".card{background:#fff;border-radius:12px;padding:2rem 3rem;"
        "box-shadow:0 2px 16px rgba(0,0,0,.1);text-align:center}"
        "h1{color:#e05252}p{color:#555}"
        "</style>"
        "</head>"
        "<body><div class='card'>"
        "<h1>Authorization Error</h1>"
        f"<p>{safe_message}</p>"
        "</div></body></html>"
    )


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

async def run_server() -> None:
    """
    Run the FastAPI app under uvicorn as an asyncio coroutine.

    Intended to be launched via asyncio.create_task() in main.py alongside
    the Telegram bot and RSS worker tasks. uvicorn access logs are suppressed;
    application-level events are logged through loguru.
    """
    uvicorn_config = uvicorn.Config(
        app=app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)
    logger.info(f"API server starting on {config.API_HOST}:{config.API_PORT}")
    await server.serve()
