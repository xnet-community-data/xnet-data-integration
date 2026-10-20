"""Offline estimator tests for autonomous standing-operation cost quotes.

``v3_autonomous_estimates`` quotes credits for continuous autonomous ops from
local evidence only (history, durable financial terminal costs, the 13 recent
approved batch operations, vetted imports). Every test is pure dict-in/dict-out:
no network, no Dune, no mutation, and every quote asserts it is NOT a cap and
NOT spending authority.
"""
import json
import statistics
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v3_autonomous_estimates as estimates
from v3_autonomous_estimates import (
    EstimatorInputError,
    estimate_operation,
    forecast_remaining_calendar,
)

NOW = "2026-10-20T12:00:00Z"
_NOW_DT = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def anchor_iso(hours_ago, base=None):
    base = base or _NOW_DT
    stamp = base - timedelta(hours=hours_ago)
    return stamp.isoformat().replace("+00:00", "Z")


def history_samples(query_id, params, costs, *, hours_ago, engine="medium",
                    shape=None, ids=None, **extra):
    rows = []
    for index, cost in enumerate(costs):
        row = {
            "query_id": query_id,
            "query_parameters": params,
            "execution_cost_credits": cost,
            "execution_ended_at": anchor_iso(hours_ago),
            "execution_id": ids[index] if ids else f"e{index}",
        }
        if engine is not None:
            row["performance"] = engine
        if shape is not None:
            row["shape_version"] = shape
        row.update(extra)
        rows.append(row)
    return rows


def spec(query_id, engine="medium", shape=None, **extra):
    value = {"query_id": query_id}
    if engine is not None:
        value["performance"] = engine
    if shape is not None:
        value["shape_version"] = shape
    value.update(extra)
    return value


def batch_op(op_id, query_id, params, cost, *, engine="medium", shape="4",
             submitted="2026-10-20T02:00:00Z", received="2026-10-20T02:03:00Z",
             **extra):
    row = {
        "execution_id": op_id,
        "query_id": query_id,
        "query_parameters": params,
        "engine": engine,
        "cost": cost,
        "submitted_at": submitted,
        "result_received": received,
    }
    if shape is not None:
        row["shape"] = shape
    row.update(extra)
    return row


def assert_quote_shape(test, result):
    """The shared human-facing contract of every estimate result."""
    test.assertIn(result["status"], ("estimated", "unknown"))
    test.assertIn("evidence", result)
    test.assertFalse(result["estimate_is_hard_cap"])
    test.assertFalse(result["estimate_is_authority"])
    test.assertIn("credible estimate", result["evidence"]["quote_basis"])
    test.assertIn("pool_summary", result["evidence"])
    for field in ("sample_execution_ids", "sample_parameters",
                  "sample_ages_hours", "method"):
        test.assertIn(field, result["evidence"])


class ExactIdentityQuoteTests(unittest.TestCase):
    """>=2 exact samples quote max(observed_max, mean+3sigma); outliers stay
    inside the mean and are never dropped; one sample needs the x2 margin."""

    def test_two_sample_quote_is_uplifted_envelope_with_outlier_inclusive_mean(self):
        history = history_samples(
            8894037, {"lookback_hours": 2}, [1.04, 0.9, 1.4],
            hours_ago=2, ids=["h1", "h2", "h3"],
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037), {"lookback_hours": 2}, NOW
        )
        assert_quote_shape(self, result)
        self.assertEqual(result["status"], "estimated")
        values = [1.04, 0.9, 1.4]
        expected_quote = max(
            max(values),
            statistics.fmean(values) + 3.0 * statistics.stdev(values),
        )
        self.assertAlmostEqual(result["cost_estimate_credits"], expected_quote)
        self.assertAlmostEqual(result["expected_credits"], statistics.fmean(values))
        self.assertEqual(result["evidence"]["cost_observed_max_credits"], 1.4)
        self.assertEqual(
            result["evidence"]["sample_execution_ids"], ["h1", "h2", "h3"]
        )
        self.assertEqual(result["evidence"]["sample_ages_hours"], [2.0, 2.0, 2.0])
        self.assertIn("exact_identity", result["evidence"]["method"])
        self.assertIn("mean_plus_3sigma", result["evidence"]["method"])
        self.assertIsNone(result["evidence"]["lookback_scaling"])

    def test_outlier_stays_inside_mean_and_quote_takes_envelope(self):
        history = history_samples(
            8894185, {}, [0.1, 0.1, 0.1, 5.0], hours_ago=1
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894185), {}, NOW
        )
        self.assertEqual(result["status"], "estimated")
        # The outlier is neither dropped nor multiplied: it is inside the mean...
        self.assertAlmostEqual(result["expected_credits"], 1.325)
        # ...and separately visible as the observed maximum...
        self.assertEqual(result["evidence"]["cost_observed_max_credits"], 5.0)
        # ...while the envelope quote absorbs the dispersion above the max.
        values = [0.1, 0.1, 0.1, 5.0]
        self.assertAlmostEqual(
            result["cost_estimate_credits"],
            max(
                max(values),
                statistics.fmean(values) + 3.0 * statistics.stdev(values),
            ),
        )

    def test_flat_two_sample_pool_quotes_observed_max(self):
        history = history_samples(8895093, {}, [0.3, 0.3], hours_ago=3)
        result = estimate_operation(
            {"execution_history": history}, spec(8895093), {}, NOW
        )
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["cost_estimate_credits"], 0.3)
        self.assertAlmostEqual(result["expected_credits"], 0.3)

    def test_single_exact_sample_quotes_with_explicit_two_x_margin(self):
        batch = [batch_op(
            "bbb", 8894132, {"lookback_hours": 120}, 0.355, shape="4",
        )]
        result = estimate_operation(
            {"approved_recovery_batches": batch},
            spec(8894132, shape="4"), {"lookback_hours": 120}, NOW,
        )
        assert_quote_shape(self, result)
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(result["cost_estimate_credits"], 0.71)
        self.assertAlmostEqual(result["expected_credits"], 0.355)
        self.assertIn("single_sample_explicit_margin", result["evidence"]["method"])
        self.assertEqual(result["evidence"]["sample_execution_ids"], ["bbb"])
        self.assertAlmostEqual(result["evidence"]["sample_ages_hours"][0], 9.95)

    def test_estimate_never_mutates_state(self):
        state = {
            "execution_history": history_samples(
                8894037, {"lookback_hours": 2}, [1.0, 1.4], hours_ago=1
            ),
            "approved_recovery_batches": [batch_op(
                "op1", 8894038, {"oldest_days_ago": 1, "newest_days_ago": 1}, 0.283,
            )],
            "autonomous_cost_samples": [],
        }
        before = deepcopy(state)
        estimated = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW
        )
        unknown = estimate_operation(
            state, spec(66666), {}, NOW
        )
        export = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW,
            kind="export", result_bytes=1000,
        )
        self.assertEqual(state, before)
        self.assertEqual(estimated["status"], "estimated")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(export["status"], "unknown")


