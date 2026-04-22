"""
auth_store.py — Persistent Dropbox refresh-token store.

Provides atomic read/write of the Dropbox OAuth2 refresh token to a local
JSON file so the running process can persist tokens obtained via the
/start command without requiring a restart or manual .env edits.

Public API:
    save_refresh_token(token)  — atomically write the token to disk
    load_refresh_token()       — read the token; returns None if absent
    is_linked()                — True if a valid token is available
"""

import json
import os
from pathlib import Path

from . import config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE      = Path(__file__).parent / "data"
TOKEN_PATH = _BASE / "dropbox_token.json"
_TMP_PATH  = _BASE / "dropbox_token.json.tmp"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_refresh_token(token: str) -> None:
    """
    Atomically write a Dropbox refresh token to disk.

    Writes to a temporary file first, then renames it into place so a crash
    mid-write cannot leave a corrupt token file.

    Args:
        token: The OAuth2 refresh token string returned by the Dropbox SDK.
    """
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TMP_PATH.write_text(json.dumps({"refresh_token": token}), encoding="utf-8")
    os.replace(_TMP_PATH, TOKEN_PATH)


def load_refresh_token() -> str | None:
    """
    Load the persisted Dropbox refresh token from disk.

    Returns:
        The token string if the file exists and is valid JSON; None otherwise.
    """
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        token = data.get("refresh_token", "").strip()
        return token if token else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def is_linked() -> bool:
    """
    Return True if a Dropbox refresh token is available from either the
    persisted token file or the static DROPBOX_REFRESH_TOKEN env var.

    Checks the token file first so a token obtained via /start takes
    precedence over a stale or empty value in config.
    """
    return bool(load_refresh_token() or config.DROPBOX_REFRESH_TOKEN)
