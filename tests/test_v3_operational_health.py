import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_operational_health import build_health


NOW = datetime(2026, 10, 17, 15, tzinfo=timezone.utc)
CONFIG = {"domains": {"chain": {"source_key": "chain", "max_age_seconds": 3600,
                                  "acceptance_timeout_seconds": 300}}}


class OperationalHealthTests(unittest.TestCase):
    def test_green_process_does_not_make_paused_stale_source_fresh(self):
        state = {"paused_at_utc": "2026-10-17T14:00:00Z", "automatic_retry": False,
                 "sources": {"chain": {"status": "healthy",
                                        "completed_at_utc": "2026-10-17T14:59:00Z",
                                        "generated_at_utc": "2026-10-17T14:59:00Z",
                                        "source_asof": "2026-10-17T10:00:00Z"}}}
        report = build_health(state, NOW, CONFIG)["domains"]["chain"]
        self.assertEqual(report["status"], "paused")
        self.assertEqual(report["last_process_check_utc"], "2026-10-17T14:59:00Z")
        self.assertEqual(report["source_asof_utc"], "2026-10-17T10:00:00Z")

    def test_stale_source_degraded_even_when_process_recent(self):
        report = build_health({"sources": {"chain": {"completed_at_utc": "2026-10-17T14:59:00Z",
                       "source_asof": "2026-10-17T10:00:00Z"}}}, NOW, CONFIG)["domains"]["chain"]
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["recovery_action"], "refresh_source_and_check_integrity")

    def test_pending_known_vs_acceptance_unknown(self):
        source = {"chain": {"source_asof": "2026-10-17T14:59:00Z"}}
        known = build_health({"sources": source, "pending_executions": {
            "chain": {"execution_id": "01KNOWN"}}}, NOW, CONFIG)["domains"]["chain"]
        unknown = build_health({"sources": source, "pending_executions": {
            "chain": {"submitted_at_utc": "2026-10-17T14:59:00Z"}}}, NOW, CONFIG)["domains"]["chain"]
        self.assertEqual(known["status"], "waiting_known")
        self.assertEqual(unknown["status"], "unknown_acceptance")

    def test_incident_id_stable_across_timestamp_changes_and_recovery_action(self):
        def evaluate(asof):
            return build_health({"sources": {"chain": {"source_asof": asof}}}, NOW, CONFIG)["domains"]["chain"]
        first = evaluate("2026-10-17T10:00:00Z")
        second = evaluate("2026-10-17T10:01:00Z")
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(first["recovery_action"], "refresh_source_and_check_integrity")
        self.assertIsNone(evaluate("2026-10-17T14:59:00Z")["incident_id"])

    def test_missing_source_is_not_fresh(self):
        result = build_health({}, NOW, CONFIG)["domains"]["chain"]
        self.assertEqual(result["status"], "source_unavailable")


if __name__ == "__main__":
    unittest.main()
