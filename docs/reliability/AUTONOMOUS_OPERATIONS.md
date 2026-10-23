# Autonomous XNET operations

## Standing authority

On 20 October 2026 the user explicitly confirmed **Approve standing autonomy**:
recurring collection, repair, presentation and bounded recovery, renewed with
verified billing periods until revoked. This replaces the earlier batch-only
spending restriction; it does not erase its liabilities or change accounting.

The reviewed policy is in `config/v3_refresh.json.autonomous_operation`. A matching
`v3_refresh_state.json.standing_authorization` must also be installed. Changing
configuration alone cannot create authority. Every paid execution/read still
gets a fresh, evidence-backed quote, exact operation ID and durable reservation
before transport. Estimates and local limits are **not provider-enforced caps**.

| Control | Approved policy |
|---|---:|
| Operating target per billing period | 2,500 credits |
| Hard admission ceiling | min(3,000, current quota − 1,000) |
| Safety reserve | 100 credits |
| Legacy unknown-charge risk allocation | 100 credits, not a claimed charge |
| Daily new-operation risk allowance | 100 credits |
| Result read attempts | at most 3 per execution per UTC day, separately reserved |

The initial cadence analysis forecast approximately **330–450 additional credits
through 4 November**, before unresolved billing adjustments. Admission recomputes
from current usage, observed costs, reservations and remaining time; it does not
treat that forecast as a guarantee or repeat a historical worst-case at every run.

The actual production-journal shadow check on 20 October returned **430.919
credits expected through renewal**, normal cadence multiplier **1**, and no
admission blockers. Official usage was **1,803.249 / 4,000**. All 18 unknown
legacy charges remained unknown; **254.814** known opening charges were also
conservatively reserved against the hard ceiling, without claiming inclusion in
the account counter. The operating target paces expected future work separately
from those uncertain historical inclusions; it does not remove their hard reserve.

For scale, the normal six-hour transfer quote was **32.494 credits** versus
**0.699 expected**, retaining the historical outlier in the risk envelope. The
current snapshot quote retained its **12.418-credit** observed outlier versus
**0.366 expected**. Actual catch-up parameters are always repriced; a normal-window
quote cannot authorize a wider query. The live MCP metadata transport was also
verified, not only mocked in tests.

## Normal rotation and pacing

- Transfers every **120 minutes**, normally with a **six-hour overlapping window**.
- Current snapshot and pools every **60 minutes**.
- Burns, tokenomics and holder presentations every **120 minutes**.
- BBB, network and commercial presentations **daily**; free/API feeds remain independent.
- Historical manifest **weekly**; shorter checks/complete-day repair only for
  detected gaps or mismatches, not a costly daily full-history download.

If the remaining forecast does not fit, the runner lengthens ordinary cadence
automatically. It never widens a paid operation without pricing the actual new
parameters. Unknown estimates or changed saved SQL isolate that operation instead
of silently spending. Account/quota exhaustion pauses new paid operations and
rechecks automatically; no daily manual resume is needed.

## Billing and financial safety

`v3_dune_metadata.py` reads **getUsage from the official Dune MCP server** using
`DUNE_API_KEY` from the environment. Exact billing instants are required. The
date-only regular usage response is not relabelled as exact midnight. A new
billing book requires fresh provider evidence and a non-overlapping new period;
unused allocations cannot be replenished by changing period keys.

`v3_standing_budget.py` is an admission ledger, not settlement. The financial
journal remains the durable charge record. Opening known charges are
conservatively accounted for; legacy unknowns remain unknown in their separately
authorized risk allocation. New operations retain their reservation until exact
actual charge evidence is available. Confirmed account usage uses a monotonic
high-water mark; lower counters cannot reopen spent headroom. Outstanding reads
and lost responses survive process restarts and renewal. Aggregate usage equality
never marks an individual charge settled.

## Recovery without routine operator intervention

- Known execution IDs are reconciled/reused, not resubmitted on timeout.
- Each repeated result download is a new quoted, reserved read. Original attempts
  and unknown costs remain recorded; a fresh UTC day does not erase them.
- A failed source or presentation backs off independently. Already paid cost
  outliers remain available for data validation; future submissions require a
  fresh quote addressing the outlier and its finite cooldown.
- Complete-day recovery pins the absolute UTC day and approved SQL before submit,
  checks the provider's actual execution-start day, verifies every existing row,
  and accepts **additions only**. It preserves every predecessor CSV byte and
  publishes its receipt, derived state and consumption marker atomically.
