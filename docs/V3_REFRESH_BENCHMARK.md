# V3 repeat benchmark and operating budget

Production key; medium engine; two batches, three repetitions per query; 54 executions, UTC 2026-10-04 22:51–22:56. Raw sources use a two-hour lookback. Status-only measurements perform no result downloads.

| Query | Batch 1 mean | Batch 2 mean | Six-run mean | Runs/day | Credits/31 days |
|---|---:|---:|---:|---:|---:|
| current | 0.206539 | 0.381176 | 0.293858 | 96 | 874.52 |
| pools | 0.068331 | 0.053143 | 0.060737 | 96 | 180.75 |
| burns | 0.357387 | 0.480134 | 0.418760 | 48 | 623.12 |
| tokenomics | 0.067869 | 0.052299 | 0.060084 | 48 | 89.40 |
| holders | 0.054174 | 0.067923 | 0.061049 | 48 | 90.84 |
| network | 0.068896 | 0.053105 | 0.061001 | 1 | 1.89 |
| commercial | 0.067677 | 0.053065 | 0.060371 | 1 | 1.87 |
| xnet_transfers | 0.058083 | 0.053736 | 0.055909 | 48 | 83.19 |
| bbb_dex | 0.047640 | 0.190935 | 0.119288 | 0 | 0.00 |

Measured compute forecast: **1945.59 credits for 31 days**. Budget 220–250 for source exports and approximately 10 for daily repair overhead: **2,176–2,206 credits/month**, rounded to **2,200**. The 30-minute transfer cadence remains because this is below 2,500. Market/current and pools remain 15-minute; burns, supply and holders 30-minute; revenue/network daily. BBB trade collection is disabled.

Exports are billed by bytes, not data-point count. Budget uses historical daily transfer volume and the overlapping two-hour collector windows, plus daily previous/current-day repair. The six raw benchmarks returned zero rows, so they do not establish a busy-hour export cost; the forecast retains the earlier nonzero historical-volume allowance. Recent normal source costs were 0.0413 and 0.0363 credits, with zero exports. Growth in activity can increase transfer/export costs.

One-off benchmark compute: 7.146337 credits, plus 0.700160 for an initial partial measurement stopped by the production cap. No benchmark exports.

Current-reader range: 0.121647–0.437147 credits, 1.225–4.435 seconds; burn-reader range: 0.111893–0.480134 credits, 0.506–1.341 seconds. Transfer range: 0.030325–0.086142 credits, 0.207–0.746 seconds. BBB range: 0.037008–0.444000, 0.327–4.329 seconds. Raw result counts were zero in all six repetitions. Pool readers produced four rows; holder reader six buckets; burn reader 279 daily rows. High burn costs occur despite unchanged result size, so computation variability matters independently of activity.

The forecast uses pre-cleanup query measurements and does not claim savings from the subsequent shared market snapshot and single-read burn query until production measurements establish them. Cost caps for current and burn readers rise to 0.75 to accommodate measured successful runs; the persistent 3,800-credit account guard and no automatic execution retries remain.

No legacy dashboard queries appear in any GitHub cron. All ten original-dashboard source queries were archived and restored, clearing their native schedules through Dune's documented archive behavior while preserving saved SQL and cached visualizations. Queries: 8879480, 8876594, 8878176, 8876668, 8879837, 8876739, 8877904, 8880465, 8876989, 8876284. One legacy market execution at 23:05 UTC cost 0.057 credits, confirming it still refreshed during the audit. Unused materialized-view source queries 8889723 and 8889854 are now archived; their SQL and materialized snapshots were preserved. Direct materialized-view schedule updates were rejected by the current plan's private-view restriction, so source-query archival stops their execution without changing privacy. Dune docs: https://docs.dune.com/web-app/query-editor/query-scheduler#query-scheduling-limitations.

Account-wide daily credit snapshots use the free Dune usage metadata endpoint. A rolling three-day elapsed-time average triggers an alert above 100 credits/day, once at least approximately one day of history exists. This includes manual executions and exports and handles billing-period rollover. It requires no new paid query.
