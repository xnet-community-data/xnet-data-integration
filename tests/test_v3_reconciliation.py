import importlib.util
import json
import tempfile
import unittest
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "refresh", ROOT / "scripts/v3_refresh_dashboard.py"
)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class HistoricalReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            **refresh.CONFIG,
            "reconciliation_generation": 1,
            "approved_backfill_lookback_hours": 240,
            "repair_lookback_hours": 168,
            "integrity_repair_lookback_hours": 720,
            "integrity_repair_cooldown_minutes": 1440,
            "integrity_repair_max_attempts": 3,
            "max_repair_export_points": 40000,
            "max_repair_export_bytes": 2000000,
            "max_integrity_repair_export_points": 150000,
            "max_integrity_repair_export_bytes": 5000000,
        }
        self.transfer = {
            "key": "xnet_transfers",
            "max_run_credits": 1.5,
            "repair_max_run_credits": 5,
            "integrity_repair_max_run_credits": 15,
            "repair_lookback_hours": 168,
        }

    def test_new_generation_forces_approved_backfill(self):
        with patch.object(refresh, "CONFIG", self.config):
            mode, hours = refresh.source_reconciliation_plan(
                {"reconciliation": {"generation": 0}}, self.transfer, False
            )
        self.assertEqual((mode, hours), ("approved_backfill", 240))

    def test_holder_anomaly_forces_deep_repair_after_generation(self):
        state = {
            "reconciliation": {"generation": 1, "integrity_attempts": 0},
            "source_errors": {"holder_integrity": {"error": "negative"}},
        }
        fixed = datetime(2026, 10, 11, 10, tzinfo=timezone.utc)
        with patch.object(refresh, "CONFIG", self.config), patch.object(
            refresh, "now", return_value=fixed
        ):
            mode, hours = refresh.source_reconciliation_plan(
                state, self.transfer, False
            )
        self.assertEqual((mode, hours), ("integrity", 720))

    def test_integrity_repair_is_rate_limited(self):
        state = {
            "reconciliation": {
                "generation": 1,
                "integrity_attempts": 1,
                "last_integrity_attempt_utc": "2026-10-11T09:30:00Z",
            },
            "source_errors": {"holder_integrity": {"error": "negative"}},
        }
        fixed = datetime(2026, 10, 11, 10, tzinfo=timezone.utc)
        with patch.object(refresh, "CONFIG", self.config), patch.object(
            refresh, "now", return_value=fixed
        ):
            self.assertFalse(refresh.integrity_repair_due(state))
            mode, hours = refresh.source_reconciliation_plan(
                state, self.transfer, False
            )
        self.assertEqual((mode, hours), (None, None))

    def test_integrity_repair_stops_after_attempt_cap(self):
        state = {
            "reconciliation": {"generation": 1, "integrity_attempts": 3},
            "source_errors": {"holder_integrity": {"error": "negative"}},
        }
        with patch.object(refresh, "CONFIG", self.config):
            self.assertFalse(refresh.integrity_repair_due(state))

    def test_routine_repair_does_not_download_full_transfer_window(self):
        state = {"reconciliation": {"generation": 1}}
        with patch.object(refresh, "CONFIG", self.config):
            mode, hours = refresh.source_reconciliation_plan(
                state, self.transfer, True
            )
        self.assertEqual((mode, hours), (None, None))


    def test_manifest_comparison_detects_late_indexed_day(self):
        remote = {
            "2026-10-06": {
                "day": "2026-10-06",
                "event_count": 228,
                "transfer_count": 225,
                "burn_count": 3,
                "mint_count": 0,
                "tx_count": 123,
                "min_block_slot": 453733987,
                "max_block_slot": 454038894,
                "amount_raw_total": "4187699332505953",
                "block_slot_total": "103480000000",
            }
        }
        local = {
            "2026-10-06": {
                **remote["2026-10-06"],
                "event_count": 206,
                "transfer_count": 203,
            }
        }
        self.assertEqual(
            refresh.manifest_mismatches(remote, local),
            ["2026-10-06"],
        )

    def test_manifest_comparison_passes_exact_match(self):
        row = {
            "day": "2026-10-06",
            "event_count": 228,
            "transfer_count": 225,
            "burn_count": 3,
            "mint_count": 0,
            "tx_count": 123,
            "min_block_slot": 453733987,
            "max_block_slot": 454038894,
            "amount_raw_total": "4187699332505953",
            "block_slot_total": "103480000000",
        }
        self.assertEqual(
            refresh.manifest_mismatches(
                {"2026-10-06": row},
                {"2026-10-06": dict(row)},
            ),
            [],
        )

    def test_weekly_manifest_defers_when_projected_usage_is_high(self):
        config = {
            **self.config,
            "monthly_target_credits": 2500,
            "reconciliation": {
                "minimum_target_headroom_credits": 150,
                "weekly_soft_projected_monthly_credits": 2200,
            },
        }
        with patch.object(refresh, "CONFIG", config):
            self.assertFalse(
                refresh.reconciliation_weekly_allowed({
                    "credits_used": 1800,
                    "target_credits": 2500,
                    "projected_period_credits": 2300,
                })
            )

    def test_weekly_manifest_allowed_with_credit_headroom(self):
        config = {
            **self.config,
            "monthly_target_credits": 2500,
            "reconciliation": {
                "minimum_target_headroom_credits": 150,
                "weekly_soft_projected_monthly_credits": 2200,
            },
        }
        with patch.object(refresh, "CONFIG", config):
            self.assertTrue(
                refresh.reconciliation_weekly_allowed({
                    "credits_used": 500,
                    "target_credits": 2500,
                    "projected_period_credits": 1900,
                })
            )

    def test_repair_modes_raise_caps_without_weakening_hot_path(self):
        with patch.object(refresh, "CONFIG", self.config):
            hot = refresh.execution_spec_for_mode(self.transfer, None)
            daily = refresh.execution_spec_for_mode(self.transfer, "daily")
            deep = refresh.execution_spec_for_mode(self.transfer, "integrity")
            self.assertEqual(hot["max_run_credits"], 1.5)
            self.assertEqual(daily["max_run_credits"], 5.0)
            self.assertEqual(deep["max_run_credits"], 15.0)
            self.assertEqual(
                refresh.export_limits_for_mode("daily"), (40000, 2000000)
            )
            self.assertEqual(
                refresh.export_limits_for_mode("integrity"), (150000, 5000000)
            )


if __name__ == "__main__":
    unittest.main()
