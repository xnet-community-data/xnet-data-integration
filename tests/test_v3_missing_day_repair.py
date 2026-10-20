import csv
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_missing_day_repair import accept_missing_day, digest, MissingDayRejected
from v3_reduce_chain import TRANSFER_FIELDS, normalise_transfer

FIXTURES = Path(__file__).parent / "fixtures"


class MissingDayTests(unittest.TestCase):
    def setUp(self):
        self.result = (FIXTURES / "dune_repair_20261014.json").read_bytes()
        old = normalise_transfer(json.loads(self.result)["data"]["rows"][0])
        old.update(block_date="2026-10-13", block_time="2026-10-13T00:00:00Z", event_id="prior")
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=TRANSFER_FIELDS)
        writer.writeheader()
        writer.writerow(old)
        self.canonical = output.getvalue().encode()
        self.trusted = {"canonical_sha256": digest(self.canonical), "result_sha256": digest(self.result),
            "query_id": 8894038, "query_version": 4, "saved_sql_sha256": digest((FIXTURES / "approved_repair_20261014.sql").read_bytes().rstrip(b"\n")),
            "submitted_at_utc": "2026-10-19T21:20:05.724602Z", "completed_received_at_utc": "2026-10-19T21:21:00Z",
            "query_parameters": {"oldest_days_ago": 5, "newest_days_ago": 5},
            "absolute_day": "2026-10-14", "execution_id": "01M5B0JH2659VTCAGS9EBB1T4P"}

    def test_approved_complete_day_appended_without_rewriting_any_old_bytes(self):
        candidate, receipt = accept_missing_day(self.canonical, self.result, self.trusted)
        self.assertTrue(candidate.startswith(self.canonical))
        self.assertEqual(receipt["added_rows"], 198)
        self.assertEqual(receipt["deleted_rows"], 0)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(candidate.decode())))), 199)
        with self.assertRaises(MissingDayRejected):
            accept_missing_day(candidate, self.result, {**self.trusted, "canonical_sha256": digest(candidate)})

    def test_identity_scope_completeness_and_predecessor_are_load_bearing(self):
        for field, bad in (("canonical_sha256", "bad"), ("result_sha256", "bad"),
            ("query_version", 5), ("saved_sql_sha256", "bad"), ("execution_id", "other"),
            ("absolute_day", "2026-10-15"), ("completed_received_at_utc", "2026-10-20T00:00:00Z"),
            ("submitted_at_utc", "2026-10-19T21:20:05")):
            with self.subTest(field=field), self.assertRaises(MissingDayRejected):
                accept_missing_day(self.canonical, self.result, {**self.trusted, field: bad})
        for mutation in ("empty", "short", "scope", "owner", "integer", "locator"):
            data = json.loads(self.result)
            if mutation == "empty": data["data"]["rows"] = []
            elif mutation == "short": data["data"]["rows"].pop()
            elif mutation == "scope": data["data"]["rows"][0]["block_date"] = "2026-10-15"
            elif mutation == "owner": data["data"]["rows"][0]["from_owner"] = None
            elif mutation == "integer": data["data"]["rows"][0]["amount_raw"] = 123.5
            elif mutation == "locator":
                data["data"]["rows"][1] = {**data["data"]["rows"][0], "amount_raw": "999"}
            raw = json.dumps(data).encode()
            with self.subTest(mutation=mutation), self.assertRaises(MissingDayRejected):
                accept_missing_day(self.canonical, raw, {**self.trusted, "result_sha256": digest(raw)})


if __name__ == "__main__":
    unittest.main()
