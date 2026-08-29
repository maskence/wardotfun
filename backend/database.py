"""Small, lazy PostgreSQL/PostGIS connection and migration helpers.

The web application can still be imported without psycopg installed.  This is
intentional: local development keeps the legacy SQLite/pickle fallback, while
production enables PostGIS by setting ``WARDOTFUN_DATABASE_URL``.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class DatabaseUnavailable(RuntimeError):
    """Raised when PostGIS was requested but cannot be used."""


def database_url() -> str | None:
    return os.getenv("WARDOTFUN_DATABASE_URL") or os.getenv("DATABASE_URL") or None


def postgis_enabled() -> bool:
    return bool(database_url())


def _psycopg():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise DatabaseUnavailable(
            "PostGIS is configured but psycopg is not installed; install backend/requirements.txt"
        ) from exc
    return psycopg


class PostGISDatabase:
    """Creates short-lived psycopg connections.

    The VPS workload is modest and nginx/browser caching absorbs most tile
    traffic. Keeping this wrapper pool-free also makes worker and CLI process
    shutdown deterministic. A PgBouncer deployment can be added transparently.
    """

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or database_url()
        if not self.dsn:
            raise DatabaseUnavailable("WARDOTFUN_DATABASE_URL is not configured")

    def connect(self, *, dict_rows: bool = False, autocommit: bool = False):
        psycopg = _psycopg()
        kwargs = {"autocommit": autocommit, "application_name": "wardotfun"}
        if dict_rows:
            from psycopg.rows import dict_row

            kwargs["row_factory"] = dict_row
        return psycopg.connect(self.dsn, **kwargs)

    @contextmanager
    def transaction(self, *, dict_rows: bool = False) -> Iterator:
        with self.connect(dict_rows=dict_rows) as conn:
            with conn.transaction():
                yield conn

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT postgis_version(), current_setting('TimeZone')"
                ).fetchone()
                return bool(row and row[0])
        except Exception:
            log.exception("PostGIS health check failed")
            return False

    def migrate(self) -> list[str]:
        """Apply ordered SQL migrations transactionally and return their names."""
        applied_now: list[str] = []
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name text PRIMARY KEY,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                row[0] for row in conn.execute("SELECT name FROM schema_migrations")
            }
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                log.info("Applying database migration %s", path.name)
                with conn.transaction():
                    # prepare=False permits a migration file to contain multiple
                    # statements while retaining transaction semantics.
                    conn.execute(sql, prepare=False)
                    conn.execute(
                        "INSERT INTO schema_migrations(name) VALUES (%s)",
                        (path.name,),
                    )
                applied_now.append(path.name)
            conn.commit()
        return applied_now


def run_migrations() -> list[str]:
    return PostGISDatabase().migrate()
