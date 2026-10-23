"""Derive complete XNET instruction rows from a finalized jsonParsed transaction.

Offline only. Net balance changes validate decoded legs; they never invent them.
This does not establish address-history or whole-mint coverage.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from v3_reduce_chain import TRANSFER_FIELDS, normalise_transfer

MINT = "xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class ProofRejected(ValueError):
    pass


def require(test, message):
    if not test:
        raise ProofRejected(message)


def integer(value):
    require(type(value) is int or isinstance(value, str), "Exact integer required")
    text = str(value)
    require(text.isascii() and text.isdigit() and str(int(text)) == text, "Invalid raw integer")
    return int(text)


def derive_transaction(capture):
    """Return legacy grouped rows and an immutable, ordinal-aware proof receipt."""
    try:
        return _derive_transaction(capture)
    except ProofRejected:
        raise
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        raise ProofRejected(f"Malformed transaction evidence: {error}") from error


def _derive_transaction(capture):
    request, result = capture.get("request", {}), capture.get("result")
    require(isinstance(result, dict), "Missing transaction response")
    # Captures may store the exact params or the method/params request envelope.
    params = request.get("params") if isinstance(request, dict) else request
    require(isinstance(params, list) and len(params) == 2, "Missing authenticated request provenance")
    signature, options = params
    require(isinstance(signature, str) and signature.strip() and signature == signature.strip(),
            "Missing exact transaction signature")
    require(options.get("encoding") == "jsonParsed" and options.get("commitment") == "finalized",
            "Finalized complete jsonParsed capture required")
    require(capture.get("signature") == signature, "Request signature mismatch")
    transaction, meta = result.get("transaction", {}), result.get("meta", {})
    require(transaction.get("signatures", [None])[0] == signature and "err" in meta and meta["err"] is None,
            "Failed or mismatched transaction")
    require("innerInstructions" in meta and isinstance(meta["innerInstructions"], list),
            "Missing inner instruction capture; balance deltas alone are not event proof")
    slot = integer(result.get("slot"))
    require(slot == capture.get("slot"), "Capture slot mismatch")
    epoch = integer(result.get("blockTime"))
    observed = datetime.fromtimestamp(epoch, timezone.utc)
    message = transaction.get("message", {})
    keys = [item.get("pubkey") if isinstance(item, dict) else item for item in message.get("accountKeys", [])]
    require(keys and len(keys) == len(set(keys)), "Invalid complete account keys")
    balances, raw = {}, {"pre": {}, "post": {}}
    for phase, field in (("pre", "preTokenBalances"), ("post", "postTokenBalances")):
        require(isinstance(meta.get(field), list), "Missing token balances")
        for entry in meta[field]:
            index = integer(entry.get("accountIndex"))
            require(index < len(keys), "Token account index outside keys")
            account = keys[index]
            token = entry.get("uiTokenAmount", {})
            identity = (entry.get("mint"), entry.get("owner"), token.get("decimals"))
            require(account not in raw[phase], "Duplicate token balance")
            require(account not in balances or balances[account] == identity,
                    "Owner/mint/decimals changed within transaction")
            balances[account] = identity
            raw[phase][account] = integer(token.get("amount"))
    xnet = {account for account, identity in balances.items() if identity[0] == MINT}
    require(xnet, "No proven XNET token accounts")
    require(all(balances[a][2] == 8 and isinstance(balances[a][1], str) and balances[a][1] for a in xnet),
            "Invalid XNET owner or decimals")
    instructions = []
    outer = message.get("instructions")
    require(isinstance(outer, list), "Missing outer instructions")
    groups = {}
    for group in meta["innerInstructions"]:
        index = integer(group.get("index"))
        require(index < len(outer) and index not in groups and isinstance(group.get("instructions"), list),
                "Invalid inner instruction group")
        groups[index] = group["instructions"]
    for index, instruction in enumerate(outer):
        instructions.append((index, None, instruction))
        instructions.extend((index, inner, item) for inner, item in enumerate(groups.get(index, [])))
    predicted, rows, identities = defaultdict(int), [], []
    for outer_index, inner_index, instruction in instructions:
        if instruction.get("programId") not in {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}:
            continue
        parsed = instruction.get("parsed")
        require(isinstance(parsed, dict), "Undecoded SPL instruction; transaction not complete for repair")
        kind, info = parsed.get("type"), parsed.get("info", {})
        source = info.get("source") or (info.get("account") if kind in {"burn", "burnChecked"} else None)
        destination = info.get("destination") or (info.get("account") if kind in {"mintTo", "mintToChecked"} else None)
        relevant = (source in xnet or destination in xnet or info.get("mint") == MINT
                    or info.get("account") in xnet)
        if not relevant:
            continue
        if kind == "getAccountDataSize":
            require(info.get("mint") == MINT, "Account-size query mint mismatch")
            # SPL's exact read-only size query cannot move tokens or change
            # authority. Every financial leg and account delta is still proved.
            continue
        if kind in {"initializeAccount", "initializeAccount2", "initializeAccount3", "initializeImmutableOwner"}:
            account = info.get("account")
            require(account in xnet and account not in raw["pre"] and account in raw["post"],
                    "Initialization requires a proven newly created XNET account")
            if kind != "initializeImmutableOwner":
                require(info.get("owner") == balances[account][1] and info.get("mint") == MINT,
                        "New account initialization owner/mint mismatch")
            continue  # Account setup is not an inferred transfer leg.
        if kind == "closeAccount":
            account = info.get("account")
            require(account in xnet and raw["post"].get(account, 0) == 0,
                    "Cannot close a nonzero XNET token account")
            continue  # Decoded transfer/burn flow must still explain its balance.
        if kind in {"transfer", "transferChecked"}:
            action = "transfer"
            require(source in xnet and destination in xnet, "Unresolved XNET transfer counterparty")
        elif kind in {"burn", "burnChecked"}:
            action = "burn"
            require(source in xnet, "Unresolved burn account")
        elif kind in {"mintTo", "mintToChecked"}:
            action = "mint"
            require(destination in xnet, "Unresolved mint account")
        else:
            # Account lifecycle/authority changes could invalidate owner attribution.
            raise ProofRejected(f"Unsupported relevant SPL instruction: {kind}")
        require(info.get("mint", MINT) == MINT, "Instruction mint mismatch")
        token = info.get("tokenAmount")
        if token is not None:
            require(token.get("decimals") == 8, "Instruction decimals mismatch")
        amount = integer(token.get("amount") if token is not None else info.get("amount"))
        if source:
            predicted[source] -= amount
        if destination:
            predicted[destination] += amount
        row = {"block_date": observed.date().isoformat(), "block_time": observed.strftime("%Y-%m-%d %H:%M:%S.000 UTC"),
               "block_slot": str(slot), "tx_id": signature, "action": action,
               "from_owner": balances[source][1] if source else "", "to_owner": balances[destination][1] if destination else "",
               "from_token_account": source or "", "to_token_account": destination or "",
               "amount_raw": str(amount), "amount_xnet": format(Decimal(amount) / Decimal(10**8), "f"),
               "event_multiplicity": "1"}
        rows.append(row)
        identities.append({"outer": outer_index, "inner": inner_index, "row": row})
    require(rows, "No decoded XNET transfer instructions")
    for account in xnet:
        delta = raw["post"].get(account, 0) - raw["pre"].get(account, 0)
        require(predicted[account] == delta, "Decoded instruction flow does not reconcile token balances")
    # Preserve existing grouped raw-times-multiplicity semantics, not one net row.
    grouped = Counter(tuple(row[field] for field in TRANSFER_FIELDS if field not in {"event_id", "event_multiplicity"}) for row in rows)
    fields = [f for f in TRANSFER_FIELDS if f not in {"event_id", "event_multiplicity"}]
    output = []
    for values, count in sorted(grouped.items()):
        row = dict(zip(fields, values))
        row["event_multiplicity"] = str(count)
        output.append(normalise_transfer(row))
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return output, {"signature": signature, "slot": slot, "source": "finalized_solana_rpc",
                    "response_sha256": digest, "instructions": identities, "balance_flow_verified": True}
