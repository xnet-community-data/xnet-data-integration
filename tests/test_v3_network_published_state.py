"""Network published-cache mismatch recovery without routine upstream polling."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("network_sync", ROOT / "scripts/v3_sync_network.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class NetworkPublishedStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        self.previous = {
            "status": "live",
            "last_success_utc": "2026-10-10T11:57:53Z",
            "last_attempt_utc": "2026-10-10T11:57:53Z",
            "offload_data_as_of": "2026-10-09",
            "device_data_as_of": "2026-10-10",
        }
        self.published = {
            "collected_at_utc": "2026-10-08T09:53:09Z",
            "offload": {"data_as_of": "2026-10-07"},
            "devices": {"data_as_of": "2026-10-07"},
        }

    def test_known_uncommitted_success_requires_recovery(self):
        self.assertTrue(module.uncommitted_network_collection(self.previous, self.published))

    def test_matching_committed_data_requires_no_recovery(self):
        committed = {
            "collected_at_utc": "2026-10-10T11:57:53Z",
            "offload": {"data_as_of": "2026-10-09"},
            "devices": {"data_as_of": "2026-10-10"},
        }
        self.assertFalse(module.uncommitted_network_collection(self.previous, committed))

    def test_upstream_failure_uses_regular_backoff_not_publication_retries(self):
        failed = {**self.previous, "status": "preserved_last_good"}
        self.assertFalse(module.uncommitted_network_collection(failed, self.published))

    def test_paused_pipeline_makes_no_upstream_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"paused": True, "external_sources": {"network": self.previous}}))
            with patch.object(module, "REFRESH_STATE", state), patch.object(module, "fetch_json") as fetch:
                self.assertEqual(module.main(), 0)
                fetch.assert_not_called()

    def test_success_without_publication_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            network = root / "network.json"
            state.write_text(json.dumps({"paused": False, "external_sources": {"network": self.previous}}))
            network.write_text(json.dumps(self.published))
            with patch.object(module, "REFRESH_STATE", state), patch.object(module, "NETWORK_STATE", network), patch.object(module, "utc_now", return_value=self.now), patch.object(module, "fetch_json", side_effect=RuntimeError("mock upstream failed")) as fetch:
                self.assertEqual(module.main(), 0)
                self.assertEqual(fetch.call_count, 1)
                retried = json.loads(state.read_text())
                self.assertIn("last_unpublished_retry_utc", retried["external_sources"]["network"])
                # The prior gap retry stamp remains; no additional upstream call.
                retried["external_sources"]["network"]["status"] = "live"
                state.write_text(json.dumps(retried))
                self.assertEqual(module.main(), 0)
                self.assertEqual(fetch.call_count, 1)

if __name__ == "__main__":
    unittest.main()
