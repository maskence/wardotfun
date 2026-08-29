"""Apply wardotfun PostGIS migrations."""
from __future__ import annotations

import logging

try:
    from .database import run_migrations
except ImportError:  # direct execution from backend/
    from database import run_migrations


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    applied = run_migrations()
    if applied:
        print("Applied migrations: " + ", ".join(applied))
    else:
        print("Database is already current")


if __name__ == "__main__":
    main()
