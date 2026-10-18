"""Runtime adapter wiring real pipeline state into credit-budget admission.

Pure and offline dict-in/dict-out bridge between ``v3_execution_tracker``
state, ``config/v3_refresh.json`` and the pure admission module
(``v3_credit_budget``). Every spend decision ends sample-backed or blocked:

* ``execution_history`` / ``pending_executions`` become outstanding
  liabilities (compute), plus separate export liabilities drawn from
  metered export telemetry on the same rows.
* Every ``result_exports`` lane marker counts as its own export liability
  until an authoritative ``cost_reconciliation`` proof settles it: cached/
  consumed markers are NOT "already incurred" — they bill the caller's
  ``risk_credits`` estimate; requested/in_progress/unknown stay UNKNOWN
  and dominate. Markers whose execution id matches a history row share the
  row's ``export:<execution-id>`` liability id, and same-id contributors
  merge on max cost (unknown dominates known: no laundering). Markers
  without an anchor keep the ``result_export:<key>`` fallback id.
* ``consumed_executions`` and a bare ``settled_execution_ids`` list are NOT
  settlement proof: they do not certify that confirmed usage includes the
  cost, so they are never consulted here. Exclusions come only from
  the explicit ``state["cost_reconciliation"]`` lane AND only when its
  ``usage_snapshot`` binds to the CURRENT verified usage payload (equal
  ``credits_used`` and equal period identity evidence); stale or unreadable
  snapshots never honor the skip. Without a bound lane every known cost
  stays outstanding.
* History entries whose ``execution_ended_at`` is strictly before the
  active billing period's start are prior-period history: excluded from
  outstanding liabilities so previous spending does not double count into
  the current period. Exports without such a history provider are placed
  by their marker's ``requested_at_utc``; undated/unparseable evidence is
  retained conservatively.
* Scheduled sources keep their configured ``lookback_hours`` parameters and
  the daily/weekly manifests their own ``lookback_days`` values (parameters
  belong to the estimator identity); presentations are chart-embedded and
  exempt from export evidence.
* Wired byte budgets are caps, never precharged export quotes; the forecast
  derives export estimates from sample telemetry at the configured
  ``export_credits_per_megabyte`` rate (blocked when evidence is missing).
"""

from v3_credit_budget import (
    BudgetInputError,
    DEFAULT_EXPORT_CREDITS_PER_MB,
    DEFAULT_MIN_COMPARABLE_SAMPLES,
    DEFAULT_SAMPLE_MAX_AGE_HOURS,
    DEFAULT_VARIANCE_UPLIFT_SIGMA,
    budget_admission,
    execution_admission,
    outstanding_liabilities,
    parse_instant,
)
from v3_result_cache import (
    CACHE_STATUSES,
    OUTSTANDING_RESULT_EXPORT_STATUSES,
)

_PRESENTATIONS_ARE_CHART_EMBEDDED = "none"  # no results download, no export bill
_DEFAULT_SOURCE_EXPORT_POLICY = "observed_max"
_BUDGET_POLICY_KEYS = (
    "min_comparable_samples",
    "sample_max_age_hours",
    "variance_uplift_sigma",
)


def _validate_flag_bool(value, label, required=False):
    if value is None and not required:
        return False
    if not isinstance(value, bool):
        raise BudgetInputError(f"{label} must be a bool, got {value!r}")
    if required and value is False:
        raise BudgetInputError(f"{label} must be true, got {value!r}")
    return value


def _require_settings(config):
    """Extract the fail-closed budget knobs; a missing value blocks loudly
    at config-build time so bad config can never loosen a limit."""
    if not isinstance(config, dict):
        raise BudgetInputError("config must be an object")
    guard = config.get("monthly_spend_guard_credits")
    if not (isinstance(guard, (int, float)) and not isinstance(guard, bool)
            and guard > 0):
        raise BudgetInputError(
            "config.monthly_spend_guard_credits must be a positive number"
        )
    reserve = config.get("minimum_credit_reserve")
    if not (isinstance(reserve, (int, float)) and not isinstance(reserve, bool)
            and reserve > 0):
        raise BudgetInputError(
            "config.minimum_credit_reserve must be a positive number"
        )
    rate = config.get("export_credits_per_megabyte")
    if not (isinstance(rate, (int, float)) and not isinstance(rate, bool)
            and rate > 0):
        raise BudgetInputError(
            "config.export_credits_per_megabyte must be a positive number"
        )
    target = config.get("monthly_target_credits")
    if not (isinstance(target, (int, float)) and not isinstance(target, bool)
            and target > 0):
        raise BudgetInputError(
            "config.monthly_target_credits must be a positive number"
        )
    reconciliation = config.get("reconciliation") or {}
    if not isinstance(reconciliation, dict):
        raise BudgetInputError("config.reconciliation must be an object")
    headroom = reconciliation.get("minimum_target_headroom_credits")
    headroom_value = None
    if headroom is not None:
        if not (isinstance(headroom, (int, float)) and not isinstance(headroom, bool)
                and headroom > 0):
            raise BudgetInputError(
                "config.reconciliation.minimum_target_headroom_credits must be "
                "a positive number"
            )
        headroom_value = headroom
    return guard, reserve, rate, target, headroom_value


