"""Financial failure boundaries; fake providers/local JSON only, no paid work."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_execution_tracker import ExecutionTracker, ExecutionPending, SubmissionRejected, CheckpointFailed
from v3_financial_journal import (
    financial_obligations, synchronize_financial_journal, export_attempt_id,
    record_export_attempt, record_export_outcome, open_allowance, evaluate_allowance as allowance_report,
    reserve_allowance,
)
from v3_live_state_store import validate_journal
from v3_credit_budget import BudgetInputError, normalize_usage, outstanding_liabilities
from tests.test_v3_budget_runtime import REAL_CONFIG, tracker_state, tracker_history, run, usage, FRESH_NOW, PERIOD_START, PERIOD_END

NOW = datetime(2026, 11, 3, 23, 59, tzinfo=timezone.utc)


def evaluate_allowance(state, allowance_id, payload, instant, **overrides):
    policy = {"guard_credits": 1800, "minimum_reserve_credits": 15, "monthly_target_credits": 2500, **overrides}
    return allowance_report(state, allowance_id, payload, instant, **policy)


class ObligationTests(unittest.TestCase):
    def tracker(self, state, api=None, checkpoint=None):
        return ExecutionTracker(state, api or Mock(), checkpoint or Mock(), lambda: {},
                                lambda: NOW, lambda _: None, "medium")

    def test_telemetry_trim_and_restart_never_erase_unknown_or_known_debt(self):
        history = [{"execution_id": f"old-{i}", "query_id": 1, "query_parameters": {},
                    "execution_cost_credits": None if i == 0 else 0.1,
                    "execution_ended_at": NOW.isoformat()} for i in range(1000)]
        pending = {"key": "current", "query_id": 1, "query_parameters": {},
                   "execution_id": "new", "submitted_at_utc": NOW.isoformat()}
        state = {"schema_version": 1, "queries": {}, "execution_history": history,
                 "pending_executions": {"current": pending}}
        snapshots = []
        tracker = self.tracker(state, checkpoint=lambda: snapshots.append(deepcopy(state)))
        tracker.observe(pending, {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.2})
        self.assertEqual(len(state["execution_history"]), 1000)
        restored = validate_journal(json.dumps(snapshots[-1]).encode())
        restored["pending_executions"] = {}
        items, _ = financial_obligations(restored)
        self.assertEqual(len([i for i in items if i["kind"] == "execution"]), 1001)
        self.assertIsNone(next(i for i in items if i["execution_id"] == "old-0").get("cost_credits"))
        api = Mock(return_value={"state": "QUERY_STATE_FAILED", "execution_cost_credits": 0.7})
        self.tracker(restored, api).reconcile()
        api.assert_called_once_with("execution/old-0/status")
        self.assertEqual(restored["financial_journal"]["obligations"]["exec:old-0"]["cost_credits"], 0.7)
        self.assertEqual(len(restored["execution_history"]), 1000)

    def test_result_metadata_without_a_read_creates_no_export_debt_or_samples(self):
        state = tracker_state()
        state["execution_history"].append({"execution_id": "presentation", "query_id": 999,
            "query_parameters": {}, "execution_cost_credits": 0.2,
            "result_bytes": 1_000_000, "result_row_count": 100,
            "execution_ended_at": NOW.isoformat()})
        items, _ = financial_obligations(state)
        self.assertFalse(any(i["kind"] == "export" and i.get("execution_id") == "presentation" for i in items))

    def test_nonterminal_cost_reading_cannot_resolve_unknown_final_cost(self):
        state = {"execution_history": [{"execution_id": "unknown", "query_id": 1,
                 "query_parameters": {}, "execution_cost_credits": None}]}
        api = Mock(return_value={"state": "QUERY_STATE_EXECUTING", "execution_cost_credits": 0.2})
        self.tracker(state, api).reconcile()
        self.assertIsNone(state["financial_journal"]["obligations"]["exec:unknown"]["cost_credits"])

    def test_current_export_of_old_execution_retained_and_unknown_never_aged_out(self):
        state = {"execution_history": [{"execution_id": "old", "query_id": 1,
                 "query_parameters": {}, "execution_cost_credits": 0.3,
                 "execution_ended_at": "2026-09-01T00:00:00Z"}]}
        record_export_attempt(state, "read-now", execution_id="old", requested_at_utc=NOW.isoformat(),
                              risk_credits=2, estimate_evidence="three comparable wire observations")
        record_export_outcome(state, "read-now", observed_credits=1.5)
        record_export_attempt(state, "lost-old-read", execution_id="old",
                              requested_at_utc="2026-09-01T00:00:00Z", risk_credits=1,
                              estimate_evidence="observations")
        items, excluded = financial_obligations(state, period_start_utc=PERIOD_START)
        self.assertIn("exec:old", excluded)
        self.assertEqual({i["liability_id"] for i in items}, {"read-now", "lost-old-read"})
        self.assertNotIn("cost_credits", next(i for i in items if i["liability_id"] == "lost-old-read"))

    def test_submission_date_cannot_exclude_straddling_compute_without_end(self):
        state = {"execution_history": [{"execution_id": "straddled", "execution_cost_credits": 2,
                 "submitted_at_utc": "2026-09-01T00:00:00Z"}]}
        items, excluded = financial_obligations(state, period_start_utc=PERIOD_START)
        self.assertEqual(excluded, [])
        self.assertEqual(items[0]["cost_credits"], 2)

    def test_two_read_attempts_of_same_execution_are_two_obligations(self):
        state = {}
        for attempt in ("read-1", "read-2"):
            record_export_attempt(state, attempt, execution_id="same", requested_at_utc=NOW.isoformat(),
                                  risk_credits=2, estimate_evidence="wire observations")
            record_export_outcome(state, attempt, observed_credits=1)
        items, _ = financial_obligations(state)
        self.assertEqual(sum(i["cost_credits"] for i in items), 2)
        before = deepcopy(state)
        with self.assertRaises(BudgetInputError):
            record_export_attempt(state, "read-1", execution_id="same", requested_at_utc=NOW.isoformat(),
                                  risk_credits=2, estimate_evidence="wire observations")
        self.assertEqual(state, before)

    def test_marker_timestamp_change_cannot_overwrite_previous_attempt(self):
        marker = {"status": "cached", "execution_id": "same", "risk_credits": 1,
                  "requested_at_utc": "2026-11-03T21:00:00Z"}
        state = {"result_exports": {"requests": {"key": marker}}}
        synchronize_financial_journal(state)
        marker["requested_at_utc"] = "2026-11-03T22:00:00Z"
        synchronize_financial_journal(state)
        items, _ = financial_obligations(state)
        self.assertEqual(len(items), 2)

    def test_explicit_attempt_can_bind_cache_marker_without_double_counting(self):
        state = {"result_exports": {"requests": {"cache-key": {"status": "cached",
                 "execution_id": "same", "requested_at_utc": NOW.isoformat(), "risk_credits": 2}}}}
        record_export_attempt(state, "unique-read-attempt", execution_id="same",
                              requested_at_utc=NOW.isoformat(), risk_credits=2,
                              estimate_evidence="wire observations", cache_marker_key="cache-key")
        record_export_outcome(state, "unique-read-attempt", observed_credits=1)
        synchronize_financial_journal(state)
        items, _ = financial_obligations(state)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["liability_id"], "unique-read-attempt")
        self.assertEqual(items[0]["cost_credits"], 1)

    def test_unknown_submission_is_durable_before_post_and_resolves_only_by_id(self):
        state = {"queries": {}}
        snapshots = []
        api = Mock(side_effect=TimeoutError("response lost"))
        with self.assertRaises(ExecutionPending):
            self.tracker(state, api, lambda: snapshots.append(deepcopy(state))).execute(
                {"key": "current", "query_id": 1, "max_run_credits": 1}, {}, True, 90)
        self.assertTrue(snapshots[0]["financial_journal"]["obligations"])
        state["pending_executions"] = {}  # Even a mistaken old-lane deletion cannot erase the debt.
        items, _ = financial_obligations(state)
        self.assertEqual(len(items), 1)
        self.assertIsNone(items[0]["cost_credits"])

    def test_proven_rejection_keeps_zero_cost_audit_not_unknown_debt(self):
        state = {"queries": {}}
        with self.assertRaises(SubmissionRejected):
            self.tracker(state, Mock(side_effect=SubmissionRejected("proven nonacceptance"))).execute(
                {"key": "current", "query_id": 1, "max_run_credits": 1}, {}, True, 90)
        items, _ = financial_obligations(state)
        self.assertEqual([i["cost_credits"] for i in items], [0])

    def test_actual_engine_shape_and_attempt_survive_status_reconciliation(self):
        state = {"queries": {}}
        api = Mock(side_effect=[{"execution_id": "new"}, {"state": "QUERY_STATE_COMPLETED",
                                "execution_cost_credits": 0.2}])
        tracker = self.tracker(state, api)
        record = tracker.execute({"key": "current", "query_id": 1, "max_run_credits": 1,
                                  "performance": "small", "shape_version": "sql-v2"}, {}, True, 90)
        tracker.reconcile()
        row = state["execution_history"][0]
        self.assertEqual((row["performance"], row["shape_version"]), ("small", "sql-v2"))
        self.assertTrue(row["financial_attempt_id"])
        self.assertEqual(len(state["financial_journal"]["obligations"]), 1)

    def test_financial_checkpoint_failure_prevents_post(self):
        state = {"financial_journal": {"schema_version": 999}}
        api = Mock()
        with self.assertRaises(CheckpointFailed):
            self.tracker(state, api).execute({"key": "a", "query_id": 1}, {}, True, 90)
        api.assert_not_called()

    def test_usage_shapes_share_one_period_and_liability_filter(self):
        raw = {"billingPeriods": [{"start_date": PERIOD_START, "end_date": PERIOD_END,
                                   "credits_used": 900, "credits_included": 2500}]}
        state = tracker_state()
        state["execution_history"].append({"execution_id": "old", "query_id": 1,
            "query_parameters": {}, "execution_cost_credits": 2,
            "execution_ended_at": "2026-09-01T00:00:00Z"})
        normalized, provider = run(state=state), run(state=state, usage_payload=raw)
        self.assertEqual(normalized["report"]["totals"], provider["report"]["totals"])
        self.assertIn("exec:old", provider["diagnostics"]["liabilities"]["prior_period_excluded_ids"])

    def test_estimator_policy_propagates_to_proposal_and_unknown_base_does_not_crash(self):
        config = deepcopy(REAL_CONFIG)
        config["budget_policy"] = {"min_comparable_samples": 1, "sample_max_age_hours": 24,
                                   "variance_uplift_sigma": 5}
        history = tracker_history()[::3]
        proposal = {"query_id": 8894185, "key": "current"}
        d = run(state=tracker_state(history=history), config=config, proposed_spec=proposal)
        self.assertEqual(d["status"], "admitted")
        self.assertEqual(d["proposed_evaluation"]["evidence"]["sample_count"], 1)
        d = run(state=tracker_state(history=history), config=config,
                usage_payload={"billing_periods": []}, proposed_spec=proposal)
        self.assertEqual(d["status"], "blocked")
        self.assertEqual(d["proposed_evaluation"]["status"], "blocked")
        config["budget_policy"]["min_comparable_samples"] = 2
        self.assertEqual(run(state=tracker_state(history=history), config=config,
                             proposed_spec=proposal)["proposed_evaluation"]["status"], "blocked")

    def test_unknown_duplicate_dominates_and_settled_flags_have_no_authority(self):
        for items in ([{"liability_id": "a", "cost_credits": 2}, {"liability_id": "a"}],
                      [{"liability_id": "a"}, {"liability_id": "a", "cost_credits": 2}]):
            result = outstanding_liabilities(items)
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["known_floor_credits"], 2)
            self.assertFalse(result["exposure_is_fully_known"])
            self.assertEqual(result["unknown"][0]["liability_id"], "a")


class AllowanceTests(unittest.TestCase):
    def setUp(self):
        self.state = {"schema_version": 1, "queries": {}}
        self.authority = {"allowance_id": "fixture-only", "authorization_ref": "test-user-decision",
            "authorized_by": "test-user", "authorized_at_utc": "2026-11-03T20:00:00Z",
            "period_start_utc": PERIOD_START, "period_end_utc": PERIOD_END,
            "allowance_credits": 100, "external_risk_reserve_credits": 20}

    def open(self, **kwargs):
        return open_allowance(self.state, self.authority, kwargs.pop("usage", usage(100)), FRESH_NOW,
                              guard_credits=1800, minimum_reserve_credits=15,
                              monthly_target_credits=kwargs.pop("monthly_target_credits", 2500), **kwargs)

    def reserve(self, op="op", quote=10, **kwargs):
        approval = {"operation_id": op, "quote_credits": quote,
                    "approval_ref": "test-specific-operation-approval", "estimate_evidence": "test-observations"}
        return reserve_allowance(self.state, "fixture-only", op, quote_credits=quote, approval=approval,
                                 liability_id=kwargs.pop("liability_id", "exec:" + op),
                                 usage=kwargs.pop("usage", usage(100)), now_utc=FRESH_NOW,
                                  guard_credits=kwargs.pop("guard_credits", 1800),
                                  minimum_reserve_credits=kwargs.pop("minimum_reserve_credits", 15),
                                  monthly_target_credits=kwargs.pop("monthly_target_credits", 2500), **kwargs)

    def test_opening_exposure_unknown_is_not_allocated_away_or_mutated(self):
        self.state["execution_history"] = [{"execution_id": "unknown", "query_id": 1, "query_parameters": {}}]
        before = deepcopy(self.state)
        with self.assertRaisesRegex(BudgetInputError, "Unknown opening"):
            self.open()
        self.assertEqual(self.state, before)

    def test_no_authority_no_allowance_and_existing_guard_is_not_raised(self):
        with self.assertRaises(BudgetInputError):
            evaluate_allowance(self.state, "fixture-only", usage(), FRESH_NOW)
        with self.assertRaisesRegex(BudgetInputError, "opening headroom"):
            self.open(usage=usage(1798.673))
        self.assertNotIn("financial_journal", self.state)

    def test_opening_known_cost_is_charged_once_not_once_per_usage_snapshot(self):
        self.state["execution_history"] = [{"execution_id": "opening", "query_id": 1,
            "query_parameters": {}, "execution_cost_credits": 2, "execution_ended_at": NOW.isoformat()}]
        self.open()
        self.reserve()
        for used in (100, 110, 102, 112):
            report = evaluate_allowance(self.state, "fixture-only", usage(used), FRESH_NOW)
            self.assertEqual(report["committed_credits"], 10)
            self.assertEqual(report["remaining_credits"], 90)
            self.assertTrue(report["authorization_required"])

    def test_replay_no_refund_overrun_and_serialized_restart(self):
        self.open()
        self.reserve(quote=10)
        self.assertEqual(self.reserve(quote=10)["committed_credits"], 10)
        with self.assertRaises(BudgetInputError):
            self.reserve(quote=5)
        self.state["execution_history"] = [{"execution_id": "op", "query_id": 1,
            "query_parameters": {}, "execution_cost_credits": 15, "execution_ended_at": NOW.isoformat()}]
        synchronize_financial_journal(self.state)
        self.state = validate_journal(json.dumps(self.state).encode())
        self.state["execution_history"] = []
        self.assertEqual(evaluate_allowance(self.state, "fixture-only", usage(115), FRESH_NOW)["remaining_credits"], 85)
        self.state["execution_history"] = [{"execution_id": "op", "query_id": 1,
            "query_parameters": {}, "execution_cost_credits": 1, "execution_ended_at": NOW.isoformat()}]
        synchronize_financial_journal(self.state)
        self.assertEqual(evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)["remaining_credits"], 85)

    def test_no_second_period_allocation_and_no_mutation_on_failed_reservation(self):
        self.open()
        before = deepcopy(self.state)
        with self.assertRaises(BudgetInputError):
            self.reserve(quote=101)
        self.assertEqual(self.state, before)
        self.authority["allowance_id"] = "another"
        with self.assertRaises(BudgetInputError):
            self.open()
        self.assertEqual(self.state, before)

    def test_equivalent_timezone_spelling_cannot_allocate_period_twice(self):
        self.open()
        before = deepcopy(self.state)
        self.authority["allowance_id"] = "another"
        self.authority["period_start_utc"] = "2026-10-04T17:35:49+01:00"
        with self.assertRaisesRegex(BudgetInputError, "already allocated"):
            self.open()
        self.assertEqual(self.state, before)

    def test_account_drift_and_renewal_are_independent_stops_not_replenishment(self):
        self.open()
        self.reserve()
        drift = evaluate_allowance(self.state, "fixture-only", usage(150), FRESH_NOW)
        self.assertIn("account_usage_drift", drift["blockers"])
        self.assertEqual(drift["remaining_credits"], 90)
        renewed = usage(0, period_start_utc=PERIOD_END, period_end_utc="2026-12-04T00:00:00Z")
        report = evaluate_allowance(self.state, "fixture-only", renewed, "2026-11-05T00:00:00Z")
        self.assertIn("allowance_period_mismatch", report["blockers"])

    def test_no_reservation_without_operation_bound_approval(self):
        self.open()
        before = deepcopy(self.state)
        with self.assertRaises(BudgetInputError):
            reserve_allowance(self.state, "fixture-only", "op", quote_credits=10, approval=None,
                              liability_id="exec:op", usage=usage(100), now_utc=FRESH_NOW,
                              guard_credits=1800, minimum_reserve_credits=15, monthly_target_credits=2500)
        self.assertEqual(self.state, before)

    def test_submission_to_execution_alias_debits_only_once(self):
        self.open()
        self.reserve(quote=10, liability_id="submission:original-attempt")
        self.state["execution_history"] = [{"execution_id": "remote", "query_id": 1,
            "query_parameters": {}, "execution_cost_credits": 12, "execution_ended_at": NOW.isoformat(),
            "financial_attempt_id": "original-attempt"}]
        synchronize_financial_journal(self.state)
        report = evaluate_allowance(self.state, "fixture-only", usage(112), FRESH_NOW)
        self.assertEqual(report["committed_credits"], 12)
        self.assertEqual(report["remaining_credits"], 88)

    def test_current_guard_and_reserve_cannot_be_ignored_by_old_allocation(self):
        self.open()
        before = deepcopy(self.state)
        with self.assertRaises(BudgetInputError):
            self.reserve(guard_credits=130)
        with self.assertRaises(BudgetInputError):
            self.reserve(minimum_reserve_credits=None)
        self.assertEqual(self.state, before)

    def test_malformed_stored_allowance_numbers_fail_closed(self):
        self.open()
        self.state["financial_journal"]["allowances"]["fixture-only"]["authority"]["allowance_credits"] = float("nan")
        with self.assertRaises(BudgetInputError):
            evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)

    def submitted(self, operation="first", execution_id="remote", submitted_at=FRESH_NOW):
        self.state["pending_executions"] = {"source": {
            "key": "source", "query_id": 7, "query_parameters": {"lookback_hours": 2},
            "financial_attempt_id": operation, "execution_id": execution_id,
            "submitted_at_utc": submitted_at, "timeout_seconds": 90, "billing": {}}}
        synchronize_financial_journal(self.state)

    def test_own_prior_approved_reservation_carries_pending_cost_and_next_action(self):
        self.open()
        self.reserve("first", quote=10, liability_id="submission:first")
        self.submitted()
        report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
        self.assertEqual(report["status"], "affordable")
        self.assertEqual(report["committed_credits"], 10)
        self.assertEqual(report["remaining_credits"], 90)
        self.assertFalse(report["exposure_is_fully_known"])
        self.assertFalse(report["estimate_is_hard_cap"])
        self.assertTrue(report["execution_lifecycle_barriers_not_cleared"])
        self.assertEqual(report["unsettled_obligations"][0]["coverage"], "approved_operation_reservation")
        next_report = self.reserve("second", quote=7, liability_id="submission:second")
        self.assertEqual(next_report["status"], "affordable")
        self.assertEqual(next_report["committed_credits"], 17)
        self.assertEqual(next_report["remaining_credits"], 83)
        # The first operation is recoverable by ID, without a replacement POST.
        api = Mock(return_value={"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 4,
                                "execution_ended_at": FRESH_NOW, "result_metadata": {}})
        guard = Mock(side_effect=AssertionError("no new-spend admission for known-ID recovery"))
        tracker = ExecutionTracker(self.state, api, Mock(), guard, lambda: NOW, lambda _: None, "medium")
        result = tracker.execute({"key": "source", "query_id": 7, "max_run_credits": 20}, {}, True, 90)
        self.assertEqual(result["execution_id"], "remote")
        api.assert_called_once_with("execution/remote/status")
        guard.assert_not_called()
        self.assertEqual(evaluate_allowance(self.state, "fixture-only", usage(104), FRESH_NOW)["committed_credits"], 17)

    def test_real_tracker_subsecond_intent_does_not_predate_its_reservation(self):
        self.open()
        reserved = NOW + timedelta(microseconds=500000)
        reserve_allowance(self.state, "fixture-only", "precise-op", quote_credits=10,
                          approval={"operation_id": "precise-op", "quote_credits": 10,
                                    "approval_ref": "fixture: precise-operation approval",
                                    "estimate_evidence": "fixture: reviewed comparable costs"},
                          liability_id="submission:precise-op", usage=usage(100), now_utc=reserved,
                          guard_credits=1800, minimum_reserve_credits=15, monthly_target_credits=2500)
        submitted = reserved + timedelta(microseconds=100000)
        api = Mock(side_effect=[{"execution_id": "pending-precise"}, RuntimeError("status unavailable")])
        tracker = ExecutionTracker(self.state, api, Mock(), lambda: {}, lambda: submitted, lambda _: None, "medium")
        with self.assertRaises(ExecutionPending):
            tracker.execute({"key": "precise-source", "query_id": 7, "max_run_credits": 1,
                             "financial_attempt_id": "precise-op"}, {}, True, 90)
        self.assertEqual(self.state["pending_executions"]["precise-source"]["submitted_at_utc"],
                         submitted.isoformat().replace("+00:00", "Z"))
        report = evaluate_allowance(self.state, "fixture-only", usage(100), submitted)
        self.assertEqual(report["status"], "affordable")
        self.assertEqual(report["committed_credits"], 10)
        self.assertEqual(report["unsettled_obligations"][0]["coverage"], "approved_operation_reservation")

    def test_reserved_unknown_uses_larger_known_floor_not_free_or_a_hard_cap(self):
        self.open()
        self.reserve("first", quote=10, liability_id="submission:first")
        self.submitted()
        self.state["pending_executions"]["source"]["execution_cost_credits"] = 17
        synchronize_financial_journal(self.state)
        report = evaluate_allowance(self.state, "fixture-only", usage(117), FRESH_NOW)
        self.assertEqual(report["status"], "affordable")
        self.assertEqual(report["committed_credits"], 17)
        self.assertEqual(report["remaining_credits"], 83)
        unsettled = report["unsettled_obligations"][0]
        self.assertEqual(unsettled["known_cost_floor_credits"], 17)
        self.assertEqual(unsettled["modeled_risk_credits"], 17)
        self.assertFalse(unsettled["estimate_is_hard_cap"])

    def test_missing_or_mismatched_approval_and_estimate_do_not_cover_unknown(self):
        for alteration in ("approval", "operation_id", "quote_credits", "estimate_evidence", "reserved_at_utc"):
            with self.subTest(alteration=alteration):
                self.state = {"schema_version": 1, "queries": {}}
                self.open()
                self.reserve("first", liability_id="submission:first")
                self.submitted()
                operation = self.state["financial_journal"]["allowances"]["fixture-only"]["operations"]["first"]
                if alteration == "approval":
                    operation.pop("approval")
                elif alteration == "reserved_at_utc":
                    operation.pop("reserved_at_utc")
                else:
                    operation["approval"][alteration] = "wrong" if alteration == "operation_id" else "" if alteration == "estimate_evidence" else 9
                report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
                self.assertEqual(report["status"], "blocked")
                self.assertIn("unknown_exposure:exec:remote", report["blockers"])
                self.assertEqual(report["committed_credits"], 10, "bad approval must not refund its reservation")

    def test_cannot_backfill_reservation_after_uncovered_submission(self):
        self.open()
        self.submitted()
        before = deepcopy(self.state)
        with self.assertRaises(BudgetInputError):
            self.reserve("first", liability_id="submission:first")
        self.assertEqual(self.state, before)

    def test_another_allowance_cannot_supply_this_allowances_coverage(self):
        self.open()
        self.reserve("first", liability_id="submission:first")
        lane = self.state["financial_journal"]
        other = deepcopy(lane["allowances"]["fixture-only"])
        other["authority"]["allowance_id"] = "other-fixture"
        lane["allowances"]["other-fixture"] = other
        lane["allowances"]["fixture-only"]["operations"] = {}
        self.submitted()
        self.assertEqual(evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)["status"], "blocked")

    def test_reserved_risk_does_not_cover_legacy_unknown_after_telemetry_trim(self):
        self.open()
        self.reserve("first", liability_id="submission:first")
        self.submitted()
        self.state["execution_history"] = [{"execution_id": "legacy-unknown", "query_id": 99,
            "query_parameters": {}, "execution_cost_credits": None}] + [
            {"execution_id": f"zero-{i}", "query_id": 99, "query_parameters": {},
             "execution_cost_credits": 0} for i in range(1000)]
        synchronize_financial_journal(self.state)
        self.state["execution_history"] = self.state["execution_history"][-1000:]
        synchronize_financial_journal(self.state)
        report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("unknown_exposure:exec:legacy-unknown", report["blockers"])
        self.assertEqual(report["committed_credits"], 10)

    def test_one_reservation_cannot_cover_two_ambiguous_charge_identities(self):
        self.open()
        self.reserve("first", liability_id="submission:first")
        self.submitted()
        self.state["financial_journal"]["obligations"]["exec:another"] = {
            "liability_id": "exec:another", "kind": "execution", "execution_id": "another",
            "submission_id": "submission:first", "submitted_at_utc": FRESH_NOW, "cost_credits": None}
        report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(sum(item["coverage"] == "approved_operation_reservation"
                             for item in report["unsettled_obligations"]), 0)

    def test_old_or_undated_operation_cannot_be_relabelled_as_prior_reserved_risk(self):
        for timestamp in (None, "2026-11-03T23:58:59Z"):
            with self.subTest(timestamp=timestamp):
                self.state = {"schema_version": 1, "queries": {}}
                self.open()
                self.reserve("first", liability_id="submission:first")
                self.submitted(submitted_at=timestamp)
                report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
                self.assertEqual(report["status"], "blocked")

    def test_risk_coverage_does_not_clear_an_unknown_execution_id_barrier(self):
        self.open()
        self.reserve("first", liability_id="submission:first")
        self.submitted(execution_id=None)
        self.assertEqual(evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)["status"], "affordable")
        api = Mock()
        tracker = ExecutionTracker(self.state, api, Mock(), Mock(return_value={}), lambda: NOW, lambda _: None, "medium")
        with self.assertRaises(ExecutionPending):
            tracker.execute({"key": "another", "query_id": 8, "max_run_credits": 1,
                             "accept_completed_outlier": True}, {}, True, 90)
        api.assert_not_called()

    def test_approved_export_reservation_covers_only_its_exact_unsettled_read(self):
        self.open()
        self.reserve("export", quote=5, liability_id="read-A")
        record_export_attempt(self.state, "read-A", execution_id="already-computed",
                              requested_at_utc=FRESH_NOW, risk_credits=5,
                              estimate_evidence="fixture comparable wire observations")
        report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
        self.assertEqual(report["status"], "affordable")
        self.assertEqual(report["committed_credits"], 5)
        self.assertEqual(report["unsettled_obligations"][0]["coverage"], "approved_operation_reservation")
        record_export_attempt(self.state, "read-B", execution_id="already-computed",
                              requested_at_utc=FRESH_NOW, risk_credits=5,
                              estimate_evidence="same query, different read is not the same approval")
        report = evaluate_allowance(self.state, "fixture-only", usage(100), FRESH_NOW)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("unknown_exposure:read-B", report["blockers"])


if __name__ == "__main__":
    unittest.main()
