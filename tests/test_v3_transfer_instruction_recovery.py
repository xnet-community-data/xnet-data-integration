from copy import deepcopy
from datetime import datetime, timedelta, timezone
import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
sys.path.insert(0,str(ROOT/"tests"))
from v3_transfer_instruction_recovery import (prepare_proofs, proven_group, digest,
    InstructionProofDeferred, fetch_transaction)
from v3_rpc_transfer_proof import derive_transaction, ProofRejected
from v3_additive_day import validate_additive_day
from test_v3_rpc_transfer_proof import capture


def multi_capture():
    """Two distinct amounts, same account pair; the production failure pattern."""
    c=capture()
    instruction=deepcopy(c["result"]["meta"]["innerInstructions"][0]["instructions"][0])
    instruction["parsed"]["info"]["tokenAmount"]["amount"]="7"
    c["result"]["meta"]["innerInstructions"][0]["instructions"].append(instruction)
    c["result"]["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"]="70"
    c["result"]["meta"]["postTokenBalances"][1]["uiTokenAmount"]["amount"]="130"
    return c


class InstructionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.capture=multi_capture()
        self.rows,self.receipt=derive_transaction(self.capture)

    def test_multiple_amounts_require_and_accept_exact_instruction_proof(self):
        state={};fetch=Mock(return_value=self.capture)
        proofs=prepare_proofs(state,[],self.rows,fetch=fetch)
        fetch.assert_called_once_with("tx")
        proven_group([],self.rows,proofs)
        self.assertEqual(len(proofs["tx"]["receipt"]["instructions"]),2)

    def test_reappearing_proven_canonical_group_needs_no_rpc(self):
        fetch=Mock(side_effect=AssertionError("unneeded RPC"))
        prepare_proofs({},self.rows,self.rows,fetch=fetch)
        fetch.assert_not_called()

    def test_additional_leg_preserves_existing_amount_and_requires_whole_group(self):
        proofs=prepare_proofs({},[self.rows[0]],self.rows,fetch=lambda _:self.capture)
        proven_group([self.rows[0]],self.rows,proofs)
        with self.assertRaises(ProofRejected):
            proven_group([self.rows[0]],[self.rows[1]],proofs)

    def test_changed_existing_owner_amount_time_or_multiplicity_cannot_be_replaced(self):
        for key,value in (("from_owner","wrong"),("amount_raw","999"),
                          ("block_time","2026-10-13T23:59:00Z"),("event_multiplicity","2")):
            old=deepcopy(self.rows);old[0][key]=value
            fetch=Mock()
            with self.subTest(key=key),self.assertRaises(ProofRejected):
                prepare_proofs({},old,self.rows,fetch=fetch)
            fetch.assert_not_called()

    def test_partial_or_tampered_proof_does_not_authorize_the_group(self):
        state={};proofs=prepare_proofs(state,[],self.rows,fetch=lambda _:self.capture)
        proofs["tx"]["capture"]["result"]["meta"].pop("innerInstructions")
        with self.assertRaises(ProofRejected):proven_group([],self.rows,proofs)
        proofs["tx"]["capture_sha256"]=digest(proofs["tx"]["capture"])
        with self.assertRaises(ProofRejected):proven_group([],self.rows,proofs)

    def test_fake_provider_extra_leg_is_not_balance_delta_inference(self):
        rows=deepcopy(self.rows);rows[1]["amount_raw"]="8"
        with self.assertRaises(ProofRejected):prepare_proofs({},[],rows,fetch=lambda _:self.capture)

    def test_network_outage_defers_without_dune_resubmission_or_erasing_proofs(self):
        state={};fetch=Mock(side_effect=InstructionProofDeferred("503"))
        with self.assertRaises(InstructionProofDeferred):prepare_proofs(state,[],self.rows,fetch=fetch)
        self.assertFalse(state.get("transfer_instruction_proofs"))

    def test_per_run_bound_is_finite_and_keeps_verified_progress_for_next_run(self):
        c2=deepcopy(self.capture)
        c2["signature"]="tx2";c2["request"]["params"][0]="tx2"
        c2["result"]["transaction"]["signatures"]=["tx2"]
        rows2,_=derive_transaction(c2)
        state={};fetch=Mock(side_effect=[self.capture,c2])
        with self.assertRaises(InstructionProofDeferred):
            prepare_proofs(state,[],self.rows+rows2,fetch=fetch,max_requests=1)
        self.assertIn("tx",state["transfer_instruction_proofs"])
        prepare_proofs(state,[],self.rows+rows2,fetch=fetch,max_requests=1)
        self.assertEqual(fetch.call_count,2)

    def test_day_validator_uses_same_proof_without_deleting_old_financial_rows(self):
        proofs=prepare_proofs({},[],self.rows,fetch=lambda _:self.capture)
        provider=[{k:v for k,v in row.items() if k!="event_id"} for row in self.rows]
        result=validate_additive_day([self.rows[0]],provider,self.rows[0]["block_date"],
            execution_id="paid",sql_sha256="a"*64,transaction_proofs=proofs)
        self.assertEqual(len(result["additions"]),1)
        self.assertEqual(result["receipt"]["deleted_rows"],0)


class RuntimeInstructionRecoveryTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location("instruction_refresh",ROOT/"scripts/v3_refresh_dashboard.py")
        self.refresh=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.refresh)
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);(self.root/"data/canonical").mkdir(parents=True)
        self.capture=multi_capture();self.rows,_=derive_transaction(self.capture)
        from v3_reduce_chain import TRANSFER_FIELDS
        self.ledger=self.root/"data/canonical/xnet_transfers.csv"
        with self.ledger.open("w",newline="") as f:csv.DictWriter(f,fieldnames=TRANSFER_FIELDS).writeheader()
        self.path=self.root/"result.json"
        self.path.write_text(json.dumps({"state":"QUERY_STATE_COMPLETED","result":{"rows":self.rows}}))
        self.root_patch=patch.object(self.refresh,"ROOT",self.root)
        self.root_patch.start();self.addCleanup(self.root_patch.stop)

    def test_runtime_accepts_two_proven_amounts_and_keeps_exact_raw_arithmetic(self):
        state={}
        with patch("v3_transfer_instruction_recovery.fetch_transaction",return_value=self.capture):
            self.refresh.validate_hot_transfers(self.path,state)
        result=json.loads(self.path.read_text())
        self.assertEqual({r["amount_raw"] for r in result["result"]["rows"]},{"7","123"})
        self.assertEqual(state["transfer_instruction_proofs"]["tx"]["capture"],self.capture)

    def test_exact_repeated_provider_row_is_not_silently_counted_twice(self):
        self.path.write_text(json.dumps({"state":"QUERY_STATE_COMPLETED","result":{"rows":[self.rows[0]]*2}}))
        with self.assertRaisesRegex(self.refresh.ManualReviewRequired,"Duplicate transfer event ID"):
            self.refresh.validate_hot_transfers(self.path,{})

    def test_only_identified_false_positive_hold_is_automatically_revalidated(self):
        state={"source_errors":{"xnet_transfers":{"manual_reconciliation_required":True,
            "error":"Ambiguous duplicate hot transfer locator"}},"pending_executions":{"xnet_transfers":{
                "execution_id":"paid","terminal_status":{"state":"QUERY_STATE_COMPLETED"}}}}
        with patch.object(self.refresh,"autonomous_enabled",return_value=True):
            self.assertFalse(self.refresh.source_in_backoff(state,"xnet_transfers"))
            state["source_errors"]["xnet_transfers"]["error"]="Actual conflicting old owner"
            self.assertTrue(self.refresh.source_in_backoff(state,"xnet_transfers"))

    def test_free_proof_backoff_applies_even_to_known_paid_pending_result(self):
        state={"pending_executions":{"xnet_transfers":{"execution_id":"paid"}},
            "source_errors":{"xnet_transfers":{"retry_kind":"transfer_instruction_proof",
                "next_retry_at_utc":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}}}
        self.assertTrue(self.refresh.source_in_backoff(state,"xnet_transfers"))


if __name__=="__main__":unittest.main()