class AgingTests(unittest.TestCase):
    """Sparse evidence up to 30 days quotes under a documented linear aging
    margin; beyond the ceiling it is stale and unusable."""

    def test_aged_sample_carries_the_documented_aging_margin(self):
        history = history_samples(8894132, {}, [0.4, 0.6], hours_ago=720)
        result = estimate_operation(
            {"execution_history": history}, spec(8894132), {}, NOW
        )
        self.assertEqual(result["status"], "estimated")
        # factor at the 30-day ceiling: 1 + 0.5 * (720-168)/(720-168) = 1.5
        adjusted = [0.6, 0.9]
        expected_quote = max(
            max(adjusted),
            statistics.fmean(adjusted) + 3.0 * statistics.stdev(adjusted),
        )
        self.assertAlmostEqual(result["cost_estimate_credits"], expected_quote)
        # the expected mean is the un-aged outlier-inclusive pooled mean
        self.assertAlmostEqual(result["expected_credits"], 0.5)
        self.assertTrue(result["evidence"]["aging"]["applied"])
        self.assertAlmostEqual(result["evidence"]["aging"]["max_factor"], 1.5)

    def test_partial_aging_ramps_linearly_beyond_the_free_window(self):
        history = history_samples(8894185, {}, [0.8], hours_ago=400)
        result = estimate_operation(
            {"execution_history": history}, spec(8894185), {}, NOW
        )
        factor = 1.0 + 0.5 * (400.0 - 168.0) / (720.0 - 168.0)
        self.assertAlmostEqual(
            result["cost_estimate_credits"], 2.0 * 0.8 * factor
        )
        self.assertAlmostEqual(result["expected_credits"], 0.8)
        self.assertAlmostEqual(
            result["evidence"]["sample_ages_hours"][0], 400.0
        )

    def test_sample_older_than_30_days_is_stale_and_blocks(self):
        history = history_samples(8894185, {}, [0.5], hours_ago=800)
        result = estimate_operation(
            {"execution_history": history}, spec(8894185), {}, NOW
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["cost_estimate_credits"])
        self.assertIn("no_usable_exact_identity_samples", result["reasons"][0])
        self.assertIn("stale beyond", result["reasons"][0])

    def test_fresh_samples_quotable_despite_one_stale_sibling(self):
        history = history_samples(
            8894185, {}, [0.5], hours_ago=800, ids=["legacy"]
        ) + history_samples(8894185, {}, [0.2, 0.3], hours_ago=2,
                            ids=["f1", "f2"])
        result = estimate_operation(
            {"execution_history": history}, spec(8894185), {}, NOW
        )
        self.assertEqual(result["status"], "estimated")
        values, adjusted = [0.2, 0.3], [0.2, 0.3]
        self.assertAlmostEqual(
            result["cost_estimate_credits"],
            max(0.3, statistics.fmean(adjusted) + 3.0 * statistics.stdev(adjusted)),
        )
        self.assertEqual(result["evidence"]["pool_counts"]["exact_stale"], 1)
        self.assertEqual(result["evidence"]["pool_counts"]["exact_usable"], 2)

    def test_undated_row_cannot_quote_without_dated_peers(self):
        undated = {
            "query_id": 8894185, "query_parameters": {},
            "performance": "medium", "execution_cost_credits": 0.4,
            "execution_id": "e-old",
        }
        history = history_samples(
            8894185, {}, [0.2, 0.21], hours_ago=1, ids=["f1", "f2"]
        ) + [undated]
        result = estimate_operation(
            {"execution_history": history}, spec(8894185), {}, NOW
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["evidence"]["pool_counts"]["exact_undated"], 1)
        # all-undated identity never quotes: missing timestamps are evidence gaps
        only_undated = {"execution_history": [dict(undated,
                          execution_cost_credits=0.4)]}
        other = {"query_id": 8894185, "query_parameters": {},
                 "performance": "medium", "execution_cost_credits": 0.4}
        self.assertEqual(
            estimate_operation(only_undated, spec(8894185), {}, NOW)["status"],
            "unknown",
        )
        self.assertEqual(
            estimate_operation({"execution_history": [other, other]},
                               spec(8894185), {}, NOW)["status"],
            "unknown",
        )


class ExactRefusesCrossIdentityTests(unittest.TestCase):
    """Identity is (query, engine, shape, canonical params): no silent mixes."""

    def test_daily_absolute_8894038_never_mixes_with_hot_8894037(self):
        batches = [
            batch_op(f"op{index}", 8894038,
                     {"oldest_days_ago": 1, "newest_days_ago": 1}, 0.28 + index / 100)
            for index in range(2)
        ]
        result = estimate_operation(
            {"approved_recovery_batches": batches},
            spec(8894037), {"lookback_hours": 2}, NOW,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("no_samples_for_identity", result["reasons"])
        self.assertIsNone(result["evidence"]["lookback_scaling"])

    def test_engine_mismatch_does_not_mix(self):
        history = history_samples(
            8894037, {"lookback_hours": 2}, [1.0, 1.1], hours_ago=1,
            engine="medium",
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037, engine="large"),
            {"lookback_hours": 2}, NOW,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["evidence"]["pool_counts"]["matching_rows"], 0)
        # ...and a differently-engined sample pair cannot rescue the pool.
        other = history_samples(
            8894037, {"lookback_hours": 2}, [1.0, 1.1], hours_ago=1,
            engine="small",
        )
        result = estimate_operation(
            {"execution_history": history + other},
            spec(8894037, engine="large"), {"lookback_hours": 2}, NOW,
        )
        self.assertEqual(result["status"], "unknown")

    def test_shape_mismatch_does_not_mix(self):
        history = history_samples(
            8894037, {"lookback_hours": 2}, [1.0, 1.1], hours_ago=1, shape="2",
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037, shape="3"),
            {"lookback_hours": 2}, NOW,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["evidence"]["pool_counts"]["matching_rows"], 0)

    def test_absolute_day_params_translate_in_the_single_day_family(self):
        # Same-query 8894038 single-UTC-day samples at ANOTHER day offset
        # pool via calendar_translation_one_day (per-day variance is carried
        # by their costs); NO lookback-hours transfer ever mixes into
        # day-absolute parameters (transfer_usable stays 0).
        history = history_samples(
            8894038, {"oldest_days_ago": 1, "newest_days_ago": 1},
            [0.283, 0.284], hours_ago=4, engine="medium", shape="4",
        )
        result = estimate_operation(
            {"execution_history": history},
            spec(8894038, shape="4"),
            {"oldest_days_ago": 2, "newest_days_ago": 2}, NOW,
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["evidence"]["pool_counts"]["transfer_usable"], 0)
        self.assertIn(
            "calendar_translation_one_day", result["evidence"]["method"]
        )
        translation = result["evidence"]["calendar_translation"]
        self.assertEqual(translation["method"], "calendar_translation_one_day")
        self.assertEqual(translation["requested_day_offset_days"], 2)
        self.assertEqual(translation["sample_day_offsets_days"], [1, 1])
        self.assertIn("none", translation["scaling"])
        values = [0.283, 0.284]
        quote = max(
            max(values),
            statistics.fmean(values) + 3.0 * statistics.stdev(values),
        )
        self.assertAlmostEqual(result["cost_estimate_credits"], quote)
        # The pooled samples' OWN original offsets stay in the evidence.
        self.assertEqual(
            result["evidence"]["sample_parameters"],
            ['{"newest_days_ago":1,"oldest_days_ago":1}'] * 2,
        )


