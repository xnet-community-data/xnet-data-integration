# Autonomous system audit — 23 October 2026

## Scope and live baseline

Reviewed the complete scheduled path: source/feed collection, Dune admission,
execution identity and lifecycle, interrupted downloads, canonical reduction,
holder/supply checks, source reconciliation, presentation scheduling, exact-base
publication, billing renewal and operational alerting. External adapter accounting,
SQL meaning, seeds and dashboard layout are unchanged.

The overnight baseline was **not just green workflow jobs**: accepted transfer
coverage advanced through 06:07 UTC, canonical rows reached 4,703, holder integrity
was healthy, all prior financial facts/obligations remained present, and provider
usage was 1,852.060 / 4,000. Revenue-source sync, independent Dune revenue
publication and credit monitoring also completed successfully on their schedules.

## Concrete gaps hardened

1. **False-green service status.** A successful process could preserve stale data
   behind a source hold indefinitely. A read-only watchdog now checks accepted
   collection coverage, presentation execution age, source holds, standing policy,
   holder integrity and independent API/feed clocks. It runs after every refresh
   and in a separately scheduled hourly workflow. A stalled service fails the
   check and appears in GitHub Actions summaries/artifacts; it does not pause the
   runner or prevent automatic recovery. The observer has read-only GitHub access,
   no Dune secret, no Dune calls and no publication permission.
2. **Interrupted-read reservation/identity gap.** A new retry could be mistaken
   for the unknown charge of a different read sharing the same execution. Each
   new quoted read now explicitly identifies the old liabilities it is NOT
   replacing. Those old obligations remain fully accounted for. The retry archive,
   new requested marker and exact new financial attempt cross **one checkpoint**,
   not two checkpoints separated by a crash window.
3. **Cache backpressure after spending.** A full two-entry cache was previously
   detected only after downloading a third result. Capacity is now checked before
   new source compute and before a read/reservation. Unconsumed results are never
   evicted; this condition defers automatically, while presentations/independent
   domains continue. Post-download validation remains a second line of defense.
4. **Authority expiring during a slow checkpoint.** Normal execution approvals,
   not only UTC-day boundaries, are rechecked after the durable intent and before
   the execute transport. Proven local non-submission retains an audited zero-cost
   intent; no provider execution is guessed. Export approval is also rechecked
   immediately before transport. Its uncertain intent/risk is conservatively
   retained for a fresh bounded retry rather than fabricating charge settlement.
5. **Presentation starvation.** A repeatedly failing early chart could consume the
   same bounded slots every run, starving later daily charts. Source-invalidated
   charts retain priority; attempts now rotate within each priority class using
   a durable last-attempt clock. Per-run query count and ordinary budgets do not
   increase.
6. **Unbridgeable gaps wasting repair credits.** Beyond the already authorized
   60-day recovery horizon, recent-day scans cannot bridge the missing frontier.
   The planner no longer repeatedly buys such scans. It retains evidence, refuses
   unsupported new scopes and surfaces the stalled source instead of claiming
   successful coverage.
7. **Lost financial book after activation.** A previously activated service cannot
   silently create a fresh spending anchor if its durable risk book disappears.
   New spending refuses that state and the observer reports the corruption. A
   genuinely new verified billing period still renews normally within the existing
   book; this guard does not prevent the approved renewal.
8. **Provider transport edge cases.** Ordinary Dune API requests now refuse
   redirects instead of forwarding credentials or a paid POST to another URL.
   Non-object metadata produces a typed retryable error rather than an accidental
   global pause. Incomplete result transport retains its observed charge and exact
   execution and uses the bounded fresh-read recovery path, not a permanent manual
   hold. A schema/financial contradiction remains refused.
