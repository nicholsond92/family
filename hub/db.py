"""Storage for the family hub.

Two interchangeable backends behind one tiny interface:

- **SQLite** (default) — zero-setup local file, used when no
  ``HUB_DATABASE_URL`` is set. Good for self-hosting on a box with a disk.
- **Postgres** (e.g. Supabase) — used when ``HUB_DATABASE_URL`` is a
  ``postgres://`` / ``postgresql://`` URL. Required on serverless hosts like
  Vercel, which have no persistent disk. Use Supabase's *transaction pooler*
  connection string (port 6543) for serverless deployments.

Application code writes SQLite-style SQL (``?`` placeholders); the Postgres
wrapper translates placeholders and both backends return mapping-style rows.
Use :func:`insert_id` instead of ``cursor.lastrowid`` so inserts work on both.
"""

import os
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "hub.db")

SCHEMA_VERSION = "2"

# Schema targets shared by both backends: TEXT dates/times (ISO strings),
# INTEGER booleans. {ID} expands to each backend's autoincrement PK.
#
# v2 model: a household holds several ADULTS (table `parents`) and one or two
# co-parenting CIRCLES. A circle is a pair of co-parents; each kid belongs to
# one circle; custody schedules, overrides, and swaps are per-circle. Events
# can be private (details visible only to the kids' co-parents + creator;
# everyone else sees a Busy block). Feeds are per-adult so tokens can't leak
# private details.
_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parents(
  id {ID},
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  color TEXT NOT NULL DEFAULT '#3a86ff',
  password_hash TEXT,
  invite_token TEXT
);

