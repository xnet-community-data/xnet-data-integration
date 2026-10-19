#!/usr/bin/env python3
"""Credit-budget admission for XNET Dune work: pure, offline, fail-closed.

Deterministic dict-in/dict-out (no network, no file I/O, no mutation). Decides
whether Dune work can be ADMITTED under the account credit guard from inputs
already known: confirmed usage (Dune's metadata ``usage`` endpoint), observed
per-execution costs (``execution_cost_credits`` is knowable only after an
execution finishes), export telemetry (``result_bytes``/
``export_wire_bytes``/``estimated_export_credits``), and configured caps that
are POST-RUN enforcement limits, never pre-run cost estimates.

Grounded semantics (docs.dune.com, metadata only): an execution's cost is not
knowable before running it; exports charge per MB; usage may lag. Therefore:

- cost estimates are UPPER-ENVELOPE ESTIMATES: ``max(observed_max,
  mean + uplift_sigma * std)`` over at least ``min_comparable_samples`` fresh
  samples of the exact same (query_id, query_parameters, performance,
  shape_version) identity. Fewer samples, stale anchors, missing timestamps,
  or a shape mismatch are UNKNOWN and block instead of being capped;
- the forecast covers the standing rotation to the exact renewal instant. An
  entry's export component needs comparable recent export evidence (quorum)
  and is estimated with the same envelope at the configured per-MB rate;
  ``export_policy: "none"`` entries are exempt; anything that may export
  without evidence is an unknown cost that blocks. Byte caps are never
  quotes and are rejected as inputs;
- unsettled liabilities (pending executions with unknown terminal cost,
  unknown export intents) block; known compute/export costs stay outstanding.
  Legacy inclusion booleans and aggregate usage equality are not settlement
  evidence. Duplicate liability ids resolve to the MAXIMUM known cost, with
  unknown contributors dominating;
- ceilings: ``limit = min(credits_included, guard, monthly_target)`` (the
  emergency guard must stay configured) plus, optionally, an unforecastable
  buffer below the monthly target.

MALFORMED inputs (NaN, negatives, boolean-number, date-only instants, naive
datetimes, unserializable parameters, broken containers) raise
``BudgetInputError``; well-formed but undecidable or overspending states
return a detailed report (``status``/``blockers``/``reports``/``totals``/
``forecast``) instead of raising, so the pipeline can defer and keep
last-good data.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta, timezone

DEFAULT_GUARD_CREDITS = 1800.0  # monthly_spend_guard_credits; emergency fail-closed value, must stay
DEFAULT_MINIMUM_RESERVE_CREDITS = 15.0  # credits kept permanently unspendable at renewal
DEFAULT_EXPORT_CREDITS_PER_MB = 10.0  # docs.dune.com: Analyst plan charges 10 credits per MB exported
DEFAULT_MIN_COMPARABLE_SAMPLES = 3  # below this the max/mean/uplift basis is not defensible
DEFAULT_VARIANCE_UPLIFT_SIGMA = 3.0  # mean + k*sigma upper-envelope buffer
DEFAULT_SAMPLE_MAX_AGE_HOURS = 168.0  # samples older than one week are stale evidence

_EXPORT_UNSET = object()


class BudgetInputError(ValueError):
    """Malformed budget input; spending must stop until the caller fixes it."""


# ---- shared validation helpers ---------------------------------------------

def _finite_nonnegative_number(value, label, *, zero_allowed=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BudgetInputError(
            f"{label} must be a finite nonnegative number, got {value!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise BudgetInputError(
            f"{label} must be finite (no NaN/Infinity), got {value!r}"
        )
    if number < 0:
        raise BudgetInputError(f"{label} must be nonnegative, got {value!r}")
    if not zero_allowed and number <= 0:
        raise BudgetInputError(f"{label} must be positive, got {value!r}")
    return number


def _strict_bool(value, label):
    if not isinstance(value, bool):
        raise BudgetInputError(f"{label} must be a boolean, got {value!r}")
    return value


def _z(dt):
    """Render a UTC datetime as an ISO-8601 string ending in Z."""
    return dt.isoformat().replace("+00:00", "Z")


def _is_date_only(value):
    """True when a payload value is a calendar date with no exact time."""
    return isinstance(value, str) and "T" not in value.strip()


# ---- identity and instants --------------------------------------------------

def canon_parameters(parameters):
    """Canonical string for exactly this parameterisation; ``None``, missing
    and empty-object parameters all canonicalize to the empty string. Raises
    malformed input for unserializable values or non-dict payloads."""
    if parameters is None:
        return ""
    if not isinstance(parameters, dict):
        raise BudgetInputError(
            f"parameters must be an object or None, got "
            f"{type(parameters).__name__}"
        )
    if not parameters:
        return ""
    try:
        return json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise BudgetInputError(
            f"parameters are not JSON-serializable: {error}"
        ) from None


def sample_identity(query_id, query_parameters, performance, shape_version=None):
    """Grouping identity for observed-execution samples and planned work:
    exactly (query_id, query_parameters, performance, shape_version); nothing
    else may mix."""
    return (
        str(query_id),
        canon_parameters(query_parameters),
        None if performance is None else str(performance),
        None if shape_version is None else str(shape_version),
    )


def parse_instant(value, label):
    """Parse an exact, timezone-aware UTC instant.

    Date-only strings (the midnight-truncation trap) and naive datetimes are
    malformed. ``2026-11-04T00:00:00Z`` is accepted as the exact instant it is.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if "T" not in text:
            raise BudgetInputError(
                f"{label} must be an exact instant with a UTC time component, "
                f"not a midnight-truncated date-only value: {value!r}"
            )
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise BudgetInputError(
                f"{label} is not a valid ISO-8601 instant: {value!r}"
            ) from None
    else:
        raise BudgetInputError(
            f"{label} must be an ISO-8601 instant string or datetime, got "
            f"{type(value).__name__}"
        )
    if parsed.tzinfo is None:
        raise BudgetInputError(
            f"{label} must be timezone-aware; naive instants are malformed: "
            f"{value!r}"
        )
    return parsed.astimezone(timezone.utc)


