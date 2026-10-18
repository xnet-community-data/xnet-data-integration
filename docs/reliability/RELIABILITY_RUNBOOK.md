# XNET reliability runbook

**State:** integration work is pending deployment. This document describes the assembled main-branch code and known evidence, not a claim that production is deployed or recovered. The published journal still records the holder-integrity pause. Do not resume it based on workflow status.

## Data/control flow

```text
network/offload/device inputs ──> network sync ──┐
market provider inputs ─────────> market collect ├─> full data build + consumed markers
revenue source feeds ───────────> revenue build ┘             │
                                                               v
                                                    exact-parent live-state store
                                                    (receipt, manifest, CAS)
                                                               │
Dune source queries -> tracker -> execution receipt -> budget admission -> cached result
              (separate chain/BBB path; pause gates this work)  │
                                                               └─> acceptance + derived state
```

The network source pipeline is decoupled from the Dune pause; BBB/chain Dune-dependent work is not. The retired legacy refresh runner is not an alternate safe execution path. Revenue source inputs build independently of the live-state receipt interface; publication still needs the store. The public bundle schema and visualization fields are unchanged.

## Health interpretation

Per-domain operational states distinguish: `fresh` (source timestamp inside configured age), `degraded` (stale or reported degraded), `waiting_known` (known execution ID, poll/reconcile only), `unknown_acceptance` (submission may have been accepted but no ID; never resubmit blindly), `budget_blocked`, `paused`, `invalid`, and `source_unavailable`. Process-check time, source `as_of`, and accepted time are separate evidence. Neither a green workflow nor a recent `generated_at` establishes source freshness. Incident fingerprints are deduplication identifiers, not an external alerting service.

## Recovery sequence

1. Freeze write-producing workflows. Preserve `data/current/v3_refresh_state.json`, any store `pending.json`, receipts, and candidate Git objects. Never reset/force-reset the branch or recreate a journal to clear an incident.
2. Inspect only local state and cached remote metadata first. `python scripts/v3_live_state_store.py --help` and `python scripts/v3_refresh_dashboard.py --help` are local help. Use `python scripts/v3_refresh_dashboard.py --cached-only` only when explicitly authorized to run the cached/API-only publish path; this path makes no Dune submit/results calls, but may update source files and publish live-state. It is not a read-only diagnostic.
3. Resolve exact-predecessor receipt/pending conflicts from `scripts/v3_live_state_store.py` documented subcommands/help. Hydration uses a single authoritative live-state revision; do not substitute local files or merge/rewrite journal history. Use `recover` only to complete the same durable candidate under exact-parent rules; if it diverged, stop and review.
4. For a known Dune execution ID, inspect cached tracker/journal metadata and reconcile that exact execution. For unknown acceptance, preserve intent and block replacement until evidence resolves it. Result exports must match the exact execution and intent; cached results can be consumed without a second export.
5. Budget forecast/admission is pure and fail-closed. Current configured policy: monthly target 2,500 credits; emergency guard 1,800; retained reserve and renewal instant `2026-11-04T00:00Z`. Current recorded usage is approximately 1,798.673 credits, leaving essentially no guard headroom. Unknown execution/export liabilities block admission. Do not infer a quote from these figures or treat guard as available spend.
6. Holder-integrity remains paused. Historical selective repair is not implemented as safe automated repair: do not repair/rewrite from unverifiable row sets. Immutable row-level proof, trusted source scope, provider cost evidence, and explicit user approval are prerequisites before any Dune computation/export. Under AGENTS.md, credibly estimate cost and obtain explicit approval before every fresh computation/export; metadata/cached-only reads do not grant that approval.
7. After reviewed repair, verify source as-of timestamps, holder integrity, budget liabilities and journal acceptance, then publish a forward-only full generation. Keep old receipts and journal history. Roll back by a reviewed forward corrective generation, never by force-reset/history rewrite.

## Verification and limits

Run targeted offline suites (no Dune calls):

```sh
python -m unittest tests.test_v3_live_state_store tests.test_v3_store_review
python -m unittest tests.test_v3_execution_tracker tests.test_v3_tracker_boundaries tests.test_v3_acceptance
python -m unittest tests.test_v3_result_cache tests.test_v3_credit_budget tests.test_v3_budget_runtime
python -m unittest tests.test_v3_operational_health
```

Reported test count was 284 at an earlier integration point and may change. Sol implemented the store architecture and hit a usage limit before final integration/signoff; no final Sol signoff is claimed. Astra and DeepSeek review found and fixed seed preservation, derived-file visibility, checkpoint clobbering, missing-execution-ID export and presentation-atomicity issues. Historical findings still needing explicit treatment: A (unknown-acceptance not proven resolved; original latest live snapshot is not proof), E (containment, not exact repair), H (unknown liability blocks), J (no external alert service), K (network decoupled, BBB remains dependent). Do not claim fully production-hardened or ready to close. Config, SQL, visualization settings, accounting, frontend and external adapters were not modified in this scope.
