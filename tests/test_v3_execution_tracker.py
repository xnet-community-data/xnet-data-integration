import copy
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_execution_tracker import ExecutionPending, ExecutionCancelled, ExecutionTracker, SubmissionRejected

SPEC = {"key": "current", "query_id": 1, "max_run_credits": 1}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.state = {"queries": {}}
        self.snapshots = []
        self.clock = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
        self.api = Mock()
        self.guard = Mock(return_value={"credits_used": 10})
        self.tracker = self.make_tracker(self.state)

    def make_tracker(self, state):
        return ExecutionTracker(state, self.api,
                                lambda: self.snapshots.append(copy.deepcopy(state)),
                                self.guard, lambda: self.clock, self.advance, "medium")

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def pending(self, **overrides):
        value = {"key": "current", "query_id": 1, "execution_id": "existing",
                 "query_parameters": {"lookback_hours": 48},
                 "submitted_at_utc": self.clock.isoformat(), "timeout_seconds": 90,
                 "billing": {"credits_used": 10}}
        value.update(overrides)
        self.state["pending_executions"] = {"current": value}
        return value

    def completed(self, **overrides):
        value = {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                 "execution_ended_at": "2026-10-07T10:00:03Z",
                 "result_metadata": {"total_row_count": 0, "column_names": []}}
        value.update(overrides)
        return value

    def test_intent_and_id_are_durable_before_remote_poll(self):
        def api(path, payload=None):
            if path.endswith("/execute"):
                self.assertIsNone(self.snapshots[-1]["pending_executions"]["current"]["execution_id"])
                return {"execution_id": "new"}
            self.assertEqual(self.snapshots[-1]["pending_executions"]["current"]["execution_id"], "new")
            return self.completed()
        self.api.side_effect = api
        record = self.tracker.execute(SPEC, {}, True, 90)
        self.assertEqual(record["execution_id"], "new")
        self.assertIn("current", self.state["pending_executions"])
        self.tracker.acknowledge(SPEC, record)
        self.assertFalse(self.state["pending_executions"])

    def test_bounded_source_can_override_engine_and_timeout(self):
        self.api.side_effect = [{"execution_id": "small-source"}, self.completed()]
        spec = {**SPEC, "performance": "small"}
        record = self.tracker.execute(spec, {"lookback_hours": 8}, True, 18)
        self.assertEqual(record["execution_id"], "small-source")
        self.assertEqual(self.api.call_args_list[0].args,
                         ("query/1/execute", {"performance": "small",
                                              "query_parameters": {"lookback_hours": 8}}))
        self.assertEqual(self.state["pending_executions"]["current"]["timeout_seconds"], 18)

    def test_definitive_http_rejection_releases_intent_and_audits_it(self):
        self.api.side_effect = SubmissionRejected("Dune HTTP 400: tier unavailable")
        with self.assertRaises(SubmissionRejected):
            self.tracker.execute({**SPEC, "performance": "small"}, {}, True, 18)
        self.assertEqual(self.state["pending_executions"], {})
        self.assertEqual(len(self.state["rejected_submissions"]), 1)
        self.assertEqual(self.state["rejected_submissions"][0]["query_id"], 1)

    def test_queue_delay_does_not_consume_execution_budget(self):
        self.pending(
            submitted_at_utc=(self.clock - timedelta(seconds=25)).isoformat(),
            queue_timeout_seconds=30, run_timeout_seconds=15)
        self.api.side_effect = [
            {"state": "QUERY_STATE_EXECUTING", "execution_started_at":
                (self.clock - timedelta(seconds=2)).isoformat()},
            self.completed()]
        result = self.tracker.execute(SPEC, {}, True, 18)
        self.assertEqual(result["execution_id"], "existing")
        self.assertFalse(any(call.args[0].endswith("/cancel") for call in self.api.call_args_list))

    def test_compute_timeout_requests_cancel_without_replacement(self):
        self.pending(
            submitted_at_utc=(self.clock - timedelta(seconds=40)).isoformat(),
            queue_timeout_seconds=30, run_timeout_seconds=15)
        self.api.side_effect = [
            {"state": "QUERY_STATE_EXECUTING", "execution_started_at":
                (self.clock - timedelta(seconds=16)).isoformat()},
            {"success": True},
            {"state": "QUERY_STATE_EXECUTING"}]
        with self.assertRaises(ExecutionPending):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertIsNotNone(
            self.state["pending_executions"]["current"]["cancellation_requested_at_utc"])
        self.assertTrue(any(call.args[0].endswith("/cancel") for call in self.api.call_args_list))

    def test_killed_runner_resumes_the_same_execution_without_guard_or_post(self):
        self.api.side_effect = [{"execution_id": "new"}, SystemExit("runner killed")]
        with self.assertRaises(SystemExit):
            self.tracker.execute(SPEC, {"lookback_hours": 48}, True, 90)
        restored = copy.deepcopy(self.snapshots[-1])
        self.api.reset_mock()
        self.api.side_effect = None
        self.api.return_value = self.completed()
        self.guard.reset_mock()
        record = self.make_tracker(restored).execute(SPEC, {"lookback_hours": 2}, True, 90)
        self.assertEqual(record["execution_id"], "new")
        self.assertEqual(record["query_parameters"], {"lookback_hours": 48})
        self.api.assert_called_once_with("execution/new/status")
        self.guard.assert_not_called()

    def test_timeout_cancels_but_does_not_trust_cancel_response(self):
        self.pending(submitted_at_utc=(self.clock - timedelta(seconds=91)).isoformat())
        def api(path, payload=None):
            return {"success": True} if path.endswith("/cancel") else {"state": "QUERY_STATE_EXECUTING"}
        self.api.side_effect = api
        with self.assertRaisesRegex(ExecutionPending, "awaiting confirmed cancellation"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertIn("current", self.snapshots[-1]["pending_executions"])
        restored = self.make_tracker(copy.deepcopy(self.snapshots[-1]))
        with self.assertRaises(ExecutionPending):
            restored.execute(SPEC, {}, True, 90)
        self.assertFalse(any(call.args[0].endswith("/execute") for call in self.api.call_args_list))

    def test_cancellation_racing_with_completion_uses_completed_result(self):
        self.pending(submitted_at_utc=(self.clock - timedelta(seconds=91)).isoformat())
        self.api.side_effect = [{"state": "QUERY_STATE_EXECUTING"}, {"success": False}, self.completed()]
        record = self.tracker.execute(SPEC, {}, True, 90)
        self.assertEqual(record["execution_id"], "existing")
        self.assertEqual(self.state["execution_history"][0]["state"], "QUERY_STATE_COMPLETED")

    def test_failed_cancel_request_keeps_id_and_blocks_resubmission(self):
        self.pending(submitted_at_utc=(self.clock - timedelta(seconds=91)).isoformat())
        self.api.side_effect = [{"state": "QUERY_STATE_EXECUTING"}, RuntimeError("cancel unavailable")]
        with self.assertRaisesRegex(ExecutionPending, "replacement blocked"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertEqual(self.snapshots[-1]["pending_executions"]["current"]["execution_id"], "existing")
        self.guard.assert_not_called()

    def test_confirmed_cancel_is_recorded_without_immediate_replacement(self):
        self.pending(cancellation_requested_at_utc=self.clock.isoformat())
        self.api.return_value = {"state": "QUERY_STATE_CANCELLED", "execution_cost_credits": 0.4}
        with self.assertRaisesRegex(ExecutionCancelled, "cancellation confirmed"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertFalse(self.state["pending_executions"])
        self.assertEqual(self.state["execution_history"][0]["execution_cost_credits"], 0.4)
        self.assertEqual(self.api.call_count, 1)

    def test_lost_submission_response_is_not_retried(self):
        self.api.side_effect = TimeoutError("lost POST response")
        with self.assertRaisesRegex(ExecutionPending, "submission is unresolved"):
            self.tracker.execute(SPEC, {}, True, 90)
        restored = self.make_tracker(copy.deepcopy(self.snapshots[-1]))
        self.api.reset_mock()
        with self.assertRaisesRegex(ExecutionPending, "manual reconciliation"):
            restored.execute(SPEC, {}, True, 90)
        self.api.assert_not_called()

    def test_unknown_execution_id_blocks_other_new_submissions(self):
        self.pending(execution_id=None, submission_error="Dune HTTP 500")
        with self.assertRaisesRegex(ExecutionPending, "all new submissions blocked"):
            self.tracker.execute(
                {"key": "burns", "query_id": 22, "max_run_credits": 1},
                {}, True, 90,
            )
        self.api.assert_not_called()
        self.guard.assert_not_called()
        self.assertNotIn("burns", self.state["pending_executions"])

    def test_failed_checkpoint_prevents_submission(self):
        self.tracker.checkpoint = Mock(side_effect=RuntimeError("publish failed"))
        with self.assertRaisesRegex(RuntimeError, "publish failed"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.api.assert_not_called()

    def test_failed_execution_cost_is_retained(self):
        self.pending()
        self.api.return_value = {"state": "QUERY_STATE_FAILED", "execution_cost_credits": 0.6}
        with self.assertRaisesRegex(RuntimeError, "QUERY_STATE_FAILED"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertEqual(self.state["execution_history"][0]["execution_cost_credits"], 0.6)
        self.assertFalse(self.state["pending_executions"])

    def test_missing_cost_is_unknown_then_reconciled_without_export(self):
        self.pending()
        self.api.return_value = {"state": "QUERY_STATE_FAILED"}
        with self.assertRaises(RuntimeError):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertIsNone(self.state["execution_history"][0]["execution_cost_credits"])
        self.api.return_value = {"state": "QUERY_STATE_FAILED", "execution_cost_credits": 0.7}
        self.tracker.reconcile()
        self.assertEqual(self.state["execution_history"][0]["execution_cost_credits"], 0.7)
        self.assertTrue(all(call.args[0].endswith("/status") for call in self.api.call_args_list))

    def test_completed_without_cost_remains_tracked(self):
        self.pending()
        self.api.return_value = self.completed(execution_cost_credits=None)
        with self.assertRaisesRegex(ExecutionPending, "no cost metadata"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertIn("current", self.state["pending_executions"])

    def test_cost_cap_logs_cost_and_blocks_result_consumption(self):
        self.pending()
        self.api.return_value = self.completed(execution_cost_credits=2)
        with self.assertRaisesRegex(RuntimeError, "exceeded"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertEqual(self.state["execution_history"][0]["execution_cost_credits"], 2)

    def test_reconciliation_can_cancel_existing_work_when_new_spend_is_blocked(self):
        self.pending(submitted_at_utc=(self.clock - timedelta(seconds=91)).isoformat())
        self.guard.side_effect = RuntimeError("budget exhausted")
        self.api.side_effect = [{"state": "QUERY_STATE_EXECUTING"}, {"success": True},
                                {"state": "QUERY_STATE_CANCELLED", "execution_cost_credits": 0.2}]
        self.assertFalse(self.tracker.reconcile())
        self.guard.assert_not_called()
        self.assertEqual(self.state["execution_history"][0]["execution_cost_credits"], 0.2)

    def test_dune_canceled_spelling_is_terminal(self):
        self.pending(cancellation_requested_at_utc=self.clock.isoformat())
        self.api.return_value = {
            "state": "QUERY_STATE_CANCELED",
            "execution_cost_credits": 0.3,
        }
        with self.assertRaisesRegex(ExecutionCancelled, "cancellation confirmed"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertFalse(self.state["pending_executions"])
        self.assertEqual(
            self.state["execution_history"][0]["state"],
            "QUERY_STATE_CANCELED",
        )

    def test_completed_partial_is_terminal_but_never_consumed_as_success(self):
        self.pending()
        self.api.return_value = {
            "state": "QUERY_STATE_COMPLETED_PARTIAL",
            "execution_cost_credits": 0.4,
        }
        with self.assertRaisesRegex(RuntimeError, "QUERY_STATE_COMPLETED_PARTIAL"):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertFalse(self.state["pending_executions"])
        self.assertEqual(
            self.state["execution_history"][0]["state"],
            "QUERY_STATE_COMPLETED_PARTIAL",
        )

    def test_acknowledge_persists_result_and_export_telemetry(self):
        self.pending()
        self.api.return_value = self.completed(
            result_metadata={
                "total_row_count": 12,
                "total_result_set_bytes": 3456,
                "column_names": ["a"],
            }
        )
        record = self.tracker.execute(SPEC, {}, True, 90)
        record.update({
            "export_points": 12,
            "export_wire_bytes": 4000,
            "estimated_export_credits": 0.04,
        })
        self.tracker.acknowledge(SPEC, record)
        entry = self.state["execution_history"][0]
        self.assertEqual(entry["result_row_count"], 12)
        self.assertEqual(entry["result_bytes"], 3456)
        self.assertEqual(entry["export_points"], 12)
        self.assertEqual(entry["export_wire_bytes"], 4000)
        self.assertEqual(entry["estimated_export_credits"], 0.04)

    def test_terminal_record_is_deduplicated_across_restarts(self):
        self.pending()
        self.api.return_value = self.completed()
        self.tracker.reconcile()
        self.tracker.reconcile()
        self.assertEqual(len(self.state["execution_history"]), 1)


class PublicationTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()

    def test_execution_checkpoint_changes_only_dune_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin, seed, source = root / "origin.git", root / "seed", root / "source"
            self.git(root, "init", "--bare", str(origin))
            seed.mkdir()
            self.git(seed, "init", "-b", "live-state")
            self.git(seed, "config", "user.name", "test")
            self.git(seed, "config", "user.email", "test@example.com")
            (seed / "data/current").mkdir(parents=True)
            (seed / "data/current/v3_refresh_state.json").write_text('{"schema_version": 1, "queries": {}}\n')
            (seed / "data/current/xnet_chain_health.json").write_text('{"status": "HEALTHY"}\n')
            (seed / "data/xnet_defillama_revenue.json").write_text('"revenue unchanged"\n')
            self.git(seed, "add", ".")
            self.git(seed, "commit", "-m", "seed")
            self.git(seed, "remote", "add", "origin", str(origin))
            self.git(seed, "push", "origin", "live-state")
            source.mkdir()
            self.git(source, "init")
            self.git(source, "remote", "add", "origin", str(origin))
            (source / "scripts").mkdir()
            shutil.copy(ROOT / "scripts/v3_publish_live_state.sh", source / "scripts")
            shutil.copy(ROOT / "scripts/v3_live_state_store.py", source / "scripts")
            shutil.copy(ROOT / "scripts/v3_hydrate_live_state.py", source / "scripts")
            subprocess.run([sys.executable, "scripts/v3_hydrate_live_state.py"], cwd=source,
                           check=True, capture_output=True)
            (source / "data/current/v3_refresh_state.json").write_text(json.dumps({
                "schema_version": 1, "queries": {}, "pending_executions": {"current": {
                    "key": "current", "query_id": 1, "query_parameters": {},
                    "submitted_at_utc": "2026-10-09T08:25:19Z", "execution_id": "saved"}}}) + '\n')
            (source / "data/current/xnet_chain_health.json").write_text('{"status": "PAUSED", "last_refresh_completed_utc": "2026-10-09T08:25:19Z"}\n')
            subprocess.run(["bash", "scripts/v3_publish_live_state.sh", "--refresh-state-only"],
                           cwd=source, check=True, capture_output=True, text=True)
            changed = self.git(origin, "diff", "--name-only", "live-state~1", "live-state")
            self.assertEqual(set(changed.splitlines()), {"data/current/v3_refresh_state.json", "data/current/xnet_chain_health.json", "state/v3_live_generation.json"})
            self.assertEqual(json.loads(self.git(origin, "show", "live-state:data/current/xnet_chain_health.json"))["status"], "PAUSED")
            self.assertEqual(self.git(origin, "show", "live-state:data/xnet_defillama_revenue.json"), '"revenue unchanged"')


class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("integration_refresh", ROOT / "scripts/v3_refresh_dashboard.py")
        self.refresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.refresh)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_path = self.root / "state.json"
        # publish_data_generation() snapshots ROOT/data for pre-publish rollback,
        # so the orchestration fixture must provide a data directory.
        (self.root / "data" / "current").mkdir(parents=True, exist_ok=True)
        self.source = {"key": "xnet_transfers", "query_id": 1, "output": "transfers.json",
                       "max_run_credits": 1.5, "cadence_minutes": 30, "lookback_hours": 2}
        self.fixed = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
        self.state = {"schema_version": 1, "queries": {"xnet_transfers": {"query_id": 1,
                      "completed_at_utc": self.fixed.isoformat(),
                      "query_parameters": {"lookback_hours": 2}}},
                      "chain_completed_at_utc": self.fixed.isoformat(),
                      "chain_repaired_at_utc": self.fixed.isoformat(),
                      "pending_executions": {"xnet_transfers": {
                          "key": "xnet_transfers", "query_id": 1, "execution_id": "recover",
                          "query_parameters": {"lookback_hours": 48},
                          "submitted_at_utc": (self.fixed - timedelta(seconds=10)).isoformat(),
                          "timeout_seconds": 240, "billing": {}}}}
        self.state_path.write_text(json.dumps(self.state))
        # main() now loads through the durable store (hydration receipt). These
        # tests exercise orchestration, so supply the journal under test instead
        # of weakening production load_authoritative_state().
        for name, value in {"ROOT": self.root, "STATE": self.state_path,
                            "HEALTH": self.root / "health.json",
                            "load_authoritative_state":
                                lambda: json.loads(self.state_path.read_text()),
                            "CONFIG": {
                                **self.refresh.CONFIG, "sources": [self.source], "presentation": []}}.items():
            patcher = patch.object(self.refresh, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_confirmed_cancel_enters_source_cooldown_without_replacement(self):
        status = {"state": "QUERY_STATE_CANCELLED", "execution_cost_credits": 0.1}
        self.state["pending_executions"]["xnet_transfers"]["cancellation_requested_at_utc"] = self.fixed.isoformat()
        self.state_path.write_text(json.dumps(self.state))
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api", return_value=status), \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish"):
            self.assertEqual(self.refresh.main(), 0)
        saved = json.loads(self.state_path.read_text())
        self.assertNotIn("xnet_transfers", saved["pending_executions"])
        self.assertFalse(saved["source_errors"]["xnet_transfers"]["unresolved_execution"])
        self.assertTrue(saved["source_errors"]["xnet_transfers"]["next_retry_at_utc"])

    def test_no_id_ambiguous_submission_halts_future_refresh_post_calls(self):
        self.state["pending_executions"]["xnet_transfers"]["execution_id"] = None
        self.state["pending_executions"]["xnet_transfers"]["submission_error"] = "Dune HTTP 500"
        self.state_path.write_text(json.dumps(self.state))
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api") as api, \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish"):
            self.assertEqual(self.refresh.main(), 0)
        api.assert_not_called()
        saved = json.loads(self.state_path.read_text())
        self.assertIsNone(saved["pending_executions"]["xnet_transfers"]["execution_id"])
        self.assertEqual(saved["source_errors"]["xnet_transfers"]["unresolved_execution"], True)

    def test_presentation_backoff_does_not_block_existing_pending_id(self):
        until = (self.fixed + timedelta(minutes=30)).isoformat()
        state = {"presentation_errors": {"current": {"next_retry_at_utc": until}}}
        with patch.object(self.refresh, "now", return_value=self.fixed):
            self.assertTrue(self.refresh.presentation_in_backoff(state, "current"))
            state["pending_executions"] = {"current": {"execution_id": "existing"}}
            self.assertFalse(self.refresh.presentation_in_backoff(state, "current"))

    def test_recovered_source_publishes_consumed_marker_and_no_pending_in_one_commit(self):
        status = {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                  "execution_ended_at": self.fixed.isoformat(), "result_metadata": {}}
        events = []
        publications = []
        def publish(state_only=False):
            events.append("publish:state" if state_only else "publish:full")
            publications.append((state_only, json.loads(self.state_path.read_text())))
        def reduce(*args, **kwargs):
            events.append("reduce")
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api", return_value=status) as api, \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "export_source", return_value=3000) as export, \
             patch.object(self.refresh, "validate_hot_transfers") as validate, \
             patch.object(self.refresh, "reduce_atomically", reduce), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish", publish):
            self.assertEqual(self.refresh.main(), 0)
        api.assert_called_once_with("execution/recover/status")
        self.assertEqual(export.call_args.args[0]["query_parameters"], {"lookback_hours": 48})
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(events.count("reduce"), 1)
        # New invariant: publish_data_generation() stages the consumption marker and
        # only then performs the single acceptance commit. The FULL publish must
        # already contain the consumed marker and no pending id for the source;
        # no acknowledge may follow it.
        full_state = next(state for only, state in publications if not only)
        self.assertIn("recover", full_state["consumed_executions"])
        self.assertEqual(full_state["consumed_executions"]["recover"]["key"], "xnet_transfers")
        self.assertNotIn("xnet_transfers", full_state.get("pending_executions", {}))
        self.assertEqual(full_state["queries"]["xnet_transfers"]["execution_id"], "recover")
        # The acceptance commit is ordered after the hot reduction.
        self.assertLess(events.index("reduce"), events.index("publish:full"))
        self.assertFalse(json.loads(self.state_path.read_text())["pending_executions"])

    def test_failed_source_consumption_retains_execution_and_rolls_back_data(self):
        status = {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                  "execution_ended_at": self.fixed.isoformat(), "result_metadata": {}}
        partial = self.root / "data" / "current" / "reduce-partial.json"
        def reduce(*args, **kwargs):
            # Simulate a reducer that mutates canonical data and then fails.
            partial.write_text('{"partial": true}\n')
            raise RuntimeError("reducer failed")
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api", return_value=status), \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "export_source", return_value=3000), \
             patch.object(self.refresh, "validate_hot_transfers"), \
             patch.object(self.refresh, "reduce_atomically", reduce), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish"):
            self.assertEqual(self.refresh.main(), 1)
        saved = json.loads(self.state_path.read_text())
        self.assertTrue(saved["paused"])
        self.assertEqual(saved["pending_executions"]["xnet_transfers"]["execution_id"], "recover")
        # A pre-publish failure must roll the working data back to last-good.
        self.assertFalse(partial.exists())

    def test_expensive_transfer_is_quarantined_while_dashboard_stays_live(self):
        status = {"state": "QUERY_STATE_COMPLETED",
                  "execution_cost_credits": 7.14,
                  "execution_ended_at": self.fixed.isoformat(),
                  "result_metadata": {"total_row_count": 0, "column_names": []}}
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api", return_value=status), \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish"), \
             patch.object(self.refresh, "reduce_atomically") as reduce:
            self.assertEqual(self.refresh.main(), 0)
        saved = json.loads(self.state_path.read_text())
        self.assertFalse(saved["paused"])
        self.assertTrue(saved["source_errors"]["xnet_transfers"]["next_retry_at_utc"])
        self.assertEqual(json.loads((self.root / "health.json").read_text())["status"], "DEGRADED")
        reduce.assert_not_called()
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api") as api, \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish"):
            self.assertEqual(self.refresh.main(), 0)
        api.assert_not_called()  # Cooling off without duplicate Dune spending.

    def test_pending_source_does_not_set_a_global_pause(self):
        with patch("sys.argv", ["refresh"]), patch.object(self.refresh, "now", return_value=self.fixed), \
             patch.object(self.refresh, "api", side_effect=RuntimeError("status temporarily unavailable")), \
             patch.object(self.refresh, "usage_guard", return_value={}), \
             patch.object(self.refresh, "run"), patch.object(self.refresh, "publish"):
            self.assertEqual(self.refresh.main(), 0)
        saved = json.loads(self.state_path.read_text())
        self.assertFalse(saved["paused"])
        self.assertTrue(saved["source_errors"]["xnet_transfers"]["unresolved_execution"])
        self.assertIn("replacement blocked", saved["source_errors"]["xnet_transfers"]["error"])
        self.assertEqual(saved["pending_executions"]["xnet_transfers"]["execution_id"], "recover")


class SqlPartitionTests(unittest.TestCase):
    def test_transfer_partition_tracks_real_lookback(self):
        sql = (ROOT / "dune/v3/sql/40_xnet_transfer_events_hot.sql").read_text()
        self.assertIn("CAST(CURRENT_TIMESTAMP - INTERVAL '{{lookback_hours}}' HOUR AS DATE)", sql)
        self.assertIn("block_time >= CURRENT_TIMESTAMP - INTERVAL '{{lookback_hours}}' HOUR", sql)
        self.assertNotIn("CURRENT_DATE - INTERVAL '1' DAY", sql)


if __name__ == "__main__":
    unittest.main()
