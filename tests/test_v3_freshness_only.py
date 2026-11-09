"""Offline regression for the user-approved opt-in freshness-only recovery.

The mode may only: collect the xnet_transfers hot source inside the EXISTING
planner fences (hot gap <= 12h plus the untouched configured 4h overlap) and
refresh the five approved status-only charts. Paid historical reconciliation
(manifest/day recovery), every other collector, any wider window, and any other
chart defer to the normal runner. cached_only always remains zero fresh Dune
compute, free probes and integrity evidence stay visible, a pending transfer
whose recorded window is wider than the hot fence defers before execute/export
while its durable ID is preserved, and known OUT-OF-SCOPE pending execution IDs
remain durable and receive only the free startup metadata reconciliation —
never a result export, a new execution, or a cancellation.
"""
from copy import deepcopy
from contextlib import ExitStack, contextmanager, redirect_stderr
from datetime import timedelta
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "freshness_refresh_test", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

BILLING = {"credits_used": 100.0, "guard_credits": 3000.0, "target_credits": 2500,
           "projected_period_credits": 110.0, "period_start_utc": "2026-11-01T00:00:00Z",
           "period_end_utc": "2026-12-01T00:00:00Z",
           "forecast": {"expected_credits": 0.0, "entries": [],
                        "independently_unpriced_domains": []}}

CHART_KEYS = ["current", "burns", "tokenomics", "holders", "pools"]
OUT_OF_ALLOWLIST_CHART_KEYS = ["network", "commercial"]


def transfer_spec():
    return next(s for s in refresh.CONFIG["sources"] if s["key"] == "xnet_transfers")


def bbb_spec():
    return next(s for s in refresh.CONFIG["sources"] if s["key"] == "bbb_dex")


def chart_spec(key):
    return next(s for s in refresh.CONFIG["presentation"] if s["key"] == key)


def chart_record(key):
    return {"execution_id": "exec-" + key, "query_id": chart_spec(key)["query_id"],
            "query_parameters": {}, "completed_at_utc": refresh.stamp(),
            "execution_cost_credits": 0.01}


def transfer_record(params):
    return {"execution_id": "exec-transfer", "query_id": transfer_spec()["query_id"],
            "query_parameters": deepcopy(params), "execution_cost_credits": 0.2}


def bbb_record(params):
    return {"execution_id": "exec-bbb", "query_id": bbb_spec()["query_id"],
            "query_parameters": deepcopy(params), "completed_at_utc": refresh.stamp(),
            "execution_cost_credits": 0.1}


def completed_at(hours):
    return (refresh.now() - timedelta(hours=hours)).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def fresh_source_state(lookback_hours, completed=None):
    return {"completed_at_utc": completed or refresh.stamp(),
            "query_parameters": {"lookback_hours": lookback_hours}}


def fresh_chart_state():
    """Every configured chart recently completed, so none is due."""
    return {spec["key"]: {"completed_at_utc": refresh.stamp()}
            for spec in refresh.CONFIG["presentation"]}


def pending_entry(key, query_id, params, execution_id):
    return {"key": key, "query_id": query_id, "query_parameters": deepcopy(params),
            "submitted_at_utc": refresh.stamp(), "execution_id": execution_id,
            "timeout_seconds": 90}


def make_execute(records):
    """Record keyed execute calls in call order and return a canned record."""
    calls = []

    def execute(spec, params=None, enforce_cap=True, timeout_seconds=None, state=None):
        key = spec.get("key", str(spec["query_id"]))
        calls.append((key, deepcopy(params)))
        return deepcopy(records[key])

    return calls, execute


def authorized_state():
    return {"schema_version": 1, "queries": {}, "execution_history": [],
            "pending_executions": {},
            "standing_authorization": {
                "approval_ref": refresh.CONFIG["autonomous_operation"]["approval_ref"],
                "policy_sha256": refresh.standing_authority_hash(
                    refresh.CONFIG["autonomous_operation"])}}


