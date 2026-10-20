"""Durable standing risk allocations; never settlement or a provider charge cap.

Authority is explicit in reviewed config. Each operation still needs a
sample-backed quote, an immutable reservation, and a checkpoint before
transport. Opening known obligations are conservatively reserved as a risk
baseline; this module does not claim they are already inside provider usage.

For export reservations, ``operation_id`` is the financial export attempt ID.
``execution_id`` is authority/correlation data, never read identity: several
legitimate reads may share one execution ID.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import math
import re

from v3_credit_budget import BudgetInputError, normalize_usage, parse_instant
from v3_financial_journal import amount, financial_obligations

LANE = "standing_credit_budget"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_KINDS = {"execution", "export"}


def _z(value):
    return parse_instant(value, "instant").isoformat().replace("+00:00", "Z")


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise BudgetInputError(f"{label} must be a nonempty exact string")
    return value


def _day(value, label):
    if not isinstance(value, str) or _DAY_RE.fullmatch(value) is None:
        raise BudgetInputError(f"{label} must be an exact YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise BudgetInputError(f"{label} is not a real date") from error
    if parsed.isoformat() != value:
        raise BudgetInputError(f"{label} is not canonical")
    return value


def _shape(value, label):
    if value is not None and (isinstance(value, bool)
                              or not isinstance(value, (str, int))):
        raise BudgetInputError(f"{label} must be a string or integer")
    return value


def _guard(fn):
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except BudgetInputError:
            raise
        except (AttributeError, KeyError, IndexError, TypeError, ValueError,
                OverflowError) as error:
            raise BudgetInputError(
                f"Malformed standing-budget API input: {error}") from error
    wrapped.__name__ = fn.__name__
    return wrapped


def _policy(config, now_utc):
    if not isinstance(config, dict):
        raise BudgetInputError("Standing config must be an object")
    value = config.get("autonomous_operation")
    if not isinstance(value, dict) or value.get("enabled") is not True:
        raise BudgetInputError("Standing authorization is not enabled")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise BudgetInputError("Unsupported standing authorization")
    for key in ("approval_ref", "authorized_by", "authorization_text"):
        _text(value.get(key), "Missing standing authority: " + key)
    now = parse_instant(now_utc, "now")
    if parse_instant(value.get("authorized_at_utc"), "authorized at") > now:
        raise BudgetInputError("Standing authorization is in the future")
    for key in ("legacy_unknown_risk_credits", "monthly_safety_reserve_credits",
                "minimum_unspent_quota_credits", "daily_risk_limit_credits",
                "forecast_margin_credits", "period_hard_limit_credits", "operating_target_credits"):
        if amount(value.get(key), key) <= 0:
            raise BudgetInputError("Standing risk allocation must be positive: " + key)
    _text(config.get("performance"), "config performance")
    queries = value.get("queries")
    if not isinstance(queries, dict) or not queries:
        raise BudgetInputError("Standing authorization requires exact query contracts")
    for query_id, row in queries.items():
        _text(query_id, "query contract id")
        if not isinstance(row, dict):
            raise BudgetInputError("Malformed query contract")
        kinds = row.get("kinds")
        if (not isinstance(kinds, list) or not kinds
                or any(kind not in _KINDS for kind in kinds)):
            raise BudgetInputError("Malformed query contract kinds")
        if row.get("performance") is not None:
            _text(row.get("performance"), "contract performance")
        family = row.get("parameter_family")
        if family not in {"none", "hours", "days", "single_utc_day"}:
            raise BudgetInputError("Malformed query parameter family")
        if amount(row.get("max_operation_risk_credits"), "operation risk") <= 0:
            raise BudgetInputError("Operation risk authority must be positive")
        if family == "hours":
            low, high = row.get("min_lookback_hours"), row.get("max_lookback_hours")
            if (type(low) is not int or type(high) is not int or low < 1 or low > high):
                raise BudgetInputError("Malformed hour contract")
        elif family == "days":
            allowed = row.get("allowed_lookback_days")
            if (not isinstance(allowed, list) or not allowed
                    or any(type(day) is not int or day < 0 for day in allowed)):
                raise BudgetInputError("Malformed day contract")
        elif family == "single_utc_day":
            if type(row.get("max_days_ago")) is not int or row["max_days_ago"] < 0:
                raise BudgetInputError("Malformed single-day contract")
    return value


policy = _guard(_policy)


def _scope(config, spec, params, kind, now_utc):
    authority = _policy(config, now_utc)
    if not isinstance(spec, dict):
        raise BudgetInputError("Operation spec must be an object")
    kind = _text(kind, "operation kind")
    query_id = spec.get("query_id")
    if isinstance(query_id, bool) or not isinstance(query_id, (str, int)):
        raise BudgetInputError("Operation query_id is invalid")
    row = authority["queries"].get(str(query_id))
    if not isinstance(row, dict) or kind not in row["kinds"]:
        raise BudgetInputError("Operation is outside standing query authority")
    performance = spec.get("performance", config["performance"])
    if performance != row.get("performance"):
        raise BudgetInputError("Operation engine is outside standing authority")
    parameters = {} if params is None else params
    if not isinstance(parameters, dict):
        raise BudgetInputError("Operation parameters must be an object")
    family = row["parameter_family"]
    valid = False
    if family == "none":
        valid = not parameters
    elif family == "hours":
        hours = parameters.get("lookback_hours")
        valid = (set(parameters) == {"lookback_hours"} and type(hours) is int
                 and row["min_lookback_hours"] <= hours <= row["max_lookback_hours"])
    elif family == "days":
        days = parameters.get("lookback_days")
        valid = (set(parameters) == {"lookback_days"} and type(days) is int
                 and days in row["allowed_lookback_days"])
    elif family == "single_utc_day":
        oldest, newest = parameters.get("oldest_days_ago"), parameters.get("newest_days_ago")
        valid = (set(parameters) == {"oldest_days_ago", "newest_days_ago"}
                 and type(oldest) is int and type(newest) is int and oldest == newest
                 and 0 <= newest <= row["max_days_ago"])
    if not valid:
        raise BudgetInputError("Parameters are outside standing authority")
    _shape(spec.get("shape_version"), "shape_version")
    if kind == "export":
        _text(spec.get("execution_id"), "Export authority requires its exact execution ID")
    return row


scope = _guard(_scope)


def _validate_operation(row, operation_id, label, *, carry=False):
    if not isinstance(row, dict):
        raise BudgetInputError(f"{label} must be an object")
    if row.get("operation_id", operation_id) != operation_id:
        raise BudgetInputError(f"{label} operation identity mismatch")
    if row.get("kind") not in _KINDS:
        raise BudgetInputError(f"{label} kind is invalid")
    query_id = row.get("query_id")
    if isinstance(query_id, bool) or not isinstance(query_id, (str, int)):
        raise BudgetInputError(f"{label} query_id is invalid")
    if row["kind"] == "export":
        _text(row.get("execution_id"), f"{label} export execution_id")
    _text(row.get("liability_id"), f"{label} liability_id")
    amount(row.get("quote_credits"), f"{label} quote")
    parse_instant(row.get("reserved_at_utc"), f"{label} reserved_at")
    if not isinstance(row.get("query_parameters"), dict):
        raise BudgetInputError(f"{label} query_parameters must be an object")
    _shape(row.get("shape_version"), f"{label} shape_version")
    if carry:
        _text(row.get("period_key"), f"{label} period_key")
        amount(row.get("known_cost_floor_credits", 0), f"{label} known floor")
        if row.get("matched_liability_id") is not None:
            _text(row.get("matched_liability_id"), f"{label} matched liability")
        return
    if not isinstance(row.get("evidence"), dict) or not row["evidence"]:
        raise BudgetInputError(f"{label} evidence is required")
    approval = row.get("approval")
    if not isinstance(approval, dict):
        raise BudgetInputError(f"{label} approval is required")
    parse_instant(approval.get("valid_until_utc"), f"{label} approval expiry")


def _validate_book(book, label):
    if not isinstance(book, dict):
        raise BudgetInputError(f"{label} must be an object")
    for key in ("authority_sha256", "opening_usage_credits",
                "usage_high_water_credits"):
        if key not in book:
            raise BudgetInputError(f"{label} missing {key}")
    _text(book["authority_sha256"], f"{label} authority hash")
    amount(book["opening_usage_credits"], f"{label} opening usage")
    amount(book["usage_high_water_credits"], f"{label} high water")
    if not isinstance(book.get("opening_obligations"), dict):
        raise BudgetInputError(f"{label} opening_obligations must be an object")
    if not isinstance(book.get("operations"), dict):
        raise BudgetInputError(f"{label} operations must be an object")
    if "inherited_operations" in book and not isinstance(book["inherited_operations"], dict):
        raise BudgetInputError(f"{label} inherited_operations must be an object")
    parse_instant(book.get("period_start_utc"), f"{label} period start")
    parse_instant(book.get("period_end_utc"), f"{label} period end")
    if set(book["operations"]) & set(book.get("inherited_operations", {})):
        raise BudgetInputError(f"{label} double-counts one operation")
    for operation_id, row in book["operations"].items():
        _validate_operation(row, operation_id, f"{label} operation", carry=False)
    for operation_id, row in book.get("inherited_operations", {}).items():
        _validate_operation(row, operation_id, f"{label} inherited", carry=True)


def _validate_new_period(lane, start, end):
    latest_end = None
    for label, old in lane["periods"].items():
        if not isinstance(old, dict):
            raise BudgetInputError("Malformed standing period")
        old_start = parse_instant(old.get("period_start_utc"), f"{label} start")
        old_end = parse_instant(old.get("period_end_utc"), f"{label} end")
        if old_start >= old_end:
            raise BudgetInputError("Malformed standing period edges")
        if start < old_end and old_start < end:
            raise BudgetInputError("Overlapping or rewound billing period refused")
        latest_end = old_end if latest_end is None else max(latest_end, old_end)
    if latest_end is not None and start < latest_end:
        raise BudgetInputError("New period starts before the last period end")


def _item_matches(reservation, item):
    # Execution uses submission:<attempt>; export uses the exact attempt ID.
    # execution_id is deliberately not an identity for exports (reads may share it).
    liability = reservation["liability_id"]
    return liability in {item.get("liability_id"), item.get("submission_id")}


def _carry_entries(lane, exclude_key):
    entries = {}
    for period_key, old in lane["periods"].items():
        if period_key == exclude_key:
            continue
        if not isinstance(old, dict):
            raise BudgetInputError("Malformed standing period")
        for field in ("operations", "inherited_operations"):
            rows = old.get(field, {})
            if not isinstance(rows, dict):
                raise BudgetInputError("Malformed standing operation book")
            for operation_id, row in rows.items():
                _validate_operation(row, operation_id, "prior operation",
                                    carry=(field == "inherited_operations"))
                entry = {
                    "operation_id": operation_id,
                    "kind": row["kind"],
                    "liability_id": row["liability_id"],
                    "execution_id": row.get("execution_id"),
                    "quote_credits": amount(row["quote_credits"], "prior quote"),
                    "known_cost_floor_credits": amount(
                        row.get("known_cost_floor_credits", 0), "prior floor"),
                    "matched_liability_id": row.get("matched_liability_id"),
                    "reserved_at_utc": _z(row["reserved_at_utc"]),
                    "period_key": (row.get("period_key") if field == "inherited_operations"
                                   else period_key),
                    "query_id": row.get("query_id"),
                    "query_parameters": deepcopy(row.get("query_parameters", {})),
                    "performance": row.get("performance"),
                    "shape_version": row.get("shape_version"),
                }
                previous = entries.get(operation_id)
                if previous is None:
                    entries[operation_id] = entry
                    continue
                identity = ("kind", "liability_id", "execution_id", "query_id",
                            "query_parameters", "performance", "shape_version")
                if any(previous.get(k) != entry.get(k) for k in identity):
                    raise BudgetInputError("Ambiguous carried reservation identity")
                previous["quote_credits"] = max(
                    previous["quote_credits"], entry["quote_credits"])
                previous["known_cost_floor_credits"] = max(
                    previous.get("known_cost_floor_credits", 0),
                    entry.get("known_cost_floor_credits", 0))
                previous_match = previous.get("matched_liability_id")
                entry_match = entry.get("matched_liability_id")
                if previous_match and entry_match and previous_match != entry_match:
                    raise BudgetInputError("Ambiguous carried charge identity")
                if previous_match is None:
                    previous["matched_liability_id"] = entry_match
                if parse_instant(entry["reserved_at_utc"], "reserved") < parse_instant(
                        previous["reserved_at_utc"], "reserved"):
                    previous["reserved_at_utc"] = entry["reserved_at_utc"]
    return entries


def _match_carry(operation, all_items):
    matches = [item for item in all_items if _item_matches(operation, item)]
    if len(matches) > 1:
        raise BudgetInputError("One carried reservation links multiple charges")
    return matches[0] if matches else None


def _build_carry(lane, exclude_key, all_items):
    carry = {}
    for operation_id, operation in _carry_entries(lane, exclude_key).items():
        item = _match_carry(operation, all_items)
        if item is not None and item.get("cost_credits") is not None:
            continue  # terminal known cost replaces the quote
        if item is not None:
            operation["known_cost_floor_credits"] = max(
                operation.get("known_cost_floor_credits", 0),
                amount(item.get("known_cost_floor_credits", 0), "known floor"))
            operation["matched_liability_id"] = item["liability_id"]
        carry[operation_id] = operation
    return carry


def _open_period(state, config, usage, now_utc):
    authority = _policy(config, now_utc)
    if not isinstance(state, dict):
        raise BudgetInputError("Standing state must be an object")
    normalized = normalize_usage(usage, now_utc)
    p = normalized["period"]
    if normalized["blockers"] or not p or not p["period_start_utc"] or not p["period_end_utc"]:
        raise BudgetInputError("Standing operation requires exact live billing metadata")
    now = parse_instant(now_utc, "now")
    if not p["period_start_utc"] <= now < p["period_end_utc"]:
        raise BudgetInputError("Billing period is not active")
    key = _z(p["period_start_utc"]) + "/" + _z(p["period_end_utc"])
    lane = state.setdefault(LANE, {"schema_version": 1, "periods": {}})
    if (not isinstance(lane, dict) or type(lane.get("schema_version")) is not int
            or lane["schema_version"] != 1 or not isinstance(lane.get("periods"), dict)):
        raise BudgetInputError("Malformed standing budget book")
    if key not in lane["periods"]:
        _validate_new_period(lane, p["period_start_utc"], p["period_end_utc"])
        all_items, _ = financial_obligations(state)
        items, _ = financial_obligations(state, period_start_utc=p["period_start_utc"])
        opening = {item["liability_id"]: {
            "cost_credits": item.get("cost_credits"),
            "known_cost_floor_credits": item.get("known_cost_floor_credits", 0),
        } for item in items}
        lane["periods"][key] = {
            "authority_sha256": _authority_hash(authority),
            "approval_ref": authority["approval_ref"],
            "opened_at_utc": _z(now),
            "opening_usage_credits": p["credits_used"],
            "usage_high_water_credits": p["credits_used"],
            "period_start_utc": _z(p["period_start_utc"]),
            "period_end_utc": _z(p["period_end_utc"]),
            "opening_obligations": opening,
            "inherited_operations": _build_carry(lane, key, all_items),
            "carry_schema_version": 1,
            "operations": {},
        }
    book = lane["periods"][key]
    _validate_book(book, "standing period")
    if book["authority_sha256"] != _authority_hash(authority):
        raise BudgetInputError("Standing authority changed within the allocated billing period")
    if (_z(book["period_start_utc"]) != _z(p["period_start_utc"])
            or _z(book["period_end_utc"]) != _z(p["period_end_utc"])):
        raise BudgetInputError("Standing period book does not match provider period")
    all_items, _ = financial_obligations(state)
    if "inherited_operations" not in book or "carry_schema_version" not in book:
        book["inherited_operations"] = _build_carry(lane, key, all_items)
        book["carry_schema_version"] = 1
        book.pop("inherited_unsettled_risk_credits", None)
    else:
        for operation_id in list(book["inherited_operations"]):
            operation = book["inherited_operations"][operation_id]
            item = _match_carry(operation, all_items)
            if item is not None and item.get("cost_credits") is not None:
                del book["inherited_operations"][operation_id]
            elif item is not None:
                operation["known_cost_floor_credits"] = max(
                    operation.get("known_cost_floor_credits", 0),
                    amount(item.get("known_cost_floor_credits", 0), "known floor"))
                operation["matched_liability_id"] = item["liability_id"]
    book["usage_high_water_credits"] = max(
        book["usage_high_water_credits"], p["credits_used"])
    return key, book, p, all_items


open_period = _guard(
    lambda state, config, usage, now_utc: _open_period(state, config, usage, now_utc)[:3])


def _authority_hash(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()


def _evaluate(state, config, usage, now_utc, *, forecast_credits=0.0, quote=None):
    candidate = deepcopy(state)
    key, book, p, all_items = _open_period(candidate, config, usage, now_utc)
    authority = _policy(config, now_utc)
    items, _ = financial_obligations(state, period_start_utc=p["period_start_utc"])
    by_id = {item["liability_id"]: item for item in all_items}
    opening = book["opening_obligations"]
    operations, carry = book["operations"], book["inherited_operations"]
    now = parse_instant(now_utc, "now")
    today = now.date()

    current_matches, carry_matches = {}, {}
    for operation_id, operation in operations.items():
        matches = [item for item in items if _item_matches(operation, item)]
        if len(matches) > 1:
            raise BudgetInputError("Standing reservation links multiple charges")
        if matches and matches[0]["liability_id"] in opening:
            raise BudgetInputError("Reservation duplicates an opening obligation")
        current_matches[operation_id] = matches[0] if matches else None
    for operation_id, operation in carry.items():
        carry_matches[operation_id] = _match_carry(operation, all_items)

    item_ops = defaultdict(list)
    for operation_id, item in current_matches.items():
        if item is not None:
            item_ops[item["liability_id"]].append(("current", operation_id))
    for operation_id, item in carry_matches.items():
        if item is not None:
            item_ops[item["liability_id"]].append(("carry", operation_id))
    for identity, links in item_ops.items():
        if len(links) > 1:
            raise BudgetInputError("One charge links multiple standing reservations")

    opening_known = specific_unknown = post_actual = daily = 0.0
    unknown_ids, pending, blockers = [], set(), []
    processed_carry, daily_ids = set(), set()

    def add_daily(identity, value, incurred=None, reserved=None):
        if identity in daily_ids:
            return
        instant = incurred if incurred is not None else reserved
        if instant is None or parse_instant(instant, "daily date").date() == today:
            nonlocal daily
            daily += value
            daily_ids.add(identity)

    # Opening known bases are ALWAYS reserved; do not claim they are billed.
    for identity, old in opening.items():
        if old["cost_credits"] is None:
            continue
        value = max(amount(old["cost_credits"], "opening cost"),
                    amount(old.get("known_cost_floor_credits", 0), "opening floor"))
        item = next((row for row in items if row["liability_id"] == identity), None)
        if item is not None:
            value = max(value, amount(item.get("cost_credits") or 0, "current cost"),
                        amount(item.get("known_cost_floor_credits", 0), "current floor"))
        opening_known += value
        for operation_id, linked in carry_matches.items():
            if ((linked is not None and linked["liability_id"] == identity)
                    or carry[operation_id].get("matched_liability_id") == identity):
                processed_carry.add(operation_id)

    # Opening unknowns: carry quote/floor max once, otherwise reserve the floor.
    for identity, old in opening.items():
        if old["cost_credits"] is not None:
            continue
        item = next((row for row in items if row["liability_id"] == identity), None)
        all_item = by_id.get(identity)
        linked = [operation_id for operation_id, match in carry_matches.items()
                  if ((match is not None and match["liability_id"] == identity)
                      or carry[operation_id].get("matched_liability_id") == identity)]
        if len(linked) > 1:
            raise BudgetInputError("Ambiguous carried opening obligation")
        if item is not None and item.get("cost_credits") is not None:
            value = max(amount(item["cost_credits"], "known cost"),
                        amount(item.get("known_cost_floor_credits", 0), "known floor"))
            post_actual += value
            add_daily(identity, value, incurred=item.get("incurred_at_utc"))
            for operation_id in linked:
                processed_carry.add(operation_id)
            continue
        if item is None and all_item is not None and all_item.get("cost_credits") is not None:
            for operation_id in linked:
                processed_carry.add(operation_id)
            continue
        floor = (item.get("known_cost_floor_credits", 0) if item is not None
                 else old.get("known_cost_floor_credits", 0))
        if linked:
            operation = carry[linked[0]]
            value = max(operation["quote_credits"],
                        amount(operation.get("known_cost_floor_credits", 0), "carried floor"),
                        amount(floor, "known floor"))
            specific_unknown += value
            pending.add(linked[0])
            add_daily(linked[0], value, reserved=operation["reserved_at_utc"])
            processed_carry.add(linked[0])
        else:
            specific_unknown += amount(floor, "known floor")
            unknown_ids.append(identity)

    # Non-opening charges: exact reservation identity only.
    for item in items:
        identity = item["liability_id"]
        if identity in opening:
            continue
        links = item_ops.get(identity, [])
        if len(links) > 1:
            raise BudgetInputError("One charge links multiple standing reservations")
        if links:
            source, operation_id = links[0]
            operation = operations[operation_id] if source == "current" else carry[operation_id]
            known = item.get("cost_credits") is not None
            value = (max(amount(item["cost_credits"], "known cost"),
                         amount(item.get("known_cost_floor_credits", 0), "known floor"))
                     if known else max(operation["quote_credits"],
                                      amount(operation.get("known_cost_floor_credits", 0), "carried floor"),
                                      amount(item.get("known_cost_floor_credits", 0), "known floor")))
            if source == "carry":
                processed_carry.add(operation_id)
                if known:
                    post_actual += value
                    add_daily(identity, value, incurred=item.get("incurred_at_utc"),
                              reserved=operation["reserved_at_utc"])
                else:
                    specific_unknown += value
                    pending.add(operation_id)
                    add_daily(operation_id, value,
                              reserved=operation["reserved_at_utc"])
            elif known:
                post_actual += value
                add_daily(identity, value, incurred=item.get("incurred_at_utc"),
                          reserved=operation["reserved_at_utc"])
            else:
                post_actual += value
                pending.add(operation_id)
                add_daily(operation_id, value, reserved=operation["reserved_at_utc"])
        elif item.get("cost_credits") is None:
            blockers.append("unreserved_unknown_exposure:" + identity)
        else:
            value = max(amount(item["cost_credits"], "known cost"),
                        amount(item.get("known_cost_floor_credits", 0), "known floor"))
            post_actual += value
            add_daily(identity, value, incurred=item.get("incurred_at_utc"))

    # Reservations with no exact charge remain held at their quote.
    for operation_id, operation in operations.items():
        if current_matches[operation_id] is not None:
            continue
        post_actual += operation["quote_credits"]
        pending.add(operation_id)
        add_daily(operation_id, operation["quote_credits"],
                  reserved=operation["reserved_at_utc"])
    for operation_id, operation in carry.items():
        if operation_id in processed_carry:
            continue
        item = carry_matches[operation_id]
        if item is not None and item.get("cost_credits") is not None:
            continue
        value = max(operation["quote_credits"],
                    amount(operation.get("known_cost_floor_credits", 0), "carried floor"))
        specific_unknown += value
        pending.add(operation_id)
        add_daily(operation_id, value, reserved=operation["reserved_at_utc"])

    # Export identity is the exact attempt ID, never the shared execution ID.
    for source, rows in (("current", operations), ("carry", carry)):
        for operation_id, operation in rows.items():
            if operation["kind"] != "export" or not operation.get("execution_id"):
                continue
            exact = (current_matches.get(operation_id) if source == "current"
                     else carry_matches.get(operation_id))
            if exact is not None:
                continue
            mismatches = [item for item in all_items
                          if item.get("kind") == "export"
                          and item.get("execution_id") == operation["execution_id"]
                          and item.get("liability_id") != operation["liability_id"]
                          and item.get("cost_credits") is None]
            if mismatches:
                blockers.append("export_attempt_identity_mismatch:"
                                + operation_id + ":" + mismatches[0]["liability_id"])

    legacy_pool = amount(authority["legacy_unknown_risk_credits"], "legacy risk")
    old_risk = legacy_pool + specific_unknown
    known_model = book["opening_usage_credits"] + opening_known + old_risk + post_actual
    effective = max(book["usage_high_water_credits"], p["credits_used"], known_model)
    limit = min(amount(config["monthly_spend_guard_credits"], "guard"),
                amount(authority["period_hard_limit_credits"], "authorized hard limit"),
                p["credits_included"] - amount(
                    authority["minimum_unspent_quota_credits"], "minimum unspent"))
    target = min(limit, amount(config["monthly_target_credits"], "target"),
                 amount(authority["operating_target_credits"], "authorized operating target"))
    reserve = max(amount(config["minimum_credit_reserve"], "reserve"),
                  amount(authority["monthly_safety_reserve_credits"], "safety"))
    headroom = limit - effective - reserve
    cost = 0.0 if quote is None else amount(quote, "operation quote")
    if headroom < cost:
        blockers.append("monthly_risk_headroom_exhausted")
    if daily + cost > amount(authority["daily_risk_limit_credits"], "daily"):
        blockers.append("daily_risk_allowance_exhausted")
    future = amount(forecast_credits, "forecast") + amount(
        authority["forecast_margin_credits"], "forecast margin")
    # The approved operating target is a pacing goal; the separate hard ceiling
    # includes every conservative opening-known reservation. Do not multiply
    # cadence solely because those historical charges may already be in usage.
    # Nothing is marked included/settled: their full bases still fence admission.
    pacing_usage = max(book["usage_high_water_credits"], p["credits_used"],
                       book["opening_usage_credits"] + old_risk + post_actual)
    target_headroom = target - pacing_usage - reserve
    factor = max(1, math.ceil(future / max(1.0, target_headroom)))
    if headroom <= 0:
        factor = 8
    return {
        "status": "blocked" if blockers else "admitted",
        "blockers": blockers, "period_key": key, "credits_used": p["credits_used"],
        "limit_credits": limit, "effective_usage_credits": effective,
        "target_credits":target, "target_remaining_credits":max(0,target_headroom),
        "target_is_pacing_not_charge_inclusion":True,
        "remaining_credits": max(0, headroom), "minimum_reserve_credits": reserve,
        "opening_known_reserved_credits": opening_known,
        "opening_known_inclusion_claimed": False,
        "legacy_pool_credits": legacy_pool,
        "specific_unknown_risk_credits": specific_unknown,
        "legacy_unknown_risk_credits": old_risk,
        "legacy_unknown_liability_ids": sorted(unknown_ids),
        "pending_reserved_operation_ids": sorted(pending),
        "inherited_operation_ids": sorted(carry),
        "daily_committed_credits": daily,
        "forecast_credits": forecast_credits,
        "cadence_multiplier": min(8, factor),
        "liabilities_settled": False, "estimate_is_hard_cap": False,
        "period_start_utc": _z(p["period_start_utc"]),
        "period_end_utc": _z(p["period_end_utc"]),
    }


evaluate = _guard(_evaluate)


def _reserve(state, config, usage, now_utc, spec, params, kind, quote, *,
             operation_id, evidence, execution_id=None, forecast_credits=0.0):
    scoped = {**spec, "execution_id": execution_id}
    contract = _scope(config, scoped, params, kind, now_utc)
    risk = amount(quote, "operation quote")
    if risk <= 0 or risk > amount(contract["max_operation_risk_credits"], "operation risk"):
        raise BudgetInputError("Operation estimate exceeds its standing risk authority")
    operation_id = _text(operation_id, "operation identity")
    if not isinstance(evidence, dict) or not evidence:
        raise BudgetInputError("Credible estimate evidence is required")
    sql_sha256 = spec.get("verified_sql_sha256")
    if sql_sha256 is not None and (not isinstance(sql_sha256, str)
                                   or _SHA_RE.fullmatch(sql_sha256) is None):
        raise BudgetInputError("verified_sql_sha256 must be 64 lowercase hex")
    query_version = spec.get("verified_query_version")
    if query_version is not None:
        if (isinstance(query_version, bool)
                or not isinstance(query_version, (str, int))
                or not str(query_version).strip()):
            raise BudgetInputError("verified_query_version is invalid")
    absolute_day = spec.get("absolute_day")
    if absolute_day is not None:
        _day(absolute_day, "absolute_day")
    now = parse_instant(now_utc, "now")
    candidate = deepcopy(state)
    key, book, _ = _open_period(candidate, config, usage, now_utc)[:3]
    lane = candidate[LANE]
    seen = []
    for period_key, old in lane["periods"].items():
        if operation_id in old.get("operations", {}):
            seen.append((period_key, old["operations"][operation_id]))
        if operation_id in old.get("inherited_operations", {}):
            seen.append((period_key, old["inherited_operations"][operation_id]))
    if any(period_key != key for period_key, _ in seen):
        raise BudgetInputError("Operation ID is already used in another period")
    if seen:
        old = seen[0][1]
        if "approval" not in old:
            raise BudgetInputError("Operation ID is already carried; choose a new ID")
        expected = {
            "kind": kind,
            "query_id": spec["query_id"],
            "query_parameters": deepcopy(params or {}),
            "execution_id": execution_id,
            "quote_credits": risk,
            "evidence": evidence,
            "liability_id": ("submission:" + operation_id
                             if kind == "execution" else operation_id),
            "performance": spec.get("performance", config["performance"]),
            "shape_version": spec.get("shape_version"),
            "sql_sha256": sql_sha256,
            "query_version": query_version,
            "absolute_day": absolute_day,
        }
        if any(old.get(field) != value for field, value in expected.items()):
            raise BudgetInputError("Standing reservation cannot be changed or replayed as new work")
        if parse_instant(old["approval"].get("valid_until_utc"), "approval expiry") <= now:
            raise BudgetInputError("Standing approval has expired")
        return deepcopy(old["approval"])

    approval = {
        "operation_id": operation_id,
        "approval_ref": _policy(config, now_utc)["approval_ref"],
        "kind": kind, "query_id": spec["query_id"],
        "query_parameters": deepcopy(params or {}),
        "execution_id": execution_id,
        "performance": spec.get("performance", config["performance"]),
        "shape_version": spec.get("shape_version"),
        "quote_credits": risk,
        "estimate_evidence": json.dumps(evidence, sort_keys=True, allow_nan=False),
        "approved_at_utc": _z(now_utc),
        "valid_until_utc": min(
            parse_instant(book["period_end_utc"], "end"),
            now + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
        "financial_authority": False,
    }
    if sql_sha256 is not None:
        approval["sql_sha256"] = sql_sha256
    if query_version is not None:
        approval["query_version"] = query_version
    if absolute_day is not None:
        approval["absolute_day"] = absolute_day
    row = {
        "operation_id": operation_id, "kind": kind, "query_id": spec["query_id"],
        "query_parameters": deepcopy(params or {}), "execution_id": execution_id,
        "quote_credits": risk, "evidence": deepcopy(evidence),
        "reserved_at_utc": _z(now_utc), "performance": approval["performance"],
        "shape_version": approval["shape_version"], "approval": deepcopy(approval),
        "liability_id": "submission:" + operation_id if kind == "execution" else operation_id,
    }
    if sql_sha256 is not None:
        row["sql_sha256"] = sql_sha256
    if query_version is not None:
        row["query_version"] = query_version
    if absolute_day is not None:
        row["absolute_day"] = absolute_day
    pre = _evaluate(candidate, config, usage, now_utc, quote=risk,
                    forecast_credits=forecast_credits)
    if pre["status"] != "admitted":
        raise BudgetInputError("Standing admission blocked: " + "; ".join(pre["blockers"]))
    book["operations"][operation_id] = row
    post = _evaluate(candidate, config, usage, now_utc, forecast_credits=forecast_credits)
    if post["status"] != "admitted":
        raise BudgetInputError("Standing post-reservation decision blocked: "
                               + "; ".join(post["blockers"]))
    state[LANE] = candidate[LANE]
    state[LANE]["last_decision"] = post
    return deepcopy(approval)


reserve = _guard(_reserve)
