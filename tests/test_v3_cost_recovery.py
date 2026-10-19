"""Completed-cost recovery and future source admission. Fake provider only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_execution_tracker import (
    ExecutionTracker, ExecutionPending, ExecutionCostExceeded, CostBreakerOpen, CheckpointFailed,
)
from v3_credit_budget import BudgetInputError
from v3_live_state_store import validate_journal

SPEC = {"key": "transfers", "query_id": 1, "max_run_credits": 1,
        "performance": "medium", "shape_version": "sql-v1", "cost_breaker_cooldown_seconds": 60}


class CompletedCostRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = {"schema_version": 1, "queries": {}}
        self.clock = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
        self.snapshots = []
        self.api = Mock(side_effect=[{"execution_id": "already-paid"}, self.complete(2)])
        self.guard = Mock(return_value={"credits_used": 100})
        self.tracker = self.make_tracker(self.state)

    def complete(self, cost):
        return {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": cost,
                "execution_ended_at": self.clock.isoformat(),
                "result_metadata": {"column_names": ["a"], "total_row_count": 0}}

    def make_tracker(self, state):
        return ExecutionTracker(state, self.api, lambda: self.snapshots.append(deepcopy(state)),
                                self.guard, lambda: self.clock, lambda _: None, "medium")

    def detect(self, accept=False):
        spec = {**SPEC, "accept_completed_outlier": accept}
        if accept:
            return self.tracker.execute(spec, {"lookback_hours": 2}, True, 90)
        with self.assertRaises(ExecutionCostExceeded):
            self.tracker.execute(spec, {"lookback_hours": 2}, True, 90)

    def next_spec(self):
        spec = {**SPEC, "financial_attempt_id": "next-attempt"}
        spec["cost_admission_quote"] = {
            "operation_id": "next-attempt", "query_id": 1, "performance": "medium", "shape_version": "sql-v1",
            "query_parameters": {"lookback_hours": 2}, "cost_estimate_credits": 2.5,
            "estimate_evidence": "fixture: reviewed comparable costs including already-paid",
            "approval_ref": "fixture: separate next-operation user decision",
            "quoted_at_utc": self.clock.isoformat(),
            "valid_until_utc": (self.clock + timedelta(seconds=60)).isoformat(),
            "outlier_execution_ids": ["already-paid"],
        }
        return spec

    def test_refusal_preserves_completed_pending_id_and_does_not_consume(self):
        self.detect()
        pending = self.snapshots[-1]["pending_executions"]["transfers"]
        self.assertEqual(pending["execution_id"], "already-paid")
        self.assertEqual(pending["terminal_status"]["execution_cost_credits"], 2)
        self.assertNotIn("already-paid", self.state.get("consumed_executions", {}))
        self.assertIn("already-paid", self.state["cost_outliers"])

    def test_opt_in_recovers_same_execution_after_restart_without_guard_or_post(self):
        self.detect()
        restored = validate_journal(json.dumps(self.snapshots[-1]).encode())
        original_until = restored["cost_breakers"]["transfers"]["cooldown_until_utc"]
        self.clock += timedelta(seconds=30)
        self.api.reset_mock()
        self.api.side_effect = AssertionError("No provider call is needed for cached terminal recovery")
        self.guard.reset_mock()
        self.guard.side_effect = AssertionError("New-spend admission must not gate spent work")
        record = self.make_tracker(restored).execute({**SPEC, "accept_completed_outlier": True},
                                                   {"lookback_hours": 999}, True, 90)
        self.assertEqual(record["execution_id"], "already-paid")
        self.assertEqual(record["query_parameters"], {"lookback_hours": 2})
        self.assertEqual(record["execution_cost_credits"], 2)
        self.assertEqual(record["cost_outlier"]["recovery_policy"], "accept_completed_outlier")
        self.assertTrue(record["cost_outlier"]["returned_for_validation"])
        self.assertEqual(restored["cost_breakers"]["transfers"]["cooldown_until_utc"], original_until)
        self.assertIn("transfers", restored["pending_executions"], "returning work is not accepting its effects")
        self.assertEqual(self.snapshots[-1]["cost_outliers"]["already-paid"]["recovery_policy"],
                         "accept_completed_outlier")
        self.api.assert_not_called()
        self.guard.assert_not_called()

    def test_opt_in_on_first_completion_audits_cost_and_still_requires_acknowledgment(self):
        record = self.detect(accept=True)
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.state["financial_journal"]["obligations"]["exec:already-paid"]["cost_credits"], 2)
        self.assertFalse(record["cost_outlier"]["threshold_is_precharge_cap"])
        self.assertIn("transfers", self.state["pending_executions"])
        self.tracker.acknowledge(SPEC, record)
        self.assertNotIn("transfers", self.state["pending_executions"])
        self.assertEqual(self.state["execution_history"][0]["cost_outlier"]["recovery_policy"],
                         "accept_completed_outlier")

    def test_threshold_change_or_repeated_refusal_does_not_erase_or_extend_outlier(self):
        self.detect()
        original = deepcopy(self.state["cost_breakers"])
        self.clock += timedelta(seconds=120)
        self.api.reset_mock()
        with self.assertRaises(ExecutionCostExceeded):
            self.tracker.execute({**SPEC, "max_run_credits": 100}, {}, True, 90)
        self.assertEqual(self.state["cost_breakers"], original)
        self.api.assert_not_called()

    def test_durable_outlier_audit_survives_missing_legacy_pending_annotations(self):
        self.detect()
        pending = self.state["pending_executions"]["transfers"]
        pending.pop("cost_outlier")
        pending.pop("post_run_threshold_credits")
        self.api.reset_mock()
        with self.assertRaisesRegex(ExecutionCostExceeded, "exceeded 1"):
            self.tracker.execute({**SPEC, "max_run_credits": 100}, {}, True, 90)
        self.assertEqual(pending["execution_id"], "already-paid")
        self.api.assert_not_called()

    def test_unknown_id_cannot_be_accepted_even_with_completed_cache_and_opt_in(self):
        self.state["pending_executions"] = {"transfers": {
            "key": "transfers", "query_id": 1, "execution_id": None,
            "query_parameters": {}, "submitted_at_utc": self.clock.isoformat(),
            "terminal_status": self.complete(2)}}
        with self.assertRaises(ExecutionPending):
            self.tracker.execute({**SPEC, "accept_completed_outlier": True}, {}, True, 90)
        self.api.assert_not_called()

    def test_checkpoint_failure_blocks_return_and_same_session_cannot_bypass(self):
        self.tracker.checkpoint = Mock(side_effect=RuntimeError("receipt lost"))
        self.state["pending_executions"] = {"transfers": {
            "key": "transfers", "query_id": 1, "execution_id": "already-paid", "billing": {},
            "query_parameters": {}, "submitted_at_utc": self.clock.isoformat(),
            "terminal_status": self.complete(2)}}
        with self.assertRaises(CheckpointFailed):
            self.tracker.execute({**SPEC, "accept_completed_outlier": True}, {}, True, 90)
        self.tracker.checkpoint = Mock()
        with self.assertRaises(CheckpointFailed):
            self.tracker.execute({**SPEC, "accept_completed_outlier": True}, {}, True, 90)
        self.tracker.checkpoint.assert_not_called()
        self.assertEqual(self.state["pending_executions"]["transfers"]["execution_id"], "already-paid")
        self.api.assert_not_called()

    def test_breaker_cooldown_and_fresh_quote_are_both_required_only_for_new_work(self):
        record = self.detect(accept=True)
        self.tracker.acknowledge(SPEC, record)
        self.api.reset_mock()
        with self.assertRaises(CostBreakerOpen):
            self.tracker.execute(self.next_spec(), {"lookback_hours": 2}, True, 90)
        self.clock += timedelta(seconds=60)
        with self.assertRaises(CostBreakerOpen):
            self.tracker.execute(SPEC, {"lookback_hours": 2}, True, 90)
        self.api.assert_not_called()
        status = self.tracker.cost_breaker_status(self.next_spec(), {"lookback_hours": 2})
        self.assertEqual(status["status"], "ready_for_admission")
        self.assertFalse(status["estimate_is_hard_cap"])

    def test_fresh_quote_does_not_bypass_guard_and_is_bound_to_next_attempt(self):
        record = self.detect(accept=True)
        self.tracker.acknowledge(SPEC, record)
        self.clock += timedelta(seconds=60)
        spec = self.next_spec()
        self.api.reset_mock()
        self.guard.side_effect = RuntimeError("current allowance/guard refuses")
        with self.assertRaisesRegex(RuntimeError, "guard refuses"):
            self.tracker.execute(spec, {"lookback_hours": 2}, True, 90)
        self.api.assert_not_called()
        self.guard.side_effect = None
        self.api.side_effect = [{"execution_id": "new-approved"}, self.complete(0.2)]
        result = self.tracker.execute(spec, {"lookback_hours": 2}, True, 90)
        self.assertEqual(result["execution_id"], "new-approved")
        before_post = next(snapshot["pending_executions"]["transfers"] for snapshot in self.snapshots
                           if snapshot.get("pending_executions", {}).get("transfers", {}).get("financial_attempt_id") == "next-attempt"
                           and snapshot["pending_executions"]["transfers"]["execution_id"] is None)
        self.assertEqual(before_post["cost_admission_quote"]["approval_ref"], spec["cost_admission_quote"]["approval_ref"])
        self.assertEqual(before_post["cost_admission_quote"]["cost_estimate_credits"], 2.5)
        self.tracker.acknowledge(spec, result)
        calls = self.api.call_count
        with self.assertRaises(ExecutionPending):
            self.tracker.execute(spec, {"lookback_hours": 2}, True, 90)
        self.assertEqual(self.api.call_count, calls, "a prior operation quote cannot buy another POST")

    def test_stale_mismatched_or_unreviewed_quote_cannot_reopen_source(self):
        record = self.detect(accept=True)
        self.tracker.acknowledge(SPEC, record)
        self.clock += timedelta(seconds=60)
        for field, value in (("query_parameters", {"lookback_hours": 3}), ("performance", "small"),
                             ("operation_id", "another"), ("outlier_execution_ids", []),
                             ("estimate_evidence", ""), ("approval_ref", ""),
                             ("valid_until_utc", self.clock.isoformat())):
            spec = self.next_spec()
            spec["cost_admission_quote"][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.tracker.cost_breaker_status(spec, {"lookback_hours": 2})["status"], "blocked")

    def test_breaker_is_not_a_global_pause_and_survives_sample_retention(self):
        self.detect(accept=True)
        self.state["execution_history"] = []
        self.assertEqual(self.tracker.cost_breaker_status({"key": "network", "query_id": 2})["status"], "clear")
        renamed = {**SPEC, "key": "renamed-same-shape"}
        self.assertEqual(self.tracker.cost_breaker_status(renamed, {"lookback_hours": 2})["status"], "blocked")
        self.assertIn("already-paid", self.state["cost_outliers"])

    def test_non_boolean_policy_is_rejected_before_spending(self):
        with self.assertRaises(BudgetInputError):
            self.tracker.execute({**SPEC, "accept_completed_outlier": "yes"}, {}, True, 90)
        self.api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