class FreshnessOnlyScopeTests(unittest.TestCase):
    """The opt-in mode never combines with an all-free lane and stays opt-in."""

    def test_cached_only_and_freshness_only_never_run_any_work(self):
        # cached_only must always mean zero fresh Dune compute; combining it
        # with the spending scope is a contract error before any state load.
        for argv in (["--freshness-only", "--cached-only"],
                     ["--freshness-only", "--benchmark"],
                     ["--freshness-only", "--benchmark-sources"]):
            with self.subTest(argv=argv), \
                 patch("sys.argv", ["refresh", *argv]), \
                 patch.object(refresh, "load_authoritative_state") as load, \
                 patch.object(refresh, "api") as api:
                with self.assertRaises(SystemExit) as failure:
                    refresh.main()
            self.assertEqual(failure.exception.code, 2)
            load.assert_not_called()
            api.assert_not_called()

    def test_freshness_allowlists_match_the_approved_scope_and_configuration(self):
        self.assertEqual(refresh.FRESHNESS_SOURCE_KEYS, {"xnet_transfers"})
        self.assertEqual(refresh.FRESHNESS_PRESENTATION_KEYS, set(CHART_KEYS))
        configured_charts = {spec["key"] for spec in refresh.CONFIG["presentation"]}
        self.assertTrue(refresh.FRESHNESS_PRESENTATION_KEYS <= configured_charts)
        contracts = refresh.CONFIG["autonomous_operation"]["queries"]
        for key in (*CHART_KEYS, "xnet_transfers"):
            spec = chart_spec(key) if key in refresh.FRESHNESS_PRESENTATION_KEYS else transfer_spec()
            self.assertIn(str(spec["query_id"]), contracts)

    def test_allowlist_drift_refuses_only_the_opt_in_invocation(self):
        drifted = deepcopy(refresh.CONFIG)
        drifted["presentation"] = [spec for spec in drifted["presentation"]
                                   if spec["key"] != "pools"]
        with patch.object(refresh, "CONFIG", drifted), \
             patch("sys.argv", ["refresh", "--freshness-only"]), \
             patch.object(refresh, "load_authoritative_state") as load, \
             patch.object(refresh, "api") as api, \
             redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as failure:
                refresh.main()
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("pools", stderr.getvalue())
        # Refused before any state load or Dune access.
        load.assert_not_called()
        api.assert_not_called()

    def test_default_runner_is_not_blocked_by_allowlist_drift(self):
        drifted = deepcopy(refresh.CONFIG)
        drifted["presentation"] = [spec for spec in drifted["presentation"]
                                   if spec["key"] != "pools"]
        with patch.object(refresh, "CONFIG", drifted), \
             patch("sys.argv", ["refresh"]), \
             patch.object(refresh, "load_authoritative_state",
                          side_effect=RuntimeError("sentinel after the checks")) as load, \
             patch.object(refresh, "api") as api:
            with self.assertRaises(RuntimeError):
                refresh.main()
        # The allowlist gate never fires on the default path: main reached the
        # sentinel state load instead of exiting, so a renamed chart cannot
        # break the ordinary runner (and import-time is likewise unguarded).
        load.assert_called_once()
        api.assert_not_called()


