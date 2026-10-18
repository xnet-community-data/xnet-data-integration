"""Offline contracts for lossless writer queuing and portable source timestamps."""
from pathlib import Path
import sys
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_operational_health import build_health


class WorkflowContracts(unittest.TestCase):
    def test_all_shared_writers_queue_instead_of_replacing_pending_work(self):
        for name, group in (
            ("refresh-v3-history", "xnet-live-state"),
            ("monitor-v3-credits", "xnet-live-state"),
            ("sync-dune-revenue", "xnet-live-state"),
            ("sync-revenue", "xnet-public-data-feed"),
            ("sync-offload", "xnet-public-data-feed"),
        ):
            with self.subTest(workflow=name):
                config = yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())
                self.assertEqual(config["concurrency"], {
                    "group": group, "queue": "max", "cancel-in-progress": False})

    def test_naive_source_timestamp_never_depends_on_runner_timezone(self):
        result = build_health({"sources": {"source": {"source_asof": "2026-10-18T03:00:00"}}},
            "2026-10-18T03:10:00Z", {"domains": {"source": {"max_age_seconds": 3600}}})
        self.assertEqual(result["domains"]["source"]["status"], "invalid")
        self.assertIsNone(result["domains"]["source"]["source_asof_utc"])


if __name__ == "__main__":
    unittest.main()
