"""Acceptance tests for the single-commit generation publication contract.

These target the helper ``publish_data_generation(state, paths, source_records)``
directly (the block Astra is moving out of ``main``) plus the HTTP ambiguity and
manifest-decision boundaries. No network: every provider call is patched.

Covered invariants:
  * a pre-publish crash restores both in-memory acceptance state and working data;
  * a post-publish StateError preserves the exact candidate and performs no
    compensating write or second publication;
  * Dune execute HTTP 408/500 stay ambiguous end to end (no definitive rejection,
    durable intent retained);
  * a manifest mismatch performs no destructive reduce and stages the consumption
    marker together with the manual-review decision in one commit.
"""
import contextlib
import copy
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_execution_tracker import ExecutionPending, ExecutionTracker

SPEC = {"key": "xnet_transfers", "query_id": 1, "output": "transfers.json",
        "max_run_credits": 1.5, "cadence_minutes": 30, "lookback_hours": 2}
RECORD = {"execution_id": "recover", "query_id": 1,
          "query_parameters": {"lookback_hours": 48}}


def load_refresh():
    spec = importlib.util.spec_from_file_location(
        "acceptance_refresh", ROOT / "scripts/v3_refresh_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AcceptanceTestCase(unittest.TestCase):
    def setUp(self):
        self.refresh = load_refresh()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "data" / "current").mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.health = self.root / "health.json"
        self.fixed = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
        self.config = {**self.refresh.CONFIG, "sources": [SPEC], "presentation": [],
                       "chain_cadence_minutes": 30}

    def stack(self, **overrides):
        """Patch review base globals; caller supplies the mocks it inspects."""
        base = {"ROOT": self.root, "STATE": self.state_path, "HEALTH": self.health,
                "CONFIG": self.config, "run": Mock(), "validate_hot_transfers": Mock()}
        base.update(overrides)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in base.items():
            stack.enter_context(patch.object(self.refresh, name, value))
        return stack

    @staticmethod
    def pending_state():
        return {"queries": {}, "pending_executions": {"xnet_transfers": {
            "key": "xnet_transfers", "query_id": 1, "execution_id": "recover",
            "query_parameters": {"lookback_hours": 48},
            "submitted_at_utc": "2026-10-07T09:59:00Z", "timeout_seconds": 240,
            "billing": {}}}}


class PublishDataGenerationTests(AcceptanceTestCase):
    def test_prepublish_crash_restores_in_memory_state_and_data(self):
        state = self.pending_state()
        before = copy.deepcopy(state)
        partial = self.root / "data" / "current" / "partial.json"
        publish = Mock()

        def reduce(*args, **kwargs):
            partial.write_text('{"partial": true}\n')  # reducer mutated canonical data

        def run(script, *args):
            if script == "v3_build_chain_snapshot.py":
                raise RuntimeError("snapshot builder crashed")

        self.stack(run=run, reduce_atomically=reduce, publish=publish)
        with self.assertRaises(RuntimeError):
            self.refresh.publish_data_generation(
                state, {"xnet_transfers": self.root / "transfers.json"},
                {"xnet_transfers": (SPEC, RECORD)})
        self.assertEqual(state, before, "in-memory acceptance state was not reset")
        self.assertFalse(partial.exists(), "working data was not rolled back")
        publish.assert_not_called()

    def test_postpublish_stateerror_preserves_candidate_without_compensating_write(self):
        state = self.pending_state()
        candidate = self.root / "data" / "current" / "candidate.json"
        publish = Mock(side_effect=self.refresh.StateError(
            "Publication not acknowledged; durable intent retained"))

        def reduce(*args, **kwargs):
            candidate.write_text('{"candidate": true}\n')

        self.stack(reduce_atomically=reduce, publish=publish)
        with self.assertRaises(self.refresh.StateError):
            self.refresh.publish_data_generation(
                state, {"xnet_transfers": self.root / "transfers.json"},
                {"xnet_transfers": (SPEC, RECORD)})
        # Durability barrier: the exact candidate survives, no compensating write,
        # and the staged acceptance state is preserved for store recovery.
        self.assertTrue(candidate.exists(), "post-publish candidate was rolled back")
        self.assertEqual(publish.call_count, 1, "a second publication was attempted")
        self.assertIn("recover", state["consumed_executions"])
        self.assertNotIn("xnet_transfers", state.get("pending_executions", {}))
        self.assertIn("chain_completed_at_utc", state)
        saved = json.loads(self.state_path.read_text())
        self.assertIn("recover", saved["consumed_executions"])
        self.assertNotIn("xnet_transfers", saved.get("pending_executions", {}))