class LookbackTransferTests(unittest.TestCase):
    """Same query+engine+shape transfers across lookback-hours inside the
    documented domain with max(1, requested/sample_hours) scaling."""

    def test_transfer_scales_up_with_documented_formula(self):
        history = history_samples(
            8894037, {"lookback_hours": 2}, [1.0, 1.2], hours_ago=1
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037),
            {"lookback_hours": 3}, NOW,
        )
        assert_quote_shape(self, result)
        self.assertEqual(result["evidence"]["method"].split("_")[0],
                         "lookback")
        self.assertIn("lookback_transfer", result["evidence"]["method"])
        scaled = [1.5, 1.8]
        self.assertAlmostEqual(
            result["cost_estimate_credits"],
            max(
                max(scaled),
                statistics.fmean(scaled) + 3.0 * statistics.stdev(scaled),
            ),
        )
        self.assertAlmostEqual(
            result["expected_credits"], statistics.fmean(scaled)
        )
        scaling = result["evidence"]["lookback_scaling"]
        self.assertTrue(scaling["applied"])
        self.assertEqual(scaling["scaling_factors"], [1.5, 1.5])
        self.assertEqual(
            scaling["formula"], "max(1, requested_hours / sample_hours)"
        )
        self.assertEqual(scaling["requested_lookback_hours"], 3.0)

    def test_transfer_never_scales_down(self):
        history = history_samples(
            8894037, {"lookback_hours": 3}, [1.0, 1.2], hours_ago=1
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037),
            {"lookback_hours": 2}, NOW,
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["evidence"]["lookback_scaling"]["scaling_factors"],
                         [1.0, 1.0])
        values = [1.0, 1.2]
        self.assertAlmostEqual(
            result["cost_estimate_credits"],
            max(
                max(values),
                statistics.fmean(values) + 3.0 * statistics.stdev(values),
            ),
        )

    def test_request_outside_default_48h_domain_blocks_transfer(self):
        history = history_samples(
            8894037, {"lookback_hours": 47}, [0.1], hours_ago=2
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037),
            {"lookback_hours": 120}, NOW,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(
            any(r.startswith("requested_lookback_outside_transfer_domain")
                for r in result["reasons"])
        )

    def test_bbb_query_documented_168h_domain_allows_120h_transfer(self):
        batch = [batch_op("bbb", 8894132, {"lookback_hours": 120}, 0.355,
                          shape="4")]
        result = estimate_operation(
            {"approved_recovery_batches": batch},
            spec(8894132, shape="4"), {"lookback_hours": 168}, NOW,
        )
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(
            result["cost_estimate_credits"], 0.355 * (168.0 / 120.0) * 2.0
        )
        self.assertAlmostEqual(
            result["expected_credits"], 0.355 * (168.0 / 120.0)
        )
        self.assertEqual(
            result["evidence"]["lookback_scaling"]["domain_max_hours"], 168.0
        )
        self.assertEqual(
            result["evidence"]["lookback_scaling"]["scaling_factors"], [1.4]
        )

    def test_policy_can_shrink_a_query_transfer_domain(self):
        batch = [batch_op("bbb", 8894132, {"lookback_hours": 120}, 0.355,
                          shape="4")]
        result = estimate_operation(
            {"approved_recovery_batches": batch},
            spec(8894132, shape="4"), {"lookback_hours": 168}, NOW,
            policy={"lookback_transfer": {"query_max_hours": {"8894132": 120.0}}},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(
            any(r.startswith("requested_lookback_outside_transfer_domain")
                for r in result["reasons"])
        )

    def test_transfer_refuses_other_parameter_differences(self):
        history = history_samples(
            8894037, {"lookback_hours": 2, "holder_rank": 5}, [1.0, 1.1],
            hours_ago=1,
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037),
            {"lookback_hours": 3}, NOW,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["evidence"]["pool_counts"]["transfer_usable"], 0)

    def test_transfer_disabled_by_policy_blocks_without_exact_samples(self):
        history = history_samples(
            8894037, {"lookback_hours": 2}, [1.0, 1.1], hours_ago=1
        )
        result = estimate_operation(
            {"execution_history": history}, spec(8894037),
            {"lookback_hours": 4}, NOW,
            policy={"lookback_transfer": {"enabled": False}},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("lookback_transfer_disabled_by_policy", result["reasons"])


class DuplicateAndLaneTests(unittest.TestCase):
    """Dedupe by execution id; conflicting costs keep the MAXIMUM and are
    never hidden; every local lane contributes."""

    def test_duplicate_execution_id_resolves_to_max_conflicting_cost(self):
        state = {
            "execution_history": history_samples(
                8894037, {"lookback_hours": 2}, [0.2], hours_ago=1, ids=["dup"]
            ),
            "approved_recovery_batches": [
                batch_op("dup", 8894037, {"lookback_hours": 2}, 0.3,
                         shape=None)
            ],
        }
        result = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(
            result["evidence"]["pool_summary"]["duplicate_cost_conflicts"], 1
        )
        # one merged sample (max cost 0.3) quotes with the single-sample margin
        self.assertAlmostEqual(result["cost_estimate_credits"], 0.6)
        self.assertEqual(
            result["evidence"]["sample_sources"], [["approved_recovery_batches[0]",
                                                    "execution_history"]]
        )

    def test_identical_duplicate_ids_collapse_without_conflicts(self):
        state = {
            "execution_history": history_samples(
                8894037, {"lookback_hours": 2}, [0.2, 0.2], hours_ago=1,
                ids=["same", "same"],
            ),
        }
        result = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW
        )
        self.assertEqual(
            result["evidence"]["pool_summary"]["deduped_rows"], 1
        )
        self.assertEqual(
            result["evidence"]["pool_summary"]["duplicate_cost_conflicts"], 0
        )

    def test_batch_lane_accepts_operations_list_and_id_map(self):
        params = {"oldest_days_ago": 1, "newest_days_ago": 1}
        ops = [
            batch_op("op1", 8894038, params, 0.283),
            batch_op("op2", 8894038, params, 0.284),
        ]
        from_list = estimate_operation(
            {"approved_recovery_batches": ops},
            spec(8894038, shape="4"), params, NOW,
        )
        from_map = estimate_operation(
            {"approved_recovery_batches": {"op1": ops[0], "op2": ops[1]}},
            spec(8894038, shape="4"), params, NOW,
        )
        self.assertEqual(from_list["status"], "estimated")
        self.assertEqual(
            from_list["cost_estimate_credits"], from_map["cost_estimate_credits"]
        )
        self.assertEqual(len(from_list["evidence"]["sample_execution_ids"]), 2)

    def test_real_nested_batch_containers_are_not_cost_rows(self):
        params={"oldest_days_ago":1,"newest_days_ago":1}
        ops={"one":batch_op("op1",8894038,params,0.283),"two":batch_op("op2",8894038,params,0.284)}
        for batches in ({"authorization":"approved","operations":ops},
                        {"batch-id":{"authorization":"approved","status":"completed","operations":ops},
                         "metadata-only":{"authorization":"no operation here"}}):
            result=estimate_operation({"approved_recovery_batches":batches},spec(8894038,shape="4"),params,NOW)
            self.assertEqual(result["status"],"estimated")
            self.assertEqual(set(result["evidence"]["sample_execution_ids"]),{"op1","op2"})

    def test_nested_batch_does_not_hide_malformed_priced_operations(self):
        for batches in ({"batch":{"operations":{"bad":{"execution_cost_credits":2}}}},
                        {"operations":[None]}, {"bad-priced-row":{"execution_cost_credits":2}}):
            with self.assertRaises(EstimatorInputError):
                estimate_operation({"approved_recovery_batches":batches},spec(8894038),
                                   {"oldest_days_ago":1,"newest_days_ago":1},NOW)

    def test_real_archived_daily_results_import_through_production_container_shape(self):
        root=Path(__file__).resolve().parent/"fixtures/approved_batch_20261020"
        receipt=json.loads((root/"source_acceptance_receipt.json").read_text())
        operations={}
        for source in receipt["provider_sources"]:
            response=json.loads((root/(source["operation"]+".json")).read_text())
            operations[source["operation"]]={"query_id":8894038,"query_parameters":source["query_parameters"],
                "execution_id":response["executionId"],"execution_cost_credits":float(response["resultMetadata"]["executionCostCredits"]),
                "result_received_at_utc":source["received_at_utc"],"status":"accepted"}
        result=estimate_operation({"approved_recovery_batches":{"approved_batch":{"authorization":"user",
            "operations":operations}}},spec(8894038),{"oldest_days_ago":9,"newest_days_ago":9},NOW,
            policy={"default_engine":"medium"})
        self.assertEqual(result["status"],"estimated")
        self.assertEqual(len(result["evidence"]["sample_execution_ids"]),5)
        self.assertAlmostEqual(result["expected_credits"],0.511)
        self.assertGreaterEqual(result["cost_estimate_credits"],1.435)

    def test_financial_terminal_rows_quote_and_engineless_inherit_policy_engine(self):
        state = {"financial_journal": {"obligations": {
            "exec:x1": {
                "liability_id": "exec:x1", "kind": "execution",
                "execution_id": "x1", "query_id": 8894185,
                "query_parameters": {}, "cost_credits": 0.2,
                "submitted_at_utc": "2026-10-20T02:00:00Z",
                "incurred_at_utc": "2026-10-20T02:01:00Z",
            },
            "exec:x2": {
                "liability_id": "exec:x2", "kind": "execution",
                "execution_id": "x2", "query_id": 8894185,
                "query_parameters": {}, "cost_credits": 0.3,
                "submitted_at_utc": "2026-10-20T03:00:00Z",
                "incurred_at_utc": "2026-10-20T03:01:00Z",
            },
        }}}
        result = estimate_operation(
            state, spec(8894185, engine=None), {}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["evidence"]["cost_observed_max_credits"], 0.3)
        self.assertEqual(
            result["evidence"]["sample_execution_ids"], ["x1", "x2"]
        )
        # a different engine tier is NOT rescued by engine-less evidence
        result = estimate_operation(
            state, spec(8894185), {}, NOW, policy={"default_engine": "small"}
        )
        self.assertEqual(result["status"], "unknown")

    def test_financial_unresolved_rows_are_not_evidence(self):
        state = {"financial_journal": {"obligations": {
            "exec:f1": {
                "liability_id": "exec:f1", "kind": "execution",
                "execution_id": "f1", "query_id": 8894185,
                "query_parameters": {}, "cost_credits": None,
                "known_cost_floor_credits": 0.1, "unresolved": True,
                "submitted_at_utc": "2026-10-20T02:00:00Z",
            },
        }}}
        result = estimate_operation(
            state, spec(8894185, engine=None), {}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            result["evidence"]["pool_summary"]["deduped_rows"], 0
        )

    def test_vetted_import_lane_carries_timestamped_samples(self):
        imported = history_samples(
            8895090, {}, [0.15, 0.16], hours_ago=5, engine="medium",
        )
        state = {"autonomous_cost_samples": imported}
        result = estimate_operation(state, spec(8895090), {}, NOW)
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(
            result["evidence"]["sample_sources"],
            [["autonomous_cost_samples"], ["autonomous_cost_samples"]],
        )