CREATE TABLE IF NOT EXISTS circles(
  id {ID},
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS circle_parents(
  circle_id INTEGER NOT NULL REFERENCES circles(id) ON DELETE CASCADE,
  parent_id INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
  PRIMARY KEY (circle_id, parent_id)
);

CREATE TABLE IF NOT EXISTS kids(
  id {ID},
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#2a9d8f',
  circle_id INTEGER REFERENCES circles(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS custody_schedule(
  circle_id INTEGER PRIMARY KEY REFERENCES circles(id) ON DELETE CASCADE,
  pattern TEXT NOT NULL,
  anchor_date TEXT NOT NULL,
  cycle TEXT NOT NULL,
  handoff_time TEXT NOT NULL DEFAULT '18:00'
);

CREATE TABLE IF NOT EXISTS custody_overrides(
  circle_id INTEGER NOT NULL REFERENCES circles(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  parent_id INTEGER NOT NULL REFERENCES parents(id),
  swap_id INTEGER,
  note TEXT,
  PRIMARY KEY (circle_id, date)
);

CREATE TABLE IF NOT EXISTS events(
  id {ID},
  title TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'other',
  date TEXT NOT NULL,
  start_time TEXT,
  end_time TEXT,
  all_day INTEGER NOT NULL DEFAULT 0,
  location TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  private INTEGER NOT NULL DEFAULT 0,
  series_id TEXT,
  created_by INTEGER REFERENCES parents(id),
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);

CREATE TABLE IF NOT EXISTS event_kids(
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  kid_id INTEGER NOT NULL REFERENCES kids(id) ON DELETE CASCADE,
  PRIMARY KEY (event_id, kid_id)
);

CREATE TABLE IF NOT EXISTS swaps(
  id {ID},
  circle_id INTEGER NOT NULL REFERENCES circles(id) ON DELETE CASCADE,
  created_by INTEGER NOT NULL REFERENCES parents(id),
  status TEXT NOT NULL DEFAULT 'pending',
  reason TEXT NOT NULL DEFAULT '',
  range1_start TEXT NOT NULL,
  range1_end TEXT NOT NULL,
  range1_parent INTEGER NOT NULL REFERENCES parents(id),
  range2_start TEXT,
  range2_end TEXT,
  range2_parent INTEGER REFERENCES parents(id),
  created_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by INTEGER REFERENCES parents(id),
  thread_id INTEGER
);

CREATE TABLE IF NOT EXISTS threads(
  id {ID},
  subject TEXT NOT NULL,
  circle_id INTEGER REFERENCES circles(id) ON DELETE CASCADE,
  kid_id INTEGER REFERENCES kids(id) ON DELETE SET NULL,
  swap_id INTEGER,
  created_by INTEGER REFERENCES parents(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages(
  id {ID},
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  author_id INTEGER NOT NULL REFERENCES parents(id),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds(
  id {ID},
  token TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  kid_id INTEGER REFERENCES kids(id) ON DELETE CASCADE,
  owner_parent_id INTEGER REFERENCES parents(id) ON DELETE CASCADE
);
"""

# Dropped (children first) when migrating an empty pre-v2 database.
_APP_TABLES = [
    "messages", "threads", "swaps", "custody_overrides", "custody_schedule",
    "event_kids", "events", "feeds", "kids", "circle_parents", "circles",
    "parents",
]

SCHEMA = _SCHEMA_TEMPLATE.format(ID="INTEGER PRIMARY KEY AUTOINCREMENT")
PG_SCHEMA = _SCHEMA_TEMPLATE.format(
    ID="INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
)


def db_path() -> str:
    return os.environ.get("HUB_DB", DEFAULT_DB_PATH)


# Query params some platforms append (Prisma/pooler hints) that libpq/psycopg
# would reject as unknown connection options.
_NON_LIBPQ_PARAMS = {"pgbouncer", "connection_limit", "pool_timeout", "supa", "schema"}


def _sanitize_pg_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query)
            if k.lower() not in _NON_LIBPQ_PARAMS]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


def database_url() -> str:
    """Postgres URL if configured: HUB_DATABASE_URL, or POSTGRES_URL as set
    automatically by the Vercel <-> Supabase integration."""
    url = os.environ.get("HUB_DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
    # Pasted values often carry whitespace or wrapping quotes.
    url = url.strip().strip("'\"").strip()
    if url.startswith(("postgres://", "postgresql://")):
        return _sanitize_pg_url(url)
    return url


class PgConnection:
    """Thin wrapper giving a psycopg connection the sqlite3-ish interface the
    app uses: ``conn.execute(sql_with_question_marks, params)``."""

    is_postgres = True

    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _translate(sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params=()):
        return self.raw.execute(self._translate(sql), params)

    def executescript(self, sql: str):
        return self.raw.execute(sql)

    def commit(self):
        self.raw.commit()

    def close(self):
        self.raw.close()


def _ipv4_pinned_conninfo(url: str) -> str:
    """Pin the connection to an IPv4 address via libpq's hostaddr.

    Serverless platforms (notably Vercel) have no outbound IPv6, but database
    hostnames often resolve to AAAA records first, failing with "Cannot assign
    requested address". Resolving an A record ourselves and passing hostaddr
    forces IPv4 while the hostname is still used for TLS verification. Falls
    back to the plain URL when no IPv4 address can be resolved.
    """
    import socket

    from psycopg.conninfo import make_conninfo

    try:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            return url
        infos = socket.getaddrinfo(
            host, parts.port or 5432, socket.AF_INET, socket.SOCK_STREAM
        )
        if not infos:
            return url
        return make_conninfo(url, hostaddr=infos[0][4][0])
    except (OSError, ValueError):
        return url


def _pg_connect(url: str) -> PgConnection:
    import psycopg
    from psycopg.rows import dict_row

    # prepare_threshold=None keeps psycopg compatible with transaction-mode
    # poolers (Supabase's pooler / pgbouncer), which serverless deploys use.
    raw = psycopg.connect(
        _ipv4_pinned_conninfo(url), row_factory=dict_row, prepare_threshold=None
    )
    return PgConnection(raw)


_initialized_targets: set[str] = set()


def connect(path: str | None = None):
    """Open a connection; the schema is ensured once per process per target.

    An explicit ``path`` always means SQLite (used by tests); otherwise
    ``HUB_DATABASE_URL`` selects Postgres when set.
    """
    url = database_url()
    if url.startswith(("postgres://", "postgresql://")) and path is None:
        conn = _pg_connect(url)
        target = url
    else:
        target = path or db_path()
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    if target not in _initialized_targets:
        init_db(conn)
        _initialized_targets.add(target)
    return conn


def init_db(conn) -> None:
    schema = PG_SCHEMA if getattr(conn, "is_postgres", False) else SCHEMA
    # Settings first, so the schema version can be read on legacy databases.
    settings_ddl = schema.split(";", 1)[0] + ";"
    conn.executescript(settings_ddl)
    conn.commit()
    version = get_setting(conn, "schema_version")
    if version != SCHEMA_VERSION:
        _migrate_pre_v2(conn)
    conn.executescript(schema)
    conn.commit()
    if version != SCHEMA_VERSION:
        set_setting(conn, "schema_version", SCHEMA_VERSION)


def _table_empty_or_missing(conn, table: str) -> bool:
    try:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return row["n"] == 0
    except Exception:  # noqa: BLE001 — table doesn't exist
        if getattr(conn, "is_postgres", False):
            conn.raw.rollback()
        return True


def _migrate_pre_v2(conn) -> None:
    """Upgrade a pre-v2 database.

    The v1 -> v2 model change (co-parent circles) restructures most tables.
    A v1 database that was never set up (no parents) is simply rebuilt;
    settings (session secret etc.) are preserved. A v1 database WITH data is
    left untouched so nothing is destroyed — the schema create below will
    surface errors that make the situation visible instead of silently
    corrupting it.
    """
    if not _table_empty_or_missing(conn, "parents"):
        return
    for table in _APP_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def insert_id(conn, sql: str, params=()) -> int:
    """Run an INSERT and return the new row's id on either backend."""
    if getattr(conn, "is_postgres", False):
        cur = conn.execute(sql + " RETURNING id", params)
        return cur.fetchone()["id"]
    return conn.execute(sql, params).lastrowid


def get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
