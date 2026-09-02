"""Optional acceptance checks against a disposable PostGIS database.

Set WARDOTFUN_TEST_DATABASE_URL to run these in CI or during the seven-day
shadow phase. They are skipped in the lightweight local suite.
"""
import os
import sys
import unittest
import uuid
import json
import math
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


@unittest.skipUnless(os.getenv("WARDOTFUN_TEST_DATABASE_URL"), "PostGIS test DSN not configured")
class PostGISIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database import PostGISDatabase
        from temporal_repository import TemporalMapRepository

        cls.database = PostGISDatabase(os.environ["WARDOTFUN_TEST_DATABASE_URL"])
        cls.database.migrate()
        cls.repository = TemporalMapRepository(cls.database)

    def test_atomic_deduplicated_snapshots_and_mvt_isolation(self):
        source_id = f"test-{uuid.uuid4().hex[:12]}"
        self.repository.register_source(
            source_id=source_id,
            kind="mapper",
            display_name="Integration Test",
            source_url="https://example.test",
            attribution="test",
            upstream_type="test",
        )

        def payload(lon):
            return {
                "mapper_id": source_id,
                "layers": [{
                    "id": "points", "label": "Points", "geom_type": "point",
                    "paint": {"circle_color": "#fff"},
                    "data": {"type": "FeatureCollection", "features": [{
                        "type": "Feature", "id": "stable-1",
                        "geometry": {"type": "Point", "coordinates": [lon, 50.45]},
                        "properties": {"name": "Kyiv"},
                    }]},
                }],
            }

        captured = datetime.now(timezone.utc)
        first = self.repository.ingest_overlay(payload(30.52), captured_at=captured)
        duplicate = self.repository.ingest_overlay(payload(30.52), captured_at=captured)
        second = self.repository.ingest_overlay(payload(30.60), captured_at=captured)
        self.assertEqual(first.status, "stored")
        self.assertEqual(duplicate.status, "unchanged")
        self.assertEqual(duplicate.snapshot_id, first.snapshot_id)
        self.assertNotEqual(second.snapshot_id, first.snapshot_id)

        # z=6/x=37/y=21 contains Kyiv. Both immutable snapshots remain queryable.
        old_tile, old_etag = self.repository.get_tile(source_id, first.snapshot_id, 6, 37, 21)
        new_tile, new_etag = self.repository.get_tile(source_id, second.snapshot_id, 6, 37, 21)
        self.assertTrue(old_tile)
        self.assertTrue(new_tile)
        self.assertNotEqual(old_etag, new_etag)

    def test_kyiv_end_of_day_resolution_and_pre_history_unavailable_state(self):
        source_id = f"test-{uuid.uuid4().hex[:12]}"
        self.repository.register_source(
            source_id=source_id, kind="mapper", display_name="Boundary Test",
            source_url=None, attribution="test", upstream_type="test",
        )

        def payload(lon):
            return {
                "mapper_id": source_id,
                "layers": [{
                    "id": "point", "label": "Point", "geom_type": "point", "paint": {},
                    "data": {"type": "FeatureCollection", "features": [{
                        "type": "Feature", "id": "one",
                        "geometry": {"type": "Point", "coordinates": [lon, 50.45]},
                        "properties": {},
                    }]},
                }],
            }

        before_midnight = self.repository.ingest_overlay(
            payload(30.5), captured_at=datetime(2026, 8, 20, 20, 59, tzinfo=timezone.utc)
        )
        after_midnight = self.repository.ingest_overlay(
            payload(30.6), captured_at=datetime(2026, 8, 20, 21, 0, tzinfo=timezone.utc)
        )
        day_one = self.repository.get_map_state("20260820")
        day_two = self.repository.get_map_state("20260821")
        source_one = next(item for item in day_one["mappers"] if item["id"] == source_id)
        source_two = next(item for item in day_two["mappers"] if item["id"] == source_id)
        self.assertEqual(source_one["snapshot_id"], before_midnight.snapshot_id)
        self.assertEqual(source_two["snapshot_id"], after_midnight.snapshot_id)

        # GeoConfirmed retention can make a date selectable before mapper history.
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO geolocation_metadata(key, value) VALUES ('retention_start', '2026-08-01')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )
        old_state = self.repository.get_map_state("20260801")
        old_source = next(item for item in old_state["mappers"] if item["id"] == source_id)
        self.assertFalse(old_source["available"])
        self.assertEqual(old_source["status"], "unavailable")

    def test_change_feed_records_reversions_and_splits_distant_areas(self):
        source_id = f"test-{uuid.uuid4().hex[:12]}"
        self.repository.register_source(
            source_id=source_id, kind="mapper", display_name="Change Test",
            source_url=None, attribution="test", upstream_type="test",
        )

        def payload(kyiv_lon, donetsk_lon):
            return {
                "mapper_id": source_id,
                "layers": [{
                    "id": "points", "label": "Points", "geom_type": "point",
                    "paint": {"circle_color": "#fff"},
                    "data": {"type": "FeatureCollection", "features": [
                        {"type": "Feature", "id": "kyiv", "geometry": {"type": "Point", "coordinates": [kyiv_lon, 50.45]}, "properties": {"name": "Kyiv"}},
                        {"type": "Feature", "id": "donetsk", "geometry": {"type": "Point", "coordinates": [donetsk_lon, 48.0]}, "properties": {"name": "Donetsk"}},
                    ]},
                }],
            }

        first = self.repository.ingest_overlay(
            payload(30.52, 37.80), captured_at=datetime(2026, 8, 29, 8, tzinfo=timezone.utc)
        )
        second = self.repository.ingest_overlay(
            payload(30.62, 37.90), captured_at=datetime(2026, 8, 29, 9, tzinfo=timezone.utc)
        )
        reverted = self.repository.ingest_overlay(
            payload(30.52, 37.80), captured_at=datetime(2026, 8, 29, 10, tzinfo=timezone.utc)
        )
        self.assertEqual(reverted.snapshot_id, first.snapshot_id)
        self.assertNotEqual(reverted.observation_id, first.observation_id)
        self.assertNotEqual(second.snapshot_id, first.snapshot_id)

        with self.database.connect(dict_rows=True) as conn:
            observations = conn.execute(
                "SELECT count(*) AS count FROM map_snapshot_observations WHERE source_id = %s",
                (source_id,),
            ).fetchone()["count"]
        self.assertEqual(observations, 3)

        feed = self.repository.get_map_changes(selected="20260829", source_id=source_id)
        self.assertEqual(len(feed["items"]), 4)
        page_one = self.repository.get_map_changes(
            selected="20260829", source_id=source_id, limit=1
        )
        page_two = self.repository.get_map_changes(
            selected="20260829", source_id=source_id,
            cursor=page_one["next_cursor"], limit=1,
        )
        self.assertNotEqual(page_one["items"][0]["id"], page_two["items"][0]["id"])
        status = self.repository.get_map_change_status(
            selected="20260829", after=feed["items"][-1]["cursor"]
        )
        self.assertGreaterEqual(status["unread_count"], 3)
        state = self.repository.get_map_state("20260829")
        state_source = next(item for item in state["mappers"] if item["id"] == source_id)
        self.assertEqual(state_source["snapshot_id"], first.snapshot_id)
        latest = feed["items"][0]
        self.assertEqual(latest["counts"]["modified"], 1)
        detail = self.repository.get_map_change(latest["id"])
        self.assertEqual(detail["after"]["id"], first.snapshot_id)
        self.assertEqual(detail["before"]["id"], second.snapshot_id)
        svg, svg_etag = self.repository.get_change_svg(latest["id"])
        self.assertTrue(svg.startswith(b'<svg xmlns="http://www.w3.org/2000/svg"'))
        self.assertIn(b"#54e383", svg)
        self.assertIn(b"#ff6868", svg)
        self.assertTrue(svg_etag.startswith('"'))
        west, south, east, north = latest["bounds"]
        lon, lat = (west + east) / 2, (south + north) / 2
        tile_x = int((lon + 180) / 360 * (1 << 6))
        tile_y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * (1 << 6))
        tile, tile_etag = self.repository.get_change_tile(latest["id"], 6, tile_x, tile_y)
        self.assertTrue(tile)
        self.assertNotEqual(svg_etag, tile_etag)

    def test_change_feed_ignores_internal_metadata_but_records_style(self):
        source_id = f"test-{uuid.uuid4().hex[:12]}"
        self.repository.register_source(
            source_id=source_id, kind="mapper", display_name="Style Test",
            source_url=None, attribution="test", upstream_type="test",
        )

        def payload(updated, color="#fff"):
            return {
                "mapper_id": source_id,
                "layers": [{
                    "id": "point", "label": "Point", "geom_type": "point",
                    "paint": {"circle_color": color},
                    "data": {"type": "FeatureCollection", "features": [{
                        "type": "Feature", "id": "one",
                        "geometry": {"type": "Point", "coordinates": [30.52, 50.45]},
                        "properties": {"name": "Kyiv", "updated_at": updated},
                    }]},
                }],
            }

        self.repository.ingest_overlay(payload("one"), captured_at=datetime(2026, 8, 28, 8, tzinfo=timezone.utc))
        metadata = self.repository.ingest_overlay(payload("two"), captured_at=datetime(2026, 8, 28, 9, tzinfo=timezone.utc))
        style = self.repository.ingest_overlay(payload("two", "#000"), captured_at=datetime(2026, 8, 28, 10, tzinfo=timezone.utc))
        self.assertNotEqual(metadata.snapshot_id, style.snapshot_id)
        feed = self.repository.get_map_changes(selected="20260828", source_id=source_id)
        self.assertEqual(len(feed["items"]), 1)
        self.assertEqual(feed["items"][0]["counts"]["style"], 1)
        self.assertEqual(feed["items"][0]["counts"]["modified"], 0)
        svg, _etag = self.repository.get_change_svg(feed["items"][0]["id"])
        self.assertIn(b"STYLE CHANGE", svg)

    def test_sqlite_geoconfirmed_migration_preserves_uuid_and_detail_fields(self):
        from geolocation_service import GeoConfirmedGeolocationsSource
        from migrate_geolocations import migrate_sqlite_geolocations

        event_uuid = str(uuid.uuid4())
        icon_uuid = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = GeoConfirmedGeolocationsSource(
                root / "geo.sqlite3", root / "icons", today=lambda: date(2026, 8, 28)
            )
            record = {
                "uuid": event_uuid, "event_date": "2026-08-20",
                "timestamp": "2026-08-20T14:30:00Z", "time_precision": "minute",
                "lat": 48.2, "lon": 37.3, "description": "Exact detail",
                "faction_id": "9", "faction_name": "Ukraine", "faction_color": "#0051CA",
                "icon_id": icon_uuid, "icon_name": "Strike", "icon_path": "/icon.png",
                "origin": "X", "equipment": "Tank", "units": "Unit A",
                "plus_code": "8GX", "evidence_links": ["https://x.test/evidence"],
                "geolocation_links": ["https://maps.test/proof"],
                "gear_items": [{"name": "T-72"}], "orbat_units": [{"name": "Brigade A"}],
                "source_hash": "a" * 64, "updated_at": time.time(),
            }
            with source._db() as sqlite:
                sqlite.execute(
                    "INSERT INTO icons(id,name,upstream_path,content_type) VALUES(?,?,?,?)",
                    (icon_uuid, "Strike", "/icon.png", "image/png"),
                )
                source._upsert(sqlite, [record])
            result = migrate_sqlite_geolocations(source.db_path, self.database)
            self.assertEqual(result["uuids_verified"], 1)

        with self.database.connect(dict_rows=True) as conn:
            row = conn.execute(
                """
                SELECT *, ST_Y(location) AS lat, ST_X(location) AS lon
                FROM geolocation_events WHERE uuid = %s
                """,
                (event_uuid,),
            ).fetchone()
        self.assertEqual(row["uuid"], event_uuid)
        self.assertEqual(row["description"], "Exact detail")
        self.assertEqual(row["lat"], 48.2)
        self.assertEqual(row["lon"], 37.3)
        self.assertEqual(row["evidence_links"], ["https://x.test/evidence"])
        self.assertEqual(row["geolocation_links"], ["https://maps.test/proof"])
        self.assertNotEqual(row["evidence_links"], row["geolocation_links"])


if __name__ == "__main__":
    unittest.main()
