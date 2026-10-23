from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_credit_budget import BudgetInputError
from v3_standing_budget import LANE, evaluate, open_period, reserve, scope

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
NOW2 = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
NOW3 = datetime(2026, 12, 5, 12, tzinfo=timezone.utc)
USAGE = {"credits_used":1803.249, "credits_included":4000,
         "period_start_utc":"2026-10-04T16:35:49Z", "period_end_utc":"2026-11-04T00:00:00Z"}
USAGE2 = {**USAGE, "credits_used":0, "period_start_utc":"2026-11-04T00:00:00Z",
          "period_end_utc":"2026-12-04T00:00:00Z"}
USAGE3 = {**USAGE, "credits_used":0, "period_start_utc":"2026-12-04T00:00:00Z",
          "period_end_utc":"2027-01-04T00:00:00Z"}
BASE = 1803.249 + 254.814 + 100.0


def config():
    return {"monthly_spend_guard_credits":3000, "monthly_target_credits":2500,
        "minimum_credit_reserve":100, "performance":"medium", "autonomous_operation":{
        "schema_version":1, "enabled":True, "approval_ref":"user-standing-20261020",
        "authorized_by":"user", "authorization_text":"Run continuously with sensible credits left at month end",
        "authorized_at_utc":"2026-10-20T10:00:00Z", "legacy_unknown_risk_credits":100,
        "period_hard_limit_credits":3000,"operating_target_credits":2500,
        "monthly_safety_reserve_credits":100, "minimum_unspent_quota_credits":1000,
        "daily_risk_limit_credits":100, "forecast_margin_credits":50,
        "queries":{"1":{"kinds":["execution","export"], "performance":"medium", "parameter_family":"hours",
                            "min_lookback_hours":1,"max_lookback_hours":168,"max_operation_risk_credits":30}}}}


def state():
    return {"financial_journal":{"schema_version":1, "obligations":{
        "exec:old":{"liability_id":"exec:old", "kind":"execution", "execution_id":"old",
                    "cost_credits":254.814,"incurred_at_utc":"2026-10-15T00:00:00Z"},
        "old-export":{"liability_id":"old-export", "kind":"export", "cost_credits":None, "unresolved":True}},
        "allowances":{}}}


