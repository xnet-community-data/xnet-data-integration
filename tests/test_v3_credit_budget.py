import json
import math
import statistics
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v3_credit_budget as budget
from v3_credit_budget import (
    BudgetInputError,
    DEFAULT_EXPORT_CREDITS_PER_MB,
    DEFAULT_GUARD_CREDITS,
    DEFAULT_MIN_COMPARABLE_SAMPLES,
    DEFAULT_MINIMUM_RESERVE_CREDITS,
    DEFAULT_SAMPLE_MAX_AGE_HOURS,
    DEFAULT_VARIANCE_UPLIFT_SIGMA,
    budget_admission,
    canon_parameters,
    execution_admission,
    export_liability_estimate,
    group_samples,
    outstanding_liabilities,
    parse_instant,
    sample_estimate,
    scheduled_forecast,
    select_active_period,
)

NOW = "2026-10-16T22:00:00Z"
FORECAST_NOW = "2026-10-31T12:00:00Z"  # 302400 s before the exact renewal instant
PERIOD_START = "2026-10-04T16:35:49Z"
PERIOD_END = "2026-11-04T00:00:00Z"
CURRENT_USAGE = {
    "credits_used": 1798.673,
    "credits_included": 2500,
    "period_start_utc": PERIOD_START,
    "period_end_utc": PERIOD_END,
}
RECEIVED_TARGET = 2500  # config monthly_target_credits


def usage(**overrides):
    value = dict(CURRENT_USAGE)
    value.update(overrides)
    return value


def history_cost_samples(query_id, params, costs, *, ended_hours_ago, perf=None,
                         ids=None, bytes_=0, credits=None, shape=None,
                         base_utc=None):
    """Build tracker-shaped execution_history-derived cost samples.

    Anchors sit ``ended_hours_ago`` hours before ``base_utc`` (default: NOW)
    so fixtures stay fresh against whichever evaluation instant the caller
    uses — e.g. renewal-window tests anchored at FORECAST_NOW."""
    base = parse_instant(base_utc or NOW, "base_utc")
    rows = []
    for index, cost in enumerate(costs):
        anchor = base - timedelta(hours=ended_hours_ago)
        row = {
            "query_id": query_id,
            "execution_cost_credits": cost,
            "execution_ended_at": anchor.isoformat().replace("+00:00", "Z"),
            "execution_id": (ids[index] if ids else f"e{index}"),
        }
        if params is not None:
            row["query_parameters"] = params
        if perf is not None:
            row["performance"] = perf
        if shape is not None:
            row["shape_version"] = shape
        if bytes_ is not None:
            row["result_bytes"] = bytes_
        if credits is not None:
            row["estimated_export_credits"] = credits
        rows.append(row)
    return rows


def forecast_detail_by_key(report):
    return {row["key"]: row for row in report["forecast"]["detail"]}


class CurrentGuardStateTests(unittest.TestCase):
    """Live shape: usage 1798.673 in a period ending 2026-11-04T00:00:00Z,
    guard 1800 enforced alongside the 2500 monthly target."""

    def test_current_state_blocks_on_guard_reached(self):
        report = budget_admission(CURRENT_USAGE, NOW, monthly_target_credits=RECEIVED_TARGET)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            any(b.startswith("monthly_spend_guard_reached") for b in report["blockers"])
        )
        self.assertEqual(report["totals"]["limit_credits"], 1800.0)
        self.assertAlmostEqual(report["totals"]["usage_plus_reserve"], 1813.673)
        self.assertIn("guard 1800.000000", report["totals"]["limit_binding"][0])
        # The target ceiling is enforced, and the guard is the binding bound.
        self.assertEqual(
            report["totals"]["limit_binding"],
            ["guard 1800.000000"],
        )

    def test_guard_default_is_1800_and_boundary_holds(self):
        self.assertEqual(DEFAULT_GUARD_CREDITS, 1800.0)
        self.assertEqual(DEFAULT_MINIMUM_RESERVE_CREDITS, 15.0)
        self.assertEqual(DEFAULT_EXPORT_CREDITS_PER_MB, 10.0)
        self.assertEqual(DEFAULT_MIN_COMPARABLE_SAMPLES, 3)
        self.assertEqual(
            budget_admission(usage(credits_used=1784.0), NOW)["status"], "admitted"
        )
        # 1785 + 15 reserve == 1800 >= guard: reached, blocked.
        self.assertEqual(
            budget_admission(usage(credits_used=1785.0), NOW)["status"], "blocked"
        )

    def test_no_dependencies_leaves_headroom_admission(self):
        report = budget_admission(
            usage(credits_used=1600.0), NOW, monthly_target_credits=RECEIVED_TARGET
        )
        self.assertEqual(report["status"], "admitted")
        self.assertEqual(report["blockers"], [])
        self.assertAlmostEqual(report["totals"]["headroom_after_reserve"], 185.0)
        self.assertEqual(
            report["current_usage"]["period_end_utc"], "2026-11-04T00:00:00Z"
        )


