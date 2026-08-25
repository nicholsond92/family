"""Family Hub web application (FastAPI).

v2 model: one household, several adults, one or two co-parenting circles.
Each circle (a pair of co-parents) has its own custody schedule and swap
requests. Events can be private: full details stay visible to that kid's
co-parents and the creator; everyone else sees a Busy block. Calendar feeds
are per-adult so a shared URL can't leak private details.
"""

import base64
import csv
import html
import io
import json
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import custody, db, feeds, lunch, security

BASE_DIR = Path(__file__).resolve().parent

KID_COLORS = ["#e63946", "#f4a261", "#2a9d8f", "#457b9d", "#8d5bd4", "#d81b8c", "#3a86ff", "#588157"]
PARENT_COLORS = ["#3a86ff", "#e63946", "#2a9d8f", "#8d5bd4", "#f4a261", "#d81b8c"]
# One-click preset swatches shown in Settings (custom hex stays available too).
PALETTE = [
    "#e63946", "#f4a261", "#f5b942", "#588157", "#2a9d8f",
    "#3a86ff", "#457b9d", "#8d5bd4", "#d81b8c", "#64748b",
]
CATEGORIES = ["school", "activity", "medical", "other"]
TASK_SECTIONS = ["morning", "afternoon", "evening", "chores"]
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


# Stable badge colors for school monograms — distinct from the kid/adult
# palette so lunch badges read as "places", not people.
SCHOOL_COLORS = ["#b45309", "#0e7490", "#7c3aed", "#be185d",
                 "#15803d", "#b91c1c", "#4338ca", "#a16207"]


def school_badge(label: str) -> dict:
    """Monogram badge for a school lunch label: initials + a stable color
    derived from the label, so each school keeps its identity everywhere."""
    label = (label or "").strip()
    if not label or label.lower() == "all schools":
        return {"initials": "🍽", "color": "#8a8378"}
    words = [w for w in re.split(r"[^A-Za-z0-9]+", label) if w]
    initials = "".join(w[0] for w in words[:2]).upper() or label[:1].upper()
    color = SCHOOL_COLORS[sum(label.lower().encode()) % len(SCHOOL_COLORS)]
    return {"initials": initials, "color": color}