def select_active_period(billing_periods, now_utc):
    """Select the billing period active at ``now_utc`` from a Dune ``usage``
    payload's ``billing_periods`` (accepts the camelCase ``billingPeriods``).

    Returns ``{"period": normalized_or_None, "reasons": [...], "candidates":
    [...]}``; blocking is a decision, never an exception. Rows with date-only
    edges or invalid counters are dropped with an explaining reason — never
    truncated to midnight to make them usable."""

    now = parse_instant(now_utc, "now_utc")
    if not isinstance(billing_periods, list):
        raise BudgetInputError(
            f"billing_periods must be a list, got "
            f"{type(billing_periods).__name__}"
        )

    reasons = []
    candidates = []
    for index, raw in enumerate(billing_periods):
        if not isinstance(raw, dict):
            reasons.append(f"billing_period[{index}] is not an object")
            continue
        try:
            start = parse_instant(raw.get("start_date"), f"start_date[{index}]")
            end_raw = raw.get("end_date")
            if _is_date_only(end_raw):
                reasons.append(
                    f"end_date[{index}] is date-only; an exact instant is "
                    "required, midnight truncation is refused"
                )
                continue
            end = parse_instant(end_raw, f"end_date[{index}]")
        except BudgetInputError as error:
            reasons.append(
                f"billing_period[{index}] edges are not exact instants: {error}"
            )
            continue
        try:
            used = _finite_nonnegative_number(
                raw.get("credits_used"), f"credits_used[{index}]"
            )
            included = _finite_nonnegative_number(
                raw.get("credits_included"), f"credits_included[{index}]"
            )
        except BudgetInputError as error:
            reasons.append(
                f"billing_period[{index}] has invalid credit counters: {error}"
            )
            continue
        candidates.append(
            {
                "credits_used": used,
                "credits_included": included,
                "period_start_utc": start,
                "period_end_utc": end,
                "start_date_source": raw.get("start_date"),
                "end_date_source": end_raw,
            }
        )

    active = [
        candidate
        for candidate in candidates
        if candidate["period_start_utc"] <= now < candidate["period_end_utc"]
    ]
    if len(active) == 1:
        return {"period": active[0], "reasons": reasons, "candidates": candidates}
    if not active:
        reasons.append("no_active_billing_period")
    else:
        reasons.append("ambiguous_active_billing_period")
    return {"period": None, "reasons": reasons, "candidates": candidates}


def normalize_usage(usage, now_utc):
    """One period/counter parser for admission AND liability attribution.

    No date-only renewal inference, settlement inference, or network I/O.
    Normalized legacy payloads may omit a start; that cannot authorize period
    attribution or a period allowance, but remains readable by v1 callers.
    """
    now = parse_instant(now_utc, "now_utc")
    if not isinstance(usage, dict):
        raise BudgetInputError("usage must be an object")
    if "billing_periods" in usage or "billingPeriods" in usage:
        selected = select_active_period(
            usage.get("billing_periods", usage.get("billingPeriods")), now)
        return {"period": selected["period"], "reasons": selected["reasons"],
                "blockers": [] if selected["period"] else ["no_active_billing_period"]}
    used = _finite_nonnegative_number(usage.get("credits_used"), "usage credits_used")
    included = _finite_nonnegative_number(usage.get("credits_included"), "usage credits_included")
    start = usage.get("period_start_utc")
    start = None if start is None else parse_instant(start, "usage period_start_utc")
    end_raw = usage.get("period_end_utc")
    inexact = _is_date_only(end_raw) or usage.get("exact_end") is False
    end = None if inexact else parse_instant(end_raw, "usage period_end_utc")
    if start is not None and end is not None and start >= end:
        raise BudgetInputError("billing period start must precede end")
    return {"period": {"credits_used": used, "credits_included": included,
                       "period_start_utc": start, "period_end_utc": end,
                       "inexact_source_end_date": end_raw if inexact else None},
            "blockers": ["period_end_not_exact_utc"] if inexact else [],
            "reasons": ["period_end_not_exact_utc: date-only or declared-inexact renewal; "
                        "midnight truncation is refused"] if inexact else []}


# ---- observed samples and the estimator -------------------------------------

_SHAPE_KEYS = ("shape_version", "shape", "schema_version")


def _sample_shape_version(record):
    for key in _SHAPE_KEYS:
        if key in record and record[key] is not None:
            shape = record[key]
            if not isinstance(shape, (str, int)):
                raise BudgetInputError(
                    f"shape_version must be a string or int, got {shape!r}"
                )
            return str(shape)
    return None


def _sample_anchor(record, index):
    """Timeout anchor for staleness: completion, then start, then submission."""
    for key in ("completed_at_utc", "execution_ended_at", "execution_started_at",
                "submitted_at_utc"):
        if record.get(key) is not None:
            return parse_instant(record[key], f"sample[{index}].{key}")
    return None


def _sample_export_bytes(record, index):
    """Number of bytes this sample is evidence the operation must move."""
    value = _EXPORT_UNSET
    for key in ("export_wire_bytes",):
        if record.get(key) is not None:
            candidate = _finite_nonnegative_number(
                record[key], f"sample[{index}].{key}"
            )
            if value is _EXPORT_UNSET or candidate > value:
                value = candidate
    return None if value is _EXPORT_UNSET else value


def _sample_export_credits(record, index):
    if record.get("estimated_export_credits") is None:
        return None
    return _finite_nonnegative_number(
        record["estimated_export_credits"],
        f"sample[{index}].estimated_export_credits",
    )


