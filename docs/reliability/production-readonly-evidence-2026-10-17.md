# Production read-only evidence — 2026-10-17

**Scope:** evidence collection only. No workflow/script execution, Dune computation/export/refresh, dashboard writes, or remote repository writes. `gh api user` verified `xnet-community-data`; authenticated GitHub requests explicitly targeted `xnet-community-data/xnet-data-integration`.

## Git refs and correct baseline comparisons

Locally observed refs: `origin/main=ca8a076ad863516bcd50732f2fd2a31153dcefc6`; `origin/live-state=0fe79005c20fab45976d1cb1577bcbdf8acb4de0`.

The requested anchors are branch-specific. Do not compare main baseline to live-state as a single delta:

- `b9d67d3681ff8304a4c3078e24f72927faf40ba4` is the supplied main audit baseline. Its own commit changes three revenue/offload/epoch files (stat: 114 insertions, 24 deletions). No validated baseline-to-current main delta is available in this evidence.
- `ebd7174b3ad6e308f0cead94d4f6eb749507611c` is the supplied live-state audit baseline. Its own commit changes the refresh state, chain health and six derived-history files (stat: 9 insertions, 9 deletions). No validated baseline-to-current live-state delta is available in this evidence.
- A subsequent attempt to collect both branch-specific diffs timed out. The earlier cross-branch structural comparison is withdrawn and must not be used as evidence for either delta.
- Current main’s most recent supplied scheduled run checks out `ca8a076…`; its job log explicitly says `Dune globally paused; deferring network source calls` and `Refresh remains paused after a previous failure; no queries submitted.` Green workflow conclusion means the pause-aware job ran and published its artifact; it does not mean a refresh happened.

## GitHub Actions observations

Authenticated read, window requested from 2026-10-14 UTC, Actions API runs limited to 100 results. Returned 29 `Refresh XNET V3` successes and one `Monitor Dune credits` success, with two failed runs at `2026-10-15T21:37:22Z`: run `38811268992` Refresh XNET V3 and `38811268871` Test Dune refresh recovery. This is the observed result page, not proof there were no older failures beyond the page limit.

Run `39035934460` (Oct 17 13:17 UTC) full log retrieved. Stages explicitly restore production state, defer network sources because Dune is globally paused, skip query submission because the refresh remains paused, then upload `xnet-v3-refresh-state` artifact. Run summary says success; the actual execution work was deliberately skipped. The action-log endpoint returns a ZIP; do not print raw response bodies into reports.

**Unclosed gap:** failure logs for runs `38811268992` and `38811268871` were requested via `gh run view --log-failed` but timed out as a combined command. Root causes remain unverified from those logs. Per-run exact durations are not summarized here; timestamps are available from the Actions API and can be recomputed without triggering a workflow.

## Dune saved-query metadata (official MCP; metadata only)

Fetched `getDuneQuery` for IDs 8894185, 8894037, 8894132, 8893945, 8894038. No result retrieval or execution was made.

| ID | Name | version | visibility | latest execution metadata |
|---|---|---:|---|---|
| 8894185 | XNET — Live Current Snapshot | 18 | private, team_id 1013457 | `01M5386G8NBK82YEDF2HX379TW`, completed |
| 8894037 | XNET V3 — Canonical Transfer Events HOT | 10 | private, team_id 1013457 | `01M53853C0DC9HYXY75NTRRJ48`, state null |
| 8894132 | XNET V3 — BBB DEX Transactions HOT | 11 | private, team_id 1013457 | `01M5385ZWGDQBPDMTAQK8T4Z8N`, state null |
| 8893945 | XNET V3 — Transfer Reconciliation Manifest | 4 | private, team_id 1013457 | `01M50QVNESBXMT31576X7058MG` |
| 8894038 | XNET V3 — Historical Transfer Repair | 4 | private, team_id 1013457 | `01M4YADHX63B5TQ7VPYGGM9M1K` |

The transfer and BBB source SQLs bound partition dates and timestamp lookbacks; transfer source default is 2 hours and BBB defaults to 2 hours (description documents production use of 26 hours near 01:00 UTC). Manifest default covers seven completed UTC days. Repair uses bounded day offsets. Query metadata confirms query text/versions and latest IDs only; no new freshness or result-success claim is inferred here. Metadata descriptions say manifest cost cap/repair constraints; the live journal records the actual over-cap quarantine and manual clearance requirement.

## Dashboard parity fixture status

No dashboard mutation was attempted. Astra reports a prior read-only `getDashboard` snapshot with 58 visualization widgets, 13 text widgets, 0 parameter widgets; widget IDs reportedly include visualization IDs around 1293… and widget IDs around 2517…. This agent did not receive the dashboard ID, full widget-ID mapping, positions, or `getVisualization` outputs, so a comprehensive parity fixture with visualization options **cannot yet be captured or claimed**. Do not put embed URLs or API-key-bearing URLs into evidence. To close this gap, obtain the dashboard ID (or owner handle + slug) and perform `getDashboard`, then `getVisualization` for each of the 58 visualization IDs; record sanitized full chart configuration, titles, positions, and text-widget bodies without secret-bearing embed fields. Do not fetch query results.

## Published DeFiLlama feed spot check

Read-only `https://defillama.com/protocol/xnet` page fetch succeeded; it presents fees/revenue and dated history, and names `https://api.llama.fi/protocol/xnet` as the public API. The live-state raw GitHub path `.../live-state/data/xnet_defillama_revenue.json` returned 404, so it is not the correct published current path (main-side path may differ). No direct API endpoint status/body validation was completed. Treat the rendered site view as a successful page fetch, not proof that the machine-readable endpoint is healthy or current.

## Operational interpretation

Committed live-state at the inspected ref is paused, automatic retry false, with negative holder balance (`-51,907.828371 XNET`) and over-budget historical manifest quarantine requiring manual clearance. Successful Actions conclusions observed on main reflect a pause-aware no-submit workflow. No rollout/unpause trigger should rely on a green workflow alone; require current journal and independently verified source freshness, holder-integrity recovery, and explicit reconciliation clearance. All compute and publishing remain frozen.
