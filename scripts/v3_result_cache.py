"""``v3_result_cache`` - reuse an exact public onchain export result without re-paying.

No API client, no Git: the caller fetches; the caller's ``checkpoint(state)`` bridge (Astra wires it
to the state store) durably commits changes. Two ``data/current/v3_result_cache.json`` entries live
within 6 MB (standard ``encoded()`` JSON written via the store's ``atomic_write``) and
``state["result_exports"]`` markers carry the caller's risk_credits/evidence/timestamp. Estimates
MUST come from the caller with sample evidence (never computed from compute telemetry); markers
never retire by age; no TTL.

``prepare_export`` returns the validated cached entry dict, or None after stamping ``requested``
and checkpointing it BEFORE the caller fetches. Any requested/in_progress/unknown marker, and a
cached marker whose file entry is missing, raises ``ManualExportReview`` (no second export).
``prepare_export(..., isolate_unrelated=True)`` is the opt-in guarded-autonomous mode: unresolved
markers that belong to OTHER exact bindings no longer hold the new binding, while the caller's own
marker still blocks until an authorized retry clears it. The default remains the global hold.

``retry_export`` is the ONLY authorized recovery for an unresolved result read (requested/
in_progress/unknown, or cached bytes that are missing). It never re-executes compute and never
retries by age: it requires a caller-supplied, trust-verified standing approval whose exact kind/
query/parameters/execution identifies this binding, whose positive quote is the NEW read's own
credible estimate (the archived old risk is never rewritten or used as the new quote), whose
operation id is new, and whose exact approved/expiry instants cover the retry timestamp. It
synchronizes the financial journal BEFORE any marker change, archives the complete original marker
plus the exact new approval under ``state["result_export_retries"][binding_key]`` (never trimmed),
removes only that binding's marker, and checkpoints BEFORE the caller stages a new
``prepare_export``/fetch. The original financial obligation survives unknown and unaltered; a
replacement is never justified by elapsed time alone. At most ``max_attempts`` read attempts are
allowed per exact execution per UTC day (the original plus retries that day), while the lifetime
archive is unbounded. A daily cap raises ``ExportRetryExhausted`` with ``retry_after_utc`` set to
the next UTC day; it is transient, not a permanent manual hold. A checkpoint failure raises
``CheckpointFailed`` (a ``StateError``) and preserves the candidate in memory rather than rolling
back a possibly accepted barrier.

``cache_result`` validates the completed fetch (exact execution, complete row count - pagination
refused, scalar public rows on the allowlist, checksum), writes the file, marks ``cached``. When a
retry recovered a marker whose cached bytes were missing, the archived prior digest must still
match the recovered rows before anything is written.
``prune_consumed`` (main, after the source result is fully accepted) is the only removal:
consumed entries drop from the file, markers stay as telemetry, unconsumed entries are never
evicted."""
import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from v3_credit_budget import BudgetInputError, parse_instant
from v3_financial_journal import synchronize_financial_journal
from v3_live_state_store import StateError, atomic_write, encoded
CACHE_PATH_RELATIVE = "data/current/v3_result_cache.json"
CACHE_SCHEMA_VERSION = 1
MAX_CACHE_RECORDS = 2
MAX_CACHE_PAYLOAD_BYTES = 6_000_000
MAX_EXPORT_ROWS = 50_000
OUTSTANDING_RESULT_EXPORT_STATUSES = frozenset({"requested", "in_progress", "unknown"})
CACHE_STATUSES = frozenset(OUTSTANDING_RESULT_EXPORT_STATUSES | {"cached", "consumed"})
_LANE_KEYS = frozenset({"schema_version", "requests"})
_FILE_KEYS = frozenset({"schema_version", "records"})
_SPEC_KEYS = frozenset({"query_id", "sql_sha256", "parameters", "columns"})
_MARKER_KEYS = frozenset({"key", "query_id", "sql_sha256", "parameters", "columns", "execution_id", "status", "requested_at_utc", "risk_credits", "evidence", "result_sha256", "total_row_count"})
_ENTRY_KEYS = frozenset({"schema_version", "key", "query_id", "sql_sha256", "parameters", "columns", "execution_id", "total_row_count", "result_sha256", "result_rows", "stored_at_utc"})
_RESULT_KEYS = frozenset({"execution_id", "total_row_count", "result_rows"})
_ARCHIVE_KEYS = frozenset({"marker", "approval"})
_APPROVAL_KEYS = frozenset({"operation_id", "kind", "query_id", "query_parameters", "execution_id", "quote_credits", "approved_at_utc", "valid_until_utc"})
_RETRYABLE_STATUSES = frozenset(OUTSTANDING_RESULT_EXPORT_STATUSES | {"cached"})
RETRY_LANE = "result_export_retries"
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_CREDENTIALISH = ("api_key", "apikey", "api-key", "bearer", "authorization", "password", "secret", "credential", "access_key", "private_key")
class CacheError(RuntimeError):
    """The cache refused malformed, tampered, or bound-violating data."""
