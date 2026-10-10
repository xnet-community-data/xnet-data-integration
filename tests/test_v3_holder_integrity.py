"""Fail-closed holder quarantine: never fabricate balances, never mask unknown failures."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("reducer", ROOT / "scripts/v3_reduce_chain.py")
reducer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reducer)

class HolderIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        Path("data/current").mkdir(parents=True)
        self.verified = {
            "positive_holder_count": 5323,
            "latest_transfer_event_utc": "2026-10-08 17:57:58.000 UTC",
        }
        Path("data/current/xnet_holder_state.json").write_text(json.dumps(self.verified))

    def test_material_negative_quarantines_holders_without_editing_verified_values(self):
        before = Path("data/current/xnet_holder_state.json").read_text()
        error = "Negative holder balance after reduction: wallet = -3709.53558595"
        with patch.object(reducer, "derive_holders", side_effect=RuntimeError(error)):
            reducer.derive_holders_guarded([], allow_stale_holders=True)
        self.assertEqual(Path("data/current/xnet_holder_state.json").read_text(), before)
        quality = json.loads(Path("data/current/xnet_holder_integrity.json").read_text())
        self.assertEqual(quality["status"], "DEGRADED")
        self.assertEqual(quality["holder_data_as_of_utc"], self.verified["latest_transfer_event_utc"])
        self.assertEqual(quality["error"], error)
        self.assertTrue(quality["automatic_recheck"])

    def test_direct_reducer_still_fails_closed_without_opt_in(self):
        with patch.object(reducer, "derive_holders", side_effect=RuntimeError(
            "Negative holder balance after reduction: wallet = -1"
        )):
            with self.assertRaisesRegex(RuntimeError, "Negative holder"):
                reducer.derive_holders_guarded([], allow_stale_holders=False)

    def test_unknown_holder_errors_are_not_silenced(self):
        with patch.object(reducer, "derive_holders", side_effect=RuntimeError("Unrecognized schema")):
            with self.assertRaisesRegex(RuntimeError, "Unrecognized schema"):
                reducer.derive_holders_guarded([], allow_stale_holders=True)

    def test_without_verified_last_good_never_quarantines(self):
        Path("data/current/xnet_holder_state.json").unlink()
        with patch.object(reducer, "derive_holders", side_effect=RuntimeError(
            "Negative holder balance after reduction: wallet = -1"
        )):
            with self.assertRaisesRegex(RuntimeError, "Negative holder"):
                reducer.derive_holders_guarded([], allow_stale_holders=True)

    def test_recovery_restores_healthy_status(self):
        with patch.object(reducer, "derive_holders", return_value=None):
            reducer.derive_holders_guarded([], allow_stale_holders=True)
        quality = json.loads(Path("data/current/xnet_holder_integrity.json").read_text())
        self.assertEqual(quality["status"], "HEALTHY")
        self.assertEqual(quality["holder_data_as_of_utc"], self.verified["latest_transfer_event_utc"])

if __name__ == "__main__":
    unittest.main()
