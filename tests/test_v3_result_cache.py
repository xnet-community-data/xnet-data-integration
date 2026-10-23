"""Tests for scripts/v3_result_cache.py - crash-safe exact-result reuse store.

Binding contract locked here (post-2026-10-18 execution-id fix):
  * ``_binding_fields`` REQUIRES a non-empty ``record["execution_id"]``; a result
    export can no longer be staged against an absent/blank execution identity.
  * the binding key includes the execution id, so two distinct executions of the
    same query with identical parameters can never reuse each other's rows, and a
    cache entry whose metadata does not re-derive its own key is refused.
  * ``state["consumed_executions"]`` is a dict keyed by execution id (exactly what
    ``v3_execution_tracker.stage_consumption`` writes); prune reads those keys and
    drops only entries whose execution id is genuinely accepted.
  * a valid cached entry is served while an UNRELATED intent is still outstanding
    (the cached-hit check resolves before the outstanding-intent scan).
Legacy shapes that predate those rules (list-shaped lanes, entries or markers
without a usable execution id) are refused, never silently reinterpreted.

Audit findings pinned at the bottom (``AuditSemanticsTests``):
  * the cache file is durable before the caller's ``checkpoint`` bridge commits,
    so a dead callback leaves the journal behind the file (reuse still works; the
    same-key ``cache_result`` repairs the marker idempotently);
  * RESOLVED (F-PRUNE): ``prune_consumed`` writes the pruned file BEFORE committing
    the ``consumed`` markers, so a dead checkpoint leaves the durable pair
    (marker=cached, entry=absent). It now repairs consumed markers even when no
    entry is left to drop (the ``changed`` flag), so the next prune self-heals the
    marker, never serves invented bytes, and lets that key be fetched again
    deliberately - pinned by
    ``test_prune_repairs_a_consumed_marker_when_the_commit_was_lost``.
"""
import copy
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v3_result_cache import (
    CACHE_SCHEMA_VERSION,
    MAX_CACHE_PAYLOAD_BYTES,
    CacheError,
    ManualExportReview,
    ResultCache,
)

HEXA = hashlib.sha256(b"SELECT 1").hexdigest()
RISK, EV, AT = 0.5, ["sampled 3/1 rows against Explorer"], "2026-10-18T12:00:00Z"
EXEC_A, EXEC_B, EXEC_C = "01ARZ3NDEKTSV4RR", "01ARZ3NDEKTSV4RX", "01ARZ3NDEKTSV4RY"


def spec(qid=8894037, params=None, cols=None, sql_hex=HEXA):
    return {"query_id": qid, "sql_sha256": sql_hex, "parameters": params if params is not None else {"a": 1}, "columns": cols if cols is not None else ["a_b", "day"]}


def record(execution_id=EXEC_A):
    return {"execution_id": execution_id}


def result(execution_id=EXEC_A, rows=None, total=None):
    rows = [{"a_b": 1, "day": "2026-01-01"}] if rows is None else rows
    return {"execution_id": execution_id, "total_row_count": len(rows) if total is None else total, "result_rows": rows}


def rows_of(execution_id):
    """Distinct rows per execution, so a wrong-id reuse cannot pass by luck."""
    tag = execution_id[-1]
    return [{"a_b": f"row-{tag}", "day": f"day-{tag}"}]


def consumed(*execution_ids):
    """The dict shape v3_execution_tracker.stage_consumption writes."""
    return {eid: {"key": eid, "query_id": 8894037, "effect": "source_consumed"} for eid in execution_ids}


class Bridge:
    def __init__(self, crash_on=None):
        self.calls, self.crash_on = [], crash_on

    def __call__(self, state):
        self.calls.append("checkpoint")
        if self.crash_on is not None and len(self.calls) == self.crash_on:
            raise RuntimeError("process died mid-commit")


