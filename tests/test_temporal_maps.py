import gzip
import math
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from ingestion_worker import RawArchive  # noqa: E402
from mapper_service import GoogleMyMapsMapperSource  # noqa: E402
from temporal_repository import (  # noqa: E402
    MVT_EXTENT,
    TemporalDataError,
    TemporalMapRepository,
    WEB_MERCATOR_WIDTH,
    content_hash,
    kyiv_calendar_date,
    logical_feature_key,
    normalized_overlay,
    parse_compact_date,
)


def feature(coordinates, properties=None, feature_id=None):
    value = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coordinates},
        "properties": properties or {},
    }
    if feature_id is not None:
        value["id"] = feature_id
    return value


def overlay(features, paint=None):
    return {
        "mapper_id": "test-map",
        "layers": [{
            "id": "frontline",
            "label": "Frontline",
            "geom_type": "point",
            "paint": paint or {"circle_color": "#fff"},
            "data": {"type": "FeatureCollection", "features": features},
        }],
    }


class TemporalNormalizationTests(unittest.TestCase):
    def test_compact_date_validation(self):
        self.assertEqual(parse_compact_date("20260828"), date(2026, 8, 28))
        with self.assertRaises(TemporalDataError):
            parse_compact_date("2026-08-28")
        with self.assertRaises(TemporalDataError):
            parse_compact_date("20260230")

    def test_kyiv_calendar_boundary(self):
        self.assertEqual(
            kyiv_calendar_date(datetime(2026, 8, 28, 20, 59, tzinfo=timezone.utc)),
            date(2026, 8, 28),
        )
        self.assertEqual(
            kyiv_calendar_date(datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)),
            date(2026, 8, 29),
        )

    def test_snapshot_hash_ignores_feature_order_but_not_style_or_geometry(self):
        first = feature([31.000000001, 48], {"name": "A"})
        second = feature([32, 49], {"name": "B"})
        ordered = content_hash(normalized_overlay(overlay([first, second])))
        reversed_order = content_hash(normalized_overlay(overlay([second, first])))
        self.assertEqual(ordered, reversed_order)
        self.assertNotEqual(
            ordered,
            content_hash(normalized_overlay(overlay([first, second], {"circle_color": "#000"}))),
        )
        moved = feature([33, 49], {"name": "B"})
        self.assertNotEqual(
            ordered,
            content_hash(normalized_overlay(overlay([first, moved]))),
        )

    def test_identity_prefers_arcgis_and_kml_ids(self):
        arcgis = feature([31, 48], {"OBJECTID": 42, "name": "Area"})
        key, confidence = logical_feature_key("isw", "control", arcgis)
        self.assertIn("objectid:42", key)
        self.assertGreaterEqual(confidence, 0.95)

        kml = feature([31, 48], {"placemark_id": "front-7", "name": "Area"})
        key, confidence = logical_feature_key("amk", "front", kml)
        self.assertIn("placemark_id:front-7", key)
        self.assertEqual(confidence, 1.0)

        fallback = feature([31, 48], {"name": "Area"})
        key, confidence = logical_feature_key("amk", "front", fallback)
        self.assertIn("fallback:area:point", key)
        self.assertLess(confidence, 0.9)

    def test_kml_placemark_id_survives_parsing(self):
        source = GoogleMyMapsMapperSource(
            mapper_id="test-map",
            display_name="Test",
            source_url="https://example.test",
            attribution="Test",
            kml_mid="unused",
        )
        payload = source._parse_kml(b"""<?xml version='1.0'?>
        <kml xmlns='http://www.opengis.net/kml/2.2'><Document><Folder>
          <name>Front</name><Placemark id='pm-123'><name>Line</name>
          <LineString><coordinates>31,48 32,49</coordinates></LineString>
          </Placemark></Folder></Document></kml>""")
        props = payload["layers"][0]["data"]["features"][0]["properties"]
        self.assertEqual(props["placemark_id"], "pm-123")

    def test_exact_duplicate_features_receive_distinct_replayable_keys(self):
        class Repository(TemporalMapRepository):
            @staticmethod
            def _previous_candidates(_conn, _source_id, _bases):
                return {}

        duplicate = feature([31, 48], {"OBJECTID": 7, "name": "Area"})
        layers = Repository(FakeDatabase())._prepare_layers(
            None, "isw", overlay([duplicate, duplicate])
        )
        prepared = layers[0]["features"]
        self.assertEqual(len({item["logical_key"] for item in prepared}), 2)
        self.assertTrue(all(item["confidence"] >= 0.85 for item in prepared))


