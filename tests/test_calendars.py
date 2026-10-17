"""Subscribed (incoming) iCal calendars."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from hub import calendars, db
from test_app import client, do_setup, env  # noqa: F401 — fixtures

TODAY = date.today()


def _ics(today=TODAY):
    d = today + timedelta(days=3)
    nxt = d + timedelta(days=2)
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:pickup@test
DTSTART:{d:%Y%m%d}T220000Z
DTEND:{d:%Y%m%d}T230000Z
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Pickup at Garrett's
LOCATION:123 Main St
END:VEVENT
BEGIN:VEVENT
UID:trip@test
DTSTART;VALUE=DATE:{d:%Y%m%d}
DTEND;VALUE=DATE:{nxt:%Y%m%d}
SUMMARY:Camping trip
END:VEVENT
BEGIN:VEVENT
UID:gone@test
DTSTART;VALUE=DATE:{d:%Y%m%d}
SUMMARY:Cancelled thing
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
""".encode()


def test_normalize_url():
    gid = "abc123@group.calendar.google.com"
    assert calendars.normalize_url(gid) == (
        "https://calendar.google.com/calendar/ical/"
        "abc123%40group.calendar.google.com/public/basic.ics")
    assert calendars.normalize_url("webcal://p01.icloud.com/x.ics") == (
        "https://p01.icloud.com/x.ics")
    assert calendars.normalize_url("https://a.b/c.ics") == "https://a.b/c.ics"
    assert calendars.normalize_url("not a link") is None
    assert calendars.normalize_url("ftp://a.b/c") is None


def test_parse_expands_recurrence_and_multiday():
    rows = calendars.parse(_ics(), TODAY, TODAY + timedelta(days=60),
                           ZoneInfo("America/Chicago"))
    pickups = [r for r in rows if r["title"] == "Pickup at Garrett's"]
    assert len(pickups) == 3
    # 22:00Z is 16:00 or 17:00 in Chicago depending on DST.
    assert pickups[0]["start_time"] in ("16:00", "17:00")
    assert pickups[0]["all_day"] == 0
    assert pickups[0]["location"] == "123 Main St"
    trip = sorted(r["date"] for r in rows if r["title"] == "Camping trip")
    assert trip == [(TODAY + timedelta(days=3)).isoformat(),
                    (TODAY + timedelta(days=4)).isoformat()]
    assert not any(r["title"] == "Cancelled thing" for r in rows)


def test_subscribe_sync_and_remove(env, client, monkeypatch):  # noqa: F811
    do_setup(client)
    feed = {"body": _ics()}
    monkeypatch.setattr(calendars, "fetch", lambda url: feed["body"])
    conn = db.connect()
    leo = conn.execute("SELECT id FROM kids WHERE name = 'Leo'").fetchone()["id"]

    r = client.post("/settings/calendars/add", data={
        "name": "Garrett", "url": "https://example.com/g.ics",
        "category": "activity", "kid_ids": [str(leo)]})
    assert r.status_code == 200
    assert "5 event(s) synced" in r.text
    evs = conn.execute("SELECT * FROM events WHERE source_id IS NOT NULL").fetchall()
    assert len(evs) == 5
    assert {e["category"] for e in evs} == {"activity"}
    kid_links = conn.execute(
        "SELECT DISTINCT kid_id FROM event_kids WHERE event_id = ?",
        (evs[0]["id"],)).fetchall()
    assert [k["kid_id"] for k in kid_links] == [leo]

    # The events reach the week view.
    week = (TODAY + timedelta(days=3))
    r = client.get(f"/calendar?start={week.isoformat()}")
    assert "Camping trip" in r.text

    # A re-sync replaces rather than duplicates; a broken feed keeps the copy.
    sub_id = conn.execute("SELECT id FROM calendar_subscriptions").fetchone()["id"]
    client.post(f"/settings/calendars/{sub_id}/sync")
    assert conn.execute("SELECT COUNT(*) AS n FROM events "
                        "WHERE source_id = ?", (sub_id,)).fetchone()["n"] == 5
    feed["body"] = b"<html>nope</html>"

    def broken(url):
        raise ValueError("access denied (403)")
    monkeypatch.setattr(calendars, "fetch", broken)
    r = client.post(f"/settings/calendars/{sub_id}/sync")
    assert "Last sync failed: access denied" in r.text
    assert conn.execute("SELECT COUNT(*) AS n FROM events "
                        "WHERE source_id = ?", (sub_id,)).fetchone()["n"] == 5

    client.post(f"/settings/calendars/{sub_id}/delete")
    assert conn.execute("SELECT COUNT(*) AS n FROM events "
                        "WHERE source_id IS NOT NULL").fetchone()["n"] == 0
    conn.close()


