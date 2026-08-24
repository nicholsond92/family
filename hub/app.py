"""Family Hub web application (FastAPI)."""

import html
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import custody, db, feeds, security

BASE_DIR = Path(__file__).resolve().parent

KID_COLORS = ["#e63946", "#f4a261", "#2a9d8f", "#457b9d", "#8d5bd4", "#d81b8c", "#3a86ff", "#588157"]
PARENT_COLORS = ["#3a86ff", "#e63946", "#8d5bd4", "#2a9d8f"]
CATEGORIES = ["school", "activity", "medical", "other"]


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


def _db_error_page(exc: Exception) -> HTMLResponse:
    items = "".join(f"<li>{html.escape(h)}</li>" for h in _db_hints(str(exc)))
    detail = html.escape(f"{type(exc).__name__}: {exc}")
    return HTMLResponse(
        "<div style='font-family:sans-serif;max-width:640px;margin:4rem auto'>"
        "<h1>🏠 Family Hub can't reach its database</h1>"
        f"<p style='color:#a11622'><code>{detail}</code></p>"
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

    def render(request, name, conn, **ctx):
        ctx.setdefault("parent", current_parent(request, conn))
        ctx.setdefault("household", db.get_setting(conn, "household_name", "Family Hub"))
        ctx["request"] = request
        return templates.TemplateResponse(request, name, ctx)

    def events_between(conn, start: date, end: date):
        rows = conn.execute(
            "SELECT * FROM events WHERE date BETWEEN ? AND ? "
            "ORDER BY date, all_day DESC, start_time",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        kid_rows = conn.execute(
            "SELECT ek.event_id, k.id, k.name, k.color FROM event_kids ek "
            "JOIN kids k ON k.id = ek.kid_id ORDER BY k.id"
        ).fetchall()
        kids_by_event = {}
        for kr in kid_rows:
            kids_by_event.setdefault(kr["event_id"], []).append(kr)
        out = []
        for r in rows:
            out.append({**dict(r), "kids": kids_by_event.get(r["id"], [])})
        return out

    def week_context(conn, start: date, today: date | None = None):
        today = today or hub_today(conn)
        parent_by_id = {p["id"]: p for p in parents(conn)}
        schedule = custody.load_schedule(conn)
        evs = events_between(conn, start, start + timedelta(days=6))
        evs_by_date = {}
        for e in evs:
            evs_by_date.setdefault(e["date"], []).append(e)
        days = []
        for i in range(7):
            d = start + timedelta(days=i)
            custodian_id = custody.custodian_on(conn, d, schedule) if schedule else None
            days.append({
                "date": d,
                "iso": d.isoformat(),
                "is_today": d == today,
                "custodian": parent_by_id.get(custodian_id),
                "has_override": bool(custody.override_on(conn, d)),
                "events": evs_by_date.get(d.isoformat(), []),
            })
        return {"days": days, "schedule": schedule, "parent_by_id": parent_by_id}

    def fmt_time(hhmm: str | None) -> str:
        if not hhmm:
            return ""
        t = datetime.strptime(hhmm, "%H:%M")
        return t.strftime("%-I:%M%p").lower().replace(":00", "")

    templates.env.filters["fmt_time"] = fmt_time
    templates.env.filters["fmt_date"] = lambda d: (
        date.fromisoformat(d) if isinstance(d, str) else d
    ).strftime("%a %b %-d")

    @app.get("/health")
    def health():
        return {"ok": True}

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

            parent1_id = db.insert_id(
                conn,
                "INSERT INTO parents(name, email, color, password_hash) VALUES(?, ?, ?, ?)",
                (
                    form["parent1_name"].strip(),
                    (form.get("parent1_email") or "").strip() or None,
                    PARENT_COLORS[0],
                    security.hash_password(form["parent1_password"]),
                ),
            )
            invite = security.new_token(16)
            parent2_id = db.insert_id(
                conn,
                "INSERT INTO parents(name, email, color, invite_token) VALUES(?, ?, ?, ?)",
                (
                    form["parent2_name"].strip(),
                    (form.get("parent2_email") or "").strip() or None,
                    PARENT_COLORS[1],
                    invite,
                ),
            )

            kid_ids = []
            for i in range(1, 7):
                name = (form.get(f"kid{i}_name") or "").strip()
                if name:
                    new_kid_id = db.insert_id(
                        conn,
                        "INSERT INTO kids(name, color) VALUES(?, ?)",
                        (name, KID_COLORS[(i - 1) % len(KID_COLORS)]),
                    )
                    kid_ids.append((new_kid_id, name))

            pattern = form.get("pattern") or ""
            if pattern in custody.PATTERNS:
                first = parent1_id if form.get("first_parent") != "2" else parent2_id
                second = parent2_id if first == parent1_id else parent1_id
                anchor = custody.monday_of(
                    date.fromisoformat(form.get("anchor_date") or date.today().isoformat())
                )
                custom = None
                if pattern == "custom_week":
                    custom = [
                        parent1_id if form.get(f"weekday{i}") != "2" else parent2_id
                        for i in range(7)
                    ]
                cycle = custody.compile_cycle(pattern, first, second, custom)
                custody.save_schedule(
                    conn, pattern, anchor, cycle, form.get("handoff_time") or "18:00"
                )

            # Default feeds: whole family, custody only, one per kid.
            conn.execute(
                "INSERT INTO feeds(token, name, kind) VALUES(?, ?, 'all')",
                (security.new_token(16), f"{form.get('household_name') or 'Family'} — everything"),
            )
            conn.execute(
                "INSERT INTO feeds(token, name, kind) VALUES(?, ?, 'custody')",
                (security.new_token(16), "Custody schedule"),
            )
            for kid_id, kid_name in kid_ids:
                conn.execute(
                    "INSERT INTO feeds(token, name, kind, kid_id) VALUES(?, ?, 'kid', ?)",
                    (security.new_token(16), f"{kid_name}'s schedule", kid_id),
                )
            conn.commit()

            request.session["parent_id"] = parent1_id
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
            # Check every matching row so two parents sharing a name (or a
            # name matching another's email) can both log in.
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
            # Invites are only for accounts that haven't joined yet — a claimed
            # account must never be re-claimable through an invite link, or one
            # parent could take over the other's identity.
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
    def home(request: Request, start: str | None = None):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            week_start = custody.monday_of(
                date.fromisoformat(start) if start else hub_today(conn)
            )
            ctx = week_context(conn, week_start)
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM swaps WHERE status = 'pending'"
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
                    "location, notes, series_id, created_by, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f["title"], f["category"], d.isoformat(), f["start_time"],
                        f["end_time"], f["all_day"], f["location"], f["notes"],
                        series_id, me["id"], now,
                    ),
                )
                _set_event_kids(conn, event_id, f["kid_ids"])
            conn.commit()
            return RedirectResponse(f"/?start={f['date']}", status_code=303)
        finally:
            conn.close()

    @app.get("/events/{event_id}/edit", response_class=HTMLResponse)
    def event_edit_form(request: Request, event_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
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
            ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                return HTMLResponse("Event not found", status_code=404)
            f = await _event_form_fields(request)
            conn.execute(
                "UPDATE events SET title = ?, category = ?, date = ?, start_time = ?, "
                "end_time = ?, all_day = ?, location = ?, notes = ? WHERE id = ?",
                (
                    f["title"], f["category"], f["date"], f["start_time"], f["end_time"],
                    f["all_day"], f["location"], f["notes"], event_id,
                ),
            )
            _set_event_kids(conn, event_id, f["kid_ids"])
            conn.commit()
            return RedirectResponse(f"/?start={f['date']}", status_code=303)
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
            ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if not ev:
                return HTMLResponse("Event not found", status_code=404)
            if form.get("scope") == "series" and ev["series_id"]:
                conn.execute("DELETE FROM events WHERE series_id = ?", (ev["series_id"],))
            else:
                conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
            return RedirectResponse(f"/?start={ev['date']}", status_code=303)
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
            rows = conn.execute(
                "SELECT * FROM swaps ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, "
                "created_at DESC"
            ).fetchall()
            parent_by_id = {p["id"]: p for p in parents(conn)}
            return render(
                request, "swaps.html", conn,
                swaps=rows, parent_by_id=parent_by_id, parents=parents(conn),
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
            now = datetime.now().isoformat(timespec="seconds")
            r1s, r1e = form["range1_start"], form["range1_end"]
            if r1e < r1s:
                r1s, r1e = r1e, r1s
            r2s = form.get("range2_start") or None
            r2e = form.get("range2_end") or None
            if r2s and r2e and r2e < r2s:
                r2s, r2e = r2e, r2s
            range1_parent = int(form["range1_parent"])
            others = [p for p in parents(conn) if p["id"] != range1_parent]
            range2_parent = others[0]["id"] if (r2s and r2e and others) else None
            swap_id = db.insert_id(
                conn,
                "INSERT INTO swaps(created_by, status, reason, range1_start, range1_end, "
                "range1_parent, range2_start, range2_end, range2_parent, created_at) "
                "VALUES(?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    me["id"], (form.get("reason") or "").strip(),
                    r1s, r1e, range1_parent,
                    r2s if (r2s and r2e) else None, r2e if (r2s and r2e) else None,
                    range2_parent, now,
                ),
            )
            thread_id = db.insert_id(
                conn,
                "INSERT INTO threads(subject, swap_id, created_by, created_at) "
                "VALUES(?, ?, ?, ?)",
                (f"Swap request #{swap_id}", swap_id, me["id"], now),
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
            msgs = conn.execute(
                "SELECT m.*, p.name AS author_name, p.color AS author_color FROM messages m "
                "JOIN parents p ON p.id = m.author_id WHERE m.thread_id = ? "
                "ORDER BY m.created_at",
                (swap["thread_id"],),
            ).fetchall()
            me = current_parent(request, conn)
            conflicts = (
                custody.swap_conflicts(conn, swap) if swap["status"] == "pending" else []
            )
            return render(
                request, "swap_detail.html", conn,
                swap=swap,
                parent_by_id={p["id"]: p for p in parents(conn)},
                messages=msgs,
                conflicts=conflicts,
                can_decide=(swap["status"] == "pending" and me["id"] != swap["created_by"]
                            and not conflicts),
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
            if me["id"] == swap["created_by"]:
                return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
            form = await request.form()
            decision = form.get("decision")
            if decision not in ("approved", "declined"):
                return RedirectResponse(f"/swaps/{swap_id}", status_code=303)
            now = datetime.now().isoformat(timespec="seconds")
            if decision == "approved":
                # Never silently overwrite dates already agreed via another swap.
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
            form = await request.form()
            body = (form.get("body") or "").strip()
            if swap and body:
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

    @app.get("/messages", response_class=HTMLResponse)
    def messages_list(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            threads = conn.execute(
                "SELECT t.*, k.name AS kid_name, k.color AS kid_color, "
                "(SELECT COUNT(*) FROM messages m WHERE m.thread_id = t.id) AS n_messages, "
                "(SELECT MAX(m.created_at) FROM messages m WHERE m.thread_id = t.id) AS last_at "
                "FROM threads t LEFT JOIN kids k ON k.id = t.kid_id "
                "WHERE t.swap_id IS NULL "
                "ORDER BY COALESCE(last_at, t.created_at) DESC"
            ).fetchall()
            return render(request, "messages.html", conn, threads=threads, kids=kids(conn))
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
            kid_id = int(form["kid_id"]) if form.get("kid_id") else None
            thread_id = db.insert_id(
                conn,
                "INSERT INTO threads(subject, kid_id, created_by, created_at) "
                "VALUES(?, ?, ?, ?)",
                (form["subject"].strip(), kid_id, me["id"], now),
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

    @app.get("/messages/{thread_id}", response_class=HTMLResponse)
    def thread_view(request: Request, thread_id: int):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            thread = conn.execute(
                "SELECT t.*, k.name AS kid_name, k.color AS kid_color FROM threads t "
                "LEFT JOIN kids k ON k.id = t.kid_id WHERE t.id = ?",
                (thread_id,),
            ).fetchone()
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
            rows = conn.execute(
                "SELECT f.*, k.name AS kid_name FROM feeds f "
                "LEFT JOIN kids k ON k.id = f.kid_id ORDER BY f.id"
            ).fetchall()
            base = str(request.base_url).rstrip("/")
            display_token = db.get_setting(conn, "display_token", "")
            return render(
                request, "feeds.html", conn,
                feeds=rows, base_url=base, display_token=display_token,
            )
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
    def display(request: Request, token: str | None = None):
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
            ctx = week_context(conn, week_start, today)
            next_ctx = week_context(conn, week_start + timedelta(days=7), today)
            upcoming_days = (ctx["days"] + next_ctx["days"])
            # Display shows today plus the next 6 days.
            upcoming_days = [d for d in upcoming_days if d["date"] >= today][:7]
            custodian_today = next(
                (d["custodian"] for d in upcoming_days if d["date"] == today), None
            )
            # How long the current custody run lasts.
            run_end = today
            if custodian_today:
                schedule = ctx["schedule"]
                probe = today
                for _ in range(30):
                    nxt = probe + timedelta(days=1)
                    who = custody.custodian_on(conn, nxt, schedule)
                    if who != custodian_today["id"]:
                        break
                    probe = nxt
                run_end = probe
            return render(
                request, "display.html", conn,
                days=upcoming_days,
                today=today,
                custodian_today=custodian_today,
                run_end=run_end,
                handoff=(ctx["schedule"] or {}).get("handoff_time", "18:00"),
                kids=kids(conn),
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
            schedule = custody.load_schedule(conn)
            base = str(request.base_url).rstrip("/")
            return render(
                request, "settings.html", conn,
                parents_list=parents(conn), kids=kids(conn),
                schedule=schedule, base_url=base, kid_colors=KID_COLORS,
                timezone=db.get_setting(conn, "timezone", "") or "",
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
            return RedirectResponse("/settings", status_code=303)
        finally:
            conn.close()

    @app.post("/settings/custody")
    async def settings_custody(request: Request):
        conn = get_conn()
        try:
            redirect = guard(request, conn)
            if redirect:
                return redirect
            form = await request.form()
            plist = parents(conn)
            if len(plist) < 2:
                return RedirectResponse("/settings", status_code=303)
            pattern = form.get("pattern")
            if pattern not in custody.PATTERNS:
                return RedirectResponse("/settings", status_code=303)
            first_id = int(form.get("first_parent") or plist[0]["id"])
            second = next((p["id"] for p in plist if p["id"] != first_id), plist[0]["id"])
            anchor = custody.monday_of(
                date.fromisoformat(form.get("anchor_date") or date.today().isoformat())
            )
            custom = None
            if pattern == "custom_week":
                custom = [int(form.get(f"weekday{i}") or first_id) for i in range(7)]
            cycle = custody.compile_cycle(pattern, first_id, second, custom)
            custody.save_schedule(conn, pattern, anchor, cycle,
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
            form = await request.form()
            name = (form.get("name") or "").strip()
            if name:
                color = form.get("color") or KID_COLORS[0]
                kid_id = db.insert_id(
                    conn, "INSERT INTO kids(name, color) VALUES(?, ?)", (name, color)
                )
                conn.execute(
                    "INSERT INTO feeds(token, name, kind, kid_id) VALUES(?, ?, 'kid', ?)",
                    (security.new_token(16), f"{name}'s schedule", kid_id),
                )
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
