#!/usr/bin/env python3
"""Pure, offline credit-cost estimates for autonomous XNET standing operations.

Dict-in/dict-out only (no network, no GitHub, no Dune, no RPC, no file I/O,
no mutation). This module does NOT decide admission and is NOT spending
authority: it returns defensible quotes so a human can finance continuous
autonomous operation without touching it daily. ``v3_credit_budget.py`` stays
the fail-closed admission gate; its 3-fresh-samples-per-exact-identity
envelope and a global forecast that permanently blocks on arbitrary BBB
lookbacks and outliers cannot quote standing autonomous work, so this module
quotes with these documented rules instead:

- samples come only from state already held locally: ``execution_history``,
  the durable financial journal's terminal known-cost rows,
  ``approved_recovery_batches`` (the 13 recent approved batch operations,
  which carry ``submitted_at``/``result_received`` instants) and the explicit
  vetted import lane ``state.autonomous_cost_samples``. Rows dedupe by
  execution id; conflicting duplicate costs resolve to the MAXIMUM,
  never silently hidden;
- identity is (query_id, engine/performance, shape_version, canonical
  parameters). Exact parameter matches are preferred and samples are never
  mixed across queries, engines or shapes, so the daily absolute query
  8894038 and the hot query 8894037 can never contaminate each other;
- with >= 2 usable samples a per-run quote is
  ``max(observed_max, mean + 3*stdev)`` (the repo's empirical uplift
  envelope); outliers are NEVER dropped — they stay inside the mean. ONE
  usable sample may quote only with the explicit ``single_sample_margin``
  (default x2). The separately reported ``expected_credits`` is the plain
  outlier-inclusive mean, and the raw observed maximum rides in the evidence;
- identity samples within ``max_sample_age_hours`` (default 30 days) are
  quotable under an explicit linear aging margin that ramps from no margin
  at ``aging_free_hours`` (168 h) to ``aging_margin_fraction`` at the
  30-day ceiling; older evidence is stale and unusable;
- when no usable exact-parameter sample exists, a semantically justified
  lookback-hours transfer may pool samples of the SAME query + engine +
  shape whose remaining parameters canonicalize identically, whose
  ``lookback_hours`` stays inside the query's transfer domain (default
  1..48 h; the BBB query 8894132 has the documented 1..168 h v12 partition
  domain), each scaled by ``max(1, requested_hours / sample_hours)``.
  Extending a hot query's domain beyond 48 h is possible ONLY via an
  explicit ``lookback_transfer.query_max_hours`` override (which merges
  over the documented per-query defaults);
- absolute-day parameters have no lookback-hours axis. The single-UTC-day
  family (``oldest_days_ago == newest_days_ago``, integer day offset
  0..60 — the daily repair query 8894038) translates across day offsets
  when no exact sample exists: SAME query + engine + shape samples of
  OTHER single-day offsets pool with NO extra scaling (per-day variance
  is carried by their costs), method ``calendar_translation_one_day``,
  with the original offsets and execution ids retained in the evidence.
  Multi-day (unequal-offset) requests and other period shapes are never
  pooled; query/engine/shape identity is never mixed;
- timestamps must exist and be exact UTC instants (date-only, naive or
  future instants are malformed); costs must be finite nonnegative numbers
  (booleans refuse). Malformed evidence raises ``EstimatorInputError``;
  well-formed but unquotable states return ``{"status": "unknown", ...}``
  with reasons — never an invented bound;
- ``policy`` carries knobs ONLY (ages, margins, sigma, transfer domains,
  export rate/overhead/fallback). It cannot inject costs, byte caps or
  spending limits: unknown or cost-shaped policy keys are refused. The
  exact accepted keys, types and defaults are exported as
  ``POLICY_SCHEMA`` (a minimal runtime config is
  ``{"default_engine": "medium", "transport_format_bound_fallback": True}``);
  engine-less history rows and engine-less specs attribute to
  ``default_engine`` so unattributed legacy/verified rows land on the
  pipeline tier, and sample parameters are ever only what each sample row
  itself records;
- exports are quoted from the ACTUAL completed result: the declared
  ``result_bytes`` (Dune result metadata size is a declared metadata
  estimate, not metered wire) inflated by same-query wire/declared ratio
  samples (metered ``export_wire_bytes`` or vetted fixture serialized
  bytes) plus a 25% margin and a fixed 16 KiB JSON overhead. A nonempty
  dataset requires a positive declared size. Without ratio samples the
  export quote is unknown by default; policy may explicitly turn on the
  documented conservative format bound (4x declared + 16 KiB), which is
  then labelled as an explicit format bound. Every export quote is
  transport-ESTIMATED, never a metered or settled charge. Zero-row results
  legitimately quote 0;
- ``forecast_remaining_calendar`` multiplies the outlier-inclusive expected
  mean by conservative cadence run counts (ceil over exact remaining
  seconds) and returns the observed-maximum risk SEPARATELY, never folding
  it silently into the mean.

Every returned quote ships its evidence (sample execution ids, canonical
parameters, method, sample ages) and is explicitly ``not a hard cap`` and
``not spending authority``; actual charges only exist after executions and
reconcile against the official usage endpoint.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics

from v3_credit_budget import (
    BudgetInputError,
    DEFAULT_EXPORT_CREDITS_PER_MB,
    canon_parameters,
    parse_instant,
)

__all__ = [
    "EstimatorInputError",
    "estimate_operation",
    "forecast_remaining_calendar",
    "POLICY_SCHEMA",
    "DEFAULT_MAX_SAMPLE_AGE_HOURS",
    "DEFAULT_AGING_FREE_HOURS",
    "DEFAULT_AGING_MARGIN_FRACTION",
    "DEFAULT_SINGLE_SAMPLE_MARGIN",
    "DEFAULT_VARIANCE_UPLIFT_SIGMA",
    "DEFAULT_LOOKBACK_TRANSFER_DOMAIN_HOURS",
    "DEFAULT_QUERY_LOOKBACK_DOMAIN_HOURS",
    "DEFAULT_EXPORT_CREDITS_PER_MB",
]

# ---- documented defaults ------------------------------------------------------

MAX_SAMPLE_AGE_HOURS = 720.0  # 30 days: sparse-evidence ceiling
AGING_FREE_HOURS = 168.0  # matches the repo's fresh comparable window
AGING_MARGIN_FRACTION = 0.5  # quote uplift at the 30-day ceiling
SINGLE_SAMPLE_MARGIN = 2.0  # explicit margin for a single usable sample
VARIANCE_UPLIFT_SIGMA = 3.0  # same sigma as v3_credit_budget's envelope
LOOKBACK_TRANSFER_DOMAIN_HOURS = 48.0  # standing-ops lookback domain 1..48 h
# BBB query 8894132 v12: the partition lower bound follows lookback_hours and
# the approved catch-up ran it at 120 h (APPROVED_CATCHUP_2026-10-20.md), so
# its documented transfer domain is 1..168 h.
QUERY_LOOKBACK_DOMAIN_HOURS = {"8894132": 168.0}

# Daily absolute-repair family (saved query 8894038, SQL v4): parameters
# {"oldest_days_ago": d, "newest_days_ago": d} with an integer d in 0..60
# denote exactly ONE UTC day. The approved catch-up captured the whole
# family (offsets 0..4 on 2026-10-20, costs 1.435..0.158), so same-query +
# engine + shape samples of OTHER single-day offsets may translate across
# the calendar with NO extra scaling — per-day cost variance is carried by
# the pooled sample costs themselves.
DAY_TRANSFER_METHOD = "calendar_translation_one_day"
DAY_FAMILY_MIN_OFFSET_DAYS = 0
DAY_FAMILY_MAX_OFFSET_DAYS = 60
DAY_FAMILY_DEFAULTS = {
    "enabled": True,
    "min_offset_days": DAY_FAMILY_MIN_OFFSET_DAYS,
    "max_offset_days": DAY_FAMILY_MAX_OFFSET_DAYS,
}
TRANSPORT_OVERHEAD_FRACTION = 0.25  # 25% on top of the inflated transport bytes
TRANSPORT_FIXED_OVERHEAD_BYTES = 16 * 1024  # fixed JSON container overhead
TRANSPORT_FORMAT_BOUND_MULTIPLIER = 4.0  # documented conservative 4x bound
TRANSPORT_BASIS_NOTE = "transport_estimated_not_metered"

TERMINAL_EXECUTION_STATES = frozenset({
    "QUERY_STATE_COMPLETED", "QUERY_STATE_COMPLETED_PARTIAL",
    "QUERY_STATE_FAILED", "QUERY_STATE_CANCELED", "QUERY_STATE_CANCELLED",
    "QUERY_STATE_EXPIRED",
})

KINDS = ("execution", "export")

# Exact documented policy surface: names -> type, default, semantics. A
# runtime config needs nothing (all defaults are conservative); the minimal
# production config is {"default_engine": "medium",
# "transport_format_bound_fallback": True}.
POLICY_SCHEMA = {
    "default_engine": {
        "type": "string | int | None",
        "default": None,
        "notes": "engine-less history rows and engine-less specs attribute "
                 "to this tier ('medium' is the verified legacy pipeline "
                 "tier); bools and other types refuse",
    },
    "transport_format_bound_fallback": {
        "type": "bool",
        "default": False,
        "notes": "True accepts the documented conservative 4x declared + "
                 "16 KiB format bound when no wire/declared ratio sample "
                 "exists (labelled transport_estimated_not_metered)",
    },
    "max_sample_age_hours": {
        "type": "nonnegative number",
        "default": 720.0,
        "notes": "30-day sparse-evidence evidence ceiling",
    },
    "aging_free_hours": {
        "type": "nonnegative number < max_sample_age_hours",
        "default": 168.0,
    },
    "aging_margin_fraction": {
        "type": "nonnegative number",
        "default": 0.5,
        "notes": "quote uplift at the evidence-ceiling age",
    },
    "single_sample_margin": {
        "type": "number >= 1.0",
        "default": 2.0,
    },
    "variance_uplift_sigma": {
        "type": "nonnegative number",
        "default": 3.0,
    },
    "lookback_transfer": {
        "type": "object",
        "default": {"enabled": True, "default_max_hours": 48.0,
                    "query_max_hours": {"8894132": 168.0}},
        "keys": {
            "enabled": "bool",
            "default_max_hours": "number > 0",
            "query_max_hours": "object {query_id: number > 0}; per-query "
                               "entries MERGE over the documented defaults "
                               "(an explicit entry wins for that query id). "
                               "The hot query 8894037 stays 1..48 h unless "
                               "explicitly overridden, e.g. "
                               "{'query_max_hours': {'8894037': 168.0}} - "
                               "beyond 48 h without the override the quote "
                               "is unknown and the runtime falls back to "
                               "the single-day repair family",
        },
    },
    "day_family_transfer": {
        "type": "object",
        "default": DAY_FAMILY_DEFAULTS,
        "keys": {
            "enabled": "bool",
            "min_offset_days": "nonnegative integer",
            "max_offset_days": "integer >= min_offset_days",
        },
        "notes": "single-UTC-day translation family (oldest_days_ago == "
                 "newest_days_ago, integer offset range; the daily repair "
                 "query 8894038)",
    },
    "export_rate_per_mb": {
        "type": "nonnegative number",
        "default": 10.0,
    },
    "overhead_transport_fraction": {
        "type": "nonnegative number",
        "default": 0.25,
    },
    "fixed_transport_overhead_bytes": {
        "type": "nonnegative integer",
        "default": 16384,
    },
    "format_bound_multiplier": {
        "type": "number >= 1.0",
        "default": 4.0,
    },
}


class EstimatorInputError(BudgetInputError):
    """Malformed estimator input; quoting must stop until the caller fixes it."""


# ---- small validation helpers --------------------------------------------------

def _nonnegative_number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EstimatorInputError(
            f"{label} must be a finite nonnegative number, got {value!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise EstimatorInputError(
            f"{label} must be finite (no NaN/Infinity), got {value!r}"
        )
    if number < 0 or (positive and number == 0):
        raise EstimatorInputError(
            f"{label} must be {'positive' if number == 0 else 'nonnegative'}, "
            f"got {value!r}"
        )
    return number


def _integral(value, label):
    number = _nonnegative_number(value, label)
    if number != int(number):
        raise EstimatorInputError(
            f"{label} must be an integral count, got {value!r}"
        )
    return int(number)


def _finite_bool(value, label):
    if value is None:
        return False
    if not isinstance(value, bool):
        raise EstimatorInputError(f"{label} must be a boolean, got {value!r}")
    return value


def _z(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _stringish_tag(value, label):
    """Accept str/int (and None) id-like tags; refuse bools and other types."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise EstimatorInputError(
            f"{label} must be a string or int (or None), got {value!r}"
        )
    return str(value)


