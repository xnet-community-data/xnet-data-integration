"""Internal v1-compatible financial obligations and explicitly authorized allowances.

No I/O or authority discovery. Callers publish this optional journal lane with
the existing exact-predecessor checkpoint. Telemetry retention never trims it.
Consumption, elapsed time and numerical usage equality are NOT settlement.
Legacy exports lacking attempt identity/time remain conservatively unattributed.
"""
from copy import deepcopy
import hashlib
import json
import math

from v3_credit_budget import BudgetInputError, normalize_usage, parse_instant

LANE = "financial_journal"
EXPORT_STATUSES = {"requested", "in_progress", "unknown", "cached", "consumed"}
COMPUTE_TERMINAL_STATES = frozenset({
    "QUERY_STATE_COMPLETED", "QUERY_STATE_COMPLETED_PARTIAL", "QUERY_STATE_FAILED",
    "QUERY_STATE_CANCELED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED",
})


def amount(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise BudgetInputError(f"{label} must be a finite nonnegative number")
    return float(value)


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise BudgetInputError(f"{label} must be a nonempty string")
    return value


def _lane(state):
    if not isinstance(state, dict):
        raise BudgetInputError("financial state must be an object")
    lane = state.setdefault(LANE, {"schema_version": 1, "obligations": {}, "allowances": {}})
    if (not isinstance(lane, dict) or type(lane.get("schema_version")) is not int
            or lane["schema_version"] != 1
            or not isinstance(lane.get("obligations"), dict)
            or not isinstance(lane.get("allowances"), dict)):
        raise BudgetInputError("Malformed financial_journal; reviewed recovery required")
    for key, row in lane["obligations"].items():
        if not isinstance(row, dict) or row.get("liability_id") != key or row.get("kind") not in {"execution", "export"}:
            raise BudgetInputError("Malformed financial obligation identity")
        if row.get("cost_credits") is not None:
            amount(row["cost_credits"], "obligation cost")
        if row.get("known_cost_floor_credits") is not None:
            amount(row["known_cost_floor_credits"], "obligation known floor")
        if row.get("reserved_credits") is not None:
            amount(row["reserved_credits"], "obligation reservation")
    for identity, record in lane["allowances"].items():
        if (not isinstance(record, dict) or not isinstance(record.get("authority"), dict)
                or not isinstance(record.get("operations"), dict)
                or not isinstance(record.get("opening_obligations"), dict)):
            raise BudgetInputError("Malformed allowance record")
        authority = record["authority"]
        if authority.get("allowance_id") != identity:
            raise BudgetInputError("Allowance identity changed")
        for name in ("authorization_ref", "authorized_by"):
            _text(authority.get(name), name)
        for name in ("authorized_at_utc", "period_start_utc", "period_end_utc"):
            parse_instant(authority.get(name), name)
        for name in ("limit_credits", "reserve_credits", "target_buffer_credits", "opening_usage_credits"):
            amount(record.get(name), name)
        for value in record["opening_obligations"].values():
            amount(value, "opening obligation")
        for name in ("allowance_credits", "external_risk_reserve_credits"):
            amount(record["authority"].get(name), name)
        for operation in record["operations"].values():
            if not isinstance(operation, dict):
                raise BudgetInputError("Malformed allowance operation")
            amount(operation.get("reserved_credits"), "reservation")
            amount(operation.get("observed_credits", 0), "observed debit")
            _text(operation.get("liability_id"), "reserved liability id")
    return lane


def _put(lane, row):
    previous = lane["obligations"].get(row["liability_id"], {})
    for field in ("kind", "execution_id", "query_id", "query_parameters", "requested_at_utc", "submission_id", "cache_marker_key", "financial_history_id"):
        if previous.get(field) is not None and row.get(field) is not None and previous[field] != row[field]:
            raise BudgetInputError(f"Financial obligation {field} changed")
    for field in ("incurred_at_utc", "submitted_at_utc"):
        old_time, new_time = previous.get(field), row.get(field)
        if old_time is not None and new_time is not None and old_time != new_time:
            try:
                same_instant = parse_instant(old_time, "original operation time") == parse_instant(new_time, "operation time")
            except BudgetInputError:
                same_instant = False
            if not same_instant:
                raise BudgetInputError(f"Financial obligation {field} changed; reviewed attribution required")
            row = {**row, field: old_time}
    old, new = previous.get("cost_credits"), row.get("cost_credits")
    merged = {**previous, **row}
    for field in ("execution_id", "query_id", "query_parameters", "submission_id", "incurred_at_utc", "submitted_at_utc", "financial_history_id"):
        if merged.get(field) is None and previous.get(field) is not None:
            merged[field] = previous[field]
    if old is not None:
        merged["cost_credits"] = max(old, new) if new is not None else old
    for field in ("known_cost_floor_credits", "reserved_credits"):
        if previous.get(field) is not None:
            merged[field] = max(previous[field], row[field]) if row.get(field) is not None else previous[field]
    lane["obligations"][row["liability_id"]] = merged


def export_attempt_id(key, requested_at_utc):
    """A second read of one execution is a second possible charge, not a duplicate."""
    digest = hashlib.sha256(str(requested_at_utc).encode()).hexdigest()[:16]
    return f"result_export:{key}:{digest}"


def synchronize_financial_journal(state, export_rate=10.0):
    """Atomically stage migration, preserving caller-held history references.

    Read-only adapters call this on a copy. Only the existing publication
    boundary makes these identities durable; a diagnostic read is not a write.
    """
    if not isinstance(state, dict):
        raise BudgetInputError("financial state must be an object")
    candidate = deepcopy(state)
    _synchronize(candidate, export_rate)
    changed = state.get(LANE) != candidate[LANE]
    for original, migrated in zip(state.get("execution_history", []), candidate.get("execution_history", [])):
        if original != migrated:
            changed = True
            original.clear()
            original.update(migrated)
    state[LANE] = candidate[LANE]
    return changed


def _history_identity(row, occurrences, used_ids):
    """Stable local migration identity, NOT proof of provider execution identity.

    Anonymous evidence includes cost in its initial fingerprint: a different
    priced observation cannot resolve an old unknown by reusing its list slot.
    Identical rows get distinct occurrence tokens. Tokens persist before trim;
    already-published positional liabilities are preserved, never auto-aliased.
    """
    token = row.get("financial_history_id")
    if token is not None:
        return _text(token, "financial history identity")
    evidence = {k: v for k, v in row.items() if k not in {"financial_history_id", "export_attempt_id"}}
    try:
        fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                                                allow_nan=False).encode()).hexdigest()
    except (ValueError, TypeError) as error:
        raise BudgetInputError("Unserializable legacy history evidence") from error
    occurrence = occurrences.get(fingerprint, 0)
    while f"{fingerprint}:{occurrence}" in used_ids:
        occurrence += 1
    occurrences[fingerprint] = occurrence + 1
    token = f"{fingerprint}:{occurrence}"
    used_ids.add(token)
    row["financial_history_id"] = token
    return token