class TargetCeilingTests(unittest.TestCase):
    """monthly_target_credits 2500 is enforced, not just reported; so is the
    reconciliation buffer of 150 credits below the target."""

    def test_target_binds_when_guard_is_raised(self):
        report = budget_admission(
            usage(credits_used=1900.0),
            NOW,
            guard_credits=2400.0,
            monthly_target_credits=2000.0,
        )
        self.assertEqual(report["status"], "admitted")
        self.assertEqual(report["totals"]["limit_credits"], 2000.0)
        self.assertEqual(
            report["totals"]["limit_binding"], ["monthly target 2000.000000"]
        )
        blocked = budget_admission(
            usage(credits_used=1990.0),
            NOW,
            guard_credits=2400.0,
            monthly_target_credits=2000.0,
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(
            any("min(monthly target 2000.000000)" in b for b in blocked["blockers"])
        )

    def test_minimum_headroom_below_target_is_enforced(self):
        base = dict(guard_credits=2500.0, monthly_target_credits=2500.0,
                    minimum_headroom_below_target=150.0)
        admitted = budget_admission(usage(credits_used=2200.0), NOW, **base)
        self.assertEqual(admitted["status"], "admitted")
        self.assertEqual(admitted["blockers"], [])
        blocked = budget_admission(
            usage(credits_used=2200.0),
            NOW,
            scheduled=[
                {"key": "planned", "query_id": 8894037, "cadence_minutes": 30,
                 "export_policy": "none"}
            ],
            samples=history_cost_samples(
                8894037, None,
                [1.0, 1.0, 1.0], ended_hours_ago=1,
            ),
            default_sample_performance="medium",
            **base,
        )
        # deps = 168 runs x 1.0 covers way past the buffer: blocked.
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(
            any(b.startswith("minimum_target_headroom_exceeded") for b in blocked["blockers"])
        )


class UpperEnvelopeEstimateTests(unittest.TestCase):
    """The max-observed estimate alone is not a defensible conservative upper:
    at least 3 comparable recent samples with a variance uplift, an explicit
    estimate and never a guarantee."""

    def test_estimate_is_uplifted_envelope_not_raw_max(self):
        grouped = group_samples(
            history_cost_samples(8894037, {"lookback_hours": 2}, [1.04, 0.9, 1.4],
                                 ended_hours_ago=2),
            default_performance="medium",
        )
        estimate = sample_estimate(
            grouped, 8894037, {"lookback_hours": 2}, now_utc=NOW, performance="medium"
        )
        expected = max(
            1.4,
            statistics.fmean([1.04, 0.9, 1.4]) + DEFAULT_VARIANCE_UPLIFT_SIGMA
            * statistics.stdev([1.04, 0.9, 1.4]),
        )
        self.assertEqual(estimate["status"], "sampled")
        self.assertAlmostEqual(estimate["estimate"], expected, places=9)
        self.assertGreater(estimate["estimate"], 1.4)
        self.assertEqual(estimate["evidence"]["comparable_count"], 3)
        self.assertEqual(
            estimate["identity"]["parameters"],
            canon_parameters({"lookback_hours": 2}),
        )

    def test_flat_samples_estimate_equals_observed_max(self):
        grouped = group_samples(
            history_cost_samples(8894132, None, [0.42, 0.42, 0.42], ended_hours_ago=4),
            default_performance="medium",
        )
        estimate = sample_estimate(grouped, 8894132, None, now_utc=NOW, performance="medium")
        self.assertEqual(estimate["status"], "sampled")
        self.assertAlmostEqual(estimate["estimate"], 0.42)
        self.assertAlmostEqual(estimate["evidence"]["cost_mean"], 0.42)

    def test_fewer_than_three_samples_is_unknown(self):
        grouped = group_samples(
            history_cost_samples(8894037, {"lookback_hours": 2}, [1.04, 1.1],
                                 ended_hours_ago=1),
            default_performance="medium",
        )
        estimate = sample_estimate(grouped, 8894037, {"lookback_hours": 2}, now_utc=NOW, performance="medium")
        self.assertEqual(estimate["status"], "unknown")
        self.assertIsNone(estimate["estimate"])
        self.assertTrue(
            any(r.startswith("insufficient_comparable_samples") for r in estimate["reasons"])
        )

    def test_stale_and_undated_samples_are_not_comparable(self):
        fresh = history_cost_samples(8894037, {"lookback_hours": 2}, [1.0, 1.0],
                                     ended_hours_ago=2)
        stale = history_cost_samples(8894037, {"lookback_hours": 2}, [0.2],
                                     ended_hours_ago=200)
        undated = [{"query_id": 8894037, "query_parameters": {"lookback_hours": 2},
                    "execution_cost_credits": 0.5}]
        grouped = group_samples(fresh + stale + undated, default_performance="medium")
        estimate = sample_estimate(grouped, 8894037, {"lookback_hours": 2}, now_utc=NOW, performance="medium")
        self.assertEqual(estimate["status"], "unknown")
        self.assertEqual(estimate["evidence"]["stale_count"], 1)
        self.assertEqual(estimate["evidence"]["missing_timestamp_count"], 1)
        self.assertEqual(estimate["evidence"]["comparable_count"], 2)
        # Stale is a policy knob; tightened policy keeps the same block.
        policy = sample_estimate(grouped, 8894037, {"lookback_hours": 2}, now_utc=NOW,
                                 performance="medium", max_age_hours=24.0)
        self.assertEqual(policy["status"], "unknown")

    def test_shape_version_mismatch_or_unknown_blocks(self):
        shaped = history_cost_samples(8894037, {"lookback_hours": 2}, [1.0, 1.0, 1.0],
                                      ended_hours_ago=1, shape="2")
        grouped = group_samples(shaped, default_performance="medium")
        matching = sample_estimate(grouped, 8894037, {"lookback_hours": 2},
                                   now_utc=NOW, performance="medium", shape_version="2")
        self.assertEqual(matching["status"], "sampled")
        unknown = sample_estimate(grouped, 8894037, {"lookback_hours": 2},
                                  now_utc=NOW, performance="medium", shape_version="1")
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["evidence"]["shape_mismatch_count"], 3)