def group_samples(samples, default_performance=None):
    """Group observed execution records by exact (query_id, parameters,
    performance, shape_version). A record missing ``performance`` inherits
    ``default_performance`` (the pipeline-level engine tier). Validation
    failure is a malformed-state error, not a silent drop."""
    if not isinstance(samples, (list, tuple)):
        raise BudgetInputError("samples must be a list of execution records")
    grouped = {}
    for index, record in enumerate(samples):
        if not isinstance(record, dict):
            raise BudgetInputError(f"sample[{index}] is not an object: {record!r}")
        if record.get("query_id") is None:
            raise BudgetInputError(f"sample[{index}] has no query_id")
        cost = _finite_nonnegative_number(
            record.get("execution_cost_credits"),
            f"sample[{index}] execution_cost_credits",
        )
        parameters = record.get("query_parameters")
        if parameters is None:
            parameters = record.get("parameters")
        performance = record.get("performance")
        if performance is None and default_performance is not None:
            performance = default_performance
        shape_version = _sample_shape_version(record)
        identity = sample_identity(
            record["query_id"], parameters, performance, shape_version
        )
        grouped.setdefault(identity, []).append(
            {
                "execution_cost_credits": cost,
                "anchor_utc": _sample_anchor(record, index),
                "execution_id": record.get("execution_id"),
                "export_bytes": _sample_export_bytes(record, index),
                "export_credits": _sample_export_credits(record, index),
                "shape_version": shape_version,
            }
        )
    return grouped


def sample_estimate(
    grouped_samples,
    query_id,
    query_parameters,
    *,
    now_utc,
    performance=None,
    shape_version=None,
    max_age_hours=DEFAULT_SAMPLE_MAX_AGE_HOURS,
    min_comparable_samples=DEFAULT_MIN_COMPARABLE_SAMPLES,
    variance_uplift_sigma=DEFAULT_VARIANCE_UPLIFT_SIGMA,
    export_rate_per_mb=DEFAULT_EXPORT_CREDITS_PER_MB,
):
    """Observation-backed upper-envelope estimate for exactly one planned
    operation: identity (query_id, query_parameters, performance,
    shape_version). Conservative but explicitly NOT a guarantee:
    ``estimate = max(observed_max, mean + sigma * stdev)`` over at least
    ``min_comparable_samples`` recent (anchor within ``max_age_hours``)
    comparable samples with a known exact timestamp.

    Unknown outcomes (no samples, stale/undated/mismatched evidence,
    insufficient count) return ``{"status": "unknown", "estimate": None,
    "reasons": [...]}`` and callers MUST block instead of inventing a cap."""
    if not isinstance(grouped_samples, dict):
        raise BudgetInputError("grouped_samples must come from group_samples")
    _finite_nonnegative_number(max_age_hours, "max_age_hours")
    if not isinstance(min_comparable_samples, int) or isinstance(
        min_comparable_samples, bool
    ) or min_comparable_samples < 1:
        raise BudgetInputError(
            f"min_comparable_samples must be a positive integer, got "
            f"{min_comparable_samples!r}"
        )
    _finite_nonnegative_number(variance_uplift_sigma, "variance_uplift_sigma")
    identity = sample_identity(query_id, query_parameters, performance, shape_version)
    rows = grouped_samples.get(identity, [])
    folded = _estimate_rows(
        rows,
        now_utc=now_utc,
        max_age_hours=max_age_hours,
        min_count=min_comparable_samples,
        uplift_sigma=variance_uplift_sigma,
        export_rate_per_mb=export_rate_per_mb,
    )
    if folded["status"] == "unknown" and not rows:
        # The exact identity is missing: diagnose whether comparable samples
        # exist under a DIFFERENT shape/version, so a shape-generation change
        # is reported as shape-version-unknown evidence instead of a silent
        # "no samples" that hides the live history.
        siblings = [
            group_id
            for group_id in grouped_samples
            if group_id[:3] == identity[:3] and group_id[3] != identity[3]
        ]
        sibling_rows = sum(
            len(grouped_samples[group_id]) for group_id in siblings
        )
        if siblings:
            sibling_shapes = sorted(
                {str(group_id[3]) for group_id in siblings}
            )
            folded["reasons"].append(
                "shape_version_unknown: "
                f"{sibling_rows} sample(s) exist for this "
                "query+parameters+performance under shape_version(s) "
                f"{sibling_shapes}; none are comparable to requested "
                f"{identity[3]!r}"
            )
            folded["evidence"]["shape_mismatch_count"] = sibling_rows
    folded["identity"] = {
        "query_id": str(query_id),
        "parameters": canon_parameters(query_parameters),
        "performance": None if performance is None else str(performance),
        "shape_version": None if shape_version is None else str(shape_version),
    }
    return folded


def _uplifted_envelope(values, sigma):
    """max(observed_max, mean + sigma*stdev) over a sample series; explicit
    conservative estimate, never a guarantee. Single value or flat series
    degenerate to the observed max."""
    if not values:
        return None
    if len(values) == 1 or len({value for value in values}) == 1:
        return max(values)
    return max(max(values), statistics.fmean(values) + sigma * statistics.stdev(values))


