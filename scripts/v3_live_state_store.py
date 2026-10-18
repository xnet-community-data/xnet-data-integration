#!/usr/bin/env python3
"""Small exact-predecessor Git state store. No Dune API calls.

Local receipts/push intents live in the Git directory, never in public schemas.
Every new commit has exactly the hydrated parent. An explicit lease is used as
compare-and-swap, NOT to rewrite history: the candidate always extends that parent.
No merge, rebase, reset, branch creation or automatic conflict retry is permitted.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import uuid

JOURNAL = "data/current/v3_refresh_state.json"
GENERATION = "state/v3_live_generation.json"
# Retention bound for the append-only execution journal history. The tracker
# caps execution_history at this size (production blob carries exactly
# 1000 rows today), so the bound is backward-safe: it only refuses journals
# that grew past the documented trim point.
MAX_EXECUTION_HISTORY_ROWS = 1000
EXACT = {"data/derived/xnet_owner_balances.csv", "data/derived/market_snapshots.csv",
         "data/derived/bbb_burn_daily.csv", "data/derived/bbb_trades_daily.csv",
         "data/xnet_supply_history.json", "data/xnet_supply_daily.csv"}
REQUIRED = (
    "data/canonical/xnet_transfers.csv", "data/canonical/bbb_trades.csv",
    "data/derived/market_snapshots.csv", "data/current/xnet_market_state.json",
    "data/history/bbb_wallet_usdc_daily.json", "data/xnet_supply_history.json",
    JOURNAL, "data/current/xnet_chain_snapshot.json", "data/current/xnet_live_bundle.json",
    "data/current/xnet_holder_state.json", "data/current/xnet_supply_state.json",
    "data/current/xnet_bbb_burn_state.json", "data/current/xnet_bbb_trade_state.json",
    "data/current/xnet_chain_health.json", "data/current/xnet_network_state.json",
    "data/current/xnet_revenue_state.json", "data/network/device_history.json",
    "data/network/offload_monthly.json", "data/derived/xnet_owner_balances.csv",
    "data/derived/bbb_burn_daily.csv", "data/derived/bbb_trades_daily.csv",
    "data/xnet_supply_daily.csv",
)
OPTIONAL = ("data/canonical/market_pair_snapshots.csv", "data/current/xnet_market_pairs.json",
            "data/history/circulating_supply_seed.json", "data/current/xnet_holder_integrity.json",
            "data/current/v3_operational_health.json", "data/current/v3_result_cache.json")
PRESENTATION = ("network_history", "commercial_history", "burn_history", "tokenomics_history",
                "holder_distribution", "market_pools")
PUBLICATION_PATHS = set(REQUIRED) | set(OPTIONAL) | {
    "data/current/v3_credit_usage.json", "data/derived/bbb_wallet_usdc_daily.json",
    *(f"data/derived/{name}.json" for name in PRESENTATION),
}
# Code-owned repository inputs that live under the same data/ prefixes as
# machine state. They are build inputs for main-branch scripts (e.g. the BBB
# burn seed read by scripts/v3_build_presentation_history.py during publish
# ("full"), and the current snapshot outputs of v3_build_current_snapshot.py).
# They are NOT machine-owned: live-state must never hydrate or delete them.
KEEP = {
    "data/history/bbb_burn_history_seed.csv",
    "data/current/xnet_current_snapshot.json",
    "data/current/xnet_current_snapshot.csv",
}
# Closed ownership registry: exactly the paths this store is responsible for.
# Data artifacts previously hidden behind an open prefix scan (the published
# data/derived/*.json charts, the BBB history mirror, credit usage) are owned,
# restored, and staleness-checked like every other authoritative file.
MACHINE_OWNED = PUBLICATION_PATHS | set(EXACT)
assert KEEP.isdisjoint(MACHINE_OWNED)


class StateError(RuntimeError):
    """Authoritative state is invalid or publication requires reconciliation."""


def object_id(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value)


def validate_owner_inventory(inventory, label):
    """Validate a receipt/intent authority inventory path->sha256 map."""
    if not isinstance(inventory, dict) or not inventory:
        raise StateError(f"{label} lacks the authoritative file inventory")
    for path, digest in inventory.items():
        if (not isinstance(path, str) or path not in MACHINE_OWNED
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)):
            raise StateError(f"Invalid {label} inventory path/checksum: {path}")
    if JOURNAL not in inventory:
        raise StateError(f"{label} inventory lacks the execution journal")


def decode_json(raw, label):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key}")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, UnicodeError) as error:
        raise StateError(f"Invalid {label}: {error}") from error


def validate_journal(raw):
    """Accept the existing v1 journal without inventing an empty replacement."""
    state = decode_json(raw, "execution journal")
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != 1:
        raise StateError("Missing/unsupported execution journal schema_version (expected 1)")
    if not isinstance(state.get("queries"), dict):
        raise StateError("Execution journal queries must be an object")
    if any(not isinstance(row, dict) for row in state["queries"].values()):
        raise StateError("Execution journal query entries must be objects")
    for name in ("pending_executions", "source_errors", "external_sources"):
        if name in state and not isinstance(state[name], dict):
            raise StateError(f"Execution journal {name} must be an object")
    for key, pending in state.get("pending_executions", {}).items():
        if (not isinstance(pending, dict) or pending.get("key") != key
                or type(pending.get("query_id")) is not int
                or not isinstance(pending.get("query_parameters"), dict)
                or not isinstance(pending.get("submitted_at_utc"), str)
                or "execution_id" not in pending
                or (pending["execution_id"] is not None and
                    (not isinstance(pending["execution_id"], str) or not pending["execution_id"]))):
            raise StateError(f"Malformed pending execution {key}; reviewed recovery required")
    for name in ("execution_history", "rejected_submissions"):
        if name in state and (not isinstance(state[name], list) or
                              any(not isinstance(row, dict) for row in state[name])):
            raise StateError(f"Execution journal {name} must be a list of objects")
    history = state.get("execution_history", [])
    if len(history) > MAX_EXECUTION_HISTORY_ROWS:
        raise StateError(
            f"Execution journal history ({len(history)} rows) exceeds the "
            f"retention bound {MAX_EXECUTION_HISTORY_ROWS}; reviewed history "
            "trim required before publication"
        )
    for row in state.get("execution_history", []):
        if (not isinstance(row.get("execution_id"), str) or not row["execution_id"]
                or type(row.get("query_id")) is not int
                or not isinstance(row.get("query_parameters"), dict)):
            raise StateError("Malformed execution history identity; reviewed recovery required")
    return state


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def atomic_write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".live-state-")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def durable_unlink(path):
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class LiveStateStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        gitdir = self.git("rev-parse", "--absolute-git-dir").decode().strip()
        self.local = Path(gitdir) / "xnet-live-state"
        self.local.mkdir(exist_ok=True)
        self.receipt_path = self.local / "receipt.json"
        self.pending_path = self.local / "pending.json"
        # Separate worktrees share Git refs but not receipts/locks. Isolate their
        # private refs, and anchor durable objects against Git garbage collection.
        self.refs = "refs/xnet-live-state/" + hashlib.sha256(str(self.local).encode()).hexdigest()[:16]

    def git(self, *args, env=None, input=None):
        proc = subprocess.run(["git", *args], cwd=self.root, env=env, input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode:
            # Do not print transport output: authenticated URLs may contain tokens.
            raise StateError(f"git {args[0]} failed (exit {proc.returncode}); no state receipt advanced")
        return proc.stdout

    @contextmanager
    def locked(self):
        with (self.local / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def remote(self):
        # Bind to the configured origin without persisting/printing credential URLs.
        return hashlib.sha256(self.git("remote", "get-url", "origin").strip()).hexdigest()

    def head(self):
        # Neither read nor overwrite FETCH_HEAD: unrelated fetches may use it.
        ref = self.refs + "/fetched"
        self.git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "origin",
                 f"+refs/heads/live-state:{ref}")
        return self.git("rev-parse", ref + "^{commit}").decode().strip()

    def blob(self, revision, path):
        return self.git("show", f"{revision}:{path}")

    def tree(self, revision):
        result = {}
        for entry in self.git("ls-tree", "-rz", revision).split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            path = path.decode()
            if self.allowed(path):
                if mode != "100644" or kind != "blob":
                    raise StateError(f"Non-regular authoritative data path: {path}")
                result[path] = oid
        return result

    @staticmethod
    def allowed(path):
        """Closed machine-ownership registry as a pickle path set.

        A remote path outside MACHINE_OWNED is never treated as authoritative
        machine state: it is neither materialized locally nor mirrored into
        staleness receipts, so the durable branch cannot silently inject new
        authoritative files (the previous open prefix accepted anything under
        data/canonical|current|network|history on sight).
        """
        return path in MACHINE_OWNED

    def target(self, path):
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise StateError("Unsafe data path")
        target = self.root / path
        if target.is_symlink() or self.root not in target.resolve().parents:
            raise StateError(f"Unsafe local path: {path}")
        # Disallow symlink directories even if their target happens to be in root.
        if any(parent.is_symlink() for parent in target.parents if parent != self.root):
            raise StateError(f"Symlink parent: {path}")
        return target

    def owned_revision_digests(self, revision):
        """SHA-256 inventory of the owned authoritative files on a revision.

        The receipt carries this map so startup can re-verify EVERY
        authoritative owned file against the receipted revision, not just
        the journal blob (which alone allowed tampered canonical or
        published chart files to pass silently).
        """
        digests = {}
        for entry in self.git("ls-tree", "-rz", revision).split(b"\0"):
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            path = path.decode()
            if not self.allowed(path):
                continue
            if mode != "100644" or kind != "blob":
                raise StateError(f"Non-regular authoritative data path: {path}")
            digests[path] = hashlib.sha256(self.blob(revision, path)).hexdigest()
        if JOURNAL not in digests:
            raise StateError(
                "Authoritative execution journal missing from ownership "
                "inventory; reviewed recovery required"
            )
        return digests

    def receipt(self):
        if not self.receipt_path.exists():
            raise StateError("No authoritative hydration receipt; hydrate before any operation")
        value = decode_json(self.receipt_path.read_bytes(), "local revision receipt")
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 2 or value.get("remote") != self.remote()
                or not object_id(value.get("revision"))):
            raise StateError("Invalid revision receipt or origin changed; rehydrate/replan")
        # The v2 receipt carries the authoritative file inventory; receipts from
        # an older store version cannot be silently extended and must rehydrate.
        validate_owner_inventory(value.get("authoritative_files"),
                                 "revision receipt")
        self.git("rev-parse", "--verify", value["revision"] + "^{commit}")
        validate_journal(self.blob(value["revision"], JOURNAL))
        return value

    def hydrate(self):
        with self.locked():
            if self.pending_path.exists():
                raise StateError("Unacknowledged publication; recover before hydration")
            revision = self.head()
            paths = self.tree(revision)
            if JOURNAL not in paths:
                raise StateError("Authoritative execution journal missing; refusing local fallback")
            validate_journal(self.blob(revision, JOURNAL))
            # Read/validate all bytes and all local paths before invalidating the old receipt.
            incoming = {path: self.blob(revision, path) for path in paths}
            history = "data/history/bbb_wallet_usdc_daily.json"
            legacy_history = "data/derived/bbb_wallet_usdc_daily.json"
            if history not in incoming and (self.target(history).exists() or
                    self.git("ls-tree", revision, "--", legacy_history)):
                raise StateError("Authoritative canonical BBB history missing; reviewed forward migration required")
            for path, raw in incoming.items():
                if path.endswith(".json"):
                    decode_json(raw, path)
                self.target(path)
            # Stale-residue scope is the closed machine-ownership registry, not
            # an open prefix: owned files absent from the authoritative tree are
            # cleared, while code-owned inputs (KEEP) and unrelated local paths
            # are preserved untouched.
            existing = set()
            for path in MACHINE_OWNED:
                if (self.root / path).exists():
                    self.target(path)  # raises on unsafe paths before mutation
                    existing.add(path)
            # A killed/partial hydration cannot leave an apparently usable receipt.
            durable_unlink(self.receipt_path)
            for path, raw in incoming.items():
                atomic_write(self.target(path), raw)
            for path in existing - paths.keys():
                self.target(path).unlink()
            receipt = {"schema_version": 2, "remote": self.remote(), "revision": revision,
                       "parent_revision": None, "generation_id": None,
                       "authoritative_files": {
                           path: hashlib.sha256(raw).hexdigest()
                           for path, raw in incoming.items()}}
            self.git("update-ref", self.refs + "/base", revision)
            atomic_write(self.receipt_path, encoded(receipt))
            return receipt

    def load_authoritative_journal(self):
        """Startup only: reject unhydrated/stale local residue, never default to {}.

        Validates the journal AND every receipted authoritative owned file
        (byte digest against the receipted revision), so tampered canonical
        data or published chart artifacts cannot pass startup.
        """
        with self.locked():
            if self.pending_path.exists():
                raise StateError("Publication unresolved; recover before execution")
            receipt = self.receipt()
            try:
                raw = self.target(JOURNAL).read_bytes()
            except FileNotFoundError as error:
                raise StateError("Local execution journal missing; hydrate before execution") from error
            if raw != self.blob(receipt["revision"], JOURNAL):
                raise StateError("Local journal differs from authoritative receipt; reconcile before execution")
            for path, digest in receipt["authoritative_files"].items():
                if path == JOURNAL:
                    continue  # already compared byte-for-byte above
                try:
                    local = self.target(path).read_bytes()
                except FileNotFoundError as error:
                    raise StateError(
                        f"Receipted authoritative file missing locally: {path}; "
                        "hydrate before execution"
                    ) from error
                if hashlib.sha256(local).hexdigest() != digest:
                    raise StateError(
                        f"Local authoritative file diverges from receipt "
                        f"{receipt['revision']}: {path}; reconcile before execution"
                    )
            return validate_journal(raw)

    def files(self, mode):
        if mode == "checkpoint":
            mapping = {JOURNAL: JOURNAL}
            health = "data/current/xnet_chain_health.json"
            if self.target(health).exists():
                mapping[health] = health
            internal = "data/current/v3_operational_health.json"
            if self.target(internal).exists():
                mapping[internal] = internal
            cache = "data/current/v3_result_cache.json"
            if self.target(cache).exists():
                mapping[cache] = cache
        elif mode == "credit-usage":
            mapping = {"data/current/v3_credit_usage.json": "data/current/v3_credit_usage.json"}
        else:
            mapping = {path: path for path in REQUIRED}
            mapping.update({path: path for path in OPTIONAL if self.target(path).exists()})
            mapping.update({f"data/derived/{name}.json": f"data/presentation/{name}.json"
                            for name in PRESENTATION})
            mapping["data/derived/bbb_wallet_usdc_daily.json"] = "data/history/bbb_wallet_usdc_daily.json"
        result = {}
        for destination, source in mapping.items():
            try:
                raw = self.target(source).read_bytes()
            except FileNotFoundError as error:
                raise StateError(f"Required publication input missing: {source}") from error
            if source.endswith(".json"):
                decode_json(raw, source)
            if source == JOURNAL:
                validate_journal(raw)
            result[destination] = raw
        return result

    @staticmethod
    def hashes(files):
        return {path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()}

    def push(self, commit, parent):
        # The candidate is constructed with this sole parent. The lease additionally
        # fences branch deletion/rewind races that a plain fast-forward push misses.
        if self.git("rev-list", "--parents", "-n", "1", commit).decode().split() != [commit, parent]:
            raise StateError("Candidate does not extend the exact predecessor")
        self.git("push", "--quiet", f"--force-with-lease=refs/heads/live-state:{parent}",
                 "origin", f"{commit}:refs/heads/live-state")

    def finish(self, intent, retry_push=True):
        # Validate the complete durable candidate BEFORE any recovery push. A corrupt
        # local intent must never authorize publishing a different/unverified tree.
        if (not isinstance(intent, dict) or type(intent.get("schema_version")) is not int
                or intent["schema_version"] != 2 or intent.get("remote") != self.remote()
                or not object_id(intent.get("revision"))
                or not object_id(intent.get("parent_revision"))
                or not isinstance(intent.get("generation_id"), str) or not intent["generation_id"]
                or intent.get("mode") not in {"full", "checkpoint", "credit-usage"}
                or not isinstance(intent.get("files"), dict) or not intent["files"]):
            raise StateError("Malformed durable publication intent; reviewed recovery required")
        for path, digest in intent["files"].items():
            if (path not in PUBLICATION_PATHS or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise StateError("Invalid publication intent path/checksum")
        # Receipts (and therefore intents) carry the full authoritative file
        # inventory; older-store intents cannot be validated and stay unusable.
        validate_owner_inventory(intent.get("authoritative_files"),
                                 "publication intent")
        commit, parent = intent["revision"], intent["parent_revision"]
        if self.git("rev-list", "--parents", "-n", "1", commit).decode().split() != [commit, parent]:
            raise StateError("Durable generation does not extend its exact predecessor")
        validate_journal(self.blob(parent, JOURNAL))
        validate_journal(self.blob(commit, JOURNAL))
        manifest = decode_json(self.blob(commit, GENERATION), "generation manifest")
        # The committed manifest keeps its own v1 schema (public data schemas
        # are unchanged); the durable intent is the v2 receipt envelope.
        if manifest != {"schema_version": 1,
                        "generation_id": intent["generation_id"],
                        "parent_revision": intent["parent_revision"],
                        "mode": intent["mode"], "files": intent["files"]}:
            raise StateError("Committed generation does not match durable publication intent")
        for path, digest in intent["files"].items():
            if hashlib.sha256(self.blob(commit, path)).hexdigest() != digest:
                raise StateError("Committed generation checksum mismatch")
        changed = set(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                               parent, commit).decode().rstrip("\0").split("\0"))
        if not changed <= set(intent["files"]) | {GENERATION}:
            raise StateError("Durable generation changes paths outside its manifest")
        head = self.head()
        if head == parent:
            if not retry_push:
                raise StateError("Publication not acknowledged; durable intent retained, recover explicitly")
            try:
                self.push(commit, parent)
            except StateError:
                pass  # Recovery pushes can also lose their acknowledgement.
            head = self.head()
            if head == parent:
                raise StateError("Publication not acknowledged; durable intent retained, recover explicitly")
        # Success despite lost push acknowledgement is proven by exact object identity
        # and reachability, never by comparing mutable filenames at the latest HEAD.
        self.git("merge-base", "--is-ancestor", commit, head)
        # Selected-destination materialization: reconcile only the intent's
        # own generation destinations (for "full" these include the alias
        # derived destinations, so published artifacts re-materialize their
        # local mirror). The bug this replaces: rematerializing every owned
        # path from the full receipt inventory after every checkpoint
        # overwrote intentionally newer unselected owned files (network and
        # data candidates gathered since the last accepted revision) with
        # the inherited revision blobs — losing the local source fetch while
        # the journal clock in the same commit persisted: an accepted clock
        # without data. Unselected owned paths are therefore preserved
        # byte-for-byte as intentional uncommitted changes; startup still
        # validates the whole receipted inventory and rejects them until
        # rehydrate, and the active run owns its validated in-memory
        # journal. The committed blob for a selected destination is already
        # verified against its manifest digest above, so any local divergence
        # is rewritten exactly; a write failure raises here, before the
        # receipt advances, so the durable intent stays pending and
        # recoverable ("Publication not acknowledged" state).
        for path in intent["files"]:
            target = self.target(path)
            raw = self.blob(commit, path)
            try:
                existing = target.read_bytes()
            except FileNotFoundError:
                existing = None
            if existing != raw:
                atomic_write(target, raw)
        receipt = {"schema_version": 2, "remote": intent["remote"],
                   "revision": commit, "parent_revision": parent,
                   "generation_id": intent["generation_id"],
                   "authoritative_files": intent["authoritative_files"]}
        self.git("update-ref", self.refs + "/base", commit)
        atomic_write(self.receipt_path, encoded(receipt))
        durable_unlink(self.pending_path)
        self.git("update-ref", "-d", self.refs + "/pending")
        return receipt

    def recover(self):
        with self.locked():
            if not self.pending_path.exists():
                return self.receipt()
            intent = decode_json(self.pending_path.read_bytes(), "publication intent")
            return self.finish(intent)

    def publish(self, mode="full"):
        with self.locked():
            if mode not in {"full", "checkpoint", "credit-usage"}:
                raise StateError("Unsupported publication mode")
            receipt = self.receipt()
            if self.pending_path.exists():
                intent = decode_json(self.pending_path.read_bytes(), "publication intent")
                if (not isinstance(intent, dict) or intent.get("remote") != self.remote() or intent.get("mode") != mode or
                        self.hashes(self.files(mode)) != intent.get("files")):
                    raise StateError("Unacknowledged generation differs from local candidate; explicit recover required")
                return self.finish(intent)
            parent = receipt["revision"]
            if self.head() != parent:
                raise StateError("live-state moved from hydrated revision; rehydrate and replan (no overlay)")
            if mode == "full":
                for script in ("v3_record_bbb_usdc_history.py", "v3_build_presentation_history.py"):
                    subprocess.run([os.sys.executable, str(self.root / "scripts" / script)],
                                   cwd=self.root, check=True)
            files = self.files(mode)
            generation = str(uuid.uuid4())
            manifest = {"schema_version": 1, "generation_id": generation,
                        "parent_revision": parent, "mode": mode, "files": self.hashes(files)}
            # A private index stages only the explicit allowlist over the exact base.
            # No checkout/index changes, and all unselected remote paths are preserved.
            with tempfile.TemporaryDirectory(dir=self.local) as temporary:
                env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / "index"),
                           GIT_AUTHOR_NAME="xnet-community-data-bot",
                           GIT_AUTHOR_EMAIL="xnet-community-data-bot@users.noreply.github.com",
                           GIT_COMMITTER_NAME="xnet-community-data-bot",
                           GIT_COMMITTER_EMAIL="xnet-community-data-bot@users.noreply.github.com")
                self.git("read-tree", parent, env=env)
                for path, raw in {**files, GENERATION: encoded(manifest)}.items():
                    oid = self.git("hash-object", "-w", "--stdin", input=raw).decode().strip()
                    self.git("update-index", "--add", "--cacheinfo", "100644", oid, path, env=env)
                tree = self.git("write-tree", env=env).decode().strip()
                commit = self.git("commit-tree", tree, "-p", parent, env=env,
                                  input=f"Publish XNET live state ({mode})\n".encode()).decode().strip()
            intent = {"schema_version": 2,
                      "remote": self.remote(),
                      "revision": commit,
                      "parent_revision": parent,
                      "generation_id": generation,
                      "mode": mode,
                      "files": manifest["files"],
                      # Authoritative inventory of the exact commit (owned
                      # paths only, including the unchanged inherited blobs),
                      # so the advanced receipt re-verifies everything.
                      "authoritative_files": self.owned_revision_digests(commit)}
            self.git("update-ref", self.refs + "/pending", commit)
            atomic_write(self.pending_path, encoded(intent))
            try:
                self.push(commit, parent)
            except StateError:
                # A push error can mean accepted-but-response-lost. Probe once using
                # the durable exact generation; failures leave intent for recovery.
                pass
            return self.finish(intent, retry_push=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("hydrate", "publish", "recover", "validate"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--refresh-state-only", action="store_true")
    modes.add_argument("--credit-usage-only", action="store_true")
    args = parser.parse_args()
    store = LiveStateStore(Path(__file__).resolve().parents[1])
    try:
        if args.command == "publish":
            result = store.publish("checkpoint" if args.refresh_state_only else
                                   "credit-usage" if args.credit_usage_only else "full")
        elif args.command == "validate":
            store.load_authoritative_journal()
            result = store.receipt()
        else:
            result = getattr(store, args.command)()
    except (StateError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Live state blocked: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