class ScheduledForecastTests(unittest.TestCase):
    """Remaining-window forecast covers compute AND export, at the observed
    maximum required export bytes under the configured billing rate."""

    SAMPLES = (
        history_cost_samples(8894037, {"lookback_hours": 2}, [1.04, 1.04, 1.04],
                             ended_hours_ago=2, base_utc=FORECAST_NOW)
        + history_cost_samples(8894132, None, [0.42, 0.42, 0.42],
                               ended_hours_ago=6, base_utc=FORECAST_NOW)
        + history_cost_samples(8894185, None, [1.5, 1.5, 1.5],
                               ended_hours_ago=1, bytes_=None, base_utc=FORECAST_NOW)
        + history_cost_samples(8899999, None, [2.0, 2.0, 2.0],
                               ended_hours_ago=3, bytes_=200_000,
                               base_utc=FORECAST_NOW)
    )
    ENTRIES = [
        {"key": "xnet_transfers", "query_id": 8894037,
         "query_parameters": {"lookback_hours": 2},
         "cadence_minutes": 30,
         "export_policy": "none"},
        {"key": "bbb_dex", "query_id": 8894132, "cadence_minutes": 1440,
         "export_policy": "none"},
        {"key": "presentation:current", "query_id": 8894185, "cadence_minutes": 15,
         "export_policy": "none"},
        {"key": "manifest:daily", "query_id": 8899999, "cadence_minutes": 1440,
         "export_policy": "observed_max"},
    ]

    def test_forecast_counts_compute_and_export_runs_to_renewal(self):
        report = budget_admission(
            usage(credits_used=900.0),
            FORECAST_NOW,
            samples=self.SAMPLES,
            scheduled=self.ENTRIES,
            default_sample_performance="medium",
        )
        detail = forecast_detail_by_key(report)
        self.assertEqual(report["status"], "admitted")
        self.assertEqual(detail["xnet_transfers"]["runs"], 168)  # 302400 s / 1800 s
        self.assertAlmostEqual(detail["xnet_transfers"]["compute_estimates"], 174.72)
        self.assertEqual(detail["bbb_dex"]["runs"], 4)
        self.assertEqual(detail["presentation:current"]["runs"], 336)
        self.assertEqual(detail["manifest:daily"]["runs"], 4)
        # Export: flat, quorum-satisfying wire evidence (200000 bytes x3 ->
        # 2.0 credits/run at 10 credits/MB) drives the sample-backed quote.
        self.assertAlmostEqual(detail["manifest:daily"]["export_cost_per_run"], 2.0)
        self.assertAlmostEqual(detail["manifest:daily"]["export_estimates"], 8.0)
        self.assertAlmostEqual(detail["manifest:daily"]["export_bytes_max"], 200_000)
        self.assertAlmostEqual(
            report["totals"]["scheduled_forecast_credits"],
            174.72 + 1.68 + 504.0 + (4 * 2.0) + 8.0,
        )

    def test_export_quote_is_sample_backed_envelope_not_a_byte_cap(self):
        export_samples = history_cost_samples(
            8899999, None, [2.0, 2.0, 2.0], ended_hours_ago=3, bytes_=250_000,
            base_utc=FORECAST_NOW,
        )
        grouped = group_samples(export_samples, default_performance="medium")
        forecast = scheduled_forecast(
            [{"key": "manifest:daily", "query_id": 8899999, "cadence_minutes": 1440,
              "export_policy": "observed_max"}],
            grouped,
            now_utc=FORECAST_NOW,
            period_end_utc=PERIOD_END,
            default_performance="medium",
        )
        row = forecast["detail"][0]
        self.assertAlmostEqual(row["export_cost_per_run"], 2.5)  # 250000/1e6*10
        self.assertEqual(row["export_bytes_max"], 250_000)
        self.assertEqual(row["sample_count"], 3)
        self.assertAlmostEqual(row["compute_estimates"], 4 * 2.0)

    def test_export_quote_applies_variance_uplift(self):
        varying = history_cost_samples(
            8899999, None, [2.0, 2.0, 2.0], ended_hours_ago=3, bytes_=250_000,
            base_utc=FORECAST_NOW,
        )
        for cost, byte_value in zip(varying, (250_000, 150_000, 100_000)):
            cost["result_bytes"] = byte_value
        grouped = group_samples(varying, default_performance="medium")
        forecast = scheduled_forecast(
            [{"key": "manifest:daily", "query_id": 8899999, "cadence_minutes": 1440,
              "export_policy": "observed_max"}],
            grouped,
            now_utc=FORECAST_NOW,
            period_end_utc=PERIOD_END,
            default_performance="medium",
        )
        values = (2.5, 1.5, 1.0)  # bytes scaled at 10 credits/MB
        expected = max(
            max(values),
            statistics.fmean(values) + 3.0 * statistics.stdev(values),
        )
        self.assertAlmostEqual(
            forecast["detail"][0]["export_cost_per_run"], expected, places=9
        )
        self.assertGreater(
            forecast["detail"][0]["export_cost_per_run"], 2.5
        )

    def test_export_evidence_below_quorum_blocks(self):
        # Three comparable compute samples, but only ONE carries export
        # wire evidence -> export quorum unmet -> unknown export cost.
        samples = history_cost_samples(
            8899999, None, [2.0, 2.0, 2.0], ended_hours_ago=3, bytes_=None,
            base_utc=FORECAST_NOW,
        )
        samples[0]["result_bytes"] = 250_000
        report = budget_admission(
            usage(credits_used=900.0),
            FORECAST_NOW,
            samples=samples,
            scheduled=[{"key": "manifest:daily", "query_id": 8899999,
                        "cadence_minutes": 1440,
                        "export_policy": "observed_max"}],
            default_sample_performance="medium",
        )
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            any("unknown_scheduled_cost:manifest:daily" in b
                and "(query 8899999, component export)" in b
                and "below the comparable-sample quorum (1/3)" in b
                for b in report["blockers"])
        )

    def test_unknown_export_cost_blocks_when_evidence_is_not_declared(self):
        no_bytes = history_cost_samples(
            8899999, None, [2.0, 2.0, 2.0], ended_hours_ago=3, bytes_=None
        )
        report = budget_admission(
            usage(credits_used=900.0),
            NOW,
            samples=no_bytes,
            scheduled=[
                {"key": "manifest:daily", "query_id": 8899999, "cadence_minutes": 1440,
                 "export_policy": "observed_max"}
            ],
            default_sample_performance="medium",
        )
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            any("unknown_scheduled_cost:manifest:daily" in b
                and "(query 8899999, component export)" in b
                for b in report["blockers"])
        )

    def test_export_policy_none_is_exempt_from_evidence(self):
        no_bytes = history_cost_samples(
            8894185, None, [0.4, 0.4, 0.4], ended_hours_ago=1, bytes_=None
        )
        report = budget_admission(
            usage(credits_used=900.0),
            NOW,
            samples=no_bytes,
            scheduled=[
                {"key": "presentation:current", "query_id": 8894185,
                 "cadence_minutes": 15, "export_policy": "none"}
            ],
            default_sample_performance="medium",
        )
        self.assertEqual(report["status"], "admitted")

    def test_overspending_known_forecast_blocks(self):
        report = budget_admission(
            usage(credits_used=1600.0),
            NOW,
            samples=self.SAMPLES,
            scheduled=self.ENTRIES,
            default_sample_performance="medium",
        )
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            any(b.startswith("forecast_exceeds_guard_headroom") for b in report["blockers"])
        )

    def test_disabled_entries_are_skipped_without_samples(self):
        report = budget_admission(
            usage(credits_used=1600.0),
            NOW,
            scheduled=[
                {"key": "off", "query_id": 8894037, "cadence_minutes": 30,
                 "enabled": False}
            ],
            default_sample_performance="medium",
        )
        self.assertEqual(report["status"], "admitted")
        detail = forecast_detail_by_key(report)
        self.assertEqual(detail["off"]["status"], "skipped")
        self.assertEqual(report["forecast"]["unknown_keys"], [])

    def test_unsampled_compute_entry_blocks_while_due(self):
        report = budget_admission(
            usage(credits_used=1600.0),
            NOW,
            scheduled=[
                {"key": "xnet_transfers", "query_id": 8894037,
                 "cadence_minutes": 30, "export_policy": "none"}
            ],
            default_sample_performance="medium",
        )
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            any(b.startswith("unknown_scheduled_cost:xnet_transfers") and "component compute" in b
                for b in report["blockers"])
        )


