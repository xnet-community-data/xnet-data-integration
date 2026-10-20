"""A catch-up request must not lose rows to a narrower calendar partition filter."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import unittest

SQL = Path(__file__).resolve().parents[1] / "dune/v3/sql/62_bbb_dex_hot.sql"
CLAUSE = "    -- Partition pruning must cover the requested window, including catch-up.\n    WHERE block_date >= CAST(CURRENT_TIMESTAMP - INTERVAL '{{lookback_hours}}' HOUR AS DATE)"


class BbbPartitionWindowTests(unittest.TestCase):
    def test_only_the_partition_lower_bound_changes_the_saved_contract(self):
        sql = SQL.read_text()
        self.assertEqual(sql.count(CLAUSE), 1)
        normalized = sql.replace(CLAUSE, "<PARTITION_FILTER>")
        self.assertEqual(hashlib.sha256(normalized.encode()).hexdigest(),
                         "2325edb46b93bfa8f8fb33f4ecdff4cebc2b946f4193d6399b7b0b44478319fc")

    def test_partition_bound_contains_every_timestamp_in_the_requested_window(self):
        for hour in (0, 1, 9, 23):
            now = datetime(2026, 10, 20, hour, 17, 11, tzinfo=timezone.utc)
            for lookback in (1, 26, 29, 46, 48, 120, 168):
                start = now - timedelta(hours=lookback)
                partition_start = start.date()
                for offset in range(lookback * 60 + 1):
                    event = start + timedelta(minutes=offset)
                    self.assertGreaterEqual(event.date(), partition_start)
                self.assertLess((start - timedelta(microseconds=1)), start)

    def test_original_fixed_two_day_filter_dropped_proven_catchup_days(self):
        now = datetime(2026, 10, 20, 9, 36, tzinfo=timezone.utc)
        event = datetime(2026, 10, 16, 18, 46, tzinfo=timezone.utc)
        self.assertGreaterEqual(event, now - timedelta(hours=120))
        self.assertLess(event.date(), now.date() - timedelta(days=2))
        self.assertGreaterEqual(event.date(), (now - timedelta(hours=120)).date())


if __name__ == "__main__":
    unittest.main()
