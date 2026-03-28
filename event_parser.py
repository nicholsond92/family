"""Extract event dates, times, and descriptions from school message text."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from dateutil import parser as dateutil_parser

logger = logging.getLogger(__name__)

# Keywords that signal an event or important date
EVENT_KEYWORDS = [
    r"school\s+trip",
    r"field\s+trip",
    r"sports\s+day",
    r"parents?\s*[''']?\s*evening",
    r"parents?\s*[''']?\s*night",
    r"open\s+(day|evening|night)",
    r"concert",
    r"performance",
    r"play\b",
    r"assembly",
    r"mass\b",
    r"liturgy",
    r"nativity",
    r"celebration",
    r"fete|fair|fayre",
    r"disco",
    r"non[\s-]?uniform",
    r"mufti",
    r"dress[\s-]?up",
    r"costume",
    r"book\s+week",
    r"cake\s+sale",
    r"bake\s+sale",
    r"fundrais",
    r"charity",
    r"photo(graph)?\s+day",
    r"picture\s+day",
    r"inset\s+day",
    r"training\s+day",
    r"half[\s-]?term",
    r"holiday",
    r"break\s+up",
    r"term\s+(start|end|begin)",
    r"return\s+to\s+school",
    r"school\s+clos",
    r"no\s+school",
    r"early\s+(finish|closure|dismissal)",
    r"late\s+start",
    r"club",
    r"workshop",
    r"meeting",
    r"mass\b",
    r"confession",
    r"sacrament",
    r"communion",
    r"confirmation",
    r"exam",
    r"test\s+week",
    r"sats?\b",
    r"assessment",
    r"report",
    r"deadline",
    r"due\s+date",
    r"reminder",
    r"picture\s+day",
    r"vaccination",
    r"nurse\s+visit",
    r"swimming",
    r"pe\s+kit",
    r"uniform",
    r"lunch\s+menu",
]

# Date patterns commonly found in school messages
DATE_PATTERNS = [
    # "Monday 15th January 2024" / "Monday 15 January"
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{4}))?",
    # "15th January 2024" / "15 January"
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{4}))?",
    # "January 15th, 2024"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
    # "15/01/2024" or "15-01-2024"  (DD/MM/YYYY - UK format)
    r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})",
    # "w/c 15th January" (week commencing)
    r"w/c\s+(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+(\d{4}))?",
]

TIME_PATTERNS = [
    r"(\d{1,2})[:\.](\d{2})\s*(am|pm|AM|PM)",  # 2:30pm, 2.30PM
    r"(\d{1,2})\s*(am|pm|AM|PM)",                # 2pm
    r"(\d{1,2})[:\.](\d{2})\s*(?:hrs?|hours?)?",  # 14:30, 14.30
]


@dataclass
class ParsedEvent:
    title: str
    date: datetime
    end_date: datetime | None = None
    is_all_day: bool = True
    location: str = ""
    description: str = ""
    source_text: str = ""


def parse_events(message_text: str, subject: str = "") -> list[ParsedEvent]:
    """Parse school message text and extract calendar-worthy events."""
    events: list[ParsedEvent] = []
    full_text = f"{subject}\n{message_text}"

    # Split into sentences/lines for context
    lines = re.split(r"[\n\r]+|(?<=[.!?])\s+", full_text)

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # Check if this line mentions an event
        event_match = None
        for pattern in EVENT_KEYWORDS:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                event_match = m
                break

        if not event_match:
            continue

        # Look for dates in this line and surrounding context
        context = " ".join(lines[max(0, i - 1) : min(len(lines), i + 3)])
        dates = _extract_dates(context)
        times = _extract_times(context)

        if not dates:
            # No date found — skip this event candidate
            logger.debug("Event keyword found but no date: %s", line[:80])
            continue

        # Build the event title
        title = _build_title(line, event_match.group())

        for date in dates:
            start_time, end_time = None, None
            if times:
                start_time = times[0]
                if len(times) > 1:
                    end_time = times[1]

            is_all_day = start_time is None

            if start_time:
                start_dt = datetime.combine(date, start_time)
                if end_time:
                    end_dt = datetime.combine(date, end_time)
                else:
                    end_dt = start_dt + timedelta(hours=1)
            else:
                start_dt = datetime.combine(date, time.min)
                end_dt = start_dt + timedelta(days=1)

            # Extract location if mentioned
            location = _extract_location(context)

            event = ParsedEvent(
                title=title,
                date=start_dt,
                end_date=end_dt,
                is_all_day=is_all_day,
                location=location,
                description=line,
                source_text=context[:500],
            )
            events.append(event)
            logger.info("Parsed event: %s on %s", title, start_dt)

    return events


def _extract_dates(text: str) -> list[datetime]:
    """Extract date objects from text using multiple patterns."""
    dates = []
    current_year = datetime.now().year

    for pattern in DATE_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            try:
                date_str = m.group(0).replace("w/c ", "").strip()
                # Remove ordinal suffixes for parsing
                date_str = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str)
                parsed = dateutil_parser.parse(date_str, dayfirst=True, default=datetime(current_year, 1, 1))
                # Don't return dates far in the past
                if parsed.year < current_year - 1:
                    parsed = parsed.replace(year=current_year)
                dates.append(parsed)
            except (ValueError, TypeError):
                continue

    # Deduplicate by date
    seen = set()
    unique = []
    for d in dates:
        key = d.date()
        if key not in seen:
            seen.add(key)
            unique.append(d)

    return unique


def _extract_times(text: str) -> list[time]:
    """Extract time objects from text."""
    times = []
    for pattern in TIME_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            groups = m.groups()
            try:
                hour = int(groups[0])
                minutes = int(groups[1]) if groups[1] and groups[1].isdigit() else 0
                # Check for am/pm
                ampm = None
                for g in groups:
                    if g and g.lower() in ("am", "pm"):
                        ampm = g.lower()
                if ampm == "pm" and hour < 12:
                    hour += 12
                elif ampm == "am" and hour == 12:
                    hour = 0
                times.append(time(hour, minutes))
            except (ValueError, TypeError):
                continue

    times.sort()
    return times


def _build_title(line: str, keyword: str) -> str:
    """Build a concise event title from the line and keyword."""
    # Clean up the keyword for display
    title = keyword.strip().title()

    # If the line is short enough, use it as the title
    if len(line) <= 80:
        return line.strip()

    # Otherwise use a trimmed version centred on the keyword
    idx = line.lower().find(keyword.lower())
    if idx >= 0:
        start = max(0, idx - 20)
        end = min(len(line), idx + len(keyword) + 40)
        snippet = line[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(line):
            snippet = snippet + "..."
        return snippet

    return title


def _extract_location(text: str) -> str:
    """Try to extract a location from the text."""
    location_patterns = [
        r"(?:at|in|venue:?)\s+(?:the\s+)?([A-Z][A-Za-z\s']+(?:Hall|Church|Centre|Center|Room|Field|Park|School|Gym|Theatre|Theater|Library|Chapel))",
        r"(?:Hall|Church|Centre|Room|Field)\s*\d*",
    ]
    for pattern in location_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).strip() if m.lastindex else m.group(0).strip()
    return ""