def _synchronize(state, export_rate=10.0):
    """Import readable v1 evidence idempotently, BEFORE any telemetry trim.

    No automatic deletion/settlement. Unknown costs survive even if their
    original history/pending record disappears. A known ID can resolve its
    own submission intent; only proven nonacceptance may zero that intent.
    """
    lane = _lane(state)
    rate = amount(export_rate, "export rate")
    history = state.get("execution_history", [])
    pending = state.get("pending_executions", {})
    if not isinstance(history, list) or not isinstance(pending, dict):
        raise BudgetInputError("Malformed execution history/pending lane")
    rows = list(history)
    for key, work in pending.items():
        if not isinstance(work, dict):
            raise BudgetInputError("Malformed pending execution")
        intent = "submission:" + work["financial_attempt_id"] if work.get("financial_attempt_id") else f"pending:{key}"
        if work.get("execution_id"):
            # The original intent and provider ID are checkpointed together.
            original_intent = lane["obligations"].get(intent, {})
            if original_intent.get("basis") == "proven_nonacceptance":
                raise BudgetInputError("Provider identity contradicts recorded nonacceptance; review required")
            status = work.get("terminal_status") or {}
            if not isinstance(status, dict):
                raise BudgetInputError("Malformed pending terminal status")
            observed = {**work, **status}
            pending_cost = work.get("execution_cost_credits")
            floor = max(original_intent.get("known_cost_floor_credits", 0),
                        amount(0 if pending_cost is None else pending_cost, "pending cost floor"))
            if status.get("state") not in COMPUTE_TERMINAL_STATES:
                if status.get("execution_cost_credits") is not None:
                    floor = max(floor, amount(status["execution_cost_credits"], "nonterminal cost floor"))
                observed["execution_cost_credits"] = None
                observed["execution_ended_at"] = None
            if floor:
                observed["known_cost_floor_credits"] = floor
            lane["obligations"].pop(intent, None)
            rows.append(observed)
        else:
            unresolved = {"liability_id": intent, "kind": "execution", "cost_credits": None,
                          "incurred_at_utc": work.get("submitted_at_utc"),
                          "submitted_at_utc": work.get("submitted_at_utc"),
                          "query_id": work.get("query_id"), "query_parameters": work.get("query_parameters")}
            if work.get("execution_cost_credits") is not None:
                unresolved["known_cost_floor_credits"] = amount(work["execution_cost_credits"], "unidentified cost floor")
            _put(lane, unresolved)
    occurrences = {}
    used_ids = {row["financial_history_id"] for row in rows
                if isinstance(row, dict) and isinstance(row.get("financial_history_id"), str)}
    used_ids.update(row["financial_history_id"] for row in lane["obligations"].values()
                    if isinstance(row.get("financial_history_id"), str))
    for row in rows:
        if not isinstance(row, dict):
            raise BudgetInputError("Malformed execution history row")
        execution_id = row.get("execution_id")
        identifier = f"exec:{execution_id}" if execution_id else "exec:legacy:" + _history_identity(row, occurrences, used_ids)
        cost = row.get("execution_cost_credits")
        if cost is not None:
            cost = amount(cost, "execution cost")
        compute = {"liability_id": identifier, "kind": "execution", "execution_id": execution_id,
                    "query_id": row.get("query_id"), "query_parameters": row.get("query_parameters"),
                    "cost_credits": cost, "unresolved": cost is None or not execution_id,
                    "submitted_at_utc": row.get("submitted_at_utc"),
                    "submission_id": "submission:" + row["financial_attempt_id"] if row.get("financial_attempt_id") else None,
                    # Submission time alone cannot place a straddling computation.
                    "incurred_at_utc": row.get("execution_ended_at")}
        if row.get("known_cost_floor_credits") is not None:
            compute["known_cost_floor_credits"] = amount(row["known_cost_floor_credits"], "compute cost floor")
        if not execution_id:
            compute.update(unresolved=True, identity_basis="local_migration_not_provider_identity",
                           financial_history_id=row["financial_history_id"])
        _put(lane, compute)
        # Current callers explicitly distinguish spend-free cache reuse. Without
        # an original attempt link, points/old copied telemetry do not evidence a
        # NEW read. Existing marker/legacy obligations remain separately held.
        if row.get("export_reused") is True and not row.get("export_attempt_id"):
            continue
        # Result metadata/row counts/byte counts are NOT evidence of a paid read.
        metered = row.get("estimated_export_credits") is not None or row.get("export_wire_bytes") is not None
        attempted = metered or bool(row.get("export_points")) or bool(row.get("export_attempt_id"))
        if not attempted:
            continue
        export_cost = row.get("estimated_export_credits")
        if export_cost is None and row.get("export_wire_bytes") is not None:
            export_cost = amount(row["export_wire_bytes"], "export wire bytes") / 1_000_000 * rate
        if export_cost is not None:
            export_cost = amount(export_cost, "export cost")
        if not row.get("export_attempt_id"):
            row["export_attempt_id"] = f"legacy_export:{identifier}:" + _history_identity(row, occurrences, used_ids)
        _put(lane, {"liability_id": row["export_attempt_id"],
                    "kind": "export", "execution_id": execution_id, "cost_credits": export_cost,
                    "financial_history_id": row.get("financial_history_id"),
                    "incurred_at_utc": row.get("export_requested_at_utc"),
                    "basis": "metered_legacy" if metered else "unmetered_legacy"})
    exports = state.get("result_exports")
    if exports is not None:
        if not isinstance(exports, dict) or not isinstance(exports.get("requests"), dict):
            raise BudgetInputError("Malformed result_exports requests")
        for key, marker in exports["requests"].items():
            if not isinstance(marker, dict) or marker.get("status") not in EXPORT_STATUSES:
                raise BudgetInputError("Malformed result export marker")
            requested = marker.get("requested_at_utc")
            unresolved = marker["status"] in {"requested", "in_progress", "unknown"}
            risk = marker.get("risk_credits")
            if risk is not None:
                risk = amount(risk, "export risk")
            linked = [row["liability_id"] for row in lane["obligations"].values()
                      if row.get("cache_marker_key") == key and row.get("requested_at_utc") == requested]
            if len(linked) > 1:
                raise BudgetInputError("Ambiguous export marker/attempt linkage")
            identifier = linked[0] if linked else export_attempt_id(key, requested)
            prior = lane["obligations"].get(identifier, {})
            observed = prior.get("cost_credits")
            metered = prior.get("charge_observed") is True and observed is not None
            _put(lane, {"liability_id": identifier, "kind": "export",
                        "execution_id": marker.get("execution_id"), "requested_at_utc": requested,
                        "incurred_at_utc": requested, "cost_credits": None,
                        "reserved_credits": risk,
                         "unresolved": False if metered else unresolved or observed is None,
                         "cache_status": marker["status"],
                         "basis": "observed_charge_not_inclusion" if metered else "reservation_not_observed_charge"})


