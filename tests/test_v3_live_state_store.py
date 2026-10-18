"""Real local Git transports; never contact GitHub or Dune."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_live_state_store import (GENERATION, JOURNAL, MACHINE_OWNED,
                                 PRESENTATION, REQUIRED,
                                 LiveStateStore, StateError, validate_journal)

ROOT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", *args], cwd=root,
                                       stderr=subprocess.DEVNULL).decode().strip()

    def write(self, root, path, value):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value) + "\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.git(self.root, "init", "--bare", str(self.origin))
        self.seed.mkdir()
        self.git(self.seed, "init", "-b", "live-state")
        self.git(self.seed, "config", "user.name", "test")
        self.git(self.seed, "config", "user.email", "test@example.com")
        self.initial = {"schema_version": 1, "queries": {}, "execution_history": []}
        self.write(self.seed, JOURNAL, self.initial)
        self.write(self.seed, "data/current/xnet_chain_health.json", {"status": "HEALTHY"})
        self.write(self.seed, "data/xnet_defillama_revenue.json", {"untouched": True})
        self.write(self.seed, "data/canonical/remote_only.json", {"authoritative": True})
        (self.seed / "unrelated-source.py").write_text("do not hydrate\n")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "seed")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "origin", "live-state")
        self.base = self.head()

    def head(self):
        return self.git(self.origin, "rev-parse", "refs/heads/live-state")

    def writer(self, name="writer"):
        root = self.root / name
        root.mkdir()
        self.git(root, "init")
        self.git(root, "remote", "add", "origin", str(self.origin))
        store = LiveStateStore(root)
        store.hydrate()
        return store

    def stage(self, store, tag):
        state = {**self.initial, "marker": tag}
        self.write(store.root, JOURNAL, state)

    def change_remote(self, path, value):
        self.write(self.seed, path, value)
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "remote change")
        self.git(self.seed, "push", "origin", "live-state")
        return self.head()

    def test_hydration_clears_owned_residue_and_preserves_unowned_local_files(self):
        store = self.writer()
        # Owned machine residue: REQUIRED data path absent from the remote tree.
        owned_stale = store.root / "data/derived/bbb_burn_daily.csv"
        owned_stale.parent.mkdir(parents=True, exist_ok=True)
        owned_stale.write_text("stale,owned,residue\n")
        # Unowned local file under the same prefixes: the closed registry
        # prefers preserving unrelated paths over deleting them.
        self.write(store.root, "data/current/stale.json", {"pending": "local residue"})
        (store.root / "unrelated-source.py").write_text("local code\n")
        receipt = store.hydrate()
        self.assertEqual(receipt["revision"], self.base)
        self.assertFalse(owned_stale.exists(),
                         "owned machine residue must be cleared by hydration")
        self.assertEqual((store.root / "data/current/stale.json").read_text(),
                         json.dumps({"pending": "local residue"}) + "\n")
        self.assertEqual((store.root / "unrelated-source.py").read_text(), "local code\n")
        self.assertEqual(store.load_authoritative_journal(), self.initial)

    def test_missing_canonical_bbb_history_does_not_silently_delete_seed(self):
        store = self.writer()
        self.write(store.root, "data/history/bbb_wallet_usdc_daily.json", {"data": [{"day": "2026-10-01"}]})
        with self.assertRaisesRegex(StateError, "BBB history missing"):
            store.hydrate()
        self.assertTrue((store.root / "data/history/bbb_wallet_usdc_daily.json").exists())

    def test_receipt_covers_every_owned_path_and_missing_file_blocks_startup(self):
        store = self.writer()
        receipt = store.hydrate()
        self.assertEqual(receipt["authoritative_files"],
                         store.owned_revision_digests(receipt["revision"]),
                         "receipt inventory must cover the full owned set")
        self.assertIn(JOURNAL, receipt["authoritative_files"])
        remote_owned = set(store.tree(receipt["revision"])) & MACHINE_OWNED
        self.assertEqual(set(receipt["authoritative_files"]), remote_owned)
        self.assertEqual(store.load_authoritative_journal(), self.initial)
        # Startup also blocks when a receipted artifact vanishes locally.
        (store.root / "data/current/xnet_chain_health.json").unlink()
        with self.assertRaisesRegex(StateError, "missing locally"):
            store.load_authoritative_journal()
        with self.assertRaisesRegex(StateError, "missing locally"):
            store.load_authoritative_journal()

    def test_missing_corrupt_and_unsupported_remote_journal_never_fallback(self):
        store = self.writer()
        for raw in (None, b"{", b'{}', b'{"schema_version":99,"queries":{}}',
                    b'{"schema_version":1,"queries":{},"pending_executions":[]}'):
            with self.subTest(raw=raw):
                target = self.seed / JOURNAL
                if raw is None:
                    target.unlink()
                else:
                    target.write_bytes(raw)
                self.git(self.seed, "add", "-A")
                self.git(self.seed, "commit", "-m", "broken authoritative journal")
                self.git(self.seed, "push", "origin", "live-state")
                original = (store.root / JOURNAL).read_bytes()
                with self.assertRaises(StateError):
                    store.hydrate()
                self.assertEqual((store.root / JOURNAL).read_bytes(), original)
                self.assertEqual(store.receipt()["revision"], self.base)
                with self.assertRaises(StateError):
                    store.publish("credit-usage")

    def test_unhydrated_and_local_corrupt_journal_fail_closed(self):
        root = self.root / "not_hydrated"
        root.mkdir()
        self.git(root, "init")
        self.git(root, "remote", "add", "origin", str(self.origin))
        store = LiveStateStore(root)
        self.write(root, JOURNAL, self.initial)
        with self.assertRaises(StateError):
            store.publish("checkpoint")
        store.hydrate()
        (root / JOURNAL).write_text("{}")
        with self.assertRaises(StateError):
            store.load_authoritative_journal()
        with self.assertRaises(StateError):
            store.publish("checkpoint")
        self.assertEqual(self.head(), self.base)

    def test_two_writers_and_stale_base_cannot_overlay_winner(self):
        first, second = self.writer("first"), self.writer("second")
        self.stage(first, "first")
        self.stage(second, "stale second")
        receipt = first.publish("checkpoint")
        self.assertEqual(receipt["parent_revision"], self.base)
        self.assertEqual(receipt["revision"], self.head())
        with self.assertRaisesRegex(StateError, "moved"):
            second.publish("checkpoint")
        self.assertEqual(self.head(), receipt["revision"])
        second.hydrate()
        self.assertEqual(second.load_authoritative_journal()["marker"], "first")
        self.stage(second, "replanned second")
        next_receipt = second.publish("checkpoint")
        self.assertEqual(next_receipt["parent_revision"], receipt["revision"])
        self.assertEqual(self.git(self.origin, "rev-list", "--count", "live-state"), "3")

    def test_simultaneous_writers_race_at_remote_compare_and_swap(self):
        first, second = self.writer("first"), self.writer("second")
        self.stage(first, "first")
        self.stage(second, "second")
        barrier = threading.Barrier(2, timeout=15)
        def competing(store):
            original = store.head
            first_call = True
            def fenced_head():
                nonlocal first_call
                revision = original()
                if first_call:
                    first_call = False
                    barrier.wait()
                return revision
            with patch.object(store, "head", fenced_head):
                try:
                    return store.publish("checkpoint")
                except StateError:
                    return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(competing, (first, second)))
        winners = [receipt for receipt in results if receipt]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0]["revision"], self.head())
        self.assertEqual(self.git(self.origin, "rev-list", "--count", "live-state"), "2")

    def test_checkpoint_isolation_and_receipt_advances_on_each_checkpoint(self):
        store = self.writer()
        self.stage(store, "intent")
        first = store.publish("checkpoint")
        self.stage(store, "execution ID")
        second = store.publish("checkpoint")
        self.assertEqual(second["parent_revision"], first["revision"])
        changed = self.git(self.origin, "diff", "--name-only", self.base, self.head())
        self.assertEqual(set(changed.splitlines()), {JOURNAL, GENERATION})
        self.assertEqual(json.loads(store.blob(self.base, JOURNAL)), self.initial)

    def test_branch_moves_between_validation_and_push(self):
        store, rival = self.writer(), self.writer("rival")
        self.stage(store, "loser")
        self.stage(rival, "winner")
        original_push = store.push
        def competing_push(commit, parent):
            rival.publish("checkpoint")
            original_push(commit, parent)
        with patch.object(store, "push", competing_push):
            with self.assertRaises(StateError):
                store.publish("checkpoint")
        winner = self.head()
        self.assertEqual(json.loads(store.blob(winner, JOURNAL))["marker"], "winner")
        self.assertTrue(store.pending_path.exists())
        with self.assertRaises(StateError):
            store.recover()
        self.assertEqual(self.head(), winner)

    def test_branch_rewind_race_is_rejected_by_exact_lease(self):
        store = self.writer()
        self.stage(store, "first")
        first = store.publish("checkpoint")
        self.stage(store, "must not resurrect rewound head")
        original_push = store.push
        def rewind_before_push(commit, parent):
            self.git(self.origin, "update-ref", "refs/heads/live-state", self.base)
            original_push(commit, parent)
        with patch.object(store, "push", rewind_before_push):
            with self.assertRaises(StateError):
                store.publish("checkpoint")
        self.assertEqual(self.head(), self.base)
        self.assertEqual(store.receipt()["revision"], first["revision"])

    def test_lost_push_ack_is_resolved_by_exact_commit_not_latest_files(self):
        store = self.writer()
        self.stage(store, "accepted")
        original_push = store.push
        def lost_ack(commit, parent):
            original_push(commit, parent)
            raise StateError("transport lost acknowledgement")
        with patch.object(store, "push", lost_ack):
            receipt = store.publish("checkpoint")
        self.assertEqual(receipt["revision"], self.head())
        self.assertFalse(store.pending_path.exists())
        self.assertEqual(self.git(self.origin, "rev-list", "--count", "live-state"), "2")

    def test_rejected_push_keeps_durable_candidate_and_can_recover_without_rebuild(self):
        store = self.writer()
        self.stage(store, "retry same commit")
        with patch.object(store, "push", side_effect=StateError("rejected")):
            with self.assertRaisesRegex(StateError, "not acknowledged"):
                store.publish("checkpoint")
        intent = json.loads(store.pending_path.read_text())
        self.assertEqual(self.head(), self.base)
        receipt = store.recover()
        self.assertEqual(receipt["revision"], intent["revision"])
        self.assertEqual(self.head(), intent["revision"])

    def test_corrupt_recovery_intent_never_pushes_candidate(self):
        store = self.writer()
        self.stage(store, "not yet pushed")
        self.killed_publisher(store, False)
        original = json.loads(store.pending_path.read_text())
        for broken in ([], {**original, "schema_version": 9},
                       {**original, "generation_id": "wrong-generation"},
                       {**original, "files": {JOURNAL: "0" * 64}}):
            with self.subTest(broken=broken):
                store.pending_path.write_text(json.dumps(broken))
                with self.assertRaises(StateError):
                    store.recover()
                self.assertEqual(self.head(), self.base)
        store.pending_path.write_text(json.dumps(original))
        self.assertEqual(store.recover()["revision"], original["revision"])

    def test_branch_deletion_between_validation_and_push_cannot_recreate_it(self):
        store = self.writer()
        self.stage(store, "must not recreate deleted branch")
        original_push = store.push
        def delete_before_push(commit, parent):
            self.git(self.origin, "update-ref", "-d", "refs/heads/live-state")
            original_push(commit, parent)
        with patch.object(store, "push", delete_before_push):
            with self.assertRaises(StateError):
                store.publish("checkpoint")
        result = subprocess.run(["git", "show-ref", "--verify", "refs/heads/live-state"],
                                cwd=self.origin, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(store.pending_path.exists())

    def test_missing_local_journal_and_corrupt_receipt_block_startup(self):
        store = self.writer()
        (store.root / JOURNAL).unlink()
        with self.assertRaisesRegex(StateError, "journal missing"):
            store.load_authoritative_journal()
        store.hydrate()
        store.receipt_path.write_text('{}')
        with self.assertRaises(StateError):
            store.load_authoritative_journal()

    def killed_publisher(self, store, after_push):
        code = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
from v3_live_state_store import LiveStateStore
store = LiveStateStore(sys.argv[2])
original = store.push
def crash(commit, parent):
    if sys.argv[3] == 'after':
        original(commit, parent)
    os.kill(os.getpid(), signal.SIGKILL)
store.push = crash
store.publish('checkpoint')
"""
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"),
                                 str(store.root), "after" if after_push else "before"],
                                capture_output=True)
        self.assertEqual(result.returncode, -9)

    def test_process_killed_after_commit_recovers_exact_durable_candidate(self):
        store = self.writer()
        self.stage(store, "committed not pushed")
        self.killed_publisher(store, False)
        intent = json.loads(store.pending_path.read_text())
        self.assertEqual(self.head(), self.base)
        with self.assertRaises(StateError):
            store.hydrate()
        # The durable candidate must be a Git reachability root, not merely a hash
        # in JSON that git gc is free to delete before explicit recovery.
        self.git(store.root, "gc", "--prune=now")
        self.assertEqual(store.recover()["revision"], intent["revision"])
        self.assertEqual(self.head(), intent["revision"])

    def test_process_killed_after_push_and_later_writer_does_not_adopt_foreign_head(self):
        store = self.writer()
        self.stage(store, "pushed without receipt")
        self.killed_publisher(store, True)
        own_commit = self.head()
        rival = self.writer("rival")
        self.stage(rival, "newer independent writer")
        newer = rival.publish("checkpoint")["revision"]
        receipt = store.recover()
        self.assertEqual(receipt["revision"], own_commit)
        self.assertNotEqual(receipt["revision"], newer)
        with self.assertRaisesRegex(StateError, "moved"):
            store.publish("checkpoint")
        self.assertEqual(self.head(), newer)

    def test_pending_generation_cannot_acknowledge_different_local_files(self):
        store = self.writer()
        self.stage(store, "durable candidate")
        self.killed_publisher(store, True)
        self.stage(store, "not yet published")
        with self.assertRaisesRegex(StateError, "differs"):
            store.publish("checkpoint")
        receipt = store.recover()
        self.assertEqual(json.loads(store.blob(receipt["revision"], JOURNAL))["marker"], "durable candidate")

    def test_partial_hydration_invalidates_previous_receipt(self):
        store = self.writer()
        import v3_live_state_store as module
        with patch.object(module, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.hydrate()
        self.assertFalse(store.receipt_path.exists())
        with self.assertRaises(StateError):
            store.load_authoritative_journal()

    def test_credit_writer_preserves_authoritative_journal_even_with_local_residue(self):
        store = self.writer()
        (store.root / JOURNAL).write_text("{}")
        self.write(store.root, "data/current/v3_credit_usage.json", {"samples": []})
        receipt = store.publish("credit-usage")
        self.assertEqual(json.loads(store.blob(receipt["revision"], JOURNAL)), self.initial)
        changed = self.git(self.origin, "diff", "--name-only", self.base, self.head())
        self.assertEqual(set(changed.splitlines()), {"data/current/v3_credit_usage.json", GENERATION})

    def test_full_writer_and_subsequent_checkpoint_share_exact_revision(self):
        store = self.writer()
        for path in REQUIRED:
            if path == JOURNAL:
                continue
            if path.endswith(".json"):
                self.write(store.root, path, {})
            else:
                target = store.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("header\n")
        for name in PRESENTATION:
            self.write(store.root, f"data/presentation/{name}.json", {"name": name})
        (store.root / "scripts").mkdir()
        for script in ("v3_record_bbb_usdc_history.py", "v3_build_presentation_history.py"):
            (store.root / "scripts" / script).write_text("# deterministic fixture builder\n")
        first = store.publish()
        self.stage(store, "checkpoint after data generation")
        second = store.publish("checkpoint")
        self.assertEqual(second["parent_revision"], first["revision"])
        self.assertEqual(json.loads(store.blob(self.head(), "data/canonical/remote_only.json")),
                         {"authoritative": True})
        self.assertEqual(json.loads(store.blob(self.head(), "data/xnet_defillama_revenue.json")),
                         {"untouched": True})

    def test_checkpoint_preserves_unselected_candidate_then_full_publishes_it(self):
        """RealGit counterexample against the pre-fix finish(): after EVERY
        checkpoint the receipt full inventory was rematerialized locally and
        intentionally newer unselected owned files (network/data candidates
        gathered before the tracker intent) were overwritten with the
        inherited revision blobs — losing the source fetch while the journal
        clock in the same commit persisted. The fix rematerializes only the
        intent's selected destinations; unselected paths stay both local-newer
        and remote-prior until the next full publish accepts them together."""
        store = self.writer()
        # >= 1 full publish first so a later full generation is possible.
        for path in REQUIRED:
            if path == JOURNAL:
                continue
            if path.endswith(".json"):
                self.write(store.root, path, {"seed": True})
            else:
                target = store.root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("header\n")
        for name in PRESENTATION:
            self.write(store.root, f"data/presentation/{name}.json", {"name": name})
        (store.root / "scripts").mkdir()
        for script in ("v3_record_bbb_usdc_history.py", "v3_build_presentation_history.py"):
            (store.root / "scripts" / script).write_text("# deterministic fixture builder\n")
        store.publish()
        accepted = self.head()

        # Candidate fetches AFTER the accepted revision: owned files that a
        # tracker checkpoint intent does not select.
        candidate = {"generated_at": "2026-11-03T23:00:00Z",
                     "source": "network-crawl",
                     "devices": [{"day": "2026-11-03"}]}
        network_path = "data/network/device_history.json"
        prior_remote = json.loads(store.blob(accepted, network_path))
        self.assertNotEqual(prior_remote, candidate)
        self.write(store.root, network_path, candidate)
        self.stage(store, "tracker checkpoint")
        receipt = store.publish("checkpoint")
        self.assertEqual(receipt["parent_revision"], accepted)

        # Remote: exactly the journal advanced; unselected owned paths
        # (network candidates included) are never applied to the remote by a
        # checkpoint generation.
        changed = self.git(self.origin, "diff", "--name-only",
                           accepted, receipt["revision"])
        self.assertEqual(set(changed.splitlines()), {JOURNAL, GENERATION})
        self.assertEqual(json.loads(store.blob(receipt["revision"], network_path)),
                         prior_remote)
        # Local: the newer candidate is preserved byte-for-byte.
        self.assertEqual(json.loads((store.root / network_path).read_text()), candidate)
        self.assertEqual(json.loads(store.blob(receipt["revision"], JOURNAL))["marker"],
                         "tracker checkpoint")
        # Startup meanwhile rejects the intentional uncommitted candidate
        # until rehydrate; the active run owns its validated in-memory journal.
        with self.assertRaisesRegex(StateError, "diverges from receipt.*network"):
            store.load_authoritative_journal()

        # A full-mode alias/derived destination that drifted locally must be
        # mirrored back from the published commit when it IS selected.
        alias = store.root / "data/derived/network_history.json"
        alias.write_text('{"drifted": true}\n')

        # The next full publish accepts the newer candidate together with
        # its source metadata, from the exact checkpoint revision.
        self.stage(store, "full with candidate")
        final = store.publish()
        self.assertEqual(final["parent_revision"], receipt["revision"])
        self.assertEqual(json.loads(store.blob(final["revision"], network_path)),
                         candidate)
        self.assertEqual(json.loads(store.blob(final["revision"],
                                               "data/derived/network_history.json")),
                         {"name": "network_history"})
        self.assertEqual(self.head(), final["revision"])
        # Selected destinations (candidate files and the alias mirror) match
        # the accepted generation again and startup validates cleanly.
        self.assertEqual(json.loads((store.root / network_path).read_text()), candidate)
        self.assertEqual(json.loads(alias.read_text()), {"name": "network_history"})
        self.assertEqual(store.load_authoritative_journal()["marker"],
                         "full with candidate")

    def test_selected_write_failure_preserves_durable_pending_for_recovery(self):
        """A failed selected-file write raises BEFORE the receipt advances:
        the durable intent stays pending, the drifted local copy stays
        untouched, and a clean recovery then rewrites the selected
        destination from the committed blob."""
        store = self.writer()
        self.stage(store, "durable candidate")
        self.killed_publisher(store, False)  # commit built, push never happened
        intent = json.loads(store.pending_path.read_text())
        self.assertEqual(self.head(), self.base)
        # Selected destination drifts after the durable candidate.
        health = store.root / "data/current/xnet_chain_health.json"
        health.write_text('{"status": "DRIFT"}\n')
        import v3_live_state_store as module
        with patch.object(module, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.recover()
        self.assertTrue(store.pending_path.exists(),
                        "failed selected write must preserve the pending intent")
        self.assertEqual(json.loads(health.read_text()), {"status": "DRIFT"})
        # Explicit recovery: head already at the candidate (pushed in the
        # failed attempt), so no second push happens; the write succeeds now.
        receipt = store.recover()
        self.assertEqual(receipt["revision"], intent["revision"])
        self.assertEqual(self.head(), intent["revision"])
        self.assertFalse(store.pending_path.exists())
        self.assertEqual(json.loads(health.read_text()), {"status": "HEALTHY"})
        self.assertEqual(store.load_authoritative_journal()["marker"], "durable candidate")

    def test_cli_returns_machine_readable_checkpoint_receipt(self):
        store = self.writer()
        (store.root / "scripts").mkdir()
        for script in ("v3_publish_live_state.sh", "v3_live_state_store.py"):
            shutil.copy(ROOT / "scripts" / script, store.root / "scripts")
        self.stage(store, "CLI")
        result = subprocess.check_output(["bash", "scripts/v3_publish_live_state.sh",
                                          "--refresh-state-only"], cwd=store.root)
        self.assertEqual(json.loads(result)["revision"], self.head())

    def test_symlink_data_and_duplicate_json_keys_rejected(self):
        store = self.writer()
        target = store.root / "data/current/xnet_chain_health.json"
        target.unlink()
        target.symlink_to(store.root / JOURNAL)
        with self.assertRaisesRegex(StateError, "Unsafe"):
            store.hydrate()
        with self.assertRaises(StateError):
            validate_journal(b'{"schema_version":1,"queries":{},"queries":{}}')


if __name__ == "__main__":
    unittest.main()
