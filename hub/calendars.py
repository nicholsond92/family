"""Subscribed calendars: pull events in from someone else's iCal feed.

A co-parent shares their Google/Outlook/iCloud calendar; an adult pastes its
iCal address in Settings. Every so often (lazily, on page loads, the same way
lunch menus refresh) the feed is fetched and its events are copied into the
`events` table, tagged with ``source_id`` so the next sync can replace them.
Copying keeps everything downstream — the calendar, the wall display, the
outgoing .ics feeds, privacy masking — working without special cases.

Feeds served by the Google Apps Script bridge (google-calendar-feed.gs) are
also writable: edits and deletes made in the hub are posted back to that
script, which applies them to the Google calendar as the adult who owns it.
"""

import json
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import quote, urlsplit

import requests

from . import db

SYNC_TTL_SECONDS = 30 * 60
FETCH_TIMEOUT = 15
# Window of occurrences copied in: recent past for context, a year ahead.
PAST_DAYS = 30
FUTURE_DAYS = 365
MAX_DAY_SPAN = 120


def normalize_url(raw: str) -> str | None:
    """An https URL for the feed, or None if it can't be one.

    Accepts a full iCal URL, a ``webcal://`` link, or a bare Google Calendar
    ID (``...@group.calendar.google.com``), which only works when that
    calendar is public — the "secret address in iCal format" works always.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("webcal://"):
        raw = "https://" + raw[len("webcal://"):]
    if "://" not in raw:
        if "@" in raw and " " not in raw:
            return ("https://calendar.google.com/calendar/ical/"
                    f"{quote(raw, safe='')}/public/basic.ics")
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return raw


def fetch(url: str) -> bytes:
    resp = requests.get(url, timeout=FETCH_TIMEOUT,
                        headers={"User-Agent": "FamilyHub/1.0 (calendar sync)"})
    if resp.status_code == 404:
        raise ValueError("calendar not found (404) — if it's a Google calendar, "
                         "use its secret address in iCal format")
    if resp.status_code in (401, 403):
        raise ValueError(f"access denied ({resp.status_code}) — the calendar "
                         "isn't public; use its secret iCal address")
    resp.raise_for_status()
    if b"BEGIN:VCALENDAR" not in resp.content[:2048]:
        raise ValueError("that link didn't return an iCal calendar")
    return resp.content


def _local(dt: datetime, tz) -> datetime:
    if dt.tzinfo is None:
        return dt  # floating time: already "wall clock"
    return dt.astimezone(tz) if tz else dt.astimezone()


def _text(component, key: str) -> str:
    value = component.get(key)
    return str(value).strip() if value is not None else ""


def parse(ics: bytes, start: date, end: date, tz=None) -> list[dict]:
    """Occurrences between start and end (inclusive) as hub event rows.

    Recurring events are expanded; multi-day all-day events become one
    all-day row per day, like the CSV importer does.
    """
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    out = []
    occurrences = recurring_ical_events.of(cal).between(
        start, end + timedelta(days=1))
    for ev in occurrences:
        if _text(ev, "STATUS").upper() == "CANCELLED":
            continue
        title = _text(ev, "SUMMARY") or "Busy"
        base = {
            "uid": _text(ev, "UID"),
            "title": title[:200],
            "location": _text(ev, "LOCATION")[:300],
            "notes": _text(ev, "DESCRIPTION")[:2000],
        }
        dtstart = ev.get("DTSTART").dt
        dtend = ev.get("DTEND").dt if ev.get("DTEND") else None
        if not isinstance(dtstart, datetime):
            first = dtstart
            last = (dtend - timedelta(days=1)
                    if isinstance(dtend, date) and not isinstance(dtend, datetime)
                    and dtend > dtstart else dtstart)
            if (last - first).days > MAX_DAY_SPAN:
                last = first + timedelta(days=MAX_DAY_SPAN)
            d = first
            while d <= last:
                if start <= d <= end:
                    out.append({**base, "date": d.isoformat(), "all_day": 1,
                                "last_date": d.isoformat(),
                                "start_time": None, "end_time": None})
                d += timedelta(days=1)
            continue
        s = _local(dtstart, tz)
        e = _local(dtend, tz) if isinstance(dtend, datetime) else None
        if not start <= s.date() <= end:
            continue
        # Last day a multi-day stay covers: ending before noon means the
        # kids left that morning, so that day isn't counted.
        last = s.date()
        if e and e.date() > s.date():
            last = e.date() if e.time() >= time(12) else e.date() - timedelta(days=1)
            last = max(last, s.date())
        out.append({
            **base,
            "date": s.date().isoformat(),
            "last_date": last.isoformat(),
            "all_day": 0,
            "start_time": s.strftime("%H:%M"),
            # Overnight events keep their start day; the end time would lie.
            "end_time": e.strftime("%H:%M") if e and e.date() == s.date() else None,
        })
    return out


def subscriptions(conn):
    return conn.execute(
        "SELECT * FROM calendar_subscriptions ORDER BY id").fetchall()


def kid_ids_of(sub) -> list[int]:
    return [int(k) for k in (sub["kid_ids"] or "").split(",") if k.strip()]


def sync(conn, sub, tz=None, today: date | None = None,
         fetcher=None) -> int:
    """Replace this subscription's events with a fresh copy of its feed.

    On failure the previous copy is kept and the error recorded, so a flaky
    feed never blanks the calendar. Returns the number of events stored.
    """
    today = today or (datetime.now(tz).date() if tz else date.today())
    now = datetime.utcnow().isoformat(timespec="seconds")
    try:
        rows = parse((fetcher or fetch)(sub["url"]),
                     today - timedelta(days=PAST_DAYS),
                     today + timedelta(days=FUTURE_DAYS), tz)
    except Exception as exc:  # noqa: BLE001 — any feed problem is reported, not raised
        conn.execute(
            "UPDATE calendar_subscriptions SET last_synced = ?, last_error = ? "
            "WHERE id = ?", (now, str(exc)[:300] or exc.__class__.__name__,
                             sub["id"]))
        conn.commit()
        return -1
    # Removed while the feed was downloading: don't resurrect its events.
    if not conn.execute("SELECT id FROM calendar_subscriptions WHERE id = ?",
                        (sub["id"],)).fetchone():
        return 0
    if sub["custody_circle_id"]:
        return _sync_custody(conn, sub, rows, now)
    conn.execute("DELETE FROM events WHERE source_id = ?", (sub["id"],))
    # kid_ids is a plain list, so skip kids removed since it was saved.
    existing = {r["id"] for r in conn.execute("SELECT id FROM kids")}
    kid_ids = [k for k in kid_ids_of(sub) if k in existing]
    for r in rows:
        event_id = db.insert_id(
            conn,
            "INSERT INTO events(title, category, date, start_time, end_time, "
            "all_day, location, notes, private, series_id, created_by, "
            "created_at, source_id, source_uid) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (r["title"], sub["category"], r["date"], r["start_time"],
             r["end_time"], r["all_day"], r["location"], r["notes"],
             sub["private"], sub["created_by"], now, sub["id"], r["uid"]),
        )
        for kid_id in kid_ids:
            conn.execute(
                "INSERT INTO event_kids(event_id, kid_id) VALUES(?, ?)",
                (event_id, kid_id))
    conn.execute(
        "UPDATE calendar_subscriptions SET last_synced = ?, last_error = NULL, "
        "last_count = ?, last_note = NULL WHERE id = ?",
        (now, len(rows), sub["id"]))
    conn.commit()
    return len(rows)


def _due(sub) -> bool:
    if not sub["last_synced"]:
        return True
    try:
        last = datetime.fromisoformat(sub["last_synced"])
    except ValueError:
        return True
    return (datetime.utcnow() - last).total_seconds() >= SYNC_TTL_SECONDS


def refresh_due(conn, tz=None, today: date | None = None) -> None:
    """Sync any subscription whose copy is older than the TTL."""
    try:
        subs = subscriptions(conn)
    except Exception:  # noqa: BLE001 — never break a page over calendar sync
        return
    for sub in subs:
        if _due(sub):
            sync(conn, sub, tz, today)


def writable(sub) -> bool:
    """True for feeds from the Apps Script bridge, which accepts edits."""
    return bool(sub) and "script.google.com/macros/" in (sub["url"] or "")


def push_change(sub, action: str, uid: str, fields: dict | None = None,
                tz=None, poster=None) -> str:
    """Apply an edit ("update") or delete ("delete") to the source calendar.

    Returns the event's new UID (it changes when the start time does).
    Raises ValueError with a readable message when the bridge refuses.
    """
    payload = {"action": action, "uid": uid}
    if fields is not None:
        payload.update({
            "title": fields["title"],
            "location": fields["location"],
            "notes": fields["notes"],
            "all_day": bool(fields["all_day"]),
            "date": fields["date"],
        })
        if not fields["all_day"]:
            day = date.fromisoformat(fields["date"])
            start_t = fields["start_time"] or "00:00"
            start = datetime.combine(
                day, datetime.strptime(start_t, "%H:%M").time())
            end = (datetime.combine(
                day, datetime.strptime(fields["end_time"], "%H:%M").time())
                if fields["end_time"] else start + timedelta(hours=1))
            if end <= start:
                end += timedelta(days=1)
            if tz:
                start, end = start.replace(tzinfo=tz), end.replace(tzinfo=tz)
            else:
                start, end = start.astimezone(), end.astimezone()
            payload["start"] = start.isoformat()
            payload["end"] = end.isoformat()
    post = poster or (lambda url, body: requests.post(
        url, json=body, timeout=FETCH_TIMEOUT).json())
    try:
        result = post(sub["url"], payload)
    except Exception as exc:  # noqa: BLE001 — surfaced to the user
        raise ValueError(f"couldn't reach Google ({exc.__class__.__name__}) — "
                         "is the calendar script updated and redeployed?")
    if not isinstance(result, dict) or not result.get("ok"):
        err = result.get("error") if isinstance(result, dict) else None
        raise ValueError(err or "Google rejected the change")
    return result.get("uid") or uid


def custody_keywords(sub, members) -> dict[int, list[str]]:
    """parent id -> lowercase words that mark an event as that adult's time.
    Defaults to each adult's first name."""
    try:
        saved = json.loads(sub["custody_keywords"] or "{}")
    except ValueError:
        saved = {}
    out = {}
    for p in members:
        raw = saved.get(str(p["id"])) or p["name"].split()[0]
        out[p["id"]] = [w.strip().lower() for w in raw.split(",") if w.strip()]
    return out


