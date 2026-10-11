#!/usr/bin/env python3
"""Bounded sources, status-only chart refreshes, and a persistent spend guard."""
from __future__ import annotations
import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from v3_execution_tracker import (
    ExecutionPending, ExecutionTracker, SubmissionRejected,
    ExecutionCostExceeded, ExecutionFailed,
)

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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)

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
        if path.endswith("/execute") and error.code in (400, 401, 403, 404, 422):
            raise SubmissionRejected(detail) from None
        raise RuntimeError(detail) from None

def usage_guard():
    usage = api("usage", {"start_date": now().date().replace(day=1).isoformat(),
                          "end_date": (now().date() + timedelta(days=1)).isoformat()})
    periods = usage.get("billing_periods", [])
    today = now().date().isoformat()
    active = [p for p in periods if str(p["start_date"])[:10] <= today < str(p["end_date"])[:10]]
    if not active:
        raise RuntimeError("Unable to identify active billing period; spending stopped.")
    period = max(active, key=lambda p: p["start_date"])
    used = float(period["credits_used"])
    limit = min(float(period["credits_included"]), CONFIG["monthly_spend_guard_credits"])
    if used + CONFIG["minimum_credit_reserve"] >= limit:
        raise RuntimeError(f"Monthly spend guard reached: {used:.3f}/{limit:.0f} credits.")
    start = datetime.fromisoformat(str(period["start_date"])[:10]).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(str(period["end_date"])[:10]).replace(tzinfo=timezone.utc)
    elapsed_days = max((now() - start).total_seconds() / 86400, 1.0)
    period_days = max((end - start).total_seconds() / 86400, 1.0)
    projected = used / elapsed_days * period_days
    target = min(float(CONFIG.get("monthly_target_credits", limit)), limit)
    return {
        "credits_used": used,
        "guard_credits": limit,
        "target_credits": target,
        "projected_period_credits": projected,
        "period_start": period["start_date"],
        "period_end": period["end_date"],
    }

def checkpoint(state):
    # Only Dune execution state is published here. Revenue/feed builders and
    # dashboard-data publication are deliberately excluded.
    save(STATE, state)
    publish(state_only=True)

def tracker(state):
    return ExecutionTracker(state, api, lambda: checkpoint(state), usage_guard,
                            now, time.sleep, CONFIG["performance"])

def execute(spec, params=None, enforce_cap=True, timeout_seconds=None, state=None):
    if state is None:
        state = load(STATE, {"schema_version": 1, "queries": {}})
    if timeout_seconds is None:
        timeout_seconds = CONFIG.get("source_execution_timeout_seconds", 240)
    return tracker(state).execute(spec, params, enforce_cap, timeout_seconds)
def export_source(record, spec, remaining, max_bytes=None):
    meta = record["result_metadata"]
    rows = int(meta.get("total_row_count", meta.get("row_count", 0)))
    if "column_names" not in meta or "total_row_count" not in meta:
        raise RuntimeError("Missing complete result metadata; source export stopped.")
    points = int(meta.get("datapoint_count", rows * len(meta["column_names"])))
    result_bytes = int(meta.get("total_result_set_bytes", meta.get("result_set_bytes", 0)))
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
        usage_guard()
        result, wire_bytes = api(f'execution/{record["execution_id"]}/results?limit=10000&allow_partial_results=false', include_wire_size=True)
        fetched = result.get("result", {}).get("rows", [])
        if len(fetched) != rows or result.get("next_uri") or result.get("next_offset"):
            raise RuntimeError("Source result is incomplete; canonical state not reduced.")
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
    pending = state.get("pending_executions", {})
    if key in pending:
        return False  # Always reconcile an existing execution ID.
    until = state.get("source_errors", {}).get(key, {}).get("next_retry_at_utc")
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


