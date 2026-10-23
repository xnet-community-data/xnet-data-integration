"""Complete, same-finalized-bank holder state; never infer opening adjustments.

All mint-filtered Token-2022 accounts and the mint itself MUST have the exact
same RPC context slot. Their unsigned raw balances MUST sum to mint supply.
This proves a complete positive-balance inventory at that slot, independently
of Dune indexing or the preserved historical seed. No transfer is manufactured.
"""
import base64
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
import urllib.request

RPC = "https://api.mainnet-beta.solana.com"
MINT = "xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L"
PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PATH = "data/current/v3_holder_baseline.json"
MAX_BYTES = 32_000_000
MAX_ACCOUNTS = 50_000
MAX_ATTEMPTS = 8
MAX_ACQUISITION_BYTES = 64_000_000
ACQUISITION_VERSION = 2
MAX_AGE_SECONDS = 6 * 3600
AUTHORIZATION_REF = "user-end-to-end-holder-recovery-20261023"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
REQUESTS = [
    {"jsonrpc":"2.0", "id":"accounts", "method":"getProgramAccounts", "params":[PROGRAM, {
        "commitment":"finalized", "encoding":"base64", "withContext":True,
        "filters":[{"memcmp":{"offset":0,"bytes":MINT}}], "dataSlice":{"offset":0,"length":165}}]},
    {"jsonrpc":"2.0", "id":"mint", "method":"getAccountInfo", "params":[MINT, {
        "commitment":"finalized", "encoding":"base64"}]},
]


