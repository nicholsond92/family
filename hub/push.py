"""Web-push reminders for events.

Adults enable notifications per device from Settings (on iPhone the hub must
be installed to the Home Screen first — Apple only allows web push for
installed PWAs). Each event can carry a reminder offset; a dispatcher
endpoint, poked every few minutes by a scheduled GitHub Action, sends the
due reminders through the browsers' push services and marks them sent.

VAPID keys are generated once and kept in the settings table, so no manual
key setup is needed. Everything fails soft: a dead subscription is pruned,
a push-service hiccup never breaks the hub.
"""

import json
from datetime import datetime, timedelta

from . import db

# When a reminder's moment falls inside this window behind "now" it is still
# sent (the dispatcher only runs every few minutes); older ones are dropped
# as stale rather than arriving absurdly late.
GRACE = timedelta(minutes=30)
# All-day events with a reminder notify at this local time.
ALLDAY_AT = "07:00"
# The evening lunch check (who still needs a lunch packed) fires at this
# household-local time.
LUNCH_CHECK_AT = "19:00"

# Notification categories, each toggleable per adult in Settings.
PREF_KEYS = [
    ("reminders", "Event reminders I asked for"),
    ("new_events", "Events other people add"),
    ("swaps", "Custody swap requests & decisions"),
    ("messages", "New messages"),
    ("lunch_check", "Evening lunch check (who needs one packed)"),
]


def prefs(conn, parent_id: int) -> dict:
    """This adult's notification toggles; anything unset defaults to on."""
    raw = db.get_setting(conn, f"notify_prefs:{parent_id}", "") or ""
    try:
        stored = json.loads(raw)
    except ValueError:
        stored = {}
    return {key: bool(stored.get(key, True)) for key, _ in PREF_KEYS}


def set_prefs(conn, parent_id: int, enabled: set[str]) -> None:
    db.set_setting(
        conn, f"notify_prefs:{parent_id}",
        json.dumps({key: key in enabled for key, _ in PREF_KEYS}),
    )
    conn.commit()


def notify(conn, parent_ids: set[int], category: str, payload: dict) -> int:
    """send_to_parents, minus anyone who turned this category off."""
    ids = {pid for pid in parent_ids if prefs(conn, pid).get(category, True)}
    return send_to_parents(conn, ids, payload)

REMINDER_CHOICES = [
    ("", "No reminder"),
    ("0", "At the time"),
    ("10", "10 minutes before"),
    ("30", "30 minutes before"),
    ("60", "1 hour before"),
    ("1440", "1 day before"),
]


def get_vapid(conn) -> dict:
    """{private_pem, public_key} — generated on first use and stored."""
    pem = db.get_setting(conn, "vapid_private_pem")
    pub = db.get_setting(conn, "vapid_public_key")
    if pem and pub:
        return {"private_pem": pem, "public_key": pub}
    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid02, b64urlencode

    vapid = Vapid02()
    vapid.generate_keys()
    pem = vapid.private_pem().decode()
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    pub = b64urlencode(raw)
    db.set_setting(conn, "vapid_private_pem", pem)
    db.set_setting(conn, "vapid_public_key", pub)
    conn.commit()
    return {"private_pem": pem, "public_key": pub}


