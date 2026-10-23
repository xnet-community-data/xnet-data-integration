from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / "scripts"))
from v3_autonomous_reconcile import repair_days, _publish_day, recover_additive_days, route_seeded_mismatches, owner_seed_day
from v3_additive_day import AdditiveDayRejected
from v3_live_state_store import StateError, encoded
from v3_reduce_chain import normalise_transfer, TRANSFER_FIELDS

NOW=datetime(2026,10,20,12,tzinfo=timezone.utc)
SQL="a"*64


class AutonomousReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        for folder in ("data/canonical","data/current"):(self.root/folder).mkdir(parents=True)
        self.ledger=self.root/"data/canonical/xnet_transfers.csv"
        row=normalise_transfer(json.loads((ROOT/"tests/fixtures/dune_repair_20261015.json").read_text())["data"]["rows"][0])
        out=io.StringIO(newline=""); writer=csv.DictWriter(out,fieldnames=TRANSFER_FIELDS);writer.writeheader();writer.writerow(row)
        self.original=out.getvalue().encode();self.ledger.write_bytes(self.original)
        (self.root/"data/xnet_owner_balances_seed_metadata.json").write_text(json.dumps({"latest_balance_event_time":"2026-09-28T00:00:00Z"}))
        self.key="day_repair:2026-10-14"
        self.state={"queries":{"xnet_transfers":{"completed_at_utc":"2026-10-14T02:00:00Z"}},
            "pending_executions":{self.key:{"execution_id":"repair-id","query_id":8894038,"query_parameters":{"oldest_days_ago":6,"newest_days_ago":6},"operation_approval":{
                "absolute_day":"2026-10-14","sql_sha256":SQL,"query_parameters":{"oldest_days_ago":6,"newest_days_ago":6}}}}}
        config={"autonomous_operation":{"queries":{"8894038":{"sql_sha256":SQL}},"max_day_repairs_per_run":2},
                "reconciliation":{"repair_query_id":8894038,"repair_max_run_credits":5,"repair_max_export_points":50000,
                                  "repair_max_export_bytes":3000000},"performance":"medium"}
        def load(path,default):return json.loads(path.read_text()) if path.exists() else default
        def save(path,value):path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(encoded(value))
        def stage(state,spec,record,effect):
            del state["pending_executions"][spec["key"]]
            state.setdefault("consumed_executions",{})[record["execution_id"]]={"effect":effect}
        self.runtime=SimpleNamespace(ROOT=self.root,CONFIG=config,STATE=self.root/"data/current/v3_refresh_state.json",
            now=lambda:NOW,stamp=lambda:NOW.isoformat().replace("+00:00","Z"),encoded=encoded,load=load,save=save,
            run=Mock(),publish=Mock(),checkpoint=Mock(),stage_consumption=stage,sync_holder_integrity_error=Mock(),
            write_operational_health=Mock(),execute=Mock(),export_source=Mock())
        self.spec={"key":self.key,"query_id":8894038}
        fixture=json.loads((ROOT/"tests/fixtures/dune_repair_20261014.json").read_text())
        self.result={"state":"QUERY_STATE_COMPLETED","result":{"rows":fixture["data"]["rows"]}}
        self.record={"query_id":8894038,"execution_id":"repair-id","query_parameters":{"oldest_days_ago":6,"newest_days_ago":6},
                     "completed_at_utc":"2026-10-20T11:59:00Z","execution_started_at":"2026-10-20T11:58:00Z","result_metadata":{"total_row_count":198}}

    def test_real_captured_day_is_published_additively_with_consumption(self):
        self.assertTrue(_publish_day(self.runtime,self.state,self.spec,self.record,self.result,"2026-10-14"))
        self.assertTrue(self.ledger.read_bytes().startswith(self.original))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.ledger.read_text())))),199)
        self.assertNotIn(self.key,self.state["pending_executions"])
        self.assertIn("repair-id",self.state["consumed_executions"])
        receipt=next(iter(self.state["autonomous_day_receipts"].values()))
        self.assertEqual(receipt["added_rows"],198)
        self.assertEqual(receipt["deleted_rows"],0)
        self.assertEqual(self.state["queries"]["xnet_transfers"]["continuous_coverage_end_utc"],"2026-10-15T00:00:00Z")
        self.runtime.publish.assert_called_once()

    def test_missing_immutable_day_or_sql_receipt_refuses_before_any_mutation(self):
        for field in ("absolute_day","sql_sha256"):
            state=deepcopy(self.state);del state["pending_executions"][self.key]["operation_approval"][field]
            with self.assertRaises(AdditiveDayRejected):_publish_day(self.runtime,state,self.spec,self.record,self.result,"2026-10-14")
        self.assertEqual(self.ledger.read_bytes(),self.original);self.runtime.run.assert_not_called()

    def test_incomplete_response_is_not_accepted(self):
        self.result["result"]["rows"].pop()
        with self.assertRaises(AdditiveDayRejected):_publish_day(self.runtime,self.state,self.spec,self.record,self.result,"2026-10-14")
        self.assertEqual(self.ledger.read_bytes(),self.original)

    def test_provider_started_after_midnight_cannot_rebind_even_empty_day(self):
        self.record["execution_started_at"]="2026-10-21T00:00:01Z"
        self.record["result_metadata"]["total_row_count"]=0
        self.result["result"]["rows"]=[]
        with self.assertRaisesRegex(AdditiveDayRejected,"scope changed"):
            _publish_day(self.runtime,self.state,self.spec,self.record,self.result,"2026-10-14")
        self.assertEqual(self.ledger.read_bytes(),self.original)

    def test_reducer_failure_restores_data_and_markers_without_publication(self):
        before=deepcopy(self.state)
        self.runtime.run.side_effect=RuntimeError("strict failure")
        with self.assertRaisesRegex(RuntimeError,"strict failure"):_publish_day(self.runtime,self.state,self.spec,self.record,self.result,"2026-10-14")
        self.assertEqual(self.ledger.read_bytes(),self.original);self.assertEqual(self.state,before)
        self.runtime.publish.assert_not_called()

    def test_uncertain_publication_preserves_candidate_never_compensates(self):
        self.runtime.publish.side_effect=StateError("uncertain transport")
        with self.assertRaises(StateError):_publish_day(self.runtime,self.state,self.spec,self.record,self.result,"2026-10-14")
        self.assertNotEqual(self.ledger.read_bytes(),self.original)
        self.assertNotIn(self.key,self.state["pending_executions"])
        self.runtime.publish.assert_called_once()

    def test_gap_days_include_today_as_explicit_partial_scope_and_are_bounded(self):
        days=repair_days(self.state,self.runtime.CONFIG,NOW)
        self.assertEqual(days[0],"2026-10-14");self.assertEqual(days[-1],"2026-10-20")
        self.runtime.execute.side_effect=RuntimeError("provider temporarily unavailable")
        self.assertFalse(recover_additive_days(self.runtime,self.state,{}))
        self.assertEqual(self.runtime.execute.call_count,2)
        self.assertTrue(all(v["status"]=="deferred" for v in self.state["additive_day_recovery"].values()))

    def test_recovery_never_rebinds_pending_relative_offsets_after_midnight(self):
        self.state["queries"]={}
        self.state["pending_executions"][self.key]["query_parameters"]={"oldest_days_ago":5,"newest_days_ago":5}
        self.runtime.execute.side_effect=RuntimeError("pending transport")
        recover_additive_days(self.runtime,self.state,{})
        self.assertEqual(self.runtime.execute.call_args.args[1],{"oldest_days_ago":5,"newest_days_ago":5})

    def test_backoff_is_scoped_to_day_not_all_collection(self):
        self.state["additive_day_recovery"]={"2026-10-14":{"next_retry_at_utc":"2026-10-21T00:00:00Z"}}
        days=repair_days(self.state,self.runtime.CONFIG,NOW)
        self.assertNotIn("2026-10-14",days);self.assertIn("2026-10-15",days)

    def test_seeded_days_are_filtered_before_execution_not_after_spending(self):
        (self.root/"data/xnet_owner_balances_seed_metadata.json").write_text(json.dumps({"latest_balance_event_time":"2026-10-20 01:46:12.000 UTC"}))
        self.assertEqual(owner_seed_day(self.root,self.runtime.load),"2026-10-20")
        self.assertFalse(recover_additive_days(self.runtime,self.state,{}))
        self.runtime.execute.assert_not_called();self.runtime.export_source.assert_not_called()
        self.assertIn(self.key,self.state["pending_executions"])

    def test_seed_mismatches_remain_advisory_without_starving_post_seed_repair(self):
        days=["2026-10-02","2026-10-03","2026-10-14"]
        eligible=route_seeded_mismatches(self.state,days,"2026-10-03")
        self.assertEqual(eligible,["2026-10-14"])
        self.assertEqual(self.state["reconciliation"]["seeded_mismatch_days"],["2026-10-02","2026-10-03"])
        self.assertEqual(repair_days(self.state,self.runtime.CONFIG,NOW,seed_cutoff="2026-10-20"),[])

    def test_current_day_acceptance_advances_only_to_engine_snapshot_not_midnight(self):
        day="2026-10-20";key="day_repair:"+day
        pending=self.state["pending_executions"].pop(self.key)
        pending["query_parameters"]={"oldest_days_ago":0,"newest_days_ago":0}
        pending["operation_approval"].update(absolute_day=day,query_parameters=pending["query_parameters"])
        self.state["pending_executions"][key]=pending
        self.state["queries"]["xnet_transfers"]={"completed_at_utc":"2026-10-20T01:00:00Z"}
        self.state["source_errors"]={"xnet_transfers":{"retry_kind":"bounded_day_catchup"}}
        self.record["query_parameters"]=pending["query_parameters"]
        self.record["result_metadata"]["total_row_count"]=0
        self.result["result"]["rows"]=[]
        _publish_day(self.runtime,self.state,{**self.spec,"key":key},self.record,self.result,day)
        self.assertEqual(self.state["queries"]["xnet_transfers"]["continuous_coverage_end_utc"],"2026-10-20T11:58:00Z")
        self.assertNotIn("xnet_transfers",self.state["source_errors"])
        receipt=next(iter(self.state["autonomous_day_receipts"].values()))
        self.assertFalse(receipt["full_current_day_claimed"])

    def test_gap_recovery_never_advances_over_an_unverified_day(self):
        self.state["queries"]["xnet_transfers"]["completed_at_utc"]="2026-10-13T01:00:00Z"
        _publish_day(self.runtime,self.state,self.spec,self.record,self.result,"2026-10-14")
        self.assertNotIn("continuous_coverage_end_utc",self.state["queries"]["xnet_transfers"])

    def test_gap_outside_authorized_horizon_does_not_buy_endless_recent_scans(self):
        self.state["queries"]["xnet_transfers"]["completed_at_utc"]="2026-08-01T00:00:00Z"
        self.state["pending_executions"]={}
        self.state["source_errors"]={"holder_integrity":{"error":"missing historical flows"}}
        self.assertEqual(repair_days(self.state,self.runtime.CONFIG,NOW),[])

    def test_missing_seed_receipt_refuses_recovery_before_any_provider_operation(self):
        (self.root/"data/xnet_owner_balances_seed_metadata.json").unlink()
        with self.assertRaises(StateError):recover_additive_days(self.runtime,self.state,{})
        self.runtime.execute.assert_not_called();self.runtime.export_source.assert_not_called()


if __name__=="__main__":unittest.main()