class ExactRenewalInstantTests(unittest.TestCase):
    """The renewal boundary is an exact UTC instant, never a truncated date."""

    def test_exact_end_is_respected_at_boundary(self):
        fresh = history_cost_samples(
            8894132, None, [0.42, 0.42, 0.42],
            ended_hours_ago=4, base_utc="2026-10-30T00:00:00Z",
        )
        report = budget_admission(
            usage(credits_used=1600.0),
            "2026-10-30T00:00:00Z",
            scheduled=[{"key": "daily", "query_id": 8894132, "cadence_minutes": 1440,
                        "export_policy": "none"}],
            samples=fresh,
            default_sample_performance="medium",
        )
        detail = forecast_detail_by_key(report)
        self.assertEqual(detail["daily"]["runs"], 5)
        self.assertAlmostEqual(detail["daily"]["compute_estimates"], 2.1)
        self.assertEqual(report["status"], "admitted")

    def test_time_right_at_renewal_blocks_until_usage_refreshed(self):
        report = budget_admission(usage(credits_used=1600.0), PERIOD_END)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("billing_period_ended", report["blockers"])

    def test_date_only_end_soft_blocks_without_raising(self):
        report = budget_admission(usage(period_end_utc="2026-11-04"), NOW)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("period_end_not_exact_utc", report["blockers"])
        self.assertIsNone(report["forecast"])

    def test_date_only_end_in_billing_payload_blocks_without_active_period(self):
        payload = {
            "billing_periods": [
                {
                    "start_date": PERIOD_START,
                    "end_date": "2026-11-04",
                    "credits_used": 100.0,
                    "credits_included": 2500,
                }
            ]
        }
        report = budget_admission(payload, NOW)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("no_active_billing_period", report["blockers"])
        self.assertTrue(any("date-only" in reason for reason in report["reasons"]))

    def test_partial_interval_counts_conservatively(self):
        fresh = history_cost_samples(
            8894132, None, [0.42, 0.42, 0.42],
            ended_hours_ago=2, base_utc="2026-11-03T23:59:00Z",
        )
        grouped = group_samples(fresh, default_performance="medium")
        forecast = scheduled_forecast(
            [{"key": "daily", "query_id": 8894132, "cadence_minutes": 1440,
              "export_policy": "none"}],
            grouped,
            now_utc="2026-11-03T23:59:00Z",
            period_end_utc=PERIOD_END,
            default_performance="medium",
        )
        self.assertEqual(forecast["detail"][0]["runs"], 1)
        self.assertAlmostEqual(
            forecast["remaining_seconds_before_renewal"], 60.0
        )


