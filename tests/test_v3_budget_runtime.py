"""Legacy conservative adapter regressions plus deployed configuration wiring.

Offline and deterministic. ``config/v3_refresh.json`` is read once (no
network, repository file) and every test fails loudly if it changes in a
way that would loosen a budget limit rather than silently producing
"different" numbers. Historical 1,800/15 arithmetic uses an explicit frozen
fixture; the newly approved standing policy is asserted separately below.
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v3_budget_runtime as rt
import v3_result_cache as v3cache
from v3_credit_budget import BudgetInputError
from v3_financial_journal import export_attempt_id, synchronize_financial_journal, record_export_outcome

CONFIG_PATH = ROOT / "config" / "v3_refresh.json"
REAL_CONFIG = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else None
DEPLOYED_CONFIG = deepcopy(REAL_CONFIG)
if REAL_CONFIG is not None:
    REAL_CONFIG.update(monthly_spend_guard_credits=1800, minimum_credit_reserve=15)
    REAL_CONFIG["sources"][0].update(lookback_hours=2, cadence_minutes=30)
    for spec in REAL_CONFIG["presentation"]:
        if spec["key"] in {"current", "pools"}:
            spec["cadence_minutes"] = 15
        elif spec["key"] in {"burns", "holders", "tokenomics"}:
            spec["cadence_minutes"] = 30

FRESH_NOW = "2026-11-03T23:59:00Z"  # 60 s before the exact renewal instant
GUARD_NOW = "2026-10-16T22:00:00Z"  # mid-window; the guard is already reached
PERIOD_START = "2026-10-04T16:35:49Z"
PERIOD_END = "2026-11-04T00:00:00Z"
EXEC_ID = "01ARZ3NDEKTSV4RR"  # exact result-export execution id for fixtures

SCHEDULE_KEYS = [
    "source:xnet_transfers",
    "source:bbb_dex",
    "presentation:current",
    "presentation:burns",
    "presentation:tokenomics",
    "presentation:holders",
    "presentation:pools",
    "presentation:network",
    "presentation:commercial",
    "manifest:daily",
    "manifest:weekly",
]

# Tracker key -> (query_id, parameters, compute cost, result_bytes).
# Presentations are chart-embedded: no result_bytes evidence at all.
# Manifests share one query id and differ ONLY by their lookback_days
# parameter; their metered result_bytes exercises the export lane.
SCHEDULE_SOURCES = [
    ("source:xnet_transfers", 8894037, {"lookback_hours": 2}, 1.04, 0),
    ("source:bbb_dex", 8894132, {"lookback_hours": 26}, 0.42, 0),
    ("presentation:current", 8894185, {}, 0.5, None),
    ("presentation:burns", 8895090, {}, 0.5, None),
    ("presentation:tokenomics", 8895091, {}, 0.5, None),
    ("presentation:holders", 8895092, {}, 0.5, None),
    ("presentation:pools", 8895093, {}, 0.5, None),
    ("presentation:network", 8895088, {}, 0.5, None),
    ("presentation:commercial", 8895089, {}, 0.5, None),
    ("manifest:daily", 8893945, {"lookback_days": 7}, 2.0, 200_000),
    ("manifest:weekly", 8893945, {"lookback_days": 30}, 2.0, 200_000),
]
EXPORT_RATE = 10.0  # config/v3_refresh.json: export_credits_per_megabyte
# Known, non-reconciled compute debts: 3 fresh tracker-history rows per key.
KNOWN_LIABILITY_TOTAL = sum(cost * 3 for _, _, _, cost, _ in SCHEDULE_SOURCES)
# Incurred exports are DISTINCT liabilities (one per byte-bearing history row).
EXPORT_LIABILITY_TOTAL = sum(
    (bytes_ / 1_000_000) * EXPORT_RATE * 3
    for _, _, _, _, bytes_ in SCHEDULE_SOURCES
    if bytes_ is not None
)
TOTAL_OUTSTANDING = KNOWN_LIABILITY_TOTAL + EXPORT_LIABILITY_TOTAL
SCHEDULED_FORECAST_TOTAL = 1.04 + 0.42 + 7 * 0.5 + 2 * (2.0 + 2.0)
# Per-row cost of the `{key}#0` reconcile subset: compute per key plus the
# export liability of the manifest rows (xnet/bbb rows export 0 credits).
RECONCILE_SUBSET_COST = (
    1.04 + 0.42 + 7 * 0.5 + 2 * 2.0      # compute, one row per key
    + 2 * (200_000 / 1_000_000) * EXPORT_RATE  # manifest export rows
)


def usage(credits_used=900.0, **overrides):
    payload = {
        "credits_used": credits_used,
        "credits_included": 2500,
        "period_start_utc": PERIOD_START,
        "period_end_utc": PERIOD_END,
    }
    payload.update(overrides)
    return payload


def tracker_history(base_utc="2026-11-03T21:00:00Z"):
    """Real execution_history rows: tracker key labels, query_parameters,
    telemetry only where the pipeline meters it, NO performance field (the
    adapter derives that from config)."""
    base = datetime.fromisoformat(base_utc.replace("Z", "+00:00"))
    ended = (base - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    rows = []
    for key, query_id, params, cost, result_bytes in SCHEDULE_SOURCES:
        for index in range(3):
            row = {
                "key": key,
                "query_id": query_id,
                "execution_id": f"{key}#{index}",
                "execution_cost_credits": cost,
                "query_parameters": deepcopy(params),
                "execution_ended_at": ended,
            }
            if result_bytes is not None:
                row["result_bytes"] = result_bytes
                row["export_wire_bytes"] = result_bytes
                row["export_requested_at_utc"] = ended
            rows.append(row)
    return rows


def tracker_state(history=None, pending=None, consumed=None, reconcile=None):
    state = {
        "execution_history": list(tracker_history() if history is None else history),
        "pending_executions": pending if pending is not None else {},
        "consumed_executions": consumed if consumed is not None else {},
    }
    if reconcile is not None:
        state["cost_reconciliation"] = reconcile
    return state


def run(state=None, config=None, usage_payload=None, now_utc=FRESH_NOW, **kwargs):
    return rt.evaluate_budget(
        tracker_state() if state is None else state,
        REAL_CONFIG if config is None else config,
        usage() if usage_payload is None else usage_payload,
        now_utc,
        **kwargs,
    )


def entry_by_key(entries, key):
    return next(entry for entry in entries if entry["key"] == key)


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class ScheduledEntryWiringTests(unittest.TestCase):
    def test_entries_mirror_the_real_rotation(self):
        entries, diagnostics = rt.build_scheduled_entries(DEPLOYED_CONFIG)
        self.assertEqual([entry["key"] for entry in entries], SCHEDULE_KEYS)
        self.assertEqual(diagnostics["skipped_entries"], [])
        transfers = entry_by_key(entries, "source:xnet_transfers")
        self.assertEqual(transfers["query_id"], 8894037)
        self.assertEqual(transfers["query_parameters"], {"lookback_hours": 6})
        self.assertEqual(transfers["performance"], "medium")
        self.assertEqual(transfers["cadence_minutes"], 120)
        self.assertEqual(transfers["export_policy"], "observed_max")
        bbb = entry_by_key(entries, "source:bbb_dex")
        self.assertEqual(bbb["query_parameters"], {"lookback_hours": 26})
        self.assertEqual(bbb["cadence_minutes"], 1440)
        current = entry_by_key(entries, "presentation:current")
        self.assertEqual(current["query_parameters"], {})
        self.assertEqual(current["export_policy"], "none")
        self.assertEqual(current["cadence_minutes"], 60)
        daily = entry_by_key(entries, "manifest:daily")
        self.assertEqual(daily["query_id"], 8893945)
        self.assertEqual(daily["query_parameters"], {"lookback_days": 7})
        self.assertEqual(daily["cadence_minutes"], 1440)
        self.assertEqual(daily["export_policy"], "observed_max")
        weekly = entry_by_key(entries, "manifest:weekly")
        self.assertEqual(weekly["query_parameters"], {"lookback_days": 30})
        self.assertEqual(weekly["cadence_minutes"], 10080)
        # The configured byte cap is a run-time compliance limit, NOT a
        # precharged export quote: no planned-byte fields are wired.
        self.assertNotIn("planned_export_bytes", daily)
        self.assertNotIn("planned_export_bytes", weekly)

    def test_manifest_byte_cap_still_validates_loudly(self):
        config = deepcopy(REAL_CONFIG)
        config["reconciliation"]["manifest_max_export_bytes"] = -5
        with self.assertRaises(BudgetInputError):
            rt.build_scheduled_entries(config)

    def test_budget_knobs_extract_from_the_real_config(self):
        self.assertEqual(rt._require_settings(DEPLOYED_CONFIG),
                          (3000, 100, 10, 2500, 150))
        self.assertEqual(DEPLOYED_CONFIG["autonomous_operation"]["minimum_unspent_quota_credits"], 1000)

    def test_disabled_sources_are_skipped_with_diagnostics(self):
        config = deepcopy(REAL_CONFIG)
        config["sources"][0]["enabled"] = False
        entries, diagnostics = rt.build_scheduled_entries(config)
        keys = [entry["key"] for entry in entries]
        self.assertNotIn("source:xnet_transfers", keys)
        self.assertEqual(
            diagnostics["skipped_entries"],
            [{"key": "source:xnet_transfers", "reason": "disabled"}],
        )


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class CurrentStateBlockedTests(unittest.TestCase):
    """The actual mission moment: 1798.673 used, guard 1800, target 2500."""

    def test_guard_reached_with_detailed_forecast_reported(self):
        result = run(state=tracker_state(history=[]),
                     usage_payload=usage(1798.673), now_utc=GUARD_NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(
            any(b.startswith("monthly_spend_guard_reached")
                for b in result["blockers"])
        )
        self.assertIsNotNone(result["report"]["forecast"])
        totals = result["report"]["totals"]
        self.assertEqual(totals["limit_credits"], 1800.0)
        self.assertAlmostEqual(totals["usage_plus_reserve"], 1813.673)
        policy = result["diagnostics"]["config_policy"]
        self.assertEqual(policy["monthly_spend_guard_credits"], 1800)
        self.assertEqual(policy["monthly_target_credits"], 2500)
        self.assertEqual(policy["minimum_target_headroom_credits"], 150)
        self.assertEqual(policy["export_credits_per_megabyte"], 10)
        self.assertEqual(policy["minimum_credit_reserve"], 15)

    def test_date_only_usage_end_blocks_without_inventing_precision(self):
        payload = usage(period_end_utc="2026-11-04")
        result = run(state=tracker_state(history=[]), usage_payload=payload,
                     now_utc=GUARD_NOW)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("period_end_not_exact_utc", result["blockers"])
        self.assertIsNone(result["report"]["forecast"])


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class FreshWindowAdmissionTests(unittest.TestCase):
    def test_green_state_near_renewal_admits_the_rotation(self):
        result = run()
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["blockers"], [])
        totals = result["report"]["totals"]
        self.assertAlmostEqual(totals["scheduled_forecast_credits"],
                               SCHEDULED_FORECAST_TOTAL)
        self.assertAlmostEqual(totals["outstanding_liability_credits"],
                               TOTAL_OUTSTANDING)
        self.assertAlmostEqual(totals["dependencies_total"],
                               SCHEDULED_FORECAST_TOTAL + TOTAL_OUTSTANDING)

    def test_manifest_weekly_is_a_separate_identity_from_daily(self):
        history = tracker_history()
        for row in history:
            if row["key"] == "manifest:daily":
                row["execution_cost_credits"] = 2.0
            elif row["key"] == "manifest:weekly":
                row["execution_cost_credits"] = 9.0
        result = run(state=tracker_state(history=history))
        self.assertEqual(result["status"], "admitted")
        detail = {row["key"]: row
                  for row in result["report"]["forecast"]["detail"]}
        self.assertAlmostEqual(detail["manifest:weekly"]["compute_estimates"], 9.0)
        self.assertAlmostEqual(detail["manifest:daily"]["compute_estimates"], 2.0)

    def test_mixed_performance_rows_split_identities_and_block(self):
        history = [row for row in tracker_history()
                   if row["query_id"] != 8894037]
        for index, performance in enumerate(("medium", "medium", "small")):
            history.append({
                "key": "source:xnet_transfers",
                "query_id": 8894037,
                "execution_id": f"xnet-split-{index}",
                "execution_cost_credits": 1.04,
                "query_parameters": {"lookback_hours": 2},
                "performance": performance,
                "execution_ended_at": "2026-11-03T20:00:00Z",
                "result_bytes": 0,
            })
        result = run(state=tracker_state(history=history))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            "unknown_scheduled_cost:source:xnet_transfers" in blocker
            and "(query 8894037, component compute)" in blocker
            for blocker in result["blockers"]
        ))


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class LiabilityIntegrationTests(unittest.TestCase):
    def test_known_costs_are_retained_until_the_settle_lane_says_so(self):
        result = run()
        self.assertEqual(result["status"], "admitted")
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        items = result["report"]["outstanding_liabilities"]["items"]
        compute_items = [item for item in items if item["kind"] != "export"]
        export_items = [item for item in items if item["kind"] == "export"]
        self.assertEqual(len(compute_items), len(SCHEDULE_SOURCES) * 3)
        # Only the byte-bearing rows carry export liabilities: xnet, bbb and
        # the two manifest keys (3 rows each); presentations have no metered
        # export telemetry.
        self.assertEqual(len(export_items), 12)
        self.assertFalse(any(
            "presentation:" in item["liability_id"]
            for item in export_items
        ))
        self.assertTrue(all(
            item.get("included_in_confirmed_usage") is not True for item in items
        ))

    def test_export_and_compute_liabilities_share_the_row_id_pair(self):
        state = tracker_state()
        synchronize_financial_journal(state)
        items = run(state=state)["report"]["outstanding_liabilities"]["items"]
        by_id = {item["liability_id"]: item for item in items}
        for row in state["execution_history"]:
            if row.get("export_attempt_id"):
                self.assertIn(row["export_attempt_id"], by_id)
                self.assertIn(f"exec:{row['execution_id']}", by_id)

    def test_consumed_executions_are_not_settlement_proof(self):
        """``consumed_executions`` does not certify the cost sits inside the
        confirmed usage figure: every known liability stays outstanding."""
        consumed = {
            f"{key}#{index}": {
                "execution_id": f"{key}#{index}",
                "accepted_at_utc": FRESH_NOW,
            }
            for key, _, _, _, _ in SCHEDULE_SOURCES
            for index in range(3)
        }
        result = run(state=tracker_state(consumed=consumed))
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["blockers"], [])
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        self.assertEqual(
            result["diagnostics"]["liabilities"]["settled_ids_flagged"], []
        )

    def test_numerically_bound_snapshot_is_not_charge_inclusion_proof(self):
        settled = [f"{key}#0" for key, _, _, _, _ in SCHEDULE_SOURCES]
        result = run(state=tracker_state(reconcile={
            "usage_snapshot": {
                "credits_used": 900.0,
                "period_start_utc": PERIOD_START,  # binds to the current
                "period_end_utc": PERIOD_END,      # usage payload's period
            },
            "settled_execution_ids": settled,
        }))
        self.assertEqual(result["status"], "admitted")
        expected = TOTAL_OUTSTANDING
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            expected,
        )
        flagged = result["diagnostics"]["liabilities"]["settled_ids_flagged"]
        self.assertEqual(flagged, [])
        self.assertTrue(result["diagnostics"]["liabilities"]["legacy_settlement_ignored"])

    def test_top_level_settled_ids_are_not_settlement_proof(self):
        state = tracker_state()
        state["settled_execution_ids"] = [
            f"{key}#{index}" for key, _, _, _, _ in SCHEDULE_SOURCES
            for index in range(3)
        ]
        result = run(state=state)
        self.assertEqual(result["status"], "admitted")
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        self.assertEqual(
            result["diagnostics"]["liabilities"]["settled_ids_flagged"], []
        )

    def test_cost_reconciliation_lane_requires_its_evidence(self):
        ids = [f"{key}#0" for key, _, _, _, _ in SCHEDULE_SOURCES]
        good_snapshot = {"credits_used": 900.0, "billing_period_id": "bp"}
        for label, lane in (
            ("lane not object", []),
            ("without usage snapshot", {"settled_execution_ids": ids}),
            ("snapshot without credits", {
                "usage_snapshot": {"billing_period_id": "bp"},
                "settled_execution_ids": ids,
            }),
            ("snapshot without period identity", {
                "usage_snapshot": {}, "settled_execution_ids": ids,
            }),
            ("ids not a list", {
                "usage_snapshot": good_snapshot,
                "settled_execution_ids": "x",
            }),
        ):
            with self.subTest(label=label):
                with self.assertRaises(BudgetInputError):
                    run(state=tracker_state(reconcile=lane))

    def test_pending_submission_without_id_blocks_unknown_liability(self):
        pending = {
            "source:xnet_transfers": {
                "key": "source:xnet_transfers",
                "query_id": 8894037,
                "query_parameters": {"lookback_hours": 2},
                "spec_identity": {
                    "key": "source:xnet_transfers",
                    "query_id": 8894037,
                    "performance": "medium",
                },
                "submitted_at_utc": "2026-11-03T22:00:00Z",
                "submission_error": "response lost",
            }
        }
        result = run(state=tracker_state(pending=pending))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            "unknown_outstanding_liability:pending:source:xnet_transfers"
            in blocker
            for blocker in result["blockers"]
        ))
        self.assertIn(
            "pending:source:xnet_transfers",
            result["diagnostics"]["liabilities"]["unknown_liability_ids"],
        )

    def test_pending_terminal_status_without_history_is_its_own_liability(self):
        pending = {
            "source:xnet_transfers": {
                "key": "source:xnet_transfers",
                "query_id": 8894037,
                "query_parameters": {"lookback_hours": 2},
                "execution_id": "e-terminal",
                "submitted_at_utc": "2026-11-03T22:00:00Z",
                "terminal_status": {
                    "state": "QUERY_STATE_COMPLETED",
                    "execution_cost_credits": 7.5,
                },
            }
        }
        result = run(state=tracker_state(pending=pending))
        self.assertEqual(result["status"], "admitted")
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING + 7.5,
        )

    def test_pending_terminal_status_without_cost_stays_unknown(self):
        pending = {
            "source:xnet_transfers": {
                "key": "source:xnet_transfers",
                "query_id": 8894037,
                "query_parameters": {"lookback_hours": 2},
                "execution_id": "e-terminal-unknown",
                "submitted_at_utc": "2026-11-03T22:00:00Z",
                "terminal_status": {"state": "QUERY_STATE_COMPLETED"},
            }
        }
        result = run(state=tracker_state(pending=pending))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            blocker.startswith(
                "unknown_outstanding_liability:exec:e-terminal-unknown"
            ) for blocker in result["blockers"]
        ))

    def test_history_entry_without_cost_blocks_unknown_risk(self):
        history = tracker_history()
        history.append({
            "key": "source:xnet_transfers",
            "query_id": 8894037,
            "execution_id": "e-cost-unknown",
            "query_parameters": {"lookback_hours": 2},
            "execution_ended_at": "2026-11-03T20:00:00Z",
        })
        result = run(state=tracker_state(history=history))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            "unknown_outstanding_liability:exec:e-cost-unknown: "
            "no_cost_estimate_available" in blocker
            for blocker in result["blockers"]
        ))

    def test_export_hint_without_cost_evidence_blocks_unknown(self):
        history = tracker_history()
        history.append({
            "key": "source:xnet_transfers",
            "query_id": 8894037,
            "execution_id": "e-hint",
            "execution_cost_credits": 1.0,
            "query_parameters": {"lookback_hours": 2},
            "execution_ended_at": "2026-11-03T20:00:00Z",
            "export_points": 5,
        })
        result = run(state=tracker_state(history=history))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            blocker.startswith("unknown_outstanding_liability:legacy_export:exec:e-hint")
            for blocker in result["blockers"]
        ))

    def test_estimated_export_credits_is_a_known_export_liability(self):
        history = tracker_history()
        history.append({
            "key": "source:xnet_transfers",
            "query_id": 8894037,
            "execution_id": "e-metered-export",
            "execution_cost_credits": 1.0,
            "result_bytes": 0,
            "estimated_export_credits": 3.25,
            "query_parameters": {"lookback_hours": 2},
            "execution_ended_at": "2026-11-03T20:00:00Z",
        })
        result = run(state=tracker_state(history=history))
        self.assertEqual(result["status"], "admitted")
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING + 1.0 + 3.25,
        )
        items = result["report"]["outstanding_liabilities"]["items"]
        export_item = next(
            item for item in items
            if item["kind"] == "export"
            and item["liability_id"].startswith("legacy_export:exec:e-metered-export:")
        )
        self.assertAlmostEqual(export_item["cost_credits"], 3.25)


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class BillingPeriodLiabilityTests(unittest.TestCase):
    """Prior-period history does not double count into the active period:
    exclusion needs execution_ended_at evidence strictly before the period
    start; undated rows are conservatively retained."""

    def prior_row(self, execution_id, ended_at=None, *, with_export=True):
        row = {
            "key": "source:xnet_transfers",
            "query_id": 8894037,
            "execution_id": execution_id,
            "execution_cost_credits": 2.5,
            "query_parameters": {"lookback_hours": 2},
            "result_bytes": 200_000 if with_export else 0,
            "export_wire_bytes": 200_000 if with_export else 0,
            "export_requested_at_utc": ended_at,
        }
        if ended_at is not None:
            row["execution_ended_at"] = ended_at
        return row

    def test_rows_ended_strictly_before_period_start_are_excluded(self):
        history = tracker_history() + [
            self.prior_row("e-prior-1", "2026-09-20T12:00:00Z"),
            self.prior_row("e-prior-2", "2026-10-04T00:00:00Z"),
        ]
        state = tracker_state(history=history)
        synchronize_financial_journal(state)
        result = run(state=state)
        self.assertEqual(result["status"], "admitted")
        # Both the compute AND the export side of prior-period rows vanish.
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        expected_exports = {row["export_attempt_id"] for row in state["execution_history"]
                            if row["execution_id"] in {"e-prior-1", "e-prior-2"}}
        self.assertEqual(set(result["diagnostics"]["liabilities"]["prior_period_excluded_ids"]),
                         {"exec:e-prior-1", "exec:e-prior-2"} | expected_exports)

    def test_row_ended_exactly_at_period_start_is_retained(self):
        history = tracker_history() + [
            self.prior_row("e-boundary", PERIOD_START)  # not strictly before
        ]
        result = run(state=tracker_state(history=history))
        # Retention covers BOTH the compute and the export side of the row.
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING + 2.5 + (200_000 / 1_000_000) * EXPORT_RATE,
        )

    def test_undated_history_rows_are_retained_conservatively(self):
        history = tracker_history() + [self.prior_row("e-undated", None)]
        result = run(state=tracker_state(history=history))
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING + 2.5 + (200_000 / 1_000_000) * EXPORT_RATE,
        )

    def test_unresolvable_period_filters_nothing_but_module_still_blocks(self):
        history = [self.prior_row("e-prior", "2026-09-20T12:00:00Z")]
        usage_payload = {"billing_periods": []}
        result = run(state=tracker_state(history=history),
                     usage_payload=usage_payload)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            "no_active_billing_period" in blocker
            for blocker in result["blockers"]
        ))
        # Without an active period the module refuses to quantify spending
        # (totals stay None — no attribution basis, period or not).
        self.assertIsNone(result["report"]["totals"])
        # The conservative filter excluded nothing: the row stayed
        # outstanding in the build-time diagnostics.
        liab = result["diagnostics"]["liabilities"]
        self.assertNotIn("exec:e-prior", liab["prior_period_excluded_ids"])
        self.assertIn("exec:e-prior", liab["known_liability_ids"])


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class ProposedExecutionTests(unittest.TestCase):
    """Proposals ride the same admission: sampled AND within the headroom
    that remains after scheduled forecast + liabilities + reserve -> run;
    otherwise blocked, never stretched into a number. The scheduled
    forecast STAYS in the math (removing scheduled work to make a proposal
    look affordable is refused by design)."""

    PROPOSED = {"key": "integrity:xnet", "query_id": 8894038,
                "query_parameters": {"lookback_hours": 168}}

    def repair_history(self):
        return tracker_history() + [
            {
                "key": "integrity-repair",
                "query_id": 8894038,
                "execution_id": f"repair-{index}",
                "execution_cost_credits": 6.0,
                "query_parameters": {"lookback_hours": 168},
                "execution_ended_at": "2026-11-03T20:00:00Z",
                "result_bytes": 0,
            }
            for index in range(3)
        ]

    def test_sample_backed_proposal_admits_within_headroom(self):
        result = run(state=tracker_state(history=self.repair_history()),
                     proposed_spec=dict(self.PROPOSED))
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["blockers"], [])
        evaluation = result["proposed_evaluation"]
        self.assertEqual(evaluation["status"], "admitted")
        self.assertAlmostEqual(evaluation["cost_estimate_credits"], 6.0)
        # 1800 limit minus (900 usage + 15 reserve + scheduled 12.96 +
        # outstanding 38.88 + repair liabilities 18).
        self.assertAlmostEqual(evaluation["headroom_after_base"],
                               1800.0 - (915.0 + 12.96 + TOTAL_OUTSTANDING
                                         + 18.0))

    def test_unknown_proposed_cost_blocks_without_a_number(self):
        result = run(proposed_spec=dict(self.PROPOSED))
        self.assertEqual(result["status"], "blocked")
        evaluation = result["proposed_evaluation"]
        self.assertEqual(evaluation["status"], "blocked")
        self.assertNotIn("cost_estimate_credits", evaluation)
        self.assertTrue(any(
            blocker.startswith("unknown_proposed_cost:integrity:xnet")
            for blocker in result["blockers"]
        ))
        self.assertIsNotNone(result["report"]["forecast"])

    def test_proposed_params_merge_into_the_spec_identity(self):
        result = run(state=tracker_state(history=self.repair_history()),
                     proposed_spec={"key": "integrity:xnet",
                                    "query_id": 8894038},
                     proposed_params={"lookback_hours": 168})
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["proposed_evaluation"]["status"], "admitted")

    def test_spec_parameters_win_over_proposed_params(self):
        result = run(state=tracker_state(history=self.repair_history()),
                     proposed_spec={"key": "integrity:xnet", "query_id": 8894038,
                                    "query_parameters": {"lookback_days": 9}},
                     proposed_params={"lookback_hours": 168})
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["proposed_evaluation"]["status"], "blocked")

    def test_base_blockage_is_never_hidden_by_a_valid_proposal(self):
        result = run(state=tracker_state(history=self.repair_history()),
                     usage_payload=usage(1798.673),
                     proposed_spec=dict(self.PROPOSED))
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            blocker.startswith("monthly_spend_guard_reached")
            for blocker in result["blockers"]
        ))
        # The sampled proposal is evaluated against the committed ledger too:
        # with usage 1798.673 the headroom after base is negative, so the
        # proposal is blocked ON ITS OWN NUMBERS as well.
        evaluation = result["proposed_evaluation"]
        self.assertEqual(evaluation["status"], "blocked")
        self.assertAlmostEqual(evaluation["cost_estimate_credits"], 6.0)
        proposal_blockers = [
            blocker for blocker in result["blockers"]
            if blocker.startswith("proposed_execution_blocked")
        ]
        self.assertTrue(proposal_blockers)
        self.assertIn("exceeds the -83.513000", proposal_blockers[0])

    def test_proposal_blocked_by_headroom_even_when_the_base_is_green(self):
        # usage 1714 leaves the standing rotation admitted (the 69.84 of
        # scheduled forecast + outstanding + repair liabilities fit under
        # the guard) but only ~1.16 credits of headroom remain after
        # schedule + liabilities + reserve: a 6.0 credit proposal cannot
        # ride in, and the schedule was never dropped from the math.
        result = run(state=tracker_state(history=self.repair_history()),
                     usage_payload=usage(1714.0),
                     proposed_spec=dict(self.PROPOSED))
        base_blockers = [blocker for blocker in result["blockers"]
                         if not blocker.startswith("proposed_execution_blocked")]
        self.assertEqual(base_blockers, [])
        evaluation = result["proposed_evaluation"]
        self.assertEqual(evaluation["status"], "blocked")
        self.assertAlmostEqual(evaluation["cost_estimate_credits"], 6.0)
        self.assertIn("proposed_execution_blocked", result["blockers"][0])
        self.assertIn("1.160", result["blockers"][0])


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class PurityAndDiagnosticsTests(unittest.TestCase):
    def test_evaluate_budget_is_pure_and_repeatable(self):
        state = tracker_state()
        config = deepcopy(REAL_CONFIG)
        usage_payload = usage(900.0)
        before = (deepcopy(state), deepcopy(config), deepcopy(usage_payload))
        first = rt.evaluate_budget(state, config, usage_payload, FRESH_NOW)
        second = rt.evaluate_budget(state, config, usage_payload, FRESH_NOW)
        self.assertEqual(json.dumps(first), json.dumps(second))
        self.assertEqual((state, config, usage_payload), before)

    def test_budget_policy_overrides_flow_through_diagnostics(self):
        config = deepcopy(REAL_CONFIG)
        config["budget_policy"] = {
            "min_comparable_samples": 4,
            "sample_max_age_hours": 24.0,
            "variance_uplift_sigma": 2.0,
        }
        result = run(config=config)
        policy = result["diagnostics"]["config_policy"]
        self.assertEqual(policy["budget_policy.min_comparable_samples"], 4)
        self.assertEqual(policy["budget_policy.sample_max_age_hours"], 24.0)
        self.assertEqual(policy["budget_policy.variance_uplift_sigma"], 2.0)

    def test_budget_policy_override_tightens_even_green_states(self):
        config = deepcopy(REAL_CONFIG)
        config["budget_policy"] = {"sample_max_age_hours": 0.5}
        # All samples are 1h+ old relative to FRESH_NOW: a 0.5h policy makes
        # every scheduled identity unknown -> blocked, not admitted.
        result = run(config=config)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(
            blocker.startswith("unknown_scheduled_cost:source:xnet_transfers")
            for blocker in result["blockers"]
        ))


@unittest.skipIf(REAL_CONFIG is None, "config/v3_refresh.json not found")
class MalformedIntegrationTests(unittest.TestCase):
    def test_missing_guard_blocks_config_loudly(self):
        config = deepcopy(REAL_CONFIG)
        config.pop("monthly_spend_guard_credits")
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_bool_guard_blocks_config(self):
        config = deepcopy(REAL_CONFIG)
        config["monthly_spend_guard_credits"] = True
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_zero_reserve_blocks_config(self):
        config = deepcopy(REAL_CONFIG)
        config["minimum_credit_reserve"] = 0
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_negative_rate_blocks_config(self):
        config = deepcopy(REAL_CONFIG)
        config["export_credits_per_megabyte"] = -2
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_string_target_blocks_config(self):
        config = deepcopy(REAL_CONFIG)
        config["monthly_target_credits"] = "2500"
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_bool_headroom_blocks_config(self):
        config = deepcopy(REAL_CONFIG)
        config["reconciliation"]["minimum_target_headroom_credits"] = False
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_string_budget_policy_override_blocks(self):
        config = deepcopy(REAL_CONFIG)
        config["budget_policy"] = {"min_comparable_samples": "3"}
        with self.assertRaises(BudgetInputError):
            run(config=config)

    def test_source_without_query_id_raises(self):
        config = deepcopy(REAL_CONFIG)
        config["sources"][0].pop("query_id")
        with self.assertRaises(BudgetInputError):
            rt.build_scheduled_entries(config)

    def test_presentation_without_key_raises(self):
        config = deepcopy(REAL_CONFIG)
        config["presentation"][0].pop("key")
        with self.assertRaises(BudgetInputError):
            rt.build_scheduled_entries(config)

    def test_manifest_cadence_and_lookback_bounds(self):
        for key, bad_value in (("daily_cadence_minutes", 0),
                               ("daily_lookback_days", "7")):
            with self.subTest(key=key):
                config = deepcopy(REAL_CONFIG)
                config["reconciliation"][key] = bad_value
                with self.assertRaises(BudgetInputError):
                    rt.build_scheduled_entries(config)

    def test_state_shape_bounds(self):
        bad_history_cost = {
            "execution_history": [{
                "key": "source:xnet_transfers",
                "query_id": 8894037,
                "execution_id": "e",
                "execution_cost_credits": -3,
                "execution_ended_at": "2026-11-03T20:00:00Z",
            }],
        }
        reconcile_lane_bad = {
            "settled_execution_ids": [],  # no usage snapshot
        }
        reconcile_no_period = {
            "usage_snapshot": {},  # no period identity evidence
            "settled_execution_ids": [],
        }
        reconcile_ids_string = {
            "usage_snapshot": {"credits_used": 0.0, "billing_period_id": "b"},
            "settled_execution_ids": "x",
        }
        for label, state in (
            ("string state", "no"),
            ("history not list", {"execution_history": {}}),
            ("pending not dict", {"pending_executions": []}),
            ("reconciliation not object", {"cost_reconciliation": []}),
            ("reconciliation without usage snapshot", {
                 "cost_reconciliation": reconcile_lane_bad}),
            ("reconciliation without period identity", {
                 "cost_reconciliation": reconcile_no_period}),
            ("reconciliation ids not a list", {
                 "cost_reconciliation": reconcile_ids_string}),
            ("negative history cost", bad_history_cost),
        ):
            with self.subTest(label=label):
                with self.assertRaises(BudgetInputError):
                    run(state=state)

    def test_proposed_params_require_a_spec(self):
        with self.assertRaises(BudgetInputError):
            run(proposed_params={"lookback_hours": 2})


class ResultExportLaneTests(unittest.TestCase):
    """Export attempts, not execution IDs, identify possible charges.

    Cached/consumed risk is a reservation, not an observed charge or inclusion proof.
    """

    def setUp(self):
        if REAL_CONFIG is None:
            self.skipTest("config/v3_refresh.json is unavailable")

    def _stamped_state(self):
        base = tempfile.mkdtemp(prefix="v3-budget-result-export-")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        (Path(base) / "data" / "current").mkdir(parents=True)

        def bridge(state):
            return None

        cache = v3cache.ResultCache(
            Path(base), bridge, allowed_query_ids=frozenset({8894037}),
        )
        state = {"execution_history": [], "pending_executions": {}}
        spec = {
            "query_id": 8894037,
            "sql_sha256": "a" * 64,
            "parameters": {"lookback_hours": "25"},
            "columns": ["block_time", "amount_xnet"],
        }
        instruction = cache.prepare_export(
            state,
            {"execution_id": EXEC_ID},
            spec,
            risk_credits=0.5,
            evidence=["dune execution 01ARZ3NDEKTSV4RR billed 0.5 credits"],
            timestamp="2026-10-18 12:00:00",
        )
        requests = state["result_exports"]["requests"]
        key = next(iter(requests))
        return state, key, cache, spec

    def test_outstanding_intent_is_an_unknown_result_export_liability(self):
        state, key, _cache, _spec = self._stamped_state()
        items, diagnostics = rt.collect_outstanding_liabilities(
            deepcopy(state), REAL_CONFIG
        )
        liability_id = export_attempt_id(key, "2026-10-18 12:00:00")
        self.assertIn(liability_id, diagnostics["unknown_liability_ids"])
        self.assertIn(
            liability_id, diagnostics["result_export_liability_ids"]
        )
        matching = [
            item for item in items if item["liability_id"] == liability_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["kind"], "export")
        # No cost evidence yet: UNKNOWN, so admission blocks instead of
        # capping the attempt.
        self.assertNotIn("cost_credits", matching[0])

    def test_anchorless_intent_falls_back_to_result_export_key(self):
        state = {
            "execution_history": [],
            "pending_executions": {},
            "result_exports": {"requests": {
                "aa"*32: {
                    "status": "cached",
                    "risk_credits": 0.4,
                    "requested_at_utc": "2026-10-18 12:00:00",
                },
            }},
        }
        items, diagnostics = rt.collect_outstanding_liabilities(
            state, REAL_CONFIG
        )
        liability_id = export_attempt_id("aa" * 32, "2026-10-18 12:00:00")
        matching = [
            item for item in items if item["liability_id"] == liability_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertIn(liability_id, diagnostics["result_export_liability_ids"])
        self.assertIsNone(matching[0].get("cost_credits"))
        self.assertEqual(matching[0]["reserved_credits"], 0.4)
        self.assertIn(liability_id, diagnostics["unknown_liability_ids"])

    def test_cached_marker_is_counted_until_reconciled_even_when_consumed(self):
        state, key, cache, spec = self._stamped_state()
        rows = [
            {"block_time": "2026-10-15 16:30:22", "amount_xnet": "1.25"},
        ]
        cache.cache_result(
            state,
            {"execution_id": EXEC_ID},
            spec,
            {"execution_id": EXEC_ID, "total_row_count": 1, "result_rows": rows},
            timestamp="2026-10-18 12:05:00",
        )
        liability_id = export_attempt_id(key, "2026-10-18 12:00:00")
        # A complete cached payload does not meter its own wire charge.
        _items, before = rt.collect_outstanding_liabilities(state, REAL_CONFIG)
        self.assertIn(liability_id, before["unknown_liability_ids"])
        synchronize_financial_journal(state)
        record_export_outcome(state, liability_id, observed_credits=0.5)
        items, diagnostics = rt.collect_outstanding_liabilities(
            deepcopy(state), REAL_CONFIG
        )
        matching = [
            item for item in items if item["liability_id"] == liability_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].get("cost_credits"), 0.5)  # risk_credits
        self.assertEqual(
            diagnostics["result_export_liability_ids"], [liability_id]
        )
        state["consumed_executions"] = {EXEC_ID: {"accepted_at_utc": "n"}}
        cache.prune_consumed(state)
        marker = state["result_exports"]["requests"][key]
        self.assertEqual(marker["status"], "consumed")
        items, diagnostics = rt.collect_outstanding_liabilities(
            deepcopy(state), REAL_CONFIG
        )
        matching = [
            item for item in items if item["liability_id"] == liability_id
        ]
        self.assertEqual(len(matching), 1)  # consumed still counts
        self.assertEqual(matching[0].get("cost_credits"), 0.5)
        self.assertEqual(
            diagnostics["result_export_liability_ids"], [liability_id]
        )

    def test_missing_lane_keeps_schema_optional(self):
        state = {"execution_history": [], "pending_executions": {}}
        _items, diagnostics = rt.collect_outstanding_liabilities(
            state, REAL_CONFIG
        )
        self.assertEqual(diagnostics["result_export_liability_ids"], [])

    def test_malformed_lane_raises_instead_of_being_ignored(self):
        state = {
            "execution_history": [],
            "pending_executions": {},
            "result_exports": [],
        }
        with self.assertRaises(BudgetInputError):
            rt.collect_outstanding_liabilities(state, REAL_CONFIG)
        state_with_bad_marker = {
            "execution_history": [],
            "pending_executions": {},
            "result_exports": {"requests": ["not-a-marker"]},
        }
        with self.assertRaises(BudgetInputError):
            rt.collect_outstanding_liabilities(
                state_with_bad_marker, REAL_CONFIG
            )
        state_with_bad_status = {
            "execution_history": [],
            "pending_executions": {},
            "result_exports": {"requests": {"k": {"status": "archived"}}},
        }
        with self.assertRaises(BudgetInputError):
            rt.collect_outstanding_liabilities(
                state_with_bad_status, REAL_CONFIG
            )


class ResultExportMergeTests(unittest.TestCase):
    """Only an explicit shared ATTEMPT id permits max-merge of telemetry.
    Unknown attempts dominate known telemetry; execution dates do not date exports.
    """

    def setUp(self):
        if REAL_CONFIG is None:
            self.skipTest("config/v3_refresh.json is unavailable")

    def _history_row(self, export_credits):
        return {
            "execution_id": EXEC_ID,
            "execution_cost_credits": 0.3,
            "estimated_export_credits": export_credits,
            "execution_ended_at": "2026-11-03T20:00:00Z",
        }

    def _marker(self, status, risk=None):
        marker = {
            "status": status,
            "execution_id": EXEC_ID,
            "requested_at_utc": "2026-10-18 12:00:00",
        }
        if risk is not None:
            marker["risk_credits"] = risk
        return marker

    def _state(self, export_credits, marker):
        key = hashlib.sha256(EXEC_ID.encode()).hexdigest()
        row = self._history_row(export_credits)
        row["export_attempt_id"] = export_attempt_id(key, marker["requested_at_utc"])
        row["export_requested_at_utc"] = marker["requested_at_utc"]
        return {
            "execution_history": [row],
            "pending_executions": {},
            "result_exports": {"requests": {key: marker}},
        }

    def _attempt_id(self, requested="2026-10-18 12:00:00"):
        return export_attempt_id(hashlib.sha256(EXEC_ID.encode()).hexdigest(), requested)

    def test_history_and_cached_marker_merge_on_max_cost(self):
        for history_cost, marker_cost, expected in (
            (0.7, 0.5, 0.7), (0.3, 0.5, 0.3),
        ):
            with self.subTest(history=history_cost, marker=marker_cost):
                state = self._state(
                    history_cost, self._marker("cached", risk=marker_cost)
                )
                items, diagnostics = rt.collect_outstanding_liabilities(
                    state, REAL_CONFIG
                )
                liability_id = self._attempt_id()
                merged = [
                    item for item in items
                    if item["liability_id"] == liability_id
                ]
                self.assertEqual(len(merged), 1)
                self.assertAlmostEqual(merged[0]["cost_credits"], expected)
                self.assertAlmostEqual(merged[0]["reserved_credits"], marker_cost)
                self.assertIn(
                    liability_id, diagnostics["result_export_liability_ids"]
                )
                self.assertEqual(
                    [i for i in items
                     if i["liability_id"] == liability_id and
                     i["kind"] == "export"],
                    [merged[0]],
                )

    def test_outstanding_marker_dominates_known_history_estimate(self):
        state = self._state(0.7, self._marker("in_progress"))
        items, _diagnostics = rt.collect_outstanding_liabilities(
            state, REAL_CONFIG
        )
        merged = [
            item for item in items
            if item["liability_id"] == self._attempt_id()
        ]
        self.assertEqual(len(merged), 1)
        self.assertNotIn("cost_credits", merged[0])  # unknown dominates
        self.assertIn(
            self._attempt_id(), _diagnostics["unknown_liability_ids"]
        )

    def test_unknown_estimate_is_not_assumed_cheap(self):
        state = {
            "execution_history": [],
            "pending_executions": {},
            "result_exports": {"requests": {"bb"*32: {
                "status": "cached", "risk_credits": -1.0,
                "requested_at_utc": "2026-10-18 12:00:00",
            }}},
        }
        with self.assertRaises(BudgetInputError):
            rt.collect_outstanding_liabilities(state, REAL_CONFIG)

    def test_export_period_uses_attempt_not_provider_execution_date(self):
        state = self._state(0.3, {
            "status": "cached",
            "execution_id": EXEC_ID,
            "requested_at_utc": "2026-09-01T00:00:00Z",
            "risk_credits": 0.5,
        })
        evaluated = rt.collect_outstanding_liabilities(
            deepcopy(state), REAL_CONFIG,
            period_start_utc=rt.parse_instant(PERIOD_START, "period"),
        )
        self.assertEqual(evaluated[1]["prior_period_excluded_ids"],
                         [self._attempt_id("2026-09-01T00:00:00Z")])
        self.assertIn(f"exec:{EXEC_ID}", [item["liability_id"] for item in evaluated[0]])

    def test_prior_period_requested_marker_is_excluded_but_dated(self):
        # No history provider: the marker's requested_at_utc places it in
        # the prior period (before PERIOD_START) -> excluded from the fold,
        # while an unparseable stamp is retained conservatively.
        state = {
            "execution_history": [],
            "pending_executions": {},
            "result_exports": {"requests": {"cc"*32: {
                "status": "cached",
                "risk_credits": 0.5,
                "requested_at_utc": "2026-09-15T00:00:00Z",
            }}},
        }
        synchronize_financial_journal(state)
        record_export_outcome(state, export_attempt_id("cc" * 32, "2026-09-15T00:00:00Z"),
                              observed_credits=0.5)
        evaluated = rt.collect_outstanding_liabilities(
            deepcopy(state), REAL_CONFIG,
            period_start_utc=rt.parse_instant(PERIOD_START, "period"),
        )
        key_id = export_attempt_id("cc" * 32, "2026-09-15T00:00:00Z")
        self.assertIn(key_id, evaluated[1]["prior_period_excluded_ids"])
        self.assertFalse(any(
            item["liability_id"] == key_id for item in evaluated[0]
        ))
        undated = deepcopy(state)
        undated["result_exports"]["requests"]["cc" * 32][
            "requested_at_utc"
        ] = "not-a-moment"
        evaluated = rt.collect_outstanding_liabilities(
            deepcopy(undated), REAL_CONFIG,
            period_start_utc=rt.parse_instant(PERIOD_START, "period"),
        )
        self.assertIn(
            export_attempt_id("cc" * 32, "not-a-moment"),
            [item["liability_id"] for item in evaluated[0]]
        )

    def test_bound_snapshot_does_not_settle_attempt_or_compute(self):
        state = self._state(0.5, self._marker("cached", risk=0.5))
        state["cost_reconciliation"] = {
            "usage_snapshot": {
                "credits_used": 900.0,
                "period_start_utc": PERIOD_START,
                "period_end_utc": PERIOD_END,
            },
            "settled_execution_ids": [EXEC_ID],
        }
        evaluated = rt.collect_outstanding_liabilities(
            state, REAL_CONFIG,
            period_start_utc=rt.parse_instant(PERIOD_START, "period"),
            usage_payload={"credits_used": 900.0,
                           "period_start_utc": PERIOD_START,
                           "period_end_utc": PERIOD_END},
        )
        self.assertEqual(evaluated[1]["settled_ids_flagged"], [])
        self.assertIn(self._attempt_id(), evaluated[1]["known_liability_ids"])
        self.assertIn(f"exec:{EXEC_ID}", evaluated[1]["known_liability_ids"])


class ReconciliationBindingTests(unittest.TestCase):
    """Neither stale nor numerically matching aggregate usage proves inclusion."""

    def test_stale_snapshot_never_skips(self):
        settled = [f"{key}#0" for key, _, _, _, _ in SCHEDULE_SOURCES]
        # Same ids as the honored lane, but the snapshot's credits_used
        # disagree with the CURRENT verified usage: no skip.
        stale = {
            "usage_snapshot": {
                "credits_used": 800.0,
                "period_start_utc": PERIOD_START,
                "period_end_utc": PERIOD_END,
            },
            "settled_execution_ids": settled,
        }
        result = run(state=tracker_state(reconcile=stale))
        self.assertEqual(result["status"], "admitted")
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        self.assertEqual(
            result["diagnostics"]["liabilities"]["settled_ids_flagged"], []
        )

    def test_period_mismatch_never_skips(self):
        settled = [f"{key}#0" for key, _, _, _, _ in SCHEDULE_SOURCES]
        # Bound credits but period bounds that match no usage window.
        stale_bounds = {
            "usage_snapshot": {
                "credits_used": 900.0,
                "period_start_utc": "2025-01-01T00:00:00Z",
                "period_end_utc": "2025-02-01T00:00:00Z",
            },
            "settled_execution_ids": settled,
        }
        result = run(state=tracker_state(reconcile=stale_bounds))
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        # billing_period_id evidence without a matching id in the usage
        # payload binds nothing either.
        id_only = {
            "usage_snapshot": {
                "credits_used": 900.0, "billing_period_id": "bp-c1b3",
            },
            "settled_execution_ids": settled,
        }
        result = run(state=tracker_state(reconcile=id_only))
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        self.assertEqual(
            result["diagnostics"]["liabilities"]["settled_ids_flagged"], []
        )

    def test_unparseable_snapshot_bounds_bind_nothing(self):
        stale = {
            "usage_snapshot": {
                "credits_used": 900.0,
                "period_start_utc": "not-a-date",
                "period_end_utc": "not-a-date",
            },
            "settled_execution_ids": [f"{key}#0" for key, _, _, _, _
                                      in SCHEDULE_SOURCES],
        }
        result = run(state=tracker_state(reconcile=stale))
        self.assertAlmostEqual(
            result["report"]["totals"]["outstanding_liability_credits"],
            TOTAL_OUTSTANDING,
        )
        self.assertEqual(
            result["diagnostics"]["liabilities"]["settled_ids_flagged"], []
        )


if __name__ == "__main__":
    unittest.main()
