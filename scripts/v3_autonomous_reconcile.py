"""Runtime integration for scope-bound, append-only UTC-day recovery.

No SQL mutation, instruction guessing, replacement, seed reset or amount repair.
The injected runtime owns provider calls, standing quotes and exact-predecessor
publication. A failed/ambiguous publication is never compensated by a stale write.
"""
from copy import deepcopy
import csv
from datetime import timedelta
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from v3_additive_day import validate_additive_day, AdditiveDayRejected
from v3_credit_budget import parse_instant
from v3_live_state_store import StateError
from v3_reduce_chain import TRANSFER_FIELDS, parse_ts
from v3_transfer_instruction_recovery import prepare_proofs


def hot_catchup_limit(config):
    # An old outlier makes wide hot windows a poor recovery strategy. Exact
    # one-day scopes use their own observed cost family and existing authority.
    return min(12, int(config.get("max_catchup_hours", 168)))


def owner_seed_day(root, load):
    metadata=load(root / "data/xnet_owner_balances_seed_metadata.json",{})
    cutoff=metadata.get("latest_balance_event_time")
    if not cutoff:
        raise StateError("Owner seed cutoff is required before pricing day recovery")
    return parse_ts(cutoff).date().isoformat()


def route_seeded_mismatches(state, days, seed_day):
    """Retain pre/seed-day anomalies as advisory evidence; never pay to replay them."""
    seeded=[day for day in days if day <= seed_day]
    if seeded:
        recon=state.setdefault("reconciliation",{})
        recon["seeded_mismatch_days"]=sorted(set(recon.get("seeded_mismatch_days",[])) | set(seeded))
        recon["seeded_mismatch_action"]="Retained for reviewed seed-aware reconciliation; no automatic full-day replay"
    return [day for day in days if day > seed_day]


def repair_days(state, config, now, *, seed_cutoff=None):
    pending = state.get("pending_executions", {})
    days = {key.removeprefix("day_repair:") for key in pending if key.startswith("day_repair:")}
    days.update(state.get("reconciliation", {}).get("pending_mismatch_days", []))
    previous = state.get("queries", {}).get("xnet_transfers", {})
    covered = previous.get("continuous_coverage_end_utc") or previous.get("completed_at_utc")
    outside_authorized_horizon = False
    if covered:
        start = parse_instant(covered, "transfer coverage")
        outside_authorized_horizon = (now.date()-start.date()).days > 60
        if not outside_authorized_horizon and (now - start).total_seconds() > hot_catchup_limit(config) * 3600:
            start_day = max(start.date(), now.date() - timedelta(days=60))
            while start_day < now.date():
                days.add(start_day.isoformat())
                start_day += timedelta(days=1)
            days.add(now.date().isoformat())
    primary_days = set(days)
    from v3_holder_seed_review import held as seed_review_held
    if ("holder_integrity" in state.get("source_errors", {}) and not seed_review_held(state)
            and not state.get("holder_reconciliation",{}).get("active_baseline_id")):
        # An outflow can arrive before its older indexed inflow, including on
        # TODAY. Recheck recent days first, not a week of old healthy days before
        # the affected current day. Successful days still retain their cooldown.
        days.update((now.date() - timedelta(days=i)).isoformat() for i in range(8))
    issues = state.get("additive_day_recovery", {})
    result = []
    for day in sorted(days,key=lambda value:(value not in primary_days,
            value if value in primary_days else -parse_instant(value+"T00:00:00Z","repair day").timestamp())):
        if outside_authorized_horizon and "day_repair:"+day not in pending:
            continue  # Cannot buy repeated recent-day scans to bridge an unapproved hole.
        if seed_cutoff is not None and day <= seed_cutoff:
            continue
        target = parse_instant(day + "T00:00:00Z", "repair day")
        if target.date() > now.date() or (now.date() - target.date()).days > 60:
            continue
        issue = issues.get(day, {})
        retry = issue.get("next_retry_at_utc")
        if retry and now < parse_instant(retry, "day retry"):
            continue
        result.append(day)
    return result


