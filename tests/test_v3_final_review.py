"""Independent final review of the assembled V3 refresh path.

One test per invariant the integration must hold. The three findings raised by this review
and the deployment contract are pinned as regressions with their resolved behaviour:
A) a cache hit copies the telemetry of the paid export it reuses; B) a failed presentation
acceptance rolls its local clock/marker pair back; C) a result-cache refusal quarantines only
the refusing source. Nothing here opens the network, submits Dune work, or touches the
worktree's ``data/``: every fixture owns a temporary repo/state/health path.

Invariants pinned here:
  * ``--cached-only`` publishes independent API data and never opens a Dune route;
  * a paused chain never evaluates admission, submits, exports, or publishes, yet still
    reconciles known execution ids so an accepted-but-lost execution is not duplicated;
  * a durability barrier stops the run before any further submission and never publishes a
    compensating snapshot;
  * an ambiguous submission (no execution id) blocks every new submission globally;
  * soft source errors back off and recover automatically, manual quarantine never does;
  * source acceptance publishes the query clock and its consumption marker in one commit;
  * a cache-hit reuse copies the wire telemetry recorded when that export was first paid,
    and never invents a charge when no telemetry is on record;
  * a presentation acceptance failure rolls its local clock/marker pair back (retaining the
    durable execution id) instead of persisting a clock without its consumption marker;
  * a result-cache refusal during export quarantines just that source, without pausing the chain;
  * a push deployment to main runs ``--cached-only``, so deploys never submit paid Dune work.
"""
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_LOADER = importlib.util.spec_from_file_location("refresh", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(_LOADER)
_LOADER.loader.exec_module(refresh)

EXEC = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def source_spec(key="xnet_transfers", **overrides):
    """A real configured source spec, so ids/params mirror production."""
    return {**next(s for s in refresh.CONFIG["sources"] if s["key"] == key), **overrides}


def presentation_spec(key="current", **overrides):
    return {**next(s for s in refresh.CONFIG["presentation"] if s["key"] == key), **overrides}


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repo = self.directory / "repo"
        (self.repo / "data/current").mkdir(parents=True)
        self.state_path = self.directory / "state.json"
        self.health_path = self.directory / "health.json"
        for name, value in (("ROOT", self.repo), ("STATE", self.state_path), ("HEALTH", self.health_path)):
            patcher = patch.object(refresh, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        publish_patcher = patch.object(refresh, "publish")
        self.publish = publish_patcher.start()
        self.addCleanup(publish_patcher.stop)

    def write_journal(self, value):
        self.state_path.write_text(json.dumps(value))
        return value

    def journal(self):
        return json.loads(self.state_path.read_text())

    def config(self, **overrides):
        """Real configuration with the network/market and Dune lanes narrowed for the fixture."""
        return {**refresh.CONFIG, "reconciliation": None, "presentation": [], "sources": [], **overrides}

    # ---------------------------------------------------------------- cached-only

    def test_cached_only_publishes_independent_data_and_never_opens_a_dune_route(self):
        paused = self.write_journal({"schema_version": 1, "queries": {}, "paused": True,
                                     "last_error": "cost anomaly"})
        with patch("sys.argv", ["refresh", "--cached-only"]), \
             patch.object(refresh, "load_authoritative_state", return_value=copy.deepcopy(paused)), \
             patch.object(refresh, "run") as run, patch.object(refresh, "api") as api, \
             patch.object(refresh, "execute") as execute, patch.object(refresh, "export_source") as export, \
             patch.object(refresh, "usage_guard") as guard, patch.object(refresh, "result_cache") as cache, \
             patch.object(refresh, "reconcile_historical_manifest") as manifest:
            self.assertEqual(refresh.main(), 0)
        for name, mock in (("api", api), ("execute", execute), ("export_source", export),
                           ("usage_guard", guard), ("result_cache", cache),
                           ("reconcile_historical_manifest", manifest)):
            with self.subTest(dune_route=name):
                mock.assert_not_called()
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         ["v3_sync_network.py", "v3_collect_market.py", "v3_build_live_bundle.py"])
        self.publish.assert_called_once_with()  # one acceptance publication, no state-only checkpoint
        committed = self.journal()
        self.assertTrue(committed["paused"])  # the exact chain hold is retained
        self.assertEqual(committed["last_error"], "cost anomaly")
        self.assertNotIn("consumed_executions", committed)

    # ----------------------------------------------------------------- pause hold

    def test_pause_blocks_every_spending_path_and_publishes_nothing(self):
        paused = self.write_journal({"schema_version": 1, "queries": {}, "paused": True, "last_error": "x"})
        with patch("sys.argv", ["refresh"]), patch.dict("os.environ", {"EVENT": ""}), \
             patch.object(refresh, "load_authoritative_state", return_value=copy.deepcopy(paused)), \
             patch.object(refresh, "run") as run, patch.object(refresh, "api") as api, \
             patch.object(refresh, "execute") as execute, patch.object(refresh, "export_source") as export, \
             patch.object(refresh, "usage_guard") as guard, patch.object(refresh, "result_cache") as cache:
            with self.assertRaisesRegex(SystemExit, "paused"):
                refresh.main()
        run.assert_not_called()  # no collection without a schedule/push event
        api.assert_not_called()
        execute.assert_not_called()
        export.assert_not_called()
        guard.assert_not_called()  # admission is not even evaluated while paused
        cache.assert_not_called()
        self.publish.assert_not_called()

    def test_pause_still_reconciles_known_execution_ids_without_new_submissions(self):
        pending = {"execution_id": EXEC, "key": "xnet_transfers",
                   "query_id": source_spec()["query_id"], "query_parameters": {"lookback_hours": 2},
                   "submitted_at_utc": "2026-10-18T00:00:00Z", "timeout_seconds": 240, "billing": {}}
        paused = self.write_journal({"schema_version": 1, "queries": {}, "paused": True,
                                     "last_error": "x", "pending_executions": {"xnet_transfers": pending}})
        calls = []
        def api(path, payload=None, include_wire_size=False):
            calls.append((path, payload))
            return {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                    "execution_ended_at": "2026-10-18T00:02:00Z",
                    "result_metadata": {"total_row_count": 1}}
        with patch("sys.argv", ["refresh"]), patch.dict("os.environ", {"EVENT": ""}), \
             patch.object(refresh, "load_authoritative_state", return_value=copy.deepcopy(paused)), \
             patch.object(refresh, "run") as run, patch.object(refresh, "api", api), \
             patch.object(refresh, "execute") as execute, patch.object(refresh, "export_source") as export, \
             patch.object(refresh, "usage_guard") as guard, patch.object(refresh, "result_cache") as cache:
            with self.assertRaisesRegex(SystemExit, "paused"):
                refresh.main()
        self.assertEqual([path for path, _ in calls],
                         [f"execution/{EXEC}/status"])  # reconciled by id, never resubmitted
        run.assert_not_called()
        execute.assert_not_called()
        export.assert_not_called()
        guard.assert_not_called()
        cache.assert_not_called()

    # ------------------------------------------------------- durability boundaries

    def test_durability_barrier_halts_the_run_before_any_further_submission(self):
        self.write_journal({"schema_version": 1, "queries": {}})
        before = self.state_path.read_bytes()
        record = {"execution_id": EXEC, "query_id": source_spec()["query_id"],
                  "query_parameters": {"lookback_hours": 2}, "execution_cost_credits": 0.1,
                  "completed_at_utc": "2026-10-18T00:00:00Z",
                  "result_metadata": {"total_row_count": 1}}
        config = self.config(sources=[source_spec(output="state/hot.json"),
                                      source_spec("bbb_dex", output="state/bbb.json")])
        with patch("sys.argv", ["refresh"]), patch.object(refresh, "CONFIG", config), \
             patch.object(refresh, "load_authoritative_state", return_value={"schema_version": 1, "queries": {}}), \
             patch.object(refresh, "run"), patch.object(refresh, "api") as api, \
             patch.object(refresh, "usage_guard", return_value={}), \
             patch.object(refresh, "execute", return_value=dict(record)) as execute, \
             patch.object(refresh, "export_source",
                          side_effect=refresh.StateError("publication durability failed")) as export:
            self.assertEqual(refresh.main(), 1)
        self.assertEqual(execute.call_count, 1)  # the second due source was never submitted
        self.assertEqual(export.call_count, 1)
        api.assert_not_called()
        self.publish.assert_not_called()  # never a compensating publication
        self.assertEqual(self.state_path.read_bytes(), before)  # and no unsound local commit

    def test_ambiguous_submission_blocks_every_new_submission(self):
        state = {"queries": {}, "pending_executions": {"xnet_transfers": {
            "key": "xnet_transfers", "query_id": source_spec()["query_id"], "execution_id": None,
            "query_parameters": {}, "submitted_at_utc": "2026-10-18T00:00:00Z",
            "timeout_seconds": 240, "billing": {}}}}
        before = copy.deepcopy(state)
        cases = ((source_spec("bbb_dex", output="state/bbb.json"), "all new submissions blocked"),
                 (source_spec(output="state/hot.json"), "unresolved submission without an ID"))
        for spec, expected in cases:
            with self.subTest(key=spec["key"]):
                with patch.object(refresh, "api") as api, patch.object(refresh, "usage_guard") as guard:
                    with self.assertRaises(refresh.ExecutionPending) as ctx:
                        refresh.tracker(state).execute(spec, {"lookback_hours": 2}, True, 240)
                self.assertIn(expected, str(ctx.exception))
                api.assert_not_called()  # no POST, so no speculative replacement
                guard.assert_not_called()  # and no admission evaluation for a blocked submission
        self.assertEqual(state, before)  # the durable intent is retained untouched

    # ------------------------------------------------------- errors and quarantine

    def test_soft_source_error_backs_off_then_recovers_without_manual_review(self):
        state = {}
        refresh.record_source_issue(state, "xnet_transfers", "upstream 503")
        issue = state["source_errors"]["xnet_transfers"]
        self.assertEqual(issue["consecutive_failures"], 1)
        self.assertFalse(issue.get("manual_reconciliation_required", False))
        self.assertTrue(refresh.source_in_backoff(state, "xnet_transfers"))
        issue["next_retry_at_utc"] = (refresh.now() - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        self.assertFalse(refresh.source_in_backoff(state, "xnet_transfers"))  # automatic recovery, no human

    def test_manual_quarantine_never_auto_retries_even_with_work_pending(self):
        pending_only = {"pending_executions": {"xnet_transfers": {"execution_id": EXEC}}}
        self.assertFalse(refresh.source_in_backoff(pending_only, "xnet_transfers"))  # known ids are reconciled
        state = copy.deepcopy(pending_only)
        refresh.quarantine_source(state, "xnet_transfers", "cost overrun needs review")
        issue = state["source_errors"]["xnet_transfers"]
        self.assertTrue(issue["manual_reconciliation_required"])
        self.assertFalse(issue["automatic_recheck"])
        self.assertIsNone(issue["next_retry_at_utc"])
        self.assertTrue(refresh.source_in_backoff(state, "xnet_transfers"))  # manual clearance only

    # ---------------------------------------------------- acceptance atomicity

    def test_source_acceptance_publishes_the_clock_and_its_consumption_together(self):
        journal = self.write_journal({"schema_version": 1, "queries": {}})
        record = {"execution_id": EXEC, "query_id": source_spec()["query_id"],
                  "query_parameters": {"lookback_hours": 2}, "execution_cost_credits": 0.1,
                  "result_metadata": {"total_row_count": 1}}
        published = []
        def full_publish(*args, **kwargs):
            if not args and not kwargs:  # the single acceptance publication
                published.append(self.journal())
        self.publish.side_effect = full_publish
        with patch("sys.argv", ["refresh"]), \
             patch.object(refresh, "CONFIG", self.config(sources=[source_spec(output="state/hot.json")])), \
             patch.object(refresh, "load_authoritative_state", return_value=copy.deepcopy(journal)), \
             patch.object(refresh, "run"), patch.object(refresh, "api") as api, \
             patch.object(refresh, "usage_guard", return_value={}), \
             patch.object(refresh, "execute", return_value=dict(record)), \
             patch.object(refresh, "export_source", return_value=100), \
             patch.object(refresh, "validate_hot_transfers"), patch.object(refresh, "reduce_atomically"):
            self.assertEqual(refresh.main(), 0)
        api.assert_not_called()
        self.assertTrue(published, "acceptance must publish")
        self.assertEqual(published[0]["queries"]["xnet_transfers"]["execution_id"], EXEC)
        self.assertEqual(published[0]["consumed_executions"][EXEC]["effect"],
                         "non_destructive_hot_union")  # clock and marker in the same generation
        self.assertNotIn("xnet_transfers", published[0].get("pending_executions", {}))
        committed = self.journal()
        self.assertEqual(committed["queries"]["xnet_transfers"], record)
        self.assertEqual(committed["consumed_executions"][EXEC]["query_id"], source_spec()["query_id"])
        self.assertEqual(committed.get("pending_executions", {}), {})

    def test_cached_reuse_copies_prior_wire_telemetry_and_never_invents_it(self):
        """Finding A (resolved on main): a spend-free cache hit copies the wire/credit telemetry
        recorded when that export was first paid, from the execution's history entry - and never
        fabricates a charge for an execution whose telemetry is not on record."""
        spec = {"output": "state/hot.json", "performance": source_spec()["performance"]}
        base = {"execution_id": EXEC, "query_id": source_spec()["query_id"],
                "query_parameters": {"lookback_hours": 2}, "execution_cost_credits": 0.4,
                "result_metadata": {"total_row_count": 1, "column_names": ["a_b", "day"],
                                    "datapoint_count": 4, "total_result_set_bytes": 1500000}}
        rows = [{"a_b": 1, "day": "2026-10-18"}]
        cache = Mock()
        cache.lookup.return_value = {"execution_id": EXEC, "result_rows": rows, "columns": ["a_b", "day"]}
        state = {"queries": {}, "pending_executions": {}, "result_exports": {"schema_version": 1, "requests": {}},
                 "execution_history": [{"execution_id": EXEC, "export_wire_bytes": 4096,
                                        "estimated_export_credits": 2.5},
                                       {"execution_id": "OTHER", "export_wire_bytes": 999,
                                        "estimated_export_credits": 9.9}]}
        record = copy.deepcopy(base)
        with patch.object(refresh, "result_cache", return_value=cache), \
             patch.object(refresh, "usage_guard") as guard, patch.object(refresh, "api") as api, \
             patch.object(refresh, "save") as save:
            self.assertEqual(refresh.export_source(record, spec, 3000, max_bytes=2000000, state=state), 2996)
        guard.assert_not_called()  # reuse is spend-free
        api.assert_not_called()  # and never re-downloads
        self.assertEqual(record["export_points"], 4)
        self.assertEqual(record["export_wire_bytes"], 4096)  # copied from this execution's paid history
        self.assertEqual(record["estimated_export_credits"], 2.5)  # not the OTHER entry's 9.9
        self.assertEqual(save.call_args.args[1]["result"]["rows"], rows)
        # With no recorded telemetry the reuse is still spend-free and invents no charge.
        plain = copy.deepcopy(base)
        state["execution_history"] = [entry for entry in state["execution_history"]
                                      if entry["execution_id"] == "OTHER"]
        with patch.object(refresh, "result_cache", return_value=cache), \
             patch.object(refresh, "usage_guard"), patch.object(refresh, "api"), patch.object(refresh, "save"):
            self.assertEqual(refresh.export_source(plain, spec, 3000, max_bytes=2000000, state=state), 2996)
        self.assertEqual(plain["export_points"], 4)
        self.assertNotIn("export_wire_bytes", plain)
        self.assertNotIn("estimated_export_credits", plain)

    def run_presentation(self, result_metadata, queries=None):
        current = presentation_spec()
        journal = self.write_journal({"schema_version": 1, "queries": queries or {}})
        calls = []
        def api(path, payload=None, include_wire_size=False):
            calls.append(path)
            if path.endswith("/execute"):
                return {"execution_id": EXEC}
            if path.endswith("/status"):
                return {"state": "QUERY_STATE_COMPLETED", "execution_cost_credits": 0.1,
                        "execution_started_at": "2026-10-18T00:00:00Z",
                        "execution_ended_at": "2026-10-18T00:01:00Z",
                        "result_metadata": result_metadata}
            raise AssertionError(f"unexpected Dune route {path}")
        with patch("sys.argv", ["refresh"]), \
             patch.object(refresh, "CONFIG", self.config(presentation=[current])), \
             patch.object(refresh, "load_authoritative_state", return_value=copy.deepcopy(journal)), \
             patch.object(refresh, "run"), patch.object(refresh, "api", api), \
             patch.object(refresh, "usage_guard", return_value={}):
            self.assertEqual(refresh.main(), 0)
        return current, calls

    def test_presentation_acceptance_publishes_the_clock_and_its_consumption_together(self):
        current, calls = self.run_presentation({"total_row_count": 1})
        committed = self.journal()
        self.assertIn(f"query/{current['query_id']}/execute", calls)
        self.assertEqual(committed["queries"]["current"]["execution_id"], EXEC)
        self.assertEqual(committed["consumed_executions"][EXEC]["effect"], "presentation_terminal_success")
        self.assertNotIn("current", committed.get("pending_executions", {}))
        self.assertNotIn("current", committed.get("presentation_errors", {}))

    def test_presentation_acceptance_rolls_back_a_failed_local_pair(self):
        """Finding B (resolved on main): the loop snapshots after ``execute`` and rolls its local
        acceptance attempt back when staging fails, so the journal can no longer hold a query
        clock without its consumption marker. The durable execution identity from ``execute``
        survives that rollback and the execution stays pending for the next run; the failure is
        fail-soft (no fabricated backoff for a non-terminal error) and never a global pause."""
        previous = {"query_id": presentation_spec()["query_id"], "execution_id": "PREVIOUS",
                    "completed_at_utc": "2026-01-01T00:00:00Z"}
        self.run_presentation({"total_row_count": "not-an-integer"}, queries={"current": previous})
        committed = self.journal()
        self.assertEqual(committed["queries"]["current"], previous)  # clock rolled back, not advanced
        # stage_consumption creates this lane before it validates, so its absence proves the
        # local acceptance attempt (not just the write order) was rolled back.
        self.assertNotIn("consumed_executions", committed)
        self.assertNotIn(EXEC, committed.get("consumed_executions", {}))  # no invented acceptance
        self.assertIn("current", committed.get("pending_executions", {}))  # retained, not resubmitted
        self.assertEqual([entry["execution_id"] for entry in committed["execution_history"]], [EXEC])
        self.assertFalse(committed["paused"])  # fail-soft, never a global pause
        error = committed["presentation_errors"]["current"]
        self.assertIn("invalid literal", error["error"])
        self.assertIsNone(error["next_retry_at_utc"])  # non-terminal: no fabricated backoff

    def test_cache_refusal_quarantines_the_source_without_a_global_pause(self):
        """Finding C (resolved on main): a result-cache refusal during export is isolated to that
        source (manual-reconciliation quarantine with a checkpoint) instead of pausing the chain,
        and the rest of the run still publishes."""
        current = presentation_spec()
        journal = {"schema_version": 1, "queries": {}}
        record = {"execution_id": EXEC, "query_id": source_spec()["query_id"],
                  "query_parameters": {"lookback_hours": 2}, "execution_cost_credits": 0.1,
                  "completed_at_utc": "2026-10-18T00:00:00Z",
                  "result_metadata": {"total_row_count": 1}}
        refusals = (refresh.CacheError("cache entry binding refused"),
                    refresh.ManualExportReview("no second export"))
        for refusal in refusals:
            with self.subTest(refusal=type(refusal).__name__):
                self.write_journal(copy.deepcopy(journal))
                config = self.config(sources=[source_spec(output="state/hot.json")],
                                     presentation=[current])
                with patch("sys.argv", ["refresh"]), patch.object(refresh, "CONFIG", config), \
                     patch.object(refresh, "load_authoritative_state", return_value=copy.deepcopy(journal)), \
                     patch.object(refresh, "run"), patch.object(refresh, "api") as api, \
                     patch.object(refresh, "usage_guard", return_value={}), \
                     patch.object(refresh, "execute",
                                  side_effect=lambda *a, **k: copy.deepcopy(record)) as execute, \
                     patch.object(refresh, "export_source", side_effect=refusal):
                    self.assertEqual(refresh.main(), 0)
                committed = self.journal()
                issue = committed["source_errors"]["xnet_transfers"]
                self.assertTrue(issue["manual_reconciliation_required"])
                self.assertFalse(issue["automatic_recheck"])
                self.assertIsNone(issue["next_retry_at_utc"])
                self.assertFalse(issue["unresolved_execution"])  # the refusal never claims an unknown ID
                self.assertFalse(committed["paused"])  # no global pause
                self.assertNotIn("xnet_transfers", committed["queries"])  # a refused source keeps no clock
                self.assertEqual([call.args[0]["key"] for call in execute.call_args_list],
                                 ["xnet_transfers", "current"])  # the run continued past the refusal
                self.assertEqual(committed["queries"]["current"]["execution_id"], EXEC)
                self.assertIn(EXEC, committed["consumed_executions"])
                api.assert_not_called()  # execute was stubbed: no provider work in this fixture

    def test_push_deployments_run_cached_only_and_never_submit_paid_work(self):
        """Deployment contract: a push to main must take the cached-only invocation, so a deploy
        can never submit paid Dune work, and the runner still receives the real event name."""
        workflow = yaml.safe_load((ROOT / ".github/workflows/refresh-v3-history.yml").read_text())
        triggers = workflow["on"] if "on" in workflow else workflow[True]  # YAML 1.1 parses `on` as True
        self.assertEqual(triggers["push"]["branches"], ["main"])
        steps = [step for step in workflow["jobs"]["refresh"]["steps"]
                 if "v3_refresh_dashboard.py" in step.get("run", "")]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertIn("push", step["env"]["CACHED_ONLY"])  # push deployments are cached-only
        self.assertIn("EVENT", step["env"])  # the runner needs the real event to retain pauses
        script = step["run"]
        self.assertLess(script.index('if [ "$CACHED_ONLY" = "true" ]'), script.index("--cached-only"))
        self.assertLess(script.index("--cached-only"), script.index("--resume"))


if __name__ == "__main__":
    unittest.main()
