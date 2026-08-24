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
    unfolded = folded.replace("\r\n ", "")
    assert unfolded == line


@pytest.fixture
def conn(tmp_path):
    """Blended household: Dylan+Sarah (Emma), Mark+Jess (Leo)."""
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    c.execute(
        "INSERT INTO parents(id, name) VALUES(1, 'Dylan'), (2, 'Sarah'), "
        "(3, 'Mark'), (4, 'Jess')"
    )
    c.execute("INSERT INTO circles(id, name) VALUES(10, 'D&S'), (20, 'M&J')")
    c.execute("INSERT INTO circle_parents VALUES(10, 1), (10, 2), (20, 3), (20, 4)")
    c.execute(
        "INSERT INTO kids(id, name, color, circle_id) VALUES"
        "(1, 'Emma', '#e63946', 10), (2, 'Leo', '#457b9d', 20)"
    )
    c.commit()
    yield c
    c.close()


def _add_event(conn, event_id, title, kid_id, private=0, created_by=1):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO events(id, title, category, date, start_time, end_time, all_day, "
        "location, notes, private, created_by, created_at) "
        "VALUES(?, ?, 'activity', ?, '16:00', '17:30', 0, 'Park', 'note', ?, ?, '')",
        (event_id, title, tomorrow, private, created_by),
    )
    conn.execute("INSERT INTO event_kids VALUES(?, ?)", (event_id, kid_id))
    conn.commit()


def _feed(owner, kind="all", kid_id=None):
    return {"kind": kind, "kid_id": kid_id, "name": "Test", "owner_parent_id": owner}


def test_private_event_masked_for_other_circle(conn):
    # Sarah creates a private event for Emma (circle Dylan+Sarah).
    _add_event(conn, 1, "Therapy session", kid_id=1, private=1, created_by=2)

    # Dylan (Emma's co-parent) sees details.
    assert "Therapy session" in feeds.generate_feed(conn, _feed(1))
    # Sarah (creator) sees details.
    assert "Therapy session" in feeds.generate_feed(conn, _feed(2))
    # Mark (other circle) sees a Busy block with the kid but no details.
    mark = feeds.generate_feed(conn, _feed(3))
    assert "Therapy session" not in mark
    assert "Busy (Emma)" in mark
    assert "Park" not in mark and "note" not in mark
    # The busy block still reserves the time slot.
    assert "T160000" in mark
    # An ownerless feed (wall display / shared) masks it too.
    assert "Therapy session" not in feeds.generate_feed(conn, _feed(None))


def test_public_event_visible_to_everyone(conn):
    _add_event(conn, 1, "Soccer practice", kid_id=2, private=0, created_by=3)
    for viewer in (1, 2, 3, 4, None):
        assert "Soccer practice" in feeds.generate_feed(conn, _feed(viewer))


def test_custody_blocks_labeled_per_circle(conn):
    anchor = custody.monday_of(date.today())
    custody.save_schedule(conn, 10, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    custody.save_schedule(conn, 20, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 3, 4))
    ics = feeds.generate_feed(conn, _feed(1))
    assert "Emma with " in ics
    assert "Leo with " in ics


def test_kid_feed_scopes_events_and_custody(conn):
    _add_event(conn, 1, "Emma thing", kid_id=1)
    _add_event(conn, 2, "Leo thing", kid_id=2)
    anchor = custody.monday_of(date.today())
    custody.save_schedule(conn, 10, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    custody.save_schedule(conn, 20, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 3, 4))
    ics = feeds.generate_feed(conn, _feed(1, kind="kid", kid_id=1))
    assert "Emma thing" in ics
    assert "Leo thing" not in ics
    assert "Emma with " in ics
    assert "Leo with " not in ics


def test_custody_only_feed_has_no_events(conn):
    _add_event(conn, 1, "Emma thing", kid_id=1)
    anchor = custody.monday_of(date.today())
    custody.save_schedule(conn, 10, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    ics = feeds.generate_feed(conn, _feed(None, kind="custody"))
    assert "Emma thing" not in ics
    assert "Emma with " in ics
