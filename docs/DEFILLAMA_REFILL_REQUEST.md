# XNET DeFiLlama historical refill

XNET's updated `fees/xnet.ts` adapter was merged into DefiLlama on **October 6, 2026**. Current daily execution is live, but the public historical series still contains pre-update stored values and therefore needs an explicit historical refill.

## Refill request

Please refill `fees/xnet` from **2024-09-01 through the latest available service day**, including Fees, Revenue, Supply-Side Revenue, Holders Revenue and Protocol Revenue.

The adapter reads the public daily feed:

https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_defillama_revenue.json

### Source-faithful restatement policy

This is a **community analytics pipeline**, not an independent accounting publisher. Its purpose is to reflect XNET's own published accounting as transparently and reproducibly as possible.

The canonical source is the XNET team's public revenue sheet. When that source changes a historical value, the community pipeline intentionally carries the revision forward on the next rebuild. It does **not** freeze an older snapshot simply because it was previously published. The affected service-month accrual, settlement reconciliation and daily history are deterministically recomputed from the current source.

That behavior matters for this refill because the team revised historical 2026 source values after the adapter PR was merged. For example, the published sheet changed January 2026 service revenue from **$9,707.31 to $16,275.62** and March 2026 from **$14,957.00 to $15,000.00**. Those are source-sheet accounting revisions and are therefore reflected by the community feed.

This should be treated the same way as later carrier settlements: the historical daily rows are revised to the latest published source truth rather than adding a second revenue event.

### Accounting behavior

- Closed service months use the team's official projected revenue until a carrier settlement resolves them.
- A confirmed settlement replaces/reconciles the prior accrual; it is never added on top.
- April-June 2026 are reconciled to the final historical **$15,000** carrier-cap settlements.
- XIP-13.1 fiat-operator payouts inherit the service month from the reconciled carrier settlement that funds the payout.
- The July 2026 fiat allocation is therefore attributed to July service, not the October source-row month.
- Current incomplete periods use measured network offload and the conservative effective API-GB rate until stronger source data is published.
- Historical source-sheet revisions are intentionally propagated on every rebuild.

### Why a refill is required

After merge, DefiLlama began running the new adapter and current rolling values populated, but previously stored historical dates were not fully rewritten. The public all-time series therefore remains below the canonical feed even though current daily execution is working.

A full refill from the adapter start date is the safest option because the source is date-addressable, deterministic and already validates continuity, duplicate dates, non-negative values and accounting identities before publication.

No methodology change is being requested here. This is a request to recompute stored DefiLlama history using the **already merged adapter** and the source-faithful current feed.

---

## Metadata clarification posted

The [official-token metadata request](https://github.com/DefiLlama/dimension-adapters/pull/9872#issuecomment-5990991603) was posted and verified on October 5, 2026. A fresh live protocol API check identifies XNET as protocol ID `8867`, with the correct Solana mint and `dimensions.fees: "xnet"` already present. The remaining observed metadata gaps are an empty website and null `gecko_id` / `cmcId`; the fees/revenue summary endpoints still return HTTP 400. Requested identifiers are CoinGecko `xnet-mobile-2` and CoinMarketCap `32753`, with website `https://xnetmobile.com/`. Carrier fees remain Off Chain accounting, while the token is on Solana.

The [full sourced metadata and XNET-team handoff](XNET_DEFILLAMA_METADATA_HANDOFF_2026-10-05.md) includes the field checklist, team confirmations, financial coverage requirements, and a draft email to the documented `metadata@defillama.com` address. That email and the team message have not been sent. Metadata completion and successful historical fees ingestion remain unconfirmed.
