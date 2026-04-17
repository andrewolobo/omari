"""
bot.py — Telegram command handlers and notification helpers.

Commands:
    /start                         — Link Dropbox account via OAuth2 (or confirm already linked).
    /rent <magnet_uri> [tv|movie]  — Queue a magnet link for download.
    /status                        — List all active (queued/downloading/uploading) items.
    /list                          — Show the 10 most recent completed or failed items.
    /cancel <identifier_prefix>    — Cancel a queued item (first 8 chars of its identifier).
    /help                          — Show available commands.

Only Telegram user IDs listed in config.ALLOWED_USER_IDS may issue commands.
"""

import re
from typing import Optional

from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from loguru import logger
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

import auth_store
import config
from database import (
    MEDIA_TYPES,
    add_download,
    get_download,
    get_downloads_by_status,
    get_recent,
    update_status,
)

# ---------------------------------------------------------------------------
# Magnet URI validation
# ---------------------------------------------------------------------------

# Accepts both 40-char hex (SHA-1) and 32-char base32 (v2) info-hashes.
_MAGNET_RE = re.compile(
    r"^magnet:\?xt=urn:btih:([0-9a-fA-F]{40}|[A-Z2-7]{32})",
    re.IGNORECASE,
)

# In-progress Dropbox OAuth2 flows, keyed by Telegram user_id.
# Populated by /start; consumed when the user pastes back the auth code.
_pending_auth_flows: dict[int, DropboxOAuth2FlowNoRedirect] = {}

# Statuses considered "active" for the /status command.
_ACTIVE_STATUSES = ("queued", "downloading", "uploading")

# Status emoji map for display.
_STATUS_EMOJI = {
    "queued":     "",
    "downloading": "⬇",
    "uploading":  "",
    "completed":  "",
    "failed":     "",
    "cancelled":  "",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def notify_user(bot: Bot, chat_id: int, message: str) -> None:
    """
    Send a plain-text message to a specific Telegram chat.

    Used by queue_processor and rss_worker to push completion/failure
    notifications back to the user who originally queued the download.
    No-ops silently if chat_id is 0 (RSS-sourced items with no originating chat).
    """
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=message)
    except Exception as exc:
        logger.warning(f"notify_user: failed to send message to {chat_id}: {exc}")


def _auth_check(update: Update) -> bool:
    """Return True if the sender is in the authorised user list."""
    return str(update.effective_user.id) in config.ALLOWED_USER_IDS


def _short_id(identifier: str, length: int = 8) -> str:
    """Return a short display prefix of an identifier for user-facing output."""
    return identifier[:length]


