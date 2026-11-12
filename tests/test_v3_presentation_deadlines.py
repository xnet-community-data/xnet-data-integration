"""Offline regressions for hard deadlines versus approaching deadlines."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
module = importlib.util.spec_from_file_location(
    "presentation_deadline_refresh_test", ROOT / "scripts/v3_refresh_dashboard.py")
refresh = importlib.util.module_from_spec(module)
module.loader.exec_module(refresh)

NOW = datetime(2026, 11, 12, 20, 58, tzinfo=timezone.utc)


def production_fixture():
    # Accepted clocks immediately before run 43305173249; no financial or
    # wallet data is needed to reproduce its five-slot scheduling decision.
    accepted = {
        "current": "2026-11-12T13:13:23Z",
        "burns": "2026-11-12T13:14:11Z",
        "tokenomics": "2026-11-12T04:28:22Z",
        "holders": "2026-11-12T13:12:56Z",
        "pools": "2026-11-12T13:14:34Z",
        "network": "2026-11-12T13:13:45Z",
        "commercial": "2026-11-10T22:37:05Z",
    }
    return {
        "queries": {key: {"completed_at_utc": value} for key, value in accepted.items()},
        "presentation_last_attempts": dict(accepted),
        "forced_presentations": ["current", "holders", "commercial"],
    }


class PresentationDeadlineTests(unittest.TestCase):
    def test_november_12_overdue_pools_is_not_displaced_by_near_deadline_commercial(self):
        state = production_fixture()
        before = deepcopy(state)
        with patch.object(refresh, "now", return_value=NOW):
            ordered = sorted(refresh.CONFIG["presentation"], key=lambda spec:
                refresh.presentation_priority(state, spec, set(state["forced_presentations"])))
        selected = {spec["key"] for spec in ordered[:refresh.CONFIG["max_presentation_queries_per_run"]]}
        self.assertEqual(selected, {"current", "holders", "burns", "tokenomics", "pools"})
        self.assertEqual(state, before)

    def test_main_keeps_five_attempt_bound_and_backoff_with_the_production_fixture(self):
        state = {"schema_version": 1, "pending_executions": {}, **production_fixture()}
        accepted_before = deepcopy(state["queries"])
        config = {**refresh.CONFIG, "sources": [], "reconciliation": None}
        attempted = []

        def fail(spec, **kwargs):
            attempted.append(spec["key"])
            raise refresh.ExecutionFailed("offline provider failure")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(refresh, "now", return_value=NOW), \
             patch.object(refresh, "CONFIG", config), \
             patch.object(refresh, "ROOT", Path(directory)), \
             patch.object(refresh, "STATE", Path(directory) / "state.json"), \
             patch.object(refresh, "HEALTH", Path(directory) / "health.json"), \
             patch.object(refresh, "load_authoritative_state", return_value=state), \
             patch.object(refresh, "save"), patch.object(refresh, "checkpoint"), \
             patch.object(refresh, "run"), patch.object(refresh, "sync_independent_network"), \
             patch.object(refresh, "publish_data_generation"), \
             patch.object(refresh, "usage_guard", return_value={}), \
             patch.object(refresh, "execute", side_effect=fail), \
             patch.object(refresh, "api") as api, patch("sys.argv", ["refresh"]):
            self.assertEqual(refresh.main(), 0)
        self.assertEqual(len(attempted), 5)
        self.assertEqual(set(attempted), {"current", "holders", "burns", "tokenomics", "pools"})
        self.assertEqual(state["queries"], accepted_before)
        self.assertFalse(state["paused"])
        self.assertTrue(all(state["presentation_errors"][key]["next_retry_at_utc"]
                            for key in attempted))
        api.assert_not_called()
        # A deadline never grants permission to bypass a failed query's retry.
        with patch.object(refresh, "now", return_value=NOW):
            self.assertTrue(refresh.presentation_in_backoff(state, "pools"))
        state["pending_executions"]["pools"] = {"execution_id": "existing-id"}
        with patch.object(refresh, "now", return_value=NOW):
            self.assertFalse(refresh.presentation_in_backoff(state, "pools"))

    def test_expired_attempts_rotate_before_repeated_source_invalidation(self):
        specs = deepcopy(refresh.CONFIG["presentation"][:6])
        forced = {spec["key"] for spec in specs} - {"pools"}
        state = {"queries": {}, "presentation_last_attempts": {}}
        for spec in specs:
            key = spec["key"]
            state["queries"][key] = {"completed_at_utc": (NOW - timedelta(days=3)).isoformat()}
            state["presentation_last_attempts"][key] = (
                NOW - timedelta(hours=1 if key == "pools" else 2)).isoformat()
        attempted = []
        with patch.object(refresh, "now", return_value=NOW):
            for _ in range(2):
                ordered = sorted(specs, key=lambda spec:
                    refresh.presentation_priority(state, spec, forced))
                selected = ordered[:refresh.CONFIG["max_presentation_queries_per_run"]]
                self.assertEqual(len(selected), 5)
                attempted.append([spec["key"] for spec in selected])
                # Even a failure advances only its attempt clock, not accepted
                # freshness. Repeated invalidation must not crowd pools out.
                for spec in selected:
                    state["presentation_last_attempts"][spec["key"]] = NOW.isoformat()
        self.assertNotIn("pools", attempted[0])
        self.assertEqual(attempted[1][0], "pools")

    def test_near_deadline_and_safe_charts_keep_forced_then_attempt_rotation(self):
        specs = deepcopy(refresh.CONFIG["presentation"][:3])
        for age in (2, 5):
            with self.subTest(age_hours=age):
                state = {"queries": {}, "presentation_last_attempts": {}}
                for index, spec in enumerate(specs):
                    state["queries"][spec["key"]] = {
                        "completed_at_utc": (NOW - timedelta(hours=age)).isoformat()}
                    state["presentation_last_attempts"][spec["key"]] = (
                        NOW - timedelta(minutes=index)).isoformat()
                with patch.object(refresh, "now", return_value=NOW):
                    ordered = sorted(specs, key=lambda spec:
                        refresh.presentation_priority(state, spec, {specs[0]["key"]}))
                self.assertEqual(ordered, [specs[0], specs[2], specs[1]])

    def test_invalid_accepted_clocks_enter_the_expired_class_without_rewriting_them(self):
        spec = refresh.CONFIG["presentation"][0]
        for value in (None, "invalid", (NOW + timedelta(minutes=6)).isoformat()):
            with self.subTest(value=value):
                state = {"queries": {spec["key"]: {"completed_at_utc": value}}}
                before = deepcopy(state)
                with patch.object(refresh, "now", return_value=NOW):
                    self.assertEqual(refresh.presentation_priority(state, spec, set())[0], 0)
                self.assertEqual(state, before)

    def test_expiry_uses_the_watchdogs_budget_adjusted_deadline(self):
        spec = next(spec for spec in refresh.CONFIG["presentation"] if spec["key"] == "holders")
        state = {"queries": {}, "standing_credit_budget": {"last_decision": {"cadence_multiplier": 4}}}
        state["standing_authorization"] = {
            "approval_ref": refresh.CONFIG["autonomous_operation"]["approval_ref"],
            "policy_sha256": refresh.standing_authority_hash(refresh.CONFIG["autonomous_operation"]),
        }
        for hours, expected_class in ((6, 2), (15, 1), (17, 0)):
            with self.subTest(age_hours=hours):
                state["queries"][spec["key"]] = {"completed_at_utc": (NOW - timedelta(hours=hours)).isoformat()}
                with patch.object(refresh, "now", return_value=NOW):
                    self.assertEqual(refresh.presentation_priority(state, spec, set())[0], expected_class)


if __name__ == "__main__":
    unittest.main()
