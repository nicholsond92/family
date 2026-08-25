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
    r = client.get("/")
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
    assert "Emma therapy" in client.get(f"/?start={when}").text
    # Mark sees a Busy block, not the details, and can't open the event.
    mark_cal = mark.get(f"/?start={when}").text
    assert "Emma therapy" not in mark_cal
    assert "Busy" in mark_cal
    assert mark.get(f"/events/{event_id}/edit").status_code == 404
    # Sarah (Emma's other co-parent) has full access.
    sarah = join(env, conn, "Sarah", "sarahpass")
    assert "Emma therapy" in sarah.get(f"/?start={when}").text
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
        assert "Week of" in r.text
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


def test_display_has_tabbed_views(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert 'data-view="today"' in display
    assert 'data-view="week"' in display
    assert 'id="view-today"' in display
    assert 'id="view-week"' in display
    assert "Tomorrow" in display
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
    # The banner names who takes over and when, instead of an ambiguous
    # "handoff around 18:00" — 12-hour time, explicit day.
    assert "switching to" in display
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
    assert 'data-theme="dark"' in client.get("/").text
    token = db.get_setting(conn, "display_token")
    display = TestClient(env).get(f"/display?token={token}").text
    assert "theme-dark" in display
    # Another adult keeps their own (light) theme.
    sarah = join(env, conn, "Sarah", "sarahpass")
    assert 'data-theme="light"' in sarah.get("/").text
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