class StandingBudgetTests(unittest.TestCase):
    def test_activated_book_loss_cannot_silently_create_a_new_spending_anchor(self):
        self.state["standing_authority_activation"]={"approved":{"predecessor_revision":"known"}}
        before=deepcopy(self.state)
        with self.assertRaisesRegex(BudgetInputError,"refusing a new risk anchor"):
            open_period(self.state,self.config,USAGE,NOW)
        self.assertEqual(self.state,before)

    def test_new_read_may_reference_old_unknown_attempt_without_settling_or_aliasing_it(self):
        self.state["financial_journal"]["obligations"]["old-export"]["execution_id"]="same-exec"
        before=deepcopy(self.state["financial_journal"])
        self.reserve(kind="export",operation_id="fresh-read",execution_id="same-exec",quote=2,
            evidence={"sample_ids":["observed"],"prior_read_liability_ids":["old-export"]})
        result=evaluate(self.state,self.config,USAGE,NOW)
        self.assertEqual(result["status"],"admitted")
        self.assertAlmostEqual(result["effective_usage_credits"],BASE+2)
        self.assertEqual(self.state["financial_journal"],before)
        self.assertIn("old-export",result["legacy_unknown_liability_ids"])
        self.assertIn("fresh-read",result["pending_reserved_operation_ids"])

    def test_retry_cannot_cite_an_unrelated_or_nonexistent_liability(self):
        for ids in (["exec:old"],["missing"],["old-export","old-export"]):
            with self.subTest(ids=ids),self.assertRaises(BudgetInputError):
                self.reserve(kind="export",operation_id="fresh-read",execution_id="same-exec",quote=2,
                    evidence={"sample_ids":["observed"],"prior_read_liability_ids":ids})

    def setUp(self):
        self.config, self.state = config(), state()
        self.spec = {"key":"transfers", "query_id":1, "performance":"medium"}

    def reserve(self, *, spec=None, params=None, kind="execution", quote=10,
                operation_id="attempt", execution_id=None, now=NOW, usage=USAGE,
                evidence=None, state=None, config=None):
        return reserve(state or self.state, config or self.config, usage, now,
                       spec or self.spec, {"lookback_hours":6} if params is None else params,
                       kind, quote, operation_id=operation_id,
                       evidence={"sample_ids":["observed"]} if evidence is None else evidence,
                       execution_id=execution_id)

    def export(self, operation_id="export-attempt", execution_id="exec-1", quote=10,
               now=NOW, usage=USAGE):
        return self.reserve(kind="export", quote=quote, operation_id=operation_id,
                            execution_id=execution_id, now=now, usage=usage)

    def add(self, identity, **row):
        self.state["financial_journal"]["obligations"][identity] = {"liability_id":identity, **row}

    def test_opening_known_is_reserved_conservatively_not_recharged(self):
        old = deepcopy(self.state)
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertAlmostEqual(result["effective_usage_credits"], BASE)
        self.assertAlmostEqual(result["opening_known_reserved_credits"], 254.814)
        self.assertFalse(result["opening_known_inclusion_claimed"])
        self.assertEqual(self.state, old)
        self.assertFalse(result["liabilities_settled"])
        self.assertEqual(result["legacy_unknown_liability_ids"], ["old-export"])

    def test_opening_known_base_is_reserved_even_with_zero_usage(self):
        result = evaluate(self.state, self.config, {**USAGE, "credits_used":0}, NOW)
        self.assertAlmostEqual(result["effective_usage_credits"], 354.814)

    def test_opening_known_increase_uses_full_current_cost(self):
        self.add("exec:old", kind="execution", execution_id="old",
                  cost_credits=1100, incurred_at_utc="2026-10-15T00:00:00Z")
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertAlmostEqual(result["effective_usage_credits"], 3003.249)
        self.assertIn("monthly_risk_headroom_exhausted", result["blockers"])

    def test_no_second_anchor_or_lower_usage_replenishment(self):
        _, book, _ = open_period(self.state, self.config, USAGE, NOW)
        open_period(self.state, self.config, {**USAGE, "credits_used":1850}, NOW)
        open_period(self.state, self.config, {**USAGE, "credits_used":1700}, NOW)
        self.assertEqual(book["opening_usage_credits"], 1803.249)
        self.assertEqual(book["usage_high_water_credits"], 1850)
        self.assertEqual(len(self.state[LANE]["periods"]), 1)

    def test_changed_authority_cannot_replenish_existing_period(self):
        open_period(self.state, self.config, USAGE, NOW)
        self.config["autonomous_operation"]["daily_risk_limit_credits"] = 200
        with self.assertRaises(BudgetInputError):
            open_period(self.state, self.config, USAGE, NOW)

    def test_exact_reservation_replay_and_expiry(self):
        approval = self.reserve()
        self.assertEqual(approval["query_parameters"], {"lookback_hours":6})
        self.assertEqual(approval["operation_id"], "attempt")
        self.assertEqual(self.reserve(), approval)
        with self.assertRaises(BudgetInputError):
            self.reserve(params={"lookback_hours":7})
        with self.assertRaises(BudgetInputError):
            self.reserve(now=NOW + timedelta(minutes=16))

    def test_pending_estimate_replaced_only_by_known_actual_charge(self):
        self.reserve()
        prior = evaluate(self.state, self.config, USAGE, NOW)
        self.assertAlmostEqual(prior["effective_usage_credits"], BASE + 10)
        self.add("exec:new", kind="execution", execution_id="new",
                 submission_id="submission:attempt", cost_credits=2,
                 incurred_at_utc="2026-10-20T12:00:00Z")
        after = evaluate(self.state, self.config, USAGE, NOW)
        self.assertAlmostEqual(after["effective_usage_credits"], BASE + 2)
        self.assertEqual(after["daily_committed_credits"], 2)
        self.assertNotIn("attempt", after["pending_reserved_operation_ids"])

    def test_usage_and_post_anchor_cost_are_max_not_sum(self):
        self.reserve()
        decision = evaluate(self.state, self.config, {**USAGE, "credits_used":2200}, NOW)
        self.assertEqual(decision["effective_usage_credits"], 2200)

    def test_legacy_pool_and_opening_unknown_floor_are_separate(self):
        self.add("legacy-floor", kind="export", cost_credits=None,
                 unresolved=True, known_cost_floor_credits=10)
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertAlmostEqual(result["effective_usage_credits"], BASE + 10)
        self.assertEqual(result["legacy_pool_credits"], 100)
        self.assertEqual(result["specific_unknown_risk_credits"], 10)

    def test_unknown_new_unreserved_blocks_without_erasing_legacy(self):
        open_period(self.state, self.config, USAGE, NOW)
        self.add("new-export", kind="export", cost_credits=None)
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertIn("unreserved_unknown_exposure:new-export", result["blockers"])

    def test_overrun_increases_charge_and_monthly_stop(self):
        self.reserve()
        self.add("exec:new", kind="execution", submission_id="submission:attempt",
                  cost_credits=1000, incurred_at_utc="2026-10-20T12:00:00Z")
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("monthly_risk_headroom_exhausted", result["blockers"])

    def test_forecast_slows_cadence_instead_of_globally_blocking(self):
        result = evaluate(self.state, self.config, USAGE, NOW, forecast_credits=1000)
        self.assertEqual(result["status"], "admitted")
        self.assertGreater(result["cadence_multiplier"], 1)

    def test_target_paces_expected_work_while_hard_limit_reserves_opening_known(self):
        result=evaluate(self.state,self.config,USAGE,NOW,forecast_credits=400)
        self.assertEqual(result["limit_credits"],3000)
        self.assertEqual(result["target_credits"],2500)
        self.assertEqual(result["opening_known_reserved_credits"],254.814)
        self.assertEqual(result["cadence_multiplier"],1)
        self.assertFalse(result["opening_known_inclusion_claimed"])

    def test_live_quota_minus_unspent_reserve_is_always_a_hard_limit(self):
        result=evaluate(self.state,self.config,{**USAGE,"credits_included":3000},NOW)
        self.assertEqual(result["limit_credits"],2000)
        self.assertEqual(result["status"],"blocked")

    def test_unsigned_outer_budget_knobs_cannot_raise_standing_authority(self):
        self.config.update(monthly_spend_guard_credits=10000,monthly_target_credits=9000)
        result=evaluate(self.state,self.config,{**USAGE,"credits_included":20000},NOW)
        self.assertEqual(result["limit_credits"],3000)
        self.assertEqual(result["target_credits"],2500)

    def test_new_period_carries_unknown_and_does_not_recharge_old_known(self):
        self.reserve()
        self.add("exec:new", kind="execution", submission_id="submission:attempt",
                 cost_credits=None, incurred_at_utc=None)
        result = evaluate(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(result["effective_usage_credits"], 110)
        self.assertIn("old-export", result["legacy_unknown_liability_ids"])
        self.assertIn("attempt", result["inherited_operation_ids"])

    def test_inherited_known_current_charge_replaces_quote(self):
        self.reserve()
        self.add("exec:new", kind="execution", submission_id="submission:attempt",
                 cost_credits=None, incurred_at_utc=None)
        self.assertEqual(evaluate(self.state, self.config, USAGE2, NOW2)["effective_usage_credits"], 110)
        self.state["financial_journal"]["obligations"]["exec:new"].update(
            cost_credits=2, incurred_at_utc="2026-11-05T12:00:00Z")
        result = evaluate(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(result["effective_usage_credits"], 102)
        self.assertNotIn("attempt", result["inherited_operation_ids"])

    def test_inherited_known_prior_period_charge_releases_quote(self):
        self.reserve()
        self.add("exec:new", kind="execution", submission_id="submission:attempt",
                 cost_credits=None, incurred_at_utc=None)
        self.assertEqual(evaluate(self.state, self.config, USAGE2, NOW2)["effective_usage_credits"], 110)
        self.state["financial_journal"]["obligations"]["exec:new"].update(
            cost_credits=2, incurred_at_utc="2026-10-20T00:00:00Z")
        result = evaluate(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(result["effective_usage_credits"], 100)
        self.assertNotIn("attempt", result["inherited_operation_ids"])

    def test_inherited_floor_and_quote_are_not_double_counted(self):
        self.reserve()
        self.add("exec:new", kind="execution", submission_id="submission:attempt",
                 cost_credits=None, incurred_at_utc=None, known_cost_floor_credits=40)
        result = evaluate(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(result["effective_usage_credits"], 140)
        self.assertAlmostEqual(result["specific_unknown_risk_credits"], 40)
        self.assertIn("attempt", result["inherited_operation_ids"])
        open_period(self.state, self.config, USAGE2, NOW2)
        del self.state["financial_journal"]["obligations"]["exec:new"]
        retained = evaluate(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(retained["effective_usage_credits"], 140)
        self.assertIn("attempt", retained["inherited_operation_ids"])
        open_period(self.state, self.config, USAGE3, NOW3)
        carried = evaluate(self.state, self.config, USAGE3, NOW3)
        self.assertEqual(carried["effective_usage_credits"], 140)
        self.assertIn("attempt", carried["inherited_operation_ids"])

    def test_rollover_retains_export_reservation_without_an_obligation(self):
        self.export()
        result = evaluate(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(result["effective_usage_credits"], 110)
        self.assertIn("export-attempt", result["inherited_operation_ids"])
        self.assertIn("export-attempt", result["pending_reserved_operation_ids"])

    def test_rollover_carries_all_prior_books_without_duplicates(self):
        self.reserve()
        open_period(self.state, self.config, USAGE2, NOW2)
        self.reserve(now=NOW2, usage=USAGE2, operation_id="attempt-2")
        result = evaluate(self.state, self.config, USAGE3, NOW3)
        self.assertEqual(result["effective_usage_credits"], 120)
        self.assertEqual(result["inherited_operation_ids"], ["attempt", "attempt-2"])
        self.assertEqual(result["pending_reserved_operation_ids"], ["attempt", "attempt-2"])

    def test_export_unknown_identity_mismatch_is_a_clear_blocker(self):
        self.export(operation_id="attempt-1", execution_id="exec-shared")
        self.add("attempt-2", kind="export", execution_id="exec-shared",
                 cost_credits=None, unresolved=True)
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertIn("unreserved_unknown_exposure:attempt-2", result["blockers"])
        self.assertIn("export_attempt_identity_mismatch:attempt-1:attempt-2",
                      result["blockers"])

    def test_export_known_mismatch_is_not_aliased_to_reservation(self):
        self.export(operation_id="attempt-1", execution_id="exec-shared")
        self.add("attempt-2", kind="export", execution_id="exec-shared",
                 cost_credits=2, incurred_at_utc="2026-10-20T12:00:00Z")
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertEqual(result["effective_usage_credits"], BASE + 12)
        self.assertEqual(result["pending_reserved_operation_ids"], ["attempt-1"])
        self.assertFalse(any("identity_mismatch" in item for item in result["blockers"]))

    def test_export_exact_attempt_id_replaces_quote_with_actual(self):
        self.export(operation_id="attempt-1")
        self.add("attempt-1", kind="export", execution_id="exec-1",
                 cost_credits=2, incurred_at_utc="2026-10-20T12:00:00Z")
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertEqual(result["effective_usage_credits"], BASE + 2)
        self.assertEqual(result["pending_reserved_operation_ids"], [])
        self.assertEqual(result["daily_committed_credits"], 2)

    def test_multiple_reads_may_share_execution_if_attempt_ids_are_exact(self):
        self.export(operation_id="read-1", execution_id="exec-shared")
        self.export(operation_id="read-2", execution_id="exec-shared")
        self.add("read-1", kind="export", execution_id="exec-shared",
                 cost_credits=None, unresolved=True)
        self.add("read-2", kind="export", execution_id="exec-shared",
                 cost_credits=None, unresolved=True)
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertFalse(any("identity_mismatch" in item for item in result["blockers"]))
        self.assertEqual(result["pending_reserved_operation_ids"], ["read-1", "read-2"])

    def test_overlapping_shifted_and_rewound_periods_are_rejected(self):
        open_period(self.state, self.config, USAGE, NOW)
        for usage in ({**USAGE, "period_start_utc":"2026-10-04T16:35:50Z"},
                      {**USAGE, "period_end_utc":"2026-11-03T00:00:00Z"},
                      {**USAGE, "period_start_utc":"2026-10-03T16:35:49Z"}):
            with self.assertRaises(BudgetInputError):
                evaluate(self.state, self.config, usage, NOW)
        open_period(self.state, self.config, USAGE2, NOW2)
        self.assertEqual(len(self.state[LANE]["periods"]), 2)

    def test_cross_period_operation_id_is_rejected(self):
        self.reserve()
        with self.assertRaises(BudgetInputError):
            self.reserve(now=NOW2, usage=USAGE2, operation_id="attempt")

    def test_replay_rejects_changed_shape_and_engine(self):
        spec = {**self.spec, "shape_version":2}
        self.reserve(spec=spec, operation_id="shape-attempt")
        with self.assertRaises(BudgetInputError):
            self.reserve(spec={**spec, "shape_version":3}, operation_id="shape-attempt")
        with self.assertRaises(BudgetInputError):
            self.reserve(spec={**spec, "performance":"large"}, operation_id="shape-attempt")

    def test_approval_auto_binds_evidence_and_returns_a_deepcopy(self):
        spec = {**self.spec, "shape_version":2, "verified_sql_sha256":"a"*64,
                "verified_query_version":4, "absolute_day":"2026-10-20"}
        approval = self.reserve(spec=spec, operation_id="bound")
        self.assertEqual(approval["sql_sha256"], "a"*64)
        self.assertEqual(approval["query_version"], 4)
        self.assertEqual(approval["absolute_day"], "2026-10-20")
        approval["sql_sha256"] = "b"*64
        stored = next(iter(self.state[LANE]["periods"].values()))[
            "operations"]["bound"]["approval"]
        self.assertEqual(stored["sql_sha256"], "a"*64)

    def test_daily_cap_uses_actual_incurred_date(self):
        self.reserve()
        self.add("exec:new", kind="execution", submission_id="submission:attempt",
                 cost_credits=150, incurred_at_utc="2026-10-20T13:00:00Z")
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertEqual(result["daily_committed_credits"], 150)
        self.assertIn("daily_risk_allowance_exhausted", result["blockers"])

    def test_daily_cap_includes_uncovered_known_today(self):
        open_period(self.state, self.config, USAGE, NOW)
        self.add("exec:free", kind="execution", cost_credits=200,
                 incurred_at_utc="2026-10-20T13:00:00Z")
        result = evaluate(self.state, self.config, USAGE, NOW)
        self.assertEqual(result["daily_committed_credits"], 200)
        self.assertIn("daily_risk_allowance_exhausted", result["blockers"])

    def test_daily_cap_uses_original_date_for_pending_quote(self):
        self.reserve()
        self.assertEqual(evaluate(self.state, self.config, USAGE, NOW)["daily_committed_credits"], 10)
        self.assertEqual(evaluate(self.state, self.config, USAGE, NOW + timedelta(days=1))["daily_committed_credits"], 0)

    def test_post_reservation_decision_includes_the_new_quote_once(self):
        self.reserve()
        stored = self.state[LANE]["last_decision"]
        fresh = evaluate(self.state, self.config, USAGE, NOW)
        self.assertAlmostEqual(stored["effective_usage_credits"], BASE + 10)
        self.assertEqual(stored["daily_committed_credits"], 10)
        self.assertEqual(stored["pending_reserved_operation_ids"], ["attempt"])
        self.assertEqual(stored["effective_usage_credits"], fresh["effective_usage_credits"])

    def test_malformed_api_and_books_raise_budgetinputerror(self):
        for call in (lambda: scope(None, self.spec, {"lookback_hours":6}, "execution", NOW),
                     lambda: scope(self.config, None, {"lookback_hours":6}, "execution", NOW),
                     lambda: scope(self.config, self.spec, 5, "execution", NOW),
                     lambda: evaluate(self.state, None, USAGE, NOW),
                     lambda: evaluate({LANE:{"schema_version":1,"periods":[]}},
                                      self.config, USAGE, NOW)):
            with self.assertRaises(BudgetInputError):
                call()

    def test_inexact_periods_are_rejected(self):
        for usage in ({**USAGE, "period_end_utc":"2026-11-04"},
                      {**USAGE, "period_end_utc":"2026-10-19T00:00:00Z"}):
            with self.assertRaises(BudgetInputError):
                evaluate(self.state, self.config, usage, NOW)

    def test_wrong_scope_parameters_engine_and_missing_export_identity(self):
        for spec, params, kind in ((self.spec, {"lookback_hours":True}, "execution"),
                (self.spec, {"lookback_hours":169}, "execution"),
                ({**self.spec, "performance":"large"}, {"lookback_hours":6}, "execution"),
                (self.spec, {"lookback_hours":6}, "export")):
            with self.assertRaises(BudgetInputError):
                scope(self.config, spec, params, kind, NOW)

    def test_estimate_is_not_a_cap_and_operation_ceiling_is_not_an_estimate(self):
        with self.assertRaises(BudgetInputError):
            self.reserve(quote=31, operation_id="too-large")
        self.assertFalse(evaluate(self.state, self.config, USAGE, NOW)["estimate_is_hard_cap"])


if __name__ == "__main__":
    unittest.main()
