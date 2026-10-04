# XNET V3 refresh benchmark

Measured on October 4, 2026. These are internal operating notes; they do not appear on the dashboard.

## Result

The GitHub DUNE_API_KEY works on the medium engine. Both raw sources and a complete production refresh succeeded. No replacement key is needed.

Estimated recurring use: **about 2,000–3,400 credits per month**, including compute, exports and daily chain repair. This is a projection from measured runs and current traffic, not a month of observed billing. The account has 4,000 included credits. Automated execution stops before the **3,800-credit account guard**, with a further 15-credit execution reserve.

| Month length | Latest source benchmark | Higher observed transfer cost |
|---|---:|---:|
| 30 days | ~2,025 credits | ~3,295 credits |
| 31 days | ~2,092 credits | ~3,405 credits |

Source costs vary considerably between executions. The upper column uses the higher observed transfer cost rather than assuming every execution gets the cheapest run.

## Refresh configuration

| Dataset | Interval | Mechanism |
|---|---|---|
| Price, valuation and pools | 15 minutes | External market snapshots and shared Dune readers |
| Wallet USDC observation | 15 minutes | Solana RPC |
| Transfers, holders, burns, supply and BBB trades | 30 minutes | Two shared raw queries, local reduction and cached readers |
| Chain repair | Daily | Previous-day and current-day partitions, bounded export |
| Revenue, network and commercial history | Daily | Canonical inputs and cached readers |

Intervals, query IDs, export limits and spend limits are configurable in `config/v3_refresh.json`. Revenue and network inputs remain independent of market and chain collection.

## Measured compute

Presentation costs below come from successful GitHub production run [37238292262](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37238292262).

| Query | Query ID | Credits |
|---|---:|---:|
| Current metrics | 8894185 | 0.182382 |
| Burn history | 8895090 | 0.109084 |
| Tokenomics history | 8895091 | 0.107101 |
| Holder distribution | 8895092 | 0.093253 |
| DEX pools | 8895093 | 0.098783 |
| Network history | 8895088 | 0.098137 |
| Commercial history | 8895089 | 0.163309 |
| Transfers, daily repair window | 8894037 | 0.289794 |
| BBB trades, daily repair window | 8894132 | 0.124265 |

The bounded source benchmark [37237521578](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37237521578) measured 0.105353 credits for transfers and 0.260265 for BBB trades. An earlier medium-engine transfer run cost 0.987588 credits. The first regular catch-up measured 0.084853 and 0.031324 respectively.

The forecast uses presentation costs at their configured intervals, 48 daily source executions, plus a conservatively additional daily repair execution. The repair actually replaces a regular run, so this slightly overstates compute.

## Export billing

The current [Dune billing documentation](https://docs.dune.com/api-reference/overview/billing) charges Analyst API exports at **10 credits per MB**, rather than the older datapoint model. Presentation executions are polled for status; their rows are not exported by the scheduler.

The forecast uses the October 2–3 transfer traffic (304 and 107 rows/day), six average BBB transactions/day, approximately six overlapping exports of each day's events including daily repair, and response metadata overhead. This gives roughly **7.40 export credits/day**, or 222–229 credits/month. Actual charges depend on exported bytes and traffic.

The runner records response bytes and result metadata separately, estimates exports from the larger size, and records execution cost from Dune. Datapoint limits remain a separate guard on result size. Zero-row sources are not downloaded.

## Production checks and recovery

- Full production refresh completed, published live state twice, and refreshed all seven shared presentation queries.
- Eight focused tests pass, including spend limits, pause behavior, cadence, export accounting and rollback after a failed reduction.
- Daily repair fills late-indexed transfers missed by the older collector. Revisions to partial daily totals are retained.
- Failed reductions restore canonical and derived data before publishing a persistent pause.
- Supply history is regenerated from the dated daily ledger. Its latest value reconciles to current circulation.
- BBB burn history reconciles to the current total; treasury supply burns remain outside BBB charts.
- The original dashboard is unchanged. V3 has 54 visualizations and 12 text widgets, with no grid overlaps.
- Freshness displays only Source, Freshness and Data Points. Reader-facing text contains no credit budgets, refresh intervals or “verified” labels.
- BBB USDC history starts December 30, 2025; unchanged daily balances carry forward from the account's last balance change.
- Circulation history starts September 30, 2024, reconstructed from the September 24, 2026 checkpoint and the 61-wallet exclusion list. Treasury burns reduce both outstanding supply and exclusions, leaving circulation unchanged.
- Scheduled emissions use 2.5 million XNET per 14-day epoch, prorated by calendar-month length. The redundant standalone emissions chart is removed.
- Circulating Supply Growth is a percentage line, measured against the preceding month-end observation.

## Comparison with the older dashboard and Helium

The updated dashboard connects network activity, operational devices, revenue, liquidity, emission intensity and burns. The original branding, introduction/link area, revenue settlement explanation and source methodology are represented in V3.

Helium's [network snapshot](https://www.helium.com/snapshot) also highlights connected users, carrier breakdowns and geographic coverage. Those remain useful future additions when equivalent XNET datasets are available. They are not inferred from device counts.

The Network, Usage & Revenue Growth visualization has three explicitly named series and no grouping column or “All” series. Dune's own chart controls are outside the exposed configuration; a full rendered browser review was not performed.

## Remaining source access

The XNET offload/device APIs return HTTP 402, so network metrics retain dated last-good observations. A daily reader refresh cannot make an unavailable upstream API current. Restored upstream access is required.

The DeFiLlama issue is deferred to the next task.

Current billing usage was 107.549 credits at the last account check, including setup, manual research, historical reconstruction and tests. That figure is account-wide, rather than a recurring dashboard charge. Historical reconstruction is one-time work and is not scheduled.