def financial_obligations(state, *, period_start_utc=None, export_rate=10.0):
    """Read-only snapshot. Unknown or undated exposure is NEVER aged away."""
    snapshot = deepcopy(state)
    synchronize_financial_journal(snapshot, export_rate)
    start = parse_instant(period_start_utc, "period start") if period_start_utc is not None else None
    items, excluded = [], []
    for row in snapshot[LANE]["obligations"].values():
        item = deepcopy(row)
        floor = item.get("known_cost_floor_credits", 0)
        if item.get("unresolved") or (item.get("cost_credits") is not None and floor > item["cost_credits"]):
            if item.get("cost_credits") is not None:
                item["known_cost_floor_credits"] = max(item["cost_credits"], item.get("known_cost_floor_credits", 0))
            item.pop("cost_credits", None)
        elif item.get("cost_credits") is not None:
            # A final execution-bound cost covers its earlier partial reading;
            # retain that floor in the durable audit, not as a false unknown.
            item.pop("known_cost_floor_credits", None)
        when = item.get("incurred_at_utc")
        try:
            instant = parse_instant(when, "obligation timestamp") if when else None
        except BudgetInputError:
            instant = None
        # Exclusion is attribution only: the durable record is never removed.
        if start and instant and instant < start and item.get("cost_credits") is not None:
            excluded.append(item["liability_id"])
        else:
            items.append(item)
    return items, excluded