def process_logo(data: bytes) -> str | None:
    """Downscale an uploaded school logo to a small square badge and inline
    it as a PNG data URI — the serverless host has no persistent disk, so
    logos live in the settings row alongside their menu."""
    if not data or len(data) > 5 * 1024 * 1024:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((96, 96))
        canvas = Image.new("RGBA", (96, 96), (255, 255, 255, 0))
        canvas.paste(im, ((96 - im.width) // 2, (96 - im.height) // 2), im)
        buf = io.BytesIO()
        canvas.save(buf, "PNG", optimize=True)
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 — a bad image never breaks settings
        return None


def _hhmm(value: str) -> str | None:
    """Normalized 'HH:MM' or None."""
    m = HHMM.match((value or "").strip())
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"

# Open-Meteo WMO weather codes -> short display words.
WEATHER_WORDS = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Foggy", 48: "Foggy",
    51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 56: "Drizzle", 57: "Drizzle",
    61: "Rain", 63: "Rain", 65: "Rain", 66: "Rain", 67: "Rain",
    71: "Snow", 73: "Snow", 75: "Snow", 77: "Snow",
    80: "Showers", 81: "Showers", 82: "Showers",
    85: "Snow", 86: "Snow",
    95: "Storms", 96: "Storms", 99: "Storms",
}

# Open-Meteo WMO weather codes -> kid-friendly symbols for the wall display.
WEATHER_ICONS = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌦️", 56: "🌦️", 57: "🌦️",
    61: "🌧️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
    71: "❄️", 73: "❄️", 75: "❄️", 77: "❄️",
    80: "🌦️", 81: "🌦️", 82: "🌦️",
    85: "❄️", 86: "❄️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


def _session_secret() -> str:
    try:
        conn = db.connect()
        try:
            secret = db.get_setting(conn, "session_secret")
            if not secret:
                secret = security.new_token(32)
                db.set_setting(conn, "session_secret", secret)
            return secret
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — DB down at cold start
        # Don't crash at import: run with an ephemeral secret so requests can
        # still reach the diagnostic error page below.
        print(f"family-hub: database unavailable at startup: {exc!r}", file=sys.stderr)
        return security.new_token(32)


def _db_hints(exc_text: str) -> list[str]:
    """Human-readable causes for common database connection failures."""
    hints = []
    low = exc_text.lower()
    host = ""
    try:
        host = urlsplit(db.database_url()).hostname or ""
    except ValueError:
        pass
    if host.startswith("db.") and host.endswith(".supabase.co"):
        hints.append(
            "The connection string uses Supabase's DIRECT hostname "
            f"({host}), which is IPv6-only — serverless platforms can't reach "
            "it, whatever the port. In Supabase click Connect and copy the "
            "'Transaction pooler' URI instead: its host looks like "
            "aws-1-<region>.pooler.supabase.com and its username includes "
            "your project ref (postgres.<ref>)."
        )
    if not db.database_url():
        hints.append(
            "No Postgres database is configured. On serverless hosts (Vercel), "
            "set the HUB_DATABASE_URL environment variable to your Supabase "
            "connection string (Project Settings → Environment Variables) and "
            "redeploy — SQLite can't be used there because the filesystem is "
            "read-only."
        )
    if "password" in low and "authentication" in low:
        hints.append(
            "The database password in the connection string is wrong — or still "
            "the [YOUR-PASSWORD] placeholder. If the password has special "
            "characters, URL-encode them (@ → %40, # → %23, etc.)."
        )
    if "tenant or user not found" in low:
        hints.append(
            "With Supabase's pooler the username must include the project ref "
            "(e.g. postgres.abcdefghijk). Copy the full Transaction pooler "
            "string from Supabase → Connect rather than editing it by hand."
        )
    if "cannot assign requested address" in low:
        hints.append(
            "The database address resolved to IPv6, which this platform can't "
            "reach. The app pins an IPv4 address automatically; if this "
            "persists, the host may have no IPv4 address — use Supabase's "
            "pooler hostname (aws-…pooler.supabase.com), which supports IPv4."
        )
    if any(w in low for w in ("unreachable", "timed out", "timeout", "could not translate")):
        hints.append(
            "Use Supabase's Transaction pooler connection string (port 6543), "
            "not the direct connection (port 5432) — serverless platforms "
            "usually can't reach the direct address."
        )
    if "unable to open database file" in low or "readonly database" in low:
        hints.append(
            "The app fell back to SQLite on a read-only filesystem. Set "
            "HUB_DATABASE_URL to your Supabase Postgres connection string."
        )
    if not hints:
        hints.append(
            "Check that the database is running and that the connection string "
            "in HUB_DATABASE_URL (or POSTGRES_URL) is exactly the one your "
            "provider shows."
        )
    return hints


def _is_connection_error(exc: Exception) -> bool:
    return type(exc).__name__ in (
        "OperationalError", "InterfaceError", "ConnectionTimeout",
    )


def _db_error_page(exc: Exception) -> HTMLResponse:
    if not _is_connection_error(exc):
        # A query failed — an application bug, not a reachability problem.
        detail = html.escape(f"{type(exc).__name__}: {exc}")
        return HTMLResponse(
            "<div style='font-family:sans-serif;max-width:640px;margin:4rem auto'>"
            "<h1>Family Hub hit a database error</h1>"
            f"<p style='color:#a11622'><code>{detail}</code></p>"
            "<p>The database is reachable, but this request failed. Your data "
            "is safe — the change was rolled back. Please report the text "
            "above so it can be fixed.</p></div>",
            status_code=500,
        )
    items = "".join(f"<li>{html.escape(h)}</li>" for h in _db_hints(str(exc)))
    detail = html.escape(f"{type(exc).__name__}: {exc}")
    tried = ""
    try:
        parts = urlsplit(db.database_url())
        if parts.hostname:
            tried = (
                "<p>Configured database host: "
                f"<code>{html.escape(parts.hostname)}:{parts.port or 5432}"
                "</code></p>"
            )
    except ValueError:
        pass
    return HTMLResponse(
        "<div style='font-family:sans-serif;max-width:640px;margin:4rem auto'>"
        "<h1>Family Hub can't reach its database</h1>"
        f"<p style='color:#a11622'><code>{detail}</code></p>"
        f"{tried}"
        f"<p>Likely fix:</p><ul>{items}</ul>"
        "<p>After changing an environment variable, redeploy for it to take "
        "effect.</p></div>",
        status_code=500,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="Family Hub")
    app.add_middleware(SessionMiddleware, secret_key=_session_secret(), max_age=60 * 60 * 24 * 90)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    async def _db_error_handler(request: Request, exc: Exception):
        return _db_error_page(exc)

    app.add_exception_handler(sqlite3.Error, _db_error_handler)
    try:
        import psycopg

        app.add_exception_handler(psycopg.Error, _db_error_handler)
    except ImportError:
        pass

    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    templates.env.globals["categories"] = CATEGORIES
    templates.env.globals["patterns"] = custody.PATTERNS

    # ------------------------------------------------------------------ helpers

    def get_conn():
        return db.connect()

    def parents(conn):
        return conn.execute("SELECT * FROM parents ORDER BY id").fetchall()

    def kids(conn):
        return conn.execute("SELECT * FROM kids ORDER BY id").fetchall()

    def circles(conn):
        return conn.execute("SELECT * FROM circles ORDER BY id").fetchall()

    def circle_members(conn) -> dict[int, list]:
        """circle id -> [parent rows] (the circle's two co-parents)."""
        by_id = {p["id"]: p for p in parents(conn)}
        out: dict[int, list] = {}
        for row in conn.execute(
            "SELECT circle_id, parent_id FROM circle_parents ORDER BY parent_id"
        ):
            if row["parent_id"] in by_id:
                out.setdefault(row["circle_id"], []).append(by_id[row["parent_id"]])
        return out

    def my_circle_ids(conn, parent_id: int) -> list[int]:
        return [
            r["circle_id"] for r in conn.execute(
                "SELECT circle_id FROM circle_parents WHERE parent_id = ? "
                "ORDER BY circle_id",
                (parent_id,),
            )
        ]

    def circle_kid_labels(conn) -> dict[int, str]:
        """circle id -> 'Emma & Ava' (that circle's kids, first names)."""
        names: dict[int, list[str]] = {}
        for row in conn.execute(
            "SELECT circle_id, name FROM kids WHERE circle_id IS NOT NULL ORDER BY name"
        ):
            names.setdefault(row["circle_id"], []).append(row["name"].split()[0])
        return {cid: " & ".join(ns) for cid, ns in names.items()}

    def circle_kid_rows(conn) -> dict[int, list]:
        """circle id -> kid rows, for color dots next to custody pills."""
        out: dict[int, list] = {}
        for row in conn.execute(
            "SELECT * FROM kids WHERE circle_id IS NOT NULL ORDER BY name"
        ):
            out.setdefault(row["circle_id"], []).append(row)
        return out

    def is_admin(conn, parent_id: int) -> bool:
        """The household admin is the adult who created the hub. Stored as a
        setting; older databases fall back to the first-created adult (the
        setup submitter is always the first insert)."""
        stored = db.get_setting(conn, "admin_parent_id")
        if stored:
            try:
                return int(stored) == parent_id
            except ValueError:
                pass
        row = conn.execute("SELECT MIN(id) AS m FROM parents").fetchone()
        return row is not None and row["m"] == parent_id

    def current_parent(request: Request, conn):
        pid = request.session.get("parent_id")
        if not pid:
            return None
        return conn.execute("SELECT * FROM parents WHERE id = ?", (pid,)).fetchone()

    def guard(request: Request, conn):
        """Returns a redirect response if setup/login is needed, else None."""
        if not parents(conn):
            return RedirectResponse("/setup", status_code=303)
        if not current_parent(request, conn):
            return RedirectResponse("/login", status_code=303)
        return None

    def render(request, name, conn, **ctx):
        parent = ctx.setdefault("parent", current_parent(request, conn))
        ctx.setdefault("household", db.get_setting(conn, "household_name", "Family Hub"))
        theme = "light"
        if parent:
            theme = db.get_setting(conn, f"theme:{parent['id']}", "light") or "light"
        ctx.setdefault("theme", theme)
        ctx["request"] = request
        return templates.TemplateResponse(request, name, ctx)

    def hub_tz(conn):
        name = db.get_setting(conn, "timezone", "") or ""
        if name:
            try:
                return ZoneInfo(name)
            except (KeyError, ValueError):
                pass
        return None

    def hub_today(conn) -> date:
        """Today in the household's timezone (falls back to server-local)."""
        tz = hub_tz(conn)
        return datetime.now(tz).date() if tz else date.today()

    def event_allowed_ids(conn, event_id: int) -> set[int]:
        """Adults allowed to see a private event's details: co-parents of the
        event's kids, plus the creator."""
        allowed: set[int] = set()
        for row in conn.execute(
            "SELECT cp.parent_id FROM event_kids ek "
            "JOIN kids k ON k.id = ek.kid_id "
            "JOIN circle_parents cp ON cp.circle_id = k.circle_id "
            "WHERE ek.event_id = ?",
            (event_id,),
        ):
            allowed.add(row["parent_id"])
        row = conn.execute(
            "SELECT created_by FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row and row["created_by"]:
            allowed.add(row["created_by"])
        return allowed

    def can_view_event(conn, ev, viewer_id: int | None) -> bool:
        if not ev["private"]:
            return True
        if viewer_id is None:
            return False
        return viewer_id in event_allowed_ids(conn, ev["id"])

    def events_between(conn, start: date, end: date, viewer_id: int | None):
        """Events with kid chips and a per-viewer `visible` flag."""
        rows = conn.execute(
            "SELECT * FROM events WHERE date BETWEEN ? AND ? "
            "ORDER BY date, all_day DESC, start_time",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        kid_rows = conn.execute(
            "SELECT ek.event_id, k.id, k.name, k.color FROM event_kids ek "
            "JOIN kids k ON k.id = ek.kid_id ORDER BY k.id"
        ).fetchall()
        kids_by_event: dict[int, list] = {}
        for kr in kid_rows:
            kids_by_event.setdefault(kr["event_id"], []).append(kr)
        allowed = feeds.viewer_allowed_parents(conn)
        out = []
        for r in rows:
            visible = feeds.event_visible_to(r, viewer_id, allowed)
            out.append({**dict(r), "kids": kids_by_event.get(r["id"], []),
                        "visible": visible})
        return out

    def week_context(conn, start: date, viewer_id: int | None,
                     today: date | None = None):
        today = today or hub_today(conn)
        parent_by_id = {p["id"]: p for p in parents(conn)}
        schedules = custody.load_schedules(conn)
        labels = circle_kid_labels(conn)
        evs = events_between(conn, start, start + timedelta(days=6), viewer_id)
        evs_by_date: dict[str, list] = {}
        for e in evs:
            evs_by_date.setdefault(e["date"], []).append(e)
        days = []
        for i in range(7):
            d = start + timedelta(days=i)
            day_custody = []
            for cid, schedule in schedules.items():
                who = custody.custodian_on(conn, cid, d, schedule)
                day_custody.append({
                    "circle_id": cid,
                    "label": labels.get(cid, "Kids"),
                    "custodian": parent_by_id.get(who),
                    "has_override": bool(custody.override_on(conn, cid, d)),
                })
            days.append({
                "date": d,
                "iso": d.isoformat(),
                "is_today": d == today,
                "custody": day_custody,
                "events": evs_by_date.get(d.isoformat(), []),
            })
        return {"days": days, "schedules": schedules, "parent_by_id": parent_by_id,
                "circle_labels": labels}

    def fmt_time(hhmm: str | None) -> str:
        if not hhmm:
            return ""
        t = datetime.strptime(hhmm, "%H:%M")
        return t.strftime("%-I:%M%p").lower().replace(":00", "")

    templates.env.filters["fmt_time"] = fmt_time
    templates.env.filters["school_badge"] = school_badge
    templates.env.filters["fmt_date"] = lambda d: (
        date.fromisoformat(d) if isinstance(d, str) else d
    ).strftime("%a %b %-d")
    templates.env.filters["first_name"] = lambda s: (s or "").split()[0] if s else ""

    def get_routines(conn) -> list[dict]:
        raw = db.get_setting(conn, "routines", "") or "[]"
        try:
            routines = json.loads(raw)
            return [r for r in routines if isinstance(r, dict) and r.get("label")]
        except ValueError:
            return []

    def save_routines(conn, routines: list[dict]) -> None:
        db.set_setting(conn, "routines", json.dumps(routines))

    def routines_for_day(conn, d: date) -> list[dict]:
        """Routine reminders resolved against the custody schedule for a
        date: 'picks up the girls at 3pm' becomes whoever actually has the
        kids that day (or a pinned adult)."""
        parent_by_id = {p["id"]: p for p in parents(conn)}
        kid_rows = circle_kid_rows(conn)
        out = []
        for rt in get_routines(conn):
            days = rt.get("days") or [0, 1, 2, 3, 4]
            if d.weekday() not in days:
                continue
            circle_id = rt.get("circle_id")
            who = rt.get("who", "custodian")
            if who == "custodian":
                pid = custody.custodian_on(conn, circle_id, d) if circle_id else None
            else:
                try:
                    pid = int(who)
                except (TypeError, ValueError):
                    pid = None
            parent = parent_by_id.get(pid)
            if not parent:
                continue
            out.append({
                "is_routine": True,
                "start_time": rt.get("time") or "12:00",
                "end_time": None,
                "title": rt["label"],
                "parent": parent,
                "kids": kid_rows.get(circle_id, []),
                "visible": True,
                "all_day": 0,
                "location": "",
            })
        return out

    def task_rewards(conn) -> dict:
        try:
            rewards = json.loads(db.get_setting(conn, "task_rewards", "") or "{}")
            return rewards if isinstance(rewards, dict) else {}
        except ValueError:
            return {}

    def week_star_totals(conn, today: date) -> dict[int, int]:
        """Stars each kid has earned so far this week (Mon–Sun)."""
        wstart = custody.monday_of(today)
        totals: dict[int, int] = {}
        for r in conn.execute(
            "SELECT t.kid_id AS kid_id, SUM(t.points) AS stars "
            "FROM task_checks tc JOIN tasks t ON t.id = tc.task_id "
            "WHERE tc.date >= ? AND tc.date <= ? GROUP BY t.kid_id",
            (wstart.isoformat(), (wstart + timedelta(days=6)).isoformat()),
        ).fetchall():
            totals[r["kid_id"]] = r["stars"] or 0
        return totals

    def tasks_context(conn, today: date) -> list[dict]:
        """Per-kid task cards for the display: today's tasks by section,
        which are already checked off, and stars earned this week toward
        the kid's reward goal."""
        rows = conn.execute(
            "SELECT * FROM tasks WHERE active = 1 "
            "ORDER BY time IS NULL, time, id"
        ).fetchall()
        if not rows:
            return []
        done_today = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM task_checks WHERE date = ?",
            (today.isoformat(),)).fetchall()}
        stars = week_star_totals(conn, today)
        rewards = task_rewards(conn)
        weekday = str(today.weekday())
        cards = []
        for kid in kids(conn):
            ktasks = [dict(t) for t in rows if t["kid_id"] == kid["id"]
                      and weekday in (t["days"] or "").split(",")]
            if not ktasks:
                continue
            for t in ktasks:
                t["done"] = t["id"] in done_today
            sections = [
                {"name": name, "tasks": sec}
                for name in TASK_SECTIONS
                if (sec := [t for t in ktasks if t["section"] == name])
            ]
            conf = rewards.get(str(kid["id"]), {})
            cards.append({
                "kid": kid,
                "sections": sections,
                "done_count": sum(1 for t in ktasks if t["done"]),
                "total": len(ktasks),
                "stars_week": stars.get(kid["id"], 0),
                "goal": conf.get("goal") or 0,
                "reward": conf.get("reward") or "",
            })
        return cards

    def home_parent_ids(conn) -> set[int]:
        """Adults who live where the wall display hangs. Stored at setup
        (you + your partner); older databases fall back to the hub creator
        plus the first-created adult of each other circle."""
        raw = db.get_setting(conn, "home_parent_ids", "") or ""
        try:
            ids = {int(x) for x in raw.split(",") if x.strip()}
            if ids:
                return ids
        except ValueError:
            pass
        row = conn.execute("SELECT MIN(id) AS m FROM parents").fetchone()
        if not row or row["m"] is None:
            return set()
        admin_id = row["m"]
        stored = db.get_setting(conn, "admin_parent_id")
        if stored:
            try:
                admin_id = int(stored)
            except ValueError:
                pass
        ids = {admin_id}
        members = circle_members(conn)
        for cid, plist in members.items():
            member_ids = [p["id"] for p in plist]
            if admin_id not in member_ids and member_ids:
                ids.add(min(member_ids))
        return ids

    def fetch_weather(conn):
        """Current conditions for the wall display via Open-Meteo (no API key).
        Returns None unless a location is configured; never raises."""
        lat = db.get_setting(conn, "weather_lat", "")
        lon = db.get_setting(conn, "weather_lon", "")
        if not lat or not lon:
            return None
        unit = db.get_setting(conn, "weather_unit", "fahrenheit")
        try:
            import requests

            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,weather_code",
                    "daily": "temperature_2m_max,temperature_2m_min,weather_code",
                    "temperature_unit": unit,
                    "timezone": "auto", "forecast_days": 1,
                },
                timeout=4,
            )
            data = resp.json()
            # The symbol/word reflect the day's forecast (not the moment's
            # conditions) so kids see what the day will be like.
            day_codes = data.get("daily", {}).get("weather_code") or []
            code = day_codes[0] if day_codes else data["current"]["weather_code"]
            return {
                "temp": round(data["current"]["temperature_2m"]),
                "hi": round(data["daily"]["temperature_2m_max"][0]),
                "lo": round(data["daily"]["temperature_2m_min"][0]),
                "cond": WEATHER_WORDS.get(code, ""),
                "icon": WEATHER_ICONS.get(code, ""),
            }
        except Exception:  # noqa: BLE001 — weather is decorative, never break the wall
            return None

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    def manifest():
        conn = get_conn()
        try:
            name = db.get_setting(conn, "household_name", "Family Hub") or "Family Hub"
        finally:
            conn.close()
        return Response(
            content=json.dumps({
                "name": name,
                "short_name": name if len(name) <= 12 else "Family Hub",
                "description": "Custody, school, and activity schedules for the whole family.",
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": "#fafafa",
                "theme_color": "#18181b",
                "icons": [
                    {"src": "/static/icons/icon-192.png", "sizes": "192x192",
                     "type": "image/png", "purpose": "any maskable"},
                    {"src": "/static/icons/icon-512.png", "sizes": "512x512",
                     "type": "image/png", "purpose": "any maskable"},
                ],
            }),
            media_type="application/manifest+json",
        )

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so the service worker can control "/".
        return Response(
            content=(BASE_DIR / "static" / "sw.js").read_text(),
            media_type="application/javascript",
        )

    # -------------------------------------------------------------------- setup

    @app.get("/setup", response_class=HTMLResponse)
    def setup_form(request: Request):
        conn = get_conn()
        try:
            if parents(conn):
                return RedirectResponse("/", status_code=303)
            return render(request, "setup.html", conn, kid_colors=KID_COLORS)
        finally:
            conn.close()

    @app.post("/setup")
    async def setup_submit(request: Request):
        conn = get_conn()
        try:
            if parents(conn):
                return RedirectResponse("/", status_code=303)
            form = await request.form()
            db.set_setting(conn, "household_name", form.get("household_name") or "Our Family")
            db.set_setting(conn, "display_token", security.new_token(16))

            def add_adult(name, email, color, password=None):
                if not (name or "").strip():
                    return None
                return db.insert_id(
                    conn,
                    "INSERT INTO parents(name, email, color, password_hash, invite_token) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (
                        name.strip(),
                        (email or "").strip() or None,
                        color,
                        security.hash_password(password) if password else None,
                        None if password else security.new_token(16),
                    ),
                )

            me_id = add_adult(form["adult1_name"], form.get("adult1_email"),
                              PARENT_COLORS[0], form["adult1_password"])
            db.set_setting(conn, "admin_parent_id", str(me_id))
            coparent_id = add_adult(form["adult2_name"], form.get("adult2_email"),
                                    PARENT_COLORS[1])
            partner_id = add_adult(form.get("adult3_name", ""), form.get("adult3_email"),
                                   PARENT_COLORS[2])
            partner_co_id = add_adult(form.get("adult4_name", ""), form.get("adult4_email"),
                                      PARENT_COLORS[3])

            def add_circle(a, b):
                pa = conn.execute("SELECT name FROM parents WHERE id = ?", (a,)).fetchone()
                pb = conn.execute("SELECT name FROM parents WHERE id = ?", (b,)).fetchone()
                cid = db.insert_id(
                    conn, "INSERT INTO circles(name) VALUES(?)",
                    (f"{pa['name']} & {pb['name']}",),
                )
                conn.execute(
                    "INSERT INTO circle_parents(circle_id, parent_id) VALUES(?, ?), (?, ?)",
                    (cid, a, cid, b),
                )
                return cid

            circle1 = add_circle(me_id, coparent_id)
            circle2 = None
            if partner_id and partner_co_id:
                circle2 = add_circle(partner_id, partner_co_id)
            # You (and your partner) live where the wall display hangs.
            db.set_setting(conn, "home_parent_ids",
                           ",".join(str(i) for i in [me_id, partner_id] if i))

            kid_index = 0
            for i in range(1, 7):
                name = (form.get(f"kid{i}_name") or "").strip()
                if not name:
                    continue
                which = form.get(f"kid{i}_circle") or "1"
                cid = circle2 if (which == "2" and circle2) else circle1
                conn.execute(
                    "INSERT INTO kids(name, color, circle_id) VALUES(?, ?, ?)",
                    (name, KID_COLORS[kid_index % len(KID_COLORS)], cid),
                )
                kid_index += 1

            def setup_schedule(suffix, cid, first, second):
                pattern = form.get(f"pattern{suffix}") or ""
                if pattern not in custody.PATTERNS or not cid:
                    return
                first_id = first if form.get(f"first_parent{suffix}") != "2" else second
                second_id = second if first_id == first else first
                anchor = custody.monday_of(date.fromisoformat(
                    form.get(f"anchor_date{suffix}") or date.today().isoformat()
                ))
                custom = None
                if pattern == "custom_week":
                    custom = [
                        first if form.get(f"weekday{suffix}_{i}") != "2" else second
                        for i in range(7)
                    ]
                cycle = custody.compile_cycle(pattern, first_id, second_id, custom)
                custody.save_schedule(conn, cid, pattern, anchor, cycle,
                                      form.get(f"handoff_time{suffix}") or "18:00")

            setup_schedule("1", circle1, me_id, coparent_id)
            if circle2:
                setup_schedule("2", circle2, partner_id, partner_co_id)

            # Personal full-schedule feed per adult; shared custody-only feed.
            for pid in (me_id, coparent_id, partner_id, partner_co_id):
                if not pid:
                    continue
                p = conn.execute("SELECT name FROM parents WHERE id = ?", (pid,)).fetchone()
                conn.execute(
                    "INSERT INTO feeds(token, name, kind, owner_parent_id) "
                    "VALUES(?, ?, 'all', ?)",
                    (security.new_token(16), f"{p['name']} — full schedule", pid),
                )
            conn.execute(
                "INSERT INTO feeds(token, name, kind) VALUES(?, 'Custody schedules', 'custody')",
                (security.new_token(16),),
            )
            conn.commit()

            request.session["parent_id"] = me_id
            return RedirectResponse("/feeds", status_code=303)
        finally:
            conn.close()

    # --------------------------------------------------------------------- auth

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        conn = get_conn()
        try:
            if not parents(conn):
                return RedirectResponse("/setup", status_code=303)
            return render(request, "login.html", conn, error=None)
        finally:
            conn.close()

    @app.post("/login")
    async def login_submit(request: Request):
        conn = get_conn()
        try:
            form = await request.form()
            name = (form.get("name") or "").strip()
            rows = conn.execute(
                "SELECT * FROM parents WHERE lower(name) = lower(?) OR lower(email) = lower(?)",
                (name, name),
            ).fetchall()
            for row in rows:
                if row["password_hash"] and security.verify_password(
                    form.get("password") or "", row["password_hash"]
                ):
                    request.session["parent_id"] = row["id"]
                    return RedirectResponse("/", status_code=303)
            return render(request, "login.html", conn, error="Wrong name or password.")
        finally:
            conn.close()

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/invite/{token}", response_class=HTMLResponse)
    def invite_form(request: Request, token: str):
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM parents WHERE invite_token = ? AND password_hash IS NULL",
                (token,),
            ).fetchone()
            if not row:
                return HTMLResponse("Invite link is invalid or already used.", status_code=404)
            return render(request, "invite.html", conn, invitee=row, token=token)
        finally:
            conn.close()

    @app.post("/invite/{token}")
    async def invite_submit(request: Request, token: str):
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM parents WHERE invite_token = ? AND password_hash IS NULL",
                (token,),
            ).fetchone()
            if not row:
                return HTMLResponse("Invite link is invalid or already used.", status_code=404)
            form = await request.form()
            conn.execute(
                "UPDATE parents SET password_hash = ?, invite_token = NULL, email = ? "
                "WHERE id = ? AND password_hash IS NULL",
                (
                    security.hash_password(form["password"]),
                    (form.get("email") or "").strip() or row["email"],
                    row["id"],
                ),
            )
            conn.commit()
            request.session["parent_id"] = row["id"]
            return RedirectResponse("/", status_code=303)
        finally:
            conn.close()

    # ----------------------------------------------------------------- calendar

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        """The board is the app; management pages are the back office. A
        logged-in adult (or the installed PWA) lands on the display."""
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            return RedirectResponse("/display", status_code=303)
        finally:
            conn.close()

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page(request: Request, start: str | None = None):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            week_start = custody.monday_of(
                date.fromisoformat(start) if start else hub_today(conn)
            )
            ctx = week_context(conn, week_start, me["id"])
            mine = my_circle_ids(conn, me["id"])
            pending = 0
            if mine:
                placeholders = ",".join("?" * len(mine))
                pending = conn.execute(
                    f"SELECT COUNT(*) AS n FROM swaps WHERE status = 'pending' "
                    f"AND circle_id IN ({placeholders}) AND created_by != ?",
                    (*mine, me["id"]),
                ).fetchone()["n"]
            return render(
                request, "calendar.html", conn,
                week_start=week_start,
                prev_week=(week_start - timedelta(days=7)).isoformat(),
                next_week=(week_start + timedelta(days=7)).isoformat(),
                pending_swaps=pending,
                kids=kids(conn),
                **ctx,
            )
        finally:
            conn.close()

    @app.get("/events/new", response_class=HTMLResponse)
    def event_new_form(request: Request, date_: str | None = None):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            return render(
                request, "event_form.html", conn,
                event=None, kids=kids(conn), event_kid_ids=[],
                default_date=date_ or hub_today(conn).isoformat(),
            )
        finally:
            conn.close()

    async def _event_form_fields(request: Request):
        form = await request.form()
        all_day = 1 if form.get("all_day") else 0
        return {
            "title": form["title"].strip(),
            "category": form.get("category") if form.get("category") in CATEGORIES else "other",
            "date": form["date"],
            "start_time": None if all_day else (form.get("start_time") or None),
            "end_time": None if all_day else (form.get("end_time") or None),
            "all_day": all_day,
            "location": (form.get("location") or "").strip(),
            "notes": (form.get("notes") or "").strip(),
            "private": 1 if form.get("private") else 0,
            "kid_ids": [int(k) for k in form.getlist("kid_ids")],
            "repeat_until": form.get("repeat_until") or None,
        }

    def _set_event_kids(conn, event_id: int, kid_ids: list[int]):
        conn.execute("DELETE FROM event_kids WHERE event_id = ?", (event_id,))
        for kid_id in kid_ids:
            conn.execute(
                "INSERT INTO event_kids(event_id, kid_id) VALUES(?, ?) "
                "ON CONFLICT DO NOTHING",
                (event_id, kid_id),
            )

    @app.post("/events/new")
    async def event_create(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            f = await _event_form_fields(request)
            first = date.fromisoformat(f["date"])
            dates = [first]
            series_id = None
            if f["repeat_until"]:
                until = min(date.fromisoformat(f["repeat_until"]), first + timedelta(weeks=52))
                series_id = uuid.uuid4().hex
                d = first + timedelta(weeks=1)
                while d <= until:
                    dates.append(d)
                    d += timedelta(weeks=1)
            now = datetime.now().isoformat(timespec="seconds")
            for d in dates:
                event_id = db.insert_id(
                    conn,
                    "INSERT INTO events(title, category, date, start_time, end_time, all_day, "
                    "location, notes, private, series_id, created_by, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f["title"], f["category"], d.isoformat(), f["start_time"],
                        f["end_time"], f["all_day"], f["location"], f["notes"],
                        f["private"], series_id, me["id"], now,
                    ),
                )
                _set_event_kids(conn, event_id, f["kid_ids"])
            conn.commit()
            return RedirectResponse(f"/calendar?start={f['date']}", status_code=303)
        finally:
            conn.close()

    def _editable_event(request: Request, conn, event_id: int):
        """The event row if the current adult may see/edit it, else None."""
        ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if not ev:
            return None
        me = current_parent(request, conn)
        if not can_view_event(conn, ev, me["id"] if me else None):
            return None
        return ev

    @app.get("/events/{event_id}/edit", response_class=HTMLResponse)
    def event_edit_form(request: Request, event_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            ev = _editable_event(request, conn, event_id)
            if not ev:
                return HTMLResponse("Event not found", status_code=404)
            kid_ids = [
                r["kid_id"] for r in conn.execute(
                    "SELECT kid_id FROM event_kids WHERE event_id = ?", (event_id,)
                )
            ]
            return render(
                request, "event_form.html", conn,
                event=ev, kids=kids(conn), event_kid_ids=kid_ids,
                default_date=ev["date"],
            )
        finally:
            conn.close()

    @app.post("/events/{event_id}/edit")
    async def event_update(request: Request, event_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            ev = _editable_event(request, conn, event_id)
            if not ev:
                return HTMLResponse("Event not found", status_code=404)
            f = await _event_form_fields(request)
            conn.execute(
                "UPDATE events SET title = ?, category = ?, date = ?, start_time = ?, "
                "end_time = ?, all_day = ?, location = ?, notes = ?, private = ? "
                "WHERE id = ?",
                (
                    f["title"], f["category"], f["date"], f["start_time"], f["end_time"],
                    f["all_day"], f["location"], f["notes"], f["private"], event_id,
                ),
            )
            _set_event_kids(conn, event_id, f["kid_ids"])
            conn.commit()
            return RedirectResponse(f"/calendar?start={f['date']}", status_code=303)
        finally:
            conn.close()

    @app.post("/events/{event_id}/delete")
    async def event_delete(request: Request, event_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            ev = _editable_event(request, conn, event_id)
            if not ev:
                return HTMLResponse("Event not found", status_code=404)
            if form.get("scope") == "series" and ev["series_id"]:
                conn.execute("DELETE FROM events WHERE series_id = ?", (ev["series_id"],))
            else:
                conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
            return RedirectResponse(f"/calendar?start={ev['date']}", status_code=303)
        finally:
            conn.close()

    # -------------------------------------------------------------------- swaps

    @app.get("/swaps", response_class=HTMLResponse)
    def swaps_list(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            rows = conn.execute(
                "SELECT * FROM swaps ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, "
                "created_at DESC"
            ).fetchall()
            members = circle_members(conn)
            return render(
                request, "swaps.html", conn,
                swaps=rows,
                parent_by_id={p["id"]: p for p in parents(conn)},
                circle_labels=circle_kid_labels(conn),
                my_circles=[
                    {"id": cid, "members": members.get(cid, []),
                     "label": circle_kid_labels(conn).get(cid, f"Circle {cid}")}
                    for cid in my_circle_ids(conn, me["id"])
                ],
            )
        finally:
            conn.close()

    @app.post("/swaps/new")
    async def swap_create(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            circle_id = int(form.get("circle_id") or 0)
            mine = my_circle_ids(conn, me["id"])
            if circle_id not in mine:
                return HTMLResponse(
                    "Only that circle's co-parents can request swaps for it.",
                    status_code=403,
                )
            member_ids = {p["id"] for p in circle_members(conn).get(circle_id, [])}
            range1_parent = int(form["range1_parent"])
            if range1_parent not in member_ids:
                return HTMLResponse("Pick a parent from that circle.", status_code=400)
            now = datetime.now().isoformat(timespec="seconds")
            r1s, r1e = form["range1_start"], form["range1_end"]
            if r1e < r1s:
                r1s, r1e = r1e, r1s
            r2s = form.get("range2_start") or None
            r2e = form.get("range2_end") or None
            if r2s and r2e and r2e < r2s:
                r2s, r2e = r2e, r2s
            others = [pid for pid in member_ids if pid != range1_parent]
            range2_parent = others[0] if (r2s and r2e and others) else None
            swap_id = db.insert_id(
                conn,
                "INSERT INTO swaps(circle_id, created_by, status, reason, range1_start, "
                "range1_end, range1_parent, range2_start, range2_end, range2_parent, "
                "created_at) VALUES(?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    circle_id, me["id"], (form.get("reason") or "").strip(),
                    r1s, r1e, range1_parent,
                    r2s if (r2s and r2e) else None, r2e if (r2s and r2e) else None,
                    range2_parent, now,
                ),
            )
            thread_id = db.insert_id(
                conn,
                "INSERT INTO threads(subject, circle_id, swap_id, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (f"Swap request #{swap_id}", circle_id, swap_id, me["id"], now),
            )
            conn.execute("UPDATE swaps SET thread_id = ? WHERE id = ?", (thread_id, swap_id))
            if form.get("reason"):
                conn.execute(
                    "INSERT INTO messages(thread_id, author_id, body, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (thread_id, me["id"], form["reason"].strip(), now),
                )
            conn.commit()
            return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
        finally:
            conn.close()

    @app.get("/swaps/{swap_id}", response_class=HTMLResponse)
    def swap_detail(request: Request, swap_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            swap = conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
            if not swap:
                return HTMLResponse("Swap not found", status_code=404)
            me = current_parent(request, conn)
            member_ids = {
                p["id"] for p in circle_members(conn).get(swap["circle_id"], [])
            }
            is_member = me["id"] in member_ids
            msgs = []
            if is_member:
                msgs = conn.execute(
                    "SELECT m.*, p.name AS author_name, p.color AS author_color "
                    "FROM messages m JOIN parents p ON p.id = m.author_id "
                    "WHERE m.thread_id = ? ORDER BY m.created_at",
                    (swap["thread_id"],),
                ).fetchall()
            conflicts = (
                custody.swap_conflicts(conn, swap) if swap["status"] == "pending" else []
            )
            return render(
                request, "swap_detail.html", conn,
                swap=swap,
                parent_by_id={p["id"]: p for p in parents(conn)},
                circle_label=circle_kid_labels(conn).get(swap["circle_id"], ""),
                messages=msgs,
                is_member=is_member,
                conflicts=conflicts,
                can_decide=(swap["status"] == "pending" and is_member
                            and me["id"] != swap["created_by"] and not conflicts),
                can_cancel=(swap["status"] == "pending" and me["id"] == swap["created_by"]),
            )
        finally:
            conn.close()

    @app.post("/swaps/{swap_id}/decide")
    async def swap_decide(request: Request, swap_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            swap = conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
            if not swap or swap["status"] != "pending":
                return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
            member_ids = {
                p["id"] for p in circle_members(conn).get(swap["circle_id"], [])
            }
            if me["id"] not in member_ids or me["id"] == swap["created_by"]:
                return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
            form = await request.form()
            decision = form.get("decision")
            if decision not in ("approved", "declined"):
                return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
            now = datetime.now().isoformat(timespec="seconds")
            if decision == "approved":
                conflicts = custody.swap_conflicts(conn, swap)
                if conflicts:
                    days = ", ".join(d.isoformat() for d in conflicts[:10])
                    conn.execute(
                        "INSERT INTO messages(thread_id, author_id, body, created_at) "
                        "VALUES(?, ?, ?, ?)",
                        (swap["thread_id"], me["id"],
                         "Couldn't approve: these dates were already changed by another "
                         f"approved swap: {days}. Cancel this request or the conflicting "
                         "one first.", now),
                    )
                    conn.commit()
                    return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
            conn.execute(
                "UPDATE swaps SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                (decision, now, me["id"], swap_id),
            )
            conn.execute(
                "INSERT INTO messages(thread_id, author_id, body, created_at) VALUES(?, ?, ?, ?)",
                (swap["thread_id"], me["id"], f"{decision.capitalize()} this swap request.", now),
            )
            conn.commit()
            if decision == "approved":
                custody.apply_swap_overrides(conn, swap)
            return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
        finally:
            conn.close()

    @app.post("/swaps/{swap_id}/cancel")
    def swap_cancel(request: Request, swap_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            conn.execute(
                "UPDATE swaps SET status = 'cancelled' WHERE id = ? AND created_by = ? "
                "AND status = 'pending'",
                (swap_id, me["id"]),
            )
            conn.commit()
            return RedirectResponse("/swaps", status_code=303)
        finally:
            conn.close()

    @app.post("/swaps/{swap_id}/reply")
    async def swap_reply(request: Request, swap_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            swap = conn.execute("SELECT * FROM swaps WHERE id = ?", (swap_id,)).fetchone()
            if not swap:
                return HTMLResponse("Swap not found", status_code=404)
            member_ids = {
                p["id"] for p in circle_members(conn).get(swap["circle_id"], [])
            }
            form = await request.form()
            body = (form.get("body") or "").strip()
            if body and me["id"] in member_ids:
                conn.execute(
                    "INSERT INTO messages(thread_id, author_id, body, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (swap["thread_id"], me["id"], body,
                     datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
            return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
        finally:
            conn.close()

    # ----------------------------------------------------------------- messages

    def _thread_audiences(conn, me):
        """Spaces the current adult can post in: household + their circles."""
        labels = circle_kid_labels(conn)
        members = circle_members(conn)
        out = [{"value": "", "label": "Everyone (household)"}]
        for cid in my_circle_ids(conn, me["id"]):
            other = [p["name"] for p in members.get(cid, []) if p["id"] != me["id"]]
            label = f"Just me & {other[0]}" if other else labels.get(cid, f"Circle {cid}")
            out.append({"value": str(cid), "label": label})
        return out

    @app.get("/messages", response_class=HTMLResponse)
    def messages_list(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            mine = my_circle_ids(conn, me["id"])
            placeholders = ",".join("?" * len(mine)) if mine else "NULL"
            threads = conn.execute(
                "SELECT t.*, k.name AS kid_name, k.color AS kid_color, "
                "c.name AS circle_name, "
                "(SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS n_messages, "
                "(SELECT MAX(m.created_at) FROM messages m WHERE m.thread_id = t.id) AS last_at "
                "FROM threads t LEFT JOIN kids k ON k.id = t.kid_id "
                "LEFT JOIN circles c ON c.id = t.circle_id "
                "WHERE t.swap_id IS NULL AND "
                f"(t.circle_id IS NULL OR t.circle_id IN ({placeholders})) "
                "ORDER BY COALESCE((SELECT MAX(m.created_at) FROM messages m "
                "WHERE m.thread_id = t.id), t.created_at) DESC",
                tuple(mine),
            ).fetchall()
            return render(
                request, "messages.html", conn,
                threads=threads, kids=kids(conn),
                audiences=_thread_audiences(conn, me),
            )
        finally:
            conn.close()

    @app.post("/messages/new")
    async def thread_create(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            now = datetime.now().isoformat(timespec="seconds")
            circle_id = None
            if form.get("circle_id"):
                circle_id = int(form["circle_id"])
                if circle_id not in my_circle_ids(conn, me["id"]):
                    return HTMLResponse("Not a member of that circle.", status_code=403)
            kid_id = int(form["kid_id"]) if form.get("kid_id") else None
            thread_id = db.insert_id(
                conn,
                "INSERT INTO threads(subject, circle_id, kid_id, created_by, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (form["subject"].strip(), circle_id, kid_id, me["id"], now),
            )
            body = (form.get("body") or "").strip()
            if body:
                conn.execute(
                    "INSERT INTO messages(thread_id, author_id, body, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (thread_id, me["id"], body, now),
                )
            conn.commit()
            return RedirectResponse(f"/messages/{thread_id}", status_code=303)
        finally:
            conn.close()

    def _readable_thread(conn, me, thread_id: int):
        thread = conn.execute(
            "SELECT t.*, k.name AS kid_name, k.color AS kid_color, c.name AS circle_name "
            "FROM threads t LEFT JOIN kids k ON k.id = t.kid_id "
            "LEFT JOIN circles c ON c.id = t.circle_id WHERE t.id = ?",
            (thread_id,),
        ).fetchone()
        if not thread:
            return None
        if thread["circle_id"] and thread["circle_id"] not in my_circle_ids(conn, me["id"]):
            return None
        return thread

    @app.get("/messages/{thread_id}", response_class=HTMLResponse)
    def thread_view(request: Request, thread_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            thread = _readable_thread(conn, me, thread_id)
            if not thread:
                return HTMLResponse("Thread not found", status_code=404)
            if thread["swap_id"]:
                return RedirectResponse(f"/swaps/{thread['swap_id']}", status_code=303)
            msgs = conn.execute(
                "SELECT m.*, p.name AS author_name, p.color AS author_color FROM messages m "
                "JOIN parents p ON p.id = m.author_id WHERE m.thread_id = ? "
                "ORDER BY m.created_at",
                (thread_id,),
            ).fetchall()
            return render(request, "thread.html", conn, thread=thread, messages=msgs)
        finally:
            conn.close()

    @app.post("/messages/{thread_id}/reply")
    async def thread_reply(request: Request, thread_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            thread = _readable_thread(conn, me, thread_id)
            if not thread:
                return HTMLResponse("Thread not found", status_code=404)
            form = await request.form()
            body = (form.get("body") or "").strip()
            if body:
                conn.execute(
                    "INSERT INTO messages(thread_id, author_id, body, created_at) "
                    "VALUES(?, ?, ?, ?)",
                    (thread_id, me["id"], body,
                     datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
            return RedirectResponse(f"/messages/{thread_id}", status_code=303)
        finally:
            conn.close()

    # ---------------------------------------------------------- feeds & display

    @app.get("/feeds", response_class=HTMLResponse)
    def feeds_page(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            rows = conn.execute(
                "SELECT f.*, k.name AS kid_name FROM feeds f "
                "LEFT JOIN kids k ON k.id = f.kid_id "
                "WHERE f.owner_parent_id = ? OR f.owner_parent_id IS NULL "
                "ORDER BY f.id",
                (me["id"],),
            ).fetchall()
            base = str(request.base_url).rstrip("/")
            display_token = db.get_setting(conn, "display_token", "")
            return render(
                request, "feeds.html", conn,
                feeds=rows, base_url=base, display_token=display_token,
                kids=kids(conn),
            )
        finally:
            conn.close()

    @app.post("/feeds/new")
    async def feed_create(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            kid_id = int(form["kid_id"]) if form.get("kid_id") else None
            if kid_id:
                kid = conn.execute("SELECT * FROM kids WHERE id = ?", (kid_id,)).fetchone()
                if kid:
                    conn.execute(
                        "INSERT INTO feeds(token, name, kind, kid_id, owner_parent_id) "
                        "VALUES(?, ?, 'kid', ?, ?)",
                        (security.new_token(16), f"{kid['name']} — schedule", kid_id, me["id"]),
                    )
                    conn.commit()
            return RedirectResponse("/feeds", status_code=303)
        finally:
            conn.close()

    @app.get("/ics/{token}.ics")
    def ics_feed(token: str):
        conn = get_conn()
        try:
            feed = conn.execute("SELECT * FROM feeds WHERE token = ?", (token,)).fetchone()
            if not feed:
                return Response("Not found", status_code=404)
            body = feeds.generate_feed(conn, feed)
            return Response(
                content=body,
                media_type="text/calendar; charset=utf-8",
                headers={"Content-Disposition": f'inline; filename="{feed["kind"]}.ics"'},
            )
        finally:
            conn.close()

    @app.get("/display", response_class=HTMLResponse)
    def display(request: Request, token: str | None = None,
                week: str | None = None, month: str | None = None):
        conn = get_conn()
        try:
            expected = db.get_setting(conn, "display_token")
            authorized = (
                (token and expected and token == expected)
                or current_parent(request, conn) is not None
            )
            if not authorized:
                return HTMLResponse(
                    "<h1 style='font-family:sans-serif'>Family Hub display</h1>"
                    "<p style='font-family:sans-serif'>Missing or wrong display token. "
                    "Get the display link from the Feeds &amp; Display page.</p>",
                    status_code=403,
                )
            today = hub_today(conn)
            week_start = custody.monday_of(today)
            # The wall display is shared: private events always render as Busy.
            ctx = week_context(conn, week_start, None, today)
            next_ctx = week_context(conn, week_start + timedelta(days=7), None, today)
            upcoming_days = [
                d for d in (ctx["days"] + next_ctx["days"]) if d["date"] >= today
            ][:7]

            # Week view: offset 0 is the rolling next-7-days; ±N are
            # Monday-anchored calendar weeks reachable with the arrows.
            try:
                week_offset = max(-52, min(52, int(week or 0)))
            except ValueError:
                week_offset = 0
            if week_offset:
                wstart = week_start + timedelta(weeks=week_offset)
                week_days = week_context(conn, wstart, None, today)["days"]
                week_label = "{} – {}".format(
                    wstart.strftime("%b %-d"),
                    (wstart + timedelta(days=6)).strftime("%b %-d"),
                )
            else:
                week_days = upcoming_days
                week_label = "Next 7 days"

            # Month view: a Monday-aligned grid of week_context weeks.
            try:
                y, m = (month or "").split("-")
                month_first = date(int(y), int(m), 1)
            except ValueError:
                month_first = today.replace(day=1)
            if abs((month_first.year - today.year) * 12
                   + month_first.month - today.month) > 18:
                month_first = today.replace(day=1)
            month_last = (month_first.replace(day=28)
                          + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            month_weeks = []
            wstart = custody.monday_of(month_first)
            while wstart <= month_last:
                month_weeks.append(week_context(conn, wstart, None, today)["days"])
                wstart += timedelta(days=7)

            lunch_dates = {d["date"] for d in upcoming_days}
            lunch_dates.update(d["date"] for d in week_days)
            lunches = lunch.lunches_for(conn, sorted(lunch_dates))
            today_allday, today_timeline = [], []
            if upcoming_days:
                today_events = upcoming_days[0]["events"]
                today_allday = [e for e in today_events if e["all_day"]]
                timed = [{**e, "is_routine": False}
                         for e in today_events if not e["all_day"]]
                today_timeline = sorted(
                    timed + routines_for_day(conn, today),
                    key=lambda x: x.get("start_time") or "99:99",
                )
            banners = []
            for cid, schedule in ctx["schedules"].items():
                who = custody.custodian_on(conn, cid, today, schedule)
                if who is None:
                    continue
                run_end = today
                probe = today
                for _ in range(30):
                    nxt = probe + timedelta(days=1)
                    if custody.custodian_on(conn, cid, nxt, schedule) != who:
                        break
                    probe = nxt
                run_end = probe
                # The exchange is an event: the next parent takes over on the
                # first day of their block, at the handoff time.
                switch_date = run_end + timedelta(days=1)
                next_who = custody.custodian_on(conn, cid, switch_date, schedule)
                next_custodian = (
                    ctx["parent_by_id"].get(next_who)
                    if next_who is not None and next_who != who else None
                )
                if switch_date == today + timedelta(days=1):
                    switch_text = "tomorrow"
                elif (switch_date - today).days <= 6:
                    switch_text = switch_date.strftime("%A")
                else:
                    switch_text = switch_date.strftime("%A, %b %-d")
                banners.append({
                    "circle_id": cid,
                    "label": ctx["circle_labels"].get(cid, "Kids"),
                    "custodian": ctx["parent_by_id"].get(who),
                    "next_custodian": next_custodian,
                    "switch_text": switch_text,
                    "handoff": schedule["handoff_time"],
                })
            return render(
                request, "display.html", conn,
                days=upcoming_days, today=today, banners=banners, kids=kids(conn),
                kids_by_circle=circle_kid_rows(conn),
                parents_list=parents(conn), display_token=token or "",
                week_days=week_days, week_offset=week_offset,
                week_label=week_label,
                month_weeks=month_weeks, month_first=month_first,
                month_label=month_first.strftime("%B %Y"),
                month_prev=(month_first - timedelta(days=1)).strftime("%Y-%m"),
                month_next=(month_last + timedelta(days=1)).strftime("%Y-%m"),
                month_is_current=(month_first == today.replace(day=1)),
                tasks_cards=tasks_context(conn, today),
                weather=fetch_weather(conn),
                display_theme=db.get_setting(conn, "display_theme", "warm") or "warm",
                lunches=lunches,
                custody_mode=db.get_setting(conn, "display_custody_mode", "home_away")
                or "home_away",
                home_color=db.get_setting(conn, "display_home_color") or "#45a06c",
                away_color=db.get_setting(conn, "display_away_color") or "#b1a99e",
                home_ids=home_parent_ids(conn),
                today_allday=today_allday,
                today_timeline=today_timeline,
            )
        finally:
            conn.close()

    # ----------------------------------------------------------------- settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            members = circle_members(conn)
            labels = circle_kid_labels(conn)
            my_ids = my_circle_ids(conn, me["id"])
            admin = is_admin(conn, me["id"])
            circle_rows = []
            for c in circles(conn):
                circle_rows.append({
                    "id": c["id"],
                    "name": c["name"],
                    "label": labels.get(c["id"], ""),
                    "members": members.get(c["id"], []),
                    "schedule": custody.load_schedule(conn, c["id"]),
                    "editable": c["id"] in my_ids or admin,
                    "admin_edit": admin and c["id"] not in my_ids,
                })
            base = str(request.base_url).rstrip("/")
            return render(
                request, "settings.html", conn,
                parents_list=parents(conn), kids=kids(conn),
                circle_rows=circle_rows, all_circles=circles(conn),
                base_url=base, kid_colors=KID_COLORS, palette=PALETTE,
                timezone=db.get_setting(conn, "timezone", "") or "",
                weather_lat=db.get_setting(conn, "weather_lat", "") or "",
                weather_lon=db.get_setting(conn, "weather_lon", "") or "",
                weather_unit=db.get_setting(conn, "weather_unit", "fahrenheit"),
                my_theme=db.get_setting(conn, f"theme:{me['id']}", "light") or "light",
                display_theme=db.get_setting(conn, "display_theme", "warm") or "warm",
                custody_mode=db.get_setting(conn, "display_custody_mode", "home_away")
                or "home_away",
                home_color=db.get_setting(conn, "display_home_color") or "#45a06c",
                away_color=db.get_setting(conn, "display_away_color") or "#b1a99e",
                home_ids=home_parent_ids(conn),
                routines=get_routines(conn),
                tasks_list=conn.execute(
                    "SELECT t.*, k.name AS kid_name, k.color AS kid_color "
                    "FROM tasks t JOIN kids k ON k.id = t.kid_id "
                    "WHERE t.active = 1 ORDER BY k.name, t.section, t.id"
                ).fetchall(),
                task_rewards=task_rewards(conn),
                task_sections=TASK_SECTIONS,
                lunch_menus=lunch.get_menus(conn),
                lunch_ignore=(
                    db.get_setting(conn, "lunch_ignore")
                    if db.get_setting(conn, "lunch_ignore") is not None
                    else lunch.DEFAULT_IGNORE
                ),
            )
        finally:
            conn.close()

    @app.post("/settings/household")
    async def settings_household(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            if form.get("household_name"):
                db.set_setting(conn, "household_name", form["household_name"].strip())
            if "timezone" in form:
                tz_name = (form.get("timezone") or "").strip()
                if not tz_name:
                    db.set_setting(conn, "timezone", "")
                else:
                    try:
                        ZoneInfo(tz_name)
                        db.set_setting(conn, "timezone", tz_name)
                    except (KeyError, ValueError):
                        pass  # unknown timezone name — keep the old setting
            if "weather_lat" in form:
                lat = (form.get("weather_lat") or "").strip()
                lon = (form.get("weather_lon") or "").strip()
                try:
                    if lat and lon:
                        float(lat), float(lon)
                    db.set_setting(conn, "weather_lat", lat)
                    db.set_setting(conn, "weather_lon", lon)
                except ValueError:
                    pass  # not numbers — keep old values
                unit = form.get("weather_unit") or "fahrenheit"
                if unit in ("fahrenheit", "celsius"):
                    db.set_setting(conn, "weather_unit", unit)
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/custody/{circle_id}")
    async def settings_custody(request: Request, circle_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            if circle_id not in my_circle_ids(conn, me["id"]) and not is_admin(conn, me["id"]):
                return HTMLResponse(
                    "Only that circle's co-parents (or the household admin) can "
                    "change its custody schedule.",
                    status_code=403,
                )
            member_list = circle_members(conn).get(circle_id, [])
            if len(member_list) < 2:
                return RedirectResponse("/settings", status_code=303)
            form = await request.form()
            pattern = form.get("pattern")
            if pattern not in custody.PATTERNS:
                return RedirectResponse("/settings", status_code=303)
            member_ids = [p["id"] for p in member_list]
            first_id = int(form.get("first_parent") or member_ids[0])
            if first_id not in member_ids:
                first_id = member_ids[0]
            second_id = next(pid for pid in member_ids if pid != first_id)
            anchor = custody.monday_of(
                date.fromisoformat(form.get("anchor_date") or date.today().isoformat())
            )
            custom = None
            if pattern == "custom_week":
                custom = []
                for i in range(7):
                    val = int(form.get(f"weekday{i}") or first_id)
                    custom.append(val if val in member_ids else first_id)
            cycle = custody.compile_cycle(pattern, first_id, second_id, custom)
            custody.save_schedule(conn, circle_id, pattern, anchor, cycle,
                                  form.get("handoff_time") or "18:00")
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/kids/add")
    async def settings_kid_add(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            name = (form.get("name") or "").strip()
            if name:
                color = (form.get("color") or "").strip()
                if not HEX_COLOR.match(color):
                    color = KID_COLORS[0]
                circle_id = int(form["circle_id"]) if form.get("circle_id") else None
                kid_id = db.insert_id(
                    conn,
                    "INSERT INTO kids(name, color, circle_id) VALUES(?, ?, ?)",
                    (name, color, circle_id),
                )
                conn.execute(
                    "INSERT INTO feeds(token, name, kind, kid_id, owner_parent_id) "
                    "VALUES(?, ?, 'kid', ?, ?)",
                    (security.new_token(16), f"{name} — schedule", kid_id, me["id"]),
                )
                conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/lunch")
    async def settings_lunch(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            old_menus = lunch.get_menus(conn)
            menus = []
            for i in (1, 2, 3, 4):
                url = (form.get(f"lunch_url{i}") or "").strip()
                label = (form.get(f"lunch_label{i}") or "").strip()
                if not url or not lunch.valid_menu_url(url):
                    continue
                menu = {"url": url, "label": label}
                upload = form.get(f"lunch_logo{i}")
                logo = None
                if upload is not None and hasattr(upload, "read"):
                    logo = process_logo(await upload.read())
                if logo:
                    menu["logo"] = logo
                elif not form.get(f"lunch_logo_clear{i}"):
                    # Keep the stored logo for this menu (matched by URL).
                    old = next((m for m in old_menus
                                if m.get("url") == url), None)
                    if old and old.get("logo"):
                        menu["logo"] = old["logo"]
                menus.append(menu)
            lunch.set_menus(conn, menus)
            if "lunch_ignore" in form:
                db.set_setting(conn, "lunch_ignore",
                               (form.get("lunch_ignore") or "").strip())
            # Drop caches so the new source shows up immediately.
            conn.execute("DELETE FROM settings WHERE key LIKE 'lunch_cache:%'")
            conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.get("/settings/lunch/test", response_class=HTMLResponse)
    def settings_lunch_test(request: Request):
        """Live-fetch the configured lunch menus and show exactly what the
        API returned — for debugging Health-e Pro's undocumented endpoints."""
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            today = hub_today(conn)
            next_first = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            months = [(today.year, today.month), (next_first.year, next_first.month)]
            sections = []
            for menu in lunch.get_menus(conn):
                month_parts = []
                any_days = False
                for (y, m) in months:
                    lunches, attempts = lunch.fetch_month(menu["url"], y, m)
                    any_days = any_days or bool(lunches)
                    rows = "".join(
                        "<li><code>{}</code> → <strong>{}</strong>{}{}"
                        "<pre style='white-space:pre-wrap;background:#f4f4f5;"
                        "padding:8px;border-radius:6px'>{}</pre></li>".format(
                            html.escape(str(a["endpoint"])), html.escape(str(a["status"])),
                            f" · parsed {a['parsed_days']} days" if "parsed_days" in a else "",
                            f" · {html.escape(a['note'])}" if a.get("note") else "",
                            html.escape(str(a["excerpt"])),
                        )
                        for a in attempts
                    )
                    notes = list(dict.fromkeys(
                        a["note"] for a in attempts if a.get("note")))
                    returned = (
                        "<p>What the site returned: {}.</p>".format(
                            html.escape("; ".join(notes)))
                        if notes else "")
                    sample = "".join(
                        "<li><strong>{}</strong>: {}</li>".format(
                            html.escape(d),
                            html.escape(", ".join(lunch.prettify_item(i) for i in t)
                                        if isinstance(t, list) else str(t)),
                        )
                        for d, t in sorted(lunches.items())[:5]
                    )
                    month_parts.append(
                        f"<h3>{date(y, m, 1).strftime('%B %Y')}</h3>"
                        + (f"<p>Parsed {len(lunches)} days. Sample "
                           "(before display filtering):</p>"
                           f"<ul>{sample}</ul>" if lunches else
                           "<p><strong>No days parsed.</strong></p>" + returned)
                        + f"<details><summary>Fetch log</summary><ul>{rows}</ul>"
                        "</details>"
                    )
                discovery = ""
                # Route discovery only makes sense for Health-e Pro's SPA;
                # FastDirect pages are server-rendered, the raw excerpt above
                # is the whole story.
                if not any_days and not lunch.is_fastdirect(menu["url"]):
                    routes, notes = lunch.discover_api_routes(menu["url"])
                    route_items = "".join(
                        f"<li><code>{html.escape(r)}</code></li>" for r in routes
                    ) or "<li>none found</li>"
                    note_items = "".join(
                        f"<li>{html.escape(n)}</li>" for n in notes
                    )
                    discovery = (
                        "<h3>API routes referenced by the site's own code</h3>"
                        f"<ul>{route_items}</ul>"
                        "<details><summary>Fetch log</summary>"
                        f"<ul>{note_items}</ul></details>"
                    )
                sections.append(
                    f"<h2>{html.escape(menu.get('label') or menu['url'])}</h2>"
                    "<p class=small>Days listed as only “No School” are kept "
                    "here but never shown as a lunch on the display.</p>"
                    + "".join(month_parts) + discovery
                )
            body = "".join(sections) or "<p>No lunch menus configured yet.</p>"
            return HTMLResponse(
                "<div style='font-family:sans-serif;max-width:760px;margin:2rem auto'>"
                "<h1>Lunch menu test</h1>" + body +
                "<p><a href='/settings'>Back to settings</a></p></div>"
            )
        finally:
            conn.close()

    @app.post("/settings/events/import")
    async def settings_events_import(request: Request):
        """Bulk-add calendar events from pasted or uploaded CSV rows:
        date, title[, start[, end]]. The date can be a range
        (2026-12-21..2027-01-01) which expands to one all-day event per day.
        Rows whose date+title already exist are skipped, so re-importing an
        updated school calendar is safe."""
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            text = form.get("rows") or ""
            upload = form.get("file")
            if upload is not None and hasattr(upload, "read"):
                text += "\n" + (await upload.read()).decode("utf-8", "replace")
            kid_ids = [int(k) for k in form.getlist("kid_ids")]
            category = (form.get("category")
                        if form.get("category") in CATEGORIES else "school")
            now = datetime.now().isoformat(timespec="seconds")
            imported = skipped = bad = 0
            for row in csv.reader(io.StringIO(text)):
                row = [c.strip() for c in row]
                if (not row or not row[0] or row[0].startswith("#")
                        or row[0].lower() == "date"):
                    continue
                if len(row) < 2 or not row[1]:
                    bad += 1
                    continue
                dspec, title = row[0], row[1]
                start = _hhmm(row[2]) if len(row) > 2 else None
                end = _hhmm(row[3]) if len(row) > 3 else None
                try:
                    if ".." in dspec:
                        a, b = dspec.split("..", 1)
                        first = date.fromisoformat(a.strip())
                        last = date.fromisoformat(b.strip())
                    else:
                        first = last = date.fromisoformat(dspec)
                except ValueError:
                    bad += 1
                    continue
                if last < first or (last - first).days > 120:
                    bad += 1
                    continue
                d = first
                while d <= last:
                    exists = conn.execute(
                        "SELECT id FROM events WHERE date = ? AND title = ?",
                        (d.isoformat(), title),
                    ).fetchone()
                    if exists:
                        skipped += 1
                    else:
                        event_id = db.insert_id(
                            conn,
                            "INSERT INTO events(title, category, date, "
                            "start_time, end_time, all_day, location, notes, "
                            "private, series_id, created_by, created_at) "
                            "VALUES(?, ?, ?, ?, ?, ?, '', '', 0, NULL, ?, ?)",
                            (title, category, d.isoformat(), start,
                             end if start else None, 0 if start else 1,
                             me["id"], now),
                        )
                        _set_event_kids(conn, event_id, kid_ids)
                        imported += 1
                    d += timedelta(days=1)
            conn.commit()
            return RedirectResponse(
                f"/settings?imported={imported}&skipped={skipped}&bad={bad}",
                status_code=303,
            )
        finally:
            conn.close()

    @app.post("/display/events/new")
    async def display_event_create(request: Request):
        """Quick-add from the wall display. The display token authorizes the
        kiosk; the tapped adult becomes the event's creator. Kiosk events are
        never private — the display is a shared surface."""
        conn = get_conn()
        try:
            form = await request.form()
            token = form.get("token") or request.query_params.get("token")
            expected = db.get_setting(conn, "display_token")
            authorized = (
                (token and expected and token == expected)
                or current_parent(request, conn) is not None
            )
            if not authorized:
                return HTMLResponse("Missing or wrong display token.",
                                    status_code=403)
            title = (form.get("title") or "").strip()[:120]
            try:
                pid = int(form.get("parent_id") or 0)
            except ValueError:
                pid = 0
            adult = conn.execute(
                "SELECT id FROM parents WHERE id = ?", (pid,)
            ).fetchone()
            try:
                d = date.fromisoformat(form.get("date") or "")
            except ValueError:
                d = hub_today(conn)
            if title and adult:
                start = _hhmm(form.get("start_time") or "")
                event_id = db.insert_id(
                    conn,
                    "INSERT INTO events(title, category, date, start_time, "
                    "end_time, all_day, location, notes, private, series_id, "
                    "created_by, created_at) "
                    "VALUES(?, 'other', ?, ?, NULL, ?, '', '', 0, NULL, ?, ?)",
                    (title, d.isoformat(), start, 0 if start else 1,
                     adult["id"], datetime.now().isoformat(timespec="seconds")),
                )
                _set_event_kids(
                    conn, event_id,
                    [int(k) for k in form.getlist("kid_ids")],
                )
                conn.commit()
            dest = "/display?view=today"
            if token:
                dest += f"&token={token}"
            return RedirectResponse(dest, status_code=303)
        finally:
            conn.close()

    @app.post("/display/tasks/toggle")
    async def display_task_toggle(request: Request):
        """Tap a task chip on the wall display to check it off (or back on).
        Authorized by the display token, like the display itself."""
        conn = get_conn()
        try:
            form = await request.form()
            token = form.get("token") or request.query_params.get("token")
            expected = db.get_setting(conn, "display_token")
            authorized = (
                (token and expected and token == expected)
                or current_parent(request, conn) is not None
            )
            if not authorized:
                return JSONResponse({"error": "unauthorized"}, status_code=403)
            try:
                task_id = int(form.get("task_id") or 0)
            except ValueError:
                task_id = 0
            task = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND active = 1", (task_id,)
            ).fetchone()
            if not task:
                return JSONResponse({"error": "unknown task"}, status_code=404)
            today = hub_today(conn)
            iso = today.isoformat()
            existing = conn.execute(
                "SELECT task_id FROM task_checks WHERE task_id = ? AND date = ?",
                (task_id, iso)).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM task_checks WHERE task_id = ? AND date = ?",
                    (task_id, iso))
                done = False
            else:
                conn.execute(
                    "INSERT INTO task_checks(task_id, date) VALUES(?, ?)",
                    (task_id, iso))
                done = True
            conn.commit()
            stars = week_star_totals(conn, today)
            return JSONResponse({
                "done": done,
                "kid_id": task["kid_id"],
                "stars_week": stars.get(task["kid_id"], 0),
            })
        finally:
            conn.close()

    @app.post("/settings/tasks/add")
    async def settings_task_add(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            try:
                kid_id = int(form.get("kid_id") or 0)
            except ValueError:
                kid_id = 0
            kid = conn.execute(
                "SELECT id FROM kids WHERE id = ?", (kid_id,)).fetchone()
            label = (form.get("label") or "").strip()[:80]
            if kid and label:
                section = (form.get("section")
                           if form.get("section") in TASK_SECTIONS else "morning")
                try:
                    points = max(0, min(1000, int(form.get("points") or 10)))
                except ValueError:
                    points = 10
                days = sorted({d for d in form.getlist("days")
                               if d in {"0", "1", "2", "3", "4", "5", "6"}})
                conn.execute(
                    "INSERT INTO tasks(kid_id, label, emoji, section, points, "
                    "time, days, active) VALUES(?, ?, ?, ?, ?, ?, ?, 1)",
                    (kid["id"], label, (form.get("emoji") or "").strip()[:8],
                     section, points, _hhmm(form.get("time") or ""),
                     ",".join(days) if days else "0,1,2,3,4,5,6"),
                )
                conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/tasks/{task_id}/delete")
    async def settings_task_delete(request: Request, task_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/tasks/rewards")
    async def settings_task_rewards(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            rewards = {}
            for kid in kids(conn):
                try:
                    goal = max(0, int(form.get(f"goal_{kid['id']}") or 0))
                except ValueError:
                    goal = 0
                reward = (form.get(f"reward_{kid['id']}") or "").strip()[:60]
                if goal or reward:
                    rewards[str(kid["id"])] = {"goal": goal, "reward": reward}
            db.set_setting(conn, "task_rewards", json.dumps(rewards))
            conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/routines/add")
    async def settings_routine_add(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            label = (form.get("label") or "").strip()
            time_val = form.get("time") or "15:00"
            try:
                datetime.strptime(time_val, "%H:%M")
            except ValueError:
                time_val = "15:00"
            circle_id = int(form["circle_id"]) if form.get("circle_id") else None
            who = form.get("who") or "custodian"
            if who != "custodian":
                valid = {str(p["id"]) for p in parents(conn)}
                if who not in valid:
                    who = "custodian"
            days = sorted({
                int(x) for x in form.getlist("days") if x.isdigit() and int(x) < 7
            }) or [0, 1, 2, 3, 4]
            if label and circle_id:
                routines = get_routines(conn)
                routines.append({
                    "label": label, "time": time_val, "circle_id": circle_id,
                    "who": who, "days": days,
                })
                save_routines(conn, routines)
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/routines/{index}/delete")
    def settings_routine_delete(request: Request, index: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            routines = get_routines(conn)
            if 0 <= index < len(routines):
                routines.pop(index)
                save_routines(conn, routines)
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/appearance")
    async def settings_appearance(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            if form.get("my_theme") in ("light", "dark"):
                db.set_setting(conn, f"theme:{me['id']}", form["my_theme"])
            if form.get("display_theme") in ("warm", "dark"):
                db.set_setting(conn, "display_theme", form["display_theme"])
            if form.get("custody_mode") in ("home_away", "parent"):
                db.set_setting(conn, "display_custody_mode", form["custody_mode"])
            for key in ("display_home_color", "display_away_color"):
                value = (form.get(key) or "").strip()
                if HEX_COLOR.match(value):
                    db.set_setting(conn, key, value)
            if "custody_mode" in form:
                valid_ids = {p["id"] for p in parents(conn)}
                chosen = [
                    x for x in form.getlist("home_parents")
                    if x.isdigit() and int(x) in valid_ids
                ]
                if chosen:
                    db.set_setting(conn, "home_parent_ids", ",".join(chosen))
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/my-color")
    async def settings_my_color(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            color = (form.get("color") or "").strip()
            if HEX_COLOR.match(color):
                conn.execute("UPDATE parents SET color = ? WHERE id = ?", (color, me["id"]))
                conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/kids/{kid_id}/color")
    async def settings_kid_color(request: Request, kid_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            color = (form.get("color") or "").strip()
            if HEX_COLOR.match(color):
                conn.execute("UPDATE kids SET color = ? WHERE id = ?", (color, kid_id))
                conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/kids/{kid_id}/delete")
    def settings_kid_delete(request: Request, kid_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            conn.execute("DELETE FROM kids WHERE id = ?", (kid_id,))
            conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/password")
    async def settings_password(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            me = current_parent(request, conn)
            form = await request.form()
            new = form.get("new_password") or ""
            if len(new) >= 4:
                conn.execute(
                    "UPDATE parents SET password_hash = ? WHERE id = ?",
                    (security.hash_password(new), me["id"]),
                )
                conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/parents/{parent_id}/invite")
    def settings_parent_invite(request: Request, parent_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            # Only unclaimed accounts can get an invite link; a parent who has
            # joined manages their own password.
            conn.execute(
                "UPDATE parents SET invite_token = ? WHERE id = ? AND password_hash IS NULL",
                (security.new_token(16), parent_id),
            )
            conn.commit()
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    return app


app = create_app()
