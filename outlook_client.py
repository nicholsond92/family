"""Microsoft Graph API client for reading emails and creating Outlook calendar events."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import msal
import requests

import config

logger = logging.getLogger(__name__)

TOKEN_CACHE_FILE = "token_cache.json"


class OutlookClient:
    """Handles Microsoft Graph authentication, email reading, and calendar operations."""

    def __init__(self):
        self.token: str | None = None
        self._init_msal()

    def _init_msal(self):
        """Initialise the MSAL app for authentication."""
        cache = msal.SerializableTokenCache()
        if os.path.exists(TOKEN_CACHE_FILE):
            with open(TOKEN_CACHE_FILE) as f:
                cache.deserialize(f.read())
        self._cache = cache

        if config.MS_CLIENT_SECRET:
            self._app = msal.ConfidentialClientApplication(
                config.MS_CLIENT_ID,
                authority=f"https://login.microsoftonline.com/{config.MS_TENANT_ID}",
                client_credential=config.MS_CLIENT_SECRET,
                token_cache=cache,
            )
        else:
            self._app = msal.PublicClientApplication(
                config.MS_CLIENT_ID,
                authority=f"https://login.microsoftonline.com/{config.MS_TENANT_ID}",
                token_cache=cache,
            )

    def _save_cache(self):
        if self._cache.has_state_changed:
            with open(TOKEN_CACHE_FILE, "w") as f:
                f.write(self._cache.serialize())

    def authenticate(self) -> bool:
        """Authenticate via cached token or device-code flow."""
        accounts = self._app.get_accounts()
        if accounts:
            result = self._app.acquire_token_silent(
                config.MS_SCOPES, account=accounts[0]
            )
            if result and "access_token" in result:
                self.token = result["access_token"]
                self._save_cache()
                logger.info("Authenticated using cached token")
                return True

        # Device-code flow (interactive — user opens a browser)
        flow = self._app.initiate_device_flow(scopes=config.MS_SCOPES)
        if "user_code" not in flow:
            logger.error("Failed to initiate device flow: %s", flow)
            return False

        print("\n" + "=" * 60)
        print("MICROSOFT SIGN-IN REQUIRED")
        print("=" * 60)
        print(flow["message"])
        print("=" * 60 + "\n")

        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" in result:
            self.token = result["access_token"]
            self._save_cache()
            logger.info("Authenticated via device code flow")
            return True

        logger.error("Authentication failed: %s", result.get("error_description"))
        return False

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # ── Email reading ───────────────────────────────────────────

    def get_fastdirect_emails(
        self, lookback_days: int | None = None
    ) -> list[dict]:
        """Fetch recent emails from FastDirect notification senders."""
        if not self.token:
            raise RuntimeError("Not authenticated — call authenticate() first")

        days = lookback_days or config.EMAIL_LOOKBACK_DAYS
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Filter for FastDirect-related emails
        filter_query = (
            f"receivedDateTime ge {since} and ("
            "contains(from/emailAddress/address, 'fastdir') or "
            "contains(from/emailAddress/address, 'fastdirect') or "
            "contains(subject, 'FastDirect') or "
            "contains(subject, 'fast direct')"
            ")"
        )

        url = f"{config.MS_GRAPH_BASE}/me/messages"
        params = {
            "$filter": filter_query,
            "$orderby": "receivedDateTime desc",
            "$top": 50,
            "$select": "id,subject,bodyPreview,body,from,receivedDateTime,webLink",
        }

        resp = requests.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        messages = resp.json().get("value", [])
        logger.info("Found %d FastDirect emails in the last %d days", len(messages), days)
        return messages

    def extract_message_links(self, email: dict) -> list[str]:
        """Pull FastDirect portal links out of an email body."""
        from bs4 import BeautifulSoup
        import re

        links = []
        body_html = email.get("body", {}).get("content", "")
        soup = BeautifulSoup(body_html, "lxml")
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if re.search(r"fastdir\.com|fastdirect", href, re.I):
                links.append(href)
        return links

    # ── Calendar operations ─────────────────────────────────────

    def _get_calendar_id(self) -> str | None:
        """Get the ID of the target calendar (or default)."""
        if not config.MS_CALENDAR_NAME:
            return None  # use default calendar

        resp = requests.get(
            f"{config.MS_GRAPH_BASE}/me/calendars",
            headers=self._headers(),
        )
        resp.raise_for_status()
        for cal in resp.json().get("value", []):
            if cal["name"].lower() == config.MS_CALENDAR_NAME.lower():
                return cal["id"]

        logger.warning(
            "Calendar '%s' not found — using default", config.MS_CALENDAR_NAME
        )
        return None

    def create_event(
        self,
        subject: str,
        start: datetime,
        end: datetime | None = None,
        body: str = "",
        location: str = "",
        is_all_day: bool = False,
        reminder_minutes: int = 1440,
    ) -> dict | None:
        """Create an event on the user's Outlook calendar."""
        if not self.token:
            raise RuntimeError("Not authenticated — call authenticate() first")

        if end is None:
            end = start + timedelta(hours=1) if not is_all_day else start + timedelta(days=1)

        calendar_id = self._get_calendar_id()
        if calendar_id:
            url = f"{config.MS_GRAPH_BASE}/me/calendars/{calendar_id}/events"
        else:
            url = f"{config.MS_GRAPH_BASE}/me/events"

        if is_all_day:
            event_body = {
                "subject": subject,
                "body": {"contentType": "text", "content": body},
                "start": {"dateTime": start.strftime("%Y-%m-%dT00:00:00"), "timeZone": "UTC"},
                "end": {"dateTime": end.strftime("%Y-%m-%dT00:00:00"), "timeZone": "UTC"},
                "isAllDay": True,
                "isReminderOn": True,
                "reminderMinutesBeforeStart": reminder_minutes,
            }
        else:
            event_body = {
                "subject": subject,
                "body": {"contentType": "text", "content": body},
                "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
                "isReminderOn": True,
                "reminderMinutesBeforeStart": reminder_minutes,
            }

        if location:
            event_body["location"] = {"displayName": location}

        resp = requests.post(url, headers=self._headers(), json=event_body)
        if resp.status_code == 201:
            event = resp.json()
            logger.info("Created calendar event: %s", subject)
            return event
        else:
            logger.error(
                "Failed to create event '%s': %s %s",
                subject, resp.status_code, resp.text,
            )
            return None

    def event_exists(self, subject: str, start: datetime) -> bool:
        """Check if an event with the same subject and start already exists (dedup)."""
        if not self.token:
            return False

        window_start = (start - timedelta(hours=1)).isoformat()
        window_end = (start + timedelta(hours=1)).isoformat()

        url = f"{config.MS_GRAPH_BASE}/me/calendarView"
        params = {
            "startDateTime": window_start,
            "endDateTime": window_end,
            "$filter": f"contains(subject, '{subject}')",
            "$select": "subject,start",
            "$top": 5,
        }

        try:
            resp = requests.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            return len(resp.json().get("value", [])) > 0
        except requests.RequestException:
            return False