9. **Mutable CLI dependency.** The nightly label updater previously executed the
   upstream `main` installer and selected a changing latest CLI beside the Dune
   key. It now downloads the same v0.1.18 release already used successfully in
   production, verifies its hardcoded SHA-256 before extraction/execution, and
   installs it in a step without the Dune secret. Counter logic/layout is unchanged;
   no query execution or export is introduced by this metadata-only updater.
10. **Late indexing outside the hot overlap.** A finalized-chain cross-check
    proved actual transfers absent from the indexed ledger even though collection
    checks were succeeding. A bounded free probe now samples at most two
    32-signature pages and two finalized transactions every six hours (one-hour
    retry on probe failure). Remembering the prior head lets it inspect mature
    activity even during high traffic. A mint mention alone is never a transfer:
    complete decoded instructions and balance-flow proof are required.

    Positive missing/conflicting witnesses enqueue the existing separately quoted
    UTC-day repair family. Known omissions stay visible, cannot advance a verified
    repair frontier, and are not marked resolved merely because another query
    completed. Unsuccessful day rechecks back off from 6 to 12 to 24 hours; their
    expected cost enters the period forecast. The probe never inserts RPC rows or
    claims whole-mint completeness from a small sample.

    Holder recovery also checks **today first**, then recent days, rather than
    spending on a week of older days before the affected day. Optional scans stop
    as soon as integrity is restored. Expensive wide integrity checks are bounded
    by the existing daily cooldown and three-attempt incident limit; precise-day
    recovery continues without buying a wide scan on every cron.

## Safety/availability boundaries checked

| Failure/transition | Required behavior |
|---|---|
| Provider timeout / known execution ID | Poll/reuse the exact execution; never resubmit it |
| Lost submission response / no ID | Deny speculative new paid submissions; alert |
| Billing/MCP unavailable | Deny new spending, keep free feeds and known-result recovery available, recheck |
| Cost outlier | Record actual charge; reprice future work, never rewrite an unknown bill |
| Cache/download interruption | Fresh read approval, old liabilities retained, at most three read attempts/execution/UTC day |
| Cache full | No new compute/read purchase; no eviction of unconsumed evidence |
| Duplicate locator / new account | Complete finalized instruction proof and token balance-flow reconciliation |
| Holder deficit | Explicit last-good/degraded holder state; bounded additive-day repair, no material clamp or seed reset |
| Midnight / current partial day | Bind original engine day; advance only the verified contiguous frontier |
| Publication response lost / moved predecessor | Exact generation/receipt barrier; no stale compensating write |
| One source or presentation fails | Preserve last good, bounded retry/backoff, fair opportunities for other domains |
| Billing period renews | Exact fresh non-overlapping period; carry unknown risks, do not reset liabilities |
| Scheduler/poller stalls | Independently scheduled read-only observer reports missing accepted coverage |
| Finalized transfers escape hot overlap | Retain exact witnesses, price targeted day rechecks, back off nonprogress and surface indexer lag |

The existing fault-injection suites cover publication crashes before/after
acceptance, lost HTTP responses, tampered cache data, outliers, scoped SQL drift,
renewal, lower usage counters, carryover liabilities, feed isolation and exact
bundle agreement. Added regressions exercise the concrete gaps above without
performing paid investigation.

An **offline simulation using the production journal** confirmed that a lower
account counter cannot reduce the usage high-water and that verified November
renewal admits normal operation while retaining all 1,401 baseline obligations
and 18 legacy unknowns. This is a simulation, not a claim that the future billing
renewal has already happened in production.

## What “autonomous” does not guarantee

- Dune estimates and local admission controls are not provider-enforced caps.
  Actual charges may exceed quotes; new spending then uses the observed higher
  exposure. The approved target/reserves and unspent-quota fence remain unchanged.
- Query completion proves a collection attempt/snapshot, not whole-chain indexer
  completeness or fresh activity on a quiet token. Event timestamps stay distinct
  from collection coverage and newly generated presentation timestamps.
