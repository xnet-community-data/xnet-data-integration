"""Durable, single-flight Dune executions; status checks never fetch results."""
from copy import deepcopy
from datetime import datetime
import math
import re

try:  # Authoritative-state error shared with the durable store.
    from v3_live_state_store import StateError
except ImportError:  # pragma: no cover - store shipped alongside tracker
    class StateError(RuntimeError):
        """Fallback if the shared live-state store is unavailable."""

TERMINAL = {
    "QUERY_STATE_COMPLETED",
    "QUERY_STATE_COMPLETED_PARTIAL",
    "QUERY_STATE_FAILED",
    # Dune's current docs use CANCELED; older/live responses have also used
    # CANCELLED. Accept both so a spelling change can never strand work.
    "QUERY_STATE_CANCELED",
    "QUERY_STATE_CANCELLED",
    "QUERY_STATE_EXPIRED",
}

CANCELED_STATES = {"QUERY_STATE_CANCELED", "QUERY_STATE_CANCELLED"}

HISTORY_TELEMETRY_FIELDS = (
    "result_row_count",
    "result_bytes",
    "export_points",
    "export_wire_bytes",
    "estimated_export_credits",
)

# Dune execution IDs are ULIDs. We accept either a realistic ULID or a
# conservative opaque token so existing fixtures stay valid, but never
# whitespace, path separators, traversal, control characters or overlong input.
MAX_EXECUTION_ID_LENGTH = 128
_SAFE_EXECUTION_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")


def valid_execution_id(value):
    """Return True only for a non-empty, non-injecting execution ID."""
    if not isinstance(value, str) or not value:
        return False
    if len(value) > MAX_EXECUTION_ID_LENGTH:
        return False
    return _SAFE_EXECUTION_ID.fullmatch(value) is not None


def is_ulid(value):
    """True when the value is a syntactically realistic ULID."""
    return isinstance(value, str) and _ULID.fullmatch(value) is not None


class CheckpointFailed(StateError):
    """Durability barrier: a lifecycle checkpoint (state save/publication) failed.

    This must never be downgraded to a retryable API/status error by reconcile()
    or execute(); callers fail closed and leave durable intent for reconciliation.
    """


class ExecutionPending(RuntimeError):
    """An execution remains unresolved; a replacement must not be submitted."""


class ExecutionCancelled(ExecutionPending):
    """Cancellation confirmed; replacement may be deferred by a cooldown."""


class SubmissionRejected(RuntimeError):
    """A definitive 4xx execute rejection: Dune did not accept an execution."""


class ExecutionCostExceeded(RuntimeError):
    """A completed execution exceeded its permitted spending cap."""


class ExecutionFailed(RuntimeError):
    """A terminal engine failure that is safe to isolate to its source."""


