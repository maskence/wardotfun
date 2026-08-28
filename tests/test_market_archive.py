import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "utils"))
sys.path.insert(0, str(ROOT / "backend"))

import city_map  # noqa: E402
import manage_city_market_map as updater  # noqa: E402
from polymarket_service import PolymarketDataService  # noqa: E402
import market_map_update_service as scheduler_module  # noqa: E402


def remote_market(market_id, *, closed=False, prices='["0.4", "0.6"]'):
    return {
        "id": str(market_id),
        "conditionId": f"condition-{market_id}",
        "question": f"Will Russia enter Testville by December 31, 2026?",
        "slug": f"testville-{market_id}",
        "closed": closed,
        "closedTime": "2026-08-18 12:00:00+00" if closed else None,
        "endDate": "2026-12-31T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": prices,
    }


def mapping_with(markets):
    return {
        "schema_version": 3,
        "cities": {
            "city-1": {
                "city": {"name_en": "Testville", "global_id": "city-1"},
                "geometry": {"rings": []},
                "markets": {"enter": markets, "capture": [], "capture_all": []},
            },
        },
    }


class MarketStatusTests(unittest.TestCase):
    def test_remote_status_distinguishes_active_resolved_and_closed(self):
        self.assertEqual(updater.remote_market_status(remote_market(1)), {"status": "active"})
        resolved = updater.remote_market_status(remote_market(2, closed=True, prices='["1", "0"]'))
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["outcome"], "Yes")
        ambiguous = updater.remote_market_status(remote_market(3, closed=True))
        self.assertEqual(ambiguous["status"], "closed")
        self.assertNotIn("outcome", ambiguous)

    def test_v2_status_migration_is_compatible(self):
        active = {"active": True}
        inactive = {"active": False}
        updater.migrate_market_status(active)
        updater.migrate_market_status(inactive)
        self.assertEqual(active, {"status": "active"})
        self.assertEqual(inactive, {"status": "closed"})

    def test_non_interactive_helpers_never_prompt(self):
        with patch("builtins.input") as user_input:
            self.assertIsNone(updater.resolve_capture_target("Missing", [], interactive=False))
            self.assertIsNone(updater.choose_feature([{}, {}], "Ambiguous", interactive=False))
        user_input.assert_not_called()

    def test_google_maps_destination_coordinates_are_extracted(self):
        destination = (
            "https://www.google.com/maps/place/Test/"
            "@48.5109616,37.7227663,3383m/data=!3d48.5119856!4d37.7296144"
        )
        self.assertEqual(updater.extract_coordinates(destination), (48.5119856, 37.7296144))

    def test_capture_objective_label_distinguishes_buildings(self):
        event = {
            "title": "Will Russia capture the Royal Café Alex in Kostyantynivka by...?",
        }
        self.assertEqual(updater.capture_target_label(event, {}), "Royal Café Alex")
        generic = {"title": "Will Russia capture Kostyantynivka by...?"}
        market = {"description": "Russia captures the Kostyantynivka railroad station located on Main St."}
        self.assertEqual(
            updater.capture_target_label(generic, market, "Kostyantynivka"),
            "Railroad station",
        )

    def test_known_event_backfill_is_idempotent(self):
        existing = {
            "id": "1", "eventSlug": "test-event", "title": "Will Russia enter Testville by December 31?",
            "slug": "testville-1", "conditionId": "condition-1", "status": "active",
        }
        mapping = mapping_with([existing])
        event = {
            "slug": "test-event",
            "title": "Will Russia enter Testville by...?",
            "markets": [
                remote_market(1),
                remote_market(2, closed=True, prices='["1", "0"]'),
                remote_market(3, closed=True, prices='["0", "1"]'),
            ],
        }
        keys = updater.collect_existing_keys(mapping)
        with patch.object(updater, "fetch_gamma_events", return_value={"test-event": event}):
            self.assertEqual(updater.backfill_known_events(mapping, keys), (2, 0))
            self.assertEqual(updater.backfill_known_events(mapping, keys), (0, 0))
        stored = mapping["cities"]["city-1"]["markets"]["enter"]
        self.assertEqual(len(stored), 3)
        self.assertEqual({market.get("outcome") for market in stored[1:]}, {"Yes", "No"})


class ArchiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.original = city_map._data

    def tearDown(self):
        city_map._data = self.original

    def test_live_references_and_recent_feed_use_separate_statuses(self):
        city_map._data = mapping_with([
            {"id": "1", "status": "active", "title": "Active"},
            {"id": "2", "status": "resolved", "outcome": "Yes", "resolvedAt": "2026-08-18T12:00:00Z", "title": "Yes"},
            {"id": "3", "status": "resolved", "outcome": "No", "resolvedAt": "2026-08-19T12:00:00Z", "title": "No"},
            {"id": "4", "status": "closed", "title": "Ambiguous"},
        ])
        service = PolymarketDataService()
        self.assertEqual(set(service._market_references()), {"1", "2", "3"})
        recent = service._resolved_from_archive()
        self.assertEqual([market["id"] for market in recent], ["3", "2"])
        self.assertEqual(recent[0]["city_id"], "city-1")

    def test_resolved_markets_fetch_history_but_not_orderbooks(self):
        city_map._data = mapping_with([
            {"id": "1", "status": "active", "title": "Active"},
            {"id": "2", "status": "resolved", "outcome": "Yes",
             "resolvedAt": "2026-08-18T12:00:00Z", "title": "Resolved Yes"},
            {"id": "3", "status": "resolved", "outcome": "No",
             "resolvedAt": "2026-08-19T12:00:00Z", "title": "Resolved No"},
        ])
        active = {**remote_market(1), "clobTokenIds": '["token-1", "no-1"]'}
        resolved_yes = {**remote_market(2, closed=True, prices='["1", "0"]'),
                        "clobTokenIds": '["token-2", "no-2"]'}
        resolved_no = {**remote_market(3, closed=True, prices='["0", "1"]'),
                       "clobTokenIds": '["token-3", "no-3"]'}
        service = PolymarketDataService()
        histories = {
            "token-1": [{"t": 1, "p": 0.4}],
            "token-2": [{"t": 2, "p": 1.0}],
            "token-3": [{"t": 3, "p": 0.0}],
        }
        remotes = {"1": active, "2": resolved_yes, "3": resolved_no}
        with patch.object(service, "_fetch_gamma_markets", return_value=remotes),                 patch.object(service, "_get_histories", return_value=histories) as get_histories,                 patch.object(service, "_get_orderbooks", return_value={}) as get_orderbooks:
            payload = service._refresh(100)
        self.assertEqual(get_histories.call_args.args[0], ["token-1", "token-2", "token-3"])
        self.assertEqual(get_orderbooks.call_args.args[0], ["token-1"])
        self.assertEqual(payload["markets"]["2"]["history"], histories["token-2"])
        self.assertEqual(payload["markets"]["3"]["history"], histories["token-3"])
        self.assertIsNone(payload["markets"]["2"]["orderbook"])
        self.assertIsNone(payload["markets"]["3"]["orderbook"])


class SchedulerTests(unittest.TestCase):
    def test_successful_run_is_non_interactive_and_reloads_archive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            reloaded = []
            completed = subprocess.CompletedProcess([], 0, stdout="updated", stderr="")
            with patch.object(scheduler_module, "STATE_PATH", state_path), \
                    patch.object(scheduler_module.subprocess, "run", return_value=completed) as run:
                service = scheduler_module.MarketMapUpdateService(on_updated=lambda: reloaded.append(True))
                self.assertTrue(service._run_once())
            command = run.call_args.args[0]
            self.assertIn("--non-interactive", command)
            self.assertEqual(reloaded, [True])
            self.assertIsNotNone(service.status().get("last_success"))
            self.assertTrue(state_path.exists())


if __name__ == "__main__":
    unittest.main()
