import sys, tempfile, unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from geolocation_service import (GeoConfirmedGeolocationsSource, compact_index, extract_urls,
                                  is_near_ukraine, parse_csv_export, timestamp_precision)

UUID1 = "11111111-1111-4111-8111-111111111111"
UUID2 = "22222222-2222-4222-8222-222222222222"
ICON = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def csv_payload(rows):
    header = "Date;Name;Faction;Origin;Latitude;Longitude;PlusCode;Description;Source;Geolocation;Equipment;EquipmentItems;Units;OrbatUnits;Id\n"
    return header + "\n".join(rows)


def index_payload(ids=(UUID1,), stamp="2026-08-06T14:30:00"):
    return [{"factionId": 9, "name": "Ukraine", "color": "#0051CA", "icons": [{
        "iconId": ICON, "icon": "/icon.png", "name": "Strike", "placemarks": [
            {"id": uid, "date": stamp, "la": 48.1 + i, "lo": 37.2 + i} for i, uid in enumerate(ids)
        ]}]}]


class FakeClient:
    def __init__(self, csv_text, ids=(UUID1,)):
        self.csv_text, self.ids, self.fail = csv_text, ids, False
    def get_csv(self, start, end):
        if self.fail: raise OSError("offline")
        return parse_csv_export(self.csv_text)
    def get_json(self, path):
        if self.fail: raise OSError("offline")
        if path.endswith("/icons"): return index_payload(())
        if "/detail/" in path:
            uid = path.rsplit("/", 1)[-1]
            return {"id": uid, "date": "2026-08-06T16:45:00", "latitude": 48.2,
                    "longitude": 37.3, "description": "Corrected", "faction": "Ukraine",
                    "icon": "/icon.png", "originalSource": "https://x.com/a/status/123",
                    "geolocation": "https://maps.example/proof", "gearItems": [{"name": "Tank"}]}
        return index_payload(self.ids)
    def get_bytes(self, path, params=None): return b"png", "image/png"


class ParsingTests(unittest.TestCase):
    def test_ukraine_bounds_include_margin_but_exclude_moscow(self):
        self.assertTrue(is_near_ukraine(48.2, 37.3))
        self.assertTrue(is_near_ukraine(43.8, 40.8))
        self.assertFalse(is_near_ukraine(55.7558, 37.6173))

    def test_multiline_semicolon_csv_and_url_sections(self):
        payload = csv_payload([f'2026-08-06;06 AUG;Ukraine;X;48;37;;"line one\nline; two";"https://x.com/a/status/1, https://x.com/a/status/1";https://maps.example/proof;;;;;{UUID1}'])
        row = parse_csv_export(payload)[0]
        self.assertEqual(row["Description"], "line one\nline; two")
        self.assertEqual(extract_urls(row["Source"]), ["https://x.com/a/status/1"])
        self.assertEqual(extract_urls(row["Geolocation"]), ["https://maps.example/proof"])

    def test_compact_join_and_timestamp_precision(self):
        events, icons = compact_index(index_payload())
        self.assertEqual(events[UUID1]["icon_id"], ICON)
        self.assertEqual(icons[ICON]["faction_color"], "#0051CA")
        self.assertEqual(timestamp_precision("2026-08-06T00:00:00"), "day")
        self.assertEqual(timestamp_precision("2026-08-06T14:30:00"), "minute")


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.today = lambda: date(2026, 8, 23)
        self.row1 = f'2026-08-06;06 AUG;Ukraine;X;48;37;ABC;First;https://x.com/a/status/1;https://maps.example/proof;Tank;T-72;Unit A;Brigade A;{UUID1}'
        self.client = FakeClient(csv_payload([self.row1]))
        self.source = GeoConfirmedGeolocationsSource(self.base / "geo.db", self.base / "icons", self.client, self.today)

    def tearDown(self): self.temp.cleanup()

    def test_fixed_cutoff_initial_import_and_idempotent_upsert(self):
        self.assertEqual(self.source.retention_start, date(2026, 5, 25))
        self.assertTrue(self.source.initial_import())
        self.assertTrue(self.source.initial_import())
        result = self.source.get_all("20260806")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["uuid"], UUID1)
        self.assertEqual(result["daily_counts"]["20260807"], 0)
        self.assertEqual(result["sources"][0]["id"], "geoconfirmed")
        self.assertEqual(len(result["dates"]), 91)

    def test_filter_detail_icon_and_validation(self):
        self.source.initial_import()
        self.assertEqual(len(self.source.get_all("20260806", q="tank")["events"]), 1)
        self.assertEqual(len(self.source.get_all("20260806", faction="9")["events"]), 1)
        detail = self.source.get_detail(UUID1)
        self.assertNotEqual(detail["evidence_links"], detail["geolocation_links"])
        self.assertEqual(detail["attribution"]["name"], "GeoConfirmed")
        icon = self.source.get_icon(ICON)
        self.assertTrue(icon and icon[0].read_bytes() == b"png")
        with self.assertRaises(ValueError): self.source.get_all("2026-08-06")
        with self.assertRaises(ValueError): self.source.get_all("20260524")

    def test_feed_counts_and_details_exclude_events_outside_ukraine_bounds(self):
        outside = f'2026-08-06;06 AUG;Ukraine;X;55.7558;37.6173;;Moscow;;;;;;;{UUID2}'
        self.client.csv_text = csv_payload([self.row1, outside])
        self.source.initial_import()
        result = self.source.get_all("20260806")
        self.assertEqual([event["uuid"] for event in result["events"]], [UUID1])
        self.assertEqual(result["daily_counts"]["20260806"], 1)
        self.assertEqual(result["sources"][0]["retained_event_count"], 1)
        self.assertIsNone(self.source.get_detail(UUID2))

    def test_geoconfirmed_guid_without_rfc_variant_is_accepted(self):
        icon_id = "5e50efc2-e725-446b-60ff-08db18fdbbf7"
        path = self.base / "icons" / f"{icon_id}.png"
        path.write_bytes(b"png")
        with self.source._db() as db:
            db.execute(
                "INSERT INTO icons(id,local_name,content_type) VALUES(?,?,?)",
                (icon_id, path.name, "image/png"),
            )
        self.assertEqual(self.source.get_icon(icon_id)[0], path)

    def test_incremental_correction_failure_and_reconciliation_removal(self):
        self.source.initial_import()
        changed = self.row1.replace("First", "Changed")
        self.client.csv_text = csv_payload([changed])
        self.assertTrue(self.source.incremental_sync())
        self.assertEqual(self.source.get_detail(UUID1)["description"], "Corrected")
        self.client.fail = True
        self.assertFalse(self.source.incremental_sync())
        self.assertEqual(len(self.source.get_all("20260806")["events"]), 1)
        self.assertEqual(self.source.get_all("20260806")["sources"][0]["status"], "stale")
        self.client.fail = False; self.client.csv_text = csv_payload([]); self.client.ids = ()
        self.assertTrue(self.source.reconcile())
        self.assertEqual(self.source.get_all("20260806")["events"], [])


if __name__ == "__main__": unittest.main()
