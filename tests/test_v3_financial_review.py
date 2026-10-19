"""DeepSeek financial counterexamples, adjudicated and owned by Sol for regression.

Real defects retained as regressions: H1 positional identity loss; H2 collapsed
legacy reads; H3 mutable attribution; H4b lost known floor; H5 optional target;
H7 missing-intent error typing. H1's impact case now publishes its migration
before truncation: the diagnostic financial_obligations API is read-only.

Rejected assumptions are explicitly reversed, not implemented unsafely:
H4a an inclusion boolean is NOT settlement proof; H7b a caller's assertion of
nonacceptance cannot zero a promoted provider execution or observed charge.
Actual billing adjustments would require a separate explicit reviewed record.

The original seven hypotheses and safe properties remain covered. All tests
must pass; this file is not intentionally red or excluded from CI. No Dune,
network, remote access, real authorization or financial data edits occur here.
"""
import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v3_credit_budget import BudgetInputError, outstanding_liabilities
from v3_financial_journal import (
    evaluate_allowance,
    export_attempt_id,
    financial_obligations,
    open_allowance,
    record_export_attempt,
    record_export_outcome,
    reject_submission,
    reserve_allowance,
    synchronize_financial_journal,
)
from v3_budget_runtime import build_scheduled_entries, collect_outstanding_liabilities

CONFIG = json.loads((ROOT / "config" / "v3_refresh.json").read_text())
PERIOD_START = "2026-10-04T16:35:49Z"
PERIOD_END = "2026-11-04T00:00:00Z"
NOW = "2026-10-16T22:00:00Z"
NEXT_START = "2026-11-04T00:00:00Z"
NEXT_END = "2026-12-04T00:00:00Z"
NEXT_NOW = "2026-11-10T00:00:00Z"
GUARD, RESERVE, TARGET, RATE = 1800.0, 15.0, 2500.0, 10.0


def usage(used=1500.0, *, included=2500.0, start=PERIOD_START, end=PERIOD_END):
    return {"credits_used": used, "credits_included": included,
            "period_start_utc": start, "period_end_utc": end}


def authority(allowance_id="allow-1", *, credits=100.0, external=0.0,
              start=PERIOD_START, end=PERIOD_END):
    return {"allowance_id": allowance_id, "authorization_ref": "ref-" + allowance_id,
            "authorized_by": "astra", "authorized_at_utc": NOW,
            "period_start_utc": start, "period_end_utc": end,
            "allowance_credits": credits, "external_risk_reserve_credits": external}


def history_row(query_id=42, *, cost=None, execution_id=None, ended="2026-10-10T00:01:00Z",
                **extra):
    row = {"query_id": query_id, "query_parameters": {}, "performance": "medium",
           "execution_started_at": "2026-10-10T00:00:00Z", "execution_ended_at": ended}
    if execution_id is not None:
        row["execution_id"] = execution_id
    if cost is not None:
        row["execution_cost_credits"] = cost
    row.update(extra)
    return row


def obligations(state):
    return state["financial_journal"]["obligations"]


def _raises(call):
    """Return the raised exception, so a finding can report its exact type."""
    try:
        call()
    except BaseException as error:  # noqa: BLE001 - the type IS the finding
        return error
    return None