def mk(root, crash_on=None):
    bridge = Bridge(crash_on)
    return ResultCache(root, bridge, allowed_query_ids={8894037}), bridge


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "data" / "current").mkdir(parents=True)  # store's atomic_write needs the real parent

    def file(self):
        return self.root / "data" / "current" / "v3_result_cache.json"

    def records(self):
        return json.loads(self.file().read_text())["records"]

    def markers(self, state):
        return state["result_exports"]["requests"]

    def stage(self, crash_on=None, execution_id=EXEC_A):
        """Fresh prepared state plus cache: one bridge call committed."""
        state = {}
        cache, bridge = mk(self.root, crash_on)
        cache.prepare_export(state, record(execution_id), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        return state, cache, bridge

    def cached(self, state, cache, execution_id=EXEC_A, params=None):
        """Stage a completed fetch for an anchored execution (rows identify it)."""
        return cache.cache_result(state, record(execution_id), spec(params=params), result(execution_id, rows=rows_of(execution_id)), timestamp=AT)


class ExecutionBindingTests(Base):
    """The execution id is a required part of the binding, never optional."""

    def test_absent_or_blank_execution_id_cannot_stage_an_export(self):
        cache, _ = mk(self.root)
        for bad in (None, {}, {"execution_id": ""}, {"execution_id": 0}, {"execution_id": 12345}):
            with self.subTest(record=bad):
                with self.assertRaises(CacheError) as ctx:
                    cache.prepare_export({}, bad, spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
                self.assertIn("execution", str(ctx.exception))
        self.assertFalse(self.file().exists())  # a refused binding never writes artifacts

    def test_legacy_list_shaped_requests_lane_is_refused(self):
        cache, _ = mk(self.root)
        for lane in ({"schema_version": 1, "requests": []}, {"schema_version": 1, "requests": ["EXECA"]}):
            with self.subTest(lane=lane):
                with self.assertRaises(CacheError) as ctx:
                    cache.prepare_export({"result_exports": lane}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
                self.assertIn("lane is malformed", str(ctx.exception))

    def test_legacy_marker_without_execution_id_is_refused(self):
        state, cache, _ = self.stage()
        marker = next(iter(self.markers(state).values()))
        del marker["execution_id"]  # pre-fix markers did not carry an anchor
        with self.assertRaises(CacheError) as ctx:
            cache.prepare_export(state, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("must carry exactly", str(ctx.exception))

    def test_legacy_cache_entry_without_execution_id_is_refused(self):
        state, cache, _ = self.stage()
        self.cached(state, cache)
        entry = next(iter(self.records().values()))
        del entry["execution_id"]
        self.file().write_text(json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "records": {entry["key"]: entry}}))
        with self.assertRaises(CacheError) as ctx:
            cache.prepare_export({}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("must carry exactly", str(ctx.exception))


class NoInventedRiskTests(Base):
    def test_risk_estimate_must_come_from_caller(self):
        cache, _ = mk(self.root)
        for risk in (None, -1.0, float("nan"), float("inf"), True, "1"):
            with self.subTest(risk=risk):
                with self.assertRaises(CacheError):
                    cache.prepare_export({}, record(), spec(), risk_credits=risk, evidence=EV, timestamp=AT)
        for evidence in (None, [], [""], [1], "sampled"):
            with self.subTest(evidence=evidence):
                with self.assertRaises(CacheError):
                    cache.prepare_export({}, record(), spec(), risk_credits=RISK, evidence=evidence, timestamp=AT)
        with self.assertRaises(CacheError):
            cache.prepare_export({}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp="")
        self.assertFalse(self.file().exists())  # a rejected estimate never writes artifacts
        zero = {}
        cache.prepare_export(zero, record(), spec(), risk_credits=0.0, evidence=EV, timestamp=AT)
        marker = next(iter(self.markers(zero).values()))
        self.assertEqual(marker["risk_credits"], 0.0)  # module stores caller values, never invents one


class FreshRequestTests(Base):
    def test_prepare_stamps_requested_and_checkpoints_before_any_fetch(self):
        state = {}
        cache, bridge = mk(self.root)
        self.assertIsNone(cache.prepare_export(state, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT))
        lane = state["result_exports"]
        self.assertEqual(lane["schema_version"], CACHE_SCHEMA_VERSION)
        marker = next(iter(self.markers(state).values()))
        self.assertEqual(marker["status"], "requested")
        self.assertEqual(marker["execution_id"], EXEC_A)
        self.assertEqual((marker["risk_credits"], marker["evidence"], marker["requested_at_utc"]), (RISK, EV, AT))
        self.assertEqual(bridge.calls, ["checkpoint"])  # stamped before the caller may fetch
        with self.assertRaises(ManualExportReview):  # a different new export is blocked meanwhile
            cache.prepare_export(state, record(EXEC_B), spec(params={"a": 2}), risk_credits=RISK, evidence=EV, timestamp=AT)

    def test_outstanding_marker_blocks_after_restart_with_any_outstanding_status(self):
        state, cache, bridge = self.stage()
        disk = copy.deepcopy(state)
        marker = next(iter(self.markers(disk).values()))
        for status in ("in_progress", "unknown", "requested"):
            block = copy.deepcopy(disk)
            next(iter(self.markers(block).values()))["status"] = status
            with self.subTest(status=status):
                cache2, bridge2 = mk(self.root)
                with self.assertRaises(ManualExportReview):
                    cache2.prepare_export(block, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
                self.assertEqual(bridge2.calls, [])  # blocked attempts commit nothing
        self.assertEqual(marker["status"], "requested")


class CrashRecoveryTests(Base):
    def test_crash_before_first_checkpoint_leaves_no_artifacts_and_can_retry(self):
        cache, _ = mk(self.root, crash_on=1)
        with self.assertRaises(RuntimeError):
            cache.prepare_export({}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertFalse(self.file().exists())  # only the commit died; store replay starts clean
        retry, bridge = mk(self.root)
        self.assertIsNone(retry.prepare_export({}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT))
        self.assertEqual(bridge.calls, ["checkpoint"])

    def test_crash_after_result_write_recovers_idempotently_and_then_reuses(self):
        state, _, _ = self.stage()
        committed = copy.deepcopy(state)  # what the state store actually persisted
        cache, _ = mk(self.root, crash_on=1)
        with self.assertRaises(RuntimeError):
            cache.cache_result(state, record(), spec(), result(), timestamp=AT)  # file landed, marker commit died
        self.assertTrue(self.file().exists())
        on_disk = next(iter(self.records().values()))
        self.assertEqual(on_disk["result_rows"], result()["result_rows"])
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "requested")  # stale
        resumed, bridge = mk(self.root)
        with self.assertRaises(ManualExportReview):  # an unrelated new export is still blocked
            resumed.prepare_export(committed, record(EXEC_B), spec(params={"a": 2}), risk_credits=RISK, evidence=EV, timestamp=AT)
        # The file is authoritative for reuse: the exact cached execution is still served.
        reused = resumed.prepare_export(committed, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual(reused["result_rows"], result()["result_rows"])
        self.assertEqual(bridge.calls, [])  # reuse costs no commit
        entry = resumed.cache_result(committed, record(), spec(), result(), timestamp=AT)  # same digest: idempotent repair
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "cached")
        self.assertEqual(bridge.calls, ["checkpoint"])
        again = resumed.prepare_export(copy.deepcopy(committed), record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual(again["result_rows"], entry["result_rows"])  # zero extra fetch implied


class CacheReuseTests(Base):
    def test_cached_entry_reused_without_new_checkpoint_and_second_stage_refused(self):
        state, cache, bridge = self.stage()
        entry = self.cached(state, cache)
        marker = next(iter(self.markers(state).values()))
        self.assertEqual((marker["status"], marker["result_sha256"]), ("cached", entry["result_sha256"]))
        self.assertEqual(bridge.calls, ["checkpoint", "checkpoint"])
        reused = cache.prepare_export(state, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual(reused["result_rows"], result(rows=rows_of(EXEC_A))["result_rows"])
        self.assertEqual(bridge.calls, ["checkpoint", "checkpoint"])  # reuse costs no commit
        with self.assertRaises(CacheError):  # staging into an already cached intent is refused
            cache.cache_result(state, record(), spec(), result(), timestamp=AT)

    def test_two_executions_same_query_and_params_never_reuse_each_other(self):
        state, cache, _ = self.stage()  # EXEC_A asked for, same query/params as EXEC_B below
        self.cached(state, cache, EXEC_A)
        entry_a = cache.prepare_export(state, record(EXEC_A), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual(entry_a["execution_id"], EXEC_A)
        self.assertEqual(entry_a["result_rows"], rows_of(EXEC_A))
        # A second execution of the identical query+parameters must NOT be served A's rows.
        self.assertIsNone(cache.prepare_export(state, record(EXEC_B), spec(), risk_credits=RISK, evidence=EV, timestamp=AT))
        self.cached(state, cache, EXEC_B)
        keys = {marker["execution_id"]: marker["key"] for marker in self.markers(state).values()}
        self.assertEqual(set(keys), {EXEC_A, EXEC_B})
        self.assertNotEqual(keys[EXEC_A], keys[EXEC_B])  # the execution id is inside the binding
        got_a = cache.prepare_export(state, record(EXEC_A), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        got_b = cache.prepare_export(state, record(EXEC_B), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual((got_a["execution_id"], got_b["execution_id"]), (EXEC_A, EXEC_B))
        self.assertEqual((got_a["result_rows"], got_b["result_rows"]), (rows_of(EXEC_A), rows_of(EXEC_B)))
        self.assertNotEqual(got_a["result_rows"], got_b["result_rows"])  # would catch a swapped id

    def test_valid_cached_read_survives_an_unrelated_outstanding_intent(self):
        state, cache, _ = self.stage()
        self.cached(state, cache, EXEC_A)
        # A different, uncached intent is now outstanding for the same query.
        self.assertIsNone(cache.prepare_export(state, record(EXEC_B), spec(params={"a": 2}), risk_credits=RISK, evidence=EV, timestamp=AT))
        self.assertEqual(sorted(m["status"] for m in self.markers(state).values()), ["cached", "requested"])
        hit = cache.prepare_export(state, record(EXEC_A), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)  # not blocked
        self.assertEqual(hit["execution_id"], EXEC_A)
        with self.assertRaises(ManualExportReview):  # but a brand-new export still needs deliberate resolution
            cache.prepare_export(state, record(EXEC_C), spec(params={"a": 3}), risk_credits=RISK, evidence=EV, timestamp=AT)

    def test_cached_marker_without_file_entry_requires_manual_review(self):
        state, _, _ = self.stage()
        cache, _ = mk(self.root)
        staged = copy.deepcopy(state)
        self.cached(staged, cache)  # second cache stamps its own marker
        self.file().unlink()
        with self.assertRaises(ManualExportReview) as ctx:
            cache.prepare_export(staged, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("no second export", str(ctx.exception))

    def test_cache_entry_whose_execution_metadata_does_not_derive_its_key_is_refused(self):
        state, cache, _ = self.stage()
        self.cached(state, cache, EXEC_A)
        key, entry = next(iter(self.records().items()))
        entry["execution_id"] = EXEC_B  # rows still belong to A; the binding no longer re-derives
        self.file().write_text(json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "records": {key: entry}}))
        with self.assertRaises(CacheError) as ctx:
            cache.prepare_export(state, record(EXEC_A), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("binding does not match", str(ctx.exception))


class SpendFreeLookupTests(Base):
    """``lookup`` is the spend-free read path ``export_source`` uses before it spends:
    it must never stamp an intent, never commit, and only serve the anchored execution."""

    def test_lookup_miss_creates_and_commits_nothing(self):
        cache, bridge = mk(self.root)
        state = {}
        self.assertIsNone(cache.lookup(state, record(), spec()))
        self.assertEqual(state, {})  # no lane, no marker, no staged intent
        self.assertEqual(bridge.calls, [])
        self.assertFalse(self.file().exists())

    def test_lookup_returns_only_the_anchored_execution_rows(self):
        state, cache, _ = self.stage()
        self.cached(state, cache, EXEC_A)
        hit = cache.lookup(state, record(EXEC_A), spec())
        self.assertEqual(hit["execution_id"], EXEC_A)
        self.assertEqual(hit["result_rows"], rows_of(EXEC_A))
        self.assertIsNone(cache.lookup(state, record(EXEC_B), spec()))  # same query/params, other execution
        self.assertEqual(len(self.markers(state)), 1)  # lookups stamped no intent
        self.assertEqual(next(iter(self.markers(state).values()))["status"], "cached")
        with self.assertRaises(CacheError):  # the binding still requires an execution id
            cache.lookup(state, None, spec())

    def test_lookup_refuses_a_repeat_read_when_the_bytes_are_gone(self):
        state, cache, _ = self.stage()  # intent stamped, nothing fetched yet
        with self.assertRaises(ManualExportReview) as ctx:
            cache.lookup(state, record(), spec())
        self.assertIn("no repeat download", str(ctx.exception))
        self.cached(state, cache, EXEC_A)
        self.file().unlink()  # bytes vanished after the marker said cached
        with self.assertRaises(ManualExportReview):
            cache.lookup(state, record(EXEC_A), spec())

    def test_lookup_never_touches_a_malformed_lane(self):
        cache, bridge = mk(self.root)
        with self.assertRaises(CacheError):
            cache.lookup({"result_exports": {"schema_version": 1, "requests": {"junk": "nope"}}}, record(), spec())
        self.assertEqual(bridge.calls, [])


class ValidationTests(Base):
    def test_result_must_be_complete_no_pagination(self):
        state, cache, _ = self.stage()
        for bad in (result(total=2), result(rows=[{"a_b": 1, "day": "ok"}, {"a_b": 2, "extra": 1}]), result(rows=[{"a_b": math.inf, "day": "d"}]), result(rows=[{"a_b": {"x": 1}, "day": "d"}]), result(rows=[{"a_b": 0, "day": "d"}] * 50001)):
            with self.subTest(total=bad["total_row_count"]):
                with self.assertRaises(CacheError):
                    cache.cache_result(copy.deepcopy(state), record(), spec(), bad, timestamp=AT)
        self.assertEqual(next(iter(self.markers(state).values()))["status"], "requested")

    def test_result_must_name_the_exact_anchored_execution(self):
        state, cache, _ = self.stage()  # anchored to EXEC_A
        with self.assertRaises(CacheError) as ctx:
            cache.cache_result(state, record(EXEC_A), spec(), result(EXEC_B), timestamp=AT)
        self.assertIn("exact requested record's execution", str(ctx.exception))
        with self.assertRaises(CacheError):  # staging a different execution has no intent at all
            cache.cache_result(state, record(EXEC_B), spec(), result(EXEC_B), timestamp=AT)
        with self.assertRaises(CacheError):  # no record: refuse before touching the marker
            cache.cache_result(state, None, spec(), result(), timestamp=AT)

    def test_binding_allowlist_and_scalar_rules(self):
        cache, _ = mk(self.root)
        cases = (spec(qid=999999), spec(params={"db_password": "x"}), spec(params={"a": [1]}), spec(cols=["a", "a"]), spec(sql_hex="nothex"))
        for bad_spec in cases:
            with self.subTest(qid=bad_spec["query_id"]):
                with self.assertRaises(CacheError):
                    cache.prepare_export({}, record(), bad_spec, risk_credits=RISK, evidence=EV, timestamp=AT)
        with self.assertRaises(CacheError) as ctx:
            cache.prepare_export({}, record(), spec(qid=999999), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("allowed public onchain set", str(ctx.exception))

    def test_malformed_lane_and_missing_intent_refused(self):
        cache, _ = mk(self.root)
        with self.assertRaises(CacheError):
            cache.prepare_export({"result_exports": {"schema_version": 1, "requests": {"junk": "nope"}}}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        with self.assertRaises(CacheError):  # staging without a stamped intent is refused
            cache.cache_result({"result_exports": {"schema_version": 1, "requests": {}}}, record(), spec(), result(), timestamp=AT)


class TamperTests(Base):
    def test_tampered_rows_rejected_on_next_read(self):
        state, _, _ = self.stage()
        cache, _ = mk(self.root)
        self.cached(copy.deepcopy(state), cache)
        raw = json.loads(self.file().read_text())
        for tampered in raw["records"].values():
            tampered["result_rows"][0]["a_b"] = "swapped"
        self.file().write_text(json.dumps(raw))
        with self.assertRaises(CacheError):
            cache.prepare_export(state, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)


class CapacityTests(Base):
    def test_two_entry_cap_never_evicts_and_prune_unblocks(self):
        state, cache, bridge = self.stage()
        self.cached(state, cache, EXEC_A)
        cache.prepare_export(state, record(EXEC_B), spec(params={"a": 2}), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.cached(state, cache, EXEC_B, params={"a": 2})
        state3 = copy.deepcopy(state)
        with self.assertRaises(CacheError) as ctx:
            cache.prepare_export(state3, record(EXEC_C), spec(params={"a": 3}), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("unconsumed", str(ctx.exception))
        self.assertEqual(state3,state)  # no new paid-read intent while full
        self.assertEqual(len(self.records()), 2)
        state4 = copy.deepcopy(state3)
        state4["consumed_executions"] = consumed(EXEC_A)  # the tracker's dict shape
        info = cache.prune_consumed(state4)  # deliberate prune frees headroom
        self.assertEqual(info, {"instruction": "pruned", "dropped": 1})
        cache.prepare_export(state4, record(EXEC_C), spec(params={"a": 3}), risk_credits=RISK, evidence=EV, timestamp=AT)
        entry = self.cached(state4, cache, EXEC_C, params={"a": 3})
        self.assertEqual(len(self.records()), 2)
        statuses = [m["status"] for m in self.markers(state4).values()]
        self.assertEqual(statuses.count("cached"), 2)
        self.assertIn("consumed", statuses)
        self.assertEqual(entry["execution_id"], EXEC_C)

    def test_prune_without_match_leaves_everything_and_consumed_marker_allows_refresh(self):
        state, cache, bridge = self.stage()
        self.cached(state, cache)
        calls = len(bridge.calls)
        idle = cache.prune_consumed(copy.deepcopy(state))  # nothing consumed yet
        self.assertEqual(idle, {"instruction": "nothing_to_prune", "dropped": 0})
        self.assertEqual(len(bridge.calls), calls)  # no-op prunes commit nothing
        state["consumed_executions"] = consumed(EXEC_A)  # source result consumed later
        pruned = cache.prune_consumed(state)
        self.assertEqual(pruned["dropped"], 1)
        marker = next(iter(self.markers(state).values()))
        self.assertEqual(marker["status"], "consumed")  # marker stays as export telemetry
        self.assertEqual(self.records(), {})  # consumed entry dropped
        fresh = {}
        cache.prepare_export(fresh, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)  # consumed intent no longer blocks
        self.assertEqual(next(iter(self.markers(fresh).values()))["status"], "requested")

    def test_prune_drops_only_genuinely_accepted_executions(self):
        state, cache, bridge = self.stage()
        self.cached(state, cache, EXEC_A)
        calls = len(bridge.calls)
        state["consumed_executions"] = consumed(EXEC_C)  # some other accepted execution
        self.assertEqual(cache.prune_consumed(copy.deepcopy(state)), {"instruction": "nothing_to_prune", "dropped": 0})
        self.assertEqual([e["execution_id"] for e in self.records().values()], [EXEC_A])  # entry untouched
        self.assertEqual(len(bridge.calls), calls)
        state["consumed_executions"] = consumed(EXEC_C, EXEC_A)
        self.assertEqual(cache.prune_consumed(state), {"instruction": "pruned", "dropped": 1})
        self.assertEqual(self.records(), {})

    def test_list_shaped_consumed_executions_is_refused(self):
        cache, _ = mk(self.root)
        for bad in (["EXECA"], "EXECA", None):
            with self.subTest(bad=bad):
                with self.assertRaises(CacheError) as ctx:
                    cache.prune_consumed({"consumed_executions": bad})
                self.assertIn("object of accepted identities", str(ctx.exception))


class AuditSemanticsTests(Base):
    """Findings from the cache_result / prune_consumed audit, pinned as tests."""

    def test_cache_file_is_durable_before_the_marker_commit(self):
        """A dead checkpoint callback leaves the journal behind the file; same-key reuse
        still works because the file is authoritative, and cache_result repairs the marker."""
        state, cache, _ = self.stage()
        committed = copy.deepcopy(state)  # what the store persisted before the fetch
        dead, _ = mk(self.root, crash_on=1)
        with self.assertRaises(RuntimeError):
            dead.cache_result(state, record(), spec(), result(), timestamp=AT)
        self.assertTrue(self.file().exists())  # rows are durable ...
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "requested")  # ... journal is not
        self.assertEqual(next(iter(self.markers(state).values()))["status"], "cached")  # in-memory state diverges
        resumed, bridge = mk(self.root)
        reused = resumed.prepare_export(committed, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertEqual(reused["result_rows"], result()["result_rows"])
        self.assertEqual(bridge.calls, [])  # no second fetch, no commit
        resumed.cache_result(committed, record(), spec(), result(), timestamp=AT)
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "cached")
        self.assertEqual(bridge.calls, ["checkpoint"])

    def test_staged_payload_over_the_byte_bound_is_refused_before_any_write(self):
        state, cache, bridge = self.stage()
        calls = len(bridge.calls)
        huge = result(rows=[{"a_b": "x" * (MAX_CACHE_PAYLOAD_BYTES + 1), "day": "d"}])
        with self.assertRaises(CacheError) as ctx:
            cache.cache_result(state, record(), spec(), huge, timestamp=AT)
        self.assertIn("over the byte bound", str(ctx.exception))
        self.assertFalse(self.file().exists())  # refused before atomic_write
        self.assertEqual(len(bridge.calls), calls)  # and before the marker commit
        self.assertEqual(next(iter(self.markers(state).values()))["status"], "requested")

    def test_cache_file_over_the_byte_bound_is_refused_on_load(self):
        cache, _ = mk(self.root)
        self.file().write_text(" " * (MAX_CACHE_PAYLOAD_BYTES + 1))
        with self.assertRaises(CacheError) as ctx:
            cache.prune_consumed({})
        self.assertIn("over the byte bound", str(ctx.exception))

    def test_cache_file_over_the_record_cap_is_refused_on_load(self):
        state, cache, _ = self.stage()
        self.cached(state, cache, EXEC_A)
        key, entry = next(iter(self.records().items()))
        third = dict(entry, key=hashlib.sha256(b"third").hexdigest())
        other = dict(entry, key=hashlib.sha256(b"fourth").hexdigest())
        self.file().write_text(json.dumps({"schema_version": CACHE_SCHEMA_VERSION, "records": {key: entry, third["key"]: third, other["key"]: other}}))
        with self.assertRaises(CacheError) as ctx:
            cache.prepare_export({}, record(), spec(), risk_credits=RISK, evidence=EV, timestamp=AT)
        self.assertIn("at most 2 records", str(ctx.exception))

    def test_prune_repairs_a_consumed_marker_when_the_commit_was_lost(self):
        """F-PRUNE (resolved): prune writes the pruned file before checkpointing the
        ``consumed`` markers. A dead checkpoint leaves (marker=cached, entry=absent);
        because the entry is already gone, a retry used to be a no-op, wedging that key
        for a human. ``prune_consumed`` now repairs consumed markers even when nothing
        is left to drop, so the next prune self-heals: it commits once, the marker becomes
        ``consumed``, no bytes are invented, and that key can be fetched again deliberately."""
        state, cache, _ = self.stage()
        self.cached(state, cache, EXEC_A)
        state["consumed_executions"] = consumed(EXEC_A)
        committed = copy.deepcopy(state)  # what the store persisted before prune
        dead, _ = mk(self.root, crash_on=1)
        with self.assertRaises(RuntimeError):
            dead.prune_consumed(state)
        self.assertEqual(self.records(), {})  # entry already dropped durably
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "cached")  # marker commit lost
        resumed, bridge = mk(self.root)
        # The repair is not a no-op even though there is nothing left to drop.
        self.assertEqual(resumed.prune_consumed(committed), {"instruction": "pruned", "dropped": 0})
        self.assertEqual(bridge.calls, ["checkpoint"])  # exactly one repairing commit
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "consumed")
        # Idempotent once repaired, and no cached bytes are invented from the missing entry.
        self.assertEqual(resumed.prune_consumed(committed), {"instruction": "nothing_to_prune", "dropped": 0})
        self.assertEqual(bridge.calls, ["checkpoint"])
        self.assertIsNone(resumed.lookup(committed, record(EXEC_A), spec()))
        # The repaired key is deliberately re-exportable instead of needing manual review.
        self.assertIsNone(resumed.prepare_export(committed, record(EXEC_A), spec(), risk_credits=RISK, evidence=EV, timestamp=AT))
        self.assertEqual(next(iter(self.markers(committed).values()))["status"], "requested")


if __name__ == "__main__":
    unittest.main()