- A revoked credential, unprovable acceptance, real contradictory financial
  correction, unauthorized SQL/policy change or historical scope outside approval
  may require administrative review. Guessing those facts is not safe autonomy.
- GitHub’s scheduled jobs can be delayed; grace windows accommodate ordinary
  delays. The independent observer detects a stopped writer while GitHub Actions
  still runs. A platform-wide GitHub outage cannot be detected from within GitHub
  itself; external monitoring would be a separate service decision.
- Failed checks use standard GitHub Actions visibility/notifications. Actual email
  or mobile delivery depends on repository/account notification settings; no new
  notification service was silently connected.

The goal is bounded self-recovery for routine failures, fair ongoing collection,
preserved financial meaning, and visible failure when safe automatic recovery is
impossible—not a promise that upstream services or credentials can never fail.

## Scheduled verification exposed a historical boundary

Do **not** read the healthy overnight baseline above as a final all-clear.
Scheduled run `39923114670` correctly failed the new service-health check after
new indexed outflows exposed a material holder deficit. Scheduled run
`39928219403` then exercised the day-recovery follow-up without dispatch/resume:
it added **20** genuine late-indexed 22 October rows and retained all predecessor
facts, but holder integrity **remained degraded**. Ordinary collection/publication
continued and the last verified holder files were preserved exactly.

Free finalized evidence narrowed that remaining failure beyond an indexing delay:

- Owner/token account `As4nhNHHrhNZMoReGZvEbDTiooNmNATjyjjbadArEJVb` is absent
  from the immutable 3 October owner seed.
- A fully decoded 13 May **2025** transaction
  `ciG7yt1Lmj1givcBTAUGTZe29tyR4jikCQDPjixS1LGFg9p6nKZFenLRNXEd5aMbMFyLQrpUc8jHwatBNJeLara`
  proves its closing balance of **668601266666670 raw units**.
- A fully decoded 23 October **2026** transaction
  `5jVXNRNPHwVdjBa4MnFaBYNPQRt6SF2Kfyapn5KdVQKT7d7s6sZ8rjG7bWzuXWhw4sBRqBKMmvXAu1nBK9dHMyoY`
  has that **same opening balance** and proves the newly indexed withdrawals.
  Owner attribution, exact instructions and transaction balance flow agree.
- Sparse signature history is **not** claimed as complete historical coverage.
  Equal positive snapshots on either side of the seed are positive grounds for
  review, not permission to invent an opening balance or missing transfer.

The generalized free diagnostic retains both captures/receipts, the signature
metadata, exact seed hash and explicit uncertainty. It makes at most one
signature-page read plus three transaction reads per 24 hours until classified.
Once a pre-seed conflict requires review, speculative holder-day sweeps and broad
holder manifests stop; independently proven day omissions and ordinary collectors
remain eligible under the same standing budget. No global pause/resume is added.

The holder reducer preserves last-good files even if later inflows would make an
arithmetic balance positive: a known opening conflict cannot be silently cleared
by unrelated new activity. The observer stays red with the actual review reason.
Neither canonical rows nor seed entries are replaced or adjusted automatically.

The free indexer sampler also rotates attempted candidates so unsupported early
instructions cannot consume both proof slots forever. Malformed non-object RPC
metadata and extreme timestamps fail/defer without turning into a global pause.

**Remaining blocker:** holder accounting cannot honestly be called repaired until
the historical opening-balance attribution is reviewed. Automatically adding
6,686,012.66666670 tokens to a seed could double-count supply or misattribute
another opening owner. Standing spend approval is not approval to change those
financial facts. This boundary is deliberately visible and financially fenced,
not disguised as successful autonomous recovery.

The [continued holder-opening review](HOLDER_OPENING_REVIEW_2026-10-23.md)
records subsequent genuine scheduled verification, original funding proof,
bounded operational charges, and the independent mint-total check. The production
review fence is verified active; the historical accounting prerequisite remains
unresolved, not represented as an all-clear.
