from hub import db


def test_database_url_prefers_hub_var(monkeypatch):
    monkeypatch.setenv("HUB_DATABASE_URL", "postgresql://a@h/db1")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://b@h/db2")
    assert db.database_url() == "postgresql://a@h/db1"


def test_database_url_falls_back_to_vercel_supabase_var(monkeypatch):
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgres://u:p@h:6543/db")
    assert db.database_url() == "postgres://u:p@h:6543/db"


def test_database_url_strips_non_libpq_params(monkeypatch):
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "POSTGRES_URL",
        "postgres://u:p@h:6543/db?pgbouncer=true&sslmode=require&connection_limit=1",
    )
    assert db.database_url() == "postgres://u:p@h:6543/db?sslmode=require"


def test_database_url_empty_when_unset(monkeypatch):
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    assert db.database_url() == ""
