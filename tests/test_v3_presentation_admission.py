"""A proven pre-POST budget denial must not consume a paid chart slot."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
module = importlib.util.spec_from_file_location(
    "presentation_admission_refresh_test", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(module)
module.loader.exec_module(refresh)
NOW = datetime(2026, 11, 20, 1, tzinfo=timezone.utc)


def queued_state():
    order = ["commercial", "network", "current", "pools", "burns", "holders", "tokenomics"]
    return {"schema_version": 1, "paused": False, "pending_executions": {},
            "queries": {key: {"completed_at_utc": (NOW - timedelta(days=3)).isoformat()}
                        for key in order},
            "presentation_last_attempts": {
                key: (NOW - timedelta(hours=8, minutes=-index)).isoformat()
                for index, key in enumerate(order)}}


class PresentationAdmissionTests(unittest.TestCase):
    def run_refresh(self, state, execute, guard=None, stage=None):
        config = {**refresh.CONFIG, "sources": [], "reconciliation": None}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(refresh, "now", return_value=NOW), \
             patch.object(refresh, "CONFIG", config), \
             patch.object(refresh, "ROOT", Path(directory)), \
             patch.object(refresh, "STATE", Path(directory) / "state.json"), \
             patch.object(refresh, "HEALTH", Path(directory) / "health.json"), \
             patch.object(refresh, "load_authoritative_state", return_value=state), \
             patch.object(refresh, "save"), patch.object(refresh, "checkpoint"), \
             patch.object(refresh, "run"), patch.object(refresh, "sync_independent_network"), \
             patch.object(refresh, "publish_data_generation"), \
             patch.object(refresh, "stage_consumption", side_effect=stage), \
             patch.object(refresh, "usage_guard", side_effect=guard or (lambda *args: {})), \
             patch.object(refresh, "execute", side_effect=execute), \
             patch.object(refresh, "api") as api, patch("sys.argv", ["refresh"]):
            self.assertEqual(refresh.main(), 0)
        return api

    def test_real_pre_post_guard_denial_leaves_five_slots_for_other_charts(self):
        state = queued_state()
        before_commercial = deepcopy(state["queries"]["commercial"])
        attempted, accepted = [], []

        def guard(state, proposed_spec=None, proposed_params=None):
            if proposed_spec and proposed_spec["key"] == "commercial":
                raise refresh.BudgetBlocked("Operation estimate exceeds its standing risk authority")
            return {}

        def execute(spec, params=None, enforce_cap=True, timeout_seconds=None, state=None):
            attempted.append(spec["key"])
            if spec["key"] == "commercial":
                # Exercise the REAL tracker/guard boundary, not a synthetic
                # classification of an arbitrary BudgetBlocked exception.
                return refresh.tracker(state, spec, params).execute(
                    spec, params, enforce_cap, timeout_seconds)
            accepted.append(spec["key"])
            return {"query_id": spec["query_id"], "execution_id": "paid-" + spec["key"],
                    "query_parameters": {}, "completed_at_utc": NOW.isoformat(),
                    "execution_cost_credits": 0.1}

        api = self.run_refresh(state, execute, guard)
        self.assertEqual(len(accepted), 5)
        self.assertEqual(len(attempted), 6)
        self.assertIn("network", accepted)
        self.assertIn("holders", accepted)
        self.assertEqual(state["queries"]["commercial"], before_commercial)
        self.assertIn("standing risk authority", state["presentation_errors"]["commercial"]["error"])
        self.assertTrue(state["presentation_errors"]["commercial"]["admission_denied_before_submission"])
        self.assertEqual(state["pending_executions"], {})
        api.assert_not_called()

    def test_all_local_denials_scan_only_the_finite_candidates_without_transport(self):
        state = queued_state()
        before = deepcopy(state["queries"])
        attempted = []

        def guard(state, proposed_spec=None, proposed_params=None):
            if proposed_spec is not None:
                raise refresh.BudgetBlocked("This operation is not admitted")
            return {}

        def execute(spec, params=None, enforce_cap=True, timeout_seconds=None, state=None):
            attempted.append(spec["key"])
            return refresh.tracker(state, spec, params).execute(spec, params, enforce_cap, timeout_seconds)

        api = self.run_refresh(state, execute, guard)
        self.assertEqual(len(attempted), len(refresh.CONFIG["presentation"]))
        self.assertEqual(len(set(attempted)), len(attempted))
        self.assertEqual(state["queries"], before)
        self.assertEqual(state["pending_executions"], {})
        api.assert_not_called()

    def test_global_admission_block_still_defers_all_new_chart_attempts(self):
        state = queued_state()
        attempted = []

        def guard(*args):
            raise refresh.BudgetBlocked("Global budget admission denied")

        def execute(spec, **kwargs):
            attempted.append(spec["key"])
            raise AssertionError("No new execution may reach this point")

        api = self.run_refresh(state, execute, guard)
        self.assertEqual(attempted, [])
        api.assert_not_called()

    def test_generic_budget_error_after_transport_keeps_its_slot(self):
        state = queued_state()
        attempted = []

        def execute(spec, **kwargs):
            attempted.append(spec["key"])
            refresh.api(f"query/{spec['query_id']}/execute", {"performance": "medium"})
            raise refresh.BudgetBlocked("Budget recheck after possible provider acceptance")

        api = self.run_refresh(state, execute)
        self.assertEqual(len(attempted), 5)
        self.assertEqual(api.call_count, 5)
        self.assertTrue(all(not issue.get("admission_denied_before_submission")
                            for issue in state["presentation_errors"].values()))

    def test_failure_after_execute_return_never_refunds_a_slot(self):
        state = queued_state()
        attempted = []

        def execute(spec, **kwargs):
            attempted.append(spec["key"])
            return {"query_id": spec["query_id"], "execution_id": "paid-" + spec["key"],
                    "completed_at_utc": NOW.isoformat(), "execution_cost_credits": 0.1}

        def stage(*args):
            raise refresh.ExecutionAdmissionDenied("Must not be treated as a pre-POST refusal here")

        self.run_refresh(state, execute, stage=stage)
        self.assertEqual(len(attempted), 5)
        self.assertTrue(all(not issue.get("admission_denied_before_submission")
                            for issue in state["presentation_errors"].values()))

    def test_terminal_failures_still_use_the_five_attempt_limit_and_backoff(self):
        state = queued_state()
        attempted = []

        def execute(spec, **kwargs):
            attempted.append(spec["key"])
            raise refresh.ExecutionFailed("Real terminal engine failure")

        self.run_refresh(state, execute)
        self.assertEqual(len(attempted), 5)
        self.assertTrue(all(issue["next_retry_at_utc"] for issue in state["presentation_errors"].values()))

    def test_quote_refusal_is_classified_before_tracker_or_transport(self):
        state = queued_state()
        spec = next(v for v in refresh.CONFIG["presentation"] if v["key"] == "commercial")
        with patch.object(refresh, "autonomous_enabled", return_value=True), \
             patch.object(refresh, "prepare_standing_spec", side_effect=refresh.BudgetBlocked("Unknown estimate")), \
             patch.object(refresh, "tracker") as tracker, patch.object(refresh, "api") as api:
            with self.assertRaises(refresh.ExecutionAdmissionDenied):
                refresh.execute(spec, state=state)
        tracker.assert_not_called()
        api.assert_not_called()

    def test_real_scope_refusal_is_classified_before_any_metadata_or_transport(self):
        state = queued_state()
        state["standing_authorization"] = {
            "approval_ref": refresh.CONFIG["autonomous_operation"]["approval_ref"],
            "policy_sha256": refresh.standing_authority_hash(refresh.CONFIG["autonomous_operation"]),
        }
        with patch.object(refresh, "now", return_value=NOW), patch.object(refresh, "api") as api:
            with self.assertRaisesRegex(refresh.ExecutionAdmissionDenied, "standing query authority"):
                refresh.execute({"key": "commercial", "query_id": 999999}, state=state)
        api.assert_not_called()

    def test_real_quote_preparation_uses_only_metadata_before_guard_denies(self):
        state = queued_state()
        state["standing_authorization"] = {
            "approval_ref": refresh.CONFIG["autonomous_operation"]["approval_ref"],
            "policy_sha256": refresh.standing_authority_hash(refresh.CONFIG["autonomous_operation"]),
        }
        spec = next(v for v in refresh.CONFIG["presentation"] if v["key"] == "commercial")
        sql = (ROOT / "dune/v3/sql/71_commercial_history.sql").read_text()
        paths = []

        def metadata(path, payload=None, **kwargs):
            self.assertIsNone(payload, "Pre-admission preparation must never POST")
            self.assertEqual(path, f"query/{spec['query_id']}")
            paths.append(path)
            return {"query_id": spec["query_id"], "query_sql": sql, "version": 8}

        quote = {"status": "estimated", "cost_estimate_credits": 13.33, "expected_credits": 0.88,
                 "evidence": {"test": "exact-query observations with retained outlier"}}
        with patch.object(refresh, "now", return_value=NOW), \
             patch.object(refresh, "api", side_effect=metadata), \
             patch.object(refresh, "autonomous_estimate", return_value=quote), \
             patch.object(refresh, "usage_guard", side_effect=refresh.BudgetBlocked("Operation risk exceeded")):
            with self.assertRaises(refresh.ExecutionAdmissionDenied):
                refresh.execute(spec, state=state)
        self.assertEqual(paths, [f"query/{spec['query_id']}"])
        self.assertEqual(state["pending_executions"], {})

    def test_two_pre_post_denials_leave_at_most_five_real_transport_attempts(self):
        state = queued_state()
        attempted = []

        def guard(state, proposed_spec=None, proposed_params=None):
            if proposed_spec and proposed_spec["key"] in {"commercial", "network"}:
                raise refresh.BudgetBlocked("Operation risk exceeded")
            return {}

        def execute(spec, params=None, enforce_cap=True, timeout_seconds=None, state=None):
            attempted.append(spec["key"])
            if spec["key"] in {"commercial", "network"}:
                return refresh.tracker(state, spec, params).execute(spec, params, enforce_cap, timeout_seconds)
            refresh.api(f"query/{spec['query_id']}/execute", {"performance": "medium"})
            raise refresh.ExecutionFailed("Terminal provider failure still counts")

        api = self.run_refresh(state, execute, guard)
        self.assertEqual(len(attempted), 7)
        self.assertEqual(api.call_count, 5)

    def test_late_denial_is_not_even_visited_after_five_counted_attempts(self):
        state = queued_state()
        attempted = []

        def execute(spec, **kwargs):
            attempted.append(spec["key"])
            if spec["key"] == "holders":
                raise refresh.ExecutionAdmissionDenied("Must not be reached beyond the bound")
            refresh.api(f"query/{spec['query_id']}/execute", {"performance": "medium"})
            return {"query_id": spec["query_id"], "execution_id": "paid-" + spec["key"],
                    "completed_at_utc": NOW.isoformat(), "execution_cost_credits": 0.1}

        api = self.run_refresh(state, execute)
        self.assertEqual(len(attempted), 5)
        self.assertNotIn("holders", attempted)
        self.assertEqual(api.call_count, 5)

    def test_cross_chart_unknown_submission_still_prevents_all_new_attempts(self):
        state = queued_state()
        state["pending_executions"]["other-source"] = {"query_id": 123, "execution_id": None}
        attempted = []

        def execute(spec, **kwargs):
            attempted.append(spec["key"])
            raise refresh.ExecutionAdmissionDenied("A refund cannot reopen the unknown-ID fence")

        api = self.run_refresh(state, execute)
        self.assertEqual(attempted, [])
        self.assertIsNone(state["pending_executions"]["other-source"]["execution_id"])
        api.assert_not_called()

    def test_durability_errors_are_not_converted_to_refundable_admission_denials(self):
        state = queued_state()
        spec = next(v for v in refresh.CONFIG["presentation"] if v["key"] == "commercial")
        with patch.object(refresh, "autonomous_enabled", return_value=True), \
             patch.object(refresh, "prepare_standing_spec", side_effect=refresh.StateError("CAS uncertainty")), \
             patch.object(refresh, "tracker") as tracker:
            with self.assertRaises(refresh.StateError):
                refresh.execute(spec, state=state)
        tracker.assert_not_called()

    def test_unknown_existing_submission_never_reaches_the_admission_refund_boundary(self):
        state = queued_state()
        state["pending_executions"]["unresolved"] = {"query_id": 123, "execution_id": None}
        spec = next(v for v in refresh.CONFIG["presentation"] if v["key"] == "commercial")
        before = deepcopy(state["pending_executions"])
        with patch.object(refresh, "usage_guard") as guard, patch.object(refresh, "api") as api:
            with self.assertRaises(refresh.ExecutionPending):
                refresh.tracker(state, spec, {}).execute(spec, {}, True, 90)
        guard.assert_not_called()
        api.assert_not_called()
        self.assertEqual(state["pending_executions"], before)

    def test_known_execution_reconciliation_bypasses_new_spend_guard_without_losing_id(self):
        state = queued_state()
        spec = next(v for v in refresh.CONFIG["presentation"] if v["key"] == "holders")
        entry = {"query_id": spec["query_id"], "execution_id": "existing-paid-id",
                 "query_parameters": {}, "timeout_seconds": 90, "billing": {},
                 "terminal_status": {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                                     "execution_ended_at": NOW.isoformat()}}
        state["pending_executions"]["holders"] = deepcopy(entry)
        with patch.object(refresh, "usage_guard", side_effect=refresh.BudgetBlocked("New spending denied")) as guard, \
             patch.object(refresh, "api") as api:
            record = refresh.tracker(state, spec, {}).execute(spec, {}, True, 90)
        self.assertEqual(record["execution_id"], entry["execution_id"])
        self.assertEqual(state["pending_executions"]["holders"], entry)
        guard.assert_not_called()
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
