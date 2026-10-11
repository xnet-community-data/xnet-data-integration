import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bundle", ROOT / "scripts/v3_build_live_bundle.py"
)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


class LiveBundleTests(unittest.TestCase):
    def test_bundle_nests_exact_verified_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "data/current"
            current.mkdir(parents=True)
            values = {
                "chain": {"chain": 1, "generated_at_utc": "2026-10-11T12:00:00Z"},
                "network": {"network": 2, "status": "live"},
                "revenue": {"revenue": 3, "generated_at_utc": "2026-10-11T12:00:00Z"},
                "market": {"market": 4, "fetched_at_utc": "2026-10-11T12:00:00Z"},
            }
            names = {
                "chain": "xnet_chain_snapshot.json",
                "network": "xnet_network_state.json",
                "revenue": "xnet_revenue_state.json",
                "market": "xnet_market_state.json",
            }
            for key, name in names.items():
                (current / name).write_text(json.dumps(values[key]) + "\n")
            built = bundle.build_bundle(root)
            self.assertEqual(built["schema_version"], 1)
            for key, value in values.items():
                self.assertEqual(built[key], value)

    def test_bundle_fails_closed_when_component_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "data/current"
            current.mkdir(parents=True)
            for key, relative in bundle.SOURCES.items():
                if key == "revenue":
                    continue
                (root / relative).write_text("{}\n")
            with self.assertRaisesRegex(RuntimeError, "Missing verified live state"):
                bundle.build_bundle(root)

    def test_current_snapshot_uses_one_livefetch_bundle(self):
        sql = (ROOT / "dune/v3/sql/60_live_current_snapshot.sql").read_text()
        self.assertEqual(sql.count("http_get("), 1)
        self.assertIn("xnet_live_bundle.json", sql)
        for old in (
            "xnet_chain_snapshot.json",
            "xnet_network_state.json",
            "xnet_revenue_state.json",
            "xnet_market_state.json",
        ):
            self.assertNotIn(old, sql)


if __name__ == "__main__":
    unittest.main()
