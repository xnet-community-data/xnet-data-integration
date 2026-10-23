from copy import deepcopy
from datetime import datetime,timedelta,timezone
import base64,csv,hashlib,json,os,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"));sys.path.insert(0,str(ROOT/"tests"))
import v3_reduce_chain as reducer
from v3_holder_snapshot import PATH,AUTHORIZATION_REF,SnapshotRejected,make_baseline,ACQUISITION_VERSION
from v3_holder_snapshot_runtime import activate,refresh
from v3_live_state_store import MACHINE_OWNED,StateError
from test_v3_holder_snapshot import fixture,NOW,OWNER_A,OWNER_B


class HolderSnapshotRuntimeTests(unittest.TestCase):
    def test_free_holder_activation_preserves_the_approved_dune_policy_hash(self):
        config=json.loads((ROOT/"config/v3_refresh.json").read_text())
        digest=hashlib.sha256(json.dumps(config["autonomous_operation"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
        self.assertEqual(digest,"acb6fccc05230070c274d8a296f59dc26f51068738ff588138c55ffd7d69d9e4")
        self.assertFalse(config["holder_reconciliation"]["dune_spending_authority"])
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        for path in ("data/current","data/derived","data/canonical"):(self.root/path).mkdir(parents=True)
        self.seed=self.root/"data/xnet_owner_balances_seed.csv"
        self.seed.write_text("owner,xnet_balance\nold-owner,5\n")
        self.meta=self.root/"data/xnet_owner_balances_seed_metadata.json"
        self.meta.write_text(json.dumps({"latest_balance_event_time":"2026-10-03T00:00:00Z"}))
        (self.root/"data/canonical/xnet_transfers.csv").write_text("event_id,block_time\n")
        (self.root/"data/current/xnet_holder_state.json").write_text(json.dumps({
            "positive_holder_count":1,"latest_transfer_event_utc":"2026-10-22T00:00:00Z"}))
        (self.root/"data/derived/xnet_owner_balances.csv").write_text("owner,xnet_balance\nold-owner,5\n")
        (self.root/"data/current/xnet_holder_integrity.json").write_text(json.dumps({"status":"DEGRADED"}))
        self.state={"schema_version":1,"queries":{},"paused":False,
            "financial_journal":{"obligations":{"old":{"cost_credits":None,"unresolved":True}}},
            "standing_credit_budget":{"preserve":"financial book"},
            "holder_reconciliation":{"schema_version":1,"baseline_history":[],"acquisition_version":ACQUISITION_VERSION},
            "holder_seed_review":{"status":"requires_review","error":"opening conflict","capture":"retain"},
            "source_errors":{"holder_integrity":{"error":"opening conflict"}}}
        self.clock=NOW
        self.runtime=SimpleNamespace(ROOT=self.root,STATE=self.root/"data/current/v3_refresh_state.json",
            HEALTH=self.root/"data/current/xnet_chain_health.json",
            CONFIG={"holder_reconciliation":{"enabled":True,"approval_ref":AUTHORIZATION_REF,"cadence_minutes":120}},
            now=lambda:self.clock,stamp=lambda:self.clock.isoformat().replace("+00:00","Z"),
            save=self.save,load=lambda path,default:json.loads(path.read_text()) if path.exists() else default,
            checkpoint=Mock(),publish=Mock(),write_operational_health=Mock(),
            sync_holder_integrity_error=self.sync,run=Mock(side_effect=self.run_script))
    def save(self,path,value):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2)+"\n")
    def sync(self,state):
        q=self.runtime.load(self.root/"data/current/xnet_holder_integrity.json",{})
        if q.get("status")=="HEALTHY":state["source_errors"].pop("holder_integrity",None)
    def run_script(self,script,*args):
        if script=="v3_reduce_chain.py":
            previous=Path.cwd()
            try:
                os.chdir(self.root)
                with patch.object(reducer,"datetime",wraps=datetime) as clock:
                    clock.now.return_value=self.clock
                    reducer.derive_holders_guarded([],allow_stale_holders=True)
            finally:os.chdir(previous)
    def activate(self):
        activate(self.runtime,self.state)
    def baseline(self,slot=100):return make_baseline(fixture(self.clock,slot))
    def test_configuration_alone_does_not_activate_a_new_source(self):
        acquire=Mock();self.assertFalse(refresh(self.runtime,self.state,acquire_snapshot=acquire))
        acquire.assert_not_called();self.runtime.publish.assert_not_called()
    def test_explicit_activation_is_durable_and_is_not_spending_authority(self):
        old=deepcopy(self.state["financial_journal"]);self.activate()
        self.assertEqual(self.state["holder_reconciliation_authorization"]["approval_ref"],AUTHORIZATION_REF)
        self.assertFalse(self.state["holder_reconciliation_authorization"]["dune_spending_authority"])
        self.runtime.checkpoint.assert_called_once_with(self.state)
        self.assertEqual(self.state["financial_journal"],old)
        self.activate();self.assertEqual(self.runtime.checkpoint.call_count,1)
    def test_full_snapshot_resolves_working_basis_without_rewriting_old_facts(self):
        self.activate();before=deepcopy(self.state);seed=self.seed.read_bytes()
        canonical=(self.root/"data/canonical/xnet_transfers.csv").read_bytes()
        baseline=self.baseline()
        self.assertTrue(refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=baseline)))
        self.runtime.publish.assert_called_once_with()
        holder=self.runtime.load(self.root/"data/current/xnet_holder_state.json",{})
        quality=self.runtime.load(self.root/"data/current/xnet_holder_integrity.json",{})
        self.assertEqual(holder["positive_holder_count"],2)
        self.assertEqual(holder["outstanding_balance_sum_xnet"],"3.5")
        self.assertEqual(holder["balance_as_of_utc"],baseline["receipt"]["block_time_utc"])
        self.assertIsNone(holder["holders_created_since_seed"])
        self.assertEqual(quality["status"],"HEALTHY")
        self.assertEqual(quality["baseline_id"],baseline["baseline_id"])
        self.assertEqual(self.state["holder_seed_review"]["capture"],"retain")
        self.assertEqual(self.state["holder_seed_review"]["status"],"resolved_by_verified_finalized_snapshot")
        self.assertEqual(self.state["financial_journal"],before["financial_journal"])
        self.assertEqual(self.state["standing_credit_budget"],before["standing_credit_budget"])
        self.assertEqual(self.state["queries"],before["queries"])
        self.assertEqual(self.seed.read_bytes(),seed)
        self.assertEqual((self.root/"data/canonical/xnet_transfers.csv").read_bytes(),canonical)
        self.assertFalse(self.state["paused"])
        self.assertIn(PATH,MACHINE_OWNED)
    def test_snapshot_stays_independent_of_late_unbalanced_indexed_transfers(self):
        self.activate();baseline=self.baseline()
        refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=baseline))
        self.save(self.runtime.STATE,self.state)
        old=(self.root/"data/derived/xnet_owner_balances.csv").read_bytes()
        previous=Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(reducer,"datetime",wraps=datetime) as clock:
                clock.now.return_value=self.clock
                reducer.derive_holders_guarded([{"block_time":"2026-10-23T19:59:59Z",
                    "from_owner":"unseeded","amount_raw":"999999999999999","action":"transfer"}],True)
        finally:os.chdir(previous)
        self.assertEqual((self.root/"data/derived/xnet_owner_balances.csv").read_bytes(),old)
        self.assertEqual(self.runtime.load(self.root/"data/current/xnet_holder_integrity.json",{})["status"],"HEALTHY")
    def test_ordinary_runs_reuse_fresh_snapshot_until_two_hour_renewal(self):
        self.activate();refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        acquire=Mock();self.clock+=timedelta(minutes=119)
        self.assertFalse(refresh(self.runtime,self.state,acquire_snapshot=acquire));acquire.assert_not_called()
        self.clock+=timedelta(minutes=2);acquire.return_value=self.baseline(slot=101)
        self.assertTrue(refresh(self.runtime,self.state,acquire_snapshot=acquire));acquire.assert_called_once()
    def test_free_source_failure_preserves_data_and_retries_without_a_pause(self):
        self.activate();old=(self.root/"data/derived/xnet_owner_balances.csv").read_bytes()
        acquire=Mock(side_effect=SnapshotRejected("429"))
        self.assertFalse(refresh(self.runtime,self.state,acquire_snapshot=acquire))
        self.assertFalse(self.state["paused"]);self.runtime.publish.assert_not_called()
        self.assertEqual((self.root/"data/derived/xnet_owner_balances.csv").read_bytes(),old)
        self.clock+=timedelta(minutes=30);refresh(self.runtime,self.state,acquire_snapshot=acquire)
        self.assertEqual(acquire.call_count,1)
        self.clock+=timedelta(minutes=31);acquire.side_effect=None;acquire.return_value=self.baseline()
        self.assertTrue(refresh(self.runtime,self.state,acquire_snapshot=acquire))
    def test_client_upgrade_retries_an_old_alignment_failure_without_erasing_evidence(self):
        self.activate();lane=self.state["holder_reconciliation"]
        lane.update(acquisition_version=1,last_error="old two-attempt bank alignment",
                    next_retry_at_utc=(self.clock+timedelta(hours=1)).isoformat())
        acquire=Mock(return_value=self.baseline())
        self.assertTrue(refresh(self.runtime,self.state,acquire_snapshot=acquire))
        acquire.assert_called_once()
        history=self.state["holder_reconciliation"]["acquisition_policy_history"]
        self.assertEqual(history[0]["previous_error"],"old two-attempt bank alignment")
        self.assertEqual(history[0]["previous_version"],1)
        self.assertFalse(self.state["paused"])
    def test_build_failure_rolls_back_only_candidate_machine_files_and_state(self):
        self.activate();before=deepcopy(self.state);old=(self.root/"data/derived/xnet_owner_balances.csv").read_bytes()
        real=self.run_script
        def fail(script,*args):
            if script=="v3_build_chain_snapshot.py":raise RuntimeError("builder failed")
            return real(script,*args)
        self.runtime.run.side_effect=fail
        with self.assertRaisesRegex(RuntimeError,"builder failed"):
            refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        self.assertEqual(self.state,before)
        self.assertEqual((self.root/"data/derived/xnet_owner_balances.csv").read_bytes(),old)
        self.assertFalse((self.root/PATH).exists());self.runtime.publish.assert_not_called()
    def test_ambiguous_publication_is_never_compensated_by_old_state(self):
        self.activate();self.runtime.publish.side_effect=StateError("accepted response lost")
        with self.assertRaises(StateError):refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        self.assertTrue((self.root/PATH).exists())
        self.assertEqual(self.state["holder_seed_review"]["status"],"resolved_by_verified_finalized_snapshot")
        self.runtime.publish.assert_called_once_with()
    def test_accepted_slot_cannot_regress(self):
        self.activate();refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        active=self.state["holder_reconciliation"]["active_baseline_id"]
        self.clock+=timedelta(hours=3)
        self.assertFalse(refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline(slot=99))))
        self.assertEqual(self.state["holder_reconciliation"]["active_baseline_id"],active)
    def test_contradictory_owner_balances_at_the_same_finalized_slot_are_refused(self):
        self.activate();refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        active=self.state["holder_reconciliation"]["active_baseline_id"]
        self.clock+=timedelta(hours=3);capture=fixture(self.clock,slot=100)
        rows=capture["responses"][0]["result"]["value"]
        for index,amount in ((0,90_000_000),(2,210_000_000)):
            data=bytearray(base64.b64decode(rows[index]["account"]["data"][0]))
            data[64:72]=amount.to_bytes(8,"little")
            rows[index]["account"]["data"][0]=base64.b64encode(data).decode()
        self.assertFalse(refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=make_baseline(capture))))
        self.assertEqual(self.state["holder_reconciliation"]["active_baseline_id"],active)
        self.assertIn("Conflicting holder state",self.state["source_errors"]["holder_snapshot"]["error"])
    def test_stale_snapshot_preserves_last_good_and_does_not_fake_freshness(self):
        self.activate();refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        old=(self.root/"data/derived/xnet_owner_balances.csv").read_bytes()
        self.save(self.runtime.STATE,self.state);self.clock+=timedelta(hours=7)
        self.run_script("v3_reduce_chain.py")
        quality=self.runtime.load(self.root/"data/current/xnet_holder_integrity.json",{})
        self.assertEqual(quality["status"],"DEGRADED")
        self.assertEqual(quality["reason"],"holder_snapshot_unavailable")
        self.assertEqual((self.root/"data/derived/xnet_owner_balances.csv").read_bytes(),old)
    def test_free_holder_recovery_cannot_clear_a_financial_pause(self):
        self.activate();self.state["paused"]=True;self.state["last_error"]="operator financial hold"
        self.assertTrue(refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline())))
        self.assertTrue(self.state["paused"]);self.assertEqual(self.state["last_error"],"operator financial hold")
    def test_holder_renewal_cannot_mutate_the_financial_book(self):
        self.activate();before=deepcopy(self.state)
        def corrupt(state):state["standing_credit_budget"]["preserve"]="changed"
        self.runtime.write_operational_health.side_effect=corrupt
        with self.assertRaisesRegex(StateError,"spending/accepted-query facts"):
            refresh(self.runtime,self.state,acquire_snapshot=Mock(return_value=self.baseline()))
        self.assertEqual(self.state,before);self.runtime.publish.assert_not_called()


if __name__=="__main__":unittest.main()
