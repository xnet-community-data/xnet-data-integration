# Recovery integration — 19 October 2026

## Production versus work in progress

The publication/receipt hardening from PRs #40 and #41 is deployed. The second
round of financial and domain-isolation changes is deployed through PR #42
(`8de4763c278f16b11e9fc2aad62797ec2ac0e661`). Its cached-only production refresh
`39329773992` succeeded without new Dune computation or result exports.

## Approved paid operation

The user explicitly approved **one** execution of saved query `8894038` v4 for
14 October and **one** full retrieval, estimated at five credits, with a
one-operation exception to the 1,800-credit emergency guard. No retries,
other days, standing schedule or downstream refresh was authorized.

- Execution: `01M5B0JH2659VTCAGS9EBB1T4P`.
- Parameters: `oldest_days_ago=5`, `newest_days_ago=5`.
- Complete result: 198 rows, all dated 14 October; no projection/filter/sample.
- Reported compute: **0.342 credits**. Billing read afterward: **1,799.015**,
  renewal **4 November 2026, 00:00 UTC**, quota 4,000.
- Export charge: **unknown**. Pretty-printed artifact size is not metered wire
  size, and an equal aggregate usage delta is not charge-level settlement proof.
- Durable intent, ID and result-review commits and artifact hashes are recorded
  in `tests/fixtures/dune_repair_20261014_receipt.json`.
- A subsequent exact-predecessor checkpoint `2125bfbf081501d33e8d2bacca792ad089034a6a`
  preserves the known compute and the forensic unknown export obligation. This
  is not a claim that the old MCP read had the new pre-export checkpoint API.

## Validation outcome

The canonical predecessor contains 3,143 rows and no 14 October rows. The complete
day would add 198 rows without deleting old events. Offline full-holder reduction
of that candidate still fails for BBB at **−58,683.7110614 XNET**. The candidate
has **not** been published or consumed as a successful complete repair.

The earlier 17-transaction fixture was condensed: its balance deltas were **not
sufficient proof of individual routed transfer legs**. Full finalized captures
have since been obtained for all 17 transactions. The independent parser decodes
both SPL Token and Token-2022 instructions and reconciles every XNET account's
pre/post flow; routed intermediate legs are preserved rather than invented from
net balance changes. No amount scaling or seed reset is permitted.

The user subsequently approved exactly two additional one-day operations for
15 and 16 October, five credits of estimated risk each including retrieval:

- 15 October: `01M5B1FMNJYSZ5AZ2FK7WTZ9Z9`, **301 rows**, **0.273 compute
  credits**. All 177 existing day rows match exactly; 124 rows are additive.
- 16 October: `01M5B1HQBFK0629R29YBC6QYWW`, **FAILED**: upstream Dune stored
  view transaction-log metadata was unavailable. Cost was not supplied. No retry
  was made or authorized; no full-day completeness is asserted for 16 October.
- Both reads had durable pre-read financial attempts. No wire/export charge was
  supplied by MCP, so the financial amounts remain unknown, not zero.

Adding the complete 14/15 October evidence and five independently decoded missing
16 October rows yields **3,470 rows (+327)**. The strict full production reducer,
**without stale-holder fallback**, succeeds. Independent conservation and row
review passed. The append-only generation was atomically published on 20 October:
`fa9150f014d85024df9934f5b54c5a0f4e39dee4`. See
`ADDITIVE_REPAIR_ACCEPTANCE_2026-10-20.md`. This historical repair does not establish
current chain coverage or current Dune presentation freshness.

## Review and rollout boundaries

- Known paid executions must survive cost-outlier refusal; break only future
  admission, not result recovery.
- Export financial outcome and source/cache acceptance are independent. A
  successfully metered but invalid payload must retain its known transport cost
  while remaining quarantined against repeat retrieval.
- BBB source failure/recovery and holder integrity must not become unrelated
  source or presentation failures. Exact-predecessor durability failures remain
  fatal; never publish a compensating stale generation.
- The unintegrated broad recovery planner and instruction-aware scoped-repair
  draft are not operational merely because their pure tests pass. Historical
  replacement requires every writer to enforce the durable receipt fences.
- Ordinary budget admission is not explicit spending authority. No further Dune
  execution/export or standing schedule is authorized by the one-day approval.
- Public dashboard layout and external DeFiLlama adapter remain unchanged.

## Still required for mission completion

Obtain complete remaining source coverage when the Dune provider fault is
resolved, with operation-specific approval for retries/catch-up/presentation
refresh. Install a separately authorized sustainable operating plan; the cost
analysis is not spending authority. Verify actual source-as-of timestamps rather
than using workflow success as freshness evidence. The public Dune dashboard is
**not yet verified fresh**.