class OutstandingLiabilityTests(unittest.TestCase):
    """Unsettled execution/export costs conservatively join confirmed usage;
    duplicate liability ids never discard the higher risk."""

    def test_observed_cost_liability_counts(self):
        result = outstanding_liabilities(
            [{"kind": "execution", "liability_id": "exec-42", "cost_credits": 1.09}]
        )
        self.assertAlmostEqual(result["total"], 1.09)

    def test_computed_export_liability_from_bytes(self):
        result = outstanding_liabilities(
            [{"kind": "export", "liability_id": "export-wire-1",
              "total_result_set_bytes": 2_500_000}],
            export_credits_per_megabyte=10.0,
        )
        self.assertAlmostEqual(result["total"], 25.0)

    def test_included_in_confirmed_usage_skips_without_double_counting(self):
        result = outstanding_liabilities(
            [
                {"kind": "execution", "liability_id": "already-counted",
                 "included_in_confirmed_usage": True},
                {"kind": "execution", "liability_id": "pending",
                 "cost_credits": 1.09},
            ]
        )
        self.assertAlmostEqual(result["total"], 1.09)
        self.assertEqual(
            result["skipped"],
            [{"liability_id": "already-counted", "reason": "already_in_confirmed_usage"}],
        )
        self.assertTrue(any("no double counting" in r for r in result["reasons"]))

    def test_duplicate_with_identical_cost_is_skipped(self):
        result = outstanding_liabilities(
            [
                {"liability_id": "dup-1", "kind": "execution", "cost_credits": 1.0},
                {"liability_id": "dup-1", "kind": "execution", "cost_credits": 1.0},
            ]
        )
        self.assertAlmostEqual(result["total"], 1.0)
        self.assertEqual(
            result["skipped"], [{"liability_id": "dup-1", "reason": "duplicate_id"}]
        )

    def test_conflicting_duplicates_use_the_maximum_higher_risk(self):
        result = outstanding_liabilities(
            [
                {"liability_id": "conflict", "kind": "execution", "cost_credits": 1.0},
                {"liability_id": "conflict", "kind": "execution", "cost_credits": 2.0},
            ]
        )
        self.assertAlmostEqual(result["total"], 2.0)
        self.assertTrue(any("using the maximum" in r for r in result["reasons"]))
        # And in the reverse order of arrival.
        reverse = outstanding_liabilities(
            [
                {"liability_id": "conflict", "kind": "execution", "cost_credits": 2.0},
                {"liability_id": "conflict", "kind": "execution", "cost_credits": 1.0},
            ]
        )
        self.assertAlmostEqual(reverse["total"], 2.0)
        self.assertTrue(any("keeping the maximum" in r for r in reverse["reasons"]))

    def test_unknown_liability_blocks_and_never_gets_a_cap(self):
        report = budget_admission(
            usage(credits_used=1600.0),
            NOW,
            outstanding=[{"kind": "execution", "liability_id": "pending-exec-1"}],
        )
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(
            any(b.startswith(
                "unknown_outstanding_liability:pending-exec-1: no_cost_estimate_available")
                for b in report["blockers"])
        )

    def test_liabilities_decide_admission_only_when_headroom_exists(self):
        liabilities = [
            {"kind": "execution", "liability_id": "lag", "cost_credits": 1.09},
            {"kind": "export", "liability_id": "wire",
             "total_result_set_bytes": 2_500_000},
        ]
        admitted = budget_admission(usage(credits_used=1590.0), NOW, outstanding=liabilities)
        blocked = budget_admission(usage(credits_used=1775.0), NOW, outstanding=liabilities)
        self.assertEqual(admitted["status"], "admitted")
        self.assertAlmostEqual(admitted["totals"]["dependencies_total"], 26.09)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(
            any("forecast_exceeds_guard_headroom" in b for b in blocked["blockers"])
        )