class MalformedInputTests(unittest.TestCase):
    """Fail-loud on unusable evidence; fail-closed (unknown) when merely
    unquotable. Timestamps must be exact UTC instants; costs finite numbers."""

    def test_nonfinite_and_boolean_costs_raise(self):
        for bad in (True, float("nan"), float("inf")):
            state = {"execution_history": [{
                "query_id": 8894037, "query_parameters": {"lookback_hours": 2},
                "performance": "medium", "execution_cost_credits": bad,
                "execution_ended_at": anchor_iso(1), "execution_id": "bad",
            }]}
            with self.assertRaises(EstimatorInputError):
                estimate_operation(
                    state, spec(8894037), {"lookback_hours": 2}, NOW
                )

    def test_missing_timestamps_are_skipped_not_fatal(self):
        state = {"execution_history": [{
            "query_id": 8894037, "query_parameters": {"lookback_hours": 2},
            "performance": "medium", "execution_cost_credits": 0.5,
            "execution_id": "undated",
        }]}
        result = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("no_usable_exact_identity_samples", result["reasons"][0])

    def test_future_naive_and_date_only_timestamps_raise(self):
        base = {
            "query_id": 8894037, "query_parameters": {"lookback_hours": 2},
            "performance": "medium", "execution_cost_credits": 0.5,
        }
        for stamp in (
            "2026-10-21T00:00:00Z",        # future
            "2026-10-19T10:00:00",         # naive
            "2026-10-19",                  # date-only
            "not-a-stamp",
        ):
            state = {"execution_history": [dict(base, execution_ended_at=stamp)]}
            with self.assertRaises(EstimatorInputError):
                estimate_operation(
                    state, spec(8894037), {"lookback_hours": 2}, NOW
                )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": [base]}, spec(8894037),
                {"lookback_hours": 2}, "2026-10-20",
            )

    def test_naive_forecast_instant_raises(self):
        with self.assertRaises(EstimatorInputError):
            forecast_remaining_calendar(
                [], now_utc="2026-10-20T10:00:00", period_end_utc=NOW,
            )

    def test_nonfinite_now_and_bad_spec_and_kind_raise(self):
        with self.assertRaises(EstimatorInputError):
            estimate_operation({}, spec(8894037), {}, "not-a-stamp")
        with self.assertRaises(EstimatorInputError):
            estimate_operation({"execution_history": []}, {"query_id": None},
                               {}, NOW)
        with self.assertRaises(EstimatorInputError):
            estimate_operation({"execution_history": []}, spec(8894037),
                               {}, NOW, kind="storage")
        with self.assertRaises(EstimatorInputError):
            estimate_operation({"execution_history": []}, spec(8894037),
                               ["lookback", 2], NOW)
        with self.assertRaises(EstimatorInputError):
            estimate_operation({"execution_history": []}, spec(8894037),
                               {"lookback_hours": 0}, NOW)
        with self.assertRaises(EstimatorInputError):
            estimate_operation({"execution_history": []}, spec(8894037),
                               {"lookback_hours": True}, NOW)

    def test_contradictory_timestamp_and_cost_aliases_raise(self):
        base = {
            "query_id": 8894037, "query_parameters": {"lookback_hours": 2},
            "performance": "medium", "execution_id": "conflict",
        }
        state = {"execution_history": [dict(
            base, execution_cost_credits=0.5, cost_credits=0.9,
            execution_ended_at=anchor_iso(1),
        )]}
        with self.assertRaises(EstimatorInputError):
            estimate_operation(state, spec(8894037), {"lookback_hours": 2}, NOW)
        state = {"execution_history": [dict(
            base, execution_cost_credits=0.5,
            execution_ended_at=anchor_iso(1),
            submitted_at=anchor_iso(99),
        )]}
        # different FIELDS for one row may legitimately describe different
        # instants; the LATEST exact instant anchors the row.
        result = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW
        )
        self.assertEqual(
            result["evidence"]["pool_counts"]["exact_undated"], 0
        )

    def test_policy_rejects_unknown_and_cost_shaped_keys(self):
        for bad_policy in (
            {"max_run_credits": 5.0},
            {"cost_estimate_credits": 1.0},
            {"not_a_knob": True},
            {"lookback_transfer": {"bogus": 1}},
        ):
            with self.assertRaises(EstimatorInputError):
                estimate_operation(
                    {"execution_history": []}, spec(8894037), {}, NOW,
                    policy=bad_policy,
                )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []}, spec(8894037), {}, NOW,
                policy={"single_sample_margin": 0.5},
            )

    def test_policy_knobs_propagate_to_quotes(self):
        history_aged = history_samples(
            8894132, {}, [0.4, 0.6], hours_ago=300
        )
        tucked = estimate_operation(
            {"execution_history": history_aged}, spec(8894132), {}, NOW,
            policy={"aging_margin_fraction": 0.0},
        )
        values, adjusted = [0.4, 0.6], [0.4, 0.6]
        self.assertAlmostEqual(
            tucked["cost_estimate_credits"],
            max(
                max(adjusted),
                statistics.fmean(adjusted) + 3.0 * statistics.stdev(adjusted),
            ),
        )
        self.assertFalse(tucked["evidence"]["aging"]["applied"])
        # a tighter evidence ceiling turns 300h-old samples stale
        stale = estimate_operation(
            {"execution_history": history_aged}, spec(8894132), {}, NOW,
            policy={"max_sample_age_hours": 240.0, "aging_free_hours": 24.0},
        )
        self.assertEqual(stale["status"], "unknown")
        self.assertIn("stale beyond", stale["reasons"][0])
        # single-sample margin is policy-scaled, never below the observation
        single = history_samples(8894185, {}, [0.3], hours_ago=1)
        result = estimate_operation(
            {"execution_history": single}, spec(8894185), {}, NOW,
            policy={"single_sample_margin": 3.0, "variance_uplift_sigma": 2.0},
        )
        self.assertAlmostEqual(result["cost_estimate_credits"], 0.9)

    def test_nonterminal_state_rows_are_not_comparable(self):
        state = {"execution_history": history_samples(
            8894037, {"lookback_hours": 2}, [0.4, 0.5], hours_ago=1
        )}
        state["execution_history"][0]["state"] = "QUERY_STATE_COMPLETED"
        state["execution_history"][1]["state"] = "QUERY_STATE_RUNNING"
        result = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW
        )
        # the running row refused with a skip note; one usable sample quotes
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(
            result["evidence"]["pool_summary"]["skipped"],
            {"nonterminal_state_sample": 1},
        )
        self.assertEqual(result["evidence"]["pool_counts"]["exact_usable"], 1)


