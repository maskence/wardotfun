import os
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


class MapChangeMaterialityStaticTests(unittest.TestCase):
    def test_material_component_thresholds_and_v5_assets_are_declared(self):
        sql = (ROOT / "backend/migrations/006_map_change_material_components.sql").read_text()
        self.assertIn("area_m2 < 500000", sql)
        self.assertIn("inradius_m < 50", sql)
        self.assertIn("< 0.01", sql)
        self.assertIn("overlap_ratio >= 0.90", sql)
        self.assertIn("map_material_change_delta", sql)

        repository = (ROOT / "backend/temporal_repository.py").read_text()
        routes = (ROOT / "backend/main.py").read_text()
        self.assertIn("wardotfun:change:v3:", repository)
        self.assertIn("/api/map-change-images/v5/", repository)
        self.assertIn("/api/map-change-tiles/v5/", repository)
        self.assertIn("/api/map-change-images/v5/", routes)
        self.assertIn("/api/map-change-tiles/v5/", routes)


@unittest.skipUnless(
    os.getenv("WARDOTFUN_TEST_DATABASE_URL"),
    "PostGIS test DSN not configured",
)
class MapChangeMaterialityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database import PostGISDatabase
        from temporal_repository import TemporalMapRepository

        cls.database = PostGISDatabase(os.environ["WARDOTFUN_TEST_DATABASE_URL"])
        cls.database.migrate()
        cls.repository = TemporalMapRepository(cls.database)

    def test_hairline_component_is_filtered_but_material_component_remains(self):
        with self.database.connect() as conn:
            # Roughly 15 km long and 22 m wide at this latitude: large enough
            # to be noisy when outlined, but below every materiality threshold.
            sliver = conn.execute(
                """
                WITH old_shape AS (
                    SELECT ST_MakeEnvelope(35.8, 47.5, 36.0, 47.6, 4326) AS g
                ), new_shape AS (
                    SELECT ST_Union(
                        g, ST_MakeEnvelope(35.8, 47.49980, 36.0, 47.5, 4326)
                    ) AS g FROM old_shape
                )
                SELECT count(*)
                FROM old_shape, new_shape,
                     LATERAL map_material_change_delta(old_shape.g, new_shape.g, 'after')
                """
            ).fetchone()[0]
            material = conn.execute(
                """
                WITH old_shape AS (
                    SELECT ST_MakeEnvelope(35.8, 47.5, 36.0, 47.6, 4326) AS g
                ), new_shape AS (
                    SELECT ST_MakeEnvelope(35.8, 47.51, 36.0, 47.6, 4326) AS g
                )
                SELECT count(*)
                FROM old_shape, new_shape,
                     LATERAL map_material_change_delta(old_shape.g, new_shape.g, 'before')
                """
            ).fetchone()[0]
        self.assertEqual(sliver, 0)
        self.assertEqual(material, 1)

    def test_rotated_polygon_id_is_reconciled_as_modified(self):
        source_id = f"test-material-{uuid.uuid4().hex[:10]}"
        self.repository.register_source(
            source_id=source_id,
            kind="mapper",
            display_name="Materiality Test",
            source_url=None,
            attribution="test",
            upstream_type="test",
        )

        def payload(feature_id, east):
            return {
                "mapper_id": source_id,
                "layers": [{
                    "id": "control",
                    "label": "Control",
                    "geom_type": "polygon",
                    "paint": {"fill_color": "#a52714", "fill_opacity": 0.3},
                    "data": {
                        "type": "FeatureCollection",
                        "features": [{
                            "type": "Feature",
                            "id": feature_id,
                            "properties": {},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[
                                    [35.8, 47.5], [east, 47.5],
                                    [east, 47.6], [35.8, 47.6], [35.8, 47.5],
                                ]],
                            },
                        }],
                    },
                }],
            }

        self.repository.ingest_overlay(
            payload("old-global-id", 35.9),
            captured_at=datetime(2026, 9, 3, 8, tzinfo=timezone.utc),
        )
        self.repository.ingest_overlay(
            payload("new-global-id", 35.91),
            captured_at=datetime(2026, 9, 3, 9, tzinfo=timezone.utc),
        )
        feed = self.repository.get_map_changes(
            selected="20260903", source_id=source_id
        )
        self.assertEqual(len(feed["items"]), 1)
        self.assertEqual(feed["items"][0]["counts"], {
            "added": 0, "removed": 0, "modified": 1, "style": 0,
        })


if __name__ == "__main__":
    unittest.main()
