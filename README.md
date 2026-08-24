# Family Hub

A self-hosted central hub for the family's schedules: custody, school,
extracurriculars — with calendar feeds anyone can follow from Google, Outlook,
or iCloud, schedule messaging between co-parents, custody swap requests, and a
Skylight-style wall-display mode for a smart display (e.g. ApoloSign).

This repo contains two pieces:

1. **Family Hub web app** (`hub/`) — the schedule hub (below)
2. **FastDirect → Outlook sync** (`sync.py` and friends) — the original script
   that scrapes school emails into an Outlook calendar
   ([docs further down](#fastdirect--outlook-calendar-sync))

## Family Hub

### Features

- **Custody schedule engine** — alternating weeks, 2-2-3, 2-2-5-5, or a custom
  weekly pattern, with a handoff time. Every day shows who has the kids.
- **Events** — school, activities, medical, other; color-coded per kid;
  optional weekly repeats (e.g. "soccer practice every Tuesday until June").
- **Use any calendar app** — the hub publishes standard iCal (.ics) feed URLs.
  Family members who don't want another app just subscribe once from Google
  Calendar, Outlook, or iPhone/iCloud and stay in sync automatically. There
  are feeds for everything, custody-only, and one per kid.
- **Messaging** — conversation threads about the kids' schedules, optionally
  tagged to a specific kid.
- **Custody swap requests** — ask to switch days for vacations etc., optionally
  offering days in return. The other parent approves or declines; on approval
  the schedule (and every subscribed calendar) updates automatically. Each
  request has its own discussion thread.
- **Wall display mode** — a full-screen, dark, large-type week view designed
  for a wall-mounted smart display (ApoloSign, any Android tablet, a spare
  iPad). It's a plain web page behind a device token — open it in the
  display's browser (a kiosk browser app like Fully Kiosk works well), and it
  refreshes itself every 5 minutes. No login needed on the device.

### Run it

```bash
pip install -r requirements.txt
python serve.py            # serves on http://0.0.0.0:8000
```

Open the site, and the first visit walks you through setup: household name,
both parents, kids, and the custody pattern. Your co-parent gets an invite
link (shown in Settings) to set their own password.

Environment variables (all optional): `HUB_HOST`, `HUB_PORT`, `HUB_DB`
(path to the SQLite database, default `./hub.db`).

### Deploying (making it reachable from anywhere)

For Google/Outlook/iCloud to pull the feeds — and for the wall display and
your co-parent to reach the hub — it needs to be accessible over HTTPS from
the internet. Storage is pluggable: **SQLite** (default, needs a disk) or
**Postgres** via the `HUB_DATABASE_URL` environment variable (required on
serverless hosts, which have no persistent disk). Tables are created
automatically on first request — no migrations to run.

#### Vercel + Supabase (serverless)

1. **Supabase**: create a project, then copy the **Transaction pooler**
   connection string (Connect → Transaction pooler, port 6543) with your
   database password filled in.
2. **Vercel**: import this GitHub repo (the included `vercel.json` and
   `api/index.py` configure the Python function), and add one environment
   variable: `HUB_DATABASE_URL` = that connection string.
3. Deploy. Open the Vercel URL and run the setup wizard.

#### Container platforms (SQLite on a volume)

The repo also ships a `Dockerfile`; mount a volume at `/data` so the SQLite
database (`HUB_DB=/data/hub.db`) survives redeploys:

- **Railway**: Deploy from GitHub repo → add a **Volume** at `/data` →
  Generate Domain. (`PORT` is injected automatically.)
- **Render**: New → **Blueprint** → this repo; `render.yaml` sets up the
  service and persistent disk (needs the Starter plan).
- **Fly.io**: `fly launch` (uses `fly.toml`), `fly volumes create hub_data
  --size 1`, `fly deploy`.
- **Any VPS / home server**:
  `docker build -t family-hub . && docker run -d -p 8000:8000 -v hub-data:/data family-hub`
  behind Caddy or a Cloudflare Tunnel for HTTPS. (These platforms can also
  use Supabase instead — just set `HUB_DATABASE_URL`.)

Feed URLs contain unguessable tokens; share them only with family.

Note: Google Calendar refreshes subscribed iCal feeds on its own schedule
(typically every few hours up to ~a day) — that's a Google-side limit common
to all iCal feeds.

### Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

### Roadmap ideas

- Point the FastDirect sync at the hub (create hub events instead of Outlook
  events) so school messages land on everyone's calendars automatically.
- Push/email notifications for new messages and swap requests.
- Weather on the wall display.

---

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
