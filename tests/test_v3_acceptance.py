"""Acceptance tests for the single-commit generation publication contract.

These target the helper ``publish_data_generation(state, paths, source_records)``
directly (the block Astra is moving out of ``main``) plus the HTTP ambiguity and
manifest-decision boundaries. No network: every provider call is patched.

Covered invariants:
  * a pre-publish crash restores both in-memory acceptance state and working data;
  * a post-publish StateError preserves the exact candidate and performs no
    compensating write or second publication;
  * Dune execute HTTP 408/500 stay ambiguous end to end (no definitive rejection,
    durable intent retained);
  * a manifest mismatch performs no destructive reduce and stages the consumption
    marker together with the manual-review decision in one commit.
"""
import contextlib
import copy
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_execution_tracker import ExecutionPending, ExecutionTracker

SPEC = {"key": "xnet_transfers", "query_id": 1, "output": "transfers.json",
        "max_run_credits": 1.5, "cadence_minutes": 30, "lookback_hours": 2}
RECORD = {"execution_id": "recover", "query_id": 1,
          "query_parameters": {"lookback_hours": 48}}


def load_refresh():
    spec = importlib.util.spec_from_file_location(
        "acceptance_refresh", ROOT / "scripts/v3_refresh_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AcceptanceTestCase(unittest.TestCase):
    def setUp(self):
        self.refresh = load_refresh()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "data" / "current").mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.health = self.root / "health.json"
        self.fixed = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
        self.config = {**self.refresh.CONFIG, "sources": [SPEC], "presentation": [],
                       "chain_cadence_minutes": 30}

    def stack(self, **overrides):
        """Patch review base globals; caller supplies the mocks it inspects."""
        base = {"ROOT": self.root, "STATE": self.state_path, "HEALTH": self.health,
                "CONFIG": self.config, "run": Mock(), "validate_hot_transfers": Mock()}
        base.update(overrides)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in base.items():
            stack.enter_context(patch.object(self.refresh, name, value))
        return stack

    @staticmethod
    def pending_state():
        return {"queries": {}, "pending_executions": {"xnet_transfers": {
            "key": "xnet_transfers", "query_id": 1, "execution_id": "recover",
            "query_parameters": {"lookback_hours": 48},
            "submitted_at_utc": "2026-10-07T09:59:00Z", "timeout_seconds": 240,
            "billing": {}}}}