class RawArchiveTests(unittest.TestCase):
    def test_content_addressing_archives_distinct_bytes_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = RawArchive(temporary)
            first = archive.store(
                "isw", "https://example.test/data.json", b'{"ok":true}',
                {"Content-Type": "application/json", "ETag": '"upstream"', "Last-Modified": "today"},
                captured_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
            second = archive.store(
                "another-source", "https://example.test/data.json", b'{"ok":true}',
                {"Content-Type": "application/json"},
                captured_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
            )
            self.assertEqual(first["path"], second["path"])
            self.assertEqual(first["etag"], '"upstream"')
            self.assertEqual(first["last_modified"], "today")
            files = list(Path(temporary).rglob("*.gz"))
            self.assertEqual(len(files), 1)
            with gzip.open(files[0], "rb") as source:
                self.assertEqual(source.read(), b'{"ok":true}')


class FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if len(self.calls) == 1:
            return FakeResult((1,))
        return FakeResult((memoryview(b"tile"),))


class FakeDatabase:
    def __init__(self):
        self.connection = FakeConnection()

    def connect(self, **_kwargs):
        return self.connection


class TileQueryTests(unittest.TestCase):
    def test_tile_query_uses_half_pixel_simplification_and_immutable_identity(self):
        database = FakeDatabase()
        repository = TemporalMapRepository(database)
        snapshot = "11111111-1111-4111-8111-111111111111"
        tile, etag = repository.get_tile("test-map", snapshot, 11, 1100, 700)
        self.assertEqual(tile, b"tile")
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))
        sql, params = database.connection.calls[1]
        self.assertIn("ST_AsMVTGeom", sql)
        self.assertIn("ST_SimplifyPreserveTopology", sql)
        expected = (WEB_MERCATOR_WIDTH / (1 << 11)) / (MVT_EXTENT * 2)
        self.assertTrue(math.isclose(params[4], expected, rel_tol=1e-12))

    def test_tile_coordinates_are_validated_before_database_access(self):
        database = FakeDatabase()
        repository = TemporalMapRepository(database)
        with self.assertRaises(TemporalDataError):
            repository.get_tile("test-map", "not-a-uuid", 6, 1, 1)
        with self.assertRaises(TemporalDataError):
            repository.get_tile(
                "test-map", "11111111-1111-4111-8111-111111111111", 6, 64, 1
            )
        self.assertEqual(database.connection.calls, [])


class HttpTransferTests(unittest.TestCase):
    def test_conditional_json_etag_and_gzip_middleware(self):
        from starlette.requests import Request
        import main

        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
        response = main._conditional_json(request, {"payload": "x" * 4000})
        self.assertEqual(response.status_code, 200)
        self.assertIn("etag", response.headers)
        conditional = Request({
            "type": "http", "method": "GET", "path": "/",
            "headers": [(b"if-none-match", response.headers["etag"].encode())],
        })
        not_modified = main._conditional_json(conditional, {"payload": "x" * 4000})
        self.assertEqual(not_modified.status_code, 304)
        self.assertEqual(not_modified.headers["etag"], response.headers["etag"])
        self.assertTrue(any(middleware.cls.__name__ == "GZipMiddleware" for middleware in main.app.user_middleware))

    def test_vector_tile_route_sets_immutable_cache_headers(self):
        from starlette.requests import Request
        import main

        class Repository:
            @staticmethod
            def get_tile(*_args):
                return b"mvt", '"tile-etag"'

        previous = main.temporal_repository
        main.temporal_repository = Repository()
        try:
            request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
            response = main.map_tile(
                request, "isw", "11111111-1111-4111-8111-111111111111", 6, 37, 21
            )
            self.assertEqual(response.media_type, "application/vnd.mapbox-vector-tile")
            self.assertIn("max-age=31536000", response.headers["cache-control"])
            self.assertIn("immutable", response.headers["cache-control"])
            conditional = Request({
                "type": "http", "method": "GET", "path": "/",
                "headers": [(b"if-none-match", b'"tile-etag"')],
            })
            self.assertEqual(
                main.map_tile(
                    conditional, "isw", "11111111-1111-4111-8111-111111111111",
                    6, 37, 21,
                ).status_code,
                304,
            )
        finally:
            main.temporal_repository = previous


if __name__ == "__main__":
    unittest.main()
