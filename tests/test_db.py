import socket

from hub import db


def test_ipv4_pinning_adds_hostaddr(monkeypatch):
    def fake_getaddrinfo(host, port, family, kind):
        assert host == "db.example.com"
        assert port == 6543
        assert family == socket.AF_INET
        return [(socket.AF_INET, kind, 6, "", ("192.0.2.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    conninfo = db._ipv4_pinned_conninfo("postgresql://u:p@db.example.com:6543/db")
    assert "hostaddr=192.0.2.10" in conninfo
    assert "db.example.com" in conninfo


def test_ipv4_pinning_falls_back_without_a_record(monkeypatch):
    def fake_getaddrinfo(*args, **kwargs):
        raise socket.gaierror("no A record")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    url = "postgresql://u:p@ipv6only.example.com:6543/db"
    assert db._ipv4_pinned_conninfo(url) == url


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


def test_database_url_strips_quotes_and_whitespace(monkeypatch):
    monkeypatch.setenv("HUB_DATABASE_URL", '  "postgresql://u@h/db"\n')
    assert db.database_url() == "postgresql://u@h/db"


def test_database_url_empty_when_unset(monkeypatch):
    monkeypatch.delenv("HUB_DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    assert db.database_url() == ""
