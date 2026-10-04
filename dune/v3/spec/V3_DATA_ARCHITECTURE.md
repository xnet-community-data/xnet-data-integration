# XNET V3 data architecture

## Design rule

Expensive or external work happens once per domain at the cadence that information can materially change.
Everything else reads stored canonical outputs.

### Fast domain
- Market pool base: 15 minutes
- Market snapshot: derived from stored pool base
- Future on-chain transfer/trade incremental bases: benchmark hourly first, then move to 15 minutes only if marginal cost is small

### Slow domain
- Offload mirror: daily
- Device mirror: daily
- Revenue mirror: daily
- Commercial/network derived metrics: daily
- Emissions schedule: deterministic; refresh only when policy changes

## Canonical components

1. `xnet_v3_protocol_config`
2. `xnet_v3_wallet_registry`
3. `result_xnet_v3_market_pools_live`
4. `xnet_v3_market_snapshot`
5. future `xnet_v3_transfer_events`
6. future `xnet_v3_dex_trades`
7. future `xnet_v3_supply_holders`
8. future `xnet_v3_bbb_daily`
9. future `xnet_v3_offchain_daily`
10. future `xnet_v3_commercial_monthly`
11. future `xnet_v3_token_economics_monthly`
12. future `xnet_v3_current_snapshot`

## Failure isolation

A dashboard visualization must never directly call Vercel, DexScreener, or scan full-history Solana tables.
Fix the relevant canonical base once; downstream presentation remains unchanged.
