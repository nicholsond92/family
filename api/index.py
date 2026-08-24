"""Vercel serverless entrypoint — exposes the Family Hub ASGI app.

Requires HUB_DATABASE_URL to point at Postgres (e.g. Supabase's transaction
pooler connection string); Vercel functions have no persistent disk for
SQLite.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.app import app  # noqa: E402,F401
