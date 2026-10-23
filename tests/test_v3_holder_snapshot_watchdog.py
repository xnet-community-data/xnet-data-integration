from copy import deepcopy
import contextlib,csv,io,json,os,sys,tempfile,unittest
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"));sys.path.insert(0,str(ROOT/"tests"))
import v3_autonomy_watchdog as watchdog
from v3_holder_snapshot import PATH,AUTHORIZATION_REF,make_baseline
from test_v3_holder_snapshot import fixture


class SnapshotWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);now=datetime.now(timezone.utc)
        self.baseline=make_baseline(fixture(now));self.receipt=self.baseline["receipt"]
        at=now.isoformat().replace("+00:00","Z")
        self.write(PATH,self.baseline)
        self.write("config/v3_refresh.json",{"sources":[],"presentation":[]})
        self.write("data/current/v3_refresh_state.json",{"schema_version":1,"queries":{},"paused":False,
            "holder_reconciliation_authorization":{"approval_ref":AUTHORIZATION_REF},
            "holder_reconciliation":{"active_baseline_id":self.baseline["baseline_id"]}})
        self.write("data/current/xnet_holder_integrity.json",{"status":"HEALTHY",
            "holder_data_as_of_utc":self.receipt["block_time_utc"],"baseline_slot":100,
            "baseline_id":self.baseline["baseline_id"],"mint_conservation_residual_raw":"0"})
        holder={"positive_holder_count":2,"holders_ge_1_xnet":2,"holders_ge_100_xnet":0,
            "holders_ge_1000_xnet":0,"outstanding_balance_sum_xnet":"3.5","baseline_id":self.baseline["baseline_id"]}
        self.write("data/current/xnet_holder_state.json",holder)
        chain={"holder_count_positive":2,"holders_ge_1_xnet":2,"holders_ge_100_xnet":0,
               "holders_ge_1000_xnet":0,"holder_snapshot_id":self.baseline["baseline_id"]}
        self.write("data/current/xnet_chain_snapshot.json",chain)
        self.write("data/current/xnet_live_bundle.json",{"chain":chain})
        for key,field in (("market","fetched_at_utc"),("network","collected_at_utc"),("revenue","generated_at_utc")):
            self.write(f"data/current/xnet_{key}_state.json",{field:at})
        self.csv_path=self.root/"data/derived/xnet_owner_balances.csv";self.csv_path.parent.mkdir(parents=True)
        with self.csv_path.open("w",newline="") as handle:
            writer=csv.DictWriter(handle,fieldnames=["owner","xnet_balance"]);writer.writeheader()
            writer.writerows({"owner":owner,"xnet_balance":str(Decimal(raw)/10**8)}
                            for owner,raw in self.baseline["owners_raw"].items())
    def write(self,path,value):
        target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(value))
    def check(self):
        with patch("sys.argv",["watchdog","--root",str(self.root)]),patch.dict(os.environ,{"GITHUB_STEP_SUMMARY":""}), \
             contextlib.redirect_stdout(io.StringIO()):
            result=watchdog.main()
        report=json.loads((self.root/"state/v3_autonomy_audit.json").read_text())
        return result,report
    def test_actual_vectors_counts_mint_and_bundle_must_all_match(self):
        result,report=self.check();self.assertEqual(result,0);self.assertEqual(report["status"],"healthy")
    def test_good_health_label_cannot_hide_missing_or_modified_owner_balances(self):
        rows=self.csv_path.read_text().splitlines()
        self.csv_path.write_text("\n".join(rows[:-1])+"\n")
        result,report=self.check();self.assertEqual(result,1)
        self.assertIn("Published owner balances",report["problems"][0])
    def test_stale_bundle_cannot_hide_behind_a_fresh_holder_state(self):
        self.write("data/current/xnet_live_bundle.json",{"chain":{"holder_count_positive":1}})
        result,report=self.check();self.assertEqual(result,1);self.assertIn("counts/bundle",report["problems"][0])
    def test_fresh_generated_time_cannot_replace_exact_bank_time(self):
        path=self.root/"data/current/xnet_holder_integrity.json";quality=json.loads(path.read_text())
        quality["holder_data_as_of_utc"]=datetime.now(timezone.utc).isoformat();path.write_text(json.dumps(quality))
        result,report=self.check();self.assertEqual(result,1);self.assertIn("does not match",report["problems"][0])
    def test_missing_proof_cannot_leave_a_green_holder_service(self):
        (self.root/PATH).unlink()
        result,report=self.check();self.assertEqual(result,1);self.assertEqual(report["status"],"unhealthy")
    def test_fake_thresholds_cannot_pass_a_correct_positive_total(self):
        path=self.root/"data/current/xnet_holder_state.json";holder=json.loads(path.read_text())
        holder["holders_ge_1000_xnet"]=1;path.write_text(json.dumps(holder))
        result,report=self.check();self.assertEqual(result,1);self.assertIn("thresholds",report["problems"][0])


if __name__=="__main__":unittest.main()