def _budget_policy(config):
    policy = config.get("budget_policy") or {}
    if not isinstance(policy, dict):
        raise BudgetInputError("config.budget_policy must be an object")
    overrides = {}
    for key in _BUDGET_POLICY_KEYS:
        if key in policy and policy[key] is not None:
            overrides[key] = policy[key]
    kwargs = {
        "min_comparable_samples": DEFAULT_MIN_COMPARABLE_SAMPLES,
        "sample_max_age_hours": DEFAULT_SAMPLE_MAX_AGE_HOURS,
        "variance_uplift_sigma": DEFAULT_VARIANCE_UPLIFT_SIGMA,
    }
    kwargs.update(overrides)
    return kwargs


def _performance_by_key(config):
    """Per-key engine tiers used to bound estimator identities. Sources carry
    their own ``performance`` in the real config; presentations default to
    the pipeline tier."""
    default_performance = config.get("performance", "medium")
    if not isinstance(default_performance, str) or not default_performance:
        raise BudgetInputError(
            "config.performance must be a non-empty string"
        )
    by_key = {}
    budget_policy = config.get("budget_policy") or {}
    if not isinstance(budget_policy, dict):
        raise BudgetInputError("config.budget_policy must be an object")
    explicit = budget_policy.get("sample_performance_overrides") or {}
    if not isinstance(explicit, dict):
        raise BudgetInputError(
            "config.budget_policy.sample_performance_overrides must be an object"
        )
    for entry in config.get("sources", []):
        if not isinstance(entry, dict):
            raise BudgetInputError("config.sources must contain objects")
        key = entry.get("key")
        performance = entry.get("performance")
        if key is not None and performance is not None:
            if not isinstance(performance, str) or not performance:
                raise BudgetInputError(
                    f"source {key!r} performance must be a non-empty string"
                )
            by_key[f"source:{key}"] = performance
    by_key.update(explicit)
    return default_performance, by_key


def collect_sample_history(state, *, default_performance, performance_by_key):
    """Map tracker ``execution_history`` into estimator-shaped sample rows.

    Rows without a compute cost are dropped from the sample set (they are
    unknown-cost work and must surface as liabilities, not as comparables).
    Anchor preference mirrors the module: completion, start, submission.
    Returns ``(samples, diagnostics)``.
    """
    if not isinstance(state, dict):
        raise BudgetInputError("state must be an object")
    history = state.get("execution_history", [])
    if not isinstance(history, list):
        raise BudgetInputError("state.execution_history must be a list")
    if not isinstance(performance_by_key, dict):
        raise BudgetInputError("performance_by_key must be an object")
    samples = []
    diagnostics = {
        "costless_history_entries": 0,
        "default_default_performance": default_performance,
        "per_key_performance_applied": 0,
    }
    for index, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise BudgetInputError(
                f"state.execution_history[{index}] is not an object"
            )
        cost = entry.get("execution_cost_credits")
        if cost is None:
            diagnostics["costless_history_entries"] += 1
            continue
        key_label = entry.get("key")
        performance = entry.get("performance")
        if performance is None:
            performance = performance_by_key.get(key_label)
            if performance is not None:
                diagnostics["per_key_performance_applied"] += 1
        samples.append(
            {
                "key": key_label,
                "query_id": entry.get("query_id"),
                "execution_id": entry.get("execution_id"),
                "execution_cost_credits": cost,
                "query_parameters": entry.get("query_parameters"),
                "performance": performance,
                "shape_version": entry.get("shape_version",
                                           entry.get("query_shape_version")),
                "result_bytes": entry.get("result_bytes",
                                          entry.get("result_set_bytes")),
                "export_wire_bytes": entry.get("export_wire_bytes"),
                "estimated_export_credits": entry.get(
                    "estimated_export_credits"
                ),
                "execution_ended_at": entry.get("execution_ended_at"),
                "execution_started_at": entry.get("execution_started_at"),
                "submitted_at_utc": entry.get("submitted_at_utc"),
            }
        )
    return samples, diagnostics


