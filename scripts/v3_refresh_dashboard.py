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
import uuid
from types import SimpleNamespace
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
from v3_credit_budget import group_samples, sample_estimate, parse_instant, BudgetInputError
from v3_budget_runtime import collect_sample_history, _budget_policy
from v3_financial_journal import (
    synchronize_financial_journal, record_export_attempt, record_export_outcome, amount, financial_obligations,
)
from v3_standing_budget import (
    policy as standing_policy, scope as standing_scope, open_period as open_standing_period,
    evaluate as evaluate_standing_budget, reserve as reserve_standing_operation,
    _authority_hash as standing_authority_hash,
)
from v3_dune_metadata import DuneMetadataClient, DuneMetadataError

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
        if path == STATE:
            synchronize_financial_journal(value, CONFIG["export_credits_per_megabyte"])
        atomic_write(path, encoded(value))
    except (OSError, ValueError) as error:
        raise CheckpointFailed(f"Unable to durably save {path.name}: {error}") from error


def load_authoritative_state():
    return LiveStateStore(ROOT).load_authoritative_journal()

class _NoDuneRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward API credentials or a paid POST to another URL.

def dune_open(request, *, timeout):
    return urllib.request.build_opener(_NoDuneRedirect()).open(request,timeout=timeout)

def api(path, payload=None, include_wire_size=False):
    key = os.environ.get("DUNE_API_KEY")
    if not key:
        raise RuntimeError("DUNE_API_KEY is not configured; no executions submitted.")
    request = urllib.request.Request("https://api.dune.com/api/v1/" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-Dune-Api-Key": key, "Content-Type": "application/json"})
    try:
        with dune_open(request, timeout=45) as response:
            body = response.read()
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                if not include_wire_size:
                    raise
                value = None  # Completed transport is still metered; content is invalid.
            if not isinstance(value,dict):
                if not include_wire_size:
                    raise RuntimeError("Dune metadata returned a non-object response; retry metadata without resubmitting compute")
                value = None  # Preserve completed-read metering before refusing its body.
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


def autonomous_enabled(state):
    """Config alone never creates authority; activation is an explicit journal commit."""
    authority = CONFIG.get("autonomous_operation", {})
    installed = state.get("standing_authorization", {})
    if authority.get("enabled") is not True or not installed:
        return False
    if (installed.get("approval_ref") != authority.get("approval_ref")
            or installed.get("policy_sha256") != standing_authority_hash(authority)):
        raise BudgetBlocked("Installed standing authority does not match the reviewed policy")
    standing_policy(CONFIG, now())
    return True


def autonomous_estimate(state, spec, params, *, kind="execution", result_bytes=None):
    from v3_autonomous_estimates import estimate_operation
    decision = estimate_operation(state, spec, params, now(), kind=kind, result_bytes=result_bytes,
        policy={"default_engine":CONFIG["performance"], "transport_format_bound_fallback":True})
    if decision.get("status") != "estimated":
        raise BudgetBlocked("No credible standing operation estimate: " + json.dumps(decision, sort_keys=True))
    # Keep durable approvals compact without changing the estimate or deleting
    # liabilities. Full input vectors remain bound by digest and summary stats;
    # representative witness IDs are retained rather than copied thousands of
    # times into an ever-growing per-operation financial book.
    original = decision["evidence"]
    def compact(value):
        if isinstance(value,dict):
            return {key:compact(item) for key,item in value.items()}
        if isinstance(value,list):
            if len(value)>8:
                return {"count":len(value),"witnesses":value[:4]+value[-4:],
                        "all_values_sha256":hashlib.sha256(encoded(value)).hexdigest()}
            return [compact(item) for item in value]
        return value
    decision = {**decision,"evidence":{**compact(original),
        "full_evidence_sha256":hashlib.sha256(encoded(original)).hexdigest()}}
    return decision


def standing_forecast(state, period_end):
    """Outlier-inclusive expected cost, with a separate standing variance reserve.

    An unpriced domain is deferred independently, not used to freeze other domains.
    Each actual operation must still have its own conservative credible quote.
    """
    duration = max(0, (parse_instant(period_end, "renewal") - now()).total_seconds())
    entries, total, unpriced = [], 0.0, []
    specs = [(s, {"lookback_hours": int(s["lookback_hours"])}, True)
             for s in CONFIG.get("sources", []) if s.get("enabled", True)]
    specs += [(s, {}, False) for s in CONFIG.get("presentation", []) if s.get("enabled", True)]
    recon = CONFIG.get("reconciliation", {})
    if recon:
        specs.append(({"key":"reconciliation_manifest", "query_id":recon["manifest_query_id"],
                       "cadence_minutes":recon["weekly_cadence_minutes"], "performance":CONFIG["performance"]},
                       {"lookback_days":recon["weekly_lookback_days"]}, True))
        # A proven indexing backlog has its own bounded recheck cadence/cost
        # family. Include it in pacing instead of pretending recovery is free.
        for day in state.get("source_errors",{}).get("indexer_lag",{}).get("unresolved_days",[]):
            offset=(now().date()-datetime.fromisoformat(day).date()).days
            if 0<=offset<=60:
                misses=state.get("additive_day_recovery",{}).get(day,{}).get("indexer_misses",0)
                specs.append(({"key":"indexer_lag:"+day,"query_id":recon["repair_query_id"],
                    "cadence_minutes":60*min(24,6*2**min(max(0,misses-1),2)),"performance":CONFIG["performance"]},
                    {"oldest_days_ago":offset,"newest_days_ago":offset},True))
    for spec, params, exports in specs:
        try:
            quote = autonomous_estimate(state, spec, params)
        except BudgetBlocked as error:
            unpriced.append({"key":spec["key"], "reason":str(error)})
            continue
        unit = amount(quote["expected_credits"], "forecast expected compute")
        export_unit = 0.0
        if exports:
            values = [amount(h["estimated_export_credits"], "observed export")
                      for h in state.get("execution_history", [])
                      if h.get("query_id") == spec["query_id"] and h.get("estimated_export_credits") is not None]
            if values:
                export_unit = statistics.mean(values)
        count = math.ceil(duration / (float(spec["cadence_minutes"]) * 60))
        cost = count * (unit + export_unit)
        total += cost
        entries.append({"key":spec["key"], "runs":count, "expected_compute_credits":unit,
                        "expected_export_credits":export_unit, "total_credits":cost,
                        "estimate_evidence":quote.get("evidence")})
    return {"expected_credits":total, "entries":entries, "independently_unpriced_domains":unpriced}


def standing_usage_guard(state, proposed_spec=None, proposed_params=None):
    usage = DuneMetadataClient().get_usage(now=now())
    _, book, _ = open_standing_period(state, CONFIG, usage, now())
    forecast = standing_forecast(state, book["period_end_utc"])
    quote = proposed_spec.get("standing_estimate") if proposed_spec is not None else None
    decision = evaluate_standing_budget(state, CONFIG, usage, now(), forecast_credits=forecast["expected_credits"],
                                       quote=quote["cost_estimate_credits"] if quote else None)
    state["standing_credit_budget"]["last_decision"] = decision
    state["budget_decision"] = {"status":decision["status"], "blockers":decision["blockers"],
        "report":{"current_usage":usage, "totals":{"limit_credits":decision["limit_credits"],
            "usage_plus_reserve":decision["effective_usage_credits"] + decision["minimum_reserve_credits"],
            "dependencies_total":0.0}, "forecast":forecast}, "standing_risk_accounting":decision}
    if decision["status"] != "admitted":
        raise BudgetBlocked("Standing credit admission blocked: " + "; ".join(decision["blockers"]))
    evidence = {}
    if proposed_spec is not None:
        if not quote:
            raise BudgetBlocked("Standing execution requires its operation-specific estimate")
        approval = reserve_standing_operation(state, CONFIG, usage, now(), proposed_spec, proposed_params,
            "execution", quote["cost_estimate_credits"], operation_id=proposed_spec["financial_attempt_id"],
            evidence=quote["evidence"], forecast_credits=forecast["expected_credits"])
        if (approval.get("sql_sha256") != proposed_spec["verified_sql_sha256"]
                or approval.get("query_version") != proposed_spec["verified_query_version"]
                or approval.get("absolute_day") != proposed_spec.get("absolute_day")):
            raise BudgetBlocked("Standing reservation lacks its immutable query/day evidence")
        proposed_spec["operation_approval"] = approval
        require_operation_approval(proposed_spec, "execution", quote["cost_estimate_credits"], proposed_params)
        evidence = {"operation_approval":deepcopy(approval), "operation_quote":deepcopy(quote)}
        decision = state["standing_credit_budget"]["last_decision"]
        state["budget_decision"]["standing_risk_accounting"] = decision
        state["budget_decision"]["report"]["totals"]["usage_plus_reserve"] = (
            decision["effective_usage_credits"] + decision["minimum_reserve_credits"])
    return {"credits_used":usage["credits_used"], "guard_credits":decision["limit_credits"],
        "target_credits":CONFIG["monthly_target_credits"],
        "projected_period_credits":usage["credits_used"] + forecast["expected_credits"],
        "period_start":usage["period_start_utc"], "period_end":usage["period_end_utc"],
        "forecast":forecast, "standing_usage":usage, **evidence}


def prepare_standing_spec(state, spec, params):
    standing_scope(CONFIG, spec, params, "execution", now())
    contract = CONFIG["autonomous_operation"]["queries"][str(spec["query_id"])]
    saved = api(f"query/{spec['query_id']}")  # metadata only; never an execution or export
    sql = saved.get("query_sql")
    if (saved.get("query_id") != spec["query_id"] or not isinstance(sql, str)
            or hashlib.sha256(sql.encode()).hexdigest() != contract["sql_sha256"]
            or saved.get("is_archived") is True):
        raise BudgetBlocked("Saved query differs from its standing SQL contract")
    spec = {**spec, "financial_attempt_id":uuid.uuid4().hex,
            "verified_sql_sha256":contract["sql_sha256"], "verified_query_version":saved.get("version")}
    if "absolute_day" in spec:
        offset = (now().date() - datetime.fromisoformat(spec["absolute_day"]).date()).days
        if params != {"oldest_days_ago":offset,"newest_days_ago":offset}:
            raise BudgetBlocked("UTC date changed while pricing the day repair; replan its offsets")
        midnight = (now()+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        spec["execution_not_after_utc"]=(midnight-timedelta(minutes=2)).isoformat().replace("+00:00","Z")
        if now() >= midnight-timedelta(minutes=2):
            raise BudgetBlocked("Day repair waits across the UTC boundary rather than risking a shifted window")
    quote = autonomous_estimate(state, spec, params)
    spec["standing_estimate"] = quote
    spec["cost_admission_quote"] = {"operation_id":spec["financial_attempt_id"], "query_id":spec["query_id"],
        "query_parameters":params or {}, "performance":spec.get("performance",CONFIG["performance"]),
        "shape_version":spec.get("shape_version"), "cost_estimate_credits":quote["cost_estimate_credits"],
        "estimate_evidence":json.dumps(quote["evidence"], sort_keys=True),
        "approval_ref":CONFIG["autonomous_operation"]["approval_ref"], "quoted_at_utc":stamp(),
        "valid_until_utc":(now() + timedelta(minutes=15)).isoformat().replace("+00:00","Z"),
        "outlier_execution_ids":[b["execution_id"] for b in state.get("cost_breakers",{}).values()
                                 if b.get("query_id") == spec["query_id"]]}
    print(f"Standing execution quote {spec['financial_attempt_id']}: {quote['cost_estimate_credits']:.6f} "
          f"estimated credits, query {spec['query_id']}, parameters {params or {}}; not a provider cap", flush=True)
    return spec


def cadence_multiplier(state, key):
    if not autonomous_enabled(state) or key in {"bbb_dex", "network", "commercial"}:
        return 1
    return int(state.get("standing_credit_budget", {}).get("last_decision", {}).get("cadence_multiplier", 1))


def require_operation_approval(spec, kind, quote, params=None, execution_id=None):
    """Fail closed without an externally reviewed, exact-operation approval.

    This validates a supplied decision, never creates user authority. The trusted
    operator must verify approval_ref/evidence before installing it in the spec.
    Estimates are risk allocations, not provider charge caps.
    """
    approval = spec.get("operation_approval") or {}
    try:
        for field in ("operation_id", "approval_ref", "estimate_evidence"):
            if not isinstance(approval.get(field), str) or not approval[field].strip():
                raise BudgetInputError("Missing explicit operation approval/evidence")
        if (approval.get("kind") != kind or approval.get("query_id") != spec.get("query_id")
                or (approval.get("query_parameters") or {}) != (params or {})
                or approval.get("execution_id") != execution_id
                or approval.get("performance") != spec.get("performance", CONFIG["performance"])
                or approval.get("shape_version") != spec.get("shape_version")
                or amount(approval.get("quote_credits"), "approved quote") != quote):
            raise BudgetInputError("Approval does not bind the exact operation and estimate")
        if kind == "execution" and approval["operation_id"] != spec.get("financial_attempt_id"):
            raise BudgetInputError("Approval must bind the durable submission attempt")
        if not (parse_instant(approval.get("approved_at_utc"), "approved at") <= now()
                < parse_instant(approval.get("valid_until_utc"), "approval expiry")):
            raise BudgetInputError("Operation approval is not current")
    except BudgetInputError as error:
        raise BudgetBlocked(str(error)) from error
    return approval


def usage_guard(state=None, proposed_spec=None, proposed_params=None):
    if state is None:
        state = load_authoritative_state()
    if global_hold(state):
        raise BudgetBlocked("Global hold prohibits new paid operations")
    if autonomous_enabled(state):
        try:
            return standing_usage_guard(state, proposed_spec, proposed_params)
        except (BudgetInputError, DuneMetadataError, OSError) as error:
            # Billing outages deny new spending, not the entire service forever.
            # Metadata/free feeds and known-ID recovery recheck on the next cron.
            state["budget_decision"] = {"status":"blocked", "blockers":[str(error)],
                "automatic_recheck":True,"reason":"standing_billing_or_evidence_unavailable"}
            raise BudgetBlocked(str(error)) from error
    usage = api("usage", {"start_date": (now().date() - timedelta(days=40)).isoformat(),
                          "end_date": (now().date() + timedelta(days=1)).isoformat()})
    decision = evaluate_budget(state, CONFIG, usage, now(),
                               proposed_spec=proposed_spec, proposed_params=proposed_params)
    state["budget_decision"] = decision
    report = decision["report"]
    if decision["status"] != "admitted":
        raise BudgetBlocked("Dune credit admission blocked: " + "; ".join(decision["blockers"]))
    approval_evidence = {}
    if proposed_spec is not None:
        approval = require_operation_approval(proposed_spec, "execution",
            decision["proposed_evaluation"]["cost_estimate_credits"], proposed_params)
        approval_evidence = {"operation_approval": deepcopy(approval),
                             "operation_quote": deepcopy(decision["proposed_evaluation"])}
    current, totals = report["current_usage"], report["totals"]
    return {
        "credits_used": current["credits_used"], "guard_credits": totals["limit_credits"],
        "target_credits": CONFIG["monthly_target_credits"],
        "projected_period_credits": current["credits_used"] + totals["dependencies_total"],
        "period_start": current["period_start_utc"], "period_end": current["period_end_utc"],
        "forecast": report["forecast"],
        **approval_evidence,
    }

def checkpoint(state):
    # Only Dune execution state is published here. Revenue/feed builders and
    # dashboard-data publication are deliberately excluded.
    write_operational_health(state)
    save(STATE, state)
    return publish(state_only=True)


def source_instant(value, label):
    """Normalize the existing Dune UTC rendering, never substitute process time."""
    if isinstance(value, str) and value.endswith(" UTC"):
        value = value[:-4].replace(" ", "T", 1) + "Z"
    return parse_instant(value, label)


def write_operational_health(state):
    """Internal-only provenance and stable incident identities; no public fields added."""
    specs = [*CONFIG.get("sources", []), *CONFIG.get("presentation", [])]
    config = {"domains": {s["key"]: {"source_key": s["key"],
               "max_age_seconds": int(s.get("cadence_minutes", 30)) * 120}
               for s in specs if s.get("enabled", True)}}
    sources = deepcopy(state.get("queries", {}))
    def evidence(key, path, field, *, daily=False, max_age=86400):
        value = load(ROOT / path, {})
        at = value
        for part in field.split("."):
            at = at.get(part) if isinstance(at, dict) else None
        if daily and isinstance(at, str) and len(at) == 10:
            at += "T00:00:00Z"  # Date precision is explicit, never process time.
        if at is not None:
            try:
                at = source_instant(at, key + " source asof").isoformat().replace("+00:00", "Z")
            except BudgetInputError:
                pass  # Keep invalid source evidence visible; never invent a timestamp.
        sources[key] = {**sources.get(key, {}), "source_asof": at}
        config["domains"][key] = {"source_key": key, "max_age_seconds": max_age}
    evidence("xnet_transfers", "data/current/xnet_supply_state.json", "latest_transfer_event_utc")
    evidence("holders", "data/current/xnet_holder_state.json", "latest_transfer_event_utc")
    evidence("bbb_dex", "data/current/xnet_bbb_trade_state.json", "latest_trade_utc", max_age=172800)
    evidence("devices", "data/current/xnet_network_state.json", "devices.observed_at_utc", max_age=172800)
    evidence("offload", "data/current/xnet_network_state.json", "offload.data_as_of", daily=True, max_age=172800)
    evidence("revenue", "data/current/xnet_revenue_state.json", "live_fee_accrual.as_of", daily=True, max_age=172800)
    evidence("market", "data/current/xnet_market_state.json", "fetched_at_utc", max_age=7200)
    issues = deepcopy(state.get("source_errors", {}))
    if "indexer_lag" in issues:
        issues.setdefault("xnet_transfers",issues["indexer_lag"])
        issues.setdefault("holders",issues["indexer_lag"])
    if "holder_integrity" in issues or "holders" in state.get("domain_holds", {}):
        issues["holders"] = issues.get("holder_integrity", state.get("domain_holds", {}).get("holders"))
    # Report source freshness separately from permission to spend. A Dune guard
    # cannot make an observed device, revenue, market or accepted transfer paused.
    report = build_health({"sources": sources, "pending_executions": state.get("pending_executions", {}),
                           "degraded_sources": issues}, now(), config)
    report["admission"] = {"global_hold": global_hold(state),
                           "budget_blocked": state.get("budget_decision", {}).get("status") == "blocked",
                           "domain_holds": deepcopy(state.get("domain_holds", {}))}
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
    # Recovery policy applies ONLY to an already known pending ID. A future POST
    # keeps post-run refusal and the source breaker's independent quote barrier.
    pending = state.get("pending_executions", {}).get(spec.get("key", str(spec["query_id"])), {})
    if autonomous_enabled(state) and not pending:
        public_sources = {s["query_id"] for s in CONFIG.get("sources", [])}
        if CONFIG.get("reconciliation"):
            public_sources.update(CONFIG["reconciliation"][name] for name in ("manifest_query_id","repair_query_id"))
        if spec["query_id"] in public_sources:
            result_cache().require_capacity(state)
        spec = prepare_standing_spec(state, spec, params)
    spec = {**spec, "accept_completed_outlier": bool(pending.get("execution_id")) or autonomous_enabled(state)}
    return tracker(state, spec, params).execute(spec, params, enforce_cap, timeout_seconds)


def result_cache(checkpoint_callback=None):
    public_ids = {int(s["query_id"]) for s in CONFIG.get("sources", [])}
    if CONFIG.get("reconciliation"):
        public_ids.add(int(CONFIG["reconciliation"]["manifest_query_id"]))
        # The selective repair is the same public XNET transfer contract as hot
        # collection. Excluding it would fail cache_result AFTER a paid read.
        public_ids.add(int(CONFIG["reconciliation"]["repair_query_id"]))
    return ResultCache(ROOT, checkpoint_callback or checkpoint, allowed_query_ids=public_ids)


def export_risk(state, record, spec, result_bytes):
    """Comparable wire-size envelope, not a configured download/spend ceiling."""
    if autonomous_enabled(state):
        estimate = autonomous_estimate(state, {**spec, "query_id":record["query_id"]},
            record.get("query_parameters", {}), kind="export", result_bytes=result_bytes)
        return estimate["cost_estimate_credits"], [json.dumps(estimate["evidence"], sort_keys=True)]
    samples, _ = collect_sample_history(state, default_performance=CONFIG["performance"],
                                       performance_by_key={})
    policy = _budget_policy(CONFIG)
    estimate = sample_estimate(group_samples(samples, CONFIG["performance"]),
        record["query_id"], record.get("query_parameters", {}), now_utc=now(),
        performance=spec.get("performance", CONFIG["performance"]),
        shape_version=spec.get("shape_version"), max_age_hours=policy["sample_max_age_hours"],
        min_comparable_samples=policy["min_comparable_samples"],
        variance_uplift_sigma=policy["variance_uplift_sigma"],
        export_rate_per_mb=CONFIG["export_credits_per_megabyte"])
    evidence = estimate["evidence"]
    values = evidence["export_cost_values"]
    if (not values or evidence["export_evidence_count"] < policy["min_comparable_samples"]
            or evidence["export_bytes_max"] is None or result_bytes > evidence["export_bytes_max"]):
        raise ManualExportReview("Export size/cost lacks policy-comparable recent wire observations; explicit review required.")
    risk = max(max(values), statistics.mean(values) + policy["variance_uplift_sigma"] * statistics.pstdev(values))
    return risk, ["Comparable exact query/parameters/engine/shape; observed wire risk, NOT a hard cap; "
                  + json.dumps({**policy, "evidence": evidence}, sort_keys=True)]


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
        staged_attempt = {}
        def export_checkpoint(candidate):
            # Cache owns the canonical binding key. Its callback sees the staged
            # intent BEFORE publication, so both lanes cross one durability barrier.
            if staged_attempt and not staged_attempt.get("recorded"):
                markers = [(key, marker) for key, marker in candidate["result_exports"]["requests"].items()
                           if marker["execution_id"] == record["execution_id"]
                           and marker["requested_at_utc"] == staged_attempt["timestamp"]
                           and marker["status"] == "requested"]
                if len(markers) != 1:
                    raise StateError("Export intent does not identify exactly one cache marker")
                record_export_attempt(candidate, staged_attempt["attempt_id"],
                    execution_id=record["execution_id"], requested_at_utc=staged_attempt["timestamp"],
                    risk_credits=staged_attempt["risk"], estimate_evidence=staged_attempt["evidence"],
                    cache_marker_key=markers[0][0])
                staged_attempt["recorded"] = True
            return checkpoint(candidate)
        cache = result_cache(export_checkpoint)
        # Cached reuse is spend-free and remains possible while admission is closed.
        retry_needed = False
        try:
            cached = cache.lookup(state, record, binding)
        except ManualExportReview:
            if not autonomous_enabled(state):
                raise
            retry_needed, cached = True, None
        if cached is not None:
            result = {"execution_id": record["execution_id"], "state": "QUERY_STATE_COMPLETED",
                      "result": {"rows": cached["result_rows"], "metadata": meta}}
            save(ROOT / spec["output"], result)
            record["export_points"] = points
            record["export_reused"] = True
            prior = next((entry for entry in state.get("execution_history", [])
                          if entry.get("execution_id") == record["execution_id"]), {})
            for name in ("export_wire_bytes", "estimated_export_credits", "export_attempt_id", "export_requested_at_utc"):
                if name in prior:
                    record[name] = prior[name]
            return remaining - points
        # Backpressure must be discovered before a paid read or financial
        # reservation. Only genuinely consumed entries may be removed.
        cache.require_capacity(state)
        cache.prune_consumed(state)
        billing = usage_guard(state)
        risk, evidence = export_risk(state, record, spec, result_bytes)
        totals = state["budget_decision"]["report"]["totals"]
        spare = totals["limit_credits"] - totals["usage_plus_reserve"] - totals["dependencies_total"]
        if risk > spare:
            raise BudgetBlocked("Export risk exceeds headroom after all remaining scheduled obligations")
        if autonomous_enabled(state):
            previous_standing_book = deepcopy(state.get("standing_credit_budget"))
            prior_reads = ([item["liability_id"] for item in financial_obligations(state)[0]
                if item.get("kind")=="export" and item.get("execution_id")==record["execution_id"]]
                if retry_needed else [])
            try:
                approval = reserve_standing_operation(state, CONFIG, billing["standing_usage"], now(),
                    {**spec, "query_id":record["query_id"]}, record.get("query_parameters"), "export", risk,
                    operation_id="export_" + uuid.uuid4().hex,
                    evidence={"transport_quote":evidence, "result_bytes":result_bytes,
                              "prior_read_liability_ids":prior_reads},
                    execution_id=record["execution_id"])
            except BudgetInputError as error:
                raise BudgetBlocked(str(error)) from error
            approved_spec = {**spec, "query_id":record["query_id"], "operation_approval":approval}
            require_operation_approval(approved_spec, "export", risk, record.get("query_parameters"), record["execution_id"])
            if retry_needed:
                try:
                    retry_timestamp = now().isoformat().replace("+00:00", "Z")
                    staged_attempt.update(attempt_id=approval["operation_id"], timestamp=retry_timestamp,
                                          risk=risk, evidence="; ".join(evidence))
                    cache.retry_export(state, record, binding, approval=approval,
                        timestamp=retry_timestamp, stage_new_intent=True, evidence=evidence,
                        minimum_delay_seconds=CONFIG["autonomous_operation"]["export_retry_delay_seconds"],
                        max_attempts=CONFIG["autonomous_operation"]["max_export_attempts"])
                except CacheError:
                    # No transport/checkpoint was attempted by a failed preflight.
                    # Do not accumulate reservations for operations never started.
                    if previous_standing_book is None:
                        state.pop("standing_credit_budget",None)
                    else:
                        state["standing_credit_budget"] = previous_standing_book
                    raise
            print(f"Standing export approval {approval['operation_id']}: {risk:.6f} estimated credits, "
                  f"query {record['query_id']}, execution {record['execution_id']}; not a provider cap", flush=True)
        else:
            approval = require_operation_approval({**spec, "query_id": record["query_id"]}, "export", risk,
                                                  record.get("query_parameters"), record["execution_id"])
        # Prune only after a prior full acceptance. Never evict pending source work.
        requested_at = staged_attempt.get("timestamp") or now().isoformat().replace("+00:00", "Z")
        reused = None
        if not staged_attempt.get("recorded"):
            staged_attempt.update(attempt_id=approval["operation_id"], timestamp=requested_at,
                                  risk=risk, evidence="; ".join(evidence))
            prepare_options = {"isolate_unrelated":True} if autonomous_enabled(state) else {}
            reused = cache.prepare_export(state, record, binding, risk_credits=risk, evidence=evidence,
                                          timestamp=requested_at, **prepare_options)
        if reused is not None or not staged_attempt.get("recorded"):
            raise StateError("No durable exact export attempt; refusing HTTP retrieval")
        try:
            require_operation_approval({**spec,"query_id":record["query_id"],"operation_approval":approval},
                "export",risk,record.get("query_parameters"),record["execution_id"])
        except BudgetBlocked as error:
            # A slow publication cannot extend spending authority. Retain the
            # intent conservatively; the next bounded retry needs fresh authority.
            from v3_result_cache import ExportRetryDeferred
            raise ExportRetryDeferred(f"Export approval expired before transport: {error}") from error
        result, wire_bytes = api(f'execution/{record["execution_id"]}/results?limit=10000&allow_partial_results=false', include_wire_size=True)
        if type(wire_bytes) is not int or wire_bytes < 0:
            raise RuntimeError("Missing actual export wire metering; retain unknown attempt")
        incurred = wire_bytes / 1000000 * CONFIG["export_credits_per_megabyte"]
        record_export_outcome(state, approval["operation_id"], observed_credits=incurred,
                              completed_transport_observed_at_utc=now().isoformat().replace("+00:00", "Z"))
        record.update(export_points=points, export_wire_bytes=wire_bytes,
                       estimated_export_credits=incurred, export_attempt_id=approval["operation_id"],
                       export_requested_at_utc=requested_at, export_reused=False)
        for entry in state.get("execution_history", []):
            if entry.get("execution_id") == record["execution_id"]:
                entry.update(export_points=points, export_wire_bytes=wire_bytes,
                             estimated_export_credits=incurred, result_bytes=result_bytes,
                             export_attempt_id=approval["operation_id"], export_requested_at_utc=requested_at,
                             export_reused=False)
        checkpoint(state)  # Charge evidence is durable independently of data acceptance.
        fetched = result.get("result", {}).get("rows") if isinstance(result, dict) and isinstance(result.get("result"), dict) else None
        if (not isinstance(result, dict) or result.get("state") != "QUERY_STATE_COMPLETED"
                or not isinstance(fetched, list) or len(fetched) != rows
                or result.get("next_uri") or result.get("next_offset")
                or (result.get("execution_id") is not None and result["execution_id"] != record["execution_id"])):
            if autonomous_enabled(state):
                from v3_result_cache import ExportRetryDeferred
                raise ExportRetryDeferred("Source result transport is incomplete; retain this execution and retry its read under fresh bounded authority")
            raise ManualExportReview("Source result is incomplete; canonical state not reduced.")
        cache.cache_result(state, record, binding, {"execution_id": record["execution_id"],
            "total_row_count": rows, "result_rows": fetched}, timestamp=stamp())
    record["export_points"] = points
    record["export_wire_bytes"] = wire_bytes
    record["estimated_export_credits"] = wire_bytes / 1000000 * CONFIG["export_credits_per_megabyte"]
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
    issue = state.get("source_errors", {}).get(key, {})
    if issue.get("manual_reconciliation_required"):
        # A narrowly identified legacy false-positive may be revalidated using
        # its already-paid exact result, never by clearing all manual holds.
        pending = state.get("pending_executions", {}).get(key, {})
        recoverable = (key == "xnet_transfers" and autonomous_enabled(state)
            and issue.get("error") == "Ambiguous duplicate hot transfer locator"
            and pending.get("execution_id")
            and pending.get("terminal_status", {}).get("state") == "QUERY_STATE_COMPLETED")
        if not recoverable:
            return True
    if issue.get("retry_kind") == "transfer_instruction_proof":
        until = issue.get("next_retry_at_utc")
        if until and now() < parse_instant(until, "instruction proof retry"):
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


LEGACY_HOLDER_ERROR = ("Negative holder balance after reduction: "
    "5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd = -51907.828371000000000000")
LEGACY_HOLDER_PAUSES = frozenset({LEGACY_HOLDER_ERROR,
    "Controlled five-credit recovery halted: BBB wallet holder-integrity regression "
    "(-51907.828371 XNET); manual evidence-based reconciliation required."})
HOLDER_MIGRATION = "legacy_bbb_negative_holder_v1"


def _holder_quality_proven(quality, holder):
    """File evidence, not an arbitrary domain-hold label or generated timestamp."""
    try:
        at = source_instant(quality.get("holder_data_as_of_utc"), "holder asof")
        checked = source_instant(quality.get("checked_at_utc"), "holder checked")
        valid_status = ((quality.get("status") == "DEGRADED"
                         and quality.get("reason") == "holder_balance_integrity"
                         and str(quality.get("error", "")).startswith("Negative holder balance after reduction:"))
                        or (quality.get("status") == "HEALTHY" and not quality.get("error")))
        return (valid_status and quality.get("methodology_changed") is False
                and quality["holder_data_as_of_utc"] == holder.get("latest_transfer_event_utc")
                and at <= checked <= now())
    except BudgetInputError:
        return False


def _holder_migration_origin(state):
    migration = state.get("holder_pause_migration", {})
    origin = migration.get("origin", {})
    quality = origin.get("quality", {})
    return (migration.get("migration_code") == HOLDER_MIGRATION
            and state.get("last_error") == origin.get("pause_error", quality.get("error"))
            and state.get("last_error") in LEGACY_HOLDER_PAUSES
            and quality.get("error") == LEGACY_HOLDER_ERROR
            and origin.get("failed_at_utc") == state.get("failed_at_utc")
            and quality.get("status") == "DEGRADED"
            and _holder_quality_proven(quality, {"latest_transfer_event_utc": origin.get("holder_asof")}))


def migrate_holder_hold(state):
    """One exact known incident, with persisted quality AND last-good evidence.

    Preserve the original pause/error as audit. This is not resume, holder repair,
    execution authorization, or permission to widen the financial guard.
    """
    if not state.get("paused") or state.get("last_error") not in LEGACY_HOLDER_PAUSES:
        return False
    if "holder_pause_migration" in state:
        return False  # Immutable origin; never re-migrate a changed diagnosis.
    quality = load(ROOT / "data/current/xnet_holder_integrity.json", {})
    holder = load(ROOT / "data/current/xnet_holder_state.json", {})
    if quality.get("error") != LEGACY_HOLDER_ERROR or not _holder_quality_proven(quality, holder):
        return False
    state["holder_pause_migration"] = {
        "migration_code": HOLDER_MIGRATION,
        "origin": {"quality": deepcopy(quality), "holder_asof": holder["latest_transfer_event_utc"],
                   "failed_at_utc": state.get("failed_at_utc"), "pause_error": state["last_error"]},
        "current_quality": deepcopy(quality), "holder_sha256": hashlib.sha256(encoded(holder)).hexdigest()}
    state.setdefault("domain_holds", {})["holders"] = {
        "migration_code": HOLDER_MIGRATION, "error": LEGACY_HOLDER_ERROR,
        "holder_data_as_of_utc": quality["holder_data_as_of_utc"], "automatic_clearance": False}
    sync_holder_integrity_error(state)
    return True


def global_hold(state):
    classified = _holder_migration_origin(state)
    if classified:
        migration = state["holder_pause_migration"]
        quality = load(ROOT / "data/current/xnet_holder_integrity.json", {})
        holder = load(ROOT / "data/current/xnet_holder_state.json", {})
        classified = (_holder_quality_proven(quality, holder) and quality == migration.get("current_quality")
                      and hashlib.sha256(encoded(holder)).hexdigest() == migration.get("holder_sha256"))
        source = migration.get("accepted_source")
        if source:
            consumed = state.get("consumed_executions", {}).get(source.get("execution_id"), {})
            classified = (classified and consumed.get("key") == "xnet_transfers"
                          and consumed.get("effect") == "non_destructive_hot_union"
                          and consumed.get("query_id") == source.get("query_id"))
        else:
            classified = classified and quality == migration["origin"]["quality"]
        if quality.get("status") == "HEALTHY":
            supply = load(ROOT / "data/current/xnet_supply_state.json", {})
            classified = classified and bool(source) and supply.get("latest_transfer_event_utc") == holder.get("latest_transfer_event_utc")
    return bool(state.get("paused") and not classified)


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
        and projected <= float(CONFIG["monthly_target_credits"] if billing.get("standing_usage")
                               else policy["weekly_soft_projected_monthly_credits"])
    )


def sync_holder_integrity_error(state, accepted_source=None):
    quality = load(ROOT / "data/current/xnet_holder_integrity.json", {})
    if accepted_source is not None and _holder_migration_origin(state):
        holder = load(ROOT / "data/current/xnet_holder_state.json", {})
        migration = state["holder_pause_migration"]
        consumed = state.get("consumed_executions", {}).get(accepted_source.get("execution_id"), {})
        if (consumed.get("key") != "xnet_transfers" or consumed.get("effect") != "non_destructive_hot_union"
                or consumed.get("query_id") != accepted_source.get("query_id")
                or not _holder_quality_proven(quality, holder)
                or source_instant(quality["checked_at_utc"], "holder checked")
                < source_instant(accepted_source.get("completed_at_utc"), "accepted source completed")
                or source_instant(quality["holder_data_as_of_utc"], "holder asof")
                < source_instant(migration["origin"]["holder_asof"], "original holder asof")):
            raise ManualReviewRequired("Holder transition lacks matching fresh source/quality evidence")
        if quality["status"] == "HEALTHY":
            supply = load(ROOT / "data/current/xnet_supply_state.json", {})
            if supply.get("latest_transfer_event_utc") != holder.get("latest_transfer_event_utc"):
                raise ManualReviewRequired("Healthy holder evidence does not match accepted chain asof")
            state.setdefault("domain_holds", {}).pop("holders", None)
        else:
            state.setdefault("domain_holds", {})["holders"] = {
                "migration_code": HOLDER_MIGRATION, "error": quality["error"],
                "holder_data_as_of_utc": quality["holder_data_as_of_utc"], "automatic_clearance": False}
        migration.update(current_quality=deepcopy(quality), holder_sha256=hashlib.sha256(encoded(holder)).hexdigest(),
                         accepted_source={"execution_id": accepted_source["execution_id"],
                                          "query_id": accepted_source["query_id"],
                                          "completed_at_utc": accepted_source["completed_at_utc"]})
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
        state["reconciliation"].pop("integrity_wide_attempts",None)
        state["reconciliation"].pop("last_integrity_wide_attempt_utc",None)
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

    if autonomous_enabled(state):
        from v3_autonomous_reconcile import repair_days, recover_additive_days, owner_seed_day, route_seeded_mismatches
        seed_day=owner_seed_day(ROOT,load)
        recon=state.setdefault("reconciliation",{})
        recon["pending_mismatch_days"]=route_seeded_mismatches(state,recon.get("pending_mismatch_days",[]),seed_day)
        # First atomically accept current staged source results; never fill the
        # small result cache with repair work ahead of those unconsumed effects.
        if any(key in state.get("pending_executions", {}) for key in ("xnet_transfers", "bbb_dex")):
            return False
        from v3_indexer_lag import probe as probe_indexer_lag
        probe_indexer_lag(state,ROOT,load,now())
        if repair_days(state, CONFIG, now(), seed_cutoff=seed_day):
            names = ("ROOT", "CONFIG", "STATE", "now", "stamp", "encoded", "load", "save", "run",
                     "execute", "export_source", "stage_consumption", "publish", "checkpoint",
                     "sync_holder_integrity_error", "write_operational_health")
            return recover_additive_days(SimpleNamespace(**{key:globals()[key] for key in names}), state, billing)

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
    if historical_cost_quarantined(state) and not autonomous_enabled(state):
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
    blocking_pending = {key for key in pending - {"reconciliation_manifest", "historical_repair"}
                        if state["pending_executions"][key].get("terminal_status", {}).get("state")
                        not in {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_CANCELED", "QUERY_STATE_EXPIRED"}}
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
    elif daily_due and not (autonomous_enabled(state) and policy.get("daily_manifest_on_gap_only", False)):
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

    if (autonomous_enabled(state) and force_wide
            and "reconciliation_manifest" not in state.get("pending_executions",{})):
        # Precise day recovery remains available, but an unchanged holder
        # incident must not buy the expensive wide manifest on every cron.
        attempts=int(recon.get("integrity_wide_attempts",0))
        if (attempts>=int(CONFIG.get("integrity_repair_max_attempts",3))
                or not due(recon.get("last_integrity_wide_attempt_utc"),
                           int(CONFIG.get("integrity_repair_cooldown_minutes",1440)))):
            recon["manifest_deferred_reason"]="Wide integrity check is bounded per incident/cooldown; precise-day recovery continues"
            return False
        recon["integrity_wide_attempts"]=attempts+1
        recon["last_integrity_wide_attempt_utc"]=stamp()
        checkpoint(state)

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

        if autonomous_enabled(state):
            # The seeded/partially seeded day cannot be replayed as a full day.
            # Keep its aggregate mismatch visible without buying an inadmissible
            # repair or starving all future post-seed historical verification.
            from v3_autonomous_reconcile import owner_seed_day, route_seeded_mismatches
            mismatches=route_seeded_mismatches(state,mismatches,owner_seed_day(ROOT,load))
            recon["last_manifest_eligible_mismatch_days"]=list(mismatches)

        if not mismatches:
            recon.pop("pending_mismatch_days", None)
            # Counts/weighted totals cannot prove owner, amount, deletion, or event
            # identity. Never turn aggregate equality into an integrity clearance.
            recon.pop("integrity_manifest_clean_at_utc", None)
            if autonomous_enabled(state):
                old_issue = state.setdefault("source_errors", {}).pop("historical_reconciliation", None)
                if old_issue:
                    state.setdefault("autonomous_resolved_source_issues", []).append({
                        "source_key":"historical_reconciliation", "previous_issue":old_issue,
                        "resolved_at_utc":stamp(), "verified_execution_id":record["execution_id"],
                        "basis":"Fresh standing quote and successfully consumed manifest; no settlement inferred"})
            stage_consumption(state, manifest_spec, record, "manifest_decision")
            checkpoint(state)  # decision AND its consumption marker in one commit
            return False
        recon["pending_mismatch_days"] = mismatches
        if autonomous_enabled(state):
            # Aggregate mismatches authorize only complete, day-bound additive
            # verification on a later run, never destructive replacement.
            stage_consumption(state, manifest_spec, record, "manifest_decision")
            checkpoint(state)
            return False
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
    canonical, canonical_rows = {}, []
    with canonical_path.open(newline="") as source:
        for row in csv.DictReader(source):
            row = normalized(row)
            canonical_rows.append(row)
            canonical.setdefault(locator(row), set()).add(signature(row))
    recon = state.get("reconciliation", {})
    repaired_days = {item["day"] for item in recon.get("last_repaired_days", []) if item.get("day")}
    legacy_cutoff = recon.get("last_completed_utc") if recon.get("last_mode") == "selective_repair" else None
    seed = load(ROOT / "data/xnet_owner_balances_seed_metadata.json", {})
    seed_cutoff = seed.get("latest_balance_event_time")
    normalized_rows = [normalized(row) for row in rows]
    from v3_transfer_instruction_recovery import prepare_proofs, groups, proven_group
    from v3_rpc_transfer_proof import ProofRejected
    # Reject duplicate event IDs rather than collapsing rows or inventing an
    # instruction ordinal. Distinct amounts in one locator need exact RPC proof.
    if len({row["event_id"] for row in normalized_rows}) != len(normalized_rows):
        raise ManualReviewRequired("Duplicate transfer event ID in provider result")
    try:
        proofs = prepare_proofs(state, canonical_rows, normalized_rows)
        old_groups = groups(canonical_rows)
        for key, incoming in groups(normalized_rows).items():
            proven_group(old_groups.get(key, []), incoming, proofs)
    except ProofRejected as error:
        raise ManualReviewRequired(str(error)) from error
    for row in normalized_rows:
        key, value = locator(row), signature(row)
        old = canonical.get(key, set())
        if value not in old:
            timestamp = parse_ts(row["block_time"])
            if (row["block_date"] in repaired_days or
                    (legacy_cutoff and timestamp <= parse_ts(legacy_cutoff)) or
                    (seed_cutoff and timestamp <= parse_ts(seed_cutoff))):
                raise ManualReviewRequired("Hot row overlaps a repaired/seeded window; stale resurrection fenced")
    # Canonical IDs are formed from normalized raw integers, not JSON float text.
    save(path, {**result, "result": {**result["result"], "rows": normalized_rows}})


def reduce_atomically(transfer_path=None, bbb_path=None):
    # A failed reduction must preserve canonical data and all derived state.
    with tempfile.TemporaryDirectory() as directory:
        backup = Path(directory) / "data"
        shutil.copytree(ROOT / "data", backup)
        try:
            args = []
            if transfer_path is not None:
                args.extend(["--transfer-result", str(transfer_path)])
            if bbb_path:
                args.extend(["--bbb-result", str(bbb_path)])
            if not args:
                raise RuntimeError("No source result to reduce")
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
            # Each reducer invocation has its own data rollback. Refusing one
            # source must not suppress a valid independent source or presentation.
            for key in ("xnet_transfers", "bbb_dex"):
                if key not in paths:
                    continue
                spec, record = source_records[key]
                source_before = deepcopy(state)
                source_backup = Path(directory) / ("before-" + key)
                shutil.copytree(ROOT / "data", source_backup)
                try:
                    # Validate/stage the clock-marker pair before any reducer
                    # mutation. No checkpoint occurs until the full generation.
                    stage_consumption(state, spec, record,
                                      "non_destructive_hot_union" if key == "xnet_transfers" else "bbb_trade_union")
                    if key == "xnet_transfers":
                        validate_hot_transfers(paths[key], state)
                        reduce_atomically(paths[key], None)
                        if state.get("indexer_lag_watch",{}).get("witnesses"):
                            from v3_indexer_lag import reconcile_witnesses
                            with (ROOT/"data/canonical/xnet_transfers.csv").open(newline="") as handle:
                                reconcile_witnesses(state,list(csv.DictReader(handle)))
                        sync_holder_integrity_error(state, accepted_source=record)
                    else:
                        reduce_atomically(None, paths[key])
                except StateError:
                    raise
                except (ManualReviewRequired, CacheError) as error:
                    proofs = deepcopy(state.get("transfer_instruction_proofs", {}))
                    shutil.rmtree(ROOT / "data")
                    shutil.copytree(source_backup, ROOT / "data")
                    state.clear()
                    state.update(source_before)
                    if proofs:
                        state["transfer_instruction_proofs"] = proofs
                    quarantine_source(state, key, str(error))
                    continue
                except (RuntimeError, subprocess.CalledProcessError, OSError, ValueError) as error:
                    from v3_transfer_instruction_recovery import InstructionProofDeferred
                    proofs = deepcopy(state.get("transfer_instruction_proofs", {}))
                    shutil.rmtree(ROOT / "data")
                    shutil.copytree(source_backup, ROOT / "data")
                    state.clear()
                    state.update(source_before)
                    if proofs:
                        state["transfer_instruction_proofs"] = proofs
                    record_source_issue(state, key, error)
                    if isinstance(error, InstructionProofDeferred):
                        state["source_errors"][key].update(retry_kind="transfer_instruction_proof", automatic_recheck=True)
                    continue
                else:
                    if key == "xnet_transfers":
                        state["chain_completed_at_utc"] = stamp()
                    state["queries"][spec["key"]] = record
                    state.setdefault("source_errors", {}).pop(spec["key"], None)
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

def sync_independent_network(state):
    """Import ONLY the collector-owned lane; never replace financial recovery."""
    try:
        run("v3_sync_network.py")
        refreshed = validate_journal(STATE.read_bytes())
        if "network" in refreshed.get("external_sources", {}):
            state.setdefault("external_sources", {})["network"] = refreshed["external_sources"]["network"]
        state.setdefault("source_errors", {}).pop("network", None)
    except StateError:
        raise
    except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
        record_source_issue(state, "network", error)


def refresh_independent_data(state):
    """No Dune API routes at all; retain the exact chain pause and accepted ledger."""
    try:
        sync_independent_network(state)
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
    migrate_holder_hold(state)
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
    if not global_hold(state) or args.resume:
        # Startup receipt was checked before any producer mutation. Network is a
        # separate non-Dune domain; import only its journal update in this run.
        sync_independent_network(state)
    reconciliation_errors = tracker(state).reconcile(metadata_only=True)
    for key, error in reconciliation_errors.items():
        print(f"::warning::Execution reconciliation {key}: {error}", flush=True)
    if global_hold(state) and not args.resume:
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
    if args.resume:
        state["paused"] = False
    state.setdefault("paused", False)
    state["started_at_utc"] = stamp()
    state.setdefault("queries", {})
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
                previous_completed = previous.get("continuous_coverage_end_utc") or previous.get("completed_at_utc")
                cadence = int(
                    spec.get(
                        "cadence_minutes",
                        CONFIG["chain_cadence_minutes"],
                    )
                )
                cadence *= cadence_multiplier(state, spec["key"])

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
                calendar_due = bool(
                    autonomous_enabled(state) and preferred_hour is not None
                    and previous_completed is not None
                    and parse_instant(previous_completed,"previous daily collection").date() < now().date()
                    and now().hour >= int(preferred_hour)
                )

                # Recover a missed preferred hour after a 3-hour grace
                # period instead of deferring BBB collection another day.
                # The existing max_catchup_hours guard still applies.
                in_preferred_hour = (
                    preferred_hour is None
                    or calendar_due
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
                        and (calendar_due or due(previous_completed,cadence))
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

            paths = {}
            successful_sources = []
            source_modes = {}
            source_lookbacks = {}

            for spec in due_sources:
                if billing is None and not state.get("pending_executions", {}).get(spec["key"], {}).get("execution_id"):
                    continue
                previous = (
                    state.get("queries", {})
                    .get(spec["key"], {})
                )
                previous_completed = previous.get("continuous_coverage_end_utc") or previous.get("completed_at_utc")

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
                            elapsed_hours + int(spec.get("overlap_hours", 1))
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

                if autonomous_enabled(state) and spec["key"] == "xnet_transfers" and not pending:
                    from v3_autonomous_reconcile import hot_catchup_limit
                    if lookback > hot_catchup_limit(CONFIG) + int(spec.get("overlap_hours",1)):
                        record_source_issue(state, spec["key"],
                            "Wide collection gap routed to bounded, separately quoted UTC-day recovery")
                        state["source_errors"][spec["key"]].update(
                            retry_kind="bounded_day_catchup", automatic_recheck=True)
                        continue

                try:
                    record = execute(
                        execution_spec_for_mode(spec, actual_mode),
                        {"lookback_hours": lookback},
                        state=state,
                        timeout_seconds=spec.get("execution_timeout_seconds"),
                    )
                except StateError:
                    raise
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
                except (RuntimeError, OSError, ValueError) as error:
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
                except StateError:
                    raise
                except BudgetBlocked as error:
                    record_source_issue(state, spec["key"], error, unresolved=True)
                    continue
                except CacheError as error:
                    quarantine_source(state, spec["key"], str(error))
                    if autonomous_enabled(state):
                        from v3_result_cache import ExportRetryDeferred
                        if isinstance(error, ExportRetryDeferred):
                            state["source_errors"].pop(spec["key"], None)
                            record_source_issue(state, spec["key"], error)
                    checkpoint(state)
                    continue
                except (RuntimeError, OSError, ValueError) as error:
                    record_source_issue(state, spec["key"], error, unresolved=True)
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
            if due(last, spec["cadence_minutes"] * cadence_multiplier(state, spec["key"])) or spec["key"] in state.get("pending_executions", {}):
                due_presentation.append(spec)

        # Confirmed terminal failures wait for bounded backoff. Pending work is
        # exempt so existing execution IDs are always reconciled first.
        due_presentation = [s for s in due_presentation if not presentation_in_backoff(state, s["key"])]
        # A perpetually failing early chart must not starve later daily charts.
        # Keep source-invalidated charts first, but rotate attempts within each
        # priority class rather than sorting forever by configuration position.
        attempts = state.setdefault("presentation_last_attempts", {})
        def attempt_order(spec):
            at = attempts.get(spec["key"])
            try:
                oldest = parse_instant(at, "presentation attempt").timestamp() if at else float("-inf")
            except BudgetInputError:
                oldest = float("-inf")
            return (spec["key"] not in forced, oldest)
        due_presentation.sort(key=attempt_order)
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
            state.setdefault("presentation_last_attempts", {})[spec["key"]] = stamp()
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
        state["completed_at_utc"] = stamp()
        if not state.get("paused"):
            state["last_error"] = None
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