def reject_submission(state, pending):
    """Zero only a real, unpromoted intent after proven nonacceptance.

    A caller's assertion cannot reverse a known provider ID/charge. Contradictory
    evidence requires reviewed reconciliation, not an invented settlement flag.
    Failure is typed and leaves the original journal untouched.
    """
    if not isinstance(pending, dict):
        raise BudgetInputError("Rejection requires original submission identity")
    candidate = deepcopy(state)
    synchronize_financial_journal(candidate)
    attempt = pending.get("financial_attempt_id")
    key = "submission:" + _text(attempt, "attempt identity") if attempt else "pending:" + _text(pending.get("key"), "pending key")
    authoritative = candidate.get("pending_executions", {}).get(pending.get("key"), {})
    if attempt and authoritative.get("financial_attempt_id") not in (None, attempt):
        raise BudgetInputError("Rejection does not match authoritative pending attempt")
    if authoritative:
        authoritative_key = ("submission:" + authoritative["financial_attempt_id"]
                             if authoritative.get("financial_attempt_id") else "pending:" + pending["key"])
        if authoritative_key != key:
            raise BudgetInputError("Rejection does not identify the authoritative pending intent")
    rows = candidate[LANE]["obligations"]
    if (pending.get("execution_id") or authoritative.get("execution_id")
            or any(row.get("submission_id") == key for row in rows.values())):
        raise BudgetInputError("Submission already has provider identity; reviewed reconciliation required")
    row = rows.get(key)
    if row is None or row.get("kind") != "execution":
        raise BudgetInputError("No matching unpromoted submission intent")
    if row.get("cost_credits") not in (None, 0.0) or row.get("known_cost_floor_credits", 0) > 0:
        raise BudgetInputError("Observed charge contradicts nonacceptance; reviewed reconciliation required")
    row.update(cost_credits=0.0, unresolved=False, basis="proven_nonacceptance")
    state[LANE] = candidate[LANE]