- Days at or before the owner-seed cutoff are excluded **before pricing/execution**.
  Their aggregate anomalies remain advisory evidence for seed-aware review; they
  cannot trigger a paid full-day replay or starve post-seed verification.
- A free finalized-chain sampling probe detects positive omissions older than the
  normal hot overlap. Its witnesses trigger priced, bounded day rechecks with
  6/12/24-hour nonprogress backoff. Sampling silence is not whole-mint coverage,
  and a fresh query does not clear a known missing witness. Current-day holder
  repair takes priority over older advisory scans; optional work stops once fixed.
- A bounded free diagnostic can distinguish a positive **pre-seed opening
  conflict** from a missing recent day. Such evidence stops speculative holder
  backfills, retains last-good holder files and keeps the health check red for
  review; it never resets a seed or invents a balance adjustment. Other collectors,
  precisely witnessed day recovery and independent feeds remain autonomous.
- Holder last-good fallback, when needed during multi-day recovery, remains
  explicitly degraded; it never becomes a claim of repaired balances. No seed
  resets, amount rescaling, invented routed legs, negative-balance clamping or
  destructive row replacement are permitted.

Genuinely ambiguous submissions without a provider ID, tampered state, schema/SQL
changes or corrections requiring instruction-level deletion/replacement still
fail safely and surface in operational health. Autonomy does not mean guessing
financial facts. Other verified feeds continue where their own authority permits.

## Instruction-backed transfer recovery (22 October follow-up)

Multiple amounts on one transaction/account pair are not necessarily duplicate
events. The runner now resolves this pattern against finalized Solana
`getTransaction` evidence using the existing ordinal-aware decoder. Every leg,
owner, amount, multiplicity, slot and timestamp must match; decoded instruction
flows must reconcile pre/post token balances. The original capture, checksum and
ordinal receipt are retained in the financial-state journal for offline replay.
No balance-delta inference, arbitrary deduplication, amount correction, row
deletion or seed change is permitted.

- Already accepted multi-leg groups need no repeat RPC request.
- New ambiguous groups use at most four free transaction requests per validation
  (at most eight in a two-day recovery run), with
  a 15-second timeout and a 2 MB response bound each. Verified progress survives
  a later deferral. RPC outages retry with backoff using the same paid Dune
  result; they do not resubmit compute or buy another download.
- The specific legacy duplicate-locator hold may be automatically revalidated
  against its exact completed execution. Unrelated manual holds are not cleared.
- Gaps over 12 hours route through bounded one-UTC-day queries instead of an
  increasingly expensive hot window. At most two day scopes per run retain the
  existing authority and sample-backed admission. Closed-day verification takes
  priority over older advisory scans. A current-day result advances coverage only
  to the original engine snapshot, **never** to a future midnight.
- Any transient holder imbalance retains the last-good holder state explicitly
  degraded while complete-day additive verification fills late-indexed inflows.
  A green workflow is not proof of fresh transfer/holder data; inspect accepted
  source coverage and integrity state as well as the workflow conclusion.

## Deployment and observation

The system-wide follow-up audit and fault boundaries are recorded in
[`SYSTEM_ROBUSTNESS_AUDIT_2026-10-23.md`](SYSTEM_ROBUSTNESS_AUDIT_2026-10-23.md).
Current holder recovery is described in
[`FINALIZED_HOLDER_RECONCILIATION.md`](FINALIZED_HOLDER_RECONCILIATION.md): a
renewable same-finalized-bank account inventory conserves mint supply exactly and
does not depend on silently correcting the original historical seed. Source
activation is a one-time cached-only operation; later scheduled renewal is free
and autonomous under its own durable, non-spending authority.
Every refresh now runs a read-only service-health gate, with a separate hourly
observer for a stopped writer. It checks accepted coverage, not merely a green
job/generated timestamp or the age of a quiet-chain event. A red health check
does **not** set an operator pause or require manual resume: recovery continues
and the check becomes green again when service evidence is healthy. Standard
GitHub notification delivery depends on account/repository settings.

GitHub's existing 15-minute scheduler starts the runner; its per-source cadence
decides which work is due. Push deployments remain **cached-only**, so a code
merge cannot silently buy verification work. Production activation and a normal
scheduled/dispatch run are verified separately.

Inspect `data/current/v3_operational_health.json`, the standing budget decision,
exact source timestamps and workflow summaries. A successful job or a newly
generated bundle is not proof that the chain/indexer has emitted new events.
The public dashboard layout, accounting definitions and external DeFiLlama
adapter remain unchanged.
