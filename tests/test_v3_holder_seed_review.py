from copy import deepcopy
import csv,json,sys,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"));sys.path.insert(0,str(ROOT/"tests"))
from test_v3_rpc_transfer_proof import capture
from v3_rpc_transfer_proof import derive_transaction
from v3_reduce_chain import TRANSFER_FIELDS
from v3_holder_seed_review import probe,held
from v3_autonomous_reconcile import repair_days


class SeedReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);(self.root/"data/canonical").mkdir(parents=True)
        self.seed=self.root/"data/xnet_owner_balances_seed.csv";self.seed.write_text("owner,xnet_balance\nother,100\n")
        self.current=capture();self.rows,_=derive_transaction(self.current)
        self.now=datetime.fromtimestamp(self.current["result"]["blockTime"],timezone.utc)+timedelta(hours=8)
        self.historical=deepcopy(self.current)
        self.historical.update(signature="old")
        self.historical["request"]["params"][0]="old"
        self.historical["result"]["transaction"]["signatures"]=["old"]
        self.historical["result"]["blockTime"]=int(datetime(2026,10,2,tzinfo=timezone.utc).timestamp())
        self.historical["result"]["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"]="323"
        self.historical["result"]["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"]="200"
        self.fetch=Mock(side_effect=lambda tx:self.current if tx=="tx" else self.historical)
        self.listing=Mock(return_value=[{"signature":"tx","blockTime":self.current["result"]["blockTime"],"err":None},
            {"signature":"old","blockTime":self.historical["result"]["blockTime"],"err":None}])
        self.state={"source_errors":{"holder_integrity":{"error":"Negative holder balance after reduction: owner-a = -1"}},
                    "queries":{"xnet_transfers":{"completed_at_utc":self.now.isoformat()}},
                    "financial_journal":{"preserve":"sentinel"}}
        with (self.root/"data/canonical/xnet_transfers.csv").open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=TRANSFER_FIELDS);w.writeheader();w.writerows(self.rows)
    def run_probe(self,now=None):
        probe(self.state,self.root,lambda *_:{"latest_balance_event_time":"2026-10-03T00:00:00Z"},
              now or self.now,list_signatures=self.listing,fetch=self.fetch)
    def test_preseed_balance_conflict_requires_review_without_financial_mutation(self):
        seed_before=self.seed.read_bytes();ledger=self.root/"data/canonical/xnet_transfers.csv";before=ledger.read_bytes()
        self.run_probe()
        self.assertTrue(held(self.state))
        self.assertEqual(self.state["holder_seed_review"]["observed_raw_balance"],"200")
        self.assertEqual(self.state["financial_journal"],{"preserve":"sentinel"})
        self.assertEqual(self.seed.read_bytes(),seed_before);self.assertEqual(ledger.read_bytes(),before)
        self.assertTrue(self.state["holder_seed_review"]["absence_of_intermediate_signatures_is_not_proof_of_complete_history"])
    def test_existing_seeded_owner_is_not_replaced_by_rpc_balance(self):
        self.seed.write_text("owner,xnet_balance\nowner-a,0\n")
        self.run_probe();self.fetch.assert_not_called();self.assertFalse(held(self.state))
    def test_current_prebalance_alone_is_not_an_opening_balance(self):
        self.listing.return_value=self.listing.return_value[:1]
        self.run_probe();self.assertFalse(held(self.state))
    def test_possible_postseed_inflow_keeps_ordinary_day_recovery_available(self):
        self.listing.return_value[1]["blockTime"]=int((self.now-timedelta(hours=12)).timestamp())
        self.run_probe();self.assertFalse(held(self.state));self.assertEqual(self.fetch.call_count,1)
        self.assertTrue(repair_days(self.state,{},self.now,seed_cutoff="2026-10-03"))
    def test_unmatched_preseed_amount_or_owner_is_not_classified(self):
        self.historical["result"]["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"]="324"
        self.historical["result"]["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"]="201"
        self.run_probe();self.assertFalse(held(self.state))
    def test_bad_current_or_historical_proof_cannot_create_review_evidence(self):
        self.historical["result"]["meta"].pop("innerInstructions")
        self.run_probe();self.assertFalse(held(self.state))
    def test_review_hold_stops_speculative_sweeps_but_keeps_precise_witness_days(self):
        self.run_probe()
        self.assertEqual(repair_days(self.state,{},self.now,seed_cutoff="2026-10-03"),[])
        day=self.now.date().isoformat();self.state["reconciliation"]={"pending_mismatch_days":[day]}
        self.assertEqual(repair_days(self.state,{},self.now,seed_cutoff="2026-10-03"),[day])
    def test_evidence_is_retained_and_does_not_keep_polling_after_conflict(self):
        self.run_probe();before=deepcopy(self.state);count=self.fetch.call_count
        self.run_probe(self.now+timedelta(days=2))
        self.assertEqual(self.fetch.call_count,count);self.assertEqual(self.state,before)
    def test_probe_outage_is_bounded_and_does_not_invent_a_seed_conflict(self):
        self.fetch.side_effect=OSError("429")
        self.run_probe();self.run_probe(self.now+timedelta(hours=1))
        self.assertEqual(self.fetch.call_count,1);self.assertFalse(held(self.state))
        self.assertIn("last_probe_error",self.state["holder_seed_review"])
    def test_spam_does_not_count_as_an_intervening_token_transfer(self):
        spam=deepcopy(self.current);spam["result"]["meta"]["preTokenBalances"]=[];spam["result"]["meta"]["postTokenBalances"]=[]
        self.fetch.side_effect=lambda tx:spam if tx=="spam" else self.current if tx=="tx" else self.historical
        self.listing.return_value.insert(1,{"signature":"spam","err":None,"blockTime":self.historical["result"]["blockTime"]+1})
        self.run_probe();self.assertTrue(held(self.state));self.assertEqual(self.fetch.call_count,3)


if __name__=="__main__":unittest.main()
