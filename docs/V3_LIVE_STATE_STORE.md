# Exact-base live-state publication

## Boundary and invariants

`v3_live_state_store.py` is the common publication boundary for full data,
execution-state checkpoints, and credit usage. Existing shell calls remain valid.
The refresh, revenue, and credit-monitor workflows all hydrate first; the monitor
no longer switches its code checkout onto the data branch or commits directly.
The legacy `v3_refresh_chain.py` publication call also passes through this boundary.

1. Hydration fetches one immutable commit **H**, validates the authoritative v1
   execution journal and allowlisted JSON, and restores only regular machine-data
   files. Ownership over data/ prefixes is a **closed registry** (the published
   paths plus the exact machine-data allowlist). Files outside the registry,
   including code-owned build inputs (`data/history/bbb_burn_history_seed.csv`,
   `data/current/xnet_current_snapshot.json/.csv`), are preserved untouched and
   never hydrated. Registry-owned files absent from **H** are cleared; unrelated
   local files are left alone. There is no empty-journal fallback, orphan
   bootstrap, source-code checkout, or merge with old local journal/history. A
   missing/corrupt remote journal fails **before** replacing data. A
   failed/interrupted replacement invalidates the local hydration receipt.
2. Local metadata is under the checkout's Git directory, `xnet-live-state/`.
   `receipt.json` (schema 2) records the expected revision plus an inventory of
   every registry-owned path at that revision with its SHA-256
   (`authoritative_files`); `pending.json` (schema 2) additionally records the
   exact candidate commit, predecessor, generation UUID, and published-file
   SHA-256 checksums **before any push**. The committed generation manifest
   itself stays at schema 1 (public data schemas are unchanged). Legacy
   v1 receipts/intents from an older store version fail closed and require
   rehydration. Origin is represented by a fingerprint, never a credential URL.
   Per-worktree private refs anchor the receipt and pending Git objects against
   garbage collection; fetched revisions do not depend on shared `FETCH_HEAD`.
3. Publication checks that the remote is still **H** before running builders.
   It constructs a tree with a private Git index over **H**, replacing only the
   selected allowlist. Every candidate has exactly one parent, **H**. No merges,
   rebases, resetting history, automatic overlays, or branch creation occur.
4. Git's explicit `--force-with-lease=refs/heads/live-state:H` is used strictly as
   compare-and-swap. It is **not a history-rewriting push**: the store separately
   verifies that the candidate's sole parent is H. A plain non-force push alone
   would incorrectly accept a race where the branch was deleted or rewound to an
   ancestor after validation. The candidate is always a fast-forward from H.
5. An internal `state/v3_live_generation.json` commits the generation, predecessor,
   mode and selected-file checksums. Public data schemas are unchanged. Success
   requires proving that the exact candidate commit is reachable from fetched
   live-state and verifying its manifest/files. Only then is the receipt advanced.
   If another writer advances the branch after our commit, our receipt remains
   **our commit**, not that unrelated HEAD. The next publication will conflict and
   require rehydration/replanning.
6. Execution journals remain in Git history at every checkpoint. An unchanged
   checkpoint still receives a generation commit/receipt. Credit-only publication
   never copies the local journal or health. Full publication preserves remote
   credit usage and all other unselected paths. Checkpoints replace only journal
   and optional chain health, plus internal generation metadata.
7. The journal history is bounded: `validate_journal` accepts at most
   `MAX_EXECUTION_HISTORY_ROWS` (1000) rows, matching the documented production
   retention cap; a larger journal fails closed pending a reviewed history trim.
8. On success — full or checkpoint — the receipt is advanced with the inventory
   of the exact published commit, and any locally divergent registry-owned file
   is re-materialized from it. Startup validation (`load_authoritative_journal`)
   then checks **every** inventory entry, not just the journal: a missing or
   tampered canonical file, published chart artifact, or credit-usage report
   blocks execution with an exact reconciliation instruction.

## Caller integration

The refresh caller and execution tracker now use the store receipt as their
durability barrier. The tracker checkpoints intent before external execution,
preserves ambiguous outcomes, and does not allow acceptance/consumption to
advance past a failed checkpoint. Full publication includes the complete data
generation and consumed markers atomically; checkpoint-only publication is not
an acceptance substitute.