def _instant(value, label):
    """Parse an exact UTC instant, surfacing malformed input as the
    estimator's own error type (naive, date-only and unparseable values raise)."""
    try:
        return parse_instant(value, label)
    except BudgetInputError as error:
        raise EstimatorInputError(str(error)) from None


# ---- policy ----------------------------------------------------------------------

_POLICY_KEYS = frozenset({
    "max_sample_age_hours",
    "aging_free_hours",
    "aging_margin_fraction",
    "single_sample_margin",
    "variance_uplift_sigma",
    "lookback_transfer",
    "day_family_transfer",
    "export_rate_per_mb",
    "transport_format_bound_fallback",
    "overhead_transport_fraction",
    "fixed_transport_overhead_bytes",
    "format_bound_multiplier",
    "default_engine",
})
_LOOKBACK_POLICY_KEYS = frozenset(
    {"enabled", "default_max_hours", "query_max_hours"}
)
_DAY_FAMILY_POLICY_KEYS = frozenset(
    {"enabled", "min_offset_days", "max_offset_days"}
)


def _policy_config(policy):
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise EstimatorInputError(
            f"policy must be an object or None, got {type(policy).__name__}"
        )
    unknown = sorted(set(policy) - _POLICY_KEYS)
    if unknown:
        # The policy may carry knobs only. Cost- or cap-shaped extras (never
        # part of the allowlist) cannot masquerade as evidence.
        raise EstimatorInputError(
            f"policy has unsupported keys {unknown}; only documented knobs "
            f"{sorted(_POLICY_KEYS)} are accepted"
        )

    max_age = _nonnegative_number(
        policy.get("max_sample_age_hours", MAX_SAMPLE_AGE_HOURS),
        "policy max_sample_age_hours",
    )
    aging_free = _nonnegative_number(
        policy.get("aging_free_hours", AGING_FREE_HOURS),
        "policy aging_free_hours",
    )
    if aging_free >= max_age:
        raise EstimatorInputError(
            "policy aging_free_hours must be below max_sample_age_hours"
        )
    margin = _nonnegative_number(
        policy.get("aging_margin_fraction", AGING_MARGIN_FRACTION),
        "policy aging_margin_fraction",
    )
    single = _nonnegative_number(
        policy.get("single_sample_margin", SINGLE_SAMPLE_MARGIN),
        "policy single_sample_margin",
    )
    if single < 1.0:
        raise EstimatorInputError(
            "policy single_sample_margin must be at least 1.0 (a single "
            "usable sample may never quote below its observed cost)"
        )
    sigma = _nonnegative_number(
        policy.get("variance_uplift_sigma", VARIANCE_UPLIFT_SIGMA),
        "policy variance_uplift_sigma",
    )
    rate = _nonnegative_number(
        policy.get("export_rate_per_mb", DEFAULT_EXPORT_CREDITS_PER_MB),
        "policy export_rate_per_mb",
    )
    overhead = _nonnegative_number(
        policy.get("overhead_transport_fraction", TRANSPORT_OVERHEAD_FRACTION),
        "policy overhead_transport_fraction",
    )
    fixed_bytes = _integral(
        policy.get(
            "fixed_transport_overhead_bytes", TRANSPORT_FIXED_OVERHEAD_BYTES
        ),
        "policy fixed_transport_overhead_bytes",
    )
    bound = _nonnegative_number(
        policy.get(
            "format_bound_multiplier", TRANSPORT_FORMAT_BOUND_MULTIPLIER
        ),
        "policy format_bound_multiplier",
    )
    if bound < 1.0:
        raise EstimatorInputError(
            "policy format_bound_multiplier must be at least 1.0"
        )
    default_engine = policy.get("default_engine")
    _stringish_tag(default_engine, "policy default_engine")
    fallback = _finite_bool(
        policy.get("transport_format_bound_fallback"),
        "policy transport_format_bound_fallback",
    )

    lookback_raw = policy.get("lookback_transfer", {})
    if not isinstance(lookback_raw, dict):
        raise EstimatorInputError(
            "policy lookback_transfer must be an object"
        )
    unknown = sorted(set(lookback_raw) - _LOOKBACK_POLICY_KEYS)
    if unknown:
        raise EstimatorInputError(
            f"policy lookback_transfer has unsupported keys {unknown}"
        )
    enabled = _finite_bool(
        lookback_raw.get("enabled", True),
        "policy lookback_transfer enabled",
    )
    domain_default = _nonnegative_number(
        lookback_raw.get("default_max_hours", LOOKBACK_TRANSFER_DOMAIN_HOURS),
        "policy lookback_transfer default_max_hours",
        positive=True,
    )
    query_domains_raw = lookback_raw.get("query_max_hours")
    # Per-query overrides MERGE over the documented defaults: an explicit
    # entry wins for its query id; unmentioned queries keep their documented
    # domain (e.g. BBB 8894132 stays 168 h unless explicitly shrunk).
    query_domains = dict(QUERY_LOOKBACK_DOMAIN_HOURS)
    if query_domains_raw is not None:
        if not isinstance(query_domains_raw, dict):
            raise EstimatorInputError(
                "policy lookback_transfer query_max_hours must be an object"
            )
        for key, value in query_domains_raw.items():
            if not isinstance(key, (str, int)) or isinstance(key, bool):
                raise EstimatorInputError(
                    f"policy lookback_transfer query_max_hours key {key!r} "
                    "must be a string or int query id"
                )
            query_domains[str(key)] = _nonnegative_number(
                value,
                f"policy lookback_transfer query_max_hours[{key!r}]",
                positive=True,
            )
    day_raw = policy.get("day_family_transfer", {})
    if not isinstance(day_raw, dict):
        raise EstimatorInputError(
            "policy day_family_transfer must be an object"
        )
    day_unknown = sorted(set(day_raw) - _DAY_FAMILY_POLICY_KEYS)
    if day_unknown:
        raise EstimatorInputError(
            f"policy day_family_transfer has unsupported keys {day_unknown}"
        )
    day_enabled = _finite_bool(
        day_raw.get("enabled", True),
        "policy day_family_transfer enabled",
    )
    day_min = _integral(
        day_raw.get("min_offset_days", DAY_FAMILY_MIN_OFFSET_DAYS),
        "policy day_family_transfer min_offset_days",
    )
    day_max = _integral(
        day_raw.get("max_offset_days", DAY_FAMILY_MAX_OFFSET_DAYS),
        "policy day_family_transfer max_offset_days",
    )
    if day_max < day_min:
        raise EstimatorInputError(
            "policy day_family_transfer max_offset_days must be at least "
            "min_offset_days"
        )
    return {
        "max_age_hours": max_age,
        "aging_free_hours": aging_free,
        "aging_margin_fraction": margin,
        "single_sample_margin": single,
        "variance_uplift_sigma": sigma,
        "export_rate_per_mb": rate,
        "transport_format_bound_fallback": fallback,
        "overhead_transport_fraction": overhead,
        "fixed_transport_overhead_bytes": fixed_bytes,
        "format_bound_multiplier": bound,
        "default_engine": default_engine,
        "lookback_transfer": {
            "enabled": enabled,
            "default_max_hours": domain_default,
            "query_max_hours": query_domains,
        },
        "day_family_transfer": {
            "enabled": day_enabled,
            "min_offset_days": day_min,
            "max_offset_days": day_max,
        },
    }


