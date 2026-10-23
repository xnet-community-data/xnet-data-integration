# Continued holder-opening review — 23 October 2026

**Subsequent operational resolution:** [finalized holder reconciliation](FINALIZED_HOLDER_RECONCILIATION.md)
replaces the live working basis with complete, same-slot account/mint snapshots.
The historical findings below are retained, not rewritten; the old seed is no
longer a prerequisite for correct current holder balances.

## Production fence is active, not merely configured

Read-only verification of live-state revision
`cb197e55a6631f0b021584e69dcfd833f9b0176f` after genuine scheduled run
[`39995929512`](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/39995929512)
(17:32 UTC) confirmed:

- The pre-seed review fence activated automatically at **10:11:24 UTC**.
  Both finalized transaction proofs replay successfully; their captured bytes,
  owner, raw balance and immutable seed hash still agree.
- Holder integrity remains explicitly **DEGRADED / holder_seed_review**.
  Both last-good holder files remain byte-for-byte unchanged. The health check is
  correctly red; this is **not** an all-green or repaired-holder claim.
- The runner is **not paused**. Transfer collection advanced through
  **16:22:42 UTC**, with 4,767 unique canonical transfers. The latest returned
  event is **10:27:28 UTC**; collection coverage is not provider index completeness.
- A separately proven indexing omission triggered **one** precisely quoted day
  recheck at 13:26 UTC, adding **22** genuine transfers. Its positive witness is
  now resolved. This is permitted evidence-driven recovery, not speculative seed
  repair. There were **no new wide manifests** after the 09:36 comparison baseline.
- All **1,419** comparison-baseline charge obligations remain, now **1,451** in
  total. All **18** legacy unknowns remain reserved; no new unknown charges arose.
  Added known journal charges were approximately **4.269 credits**, including
  ordinary scheduled collection/presentation and the witnessed repair. This is
  not a claim that every journal charge is already included in the account counter.
- Provider usage was **1,860.099 / 4,000 credits**. Normal cadence remained admitted
  at multiplier 1, with no pending new spending reservations. Target, ceiling,
  reserves and standing policy hash are unchanged.

## Historical funding was reviewed further

The omitted owner and token account are both
`As4nhNHHrhNZMoReGZvEbDTiooNmNATjyjjbadArEJVb`.

Fully decoded, finalized instructions and exact transaction balance flow show:

| UTC transaction time | Proven raw pre-balance | Proven raw post-balance |
|---|---:|---:|
| 2024-08-14 23:37:26 | New account | 716358500000000 |
| 2025-05-02 20:24:48 | 716358500000000 | 680540575000004 |
| 2025-05-13 20:22:14 | 680540575000004 | 668601266666670 |
| 2026-10-23 05:47:29 | 668601266666670 | 568216358333337 |

The original funding transaction is
`5Pai1jqzCSTYcRm1bADWbfoqpBfBMoA6NPau8wrE8V5DbhmjRBnTvB4xC1YePWfhVtH9ASr9TmmYkuGRpthf6PoW`;
the May 2 withdrawal is
`5GE2DKe8s1L28qazeRSSg4iqt4stYqwbYSNqJy6qtHzjGKx69f92R653s9oHqWEukUxAc4p5LXm6rTFAo9ZbXx33`.
The May 13/October 23 captures are retained in the production review evidence.
The intervening August 2025 SOL/memo spam transaction is not treated as an XNET
transfer. Sparse RPC signature history is not asserted to prove complete history
or the exact balance at an arbitrary historical cutoff.

The original funding capture contains SPL's **read-only `getAccountDataSize`**
query. The decoder now recognizes precisely that operation without inventing a
financial leg. Account initialization, all actual transfer instructions, owner
identity, raw amounts and token balance flow must still validate. Authority
changes, unknown relevant instructions and unexplained balance changes remain
refused. This also removes a recurring non-mutating instruction obstacle in the
bounded free indexer probe; request limits and paid-operation authority do not
increase.

## Why this is not permission to insert 6.686 million tokens

The seed came from the one-time owner query over `solana_utils.latest_balances`
(source query 8889854; repository definition `dune/v3/sql/30_owner_balances_current.sql`).
It is not a cryptographically complete opening snapshot merely because later
arithmetic conserves its own starting sum.

A separate free finalized `getTokenSupply` observation at **18:01:31 UTC**, slot
**460537447**, reported total mint supply **1,289,244,880.79419788 XNET**. The seed
plus the currently accepted post-seed mint/burn ledger reconstructs an owner total
of **1,181,436,931.57616996 XNET**. These snapshots are **not time-aligned** and
indexed mint/burn completeness is not established, so the difference
**107,807,949.21802792** is an investigation signal, **not** a seed adjustment or
proof that exactly that quantity was missing at the seed cutoff. Mint total is
also not the dashboard's excluded-wallet circulating-supply metric.

Consequently, patching only this owner would not establish a globally reconciled
opening basis. No balance, transfer, owner seed, supply checkpoint, accounting
definition or external adapter was changed by this review.

**Unresolved prerequisite for a full all-clear:** establish a reviewed, complete
opening-balance attribution and reconcile it to the mint and accepted post-seed
events at compatible cutoffs. A new seed or explicit opening correction would
change protected financial facts; ordinary standing spend authority does not
silently authorize that change. Until then, the system continues collecting and
performing bounded evidence-driven repairs, but preserves last-good holders and
keeps the review visible rather than guessing an opening balance.
