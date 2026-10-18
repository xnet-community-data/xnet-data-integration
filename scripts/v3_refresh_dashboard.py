#!/usr/bin/env python3
"""Bounded sources, status-only chart refreshes, and a persistent spend guard."""
from __future__ import annotations
import argparse
from copy import deepcopy
import csv
import json
import hashlib
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from v3_execution_tracker import (
    ExecutionPending, ExecutionCancelled, ExecutionTracker, SubmissionRejected,
    ExecutionCostExceeded, ExecutionFailed,
    CheckpointFailed,
)
from v3_live_state_store import LiveStateStore, StateError, atomic_write, encoded, validate_journal
from v3_budget_runtime import evaluate_budget
from v3_operational_health import build_health
from v3_result_cache import ResultCache, ManualExportReview, CacheError
from v3_credit_budget import group_samples, sample_estimate
from v3_budget_runtime import collect_sample_history

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "config/v3_refresh.json").read_text())
STATE = ROOT / "data/current/v3_refresh_state.json"
HEALTH = ROOT / "data/current/xnet_chain_health.json"

def now():
    return datetime.now(timezone.utc)

def stamp():
    return now().isoformat(timespec="seconds").replace("+00:00", "Z")

def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default

def save(path, value):
    try:
        atomic_write(path, encoded(value))
    except (OSError, ValueError) as error:
        raise CheckpointFailed(f"Unable to durably save {path.name}: {error}") from error


def load_authoritative_state():
    return LiveStateStore(ROOT).load_authoritative_journal()

def api(path, payload=None, include_wire_size=False):
    key = os.environ.get("DUNE_API_KEY")
    if not key:
        raise RuntimeError("DUNE_API_KEY is not configured; no executions submitted.")
    request = urllib.request.Request("https://api.dune.com/api/v1/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-Dune-Api-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read()
            value = json.loads(body)
            return (value, len(body)) if include_wire_size else value
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read())
            message = str(body.get("error") or body.get("message") or "")[:400].replace(key, "[redacted]")
        except Exception:
            message = ""
        detail = f"Dune HTTP {error.code} at {path.split('?')[0]}: {message}; no automatic execution retry."
        # HTTP status alone is not authoritative evidence of nonacceptance.
        # Proxies/backends may lose an accepted response even for 4xx/408/429.
        # Retain the durable intent for every execute HTTP failure. Only a future
        # provider-specific, proven nonacceptance contract may raise SubmissionRejected.
        raise RuntimeError(detail) from None

class BudgetBlocked(RuntimeError):
    """New spending is not admitted; metadata reconciliation may continue."""


def usage_guard(state=None, proposed_spec=None, proposed_params=None):
    if state is None:
        state = load_authoritative_state()
    usage = api("usage", {"start_date": (now().date() - timedelta(days=40)).isoformat(),
                          "end_date": (now().date() + timedelta(days=1)).isoformat()})
    decision = evaluate_budget(state, CONFIG, usage, now(),
                               proposed_spec=proposed_spec, proposed_params=proposed_params)
    state["budget_decision"] = decision
    report = decision["report"]
    if decision["status"] != "admitted":
        raise BudgetBlocked("Dune credit admission blocked: " + "; ".join(decision["blockers"]))
    current, totals = report["current_usage"], report["totals"]
    return {
        "credits_used": current["credits_used"], "guard_credits": totals["limit_credits"],
        "target_credits": CONFIG["monthly_target_credits"],
        "projected_period_credits": current["credits_used"] + totals["dependencies_total"],
        "period_start": current["period_start_utc"], "period_end": current["period_end_utc"],
        "forecast": report["forecast"],
    }

def checkpoint(state):
    # Only Dune execution state is published here. Revenue/feed builders and
    # dashboard-data publication are deliberately excluded.
    write_operational_health(state)
    save(STATE, state)
    return publish(state_only=True)


def write_operational_health(state):
    """Internal-only provenance and stable incident identities; no public fields added."""
    specs = [*CONFIG.get("sources", []), *CONFIG.get("presentation", [])]
    config = {"domains": {s["key"]: {"source_key": s["key"],
               "max_age_seconds": int(s.get("cadence_minutes", 30)) * 120}
               for s in specs if s.get("enabled", True)}}
    report = build_health({**state, "sources": state.get("queries", {}),
                           "degraded_sources": state.get("source_errors", {})}, now(), config,
                          {"blocked": state.get("budget_decision", {}).get("status") == "blocked"})
    previous = state.get("operational_incidents", {})
    incidents = {key: item["incident_id"] for key, item in report["domains"].items()
                 if item["incident_id"]}
    for key, identity in incidents.items():
        if previous.get(key) != identity:
            item = report["domains"][key]
            print(f"::warning::XNET {key}: {item['status']} [{identity}]; {item['recovery_action']}", flush=True)
    state["operational_incidents"] = incidents
    save(ROOT / "data/current/v3_operational_health.json", report)

def tracker(state, proposed_spec=None, proposed_params=None):
    return ExecutionTracker(state, api, lambda: checkpoint(state),
                            lambda: usage_guard(state, proposed_spec, proposed_params),
                            now, time.sleep, CONFIG["performance"])

def execute(spec, params=None, enforce_cap=True, timeout_seconds=None, state=None):
    if state is None:
        state = load_authoritative_state()
    if timeout_seconds is None:
        timeout_seconds = CONFIG.get("source_execution_timeout_seconds", 240)
    return tracker(state, spec, params).execute(spec, params, enforce_cap, timeout_seconds)


def result_cache():
    public_ids = {int(s["query_id"]) for s in CONFIG.get("sources", [])}
    if CONFIG.get("reconciliation"):
        public_ids.add(int(CONFIG["reconciliation"]["manifest_query_id"]))
    return ResultCache(ROOT, checkpoint, allowed_query_ids=public_ids)


def export_risk(state, record, spec, result_bytes):
    """Comparable wire-size envelope, not a configured download/spend ceiling."""
    samples, _ = collect_sample_history(state, default_performance=CONFIG["performance"],
                                       performance_by_key={})
    estimate = sample_estimate(group_samples(samples, CONFIG["performance"]),
        record["query_id"], record.get("query_parameters", {}), now_utc=now(),
        performance=spec.get("performance", CONFIG["performance"]),
        export_rate_per_mb=CONFIG["export_credits_per_megabyte"])
    evidence = estimate["evidence"]
    values = evidence["export_cost_values"]
    if len(values) < 3 or result_bytes > (evidence["export_bytes_max"] or 0):
        raise ManualExportReview("Export size/cost lacks three comparable recent wire observations; explicit review required.")
    return max(max(values), statistics.mean(values) + 3 * statistics.pstdev(values)), [
        "Comparable query/parameters/engine; >=3 recent observed wire charges; max(mean+3sigma, observed max)"]


