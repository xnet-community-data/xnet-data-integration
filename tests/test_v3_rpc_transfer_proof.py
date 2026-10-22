from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_rpc_transfer_proof import derive_transaction, MINT, TOKEN_PROGRAM, TOKEN_2022_PROGRAM, ProofRejected


def capture():
    return {"signature": "tx", "slot": 100, "request": {"params": ["tx", {
        "encoding": "jsonParsed", "commitment": "finalized"}]}, "result": {
        "slot": 100, "blockTime": 1791975600,
        "transaction": {"signatures": ["tx"], "message": {"accountKeys": ["a", "b"],
            "instructions": [{"programId": "router"}]}},
        "meta": {"err": None, "innerInstructions": [{"index": 0, "instructions": [{
            "programId": TOKEN_PROGRAM, "parsed": {"type": "transferChecked", "info": {
                "source": "a", "destination": "b", "mint": MINT,
                "tokenAmount": {"amount": "123", "decimals": 8}}}}]}],
            "preTokenBalances": [balance(0, "owner-a", "200"), balance(1, "owner-b", "0")],
            "postTokenBalances": [balance(0, "owner-a", "77"), balance(1, "owner-b", "123")]}}}


def balance(index, owner, raw):
    return {"accountIndex": index, "mint": MINT, "owner": owner,
            "uiTokenAmount": {"amount": raw, "decimals": 8}}


class RpcProofTests(unittest.TestCase):
    def test_inner_instruction_not_net_balance_inference(self):
        rows, proof = derive_transaction(capture())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_raw"], "123")
        self.assertEqual(rows[0]["from_owner"], "owner-a")
        self.assertEqual(proof["instructions"][0]["inner"], 0)
        self.assertTrue(proof["balance_flow_verified"])

    def test_reject_partial_failed_mismatched_or_wrong_decimals(self):
        changes = [
            lambda c: c["result"]["meta"].pop("innerInstructions"),
            lambda c: c["result"]["meta"].update(err={"InstructionError": 1}),
            lambda c: c["result"]["meta"].pop("err"),
            lambda c: c["result"]["transaction"].update(signatures=["other"]),
            lambda c: c["result"]["meta"]["postTokenBalances"][0]["uiTokenAmount"].update(amount="78"),
            lambda c: c["request"]["params"][1].update(commitment="confirmed"),
            lambda c: c["result"]["meta"]["postTokenBalances"][0].update(owner="new"),
            lambda c: c["result"]["meta"]["preTokenBalances"][0]["uiTokenAmount"].update(decimals=9),
        ]
        for change in changes:
            with self.subTest(change=change):
                c = capture()
                change(c)
                with self.assertRaises(ProofRejected):
                    derive_transaction(c)

    def test_two_identical_real_instructions_preserve_multiplicity(self):
        c = capture()
        group = c["result"]["meta"]["innerInstructions"][0]["instructions"]
        group.append(deepcopy(group[0]))
        c["result"]["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"] = "300"
        c["result"]["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "54"
        c["result"]["meta"]["postTokenBalances"][1]["uiTokenAmount"]["amount"] = "246"
        rows, proof = derive_transaction(c)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_multiplicity"], "2")
        self.assertEqual(len(proof["instructions"]), 2)

    def test_token_2022_checked_instruction_is_not_silently_skipped(self):
        c = capture()
        c["result"]["meta"]["innerInstructions"][0]["instructions"][0]["programId"] = TOKEN_2022_PROGRAM
        rows, proof = derive_transaction(c)
        self.assertEqual(rows[0]["amount_raw"], "123")
        self.assertTrue(proof["balance_flow_verified"])

    def test_real_captures_require_complete_instructions(self):
        path = Path(__file__).parent / "fixtures/bbb_rpc_repair_20261014_16.json"
        fixture = json.loads(path.read_text())
        rows = fixture["repair_rows"]
        # Regression of the initially partial capture: no fallback to its
        # author-provided `derived` balance-delta claims is ever allowed.
        partial = deepcopy(rows[0])
        partial["result"]["meta"].pop("innerInstructions", None)
        with self.assertRaises(ProofRejected):
            derive_transaction(partial)
        for row in rows:
            with self.subTest(signature=row["signature"]):
                if "innerInstructions" not in row["result"]["meta"]:
                    with self.assertRaises(ProofRejected):
                        derive_transaction(row)
                else:
                    derived, proof = derive_transaction(row)
                    self.assertTrue(derived)
                    self.assertTrue(proof["balance_flow_verified"])

    def test_new_ata_initialization_is_supported_only_with_exact_post_owner(self):
        for kind in ("initializeAccount","initializeAccount2","initializeAccount3"):
            c=capture();c["result"]["meta"]["preTokenBalances"].pop()
            setup={"programId":TOKEN_PROGRAM,"parsed":{"type":kind,
                "info":{"account":"b","mint":MINT,"owner":"owner-b"}}}
            c["result"]["meta"]["innerInstructions"][0]["instructions"].insert(0,setup)
            rows,_=derive_transaction(c)
            self.assertEqual(rows[0]["to_owner"],"owner-b")
            setup["parsed"]["info"]["owner"]="other"
            with self.assertRaises(ProofRejected):derive_transaction(c)

    def test_authority_changes_cannot_hide_behind_account_only_instruction(self):
        c=capture()
        c["result"]["meta"]["innerInstructions"][0]["instructions"].append({
            "programId":TOKEN_PROGRAM,"parsed":{"type":"setAuthority","info":{"account":"b","newAuthority":"other"}}})
        with self.assertRaisesRegex(ProofRejected,"Unsupported"):
            derive_transaction(c)

    def test_close_account_is_not_a_fabricated_token_flow(self):
        c=capture()
        close={"programId":TOKEN_PROGRAM,"parsed":{"type":"closeAccount","info":{"account":"a","destination":"owner-a"}}}
        c["result"]["meta"]["innerInstructions"][0]["instructions"].append(close)
        with self.assertRaisesRegex(ProofRejected,"nonzero"):
            derive_transaction(c)
        c["result"]["meta"]["preTokenBalances"][0]["uiTokenAmount"]["amount"]="123"
        c["result"]["meta"]["postTokenBalances"].pop(0)
        rows,receipt=derive_transaction(c)
        self.assertEqual(len(rows),1)
        self.assertEqual(len(receipt["instructions"]),1)


if __name__ == "__main__":
    unittest.main()