def build_scheduled_entries(config):
    """Translate the refresh rotation into admission scheduled entries.

    Real config sources of truth (``config/v3_refresh.json``):

    * ``sources[]``: ``lookback_hours`` (per source, e.g. 2 and 26) becomes a
      query parameter; export policy defaults to observed-maximum so metered
      export telemetry feeds the quote unless config opts out explicitly.
    * ``presentation[]``: chart-embedded dashboards, results stay out of the
      API meter; export policy ``none`` with no query parameters.
    * ``reconciliation``: one manifest query shells out the daily/weekly
      lookbacks as separate ``lookback_days`` parameters.
      ``manifest_max_export_bytes`` is validated as a positive run-time
      compliance cap only — it is NOT wired as a precharged export quote.

    Integrity repair runs are opportunistic (cooldown/candidate driven) and
    therefore belong to the ``proposed_spec`` lane of ``evaluate_budget``,
    NOT the steady rotation forecast.
    """
    (guard_credits, reserve_credits, export_rate, target_credits,
     minimum_headroom) = _require_settings(config)
    _ = guard_credits, reserve_credits, export_rate, target_credits, minimum_headroom

    entries = []
    diagnostics = {"skipped_entries": []}

    for source in config.get("sources", []):
        if not isinstance(source, dict):
            raise BudgetInputError("config.sources must contain objects")
        key = source.get("key")
        if not isinstance(key, str) or not key:
            raise BudgetInputError(
                f"config source has no usable key: {source!r}"
            )
        enabled = _validate_flag_bool(source.get("enabled", True),
                                      f"source {key} enabled")
        query_id = source.get("query_id")
        cadence = source.get("cadence_minutes")
        if not enabled:
            diagnostics["skipped_entries"].append(
                {"key": f"source:{key}", "reason": "disabled"}
            )
            continue
        if query_id is None:
            raise BudgetInputError(f"source {key} has no query_id")
        if not (isinstance(cadence, (int, float)) and not isinstance(cadence, bool)
                and cadence > 0):
            raise BudgetInputError(f"source {key} cadence_minutes must be positive")
        lookback_hours = source.get("lookback_hours")
        parameters = (
            None if lookback_hours is None else {"lookback_hours": lookback_hours}
        )
        entries.append(
            {
                "key": f"source:{key}",
                "query_id": query_id,
                "query_parameters": parameters,
                "performance": source.get("performance"),
                "cadence_minutes": cadence,
                "enabled": True,
                "export_policy": source.get(
                    "export_policy", _DEFAULT_SOURCE_EXPORT_POLICY
                ),
            }
        )

    for presentation in config.get("presentation", []):
        if not isinstance(presentation, dict):
            raise BudgetInputError("config.presentation must contain objects")
        key = presentation.get("key")
        if not isinstance(key, str) or not key:
            raise BudgetInputError(
                f"config presentation has no usable key: {presentation!r}"
            )
        query_id = presentation.get("query_id")
        cadence = presentation.get("cadence_minutes")
        if query_id is None:
            raise BudgetInputError(f"presentation {key} has no query_id")
        if not (isinstance(cadence, (int, float))
                and not isinstance(cadence, bool) and cadence > 0):
            raise BudgetInputError(
                f"presentation {key} cadence_minutes must be positive"
            )
        entries.append(
            {
                "key": f"presentation:{key}",
                "query_id": query_id,
                "query_parameters": {},
                "performance": presentation.get("performance"),
                "cadence_minutes": cadence,
                "enabled": True,
                "export_policy": _PRESENTATIONS_ARE_CHART_EMBEDDED,
            }
        )

    reconciliation = config.get("reconciliation") or {}
    manifest_query_id = reconciliation.get("manifest_query_id")
    manifest_bytes = reconciliation.get("manifest_max_export_bytes")
    for window, cadence_key, lookback_key in (
        ("daily", "daily_cadence_minutes", "daily_lookback_days"),
        ("weekly", "weekly_cadence_minutes", "weekly_lookback_days"),
    ):
        cadence = reconciliation.get(cadence_key)
        lookback_days = reconciliation.get(lookback_key)
        if manifest_query_id is None:
            diagnostics["skipped_entries"].append(
                {"key": f"manifest:{window}",
                 "reason": "no reconciliation manifest_query_id"}
            )
            continue
        if not (isinstance(cadence, (int, float))
                and not isinstance(cadence, bool) and cadence > 0):
            raise BudgetInputError(
                f"reconciliation.{cadence_key} must be positive"
            )
        if not (isinstance(lookback_days, int) and not isinstance(
                lookback_days, bool) and lookback_days > 0):
            raise BudgetInputError(
                f"reconciliation.{lookback_key} must be a positive integer"
            )
        if manifest_bytes is not None:
            if not (isinstance(manifest_bytes, (int, float))
                    and not isinstance(manifest_bytes, bool)
                    and manifest_bytes > 0):
                raise BudgetInputError(
                    "reconciliation.manifest_max_export_bytes must be positive"
                )
        # The configured byte cap is a run-time compliance limit, not a
        # precharged export quote; the module derives export costs only from
        # sample telemetry.
        entries.append(
            {
                "key": f"manifest:{window}",
                "query_id": manifest_query_id,
                "query_parameters": {"lookback_days": lookback_days},
                "cadence_minutes": cadence,
                "enabled": True,
                "export_policy": _DEFAULT_SOURCE_EXPORT_POLICY,
            }
        )

    diagnostics["scheduled_keys"] = [entry["key"] for entry in entries]
    return entries, diagnostics


_EXPORT_HINT_KEYS = (
    "estimated_export_credits",
    "total_result_set_bytes",
    "result_bytes",
    "result_set_bytes",
    "export_wire_bytes",
    "export_points",
)