def _format_record(record: dict) -> str:
    """Format a single DB record as a single line for list/status output."""
    emoji = _STATUS_EMOJI.get(record.get("status", ""), "•")
    title = record.get("title", "Unknown")
    media = record.get("media_type", "unknown").upper()
    short = _short_id(record.get("identifier", ""))
    status = record.get("status", "?")
    return f"{emoji} [{media}] {title} — {status} (id: {short}…)"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /start — Initiate Dropbox OAuth2 linking.

    If the account is already linked, confirms this and directs the user to
    /help. Otherwise, generates a Dropbox authorization URL and instructs the
    user to visit it and paste the resulting code back into the chat.
    """
    if not _auth_check(update):
        await update.message.reply_text("Unauthorized.")
        return

    if auth_store.is_linked():
        await update.message.reply_text(
            "✅ Dropbox is already linked. Use /help to see available commands."
        )
        return

    user_id = update.effective_user.id

    if user_id in _pending_auth_flows:
        await update.message.reply_text(
            "Authorization already in progress. "
            "Paste the code from the Dropbox page, "
            "or run /start again to generate a fresh link."
        )
        return

    flow = DropboxOAuth2FlowNoRedirect(
        config.DROPBOX_APP_KEY,
        config.DROPBOX_APP_SECRET,
        token_access_type="offline",
    )
    auth_url = flow.start()
    _pending_auth_flows[user_id] = flow

    await update.message.reply_text(
        "To link your Dropbox account:\n"
        f"1. Open this URL: {auth_url}\n"
        "2. Click \"Allow\"\n"
        "3. Copy the authorization code shown and paste it here."
    )
    logger.info(f"OAuth flow started for user {user_id}.")


async def handle_auth_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    MessageHandler for plain text (non-command) messages.

    If the sender has a pending OAuth flow, treats the message as the
    authorization code returned by Dropbox and completes the flow.
    All other text messages are silently ignored.
    """
    if not _auth_check(update):
        return

    user_id = update.effective_user.id
    flow = _pending_auth_flows.get(user_id)
    if flow is None:
        return  # No pending auth — ignore the message.

    code = (update.message.text or "").strip()
    try:
        result = flow.finish(code)
        auth_store.save_refresh_token(result.refresh_token)
        del _pending_auth_flows[user_id]
        await update.message.reply_text(
            "✅ Dropbox linked successfully! You can now use /rent to queue downloads."
        )
        logger.info(f"Dropbox account linked for user {user_id}.")
    except Exception as exc:
        del _pending_auth_flows[user_id]
        logger.warning(f"OAuth code exchange failed for user {user_id}: {exc}")
        await update.message.reply_text(
            "❌ Invalid authorization code. Please run /start again and paste the correct code."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _auth_check(update):
        await update.message.reply_text("Unauthorized.")
        return

    await update.message.reply_text(
        "Available commands:\n"
        "/start — Link your Dropbox account\n"
        "/rent <magnet_uri> [tv|movie] — Queue a download\n"
        "/status — Show active downloads\n"
        "/list — Show last 10 completed/failed\n"
        "/cancel <id_prefix> — Cancel a queued item\n"
        "/help — Show this message",
        parse_mode=ParseMode.HTML,
    )


async def rent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /rent <magnet_uri> [tv|movie]

    Validates the magnet URI, optionally accepts a media type hint,
    and queues the download. Stores the originating chat_id so the
    queue processor can send a completion notification.
    """
    if not _auth_check(update):
        await update.message.reply_text("Unauthorized.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /rent <magnet_uri> [tv|movie]")
        return

    magnet_uri = args[0]

    # Full regex validation — not just startswith.
    if not _MAGNET_RE.match(magnet_uri):
        await update.message.reply_text(
            "Invalid Magnet URI. Expected format:\n"
            "<code>magnet:?xt=urn:btih:&lt;40-char-hash&gt;&amp;dn=...</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Optional media type hint: /rent <magnet> tv  or  /rent <magnet> movie
    media_type = "unknown"
    if len(args) >= 2:
        hint = args[1].lower()
        if hint in MEDIA_TYPES:
            media_type = hint
        else:
            await update.message.reply_text(
                f"Unknown media type '{hint}'. Use 'tv' or 'movie' (or omit for auto-detect)."
            )
            return

    chat_id = update.effective_chat.id

    added = add_download(
        identifier=magnet_uri,
        source_type="magnet",
        title="Manual magnet",   # queue_processor will update once metadata is fetched
        chat_id=chat_id,
        media_type=media_type,
    )

    if added:
        type_label = f" [{media_type.upper()}]" if media_type != "unknown" else ""
        await update.message.reply_text(f"Queued{type_label}. You'll be notified when done.")
        logger.info(f"Telegram /rent queued: {magnet_uri[:60]}…")
    else:
        await update.message.reply_text("Already in the system.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /status — Show all active (queued, downloading, uploading) items.
    """
    if not _auth_check(update):
        await update.message.reply_text("Unauthorized.")
        return

    active = []
    for s in _ACTIVE_STATUSES:
        active.extend(get_downloads_by_status(s))

    if not active:
        await update.message.reply_text("No active downloads.")
        return

    lines = [_format_record(r) for r in active]
    await update.message.reply_text("\n".join(lines))


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /list — Show the 10 most recent completed or failed items.
    """
    if not _auth_check(update):
        await update.message.reply_text("Unauthorized.")
        return

    completed = get_recent("completed", limit=5)
    failed = get_recent("failed", limit=5)
    records = sorted(
        completed + failed,
        key=lambda r: r.get("updated_at", 0),
        reverse=True,
    )[:10]

    if not records:
        await update.message.reply_text("No completed or failed downloads yet.")
        return

    lines = [_format_record(r) for r in records]
    await update.message.reply_text("\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /cancel <id_prefix>

    Cancels a queued item by matching the start of its identifier
    (use the short id shown in /status). Only queued items can be
    cancelled — in-progress downloads cannot be safely interrupted here.
    """
    if not _auth_check(update):
        await update.message.reply_text("Unauthorized.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /cancel <id_prefix>")
        return

    prefix = args[0].strip()

    # Search only within queued items to prevent accidental cancellation
    # of items already downloading or uploading.
    queued = get_downloads_by_status("queued")
    matches = [r for r in queued if r["identifier"].startswith(prefix)]

    if not matches:
        await update.message.reply_text(
            f"No queued item found with id starting with '{prefix}'.\n"
            "Use /status to see queued items and their id prefixes."
        )
        return

    if len(matches) > 1:
        lines = [f"• {_short_id(r['identifier'])}… — {r.get('title', '?')}" for r in matches]
        await update.message.reply_text(
            f"Ambiguous prefix — {len(matches)} items match:\n" + "\n".join(lines) +
            "\nProvide more characters to narrow it down."
        )
        return

    record = matches[0]
    update_status(record["identifier"], "cancelled")
    await update.message.reply_text(
        f"Cancelled: {record.get('title', record['identifier'][:40])}"
    )
    logger.info(f"Telegram /cancel: {record['identifier'][:60]}…")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def get_bot_application():
    """
    Build and return the configured telegram Application.
    Called once from main.py — handlers are registered here.
    """
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",  start_command))
    app.add_handler(CommandHandler("help",   help_command))
    app.add_handler(CommandHandler("rent",   rent_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("list",   list_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    # Must be registered after command handlers so commands are not captured here.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_auth_code))

    logger.info("Telegram bot handlers registered.")
    return app