def match_parent(title: str, keywords: dict[int, list[str]]) -> int | None:
    """The one adult whose keyword appears in the title, else None."""
    text = title.lower()
    hits = {pid for pid, words in keywords.items()
            if any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)}
    return hits.pop() if len(hits) == 1 else None


def _sync_custody(conn, sub, rows, now) -> int:
    """Turn a custody calendar's events into custody days for its circle.
    Days the calendar doesn't cover follow the regular schedule; approved
    swaps keep precedence."""
    circle_id = sub["custody_circle_id"]
    members = [r for r in conn.execute(
        "SELECT p.* FROM parents p JOIN circle_parents cp ON cp.parent_id = p.id "
        "WHERE cp.circle_id = ? ORDER BY p.id", (circle_id,))]
    keywords = custody_keywords(sub, members)
    days: dict[str, tuple[int, str]] = {}
    unmatched: list[str] = []
    for r in rows:
        parent_id = match_parent(r["title"], keywords)
        if parent_id is None:
            if r["title"] not in unmatched:
                unmatched.append(r["title"])
            continue
        d = date.fromisoformat(r["date"])
        last = date.fromisoformat(r.get("last_date") or r["date"])
        while d <= last:
            days[d.isoformat()] = (parent_id, r["title"][:200])
            d += timedelta(days=1)
    conn.execute("DELETE FROM events WHERE source_id = ?", (sub["id"],))
    conn.execute("DELETE FROM custody_overrides WHERE source_id = ?", (sub["id"],))
    for iso, (parent_id, title) in sorted(days.items()):
        conn.execute(
            "INSERT INTO custody_overrides(circle_id, date, parent_id, swap_id, "
            "note, source_id) VALUES(?, ?, ?, NULL, ?, ?) "
            "ON CONFLICT(circle_id, date) DO NOTHING",
            (circle_id, iso, parent_id, title, sub["id"]))
    note = None
    if unmatched:
        shown = ", ".join(f"“{t}”" for t in unmatched[:3])
        more = f" and {len(unmatched) - 3} more" if len(unmatched) > 3 else ""
        note = f"Couldn't tell whose time these are: {shown}{more}"
    conn.execute(
        "UPDATE calendar_subscriptions SET last_synced = ?, last_error = NULL, "
        "last_count = ?, last_note = ? WHERE id = ?",
        (now, len(days), note, sub["id"]))
    conn.commit()
    db.invalidate_memo(conn)
    return len(days)
