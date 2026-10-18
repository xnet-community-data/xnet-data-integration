# Reliability delta matrix

This matrix distinguishes implementation/evidence from remaining liabilities. It is not a deployment attestation; deployment is pending.

| Area | Implemented / verified locally | Remaining risk or proof required |
|---|---|---|
| Live-state authority | Schema-2 local receipts; exact-parent compare-and-swap; candidate manifest; checkpoint receipts; history is append-only (no rebase/reset/rewrites). | Deployment of all writers together and remote runtime proof pending. A lost acknowledgement is not proof that an earlier ambiguous operation resolved. |
| Hydration ownership | Closed owned-file registry; code-owned seed inputs preserved; receipt inventory restores/validates published derived data. | Verify deployed remote has authoritative canonical history before rollout; never bootstrap empty journal. |
| Tracker lifecycle | Checkpoint durability barrier; ambiguous HTTP outcome retained as intent; known IDs remain bound to existing work. | Unknown-acceptance cannot be cleared without evidence. Preserve it; do not infer no charge/no accepted execution from missing local ID. |
| Result export | Exact execution/request binding; atomic request intent and accepted data/consumed marker; two-entry/6 MB cache; repeated export blocked on unresolved intent or missing cached bytes. | Validate provider/account export behavior only after explicit approved computation. Bounds do not establish an export cost quote. |
| Reconciliation | Manifest aggregates are advisory; destructive repair gated on immutable row proof. | Targeted historical repair is intentionally not implemented; trusted source scope, provider-cost evidence and explicit approval absent. Finding E is containment, not exact repair. |
| Budget | Pure forecast/admission; target 2,500, emergency guard 1,800; reserve retained; renewal instant 2026-11-04T00:00Z; fail-closed on unknown liability. | Current usage ~1,798.673 is at the guard. Cost estimates require comparable evidence; do not treat remaining ~1.327 as safe spend. Finding H remains a blocker when liability is unknown. |
| Operational health | Per-domain status separates process check, source as-of, acceptance; incident IDs stable across timestamps; recovery action is emitted. | Incident IDs are not wired to an external alert service (finding J). Workflow success/generated timestamp alone never proves freshness. |
| Source paths | Network feed path decoupled from Dune pause; revenue inputs are independently built and published through receipt interface. | BBB/chain pipeline remains Dune-dependent (finding K). Current holder-integrity pause remains in force. |
| Legacy runner | Legacy charged runner removed/retired from the safe execution authority. | Do not invoke a legacy path as a bypass; verify deployed workflows use unified tracker. |
| Review and tests | Store architecture implemented by Sol; Astra/DeepSeek findings fixed for seed preservation, derived visibility, checkpoint clobbering, cache missing-execution-ID, presentation atomicity. Earlier aggregate reported 284 tests. | Sol hit usage limit mid-integration; no final Sol signoff. Test count may change. Deployment and production proof outstanding. |
| Dashboard/accounting | Public bundle schema, visualization fields, SQL/config/dashboard visualization settings, accounting and external adapters untouched. | No dashboard frontend mutation or new dashboard parity proof in this scope. |

## Prior finding disposition

- **A — unresolved:** original fetched live snapshot does not prove the previous unknown-acceptance state was resolved. Keep unresolved until exact execution/intent evidence.
- **B/C/D — fixed per integration review:** see review/test evidence; do not generalize beyond the covered cases.
- **E — contained, not fully resolved:** immutable row-level proof is required; targeted historical repair remains unimplemented.
- **F/G — fixed per integration review:** result cache is bounded and replay-safe, but not proof of provider-side billing/acceptance.
- **H — fail-closed:** unknown execution/export liabilities prevent budget admission.
- **I — preserved:** known execution IDs remain attached to their original work.
- **J — gap:** stable incident IDs deduplicate locally; no external alert transport exists.
- **K — partial:** network inputs remain independent of pause; BBB remains Dune-dependent.
- **L — preserved:** no configuration, SQL, visualization-settings or accounting change.

## Safe operational boundary

Offline/unit tests and cached journal/receipt inspection are permitted without Dune computation. The explicit `--cached-only` runner path avoids Dune submission and result retrieval but can still change and publish data, so it is not inherently read-only. Any fresh Dune execution, rerun, refresh or export requires a credible cost estimate and explicit user approval first. No deployment, unpause, historical repair, forced journal reset or external write is authorized by this matrix.
