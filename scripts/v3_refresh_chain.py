#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

CONFIG_PATH = ROOT / "config/v3_pipeline.json"

HEALTH_PATH = (
    ROOT / "data/current/xnet_chain_health.json"
)

TRANSFER_RESULT = (
    ROOT / "state/v3_transfer_hot_latest.json"
)

BBB_RESULT = (
    ROOT / "state/v3_bbb_hot_latest.json"
)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        json.dumps(obj, indent=2) + "\n"
    )

    tmp.replace(path)


def run_dune_query(
    qid: int,
    output_path: Path,
) -> dict:

    proc = subprocess.run(
        [
            "dune",
            "query",
            "run",
            str(qid),
            "--limit",
            "10000",
            "-o",
            "json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"Dune query {qid} failed.\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Could not parse Dune JSON for {qid}:\n"
            f"{proc.stdout}"
        ) from e

    if result.get("state") != "QUERY_STATE_COMPLETED":
        raise RuntimeError(
            f"Dune query {qid} did not complete: "
            f"{result.get('state')}"
        )

    write_json(output_path, result)

    return result


def dune_execution_cost(qid: int) -> tuple[str, float]:

    key = os.environ.get("DUNE_API_KEY")

    if not key:
        raise RuntimeError(
            "DUNE_API_KEY is not set"
        )

    headers = {
        "X-Dune-API-Key": key,
    }

    req = urllib.request.Request(
        (
            "https://api.dune.com/api/v1/query/"
            f"{qid}/results?limit=1"
        ),
        headers=headers,
    )

    with urllib.request.urlopen(
        req,
        timeout=30,
    ) as r:
        latest = json.load(r)

    execution_id = latest.get("execution_id")

    if not execution_id:
        raise RuntimeError(
            f"No execution_id returned for query {qid}"
        )

    req = urllib.request.Request(
        (
            "https://api.dune.com/api/v1/execution/"
            f"{execution_id}/status"
        ),
        headers=headers,
    )

    with urllib.request.urlopen(
        req,
        timeout=30,
    ) as r:
        status = json.load(r)

    cost = status.get("execution_cost_credits")

    if cost is None:
        raise RuntimeError(
            f"No execution cost returned for {qid}"
        )

    return execution_id, float(cost)


def result_metrics(result: dict) -> dict:

    meta = (
        result
        .get("result", {})
        .get("metadata", {})
    )

    rows = (
        result
        .get("result", {})
        .get("rows", [])
    )

    row_count = meta.get(
        "total_row_count",
        meta.get(
            "row_count",
            len(rows),
        ),
    )

    result_bytes = meta.get(
        "total_result_set_bytes",
        meta.get(
            "result_set_bytes",
            0,
        ),
    )

    return {
        "row_count": int(row_count or 0),
        "result_bytes": int(result_bytes or 0),
    }


def run_reducer() -> None:

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/v3_reduce_chain.py",
            "--transfer-result",
            str(TRANSFER_RESULT),
            "--bbb-result",
            str(BBB_RESULT),
        ],
        cwd=ROOT,
        text=True,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            "v3_reduce_chain.py failed"
        )


