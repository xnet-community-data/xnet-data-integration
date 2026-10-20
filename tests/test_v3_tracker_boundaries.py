"""Durability-boundary tests for the Dune execution tracker.

These pin the boundaries where a local checkpoint (state save/publication) must
never be mistaken for a retryable API/status error, where a returned execution
ID must be usable before it is persisted, and where consumption may be staged
without an early, non-atomic publication.
"""
import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_execution_tracker import (  # noqa: E402
    CheckpointFailed,
    ExecutionPending,
    ExecutionTracker,
    SubmissionRejected,
    is_ulid,
    valid_execution_id,
)
from v3_live_state_store import StateError  # noqa: E402

SPEC = {"key": "current", "query_id": 1, "max_run_credits": 1}
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class TrackerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.state = {"queries": {}}
        self.snapshots = []
        self.clock = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
        self.api = Mock()
        self.guard = Mock(return_value={"credits_used": 10})
        self.checkpoint = lambda: self.snapshots.append(copy.deepcopy(self.state))
        self.tracker = self.make_tracker(self.state)

    def make_tracker(self, state, performance="medium", checkpoint=None):
        return ExecutionTracker(
            state, self.api, checkpoint or self.checkpoint, self.guard,
            lambda: self.clock, self.advance, performance,
        )

    def advance(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def pending(self, **overrides):
        value = {"key": "current", "query_id": 1, "execution_id": "existing",
                 "query_parameters": {"lookback_hours": 48},
                 "submitted_at_utc": self.clock.isoformat(), "timeout_seconds": 90,
                 "billing": {"credits_used": 10},
                 "spec_identity": {"key": "current", "query_id": 1, "performance": "medium"}}
        value.update(overrides)
        self.state["pending_executions"] = {"current": value}
        return value

    def completed(self, **overrides):
        value = {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                 "execution_ended_at": "2026-10-07T10:00:03Z",
                 "result_metadata": {"total_row_count": 0, "column_names": []}}
        value.update(overrides)
        return value

    # --- execution-ID validation -------------------------------------------

    def test_execution_id_validation_rejects_unsafe_values(self):
        self.assertTrue(valid_execution_id("new"))
        self.assertTrue(valid_execution_id(ULID))
        self.assertTrue(valid_execution_id("opaque-token_01"))
        for bad in (None, "", 123, [], " ", "a b", "a/b", "a\\b", "..", ".", "a\nb", "x" * 129):
            with self.subTest(bad=bad):
                self.assertFalse(valid_execution_id(bad))
        self.assertTrue(is_ulid(ULID))
        self.assertFalse(is_ulid("new"))
        self.assertFalse(is_ulid(ULID.lower()))

    def test_checkpoint_failure_is_the_shared_state_error(self):
        self.assertTrue(issubclass(CheckpointFailed, StateError))
        self.assertTrue(issubclass(CheckpointFailed, RuntimeError))

    # --- durability barrier before submission ------------------------------

    def test_no_submission_when_intent_checkpoint_fails(self):
        failing = Mock(side_effect=RuntimeError("store publish failed"))
        tracker = self.make_tracker(self.state, checkpoint=failing)
        with self.assertRaises(CheckpointFailed) as ctx:
            tracker.execute(SPEC, {}, True, 90)
        self.assertIn("store publish failed", str(ctx.exception))
        self.api.assert_not_called()

    def test_slow_checkpoint_cannot_shift_a_relative_day_execution(self):
        self.clock=datetime(2026,10,7,23,57,tzinfo=timezone.utc)
        def checkpoint():
            self.snapshots.append(copy.deepcopy(self.state))
            self.clock=datetime(2026,10,7,23,59,tzinfo=timezone.utc)
        tracker=self.make_tracker(self.state,checkpoint=checkpoint)
        spec={**SPEC,"execution_not_after_utc":"2026-10-07T23:58:00Z"}
        with self.assertRaisesRegex(SubmissionRejected,"deadline"):
            tracker.execute(spec,{"oldest_days_ago":1,"newest_days_ago":1},True,90)
        self.api.assert_not_called()
        self.assertEqual(self.state["pending_executions"],{})
        obligation=next(iter(self.state["financial_journal"]["obligations"].values()))
        self.assertEqual(obligation["cost_credits"],0)
        self.assertEqual(obligation["basis"],"proven_nonacceptance")

    def test_failed_nonacceptance_checkpoint_is_still_a_durability_barrier(self):
        self.clock=datetime(2026,10,7,23,59,tzinfo=timezone.utc)
        count=0
        def checkpoint():
            nonlocal count
            count+=1
            if count==2:raise RuntimeError("uncertain rejection publication")
        tracker=self.make_tracker(self.state,checkpoint=checkpoint)
        with self.assertRaises(CheckpointFailed):
            tracker.execute({**SPEC,"execution_not_after_utc":"2026-10-07T23:58:00Z"},{},True,90)
        self.api.assert_not_called()

    def test_invalid_execution_id_is_not_persisted_and_blocks_replacement(self):
        self.api.side_effect = [{"execution_id": "a/b/../etc"}]
        with self.assertRaises(ExecutionPending) as ctx:
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertIn("invalid execution ID", str(ctx.exception))
        pending = self.state["pending_executions"]["current"]
        self.assertIsNone(pending["execution_id"])
        self.assertIn("invalid execution ID", pending["submission_error"])
        # The durable snapshot also carries only the ambiguous (ID-less) intent.
        self.assertIsNone(self.snapshots[-1]["pending_executions"]["current"]["execution_id"])

    def test_non_dict_execute_response_keeps_ambiguous_intent(self):
        self.api.side_effect = [["not", "a", "mapping"]]
        with self.assertRaises(ExecutionPending):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertIsNone(self.state["pending_executions"]["current"]["execution_id"])

    def test_id_checkpoint_failure_is_not_retried_as_resubmission(self):
        calls = {"n": 0}

        def checkpoint():
            calls["n"] += 1
            if calls["n"] == 2:  # pre-submit intent persisted; ID publish fails
                raise RuntimeError("journal write failed")
            self.snapshots.append(copy.deepcopy(self.state))

        tracker = self.make_tracker(self.state, checkpoint=checkpoint)
        self.api.side_effect = [{"execution_id": ULID}]
        with self.assertRaises(CheckpointFailed) as ctx:
            tracker.execute(SPEC, {}, True, 90)
        self.assertIn("journal write failed", str(ctx.exception))
        self.assertEqual(self.api.call_args_list[0].args[0], "query/1/execute")

        # Recover from the last durable snapshot (ID-less intent) and prove the
        # tracker refuses to resubmit rather than issuing a second paid execution.
        restored = copy.deepcopy(self.snapshots[-1])
        self.assertIsNone(restored["pending_executions"]["current"]["execution_id"])
        self.api.reset_mock()
        self.api.side_effect = None
        self.api.return_value = self.completed()
        recovery = self.make_tracker(restored)
        with self.assertRaises(ExecutionPending) as ctx:
            recovery.execute(SPEC, {}, True, 90)
        self.assertIn("unresolved submission without an ID", str(ctx.exception))
        self.api.assert_not_called()

    # --- recovery binds original operation evidence ------------------------

    def test_known_id_recovery_reuses_original_parameters_without_post(self):
        self.pending()
        self.api.return_value = self.completed()
        record = self.tracker.execute(SPEC, {"lookback_hours": 2}, True, 90)
        self.assertEqual(record["execution_id"], "existing")
        self.assertEqual(record["query_parameters"], {"lookback_hours": 48})
        self.assertEqual(record["spec_identity"],
                         {"key": "current", "query_id": 1, "performance": "medium"})
        self.api.assert_called_once_with("execution/existing/status")

    def test_recovered_operations_expose_original_evidence(self):
        self.pending()
        evidence = self.tracker.recovered_operations()
        self.assertEqual(evidence["current"]["query_parameters"], {"lookback_hours": 48})
        self.assertEqual(evidence["current"]["execution_id"], "existing")

    def test_incompatible_operation_identity_blocks_recovery(self):
        self.pending(spec_identity={"key": "current", "query_id": 1, "performance": "medium"})
        tracker = self.make_tracker(self.state, performance="large")
        with self.assertRaises(ExecutionPending) as ctx:
            tracker.execute(SPEC, {}, True, 90)
        self.assertIn("identity changed", str(ctx.exception))
        self.api.assert_not_called()

    def test_changed_query_id_blocks_recovery(self):
        self.pending()
        with self.assertRaises(ExecutionPending) as ctx:
            self.tracker.execute({**SPEC, "query_id": 2}, {}, True, 90)
        self.assertIn("Query id changed", str(ctx.exception))
        self.api.assert_not_called()

    # --- staged consumption / atomic publish -------------------------------

    def test_stage_acknowledge_does_not_publish_until_committed(self):
        self.pending()
        checkpoint = Mock()
        tracker = self.make_tracker(self.state, checkpoint=checkpoint)
        record = {"execution_id": "existing", "result_metadata": {"total_row_count": 7},
                  "export_points": 3}
        changed = tracker.stage_acknowledge(SPEC, record)
        self.assertTrue(changed)
        self.assertNotIn("current", self.state["pending_executions"])
        checkpoint.assert_not_called()
        tracker.commit_checkpoint()
        checkpoint.assert_called_once()

    def test_commit_checkpoint_wraps_failure_as_barrier(self):
        tracker = self.make_tracker(self.state, checkpoint=Mock(side_effect=OSError("disk")))
        with self.assertRaises(CheckpointFailed):
            tracker.commit_checkpoint()

    def test_default_acknowledge_still_publishes(self):
        self.pending()
        checkpoint = Mock()
        tracker = self.make_tracker(self.state, checkpoint=checkpoint)
        tracker.acknowledge(SPEC, {"execution_id": "existing"})
        checkpoint.assert_called_once()

    # --- reconcile never swallows the barrier ------------------------------

    def test_reconcile_propagates_checkpoint_failure(self):
        self.pending()
        self.api.return_value = self.completed()
        tracker = self.make_tracker(self.state, checkpoint=Mock(side_effect=RuntimeError("publish")))
        with self.assertRaises(CheckpointFailed):
            tracker.reconcile()

    def test_reconcile_still_reports_retryable_api_errors(self):
        self.pending()
        self.api.side_effect = RuntimeError("status temporarily unavailable")
        errors = self.tracker.reconcile()
        self.assertIn("current", errors)

    # --- definitive rejection path stays intact ----------------------------

    def test_definitive_rejection_releases_intent_and_audits(self):
        self.api.side_effect = SubmissionRejected("Dune HTTP 400: rejected")
        with self.assertRaises(SubmissionRejected):
            self.tracker.execute(SPEC, {}, True, 90)
        self.assertEqual(self.state["pending_executions"], {})
        self.assertEqual(self.state["rejected_submissions"][0]["query_id"], 1)


if __name__ == "__main__":
    unittest.main()
