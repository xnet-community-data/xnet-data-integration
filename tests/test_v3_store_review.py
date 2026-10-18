"""Bounded critical review of scripts/v3_live_state_store.py.

History: the three counterexamples below were INTENTIONALLY failing review
tests (F1 open-prefix hydration deleting repo-tracked inputs, F2 published
data/derived artifacts never restored by hydration, F3 startup validating
only the journal blob). The store owner fixed the implementation, so each
former counterexample is now a REGRESSION test asserting the fix. They must
not be weakened back to the buggy behavior.

The passing evidence tests stay: the force-with-lease proof is untouched, and
the open-durable-cache exposure test now documents the repaired contracts
(bounded history retention, closed ownership registry). One test validates
the real production journal bytes and required paths offline (no network).

Everything here uses local bare-repo transports only. No network, no Dune.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v3_live_state_store import (JOURNAL, KEEP, MACHINE_OWNED,
                                 MAX_EXECUTION_HISTORY_ROWS, OPTIONAL,
                                 PRESENTATION, PUBLICATION_PATHS, REQUIRED,
                                 LiveStateStore, StateError,
                                 validate_journal)

ROOT = Path(__file__).resolve().parents[1]
LIVE_STATE_REF = "refs/remotes/origin/live-state"


def run_git(cwd, *args, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    if check and result.returncode:
        raise AssertionError(f"git {args[0]} failed: {result.stderr.decode()}")
    return result


class StoreReviewTests(unittest.TestCase):
    def git(self, cwd, *args):
        return run_git(cwd, *args).stdout.decode().strip()

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
        # Canonical BBB history must exist or hydrate refuses (forward-migration guard).
        self.write(self.seed, "data/history/bbb_wallet_usdc_daily.json", {"data": []})
        # Published presentation-derived artifacts: live-state really carries these.
        self.derived = {}
        for name in PRESENTATION:
            value = {"authoritative": name}
            self.derived[name] = value
            self.write(self.seed, f"data/derived/{name}.json", value)
        self.write(self.seed, "data/derived/bbb_wallet_usdc_daily.json",
                   {"authoritative": "derived-mirror"})
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "seed")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "origin", "live-state")

    def writer(self, name="writer"):
        root = self.root / name
        root.mkdir()
        self.git(root, "init")
        self.git(root, "config", "user.name", "test")
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "remote", "add", "origin", str(self.origin))
        return LiveStateStore(root)

    def test_hydrate_preserves_code_owned_repo_inputs(self):
        """FORMER COUNTEREXAMPLE (F1), now asserted fixed: hydration keeps
        code-owned build inputs intact.

        `main` tracks inputs under the same data/ prefixes that live-state
        publishes into, e.g. data/history/bbb_burn_history_seed.csv (read by
        scripts/v3_build_presentation_history.py:341 during publish("full"))
        and the data/current/xnet_current_snapshot.* outputs. Ownership over
        those prefixes is now a closed registry (MACHINE_OWNED =
        PUBLICATION_PATHS | EXACT); anything outside it — including the
        explicit KEEP inputs — is preserved byte-for-byte by hydrate().
        """
        store = self.writer()
        inputs = {
            "data/history/bbb_burn_history_seed.csv":
                "day,xnet_burned,burn_transactions\n2026-01-01,1,1\n",
            "data/current/xnet_current_snapshot.json": '{"legacy": true}\n',
            "data/current/xnet_current_snapshot.csv": "day,supply\n2026-01-01,4\n",
        }
        for path, text in inputs.items():
            target = store.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        # The keep list is explicit, disjoint from machine ownership, and
        # excluded from both hydration destinations and residue deletion.
        self.assertEqual(KEEP, {
            "data/history/bbb_burn_history_seed.csv",
            "data/current/xnet_current_snapshot.json",
            "data/current/xnet_current_snapshot.csv",
        })
        self.assertTrue(KEEP.isdisjoint(MACHINE_OWNED))
        self.assertFalse(LiveStateStore.allowed(
            "data/history/bbb_burn_history_seed.csv"))
        store.hydrate()
        for path, text in inputs.items():
            self.assertEqual((store.root / path).read_text(), text,
                             f"hydrate disturbed tracked repo input {path}")

    def test_published_derived_artifacts_hydrate_and_stale_copies_clear(self):
        """FORMER COUNTEREXAMPLE (F2), now asserted fixed: every published
        path belongs to the closed ownership registry, so hydrate restores
        the authoritative bytes for the seven data/derived/*.json artifacts
        and clears any stale local copy that diverges from the revision.
        """
        hidden = sorted(p for p in PUBLICATION_PATHS
                        if not LiveStateStore.allowed(p))
        self.assertEqual(hidden, [],
                         "published paths must all be ownable by the store")
        store = self.writer()
        stale = {"stale": "must be replaced by hydrate"}
        # Stale local copies for every published derived JSON the remote seed
        # carries (the seven presentation artifacts plus the derived mirror);
        # these names come from the store's own PRESENTATION contract.
        remote_published_jsons = [f"data/derived/{name}.json" for name in PRESENTATION]
        remote_published_jsons.append("data/derived/bbb_wallet_usdc_daily.json")
        for path in remote_published_jsons:
            target = store.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(stale) + "\n")
        # One owned-but-unpublished REQUIRED file absent from the remote tree:
        # hydrate clears owned residue instead of leaving it divergent.
        owned_unpublished = store.root / "data/derived/market_snapshots.csv"
        owned_unpublished.parent.mkdir(exist_ok=True)
        owned_unpublished.write_text("stale,owned\n")
        store.hydrate()
        for path in remote_published_jsons:
            local = json.loads((store.root / path).read_text())
            authoritative = json.loads(self.git(
                self.origin, "show", f"live-state:{path}"))
            self.assertEqual(local, authoritative,
                             f"hydrate left a stale published artifact at {path}")
        self.assertFalse(owned_unpublished.exists(),
                         "owned local residue absent from the revision must clear")

    def test_startup_detects_tampered_published_artifact(self):
        """FORMER COUNTEREXAMPLE (F3), now asserted fixed: startup checks
        EVERY receipted authoritative owned file, not just the journal, so a
        tampered published presentation artifact is blocked with an exact
        reconciliation instruction. A clean hydrate plus untampered files
        still validates, and a legacy v1 receipt without the inventory is
        rejected instead of silently skipping file validation.
        """
        store = self.writer()
        store.hydrate()
        # Positive path first: receipt records the full owned inventory and
        # startup passes while every file matches.
        inventory = store.receipt()["authoritative_files"]
        self.assertIn("data/derived/burn_history.json", inventory)
        self.assertIn(JOURNAL, inventory)
        self.assertEqual(store.load_authoritative_journal(), self.initial)
        # Published chart artifact tampering is detected.
        target = store.root / "data/derived/burn_history.json"
        target.write_text('{"tampered": true}\n')
        with self.assertRaisesRegex(StateError, "diverges from receipt.*burn_history"):
            store.load_authoritative_journal()
        # Canonical file tampering is detected the same way.
        store.hydrate()
        (store.root / "data/current/xnet_chain_health.json").write_text(
            '{"status": "TAMPERED"}\n')
        with self.assertRaisesRegex(StateError, "diverges from receipt.*chain_health"):
            store.load_authoritative_journal()
        # A pre-inventory receipt cannot quietly skip file validation.
        store.hydrate()
        legacy = json.loads(store.receipt_path.read_text())
        legacy.pop("authoritative_files")
        legacy["schema_version"] = 1
        store.receipt_path.write_text(json.dumps(legacy))
        with self.assertRaises(StateError):
            store.load_authoritative_journal()

    # ------------------------------------------------------------------
    # Passing evidence (do not delete): these encode the force-with-lease
    # decision. They exercise raw git against a local bare repo only.
    # ------------------------------------------------------------------
    def test_plain_push_would_resurrect_an_ancestor_rewind_so_the_lease_is_load_bearing(self):
        """A non-force push is NOT equivalent to --force-with-lease.

        publish() only calls push() after asserting the remote is exactly
        `parent` (via head()), but that check and the push are not atomic. If a
        concurrent actor rewinds live-state to an *ancestor* of `parent`, the
        candidate commit still fast-forwards from that ancestor, so a plain
        (non-force) push would resurrect the abandoned branch state. Only the
        explicit lease (expect == parent) rejects it. `receive.denyNonFastForwards`
        does not help, because the push is a fast-forward.
        """
        base = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "commit", "--allow-empty", "-m", "first")
        first = self.git(self.seed, "rev-parse", "HEAD")
        self.git(self.seed, "commit", "--allow-empty", "-m", "candidate")
        candidate = self.git(self.seed, "rev-parse", "HEAD")

        # Rewind the remote to the ancestor (simulates a concurrent rollback).
        self.git(self.origin, "update-ref", "refs/heads/live-state", base)
        plain = run_git(self.seed, "push", "origin",
                        f"{candidate}:refs/heads/live-state", check=False)
        self.assertEqual(plain.returncode, 0,
                         "precondition: plain push is expected to fast-forward here")
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/heads/live-state"),
                         candidate, "plain push resurrected abandoned state")

        # Same race, now with the exact lease the store uses.
        self.git(self.origin, "update-ref", "refs/heads/live-state", base)
        leased = run_git(self.seed, "push", "--quiet",
                         f"--force-with-lease=refs/heads/live-state:{first}",
                         "origin", f"{candidate}:refs/heads/live-state", check=False)
        self.assertNotEqual(leased.returncode, 0, "expected the lease to reject the rewind")
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/heads/live-state"), base)

    def test_journal_history_is_bounded_and_hydration_is_allowlist_scoped(self):
        """FORMER EXPOSURE (open prefix cache, unbounded journal), repaired:

        1. `validate_journal` now bounds `execution_history` to
           MAX_EXECUTION_HISTORY_ROWS (1000). The production journal carries
           exactly 1000 rows, so the bound is backward-safe; a journal at the
           bound validates and one past it is refused with an exact error.
        2. `hydrate` materializes only paths inside the closed ownership
           registry (data/cache_events, data/canonical, data/counts,
           data/current, data/counts, data/current, data/network,  data/history,
           data/chain-health excluded etc.) — machine-owned extraction/monitoring
           paths, csv, and derived analysis artifacts). Anything outside it
           (e.g., injected_cache_row.json) is neither created nor maintained.
           Local unowned residue is preserved untouched.
        """
        bounded = {"schema_version": 1, "queries": {},
                   "execution_history": [{"execution_id": "e", "query_id": 1,
                                          "query_parameters": {}}] * MAX_EXECUTION_HISTORY_ROWS,
                   "external_sources": {}}
        self.assertEqual(len(validate_journal(json.dumps(bounded).encode())["execution_history"]),
                         MAX_EXECUTION_HISTORY_ROWS)
        over = dict(bounded, execution_history=bounded["execution_history"] +
                    [{"execution_id": "over", "query_id": 1, "query_parameters": {}}])
        with self.assertRaisesRegex(
                StateError, r"exceeds the retention bound "
                f"{MAX_EXECUTION_HISTORY_ROWS}"):
            validate_journal(json.dumps(over).encode())

        self.write(self.seed, "data/current/injected_cache_row.json",
                   {"token": "not-allowlisted"})
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "unvetted durable row")
        self.git(self.seed, "push", "origin", "live-state")
        self.assertNotIn("data/current/injected_cache_row.json", PUBLICATION_PATHS)
        store = self.writer()
        existing_local = store.root / "data/current/unowned_local_file.json"
        existing_local.parent.mkdir(parents=True, exist_ok=True)
        existing_local.write_text(json.dumps({"note": "unrelated local path"}) + "\n")
        store.hydrate()
        # Closed registry: the unvetted remote row is NOT materialized, and
        # the unrelated local file is preserved rather than deleted.
        self.assertFalse((store.root / "data/current/injected_cache_row.json").exists())
        self.assertTrue(existing_local.exists())

    def test_production_journal_bytes_and_required_paths_validate_offline(self):
        """Offline validation of the real live-state ref; no fetch/network."""
        exists = subprocess.run(["git", "show-ref", "--verify", "--quiet", LIVE_STATE_REF],
                                cwd=ROOT).returncode == 0
        if not exists:
            self.skipTest("refs/remotes/origin/live-state not present offline")
        tree = set(self.git(ROOT, "ls-tree", "-r", "--name-only", "origin/live-state").split())
        for path in REQUIRED:
            self.assertIn(path, tree, f"required live-state path missing: {path}")
        raw = run_git(ROOT, "show", f"origin/live-state:{JOURNAL}").stdout
        state = validate_journal(raw)
        self.assertEqual(state["schema_version"], 1)
        self.assertIsInstance(state["queries"], dict)
        # Optional artifacts may legitimately be absent from a pre-deploy ref; the
        # closed ownership registry — not pre-deployment presence — grants
        # ownership. The internal operational-health report is exactly such an
        # optional path: it is registered (allowed) but need not be in the tree.
        internal_health = "data/current/v3_operational_health.json"
        self.assertIn(internal_health, OPTIONAL)
        self.assertNotIn(internal_health, REQUIRED)
        for path in PUBLICATION_PATHS:
            self.assertTrue(LiveStateStore.allowed(path),
                            f"published path is not ownable by the store: {path}")
        required_published = PUBLICATION_PATHS - set(OPTIONAL)
        self.assertTrue(required_published.issubset(tree),
                        f"published-but-absent from live-state: "
                        f"{sorted(required_published - tree)}")


if __name__ == "__main__":
    unittest.main()