def _estimate_rows(grouped_rows, *, now_utc, max_age_hours, min_count,
                   uplift_sigma, export_rate_per_mb):
    """Filter comparable-recent rows for one identity and fold them into the
    upper-envelope estimate plus export evidence detail. ``shape_version`` is
    part of the identity, so a group's rows always share one shape; a
    requested shape that matches no group is diagnosed by the caller
    (sample_estimate) via the sibling-group scan."""
    now = parse_instant(now_utc, "now_utc")
    total_rows = len(grouped_rows)
    stale, missing_ts = 0, 0
    comparable = []
    for row in grouped_rows:
        anchor = row["anchor_utc"]
        if anchor is None:
            missing_ts += 1
            continue
        if now - anchor > timedelta(hours=max_age_hours):
            stale += 1
            continue
        comparable.append(row)

    def note():
        reasons = []
        if not comparable and total_rows == 0:
            reasons.append("no_samples_for_identity")
        if not comparable and (missing_ts or stale):
            if missing_ts:
                reasons.append(
                    f"{missing_ts} sample(s) have no usable exact timestamp "
                    "(evidence unavailable)"
                )
            if stale:
                reasons.append(
                    f"{stale} sample(s) are stale (older than "
                    f"{max_age_hours:g}h)"
                )
        if 0 < len(comparable) < min_count:
            reasons.append(
                f"insufficient_comparable_samples: {len(comparable)} recent "
                f"comparable sample(s) < required {min_count}"
            )
        return reasons

    if len(comparable) < min_count:
        return {
            "status": "unknown",
            "estimate": None,
            "reasons": note(),
            "evidence": {
                "comparable_count": len(comparable),
                "total_count": total_rows,
                "stale_count": stale,
                "missing_timestamp_count": missing_ts,
                "shape_mismatch_count": 0,
                "cost_min": None, "cost_mean": None, "cost_max": None,
                "sample_ids": [],
            },
        }

    costs = [row["execution_cost_credits"] for row in comparable]
    mean = statistics.fmean(costs)
    observed_max = max(costs)
    estimate = _uplifted_envelope(costs, uplift_sigma)
    export_values = []
    export_rows = 0
    export_bytes_max = None
    for row in comparable:
        candidates = []
        if row["export_credits"] is not None:
            candidates.append(row["export_credits"])
        if export_rate_per_mb is not None and row["export_bytes"] is not None:
            candidates.append(
                row["export_bytes"] / 1_000_000.0 * export_rate_per_mb
            )
            if export_bytes_max is None or row["export_bytes"] > export_bytes_max:
                export_bytes_max = row["export_bytes"]
        if candidates:
            export_values.extend(candidates)
            export_rows += 1
    return {
        "status": "sampled",
        "estimate": estimate,
        "estimate_is_hard_cap": False,
        "estimate_quality": "single_observation_no_dispersion" if len(costs) == 1 else "empirical_envelope",
        "reasons": [],
        "evidence": {
            "comparable_count": len(comparable),
            "total_count": total_rows,
            "stale_count": stale,
            "missing_timestamp_count": missing_ts,
            "shape_mismatch_count": 0,
            "cost_min": min(costs),
            "cost_mean": mean,
            "cost_max": observed_max,
            "export_cost_max": max(export_values) if export_values else None,
            "export_cost_values": export_values,
            "export_evidence_count": export_rows,
            "export_bytes_max": export_bytes_max,
            "sample_ids": [row.get("execution_id") for row in comparable],
        },
    }


def export_liability_estimate(total_result_set_bytes, export_credits_per_megabyte):
    """Computed export liability before the download happens; the observed
    wire cost is reconciled afterwards. Not a sample — a documented per-MB
    rate the caller must pass (mirrors ``export_credits_per_megabyte``)."""
    byte_count = _finite_nonnegative_number(
        total_result_set_bytes, "total_result_set_bytes"
    )
    scale = _finite_nonnegative_number(
        export_credits_per_megabyte, "export_credits_per_megabyte"
    )
    return byte_count / 1_000_000.0 * scale


# ---- unsettled liabilities ---------------------------------------------------

