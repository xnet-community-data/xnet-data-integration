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
    def test_disabled_collector_is_not_executed_or_reduced(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            state.write_text(json.dumps({"queries": {"bbb_dex": {"query_id": 2}}}))
            config = {**refresh.CONFIG, "sources": [
                {"key": "xnet_transfers", "query_id": 1, "output": "transfers"},
                {"key": "bbb_dex", "query_id": 2, "output": "bbb", "enabled": False}], "presentation": []}
            with patch.object(refresh, "ROOT", root), patch.object(refresh, "STATE", state), patch.object(refresh, "HEALTH", root / "health.json"), patch.object(refresh, "CONFIG", config), patch("sys.argv", ["refresh"]), patch.object(refresh, "usage_guard", return_value={}), patch.object(refresh, "execute", return_value={}) as execute, patch.object(refresh, "export_source", return_value=100), patch.object(refresh, "reduce_atomically") as reduce, patch.object(refresh, "run"), patch.object(refresh, "publish"):
                self.assertEqual(refresh.main(), 0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0]["key"], "xnet_transfers")
            reduce.assert_called_once_with(root / "transfers", None)
            self.assertNotIn("bbb_dex", json.loads(state.read_text())["queries"])

    def test_failed_reduction_restores_canonical_and_derived_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data").mkdir()
            original = root / "data/canonical.csv"
            original.write_text("last good data")
            def broken(*args):
                original.write_text("partial result")
                (root / "data/partial.json").write_text("bad")
                raise RuntimeError("reduction failed")
            with patch.object(refresh, "ROOT", root), patch.object(refresh, "run", broken):
                with self.assertRaisesRegex(RuntimeError, "reduction failed"):
                    refresh.reduce_atomically("transfers", "bbb")
            self.assertEqual(original.read_text(), "last good data")
            self.assertFalse((root / "data/partial.json").exists())

    def test_cadence_skips_early_runs_and_catches_up(self):
        from datetime import timedelta
        fixed = refresh.now()
        with patch.object(refresh, "now", return_value=fixed):
            self.assertFalse(refresh.due((fixed - timedelta(minutes=15)).isoformat(), 30))
            self.assertTrue(refresh.due((fixed - timedelta(minutes=31)).isoformat(), 30))
            self.assertTrue(refresh.due(None, 30))

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

    def test_export_estimate_uses_megabytes_and_actual_response_bytes(self):
        record = {"execution_id": "test", "result_metadata": {
            "total_row_count": 1, "column_names": ["a"], "datapoint_count": 1, "total_result_set_bytes": 100}}
        with patch.object(refresh, "api", return_value=({"result": {"rows": [{"a": 1}]}}, 200)), patch.object(refresh, "usage_guard", return_value={}), patch.object(refresh, "save"):
            refresh.export_source(record, {"output": "unused"}, 3000)
        self.assertEqual(record["export_wire_bytes"], 200)
        self.assertAlmostEqual(record["estimated_export_credits"], 200 / 1000000 * refresh.CONFIG["export_credits_per_megabyte"])

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