class PublishDataGenerationTests(AcceptanceTestCase):
    def test_free_proof_deferral_keeps_paid_identity_and_verified_progress(self):
        from v3_transfer_instruction_recovery import InstructionProofDeferred
        state=self.pending_state()
        def validate(path,current):
            current["transfer_instruction_proofs"]={"tx":{"capture_sha256":"witness"}}
            raise InstructionProofDeferred("Free RPC request bound reached")
        reduce=Mock()
        self.stack(validate_hot_transfers=validate,reduce_atomically=reduce,publish=Mock())
        self.refresh.publish_data_generation(state,{"xnet_transfers":self.root/"transfers.json"},
                                              {"xnet_transfers":(SPEC,RECORD)})
        reduce.assert_not_called()
        self.assertIn("xnet_transfers",state["pending_executions"])
        self.assertNotIn("recover",state.get("consumed_executions",{}))
        self.assertIn("tx",state["transfer_instruction_proofs"])
        issue=state["source_errors"]["xnet_transfers"]
        self.assertTrue(issue["automatic_recheck"])
        self.assertEqual(issue["retry_kind"],"transfer_instruction_proof")
        self.assertFalse(issue.get("manual_reconciliation_required",False))

    def test_bbb_only_acceptance_needs_no_transfer_input_or_transfer_clock(self):
        state = {"queries": {"xnet_transfers": {"execution_id": "last-good"}}}
        bbb = {"key": "bbb_dex", "query_id": 2, "output": "bbb.json"}
        record = {"execution_id": "bbb-known", "query_id": 2, "query_parameters": {"lookback_hours": 26}}
        reduce = Mock()
        self.stack(reduce_atomically=reduce, publish=Mock())
        self.refresh.publish_data_generation(state, {"bbb_dex": self.root / "bbb.json"},
                                             {"bbb_dex": (bbb, record)})
        reduce.assert_called_once_with(None, self.root / "bbb.json")
        self.assertEqual(state["queries"]["xnet_transfers"], {"execution_id": "last-good"})
        self.assertNotIn("chain_completed_at_utc", state)
        self.assertEqual(state["consumed_executions"]["bbb-known"]["effect"], "bbb_trade_union")

    def test_missing_or_ambiguous_transfer_does_not_discard_eligible_bbb(self):
        for message in ("Canonical transfer ledger missing", "Ambiguous duplicate hot transfer locator"):
            with self.subTest(message=message):
                state = self.pending_state()
                bbb = {"key": "bbb_dex", "query_id": 2}
                record = {"execution_id": "bbb-known", "query_id": 2, "query_parameters": {}}
                reduce = Mock()
                self.stack(validate_hot_transfers=Mock(side_effect=self.refresh.ManualReviewRequired(message)),
                           reduce_atomically=reduce, publish=Mock())
                self.refresh.publish_data_generation(state,
                    {"xnet_transfers": self.root / "transfers.json", "bbb_dex": self.root / "bbb.json"},
                    {"xnet_transfers": (SPEC, RECORD), "bbb_dex": (bbb, record)})
                reduce.assert_called_once_with(None, self.root / "bbb.json")
                self.assertIn("xnet_transfers", state["pending_executions"])
                self.assertNotIn("recover", state.get("consumed_executions", {}))
                self.assertTrue(state["source_errors"]["xnet_transfers"]["manual_reconciliation_required"])
                self.assertIn("bbb-known", state["consumed_executions"])

    def test_checkpoint_keeps_financial_debt_after_history_disappears(self):
        state = {"queries": {}, "execution_history": [{"execution_id": "old-unknown", "query_id": 8}]}
        self.stack(publish=Mock())
        self.refresh.checkpoint(state)
        state["execution_history"] = []
        self.refresh.checkpoint(state)
        saved = json.loads(self.state_path.read_text())
        obligation = saved["financial_journal"]["obligations"]["exec:old-unknown"]
        self.assertIsNone(obligation["cost_credits"])
        self.assertTrue(obligation["unresolved"])

    def test_prepublish_crash_restores_in_memory_state_and_data(self):
        state = self.pending_state()
        before = copy.deepcopy(state)
        partial = self.root / "data" / "current" / "partial.json"
        publish = Mock()

        def reduce(*args, **kwargs):
            partial.write_text('{"partial": true}\n')  # reducer mutated canonical data

        def run(script, *args):
            if script == "v3_build_chain_snapshot.py":
                raise RuntimeError("snapshot builder crashed")

        self.stack(run=run, reduce_atomically=reduce, publish=publish)
        with self.assertRaises(RuntimeError):
            self.refresh.publish_data_generation(
                state, {"xnet_transfers": self.root / "transfers.json"},
                {"xnet_transfers": (SPEC, RECORD)})
        self.assertEqual(state, before, "in-memory acceptance state was not reset")
        self.assertFalse(partial.exists(), "working data was not rolled back")
        publish.assert_not_called()

    def test_postpublish_stateerror_preserves_candidate_without_compensating_write(self):
        state = self.pending_state()
        candidate = self.root / "data" / "current" / "candidate.json"
        publish = Mock(side_effect=self.refresh.StateError(
            "Publication not acknowledged; durable intent retained"))

        def reduce(*args, **kwargs):
            candidate.write_text('{"candidate": true}\n')

        self.stack(reduce_atomically=reduce, publish=publish)
        with self.assertRaises(self.refresh.StateError):
            self.refresh.publish_data_generation(
                state, {"xnet_transfers": self.root / "transfers.json"},
                {"xnet_transfers": (SPEC, RECORD)})
        # Durability barrier: the exact candidate survives, no compensating write,
        # and the staged acceptance state is preserved for store recovery.
        self.assertTrue(candidate.exists(), "post-publish candidate was rolled back")
        self.assertEqual(publish.call_count, 1, "a second publication was attempted")
        self.assertIn("recover", state["consumed_executions"])
        self.assertNotIn("xnet_transfers", state.get("pending_executions", {}))
        self.assertIn("chain_completed_at_utc", state)
        saved = json.loads(self.state_path.read_text())
        self.assertIn("recover", saved["consumed_executions"])
        self.assertNotIn("xnet_transfers", saved.get("pending_executions", {}))