def _query_domain_hours(cfg, query_tag):
    transfer = cfg["lookback_transfer"]
    return transfer["query_max_hours"].get(
        query_tag, transfer["default_max_hours"]
    )


# ---- timestamps and lookbacks ---------------------------------------------------

_STAMP_FIELDS = (
    # Terminal (completion) instants first, then start, then submission.
    "result_received_at_utc", "result_received_utc", "result_received_at",
    "result_received", "execution_ended_at", "completed_at_utc",
    "incurred_at_utc", "execution_started_at", "submitted_at_utc",
    "submitted_at",
)

_COST_FIELDS = (
    "execution_cost_credits", "cost_credits", "compute_cost_credits",
    "observed_cost_credits", "cost",
)
_DECLARED_BYTES_FIELDS = (
    "result_bytes", "declared_result_bytes", "total_result_set_bytes",
    "result_set_bytes",
)
_WIRE_FIELDS = (
    ("export_wire_bytes", "metered_export_wire"),
    ("wire_bytes", "metered_export_wire"),
    ("serialized_bytes", "vetted_serialized_bytes"),
)
_ROWS_FIELDS = ("total_row_count", "total_rows", "result_row_count")
_EXECUTION_ID_FIELDS = ("execution_id", "operation_id")


def _collect_single_field(row, fields, label, index):
    """Read one value from an allowlist of field aliases.

    Multiple present aliases must agree numerically; disagreement is
    contradictory evidence and refuses, never silently picks one.
    """
    present = [(field, row[field]) for field in fields if row.get(field) is not None]
    if not present:
        return None, None
    parsed = []
    for field, raw in present:
        parsed.append((field, _nonnegative_number(raw, f"{label}[{field}]")))
    first_field, first_value = parsed[0]
    for field, value in parsed[1:]:
        if abs(value - first_value) > 1e-12:
            raise EstimatorInputError(
                f"sample[{index}] has contradictory {label} evidence: "
                f"{first_field}={first_value!r} vs {field}={value!r}"
            )
    return present[0][1], present[0][0]


def _row_anchor(row, index, now):
    """Latest usable exact instant on the row (terminal beats start beats
    submission). Anything present but malformed (date-only, naive, not a
    string/datetime) raises; a future instant is contradictory evidence."""
    found = []
    for field in _STAMP_FIELDS:
        if row.get(field) is None:
            continue
        found.append((field, _instant(row[field], f"sample[{index}].{field}")))
    if not found:
        return None, None, None
    anchor_field, anchor = max(found, key=lambda item: item[1])
    if anchor > now:
        raise EstimatorInputError(
            f"sample[{index}].{anchor_field} is in the future "
            f"({_z(anchor)} > {_z(now)}); timestamped evidence cannot "
            "postdate the evaluation instant"
        )
    return anchor, anchor_field, _z(anchor)


def _lookback_hours(params, label):
    if params is None:
        return None
    value = params.get("lookback_hours")
    if value is None:
        return None
    number = _nonnegative_number(value, f"{label}.lookback_hours")
    if number < 1.0:
        raise EstimatorInputError(
            f"{label}.lookback_hours must be at least 1.0; zero or sub-hour "
            f"lookbacks are not semantically valid windows, got {value!r}"
        )
    return number


_DAY_KEYS = frozenset({"oldest_days_ago", "newest_days_ago"})


