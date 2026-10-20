"""Durable, single-flight Dune executions; status checks never fetch results."""
from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
import uuid
from v3_credit_budget import BudgetInputError, parse_instant, canon_parameters
from v3_financial_journal import (
    synchronize_financial_journal, reject_submission, amount, COMPUTE_TERMINAL_STATES as TERMINAL,
)

try:  # Authoritative-state error shared with the durable store.
    from v3_live_state_store import StateError
except ImportError:  # pragma: no cover - store shipped alongside tracker
    class StateError(RuntimeError):
        """Fallback if the shared live-state store is unavailable."""

CANCELED_STATES = {"QUERY_STATE_CANCELED", "QUERY_STATE_CANCELLED"}

HISTORY_TELEMETRY_FIELDS = (
    "result_row_count",
    "result_bytes",
    "export_points",
    "export_wire_bytes",
    "estimated_export_credits",
    "export_attempt_id",
    "export_requested_at_utc",
    "export_reused",
    "financial_history_id",
    "cost_outlier",
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
    """Post-run threshold exceeded; the unconsumed completed ID is retained."""


class CostBreakerOpen(ExecutionPending):
    """New source/shape spending needs cooldown and a fresh approved quote."""


class ExecutionFailed(RuntimeError):
    """A terminal engine failure that is safe to isolate to its source."""


class ExecutionTracker:
    def __init__(self, state, api, checkpoint, guard, now, sleep, performance):
        self.state, self.api, self.checkpoint = state, api, checkpoint
        self.guard, self.now, self.sleep = guard, now, sleep
        self.performance = performance
        self._checkpoint_error = None

    def stamp(self):
        return self.now().isoformat(timespec="seconds").replace("+00:00", "Z")

    def pending(self):
        return self.state.setdefault("pending_executions", {})

    def key(self, spec):
        return spec.get("key", str(spec["query_id"]))

    def _checkpoint(self):
        """Lifecycle durability barrier; failures are never API-retryable."""
        self._require_durable_session()
        try:
            synchronize_financial_journal(self.state)
            self.checkpoint()
        except CheckpointFailed as error:
            self._checkpoint_error = str(error)
            raise
        except Exception as error:
            self._checkpoint_error = str(error)
            raise CheckpointFailed(str(error)) from error

    def _require_durable_session(self):
        if self._checkpoint_error is not None:
            raise CheckpointFailed("Prior checkpoint failed; recover and rehydrate authoritative state: "
                                   + self._checkpoint_error)

    def _resolved_performance(self, spec):
        return spec.get("performance", self.performance)

    def _spec_identity(self, spec):
        return {
            "key": self.key(spec),
            "query_id": spec["query_id"],
            "performance": self._resolved_performance(spec),
        }

    def cost_breaker_status(self, spec, params=None):
        """Spend-free future-admission check. Never gates recovery of a known ID.

        Caller verifies the quote/approval out of band. A quote is an estimate,
        not a precharge cap, and never replaces guard() or operation approval.
        """
        breakers = self.state.get("cost_breakers", {})
        if not isinstance(breakers, dict):
            raise StateError("Malformed source cost breakers")
        affected = []
        for breaker in breakers.values():
            if not isinstance(breaker, dict):
                raise StateError("Malformed source cost breaker")
            if (breaker.get("source_key") == self.key(spec)
                    or (breaker.get("query_id") == spec["query_id"]
                        and breaker.get("performance") == self._resolved_performance(spec)
                        and breaker.get("shape_version") == spec.get("shape_version"))):
                affected.append(breaker)
        if not affected:
            return {"status": "clear", "blockers": [], "authorization_required": True}
        now = parse_instant(self.now(), "now")
        blockers = []
        for breaker in affected:
            if now < parse_instant(breaker.get("cooldown_until_utc"), "breaker cooldown"):
                blockers.append("cost_breaker_cooldown")
        quote = spec.get("cost_admission_quote")
        try:
            if not isinstance(quote, dict):
                raise BudgetInputError("No future quote")
            identity = {"query_id": spec["query_id"], "performance": self._resolved_performance(spec),
                        "shape_version": spec.get("shape_version")}
            if any(quote.get(k) != v for k, v in identity.items()):
                raise BudgetInputError("Quote shape/engine changed")
            if canon_parameters(quote.get("query_parameters")) != canon_parameters(params):
                raise BudgetInputError("Quote parameters changed")
            operation_id = spec.get("financial_attempt_id")
            if not valid_execution_id(operation_id) or quote.get("operation_id") != operation_id:
                raise BudgetInputError("Quote must bind the next financial attempt")
            amount(quote.get("cost_estimate_credits"), "future cost estimate")
            for field in ("estimate_evidence", "approval_ref"):
                if not isinstance(quote.get(field), str) or not quote[field].strip():
                    raise BudgetInputError("Missing future quote evidence/approval")
            quoted = parse_instant(quote.get("quoted_at_utc"), "quote time")
            expires = parse_instant(quote.get("valid_until_utc"), "quote expiry")
            if not quoted <= now < expires:
                raise BudgetInputError("Future quote is stale or not yet valid")
            reviewed = quote.get("outlier_execution_ids", [])
            if not isinstance(reviewed, list):
                raise BudgetInputError("Quote must address the observed outliers")
            for breaker in affected:
                if (breaker.get("execution_id") not in reviewed
                        or quoted < parse_instant(breaker.get("opened_at_utc"), "breaker opened")):
                    raise BudgetInputError("Quote predates or omits the cost outlier")
        except (BudgetInputError, TypeError):
            blockers.append("cost_breaker_fresh_operation_quote_required")
        return {"status": "blocked" if blockers else "ready_for_admission",
                "blockers": sorted(set(blockers)), "breakers": deepcopy(affected),
                "authorization_required": True, "estimate_is_hard_cap": False}

    def _record_cost_outlier(self, pending, spec, cost, threshold, recovery_policy=None):
        """Audit spent compute once; replay never restarts its finite cooldown."""
        outliers = self.state.setdefault("cost_outliers", {})
        breakers = self.state.setdefault("cost_breakers", {})
        if not isinstance(outliers, dict) or not isinstance(breakers, dict):
            raise StateError("Malformed cost outlier audit/breaker lane")
        execution_id = pending["execution_id"]
        audit = outliers.get(execution_id)
        if audit is None:
            cooldown = amount(spec.get("cost_breaker_cooldown_seconds", 1800), "cost breaker cooldown")
            if cooldown <= 0:
                raise BudgetInputError("Cost breaker cooldown must be positive and finite")
            try:
                until = self.now() + timedelta(seconds=cooldown)
            except OverflowError as error:
                raise BudgetInputError("Cost breaker cooldown is outside the clock range") from error
            audit = {"execution_id": execution_id, "source_key": pending["key"],
                     "query_id": pending["query_id"],
                     "query_parameters": deepcopy(pending["query_parameters"]),
                     "performance": (pending.get("spec_identity") or {}).get("performance"),
                     "shape_version": pending.get("shape_version"),
                     "observed_cost_credits": cost, "post_run_threshold_credits": threshold,
                     "detected_at_utc": self.stamp(), "threshold_is_precharge_cap": False,
                     "cooldown_until_utc": until.isoformat().replace("+00:00", "Z")}
            outliers[execution_id] = audit
            breakers[pending["key"]] = {key: deepcopy(audit[key]) for key in (
                "execution_id", "source_key", "query_id", "performance", "shape_version",
                "observed_cost_credits", "cooldown_until_utc")}
            breakers[pending["key"]]["opened_at_utc"] = audit["detected_at_utc"]
        elif not isinstance(audit, dict):
            raise StateError("Malformed completed cost outlier")
        audit["observed_cost_credits"] = max(audit["observed_cost_credits"], cost)
        if recovery_policy is not None:
            audit["recovery_policy"] = recovery_policy
            audit.setdefault("returned_for_validation_at_utc", self.stamp())
        pending["cost_outlier"] = deepcopy(audit)
        for entry in self.state.get("execution_history", []):
            if entry.get("execution_id") == execution_id:
                entry["cost_outlier"] = deepcopy(audit)
        # Even an unchanged replay crosses the barrier before returning work or
        # refusing it. Never let a failed prior publication look like acceptance.
        self._checkpoint()
        return deepcopy(audit)

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
            if isinstance(cost, bool):
                raise ExecutionPending("Invalid boolean execution cost; replacement blocked.")
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
            "performance": (pending.get("spec_identity") or {}).get("performance"),
            "shape_version": pending.get("shape_version"),
            "financial_attempt_id": pending.get("financial_attempt_id"),
        }
        history = self.state.setdefault("execution_history", [])
        previous = next((e for e in history if e["execution_id"] == entry["execution_id"]), None)
        if previous and cost is None:
            entry["execution_cost_credits"] = previous.get("execution_cost_credits")
        if previous:
            for field in ("performance", "shape_version", "financial_attempt_id"):
                if entry.get(field) is None and previous.get(field) is not None:
                    entry[field] = previous[field]
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
                # The financial lane is not the bounded estimator sample lane.
                # Copy ALL old obligations before discarding any telemetry.
                try:
                    synchronize_financial_journal(self.state)
                except Exception as error:
                    raise CheckpointFailed(str(error)) from error
                history.append(entry)
            self.state["execution_history"] = history[-1000:]
            pending["terminal_status"] = pending_status
            self._checkpoint()
        return True

    def check(self, pending, metadata_only=False):
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
        if metadata_only:
            return status  # A hold/reconciliation scan must never issue a POST.
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

    def reconcile(self, metadata_only=False):
        """Resolve/cancel existing work even when new spending is paused."""
        self._require_durable_session()
        errors = {}
        for key, pending in list(self.pending().items()):
            try:
                self.check(pending, metadata_only=metadata_only)
            except CheckpointFailed:
                raise
            except Exception as error:
                errors[key] = str(error)
        # Include costless obligations whose telemetry was already trimmed.
        # Persist legacy migration even if this run has no pending executions.
        if not any(self.state.get(name) for name in (
                "execution_history", "pending_executions", "result_exports", "financial_journal")):
            return errors  # A spend-free empty legacy journal needs no migration commit.
        if synchronize_financial_journal(self.state):
            self._checkpoint()
        candidates = {entry["execution_id"]: entry
                      for entry in self.state.get("execution_history", [])}
        for obligation in self.state["financial_journal"]["obligations"].values():
            if obligation.get("kind") == "execution" and obligation.get("execution_id"):
                candidates.setdefault(obligation["execution_id"], obligation)
        for entry in candidates.values():
            financial = self.state["financial_journal"]["obligations"].get(f"exec:{entry['execution_id']}", {})
            if financial.get("cost_credits") is not None:
                continue
            if entry.get("execution_cost_credits") is not None:
                continue
            try:
                status = self.api(f"execution/{entry['execution_id']}/status")
                cost = status.get("execution_cost_credits")
                if (status.get("state") in TERMINAL and cost is not None and not isinstance(cost, bool)
                        and math.isfinite(float(cost)) and float(cost) >= 0):
                    financial["cost_credits"] = float(cost)
                    financial["unresolved"] = False
                    if "liability_id" not in entry:
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
        self._require_durable_session()
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
                "export_attempt_id",
                "export_requested_at_utc",
                "export_reused",
            ):
                if field in record:
                    telemetry[field] = record[field]
            for field, value in telemetry.items():
                if entry.get(field) != value:
                    entry[field] = value
                    changed = True

        pending = self.pending().get(key)
        synchronize_financial_journal(self.state)
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
        self._require_durable_session()
        if type(spec.get("accept_completed_outlier", False)) is not bool:
            raise BudgetInputError("accept_completed_outlier must be a bool")
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
            breaker = self.cost_breaker_status(spec, params)
            if breaker["status"] == "blocked":
                raise CostBreakerOpen("New execution held: " + "; ".join(breaker["blockers"]))
            billing = self.guard()
            attempt_id = spec.get("financial_attempt_id") or uuid.uuid4().hex
            if not valid_execution_id(attempt_id):
                raise ExecutionPending("Invalid financial attempt identity; no submission")
            financial_id = "submission:" + attempt_id
            if any(row.get("submission_id") == financial_id or row.get("liability_id") == financial_id
                   for row in self.state.get("financial_journal", {}).get("obligations", {}).values()):
                raise ExecutionPending("Financial attempt already recorded; reconcile instead of resubmitting")
            pending = {
                "key": key, "query_id": spec["query_id"],
                "query_parameters": deepcopy(params or {}),
                # Reservation timestamps retain subsecond precision. Truncating
                # this intent to seconds could falsely put it BEFORE its own
                # already-durable reservation and deadlock allowance coverage.
                "submitted_at_utc": self.now().isoformat().replace("+00:00", "Z"),
                "timeout_seconds": timeout_seconds,
                "billing": billing, "execution_id": None,
                "spec_identity": self._spec_identity(spec),
                "financial_attempt_id": attempt_id,
                "shape_version": spec.get("shape_version"),
                "post_run_threshold_credits": spec.get("max_run_credits"),
                **({
                    "queue_timeout_seconds": int(spec["queue_timeout_seconds"]),
                    "run_timeout_seconds": int(spec["run_timeout_seconds"]),
                } if "queue_timeout_seconds" in spec and "run_timeout_seconds" in spec else {}),
            }
            # Only the admission boundary's verified evidence, never an unchecked
            # spec marker. Preserve the exact ordinary approval before the POST.
            for field in ("operation_approval", "operation_quote"):
                if isinstance(billing, dict) and field in billing:
                    pending[field] = deepcopy(billing[field])
            if breaker["status"] == "ready_for_admission":
                # Preserve the actual reviewed admission basis before POST, not
                # an arbitrary caller payload or a subsequently changed quote.
                pending["cost_admission_quote"] = {field: deepcopy(spec["cost_admission_quote"].get(field))
                    for field in ("operation_id", "query_id", "query_parameters", "performance", "shape_version",
                                  "cost_estimate_credits", "estimate_evidence", "approval_ref", "quoted_at_utc",
                                  "valid_until_utc", "outlier_execution_ids")}
            self.pending()[key] = pending
            # If the POST response is lost or the runner dies, this intent blocks
            # automatic resubmission rather than guessing that nothing ran.
            self._checkpoint()
            if spec.get("execution_not_after_utc") is not None and self.now() >= parse_instant(spec["execution_not_after_utc"], "execution scope deadline"):
                # This is authoritative local nonacceptance: no execute transport
                # has been invoked. Do not let a slow checkpoint shift a relative
                # UTC-day query across midnight after its approval was recorded.
                reject_submission(self.state, pending)
                self.state.setdefault("rejected_submissions", []).append({
                    "key":key,"query_id":spec["query_id"],"at_utc":self.stamp(),
                    "basis":"local_scope_deadline_before_transport"})
                self.state["rejected_submissions"] = self.state["rejected_submissions"][-50:]
                del self.pending()[key]
                self._checkpoint()
                raise SubmissionRejected("Immutable UTC-day deadline passed before transport; replan offsets")
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
                reject_submission(self.state, pending)
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

        if not valid_execution_id(pending.get("execution_id")):
            raise ExecutionPending(f"Query {pending['query_id']} has an unresolved submission without an ID "
                                   "or with an invalid ID; manual reconciliation required, replacement blocked.")
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
        threshold = pending.get("post_run_threshold_credits")
        threshold = amount(spec["max_run_credits"] if threshold is None else threshold, "post-run threshold")
        outlier = None
        prior_outliers = self.state.get("cost_outliers", {})
        if not isinstance(prior_outliers, dict):
            raise StateError("Malformed completed cost outlier audit")
        if float(cost) > threshold or pending.get("cost_outlier") or pending["execution_id"] in prior_outliers:
            policy = ("accept_completed_outlier" if spec.get("accept_completed_outlier", False)
                      else "cap_enforcement_disabled" if not enforce_cap else None)
            outlier = self._record_cost_outlier(pending, spec, float(cost), threshold, policy)
            threshold = outlier["post_run_threshold_credits"]
        if outlier and enforce_cap and not spec.get("accept_completed_outlier", False):
            raise ExecutionCostExceeded(
                f"Query {spec['query_id']} cost {cost} exceeded {threshold}; completed ID retained for explicit recovery."
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
            "financial_attempt_id": pending.get("financial_attempt_id"),
            "performance": (pending.get("spec_identity") or {}).get("performance"),
            "shape_version": pending.get("shape_version"),
        }
        if outlier:
            record["cost_outlier"] = {**outlier, "returned_for_validation": True,
                                      "recovery_policy": policy}
        print(f"Query {spec['query_id']} execution {pending['execution_id']}: "
              f"{status['state']}, {float(cost):.6f} credits (post-run threshold {threshold})", flush=True)
        return record
