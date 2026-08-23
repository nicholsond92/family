from datetime import date, timedelta

import pytest

from hub import custody, db, feeds


def test_ics_escape():
    assert feeds.ics_escape("a,b;c\nd\\e") == "a\\,b\\;c\\nd\\\\e"


def test_fold_line_short_unchanged():
    assert feeds.fold_line("SUMMARY:short") == "SUMMARY:short"


def test_fold_line_long():
    line = "DESCRIPTION:" + "x" * 200
    folded = feeds.fold_line(line)
    parts = folded.split("\r\n")
    assert len(parts) > 1
    for part in parts:
        assert len(part.encode()) <= 75
    for part in parts[1:]:
        assert part.startswith(" ")
    assert "".join([parts[0]] + [p[1:] for p in parts[1:]]) == line


def test_fold_line_multibyte_not_split():
    line = "SUMMARY:" + "é" * 100
    folded = feeds.fold_line(line)
    # Every part must decode cleanly on its own (no split multi-byte chars).
    unfolded = folded.replace("\r\n ", "")
    assert unfolded == line


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    c.execute("INSERT INTO parents(id, name) VALUES(1, 'Dylan'), (2, 'Alex')")
    c.execute("INSERT INTO kids(id, name, color) VALUES(1, 'Sam', '#e63946')")
    c.commit()
    yield c
    c.close()


def test_generate_feed_contains_event_and_custody(conn):
    anchor = custody.monday_of(date.today())
    custody.save_schedule(conn, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO events(id, title, category, date, start_time, end_time, all_day, "
        "location, notes, created_at) "
        "VALUES(1, 'Soccer practice', 'activity', ?, '16:00', '17:30', 0, 'Park', '', '')",
        (tomorrow,),
    )
    conn.execute("INSERT INTO event_kids(event_id, kid_id) VALUES(1, 1)")
    conn.commit()

    feed = {"kind": "all", "kid_id": None, "name": "Everything"}
    ics = feeds.generate_feed(conn, feed)
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.rstrip().endswith("END:VCALENDAR")
    assert "Soccer practice (Sam)" in ics
    assert "Kids with Dylan" in ics or "Kids with Alex" in ics
    assert "UID:event-1@family-hub" in ics


def test_custody_only_feed_has_no_events(conn):
    anchor = custody.monday_of(date.today())
    custody.save_schedule(conn, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO events(id, title, category, date, all_day, created_at) "
        "VALUES(1, 'Soccer practice', 'activity', ?, 1, '')",
        (tomorrow,),
    )
    conn.commit()
    ics = feeds.generate_feed(conn, {"kind": "custody", "kid_id": None, "name": "Custody"})
    assert "Soccer practice" not in ics
    assert "Kids with" in ics


def test_kid_feed_filters_other_kids(conn):
    conn.execute("INSERT INTO kids(id, name, color) VALUES(2, 'Riley', '#457b9d')")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO events(id, title, category, date, all_day, created_at) "
        "VALUES(1, 'Sam thing', 'school', ?, 1, ''), (2, 'Riley thing', 'school', ?, 1, '')",
        (tomorrow, tomorrow),
    )
    conn.execute("INSERT INTO event_kids VALUES(1, 1), (2, 2)")
    conn.commit()
    ics = feeds.generate_feed(conn, {"kind": "kid", "kid_id": 1, "name": "Sam"})
    assert "Sam thing" in ics
    assert "Riley thing" not in ics
