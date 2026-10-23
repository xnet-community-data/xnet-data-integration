"""Free positive evidence of a pre-seed opening-balance conflict.

Never change a seed, add an inferred transfer or invent a balance adjustment.
Such a conflict stops speculative holder backfills, not ordinary collection.
"""
import csv
from datetime import datetime,timedelta,timezone
import hashlib

from v3_credit_budget import parse_instant
from v3_indexer_lag import recent_signatures
from v3_reduce_chain import parse_ts
from v3_rpc_transfer_proof import derive_transaction, ProofRejected, MINT
from v3_transfer_instruction_recovery import fetch_transaction, digest, locator, signature, InstructionProofDeferred

LANE="holder_seed_review"


def held(state):
    return state.get(LANE,{}).get("status")=="requires_review"


def balance(capture,side,account,owner):
    result=capture["result"]
    keys=[key["pubkey"] if isinstance(key,dict) else key
          for key in result["transaction"]["message"]["accountKeys"]]
    matches=[row for row in result["meta"][side] if keys[row["accountIndex"]]==account
             and row.get("mint")==MINT and row.get("owner")==owner]
    return matches[0]["uiTokenAmount"]["amount"] if len(matches)==1 else None


def probe(state,root,load,now,*,list_signatures=None,fetch=None):
    """At most one signature page + three finalized transaction reads/24 hours.

    An equal positive pre-seed/current balance is evidence for REVIEW, not
    permission to reconstruct an opening balance from a sparse RPC history.
    """
    if held(state):return
    error=state.get("source_errors",{}).get("holder_integrity",{}).get("error","")
    prefix="Negative holder balance after reduction: "
    if not error.startswith(prefix) or " = -" not in error:return
    owner=error[len(prefix):].split(" = -",1)[0]
    seed=root/"data/xnet_owner_balances_seed.csv"
    with seed.open(newline="") as handle:
        if any(row["owner"]==owner for row in csv.DictReader(handle)):return
    lane=state.setdefault(LANE,{"status":"checking"})
    last=lane.get("last_probe_at_utc")
    if last and now<parse_instant(last,"seed review probe")+timedelta(days=1):return
    lane["last_probe_at_utc"]=now.isoformat().replace("+00:00","Z")
    metadata=load(root/"data/xnet_owner_balances_seed_metadata.json",{})
    cutoff=parse_ts(metadata["latest_balance_event_time"])
    with (root/"data/canonical/xnet_transfers.csv").open(newline="") as handle:
        outflows=sorted((row for row in csv.DictReader(handle)
            if row["from_owner"]==owner and parse_ts(row["block_time"])>cutoff),key=lambda row:parse_ts(row["block_time"]))
    if not outflows:return
    row=outflows[0];tx=row["tx_id"];account=row["from_token_account"]
    listing=list_signatures or recent_signatures;fetch=fetch or fetch_transaction
    try:
        current=fetch(tx);derived,current_receipt=derive_transaction(current)
        if not any(locator(row)==locator(proven) and signature(row)==signature(proven) for proven in derived):return
        opening=balance(current,"preTokenBalances",account,owner)
        if opening is None or int(opening)<=0:return
        activity=listing(address=account)
        position=next((i for i,item in enumerate(activity) if item.get("signature")==tx),None)
        if position is None:return
        reads=1
        for item in activity[position+1:]:
            if not isinstance(item,dict) or "err" not in item:raise ValueError("Incomplete account history metadata")
            if item["err"] is not None:continue
            epoch=item.get("blockTime")
            if type(epoch) is not int or not 0<=epoch<=now.timestamp():raise ValueError("Invalid account history timestamp")
            at=datetime.fromtimestamp(epoch,timezone.utc)
            if at>cutoff:return  # Could be a real missing post-seed inflow: normal recovery continues.
            if reads>=3:break
            reads+=1
            historical=fetch(item["signature"])
            try:_,historical_receipt=derive_transaction(historical)
            except ProofRejected:continue  # e.g. a SOL/memo spam transaction is not token history.
            if (historical_receipt["signature"]!=item["signature"]
                    or parse_ts(derived[0]["block_time"])<=cutoff
                    or historical["result"]["blockTime"]!=epoch):return
            if balance(historical,"postTokenBalances",account,owner)!=opening:return
            lane.update(status="requires_review",owner=owner,token_account=account,
                error="Pre-seed finalized token balance conflicts with an absent owner opening in the immutable seed; no automatic seed adjustment",
                seed_sha256=hashlib.sha256(seed.read_bytes()).hexdigest(),seed_cutoff_utc=cutoff.isoformat(),
                observed_raw_balance=opening,current_capture=current,current_receipt=current_receipt,
                historical_capture=historical,historical_receipt=historical_receipt,
                current_capture_sha256=digest(current),historical_capture_sha256=digest(historical),
                signature_history=activity,seed_or_canonical_mutated=False,
                absence_of_intermediate_signatures_is_not_proof_of_complete_history=True)
            return
    except (OSError,ValueError,TypeError,KeyError,AttributeError,IndexError,InstructionProofDeferred,ProofRejected) as failure:
        lane["last_probe_error"]=str(failure)