def export_source(record, spec, remaining, max_bytes=None, state=None):
    meta = record["result_metadata"]
    if "column_names" not in meta or "total_row_count" not in meta:
        raise RuntimeError("Missing complete result metadata; source export stopped.")
    def count(value):
        if type(value) is not int or value < 0:
            raise RuntimeError("Invalid nonnegative integer source result metadata")
        return value
    rows = count(meta["total_row_count"])
    if not isinstance(meta["column_names"], list):
        raise RuntimeError("Invalid source result columns")
    points = max(count(meta.get("datapoint_count", rows * len(meta["column_names"]))),
                 rows * len(meta["column_names"]))
    result_bytes = count(meta.get("total_result_set_bytes", meta.get("result_set_bytes", 0)))
    if max_bytes is None:
        max_bytes = CONFIG["max_export_bytes_per_run"]
    if result_bytes > max_bytes:
        raise RuntimeError("Source result exceeds export byte allowance; manual review required.")
    if points > remaining or rows > 10000:
        raise RuntimeError(f"Source export exceeds bounded allowance ({points} points); manual review required.")
    if rows == 0:
        result = {"state": "QUERY_STATE_COMPLETED", "result": {"rows": [], "metadata": meta}}
        wire_bytes = 0
    else:
        if state is None:
            state = load_authoritative_state()
        # Bind the immutable provider execution and original params; no SQL-version
        # inference is needed to reuse that exact execution's validated response.
        binding = {"query_id": record["query_id"], "parameters": record.get("query_parameters", {}),
                   "columns": meta["column_names"],
                   "sql_sha256": hashlib.sha256(encoded({"execution_id": record["execution_id"],
                       "query_id": record["query_id"], "parameters": record.get("query_parameters", {})})).hexdigest()}
        cache = result_cache()
        # Cached reuse is spend-free and remains possible while admission is closed.
        cached = cache.lookup(state, record, binding)
        if cached is not None:
            result = {"execution_id": record["execution_id"], "state": "QUERY_STATE_COMPLETED",
                      "result": {"rows": cached["result_rows"], "metadata": meta}}
            save(ROOT / spec["output"], result)
            record["export_points"] = points
            prior = next((entry for entry in state.get("execution_history", [])
                          if entry.get("execution_id") == record["execution_id"]), {})
            for name in ("export_wire_bytes", "estimated_export_credits"):
                if name in prior:
                    record[name] = prior[name]
            return remaining - points
        usage_guard(state)
        risk, evidence = export_risk(state, record, spec, result_bytes)
        totals = state["budget_decision"]["report"]["totals"]
        spare = totals["limit_credits"] - totals["usage_plus_reserve"] - totals["dependencies_total"]
        if risk > spare:
            raise BudgetBlocked("Export risk exceeds headroom after all remaining scheduled obligations")
        # Prune only after a prior full acceptance. Never evict pending source work.
        cache.prune_consumed(state)
        cache.prepare_export(state, record, binding, risk_credits=risk, evidence=evidence, timestamp=stamp())
        result, wire_bytes = api(f'execution/{record["execution_id"]}/results?limit=10000&allow_partial_results=false', include_wire_size=True)
        fetched = result.get("result", {}).get("rows", [])
        if (result.get("state") != "QUERY_STATE_COMPLETED" or not isinstance(fetched, list)
                or len(fetched) != rows or result.get("next_uri") or result.get("next_offset")
                or (result.get("execution_id") is not None and
                    result["execution_id"] != record["execution_id"])):
            raise RuntimeError("Source result is incomplete; canonical state not reduced.")
        incurred = max(result_bytes, wire_bytes) / 1000000 * CONFIG["export_credits_per_megabyte"]
        record.update(export_points=points, export_wire_bytes=wire_bytes,
                      estimated_export_credits=incurred)
        for marker in state["result_exports"]["requests"].values():
            if marker["execution_id"] == record["execution_id"]:
                marker["risk_credits"] = max(marker["risk_credits"], incurred)
        for entry in state.get("execution_history", []):
            if entry.get("execution_id") == record["execution_id"]:
                entry.update(export_points=points, export_wire_bytes=wire_bytes,
                             estimated_export_credits=incurred, result_bytes=result_bytes)
        cache.cache_result(state, record, binding, {"execution_id": record["execution_id"],
            "total_row_count": rows, "result_rows": fetched}, timestamp=stamp())
    record["export_points"] = points
    record["export_wire_bytes"] = wire_bytes
    record["estimated_export_credits"] = max(result_bytes, wire_bytes) / 1000000 * CONFIG["export_credits_per_megabyte"]
    save(ROOT / spec["output"], result)
    return remaining - points

def run(script, *args):
    subprocess.run([sys.executable, f"scripts/{script}", *args], cwd=ROOT, check=True)

