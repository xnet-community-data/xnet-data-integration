"""Bounded, spend-free instruction proof for otherwise ambiguous transfer groups.

RPC instructions, not net balance inference, must prove every grouped leg. The
original provider capture is retained in the journal for offline replay. Nothing
here mutates canonical rows, settles charges, or authorizes a Dune submission.
"""
from collections import Counter, defaultdict
import hashlib
import json
import urllib.request

from v3_reduce_chain import parse_ts
from v3_rpc_transfer_proof import derive_transaction, ProofRejected

RPC = "https://api.mainnet-beta.solana.com"
MAX_RESPONSE_BYTES = 2_000_000


class InstructionProofDeferred(RuntimeError):
    """Keep the paid result and retry free proof after bounded backoff."""


def locator(row):
    return tuple(str(row.get(key) or "") for key in
                 ("tx_id", "block_slot", "action", "from_token_account", "to_token_account"))


def signature(row):
    return (str(row["amount_raw"]), str(row.get("from_owner") or ""),
            str(row.get("to_owner") or ""), str(row.get("event_multiplicity") or 1),
            parse_ts(row["block_time"]).isoformat())


def groups(rows):
    result = defaultdict(list)
    for row in rows:
        result[locator(row)].append(row)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def fetch_transaction(tx_id):
    request = {"jsonrpc": "2.0", "id": 1, "method": "getTransaction", "params": [tx_id,
        {"encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 0}]}
    req = urllib.request.Request(RPC, data=json.dumps(request).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "xnet-instruction-recovery/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Transaction response exceeds proof size bound")
        body = json.loads(raw)
        if body.get("id") != 1 or body.get("error") or not isinstance(body.get("result"), dict):
            raise ValueError("Finalized transaction is not yet available")
        result = body["result"]
        return {"signature": tx_id, "slot": result.get("slot"), "request": request, "result": result}
    except (OSError, ValueError) as error:
        raise InstructionProofDeferred(f"Finalized instruction proof temporarily unavailable: {error}") from error


def needs_proof(old, incoming):
    before, after = {signature(row) for row in old}, {signature(row) for row in incoming}
    return not after <= before and (bool(before) or len(incoming) > 1)


def prepare_proofs(state, canonical, provider, *, fetch=None, max_requests=4):
    """Cache only verified evidence. Prior successful proofs survive a later deferral."""
    old, new = groups(canonical), groups(provider)
    lane = state.get("transfer_instruction_proofs", {})
    requests = 0
    for key, rows in new.items():
        if not needs_proof(old.get(key, []), rows):
            continue
        if not {signature(row) for row in old.get(key, [])} <= {signature(row) for row in rows}:
            raise ProofRejected("Hot overlap changes an existing financial fact; replacement is not an addition")
        tx_id = key[0]
        if tx_id not in lane:
            if requests >= max_requests:
                raise InstructionProofDeferred("Per-run free instruction-proof request bound reached")
            requests += 1
            capture = (fetch or fetch_transaction)(tx_id)
            try:
                _, receipt = derive_transaction(capture)
            except ProofRejected as error:
                raise InstructionProofDeferred(f"Complete decoded RPC evidence not yet usable: {error}") from error
            if receipt["signature"] != tx_id:
                raise ProofRejected("Instruction proof belongs to another transaction")
            state["transfer_instruction_proofs"] = lane
            lane[tx_id] = {"capture": capture, "capture_sha256": digest(capture), "receipt": receipt}
        # Recheck even stored evidence; a cached label is not proof by itself.
        proven_group(old.get(key, []), rows, lane)
    return lane


def proven_group(old, incoming, proofs):
    """Accept only an exact instruction-backed group preserving every old fact."""
    if not needs_proof(old, incoming):
        return
    key = locator(incoming[0])
    entry = proofs.get(key[0]) if isinstance(proofs, dict) else None
    if not isinstance(entry, dict) or digest(entry.get("capture")) != entry.get("capture_sha256"):
        raise ProofRejected("Ambiguous transfer group requires finalized instruction proof")
    derived, receipt = derive_transaction(entry["capture"])
    if receipt["signature"] != key[0] or receipt != entry.get("receipt"):
        raise ProofRejected("Stored instruction proof identity changed")
    proven = [row for row in derived if locator(row) == key]
    expected = Counter(signature(row) for row in proven)
    if Counter(signature(row) for row in incoming) != expected:
        raise ProofRejected("Provider transfer group differs from finalized instructions")
    if not set(signature(row) for row in old) <= set(expected):
        raise ProofRejected("Finalized proof would change or remove an existing canonical fact")
    # The canonical ID omits owner/time/multiplicity. Never silently overwrite it.
    old_amounts = {str(row["amount_raw"]): signature(row) for row in old}
    if any(str(row["amount_raw"]) in old_amounts
           and signature(row) != old_amounts[str(row["amount_raw"])] for row in incoming):
        raise ProofRejected("Instruction group collides with an existing financial event")
