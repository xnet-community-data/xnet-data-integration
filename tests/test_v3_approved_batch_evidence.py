"""Offline integrity checks for the finite, user-approved 20 October recovery."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_reduce_chain import BBB_WALLET, normalise_transfer, parse_ts

FIXTURES = ROOT / "tests/fixtures/approved_batch_20261020"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class ApprovedBatchEvidenceTests(unittest.TestCase):
    def test_sealed_source_and_presentation_receipts(self):
        for name in ("source_acceptance_receipt.json", "presentation_verification_receipt.json"):
            receipt = load(name)
            digest = receipt.pop("receipt_id")
            self.assertEqual(hashlib.sha256(json.dumps(receipt, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest(), digest)
        source = load("source_acceptance_receipt.json")
        presentation = load("presentation_verification_receipt.json")
        self.assertEqual(presentation["source_acceptance_receipt_sha256"], source["receipt_id"])
        self.assertEqual(source["added_rows"], 770)
        self.assertEqual(source["old_rows"], 3470)
        self.assertEqual(source["deleted_rows"], 0)
        self.assertTrue(source["original_record_bytes_preserved"])
        self.assertFalse(source["full_current_utc_day_claimed"])

    def test_all_five_complete_result_artifacts_are_bound_to_scope(self):
        receipt = load("source_acceptance_receipt.json")
        expected = {16: (187, 117, 70), 17: (217, 0, 217), 18: (280, 0, 280),
                    19: (190, 0, 190), 20: (13, 0, 13)}
        for proof in receipt["provider_sources"]:
            day = int(proof["day"][-2:])
            path = FIXTURES / (proof["operation"] + ".json")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), proof["source_sha256"])
            result = json.loads(path.read_text())
            rows = result["data"]["rows"]
            self.assertEqual(result["executionId"], proof["execution_id"])
            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(len(rows), result["resultMetadata"]["totalRowCount"])
            self.assertEqual((len(rows), proof["matched_old_rows"], proof["added_rows"]), expected[day])
            self.assertEqual(proof["query_parameters"], {"oldest_days_ago": 20-day, "newest_days_ago": 20-day})
            self.assertEqual(proof["complete_utc_day"], day < 20)
            self.assertEqual({r["block_date"] for r in rows}, {proof["day"]})
            normalized = [normalise_transfer(r) for r in rows]
            self.assertEqual(len({r["event_id"] for r in normalized}), len(rows))
            for row in normalized:
                self.assertEqual(Decimal(row["amount_xnet"]), Decimal(row["amount_raw"]) / Decimal(10**8))
                self.assertLessEqual(parse_ts(row["block_time"]), parse_ts(proof["received_at_utc"]))

    def test_bbb_economic_records_and_finalized_usdc_capture(self):
        receipt = load("source_acceptance_receipt.json")
        path = FIXTURES / "bbb_120h.json"
        result = load(path.name)
        rows = result["data"]["rows"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), receipt["bbb"]["source_sha256"])
        self.assertEqual(len(rows), 31)
        self.assertEqual(len({r["tx_id"] for r in rows}), 31)
        self.assertEqual({r["trader_id"] for r in rows}, {BBB_WALLET})
        self.assertEqual(load("bbb_sql_v12_proof.json")["version"], 12)
        capture = load("bbb_usdc_rpc.json")
        self.assertEqual(capture["request"]["params"][2]["commitment"], "finalized")
        total = sum((Decimal(r["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) / Decimal(10**6)
                     for r in capture["response"]["result"]["value"]), Decimal(0))
        self.assertEqual(total, Decimal("12768.416038"))
        observation = receipt["external_observations"][0]
        self.assertEqual(observation["capture_sha256"], hashlib.sha256((FIXTURES / "bbb_usdc_rpc.json").read_bytes()).hexdigest())
        self.assertEqual(observation["slot"], capture["response"]["result"]["context"]["slot"])

    def test_verified_presentations_and_reported_compute_not_export_settlement(self):
        receipt = load("presentation_verification_receipt.json")
        total = Decimal(0)
        for name, proof in receipt["results"].items():
            path = FIXTURES / ("presentation_" + name + ".json")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), proof["capture_sha256"])
            result = json.loads(path.read_text())
            self.assertEqual(result["executionId"], proof["execution_id"])
            self.assertEqual(result["state"], "COMPLETED")
            self.assertEqual(len(result["data"]["rows"]), proof["rows"])
            self.assertEqual(proof["rows"], result["resultMetadata"]["totalRowCount"])
            total += Decimal(result["resultMetadata"]["executionCostCredits"])
        self.assertEqual(total, Decimal("1.051"))
        for name in [f"transfer_{d}.json" for d in range(16,21)] + ["bbb_120h.json"]:
            total += Decimal(load(name)["resultMetadata"]["executionCostCredits"])
        self.assertEqual(total, Decimal("3.961"))
        for row in load("presentation_current.json")["data"]["rows"]:
            self.assertEqual(row["holder_count_positive"], 5306)
            self.assertAlmostEqual(row["circulating_supply_xnet"], 450854891.88866935994)
            self.assertEqual(row["latest_transfer_event_utc"], "2026-10-20 05:43:06.000 UTC")
            self.assertEqual(row["bbb_wallet_usdc_observed_at_utc"], "2026-10-20T09:43:14.975129Z")
        holders = load("presentation_holders.json")["data"]["rows"]
        self.assertEqual(sum(r["holder_count"] for r in holders), 5306)


if __name__ == "__main__":
    unittest.main()