class HttpAmbiguityTests(AcceptanceTestCase):
    def test_http_408_and_500_are_ambiguous_never_definitive_rejections(self):
        for code in (408, 500):
            error = urllib.error.HTTPError(
                "https://api.dune.com/api/v1/query/1/execute", code, "error", None, None)
            with self.subTest(code=code), \
                 patch.dict(os.environ, {"DUNE_API_KEY": "test-key"}), \
                 patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(RuntimeError) as context:
                    self.refresh.api("query/1/execute", {"performance": "medium"})
                self.assertNotIsInstance(context.exception, self.refresh.SubmissionRejected)

        # Tracker boundary: the same failure must retain a durable ambiguous
        # intent, never record a definitive rejection.
        state = {"queries": {}}
        snapshots = []

        def failing_api(*args, **kwargs):
            raise RuntimeError("Dune HTTP 500 at query/1/execute")

        tracker = ExecutionTracker(
            state, failing_api, lambda: snapshots.append(copy.deepcopy(state)),
            lambda: {}, lambda: self.fixed, lambda seconds: None, "medium")
        with self.assertRaises(ExecutionPending):
            tracker.execute({"key": "current", "query_id": 1, "max_run_credits": 1},
                            {}, True, 90)
        self.assertNotIn("rejected_submissions", state)
        intent = state["pending_executions"]["current"]
        self.assertIsNone(intent["execution_id"])
        self.assertIn("submission_error", intent)
        self.assertTrue(snapshots, "the ambiguous intent was not durably checkpointed")


class ManifestDecisionTests(AcceptanceTestCase):
    def test_manifest_mismatch_does_not_reduce_and_stages_marker_with_decision(self):
        policy = {"daily_cadence_minutes": 60, "weekly_cadence_minutes": 10080,
                  "weekly_lookback_days": 30, "daily_lookback_days": 1,
                  "manifest_query_id": 9, "manifest_max_run_credits": 1.0,
                  "manifest_max_export_points": 10, "manifest_max_export_bytes": 1000}
        config = {**self.config, "reconciliation": policy}
        state = {
            "queries": {},
            "pending_executions": {"reconciliation_manifest": {
                "key": "reconciliation_manifest", "query_id": 9,
                "execution_id": "manifest-exec", "query_parameters": {"lookback_days": 1},
                "submitted_at_utc": "2026-10-07T09:59:00Z", "timeout_seconds": 240,
                "billing": {}}},
            "reconciliation": {"last_weekly_manifest_utc": self.fixed.isoformat()},
        }
        output = self.root / "state" / "v3_transfer_manifest_latest.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"result": {"rows": [{
            "day": "2026-10-01", "event_count": 5, "transfer_count": 5, "burn_count": 0,
            "mint_count": 0, "tx_count": 5, "min_block_slot": 1, "max_block_slot": 9,
            "amount_raw_total": "1", "block_slot_total": "2"}]}}))
        local = {"2026-10-01": {"day": "2026-10-01", "event_count": 999,
                                "transfer_count": 999, "burn_count": 0, "mint_count": 0,
                                "tx_count": 999, "min_block_slot": 1, "max_block_slot": 9,
                                "amount_raw_total": "1", "block_slot_total": "2"}}
        record = {"execution_id": "manifest-exec", "query_id": 9,
                  "execution_cost_credits": 0.2, "query_parameters": {"lookback_days": 1},
                  "state": "QUERY_STATE_COMPLETED"}
        captured = []
        reduce_source = Mock()
        publish = Mock()

        def checkpoint(value):
            captured.append(copy.deepcopy(value))

        self.stack(CONFIG=config, now=lambda: self.fixed, execute=Mock(return_value=record),
                   export_source=Mock(), canonical_transfer_manifest=Mock(return_value=local),
                   reduce_atomically=reduce_source, checkpoint=checkpoint, publish=publish)
        self.assertFalse(self.refresh.reconcile_historical_manifest(state, {}))

        reduce_source.assert_not_called()  # mismatch is never reduced destructively
        publish.assert_not_called()
        self.assertEqual(len(captured), 1, "decision and marker were not one commit")
        committed = captured[0]
        self.assertEqual(committed["reconciliation"]["manifest_decision"],
                         "aggregate_mismatch_manual_review")
        self.assertTrue(committed["reconciliation"]["repair_manual_review_required"])
        self.assertEqual(committed["consumed_executions"]["manifest-exec"]["effect"],
                         "manifest_decision")
        self.assertNotIn("reconciliation_manifest", committed.get("pending_executions", {}))
        self.assertIn("historical_reconciliation", committed["source_errors"])


if __name__ == "__main__":
    unittest.main()
