import importlib.util
import io
import tempfile
import unittest
import sys
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("refresh", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # checkpoint() now also writes internal operational health, and main()
        # owns the network/market collection, so give the fixture its own repo
        # root with a real data/ directory instead of touching the worktree.
        self.repo = Path(self.directory.name) / "repo"
        (self.repo / "data/current").mkdir(parents=True)
        root = patch.object(refresh, "ROOT", self.repo)
        root.start()
        self.addCleanup(root.stop)
        state = patch.object(refresh, "STATE", Path(self.directory.name) / "state.json")
        state.start()
        self.addCleanup(state.stop)
        publish = patch.object(refresh, "publish")
        publish.start()
        self.addCleanup(publish.stop)

    def test_disabled_collector_is_not_executed_or_reduced(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # publish_data_generation() snapshots ROOT/data for pre-publish
            # rollback, and main() now owns the network/market collection, so the
            # orchestration fixture needs a real data directory.
            (root / "data/current").mkdir(parents=True)
            state = root / "state.json"
            state.write_text(json.dumps({"schema_version": 1,
                                         "queries": {"bbb_dex": {"query_id": 2}}}))
            config = {**refresh.CONFIG, "sources": [
                {"key": "xnet_transfers", "query_id": 1, "output": "transfers"},
                {"key": "bbb_dex", "query_id": 2, "output": "bbb", "enabled": False}],
                "presentation": [], "reconciliation": None}
            record = {"execution_id": "exec-1", "query_id": 1,
                      "query_parameters": {"lookback_hours": 2},
                      "execution_cost_credits": 0.1}
            with patch.object(refresh, "ROOT", root), patch.object(refresh, "STATE", state), patch.object(refresh, "HEALTH", root / "health.json"), patch.object(refresh, "load_authoritative_state", return_value=json.loads(state.read_text())), patch.object(refresh, "CONFIG", config), patch("sys.argv", ["refresh"]), patch.object(refresh, "usage_guard", return_value={}), patch.object(refresh, "execute", return_value=record) as execute, patch.object(refresh, "export_source", return_value=100), patch.object(refresh, "validate_hot_transfers"), patch.object(refresh, "reduce_atomically") as reduce, patch.object(refresh, "run"), patch.object(refresh, "publish"):
                self.assertEqual(refresh.main(), 0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(execute.call_args.args[0]["key"], "xnet_transfers")
            reduce.assert_called_once_with(root / "transfers", None)
            # Configuration changes must not erase prior accepted provenance, so
            # the disabled source's persisted entry is retained unchanged while it
            # is neither submitted nor reduced.
            self.assertEqual(json.loads(state.read_text())["queries"]["bbb_dex"], {"query_id": 2})

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

    def test_bbb_only_reducer_invocation_does_not_fetch_or_rebuild_transfers(self):
        with patch.object(refresh, "run") as run:
            refresh.reduce_atomically(None, "bbb.json")
        run.assert_called_once_with("v3_reduce_chain.py", "--bbb-result", "bbb.json", "--allow-stale-holders")

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
        with patch.object(refresh, "load_authoritative_state", return_value={}), patch.object(refresh, "api", api), patch.object(refresh, "usage_guard", return_value={}):
            result = refresh.execute({"query_id": 1, "max_run_credits": 0.35})
        self.assertEqual(result["execution_cost_credits"], 0.1)
        self.assertEqual(calls[0][1]["performance"], refresh.CONFIG["performance"])
        self.assertFalse(any("results" in path for path, _ in calls))

    def test_unexpected_cost_stops(self):
        def api(path, payload=None):
            return {"execution_id": "test"} if path.endswith("/execute") else {
                "state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 2}
        with patch.object(refresh, "load_authoritative_state", return_value={}), patch.object(refresh, "api", api), patch.object(refresh, "usage_guard", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "exceeded"):
                refresh.execute({"query_id": 1, "max_run_credits": 0.35})

    def test_execute_http_failure_is_ambiguous_until_proven_nonacceptance(self):
        # An accepted submission can be lost behind any HTTP status, including
        # 4xx/408/429. Status alone is not proof of nonacceptance, so every
        # execute HTTP failure stays ambiguous until a proven provider contract
        # says otherwise; none of these may be treated as a definitive rejection.
        for code in (400, 402, 408, 409, 429, 500):
            error = urllib.error.HTTPError(
                url="https://api.dune.com/api/v1/query/1/execute",
                code=code,
                msg="error",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"rejected"}'),
            )
            with self.subTest(code=code), patch.dict(
                "os.environ", {"DUNE_API_KEY": "test-key"}
            ), patch.object(refresh,"dune_open", side_effect=error):
                with self.assertRaises(RuntimeError) as ctx:
                    refresh.api("query/1/execute", {"performance": "medium"})
                self.assertNotIsInstance(ctx.exception, refresh.SubmissionRejected)

    def test_execute_5xx_remains_ambiguous(self):
        error = urllib.error.HTTPError(
            url="https://api.dune.com/api/v1/query/1/execute",
            code=500,
            msg="error",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"server failure"}'),
        )
        with patch.dict("os.environ", {"DUNE_API_KEY": "test-key"}), patch.object(
            refresh,"dune_open", side_effect=error
        ):
            with self.assertRaises(RuntimeError):
                refresh.api("query/1/execute", {"performance": "medium"})

    def test_dune_transport_refuses_redirects_and_uses_opener_open(self):
        opener=Mock()
        with patch("urllib.request.build_opener",return_value=opener) as build:
            refresh.dune_open("request",timeout=45)
        opener.open.assert_called_once_with("request",timeout=45)
        handler=build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None,None,302,"redirect",{},"https://untrusted.example/"))

    def test_non_object_metadata_is_a_typed_transient_failure(self):
        response=Mock();response.read.return_value=b'[]'
        transport=Mock();transport.__enter__=Mock(return_value=response);transport.__exit__=Mock(return_value=False)
        with patch.dict("os.environ",{"DUNE_API_KEY":"test-key"}),patch.object(refresh,"dune_open",return_value=transport):
            with self.assertRaisesRegex(RuntimeError,"non-object"):
                refresh.api("query/1")
            self.assertEqual(refresh.api("execution/known/results",include_wire_size=True),(None,2))

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

    def test_export_arithmetic_is_isolated_from_ambient_state(self):
        """Fetch-path arithmetic must be provable without ambient authoritative hydration:
        the fixture owns the spend-free cache lookup (a read-only miss), the wire-size risk,
        admission, and the explicit budget totals, so this asserts accounting and ordering
        only - lookup -> guard -> risk -> prune -> stamp -> download -> cache."""
        source = next(s for s in refresh.CONFIG["sources"] if s["key"] == "xnet_transfers")
        spec = {"output": "state/v3_transfer_hot_latest.json", "performance": source["performance"]}
        params = {"lookback_hours": source["lookback_hours"]}
        rows = [{"a_b": 1, "day": "2026-10-18"}, {"a_b": 2, "day": "2026-10-18"}]
        record = {"execution_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "query_id": source["query_id"],
                  "query_parameters": params, "execution_cost_credits": 0.4,
                  "result_metadata": {"total_row_count": 2, "column_names": ["a_b", "day"],
                                      "datapoint_count": 4, "total_result_set_bytes": 1500000}}
        state = {"queries": {}, "execution_history": [], "pending_executions": {},
                 "result_exports": {"schema_version": 1, "requests": {}},
                 "budget_decision": {"status": "admitted", "report": {"totals": {
                     "limit_credits": 100.0, "usage_plus_reserve": 40.0, "dependencies_total": 10.0}}}}
        cache = Mock()
        cache.lookup.return_value = None  # read-only miss: nothing cached, nothing staged
        def cache_factory(callback):
            def prepare(candidate, record, binding, **kwargs):
                candidate["result_exports"]["requests"]["fixture-key"] = {
                    "execution_id": record["execution_id"], "status": "requested",
                    "requested_at_utc": kwargs["timestamp"], "risk_credits": kwargs["risk_credits"]}
                callback(candidate)
            cache.prepare_export.side_effect = prepare
            return cache
        order = Mock()
        with patch.object(refresh, "result_cache", side_effect=cache_factory), \
             patch.object(refresh, "checkpoint"), \
             patch.object(refresh, "require_operation_approval", return_value={"operation_id": "fixture-read"}), \
             patch.object(refresh, "export_risk", return_value=(5.0, ["3 comparable samples"])) as risk, \
             patch.object(refresh, "usage_guard") as guard, \
             patch.object(refresh, "api", return_value=({"state": "QUERY_STATE_COMPLETED",
                 "execution_id": record["execution_id"], "result": {"rows": rows}}, 400)) as api, \
             patch.object(refresh, "save") as save:
            for name, mock in (("lookup", cache.lookup), ("usage_guard", guard), ("export_risk", risk),
                               ("prune_consumed", cache.prune_consumed), ("prepare_export", cache.prepare_export),
                               ("api", api), ("cache_result", cache.cache_result)):
                order.attach_mock(mock, name)
            remaining = refresh.export_source(record, spec, 3000, max_bytes=2000000, state=state)
        self.assertEqual(remaining, 2996)  # 3000 point allowance minus 4 (2 rows x 2 columns)
        self.assertEqual([entry[0] for entry in order.mock_calls],
                          ["lookup", "prune_consumed", "usage_guard", "export_risk", "prepare_export", "api", "cache_result"])
        guard.assert_called_once_with(state)
        risk.assert_called_once_with(state, record, spec, 1500000)
        binding = cache.lookup.call_args.args[2]  # the immutable execution binding, not a SQL-version guess
        self.assertEqual(binding["query_id"], 8894037)
        self.assertEqual(binding["parameters"], {"lookback_hours": source["lookback_hours"]})
        self.assertEqual(binding["columns"], ["a_b", "day"])
        self.assertEqual(len(binding["sql_sha256"]), 64)
        cache.prepare_export.assert_called_once()
        self.assertEqual(cache.prepare_export.call_args.args, (state, record, binding))
        self.assertEqual((cache.prepare_export.call_args.kwargs["risk_credits"],
                          cache.prepare_export.call_args.kwargs["evidence"]),
                         (5.0, ["3 comparable samples"]))  # caller-supplied estimate, never inferred
        api.assert_called_once_with(f'execution/{record["execution_id"]}/results?limit=10000&allow_partial_results=false', include_wire_size=True)
        cache.cache_result.assert_called_once()
        self.assertEqual(cache.cache_result.call_args.args,
                         (state, record, binding,
                          {"execution_id": record["execution_id"], "total_row_count": 2, "result_rows": rows}))
        self.assertTrue(cache.cache_result.call_args.kwargs["timestamp"].endswith("Z"))
        self.assertEqual(record["export_points"], 4)
        self.assertEqual(record["export_wire_bytes"], 400)
        # Provider metadata is not a wire charge. Only this completed read is metered.
        self.assertAlmostEqual(record["estimated_export_credits"],
                               400 / 1000000 * refresh.CONFIG["export_credits_per_megabyte"])
        self.assertEqual(save.call_args.args[0], refresh.ROOT / spec["output"])
        self.assertEqual(save.call_args.args[1]["result"]["rows"], rows)

    def test_cached_export_needs_no_admission_headroom_or_download(self):
        """A validated cached reuse is spend-free: it stays possible with the
        budget lane closed and must not consult admission, risk, or the provider."""
        source = next(s for s in refresh.CONFIG["sources"] if s["key"] == "xnet_transfers")
        spec = {"output": "state/v3_transfer_hot_latest.json", "performance": source["performance"]}
        record = {"execution_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "query_id": source["query_id"],
                  "query_parameters": {"lookback_hours": source["lookback_hours"]},
                  "execution_cost_credits": 0.4,
                  "result_metadata": {"total_row_count": 1, "column_names": ["a_b", "day"],
                                      "datapoint_count": 4, "total_result_set_bytes": 1500000}}
        cached_rows = [{"a_b": 7, "day": "2026-10-17"}]
        cache = Mock()
        cache.lookup.return_value = {"execution_id": record["execution_id"], "result_rows": cached_rows,
                                     "columns": ["a_b", "day"]}
        state = {}  # no budget lane, no result_exports lane: the cached path needs neither
        with patch.object(refresh, "result_cache", return_value=cache), \
             patch.object(refresh, "export_risk") as risk, patch.object(refresh, "usage_guard") as guard, \
             patch.object(refresh, "api") as api, patch.object(refresh, "save") as save:
            self.assertEqual(refresh.export_source(record, spec, 3000, max_bytes=2000000, state=state), 2996)
        api.assert_not_called()
        guard.assert_not_called()
        risk.assert_not_called()
        cache.prune_consumed.assert_not_called()
        cache.prepare_export.assert_not_called()
        cache.cache_result.assert_not_called()
        self.assertEqual(state, {})  # no intent stamped, no telemetry written
        self.assertEqual(record["export_points"], 4)
        saved = save.call_args.args[1]
        self.assertEqual(saved["execution_id"], record["execution_id"])
        self.assertEqual(saved["result"]["rows"], cached_rows)  # the exact served execution's rows

    def test_pause_prevents_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text('{"schema_version": 1, "queries": {}, "paused": true}')
            # Keep the repo's real health file untouched: pause reporting writes
            # HEALTH, so point it at an isolated path for this orchestration test.
            health = Path(directory) / "health.json"
            with patch.object(refresh, "STATE", state), patch.object(refresh, "HEALTH", health), patch.object(refresh, "load_authoritative_state", return_value={"schema_version": 1, "queries": {}, "paused": True}), patch("sys.argv", ["refresh"]), patch.object(refresh, "api") as api:
                with self.assertRaisesRegex(SystemExit, "paused"):
                    refresh.main()
                api.assert_not_called()


    def test_scheduled_pause_publishes_independent_data_without_dune_or_state_change(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # refresh_independent_data() writes ROOT/data/... operational health
            # and main() snapshots data/, so the fixture owns a real data dir.
            (root / "data/current").mkdir(parents=True)
            state = root / "data/current/v3_refresh_state.json"
            original = {"schema_version": 1, "paused": True,
                        "last_error": "cost anomaly", "queries": {}}
            state.write_text(json.dumps(original))
            summary = root / "summary.md"
            health = root / "health.json"
            health.write_text('{"status": "HEALTHY", "last_refresh_completed_utc": "2026-10-09T08:25:19Z"}')
            with patch.object(refresh, "ROOT", root), patch.object(refresh, "STATE", state), patch.object(refresh, "HEALTH", health), patch.object(refresh, "load_authoritative_state", return_value=json.loads(json.dumps(original))), patch("sys.argv", ["refresh"]), patch.dict("os.environ", {"EVENT": "schedule", "GITHUB_STEP_SUMMARY": str(summary)}), patch.object(refresh, "api") as api, patch.object(refresh, "execute") as execute, patch.object(refresh, "export_source") as export, patch.object(refresh, "run") as run, patch.object(refresh, "publish") as publish:
                self.assertEqual(refresh.main(), 0)
            # No Dune work at all: no status calls, no submissions, no exports.
            api.assert_not_called()
            execute.assert_not_called()
            export.assert_not_called()
            # Independent, non-Dune upstream collection ran (its own cadence).
            collected = [call.args[0] for call in run.call_args_list]
            self.assertIn("v3_sync_network.py", collected)
            self.assertIn("v3_build_live_bundle.py", collected)
            publish.assert_called_once_with()
            # The core journal is unchanged, including the retained pause, and no
            # consumption was staged by the independent publication. The only
            # addition is the internal operational-health provenance map.
            committed = json.loads(state.read_text())
            self.assertEqual(committed["schema_version"], original["schema_version"])
            self.assertTrue(committed["paused"])
            self.assertEqual(committed["last_error"], "cost anomaly")
            self.assertEqual(committed["queries"], {})
            self.assertEqual(committed.get("pending_executions", {}), {})
            self.assertNotIn("consumed_executions", committed)
            self.assertEqual(set(committed) - set(original), {"operational_incidents", "source_errors", "financial_journal", "pending_executions"})
            self.assertIn("Chain pause retained: True", summary.read_text())
            self.assertEqual(json.loads(health.read_text())["status"], "PAUSED")
            self.assertEqual(json.loads(health.read_text())["last_refresh_completed_utc"], "2026-10-09T08:25:19Z")

    def test_overbudget_manifest_is_quarantined_without_dune_calls(self):
        state = {
            "source_errors": {
                "historical_reconciliation": {
                    "error": "Query 8893945 cost 39.136617648 exceeded 15.0; source quarantined."
                }
            }
        }
        with patch.object(refresh, "execute") as execute:
            self.assertFalse(refresh.reconcile_historical_manifest(state, {"credits_used": 100}))
        execute.assert_not_called()
        self.assertIn("manual review", state["reconciliation"]["manifest_deferred_reason"])

    def test_cost_overrun_sets_persistent_quarantine(self):
        state = {"queries": {}, "source_errors": {}}
        with patch.object(refresh, "execute",
                          side_effect=refresh.ExecutionCostExceeded("cost exceeded cap")) as execute, \
             patch.object(refresh, "save"):
            self.assertFalse(refresh.reconcile_historical_manifest(state, {"credits_used": 100}))
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(state["source_errors"]["historical_reconciliation"]["cost_overrun_quarantined"])

        with patch.object(refresh, "execute") as no_retry:
            self.assertFalse(refresh.reconcile_historical_manifest(state, {"credits_used": 100}))
        no_retry.assert_not_called()

    def test_observed_completed_cost_variation_is_within_presentation_guard(self):
        observed_cost = 0.57805981
        tokenomics = next(s for s in refresh.CONFIG["presentation"] if s["key"] == "tokenomics")
        def api(path, payload=None):
            return {"execution_id": "observed"} if path.endswith("/execute") else {
                "state": "QUERY_STATE_COMPLETED", "execution_cost_credits": observed_cost}
        with patch.object(refresh, "load_authoritative_state", return_value={}), patch.object(refresh, "api", api), patch.object(refresh, "usage_guard", return_value={}):
            record = refresh.execute(tokenomics)
        self.assertEqual(record["execution_cost_credits"], observed_cost)
        self.assertGreater(refresh.CONFIG["monthly_spend_guard_credits"], 0)
        self.assertLessEqual(refresh.CONFIG["monthly_spend_guard_credits"], 3800)
        self.assertEqual(refresh.CONFIG["minimum_credit_reserve"], 100)
        self.assertEqual(refresh.CONFIG["autonomous_operation"]["minimum_unspent_quota_credits"], 1000)

if __name__ == "__main__":
    unittest.main()