class HttpAmbiguityTests(AcceptanceTestCase):
    def test_http_408_and_500_are_ambiguous_never_definitive_rejections(self):
        for code in (408, 500):
            error = urllib.error.HTTPError(
                "https://api.dune.com/api/v1/query/1/execute", code, "error", None, None)
            with self.subTest(code=code), \
                 patch.dict(os.environ, {"DUNE_API_KEY": "test-key"}), \
                 patch.object(self.refresh,"dune_open", side_effect=error):
                with self.assertRaises(RuntimeError) as context:
                    self.refresh.api("query/1/execute", {"performance": "medium"})
                self.assertNotIsInstance(context.exception, self.refresh.SubmissionRejected)

        # Tracker boundary: the same failure must retain a durable ambiguous
        # intent, never record a definitive rejection.
        state = {"queries": {}}
        snapshots = []

        def failing_api(*args, **kwargs):
            raise RuntimeError("Dune HTTP 500 at query/1/execute")

        tracker = ExecutionTracker(
            state, failing_api, lambda: snapshots.append(copy.deepcopy(state)),
            lambda: {}, lambda: self.fixed, lambda seconds: None, "medium")
        with self.assertRaises(ExecutionPending):
            tracker.execute({"key": "current", "query_id": 1, "max_run_credits": 1},
                            {}, True, 90)
        self.assertNotIn("rejected_submissions", state)
        intent = state["pending_executions"]["current"]
        self.assertIsNone(intent["execution_id"])
        self.assertIn("submission_error", intent)
        self.assertTrue(snapshots, "the ambiguous intent was not durably checkpointed")