def due(last, cadence_minutes):
    if not last:
        return True
    elapsed = (now() - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
    return elapsed >= cadence_minutes * 60 - 60

def record_source_issue(state, key, error, unresolved=False):
    issues = state.setdefault("source_errors", {})
    previous = issues.get(key, {})
    failures = previous.get("consecutive_failures", 0) if unresolved else (
        previous.get("consecutive_failures", 0) + 1
    )
    # One bounded recovery attempt per backoff, no noisy repeated spending.
    retry_time = None if unresolved else now() + timedelta(
        minutes=min(120, 30 * 2 ** min(failures - 1, 2))
    )
    issues[key] = {
        "error": str(error), "failed_at_utc": stamp(),
        "consecutive_failures": failures,
        "next_retry_at_utc": retry_time.isoformat().replace("+00:00", "Z") if retry_time else None,
        "unresolved_execution": bool(unresolved),
    }
    save(STATE, state)
    print(f"::warning::Source {key} degraded; last verified data retained: {error}", flush=True)


def source_in_backoff(state, key):
    if state.get("source_errors", {}).get(key, {}).get("manual_reconciliation_required"):
        return True
    pending = state.get("pending_executions", {})
    if key in pending:
        return False  # Always reconcile an existing execution ID.
    until = state.get("source_errors", {}).get(key, {}).get("next_retry_at_utc")
    return bool(until and now() < datetime.fromisoformat(until.replace("Z", "+00:00")))


def ambiguous_submission_keys(state):
    """Unknown execute IDs must never trigger speculative replacements."""
    return sorted(
        key for key, pending in state.get("pending_executions", {}).items()
        if not pending.get("execution_id")
    )


def presentation_in_backoff(state, key):
    # An in-flight execution must always be reconciled before any cooldown.
    if key in state.get("pending_executions", {}):
        return False
    until = state.get("presentation_errors", {}).get(key, {}).get("next_retry_at_utc")
    return bool(until and now() < datetime.fromisoformat(until.replace("Z", "+00:00")))


def reconciliation_generation_due(state):
    configured = int(CONFIG.get("reconciliation_generation", 0))
    completed = int(state.get("reconciliation", {}).get("generation", 0))
    return completed < configured


def holder_integrity_degraded(state):
    return "holder_integrity" in state.get("source_errors", {})


def integrity_repair_due(state):
    if not holder_integrity_degraded(state):
        return False
    recon = state.get("reconciliation", {})
    attempts = int(recon.get("integrity_attempts", 0))
    if attempts >= int(CONFIG.get("integrity_repair_max_attempts", 0)):
        return False
    return due(
        recon.get("last_integrity_attempt_utc"),
        int(CONFIG.get("integrity_repair_cooldown_minutes", 1440)),
    )


def source_reconciliation_plan(state, spec, regular_repair_due):
    """Return (mode, lookback_hours) for bounded historical reconciliation."""
    if spec["key"] != "xnet_transfers":
        if regular_repair_due and spec.get("repair_on_global_cycle", True):
            return (
                "daily",
                int(spec.get("repair_lookback_hours", CONFIG["repair_lookback_hours"])),
            )
        return (None, None)

    if reconciliation_generation_due(state):
        return (
            "approved_backfill",
            int(CONFIG["approved_backfill_lookback_hours"]),
        )
    if integrity_repair_due(state):
        recon = state.get("reconciliation", {})
        if recon.get("integrity_manifest_clean_at_utc"):
            return (
                "integrity",
                int(CONFIG["integrity_repair_lookback_hours"]),
            )
        return (None, None)
    # Routine historical reconciliation is manifest-driven. Do not download a
    # seven-day transfer window every day merely because the repair clock is due.
    return (None, None)


def execution_spec_for_mode(spec, mode):
    execution_spec = dict(spec)
    base_cap = spec.get("max_run_credits")
    if base_cap is None:
        return execution_spec
    if mode == "integrity":
        execution_spec["max_run_credits"] = float(
            spec.get(
                "integrity_repair_max_run_credits",
                spec.get("repair_max_run_credits", base_cap),
            )
        )
    elif mode in {"daily", "approved_backfill"}:
        execution_spec["max_run_credits"] = float(
            spec.get("repair_max_run_credits", base_cap)
        )
    return execution_spec


def export_limits_for_mode(mode):
    if mode == "integrity":
        return (
            int(CONFIG["max_integrity_repair_export_points"]),
            int(CONFIG["max_integrity_repair_export_bytes"]),
        )
    if mode in {"daily", "approved_backfill"}:
        return (
            int(CONFIG["max_repair_export_points"]),
            int(CONFIG["max_repair_export_bytes"]),
        )
    return (
        int(CONFIG["max_export_points_per_run"]),
        int(CONFIG["max_export_bytes_per_run"]),
    )


def canonical_transfer_manifest(lookback_days):
    path = ROOT / "data/canonical/xnet_transfers.csv"
    if not path.exists():
        raise RuntimeError("Canonical transfer ledger is missing.")

    today = now().date()
    start = today - timedelta(days=int(lookback_days))
    daily = {}

    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            day_text = str(row.get("block_date") or "")[:10]
            if not day_text:
                continue
            try:
                day = datetime.fromisoformat(day_text).date()
            except ValueError:
                raise RuntimeError(f"Invalid canonical transfer date: {day_text}")
            if day < start or day >= today:
                continue

            item = daily.setdefault(day_text, {
                "day": day_text,
                "event_count": 0,
                "transfer_count": 0,
                "burn_count": 0,
                "mint_count": 0,
                "tx_ids": set(),
                "min_block_slot": None,
                "max_block_slot": None,
                "amount_raw_total": 0,
                "block_slot_total": 0,
            })
            action = str(row.get("action") or "").lower()
            slot = int(row.get("block_slot") or 0)
            amount = int(row.get("amount_raw") or 0)
            multiplicity = int(row.get("event_multiplicity") or 1)
            item["event_count"] += 1
            if action in {"transfer", "burn", "mint"}:
                item[action + "_count"] += 1
            item["tx_ids"].add(str(row.get("tx_id") or ""))
            item["min_block_slot"] = slot if item["min_block_slot"] is None else min(item["min_block_slot"], slot)
            item["max_block_slot"] = slot if item["max_block_slot"] is None else max(item["max_block_slot"], slot)
            item["amount_raw_total"] += amount * multiplicity
            item["block_slot_total"] += slot * multiplicity

    result = {}
    for day, item in daily.items():
        result[day] = {
            "day": day,
            "event_count": item["event_count"],
            "transfer_count": item["transfer_count"],
            "burn_count": item["burn_count"],
            "mint_count": item["mint_count"],
            "tx_count": len(item["tx_ids"]),
            "min_block_slot": item["min_block_slot"],
            "max_block_slot": item["max_block_slot"],
            "amount_raw_total": str(item["amount_raw_total"]),
            "block_slot_total": str(item["block_slot_total"]),
        }
    return result


def normalize_manifest_rows(rows):
    result = {}
    integer_fields = (
        "event_count", "transfer_count", "burn_count", "mint_count",
        "tx_count", "min_block_slot", "max_block_slot",
    )
    for row in rows:
        day = str(row.get("day") or "")[:10]
        if not day:
            continue
        item = {"day": day}
        for field in integer_fields:
            value = row.get(field)
            item[field] = None if value is None else int(value)
        item["amount_raw_total"] = str(row.get("amount_raw_total") or "0")
        item["block_slot_total"] = str(row.get("block_slot_total") or "0")
        result[day] = item
    return result


def manifest_mismatches(remote, local):
    keys = set(remote) | set(local)
    fields = (
        "event_count", "transfer_count", "burn_count", "mint_count",
        "tx_count", "min_block_slot", "max_block_slot",
        "amount_raw_total", "block_slot_total",
    )
    mismatches = []
    for day in sorted(keys):
        r = remote.get(day)
        l = local.get(day)
        if r is None or l is None or any(r.get(k) != l.get(k) for k in fields):
            mismatches.append(day)
    return mismatches


def reconciliation_weekly_allowed(billing):
    policy = CONFIG["reconciliation"]
    target = float(billing.get("target_credits", CONFIG.get("monthly_target_credits", 2500)))
    headroom = target - float(billing.get("credits_used", 0))
    projected = float(billing.get("projected_period_credits", 0))
    return (
        headroom >= float(policy["minimum_target_headroom_credits"])
        and projected <= float(policy["weekly_soft_projected_monthly_credits"])
    )


def sync_holder_integrity_error(state):
    quality = load(ROOT / "data/current/xnet_holder_integrity.json", {})
    if quality.get("status") == "DEGRADED":
        state.setdefault("source_errors", {})["holder_integrity"] = {
            "error": quality["error"],
            "failed_at_utc": quality["checked_at_utc"],
            "holder_data_as_of_utc": quality["holder_data_as_of_utc"],
            "automatic_recheck": True,
        }
    elif quality.get("status") == "HEALTHY":
        state.setdefault("source_errors", {}).pop("holder_integrity", None)
        state.setdefault("reconciliation", {}).pop("integrity_manifest_clean_at_utc", None)
    return quality


def historical_cost_quarantined(state):
    """Do not automatically retry a reconciliation that exceeded its cost cap."""
    issue = state.get("source_errors", {}).get("historical_reconciliation", {})
    error = str(issue.get("error", "")).lower()
    # Recognize pre-existing production incidents as well as newly tagged ones.
    return bool(issue.get("cost_overrun_quarantined")) or (
        " cost " in f" {error} " and " exceeded " in f" {error} "
    )


def reconcile_historical_manifest(state, billing):
    policy = CONFIG.get("reconciliation")
    if not policy:
        return False

    recon = state.setdefault("reconciliation", {})
    # Existing repairs use CURRENT_DATE-relative offsets, with no saved SQL/version
    # and immutable window proof bound to their original execution. Do NOT download,
    # reduce, consume, or resubmit them after midnight (or on any other recovery).
    if "historical_repair" in state.get("pending_executions", {}):
        quarantine_source(state, "historical_repair", REPAIR_QUARANTINE)
        recon["repair_manual_review_required"] = True
        return False
    if recon.get("repair_manual_review_required"):
        return False  # An aggregate manifest is not permission to repair by union.
    if historical_cost_quarantined(state):
        # Result caps are observed after Dune execution, not pre-charge caps.
        # Preserve last-good history until an operator reviews cost and clears
        # the quarantine explicitly; do not retry the 30-day manifest on cron.
        recon["manifest_deferred_at_utc"] = stamp()
        recon["manifest_deferred_reason"] = (
            "Prior reconciliation exceeded its Dune credit cap; "
            "manual review and explicit clearance required."
        )
        return False

    # Do not open new reconciliation work while another source execution is
    # still awaiting durable consumption/acknowledgement. This keeps exactly-once
    # recovery simple and avoids spending credits during an unsettled hot path.
    pending = set(state.get("pending_executions", {}))
    blocking_pending = pending - {"reconciliation_manifest", "historical_repair"}
    if blocking_pending:
        recon["manifest_deferred_at_utc"] = stamp()
        recon["manifest_deferred_reason"] = (
            "Pending source recovery: " + ", ".join(sorted(blocking_pending))
        )
        return False

    if (
        "xnet_transfers" in state.get("source_errors", {})
        and not holder_integrity_degraded(state)
    ):
        recon["manifest_deferred_at_utc"] = stamp()
        recon["manifest_deferred_reason"] = (
            "Primary transfer collector is degraded; preserve credits until hot-path recovery."
        )
        return False
    force_wide = holder_integrity_degraded(state)
    pending_days = list(recon.get("pending_mismatch_days", []))
    daily_due = due(recon.get("last_daily_manifest_utc"), int(policy["daily_cadence_minutes"]))
    weekly_due = due(recon.get("last_weekly_manifest_utc"), int(policy["weekly_cadence_minutes"]))

    mode = None
    lookback_days = None
    if force_wide or pending_days:
        mode = "integrity" if force_wide else "pending"
        lookback_days = int(policy["weekly_lookback_days"])
    elif weekly_due and reconciliation_weekly_allowed(billing):
        mode = "weekly"
        lookback_days = int(policy["weekly_lookback_days"])
    elif daily_due:
        mode = "daily"
        lookback_days = int(policy["daily_lookback_days"])
    elif weekly_due:
        recon["weekly_deferred_at_utc"] = stamp()
        recon["weekly_deferred_reason"] = (
            f"Projected Dune usage {billing.get('projected_period_credits', 0):.1f} "
            f"credits; preserving target {billing.get('target_credits', 0):.0f}."
        )
        return False
    else:
        return False

    manifest_spec = {
        "key": "reconciliation_manifest",
        "query_id": int(policy["manifest_query_id"]),
        "max_run_credits": float(policy["manifest_max_run_credits"]),
        "output": "state/v3_transfer_manifest_latest.json",
        "performance": CONFIG["performance"],
    }

    try:
        record = execute(
            manifest_spec,
            {"lookback_days": lookback_days},
            state=state,
            timeout_seconds=CONFIG.get("source_execution_timeout_seconds", 240),
        )
        export_source(
            record,
            manifest_spec,
            int(policy["manifest_max_export_points"]),
            max_bytes=int(policy["manifest_max_export_bytes"]),
            state=state,
        )
        result = load(ROOT / manifest_spec["output"], {})
        rows = result.get("result", {}).get("rows", [])
        remote = normalize_manifest_rows(rows)
        local = canonical_transfer_manifest(lookback_days)

        # The canonical ledger intentionally starts later than a long manifest
        # window. Only compare days at or after the first canonical day.
        if local:
            first_local = min(local)
            remote = {day: row for day, row in remote.items() if day >= first_local}

        mismatches = manifest_mismatches(remote, local)
        recon.update({
            "last_manifest_mode": mode,
            "last_manifest_utc": stamp(),
            "last_manifest_lookback_days": lookback_days,
            "last_manifest_execution_id": record["execution_id"],
            "last_manifest_cost_credits": record["execution_cost_credits"],
            "last_manifest_mismatch_days": mismatches,
            "manifest_identity_verified": False,
            "manifest_decision": "aggregate_mismatch_manual_review" if mismatches else "aggregate_match_advisory_only",
        })
        if mode in {"daily", "weekly"}:
            recon[f"last_{mode}_manifest_utc"] = recon["last_manifest_utc"]
        elif mode == "integrity":
            recon["last_weekly_manifest_utc"] = recon["last_manifest_utc"]
        if mode == "weekly":
            recon.pop("weekly_deferred_at_utc", None)
            recon.pop("weekly_deferred_reason", None)

        if not mismatches:
            recon.pop("pending_mismatch_days", None)
            # Counts/weighted totals cannot prove owner, amount, deletion, or event
            # identity. Never turn aggregate equality into an integrity clearance.
            recon.pop("integrity_manifest_clean_at_utc", None)
            stage_consumption(state, manifest_spec, record, "manifest_decision")
            checkpoint(state)  # decision AND its consumption marker in one commit
            return False
        recon["pending_mismatch_days"] = mismatches
        recon["repair_manual_review_required"] = True
        quarantine_source(state, "historical_reconciliation", REPAIR_QUARANTINE)
        stage_consumption(state, manifest_spec, record, "manifest_decision")
        checkpoint(state)
        return False
    except StateError:
        raise  # Publication failure is a terminal lifecycle barrier, not a source issue.
    except ExecutionPending as error:
        record_source_issue(state, "historical_reconciliation", error, unresolved=True)
        return False
    except ExecutionCostExceeded as error:
        record_source_issue(state, "historical_reconciliation", error)
        state["source_errors"]["historical_reconciliation"]["cost_overrun_quarantined"] = True
        save(STATE, state)
        return False
    except (SubmissionRejected, ExecutionFailed, RuntimeError) as error:
        record_source_issue(state, "historical_reconciliation", error)
        return False


REPAIR_QUARANTINE = (
    "Manual reconciliation required: relative CURRENT_DATE/day-offset repair has no "
    "execution-bound immutable UTC scope, pinned SQL identity, exact row/deletion proof "
    "or verified seed/overlap replacement. Pending execution retained; no reduction or consumption."
)


class ManualReviewRequired(RuntimeError):
    """Candidate cannot be accepted safely; preserve pending provider evidence."""


def quarantine_source(state, key, reason):
    previous = state.setdefault("source_errors", {}).get(key, {})
    state["source_errors"][key] = {
        **previous, "error": reason,
        "failed_at_utc": previous.get("failed_at_utc") or stamp(),
        "manual_reconciliation_required": True, "automatic_recheck": False,
        "unresolved_execution": key in state.get("pending_executions", {}),
        "next_retry_at_utc": None,
    }


def stage_consumption(state, spec, record, effect):
    """Stage an execution's acceptance effect; caller supplies the durable commit."""
    execution_id = record["execution_id"]
    identity = {"key": spec["key"], "query_id": int(record["query_id"]),
                "query_parameters": deepcopy(record.get("query_parameters", {})), "effect": effect}
    consumed = state.setdefault("consumed_executions", {})
    previous = consumed.get(execution_id)
    if previous and any(previous.get(field) != value for field, value in identity.items()):
        raise StateError("Consumed execution identity/effect changed; reviewed reconciliation required")
    tracker(state).stage_acknowledge(spec, record)
    if not previous:
        consumed[execution_id] = {**identity, "accepted_at_utc": stamp()}
    return not previous


def validate_hot_transfers(path, state):
    """Non-destructive hot union only. Ambiguous corrections are NOT additions.

    The reducer's event ID contains amount but omits owner/multiplicity. Without
    instruction ordinals or an exact replacement proof, changed rows sharing a
    stable transaction/token-account locator must be quarantined, not unioned.
    """
    from v3_reduce_chain import normalise_transfer, parse_ts
    result = load(path, {})
    if result.get("state") != "QUERY_STATE_COMPLETED":
        raise ManualReviewRequired("Transfer result is not complete")
    rows = result.get("result", {}).get("rows")
    if not isinstance(rows, list):
        raise ManualReviewRequired("Missing complete transfer rows")
    def normalized(row):
        row = dict(row)
        try:
            for field in ("amount_raw", "block_slot", "event_multiplicity"):
                number = Decimal(str(row.get(field, "1" if field == "event_multiplicity" else "")))
                if not number.is_finite() or number != number.to_integral_value() or number < (1 if field == "event_multiplicity" else 0):
                    raise ValueError(field)
                row[field] = str(int(number))
            if not row.get("tx_id") or row.get("action") not in {"mint", "burn", "transfer"}:
                raise ValueError("transfer identity/action")
            timestamp = parse_ts(str(row["block_time"]))
            if str(row["block_date"])[:10] != timestamp.date().isoformat():
                raise ValueError("transfer date/time")
            row["block_time"] = timestamp.isoformat()
            row["block_date"] = timestamp.date().isoformat()
            row["amount_xnet"] = format(Decimal(row["amount_raw"]) / Decimal(100000000), "f")
        except (KeyError, ValueError, ArithmeticError) as error:
            raise ManualReviewRequired(f"Invalid transfer row: {error}") from error
        return normalise_transfer(row)
    def locator(row):
        return tuple(row[key] for key in ("tx_id", "block_slot", "action", "from_token_account", "to_token_account"))
    def signature(row):
        return tuple(row[key] for key in ("amount_raw", "from_owner", "to_owner", "event_multiplicity", "block_time"))
    canonical_path = ROOT / "data/canonical/xnet_transfers.csv"
    if not canonical_path.exists():
        raise ManualReviewRequired("Canonical transfer ledger missing")
    canonical = {}
    with canonical_path.open(newline="") as source:
        for row in csv.DictReader(source):
            row = normalized(row)
            canonical.setdefault(locator(row), set()).add(signature(row))
    recon = state.get("reconciliation", {})
    repaired_days = {item["day"] for item in recon.get("last_repaired_days", []) if item.get("day")}
    legacy_cutoff = recon.get("last_completed_utc") if recon.get("last_mode") == "selective_repair" else None
    seed = load(ROOT / "data/xnet_owner_balances_seed_metadata.json", {})
    seed_cutoff = seed.get("latest_balance_event_time")
    normalized_rows, seen = [], {}
    for row in rows:
        row = normalized(row)
        key, value = locator(row), signature(row)
        old = canonical.get(key, set())
        if old and value not in old:
            raise ManualReviewRequired("Hot overlap changes amount/owner/multiplicity/time; exact scoped replacement required")
        if key in seen:
            # A duplicate group or multiple amount variants without instruction
            # identity cannot be represented safely by this union reducer.
            raise ManualReviewRequired("Ambiguous duplicate hot transfer locator")
        seen[key] = value
        if value not in old:
            timestamp = parse_ts(row["block_time"])
            if (row["block_date"] in repaired_days or
                    (legacy_cutoff and timestamp <= parse_ts(legacy_cutoff)) or
                    (seed_cutoff and timestamp <= parse_ts(seed_cutoff))):
                raise ManualReviewRequired("Hot row overlaps a repaired/seeded window; stale resurrection fenced")
        normalized_rows.append(row)
    # Canonical IDs are formed from normalized raw integers, not JSON float text.
    save(path, {**result, "result": {**result["result"], "rows": normalized_rows}})


def reduce_atomically(transfer_path, bbb_path=None):
    # A failed reduction must preserve canonical data and all derived state.
    with tempfile.TemporaryDirectory() as directory:
        backup = Path(directory) / "data"
        shutil.copytree(ROOT / "data", backup)
        try:
            args = ["--transfer-result", str(transfer_path)]
            if bbb_path:
                args.extend(["--bbb-result", str(bbb_path)])
            run("v3_reduce_chain.py", *args, "--allow-stale-holders")
        except Exception:
            shutil.rmtree(ROOT / "data")
            shutil.copytree(backup, ROOT / "data")
            raise

def publish(state_only=False):
    try:
        return LiveStateStore(ROOT).publish("checkpoint" if state_only else "full")
    except StateError:
        raise
    except Exception as error:
        raise CheckpointFailed(f"Publication durability failed: {error}") from error


def publish_data_generation(state, paths, source_records):
    """Validate/build candidate effects, then commit data + consumption together.

    No tracker/provider calls may checkpoint a half-built generation. Pre-publish
    failures restore last-good data and in-memory acceptance state. Once publication
    is attempted, uncertainty is a durability barrier: preserve the exact candidate
    for the store's recovery, never publish a compensating stale snapshot.
    """
    before = deepcopy(state)
    publishing = False
    with tempfile.TemporaryDirectory() as directory:
        backup = Path(directory) / "data"
        shutil.copytree(ROOT / "data", backup)
        try:
            if "xnet_transfers" in paths:
                validate_hot_transfers(paths["xnet_transfers"], state)
                reduce_atomically(paths["xnet_transfers"], paths.get("bbb_dex"))
                sync_holder_integrity_error(state)
                state["chain_completed_at_utc"] = stamp()
                for spec, record in source_records.values():
                    state["queries"][spec["key"]] = record
                    state.setdefault("source_errors", {}).pop(spec["key"], None)
                    stage_consumption(state, spec, record, "non_destructive_hot_union")
            # Keep public health shape compatible; Astra owns broader health policy.
            health = load(HEALTH, {})
            health.update({"schema_version": 2,
                "last_refresh_completed_utc": state.get("chain_completed_at_utc"),
                "status": "DEGRADED" if state.get("source_errors") else "HEALTHY",
                "cadence_minutes": CONFIG["chain_cadence_minutes"],
                "paused_due_to_cost": False,
                "sources": {spec["key"]: state["queries"][spec["key"]]
                            for spec in CONFIG["sources"] if spec["key"] in state["queries"]},
                "degraded_sources": state.get("source_errors", {}),
                "reconciliation": state.get("reconciliation", {}), "automatic_retry": True})
            save(HEALTH, health)
            run("v3_build_chain_snapshot.py")
            try:
                run("v3_collect_market.py")
            except subprocess.CalledProcessError as error:
                if not (ROOT / "data/current/xnet_market_state.json").exists():
                    raise
                state.setdefault("source_errors", {})["market"] = {
                    "error": str(error), "failed_at_utc": stamp(), "automatic_recheck": True}
            else:
                state.setdefault("source_errors", {}).pop("market", None)
            run("v3_build_live_bundle.py")
            write_operational_health(state)
            save(STATE, state)
            publishing = True
            return publish()  # the only acceptance commit, including staged markers
        except Exception:
            if not publishing:
                shutil.rmtree(ROOT / "data")
                shutil.copytree(backup, ROOT / "data")
                state.clear()
                state.update(before)
            raise

def refresh_independent_data(state):
    """No Dune API routes at all; retain the exact chain pause and accepted ledger."""
    try:
        run("v3_sync_network.py")
        # The source collector records its own provenance, not Dune acceptance.
        refreshed = validate_journal(STATE.read_bytes())
        state.clear()
        state.update(refreshed)
        try:
            run("v3_collect_market.py")
        except subprocess.CalledProcessError as error:
            if not (ROOT / "data/current/xnet_market_state.json").exists():
                raise
            state.setdefault("source_errors", {})["market"] = {
                "error": str(error), "failed_at_utc": stamp(), "automatic_recheck": True}
        run("v3_build_live_bundle.py")
        write_operational_health(state)
        save(STATE, state)
        receipt = publish()
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as report:
                report.write("## XNET independent data publication\n\n")
                report.write("No Dune executions or result exports. Chain pause retained: "
                             + str(state.get("paused", False)) + ".\n\n")
                report.write("Generation revision: " + str((receipt or {}).get("revision", "see state receipt")) + "\n")
        return 0
    except Exception as error:
        # Do not publish a compensating snapshot after uncertain acceptance.
        print(f"Independent publication failed; last remote generation retained: {error}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Explicitly clear a reviewed pause; never used by cron")
    parser.add_argument("--cached-only", action="store_true", help="Publish independent API/cache data; never submit or export Dune work")
    parser.add_argument("--benchmark", action="store_true", help="Measure query costs without changing canonical state")
    parser.add_argument("--benchmark-sources", action="store_true", help="Measure bounded sources only")
    parser.add_argument("--benchmark-batches", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--benchmark-repetitions", type=int, choices=range(1, 7), default=1)
    args = parser.parse_args()
    state = load_authoritative_state()
    if args.cached_only:
        return refresh_independent_data(state)
    if state.get("paused") and (args.benchmark or args.benchmark_sources):
        raise SystemExit("Refresh paused; benchmark cannot bypass the production pause.")
    if args.benchmark or args.benchmark_sources:
        records = []
        # Start with the smallest presentation query, then each shared query.
        specs = sorted(CONFIG["presentation"], key=lambda spec: spec["query_id"] != 8895092) + CONFIG["sources"]
        if args.benchmark_sources:
            specs = CONFIG["sources"]
        for batch in range(1, args.benchmark_batches + 1):
            for repetition in range(1, args.benchmark_repetitions + 1):
                for spec in specs:
                    lookback = int(
                        spec.get("lookback_hours", 2)
                    ) if spec in CONFIG["sources"] else None
                    params = (
                        {"lookback_hours": lookback}
                        if lookback is not None
                        else None
                    )
                    benchmark_state = state
                    # Benchmarks have a distinct metadata-only acceptance effect;
                    # never retire a pending production-source operation by name.
                    spec = {**spec, "key": "benchmark:" + spec["key"]}
                    records.append({"batch": batch, "repetition": repetition,
                        "key": spec["key"], "lookback_hours": lookback,
                        **execute(spec, params, enforce_cap=False, state=benchmark_state)})
                    save(ROOT / "state/v3_credit_benchmark.json", {"generated_at_utc": stamp(), "performance": CONFIG["performance"], "queries": records})
                    stage_consumption(benchmark_state, spec, records[-1], "benchmark_metadata")
                    checkpoint(benchmark_state)
                    print(batch, repetition, spec["key"], records[-1]["execution_cost_credits"], "credits", flush=True)
                    if records[-1]["execution_cost_credits"] > 2 or sum(r["execution_cost_credits"] for r in records) > 30:
                        raise RuntimeError("Benchmark safety allowance reached; costs saved for review.")
        return 0
    if not state.get("paused") or args.resume:
        # Startup receipt was checked before any producer mutation. Network is a
        # separate non-Dune domain; import only its journal update in this run.
        run("v3_sync_network.py")
        refreshed = validate_journal(STATE.read_bytes())
        state.clear()
        state.update(refreshed)
    reconciliation_errors = tracker(state).reconcile()
    for key, error in reconciliation_errors.items():
        print(f"::warning::Execution reconciliation {key}: {error}", flush=True)
    if state.get("paused") and not args.resume:
        # A green last-good snapshot must not conceal a global production pause.
        old_health = load(HEALTH, {})
        if old_health.get("status") != "PAUSED":
            old_health.update({
                "status": "PAUSED",
                "paused_at_utc": state.get("failed_at_utc"),
                "pause_reason": state.get("last_error", "unknown"),
                "automatic_retry": False,
            })
            save(HEALTH, old_health)
        if os.environ.get("EVENT") in {"schedule", "push"}:
            # Independent public APIs remain available during a chain hold.
            return refresh_independent_data(state)
        message = "Refresh remains paused after a previous failure; no queries submitted."
        raise SystemExit("Refresh paused after previous failure; review state and manually resume.")
    state.update({"paused": False, "started_at_utc": stamp()})
    # Configuration changes must not erase prior accepted provenance/known IDs.
    paths, source_records = {}, {}
    try:
        try:
            billing = usage_guard(state)
        except BudgetBlocked as error:
            billing = None
            print(f"::warning::{error}", flush=True)
        health = load(HEALTH, {})
        last = state.get("chain_completed_at_utc") or health.get("last_refresh_completed_utc")
        repair = due(state.get("chain_repaired_at_utc"), CONFIG["repair_cadence_minutes"])
        generation_due = reconciliation_generation_due(state)
        integrity_due = integrity_repair_due(state)

        source_config_changed = any(
            int(
                (
                    state.get("queries", {})
                    .get(spec["key"], {})
                    .get("query_parameters", {})
                    or {}
                ).get("lookback_hours", 0)
                or 0
            )
            < int(spec.get("lookback_hours", 2))
            for spec in CONFIG["sources"]
            if spec.get("enabled", True)
        )

        if (
            source_config_changed
            or repair
            or generation_due
            or integrity_due
            or due(last, CONFIG["chain_cadence_minutes"])
            or any(s["key"] in state.get("pending_executions", {}) for s in CONFIG["sources"])
        ):
            if integrity_due:
                remaining, default_export_max_bytes = export_limits_for_mode("integrity")
            elif generation_due or repair:
                remaining, default_export_max_bytes = export_limits_for_mode("daily")
            else:
                remaining, default_export_max_bytes = export_limits_for_mode(None)
            active_sources = [
                s
                for s in CONFIG["sources"]
                if s.get("enabled", True)
            ]

            due_sources = []

            for spec in active_sources:
                previous = (
                    state.get("queries", {})
                    .get(spec["key"], {})
                )
                previous_completed = previous.get(
                    "completed_at_utc"
                )
                cadence = int(
                    spec.get(
                        "cadence_minutes",
                        CONFIG["chain_cadence_minutes"],
                    )
                )

                configured_lookback = int(
                    spec.get(
                        "lookback_hours",
                        2,
                    )
                )

                previous_lookback = int(
                    (
                        previous.get(
                            "query_parameters",
                            {},
                        )
                        or {}
                    ).get(
                        "lookback_hours",
                        0,
                    )
                    or 0
                )

                lookback_changed = (
                    previous_lookback
                    < configured_lookback
                )

                preferred_hour = spec.get(
                    "preferred_hour_utc"
                )

                # Recover a missed preferred hour after a 3-hour grace
                # period instead of deferring BBB collection another day.
                # The existing max_catchup_hours guard still applies.
                in_preferred_hour = (
                    preferred_hour is None
                    or now().hour == int(preferred_hour)
                    or (
                        previous_completed is not None
                        and due(previous_completed, cadence + 180)
                    )
                )

                reconciliation_mode, _ = source_reconciliation_plan(
                    state, spec, repair
                )

                if (
                    lookback_changed
                    or reconciliation_mode is not None
                    or spec["key"] in state.get("pending_executions", {})
                    or (
                        in_preferred_hour
                        and due(
                            previous_completed,
                            cadence,
                        )
                    )
                ):
                    due_sources.append(
                        spec
                    )

            # Do not submit a replacement during a source-specific cooling-off
            # period. Presentation refreshes still proceed using last-good data.
            due_sources = [
                spec for spec in due_sources
                if not source_in_backoff(state, spec["key"])
            ]

            # A recovered BBB result must be reduced even if the transfer clock
            # isn't due yet. The unchanged canonical CSV remains deduplicated.
            if any(s["key"] == "bbb_dex" for s in due_sources):
                transfer = next((s for s in active_sources if s["key"] == "xnet_transfers"), None)
                if transfer and transfer not in due_sources:
                    due_sources.insert(0, transfer)
            paths = {}
            successful_sources = []
            source_modes = {}
            source_lookbacks = {}

            for spec in due_sources:
                # BBB-only results cannot be committed without the canonical
                # transfer clock; the reducer requires a transfer input.
                if spec["key"] == "bbb_dex" and "xnet_transfers" not in paths:
                    continue
                previous = (
                    state.get("queries", {})
                    .get(spec["key"], {})
                )
                previous_completed = previous.get(
                    "completed_at_utc"
                )

                planned_mode, planned_lookback = source_reconciliation_plan(
                    state, spec, repair
                )

                pending = state.get("pending_executions", {}).get(spec["key"])
                actual_mode = None
                if pending:
                    lookback = pending["query_parameters"].get("lookback_hours", spec.get("lookback_hours", 2))
                elif planned_mode is not None:
                    actual_mode = planned_mode
                    lookback = planned_lookback
                elif previous_completed:
                    elapsed_hours = (
                        now()
                        - datetime.fromisoformat(
                            previous_completed.replace(
                                "Z",
                                "+00:00",
                            )
                        )
                    ).total_seconds() / 3600

                    lookback = max(
                        int(
                            spec.get(
                                "lookback_hours",
                                2,
                            )
                        ),
                        math.ceil(
                            elapsed_hours + 1
                        ),
                    )

                    if (
                        lookback
                        > int(
                            spec.get(
                                "max_catchup_hours",
                                CONFIG["max_catchup_hours"],
                            )
                        )
                    ):
                        record_source_issue(
                            state, spec["key"],
                            f"Canonical collection gap ({lookback}h) exceeds "
                            "configured safe catch-up limit; reviewed repair required."
                        )
                        continue
                else:
                    lookback = int(
                        spec.get(
                            "lookback_hours",
                            2,
                        )
                    )

                try:
                    record = execute(
                        execution_spec_for_mode(spec, actual_mode),
                        {"lookback_hours": lookback},
                        state=state,
                        timeout_seconds=spec.get("execution_timeout_seconds"),
                    )
                except ExecutionCancelled as error:
                    # The terminal cancellation is resolved, so apply a cooldown.
                    record_source_issue(state, spec["key"], error)
                    continue
                except ExecutionPending as error:
                    record_source_issue(state, spec["key"], error, unresolved=True)
                    continue
                except (SubmissionRejected, ExecutionCostExceeded, ExecutionFailed, BudgetBlocked) as error:
                    record_source_issue(state, spec["key"], error)
                    continue

                if spec.get(
                    "track_continuous_coverage",
                    False,
                ):
                    completed_dt = (
                        datetime.fromisoformat(
                            record[
                                "completed_at_utc"
                            ].replace(
                                "Z",
                                "+00:00",
                            )
                        )
                    )

                    interval_start = (
                        completed_dt
                        - timedelta(
                            hours=lookback
                        )
                    )
                    interval_end = completed_dt

                    previous_start = previous.get(
                        "continuous_coverage_start_utc"
                    )
                    previous_end = previous.get(
                        "continuous_coverage_end_utc"
                    )

                    if (
                        previous_start
                        and previous_end
                    ):
                        previous_start_dt = (
                            datetime.fromisoformat(
                                previous_start.replace(
                                    "Z",
                                    "+00:00",
                                )
                            )
                        )
                        previous_end_dt = (
                            datetime.fromisoformat(
                                previous_end.replace(
                                    "Z",
                                    "+00:00",
                                )
                            )
                        )

                        if (
                            interval_start
                            <= previous_end_dt
                            + timedelta(minutes=5)
                        ):
                            interval_start = min(
                                interval_start,
                                previous_start_dt,
                            )
                            interval_end = max(
                                interval_end,
                                previous_end_dt,
                            )

                    record[
                        "continuous_coverage_start_utc"
                    ] = (
                        interval_start
                        .isoformat(
                            timespec="seconds"
                        )
                        .replace(
                            "+00:00",
                            "Z",
                        )
                    )

                    record[
                        "continuous_coverage_end_utc"
                    ] = (
                        interval_end
                        .isoformat(
                            timespec="seconds"
                        )
                        .replace(
                            "+00:00",
                            "Z",
                        )
                    )

                _, export_max_bytes = export_limits_for_mode(actual_mode)
                try:
                    remaining = export_source(record, spec, remaining,
                                              max_bytes=export_max_bytes, state=state)
                except BudgetBlocked as error:
                    record_source_issue(state, spec["key"], error, unresolved=True)
                    continue
                except CacheError as error:
                    quarantine_source(state, spec["key"], str(error))
                    checkpoint(state)
                    continue
                source_modes[spec["key"]] = actual_mode
                source_lookbacks[spec["key"]] = int(lookback)

                paths[
                    spec["key"]
                ] = (
                    ROOT
                    / spec["output"]
                )
                successful_sources.append(spec)
                source_records[spec["key"]] = (spec, record)

        # No canonical files or accepted source clocks have been mutated yet.
        # A manifest decision can checkpoint safely before the data transaction.
        historical_changed = reconcile_historical_manifest(state, billing) if billing is not None else False
        if historical_changed:
            sync_holder_integrity_error(state)
        publish_data_generation(state, paths, source_records)
        forced = set(state.get("forced_presentations", []))
        due_presentation = []
        # Historical corrections take priority over ordinary cadence so Dune
        # cannot display a stale pre-reconciliation holder/burn snapshot.
        for spec in CONFIG["presentation"]:
            if spec["key"] in forced:
                due_presentation.append(spec)
        for spec in CONFIG["presentation"]:
            if spec in due_presentation:
                continue
            last = state["queries"].get(spec["key"], {}).get("completed_at_utc")
            if due(last, spec["cadence_minutes"]) or spec["key"] in state.get("pending_executions", {}):
                due_presentation.append(spec)

        # Confirmed terminal failures wait for bounded backoff. Pending work is
        # exempt so existing execution IDs are always reconciled first.
        due_presentation = [s for s in due_presentation if not presentation_in_backoff(state, s["key"])]
        if billing is None:
            due_presentation = [s for s in due_presentation
                                if state.get("pending_executions", {}).get(s["key"], {}).get("execution_id")]
        unknown = ambiguous_submission_keys(state)
        if unknown:
            # A Dune execute HTTP 5xx may have accepted work. One such
            # ambiguous response must halt further POSTs globally, but cached
            # state and the last verified dashboard remain publishable.
            print(
                f"::warning::Dune execution submissions paused: unknown "
                f"execution ID(s) for {', '.join(unknown)}; reviewed "
                "reconciliation required. No new Dune queries submitted.",
                flush=True,
            )
            due_presentation = [s for s in due_presentation
                                if state.get("pending_executions", {}).get(s["key"], {}).get("execution_id")]

        # Bound each cron run so one slow cached chart cannot consume the
        # whole GitHub Actions timeout. Overdue charts simply remain due for
        # the next run.
        max_presentation = int(
            CONFIG.get("max_presentation_queries_per_run", 5)
        )
        presentation_timeout = int(
            CONFIG.get("presentation_execution_timeout_seconds", 90)
        )

        presentation_errors = {}
        for spec in due_presentation[:max_presentation]:
            acceptance_before = None
            try:
                # Do not download presentation rows: charts use Dune's cached
                # executions. Presentation failures are fail-soft because the
                # previous successful Dune result remains valid.
                record = execute(
                    spec,
                    timeout_seconds=presentation_timeout,
                    state=state,
                )
                # Execute/checkpoints are already durable. Roll back only the
                # local acceptance attempt if its validation fails, never the ID.
                acceptance_before = deepcopy(state)
                stage_consumption(state, spec, record, "presentation_terminal_success")
                state["queries"][spec["key"]] = record
                state.get("presentation_errors", {}).pop(spec["key"], None)
                forced.discard(spec["key"])
                state["forced_presentations"] = sorted(forced)
                checkpoint(state)
            except StateError:
                raise
            except Exception as error:
                if acceptance_before is not None:
                    state.clear()
                    state.update(acceptance_before)
                    forced = set(state.get("forced_presentations", []))
                previous = state.get("presentation_errors", {}).get(spec["key"], {})
                terminal = isinstance(error, (
                    ExecutionCancelled, ExecutionFailed, ExecutionCostExceeded,
                    SubmissionRejected,
                ))
                failures = int(previous.get("consecutive_failures", 0)) + (1 if terminal else 0)
                retry_at = (now() + timedelta(minutes=min(120, 30 * 2 ** min(failures - 1, 2)))) if terminal else None
                presentation_errors[spec["key"]] = {
                    "failed_at_utc": stamp(),
                    "error": str(error),
                    "consecutive_failures": failures,
                    "next_retry_at_utc": (
                        retry_at.isoformat().replace("+00:00", "Z") if retry_at else None
                    ),
                }
                print(
                    f"::warning::Presentation refresh {spec['key']} "
                    f"failed; preserving last-good Dune result: {error}"
                )
            save(STATE, state)

        if presentation_errors:
            state.setdefault("presentation_errors", {}).update(
                presentation_errors
            )
            save(STATE, state)
        try:
            state["billing"] = usage_guard(state)
        except BudgetBlocked:
            pass  # New spending denied; accepted known work remains publishable.
        state.update({"completed_at_utc": stamp(), "last_error": None})
        # Health is reconciled after successful source commits, not before them.
        if HEALTH.exists():
            health = load(HEALTH, {})
            health["status"] = "DEGRADED" if state.get("source_errors") else "HEALTHY"
            health["degraded_sources"] = state.get("source_errors", {})
            health["reconciliation"] = state.get("reconciliation", {})
            health["automatic_retry"] = True
            save(HEALTH, health)
        checkpoint(state)
        return 0
    except ManualReviewRequired as error:
        quarantine_source(state, "xnet_transfers", str(error))
        checkpoint(state)
        print(f"::warning::{error}", flush=True)
        return 0
    except StateError as error:
        # Never follow an uncertain publication with a compensating publication.
        # The exact candidate, its journal and store intent remain recovery evidence.
        print(f"Durability barrier: {error}; preserve diagnostic artifacts and reconcile.", file=sys.stderr)
        return 1
    except ExecutionPending as error:
        # Recover unresolved work on the next schedule without a global pause.
        state.update({"last_pending_error": str(error), "pending_at_utc": stamp()})
        checkpoint(state)
        print(f"::warning::{error}", flush=True)
        return 0
    except Exception as error:
        state.update({"paused": True, "last_error": str(error), "failed_at_utc": stamp()})
        save(STATE, state)
        old_health = load(HEALTH, {})
        old_health.update({
            "status": "PAUSED",
            "paused_at_utc": state["failed_at_utc"],
            "pause_reason": str(error),
            "automatic_retry": False,
        })
        save(HEALTH, old_health)
        # Preserve last verified measurements but expose pause in health metadata.
        try:
            checkpoint(state)
        except Exception:
            print("Could not persist pause. Review workflow artifact before resuming.", file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
