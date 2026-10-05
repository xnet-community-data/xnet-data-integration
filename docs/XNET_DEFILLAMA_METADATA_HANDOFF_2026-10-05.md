# XNET DeFiLlama metadata and revenue handoff

Prepared 5 October 2026. Public sources and live API responses checked on this date. This is a preparation pack for the XNET team, not a claim of team endorsement or completed DeFiLlama activation.

## What remains to do

XNET already has a DeFiLlama protocol record and a merged fees adapter. Use the existing record rather than create another listing for the same carrier revenue.

1. Complete the existing record's website and market identifiers. The correct token address is already present, but CoinGecko and CoinMarketCap identifiers are missing.
2. Have DeFiLlama resolve fees ingestion and load the historical service dates. A protocol-to-adapter mapping exists, yet the fees/revenue summaries still return HTTP 400. Public evidence does not identify the internal cause.
3. Agree how earlier service dates will be reprocessed after late settlements or corrections. Updating the public feed does not, by itself, prove that DeFiLlama re-fetches older dates.
4. Obtain XNET's confirmation of metadata, financial methodology, wallet roles and a named ongoing contact. Extending the adapter beyond July 2026 needs additional accounting evidence and an adapter change.

The registration/history request is [posted on PR #9872](https://github.com/DefiLlama/dimension-adapters/pull/9872#issuecomment-5990513239). The [official-token metadata clarification](https://github.com/DefiLlama/dimension-adapters/pull/9872#issuecomment-5990991603) is also posted, as `xnet-community-data`. Neither comment confirms that the requested changes have been applied.

## 1. Live DeFiLlama record

Source: [GET /protocol/xnet](https://api.llama.fi/protocol/xnet). The following fields were read directly from its response.

| Field | Observed value | Requested action |
| --- | --- | --- |
| `id` | `8867` | Retain this existing record |
| `name` / `symbol` | `XNET` / `XNET` | Retain unless team explicitly prefers a display-name update |
| `address` | `solana:xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L` | Already correct; retain |
| `gecko_id` | `null` | Set to `xnet-mobile-2` |
| `cmcId` | `null` | Set to `32753`, using DeFiLlama's expected field type |
| `url` | Empty string | Set to `https://xnetmobile.com/` |
| `description` | `Decentralized Connectivity.` | Team may approve the fuller description below |
| `category` | `DePIN` | Retain |
| `twitter` | `XNET_Mobile` | Retain |
| `logo` | `https://icons.llamao.fi/icons/protocols/xnet` | Existing asset; replace only with team-approved artwork if needed |
| `chain` / `chains` | `Off Chain` / `["Off Chain"]` | Retain for carrier-revenue accounting |
| `dimensions.fees` | `xnet` | Mapping exists; investigate missing fees ingestion/history |
| `module` | `dummy.js` | Existing metadata-only record; do not invent a TVL adapter |
| `audits` / `mcap` | `null` / `null` | Audit status not established; market metadata incomplete |

Both [dailyFees](https://api.llama.fi/summary/fees/xnet?dataType=dailyFees) and [dailyRevenue](https://api.llama.fi/summary/fees/xnet?dataType=dailyRevenue) returned HTTP 400, stating that XNET fees were not found. The general listing's existence is therefore separate from successful fees ingestion.

## 2. Verified identity and public links

| Item | Collected value | Evidence / qualification |
| --- | --- | --- |
| Project | XNET; commercial name XNET Mobile | [Official website](https://xnetmobile.com/) and [network overview](https://docs.xnetmobile.com/mobile/overview) |
| Current token | XNET Mobile, ticker `XNET` | Official token documentation and current market listings |
| Token network | Solana mainnet | Official network docs; Solscan token page |
| Current mint | `xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L` | [Official token overview](https://docs.xnetmobile.com/foundation/xnet-token/overview) |
| Explorer | `https://solscan.io/token/xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L` | [Solscan](https://solscan.io/token/xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L) |
| CoinGecko ID | `xnet-mobile-2` | [Current CoinGecko page](https://www.coingecko.com/en/coins/xnet-mobile-2), which shows this API ID and a Polygon-to-Solana migration notice |
| CoinMarketCap ID | `32753` | [Current CMC page](https://coinmarketcap.com/currencies/xnet-mobile/) shows UCID 32753 and the Solana token |
| Website | `https://xnetmobile.com/` | Live official site; `https://xnet.company/` redirects to it, providing migration evidence |
| Documentation | `https://docs.xnetmobile.com/` | Linked from the official site; use deep token/network pages as supporting evidence |
| X | `https://x.com/XNET_Mobile` | Linked from the official website; matches existing DeFiLlama handle |
| Community | `https://discord.gg/xnet` | Linked from official site; resolves to the XNET Discord invite |
| Foundation | `https://xnet.foundation/` | Linked from official site and [Foundation overview](https://docs.xnetmobile.com/foundation/overview) |
| Official public API repository | `https://github.com/xnetmobile/api` | Linked as Developer Hub from the official website |
| Community integration repository | `https://github.com/xnet-community-data/xnet-data-integration` | Maintains this data pipeline; do not describe it as the team's official GitHub organization |
| Community Dune dashboard | `https://dune.com/xnet_community_data/xnet-network-revenue-buy-burn-v3` | Current community-built dashboard; team should confirm whether to endorse/link it |
| Existing website Dune link | `https://dune.com/xnet/xnetmobile` | Currently linked by the official site; distinct from the community V3 dashboard |

The [DeFiLlama coin-price endpoint](https://coins.llama.fi/prices/current/coingecko:xnet-mobile-2,solana:xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L) already returns an XNET price for both identifiers, with the same price/timestamp and 8 decimals for the Solana mint. A new pricing adapter is not indicated by this test. Setting the protocol's market identifiers is a separate request. It may enable additional market-data presentation, but DeFiLlama controls the supported views and ratios.

**Proposed short description for team approval**

> XNET is a decentralized wireless network that uses community-deployed carrier-grade Wi-Fi infrastructure to offload mobile data traffic. Contributors earn XNET tokens for supporting the network, while carrier service revenue supports the ecosystem's published buyback-and-burn and liquidity policies.

This is a proposed description, based on official network/token documentation. Retaining the current brief description is also possible. Avoid listing every carrier, current device count or market cap in static metadata without a dated source and team confirmation.

## 3. Best way to link the revenue to the official token

Ask DeFiLlama to update **protocol ID 8867 / slug `xnet`**, retain its current Solana address and `fees: xnet` mapping, and fill `gecko_id`, `cmcId` and website. Explicitly ask for its existing token view and fees/revenue/holder-revenue views to use this same current-token identity wherever supported.

Do not request a second XNET listing, use the retired Polygon identity, or rely on ticker matching alone. A ticker is not a unique identifier. The original PR mentions `/token/XNET`; this research could not verify that older route, so use `/protocol/xnet` as the confirmed record and ask maintainers to associate any existing token view.

Preserve **Off Chain** as the carrier-revenue source classification. The XNET token is on Solana, but the fees are telecom service receipts, not Solana network transaction fees. Changing the fees adapter's chain solely to attach a token would change the meaning of the accounting.

DeFiLlama's official [metadata update guide](https://docs.llama.fi/list-your-project/how-to-update-project-metadata) directs listing updates to **metadata@defillama.com**, supported by public proof. Metadata is reviewed manually. The [current repository template](https://github.com/DefiLlama/dimension-adapters/blob/master/pull_request_template.md) gives the same route. The PR thread is appropriate for the existing adapter's ingestion/backfill problem; the email is the documented route for metadata changes. Coordinate the two requests and reference the same record and PR.

## 4. Complete metadata checklist for XNET's team

The repository's template asks for the fields below. It says the full new-listing form is only for new protocols. Because XNET is already listed, this is a handoff checklist, not a requirement to restart its listing.

| Field | Prepared answer | Team confirmation needed |
| --- | --- | --- |
| Display name | Keep `XNET` | Preferred public name if changing |
| Ticker, token address | Verified `XNET` and Solana mint above | Notify us of any subsequent migration |
| CoinGecko / CMC IDs | `xnet-mobile-2` / `32753` | None needed to identify these current public listings |
| Website, X, docs | Links above | Confirm preferred canonical website and public contact |
| Description | Proposed text above, or retain existing | Approve wording |
| Category | DePIN | Retain one main category |
| Logo | Existing DeFiLlama logo available | Supply approved high-resolution square artwork or approve existing image |
| Treasury addresses | Candidate BBB addresses below | Official role, owner/entity and explorer link for each address |
| Chain | Off Chain revenue; Solana token | Keep the distinction in the metadata handoff |
| Audit links | No audit report verified in this research | Provide reports and audited contract scope, or confirm none available; do not label unaudited without evidence |
| Current TVL / TVL methodology | Outside this fees-only integration | No TVL number should be invented from revenue, treasury balances, hardware or token-pool liquidity |
| Oracle providers and integration proof | This adapter consumes reconciled USD amounts, not an on-chain price oracle | Separate actual protocol oracle usage, if any, from DeFiLlama pricing and this feed |
| `forkedFrom` | Unknown / not established | Confirm whether applicable |
| Official GitHub org | Public `xnetmobile/api` link verified | Confirm organization(s) for code-activity tracking; community repo remains separate |
| Referral program | Not established | Team to confirm if relevant to requested metadata |
| Ongoing contact / authorization | Community maintainer `xnet-community-data` | Team-designated contact and confirmation that this public methodology may be represented as team-approved |

DeFiLlama requests a high-resolution logo suitable for rounded borders; the template does not specify a mandatory pixel size. A square PNG and original SVG are practical deliverables, not official format requirements. The official [Press Kit route](https://docs.xnetmobile.com/mobile/press-kit) was found, but a direct fetch returned HTTP 402, so its current downloadable assets were not verified.

The public revenue sheet labels these addresses as **XNET Burn Wallets**:

- `6UE1gdvgFPbu8REp5YWKRkC5CZiXgwd7iEfTAqwbzUqV`
- `B9SXSuPwpzmYUgk1GRfuW9R9QDMJ6P9SfTybSoawHiLj`
- `5QsyByFVJcg7oN76Ma26KEDFQdHt1tsiVExK94zURzfd`

The last address is classified by community analysis as the current BBB/liquidity wallet. That is not sufficient to label every address a Foundation treasury or every inbound transfer a completed burn. Ask the team to provide a role table for treasury, BBB execution, protocol-owned liquidity, operations and deployer payments. No private keys or credentials are needed for this metadata pack.

## 5. Revenue evidence and the accounting information still needed

**Available now**

- [Public source sheet](https://docs.google.com/spreadsheets/u/0/d/1NebqJ876SNlO4xPihfJWzsH-V0xzgeA-Qw8i5VcHDU4/htmlview?pli=1#gid=1205842263), tab `gid=1205842263`.
- [Normalized public feed](https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_defillama_revenue.json), schema version 1, `settled_service_period` basis.
- [Merged adapter](https://github.com/DefiLlama/dimension-adapters/blob/master/fees/xnet.ts), `version: 2`, `pullHourly: false`, Off Chain, starting 2024-09-30, current upper coverage boundary 2026-07-31.
- Daily sheet refresh scheduled for 09:17 UTC, currently 10:17 UK time during BST. Source cadence is daily; revenue observations are monthly. This schedule is not DeFiLlama's ingestion or historical-refill schedule.
- 20 recognized service months totaling **$119,816.08**, spanning September 2024 through July 2026. These are recognized rows, not proof that every month in that range is settled.
- Source payments received total **$131,816.09**. **$12,000.01** remains excluded for insufficient date/service attribution. Unsettled forecasts are also excluded.
- Service revenue is booked at the service month's end after settlement reconciliation. July's **$33,688.91**, received 25 September, belongs to 31 July.
- [Successful revenue sync](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37262542166) and [official runner / TypeScript validation](https://github.com/xnet-community-data/xnet-data-integration/actions/runs/37242743651).

The merged adapter reports Fees, User Fees, Revenue, Holders Revenue and Protocol Revenue. Its holder/protocol split is a **policy allocation**, not a measurement of completed on-chain purchases and burns. Before 22 May 2025 it applies 80% BBB / 20% operations. From that date it applies 60% BBB / 20% protocol-owned liquidity / 20% operations. The date and percentages are encoded in the current merged implementation. The general tokenomics documentation still describes 80% BBB, while an official XNET post describes XIP-12's 20% liquidity allocation. Obtain the authoritative XIP-12 approval/effective-date reference and team confirmation rather than treating the older general page as the complete current policy.

**Ask the team for a monthly reconciliation table**

| Column | Why it is needed |
| --- | --- |
| Service month / covered dates | Assign activity and costs to their actual period |
| Gross carrier service fees in USD | Establish the top-line carrier fees |
| Receipt date, amount, currency and conversion basis | Verify settlement and link bundled payments to months |
| Fiat operator / other supplier share, service month and payment evidence | Calculate Supply-Side Revenue and retained Revenue |
| Retained share and allocation regime/effective date | Verify the income statement and XIP-12 boundary |
| BBB and liquidity allocations versus executed transfers | Keep policy allocation distinct from implementation |
| Corrections and previously unmatched payments | Resolve gaps without duplicate counting |
| Public evidence URL / team attestation | Establish source ownership and verifiability |

DeFiLlama's [data definitions](https://docs.llama.fi/analysts/data-definitions) distinguish carrier/user fees, supplier payments, retained revenue and token-holder value. Supply-side payments to operators must not be counted as retained protocol revenue. Its accrual rule also supports attributing late-paid service fees to the service period. The current July cutoff must remain until later-period supplier attribution is established. Public invoicing/settlement evidence, or a team-owned verifiable dataset agreed with maintainers, would strengthen the feed's provenance.

Ask DeFiLlama to confirm the refill method for late settlements, corrections and historical gaps. After activation, verify historical totals and the holder/protocol breakdowns against the feed. A daily scheduled run alone cannot guarantee that older changed rows get processed. Sparse month-end observations and excluded recent months also mean recent-day charts may show zero or missing data even after valid historical ingestion; do not promise continuously rising daily fees.

## 6. Ready-to-send team message

> I've set up XNET's public revenue feed and the DeFiLlama adapter has been merged. I've also requested the historical revenue load and asked them to link it to the official Solana XNET token using CoinGecko `xnet-mobile-2` and CoinMarketCap `32753`.
>
> I've gathered the metadata and prepared the email for DeFiLlama. Could you confirm the preferred name/description, approved logo, official treasury/BBB/liquidity wallet roles, and a team contact? For revenue coverage after July, we also need each fiat-operator payment attributed to its service month and confirmation of the current allocation policy and effective date. The pack includes the verified links and the exact remaining fields, so most of this is confirmation rather than starting from scratch.

This message is prepared, not sent to an XNET team member. No recipient has been identified in this task.

## 7. Metadata email draft

To: metadata@defillama.com

Subject: XNET metadata update and current Solana token association — protocol 8867

Hello DeFiLlama team,

Please update the existing XNET record, protocol ID **8867**, at https://defillama.com/protocol/xnet. Its merged fees adapter is `fees/xnet.ts`, from https://github.com/DefiLlama/dimension-adapters/pull/9872.

The public protocol API already shows the correct token address and `dimensions.fees: "xnet"`, but its website, CoinGecko ID and CoinMarketCap ID are empty. Please set:

- Website: https://xnetmobile.com/
- CoinGecko ID: `xnet-mobile-2` — https://www.coingecko.com/en/coins/xnet-mobile-2
- CoinMarketCap ID: `32753` — https://coinmarketcap.com/currencies/xnet-mobile/

Please retain the existing XNET name, XNET symbol, DePIN category and `XNET_Mobile` X handle, and retain the token address `solana:xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L`.

The official token documentation confirms the mint: https://docs.xnetmobile.com/foundation/xnet-token/overview. CoinGecko identifies the current Solana token and notes the old Polygon contract migration. The previous website https://xnet.company/ redirects to https://xnetmobile.com/, providing evidence for the website update.

Please associate XNET's fees/revenue and holder-revenue history with this same current-token identity and any existing token view wherever supported. Carrier service revenue remains **Off Chain** accounting, while the token is on **Solana**. We are requesting an update to the existing record, with each carrier-revenue flow counted once.

The fees and revenue summary APIs still report that XNET is not found, despite the existing adapter mapping. The history/ingestion request and tested dataset are in the PR thread, including a request to refill 2024-09-30 through 2026-07-31 and confirm how late settlements' earlier service dates should be reprocessed.

Metadata clarification: https://github.com/DefiLlama/dimension-adapters/pull/9872#issuecomment-5990991603

History request: https://github.com/DefiLlama/dimension-adapters/pull/9872#issuecomment-5990513239

Please let us know if you need any additional proof or a direct confirmation from XNET's team.

Thank you,

XNET community data maintainer (`xnet-community-data`)

**Draft only.** This email uses verified public identifiers and retains the current description/logo. It does not imply team affiliation or endorsement. A team-approved sender may replace the signature and provide their contact details. A description or logo replacement should be added only after the team supplies/approves it.

## 8. Optional expansion after the core listing works

DeFiLlama's [Token Rights submission](https://docs.llama.fi/list-your-project/token-rights) is a separate opportunity. It asks about governance/revenue decisions, buybacks, burns, distributions, entity structure, IP/domain ownership, fundraising, equity revenue capture, treasury addresses and financial reports. [Submission form](https://forms.defillama.com/token-rights). This is not required merely to associate the current token with the fees listing.

The official Foundation overview distinguishes the Foundation from the commercial company. Obtain the team's current legal/entity and governance evidence before filling ownership or equity-revenue fields. Similarly, reconcile supply/emissions before a future unlocks request: the current official tokenomics page and market sites show different supply figures. Do not copy a stale market-site maximum supply into an official team submission.

## Sources

- [DeFiLlama metadata update guide](https://docs.llama.fi/list-your-project/how-to-update-project-metadata)
- [DeFiLlama dimensions guide](https://docs.llama.fi/list-your-project/other-dashboards)
- [Current metadata template](https://github.com/DefiLlama/dimension-adapters/blob/master/pull_request_template.md)
- [DeFiLlama data definitions](https://docs.llama.fi/analysts/data-definitions)
- [DeFiLlama Token Rights framework](https://docs.llama.fi/list-your-project/token-rights)
- [Live XNET protocol metadata](https://api.llama.fi/protocol/xnet)
- [DeFiLlama token pricing identifiers](https://coins.llama.fi/prices/current/coingecko:xnet-mobile-2,solana:xNETbUB7cRb3AAu2pNG2pUwQcJ2BHcktfvSB8x1Pq6L)
- [Official network description](https://docs.xnetmobile.com/mobile/overview)
- [Official token mint](https://docs.xnetmobile.com/foundation/xnet-token/overview)
- [Official Foundation overview](https://docs.xnetmobile.com/foundation/overview)
- [Official tokenomics page](https://docs.xnetmobile.com/foundation/xnet-token/tokenomics)
- [Official XIP-12 liquidity post](https://x.com/XNET_Mobile/status/1995530832447242481) — search-indexed official statement, not a complete effective-date record
- [Official website](https://xnetmobile.com/) and [old-domain redirect](https://xnet.company/)
- [CoinGecko current token](https://www.coingecko.com/en/coins/xnet-mobile-2)
- [CoinMarketCap current token](https://coinmarketcap.com/currencies/xnet-mobile/)
- [Merged fees adapter](https://github.com/DefiLlama/dimension-adapters/blob/master/fees/xnet.ts) and [public feed](https://raw.githubusercontent.com/xnet-community-data/xnet-data-integration/main/data/xnet_defillama_revenue.json)

Public links were researched without changing XNET's website, market listings or DeFiLlama backend. Only the PR clarification was posted; the metadata email and team message remain drafts.
