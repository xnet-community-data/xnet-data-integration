#!/usr/bin/env python3
"""Bounded sources, status-only chart refreshes, and a persistent spend guard."""
from __future__ import annotations
import argparse
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
        raise RuntimeError(f"Dune HTTP {error.code} at {path.split('?')[0]}: {message}; no automatic execution retry.") from None

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
    return {"credits_used": used, "guard_credits": limit, "period_start": period["start_date"], "period_end": period["end_date"]}

def execute(spec, params=None, enforce_cap=True):
    usage = usage_guard()
    body = {"performance": CONFIG["performance"]}
    if params:
        body["query_parameters"] = params
    execution = api(f'query/{spec["query_id"]}/execute', body)
    execution_id = execution["execution_id"]
    deadline = time.monotonic() + 600
    while True:
        status = api(f"execution/{execution_id}/status")
        if status.get("is_execution_finished") or status.get("state") in {
            "QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED"}:
            break
        if time.monotonic() > deadline:
            raise RuntimeError(f"Execution {execution_id} polling timeout; do not automatically resubmit.")
        time.sleep(4)
    cost = status.get("execution_cost_credits")
    if cost is None:
        raise RuntimeError(f"Execution {execution_id} has no cost metadata.")
    record = {"query_id": spec["query_id"], "execution_id": execution_id,
        "execution_cost_credits": float(cost), "completed_at_utc": stamp(),
        "execution_started_at": status.get("execution_started_at"),
        "execution_ended_at": status.get("execution_ended_at"),
        "result_metadata": status.get("result_metadata", {}), "billing": usage}
    if status["state"] != "QUERY_STATE_COMPLETED":
        raise RuntimeError(f"Execution {execution_id}: {status['state']}; no automatic retry.")
    if enforce_cap and float(cost) > spec["max_run_credits"]:
        raise RuntimeError(f"Query {spec['query_id']} cost {cost} exceeded {spec['max_run_credits']}; refresh paused.")
    return record

def export_source(record, spec, remaining):
    meta = record["result_metadata"]
    rows = int(meta.get("total_row_count", meta.get("row_count", 0)))
    if "column_names" not in meta or "total_row_count" not in meta:
        raise RuntimeError("Missing complete result metadata; source export stopped.")
    points = int(meta.get("datapoint_count", rows * len(meta["column_names"])))
    result_bytes = int(meta.get("total_result_set_bytes", meta.get("result_set_bytes", 0)))
    if result_bytes > CONFIG["max_export_bytes_per_run"]:
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

def reduce_atomically(transfer_path, bbb_path=None):
    # A failed reduction must preserve canonical data and all derived state.
    with tempfile.TemporaryDirectory() as directory:
        backup = Path(directory) / "data"
        shutil.copytree(ROOT / "data", backup)
        try:
            args = ["--transfer-result", str(transfer_path)]
            if bbb_path:
                args.extend(["--bbb-result", str(bbb_path)])
            run("v3_reduce_chain.py", *args)
        except Exception:
            shutil.rmtree(ROOT / "data")
            shutil.copytree(backup, ROOT / "data")
            raise

def publish():
    subprocess.run(["bash", "scripts/v3_publish_live_state.sh"], cwd=ROOT, check=True)

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
                    params = {"lookback_hours": 2} if spec in CONFIG["sources"] else None
                    records.append({"batch": batch, "repetition": repetition,
                        "key": spec["key"], "lookback_hours": 2 if params else None,
                        **execute(spec, params, enforce_cap=False)})
                    save(ROOT / "state/v3_credit_benchmark.json", {"generated_at_utc": stamp(), "performance": CONFIG["performance"], "queries": records})
                    print(batch, repetition, spec["key"], records[-1]["execution_cost_credits"], "credits", flush=True)
                    if records[-1]["execution_cost_credits"] > 2 or sum(r["execution_cost_credits"] for r in records) > 30:
                        raise RuntimeError("Benchmark safety allowance reached; costs saved for review.")
        return 0
    state = load(STATE, {"schema_version": 1, "queries": {}})
    if state.get("paused") and not args.resume:
        raise SystemExit("Refresh paused after previous failure; review state and manually resume.")
    state.update({"paused": False, "started_at_utc": stamp()})
    for source in CONFIG["sources"]:
        if not source.get("enabled", True):
            state["queries"].pop(source["key"], None)
    try:
        usage_guard()
        health = load(HEALTH, {})
        last = state.get("chain_completed_at_utc") or health.get("last_refresh_completed_utc")
        repair = due(state.get("chain_repaired_at_utc"), CONFIG["repair_cadence_minutes"])
        if due(last, CONFIG["chain_cadence_minutes"]):
            elapsed = (now() - datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() / 3600 if last else 2
            lookback = max(2, math.ceil(elapsed + 1))
            if lookback > CONFIG["max_catchup_hours"]:
                raise RuntimeError("Canonical collection gap exceeds catch-up limit; reviewed repair required.")
            if repair:
                lookback = CONFIG["repair_lookback_hours"]
            remaining = CONFIG["max_repair_export_points"] if repair else CONFIG["max_export_points_per_run"]
            records = []
            active_sources = [s for s in CONFIG["sources"] if s.get("enabled", True)]
            for spec in active_sources:
                record = execute(spec, {"lookback_hours": lookback})
                remaining = export_source(record, spec, remaining)
                state["queries"][spec["key"]] = record
                save(STATE, state)
                records.append(record)
            paths = {s["key"]: ROOT / s["output"] for s in active_sources}
            reduce_atomically(paths["xnet_transfers"], paths.get("bbb_dex"))
            state["chain_completed_at_utc"] = stamp()
            if repair:
                state["chain_repaired_at_utc"] = state["chain_completed_at_utc"]
            save(HEALTH, {"schema_version": 1, "last_refresh_completed_utc": state["chain_completed_at_utc"],
                "status": "HEALTHY", "cadence_minutes": CONFIG["chain_cadence_minutes"], "paused_due_to_cost": False,
                "sources": {s["key"]: r for s, r in zip(active_sources, records)}, "automatic_retry": False})
        run("v3_build_chain_snapshot.py")
        run("v3_collect_market.py")
        save(STATE, state)
        publish()
        for spec in CONFIG["presentation"]:
            last = state["queries"].get(spec["key"], {}).get("completed_at_utc")
            if not due(last, spec["cadence_minutes"]):
                continue
            # Do not download presentation rows: charts use Dune's cached executions.
            state["queries"][spec["key"]] = execute(spec)
            save(STATE, state)
        state.update({"completed_at_utc": stamp(), "last_error": None, "billing": usage_guard()})
        save(STATE, state)
        publish()
        return 0
    except Exception as error:
        state.update({"paused": True, "last_error": str(error), "failed_at_utc": stamp()})
        save(STATE, state)
        # Persist pause without relabelling last-good chain/source observations.
        try:
            publish()
        except Exception:
            print("Could not persist pause. Review workflow artifact before resuming.", file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