def outstanding_liabilities(items, *, export_credits_per_megabyte=None):
    """Fold unsettled work that Dune's confirmed usage may not yet include
    (the usage endpoint can lag the newest executions).

    Each item is a dict:
      - ``kind``: "execution" | "export" | other label (informational),
      - ``liability_id``: stable identity for de-duplication,
      - ``cost_credits``: observed execution cost, or
        ``total_result_set_bytes``/``result_set_bytes`` for a computed export
        charge using the passed per-MB scale,
      - ``included_in_confirmed_usage``: legacy readable boolean, ignored.
        A flag or numerically matching usage is not charge-level evidence.

    Duplicate ids no longer discard the higher risk: when two entries for the
    same id disagree on cost, the MAXIMUM is used and the conflict is recorded
    in ``reasons``. Items without a credible cost are returned in ``unknown``
    and the caller must block; nothing is capped or guessed.

    ``total`` and ``known_floor_credits`` retain the known numeric portion even
    when the same identity has unknown exposure. ``exposure_is_fully_known`` is
    false in that case; items carry ``cost_is_lower_bound`` and must NOT be
    interpreted as a complete estimate or permission to spend. Refolding those
    items preserves the unknown signal.

    Returns {"total", "known_floor_credits", "exposure_is_fully_known",
             "items", "skipped", "unknown", "reasons"}."""
    if not isinstance(items, (list, tuple)):
        raise BudgetInputError("outstanding liabilities must be a list")
    if export_credits_per_megabyte is not None:
        _finite_nonnegative_number(
            export_credits_per_megabyte, "export_credits_per_megabyte"
        )

    total = 0.0
    items_by_id = {}
    skipped, unknown, reasons = [], [], []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise BudgetInputError(
                f"liability[{index}] is not an object: {item!r}"
            )
        kind = item.get("kind", "other")
        if not isinstance(kind, str):
            raise BudgetInputError(
                f"liability[{index}] kind must be a string, got {kind!r}"
            )
        identity = item.get("liability_id")
        if identity is None:
            identity = f"{kind}:{index}"
        if not isinstance(identity, (str, int)):
            raise BudgetInputError(
                f"liability[{index}] liability_id must be a string or int"
            )
        if "included_in_confirmed_usage" in item:
            included = _strict_bool(
                item["included_in_confirmed_usage"],
                f"liability[{index}] included_in_confirmed_usage",
            )
        else:
            included = False
        if included:
            reasons.append(
                f"liability[{identity}] legacy inclusion flag ignored: "
                "a boolean is not charge-level billing inclusion evidence"
            )

        is_floor = item.get("known_cost_floor_credits") is not None
        if is_floor:
            unknown.append({"liability_id": identity, "kind": kind,
                            "reason": "unknown_above_observed_floor"})
            observed = item.get("cost_credits")
            cost = max(_finite_nonnegative_number(item["known_cost_floor_credits"], "known cost floor"),
                       _finite_nonnegative_number(0 if observed is None else observed, "known cost"))
        elif item.get("cost_credits") is not None:
            cost = _finite_nonnegative_number(
                item["cost_credits"], f"liability[{index}] cost_credits"
            )
        elif item.get("total_result_set_bytes") is not None or item.get(
            "result_set_bytes"
        ) is not None:
            raw_bytes = item.get("total_result_set_bytes")
            if raw_bytes is None:
                raw_bytes = item.get("result_set_bytes")
            if export_credits_per_megabyte is None:
                reasons.append(
                    f"liability[{identity}] has export bytes but no per-MB "
                    "scale was passed; treating as unknown"
                )
                unknown.append(
                    {
                        "liability_id": identity,
                        "kind": kind,
                        "reason": "export_bytes_without_scale",
                    }
                )
                continue
            cost = export_liability_estimate(raw_bytes, export_credits_per_megabyte)
        else:
            reasons.append(f"liability[{identity}] unknown cost; no cap-based guessing")
            unknown.append({"liability_id": identity, "kind": kind,
                            "reason": "no_cost_estimate_available"})
            continue

        if identity in items_by_id:
            previous = items_by_id[identity]["cost_credits"]
            if abs(previous - cost) <= 1e-9:
                skipped.append(
                    {"liability_id": identity, "reason": "duplicate_id"}
                )
                continue
            if cost > previous:
                total += cost - previous
                items_by_id[identity]["cost_credits"] = cost
                reasons.append(
                    f"liability[{identity}] conflicting duplicates "
                    f"({previous:.6f} vs {cost:.6f}): using the maximum so a "
                    "recurring entry cannot hide a higher-risk duplicate"
                )
            else:
                reasons.append(
                    f"liability[{identity}] conflicting duplicates "
                    f"({previous:.6f} vs {cost:.6f}): keeping the maximum "
                    f"{previous:.6f}"
                )
            continue

        total += cost
        items_by_id[identity] = {
            "liability_id": identity,
            "kind": kind,
            "cost_credits": cost,
        }

    # Unknown dominates ADMISSION, not the observed numeric floor. Never report
    # the known portion as zero, or misrepresent that floor as a complete bound.
    unknown = list({item["liability_id"]: item for item in unknown}.values())
    for item in unknown:
        previous = items_by_id.get(item["liability_id"])
        if previous is not None:
            item["known_cost_floor_credits"] = previous["cost_credits"]
            previous["known_cost_floor_credits"] = previous["cost_credits"]
            previous["cost_is_lower_bound"] = True
    return {
        "total": total,
        "known_floor_credits": total,
        "exposure_is_fully_known": not unknown,
        "items": list(items_by_id.values()),
        "skipped": skipped,
        "unknown": unknown,
        "reasons": reasons,
    }


# ---- scheduled forecast ------------------------------------------------------

_EXPORT_POLICIES = ("none", "observed_max")