def save_subscription(conn, parent_id: int, sub: dict) -> bool:
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return False
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?",
                 (endpoint,))
    conn.execute(
        "INSERT INTO push_subscriptions(parent_id, endpoint, p256dh, auth, "
        "created_at) VALUES(?, ?, ?, ?, ?)",
        (parent_id, endpoint, keys["p256dh"], keys["auth"],
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return True


def remove_subscription(conn, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?",
                 (endpoint,))
    conn.commit()


def _webpush(subscription_info: dict, payload: str, vapid: dict):
    """Isolated so tests can stub the network out."""
    from pywebpush import webpush

    return webpush(
        subscription_info=subscription_info,
        data=payload,
        vapid_private_key=vapid["private_pem"],
        vapid_claims={"sub": "mailto:hub@example.invalid"},
        timeout=10,
    )


def send_to_parents(conn, parent_ids: set[int], payload: dict) -> int:
    """Send one payload to every subscribed device of the given parents.
    Dead subscriptions (410/404 from the push service) are pruned."""
    if not parent_ids:
        return 0
    placeholders = ",".join("?" * len(parent_ids))
    subs = conn.execute(
        f"SELECT * FROM push_subscriptions WHERE parent_id IN ({placeholders})",
        tuple(parent_ids),
    ).fetchall()
    if not subs:
        return 0
    vapid = get_vapid(conn)
    body = json.dumps(payload)
    sent = 0
    for sub in subs:
        info = {"endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            _webpush(info, body, vapid)
            sent += 1
        except Exception as exc:  # noqa: BLE001 — prune gone, skip the rest
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                remove_subscription(conn, sub["endpoint"])
    return sent


def event_notify_parents(conn, event) -> set[int]:
    """Who should hear about this event: everyone for open events; for
    private ones, the creator plus the co-parents of the event's kids."""
    if not event["private"]:
        return {p["id"] for p in conn.execute(
            "SELECT id FROM parents").fetchall()}
    allowed = {event["created_by"]} if event["created_by"] else set()
    for row in conn.execute(
        "SELECT DISTINCT cp.parent_id AS pid FROM event_kids ek "
        "JOIN kids k ON k.id = ek.kid_id "
        "JOIN circle_parents cp ON cp.circle_id = k.circle_id "
        "WHERE ek.event_id = ?",
        (event["id"],),
    ).fetchall():
        allowed.add(row["pid"])
    return allowed


def _fmt_time(hhmm: str | None) -> str:
    if not hhmm:
        return ""
    t = datetime.strptime(hhmm, "%H:%M")
    return t.strftime("%-I:%M%p").lower().replace(":00", "")


def dispatch(conn, now: datetime) -> dict:
    """Send every due, unsent event reminder. Idempotent — safe to poke as
    often as you like; each reminder goes out once."""
    today = now.date()
    sent_total = events_done = 0
    rows = conn.execute(
        "SELECT * FROM events WHERE reminder_minutes IS NOT NULL "
        "AND reminder_sent = 0 AND date >= ? AND date <= ?",
        ((today - timedelta(days=1)).isoformat(),
         (today + timedelta(days=2)).isoformat()),
    ).fetchall()
    for ev in rows:
        start = ev["start_time"] if not ev["all_day"] else ALLDAY_AT
        try:
            event_dt = datetime.strptime(
                f"{ev['date']} {start or ALLDAY_AT}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=now.tzinfo)
        except ValueError:
            continue
        notify_at = event_dt - timedelta(minutes=ev["reminder_minutes"])
        if notify_at > now:
            continue
        # Mark stale and fresh alike so nothing fires twice or forever.
        conn.execute("UPDATE events SET reminder_sent = 1 WHERE id = ?",
                     (ev["id"],))
        conn.commit()
        events_done += 1
        if now - notify_at > GRACE:
            continue  # too old to be useful
        kid_names = [r["name"].split()[0] for r in conn.execute(
            "SELECT k.name AS name FROM event_kids ek "
            "JOIN kids k ON k.id = ek.kid_id WHERE ek.event_id = ? "
            "ORDER BY k.name", (ev["id"],)).fetchall()]
        when = ("today" if ev["all_day"]
                else f"at {_fmt_time(ev['start_time'])}")
        body = f"{ev['title']} {when}"
        if kid_names:
            body += " · " + ", ".join(kid_names)
        sent_total += notify(
            conn, event_notify_parents(conn, ev), "reminders",
            {"title": "Family Hub", "body": body, "url": "/"},
        )
    lunch_sent = _lunch_check(conn, now)
    return {"processed": events_done, "sent": sent_total,
            "lunch_check_sent": lunch_sent}


def _lunch_check(conn, now: datetime) -> int:
    """Once each evening: tell whoever has each kid tonight which lunches
    still need packing for tomorrow (chose Pack, or hasn't answered).
    Kids who picked School lunch are skipped; a night where everyone picked
    School sends nothing."""
    from . import custody, lunch  # late import — lunch pulls in fetchers

    today = now.date()
    check_at = datetime.strptime(
        f"{today.isoformat()} {LUNCH_CHECK_AT}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=now.tzinfo)
    if now < check_at or now - check_at > GRACE:
        return 0
    if db.get_setting(conn, "lunch_check_sent") == today.isoformat():
        return 0
    # Claim the day before sending so a concurrent poke can't double-send.
    db.set_setting(conn, "lunch_check_sent", today.isoformat())
    conn.commit()
    tomorrow = today + timedelta(days=1)
    if not lunch.lunches_for(conn, [tomorrow]).get(tomorrow.isoformat()):
        return 0  # no school lunch tomorrow — nothing to decide
    choices = {
        r["kid_id"]: r["choice"] for r in conn.execute(
            "SELECT kid_id, choice FROM lunch_choices WHERE date = ?",
            (tomorrow.isoformat(),),
        ).fetchall()
    }
    per_parent: dict[int, dict[str, list[str]]] = {}
    for kid in conn.execute("SELECT * FROM kids ORDER BY name").fetchall():
        choice = choices.get(kid["id"])
        if choice == "school":
            continue
        bucket = "pack" if choice == "pack" else "open"
        # Tonight's custodian packs; without a schedule, both co-parents hear.
        who = custody.custodian_on(conn, kid["circle_id"], today)
        pids = [who] if who else [
            r["parent_id"] for r in conn.execute(
                "SELECT parent_id FROM circle_parents WHERE circle_id = ?",
                (kid["circle_id"],),
            ).fetchall()
        ]
        first = kid["name"].split()[0]
        for pid in pids:
            per_parent.setdefault(pid, {"pack": [], "open": []})[bucket] \
                .append(first)
    sent = 0
    for pid, buckets in per_parent.items():
        parts = []
        if buckets["pack"]:
            parts.append("Pack tonight: " + ", ".join(buckets["pack"]))
        if buckets["open"]:
            parts.append("No answer yet: " + ", ".join(buckets["open"]))
        sent += notify(
            conn, {pid}, "lunch_check",
            {"title": "Lunch check 🥪", "body": " · ".join(parts),
             "url": "/display?view=tasks"},
        )
    return sent
