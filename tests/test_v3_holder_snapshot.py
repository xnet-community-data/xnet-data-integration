from copy import deepcopy
from datetime import datetime,timedelta,timezone
import base64,json,sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from v3_holder_snapshot import (MINT,PROGRAM,RPC,REQUESTS,SnapshotRejected,acquire,
    b58encode,public_key,make_baseline,validate_baseline,derive,MAX_ATTEMPTS)

NOW=datetime(2026,10,23,20,tzinfo=timezone.utc)
OWNER_A=b58encode(b"a"*32);OWNER_B=b58encode(b"b"*32)

def fixture(now=NOW,slot=100):
    rows=[]
    amounts=[(b"a"*32,100_000_000),(b"a"*32,50_000_000),(b"b"*32,200_000_000),(b"c"*32,0)]
    for index,(owner,amount) in enumerate(amounts):
        data=bytearray(165);data[:32]=public_key(MINT);data[32:64]=owner
        data[64:72]=amount.to_bytes(8,"little");data[108]=1 if index%2==0 else 2
        rows.append({"pubkey":b58encode(bytes([index+1])*32),"account":{
            "owner":PROGRAM,"executable":False,"data":[base64.b64encode(data).decode(),"base64"]}})
    mint=bytearray(82);mint[36:44]=(350_000_000).to_bytes(8,"little");mint[44]=8;mint[45]=1
    epoch=int((now-timedelta(seconds=20)).timestamp())
    return {"schema_version":1,"endpoint":RPC,"requests":deepcopy(REQUESTS),
        "responses":[{"jsonrpc":"2.0","id":"accounts","result":{"context":{"slot":slot},"value":rows}},
                     {"jsonrpc":"2.0","id":"mint","result":{"context":{"slot":slot},"value":{
                         "owner":PROGRAM,"executable":False,"data":[base64.b64encode(mint).decode(),"base64"]}}}],
        "block_time_request":{"jsonrpc":"2.0","id":"block_time","method":"getBlockTime","params":[slot]},
        "block_time_response":{"jsonrpc":"2.0","id":"block_time","result":epoch},
        "acquired_at_utc":now.isoformat().replace("+00:00","Z")}


