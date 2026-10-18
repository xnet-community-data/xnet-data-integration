# Astra delivery and production evidence — 18 October 2026

## Delivery boundary

PR [#40](https://github.com/xnet-community-data/xnet-data-integration/pull/40)
merged as `1f2a4ef16de1f4da1cbc83f7b6840607803ffb7e` at 03:32:44 UTC.
This deploys reliability controls, not a fabricated financial repair. The chain
pause, reconciliation quarantine and 1,800-credit emergency guard remain intact.
The mission's fully fresh chain/Dune ready-to-close standard is **not met**.

The initial CI exposed an undeclared test-only PyYAML dependency. A separate
commit pinned it; PR CI `39093951062` and main CI `39094004345` then passed.
The initial implementation has 287 tests. The rollout follow-up adds standalone
source-journal, queue and timezone regressions: **294 local tests pass**.
Production scripts remain standard-library-only.

## Directly verified operation

| Evidence | Observed result |
|---|---|
| Refresh run `39094004354` | Successful real source/API collection, bundle rebuild and publication; log explicitly shows `CACHED_ONLY: true`, not a paid Dune recovery |
| Credit monitor `39094004358` | Successful metadata-only usage publication through the new writer boundary |
| Revenue run `39094100490` | Successful feed-to-revenue-state and bundle publication, with existing automatic label logic unchanged |
| Pinned live-state `bf9a8351a7c0934cfaf4b568bf8824effd46015e` | Full generation `26950cdc-6aa1-4155-8f5a-ba8c0159e145`, sole parent `084ddcf55fda54a9d97eb9d34a9d2498575450ab` |
| Generation verification | All **34 selected-file SHA-256 checksums** verified against pinned remote blobs; the bundle exactly equals the four component objects |
| Network | Collected 03:32:57 UTC; measured offload through October 17: 4,193 GB; device observation remains October 16 06:36:03.611 UTC, 6,735 total / 5,968 operational |
| Market | Fetched 03:32:57 UTC; collector reports the 03:30 snapshot and four pools |
| Revenue state | Generated 03:34:36 UTC from the separately published canonical feed |
| Chain | Still paused; last completed October 16 20:59:18 UTC; zero pending execution entries at the pinned generation |

Git transport on the local workstation timed out. Deployment used `gh` and the
verified `xnet-community-data` identity instead, creating commits via GitHub's Git
API. Each uploaded source blob matched the reviewed local Git blob. The newer
automated `main` feed update was retained as parent, not overwritten. No journal
reset, history rewind, branch-protection bypass or other-account credential use
was performed. Local and remote commit hashes differ because their parents differ.

## Root causes addressed and review

- Exact-predecessor state publication, explicit revision receipts, durable
  candidate identity and lost-push-ack recovery across all live-state writers.
- Closed hydration ownership, preservation of code-owned seed inputs, validation
  of authoritative artifacts, and no missing-journal initialization.
- Atomic source data, provenance and consumption; rollback before publication;
  no compensating publication after an uncertain durability boundary.
- Ambiguous HTTP acceptance retained, known IDs reconciled without replacement,
  isolated source quarantine, staged presentation acceptance and retired legacy executor.
- Exact-execution bounded result reuse, durable pre-export intent and no automatic
  repeat paid download after an interrupted/uncertain attempt.
- Finite-input credit admission with actual renewal instants, comparable sample
  envelopes, export charges, unresolved liabilities and full scheduled obligations.
  Consumption is not mistaken for proof that billing already includes a charge.
- Independent source/API publication during chain holds and stable internal incident IDs.

Sol implemented and reviewed the initial publication architecture and real-Git
fault tests, then hit its session usage limit during integration. **There is no
final assembled Sol signoff.** Astra integrated the changes and DeepSeek performed
independent adversarial review. Found defects—seed deletion, hidden derived
artifacts, checkpoint clobbering of unselected candidates, cache keys missing the
execution ID, prune recovery and presentation acceptance ordering—were corrected
and covered by regression tests. No alternative paid worker was silently substituted.

The real rollout also demonstrated GitHub's default pending-run replacement:
`39094004383` was cancelled when another writer became pending. The metadata-only
revenue job was explicitly recovered (`39094100490`). The rollout follow-up uses
the officially documented `queue: max` on both shared writer groups, retaining
`cancel-in-progress: false`. Up to 100 pending runs can queue; this is not an
unbounded queue or a guarantee against GitHub cron delays. Cadences are unchanged.

## Dashboard and independent DeFiLlama

Two read-only dashboard inspections returned the same 58 visualization widgets,
13 text widgets, zero parameter widgets, IDs, positions and text. No dashboard,
visualization or SQL mutation was issued, and no dashboard/SQL configuration
files changed. The retired earnings section was not restored. A complete
before/after capture of all 58 visualization option objects was **not obtained**;
do not describe the layout/text comparison as that stronger proof.

DeFiLlama's public fees API returned the active XNET integration and nonzero
24h/7d/30d values (702 / 4,176 / 22,238 USD at inspection). The canonical feed
returned schema 4, measured offload/accrual through October 16, 776 daily rows,
and no active outage fallback. API display values are rounded; a live endpoint
does not independently prove every historical settlement. Offline accounting
checks and the source-faithful rebuild were preserved. **No external adapter
modification or PR was made or proposed.**

## Credit decision and remaining work

Official Dune usage metadata was checked twice: **1,798.673 credits used**, 4,000
quota, period October 4 16:35:49 UTC through **November 4 00:00 UTC**. Sustainable
target remains 2,500; emergency guard remains 1,800. Usage plus the existing
15-credit minimum reserve is already **1,813.673**, above the emergency guard,
before uncertain liabilities or scheduled obligations. No fresh Dune computation
or paid result export was performed for this mission.

The new forecast fails closed where comparable shapes, current billing inclusion
or export risk cannot be established. A complete affordable full-cadence forecast
has **not** been established; historical average spend is not a substitute. With
the current hold, no new scheduled Dune computation is admitted. Re-enabling it
requires verified source corrections, renewed cost/renewal evidence and explicit
user approval under the active workspace policy—not just `--resume` or an old cap.

Offline reconstruction reproduces the BBB holder deficit exactly from the static
seed plus published transfers. Changed-amount locator collisions also exist in
the legacy ledger. These observations do **not** establish which financial rows
should be deleted or rescaled. Destructive historical replacement remains held
until immutable scope, complete row identity, seed/overlap safety and provider
evidence are available. Do not guess amounts or fund a novel query without a
credible estimate. No broad backfill was attempted.

Subsequent bounded public Solana RPC inspection (six total calls, no Dune)
confirmed that the two implicated October 15/16 million-scale BBB rows match
on-chain raw balance deltas, slot, timestamp and eight-decimal scaling exactly.
They must **not** be divided by ten to make the ledger positive. This proves
only those rows, not the intervening coverage or all holder balances; see
[source evidence](HOLDER_SOURCE_EVIDENCE.md), which also retracts an initially
irrelevant pre-seed sample. The remaining deficit needs bridge/coverage evidence,
not an arbitrary adjustment or a reset of the holder seed.

Ordinary scheduled operation over a longer observation window, paid Dune
presentation refresh, complete visualization-option parity and final Sol review
remain unverified. Use the [runbook](RELIABILITY_RUNBOOK.md) and
[delta matrix](DELTA_MATRIX.md); rollback must preserve the latest journal,
accepted identities and outstanding obligations while moving state forward.
