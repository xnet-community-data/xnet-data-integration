import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("refresh", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

class RefreshTests(unittest.TestCase):
    def test_configured_engine_and_no_results_download_for_chart(self):
        calls = []
        def api(path, payload=None):
            calls.append((path, payload))
            if path.endswith("/execute"):
                return {"execution_id": "test"}
            return {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1, "result_metadata": {}}
        with patch.object(refresh, "api", api), patch.object(refresh, "usage_guard", return_value={}):
            result = refresh.execute({"query_id": 1, "max_run_credits": 0.35})
        self.assertEqual(result["execution_cost_credits"], 0.1)
        self.assertEqual(calls[0][1]["performance"], refresh.CONFIG["performance"])
        self.assertFalse(any("results" in path for path, _ in calls))

    def test_unexpected_cost_stops(self):
        def api(path, payload=None):
            return {"execution_id": "test"} if path.endswith("/execute") else {
                "state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 2}
        with patch.object(refresh, "api", api), patch.object(refresh, "usage_guard", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "exceeded"):
                refresh.execute({"query_id": 1, "max_run_credits": 0.35})

    def test_export_allowance_checked_before_download(self):
        record = {"execution_id": "test", "result_metadata": {
            "total_row_count": 500, "column_names": ["a", "b"], "datapoint_count": 1000}}
        with patch.object(refresh, "api") as api:
            with self.assertRaisesRegex(RuntimeError, "bounded allowance"):
                refresh.export_source(record, {"output": "unused"}, 100)
            api.assert_not_called()

    def test_quiet_source_has_no_export_charge(self):
        record = {"execution_id": "test", "result_metadata": {
            "total_row_count": 0, "column_names": ["a"], "datapoint_count": 0}}
        with patch.object(refresh, "api") as api, patch.object(refresh, "save"):
            self.assertEqual(refresh.export_source(record, {"output": "unused"}, 3000), 3000)
            api.assert_not_called()
            self.assertEqual(record["estimated_export_credits"], 0)

    def test_pause_prevents_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text('{"paused": true}')
            with patch.object(refresh, "STATE", state), patch("sys.argv", ["refresh"]), patch.object(refresh, "api") as api:
                with self.assertRaisesRegex(SystemExit, "paused"):
                    refresh.main()
                api.assert_not_called()

if __name__ == "__main__":
    unittest.main()
