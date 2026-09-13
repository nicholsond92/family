"""Reminder / web-push pipeline, with the actual network send stubbed."""

from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from hub import db, push
from hub.app import create_app

from test_app import client, do_setup, env, join, parent_id  # noqa: F401


SUB = {"endpoint": "https://push.example/dev1",
       "keys": {"p256dh": "P", "auth": "A"}}


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(push, "_webpush",
                        lambda info, payload, vapid: calls.append((info, payload)))
    monkeypatch.setattr(push, "get_vapid",
                        lambda conn: {"private_pem": "pem", "public_key": "pub"})
    return calls


def test_subscribe_requires_login_and_roundtrips(env, client):
    do_setup(client)
    anon = TestClient(env)
    assert anon.post("/push/subscribe", json=SUB).status_code == 403
    assert client.post("/push/subscribe", json=SUB).status_code == 200
    conn = db.connect()
    row = conn.execute("SELECT * FROM push_subscriptions").fetchone()
    assert row["endpoint"] == SUB["endpoint"]
    assert row["parent_id"] == parent_id(conn, "Dylan")
    # Re-subscribing the same endpoint replaces, not duplicates.
    client.post("/push/subscribe", json=SUB)
    assert conn.execute("SELECT COUNT(*) AS n FROM push_subscriptions") \
        .fetchone()["n"] == 1
    client.post("/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
    assert conn.execute("SELECT COUNT(*) AS n FROM push_subscriptions") \
        .fetchone()["n"] == 0
    conn.close()


def test_dispatch_sends_due_reminder_once(env, client, sent):
    do_setup(client)
    conn = db.connect()
    client.post("/push/subscribe", json=SUB)
    today = date.today()
    client.post("/events/new", data={
        "title": "Soccer practice", "category": "activity",
        "date": today.isoformat(), "start_time": "16:00",
        "reminder_minutes": "30",
        "kid_ids": [str(conn.execute(
            "SELECT id FROM kids WHERE name='Emma'").fetchone()["id"])],
    })

    # Too early: nothing goes out, reminder stays armed.
    early = datetime.combine(today, datetime.strptime("15:00", "%H:%M").time())
    out = push.dispatch(conn, early)
    assert out == {"processed": 0, "sent": 0, "lunch_check_sent": 0}

    due = datetime.combine(today, datetime.strptime("15:35", "%H:%M").time())
    out = push.dispatch(conn, due)
    assert out["processed"] == 1 and out["sent"] == 1
    info, payload = sent[0]
    assert info["endpoint"] == SUB["endpoint"]
    assert "Soccer practice at 4pm" in payload
    assert "Emma" in payload

    # Idempotent: a second poke sends nothing.
    assert push.dispatch(conn, due) == {"processed": 0, "sent": 0,
                                        "lunch_check_sent": 0}
    conn.close()


def test_stale_reminders_marked_not_sent(env, client, sent):
    do_setup(client)
    conn = db.connect()
    client.post("/push/subscribe", json=SUB)
    today = date.today()
    client.post("/events/new", data={
        "title": "Missed thing", "category": "other",
        "date": today.isoformat(), "start_time": "08:00",
        "reminder_minutes": "10",
    })
    late = datetime.combine(today, datetime.strptime("12:00", "%H:%M").time())
    out = push.dispatch(conn, late)
    assert out == {"processed": 1, "sent": 0, "lunch_check_sent": 0}
    conn.close()


def test_private_event_reminds_only_circle(env, client, sent):
    do_setup(client)
    conn = db.connect()
    # Dylan and Mark (other circle) both subscribe on their devices.
    client.post("/push/subscribe", json=SUB)
    mark = join(env, conn, "Mark", "markpass")
    mark.post("/push/subscribe", json={
        "endpoint": "https://push.example/mark",
        "keys": {"p256dh": "P", "auth": "A"}})
    today = date.today()
    client.post("/events/new", data={
        "title": "Emma therapy", "category": "medical",
        "date": today.isoformat(), "start_time": "16:00",
        "reminder_minutes": "0", "private": "on",
        "kid_ids": [str(conn.execute(
            "SELECT id FROM kids WHERE name='Emma'").fetchone()["id"])],
    })
    due = datetime.combine(today, datetime.strptime("16:05", "%H:%M").time())
    out = push.dispatch(conn, due)
    endpoints = {info["endpoint"] for info, _ in sent}
    assert out["sent"] == 1
    assert endpoints == {SUB["endpoint"]}  # Mark's device stays silent
    conn.close()


def test_quick_add_carries_reminder(env, client):
    do_setup(client)
    conn = db.connect()
    token = db.get_setting(conn, "display_token")
    dylan = parent_id(conn, "Dylan")
    kiosk = TestClient(env, follow_redirects=False)
    kiosk.post("/display/events/new", data={
        "token": token, "title": "Dentist", "date": date.today().isoformat(),
        "start_time": "10:00", "reminder_minutes": "60",
        "parent_id": str(dylan)})
    ev = conn.execute("SELECT * FROM events WHERE title = 'Dentist'").fetchone()
    assert ev["reminder_minutes"] == 60
    conn.close()


SARAH_SUB = {"endpoint": "https://push.example/sarah",
             "keys": {"p256dh": "P", "auth": "A"}}


def test_new_event_notifies_other_parents(env, client, sent):
    do_setup(client)
    conn = db.connect()
    client.post("/push/subscribe", json=SUB)  # Dylan's device
    sarah = join(env, conn, "Sarah", "sarahpass")
    sarah.post("/push/subscribe", json=SARAH_SUB)
    client.post("/events/new", data={
        "title": "Book fair", "category": "school",
        "date": date.today().isoformat(), "start_time": "09:00"})
    endpoints = {info["endpoint"] for info, _ in sent}
    # Everyone but the creator hears about it.
    assert SARAH_SUB["endpoint"] in endpoints
    assert SUB["endpoint"] not in endpoints
    payload = sent[0][1]
    assert "Dylan added an event" in payload and "Book fair" in payload


def test_notification_prefs_respected(env, client, sent):
    do_setup(client)
    conn = db.connect()
    sarah = join(env, conn, "Sarah", "sarahpass")
    sarah.post("/push/subscribe", json=SARAH_SUB)
    # Sarah keeps reminders but mutes new-event pushes.
    sarah.post("/settings/notifications", data={"reminders": "on"})
    db.invalidate_memo(conn)
    prefs = push.prefs(conn, parent_id(conn, "Sarah"))
    assert prefs["reminders"] is True and prefs["new_events"] is False
    client.post("/events/new", data={
        "title": "Muted thing", "category": "other",
        "date": date.today().isoformat(), "start_time": "09:00"})
    assert sent == []


def test_swap_request_and_decision_notify(env, client, sent):
    do_setup(client)
    conn = db.connect()
    client.post("/push/subscribe", json=SUB)  # Dylan
    sarah = join(env, conn, "Sarah", "sarahpass")
    sarah.post("/push/subscribe", json=SARAH_SUB)
    start = (date.today() + timedelta(days=14)).isoformat()
    r = client.post("/swaps/new", data={
        "circle_id": "1", "range1_start": start, "range1_end": start,
        "range1_parent": str(parent_id(conn, "Sarah")),
        "reason": "Work trip"}, follow_redirects=False)
    assert r.status_code == 303
    swap_id = r.headers["location"].rsplit("/", 1)[1]
    # The other co-parent hears about the request; the requester doesn't.
    assert {info["endpoint"] for info, _ in sent} == {SARAH_SUB["endpoint"]}
    assert "swap" in sent[0][1].lower()
    sent.clear()
    sarah.post(f"/swaps/{swap_id}/decide", data={"decision": "approved"})
    # The requester hears the decision.
    assert {info["endpoint"] for info, _ in sent} == {SUB["endpoint"]}
    assert "approved" in sent[0][1]


def test_message_notifies_audience(env, client, sent):
    do_setup(client)
    conn = db.connect()
    client.post("/push/subscribe", json=SUB)  # Dylan
    sarah = join(env, conn, "Sarah", "sarahpass")
    sarah.post("/push/subscribe", json=SARAH_SUB)
    r = client.post("/messages/new", data={
        "subject": "Shoes", "body": "Nora needs new sneakers"},
        follow_redirects=False)
    thread_id = r.headers["location"].rsplit("/", 1)[1]
    assert {info["endpoint"] for info, _ in sent} == {SARAH_SUB["endpoint"]}
    assert "sneakers" in sent[0][1]
    sent.clear()
    sarah.post(f"/messages/{thread_id}/reply", data={"body": "On it"})
    assert {info["endpoint"] for info, _ in sent} == {SUB["endpoint"]}


def test_evening_lunch_check(env, client, sent):
    do_setup(client)
    conn = db.connect()
    client.post("/push/subscribe", json=SUB)  # Dylan — circle 1 custodian
    import json as _json
    from datetime import datetime as _dt
    today = date.today()
    tomorrow = today + timedelta(days=1)
    db.set_setting(conn, "lunch_menus", _json.dumps(
        [{"label": "Washington", "url": "https://example.com/menu.pdf"}]))
    for d in (today, tomorrow):
        db.set_setting(
            conn, f"lunch_cache:0:{d.year}-{d.month:02d}",
            _json.dumps({"at": _dt.utcnow().timestamp(),
                         "days": {tomorrow.isoformat(): "Tacos, Corn"}}))
    emma = conn.execute("SELECT id FROM kids WHERE name='Emma'").fetchone()["id"]
    conn.execute("INSERT INTO lunch_choices(kid_id, date, choice) "
                 "VALUES(?, ?, 'pack')", (emma, tomorrow.isoformat()))
    conn.commit()
    db.invalidate_memo(conn)

    # Before 7pm: nothing, and the day is not claimed.
    early = datetime.combine(today, datetime.strptime("18:00", "%H:%M").time())
    assert push.dispatch(conn, early)["lunch_check_sent"] == 0
    assert sent == []

    due = datetime.combine(today, datetime.strptime("19:05", "%H:%M").time())
    out = push.dispatch(conn, due)
    assert out["lunch_check_sent"] >= 1
    payload = sent[0][1]
    # Emma chose Pack; Ava (same circle) never answered.
    assert "Pack tonight" in payload and "Emma" in payload
    assert "No answer yet" in payload and "Ava" in payload

    # Once per evening.
    sent.clear()
    assert push.dispatch(conn, due)["lunch_check_sent"] == 0
    assert sent == []
