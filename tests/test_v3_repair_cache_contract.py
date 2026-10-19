"""Every machine-only public repair export must be cacheable after a paid read."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


class RepairCacheContractTests(unittest.TestCase):
    def test_configured_transfer_repair_is_on_the_public_result_allowlist(self):
        spec = importlib.util.spec_from_file_location("refresh_repair_cache_contract", ROOT / "scripts/v3_refresh_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cache = module.result_cache(lambda _state: None)
        reconciliation = module.CONFIG["reconciliation"]
        self.assertIn(reconciliation["repair_query_id"], cache.allowed_query_ids)
        self.assertIn(reconciliation["manifest_query_id"], cache.allowed_query_ids)
        self.assertNotIn(1, cache.allowed_query_ids)


if __name__ == "__main__":
    unittest.main()
