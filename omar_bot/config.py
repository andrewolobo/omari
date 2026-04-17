"""
config.py — Environment configuration loader.

Loads variables from a .env file at startup and exposes them as typed
module-level constants. Import this module everywhere instead of accessing
os.environ directly.

Usage:
    import config
    print(config.TELEGRAM_TOKEN)
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

# Resolve .env relative to this file so the app works regardless of cwd.
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require(key: str) -> str:
    """Return the value of a required env var; raise at startup if missing."""
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example to .env and fill in the value."
        )
    return val


def _optional(key: str, default: str) -> str:
    """Return the value of an optional env var, falling back to default."""
    return os.environ.get(key, default).strip() or default


def _int(key: str, default: int) -> int:
    """Return an integer env var, falling back to default."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            f"Config: '{key}' value '{raw}' is not a valid integer. "
            f"Using default: {default}"
        )
        return default


def _str_list(key: str, default: str = "") -> list[str]:
    """Return a comma-separated env var as a stripped, non-empty list."""
    raw = os.environ.get(key, default)
    return [v.strip() for v in raw.split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN: str = _require("TELEGRAM_TOKEN")

ALLOWED_USER_IDS: list[str] = _str_list("ALLOWED_USER_IDS")
if not ALLOWED_USER_IDS:
    logger.warning(
        "Config: ALLOWED_USER_IDS is empty. "
        "No Telegram users will be able to issue commands."
    )

# Optional: Telegram user ID that receives notifications for RSS-sourced downloads.
# Set to your own user ID so you are notified when an RSS download completes.
# Leave as 0 (default) to disable RSS completion notifications.
NOTIFICATION_CHAT_ID: int = _int("NOTIFICATION_CHAT_ID", default=0)

# ---------------------------------------------------------------------------
# Dropbox (refresh token auth — not a short-lived access token)
# ---------------------------------------------------------------------------

# APP_KEY and APP_SECRET are always required — they are needed both to initiate
# the OAuth2 flow (/start command) and to refresh access tokens at runtime.
DROPBOX_APP_KEY: str = _require("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET: str = _require("DROPBOX_APP_SECRET")

# REFRESH_TOKEN is optional at startup: it may be absent when the user has not
# yet run /start. auth_store.py is the authoritative source at runtime; this
# value serves as a static fallback for pre-linked deployments.
DROPBOX_REFRESH_TOKEN: str = _optional("DROPBOX_REFRESH_TOKEN", default="")

# REDIRECT_URI must match exactly what is registered in the Dropbox App Console
# under "Redirect URIs" (e.g. http://yourhost:8080/auth/dropbox/callback).
DROPBOX_REDIRECT_URI: str = _require("DROPBOX_REDIRECT_URI")

# ---------------------------------------------------------------------------
# API server (FastAPI / uvicorn — serves the OAuth2 callback endpoint)
# ---------------------------------------------------------------------------

# Base URL the bot advertises to users in Telegram, e.g. http://yourhost:8080.
# Must share the same host:port as DROPBOX_REDIRECT_URI.
OAUTH_BASE_URL: str = _optional("OAUTH_BASE_URL", default="http://localhost:8080")

# Interface and port uvicorn binds to.
API_HOST: str = _optional("API_HOST", default="0.0.0.0")
API_PORT: int = _int("API_PORT", default=8080)

# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

RSS_URLS: list[str] = _str_list("RSS_URLS")
if not RSS_URLS:
    logger.warning(
        "Config: RSS_URLS is empty. The RSS worker will have nothing to poll."
    )

RSS_POLL_INTERVAL: int = _int("RSS_POLL_INTERVAL", default=900)

# ---------------------------------------------------------------------------
# Download / Queue
# ---------------------------------------------------------------------------

MAX_CONCURRENT_DOWNLOADS: int = _int("MAX_CONCURRENT_DOWNLOADS", default=1)
DOWNLOAD_PATH: str = _optional("DOWNLOAD_PATH", default="./downloads")
DOWNLOAD_TIMEOUT_MINUTES: int = _int("DOWNLOAD_TIMEOUT_MINUTES", default=30)

# ---------------------------------------------------------------------------
# Dropbox destination directories (media-type routing)
# ---------------------------------------------------------------------------

SHOWS_DIRECTORY: str = _optional("SHOWS_DIRECTORY", default="Shows")
MOVIES_DIRECTORY: str = _optional("MOVIES_DIRECTORY", default="Movies")

# ---------------------------------------------------------------------------
# Ensure required directories exist at import time
# ---------------------------------------------------------------------------

Path(DOWNLOAD_PATH).mkdir(parents=True, exist_ok=True)
Path("data").mkdir(exist_ok=True)
