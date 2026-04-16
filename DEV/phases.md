### Phase 0: Project Setup

First, let's establish our dependencies and the modular directory structure.

**`requirements.txt`**
```text
python-telegram-bot==20.7
tinydb==4.8.0
feedparser==6.0.11
dropbox==11.36.2
libtorrent==2.0.9
```
*(Note: `libtorrent` can sometimes be tricky to install via `pip` depending on your OS. If you are on Linux, `apt-get install python3-libtorrent` is often the most stable route.)*

**Project Directory Structure**
```text
omar_bot/
│
├── config.py             # Environment variables and constants
├── main.py               # Main execution loop and RSS worker
├── database.py           # TinyDB state management
├── bot.py                # Telegram command handlers
├── torrent.py            # libtorrent engine and quality logic
├── dropbox_sync.py       # Chunked upload service
└── data/
    └── db.json           # TinyDB storage file
```

---

### Phase 1: Database Schema & Tracking (`database.py`)

We need a simple way to insert downloads, update their statuses (`queued`, `downloading`, `uploading`, `completed`, `failed`), and query them to prevent duplicates.

```python
from tinydb import TinyDB, Query
import time

db = TinyDB('data/db.json')
Downloads = Query()

def add_download(identifier, source_type, title="Unknown"):
    # source_type can be 'magnet' or 'rss'
    if not db.search(Downloads.identifier == identifier):
        db.insert({
            'identifier': identifier,
            'title': title,
            'source_type': source_type,
            'status': 'queued',
            'added_at': time.time()
        })
        return True
    return False

def update_status(identifier, new_status):
    db.update({'status': new_status}, Downloads.identifier == identifier)

def get_downloads_by_status(status):
    return db.search(Downloads.status == status)

def is_downloaded(identifier):
    # Check if we've already processed this to avoid RSS duplicates
    return len(db.search(Downloads.identifier == identifier)) > 0
```

---

### Phase 2: Telegram Bot Interface (`bot.py`)

Using `python-telegram-bot`, we'll set up the command listener and enforce basic user authentication.

```python
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from database import add_download
import config

async def auth_check(update: Update) -> bool:
    if str(update.effective_user.id) not in config.ALLOWED_USER_IDS:
        await update.message.reply_text("Unauthorized user.")
        return False
    return True

async def rent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return

    if not context.args:
        await update.message.reply_text("Usage: /rent <magnet_uri>")
        return

    magnet_uri = context.args[0]
    
    # Basic Magnet validation
    if not magnet_uri.startswith("magnet:?xt=urn:btih:"):
        await update.message.reply_text("Invalid Magnet URI format.")
        return

    if add_download(identifier=magnet_uri, source_type='magnet'):
        await update.message.reply_text("Magnet added to queue.")
    else:
        await update.message.reply_text("Magnet is already in the system.")

def get_bot_application():
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("rent", rent_command))
    return app
```

---

### Phase 3: Torrent Engine & Quality Selection (`torrent.py`)

Here we handle the `libtorrent` session and the regex-based quality parser for the RSS feed.

```python
import libtorrent as lt
import time
import re

# Quality selection logic
def get_best_quality_torrent(torrent_entries):
    quality_weights = {
        '2160p': 4,
        '1080p': 3,
        '720p': 2,
        '480p': 1
    }
    
    best_entry = None
    highest_weight = 0

    for entry in torrent_entries:
        title = entry.title
        # Find resolution tag
        match = re.search(r'(2160p|1080p|720p|480p)', title, re.IGNORECASE)
        if match:
            res = match.group(1).lower()
            weight = quality_weights.get(res, 0)
            if weight > highest_weight:
                highest_weight = weight
                best_entry = entry
                
    return best_entry

# Torrent Engine wrapper
class TorrentManager:
    def __init__(self, save_path='./downloads'):
        self.session = lt.session()
        self.session.listen_on(6881, 6891)
        self.save_path = save_path

    def download_magnet(self, magnet_uri):
        params = {
            'save_path': self.save_path,
            'storage_mode': lt.storage_mode_t(2)
        }
        handle = lt.add_magnet_uri(self.session, magnet_uri, params)
        
        # Block until metadata is downloaded
        while not handle.has_metadata():
            time.sleep(1)
            
        print(f"Starting download: {handle.name()}")
        
        while not handle.is_seed():
            s = handle.status()
            print(f"Progress: {s.progress * 100:.2f}% - State: {s.state}")
            time.sleep(5) # In production, make this async/event-driven
            
        print(f"Download complete: {handle.name()}")
        return handle.name() # Return folder/file name for Dropbox
```

---

### Phase 4: Dropbox Upload Service (`dropbox_sync.py`)