def record_export_attempt(state, attempt_id, *, execution_id, requested_at_utc,
                          risk_credits, estimate_evidence, cache_marker_key=None):
    """Stage BEFORE a paid read; caller must durably publish before fetching.

    Reusing the same execution does not reuse an attempt ID for another read.
    An estimate reserves risk; it does not make an interrupted read's bill known.
    """
    _text(attempt_id, "attempt id")
    _text(execution_id, "execution id")
    parse_instant(requested_at_utc, "export requested at")
    _text(estimate_evidence, "estimate evidence")
    if cache_marker_key is not None:
        _text(cache_marker_key, "cache marker key")
    lane = _lane(state)
    if attempt_id in lane["obligations"]:
        raise BudgetInputError("Export attempt already exists; reconcile, never repeat the read")
    _put(lane, {"liability_id": attempt_id, "kind": "export", "execution_id": execution_id,
                "requested_at_utc": requested_at_utc, "incurred_at_utc": requested_at_utc,
                "cost_credits": None, "unresolved": True,
                "reserved_credits": amount(risk_credits, "export risk"),
                "estimate_evidence": estimate_evidence, "cache_marker_key": cache_marker_key})


def record_export_outcome(state, attempt_id, *, observed_credits, completed_transport_observed_at_utc=None):
    """Stage metered outcome of that exact attempt; never infer billing inclusion."""
    lane = _lane(state)
    row = lane["obligations"].get(attempt_id)
    if not row or row["kind"] != "export":
        raise BudgetInputError("No matching export attempt")
    transport = {}
    if completed_transport_observed_at_utc is not None:
        parse_instant(completed_transport_observed_at_utc, "completed transport observed at")
        transport["completed_transport_observed_at_utc"] = completed_transport_observed_at_utc
    _put(lane, {**row, "cost_credits": amount(observed_credits, "observed export credits"),
                "unresolved": False, "charge_observed": True,
                "basis": "observed_charge_not_inclusion", **transport})