class ExportAttemptIntegrationTests(AcceptanceTestCase):
    def fixture(self):
        spec = {**SPEC, "output": "state/hot.json", "performance": "medium"}
        record = {"execution_id": "known-result", "query_id": 1, "query_parameters": {},
                  "result_metadata": {"total_row_count": 1, "column_names": ["a"],
                                      "total_result_set_bytes": 1000}}
        spec["operation_approval"] = {"operation_id": "reviewed-read", "kind": "export", "query_id": 1,
            "query_parameters": {}, "execution_id": "known-result", "performance": "medium",
            "shape_version": None, "quote_credits": 0.1, "approval_ref": "fixture: explicit user decision",
            "estimate_evidence": "fixture: wire observations", "approved_at_utc": "2026-10-07T09:00:00Z",
            "valid_until_utc": "2026-10-07T11:00:00Z"}
        state = {"queries": {}, "budget_decision": {"report": {"totals": {
            "limit_credits": 1800, "usage_plus_reserve": 100, "dependencies_total": 10}}}}
        return state, spec, record

    def test_financial_attempt_and_cache_intent_publish_together_before_http_and_meter_exact_read(self):
        state, spec, record = self.fixture()
        snapshots = []
        def publish(**kwargs):
            snapshots.append(json.loads(self.state_path.read_text()))
        def api(path, **kwargs):
            self.assertEqual(path, "execution/known-result/results?limit=10000&allow_partial_results=false")
            before_http = snapshots[-1]
            obligation = before_http["financial_journal"]["obligations"]["reviewed-read"]
            marker = before_http["result_exports"]["requests"][obligation["cache_marker_key"]]
            self.assertEqual(marker["requested_at_utc"], obligation["requested_at_utc"])
            self.assertEqual(marker["status"], "requested")
            self.assertTrue(obligation["unresolved"])
            return {"execution_id": "known-result", "state": "QUERY_STATE_COMPLETED",
                    "result": {"rows": [{"a": 1}]}}, 250
        self.stack(now=lambda: self.fixed, usage_guard=Mock(), export_risk=Mock(return_value=(0.1, ["fixture evidence"])),
                   api=Mock(side_effect=api), publish=publish)
        self.assertEqual(self.refresh.export_source(record, spec, 10, state=state), 9)
        observed = snapshots[-1]["financial_journal"]["obligations"]["reviewed-read"]
        self.assertAlmostEqual(observed["cost_credits"], 250 / 1000000 * self.config["export_credits_per_megabyte"])
        self.assertFalse(observed["unresolved"])
        self.assertEqual(record["export_attempt_id"], "reviewed-read")
        self.assertEqual(record["export_requested_at_utc"], observed["requested_at_utc"])
        before = copy.deepcopy(state["financial_journal"])
        self.refresh.api.reset_mock()
        self.refresh.usage_guard.side_effect = AssertionError("cache hit cannot consult admission")
        self.refresh.export_source(record, spec, 10, state=state)
        self.refresh.api.assert_not_called()
        self.assertEqual(state["financial_journal"], before, "a cache hit must not create another charge")

    def test_export_checkpoint_fault_never_reaches_http(self):
        state, spec, record = self.fixture()
        api = Mock()
        self.stack(now=lambda: self.fixed, usage_guard=Mock(), export_risk=Mock(return_value=(0.1, ["fixture evidence"])),
                   api=api, publish=Mock(side_effect=self.refresh.StateError("uncertain intent publication")))
        with self.assertRaises(self.refresh.StateError):
            self.refresh.export_source(record, spec, 10, state=state)
        api.assert_not_called()
        self.assertIn("reviewed-read", state["financial_journal"]["obligations"])

    def test_incomplete_content_retains_known_wire_charge_but_cannot_repeat(self):
        state, spec, record = self.fixture()
        api = Mock(return_value=({"state": "QUERY_STATE_COMPLETED", "result": {"rows": []}}, 100))
        self.stack(now=lambda: self.fixed, usage_guard=Mock(), export_risk=Mock(return_value=(0.1, ["fixture evidence"])),
                   api=api, publish=Mock())
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.refresh.export_source(record, spec, 10, state=state)
        obligation = state["financial_journal"]["obligations"]["reviewed-read"]
        self.assertFalse(obligation["unresolved"])
        self.assertAlmostEqual(obligation["cost_credits"], 0.001)
        self.assertTrue(obligation["charge_observed"])
        self.assertEqual(obligation["cache_status"], "requested")
        saved = json.loads(self.state_path.read_text())["financial_journal"]["obligations"]["reviewed-read"]
        self.assertEqual(saved, obligation, "known charge must be durable before content refusal")
        with self.assertRaises(self.refresh.ManualExportReview):
            self.refresh.export_source(record, spec, 10, state=state)
        self.assertEqual(api.call_count, 1)

    def test_invalid_json_body_still_has_wire_metering(self):
        response = Mock()
        response.read.return_value = b"not-json"
        transport = Mock()
        transport.__enter__ = Mock(return_value=response)
        transport.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {"DUNE_API_KEY": "fixture-key"}), \
             patch.object(self.refresh,"dune_open", return_value=transport):
            self.assertEqual(self.refresh.api("execution/known-result/results", include_wire_size=True), (None, 8))

    def test_malformed_body_charge_survives_quarantine_and_restart(self):
        for body in (None, [], {"result": []}, {"state": "QUERY_STATE_COMPLETED", "result": {"rows": [{}]}}):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "data/current").mkdir(parents=True)
                state, spec, record = self.fixture()
                api = Mock(return_value=(body, 100))
                self.stack(ROOT=root, STATE=root / "journal.json", now=lambda: self.fixed, usage_guard=Mock(),
                           export_risk=Mock(return_value=(0.1, ["fixture evidence"])), api=api, publish=Mock())
                with self.assertRaises(self.refresh.CacheError) as error:
                    self.refresh.export_source(record, spec, 10, state=state)
                self.refresh.quarantine_source(state, spec["key"], str(error.exception))
                self.refresh.checkpoint(state)
                restored = json.loads((root / "journal.json").read_text())
                obligation = restored["financial_journal"]["obligations"]["reviewed-read"]
                self.assertAlmostEqual(obligation["cost_credits"], 0.001)
                self.assertFalse(obligation["unresolved"])
                self.assertNotIn("included_in_confirmed_usage", obligation)
                self.assertTrue(restored["source_errors"][spec["key"]]["manual_reconciliation_required"])
                with self.assertRaises(self.refresh.ManualExportReview):
                    self.refresh.export_source(record, spec, 10, state=restored)
                self.assertEqual(api.call_count, 1)

    def test_interrupted_transport_without_metering_stays_unknown(self):
        state, spec, record = self.fixture()
        self.stack(now=lambda: self.fixed, usage_guard=Mock(), export_risk=Mock(return_value=(0.1, ["fixture evidence"])),
                   api=Mock(side_effect=TimeoutError("response interrupted")), publish=Mock())
        with self.assertRaises(TimeoutError):
            self.refresh.export_source(record, spec, 10, state=state)
        obligation = state["financial_journal"]["obligations"]["reviewed-read"]
        self.assertTrue(obligation["unresolved"])
        self.assertIsNone(obligation["cost_credits"])
        self.assertNotIn("completed_transport_observed_at_utc", obligation)

    def test_no_approval_means_no_export_even_with_sample_quote_and_budget(self):
        state, spec, record = self.fixture()
        spec.pop("operation_approval")
        api = Mock()
        self.stack(now=lambda: self.fixed, usage_guard=Mock(), export_risk=Mock(return_value=(0.1, ["fixture evidence"])), api=api)
        with self.assertRaises(self.refresh.BudgetBlocked):
            self.refresh.export_source(record, spec, 10, state=state)
        api.assert_not_called()
        self.assertNotIn("financial_journal", state)

    def test_cache_reuse_acceptance_does_not_invent_another_legacy_read(self):
        state, spec, record = self.fixture()
        state["execution_history"] = [{"execution_id": "known-result", "query_id": 1,
                                       "query_parameters": {}, "execution_cost_credits": 0.2}]
        self.stack(publish=Mock(), api=Mock(side_effect=AssertionError("no Dune on cache hit")))
        binding = {"query_id": 1, "parameters": {}, "columns": ["a"],
            "sql_sha256": self.refresh.hashlib.sha256(self.refresh.encoded({
                "execution_id": "known-result", "query_id": 1, "parameters": {}})).hexdigest()}
        cache = self.refresh.result_cache()
        cache.prepare_export(state, record, binding, risk_credits=0.1, evidence=["legacy reviewed estimate"],
                             timestamp="2026-10-07T09:00:00Z")
        cache.cache_result(state, record, binding, {"execution_id": "known-result", "total_row_count": 1,
                                                   "result_rows": [{"a": 1}]}, timestamp="2026-10-07T09:01:00Z")
        before = {key for key, item in state["financial_journal"]["obligations"].items() if item["kind"] == "export"}
        self.assertEqual(len(before), 1)
        self.refresh.export_source(record, spec, 10, state=state)
        self.refresh.stage_consumption(state, spec, record, "non_destructive_hot_union")
        self.refresh.checkpoint(state)
        after = {key for key, item in state["financial_journal"]["obligations"].items() if item["kind"] == "export"}
        self.assertEqual(after, before)
        self.assertTrue(next(item for item in state["financial_journal"]["obligations"].values()
                             if item["kind"] == "export")["unresolved"], "original unknown charge is still held")
        self.refresh.api.assert_not_called()


