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
from v3_indexer_lag import probe,reconcile_witnesses,recent_signatures
from v3_live_state_store import StateError
from v3_autonomy_watchdog import evaluate


class IndexerLagTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);(self.root/"data/canonical").mkdir(parents=True)
        self.cap=capture();self.rows,_=derive_transaction(self.cap)
        self.event=datetime.fromtimestamp(self.cap["result"]["blockTime"],timezone.utc)
        self.now=self.event+timedelta(hours=8)
        self.state={"queries":{"xnet_transfers":{"completed_at_utc":(self.now-timedelta(hours=1)).isoformat()}},
                    "financial_journal":{"untouched":"sentinel"}}
        self.write([])
        self.listing=Mock(return_value=[{"signature":"tx","blockTime":self.cap["result"]["blockTime"],"err":None}])
        self.fetch=Mock(return_value=self.cap)
    def write(self,rows):
        with (self.root/"data/canonical/xnet_transfers.csv").open("w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=TRANSFER_FIELDS);w.writeheader();w.writerows(rows)
    def run_probe(self,now=None):
        probe(self.state,self.root,lambda *_:{"latest_balance_event_time":"2026-10-03T00:00:00Z"},
              now or self.now,list_signatures=self.listing,fetch=self.fetch)
    def test_finalized_mature_missing_transaction_enqueues_day_without_canonical_insertion(self):
        before=(self.root/"data/canonical/xnet_transfers.csv").read_bytes()
        self.run_probe()
        day=self.rows[0]["block_date"]
        self.assertEqual(self.state["reconciliation"]["pending_mismatch_days"],[day])
        self.assertIn("indexer_lag",self.state["source_errors"])
        self.assertEqual((self.root/"data/canonical/xnet_transfers.csv").read_bytes(),before)
        self.assertEqual(self.state["financial_journal"],{"untouched":"sentinel"})
    def test_recent_or_not_yet_polled_activity_is_not_called_an_indexing_gap(self):
        self.run_probe(self.event+timedelta(hours=2));self.fetch.assert_not_called()
        self.state.pop("indexer_lag_watch")
        self.state["queries"]["xnet_transfers"]["completed_at_utc"]=(self.event-timedelta(hours=1)).isoformat()
        self.run_probe();self.fetch.assert_not_called()
    def test_known_transaction_and_failed_transaction_do_not_buy_proof(self):
        self.write(self.rows);self.run_probe();self.fetch.assert_not_called()
        self.state.pop("indexer_lag_watch");self.write([])
        self.listing.return_value[0]["err"]={"InstructionError":1}
        self.run_probe();self.fetch.assert_not_called()
    def test_mint_mention_or_partial_rpc_is_not_inferred_as_a_transfer(self):
        self.cap["result"]["meta"].pop("innerInstructions")
        self.run_probe()
        self.assertFalse(self.state["indexer_lag_watch"]["witnesses"])
        self.assertNotIn("indexer_lag",self.state.get("source_errors",{}))
        self.assertIn("last_probe_error",self.state["indexer_lag_watch"])
    def test_successful_probe_has_six_hour_bound(self):
        self.run_probe();count=self.listing.call_count
        self.run_probe(self.now+timedelta(hours=5))
        self.assertEqual(self.listing.call_count,count)
    def test_rpc_outage_retries_after_one_hour_not_every_cron(self):
        self.listing.side_effect=OSError("503")
        self.run_probe();self.run_probe(self.now+timedelta(minutes=30))
        self.assertEqual(self.listing.call_count,1)
        self.run_probe(self.now+timedelta(hours=1,seconds=1))
        self.assertEqual(self.listing.call_count,2)
        self.fetch.assert_not_called()
    def test_previous_head_avoids_only_sampling_perpetually_young_high_activity(self):
        self.run_probe()
        self.listing.reset_mock()
        self.run_probe(self.now+timedelta(hours=6))
        self.assertEqual(self.listing.call_count,2)
        self.assertEqual(self.listing.call_args.kwargs,{"before":"tx"})
    def test_exact_witness_resolution_does_not_clear_a_conflicting_owner_or_other_hold(self):
        self.run_probe();self.state["source_errors"]["other"]={"error":"preserve"}
        wrong=deepcopy(self.rows);wrong[0]["to_owner"]="wrong"
        self.assertTrue(reconcile_witnesses(self.state,wrong))
        self.assertEqual(reconcile_witnesses(self.state,self.rows),set())
        self.assertNotIn("indexer_lag",self.state["source_errors"])
        self.assertIn("other",self.state["source_errors"])
        self.assertIn("tx",self.state["indexer_lag_watch"]["witnesses"])
    def test_lost_or_tampered_capture_never_clears_the_lag(self):
        self.run_probe();self.state["transfer_instruction_proofs"]["tx"]["capture"]["result"]["slot"]+=1
        with self.assertRaises(StateError):reconcile_witnesses(self.state,self.rows)
        self.assertIn("indexer_lag",self.state["source_errors"])
    def test_known_gap_is_not_a_healthy_collector_even_with_a_fresh_poll(self):
        self.run_probe()
        report=evaluate(self.state,{"sources":[{"key":"xnet_transfers","cadence_minutes":120}]},
                        {"status":"HEALTHY"},self.now)
        self.assertEqual(report["status"],"unhealthy")
        self.assertTrue(any("witnesses" in text for text in report["problems"]))
    def test_only_two_transaction_proofs_per_probe(self):
        caps={};items=[]
        for tx in ("a","b","c"):
            cap=deepcopy(self.cap);cap["signature"]=tx
            cap["request"]["params"][0]=tx;cap["result"]["transaction"]["signatures"]=[tx]
            caps[tx]=cap;items.append({"signature":tx,"err":None,"blockTime":cap["result"]["blockTime"]})
        self.listing.return_value=items;self.fetch.side_effect=lambda tx:caps[tx]
        self.run_probe();self.assertEqual(self.fetch.call_count,2)
        self.assertEqual(len(self.state["indexer_lag_watch"]["witnesses"]),2)
    def test_proven_backlog_is_priced_in_remaining_period_forecast(self):
        import importlib.util
        loader=importlib.util.spec_from_file_location("lag_forecast_refresh",ROOT/"scripts/v3_refresh_dashboard.py")
        runtime=importlib.util.module_from_spec(loader);loader.loader.exec_module(runtime)
        day=self.rows[0]["block_date"]
        state={"source_errors":{"indexer_lag":{"unresolved_days":[day]}}}
        with patch.object(runtime,"now",return_value=self.now),patch.object(runtime,"autonomous_estimate",return_value={
                "expected_credits":0.2,"cost_estimate_credits":0.4,"evidence":{}}):
            forecast=runtime.standing_forecast(state,(self.now+timedelta(days=1)).isoformat())
        entry=next(row for row in forecast["entries"] if row["key"]=="indexer_lag:"+day)
        self.assertEqual(entry["runs"],4)
        self.assertEqual(entry["expected_compute_credits"],0.2)
        state["additive_day_recovery"]={day:{"indexer_misses":3}}
        with patch.object(runtime,"now",return_value=self.now),patch.object(runtime,"autonomous_estimate",return_value={
                "expected_credits":0.2,"cost_estimate_credits":0.4,"evidence":{}}):
            forecast=runtime.standing_forecast(state,(self.now+timedelta(days=1)).isoformat())
        entry=next(row for row in forecast["entries"] if row["key"]=="indexer_lag:"+day)
        self.assertEqual(entry["runs"],1)  # No repeated purchase every six hours forever.
    def test_unsupported_candidates_cannot_starve_later_provable_activity(self):
        caps={};items=[]
        for tx in ("a","b","c"):
            cap=deepcopy(self.cap);cap["signature"]=tx
            cap["request"]["params"][0]=tx;cap["result"]["transaction"]["signatures"]=[tx]
            if tx!="c":cap["result"]["meta"].pop("innerInstructions")
            caps[tx]=cap;items.append({"signature":tx,"err":None,"blockTime":cap["result"]["blockTime"]})
        self.listing.return_value=items;self.fetch.side_effect=lambda tx:caps[tx]
        self.run_probe();self.assertFalse(self.state["indexer_lag_watch"]["witnesses"])
        self.run_probe(self.now+timedelta(hours=1,seconds=1))
        self.assertIn("c",self.state["indexer_lag_watch"]["witnesses"])
        self.assertEqual(self.fetch.call_count,4)
    def test_non_object_rpc_metadata_is_a_typed_free_probe_failure(self):
        from unittest.mock import MagicMock
        response=MagicMock();response.__enter__.return_value.read.return_value=b'[]'
        with patch("v3_indexer_lag.urllib.request.urlopen",return_value=response):
            with self.assertRaisesRegex(ValueError,"activity metadata"):recent_signatures()
    def test_extreme_external_timestamp_is_skipped_without_overflow_or_probe(self):
        self.listing.return_value[0]["blockTime"]=10**40
        self.run_probe();self.fetch.assert_not_called()


if __name__=="__main__":unittest.main()