def test_bad_url_rejected(env, client):  # noqa: F811
    do_setup(client)
    r = client.post("/settings/calendars/add",
                    data={"name": "x", "url": "hello there"})
    assert "doesn't look like a calendar link" in r.text


def test_sync_skips_removed_kids_and_subscriptions(env, client):  # noqa: F811
    do_setup(client)
    conn = db.connect()
    leo = conn.execute("SELECT id FROM kids WHERE name = 'Leo'").fetchone()["id"]
    sub_id = db.insert_id(
        conn, "INSERT INTO calendar_subscriptions(name, url, kid_ids) "
        "VALUES('G', 'https://x/y.ics', ?)", (f"{leo},9999",))
    conn.commit()
    sub = conn.execute("SELECT * FROM calendar_subscriptions WHERE id = ?",
                       (sub_id,)).fetchone()
    assert calendars.sync(conn, sub, fetcher=lambda url: _ics()) == 5
    assert {r["kid_id"] for r in conn.execute(
        "SELECT kid_id FROM event_kids")} == {leo}

    # Deleted mid-fetch: nothing comes back.
    def fetch_then_delete(url):
        conn.execute("DELETE FROM events WHERE source_id = ?", (sub_id,))
        conn.execute("DELETE FROM calendar_subscriptions WHERE id = ?", (sub_id,))
        conn.commit()
        return _ics()
    calendars.sync(conn, sub, fetcher=fetch_then_delete)
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE source_id = ?",
                        (sub_id,)).fetchone()["n"] == 0
    conn.close()


def test_edits_and_deletes_push_back_to_bridge(env, client, monkeypatch):  # noqa: F811
    do_setup(client)
    monkeypatch.setattr(calendars, "fetch", lambda url: _ics())
    sent = []

    def fake_push(sub, action, uid, fields=None, tz=None, poster=None):
        sent.append((action, uid, fields and fields["title"]))
        if fields and fields["title"] == "boom":
            raise ValueError("that event is no longer on the Google calendar")
        return "new-uid"
    monkeypatch.setattr(calendars, "push_change", fake_push)
    client.post("/settings/calendars/add", data={
        "name": "Bridge",
        "url": "https://script.google.com/macros/s/X/exec?key=k&id=c"})
    conn = db.connect()
    ev = conn.execute("SELECT * FROM events WHERE title = 'Camping trip' "
                      "ORDER BY date").fetchone()
    assert ev["source_uid"] == "trip@test"
    r = client.get(f"/events/{ev['id']}/edit")
    assert "saved back to Google Calendar" in r.text

    form = {"title": "Camping trip!", "date": ev["date"], "all_day": "on"}
    client.post(f"/events/{ev['id']}/edit", data=form)
    assert sent[-1] == ("update", "trip@test", "Camping trip!")
    row = conn.execute("SELECT title, source_uid FROM events WHERE id = ?",
                       (ev["id"],)).fetchone()
    assert (row["title"], row["source_uid"]) == ("Camping trip!", "new-uid")

    # A refused change leaves the hub untouched.
    r = client.post(f"/events/{ev['id']}/edit", data={**form, "title": "boom"})
    assert r.status_code == 502
    assert conn.execute("SELECT title FROM events WHERE id = ?",
                        (ev["id"],)).fetchone()["title"] == "Camping trip!"

    client.post(f"/events/{ev['id']}/delete")
    assert sent[-1][0] == "delete"
    assert not conn.execute("SELECT id FROM events WHERE id = ?",
                            (ev["id"],)).fetchone()
    conn.close()


def test_push_change_payload():
    sub = {"url": "https://script.google.com/macros/s/X/exec?key=k&id=c"}
    seen = {}

    def poster(url, body):
        seen.update(body)
        return {"ok": True, "uid": "u2"}
    uid = calendars.push_change(sub, "update", "u1", {
        "title": "Game", "location": "", "notes": "", "all_day": 0,
        "date": "2026-10-20", "start_time": "18:00", "end_time": "20:00"},
        ZoneInfo("America/Chicago"), poster)
    assert uid == "u2"
    assert seen["start"] == "2026-10-20T18:00:00-05:00"
    assert seen["end"] == "2026-10-20T20:00:00-05:00"
    try:
        calendars.push_change(sub, "delete", "u1",
                              poster=lambda u, b: {"ok": False, "error": "nope"})
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert str(exc) == "nope"