def _export_liability_item(entry, liability_id, export_rate):
    """Build the export liability for one metered execution row.

    Returns None when the row shows no export attempt at all. Incurred
    exports become DISTINCT liabilities: a known credit figure (metered
    ``estimated_export_credits``) or byte evidence at the configured rate
    yields a costed item; any export hint without costable evidence yields
    an UNKNOWN item (blocked, never capped). Malformed numeric evidence is
    treated as unknown rather than assumed cheap. Only INCURRED bytes count
    (``min(attempted, delivered)`` guidelines live at the tracker; here the
    row's fields are already what the tracker recorded).
    """
    hints = [key for key in _EXPORT_HINT_KEYS if entry.get(key) is not None]
    if not hints:
        return None
    credit_value = entry.get("estimated_export_credits")
    known_cost = None
    if credit_value is not None:
        if not (isinstance(credit_value, (int, float))
                and not isinstance(credit_value, bool) and credit_value >= 0):
            return {"kind": "export", "liability_id": liability_id}
        known_cost = float(credit_value)
    byte_values = []
    for key in ("total_result_set_bytes", "result_bytes", "result_set_bytes",
                "export_wire_bytes"):
        value = entry.get(key)
        if value is None:
            continue
        if not (isinstance(value, (int, float))
                and not isinstance(value, bool) and value >= 0):
            return {"kind": "export", "liability_id": liability_id}
        byte_values.append(value)
    if known_cost is not None:
        return {
            "kind": "export",
            "liability_id": liability_id,
            "cost_credits": known_cost,
        }
    if byte_values:
        bytes_cost = max(byte_values) / 1_000_000.0 * export_rate
        return {
            "kind": "export",
            "liability_id": liability_id,
            "cost_credits": bytes_cost,
        }
    return {"kind": "export", "liability_id": liability_id}


def _liability_items_from_row(entry, liability_id, export_liability_id,
                              export_rate):
    """Split one metered row into its compute + export liability items."""
    cost = entry.get("execution_cost_credits")
    if cost is not None:
        if not (isinstance(cost, (int, float)) and not isinstance(cost, bool)
                and cost >= 0):
            raise BudgetInputError(
                f"execution {liability_id} cost must be non-negative"
            )
    items = [{"kind": "execution", "liability_id": liability_id}]
    if cost is not None:
        items[0]["cost_credits"] = cost
    export_item = _export_liability_item(entry, export_liability_id,
                                         export_rate)
    if export_item is not None:
        items.append(export_item)
    return items


