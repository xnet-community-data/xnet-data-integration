"""Offline checks for source-faithful epoch publishing."""
import csv
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sync_revenue import parse_epoch_schedule


def epoch_csv(totals=None, starts=None):
    epochs = [98, 99, 100, 101]
    totals = totals or {98: "2500000", 99: "2500000", 100: "2500000", 101: ""}
    starts = starts or {e: "09/01/2026" for e in epochs}
    rows = [
        ["", "Total to date"] + [f"Epoch {e}" for e in epochs],
        ["Epoch Start Date", ""] + [starts[e] for e in epochs],
        ["Epoch End Date", ""] + ["09/14/2026" for _ in epochs],
        ["", "100000000"] + [totals[e] for e in epochs],
        ["Fiat Operator Burn", ""] + ["0" for _ in epochs],
    ]
    stream = io.StringIO()
    csv.writer(stream).writerows(rows)
    return stream.getvalue()


class EpochPublicationTests(unittest.TestCase):
    def test_incomplete_trailing_epoch_does_not_block_verified_schedule(self):
        result = parse_epoch_schedule(epoch_csv())
        self.assertEqual([r["epoch"] for r in result], [98, 99, 100])
        self.assertEqual(result[-1]["total_reward_tokens_xnet"], 2500000)

    def test_trailing_epoch_is_automatically_published_when_complete(self):
        result = parse_epoch_schedule(epoch_csv(totals={
            98: "2500000", 99: "2500000", 100: "2500000", 101: "3000000"
        }))
        self.assertEqual(result[-1]["epoch"], 101)
        self.assertEqual(result[-1]["total_reward_tokens_xnet"], 3000000)

    def test_missing_trailing_date_is_not_invented(self):
        starts = {98: "09/01/2026", 99: "09/01/2026",
                  100: "09/01/2026", 101: ""}
        result = parse_epoch_schedule(epoch_csv(starts=starts))
        self.assertEqual(result[-1]["epoch"], 100)

    def test_incomplete_middle_epoch_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "follows incomplete"):
            parse_epoch_schedule(epoch_csv(totals={
                98: "2500000", 99: "", 100: "2500000", 101: "3000000"
            }))

    def test_invalid_trailing_numeric_value_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "Could not parse numeric value"):
            parse_epoch_schedule(epoch_csv(totals={
                98: "2500000", 99: "2500000", 100: "2500000", 101: "unknown"
            }))

    def test_source_revision_to_published_history_is_reflected(self):
        result = parse_epoch_schedule(epoch_csv(totals={
            98: "2500000", 99: "2500000", 100: "2600000", 101: ""
        }))
        self.assertEqual(result[-1]["epoch"], 100)
        self.assertEqual(result[-1]["total_reward_tokens_xnet"], 2600000)

    def test_duplicate_epoch_heading_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "Duplicate epoch"):
            parse_epoch_schedule(epoch_csv().replace("Epoch 99", "Epoch 98"))


if __name__ == "__main__":
    unittest.main()