class ManualExportReview(CacheError): pass
class CheckpointFailed(StateError):
    """A retry checkpoint may or may not have been accepted; do not resume blindly."""
class ExportRetryDeferred(ManualExportReview):
    """A transient preflight block; retry no earlier than ``retry_after_utc``."""
    def __init__(self, message, *, retry_after_utc=None):
        super().__init__(message)
        self.retry_after_utc = retry_after_utc
class ExportRetryExhausted(ExportRetryDeferred):
    """Today's per-execution read budget is spent; retry on the next UTC day.

    This is a transient daily cap, not a permanent manual hold. The lifetime
    archive and all prior financial obligations are retained unchanged.
    """

def _fail(message):
    raise CacheError(message)
def _hex64(value, label):
    if not isinstance(value, str) or not _HEX64.match(value):
        _fail(f"{label} must be a lowercase 64-hex digest, got {value!r}")
def _scalar(value):
    return value is None or isinstance(value, (str, bool, int)) or (isinstance(value, float) and math.isfinite(value))
def _instant(value, label):
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty exact UTC instant string")
    text = value.strip()
    if "T" not in text:
        _fail(f"{label} must carry an exact time component, not a date-only value: {value!r}")
    try:
        return parse_instant(text, label)
    except BudgetInputError as error:
        _fail(str(error))
def _utc_text(value):
    return value.isoformat().replace("+00:00", "Z")
def _delay(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or value < 0):
        _fail(f"{label} must be a finite nonnegative number of seconds")
    return float(value)
def _attempt_count(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{label} must be a positive integer")
    return value
def _positive_quote(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or value <= 0):
        _fail(f"{label} must be a finite positive number")
    return float(value)
def _risk_block(risk_credits, evidence, timestamp, label):
    if not (isinstance(risk_credits, (int, float)) and not isinstance(risk_credits, bool) and math.isfinite(float(risk_credits)) and risk_credits >= 0):
        _fail(f"{label} risk_credits must be a finite non-negative caller estimate, never computed here")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) or not item for item in evidence) or not isinstance(timestamp, str) or not timestamp:
        _fail(f"{label} evidence must cite samples (non-empty string list) and timestamp must be a non-empty string")
