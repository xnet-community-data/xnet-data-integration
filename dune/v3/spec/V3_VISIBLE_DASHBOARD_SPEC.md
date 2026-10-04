# XNET V3 visible dashboard specification

V3 stays private until all overlapping V2 metrics reconcile and the supply/holder methodology passes QA.

The public dashboard should be rich but non-redundant. Store many more fields than we visualize.

## 1. Market & Token

1. **XNET Price** — counter
2. **Market Cap** — counter; use reconstructed circulating supply once verified, otherwise clearly flag provider/raw during private QA
3. **FDV** — counter
4. **DEX Liquidity** — counter
5. **24h DEX Volume** — counter
6. **Circulating Supply** — counter; reconstructed and reconciled before publication
7. **XNET Holders** — counter; unique positive-balance owners, not token accounts
8. **Market Cap / Projected WiFi Revenue ARR** — counter
9. **XNET Market History** — line chart; price plus DEX liquidity, dual axes

Underlying only: pool-level liquidity, venue share, quote-asset share, buy/sell transactions, holder concentration, holder thresholds, provider-vs-reconstructed market-cap QA.

## 2. Network

10. **Latest Daily Offload** — counter
11. **All-Time Network Offload** — counter
12. **Total Devices** — counter
13. **Operational Devices** — counter
14. **30-Day Avg Daily Offload** — counter
15. **Monthly Network Offload** — column chart
16. **Device Growth** — line chart, total + operational
17. **Offload per Operational Device per Day** — line chart

Underlying only: operational ratio, 7d/30d device growth, 30d offload growth, record days/months, data-completeness metrics.

## 3. Revenue & Commercial Economics

18. **Projected WiFi Revenue ARR** — counter
19. **Outstanding Transfer Balance** — counter
20. **Projected WiFi Revenue vs Payment Received** — line chart
21. **Projected WiFi Revenue per Operational Device** — line chart
22. **Projected Blended Revenue per GB** — line chart
23. **Cumulative Commercial Economics** — line chart: projected revenue, recorded payments, projected BBB allocation, recorded BBB transfers

Underlying only: payment/projected ratio, BBB-transfer/projected-BBB ratio, actual-network-GB revenue ratio, network-vs-revenue-sheet GB divergence.

## 4. Verified Buy & Burn

24. **Latest Verified Burn** — counter/date
25. **30-Day Verified Burn Total** — counter
26. **Total Verified BBB XNET Burned** — counter
27. **Max Supply Burned by Verified BBB** — counter
28. **Daily Verified XNET Burns** — column chart

Underlying only: all on-chain burns, non-BBB burns, burn initiators, burn frequency, median burn size, latest burn amount, transaction count, BBB share of all burns.

## 5. Emissions & Token Economics

29. **Current Scheduled Operator Emissions / 14-Day Epoch** — counter
30. **Scheduled Operator Emissions** — column/step chart across regimes
31. **Verified BBB Burns vs Scheduled Operator Emissions** — grouped monthly column chart, same XNET unit
32. **Scheduled Emissions per Network Offload GB** — line chart

Underlying only: emissions/device/day, burn/emissions ratio, cumulative burn/emissions ratio, scheduled emissions less verified burns, next reduction date/rate.

## Methodology / freshness

Use text widgets, not extra charts, for:
- last successful source refresh
- source freshness
- source provenance
- current supply methodology
- raw/provider vs reconstructed market metrics
- exact definitions for offload vs revenue-sheet GB

Target visible visualizations: **32**, down from 36 in V2 while adding materially richer data.