This is the most critical part for large files. We must use the `files_upload_session` endpoints to chunk the upload. 

```python
import dropbox
import os
import config

dbx = dropbox.Dropbox(config.DROPBOX_ACCESS_TOKEN)
CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB chunks

def upload_large_file(local_path, dropbox_path):
    file_size = os.path.getsize(local_path)
    
    with open(local_path, 'rb') as f:
        if file_size <= CHUNK_SIZE:
            dbx.files_upload(f.read(), dropbox_path)
            return

        # Chunked upload logic
        upload_session_start_result = dbx.files_upload_session_start(f.read(CHUNK_SIZE))
        cursor = dropbox.files.UploadSessionCursor(
            session_id=upload_session_start_result.session_id,
            offset=f.tell()
        )
        commit = dropbox.files.CommitInfo(path=dropbox_path)

        while f.tell() < file_size:
            if (file_size - f.tell()) <= CHUNK_SIZE:
                dbx.files_upload_session_finish(f.read(CHUNK_SIZE), cursor, commit)
            else:
                dbx.files_upload_session_append_v2(f.read(CHUNK_SIZE), cursor)
                cursor.offset = f.tell()

def sync_to_dropbox(local_target_name):
    base_path = os.path.join('./downloads', local_target_name)
    
    if os.path.isfile(base_path):
        upload_large_file(base_path, f"/{local_target_name}")
        os.remove(base_path) # Cleanup
    else:
        # Recursive folder upload
        for root, dirs, files in os.walk(base_path):
            for file in files:
                local_file_path = os.path.join(root, file)
                # Preserve directory structure in Dropbox
                relative_path = os.path.relpath(local_file_path, './downloads')
                dropbox_path = f"/{relative_path.replace(os.sep, '/')}"
                
                upload_large_file(local_file_path, dropbox_path)
                os.remove(local_file_path) # Cleanup file
                
        # Cleanup empty directories
        for root, dirs, files in os.walk(base_path, topdown=False):
            for name in dirs:
                os.rmdir(os.path.join(root, name))
        os.rmdir(base_path)
```

---

### Phase 5: RSS Worker & Main Loop (`main.py`)

Finally, we tie it all together using `asyncio` to run the Telegram bot and our background worker concurrently.

```python
import asyncio
import feedparser
from bot import get_bot_application
from database import update_status, get_downloads_by_status, is_downloaded, add_download
from torrent import TorrentManager, get_best_quality_torrent
from dropbox_sync import sync_to_dropbox
import config

async def rss_worker():
    while True:
        try:
            feed = feedparser.parse(config.RSS_URL)
            # Group entries by series/movie (you'll need custom logic here based on your feed's naming conventions)
            # For simplicity, let's assume we just grab the best quality of all recent entries
            best_entry = get_best_quality_torrent(feed.entries)
            
            if best_entry and not is_downloaded(best_entry.link):
                print(f"Found new RSS entry: {best_entry.title}")
                add_download(best_entry.link, 'rss', best_entry.title)
        except Exception as e:
            print(f"RSS Worker Error: {e}")
            
        await asyncio.sleep(config.RSS_POLL_INTERVAL)

async def processing_queue():
    tm = TorrentManager()
    while True:
        queued_items = get_downloads_by_status('queued')
        for item in queued_items:
            try:
                # 1. Download
                update_status(item['identifier'], 'downloading')
                target_name = tm.download_magnet(item['identifier'])
                
                # 2. Upload
                update_status(item['identifier'], 'uploading')
                sync_to_dropbox(target_name)
                
                # 3. Complete
                update_status(item['identifier'], 'completed')
                
            except Exception as e:
                print(f"Pipeline failed for {item['identifier']}: {e}")
                update_status(item['identifier'], 'failed')
                
        await asyncio.sleep(10)

async def main():
    # Initialize Telegram Bot
    bot_app = get_bot_application()
    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling()

    # Run background tasks
    task1 = asyncio.create_task(rss_worker())
    task2 = asyncio.create_task(processing_queue())

    # Keep loop running
    await asyncio.gather(task1, task2)

if __name__ == "__main__":
    asyncio.run(main())
```

### Next Steps for You
1. **Event-Driven UI Updates:** The `torrent.py` currently uses `time.sleep()` for polling progress. To send real-time Telegram updates (e.g., 50% complete), you'll need to pass the `bot` context into the `TorrentManager` and use `libtorrent`'s alert system (`session.pop_alerts()`) to trigger message edits asynchronously.
2. **Error Recovery:** Implement retry logic in the `processing_queue` for items marked as `failed`, especially for Dropbox rate limits (`dropbox.exceptions.RateLimitError`). 

Let me know if you want to drill down into the Telegram progress-bar logic or the error handling specifically.