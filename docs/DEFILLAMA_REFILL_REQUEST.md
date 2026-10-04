# XNET DeFiLlama registration and refill

Status: feed publication and official adapter tests pass. Production fees/revenue listing registration remains missing. The GitHub connector rejected posting this request to merged PR 9872 with HTTP 403, Resource not accessible by integration. No new adapter PR or CodeRabbit review is required for registration.

The following request is ready to post on https://github.com/DefiLlama/dimension-adapters/pull/9872.

The merged adapter and published feed are working, but XNET is not yet available in the production fees/revenue API. Could the existing XNET listing be wired to the `xnet` fees module and its historical service dates refilled?

Checked 2026-10-04 23:03 UTC:

- `https://api.llama.fi/summary/fees/xnet?dataType=dailyFees` and the corresponding `dailyRevenue` request both return HTTP 400: “Fees for xnet not found, please visit /overview/fees to see available protocols.” The `xnet-mobile` alternative also returns not found.
- The public feed at https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_defillama_revenue.json contains 20 reconciled service-month rows totaling **$119,816.08**. Source payments total $131,816.09; $12,000.01 without payment-date/service attribution remains excluded.
- The merged upstream adapter passes the official runner on three service dates and a day without a recognition entry, plus both adapter and CLI TypeScript checks: https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37242743651.
- Runner dates are window ends: `npm test fees xnet 2025-03-01` reports February 2025 fees/revenue of $1,704.18; `2026-03-01` reports February 2026 $11,915.76; `2026-08-01` reports July 2026 $33,688.91. `2026-07-16` returns zero because there is no service-month recognition entry that day.
- Please refill from **2024-09-30 through 2026-07-31** for Fees, Revenue and their existing holder/protocol breakdowns. Revenue uses the **service-month-end date**, not the carrier payment receipt date. The July $33,688.91 was paid on September 25, so re-reading only recent receipt days will miss it.
- The feed is refreshed daily by the public XNET repository. Future late settlements require refilling their earlier service dates; the adapter currently retains its July 2026 coverage boundary. October's separately reported fiat-operator transfers need period attribution before extending retained-revenue accounting to that activity.

Website: https://www.xnetmobile.com/ · X/Twitter: https://x.com/XNET_Mobile · Docs: https://docs.xnetmobile.com/

No adapter code change or additional CodeRabbit review is needed to enable the already merged history. The remaining step appears to be the server-side fees-module registration and refill.