class ExportQuoteTests(unittest.TestCase):
    """Exports quote the ACTUAL completed result: declared bytes inflated by
    same-query wire/declared ratio samples + 25% + fixed 16 KiB JSON overhead;
    transport-estimated, never metered; no ratio -> unknown unless the
    explicit policy fallback documents the 4x bound."""

    RATIO_STATE = {
        "autonomous_cost_samples": [
            {
                "query_id": 8894037, "query_parameters": {"lookback_hours": 2},
                "performance": "medium", "shape_version": "2",
                "cost": 0.1, "submitted_at": anchor_iso(3),
                "result_received": anchor_iso(2.9),
                "result_bytes": 100000, "export_wire_bytes": 188000,
                "execution_id": "w1",
            },
            {
                "query_id": 8894037, "query_parameters": {"lookback_hours": 2},
                "performance": "medium", "shape_version": "2",
                "cost": 0.1, "submitted_at": anchor_iso(2),
                "result_received": anchor_iso(1.9),
                "result_bytes": 100000, "export_wire_bytes": 292000,
                "execution_id": "w2",
            },
        ]
    }

    def test_export_quote_from_ratio_envelope_with_overheads(self):
        result = estimate_operation(
            self.RATIO_STATE, spec(8894037, shape="2"), {"lookback_hours": 2},
            NOW, kind="export", result_bytes=100000,
        )
        assert_quote_shape(self, result)
        self.assertEqual(result["status"], "estimated")
        ratios = [1.88, 2.92]
        inflation = max(
            max(ratios),
            statistics.fmean(ratios) + 3.0 * statistics.stdev(ratios),
        )
        transport = 100000 * inflation * 1.25 + 16384
        self.assertAlmostEqual(
            result["cost_estimate_credits"], transport / 1_000_000.0 * 10.0
        )
        self.assertAlmostEqual(
            result["expected_credits"], result["cost_estimate_credits"]
        )
        export = result["evidence"]["export"]
        self.assertEqual(export["method"], "ratio_envelope_mean_plus_3sigma")
        self.assertEqual(export["overhead_transport_fraction"], 0.25)
        self.assertEqual(export["fixed_transport_overhead_bytes"], 16384)
        self.assertEqual(export["basis"], "transport_estimated_not_metered")
        self.assertEqual(
            [entry["id"] for entry in export["ratio_samples"]], ["w1", "w2"]
        )
        self.assertNotIn("wire_inflation", result["evidence"])

    def test_export_single_ratio_sample_needs_explicit_two_x_margin(self):
        state = {"autonomous_cost_samples": [
            self.RATIO_STATE["autonomous_cost_samples"][0]]}
        result = estimate_operation(
            state, spec(8894037, shape="2"), {"lookback_hours": 2}, NOW,
            kind="export", result_bytes=100000,
        )
        self.assertEqual(result["status"], "estimated")
        transport = 100000 * 1.88 * 2.0 * 1.25 + 16384
        self.assertAlmostEqual(
            result["cost_estimate_credits"], transport / 1_000_000.0 * 10.0
        )
        self.assertEqual(
            result["evidence"]["export"]["method"],
            "single_ratio_sample_explicit_margin",
        )

    def test_export_requires_positive_declared_size_for_nonempty_data(self):
        result = estimate_operation(
            {"execution_history": []}, spec(8894037, shape="2"),
            {"lookback_hours": 2}, NOW, kind="export",
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn(
            "missing_export_transport_basis", result["reasons"][0]
        )
        result = estimate_operation(
            {"execution_history": []}, spec(8894037, shape="2"),
            {"lookback_hours": 2}, NOW, kind="export", result_bytes=0,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn(
            "declared_result_bytes_not_positive", result["reasons"][0]
        )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []},
                spec(8894037, shape="2", total_rows=7),
                {"lookback_hours": 2}, NOW, kind="export", result_bytes=0,
            )

    def test_export_zero_rows_quotes_zero(self):
        result = estimate_operation(
            {"execution_history": []}, spec(8894037, total_rows=0), {}, NOW,
            kind="export",
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["cost_estimate_credits"], 0.0)
        self.assertEqual(
            result["evidence"]["export"]["method"], "zero_row_result"
        )
        self.assertEqual(
            result["evidence"]["export"]["basis"],
            "transport_estimated_not_metered",
        )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []},
                spec(8894037, total_rows=0),
                {}, NOW, kind="export", result_bytes=1000,
            )

    def test_export_without_ratio_evidence_is_unknown_by_default(self):
        state = {"execution_history": history_samples(
            8894037, {"lookback_hours": 2}, [0.2, 0.3], hours_ago=1
        )}
        result = estimate_operation(
            state, spec(8894037), {"lookback_hours": 2}, NOW,
            kind="export", result_bytes=50000,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(
            any(r.startswith("no_export_ratio_evidence") for r in result["reasons"])
        )

    def test_export_format_bound_fallback_is_explicit_policy_not_evidence(self):
        result = estimate_operation(
            {"execution_history": []}, spec(8894037), {}, NOW,
            kind="export", result_bytes=100000,
            policy={"transport_format_bound_fallback": True},
        )
        self.assertEqual(result["status"], "estimated")
        transport = 100000 * 4.0 + 16384  # documented bound: no extra 25%
        self.assertAlmostEqual(
            result["cost_estimate_credits"], transport / 1_000_000.0 * 10.0
        )
        export = result["evidence"]["export"]
        self.assertEqual(export["method"], "declared_format_bound_fallback")
        self.assertIn("not ratio evidence", export["fallback"])
        self.assertEqual(export["basis"], "transport_estimated_not_metered")

    def test_export_ratio_evidence_is_never_taken_from_other_queries(self):
        state = deepcopy(self.RATIO_STATE)
        for row in state["autonomous_cost_samples"]:
            row["query_id"] = 8894132
        result = estimate_operation(
            state, spec(8894037, shape="2"), {"lookback_hours": 2}, NOW,
            kind="export", result_bytes=100000,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(
            any(r.startswith("no_export_ratio_evidence") for r in result["reasons"])
        )

    def test_export_accepts_vetted_serialized_bytes_ratio(self):
        state = deepcopy(self.RATIO_STATE)
        for row in state["autonomous_cost_samples"]:
            row["export_wire_bytes"] = None
            row["serialized_bytes"] = 188000
        result = estimate_operation(
            state, spec(8894037, shape="2"), {"lookback_hours": 2}, NOW,
            kind="export", result_bytes=100000,
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(
            [entry["provenance"] for entry in
             result["evidence"]["export"]["ratio_samples"]],
            ["vetted_serialized_bytes", "vetted_serialized_bytes"],
        )
        self.assertEqual(
            result["evidence"]["export"]["basis"],
            "transport_estimated_not_metered",
        )


class RemainingCalendarForecastTests(unittest.TestCase):
    """Mean-based expected forecast per cadence (ceil seconds); the observed
    maximum rides separately as risk."""

    def _entry_result(self, costs, hours_ago=1, query_id=8894037):
        history = history_samples(
            query_id, {"lookback_hours": 2}, costs, hours_ago=hours_ago
        )
        return estimate_operation(
            {"execution_history": history}, spec(query_id), {"lookback_hours": 2}, NOW
        )

    def test_forecast_builds_expected_mean_runs_and_separate_observedmax_risk(self):
        transfers = self._entry_result([0.1, 0.1, 5.0])
        current = self._entry_result([0.2, 0.2])
        forecast = forecast_remaining_calendar(
            [
                {"key": "transfers", "cadence_seconds": 600,
                 "estimate": transfers},
                {"key": "current", "cadence_minutes": 30, "estimate": current},
            ],
            now_utc=NOW, period_end_utc="2026-10-20T13:05:00Z",
        )
        self.assertEqual(forecast["entries"][0]["runs"], 7)
        self.assertEqual(forecast["entries"][1]["runs"], 3)
        expected_transfers = 7 * statistics.fmean([0.1, 0.1, 5.0])
        expected_current = 3 * statistics.fmean([0.2, 0.2])
        self.assertAlmostEqual(
            forecast["expected_total_credits"],
            expected_transfers + expected_current,
        )
        self.assertAlmostEqual(
            forecast["observed_max_risk_total_credits"], 7 * 5.0 + 3 * 0.2
        )
        self.assertGreater(
            forecast["observed_max_risk_total_credits"],
            forecast["expected_total_credits"],
        )
        self.assertTrue(forecast["exposure_is_fully_known"])
        self.assertEqual(forecast["unknown_entries"], [])
        self.assertIn("outlier-INCLUSIVE", forecast["mean_basis_note"])

    def test_forecast_surfaces_unknown_entries_and_blocks_exposure(self):
        unknown = estimate_operation(
            {"execution_history": []}, spec(8894037), {"lookback_hours": 2}, NOW
        )
        known = self._entry_result([0.2, 0.2])
        forecast = forecast_remaining_calendar(
            [
                {"key": "gap", "cadence_minutes": 30, "estimate": unknown},
                {"key": "ok", "cadence_seconds": 600, "estimate": known},
            ],
            now_utc=NOW, period_end_utc="2026-10-20T13:05:00Z",
        )
        self.assertFalse(forecast["exposure_is_fully_known"])
        self.assertEqual(
            [entry["key"] for entry in forecast["unknown_entries"]], ["gap"]
        )
        self.assertEqual(
            forecast["unknown_entries"][0]["reasons"],
            unknown["reasons"],
        )
        self.assertEqual(forecast["entries"][0]["status"], "unknown")
        self.assertEqual(forecast["entries"][0]["runs"], 3)

    def test_forecast_rejects_nonpositive_cadence_and_bad_estimates(self):
        known = self._entry_result([0.2, 0.2])
        with self.assertRaises(EstimatorInputError):
            forecast_remaining_calendar(
                [{"key": "x", "cadence_seconds": 0, "estimate": known}],
                now_utc=NOW, period_end_utc="2026-10-20T13:05:00Z",
            )
        with self.assertRaises(EstimatorInputError):
            forecast_remaining_calendar(
                [{"key": "x", "estimate": known}],
                now_utc=NOW, period_end_utc="2026-10-20T13:05:00Z",
            )
        with self.assertRaises(EstimatorInputError):
            forecast_remaining_calendar(
                [{"key": "x", "cadence_seconds": 600, "estimate": {"status": "??"}}],
                now_utc=NOW, period_end_utc="2026-10-20T13:05:00Z",
            )
        with self.assertRaises(EstimatorInputError):
            forecast_remaining_calendar(
                [], now_utc="2026-10-20", period_end_utc=NOW,
            )

    def test_forecast_disabled_entries_and_elapsed_periods_charge_zero(self):
        known = self._entry_result([0.2, 0.2])
        forecast = forecast_remaining_calendar(
            [
                {"key": "off", "cadence_minutes": 30, "estimate": known,
                 "enabled": False},
                {"key": "due", "cadence_seconds": 600, "estimate": known},
            ],
            now_utc="2026-10-20T13:30:00Z",
            period_end_utc="2026-10-20T13:05:00Z",
        )
        self.assertEqual(forecast["remaining_seconds_before_renewal"], 0.0)
        self.assertEqual(forecast["entries"][0]["status"], "disabled")
        self.assertEqual(forecast["entries"][0]["runs"], 0)
        self.assertEqual(forecast["entries"][1]["runs"], 0)
        self.assertEqual(forecast["expected_total_credits"], 0.0)


APPROVED_BATCH_FIXTURES = ROOT / "tests" / "fixtures" / "approved_batch_20261020"


def approved_day_family_samples():
    """The approved 8894038 single-UTC-day repair family read from the ACTUAL
    approved-batch fixtures: real execution ids, real captured costs
    (resultMetadata.executionCostCredits arrives as string credits and is
    numeric-normalized here) and the receipt's real capture instants. The
    fixtures carry no engine attribution, so the rows stay engine-less and
    attribute to the policy default engine ('medium', the verified legacy
    pipeline tier); ``query_version`` 4 maps to ``shape_version`` 4."""
    receipt = json.loads(
        (APPROVED_BATCH_FIXTURES / "source_acceptance_receipt.json").read_text()
    )
    rows = []
    for entry in receipt["provider_sources"]:
        if entry.get("query_id") != 8894038:
            continue
        result = json.loads(
            (APPROVED_BATCH_FIXTURES / f"{entry['operation']}.json").read_text()
        )
        metadata = result["resultMetadata"]
        rows.append({
            "execution_id": result["executionId"],
            "query_id": entry["query_id"],
            "query_parameters": dict(entry["query_parameters"]),
            "shape_version": entry["query_version"],
            "execution_cost_credits": float(
                Decimal(metadata["executionCostCredits"])
            ),
            "result_received_at_utc": entry["received_at_utc"],
        })
    return rows


class CalendarTranslationOneDayTests(unittest.TestCase):
    """The 8894038-style single-UTC-day family: when no exact sample exists,
    same query+engine+shape samples of OTHER integer day offsets (0..60)
    pool with no extra scaling via calendar_translation_one_day; multi-day
    and malformed offsets never pool and malformed types refuse."""

    def test_approved_batch_actual_fixture_family_quotes_new_day(self):
        rows = approved_day_family_samples()
        self.assertEqual(len(rows), 5)
        result = estimate_operation(
            {"execution_history": rows},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 5, "newest_days_ago": 5}, NOW,
            policy={"default_engine": "medium",
                    "transport_format_bound_fallback": True},
        )
        assert_quote_shape(self, result)
        self.assertEqual(result["status"], "estimated")
        costs = [1.435, 0.285, 0.378, 0.158, 0.299]
        quote = max(
            max(costs),
            statistics.fmean(costs) + 3.0 * statistics.stdev(costs),
        )
        self.assertAlmostEqual(result["cost_estimate_credits"], quote)
        self.assertAlmostEqual(result["expected_credits"], statistics.fmean(costs))
        self.assertAlmostEqual(
            result["evidence"]["cost_observed_max_credits"], 1.435
        )
        self.assertEqual(
            result["evidence"]["pool_counts"]["family_usable"], 5
        )
        self.assertEqual(
            result["evidence"]["pool_counts"]["transfer_usable"], 0
        )
        self.assertIn(
            "calendar_translation_one_day", result["evidence"]["method"]
        )
        translation = result["evidence"]["calendar_translation"]
        self.assertEqual(translation["requested_day_offset_days"], 5)
        # The ORIGINAL offsets of every pooled sample are retained.
        self.assertEqual(
            translation["sample_day_offsets_days"], [4, 3, 2, 1, 0]
        )
        self.assertEqual(
            result["evidence"]["sample_execution_ids"],
            [row["execution_id"] for row in rows],
        )

    def test_production_runtime_config_quotes_legacy_history_rows(self):
        # Production forecast parity: execution_history rows without a
        # `performance` field attribute to the policy default engine
        # ('medium', the verified legacy pipeline tier); sample parameters
        # are only what each row itself records. The legacy 1-day family
        # costs 0.178..0.342 quote a fresh day request.
        legacy = history_samples(
            8894038,
            {"oldest_days_ago": 8, "newest_days_ago": 8},
            [0.178, 0.342, 0.2836],
            hours_ago=26, engine=None, shape="4",
        )
        rows = approved_day_family_samples() + legacy
        result = estimate_operation(
            {"execution_history": rows},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 9, "newest_days_ago": 9}, NOW,
            policy={"default_engine": "medium",
                    "transport_format_bound_fallback": True},
        )
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["evidence"]["pool_counts"]["family_usable"], 8)
        # The new-batch outlier is never dropped: the observed max is 1.435.
        self.assertAlmostEqual(
            result["evidence"]["cost_observed_max_credits"], 1.435
        )
        self.assertAlmostEqual(result["evidence"]["cost_min_credits"], 0.158)
        self.assertIn(
            "calendar_translation_one_day", result["evidence"]["method"]
        )

    def test_exact_day_params_are_preferred_over_family_translation(self):
        rows = approved_day_family_samples()
        exact = history_samples(
            8894038, {"oldest_days_ago": 1, "newest_days_ago": 1},
            [0.409], hours_ago=1, shape="4", ids=["exact1"],
        )
        result = estimate_operation(
            {"execution_history": rows + exact},
            spec(8894038, shape="4"),
            {"oldest_days_ago": 1, "newest_days_ago": 1}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "estimated")
        # Exact params win: the fixture offset-1 sample AND the fresh exact
        # sample quote as one exact-identity pool — no translation.
        self.assertIn("exact_identity", result["evidence"]["method"])
        self.assertNotIn(
            "calendar_translation_one_day", result["evidence"]["method"]
        )
        self.assertEqual(
            result["evidence"]["sample_execution_ids"],
            ["01M5CAA3NCH6P0H3XRMNMD46QK", "exact1"],
        )
        self.assertIsNone(result["evidence"]["calendar_translation"])

    def test_multi_day_unequal_offset_requests_never_pool(self):
        rows = approved_day_family_samples()
        result = estimate_operation(
            {"execution_history": rows},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 1, "newest_days_ago": 4}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["cost_estimate_credits"])
        self.assertEqual(result["evidence"]["pool_counts"]["family_usable"], 0)
        self.assertTrue(
            any("no semantically justified transfer axis" in reason
                for reason in result["reasons"])
        )

    def test_negative_bool_and_fractional_day_offsets_refuse(self):
        rows = approved_day_family_samples()
        state = {"execution_history": rows}
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                state, spec(8894038, engine=None, shape="4"),
                {"oldest_days_ago": -1, "newest_days_ago": -1}, NOW,
                policy={"default_engine": "medium"},
            )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                state, spec(8894038, engine=None, shape="4"),
                {"oldest_days_ago": True, "newest_days_ago": True}, NOW,
                policy={"default_engine": "medium"},
            )
        # The same malformed types refuse in SAMPLE evidence.
        bad_bool = dict(rows[0], query_parameters={
            "oldest_days_ago": False, "newest_days_ago": False})
        bad_negative = dict(rows[0], query_parameters={
            "oldest_days_ago": -2, "newest_days_ago": -2})
        for bad in (bad_bool, bad_negative):
            with self.assertRaises(EstimatorInputError):
                estimate_operation(
                    {"execution_history": [bad]},
                    spec(8894038, engine=None, shape="4"),
                    {"oldest_days_ago": 1, "newest_days_ago": 1}, NOW,
                    policy={"default_engine": "medium"},
                )

    def test_offsets_outside_0_60_leave_the_family(self):
        rows = approved_day_family_samples()
        result = estimate_operation(
            {"execution_history": rows},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 61, "newest_days_ago": 61}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(
            any(r.startswith("requested_day_offset_outside_"
                             "single_day_family_domain")
                for r in result["reasons"])
        )
        # An in-sample offset beyond the domain is skipped with a reason.
        old = dict(rows[0], query_parameters={
            "oldest_days_ago": 240, "newest_days_ago": 240})
        result = estimate_operation(
            {"execution_history": [old]},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 1, "newest_days_ago": 1}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(
            result["evidence"]["pool_counts"]["family_blocked"], 1
        )
        self.assertTrue(
            any("sample_day_offset_outside_family_domain" in reason
                for reason in result["reasons"])
        )

    def test_day_family_is_disabled_by_policy_and_never_mixes_queries(self):
        rows = approved_day_family_samples()
        result = estimate_operation(
            {"execution_history": rows},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 5, "newest_days_ago": 5}, NOW,
            policy={"default_engine": "medium",
                    "day_family_transfer": {"enabled": False}},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn(
            "day_family_transfer_disabled_by_policy", result["reasons"]
        )
        # Identity is never mixed: day-absolute params of ANOTHER query do
        # not see the 8894038 family.
        other = [dict(row, query_id=9999999) for row in rows]
        result = estimate_operation(
            {"execution_history": other},
            spec(8894038, engine=None, shape="4"),
            {"oldest_days_ago": 5, "newest_days_ago": 5}, NOW,
            policy={"default_engine": "medium"},
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("no_samples_for_identity", result["reasons"])


class PolicySchemaAndTransferOverrideTests(unittest.TestCase):
    """The documented policy surface is exported and enforced, and the hot
    query's 1..168h extension works ONLY via an explicit override."""

    def test_policy_schema_exports_the_exact_documented_surface(self):
        schema = estimates.POLICY_SCHEMA
        for key in (
            "default_engine", "transport_format_bound_fallback",
            "max_sample_age_hours", "aging_free_hours",
            "aging_margin_fraction", "single_sample_margin",
            "variance_uplift_sigma", "lookback_transfer",
            "day_family_transfer", "export_rate_per_mb",
            "overhead_transport_fraction", "fixed_transport_overhead_bytes",
            "format_bound_multiplier",
        ):
            self.assertIn(key, schema)
            self.assertIn("type", schema[key])
            self.assertIn("default", schema[key])
        self.assertEqual(
            schema["transport_format_bound_fallback"]["type"], "bool"
        )
        self.assertIs(
            schema["transport_format_bound_fallback"]["default"], False
        )
        self.assertEqual(schema["default_engine"]["default"], None)
        self.assertEqual(
            schema["lookback_transfer"]["default"],
            {"enabled": True, "default_max_hours": 48.0,
             "query_max_hours": {"8894132": 168.0}},
        )
        self.assertEqual(
            schema["day_family_transfer"]["default"],
            {"enabled": True, "min_offset_days": 0, "max_offset_days": 60},
        )

    def test_runtime_policy_types_are_enforced(self):
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []}, spec(8894037), {}, NOW,
                policy={"transport_format_bound_fallback": {"x": 1}},
            )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []}, spec(8894037), {}, NOW,
                policy={"default_engine": True},
            )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []}, spec(8894037), {}, NOW,
                policy={"day_family_transfer": {"bogus": 1}},
            )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []}, spec(8894037), {}, NOW,
                policy={"day_family_transfer": {"min_offset_days": True}},
            )
        with self.assertRaises(EstimatorInputError):
            estimate_operation(
                {"execution_history": []}, spec(8894037), {}, NOW,
                policy={"day_family_transfer": {"min_offset_days": 5,
                                                "max_offset_days": 2}},
            )

    def test_hot_query_120h_requires_explicit_override_and_scales(self):
        history = history_samples(
            8894037, {"lookback_hours": 24}, [0.234, 0.256], hours_ago=2
        )
        state = {"execution_history": history}
        # Default: the hot query's documented domain stays 1..48 h.
        without_override = estimate_operation(
            state, spec(8894037), {"lookback_hours": 120}, NOW,
        )
        self.assertEqual(without_override["status"], "unknown")
        self.assertTrue(
            any(r.startswith("requested_lookback_outside_transfer_domain")
                for r in without_override["reasons"])
        )
        # Extension beyond 48h happens ONLY per explicit policy override,
        # and the documented risk scaling still applies per sample.
        overridden = estimate_operation(
            state, spec(8894037), {"lookback_hours": 120}, NOW,
            policy={"lookback_transfer": {"query_max_hours": {"8894037": 168.0}}},
        )
        self.assertEqual(overridden["status"], "estimated")
        self.assertEqual(
            overridden["evidence"]["lookback_scaling"]["scaling_factors"],
            [5.0, 5.0],
        )
        scaled = [0.234 * 5.0, 0.256 * 5.0]
        self.assertAlmostEqual(
            overridden["cost_estimate_credits"],
            max(
                max(scaled),
                statistics.fmean(scaled)
                + 3.0 * statistics.stdev(scaled),
            ),
        )

    def test_query_max_hours_override_merges_over_documented_defaults(self):
        batch = [batch_op("bbb", 8894132, {"lookback_hours": 120}, 0.355,
                          shape="4")]
        # Extending 8894037 does NOT quietly remove the documented BBB domain.
        result = estimate_operation(
            {"approved_recovery_batches": batch},
            spec(8894132, shape="4"), {"lookback_hours": 168}, NOW,
            policy={"lookback_transfer": {"query_max_hours": {"8894037": 168.0}}},
        )
        self.assertEqual(result["status"], "estimated")
        self.assertAlmostEqual(
            result["cost_estimate_credits"], 0.355 * (168.0 / 120.0) * 2.0
        )


if __name__ == "__main__":
    unittest.main()