class CounterexampleTests(unittest.TestCase):
    """Regression tests for accepted findings and corrected review assumptions."""

    def test_H1_unknown_exposure_is_replaced_by_an_unrelated_row_after_a_trim(self):
        """H1: an unknown exposure must not become a known cost via trim aliasing.

        observed: ``exec:history[0]`` cost None -> 5.0, no error, and the lane's
        unknown-exposure signal disappears, so a blocked account can look account-ed.
        expected: the unknown exposure stays unknown, or the identity is stable.
        """
        state = {"execution_history": [history_row(cost=None)]}
        synchronize_financial_journal(state)
        original = next(iter(obligations(state)))
        self.assertIsNone(obligations(state)[original]["cost_credits"])
        # the 1000-row trim drops that row; a DIFFERENT execution of the same query
        # (same identity fields, so _put cannot object) now sits at index 0
        state["execution_history"] = [history_row(cost=5.0)]
        synchronize_financial_journal(state)
        self.assertIsNone(
            obligations(state)[original]["cost_credits"],
            msg=("H1 DEFECT: a different execution aliased the positional identity and "
                 "overwrote the unknown exposure with a known cost, unblocking admission "
                 f"(now {obligations(state)[original]['cost_credits']!r})"))

    def test_H1_the_unknown_signal_is_lost_after_the_alias(self):
        """H1 (impact): the unknown-liability signal that blocks admission disappears."""
        state = {"execution_history": [history_row(cost=None)]}
        synchronize_financial_journal(state)  # The tracker checkpoints this BEFORE trimming.
        original = next(iter(obligations(state)))
        before, _ = financial_obligations(state)
        self.assertIn(original,
                      [item["liability_id"] for item in before if item.get("cost_credits") is None])
        state["execution_history"] = [history_row(cost=5.0)]
        items, diagnostics = collect_outstanding_liabilities(state, CONFIG)
        self.assertIn(original, diagnostics["unknown_liability_ids"],
                      msg=("H1 DEFECT: after a trim the previously unknown liability is "
                           f"reported as known {[ (i['liability_id'], i.get('cost_credits')) for i in items]}"))

    def test_H2_two_legacy_export_reads_of_one_execution_collapse_into_one_charge(self):
        """H2: a second paid read of one execution is a second charge, not a duplicate.

        observed: 2MB + 3MB at 10 credits/MB -> one obligation of 30.0 (MAX), 50.0 lost.
        """
        state = {"execution_history": [
            history_row(execution_id="same", export_wire_bytes=2_000_000,
                        export_requested_at_utc="2026-10-10T01:00:00Z"),
            history_row(execution_id="same", export_wire_bytes=3_000_000,
                        export_requested_at_utc="2026-10-10T02:00:00Z")]}
        synchronize_financial_journal(state, export_rate=RATE)
        exports = {key: value.get("cost_credits") for key, value in obligations(state).items()
                   if value.get("kind") == "export"}
        self.assertEqual(len(exports), 2,
                         msg=("H2 DEFECT: two metered legacy reads of one execution "
                              f"collapsed into {exports} (expected two charges)"))
        self.assertEqual(sum(value or 0 for value in exports.values()), 50.0,
                         msg=f"H2 DEFECT: total charge is {exports}, expected 50.0 (2MB+3MB at {RATE}/MB)")

    def test_H3_incurred_at_utc_can_move_a_charge_across_the_period_boundary(self):
        """H3: a charge's period attribution must not change silently on a re-sync.

        observed: 2026-10-01 -> 2026-11-10 with no error and no immutability guard
        (``requested_at_utc`` IS guarded, ``incurred_at_utc`` is not).
        """
        state = {"execution_history": [history_row(execution_id="e", cost=4.0,
                                                   ended="2026-10-01T00:00:00Z")]}
        synchronize_financial_journal(state)
        before = obligations(state)["exec:e"]["incurred_at_utc"]
        state["execution_history"] = [history_row(execution_id="e", cost=4.0,
                                                  ended="2026-11-10T00:00:00Z")]
        before_rejected_sync = copy.deepcopy(state)
        with self.assertRaisesRegex(BudgetInputError, "incurred_at_utc changed"):
            synchronize_financial_journal(state)
        after = obligations(state)["exec:e"]["incurred_at_utc"]
        self.assertEqual(state, before_rejected_sync)
        self.assertEqual(before, after,
                         msg=("H3 DEFECT: the obligation's period attribution moved from "
                              f"{before} to {after} with no error; it silently entered the "
                              "next period's obligations"))

    def test_H4_unproven_inclusion_boolean_cannot_settle_debt(self):
        """H4a rejected: this test supplies no provider inclusion evidence."""
        folded = outstanding_liabilities(
            [{"liability_id": "exec:a", "kind": "execution", "cost_credits": 12.0,
              "included_in_confirmed_usage": True}])
        self.assertEqual(folded["total"], 12.0)
        self.assertEqual(folded["items"][0]["cost_credits"], 12.0)
        self.assertTrue(any("inclusion flag ignored" in reason for reason in folded["reasons"]))

    def test_H4_an_unknown_entry_erases_a_known_cost_instead_of_dominating_it(self):
        """H4b: unknown evidence must dominate a known number, never delete it.

        observed: {exec:b: 7.5, exec:b: unknown} -> total 0.0, items [] (same reversed).
        """
        folded = outstanding_liabilities(
            [{"liability_id": "exec:b", "kind": "execution", "cost_credits": 7.5},
             {"liability_id": "exec:b", "kind": "execution"}])
        self.assertEqual(folded["total"], 7.5,
                         msg=("H4 DEFECT: the known cost was erased from the reported total "
                              f"(observed {folded['total']}, expected the known 7.5 retained "
                              f"alongside the unknown marker; items={folded['items']})"))
        self.assertEqual([item["liability_id"] for item in folded["items"]], ["exec:b"],
                         msg=("H4 DEFECT: the known liability vanished from items, so any "
                               "consumer of items/total understates exposure"))
        self.assertEqual(folded["known_floor_credits"], 7.5)
        self.assertFalse(folded["exposure_is_fully_known"])
        self.assertTrue(folded["items"][0]["cost_is_lower_bound"])
        self.assertEqual(folded["unknown"][0]["known_cost_floor_credits"], 7.5)

    def test_H5_a_missing_cost_target_silently_widens_the_allowance_ceiling(self):
        """H5: a missing monthly target must fail closed, not remove the ceiling.

        observed: with ``monthly_target_credits=None`` the allowance opens at limit
        1800 and reports affordable; the same input with the real target 500 is refused
        ("exceeds conservative opening headroom").
        """
        omitted, reported = {}, {}
        with self.assertRaises(BudgetInputError):
            open_allowance(omitted, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                           minimum_reserve_credits=RESERVE, monthly_target_credits=None)
        with self.assertRaises(BudgetInputError):
            open_allowance(reported, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                           minimum_reserve_credits=RESERVE, monthly_target_credits=500.0)
        self.assertEqual(omitted, {})
        self.assertEqual(reported, {})

    def test_H7_reject_submission_on_an_absent_intent_raises_keyerror(self):
        """H7 (robustness): an unhandled error type escapes the module's contract.

        observed: ``KeyError: 'submission:A2'`` instead of ``BudgetInputError``, so a
        caller that guards the documented error type cannot contain the failure.
        """
        state = {}
        observed = _raises(lambda: reject_submission(
            state, {"key": "gone", "financial_attempt_id": "A2"}))
        self.assertIsInstance(
            observed, BudgetInputError,
            msg=(f"H7 DEFECT: rejecting an absent intent raised "
                 f"{type(observed).__name__}: {observed} instead of BudgetInputError, so "
                 "an unhandled error type escapes the module's error contract"))

    def test_H7_caller_claim_cannot_zero_a_promoted_execution(self):
        """H7b rejected: no authoritative adjustment is supplied by this fixture."""
        state = {"pending_executions": {"k": {"financial_attempt_id": "A3",
                                              "execution_id": "e1", "query_id": 42,
                                              "query_parameters": {}}}}
        synchronize_financial_journal(state)
        self.assertNotIn("submission:A3", obligations(state),
                         msg="the intent key is retired once the execution id is known")
        self.assertIsNone(obligations(state).get("exec:e1", {}).get("cost_credits"))
        before = copy.deepcopy(state)
        observed = _raises(lambda: reject_submission(
            state, {"key": "k", "financial_attempt_id": "A3"}))
        self.assertIsInstance(
            observed, BudgetInputError,
            msg=("A nonacceptance claim contradicting a provider ID must fail with "
                 f"the documented review-required error, not {type(observed).__name__}: {observed}"))
        self.assertEqual(state, before)
        self.assertIsNone(obligations(state)["exec:e1"]["cost_credits"])


