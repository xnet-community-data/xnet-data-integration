"""Pure v1 runtime adapter: financial obligations are NOT estimator telemetry.

No I/O, state mutation, authorization creation, or inference of billing inclusion.
The default standing-rotation admission remains conservative. Allowance APIs in
v3_financial_journal are explicit, separate risk allocations, not bypass switches.
"""
from v3_credit_budget import (
    BudgetInputError, DEFAULT_MIN_COMPARABLE_SAMPLES, DEFAULT_SAMPLE_MAX_AGE_HOURS,
    DEFAULT_VARIANCE_UPLIFT_SIGMA, budget_admission, execution_admission,
    normalize_usage, parse_instant,
)
from v3_financial_journal import amount, financial_obligations


def _require_settings(config):
    if not isinstance(config, dict):
        raise BudgetInputError("config must be an object")
    values = []
    for key in ("monthly_spend_guard_credits", "minimum_credit_reserve",
                "export_credits_per_megabyte", "monthly_target_credits"):
        value = amount(config.get(key), f"config.{key}")
        if value <= 0:
            raise BudgetInputError(f"config.{key} must be positive")
        values.append(value)
    reconciliation = config.get("reconciliation") or {}
    if not isinstance(reconciliation, dict):
        raise BudgetInputError("config.reconciliation must be an object")
    headroom = reconciliation.get("minimum_target_headroom_credits")
    if headroom is not None:
        headroom = amount(headroom, "minimum_target_headroom_credits")
        if headroom <= 0:
            raise BudgetInputError("minimum_target_headroom_credits must be positive")
    return (*values, headroom)


def _budget_policy(config):
    policy = config.get("budget_policy") or {}
    if not isinstance(policy, dict):
        raise BudgetInputError("config.budget_policy must be an object")
    result = {"min_comparable_samples": DEFAULT_MIN_COMPARABLE_SAMPLES,
              "sample_max_age_hours": DEFAULT_SAMPLE_MAX_AGE_HOURS,
              "variance_uplift_sigma": DEFAULT_VARIANCE_UPLIFT_SIGMA}
    for key in result:
        if policy.get(key) is not None:
            result[key] = policy[key]
    if type(result["min_comparable_samples"]) is not int or result["min_comparable_samples"] < 1:
        raise BudgetInputError("min_comparable_samples must be a positive integer")
    for key in ("sample_max_age_hours", "variance_uplift_sigma"):
        amount(result[key], key)
    return result


def _performance_by_key(config):
    default = config.get("performance", "medium")
    if not isinstance(default, str) or not default:
        raise BudgetInputError("config.performance must be a nonempty string")
    by_key = {}
    for source in config.get("sources", []):
        if not isinstance(source, dict):
            raise BudgetInputError("config.sources must contain objects")
        performance = source.get("performance")
        if performance is not None:
            if not isinstance(performance, str) or not performance:
                raise BudgetInputError("source performance must be a nonempty string")
            by_key[source.get("key")] = performance
            by_key[f"source:{source.get('key')}"] = performance
    policy = config.get("budget_policy") or {}
    if not isinstance(policy, dict):
        raise BudgetInputError("config.budget_policy must be an object")
    overrides = policy.get("sample_performance_overrides") or {}
    if not isinstance(overrides, dict):
        raise BudgetInputError("sample_performance_overrides must be an object")
    by_key.update(overrides)
    return default, by_key


def collect_sample_history(state, *, default_performance, performance_by_key):
    history = state.get("execution_history", []) if isinstance(state, dict) else None
    if not isinstance(history, list) or not isinstance(performance_by_key, dict):
        raise BudgetInputError("Malformed sample history/performance map")
    samples = []
    diagnostics = {"costless_history_entries": 0, "default_default_performance": default_performance,
                   "per_key_performance_applied": 0}
    for entry in history:
        if not isinstance(entry, dict):
            raise BudgetInputError("execution_history entries must be objects")
        if entry.get("execution_cost_credits") is None:
            diagnostics["costless_history_entries"] += 1
            continue
        performance = entry.get("performance") or (entry.get("spec_identity") or {}).get("performance")
        if performance is None:
            performance = performance_by_key.get(entry.get("key"))
            diagnostics["per_key_performance_applied"] += int(performance is not None)
        sample = {name: entry.get(name) for name in (
            "key", "query_id", "execution_id", "execution_cost_credits", "query_parameters",
            "execution_ended_at", "execution_started_at", "submitted_at_utc")}
        sample.update(performance=performance,
                      shape_version=entry.get("shape_version", entry.get("query_shape_version")))
        # Only metered export observations inform export estimates. Provider
        # result metadata alone does not establish wire charge or even a read.
        if entry.get("export_wire_bytes") is not None or entry.get("estimated_export_credits") is not None:
            sample.update({name: entry.get(name) for name in (
                "export_wire_bytes", "estimated_export_credits")})
        samples.append(sample)
    return samples, diagnostics


