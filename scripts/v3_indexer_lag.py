"""Positive finalized-chain witnesses for data escaping the hot overlap.

This is a bounded, free probe, NOT whole-mint history/coverage. It never inserts
RPC rows into canonical data. Proven omissions trigger existing quoted UTC-day
verification until Dune supplies the missing facts. No mint mention alone counts
as a transfer, and no silence is reported as proof of completeness.
"""
import csv
from datetime import datetime,timedelta,timezone
import json
import urllib.request

from v3_credit_budget import parse_instant
from v3_live_state_store import StateError
from v3_reduce_chain import parse_ts
from v3_rpc_transfer_proof import MINT, derive_transaction, ProofRejected
from v3_transfer_instruction_recovery import (RPC, digest, fetch_transaction,
    locator, signature, InstructionProofDeferred, MAX_RESPONSE_BYTES)

PROBE_HOURS = 6
MAX_TRANSACTION_READS = 2


def recent_signatures(before=None):
    options={"limit":32,"commitment":"finalized"}
    if before:options["before"]=before
    body={"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress",
          "params":[MINT,options]}
    req=urllib.request.Request(RPC,data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json","User-Agent":"xnet-indexer-lag-probe/1.0"})
    with urllib.request.urlopen(req,timeout=15) as response:
        raw=response.read(MAX_RESPONSE_BYTES+1)
    if len(raw)>MAX_RESPONSE_BYTES:raise ValueError("Oversized activity probe")
    result=json.loads(raw)
    if result.get("id")!=1 or not isinstance(result.get("result"),list):
        raise ValueError("No finalized activity metadata")
    return result["result"]


def reconcile_witnesses(state, canonical):
    watch=state.get("indexer_lag_watch",{})
    proofs=state.get("transfer_instruction_proofs",{})
    by_id={row["event_id"]:row for row in canonical}
    unresolved=set()
    for tx,entry in watch.get("witnesses",{}).items():
        proof=proofs.get(tx,{})
        if digest(proof.get("capture"))!=proof.get("capture_sha256"):
            raise StateError("Finalized indexing witness lost its immutable capture")
        rows,receipt=derive_transaction(proof["capture"])
        if receipt!=proof.get("receipt") or receipt["signature"]!=tx:
            raise StateError("Finalized indexing witness identity changed")
        missing=[row["event_id"] for row in rows if row["event_id"] not in by_id
            or locator(row)!=locator(by_id[row["event_id"]])
            or signature(row)!=signature(by_id[row["event_id"]])]
        entry["missing_event_ids"]=missing
        if missing:unresolved.update(row["block_date"] for row in rows if row["event_id"] in missing)
    if unresolved:
        recon=state.setdefault("reconciliation",{})
        recon["pending_mismatch_days"]=sorted(set(recon.get("pending_mismatch_days",[]))|unresolved)
        state.setdefault("source_errors",{})["indexer_lag"]={"automatic_recheck":True,
            "error":"Finalized transfer witnesses are absent or conflicting in the indexed ledger",
            "unresolved_days":sorted(unresolved),"recheck_cadence_minutes":PROBE_HOURS*60}
    else:
        state.get("source_errors",{}).pop("indexer_lag",None)
    return unresolved


def probe(state, root, load, now, *, list_signatures=None, fetch=None):
    """At most two 32-signature pages and two transaction reads every six hours.

    Remember the previous head and inspect just before it next time, so high
    activity cannot keep every bounded sample perpetually younger than overlap.
    """
    watch=state.setdefault("indexer_lag_watch",{"schema_version":1,"witnesses":{}})
    ledger=root/"data/canonical/xnet_transfers.csv"
    with ledger.open(newline="") as handle:canonical=list(csv.DictReader(handle))
    reconcile_witnesses(state,canonical)
    last=watch.get("last_probe_at_utc")
    delay=1 if watch.get("last_probe_error") else PROBE_HOURS
    if last and now<parse_instant(last,"activity probe")+timedelta(hours=delay):return
    watch["last_probe_at_utc"]=now.isoformat().replace("+00:00","Z")
    previous=state.get("queries",{}).get("xnet_transfers",{})
    covered=previous.get("continuous_coverage_end_utc") or previous.get("completed_at_utc")
    if not covered:return
    covered=parse_instant(covered,"collector coverage")
    cutoff=load(root/"data/xnet_owner_balances_seed_metadata.json",{}).get("latest_balance_event_time")
    if not cutoff:return
    cutoff=parse_ts(cutoff)
    known={row["tx_id"] for row in canonical}
    requests=0
    try:
        listing=list_signatures or recent_signatures
        head=listing()
        previous_head=watch.get("previous_head_signature")
        items=listing(before=previous_head) if previous_head else head
        if head and isinstance(head[0],dict) and isinstance(head[0].get("signature"),str):
            watch["previous_head_signature"]=head[0]["signature"]
        watch.pop("last_probe_error",None)
        for item in items:
            if requests>=MAX_TRANSACTION_READS:break
            if not isinstance(item,dict) or "err" not in item:continue
            tx=item.get("signature");epoch=item.get("blockTime")
            if (item.get("err") is not None or not isinstance(tx,str) or not tx
                    or type(epoch) is not int):continue
            at=datetime.fromtimestamp(epoch,timezone.utc)
            # Only evidence that already escaped the normal hot window, was
            # before the accepted poll, and lies inside existing repair scope.
            if (at.date()<=cutoff.date() or not cutoff<at<min(covered,now-timedelta(hours=6))
                    or (now.date()-at.date()).days>60 or tx in known or tx in watch["witnesses"]):continue
            requests+=1
            capture=(fetch or fetch_transaction)(tx)
            try:rows,receipt=derive_transaction(capture)
            except ProofRejected as error:
                watch["last_probe_error"]=str(error);continue
            if receipt["signature"]!=tx or any(parse_ts(row["block_time"])!=at for row in rows):
                watch["last_probe_error"]="Activity/transaction identity mismatch";continue
            state.setdefault("transfer_instruction_proofs",{})[tx]={
                "capture":capture,"capture_sha256":digest(capture),"receipt":receipt}
            watch["witnesses"][tx]={"first_observed_at_utc":now.isoformat().replace("+00:00","Z"),
                                       "missing_event_ids":[row["event_id"] for row in rows]}
        reconcile_witnesses(state,canonical)
    except (OSError,ValueError,InstructionProofDeferred) as error:
        watch["last_probe_error"]=str(error)  # No mint-mention inference or paid fallback.
