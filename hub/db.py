"""SQLite storage for the family hub."""

import os
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "hub.db")


def db_path() -> str:
    return os.environ.get("HUB_DB", DEFAULT_DB_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parents(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT UNIQUE,
  color TEXT NOT NULL DEFAULT '#3a86ff',
  password_hash TEXT,
  invite_token TEXT
);

CREATE TABLE IF NOT EXISTS kids(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  color TEXT NOT NULL DEFAULT '#2a9d8f'
);

-- Single active custody schedule (id is always 1).
CREATE TABLE IF NOT EXISTS custody_schedule(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  pattern TEXT NOT NULL,
  anchor_date TEXT NOT NULL,
  cycle TEXT NOT NULL,
  handoff_time TEXT NOT NULL DEFAULT '18:00'
);

CREATE TABLE IF NOT EXISTS custody_overrides(
  date TEXT PRIMARY KEY,
  parent_id INTEGER NOT NULL REFERENCES parents(id),
  swap_id INTEGER,
  note TEXT
);

CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'other',
  date TEXT NOT NULL,
  start_time TEXT,
  end_time TEXT,
  all_day INTEGER NOT NULL DEFAULT 0,
  location TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
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
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject TEXT NOT NULL,
  kid_id INTEGER REFERENCES kids(id) ON DELETE SET NULL,
  swap_id INTEGER,
  created_by INTEGER REFERENCES parents(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  author_id INTEGER NOT NULL REFERENCES parents(id),
  body TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  kid_id INTEGER REFERENCES kids(id) ON DELETE CASCADE
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
