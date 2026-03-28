#!/usr/bin/env python3
"""
School Messages → Outlook Calendar Sync

Reads FastDirect school notification emails from your Microsoft 365 inbox,
follows the links to scrape full message content, extracts events/dates,
and creates corresponding Outlook calendar entries.

Usage:
    python sync.py              # Run once
    python sync.py --dry-run    # Preview events without creating them
    python sync.py --days 14    # Look back 14 days instead of default
"""

import argparse
import logging
import sys

from event_parser import parse_events
from fastdirect_scraper import FastDirectScraper
from outlook_client import OutlookClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Sync FastDirect school messages to Outlook calendar"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview events without creating calendar entries",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to look back for emails (default: from .env)",
    )
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Only parse email previews, don't log in to FastDirect for full messages",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ── Step 1: Authenticate with Microsoft 365 ────────────────
    logger.info("Authenticating with Microsoft 365...")
    outlook = OutlookClient()
    if not outlook.authenticate():
        logger.error("Failed to authenticate with Microsoft 365")
        sys.exit(1)

    # ── Step 2: Fetch FastDirect notification emails ───────────
    logger.info("Fetching FastDirect notification emails...")
    emails = outlook.get_fastdirect_emails(lookback_days=args.days)

    if not emails:
        logger.info("No FastDirect emails found — nothing to do")
        return

    logger.info("Found %d FastDirect email(s)", len(emails))

    # ── Step 3: Optionally scrape full messages from FastDirect ─
    scraper = None
    if not args.skip_scrape:
        logger.info("Logging in to FastDirect...")
        scraper = FastDirectScraper()
        if not scraper.login():
            logger.warning(
                "Could not log in to FastDirect — will use email previews only"
            )
            scraper = None

    # ── Step 4: Process each email ─────────────────────────────
    all_events = []
    for email in emails:
        subject = email.get("subject", "(no subject)")
        preview = email.get("bodyPreview", "")
        logger.info("Processing: %s", subject)

        # Try to get full message from FastDirect
        full_text = preview
        if scraper:
            links = outlook.extract_message_links(email)
            for link in links:
                msg = scraper.get_message_from_link(link)
                if msg and msg.body:
                    full_text = msg.body
                    logger.info("  Got full message from FastDirect (%d chars)", len(full_text))
                    break

        # Parse events from the message
        events = parse_events(full_text, subject=subject)
        if events:
            logger.info("  Found %d event(s) in this message", len(events))
            all_events.extend(events)
        else:
            logger.info("  No calendar events found in this message")

    if not all_events:
        logger.info("No events found across all messages — nothing to add")
        return

    # ── Step 5: Create calendar events ─────────────────────────
    logger.info("\n%s %d event(s) to process:", "Preview:" if args.dry_run else "Creating", len(all_events))

    created = 0
    skipped = 0
    for event in all_events:
        date_str = event.date.strftime("%a %d %b %Y")
        time_str = "" if event.is_all_day else event.date.strftime(" at %H:%M")

        if args.dry_run:
            print(f"  [DRY RUN] {event.title} — {date_str}{time_str}")
            if event.location:
                print(f"            Location: {event.location}")
            continue

        # Dedup — skip if event already exists
        if outlook.event_exists(event.title, event.date):
            logger.info("  SKIP (already exists): %s — %s", event.title, date_str)
            skipped += 1
            continue

        result = outlook.create_event(
            subject=event.title,
            start=event.date,
            end=event.end_date,
            body=f"Auto-synced from FastDirect school message.\n\n{event.description}",
            location=event.location,
            is_all_day=event.is_all_day,
        )

        if result:
            print(f"  ✓ Created: {event.title} — {date_str}{time_str}")
            created += 1
        else:
            print(f"  ✗ Failed:  {event.title} — {date_str}{time_str}")

    if not args.dry_run:
        logger.info(
            "Done! Created %d event(s), skipped %d duplicate(s)", created, skipped
        )


if __name__ == "__main__":
    main()
