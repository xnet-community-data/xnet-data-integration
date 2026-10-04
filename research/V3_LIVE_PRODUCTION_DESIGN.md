# XNET V3 Live Production Design

## Current state

### Market and public headline metrics

Dune query 8894185 uses LiveFetch to combine:

1. DexScreener current XNET market state
2. verified XNET chain state from the `live-state` Git branch

Target refresh:

- 15 minutes

Measured benchmark:

- approximately 0.069 credits per execution on 2026-10-04

### Chain state

Two narrow Dune queries:

- XNET transfers
- BBB-wallet DEX trades

Target refresh:

- 30 minutes
- two-hour overlap
- deterministic local deduplication

Measured combined benchmark:

- approximately 0.066 credits per refresh on 2026-10-04

Derived from the transfer feed:

- circulating supply
- holder state
- BBB burns

Derived from BBB DEX:

- verified recent BBB purchases
- DEX value
- BBB transaction history

## Persistence

`main` is the human/code branch.

`live-state` is a machine-maintained branch containing current canonical and
derived production state.

The branch is force-updated as a single orphan commit. This prevents
high-frequency refreshes from creating thousands of commits in the main
repository or an indefinitely growing Git history.

## History

High-frequency current state is not inserted into Dune upload tables.

Daily historical facts will later be batched into a single Dune table at a
much lower cadence.

This avoids the minimum per-insert credit charge associated with high-frequency
Dune Upload API inserts.

## Production cadence

- market/current-state LiveFetch: 15 minutes
- transfer state: 30 minutes
- BBB DEX: 30 minutes
- network/offload/devices: daily
- revenue: daily
- historical fact persistence: daily
- emissions: deterministic
- all-XNET detailed DEX scan: manual research only