class FreshnessOnlyWorkflowTests(unittest.TestCase):
    def test_workflow_input_is_opt_in_and_cached_only_stays_free_first(self):
        import yaml
        path = ROOT / ".github/workflows/refresh-v3-history.yml"
        config = yaml.safe_load(path.read_text())
        trigger = config.get("on") or config.get(True)
        inputs = trigger["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["freshness_only"]["type"], "boolean")
        self.assertIs(inputs["freshness_only"]["default"], False)
        text = path.read_text()
        cached = text.index('if [ "$CACHED_ONLY" = "true" ]; then')
        freshness = text.index('elif [ "$FRESHNESS_ONLY" = "true" ]; then')
        resume = text.index('elif [ "$RESUME" = "true" ]; then')
        self.assertLess(cached, freshness)
        self.assertLess(freshness, resume)
        self.assertIn("python scripts/v3_refresh_dashboard.py --freshness-only", text)
        self.assertIn("FRESHNESS_ONLY: ${{ inputs.freshness_only }}", text)


class FreshnessOnlyPlanTests(unittest.TestCase):
    """source_reconciliation_plan: due paid repair scopes defer, normal paths unchanged."""

    def test_normal_mode_still_plans_due_paid_repairs(self):
        generation = refresh.CONFIG["reconciliation_generation"]
        cases = (
            ({"reconciliation": {}}, ("approved_backfill", 240)),
            ({"reconciliation": {"generation": generation,
                                 "integrity_manifest_clean_at_utc": refresh.stamp()},
              "source_errors": {"holder_integrity": {"error": "missing inflow"}}},
             ("integrity", 720)),
        )
        for source_state, expected in cases:
            state = {**source_state}
            with self.subTest(state=source_state, expected=expected):
                self.assertEqual(
                    refresh.source_reconciliation_plan(state, transfer_spec(), False),
                    expected)

    def test_normal_mode_still_plans_other_collector_daily_repair(self):
        spec = {"key": "network_collector", "query_id": 123, "repair_lookback_hours": 48}
        self.assertEqual(
            refresh.source_reconciliation_plan({"reconciliation": {}}, spec, True),
            ("daily", 48))

    def test_freshness_mode_defers_due_repairs_with_durable_evidence(self):
        generation = refresh.CONFIG["reconciliation_generation"]
        cases = (
            ({"reconciliation": {}},
             {"key": "xnet_transfers", "mode": "approved_backfill", "lookback_hours": 240}),
            ({"reconciliation": {"generation": generation,
                                 "integrity_manifest_clean_at_utc": refresh.stamp()},
              "source_errors": {"holder_integrity": {"error": "missing inflow"}}},
             {"key": "xnet_transfers", "mode": "integrity", "lookback_hours": 720}),
        )
        for source_state, expected in cases:
            with self.subTest(expected=expected):
                state = {"freshness_only_recovery": {"deferred_repairs": []}, **source_state}
                self.assertEqual(
                    refresh.source_reconciliation_plan(
                        state, transfer_spec(), False, allow_paid=False),
                    (None, None))
                recorded = state["freshness_only_recovery"]["deferred_repairs"]
                self.assertEqual(len(recorded), 1)
                self.assertEqual({field: recorded[0][field]
                                  for field in ("key", "mode", "lookback_hours")}, expected)
                self.assertIn("deferred_at_utc", recorded[0])

    def test_freshness_mode_defers_other_collector_daily_repair(self):
        spec = {"key": "network_collector", "query_id": 123, "repair_lookback_hours": 48}
        state = {"freshness_only_recovery": {"deferred_repairs": []}}
        self.assertEqual(
            refresh.source_reconciliation_plan(state, spec, True, allow_paid=False),
            (None, None))
        recorded = state["freshness_only_recovery"]["deferred_repairs"]
        self.assertEqual(recorded, [{"key": "network_collector", "mode": "daily",
                                     "lookback_hours": 48, "deferred_at_utc": recorded[0]["deferred_at_utc"]}])

    def test_freshness_mode_records_a_deferral_once_and_stays_silent_when_free(self):
        state = {"freshness_only_recovery": {"deferred_repairs": []}, "reconciliation": {}}
        for _ in range(2):
            self.assertEqual(
                refresh.source_reconciliation_plan(state, transfer_spec(), False,
                                                   allow_paid=False),
                (None, None))
        self.assertEqual(len(state["freshness_only_recovery"]["deferred_repairs"]), 1)
        settled = {"freshness_only_recovery": {"deferred_repairs": []},
                   "reconciliation": {"generation": refresh.CONFIG["reconciliation_generation"]}}
        self.assertEqual(
            refresh.source_reconciliation_plan(settled, transfer_spec(), False,
                                               allow_paid=False),
            (None, None))
        self.assertEqual(settled["freshness_only_recovery"]["deferred_repairs"], [])


class FreshnessOnlyManifestTests(unittest.TestCase):
    """reconcile_historical_manifest: free probes stay, paid recovery defers."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        (self.work / "data/current").mkdir(parents=True)
        # Route any state write into scratch: unit tests never stage the
        # repository's runtime journal.
        (self.work / "state.json").write_text("{}")

    @contextmanager
    def scratch_state(self):
        """Patch ROOT/STATE/HEALTH into scratch so tests never write the repo."""
        with ExitStack() as stack:
            stack.enter_context(patch.object(refresh, "ROOT", self.work))
            stack.enter_context(patch.object(refresh, "STATE", self.work / "state.json"))
            stack.enter_context(patch.object(refresh, "HEALTH", self.work / "health.json"))
            yield self.work

    def test_freshness_defers_paid_recovery_and_keeps_free_probes(self):
        state = authorized_state()
        with patch("v3_indexer_lag.probe") as indexer_probe, \
             patch("v3_holder_seed_review.probe") as seed_probe, \
             patch("v3_autonomous_reconcile.owner_seed_day", return_value="2026-10-01"), \
             patch("v3_autonomous_reconcile.repair_days") as repair_days, \
             patch("v3_autonomous_reconcile.recover_additive_days") as recover, \
             patch.object(refresh, "checkpoint"), \
             patch.object(refresh, "execute") as execute, \
             patch.object(refresh, "export_source") as export:
            result = refresh.reconcile_historical_manifest(state, {}, allow_paid=False)
        self.assertFalse(result)
        indexer_probe.assert_called_once()
        seed_probe.assert_not_called()
        repair_days.assert_not_called()
        recover.assert_not_called()
        execute.assert_not_called()
        export.assert_not_called()
        self.assertFalse(state.get("paused", False))
        self.assertEqual(state["reconciliation"]["manifest_deferred_reason"],
                         refresh.FRESHNESS_PAID_DEFERRED)

    def test_freshness_keeps_holder_seed_probe_when_integrity_is_degraded(self):
        state = authorized_state()
        state["source_errors"] = {"holder_integrity": {"error": "missing inflow"}}
        with patch("v3_indexer_lag.probe"), \
             patch("v3_holder_seed_review.probe") as seed_probe, \
             patch("v3_autonomous_reconcile.owner_seed_day", return_value="2026-10-01"), \
             patch("v3_autonomous_reconcile.repair_days"), \
             patch.object(refresh, "checkpoint"), \
             patch.object(refresh, "execute") as execute, \
             patch.object(refresh, "export_source"):
            result = refresh.reconcile_historical_manifest(state, {}, allow_paid=False)
        self.assertFalse(result)
        seed_probe.assert_called_once()
        execute.assert_not_called()
        self.assertEqual(state["reconciliation"]["manifest_deferred_reason"],
                         refresh.FRESHNESS_PAID_DEFERRED)

    def test_freshness_defers_a_non_autonomous_paid_manifest_scan(self):
        state = {"reconciliation": {}}
        with patch.object(refresh, "execute") as execute, \
             patch.object(refresh, "export_source") as export:
            result = refresh.reconcile_historical_manifest(state, {}, allow_paid=False)
        self.assertFalse(result)
        execute.assert_not_called()
        export.assert_not_called()
        self.assertEqual(state["reconciliation"]["manifest_deferred_reason"],
                         refresh.FRESHNESS_PAID_DEFERRED)

    def test_normal_mode_still_plans_and_executes_the_weekly_manifest(self):
        state = {"reconciliation": {}}
        record = {"execution_id": "exec-manifest", "query_id": 8893945,
                  "query_parameters": {"lookback_days": 30},
                  "execution_cost_credits": 0.3}
        with self.scratch_state(), \
             patch.object(refresh, "execute", return_value=record) as execute, \
             patch.object(refresh, "export_source", return_value=100) as export, \
             patch.object(refresh, "checkpoint"):
            self.assertFalse(refresh.reconcile_historical_manifest(state, {}))
        # The paid manifest path remains reachable exactly as before freshness
        # mode existed; the scratch fixture lacks a canonical ledger, so the
        # subsequent verification degrades the source honestly instead.
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(execute.call_args.args[0]["key"], "reconciliation_manifest")
        self.assertEqual(execute.call_args.args[1], {"lookback_days": 30})
        self.assertEqual(export.call_count, 1)
        self.assertIn("historical_reconciliation", state["source_errors"])
        self.assertNotIn("manifest_deferred_reason", state["reconciliation"])

    def test_normal_autonomous_mode_still_reaches_the_day_repair_planner(self):
        state = authorized_state()
        with self.scratch_state(), \
             patch("v3_indexer_lag.probe"), \
             patch("v3_autonomous_reconcile.owner_seed_day", return_value="2026-10-01"), \
             patch("v3_autonomous_reconcile.repair_days", return_value=[]) as repair_days, \
             patch.object(refresh, "checkpoint"):
            self.assertFalse(refresh.reconcile_historical_manifest(state, {}))
        repair_days.assert_called_once()
        self.assertNotIn("manifest_deferred_reason", state["reconciliation"])


class FreshnessOnlyRunTests(unittest.TestCase):
    """Orchestration-level opt-in behavior driven through main()."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)

    def work_root(self):
        root = self.work / "repo"
        (root / "data/current").mkdir(parents=True, exist_ok=True)
        return root

    def base_state(self):
        return {"schema_version": 1, "paused": False, "queries": {},
                "pending_executions": {}, "source_errors": {}}

    def run_refresh(self, state, argv=(), records=None, execute=None):
        calls, submitted = make_execute(records or {})
        submit = execute or submitted
        root = self.work_root()
        state_path = self.work / "v3_refresh_state.json"
        state_path.write_text(json.dumps(state))
        status = Mock(return_value={"state": "QUERY_STATE_COMPLETED",
                                    "execution_cost_credits": 0.5})
        with patch.object(refresh, "ROOT", root), \
             patch.object(refresh, "STATE", state_path), \
             patch.object(refresh, "HEALTH", root / "data/current/xnet_chain_health.json"), \
             patch.object(refresh, "load_authoritative_state", return_value=deepcopy(state)), \
             patch.object(refresh, "usage_guard", return_value=dict(BILLING)), \
             patch.object(refresh, "api", status), \
             patch.object(refresh, "publish"), \
             patch.object(refresh, "run"), \
             patch.object(refresh, "validate_hot_transfers"), \
             patch.object(refresh, "reduce_atomically"), \
             patch.object(refresh, "export_source") as export, \
             patch.object(refresh, "execute", submit), \
             patch("sys.argv", ["refresh", *argv] if argv else ["refresh"]):
            code = refresh.main()
        return {"code": code, "state": json.loads(state_path.read_text()),
                "export_source": export, "api": status,
                "execute_calls": calls}

    def assert_free_api_only(self, api_mock):
        for call in api_mock.call_args_list:
            path = call.args[0]
            self.assertTrue(path.startswith("execution/") and path.endswith("/status"), path)
        # Never an execute or results transport, even with a pending known ID.
        self.assertFalse(any("/execute" in call.args[0] or "/results" in call.args[0]
                             for call in api_mock.call_args_list))

    def test_ordinary_hot_gap_runs_and_due_repairs_defer(self):
        """13h hot lookback (8h19m gap + untouched 4h overlap) is allowed; BBB,
        the due approved_backfill and the manifest scan defer with evidence."""
        state = self.base_state()
        state["queries"] = {
            "xnet_transfers": fresh_source_state(6, completed_at(8 + 19 / 60)),
            "bbb_dex": fresh_source_state(26, completed_at(48)),
            **fresh_chart_state(),
        }
        records = {"xnet_transfers": transfer_record({"lookback_hours": 13})}
        result = self.run_refresh(state, ["--freshness-only"], records=records)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["execute_calls"],
                         [("xnet_transfers", {"lookback_hours": 13})])
        self.assertEqual(result["export_source"].call_count, 1)
        final = result["state"]
        self.assertEqual(final["queries"]["xnet_transfers"]["execution_id"], "exec-transfer")
        self.assertNotIn("exec-bbb", final["queries"].get("bbb_dex", {}).get("execution_id") or "")
        self.assertEqual(final["pending_executions"], {})
        self.assertEqual(final["reconciliation"]["manifest_deferred_reason"],
                         refresh.FRESHNESS_PAID_DEFERRED)
        self.assertEqual([{"key": "xnet_transfers", "mode": "approved_backfill",
                           "lookback_hours": 240,
                           "deferred_at_utc": final["freshness_only_recovery"]
                           ["deferred_repairs"][0]["deferred_at_utc"]}],
                         final["freshness_only_recovery"]["deferred_repairs"])
        self.assertIn("completed_at_utc", final["freshness_only_recovery"])
        self.assertEqual(final["consumed_executions"]["exec-transfer"]["effect"],
                         "non_destructive_hot_union")

    def test_wide_hot_gap_defers_to_day_recovery_without_any_execution(self):
        """A raw gap of 13h makes lookback 17h+ (beyond 12h gap + 4h overlap);
        the fence defers it honestly and submits nothing."""
        state = self.base_state()
        state["queries"] = {
            "xnet_transfers": fresh_source_state(6, completed_at(13)),
            "bbb_dex": fresh_source_state(26, refresh.stamp()),
            **fresh_chart_state(),
        }
        result = self.run_refresh(state, ["--freshness-only"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["execute_calls"], [])
        self.assertEqual(result["export_source"].call_count, 0)
        issue = result["state"]["source_errors"]["xnet_transfers"]
        self.assertIn("routed to bounded, separately quoted UTC-day recovery", issue["error"])
        self.assertEqual(issue["retry_kind"], "bounded_day_catchup")
        self.assertIn("completed_at_utc", result["state"]["freshness_only_recovery"])

    def test_only_the_five_approved_charts_run_within_the_per_run_bound(self):
        state = self.base_state()
        state["queries"] = {
            "xnet_transfers": fresh_source_state(6),
            "bbb_dex": fresh_source_state(26),
        }  # every chart missing from queries => due
        records = {spec["key"]: chart_record(spec["key"])
                   for spec in refresh.CONFIG["presentation"]}
        result = self.run_refresh(state, ["--freshness-only"], records=records)
        self.assertEqual(result["code"], 0)
        self.assertEqual([key for key, _ in result["execute_calls"]], CHART_KEYS)
        executed = {key for key, _ in result["execute_calls"]}
        self.assertEqual(executed, set(CHART_KEYS))
        self.assertLessEqual(len(executed), refresh.CONFIG["max_presentation_queries_per_run"])

    def test_known_pending_out_of_scope_chart_stays_durable_and_metadata_reconciled(self):
        """Strict allowlist: a pending execution for a chart outside the
        approved scope is never paid-processed by this mode. Its durable ID
        still receives the free startup metadata reconciliation and nothing
        more — no result export, no new execution, no cancellation."""
        entry = pending_entry("commercial", 8895089, {}, "exec-commercial")
        state = self.base_state()
        state["queries"] = fresh_chart_state()
        state["queries"]["xnet_transfers"] = fresh_source_state(6)
        state["pending_executions"] = {"commercial": entry}
        result = self.run_refresh(state, ["--freshness-only"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["execute_calls"], [])
        self.assertEqual(result["export_source"].call_count, 0)
        self.assert_free_api_only(result["api"])
        final = result["state"]
        stored = final["pending_executions"]["commercial"]
        self.assertTrue(entry.items() <= stored.items())
        self.assertEqual(stored.get("terminal_status", {}).get("state"),
                         "QUERY_STATE_COMPLETED")
        self.assertNotIn("exec-commercial", final.get("consumed_executions", {}))
        # The out-of-scope chart was not refreshed: no execution, no new clock.
        self.assertNotIn("execution_id", final["queries"]["commercial"])

    def test_known_pending_bbb_execution_is_never_paid_processed_in_freshness_mode(self):
        """The BBB collector is outside the approved scope: its pending ID
        stays pending, its result is never exported, and no new work is
        submitted; the free status poll metadata-reconciles the ID only."""
        entry = pending_entry("bbb_dex", 8894132, {"lookback_hours": 26}, "exec-bbb")
        state = self.base_state()
        state["queries"] = fresh_chart_state()
        state["queries"]["xnet_transfers"] = fresh_source_state(6)
        state["pending_executions"] = {"bbb_dex": entry}
        result = self.run_refresh(state, ["--freshness-only"])
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["execute_calls"], [])
        self.assertEqual(result["export_source"].call_count, 0)
        self.assert_free_api_only(result["api"])
        final = result["state"]
        stored = final["pending_executions"]["bbb_dex"]
        self.assertTrue(entry.items() <= stored.items())
        self.assertNotIn("exec-bbb", final.get("consumed_executions", {}))
        # The out-of-scope collector was not refreshed: no execution, no new
        # accepted result clock (previously a 26h lookback would have landed).
        self.assertNotIn("bbb_dex", final["queries"])
        self.assertNotIn("bbb_dex", final["queries"])

    def test_wide_pending_transfer_window_defers_before_execute_and_export(self):
        """A pending transfer collected with a 168h window is preserved intact
        and neither re-executed nor exported by the freshness-only run."""
        entry = pending_entry("xnet_transfers", 8894037, {"lookback_hours": 168},
                              "exec-transfer-wide")
        state = self.base_state()
        state["queries"] = fresh_chart_state()
        state["pending_executions"] = {"xnet_transfers": entry}
        result = self.run_refresh(state, ["--freshness-only"], records={})
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["execute_calls"], [])
        self.assertEqual(result["export_source"].call_count, 0)
        self.assert_free_api_only(result["api"])
        final = result["state"]
        stored = final["pending_executions"]["xnet_transfers"]
        # The durable pending entry must survive entirely (metadata-only
        # reconciliation may add terminal evidence, never remove fields).
        self.assertTrue(entry.items() <= stored.items())
        self.assertEqual(stored.get("terminal_status", {}).get("state"),
                         "QUERY_STATE_COMPLETED")
        issue = final["source_errors"]["xnet_transfers"]
        self.assertIn("exceeds the freshness-only hot fence", issue["error"])
        self.assertEqual(issue["freshness_pending_window_hours"], 168)
        self.assertIn("completed_at_utc", final["freshness_only_recovery"])
        # The due approved_backfill repair is deferred honestly (once), while
        # the wide pending execution itself is NOT recorded as a repair plan.
        self.assertEqual(
            [{"key": "xnet_transfers", "mode": "approved_backfill",
              "lookback_hours": 240,
              "deferred_at_utc": final["freshness_only_recovery"]
              ["deferred_repairs"][0]["deferred_at_utc"]}],
            final["freshness_only_recovery"]["deferred_repairs"])

    def test_no_freshness_flag_runs_the_equivalent_normal_scope(self):
        """Identical inputs without the flag: transfers hot collection, BBB
        collector, and the network/commercial charts all proceed normally.
        Reconciliation clocks are current so the due manifest only proves the
        fixture, not the flag's scope."""
        state = self.base_state()
        state["queries"] = {
            "xnet_transfers": fresh_source_state(6, completed_at(8 + 19 / 60)),
            "bbb_dex": fresh_source_state(26, completed_at(48)),
            **fresh_chart_state(),
        }  # network/commercial then overridden below to be due again
        for key in OUT_OF_ALLOWLIST_CHART_KEYS:
            state["queries"].pop(key)
        state["reconciliation"] = {
            "generation": refresh.CONFIG["reconciliation_generation"],
            "last_daily_manifest_utc": refresh.stamp(),
            "last_weekly_manifest_utc": refresh.stamp(),
        }
        records = {"xnet_transfers": transfer_record({"lookback_hours": 13}),
                   "bbb_dex": bbb_record({"lookback_hours": 50}),
                   **{key: chart_record(key) for key in OUT_OF_ALLOWLIST_CHART_KEYS}}
        result = self.run_refresh(state, records=records)
        self.assertEqual(result["code"], 0)
        executed = {key: params for key, params in result["execute_calls"]}
        self.assertEqual(set(executed), {"xnet_transfers", "bbb_dex", "network", "commercial"})
        self.assertEqual(executed["xnet_transfers"], {"lookback_hours": 13})
        self.assertEqual(executed["bbb_dex"], {"lookback_hours": 50})
        self.assertNotIn("freshness_only_recovery", result["state"])
        self.assertNotIn("manifest_deferred_reason", result["state"]["reconciliation"])


if __name__ == "__main__":
    unittest.main()
