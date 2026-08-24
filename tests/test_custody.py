from datetime import date

import pytest

from hub import custody, db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    c.execute("INSERT INTO parents(id, name) VALUES(1, 'Dylan'), (2, 'Alex')")
    c.commit()
    yield c
    c.close()


def test_compile_alternating_weeks():
    cycle = custody.compile_cycle("alternating_weeks", 1, 2)
    assert cycle == [1] * 7 + [2] * 7


def test_compile_two_two_three():
    cycle = custody.compile_cycle("two_two_three", 1, 2)
    assert len(cycle) == 14
    # Week 1: A Mon-Tue, B Wed-Thu, A Fri-Sun
    assert cycle[:7] == [1, 1, 2, 2, 1, 1, 1]
    # Week 2 mirrored
    assert cycle[7:] == [2, 2, 1, 1, 2, 2, 2]


def test_compile_two_two_five_five():
    cycle = custody.compile_cycle("two_two_five_five", 1, 2)
    assert cycle.count(1) == 7 and cycle.count(2) == 7
    assert cycle == [1, 1, 2, 2, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2]


def test_custom_week_requires_seven_days():
    with pytest.raises(ValueError):
        custody.compile_cycle("custom_week", 1, 2, [1, 2])


def test_custodian_and_blocks(conn):
    anchor = date(2026, 8, 17)  # a Monday
    custody.save_schedule(conn, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    assert custody.custodian_on(conn, date(2026, 8, 17)) == 1
    assert custody.custodian_on(conn, date(2026, 8, 24)) == 2
    assert custody.custodian_on(conn, date(2026, 8, 31)) == 1
    # Works before the anchor too.
    assert custody.custodian_on(conn, date(2026, 8, 10)) == 2

    blocks = custody.custody_blocks(conn, date(2026, 8, 17), date(2026, 8, 30))
    assert [(b["parent_id"], b["start"], b["end"]) for b in blocks] == [
        (1, date(2026, 8, 17), date(2026, 8, 23)),
        (2, date(2026, 8, 24), date(2026, 8, 30)),
    ]


def test_override_beats_pattern(conn):
    anchor = date(2026, 8, 17)
    custody.save_schedule(conn, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    conn.execute(
        "INSERT INTO custody_overrides(date, parent_id) VALUES('2026-08-18', 2)"
    )
    conn.commit()
    assert custody.custodian_on(conn, date(2026, 8, 18)) == 2
    blocks = custody.custody_blocks(conn, date(2026, 8, 17), date(2026, 8, 19))
    assert [(b["parent_id"], b["has_override"]) for b in blocks] == [
        (1, False), (2, True), (1, False),
    ]


def test_apply_swap_overrides(conn):
    anchor = date(2026, 8, 17)
    custody.save_schedule(conn, "alternating_weeks", anchor,
                          custody.compile_cycle("alternating_weeks", 1, 2))
    swap = {
        "id": 99,
        "range1_start": "2026-08-24",
        "range1_end": "2026-08-26",
        "range1_parent": 1,
        "range2_start": "2026-08-19",
        "range2_end": "2026-08-19",
        "range2_parent": 2,
    }
    custody.apply_swap_overrides(conn, swap)
    assert custody.custodian_on(conn, date(2026, 8, 24)) == 1
    assert custody.custodian_on(conn, date(2026, 8, 26)) == 1
    assert custody.custodian_on(conn, date(2026, 8, 27)) == 2  # outside the swap
    assert custody.custodian_on(conn, date(2026, 8, 19)) == 2
