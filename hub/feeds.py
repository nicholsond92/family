"""iCalendar (.ics) feed generation.

Feeds are plain RFC 5545 calendars served over HTTPS, so any calendar app —
Google Calendar, Outlook, Apple/iCloud Calendar, Fastmail, Thunderbird — can
subscribe with a URL and stay in sync without installing anything.

Timed events are emitted as floating local times (no TZID), which is correct
for a family living in one timezone. Custody blocks are all-day events.
"""

from datetime import date, datetime, timedelta

from . import custody

PRODID = "-//Family Hub//Schedule Feed//EN"

CATEGORY_EMOJI = {
    "school": "🎒",
    "activity": "⚽",
    "medical": "🩺",
    "custody": "🏠",
    "other": "📌",
}


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """Fold a content line at 75 octets per RFC 5545 (continuation lines
    start with a single space)."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts = []
    limit = 75
    while raw:
        chunk = raw[:limit]
        # Don't split inside a multi-byte UTF-8 sequence: back off until the
        # chunk decodes cleanly.
        while len(chunk) < len(raw):
            try:
                chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        raw = raw[len(chunk):]
        limit = 74  # continuation lines lose one octet to the leading space
    return "\r\n ".join(parts)


def _fmt_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _fmt_dt(d: date, hhmm: str) -> str:
    return f"{d.strftime('%Y%m%d')}T{hhmm.replace(':', '')}00"


def build_ics(name: str, vevents: list[list[str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        "X-PUBLISHED-TTL:PT1H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
    ]
    for ev in vevents:
        lines.extend(ev)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


def _dtstamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def event_vevent(ev, kid_names: list[str]) -> list[str]:
    emoji = CATEGORY_EMOJI.get(ev["category"], "📌")
    title = f"{emoji} {ev['title']}"
    if kid_names:
        title += f" ({', '.join(kid_names)})"
    d = date.fromisoformat(ev["date"])
    lines = [
        "BEGIN:VEVENT",
        f"UID:event-{ev['id']}@family-hub",
        f"DTSTAMP:{_dtstamp()}",
        f"SUMMARY:{ics_escape(title)}",
    ]
    if ev["all_day"] or not ev["start_time"]:
        lines.append(f"DTSTART;VALUE=DATE:{_fmt_date(d)}")
        lines.append(f"DTEND;VALUE=DATE:{_fmt_date(d + timedelta(days=1))}")
    else:
        lines.append(f"DTSTART:{_fmt_dt(d, ev['start_time'])}")
        end_time = ev["end_time"] or ev["start_time"]
        lines.append(f"DTEND:{_fmt_dt(d, end_time)}")
    if ev["location"]:
        lines.append(f"LOCATION:{ics_escape(ev['location'])}")
    if ev["notes"]:
        lines.append(f"DESCRIPTION:{ics_escape(ev['notes'])}")
    lines.append(f"CATEGORIES:{ics_escape(ev['category'].capitalize())}")
    lines.append("END:VEVENT")
    return lines


def custody_vevent(block, parent_name: str, handoff_time: str) -> list[str]:
    summary = f"🏠 Kids with {parent_name}"
    desc = f"Custody: {parent_name} has the kids. Handoff around {handoff_time}."
    if block["has_override"]:
        desc += " Includes an approved schedule swap."
    return [
        "BEGIN:VEVENT",
        f"UID:custody-{block['start'].isoformat()}-{block['parent_id']}@family-hub",
        f"DTSTAMP:{_dtstamp()}",
        f"SUMMARY:{ics_escape(summary)}",
        f"DTSTART;VALUE=DATE:{_fmt_date(block['start'])}",
        f"DTEND;VALUE=DATE:{_fmt_date(block['end'] + timedelta(days=1))}",
        f"DESCRIPTION:{ics_escape(desc)}",
        "TRANSP:TRANSPARENT",
        "CATEGORIES:Custody",
        "END:VEVENT",
    ]


def feed_events(conn, kind: str, kid_id: int | None, start: date, end: date):
    """Rows of events included in a feed of the given kind."""
    if kind == "custody":
        return []
    if kind == "kid" and kid_id:
        sql = (
            "SELECT DISTINCT e.* FROM events e "
            "JOIN event_kids ek ON ek.event_id = e.id "
            "WHERE ek.kid_id = ? AND e.date BETWEEN ? AND ? ORDER BY e.date"
        )
        return conn.execute(sql, (kid_id, start.isoformat(), end.isoformat())).fetchall()
    return conn.execute(
        "SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date",
        (start.isoformat(), end.isoformat()),
    ).fetchall()


def generate_feed(conn, feed) -> str:
    """Build the full .ics text for a feed row."""
    today = date.today()
    start = today - timedelta(days=30)
    end = today + timedelta(days=365)
    parents = {p["id"]: p["name"] for p in conn.execute("SELECT id, name FROM parents")}
    kid_names_by_event: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT ek.event_id, k.name FROM event_kids ek JOIN kids k ON k.id = ek.kid_id "
        "ORDER BY k.name"
    ):
        kid_names_by_event.setdefault(row["event_id"], []).append(row["name"])

    vevents = []
    for ev in feed_events(conn, feed["kind"], feed["kid_id"], start, end):
        vevents.append(event_vevent(ev, kid_names_by_event.get(ev["id"], [])))

    if feed["kind"] in ("all", "custody", "kid"):
        schedule = custody.load_schedule(conn)
        if schedule:
            handoff = schedule["handoff_time"]
            for block in custody.custody_blocks(conn, start, end):
                pname = parents.get(block["parent_id"], "parent")
                vevents.append(custody_vevent(block, pname, handoff))

    return build_ics(feed["name"], vevents)
