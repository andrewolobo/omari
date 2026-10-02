# Omari Bot — Installation & Execution Guide

Omari is a Telegram bot that monitors RSS feeds, downloads torrents via libtorrent,
and uploads completed media to Dropbox. It exposes a local FastAPI server for the
Dropbox OAuth2 callback.

---

## Prerequisites (both platforms)

| Requirement                  | Version                                                             |
| ---------------------------- | ------------------------------------------------------------------- |
| Python                       | 3.11                                                                |
| Git                          | any recent version                                                  |
| A Telegram bot token         | from [@BotFather](https://t.me/BotFather)                           |
| A Dropbox App (key + secret) | from [Dropbox App Console](https://www.dropbox.com/developers/apps) |

---

## Repository layout

```
OMARI/
├── omar_bot/           # Application package
│   ├── .venv/          # Virtual environment (created during setup)
│   ├── .env            # Secret config (created from .env.example)
│   ├── data/           # Runtime data: db.json, omari.log, dropbox_token.json
│   ├── downloads/      # In-progress torrent downloads
│   └── WHL/            # Platform-specific wheels (libtorrent for Windows)
├── pyproject.toml      # Package definition; declares the `omari` command
└── omari.service       # systemd unit template (Ubuntu only)
```

---

## Environment configuration

Before running on either platform, create `omar_bot/.env` from the example:

```
cp omar_bot/.env.example omar_bot/.env   # Linux/macOS
copy omar_bot\.env.example omar_bot\.env  # Windows
```

Then edit `omar_bot/.env` and fill in every required value:

| Variable                   | Required | Description                                                            |
| -------------------------- | -------- | ---------------------------------------------------------------------- |
| `TELEGRAM_TOKEN`           | Yes      | Bot token from @BotFather                                              |
| `ALLOWED_USER_IDS`         | Yes      | Comma-separated Telegram user IDs permitted to use the bot             |
| `DROPBOX_APP_KEY`          | Yes      | Dropbox app key                                                        |
| `DROPBOX_APP_SECRET`       | Yes      | Dropbox app secret                                                     |
| `DROPBOX_REDIRECT_URI`     | Yes      | Must match the URI registered in the Dropbox App Console               |
| `OAUTH_BASE_URL`           | Yes      | Public base URL of the API server (same host:port as the redirect URI) |
| `API_HOST`                 | No       | Interface uvicorn binds to (default: `0.0.0.0`)                        |
| `API_PORT`                 | No       | Port uvicorn listens on (default: `8080`)                              |
| `RSS_URLS`                 | No       | Comma-separated RSS feed URLs to monitor                               |
| `RSS_POLL_INTERVAL`        | No       | Feed poll interval in seconds (default: `900`)                         |
| `MAX_CONCURRENT_DOWNLOADS` | No       | Simultaneous torrent downloads (default: `1`)                          |
| `DOWNLOAD_TIMEOUT_MINUTES` | No       | Minutes before a stalled download is retried (default: `30`)           |
| `SHOWS_DIRECTORY`          | No       | Dropbox destination for TV shows (default: `Shows`)                    |
| `MOVIES_DIRECTORY`         | No       | Dropbox destination for movies (default: `Movies`)                     |
| `NOTIFICATION_CHAT_ID`     | No       | Telegram user ID to notify on RSS download completion                  |

> **Dropbox redirect URI tip:** For local development use `http://localhost:8080/auth/dropbox/callback`.
> For an Ubuntu server, use your server's public address, e.g. `http://203.0.113.10:8080/auth/dropbox/callback`.
> Register the same URL under "Redirect URIs" in the Dropbox App Console.

---

## Windows — Development setup

### 1. Clone the repository

```cmd
git clone <repo-url> OMARI
cd OMARI
```

### 2. Create a virtual environment

```cmd
python -m venv omar_bot\.venv
```

### 3. Install libtorrent (Windows wheel)

The pre-built wheel is included in the repository:

```cmd
omar_bot\.venv\Scripts\pip install omar_bot\WHL\libtorrent-2.0.11-cp311-cp311-win_amd64.whl
```

> This wheel requires **Python 3.11 64-bit**. Run `python --version` to verify.

### 4. Install the omari package

```cmd
omar_bot\.venv\Scripts\pip install -e .
```

This registers the `omari` console script inside the virtual environment and installs
all other dependencies. The `-e` flag means code changes take effect immediately without
reinstalling.

### 5. Configure environment

```cmd
copy omar_bot\.env.example omar_bot\.env
```

Edit `omar_bot\.env` with your credentials (see the table above).

### 6. Run

```cmd
omar_bot\.venv\Scripts\omari
```

Or activate the virtual environment first and use the short form:

```cmd
omar_bot\.venv\Scripts\Activate.ps1   # PowerShell
omar_bot\.venv\Scripts\activate.bat   # cmd.exe
omari
```

### Stopping

Press `Ctrl+C`. The bot performs a graceful shutdown: cancels background tasks,
resets any in-progress downloads to `queued`, pauses the libtorrent session, and
stops the Telegram updater.

---

## Ubuntu Server — Production setup

These instructions assume Ubuntu 22.04 LTS or later and a dedicated `omari` system user.

### 1. Install system dependencies

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev git
```

### 2. Create a dedicated user

```bash
sudo useradd --system --create-home --shell /bin/bash omari
```

### 3. Clone the repository

```bash
sudo mkdir -p /opt/omari
sudo chown omari:omari /opt/omari
sudo -u omari git clone <repo-url> /opt/omari
cd /opt/omari
```

### 4. Create the virtual environment

```bash
sudo -u omari python3.11 -m venv omar_bot/.venv
```

### 5. Install libtorrent

```bash
sudo -u omari omar_bot/.venv/bin/pip install libtorrent
```

> If a pip wheel is not available for your exact Python version, fall back to the
> system package and recreate the venv with `--system-site-packages`:
>
> ```bash
> sudo apt install python3-libtorrent
> sudo -u omari python3.11 -m venv --system-site-packages omar_bot/.venv
> ```

### 6. Install the omari package

```bash
sudo -u omari omar_bot/.venv/bin/pip install -e .
```

### 7. Configure environment

```bash
sudo -u omari cp omar_bot/.env.example omar_bot/.env
sudo -u omari nano omar_bot/.env
```

Fill in all required variables. For a server, set `OAUTH_BASE_URL` and
`DROPBOX_REDIRECT_URI` to use your server's public IP or domain.

### 8. Install the systemd service

The repository includes a `omari.service` template. Verify the paths inside it
match your deployment, then install:

```bash
# Confirm paths in omari.service are correct (WorkingDirectory and ExecStart)
cat /opt/omari/omari.service

sudo cp /opt/omari/omari.service /etc/systemd/system/omari.service
sudo systemctl daemon-reload
sudo systemctl enable omari
sudo systemctl start omari
```

### 9. Verify the service

```bash
sudo systemctl status omari
```

You should see `active (running)`. To follow live logs:

```bash
journalctl -u omari -f
```

---

## Dropbox OAuth2 — first-time link

On first run (when `data/dropbox_token.json` does not exist), every authorised
Telegram user receives a message with a personalised link. Tap the link in Telegram,
grant access in your browser, and the bot will confirm linkage automatically.

To re-link at any time, use the `/start` command in the Telegram chat.

---

## Updating

After pulling code changes, no reinstall is needed (editable install). Just restart:

**Windows:**

```cmd
:: Stop the running process (Ctrl+C), then:
omar_bot\.venv\Scripts\omari
```

**Ubuntu (systemd):**

```bash
cd /opt/omari
sudo -u omari git pull
sudo systemctl restart omari
```

---

## Useful commands

| Purpose                    | Windows                                   | Ubuntu                                |
| -------------------------- | ----------------------------------------- | ------------------------------------- |
| Run the bot                | `omar_bot\.venv\Scripts\omari`            | `omar_bot/.venv/bin/omari`            |
| Start service              | —                                         | `sudo systemctl start omari`          |
| Stop service               | —                                         | `sudo systemctl stop omari`           |
| Restart service            | —                                         | `sudo systemctl restart omari`        |
| View live logs             | `omar_bot\data\omari.log`                 | `journalctl -u omari -f`              |
| Service status             | —                                         | `sudo systemctl status omari`         |
| Reinstall after dep change | `omar_bot\.venv\Scripts\pip install -e .` | `omar_bot/.venv/bin/pip install -e .` |