class HolderSnapshotTests(unittest.TestCase):
    def test_complete_same_bank_accounts_aggregate_exact_raw_owner_balances(self):
        owners,receipt=derive(fixture())
        self.assertEqual(owners,{OWNER_A:"150000000",OWNER_B:"200000000"})
        self.assertEqual(receipt["mint_supply_raw"],"350000000")
        self.assertEqual(receipt["mint_conservation_residual_raw"],"0")
        self.assertEqual(receipt["positive_owner_count"],2)
        self.assertEqual(receipt["token_account_count"],4)
        self.assertTrue(receipt["same_bank_verified"])
    def test_snapshot_payload_and_receipt_are_both_replayed(self):
        baseline=make_baseline(fixture());self.assertEqual(validate_baseline(baseline)[0][OWNER_A],"150000000")
        for change in (lambda b:b["owners_raw"].update({OWNER_A:"999"}),
                       lambda b:b["receipt"].update(mint_supply_raw="999"),
                       lambda b:b.update(baseline_id="0"*64)):
            b=deepcopy(baseline);change(b)
            with self.assertRaises(SnapshotRejected):validate_baseline(b)
    def test_same_supply_at_different_slots_is_not_an_atomic_snapshot(self):
        c=fixture();c["responses"][1]["result"]["context"]["slot"]+=1
        with self.assertRaisesRegex(SnapshotRejected,"exact same finalized bank"):derive(c)
    def test_missing_positive_account_cannot_pass_independent_mint_conservation(self):
        c=fixture();c["responses"][0]["result"]["value"].pop(0)
        with self.assertRaisesRegex(SnapshotRejected,"conserve"):derive(c)
    def test_duplicate_accounts_cannot_compensate_for_a_missing_account(self):
        c=fixture();rows=c["responses"][0]["result"]["value"];rows[1]=deepcopy(rows[0])
        with self.assertRaisesRegex(SnapshotRejected,"Duplicate token account"):derive(c)
    def test_wrong_mint_program_decimals_initialization_or_truncation_are_refused(self):
        mutations=[
            lambda c:c["responses"][0]["result"]["value"][0]["account"].update(owner="other"),
            lambda c:c["responses"][1]["result"]["value"].update(executable=True),
            lambda c:c.update(endpoint="https://unapproved.example"),
            lambda c:c["requests"][0]["params"][1].update(commitment="confirmed"),
            lambda c:c["requests"][0]["params"][1].update(filters=[]),
            lambda c:c["block_time_request"].update(params=[101]),
            lambda c:c["responses"][0].update(error={"code":429}),
        ]
        for mutate in mutations:
            c=fixture();mutate(c)
            with self.subTest(mutate=mutate),self.assertRaises(SnapshotRejected):derive(c)
        for index,value,length in ((0,0,165),(108,0,165),(44,9,82),(45,0,82),(44,8,50)):
            c=fixture();account=(c["responses"][0]["result"]["value"][0]["account"] if length==165
                                 else c["responses"][1]["result"]["value"])
            data=bytearray(base64.b64decode(account["data"][0]));data[index]=value
            account["data"][0]=base64.b64encode(data[:length]).decode()
            with self.subTest(index=index,length=length),self.assertRaises(SnapshotRejected):derive(c)
    def test_missing_duplicate_or_nonobject_batch_responses_are_refused(self):
        for responses in (None,[],[{}],[fixture()["responses"][0]]*2):
            c=fixture();c["responses"]=responses
            with self.subTest(responses=responses),self.assertRaises(SnapshotRejected):derive(c)
    def test_future_or_stale_bank_times_cannot_be_freshened_by_generated_time(self):
        for delta in (-10,4000):
            c=fixture();c["block_time_response"]["result"]=int((NOW-timedelta(seconds=delta)).timestamp())
            with self.subTest(delta=delta),self.assertRaises(SnapshotRejected):derive(c)
    def test_bank_mismatch_retries_are_bounded_before_block_time_read(self):
        wrong=fixture();wrong["responses"][1]["result"]["context"]["slot"]=101
        read=Mock(side_effect=[wrong["responses"],wrong["responses"][1],
                              fixture()["responses"],fixture()["block_time_response"]])
        result=acquire(now=lambda:NOW,read=read)
        self.assertEqual(result["receipt"]["slot"],100);self.assertEqual(read.call_count,4)
        self.assertEqual(read.call_args_list[0].args[0],REQUESTS)
        self.assertEqual(read.call_args_list[1].args[0],REQUESTS[1])
        self.assertEqual(read.call_args_list[3].args[0]["method"],"getBlockTime")
    def test_network_failures_do_not_trigger_unbounded_retry_or_a_paid_fallback(self):
        read=Mock(side_effect=OSError("429"))
        with self.assertRaisesRegex(SnapshotRejected,"bounded attempts"):acquire(now=NOW,read=read)
        self.assertEqual(read.call_count,2)  # Unknown reads conservatively reserve the 64 MB byte budget.
        self.assertTrue(all(call.args[0]==REQUESTS for call in read.call_args_list))
    def test_normal_bank_advancement_can_align_after_the_old_two_attempt_limit(self):
        wrong=fixture();wrong["responses"][1]["result"]["context"]["slot"]=101
        prefix=[]
        for _ in range(3):prefix.extend([deepcopy(wrong["responses"]),deepcopy(wrong["responses"][1])])
        read=Mock(side_effect=prefix+
                  [fixture()["responses"],fixture()["block_time_response"]])
        result=acquire(now=NOW,read=read)
        self.assertEqual(result["capture"]["acquisition"]["attempts"],4)
        self.assertEqual(len(result["capture"]["acquisition"]["context_samples"]),4)
        self.assertTrue(result["receipt"]["same_bank_verified"])
    def test_finalized_responses_from_different_http_batches_can_match_the_same_bank(self):
        first=fixture();first["responses"][1]["result"]["context"]["slot"]=101
        second=fixture(slot=101);second["responses"][1]["result"]["context"]["slot"]=102
        read=Mock(side_effect=[first["responses"],first["responses"][1],second["responses"],
                              fixture(slot=101)["block_time_response"]])
        result=acquire(now=NOW,read=read)
        self.assertEqual(result["receipt"]["slot"],101)
        selection=result["capture"]["acquisition"]["response_selection"]
        self.assertEqual(selection["accounts_attempt"],2);self.assertEqual(selection["mint_attempt"],1)
        contexts=[row["result"]["context"]["slot"] for row in result["capture"]["responses"]]
        self.assertEqual(contexts,[101,101]);self.assertTrue(result["receipt"]["same_bank_verified"])
    def test_equal_supply_at_different_banks_never_becomes_an_accepted_snapshot(self):
        wrong=fixture();wrong["responses"][1]["result"]["context"]["slot"]=101
        def read(body):return deepcopy(wrong["responses"] if isinstance(body,list) else wrong["responses"][1])
        transport=Mock(side_effect=read)
        with self.assertRaises(SnapshotRejected):acquire(now=NOW,read=transport)
        self.assertEqual(transport.call_count,2*MAX_ATTEMPTS)
        self.assertFalse(any(isinstance(call.args[0],dict) and call.args[0].get("method")=="getBlockTime"
                             for call in transport.call_args_list))
    def test_contradictory_samples_at_an_immutable_context_are_not_joined(self):
        first=fixture();first["responses"][1]["result"]["context"]["slot"]=101
        other=deepcopy(first)
        account=other["responses"][0]["result"]["value"][0]["account"]
        data=bytearray(base64.b64decode(account["data"][0]));data[64:72]=(1).to_bytes(8,"little")
        account["data"][0]=base64.b64encode(data).decode()
        read=Mock(side_effect=[first["responses"],first["responses"][1],other["responses"]])
        with patch("v3_holder_snapshot.MAX_ATTEMPTS",2),self.assertRaisesRegex(SnapshotRejected,"Contradictory"):
            acquire(now=NOW,read=read)
    def test_unknown_transport_reserve_is_not_reported_as_known_download_bytes(self):
        read=Mock(side_effect=[OSError("response lost"),fixture()["responses"],fixture()["block_time_response"]])
        result=acquire(now=NOW,read=read)
        evidence=result["capture"]["acquisition"]
        self.assertGreater(evidence["uncertain_response_reserve_bytes"],evidence["known_response_bytes"])
        self.assertEqual(evidence["transport_admission_bytes"],
                         evidence["uncertain_response_reserve_bytes"]+evidence["known_response_bytes"])
    def test_alignment_retry_budget_stops_additional_large_reads(self):
        read=Mock(return_value=fixture()["responses"])
        with patch("v3_holder_snapshot.MAX_ACQUISITION_BYTES",1),self.assertRaises(SnapshotRejected):
            acquire(now=NOW,read=read)
        self.assertEqual(read.call_count,1)
    def test_elapsed_acquisition_deadline_stops_before_another_transport(self):
        read=Mock()
        with patch("v3_holder_snapshot.time.monotonic",side_effect=[0,100]),self.assertRaises(SnapshotRejected):
            acquire(now=NOW,read=read)
        read.assert_not_called()
    def test_no_positive_balance_is_dropped_because_it_is_one_raw_unit(self):
        c=fixture();rows=c["responses"][0]["result"]["value"]
        data=bytearray(base64.b64decode(rows[3]["account"]["data"][0]));data[64:72]=(1).to_bytes(8,"little")
        rows[3]["account"]["data"][0]=base64.b64encode(data).decode()
        mint=c["responses"][1]["result"]["value"];raw=bytearray(base64.b64decode(mint["data"][0]));raw[36:44]=(350_000_001).to_bytes(8,"little")
        mint["data"][0]=base64.b64encode(raw).decode()
        owners,receipt=derive(c);self.assertEqual(owners[b58encode(b"c"*32)],"1")
        self.assertEqual(receipt["positive_owner_count"],3)


if __name__=="__main__":unittest.main()