class ExecutionTracker:
    def __init__(self, state, api, checkpoint, guard, now, sleep, performance):
        self.state, self.api, self.checkpoint = state, api, checkpoint
        self.guard, self.now, self.sleep = guard, now, sleep
        self.performance = performance

    def stamp(self):
        return self.now().isoformat(timespec="seconds").replace("+00:00", "Z")

    def pending(self):
        return self.state.setdefault("pending_executions", {})

    def key(self, spec):
        return spec.get("key", str(spec["query_id"]))

    def _checkpoint(self):
        """Lifecycle durability barrier; failures are never API-retryable."""
        try:
            self.checkpoint()
        except CheckpointFailed:
            raise
        except Exception as error:
            raise CheckpointFailed(str(error)) from error

    def _resolved_performance(self, spec):
        return spec.get("performance", self.performance)

    def _spec_identity(self, spec):
        return {
            "key": self.key(spec),
            "query_id": spec["query_id"],
            "performance": self._resolved_performance(spec),
        }

    def operation_evidence(self, pending):
        """Original operation identity/parameters bound to a live execution."""
        return {
            "key": pending.get("key"),
            "query_id": pending.get("query_id"),
            "execution_id": pending.get("execution_id"),
            "execution_id_format": pending.get("execution_id_format"),
            "submitted_at_utc": pending.get("submitted_at_utc"),
            "query_parameters": deepcopy(pending.get("query_parameters", {})),
            "spec_identity": deepcopy(pending.get("spec_identity")),
            "submission_error": pending.get("submission_error"),
        }

    def recovered_operations(self):
        """Expose original evidence for every unresolved/recovered operation."""
        return {key: self.operation_evidence(pending)
                for key, pending in self.pending().items()}

    def observe(self, pending, status):
        remote_state = status.get("state")
        if remote_state not in TERMINAL:
            if status.get("is_execution_finished"):
                raise ExecutionPending("Unrecognized terminal state; replacement blocked.")
            return False
        cost = status.get("execution_cost_credits")
        if cost is not None:
            cost = float(cost)
            if not math.isfinite(cost) or cost < 0:
                raise ExecutionPending("Invalid execution cost; replacement blocked.")
        entry = {
            "key": pending["key"], "query_id": pending["query_id"],
            "execution_id": pending["execution_id"],
            "submitted_at_utc": pending["submitted_at_utc"],
            "execution_started_at": status.get("execution_started_at"),
            "execution_ended_at": status.get("execution_ended_at"),
            "state": remote_state, "execution_cost_credits": cost,
            "query_parameters": pending["query_parameters"],
            "cancellation_requested_at_utc": pending.get("cancellation_requested_at_utc"),
        }
        history = self.state.setdefault("execution_history", [])
        previous = next((e for e in history if e["execution_id"] == entry["execution_id"]), None)
        if previous and cost is None:
            entry["execution_cost_credits"] = previous.get("execution_cost_credits")
        if previous:
            # Status reconciliation must not erase telemetry recorded after a
            # paid result export has been safely consumed.
            for field in HISTORY_TELEMETRY_FIELDS:
                if field in previous:
                    entry[field] = previous[field]
        pending_status = deepcopy(status)
        pending_status["execution_cost_credits"] = entry["execution_cost_credits"]
        if previous != entry or pending.get("terminal_status") != pending_status:
            if previous is not None:
                history[history.index(previous)] = entry
            else:
                history.append(entry)
            self.state["execution_history"] = history[-1000:]
            pending["terminal_status"] = pending_status
            self._checkpoint()
        return True

    def check(self, pending):
        cached = pending.get("terminal_status")
        if cached and cached.get("execution_cost_credits") is not None:
            return cached
        execution_id = pending.get("execution_id")
        if not execution_id:
            raise ExecutionPending(
                f"Query {pending['query_id']} has an unresolved submission without an ID; "
                "manual reconciliation required, replacement blocked."
            )
        status = self.api(f"execution/{execution_id}/status")
        if self.observe(pending, status):
            return status
        # Queueing and actual computation are separate budgets. Save the
        # engine-reported start to retain correct deadlines after restarts.
        remote_start = status.get("execution_started_at")
        if remote_start and not pending.get("execution_started_at"):
            pending["execution_started_at"] = remote_start
            self._checkpoint()
        started = pending.get("execution_started_at")
        submitted = datetime.fromisoformat(pending["submitted_at_utc"].replace("Z", "+00:00"))
        if "queue_timeout_seconds" in pending and "run_timeout_seconds" in pending:
            if started:
                since = datetime.fromisoformat(started.replace("Z", "+00:00"))
                budget = pending["run_timeout_seconds"]
            else:
                since = submitted
                budget = pending["queue_timeout_seconds"]
        else:
            since = submitted
            budget = pending["timeout_seconds"]
        age = (self.now() - since).total_seconds()
        if age >= budget:
            # Persist intent before the request. A rejected/ambiguous cancellation
            # never establishes completion; only a terminal status does.
            if not pending.get("cancellation_requested_at_utc"):
                pending["cancellation_requested_at_utc"] = self.stamp()
                self._checkpoint()
            self.api(f"execution/{execution_id}/cancel", {})
            status = self.api(f"execution/{execution_id}/status")
            self.observe(pending, status)
        return status

    def reconcile(self):
        """Resolve/cancel existing work even when new spending is paused."""
        errors = {}
        for key, pending in list(self.pending().items()):
            try:
                self.check(pending)
            except CheckpointFailed:
                raise
            except Exception as error:
                errors[key] = str(error)
        # Retry missing cost metadata without executions or result downloads.
        for entry in list(self.state.get("execution_history", [])):
            if entry.get("execution_cost_credits") is not None:
                continue
            try:
                status = self.api(f"execution/{entry['execution_id']}/status")
                cost = status.get("execution_cost_credits")
                if cost is not None and math.isfinite(float(cost)) and float(cost) >= 0:
                    entry["execution_cost_credits"] = float(cost)
                    self._checkpoint()
            except CheckpointFailed:
                raise
            except Exception as error:
                errors[entry["execution_id"]] = str(error)
        return errors

    def acknowledge(self, spec, record, checkpoint=True):
        """Retire completed work only after its result has been safely applied.

        With checkpoint=False the consumption marker is mutated in memory only;
        the caller must publish state and the execution's effects atomically
        (see stage_acknowledge/commit_checkpoint).
        """
        key = self.key(spec)
        execution_id = record.get("execution_id")
        changed = False

        # Persist compact result/export telemetry before retiring the execution.
        # Dune meters API result reads separately from compute, so compute-only
        # history is not enough to explain account-level credit usage.
        history = self.state.setdefault("execution_history", [])
        entry = next(
            (item for item in history if item.get("execution_id") == execution_id),
            None,
        )
        if entry is not None:
            meta = record.get("result_metadata") or {}
            telemetry = {
                "result_row_count": int(
                    meta.get("total_row_count", meta.get("row_count", 0)) or 0
                ),
                "result_bytes": int(
                    meta.get(
                        "total_result_set_bytes",
                        meta.get("result_set_bytes", 0),
                    )
                    or 0
                ),
            }
            for field in (
                "export_points",
                "export_wire_bytes",
                "estimated_export_credits",
            ):
                if field in record:
                    telemetry[field] = record[field]
            for field, value in telemetry.items():
                if entry.get(field) != value:
                    entry[field] = value
                    changed = True

        pending = self.pending().get(key)
        if pending and pending.get("execution_id") == execution_id:
            del self.pending()[key]
            changed = True

        if changed and checkpoint:
            self._checkpoint()
        return changed

    def stage_acknowledge(self, spec, record):
        """Atomically stage consumption without publishing.

        Removes the pending marker and records telemetry in memory. The caller
        must persist this state together with the accepted effects in a single
        publication, then call commit_checkpoint() (the durability barrier).
        """
        return self.acknowledge(spec, record, checkpoint=False)

    def commit_checkpoint(self):
        """Durability barrier for staged consumption; raises CheckpointFailed."""
        self._checkpoint()

    def execute(self, spec, params, enforce_cap, timeout_seconds):
        key = self.key(spec)
        pending = self.pending().get(key)
        if pending is None:
            # A lost/HTTP-5xx submission response may represent accepted work.
            # Block *all* new submissions while ANY query has an unknown ID,
            # rather than spending on more queries during an API outage.
            unknown = sorted(
                name for name, entry in self.pending().items()
                if not entry.get("execution_id")
            )
            if unknown:
                raise ExecutionPending(
                    "Unresolved Dune submission without execution ID "
                    f"({', '.join(unknown)}); all new submissions blocked "
                    "pending reviewed reconciliation."
                )
            billing = self.guard()
            pending = {
                "key": key, "query_id": spec["query_id"],
                "query_parameters": deepcopy(params or {}),
                "submitted_at_utc": self.stamp(), "timeout_seconds": timeout_seconds,
                "billing": billing, "execution_id": None,
                "spec_identity": self._spec_identity(spec),
                **({
                    "queue_timeout_seconds": int(spec["queue_timeout_seconds"]),
                    "run_timeout_seconds": int(spec["run_timeout_seconds"]),
                } if "queue_timeout_seconds" in spec and "run_timeout_seconds" in spec else {}),
            }
            self.pending()[key] = pending
            # If the POST response is lost or the runner dies, this intent blocks
            # automatic resubmission rather than guessing that nothing ran.
            self._checkpoint()
            body = {"performance": spec.get("performance", self.performance)}
            if params:
                body["query_parameters"] = params
            try:
                execution = self.api(f"query/{spec['query_id']}/execute", body)
                candidate = execution.get("execution_id") if isinstance(execution, dict) else None
                if not valid_execution_id(candidate):
                    raise RuntimeError(f"Execute returned an invalid execution ID: {candidate!r}")
                pending["execution_id"] = candidate
                pending["execution_id_format"] = "ulid" if is_ulid(candidate) else "opaque"
            except SubmissionRejected as error:
                # A confirmed nonacceptance (classification owned by the caller's
                # transport layer) did NOT submit work. Audit and release intent.
                self.state.setdefault("rejected_submissions", []).append({
                    "key": key, "query_id": spec["query_id"],
                    "at_utc": self.stamp(), "error": str(error),
                })
                self.state["rejected_submissions"] = self.state["rejected_submissions"][-50:]
                del self.pending()[key]
                self._checkpoint()
                raise
            except CheckpointFailed:
                raise
            except Exception as error:
                # Lost response, 5xx/network ambiguity, or an unusable ID: retain
                # the durable intent (no ID) rather than guessing nothing ran.
                pending["execution_id"] = None
                pending.pop("execution_id_format", None)
                pending["submission_error"] = str(error)
                self._checkpoint()
                raise ExecutionPending(
                    f"Query {spec['query_id']} submission is unresolved; replacement blocked: {error}"
                ) from error
            self._checkpoint()
        elif pending["query_id"] != spec["query_id"]:
            raise ExecutionPending(
                "Query id changed with unresolved work; replacement blocked; "
                "original operation evidence retained."
            )
        elif (
            "spec_identity" in pending
            and pending["spec_identity"] != self._spec_identity(spec)
        ):
            raise ExecutionPending(
                "Operation identity changed with unresolved work "
                f"(original {pending['spec_identity']!r}); replacement blocked; "
                "original parameters retained."
            )

        while True:
            try:
                status = pending.get("terminal_status") or self.check(pending)
            except CheckpointFailed:
                raise
            except ExecutionPending:
                raise
            except Exception as error:
                raise ExecutionPending(
                    f"Execution {pending.get('execution_id')} status/cancellation unresolved; "
                    f"replacement blocked: {error}"
                ) from error
            if status.get("state") in TERMINAL:
                break
            if pending.get("cancellation_requested_at_utc"):
                raise ExecutionPending(
                    f"Execution {pending['execution_id']} exceeded {pending['timeout_seconds']}s; "
                    "awaiting confirmed cancellation, replacement blocked."
                )
            self.sleep(4)

        cost = status.get("execution_cost_credits")
        if status["state"] != "QUERY_STATE_COMPLETED":
            # The terminal failure is now recorded, including unknown costs.
            self.acknowledge(spec, pending)
            if status["state"] in CANCELED_STATES and pending.get("cancellation_requested_at_utc"):
                raise ExecutionCancelled(
                    f"Execution {pending['execution_id']} cancellation confirmed; "
                    "replacement deferred to a later scheduled run."
                )
            raise ExecutionFailed(f"Execution {pending['execution_id']}: {status['state']}; no automatic retry.")
        if cost is None:
            raise ExecutionPending(f"Execution {pending['execution_id']} has no cost metadata; replacement blocked.")
        if enforce_cap and float(cost) > spec["max_run_credits"]:
            self.acknowledge(spec, pending)
            raise ExecutionCostExceeded(
                f"Query {spec['query_id']} cost {cost} exceeded {spec['max_run_credits']}; source quarantined."
            )
        record = {
            "query_id": pending["query_id"], "execution_id": pending["execution_id"],
            "execution_cost_credits": float(cost),
            "completed_at_utc": status.get("execution_ended_at") or self.stamp(),
            "execution_started_at": status.get("execution_started_at"),
            "execution_ended_at": status.get("execution_ended_at"),
            "query_parameters": deepcopy(pending["query_parameters"]),
            "spec_identity": deepcopy(pending.get("spec_identity")),
            "result_metadata": status.get("result_metadata", {}), "billing": pending["billing"],
        }
        print(f"Query {spec['query_id']} execution {pending['execution_id']}: "
              f"{status['state']}, {float(cost):.6f} credits (limit {spec['max_run_credits']})", flush=True)
        return record
