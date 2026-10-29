#!/usr/bin/env python3
"""Read-only service verification. Never submit, cancel, retrieve or rerun Dune.

Workflow success is not source health. Check accepted collection coverage and
integrity independently of generated time or the age of the last quiet-chain
event. Nonzero exit alerts through GitHub Actions without pausing the runner.
"""
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path

from v3_credit_budget import parse_instant, BudgetInputError
from v3_live_state_store import validate_journal, StateError


def instant(value):
    try:
        return parse_instant(value, "accepted collection time")
    except (BudgetInputError, TypeError, ValueError):
        return None


def evaluate(state, config, quality, now, *, external=None):
    problems, warnings, domains = [], [], {}
    if state.get("paused"):
        problems.append("Global runner hold: " + str(state.get("last_error") or "paused"))
    for domain,hold in state.get("domain_holds",{}).items():
        if hold:problems.append(f"Explicit domain hold remains for {domain}: {hold.get('error')}")
    policy = config.get("autonomous_operation", {})
    if policy.get("enabled"):
        auth = state.get("standing_authorization", {})
        expected = hashlib.sha256(json.dumps(policy,sort_keys=True,separators=(",", ":")).encode()).hexdigest()
        if auth.get("policy_sha256") != expected or auth.get("approval_ref") != policy.get("approval_ref"):
            problems.append("Standing authority is missing or does not match deployed policy")
        if state.get("standing_authority_activation") and not state.get("standing_credit_budget",{}).get("periods"):
            problems.append("Previously activated financial risk book is missing; do not re-anchor it")
    pending = state.get("pending_executions", {})
    for key, work in pending.items():
        if not work.get("execution_id"):
            submitted=instant(work.get("submitted_at_utc"))
            # The independent observer can legitimately read the durable intent
            # between its pre-POST checkpoint and the accepted-ID checkpoint.
            if work.get("submission_error") or submitted is None or (now-submitted).total_seconds()>900:
                problems.append(f"Unresolved submission acceptance for {key}; do not resubmit speculatively")
            else:
                warnings.append(f"{key}: fresh submission intent is within the acceptance grace window")
    accounting = state.get("standing_credit_budget", {}).get("last_decision", {})
    multiplier = max(1, min(8, int(accounting.get("cadence_multiplier", 1))))
    if state.get("budget_decision", {}).get("status") == "blocked":
        warnings.append("New paid work is budget-blocked; runner must recheck without manual resume")
    for spec in [*config.get("sources", []), *config.get("presentation", [])]:
        if not spec.get("enabled", True):
            continue
        key = spec["key"]
        record = state.get("queries", {}).get(key, {})
        at = record.get("continuous_coverage_end_utc") or record.get("completed_at_utc")
        observed = instant(at)
        cadence = int(spec.get("cadence_minutes", config.get("chain_cadence_minutes",120)))
        if cadence <= 0:
            problems.append(f"{key}: configured cadence must be positive")
        if key not in {"bbb_dex","network","commercial"}:
            cadence *= multiplier
        maximum = max(6 * 3600, cadence * 60 * 2)
        age = (now-observed).total_seconds() if observed else None
        status = "healthy"
        if age is None or age < -300 or age > maximum:
            status = "stalled"
            problems.append(f"{key}: accepted collection/refreshed execution is missing, invalid or overdue ({at})")
        issue = state.get("source_errors", {}).get(key, {})
        if issue.get("manual_reconciliation_required"):
            status = "held"
            problems.append(f"{key}: nonautomatic source hold: {issue.get('error')}")
        elif issue:
            warnings.append(f"{key}: automatic recovery pending: {issue.get('error')}")
        presentation_issue = state.get("presentation_errors", {}).get(key)
        if presentation_issue:
            warnings.append(f"{key}: presentation retry pending: {presentation_issue.get('error')}")
        domains[key] = {"status":status,"accepted_coverage_or_refresh_utc":at,
                        "age_seconds":age,"maximum_delay_seconds":maximum}
    if quality.get("status") != "HEALTHY":
        problems.append("Holder integrity is not healthy: " + str(quality.get("error") or quality.get("status")))
    if state.get("source_errors",{}).get("indexer_lag"):
        problems.append("Finalized transfer witnesses remain absent/conflicting in indexed data; bounded day rechecks are pending")
    if state.get("holder_seed_review",{}).get("status")=="requires_review":
        problems.append(state["holder_seed_review"]["error"])
    baseline=state.get("holder_reconciliation",{}).get("active_baseline_id")
    if state.get("holder_reconciliation_authorization") and not baseline:
        problems.append("Finalized holder source is authorized but has no accepted complete inventory yet")
    if state.get("source_errors",{}).get("holder_snapshot"):
        warnings.append("Finalized holder source retry pending: "+str(state["source_errors"]["holder_snapshot"].get("error")))
    if baseline:
        from v3_holder_snapshot import AUTHORIZATION_REF, MAX_AGE_SECONDS
        at=quality.get("holder_data_as_of_utc")
        observed=instant(at)
        age=(now-observed).total_seconds() if observed else None
        healthy=(quality.get("baseline_id")==baseline and quality.get("mint_conservation_residual_raw")=="0"
                 and state.get("holder_reconciliation_authorization",{}).get("approval_ref")==AUTHORIZATION_REF
                 and age is not None and 0<=age<=MAX_AGE_SECONDS)
        if not healthy:problems.append("Independent finalized holder snapshot is stale or lacks matching source/conservation proof")
        domains["holder_inventory"]={"status":"healthy" if healthy else "stalled",
            "accepted_coverage_or_refresh_utc":at,"age_seconds":age,"maximum_delay_seconds":MAX_AGE_SECONDS}
    # Independent APIs have their own source-success clock; do not confuse an
    # unchanged daily observation with a stopped poller.
    network = state.get("external_sources", {}).get("network", {})
    if network:
        at = network.get("completed_at_utc") or network.get("last_success_at_utc")
        if at and (not instant(at) or (now-instant(at)).total_seconds() > 72*3600):
            problems.append("Independent network collector has not succeeded within 72 hours")
    if external is not None:
        for key, field, maximum in (("market","fetched_at_utc",6*3600),
                                     ("network","collected_at_utc",72*3600),
                                     ("revenue","generated_at_utc",72*3600)):
            at = external.get(key,{}).get(field)
            observed = instant(at)
            age = (now-observed).total_seconds() if observed else None
            healthy = age is not None and -300 <= age <= maximum
            if not healthy:
                problems.append(f"Independent {key} observation is missing or overdue ({at})")
            domains["external_"+key] = {"status":"healthy" if healthy else "stalled",
                 "accepted_coverage_or_refresh_utc":at,"age_seconds":age,"maximum_delay_seconds":maximum}
        revenue_source = external.get("revenue", {}).get("source", {})
        if revenue_source.get("schema_contract") == "section_aware_foundation_v2":
            at = revenue_source.get("sheet_fetched_at_utc")
            observed = instant(at)
            age = (now-observed).total_seconds() if observed else None
            healthy = age is not None and -300 <= age <= 48*3600
            if not healthy:
                problems.append(f"Revenue sheet ingestion is missing or overdue ({at}); rebuilding old data is not a fresh import")
            domains["revenue_sheet_ingestion"] = {"status":"healthy" if healthy else "stalled",
                "accepted_coverage_or_refresh_utc":at,"age_seconds":age,"maximum_delay_seconds":48*3600}
    return {"schema_version":1,"checked_at_utc":now.isoformat().replace("+00:00","Z"),
            "status":"unhealthy" if problems else "healthy", "problems":problems,
            "warnings":warnings,"domains":domains,
            "last_event_time_is_not_collection_coverage":True,
            "holder_data_as_of_utc":quality.get("holder_data_as_of_utc"),
            "dune_operations_performed":0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args();root=args.root
    try:
        state=validate_journal((root/"data/current/v3_refresh_state.json").read_bytes())
        config=json.loads((root/"config/v3_refresh.json").read_text())
        quality=json.loads((root/"data/current/xnet_holder_integrity.json").read_text())
        baseline=state.get("holder_reconciliation",{}).get("active_baseline_id")
        if baseline:
            from v3_holder_snapshot import PATH, validate_baseline
            proof=json.loads((root/PATH).read_text())
            owners,receipt=validate_baseline(proof)
            if (proof["baseline_id"]!=baseline or quality.get("baseline_slot")!=receipt["slot"]
                    or quality.get("holder_data_as_of_utc")!=receipt["block_time_utc"]):
                raise ValueError("Holder health does not match the complete finalized snapshot proof")
            with (root/"data/derived/xnet_owner_balances.csv").open(newline="") as handle:
                rows=list(csv.DictReader(handle))
            actual={row["owner"]:Decimal(row["xnet_balance"])*Decimal(10**8) for row in rows}
            if (len(actual)!=len(rows) or actual!={owner:Decimal(raw) for owner,raw in owners.items()}):
                raise ValueError("Published owner balances differ from the complete finalized inventory")
            holder=json.loads((root/"data/current/xnet_holder_state.json").read_text())
            chain=json.loads((root/"data/current/xnet_chain_snapshot.json").read_text())
            bundle=json.loads((root/"data/current/xnet_live_bundle.json").read_text())
            if (holder.get("positive_holder_count")!=len(owners) or chain.get("holder_count_positive")!=len(owners)
                    or holder.get("baseline_id")!=baseline or chain.get("holder_snapshot_id")!=baseline
                    or bundle.get("chain")!=chain):
                raise ValueError("Holder counts/bundle differ from the verified snapshot generation")
            for minimum,key in ((1,"holders_ge_1_xnet"),(100,"holders_ge_100_xnet"),(1000,"holders_ge_1000_xnet")):
                count=sum(int(raw)>=minimum*10**8 for raw in owners.values())
                if holder.get(key)!=count or chain.get(key)!=count:
                    raise ValueError("Published holder thresholds differ from the finalized raw balances")
            if Decimal(holder["outstanding_balance_sum_xnet"])*Decimal(10**8)!=Decimal(receipt["mint_supply_raw"]):
                raise ValueError("Published holder total does not conserve the same-bank mint supply")
        external={key:json.loads((root/f"data/current/xnet_{key}_state.json").read_text())
                  for key in ("market","network","revenue")}
        report=evaluate(state,config,quality,datetime.now(timezone.utc),external=external)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, ArithmeticError, StateError) as error:
        report={"status":"unhealthy","problems":[f"Cannot validate service evidence: {error}"],"warnings":[]}
    output=root/"state/v3_autonomy_audit.json";output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+"\n")
    lines=[f"## XNET autonomy: {report['status']}",
           "Collection coverage and integrity checked independently of workflow success."]
    for message in report["problems"]:
        print("::error::"+message.replace("\n"," "));lines.append("- **Failure:** "+message)
    for message in report["warnings"]:
        print("::warning::"+message.replace("\n"," "));lines.append("- Warning: "+message)
    summary=os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary,"a") as handle:handle.write("\n".join(lines)+"\n")
    print(json.dumps(report,indent=2))
    return int(bool(report["problems"]))


if __name__=="__main__":raise SystemExit(main())
