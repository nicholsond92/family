from datetime import date

import pytest

from hub import custody, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    c.execute(
        "INSERT INTO parents(id, name) VALUES(1, 'Dylan'), (2, 'Sarah'), "
        "(3, 'Mark'), (4, 'Jess')"
    )
    c.execute("INSERT INTO circles(id, name) VALUES(10, 'Dylan & Sarah'), (20, 'Mark & Jess')")
    c.execute(
        "INSERT INTO circle_parents VALUES(10, 1), (10, 2), (20, 3), (20, 4)"
    )
    c.commit()
    yield c
    c.close()


def test_compile_alternating_weeks():
    cycle = custody.compile_cycle("alternating_weeks", 1, 2)
    assert cycle == [1] * 7 + [2] * 7


def test_compile_two_two_three():
    cycle = custody.compile_cycle("two_two_three", 1, 2)
    assert len(cycle) == 14
    assert cycle[:7] == [1, 1, 2, 2, 1, 1, 1]
    assert cycle[7:] == [2, 2, 1, 1, 2, 2, 2]


def test_compile_two_two_five_five():
    cycle = custody.compile_cycle("two_two_five_five", 1, 2)
    assert cycle == [1, 1, 2, 2, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2]


def test_custom_week_requires_seven_days():
    with pytest.raises(ValueError):
        custody.compile_cycle("custom_week", 1, 2, [1, 2])


def test_two_circles_are_independent(conn):
    anchor = date(2026, 8, 17)  # a Monday
    custody.save_schedule(conn, 10, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    custody.save_schedule(conn, 20, "two_two_three", anchor,
                          custody.compile_cycle("two_two_three", 3, 4))
    # Circle 10: week 1 belongs to Dylan.
    assert custody.custodian_on(conn, 10, date(2026, 8, 17)) == 1
    assert custody.custodian_on(conn, 10, date(2026, 8, 24)) == 2
    # Circle 20 on the same dates follows its own pattern.
    assert custody.custodian_on(conn, 20, date(2026, 8, 17)) == 3
    assert custody.custodian_on(conn, 20, date(2026, 8, 19)) == 4
    assert len(custody.load_schedules(conn)) == 2


def test_override_beats_pattern_and_scopes_to_circle(conn):
    anchor = date(2026, 8, 17)
    for cid, a, b in ((10, 1, 2), (20, 3, 4)):
        custody.save_schedule(conn, cid, "alternating_weeks", anchor,
                              custody.compile_cycle("alternating_weeks", a, b))
    conn.execute(
        "INSERT INTO custody_overrides(circle_id, date, parent_id) VALUES(10, '2026-08-18', 2)"
    )
    conn.commit()
    assert custody.custodian_on(conn, 10, date(2026, 8, 18)) == 2
    # The other circle is untouched by circle 10's override.
    assert custody.custodian_on(conn, 20, date(2026, 8, 18)) == 3
    blocks = custody.custody_blocks(conn, 10, date(2026, 8, 17), date(2026, 8, 19))
    assert [(b["parent_id"], b["has_override"]) for b in blocks] == [
        (1, False), (2, True), (1, False),
    ]


def test_apply_and_conflict_scoped_to_circle(conn):
    anchor = date(2026, 8, 17)
    for cid, a, b in ((10, 1, 2), (20, 3, 4)):
        custody.save_schedule(conn, cid, "alternating_weeks", anchor,
                              custody.compile_cycle("alternating_weeks", a, b))
    swap = {
        "id": 99, "circle_id": 10,
        "range1_start": "2026-08-24", "range1_end": "2026-08-26", "range1_parent": 1,
        "range2_start": None, "range2_end": None, "range2_parent": None,
    }
    custody.apply_swap_overrides(conn, swap)
    assert custody.custodian_on(conn, 10, date(2026, 8, 25)) == 1
    assert custody.custodian_on(conn, 20, date(2026, 8, 25)) == 4  # unaffected

    # Same dates, other circle: no conflict.
    other = {**swap, "id": 100, "circle_id": 20, "range1_parent": 3}
    assert custody.swap_conflicts(conn, other) == []
    # Same dates, same circle: conflict with swap 99.
    same = {**swap, "id": 101, "range1_parent": 2}
    assert custody.swap_conflicts(conn, same) == [
        date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
    ]
