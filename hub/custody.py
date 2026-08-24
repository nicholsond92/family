"""Custody schedule engine — one schedule per co-parenting circle.

A circle is a pair of co-parents (e.g. you + your ex). Each circle's schedule
is a repeating cycle: a list of parent ids, one per day, anchored at
``anchor_date`` (day 0). Named patterns (alternating weeks, 2-2-3, 2-2-5-5,
custom week) compile down to a cycle so lookups are uniform. Approved swap
requests write rows into ``custody_overrides`` for their circle, which take
precedence over the base cycle.
"""

import json
from datetime import date, timedelta

PATTERNS = {
    "alternating_weeks": "Alternating weeks (week on / week off)",
    "two_two_three": "2-2-3 rotation",
    "two_two_five_five": "2-2-5-5 rotation",
    "custom_week": "Custom weekly (pick a parent per weekday)",
}


def compile_cycle(pattern: str, parent_a: int, parent_b: int,
                  custom_days: list[int] | None = None) -> list[int]:
    """Compile a named pattern into a per-day cycle of parent ids.

    Day 0 of the cycle is the anchor date (a Monday). ``parent_a`` is the
    parent who has the kids on day 0.
    """
    a, b = parent_a, parent_b
    if pattern == "alternating_weeks":
        return [a] * 7 + [b] * 7
    if pattern == "two_two_three":
        # Week 1: A Mon-Tue, B Wed-Thu, A Fri-Sun; week 2 mirrored.
        return [a, a, b, b, a, a, a, b, b, a, a, b, b, b]
    if pattern == "two_two_five_five":
        # A 2, B 2, A 5, B 5.
        return [a, a, b, b, a, a, a, a, a, b, b, b, b, b]
    if pattern == "custom_week":
        if not custom_days or len(custom_days) != 7:
            raise ValueError("custom_week needs 7 weekday assignments (Mon-Sun)")
        return list(custom_days)
    raise ValueError(f"unknown custody pattern: {pattern}")


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def save_schedule(conn, circle_id: int, pattern: str, anchor: date,
                  cycle: list[int], handoff_time: str = "18:00") -> None:
    conn.execute(
        "INSERT INTO custody_schedule(circle_id, pattern, anchor_date, cycle, handoff_time) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(circle_id) DO UPDATE SET pattern = excluded.pattern, "
        "anchor_date = excluded.anchor_date, cycle = excluded.cycle, "
        "handoff_time = excluded.handoff_time",
        (circle_id, pattern, anchor.isoformat(), json.dumps(cycle), handoff_time),
    )
    conn.commit()


def _schedule_from_row(row):
    return {
        "circle_id": row["circle_id"],
        "pattern": row["pattern"],
        "anchor_date": date.fromisoformat(row["anchor_date"]),
        "cycle": json.loads(row["cycle"]),
        "handoff_time": row["handoff_time"],
    }


def load_schedule(conn, circle_id: int):
    row = conn.execute(
        "SELECT * FROM custody_schedule WHERE circle_id = ?", (circle_id,)
    ).fetchone()
    return _schedule_from_row(row) if row else None


def load_schedules(conn) -> dict[int, dict]:
    """All circles' schedules, keyed by circle id."""
    return {
        row["circle_id"]: _schedule_from_row(row)
        for row in conn.execute("SELECT * FROM custody_schedule")
    }


def base_custodian_on(schedule, d: date) -> int:
    cycle = schedule["cycle"]
    offset = (d - schedule["anchor_date"]).days % len(cycle)
    return cycle[offset]


def custodian_on(conn, circle_id: int, d: date, schedule=None) -> int | None:
    """Which parent has this circle's kids on date ``d`` (override-aware)."""
    row = conn.execute(
        "SELECT parent_id FROM custody_overrides WHERE circle_id = ? AND date = ?",
        (circle_id, d.isoformat()),
    ).fetchone()
    if row:
        return row["parent_id"]
    if schedule is None:
        schedule = load_schedule(conn, circle_id)
    if not schedule:
        return None
    return base_custodian_on(schedule, d)


def override_on(conn, circle_id: int, d: date):
    return conn.execute(
        "SELECT * FROM custody_overrides WHERE circle_id = ? AND date = ?",
        (circle_id, d.isoformat()),
    ).fetchone()


def custody_blocks(conn, circle_id: int, start: date, end: date):
    """Contiguous custody runs for one circle between start and end (incl.)."""
    schedule = load_schedule(conn, circle_id)
    if not schedule:
        return []
    overrides = {
        r["date"]: r["parent_id"]
        for r in conn.execute(
            "SELECT date, parent_id FROM custody_overrides "
            "WHERE circle_id = ? AND date BETWEEN ? AND ?",
            (circle_id, start.isoformat(), end.isoformat()),
        )
    }
    blocks = []
    d = start
    while d <= end:
        iso = d.isoformat()
        parent = overrides.get(iso, base_custodian_on(schedule, d))
        is_override = iso in overrides
        if blocks and blocks[-1]["parent_id"] == parent:
            blocks[-1]["end"] = d
            blocks[-1]["has_override"] = blocks[-1]["has_override"] or is_override
        else:
            blocks.append(
                {"parent_id": parent, "start": d, "end": d, "has_override": is_override}
            )
        d += timedelta(days=1)
    return blocks


def _swap_ranges(swap):
    ranges = [(swap["range1_start"], swap["range1_end"], swap["range1_parent"])]
    if swap["range2_start"] and swap["range2_end"] and swap["range2_parent"]:
        ranges.append((swap["range2_start"], swap["range2_end"], swap["range2_parent"]))
    return ranges


def swap_conflicts(conn, swap) -> list[date]:
    """Dates in this swap's ranges already owned by a different approved swap
    in the same circle."""
    conflicts = []
    for start_s, end_s, _parent in _swap_ranges(swap):
        d = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s)
        while d <= end:
            row = conn.execute(
                "SELECT swap_id FROM custody_overrides WHERE circle_id = ? AND date = ?",
                (swap["circle_id"], d.isoformat()),
            ).fetchone()
            if row and row["swap_id"] and row["swap_id"] != swap["id"]:
                conflicts.append(d)
            d += timedelta(days=1)
    return conflicts


def apply_swap_overrides(conn, swap) -> None:
    """Write custody overrides for an approved swap request."""
    for start_s, end_s, parent_id in _swap_ranges(swap):
        d = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s)
        while d <= end:
            conn.execute(
                "INSERT INTO custody_overrides(circle_id, date, parent_id, swap_id) "
                "VALUES(?, ?, ?, ?) "
                "ON CONFLICT(circle_id, date) DO UPDATE SET "
                "parent_id = excluded.parent_id, swap_id = excluded.swap_id",
                (swap["circle_id"], d.isoformat(), parent_id, swap["id"]),
            )
            d += timedelta(days=1)
    conn.commit()