def main() -> int:

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--force",
        action="store_true",
        help=(
            "Run even if the previous refresh "
            "paused because of a cost anomaly."
        ),
    )

    args = ap.parse_args()

    config = load_json(CONFIG_PATH)

    transfer_cfg = (
        config["sources"]["xnet_transfers"]
    )

    bbb_cfg = (
        config["sources"]["bbb_dex"]
    )

    transfer_qid = int(
        transfer_cfg["query_id"]
    )

    bbb_qid = int(
        bbb_cfg["query_id"]
    )

    transfer_limit = float(
        transfer_cfg[
            "max_expected_run_credits"
        ]
    )

    bbb_limit = float(
        bbb_cfg[
            "max_expected_run_credits"
        ]
    )

    if HEALTH_PATH.exists() and not args.force:

        previous = load_json(HEALTH_PATH)

        if previous.get(
            "paused_due_to_cost",
            False,
        ):
            print(
                "REFRESH PAUSED: previous run "
                "breached a configured cost limit."
            )
            print(
                "Review data/current/"
                "xnet_chain_health.json"
            )
            print(
                "Use --force only after review."
            )
            return 2

    started = utc_now()

    try:

        print(
            f"[{started}] Running transfer hot query "
            f"{transfer_qid}"
        )

        transfer_result = run_dune_query(
            transfer_qid,
            TRANSFER_RESULT,
        )

        transfer_execution, transfer_cost = (
            dune_execution_cost(
                transfer_qid
            )
        )

        tm = result_metrics(
            transfer_result
        )

        print(
            "Transfer:",
            f"rows={tm['row_count']}",
            f"cost={transfer_cost:.9f}",
        )

        print(
            f"Running BBB hot query {bbb_qid}"
        )

        bbb_result = run_dune_query(
            bbb_qid,
            BBB_RESULT,
        )

        bbb_execution, bbb_cost = (
            dune_execution_cost(
                bbb_qid
            )
        )

        bm = result_metrics(
            bbb_result
        )

        print(
            "BBB:",
            f"rows={bm['row_count']}",
            f"cost={bbb_cost:.9f}",
        )

        print(
            "Reducing canonical chain state..."
        )

        run_reducer()

        snapshot_proc = subprocess.run(
            [
                sys.executable,
                "scripts/v3_build_chain_snapshot.py",
            ],
            cwd=ROOT,
            text=True,
        )

        if snapshot_proc.returncode != 0:
            raise RuntimeError(
                "v3_build_chain_snapshot.py failed"
            )

        breaches = []

        if transfer_cost > transfer_limit:
            breaches.append(
                {
                    "source":
                        "xnet_transfers",
                    "actual_credits":
                        transfer_cost,
                    "limit_credits":
                        transfer_limit,
                }
            )

        if bbb_cost > bbb_limit:
            breaches.append(
                {
                    "source":
                        "bbb_dex",
                    "actual_credits":
                        bbb_cost,
                    "limit_credits":
                        bbb_limit,
                }
            )

        health = {
            "schema_version": 1,
            "last_refresh_started_utc":
                started,
            "last_refresh_completed_utc":
                utc_now(),
            "status":
                (
                    "COST_PAUSED"
                    if breaches
                    else "HEALTHY"
                ),
            "paused_due_to_cost":
                bool(breaches),
            "cadence_minutes": 30,
            "sources": {
                "xnet_transfers": {
                    "query_id":
                        transfer_qid,
                    "execution_id":
                        transfer_execution,
                    "rows":
                        tm["row_count"],
                    "result_bytes":
                        tm["result_bytes"],
                    "execution_cost_credits":
                        transfer_cost,
                    "max_expected_run_credits":
                        transfer_limit,
                },
                "bbb_dex": {
                    "query_id":
                        bbb_qid,
                    "execution_id":
                        bbb_execution,
                    "rows":
                        bm["row_count"],
                    "result_bytes":
                        bm["result_bytes"],
                    "execution_cost_credits":
                        bbb_cost,
                    "max_expected_run_credits":
                        bbb_limit,
                },
            },
            "cost_breaches":
                breaches,
            "automatic_retry":
                False,
        }

        write_json(
            HEALTH_PATH,
            health,
        )

        print()
        print(
            "=== CHAIN REFRESH COMPLETE ==="
        )

        print(
            "Transfer cost:",
            f"{transfer_cost:.9f}",
        )

        print(
            "BBB cost:     ",
            f"{bbb_cost:.9f}",
        )

        print(
            "Combined:     ",
            f"{transfer_cost + bbb_cost:.9f}",
        )

        if breaches:
            print()
            print(
                "COST GUARD TRIGGERED."
            )
            print(
                "The data from this run was retained, "
                "but future automatic refreshes will "
                "stop until reviewed."
            )
            return 2

        return 0

    except Exception as e:

        failure = {
            "schema_version": 1,
            "last_refresh_started_utc":
                started,
            "last_refresh_failed_utc":
                utc_now(),
            "status":
                "FAILED",
            "paused_due_to_cost":
                False,
            "error":
                str(e),
            "automatic_retry":
                False,
        }

        write_json(
            HEALTH_PATH,
            failure,
        )

        print(
            f"REFRESH FAILED: {e}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
