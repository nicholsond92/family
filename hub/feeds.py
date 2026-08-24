"""iCalendar (.ics) feed generation — permission-aware.

Feeds are plain RFC 5545 calendars served over HTTPS, so any calendar app —
Google Calendar, Outlook, Apple/iCloud Calendar, Fastmail, Thunderbird — can
subscribe with a URL and stay in sync without installing anything.

Every feed belongs to a viewer (an adult in the household) or to no one
(household feeds, e.g. for the wall display). Private events render with full
details only when the viewer is allowed to see them — that kid's co-parents
and the event's creator; everyone else gets a "Busy" block that still
reserves the time.

Timed events are emitted as floating local times (no TZID), which is correct
for a family living in one timezone. Custody blocks are all-day events,
one stream per co-parenting circle, labeled with that circle's kids.
"""

from datetime import date, datetime, timedelta

from . import custody

PRODID = "-//Family Hub//Schedule Feed//EN"


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


def event_vevent(ev, kid_names: list[str], visible: bool) -> list[str]:
    """One VEVENT; a private event the viewer can't see becomes a Busy block."""
    title = ev["title"] if visible else "Busy"
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
    if visible:
        if ev["location"]:
            lines.append(f"LOCATION:{ics_escape(ev['location'])}")
        if ev["notes"]:
            lines.append(f"DESCRIPTION:{ics_escape(ev['notes'])}")
        lines.append(f"CATEGORIES:{ics_escape(ev['category'].capitalize())}")
    else:
        lines.append("DESCRIPTION:Details are private.")
    lines.append("END:VEVENT")
    return lines


def custody_vevent(circle_id: int, block, parent_name: str, kid_label: str,
                   handoff_time: str) -> list[str]:
    summary = f"{kid_label} with {parent_name}" if kid_label else f"Kids with {parent_name}"
    desc = f"Custody: {parent_name} has {kid_label or 'the kids'}. Handoff around {handoff_time}."
    if block["has_override"]:
        desc += " Includes an approved schedule swap."
    return [
        "BEGIN:VEVENT",
        f"UID:custody-{circle_id}-{block['start'].isoformat()}-{block['parent_id']}@family-hub",
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


def viewer_allowed_parents(conn) -> dict[int, set[int]]:
    """For each event id: the set of parent ids allowed to see full details
    of that event when it is private (kids' co-parents + creator)."""
    circle_members: dict[int, set[int]] = {}
    for row in conn.execute("SELECT circle_id, parent_id FROM circle_parents"):
        circle_members.setdefault(row["circle_id"], set()).add(row["parent_id"])
    allowed: dict[int, set[int]] = {}
    for row in conn.execute(
        "SELECT ek.event_id, k.circle_id FROM event_kids ek "
        "JOIN kids k ON k.id = ek.kid_id"
    ):
        if row["circle_id"]:
            allowed.setdefault(row["event_id"], set()).update(
                circle_members.get(row["circle_id"], set())
            )
    for row in conn.execute("SELECT id, created_by FROM events"):
        if row["created_by"]:
            allowed.setdefault(row["id"], set()).add(row["created_by"])
    return allowed


def event_visible_to(ev, viewer_id: int | None, allowed: dict[int, set[int]]) -> bool:
    if not ev["private"]:
        return True
    if viewer_id is None:
        return False
    return viewer_id in allowed.get(ev["id"], set())


def generate_feed(conn, feed) -> str:
    """Build the full .ics text for a feed row, masked for its owner."""
    today = date.today()
    start = today - timedelta(days=30)
    end = today + timedelta(days=365)
    viewer_id = feed["owner_parent_id"]
    parents = {p["id"]: p["name"] for p in conn.execute("SELECT id, name FROM parents")}
    kid_names_by_event: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT ek.event_id, k.name FROM event_kids ek JOIN kids k ON k.id = ek.kid_id "
        "ORDER BY k.name"
    ):
        kid_names_by_event.setdefault(row["event_id"], []).append(row["name"])
    allowed = viewer_allowed_parents(conn)

    vevents = []
    for ev in feed_events(conn, feed["kind"], feed["kid_id"], start, end):
        visible = event_visible_to(ev, viewer_id, allowed)
        vevents.append(
            event_vevent(ev, kid_names_by_event.get(ev["id"], []), visible)
        )

    # Custody blocks for every circle (never private). A kid-scoped feed only
    # includes that kid's circle.
    kid_circle = None
    if feed["kind"] == "kid" and feed["kid_id"]:
        row = conn.execute(
            "SELECT circle_id FROM kids WHERE id = ?", (feed["kid_id"],)
        ).fetchone()
        kid_circle = row["circle_id"] if row else None
    # Custody summaries read best with first names: "Emma & Ava with Dylan".
    kids_by_circle: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT circle_id, name FROM kids WHERE circle_id IS NOT NULL ORDER BY name"
    ):
        kids_by_circle.setdefault(row["circle_id"], []).append(row["name"].split()[0])
    for circle_id, schedule in custody.load_schedules(conn).items():
        if kid_circle is not None and circle_id != kid_circle:
            continue
        kid_label = " & ".join(kids_by_circle.get(circle_id, []))
        for block in custody.custody_blocks(conn, circle_id, start, end):
            pname = parents.get(block["parent_id"], "parent").split()[0]
            vevents.append(
                custody_vevent(circle_id, block, pname, kid_label,
                               schedule["handoff_time"])
            )

    return build_ics(feed["name"], vevents)
