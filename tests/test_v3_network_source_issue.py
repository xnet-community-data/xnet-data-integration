"""The free network lane must surface a collector-declared failure even though
``v3_sync_network.py`` exits 0, without inventing clocks or clearing on a skip.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "network_source_issue_refresh_test", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

NOW = datetime(2026, 11, 20, 1, 11, tzinfo=timezone.utc)
NOW_STAMP = "2026-11-20T01:11:00Z"
STALE_ERROR = "Offload API is stale: latest completed day is 2026-11-11"
PRIOR_SUCCESS = "2026-11-13T16:48:57Z"


def state_with(network, *, issues=None):
    return {
        "schema_version": 1,
        "paused": False,
        "queries": {},
        "pending_executions": {},
        "external_sources": {"network": deepcopy(network)},
        "source_errors": deepcopy(issues or {}),
        "financial_journal": {
            "obligations": {"old": {"liability_id": "old", "kind": "execution"}},
            "allowances": {},
        },
    }


def preserved(attempt, error=STALE_ERROR):
    return {
        "status": "preserved_last_good",
        "last_attempt_utc": attempt,
        "last_success_utc": PRIOR_SUCCESS,
        "last_error": error,
        "offload_data_as_of": "2026-11-11",
        "device_data_as_of": "2026-11-12",
    }


def live(attempt):
    return {
        "status": "live",
        "last_attempt_utc": attempt,
        "last_success_utc": attempt,
        "last_error": None,
        "offload_data_as_of": "2026-11-20",
        "device_data_as_of": "2026-11-19",
    }


class NetworkSourceIssueTests(unittest.TestCase):
    def run_sync(self, state, record, *, published=None, fail=False):
        journal = {"schema_version": 1, "external_sources": {"network": record}}

        def child(*args, **kwargs):
            if fail:
                raise refresh.subprocess.CalledProcessError(1, "v3_sync_network.py")
            return record

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "state.json"
            journal_path.write_bytes(b"{}")
            (root / "data/current").mkdir(parents=True)
            (root / "data/current/xnet_network_state.json").write_text(
                json.dumps(published if published is not None else {}))
            with patch.object(refresh, "now", return_value=NOW), \
                 patch.object(refresh, "stamp", return_value=NOW_STAMP), \
                 patch.object(refresh, "ROOT", root), \
                 patch.object(refresh, "STATE", journal_path), \
                 patch.object(refresh, "save"), \
                 patch.object(refresh, "run", side_effect=child), \
                 patch.object(refresh, "validate_journal", return_value=journal):
                refresh.sync_independent_network(state)
        return state

    def test_preserved_last_good_on_zero_exit_surfaces_child_error_and_attempt(self):
        state = state_with(preserved("2026-11-19T13:31:31Z"))
        child = preserved("2026-11-20T01:05:00Z")
        self.run_sync(state, child)

        issue = state["source_errors"]["network"]
        self.assertEqual(issue["error"], STALE_ERROR)
        # The collector's own poll time, never the evaluator's clock.
        self.assertEqual(issue["failed_at_utc"], "2026-11-20T01:05:00Z")
        self.assertNotEqual(issue["failed_at_utc"], NOW_STAMP)
        self.assertIsNone(issue["next_retry_at_utc"])
        self.assertTrue(issue["automatic_recheck"])
        self.assertFalse(issue["unresolved_execution"])
        self.assertEqual(issue["consecutive_failures"], 1)
        # Unchanged contract: the collector-owned record is imported verbatim.
        self.assertEqual(state["external_sources"]["network"], child)

    def test_not_due_after_failure_keeps_issue_and_times(self):
        state = state_with(preserved("2026-11-19T13:31:31Z"))
        child = preserved("2026-11-20T01:05:00Z")
        self.run_sync(state, child)
        first = deepcopy(state["source_errors"]["network"])
        # A cadence skip returns the identical record: no fetch, no clock moved.
        self.run_sync(state, child)
        self.assertEqual(state["source_errors"]["network"], first)
        self.assertEqual(state["source_errors"]["network"]["consecutive_failures"], 1)

    def test_skip_cannot_erase_a_recorded_failure(self):
        # The reported bug: a not-due skip returns 0 and used to pop the issue.
        state = state_with(preserved("2026-11-19T13:31:31Z"),
                            issues={"network": {"error": "older wording",
                                                "failed_at_utc": "2026-11-19T13:31:31Z",
                                                "next_retry_at_utc": "2026-11-20T13:31:31Z",
                                               "consecutive_failures": 2}})
        self.run_sync(state, preserved("2026-11-19T13:31:31Z"))
        issue = state["source_errors"]["network"]
        self.assertEqual(issue["error"], STALE_ERROR)
        self.assertEqual(issue["failed_at_utc"], "2026-11-19T13:31:31Z")
        # Recorded backoff is kept, not reset or extended by a skip.
        self.assertEqual(issue["consecutive_failures"], 2)
        self.assertEqual(issue["next_retry_at_utc"], "2026-11-20T13:31:31Z")

    def test_new_failed_attempt_increments_without_touching_other_issues(self):
        state = state_with(preserved("2026-11-19T13:31:31Z"),
                           issues={"market": {"error": "dex down",
                                              "failed_at_utc": "2026-11-19T00:00:00Z"}})
        self.run_sync(state, preserved("2026-11-20T01:05:00Z"))
        self.run_sync(state, preserved("2026-11-21T01:05:00Z"))
        issue = state["source_errors"]["network"]
        self.assertEqual(issue["failed_at_utc"], "2026-11-21T01:05:00Z")
        self.assertEqual(issue["consecutive_failures"], 2)
        self.assertEqual(state["source_errors"]["market"],
                         {"error": "dex down", "failed_at_utc": "2026-11-19T00:00:00Z"})

    def test_validated_published_publication_clears_only_the_network_issue(self):
        state = state_with(live("2026-11-13T16:48:57Z"),
                           issues={"network": {"error": STALE_ERROR,
                                               "failed_at_utc": "2026-11-19T13:31:31Z"},
                                   "market": {"error": "dex down",
                                              "failed_at_utc": "2026-11-19T00:00:00Z"}})
        self.run_sync(state, live("2026-11-20T01:05:00Z"), published={
            "collected_at_utc": "2026-11-20T01:05:00Z",
            "offload": {"data_as_of": "2026-11-20"},
            "devices": {"data_as_of": "2026-11-19"}})
        self.assertNotIn("network", state["source_errors"])
        self.assertIn("market", state["source_errors"])

    def test_not_due_skip_never_clears_a_live_record(self):
        state = state_with(live("2026-11-20T01:05:00Z"),
                           issues={"network": {"error": STALE_ERROR,
                                               "failed_at_utc": "2026-11-19T13:31:31Z"}})
        self.run_sync(state, live("2026-11-20T01:05:00Z"), published={
            "collected_at_utc": "2026-11-20T01:05:00Z",
            "offload": {"data_as_of": "2026-11-20"},
            "devices": {"data_as_of": "2026-11-19"}})
        self.assertEqual(state["source_errors"]["network"]["failed_at_utc"],
                         "2026-11-19T13:31:31Z")

    def test_success_ahead_of_publication_never_clears_the_issue(self):
        state = state_with(live("2026-11-13T16:48:57Z"),
                           issues={"network": {"error": STALE_ERROR,
                                               "failed_at_utc": "2026-11-19T13:31:31Z"}})
        self.run_sync(state, live("2026-11-20T01:05:00Z"), published={
            "collected_at_utc": "2026-11-13T16:48:56Z",
            "offload": {"data_as_of": "2026-11-11"},
            "devices": {"data_as_of": "2026-11-12"}})
        self.assertEqual(state["source_errors"]["network"]["failed_at_utc"],
                         "2026-11-19T13:31:31Z")

    def test_unrelated_process_failure_keeps_existing_path_and_finances(self):
        state = state_with(preserved("2026-11-19T13:31:31Z"),
                           issues={"market": {"error": "dex down",
                                              "failed_at_utc": "2026-11-19T00:00:00Z"}})
        before_journal = deepcopy(state["financial_journal"])
        self.run_sync(state, None, fail=True)
        issue = state["source_errors"]["network"]
        self.assertIn("v3_sync_network.py", issue["error"])
        self.assertEqual(issue["failed_at_utc"], NOW_STAMP)  # existing path semantics
        self.assertEqual(state["financial_journal"], before_journal)
        self.assertEqual(state["source_errors"]["market"],
                         {"error": "dex down", "failed_at_utc": "2026-11-19T00:00:00Z"})

    def test_invalid_child_record_fails_closed_through_existing_path(self):
        for bad in ({"status": "mystery", "last_attempt_utc": "2026-11-20T01:05:00Z"},
                    {"status": "preserved_last_good", "last_attempt_utc": "not-a-time"},
                    {"status": "preserved_last_good"},
                    {"status": None, "last_attempt_utc": "2026-11-20T01:05:00Z"}):
            with self.subTest(bad=bad):
                state = state_with({},
                                   issues={"network": {"error": STALE_ERROR,
                                                       "failed_at_utc": "2026-11-19T13:31:31Z"}})
                self.run_sync(state, bad)
                issue = state["source_errors"]["network"]
                self.assertIn("network collector", issue["error"])
                self.assertNotIn("last_success_utc", issue)

    def test_corrupt_previous_issue_does_not_break_surfacing(self):
        state = state_with(preserved("2026-11-19T13:31:31Z"),
                           issues={"network": {"error": "x",
                                               "failed_at_utc": "2026-11-19T13:31:31Z",
                                               "consecutive_failures": "junk"}})
        self.run_sync(state, preserved("2026-11-19T13:31:31Z"))
        issue = state["source_errors"]["network"]
        self.assertEqual(issue["error"], STALE_ERROR)
        self.assertEqual(issue["failed_at_utc"], "2026-11-19T13:31:31Z")
        self.assertEqual(issue["consecutive_failures"], 1)

    def test_live_record_without_success_evidence_cannot_clear_or_replace_last_good_record(self):
        for bad in ({"status": "live", "last_attempt_utc": NOW_STAMP},
                    {**live(NOW_STAMP), "last_success_utc": PRIOR_SUCCESS}):
            with self.subTest(bad=bad):
                previous = preserved("2026-11-19T13:31:31Z")
                state = state_with(previous, issues={"network": {"error": STALE_ERROR}})
                self.run_sync(state, bad)
                self.assertIn("matching success evidence", state["source_errors"]["network"]["error"])
                self.assertEqual(state["external_sources"]["network"], previous)

    def test_missing_child_record_fails_closed(self):
        state = state_with({})
        journal = {"schema_version": 1, "external_sources": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal_path = root / "state.json"
            journal_path.write_bytes(b"{}")
            (root / "data/current").mkdir(parents=True)
            with patch.object(refresh, "now", return_value=NOW), \
                 patch.object(refresh, "stamp", return_value=NOW_STAMP), \
                 patch.object(refresh, "ROOT", root), \
                 patch.object(refresh, "STATE", journal_path), \
                 patch.object(refresh, "save"), \
                 patch.object(refresh, "run"), \
                 patch.object(refresh, "validate_journal", return_value=journal):
                refresh.sync_independent_network(state)
        self.assertIn("network collector", state["source_errors"]["network"]["error"])


if __name__ == "__main__":
    unittest.main()