def _rows_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
def _binding_fields(spec, record):
    if not isinstance(spec, dict) or set(spec) != _SPEC_KEYS:
        _fail(f"spec must carry exactly {sorted(_SPEC_KEYS)}")
    query_id, sql_sha256, parameters, columns = spec["query_id"], spec["sql_sha256"], spec["parameters"], spec["columns"]
    if not isinstance(query_id, int) or isinstance(query_id, bool) or query_id <= 0:
        _fail(f"spec.query_id must be a positive integer, got {query_id!r}")
    _hex64(sql_sha256, "spec.sql_sha256")
    if not isinstance(columns, list) or not columns or len(columns) > 64 or any(not isinstance(n, str) or not n or len(n) > 128 for n in columns) or len(set(columns)) != len(columns):
        _fail("spec.columns must be unique non-empty column names")
    if parameters is not None:
        if not isinstance(parameters, dict) or len(parameters) > 64:
            _fail("spec.parameters must be an object with <=64 entries")
        for name, value in parameters.items():
            if not isinstance(name, str) or not name or len(name) > 128 or any(word in name.lower() for word in _CREDENTIALISH) or not _scalar(value):
                _fail(f"spec.parameters key {name!r} invalid, non-scalar, or credential-like")
    execution_id = record.get("execution_id") if isinstance(record, dict) else None
    if execution_id is not None and (not isinstance(execution_id, str) or not execution_id):
        _fail("record.execution_id must be a non-empty string or absent")
    if execution_id is None:
        _fail("Exact execution id is required before any result export")
    return {"parameters": parameters, "query_id": query_id, "sql_sha256": sql_sha256,
            "execution_id": execution_id}
