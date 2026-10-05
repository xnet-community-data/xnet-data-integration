# XNET DeFiLlama registration and historical refill

The revenue sheet pipeline and merged adapter pass their checks. XNET's public fees/revenue API still returns HTTP 400. The registration and historical refill request was posted as `xnet-community-data` on October 5, 2026, and the published comment was verified:

https://github.com/DefiLlama/dimension-adapters/pull/9872#issuecomment-5990513239

Pending: DeFiLlama must connect the existing listing to the fees module, load the historical service dates, and confirm how earlier dates are reprocessed for future late settlements. The request is posted; production fees/revenue activation is not yet confirmed.

---

Could you connect the existing [XNET listing](https://defillama.com/protocol/xnet) to the merged `fees/xnet.ts` module and backfill its historical fees and revenue?

Checked **2026-10-05 04:13 UTC**:

- Both `https://api.llama.fi/summary/fees/xnet?dataType=dailyFees` and the corresponding `dailyRevenue` endpoint return HTTP 400: “Fees for xnet not found, please visit /overview/fees to see available protocols.”
- The [public feed](https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_defillama_revenue.json) contains **20 reconciled service months totaling $119,816.08**. It is generated from the public XNET revenue sheet, refreshed daily at 09:17 UTC. The complete download, normalization, reconciliation, publication and Dune label workflow [passes](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37262542166).
- The unchanged merged adapter [passes the official historical runner and both TypeScript checks](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37242743651). Window-end tests return February 2025 $1,704.18; February 2026 $11,915.76; July 2026 $33,688.91; and zero for a day with no recognition entry.
- Please backfill **2024-09-30 through 2026-07-31**, including the holder/protocol breakdowns. The feed books revenue on the **service-month-end date**, rather than the later carrier payment date. July revenue was paid on September 25, so processing only recent payment dates will miss the history.
- Source payments total $131,816.09; $12,000.01 without payment-date/service attribution remains excluded. Future late settlements require reprocessing their earlier service dates. The adapter retains its July 2026 coverage boundary; October's separately reported fiat-operator transfers require service-period attribution before extending retained-revenue accounting beyond that history.

Website: https://www.xnetmobile.com/ · X: https://x.com/XNET_Mobile · Documentation: https://docs.xnetmobile.com/

No new adapter PR or CodeRabbit review is required to load the already merged history.
