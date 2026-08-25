"""End-to-end flows for the blended-household model.

Household: Dylan + Sarah co-parent Emma & Ava (circle 1); Dylan's partner
Mark + his ex Jess co-parent Leo & Max (circle 2). All four adults share one
hub; private events are masked outside the kid's circle.

Runs against SQLite by default. To run against Postgres (validating the
Supabase/Vercel path), point HUB_DATABASE_URL at a throwaway database —
its public schema is DROPPED before each test:

    HUB_DATABASE_URL=postgresql://user@host:port/db pytest tests/test_app.py
"""

import os
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from hub import db
from hub.app import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = os.environ.get("HUB_DATABASE_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        import psycopg

        with psycopg.connect(url) as c:
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            c.commit()
        db._initialized_targets.discard(url)
    else:
        monkeypatch.setenv("HUB_DB", str(tmp_path / "app.db"))
    app = create_app()
    return app


@pytest.fixture
def client(env):
    return TestClient(env, follow_redirects=True)


def monday():
    return (date.today() - timedelta(days=date.today().weekday())).isoformat()


def do_setup(client):
    r = client.post("/setup", data={
        "household_name": "Blended Testers",
        "adult1_name": "Dylan",
        "adult1_email": "dylan@example.com",
        "adult1_password": "pass1234",
        "adult2_name": "Sarah",
        "adult3_name": "Mark",
        "adult4_name": "Jess",
        "kid1_name": "Emma", "kid1_circle": "1",
        "kid2_name": "Ava", "kid2_circle": "1",
        "kid3_name": "Leo", "kid3_circle": "2",
        "kid4_name": "Max", "kid4_circle": "2",
        "pattern1": "alternating_weeks",
        "first_parent1": "1",
        "anchor_date1": monday(),
        "handoff_time1": "18:00",
        "pattern2": "two_two_three",
        "first_parent2": "1",
        "anchor_date2": monday(),
        "handoff_time2": "17:00",
    })
    assert r.status_code == 200
    return monday()


def join(env, conn, name, password):
    invite = conn.execute(
        "SELECT invite_token FROM parents WHERE name = ?", (name,)
    ).fetchone()["invite_token"]
    assert invite
    c = TestClient(env, follow_redirects=True)
    r = c.post(f"/invite/{invite}", data={"password": password})
    assert r.status_code == 200
    return c


def parent_id(conn, name):
    return conn.execute("SELECT id FROM parents WHERE name = ?", (name,)).fetchone()["id"]


def test_root_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_setup_creates_blended_household(env, client):
    do_setup(client)
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM parents").fetchone()["n"] == 4
    assert conn.execute("SELECT COUNT(*) AS n FROM circles").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM kids").fetchone()["n"] == 4
    assert conn.execute("SELECT COUNT(*) AS n FROM custody_schedule").fetchone()["n"] == 2
    # One personal feed per adult + a shared custody feed.
    rows = conn.execute("SELECT kind, owner_parent_id FROM feeds").fetchall()
    assert sum(1 for r in rows if r["kind"] == "all" and r["owner_parent_id"]) == 4
    assert sum(1 for r in rows if r["kind"] == "custody") == 1
    # Calendar shows both circles' custody chips.
    r = client.get("/calendar")
    assert "Ava &amp; Emma" in r.text
    assert "Leo &amp; Max" in r.text
    conn.close()


def test_private_event_masked_outside_circle(env, client):
    do_setup(client)
    conn = db.connect()
    mark = join(env, conn, "Mark", "markpass")

    when = (date.today() + timedelta(days=1)).isoformat()
    client.post("/events/new", data={
        "title": "Emma therapy",
        "category": "medical",
        "date": when,
        "start_time": "15:00",
        "private": "on",
        "kid_ids": [str(conn.execute("SELECT id FROM kids WHERE name='Emma'").fetchone()["id"])],
    })
    event_id = conn.execute("SELECT id FROM events").fetchone()["id"]

    # Dylan (creator + co-parent) sees the details on the calendar.
    assert "Emma therapy" in client.get(f"/calendar?start={when}").text
    # Mark sees a Busy block, not the details, and can't open the event.
    mark_cal = mark.get(f"/calendar?start={when}").text
    assert "Emma therapy" not in mark_cal
    assert "Busy" in mark_cal
    assert mark.get(f"/events/{event_id}/edit").status_code == 404
    # Sarah (Emma's other co-parent) has full access.
    sarah = join(env, conn, "Sarah", "sarahpass")
    assert "Emma therapy" in sarah.get(f"/calendar?start={when}").text
    assert sarah.get(f"/events/{event_id}/edit").status_code == 200

    # Personal feeds enforce the same rule.
    def feed_token(name):
        return conn.execute(
            "SELECT token FROM feeds WHERE kind='all' AND owner_parent_id = ?",
            (parent_id(conn, name),),
        ).fetchone()["token"]
    assert "Emma therapy" in client.get(f"/ics/{feed_token('Dylan')}.ics").text
    mark_ics = mark.get(f"/ics/{feed_token('Mark')}.ics").text
    assert "Emma therapy" not in mark_ics
    assert "Busy (Emma)" in mark_ics

    # The shared wall display always masks private events.
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert "Emma therapy" not in display
    assert "Busy" in display
    conn.close()


def test_swaps_scoped_to_circle(env, client):
    do_setup(client)
    conn = db.connect()
    mark = join(env, conn, "Mark", "markpass")
    sarah = join(env, conn, "Sarah", "sarahpass")

    circle1 = conn.execute(
        "SELECT circle_id FROM circle_parents WHERE parent_id = ?",
        (parent_id(conn, "Dylan"),),
    ).fetchone()["circle_id"]
    start = (date.today() + timedelta(days=10)).isoformat()
    end = (date.today() + timedelta(days=12)).isoformat()

    # Mark isn't in circle 1 and can't request swaps there.
    r = mark.post("/swaps/new", data={
        "circle_id": str(circle1), "range1_parent": str(parent_id(conn, "Dylan")),
        "range1_start": start, "range1_end": end,
    })
    assert r.status_code == 403

    # Dylan requests a swap in his circle.
    r = client.post("/swaps/new", data={
        "circle_id": str(circle1), "range1_parent": str(parent_id(conn, "Dylan")),
        "range1_start": start, "range1_end": end,
        "reason": "Lake weekend",
    })
    assert "pending" in r.text
    swap_id = conn.execute("SELECT id FROM swaps").fetchone()["id"]

    # Mark (not a circle member) can't decide it; Dylan can't approve his own.
    mark.post(f"/swaps/{swap_id}/decide", data={"decision": "approved"})
    client.post(f"/swaps/{swap_id}/decide", data={"decision": "approved"})
    assert conn.execute("SELECT status FROM swaps WHERE id = ?", (swap_id,)) \
        .fetchone()["status"] == "pending"
    # Mark sees the swap facts but not the co-parents' discussion.
    detail = mark.get(f"/swaps/{swap_id}").text
    assert "Lake weekend" not in detail

    # Sarah approves; overrides land in circle 1 only.
    r = sarah.post(f"/swaps/{swap_id}/decide", data={"decision": "approved"})
    assert "approved" in r.text
    rows = conn.execute("SELECT circle_id, parent_id FROM custody_overrides").fetchall()
    assert len(rows) == 3
    assert all(row["circle_id"] == circle1 for row in rows)
    conn.close()


def test_circle_thread_hidden_from_other_circle(env, client):
    do_setup(client)
    conn = db.connect()
    mark = join(env, conn, "Mark", "markpass")
    circle1 = conn.execute(
        "SELECT circle_id FROM circle_parents WHERE parent_id = ?",
        (parent_id(conn, "Dylan"),),
    ).fetchone()["circle_id"]

    # Dylan starts a circle-only thread and a household thread.
    client.post("/messages/new", data={
        "subject": "Sarah only: pickup change", "circle_id": str(circle1),
        "body": "Can we move Thursday?",
    })
    client.post("/messages/new", data={
        "subject": "Grocery run", "circle_id": "", "body": "Anyone need anything?",
    })
    private_thread = conn.execute(
        "SELECT id FROM threads WHERE circle_id IS NOT NULL AND swap_id IS NULL"
    ).fetchone()["id"]

    mark_list = mark.get("/messages").text
    assert "Grocery run" in mark_list
    assert "Sarah only" not in mark_list
    assert mark.get(f"/messages/{private_thread}").status_code == 404
    # Mark can't post into circle 1 either.
    r = mark.post("/messages/new", data={
        "subject": "sneak", "circle_id": str(circle1), "body": "hi",
    })
    assert r.status_code == 403
    conn.close()


def test_feeds_page_shows_only_own_tokens(env, client):
    do_setup(client)
    conn = db.connect()
    mark = join(env, conn, "Mark", "markpass")
    dylan_token = conn.execute(
        "SELECT token FROM feeds WHERE kind='all' AND owner_parent_id = ?",
        (parent_id(conn, "Dylan"),),
    ).fetchone()["token"]
    mark_page = mark.get("/feeds").text
    assert dylan_token not in mark_page
    conn.close()


def test_login_checks_all_rows_with_same_name(env, client):
    do_setup(client)
    conn = db.connect()
    join(env, conn, "Sarah", "sarahpass")
    conn.execute(
        "UPDATE parents SET name = 'Sam Parent' WHERE name IN ('Dylan', 'Sarah')"
    )
    conn.commit()
    for password in ("pass1234", "sarahpass"):
        fresh = TestClient(env, follow_redirects=True)
        r = fresh.post("/login", data={"name": "Sam Parent", "password": password})
        # Login lands on the board (the display is the app's front door).
        assert 'id="view-calendar"' in r.text
    assert "Wrong name" in TestClient(env, follow_redirects=True).post(
        "/login", data={"name": "Sam Parent", "password": "nope"}
    ).text
    conn.close()


def test_invite_cannot_take_over_joined_account(env, client):
    do_setup(client)
    conn = db.connect()
    join(env, conn, "Sarah", "sarahpass")
    sarah_id = parent_id(conn, "Sarah")

    client.post(f"/settings/parents/{sarah_id}/invite")
    assert conn.execute(
        "SELECT invite_token FROM parents WHERE id = ?", (sarah_id,)
    ).fetchone()["invite_token"] is None

    conn.execute(
        "UPDATE parents SET invite_token = 'stale-token' WHERE id = ?", (sarah_id,)
    )
    conn.commit()
    r = client.post("/invite/stale-token", data={"password": "hijacked"},
                    follow_redirects=False)
    assert r.status_code == 404
    conn.close()


def test_custody_settings_admin_and_scoping(env, client):
    do_setup(client)
    conn = db.connect()
    circle2 = conn.execute(
        "SELECT circle_id FROM circle_parents WHERE parent_id = ?",
        (parent_id(conn, "Mark"),),
    ).fetchone()["circle_id"]
    # Sarah is neither a circle-2 co-parent nor the admin — still forbidden.
    sarah = join(env, conn, "Sarah", "sarahpass")
    r = sarah.post(f"/settings/custody/{circle2}", data={
        "pattern": "alternating_weeks",
        "first_parent": str(parent_id(conn, "Mark")),
        "anchor_date": monday(),
    })
    assert r.status_code == 403
    assert conn.execute(
        "SELECT pattern FROM custody_schedule WHERE circle_id = ?", (circle2,)
    ).fetchone()["pattern"] == "two_two_three"
    # Dylan created the hub, so as household admin he CAN configure circle 2.
    r = client.post(f"/settings/custody/{circle2}", data={
        "pattern": "alternating_weeks",
        "first_parent": str(parent_id(conn, "Mark")),
        "anchor_date": monday(),
    })
    assert r.status_code == 200
    assert conn.execute(
        "SELECT pattern FROM custody_schedule WHERE circle_id = ?", (circle2,)
    ).fetchone()["pattern"] == "alternating_weeks"
    # Admin power stops at configuration: Dylan still can't decide circle-2
    # swaps (covered in test_swaps_scoped_to_circle) or see their privates.
    conn.close()


def test_household_timezone_setting(env, client):
    do_setup(client)
    conn = db.connect()
    client.post("/settings/household", data={
        "household_name": "Blended Testers", "timezone": "America/Chicago",
    })
    assert db.get_setting(conn, "timezone") == "America/Chicago"
    client.post("/settings/household", data={
        "household_name": "Blended Testers", "timezone": "Not/AZone",
    })
    assert db.get_setting(conn, "timezone") == "America/Chicago"
    assert client.get("/").status_code == 200
    conn.close()


def test_lunch_on_display(env, client, monkeypatch):
    from hub import lunch as lunch_mod

    do_setup(client)
    conn = db.connect()
    client.post("/settings/lunch", data={
        "lunch_url1": "https://menus.healthepro.com/organizations/99/sites/760/menus/104901",
        "lunch_label1": "Elementary",
        "lunch_url2": "not a menu url",
    })
    menus = lunch_mod.get_menus(conn)
    assert len(menus) == 1 and menus[0]["label"] == "Elementary"

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    monkeypatch.setattr(
        lunch_mod, "fetch_month",
        lambda url, y, m: ({tomorrow: ["CHEESE PIZZA", "CORN", "MILK"]}, []),
    )
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert "Cheese Pizza, Corn" in display  # title-cased, Milk filtered
    assert "MILK" not in display and "Milk" not in display
    assert "Lunch" in display
    conn.close()


def test_home_away_custody_colors(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    # Default mode is home & away: status badges and both colors render
    # (the anchor week always includes at least one away day per circle).
    assert 'class="dwhostatus"' in display
    assert "#45a06c" in display and "#b1a99e" in display
    assert "Kids home" in display
    # Setup stored Dylan + Mark as the home adults.
    assert db.get_setting(conn, "home_parent_ids") == "{},{}".format(
        parent_id(conn, "Dylan"), parent_id(conn, "Mark")
    )
    # Switching back to per-parent coloring removes the badges.
    client.post("/settings/appearance", data={
        "my_theme": "light", "display_theme": "warm", "custody_mode": "parent",
    })
    display2 = TestClient(env).get(f"/display?token={token}").text
    assert 'class="dwhostatus"' not in display2
    conn.close()


def test_custody_driven_routines(env, client):
    from datetime import date as date_cls

    from hub import custody as custody_mod

    do_setup(client)
    conn = db.connect()
    circle1 = conn.execute(
        "SELECT circle_id FROM circle_parents WHERE parent_id = ?",
        (parent_id(conn, "Dylan"),),
    ).fetchone()["circle_id"]

    # Custody-resolved routine, every day of the week.
    client.post("/settings/routines/add", data={
        "label": "picks up the girls from school",
        "time": "15:00",
        "circle_id": str(circle1),
        "who": "custodian",
        "days": ["0", "1", "2", "3", "4", "5", "6"],
    })
    # Pinned-adult routine.
    client.post("/settings/routines/add", data={
        "label": "handles bedtime",
        "time": "20:00",
        "circle_id": str(circle1),
        "who": str(parent_id(conn, "Mark")),
        "days": ["0", "1", "2", "3", "4", "5", "6"],
    })
    # A routine on a different weekday must not show today.
    client.post("/settings/routines/add", data={
        "label": "takes out the trash",
        "time": "08:00",
        "circle_id": str(circle1),
        "who": "custodian",
        "days": [str((date_cls.today().weekday() + 1) % 7)],
    })

    who_id = custody_mod.custodian_on(conn, circle1, date_cls.today())
    who_first = conn.execute(
        "SELECT name FROM parents WHERE id = ?", (who_id,)
    ).fetchone()["name"].split()[0]
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert f"<strong>{who_first}</strong> picks up the girls from school" in display
    assert "<strong>Mark</strong> handles bedtime" in display
    assert "takes out the trash" not in display

    # Removing the first routine leaves the pinned one.
    client.post("/settings/routines/0/delete")
    display2 = TestClient(env).get(f"/display?token={token}").text
    assert "picks up the girls" not in display2
    assert "handles bedtime" in display2
    conn.close()


def test_display_has_tabbed_views(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert 'data-view="calendar"' in display
    assert 'data-view="tasks"' in display
    assert 'data-view="custody"' in display
    assert 'data-cal="today"' in display and 'data-cal="month"' in display
    assert 'id="cal-today"' in display
    assert 'id="cal-week"' in display
    assert 'id="view-custody"' in display
    assert "Tomorrow" in display
    # Custody presentation lives only in its own tab: the who-cards appear
    # after the custody section starts, not inside the Today view.
    assert display.index('class="dwhocard"') > display.index('id="view-custody"')
    today_section = display[
        display.index('id="cal-today"'):display.index('id="cal-week"')
    ]
    assert "dwhocard" not in today_section
    assert "takes over" not in today_section
    conn.close()


def test_pwa_install_surface(env, client):
    do_setup(client)
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200
    assert "application/manifest+json" in m.headers["content-type"]
    assert '"display": "standalone"' in m.text
    assert "Blended Testers" in m.text  # named after the household
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]
    for size in (180, 192, 512):
        assert client.get(f"/static/icons/icon-{size}.png").status_code == 200
    page = client.get("/").text
    assert "manifest.webmanifest" in page
    assert "serviceWorker" in page
    assert "apple-touch-icon" in page


def test_custody_switch_is_stated_as_an_event(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    # The who-has-the-kids card names who takes over and when, instead of an
    # ambiguous "handoff around 18:00" — 12-hour time, explicit day.
    assert "takes over" in display
    assert 'class="davatar"' in display
    assert "18:00" not in display and "17:00" not in display
    # Feed custody descriptions carry the same explicit transition.
    feed_token = conn.execute(
        "SELECT token FROM feeds WHERE kind='all' AND owner_parent_id = ?",
        (parent_id(conn, "Dylan"),),
    ).fetchone()["token"]
    ics = client.get(f"/ics/{feed_token}.ics").text
    assert "takes over" in ics
    conn.close()


def test_themes_and_custom_colors(env, client):
    do_setup(client)
    conn = db.connect()

    # Kid and adult colors are editable with valid hex; junk is ignored.
    client.post("/settings/kids/1/color", data={"color": "#123abc"})
    assert conn.execute("SELECT color FROM kids WHERE id = 1").fetchone()["color"] == "#123abc"
    client.post("/settings/kids/1/color", data={"color": "red"})
    assert conn.execute("SELECT color FROM kids WHERE id = 1").fetchone()["color"] == "#123abc"
    client.post("/settings/my-color", data={"color": "#00aa77"})
    assert conn.execute(
        "SELECT color FROM parents WHERE name = 'Dylan'"
    ).fetchone()["color"] == "#00aa77"

    # Per-adult web theme + household display theme.
    client.post("/settings/appearance", data={"my_theme": "dark", "display_theme": "dark"})
    assert 'data-theme="dark"' in client.get("/calendar").text
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert "theme-dark" in display
    # Another adult keeps their own (light) theme.
    sarah = join(env, conn, "Sarah", "sarahpass")
    assert 'data-theme="light"' in sarah.get("/calendar").text
    conn.close()


def test_unreachable_database_shows_diagnostic_page(monkeypatch):
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://u:p@127.0.0.1:59999/nope")
    app = create_app()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/")
    assert r.status_code == 500
    assert "can't reach its database" in r.text


def test_repeating_event_creates_series(env, client):
    do_setup(client)
    conn = db.connect()
    first = date.today() + timedelta(days=2)
    until = first + timedelta(weeks=3)
    client.post("/events/new", data={
        "title": "Piano lesson",
        "category": "activity",
        "date": first.isoformat(),
        "start_time": "15:00",
        "repeat_until": until.isoformat(),
        "kid_ids": [str(conn.execute("SELECT id FROM kids WHERE name='Ava'").fetchone()["id"])],
    })
    rows = conn.execute("SELECT * FROM events ORDER BY date").fetchall()
    assert len(rows) == 4
    assert len({r["series_id"] for r in rows}) == 1
    client.post(f"/events/{rows[0]['id']}/delete", data={"scope": "series"})
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0
    conn.close()


def test_bulk_event_import(env, client):
    do_setup(client)
    conn = db.connect()
    kid_id = conn.execute("SELECT id FROM kids WHERE name = 'Emma'").fetchone()["id"]

    rows = "\n".join([
        "# comment line",
        "date, title",
        "2026-09-07, No School — Labor Day",
        "2026-09-10..2026-09-11, Picture Days",
        "2026-12-03, Christmas Program, 18:30",
        "not-a-date, Broken line",
    ])
    r = client.post("/settings/events/import",
                    data={"rows": rows, "category": "school",
                          "kid_ids": [str(kid_id)]})
    assert r.status_code == 200
    assert "Imported 4 event(s)" in r.text
    assert "couldn't read 1 line(s)" in r.text

    evs = {e["date"]: e for e in conn.execute(
        "SELECT * FROM events ORDER BY date").fetchall()}
    assert evs["2026-09-07"]["all_day"] == 1
    assert evs["2026-09-10"]["title"] == "Picture Days"
    assert evs["2026-09-11"]["title"] == "Picture Days"
    assert evs["2026-12-03"]["start_time"] == "18:30"
    assert evs["2026-12-03"]["all_day"] == 0
    linked = conn.execute(
        "SELECT kid_id FROM event_kids WHERE event_id = ?",
        (evs["2026-09-07"]["id"],)).fetchall()
    assert [row["kid_id"] for row in linked] == [kid_id]

    # Re-importing the same rows is a no-op.
    r = client.post("/settings/events/import", data={"rows": rows})
    assert "Imported 0 event(s)" in r.text
    assert "skipped 4" in r.text
    conn.close()


def test_display_quick_add(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    dylan = conn.execute("SELECT id FROM parents WHERE name = 'Dylan'").fetchone()["id"]
    kid_id = conn.execute("SELECT id FROM kids WHERE name = 'Emma'").fetchone()["id"]
    kiosk = TestClient(env, follow_redirects=False)

    # No token, no session -> refused.
    r = kiosk.post("/display/events/new", data={
        "title": "Nope", "date": date.today().isoformat(), "parent_id": str(dylan)})
    assert r.status_code == 403

    r = kiosk.post("/display/events/new", data={
        "token": token, "title": "Dentist for Emma",
        "date": date.today().isoformat(), "start_time": "15:30",
        "end_time": "16:15",
        "parent_id": str(dylan), "kid_ids": [str(kid_id)]})
    assert r.status_code == 303
    assert "/display" in r.headers["location"]

    ev = conn.execute("SELECT * FROM events WHERE title = 'Dentist for Emma'").fetchone()
    assert ev["start_time"] == "15:30"
    assert ev["end_time"] == "16:15"
    assert ev["created_by"] == dylan
    assert ev["private"] == 0

    # The add button and modal render on the display.
    page = TestClient(env).get(f"/display?token={token}").text
    assert "addmodal" in page and "Add to the calendar" in page
    assert "Dentist for Emma" in page
    conn.close()


def test_display_month_and_week_navigation(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    # An event two months out, visible only when that month is requested.
    target = (date.today().replace(day=1) + timedelta(days=62)).replace(day=15)
    client.post("/settings/events/import", data={
        "rows": f"{target.isoformat()}, Far Future Field Trip"})
    kiosk = TestClient(env)

    page = kiosk.get(f"/display?token={token}").text
    assert "cal-month" in page and "dmonth" in page
    assert date.today().strftime("%B %Y") in page
    assert "Far Future Field Trip" not in page

    month_q = target.strftime("%Y-%m")
    page = kiosk.get(f"/display?token={token}&month={month_q}").text
    assert target.strftime("%B %Y") in page
    assert "Far Future Field Trip" in page
    assert "Back to now" in page

    # Week arrows: an offset week is Monday-anchored and labeled.
    page = kiosk.get(f"/display?token={token}&week=1").text
    assert "Back to now" in page
    # Garbage offsets and months fall back to now instead of erroring.
    assert kiosk.get(f"/display?token={token}&week=zzz&month=nope").status_code == 200
    conn.close()


def test_tasks_and_rewards(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    emma = conn.execute("SELECT id FROM kids WHERE name = 'Emma'").fetchone()["id"]
    ava = conn.execute("SELECT id FROM kids WHERE name = 'Ava'").fetchone()["id"]

    # Add tasks in Settings: an everyday one, and one only on weekdays.
    client.post("/settings/tasks/add", data={
        "kid_id": str(emma), "label": "Brush teeth", "emoji": "🪥",
        "section": "morning", "points": "10",
        "days": ["0", "1", "2", "3", "4", "5", "6"]})
    client.post("/settings/tasks/add", data={
        "kid_id": str(emma), "label": "Feed cat", "section": "chores",
        "points": "5", "time": "17:00",
        "days": ["0", "1", "2", "3", "4", "5", "6"]})
    client.post("/settings/tasks/add", data={
        "kid_id": str(ava), "label": "Make bed", "section": "morning",
        "points": "10", "days": [str((date.today().weekday() + 1) % 7)]})
    client.post("/settings/tasks/rewards", data={
        f"goal_{emma}": "50", f"reward_{emma}": "Movie night pick"})

    # The display shows only today's tasks: Emma's two, not Ava's
    # tomorrow-only task.
    page = TestClient(env).get(f"/display?token={token}").text
    assert "Brush teeth" in page and "Feed cat" in page
    assert "Make bed" not in page
    assert "Movie night pick" in page and "/ 50" in page

    # Tap to check off: stars accrue; tap again to undo.
    task_id = conn.execute(
        "SELECT id FROM tasks WHERE label = 'Brush teeth'").fetchone()["id"]
    kiosk = TestClient(env)
    r = kiosk.post("/display/tasks/toggle",
                   data={"token": token, "task_id": str(task_id)})
    assert r.json() == {"done": True, "kid_id": emma, "stars_week": 10}
    r = kiosk.post("/display/tasks/toggle",
                   data={"token": token, "task_id": str(task_id)})
    assert r.json() == {"done": False, "kid_id": emma, "stars_week": 0}
    # No token -> refused.
    assert kiosk.post("/display/tasks/toggle",
                      data={"task_id": str(task_id)}).status_code == 403

    # Removing a task removes it from the display.
    client.post(f"/settings/tasks/{task_id}/delete")
    page = TestClient(env).get(f"/display?token={token}").text
    assert "Brush teeth" not in page
    conn.close()


def test_lunch_logo_upload_roundtrip(env, client):
    import io as _io
    from PIL import Image
    do_setup(client)
    conn = db.connect()

    buf = _io.BytesIO()
    Image.new("RGBA", (300, 200), (180, 30, 30, 255)).save(buf, "PNG")
    r = client.post(
        "/settings/lunch",
        data={"lunch_url1": "https://ssl.fastdir.com/~fastdir/cgi/0124/Lunch.pl",
              "lunch_label1": "St. Paul"},
        files={"lunch_logo1": ("shield.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200
    from hub import lunch as lunchmod
    menu = lunchmod.get_menus(conn)[0]
    assert menu["logo"].startswith("data:image/png;base64,")
    # Re-saving without a new file keeps the logo; the clear box removes it.
    client.post("/settings/lunch", data={
        "lunch_url1": menu["url"], "lunch_label1": "St. Paul"})
    assert lunchmod.get_menus(conn)[0].get("logo") == menu["logo"]
    client.post("/settings/lunch", data={
        "lunch_url1": menu["url"], "lunch_label1": "St. Paul",
        "lunch_logo_clear1": "on"})
    assert "logo" not in lunchmod.get_menus(conn)[0]
    conn.close()


def test_display_quick_add_recurring(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    dylan = conn.execute("SELECT id FROM parents WHERE name = 'Dylan'").fetchone()["id"]
    start = date.today()
    kiosk = TestClient(env, follow_redirects=False)
    r = kiosk.post("/display/events/new", data={
        "token": token, "title": "Piano lesson",
        "date": start.isoformat(), "start_time": "16:00",
        "repeat_until": (start + timedelta(weeks=3)).isoformat(),
        "parent_id": str(dylan)})
    assert r.status_code == 303
    rows = conn.execute(
        "SELECT date, series_id FROM events WHERE title = 'Piano lesson' "
        "ORDER BY date").fetchall()
    assert [row["date"] for row in rows] == [
        (start + timedelta(weeks=i)).isoformat() for i in range(4)]
    # One shared series id ties the occurrences together.
    assert len({row["series_id"] for row in rows}) == 1
    assert rows[0]["series_id"]
    conn.close()


def test_today_whos_where_strip(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    page = TestClient(env).get(f"/display?token={token}").text
    # The Today view answers "who has which kids" up top: both circles'
    # custodians appear in the strip with their kid dots.
    strip = page[page.index('class="twho"'):page.index("lunchrow") if "lunchrow" in page else page.index("ttimeline")]
    assert "Dylan" in strip  # custodian of circle 1 this week (anchor monday)
    assert 'class="twhoitem"' in strip
    conn.close()
