# FastDirect → Outlook Calendar Sync

Automatically reads school notification emails from FastDirect, scrapes full
message content from the FastDirect portal, extracts events and dates, and
adds them to your Microsoft 365 Outlook calendar.

## How it works

1. Connects to your Microsoft 365 mailbox via Graph API
2. Finds emails from FastDirect (the school communication platform)
3. Follows links in those emails to log in to FastDirect and read the full message
4. Parses event dates, times, and details from the message text
5. Creates calendar events in your Outlook calendar (with deduplication)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Register a Microsoft Azure AD app

1. Go to [Azure Portal > App Registrations](https://portal.azure.com/#blade/Microsoft_Intl/AppRegistrationBlade)
2. Click **New registration**
3. Name it something like "School Calendar Sync"
4. Under **Supported account types**, select "Accounts in any organizational directory and personal Microsoft accounts"
5. Under **Redirect URI**, select **Public client/native** and enter `http://localhost`
6. Click **Register**
7. Copy the **Application (client) ID** and **Directory (tenant) ID**
8. Go to **API permissions** > **Add a permission** > **Microsoft Graph** > **Delegated permissions**
9. Add: `Mail.Read`, `Calendars.ReadWrite`
10. Click **Grant admin consent** (or consent will be requested on first login)

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

- **FASTDIRECT_SCHOOL_CODE**: Your school's FastDirect code (the part after `fastdir.com/`)
- **FASTDIRECT_USERNAME**: Your family ID for FastDirect
- **FASTDIRECT_PASSWORD**: Your FastDirect password
- **MS_CLIENT_ID**: From Azure app registration (step 6 above)
- **MS_TENANT_ID**: From Azure app registration (step 6 above)

### 4. First run

```bash
python sync.py
```

On first run, you'll be prompted to sign in to Microsoft via a browser (device code flow). After that, the token is cached locally.

## Usage

```bash
# Standard run — fetch emails, scrape messages, create calendar events
python sync.py

# Preview what would be created without actually creating events
python sync.py --dry-run

# Look back further for emails
python sync.py --days 14

# Skip FastDirect login, just parse email previews
python sync.py --skip-scrape

# Verbose logging
python sync.py -v
```

## Automation

To run this automatically, set up a cron job or scheduled task:

```bash
# Every morning at 7am
0 7 * * * cd /path/to/family && python sync.py >> sync.log 2>&1
```

## Project structure

```
config.py               — Configuration from environment variables
fastdirect_scraper.py   — FastDirect portal login and message scraping
outlook_client.py       — Microsoft Graph API (email + calendar)
event_parser.py         — Date/event extraction from message text
sync.py                 — Main entry point / orchestrator
```

## Notes

- FastDirect has no official API, so the scraper works by parsing HTML. If
  FastDirect changes their page layout, the scraper may need updating.
- The event parser recognises common UK school event keywords and date formats
  (DD/MM/YYYY, "15th January", etc.)
- Events are deduplicated by subject + date, so re-running is safe.
- Credentials are stored locally in `.env` and never committed to git.