def open_allowance(state, authority, usage, now_utc, *, guard_credits,
                   minimum_reserve_credits, monthly_target_credits,
                   minimum_headroom_below_target=0.0):
    """Install an EXPLICIT external authorization, never discover/create authority.

    Caller must verify the user's authorization_ref out of band. This is an
    internal risk allocation, NOT proof of billing inclusion or a provider cap.
    Unknown opening obligations refuse allocation. Publish before use.
    """
    target = amount(monthly_target_credits, "monthly target")
    candidate = deepcopy(state)
    synchronize_financial_journal(candidate)
    lane = _lane(candidate)
    if not isinstance(authority, dict):
        raise BudgetInputError("Explicit allowance authority is required")
    for name in ("allowance_id", "authorization_ref", "authorized_by", "authorized_at_utc"):
        _text(authority.get(name), name)
    now = parse_instant(now_utc, "now")
    if parse_instant(authority["authorized_at_utc"], "authorized at") > now:
        raise BudgetInputError("Allowance authorization is in the future")
    selection = normalize_usage(usage, now)
    period = selection["period"]
    if selection["blockers"] or not period or not period["period_start_utc"] or not period["period_end_utc"]:
        raise BudgetInputError("Allowance requires an exact active period")
    start, end = period["period_start_utc"], period["period_end_utc"]
    if not start <= now < end or any(parse_instant(authority.get(k), k) != v for k, v in (
            ("period_start_utc", start), ("period_end_utc", end))):
        raise BudgetInputError("Allowance period does not match active billing period")
    identity = authority["allowance_id"]
    if identity in lane["allowances"]:
        if lane["allowances"][identity]["authority"] != authority:
            raise BudgetInputError("Allowance authority is immutable")
        return deepcopy(lane["allowances"][identity])
    if any(parse_instant(a["authority"]["period_start_utc"], "allocated period start") == start
           for a in lane["allowances"].values()):
        raise BudgetInputError("Period already allocated; no automatic replenishment")
    items, _ = financial_obligations(candidate, period_start_utc=start)
    if any(item.get("cost_credits") is None for item in items):
        raise BudgetInputError("Unknown opening exposure cannot be allocated away")
    opening = {item["liability_id"]: max(item["cost_credits"], item.get("reserved_credits") or 0)
               for item in items}
    limit = min(amount(guard_credits, "guard"), period["credits_included"], target)
    reserve = amount(minimum_reserve_credits, "reserve")
    buffer = amount(minimum_headroom_below_target, "target buffer")
    external = amount(authority.get("external_risk_reserve_credits"), "external risk reserve")
    allocation = amount(authority.get("allowance_credits"), "allowance credits")
    opening_book = period["credits_used"] + sum(opening.values())
    if opening_book + allocation + reserve + buffer + external > limit:
        raise BudgetInputError("Allowance exceeds conservative opening headroom")
    record = {"authority": deepcopy(authority), "opened_at_utc": now.isoformat(),
              "opening_usage_credits": period["credits_used"], "opening_obligations": opening,
              "limit_credits": limit, "reserve_credits": reserve, "target_buffer_credits": buffer,
              "operations": {}}
    lane["allowances"][identity] = record
    state[LANE] = lane
    return deepcopy(record)


def _reservation_covers(operation_id, operation, record, item, now):
    """Validate this allowance's original reservation, not a generic risk label.

    Trust boundary: the caller verifies approval_ref and estimate_evidence before
    reserve_allowance. Reading a marker, post-run cap or another allowance's
    reservation never supplies that authority. Missing legacy timing stays held.
    """
    approval = operation.get("approval")
    if not isinstance(approval, dict):
        return False
    try:
        if (approval.get("operation_id") != operation_id
                or amount(approval.get("quote_credits"), "approved quote") != operation["reserved_credits"]):
            return False
        _text(approval.get("approval_ref"), "approval ref")
        _text(approval.get("estimate_evidence"), "estimate evidence")
        reserved = parse_instant(operation.get("reserved_at_utc"), "reserved at")
        submitted = parse_instant(item.get("submitted_at_utc") if item["kind"] == "execution"
                                  else item.get("requested_at_utc"), "operation start")
        start = parse_instant(record["authority"]["period_start_utc"], "period start")
        end = parse_instant(record["authority"]["period_end_utc"], "period end")
        opened = parse_instant(record["opened_at_utc"], "allowance opened")
        return start <= opened <= reserved <= submitted <= now < end
    except (BudgetInputError, KeyError):
        return False