class ManifestDecisionTests(AcceptanceTestCase):
    def test_manifest_mismatch_does_not_reduce_and_stages_marker_with_decision(self):
        policy = {"daily_cadence_minutes": 60, "weekly_cadence_minutes": 10080,
                  "weekly_lookback_days": 30, "daily_lookback_days": 1,
                  "manifest_query_id": 9, "manifest_max_run_credits": 1.0,
                  "manifest_max_export_points": 10, "manifest_max_export_bytes": 1000}
        config = {**self.config, "reconciliation": policy}
        state = {
            "queries": {},
            "pending_executions": {"reconciliation_manifest": {
                "key": "reconciliation_manifest", "query_id": 9,
                "execution_id": "manifest-exec", "query_parameters": {"lookback_days": 1},
                "submitted_at_utc": "2026-10-07T09:59:00Z", "timeout_seconds": 240,
                "billing": {}}},
            "reconciliation": {"last_weekly_manifest_utc": self.fixed.isoformat()},
        }
        output = self.root / "state" / "v3_transfer_manifest_latest.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"result": {"rows": [{
            "day": "2026-10-01", "event_count": 5, "transfer_count": 5, "burn_count": 0,
            "mint_count": 0, "tx_count": 5, "min_block_slot": 1, "max_block_slot": 9,
            "amount_raw_total": "1", "block_slot_total": "2"}]}}))
        local = {"2026-10-01": {"day": "2026-10-01", "event_count": 999,
                                "transfer_count": 999, "burn_count": 0, "mint_count": 0,
                                "tx_count": 999, "min_block_slot": 1, "max_block_slot": 9,
                                "amount_raw_total": "1", "block_slot_total": "2"}}
        record = {"execution_id": "manifest-exec", "query_id": 9,
                  "execution_cost_credits": 0.2, "query_parameters": {"lookback_days": 1},
                  "state": "QUERY_STATE_COMPLETED"}
        captured = []
        reduce_source = Mock()
        publish = Mock()

        def checkpoint(value):
            captured.append(copy.deepcopy(value))

        self.stack(CONFIG=config, now=lambda: self.fixed, execute=Mock(return_value=record),
                   export_source=Mock(), canonical_transfer_manifest=Mock(return_value=local),
                   reduce_atomically=reduce_source, checkpoint=checkpoint, publish=publish)
        self.assertFalse(self.refresh.reconcile_historical_manifest(state, {}))

        reduce_source.assert_not_called()  # mismatch is never reduced destructively
        publish.assert_not_called()
        self.assertEqual(len(captured), 1, "decision and marker were not one commit")
        committed = captured[0]
        self.assertEqual(committed["reconciliation"]["manifest_decision"],
                         "aggregate_mismatch_manual_review")
        self.assertTrue(committed["reconciliation"]["repair_manual_review_required"])
        self.assertEqual(committed["consumed_executions"]["manifest-exec"]["effect"],
                         "manifest_decision")
        self.assertNotIn("reconciliation_manifest", committed.get("pending_executions", {}))
        self.assertIn("historical_reconciliation", committed["source_errors"])


if __name__ == "__main__":
    unittest.main()