def collect_outstanding_liabilities(state, config, *, period_start_utc=None,
                                    usage_payload=None):
    """Fold unsettled work into liability items for the admission module.

    ``period_start_utc`` is the parsed start of the active billing period
    (datetime or exact string). History rows whose ``execution_ended_at``
    parses to a value strictly before it are prior-period history and are
    excluded so earlier spending does not double count into the current
    period; rows without an ``execution_ended_at`` (or with an unparseable
    one) are conservatively retained. When no usable period start is given,
    nothing is filtered by period (the module still blocks on an unusable
    billing payload).

    ``usage_payload`` is the CURRENT verified usage payload. The
    reconciliation lane honors the settle skip ONLY when its
    ``usage_snapshot`` binds to that payload: equal numeric
    ``credits_used`` plus matching period identity evidence (exact
    ``billing_period_id`` equality against the payload's id when present,
    or exact parsed period bounds). With ``usage_payload=None`` (or
    anything unreadable) a lane can never bind and every known cost stays
    outstanding: stale snapshot tokens never launder into a skip.

    Lane markers (``state["result_exports"]["requests"]``): cached and
    consumed markers still count as their own export liability carrying
    the marker's ``risk_credits`` estimate (terminal does NOT mean
    "already in billing"); outstanding attempts stay UNKNOWN and dominate
    known numbers. Anchor-bearing markers use the shared
    ``export:<execution-id>`` id when the execution exists in history, so
    same-charge contributors merge on max cost and can never double
    count; unparseable/absent numeric estimates stay unknown. Prior-period
    membership uses the history provider's row when the execution is known
    there, otherwise the marker's ``requested_at_utc``. Markers
    without an anchor fall back to ``result_export:<key>`` ids.

    Settlement policy: ONLY the ``state["cost_reconciliation"]`` lane may
    mark a known cost as already included in confirmed usage. It must carry
    ``usage_snapshot`` (an object with numeric ``credits_used`` >= 0 and
    some period identity evidence such as ``billing_period_id`` or
    ``period_start_utc``/``period_end_utc``) plus ``settled_execution_ids``.
    A missing lane keeps every known cost outstanding (conservative);
    a malformed lane raises rather than being ignored. ``consumed_executions``
    and a bare ``settled_execution_ids`` list are deliberately NOT consulted:
    result consumption does not certify the cost is inside confirmed usage.

    Dedup/unknown principles (module costs may merge identical ids):

    * ``pending_executions`` without ``execution_id`` or with unknown cost
      → UNKNOWN liability (blocked, never capped).
    * A pending entry whose execution id already exists in
      ``execution_history`` contributes nothing extra.
    * History rows produce compute liabilities; their metered export
      telemetry produces separate export liabilities (distinct ids), so an
      incurred export can never hide inside a compute figure.
    * Pending terminal readings with a cost and no history row yet become
      compute/export liabilities under the same ``exec:<id>`` so metered
      work cannot hide between observe() snapshots.
    """
    if not isinstance(state, dict):
        raise BudgetInputError("state must be an object")
    pending = state.get("pending_executions")
    if pending is None:
        pending = {}
    if not isinstance(pending, dict):
        raise BudgetInputError("state.pending_executions must be an object")
    history = state.get("execution_history", [])
    if not isinstance(history, list):
        raise BudgetInputError("state.execution_history must be a list")

    settled = set()
    reconciliation = state.get("cost_reconciliation")
    if reconciliation is not None:
        if not isinstance(reconciliation, dict):
            raise BudgetInputError(
                "state.cost_reconciliation must be an object"
            )
        snapshot = reconciliation.get("usage_snapshot")
        if not isinstance(snapshot, dict):
            raise BudgetInputError(
                "state.cost_reconciliation.usage_snapshot must be an object"
            )
        used = snapshot.get("credits_used")
        if not (isinstance(used, (int, float)) and not isinstance(used, bool)
                and used >= 0):
            raise BudgetInputError(
                "state.cost_reconciliation.usage_snapshot.credits_used must "
                "be a non-negative number"
            )
        has_period_evidence = (
            isinstance(snapshot.get("billing_period_id"), str)
            or snapshot.get("period_start_utc") is not None
            or snapshot.get("period_end_utc") is not None
        )
        if not has_period_evidence:
            raise BudgetInputError(
                "state.cost_reconciliation.usage_snapshot needs period "
                "identity evidence (billing_period_id or period bounds)"
            )
        ids = reconciliation.get("settled_execution_ids")
        if ids is None:
            ids = []
        if not isinstance(ids, list):
            raise BudgetInputError(
                "state.cost_reconciliation.settled_execution_ids must be a list"
            )
        # Only a snapshot bound to the CURRENT verified usage and its period
        # may honor the settle skip; a stale token (or no usage evidence at
        # all) keeps every landed/unlanded known cost outstanding.
        if _reconciliation_bound(snapshot, usage_payload):
            settled.update(str(identifier) for identifier in ids)

    def _prior_period(entry):
        if period_start_utc is None:
            return False
        ended_raw = entry.get("execution_ended_at")
        if ended_raw is None or not isinstance(ended_raw, str):
            return False
        try:
            ended = parse_instant(ended_raw, "execution_ended_at")
        except BudgetInputError:
            return False  # unparseable evidence: retain (conservative)
        return ended < period_start_utc

    history_ids = set()
    for index, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise BudgetInputError(
                f"state.execution_history[{index}] is not an object"
            )
        execution_id = entry.get("execution_id")
        if execution_id is not None:
            history_ids.add(str(execution_id))

    raw_items = []
    prior_export_ids = set()
    diagnostics = {
        "unknown_liability_ids": [],
        "known_liability_ids": [],
        "settled_ids_flagged": [],
        "prior_period_excluded_ids": [],
        "export_liability_ids": [],
        "result_export_liability_ids": [],
    }

    def _emit(row, liability_id, settling_id, export_liability_id):
        if _prior_period(row):
            diagnostics["prior_period_excluded_ids"].append(liability_id)
            if settling_id is not None:
                prior_export_ids.add(f"export:{settling_id}")
            return
        for item in _liability_items_from_row(
                row, liability_id, export_liability_id, export_rate):
            if settling_id is not None and settling_id in settled:
                item["included_in_confirmed_usage"] = True
            raw_items.append(item)

    export_rate = _require_settings(config)[2]

    for index, entry in enumerate(history):
        execution_id = entry.get("execution_id")
        if execution_id is not None:
            _emit(
                entry,
                f"exec:{execution_id}",
                str(execution_id),
                f"export:{execution_id}",
            )
        else:
            _emit(entry, f"exec:history[{index}]", None,
                  f"export:exec:history[{index}]")

    for key, entry in pending.items():
        if not isinstance(entry, dict):
            raise BudgetInputError(
                f"state.pending_executions[{key!r}] is not an object"
            )
        execution_id = entry.get("execution_id")
        liability_id_base = (
            f"exec:{execution_id}" if execution_id is not None
            else f"pending:{key}"
        )
        if execution_id is None:
            rows = [(liability_id_base, entry, None, f"export:{key}")]
        elif str(execution_id) in history_ids:
            continue  # history already accounts for this execution
        else:
            terminal_status = entry.get("terminal_status")
            if terminal_status is None:
                terminal_status = {}
            if not isinstance(terminal_status, dict):
                raise BudgetInputError(
                    f"pending {key} terminal_status must be an object"
                )
            rows = [
                (liability_id_base, terminal_status, str(execution_id),
                 f"export:{execution_id}")
            ]
        for liability_id, row, settling_id, export_id in rows:
            if any(
                item["liability_id"] == liability_id
                or item["liability_id"] == export_id
                for item in raw_items
            ):
                continue
            _emit(row, liability_id, settling_id, export_id)

    # ``v3_result_cache`` request intents join the fold directly. A cached
    # or consumed marker does NOT mean its export cost already sits inside
    # the confirmed usage figure: until an authoritative
    # ``cost_reconciliation`` proof settles it, it still counts as its own
    # export liability carrying the caller's ``risk_credits`` estimate.
    # Outstanding attempts (requested/in_progress/unknown) stay UNKNOWN so
    # the attempt blocks instead of being capped. Markers whose anchor
    # execution id already exists in history share that row's
    # ``export:<execution-id>`` liability id; the fold merges same-id
    # contributors below (max numeric cost, unknown dominating known, no
    # laundering). Anchors absent from history fall back to
    # ``result_export:<key>`` with no merge partner. Prior-period
    # membership follows the history provider's row when the execution is
    # known there, otherwise the marker's ``requested_at_utc`` (unparseable
    # or missing evidence is retained conservatively). A missing lane keeps
    # the schema optional; a present-but-malformed lane raises.
    result_exports = state.get("result_exports")
    if result_exports is not None:
        if not isinstance(result_exports, dict):
            raise BudgetInputError("state.result_exports must be an object")
        requests = result_exports.get("requests")
        if not isinstance(requests, dict):
            raise BudgetInputError(
                "state.result_exports.requests must be an object"
            )
        for key, marker in requests.items():
            if not isinstance(marker, dict):
                raise BudgetInputError(
                    f"state.result_exports.requests[{key!r}] is not an object"
                )
            status = marker.get("status")
            if status not in CACHE_STATUSES:
                raise BudgetInputError(
                    f"state.result_exports.requests[{key!r}] has unsupported "
                    f"status {status!r}"
                )
            anchor = marker.get("execution_id")
            shared = isinstance(anchor, str) and bool(anchor)
            liability_id = (
                f"export:{anchor}" if shared else f"result_export:{key}"
            )
            item = {"kind": "export", "liability_id": liability_id}
            if status not in OUTSTANDING_RESULT_EXPORT_STATUSES:
                estimate = marker.get("risk_credits")
                if (isinstance(estimate, (int, float))
                        and not isinstance(estimate, bool)
                        and estimate >= 0):
                    item["cost_credits"] = float(estimate)
                # unknown otherwise: a terminal intent is not assumed cheap.
            if shared and anchor in history_ids:
                prior = liability_id in prior_export_ids
            else:
                prior = _marker_prior(
                    marker.get("requested_at_utc"), period_start_utc
                )
            if prior:
                diagnostics["prior_period_excluded_ids"].append(liability_id)
                continue
            diagnostics["result_export_liability_ids"].append(liability_id)
            if shared and str(anchor) in settled:
                item["included_in_confirmed_usage"] = True
            raw_items.append(item)

    # Same-charge dedup: contributors sharing one liability id (a history
    # row's metered export estimate and a cached/consumed marker for the
    # same execution) merge on max numeric cost; an unknown contribution
    # dominates known numbers so shared ids can never be laundered into a
    # capped known figure.
    merged = {}
    ordered_ids = []
    for item in raw_items:
        liability_id = item["liability_id"]
        current = merged.get(liability_id)
        if current is None:
            merged[liability_id] = dict(item)
            ordered_ids.append(liability_id)
            continue
        if "cost_credits" in item and "cost_credits" in current:
            current["cost_credits"] = max(
                current["cost_credits"], item["cost_credits"])
        elif "cost_credits" in item or "cost_credits" in current:
            current.pop("cost_credits", None)
        if item.get("included_in_confirmed_usage") is True:
            current["included_in_confirmed_usage"] = True
    items = [merged[key] for key in ordered_ids]
    for item in items:
        if item["kind"] == "export":
            diagnostics["export_liability_ids"].append(item["liability_id"])
        if item.get("cost_credits") is not None:
            diagnostics["known_liability_ids"].append(item["liability_id"])
        else:
            diagnostics["unknown_liability_ids"].append(item["liability_id"])
        if item.get("included_in_confirmed_usage") is True:
            diagnostics["settled_ids_flagged"].append(item["liability_id"])

    evaluated = outstanding_liabilities(items)
    diagnostics["duplicate_ids_skipped"] = [
        skipped["liability_id"] for skipped in evaluated["skipped"]
        if skipped["reason"] in ("duplicate_id",)
    ]
    # The pure module files unknown-cost entries under ``unknown`` (not
    # ``items``); the admission decision needs them IN the item list, so
    # merge them back once per id.
    final_items = list(evaluated["items"])
    represented_ids = {item["liability_id"] for item in final_items}
    for unknown_entry in evaluated["unknown"]:
        if unknown_entry["liability_id"] not in represented_ids:
            final_items.append(
                {
                    "kind": unknown_entry["kind"],
                    "liability_id": unknown_entry["liability_id"],
                }
            )
            represented_ids.add(unknown_entry["liability_id"])
    return final_items, diagnostics


