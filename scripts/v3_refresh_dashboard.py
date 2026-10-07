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
from v3_execution_tracker import ExecutionPending, ExecutionTracker

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
        usage_guard()
        health = load(HEALTH, {})
        last = state.get("chain_completed_at_utc") or health.get("last_refresh_completed_utc")
        repair = due(state.get("chain_repaired_at_utc"), CONFIG["repair_cadence_minutes"])

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
            != int(spec.get("lookback_hours", 2))
            for spec in CONFIG["sources"]
            if spec.get("enabled", True)
        )

        if (
            source_config_changed
            or repair
            or due(last, CONFIG["chain_cadence_minutes"])
            or any(s["key"] in state.get("pending_executions", {}) for s in CONFIG["sources"])
        ):
            remaining = (
                CONFIG["max_repair_export_points"]
                if repair
                else CONFIG["max_export_points_per_run"]
            )
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
                    != configured_lookback
                )

                preferred_hour = spec.get(
                    "preferred_hour_utc"
                )

                in_preferred_hour = (
                    preferred_hour is None
                    or now().hour
                    == int(preferred_hour)
                )

                repair_for_source = (
                    repair
                    and spec.get(
                        "repair_on_global_cycle",
                        True,
                    )
                )

                if (
                    lookback_changed
                    or repair_for_source
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

            # A recovered BBB result must be reduced even if the transfer clock
            # isn't due yet. The unchanged canonical CSV remains deduplicated.
            if any(s["key"] == "bbb_dex" for s in due_sources):
                transfer = next((s for s in active_sources if s["key"] == "xnet_transfers"), None)
                if transfer and transfer not in due_sources:
                    due_sources.insert(0, transfer)
            paths = {}

            for spec in due_sources:
                previous = (
                    state.get("queries", {})
                    .get(spec["key"], {})
                )
                previous_completed = previous.get(
                    "completed_at_utc"
                )

                repair_for_source = (
                    repair
                    and spec.get(
                        "repair_on_global_cycle",
                        True,
                    )
                )

                pending = state.get("pending_executions", {}).get(spec["key"])
                if pending:
                    lookback = pending["query_parameters"].get("lookback_hours", spec.get("lookback_hours", 2))
                elif repair_for_source:
                    lookback = int(
                        spec.get(
                            "repair_lookback_hours",
                            CONFIG["repair_lookback_hours"],
                        )
                    )
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
                        raise RuntimeError(
                            f"{spec['key']} canonical collection gap "
                            "exceeds catch-up limit; reviewed repair required."
                        )
                else:
                    lookback = int(
                        spec.get(
                            "lookback_hours",
                            2,
                        )
                    )

                record = execute(
                    spec,
                    {
                        "lookback_hours":
                            lookback,
                    },
                    state=state,
                )

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

                remaining = export_source(
                    record,
                    spec,
                    remaining,
                )

                state["queries"][
                    spec["key"]
                ] = record

                paths[
                    spec["key"]
                ] = (
                    ROOT
                    / spec["output"]
                )

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
                state["chain_completed_at_utc"] = stamp()

                if repair:
                    state["chain_repaired_at_utc"] = (
                        state[
                            "chain_completed_at_utc"
                        ]
                    )

                consumed_sources = due_sources

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
                    "status": "HEALTHY",
                    "cadence_minutes":
                        CONFIG[
                            "chain_cadence_minutes"
                        ],
                    "paused_due_to_cost": False,
                    "sources":
                        health_sources,
                    "automatic_retry": False,
                },
            )
        run("v3_build_chain_snapshot.py")
        run("v3_collect_market.py")
        save(STATE, state)
        publish()
        # Canonical data, rebuilt snapshots and their clock are now durable.
        # A killed runner before this point can replay the same source result.
        for spec in consumed_sources:
            tracker(state).acknowledge(spec, state["queries"][spec["key"]])
        due_presentation = []
        for spec in CONFIG["presentation"]:
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
        # Persist pause without relabelling last-good chain/source observations.
        try:
            publish()
        except Exception:
            print("Could not persist pause. Review workflow artifact before resuming.", file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
