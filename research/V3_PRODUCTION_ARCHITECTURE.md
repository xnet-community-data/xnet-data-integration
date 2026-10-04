# XNET V3 Production Architecture

## Objective

Build a public XNET performance dashboard that is:

- accurate
- current
- inexpensive to maintain
- easy to extend
- understandable to non-technical viewers
- detailed enough for serious community and industry analysis

The architecture optimises sources, not individual visualisations.

A raw source should normally be read once and reused for many metrics.

---

## 1. Market

Source: DexScreener.

Refresh: every 15 minutes.

Persist timestamped observations for:

- XNET price
- liquidity by pool
- total observed DEX liquidity
- 24-hour volume
- buy/sell transaction counts
- market-cap/FDV provider values for reconciliation
- pool composition

This source provides high-frequency public market state without requiring
continuous Dune DEX scans.

---

## 2. XNET transfer stream

Source: `tokens_solana.transfers`.

Refresh target: hourly.

Hot window: previous two hours with overlap and deduplication.

Daily repair: previous three days.

This is the shared blockchain source for:

- holder balance updates
- holder count
- circulating-supply changes
- burns
- BBB burns
- BBB wallet balance/activity
- future wallet-flow analytics

Holder count, supply and burns must not create separate raw-chain scans.

---

## 3. BBB DEX transactions

Source: `dex_solana.trades`.

Scope: primary BBB wallet only.

Refresh target: hourly if benchmark cost is acceptable.

Store transaction-level economic BBB activity rather than every XNET DEX
trade in the market.

Use it for:

- verified BBB XNET purchases
- BBB XNET sales if any
- net XNET acquired
- approximate DEX trade value
- venue
- quote asset
- transaction identity

Routed transactions may contain multiple decoded DEX legs. The canonical
BBB layer therefore reduces XNET-facing legs by transaction before storing
the result.

---

## 4. Full XNET DEX data

No recurring production schedule.

The all-XNET DEX query is retained for manual forensic and research use only.

Possible future uses include:

- trader analysis
- route analysis
- whale behaviour
- exact historical DEX investigations
- venue research

These do not justify continuous production cost today.

---

## 5. Network

Sources: XNET offload and device APIs.

Refresh: once daily.

Used for:

- daily offload
- cumulative offload
- total devices
- operational devices
- device growth
- offload per operational device
- monthly network growth

Do not fabricate zero observations between sparse device measurements.

---

## 6. Revenue

Refresh: once daily.

Preserve the distinction between:

- projected revenue
- payment received
- projected BBB allocation
- recorded BBB transfers
- outstanding transfers

Revenue stages are asynchronous and must not be collapsed into one synthetic
cash-flow series.

---

## 7. Emissions

The emission schedule is deterministic.

Do not repeatedly fetch or query it.

Generate the epoch calendar locally and reuse it.

Scheduled emissions must remain distinct from actual minted/claimed tokens.

---

## Dashboard editorial rule

The dashboard has no arbitrary target visual count.

A visual exists only if it helps explain:

1. network scale or productivity
2. commercial/revenue performance
3. market/token health
4. circulating supply and holders
5. BBB activity
6. emissions/token economics
7. source freshness or methodology

Avoid trading-terminal detail that does not help explain XNET performance.
