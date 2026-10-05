# XNET Public Data Integration Guide

> **Community-maintained resource.** This repository documents public and project-supplied XNET data sources for third-party analytics integrations. It is not an official XNET corporate repository. The linked upstream sources remain the source of truth.

**Purpose:** a compact source-of-truth for third-party data providers integrating XNET network, device, revenue and on-chain metrics.

**Last validated:** 29 September 2026

This guide points providers to the upstream public sources. It is not intended to replace those sources. Integrations should fetch the source data directly and preserve the distinctions between network usage, billing/revenue data and on-chain activity.

## Quick reference

| Metric | Source | Format | Authentication |
| --- | --- | --- | --- |
| Network offload | `https://xnet-offload-scraper.vercel.app` | JSON | None observed |
| Total and operational devices | `https://xnet-total-devices-api.vercel.app` | JSON | None observed |
| Revenue and billing GB | XNET public revenue spreadsheet | Google Sheet / CSV | Public |
| Token, burns, transfers and liquidity | Solana | On-chain | Public |
| Market price | Provider's preferred market source | Market data | Provider-specific |

## 1. Network offload

**Base URL**

`https://xnet-offload-scraper.vercel.app`

This feed reports daily network offload in gigabytes.

| Endpoint | Purpose |
| --- | --- |
| `/api/data` | All stored daily records, newest first |
| `/api/latest` | Newest available daily record |
| `/api/data?days=N` | Records for the last N calendar days, including the current day |
| `/api/summary?days=N` | Total, daily average, date range and record count |
| `/api/data?start=YYYY-MM-DD&end=YYYY-MM-DD` | Inclusive date-range query |

Core fields are `day`, `gigabytes` and `formattedGigabytes`. Use `gigabytes` for calculations. `formattedGigabytes` is display text.

### Integration note

The current calendar day's record may be incomplete while traffic is still accumulating. For a finalized headline daily metric, use the latest completed calendar day rather than assuming `/api/latest` is a closed period.

Do not fill a missing day with zero unless XNET explicitly defines the missing observation as zero.

The API does not expose timezone metadata. Preserve the returned `day` value rather than applying an assumed timezone conversion.

### Fail-safe offload cache

`data/xnet_offload_api.json` is a normalized completed-day mirror used by the accrual builder. The sync job merges newly returned completed observations into the cache rather than replacing history wholesale. If the upstream API is temporarily unavailable, the job preserves the last valid measured cache. The revenue builder may then use the bounded short-outage fallback described above for the trailing gap only; the cache itself remains measured-source data and is not polluted with imputed observations. When the API resumes, new measured observations are incorporated and affected provisional revenue is rebuilt automatically.

## 2. Total and operational devices

**Base URL**

`https://xnet-total-devices-api.vercel.app`

| Endpoint | Purpose |
| --- | --- |
| `/api/latest` | Latest total and operational device counts with scrape timestamps |
| `/api/history?limit=N` | Historical observations, newest first. Supported limit: 1 to 100 |

Core fields:

- `scrapeDate` — observation date
- `scrapeTime` — upstream scrape timestamp
- `totalDevices` — total device count reported by the feed
- `totalOperational` — operational device count reported by the feed
- `createdAt` — API record creation timestamp

Preserve the upstream term **operational**. Do not silently relabel it as "active in the last 24 hours", "online", or another activity-window definition unless XNET publishes that definition.

Historical observations are scrape records, not a guaranteed gap-free daily series. A missing observation must not be treated as zero devices.

## 3. Revenue and billing GB

**Human-readable sheet**

https://docs.google.com/spreadsheets/u/0/d/1NebqJ876SNlO4xPihfJWzsH-V0xzgeA-Qw8i5VcHDU4/htmlview?pli=1#gid=1205842263

**Machine-readable CSV export**

https://docs.google.com/spreadsheets/d/1NebqJ876SNlO4xPihfJWzsH-V0xzgeA-Qw8i5VcHDU4/export?format=csv&gid=1205842263

For revenue calculations, use the spreadsheet's **`GB per month`** series. 

Relevant spreadsheet rows include:

- `GB per month`
- `WiFi Revenue (Projected)`
- `Blended Rate per GB (Projected)`
- `WiFi Payment (Received)`
- `WiFi Payment Date`
- `Total Emitted Tokens`
- `Projected Buy & Burn`
- `Transferred to Buy & Burn`
- `Transferred to Fiat Operators`
- `Balance Outstanding to Transfer`

### Revenue classification

`WiFi Revenue (Projected)` is a projected source figure. It should not be presented as cash received or verified realized revenue.

`WiFi Payment (Received)` is the separate payment-received series.


### Reconciled DeFiLlama revenue feed

A derived, machine-readable settlement-reconciliation feed is published at:

`data/xnet_defillama_revenue.json`

It is generated by:

`scripts/build_defillama_revenue.py`

The source-faithful `xnet_revenue_monthly.json` remains unchanged. The derived feed reconciles recorded carrier payments to the underlying service month or contiguous service months only when the amounts match uniquely.

Both timelines are retained:

- the service period in which the WiFi revenue was generated
- the later date on which the carrier payment was reported received

For example, the $33,688.91 payment received on 25 September 2026 reconciles exactly to July 2026 WiFi service revenue.

Payments that cannot be uniquely reconciled remain explicitly unattributed rather than being silently forced into a service period. Exact amount matching is preferred. When no unique exact match exists, the established roughly two-month carrier-payment cadence can be used conservatively: a payment below the remaining official projection is treated as a partial settlement of the service month two months earlier; a payment above the remaining projection is automatically accepted only when the overage is within 15%. The feed is rebuilt from the latest source sheet, so revised or newly dated carrier-payment entries supersede earlier provisional source states.

### DeFiLlama daily accrual and reconciliation

DeFiLlama's 24-hour, 7-day and 30-day comparisons require a daily flow series. XNET's carrier settlements arrive later than the service activity, so the public feed uses an accrual-and-reconciliation model rather than booking an entire month on one settlement day or showing zero activity while a carrier invoice is pending.

The daily series follows this hierarchy:

1. **Settlement-confirmed month.** The confirmed service-month revenue is distributed across the actual daily offload observations in proportion to each day's measured GB. The daily values are rounded with a cent-preserving allocation so the month sums exactly to the confirmed carrier revenue.
2. **Closed month with an official XNET projection but not yet fully settled.** The official `WiFi Revenue (Projected)` amount is distributed across that month's actual daily offload observations using the same proportional method. The amount remains explicitly provisional until settlement.
3. **Newer days before an official monthly projection exists.** Daily network offload is multiplied by the latest conservative effective revenue per API GB. The effective rate is calculated as the latest complete month's official projected WiFi revenue divided by that month's summed daily offload API GB. It is also capped at the published blended billing rate.

This last step is intentionally conservative. Daily network-offload GB and revenue-sheet billing GB are related but not identical. In the recent complete May-August 2026 comparison, summed daily API offload exceeded the revenue-sheet billing GB by roughly 6-8%. Using the raw billing `$/GB` directly on network-offload GB would therefore overstate the live projection. The effective API-GB rate absorbs that difference.

The model is designed to revise historical provisional values, not to preserve a forecast after better information arrives. When an official monthly projection is published, the live daily estimate for that service month is replaced and rescaled to the official projection. When a carrier settlement can be reconciled to the service month, the daily values are rescaled again to the settlement-confirmed amount. In each case the relative day-to-day shape comes from measured offload rather than a blind equal-per-day average.

Settlement attribution is fail-closed. Exact amount reconciliation to one service month or a contiguous run of service months is preferred. If no unique exact match exists, the established two-month settlement cadence is used only against the service month two calendar months earlier. A payment below that month's remaining official projection is a partial settlement and confirms only the amount actually received. A final payment above the remaining projection is accepted automatically only when the overage is within 15%. Anything outside those rules stays unattributed for review. This handles real partial carrier payments without confusing them with extra revenue.

If the daily offload API itself is temporarily stale, the model can bridge a **short outage only**. For a trailing gap of at most 14 completed days, missing trailing days are provisionally assigned the average offload of the latest seven measured days. These rows are explicitly flagged as imputed and are replaced as soon as measured observations return. This keeps current 24-hour/7-day comparisons from collapsing to zero during a brief source outage without pretending the imputed GB are measured data. If the source remains stale for more than 14 completed days, the model fails closed and stops extending the synthetic series.

The current calibration and recent previous-month-rate backtest are published inside `data/xnet_defillama_revenue.json` under `projection_model`, so third parties can audit both the rate and the forecast error.

Payment receipt dates remain separate from service accrual dates. Projected values must not be described as cash received. Partial receipts confirm part of an already-accrued service month and are never added on top of that month's provisional revenue. When the source sheet is revised with a newer payment amount or date, the next rebuild follows the latest source and recalculates confirmation status automatically.

The adapter should consume `daily_data` from the feed and should not impose a hard-coded final service date. October 2026 fiat-operator transfers still need service-period attribution before they can affect retained-revenue accounting.

Run both normalization stages with:

`python3 scripts/sync_all.py`

## 4. Revenue settlement pipeline

The revenue spreadsheet represents several different stages of the revenue and Buy & Burn process. They should not be collapsed into a single same-month metric.

The practical flow is:

`GB per month`
→ `WiFi Revenue (Projected)`
→ `WiFi Payment (Received)`
→ `Transferred to Buy & Burn`
→ on-chain BBB, liquidity and burn activity

