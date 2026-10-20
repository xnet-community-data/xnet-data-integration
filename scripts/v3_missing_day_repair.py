"""Offline acceptance for a *previously absent* day, not general replacement.

The trusted receipt is pinned by the authenticated retrieval boundary. This
module cannot authenticate an input claiming to be from Dune. It cannot delete
or modify existing events and never converts an empty source result into zeros.
Use the instruction-aware scoped replacement for nonempty/restated windows.
"""
import csv
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import io
import json

from v3_reduce_chain import TRANSFER_FIELDS, normalise_transfer, parse_ts


class MissingDayRejected(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def accept_missing_day(canonical_bytes, result_bytes, trusted):
    """Return candidate bytes+receipt; callers stage/reduce/validate/publish once."""
    try:
        return _accept_missing_day(canonical_bytes, result_bytes, trusted)
    except MissingDayRejected:
        raise
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        raise MissingDayRejected(f"Malformed repair evidence: {error}") from error


def _accept_missing_day(canonical_bytes, result_bytes, trusted):
    def require(test, message):
        if not test:
            raise MissingDayRejected(message)
    require(isinstance(trusted, dict), "Independent authenticated receipt required")
    require(isinstance(trusted.get("execution_id"), str) and trusted["execution_id"].strip()
            and trusted["execution_id"] == trusted["execution_id"].strip(), "Missing exact execution identity")
    require(trusted.get("canonical_sha256") == digest(canonical_bytes), "Stale canonical predecessor")
    require(trusted.get("result_sha256") == digest(result_bytes), "Source artifact fingerprint changed")
    require(trusted.get("query_id") == 8894038 and trusted.get("query_version") == 4,
            "Unreviewed query contract")
    require(trusted.get("saved_sql_sha256") == "fb81e9dbf429ece52f3481e2e54388d3e7d0b0c219dccf3582a5d7516250cc92",
            "Unpinned saved query")
    # A locally authenticated submit/completion bracket proves CURRENT_DATE only
    # if the entire possible execution interval lies on the same UTC calendar day.
    require(all(isinstance(trusted.get(k), str) and trusted[k].endswith("Z")
                for k in ("submitted_at_utc", "completed_received_at_utc")), "Explicit UTC interval required")
    submitted = parse_ts(trusted["submitted_at_utc"])
    received = parse_ts(trusted["completed_received_at_utc"])
    require(submitted <= received and submitted.date() == received.date(), "Relative scope crossed UTC midnight")
    require(submitted.tzinfo is not None and received.tzinfo is not None, "Explicit UTC scope evidence required")
    params = trusted.get("query_parameters", {})
    require(set(params) == {"oldest_days_ago", "newest_days_ago"}, "Wrong query parameters")
    offset = params["oldest_days_ago"]
    require(type(offset) is int and offset >= 1 and params["newest_days_ago"] == offset, "Not an exact completed day")
    day = (submitted.date() - timedelta(days=offset)).isoformat()
    require(day == trusted.get("absolute_day") and day > "2026-10-03", "Scope/seed boundary mismatch")
    result = json.loads(result_bytes)
    require(result.get("executionId") == trusted.get("execution_id") and result.get("state") == "COMPLETED",
            "Wrong or incomplete execution")
    rows = result.get("data", {}).get("rows")
    metadata = result.get("resultMetadata", {})
    require(isinstance(rows, list) and 0 < len(rows) <= 10000 and
            type(metadata.get("totalRowCount")) is int and metadata["totalRowCount"] == len(rows),
            "Empty, incomplete or unbounded source result")
    columns = [column["name"] for column in metadata.get("columns", [])]
    expected = set(TRANSFER_FIELDS) - {"event_id"}
    require(set(columns) == expected and len(columns) == len(expected), "Projected/incompatible source columns")
    reader = csv.DictReader(io.StringIO(canonical_bytes.decode()))
    require(reader.fieldnames == TRANSFER_FIELDS, "Wrong canonical schema")
    existing = list(reader)
    require(existing and all(row.get("block_date") != day and
                            parse_ts(row["block_time"]).date().isoformat() != day for row in existing),
            "Day is already populated; exact replacement proof is required")
    old_ids = {r["event_id"] for r in existing}
    added, ids, locators = [], set(), set()
    for source in rows:
        require(set(source) == expected and source["block_date"] == day and
                parse_ts(source["block_time"]).date().isoformat() == day, "Out-of-scope/incomplete source row")
        row = dict(source)
        for key in ("amount_raw", "block_slot", "event_multiplicity"):
            value = row[key]
            require(type(value) is int or isinstance(value, str), "Integer financial fields required")
            text = str(value)
            require(text.isascii() and text.isdigit() and str(int(text)) == text, "Non-exact raw integer")
            require(int(text) >= (1 if key == "event_multiplicity" else 0), "Invalid raw integer")
            row[key] = text
        require(row["action"] in {"transfer", "burn", "mint"}, "Unsupported action")
        for side in ("from", "to"):
            needed = row["action"] == "transfer" or (side == "from" and row["action"] == "burn") or (side == "to" and row["action"] == "mint")
            require(not needed or all(isinstance(row.get(f"{side}_{suffix}"), str) and row[f"{side}_{suffix}"].strip()
                                     and row[f"{side}_{suffix}"] == row[f"{side}_{suffix}"].strip()
                                     for suffix in ("owner", "token_account")), "Unresolved financial owner/account")
        require(isinstance(row["tx_id"], str) and row["tx_id"].strip()
                and row["tx_id"] == row["tx_id"].strip(), "Missing transaction identity")
        row["amount_xnet"] = format(Decimal(row["amount_raw"]) / Decimal(10**8), "f")
        row = normalise_transfer(row)
        locator = tuple(row[k] for k in ("tx_id", "block_slot", "action", "from_token_account", "to_token_account"))
        require(row["event_id"] not in old_ids | ids and locator not in locators,
                "Ambiguous legacy event locator requires instruction-aware replacement")
        ids.add(row["event_id"])
        locators.add(locator)
        added.append(row)
    # The old day is absent, so no old row is removed, normalized or re-encoded.
    require(canonical_bytes.endswith(b"\n"), "Canonical final record is incomplete")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=TRANSFER_FIELDS)
    writer.writerows(sorted(added, key=lambda r: (r["block_time"], int(r["block_slot"]), r["tx_id"])))
    candidate = canonical_bytes + output.getvalue().encode()
    receipt = {"schema_version": 1, "kind": "verified_missing_day_addition", "day": day,
               "execution_id": trusted["execution_id"], "query_id": 8894038, "query_version": 4,
               "query_parameters": params, "source_sha256": digest(result_bytes),
               "canonical_before_sha256": digest(canonical_bytes), "canonical_after_sha256": digest(candidate),
               "added_rows": len(added), "deleted_rows": 0, "outside_bytes_preserved": True,
               "scope_start_utc": day + "T00:00:00Z",
               "scope_end_utc": (date.fromisoformat(day) + timedelta(days=1)).isoformat() + "T00:00:00Z"}
    return candidate, receipt