class ExecutionAdmissionTests(unittest.TestCase):
    SAMPLES = (
        history_cost_samples(8894037, {"chunk": "2026-11-01"},
                             [1.0, 1.04, 0.9], ended_hours_ago=1)
    )

    def test_post_run_cap_is_never_a_pre_run_estimate(self):
        spec = {"query_id": 8894037, "max_run_credits": 99}
        result = execution_admission(
            spec, [], now_utc=NOW, default_sample_performance="medium"
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["cost_estimate_credits"])
        self.assertTrue(
            any("no_samples_for_identity" in r or "insufficient" in r
                for r in result["reasons"])
        )

    def test_sample_backed_estimate_uses_uplifted_envelope(self):
        spec = {"query_id": 8894037, "query_parameters": {"chunk": "2026-11-01"},
                "max_run_credits": 99}
        result = execution_admission(
            spec, self.SAMPLES, now_utc=NOW, default_sample_performance="medium"
        )
        self.assertEqual(result["status"], "sampled")
        expected = max(
            1.04,
            statistics.fmean([1.0, 1.04, 0.9]) + 3 * statistics.stdev([1.0, 1.04, 0.9]),
        )
        self.assertAlmostEqual(result["cost_estimate_credits"], expected, places=9)
        self.assertEqual(result["sample_count"], 3)

    def test_performance_mismatch_is_unknown_not_mixed(self):
        spec = {"query_id": 8894037, "query_parameters": {"chunk": "2026-11-01"},
                "performance": "large"}
        result = execution_admission(
            spec, self.SAMPLES, now_utc=NOW, default_sample_performance="medium"
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["cost_estimate_credits"])


