"""Deterministic operational health derived from journal and source evidence.

This is an internal report helper; it does not alter any public bundle schema.
"""
from datetime import datetime, timezone
import hashlib
import json


def _time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value else None


def _incident(domain, status, reason):
    payload = json.dumps([domain, status, reason], separators=(",", ":"))
    return "inc-" + hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_health(state, now, config, budget=None):
    """Return per-domain states using source as-of, not workflow/generated time.

    ``config['domains']`` maps domain -> ``source_key``, optional
    ``max_age_seconds`` and ``acceptance_timeout_seconds``. Journal records
    may contain a ``sources`` map, ``pending_executions`` map, and global pause
    or budget flags. A source record must provide ``source_asof`` (or
    ``data_as_of_utc``); a completed process timestamp alone is insufficient.
    """
    now = _time(now) if isinstance(now, str) else now
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime or ISO-8601 timestamp")
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(timezone.utc)
    state = state or {}
    config = config or {}
    sources = state.get("sources") or {}
    pending = state.get("pending_executions") or {}
    degraded = state.get("degraded_sources") or {}
    budget = budget or state.get("budget") or {}
    globally_paused = bool(state.get("paused") or state.get("automatic_retry") is False
                           and state.get("paused_at_utc"))
    output = {"evaluated_at_utc": _iso(now), "domains": {}}

    for domain, spec in (config.get("domains") or {}).items():
        spec = spec or {}
        key = spec.get("source_key", domain)
        source = sources.get(key) or {}
        process_at = (_time(source.get("last_process_check"))
                      or _time(source.get("completed_at_utc"))
                      or _time(source.get("last_refresh_completed_utc")))
        source_at = (_time(source.get("source_asof"))
                     or _time(source.get("data_as_of_utc"))
                     or _time(source.get("as_of_utc")))
        accepted_at = (_time(source.get("accepted_at"))
                       or _time(source.get("accepted_at_utc")))
        work = pending.get(key) or {}
        status, reason, action = "source_unavailable", "missing_source_evidence", "inspect_source_and_journal"

        if globally_paused or state.get("paused_due_to_cost"):
            status, reason, action = "paused", "refresh_paused", "resolve_pause_and_require_manual_clearance"
        elif budget.get("blocked") or budget.get("budget_blocked"):
            status, reason, action = "budget_blocked", "budget_guard_blocked", "review_budget_before_retry"
        elif key in degraded:
            status, reason, action = "degraded", "source_reported_degraded", "repair_source_then_verify"
        elif not source:
            status, reason, action = "source_unavailable", "source_record_missing", "restore_source_observation"
        elif work:
            if work.get("execution_id"):
                status, reason, action = "waiting_known", "execution_pending", "poll_existing_execution_no_resubmit"
            else:
                submitted = _time(work.get("submitted_at_utc"))
                timeout = int(spec.get("acceptance_timeout_seconds", 0) or 0)
                if submitted and timeout and (now - submitted).total_seconds() > timeout:
                    status, reason, action = "invalid", "acceptance_timeout_elapsed", "reconcile_submission_before_retry"
                else:
                    status, reason, action = "unknown_acceptance", "submission_acceptance_unconfirmed", "resolve_submission_before_retry"
        elif not source_at:
            status, reason, action = "invalid", "source_asof_missing_or_invalid", "obtain_source_timestamp"
        elif source_at > now:
            status, reason, action = "invalid", "source_asof_in_future", "validate_source_clock_and_payload"
        elif spec.get("max_age_seconds") is not None and (now - source_at).total_seconds() > float(spec["max_age_seconds"]):
            status, reason, action = "degraded", "source_stale", "refresh_source_and_check_integrity"
        elif source.get("status") in ("unavailable", "error", "failed"):
            status, reason, action = "source_unavailable", "source_status_unavailable", "restore_source_observation"
        else:
            status, reason, action = "fresh", "source_within_freshness_window", "none"

        fingerprint = None if status == "fresh" else _incident(domain, status, reason)
        output["domains"][domain] = {
            "status": status, "reason": reason,
            "last_process_check_utc": _iso(process_at),
            "source_asof_utc": _iso(source_at),
            "accepted_at_utc": _iso(accepted_at),
            "age_seconds": round((now - source_at).total_seconds(), 3) if source_at else None,
            "incident_id": fingerprint,
            "recovery_action": action,
        }
    return output