def _day_offset(params, label):
    """Integer day offset ``d`` when params denote exactly ONE UTC day as
    ``oldest_days_ago == newest_days_ago`` (the 8894038-style daily
    absolute-repair family); otherwise None: extra keys, a missing key or
    unequal offsets are not the single-day family. Malformed types — bool,
    non-number, nonfinite, fractional or negative — raise so bad evidence
    never silently pools."""
    if params is None:
        return None
    if not _DAY_KEYS.issuperset(params):
        return None
    oldest = params.get("oldest_days_ago")
    newest = params.get("newest_days_ago")
    if oldest is None or newest is None:
        return None
    offsets = []
    for name, value in (("oldest_days_ago", oldest),
                        ("newest_days_ago", newest)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EstimatorInputError(
                f"{label}.{name} must be an integer day offset, got {value!r}"
            )
        number = float(value)
        if (not math.isfinite(number) or number < 0
                or number != int(number)):
            raise EstimatorInputError(
                f"{label}.{name} must be a finite nonnegative integer day "
                f"offset, got {value!r}"
            )
        offsets.append(int(number))
    if offsets[0] != offsets[1]:
        return None
    return offsets[0]


# ---- sample collection ----------------------------------------------------------

def _normalized_row(raw, source, index, now, cfg):
    """Inline validation of one raw evidence row. Returns
    (normalized_sample_or_None, skip_reason): a structurally present row
    that carries no usable cost evidence skips with an explaining reason."""
    if not isinstance(raw, dict):
        raise EstimatorInputError(
            f"{source}[{index}] is not an object: {raw!r}"
        )
    execution_id = None
    for field in _EXECUTION_ID_FIELDS:
        if raw.get(field) is not None:
            execution_id = _stringish_tag(
                raw[field], f"{source}[{index}].{field}"
            )
            break

    query_id = raw.get("query_id")
    if query_id is None:
        raise EstimatorInputError(f"{source}[{index}] has no query_id")
    query_tag = _stringish_tag(query_id, f"{source}[{index}].query_id")

    state_label = raw.get("state")
    if state_label is not None:
        if not isinstance(state_label, str):
            raise EstimatorInputError(
                f"{source}[{index}].state must be a string or None, got "
                f"{state_label!r}"
            )
        if state_label not in TERMINAL_EXECUTION_STATES:
            return None, "nonterminal_state_sample"

    cost, cost_field = _collect_single_field(
        raw, _COST_FIELDS, f"{source}[{index}] cost", index
    )

    engine = raw.get("performance")
    if engine is None:
        engine = raw.get("engine")
    if engine is None:
        engine = cfg["default_engine"]
    engine = _stringish_tag(engine, f"{source}[{index}] engine")
    shape = raw.get("shape_version")
    if shape is None:
        shape = raw.get("shape")
    if shape is None:
        shape = raw.get("schema_version")
    shape = _stringish_tag(shape, f"{source}[{index}] shape_version")

    parameters = raw.get("query_parameters")
    if parameters is None:
        parameters = raw.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise EstimatorInputError(
            f"{source}[{index}] parameters must be an object or None, got "
            f"{type(parameters).__name__}"
        )
    try:
        params_canon = canon_parameters(parameters)
    except BudgetInputError as error:
        raise EstimatorInputError(
            f"{source}[{index}] parameters are not canonicalizable: {error}"
        ) from None
    lookback = _lookback_hours(parameters, f"{source}[{index}] parameters")
    day_offset = _day_offset(parameters, f"{source}[{index}] parameters")

    anchor, anchor_field, anchor_iso = _row_anchor(raw, index, now)

    declared, _ = _collect_single_field(
        raw, _DECLARED_BYTES_FIELDS,
        f"{source}[{index}] declared result bytes", index,
    )
    wire, wire_provenance = None, None
    for field, provenance in _WIRE_FIELDS:
        if raw.get(field) is not None:
            wire = _nonnegative_number(
                raw[field], f"{source}[{index}].{field}"
            )
            wire_provenance = provenance
            break
    total_rows = None
    for field in _ROWS_FIELDS:
        if raw.get(field) is not None:
            total_rows = _integral(raw[field], f"{source}[{index}].{field}")
            break

    sample = {
        "source": source,
        "execution_id": execution_id,
        "query_id": query_tag,
        "params_canon": params_canon,
        "lookback_hours": lookback,
        "day_offset": day_offset,
        "engine": engine,
        "shape": shape,
        "cost": cost,
        "cost_field": cost_field,
        "anchor_utc": anchor,
        "anchor_field": anchor_field,
        "anchor_iso": anchor_iso,
        "declared_result_bytes": declared,
        "wire_bytes": wire,
        "wire_provenance": wire_provenance,
        "total_rows": total_rows,
        "state": state_label,
    }
    return sample, None


def _fingerprint_row(row, occurrence):
    material = {
        "source": row["source"],
        "query_id": row["query_id"],
        "params_canon": row["params_canon"],
        "engine": row["engine"],
        "shape": row["shape"],
        "cost": row["cost"],
        "anchor_iso": row["anchor_iso"],
        "declared_result_bytes": row["declared_result_bytes"],
        "wire_bytes": row["wire_bytes"],
        "total_rows": row["total_rows"],
    }
    try:
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    except (TypeError, ValueError) as error:
        raise EstimatorInputError(
            f"sample evidence for {row['query_id']} is not fingerprintable: "
            f"{error}"
        ) from None
    return f"anon:{row['source']}:{digest}:{occurrence}"


def _merge_rows(a, b):
    """Merge duplicate evidence for one execution id. Conflicting costs
    resolve to the MAXIMUM (a recurring entry must not hide a higher-risk
    duplicate); Missing fields fill from the other row."""
    a_cost, b_cost = a["cost"], b["cost"]
    if a_cost is not None and b_cost is not None and abs(a_cost - b_cost) > 1e-12:
        base, other = (a, b) if a_cost > b_cost else (b, a)
    else:
        def score(row):
            return (
                1 if row["cost"] is not None else 0,
                1 if row["anchor_utc"] is not None else 0,
                1 if row["wire_bytes"] is not None else 0,
                1 if row["declared_result_bytes"] is not None else 0,
            )
        base, other = (a, b) if score(a) >= score(b) else (b, a)
    merged = dict(base)
    for field in ("execution_id", "query_id", "params_canon", "lookback_hours",
                  "day_offset", "engine", "shape", "cost", "cost_field",
                  "anchor_utc", "anchor_field", "anchor_iso",
                  "declared_result_bytes", "wire_bytes", "wire_provenance",
                  "total_rows", "state"):
        if merged.get(field) is None and other.get(field) is not None:
            merged[field] = other[field]
    merged["sources"] = tuple(dict.fromkeys(
        (base.get("sources") or (base["source"],))
        + (other.get("sources") or (other["source"],))
    ))
    return merged


def _batch_lane_rows(batches):
    """Candidate cost rows from ``state.approved_recovery_batches``.

    The REAL state shape is a batch-id keyed map of batch CONTAINERS holding
    authorization/metadata plus an ``operations`` map (operation name ->
    operation row); the container itself is batch metadata, never a
    financial sample. Only the actual operation rows are extracted,
    preserving their query id, parameters and capture stamps. Alternative
    shapes stay supported: a list of operation rows, a flat id->row map, or
    a single batch container object. A container without ``operations`` and
    without its own ``query_id`` (e.g. a pure authorization record) is
    recognized as batch structure and skipped: it is not financial evidence
    and not a malformed cost row. Operation rows themselves are never
    silently ignored — a malformed cost there still raises."""
    def rows(operations, source):
        if isinstance(operations, list):
            entries = enumerate(operations)
        elif isinstance(operations, dict):
            entries = operations.items()
        else:
            raise EstimatorInputError(f"{source} must be an operation list or map")
        result = []
        for key, row in entries:
            if not isinstance(row, dict):
                raise EstimatorInputError(f"{source}[{key!r}] is not an operation object")
            result.append((row, f"{source}[{key!r}]"))
        return result
    if isinstance(batches, list):
        return rows(batches, "approved_recovery_batches"), {}
    if not isinstance(batches, dict):
        raise EstimatorInputError(
            "state approved_recovery_batches must be a list, an operations "
            "list object, a batch-container map, or an id->operation object, "
            f"got {type(batches).__name__}"
        )
    candidates, skipped = [], {}
    if "operations" in batches:
        return rows(batches["operations"], "approved_recovery_batches.operations"), {}
    for key, container in batches.items():
        source = f"approved_recovery_batches[{key}]"
        if not isinstance(container, dict):
            raise EstimatorInputError(
                f"{source} is not an object: {container!r}"
            )
        if "operations" in container:
            candidates.extend(rows(container["operations"], source + ".operations"))
            continue
        if container.get("query_id") is None:
            if any(field in container for field in _COST_FIELDS):
                raise EstimatorInputError(f"{source} has a cost but no query_id")
            skipped[source] = skipped.get(source, 0) + 1
            continue
        candidates.append((container, source))
    return candidates, skipped


def _sample_pool(state, now, cfg):
    """Normalized, id-deduped sample pool from every local evidence lane."""
    if not isinstance(state, dict):
        raise EstimatorInputError(
            f"state must be an object, got {type(state).__name__}"
        )
    raw_rows, batch_skipped = [], {}

    history = state.get("execution_history")
    if history is not None:
        if not isinstance(history, list):
            raise EstimatorInputError(
                "state execution_history must be a list, got "
                f"{type(history).__name__}"
            )
        raw_rows.extend((row, "execution_history") for row in history)

    batches = state.get("approved_recovery_batches")
    if batches is not None:
        batch_rows, batch_skipped = _batch_lane_rows(batches)
        raw_rows.extend(batch_rows)

    imported = state.get("autonomous_cost_samples")
    if imported is not None:
        if not isinstance(imported, list):
            raise EstimatorInputError(
                "state autonomous_cost_samples must be a list of vetted "
                f"sample rows, got {type(imported).__name__}"
            )
        raw_rows.extend((row, "autonomous_cost_samples") for row in imported)

    lane = state.get("financial_journal")
    if lane is not None:
        if not isinstance(lane, dict) or not isinstance(
            lane.get("obligations"), dict
        ):
            raise EstimatorInputError(
                "state financial_journal must carry an obligations object"
            )
        for key, row in lane["obligations"].items():
            if not isinstance(row, dict):
                raise EstimatorInputError(
                    f"financial_journal obligation {key!r} is not an object"
                )
            if row.get("kind") != "execution":
                continue
            if row.get("unresolved") is True:
                continue
            raw_rows.append((row, "financial_journal"))

    normalized, skips = [], dict(batch_skipped)
    for index, (row, source) in enumerate(raw_rows):
        sample, skip_reason = _normalized_row(row, source, index, now, cfg)
        if sample is None:
            skips[skip_reason] = skips.get(skip_reason, 0) + 1
            continue
        normalized.append(sample)

    deduped, conflicts = [], 0
    by_key = {}
    for row in normalized:
        key = row["execution_id"]
        if key is None:
            key = _fingerprint_row(row, 0)
            while key in by_key:
                key = _fingerprint_row(row, int(key.rsplit(":", 1)[1]) + 1)
            row["execution_id"] = key
        previous_index = by_key.get(key)
        if previous_index is not None:
            previous = deduped[previous_index]
            if (previous["cost"] is not None and row["cost"] is not None
                    and abs(previous["cost"] - row["cost"]) > 1e-12):
                conflicts += 1
            deduped[previous_index] = _merge_rows(previous, row)
            by_key[key] = previous_index
            continue
        by_key[key] = len(deduped)
        row["sources"] = (row["source"],)
        deduped.append(row)

    return deduped, {
        "raw_rows": len(raw_rows),
        "deduped_rows": len(deduped),
        "duplicate_cost_conflicts": conflicts,
        "skipped": skips,
    }


def _aging_factor(age_hours, cfg):
    span = cfg["max_age_hours"] - cfg["aging_free_hours"]
    if span <= 0:
        return 1.0
    excess = age_hours - cfg["aging_free_hours"]
    if excess <= 0:
        return 1.0
    fraction = min(excess / span, 1.0)
    return 1.0 + cfg["aging_margin_fraction"] * fraction


# ---- identity and pools ---------------------------------------------------------

def _identity_triplet(query_tag, engine, shape):
    return (
        query_tag,
        None if engine is None else str(engine),
        None if shape is None else str(shape),
    )


def _classify_pools(pool, triplet, params_canon, req_lookback, req_day_offset,
                    cfg, now):
    """Split identity-matching pool rows into exact pools, lookback-transfer
    candidates and single-UTC-day calendar-translation candidates, with
    explicit blocking reasons."""
    cfg_transfer = cfg["lookback_transfer"]
    cfg_day = cfg["day_family_transfer"]
    matching = [
        row for row in pool if _identity_triplet(
            row["query_id"], row["engine"], row["shape"]
        ) == triplet
    ]
    base_canon = _rebase_canon(params_canon)
    exact_usable, exact_stale, exact_undated, exact_unknown_cost = [], [], [], []
    transfer_usable, transfer_blocked = [], []
    family_usable, family_blocked = [], []
    transfer_domain = (
        None if req_lookback is None
        else _query_domain_hours(cfg, triplet[0])
    )
    transfer_enabled = (
        cfg_transfer["enabled"]
        and req_lookback is not None
        and transfer_domain is not None
        and req_lookback <= transfer_domain
    )
    day_floor, day_ceiling = (
        cfg_day["min_offset_days"], cfg_day["max_offset_days"]
    )
    req_day_in_domain = (
        req_day_offset is not None and day_floor <= req_day_offset <= day_ceiling
    )
    day_enabled = cfg_day["enabled"] and req_day_in_domain
    for row in matching:
        age_hours = None
        if row["anchor_utc"] is not None:
            age_hours = (now - row["anchor_utc"]).total_seconds() / 3600.0
        if row["params_canon"] == params_canon:
            if row["cost"] is None:
                exact_unknown_cost.append(row)
            elif age_hours is None:
                exact_undated.append(row)
            elif age_hours > cfg["max_age_hours"]:
                exact_stale.append(row)
            else:
                exact_usable.append(row)
            continue
        if transfer_enabled:
            if row["lookback_hours"] is None:
                transfer_blocked.append("no_lookback_parameters")
            elif _rebase_canon(row["params_canon"]) != base_canon:
                transfer_blocked.append("other_parameters_differ")
            elif row["lookback_hours"] > transfer_domain:
                transfer_blocked.append("sample_lookback_outside_domain")
            elif row["cost"] is None:
                transfer_blocked.append("unknown_cost")
            elif age_hours is None:
                transfer_blocked.append("no_exact_timestamp")
            elif age_hours > cfg["max_age_hours"]:
                transfer_blocked.append("stale_sample")
            else:
                transfer_usable.append(
                    (row, max(1.0, req_lookback / row["lookback_hours"]))
                )
        if (day_enabled and row["day_offset"] is not None
                and row["day_offset"] != req_day_offset):
            if not day_floor <= row["day_offset"] <= day_ceiling:
                family_blocked.append("sample_day_offset_outside_family_domain")
            elif row["cost"] is None:
                family_blocked.append("unknown_cost")
            elif age_hours is None:
                family_blocked.append("no_exact_timestamp")
            elif age_hours > cfg["max_age_hours"]:
                family_blocked.append("stale_sample")
            else:
                family_usable.append(row)

    return {
        "matching": matching,
        "exact_usable": exact_usable,
        "exact_stale": exact_stale,
        "exact_undated": exact_undated,
        "exact_unknown_cost": exact_unknown_cost,
        "transfer_usable": transfer_usable,
        "transfer_blocked": transfer_blocked,
        "transfer_domain": transfer_domain,
        "transfer_enabled": transfer_enabled,
        "day_family_enabled": day_enabled,
        "requested_day_offset_days": req_day_offset,
        "day_offset_domain_days": (day_floor, day_ceiling),
        "family_usable": family_usable,
        "family_blocked": family_blocked,
    }


def _rebase_canon(params_canon):
    """Canonical string of the parameters without lookback_hours."""
    try:
        params = json.loads(params_canon) if params_canon else None
    except ValueError:
        return params_canon
    if params is None:
        return ""
    if not isinstance(params, dict):
        return params_canon
    params = {k: v for k, v in params.items() if k != "lookback_hours"}
    try:
        return canon_parameters(params)
    except BudgetInputError:
        return params_canon


# ---- envelope folding -----------------------------------------------------------

def _fold_quote(adjusted, scaled_raw, cfg):
    """Per-run quote from one pool's adjusted (aging/scaled) costs plus the
    raw lookback-scaled costs. >= 2 samples: max(observed_max, mean+3sigma);
    exactly 1: explicit single-sample margin. Returns
    (quote, expected_mean, observed_max, method_fragment)."""
    mean = statistics.fmean(scaled_raw)
    if len(scaled_raw) == 1:
        quote = cfg["single_sample_margin"] * adjusted[0]
        method = "single_sample_explicit_margin"
    else:
        sigma = statistics.stdev(adjusted)
        quote = max(
            max(adjusted),
            statistics.fmean(adjusted) + cfg["variance_uplift_sigma"] * sigma,
        )
        method = "empirical_envelope_mean_plus_3sigma"
    return quote, mean, max(scaled_raw), method

QUOTE_BASIS_NOTE = (
    "credible estimate from local evidence; not a cap, not a guarantee and "
    "not spending authority - actual charge is knowable only after execution"
)


def _pool_counts(pools):
    return {
        "matching_rows": len(pools["matching"]),
        "exact_usable": len(pools["exact_usable"]),
        "exact_stale": len(pools["exact_stale"]),
        "exact_undated": len(pools["exact_undated"]),
        "exact_unknown_cost": len(pools["exact_unknown_cost"]),
        "transfer_usable": len(pools["transfer_usable"]),
        "transfer_blocked": len(pools["transfer_blocked"]),
        "family_usable": len(pools["family_usable"]),
        "family_blocked": len(pools["family_blocked"]),
    }


def _blocked_summary(blocked):
    summary = {}
    for reason in blocked:
        summary[reason] = summary.get(reason, 0) + 1
    return ", ".join(f"{key}={count}" for key, count in sorted(summary.items()))


def _execution_reasons(pools, cfg, req_lookback, req_day_offset):
    """Explain why NO credible quote exists (unknown status), fail-closed."""
    reasons = []
    if not pools["matching"]:
        reasons.append("no_samples_for_identity")
        return reasons
    exact_parts = []
    if pools["exact_stale"]:
        exact_parts.append(
            f"{len(pools['exact_stale'])} stale beyond "
            f"{cfg['max_age_hours']:g}h (30d evidence ceiling)"
        )
    if pools["exact_undated"]:
        exact_parts.append(
            f"{len(pools['exact_undated'])} without an exact timestamp"
        )
    if pools["exact_unknown_cost"]:
        exact_parts.append(
            f"{len(pools['exact_unknown_cost'])} with unknown terminal cost"
        )
    if not pools["exact_usable"]:
        if exact_parts:
            reasons.append(
                "no_usable_exact_identity_samples (" + "; ".join(exact_parts) + ")"
            )
        else:
            reasons.append("no_samples_for_exact_identity")
    if req_lookback is not None:
        if not cfg["lookback_transfer"]["enabled"]:
            reasons.append("lookback_transfer_disabled_by_policy")
            return reasons
        if req_lookback > pools["transfer_domain"]:
            reasons.append(
                f"requested_lookback_outside_transfer_domain: {req_lookback:g}h "
                f"exceeds the {pools['transfer_domain']:g}h domain for query "
                f"{pools['matching'][0]['query_id'] if pools['matching'] else '?'}"
            )
            return reasons
        if pools["transfer_blocked"]:
            reasons.append(
                "no_in_domain_transfer_samples (blocked: "
                + _blocked_summary(pools["transfer_blocked"]) + ")"
            )
    day_cfg = cfg["day_family_transfer"]
    if req_day_offset is not None:
        if not day_cfg["enabled"]:
            reasons.append("day_family_transfer_disabled_by_policy")
            return reasons
        floor, ceiling = day_cfg["min_offset_days"], day_cfg["max_offset_days"]
        if not floor <= req_day_offset <= ceiling:
            reasons.append(
                f"requested_day_offset_outside_single_day_family_domain: "
                f"{req_day_offset} is outside the one-UTC-day offset domain "
                f"{floor}..{ceiling}"
            )
            return reasons
        if pools["family_blocked"]:
            reasons.append(
                "no_other_single_day_family_samples (blocked: "
                + _blocked_summary(pools["family_blocked"]) + ")"
            )
        else:
            reasons.append(
                "no_other_in_domain_single_day_samples: no usable same-query "
                "single-UTC-day sample at another exact day offset"
            )
        return reasons
    if req_lookback is None and req_day_offset is None:
        reasons.append(
            "no_lookback_hours_parameter: no exact samples and no "
            "semantically justified transfer axis"
        )
    return reasons


def _execution_estimate(pool, pool_summary, spec_identity, params_canon,
                        req_lookback, req_day_offset, now, cfg):
    triplet, query_tag = spec_identity, spec_identity[0]
    pools = _classify_pools(
        pool, triplet, params_canon, req_lookback, req_day_offset, cfg, now
    )
    evidence = {
        "identity": {
            "query_id": query_tag,
            "parameters": params_canon,
            "engine": triplet[1],
            "shape_version": triplet[2],
        },
        "requested_lookback_hours": req_lookback,
        "requested_day_offset_days": req_day_offset,
        "pool_counts": _pool_counts(pools),
        "pool_summary": pool_summary,
        "aging": {
            "free_hours": cfg["aging_free_hours"],
            "max_age_hours": cfg["max_age_hours"],
            "margin_fraction": cfg["aging_margin_fraction"],
            "applied": False,
            "max_factor": 1.0,
        },
        "lookback_scaling": None,
        "calendar_translation": None,
        "sample_sources": [],
        "sample_execution_ids": [],
        "sample_parameters": [],
        "sample_ages_hours": [],
        "cost_min_credits": None,
        "cost_mean_pooled_credits": None,
        "cost_observed_max_credits": None,
    }

    def finish(pool_rows, scaled, *, method):
        aging_values = []
        for row in pool_rows:
            if row["anchor_utc"] is not None:
                age_hours = (now - row["anchor_utc"]).total_seconds() / 3600.0
            else:
                age_hours = 0.0
            aging_values.append(_aging_factor(age_hours, cfg))
        adjusted = [
            cost * value for cost, value in zip(scaled, aging_values)
        ]
        quote, expected, observed_max, fold_method = _fold_quote(
            adjusted, scaled, cfg
        )
        aging_max = max(aging_values) if aging_values else 1.0
        method_label = method + "_" + fold_method
        if len(pool_rows) == 1:
            method_label += "_single_usable_sample"
        sample_costs = [row["cost"] for row in pool_rows]
        evidence["sample_sources"] = [
            sorted(set(row["sources"])) for row in pool_rows
        ]
        evidence["sample_execution_ids"] = [
            row["execution_id"] for row in pool_rows
        ]
        evidence["sample_parameters"] = [row["params_canon"] for row in pool_rows]
        evidence["sample_ages_hours"] = [
            None if row["anchor_utc"] is None
            else (now - row["anchor_utc"]).total_seconds() / 3600.0
            for row in pool_rows
        ]
        evidence["method"] = method_label
        evidence["aging"] = {
            "free_hours": cfg["aging_free_hours"],
            "max_age_hours": cfg["max_age_hours"],
            "margin_fraction": cfg["aging_margin_fraction"],
            "applied": aging_max > 1.0,
            "max_factor": aging_max,
        }
        evidence["cost_min_credits"] = min(sample_costs)
        evidence["cost_mean_pooled_credits"] = expected
        evidence["cost_observed_max_credits"] = observed_max
        return {
            "status": "estimated",
            "cost_estimate_credits": quote,
            "expected_credits": expected,
            "evidence": evidence,
            "reasons": [],
        }

    if pools["exact_usable"]:
        rows = pools["exact_usable"]
        raw_costs = [row["cost"] for row in rows]
        return finish(rows, raw_costs, method="exact_identity")

    if req_lookback is not None and pools["transfer_usable"]:
        rows = [row for row, _factor in pools["transfer_usable"]]
        factors = [factor for _row, factor in pools["transfer_usable"]]
        scaled = [row["cost"] * factor for row, factor in pools["transfer_usable"]]
        request_scale_note = {
            "applied": True,
            "requested_lookback_hours": req_lookback,
            "domain_max_hours": pools["transfer_domain"],
            "sample_lookback_hours": [row["lookback_hours"] for row in rows],
            "scaling_factors": factors,
            "formula": "max(1, requested_hours / sample_hours)",
        }
        evidence["lookback_scaling"] = request_scale_note
        return finish(rows, scaled, method="lookback_transfer_scaled")

    if pools["family_usable"]:
        rows = pools["family_usable"]
        evidence["calendar_translation"] = {
            "applied": True,
            "method": DAY_TRANSFER_METHOD,
            "predicate": (
                "oldest_days_ago == newest_days_ago: each pooled sample and "
                "the request are exactly one UTC day absolute scan"
            ),
            "requested_day_offset_days": req_day_offset,
            "sample_day_offsets_days": [row["day_offset"] for row in rows],
            "scaling": (
                "none - per-day scan cost variance is carried by the pooled "
                "same-family sample costs (no extra margin is invented)"
            ),
        }
        return finish(
            rows, [row["cost"] for row in rows], method=DAY_TRANSFER_METHOD
        )

    reasons = _execution_reasons(pools, cfg, req_lookback, req_day_offset)
    evidence["method"] = "no_usable_identity_samples"
    return {
        "status": "unknown",
        "cost_estimate_credits": None,
        "expected_credits": None,
        "evidence": evidence,
        "reasons": reasons,
    }


# ---- export transport estimate --------------------------------------------------

def _ratio_evidence(pool, triplet, now):
    """Same-query wire/declared ratio samples: rows sharing the query+engine+
    shape with a positive declared size and actual-bytes evidence."""
    ratios = []
    for row in pool:
        if _identity_triplet(
            row["query_id"], row["engine"], row["shape"]
        ) != triplet:
            continue
        declared = row["declared_result_bytes"]
        wire = row["wire_bytes"]
        if declared is None or not declared > 0 or wire is None:
            continue
        if row["total_rows"] == 0:
            continue
        ratios.append({
            "id": row["execution_id"],
            "params": row["params_canon"],
            "age_hours": None if row["anchor_utc"] is None
            else (now - row["anchor_utc"]).total_seconds() / 3600.0,
            "declared_bytes": declared,
            "bytes": wire,
            "ratio": wire / declared,
            "provenance": row["wire_provenance"],
        })
    return ratios


def _export_estimate(pool, pool_summary, spec_identity, params_canon,
                     declared_bytes, total_rows, cfg, now):
    triplet, query_tag = spec_identity, spec_identity[0]
    declared = (
        None if declared_bytes is None
        else _nonnegative_number(declared_bytes, "result_bytes")
    )
    if total_rows is not None:
        rows_count = _integral(total_rows, "total_rows")
    else:
        rows_count = None

    def finalize(evidence, method=None):
        export = evidence["export"]
        if method is not None:
            export["method"] = method
        ratios = export.get("ratio_samples") or []
        evidence["method"] = export["method"]
        evidence["sample_execution_ids"] = [
            entry["id"] for entry in ratios
        ]
        evidence["sample_parameters"] = [
            entry["params"] for entry in ratios
        ]
        evidence["sample_ages_hours"] = [
            entry["age_hours"] for entry in ratios
        ]
        return evidence

    if rows_count == 0:
        if declared is not None and declared > 0:
            raise EstimatorInputError(
                "contradictory export evidence: a zero-row dataset cannot "
                f"declare {declared!r} result bytes"
            )
        evidence = finalize(_export_evidence_skeleton(
            pool, pool_summary, triplet, params_canon, declared, rows_count, cfg
        ), method="zero_row_result")
        evidence["export"]["transport_bytes"] = 0.0
        evidence["export"]["basis"] = TRANSPORT_BASIS_NOTE
        return {
            "status": "estimated",
            "cost_estimate_credits": 0.0,
            "expected_credits": 0.0,
            "evidence": evidence,
            "reasons": [],
        }

    if declared is None or declared <= 0:
        if declared == 0 and rows_count is not None and rows_count > 0:
            raise EstimatorInputError(
                "contradictory export evidence: a nonempty dataset "
                f"({rows_count} rows) cannot declare zero result bytes; "
                "a positive declared size is required"
            )
        reasons = [(
            "declared_result_bytes_not_positive: a nonempty dataset requires "
            "a positive declared result size (Dune result metadata size is a "
            "declared estimate of the completed query, not a wire metering)"
        )]
        if declared is None:
            reasons.insert(0, "missing_export_transport_basis: no declared "
                              "result_bytes for the completed result")
        return {
            "status": "unknown",
            "cost_estimate_credits": None,
            "expected_credits": None,
            "evidence": finalize(_export_evidence_skeleton(
                pool, pool_summary, triplet, params_canon, declared,
                rows_count, cfg
            ), method="no_transport_basis"),
            "reasons": reasons,
        }

    ratios = _ratio_evidence(pool, triplet, now)
    evidence = _export_evidence_skeleton(
        pool, pool_summary, triplet, params_canon, declared, rows_count, cfg,
        ratios=ratios,
    )
    export_evidence = evidence["export"]

    if ratios:
        values = [entry["ratio"] for entry in ratios]
        if len(values) == 1:
            ratio_quote = cfg["single_sample_margin"] * values[0]
            ratio_method = "single_ratio_sample_explicit_margin"
        else:
            sigma = statistics.stdev(values)
            ratio_quote = max(
                max(values),
                statistics.fmean(values) + cfg["variance_uplift_sigma"] * sigma,
            )
            ratio_method = "ratio_envelope_mean_plus_3sigma"
        ratio_quote = max(ratio_quote, 1.0)
        export_evidence.update({
            "method": ratio_method,
            "wire_inflation_ratio": ratio_quote,
        })
        transport_bytes = (
            declared * ratio_quote * (1.0 + cfg["overhead_transport_fraction"])
            + cfg["fixed_transport_overhead_bytes"]
        )
    elif cfg["transport_format_bound_fallback"]:
        ratio_quote = cfg["format_bound_multiplier"]
        export_evidence.update({
            "method": "declared_format_bound_fallback",
            "wire_inflation_ratio": ratio_quote,
            "fallback": (f"policy-accepted documented conservative format "
                         f"bound: {ratio_quote:g}x declared serialised bytes; "
                         "not ratio evidence and no further 25% margin "
                         "(the 4x bound absorbs it)"),
        })
        transport_bytes = (
            declared * ratio_quote + cfg["fixed_transport_overhead_bytes"]
        )
    else:
        export_evidence["method"] = "no_ratio_evidence"
        return {
            "status": "unknown",
            "cost_estimate_credits": None,
            "expected_credits": None,
            "evidence": finalize(evidence),
            "reasons": [
                "no_export_ratio_evidence: no same-query wire/declared "
                "result-bytes ratio samples; refusing to quote transport "
                "without evidence (policy may explicitly accept the "
                "documented 4x+16KiB format bound via "
                "transport_format_bound_fallback)"
            ],
        }
    export_credits = transport_bytes / 1_000_000.0 * cfg["export_rate_per_mb"]
    export_evidence["transport_bytes"] = transport_bytes
    export_evidence["basis"] = TRANSPORT_BASIS_NOTE
    return {
        "status": "estimated",
        "cost_estimate_credits": export_credits,
        "expected_credits": export_credits,
        "evidence": finalize(evidence),
        "reasons": [],
    }


def _export_evidence_skeleton(pool, pool_summary, triplet, params_canon,
                              declared, rows_count, cfg, ratios=None):
    evidence = {
        "identity": {
            "query_id": triplet[0],
            "parameters": params_canon,
            "engine": triplet[1],
            "shape_version": triplet[2],
        },
        "pool_summary": pool_summary,
        "kind": "export",
        "export": {
            "declared_result_bytes": declared,
            "total_rows": rows_count,
            "declared_is_estimate": (
                "Dune result metadata declared size estimates the completed "
                "result set; it is not metered wire"
            ),
            "overhead_transport_fraction": cfg["overhead_transport_fraction"],
            "fixed_transport_overhead_bytes": cfg["fixed_transport_overhead_bytes"],
            "export_rate_per_mb": cfg["export_rate_per_mb"],
            "ratio_samples": ratios or [],
            "transport_bytes": None,
            "fallback": None,
            "method": None,
            "basis": None,
        },
    }
    return evidence


# ---- public per-operation API ---------------------------------------------------

def _validate_spec(spec, kind):
    if not isinstance(spec, dict):
        raise EstimatorInputError(
            f"spec must be an object, got {type(spec).__name__}"
        )
    if spec.get("query_id") is None:
        raise EstimatorInputError("spec has no query_id")
    query_tag = _stringish_tag(spec["query_id"], "spec.query_id")
    engine = spec.get("performance")
    if engine is None:
        engine = spec.get("engine")
    engine = _stringish_tag(engine, "spec engine")
    shape = spec.get("shape_version")
    if shape is None:
        shape = spec.get("shape")
    shape = _stringish_tag(shape, "spec shape_version")
    total_rows = spec.get("total_rows")
    if total_rows is None:
        total_rows = spec.get("result_row_count")
    if total_rows is not None:
        total_rows = _integral(total_rows, "spec total_rows")
    declared = spec.get("result_bytes")
    if declared is not None:
        declared = _nonnegative_number(declared, "spec result_bytes")
    return query_tag, engine, shape, total_rows, declared


def estimate_operation(state, spec, params, now_utc, *, kind="execution",
                       result_bytes=None, policy=None):
    """Defensible credit estimate for ONE planned autonomous operation.

    Returns {"status": "estimated"|"unknown", "cost_estimate_credits",
    "expected_credits", "evidence": {...}, "reasons": [...]}. The quote is a
    conservative evidence-backed estimate — never a hard cap and never
    spending authority; ``unknown`` blocks instead of inventing numbers.
    Inputs are read-only: ``state`` is never mutated.
    """
    if not isinstance(kind, str) or kind not in KINDS:
        raise EstimatorInputError(
            f"kind must be one of {KINDS}, got {kind!r}"
        )
    cfg = _policy_config(policy)
    now = _instant(now_utc, "now_utc")
    query_tag, engine, shape, spec_rows, spec_declared = _validate_spec(spec, kind)
    if engine is None:
        engine = cfg["default_engine"]

    params_raw = params
    if params_raw is not None and not isinstance(params_raw, dict):
        raise EstimatorInputError(
            f"params must be an object or None, got "
            f"{type(params_raw).__name__}"
        )
    try:
        params_canon = canon_parameters(params_raw)
    except BudgetInputError as error:
        raise EstimatorInputError(
            f"params are not canonicalizable: {error}"
        ) from None
    req_lookback = _lookback_hours(params_raw, "params")
    req_day_offset = _day_offset(params_raw, "params")

    if result_bytes is None:
        declared = spec_declared
    else:
        declared = _nonnegative_number(result_bytes, "result_bytes")

    pool, pool_summary = _sample_pool(state, now, cfg)
    triplet = _identity_triplet(query_tag, engine, shape)

    if kind == "export":
        total_rows = spec_rows
        result = _export_estimate(
            pool, pool_summary, triplet, params_canon, declared,
            total_rows, cfg, now,
        )
    else:
        result = _execution_estimate(
            pool, pool_summary, triplet, params_canon, req_lookback,
            req_day_offset, now, cfg,
        )
    result["kind"] = kind
    result["now_utc"] = _z(now)
    evidence = result["evidence"]
    evidence["pool_summary"] = pool_summary
    evidence["quote_basis"] = QUOTE_BASIS_NOTE
    result["estimate_is_hard_cap"] = False
    result["estimate_is_authority"] = False
    return result


# ---- remaining-calendar forecast --------------------------------------------------

def _forecast_runs(remaining_seconds, cadence_seconds):
    if remaining_seconds <= 0:
        return 0
    exact, remainder = divmod(remaining_seconds, cadence_seconds)
    return int(exact) + (1 if remainder > 0 else 0)


def forecast_remaining_calendar(entries, *, now_utc, period_end_utc):
    """Mean-based forecast of standing entries to the exact period end.

    Each entry is {"key", "cadence_seconds"|"cadence_minutes", "estimate"}:
    ``estimate`` is one :func:`estimate_operation` result. Runs use the
    repo's conservative ceil rule over exact remaining seconds. The expected
    total multiplies each unit's outlier-INCLUSIVE expected mean; the
    observed-maximum risk is returned separately as
    ``observed_max_risk_total_credits`` and never silently folded in.
    Unknown-cost entries surface in ``unknown_entries`` and keep
    ``exposure_is_fully_known`` False instead of inflating the total.
    """
    now = _instant(now_utc, "now_utc")
    end = _instant(period_end_utc, "period_end_utc")
    remaining = max((end - now).total_seconds(), 0.0)

    detail, unknown = [], []
    expected_total = quote_total = observed_max_total = 0.0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise EstimatorInputError(
                f"forecast entry[{index}] is not an object: {entry!r}"
            )
        key = entry.get("key")
        if not isinstance(key, (str, int)) or isinstance(key, bool):
            raise EstimatorInputError(
                f"forecast entry[{index}] needs a string or int key, got {key!r}"
            )
        if "cadence_seconds" in entry:
            cadence_seconds = _nonnegative_number(
                entry["cadence_seconds"],
                f"forecast entry {key!r} cadence_seconds",
                positive=True,
            )
        elif "cadence_minutes" in entry:
            cadence_seconds = _nonnegative_number(
                entry["cadence_minutes"],
                f"forecast entry {key!r} cadence_minutes",
                positive=True,
            ) * 60.0
        else:
            raise EstimatorInputError(
                f"forecast entry {key!r} needs cadence_seconds or "
                "cadence_minutes"
            )
        enabled = _finite_bool(
            entry.get("enabled", True), f"forecast entry {key!r} enabled"
        )
        runs = _forecast_runs(remaining, cadence_seconds)
        estimate = entry.get("estimate")
        if not isinstance(estimate, dict) or estimate.get("status") not in (
            "estimated", "unknown"
        ):
            raise EstimatorInputError(
                f"forecast entry {key!r} estimate must be an "
                "estimate_operation result with status estimated/unknown"
            )
        if not enabled:
            detail.append({
                "key": key, "status": "disabled", "cadence_seconds":
                cadence_seconds, "runs": 0, "expected_credits": 0.0,
                "observed_max_risk_credits": 0.0,
                "cost_estimate_credits": 0.0,
            })
            continue
        if estimate["status"] == "unknown":
            unknown.append({
                "key": key,
                "query_id": (estimate.get("evidence") or {}).get(
                    "identity", {}
                ).get("query_id"),
                "runs": runs,
                "reasons": estimate.get("reasons")
                or (estimate.get("evidence") or {}).get("reasons") or [],
            })
            detail.append({
                "key": key, "status": "unknown",
                "cadence_seconds": cadence_seconds, "runs": runs,
                "expected_credits": None,
                "observed_max_risk_credits": None,
                "cost_estimate_credits": None,
            })
            continue
        unit = estimate.get("cost_estimate_credits")
        unit_expected = estimate.get("expected_credits")
        evidence = estimate.get("evidence") or {}
        unit_max = evidence.get("cost_observed_max_credits")
        if unit_expected is None:
            unit_expected = unit
        unit = _nonnegative_number(unit, f"forecast entry {key!r} unit quote")
        unit_expected = _nonnegative_number(
            unit_expected, f"forecast entry {key!r} unit expected"
        )
        unit_max = (
            unit if unit_max is None
            else _nonnegative_number(unit_max, f"forecast entry {key!r} "
                                     "unit observed max")
        )
        expected_total += runs * unit_expected
        quote_total += runs * unit
        observed_max_total += runs * unit_max
        detail.append({
            "key": key,
            "query_id": (estimate.get("evidence") or {}).get(
                "identity", {}
            ).get("query_id"),
            "status": "scheduled",
            "cadence_seconds": cadence_seconds,
            "runs": runs,
            "unit_expected_credits": unit_expected,
            "unit_cost_estimate_credits": unit,
            "unit_observed_max_credits": unit_max,
            "expected_credits": runs * unit_expected,
            "observed_max_risk_credits": runs * unit_max,
            "cost_estimate_credits": runs * unit,
        })
    return {
        "now_utc": _z(now),
        "period_end_utc": _z(end),
        "remaining_seconds_before_renewal": remaining,
        "entries": detail,
        "expected_total_credits": expected_total,
        "cost_estimate_total_credits": quote_total,
        "observed_max_risk_total_credits": observed_max_total,
        "unknown_entries": unknown,
        "exposure_is_fully_known": not unknown,
        "mean_basis_note": (
            "expected means are outlier-INCLUSIVE (outliers are neither "
            "dropped nor multiplied); the observed-maximum risk is reported "
            "separately, never folded into the mean"
        ),
    }