class VerifiedSafeTests(unittest.TestCase):
    """GREEN: the guarantees that hold today. A fix must not regress these."""

    # --- H1: durability of the record itself -------------------------------------
    def test_H1_the_trimmed_rows_obligation_is_retained(self):
        state = {"execution_history": [history_row(cost=None)] + [history_row(cost=1.0)
                                                                  for _ in range(1000)]}
        synchronize_financial_journal(state)
        self.assertEqual(len(obligations(state)), 1001)
        state["execution_history"] = state["execution_history"][-1000:]
        synchronize_financial_journal(state)
        self.assertEqual(len(obligations(state)), 1001,
                         msg="the trimmed execution's obligation must survive the trim")

    def test_H1_an_unknown_undated_exposure_is_never_aged_out(self):
        state = {"execution_history": [history_row(cost=None, ended=None)]}
        synchronize_financial_journal(state)
        items, excluded = financial_obligations(state, period_start_utc=PERIOD_START)
        self.assertEqual(excluded, [])
        self.assertEqual([item["liability_id"] for item in items], list(obligations(state)))
        self.assertIsNone(items[0].get("cost_credits"))

    # --- H2: the modern attempt-at-a-time path -----------------------------------
    def test_H2_a_repeated_attempt_id_is_refused_not_recharged(self):
        state = {}
        record_export_attempt(state, "read-1", execution_id="e", requested_at_utc=NOW,
                              risk_credits=5.0, estimate_evidence="ev")
        with self.assertRaises(BudgetInputError):
            record_export_attempt(state, "read-1", execution_id="e", requested_at_utc=NOW,
                                  risk_credits=5.0, estimate_evidence="ev")

    def test_H2_two_reads_of_one_execution_are_two_obligations(self):
        state = {}
        record_export_attempt(state, "read-1", execution_id="e", requested_at_utc=NOW,
                              risk_credits=5.0, estimate_evidence="ev")
        record_export_attempt(state, "read-2", execution_id="e",
                              requested_at_utc="2026-10-16T22:05:00Z",
                              risk_credits=5.0, estimate_evidence="ev")
        self.assertEqual({key for key in obligations(state) if key.startswith("read-")},
                         {"read-1", "read-2"})

    def test_H2_attempt_ids_differ_per_requested_time(self):
        self.assertNotEqual(export_attempt_id("k", "2026-10-16T22:00:00Z"),
                            export_attempt_id("k", "2026-10-16T22:00:01Z"))

    def test_H2_a_staged_read_is_unresolved_until_its_outcome_is_metered(self):
        state = {}
        record_export_attempt(state, "read-1", execution_id="e", requested_at_utc=NOW,
                              risk_credits=5.0, estimate_evidence="ev")
        self.assertIsNone(obligations(state)["read-1"]["cost_credits"])
        record_export_outcome(state, "read-1", observed_credits=1.25)
        self.assertEqual(obligations(state)["read-1"]["cost_credits"], 1.25)
        self.assertFalse(obligations(state)["read-1"]["unresolved"])
        with self.assertRaises(BudgetInputError):
            record_export_outcome(state, "nope", observed_credits=1.0)

    # --- H3: attribution rules that do hold --------------------------------------
    def test_H3_a_prior_period_known_charge_is_excluded_from_the_live_period(self):
        state = {"execution_history": [history_row(execution_id="old", cost=9.0,
                                                   ended="2026-10-01T00:00:00Z")]}
        synchronize_financial_journal(state)
        items, excluded = financial_obligations(state, period_start_utc=PERIOD_START)
        self.assertEqual(excluded, ["exec:old"])
        self.assertEqual(items, [])

    def test_H3_an_unknown_charge_is_never_excluded_by_its_date(self):
        state = {"execution_history": [history_row(execution_id="old", cost=None,
                                                   ended="2026-10-01T00:00:00Z")]}
        synchronize_financial_journal(state)
        items, excluded = financial_obligations(state, period_start_utc=PERIOD_START)
        self.assertEqual(excluded, [])
        self.assertEqual([item["liability_id"] for item in items], ["exec:old"])

    def test_H3_a_malformed_obligation_timestamp_is_never_aged_out(self):
        state = {"execution_history": [history_row(execution_id="bad", cost=3.0,
                                                   ended="not-a-time")]}
        synchronize_financial_journal(state)
        items, excluded = financial_obligations(state, period_start_utc=PERIOD_START)
        self.assertEqual(excluded, [])
        self.assertEqual([item["liability_id"] for item in items], ["exec:bad"])

    # --- H4: the safety net that does exist --------------------------------------
    def test_H4_an_unknown_contribution_still_blocks_admission(self):
        folded = outstanding_liabilities(
            [{"liability_id": "exec:b", "kind": "execution", "cost_credits": 7.5},
             {"liability_id": "exec:b", "kind": "execution"}])
        self.assertEqual([item["liability_id"] for item in folded["unknown"]], ["exec:b"])

    def test_H4_caps_are_never_used_as_costs(self):
        folded = outstanding_liabilities(
            [{"liability_id": "exec:c", "kind": "execution", "max_run_credits": 99.0}])
        self.assertEqual(folded["total"], 0.0)
        self.assertEqual([item["liability_id"] for item in folded["unknown"]], ["exec:c"])

    def test_H4_duplicate_known_costs_resolve_to_the_maximum(self):
        folded = outstanding_liabilities(
            [{"liability_id": "exec:d", "kind": "execution", "cost_credits": 3.0},
             {"liability_id": "exec:d", "kind": "execution", "cost_credits": 8.0}])
        self.assertEqual(folded["total"], 8.0)

    # --- H6: no replenishment, and regular spend is not forgotten -----------------
    def test_H6_a_second_allowance_in_the_same_period_is_refused(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        with self.assertRaises(BudgetInputError):
            open_allowance(state, authority("allow-2"), usage(1500.0), NOW,
                           guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                           monthly_target_credits=TARGET)

    def test_H6_a_new_period_starts_a_fresh_empty_allowance(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        reserve_allowance(state, "allow-1", "op-1", quote_credits=10.0,
                          approval={"operation_id": "op-1", "quote_credits": 10.0,
                                    "approval_ref": "r", "estimate_evidence": "e"},
                          liability_id="exec:x", usage=usage(1500.0), now_utc=NOW,
                          guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                          monthly_target_credits=TARGET)
        self.assertEqual(list(obligations(state)) or [], [])
        next_state = copy.deepcopy(state)
        record = open_allowance(next_state, authority("allow-2", start=NEXT_START, end=NEXT_END),
                                usage(1200.0, start=NEXT_START, end=NEXT_END), NEXT_NOW,
                                guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                                monthly_target_credits=TARGET)
        self.assertEqual(record["operations"], {},
                         msg="a new period must not inherit the prior period's reservations")
        self.assertEqual(record["opening_usage_credits"], 1200.0)

    def test_H6_regular_spend_growth_is_not_forgotten(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        grew = evaluate_allowance(state, "allow-1", usage(1600.0), NOW, guard_credits=GUARD,
                                  minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        self.assertEqual(grew["status"], "blocked")
        self.assertIn("account_usage_drift", grew["blockers"])
        stopped = evaluate_allowance(state, "allow-1", usage(1790.0), NOW, guard_credits=GUARD,
                                     minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        self.assertIn("account_usage_stop", stopped["blockers"])
        self.assertIn("account_usage_drift", stopped["blockers"])

    def test_H6_a_reservation_is_refused_once_regular_spend_consumed_headroom(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        with self.assertRaises(BudgetInputError):
            reserve_allowance(state, "allow-1", "op-1", quote_credits=50.0,
                              approval={"operation_id": "op-1", "quote_credits": 50.0,
                                        "approval_ref": "r", "estimate_evidence": "e"},
                              liability_id="exec:x", usage=usage(1790.0), now_utc=NOW,
                              guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                              monthly_target_credits=TARGET)

    def test_H6_an_allowance_never_authorizes_paid_work_by_itself(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        report = evaluate_allowance(state, "allow-1", usage(1500.0), NOW, guard_credits=GUARD,
                                    minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        self.assertTrue(report["authorization_required"])
        self.assertFalse(report["estimate_is_hard_cap"])

    def test_H6_approval_must_bind_the_operation_and_the_exact_quote(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        for approval in ({"operation_id": "other", "quote_credits": 10.0, "approval_ref": "r",
                          "estimate_evidence": "e"},
                         {"operation_id": "op-1", "quote_credits": 11.0, "approval_ref": "r",
                          "estimate_evidence": "e"},
                         {"operation_id": "op-1", "quote_credits": 10.0,
                          "estimate_evidence": "e"}):
            with self.assertRaises(BudgetInputError):
                reserve_allowance(state, "allow-1", "op-1", quote_credits=10.0,
                                  approval=approval, liability_id="exec:x", usage=usage(1500.0),
                                  now_utc=NOW, guard_credits=GUARD,
                                  minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)

    def test_H6_a_reserved_operation_replays_idempotently_and_is_immutable(self):
        state = {}
        open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        approval = {"operation_id": "op-1", "quote_credits": 10.0, "approval_ref": "r",
                    "estimate_evidence": "e"}
        first = reserve_allowance(state, "allow-1", "op-1", quote_credits=10.0,
                                  approval=approval, liability_id="exec:x", usage=usage(1500.0),
                                  now_utc=NOW, guard_credits=GUARD,
                                  minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        replay = reserve_allowance(state, "allow-1", "op-1", quote_credits=10.0,
                                   approval=approval, liability_id="exec:x", usage=usage(1500.0),
                                   now_utc=NOW, guard_credits=GUARD,
                                   minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)
        self.assertEqual(first["committed_credits"], replay["committed_credits"])
        with self.assertRaises(BudgetInputError):
            reserve_allowance(state, "allow-1", "op-1", quote_credits=20.0,
                              approval={**approval, "quote_credits": 20.0},
                              liability_id="exec:x", usage=usage(1500.0), now_utc=NOW,
                              guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                              monthly_target_credits=TARGET)
        with self.assertRaises(BudgetInputError):
            reserve_allowance(state, "allow-1", "op-2", quote_credits=1.0,
                              approval={"operation_id": "op-2", "quote_credits": 1.0,
                                        "approval_ref": "r", "estimate_evidence": "e"},
                              liability_id="exec:x", usage=usage(1500.0), now_utc=NOW,
                              guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                              monthly_target_credits=TARGET)

    def test_H6_an_unknown_opening_exposure_blocks_allocation(self):
        state = {"execution_history": [history_row(cost=None)]}
        with self.assertRaises(BudgetInputError):
            open_allowance(state, authority(), usage(1500.0), NOW, guard_credits=GUARD,
                           minimum_reserve_credits=RESERVE, monthly_target_credits=TARGET)

    def test_H5_an_omitted_export_policy_defaults_to_estimation(self):
        """H5 (observation, NOT a defect): the omitted-policy default is the safe direction.

        A source entry without ``export_policy`` becomes ``observed_max`` (estimate the
        export cost from observed history) while presentation becomes ``none``. Pinned so
        a future change to a zero-cost default is caught; note it is an implicit choice -
        the entry is treated as export-capable without the operator saying so.
        """
        entries, _ = build_scheduled_entries({
            "monthly_spend_guard_credits": GUARD, "minimum_credit_reserve": RESERVE,
            "export_credits_per_megabyte": RATE, "monthly_target_credits": TARGET,
            "sources": [{"key": "x", "query_id": 1, "cadence_minutes": 60}],
            "presentation": [{"key": "p", "query_id": 2, "cadence_minutes": 60}]})
        by_key = {entry["key"]: entry["export_policy"] for entry in entries}
        self.assertEqual(by_key, {"source:x": "observed_max", "presentation:p": "none"},
                         msg=("a missing source export_policy must not default to a "
                              f"zero-cost policy: {by_key}"))

    # --- H7: the rejection path that does work -----------------------------------
    def test_H7_proven_rejection_zeroes_the_intent_and_survives_removal_and_resync(self):
        state = {"pending_executions": {
            "k": {"financial_attempt_id": "A1", "query_id": 42, "query_parameters": {}}}}
        synchronize_financial_journal(state)
        self.assertIsNone(obligations(state)["submission:A1"]["cost_credits"])
        reject_submission(state, {"key": "k", "financial_attempt_id": "A1"})
        self.assertEqual(obligations(state)["submission:A1"]["cost_credits"], 0.0)
        del state["pending_executions"]["k"]
        synchronize_financial_journal(state)
        self.assertEqual(obligations(state)["submission:A1"]["cost_credits"], 0.0,
                         msg="a zeroed intent must not be resurrected by a later sync")
        self.assertEqual(obligations(state)["submission:A1"]["basis"], "proven_nonacceptance")

    def test_H7_rejection_does_not_touch_a_known_sibling_obligation(self):
        state = {"execution_history": [history_row(execution_id="other", cost=6.0)],
                 "pending_executions": {"k": {"financial_attempt_id": "A1", "query_id": 42,
                                              "query_parameters": {}}}}
        synchronize_financial_journal(state)
        reject_submission(state, {"key": "k", "financial_attempt_id": "A1"})
        self.assertEqual(obligations(state)["exec:other"]["cost_credits"], 6.0)

    def test_H7_the_tracker_rejects_only_inside_the_confirmed_nonacceptance_branch(self):
        lines = (ROOT / "scripts" / "v3_execution_tracker.py").read_text().splitlines()
        calls = [i for i, line in enumerate(lines) if "reject_submission(self.state" in line]
        self.assertEqual(len(calls), 1,
                         msg=f"expected exactly one rejection call site, found {calls}")
        handlers = [i for i, line in enumerate(lines) if line.strip().startswith("except ")]
        enclosing = max((i for i in handlers if i < calls[0]), default=None)
        self.assertIsNotNone(enclosing, msg="the rejection call is outside any handler")
        self.assertIn("SubmissionRejected", lines[enclosing],
                      msg=("the durable intent must only be zeroed on a confirmed "
                           f"nonacceptance, but the enclosing handler is "
                           f"{lines[enclosing].strip()!r}"))


class InterfaceContractTests(unittest.TestCase):
    """GREEN: smaller contracts worth protecting during the fix."""

    def test_a_malformed_lane_is_refused_not_reinterpreted(self):
        for bad in ({"financial_journal": {"schema_version": 2, "obligations": {},
                                           "allowances": {}}},
                    {"financial_journal": {"schema_version": 1, "obligations": []}},
                    {"financial_journal": {"schema_version": 1, "obligations": {},
                                           "allowances": {"a": {"authority": {}}}}}):
            with self.assertRaises(BudgetInputError):
                synchronize_financial_journal(bad)

    def test_obligation_identity_fields_are_immutable(self):
        state = {"execution_history": [history_row(execution_id="e", cost=1.0)]}
        synchronize_financial_journal(state)
        state["execution_history"] = [history_row(execution_id="e", cost=1.0, query_id=99)]
        with self.assertRaises(BudgetInputError):
            synchronize_financial_journal(state)

    def test_a_known_cost_never_shrinks_on_a_later_observation(self):
        state = {"execution_history": [history_row(execution_id="e", cost=9.0)]}
        synchronize_financial_journal(state)
        state["execution_history"] = [history_row(execution_id="e", cost=2.0)]
        synchronize_financial_journal(state)
        self.assertEqual(obligations(state)["exec:e"]["cost_credits"], 9.0)

    def test_the_lane_is_published_by_the_tracker_before_discarding_telemetry(self):
        source = (ROOT / "scripts" / "v3_execution_tracker.py").read_text()
        trim = source.index('self.state["execution_history"] = history[-1000:]')
        sync_at = source.rindex("synchronize_financial_journal(self.state)", 0, trim)
        self.assertGreater(sync_at, 0,
                           msg="the financial lane must be copied before the 1000-row trim")

    def test_an_inexact_renewal_end_is_reported_not_inferred(self):
        from v3_credit_budget import normalize_usage
        selection = normalize_usage({"credits_used": 10.0, "credits_included": 100.0,
                                     "period_start_utc": PERIOD_START,
                                     "period_end_utc": "2026-11-04"}, NOW)
        self.assertIn("period_end_not_exact_utc", selection["blockers"])
        self.assertIsNone(selection["period"]["period_end_utc"])


class MigrationRegressionTests(unittest.TestCase):
    """Replay, restart, mutation and lower-bound cases adjacent to the review."""

    def test_preexisting_positional_unknown_is_not_reassigned_or_erased(self):
        state = {"financial_journal": {"schema_version": 1, "allowances": {}, "obligations": {
            "exec:history[0]": {"liability_id": "exec:history[0]", "kind": "execution",
                                "query_id": 42, "query_parameters": {}, "cost_credits": None}}},
                 "execution_history": [history_row(cost=5)]}
        synchronize_financial_journal(state)
        self.assertIsNone(obligations(state)["exec:history[0]"]["cost_credits"])
        items, diagnostics = collect_outstanding_liabilities(state, CONFIG)
        self.assertIn("exec:history[0]", diagnostics["unknown_liability_ids"])
        self.assertEqual(diagnostics["known_floor_credits"], 5)
        self.assertEqual(outstanding_liabilities(items)["known_floor_credits"], 5)

    def test_anonymous_identity_survives_json_restart_and_new_identical_row(self):
        state = {"execution_history": [history_row(cost=None), history_row(cost=1)]}
        synchronize_financial_journal(state)
        unknown_id = next(key for key, row in obligations(state).items() if row.get("cost_credits") is None)
        original_ids = set(obligations(state))
        state = json.loads(json.dumps(state))
        state["execution_history"] = state["execution_history"][-1:] + [history_row(cost=1)]
        synchronize_financial_journal(state)
        self.assertEqual(len(obligations(state)), 3)
        self.assertTrue(original_ids < set(obligations(state)))
        self.assertIsNone(obligations(state)[unknown_id]["cost_credits"])
        self.assertFalse(synchronize_financial_journal(state), "replaying migration must not mint more IDs")

    def test_legacy_read_tokens_survive_trim_and_separate_identical_new_reads(self):
        row = history_row(execution_id="same", cost=1, export_wire_bytes=2_000_000,
                          export_requested_at_utc=NOW)
        state = {"execution_history": [copy.deepcopy(row), copy.deepcopy(row)]}
        synchronize_financial_journal(state)
        old_ids = {r["export_attempt_id"] for r in state["execution_history"]}
        self.assertEqual(len(old_ids), 2)
        state = json.loads(json.dumps(state))
        state["execution_history"] = state["execution_history"][-1:] + [copy.deepcopy(row)]
        synchronize_financial_journal(state)
        exports = [r for r in obligations(state).values() if r["kind"] == "export"]
        self.assertEqual(len(exports), 3)
        self.assertEqual(sum(r["cost_credits"] for r in exports), 60)
        self.assertFalse(synchronize_financial_journal(state))

    def test_read_only_snapshot_does_not_mutate_or_create_durable_identity(self):
        state = {"execution_history": [history_row(cost=None), history_row(cost=1)]}
        before = copy.deepcopy(state)
        first, _ = financial_obligations(state)
        second, _ = financial_obligations(state)
        self.assertEqual(state, before)
        self.assertEqual(first, second)

    def test_equivalent_utc_instants_do_not_rewrite_original_attribution(self):
        state = {"execution_history": [history_row(execution_id="e", cost=2,
                                                   ended="2026-10-10T00:01:00Z")]}
        synchronize_financial_journal(state)
        state["execution_history"][0]["execution_ended_at"] = "2026-10-10T01:01:00+01:00"
        synchronize_financial_journal(state)
        self.assertEqual(obligations(state)["exec:e"]["incurred_at_utc"], "2026-10-10T00:01:00Z")

    def test_malformed_time_cannot_be_silently_replaced_to_exclude_a_charge(self):
        state = {"execution_history": [history_row(execution_id="e", cost=2, ended="unknown-time")]}
        synchronize_financial_journal(state)
        state["execution_history"][0]["execution_ended_at"] = "2026-09-01T00:00:00Z"
        before = copy.deepcopy(state)
        with self.assertRaises(BudgetInputError):
            synchronize_financial_journal(state)
        self.assertEqual(state, before)

    def test_unknown_preserves_known_floor_in_both_orders_and_after_refolding(self):
        for contributions in ([{"liability_id": "e", "cost_credits": 7.5}, {"liability_id": "e"}],
                              [{"liability_id": "e"}, {"liability_id": "e", "cost_credits": 7.5}]):
            result = outstanding_liabilities(contributions)
            self.assertEqual(result["known_floor_credits"], 7.5)
            self.assertEqual(result["total"], 7.5)
            self.assertFalse(result["exposure_is_fully_known"])
            again = outstanding_liabilities(result["items"])
            self.assertEqual(again["known_floor_credits"], 7.5)
            self.assertFalse(again["exposure_is_fully_known"])

    def test_unresolved_metered_attempt_keeps_floor_but_is_not_an_admission_bound(self):
        state = {}
        record_export_attempt(state, "read", execution_id="e", requested_at_utc=NOW,
                              risk_credits=9, estimate_evidence="fixture-only")
        record_export_outcome(state, "read", observed_credits=2)
        obligations(state)["read"]["unresolved"] = True
        items, _ = financial_obligations(state)
        self.assertIsNone(items[0].get("cost_credits"))
        self.assertEqual(items[0]["known_cost_floor_credits"], 2)
        folded = outstanding_liabilities(items)
        self.assertEqual(folded["known_floor_credits"], 2)
        self.assertFalse(folded["exposure_is_fully_known"])

    def test_canceled_cost_two_and_old_unknown_both_survive_without_fake_settlement(self):
        state = {"execution_history": [history_row(execution_id="canceled", cost=2, state="QUERY_STATE_CANCELED"),
                                       history_row(execution_id="cost-unknown", state="QUERY_STATE_CANCELED")]}
        synchronize_financial_journal(state)
        state["execution_history"] = []
        before = copy.deepcopy(state)
        for execution in ("canceled", "cost-unknown"):
            with self.assertRaises(BudgetInputError):
                reject_submission(state, {"key": "gone", "financial_attempt_id": execution,
                                          "execution_id": execution, "included_in_confirmed_usage": True})
        self.assertEqual(state, before)
        self.assertEqual(obligations(state)["exec:canceled"]["cost_credits"], 2)
        self.assertIsNone(obligations(state)["exec:cost-unknown"]["cost_credits"])
        items, diagnostics = collect_outstanding_liabilities(state, CONFIG)
        self.assertIn("exec:cost-unknown", diagnostics["unknown_liability_ids"])
        self.assertEqual(outstanding_liabilities(items)["known_floor_credits"], 2)

    def test_unknown_submission_floor_two_is_not_zeroed_by_caller_claim(self):
        state = {"pending_executions": {"k": {"financial_attempt_id": "A", "query_id": 42,
                 "query_parameters": {}, "execution_cost_credits": 2}}}
        synchronize_financial_journal(state)
        state["pending_executions"]["k"]["execution_cost_credits"] = 1
        synchronize_financial_journal(state)
        self.assertEqual(obligations(state)["submission:A"]["known_cost_floor_credits"], 2)
        before = copy.deepcopy(state)
        with self.assertRaises(BudgetInputError):
            reject_submission(state, {"key": "k", "financial_attempt_id": "A"})
        self.assertEqual(state, before)
        items, _ = financial_obligations(state)
        self.assertFalse(outstanding_liabilities(items)["exposure_is_fully_known"])
        self.assertEqual(outstanding_liabilities(items)["known_floor_credits"], 2)

    def test_promotion_preserves_floor_until_execution_bound_terminal_cost(self):
        pending = {"financial_attempt_id": "A", "query_id": 42,
                   "query_parameters": {}, "execution_cost_credits": 2}
        state = {"pending_executions": {"k": pending}}
        synchronize_financial_journal(state)
        pending["execution_id"] = "remote"
        synchronize_financial_journal(state)
        items, _ = financial_obligations(state)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["known_cost_floor_credits"], 2)
        self.assertIsNone(items[0].get("cost_credits"))
        pending["terminal_status"] = {"state": "QUERY_STATE_CANCELED", "execution_cost_credits": 2}
        synchronize_financial_journal(state)
        items, _ = financial_obligations(state)
        self.assertEqual(items[0]["cost_credits"], 2)
        self.assertTrue(outstanding_liabilities(items)["exposure_is_fully_known"])
        self.assertEqual(outstanding_liabilities(items)["total"], 2, "Known price is not billing settlement")

    def test_target_is_required_for_open_evaluate_and_reserve_without_mutation(self):
        state = {}
        with self.assertRaises(TypeError):
            open_allowance(state, authority(), usage(100), NOW, guard_credits=GUARD,
                           minimum_reserve_credits=RESERVE)
        self.assertEqual(state, {})
        open_allowance(state, authority(), usage(100), NOW, guard_credits=GUARD,
                       minimum_reserve_credits=RESERVE, monthly_target_credits=500)
        before = copy.deepcopy(state)
        with self.assertRaises(BudgetInputError):
            evaluate_allowance(state, "allow-1", usage(100), NOW, guard_credits=GUARD,
                               minimum_reserve_credits=RESERVE, monthly_target_credits=None)
        with self.assertRaises(BudgetInputError):
            reserve_allowance(state, "allow-1", "op", quote_credits=1,
                              approval={"operation_id": "op", "quote_credits": 1,
                                        "approval_ref": "test-only", "estimate_evidence": "test-only"},
                              liability_id="exec:op", usage=usage(100), now_utc=NOW,
                              guard_credits=GUARD, minimum_reserve_credits=RESERVE,
                              monthly_target_credits=None)
        self.assertEqual(state, before)


if __name__ == "__main__":
    unittest.main()