def _proposed_lane(proposed_spec, proposed_params, samples, *, now_utc,
                   default_performance):
    """Admission for one prospective execution; unknown evidence never turns
    into a number."""
    if proposed_spec is None and proposed_params is None:
        return None
    if proposed_spec is None:
        raise BudgetInputError(
            "proposed_params requires proposed_spec to bind them to"
        )
    if not isinstance(proposed_spec, dict):
        raise BudgetInputError("proposed_spec must be an object")
    parameters = proposed_spec.get("query_parameters")
    if parameters is None:
        parameters = proposed_params
    if parameters is not None and not isinstance(parameters, dict):
        raise BudgetInputError("proposed parameters must be an object")
    spec = dict(proposed_spec)
    spec["query_parameters"] = parameters
    admission = execution_admission(
        spec,
        samples,
        now_utc=now_utc,
        default_sample_performance=default_performance,
    )
    return admission


def _resolve_active_period(usage, now_utc):
    """Parse the start instant of the billing period covering ``now_utc``.

    Returns the parsed start (datetime) or None. None also covers unknown
    payloads: the admission module still validates the usage payload itself
    (date-only ends soft-block there), so this lane must not invent its own
    blockers — it only feeds the prior-period liability filter. Date-only
    or otherwise inexact candidates are skipped, and ambiguity between two
    covering periods yields None (conservative: nothing filtered).
    """
    now = parse_instant(now_utc, "now_utc")
    if not isinstance(usage, dict):
        return None
    raw = usage.get("billing_periods")
    if raw is None:
        raw = [usage]
    if not isinstance(raw, list):
        return None
    active = None
    for candidate in raw:
        if not isinstance(candidate, dict):
            continue
        start_raw = candidate.get("period_start_utc")
        end_raw = candidate.get("period_end_utc")
        if not isinstance(start_raw, str) or not isinstance(end_raw, str):
            continue
        try:
            start = parse_instant(start_raw, "period_start_utc")
            end = parse_instant(end_raw, "period_end_utc")
        except BudgetInputError:
            continue  # inexact candidate: module still soft-blocks
        if start <= now < end:
            if active is not None:
                return None  # ambiguous: filter nothing
            active = start
    return active