class ActivePeriodSelectionTests(unittest.TestCase):
    def test_selects_the_exact_active_period(self):
        payload = {
            "billing_periods": [
                {"start_date": "2026-09-04T16:35:49Z", "end_date": "2026-10-04T16:35:49Z",
                 "credits_used": 2100.0, "credits_included": 2500},
                {"start_date": PERIOD_START, "end_date": PERIOD_END,
                 "credits_used": 1798.673, "credits_included": 2500},
            ]
        }
        selection = select_active_period(payload["billing_periods"], NOW)
        self.assertIsNotNone(selection["period"])
        self.assertAlmostEqual(selection["period"]["credits_used"], 1798.673)
        payload_report = budget_admission(payload, NOW)
        self.assertEqual(payload_report["status"], "blocked")

    def test_empty_periods_yields_reason_not_exception(self):
        selection = select_active_period([], NOW)
        self.assertIsNone(selection["period"])
        self.assertIn("no_active_billing_period", selection["reasons"])

    def test_period_after_renewal_is_not_active(self):
        payload = {
            "billing_periods": [
                {"start_date": PERIOD_START, "end_date": PERIOD_END,
                 "credits_used": 10.0, "credits_included": 2500},
            ]
        }
        report = budget_admission(payload, "2026-11-05T00:00:00Z")
        self.assertEqual(report["status"], "blocked")
        self.assertIn("no_active_billing_period", report["blockers"])


