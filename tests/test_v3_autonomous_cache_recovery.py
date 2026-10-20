"""Autonomous result-read recovery contract for scripts/v3_result_cache.py.

Pins the bounded, approval-gated ``retry_export`` path, the opt-in
``prepare_export(isolate_unrelated=True)`` scope, permanent marker/approval
archives, financial-journal preservation, digest-bound cached-byte recovery,
and the unchanged default global hold.
"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v3_financial_journal import LANE as JOURNAL, export_attempt_id
from v3_live_state_store import StateError
from v3_result_cache import (
    RETRY_LANE,
    CacheError,
    CheckpointFailed,
    ExportRetryDeferred,
    ExportRetryExhausted,
    ManualExportReview,
    ResultCache,
)

HEXA = hashlib.sha256(b"SELECT 1").hexdigest()
RISK, EV, AT = 0.5, ["sampled 3/1 rows against Explorer"], "2026-10-18T12:00:00Z"
BASE = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
EXEC_A, EXEC_B, EXEC_C = "01ARZ3NDEKTSV4RR", "01ARZ3NDEKTSV4RX", "01ARZ3NDEKTSV4RY"


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def spec(qid=8894037, params=None, cols=None, sql_hex=HEXA):
    return {"query_id": qid, "sql_sha256": sql_hex,
            "parameters": {"a": 1} if params is None else params,
            "columns": ["a_b", "day"] if cols is None else cols}


def record(execution_id=EXEC_A):
    return {"execution_id": execution_id}


def result(execution_id=EXEC_A, rows=None):
    rows = [{"a_b": 1, "day": "2026-01-01"}] if rows is None else rows
    return {"execution_id": execution_id, "total_row_count": len(rows), "result_rows": rows}


def rows_of(execution_id):
    tag = execution_id[-1]
    return [{"a_b": f"row-{tag}", "day": f"day-{tag}"}]


def approval(operation_id="op-export-retry-1", *, quote=RISK, approved_at=AT,
             valid_until=at(86_400), kind="export", query_id=8894037, params=None,
             execution_id=EXEC_A, include_performance=True):
    payload = {
        "operation_id": operation_id,
        "approval_ref": "standing-export-recovery-approval",
        "kind": kind,
        "query_id": query_id,
        "query_parameters": {"a": 1} if params is None else params,
        "execution_id": execution_id,
        "quote_credits": quote,
        "approved_at_utc": approved_at,
        "valid_until_utc": valid_until,
        "estimate_evidence": "sample-backed export wire risk",
    }
    if include_performance:
        payload["performance"] = "medium"
    return payload


class Bridge:
    def __init__(self, crash_on=None):
        self.calls = 0
        self.snapshots = []
        self.crash_on = crash_on
        self.crash = False

    def __call__(self, state):
        self.calls += 1
        self.snapshots.append(copy.deepcopy(state))
        if self.crash_on is not None and self.calls == self.crash_on:
            raise RuntimeError("process died mid-commit")


def mk(root, crash_on=None):
    bridge = Bridge(crash_on)
    return ResultCache(root, bridge, allowed_query_ids={8894037}), bridge


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "data" / "current").mkdir(parents=True)

    def file(self):
        return self.root / "data" / "current" / "v3_result_cache.json"

    def records(self):
        return json.loads(self.file().read_text())["records"]

    def markers(self, state):
        return state["result_exports"]["requests"]

    def stage(self, *, risk=RISK, execution_id=EXEC_A, crash_on=None, params=None):
        state = {}
        cache, bridge = mk(self.root, crash_on)
        cache.prepare_export(state, record(execution_id), spec(params=params),
                             risk_credits=risk, evidence=EV, timestamp=AT)
        return state, cache, bridge

    def cached(self, state, cache, execution_id=EXEC_A, params=None):
        return cache.cache_result(state, record(execution_id), spec(params=params),
                                  result(execution_id, rows=rows_of(execution_id)),
                                  timestamp=AT)

    def retry(self, cache, state, *, operation_id, timestamp, spec_value=None,
              approval_value=None):
        return cache.retry_export(
            state, record(), spec_value or spec(),
            approval=approval_value or approval(operation_id),
            timestamp=timestamp,
        )


class DefaultBehaviorTests(Base):
    def test_default_global_hold_is_preserved(self):
        state, cache, bridge = self.stage()
        before = bridge.calls
        for explicit in (False,):
            with self.subTest(isolate_unrelated=explicit):
                with self.assertRaises(ManualExportReview):
                    cache.prepare_export(
                        state, record(EXEC_B), spec(params={"a": 2}),
                        risk_credits=RISK, evidence=EV, timestamp=AT,
                        isolate_unrelated=explicit)
        self.assertEqual(bridge.calls, before)
        self.assertEqual(len(self.markers(state)), 1)

    def test_isolate_unrelated_skips_other_bindings_but_not_own_marker(self):
        state, cache, bridge = self.stage()
        self.assertIsNone(cache.prepare_export(
            state, record(EXEC_B), spec(params={"a": 2}),
            risk_credits=RISK, evidence=EV, timestamp=AT, isolate_unrelated=True))
        self.assertEqual(len(self.markers(state)), 2)
        with self.assertRaises(ManualExportReview):
            cache.prepare_export(
                state, record(EXEC_A), spec(params={"a": 1}),
                risk_credits=RISK, evidence=EV, timestamp=AT, isolate_unrelated=True)
        with self.assertRaises(ManualExportReview):
            cache.prepare_export(
                state, record(EXEC_C), spec(params={"a": 3}),
                risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual(bridge.calls, 2)

    def test_isolate_unrelated_must_be_a_boolean(self):
        cache, bridge = mk(self.root)
        state = {}
        for value in ("yes", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(CacheError):
                    cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                                         evidence=EV, timestamp=AT,
                                         isolate_unrelated=value)
        self.assertEqual(state, {})
        self.assertEqual(bridge.calls, 0)


class RetryAuthorizationTests(Base):
    def test_retry_archives_full_marker_and_checkpoints_before_fetch(self):
        state, cache, bridge = self.stage()
        original = copy.deepcopy(next(iter(self.markers(state).values())))
        key = original["key"]
        approval_value = approval()
        self.assertIsNone(cache.retry_export(
            state, record(), spec(), approval=approval_value, timestamp=at(900)))
        self.assertEqual(self.markers(state), {})
        entry = state[RETRY_LANE][key][0]
        self.assertEqual(set(entry), {"marker", "approval"})
        self.assertEqual(entry["marker"], original)  # full original marker, never trimmed
        self.assertEqual(entry["approval"], approval_value)  # exact new approval
        self.assertEqual(bridge.calls, 2)
        committed = bridge.snapshots[-1]
        self.assertEqual(committed[RETRY_LANE][key][0]["marker"], original)
        self.assertNotIn(key, committed["result_exports"]["requests"])
        self.assertFalse(self.file().exists())  # a retry never downloads or writes rows

    def test_retry_preserves_the_original_financial_obligation_unknown(self):
        state, cache, _ = self.stage()
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        row = state[JOURNAL]["obligations"][export_attempt_id(key, AT)]
        self.assertIsNone(row["cost_credits"])
        self.assertIs(row["unresolved"], True)
        self.assertEqual(row["reserved_credits"], RISK)
        self.assertEqual(row["cache_status"], "requested")
        self.assertEqual(row["requested_at_utc"], AT)

    def test_after_retry_prepare_export_stages_a_new_distinct_attempt(self):
        state, cache, _ = self.stage()
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        archive = copy.deepcopy(state[RETRY_LANE])
        self.assertIsNone(cache.prepare_export(
            state, record(), spec(), risk_credits=0.75, evidence=["new sample evidence"],
            timestamp=at(1_000)))
        marker = self.markers(state)[key]
        self.assertEqual(marker["status"], "requested")
        self.assertEqual(marker["requested_at_utc"], at(1_000))
        self.assertEqual(marker["risk_credits"], 0.75)
        self.assertEqual(marker["evidence"], ["new sample evidence"])
        self.assertEqual(state[RETRY_LANE], archive)  # archives are append-only telemetry

    def test_cached_entry_returns_no_change(self):
        state, cache, bridge = self.stage()
        self.cached(state, cache)
        before = copy.deepcopy(state)
        calls = bridge.calls
        self.assertIsNone(cache.retry_export(
            state, record(), spec(), approval=approval(), timestamp=at(900)))
        self.assertEqual(state, before)
        self.assertEqual(bridge.calls, calls)
        self.assertNotIn(RETRY_LANE, state)

    def test_cached_marker_with_missing_bytes_is_eligible_and_digest_bound(self):
        state, cache, _ = self.stage()
        entry = self.cached(state, cache)
        key, digest = entry["key"], entry["result_sha256"]
        self.file().unlink()
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        archived = state[RETRY_LANE][key][0]["marker"]
        self.assertEqual((archived["status"], archived["result_sha256"]), ("cached", digest))
        self.assertEqual(self.markers(state), {})
        self.assertIsNone(cache.prepare_export(
            state, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=at(1_000)))
        with self.assertRaises(CacheError):
            cache.cache_result(
                state, record(), spec(),
                result(EXEC_A, rows=[{"a_b": "different", "day": "different"}]),
                timestamp=at(1_100))
        self.assertFalse(self.file().exists())  # refused before atomic_write
        recovered = cache.cache_result(
            state, record(), spec(), result(EXEC_A, rows=rows_of(EXEC_A)),
            timestamp=at(1_100))
        self.assertEqual(recovered["result_sha256"], digest)
        self.assertEqual(self.markers(state)[key]["status"], "cached")

    def test_every_unresolved_status_is_retryable(self):
        for status in ("requested", "in_progress", "unknown"):
            with self.subTest(status=status):
                state, cache, _ = self.stage()
                marker = next(iter(self.markers(state).values()))
                key = marker["key"]
                marker["status"] = status
                self.retry(cache, state, operation_id=f"op-{status}", timestamp=at(900))
                self.assertEqual(state[RETRY_LANE][key][0]["marker"]["status"], status)
                self.assertEqual(self.markers(state), {})

    def test_retry_clears_only_its_own_binding(self):
        state, cache, _ = self.stage()
        cache.prepare_export(state, record(EXEC_B), spec(params={"a": 2}),
                             risk_credits=0.8, evidence=EV, timestamp=AT,
                             isolate_unrelated=True)
        key_a = next(k for k, m in self.markers(state).items() if m["execution_id"] == EXEC_A)
        marker_b = copy.deepcopy(next(m for k, m in self.markers(state).items() if k != key_a))
        self.retry(cache, state, operation_id="op-a", timestamp=at(900))
        self.assertEqual(set(self.markers(state)), {marker_b["key"]})
        self.assertEqual(self.markers(state)[marker_b["key"]], marker_b)
        self.assertEqual(list(state[RETRY_LANE]), [key_a])


class ApprovalValidationTests(Base):
    def assert_refused(self, state, cache, bridge, approval_value, *, timestamp=at(900),
                       spec_value=None, message=None):
        calls = bridge.calls
        marker = copy.deepcopy(state["result_exports"])
        with self.assertRaises(CacheError) as caught:
            cache.retry_export(state, record(), spec_value or spec(),
                               approval=approval_value, timestamp=timestamp)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertEqual(state["result_exports"], marker)
        self.assertNotIn(RETRY_LANE, state)
        self.assertEqual(bridge.calls, calls)

    def test_approval_must_be_current_and_unexpired(self):
        state, cache, bridge = self.stage()
        cases = {
            "not_yet_valid": approval(approved_at=at(901)),
            "expired_at_timestamp": approval(valid_until=at(900)),
            "expired_before_timestamp": approval(valid_until=at(100)),
            "window_reversed": approval(approved_at=at(100), valid_until=at(50)),
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                self.assert_refused(state, cache, bridge, value)

    def test_date_only_or_naive_instants_are_rejected(self):
        state, cache, bridge = self.stage()
        cases = {
            "date_only_approved": approval(approved_at="2026-10-18"),
            "naive_valid_until": approval(valid_until="2026-10-19T12:00:00"),
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                self.assert_refused(state, cache, bridge, value)
        self.assert_refused(state, cache, bridge, approval(), timestamp="2026-10-18")
        self.assert_refused(state, cache, bridge, approval(),
                            timestamp="2026-10-18T12:15:00")

    def test_wrong_approval_identity_or_nonpositive_quote_is_rejected(self):
        state, cache, bridge = self.stage()
        cases = {
            "kind": approval(kind="execution"),
            "query": approval(query_id=8894038),
            "parameters": approval(params={"a": 2}),
            "execution": approval(execution_id=EXEC_B),
            "quote_zero": approval(quote=0),
            "quote_negative": approval(quote=-0.5),
            "quote_nan": approval(quote=float("nan")),
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                self.assert_refused(state, cache, bridge, value)

    def test_new_positive_quote_need_not_equal_old_risk(self):
        state, cache, _ = self.stage()
        key = next(iter(self.markers(state)))
        new_quote = 0.91  # a fresh credible estimate for the new read
        self.retry(cache, state, operation_id="op-new-quote", timestamp=at(900),
                   approval_value=approval("op-new-quote", quote=new_quote))
        entry = state[RETRY_LANE][key][0]
        self.assertEqual(entry["marker"]["risk_credits"], RISK)  # old risk immutable
        self.assertEqual(entry["approval"]["quote_credits"], new_quote)
        row = state[JOURNAL]["obligations"][export_attempt_id(key, AT)]
        self.assertEqual(row["reserved_credits"], RISK)  # old obligation untouched
        self.assertIsNone(row["cost_credits"])
        self.assertIs(row["unresolved"], True)

    def test_marker_identity_must_match_the_exact_spec(self):
        state, cache, bridge = self.stage()
        cases = {
            "parameters": spec(params={"a": 2}),
            "execution": spec(),
            "columns": spec(cols=["a_b"]),
            "query": spec(qid=8894038),
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                call_spec = value
                if label == "execution":
                    # The spec stays exact; the record names a different execution.
                    with self.assertRaises(CacheError):
                        cache.retry_export(state, record(EXEC_B), spec(),
                                           approval=approval(execution_id=EXEC_B),
                                           timestamp=at(900))
                    self.assertNotIn(RETRY_LANE, state)
                    continue
                self.assert_refused(state, cache, bridge, approval(), spec_value=call_spec)

    def test_zero_old_risk_is_archived_while_new_quote_is_positive(self):
        state, cache, _ = self.stage(risk=0.0)
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-zero-old", timestamp=at(900),
                   approval_value=approval("op-zero-old", quote=0.5))
        entry = state[RETRY_LANE][key][0]
        self.assertEqual(entry["marker"]["risk_credits"], 0.0)
        self.assertEqual(entry["approval"]["quote_credits"], 0.5)

    def test_operation_id_must_be_new(self):
        state, cache, _ = self.stage()
        self.retry(cache, state, operation_id="op-reused", timestamp=at(900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(1_000))
        with self.assertRaises(CacheError) as caught:
            self.retry(cache, state, operation_id="op-reused", timestamp=at(1_900))
        self.assertIn("operation id", str(caught.exception))
        self.assertEqual(len(state[RETRY_LANE][next(iter(state[RETRY_LANE]))]), 1)

    def test_operation_ids_are_unique_across_bindings(self):
        state, cache, _ = self.stage()
        cache.prepare_export(state, record(EXEC_B), spec(params={"a": 2}),
                             risk_credits=0.8, evidence=EV, timestamp=AT,
                             isolate_unrelated=True)
        key_b = next(k for k, m in self.markers(state).items() if m["execution_id"] == EXEC_B)
        self.retry(cache, state, operation_id="op-global", timestamp=at(900))
        with self.assertRaises(CacheError):
            cache.retry_export(state, record(EXEC_B), spec(params={"a": 2}),
                               approval=approval("op-global", quote=0.8,
                                                 params={"a": 2}, execution_id=EXEC_B),
                               timestamp=at(900))
        self.assertIn(key_b, self.markers(state))

    def test_approval_performance_is_optional_but_typed(self):
        state, cache, bridge = self.stage()
        self.retry(cache, state, operation_id="op-no-performance", timestamp=at(900),
                   approval_value=approval("op-no-performance", include_performance=False))
        state2, cache2, bridge2 = self.stage()
        for value in ("", 1, None):
            with self.subTest(value=value):
                bad = approval("op-bad-performance")
                bad["performance"] = value
                self.assert_refused(state2, cache2, bridge2, bad)


class BoundedRetryTests(Base):
    def test_delay_boundary_defers_without_mutation_then_allows(self):
        state, cache, bridge = self.stage()
        calls = bridge.calls
        with self.assertRaises(ExportRetryDeferred) as caught:
            self.retry(cache, state, operation_id="op-1", timestamp=at(899))
        self.assertIsInstance(caught.exception, ManualExportReview)
        self.assertEqual(caught.exception.retry_after_utc, at(900))
        self.assertIn("no manual review required", str(caught.exception))
        self.assertIn("requested", [m["status"] for m in self.markers(state).values()])
        self.assertNotIn(RETRY_LANE, state)
        self.assertNotIn(JOURNAL, state)
        self.assertEqual(bridge.calls, calls)
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        self.assertEqual(self.markers(state), {})

    def test_three_attempts_per_utc_day_then_next_day_allowed(self):
        state, cache, bridge = self.stage()
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(1_000))
        self.retry(cache, state, operation_id="op-2", timestamp=at(1_900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(2_000))
        calls = bridge.calls
        with self.assertRaises(ExportRetryExhausted) as caught:
            self.retry(cache, state, operation_id="op-3", timestamp=at(2_900))
        self.assertIsInstance(caught.exception, ManualExportReview)
        self.assertIsInstance(caught.exception, ExportRetryDeferred)  # transient daily cap
        self.assertEqual(caught.exception.retry_after_utc, "2026-10-19T00:00:00Z")
        self.assertEqual(len(state[RETRY_LANE][key]), 2)  # lifetime archive, not trimmed
        self.assertIn(key, self.markers(state))  # unresolved read still held
        self.assertEqual(bridge.calls, calls)
        # Next UTC day: the read-count limit resets, the old archive/liabilities do not.
        self.assertIsNone(cache.retry_export(
            state, record(), spec(),
            approval=approval("op-next-day", quote=0.8,
                              approved_at=at(86_400), valid_until=at(2 * 86_400)),
            timestamp=at(86_400)))
        self.assertEqual(len(state[RETRY_LANE][key]), 3)
        self.assertEqual(
            [entry["marker"]["requested_at_utc"] for entry in state[RETRY_LANE][key]],
            [AT, at(1_000), at(2_000)])
        self.assertEqual(self.markers(state), {})

    def test_custom_max_attempts_is_per_utc_day_and_resets_next_day(self):
        state, cache, _ = self.stage()
        key = next(iter(self.markers(state)))
        self.assertIsNone(cache.retry_export(
            state, record(), spec(), approval=approval("op-1"), timestamp=at(900),
            max_attempts=2))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(1_000))
        with self.assertRaises(ExportRetryExhausted) as caught:
            cache.retry_export(state, record(), spec(), approval=approval("op-2"),
                               timestamp=at(1_900), max_attempts=2)
        self.assertEqual(caught.exception.retry_after_utc, "2026-10-19T00:00:00Z")
        self.assertEqual(len(state[RETRY_LANE][key]), 1)
        self.assertIsNone(cache.retry_export(
            state, record(), spec(),
            approval=approval("op-3", approved_at=at(86_400),
                              valid_until=at(2 * 86_400)),
            timestamp=at(86_400), max_attempts=2))
        self.assertEqual(len(state[RETRY_LANE][key]), 2)

    def test_invalid_bounds_are_rejected_before_any_change(self):
        state, cache, bridge = self.stage()
        calls = bridge.calls
        for delay in (-1, float("nan"), float("inf"), True, "900", None):
            with self.subTest(delay=delay):
                with self.assertRaises(CacheError):
                    cache.retry_export(state, record(), spec(), approval=approval(),
                                       timestamp=at(900), minimum_delay_seconds=delay)
        for attempts in (0, -1, True, 2.5, "3", None):
            with self.subTest(max_attempts=attempts):
                with self.assertRaises(CacheError):
                    cache.retry_export(state, record(), spec(), approval=approval(),
                                       timestamp=at(900), max_attempts=attempts)
        self.assertNotIn(RETRY_LANE, state)
        self.assertEqual(bridge.calls, calls)

    def test_consumed_or_absent_marker_is_not_retried(self):
        cache, bridge = mk(self.root)
        with self.assertRaises(ManualExportReview):
            cache.retry_export({}, record(), spec(), approval=approval(), timestamp=at(900))
        state, cache, _ = self.stage()
        next(iter(self.markers(state).values()))["status"] = "consumed"
        with self.assertRaises(ManualExportReview):
            self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        self.assertNotIn(RETRY_LANE, state)


class JournalAndArchiveTests(Base):
    def test_old_attempt_stays_unknown_and_new_attempt_is_distinct(self):
        state, cache, _ = self.stage()
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        first_id = export_attempt_id(key, AT)
        first_row = copy.deepcopy(state[JOURNAL]["obligations"][first_id])
        cache.prepare_export(state, record(), spec(), risk_credits=0.75,
                             evidence=EV, timestamp=at(1_000))
        self.retry(cache, state, operation_id="op-2", timestamp=at(1_900),
                   approval_value=approval("op-2", quote=0.95))
        second_id = export_attempt_id(key, at(1_000))
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(state[JOURNAL]["obligations"][first_id], first_row)
        for identifier in (first_id, second_id):
            row = state[JOURNAL]["obligations"][identifier]
            self.assertIsNone(row["cost_credits"])
            self.assertIs(row["unresolved"], True)
        self.assertEqual(
            [entry["marker"]["requested_at_utc"] for entry in state[RETRY_LANE][key]],
            [AT, at(1_000)])
        self.assertEqual(
            [entry["approval"]["operation_id"] for entry in state[RETRY_LANE][key]],
            ["op-1", "op-2"])

    def test_day_rollover_keeps_old_unknown_obligations_and_archive(self):
        state, cache, _ = self.stage()
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(1_000))
        self.retry(cache, state, operation_id="op-2", timestamp=at(1_900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(2_000))
        first_id = export_attempt_id(key, AT)
        second_id = export_attempt_id(key, at(1_000))
        first_row = copy.deepcopy(state[JOURNAL]["obligations"][first_id])
        second_row = copy.deepcopy(state[JOURNAL]["obligations"][second_id])
        self.assertEqual(len(state[RETRY_LANE][key]), 2)
        self.assertIsNone(cache.retry_export(
            state, record(), spec(),
            approval=approval("op-next-day", quote=0.99,
                              approved_at=at(86_400), valid_until=at(2 * 86_400)),
            timestamp=at(86_400)))
        self.assertEqual(state[JOURNAL]["obligations"][first_id], first_row)
        self.assertEqual(state[JOURNAL]["obligations"][second_id], second_row)
        for identifier in (first_id, second_id):
            row = state[JOURNAL]["obligations"][identifier]
            self.assertIsNone(row["cost_credits"])
            self.assertIs(row["unresolved"], True)
            self.assertEqual(row["reserved_credits"], RISK)
        self.assertEqual(len(state[RETRY_LANE][key]), 3)  # lifetime archive grows
        self.assertEqual(
            [entry["marker"]["requested_at_utc"] for entry in state[RETRY_LANE][key]],
            [AT, at(1_000), at(2_000)])

    def test_journal_preflight_failure_is_cache_error_without_mutation(self):
        state, cache, bridge = self.stage()
        state["execution_history"] = "malformed"
        before = copy.deepcopy(state)
        calls = bridge.calls
        with self.assertRaises(CacheError) as caught:
            self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        self.assertNotIsInstance(caught.exception, StateError)
        self.assertEqual(state, before)
        self.assertEqual(bridge.calls, calls)

    def test_checkpoint_failure_raises_state_error_and_preserves_candidate(self):
        state, cache, bridge = self.stage(crash_on=2)
        key = next(iter(self.markers(state)))
        fetched = False
        with self.assertRaises(CheckpointFailed) as caught:
            self.retry(cache, state, operation_id="op-1", timestamp=at(900))
            fetched = True
        self.assertIsInstance(caught.exception, StateError)
        self.assertNotIsInstance(caught.exception, CacheError)
        self.assertFalse(fetched)  # no transport/read follows an ambiguous checkpoint
        self.assertNotIn(key, self.markers(state))  # candidate preserved, not rolled back
        self.assertIn(key, state[RETRY_LANE])
        self.assertEqual(len(state[RETRY_LANE][key]), 1)
        self.assertIn(JOURNAL, state)  # pre-checkpoint synchronization also preserved
        self.assertFalse(self.file().exists())
        self.assertEqual(bridge.calls, 2)
        snapshot = bridge.snapshots[-1]
        self.assertIn(key, snapshot[RETRY_LANE])
        self.assertNotIn(key, snapshot["result_exports"]["requests"])

    def test_malformed_archive_is_refused(self):
        state, cache, bridge = self.stage()
        key = next(iter(self.markers(state)))
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(1_000))
        valid_entry = copy.deepcopy(state[RETRY_LANE][key][0])
        cases = {
            "lane_not_object": [],
            "entry_missing_approval": {"marker": valid_entry["marker"]},
            "duplicate_operation": [valid_entry, copy.deepcopy(valid_entry)],
            "marker_wrong_key": [dict(valid_entry, marker=dict(valid_entry["marker"],
                                                               key=hashlib.sha256(b"x").hexdigest()))],
        }
        calls = bridge.calls
        for label, lane in cases.items():
            with self.subTest(case=label):
                tampered = copy.deepcopy(state)
                tampered[RETRY_LANE] = lane
                with self.assertRaises(CacheError):
                    self.retry(cache, tampered, operation_id="op-2", timestamp=at(1_900))
                self.assertIn(key, tampered["result_exports"]["requests"])
        self.assertEqual(bridge.calls, calls)

    def test_archive_is_never_trimmed(self):
        state, cache, _ = self.stage()
        self.retry(cache, state, operation_id="op-1", timestamp=at(900))
        cache.prepare_export(state, record(), spec(), risk_credits=RISK,
                             evidence=EV, timestamp=at(1_000))
        self.retry(cache, state, operation_id="op-2", timestamp=at(1_900))
        key = next(iter(state[RETRY_LANE]))
        self.assertEqual(len(state[RETRY_LANE][key]), 2)
        self.assertEqual([entry["marker"]["requested_at_utc"]
                          for entry in state[RETRY_LANE][key]], [AT, at(1_000)])


if __name__ == "__main__":
    unittest.main()
