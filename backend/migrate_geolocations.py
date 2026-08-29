"""Atomically migrate the legacy GeoConfirmed SQLite mirror into PostGIS."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from .database import PostGISDatabase
    from .geolocation_service import DB_PATH, parse_timestamp
except ImportError:
    from database import PostGISDatabase
    from geolocation_service import DB_PATH, parse_timestamp


def _aware(value):
    parsed = parse_timestamp(value)
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def migrate_sqlite_geolocations(
    sqlite_path: str | Path = DB_PATH,
    database: PostGISDatabase | None = None,
) -> dict:
    database = database or PostGISDatabase()
    database.migrate()
    source = sqlite3.connect(Path(sqlite_path))
    source.row_factory = sqlite3.Row
    try:
        metadata = [dict(row) for row in source.execute("SELECT * FROM metadata")]
        icons = [dict(row) for row in source.execute("SELECT * FROM icons")]
        events = [dict(row) for row in source.execute("SELECT * FROM events")]
    finally:
        source.close()

    source_uuids = {row["uuid"] for row in events}
    with database.connect(dict_rows=True) as conn:
        acquired = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('wardotfun:migrate:geolocations', 0)) AS ok"
        ).fetchone()["ok"]
        if not acquired:
            raise RuntimeError("another GeoConfirmed migration is already running")

        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO geolocation_metadata(key, value) VALUES (%(key)s, %(value)s)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                metadata,
            )
        icon_records = []
        for row in icons:
            item = dict(row)
            item["updated_timestamp"] = (
                datetime.fromtimestamp(item["updated_at"], timezone.utc)
                if item.get("updated_at") else None
            )
            icon_records.append(item)
        known_icons = {item["id"] for item in icon_records}
        for icon_id in sorted({row["icon_id"] for row in events if row.get("icon_id")} - known_icons):
            icon_records.append({
                "id": icon_id,
                "name": None,
                "upstream_path": None,
                "local_name": None,
                "content_type": None,
                "updated_timestamp": None,
            })
        with conn.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO geolocation_icons(
                id, name, upstream_path, local_name, content_type, updated_at
            ) VALUES (
                %(id)s, %(name)s, %(upstream_path)s, %(local_name)s,
                %(content_type)s, %(updated_timestamp)s
            ) ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                upstream_path = excluded.upstream_path,
                local_name = excluded.local_name,
                content_type = excluded.content_type,
                updated_at = excluded.updated_at
                """,
                icon_records,
            )
        event_records = []
        for row in events:
            item = dict(row)
            item["occurred_at"] = _aware(item["timestamp"])
            item["updated_timestamp"] = datetime.fromtimestamp(
                item["updated_at"], timezone.utc
            )
            event_records.append(item)
        with conn.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO geolocation_events(
                uuid, event_date, timestamp_text, occurred_at, time_precision,
                location, description, faction_id, faction_name, faction_color,
                icon_id, icon_name, icon_path, origin, equipment, units, plus_code,
                evidence_links, geolocation_links, gear_items, orbat_units,
                source_hash, updated_at
            ) VALUES (
                %(uuid)s, %(event_date)s, %(timestamp)s, %(occurred_at)s,
                %(time_precision)s,
                ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
                %(description)s, %(faction_id)s, %(faction_name)s, %(faction_color)s,
                %(icon_id)s, %(icon_name)s, %(icon_path)s, %(origin)s, %(equipment)s,
                %(units)s, %(plus_code)s, %(evidence_links)s::jsonb,
                %(geolocation_links)s::jsonb, %(gear_items)s::jsonb,
                %(orbat_units)s::jsonb, %(source_hash)s, %(updated_timestamp)s
            ) ON CONFLICT(uuid) DO UPDATE SET
                event_date = excluded.event_date,
                timestamp_text = excluded.timestamp_text,
                occurred_at = excluded.occurred_at,
                time_precision = excluded.time_precision,
                location = excluded.location,
                description = excluded.description,
                faction_id = excluded.faction_id,
                faction_name = excluded.faction_name,
                faction_color = excluded.faction_color,
                icon_id = excluded.icon_id,
                icon_name = excluded.icon_name,
                icon_path = excluded.icon_path,
                origin = excluded.origin,
                equipment = excluded.equipment,
                units = excluded.units,
                plus_code = excluded.plus_code,
                evidence_links = excluded.evidence_links,
                geolocation_links = excluded.geolocation_links,
                gear_items = excluded.gear_items,
                orbat_units = excluded.orbat_units,
                source_hash = excluded.source_hash,
                updated_at = excluded.updated_at
                """,
                event_records,
            )

        migrated = {
            row["uuid"]: row["source_hash"]
            for row in conn.execute(
                "SELECT uuid, source_hash FROM geolocation_events WHERE uuid = ANY(%s)",
                (list(source_uuids),),
            )
        } if source_uuids else {}
        expected = {row["uuid"]: row["source_hash"] for row in events}
        if migrated != expected:
            raise RuntimeError(
                f"GeoConfirmed verification failed: expected {len(expected)} exact UUID/hash rows, got {len(migrated)}"
            )
        # The context manager commits only after the equality check succeeds.

    return {
        "events": len(events),
        "icons": len(icon_records),
        "metadata": len(metadata),
        "uuids_verified": len(source_uuids),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)
    result = migrate_sqlite_geolocations(args.sqlite)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
