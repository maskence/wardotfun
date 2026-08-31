"""Apply wardotfun PostGIS migrations."""
from __future__ import annotations

import logging
import argparse

try:
    from .database import PostGISDatabase, run_migrations
    from .temporal_repository import TemporalMapRepository
except ImportError:  # direct execution from backend/
    from database import PostGISDatabase, run_migrations
    from temporal_repository import TemporalMapRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backfill-map-changes", action="store_true")
    parser.add_argument("--rebuild-map-changes-v2", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    applied = run_migrations()
    if applied:
        print("Applied migrations: " + ", ".join(applied))
    else:
        print("Database is already current")
    if args.backfill_map_changes:
        result = TemporalMapRepository(PostGISDatabase()).backfill_change_observations()
        print(f"Backfilled {result['observations']} observations and {result['areas']} change areas")
    if args.rebuild_map_changes_v2:
        result = TemporalMapRepository(PostGISDatabase()).rebuild_change_derivatives()
        print(f"Rebuilt {result['observations']} observations, {result['features']} changes, and {result['areas']} areas")


if __name__ == "__main__":
    main()
