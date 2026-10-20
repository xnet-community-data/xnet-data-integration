"""Pure, offline validation for an additive transfer-day result.

This module validates one complete provider response for an absolute UTC day
against the canonical rows already published for that day.  It returns only
the normalized additions and a receipt; it never writes state, sorts the whole
canonical ledger, deletes a row, or claims financial authority.

The caller owns the authenticated execution/sql identity, the provider's
completed/complete state, the immutable-day receipt, the checkpoint and the
reducer.  In particular, this function has no clock and therefore makes no
future-timestamp claim; a caller that needs one must validate its captured
snapshot time separately.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from v3_reduce_chain import TRANSFER_FIELDS, normalise_transfer, parse_ts


class AdditiveDayRejected(ValueError):
    """The provider result is not a safe additive response for this day."""


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]"
    r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z| UTC|\+00:00)$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = {"transfer", "burn", "mint"}
_PROVIDER_FIELDS = tuple(f for f in TRANSFER_FIELDS if f != "event_id")
_CENT = Decimal("100000000")


def _reject(message):
    raise AdditiveDayRejected(message)


def _require(condition, message):
    if not condition:
        _reject(message)


def _day(value, label):
    _require(isinstance(value, str) and _DAY_RE.fullmatch(value) is not None,
             f"{label} must be an exact YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise AdditiveDayRejected(f"{label} is not a real date") from error
    _require(parsed.isoformat() == value, f"{label} is not canonical")
    return parsed


def _strict_utc_timestamp(value, label):
    """Parse only an explicit UTC timestamp, before the permissive legacy parse."""
    _require(isinstance(value, str) and value == value.strip() and value,
             f"{label} must be a nonempty exact UTC timestamp")
    _require(_TIME_RE.fullmatch(value) is not None,
             f"{label} must be YYYY-MM-DD[T ]HH:MM:SS[.ffffff](Z| UTC|+00:00)")
    try:
        instant = parse_ts(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise AdditiveDayRejected(f"{label} is not a valid timestamp") from error
    _require(instant.tzinfo is not None and instant.utcoffset() == timedelta(0),
             f"{label} must be UTC, not an offset")
    return instant.astimezone(timezone.utc)


def _strict_integer(value, label, minimum):
    """Exact integer text or int; bool, float, exponent and leading zero fail."""
    if type(value) is int:
        number = value
    elif isinstance(value, str):
        _require(value and value.isascii() and value.isdigit(),
                 f"{label} must be exact nonnegative integer text")
        _require(len(value) == 1 or value[0] != "0",
                 f"{label} must not contain leading zeros")
        number = int(value)
    else:
        _reject(f"{label} must be an exact integer, not {type(value).__name__}")
    _require(number >= minimum, f"{label} must be >= {minimum}")
    return str(number)


def _strict_text(value, label, *, required):
    if value is None:
        value = ""
    _require(isinstance(value, str), f"{label} must be text")
    _require(value == value.strip(), f"{label} must not have surrounding whitespace")
    if required:
        _require(bool(value), f"{label} is required")
    return value


def _normalise_row(source, day, label, *, canonical):
    expected = TRANSFER_FIELDS if canonical else _PROVIDER_FIELDS
    _require(isinstance(source, dict), f"{label} must be an object")
    _require(set(source) == set(expected),
             f"{label} must have exactly the canonical transfer fields"
             + ("" if canonical else " without event_id"))

    row = {}
    row["block_date"] = _strict_text(source["block_date"], f"{label}.block_date", required=True)
    _require(row["block_date"] == day, f"{label}.block_date does not match {day}")
    row["block_time"] = _strict_text(source["block_time"], f"{label}.block_time", required=True)
    instant = _strict_utc_timestamp(row["block_time"], f"{label}.block_time")
    _require(instant.date().isoformat() == day,
             f"{label}.block_time is outside {day}")

    row["block_slot"] = _strict_integer(source["block_slot"], f"{label}.block_slot", 0)
    row["tx_id"] = _strict_text(source["tx_id"], f"{label}.tx_id", required=True)

    action = _strict_text(source["action"], f"{label}.action", required=True)
    _require(action in _ACTIONS, f"{label}.action is unsupported")
    row["action"] = action

    for side in ("from", "to"):
        needed = (
            action == "transfer"
            or (action == "burn" and side == "from")
            or (action == "mint" and side == "to")
        )
        owner = _strict_text(source[f"{side}_owner"], f"{label}.{side}_owner", required=needed)
        account = _strict_text(
            source[f"{side}_token_account"],
            f"{label}.{side}_token_account",
            required=needed,
        )
        row[f"{side}_owner"] = owner
        row[f"{side}_token_account"] = account

    row["amount_raw"] = _strict_integer(source["amount_raw"], f"{label}.amount_raw", 0)
    row["event_multiplicity"] = _strict_integer(
        source["event_multiplicity"], f"{label}.event_multiplicity", 1
    )

    # The amount is derived from raw units.  Provider decimal rendering is
    # ignored.  For an existing canonical row, a different *value* is not a
    # rendering difference and is rejected.
    amount = Decimal(row["amount_raw"]) / _CENT
    _require(amount.is_finite(), f"{label}.amount_raw is not finite")
    row["amount_xnet"] = format(amount, "f")
    if canonical:
        try:
            stored = Decimal(str(source["amount_xnet"]))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise AdditiveDayRejected(f"{label}.amount_xnet is not decimal text") from error
        _require(stored.is_finite() and stored == amount,
                 f"{label}.amount_xnet does not match amount_raw")

    normalised = normalise_transfer(row)
    # normalise_transfer is the single event-id authority.  Preserve the derived
    # decimal rather than any provider/canonical rendering.
    normalised["amount_xnet"] = row["amount_xnet"]
    if canonical:
        _require(isinstance(source["event_id"], str) and source["event_id"],
                 f"{label}.event_id is required")
        _require(source["event_id"] == normalised["event_id"],
                 f"{label}.event_id does not match its financial identity")
    return normalised


def _semantic_time(value):
    return _strict_utc_timestamp(value, "row.block_time").isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _semantic_row(row):
    return {
        "event_id": row["event_id"],
        "block_date": row["block_date"],
        "block_time": _semantic_time(row["block_time"]),
        "block_slot": row["block_slot"],
        "tx_id": row["tx_id"],
        "action": row["action"],
        "from_owner": row["from_owner"],
        "to_owner": row["to_owner"],
        "from_token_account": row["from_token_account"],
        "to_token_account": row["to_token_account"],
        "amount_raw": row["amount_raw"],
        "amount_xnet": row["amount_xnet"],
        "event_multiplicity": row["event_multiplicity"],
    }


def _order(row):
    return (
        _strict_utc_timestamp(row["block_time"], "row.block_time"),
        int(row["block_slot"]),
        row["tx_id"],
        row["event_id"],
    )


def _financial_signature(row):
    """Everything whose change would make an existing event a different fact."""
    return (
        row["tx_id"],
        row["block_slot"],
        row["action"],
        row["from_owner"],
        row["to_owner"],
        row["from_token_account"],
        row["to_token_account"],
        row["amount_raw"],
        row["event_multiplicity"],
        _strict_utc_timestamp(row["block_time"], "row.block_time"),
    )


def _locator(row):
    return (
        row["tx_id"],
        row["block_slot"],
        row["action"],
        row["from_token_account"],
        row["to_token_account"],
    )


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _rows_hash(rows):
    return _hash([_semantic_row(row) for row in sorted(rows, key=_order)])


def _validate(canonical_rows, provider_rows, absolute_day, execution_id, sql_sha256,
              seed_cutoff):
    _require(isinstance(canonical_rows, list), "canonical_rows must be a list")
    _require(isinstance(provider_rows, list), "provider_rows must be a list")
    _require(all(isinstance(row, dict) for row in canonical_rows),
             "canonical_rows entries must be objects")
    day = _day(absolute_day, "absolute_day").isoformat()
    _require(isinstance(execution_id, str) and execution_id and execution_id == execution_id.strip(),
             "execution_id must be a nonempty exact identity")
    _require(isinstance(sql_sha256, str) and _SHA256_RE.fullmatch(sql_sha256) is not None,
             "sql_sha256 must be a lowercase 64-character sha256")
    if seed_cutoff is not None:
        cutoff = _day(seed_cutoff, "seed_cutoff").isoformat()
        _require(day > cutoff, "seeded day is not admissible as an addition")

    old_day = [row for row in canonical_rows if row.get("block_date") == day]
    old = [
        _normalise_row(row, day, f"canonical[{index}]", canonical=True)
        for index, row in enumerate(old_day)
    ]
    provider = [
        _normalise_row(row, day, f"provider[{index}]", canonical=False)
        for index, row in enumerate(provider_rows)
    ]

    _require(len({row["event_id"] for row in old}) == len(old),
             "canonical day contains a duplicate event_id")
    _require(len({row["event_id"] for row in provider}) == len(provider),
             "provider contains a duplicate result event_id")

    old_ids = {row["event_id"] for row in old}
    provider_ids = {row["event_id"] for row in provider}
    old_counts = Counter(_financial_signature(row) for row in old)
    provider_counts = Counter(_financial_signature(row) for row in provider)

    for signature, count in old_counts.items():
        _require(provider_counts[signature] >= count,
                 "provider omits or changes an existing day event")
    _require(old_ids <= provider_ids,
             "provider omits an existing canonical event_id")

    additions = sorted((row for row in provider if row["event_id"] not in old_ids), key=_order)
    canonical_ids = {
        row.get("event_id") for row in canonical_rows
        if isinstance(row.get("event_id"), str) and row.get("event_id")
    }
    _require(not ({row["event_id"] for row in additions} & canonical_ids),
             "new result event_id collides with an existing canonical event_id")

    old_locators = defaultdict(set)
    for row in old:
        old_locators[_locator(row)].add(row["amount_raw"])
    new_groups = defaultdict(list)
    for row in additions:
        locator = _locator(row)
        _require(locator not in old_locators,
                 "new row reuses an existing ambiguous locator; instruction proof required")
        new_groups[locator].append(row)

    for rows in new_groups.values():
        _require(len(rows) == 1,
                 "new locator has multiple rows/amounts; instruction proof required")
        _require(len({row["amount_raw"] for row in rows}) == 1,
                 "new locator has multiple amount variants; instruction proof required")

    # Existing multi-variant locator groups are preserved by the multiset check
    # above.  Additions are sorted, but the whole canonical ledger is never
    # returned or re-sorted here.
    before = _rows_hash(old)
    after_provider = _rows_hash(provider)
    addition_ids = [row["event_id"] for row in additions]
    start = date.fromisoformat(day)
    receipt = {
        "schema_version": 1,
        "kind": "verified_additive_day",
        "absolute_day": day,
        "scope_start_utc": start.isoformat() + "T00:00:00Z",
        "scope_end_utc": (start + timedelta(days=1)).isoformat() + "T00:00:00Z",
        "execution_id": execution_id,
        "sql_sha256": sql_sha256,
        "canonical_day_row_count": len(old),
        "provider_row_count": len(provider),
        "matched_old_rows": len(old),
        "added_rows": len(additions),
        "deleted_rows": 0,
        "canonical_day_semantics_sha256": before,
        "normalized_provider_semantics_sha256": after_provider,
        "addition_event_ids": addition_ids,
        "addition_event_ids_sha256": _hash(sorted(addition_ids)),
        "other_canonical_days_preserved": True,
        "financial_authority": False,
    }
    return {"additions": additions, "receipt": receipt}


def validate_additive_day(canonical_rows, provider_rows, absolute_day, *,
                          execution_id, sql_sha256, seed_cutoff=None):
    """Return normalized additions and an audit receipt, or raise.

    The caller must have already established that the provider response is a
    complete ``COMPLETED`` result for ``absolute_day``.  This function does not
    authenticate that claim and does not write, publish, reduce or delete
    anything.
    """
    try:
        return _validate(
            canonical_rows, provider_rows, absolute_day, execution_id,
            sql_sha256, seed_cutoff,
        )
    except AdditiveDayRejected:
        raise
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        raise AdditiveDayRejected(f"Malformed additive-day evidence: {error}") from error
