"""Durable, single-flight Dune executions; status checks never fetch results."""
from copy import deepcopy
from datetime import datetime
import math

TERMINAL = {
    "QUERY_STATE_COMPLETED", "QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED",
    "QUERY_STATE_EXPIRED",
}


class ExecutionPending(RuntimeError):
    """An execution remains unresolved; a replacement must not be submitted."""


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
        pending_status = deepcopy(status)
        pending_status["execution_cost_credits"] = entry["execution_cost_credits"]
        if previous != entry or pending.get("terminal_status") != pending_status:
            if previous is not None:
                history[history.index(previous)] = entry
            else:
                history.append(entry)
            self.state["execution_history"] = history[-1000:]
            pending["terminal_status"] = pending_status
            self.checkpoint()
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
        age = (self.now() - datetime.fromisoformat(
            pending["submitted_at_utc"].replace("Z", "+00:00")
        )).total_seconds()
        if age >= pending["timeout_seconds"]:
            # Persist intent before the request. A rejected/ambiguous cancellation
            # never establishes completion; only a terminal status does.
            if not pending.get("cancellation_requested_at_utc"):
                pending["cancellation_requested_at_utc"] = self.stamp()
                self.checkpoint()
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
                    self.checkpoint()
            except Exception as error:
                errors[entry["execution_id"]] = str(error)
        return errors

    def acknowledge(self, spec, record):
        """Retire completed work only after its result has been safely applied."""
        key = self.key(spec)
        pending = self.pending().get(key)
        if pending and pending.get("execution_id") == record.get("execution_id"):
            del self.pending()[key]
            self.checkpoint()

    def execute(self, spec, params, enforce_cap, timeout_seconds):
        key = self.key(spec)
        pending = self.pending().get(key)
        if pending is None:
            billing = self.guard()
            pending = {
                "key": key, "query_id": spec["query_id"],
                "query_parameters": deepcopy(params or {}),
                "submitted_at_utc": self.stamp(), "timeout_seconds": timeout_seconds,
                "billing": billing, "execution_id": None,
            }
            self.pending()[key] = pending
            # If the POST response is lost or the runner dies, this intent blocks
            # automatic resubmission rather than guessing that nothing ran.
            self.checkpoint()
            body = {"performance": self.performance}
            if params:
                body["query_parameters"] = params
            try:
                execution = self.api(f"query/{spec['query_id']}/execute", body)
                pending["execution_id"] = execution["execution_id"]
                if not pending["execution_id"]:
                    raise RuntimeError("No execution ID returned")
            except Exception as error:
                pending["submission_error"] = str(error)
                self.checkpoint()
                raise ExecutionPending(
                    f"Query {spec['query_id']} submission is unresolved; replacement blocked: {error}"
                ) from error
            self.checkpoint()
        elif pending["query_id"] != spec["query_id"]:
            raise ExecutionPending("Query configuration changed with unresolved work; replacement blocked.")

        while True:
            try:
                status = pending.get("terminal_status") or self.check(pending)
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
            if status["state"] == "QUERY_STATE_CANCELLED" and pending.get("cancellation_requested_at_utc"):
                raise ExecutionPending(
                    f"Execution {pending['execution_id']} cancellation confirmed; "
                    "replacement deferred to a later scheduled run."
                )
            raise RuntimeError(f"Execution {pending['execution_id']}: {status['state']}; no automatic retry.")
        if cost is None:
            raise ExecutionPending(f"Execution {pending['execution_id']} has no cost metadata; replacement blocked.")
        if enforce_cap and float(cost) > spec["max_run_credits"]:
            self.acknowledge(spec, pending)
            raise RuntimeError(
                f"Query {spec['query_id']} cost {cost} exceeded {spec['max_run_credits']}; refresh paused."
            )
        record = {
            "query_id": pending["query_id"], "execution_id": pending["execution_id"],
            "execution_cost_credits": float(cost),
            "completed_at_utc": status.get("execution_ended_at") or self.stamp(),
            "execution_started_at": status.get("execution_started_at"),
            "execution_ended_at": status.get("execution_ended_at"),
            "query_parameters": deepcopy(pending["query_parameters"]),
            "result_metadata": status.get("result_metadata", {}), "billing": pending["billing"],
        }
        print(f"Query {spec['query_id']} execution {pending['execution_id']}: "
              f"{status['state']}, {float(cost):.6f} credits (limit {spec['max_run_credits']})", flush=True)
        return record