def _reconciliation_bound(snapshot, usage_payload):
    """True only when the reconciliation snapshot binds to the CURRENT
    verified usage payload: equal numeric ``credits_used`` plus matching
    period identity evidence (exact ``billing_period_id`` equality with the
    payload's id when present, or exact parsed period bounds against a
    period window in the payload). Unreadable or partial evidence never
    binds, so a stale state token never honors the settle skip."""
    if not (isinstance(snapshot, dict) and isinstance(usage_payload, dict)):
        return False
    snapshot_used = snapshot.get("credits_used")
    used_current = usage_payload.get("credits_used")
    if not (isinstance(snapshot_used, (int, float))
            and not isinstance(snapshot_used, bool)
            and isinstance(used_current, (int, float))
            and not isinstance(used_current, bool)):
        return False
    if float(snapshot_used) != float(used_current):
        return False
    snapshot_id = snapshot.get("billing_period_id")
    if isinstance(snapshot_id, str) and snapshot_id:
        usage_id = usage_payload.get("billing_period_id")
        if isinstance(usage_id, str) and usage_id == snapshot_id:
            return True
    snap_start = snapshot.get("period_start_utc")
    snap_end = snapshot.get("period_end_utc")
    if not isinstance(snap_start, str) or not isinstance(snap_end, str):
        return False
    try:
        start = parse_instant(snap_start, "snapshot period_start_utc")
        end = parse_instant(snap_end, "snapshot period_end_utc")
    except BudgetInputError:
        return False
    candidates = usage_payload.get("billing_periods")
    if candidates is None:
        candidates = [usage_payload]
    if not isinstance(candidates, list):
        return False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        used_start = candidate.get("period_start_utc")
        used_end = candidate.get("period_end_utc")
        if not isinstance(used_start, str) or not isinstance(used_end, str):
            continue
        try:
            candidate_start = parse_instant(
                used_start, "usage period_start_utc")
            candidate_end = parse_instant(used_end, "usage period_end_utc")
        except BudgetInputError:
            continue
        if start == candidate_start and end == candidate_end:
            return True
    return False


def _marker_prior(requested_at_utc, period_start_utc):
    """Prior-period decision for a result export marker without a history
    row: its ``requested_at_utc`` anchors the period membership. Unparseable
    or missing evidence is retained (conservative)."""
    if period_start_utc is None:
        return False
    if not isinstance(requested_at_utc, str):
        return False
    try:
        requested = parse_instant(requested_at_utc, "requested_at_utc")
    except BudgetInputError:
        return False
    return requested < period_start_utc