def scheduled_forecast(
    entries,
    grouped_samples,
    *,
    now_utc,
    period_end_utc,
    default_performance=None,
    min_comparable_samples=DEFAULT_MIN_COMPARABLE_SAMPLES,
    sample_max_age_hours=DEFAULT_SAMPLE_MAX_AGE_HOURS,
    variance_uplift_sigma=DEFAULT_VARIANCE_UPLIFT_SIGMA,
    export_credits_per_megabyte=DEFAULT_EXPORT_CREDITS_PER_MB,
):
    """Forecast scheduled-enabled Dune work between now and renewal — compute
    AND export components.

    Entries mirror the refresh config: sources, presentation and
    reconciliation manifest keys with ``cadence_minutes``, exact
    ``query_parameters`` (sources use their ``lookback_hours`` default; the
    daily and weekly manifest carry their own separate ``lookback_days``)
    and ``enabled``. Export policy per entry:
      - ``"none"``: the entry never exports results (declared, exempt);
      - ``"observed_max"``: per-run export cost is the same upper-envelope
        estimate as compute —``max(observed_max, mean+sigma*stdev)`` over at
        least ``min_comparable_samples`` comparable recent samples that carry
        export evidence (metered bytes under the per-MB rate, or explicit
        ``estimated_export_credits``) — NOT a config byte cap; a due entry
        without that quorum is an unknown export cost (blocked). A zero
        byte observation with quorum is a valid zero-cost quote.
    Occurrence counting uses the EXACT period end (no midnight truncation)
    and is a conservative upper bound: any partial trailing cadence interval
    still counts as one possible run. Unit compute costs follow the sample
    estimator; an unsampled due entry yields ``unknown_keys`` and blocks."""

    if not isinstance(entries, (list, tuple)):
        raise BudgetInputError("scheduled entries must be a list")
    if not isinstance(grouped_samples, dict):
        raise BudgetInputError("grouped_samples must come from group_samples")
    rate = _finite_nonnegative_number(
        export_credits_per_megabyte, "export_credits_per_megabyte"
    )
    end = parse_instant(period_end_utc, "period_end_utc")
    now = parse_instant(now_utc, "now_utc")
    remaining_seconds = max((end - now).total_seconds(), 0.0)

    detail = []
    total = 0.0
    unknown_keys = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BudgetInputError(
                f"scheduled entry[{index}] is not an object: {entry!r}"
            )
        key = entry.get("key")
        if not isinstance(key, (str, int)):
            raise BudgetInputError(
                f"scheduled entry[{index}] needs a string or int key, got "
                f"{key!r}"
            )
        enabled = True
        if "enabled" in entry:
            enabled = _strict_bool(entry["enabled"], f"scheduled entry {key} enabled")
        if not enabled:
            detail.append(
                {
                    "key": key,
                    "status": "skipped",
                    "reason": "disabled",
                    "cadence_minutes": entry.get("cadence_minutes"),
                    "runs": 0,
                    "compute_estimates": 0.0,
                    "export_estimates": 0.0,
                }
            )
            continue
        cadence = _finite_nonnegative_number(
            entry.get("cadence_minutes"),
            f"scheduled entry {key} cadence_minutes",
            zero_allowed=False,
        )
        cadence_seconds = cadence * 60.0
        if remaining_seconds <= 0:
            runs = 0
        else:
            exact, remainder = divmod(remaining_seconds, cadence_seconds)
            # Conservative upper bound: a partial trailing interval can still
            # hold one scan-triggered run before renewal.
            runs = int(exact) + (1 if remainder > 0 else 0)
        query_id = entry.get("query_id")
        if query_id is None:
            raise BudgetInputError(f"scheduled entry {key} has no query_id")
        if "planned_export_bytes" in entry:
            # A configured byte cap is NOT a precharged export quote; export
            # cost must be sample-backed. Reject stale callers loudly.
            raise BudgetInputError(
                f"scheduled entry {key} carries planned_export_bytes; "
                "planned byte caps are not a precharged export quote — "
                "export costs are sample-backed only"
            )
        export_policy = entry.get("export_policy", "observed_max")
        if export_policy not in _EXPORT_POLICIES:
            raise BudgetInputError(
                f"scheduled entry {key} export_policy must be one of "
                f"{_EXPORT_POLICIES}, got {export_policy!r}"
            )
        parameters = entry.get("query_parameters")
        if parameters is None:
            parameters = entry.get("parameters")
        performance = entry.get("performance")
        if performance is None and default_performance is not None:
            performance = default_performance
        shape_version = entry.get("shape_version")
        estimate = sample_estimate(
            grouped_samples,
            query_id,
            parameters,
            now_utc=now,
            performance=performance,
            shape_version=shape_version,
            max_age_hours=sample_max_age_hours,
            min_comparable_samples=min_comparable_samples,
            variance_uplift_sigma=variance_uplift_sigma,
            export_rate_per_mb=rate,
        )

        if estimate["estimate"] is None:
            if runs > 0:
                unknown_keys.append(
                    {
                        "key": key,
                        "query_id": query_id,
                        "runs": runs,
                        "component": "compute",
                        "reason": (
                            "no comparable recent cost samples for exactly "
                            "this query+parameters+performance(+shape); the "
                            "post-run cap is not a pre-run estimate"
                        ),
                        "sample_reasons": estimate["reasons"],
                    }
                )
                detail.append(
                    {"key": key, "status": "unknown", "cadence_minutes": cadence,
                     "runs": runs, "sample_count": estimate["evidence"]["comparable_count"],
                     "compute_estimates": 0.0, "export_estimates": 0.0}
                )
            else:
                detail.append(
                    {"key": key, "status": "not_due", "cadence_minutes": cadence,
                     "runs": runs, "sample_count": estimate["evidence"]["comparable_count"],
                     "compute_estimates": 0.0, "export_estimates": 0.0}
                )
            continue

        # Export component ----------------------------------------------
        if export_policy == "none":
            export_cost_per_run = 0.0
            export_bytes_max = 0.0
            export_unknown = False
        else:
            export_values = estimate["evidence"]["export_cost_values"]
            export_rows = estimate["evidence"]["export_evidence_count"]
            export_bytes_max = estimate["evidence"]["export_bytes_max"]
            export_unknown = False
            if runs == 0:
                # Nothing is due: no exposure is added, so no quote needed.
                export_cost_per_run = 0.0
            elif (
                not export_values
                or export_rows < min_comparable_samples
            ):
                export_cost_per_run = 0.0
                export_unknown = True
            else:
                export_cost_per_run = _uplifted_envelope(
                    export_values, variance_uplift_sigma
                )

        compute_cost = runs * estimate["estimate"]
        export_cost = runs * export_cost_per_run
        total += compute_cost + export_cost
        row = {
            "key": key,
            "status": "scheduled",
            "cadence_minutes": cadence,
            "runs": runs,
            "sample_count": estimate["evidence"]["comparable_count"],
            "unit_cost_credits": estimate["estimate"],
            "estimate_basis": (
                "max(observed_max, mean+sigma*stdev) upper-envelope estimate, "
                "not a guarantee"
            ),
            "compute_estimates": compute_cost,
            "export_policy": export_policy,
            "export_bytes_max": export_bytes_max,
            "export_cost_per_run": export_cost_per_run,
            "export_estimates": export_cost,
            "cost_estimates": compute_cost + export_cost,
        }
        if export_unknown:
            unknown_keys.append(
                {
                    "key": key,
                    "query_id": query_id,
                    "runs": runs,
                    "component": "export",
                    "reason": (
                        "entry may export results but comparable recent "
                        "export evidence is below the comparable-sample "
                        f"quorum ({export_rows}/{min_comparable_samples}); "
                        "unknown nonzero export cost is blocked"
                    ),
                    "sample_reasons": estimate["reasons"],
                }
            )
            row["status"] = "export_unknown"
        detail.append(row)
    return {
        "total": total,
        "detail": detail,
        "unknown_keys": unknown_keys,
        "remaining_seconds_before_renewal": remaining_seconds,
        "period_end_utc": _z(end),
    }


# ---- admission --------------------------------------------------------------