def _binding_key(fields):
    return hashlib.sha256(("v1\n" + json.dumps(fields, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
def _row_set_check(rows, columns, label):
    if not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != set(columns) or not all(_scalar(value) for value in row.values()) for row in rows):
        _fail(f"{label} must be a list of rows carrying exactly the given scalar columns")
class ResultCache:
    def __init__(self, root, checkpoint, *, allowed_query_ids):
        if not isinstance(root, (str, Path)) or not callable(checkpoint):
            _fail("ResultCache(root, checkpoint) needs a caller bridge")
        self.root = Path(root)
        self._checkpoint = checkpoint
        self.allowed_query_ids = frozenset(allowed_query_ids)
        if not self.allowed_query_ids or any(not isinstance(q, int) or isinstance(q, bool) or q <= 0 for q in self.allowed_query_ids):
            _fail("allowed_query_ids must be a non-empty set of positive integers")
    def lookup(self, state, record, spec):
        """Spend-free lookup; NEVER records an intent or permits another read."""
        fields = _binding_fields(spec, record)
        key = _binding_key(fields)
        records = self._load_records()
        _, requests = self._state_lane(state)
        marker = (requests or {}).get(key)
        if key in records:
            if records[key]["columns"] != spec["columns"]:
                _fail("Cached column contract changed")
            return records[key]
        if marker and marker["status"] != "consumed":
            raise ManualExportReview("Prior export is unresolved or cached bytes are missing; no repeat download")
        return None
    def _load_records(self):
        if not (self.root / CACHE_PATH_RELATIVE).exists():
            return {}
        with (self.root / CACHE_PATH_RELATIVE).open("rb") as handle:
            raw = handle.read(MAX_CACHE_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_CACHE_PAYLOAD_BYTES:
            _fail(f"cache file is {len(raw)} bytes, over the byte bound")
        try:
            payload = json.loads(raw.decode("utf-8"), parse_constant=lambda _c: _fail("cache file carries NaN/Infinity"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            _fail(f"cache file is not readable JSON: {error}")
        records = payload.get("records") if isinstance(payload, dict) and set(payload) == _FILE_KEYS and payload.get("schema_version") == CACHE_SCHEMA_VERSION else None
        if not isinstance(records, dict) or len(records) > MAX_CACHE_RECORDS:
            _fail(f"cache file must be schema {CACHE_SCHEMA_VERSION} with at most {MAX_CACHE_RECORDS} records under {sorted(_FILE_KEYS)}")
        for key, entry in records.items():
            self._validate_entry(key, entry)
        return records

    def _validate_entry(self, key, entry):
        _hex64(key, "cache entry key")
        if not isinstance(entry, dict) or set(entry) != _ENTRY_KEYS:
            _fail(f"cache entry {key} must carry exactly {sorted(_ENTRY_KEYS)}; unexpected columns are rejected")
        if entry["key"] != key or entry["schema_version"] != CACHE_SCHEMA_VERSION or entry["query_id"] not in self.allowed_query_ids:
            _fail(f"cache entry {key} identity/schema/allowlist mismatch")
        if not isinstance(entry["execution_id"], str) or not entry["execution_id"]:
            _fail(f"cache entry {key} must name the completed execution id")
        fields = _binding_fields({"query_id": entry["query_id"], "sql_sha256": entry["sql_sha256"], "parameters": entry["parameters"], "columns": entry["columns"]}, {"execution_id": entry["execution_id"]})
        if _binding_key(fields) != key:
            _fail(f"cache entry {key} binding does not match its metadata")
        if not isinstance(entry["stored_at_utc"], str) or not entry["stored_at_utc"]:
            _fail(f"cache entry {key} needs a stored_at_utc timestamp")
        rows = entry["result_rows"]
        if not isinstance(rows, list) or len(rows) != entry["total_row_count"] or entry["total_row_count"] > MAX_EXPORT_ROWS:
            _fail(f"cache entry {key} row count must equal its stored complete result (no pagination)")
        _row_set_check(rows, entry["columns"], f"cache entry {key}")
        if _rows_digest(rows) != entry["result_sha256"]:
            _fail(f"cache entry {key} checksum does not verify; the cached entry is tampered or corrupted")
    def _state_lane(self, state):
        if not isinstance(state, dict):
            _fail("state must be a dict")
        lane = state.get("result_exports")
        if lane is None:
            return None, None
        if not isinstance(lane, dict) or set(lane) != _LANE_KEYS or lane.get("schema_version") != CACHE_SCHEMA_VERSION or not isinstance(lane.get("requests"), dict):
            _fail("state.result_exports lane is malformed")
        for key, marker in lane["requests"].items():
            _hex64(key, f"result_exports.requests[{key}] key")
            if not isinstance(marker, dict) or set(marker) != _MARKER_KEYS:
                _fail(f"result_exports.requests[{key}] must carry exactly {sorted(_MARKER_KEYS)}")
            if marker["key"] != key or marker["status"] not in CACHE_STATUSES:
                _fail(f"result_exports.requests[{key}] identity/status bad")
            _risk_block(marker["risk_credits"], marker["evidence"], marker["requested_at_utc"], f"result_exports.requests[{key}]")
        return lane, lane["requests"]
    def _validate_result(self, marker, result):
        if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
            _fail(f"result must carry exactly {sorted(_RESULT_KEYS)}")
        execution_id = result["execution_id"]
        if not isinstance(execution_id, str) or not execution_id or marker["execution_id"] not in (None, execution_id):
            _fail("result.execution_id must identify the exact requested record's execution")
        rows = result["result_rows"]
        if not isinstance(rows, list) or len(rows) != result["total_row_count"] or result["total_row_count"] > MAX_EXPORT_ROWS:
            _fail(f"result must be complete: rows {len(rows)} vs declared total_row_count {result['total_row_count']!r}; pagination is refused")
        _row_set_check(rows, marker["columns"], "result")
    def _approval(self, approval, label):
        """Validate the caller's exact standing approval structure and instants."""
        if not isinstance(approval, dict):
            _fail(f"{label} approval must be an object")
        missing = _APPROVAL_KEYS - set(approval)
        if missing:
            _fail(f"{label} approval is missing {sorted(missing)}")
        operation_id = approval["operation_id"]
        if not isinstance(operation_id, str) or not operation_id.strip():
            _fail(f"{label} approval operation_id must be a nonempty string")
        if approval["kind"] != "export":
            _fail(f"{label} approval kind must be 'export', got {approval['kind']!r}")
        query_id = approval["query_id"]
        if not isinstance(query_id, int) or isinstance(query_id, bool) or query_id <= 0:
            _fail(f"{label} approval query_id must be a positive integer")
        parameters = approval["query_parameters"]
        if not isinstance(parameters, dict):
            _fail(f"{label} approval query_parameters must be an object")
        execution_id = approval["execution_id"]
        if not isinstance(execution_id, str) or not execution_id:
            _fail(f"{label} approval execution_id must be a nonempty string")
        quote = _positive_quote(approval["quote_credits"], f"{label} approval quote_credits")
        approved = _instant(approval["approved_at_utc"], f"{label} approval approved_at_utc")
        valid_until = _instant(approval["valid_until_utc"], f"{label} approval valid_until_utc")
        if approved >= valid_until:
            _fail(f"{label} approval valid_until_utc must be after approved_at_utc")
        if "performance" in approval and (not isinstance(approval["performance"], str)
                                          or not approval["performance"].strip()):
            _fail(f"{label} approval performance must be a nonempty string when present")
        return {"operation_id": operation_id, "query_id": query_id,
                "parameters": parameters, "execution_id": execution_id,
                "quote": quote, "approved": approved, "valid_until": valid_until}

    def _approval_binding(self, parsed, *, query_id, parameters, execution_id, label):
        expected_parameters = {} if parameters is None else parameters
        if (parsed["query_id"] != query_id
                or parsed["parameters"] != expected_parameters
                or parsed["execution_id"] != execution_id):
            _fail(f"{label} approval does not identify the exact query/parameters/execution binding")

    def _validate_retry_lane(self, state):
        """Validate the permanent retry archive and return its operation ids.

        Archives are never trimmed. Each archived approval re-identifies its old
        marker and operation, while its positive quote remains the new read's
        own estimate and is never required to equal the archived old risk.
        """
        lane = state.get(RETRY_LANE)
        if lane is None:
            return {}, set()
        if not isinstance(lane, dict):
            _fail(f"state.{RETRY_LANE} must be an object keyed by exact binding")
        operations = set()
        for key, entries in lane.items():
            _hex64(key, f"{RETRY_LANE}[{key}] key")
            if not isinstance(entries, list):
                _fail(f"{RETRY_LANE}[{key}] must be a list of archived retries")
            for index, entry in enumerate(entries):
                label = f"{RETRY_LANE}[{key}][{index}]"
                if not isinstance(entry, dict) or set(entry) != _ARCHIVE_KEYS:
                    _fail(f"{label} must carry exactly {sorted(_ARCHIVE_KEYS)}")
                marker = entry["marker"]
                if not isinstance(marker, dict) or set(marker) != _MARKER_KEYS:
                    _fail(f"{label}.marker must carry exactly {sorted(_MARKER_KEYS)}")
                if marker["key"] != key or marker["status"] not in _RETRYABLE_STATUSES:
                    _fail(f"{label}.marker identity/status is not retryable")
                _risk_block(marker["risk_credits"], marker["evidence"],
                            marker["requested_at_utc"], f"{label}.marker")
                fields = _binding_fields(
                    {"query_id": marker["query_id"], "sql_sha256": marker["sql_sha256"],
                     "parameters": marker["parameters"], "columns": marker["columns"]},
                    {"execution_id": marker["execution_id"]},
                )
                if _binding_key(fields) != key:
                    _fail(f"{label}.marker binding does not re-derive its archive key")
                if marker["status"] == "cached" and marker["result_sha256"] is None:
                    _fail(f"{label}.marker is cached without the prior result digest")
                if marker["result_sha256"] is not None:
                    _hex64(marker["result_sha256"], f"{label}.marker.result_sha256")
                parsed = self._approval(entry["approval"], label)
                self._approval_binding(parsed,
                                       query_id=marker["query_id"], parameters=marker["parameters"],
                                       execution_id=marker["execution_id"], label=label)
                if parsed["operation_id"] in operations:
                    _fail(f"{label} reuses an operation id already archived in the retry lane")
                operations.add(parsed["operation_id"])
        return lane, operations

    def _archived_expected_digest(self, state, key):
        """Prior cached digest recorded by a recovery archive, if any."""
        lane, _ = self._validate_retry_lane(state)
        digests = {entry["marker"]["result_sha256"] for entry in lane.get(key, [])
                   if entry["marker"]["result_sha256"] is not None}
        if len(digests) > 1:
            _fail(f"archived retries for {key} disagree about the prior cached digest")
        return next(iter(digests), None)

    def retry_export(self, state, record, spec, *, approval, timestamp,
                     minimum_delay_seconds=900, max_attempts=3):
        """Authorize ONE bounded re-read of this exact binding; never re-execute.

        The caller has already reserved standing risk and trust-verified the
        approval. This method validates the exact binding and the new read's
        positive quote; the archived old marker risk is never rewritten or used
        as the new quote. At most ``max_attempts`` read attempts are allowed per
        exact execution per UTC day (original plus retries that day), while the
        lifetime archive and every prior financial obligation remain unbounded
        and unaltered. A daily cap raises ``ExportRetryExhausted`` with
        ``retry_after_utc`` set to the next UTC day; it is not a permanent hold.
        The marker+approval are archived and only this binding's marker is
        removed. A failed checkpoint raises ``CheckpointFailed`` (a
        ``StateError``) after preserving the candidate in memory; a possibly
        accepted checkpoint is never rolled back. External monthly/daily ledger
        bounds remain the caller's responsibility; this method resets only the
        per-day read-attempt count and never deletes or replenishes ledger costs.
        ``approval["performance"]`` is optional; when present it must be a
        non-empty string. Returns None (a cached entry is left untouched)."""
        delay = _delay(minimum_delay_seconds, "minimum_delay_seconds")
        attempts = _attempt_count(max_attempts, "max_attempts")
        now = _instant(timestamp, "retry timestamp")
        fields = _binding_fields(spec, record)
        if fields["query_id"] not in self.allowed_query_ids:
            _fail(f"query id {fields['query_id']} is not on the allowed public onchain set")
        if not isinstance(state, dict):
            _fail("state must be a dict")
        key = _binding_key(fields)
        records = self._load_records()
        _, requests = self._state_lane(state)
        requests = requests or {}
        if key in records:
            if records[key]["columns"] != spec["columns"]:
                _fail("Cached result does not match the exact column contract")
            return None  # already recovered/reused; no marker change, no commit
        marker = requests.get(key)
        if marker is None:
            raise ManualExportReview(
                "no unresolved result export marker for this exact binding; "
                "retries are never inferred or retired by age")
        if marker["status"] == "consumed":
            raise ManualExportReview(
                f"marker {key} is consumed; there is no unresolved read to retry")
        if marker["status"] not in _RETRYABLE_STATUSES:
            raise ManualExportReview(
                f"marker {key} status {marker['status']!r} is not retryable")
        if (marker["query_id"] != fields["query_id"]
                or marker["sql_sha256"] != fields["sql_sha256"]
                or marker["parameters"] != fields["parameters"]
                or marker["columns"] != spec["columns"]
                or marker["execution_id"] != fields["execution_id"]):
            _fail(f"result export marker {key} identity does not match the exact binding")
        if marker["status"] == "cached" and marker["result_sha256"] is None:
            _fail(f"cached marker {key} has no prior result digest; recovery cannot preserve it")

        retry_lane, operations = self._validate_retry_lane(state)
        archived = retry_lane.get(key, [])
        requested = _instant(marker["requested_at_utc"], "marker requested_at_utc")
        if now < requested:
            _fail(f"retry timestamp {timestamp!r} precedes the original export attempt "
                  f"{marker['requested_at_utc']!r}")

        # Attempts are counted per exact execution per UTC day. Every archived
        # marker is one past read attempt, and the current marker is the latest
        # (not yet archived) attempt; the retry authorization instant is not a
        # separate read and never adds to the count.
        attempt_times = [requested]
        for entry in archived:
            attempt_times.append(_instant(entry["marker"]["requested_at_utc"],
                                          "archived requested_at_utc"))
        today = now.date()
        attempts_today = sum(1 for instant in attempt_times if instant.date() == today)
        if attempts_today >= attempts:
            next_day = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            retry_after = _utc_text(next_day)
            raise ExportRetryExhausted(
                f"daily retry budget exhausted for {key}: {attempts_today} read attempt(s) "
                f"already on {today.isoformat()} and max_attempts={attempts} per UTC day; "
                f"retry after {retry_after} (old obligations remain held)",
                retry_after_utc=retry_after)

        parsed = self._approval(approval, "retry_export")
        self._approval_binding(parsed, query_id=fields["query_id"],
                               parameters=fields["parameters"],
                               execution_id=fields["execution_id"], label="retry_export")
        if now < parsed["approved"]:
            _fail(f"retry approval is not valid yet: approved {_utc_text(parsed['approved'])}, "
                  f"retry {timestamp!r}")
        if now >= parsed["valid_until"]:
            _fail(f"retry approval expired at {_utc_text(parsed['valid_until'])}; "
                  f"retry {timestamp!r} is too late")
        if parsed["operation_id"] in operations:
            _fail(f"operation id {parsed['operation_id']!r} was already used by an archived "
                  "recovery; a new approved operation is required")

        elapsed = (now - requested).total_seconds()
        if elapsed < delay:
            retry_after = _utc_text(requested + timedelta(seconds=delay))
            raise ExportRetryDeferred(
                f"recovery delay not elapsed for {key}: {elapsed:.0f}s < {delay:.0f}s; "
                f"retry after {retry_after}, no manual review required",
                retry_after_utc=retry_after)

        try:
            synchronize_financial_journal(state)
        except BudgetInputError as error:
            _fail(f"financial journal synchronization refused the retry: {error}")
        state.setdefault(RETRY_LANE, {}).setdefault(key, []).append(
            {"marker": deepcopy(marker), "approval": deepcopy(approval)})
        del state["result_exports"]["requests"][key]
        # The in-memory candidate (journal sync, archive, marker removal) is now
        # the only safe representation. If the checkpoint is accepted but raises,
        # rolling back would restore a stale marker and permit duplicate reads.
        try:
            self._checkpoint(state)
        except CheckpointFailed:
            raise
        except Exception as error:
            raise CheckpointFailed(
                f"result retry checkpoint outcome is ambiguous for {key}; state may have "
                f"been accepted, so no transport is safe: {error}") from error
        return None

    def prepare_export(self, state, record, spec, *, risk_credits, evidence, timestamp, isolate_unrelated=False):
        if not isinstance(isolate_unrelated, bool):
            _fail("prepare_export isolate_unrelated must be a boolean")
        fields = _binding_fields(spec, record)
        if fields["query_id"] not in self.allowed_query_ids:
            _fail(f"query id {fields['query_id']} is not on the allowed public onchain set")
        _risk_block(risk_credits, evidence, timestamp, "prepare_export")
        key = _binding_key(fields)
        anchor = record.get("execution_id") if isinstance(record, dict) else None
        records = self._load_records()
        lane, requests = self._state_lane(state)
        requests = requests or {}
        if key in records:
            if records[key]["execution_id"] != anchor or records[key]["columns"] != spec["columns"]:
                _fail("Cached result does not match the exact execution/column contract")
            return records[key]
        outstanding = [k for k, marker in requests.items()
                       if marker["status"] in OUTSTANDING_RESULT_EXPORT_STATUSES
                       and not (isolate_unrelated and k != key)]
        if outstanding:
            raise ManualExportReview(f"prior requested/in_progress/unknown result export intent {sorted(outstanding)} blocks a new export; resolve deliberately (never retired by age)")
        if key in requests and requests[key]["status"] == "cached":
            raise ManualExportReview(f"marker {key} is cached but its cache entry is missing; manual review required, no second export")
        if lane is None:
            lane = {"schema_version": CACHE_SCHEMA_VERSION, "requests": {}}
        requests = lane["requests"]
        requests[key] = {"key": key, "query_id": fields["query_id"], "sql_sha256": fields["sql_sha256"], "parameters": fields["parameters"], "columns": spec["columns"], "execution_id": anchor, "status": "requested", "requested_at_utc": timestamp, "risk_credits": float(risk_credits), "evidence": list(evidence), "result_sha256": None, "total_row_count": None}
        state["result_exports"] = lane
        self._checkpoint(state)
        return None
    def cache_result(self, state, record, spec, result, *, timestamp):
        fields = _binding_fields(spec, record)
        key = _binding_key(fields)
        if not isinstance(timestamp, str) or not timestamp:
            _fail("cache_result timestamp must be a non-empty string")
        lane, requests = self._state_lane(state)
        marker = (requests or {}).get(key)
        if marker is None:
            _fail(f"no request intent for {key}; prepare_export first")
        if marker["status"] not in OUTSTANDING_RESULT_EXPORT_STATUSES:
            _fail(f"intent {key} is {marker['status']!r}; only an outstanding attempt may stage a result")
        self._validate_result(marker, result)
        digest = _rows_digest(result["result_rows"])
        expected = self._archived_expected_digest(state, key)
        if expected is not None and expected != digest:
            _fail(f"recovered rows for {key} do not match the archived prior cached digest; "
                  "refusing to write different bytes for a reused execution")
        records = self._load_records()
        existing = records.get(key)
        if existing is not None and existing["result_sha256"] != digest:
            _fail(f"cache entry {key} already holds different rows")
        if existing is None and len(records) >= MAX_CACHE_RECORDS:
            _fail(f"cache holds {MAX_CACHE_RECORDS} entries; prune a consumed one deliberately, unconsumed entries are never evicted")
        entry = {"schema_version": CACHE_SCHEMA_VERSION, "key": key, "query_id": fields["query_id"], "sql_sha256": fields["sql_sha256"], "parameters": fields["parameters"], "columns": spec["columns"], "execution_id": result["execution_id"], "total_row_count": result["total_row_count"], "result_sha256": digest, "result_rows": result["result_rows"], "stored_at_utc": timestamp}
        self._validate_entry(key, entry)
        raw = encoded({"schema_version": CACHE_SCHEMA_VERSION, "records": {**records, key: entry}})
        if len(raw) > MAX_CACHE_PAYLOAD_BYTES:
            _fail(f"staged cache payload is {len(raw)} bytes, over the byte bound")
        atomic_write(self.root / CACHE_PATH_RELATIVE, raw)
        marker.update(status="cached", result_sha256=digest, total_row_count=result["total_row_count"])
        self._checkpoint(state)
        return entry
    def prune_consumed(self, state):
        lane, requests = self._state_lane(state)
        consumed = state.get("consumed_executions", {})
        if not isinstance(consumed, dict):
            _fail("state.consumed_executions must be an object of accepted identities")
        consumed_ids = {str(execution_id) for execution_id in consumed}
        records = self._load_records()
        remaining = {key: entry for key, entry in records.items() if entry["execution_id"] not in consumed_ids}
        changed = False
        for marker in (requests or {}).values():
            if marker["execution_id"] in consumed_ids and marker["status"] != "consumed":
                marker["status"] = "consumed"
                changed = True
        if len(remaining) == len(records) and not changed:
            return {"instruction": "nothing_to_prune", "dropped": 0}
        for key in set(records) - set(remaining):
            marker = (requests or {}).get(key)
            if marker is not None:
                marker["status"] = "consumed"
        atomic_write(self.root / CACHE_PATH_RELATIVE, encoded({"schema_version": CACHE_SCHEMA_VERSION, "records": remaining}))
        self._checkpoint(state)
        return {"instruction": "pruned", "dropped": len(records) - len(remaining)}