class SnapshotRejected(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise SnapshotRejected(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def b58encode(raw):
    number, text = int.from_bytes(raw, "big"), ""
    while number:
        number, remainder = divmod(number, 58)
        text = ALPHABET[remainder] + text
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + text


def public_key(text):
    require(isinstance(text, str) and 32 <= len(text) <= 44, "Invalid account public key")
    number = 0
    for char in text:
        require(char in ALPHABET, "Invalid base58 account public key")
        number = number * 58 + ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length()+7)//8, "big")
    raw = b"\0" * (len(text)-len(text.lstrip("1"))) + raw
    require(len(raw) == 32 and b58encode(raw) == text, "Noncanonical account public key")
    return raw


def account_data(account, minimum):
    require(isinstance(account, dict) and account.get("owner") == PROGRAM
            and account.get("executable") is False, "Wrong token program/account type")
    data = account.get("data")
    require(isinstance(data, list) and len(data) == 2 and data[1] == "base64", "Raw account data required")
    raw = base64.b64decode(data[0], validate=True)
    require(len(raw) >= minimum, "Truncated token account data")
    return raw


def derive(capture):
    try:
        return _derive(capture)
    except SnapshotRejected:
        raise
    except (KeyError, AttributeError, TypeError, ValueError, IndexError, OverflowError) as error:
        raise SnapshotRejected(f"Malformed finalized holder snapshot: {error}") from error


def _derive(capture):
    require(capture.get("schema_version") == 1 and capture.get("endpoint") == RPC,
            "Unknown holder snapshot provenance")
    require(capture.get("requests") == REQUESTS, "Full finalized account enumeration contract changed")
    responses = capture.get("responses")
    require(isinstance(responses, list) and len(responses) == 2, "Incomplete snapshot batch")
    by_id = {}
    for response in responses:
        require(isinstance(response, dict) and response.get("jsonrpc") == "2.0"
                and response.get("id") in {"accounts", "mint"} and not response.get("error"),
                "Failed or unexpected snapshot response")
        require(response["id"] not in by_id, "Duplicate snapshot response identity")
        by_id[response["id"]] = response["result"]
    accounts, mint = by_id["accounts"], by_id["mint"]
    slot = accounts["context"]["slot"]
    require(type(slot) is int and 0 < slot < 2**64 and type(mint["context"]["slot"]) is int
            and mint["context"]["slot"] == slot,
            "Account inventory and mint are not from the exact same finalized bank")
    mint_data = account_data(mint["value"], 82)
    require(mint_data[44] == 8 and mint_data[45] == 1, "Unexpected mint decimals/initialization")
    supply = int.from_bytes(mint_data[36:44], "little")
    rows = accounts["value"]
    require(isinstance(rows, list) and len(rows) <= MAX_ACCOUNTS, "Incomplete/oversized token inventory")
    owners, seen = defaultdict(int), set()
    expected_mint = public_key(MINT)
    for row in rows:
        address = row["pubkey"]
        public_key(address)
        require(address not in seen, "Duplicate token account in inventory")
        seen.add(address)
        data = account_data(row["account"], 165)
        require(len(data) == 165 and data[:32] == expected_mint and data[108] in {1, 2},
                "Wrong mint or uninitialized/truncated token account")
        owners[b58encode(data[32:64])] += int.from_bytes(data[64:72], "little")
    require(sum(owners.values()) == supply, "Token inventory does not conserve independent mint supply")
    request = {"jsonrpc":"2.0", "id":"block_time", "method":"getBlockTime", "params":[slot]}
    require(capture.get("block_time_request") == request, "Missing exact snapshot-slot time request")
    observed = capture["block_time_response"]
    require(observed.get("jsonrpc") == "2.0" and observed.get("id") == "block_time"
            and not observed.get("error") and type(observed.get("result")) is int
            and observed["result"] > 0, "Missing exact snapshot-slot block time")
    block_time = datetime.fromtimestamp(observed["result"], timezone.utc)
    acquired = datetime.fromisoformat(capture["acquired_at_utc"].replace("Z", "+00:00"))
    require(acquired.tzinfo is not None and 0 <= (acquired-block_time).total_seconds() <= 3600,
            "Stale/future snapshot acquisition")
    positive = {owner:str(amount) for owner,amount in sorted(owners.items()) if amount > 0}
    receipt = {"kind":"same_slot_finalized_full_accounts", "slot":slot,
        "block_time_utc":block_time.isoformat().replace("+00:00", "Z"),
        "mint":MINT, "token_program":PROGRAM, "decimals":8,
        "mint_supply_raw":str(supply), "account_balance_sum_raw":str(sum(owners.values())),
        "token_account_count":len(seen), "positive_owner_count":len(positive),
        "owners_sha256":digest(positive), "capture_sha256":digest(capture),
        "same_bank_verified":True, "mint_conservation_residual_raw":"0"}
    return positive, receipt


def make_baseline(capture):
    owners, receipt = derive(capture)
    return {"schema_version":1, "capture":capture, "owners_raw":owners,
            "receipt":receipt, "baseline_id":digest(receipt)}


def validate_baseline(value):
    require(isinstance(value,dict) and value.get("schema_version") == 1, "Invalid holder baseline schema")
    owners, receipt = derive(value.get("capture", {}))
    require(value.get("owners_raw") == owners and value.get("receipt") == receipt
            and value.get("baseline_id") == digest(receipt), "Holder baseline proof/digest changed")
    return owners, receipt


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def rpc_read(body):
    request = urllib.request.Request(RPC, data=encoded(body), headers={
        "Content-Type":"application/json", "User-Agent":"xnet-finalized-holder-snapshot/1.0"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        raw = response.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "Holder snapshot transport exceeds byte bound")
    from v3_live_state_store import decode_json, StateError
    try:
        return decode_json(raw, "holder RPC response")
    except StateError as error:
        raise SnapshotRejected(str(error)) from error


def acquire(now=None, read=None):
    read = read or rpc_read
    failure = None
    deadline = time.monotonic()+90
    batch_bytes, contexts = 0, []
    for attempt in range(MAX_ATTEMPTS):
        if time.monotonic() >= deadline or batch_bytes >= MAX_ACQUISITION_BYTES:
            failure = SnapshotRejected("Free holder acquisition time/byte budget exhausted")
            break
        try:
            responses = read(deepcopy(REQUESTS))
            batch_bytes += len(encoded(responses))
            require(batch_bytes <= MAX_ACQUISITION_BYTES, "Free holder acquisition byte budget exhausted")
            require(isinstance(responses,list) and len(responses) == 2, "Incomplete snapshot batch")
            result = next(row["result"] for row in responses if row.get("id") == "accounts")
            slot = result["context"]["slot"]
            require(type(slot) is int and slot > 0, "Invalid snapshot slot")
            # Reject mismatched contexts BEFORE paying even a free block-time read.
            mint = next(row["result"] for row in responses if row.get("id") == "mint")
            mint_slot = mint["context"]["slot"]
            contexts.append({"accounts":slot,"mint":mint_slot})
            require(mint_slot == slot,
                    f"Snapshot bank advanced within batch (accounts {slot}, mint {mint_slot}); bounded retry")
            request = {"jsonrpc":"2.0", "id":"block_time", "method":"getBlockTime", "params":[slot]}
            response = read(request)
            captured = now() if callable(now) else now or datetime.now(timezone.utc)
            capture = {"schema_version":1, "endpoint":RPC, "requests":deepcopy(REQUESTS),
                "responses":responses, "block_time_request":request, "block_time_response":response,
                "acquired_at_utc":captured.isoformat().replace("+00:00", "Z"),
                "acquisition":{"version":ACQUISITION_VERSION,"attempts":attempt+1,
                    "batch_bytes_read":batch_bytes,"context_samples":contexts}}
            return make_baseline(capture)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, StopIteration) as error:
            failure = error
    raise SnapshotRejected(f"Finalized holder snapshot temporarily unavailable after bounded attempts: {failure}; contexts={contexts}")