def reconcile_historical_manifest(state, billing):
    policy = CONFIG.get("reconciliation")
    if not policy:
        return False

    recon = state.setdefault("reconciliation", {})

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
        )
        tracker(state).acknowledge(manifest_spec, record)

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
            state.setdefault("source_errors", {}).pop("historical_reconciliation", None)
            state["chain_repaired_at_utc"] = recon["last_manifest_utc"]
            if force_wide and holder_integrity_degraded(state):
                recon["integrity_manifest_clean_at_utc"] = recon["last_manifest_utc"]
            save(STATE, state)
            return False

        max_days = int(policy["max_repair_days_per_run"])
        repair_days = mismatches[:max_days]
        changed = False
        repaired = []

        repair_spec = {
            "key": "historical_repair",
            "query_id": int(policy["repair_query_id"]),
            "max_run_credits": float(policy["repair_max_run_credits"]),
            "output": "state/v3_transfer_repair_latest.json",
            "performance": CONFIG["performance"],
        }

        today = now().date()
        for day_text in repair_days:
            day = datetime.fromisoformat(day_text).date()
            days_ago = (today - day).days
            if days_ago < 1 or days_ago > lookback_days:
                raise RuntimeError(f"Refusing out-of-window historical repair for {day_text}")

            before = canonical_transfer_manifest(lookback_days).get(day_text)
            repair_record = execute(
                repair_spec,
                {
                    "oldest_days_ago": days_ago,
                    "newest_days_ago": days_ago,
                },
                state=state,
                timeout_seconds=CONFIG.get("source_execution_timeout_seconds", 240),
            )
            export_source(
                repair_record,
                repair_spec,
                int(policy["repair_max_export_points"]),
                max_bytes=int(policy["repair_max_export_bytes"]),
            )
            reduce_atomically(ROOT / repair_spec["output"])
            tracker(state).acknowledge(repair_spec, repair_record)

            after = canonical_transfer_manifest(lookback_days).get(day_text)
            expected = remote.get(day_text)
            if expected != after:
                raise RuntimeError(
                    f"Historical repair verification failed for {day_text}: "
                    "canonical manifest still differs from Dune."
                )
            if before != after:
                changed = True
            repaired.append({
                "day": day_text,
                "execution_id": repair_record["execution_id"],
                "cost_credits": repair_record["execution_cost_credits"],
            })

        remaining = manifest_mismatches(remote, canonical_transfer_manifest(lookback_days))
        recon["last_repaired_days"] = repaired
        recon["pending_mismatch_days"] = remaining
        recon["last_completed_utc"] = stamp()
        recon["last_mode"] = "selective_repair"
        recon["last_lookback_hours"] = lookback_days * 24
        state["chain_repaired_at_utc"] = recon["last_completed_utc"]
        state["chain_completed_at_utc"] = recon["last_completed_utc"]

        sync_holder_integrity_error(state)

        if remaining:
            state.setdefault("source_errors", {})["historical_reconciliation"] = {
                "error": f"{len(remaining)} manifest mismatch day(s) remain after bounded repair.",
                "failed_at_utc": stamp(),
                "pending_days": remaining,
                "automatic_recheck": True,
            }
        else:
            state.setdefault("source_errors", {}).pop("historical_reconciliation", None)
            recon.pop("pending_mismatch_days", None)

        if changed:
            forced = set(state.get("forced_presentations", []))
            forced.update(policy.get("dependent_presentations", []))
            state["forced_presentations"] = sorted(forced)
            recon["presentation_invalidated_at_utc"] = stamp()

        save(STATE, state)
        return changed

    except ExecutionPending as error:
        record_source_issue(state, "historical_reconciliation", error, unresolved=True)
        return False
    except (SubmissionRejected, ExecutionCostExceeded, ExecutionFailed, RuntimeError) as error:
        record_source_issue(state, "historical_reconciliation", error)
        return False


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
    args = ["bash", "scripts/v3_publish_live_state.sh"]
    if state_only:
        args.append("--refresh-state-only")
    subprocess.run(args, cwd=ROOT, check=True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true", help="Explicitly clear a reviewed pause; never used by cron")
    parser.add_argument("--benchmark", action="store_true", help="Measure query costs without changing canonical state")
    parser.add_argument("--benchmark-sources", action="store_true", help="Measure bounded sources only")
    parser.add_argument("--benchmark-batches", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--benchmark-repetitions", type=int, choices=range(1, 7), default=1)
    args = parser.parse_args()
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
                    benchmark_state = load(STATE, {"schema_version": 1, "queries": {}})
                    records.append({"batch": batch, "repetition": repetition,
                        "key": spec["key"], "lookback_hours": lookback,
                        **execute(spec, params, enforce_cap=False, state=benchmark_state)})
                    tracker(benchmark_state).acknowledge(spec, records[-1])
                    save(ROOT / "state/v3_credit_benchmark.json", {"generated_at_utc": stamp(), "performance": CONFIG["performance"], "queries": records})
                    print(batch, repetition, spec["key"], records[-1]["execution_cost_credits"], "credits", flush=True)
                    if records[-1]["execution_cost_credits"] > 2 or sum(r["execution_cost_credits"] for r in records) > 30:
                        raise RuntimeError("Benchmark safety allowance reached; costs saved for review.")
        return 0
    state = load(STATE, {"schema_version": 1, "queries": {}})
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
                publish(state_only=True)
        message = "Refresh remains paused after a previous failure; no queries submitted."
        if os.environ.get("EVENT") in {"schedule", "push"}:
            print(f"::warning::{message}")
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with open(summary, "a") as report:
                    report.write("## XNET refresh paused\n\n")
                    report.write(message + "\n\n")
                    report.write("Last error: " + str(state.get("last_error", "unknown")) + "\n")
            return 0
        raise SystemExit("Refresh paused after previous failure; review state and manually resume.")
    state.update({"paused": False, "started_at_utc": stamp()})
    for source in CONFIG["sources"]:
        if not source.get("enabled", True):
            state["queries"].pop(source["key"], None)
    consumed_sources = []
    try:
        billing = usage_guard()
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
                except ExecutionPending as error:
                    record_source_issue(state, spec["key"], error, unresolved=True)
                    continue
                except (SubmissionRejected, ExecutionCostExceeded, ExecutionFailed) as error:
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
                remaining = export_source(
                    record,
                    spec,
                    remaining,
                    max_bytes=export_max_bytes,
                )
                source_modes[spec["key"]] = actual_mode
                source_lookbacks[spec["key"]] = int(lookback)

                state["queries"][
                    spec["key"]
                ] = record

                paths[
                    spec["key"]
                ] = (
                    ROOT
                    / spec["output"]
                )
                successful_sources.append(spec)

                save(
                    STATE,
                    state,
                )

            # The transfer source is the canonical reducer clock and remains
            # on the 30-minute cadence. BBB DEX is deliberately daily; when
            # it is not due, its canonical CSV and derived state are preserved.
            if "xnet_transfers" in paths:
                reduce_atomically(
                    paths["xnet_transfers"],
                    paths.get("bbb_dex"),
                )
                holder_quality = sync_holder_integrity_error(state)
                state["chain_completed_at_utc"] = stamp()

                transfer_mode = source_modes.get("xnet_transfers")
                if transfer_mode in {"daily", "approved_backfill", "integrity"}:
                    state["chain_repaired_at_utc"] = state["chain_completed_at_utc"]
                    recon = state.setdefault("reconciliation", {})
                    recon.update({
                        "last_mode": transfer_mode,
                        "last_completed_utc": state["chain_completed_at_utc"],
                        "last_lookback_hours": source_lookbacks["xnet_transfers"],
                    })
                    if transfer_mode == "approved_backfill":
                        recon["generation"] = int(
                            CONFIG.get("reconciliation_generation", 0)
                        )

                    if holder_quality.get("status") == "HEALTHY":
                        recon["integrity_attempts"] = 0
                        recon.pop("last_integrity_attempt_utc", None)
                    elif (
                        holder_quality.get("status") == "DEGRADED"
                        and transfer_mode in {"approved_backfill", "integrity"}
                    ):
                        recon["integrity_attempts"] = int(
                            recon.get("integrity_attempts", 0)
                        ) + 1
                        recon["last_integrity_attempt_utc"] = (
                            state["chain_completed_at_utc"]
                        )

                consumed_sources = successful_sources

            health_sources = {
                spec["key"]:
                    state.get(
                        "queries",
                        {},
                    ).get(
                        spec["key"],
                        {},
                    )
                for spec in active_sources
                if state.get(
                    "queries",
                    {},
                ).get(
                    spec["key"]
                )
            }

            save(
                HEALTH,
                {
                    "schema_version": 2,
                    "last_refresh_completed_utc":
                        state.get(
                            "chain_completed_at_utc"
                        ),
                    "status": "DEGRADED" if state.get("source_errors") else "HEALTHY",
                    "cadence_minutes":
                        CONFIG[
                            "chain_cadence_minutes"
                        ],
                    "paused_due_to_cost": False,
                    "sources":
                        health_sources,
                    "degraded_sources": state.get("source_errors", {}),
                    "reconciliation": state.get("reconciliation", {}),
                    "automatic_retry": True,
                },
            )
        historical_changed = reconcile_historical_manifest(state, billing)
        if historical_changed:
            sync_holder_integrity_error(state)

        run("v3_build_chain_snapshot.py")
        try:
            run("v3_collect_market.py")
        except subprocess.CalledProcessError as error:
            if not (ROOT / "data/current/xnet_market_state.json").exists():
                raise  # No verified fallback available.
            state.setdefault("source_errors", {})["market"] = {
                "error": str(error),
                "failed_at_utc": stamp(),
                "automatic_recheck": True,
            }
            print("::warning::Market refresh failed; keeping last-good market data.", flush=True)
        else:
            state.setdefault("source_errors", {}).pop("market", None)

        # Publish one coherent payload for Dune LiveFetch. The component files
        # remain available for provenance/debugging, but the live Current
        # Snapshot reads this single bundle so it cannot straddle Git commits.
        run("v3_build_live_bundle.py")
        save(STATE, state)
        publish()
        # Canonical data, rebuilt snapshots and their clock are now durable.
        # A killed runner before this point can replay the same source result.
        for spec in consumed_sources:
            tracker(state).acknowledge(spec, state["queries"][spec["key"]])
            state.setdefault("source_errors", {}).pop(spec["key"], None)
        # Any failure to publish/reduce stays global fail-closed, never masked.
        save(STATE, state)
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
            try:
                # Do not download presentation rows: charts use Dune's cached
                # executions. Presentation failures are fail-soft because the
                # previous successful Dune result remains valid.
                state["queries"][spec["key"]] = execute(
                    spec,
                    timeout_seconds=presentation_timeout,
                    state=state,
                )
                tracker(state).acknowledge(spec, state["queries"][spec["key"]])
                state.get("presentation_errors", {}).pop(spec["key"], None)
                forced.discard(spec["key"])
                state["forced_presentations"] = sorted(forced)
            except Exception as error:
                presentation_errors[spec["key"]] = {
                    "failed_at_utc": stamp(),
                    "error": str(error),
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
        state.update({"completed_at_utc": stamp(), "last_error": None, "billing": usage_guard()})
        # Health is reconciled after successful source commits, not before them.
        if HEALTH.exists():
            health = load(HEALTH, {})
            health["status"] = "DEGRADED" if state.get("source_errors") else "HEALTHY"
            health["degraded_sources"] = state.get("source_errors", {})
            health["reconciliation"] = state.get("reconciliation", {})
            health["automatic_retry"] = True
            save(HEALTH, health)
        save(STATE, state)
        publish()
        return 0
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
            publish()
        except Exception:
            print("Could not persist pause. Review workflow artifact before resuming.", file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