def _estimator_policy(kwargs, rate):
    """Fold the shared estimator policy from budget_admission kwargs."""
    quorum = kwargs.get("min_comparable_samples", DEFAULT_MIN_COMPARABLE_SAMPLES)
    if type(quorum) is not int or quorum < 1:
        raise BudgetInputError("min_comparable_samples must be a positive integer")
    return {
        "max_age_hours": _finite_nonnegative_number(
            kwargs.get("sample_max_age_hours", DEFAULT_SAMPLE_MAX_AGE_HOURS),
            "sample_max_age_hours",
        ),
        "min_comparable_samples": quorum,
        "variance_uplift_sigma": _finite_nonnegative_number(
            kwargs.get("variance_uplift_sigma", DEFAULT_VARIANCE_UPLIFT_SIGMA),
            "variance_uplift_sigma",
        ),
        "export_rate_per_mb": rate,
    }


def budget_admission(
    usage,
    now_utc,
    *,
    samples=None,
    scheduled=None,
    outstanding=None,
    guard_credits=DEFAULT_GUARD_CREDITS,
    minimum_reserve_credits=DEFAULT_MINIMUM_RESERVE_CREDITS,
    monthly_target_credits=None,
    minimum_headroom_below_target=None,
    export_credits_per_megabyte=DEFAULT_EXPORT_CREDITS_PER_MB,
    default_sample_performance=None,
    sample_max_age_hours=DEFAULT_SAMPLE_MAX_AGE_HOURS,
    min_comparable_samples=DEFAULT_MIN_COMPARABLE_SAMPLES,
    variance_uplift_sigma=DEFAULT_VARIANCE_UPLIFT_SIGMA,
):
    """The single fail-closed admission decision; returns a detailed report,
    never raises for well-formed input, and blocks instead of inventing
    unknown cost.

    ``usage`` accepts either a Dune ``usage`` payload with ``billing_periods``
    (active period selected internally) or a normalized period dict with
    ``credits_used``, ``credits_included``, ``period_start_utc``,
    ``period_end_utc`` and optionally ``exact_end``/``inexact_source_end_date``.

    Enforced ceilings (both bind):
      - ``limit = min(credits_included, guard_credits, monthly_target_credits)``
        — the target is enforced, not just reported, while the emergency guard
        (currently 1800) stays in force;
      - when ``minimum_headroom_below_target`` is set, an extra always-free
        buffer below the target must survive: ``used + reserve + dependencies
        + buffer <= target``.

    Any unknown component (no/insufficient/stale/shape-mismatched samples,
    undocumented export cost, liability without a terminal cost) blocks the
    decision; dependencies are only allowed when their forecast fits under
    the remaining headroom."""

    now = parse_instant(now_utc, "now_utc")
    guard = _finite_nonnegative_number(guard_credits, "guard_credits")
    reserve = _finite_nonnegative_number(
        minimum_reserve_credits, "minimum_reserve_credits"
    )
    scale = _finite_nonnegative_number(
        export_credits_per_megabyte, "export_credits_per_megabyte"
    )
    target = (
        None
        if monthly_target_credits is None
        else _finite_nonnegative_number(monthly_target_credits, "monthly_target_credits")
    )
    target_buffer = (
        None
        if minimum_headroom_below_target is None
        else _finite_nonnegative_number(
            minimum_headroom_below_target, "minimum_headroom_below_target"
        )
    )
    policy = _estimator_policy(locals(), scale)

    reasons = []
    blockers = []
    inexact_marker = None

    selection = normalize_usage(usage, now)
    if selection["period"] is None:
        reasons.extend(selection["reasons"])
        period = selection["period"]
        if period is None:
            blockers.append("no_active_billing_period")
            return {
                "status": "blocked",
                "blockers": blockers,
                "reasons": reasons,
                "policy": {
                    "guard_credits": guard,
                    "minimum_reserve_credits": reserve,
                    "monthly_target_credits": target,
                    "minimum_headroom_below_target": target_buffer,
                    "export_credits_per_megabyte": scale,
                    "min_comparable_samples": policy["min_comparable_samples"],
                    "sample_max_age_hours": policy["max_age_hours"],
                    "variance_uplift_sigma": policy["variance_uplift_sigma"],
                },
                "current_usage": {
                    "credits_used": None,
                    "credits_included": None,
                    "period_start_utc": None,
                    "period_end_utc": None,
                    "exact_end": None,
                },
                "now_utc": _z(now),
                "outstanding_liabilities": None,
                "forecast": None,
                "totals": None,
            }
    period = selection["period"]
    reasons.extend(selection["reasons"])
    blockers.extend(selection["blockers"])
    credits_used = period["credits_used"]
    credits_included = period["credits_included"]
    period_start = period["period_start_utc"]
    exact_end = period["period_end_utc"]
    inexact_marker = period.get("inexact_source_end_date")

    ceilings = [
        ("included credits", credits_included),
        ("guard", guard),
    ]
    if target is not None:
        ceilings.append(("monthly target", target))
    limit = min(value for _, value in ceilings)
    bound_names = [
        f"{name} {value:.6f}" for name, value in ceilings if value == limit
    ]

    usage_plus_reserve = credits_used + reserve
    headroom_after_reserve = limit - usage_plus_reserve
    if usage_plus_reserve >= limit:
        blockers.append(
            "monthly_spend_guard_reached: confirmed usage plus reserve has "
            f"reached the enforced limit {limit:.6f} "
            f"(binding: min({', '.join(bound_names)}))"
        )
    period_ended = exact_end is not None and now >= exact_end
    if period_ended:
        blockers.append("billing_period_ended")

    liabilities = outstanding_liabilities(
        outstanding or [], export_credits_per_megabyte=scale
    )
    forecast = None
    dependencies = None
    if exact_end is not None:
        grouped = group_samples(samples or [], default_sample_performance)
        forecast = scheduled_forecast(
            scheduled or [],
            grouped,
            now_utc=now,
            period_end_utc=exact_end,
            default_performance=default_sample_performance,
            min_comparable_samples=policy["min_comparable_samples"],
            sample_max_age_hours=policy["max_age_hours"],
            variance_uplift_sigma=policy["variance_uplift_sigma"],
            export_credits_per_megabyte=scale,
        )
        for item in liabilities["unknown"]:
            blockers.append(
                f"unknown_outstanding_liability:{item['liability_id']}: "
                f"{item['reason']}; unknown risk is blocked, not capped"
            )
        for unknown in forecast["unknown_keys"]:
            blockers.append(
                f"unknown_scheduled_cost:{unknown['key']} "
                f"(query {unknown['query_id']}, component "
                f"{unknown.get('component', 'compute')}): "
                + (f"{unknown.get('reason')}; " if unknown.get("reason") else "")
                + "no defensible sample-backed estimate; refused instead of "
                "inventing a cap-based bound"
            )
        dependencies = liabilities["total"] + forecast["total"]
        if dependencies > headroom_after_reserve + 1e-9:
            blockers.append(
                f"forecast_exceeds_guard_headroom: dependencies "
                f"{dependencies:.6f} exceed available headroom "
                f"{headroom_after_reserve:.6f}"
            )
        if target is not None:
            buffer = target_buffer if target_buffer is not None else 0.0
            projected_below_target = (
                usage_plus_reserve + dependencies + buffer
            )
            if projected_below_target > target + 1e-9:
                blockers.append(
                    "minimum_target_headroom_exceeded: usage "
                    f"{credits_used:.6f} + reserve {reserve:.6f} + "
                    f"dependencies {dependencies:.6f} + buffer {buffer:.6f} "
                    f"exceeds the enforced target {target:.6f}"
                )

    status = "blocked" if blockers else "admitted"
    return {
        "status": status,
        "blockers": blockers,
        "reasons": reasons + blockers,
        "policy": {
            "guard_credits": guard,
            "minimum_reserve_credits": reserve,
            "monthly_target_credits": target,
            "minimum_headroom_below_target": target_buffer,
            "export_credits_per_megabyte": scale,
            "min_comparable_samples": policy["min_comparable_samples"],
            "sample_max_age_hours": policy["max_age_hours"],
            "variance_uplift_sigma": policy["variance_uplift_sigma"],
            "estimate_basis": (
                "upper-envelope estimate max(observed_max, "
                "mean+sigma*stdev) over recent comparable samples; "
                "explicit estimate, NOT a guarantee"
            ),
        },
        "current_usage": {
            "credits_used": credits_used,
            "credits_included": credits_included,
            "period_start_utc": _z(period_start) if period_start else None,
            "period_end_utc": _z(exact_end) if exact_end else inexact_marker,
            "exact_end": exact_end is not None,
        },
        "now_utc": _z(now),
        "outstanding_liabilities": liabilities,
        "forecast": forecast,
        "totals": {
            "credits_used": credits_used,
            "credits_included": credits_included,
            "guard_credits": guard,
            "monthly_target_credits": target,
            "minimum_headroom_below_target": target_buffer,
            "limit_credits": limit,
            "limit_binding": bound_names,
            "reserve_credits": reserve,
            "usage_plus_reserve": usage_plus_reserve,
            "headroom_after_reserve": headroom_after_reserve,
            "outstanding_liability_credits": liabilities["total"],
            "outstanding_known_floor_credits": liabilities["known_floor_credits"],
            "outstanding_exposure_is_fully_known": liabilities["exposure_is_fully_known"],
            "scheduled_forecast_credits": None
            if forecast is None
            else forecast["total"],
            "dependencies_total": dependencies,
        },
    }