def evaluate_allowance(state, allowance_id, usage, now_utc, *, guard_credits,
                       minimum_reserve_credits, monthly_target_credits,
                       minimum_headroom_below_target=0.0):
    """Risk affordability, NOT settlement, a hard cost bound, or execution authority.

    An exact prior approved reservation can carry this operation's still-unknown
    actual charge. Uncovered legacy unknowns block. Lifecycle barriers (including
    ambiguous execute IDs) and the next operation's approval remain independent.
    """
    target = amount(monthly_target_credits, "current monthly target")
    lane = _lane(deepcopy(state))
    if allowance_id not in lane["allowances"]:
        raise BudgetInputError("No explicitly authorized allowance")
    record = lane["allowances"][allowance_id]
    authority = record["authority"]
    limit = min(record["limit_credits"], amount(guard_credits, "current guard"), target)
    reserve = max(record["reserve_credits"], amount(minimum_reserve_credits, "current reserve"))
    buffer = max(record["target_buffer_credits"], amount(minimum_headroom_below_target, "current target buffer"))
    period = normalize_usage(usage, now_utc)
    blockers = list(period["blockers"])
    p = period["period"]
    start, end = (parse_instant(authority[k], k) for k in ("period_start_utc", "period_end_utc"))
    if (not p or p["period_start_utc"] != start or p["period_end_utc"] != end
            or not start <= parse_instant(now_utc, "now") < end):
        blockers.append("allowance_period_mismatch")
    now = parse_instant(now_utc, "now")
    bases = {key: max(amount(op["reserved_credits"], "reservation"),
                      amount(op.get("observed_credits", 0), "observed debit"))
             for key, op in record["operations"].items()}
    items, _ = financial_obligations(state, period_start_utc=start)
    matches = {item["liability_id"]: [key for key, op in record["operations"].items()
               if op["liability_id"] in {item["liability_id"], item.get("submission_id")}]
               for item in items}
    match_counts = {key: sum(key in ids for ids in matches.values()) for key in bases}
    linked_costs = {key: 0.0 for key in bases}
    unlinked_cost = 0.0
    unsettled = []
    opening = record["opening_obligations"]
    for item in items:
        identifier, cost = item["liability_id"], item.get("cost_credits")
        operation_ids = matches[identifier]
        operation_id = operation_ids[0] if len(operation_ids) == 1 else None
        if cost is None:
            cost = item.get("known_cost_floor_credits", 0.0)
            covered = (operation_id is not None and match_counts[operation_id] == 1
                       and identifier not in opening
                       and _reservation_covers(operation_id, record["operations"][operation_id], record, item, now))
            if not covered:
                blockers.append("unknown_exposure:" + identifier)
            unsettled.append({"liability_id": identifier, "operation_id": operation_id,
                              "coverage": "approved_operation_reservation" if covered else "uncovered_unknown",
                              "known_cost_floor_credits": cost,
                              "reserved_risk_credits": bases.get(operation_id, 0),
                              "modeled_risk_credits": max(cost, bases.get(operation_id, 0)) if covered else None,
                              "estimate_is_hard_cap": False})
        if operation_id is not None:
            linked_costs[operation_id] += cost
        else:
            unlinked_cost += max(0.0, cost - opening.get(identifier, 0.0))
    committed = sum(max(base, linked_costs[key]) for key, base in bases.items()) + unlinked_cost
    remaining = amount(authority["allowance_credits"], "allowance") - committed
    if remaining < 0:
        blockers.append("allowance_exhausted")
    if p:
        effective_limit = min(limit, p["credits_included"])
        if p["credits_used"] + reserve + buffer >= effective_limit:
            blockers.append("account_usage_stop")
        expected = record["opening_usage_credits"] + sum(opening.values()) + committed
        remaining = min(remaining, effective_limit - expected - reserve - buffer
                        - authority["external_risk_reserve_credits"])
        if remaining < 0 and "allowance_exhausted" not in blockers:
            blockers.append("current_policy_headroom_exhausted")
        if p["credits_used"] > expected + authority["external_risk_reserve_credits"]:
            blockers.append("account_usage_drift")
    return {"status": "blocked" if blockers else "affordable", "blockers": blockers,
            "committed_credits": committed, "remaining_credits": max(0.0, remaining),
            "unsettled_obligations": unsettled, "exposure_is_fully_known": not unsettled,
            "risk_basis": "approved_reservations_and_observed_floors_not_a_guarantee",
            "authorization_required": True, "estimate_is_hard_cap": False,
            "execution_lifecycle_barriers_not_cleared": True}


