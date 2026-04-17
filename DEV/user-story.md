### **Epic: Telegram-Controlled Automated Media Downloader & Dropbox Uploader**

**Description:** Build a Telegram bot that allows users to queue media downloads via Magnet URIs or automated RSS feeds, smartly selects the highest quality files, processes them sequentially, and automatically categorizes and uploads them to Dropbox.

---

#### **User Story 1: Application Authentication & Linking**

**As a** Telegram user,
**I want to** be prompted to generate an authorization token via a provided URL when starting the bot,
**So that** the application can securely connect to my Dropbox account for future uploads.

- **Acceptance Criteria:**
  - Upon first interaction (or `/start`), the bot sends a message containing an authorization link.
  - Clicking the link navigates the user to a secure token generation page.
  - Once the token is generated, the backend application automatically retrieves and securely stores it.
  - The bot sends a confirmation message to the user once the account is successfully linked.

#### **User Story 2: Manual Download Triggering via Magnet Link**

**As a** user,
**I want to** send a Magnet URI to the Telegram bot,
**So that** I can manually initiate a specific download.

- **Acceptance Criteria:**
  - The bot accepts valid Magnet URIs as input messages.
  - The system acknowledges receipt of the link and adds the request to the application's processing pipeline.

#### **User Story 3: Smart Media Classification & Quality Selection**

**As a** user,
**I want** the application to automatically parse the requested download to determine the media type and the highest available quality,
**So that** I consistently receive the best version without needing to manually specify parameters.

- **Acceptance Criteria:**
  - The system parses the metadata/title to classify the media as either a "Movie" or a "Show".
  - If multiple quality versions (e.g., 1080p, 4K) are available within the payload or feed, the system automatically selects the highest resolution.

#### **User Story 4: Sequential Queue Management**

**As a** system administrator,
**I want** downloads to be processed strictly one at a time,
**So that** the server's network bandwidth and disk resources are not overwhelmed.

- **Acceptance Criteria:**
  - When a new download is triggered, the system checks if a download is currently active.
  - If a download is active, the new request is appended to a pending queue.
  - When the active download completes, the system automatically pulls the next item from the queue and begins processing.

#### **User Story 5: Categorized Dropbox Upload**

**As a** user,
**I want** my completed downloads to be automatically uploaded to Dropbox and sorted into specific directories based on their media type,
**So that** my cloud storage remains organized without manual file management.

- **Acceptance Criteria:**
  - Upon successful local download, the system initiates an upload to the linked Dropbox account.
  - The system routes the file to a specific Dropbox directory depending on the classification made in Story 3 (e.g., `/Dropbox/Movies/` vs `/Dropbox/Shows/`).
  - Files are successfully transferred without corruption.

#### **User Story 6: Completion Notification**

**As a** user,
**I want to** receive a message from the Telegram bot once the file has been successfully uploaded to Dropbox,
**So that** I know exactly when my media is ready to be accessed.

- **Acceptance Criteria:**
  - The system monitors the Dropbox API upload status.
  - Upon a successful upload response, the bot sends a distinct notification to the user indicating completion.

#### **User Story 7: Automated RSS Fetching**

**As a** user,
**I want** the application to periodically poll an RSS feed for new releases,
**So that** my system automatically queues and downloads new content without my manual intervention.

- **Acceptance Criteria:**
  - The system parses a provided RSS feed on a set schedule.
  - It identifies new, un-downloaded entries.
  - If a new entry is found, it evaluates the quality (per Story 3) and adds it to the download queue.
  - The queue respects the sequential processing rules (Story 4), ensuring background RSS downloads do not run concurrently with active downloads.
