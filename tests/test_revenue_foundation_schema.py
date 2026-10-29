"""Frozen official CSVs: section identity matters more than label similarity."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import importlib.util
import io
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync_revenue as source

FIXTURES = ROOT / "tests/fixtures"
LEGACY = (FIXTURES / "revenue_sheet_legacy_20261026.csv").read_text()
CURRENT = (FIXTURES / "revenue_sheet_foundation_20261029.csv").read_text()
EPOCH = (FIXTURES / "epoch_summary_20261029.csv").read_text()


def edit(text, function):
    rows = list(csv.reader(io.StringIO(text)))
    function(rows)
    result = io.StringIO(); csv.writer(result).writerows(rows)
    return result.getvalue()


class FoundationSchemaTests(unittest.TestCase):
    def test_new_labels_preserve_two_different_october_balances(self):
        rows, mapping = source.parse_revenue(CURRENT)
        row = next(r for r in rows if r["month"] == "2026-10-01")
        self.assertEqual(row["wifi_payment_received_usd"], 32643.19)
        self.assertEqual(row["wifi_payment_date"], "2026-10-26")
        self.assertEqual(row["foundation_booked_balance_usd"], 38715.05)
        self.assertEqual(row["inc_payable_to_foundation_usd"], 64829.61)
        self.assertEqual(row["foundation_due_from_inc_usd"], 64829.61)
        self.assertEqual(row["transferred_to_foundation_usd"], 23985)
        self.assertEqual(row["transferred_to_fiat_operators_usd"], 1108.18)
        self.assertEqual(mapping["balance_outstanding_to_transfer_usd"]["section"], "foundation")
    def test_legacy_schema_preserves_all_existing_numeric_meanings(self):
        parsed, _ = source.parse_revenue(LEGACY)
        accepted = json.loads((FIXTURES / "revenue_legacy_expected.json").read_text())["data"]
        # This fixture is the accepted pre-change raw CSV; no network needed.
        by_month = {r["month"]:r for r in parsed}
        for row in accepted:
            for key in source.METRICS:
                self.assertEqual(by_month[row["month"]][key], row[key], (row["month"], key))
    def test_pre_october_history_is_not_rewritten_by_new_names(self):
        old = {r["month"]:r for r in source.parse_revenue(LEGACY)[0]}
        for row in source.parse_revenue(CURRENT)[0]:
            if row["month"] >= "2026-10-01":continue
            for key in source.METRICS:self.assertEqual(row[key], old[row["month"]][key])
    def test_future_receivables_are_not_moved_into_october(self):
        rows = {r["month"]:r for r in source.parse_revenue(CURRENT)[0]}
        self.assertEqual(rows["2026-11-01"]["foundation_monthly_receivable_usd"], 26114.55)
        self.assertEqual(rows["2026-10-01"]["foundation_monthly_receivable_usd"], 26951.13)
        self.assertIsNone(rows["2026-11-01"]["wifi_payment_received_usd"])
        self.assertNotIn("BFWD", rows)
    def test_blank_values_are_not_zero(self):
        rows = source.parse_revenue(CURRENT)[0]
        self.assertIsNone(rows[-1]["wifi_revenue_projected_usd"])
    def test_duplicate_foundation_metric_is_rejected_even_if_equal(self):
        def duplicate(rows):
            index = next(i for i,r in enumerate(rows) if len(r)>1 and r[1].strip()=="Monthly Receivables from XNET")
            rows.insert(index, list(rows[index]))
        with self.assertRaisesRegex(RuntimeError,"exactly one foundation"):
            source.parse_revenue(edit(CURRENT, duplicate))
    def test_conflicting_mirrored_postings_fail_without_double_counting(self):
        def corrupt(rows):
            col = rows[0].index("Oct 2026")
            row = next(r for r in rows if len(r)>1 and r[1].strip()=="Payment Sent to Foundation")
            row[col] = "$23,986"
        with self.assertRaisesRegex(RuntimeError,"Conflicting Foundation transfer mirrors"):
            source.parse_revenue(edit(CURRENT, corrupt))
    def test_conflicting_due_from_and_inc_payable_fail(self):
        def corrupt(rows):
            col = rows[0].index("Oct 2026")
            row = next(r for r in rows if len(r)>1 and r[1].strip()=="Due Balance from XNET Inc")
            row[col] = "$64,000"
        with self.assertRaisesRegex(RuntimeError,"Conflicting Inc payable"):
            source.parse_revenue(edit(CURRENT, corrupt))
    def test_missing_foundation_row_cannot_fall_back_to_inc_same_label(self):
        def remove(rows):
            indices = [i for i,r in enumerate(rows) if len(r)>1 and r[1].strip()=="Balance Due to Foundation"]
            rows.pop(indices[-1])
        with self.assertRaisesRegex(RuntimeError,"exactly one foundation"):
            source.parse_revenue(edit(CURRENT, remove))
    def test_unknown_name_is_not_fuzzily_mapped_to_a_financial_metric(self):
        text = CURRENT.replace("Monthly Receivables from XNET", "Maybe Foundation Buying Power")
        with self.assertRaises(RuntimeError):source.parse_revenue(text)
    def test_whitespace_normalization_does_not_change_section_identity(self):
        text = CURRENT.replace("Monthly Receivables from XNET", " Monthly   Receivables from XNET ")
        self.assertEqual(source.parse_revenue(text)[0], source.parse_revenue(CURRENT)[0])
    def test_duplicate_calendar_columns_fail(self):
        with self.assertRaisesRegex(RuntimeError,"Duplicate calendar month"):
            source.parse_revenue(CURRENT.replace("Nov 2026", "Oct 2026"))
    def test_nonfinite_money_is_never_published(self):
        with self.assertRaises(RuntimeError):source.parse_revenue(CURRENT.replace("$32,643.19", "NaN"))
    def test_epoch_failure_leaves_all_previous_outputs_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory)/name for name in ("raw.csv", "monthly.json", "epoch.json")]
            for p in paths:p.write_text("last good")
            with patch.object(source,"RAW_PATH",paths[0]),patch.object(source,"JSON_PATH",paths[1]), \
                 patch.object(source,"EPOCH_JSON_PATH",paths[2]),patch.object(source,"fetch_csv",side_effect=[
                     (CURRENT.encode(),CURRENT),(b"bad epoch", "bad epoch")]):
                with self.assertRaises(RuntimeError):source.main()
            self.assertEqual([p.read_text() for p in paths],["last good"]*3)
    def test_success_publishes_source_hash_clock_and_exact_row_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory)/name for name in ("raw.csv", "monthly.json", "epoch.json")]
            with patch.object(source,"RAW_PATH",paths[0]),patch.object(source,"JSON_PATH",paths[1]), \
                 patch.object(source,"EPOCH_JSON_PATH",paths[2]),patch.object(source,"fetch_csv",side_effect=[
                     (CURRENT.encode(),CURRENT),(EPOCH.encode(),EPOCH)]),redirect_stdout(io.StringIO()):
                source.main()
            result=json.loads(paths[1].read_text())
            self.assertEqual(result["source"]["schema_contract"],"section_aware_foundation_v2")
            self.assertTrue(result["source"]["fetched_at_utc"].endswith("Z"))
            self.assertEqual(paths[0].read_bytes(),CURRENT.encode())
            self.assertIn("do not prove market buys or token burns",result["notes"]["foundation_ledger_compatibility"])
    def test_offline_source_to_settlement_to_live_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            shutil.copytree(ROOT/"data",root/"data")
            shutil.copytree(ROOT/"scripts",root/"scripts",ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT/"config",root/"config")
            with patch.object(source,"RAW_PATH",root/"data/xnet_revenue_sheet_raw.csv"), \
                 patch.object(source,"JSON_PATH",root/"data/xnet_revenue_monthly.json"), \
                 patch.object(source,"EPOCH_JSON_PATH",root/"data/xnet_epoch_schedule.json"), \
                 patch.object(source,"fetch_csv",side_effect=[(CURRENT.encode(),CURRENT),(EPOCH.encode(),EPOCH)]), \
                 redirect_stdout(io.StringIO()):source.main()
            for name in ("build_defillama_revenue.py","v3_build_revenue_state.py",
                         "v3_build_presentation_history.py","v3_build_live_bundle.py"):
                clocked = """import datetime,runpy,sys