def _enabled(value, label):
    if not isinstance(value, bool):
        raise BudgetInputError(f"{label} must be a bool")
    return value


def build_scheduled_entries(config):
    _require_settings(config)
    entries, skipped = [], []
    for family, prefix in (("sources", "source"), ("presentation", "presentation")):
        for spec in config.get(family, []):
            if not isinstance(spec, dict) or not isinstance(spec.get("key"), str) or not spec["key"]:
                raise BudgetInputError(f"config.{family} entries require a key")
            key = f"{prefix}:{spec['key']}"
            if not _enabled(spec.get("enabled", True), key + " enabled"):
                skipped.append({"key": key, "reason": "disabled"})
                continue
            if spec.get("query_id") is None or amount(spec.get("cadence_minutes"), key + " cadence") <= 0:
                raise BudgetInputError(f"{key} requires query_id and positive cadence")
            params = {} if family == "presentation" else (
                None if spec.get("lookback_hours") is None else {"lookback_hours": spec["lookback_hours"]})
            entries.append({"key": key, "query_id": spec["query_id"], "query_parameters": params,
                            "performance": spec.get("performance"), "shape_version": spec.get("shape_version"),
                            "cadence_minutes": spec["cadence_minutes"], "enabled": True,
                            "export_policy": "none" if family == "presentation" else spec.get("export_policy", "observed_max")})
    reconciliation = config.get("reconciliation") or {}
    manifest_id = reconciliation.get("manifest_query_id")
    cap = reconciliation.get("manifest_max_export_bytes")
    if cap is not None and amount(cap, "manifest export byte cap") <= 0:
        raise BudgetInputError("manifest export byte cap must be positive")
    for window in ("daily", "weekly"):
        key = f"manifest:{window}"
        if manifest_id is None:
            skipped.append({"key": key, "reason": "no reconciliation manifest_query_id"})
            continue
        cadence = reconciliation.get(f"{window}_cadence_minutes")
        lookback = reconciliation.get(f"{window}_lookback_days")
        if amount(cadence, key + " cadence") <= 0 or type(lookback) is not int or lookback <= 0:
            raise BudgetInputError(f"{key} requires positive cadence and integer lookback")
        entries.append({"key": key, "query_id": manifest_id,
                        "query_parameters": {"lookback_days": lookback}, "cadence_minutes": cadence,
                        "enabled": True, "export_policy": "observed_max"})
    return entries, {"skipped_entries": skipped, "scheduled_keys": [e["key"] for e in entries]}


def collect_outstanding_liabilities(state, config, *, period_start_utc=None, usage_payload=None):
    """Read optional financial lane plus v1 evidence without mutating either.

    Deliberately do not honor cost_reconciliation/settled_execution_ids: an
    aggregate usage snapshot is not provider charge-level inclusion evidence.
    Export identity/time belongs to the read attempt, NEVER its execution date.
    """
    if not isinstance(state, dict):
        raise BudgetInputError("state must be an object")
    # Preserve v1 shape validation, but grant this historical lane NO authority.
    legacy = state.get("cost_reconciliation")
    if legacy is not None:
        if not isinstance(legacy, dict) or not isinstance(legacy.get("usage_snapshot"), dict):
            raise BudgetInputError("Malformed legacy cost_reconciliation snapshot")
        snapshot = legacy["usage_snapshot"]
        amount(snapshot.get("credits_used"), "legacy snapshot credits")
        if not any(snapshot.get(k) is not None for k in ("billing_period_id", "period_start_utc", "period_end_utc")):
            raise BudgetInputError("Legacy snapshot lacks period identity")
        if not isinstance(legacy.get("settled_execution_ids", []), list):
            raise BudgetInputError("Legacy settled_execution_ids must be a list")
    items, excluded = financial_obligations(
        state, period_start_utc=period_start_utc, export_rate=_require_settings(config)[2])
    diagnostics = {"unknown_liability_ids": [], "known_liability_ids": [], "settled_ids_flagged": [],
                   "prior_period_excluded_ids": excluded, "export_liability_ids": [],
                   "result_export_liability_ids": [], "duplicate_ids_skipped": [],
                   "legacy_settlement_ignored": "cost_reconciliation" in state,
                   "financial_lane_present": "financial_journal" in state,
                   "known_floor_credits": sum(max(item.get("cost_credits") or 0, item.get("known_cost_floor_credits", 0.0))
                                              for item in items)}
    for item in items:
        identifier = item["liability_id"]
        diagnostics["known_liability_ids" if item.get("cost_credits") is not None else "unknown_liability_ids"].append(identifier)
        if item["kind"] == "export":
            diagnostics["export_liability_ids"].append(identifier)
            if identifier.startswith("result_export:"):
                diagnostics["result_export_liability_ids"].append(identifier)
    return items, diagnostics