```python
from v3_live_state_store import LiveStateStore
store = LiveStateStore(ROOT)
state = store.load_authoritative_journal()  # startup only; no permissive default

# Durable checkpoint; the receipt is the publication authority.
receipt = store.publish("checkpoint")
# receipt["revision"] is the durable commit; the store also persists it locally.
```

`load_authoritative_journal()` verifies that the local journal matches the
receipt's exact Git blob. It intentionally rejects modified local residue and
unresolved publication. It does not hydrate or silently discard changes. It is
for startup, not for reloading an in-memory candidate during an active operation.
Each worktree/checkout must have only one logical producer; the store serializes
publication/hydration, not arbitrary producer mutations outside that boundary.

`v3_refresh_dashboard.py` exposes `--cached-only` for the network/source/cache
publication path that does not submit Dune executions or request Dune results.
It can still modify and publish data; it is not a read-only inspection command.
The legacy charged runner is retired and must not be used as a bypass. The BBB
path still depends on Dune and remains subject to the pause and explicit compute
approval rules.

## Recovery and migration

```sh
python scripts/v3_live_state_store.py validate
python scripts/v3_live_state_store.py hydrate
python scripts/v3_live_state_store.py recover
python scripts/v3_live_state_store.py publish --refresh-state-only
python scripts/v3_live_state_store.py publish --credit-usage-only
```

These commands can access Git and publish state; run only as part of an
authorized recovery plan. `validate` is a local/remote Git metadata check and
does not call Dune. No command above authorizes Dune execution or result export.

`recover` uses the durable exact candidate. If already present remotely, it returns
that receipt (even after a later writer). If the remote is still its exact parent,
it may retry that same candidate commit. If the branch has diverged/deleted/rewound,
it blocks. A same-mode publication retry may recover only when current selected
files equal the pending generation; it cannot acknowledge newly changed local data
as if it had been in the previous commit. Hydration refuses to overwrite pending
publication. No auto-abandon operation exists: preserve the candidate and journal,
review the conflict/obligations, and manually reconcile before a fresh hydration.

Migration is deliberately fail-closed:

- The journal shape is validated by the supported store/tracker contract. Missing/corrupt/unsupported
  journals need reviewed forward repair preserving known IDs and ambiguous intents;
  deployment must not bootstrap an empty one. No remote validation was performed
  as part of this offline change.
- Remote history is authoritative. The old hydrator's local BBB-history seed merge
  and `data/derived/bbb_wallet_usdc_daily.json` fallback are removed. Verify the
  canonical `data/history/bbb_wallet_usdc_daily.json` exists and retains the governed
  history before rollout; repair its location forward if required. Hydration blocks
  rather than silently deleting an existing canonical seed or ignoring a remote
  legacy-only history when the authoritative canonical path is missing.
- Deploy all writer workflows and the shared store together; an older writer can
  still bypass these guarantees. The same Actions concurrency group remains defense
  in depth, not correctness machinery.
- A diagnostic `pending.json` alone does not contain the candidate Git objects.
  An ephemeral runner lost before push needs its journal/objects or reviewed manual
  recovery. The durable remote intent still blocks replacement after uncertain ID
  persistence; this is not a claim of exactly-once Dune billing.
- No deployment is implied by this documentation. Dune computation/export still
  requires credible cost estimation and explicit approval under AGENTS.md.

## Offline verification

`python -m unittest tests.test_v3_live_state_store tests.test_v3_store_review`
exercises real Git commits/transports, two simultaneous writers, stale bases,
branch advance/rewind races, journal failures, checkpoint receipts/isolation,
lost push acknowledgement, rejected push, SIGKILL after commit and after push,
later-writer recovery, partial hydration, and full/credit-writer preservation.
`tests/test_v3_store_review.py` additionally validates the real production
journal bytes offline and holds the regression tests for the three review
counterexamples (code-owned inputs preserved, every published derived artifact
hydrates, startup detects tampered/missing receipted artifacts) plus the
bounded-journal/closed-registry contracts. Caller/tracker integration tests are
in `tests/test_v3_execution_tracker.py`, `tests/test_v3_tracker_boundaries.py`,
`tests/test_v3_acceptance.py`, and `tests/test_v3_result_cache.py`.