class Frozen(datetime.datetime):
    @classmethod
    def now(cls,tz=None):
        value=cls(2026,10,29,0,30,tzinfo=datetime.timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)
datetime.datetime=Frozen
runpy.run_path(sys.argv[1],run_name='__main__')
"""
                result=subprocess.run([sys.executable,"-c",clocked,str(root/"scripts"/name)],cwd=root,text=True,capture_output=True)
                self.assertEqual(result.returncode,0,name+"\n"+result.stderr+result.stdout[-2000:])
            revenue=json.loads((root/"data/current/xnet_revenue_state.json").read_text())
            defi=json.loads((root/"data/xnet_defillama_revenue.json").read_text())
            bundle=json.loads((root/"data/current/xnet_live_bundle.json").read_text())
            self.assertEqual(revenue["latest_payment"]["payment_date"],"2026-10-26")
            self.assertEqual(revenue["latest_payment"]["amount_usd"],"32643.19")
            self.assertEqual(revenue["foundation_accounting"]["booked_balance_usd"],"38715.05")
            self.assertEqual(revenue["foundation_accounting"]["inc_payable_usd"],"64829.61")
            self.assertEqual(revenue["latest_fiat_operator_transfer"]["service_month"],"2026-07")
            receipt=next(r for r in defi["settlements"] if r["payment_received_date"]=="2026-10-26")
            self.assertEqual(receipt["service_months"][0]["service_month"],"2026-08")
            self.assertEqual(revenue["cumulative_source_sheet"]["wifi_payments_received_usd"],"204070.59")
            self.assertEqual(bundle["revenue"],revenue)
    def test_only_changed_source_contents_queue_charts_without_touching_finances(self):
        workflow=yaml.safe_load((ROOT/".github/workflows/sync-dune-revenue.yml").read_text())
        step=next(s for s in workflow["jobs"]["sync"]["steps"] if s.get("name", "").startswith("Queue affected"))
        script="\n".join(step["run"].splitlines()[1:-1])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);current=root/"data/current";current.mkdir(parents=True)
            journal=current/"v3_refresh_state.json";source_path=current/"xnet_revenue_state.json"
            old={"queries":{"current":{"execution_id":"accepted"}},"paused":False,
                 "financial_journal":{"obligations":{"unknown":{"cost":None}}},
                 "standing_credit_budget":{"preserve":"risk book"},"forced_presentations":["holders"]}
            journal.write_text(json.dumps(old));source_path.write_text(json.dumps({"source":{"sheet_sha256":"abc","sheet_fetched_at_utc":"2026-10-29T00:00:00Z"}}))
            import os
            previous=Path.cwd()
            try:
                os.chdir(root);exec(compile(script,"queue_changed_revenue","exec"),{})
                result=json.loads(journal.read_text())
                self.assertEqual(result["forced_presentations"],["commercial","current","holders"])
                for key in ("queries","paused","financial_journal","standing_credit_budget"):
                    self.assertEqual(result[key],old[key])
                result["forced_presentations"]=[];journal.write_text(json.dumps(result))
                exec(compile(script,"queue_unchanged_revenue","exec"),{})
                self.assertEqual(json.loads(journal.read_text())["forced_presentations"],[])
            finally:os.chdir(previous)


if __name__ == "__main__":unittest.main()