def _resolve_active_period(usage, now_utc):
    selection = normalize_usage(usage, now_utc)
    return selection["period"]["period_start_utc"] if selection["period"] and not selection["blockers"] else None


def evaluate_budget(state, config, usage, now_utc, *, proposed_spec=None, proposed_params=None):
    """Conservative standing-rotation decision. No implicit allowance or approval.

    Planner integration must respect the OVERALL status, not cherry-pick a
    proposal estimate while ignoring unknown liabilities/reserve/base blockers.
    """
    guard, reserve, rate, target, buffer = _require_settings(config)
    policy = _budget_policy(config)
    default, by_key = _performance_by_key(config)
    samples, sample_diagnostics = collect_sample_history(
        state, default_performance=default, performance_by_key=by_key)
    scheduled, schedule_diagnostics = build_scheduled_entries(config)
    start = _resolve_active_period(usage, now_utc)
    items, liability_diagnostics = collect_outstanding_liabilities(
        state, config, period_start_utc=start, usage_payload=usage)
    liability_diagnostics["active_period_start_utc"] = start.isoformat() if start else None
    report = budget_admission(usage, now_utc, samples=samples, scheduled=scheduled, outstanding=items,
                              guard_credits=guard, minimum_reserve_credits=reserve,
                              monthly_target_credits=target, minimum_headroom_below_target=buffer,
                              export_credits_per_megabyte=rate, default_sample_performance=default, **policy)
    blockers = list(report["blockers"])
    proposal = {"status": "proposed_not_requested"}
    if proposed_spec is None and proposed_params is not None:
        raise BudgetInputError("proposed_params requires proposed_spec")
    if proposed_spec is not None:
        if not isinstance(proposed_spec, dict):
            raise BudgetInputError("proposed_spec must be an object")
        spec = dict(proposed_spec)
        if spec.get("query_parameters") is None:
            spec["query_parameters"] = proposed_params
        estimate = execution_admission(spec, samples, now_utc=now_utc,
                                       default_sample_performance=default,
                                       export_credits_per_megabyte=rate, **policy)
        if estimate["status"] != "sampled":
            reason = "unknown_proposed_cost:" + str(spec.get("key", spec.get("query_id"))) + ": no defensible sample-backed estimate"
            proposal = {"status": "blocked", "admission": estimate, "blocker": reason}
            blockers.append(reason)
        elif not report["totals"] or report["totals"]["dependencies_total"] is None:
            proposal = {"status": "blocked", "blockers": list(report["blockers"]), "admission": estimate}
        else:
            totals = report["totals"]
            committed = totals["usage_plus_reserve"] + totals["dependencies_total"]
            headroom = min(totals["limit_credits"] - committed,
                           target - (buffer or 0) - committed)
            proposal_blockers = []
            if estimate["cost_estimate_credits"] > headroom + 1e-9:
                proposal_blockers.append(f"proposed_execution_blocked: estimate {estimate['cost_estimate_credits']:.6f} "
                                         f"exceeds the {headroom:.6f} credits left after confirmed usage, "
                                         "reserve, outstanding liabilities and scheduled forecast")
            # Do not label a proposal admitted when the base is unknown/blocked.
            proposal = {"status": "blocked" if blockers or proposal_blockers else "admitted",
                        "cost_estimate_credits": estimate["cost_estimate_credits"],
                        "headroom_after_base": headroom, "evidence": estimate,
                        "blockers": list(blockers) + proposal_blockers}
            blockers.extend(proposal_blockers)
    return {"status": "blocked" if blockers else "admitted", "blockers": blockers,
            "report": report, "proposed_evaluation": proposal,
            "authorization_required": True,
            "diagnostics": {"samples": sample_diagnostics, "scheduled": schedule_diagnostics,
                            "liabilities": liability_diagnostics,
                            "config_policy": {"monthly_spend_guard_credits": guard,
                                              "minimum_credit_reserve": reserve,
                                              "monthly_target_credits": target,
                                              "minimum_target_headroom_credits": buffer,
                                              "export_credits_per_megabyte": rate,
                                              "default_performance": default,
                                              "performance_by_key": by_key,
                                              **{f"budget_policy.{k}": v for k, v in policy.items()}}}}