def reserve_allowance(state, allowance_id, operation_id, *, quote_credits, approval,
                      liability_id, usage, now_utc, guard_credits,
                      minimum_reserve_credits, monthly_target_credits,
                      minimum_headroom_below_target=0.0):
    """Irrevocable debit/reservation. Approval must bind this exact operation/quote.

    Caller's trusted authorization boundary verifies approval_ref. Replays must
    match; lower observed costs, canceled work, or usage decreases never refund.
    """
    target = amount(monthly_target_credits, "current monthly target")
    quote = amount(quote_credits, "quote")
    _text(operation_id, "operation id")
    _text(liability_id, "liability id")
    if (not isinstance(approval, dict) or approval.get("operation_id") != operation_id
            or amount(approval.get("quote_credits"), "approved quote") != quote):
        raise BudgetInputError("Explicit approval must bind operation and credible quote")
    _text(approval.get("approval_ref"), "approval ref")
    _text(approval.get("estimate_evidence"), "estimate evidence")
    candidate = deepcopy(state)
    lane = _lane(candidate)
    policy = {"guard_credits": guard_credits, "minimum_reserve_credits": minimum_reserve_credits,
              "monthly_target_credits": monthly_target_credits,
              "minimum_headroom_below_target": minimum_headroom_below_target}
    report = evaluate_allowance(candidate, allowance_id, usage, now_utc, **policy)
    operations = lane["allowances"][allowance_id]["operations"]
    op = {"reserved_credits": quote, "approval": deepcopy(approval), "liability_id": liability_id}
    if operation_id in operations:
        previous = {k: v for k, v in operations[operation_id].items()
                    if k not in {"observed_credits", "reserved_at_utc"}}
        if previous != op:
            raise BudgetInputError("Operation approval/reservation is immutable")
        return report
    if report["status"] != "affordable" or quote > report["remaining_credits"]:
        raise BudgetInputError("Allowance cannot admit this reservation")
    record = lane["allowances"][allowance_id]
    period = normalize_usage(usage, now_utc)["period"]
    account_headroom = (min(record["limit_credits"], period["credits_included"], amount(guard_credits, "current guard"),
                           target)
                        - period["credits_used"] - max(record["reserve_credits"], minimum_reserve_credits)
                        - max(record["target_buffer_credits"], minimum_headroom_below_target))
    if quote > account_headroom:
        raise BudgetInputError("Account usage leaves insufficient headroom for reservation")
    if any(x["liability_id"] == liability_id for x in operations.values()):
        raise BudgetInputError("Liability already reserved under another operation")
    operations[operation_id] = {**op, "reserved_at_utc": parse_instant(now_utc, "reserved at").isoformat()}
    state[LANE] = lane
    return evaluate_allowance(state, allowance_id, usage, now_utc, **policy)