def evaluate_budget(state, config, usage, now_utc, *, proposed_spec=None,
                    proposed_params=None):
    """Pure evaluation entry point recommended for the orchestrator wiring.

    Returns::

        {
            "status": "admitted" | "blocked",
            "blockers": [...],
            "report": <budget_admission report for the standing rotation>,
            "proposed_evaluation":
                {"status": "proposed_not_requested"} |
                {"status": "blocked", "blocker": "..."}          # unknown
                | {"status": "admitted"|"blocked",
                   "cost_estimate_credits": ..., ...},          # headroom
            "diagnostics": {...}
        }

    The proposal verdict does NOT re-run a hypothetical schedule: the
    standing rotation stays scheduled in the base decision, and the
    proposal's sample-backed estimate is compared against the headroom
    that remains after confirmed usage + reserve + outstanding liabilities
    + scheduled forecast (plus the target-buffer line when a monthly
    target with buffer is configured). The scheduled forecast plus the new
    operation estimate is the protected quantity; removing scheduled work
    to make an ad-hoc execution look affordable is refused by design.
    ``status`` is blocked when the standing rotation is blocked OR the
    proposal is not evidenced within headroom (unknown → never stretched
    into a number). No network.
    """
    (guard_credits, reserve_credits, export_rate, target_credits,
     minimum_headroom) = _require_settings(config)
    policy = _budget_policy(config)
    default_performance, performance_by_key = _performance_by_key(config)
    samples, sample_diagnostics = collect_sample_history(
        state,
        default_performance=default_performance,
        performance_by_key=performance_by_key,
    )
    scheduled, scheduled_diagnostics = build_scheduled_entries(config)

    period_start_utc = _resolve_active_period(usage, now_utc)

    items, liability_diagnostics = collect_outstanding_liabilities(
        state, config, period_start_utc=period_start_utc,
        usage_payload=usage,
    )
    liability_diagnostics = dict(liability_diagnostics)
    liability_diagnostics["active_period_start_utc"] = (
        None
        if period_start_utc is None
        else getattr(period_start_utc, "isoformat", lambda: str(
            period_start_utc))()
    )

    report = budget_admission(
        usage,
        now_utc,
        samples=samples,
        scheduled=scheduled,
        outstanding=items,
        guard_credits=guard_credits,
        minimum_reserve_credits=reserve_credits,
        monthly_target_credits=target_credits,
        minimum_headroom_below_target=minimum_headroom,
        export_credits_per_megabyte=export_rate,
        default_sample_performance=default_performance,
        **policy,
    )

    proposed_admission = _proposed_lane(
        proposed_spec,
        proposed_params,
        samples,
        now_utc=now_utc,
        default_performance=default_performance,
    )
    if proposed_admission is None:
        proposed_evaluation = {"status": "proposed_not_requested"}
        proposal_blockers = []
    elif proposed_admission["status"] != "sampled":
        proposed_evaluation = {
            "status": "blocked",
            "admission": proposed_admission,
            "blocker": (
                "unknown_proposed_cost:"
                f"{proposed_spec.get('key', str(proposed_spec.get('query_id')))}: "
                "no defensible sample-backed estimate; refused instead of "
                "inventing a cap-based bound"
            ),
        }
        proposal_blockers = [proposed_evaluation["blocker"]]
    else:
        estimate = proposed_admission["cost_estimate_credits"]
        totals = report["totals"]
        committed = (
            totals["usage_plus_reserve"] + totals["dependencies_total"]
        )
        headroom_after_base = totals["limit_credits"] - committed
        violation_lines = []
        if estimate > headroom_after_base + 1e-9:
            violation_lines.append(
                "proposed_execution_blocked: estimate "
                f"{estimate:.6f} exceeds the {headroom_after_base:.6f} "
                "credits left after confirmed usage "
                f"{totals['credits_used']:.6f}, reserve "
                f"{totals['reserve_credits']:.6f}, outstanding liabilities "
                f"{totals['outstanding_liability_credits']:.6f} and "
                "scheduled forecast "
                f"{totals['scheduled_forecast_credits']:.6f}"
            )
        if target_credits is not None and minimum_headroom is not None:
            target_after = (
                target_credits - minimum_headroom - committed
            )
            if estimate > target_after + 1e-9:
                violation_lines.append(
                    "proposed_execution_blocked: estimate "
                    f"{estimate:.6f} would leave less than the "
                    f"{minimum_headroom:.6f} credit buffer below the "
                    f"monthly target {target_credits:.6f} "
                    f"(room {target_after:.6f} above the committed "
                    f"{committed:.6f})"
                )
        if violation_lines:
            proposed_evaluation = {
                "status": "blocked",
                "cost_estimate_credits": estimate,
                "blockers": violation_lines,
            }
            proposal_blockers = violation_lines
        else:
            proposed_evaluation = {
                "status": "admitted",
                "cost_estimate_credits": estimate,
                "evidence": proposed_admission,
                "headroom_after_base": headroom_after_base,
            }
            proposal_blockers = []

    overall_blockers = list(report["blockers"]) + list(proposal_blockers)
    status = (
        "admitted"
        if report["status"] == "admitted" and not proposal_blockers
        else "blocked"
    )
    return {
        "status": status,
        "blockers": overall_blockers,
        "report": report,
        "proposed_evaluation": proposed_evaluation,
        "diagnostics": {
            "samples": sample_diagnostics,
            "scheduled": scheduled_diagnostics,
            "liabilities": liability_diagnostics,
            "config_policy": {
                "monthly_spend_guard_credits": guard_credits,
                "minimum_credit_reserve": reserve_credits,
                "monthly_target_credits": target_credits,
                "minimum_target_headroom_credits": minimum_headroom,
                "export_credits_per_megabyte": export_rate,
                **{f"budget_policy.{key}": value
                   for key, value in policy.items()},
                "default_performance": default_performance,
                "performance_by_key": dict(performance_by_key),
            },
        },
    }


__all__ = [
    "BudgetInputError",
    "build_scheduled_entries",
    "collect_outstanding_liabilities",
    "collect_sample_history",
    "evaluate_budget",
]