There is a settlement delay between projected WiFi revenue and cash being received. Historically, payments have commonly arrived roughly two months after the underlying revenue period, although the exact delay varies.

A further timing difference can exist between payment receipt and funds being transferred into the Buy & Burn process.

For this reason:

- do not treat projected revenue as cash already received
- do not treat a month's projected revenue as that same month's Buy & Burn transfer
- do not derive BBB transfers directly from daily network-offload GB
- for source-faithful monthly revenue reporting, preserve the spreadsheet's `GB per month` and `WiFi Revenue (Projected)` series
- for DeFiLlama's live daily accrual only, measured daily offload may be used with the conservative calibrated effective API-GB rate described above
- once an official monthly projection or settlement is available, replace the provisional live estimate for that month rather than adding the two together
- preserve `WiFi Payment (Received)` as a separate settlement-stage metric
- preserve `Transferred to Buy & Burn` as a separate downstream cash-flow metric
- preserve `Transferred to Fiat Operators` separately as that route becomes used
- treat `Balance Outstanding to Transfer` as the source spreadsheet's outstanding-transfer accounting field

Blank spreadsheet cells should be treated as **null / not reported**, not automatically converted to zero.

## 5. On-chain data

XNET is on Solana.

**Current XNET mint**

`xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L`

### Burn and BBB wallets

The public XNET revenue spreadsheet lists the following addresses under **XNET Burn Wallets**:

- `6UE1gdvgFPbu8REp5YWKRkC5CZiXgwd7iEfTAqwbzUqV`
- `B9SXSuPwpzmYUgk1GRfuW9R9QDMJ6P9SfTybSoawHiLj`
- `5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd`

The address ending `URzfd` is additionally identified by current community on-chain analysis as the **current primary BBB/liquidity wallet**.

That BBB/liquidity role is a community-derived classification rather than an explicit role label in the revenue spreadsheet. Because the address is used in the BBB/liquidity flow, an inbound transfer should not automatically be classified as a completed burn. Providers should inspect the subsequent Solana transactions when distinguishing funding, liquidity activity and completed burns.

For token supply, transfers, burns, treasury movements, DEX liquidity and other chain-native metrics, providers should derive the metric from Solana rather than copying a dashboard snapshot where practical.

Market data can be sourced independently. CoinGecko currently lists XNET Mobile under `xnet-mobile-2`:

https://www.coingecko.com/en/coins/xnet-mobile-2

## 6. Metric separation

The following distinctions are deliberate and should be preserved:

| Metric | Use |
| --- | --- |
| Daily offload API GB | Network usage and the day-to-day shape of DeFiLlama accrual |
| Revenue-sheet `GB per month` | Source-faithful monthly revenue/billing GB |
| `WiFi Revenue (Projected)` | Official provisional service-month revenue |
| DeFiLlama live daily projection | Daily API GB × latest conservative effective API-GB revenue rate, until superseded |
| `WiFi Payment (Received)` | Recorded payments received and reconciliation evidence |
| `totalDevices` | Total devices from the device feed |
| `totalOperational` | Operational devices from the device feed |
| Solana transactions | On-chain token, burn, treasury and liquidity analytics |

Do not multiply daily network-offload GB by the raw spreadsheet billing rate. The network API has recently reported more GB than the revenue/billing series. The DeFiLlama live estimator therefore uses a separately calibrated effective API-GB rate, publishes that rate and its backtest, and later reconciles the provisional daily values to the official monthly projection and ultimately to settlement-confirmed service revenue.

## 7. Validation snapshot

A full endpoint check was run on 29 September 2026.

- Offload `/api/data` returned HTTP 200 and 793 records.
- The offload history covered 29 July 2024 through 29 September 2026 with one unique record for every calendar day in that interval.
- Offload `/api/latest`, `?days=1`, seven-day summary and explicit date-range queries all returned HTTP 200.
- Device `/api/latest` returned 6,629 total devices and 5,839 operational devices.
- Device history returned valid results at limits 1, 30 and 100.
- The Google Sheet CSV export returned HTTP 200 and the expected revenue rows.

These values are a validation snapshot, not fixed network constants. Consumers should query the live sources.


## 8. Project references

- XNET website: https://www.xnetmobile.com/
- XNET contact: https://www.xnetmobile.com/contact

For source ownership, commercial definitions or formal project confirmation, third-party providers should use XNET's official contact channels.

---

### Maintenance principle

Keep this document small. When an upstream schema or definition changes, update the source definition here and avoid creating a second competing copy of the metric.


### API consumption

No public SLA or rate-limit policy is currently documented for the network endpoints. Third-party consumers should cache responses and poll at a reasonable frequency rather than making unnecessarily frequent requests.
