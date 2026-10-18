"""Network published-cache mismatch recovery without routine upstream polling.

Also pins the journal authority boundary for this independent source: it only
annotates ``external_sources.network``, so a missing or invalid execution journal
must be refused before any fetch and before any write - a standalone invocation
may never initialize or blank the accepted ledger.
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("network_sync", ROOT / "scripts/v3_sync_network.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

EXEC = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
PENDING_EXEC = "01BZ3NDEKTSV4RRFFQ69G5FAV"


def valid_journal(paused=False, network=None, **extra):
    """A schema-1 journal with the identity lanes this source must never disturb."""
    state = {
        "schema_version": 1,
        "paused": paused,
        "queries": {"xnet_transfers": {"query_id": 8894037, "execution_id": EXEC,
                                       "completed_at_utc": "2026-10-09T00:00:00Z"}},
        "consumed_executions": {EXEC: {"key": "xnet_transfers", "effect": "source_terminal_success"}},
        "execution_history": [{"execution_id": EXEC, "query_id": 8894037,
                               "query_parameters": {"lookback_hours": 2}}],
        "pending_executions": {"bbb_dex": {"key": "bbb_dex", "query_id": 8894132,
                                           "query_parameters": {"lookback_hours": 2},
                                           "submitted_at_utc": "2026-10-09T00:00:00Z",
                                           "execution_id": PENDING_EXEC}},
        "external_sources": {"network": network} if network is not None else {},
    }
    state.update(extra)
    return state


class NetworkPublishedStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
        self.previous = {
            "status": "live",
            "last_success_utc": "2026-10-10T11:57:53Z",
            "last_attempt_utc": "2026-10-10T11:57:53Z",
            "offload_data_as_of": "2026-10-09",
            "device_data_as_of": "2026-10-10",
        }
        self.published = {
            "collected_at_utc": "2026-10-08T09:53:09Z",
            "offload": {"data_as_of": "2026-10-07"},
            "devices": {"data_as_of": "2026-10-07"},
        }

    def test_known_uncommitted_success_requires_recovery(self):
        self.assertTrue(module.uncommitted_network_collection(self.previous, self.published))

    def test_matching_committed_data_requires_no_recovery(self):
        committed = {
            "collected_at_utc": "2026-10-10T11:57:53Z",
            "offload": {"data_as_of": "2026-10-09"},
            "devices": {"data_as_of": "2026-10-10"},
        }
        self.assertFalse(module.uncommitted_network_collection(self.previous, committed))

    def test_upstream_failure_uses_regular_backoff_not_publication_retries(self):
        failed = {**self.previous, "status": "preserved_last_good"}
        self.assertFalse(module.uncommitted_network_collection(failed, self.published))

    def test_paused_chain_has_no_network_pause_gate(self):
        """A Dune/chain pause is NOT a network gate.

        v3_sync_network.py has no `paused` branch: independent, non-Dune
        telemetry keeps its own cadence and still collects once when due, while
        the chain pause is retained untouched. This is the intentional semantics,
        not a suspension of network collection.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            network = root / "network.json"
            previous = {**self.previous, "last_attempt_utc": "2026-10-08T11:57:53Z"}
            state.write_text(json.dumps(valid_journal(paused=True, network=previous)))
            # Published cache already matches the earlier success, so the only
            # reason for any upstream call is normal cadence — never the pause.
            network.write_text(json.dumps({
                "collected_at_utc": self.previous["last_success_utc"],
                "offload": {"data_as_of": self.previous["offload_data_as_of"]},
                "devices": {"data_as_of": self.previous["device_data_as_of"]},
            }))
            with patch.object(module, "REFRESH_STATE", state), \
                 patch.object(module, "NETWORK_STATE", network), \
                 patch.object(module, "DEVICE_HISTORY", root / "device_history.json"), \
                 patch.object(module, "utc_now", return_value=self.now), \
                 patch.object(module, "fetch_json",
                              side_effect=RuntimeError("mock upstream failed")) as fetch:
                self.assertEqual(module.main(), 0)
            # Called once as ordinary due-cadence collection (no pause gate).
            self.assertEqual(fetch.call_count, 1)
            preserved = json.loads(state.read_text())
            self.assertTrue(preserved["paused"], "chain pause must be preserved")
            self.assertEqual(preserved["external_sources"]["network"]["status"],
                             "preserved_last_good")

    def test_success_without_publication_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            network = root / "network.json"
            state.write_text(json.dumps(valid_journal(network=self.previous)))
            network.write_text(json.dumps(self.published))
            with patch.object(module, "REFRESH_STATE", state), patch.object(module, "NETWORK_STATE", network), patch.object(module, "utc_now", return_value=self.now), patch.object(module, "fetch_json", side_effect=RuntimeError("mock upstream failed")) as fetch:
                self.assertEqual(module.main(), 0)
                self.assertEqual(fetch.call_count, 1)
                retried = json.loads(state.read_text())
                self.assertIn("last_unpublished_retry_utc", retried["external_sources"]["network"])
                # The prior gap retry stamp remains; no additional upstream call.
                retried["external_sources"]["network"]["status"] = "live"
                state.write_text(json.dumps(retried))
                self.assertEqual(module.main(), 0)
                self.assertEqual(fetch.call_count, 1)

    def test_missing_journal_refuses_before_any_source_and_writes_nothing(self):
        """No journal on disk: refuse, never initialize, never fetch, never write."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            network = root / "network.json"
            network.write_bytes(b'{"sentinel": 1}\n')
            output = io.StringIO()
            with patch.object(module, "REFRESH_STATE", state), \
                 patch.object(module, "NETWORK_STATE", network), \
                 patch.object(module, "DEVICE_HISTORY", root / "device_history.json"), \
                 patch.object(module, "utc_now", return_value=self.now), \
                 patch.object(module, "fetch_json") as fetch, \
                 contextlib.redirect_stdout(output):
                self.assertEqual(module.main(), 1)
            fetch.assert_not_called()  # refused before any source contact
            self.assertFalse(state.exists(), "a missing journal must never be initialized")
            self.assertEqual(network.read_bytes(), b'{"sentinel": 1}\n')  # nothing published
            self.assertIn("authoritative execution journal", output.getvalue())

    def test_invalid_journal_refuses_before_any_source_and_preserves_its_bytes(self):
        """Wrong schema and undecodable bytes both refuse with the file untouched."""
        for label, raw in (("schema", json.dumps({"schema_version": 2, "queries": {}})),
                           ("queries", json.dumps({"schema_version": 1, "queries": []})),
                           ("undecodable", '{"schema_version": 1, "queries": {}} trailing')):
            with self.subTest(journal=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "state.json"
                network = root / "network.json"
                state.write_bytes(raw.encode())
                output = io.StringIO()
                with patch.object(module, "REFRESH_STATE", state), \
                     patch.object(module, "NETWORK_STATE", network), \
                     patch.object(module, "DEVICE_HISTORY", root / "device_history.json"), \
                     patch.object(module, "utc_now", return_value=self.now), \
                     patch.object(module, "fetch_json") as fetch, \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(module.main(), 1)
                fetch.assert_not_called()
                self.assertEqual(state.read_bytes(), raw.encode())  # never rewritten or blanked
                self.assertFalse(network.exists())
                self.assertIn("authoritative execution journal", output.getvalue())

    def test_mark_attempt_refuses_to_create_or_rewrite_an_invalid_journal(self):
        """The write path validates the journal from disk immediately before writing."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            with patch.object(module, "REFRESH_STATE", state):
                with self.assertRaises(module.StateError):
                    module.mark_attempt("live")
                self.assertFalse(state.exists(), "mark_attempt must not initialize a journal")
                corrupt = b'{"schema_version": 1, "queries": {}}\nnot-json'
                state.write_bytes(corrupt)
                with self.assertRaises(module.StateError):
                    module.mark_attempt("live")
                self.assertEqual(state.read_bytes(), corrupt)  # refused, never rewritten
                state.write_text(json.dumps({"schema_version": 1, "queries": {}}))
                module.mark_attempt("live")  # a valid journal is annotated in place
            annotated = json.loads(state.read_bytes())
            self.assertEqual(annotated["external_sources"]["network"]["status"], "live")
            self.assertEqual(annotated["queries"], {})
            module.validate_journal(state.read_bytes())  # the write left a valid v1 journal

    def test_valid_paused_journal_collects_independently_and_preserves_every_identity(self):
        """A due, valid journal still collects while paused - and only the network lane changes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            network = root / "network.json"
            original = valid_journal(
                paused=True,
                network={**self.previous, "last_attempt_utc": "2026-10-08T11:57:53Z"},
                presentation_errors={"current": {"error": "kept", "failed_at_utc": "2026-10-09T00:00:00Z"}},
                rejected_submissions=[{"key": "rejected", "query_id": 8894037,
                                       "query_parameters": {}, "rejected_at_utc": "2026-10-09T00:00:00Z"}],
            )
            state.write_text(json.dumps(original))
            network.write_text(json.dumps({"collected_at_utc": self.previous["last_success_utc"],
                                           "offload": {"data_as_of": self.previous["offload_data_as_of"]},
                                           "devices": {"data_as_of": self.previous["device_data_as_of"]}}))
            with patch.object(module, "REFRESH_STATE", state), \
                 patch.object(module, "NETWORK_STATE", network), \
                 patch.object(module, "DEVICE_HISTORY", root / "device_history.json"), \
                 patch.object(module, "utc_now", return_value=self.now), \
                 patch.object(module, "fetch_json", side_effect=RuntimeError("mock upstream failed")) as fetch:
                self.assertEqual(module.main(), 0)
            self.assertEqual(fetch.call_count, 1)  # independence: pause is not a gate for this source
            committed = json.loads(state.read_text())
            self.assertEqual(set(committed), set(original))  # no lane added or dropped
            self.assertTrue(committed["paused"], "chain pause must survive the annotation")
            for lane in ("queries", "consumed_executions", "execution_history",
                         "pending_executions", "presentation_errors", "rejected_submissions"):
                self.assertEqual(committed[lane], original[lane], f"{lane} must be untouched")
            record = committed["external_sources"]["network"]
            self.assertEqual(record["status"], "preserved_last_good")
            self.assertEqual(record["last_error"], "mock upstream failed")
            self.assertEqual(record["last_attempt_utc"], "2026-10-10T12:00:00Z")  # annotated only
            self.assertEqual(record["offload_data_as_of"], self.previous["offload_data_as_of"])
            self.assertEqual(record["last_success_utc"], self.previous["last_success_utc"])

    def test_invalid_journal_preserves_values_through_the_failure_path(self):
        """A journal that breaks between validation and the attempt record is refused, not written."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state.json"
            network = root / "network.json"
            state.write_text(json.dumps(valid_journal(
                network={**self.previous, "last_attempt_utc": "2026-10-08T11:57:53Z"})))
            network.write_text(json.dumps({"collected_at_utc": self.previous["last_success_utc"],
                                           "offload": {"data_as_of": self.previous["offload_data_as_of"]},
                                           "devices": {"data_as_of": self.previous["device_data_as_of"]}}))
            original = state.read_bytes()
            with patch.object(module, "REFRESH_STATE", state), \
                 patch.object(module, "NETWORK_STATE", network), \
                 patch.object(module, "DEVICE_HISTORY", root / "device_history.json"), \
                 patch.object(module, "utc_now", return_value=self.now), \
                 patch.object(module, "fetch_json", side_effect=RuntimeError("mock upstream failed")), \
                 patch.object(module, "mark_attempt", side_effect=module.StateError("journal changed")), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(module.main(), 1)
            self.assertEqual(state.read_bytes(), original)  # refused, never rewritten
            self.assertIn("authoritative execution journal", output.getvalue())


if __name__ == "__main__":
    unittest.main()
