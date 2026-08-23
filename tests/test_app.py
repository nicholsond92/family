"""End-to-end flow: setup → events → feeds → invite → swap → messages → display."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from hub import db
from hub.app import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
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
