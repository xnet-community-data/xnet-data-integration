import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("usage", Path(__file__).resolve().parents[1] / "scripts/v3_credit_usage.py")
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)

class UsageTests(unittest.TestCase):
    def test_baseline_then_rolling_average(self):
        t = datetime(2026, 10, 4, tzinfo=timezone.utc)
        state = usage.summarize({}, [{"start_date": "2026-10-04", "credits_used": 400}], t)
        self.assertIsNone(state["average_credits_per_day"])
        self.assertFalse(state["alert"])
        for day, credits in [(1, 500), (2, 610), (3, 730), (4, 840)]:
            state = usage.summarize(state, [{"start_date": "2026-10-04", "credits_used": credits}], t + timedelta(days=day))
        self.assertAlmostEqual(state["average_credits_per_day"], (840 - 500) / 3)
        self.assertTrue(state["alert"])

    def test_billing_reset_preserves_previous_period_and_usage(self):
        t = datetime(2026, 10, 3, tzinfo=timezone.utc)
        state = usage.summarize({}, [{"start_date": "2026-09-04", "credits_used": 1200}], t)
        state = usage.summarize(state, [{"start_date": "2026-09-04", "credits_used": 1250}, {"start_date": "2026-10-04", "credits_used": 60}], t + timedelta(days=1))
        self.assertAlmostEqual(state["average_credits_per_day"], 110)
        self.assertTrue(state["alert"])

    def test_exact_threshold_does_not_alert(self):
        t = datetime(2026, 10, 4, tzinfo=timezone.utc)
        state = usage.summarize({}, [{"start_date": "2026-10-04", "credits_used": 0}], t)
        state = usage.summarize(state, [{"start_date": "2026-10-04", "credits_used": 100}], t + timedelta(days=1))
        self.assertFalse(state["alert"])

if __name__ == "__main__":
    unittest.main()