def _publish_day(runtime, state, spec, record, result, absolute_day):
    root = runtime.ROOT
    pending = state["pending_executions"][spec["key"]]
    approval = pending.get("operation_approval", {})
    if (pending.get("execution_id") != record["execution_id"]
            or pending.get("query_id") != record["query_id"]
            or approval.get("absolute_day") != absolute_day
            or approval.get("sql_sha256") != runtime.CONFIG["autonomous_operation"]["queries"][str(record["query_id"])]["sql_sha256"]
            or approval.get("query_parameters") != record.get("query_parameters")):
        raise AdditiveDayRejected("Original execution lacks its immutable UTC-day/SQL approval")
    # CURRENT_DATE belongs to the actual engine start, not the current process
    # date. This also proves the scope of an empty-day result after a restart.
    started = parse_instant(record.get("execution_started_at"), "provider execution start")
    upper = min(parse_instant(absolute_day+"T00:00:00Z","day") + timedelta(days=1), started)
    original_offset = record["query_parameters"]["oldest_days_ago"]
    if (started.date() - timedelta(days=original_offset)).isoformat() != absolute_day:
        raise AdditiveDayRejected("Relative UTC-day scope changed at the provider execution start")
    meta = record.get("result_metadata", {})
    rows = result.get("result", {}).get("rows")
    if (result.get("state") != "QUERY_STATE_COMPLETED" or not isinstance(rows, list)
            or len(rows) != meta.get("total_row_count") or result.get("next_uri") or result.get("next_offset")):
        raise AdditiveDayRejected("Day result is not a complete provider response")
    if any(parse_ts(row["block_time"]) > started for row in rows):
        raise AdditiveDayRejected("Provider row is later than its captured execution snapshot")
    ledger = root / "data/canonical/xnet_transfers.csv"
    before = ledger.read_bytes()
    canonical = list(csv.DictReader(io.StringIO(before.decode())))
    seed_path = root / "data/xnet_owner_balances_seed_metadata.json"
    seed = runtime.load(seed_path, {})
    cutoff = seed.get("latest_balance_event_time")
    cutoff_day = parse_ts(cutoff).date().isoformat() if cutoff else None
    proofs = prepare_proofs(state, canonical, rows)
    validated = validate_additive_day(canonical, rows, absolute_day,
        execution_id=record["execution_id"], sql_sha256=approval["sql_sha256"], seed_cutoff=cutoff_day,
        transaction_proofs=proofs)
    additions = validated["additions"]
    source_receipt = validated["receipt"]
    before_state = deepcopy(state)
    publishing = False
    with tempfile.TemporaryDirectory() as directory:
        backup = Path(directory) / "data"
        shutil.copytree(root / "data", backup)
        try:
            output = io.StringIO(newline="")
            csv.DictWriter(output, fieldnames=TRANSFER_FIELDS).writerows(additions)
            ledger.write_bytes(before + output.getvalue().encode())
            expected_hash = hashlib.sha256(ledger.read_bytes()).hexdigest()
            runtime.run("v3_reduce_chain.py", "--rebuild-canonical", "--allow-stale-holders")
            if not ledger.read_bytes().startswith(before) or hashlib.sha256(ledger.read_bytes()).hexdigest() != expected_hash:
                raise AdditiveDayRejected("Reducer rewrote the accepted canonical prefix")
            unresolved_indexer_days=set()
            if state.get("indexer_lag_watch",{}).get("witnesses"):
                from v3_indexer_lag import reconcile_witnesses
                unresolved_indexer_days=reconcile_witnesses(state,canonical+additions)
            quality = runtime.load(root / "data/current/xnet_holder_integrity.json", {})
            receipt = {**source_receipt, "canonical_before_sha256":hashlib.sha256(before).hexdigest(),
                "canonical_after_sha256":expected_hash, "provider_artifact_sha256":hashlib.sha256(runtime.encoded(result)).hexdigest(),
                "seed_metadata_sha256":hashlib.sha256(seed_path.read_bytes()).hexdigest() if seed_path.exists() else None,
                "original_record_bytes_preserved":True, "holder_status":quality.get("status"),
                "accepted_at_utc":runtime.stamp(), "collection_completed_at_utc":record.get("completed_at_utc"),
                "provider_execution_started_at_utc":record["execution_started_at"],
                "verified_coverage_end_utc":upper.isoformat().replace("+00:00","Z"),
                "known_indexer_witnesses_unresolved":absolute_day in unresolved_indexer_days,
                "full_current_day_claimed":False,
                "instruction_proof_sha256s":{tx:entry["capture_sha256"] for tx,entry in proofs.items()
                    if any(row["tx_id"]==tx for row in rows)}}
            identity = hashlib.sha256(json.dumps(receipt,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
            state.setdefault("autonomous_day_receipts", {})[identity] = receipt
            runtime.stage_consumption(state,spec,record,"verified_additive_utc_day")
            recon = state.setdefault("reconciliation", {})
            recon["pending_mismatch_days"] = [day for day in recon.get("pending_mismatch_days",[])
                if day != absolute_day or day in unresolved_indexer_days]
            if not recon["pending_mismatch_days"] and "historical_repair" not in state.get("pending_executions", {}):
                recon.pop("repair_manual_review_required",None)
            state.setdefault("additive_day_recovery", {})[absolute_day] = {
                "status":"awaiting_indexer" if absolute_day in unresolved_indexer_days else "accepted",
                "receipt_sha256":identity, "execution_id":record["execution_id"],
                "indexer_misses":(state.get("additive_day_recovery",{}).get(absolute_day,{}).get("indexer_misses",0)+1
                    if absolute_day in unresolved_indexer_days else 0),
                "added_rows":len(additions), "next_retry_at_utc":(runtime.now()+timedelta(
                    hours=min(24,6*2**min(state.get("additive_day_recovery",{}).get(absolute_day,{}).get("indexer_misses",0),2)))
                    if absolute_day in unresolved_indexer_days else runtime.now()+timedelta(days=1)
                    if started.date().isoformat()>absolute_day else runtime.now()+timedelta(hours=2)).isoformat().replace("+00:00","Z")}
            previous = state["queries"].get("xnet_transfers", {})
            old_coverage = previous.get("continuous_coverage_end_utc") or previous.get("completed_at_utc")
            # Only advance a contiguous frontier, never jump over an unproven day.
            if old_coverage:
                old = parse_instant(old_coverage,"old coverage")
                if old.date().isoformat() == absolute_day and upper > old and absolute_day not in unresolved_indexer_days:
                    state["queries"]["xnet_transfers"] = {**record,
                        "continuous_coverage_end_utc":upper.isoformat().replace("+00:00","Z"),
                        "additive_day_receipt_sha256":identity}
                    state["chain_completed_at_utc"] = runtime.stamp()
                    issue=state.setdefault("source_errors",{}).get("xnet_transfers",{})
                    if issue.get("retry_kind")=="bounded_day_catchup":
                        state["source_errors"].pop("xnet_transfers",None)
            runtime.sync_holder_integrity_error(state)
            runtime.run("v3_build_chain_snapshot.py")
            runtime.run("v3_build_live_bundle.py")
            runtime.run("v3_record_bbb_usdc_history.py")
            runtime.run("v3_build_presentation_history.py")
            runtime.write_operational_health(state)
            runtime.save(runtime.STATE,state)
            if additions:
                state["forced_presentations"] = sorted(set(state.get("forced_presentations",[]))
                    | {"current","burns","tokenomics","holders"})
                runtime.save(runtime.STATE,state)
            publishing = True
            runtime.publish()
            return bool(additions)
        except Exception:
            if not publishing:
                shutil.rmtree(root / "data")
                shutil.copytree(backup,root / "data")
                state.clear()
                state.update(before_state)
            raise


def recover_additive_days(runtime, state, billing):
    """At most the explicitly authorized count per run; all retries are newly quoted."""
    if billing is None:
        return False
    changed = False
    cutoff=owner_seed_day(runtime.ROOT,runtime.load)
    days = repair_days(state,runtime.CONFIG,runtime.now(),seed_cutoff=cutoff)
    limit = runtime.CONFIG["autonomous_operation"]["max_day_repairs_per_run"]
    for day in days[:limit]:
        if day not in repair_days(state,runtime.CONFIG,runtime.now(),seed_cutoff=cutoff):
            continue  # A prior repair may already have restored holder integrity.
        key = "day_repair:" + day
        pending = state.get("pending_executions",{}).get(key)
        offset = (runtime.now().date() - parse_instant(day+"T00:00:00Z","day").date()).days
        params = pending["query_parameters"] if pending else {"oldest_days_ago":offset,"newest_days_ago":offset}
        spec = {"key":key,"query_id":runtime.CONFIG["reconciliation"]["repair_query_id"],
            "performance":runtime.CONFIG["performance"],"max_run_credits":runtime.CONFIG["reconciliation"]["repair_max_run_credits"],
            "output":"state/v3_autonomous_day_latest.json","absolute_day":day}
        try:
            record = runtime.execute(spec,params,state=state)
            runtime.export_source(record,spec,runtime.CONFIG["reconciliation"]["repair_max_export_points"],
                max_bytes=runtime.CONFIG["reconciliation"]["repair_max_export_bytes"],state=state)
            result = runtime.load(runtime.ROOT / spec["output"],{})
            changed = _publish_day(runtime,state,spec,record,result,day) or changed
        except StateError:
            raise
        except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as error:
            issue = state.setdefault("additive_day_recovery",{}).get(day,{})
            failures = issue.get("consecutive_failures",0)+1
            state["additive_day_recovery"][day] = {"status":"deferred", "error":str(error),
                "consecutive_failures":failures,"last_attempt_at_utc":runtime.stamp(),
                "next_retry_at_utc":(runtime.now()+timedelta(hours=min(24,2**min(failures,4)))).isoformat().replace("+00:00","Z")}
            runtime.checkpoint(state)
    return changed