class MalformedInputTests(unittest.TestCase):
    """Malformed values raise BudgetInputError so callers fail closed loudly."""

    def malformed_cases(self):
        nan = float("nan")
        return [
            ("nan usage", lambda: budget_admission(usage(credits_used=nan), NOW)),
            ("bool usage", lambda: budget_admission(usage(credits_used=True), NOW)),
            ("negative usage", lambda: budget_admission(usage(credits_used=-1.0), NOW)),
            ("string usage", lambda: budget_admission(usage(credits_used="100"), NOW)),
            ("infinite quota", lambda: budget_admission(usage(credits_included=float("inf")), NOW)),
            ("bool guard", lambda: budget_admission(CURRENT_USAGE, NOW, guard_credits=True)),
            ("negative reserve", lambda: budget_admission(CURRENT_USAGE, NOW, minimum_reserve_credits=-0.5)),
            ("infinite export scale", lambda: budget_admission(CURRENT_USAGE, NOW, export_credits_per_megabyte=float("inf"))),
            ("space-form now", lambda: budget_admission(CURRENT_USAGE, "2026-10-16 22:00:00Z")),
            ("date-only now", lambda: budget_admission(CURRENT_USAGE, "2026-10-16")),
            ("naive now", lambda: budget_admission(CURRENT_USAGE, datetime(2026, 10, 16, 22, 0))),
            ("naive end datetime", lambda: budget_admission(usage(period_end_utc=datetime(2026, 11, 4)), NOW)),
            ("zero cadence", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "query_id": 1, "cadence_minutes": 0}])),
            ("bool cadence", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "query_id": 1, "cadence_minutes": True}])),
            ("missing cadence", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "query_id": 1}])),
            ("missing key", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"query_id": 1, "cadence_minutes": 30}])),
            ("missing query id", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "cadence_minutes": 30}])),
            ("string enabled", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "query_id": 1, "cadence_minutes": 30, "enabled": "yes"}])),
            ("bad export policy", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "query_id": 1, "cadence_minutes": 30, "export_policy": "everything"}])),
            ("planned bytes cap rejected", lambda: budget_admission(CURRENT_USAGE, NOW, scheduled=[{"key": "x", "query_id": 1, "cadence_minutes": 30, "planned_export_bytes": 5000}])),
            ("negative liability", lambda: budget_admission(CURRENT_USAGE, NOW, outstanding=[{"kind": "execution", "cost_credits": -2}])),
            ("bool liability cost", lambda: budget_admission(CURRENT_USAGE, NOW, outstanding=[{"kind": "execution", "cost_credits": False}])),
            ("negative bytes", lambda: export_liability_estimate(-5, 10.0)),
            ("bool bytes", lambda: export_liability_estimate(True, 10.0)),
            ("unserializable parameters", lambda: canon_parameters({"chunk": object()})),
            ("non-dict parameters", lambda: canon_parameters(["2026-10"])),
            ("negative max age", lambda: sample_estimate({}, 1, None, now_utc=NOW, max_age_hours=-1)),
            ("zero min samples", lambda: sample_estimate({}, 1, None, now_utc=NOW, min_comparable_samples=0)),
            ("negative uplift", lambda: sample_estimate({}, 1, None, now_utc=NOW, variance_uplift_sigma=-1)),
        ]

    def test_malformed_inputs_raise_budget_input_error(self):
        for label, call in self.malformed_cases():
            with self.subTest(label=label):
                with self.assertRaises(BudgetInputError):
                    call()

    def test_non_object_containers_raise(self):
        for label, call in {
            "usage string": lambda: budget_admission("nope", NOW),
            "scheduled not list": lambda: budget_admission(CURRENT_USAGE, NOW, scheduled={"key": "x"}),
            "outstanding not list": lambda: budget_admission(CURRENT_USAGE, NOW, outstanding={"a": 1}),
            "samples not list": lambda: group_samples("history"),
            "periods not list": lambda: select_active_period({"usage": 1}, NOW),
            "sample not object": lambda: group_samples(["x"]),
            "sample without query": lambda: group_samples([{"execution_cost_credits": 1.0}]),
            "sample bool cost": lambda: group_samples([{"query_id": 1, "execution_cost_credits": True}]),
            "sample nan cost": lambda: group_samples([{"query_id": 1, "execution_cost_credits": float("nan")}]),
            "liability not object": lambda: outstanding_liabilities(["pending"]),
            "liability bool flag": lambda: outstanding_liabilities([{"liability_id": "a", "included_in_confirmed_usage": "true"}]),
            "spec not dict": lambda: execution_admission("spec", [], now_utc=NOW),
            "spec without query": lambda: execution_admission({"key": "current"}, [], now_utc=NOW),
            "shape version list": lambda: group_samples([{"query_id": 1, "execution_cost_credits": 1.0, "shape_version": ["v1"]}]),
        }.items():
            with self.subTest(label=label):
                with self.assertRaises(BudgetInputError):
                    call()


class ReportShapeTests(unittest.TestCase):
    def test_report_is_json_serializable_and_detailed(self):
        report = budget_admission(
            usage(credits_used=1600.0),
            FORECAST_NOW,
            samples=self.SAMPLES,
            scheduled=self.ENTRIES,
            outstanding=[{"kind": "export", "liability_id": "w",
                          "total_result_set_bytes": 2_500_000}],
            default_sample_performance="medium",
            monthly_target_credits=RECEIVED_TARGET,
        )
        encoded = json.dumps(report)
        self.assertIsInstance(encoded, str)
        for key in (
            "status", "blockers", "reasons", "policy", "current_usage",
            "now_utc", "forecast", "outstanding_liabilities", "totals",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["policy"]["guard_credits"], DEFAULT_GUARD_CREDITS)
        # dependencies = liabilities + scheduled compute + scheduled export
        self.assertAlmostEqual(
            report["totals"]["dependencies_total"],
            (report["outstanding_liabilities"]["total"]
             + report["forecast"]["total"]),
            delta=1e-9,
        )
        self.assertIn("NOT a guarantee", report["policy"]["estimate_basis"])
        self.assertLess(
            report["totals"]["limit_credits"], report["current_usage"]["credits_included"]
        )

    SAMPLES = ScheduledForecastTests.SAMPLES
    ENTRIES = ScheduledForecastTests.ENTRIES

    def test_parse_instant_normalizes_offset_to_utc(self):
        parsed = parse_instant("2026-11-04T00:00:00+02:00", "test")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0.0)
        self.assertEqual(parsed.day, 3)
        self.assertEqual(parsed.hour, 22)


if __name__ == "__main__":
    unittest.main()
