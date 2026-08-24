"""End-to-end flow: setup → events → feeds → invite → swap → messages → display.

Runs against SQLite by default. To run the same suite against Postgres
(validating the Supabase/Vercel path), point HUB_DATABASE_URL at a throwaway
Postgres database — its public schema is DROPPED before each test:

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


def do_setup(client):
    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    r = client.post("/setup", data={
        "household_name": "Testers",
        "parent1_name": "Dylan",
        "parent1_email": "dylan@example.com",
        "parent1_password": "pass1234",
        "parent2_name": "Alex",
        "kid1_name": "Sam",
        "kid2_name": "Riley",
        "pattern": "alternating_weeks",
        "first_parent": "1",
        "anchor_date": monday,
        "handoff_time": "18:00",
    })
    assert r.status_code == 200
    return monday


def test_root_redirects_to_setup(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_full_flow(env, client):
    do_setup(client)

    # Logged in as parent 1; calendar shows custody banner and household.
    r = client.get("/")
    assert "Testers" in r.text
    assert "Dylan" in r.text

    # Create an event with kids attached.
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post("/events/new", data={
        "title": "Soccer practice",
        "category": "activity",
        "date": tomorrow,
        "start_time": "16:00",
        "end_time": "17:30",
        "location": "Park",
        "notes": "",
        "kid_ids": ["1"],
    })
    assert "Soccer practice" in r.text

    # Feeds exist (all + custody + one per kid) and serve valid ICS.
    conn = db.connect()
    feeds_rows = conn.execute("SELECT * FROM feeds ORDER BY id").fetchall()
    assert [f["kind"] for f in feeds_rows] == ["all", "custody", "kid", "kid"]
    all_token = feeds_rows[0]["token"]
    r = client.get(f"/ics/{all_token}.ics")
    assert r.status_code == 200
    assert "text/calendar" in r.headers["content-type"]
    assert "Soccer practice" in r.text
    assert "Kids with" in r.text
    assert client.get("/ics/wrong-token.ics").status_code == 404

    # Second parent joins via invite link.
    invite = conn.execute(
        "SELECT invite_token FROM parents WHERE name = 'Alex'"
    ).fetchone()["invite_token"]
    assert invite
    alex = TestClient(env, follow_redirects=True)
    r = alex.post(f"/invite/{invite}", data={"password": "alexpass"})
    assert r.status_code == 200
    assert "Testers" in r.text

    # Dylan requests a swap; Dylan cannot approve their own request.
    start = (date.today() + timedelta(days=10)).isoformat()
    end = (date.today() + timedelta(days=12)).isoformat()
    r = client.post("/swaps/new", data={
        "range1_parent": "1",
        "range1_start": start,
        "range1_end": end,
        "reason": "Lake weekend with the kids",
    })
    assert "pending" in r.text
    swap_id = conn.execute("SELECT id FROM swaps").fetchone()["id"]
    client.post(f"/swaps/{swap_id}/decide", data={"decision": "approved"})
    assert conn.execute(
        "SELECT status FROM swaps WHERE id = ?", (swap_id,)
    ).fetchone()["status"] == "pending"

    # Alex approves; overrides are written and show up in the custody feed.
    r = alex.post(f"/swaps/{swap_id}/decide", data={"decision": "approved"})
    assert "approved" in r.text
    n_overrides = conn.execute(
        "SELECT COUNT(*) AS n FROM custody_overrides WHERE swap_id = ?", (swap_id,)
    ).fetchone()["n"]
    assert n_overrides == 3
    override_parent = conn.execute(
        "SELECT parent_id FROM custody_overrides WHERE date = ?", (start,)
    ).fetchone()["parent_id"]
    assert override_parent == 1

    # Messaging: new thread + reply from the other parent.
    r = client.post("/messages/new", data={
        "subject": "Thursday pickup",
        "kid_id": "1",
        "body": "Can you grab Sam at 3?",
    })
    assert "Can you grab Sam at 3?" in r.text
    thread_id = conn.execute(
        "SELECT id FROM threads WHERE swap_id IS NULL"
    ).fetchone()["id"]
    r = alex.post(f"/messages/{thread_id}/reply", data={"body": "Yep, no problem."})
    assert "Yep, no problem." in r.text

    # Wall display: works with token (no login), rejects a bad token.
    display_token = db.get_setting(conn, "display_token")
    anon = TestClient(env)
    assert anon.get(f"/display?token={display_token}").status_code == 200
    assert "Testers" in anon.get(f"/display?token={display_token}").text
    assert anon.get("/display?token=nope").status_code == 403
    assert anon.get("/display").status_code == 403
    conn.close()


def test_login_logout(env, client):
    do_setup(client)
    client.post("/logout")
    r = client.get("/", follow_redirects=False)
    assert r.headers["location"] == "/login"
    r = client.post("/login", data={"name": "dylan", "password": "wrong"})
    assert "Wrong name or password" in r.text
    r = client.post("/login", data={"name": "Dylan", "password": "pass1234"})
    assert "Week of" in r.text


def join_alex(env, conn):
    invite = conn.execute(
        "SELECT invite_token FROM parents WHERE name = 'Alex'"
    ).fetchone()["invite_token"]
    alex = TestClient(env, follow_redirects=True)
    alex.post(f"/invite/{invite}", data={"password": "alexpass"})
    return alex


def test_swap_conflicting_with_approved_swap_is_blocked(env, client):
    do_setup(client)
    conn = db.connect()
    alex = join_alex(env, conn)

    start = (date.today() + timedelta(days=10)).isoformat()
    end = (date.today() + timedelta(days=12)).isoformat()
    client.post("/swaps/new", data={
        "range1_parent": "1", "range1_start": start, "range1_end": end,
    })
    first_id = conn.execute("SELECT MAX(id) AS id FROM swaps").fetchone()["id"]
    alex.post(f"/swaps/{first_id}/decide", data={"decision": "approved"})
    assert conn.execute("SELECT status FROM swaps WHERE id = ?", (first_id,)) \
        .fetchone()["status"] == "approved"

    # Second swap overlapping the same dates, giving the kids to parent 2.
    client.post("/swaps/new", data={
        "range1_parent": "2", "range1_start": start, "range1_end": start,
    })
    second_id = conn.execute("SELECT MAX(id) AS id FROM swaps").fetchone()["id"]
    r = alex.post(f"/swaps/{second_id}/decide", data={"decision": "approved"})
    assert conn.execute("SELECT status FROM swaps WHERE id = ?", (second_id,)) \
        .fetchone()["status"] == "pending"
    # The earlier agreement still owns the date.
    row = conn.execute(
        "SELECT parent_id, swap_id FROM custody_overrides WHERE date = ?", (start,)
    ).fetchone()
    assert (row["parent_id"], row["swap_id"]) == (1, first_id)
    assert "already changed by another" in r.text
    conn.close()


def test_login_checks_all_rows_with_same_name(env, client):
    do_setup(client)
    conn = db.connect()
    alex = join_alex(env, conn)
    # Both parents end up named the same.
    conn.execute("UPDATE parents SET name = 'Sam Parent'")
    conn.commit()
    # Previously only one arbitrary row was checked, so one parent could
    # never log in. Now both passwords work under the shared name.
    for password in ("pass1234", "alexpass"):
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
    join_alex(env, conn)
    assert conn.execute(
        "SELECT password_hash FROM parents WHERE name = 'Alex'"
    ).fetchone()["password_hash"]

    # Dylan can no longer mint an invite link for Alex's claimed account.
    client.post("/settings/parents/2/invite")
    assert conn.execute(
        "SELECT invite_token FROM parents WHERE id = 2"
    ).fetchone()["invite_token"] is None

    # Even a lingering token can't reset a claimed account's password.
    conn.execute("UPDATE parents SET invite_token = 'stale-token' WHERE id = 2")
    conn.commit()
    r = client.post("/invite/stale-token", data={"password": "hijacked"},
                    follow_redirects=False)
    assert r.status_code == 404
    login = TestClient(env, follow_redirects=True)
    assert "Week of" in login.post(
        "/login", data={"name": "Alex", "password": "alexpass"}
    ).text
    conn.close()


def test_household_timezone_setting(env, client):
    do_setup(client)
    conn = db.connect()
    client.post("/settings/household", data={
        "household_name": "Testers", "timezone": "America/Chicago",
    })
    assert db.get_setting(conn, "timezone") == "America/Chicago"
    # An unknown timezone is ignored, keeping the old value.
    client.post("/settings/household", data={
        "household_name": "Testers", "timezone": "Not/AZone",
    })
    assert db.get_setting(conn, "timezone") == "America/Chicago"
    assert client.get("/").status_code == 200
    conn.close()


def test_unreachable_database_shows_diagnostic_page(monkeypatch):
    # A dead Postgres must not crash the function at import; requests get a
    # readable diagnostic page instead.
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://u:p@127.0.0.1:59999/nope")
    app = create_app()
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/")
    assert r.status_code == 500
    assert "can't reach its database" in r.text
    assert "Transaction pooler" in r.text or "HUB_DATABASE_URL" in r.text


def test_repeating_event_creates_series(env, client):
    do_setup(client)
    first = date.today() + timedelta(days=2)
    until = first + timedelta(weeks=3)
    client.post("/events/new", data={
        "title": "Piano lesson",
        "category": "activity",
        "date": first.isoformat(),
        "start_time": "15:00",
        "repeat_until": until.isoformat(),
        "kid_ids": ["2"],
    })
    conn = db.connect()
    rows = conn.execute("SELECT * FROM events ORDER BY date").fetchall()
    assert len(rows) == 4
    assert len({r["series_id"] for r in rows}) == 1
    # Delete the whole series.
    client.post(f"/events/{rows[0]['id']}/delete", data={"scope": "series"})
    assert conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"] == 0
    conn.close()
