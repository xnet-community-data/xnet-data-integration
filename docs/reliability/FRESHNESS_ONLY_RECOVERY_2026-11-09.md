# Freshness-only recovery mode (approved 2026-11-09)

## Purpose

A small, explicit opt-in mode for recovering presentation freshness when the
default runner cannot be used for a bounded period. It buys a strictly
narrower subset of what the normal runner already buys — it widens nothing and
introduces no new query, budget, or clock behaviour.

## Approved scope

`python3 scripts/v3_refresh_dashboard.py --freshness-only` permits exactly:

- **Hot collection:** one `xnet_transfers` run, judged by the EXISTING planner
  fences — a requested lookback must satisfy
  `lookback <= hot_catchup_limit(CONFIG) + configured overlap_hours`
  (12h default + 4h = 16h). The overlap is never reduced and no catch-up
  configuration changes.
- **Charts:** only `current`, `burns`, `tokenomics`, `holders`, `pools` — five
  already-configured status-only queries — in the usual due/forced/attempt
  order, still bounded by `max_presentation_queries_per_run` (5).

Everything else defers to the normal runner with durable evidence:

- The BBB collector and every other source are skipped outright.
- Any out-of-allowlist known in-flight execution ID stays durable and receives
  ONLY the free metadata-only startup reconciliation
  (`tracker.reconcile(metadata_only=True)` polls `/status`). This mode never
  exports its result, never executes it anew, and never cancels it.
- A pending `xnet_transfers` execution whose recorded window exceeds the fence
  defers BEFORE any `execute`/`export`: the ID, parameters and metadata are
  preserved untouched (metadata reconciliation may only add terminal evidence,
  never remove fields).
- Paid historical reconciliation (the aggregate manifest scan, UTC-day
  recovery) defers; the free indexer/witness and holder-seed probes still run
  so integrity evidence stays visible for the normal runner.

## Honest deferral evidence

Deferred work is recorded, never hidden:

- `state["reconciliation"]["manifest_deferred_at_utc"] /
  ["manifest_deferred_reason"]` — the diverged paid reconciliation decision
  (`FRESHNESS_PAID_DEFERRED` reason string).
- `state["freshness_only_recovery"]["deferred_repairs"]` — one entry per
  would-be paid repair plan (`source_reconciliation_plan` recurses once to
  compute the would-be plan, records `{key, mode, lookback_hours,
  deferred_at_utc}`, deduplicates per pair, then returns within-scope nothing).
- `state["source_errors"][...]` with `freshness_pending_window_hours` — the
  wide pending transfer deferral.
- No clock, coverage, due-repair evidence, quarantine or pause state is reset
  or cleared by this mode. The run receipt stores `started_at_utc` and
  `completed_at_utc` only.

Default runs do not touch `freshness_only_recovery` at all.

## Unsafe combinations (refused before any state load or Dune access)

- `--freshness-only` + `--cached-only` → rejected: cached_only must always
  mean zero fresh Dune compute. The cached-only branch is the workflow's first
  branch, so in CI cached-only wins if both inputs are ever set accidentally.
- `--freshness-only` + `--benchmark` / `--benchmark-sources` → rejected.
- Allowlist drift (a configured rename empties the allowlist) is refused ONLY
  when the mode is invoked — never at import, so the default runner cannot be
  blocked by a future configuration change.

## Invariants carried over

Price/budget admission, paid-result reuse, integrity checks, per-source
attempt tracking, honest health output, unknown-execution-ID fences, and the
never-replace-a-pending-operation rule behave exactly as in the default
runner. The flag only narrows which specs may enter the paid lanes; default
call sites unchanged (`allow_paid` is passed only when the mode is active, so
existing two/three-argument callers stay compatible).

## Offline regression

`tests/test_v3_freshness_only.py` (22 tests) proves: conflict refusals,
allowlist constants and lazy drift validation, due-repair deferral evidence,
free probes kept while paid recovery defers, weekly-manifest/day-repair paths
unreached in the mode, the five-chart allowlist with the per-run bound, the
  13h-lookback allowance vs 13h-raw-gap deferral fence, the wide pending transfer fence,
out-of-scope pending IDs staying durable with status-only API traffic
(`assert_free_api_only`), and the unchanged normal path
(`test_no_freshness_flag_runs_the_equivalent_normal_scope`).

Workflow: `workflow_dispatch` input `freshness_only` (default `false`) maps to
the CLI flag; branch order in `.github/workflows/refresh-v3-history.yml` is
cached-only → freshness-only → resume.