def execution_admission(
    spec,
    samples,
    *,
    now_utc,
    default_sample_performance=None,
    sample_max_age_hours=DEFAULT_SAMPLE_MAX_AGE_HOURS,
    min_comparable_samples=DEFAULT_MIN_COMPARABLE_SAMPLES,
    variance_uplift_sigma=DEFAULT_VARIANCE_UPLIFT_SIGMA,
    export_credits_per_megabyte=DEFAULT_EXPORT_CREDITS_PER_MB,
):
    """Tracker spec-op companion: may THIS query be admitted for execution?

    Estimates the cost only from recent comparable samples for exactly
    (query_id, query_parameters, performance, shape_version), using the
    upper-envelope estimator. The spec's ``max_run_credits`` is post-run
    enforcement and is deliberately never used as a pre-run estimate.
    Returns {"status": "sampled"|"unknown", "cost_estimate_credits",
    "sample_count", "export_cost_max", "reasons", "spec": {...}}; unknown cost
    means the caller must block."""
    if not isinstance(spec, dict):
        raise BudgetInputError("spec must be a dict")
    if spec.get("query_id") is None:
        raise BudgetInputError("spec has no query_id")
    parameters = spec.get("query_parameters")
    if parameters is None:
        parameters = spec.get("parameters")
    spec_shape = spec.get("shape_version")
    grouped = group_samples(samples or [], default_sample_performance)
    estimate = sample_estimate(
        grouped,
        spec["query_id"],
        parameters,
        now_utc=now_utc,
        performance=spec.get("performance", default_sample_performance),
        shape_version=spec_shape,
        max_age_hours=sample_max_age_hours,
        min_comparable_samples=min_comparable_samples,
        variance_uplift_sigma=variance_uplift_sigma,
        export_rate_per_mb=export_credits_per_megabyte,
    )
    if estimate["status"] != "sampled":
        return {
            "status": "unknown",
            "cost_estimate_credits": None,
            "sample_count": estimate["evidence"]["comparable_count"],
            "export_cost_max": None,
            "reasons": estimate["reasons"] or [
                "no sample for exactly this query+parameters+performance; "
                "cost is only knowable after execution, so admission is "
                "blocked rather than capped"
            ],
        }
    return {
        "status": "sampled",
        "cost_estimate_credits": estimate["estimate"],
        "sample_count": estimate["evidence"]["comparable_count"],
        "export_cost_max": estimate["evidence"]["export_cost_max"],
        "estimate_is_hard_cap": False,
        "estimate_quality": estimate["estimate_quality"],
        "reasons": [],
    }
